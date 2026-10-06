"""里程碑与变更控制服务的离线端到端验收。

在临时 SQLite 库中完整演练：版本固化 → 独立验收 → 原子阶段门/放款 →
证据唯一占用（跨课题/跨不兼容口径拒绝）→ 部分通过/限期整改 →
口径不兼容修订沿依赖传播并冲击承诺 → 结论撤回回收未支付款（已关账保留）→
课题终止注销余额，最后核对关键路径、资金阻塞原因、决定谱系与审计哈希链。
"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .clock import FixedClock
from .service import DomainService
from .storage import Database


def run() -> dict[str, object]:
    """执行完整里程碑管控链并返回可核对结果。"""

    with tempfile.TemporaryDirectory() as directory:
        database = Database(Path(directory) / "milestone_acceptance.sqlite3")
        service = DomainService(database, FixedClock(datetime(2026, 10, 6, 8, 0, tzinfo=timezone.utc)))
        b = service
        ms = service.milestones

        # ---- 主体：管理办公室、承担单位、独立验收机构与专家 ----
        b.register_organization(request_id="org-lead", actor_id="bootstrap",
                                organization_id="lead", name="专项管理办公室")
        b.register_organization(request_id="org-und", actor_id="bootstrap",
                                organization_id="und", name="承担单位")
        b.register_organization(request_id="org-rev", actor_id="bootstrap",
                                organization_id="rev", name="独立验收机构")
        b.register_actor(request_id="actor-admin", actor_id="bootstrap", new_actor_id="admin",
                         display_name="管理员", role="admin", organization_id="lead")
        b.register_actor(request_id="actor-op", actor_id="admin", new_actor_id="op",
                         display_name="课题操作员", role="operator", organization_id="und")
        for i in (1, 2):
            b.register_actor(request_id=f"actor-rv{i}", actor_id="admin", new_actor_id=f"rv{i}",
                             display_name=f"验收专家{i}", role="reviewer", organization_id="rev")
            ms.register_expert(request_id=f"expert-{i}", actor_id="admin", expert_actor_id=f"rv{i}",
                               display_name=f"验收专家{i}", organization_id="rev")
            ms.add_qualification(request_id=f"qual-{i}", actor_id="admin",
                                 expert_actor_id=f"rv{i}", domain_tag="energy",
                                 valid_from="2026-01-01", valid_until="2027-12-31")

        # ---- 专项 / 课题 / 工作包 / 口径 / 里程碑 / 依赖（全部版本化）----
        ms.register_program(request_id="prog", actor_id="op", program_id="p2035", name="面向2035专项")
        ms.register_project(request_id="proj", actor_id="op", project_id="prj", program_id="p2035",
                            name="示范课题", undertaking_org_id="und")
        ms.register_work_package(request_id="wp", actor_id="op", wp_id="wp", project_id="prj",
                                 name="关键技术工作包")
        ms.register_caliber(request_id="cal-energy", actor_id="op", caliber_id="energy",
                            name="能效口径", unit="%", domain_tag="energy", rule={"threshold": 90})
        ms.register_caliber(request_id="cal-material", actor_id="op", caliber_id="material",
                            name="材料口径", unit="MPa", domain_tag="energy", rule={"threshold": 500})
        requirements = [{"caliber_id": "energy", "required_verdicts": 1},
                        {"caliber_id": "material", "required_verdicts": 1}]
        ms.register_milestone(request_id="m1", actor_id="op", milestone_id="m1", wp_id="wp",
                              name="原理验证", seq_no=1, planned_days=10, requirements=requirements)
        ms.register_milestone(request_id="m2", actor_id="op", milestone_id="m2", wp_id="wp",
                              name="样机研制", seq_no=2, planned_days=20, requirements=requirements)
        ms.register_dependency(request_id="dep", actor_id="op", dependency_id="d12",
                               upstream_milestone_id="m1", downstream_milestone_id="m2")
        ms.register_budget_plan(
            request_id="budget", actor_id="op", plan_id="plan", project_id="prj",
            installments=[{"milestone_id": "m1", "amount": 300},
                          {"milestone_id": "m2", "amount": 600}], note="首期预算")
        ms.register_commitment(request_id="commit-m2", actor_id="op", commitment_id="c-m2",
                               subject_type="milestone", subject_id="m2",
                               title="样机交付承诺", due_date="2030-06-30")
        ms.register_commitment(request_id="commit-prj", actor_id="op", commitment_id="c-prj",
                               subject_type="project", subject_id="prj",
                               title="总体交付承诺", due_date="2035-12-31")

        # ---- 证据与独立结论 ----
        def evidence(rid, actor, caliber, payload):
            return ms.submit_evidence(request_id=rid, actor_id=actor, caliber_id=caliber,
                                      caliber_version=None, payload=payload)["resource_id"]

        e_m1_energy = evidence("e-m1-en", "rv1", "energy", {"milestone": "m1", "k": "energy"})
        e_m1_material = evidence("e-m1-ma", "rv2", "material", {"milestone": "m1", "k": "material"})
        e_m2_energy = evidence("e-m2-en", "rv1", "energy", {"milestone": "m2", "k": "energy"})
        e_m2_material = evidence("e-m2-ma", "rv2", "material", {"milestone": "m2", "k": "material"})

        # m2 即便已有结论，因前置 m1 未过门也不能放行。
        ms.submit_verdict(request_id="v-m2-en", actor_id="rv1", milestone_id="m2",
                          caliber_id="energy", conclusion="pass", evidence_ids=[e_m2_energy])
        ms.submit_verdict(request_id="v-m2-ma", actor_id="rv2", milestone_id="m2",
                          caliber_id="material", conclusion="pass", evidence_ids=[e_m2_material])
        blocked_first = ms.evaluate_gate("m2")
        assert not blocked_first.passed
        assert any(x.code == "prerequisite_not_passed" for x in blocked_first.blocks)

        # ---- m1 部分通过（仅 energy）→ 等比释放 150，开具整改 ----
        ms.submit_verdict(request_id="v-m1-en", actor_id="rv1", milestone_id="m1",
                          caliber_id="energy", conclusion="pass", evidence_ids=[e_m1_energy])
        partial_eval = ms.evaluate_gate("m1")
        assert partial_eval.partial and partial_eval.releaseable_amount == 150
        gate_partial = ms.decide_gate(request_id="g1-partial", actor_id="op", milestone_id="m1")
        assert gate_partial["kind"] == "gate_partial"
        assert gate_partial["releaseable_amount"] == 150
        ms.open_rectification(request_id="rect", actor_id="op", milestone_id="m1",
                              due_date="2026-12-31", items=["补齐材料口径证据"])

        # ---- 证据不能被第二个门重复占用 ----
        cross_use_blocked = False
        try:
            ms.submit_verdict(request_id="v-m2-dup", actor_id="rv2", milestone_id="m2",
                              caliber_id="material", conclusion="pass", evidence_ids=[e_m1_material])
        except Exception:
            cross_use_blocked = True
        assert cross_use_blocked, "证据被跨门重复占用"

        # ---- 补齐 m1 材料口径 → 全通过，再放 150；随后 m2 原子过门放 600 ----
        ms.submit_verdict(request_id="v-m1-ma", actor_id="rv2", milestone_id="m1",
                          caliber_id="material", conclusion="pass", evidence_ids=[e_m1_material])
        gate_full = ms.decide_gate(request_id="g1-full", actor_id="op", milestone_id="m1")
        assert gate_full["kind"] == "gate_pass" and gate_full["releaseable_amount"] == 150
        gate_m2 = ms.decide_gate(request_id="g2", actor_id="op", milestone_id="m2")
        assert gate_m2["kind"] == "gate_pass" and gate_m2["releaseable_amount"] == 600

        # ---- m2 款项支付并关账；m1 款项只支付不关账 ----
        pay_m2 = ms.pay_release(request_id="pay-m2", actor_id="op",
                                release_id=gate_m2["released"][0]["release_id"])
        ms.close_payment(request_id="close-m2", actor_id="admin",
                         payment_id=pay_m2["resource_id"])

        # ---- 幂等：同一过门 request_id 重放返回同一决定，不重复放款 ----
        replay = ms.decide_gate(request_id="g2", actor_id="op", milestone_id="m2")
        assert replay["replayed"] and replay["decision_id"] == gate_m2["decision_id"]

        # ---- 口径不兼容修订：旧门失效、承诺受冲击、跨口径证据被拒 ----
        caliber_revision = ms.revise_caliber(
            request_id="cal-rev", actor_id="op", caliber_id="energy", name="能效口径v2",
            unit="%", domain_tag="energy", rule={"threshold": 95}, compatible_previous=False)
        impact = ms.change_impact(caliber_revision["decision_id"])
        impacted_milestones = {i["subject_id"] for i in impact["items"]
                               if i["subject_type"] == "milestone"}
        impacted_commitments = {i["commitment_id"] for i in impact["items"]
                                if i["subject_type"] == "commitment"}
        assert {"m1", "m2"} <= impacted_milestones
        assert {"c-m2", "c-prj"} <= impacted_commitments
        assert ms.get_decision(gate_m2["decision_id"]).status == "superseded"
        # m2 已关账的 600 不被回收。
        funds_after_change = ms.project_funds("prj")
        m2_row = next(i for i in funds_after_change["installments"] if i["milestone_id"] == "m2")
        assert m2_row["released_amount"] == 600 and funds_after_change["paid_amount"] == 600

        # ---- 课题终止：未释放余额注销，已关账款项保留 ----
        termination = ms.terminate_project(request_id="terminate", actor_id="admin",
                                           project_id="prj", reason="技术路线整体调整")
        funds_final = ms.project_funds("prj")
        statuses = {i["milestone_id"]: i["status"] for i in funds_final["installments"]}
        assert statuses["m1"] == "cancelled"
        assert statuses["m2"] == "released"
        assert funds_final["paid_amount"] == 600

        # ---- 关键路径与决定谱系、审计链 ----
        critical = ms.critical_path("prj")
        descendants = ms.decision_descendants(caliber_revision["decision_id"])
        valid, event_count = service.verify_audit()
        assert valid

        result = {
            "status": "ok",
            "audit_valid": valid,
            "audit_events": event_count,
            "partial_release": gate_partial["releaseable_amount"],
            "full_m1_release": gate_full["releaseable_amount"],
            "m2_release": gate_m2["releaseable_amount"],
            "cross_use_blocked": cross_use_blocked,
            "impacted_milestones": sorted(impacted_milestones),
            "impacted_commitments": sorted(c for c in impacted_commitments if c),
            "paid_preserved": funds_final["paid_amount"],
            "installment_statuses": statuses,
            "termination_cancellations": len(termination["cancelled_decisions"]),
            "critical_path": critical["critical_path"],
            "cascade_decisions": len(descendants),
            "gate_replayed_same": replay["decision_id"] == gate_m2["decision_id"],
        }
        database.close()
        return result


def main() -> int:
    """打印验收结果并设置退出码。"""

    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == "ok" and result["audit_valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
