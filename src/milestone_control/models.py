"""里程碑控制服务的只读数据对象。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Versioned:
    """所有版本化定义的公共形状。"""

    id: str
    version: int
    status: str = "active"


@dataclass(frozen=True)
class Project(Versioned):
    name: str = ""
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Topic(Versioned):
    project_id: str = ""
    name: str = ""
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class WorkPackage(Versioned):
    topic_id: str = ""
    name: str = ""
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Metric(Versioned):
    name: str = ""
    unit: str = ""
    spec: dict[str, Any] = field(default_factory=dict)
    compatible: bool = True


@dataclass(frozen=True)
class Evidence(Versioned):
    title: str = ""
    content_hash: str = ""
    calibers: tuple[tuple[str, int], ...] = ()


@dataclass(frozen=True)
class Expert(Versioned):
    actor_id: str = ""
    display_name: str = ""
    qualifications: tuple[str, ...] = ()
    active: bool = True


@dataclass(frozen=True)
class Milestone(Versioned):
    project_id: str = ""
    topic_id: str | None = None
    name: str = ""
    sequence_no: int = 0
    kind: str = "milestone"
    planned_date: str = ""
    duration_days: int = 1
    required_approvers: int = 1
    metric_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class Commitment(Versioned):
    wp_id: str = ""
    milestone_id: str = ""
    title: str = ""
    due_date: str = ""
    metric_id: str | None = None
    metric_version: int | None = None
    target_value: Any = None


@dataclass(frozen=True)
class BudgetInstallment:
    installment_id: str
    project_id: str
    topic_id: str
    gate_milestone_id: str
    sequence_no: int
    amount_cents: int
    currency: str
    status: str


@dataclass(frozen=True)
class Acceptance:
    acceptance_id: str
    milestone_id: str
    milestone_version: int
    round: int
    result: str
    status: str
    readings: tuple[dict[str, Any], ...]
    lead_expert_id: str
    lead_expert_version: int
    basis_hash: str
    created_by: str
    created_at: str


@dataclass(frozen=True)
class GateDecision:
    decision_id: str
    gate_milestone_id: str
    gate_milestone_version: int
    round: int
    state: str
    result: str | None
    status: str
    acceptance_id: str | None
    basis_hash: str | None
    created_by: str
    created_at: str
    decided_at: str | None
    signoffs: tuple[dict[str, Any], ...] = ()
