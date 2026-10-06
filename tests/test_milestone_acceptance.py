import unittest

from science_strategy_foundation.acceptance_milestone import run


class MilestoneAcceptanceTest(unittest.TestCase):
    def test_milestone_offline_acceptance(self):
        result = run()
        self.assertEqual("ok", result["status"])
        self.assertTrue(result["audit_valid"])
        # 两口径等比：先放 150，补齐后再放 150。
        self.assertEqual(150, result["partial_release"])
        self.assertEqual(150, result["full_m1_release"])
        self.assertEqual(600, result["m2_release"])
        # 证据唯一占用、跨门复用被拒。
        self.assertTrue(result["cross_use_blocked"])
        # 不兼容口径修订沿依赖冲击 m1/m2 及其承诺。
        self.assertEqual(["m1", "m2"], result["impacted_milestones"])
        self.assertEqual(["c-m2", "c-prj"], result["impacted_commitments"])
        # 已关账 600 在口径变更与课题终止后仍保留。
        self.assertEqual(600, result["paid_preserved"])
        self.assertEqual({"m1": "cancelled", "m2": "released"},
                         result["installment_statuses"])
        # 过门请求幂等重放得到同一决定。
        self.assertTrue(result["gate_replayed_same"])
        # 不兼容变更生成了后继决定谱系。
        self.assertGreaterEqual(result["cascade_decisions"], 1)


if __name__ == "__main__":
    unittest.main()
