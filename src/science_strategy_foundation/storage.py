"""封装 SQLite 连接、建表和事务边界。"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS organizations (
    organization_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS actors (
    actor_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    active INTEGER NOT NULL CHECK(active IN (0, 1)),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sites (
    site_id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    name TEXT NOT NULL,
    timezone_name TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS domain_records (
    record_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    category TEXT NOT NULL,
    external_key TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL,
    UNIQUE(site_id, category, external_key)
);
CREATE TABLE IF NOT EXISTS request_receipts (
    request_id TEXT PRIMARY KEY,
    action TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    actor_id TEXT NOT NULL,
    action TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    previous_hash TEXT NOT NULL,
    event_hash TEXT NOT NULL UNIQUE,
    occurred_at TEXT NOT NULL
);

-- ===================== 里程碑与变更控制：版本化主体 =====================
CREATE TABLE IF NOT EXISTS programs (
    program_id TEXT PRIMARY KEY,
    current_version INTEGER NOT NULL CHECK(current_version >= 1),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS program_versions (
    program_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    name TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('effective','superseded')),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    request_id TEXT,
    PRIMARY KEY(program_id, version)
);
CREATE TABLE IF NOT EXISTS projects (
    project_id TEXT PRIMARY KEY,
    program_id TEXT NOT NULL,
    current_version INTEGER NOT NULL CHECK(current_version >= 1),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS project_versions (
    project_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    name TEXT NOT NULL,
    undertaking_org_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('effective','superseded','terminated')),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    request_id TEXT,
    PRIMARY KEY(project_id, version)
);
CREATE TABLE IF NOT EXISTS work_packages (
    wp_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    current_version INTEGER NOT NULL CHECK(current_version >= 1),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS wp_versions (
    wp_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    name TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('effective','superseded','terminated')),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    request_id TEXT,
    PRIMARY KEY(wp_id, version)
);
CREATE TABLE IF NOT EXISTS milestones (
    milestone_id TEXT PRIMARY KEY,
    wp_id TEXT NOT NULL,
    current_version INTEGER NOT NULL CHECK(current_version >= 1),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS milestone_versions (
    milestone_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    name TEXT NOT NULL,
    seq_no INTEGER NOT NULL,
    planned_days INTEGER NOT NULL CHECK(planned_days >= 0),
    planned_date TEXT,
    status TEXT NOT NULL CHECK(status IN ('effective','superseded','terminated')),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    request_id TEXT,
    PRIMARY KEY(milestone_id, version)
);
CREATE TABLE IF NOT EXISTS milestone_requirements (
    milestone_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    caliber_id TEXT NOT NULL,
    caliber_version INTEGER NOT NULL,
    required_verdicts INTEGER NOT NULL CHECK(required_verdicts >= 1),
    PRIMARY KEY(milestone_id, version, caliber_id)
);
CREATE TABLE IF NOT EXISTS dependencies (
    dependency_id TEXT PRIMARY KEY,
    current_version INTEGER NOT NULL CHECK(current_version >= 1),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS dependency_versions (
    dependency_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    upstream_milestone_id TEXT NOT NULL,
    downstream_milestone_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('effective','superseded')),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    request_id TEXT,
    PRIMARY KEY(dependency_id, version)
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_dependency_effective_pair
    ON dependency_versions(upstream_milestone_id, downstream_milestone_id)
    WHERE status='effective';

CREATE TABLE IF NOT EXISTS metric_calibers (
    caliber_id TEXT PRIMARY KEY,
    current_version INTEGER NOT NULL CHECK(current_version >= 1),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS caliber_versions (
    caliber_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    name TEXT NOT NULL,
    unit TEXT NOT NULL,
    domain_tag TEXT NOT NULL,
    rule_hash TEXT NOT NULL,
    compatible_previous INTEGER NOT NULL CHECK(compatible_previous IN (0,1)),
    compatibility_class TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('effective','superseded')),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    request_id TEXT,
    PRIMARY KEY(caliber_id, version)
);

-- ===================== 专家与证据 =====================
CREATE TABLE IF NOT EXISTS experts (
    expert_id TEXT PRIMARY KEY,
    actor_id TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    organization_id TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS expert_qualifications (
    qualification_id TEXT PRIMARY KEY,
    expert_id TEXT NOT NULL REFERENCES experts(expert_id),
    domain_tag TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    valid_until TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('active','revoked')),
    revoked_reason TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    revoked_at TEXT
);
CREATE TABLE IF NOT EXISTS evidence_packages (
    evidence_id TEXT PRIMARY KEY,
    caliber_id TEXT NOT NULL,
    caliber_version INTEGER NOT NULL,
    compatibility_class TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('available','void')),
    submitted_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    request_id TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS evidence_occupancy (
    evidence_id TEXT NOT NULL,
    compatibility_class TEXT NOT NULL,
    decision_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('active','freed')),
    created_at TEXT NOT NULL,
    freed_at TEXT,
    PRIMARY KEY(evidence_id, decision_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_evidence_active_occupancy
    ON evidence_occupancy(evidence_id) WHERE status='active';

-- ===================== 独立验收结论 =====================
CREATE TABLE IF NOT EXISTS acceptance_verdicts (
    verdict_id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL UNIQUE,
    milestone_id TEXT NOT NULL,
    milestone_version INTEGER NOT NULL,
    caliber_id TEXT NOT NULL,
    caliber_version INTEGER NOT NULL,
    expert_actor_id TEXT NOT NULL,
    qualification_id TEXT NOT NULL,
    conclusion TEXT NOT NULL CHECK(conclusion IN ('pass','fail')),
    evidence_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('effective','withdrawn')),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    withdrawn_at TEXT,
    withdraw_reason TEXT,
    UNIQUE(milestone_id, milestone_version, caliber_id, caliber_version, expert_actor_id)
);

-- ===================== 预算分期、释放与支付 =====================
CREATE TABLE IF NOT EXISTS budget_plans (
    plan_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    current_version INTEGER NOT NULL CHECK(current_version >= 1),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS budget_plan_versions (
    plan_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    status TEXT NOT NULL CHECK(status IN ('effective','superseded')),
    note TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    request_id TEXT,
    PRIMARY KEY(plan_id, version)
);
CREATE TABLE IF NOT EXISTS installments (
    installment_id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL,
    plan_version INTEGER NOT NULL,
    seq_no INTEGER NOT NULL,
    milestone_id TEXT NOT NULL,
    amount INTEGER NOT NULL CHECK(amount >= 0),
    released_amount INTEGER NOT NULL DEFAULT 0
        CHECK(released_amount >= 0 AND released_amount <= amount),
    status TEXT NOT NULL
        CHECK(status IN ('scheduled','partially_released','released','cancelled','superseded')),
    UNIQUE(plan_id, plan_version, milestone_id)
);
CREATE TABLE IF NOT EXISTS budget_releases (
    release_id TEXT PRIMARY KEY,
    installment_id TEXT NOT NULL,
    plan_version INTEGER NOT NULL,
    decision_id TEXT NOT NULL,
    amount INTEGER NOT NULL CHECK(amount > 0),
    status TEXT NOT NULL CHECK(status IN ('released','revoked')),
    created_at TEXT NOT NULL,
    revoked_at TEXT,
    revoked_by_decision_id TEXT
);
CREATE TABLE IF NOT EXISTS payments (
    payment_id TEXT PRIMARY KEY,
    release_id TEXT NOT NULL UNIQUE,
    amount INTEGER NOT NULL CHECK(amount > 0),
    status TEXT NOT NULL CHECK(status IN ('paid','closed','reversed')),
    request_id TEXT NOT NULL UNIQUE,
    created_by TEXT NOT NULL,
    paid_at TEXT NOT NULL,
    closed_at TEXT
);

-- ===================== 交付承诺 =====================
CREATE TABLE IF NOT EXISTS delivery_commitments (
    commitment_id TEXT PRIMARY KEY,
    subject_type TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    current_version INTEGER NOT NULL CHECK(current_version >= 1),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS commitment_versions (
    commitment_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    title TEXT NOT NULL,
    due_date TEXT,
    status TEXT NOT NULL
        CHECK(status IN ('effective','impacted','fulfilled','cancelled','superseded')),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    request_id TEXT,
    PRIMARY KEY(commitment_id, version)
);

-- ===================== 决定（阶段门结果与变更后继） =====================
CREATE TABLE IF NOT EXISTS decisions (
    decision_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK(kind IN (
        'gate_pass','gate_partial','gate_reject','rectification',
        'withdrawal','revocation','route_change','impact',
        'termination','cancellation')),
    subject_type TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    subject_version INTEGER,
    parent_decision_id TEXT,
    reason TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('effective','superseded','reversed')),
    basis_hash TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    request_id TEXT,
    created_by TEXT NOT NULL,
    occurred_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_decisions_subject
    ON decisions(subject_type, subject_id, status);
CREATE INDEX IF NOT EXISTS ix_decisions_parent ON decisions(parent_decision_id);
CREATE UNIQUE INDEX IF NOT EXISTS ux_decisions_request ON decisions(request_id) WHERE request_id IS NOT NULL;
CREATE TABLE IF NOT EXISTS decision_closure (
    ancestor_id TEXT NOT NULL,
    descendant_id TEXT NOT NULL,
    depth INTEGER NOT NULL CHECK(depth >= 1),
    PRIMARY KEY(ancestor_id, descendant_id)
);
"""


class Database:
    """管理 SQLite 数据库并为服务提供短事务。"""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        self.connection = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self.row_lock = threading.RLock()
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA busy_timeout = 5000")
        self.connection.executescript(SCHEMA)

    @contextmanager
    def transaction(self, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        """在异常时回滚，在成功时提交。

        SQLite 连接本身在进程内共享，因此这里用可重入锁把同一事务的
        BEGIN/COMMIT 串行化，避免并发请求交叉发送事务控制语句。
        """

        with self.row_lock:
            self.connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            try:
                yield self.connection
            except Exception:
                self.connection.rollback()
                raise
            else:
                self.connection.commit()

    def close(self) -> None:
        """关闭底层连接。"""

        self.connection.close()
