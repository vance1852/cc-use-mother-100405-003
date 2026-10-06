"""定义里程碑与变更控制子域使用的数据对象。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class VersionedRef:
    """精确指向某一版本的不可变引用。"""

    kind: str
    object_id: str
    version: int


@dataclass(frozen=True)
class CaliberView:
    """指标口径版本视图。"""

    caliber_id: str
    version: int
    name: str
    unit: str
    domain_tag: str
    rule_hash: str
    compatible_previous: bool
    compatibility_class: str
    status: str


@dataclass(frozen=True)
class MilestoneView:
    """里程碑版本视图。"""

    milestone_id: str
    version: int
    wp_id: str
    name: str
    seq_no: int
    planned_days: int
    planned_date: str | None
    status: str
    requirements: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class DependencyView:
    """依赖关系版本视图。"""

    dependency_id: str
    version: int
    upstream_milestone_id: str
    downstream_milestone_id: str
    status: str


@dataclass(frozen=True)
class VerdictView:
    """独立验收结论视图。"""

    verdict_id: str
    milestone_id: str
    milestone_version: int
    caliber_id: str
    caliber_version: int
    expert_actor_id: str
    qualification_id: str
    conclusion: str
    evidence: tuple[str, ...]
    status: str
    created_at: str
    withdrawn_at: str | None = None
    withdraw_reason: str | None = None


@dataclass(frozen=True)
class InstallmentView:
    """预算分期视图。"""

    installment_id: str
    plan_id: str
    plan_version: int
    seq_no: int
    milestone_id: str
    amount: int
    status: str
    released_amount: int = 0

    @property
    def blocked_amount(self) -> int:
        return self.amount - self.released_amount


@dataclass(frozen=True)
class DecisionView:
    """决定视图（阶段门结果或后继决定）。"""

    decision_id: str
    kind: str
    subject_type: str
    subject_id: str
    subject_version: int | None
    parent_decision_id: str | None
    reason: str
    status: str
    detail: dict[str, Any]
    created_by: str
    occurred_at: str


@dataclass(frozen=True)
class GateBlock:
    """阶段门不能放行的确切阻塞原因。"""

    code: str
    message: str
    subject: str = ""


@dataclass(frozen=True)
class GateEvaluation:
    """阶段门评估结果。"""

    milestone_id: str
    milestone_version: int
    passed: bool
    partial: bool
    blocks: tuple[GateBlock, ...] = ()
    satisfied_prerequisites: tuple[str, ...] = ()
    accepted_verdicts: tuple[VerdictView, ...] = ()
    releaseable_amount: int = 0
    installment_id: str | None = None


@dataclass(frozen=True)
class CriticalPathNode:
    """关键路径上的一个节点。"""

    milestone_id: str
    version: int
    name: str
    wp_id: str
    seq_no: int
    planned_days: int
    state: str
    earliest_finish_days: int
    blockers: tuple[str, ...] = ()


@dataclass(frozen=True)
class ImpactItem:
    """一次变化对单个承诺/里程碑的影响记录。"""

    subject_type: str
    subject_id: str
    commitment_id: str | None
    change: str
    decision_id: str


@dataclass(frozen=True)
class CommitmentView:
    """交付承诺版本视图。"""

    commitment_id: str
    version: int
    subject_type: str
    subject_id: str
    title: str
    due_date: str | None
    status: str
