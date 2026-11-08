import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bin"))
import smart_focus_core as core  # noqa: E402


def pane(i):
    return {"type": "pane", "pane_id": i}


def split(direction, ratio, a, b):
    return {"type": "split", "direction": direction, "ratio": ratio, "first": a, "second": b}


# A | (B / C)
TREE = split("right", 0.5, pane("A"), split("down", 0.5, pane("B"), pane("C")))


class ComputeOpsTest(unittest.TestCase):
    def test_single_pane_no_ops(self):
        self.assertEqual(core.compute_ops(pane("A"), "A"), [])

    def test_first_child_gets_ratio(self):
        self.assertEqual(core.compute_ops(TREE, "A", 0.7), [([], 0.7)])

    def test_second_child_gets_complement_and_path(self):
        self.assertEqual(core.compute_ops(TREE, "B", 0.7), [([], 0.3), ([True], 0.7)])
        self.assertEqual(core.compute_ops(TREE, "C", 0.7), [([], 0.3), ([True], 0.3)])

    def test_unknown_pane(self):
        self.assertEqual(core.compute_ops(TREE, "Z"), [])

    def test_skips_already_matching(self):
        t = split("right", 0.3, pane("A"), split("down", 0.5, pane("B"), pane("C")))
        self.assertEqual(core.compute_ops(t, "B", 0.7), [([True], 0.7)])

    def test_deep_path(self):
        t = split("right", 0.5, pane("A"),
                  split("down", 0.5, split("right", 0.5, pane("B"), pane("C")), pane("D")))
        self.assertEqual(core.compute_ops(t, "C", 0.7),
                         [([], 0.3), ([True], 0.7), ([True, False], 0.3)])

    def test_siblings_off_path_untouched(self):
        t = split("right", 0.5, split("down", 0.9, pane("A"), pane("B")), pane("C"))
        paths = [p for p, _ in core.compute_ops(t, "C", 0.7, balance_others=False)]
        self.assertEqual(paths, [[]])

    def test_clamp(self):
        self.assertEqual(core.clamp_ratio(0.99), 0.9)
        self.assertEqual(core.clamp_ratio(0.1), 0.5)
        self.assertEqual(core.clamp_ratio("abc"), core.DEFAULT_RATIO)
        self.assertEqual(core.compute_ops(TREE, "A", 5), [([], 0.9)])


COL = split("down", 0.5, pane("B"), split("down", 0.5, pane("C"), pane("D")))
ABCD = split("right", 0.5, pane("A"), COL)


class BalanceTest(unittest.TestCase):
    def test_focus_a_balances_column(self):
        self.assertEqual(core.compute_ops(ABCD, "A", 0.7),
                         [([], 0.7), ([True], 0.3333)])

    def test_focus_c_equal_others(self):
        ops = core.compute_ops(ABCD, "C", 0.7)
        self.assertEqual(ops, [([], 0.3), ([True], 0.15), ([True, True], 0.8235)])

    def test_focus_b(self):
        ops = core.compute_ops(ABCD, "B", 0.7)
        self.assertEqual(ops, [([], 0.3), ([True], 0.7)])  # C/D split already 0.5

    def test_off_path_inside_nonfocused_side(self):
        t = split("right", 0.5, pane("A"), COL)
        t["second"]["ratio"] = 0.7
        t["second"]["second"]["ratio"] = 0.9
        paths = {tuple(p): r for p, r in core.compute_ops(t, "A", 0.7)}
        self.assertEqual(paths[(True,)], 0.3333)
        self.assertEqual(paths[(True, True)], 0.5)

    def test_skip_when_equal(self):
        t = split("right", 0.7, pane("A"), split("down", 1 / 3.0, pane("B"),
                  split("down", 0.5, pane("C"), pane("D"))))
        self.assertEqual(core.compute_ops(t, "A", 0.7), [])

    def test_disabled_keeps_old_behavior(self):
        self.assertEqual(core.compute_ops(ABCD, "A", 0.7, balance_others=False),
                         [([], 0.7)])


def ratios(ops):
    return {tuple(p): r for p, r in ops}


class GroupTest(unittest.TestCase):
    T = split("right", 0.5, pane("A"), split("down", 0.5, pane("B"), split("down", 0.5, pane("C"), pane("D"))))

    def test_focus_c_in_column(self):
        # B=7.5%, C=85%, D=7.5% of the column
        r = ratios(core.compute_ops(self.T, "C", 0.85))
        self.assertEqual(r[()], 0.15)
        self.assertAlmostEqual(r[(True,)], 0.075, places=4)
        self.assertAlmostEqual(r[(True, True)], 0.85 / 0.925, places=4)

    def test_focus_d_in_column(self):
        r = ratios(core.compute_ops(self.T, "D", 0.85))
        self.assertAlmostEqual(r[(True,)], 0.075, places=4)
        self.assertAlmostEqual(r[(True, True)], 0.075 / 0.925, places=4)

    def test_focus_b_in_column(self):
        r = ratios(core.compute_ops(self.T, "B", 0.85))
        self.assertAlmostEqual(r[(True,)], 0.85, places=4)
        self.assertNotIn((True, True), r)  # C/D already 0.5

    def test_four_stacked(self):
        t = split("down", 0.5, pane("A"), split("down", 0.5, pane("B"),
                  split("down", 0.5, pane("C"), pane("D"))))
        r = ratios(core.compute_ops(t, "C", 0.7))
        self.assertEqual(r[()], 0.1)
        self.assertAlmostEqual(r[(True,)], 0.1 / 0.9, places=4)
        self.assertAlmostEqual(r[(True, True)], 0.875, places=4)

    def test_perpendicular_not_grouped(self):
        t = split("right", 0.5, pane("A"), split("down", 0.5, pane("B"), pane("C")))
        self.assertEqual(ratios(core.compute_ops(t, "C", 0.7)), {(): 0.3, (True,): 0.3})


class ShapeTest(unittest.TestCase):
    def test_ratio_independent(self):
        other = split("right", 0.9, pane("A"), split("down", 0.1, pane("B"), pane("C")))
        self.assertEqual(core.shape_signature(TREE), core.shape_signature(other))

    def test_structure_change(self):
        other = split("down", 0.5, pane("A"), split("down", 0.5, pane("B"), pane("C")))
        self.assertNotEqual(core.shape_signature(TREE), core.shape_signature(other))
        self.assertNotEqual(core.shape_signature(TREE), core.shape_signature(pane("A")))


if __name__ == "__main__":
    unittest.main()
