"""申诉服务向 API 和 CLI 暴露的稳定错误。"""


class PenaltyAppealError(RuntimeError):
    code = "penalty_appeal_error"
    status = 400


class NotFound(PenaltyAppealError):
    code = "not_found"
    status = 404


class Conflict(PenaltyAppealError):
    code = "conflict"
    status = 409


class Forbidden(PenaltyAppealError):
    code = "forbidden"
    status = 403


class InvalidState(PenaltyAppealError):
    code = "invalid_state"
    status = 409


class ValidationFailed(PenaltyAppealError):
    code = "validation_failed"
    status = 422
