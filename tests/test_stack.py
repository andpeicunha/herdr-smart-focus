import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bin"))
import smart_focus_core as core  # noqa: E402


def pane(i):
    return {"type": "pane", "pane_id": i}


def split(direction, ratio, a, b):
    return {"type": "split", "direction": direction, "ratio": ratio, "first": a, "second": b}


def ratios(ops):
    return {tuple(o[1]): o[2] for o in ops if o[0] == "ratio"}


def kinds(ops):
    return [o[0] for o in ops]


# (A | B | C) over D, ratios already at target for stack 0.72
BOTTOM_OK = split("down", 0.28, split("right", 1 / 3.0, pane("A"), split("right", 0.5, pane("B"), pane("C"))),
                  pane("D"))


class ClampTest(unittest.TestCase):
    def test_targets_clamped_to_herdr_range(self):
        # 11 stacked panes: balance target 1/11 < 0.1 must be clamped
        t = pane("P10")
        for i in range(9, -1, -1):
            t = split("down", 0.5, pane("P%d" % i), t)
        for path, r in core.compute_ops(t, "P10", 0.9, clamp_targets=True):
            self.assertGreaterEqual(r, 0.1)
            self.assertLessEqual(r, 0.9)

    def test_no_redundant_ops_at_clamp(self):
        t = split("down", 0.1, pane("A"), pane("B"))  # herdr already clamped here
        self.assertEqual(core.compute_ops(t, "B", 0.9, clamp_targets=True), [])

    def test_stack_ratio_clamp(self):
        self.assertEqual(core.clamp_stack_ratio(2), 0.9)
        self.assertEqual(core.clamp_stack_ratio(0.01), 0.1)
        self.assertEqual(core.clamp_stack_ratio("x"), core.DEFAULT_STACK_RATIO)

    def test_normalize_layout(self):
        self.assertEqual(core.normalize_layout("Stack-Bottom"), "stack-bottom")
        self.assertEqual(core.normalize_layout("bogus"), "keep")
        self.assertEqual(core.normalize_layout(None), "keep")


class PlanStackTest(unittest.TestCase):
    def test_single_pane_and_unknown(self):
        self.assertEqual(core.plan_stack(pane("A"), "A", "stack-bottom"), [])
        self.assertEqual(core.plan_stack(BOTTOM_OK, "Z", "stack-bottom"), [])
        self.assertEqual(core.plan_stack(BOTTOM_OK, "A", "keep"), [])

    def test_idempotent_when_in_place(self):
        self.assertEqual(core.plan_stack(BOTTOM_OK, "D", "stack-bottom", 0.72), [])

    def test_swap_when_focus_in_strip(self):
        ops = core.plan_stack(BOTTOM_OK, "B", "stack-bottom", 0.72)
        self.assertEqual(ops, [("swap", "B", "D")])

    def test_swap_plus_ratio_fix(self):
        t = split("down", 0.5, split("right", 0.5, pane("A"), pane("B")), pane("C"))
        ops = core.plan_stack(t, "A", "stack-bottom", 0.72)
        self.assertEqual(ops[0], ("swap", "A", "C"))
        self.assertEqual(ratios(ops), {(): 0.28})

    def test_restructure_reading_order_bottom(self):
        t = split("right", 0.5, pane("A"), split("down", 0.5, pane("B"), pane("C")))
        ops = core.plan_stack(t, "B", "stack-bottom", 0.72)
        self.assertEqual(ops[:6], [
            ("move_out", "B"), ("move_out", "C"),
            ("move_in", "B", "down", "A"),
            ("move_in", "C", "right", "A"),
            ("focus", "B"),
            ("ratio", [], 0.28)])
        self.assertEqual(ratios(ops), {(): 0.28, (False,): 0.5})

    def test_restructure_four_panes_equal_strip(self):
        t = split("right", 0.5, pane("A"), split("down", 0.5, pane("B"), split("right", 0.5, pane("C"), pane("D"))))
        ops = core.plan_stack(t, "D", "stack-bottom", 0.8)
        self.assertEqual([o for o in ops if o[0] == "move_in"], [
            ("move_in", "D", "down", "A"),
            ("move_in", "B", "right", "A"),
            ("move_in", "C", "right", "B")])
        r = ratios(ops)
        self.assertEqual(r[()], 0.2)
        self.assertAlmostEqual(r[(False,)], 1 / 3.0, places=3)
        self.assertEqual(r[(False, True)], 0.5)
        self.assertEqual(ops[-1][0], "ratio")

    def test_stack_right_directions(self):
        t = split("down", 0.5, pane("A"), split("down", 0.5, pane("B"), pane("C")))
        ops = core.plan_stack(t, "C", "stack-right", 0.7)
        self.assertEqual([o for o in ops if o[0] == "move_in"], [
            ("move_in", "C", "right", "A"),
            ("move_in", "B", "down", "A")])
        r = ratios(ops)
        self.assertAlmostEqual(r[()], 0.3, places=4)
        self.assertEqual(r[(False,)], 0.5)

    def test_stack_right_in_place_swap(self):
        t = split("right", 0.3, split("down", 0.5, pane("A"), pane("B")), pane("C"))
        self.assertEqual(core.plan_stack(t, "C", "stack-right", 0.7), [])
        self.assertEqual(core.plan_stack(t, "A", "stack-right", 0.7), [("swap", "A", "C")])

    def test_wrong_orientation_restructures(self):
        t = split("right", 0.5, pane("A"), pane("B"))  # side by side, want bottom
        ops = core.plan_stack(t, "B", "stack-bottom")
        self.assertEqual(kinds(ops)[:2], ["move_out", "move_in"])

    def test_two_panes_bottom_in_place(self):
        t = split("down", 0.28, pane("A"), pane("B"))
        self.assertEqual(core.plan_stack(t, "B", "stack-bottom", 0.72), [])
        self.assertEqual(core.plan_stack(t, "A", "stack-bottom", 0.72), [("swap", "A", "B")])

    def test_ratio_ops_always_in_herdr_range(self):
        t = split("right", 0.5, pane("P0"), pane("X"))
        for i in range(1, 14):
            t = split("right", 0.5, pane("P%d" % i), t)
        for o in core.plan_stack(t, "X", "stack-bottom", 0.95):
            if o[0] == "ratio":
                self.assertTrue(0.1 <= o[2] <= 0.9)


if __name__ == "__main__":
    unittest.main()
