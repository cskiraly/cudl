"""End-to-end tests of cudl. Hardening from the security review before the first release: what a code
repository under development, an agent's arguments or imported data can make cudl do."""

import json
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # helpers.py, however the tests run
from helpers import CudlTest  # noqa: E402


class TestSecurity(CudlTest):
    def plant(self, repo="alpha"):
        """A code repo that ships its own .cudl/cudl and .cudl.json, as any repository can."""
        d = self.w / "code" / repo
        marker = self.root / "PLANTED-RAN"
        (d / ".cudl").mkdir()
        (d / ".cudl/cudl").write_text(f"#!/bin/sh\ntouch '{marker}'\n")
        (d / ".cudl/cudl").chmod(0o755)
        (d / ".cudl.json").write_text("{}\n")
        return d, marker

    def test_claude_hooks_run_the_lab_s_cudl_from_inside_a_code_repo(self):
        """Claude Code runs hooks in the agent's current directory, which can be a code repo with a
        .cudl/cudl of its own: the hooks look from the directory the session started in. (After "Remove
        worktree", with CLAUDE_PROJECT_DIR gone: test_lab's test_claude_s_hooks_find_cudl_from_where_they_run.)"""
        d, marker = self.plant()
        hooks = json.loads((self.w / ".claude/settings.json").read_text())["hooks"]
        sid = "5ec00000-0000-4000-8000-000000000001"
        for event in ("SessionStart", "SessionEnd"):
            p = subprocess.run(["sh", "-c", hooks[event][0]["hooks"][0]["command"]], cwd=d, text=True,
                               capture_output=True, input=json.dumps({"session_id": sid, "cwd": str(d)}),
                               env=dict(self.env, CLAUDE_PROJECT_DIR=str(self.w), CLAUDE_CODE_SESSION_ID=sid))
            self.assertEqual((p.returncode, p.stderr), (0, ""), event)
            self.assertFalse(marker.exists(), event)
        self.assertIn("5ec00000", self.git(self.w, "ls-files", "journal/sessions"))  # the lab's cudl, on the lab
        self.assertTrue(self.logged(sid).get("ended"))
        self.assertFalse((d / "journal").exists())

    def test_a_code_repo_s_own_marker_doesn_t_make_it_a_lab(self):
        d, _ = self.plant()
        self.assertIn("beta", self.cudl("status", cwd=d).stdout)  # the lab around the repo, with its repos

    def test_session_start_warns_of_a_code_repo_s_own_cudl(self):
        self.plant()
        out = json.loads(self.session("session-start", "5ec00000-0000-4000-8000-000000000002", self.w).stdout)
        self.assertIn("code/alpha", out["systemMessage"])
        self.assertIn("code/alpha", out["hookSpecificOutput"]["additionalContext"])

    def test_session_start_warns_of_a_cudl_deeper_in_a_code_repo(self):
        a = self.w / "code/alpha"
        (a / "sub/.cudl").mkdir(parents=True)
        (a / "sub/.cudl/cudl").write_text("#!/bin/sh\n")
        self.git(a, "add", "sub")
        self.git(a, "commit", "-q", "-m", "ships one below its top")
        out = json.loads(self.session("session-start", "5ec00000-0000-4000-8000-000000000005", self.w).stdout)
        self.assertIn("code/alpha", out["systemMessage"])

    def test_f_takes_only_a_feature_name(self):
        for bad in (str(self.w), "../..", "/tmp"):
            p = self.cudl("index", "-f", bad, check=False)
            self.assertNotEqual(p.returncode, 0, bad)
            self.assertIn("not a feature name", p.stderr, bad)

    def test_a_stray_worktree_under_wt_isn_t_a_feature(self):
        self.cudl("new", "f1")
        self.git(self.w, "worktree", "add", "-q", "-b", "feat/_odd", str(self.w / "wt/_odd"))  # made by hand
        self.assertIn("f1", self.cudl("ls").stdout)

    def test_push_feature_takes_only_a_feature_name(self):
        p = self.cudl("push", "--feature", "/tmp", check=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("not a feature name", p.stderr)

    def test_the_lab_s_git_hooks_quote_its_path(self):
        weird = self.root / "lab$(touch PWNED)"
        self.cudl("init", str(weird), cwd=self.root)
        self.git(weird, "commit", "-q", "--allow-empty", "-m", "a commit runs the lab's hooks")
        self.assertEqual(list(self.root.rglob("PWNED")), [])

    def test_handoff_from_takes_a_file_only_from_inside_the_lab(self):
        outside = self.root / "secret.txt"
        outside.write_text("a secret\n")
        p = self.cudl("new", "f1", "--handoff-from", str(outside), check=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("outside the lab", p.stderr)
        self.assertFalse((self.w / "wt/f1").exists())
        link = self.w / "notes/link.md"
        link.symlink_to(outside)  # in the lab, but it reads the file outside
        self.assertIn("outside the lab", self.cudl("new", "f1", "--handoff-from", str(link), check=False).stderr)
        (self.w / "notes/h.md").write_text("# h\n\nState: from a file in the lab\n")
        self.cudl("new", "f1", "--handoff-from", "notes/h.md")
        self.assertIn("from a file in the lab", (self.w / "wt/f1/handoff/f1.md").read_text())

    def test_new_checks_its_handoff_seed_before_it_writes(self):
        p = self.cudl("new", "f1", "-r", "alpha", "--handoff-from", "notes/nope.md", check=False)  # a typo
        self.assertNotEqual(p.returncode, 0)
        self.assertFalse((self.w / "wt/f1").exists())
        self.assertEqual(self.git(self.w / "code/alpha", "branch", "--list", "f1"), "")
        (self.w / "notes/h.md").write_text("# h\n\nState: seeded\n")
        self.cudl("new", "f1", "-r", "alpha", "--handoff-from", "notes/h.md")
        self.assertIn("seeded", (self.w / "wt/f1/handoff/f1.md").read_text())

    def test_the_memory_index_skips_a_symlinked_memory(self):
        secret = self.root / "secret.md"
        secret.write_text("---\nname: leak\ndescription: SECRET-LINE\n---\n")
        old = time.time() - 120
        os.utime(secret, (old, old))
        (self.w / "memory/leak.md").symlink_to(secret)
        sid = "5ec00000-0000-4000-8000-000000000003"
        self.session("session-start", sid, self.w)
        self.session("session-end", sid, self.w)
        self.assertNotIn("SECRET-LINE", (self.w / "memory/MEMORY.md").read_text())

    def test_import_sessions_names_logs_by_a_date_and_an_id_only(self):
        projects = self.root / "claude-projects"
        d = projects / "-x-proj"
        d.mkdir(parents=True)
        for stem, sid, ts in (("t1", "aaaa9999-0000", "../../../evil"), ("t2", "../../evil2", "2026-08-01T10:00:00Z"),
                              ("t3", 123456789, "2026-08-02T10:00:00Z")):  # not a string at all
            rec = {"type": "user", "cwd": "/x/proj", "sessionId": sid, "timestamp": ts,
                   "message": {"role": "user", "content": "hello"}}
            (d / f"{stem}.jsonl").write_text(json.dumps(rec) + "\n")
        out = self.cudl("import-sessions", "/x/proj", "--claude-projects", str(projects),
                        "--codex-sessions", str(self.root / "none")).stdout
        self.assertIn("skipped", out)
        logs = sorted(p.name for p in (self.w / "journal/sessions").glob("*-legacy-*.md"))
        self.assertEqual(logs, ["unknown-legacy-aaaa9999.md"])
        self.assertEqual([p for p in self.root.rglob("*legacy*") if "journal/sessions" not in str(p)], [])

    def test_session_start_refuses_an_id_it_can_t_name_a_file_by(self):
        for sid in ("../../escape", "5ec00000-0000-4000-8000-00000000000a\n"):  # a newline: its log is lost
            p = self.session("session-start", sid, self.w)
            self.assertIn("session id", p.stderr, repr(sid))
            self.assertEqual([x.name for x in (self.w / "journal/sessions").iterdir()], [".gitkeep"], repr(sid))

    def test_add_takes_a_url_that_starts_with_a_dash_as_the_url(self):
        p = self.cudl("add", "evil", "--", "--depth=1", check=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("'--depth=1'", p.stderr)  # git names it as the repository


if __name__ == "__main__":
    unittest.main()
