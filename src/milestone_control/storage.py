"""里程碑与变更控制服务的 SQLite 结构与连接管理。

所有业务定义采用“版本表 + 当前版本指针”：版本行只追加、不更新，
旧版本与旧版结论永久保留，供审计核对。库中同时存在基础服务的表，
本模块通过 ``mc_`` 前缀避免冲突，并复用基础服务的审计链与幂等回执表。
"""

from __future__ import annotations

import threading
from contextlib import contextmanager

from science_strategy_foundation.storage import Database


MC_SCHEMA = """
PRAGMA foreign_keys = ON;

-- 专项（项目）版本与当前指针
CREATE TABLE IF NOT EXISTS mc_project_versions (
    project_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    name TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(project_id, version)
);
CREATE TABLE IF NOT EXISTS mc_projects (
    project_id TEXT PRIMARY KEY,
    current_version INTEGER NOT NULL CHECK(current_version >= 1),
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','terminated')),
    terminated_at TEXT
);

-- 课题版本
CREATE TABLE IF NOT EXISTS mc_topic_versions (
    topic_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    project_id TEXT NOT NULL,
    name TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(topic_id, version)
);
CREATE TABLE IF NOT EXISTS mc_topics (
    topic_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    current_version INTEGER NOT NULL CHECK(current_version >= 1),
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','terminated')),
    terminated_at TEXT
);

-- 工作包版本
CREATE TABLE IF NOT EXISTS mc_workpackage_versions (
    wp_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    topic_id TEXT NOT NULL,
    name TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(wp_id, version)
);
CREATE TABLE IF NOT EXISTS mc_workpackages (
    wp_id TEXT PRIMARY KEY,
    topic_id TEXT NOT NULL,
    current_version INTEGER NOT NULL CHECK(current_version >= 1),
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','terminated'))
);

-- 指标口径版本：compatible=0 表示该版本相对上一版不兼容，旧口径证据不可跨越占用
CREATE TABLE IF NOT EXISTS mc_metric_versions (
    metric_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    name TEXT NOT NULL,
    unit TEXT NOT NULL DEFAULT '',
    spec_json TEXT NOT NULL,
    compatible INTEGER NOT NULL DEFAULT 1 CHECK(compatible IN (0,1)),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(metric_id, version)
);
CREATE TABLE IF NOT EXISTS mc_metrics (
    metric_id TEXT PRIMARY KEY,
    current_version INTEGER NOT NULL CHECK(current_version >= 1)
);

-- 证据包版本：固化内容摘要与其生产时采用的指标口径
CREATE TABLE IF NOT EXISTS mc_evidence_versions (
    evidence_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    title TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    calibers_json TEXT NOT NULL,
    producer_actor_id TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(evidence_id, version)
);
CREATE TABLE IF NOT EXISTS mc_evidence (
    evidence_id TEXT PRIMARY KEY,
    current_version INTEGER NOT NULL CHECK(current_version >= 1)
);
-- 证据占用：同一证据版本在一次“用于阶段门释放资金的独立验收”中只能被占用一次；
-- 一份声明多口径的证据可在同一次验收中支撑多个指标（只产生一条占用）。
CREATE TABLE IF NOT EXISTS mc_evidence_usages (
    usage_id TEXT PRIMARY KEY,
    evidence_id TEXT NOT NULL,
    evidence_version INTEGER NOT NULL,
    scope_type TEXT NOT NULL CHECK(scope_type IN ('gate_acceptance')),
    scope_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'consumed' CHECK(status IN ('consumed','released')),
    created_at TEXT NOT NULL,
    UNIQUE(evidence_id, evidence_version, scope_type, scope_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS mc_evidence_single_occupant
    ON mc_evidence_usages(evidence_id, evidence_version)
    WHERE status = 'consumed' AND scope_type = 'gate_acceptance';

-- 专家资格版本：会签与验收结论快照当时的资格版本
CREATE TABLE IF NOT EXISTS mc_expert_versions (
    expert_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    actor_id TEXT NOT NULL,
    display_name TEXT NOT NULL,
    qualifications_json TEXT NOT NULL,
    active INTEGER NOT NULL CHECK(active IN (0,1)),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(expert_id, version)
);
CREATE TABLE IF NOT EXISTS mc_experts (
    expert_id TEXT PRIMARY KEY,
    actor_id TEXT NOT NULL UNIQUE,
    current_version INTEGER NOT NULL CHECK(current_version >= 1)
);

-- 里程碑（含阶段门）版本
CREATE TABLE IF NOT EXISTS mc_milestone_versions (
    milestone_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    project_id TEXT NOT NULL,
    topic_id TEXT,
    name TEXT NOT NULL,
    sequence_no INTEGER NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('gate','milestone')),
    planned_date TEXT NOT NULL,
    duration_days INTEGER NOT NULL DEFAULT 1 CHECK(duration_days >= 0),
    required_approvers INTEGER NOT NULL DEFAULT 1 CHECK(required_approvers >= 1),
    metric_ids_json TEXT NOT NULL DEFAULT '[]',
    payload_json TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(milestone_id, version)
);
CREATE TABLE IF NOT EXISTS mc_milestones (
    milestone_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    topic_id TEXT,
    current_version INTEGER NOT NULL CHECK(current_version >= 1),
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','terminated'))
);

-- 依赖关系版本（当前图取 active=1 的最新版本）
CREATE TABLE IF NOT EXISTS mc_dependency_versions (
    dependency_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    upstream_milestone_id TEXT NOT NULL,
    downstream_milestone_id TEXT NOT NULL,
    active INTEGER NOT NULL CHECK(active IN (0,1)),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(dependency_id, version)
);
CREATE TABLE IF NOT EXISTS mc_dependencies (
    dependency_id TEXT PRIMARY KEY,
    current_version INTEGER NOT NULL CHECK(current_version >= 1)
);

-- 预算分期与资金台账
CREATE TABLE IF NOT EXISTS mc_budget_installments (
    installment_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    topic_id TEXT NOT NULL,
    gate_milestone_id TEXT NOT NULL,
    sequence_no INTEGER NOT NULL,
    amount_cents INTEGER NOT NULL CHECK(amount_cents >= 0),
    currency TEXT NOT NULL DEFAULT 'CNY',
    status TEXT NOT NULL DEFAULT 'planned' CHECK(status IN ('planned','released','void')),
    created_at TEXT NOT NULL,
    UNIQUE(gate_milestone_id, sequence_no)
);
CREATE TABLE IF NOT EXISTS mc_budget_ledger (
    ledger_id TEXT PRIMARY KEY,
    installment_id TEXT NOT NULL,
    amount_cents INTEGER NOT NULL CHECK(amount_cents >= 0),
    gate_milestone_id TEXT NOT NULL,
    gate_milestone_version INTEGER NOT NULL,
    decision_id TEXT NOT NULL,
    acceptance_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('released','paid','closed','revoked')),
    released_at TEXT NOT NULL,
    paid_at TEXT,
    closed_at TEXT
);
-- 关账支付不可变：同一分期同一轮至多一条有效释放台账
CREATE UNIQUE INDEX IF NOT EXISTS mc_ledger_active_once
    ON mc_budget_ledger(installment_id)
    WHERE status IN ('released','paid','closed');

-- 交付承诺版本
CREATE TABLE IF NOT EXISTS mc_commitment_versions (
    commitment_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    wp_id TEXT NOT NULL,
    milestone_id TEXT NOT NULL,
    title TEXT NOT NULL,
    due_date TEXT NOT NULL,
    metric_id TEXT,
    metric_version INTEGER,
    target_value_json TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(commitment_id, version)
);
CREATE TABLE IF NOT EXISTS mc_commitments (
    commitment_id TEXT PRIMARY KEY,
    wp_id TEXT NOT NULL,
    current_version INTEGER NOT NULL CHECK(current_version >= 1),
    status TEXT NOT NULL DEFAULT 'active'
        CHECK(status IN ('active','superseded','terminated'))
);

-- 独立验收
CREATE TABLE IF NOT EXISTS mc_acceptances (
    acceptance_id TEXT PRIMARY KEY,
    milestone_id TEXT NOT NULL,
    milestone_version INTEGER NOT NULL,
    round INTEGER NOT NULL CHECK(round >= 1),
    result TEXT NOT NULL CHECK(result IN ('passed','partial','failed')),
    status TEXT NOT NULL CHECK(status IN ('effective','withdrawn','superseded')),
    readings_json TEXT NOT NULL,
    lead_expert_id TEXT NOT NULL,
    lead_expert_version INTEGER NOT NULL,
    basis_hash TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    withdrawn_at TEXT,
    UNIQUE(milestone_id, round)
);

-- 阶段门会签轮次与决定
CREATE TABLE IF NOT EXISTS mc_gate_decisions (
    decision_id TEXT PRIMARY KEY,
    gate_milestone_id TEXT NOT NULL,
    gate_milestone_version INTEGER NOT NULL,
    round INTEGER NOT NULL CHECK(round >= 1),
    state TEXT NOT NULL CHECK(state IN ('open','decided')),
    result TEXT CHECK(result IS NULL OR result IN ('passed','partial','rejected','terminated')),
    status TEXT NOT NULL DEFAULT 'effective' CHECK(status IN ('effective','withdrawn_basis','superseded')),
    acceptance_id TEXT,
    basis_hash TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    decided_at TEXT,
    UNIQUE(gate_milestone_id, round)
);
-- 同一里程碑同一版本至多一个生效中的通过决定（资金释放唯一）
CREATE UNIQUE INDEX IF NOT EXISTS mc_gate_effective_pass
    ON mc_gate_decisions(gate_milestone_id)
    WHERE state = 'decided' AND result = 'passed' AND status = 'effective';
-- 同一里程碑至多一个未关闭的会签轮次
CREATE UNIQUE INDEX IF NOT EXISTS mc_gate_single_open
    ON mc_gate_decisions(gate_milestone_id)
    WHERE state = 'open';

CREATE TABLE IF NOT EXISTS mc_gate_signoffs (
    decision_id TEXT NOT NULL,
    expert_id TEXT NOT NULL,
    expert_version INTEGER NOT NULL,
    opinion TEXT NOT NULL CHECK(opinion IN ('approved','rejected','abstain')),
    note TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','withdrawn')),
    signed_by TEXT NOT NULL,
    signed_at TEXT NOT NULL,
    withdrawn_at TEXT,
    PRIMARY KEY(decision_id, expert_id)
);

-- 变更单：路线调整/部分通过/限期整改/结论撤回/课题终止
CREATE TABLE IF NOT EXISTS mc_change_orders (
    change_id TEXT PRIMARY KEY,
    change_type TEXT NOT NULL CHECK(change_type IN (
        'route_adjustment','partial_pass','rectification',
        'conclusion_withdrawn','topic_termination')),
    origin_type TEXT NOT NULL,
    origin_id TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL CHECK(status IN ('effective','closed')),
    payload_json TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    effective_at TEXT NOT NULL,
    UNIQUE(origin_type, origin_id)
);
CREATE TABLE IF NOT EXISTS mc_change_effects (
    effect_id TEXT PRIMARY KEY,
    change_id TEXT NOT NULL,
    sequence_no INTEGER NOT NULL,
    target_type TEXT NOT NULL CHECK(target_type IN (
        'milestone','commitment','gate','budget','topic')),
    target_id TEXT NOT NULL,
    effect_type TEXT NOT NULL CHECK(effect_type IN (
        'require_rebaseline','block_funds','successor_round',
        'void_installment','hold_downstream','terminate')),
    status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','resolved')),
    detail_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(change_id, target_type, target_id, effect_type)
);
"""


class MilestoneDatabase(Database):
    """在基础服务库结构之上追加里程碑控制表。"""

    def __init__(self, path: str = ":memory:") -> None:
        super().__init__(path)
        # 单连接被 ThreadingHTTPServer 的多线程共享，进程内串行化写事务，
        # 配合 BEGIN IMMEDIATE 保证并发会签/恢复重放的唯一稳定结果
        self._tx_lock = threading.RLock()
        self.connection.executescript(MC_SCHEMA)

    @contextmanager
    def transaction(self, immediate: bool = False):
        with self._tx_lock:
            with super().transaction(immediate=immediate) as connection:
                yield connection
