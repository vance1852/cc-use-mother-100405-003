"""里程碑与变更控制服务的离线端到端验收。

在临时 SQLite 库中复刻“面向 2035 的重大科技项目”场景：
证据跨口径重复占用被拒绝、阶段门原子通过释放额度、路线调整与结论撤回
沿依赖生成后继决定、已关账支付与旧版结论保持可审计、管理查询能看到
关键路径与资金未释放的确切原因，请求重放得到唯一稳定结果。
"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from science_strategy_foundation.clock import FixedClock
from science_strategy_foundation.service import DomainService

from .errors import GateBlocked
from .service import MilestoneControlService
from .storage import MilestoneDatabase


def _bootstrap(database, clock):
    base = DomainService(database, clock)
    mc = MilestoneControlService(database, clock)
    base.register_organization(request_id="org", actor_id="bootstrap",
                               organization_id="o1", name="国家专项管理机构")
    base.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="a1",
                        display_name="管理员", role="admin", organization_id="o1")
    base.register_actor(request_id="op", actor_id="a1", new_actor_id="op1",
                        display_name="专项办操作员", role="operator", organization_id="o1")
    for key, actor_id, name in [("r1", "rv1", "验收专家甲"), ("r2", "rv2", "会签专家乙"),
                                ("r3", "rv3", "会签专家丙")]:
        base.register_actor(request_id=f"actor-{key}", actor_id="a1", new_actor_id=actor_id,
                            display_name=name, role="reviewer", organization_id="o1")
    return base, mc


def run() -> dict[str, object]:
    checks: dict[str, object] = {}
    with tempfile.TemporaryDirectory() as directory:
        database = MilestoneDatabase(Path(directory) / "milestone_acceptance.sqlite3")
        clock = FixedClock(datetime(2026, 10, 6, 8, 0, tzinfo=timezone.utc))
        _, mc = _bootstrap(database, clock)

        # —— 定义固化 ——
        mc.register_project(request_id="proj", actor_id="op1", project_id="proj2035",
                            name="面向2035重大科技专项")
        mc.register_topic(request_id="t1", actor_id="op1", topic_id="topic-a",
                          project_id="proj2035", name="上游技术路线课题")
        mc.register_topic(request_id="t2", actor_id="op1", topic_id="topic-b",
                          project_id="proj2035", name="下游集成交付课题")
        mc.register_work_package(request_id="wp1", actor_id="op1", wp_id="wp-a",
                                 topic_id="topic-a", name="路线验证工作包")
        mc.register_work_package(request_id="wp2", actor_id="op1", wp_id="wp-b",
                                 topic_id="topic-b", name="集成交付工作包")
        mc.register_metric(request_id="m1", actor_id="op1", metric_id="metric-eff",
                           name="能效指标", unit="%", spec={"method": "v1"})
        mc.register_metric(request_id="m2", actor_id="op1", metric_id="metric-yield",
                           name="良率指标", unit="%", spec={"method": "v1"})
        mc.register_milestone(request_id="gA", actor_id="op1", milestone_id="gate-a",
                              project_id="proj2035", topic_id="topic-a", name="路线阶段门",
                              sequence_no=1, kind="gate", planned_date="2028-06-30",
                              duration_days=10, required_approvers=1, metric_ids=["metric-eff"])
        mc.register_milestone(request_id="gB", actor_id="op1", milestone_id="gate-b",
                              project_id="proj2035", topic_id="topic-b", name="交付阶段门",
                              sequence_no=2, kind="gate", planned_date="2030-06-30",
                              duration_days=20, required_approvers=1,
                              metric_ids=["metric-eff", "metric-yield"])
        mc.register_dependency(request_id="dep-ab", actor_id="op1", dependency_id="d-ab",
                               upstream_milestone_id="gate-a",
                               downstream_milestone_id="gate-b")
        mc.register_budget_installment(request_id="b1", actor_id="op1", installment_id="inst-a",
                                       project_id="proj2035", topic_id="topic-a",
                                       gate_milestone_id="gate-a", sequence_no=1,
                                       amount_cents=500_000_00)
        mc.register_budget_installment(request_id="b2", actor_id="op1", installment_id="inst-b",
                                       project_id="proj2035", topic_id="topic-b",
                                       gate_milestone_id="gate-b", sequence_no=1,
                                       amount_cents=800_000_00)
        mc.register_commitment(request_id="c1", actor_id="op1", commitment_id="com-a",
                               wp_id="wp-a", milestone_id="gate-a", title="能效达标承诺",
                               due_date="2028-03-31", metric_id="metric-eff",
                               target_value={"min": 90})
        for key, expert_id, actor_id in [("e1", "expert-1", "rv1"), ("e2", "expert-2", "rv2"),
                                         ("e3", "expert-3", "rv3")]:
            mc.register_expert(request_id=f"exp-{key}", actor_id="a1", expert_id=expert_id,
                               expert_actor_id=actor_id, display_name=actor_id,
                               qualifications=["阶段验收专家"])
        mc.register_evidence(request_id="ev1", actor_id="rv1", evidence_id="evidence-eff",
                             title="能效试验报告", content_hash="hash-eff-1",
                             calibers=[{"metric_id": "metric-eff"}])
        mc.register_evidence(request_id="ev2", actor_id="rv1", evidence_id="evidence-yield",
                             title="良率试验报告", content_hash="hash-yield-1",
                             calibers=[{"metric_id": "metric-yield"}, {"metric_id": "metric-eff"}])

        # —— gate-a：开门、独立验收、独立专家会签、原子通过 ——
        mc.open_gate_round(request_id="oA", actor_id="op1", gate_milestone_id="gate-a")
        mc.submit_acceptance(request_id="aA", actor_id="rv1", gate_milestone_id="gate-a",
                             result="passed",
                             readings=[{"metric_id": "metric-eff", "evidence_id": "evidence-eff"}])
        # 责任专家本人会签不能计入法定人数，需要另一位独立专家赞成
        mc.sign_gate(request_id="sA2", actor_id="rv2", gate_milestone_id="gate-a",
                     opinion="approved")
        decided_a = mc.decide_gate(request_id="dA", actor_id="op1", gate_milestone_id="gate-a")
        checks["gate_a_released"] = decided_a["released_cents"] == 500_000_00
        # 决定请求重放：进程恢复/并发重试得到同一条决定，不重复释放
        replay = mc.decide_gate(request_id="dA", actor_id="op1", gate_milestone_id="gate-a")
        checks["decide_replay_stable"] = replay["replayed"] and replay["resource_id"] == decided_a["resource_id"]

        # 一部分资金支付并关账：撤回后仍须保留
        mc.mark_installment_paid(request_id="pay-a", actor_id="op1", installment_id="inst-a")
        mc.close_installment(request_id="close-a", actor_id="op1", installment_id="inst-a")

        # —— 同一份证据不能跨验收重复占用 ——
        mc.open_gate_round(request_id="oB1", actor_id="op1", gate_milestone_id="gate-b")
        reuse_blocked = False
        try:
            mc.submit_acceptance(request_id="aB-bad", actor_id="rv1", gate_milestone_id="gate-b",
                                 result="passed",
                                 readings=[{"metric_id": "metric-eff", "evidence_id": "evidence-eff"},
                                           {"metric_id": "metric-yield", "evidence_id": "evidence-yield"}])
        except Exception as exc:  # noqa: BLE001 - 验收脚本断言错误码
            reuse_blocked = getattr(exc, "code", "") == "conflict"
        checks["evidence_reuse_blocked"] = reuse_blocked

        # gate-b 还缺独立验收时强行决定：返回确切阻断原因
        blocked_reasons: list[str] = []
        try:
            mc.decide_gate(request_id="dB-bad", actor_id="op1", gate_milestone_id="gate-b")
        except GateBlocked as exc:
            blocked_reasons = [r["code"] for r in exc.reasons]
        checks["block_reasons_exact"] = "acceptance_missing" in blocked_reasons

        # 用另一份证据完成 gate-b 验收与会签
        mc.submit_acceptance(request_id="aB", actor_id="rv1", gate_milestone_id="gate-b",
                             result="passed",
                             readings=[{"metric_id": "metric-eff", "evidence_id": "evidence-yield"},
                                       {"metric_id": "metric-yield", "evidence_id": "evidence-yield"}])
        mc.sign_gate(request_id="sB3", actor_id="rv3", gate_milestone_id="gate-b",
                     opinion="approved")
        decided_b = mc.decide_gate(request_id="dB", actor_id="op1", gate_milestone_id="gate-b")
        checks["gate_b_released"] = decided_b["released_cents"] == 800_000_00

        # —— 技术路线口径不兼容变更：证据不能再占用，影响沿依赖展开 ——
        mc.revise_metric(request_id="m1v2", actor_id="op1", metric_id="metric-eff",
                         name="能效指标", unit="%", spec={"method": "v2"}, compatible=False,
                         reason="上游技术路线变更，能效测量口径不兼容")
        impact = None
        changes = mc.list_changes()
        for item in changes["items"]:
            impact = mc.change_impact(item["change_id"])
            if item["change_type"] == "route_adjustment" and "metric-eff" in item["origin_id"]:
                break
        affected = set(impact["affected_commitments"])
        checks["metric_break_commitments"] = "com-a" in affected
        effect_types = {e["effect_type"] for e in impact["effects"]}
        checks["metric_break_propagated"] = "require_rebaseline" in effect_types

        # 旧口径证据新版本验收被拒绝
        mc.revise_evidence(request_id="ev1v2", actor_id="rv1", evidence_id="evidence-eff",
                           title="能效试验报告", content_hash="hash-eff-1",
                           calibers=[{"metric_id": "metric-eff", "metric_version": 1}])
        caliber_blocked = False
        mc.open_gate_round(request_id="oA2", actor_id="op1", gate_milestone_id="gate-a")
        try:
            mc.submit_acceptance(request_id="aA-bad", actor_id="rv1", gate_milestone_id="gate-a",
                                 result="passed",
                                 readings=[{"metric_id": "metric-eff",
                                            "evidence_id": "evidence-eff",
                                            "evidence_version": 1}])
        except Exception as exc:  # noqa: BLE001
            caliber_blocked = getattr(exc, "code", "") == "validation_error"
        checks["incompatible_caliber_blocked"] = caliber_blocked

        # —— 专家结论撤回：未支付额度撤销，已关账保留，后继轮次生成 ——
        status_b = mc.gate_status("gate-b")
        acceptance_b_id = status_b["acceptance"]["acceptance_id"]
        withdraw_result = mc.withdraw_acceptance(request_id="wB2", actor_id="a1",
                                                 acceptance_id=acceptance_b_id,
                                                 reason="复核发现试验样本异常")
        checks["successor_round_created"] = withdraw_result["successor_round"] >= 2
        checks["unpaid_revoked"] = withdraw_result["revoked_cents"] == 800_000_00
        # gate-a 的分期已关账，撤回 gate-b 不影响它；台账保留为 closed
        status_a = mc.gate_status("gate-a")
        closed_kept = any(i.get("ledger_status") == "closed" for i in status_a["installments"])
        checks["closed_payment_retained"] = closed_kept

        # 撤回后 gate-b 资金回到待释放且阻断原因明确（需新验收）
        pending = mc.pending_funds("proj2035")
        pending_ids = {i["installment_id"] for i in pending["items"]}
        checks["funds_pending_again"] = "inst-b" in pending_ids
        gate_b_after = mc.gate_status("gate-b")
        reason_codes = {r["code"] for r in gate_b_after["block_reasons"]}
        checks["withdraw_shows_reasons"] = "acceptance_missing" in reason_codes

        # —— 课题终止：未释放额度冻结、下游挂起、旧结论留痕 ——
        mc.register_milestone(request_id="gC", actor_id="op1", milestone_id="gate-c",
                              project_id="proj2035", topic_id="topic-b", name="终验门",
                              sequence_no=3, kind="gate", planned_date="2033-06-30",
                              duration_days=5, required_approvers=1)
        mc.register_dependency(request_id="dep-bc", actor_id="op1", dependency_id="d-bc",
                               upstream_milestone_id="gate-b",
                               downstream_milestone_id="gate-c")
        term = mc.terminate_topic(request_id="term-b", actor_id="a1", topic_id="topic-b",
                                  reason="课题任务调整终止")
        term_impact = mc.change_impact(term["change_id"])
        term_effects = {(e["target_type"], e["effect_type"]) for e in term_impact["effects"]}
        checks["topic_termination_effects"] = ("budget", "void_installment") in term_effects
        checks["termination_holds_c"] = ("gate", "hold_downstream") in term_effects

        # —— 关键路径与版本留痕 ——
        cp = mc.critical_path("proj2035")
        checks["critical_path_starts_a"] = cp["critical_path"][0] == "gate-a"
        versions = mc.list_versions("milestone", "gate-a")
        checks["versions_immutable"] = len(versions["versions"]) == 1
        metric_versions = mc.list_versions("metric", "metric-eff")
        checks["metric_versions_kept"] = len(metric_versions["versions"]) == 2

        valid, event_count = mc.verify_audit()
        database.close()
        result = {"status": "ok", "audit_valid": valid, "audit_events": event_count, **checks}
        return result


def main() -> int:
    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    ok = result.pop("status") == "ok" and result.pop("audit_valid") and all(
        value is True for key, value in result.items() if key != "audit_events")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
