import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bin"))
import smart_focus_core as core  # noqa: E402


class SubscriptionsTest(unittest.TestCase):
    def test_main_has_no_per_pane_types(self):
        types = [s["type"] for s in core.main_subscriptions()]
        self.assertEqual(types, ["pane.focused", "pane.created", "pane.closed",
                                 "layout.updated", "tab.focused"])
        self.assertTrue(all(set(s) == {"type"} for s in core.main_subscriptions()))

    def test_status_one_per_pane_with_pane_id(self):
        subs = core.status_subscriptions(["b", "a", "a", ""])
        self.assertEqual(subs, [{"type": "pane.agent_status_changed", "pane_id": "a"},
                                {"type": "pane.agent_status_changed", "pane_id": "b"}])

    def test_status_empty(self):
        self.assertEqual(core.status_subscriptions([]), [])


if __name__ == "__main__":
    unittest.main()
