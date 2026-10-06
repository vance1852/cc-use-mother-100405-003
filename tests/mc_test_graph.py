import unittest

from milestone_control.graph import (
    caliber_compatible,
    critical_path,
    reachable_downstream,
    topological_order,
)


class GraphTest(unittest.TestCase):
    def test_reachable_downstream_follows_chain(self):
        graph = {"a": {"b"}, "b": {"c"}, "c": {"d"}, "d": set()}
        self.assertEqual(reachable_downstream(graph, "a"), {"b", "c", "d"})
        self.assertEqual(reachable_downstream(graph, "b"), {"c", "d"})

    def test_topological_order_is_stable(self):
        graph = {"a": {"b", "c"}, "b": {"d"}, "c": {"d"}, "d": set()}
        self.assertEqual(topological_order(graph, ["a", "b", "c", "d"]), ["a", "b", "c", "d"])

    def test_critical_path_picks_longest_chain(self):
        graph = {"a": {"b", "c"}, "b": {"d"}, "c": {"d"}, "d": set()}
        durations = {"a": 1, "b": 10, "c": 2, "d": 1}
        path = critical_path(graph, durations, set(), {"a"})
        self.assertEqual(path, ["a", "b", "d"])

    def test_critical_path_completed_nodes_have_zero_duration(self):
        # a2(100)->b(1) 长于 a1(1)->c(50)；a2 完工归零后最长链翻到 a1->c
        graph = {"a1": {"c"}, "a2": {"b"}, "b": set(), "c": set()}
        durations = {"a1": 1, "a2": 100, "b": 1, "c": 50}
        self.assertEqual(critical_path(graph, durations, set(), {"a1", "a2"}), ["a2", "b"])
        self.assertEqual(critical_path(graph, durations, {"a2"}, {"a1", "a2"}),
                         ["a1", "c"])

    def test_caliber_compatibility_rules(self):
        history = [
            {"version": 1, "compatible": 1},
            {"version": 2, "compatible": 1},
            {"version": 3, "compatible": 0},
            {"version": 4, "compatible": 1},
        ]
        self.assertTrue(caliber_compatible(history, 3, 4))
        self.assertFalse(caliber_compatible(history, 2, 3))
        # 一旦跨过不兼容版本，后续兼容也不能复活旧证据
        self.assertFalse(caliber_compatible(history, 2, 4))
        self.assertTrue(caliber_compatible(history, 4, 4))


if __name__ == "__main__":
    unittest.main()
