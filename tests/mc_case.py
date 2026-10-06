import unittest
from datetime import datetime, timezone

from science_strategy_foundation.clock import FixedClock
from science_strategy_foundation.service import DomainService

from milestone_control.service import MilestoneControlService
from milestone_control.storage import MilestoneDatabase


class MilestoneCase(unittest.TestCase):
    """搭建一个含专项/课题/指标/专家的可用夹具。"""

    def setUp(self):
        self.database = MilestoneDatabase()
        self.clock = FixedClock(datetime(2026, 10, 6, tzinfo=timezone.utc))
        self.base = DomainService(self.database, self.clock)
        self.mc = MilestoneControlService(self.database, self.clock)
        self.base.register_organization(request_id="org", actor_id="bootstrap",
                                        organization_id="o1", name="专项管理机构")
        self.base.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="a1",
                                 display_name="管理员", role="admin", organization_id="o1")
        self.base.register_actor(request_id="op", actor_id="a1", new_actor_id="op1",
                                 display_name="操作员", role="operator", organization_id="o1")
        for suffix, name in [("1", "专家甲"), ("2", "专家乙"), ("3", "专家丙")]:
            self.base.register_actor(request_id=f"rv{suffix}", actor_id="a1",
                                     new_actor_id=f"rv{suffix}", display_name=name,
                                     role="reviewer", organization_id="o1")

    def tearDown(self):
        self.database.close()

    def seed_minimal(self, approvers=1):
        self.mc.register_project(request_id="pr", actor_id="op1", project_id="proj",
                                 name="专项")
        self.mc.register_topic(request_id="tp", actor_id="op1", topic_id="topic",
                               project_id="proj", name="课题")
        self.mc.register_work_package(request_id="wp", actor_id="op1", wp_id="wp",
                                      topic_id="topic", name="工作包")
        self.mc.register_metric(request_id="mt", actor_id="op1", metric_id="metric",
                                name="指标", unit="x", spec={"v": 1})
        self.mc.register_milestone(request_id="ga", actor_id="op1", milestone_id="g1",
                                   project_id="proj", topic_id="topic", name="阶段门",
                                   sequence_no=1, kind="gate", planned_date="2030-01-01",
                                   required_approvers=approvers, metric_ids=["metric"])
        self.mc.register_budget_installment(request_id="bi", actor_id="op1",
                                            installment_id="inst", project_id="proj",
                                            topic_id="topic", gate_milestone_id="g1",
                                            sequence_no=1, amount_cents=1000)
        for suffix in ["1", "2", "3"]:
            self.mc.register_expert(request_id=f"ex{suffix}", actor_id="a1",
                                    expert_id=f"expert{suffix}", expert_actor_id=f"rv{suffix}",
                                    display_name=f"专家{suffix}", qualifications=["验收专家"])
        self.mc.register_evidence(request_id="ev", actor_id="rv1", evidence_id="evi",
                                  title="证据", content_hash="h1",
                                  calibers=[{"metric_id": "metric"}])

    def pass_gate(self, decision_request="decide", approver="rv2"):
        self.mc.open_gate_round(request_id="open", actor_id="op1", gate_milestone_id="g1")
        self.mc.submit_acceptance(request_id="acc", actor_id="rv1", gate_milestone_id="g1",
                                  result="passed",
                                  readings=[{"metric_id": "metric", "evidence_id": "evi"}])
        self.mc.sign_gate(request_id="sign", actor_id=approver, gate_milestone_id="g1",
                          opinion="approved")
        return self.mc.decide_gate(request_id=decision_request, actor_id="op1",
                                   gate_milestone_id="g1")
