import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bin"))
import smart_focus_core as core  # noqa: E402


def pane(i):
    return {"type": "pane", "pane_id": i}


def split(direction, ratio, a, b):
    return {"type": "split", "direction": direction, "ratio": ratio, "first": a, "second": b}


TREE = split("right", 0.5, pane("A"), split("down", 0.5, pane("B"), pane("C")))


class JournalTest(unittest.TestCase):
    def test_none_without_move_out(self):
        ops = core.plan_stack(split("down", 0.5, pane("A"), pane("B")), "A", "stack-bottom")
        self.assertIsNone(core.journal_for("w:t", TREE, ops))

    def test_records_order_anchor_tab(self):
        ops = core.plan_stack(TREE, "B", "stack-bottom")
        j = core.journal_for("w:t", TREE, ops)
        self.assertEqual(j["tab"], "w:t")
        self.assertEqual(j["order"], ["A", "B", "C"])
        self.assertEqual(j["anchor"], "A")
        self.assertIsNone(j["tmp_tab"])

    def test_ops_panes_within_tree(self):
        ops = core.plan_stack(TREE, "C", "stack-right")
        self.assertTrue(core.ops_panes(ops) <= set(core.leaves(TREE)))
        self.assertEqual(core.ops_panes([("swap", "X", "Y"), ("focus", "Z")]), {"X", "Y", "Z"})


class RecoveryTest(unittest.TestCase):
    def test_returns_in_original_order_next_to_predecessor(self):
        moves = core.recovery_moves(["A", "B", "C", "D"], "A", ["D", "B"], ["A", "C"])
        self.assertEqual(moves[0], ("B", "right", "A"))
        self.assertEqual(moves[1], ("D", "down", "C"))

    def test_never_moves_foreign_panes(self):
        moves = core.recovery_moves(["A", "B"], "A", ["B", "FOREIGN"], ["A"])
        self.assertEqual([m[0] for m in moves], ["B"])

    def test_anchor_missing_falls_back_to_home_pane(self):
        self.assertEqual(core.recovery_moves(["A", "B"], "A", ["B"], ["Z"]), [("B", "right", "Z")])

    def test_empty_home_or_nothing_stranded(self):
        self.assertEqual(core.recovery_moves(["A", "B"], "A", ["B"], []), [])
        self.assertEqual(core.recovery_moves(["A", "B"], "A", [], ["A"]), [])


class TmpDirectionTest(unittest.TestCase):
    def test_alternates(self):
        self.assertEqual([core.tmp_split(i) for i in range(1, 6)],
                         ["right", "down", "right", "down", "right"])


class BackoffTest(unittest.TestCase):
    def test_limit_and_reset(self):
        t = core.FailureTracker(3)
        self.assertFalse(t.fail("x"))
        self.assertFalse(t.fail("x"))
        t.ok("x")
        self.assertFalse(t.fail("x"))
        self.assertFalse(t.fail("x"))
        self.assertTrue(t.fail("x"))
        self.assertFalse(t.fail("y"))

    def test_parse_requests_dedupes_in_order(self):
        self.assertEqual(core.parse_requests("a\n\nb\na\n  c \n"), ["a", "b", "c"])
        self.assertEqual(core.parse_requests(None), [])


if __name__ == "__main__":
    unittest.main()
