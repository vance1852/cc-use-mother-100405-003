import tempfile
import threading
from pathlib import Path

from milestone_control.errors import GateBlocked, ImmutableError
from milestone_control.service import MilestoneControlService
from milestone_control.storage import MilestoneDatabase

from tests.mc_case import MilestoneCase


class GateFlowTest(MilestoneCase):
    def test_gate_cannot_pass_without_quorum_and_acceptance(self):
        self.seed_minimal(approvers=2)
        self.mc.open_gate_round(request_id="open", actor_id="op1", gate_milestone_id="g1")
        with self.assertRaises(GateBlocked) as caught:
            self.mc.decide_gate(request_id="decide0", actor_id="op1", gate_milestone_id="g1")
        codes = {r["code"] for r in caught.exception.reasons}
        self.assertIn("acceptance_missing", codes)
        self.assertIn("approvals_below_quorum", codes)

    def test_lead_expert_signoff_not_counted(self):
        self.seed_minimal(approvers=1)
        self.mc.open_gate_round(request_id="open", actor_id="op1", gate_milestone_id="g1")
        self.mc.submit_acceptance(request_id="acc", actor_id="rv1", gate_milestone_id="g1",
                                  result="passed",
                                  readings=[{"metric_id": "metric", "evidence_id": "evi"}])
        # 责任专家自己赞成也不能让门通过（独立性）
        with self.assertRaises(GateBlocked) as caught:
            self.mc.decide_gate(request_id="decide0", actor_id="op1", gate_milestone_id="g1")
        self.assertIn("approvals_below_quorum",
                      {r["code"] for r in caught.exception.reasons})

    def test_atomic_pass_releases_funds_once(self):
        self.seed_minimal()
        result = self.pass_gate()
        self.assertEqual(1000, result["released_cents"])
        pending = self.mc.pending_funds("proj")
        self.assertEqual(0, pending["pending_cents"])
        status = self.mc.gate_status("g1")
        self.assertEqual("passed", status["state"])
        self.assertEqual(1000, status["released_total_cents"])

    def test_evidence_cannot_be_reused_across_acceptances(self):
        self.seed_minimal()
        # 第二道同样需要 metric 的门
        self.mc.register_milestone(request_id="g2", actor_id="op1", milestone_id="g2",
                                   project_id="proj", topic_id="topic", name="门二",
                                   sequence_no=2, kind="gate", planned_date="2031-01-01",
                                   required_approvers=1, metric_ids=["metric"])
        self.pass_gate()
        self.mc.open_gate_round(request_id="open2", actor_id="op1", gate_milestone_id="g2")
        from science_strategy_foundation.errors import ConflictError
        with self.assertRaises(ConflictError):
            self.mc.submit_acceptance(request_id="acc2", actor_id="rv1",
                                      gate_milestone_id="g2", result="passed",
                                      readings=[{"metric_id": "metric", "evidence_id": "evi"}])

    def test_partial_pass_opens_successor_round_and_holds_funds(self):
        self.seed_minimal()
        self.mc.open_gate_round(request_id="open", actor_id="op1", gate_milestone_id="g1")
        self.mc.submit_acceptance(request_id="acc", actor_id="rv1", gate_milestone_id="g1",
                                  result="partial",
                                  readings=[{"metric_id": "metric", "evidence_id": "evi"}])
        result = self.mc.decide_gate(request_id="decide", actor_id="op1", gate_milestone_id="g1")
        self.assertEqual("partial", result["result"])
        self.assertEqual(0, result["released_cents"])
        self.assertIsNotNone(result["change_id"])
        # 自动生成后继轮次，资金仍待释放
        self.assertEqual(2, result["successor_round"])
        pending = self.mc.pending_funds("proj")
        self.assertEqual(1000, pending["pending_cents"])

    def test_rectification_then_pass_releases(self):
        self.seed_minimal()
        self.pass_gate()
        # 已通过门可被要求限期整改 → 新轮次
        rect = self.mc.issue_rectification(request_id="rect", actor_id="op1",
                                           gate_milestone_id="g1", due_date="2030-06-30",
                                           reason="个别指标需补测")
        self.assertEqual(2, rect["round"])
        # 新证据版本完成整改后再次通过
        self.mc.revise_evidence(request_id="ev2", actor_id="rv1", evidence_id="evi",
                                title="证据", content_hash="h2",
                                calibers=[{"metric_id": "metric"}])
        self.mc.submit_acceptance(request_id="acc2", actor_id="rv1", gate_milestone_id="g1",
                                  result="passed",
                                  readings=[{"metric_id": "metric", "evidence_id": "evi"}])
        self.mc.sign_gate(request_id="sign2", actor_id="rv2", gate_milestone_id="g1",
                          opinion="approved")
        result = self.mc.decide_gate(request_id="decide2", actor_id="op1", gate_milestone_id="g1")
        self.assertEqual("passed", result["result"])

    def test_withdraw_acceptance_revokes_unpaid_but_keeps_closed(self):
        self.seed_minimal()
        self.pass_gate()
        self.mc.mark_installment_paid(request_id="pay", actor_id="op1", installment_id="inst")
        self.mc.close_installment(request_id="close", actor_id="op1", installment_id="inst")
        acceptance_id = self.mc.gate_status("g1")["acceptance"]["acceptance_id"]
        result = self.mc.withdraw_acceptance(request_id="wd", actor_id="a1",
                                             acceptance_id=acceptance_id,
                                             reason="样本存疑")
        # 已关账金额不可撤销
        self.assertEqual(0, result["revoked_cents"])
        ledger = self.database.connection.execute(
            "SELECT status FROM mc_budget_ledger WHERE installment_id='inst'").fetchall()
        self.assertIn("closed", {row["status"] for row in ledger})
        versions = self.mc.list_versions("metric", "metric")
        self.assertEqual(1, len(versions["versions"]))

    def test_disabled_expert_signoff_drops_from_quorum(self):
        self.seed_minimal(approvers=1)
        self.mc.open_gate_round(request_id="open", actor_id="op1", gate_milestone_id="g1")
        self.mc.submit_acceptance(request_id="acc", actor_id="rv1", gate_milestone_id="g1",
                                  result="passed",
                                  readings=[{"metric_id": "metric", "evidence_id": "evi"}])
        self.mc.sign_gate(request_id="sign", actor_id="rv2", gate_milestone_id="g1",
                          opinion="approved")
        # 会签后专家资格被停用（保留旧版本快照）
        self.mc.revise_expert(request_id="ex2off", actor_id="a1", expert_id="expert2",
                              display_name="专家乙", qualifications=["验收专家"], active=False)
        with self.assertRaises(GateBlocked) as caught:
            self.mc.decide_gate(request_id="decide", actor_id="op1", gate_milestone_id="g1")
        self.assertIn("approvals_below_quorum",
                      {r["code"] for r in caught.exception.reasons})

    def test_topic_termination_freezes_planned_funds(self):
        self.seed_minimal()
        self.mc.terminate_topic(request_id="term", actor_id="a1", topic_id="topic",
                                reason="任务终止")
        row = self.database.connection.execute(
            "SELECT status FROM mc_budget_installments WHERE installment_id='inst'").fetchone()
        self.assertEqual("void", row["status"])
        with self.assertRaises(ImmutableError):
            self.mc.open_gate_round(request_id="open", actor_id="op1", gate_milestone_id="g1")

    def test_incompatible_metric_blocks_old_evidence(self):
        self.seed_minimal()
        self.pass_gate()
        self.mc.revise_metric(request_id="mt2", actor_id="op1", metric_id="metric",
                              name="指标", unit="x", spec={"v": 2}, compatible=False,
                              reason="口径变更")
        self.mc.issue_rectification(request_id="rect", actor_id="op1",
                                    gate_milestone_id="g1", due_date="2030-09-30",
                                    reason="口径升级")
        from science_strategy_foundation.errors import ValidationError
        with self.assertRaises(ValidationError):
            self.mc.submit_acceptance(request_id="acc2", actor_id="rv1",
                                      gate_milestone_id="g1", result="passed",
                                      readings=[{"metric_id": "metric", "evidence_id": "evi"}])

    def test_idempotent_replay_after_state_advances(self):
        self.seed_minimal()
        self.pass_gate()
        # 再次提交完全相同的注册请求，应回放原回执而非报“已存在”
        replay = self.mc.register_project(request_id="pr", actor_id="op1",
                                          project_id="proj", name="专项")
        self.assertTrue(replay["replayed"])

    def test_concurrent_signoffs_yield_single_stable_result(self):
        self.seed_minimal(approvers=2)
        self.mc.open_gate_round(request_id="open", actor_id="op1", gate_milestone_id="g1")
        self.mc.submit_acceptance(request_id="acc", actor_id="rv1", gate_milestone_id="g1",
                                  result="passed",
                                  readings=[{"metric_id": "metric", "evidence_id": "evi"}])
        barrier = threading.Barrier(2)

        def sign(actor, request_id):
            barrier.wait()
            self.mc.sign_gate(request_id=request_id, actor_id=actor,
                              gate_milestone_id="g1", opinion="approved")

        t1 = threading.Thread(target=sign, args=("rv2", "s2"))
        t2 = threading.Thread(target=sign, args=("rv3", "s3"))
        t1.start(); t2.start(); t1.join(); t2.join()
        result = self.mc.decide_gate(request_id="decide", actor_id="op1", gate_milestone_id="g1")
        self.assertEqual("passed", result["result"])
        self.assertEqual(1000, result["released_cents"])
        # 并发重放决定不产生第二笔释放
        replay = self.mc.decide_gate(request_id="decide", actor_id="op1", gate_milestone_id="g1")
        self.assertTrue(replay["replayed"])

    def test_state_survives_restart(self):
        import sqlite3

        self.seed_minimal()
        self.pass_gate()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "restart.sqlite3"
            target = sqlite3.connect(path)
            self.database.connection.backup(target)
            target.close()
            reopened_db = MilestoneDatabase(path)
            reopened = MilestoneControlService(reopened_db)
            status = reopened.gate_status("g1")
            self.assertEqual("passed", status["state"])
            self.assertEqual(1000, status["released_total_cents"])
            pending = reopened.pending_funds("proj")
            self.assertEqual(0, pending["pending_cents"])
            valid, _ = reopened.verify_audit()
            self.assertTrue(valid)
            reopened_db.close()

    def test_change_impact_lists_affected_commitments(self):
        self.seed_minimal()
        self.mc.register_commitment(request_id="cm", actor_id="op1", commitment_id="com",
                                    wp_id="wp", milestone_id="g1", title="承诺",
                                    due_date="2030-01-01", metric_id="metric",
                                    target_value={"min": 1})
        self.pass_gate()
        result = self.mc.revise_metric(request_id="mt2", actor_id="op1", metric_id="metric",
                                       name="指标", unit="x", spec={"v": 2},
                                       compatible=False, reason="口径变更")
        impact = self.mc.change_impact(result["change_id"])
        self.assertIn("com", impact["affected_commitments"])
        self.assertEqual("route_adjustment", impact["change_type"])


if __name__ == "__main__":
    unittest.main()
