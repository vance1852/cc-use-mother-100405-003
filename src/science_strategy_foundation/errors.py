"""领域服务使用的业务异常。"""


class DomainError(Exception):
    """所有可预期业务异常的基类。"""

    code = "domain_error"
    status = 400


class ValidationError(DomainError):
    """输入字段不符合业务约束。"""

    code = "validation_error"


class NotFoundError(DomainError):
    """请求引用的业务对象不存在。"""

    code = "not_found"
    status = 404


class PermissionDenied(DomainError):
    """操作者没有执行当前动作的权限。"""

    code = "permission_denied"
    status = 403


class ConflictError(DomainError):
    """请求编号或业务唯一键与既有内容冲突。"""

    code = "conflict"
    status = 409


class StateError(DomainError):
    """业务对象当前生命周期状态不允许该动作。"""

    code = "state_error"
    status = 409


class PreconditionError(DomainError):
    """阶段门前置条件（前置里程碑或独立验收）未满足。"""

    code = "precondition_failed"
    status = 422

    def __init__(self, message: str, blocks: list[dict[str, str]] | None = None) -> None:
        super().__init__(message)
        self.blocks = blocks or []
