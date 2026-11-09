import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bin"))
import smart_focus_core as core  # noqa: E402

Y, R, B, G = core.BADGES["working"], core.BADGES["blocked"], core.BADGES["done"], core.BADGES["idle"]


class BadgeForTest(unittest.TestCase):
    def test_mapping_matches_sidebar_colors(self):
        self.assertEqual(core.badge_for("working"), "\U0001F7E1")
        self.assertEqual(core.badge_for("blocked"), "\U0001F534")
        self.assertEqual(core.badge_for("done"), "\U0001F535")
        self.assertEqual(core.badge_for("idle"), "\U0001F7E2")
        self.assertEqual(core.badge_for("unknown"), "⚪")

    def test_no_agent_or_bad_value(self):
        self.assertEqual(core.badge_for(None), "")
        self.assertEqual(core.badge_for("bogus"), "")


class PrefixTest(unittest.TestCase):
    def test_compose_and_strip_roundtrip(self):
        for st in core.BADGES:
            self.assertEqual(core.strip_prefix(core.compose(st, "repo x")), "repo x")

    def test_strip_only_one_and_only_leading(self):
        self.assertEqual(core.strip_prefix(Y + " " + R + " a"), R + " a")
        self.assertEqual(core.strip_prefix("a " + Y), "a " + Y)
        self.assertEqual(core.strip_prefix(None), "")

    def test_compose_without_status(self):
        self.assertEqual(core.compose(None, "repo"), "repo")


class AdoptTest(unittest.TestCase):
    def test_label_matches_applied_keeps_base(self):
        e = {"base": "repo", "applied": Y + " repo"}
        self.assertEqual(core.adopt_base(Y + " repo", e), "repo")

    def test_manual_rename_adopted(self):
        e = {"base": "repo", "applied": Y + " repo"}
        self.assertEqual(core.adopt_base("my work", e), "my work")
        self.assertEqual(core.adopt_base(R + " my work", e), "my work")

    def test_first_time_uses_label_then_fallback(self):
        self.assertEqual(core.adopt_base("manual", None, "repo"), "manual")
        self.assertEqual(core.adopt_base(None, None, "repo"), "repo")
        self.assertIsNone(core.adopt_base("", None))


class PlanBadgeTest(unittest.TestCase):
    def test_first_apply_keeps_manual_text(self):
        entry, rename = core.plan_badge("hotfix", "working", None)
        self.assertEqual(rename, Y + " hotfix")
        self.assertEqual(entry, {"base": "hotfix", "applied": Y + " hotfix"})

    def test_status_change_swaps_prefix(self):
        e = {"base": "hotfix", "applied": Y + " hotfix"}
        entry, rename = core.plan_badge(Y + " hotfix", "blocked", e)
        self.assertEqual(rename, R + " hotfix")
        self.assertEqual(entry["base"], "hotfix")

    def test_already_correct_no_rename(self):
        e = {"base": "hotfix", "applied": Y + " hotfix"}
        entry, rename = core.plan_badge(Y + " hotfix", "working", e)
        self.assertIsNone(rename)
        self.assertEqual(entry, e)

    def test_user_rename_reapplies_prefix(self):
        e = {"base": "hotfix", "applied": Y + " hotfix"}
        entry, rename = core.plan_badge("novo nome", "working", e)
        self.assertEqual(rename, Y + " novo nome")
        self.assertEqual(entry["base"], "novo nome")

    def test_unlabeled_uses_repo_fallback(self):
        entry, rename = core.plan_badge(None, "idle", None, "repo")
        self.assertEqual(rename, G + " repo")

    def test_unlabeled_without_fallback_untouched(self):
        self.assertEqual(core.plan_badge(None, "idle", None), (None, None))

    def test_no_agent_strips_stale_badge(self):
        e = {"base": "sh", "applied": Y + " sh"}
        entry, rename = core.plan_badge(Y + " sh", None, e)
        self.assertEqual(rename, "sh")


class RestoreTest(unittest.TestCase):
    def test_restore_to_base(self):
        e = {"base": "repo", "applied": B + " repo"}
        self.assertEqual(core.plan_restore(B + " repo", e), "repo")

    def test_restore_after_user_edit_strips_prefix(self):
        e = {"base": "repo", "applied": B + " repo"}
        self.assertEqual(core.plan_restore(R + " other", e), "other")

    def test_restore_nothing_to_do(self):
        self.assertIsNone(core.plan_restore("plain", None))
        self.assertIsNone(core.plan_restore(None, {"base": "x", "applied": Y + " x"}))


if __name__ == "__main__":
    unittest.main()
