"""里程碑与变更控制服务使用的业务异常。"""

from __future__ import annotations

from typing import Any

from science_strategy_foundation.errors import (
    ConflictError,
    DomainError,
    NotFoundError,
    PermissionDenied,
    ValidationError,
)

__all__ = [
    "DomainError",
    "ValidationError",
    "NotFoundError",
    "PermissionDenied",
    "ConflictError",
    "GateBlocked",
    "ImmutableError",
]


class GateBlocked(ConflictError):
    """阶段门前置条件未满足，不能通过并释放资金。"""

    code = "gate_blocked"

    def __init__(self, reasons: list[dict[str, Any]]) -> None:
        super().__init__("阶段门前置条件尚未全部满足")
        self.reasons = reasons


class ImmutableError(ConflictError):
    """试图修改已经固化或关账的事实。"""

    code = "immutable"
