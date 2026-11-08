import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bin"))
import smart_focus_core as core  # noqa: E402


def P(pid, base, label=None):
    return {"pane_id": pid, "label": label, "base": base}


class RepoNameTest(unittest.TestCase):
    def test_plain_repo(self):
        self.assertEqual(core.repo_display_name("/a/foo/src", "/a/foo", "/a/foo/.git"), "foo")

    def test_worktree_uses_main_repo(self):
        self.assertEqual(core.repo_display_name(
            "/a/foo/.worktrees/x", "/a/foo/.worktrees/x", "/a/foo/.git"), "foo")

    def test_not_git_uses_cwd_basename(self):
        self.assertEqual(core.repo_display_name("/a/b/plain"), "plain")
        self.assertEqual(core.repo_display_name("/a/b/plain/"), "plain")

    def test_toplevel_without_common_dir(self):
        self.assertEqual(core.repo_display_name("/a/foo/x", "/a/foo", None), "foo")

    def test_root_cwd_fallback(self):
        self.assertEqual(core.repo_display_name("/"), "pane")


class DedupeTest(unittest.TestCase):
    def test_suffixes_in_order(self):
        self.assertEqual(core.dedupe_names(["a", "b", "a", "a", "b"]),
                         ["a", "b", "a #2", "a #3", "b #2"])

    def test_empty(self):
        self.assertEqual(core.dedupe_names([]), [])


class ShouldManageTest(unittest.TestCase):
    def test_unnamed_never_applied(self):
        self.assertTrue(core.should_manage(None, None))
        self.assertTrue(core.should_manage("", None))

    def test_manual_label_never_applied(self):
        self.assertFalse(core.should_manage("mine", None))

    def test_label_still_ours(self):
        self.assertTrue(core.should_manage("foo", "foo"))

    def test_user_renamed_after(self):
        self.assertFalse(core.should_manage("mine", "foo"))
        self.assertFalse(core.should_manage(None, "foo"))


class PlanRenamesTest(unittest.TestCase):
    def test_names_and_duplicates(self):
        r, a = core.plan_renames([P("1", "x"), P("2", "y"), P("3", "x")], {})
        self.assertEqual(r, {"1": "x", "2": "y", "3": "x #2"})
        self.assertEqual(a, r)

    def test_manual_label_untouched(self):
        r, a = core.plan_renames([P("1", "x", "mine"), P("2", "x")], {})
        self.assertEqual(r, {"2": "x"})
        self.assertNotIn("1", a)

    def test_user_renamed_managed_pane_is_released(self):
        r, a = core.plan_renames([P("1", "x", "custom"), P("2", "x", "x #2")],
                                 {"1": "x", "2": "x #2"})
        self.assertNotIn("1", r)
        self.assertNotIn("1", a)
        self.assertEqual(r, {"2": "x"})  # 2 is now first managed -> plain name
        self.assertEqual(a, {"2": "x"})

    def test_idempotent(self):
        r, a = core.plan_renames([P("1", "x", "x"), P("2", "x", "x #2")], {"1": "x", "2": "x #2"})
        self.assertEqual(r, {})
        self.assertEqual(a, {"1": "x", "2": "x #2"})

    def test_other_tab_records_kept_and_prune(self):
        r, a = core.plan_renames([P("1", "x")], {"9": "z"})
        self.assertEqual(a, {"1": "x", "9": "z"})
        self.assertEqual(core.prune_applied(a, {"1"}), {"1": "x"})

    def test_name_for_new(self):
        self.assertEqual(core.name_for_new("x", []), "x")
        self.assertEqual(core.name_for_new("x", ["x"]), "x #2")
        self.assertEqual(core.name_for_new("x", ["x", "x #2", "y"]), "x #3")

    def test_parse_bool(self):
        self.assertTrue(core.parse_bool(None))
        self.assertFalse(core.parse_bool("false"))
        self.assertFalse(core.parse_bool(False))
        self.assertTrue(core.parse_bool("on"))


if __name__ == "__main__":
    unittest.main()
