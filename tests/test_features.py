"""End-to-end tests of cudl. Working in a feature: new, take, commit at both levels, squash,
sync, open, push, status and ls."""

import json
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # helpers.py, however the tests run
from helpers import CUDL, CudlTest  # noqa: E402


class TestFeatures(CudlTest):
    def test_new_feature_layout(self):
        self.cudl("new", "f1", "-r", "alpha", "--goal", "try it")
        f = self.w / "wt/f1"
        self.assertEqual(self.branch(f), "feat/f1")
        self.assertEqual(self.branch(f / "code/alpha"), "f1")
        self.assertIsNone(self.branch(f / "code/beta"))  # pinned
        self.assertEqual(self.head(f / "code/beta"), self.pin(self.w, "code/beta"))
        self.assertTrue(self.clean(f))
        self.assertIn("try it", (f / "handoff/f1.md").read_text())
        local = json.loads((f / ".claude/settings.local.json").read_text())
        self.assertEqual(local["autoMemoryDirectory"], str(self.w / "memory"))
        self.assertEqual(local["plansDirectory"], "plans")
        # one object store: the feature's code worktree belongs to the main submodule's repository
        common = self.git(f / "code/alpha", "rev-parse", "--path-format=absolute", "--git-common-dir")
        self.assertEqual(Path(common), self.w / ".git/modules/alpha")

    def test_commit_both_levels(self):
        self.cudl("new", "f1", "-r", "alpha")
        f = self.w / "wt/f1"
        (f / "code/alpha/README").write_text("changed\n")
        (f / "notes/n.md").write_text("why\n")
        out = self.cudl("commit", "-a", "-m", "alpha=alpha: change", "-m", "lab=notes: why", cwd=f / "code/alpha").stdout
        self.assertIn("alpha:", out)
        self.assertEqual(self.git(f / "code/alpha", "log", "-1", "--format=%s"), "alpha: change")
        self.assertEqual(self.git(f, "log", "-1", "--format=%s"), "notes: why")
        self.assertIn(f"Code: alpha f1 {self.head(f / 'code/alpha')[:10]}", self.git(f, "log", "-1", "--format=%B"))
        self.assertEqual(self.pin(f, "code/alpha"), self.head(f / "code/alpha"))
        self.assertTrue(self.clean(f))
        # the main lab and its checkout did not move
        self.assertEqual(self.branch(self.w / "code/alpha"), "dev")
        self.assertNotEqual(self.head(self.w / "code/alpha"), self.head(f / "code/alpha"))
        self.assertTrue(self.clean(self.w))

    def test_commit_takes_a_pinned_repo_with_work_in_it(self):
        self.cudl("new", "f1", "-r", "alpha")
        f = self.w / "wt/f1"
        (f / "code/alpha/README").write_text("a\n")
        (f / "code/beta/README").write_text("b\n")
        out = self.cudl("commit", "-a", "-m", "both", cwd=f).stdout
        self.assertIn("beta: was pinned; now on branch f1", out)
        self.assertEqual(self.branch(f / "code/beta"), "f1")
        self.assertEqual(self.git(f / "code/beta", "log", "-1", "--format=%s"), "both")
        self.assertTrue(self.clean(f))
        # a repo on some other branch is still refused (finish recognises work by branch name)
        self.git(f / "code/alpha", "switch", "-q", "-c", "elsewhere")
        (f / "code/alpha/README").write_text("c\n")
        p = self.cudl("commit", "-a", "-m", "x", cwd=f, check=False)
        self.assertIn("alpha is on elsewhere, expected f1", p.stderr)

    def test_memory_committed_on_main_from_feature(self):
        self.cudl("new", "f1")
        f = self.w / "wt/f1"
        (self.w / "memory/lesson.md").write_text("---\nname: lesson\n---\nx\n")
        (f / "notes/n.md").write_text("n\n")
        out = self.cudl("commit", "-m", "notes", cwd=f).stdout
        self.assertIn("memory: committed on main", out)
        self.assertEqual(self.git(self.w, "log", "-1", "--format=%s"), "memory: from f1")
        self.assertTrue(self.clean(self.w))

    def test_sync_moves_pinned_repos(self):
        self.cudl("new", "f1", "-r", "alpha")
        self.cudl("new", "f2", "-r", "beta")
        (self.w / "wt/f2/code/beta/x").write_text("x\n")
        self.cudl("commit", "-A", "-m", "beta x", cwd=self.w / "wt/f2")
        self.cudl("finish", "f2")
        f1 = self.w / "wt/f1"
        self.cudl("sync", cwd=f1)
        self.assertEqual(self.head(f1 / "code/beta"), self.pin(self.w, "code/beta"))
        self.assertIsNone(self.branch(f1 / "code/beta"))
        self.assertTrue(self.clean(f1))

    def test_open_agent(self):
        self.cudl("new", "f1")
        clear = 'env -u CLAUDE_CODE_SESSION_ID -u CODEX_THREAD_ID -u CUDL_SESSION_ID "${SHELL:-/bin/sh}" -c'
        self.assertTrue(self.cudl("open", "f1", "--agent", "codex", "--print").stdout.strip().endswith(f"&& {clear} codex"))
        self.assertTrue(self.cudl("open", "f1", "--print").stdout.strip().endswith(f"&& {clear} claude"))
        # a command of several parts runs whole under the cleared environment
        line = self.cudl("open", "f1", "--print", "-c", "echo $CUDL_SESSION_ID-; echo $CLAUDE_CODE_SESSION_ID-").stdout
        out = self.sh("sh", "-c", line, env={"CUDL_SESSION_ID": "x", "CLAUDE_CODE_SESSION_ID": "y"}).stdout
        self.assertEqual(out, "-\n-\n")

    # --- a helper, not a gatekeeper ---

    def test_push_a_feature_to_a_fork(self):
        self.cudl("new", "f1", "-r", "alpha")
        self.work(self.w / "wt/f1", "alpha", "x", "x\n", "x")
        fork = self.root / "fork.git"
        self.git(self.root, "init", "-q", "--bare", str(fork))
        self.git(self.w / "code/alpha", "remote", "add", "fork", str(fork))
        out = self.cudl("push", "--feature", "f1", "--remote", "fork").stdout
        self.assertIn("push --no-follow-tags fork f1", out)
        self.assertIn("dry run", out)
        self.cudl("push", "--feature", "f1", "--remote", "fork", "--yes")
        self.assertEqual(self.git(fork, "rev-parse", "f1"), self.head(self.w / "wt/f1/code/alpha"))
        self.assertEqual(self.git(fork, "for-each-ref", "refs/cudl"), "")  # keep refs stay home

    def test_signing_failures_are_explained(self):
        (self.w / "notes/n.md").write_text("n\n")
        env = {"GIT_CONFIG_COUNT": "3", "GIT_CONFIG_KEY_0": "commit.gpgsign", "GIT_CONFIG_VALUE_0": "true",
               "GIT_CONFIG_KEY_1": "gpg.format", "GIT_CONFIG_VALUE_1": "ssh",
               "GIT_CONFIG_KEY_2": "user.signingkey", "GIT_CONFIG_VALUE_2": str(self.root / "nokey.pub"),
               "SSH_AUTH_SOCK": ""}
        p = self.cudl("commit", "-m", "n", env=env, check=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("commit signing is on (ssh", p.stderr)
        self.assertIn("SSH_AUTH_SOCK is not set", p.stderr)

    # --- a helper, not a gatekeeper: the code review ---

    def test_commit_lab_paths_leaves_other_staged_files_alone(self):
        (self.w / "notes/a.md").write_text("a\n")
        (self.w / "notes/b.md").write_text("b\n")
        self.git(self.w, "add", "notes/b.md")  # someone else's staged work
        self.cudl("commit", "--lab", "notes/a.md", "-m", "only a")
        self.assertEqual(self.git(self.w, "show", "--name-only", "--format=", "HEAD").split(), ["notes/a.md"])
        self.assertIn("A  notes/b.md", self.git(self.w, "status", "--porcelain"))

    def test_commit_retry_records_pins_of_earlier_code_commits(self):
        self.cudl("new", "f1", "-r", "alpha")
        f = self.w / "wt/f1"
        (f / "code/alpha/x").write_text("x\n")
        self.git(f / "code/alpha", "add", "-A")
        self.git(f / "code/alpha", "commit", "-q", "-m", "code committed, lab not")
        (f / "notes/n.md").write_text("n\n")
        self.cudl("commit", "--lab", "notes/n.md", "-m", "retry", cwd=f)
        self.assertEqual(self.pin(f, "code/alpha"), self.head(f / "code/alpha"))

    def test_push_an_older_feature_after_adding_a_repo(self):
        self.cudl("new", "f1", "-r", "alpha")
        self.cudl("add", "gamma", str(self.upstream("gamma", "main")))
        out = self.cudl("push", "--feature", "f1", "--remote", "origin").stdout  # gamma isn't in f1: no crash
        self.assertIn("alpha", out)
        self.assertIn("push --no-follow-tags origin f1", out)
        self.assertNotIn("gamma", out)

    # --- a helper, not a gatekeeper: the review's second pass ---

    def test_commit_lab_leaves_a_hand_staged_pin(self):
        b = self.w / "code/beta"
        (b / "y").write_text("y\n")
        self.git(b, "add", "-A")
        self.git(b, "commit", "-q", "-m", "y1")
        staged = self.head(b)
        self.git(self.w, "add", "code/beta")  # the pin staged by hand at y1
        (b / "y").write_text("y2\n")
        self.git(b, "commit", "-q", "-am", "y2")  # the checkout moves on
        (self.w / "notes/n.md").write_text("n\n")
        self.cudl("commit", "--lab", "notes/n.md", "-m", "note only")
        self.assertIn(staged, self.git(self.w, "diff", "--cached", "--raw", "--no-abbrev", "--", "code/beta"))
        self.assertNotEqual(self.pin(self.w, "code/beta"), self.head(b))

    # --- pin retention, squash, stacked features ---

    def test_squash(self):
        self.cudl("new", "f1", "-r", "alpha")
        f = self.w / "wt/f1"
        for i in range(3):
            self.work(f, "alpha", f"x{i}", f"{i}\n", f"step {i}")
        p = self.cudl("squash", cwd=self.w, check=False)
        self.assertIn("squash works in a feature", p.stderr)
        self.cudl("squash", cwd=f)
        a = f / "code/alpha"
        self.assertEqual(self.git(a, "rev-list", "--count", "dev..f1"), "1")
        body = self.git(a, "log", "-1", "--format=%B", "f1")
        self.assertTrue(body.startswith("step 0"))
        self.assertIn("- step 2", body)
        self.assertEqual(sorted(self.git(a, "ls-tree", "--name-only", "f1").split()), ["README", "x0", "x1", "x2"])
        self.assertEqual(self.pin(f, "code/alpha"), self.head(a))
        self.assertTrue(self.clean(f))
        self.gc()
        self.cudl("keep", "--check")
        self.assertIn("nothing to squash", self.cudl("squash", cwd=f).stdout)

    def test_squash_refuses_published_and_dirty(self):
        self.cudl("new", "f1", "-r", "alpha")
        f = self.w / "wt/f1"
        self.work(f, "alpha", "x0", "0\n", "step 0")
        self.work(f, "alpha", "x1", "1\n", "step 1")
        a = f / "code/alpha"
        # published on a remote with no upstream configured, like the pilot's fork
        fork = self.root / "fork.git"
        self.git(self.root, "init", "-q", "--bare", str(fork))
        self.git(a, "remote", "add", "fork", str(fork))
        self.git(a, "push", "-q", "fork", "f1")
        self.git(a, "fetch", "-q", "fork")
        p = self.cudl("squash", cwd=f, check=False)
        self.assertIn("already on fork/f1", p.stderr)
        self.assertEqual(self.git(a, "rev-list", "--count", "dev..f1"), "2")
        (a / "x1").write_text("dirty\n")
        p = self.cudl("squash", "--force", cwd=f, check=False)
        self.assertIn("uncommitted changes", p.stderr)

    def test_open_starts_an_independent_session(self):
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        log = self.root / "tmux-args"
        (bin_dir / "tmux").write_text(f"#!/bin/sh\nprintf '%s\\n' \"$@\" > {log}\n")
        (bin_dir / "tmux").chmod(0o755)
        self.cudl("new", "f1")
        self.cudl("open", "f1", env={"TMUX": "x", "PATH": f"{bin_dir}:{os.environ['PATH']}",
                                     "CLAUDE_CODE_SESSION_ID": "parent-id"})
        args = log.read_text().split("\n")
        self.assertIn("CLAUDE_CODE_SESSION_ID=", args)
        self.assertIn("CODEX_THREAD_ID=", args)

    def test_ls_and_status(self):
        self.cudl("new", "f1", "-r", "alpha")
        out = self.cudl("ls").stdout
        self.assertIn("f1", out)
        self.assertIn("pinned", out)
        out = self.cudl("status", cwd=self.w / "wt/f1").stdout
        self.assertIn("alpha: f1, +0/-0 vs dev", out)

    # --- the live checks' fixes ---

    def test_feature_option_after_the_command(self):
        self.cudl("new", "f1", "-r", "alpha")
        f = self.w / "wt/f1"
        (f / "code/alpha/x").write_text("x\n")
        self.cudl("commit", "-A", "-f", "f1", "-m", "x")  # from the main lab
        self.assertEqual(self.git(f / "code/alpha", "log", "-1", "--format=%s"), "x")
        self.assertTrue(self.clean(f))
        self.assertIn("[lab branch feat/f1]", self.cudl("status", "-f", "f1").stdout)
        self.cudl("sync", "-f", "f1")
        self.cudl("-f", "f1", "sync", "-f", "f1")  # the same feature both ways is fine
        p = self.cudl("-f", "main", "status", "-f", "f1", check=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("-f main and -f f1", p.stderr)
        # no abbreviations: the permission rules match text, and `--forc` must not pass for --force
        p = self.cudl("sync", "-f", "f1", "--rebase", "--forc", check=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("--forc", p.stderr)
        # cudl's own hints teach the form the rules allow
        self.assertNotRegex(CUDL.read_text(), r"cudl -f [<{]")

    # --- review 2 of the live-check fixes ---

    def test_open_print_uses_your_shell(self):
        self.cudl("new", "f1")
        line = self.cudl("open", "f1", "--print", "-c", "source /dev/null && echo sourced").stdout
        self.assertEqual(self.sh("sh", "-c", line, env={"SHELL": "/bin/bash"}).stdout, "sourced\n")

    # --- review 3 of the live-check fixes ---

    def test_commit_with_lab_paths_doesnt_link_a_running_log(self):
        self.session("session-start", "b0b0b0b0-1", self.w)  # running in main, its log untracked
        (self.w / "notes/n.md").write_text("n\n")
        self.cudl("commit", "--lab", "notes/n.md", "-m", "n")
        self.assertNotIn("b0b0b0b0", self.sh("git", "show", "HEAD:journal/INDEX.md").stdout)
        self.assertIn("?? journal/sessions/", self.sh("git", "status", "--porcelain").stdout)

    # --- review 4 of the live-check fixes ---

    def test_commit_lab_journal_after_a_rename_and_a_deletion(self):
        for sid in ("de1e7e00-1", "de1e7e01-2"):
            self.session("session-start", sid, self.w)
            self.session("session-end", sid, self.w)
        sessions = self.w / "journal/sessions"
        gone = next(sessions.glob("*-de1e7e00.md"))
        moved = next(sessions.glob("*-de1e7e01.md"))
        self.git(self.w, "rm", "-q", str(gone))
        self.git(self.w, "mv", str(moved), str(sessions / "2026-01-01-main-de1e7e01.md"))
        self.cudl("commit", "--lab", "journal/sessions", "-m", "tidy the journal")
        index = self.sh("git", "show", "HEAD:journal/INDEX.md").stdout
        self.assertNotIn(gone.name, index)
        self.assertNotIn(moved.name, index)
        self.assertIn("2026-01-01-main-de1e7e01.md", index)

    # --- worktree exits, notices, the memory index ---

    def test_commit_lab_records_a_removal_git_rm_made(self):
        sid = "f5f50000-1"
        self.session("session-start", sid, self.w)
        self.session("session-end", sid, self.w)
        rel = str(next((self.w / "journal/sessions").glob("*-f5f50000*.md")).relative_to(self.w))
        self.git(self.w, "rm", "-q", rel)
        self.cudl("commit", "--lab", rel, "-m", "journal: drop an empty log")
        self.assertNotIn(rel, self.git(self.w, "ls-files", "journal/sessions"))
        self.assertNotIn("f5f50000", self.sh("git", "show", "HEAD:journal/INDEX.md").stdout)
        self.assertTrue(self.clean(self.w))
        # a name that is nowhere is refused before anything is committed
        (self.w / "code/alpha/README").write_text("staged\n")
        self.git(self.w / "code/alpha", "add", "README")
        before = self.head(self.w / "code/alpha")
        p = self.cudl("commit", "--lab", "notes/nothing.md", "-m", "x", check=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("--lab notes/nothing.md: no such path", p.stderr)
        self.assertEqual(self.head(self.w / "code/alpha"), before)

    # --- a feature's base is a local branch ---

    def test_new_takes_only_a_local_branch_as_base(self):
        a = self.w / "code/alpha"
        p = self.cudl("new", "f1", "-r", "alpha", "--base", "alpha=origin/dev", check=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("origin/dev is a remote-tracking branch", p.stderr)
        self.assertIn(f"git -C {a} branch <name> origin/dev", p.stderr)
        for name in ("nosuch", "dev~0"):  # a revision expression isn't a branch either
            p = self.cudl("new", "f1", "-r", "alpha", "--base", f"alpha={name}", check=False)
            self.assertNotEqual(p.returncode, 0)
            self.assertIn(f"alpha has no branch {name}", p.stderr)
        # refused before anything was written
        self.assertFalse((self.w / "wt/f1").exists())
        self.assertEqual(self.git(self.w, "branch", "--list", "feat/f1"), "")
        self.assertEqual(self.git(a, "branch", "--list", "f1"), "")
        # the local branch it suggests works, and the feature follows it
        self.git(a, "branch", "upstream", "origin/dev")
        self.cudl("new", "f1", "-r", "alpha", "--base", "alpha=upstream")
        self.assertEqual(self.git(a, "config", "branch.f1.cudlbase"), "upstream")
        self.cudl("sync", "-f", "f1")

    def test_take_takes_only_a_local_branch_as_base(self):
        self.cudl("new", "f1", "-r", "alpha")
        f = self.w / "wt/f1"
        p = self.cudl("take", "beta", "--base", "origin/main", cwd=f, check=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("origin/main is a remote-tracking branch", p.stderr)
        self.assertIsNone(self.branch(f / "code/beta"))  # still pinned
        self.cudl("take", "beta", "--base", "main", cwd=f)
        self.assertEqual(self.branch(f / "code/beta"), "f1")

    def test_sync_and_finish_explain_a_base_that_isnt_a_local_branch(self):
        """An older cudl took `--base <repo>=origin/<branch>`; after an upgrade such a feature couldn't
        sync, and the message didn't say why. Both sync and finish now say what to do."""
        self.cudl("new", "f1", "-r", "alpha")
        f = self.w / "wt/f1"
        (f / "code/alpha/README").write_text("work\n")
        self.cudl("commit", "-f", "f1", "-a", "-m", "work")
        for base, why in (("origin/dev", "origin/dev is a remote-tracking branch"),
                          ("dev~0", "alpha has no branch dev~0")):  # as an older cudl could record them
            self.git(f / "code/alpha", "config", "branch.f1.cudlbase", base)
            for args in (("sync", "-f", "f1"), ("finish", "f1")):
                p = self.cudl(*args, check=False)
                self.assertNotEqual(p.returncode, 0, args)
                self.assertIn(why, p.stderr, args)
                self.assertIn("config branch.f1.cudlbase <branch>", p.stderr, args)
                self.assertNotIn("Traceback", p.stderr, args)
        self.assertTrue(f.is_dir())
        # the repair it names
        self.git(f / "code/alpha", "config", "branch.f1.cudlbase", "dev")
        self.cudl("sync", "-f", "f1")
        self.cudl("finish", "f1")
        self.assertFalse(f.exists())

    # --- what the labs reported ---

    def test_a_repo_main_added_later_is_checked_out_in_the_feature(self):
        """A lab's feedback: a repo added to main after a feature was made came in with the feature's sync
        as an empty directory, and `take` ran git there, which found the lab around it and refused."""
        for n in ("f1", "f2", "f3"):
            self.cudl("new", n, "-r", "alpha")
        self.cudl("add", "gamma", str(self.upstream("gamma", "main")))
        f1 = self.w / "wt/f1"
        out = self.cudl("sync", "-f", "f1").stdout
        self.assertIn("gamma: checked out", out)
        g = f1 / "code/gamma"
        self.assertEqual(self.head(g), self.pin(f1, "code/gamma"))
        self.assertIsNone(self.branch(g))  # pinned, as `new` leaves a repo the feature doesn't own
        self.cudl("take", "gamma", cwd=f1)
        self.assertEqual(self.branch(g), "f1")
        # f2 and f3 took main's lab in as an older cudl did, leaving the directory empty: take and sync
        # check it out
        for n in ("f2", "f3"):
            self.git(self.w / "wt" / n, "merge", "-q", "--no-edit", "main")
            self.assertEqual(list((self.w / "wt" / n / "code/gamma").iterdir()), [])
        # --base is checked in gamma, not in the lab git finds around an empty directory (the code review)
        self.git(self.w / "code/gamma", "branch", "only-code")
        p = self.cudl("take", "gamma", "--base", "feat/f1", cwd=self.w / "wt/f2", check=False)  # the lab's
        self.assertIn("gamma has no branch feat/f1", p.stderr)
        self.cudl("take", "gamma", "--base", "only-code", cwd=self.w / "wt/f2")
        self.assertEqual(self.branch(self.w / "wt/f2/code/gamma"), "f2")
        self.assertEqual(self.git(self.w / "wt/f2/code/gamma", "config", "branch.f2.cudlbase"), "only-code")
        self.assertIn("gamma: checked out", self.cudl("sync", "-f", "f3").stdout)
        self.assertEqual(self.head(self.w / "wt/f3/code/gamma"), self.pin(self.w / "wt/f3", "code/gamma"))


if __name__ == "__main__":
    unittest.main()
