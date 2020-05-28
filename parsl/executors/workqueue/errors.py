import parsl.app.errors as perror


class WorkQueueTaskFailure(perror.AppException):
    """A failure executing a task in workqueue

    Contains:
    reason(string)
    status(int)
    """

    def __init__(self, reason, status):
        self.reason = reason
        self.status = status


class WorkQueueFailure(perror.ParslError):
    """A failure in the work queue executor that prevented the task to be
    executed.""
    """
    pass
