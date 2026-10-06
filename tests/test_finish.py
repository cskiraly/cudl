"""End-to-end tests of cudl. How a feature ends: finish, its checks and resumption, --abandon,
and the sessions a finish waits for."""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # helpers.py, however the tests run
from helpers import CudlTest  # noqa: E402


class TestFinish(CudlTest):
    def test_finish_merges_without_detaching(self):
        self.cudl("new", "f1", "-r", "alpha")
        f = self.w / "wt/f1"
        (f / "code/alpha/README").write_text("changed\n")
        (f / "notes/n.md").write_text("n\n")
        self.cudl("commit", "-a", "-m", "work", cwd=f)
        tip = self.head(f / "code/alpha")
        self.cudl("finish", "f1")
        self.assertEqual(self.branch(self.w / "code/alpha"), "dev")
        self.assertEqual(self.head(self.w / "code/alpha"), tip)
        self.assertEqual(self.pin(self.w, "code/alpha"), tip)
        self.assertTrue((self.w / "notes/n.md").exists())
        self.assertFalse((self.w / "wt/f1").exists())
        self.assertEqual(self.git(self.w / "code/alpha", "branch", "--list", "f1"), "")
        self.assertEqual(self.git(self.w, "branch", "--list", "feat/f1"), "")
        self.assertTrue(self.clean(self.w))

    def test_finish_refuses_dirty(self):
        self.cudl("new", "f1", "-r", "alpha")
        (self.w / "wt/f1/code/alpha/README").write_text("dirty\n")
        p = self.cudl("finish", "f1", check=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("uncommitted", p.stderr)
        self.assertTrue((self.w / "wt/f1").exists())

    def test_parallel_features_sync_then_finish(self):
        self.cudl("new", "f1", "-r", "alpha")
        self.cudl("new", "f2", "-r", "alpha")
        f1, f2 = self.w / "wt/f1", self.w / "wt/f2"
        (f1 / "code/alpha/one").write_text("1\n")
        self.cudl("commit", "-A", "-m", "one", cwd=f1)
        (f2 / "code/alpha/two").write_text("2\n")
        (f2 / "notes/two.md").write_text("2\n")
        self.cudl("commit", "-A", "-m", "two", cwd=f2)
        self.cudl("finish", "f1")
        p = self.cudl("finish", "f2", check=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("cudl sync", p.stderr)
        self.assertTrue(self.clean(self.w))  # a refused finish leaves main untouched
        self.cudl("sync", cwd=f2)
        self.assertTrue((f2 / "code/alpha/one").exists())
        self.assertTrue(self.clean(f2))
        self.cudl("finish", "f2")
        a = self.w / "code/alpha"
        self.assertTrue((a / "one").exists() and (a / "two").exists())
        self.assertEqual(self.branch(a), "dev")
        self.assertEqual(self.pin(self.w, "code/alpha"), self.head(a))
        self.assertTrue(self.clean(self.w))

    def test_abandon_keeps_branches(self):
        self.cudl("new", "f1", "-r", "alpha")
        f = self.w / "wt/f1"
        (f / "code/alpha/x").write_text("x\n")
        self.cudl("commit", "-A", "-m", "x", cwd=f)
        self.cudl("finish", "f1", "--abandon")
        self.assertFalse(f.exists())
        self.assertIn("abandoned/f1-", self.git(self.w / "code/alpha", "branch", "--list", "abandoned/*"))
        self.assertIn("abandoned/f1-", self.git(self.w, "branch", "--list", "abandoned/*"))
        self.assertEqual(self.branch(self.w / "code/alpha"), "dev")

    def test_finish_merges_lab_with_generated_index(self):
        self.cudl("new", "f1")
        f = self.w / "wt/f1"
        for top, sid in ((f, "aaaa1111"), (self.w, "bbbb2222")):
            self.cudl("hook", "session-start", input=json.dumps({"session_id": sid, "cwd": str(top)}), cwd=top)
            self.cudl("commit", "-m", f"session {sid}", cwd=top)
        self.session("session-end", "aaaa1111", f)  # a finish waits for the sessions running in the feature
        self.cudl("finish", "f1")
        index = (self.w / "journal/INDEX.md").read_text()
        self.assertIn("aaaa1111", index)
        self.assertIn("bbbb2222", index)
        self.assertTrue(self.clean(self.w))

    # --- a helper, not a gatekeeper ---

    def test_finish_refuses_a_busy_main_lab(self):
        self.cudl("new", "f1", "-r", "alpha")
        self.work(self.w / "wt/f1", "alpha", "x", "x\n", "x")
        (self.w / "notes/half.md").write_text("half done\n")
        self.git(self.w, "add", "notes/half.md")
        p = self.cudl("finish", "f1", check=False)
        self.assertIn("would take along: notes/half.md", p.stderr)
        self.assertTrue((self.w / "wt/f1").exists())

    # --- a helper, not a gatekeeper: the code review ---

    def test_finish_refuses_a_staged_pin_in_main(self):
        self.cudl("new", "f1", "-r", "alpha")
        self.work(self.w / "wt/f1", "alpha", "x", "x\n", "x")
        b = self.w / "code/beta"
        (b / "y").write_text("y\n")
        self.git(b, "add", "-A")
        self.git(b, "commit", "-q", "-m", "y")
        self.git(self.w, "add", "code/beta")
        p = self.cudl("finish", "f1", check=False)
        self.assertIn("would take along: code/beta", p.stderr)

    # --- a helper, not a gatekeeper: the review's second pass ---

    def test_finish_leaves_a_running_sessions_log_alone(self):
        self.cudl("new", "f1", "-r", "alpha")
        self.work(self.w / "wt/f1", "alpha", "x", "x\n", "x")
        self.session("session-start", "94940000-1", self.w)  # a session running in main
        self.cudl("finish", "f1")
        self.assertIn("?? journal/sessions/", self.git(self.w, "status", "--porcelain"))
        self.assertNotIn("94940000", self.git(self.w, "show", "HEAD:journal/INDEX.md"))

    # rewritten parents: sync --rebase, precise parent state, finish --stack

    def test_finish_resumes_after_a_failed_commit(self):
        self.cudl("new", "f1", "-r", "alpha")
        tip = self.work(self.w / "wt/f1", "alpha", "x", "1\n", "one")
        flag = self.root / "fail-commits"
        hook = self.w / ".git/hooks/commit-msg"
        hook.write_text(f"#!/bin/sh\n[ -e '{flag}' ] && exit 1\nexit 0\n")
        hook.chmod(0o755)
        flag.write_text("")
        p = self.cudl("finish", "f1", check=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertEqual(self.git(self.w / "code/alpha", "rev-parse", "dev"), tip)  # code merged, lab commit failed
        self.assertTrue((self.w / ".git/cudl-finish.json").exists())
        flag.unlink()
        self.cudl("finish", "f1")
        self.assertEqual(self.git(self.w, "log", "-1", "--format=%s"), "cudl: finish f1")
        self.assertEqual(self.pin(self.w, "code/alpha"), tip)
        self.assertFalse((self.w / "wt/f1").exists())
        self.assertFalse((self.w / ".git/cudl-finish.json").exists())

    def test_finish_resumes_a_stopped_code_merge(self):
        self.cudl("new", "f1", "-r", "alpha,beta")
        f = self.w / "wt/f1"
        (f / "code/alpha/a").write_text("1\n")
        (f / "code/beta/b").write_text("1\n")
        self.cudl("commit", "-A", "-m", "both", cwd=f)
        flag = self.root / "fail-commits"
        hooks = Path(self.git(self.w / "code/beta", "rev-parse", "--path-format=absolute", "--git-path", "hooks"))
        hooks.mkdir(exist_ok=True)
        (hooks / "commit-msg").write_text(f"#!/bin/sh\n[ -e '{flag}' ] && exit 1\nexit 0\n")
        (hooks / "commit-msg").chmod(0o755)
        flag.write_text("")
        p = self.cudl("finish", "f1", "--no-ff", check=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertTrue((self.w / ".git/cudl-finish.json").exists())
        flag.unlink()
        self.cudl("finish", "f1")  # goes on as it started (--no-ff), concluding beta's merge
        for repo, branch in (("alpha", "dev"), ("beta", "main")):
            d = self.w / "code" / repo
            self.assertEqual(len(self.git(d, "log", "-1", "--format=%P", branch).split()), 2)
            self.assertEqual(self.pin(self.w, f"code/{repo}"), self.git(d, "rev-parse", branch))
        self.assertFalse((self.w / "wt/f1").exists())
        self.assertFalse((self.w / ".git/cudl-finish.json").exists())

    def test_finish_aborts_cleanly_when_the_first_merge_fails(self):
        self.cudl("new", "f1", "-r", "alpha")
        self.work(self.w / "wt/f1", "alpha", "a", "1\n", "one")
        flag = self.root / "fail-commits"
        hooks = Path(self.git(self.w / "code/alpha", "rev-parse", "--path-format=absolute", "--git-path", "hooks"))
        hooks.mkdir(exist_ok=True)
        (hooks / "commit-msg").write_text(f"#!/bin/sh\n[ -e '{flag}' ] && exit 1\nexit 0\n")
        (hooks / "commit-msg").chmod(0o755)
        flag.write_text("")
        main_head, dev = self.head(self.w), self.git(self.w / "code/alpha", "rev-parse", "dev")
        p = self.cudl("finish", "f1", "--no-ff", check=False)
        self.assertIn("nothing merged", p.stderr)
        self.assertEqual(self.head(self.w), main_head)
        self.assertEqual(self.git(self.w / "code/alpha", "rev-parse", "dev"), dev)
        self.assertTrue(self.clean(self.w))
        self.assertEqual(self.sh("git", "rev-parse", "-q", "--verify", "MERGE_HEAD", cwd=self.w / "code/alpha",
                                 check=False).stdout, "")
        self.assertFalse((self.w / ".git/cudl-finish.json").exists())
        flag.unlink()
        self.cudl("finish", "f1", "--no-ff")

    # rewritten parents: the code review

    def test_finish_retains_a_feature_that_changed_meanwhile(self):
        self.cudl("new", "f1", "-r", "alpha")
        f = self.w / "wt/f1"
        self.work(f, "alpha", "a", "1\n", "one")
        self.cudl("new", "kid", "--from", "f1")
        flag = self.root / "fail"
        self.fail_commits(self.w, flag)
        p = self.cudl("finish", "f1", check=False)
        self.assertNotEqual(p.returncode, 0)
        flag.unlink()
        (f / "notes/late.md").write_text("late\n")
        self.cudl("commit", "-m", "a late note", cwd=f)  # like a session ending in there
        p = self.cudl("finish", "f1", check=False)
        self.assertIn("it changed since, so it stays", p.stderr)
        self.assertTrue(f.is_dir())
        self.assertIsNone(self.sh("git", "config", "branch.feat/kid.cudlparentdone", check=False).stdout.strip() or None)
        self.assertFalse((self.w / ".git/cudl-finish.json").exists())
        self.cudl("finish", "f1")  # merges the rest, then removes it
        self.assertTrue((self.w / "notes/late.md").exists())
        self.assertFalse(f.exists())
        self.assertIn("finished", self.git(self.w, "config", "branch.feat/kid.cudlparentdone"))

    def test_finish_deletes_a_branch_that_has_an_upstream(self):
        self.cudl("new", "f1", "-r", "alpha")
        f = self.w / "wt/f1"
        self.work(f, "alpha", "a", "1\n", "one")
        fork = self.root / "fork.git"
        self.git(self.root, "init", "-q", "--bare", str(fork))
        fa = f / "code/alpha"
        self.git(fa, "remote", "add", "fork", str(fork))
        self.git(fa, "push", "-q", "-u", "fork", "f1")
        self.work(f, "alpha", "b", "2\n", "two, not pushed")
        self.cudl("finish", "f1")
        self.assertFalse(f.exists())
        self.assertEqual(self.sh("git", "rev-parse", "-q", "--verify", "refs/heads/f1", cwd=self.w / "code/alpha",
                                 check=False).stdout, "")

    def test_finish_resume_checks_what_it_concludes(self):
        self.cudl("new", "f1", "-r", "alpha,beta")
        f = self.w / "wt/f1"
        (f / "code/alpha/a").write_text("1\n")
        (f / "code/beta/b").write_text("1\n")
        self.cudl("commit", "-A", "-m", "both", cwd=f)
        flag = self.root / "fail"
        self.fail_commits(self.w / "code/beta", flag)
        self.cudl("finish", "f1", "--no-ff", check=False)
        flag.unlink()
        mb = self.w / "code/beta"
        (mb / "b").write_text("tampered\n")
        self.git(mb, "add", "b")
        p = self.cudl("finish", "f1", check=False)
        self.assertIn("isn't exactly f1's", p.stderr)
        self.git(mb, "merge", "--abort")
        # the lab: switched to another branch meanwhile
        self.git(self.w, "merge", "--abort")
        self.git(self.w, "switch", "-q", "-c", "elsewhere")
        p = self.cudl("finish", "f1", check=False)
        self.assertIn("this finish merges into main", p.stderr)
        self.git(self.w, "switch", "-q", "main")
        self.cudl("finish", "f1")
        self.assertFalse(f.exists())
        self.assertEqual(self.git(mb, "show", "main:b"), "1")

    def test_one_finish_at_a_time(self):
        import fcntl
        self.cudl("new", "f1", "-r", "alpha")
        with open(self.w / ".git/cudl-finish.lock", "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            p = self.cudl("finish", "f1", check=False)
        self.assertIn("another cudl finish is running", p.stderr)
        (self.w / ".git/cudl-finish.json").write_text('{"command": "cudl finish x", "stack": null, "current": null}\n')
        self.cudl("finish", "f1")  # a finished plan left behind doesn't block

    # rewritten parents: the review's second pass

    def test_finish_resume_checks_the_main_checkout_of_a_merged_repo(self):
        self.cudl("new", "f1", "-r", "alpha")
        tip = self.work(self.w / "wt/f1", "alpha", "a", "1\n", "one")
        flag = self.root / "fail"
        self.fail_commits(self.w, flag)
        self.cudl("finish", "f1", check=False)  # code merged, lab commit failed
        flag.unlink()
        ma = self.w / "code/alpha"
        self.git(ma, "switch", "-q", "-c", "older", f"{tip}^")
        p = self.cudl("finish", "f1", check=False)
        self.assertIn("switch it back", p.stderr)
        self.git(ma, "switch", "-q", "dev")
        self.cudl("finish", "f1")
        self.assertEqual(self.pin(self.w, "code/alpha"), tip)

    def test_ls_leaves_the_finish_journal_alone(self):
        f = self.w / ".git/cudl-finish.json"
        f.write_text('{"command": "cudl finish x", "stack": null, "current": null}\n')
        self.cudl("ls")
        self.assertTrue(f.exists())

    # --- the live checks' fixes ---

    def test_finish_commits_memory_plans_and_logs_first(self):
        self.cudl("new", "f1", "-r", "alpha")
        f = self.w / "wt/f1"
        self.work(f, "alpha", "x", "x\n", "x")
        (self.w / "memory/MEMORY.md").write_text("- one\n")
        (self.w / "memory/old.md").write_text("old\n")
        (self.w / "notes/n.md").write_text("n\n")
        self.cudl("commit", "-m", "memory and a note")
        # a session in main, still running: its log committed once, then updated; memory changed
        sid = "f1f1f1f1-0001"
        self.session("session-start", sid, self.w)
        self.cudl("commit", "-m", "its log")
        log = next((self.w / "journal/sessions").glob("*-main-f1f1f1f1.md"))
        log.write_text(log.read_text().replace("outcome: open", "outcome: partial"))
        (self.w / "memory/MEMORY.md").write_text("- one\n- two\n")
        self.git(self.w, "rm", "-q", "memory/old.md")  # a staged deletion
        (self.w / "plans/p.md").write_text("# a new plan: untracked, stays out\n")
        # anything else still stops a finish, and the refusal writes nothing
        (self.w / "notes/n.md").write_text("n, edited\n")
        head = self.head(self.w)
        p = self.cudl("finish", "f1", check=False)
        self.assertIn("would take along: notes/n.md", p.stderr)
        self.assertNotIn("memory/", p.stderr)
        self.assertEqual(self.head(self.w), head)
        self.git(self.w, "checkout", "--", "notes/n.md")
        # so does a session file staged and then changed again
        (self.w / "memory/MEMORY.md").write_text("- one\n- two\n- three\n")
        self.git(self.w, "add", "memory/MEMORY.md")
        (self.w / "memory/MEMORY.md").write_text("- one\n- two\n- three, edited\n")
        p = self.cudl("finish", "f1", check=False)
        self.assertIn("would take along: memory/MEMORY.md", p.stderr)
        self.assertEqual(self.head(self.w), head)
        self.git(self.w, "add", "memory/MEMORY.md")
        self.cudl("finish", "f1")
        subjects = self.git(self.w, "log", "-2", "--first-parent", "--format=%s").splitlines()
        self.assertEqual(subjects, ["cudl: finish f1", "lab: memory, plans and session logs before finishing f1"])
        checkpoint = self.git(self.w, "show", "--name-status", "--format=", "HEAD^1").splitlines()
        self.assertEqual(sorted(checkpoint), ["D\tmemory/old.md", "M\tjournal/INDEX.md",  # rebuilt from the committed logs
                                              f"M\tjournal/sessions/{log.name}", "M\tmemory/MEMORY.md"])
        self.assertEqual(self.git(self.w, "status", "--porcelain"), "?? plans/p.md")

    def test_finish_checkpoint_stays_when_the_merge_stops(self):
        self.cudl("new", "f1", "-r", "alpha")
        f = self.w / "wt/f1"
        (f / "notes/x.md").write_text("from the feature\n")
        self.work(f, "alpha", "x", "x\n", "x")
        (self.w / "notes/x.md").write_text("from main\n")
        (self.w / "memory/MEMORY.md").write_text("- one\n")
        self.cudl("commit", "-m", "x on main too")
        (self.w / "memory/MEMORY.md").write_text("- one\n- two\n")
        p = self.cudl("finish", "f1", check=False)
        self.assertIn("lab merge of feat/f1 failed", p.stderr)
        self.assertEqual(self.git(self.w, "log", "-1", "--format=%s"),
                         "lab: memory, plans and session logs before finishing f1")  # the checkpoint stays
        self.assertTrue(self.clean(self.w))
        self.assertTrue(f.exists())
        # the usual way on: sync, resolve, finish
        p = self.cudl("sync", "-f", "f1", check=False)
        self.assertNotEqual(p.returncode, 0)
        (f / "notes/x.md").write_text("both\n")
        self.git(f, "add", "notes/x.md")
        self.cudl("commit", "-f", "f1", "-m", "both")
        self.cudl("finish", "f1")
        self.assertEqual((self.w / "notes/x.md").read_text(), "both\n")
        self.assertEqual((self.w / "memory/MEMORY.md").read_text(), "- one\n- two\n")

    # --- review 1 of the live-check fixes ---

    def test_the_checkpoint_writes_nothing_when_its_commit_fails(self):
        self.cudl("new", "f1", "-r", "alpha")
        self.work(self.w / "wt/f1", "alpha", "x", "x\n", "x")
        (self.w / "memory/MEMORY.md").write_text("- one\n")
        self.cudl("commit", "-m", "memory")
        (self.w / "memory/MEMORY.md").write_text("- one\n- two\n")
        flag = self.root / "fail"
        self.fail_commits(self.w, flag)
        head = self.head(self.w)
        self.assertNotEqual(self.cudl("finish", "f1", check=False).returncode, 0)
        self.assertEqual(self.head(self.w), head)
        self.assertEqual(self.git(self.w, "diff", "--cached", "--name-only"), "")  # nothing left staged
        flag.unlink()
        self.cudl("finish", "f1")
        self.assertEqual(self.git(self.w, "log", "-1", "--first-parent", "--format=%s", "HEAD^1"),
                         "lab: memory, plans and session logs before finishing f1")

    def test_the_checkpoint_index_lists_only_committed_logs(self):
        self.cudl("new", "f1", "-r", "alpha")
        self.work(self.w / "wt/f1", "alpha", "x", "x\n", "x")
        self.session("session-start", "c4c4c4c4-1", self.w)  # running in main, its log untracked
        self.cudl("index")  # the working index now links it
        self.assertIn("c4c4c4c4", (self.w / "journal/INDEX.md").read_text())
        self.cudl("finish", "f1")
        self.assertNotIn("c4c4c4c4", self.sh("git", "show", "HEAD^1:journal/INDEX.md").stdout)
        self.assertNotIn("c4c4c4c4", self.sh("git", "show", "HEAD:journal/INDEX.md").stdout)

    # --- review 2 of the live-check fixes ---

    def test_a_staged_index_doesnt_block_a_finish(self):
        self.cudl("new", "f1", "-r", "alpha")
        self.work(self.w / "wt/f1", "alpha", "x", "x\n", "x")
        self.session("session-start", "c5c5c5c5-1", self.w)  # running in main, its log untracked
        self.cudl("index")
        self.git(self.w, "add", "journal/INDEX.md")  # the index alone, staged by hand
        self.cudl("finish", "f1")
        self.assertEqual(self.git(self.w, "diff", "--cached", "--name-only"), "")
        self.assertNotIn("c5c5c5c5", self.sh("git", "show", "HEAD:journal/INDEX.md").stdout)

    # --- worktree exits, notices, the memory index ---

    def test_finish_and_abandon_keep_the_lab_s_ignored_files(self):
        for feat, args in (("f1", ()), ("f2", ("--abandon",))):
            self.cudl("new", feat, "-r", "alpha")
            f = self.w / "wt" / feat
            self.work(f, "alpha", "x", "x\n", "x")
            (f / "scratch/run").mkdir(parents=True)
            (f / "scratch/run/result.txt").write_text("hours of compute\n")
            (f / "journal/raw").mkdir(parents=True)
            (f / "journal/raw/t.json").write_text("{}\n")
            out = self.cudl("finish", feat, *args).stdout
            self.assertFalse(f.exists())
            kept = next((self.w / "scratch").glob(f"{feat}-*"))
            self.assertEqual((kept / "scratch/run/result.txt").read_text(), "hours of compute\n")
            self.assertTrue((kept / "journal/raw/t.json").is_file())
            self.assertFalse((kept / ".claude").exists())  # settings.local.json is cudl's to rewrite
            self.assertIn(f"in scratch/{kept.name}/", out)
            self.assertTrue(self.clean(self.w))

    def test_abandon_goes_on_after_a_stop(self):
        self.cudl("new", "f1", "-r", "alpha,beta")
        f = self.w / "wt/f1"
        self.work(f, "alpha", "x", "x\n", "x")
        self.git(self.w / "code/beta", "worktree", "lock", str(f / "code/beta"))
        p = self.cudl("finish", "f1", "--abandon", check=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertTrue(f.is_dir())
        names = {d: self.git(d, "branch", "--list", "abandoned/*", "--format=%(refname:short)")
                 for d in (self.w, self.w / "code/alpha", self.w / "code/beta")}
        self.assertEqual(len(set(names.values())), 1)  # every branch already under its final name
        self.assertEqual(self.git(self.w / "code/alpha", "branch", "--list", "f1"), "")
        self.git(self.w / "code/beta", "worktree", "unlock", str(f / "code/beta"))
        out = self.cudl("finish", "f1", "--abandon").stdout
        self.assertIn("going on with abandoning f1", out)
        self.assertFalse(f.exists())
        for d in (self.w, self.w / "code/alpha", self.w / "code/beta"):
            self.assertEqual(self.git(d, "branch", "--list", "abandoned/*", "--format=%(refname:short)"),
                             names[self.w])
        self.assertEqual(self.git(self.w, "worktree", "list", "--porcelain").count("worktree "), 1)
        self.assertEqual(self.cudl("finish", "f1", "--abandon", check=False).returncode, 1)  # nothing left

    def test_abandon_stops_for_a_clean_checkout_mid_rebase(self):
        self.cudl("new", "f1", "-r", "alpha")
        f = self.w / "wt/f1"
        self.work(f, "alpha", "x", "x\n", "x")
        self.sh("git", "rebase", "-x", "false", "HEAD~1", cwd=f / "code/alpha", check=False,
                env={"GIT_EDITOR": "true"})
        self.assertEqual(self.git(f / "code/alpha", "status", "--porcelain"), "")
        p = self.cudl("finish", "f1", "--abandon", check=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("alpha has a rebase in progress", p.stderr)
        self.assertTrue((f / "code/alpha").is_dir())

    # --- worktree exits, notices, the memory index: the code review ---

    def test_abandon_checks_again_when_it_goes_on(self):
        self.cudl("new", "f1", "-r", "alpha,beta")
        f = self.w / "wt/f1"
        self.work(f, "beta", "y", "y\n", "y")
        self.git(self.w / "code/beta", "worktree", "lock", str(f / "code/beta"))
        self.assertNotEqual(self.cudl("finish", "f1", "--abandon", check=False).returncode, 0)  # stops at beta
        # meanwhile a rebase stops at a clean state in beta, and a note appears in the lab
        self.sh("git", "rebase", "-x", "false", "HEAD~1", cwd=f / "code/beta", check=False, env={"GIT_EDITOR": "true"})
        (f / "notes/new.md").write_text("new\n")
        self.git(self.w / "code/beta", "worktree", "unlock", str(f / "code/beta"))
        p = self.cudl("finish", "f1", "--abandon", check=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("commit first", p.stderr)
        (f / "notes/new.md").unlink()
        p = self.cudl("finish", "f1", "--abandon", check=False)
        self.assertIn("beta has a rebase in progress", p.stderr)
        self.sh("git", "rebase", "--abort", cwd=f / "code/beta")
        self.cudl("finish", "f1", "--abandon")
        self.assertFalse(f.exists())
        # an intent left by an earlier feature of the same name isn't this one's
        self.cudl("new", "f2")
        common = Path(self.git(self.w, "rev-parse", "--path-format=absolute", "--git-common-dir"))
        (common / "cudl-abandon").mkdir(exist_ok=True)
        (common / "cudl-abandon/f2.json").write_text(json.dumps(
            {"stamp": "19990101-000000", "id": "not-f2", "owned": [], "kids": []}))
        self.cudl("finish", "f2", "--abandon")
        self.assertNotIn("19990101", self.git(self.w, "branch", "--list", "abandoned/f2-*"))

    def test_abandon_going_on_keeps_both_versions_of_an_ignored_file(self):
        self.cudl("new", "f1")
        f = self.w / "wt/f1"
        (f / "scratch").mkdir()
        (f / "scratch/r.txt").write_text("first\n")
        self.git(self.w, "worktree", "lock", str(f))
        self.assertNotEqual(self.cudl("finish", "f1", "--abandon", check=False).returncode, 0)
        (f / "scratch").mkdir()
        (f / "scratch/r.txt").write_text("second\n")
        self.git(self.w, "worktree", "unlock", str(f))
        self.cudl("finish", "f1", "--abandon")
        kept = sorted((d / "scratch/r.txt").read_text() for d in (self.w / "scratch").glob("f1-*"))
        self.assertEqual(kept, ["first\n", "second\n"])

    def test_an_abandoned_feature_s_session_leaves_a_child_s_copy_of_its_log_alone(self):
        self.cudl("new", "p")
        p = self.w / "wt/p"
        sid = "c2c20000-0000-4000-8000-000000000001"
        self.session("session-start", sid, p)
        self.cudl("commit", "-m", "p: the session's log, committed while it runs", cwd=p)
        self.cudl("new", "c", "--from", "p")
        c = self.w / "wt/c"
        copy = next((c / "journal/sessions").glob("*-p-c2c20000*.md"))
        before = (copy.read_text(), self.head(c))
        self.cudl("finish", "p", "--abandon", env={"CLAUDE_CODE_SESSION_ID": sid})  # the session itself abandons it
        self.session("session-end", sid, self.w)
        self.assertEqual((copy.read_text(), self.head(c)), before)

    # --- what the session design review found ---

    def test_finish_refuses_while_a_session_runs_in_the_feature(self):
        t = "f5f50000-0000-4000-8000-000000000001"
        self.cudl("new", "g", "-r", "alpha")
        g = self.w / "wt/g"
        self.start(t, top=g, CLAUDE_CODE_SESSION_ID=t)
        self.cudl("commit", "-f", "g", "-m", "a wrap", env={"CLAUDE_CODE_SESSION_ID": t})  # its log is committed
        for args in (["finish", "g"], ["finish", "g", "--abandon"]):
            p = self.cudl(*args, check=False)
            self.assertNotEqual(p.returncode, 0, args)
            self.assertIn("f5f50000", p.stderr)
            self.assertTrue(g.is_dir())
        self.session("session-end", t, g)  # it ends: the finish goes through
        self.cudl("finish", "g")
        self.assertFalse(g.exists())

    def test_finish_counts_neither_its_own_session_nor_a_visitor(self):
        # the session running the finish doesn't count; one that only worked here with -f gets a note
        t = "f5f50000-0000-4000-8000-000000000002"
        self.cudl("new", "g", "-r", "alpha")
        g = self.w / "wt/g"
        self.start(t, top=g, CLAUDE_CODE_SESSION_ID=t)
        self.cudl("commit", "-f", "g", "-m", "a wrap", env={"CLAUDE_CODE_SESSION_ID": t})
        v = "f5f5aaaa-0000-4000-8000-000000000002"
        self.start(v, CLAUDE_CODE_SESSION_ID=v)  # in main
        (g / "notes/v.md").write_text("v\n")
        self.cudl("commit", "-f", "g", "-m", "v's note", env={"CLAUDE_CODE_SESSION_ID": v})  # a linked log in g
        p = self.cudl("finish", "g", env={"CLAUDE_CODE_SESSION_ID": t})
        self.assertIn("f5f5aaaa", p.stdout + p.stderr)
        self.assertFalse(g.exists())

    def test_finish_abandon_force_goes_on_despite_a_running_session(self):
        t = "f5f50000-0000-4000-8000-000000000003"
        self.cudl("new", "g", "-r", "alpha")
        g = self.w / "wt/g"
        self.start(t, top=g, CLAUDE_CODE_SESSION_ID=t)
        self.cudl("finish", "g", "--abandon", "--force")
        self.assertFalse(g.exists())

    def test_finish_refuses_while_a_resumed_session_runs_in_the_feature(self):
        # started in main, resumed in g: its log in g is a linked one, but its working directory is g
        s = "f5f50000-0000-4000-8000-000000000004"
        self.cudl("new", "g", "-r", "alpha")
        g = self.w / "wt/g"
        self.session("session-start", s, self.w)
        self.session("session-end", s, self.w)
        self.session("session-start", s, g, source="resume")
        self.cudl("commit", "-f", "g", "-m", "its log", env={"CLAUDE_CODE_SESSION_ID": s})
        p = self.cudl("finish", "g", check=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("f5f50000", p.stderr)
        self.assertTrue(g.is_dir())

    # --- the session design review's fixes: the code review ---

    def test_finish_isnt_held_by_a_session_that_resumed_elsewhere(self):
        # started in g, quit, resumed in main; from there it works in g with -f: it runs in main
        s = "f5f50000-0000-4000-8000-000000000006"
        self.cudl("new", "g", "-r", "alpha")
        g = self.w / "wt/g"
        self.session("session-start", s, g)
        self.session("session-end", s, g)
        self.session("session-start", s, self.w, source="resume")
        (g / "notes/later.md").write_text("later\n")
        self.cudl("commit", "-f", "g", "-A", "-m", "later", env={"CLAUDE_CODE_SESSION_ID": s})  # reopens g's log
        p = self.cudl("finish", "g")
        self.assertIn("f5f50000", p.stdout)  # a note
        self.assertFalse(g.exists())

    def test_finish_isnt_held_by_a_killed_child_of_an_ended_session(self):
        # a background review in g, killed when its parent quit: the parent's end committed its open log
        s, c = "f5f50000-0000-4000-8000-000000000007", "01a0f5f5-0000-7000-8000-000000000007"
        self.cudl("new", "g", "-r", "alpha")
        g = self.w / "wt/g"
        self.start(s, top=g, CLAUDE_CODE_SESSION_ID=s)
        self.start(c, top=g, agent="codex", CLAUDE_CODE_SESSION_ID=s, CUDL_SESSION_ID=s, CODEX_THREAD_ID=c)
        self.session("session-end", s, g)
        p = self.cudl("finish", "g")
        self.assertIn("01a0f5f5", p.stdout)  # a note
        self.assertFalse(g.exists())

    def test_finish_waits_for_a_session_that_entered_the_feature_with_enterworktree(self):
        # a session in main moves into a new worktree mid-session: WorktreeCreate, no SessionStart
        s = "f5f50000-0000-4000-8000-000000000008"
        self.start(s, CLAUDE_CODE_SESSION_ID=s)
        out = self.cudl("hook", "worktree-create", env={"CLAUDE_CODE_SESSION_ID": s},
                        input=json.dumps({"session_id": s, "worktree_name": "g", "cwd": str(self.w)})).stdout
        g = Path(out.strip())
        self.cudl("commit", "-f", "g", "-m", "its log", env={"CLAUDE_CODE_SESSION_ID": s})
        p = self.cudl("finish", "g", check=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("f5f50000", p.stderr)
        self.assertTrue(g.is_dir())

    def test_finish_keep_doesnt_wait_for_a_session_in_the_feature(self):
        # --keep merges and leaves the worktree where it is: nothing is removed under the session
        t = "f5f50000-0000-4000-8000-000000000009"
        self.cudl("new", "g", "-r", "alpha")
        g = self.w / "wt/g"
        self.start(t, top=g, CLAUDE_CODE_SESSION_ID=t)
        self.cudl("commit", "-f", "g", "-m", "its log", env={"CLAUDE_CODE_SESSION_ID": t})
        self.cudl("finish", "g", "--keep")
        self.assertTrue(g.is_dir())


    # --- what an ended session's end couldn't commit ---

    def ended_but_uncommitted(self, mend=True):
        """A session worked in f1 and ended, but its end's commit failed (signing, say): its log is left
        uncommitted, with `ended:` (seen in a pilot lab). With mend, commits work again."""
        sid = "e0d0e0d0-0000-4000-8000-000000000001"
        self.cudl("new", "f1", "-r", "alpha")
        f = self.w / "wt/f1"
        self.session("session-start", sid, f)
        self.work(f, "alpha", "a1", "1\n", "a one")
        flag = self.root / "fail"
        self.fail_commits(f, flag)
        self.session("session-end", sid, f)
        if mend:
            flag.unlink()
        log = next((f / "journal/sessions").glob(f"*-f1-{sid[:8]}*.md"))
        self.assertIn("ended:", log.read_text())
        self.assertIn(log.name, self.git(f, "status", "--porcelain"))
        return f, log

    def test_finish_commits_what_an_ended_session_s_end_couldnt(self):
        f, log = self.ended_but_uncommitted()
        out = self.cudl("finish", "f1").stdout  # it said "the lab has uncommitted changes (a session still running there?)"
        self.assertIn(log.name, out)
        self.assertFalse(f.exists())
        self.assertIn("ended:", self.sh("git", "show", f"HEAD:journal/sessions/{log.name}").stdout)
        self.assertTrue(self.clean(self.w))

    def test_sync_commits_what_an_ended_session_s_end_couldnt(self):
        f, log = self.ended_but_uncommitted()
        self.assertIn(log.name, self.cudl("sync", "-f", "f1").stdout)
        self.assertTrue(self.clean(f))
        self.assertIn(log.name, self.sh("git", "show", "HEAD:journal/INDEX.md", cwd=f).stdout)

    def test_finish_commits_a_never_committed_log_and_the_index_its_end_wrote(self):
        # the session committed code with plain git, so its end was its log's first commit, and that commit
        # writes the session index first: both are left behind (the code review)
        sid = "e0d0e0d0-0000-4000-8000-000000000003"
        self.cudl("new", "f1", "-r", "alpha")
        f = self.w / "wt/f1"
        self.session("session-start", sid, f)
        (f / "code/alpha/b").write_text("b\n")
        self.git(f / "code/alpha", "add", "-A")
        self.git(f / "code/alpha", "commit", "-q", "-m", "plain git")
        flag = self.root / "fail"
        self.fail_commits(f, flag)
        self.session("session-end", sid, f)
        flag.unlink()
        status = self.git(f, "status", "--porcelain")
        self.assertIn("journal/INDEX.md", status)
        self.assertIn(f"?? journal/sessions/", status)
        self.cudl("finish", "f1")
        self.assertFalse(f.exists())
        self.assertIn(sid[:8], self.sh("git", "show", "HEAD:journal/INDEX.md").stdout)
        self.assertTrue(self.clean(self.w))

    def broken_signing(self):
        """Signing on, with a key no agent holds: a commit can't be written (as test_signing_failures_are_explained)."""
        return {"GIT_CONFIG_COUNT": "3", "GIT_CONFIG_KEY_0": "commit.gpgsign", "GIT_CONFIG_VALUE_0": "true",
                "GIT_CONFIG_KEY_1": "gpg.format", "GIT_CONFIG_VALUE_1": "ssh",
                "GIT_CONFIG_KEY_2": "user.signingkey", "GIT_CONFIG_VALUE_2": str(self.root / "nokey.pub"),
                "SSH_AUTH_SOCK": ""}

    def test_finish_says_why_it_can_t_commit_an_ended_session_s_log(self):
        # signing still fails: finish stops, names the log, says why, and keeps it (the code review)
        f, log = self.ended_but_uncommitted()
        p = self.cudl("finish", "f1", check=False, env=self.broken_signing())
        self.assertNotEqual(p.returncode, 0)
        self.assertIn(f"couldn't be committed (journal/sessions/{log.name})", p.stderr)
        self.assertIn("SSH_AUTH_SOCK is not set", p.stderr)
        self.assertEqual(p.stderr.count("commit signing is on"), 1)
        self.assertTrue(log.is_file())
        self.assertIn(log.name, self.git(f, "status", "--porcelain"))

    def test_a_finish_whose_first_commit_can_t_be_signed_says_why(self):
        # a finish whose checkpoint of main's session files failed on signing said only git's message,
        # as every commit made through git_retry did
        self.cudl("new", "f1", "-r", "alpha")
        self.work(self.w / "wt/f1", "alpha", "a1", "1\n", "a one")
        (self.w / "plans/p.md").write_text("a plan\n")
        self.cudl("commit", "-m", "a plan")
        (self.w / "plans/p.md").write_text("a plan, changed\n")  # what the checkpoint commits first
        p = self.cudl("finish", "f1", check=False, env=self.broken_signing())
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("failed to write commit object", p.stderr)
        self.assertIn("commit signing is on (ssh", p.stderr)
        self.assertIn("SSH_AUTH_SOCK is not set", p.stderr)
        self.assertTrue((self.w / "wt/f1").is_dir())

    def test_a_running_session_s_log_still_stops_a_finish(self):
        # the same, but the session hasn't ended: its log may still be written
        sid = "e0d0e0d0-0000-4000-8000-000000000002"
        self.cudl("new", "f1", "-r", "alpha")
        f = self.w / "wt/f1"
        self.session("session-start", sid, f)
        self.work(f, "alpha", "a1", "1\n", "a one")
        log = next((f / "journal/sessions").glob(f"*-f1-{sid[:8]}*.md"))
        log.write_text(log.read_text() + "a line its session wrote\n")
        p = self.cudl("finish", "f1", check=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertTrue(f.is_dir())


if __name__ == "__main__":
    unittest.main()
