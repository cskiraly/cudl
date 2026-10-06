"""End-to-end tests of cudl. Moving existing work in: import-sessions, split, clean-branch."""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # helpers.py, however the tests run
from helpers import CudlTest  # noqa: E402


class TestMigrate(CudlTest):
    def test_import_sessions(self):
        dirs = self.fake_history()
        out = self.cudl("import-sessions", "/x/client", "--dry-run", *dirs).stdout
        self.assertIn("3 to import, 0 already", out)
        self.assertEqual(list((self.w / "journal/sessions").glob("*-legacy-*.md")), [])
        self.cudl("import-sessions", "/x/client", *dirs)
        logs = {p.name: p.read_text() for p in (self.w / "journal/sessions").glob("*-legacy-*.md")}
        self.assertEqual(sorted(logs), ["2026-08-01-legacy-aaaa1111.md", "2026-08-02-legacy-bbbb2222.md",
                                        "2026-08-04-legacy-dddd4444.md"])
        a = logs["2026-08-01-legacy-aaaa1111.md"]
        self.assertIn("goal: Bound the dial deadline on the dial-fix branch", a)
        self.assertIn("agent: claude", a)
        self.assertIn("ended: 2026-08-01T12:30:00Z", a)
        self.assertIn("branch: dial-fix", a)
        self.assertIn("needs_summary: yes", a)
        self.assertIn("goal: (continued from an earlier session)", logs["2026-08-02-legacy-bbbb2222.md"])
        self.assertIn("source: /x/client/.claude/worktrees/hedge", logs["2026-08-02-legacy-bbbb2222.md"])
        d = logs["2026-08-04-legacy-dddd4444.md"]
        self.assertIn("agent: codex", d)
        self.assertIn("goal: Review the draft post in notes/", d)
        self.assertIn("branch: variant-a", d)
        index = (self.w / "journal/INDEX.md").read_text()
        self.assertIn("legacy · [aaaa1111](sessions/2026-08-01-legacy-aaaa1111.md) · claude · legacy · Bound the dial deadline", index)
        self.assertEqual(self.git(self.w, "log", "-1", "--format=%s"), "journal: import 3 legacy sessions")
        self.assertTrue(self.clean(self.w))
        # a second run finds nothing new
        self.assertIn("0 to import, 3 already", self.cudl("import-sessions", "/x/client", *dirs).stdout)

    def test_session_ids_with_a_shared_prefix(self):
        # Codex ids are time-ordered: sessions started together share their first 8 characters
        dirs = self.fake_history()
        rollouts = Path(dirs[3])
        for i, sid in enumerate(("01a0c8a2-1111-7000-8000-000000000001", "01a0c8a2-2222-7000-8000-000000000002")):
            d = rollouts / "2026" / "08" / "06"
            d.mkdir(parents=True, exist_ok=True)
            (d / f"rollout-2026-08-06T07-00-0{i}-{sid}.jsonl").write_text(json.dumps(
                {"type": "session_meta", "timestamp": f"2026-08-06T07:00:0{i}Z",
                 "payload": {"id": sid, "cwd": "/x/client"}}) + "\n")
        self.cudl("import-sessions", "/x/client", *dirs)
        names = sorted(p.name for p in (self.w / "journal/sessions").glob("2026-08-06-legacy-*.md"))
        self.assertEqual(names, ["2026-08-06-legacy-01a0c8a2.md", "2026-08-06-legacy-01a0c8a22222.md"])
        index = (self.w / "journal/INDEX.md").read_text()
        self.assertIn("[01a0c8a22222](sessions/2026-08-06-legacy-01a0c8a22222.md)", index)
        self.assertIn("0 to import, 5 already", self.cudl("import-sessions", "/x/client", *dirs).stdout)
        # a live session whose id shares the prefix gets its own log too, and finds it again
        inp = json.dumps({"session_id": "01a0c8a2-3333-7000-8000-000000000003", "cwd": str(self.w)})
        self.cudl("hook", "session-start", "--agent", "codex", input=inp)
        self.cudl("hook", "session-end", "--agent", "codex", input=inp)
        mine = [p for p in (self.w / "journal/sessions").glob("*-main-01a0c8a2*.md")]
        self.assertEqual(len(mine), 1)
        self.assertIn("ended:", mine[0].read_text())

    def test_split_moves_artifact_history_with_pins(self):
        notes = self.mixed_history()
        dry = self.cudl("split", "alpha", "--path", "notes/", "--rename", "notes/=research/", "--dry-run").stdout
        self.assertIn("(dry run: nothing written)", dry)
        self.assertIn("warning:", dry)  # the notes commits exist only in the lab's clone of alpha
        self.assertEqual(self.git(self.w, "branch", "--list", "split/*"), "")
        before = self.head(self.w)
        self.assertEqual(self.head(self.w), before)

        self.cudl("split", "alpha", "--path", "notes/", "--rename", "notes/=research/")
        imported = self.split_commits("split/alpha/dev")
        self.assertEqual([s for _, s, _ in imported], notes)
        self.assertTrue(all(origin == pin for _, origin, pin in imported))
        self.assertEqual([t for t, _, _ in imported][:2], ["notes: first finding", "code and notes: three"])
        # authorship and dates survive; the first imported commit holds only its artifacts + the pin
        first = self.git(self.w, "rev-list", "--max-parents=0", "split/alpha/dev")
        self.assertEqual(self.git(self.w, "log", "-1", "--format=%an %ad", "--date=short", first),
                         "Author 2026-08-02")
        self.assertEqual(sorted(self.git(self.w, "ls-tree", "-r", "--name-only", first).split()),
                         [".gitmodules", "code/alpha", "research/a.md"])
        self.assertEqual(self.git(self.w, "show", f"{first}:research/a.md"), "a1")
        # history in the lab: deletion, rename, merge topology
        tip = "split/alpha/dev"
        self.assertEqual(sorted(self.git(self.w, "ls-tree", "-r", "--name-only", tip, "research").split()),
                         ["research/c.md", "research/sub/a.md"])
        self.assertEqual(self.git(self.w, "rev-list", "--merges", "--count", tip), "1")
        # merged into main: the files, and the pin history visible to a plain log
        self.assertEqual(self.git(self.w, "show", "HEAD:research/sub/a.md"), "a2")
        subjects = self.git(self.w, "log", "--format=%s", "--", "code/alpha").splitlines()
        for s in ("notes: first finding", "notes: move a", "cudl: record the alpha split; pin alpha at its checkout"):
            self.assertIn(s, subjects)
        self.assertEqual(self.pin(self.w, "code/alpha"), self.head(self.w / "code/alpha"))
        self.assertIn("path = code/alpha", self.git(self.w, "show", "HEAD:.gitmodules"))
        manifest = json.loads((self.w / ".cudl/splits.json").read_text())
        self.assertEqual(manifest["imports"][0]["branches"]["dev"]["commits"], len(notes))
        self.assertTrue(self.clean(self.w))
        # the code repo is untouched
        self.assertTrue((self.w / "code/alpha/notes/sub/a.md").exists())
        self.assertEqual(self.branch(self.w / "code/alpha"), "dev")
        # once only
        p = self.cudl("split", "alpha", "--path", "notes/", "--rename", "notes/=research/", check=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("already split", p.stderr)

    def test_split_log_follows_import_when_tip_is_the_code_head(self):
        # the usual case: the last notes commit is the code repo's HEAD, so both sides of the
        # merge pin the same commit
        a = self.w / "code/alpha"
        for i in range(3):
            (a / "notes").mkdir(exist_ok=True)
            (a / "notes" / f"n{i}.md").write_text(f"{i}\n")
            self.git(a, "add", "-A")
            self.git(a, "commit", "-q", "-m", f"notes: {i}")
        self.cudl("commit", "-m", "pin")
        self.cudl("split", "alpha", "--path", "notes/")
        subjects = self.git(self.w, "log", "--format=%s", "--", "code/alpha").splitlines()
        self.assertIn("notes: 0", subjects)
        self.assertIn("notes: 2", subjects)
        first_parents = self.git(self.w, "log", "--first-parent", "--format=%s").splitlines()
        self.assertIn("notes: 0", first_parents)
        self.assertEqual(self.pin(self.w, "code/alpha"), self.head(a))
        self.assertTrue(self.clean(self.w))

    def test_split_refuses_collisions(self):
        self.mixed_history()
        (self.w / "docs/a.md").write_text("mine\n")
        self.cudl("commit", "-m", "docs")
        before = self.head(self.w)
        p = self.cudl("split", "alpha", "--path", "notes/", "--rename", "notes/sub/=docs/", check=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("collides with the lab's docs/a.md", p.stderr)
        self.assertEqual(self.head(self.w), before)
        self.assertEqual(self.git(self.w, "branch", "--list", "split/*"), "")
        # the lab's own placeholder notes/.gitkeep is not a collision
        self.cudl("split", "alpha", "--path", "notes/")

    def test_split_drops_merges_that_filtering_makes_degenerate(self):
        a = self.w / "code/alpha"
        for name, text, msg in (("notes/x.md", "x\n", "notes: x"),):
            (a / "notes").mkdir(exist_ok=True)
            (a / name).write_text(text)
            self.git(a, "add", "-A")
            self.git(a, "commit", "-q", "-m", msg)
        self.git(a, "switch", "-q", "-c", "side")
        (a / "notes/y.md").write_text("y\n")
        self.git(a, "add", "-A")
        self.git(a, "commit", "-q", "-m", "notes: y")
        self.git(a, "switch", "-q", "dev")
        (a / "src.go").write_text("code\n")
        self.git(a, "add", "-A")
        self.git(a, "commit", "-q", "-m", "code only")
        self.git(a, "merge", "-q", "--no-ff", "-m", "merge", "side")
        self.cudl("commit", "-m", "pin")
        self.cudl("split", "alpha", "--path", "notes/")
        self.assertEqual([t for t, _, _ in self.split_commits("split/alpha/dev")], ["notes: x", "notes: y"])
        self.assertEqual(self.git(self.w, "rev-list", "--merges", "--count", "split/alpha/dev"), "0")

    def test_split_other_branch_as_archive(self):
        self.mixed_history()
        self.cudl("split", "alpha", "--path", "notes/", "--branch", "side", "--no-merge")
        # the side branch brings the history it shares with dev
        self.assertEqual([t for t, _, _ in self.split_commits("split/alpha/side")],
                         ["notes: first finding", "code and notes: three", "notes: side result"])
        self.assertFalse((self.w / "notes/b.md").exists())  # not merged into main
        p = self.cudl("split", "alpha", "--path", "src.go", "--rename", "src.go=code/x", "--branch", "dev",
                      "--dry-run", check=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("lands on the lab's code/x", p.stderr)

    def test_clean_branch(self):
        self.mixed_history()
        a = self.w / "code/alpha"
        dev = self.head(a)
        dry = self.cudl("clean-branch", "alpha", "dev", "--path", "notes/", "--dry-run").stdout
        self.assertIn("(dry run: nothing written)", dry)
        self.assertEqual(self.git(a, "branch", "--list", "dev-clean"), "")

        self.cudl("clean-branch", "alpha", "dev", "--path", "notes/")
        self.assertEqual(self.head(a), dev)  # the original branch is untouched
        self.assertEqual(self.git(a, "rev-parse", "dev"), dev)
        self.assertEqual(self.git(a, "log", "--format=%H", "dev-clean", "--", "notes"), "")
        subjects = self.git(a, "log", "--format=%s", "dev-clean").splitlines()
        self.assertEqual(subjects, ["code: five", "code: four", "code and notes: three", "code: two",
                                    "code: one", "alpha: first"])
        self.assertEqual(self.git(a, "log", "-1", "--format=%an %ad", "--date=short", "dev-clean"),
                         "Author 2026-08-10")
        self.assertEqual(self.git(a, "show", "dev-clean:src.go"), "5")
        # the map: kept commits to their image, vanished ones to the image of what came before
        mapping = dict(l.split() for l in (self.w / ".cudl/clean/alpha/dev-clean.map").read_text().splitlines()
                       if not l.startswith("#"))
        by_subject = {self.git(a, "log", "-1", "--format=%s", o): n for o, n in mapping.items()}
        self.assertEqual(by_subject["notes: first finding"], self.git(a, "rev-parse", "dev-clean~4"))
        self.assertEqual(mapping[dev], self.git(a, "rev-parse", "dev-clean"))
        for new in mapping.values():
            self.assertTrue(new == "-" or self.git(a, "merge-base", "--is-ancestor", new, "dev-clean") == "")
        record = json.loads((self.w / ".cudl/clean.json").read_text())["branches"][0]
        self.assertEqual((record["branch"], record["clean"], record["kept"]), ("dev", "dev-clean", 6))
        self.assertTrue(self.clean(self.w))
        p = self.cudl("clean-branch", "alpha", "dev", "--path", "notes/", check=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("already exists", p.stderr)

    def test_clean_branch_uses_split_paths(self):
        self.mixed_history()
        self.cudl("split", "alpha", "--path", "notes/")
        self.cudl("clean-branch", "alpha", "dev", "--as", "release")
        a = self.w / "code/alpha"
        self.assertEqual(self.git(a, "log", "--format=%H", "release", "--", "notes"), "")
        self.assertIn("release", json.loads((self.w / ".cudl/clean.json").read_text())["branches"][0]["clean"])

    # --- pin retention, squash, stacked features ---

    def test_split_pins_are_kept(self):
        notes = self.mixed_history()
        self.cudl("split", "alpha", "--path", "notes/")
        self.assertTrue(set(notes) <= self.keep_refs())
        self.cudl("keep", "--check")

    # --- worktree exits, notices, the memory index ---

    def test_split_signs_its_merge_and_checks_the_signer_first(self):
        self.mixed_history()
        before = self.head(self.w)
        p = self.cudl("split", "alpha", "--path", "notes/", env=self.fake_signer(works=False), check=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("commit signing doesn't work, so nothing was imported", p.stderr)
        self.assertEqual(self.head(self.w), before)
        self.assertEqual(self.git(self.w, "branch", "--list", "split/*"), "")
        self.cudl("split", "alpha", "--path", "notes/", env=self.fake_signer())
        merge = self.git(self.w, "rev-parse", "HEAD~1")
        self.assertEqual(len(self.git(self.w, "rev-list", "--parents", "-1", merge).split()), 3)
        self.assertIn("gpgsig", self.git(self.w, "cat-file", "-p", merge))
        self.assertIn("gpgsig", self.git(self.w, "cat-file", "-p", "HEAD"))

    # `claude -w`: Claude Code runs WorktreeCreate, the session starts in the worktree; on exit with
    # "Remove worktree" WorktreeRemove runs first, then SessionEnd, in the main lab

    def test_split_still_imports_a_memory_directory_into_a_fresh_lab(self):
        a = self.w / "code/alpha"
        (a / "memory").mkdir()
        (a / "memory/MEMORY.md").write_text("- [a](a.md) — a\n")
        (a / "memory/a.md").write_text("a\n")
        self.git(a, "add", "-A")
        self.git(a, "commit", "-qm", "memory")
        self.cudl("commit", "-m", "pin alpha")
        self.cudl("split", "alpha", "--path", "memory/")
        self.assertEqual(self.git(self.w, "show", "HEAD:memory/MEMORY.md"), "- [a](a.md) — a")


if __name__ == "__main__":
    unittest.main()
