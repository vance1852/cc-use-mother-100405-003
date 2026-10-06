"""里程碑与变更控制服务的核心规则测试。"""
import json
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path

from science_strategy_foundation.clock import FixedClock
from science_strategy_foundation.errors import (
    ConflictError, PermissionDenied, PreconditionError, StateError, ValidationError)
from science_strategy_foundation.milestone_service import MilestoneService
from science_strategy_foundation.service import DomainService
from science_strategy_foundation.storage import Database


class MilestoneCase(unittest.TestCase):
    def setUp(self):
        self.db = Database()
        clock = FixedClock(datetime(2026, 10, 6, tzinfo=timezone.utc))
        self.base = DomainService(self.db, clock)
        self.ms: MilestoneService = self.base.milestones
        self._bootstrap()

    def tearDown(self):
        self.db.close()

    def _bootstrap(self):
        b = self.base
        b.register_organization(request_id="o1", actor_id="bootstrap",
                                organization_id="lead", name="管理办公室")
        b.register_organization(request_id="o2", actor_id="bootstrap",
                                organization_id="und", name="承担单位")
        b.register_organization(request_id="o3", actor_id="bootstrap",
                                organization_id="rev", name="验收机构")
        b.register_actor(request_id="aadmin", actor_id="bootstrap", new_actor_id="admin",
                         display_name="管理员", role="admin", organization_id="lead")
        b.register_actor(request_id="aop", actor_id="admin", new_actor_id="op",
                         display_name="操作员", role="operator", organization_id="und")
        for i in (1, 2, 3):
            b.register_actor(request_id=f"arv{i}", actor_id="admin", new_actor_id=f"rv{i}",
                             display_name=f"专家{i}", role="reviewer", organization_id="rev")
            self.ms.register_expert(request_id=f"ex{i}", actor_id="admin", expert_actor_id=f"rv{i}",
                                    display_name=f"专家{i}", organization_id="rev")
            self.ms.add_qualification(request_id=f"q{i}", actor_id="admin",
                                      expert_actor_id=f"rv{i}", domain_tag="dom",
                                      valid_from="2026-01-01", valid_until="2027-12-31")

    def _project(self, caliber_ids=("c1",), requirements=None, milestones=("m1",),
                 chain=True, amounts=None):
        ms = self.ms
        ms.register_program(request_id="p", actor_id="op", program_id="prog", name="专项")
        ms.register_project(request_id="pr", actor_id="op", project_id="prj",
                            program_id="prog", name="课题", undertaking_org_id="und")
        ms.register_work_package(request_id="wp", actor_id="op", wp_id="wp",
                                 project_id="prj", name="工作包")
        for cid in caliber_ids:
            ms.register_caliber(request_id=f"cal-{cid}", actor_id="op", caliber_id=cid,
                                name=f"口径{cid}", unit="u", domain_tag="dom",
                                rule={"k": cid})
        requirements = requirements or [{"caliber_id": c, "required_verdicts": 1}
                                        for c in caliber_ids]
        days = {"m1": 10, "m2": 20, "m3": 15}
        for mid in milestones:
            ms.register_milestone(request_id=f"reg-{mid}", actor_id="op", milestone_id=mid,
                                  wp_id="wp", name=f"里程碑{mid}", seq_no=int(mid[1]),
                                  planned_days=days.get(mid, 10), requirements=requirements)
        if chain and len(milestones) > 1:
            ordered = list(milestones)
            for up, down in zip(ordered, ordered[1:]):
                ms.register_dependency(request_id=f"dep-{up}-{down}", actor_id="op",
                                       dependency_id=f"d-{up}-{down}",
                                       upstream_milestone_id=up, downstream_milestone_id=down)
        if amounts:
            ms.register_budget_plan(
                request_id="bp", actor_id="op", plan_id="plan", project_id="prj",
                installments=[{"milestone_id": m, "amount": a} for m, a in zip(milestones, amounts)],
                note="预算")

    def _evidence(self, tag, caliber="c1", expert="rv1", version=None):
        rid = f"ev-{tag}-{caliber}-{expert}"
        return self.ms.submit_evidence(request_id=rid, actor_id=expert, caliber_id=caliber,
                                       caliber_version=version,
                                       payload={"tag": tag, "caliber": caliber})["resource_id"]

    def _pass(self, rid, expert, milestone, caliber, evidence):
        return self.ms.submit_verdict(request_id=rid, actor_id=expert, milestone_id=milestone,
                                      caliber_id=caliber, conclusion="pass",
                                      evidence_ids=[evidence])["resource_id"]


class GateAndFundsTest(MilestoneCase):
    def test_prerequisite_blocks_then_atomic_pass_releases(self):
        self._project(caliber_ids=("c1",), milestones=("m1", "m2"), amounts=(100, 200))
        e_m2 = self._evidence("m2")
        self._pass("v-m2", "rv1", "m2", "c1", e_m2)
        evaluation = self.ms.evaluate_gate("m2")
        self.assertFalse(evaluation.passed)
        self.assertIn("prerequisite_not_passed", [b.code for b in evaluation.blocks])
        with self.assertRaises(PreconditionError):
            self.ms.decide_gate(request_id="g2-early", actor_id="op", milestone_id="m2")
        # m1 过门
        self._pass("v-m1", "rv1", "m1", "c1", self._evidence("m1"))
        gate1 = self.ms.decide_gate(request_id="g1", actor_id="op", milestone_id="m1")
        self.assertEqual("gate_pass", gate1["kind"])
        self.assertEqual(100, gate1["releaseable_amount"])
        # 前置满足后 m2 原子过门
        gate2 = self.ms.decide_gate(request_id="g2", actor_id="op", milestone_id="m2")
        self.assertEqual("gate_pass", gate2["kind"])
        self.assertEqual(200, gate2["releaseable_amount"])
        funds = self.ms.project_funds("prj")
        self.assertEqual(2, sum(1 for i in funds["installments"] if i["status"] == "released"))

    def test_partial_gate_releases_proportional_and_rectification(self):
        self._project(caliber_ids=("c1", "c2"), milestones=("m1",), amounts=(100,))
        self._pass("v1", "rv1", "m1", "c1", self._evidence("m1", "c1", "rv1"))
        evaluation = self.ms.evaluate_gate("m1")
        self.assertFalse(evaluation.passed)
        self.assertTrue(evaluation.partial)
        self.assertEqual(50, evaluation.releaseable_amount)  # 100 // 2 个口径
        gate = self.ms.decide_gate(request_id="gp", actor_id="op", milestone_id="m1")
        self.assertEqual("gate_partial", gate["kind"])
        self.assertEqual(50, gate["releaseable_amount"])
        rect = self.ms.open_rectification(request_id="rect", actor_id="op", milestone_id="m1",
                                          due_date="2026-12-31", items=["补齐 c2 证据"])
        self.assertTrue(rect["decision_id"])
        # 补齐后再次过门，释放剩余 50
        self.ms.register_caliber(request_id="cal-c2b", actor_id="op", caliber_id="c2",
                                 name="口径c2", unit="u", domain_tag="dom", rule={"k": "c2"}) \
            if False else None
        self._pass("v2", "rv2", "m1", "c2", self._evidence("m1", "c2", "rv2"))
        gate_full = self.ms.decide_gate(request_id="gf", actor_id="op", milestone_id="m1")
        self.assertEqual("gate_pass", gate_full["kind"])
        self.assertEqual(50, gate_full["releaseable_amount"])
        inst = self.ms.list_installments("prj")[0]
        self.assertEqual(100, inst.released_amount)
        self.assertEqual("released", inst.status)

    def test_evidence_cannot_be_reused_or_cross_incompatible_caliber(self):
        self._project(caliber_ids=("c1",), milestones=("m1", "m2"), amounts=(100, 200))
        e1 = self._evidence("m1")
        self._pass("v-m1", "rv1", "m1", "c1", e1)
        self.ms.decide_gate(request_id="g1", actor_id="op", milestone_id="m1")
        # 同一证据不能再被 m2 占用
        with self.assertRaises(ConflictError):
            self._pass("v-m2-dup", "rv2", "m2", "c1", e1)
        # 口径不兼容修订后，旧兼容类证据不能用于新口径
        self.ms.revise_caliber(request_id="cv", actor_id="op", caliber_id="c1", name="口径c1v2",
                               unit="u", domain_tag="dom", rule={"k": "v2"},
                               compatible_previous=False)
        e_new = self._evidence("new", version=None)
        # m2 仍钉在 v1（注册时绑定），新证据属于新兼容类 → 不兼容
        with self.assertRaises(ConflictError):
            self._pass("v-m2-new", "rv1", "m2", "c1", e_new)

    def test_independent_expert_and_qualification_enforced(self):
        self._project(caliber_ids=("c1",), milestones=("m1",))
        # 承担机构的操作者不能当验收专家（op 属于 und）
        with self.assertRaises(PermissionDenied):
            self.ms.submit_verdict(request_id="bad-actor", actor_id="op", milestone_id="m1",
                                   caliber_id="c1", conclusion="pass",
                                   evidence_ids=[self._evidence("x")])
        # 资格撤销后不能提交结论
        qid = self.db.connection.execute(
            "SELECT qualification_id FROM expert_qualifications WHERE expert_id="
            "(SELECT expert_id FROM experts WHERE actor_id='rv1')").fetchone()[0]
        self.ms.revoke_qualification(request_id="rq", actor_id="admin",
                                     qualification_id=qid, reason="资格问题")
        with self.assertRaises(PermissionDenied):
            self.ms.submit_verdict(request_id="no-qual", actor_id="rv1", milestone_id="m1",
                                   caliber_id="c1", conclusion="pass",
                                   evidence_ids=[self._evidence("y")])


class CascadeTest(MilestoneCase):
    def _chain_with_gates(self):
        self._project(caliber_ids=("c1",), milestones=("m1", "m2", "m3"),
                      amounts=(300, 600, 900))
        self.ms.register_commitment(request_id="cm2", actor_id="op", commitment_id="cm2",
                                    subject_type="milestone", subject_id="m2",
                                    title="样机", due_date="2030-01-01")
        self.ms.register_commitment(request_id="cmp", actor_id="op", commitment_id="cmp",
                                    subject_type="project", subject_id="prj",
                                    title="总交付", due_date="2035-01-01")
        for mid in ("m1", "m2", "m3"):
            self._pass(f"v-{mid}", "rv1", mid, "c1", self._evidence(mid))
        g1 = self.ms.decide_gate(request_id="g1", actor_id="op", milestone_id="m1")
        g2 = self.ms.decide_gate(request_id="g2", actor_id="op", milestone_id="m2")
        g3 = self.ms.decide_gate(request_id="g3", actor_id="op", milestone_id="m3")
        return g1, g2, g3

    def test_incompatible_caliber_change_invalidates_downstream_and_commitments(self):
        _, g2, g3 = self._chain_with_gates()
        rev = self.ms.revise_caliber(request_id="cv", actor_id="op", caliber_id="c1",
                                     name="口径v2", unit="u", domain_tag="dom",
                                     rule={"k": "v2"}, compatible_previous=False)
        impact = self.ms.change_impact(rev["decision_id"])
        impacted_milestones = {i["subject_id"] for i in impact["items"]
                               if i["subject_type"] == "milestone"}
        self.assertEqual({"m1", "m2", "m3"}, impacted_milestones)
        impacted_commitments = {i["commitment_id"] for i in impact["items"]
                                if i["subject_type"] == "commitment"}
        self.assertEqual({"cm2", "cmp"}, impacted_commitments)
        # 旧门全部失效
        for gid in (g2["decision_id"], g3["decision_id"]):
            self.assertEqual("superseded", self.ms.get_decision(gid).status)
        # 后继决定沿闭包生成
        descendants = self.ms.decision_descendants(rev["decision_id"])
        self.assertTrue(any(d.kind == "impact" and d.subject_id == "m3" for d in descendants))

    def test_withdrawal_revokes_unpaid_but_keeps_closed_payment(self):
        g1, g2, _ = self._chain_with_gates()
        # m2 放款→支付→关账
        rel2 = g2["released"][0]["release_id"]
        pay = self.ms.pay_release(request_id="pay2", actor_id="op", release_id=rel2)
        self.ms.close_payment(request_id="close2", actor_id="admin",
                              payment_id=pay["resource_id"])
        # m1 放款但不支付
        verdict_m1 = self._pass  # noqa
        v1_id = self.db.connection.execute(
            "SELECT verdict_id FROM acceptance_verdicts WHERE milestone_id='m1'").fetchone()[0]
        self.ms.withdraw_verdict(request_id="w1", actor_id="rv1", verdict_id=v1_id,
                                 reason="数据更正")
        funds = self.ms.project_funds("prj")
        by_m = {i["milestone_id"]: i for i in funds["installments"]}
        # m1 未支付释放被回收
        self.assertEqual(0, by_m["m1"]["released_amount"])
        # m2 已关账金额保留
        self.assertEqual(600, by_m["m2"]["released_amount"])
        self.assertEqual(600, funds["paid_amount"])

    def test_termination_cancels_unreleased_keeps_paid(self):
        _, g2, _ = self._chain_with_gates()
        rel2 = g2["released"][0]["release_id"]
        pay = self.ms.pay_release(request_id="pay", actor_id="op", release_id=rel2)
        self.ms.close_payment(request_id="close", actor_id="admin",
                              payment_id=pay["resource_id"])
        term = self.ms.terminate_project(request_id="term", actor_id="admin",
                                         project_id="prj", reason="取消")
        self.assertEqual(2, len(term["cancelled_decisions"]))  # m1、m3 未支付余额注销
        funds = self.ms.project_funds("prj")
        status = {i["milestone_id"]: i["status"] for i in funds["installments"]}
        self.assertEqual("cancelled", status["m1"])
        self.assertEqual("released", status["m2"])  # 已关账保留
        self.assertEqual("cancelled", status["m3"])
        # 里程碑终止，不能再验收/过门
        with self.assertRaises(StateError):
            self.ms.submit_verdict(request_id="late", actor_id="rv2", milestone_id="m1",
                                   caliber_id="c1", conclusion="pass",
                                   evidence_ids=[self._evidence("late")])


class VersioningTest(MilestoneCase):
    def test_milestone_revision_freezes_old_and_cascades(self):
        self._project(caliber_ids=("c1",), milestones=("m1", "m2"), amounts=(100, 200))
        self._pass("v1", "rv1", "m1", "c1", self._evidence("m1"))
        self.ms.decide_gate(request_id="g1", actor_id="op", milestone_id="m1")
        self.ms.register_milestone(request_id="ignore", actor_id="op", milestone_id="m2",
                                   wp_id="wp", name="x", seq_no=2, planned_days=20,
                                   requirements=[{"caliber_id": "c1", "required_verdicts": 1}]) \
            if False else None
        rev = self.ms.revise_milestone(
            request_id="mr", actor_id="op", milestone_id="m1", name="里程碑1修订",
            seq_no=1, planned_days=12,
            requirements=[{"caliber_id": "c1", "required_verdicts": 2}], reason="路线调整")
        self.assertEqual(2, rev["version"])
        view = self.ms.get_milestone("m1")
        self.assertEqual(2, view.version)
        self.assertEqual(2, view.requirements[0]["required_verdicts"])
        # 旧版本结论仍保留可审计
        old_verdicts = [v for v in self.ms.list_verdicts("m1") if v.milestone_version == 1]
        self.assertEqual(1, len(old_verdicts))
        # m2 受影响（其前置 m1 改了版本），且传播产生 impact
        impact = self.ms.change_impact(rev["decision_id"])
        self.assertIn("m2", {i["subject_id"] for i in impact["items"]
                             if i["subject_type"] == "milestone"})

    def test_dependency_cycle_rejected(self):
        self._project(caliber_ids=("c1",), milestones=("m1", "m2"), chain=True)
        with self.assertRaises(ConflictError):
            self.ms.register_dependency(request_id="cycle", actor_id="op",
                                        dependency_id="cyc",
                                        upstream_milestone_id="m2",
                                        downstream_milestone_id="m1")


class DeterminismTest(MilestoneCase):
    def test_concurrent_gate_decisions_have_single_effective_outcome(self):
        self._project(caliber_ids=("c1",), milestones=("m1",), amounts=(100,))
        self._pass("v1", "rv1", "m1", "c1", self._evidence("m1"))
        outcomes: list[dict] = []
        errors: list[Exception] = []

        def decide(rid):
            try:
                outcomes.append(self.ms.decide_gate(request_id=rid, actor_id="op",
                                                    milestone_id="m1"))
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=decide, args=(f"g-{i}",)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertFalse(errors, errors)
        effective = [d for d in self.ms.list_decisions("milestone", "m1")
                     if d.status == "effective" and d.kind in ("gate_pass", "gate_partial")]
        self.assertEqual(1, len(effective))
        self.assertEqual({effective[0].decision_id}, {o["decision_id"] for o in outcomes})
        # 只释放一次额度
        self.assertEqual(100, self.ms.list_installments("prj")[0].released_amount)

    def test_restart_preserves_state_and_audit_chain(self):
        self._project(caliber_ids=("c1",), milestones=("m1",), amounts=(100,))
        self._pass("v1", "rv1", "m1", "c1", self._evidence("m1"))
        gate = self.ms.decide_gate(request_id="g1", actor_id="op", milestone_id="m1")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            disk = Database(path)
            disk.close()
            # 把内存库内容复制到磁盘后重开
            target = Database(path)
            self.db.connection.backup(target.connection)
            target.close()
            reopened = Database(path)
            svc = DomainService(reopened)
            self.assertEqual("gate_pass",
                             svc.milestones.get_decision(gate["decision_id"]).kind)
            self.assertEqual(100, svc.milestones.list_installments("prj")[0].released_amount)
            valid, _ = svc.verify_audit()
            self.assertTrue(valid)
            # 重放同一 request_id 返回同一决定
            again = svc.milestones.decide_gate(request_id="g1", actor_id="op", milestone_id="m1")
            self.assertTrue(again["replayed"])
            self.assertEqual(gate["decision_id"], again["decision_id"])
            reopened.close()


if __name__ == "__main__":
    unittest.main()
