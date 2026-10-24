"""End-to-end tests of cudl. Stacked features: --from, parents rewritten or finished, sync
--rebase, finish --stack."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # helpers.py, however the tests run
from helpers import CudlTest  # noqa: E402


class TestStacks(CudlTest):
    # --- a helper, not a gatekeeper ---

    def test_new_from_a_dirty_parent_starts_from_its_last_commit(self):
        self.cudl("new", "p", "-r", "alpha")
        (self.w / "wt/p/notes/wip.md").write_text("wip\n")
        p = self.cudl("new", "c", "--from", "p")
        self.assertIn("starts from its last commit", p.stderr)
        self.assertFalse((self.w / "wt/c/notes/wip.md").exists())

    # --- a helper, not a gatekeeper: the code review ---

    def test_auto_take_after_the_parent_is_gone(self):
        self.cudl("new", "p", "-r", "alpha")
        self.work(self.w / "wt/p", "alpha", "p1", "1\n", "parent")
        self.cudl("new", "c", "--from", "p")
        self.cudl("finish", "p")
        ct = self.w / "wt/c"
        (ct / "code/beta/b").write_text("b\n")
        out = self.cudl("commit", "-A", "-m", "beta work", cwd=ct).stdout
        self.assertIn("beta: was pinned; now on branch c (base main)", out)

    def test_new_from_seeds_the_committed_handoff(self):
        self.cudl("new", "p")
        pt = self.w / "wt/p"
        (pt / "handoff/p.md").write_text("# p\n\nState: committed.\n")
        self.cudl("commit", "-m", "handoff", cwd=pt)
        (pt / "handoff/p.md").write_text("# p\n\nState: not yet committed.\n")
        self.cudl("new", "c", "--from", "p")
        seeded = (self.w / "wt/c/handoff/c.md").read_text()
        self.assertIn("State: committed.", seeded)
        self.assertNotIn("not yet committed", seeded)

    # --- pin retention, squash, stacked features ---

    def test_stacked_feature_lifecycle(self):
        self.cudl("new", "p", "-r", "alpha", "--goal", "the parent")
        pt = self.w / "wt/p"
        (pt / "notes/design.md").write_text("the design\n")
        (pt / "handoff/p.md").write_text("# p\n\nGoal: the parent\n\nState: design settled.\n")
        p1 = self.work(pt, "alpha", "p1", "1\n", "parent one")
        self.cudl("new", "c", "--from", "p", "--goal", "the child")
        ct = self.w / "wt/c"
        self.assertEqual((ct / "notes/design.md").read_text(), "the design\n")  # inherits the parent's lab
        self.assertIn("design settled", (ct / "handoff/c.md").read_text())
        self.assertIn("Seeded from handoff/p.md", (ct / "handoff/c.md").read_text())
        ca = ct / "code/alpha"
        self.assertEqual(self.branch(ca), "c")
        self.assertEqual(self.head(ca), p1)
        self.assertEqual(self.git(ca, "config", "branch.c.cudlbase"), "p")
        self.assertIn("stacked on p", self.cudl("ls").stdout)
        self.work(ct, "alpha", "c1", "1\n", "child one")
        # the parent moves on; the child takes it in (code and lab)
        self.work(pt, "alpha", "p2", "2\n", "parent two")
        (pt / "notes/more.md").write_text("more\n")
        self.cudl("commit", "-m", "notes", cwd=pt)
        self.cudl("sync", cwd=ct)
        self.assertTrue((ca / "p2").exists())
        self.assertTrue((ct / "notes/more.md").exists())
        # finish bottom-up only
        p = self.cudl("finish", "c", check=False)
        self.assertIn("finish p first", p.stderr)
        # a rewrite of the parent after the sync is caught
        self.git(pt / "code/alpha", "commit", "-q", "--amend", "-m", "parent two, amended")
        self.cudl("commit", "-m", "rewrote p", cwd=pt)
        before = self.head(ca)
        p = self.cudl("sync", cwd=ct, check=False)
        self.assertIn("p was rewritten", p.stderr)
        self.assertIn("rebase --onto p", p.stderr)
        self.assertEqual(self.head(ca), before)
        # take the advice
        last = self.git(ca, "config", "branch.c.cudlparenttip")
        self.git(ca, "rebase", "-q", "--onto", "p", last, "c")
        self.git(ca, "config", "branch.c.cudlparenttip", self.git(ca, "rev-parse", "p"))
        self.cudl("commit", "-m", "child rebased", cwd=ct)
        self.cudl("sync", cwd=ct)
        # finish the parent, then the child follows the integration branch
        self.cudl("finish", "p", "--keep")  # kept worktree: still counts as finished
        self.assertIn("p is finished", self.cudl("sync", cwd=ct).stdout)
        self.assertEqual(self.git(ca, "config", "branch.c.cudlbase"), "dev")
        self.assertNotIn("stacked on", self.cudl("ls").stdout)
        self.cudl("finish", "c")
        main_alpha = self.w / "code/alpha"
        self.assertTrue((main_alpha / "c1").exists() and (main_alpha / "p2").exists())
        self.assertEqual(self.branch(main_alpha), "dev")
        self.gc()
        self.cudl("keep", "--check")

    def test_stacked_on_abandoned_parent(self):
        self.cudl("new", "p", "-r", "alpha")
        self.work(self.w / "wt/p", "alpha", "p1", "1\n", "parent one")
        self.cudl("new", "c", "--from", "p")
        self.cudl("finish", "p", "--abandon")
        ct = self.w / "wt/c"
        p = self.cudl("sync", cwd=ct, check=False)
        self.assertIn("was abandoned", p.stderr)
        self.cudl("sync", "--reparent", cwd=ct)
        self.assertEqual(self.git(ct / "code/alpha", "config", "branch.c.cudlbase"), "dev")

    # rewritten parents: sync --rebase, precise parent state, finish --stack

    def test_sync_rebase_after_the_parent_is_squashed(self):
        self.cudl("new", "p", "-r", "alpha")
        pt, ct = self.w / "wt/p", self.w / "wt/c"
        self.work(pt, "alpha", "p1", "1\n", "parent one")
        self.cudl("new", "c", "--from", "p")
        self.work(ct, "alpha", "c1", "1\n", "child one")
        self.work(pt, "alpha", "p2", "2\n", "parent two")
        self.cudl("sync", cwd=ct)  # a sync merge in the child's history
        c2 = self.work(ct, "alpha", "c2", "2\n", "child two")
        self.assertIn("c is stacked on p", self.cudl("squash", cwd=pt).stdout)
        ca = ct / "code/alpha"
        p = self.cudl("sync", cwd=ct, check=False)
        self.assertIn("cudl sync -f c --rebase", p.stderr)
        self.assertEqual(self.head(ca), c2)
        self.assertIn("rebased c's own commits onto p", self.cudl("sync", "--rebase", cwd=ct).stdout)
        self.assertEqual(self.git(ca, "log", "--format=%s", "p..c").splitlines(), ["child two", "child one"])
        self.assertEqual(self.git(ca, "rev-list", "--merges", "p..c"), "")
        self.assertEqual(self.git(ca, "config", "branch.c.cudlparenttip"), self.git(ca, "rev-parse", "p"))
        self.assertEqual(self.pin(ct, "code/alpha"), self.head(ca))
        self.assertTrue(self.clean(ct))
        self.assertIn(c2, self.keep_refs())
        self.gc()
        self.cudl("keep", "--check")
        self.cudl("finish", "--stack", "c")
        self.assertEqual(sorted(self.git(self.w / "code/alpha", "ls-tree", "--name-only", "dev").split()),
                         ["README", "c1", "c2", "p1", "p2"])

    def test_sync_rebase_refusals_touch_nothing(self):
        self.cudl("new", "p", "-r", "alpha")
        pt, ct = self.w / "wt/p", self.w / "wt/c"
        p1 = self.work(pt, "alpha", "p1", "1\n", "parent one")
        self.work(pt, "alpha", "p2", "2\n", "parent two")
        self.cudl("new", "c", "--from", "p")
        c1 = self.work(ct, "alpha", "c1", "1\n", "child one")
        ca = ct / "code/alpha"
        fork = self.root / "fork.git"
        self.git(self.root, "init", "-q", "--bare", str(fork))
        self.git(ca, "remote", "add", "fork", str(fork))
        self.git(ca, "push", "-q", "fork", "c")
        self.git(ca, "fetch", "-q", "fork")
        self.cudl("squash", "--force", cwd=pt)  # c's push published p's commits too
        p = self.cudl("sync", "--rebase", cwd=ct, check=False)
        self.assertIn("is published (fork/c)", p.stderr)
        self.assertEqual(self.head(ca), c1)
        # a merge of something else in the child's own range: not cudl's to linearise away
        side = self.git(ca, "commit-tree", "dev^{tree}", "-p", "dev", "-m", "side")
        self.git(ca, "merge", "-q", "--no-edit", side)
        merged = self.head(ca)
        p = self.cudl("sync", "--rebase", "--force", cwd=ct, check=False)
        self.assertIn("a rebase would drop", p.stderr)
        self.assertEqual(self.head(ca), merged)
        self.assertEqual(self.pin(ct, "code/alpha"), c1)  # a refusal writes nothing, not even the pin
        # back on one of the parent's dropped commits: neither what it took nor the new parent
        self.git(ca, "reset", "-q", "--hard", p1)
        p = self.cudl("sync", "--rebase", "--force", cwd=ct, check=False)
        self.assertIn("doesn't follow from what it took", p.stderr)
        self.assertEqual(self.head(ca), p1)
        # reset to the integration branch instead: nothing of the parent's, nothing dropped held: it just follows p
        self.git(ca, "reset", "-q", "--hard", "dev")
        self.cudl("sync", cwd=ct)
        self.assertEqual(self.head(ca), self.git(ca, "rev-parse", "p"))

    def test_sync_rebase_stops_on_a_conflict_and_sync_picks_it_up(self):
        self.cudl("new", "p", "-r", "alpha")
        pt, ct = self.w / "wt/p", self.w / "wt/c"
        self.work(pt, "alpha", "f", "1\n", "parent one")
        self.cudl("new", "c", "--from", "p")
        self.work(ct, "alpha", "f", "child\n", "child one")
        ca = ct / "code/alpha"
        last = self.git(ca, "config", "branch.c.cudlparenttip")
        (pt / "code/alpha/f").write_text("parent, amended\n")
        self.git(pt / "code/alpha", "commit", "-q", "-a", "--amend", "--no-edit")
        p = self.cudl("sync", "--rebase", cwd=ct, check=False)
        self.assertIn("stopped", p.stderr)
        self.assertIn("rebase --continue", p.stderr)
        self.assertEqual(self.git(ca, "config", "branch.c.cudlparenttip"), last)
        p = self.cudl("sync", cwd=ct, check=False)
        self.assertIn("a rebase in progress", p.stderr)
        (ca / "f").write_text("resolved\n")
        self.git(ca, "add", "f")
        self.sh("git", "rebase", "--continue", cwd=ca, env={"GIT_EDITOR": "true"})
        out = self.cudl("sync", cwd=ct).stdout
        self.assertIn("recorded the pins of alpha", out)
        self.assertEqual(self.git(ca, "config", "branch.c.cudlparenttip"), self.git(ca, "rev-parse", "p"))
        self.assertEqual(self.pin(ct, "code/alpha"), self.head(ca))
        self.assertTrue(self.clean(ct))

    def test_parent_squashed_then_finished(self):
        self.cudl("new", "p", "-r", "alpha")
        pt, ct = self.w / "wt/p", self.w / "wt/c"
        self.work(pt, "alpha", "p1", "1\n", "parent one")
        self.work(pt, "alpha", "p2", "2\n", "parent two")
        self.cudl("new", "c", "--from", "p")
        self.work(ct, "alpha", "c1", "1\n", "child one")
        self.cudl("squash", cwd=pt)
        out = self.cudl("finish", "p").stdout
        self.assertIn("with --rebase", out)
        ca = ct / "code/alpha"
        p = self.cudl("sync", cwd=ct, check=False)  # the check happens although p's branch is gone
        self.assertIn("cudl sync -f c --rebase", p.stderr)
        self.assertEqual(self.git(ca, "config", "branch.c.cudlbase"), "p")
        self.assertIn("p is finished", self.cudl("sync", "--rebase", cwd=ct).stdout)
        self.assertEqual(self.git(ca, "config", "branch.c.cudlbase"), "dev")
        self.assertEqual(self.git(ca, "log", "--format=%s", "dev..c").splitlines(), ["child one"])
        self.cudl("finish", "c")
        self.gc()
        self.cudl("keep", "--check")

    def test_parent_state_is_precise(self):
        self.cudl("new", "p", "-r", "alpha")
        self.work(self.w / "wt/p", "alpha", "p1", "1\n", "parent one")
        self.cudl("new", "p2", "-r", "beta")
        self.work(self.w / "wt/p2", "beta", "q1", "1\n", "other one")
        self.cudl("new", "c", "--from", "p")
        ct = self.w / "wt/c"
        self.cudl("finish", "p2")  # its finish commit's subject starts with "cudl: finish p"
        self.assertNotIn("is finished", self.cudl("sync", cwd=ct).stdout)
        self.assertEqual(self.git(ct / "code/alpha", "config", "branch.c.cudlbase"), "p")
        self.cudl("finish", "p")
        self.cudl("new", "p", "-r", "alpha")  # the name again, a different feature
        self.assertIn("p is finished", self.cudl("sync", cwd=ct).stdout)
        self.assertEqual(self.git(ct / "code/alpha", "config", "branch.c.cudlbase"), "dev")
        # a child of an abandoned feature stays with it, whatever comes next under that name
        self.cudl("new", "q", "-r", "alpha")
        self.cudl("new", "d", "--from", "q")
        self.cudl("finish", "q", "--abandon")
        self.cudl("new", "q", "-r", "alpha")
        p = self.cudl("sync", cwd=self.w / "wt/d", check=False)
        self.assertIn("was abandoned", p.stderr)

    def test_finish_stack(self):
        self.stack("a", "b", "c")
        at, bt = self.w / "wt/a", self.w / "wt/b"
        self.work(at, "alpha", "a2", "2\n", "a two")  # b hasn't taken this: a sync between the levels
        (bt / "notes/b.md").write_text("b's notes\n")
        self.cudl("commit", "-m", "notes", cwd=bt)
        out = self.cudl("finish", "--stack", "c").stdout
        self.assertIn("finishing a → b → c", out)
        main_alpha = self.w / "code/alpha"
        self.assertEqual(sorted(self.git(main_alpha, "ls-tree", "--name-only", "dev").split()),
                         ["README", "a1", "a2", "b1", "c1"])
        self.assertEqual(self.branch(main_alpha), "dev")
        self.assertTrue((self.w / "notes/b.md").exists())
        for f in "abc":
            self.assertFalse((self.w / "wt" / f).exists())
            self.assertEqual(self.sh("git", "rev-parse", "-q", "--verify", f"refs/heads/{f}", cwd=main_alpha,
                                     check=False).stdout, "")
            self.assertEqual(self.sh("git", "rev-parse", "-q", "--verify", f"refs/heads/feat/{f}", cwd=self.w,
                                     check=False).stdout, "")
        subjects = self.git(self.w, "log", "--format=%s", "--first-parent", "main").splitlines()
        self.assertEqual(subjects[:3], ["cudl: finish c", "cudl: finish b", "cudl: finish a"])
        self.assertFalse((self.w / ".git/cudl-finish.json").exists())
        self.assertTrue(self.clean(self.w))
        self.gc()
        self.cudl("keep", "--check")

    def test_finish_stack_preflight_touches_nothing(self):
        self.stack("a", "b", "c")
        main_alpha = self.w / "code/alpha"
        dev = self.git(main_alpha, "rev-parse", "dev")
        self.work(self.w / "wt/a", "alpha", "a2", "2\n", "a two")
        self.cudl("squash", cwd=self.w / "wt/a")  # b took a1, which a no longer has
        (self.w / "wt/c/code/alpha/c1").write_text("dirty\n")
        p = self.cudl("finish", "--stack", "c", check=False)
        self.assertIn("nothing finished", p.stderr)
        self.assertIn("b: alpha: b took", p.stderr)
        self.assertIn("c: alpha has uncommitted changes", p.stderr)
        self.assertEqual(self.git(main_alpha, "rev-parse", "dev"), dev)
        self.assertTrue((self.w / "wt/a").is_dir())
        self.assertFalse((self.w / ".git/cudl-finish.json").exists())

    def test_finish_stack_resumes_after_a_conflict(self):
        self.stack("a", "b")
        at, bt = self.w / "wt/a", self.w / "wt/b"
        (at / "notes/x.md").write_text("from a\n")
        self.cudl("commit", "-m", "a's x", cwd=at)
        (bt / "notes/x.md").write_text("from b\n")
        self.cudl("commit", "-m", "b's x", cwd=bt)
        p = self.cudl("finish", "--stack", "b", check=False)
        self.assertIn("notes/x.md", p.stderr)
        self.assertIn("rerun `cudl finish --stack b`", p.stderr)
        self.assertFalse(at.exists())  # a is finished
        self.assertTrue((self.w / ".git/cudl-finish.json").exists())
        self.assertIn("stopped before it was done", self.cudl("ls").stdout)
        p = self.cudl("finish", "b", check=False)
        self.assertIn("rerun it", p.stderr)
        (bt / "notes/x.md").write_text("from both\n")
        self.cudl("-f", "b", "commit", "-m", "x from both")
        self.cudl("finish", "--stack", "b")
        self.assertEqual((self.w / "notes/x.md").read_text(), "from both\n")
        self.assertFalse(bt.exists())
        self.assertFalse((self.w / ".git/cudl-finish.json").exists())

    def test_sync_rebase_refuses_a_merge_with_a_resolution(self):
        self.cudl("new", "p", "-r", "alpha")
        pt, ct = self.w / "wt/p", self.w / "wt/c"
        self.work(pt, "alpha", "f", "0\n", "parent one")
        self.cudl("new", "c", "--from", "p")
        self.work(ct, "alpha", "f", "1\n", "child one")
        self.work(pt, "alpha", "f", "2\n", "parent two")
        p = self.cudl("sync", cwd=ct, check=False)
        self.assertIn("merge conflict", p.stderr)
        ca = ct / "code/alpha"
        (ca / "f").write_text("3\n")
        self.git(ca, "add", "f")
        self.cudl("commit", "-m", "resolved", cwd=ct)
        self.cudl("sync", cwd=ct)
        self.cudl("squash", cwd=pt)
        p = self.cudl("sync", "--rebase", cwd=ct, check=False)
        self.assertIn("a rebase would drop", p.stderr)
        self.assertEqual((ca / "f").read_text(), "3\n")

    def test_reused_names_keep_terminal_marks(self):
        self.cudl("new", "p", "-r", "alpha")
        self.cudl("new", "c", "--from", "p")
        self.cudl("finish", "p", "--abandon")
        self.cudl("new", "p", "-r", "alpha")
        self.work(self.w / "wt/p", "alpha", "n1", "1\n", "new p")
        self.cudl("finish", "p")
        p = self.cudl("sync", cwd=self.w / "wt/c", check=False)
        self.assertIn("was abandoned", p.stderr)  # the new p's finish isn't c's parent's

    def test_take_after_the_parent_was_rewritten_and_finished(self):
        self.cudl("new", "p", "-r", "alpha")
        pt, ct = self.w / "wt/p", self.w / "wt/c"
        self.work(pt, "alpha", "p1", "1\n", "parent one")
        old = self.work(pt, "alpha", "p2", "2\n", "parent two")
        self.cudl("new", "c", "--from", "p", "-r", "beta")  # alpha pinned at p's old tip
        self.cudl("squash", cwd=pt)
        self.cudl("finish", "p")
        self.cudl("take", "alpha", cwd=ct)
        ca = ct / "code/alpha"
        self.assertEqual(self.git(ca, "config", "branch.c.cudlparenttip"), old)
        self.work(ct, "alpha", "c1", "1\n", "child one")
        p = self.cudl("sync", cwd=ct, check=False)
        self.assertIn("cudl sync -f c --rebase", p.stderr)
        self.cudl("sync", "--rebase", cwd=ct)
        self.assertEqual(self.git(ca, "log", "--format=%s", "dev..c").splitlines(), ["child one"])
        self.assertEqual(self.git(ca, "config", "branch.c.cudlbase"), "dev")

    def test_take_keeps_the_parent_boundary_under_a_detached_commit(self):
        self.cudl("new", "p", "-r", "alpha")
        pt, ct = self.w / "wt/p", self.w / "wt/c"
        p1 = self.work(pt, "alpha", "p1", "1\n", "parent one")
        self.cudl("new", "c", "--from", "p", "-r", "beta")
        ca = ct / "code/alpha"
        (ca / "u").write_text("u\n")
        self.git(ca, "add", "u")
        self.git(ca, "commit", "-q", "-m", "U")  # on the detached pin
        self.cudl("take", "alpha", cwd=ct)
        self.assertEqual(self.git(ca, "config", "branch.c.cudlparenttip"), p1)
        self.cudl("commit", "-m", "took alpha", cwd=ct)
        self.work(pt, "alpha", "p2", "2\n", "parent two")
        self.cudl("squash", cwd=pt)
        self.cudl("sync", "--rebase", cwd=ct)
        self.assertEqual(self.git(ca, "log", "--format=%s", "p..c").splitlines(), ["U"])

    def test_sync_rebase_sees_a_published_child_with_nothing_of_its_own(self):
        self.cudl("new", "p", "-r", "alpha")
        pt, ct = self.w / "wt/p", self.w / "wt/c"
        self.work(pt, "alpha", "p1", "1\n", "parent one")
        self.work(pt, "alpha", "p2", "2\n", "parent two")
        self.cudl("new", "c", "--from", "p")
        ca = ct / "code/alpha"
        fork = self.root / "fork.git"
        self.git(self.root, "init", "-q", "--bare", str(fork))
        self.git(ca, "remote", "add", "fork", str(fork))
        self.git(ca, "push", "-q", "fork", "c")
        self.git(ca, "fetch", "-q", "fork")
        self.cudl("squash", "--force", cwd=pt)
        p = self.cudl("sync", "--rebase", cwd=ct, check=False)
        self.assertIn("is published (fork/c)", p.stderr)
        out = self.cudl("sync", "--rebase", "--force", cwd=ct).stdout
        self.assertIn("push --force-with-lease", out)
        self.assertEqual(self.head(ca), self.git(ca, "rev-parse", "p"))

    def test_rebase_target_survives_the_parent_moving_on(self):
        self.cudl("new", "p", "-r", "alpha")
        pt, ct = self.w / "wt/p", self.w / "wt/c"
        self.work(pt, "alpha", "f", "1\n", "parent one")
        self.cudl("new", "c", "--from", "p")
        self.work(ct, "alpha", "f", "child\n", "child one")
        (pt / "code/alpha/f").write_text("parent, amended\n")
        self.git(pt / "code/alpha", "commit", "-q", "-a", "--amend", "--no-edit")
        self.cudl("commit", "-m", "amended", cwd=pt)
        p = self.cudl("sync", "--rebase", cwd=ct, check=False)
        self.assertIn("stopped", p.stderr)
        self.work(pt, "alpha", "p2", "2\n", "parent two")  # the parent moves on meanwhile
        ca = ct / "code/alpha"
        (ca / "f").write_text("resolved\n")
        self.git(ca, "add", "f")
        self.sh("git", "rebase", "--continue", cwd=ca, env={"GIT_EDITOR": "true"})
        self.cudl("sync", cwd=ct)
        self.assertTrue((ca / "p2").exists())
        self.assertEqual(self.git(ca, "config", "branch.c.cudlparenttip"), self.git(ca, "rev-parse", "p"))
        self.assertEqual(self.sh("git", "config", "branch.c.cudlrebaseonto", cwd=ca, check=False).stdout, "")

    def test_child_follows_where_the_parent_was_merged(self):
        self.git(self.w / "code/alpha", "branch", "release", "dev")
        self.cudl("new", "p", "-r", "alpha", "--base", "alpha=release")
        self.work(self.w / "wt/p", "alpha", "p1", "1\n", "parent one")
        self.cudl("new", "c", "--from", "p")
        ct = self.w / "wt/c"
        self.work(ct, "alpha", "c1", "1\n", "child one")
        self.git(self.w / "code/alpha", "switch", "-q", "release")
        self.cudl("finish", "p")
        self.assertIn("follows release", self.cudl("sync", cwd=ct).stdout)
        self.assertEqual(self.git(ct / "code/alpha", "config", "branch.c.cudlbase"), "release")
        self.cudl("finish", "c")
        self.assertTrue((self.w / "code/alpha/c1").exists())

    def test_finish_stack_over_a_kept_parent(self):
        self.stack("a", "b")
        self.cudl("finish", "a", "--keep")
        self.assertIn("finished, kept", self.cudl("ls").stdout)
        p = self.cudl("new", "x", "--from", "a", check=False)
        self.assertIn("no live feature a", p.stderr)
        self.cudl("finish", "--stack", "b")
        self.assertTrue((self.w / "code/alpha/b1").exists())
        self.assertFalse((self.w / "wt/b").exists())

    # rewritten parents: the code review

    def test_sync_rebase_refuses_a_result_that_differs(self):
        # a child commit that becomes empty on the new parent, then its revert, would undo the parent
        self.cudl("new", "p", "-r", "alpha")
        pt, ct = self.w / "wt/p", self.w / "wt/c"
        self.work(pt, "alpha", "base", "0\n", "parent zero")
        self.cudl("new", "c", "--from", "p")
        self.work(ct, "alpha", "x", "1\n", "child sets x")
        (ct / "code/alpha/x").unlink()
        self.cudl("commit", "-a", "-m", "child reverts x", cwd=ct)
        self.work(pt, "alpha", "x", "1\n", "parent sets x")
        self.work(pt, "alpha", "y", "1\n", "parent adds y")
        self.cudl("sync", cwd=ct)  # a clean merge: x from the parent
        ca = ct / "code/alpha"
        before = self.head(ca)
        self.assertTrue((ca / "x").exists())
        self.cudl("squash", cwd=pt)
        p = self.cudl("sync", "--rebase", cwd=ct, check=False)
        self.assertIn("gives a different result", p.stderr)
        self.assertEqual(self.head(ca), before)
        self.assertTrue((ca / "x").exists())
        self.assertEqual(self.sh("git", "config", "branch.c.cudlrebaseonto", cwd=ca, check=False).stdout, "")

    def test_take_after_recording_a_detached_commit(self):
        self.cudl("new", "p", "-r", "alpha")
        pt, ct = self.w / "wt/p", self.w / "wt/c"
        p1 = self.work(pt, "alpha", "p1", "1\n", "parent one")
        self.cudl("new", "c", "--from", "p", "-r", "beta")
        ca = ct / "code/alpha"
        (ca / "u").write_text("u\n")
        self.git(ca, "add", "u")
        self.git(ca, "commit", "-q", "-m", "U")
        self.cudl("commit", "-m", "record U", cwd=ct)  # the lab now pins U, still detached
        self.cudl("take", "alpha", cwd=ct)
        self.assertEqual(self.git(ca, "config", "branch.c.cudlparenttip"), p1)
        self.assertIn("in sync", self.cudl("sync", cwd=ct).stdout)  # no rewrite seen where there was none

    def test_two_repo_rebase_stopping_in_the_second(self):
        self.cudl("new", "p", "-r", "alpha,beta")
        pt, ct = self.w / "wt/p", self.w / "wt/c"
        (pt / "code/alpha/a").write_text("1\n")
        (pt / "code/beta/f").write_text("1\n")
        self.cudl("commit", "-A", "-m", "parent one", cwd=pt)
        self.cudl("new", "c", "--from", "p")
        (ct / "code/alpha/c").write_text("1\n")
        (ct / "code/beta/f").write_text("child\n")
        self.cudl("commit", "-A", "-m", "child one", cwd=ct)
        for repo, name in (("alpha", "a"), ("beta", "f")):
            (pt / "code" / repo / name).write_text("amended\n")
            self.git(pt / "code" / repo, "commit", "-q", "-a", "--amend", "--no-edit")
        self.cudl("commit", "-m", "amended", cwd=pt)
        p = self.cudl("sync", "--rebase", cwd=ct, check=False)
        self.assertIn("stopped in code/beta", p.stderr)
        (pt / "code/alpha/a2").write_text("2\n")
        (pt / "code/beta/b2").write_text("2\n")
        self.cudl("commit", "-A", "-m", "parent moves on", cwd=pt)
        cb = ct / "code/beta"
        (cb / "f").write_text("resolved\n")
        self.git(cb, "add", "f")
        self.sh("git", "rebase", "--continue", cwd=cb, env={"GIT_EDITOR": "true"})
        self.cudl("sync", cwd=ct)
        for repo in ("alpha", "beta"):
            d = ct / "code" / repo
            self.assertEqual(self.git(d, "config", "branch.c.cudlparenttip"), self.git(d, "rev-parse", "p"))
        self.assertTrue((ct / "code/alpha/a2").exists() and (cb / "b2").exists())

    def test_hand_rebase_then_the_parent_moves_on(self):
        self.cudl("new", "p", "-r", "alpha")
        pt, ct = self.w / "wt/p", self.w / "wt/c"
        self.work(pt, "alpha", "p1", "1\n", "parent one")
        self.work(pt, "alpha", "p2", "2\n", "parent two")
        self.cudl("new", "c", "--from", "p")
        self.work(ct, "alpha", "c1", "1\n", "child one")
        ca = ct / "code/alpha"
        last = self.git(ca, "config", "branch.c.cudlparenttip")
        self.cudl("squash", cwd=pt)
        self.git(ca, "rebase", "-q", "--onto", "p", last, "c")  # the advice, by hand
        self.work(pt, "alpha", "p3", "3\n", "parent three")  # before any cudl sync
        self.cudl("sync", cwd=ct)
        self.assertTrue((ca / "p3").exists())
        self.assertEqual(self.git(ca, "config", "branch.c.cudlparenttip"), self.git(ca, "rev-parse", "p"))

    def test_squash_a_child_of_a_parent_finished_elsewhere(self):
        self.git(self.w / "code/alpha", "branch", "release", "dev")
        self.cudl("new", "p", "-r", "alpha", "--base", "alpha=release")
        self.work(self.w / "wt/p", "alpha", "p1", "1\n", "parent one")
        self.cudl("new", "c", "--from", "p")
        ct = self.w / "wt/c"
        self.work(ct, "alpha", "c1", "1\n", "child one")
        self.work(ct, "alpha", "c2", "2\n", "child two")
        self.git(self.w / "code/alpha", "switch", "-q", "release")
        self.cudl("finish", "p")
        self.cudl("squash", cwd=ct)
        self.assertEqual(self.git(ct / "code/alpha", "rev-list", "--count", "release..c"), "1")

    def test_finish_stack_after_the_integration_branch_moved(self):
        self.stack("a", "b")
        main_alpha = self.w / "code/alpha"
        (main_alpha / "hotfix").write_text("1\n")
        self.git(main_alpha, "add", "hotfix")
        self.git(main_alpha, "commit", "-q", "-m", "hotfix on dev")
        self.cudl("commit", "-m", "pin the hotfix")
        self.cudl("finish", "--stack", "b")
        self.assertEqual(sorted(self.git(main_alpha, "ls-tree", "--name-only", "dev").split()),
                         ["README", "a1", "b1", "hotfix"])

    def test_legacy_child_of_a_finished_parent_ignores_a_namesake(self):
        self.cudl("new", "p", "-r", "alpha")
        self.work(self.w / "wt/p", "alpha", "p1", "1\n", "parent one")
        self.cudl("new", "c", "--from", "p")
        self.cudl("finish", "p")
        for key in ("cudlparentid", "cudlparentdone"):  # as a lab made by an older cudl would have it
            self.git(self.w, "config", "--unset", f"branch.feat/c.{key}")
        self.cudl("new", "p", "-r", "alpha")
        self.assertIn("p is finished", self.cudl("sync", cwd=self.w / "wt/c").stdout)

    # rewritten parents: the review's second pass

    def test_sync_rebase_that_cant_be_checked_changes_nothing(self):
        # the parent took over one of the child's commits: the replay is fine, the net merge conflicts
        self.cudl("new", "p", "-r", "alpha")
        pt, ct = self.w / "wt/p", self.w / "wt/c"
        self.work(pt, "alpha", "f", "a\n", "parent: f=a")
        self.cudl("new", "c", "--from", "p")
        cb = self.work(ct, "alpha", "f", "b\n", "child: f=b")
        cc = self.work(ct, "alpha", "f", "c\n", "child: f=c")
        self.git(pt / "code/alpha", "cherry-pick", cb)
        self.work(pt, "alpha", "g", "1\n", "parent: g")
        self.cudl("squash", cwd=pt)
        p = self.cudl("sync", "--rebase", cwd=ct, check=False)
        self.assertIn("can't be checked", p.stderr)
        self.assertEqual(self.head(ct / "code/alpha"), cc)
        self.assertEqual(self.sh("git", "config", "branch.c.cudlrebaseonto", cwd=ct / "code/alpha", check=False).stdout, "")

    def test_sync_rebase_refuses_a_worktree_off_its_branch(self):
        self.cudl("new", "p", "-r", "alpha")
        pt, ct = self.w / "wt/p", self.w / "wt/c"
        self.work(pt, "alpha", "p1", "1\n", "parent one")
        self.work(pt, "alpha", "p2", "2\n", "parent two")
        self.cudl("new", "c", "--from", "p")
        self.work(ct, "alpha", "c1", "1\n", "child one")
        self.cudl("squash", cwd=pt)
        ca = ct / "code/alpha"
        self.git(ca, "checkout", "-q", "--detach")
        p = self.cudl("sync", "--rebase", cwd=ct, check=False)
        self.assertIn("not c: switch back", p.stderr)
        self.assertIsNone(self.branch(ca))

    def test_take_after_a_sync_kept_the_childs_pin(self):
        self.cudl("new", "p", "-r", "alpha")
        pt, ct = self.w / "wt/p", self.w / "wt/c"
        p1 = self.work(pt, "alpha", "p1", "1\n", "parent one")
        self.cudl("new", "c", "--from", "p", "-r", "beta")
        ca = ct / "code/alpha"
        (ca / "u").write_text("u\n")
        self.git(ca, "add", "u")
        self.git(ca, "commit", "-q", "-m", "U")
        self.cudl("commit", "-m", "record U", cwd=ct)
        self.work(pt, "alpha", "p2", "2\n", "parent two")
        self.cudl("sync", cwd=ct)  # the gitlink conflict keeps the child's U
        self.assertTrue((ca / "u").exists())
        self.cudl("squash", cwd=pt)
        self.cudl("take", "alpha", cwd=ct)
        self.assertEqual(self.git(ca, "config", "branch.c.cudlparenttip"), p1)
        self.cudl("commit", "-m", "took alpha", cwd=ct)
        self.cudl("sync", "--rebase", cwd=ct)
        self.assertEqual(self.git(ca, "log", "--format=%s", "p..c").splitlines(), ["U"])

    def test_retained_legacy_parent_is_still_live(self):
        self.stack("a", "b")
        for key in ("branch.feat/a.cudlid", "branch.feat/b.cudlid", "branch.feat/b.cudlparentid"):
            self.git(self.w, "config", "--unset", key)  # as an older cudl made it
        flag = self.root / "fail"
        self.fail_commits(self.w, flag)
        self.cudl("finish", "--stack", "b", check=False)  # a's lab commit fails
        flag.unlink()
        at = self.w / "wt/a"
        (at / "notes/late.md").write_text("late\n")
        self.cudl("commit", "-m", "late note", cwd=at)
        p = self.cudl("finish", "--stack", "b", check=False)
        self.assertIn("it changed since, so it stays", p.stderr)
        self.assertNotIn("completed", self.cudl("ls").stdout)  # b is still stacked on a live a
        self.cudl("finish", "--stack", "b")
        self.assertTrue((self.w / "notes/late.md").exists())
        self.assertFalse(at.exists() or (self.w / "wt/b").exists())

    # rewritten parents: the review's third pass

    def test_a_namesakes_finish_leaves_a_legacy_child_alone(self):
        self.cudl("new", "p", "-r", "alpha")
        self.work(self.w / "wt/p", "alpha", "p1", "1\n", "parent one")
        self.cudl("new", "c", "--from", "p")
        self.cudl("finish", "p")
        old = self.git(self.w, "config", "branch.feat/c.cudlparentdone")
        for key in ("cudlparentid", "cudlparentdone"):  # as an older cudl made it
            self.git(self.w, "config", "--unset", f"branch.feat/c.{key}")
        self.cudl("new", "p", "-r", "alpha")
        self.work(self.w / "wt/p", "alpha", "n1", "1\n", "the namesake")
        self.cudl("finish", "p")
        self.assertEqual(self.git(self.w, "config", "branch.feat/c.cudlparentdone"), old)

    def test_rebase_verdicts_come_before_any_write(self):
        self.cudl("new", "p", "-r", "alpha,beta")
        pt, ct = self.w / "wt/p", self.w / "wt/c"
        (pt / "code/alpha/a").write_text("1\n")
        (pt / "code/beta/base").write_text("0\n")
        self.cudl("commit", "-A", "-m", "parent one", cwd=pt)
        self.cudl("new", "c", "--from", "p")
        self.work(ct, "alpha", "c1", "1\n", "child: alpha")  # rebases fine
        self.work(ct, "beta", "x", "1\n", "child sets x")  # beta: the empty-then-revert case
        (ct / "code/beta/x").unlink()
        self.cudl("commit", "-a", "-m", "child reverts x", cwd=ct)
        self.work(pt, "alpha", "a2", "2\n", "parent: alpha two")
        self.work(pt, "beta", "x", "1\n", "parent sets x")
        self.work(pt, "beta", "y", "1\n", "parent adds y")
        self.cudl("sync", cwd=ct)
        heads = {r: self.head(ct / "code" / r) for r in ("alpha", "beta")}
        lab = self.head(ct)
        self.cudl("squash", cwd=pt)
        hook = Path(self.git(self.w / "code/alpha", "rev-parse", "--path-format=absolute", "--git-path", "hooks"))
        hook.mkdir(exist_ok=True)
        marker = self.root / "hook-ran"
        (hook / "post-checkout").write_text(f"#!/bin/sh\ntouch '{marker}'\n")
        (hook / "post-checkout").chmod(0o755)
        p = self.cudl("sync", "--rebase", cwd=ct, check=False)
        self.assertIn("in beta", p.stderr)
        self.assertEqual({r: self.head(ct / "code" / r) for r in ("alpha", "beta")}, heads)
        self.assertEqual(self.head(ct), lab)
        self.assertFalse(marker.exists())  # the rehearsals ran no hooks

    # --- the live checks' fixes ---

    def test_finish_stack_commits_memory_first(self):
        self.stack("p", "c")
        (self.w / "memory/MEMORY.md").write_text("- one\n")
        self.cudl("commit", "-m", "memory")
        (self.w / "memory/MEMORY.md").write_text("- one\n- two\n")
        self.cudl("finish", "--stack", "c")
        subjects = self.git(self.w, "log", "--first-parent", "--format=%s").splitlines()
        self.assertEqual(subjects[:3], ["cudl: finish c", "cudl: finish p",
                                        "lab: memory, plans and session logs before finishing p"])
        self.assertTrue(self.clean(self.w))

    # --- what the session design review found ---

    def test_finish_stack_refuses_before_merging_anything(self):
        t = "f5f50000-0000-4000-8000-000000000005"
        self.cudl("new", "a", "-r", "alpha")
        self.work(self.w / "wt/a", "alpha", "a", "a\n", "a")
        self.cudl("new", "b", "--from", "a")
        b = self.w / "wt/b"
        self.start(t, top=b, CLAUDE_CODE_SESSION_ID=t)
        self.cudl("commit", "-f", "b", "-m", "its log", env={"CLAUDE_CODE_SESSION_ID": t})
        head = self.head(self.w)
        p = self.cudl("finish", "--stack", "b", check=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("f5f50000", p.stderr)
        self.assertEqual(self.head(self.w), head)
        self.assertTrue((self.w / "wt/a").is_dir())

    # --- a feature's base is a local branch ---

    def test_finish_stack_checks_every_level_s_base_before_merging(self):
        """A stacked child whose own base is a remote-tracking branch stops the stack before its parent
        is finished, not halfway through (the code review: the preflight kept one base per repo)."""
        self.stack("a", "b")
        alpha = self.w / "code/alpha"
        self.git(self.w / "wt/b/code/alpha", "config", "branch.b.cudlbase", "origin/dev")  # as an older cudl recorded it
        dev = self.git(alpha, "rev-parse", "dev")
        p = self.cudl("finish", "--stack", "b", check=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("nothing finished", p.stderr)
        self.assertIn("b: alpha's base: origin/dev is a remote-tracking branch", p.stderr)
        self.assertEqual(self.git(alpha, "rev-parse", "dev"), dev)
        self.assertTrue((self.w / "wt/a").is_dir())

    # --- lab commits made again (re-signed) ---

    def remake(self, top, onto, *extra):
        """Make top's lab commits since onto again, as a re-signing does: same trees, authors and
        messages, a new committer date, so new hashes (merges made again too)."""
        self.sh("git", "rebase", "-q", "--force-rebase", "--rebase-merges", *extra, onto, cwd=top,
                env={"GIT_COMMITTER_DATE": "2030-01-01T00:00:00+0000"})

    REWORD_A = 'test "$(git log -1 --format=%s)" != "p a" || git commit -q --amend -m "p a, reworded"'

    def parent_with_notes(self, *names):
        """Feature p (alpha) with a code commit and then one lab commit per name ("p <name>")."""
        self.cudl("new", "p", "-r", "alpha")
        pt = self.w / "wt/p"
        self.work(pt, "alpha", "p1", "1\n", "p one")
        for n in names:
            (pt / f"notes/{n}.md").write_text(f"{n}\n")
            self.cudl("commit", "-m", f"p {n}", cwd=pt)
        return pt

    def test_sync_after_the_parent_lab_was_remade(self):
        """The parent's lab commits re-signed under the child (the labs' feedback, 10-17 and 10-22):
        sync puts the child's own lab commits on the new copies, its merge kept with its tree,
        instead of a merge that keeps the old ones."""
        self.stack("p", "c")
        pt, ct = self.w / "wt/p", self.w / "wt/c"
        (pt / "notes/p.md").write_text("p\n")
        self.cudl("commit", "-m", "p note", cwd=pt)
        self.cudl("sync", cwd=ct)  # a lab merge of feat/p in the child
        merge = self.git(self.w, "rev-list", "--merges", "-n1", "feat/c")
        self.assertEqual(self.git(self.w, "rev-parse", f"{merge}^2"), self.git(self.w, "rev-parse", "feat/p"))
        tree = self.git(self.w, "rev-parse", f"{merge}^{{tree}}")
        (ct / "notes/c.md").write_text("c\n")
        self.cudl("commit", "-m", "c note", cwd=ct)
        old = set(self.git(self.w, "rev-list", "feat/p", "--not", "main").split())
        self.remake(pt, "main")
        self.assertFalse(old & set(self.git(self.w, "rev-list", "feat/p").split()))
        self.assertIn("made again", self.cudl("sync", cwd=ct).stdout)
        self.assertFalse(old & set(self.git(self.w, "rev-list", "feat/c").split()))
        self.sh("git", "merge-base", "--is-ancestor", "feat/p", "feat/c")
        merges = self.git(self.w, "rev-list", "--merges", "feat/p..feat/c").split()
        self.assertEqual(len(merges), 1)
        self.assertEqual(self.git(self.w, "rev-parse", f"{merges[0]}^2"), self.git(self.w, "rev-parse", "feat/p"))
        self.assertEqual(self.git(self.w, "rev-parse", f"{merges[0]}^{{tree}}"), tree)
        own = self.git(self.w, "log", "--format=%s", "feat/p..feat/c").splitlines()
        self.assertIn("c note", own)
        self.assertNotIn("p note", own)
        self.assertEqual((ct / "notes/p.md").read_text(), "p\n")
        self.assertTrue(self.clean(ct))
        self.cudl("finish", "--stack", "c")
        self.assertEqual(self.git(self.w, "log", "--format=%s", "main").splitlines().count("p note"), 1)

    def test_sync_rebase_after_mains_lab_was_remade(self):
        """main's lab commits re-signed under an unstacked feature: the reported `sync --rebase` swaps
        them for the new copies, and the feature's commits that it makes again are signed when
        commits are."""
        base = self.git(self.w, "rev-parse", "main")
        (self.w / "notes/m.md").write_text("m\n")
        self.cudl("commit", "-m", "main note")
        self.cudl("new", "f", "-r", "alpha")
        ft = self.w / "wt/f"
        self.work(ft, "alpha", "f1", "1\n", "f one")
        old = set(self.git(self.w, "rev-list", "main", "--not", base).split())
        self.remake(self.w, base)
        self.assertIn("made again", self.cudl("sync", "--rebase", cwd=ft, env=self.fake_signer()).stdout)
        self.assertFalse(old & set(self.git(self.w, "rev-list", "feat/f").split()))
        self.sh("git", "merge-base", "--is-ancestor", "main", "feat/f")
        own = self.git(self.w, "rev-list", "main..feat/f").split()
        self.assertTrue(own)
        for c in own:
            self.assertIn("gpgsig", self.git(self.w, "cat-file", "-p", c))
        self.assertTrue(self.clean(ft))

    def test_sync_after_the_parent_lab_was_remade_and_reworded(self):
        """A parent re-signed with one of its commits reworded under a re-made one: what's under the
        re-made commit goes with it, so the child is on the new copies all the same."""
        pt = self.parent_with_notes("a", "b")
        ct = self.w / "wt/c"
        self.cudl("new", "c", "--from", "p")
        self.work(ct, "alpha", "c1", "1\n", "c one")
        old = set(self.git(self.w, "rev-list", "feat/p", "--not", "main").split())
        self.remake(pt, "main", "-x", self.REWORD_A)
        self.cudl("sync", cwd=ct)
        self.assertFalse(old & set(self.git(self.w, "rev-list", "feat/c").split()))
        self.sh("git", "merge-base", "--is-ancestor", "feat/p", "feat/c")
        self.assertIn("p a, reworded", self.git(self.w, "log", "--format=%s", "feat/c").splitlines())
        self.assertTrue(self.clean(ct))

    def test_sync_refuses_old_copies_a_reworded_commit_keeps(self):
        """The child started on a parent commit that the re-signing reworded, and merged a later one:
        its start still reaches old copies through the reworded commit, so nothing changes."""
        pt = self.parent_with_notes("a")
        ct = self.w / "wt/c"
        self.cudl("new", "c", "--from", "p")  # on "p a"
        self.work(ct, "alpha", "c1", "1\n", "c one")
        (pt / "notes/b.md").write_text("b\n")
        self.cudl("commit", "-m", "p b", cwd=pt)
        self.cudl("sync", cwd=ct)
        self.remake(pt, "main", "-x", self.REWORD_A)
        tip = self.git(self.w, "rev-parse", "feat/c")
        p = self.cudl("sync", cwd=ct, check=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("(p a)", p.stderr)
        self.assertEqual(self.git(self.w, "rev-parse", "feat/c"), tip)
        self.assertTrue(self.clean(ct))

    def test_finish_refuses_a_feature_holding_old_copies_of_mains_lab(self):
        """A finish would give main both copies of its re-signed commits: refused until a sync."""
        base = self.git(self.w, "rev-parse", "main")
        (self.w / "notes/m.md").write_text("m\n")
        self.cudl("commit", "-m", "main note")
        self.cudl("new", "f", "-r", "alpha")
        ft = self.w / "wt/f"
        self.work(ft, "alpha", "f1", "1\n", "f one")
        self.remake(self.w, base)
        p = self.cudl("finish", "f", check=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("cudl sync -f f", p.stderr)
        self.assertTrue(ft.is_dir())
        self.cudl("sync", cwd=ft)
        self.cudl("finish", "f")
        self.assertEqual(self.git(self.w, "log", "--format=%s", "main").splitlines().count("main note"), 1)

    def test_sync_refuses_to_remake_a_published_lab_branch(self):
        """A child's lab branch on a remote: making its commits again needs --force, which says what
        still has the old ones."""
        self.stack("p", "c")
        pt, ct = self.w / "wt/p", self.w / "wt/c"
        bare = self.root / "lab-backup.git"
        self.sh("git", "init", "-q", "--bare", str(bare))
        self.git(self.w, "remote", "add", "backup", str(bare))
        self.git(self.w, "push", "-q", "backup", "feat/c")
        self.remake(pt, "main")
        tip = self.git(self.w, "rev-parse", "feat/c")
        p = self.cudl("sync", cwd=ct, check=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("backup/feat/c", p.stderr)
        self.assertIn("--force", p.stderr)
        self.assertEqual(self.git(self.w, "rev-parse", "feat/c"), tip)
        self.assertIn("backup/feat/c", self.cudl("sync", "--force", cwd=ct).stdout)
        self.sh("git", "merge-base", "--is-ancestor", "feat/p", "feat/c")

    def test_take_after_the_parents_lab_was_remade(self):
        """take before the sync that would swap the copies: the boundary is still the parent's pin
        (Codex's plan review: the lab merge base went back to before the parent's work)."""
        self.cudl("new", "p", "-r", "alpha")
        pt, ct = self.w / "wt/p", self.w / "wt/c"
        self.work(pt, "alpha", "p1", "1\n", "p one")
        p2 = self.work(pt, "alpha", "p2", "2\n", "p two")
        self.cudl("new", "c", "--from", "p", "-r", "beta")  # alpha pinned at p2
        self.remake(pt, "main")
        self.cudl("take", "alpha", cwd=ct)
        self.assertEqual(self.git(ct / "code/alpha", "config", "branch.c.cudlparenttip"), p2)

    def test_sync_after_main_remade_a_parents_finish(self):
        """main re-signed after the parent's finish: the finish recorded on the child is found by its
        new copy, so the child follows main (Codex's plan review)."""
        self.stack("p", "c")
        ct = self.w / "wt/c"
        before = self.git(self.w, "rev-parse", "main")
        old = set(self.git(self.w, "rev-list", "feat/p", "--not", "main").split())
        self.cudl("finish", "p")
        done = self.git(self.w, "config", "branch.feat/c.cudlparentdone").split()[1]
        self.remake(self.w, before)
        self.assertNotIn(done, self.git(self.w, "rev-list", "main").split())
        self.assertIn("p is finished", self.cudl("sync", cwd=ct).stdout)
        self.assertFalse(old & set(self.git(self.w, "rev-list", "feat/c").split()))
        self.sh("git", "merge-base", "--is-ancestor", "main", "feat/c")

    def test_a_signing_failure_halfway_leaves_the_lab_branch(self):
        """A signer that fails on its second signature: the lab branch doesn't move, and a rerun with
        a working one goes through."""
        self.stack("p", "c")
        pt, ct = self.w / "wt/p", self.w / "wt/c"
        self.remake(pt, "main")
        tip = self.git(self.w, "rev-parse", "feat/c")
        env = self.fake_signer()
        counter, once = self.root / "signed", self.root / "sign-once"
        once.write_text(f"#!/bin/sh\nn=$(cat '{counter}' 2>/dev/null || echo 0)\necho $((n + 1)) > '{counter}'\n"
                        f"[ \"$n\" -ge 1 ] && {{ echo 'no agent' >&2; exit 1; }}\nexec '{self.root / 'signer'}' \"$@\"\n")
        once.chmod(0o755)
        key = next(k for k, v in env.items() if v == "gpg.ssh.program")
        env[key.replace("KEY", "VALUE")] = str(once)
        p = self.cudl("sync", cwd=ct, env=env, check=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertEqual(counter.read_text().strip(), "2")
        self.assertEqual(self.git(self.w, "rev-parse", "feat/c"), tip)
        self.assertTrue(self.clean(ct))
        self.assertIn("made again", self.cudl("sync", cwd=ct, env=self.fake_signer()).stdout)
        self.sh("git", "merge-base", "--is-ancestor", "feat/p", "feat/c")

    # --- lab commits made again: the code review (Codex) ---

    def test_sync_merges_the_parent_it_checked(self):
        """The parent's lab is made again while the child's sync runs (between its checks and its lab
        merge): the child merges what it checked, and its next sync swaps the copies (Codex's code
        review: a merge of the parent's newer tip left the child holding both)."""
        self.stack("p", "c")
        pt, ct = self.w / "wt/p", self.w / "wt/c"
        self.work(pt, "alpha", "p2", "2\n", "p two")  # so the child's sync merges code
        old = set(self.git(self.w, "rev-list", "feat/p", "--not", "main").split())
        hooks = Path(self.git(ct / "code/alpha", "rev-parse", "--path-format=absolute", "--git-common-dir")) / "hooks"
        hooks.mkdir(exist_ok=True)
        flag = self.root / "remade-once"
        (hooks / "post-merge").write_text(
            f"#!/bin/sh\n[ -e '{flag}' ] && exit 0\ntouch '{flag}'\n"
            f"env -u GIT_DIR -u GIT_WORK_TREE -u GIT_INDEX_FILE GIT_COMMITTER_DATE=2030-01-01T00:00:00+0000 "
            f"git -C '{pt}' rebase -q --force-rebase --rebase-merges main\n")
        (hooks / "post-merge").chmod(0o755)
        self.cudl("sync", cwd=ct)
        self.assertTrue(flag.exists())
        self.assertFalse(old & set(self.git(self.w, "rev-list", "feat/p").split()))
        self.assertIn("made again", self.cudl("sync", cwd=ct).stdout)
        self.assertFalse(old & set(self.git(self.w, "rev-list", "feat/c").split()))

    def test_sync_finds_copies_with_signatures_shown(self):
        """log.showSignature on: the copies are still found (Codex's code review)."""
        self.stack("p", "c")
        pt, ct = self.w / "wt/p", self.w / "wt/c"
        self.git(self.w, "config", "log.showSignature", "true")
        old = set(self.git(self.w, "rev-list", "feat/p", "--not", "main").split())
        signer = self.fake_signer()
        self.sh("git", "rebase", "-q", "--force-rebase", "--rebase-merges", "-S", "main", cwd=pt,
                env=dict(signer, GIT_COMMITTER_DATE="2030-01-01T00:00:00+0000"))
        self.assertIn("made again", self.cudl("sync", cwd=ct, env=signer).stdout)
        self.assertFalse(old & set(self.git(self.w, "rev-list", "feat/c").split()))

    def test_take_after_a_finish_without_a_lab_merge(self):
        """The parent's lab merged by hand before its finish: the finish commit has one parent, and
        take still works (Codex's code review: a search from `<finish>^2` failed)."""
        self.cudl("new", "p", "-r", "alpha")
        pt, ct = self.w / "wt/p", self.w / "wt/c"
        p1 = self.work(pt, "alpha", "p1", "1\n", "p one")
        self.cudl("new", "c", "--from", "p", "-r", "beta")
        self.git(self.w, "merge", "-q", "--no-edit", "feat/p")
        self.cudl("finish", "p")
        done = self.git(self.w, "config", "branch.feat/c.cudlparentdone").split()[1]
        self.assertEqual(len(self.git(self.w, "rev-list", "--parents", "-n1", done).split()), 2)
        self.cudl("take", "alpha", cwd=ct)
        self.assertEqual(self.branch(ct / "code/alpha"), "c")
        self.assertEqual(self.head(ct / "code/alpha"), p1)

    def test_finish_stack_checks_the_lab_copies_first(self):
        """A stack whose child can't go onto its parent's new copies stops before the parent is
        finished (Codex's code review)."""
        pt = self.parent_with_notes("a")
        ct = self.w / "wt/c"
        self.cudl("new", "c", "--from", "p")
        self.work(ct, "alpha", "c1", "1\n", "c one")
        (pt / "notes/b.md").write_text("b\n")
        self.cudl("commit", "-m", "p b", cwd=pt)
        self.cudl("sync", cwd=ct)
        self.remake(pt, "main", "-x", self.REWORD_A)
        main = self.git(self.w, "rev-parse", "main")
        p = self.cudl("finish", "--stack", "c", check=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("nothing finished", p.stderr)
        self.assertIn("(p a)", p.stderr)
        self.assertTrue(pt.is_dir())
        self.assertEqual(self.git(self.w, "rev-parse", "main"), main)

    def test_sync_refuses_to_move_a_published_lab_branch_off_an_old_copy(self):
        """The child's lab branch on a remote at a commit of the parent that was made again: moving it
        onto the new copies needs --force too (Codex's code review)."""
        self.stack("p", "c")
        pt, ct = self.w / "wt/p", self.w / "wt/c"
        bare = self.root / "lab-backup.git"
        self.sh("git", "init", "-q", "--bare", str(bare))
        self.git(self.w, "remote", "add", "backup", str(bare))
        self.git(self.w, "push", "-q", "backup", "feat/p:refs/heads/feat/c")  # the child's remote branch, at p's tip
        self.remake(pt, "main")
        tip = self.git(self.w, "rev-parse", "feat/c")
        p = self.cudl("sync", cwd=ct, check=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("backup/feat/c", p.stderr)
        self.assertEqual(self.git(self.w, "rev-parse", "feat/c"), tip)

    def test_remade_commits_keep_utf8_messages(self):
        """A commit encoding set for the sync doesn't relabel the UTF-8 messages it makes again
        (Codex's code review)."""
        self.stack("p", "c")
        pt, ct = self.w / "wt/p", self.w / "wt/c"
        (ct / "notes/c.md").write_text("c\n")
        self.cudl("commit", "-m", "c note é", cwd=ct)
        self.remake(pt, "main")
        self.cudl("sync", cwd=ct, env={"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "i18n.commitEncoding",
                                       "GIT_CONFIG_VALUE_0": "ISO-8859-1"})
        c = self.git(self.w, "log", "-1", "--format=%H", "--grep", "c note", "feat/c")
        self.assertNotIn("encoding", self.git(self.w, "cat-file", "-p", c).split("\n\n")[0])
        self.assertEqual(self.git(self.w, "log", "-1", "--format=%s", c), "c note é")

    def test_sync_refuses_old_commits_a_flattened_merge_keeps(self):
        """The parent re-signed by a plain rebase, which flattens its merge of main: its commits from
        before that merge get new trees, so the child's start still sits on old ones that no copy
        replaces: refused, nothing changed (Fable's code review)."""
        pt = self.parent_with_notes("a")
        ct = self.w / "wt/c"
        self.cudl("new", "c", "--from", "p")  # on "p a"
        self.work(ct, "alpha", "c1", "1\n", "c one")
        (self.w / "notes/m.md").write_text("m\n")
        self.cudl("commit", "-m", "main note")
        self.cudl("sync", cwd=pt)  # p merges main
        (pt / "notes/b.md").write_text("b\n")
        self.cudl("commit", "-m", "p b", cwd=pt)
        self.cudl("sync", cwd=ct)
        self.sh("git", "rebase", "-q", "--force-rebase", "main", cwd=pt,
                env={"GIT_COMMITTER_DATE": "2030-01-01T00:00:00+0000"})
        tip = self.git(self.w, "rev-parse", "feat/c")
        p = self.cudl("sync", cwd=ct, check=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("(p a)", p.stderr)
        self.assertEqual(self.git(self.w, "rev-parse", "feat/c"), tip)
        self.assertTrue(self.clean(ct))

if __name__ == "__main__":
    unittest.main()
