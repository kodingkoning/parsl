from __future__ import annotations
import logging
import time
import math
from typing import List

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from parsl.dataflow.dflow import DataFlowKernel
    from parsl.dataflow.task_status_poller import PollItem

from parsl.executors.base import ParslExecutor, HasConnectedWorkers

from typing import Dict
from typing import Callable
from typing import Optional
from typing import Sequence
from typing_extensions import TypedDict

# this is used for testing a class to decide how to
# print a status line. That might be better done inside
# the executor class (i..e put the class specific behaviour
# inside the class, rather than testing class instance-ness
# here)

# smells: testing class instance; importing a specific instance
# of a thing that should be generic


from parsl.dataflow.executor_status import ExecutorStatus

from parsl.executors import HighThroughputExecutor
from parsl.providers.provider_base import JobState

logger = logging.getLogger(__name__)


class ExecutorIdleness(TypedDict):
    idle_since: Optional[float]


class Strategy(object):
    """FlowControl strategy.

    As a workflow dag is processed by Parsl, new tasks are added and completed
    asynchronously. Parsl interfaces executors with execution providers to construct
    scalable executors to handle the variable work-load generated by the
    workflow. This component is responsible for periodically checking outstanding
    tasks and available compute capacity and trigger scaling events to match
    workflow needs.

    Here's a diagram of an executor. An executor consists of blocks, which are usually
    created by single requests to a Local Resource Manager (LRM) such as slurm,
    condor, torque, or even AWS API. The blocks could contain several task blocks
    which are separate instances on workers.


    .. code:: python

                |<--min_blocks     |<-init_blocks              max_blocks-->|
                +----------------------------------------------------------+
                |  +--------block----------+       +--------block--------+ |
     executor = |  | task          task    | ...   |    task      task   | |
                |  +-----------------------+       +---------------------+ |
                +----------------------------------------------------------+

    The relevant specification options are:
       1. min_blocks: Minimum number of blocks to maintain
       2. init_blocks: number of blocks to provision at initialization of workflow
       3. max_blocks: Maximum number of blocks that can be active due to one workflow


    .. code:: python

          active_tasks = pending_tasks + running_tasks

          Parallelism = slots / tasks
                      = [0, 1] (i.e,  0 <= p <= 1)

    For example:

    When p = 0,
         => compute with the least resources possible.
         infinite tasks are stacked per slot.

         .. code:: python

               blocks =  min_blocks           { if active_tasks = 0
                         max(min_blocks, 1)   {  else

    When p = 1,
         => compute with the most resources.
         one task is stacked per slot.

         .. code:: python

               blocks = min ( max_blocks,
                        ceil( active_tasks / slots ) )


    When p = 1/2,
         => We stack upto 2 tasks per slot before we overflow
         and request a new block


    let's say min:init:max = 0:0:4 and task_blocks=2
    Consider the following example:
    min_blocks = 0
    init_blocks = 0
    max_blocks = 4
    tasks_per_node = 2
    nodes_per_block = 1

    In the diagram, X <- task

    at 2 tasks:

    .. code:: python

        +---Block---|
        |           |
        | X      X  |
        |slot   slot|
        +-----------+

    at 5 tasks, we overflow as the capacity of a single block is fully used.

    .. code:: python

        +---Block---|       +---Block---|
        | X      X  | ----> |           |
        | X      X  |       | X         |
        |slot   slot|       |slot   slot|
        +-----------+       +-----------+

    """

    def __init__(self, dfk: "DataFlowKernel") -> None:
        """Initialize strategy."""
        self.dfk = dfk
        self.config = dfk.config
        self.executors = {}  # type: Dict[str, ExecutorIdleness]
        # self.executors = {}  # type: Dict[str, Dict[str, Any]]
        self.max_idletime = self.dfk.config.max_idletime

        for e in self.dfk.config.executors:
            self.executors[e.label] = {'idle_since': None}

        self.strategies = {None: self._strategy_noop,
                           'simple': self._strategy_simple,
                           'htex_auto_scale': self._strategy_htex_auto_scale
                          }  # type: Dict[Optional[str], Callable]

        # mypy note: with mypy 0.761, the type of self.strategize is
        # correctly revealed inside this module, but isn't carried over
        #  when Strategy is used in other modules unless this specific
        # type annotation is used.

        self.strategize = self.strategies[self.config.strategy]   # type: Callable
        self.logger_flag = False
        self.prior_loghandlers = set(logging.getLogger().handlers)

        logger.debug("Scaling strategy: {0}".format(self.config.strategy))

    def add_executors(self, executors: Sequence[ParslExecutor]) -> None:
        for executor in executors:
            self.executors[executor.label] = {'idle_since': None}

    def _strategy_noop(self, status: List[ExecutorStatus], tasks: List[int]) -> None:
        """Do nothing.

        Args:
            - tasks (task_ids): Not used here.
        """

    def unset_logging(self) -> None:
        """ Mute newly added handlers to the root level, right after calling executor.status
        """
        if self.logger_flag is True:
            return

        root_logger = logging.getLogger()

        for handler in root_logger.handlers:
            if handler not in self.prior_loghandlers:
                handler.setLevel(logging.ERROR)

        self.logger_flag = True

    def _strategy_simple(self, status_list: "List[PollItem]", tasks: List[int]) -> None:
        self._general_strategy(status_list, tasks, strategy_type='simple')

    def _strategy_htex_auto_scale(self, status_list: "List[PollItem]", tasks: List[int]) -> None:
        """HTEX specific auto scaling strategy

        This strategy works only for HTEX. This strategy will scale up by
        requesting additional compute resources via the provider when the
        workload requirements exceed the provisioned capacity. The scale out
        behavior is exactly like the 'simple' strategy.

        If there are idle blocks during execution, this strategy will terminate
        those idle blocks specifically. When # of tasks >> # of blocks, HTEX places
        tasks evenly across blocks, which makes it rather difficult to ensure that
        some blocks will reach 0% utilization. Consequently, this strategy can be
        expected to scale down effectively only when # of workers, or tasks executing
        per block is close to 1.

        Args:
            - tasks (task_ids): Not used here.
        """
        self._general_strategy(status_list, tasks, strategy_type='htex')

    def _general_strategy(self, status_list: "List[PollItem]", tasks: List[int], strategy_type: str) -> None:
        for exec_status in status_list:
            executor = exec_status.executor
            label = executor.label
            if not executor.scaling_enabled:
                continue

            # Tasks that are either pending completion
            active_tasks = executor.outstanding

            status = exec_status.status
            self.unset_logging()

            # FIXME we need to handle case where provider does not define these
            # FIXME probably more of this logic should be moved to the provider
            min_blocks = executor.provider.min_blocks
            max_blocks = executor.provider.max_blocks

            # TODO: this should be related to the HasConnectedWorkers protocol
            # rather than a hard-coded whitelist of executors.
            if isinstance(executor, HighThroughputExecutor):
                tasks_per_node = executor.workers_per_node

            # this code will never fire... because ExtremeScaleExecutor is a subclass of the above matched HighThroughputExecutor
            # elif isinstance(executor, ExtremeScaleExecutor):
            #    tasks_per_node = executor.ranks_per_node

            else:
                assert(RuntimeError("BENC: missing else statement in executor case matching"))

            nodes_per_block = executor.provider.nodes_per_block
            parallelism = executor.provider.parallelism

            running = sum([1 for x in status.values() if x.state == JobState.RUNNING])
            pending = sum([1 for x in status.values() if x.state == JobState.PENDING])
            active_blocks = running + pending
            active_slots = active_blocks * tasks_per_node * nodes_per_block

            if isinstance(executor, HasConnectedWorkers):

                # mypy is not able to infer that executor has a
                # .connected_workers attribute from the above if statement,
                # so to make it happy, detyped_executor is turned into an
                # Any, which can have anything called on it. This makes this
                # code block less type safe.
                # A better approach would be for connected_workers to be
                # in a protocol, perhaps? or something else we can
                # meaningfully check in mypy. or have the executor able to
                # print its own statistics status rather than any ad-hoc
                # behaviour change here.
                # mypy issue https://github.com/python/mypy/issues/1424

                logger.debug('Executor {} has {} active tasks, {}/{} running/pending blocks, and {} connected workers'.format(
                    label, active_tasks, running, pending, executor.connected_workers))
            else:
                logger.debug('Executor {} has {} active tasks and {}/{} running/pending blocks'.format(
                    label, active_tasks, running, pending))

            # reset kill timer if executor has active tasks
            if active_tasks > 0 and self.executors[executor.label]['idle_since']:
                self.executors[executor.label]['idle_since'] = None

            # Case 1
            # No tasks.
            if active_tasks == 0:
                # Case 1a
                # Fewer blocks that min_blocks
                if active_blocks <= min_blocks:
                    # Ignore
                    # logger.debug("Strategy: Case.1a")
                    pass

                # Case 1b
                # More blocks than min_blocks. Scale down
                else:
                    # We want to make sure that max_idletime is reached
                    # before killing off resources
                    if not self.executors[executor.label]['idle_since']:
                        logger.debug("Executor {} has 0 active tasks; starting kill timer (if idle time exceeds {}s, resources will be removed)".format(
                            label, self.max_idletime)
                        )
                        self.executors[executor.label]['idle_since'] = time.time()

                    # ... this could be None, type-wise. So why aren't we seeing errors here?
                    # probably becaues usually if this is None, it will be because active_tasks>0,
                    # (although I can't see a clear proof that this will always be the case:
                    # could that setting to None have happened on a previous iteration?)

                    # if idle_since is None, then that means not idle, which means should not
                    # go down the scale_in path
                    idle_since = self.executors[executor.label]['idle_since']
                    if idle_since is not None and (time.time() - idle_since) > self.max_idletime:
                        # We have resources idle for the max duration,
                        # we have to scale_in now.
                        logger.debug("Idle time has reached {}s for executor {}; removing resources".format(
                            self.max_idletime, label)
                        )
                        exec_status.scale_in(active_blocks - min_blocks)

                    else:
                        pass
                        # logger.debug("Strategy: Case.1b. Waiting for timer : {0}".format(idle_since))

            # Case 2
            # More tasks than the available slots.
            elif (float(active_slots) / active_tasks) < parallelism:
                # Case 2a
                # We have the max blocks possible
                if active_blocks >= max_blocks:
                    # Ignore since we already have the max nodes
                    # logger.debug("Strategy: Case.2a")
                    pass

                # Case 2b
                else:
                    # logger.debug("Strategy: Case.2b")
                    excess = math.ceil((active_tasks * parallelism) - active_slots)
                    excess_blocks = math.ceil(float(excess) / (tasks_per_node * nodes_per_block))
                    excess_blocks = min(excess_blocks, max_blocks - active_blocks)
                    logger.debug("BENC: strategy: Requesting {} more blocks".format(excess_blocks))
                    exec_status.scale_out(excess_blocks)

            elif active_slots == 0 and active_tasks > 0:
                # Case 4
                logger.debug("BENC: strategy: Requesting single slot")
                if active_blocks < max_blocks:
                    exec_status.scale_out(1)

            # Case 4
            # More slots than tasks
            elif active_slots > 0 and active_slots > active_tasks:
                if strategy_type == 'htex':
                    # Scale down for htex
                    logger.debug("More slots than tasks")
                    if isinstance(executor, HighThroughputExecutor):
                        if active_blocks > min_blocks:
                            exec_status.scale_in(1, force=False, max_idletime=self.max_idletime)

                elif strategy_type == 'simple':
                    # skip for simple strategy
                    pass
                # TODO how does this elif^ differ from the default case of doing nothing in the implicit missing `else` ? I don't think it does...

            # Case 3
            # tasks ~ slots
            else:
                # logger.debug("Strategy: Case 3")
                pass
