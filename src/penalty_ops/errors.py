"""申诉复核服务向 API 与调用方暴露的稳定错误。"""


class AppealError(RuntimeError):
    code = "appeal_error"
    status = 400


class NotFound(AppealError):
    code = "not_found"
    status = 404


class Conflict(AppealError):
    code = "conflict"
    status = 409


class Forbidden(AppealError):
    code = "forbidden"
    status = 403


class InvalidState(AppealError):
    code = "invalid_state"
    status = 409


class ValidationFailed(AppealError):
    code = "validation_failed"
    status = 422
