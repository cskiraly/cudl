"""End-to-end tests of cudl. Sessions: the session hooks, logs and linked logs, subagents, idle
sessions, transcripts, compactions, the memory index."""

import json
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # helpers.py, however the tests run
from helpers import CUDL, CudlTest, read_fm  # noqa: E402


class TestSessions(CudlTest):
    def test_session_hooks(self):
        self.cudl("new", "f1", "-r", "alpha")
        f = self.w / "wt/f1"
        (f / "handoff/f1.md").write_text("# f1\n\nState: halfway.\n")
        self.cudl("commit", "-m", "handoff", cwd=f)
        sid = "abcdef12-3456-7890-abcd-ef1234567890"
        inp = json.dumps({"session_id": sid, "cwd": str(f / "code/alpha"), "source": "startup",
                          "transcript_path": "/tmp/t.jsonl"})
        out = json.loads(self.cudl("hook", "session-start", input=inp, cwd=f).stdout)
        ctx = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn("feature `f1`", ctx)
        self.assertRegex(out["systemMessage"], r"^cudl: feature f1 · log journal/sessions/.*-f1-abcdef12\.md · handoff loaded$")
        self.assertIn("State: halfway.", ctx)
        logs = list((f / "journal/sessions").glob("*-f1-abcdef12.md"))
        self.assertEqual(len(logs), 1)
        # the agent works and wraps
        (f / "code/alpha/y").write_text("y\n")
        (f / "notes/y.md").write_text("y\n")
        text = logs[0].read_text().replace("goal:\n", "goal: add y\n").replace("outcome: open", "outcome: done")
        logs[0].write_text(text)
        self.cudl("commit", "-A", "-m", "add y", cwd=f)
        t = self.compacted_transcript(sid, 1)  # compacted once on the way
        self.cudl("hook", "session-end", input=json.dumps({"session_id": sid, "cwd": str(f), "transcript_path": str(t)}),
                  cwd=f)
        text = logs[0].read_text()
        self.assertIn("compactions: 1", text)
        self.assertIn("touched: code/alpha, notes/y.md", text)
        self.assertRegex(text, r"repos: alpha=[0-9a-f]{10}\.\.[0-9a-f]{10}")
        self.assertNotIn("needs_summary", text)
        index = (f / "journal/INDEX.md").read_text()
        self.assertIn("f1 · [abcdef12]", index)
        self.assertIn("· claude · done ·", index)
        self.assertIn("done · add y", index)
        self.assertTrue(self.clean(f))
        # a second session sees the first in its context
        out = json.loads(self.cudl("hook", "session-start", input=json.dumps({"session_id": "ffff0000", "cwd": str(f)}), cwd=f).stdout)
        self.assertIn("done · add y", out["hookSpecificOutput"]["additionalContext"])

    def test_session_end_without_wrap_flags_summary(self):
        sid = "12345678-aaaa"
        self.cudl("hook", "session-start", input=json.dumps({"session_id": sid, "cwd": str(self.w)}))
        self.cudl("hook", "session-end", input=json.dumps({"session_id": sid, "cwd": str(self.w)}))
        log = next((self.w / "journal/sessions").glob("*-main-12345678.md"))
        self.assertIn("needs_summary: yes", log.read_text())
        self.assertEqual(self.git(self.w, "log", "-1", "--format=%s"), "journal: session 12345678")

    def test_codex_session_gets_memory_index(self):
        (self.w / "memory/MEMORY.md").write_text("- [Lesson](lesson.md) — always check pins\n")
        self.cudl("commit", "-m", "memory")
        self.cudl("new", "f1")
        f = self.w / "wt/f1"
        inp = json.dumps({"session_id": "c0dec0de-2", "cwd": str(f), "transcript_path": None})
        ctx = json.loads(self.cudl("hook", "session-start", "--agent", "codex", input=inp, cwd=f).stdout)
        ctx = ctx["hookSpecificOutput"]["additionalContext"]
        self.assertIn("always check pins", ctx)
        self.assertIn(f"Shared memory: {self.w / 'memory'}", ctx)
        log = next((f / "journal/sessions").glob("*-f1-c0dec0de.md"))
        self.assertIn("agent: codex", log.read_text())
        claude = json.loads(self.cudl("hook", "session-start", input=json.dumps({"session_id": "c1aude00", "cwd": str(f)}), cwd=f).stdout)
        self.assertNotIn("always check pins", claude["hookSpecificOutput"]["additionalContext"])
        self.cudl("hook", "session-end", "--agent", "codex", input=inp, cwd=f)
        self.assertIn("· codex · open ·", (f / "journal/INDEX.md").read_text())

    def test_codex_started_by_a_claude_session_is_its_subagent(self):
        parent = "aaaaaaaa-1111-2222-3333-444444444444"
        start = json.dumps({"session_id": parent, "cwd": str(self.w)})
        # Claude Code exports the session's own id to everything it runs, the hook included
        self.cudl("hook", "session-start", input=start, env={"CLAUDE_CODE_SESSION_ID": parent})
        rollout = self.root / "rollout.jsonl"
        rollout.write_text(json.dumps({"type": "response_item", "payload": {"type": "message", "role": "user",
                           "content": [{"type": "input_text", "text": "Review notes/draft.md for errors"}]}}) + "\n")
        child = "01a0ffff-0000-7000-8000-000000000001"
        cin = json.dumps({"session_id": child, "cwd": str(self.w / "code/alpha"), "transcript_path": str(rollout)})
        ctx = json.loads(self.cudl("hook", "session-start", "--agent", "codex", input=cin,
                                   env={"CLAUDE_CODE_SESSION_ID": parent}).stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertIn(f"started by session {parent}", ctx)
        self.assertIn("Don't commit", ctx)
        self.assertNotIn("Handoff", ctx)
        head = self.head(self.w)
        self.cudl("hook", "session-end", "--agent", "codex", input=cin, env={"CLAUDE_CODE_SESSION_ID": parent})
        self.assertEqual(self.head(self.w), head)  # the child commits nothing
        log = next((self.w / "journal/sessions").glob("*-main-01a0ffff*.md")).read_text()
        for line in (f"parent: {parent}", "role: subagent", "agent: codex", "goal: Review notes/draft.md for errors"):
            self.assertIn(line, log)
        self.assertNotIn("needs_summary", log)
        # the parent's end commits both, and the index lists the child under its parent
        self.cudl("hook", "session-end", input=start, env={"CLAUDE_CODE_SESSION_ID": parent})
        self.assertTrue(self.clean(self.w))
        index = (self.w / "journal/INDEX.md").read_text().splitlines()
        entries = [l for l in index if "aaaaaaaa" in l or "01a0ffff" in l]
        self.assertTrue(entries[0].startswith("- ") and "aaaaaaaa" in entries[0])
        self.assertTrue(entries[1].startswith("  - ↳ codex [01a0ffff]") and "Review notes/draft.md" in entries[1])
        # a later session's recent list shows the parent, not the child
        later = json.loads(self.cudl("hook", "session-start", input=json.dumps({"session_id": "bbbbbbbb-2", "cwd": str(self.w)}),
                                     env={"CLAUDE_CODE_SESSION_ID": "bbbbbbbb-2"}).stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("aaaaaaaa", later)
        self.assertNotIn("01a0ffff", later)

    def test_parent_outside_the_lab_means_a_normal_session(self):
        cin = json.dumps({"session_id": "cccccccc-1", "cwd": str(self.w)})
        ctx = json.loads(self.cudl("hook", "session-start", "--agent", "codex", input=cin,
                                   env={"CLAUDE_CODE_SESSION_ID": "not-in-this-lab"}).stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("Session log:", ctx)
        log = next((self.w / "journal/sessions").glob("*-main-cccccccc*.md")).read_text()
        self.assertNotIn("parent:", log)

    def test_start_context_is_facts(self):
        ctx = json.loads(self.cudl("hook", "session-start", input=json.dumps(
            {"session_id": "dddd0000-1", "cwd": str(self.w), "source": "compact"})).stdout)
        ctx = ctx["hookSpecificOutput"]["additionalContext"]
        self.assertIn("this session is in the main lab", ctx)
        self.assertIn("Session log: journal/sessions/", ctx)
        for directive in ("build in a feature", "fresh session", "run /wrap", "propose"):
            self.assertNotIn(directive, ctx)

    # --- a helper, not a gatekeeper ---

    def test_session_end_records_moved_pins(self):
        self.cudl("new", "f1", "-r", "alpha")
        f = self.w / "wt/f1"
        sid = "eeee0000-1"
        self.session("session-start", sid, f)
        a = f / "code/alpha"
        (a / "x").write_text("x\n")
        self.git(a, "add", "-A")
        self.git(a, "commit", "-q", "-m", "work, with plain git")
        (f / "notes/draft.md").write_text("not mine to commit\n")
        self.session("session-end", sid, f)
        self.assertEqual(self.pin(f, "code/alpha"), self.head(a))
        self.assertIn("Code: alpha f1", self.git(f, "log", "-1", "--format=%B"))
        self.assertIn(self.head(a), self.keep_refs())
        self.assertIn("notes/draft.md", self.git(f, "status", "--porcelain"))  # left alone
        # opted out: not recorded
        (a / "y").write_text("y\n")
        self.git(a, "add", "-A")
        self.git(a, "commit", "-q", "-m", "more")
        self.session("session-start", "eeee0000-2", f)
        self.cudl("hook", "session-end", input=json.dumps({"session_id": "eeee0000-2", "cwd": str(f)}), cwd=f,
                  env={"CUDL_NO_PIN_RECORDING": "1"})
        self.assertNotEqual(self.pin(f, "code/alpha"), self.head(a))

    def test_a_session_works_across_features(self):
        self.cudl("new", "f1", "-r", "alpha")
        self.cudl("new", "f2", "-r", "beta")
        f1, f2 = self.w / "wt/f1", self.w / "wt/f2"
        (f2 / "handoff/f2.md").write_text("# f2\n\nState: halfway through f2.\n")
        self.cudl("commit", "-m", "f2 handoff", cwd=f2)
        sid = "ffff0000-1"
        self.session("session-start", sid, f1)
        # from f1's directory, work in f2
        (f2 / "code/beta/b").write_text("b\n")
        out = self.cudl("-f", "f2", "commit", "-A", "-m", "work in f2", cwd=f1, env={"CLAUDE_CODE_SESSION_ID": sid}).stdout
        self.assertIn("now also works in f2", out)
        linked = next((f2 / "journal/sessions").glob("*-f2-ffff0000*.md")).read_text()
        self.assertIn("role: continued", linked)
        home = next((f1 / "journal/sessions").glob("*-f1-ffff0000*.md"))
        self.assertIn("features: f2", home.read_text())
        self.assertIn("last_feature: f2", home.read_text())
        # after a restart the context shows where the work is now
        ctx = json.loads(self.session("session-start", sid, f1, source="resume").stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("also worked in: f2", ctx)
        self.assertIn("halfway through f2", ctx)
        # looking around doesn't move the session
        self.cudl("-f", "f1", "status", cwd=f2, env={"CLAUDE_CODE_SESSION_ID": "someone-else"})
        self.assertEqual(list((f1 / "journal/sessions").glob("*someone*")), [])
        # the end closes both logs, each committed in its own worktree
        self.session("session-end", sid, f1)
        self.assertTrue(self.clean(f1))
        self.assertIn("ffff0000", self.git(f2, "log", "-1", "--format=%s"))
        self.assertIn("ended:", next((f2 / "journal/sessions").glob("*-f2-ffff0000*.md")).read_text())

    def test_stacked_feature_does_not_reuse_the_parents_log(self):
        self.cudl("new", "p", "-r", "alpha")
        pt = self.w / "wt/p"
        sid = "abab0000-1"
        self.session("session-start", sid, pt)
        self.cudl("commit", "-m", "log", cwd=pt)
        self.cudl("new", "c", "--from", "p")
        ct = self.w / "wt/c"
        self.assertTrue(list((ct / "journal/sessions").glob("*-p-abab0000*.md")))  # inherited copy
        self.session("session-start", sid, ct)
        self.assertTrue(list((ct / "journal/sessions").glob("*-c-abab0000*.md")))
        inherited = next((ct / "journal/sessions").glob("*-p-abab0000*.md")).read_text()
        self.session("session-end", sid, ct)
        self.assertEqual(next((ct / "journal/sessions").glob("*-p-abab0000*.md")).read_text(), inherited)

    # --- a helper, not a gatekeeper: the code review ---

    def test_a_session_moving_back_and_forth(self):
        for n in ("a", "b", "c"):
            self.cudl("new", n)
        sid = "12120000-1"
        env = {"CLAUDE_CODE_SESSION_ID": sid}
        self.session("session-start", sid, self.w / "wt/a")
        for n in ("b", "c", "b"):
            (self.w / f"wt/{n}/notes/{n}.md").write_text(n + "\n")
            self.cudl("-f", n, "commit", "-m", n, env=env)
        home = next((self.w / "wt/a/journal/sessions").glob("*-a-12120000*.md"))
        self.assertIn("last_feature: b", home.read_text())
        self.assertIn("features: b, c", home.read_text())
        (self.w / "wt/a/notes/a.md").write_text("a\n")
        self.cudl("-f", "a", "commit", "-m", "a", env=env)
        self.assertIn("last_feature: a", home.read_text())

    def test_new_associates_the_creating_session(self):
        self.cudl("new", "f1")
        sid = "34340000-1"
        env = {"CLAUDE_CODE_SESSION_ID": sid}
        self.session("session-start", sid, self.w / "wt/f1")
        self.cudl("new", "f2", "-r", "alpha", cwd=self.w / "wt/f1", env=env)
        f2 = self.w / "wt/f2"
        self.assertTrue(list((f2 / "journal/sessions").glob("*-f2-34340000*.md")))
        a = f2 / "code/alpha"
        (a / "x").write_text("x\n")
        self.git(a, "add", "-A")
        self.git(a, "commit", "-q", "-m", "plain git in the new feature")
        self.session("session-end", sid, self.w / "wt/f1")
        self.assertEqual(self.pin(f2, "code/alpha"), self.head(a))  # recorded in f2 by f1's session end

    def test_session_ending_or_resuming_elsewhere(self):
        self.cudl("new", "f1")
        self.cudl("new", "f2")
        f1, f2 = self.w / "wt/f1", self.w / "wt/f2"
        sid = "56560000-1"
        env = {"CLAUDE_CODE_SESSION_ID": sid}
        self.session("session-start", sid, f1)
        # resumed in f2: still one session, linked there, not a second home
        self.session("session-start", sid, f2, source="resume")
        linked = next((f2 / "journal/sessions").glob("*-f2-56560000*.md")).read_text()
        self.assertIn("role: continued", linked)
        # work in the main lab too, then end from f2
        (self.w / "notes/m.md").write_text("m\n")
        self.cudl("-f", "main", "commit", "-m", "main note", cwd=f2, env=env)
        self.session("session-end", sid, f2)
        for top in (f1, f2, self.w):
            self.assertTrue(self.clean(top), top)
            log = next((top / "journal/sessions").glob("*-56560000*.md")).read_text()
            self.assertIn("ended:", log)

    def test_session_end_during_a_lab_merge(self):
        self.cudl("new", "f1")
        f = self.w / "wt/f1"
        (f / "notes/x.md").write_text("feature\n")
        self.cudl("commit", "-m", "x", cwd=f)
        (self.w / "notes/x.md").write_text("main\n")
        self.cudl("commit", "-m", "x on main")
        self.sh("git", "merge", "main", cwd=f, check=False)  # conflicts: merge in progress
        sid = "78780000-1"
        self.session("session-start", sid, f)
        p = self.session("session-end", sid, f)
        self.assertIn("a merge in progress", p.stderr)
        self.assertIn("ended:", next((f / "journal/sessions").glob("*-f1-78780000*.md")).read_text())

    # --- a helper, not a gatekeeper: the review's second pass ---

    def test_a_subagent_resumed_elsewhere_stays_a_subagent(self):
        self.cudl("new", "f1")
        self.cudl("new", "f2")
        parent, child = "90900000-p", "91910000-c"
        self.session("session-start", parent, self.w / "wt/f1")
        self.cudl("hook", "session-start", "--agent", "codex", input=json.dumps({"session_id": child, "cwd": str(self.w / "wt/f1")}),
                  env={"CLAUDE_CODE_SESSION_ID": parent})
        f2 = self.w / "wt/f2"
        head = self.head(f2)
        out = json.loads(self.cudl("hook", "session-start", "--agent", "codex", input=json.dumps(
            {"session_id": child, "cwd": str(f2), "source": "resume"}), cwd=f2, env={"CLAUDE_CODE_SESSION_ID": parent}).stdout)
        self.assertIn("subagent", out["hookSpecificOutput"]["additionalContext"])
        self.cudl("hook", "session-end", "--agent", "codex", input=json.dumps({"session_id": child, "cwd": str(f2)}),
                  cwd=f2, env={"CLAUDE_CODE_SESSION_ID": parent})
        self.assertEqual(self.head(f2), head)  # it committed nothing

    def test_session_end_closes_logs_where_they_are(self):
        # after `claude -w f1` exits, its end runs in the main lab (Claude Code moves the session back):
        # its log in f1 is closed there, and the place where it merely ended gets none
        self.cudl("new", "f1")
        self.cudl("new", "f2")
        sid = "92920000-1"
        f1 = self.w / "wt/f1"
        self.session("session-start", sid, f1)
        for top in (self.w, self.w / "wt/f2"):
            self.session("session-end", sid, top)
            self.assertEqual(list((top / "journal/sessions").glob("*92920000*.md")), [])
        self.assertIn("ended:", next((f1 / "journal/sessions").glob("*-f1-92920000*.md")).read_text())
        self.assertIn("92920000", self.git(f1, "ls-files", "journal/sessions"))
        self.assertNotIn("92920000", (self.w / "journal/INDEX.md").read_text())
        self.assertTrue(self.clean(self.w))

    def test_home_found_after_its_feature_is_finished(self):
        self.cudl("new", "f1")
        f1 = self.w / "wt/f1"
        sid = "93930000-1"
        self.session("session-start", sid, f1)
        self.session("session-end", sid, f1)
        self.cudl("finish", "f1")
        self.session("session-start", sid, self.w, source="resume")
        logs = [read_fm(p.read_text()) for p in (self.w / "journal/sessions").glob("*93930000*.md")]
        # the finished feature's log stays its home; its new work in main gets a linked log
        self.assertEqual(sorted((fm.get("feature"), fm.get("role", "")) for fm in logs),
                         [("f1", ""), ("main", "continued")])

    # --- the live checks' fixes ---

    def test_session_end_commits_memory_and_plans(self):
        sid = "5e55e5e5-0001"
        self.session("session-start", sid, self.w)
        (self.w / "memory/fact.md").write_text("---\nname: fact\n---\nA fact.\n")
        (self.w / "memory/MEMORY.md").write_text("- [Fact](fact.md) — a fact\n")
        (self.w / "plans/p.md").write_text("# a plan\n")
        (self.w / "notes/draft.md").write_text("mine to commit\n")
        self.session("session-end", sid, self.w)
        tracked = self.git(self.w, "ls-files", "memory", "plans").splitlines()
        for path in ("memory/fact.md", "memory/MEMORY.md", "plans/p.md"):
            self.assertIn(path, tracked)
        self.assertIn("; memory, plans", self.git(self.w, "log", "-1", "--format=%s"))
        self.assertEqual(self.git(self.w, "status", "--porcelain"), "?? notes/draft.md")  # notes: the agent's call
        # staged, then changed again: neither version is cudl's to pick
        (self.w / "plans/p.md").write_text("# a plan, staged\n")
        self.git(self.w, "add", "plans/p.md")
        (self.w / "plans/p.md").write_text("# a plan, changed after staging\n")
        sid = "5e55e5e5-0004"
        self.session("session-start", sid, self.w)
        p = self.session("session-end", sid, self.w)
        self.assertIn("left alone, staged and then changed again: plans/p.md", p.stderr)
        self.assertEqual(self.sh("git", "show", ":plans/p.md").stdout, "# a plan, staged\n")
        self.assertIn("MM plans/p.md", self.git(self.w, "status", "--porcelain"))
        self.git(self.w, "add", "plans/p.md")
        self.git(self.w, "commit", "-q", "-m", "the plan, by hand")
        # a session in a feature: its plans on the feature's branch, the shared memory on main
        self.cudl("new", "f1")
        f = self.w / "wt/f1"
        sid = "5e55e5e5-0002"
        self.session("session-start", sid, f)
        (self.w / "memory/MEMORY.md").write_text("- [Fact](fact.md) — a fact, revised\n")
        (f / "plans/q.md").write_text("# another plan\n")
        self.session("session-end", sid, f)
        self.assertTrue(self.clean(f))
        self.assertIn("plans/q.md", self.git(f, "ls-files", "plans"))
        self.assertEqual(self.git(self.w, "status", "--porcelain"), "?? notes/draft.md")
        self.assertEqual(self.git(self.w, "log", "-1", "--format=%s"), "memory: from session 5e55e5e5 (f1)")
        # main busy (a merge stopped there): the memory waits, nothing fails
        sid = "5e55e5e5-0003"
        self.session("session-start", sid, f)
        (self.w / "memory/MEMORY.md").write_text("- [Fact](fact.md) — revised again\n")
        merge_head = Path(self.git(self.w, "rev-parse", "--path-format=absolute", "--git-dir")) / "MERGE_HEAD"
        merge_head.write_text(self.head(self.w) + "\n")
        self.session("session-end", sid, f)
        merge_head.unlink()
        self.assertIn(" M memory/MEMORY.md", self.sh("git", "status", "--porcelain").stdout)

    def test_claude_started_by_a_claude_session_is_its_subagent(self):
        parent = "aaaa1111-0000-4000-8000-000000000001"
        child = "bbbb2222-0000-4000-8000-000000000002"
        env_file = self.root / "session-env.sh"
        self.cudl("hook", "session-start", input=json.dumps({"session_id": parent, "cwd": str(self.w)}),
                  env={"CLAUDE_CODE_SESSION_ID": parent, "CLAUDE_ENV_FILE": str(env_file)})
        self.assertEqual(env_file.read_text(), f"export CUDL_SESSION_ID={parent}\n")
        # the parent's commands carry CUDL_SESSION_ID; Claude Code gives the child its own CLAUDE_CODE_SESSION_ID
        out = json.loads(self.cudl("hook", "session-start", input=json.dumps({"session_id": child, "cwd": str(self.w)}),
                                   env={"CLAUDE_CODE_SESSION_ID": child, "CUDL_SESSION_ID": parent}).stdout)
        self.assertIn(f"started by session {parent}", out["hookSpecificOutput"]["additionalContext"])
        log = next((self.w / "journal/sessions").glob("*-main-bbbb2222*.md")).read_text()
        self.assertIn(f"parent: {parent}", log)
        self.assertIn("role: subagent", log)

    def test_the_nearest_session_is_the_parent(self):
        # Claude A → Codex C → Claude B: B sees A in CUDL_SESSION_ID and C in CODEX_THREAD_ID
        a, c, b = "aaaa0000-0000-4000-8000-00000000000a", "01a0cccc-0000-7000-8000-00000000000c", \
            "bbbb0000-0000-4000-8000-00000000000b"
        self.start(a, CLAUDE_CODE_SESSION_ID=a)
        self.start(c, agent="codex", CLAUDE_CODE_SESSION_ID=a, CUDL_SESSION_ID=a, CODEX_THREAD_ID=c)
        self.start(b, CLAUDE_CODE_SESSION_ID=b, CUDL_SESSION_ID=a, CODEX_THREAD_ID=c)
        self.assertEqual(self.logged(c)["parent"], a)
        self.assertEqual(self.logged(b)["parent"], c)
        # Codex X → Claude Y → Claude Z: Z sees X in CODEX_THREAD_ID and Y in CUDL_SESSION_ID
        x, y, z = "01a0eeee-0000-7000-8000-0000000000e1", "eeee0000-0000-4000-8000-0000000000e2", \
            "eeee1111-0000-4000-8000-0000000000e3"
        self.start(x, agent="codex", CODEX_THREAD_ID=x)
        self.start(y, CLAUDE_CODE_SESSION_ID=y, CODEX_THREAD_ID=x)
        self.start(z, CLAUDE_CODE_SESSION_ID=z, CODEX_THREAD_ID=x, CUDL_SESSION_ID=y)
        self.assertEqual(self.logged(y)["parent"], x)
        self.assertEqual(self.logged(z)["parent"], y)
        # A's end commits its whole tree, and the index nests it
        for sid, agent, env in ((b, "claude", {"CLAUDE_CODE_SESSION_ID": b, "CUDL_SESSION_ID": a, "CODEX_THREAD_ID": c}),
                                (c, "codex", {"CLAUDE_CODE_SESSION_ID": a, "CODEX_THREAD_ID": c}),
                                (a, "claude", {"CLAUDE_CODE_SESSION_ID": a})):
            args = ["hook", "session-end"] + (["--agent", agent] if agent != "claude" else [])
            self.cudl(*args, input=json.dumps({"session_id": sid, "cwd": str(self.w)}), env=env)
        tracked = self.git(self.w, "ls-files", "journal/sessions")
        for sid in (a, c, b):
            self.assertIn(sid[:8], tracked)
        index = (self.w / "journal/INDEX.md").read_text().splitlines()
        i = next(n for n, l in enumerate(index) if l.startswith("- ") and "[aaaa0000]" in l)
        self.assertTrue(index[i + 1].startswith("  - ↳ codex [01a0cccc"))
        self.assertTrue(index[i + 2].startswith("    - ↳ claude [bbbb0000]"))

    def test_commands_in_codex_started_by_claude_are_codex_s(self):
        a, c = "aaaa2222-0000-4000-8000-00000000000a", "01a0c2c2-0000-7000-8000-00000000000c"
        self.cudl("new", "f1", "-r", "alpha")
        self.start(a, CLAUDE_CODE_SESSION_ID=a)
        self.start(c, agent="codex", CLAUDE_CODE_SESSION_ID=a, CUDL_SESSION_ID=a, CODEX_THREAD_ID=c)
        f = self.w / "wt/f1"
        (f / "code/alpha/x").write_text("x\n")
        self.cudl("commit", "-A", "-f", "f1", "-m", "x",
                  env={"CLAUDE_CODE_SESSION_ID": a, "CUDL_SESSION_ID": a, "CODEX_THREAD_ID": c})
        logs = [read_fm(p.read_text()) for p in (f / "journal/sessions").glob("*.md")]
        self.assertEqual([(fm["session"], fm.get("role")) for fm in logs], [(c, "continued")])

    def test_a_session_that_did_nothing_leaves_no_log(self):
        head = self.head(self.w)
        never = self.root / "never-written.jsonl"
        bookkeeping = self.root / "bookkeeping.jsonl"
        bookkeeping.write_text(json.dumps({"type": "queue-operation"}) + "\n" + json.dumps({"type": "attachment"}) + "\n")
        for sid, transcript in (("0e0e0e0e-1", never), ("0e0e0e0f-2", bookkeeping)):
            self.session("session-start", sid, self.w, transcript_path=str(transcript))
            self.session("session-end", sid, self.w, transcript_path=str(transcript))
            self.assertFalse(list((self.w / "journal/sessions").glob(f"*{sid[:8]}*")))
        self.assertEqual(self.head(self.w), head)
        self.assertTrue(self.clean(self.w))
        # Codex: a rollout with only its instructions, no prompt and no answer
        rollout = self.root / "rollout.jsonl"
        rollout.write_text(json.dumps({"type": "response_item", "payload": {"type": "message", "role": "user",
                           "content": [{"type": "input_text", "text": "# AGENTS.md instructions for /x"}]}}) + "\n")
        inp = json.dumps({"session_id": "01a0dddd-1", "cwd": str(self.w), "transcript_path": str(rollout)})
        self.cudl("hook", "session-start", "--agent", "codex", input=inp)
        self.cudl("hook", "session-end", "--agent", "codex", input=inp)
        self.assertEqual(self.head(self.w), head)
        self.assertTrue(self.clean(self.w))
        # no transcript, but the session did something (Claude can run without saving one): logged
        sid = "0e0e0e10-3"
        self.session("session-start", sid, self.w, transcript_path=str(never))
        a = self.w / "code/alpha"
        (a / "y").write_text("y\n")
        self.git(a, "add", "-A")
        self.git(a, "commit", "-q", "-m", "y")
        self.session("session-end", sid, self.w, transcript_path=str(never))
        self.assertEqual(self.git(self.w, "log", "-1", "--format=%s"), "journal: session 0e0e0e10; pins alpha")
        # a log someone wrote in: kept
        sid = "0e0e0e11-4"
        self.session("session-start", sid, self.w, transcript_path=str(never))
        log = next((self.w / "journal/sessions").glob("*-0e0e0e11.md"))
        log.write_text(log.read_text().replace("goal:", "goal: look around"))
        self.session("session-end", sid, self.w, transcript_path=str(never))
        self.assertEqual(self.git(self.w, "log", "-1", "--format=%s"), "journal: session 0e0e0e11")
        # a conversation: logged as before
        talk = self.root / "talk.jsonl"
        talk.write_text(json.dumps({"type": "user", "message": {"role": "user", "content": "add a note"}}) + "\n")
        self.session("session-start", "0e0e0e12-5", self.w, transcript_path=str(talk))
        self.session("session-end", "0e0e0e12-5", self.w, transcript_path=str(talk))
        self.assertEqual(self.git(self.w, "log", "-1", "--format=%s"), "journal: session 0e0e0e12")

    # --- review 1 of the live-check fixes ---

    def test_git_rm_cached_is_left_alone(self):
        (self.w / "memory/x.md").write_text("x\n")
        self.cudl("commit", "-m", "x")
        self.git(self.w, "rm", "-q", "--cached", "memory/x.md")  # stop tracking it, keep the file
        sid = "9a9a9a9a-1"
        self.session("session-start", sid, self.w)
        p = self.session("session-end", sid, self.w)
        self.assertIn("left alone, staged and then changed again: memory/x.md", p.stderr)
        status = self.sh("git", "status", "--porcelain").stdout
        self.assertIn("D  memory/x.md", status)
        self.assertIn("?? memory/x.md", status)
        # a finish names it rather than committing the file back
        self.cudl("new", "f1", "-r", "alpha")
        self.work(self.w / "wt/f1", "alpha", "y", "y\n", "y")
        head = self.head(self.w)
        p = self.cudl("finish", "f1", check=False)
        self.assertIn("would take along: memory/x.md", p.stderr)
        self.assertEqual(self.head(self.w), head)

    def test_paths_are_literal(self):
        (self.w / "plans/p*.md").write_text("star\n")
        (self.w / "plans/p1.md").write_text("one\n")
        self.cudl("commit", "-m", "plans")
        (self.w / "plans/p*.md").write_text("star, changed\n")
        (self.w / "plans/p1.md").write_text("one, staged\n")
        self.git(self.w, "add", "plans/p1.md")
        (self.w / "plans/p1.md").write_text("one, changed after staging\n")
        sid = "9a9a9a9b-1"
        self.session("session-start", sid, self.w)
        self.session("session-end", sid, self.w)
        self.assertEqual(self.sh("git", "show", "HEAD:plans/p*.md").stdout, "star, changed\n")
        self.assertEqual(self.sh("git", "show", ":plans/p1.md").stdout, "one, staged\n")  # the staged version kept
        self.assertEqual(self.sh("git", "show", "HEAD:plans/p1.md").stdout, "one\n")

    def test_a_split_descendant_log_is_left_alone(self):
        a, b = "aaaa3333-0000-4000-8000-00000000000a", "bbbb3333-0000-4000-8000-00000000000b"
        self.start(a, CLAUDE_CODE_SESSION_ID=a)
        self.start(b, CLAUDE_CODE_SESSION_ID=b, CUDL_SESSION_ID=a)
        log = next((self.w / "journal/sessions").glob("*-bbbb3333.md"))
        self.git(self.w, "add", str(log))
        log.write_text(log.read_text() + "edited after staging\n")
        p = self.cudl("hook", "session-end", input=json.dumps({"session_id": a, "cwd": str(self.w)}),
                      env={"CLAUDE_CODE_SESSION_ID": a})
        self.assertIn(f"left alone, staged and then changed again: journal/sessions/{log.name}", p.stderr)
        self.assertNotIn("edited after staging", self.sh("git", "show", f":journal/sessions/{log.name}").stdout)
        self.assertIn("aaaa3333", self.git(self.w, "ls-files", "journal/sessions"))

    def test_subagent_logs_elsewhere_are_committed_by_the_parent(self):
        self.cudl("new", "f1", "-r", "alpha")
        self.cudl("new", "f2")
        f1, f2 = self.w / "wt/f1", self.w / "wt/f2"
        a, c, b = "aaaa4444-0000-4000-8000-00000000000a", "01a04444-0000-7000-8000-00000000000c", \
            "bbbb4444-0000-4000-8000-00000000000b"
        self.start(a, CLAUDE_CODE_SESSION_ID=a)
        # Codex C, A's subagent, commits a note in f1: its linked log is there
        self.start(c, agent="codex", CLAUDE_CODE_SESSION_ID=a, CUDL_SESSION_ID=a, CODEX_THREAD_ID=c)
        (f1 / "notes/n.md").write_text("n\n")
        self.cudl("commit", "-f", "f1", "--lab", "notes/n.md", "-m", "n",
                  env={"CLAUDE_CODE_SESSION_ID": a, "CUDL_SESSION_ID": a, "CODEX_THREAD_ID": c})
        self.assertTrue(list((f1 / "journal/sessions").glob("*-01a04444*.md")))
        # Claude B, A's subagent too, started in f2
        self.start(b, f2, CLAUDE_CODE_SESSION_ID=b, CUDL_SESSION_ID=a)
        self.assertEqual(self.logged(b, f2)["parent"], a)
        for sid, top, agent, env in ((b, f2, "claude", {"CLAUDE_CODE_SESSION_ID": b, "CUDL_SESSION_ID": a}),
                                     (c, self.w, "codex", {"CLAUDE_CODE_SESSION_ID": a, "CODEX_THREAD_ID": c}),
                                     (a, self.w, "claude", {"CLAUDE_CODE_SESSION_ID": a})):
            args = ["hook", "session-end"] + (["--agent", agent] if agent != "claude" else [])
            self.cudl(*args, input=json.dumps({"session_id": sid, "cwd": str(top)}), env=env)
        for top in (self.w, f1, f2):
            self.assertTrue(self.clean(top), top)
        self.cudl("sync", "-f", "f1")
        self.cudl("sync", "-f", "f2")

    def test_idle_session_checks(self):
        never = str(self.root / "never-written.jsonl")
        # uncommitted code: it did something
        sid = "1d1e0000-1"
        self.session("session-start", sid, self.w, transcript_path=never)
        (self.w / "code/alpha/README").write_text("edited\n")
        self.session("session-end", sid, self.w, transcript_path=never)
        self.assertIn("1d1e0000", self.git(self.w, "ls-files", "journal/sessions"))
        self.git(self.w / "code/alpha", "checkout", "--", "README")
        # a feature session that only changed the shared memory: it did something
        self.cudl("new", "f1")
        f = self.w / "wt/f1"
        sid = "1d1e0001-2"
        self.session("session-start", sid, f, transcript_path=never)
        (self.w / "memory/m.md").write_text("m\n")
        self.session("session-end", sid, f, transcript_path=never)
        self.assertIn("1d1e0001", self.git(f, "ls-files", "journal/sessions"))
        self.assertIn("memory/m.md", self.git(self.w, "ls-files", "memory"))
        # a child that never got going, while its parent runs here with an uncommitted log: dropped
        a, b = "aaaa5555-0000-4000-8000-00000000000a", "bbbb5555-0000-4000-8000-00000000000b"
        self.start(a, CLAUDE_CODE_SESSION_ID=a)
        inp = json.dumps({"session_id": b, "cwd": str(self.w), "transcript_path": never})
        env = {"CLAUDE_CODE_SESSION_ID": b, "CUDL_SESSION_ID": a}
        self.cudl("hook", "session-start", input=inp, env=env)
        self.cudl("hook", "session-end", input=inp, env=env)
        self.assertFalse(list((self.w / "journal/sessions").glob("*-bbbb5555.md")))
        # a transcript that can't be read: the end goes on as usual
        unreadable = self.root / "unreadable.jsonl"
        unreadable.write_text("{}\n")
        unreadable.chmod(0)
        sid = "1d1e0002-3"
        self.session("session-start", sid, self.w, transcript_path=str(unreadable))
        self.session("session-end", sid, self.w, transcript_path=str(unreadable))
        self.assertIn("ended:", next((self.w / "journal/sessions").glob("*-1d1e0002.md")).read_text())
        self.assertIn("1d1e0002", self.git(self.w, "ls-files", "journal/sessions"))

    def test_the_session_running_a_command_without_a_log_here(self):
        # a logged Codex X started Claude A, which has no log in this lab: A's command is A's
        x, a = "01a06666-0000-7000-8000-0000000000e1", "aaaa6666-0000-4000-8000-00000000000a"
        self.start(x, agent="codex", CODEX_THREAD_ID=x)
        self.cudl("feedback", "from A", env={"CLAUDE_CODE_SESSION_ID": a, "CODEX_THREAD_ID": x})
        self.assertIn(f"session {a}", next((self.w / "notes/cudl-feedback").glob("*-from-a.md")).read_text())
        # an unlogged Claude Z started Codex C, which is logged here: C's command is C's
        z, c = "dddd6666-0000-4000-8000-0000000000d1", "01a07777-0000-7000-8000-0000000000c1"
        self.start(c, agent="codex", CLAUDE_CODE_SESSION_ID=z, CODEX_THREAD_ID=c)
        self.assertEqual(self.logged(c).get("outer"), z)
        self.cudl("feedback", "from C", env={"CLAUDE_CODE_SESSION_ID": z, "CODEX_THREAD_ID": c})
        self.assertIn(f"session {c}", next((self.w / "notes/cudl-feedback").glob("*-from-c.md")).read_text())

    # --- review 2 of the live-check fixes ---

    def test_a_parent_with_children_is_never_idle(self):
        never = str(self.root / "never-written.jsonl")
        talk = self.root / "talk.jsonl"
        talk.write_text(json.dumps({"type": "user", "message": {"role": "user", "content": "review x"}}) + "\n")
        a, b = "aaaa7777-0000-4000-8000-00000000000a", "bbbb7777-0000-4000-8000-00000000000b"
        self.cudl("hook", "session-start", input=json.dumps({"session_id": a, "cwd": str(self.w), "transcript_path": never}),
                  env={"CLAUDE_CODE_SESSION_ID": a})
        child = json.dumps({"session_id": b, "cwd": str(self.w), "transcript_path": str(talk)})
        self.cudl("hook", "session-start", input=child, env={"CLAUDE_CODE_SESSION_ID": b, "CUDL_SESSION_ID": a})
        self.cudl("hook", "session-end", input=child, env={"CLAUDE_CODE_SESSION_ID": b, "CUDL_SESSION_ID": a})
        self.cudl("hook", "session-end", input=json.dumps({"session_id": a, "cwd": str(self.w), "transcript_path": never}),
                  env={"CLAUDE_CODE_SESSION_ID": a})
        tracked = self.git(self.w, "ls-files", "journal/sessions")
        self.assertIn("aaaa7777", tracked)
        self.assertIn("bbbb7777", tracked)

    def test_a_split_kid_isnt_linked_from_the_committed_index(self):
        a, b = "aaaa8888-0000-4000-8000-00000000000a", "bbbb8888-0000-4000-8000-00000000000b"
        self.start(a, CLAUDE_CODE_SESSION_ID=a)
        self.start(b, CLAUDE_CODE_SESSION_ID=b, CUDL_SESSION_ID=a)
        log = next((self.w / "journal/sessions").glob("*-bbbb8888.md"))
        self.git(self.w, "add", str(log))
        log.write_text(log.read_text() + "edited after staging\n")
        self.cudl("hook", "session-end", input=json.dumps({"session_id": a, "cwd": str(self.w)}),
                  env={"CLAUDE_CODE_SESSION_ID": a})
        index = self.sh("git", "show", "HEAD:journal/INDEX.md").stdout
        self.assertIn("aaaa8888", index)
        self.assertNotIn("bbbb8888", index)

    def test_unreadable_files_dont_stop_a_session_end(self):
        # a subagent whose transcript can't be read still closes its log
        a, b = "aaaa9999-0000-4000-8000-00000000000a", "bbbb9999-0000-4000-8000-00000000000b"
        self.start(a, CLAUDE_CODE_SESSION_ID=a)
        unreadable = self.root / "unreadable.jsonl"
        unreadable.write_text("{}\n")
        unreadable.chmod(0)
        child = json.dumps({"session_id": b, "cwd": str(self.w), "transcript_path": str(unreadable)})
        self.cudl("hook", "session-start", input=child, env={"CLAUDE_CODE_SESSION_ID": b, "CUDL_SESSION_ID": a})
        self.cudl("hook", "session-end", input=child, env={"CLAUDE_CODE_SESSION_ID": b, "CUDL_SESSION_ID": a})
        self.assertIn("ended:", next((self.w / "journal/sessions").glob("*-bbbb9999.md")).read_text())
        # a log elsewhere in the lab that can't be read doesn't stop the parent's end
        self.cudl("new", "f1")
        stray = self.w / "wt/f1/journal/sessions/2026-01-01-f1-stray.md"
        stray.write_text("---\nsession: stray\n---\n")
        stray.chmod(0)
        self.cudl("hook", "session-end", input=json.dumps({"session_id": a, "cwd": str(self.w)}),
                  env={"CLAUDE_CODE_SESSION_ID": a})
        stray.chmod(0o644)
        self.assertIn("aaaa9999", self.git(self.w, "ls-files", "journal/sessions"))

    def test_a_failed_commit_then_the_session_end(self):
        sid = "faaaaaaa-1"
        self.session("session-start", sid, self.w)
        (self.w / "notes/n.md").write_text("n\n")
        self.git(self.w, "add", "-A")  # what a `cudl commit` that failed (signing) leaves staged
        self.session("session-end", sid, self.w)
        self.assertIn("faaaaaaa", self.git(self.w, "ls-files", "--with-tree=HEAD", "journal/sessions"))
        self.assertIn("faaaaaaa", self.sh("git", "show", "HEAD:journal/INDEX.md").stdout)
        self.assertNotIn("notes/n.md", self.git(self.w, "ls-tree", "-r", "--name-only", "HEAD", "notes"))
        self.assertEqual(self.git(self.w, "diff", "--cached", "--name-only"), "notes/n.md")  # still staged

    def test_a_subagent_s_plans_elsewhere_are_committed(self):
        self.cudl("new", "f2")
        f2 = self.w / "wt/f2"
        a, b = "aaaabbbb-0000-4000-8000-00000000000a", "bbbbaaaa-0000-4000-8000-00000000000b"
        self.start(a, CLAUDE_CODE_SESSION_ID=a)
        self.start(b, f2, CLAUDE_CODE_SESSION_ID=b, CUDL_SESSION_ID=a)
        (f2 / "plans/kid.md").write_text("# the kid's plan\n")
        self.cudl("hook", "session-end", input=json.dumps({"session_id": b, "cwd": str(f2)}),
                  env={"CLAUDE_CODE_SESSION_ID": b, "CUDL_SESSION_ID": a})
        self.cudl("hook", "session-end", input=json.dumps({"session_id": a, "cwd": str(self.w)}),
                  env={"CLAUDE_CODE_SESSION_ID": a})
        self.assertTrue(self.clean(f2))
        self.assertIn("plans/kid.md", self.git(f2, "ls-files", "plans"))

    # --- review 3 of the live-check fixes ---

    def test_cudls_own_writes_keep_a_staged_log_staged(self):
        sid = "5a5a5a5a-1"
        self.session("session-start", sid, self.w)
        (self.w / "notes/n.md").write_text("n\n")
        self.git(self.w, "add", "-A")  # a `cudl commit` that failed on signing
        log = next((self.w / "journal/sessions").glob("*-5a5a5a5a.md"))
        rel = f"journal/sessions/{log.name}"
        # the outage goes on: the end's commit fails too
        flag = self.root / "fail"
        self.fail_commits(self.w, flag)
        self.session("session-end", sid, self.w)
        self.assertIn(f"A  {rel}", self.sh("git", "status", "--porcelain").stdout)  # not AM
        self.session("session-start", sid, self.w, source="resume")  # drops the failed end's `ended:`
        self.assertIn(f"A  {rel}", self.sh("git", "status", "--porcelain").stdout)
        # signing is back: the next end commits it
        flag.unlink()
        self.session("session-end", sid, self.w)
        self.assertIn(rel, self.git(self.w, "ls-tree", "-r", "--name-only", "HEAD", "journal/sessions"))
        self.assertEqual(self.git(self.w, "diff", "--cached", "--name-only"), "notes/n.md")

    def test_a_kids_staged_log_is_committed_by_its_parent(self):
        self.cudl("new", "f1")
        f1 = self.w / "wt/f1"
        a, c = "aaaacccc-0000-4000-8000-00000000000a", "01a0cccd-0000-7000-8000-00000000000c"
        self.start(a, CLAUDE_CODE_SESSION_ID=a)
        env = {"CLAUDE_CODE_SESSION_ID": a, "CUDL_SESSION_ID": a, "CODEX_THREAD_ID": c}
        self.start(c, f1, agent="codex", **env)
        self.git(f1, "add", "-A")  # its `cudl commit` failed
        self.cudl("hook", "session-end", "--agent", "codex", input=json.dumps({"session_id": c, "cwd": str(f1)}), env=env)
        self.cudl("hook", "session-end", input=json.dumps({"session_id": a, "cwd": str(self.w)}),
                  env={"CLAUDE_CODE_SESSION_ID": a})
        self.assertTrue(self.clean(f1))
        self.cudl("sync", "-f", "f1")

    def test_the_committed_index_reads_other_logs_from_head(self):
        sid = "e1e1e1e1-1"
        self.session("session-start", sid, self.w)
        self.session("session-end", sid, self.w)
        other = next((self.w / "journal/sessions").glob("*-e1e1e1e1.md"))
        gone = "e2e2e2e2-2"
        self.session("session-start", gone, self.w)
        self.session("session-end", gone, self.w)
        # changed or deleted here, but not part of the next session's commit
        other.write_text(other.read_text().replace("goal:", "goal: rewritten locally"))
        next((self.w / "journal/sessions").glob("*-e2e2e2e2.md")).unlink()
        sid = "e3e3e3e3-3"
        self.session("session-start", sid, self.w)
        self.session("session-end", sid, self.w)
        index = self.sh("git", "show", "HEAD:journal/INDEX.md").stdout
        self.assertNotIn("rewritten locally", index)
        self.assertIn("e2e2e2e2", index)  # still in HEAD, so still listed
        self.assertIn("e3e3e3e3", index)

    def test_unreadable_logs_are_skipped_by_every_reader(self):
        sid = "a7a7a7a7-1"
        self.session("session-start", sid, self.w)
        clash = self.w / "journal/sessions/2026-01-01-main-a7a7a7a7x.md"  # same id prefix, unreadable
        clash.write_text("---\nsession: other\n---\n")
        clash.chmod(0)
        self.cudl("index")
        p = self.session("session-end", sid, self.w)
        clash.chmod(0o644)
        self.assertIn("a7a7a7a7", self.git(self.w, "ls-tree", "-r", "--name-only", "HEAD", "journal/sessions"))
        self.assertNotIn("Traceback", p.stderr)

    def test_log_writes_leave_no_temporary_files(self):
        sid = "7e7e7e7e-1"
        self.session("session-start", sid, self.w)
        self.session("session-end", sid, self.w)
        gitdir = Path(self.git(self.w, "rev-parse", "--absolute-git-dir"))
        self.assertFalse([p for p in (self.w / "journal/sessions").iterdir() if p.name.endswith(".tmp")])
        self.assertFalse(list(gitdir.glob("cudl-*.tmp")))

    # --- review 4 of the live-check fixes ---

    def test_a_non_utf8_log_in_head_doesnt_stop_a_session_end(self):
        sid = "e9e9e9e9-1"
        self.session("session-start", sid, self.w)
        self.session("session-end", sid, self.w)
        log = next((self.w / "journal/sessions").glob("*-e9e9e9e9.md"))
        log.write_bytes(log.read_bytes() + b"caf\xe9\n")  # committed with a byte that isn't UTF-8
        self.git(self.w, "add", str(log))
        self.git(self.w, "commit", "-q", "-m", "latin-1 in a log")
        log.write_bytes(log.read_bytes() + b"more\n")  # changed here, not part of the next commit
        sid = "e9e9e9ea-2"
        self.session("session-start", sid, self.w)
        p = self.session("session-end", sid, self.w)
        self.assertNotIn("codec", p.stderr)
        self.assertIn("e9e9e9ea", self.git(self.w, "ls-tree", "-r", "--name-only", "HEAD", "journal/sessions"))

    def test_restaging_waits_for_a_held_index(self):
        sid = "10c10c10-1"
        self.session("session-start", sid, self.w, transcript_path=str(self.root / "gone.jsonl"))
        self.git(self.w, "add", "-A")  # its log staged, as a failed `cudl commit` leaves it
        self.exchange(sid)  # its transcript turns up elsewhere: a resume records where
        lock = Path(self.git(self.w, "rev-parse", "--absolute-git-dir")) / "index.lock"
        lock.write_text("")
        releaser = subprocess.Popen(["sh", "-c", f"sleep 0.6; rm -f '{lock}'"])
        try:
            self.session("session-start", sid, self.w, source="resume")
        finally:
            releaser.wait()
        log = next((self.w / "journal/sessions").glob("*-10c10c10.md"))
        self.assertIn(f"A  journal/sessions/{log.name}", self.sh("git", "status", "--porcelain").stdout)

    # --- worktree exits, notices, the memory index ---

    def test_a_moved_transcript_is_found_by_its_id(self):
        sid = "7a7a0000-0000-4000-8000-000000000001"
        told = self.root / ".claude/projects/-wt-key" / f"{sid}.jsonl"  # what the hooks were told
        kept = self.root / ".claude/projects/-launch-key" / f"{sid}.jsonl"  # where Claude Code keeps it
        self.session("session-start", sid, self.w, transcript_path=str(told))
        self.assertEqual(self.logged(sid)["transcript"], str(told))  # a new session's file may not exist yet
        kept.parent.mkdir(parents=True)
        kept.write_text(json.dumps({"type": "user", "message": {"role": "user", "content": "hi"}}) + "\n")
        self.session("session-end", sid, self.w, transcript_path=str(told))
        self.assertEqual(self.logged(sid)["transcript"], str(kept))  # found, so the idle check saw the exchange
        self.assertIn("7a7a0000", self.git(self.w, "ls-files", "journal/sessions"))

    def test_a_subagent_end_keeps_a_transcript_path_that_exists(self):
        parent, sid = "7b7b0000-0000-4000-8000-00000000000a", "7b7b1111-0000-4000-8000-00000000000b"
        self.start(parent, CLAUDE_CODE_SESSION_ID=parent)
        good = self.root / "t" / f"{sid}.jsonl"
        good.parent.mkdir()
        good.write_text(json.dumps({"type": "user", "message": {"role": "user", "content": "review x"}}) + "\n")
        self.cudl("hook", "session-start", input=json.dumps({"session_id": sid, "cwd": str(self.w),
                                                             "transcript_path": str(good)}),
                  env={"CLAUDE_CODE_SESSION_ID": sid, "CUDL_SESSION_ID": parent})
        self.cudl("hook", "session-end", input=json.dumps({"session_id": sid, "cwd": str(self.w),
                                                           "transcript_path": str(self.root / "nowhere.jsonl")}),
                  env={"CLAUDE_CODE_SESSION_ID": sid, "CUDL_SESSION_ID": parent})
        fm = self.logged(sid)
        self.assertEqual(fm["transcript"], str(good))
        self.assertEqual(fm["goal"], "review x")

    def test_no_transcript_anywhere_is_no_activity(self):
        for sid, agent in (("7c7c0000-0000-4000-8000-000000000001", "claude"),
                           ("01a07c7c-0000-7000-8000-000000000002", "codex")):
            args = ["hook", "session-end"] + (["--agent", agent] if agent != "claude" else [])
            self.start(sid, agent=agent)
            p = self.cudl(*args, input=json.dumps({"session_id": sid, "cwd": str(self.w),
                                                   "transcript_path": str(self.root / f"{sid}.jsonl")}))
            self.assertNotIn("cudl hook", p.stderr)  # no exception swallowed by the hook
            self.assertEqual(list((self.w / "journal/sessions").glob(f"*{sid[:8]}*.md")), [])  # did nothing: no log

    # `claude -w`: Claude Code runs WorktreeCreate, the session starts in the worktree; on exit with
    # "Remove worktree" WorktreeRemove runs first, then SessionEnd, in the main lab

    def test_the_memory_index_gets_a_line_for_every_memory(self):
        mem = self.w / "memory"
        self.assertEqual((mem / "MEMORY.md").read_text(), "")  # the template's: agents edit an index, not create one
        (mem / "MEMORY.md").write_text("- [kept](kept.md) — mine\n")
        (mem / "kept.md").write_text("---\nname: kept\n---\nk\n")
        (mem / "lost.md").write_text("---\nname: lost-line\ndescription: the port is 7101\n---\nbody\n")
        (mem / "fresh.md").write_text("---\nname: fresh\n---\nits writer is about to index it\n")
        old = time.time() - 120
        for name in ("kept.md", "lost.md"):
            os.utime(mem / name, (old, old))
        sid = "d1d10000-0000-4000-8000-000000000002"
        self.start(sid)
        self.assertEqual((mem / "MEMORY.md").read_text(), "- [kept](kept.md) — mine\n")  # the end repairs, not the start
        self.session("session-end", sid, self.w)
        self.assertEqual((mem / "MEMORY.md").read_text(),
                         "- [kept](kept.md) — mine\n- [lost-line](lost.md) — the port is 7101\n")  # fresh.md: too new
        self.assertIn("memory/MEMORY.md", self.git(self.w, "show", "--name-only", "--format=", "HEAD"))
        os.utime(mem / "fresh.md", (old, old))
        self.session("session-start", sid, self.w, source="resume")
        self.session("session-end", sid, self.w)
        self.assertTrue((mem / "MEMORY.md").read_text().endswith(
            "- [fresh](fresh.md) — its writer is about to index it\n"))

    # --- worktree exits, notices, the memory index: the code review ---

    def test_an_idle_session_beside_an_unindexed_memory_leaves_no_log(self):
        def orphan(name):  # a memory committed without its index line, older than the grace period
            (self.w / "memory" / name).write_text(f"---\nname: {name[:-3]}\n---\nlost its line\n")
            self.git(self.w, "add", f"memory/{name}")
            self.git(self.w, "commit", "-qm", f"memory: {name}")
            old = time.time() - 120
            os.utime(self.w / "memory" / name, (old, old))

        orphan("one.md")
        sid = "f1f10000-0000-4000-8000-000000000001"
        self.start(sid)
        self.cudl("hook", "session-end", input=json.dumps({"session_id": sid, "cwd": str(self.w),
                                                           "transcript_path": str(self.root / "none.jsonl")}))
        self.assertEqual(list((self.w / "journal/sessions").glob("*f1f10000*")), [])
        orphan("two.md")
        sid = "f1f10000-0000-4000-8000-000000000002"
        top = self.claude_w("x", sid)
        self.remove_worktree(sid, top)
        self.assertNotIn("f1f10000", self.git(self.w, "ls-tree", "-r", "--name-only", self.abandoned(self.w, "x"),
                                              "journal/sessions"))

    # --- what the session design review found ---

    def test_compactions_are_counted_from_the_transcript(self):
        sid = "c0c00000-0000-4000-8000-000000000001"
        t = self.compacted_transcript(sid, 2)
        self.session("session-start", sid, self.w, transcript_path=str(t))
        self.session("session-end", sid, self.w, transcript_path=str(t))
        self.assertEqual(self.logged(sid).get("compactions"), "2")
        # Codex: its rollout's `compacted` records
        c = "01a0c0c0-0000-7000-8000-000000000002"
        r = self.root / ".codex/sessions/2026/10/04" / f"rollout-2026-10-04T10-00-00-{c}.jsonl"
        r.parent.mkdir(parents=True)
        r.write_text("".join(json.dumps(x) + "\n" for x in (
            {"type": "turn_context", "payload": {}},
            {"type": "compacted", "payload": {"message": "", "replacement_history": []}})))
        for event in ("session-start", "session-end"):
            self.cudl("hook", event, "--agent", "codex", env={"CODEX_THREAD_ID": c},
                      input=json.dumps({"session_id": c, "cwd": str(self.w), "transcript_path": str(r)}))
        self.assertEqual(self.logged(c).get("compactions"), "1")

    def test_a_hook_event_cudl_doesnt_have_does_nothing(self):
        # settings that still name an event an older cudl had (pre-compact, kept in an edited settings.json)
        # must not fail: an argparse error exits 2, and exit 2 from PreCompact blocks the compaction
        sid = "c1c10000-0000-4000-8000-000000000001"
        self.session("session-start", sid, self.w)
        log = next((self.w / "journal/sessions").glob(f"*-{sid[:8]}*.md"))
        before = log.read_text()
        for event in ("pre-compact", "no-such-event"):
            for agent in ("claude", "codex"):
                p = self.cudl("hook", event, "--agent", agent, check=False,
                              input=json.dumps({"session_id": sid, "cwd": str(self.w), "trigger": "auto"}))
                self.assertEqual(p.returncode, 0, p.stderr)
                self.assertIn(f"no hook {event}", p.stderr)
        self.assertEqual(log.read_text(), before)
        self.assertNotIn("PreCompact", (CUDL.parent.parent / "template/.claude/settings.json").read_text())

    def test_a_session_end_without_a_log_makes_none(self):
        # Codex's SessionStart comes with its first turn: a session closed before it only ends
        c = "01a0f6f6-0000-7000-8000-000000000001"
        self.cudl("hook", "session-end", "--agent", "codex", env={"CODEX_THREAD_ID": c},
                  input=json.dumps({"session_id": c, "cwd": str(self.w)}))
        self.assertEqual(list((self.w / "journal/sessions").glob(f"*-{c[:8]}*.md")), [])
        self.assertTrue(self.clean(self.w))

    def test_a_session_closes_its_logs_a_finish_brought_into_main(self):
        s = "f1f10000-0000-4000-8000-000000000001"
        self.session_in_a_finished_feature(s)
        self.session("session-end", s, self.w)
        linked = [read_fm(p.read_text()) for p in (self.w / "journal/sessions").glob(f"*-f-{s[:8]}*.md")]
        self.assertEqual(len(linked), 1)
        self.assertIn("ended", linked[0])
        self.assertTrue(self.clean(self.w))

    def test_a_log_whose_worktree_is_gone_gets_only_its_end(self):
        s = "f1f10000-0000-4000-8000-000000000003"
        self.cudl("new", "x", "-r", "alpha")
        x = self.w / "wt/x"
        self.start(s, top=x, CLAUDE_CODE_SESSION_ID=s)
        self.work(x, "alpha", "a", "a\n", "x: a")  # its log goes into x's history
        (self.w / "notes/elsewhere.md").write_text("main moved on\n")
        self.git(self.w, "add", "-A")
        self.git(self.w, "commit", "-q", "-m", "main: something else")
        self.cudl("finish", "x", env={"CLAUDE_CODE_SESSION_ID": s})  # the session itself finishes x
        before = self.logged(s)
        # its end runs in main (a hook can't start in a directory that is gone)
        self.cudl("hook", "session-end", input=json.dumps({"session_id": s, "cwd": str(x)}),
                  env={"CLAUDE_CODE_SESSION_ID": s})
        after = self.logged(s)
        self.assertIn("ended", after)
        for k in ("lab", "repos", "touched"):
            self.assertEqual(after.get(k), before.get(k), f"{k}: computed against main")
        self.assertTrue(self.clean(self.w))

    def test_codex_sessions_started_together_keep_both_logs(self):
        # Codex ids are time-ordered: sessions started together share their first 8 characters
        lost = 0
        for n in range(6):
            ids = [f"01b{n:05x}-000{k}-7000-8000-00000000000{k}" for k in (1, 2)]
            procs = [subprocess.Popen([str(CUDL), "hook", "session-start", "--agent", "codex"], cwd=self.w,
                                      env=dict(self.env, CODEX_THREAD_ID=i), stdin=subprocess.PIPE,
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, text=True) for i in ids]
            for proc, i in zip(procs, ids):
                proc.stdin.write(json.dumps({"session_id": i, "cwd": str(self.w)}))
                proc.stdin.close()
            for proc in procs:
                proc.wait()
            found = {read_fm(f.read_text()).get("session") for f in (self.w / "journal/sessions").glob(f"*-01b{n:05x}*.md")}
            lost += len(set(ids) - found)
        self.assertEqual(lost, 0)

    def test_a_session_resumed_in_main_after_its_feature_was_finished(self):
        # its home went into main with the finish; the new work gets a log of main's own
        s = "f1f10000-0000-4000-8000-000000000004"
        self.cudl("new", "x", "-r", "alpha")
        x = self.w / "wt/x"
        self.session("session-start", s, x)
        self.work(x, "alpha", "a", "a\n", "x: a")
        self.session("session-end", s, x)
        self.cudl("finish", "x")
        home = self.logged(s)
        self.session("session-start", s, self.w, source="resume")
        (self.w / "notes/later.md").write_text("later\n")
        t = self.compacted_transcript(s, 1)
        self.session("session-end", s, self.w, transcript_path=str(t))
        logs = {read_fm(p.read_text()).get("feature"): read_fm(p.read_text())
                for p in (self.w / "journal/sessions").glob(f"*-{s[:8]}*.md")}
        self.assertEqual(sorted(logs), ["main", "x"])
        self.assertIn("notes/later.md", logs["main"].get("touched", ""))
        for k in ("lab", "repos", "touched"):
            self.assertEqual(logs["x"].get(k), home.get(k), k)
        self.assertEqual(logs["x"].get("compactions"), "1")  # the home speaks for the whole session
        self.assertEqual(logs["x"].get("transcript"), str(t))
        self.assertEqual(self.git(self.w, "status", "--porcelain", "journal"), "")  # both committed

    # --- what the labs reported ---

    def test_a_session_that_did_nothing_leaves_no_log_beside_older_changes(self):
        """A lab's feedback: a Codex and a Claude session quit before any prompt while an upgrade lay
        uncommitted in the lab, and their logs stayed, open and without a goal. What changed before a
        session started isn't its work."""
        never = str(self.root / "never-written.jsonl")
        self.cudl("new", "f1")
        f = self.w / "wt/f1"
        (self.w / "journal.md").write_text("edited\n")
        (self.w / "notes/old.md").write_text("untracked\n")
        (self.w / "plans/.gitkeep").unlink()
        (self.w / "code/alpha/README").write_text("edited\n")
        (self.w / "memory/m.md").write_text("m\n")  # main's, for the session in f1
        old = time.time() - 120
        for p in ("journal.md", "notes/old.md", "plans", "code/alpha/README", "memory/m.md"):
            os.utime(self.w / p, (old, old))
        time.sleep(1.1)  # a start is recorded to the second, and these changes' ctimes are now
        head, before = self.head(self.w), self.git(self.w, "status", "--porcelain")
        rollout = self.root / "rollout.jsonl"  # Codex: only its session_meta, as in that lab
        rollout.write_text(json.dumps({"type": "session_meta", "payload": {"id": "01a0eeee-1", "cwd": str(self.w)}}) + "\n")
        inp = json.dumps({"session_id": "01a0eeee-1", "cwd": str(self.w), "transcript_path": str(rollout)})
        self.cudl("hook", "session-start", "--agent", "codex", input=inp)
        self.cudl("hook", "session-end", "--agent", "codex", input=inp)
        for sid, top in (("0e0e0e20-1", self.w), ("0e0e0e21-2", f)):
            self.session("session-start", sid, top, transcript_path=never)
            self.session("session-end", sid, top, transcript_path=never)
        for sid, top in (("01a0eeee", self.w), ("0e0e0e20", self.w), ("0e0e0e21", f)):
            self.assertFalse(list((top / "journal/sessions").glob(f"*-{sid}.md")), sid)
        self.assertEqual(self.head(self.w), head)
        self.assertEqual(self.git(self.w, "status", "--porcelain"), before)
        # a change made during the session, beside the older ones: its work
        sid = "0e0e0e22-3"
        self.session("session-start", sid, self.w, transcript_path=never)
        (self.w / "notes/old.md").write_text("edited in the session\n")
        self.session("session-end", sid, self.w, transcript_path=never)
        self.assertIn("0e0e0e22", self.git(self.w, "ls-files", "journal/sessions"))
        # changes that leave the mtime old: a mode, or contents copied with their times (the code review);
        # and a deletion, judged by its directory
        for sid, path, change in (
                ("0e0e0e23-4", "code/alpha/README", lambda p: p.chmod(0o755)),
                ("0e0e0e24-5", "code/alpha/README", lambda p: (p.write_text("copied\n"), os.utime(p, (old, old)))),
                ("0e0e0e26-6", "journal.md", lambda p: p.unlink())):
            time.sleep(1.1)  # the change before is older than this session
            self.session("session-start", sid, self.w, transcript_path=never)
            change(self.w / path)
            self.session("session-end", sid, self.w, transcript_path=never)
            self.assertIn(sid[:8], self.git(self.w, "ls-files", "journal/sessions"))

    def test_edits_in_a_nested_submodule_are_a_session_s_work(self):
        """A code repo's own submodule with uncommitted edits counts (the code review: the idle check had
        stopped seeing them)."""
        a = self.w / "code/alpha"
        self.git(a, "submodule", "add", "-q", str(self.up_b), "nested")
        self.git(a, "commit", "-q", "-m", "nested")
        sid = "0e0e0e25-1"
        self.session("session-start", sid, self.w, transcript_path=str(self.root / "never-written.jsonl"))
        (a / "nested/README").write_text("edited in the session\n")
        self.session("session-end", sid, self.w, transcript_path=str(self.root / "never-written.jsonl"))
        self.assertIn("0e0e0e25", self.git(self.w, "ls-files", "journal/sessions"))


if __name__ == "__main__":
    unittest.main()
