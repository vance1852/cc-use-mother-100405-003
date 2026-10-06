import unittest

from milestone_control.api import route

from tests.mc_case import MilestoneCase


class ApiTest(MilestoneCase):
    def call(self, method, path, body=None, actor="op1"):
        return route(self.mc, method, path, body, {"X-Actor-Id": actor})

    def bootstrap_and_seed(self):
        self.seed_minimal()

    def test_health(self):
        status, payload = self.call("GET", "/mc/health", None, actor="op1")
        self.assertEqual(200, status)
        self.assertEqual("ok", payload["status"])
        self.assertTrue(payload["audit_valid"])

    def test_unknown_route(self):
        status, payload = self.call("GET", "/mc/nope", None)
        self.assertEqual(404, status)
        self.assertEqual("route_not_found", payload["error"])

    def test_register_and_gate_flow_over_route(self):
        self.bootstrap_and_seed()
        status, body = self.call("POST", "/mc/gates/open",
                                 {"request_id": "open", "gate_milestone_id": "g1"})
        self.assertEqual(201, status)
        status, body = self.call(
            "POST", "/mc/gates/acceptance",
            {"request_id": "acc", "gate_milestone_id": "g1", "result": "passed",
             "readings": [{"metric_id": "metric", "evidence_id": "evi"}]}, actor="rv1")
        self.assertEqual(201, status)
        status, body = self.call("POST", "/mc/gates/sign",
                                 {"request_id": "sign", "gate_milestone_id": "g1",
                                  "opinion": "approved"}, actor="rv2")
        self.assertEqual(201, status)
        status, body = self.call("POST", "/mc/gates/decide",
                                 {"request_id": "decide", "gate_milestone_id": "g1"})
        self.assertEqual(201, status)
        self.assertEqual(1000, body["released_cents"])
        # 重放得到 200 与同一资源
        status, replay = self.call("POST", "/mc/gates/decide",
                                   {"request_id": "decide", "gate_milestone_id": "g1"})
        self.assertEqual(200, status)
        self.assertTrue(replay["replayed"])
        self.assertEqual(body["decision_id"], replay["decision_id"])

    def test_gate_blocked_returns_reasons(self):
        self.bootstrap_and_seed()
        self.call("POST", "/mc/gates/open",
                  {"request_id": "open", "gate_milestone_id": "g1"})
        status, body = self.call("POST", "/mc/gates/decide",
                                 {"request_id": "decide", "gate_milestone_id": "g1"})
        self.assertEqual(409, status)
        self.assertEqual("gate_blocked", body["error"])
        self.assertTrue(any(r["code"] == "acceptance_missing" for r in body["reasons"]))

    def test_pending_funds_and_critical_path(self):
        self.bootstrap_and_seed()
        status, body = self.call("GET", "/mc/funds/pending?project_id=proj", None)
        self.assertEqual(200, status)
        self.assertEqual(1000, body["pending_cents"])
        status, body = self.call("GET", "/mc/critical-path?project_id=proj", None)
        self.assertEqual(200, status)
        self.assertIn("g1", body["critical_path"])

    def test_change_impact_and_versions(self):
        self.bootstrap_and_seed()
        status, body = self.call(
            "POST", "/mc/metrics/revise",
            {"request_id": "mt2", "metric_id": "metric", "name": "指标", "unit": "x",
             "spec": {"v": 2}, "compatible": False, "reason": "口径变更"})
        self.assertEqual(201, status)
        change_id = body["change_id"]
        status, impact = self.call("GET", f"/mc/changes/{change_id}", None)
        self.assertEqual(200, status)
        self.assertEqual("route_adjustment", impact["change_type"])
        status, versions = self.call("GET", "/mc/versions/metric/metric", None)
        self.assertEqual(200, status)
        self.assertEqual(2, len(versions["versions"]))

    def test_invalid_json_shape_is_400(self):
        status, body = route(self.mc, "POST", "/mc/projects",
                             {"request_id": "x"}, {"X-Actor-Id": "op1"})
        self.assertEqual(400, status)
        self.assertEqual("invalid_request", body["error"])


if __name__ == "__main__":
    unittest.main()
