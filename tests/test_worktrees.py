"""End-to-end tests of cudl. Claude Code's worktrees: claude -w makes a feature, and Keep or
Remove on leaving it."""

import json
import os
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # helpers.py, however the tests run
from helpers import CudlTest, read_fm  # noqa: E402


class TestWorktrees(CudlTest):
    def test_worktree_hooks(self):
        out = self.cudl("hook", "worktree-create", input=json.dumps(
            {"worktree_name": "wtx", "worktree_path": str(self.w / ".claude/worktrees/wtx"), "cwd": str(self.w)})).stdout
        path = Path(out.strip())
        self.assertEqual(path, self.w / "wt/wtx")
        self.assertIsNone(self.branch(path / "code/alpha"))
        self.cudl("hook", "worktree-remove", input=json.dumps({"worktree_path": str(path), "cwd": str(self.w)}))
        self.assertFalse(path.exists())

    # `claude -w`: Claude Code runs WorktreeCreate, the session starts in the worktree; on exit with
    # "Remove worktree" WorktreeRemove runs first, then SessionEnd, in the main lab

    def test_remove_worktree_at_exit_abandons_the_feature(self):
        sid = "a1a10000-0000-4000-8000-000000000001"
        top = self.claude_w("x", sid)
        self.exchange(sid)
        self.remove_worktree(sid, top)
        self.assertFalse(top.exists())
        branch = self.abandoned(self.w, "x")
        log = next(p for p in self.git(self.w, "ls-tree", "-r", "--name-only", branch, "journal/sessions").split()
                   if "-x-a1a10000" in p)
        fm = read_fm(self.sh("git", "show", f"{branch}:{log}").stdout)
        self.assertTrue(fm["ended"])
        self.assertEqual(fm["transcript"], str(self.exchange(sid)))  # found where Claude Code keeps it
        self.session("session-end", sid, self.w)  # in the main lab, where Claude Code moved the session
        self.assertEqual(list((self.w / "journal/sessions").glob("*a1a10000*")), [])
        self.assertTrue(self.clean(self.w))
        self.assertIn("wt/x was removed when session a1a10000", self.cudl("ls").stdout)  # listed, not taken
        told = self.notices()
        self.assertIn("wt/x was removed when session a1a10000 exited with Remove worktree", told)
        self.assertIn(f"branches kept as {branch}", told)
        self.assertNotIn("wt/x was removed", self.notices())  # shown once

    def test_an_idle_claude_w_leaves_nothing_behind(self):
        sid = "a2a20000-0000-4000-8000-000000000001"
        top = self.claude_w("x", sid)
        self.remove_worktree(sid, top)
        self.assertFalse(top.exists())
        self.assertNotIn("a2a20000", self.git(self.w, "ls-tree", "-r", "--name-only", self.abandoned(self.w, "x"),
                                              "journal/sessions"))  # dropped: it did nothing
        self.session("session-end", sid, self.w)
        self.assertEqual(list((self.w / "journal/sessions").glob("*a2a20000*")), [])

    def test_remove_worktree_saves_older_work_of_a_session_that_did_nothing(self):
        """A session that did nothing leaves no log beside older uncommitted work, and the removal still
        commits that work before it abandons the feature (the code review)."""
        sid = "a2a20000-0000-4000-8000-000000000002"
        top = Path(self.cudl("hook", "worktree-create", input=json.dumps(
            {"session_id": sid, "worktree_name": "x", "cwd": str(self.w)}),
            env={"CLAUDE_CODE_SESSION_ID": sid}).stdout.strip())
        (top / "notes/older.md").write_text("from before the session\n")
        time.sleep(1.1)  # older than the session's start, which is recorded to the second
        self.session("session-start", sid, top)
        self.remove_worktree(sid, top)
        self.assertFalse(top.exists())
        lab = self.abandoned(self.w, "x")
        self.assertEqual(self.git(self.w, "show", f"{lab}:notes/older.md"), "from before the session")
        self.assertNotIn("a2a20000", self.git(self.w, "ls-tree", "-r", "--name-only", lab, "journal/sessions"))

    def test_remove_worktree_commits_what_was_left(self):
        sid = "a3a30000-0000-4000-8000-000000000001"
        top = self.claude_w("x", sid)  # every repo pinned, as WorktreeCreate makes it
        self.exchange(sid)
        (top / "code/alpha/new.txt").write_text("untracked\n")
        (top / "code/beta/README").write_text("modified\n")
        (top / "notes/n.md").write_text("v1\n")
        self.git(top, "add", "notes/n.md")
        (top / "notes/n.md").write_text("v2\n")  # staged, then changed again: both versions kept
        self.remove_worktree(sid, top)
        self.assertFalse(top.exists())
        lab = self.abandoned(self.w, "x")
        subjects = self.git(self.w, "log", "--format=%s", lab).splitlines()
        self.assertIn("cudl: staged at the removal of x (session a3a30000)", subjects)
        self.assertIn("cudl: uncommitted at the removal of x (session a3a30000)", subjects)
        staged = self.git(self.w, "log", "--format=%H", "--grep", "^cudl: staged at the removal", lab)
        self.assertEqual(self.git(self.w, "show", f"{staged}:notes/n.md"), "v1")
        self.assertEqual(self.git(self.w, "show", f"{lab}:notes/n.md"), "v2")
        a, b = self.w / "code/alpha", self.w / "code/beta"
        self.assertEqual(self.git(a, "show", f"{self.abandoned(a, 'x')}:new.txt"), "untracked")
        self.assertEqual(self.git(b, "show", f"{self.abandoned(b, 'x')}:README"), "modified")
        tip = self.git(a, "rev-parse", self.abandoned(a, "x"))
        self.assertEqual(self.git(self.w, "rev-parse", f"{lab}:code/alpha"), tip)  # pinned by the lab
        self.assertIn(tip, self.git(a, "for-each-ref", "--format=%(refname)", "refs/cudl/keep/"))
        self.assertIn("uncommitted work committed first, in alpha, beta, the lab", self.notices())

    def test_remove_worktree_keeps_it_when_it_should(self):
        def rebase_stop(top, feat):
            a = top / "code/alpha"
            self.git(a, "switch", "-q", "-c", feat)
            (a / "y").write_text("y\n")
            self.git(a, "add", "y")
            self.git(a, "commit", "-qm", "y")
            self.sh("git", "rebase", "-x", "false", "HEAD~1", cwd=a, check=False, env={"GIT_EDITOR": "true"})

        cases = [
            ("another session", "session c0c00000 may still be running there",
             lambda top, feat: self.session("session-start", "c0c00000-0000-4000-8000-0000000000aa", top)),
            ("a resumed session", "session c1c10000 may still be running there",
             lambda top, feat: [self.session(e, "c1c10000-0000-4000-8000-0000000000bb", top, **kw)
                                for e, kw in (("session-start", {}), ("session-end", {}),
                                              ("session-start", {"source": "resume"}))]),
            ("another branch", "alpha is on elsewhere, not {feat}",
             lambda top, feat: self.git(top / "code/alpha", "switch", "-q", "-c", "elsewhere")),
            ("too big", "its untracked files come to 101.0 MiB (biggest: code/alpha/big.bin",
             lambda top, feat: os.truncate(top / "code/alpha/big.bin", 101 * 2**20)
             if (top / "code/alpha/big.bin").touch() is None else None),
            ("a rebase", "alpha has a rebase in progress", rebase_stop),
        ]
        for i, (case, expect, make) in enumerate(cases):
            with self.subTest(case):
                sid, feat = f"a4a4000{i}-0000-4000-8000-000000000001", f"x{i}"
                top = self.claude_w(feat, sid)
                self.exchange(sid)
                make(top, feat)
                p = self.remove_worktree(sid, top, check=False)
                self.assertEqual(p.returncode, 1)  # Claude Code keeps a worktree its hook failed to remove
                self.assertTrue(top.is_dir())
                told = self.notices()
                self.assertIn(f"wt/{feat} was kept when session {sid[:8]} asked to remove it", told)
                self.assertIn(expect.format(feat=feat), told)
                self.assertIn(f"`cudl finish {feat} --abandon`", told)
                self.cudl("finish", feat, "--abandon", "--force")

    def test_remove_worktree_ignores_a_parent_feature_s_logs(self):
        self.cudl("new", "p")
        p = self.w / "wt/p"
        self.session("session-start", "c1c10000-0000-4000-8000-000000000001", p)
        self.cudl("commit", "-m", "p: a session's log, committed while it runs", cwd=p)
        self.cudl("new", "c", "--from", "p")  # c inherits p's open log
        sid = "a5a50000-0000-4000-8000-000000000001"
        c = self.w / "wt/c"
        self.session("session-start", sid, c)
        self.exchange(sid)
        self.remove_worktree(sid, c)
        self.assertFalse(c.exists())

    def test_a_subagent_s_worktree_is_removed_only_if_nothing_needs_saving(self):
        parent = "a6a60000-0000-4000-8000-000000000001"
        self.start(parent, CLAUDE_CODE_SESSION_ID=parent)
        for name, leftover in (("agent-1", False), ("agent-2", True)):
            out = self.cudl("hook", "worktree-create", input=json.dumps(
                {"session_id": parent, "worktree_name": name, "cwd": str(self.w)}),
                env={"CLAUDE_CODE_SESSION_ID": parent}).stdout
            top = Path(out.strip())
            self.assertIn("role: continued", next((top / "journal/sessions").glob("*a6a60000*.md")).read_text())
            if leftover:
                (top / "notes/found.md").write_text("a subagent's work\n")
            p = self.cudl("hook", "worktree-remove", input=json.dumps(
                {"session_id": parent, "worktree_path": str(top), "cwd": str(self.w), "reason": "subagent_end"}),
                env={"CLAUDE_CODE_SESSION_ID": parent}, check=False)
            self.assertEqual(p.returncode, 1 if leftover else 0)
            self.assertEqual(top.is_dir(), leftover)
        self.assertIn("removal: commit first", self.notices())

    def test_a_removed_feature_s_session_closes_its_other_logs(self):
        sid = "a7a70000-0000-4000-8000-000000000001"
        self.cudl("new", "g")
        top = self.claude_w("x", sid)
        self.exchange(sid)
        (self.w / "wt/g/notes/g.md").write_text("g\n")
        self.cudl("commit", "-m", "g: a note", cwd=self.w / "wt/g", env={"CLAUDE_CODE_SESSION_ID": sid})
        g_log = next((self.w / "wt/g/journal/sessions").glob("*-g-a7a70000*.md"))
        self.remove_worktree(sid, top)
        self.session("session-end", sid, self.w)
        self.assertIn("ended:", g_log.read_text())
        self.assertIn(g_log.name, self.git(self.w / "wt/g", "ls-files", "journal/sessions"))
        self.assertEqual(list((self.w / "journal/sessions").glob("*a7a70000*")), [])

    def test_a_removed_subagent_s_end_leaves_its_parent_s_work_alone(self):
        parent, sid = "a8a80000-0000-4000-8000-00000000000a", "a8a81111-0000-4000-8000-00000000000b"
        self.start(parent, CLAUDE_CODE_SESSION_ID=parent)
        top = self.claude_w("x", sid, CUDL_SESSION_ID=parent)
        self.assertEqual(self.logged(sid, top)["parent"], parent)
        self.exchange(sid)
        self.remove_worktree(sid, top)
        (self.w / "memory/m.md").write_text("---\nname: m\n---\nthe parent's\n")
        self.cudl("hook", "session-end", input=json.dumps({"session_id": sid, "cwd": str(self.w)}),
                  env={"CLAUDE_CODE_SESSION_ID": sid, "CUDL_SESSION_ID": parent})
        self.assertIn("?? memory/m.md", self.git(self.w, "status", "--porcelain"))  # the parent commits it

    def test_remove_worktree_stopping_halfway_says_what_it_did(self):
        sid = "a9a90000-0000-4000-8000-000000000001"
        top = self.claude_w("x", sid)
        self.exchange(sid)
        (top / "code/alpha/new.txt").write_text("saved\n")
        (top / "notes/n.md").write_text("not saved\n")
        hook = Path(self.git(self.w, "rev-parse", "--path-format=absolute", "--git-common-dir")) / "hooks/pre-commit"
        hook.write_text("#!/bin/sh\necho 'the lab refuses commits' >&2\nexit 1\n")
        hook.chmod(0o755)
        p = self.remove_worktree(sid, top, check=False)
        self.assertEqual(p.returncode, 1)
        self.assertTrue(top.is_dir())
        self.assertEqual(self.git(top / "code/alpha", "log", "-1", "--format=%s", "x"),
                         "cudl: uncommitted at the removal of x (session a9a90000)")
        self.assertIn("?? notes/n.md", self.git(top, "status", "--porcelain"))
        told = self.notices()
        self.assertIn("the lab refuses commits", told)
        self.assertIn("committed before this: alpha", told)

    # --- worktree exits, notices, the memory index: the code review ---

    def test_a_kept_subagent_worktree_leaves_its_parent_s_log_open(self):
        parent = "f2f20000-0000-4000-8000-000000000001"
        self.start(parent, CLAUDE_CODE_SESSION_ID=parent)
        out = self.cudl("hook", "worktree-create", input=json.dumps(
            {"session_id": parent, "worktree_name": "ag", "cwd": str(self.w)}),
            env={"CLAUDE_CODE_SESSION_ID": parent}).stdout
        top = Path(out.strip())
        (top / "notes/found.md").write_text("a subagent's work\n")
        p = self.cudl("hook", "worktree-remove", input=json.dumps(
            {"session_id": parent, "worktree_path": str(top), "cwd": str(self.w), "reason": "subagent_end"}),
            env={"CLAUDE_CODE_SESSION_ID": parent}, check=False)
        self.assertEqual(p.returncode, 1)
        log = next((top / "journal/sessions").glob("*f2f20000*.md"))
        self.assertNotIn("ended:", log.read_text())
        self.assertIn(f"?? journal/sessions/{log.name}", self.git(top, "status", "--porcelain"))

    def test_a_session_working_again_in_a_feature_counts_as_running_there(self):
        a, b = "c4c40000-0000-4000-8000-00000000000a", "c4c41111-0000-4000-8000-00000000000b"
        top = self.claude_w("x", a)
        self.exchange(a)
        self.start(b, CLAUDE_CODE_SESSION_ID=b)
        (top / "notes/b.md").write_text("b\n")
        self.cudl("commit", "-m", "b's note", cwd=top, env={"CLAUDE_CODE_SESSION_ID": b})  # a linked log in x
        self.session("session-end", b, self.w)  # both of b's logs closed
        self.session("session-start", b, self.w, source="resume")
        (top / "notes/b2.md").write_text("b2\n")
        self.cudl("commit", "-m", "b's second note", cwd=top, env={"CLAUDE_CODE_SESSION_ID": b})  # x again
        p = self.remove_worktree(a, top, check=False)
        self.assertEqual(p.returncode, 1)
        self.assertIn("session c4c41111 may still be running there", self.notices())

    def test_a_removal_stopping_after_a_staged_commit_says_so(self):
        sid = "c7c70000-0000-4000-8000-000000000001"
        top = self.claude_w("x", sid)
        self.exchange(sid)
        a = top / "code/alpha"
        self.git(a, "switch", "-q", "-c", "x")
        (a / "f.txt").write_text("v1\n")
        self.git(a, "add", "f.txt")
        (a / "f.txt").write_text("v2\n")
        hook = Path(self.git(a, "rev-parse", "--path-format=absolute", "--git-common-dir")) / "hooks/pre-commit"
        hook.write_text(f"#!/bin/sh\n[ -e {self.root}/once ] && exit 1\ntouch {self.root}/once\n")  # the second fails
        hook.chmod(0o755)
        self.assertEqual(self.remove_worktree(sid, top, check=False).returncode, 1)
        self.assertEqual(self.git(a, "log", "-1", "--format=%s"), "cudl: staged at the removal of x (session c7c70000)")
        self.assertIn("committed before this: alpha", self.notices())

    # --- what the session design review found ---

    def test_a_removed_feature_s_session_still_commits_its_memory(self):
        sid = "e6e60000-0000-4000-8000-000000000001"
        top = self.claude_w("x", sid)
        self.exchange(sid)
        m = self.w / "memory/a-fact.md"
        m.write_text("---\nname: a-fact\ndescription: a fact\n---\nA fact.\n")
        old = time.time() - 120
        os.utime(m, (old, old))  # past the memory index's grace period
        self.remove_worktree(sid, top)
        self.session("session-end", sid, self.w)  # its log went with the feature: no log here
        self.assertIn("memory/a-fact.md", self.git(self.w, "ls-files", "memory"))
        self.assertIn("(a-fact.md)", (self.w / "memory/MEMORY.md").read_text())
        self.assertEqual(list((self.w / "journal/sessions").glob(f"*-{sid[:8]}*.md")), [])

    def test_a_finished_feature_s_old_log_doesnt_hold_a_namesake(self):
        s = "f1f10000-0000-4000-8000-000000000002"
        self.session_in_a_finished_feature(s)
        self.session("session-end", s, self.w)
        t = "f1f1aaaa-0000-4000-8000-000000000002"
        top = self.claude_w("f", t)  # later, a feature of the same name, left with Remove
        self.exchange(t)
        self.remove_worktree(t, top)
        self.assertFalse(top.exists())

    def test_an_idle_claude_w_session_kept_leaves_no_log(self):
        s = "f2f20000-0000-4000-8000-000000000001"
        top = self.claude_w("k", s)
        # nothing done, and Keep: no WorktreeRemove, and the end runs in main
        self.session("session-end", s, self.w, transcript_path=str(self.root / "nowhere.jsonl"))
        self.assertEqual(list((top / "journal/sessions").glob(f"*-{s[:8]}*.md")), [])
        self.assertTrue(self.clean(top))

    def test_a_removal_isnt_held_by_the_session_s_own_children(self):
        # a background Codex review in a `claude -w` worktree, killed with its parent: its log never ends
        a = "f4f40000-0000-4000-8000-000000000001"
        top = self.claude_w("x", a)
        self.exchange(a)
        c = "01a0f4f4-0000-7000-8000-000000000001"
        self.start(c, top=top, agent="codex", CLAUDE_CODE_SESSION_ID=a, CUDL_SESSION_ID=a, CODEX_THREAD_ID=c)
        self.remove_worktree(a, top)
        self.assertFalse(top.exists())


if __name__ == "__main__":
    unittest.main()
