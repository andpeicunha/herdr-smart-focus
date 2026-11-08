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
        paths = [p for p, _ in core.compute_ops(t, "C", 0.7)]
        self.assertEqual(paths, [[]])

    def test_clamp(self):
        self.assertEqual(core.clamp_ratio(0.99), 0.9)
        self.assertEqual(core.clamp_ratio(0.1), 0.5)
        self.assertEqual(core.clamp_ratio("abc"), core.DEFAULT_RATIO)
        self.assertEqual(core.compute_ops(TREE, "A", 5), [([], 0.9)])


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
