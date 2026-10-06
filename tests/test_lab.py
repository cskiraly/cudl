"""End-to-end tests of cudl. The lab itself: init, add, setup, install, upgrade, the template,
pins and their keep refs, the lab's git hooks, feedback."""

import json
import os
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # helpers.py, however the tests run
from helpers import CUDL, CudlTest  # noqa: E402


class TestLab(CudlTest):
    def test_init_and_add(self):
        gm = (self.w / ".gitmodules").read_text()
        self.assertIn("branch = dev", gm)
        self.assertIn("branch = main", gm)
        self.assertEqual(self.branch(self.w / "code/alpha"), "dev")
        self.assertEqual(self.branch(self.w / "code/beta"), "main")
        self.assertTrue((self.w / ".cudl/cudl").is_file())
        local = json.loads((self.w / ".claude/settings.local.json").read_text())
        self.assertEqual(local["autoMemoryDirectory"], str(self.w / "memory"))
        self.assertTrue(self.clean(self.w))

    def test_template_serves_both_agents(self):
        self.assertEqual((self.w / "CLAUDE.md").read_text().strip(), "@AGENTS.md")
        for skill in ("commit", "feature", "wrap"):
            link = self.w / ".claude/skills" / skill
            self.assertTrue(link.is_symlink())
            self.assertEqual(link.resolve(), (self.w / ".agents/skills" / skill).resolve())
            self.assertTrue((link / "SKILL.md").is_file())
        hooks = json.loads((self.w / ".codex/hooks.json").read_text())["hooks"]
        self.assertEqual(sorted(hooks), ["SessionEnd", "SessionStart"])
        # the codex hook command finds .cudl/cudl from a subdirectory, as Codex runs it from the cwd
        cmd = hooks["SessionStart"][0]["hooks"][0]["command"]
        self.cudl("new", "f1", "-r", "alpha")
        out = self.sh("sh", "-c", cmd, cwd=self.w / "wt/f1/code/alpha",
                      input=json.dumps({"session_id": "c0dec0de-1", "cwd": str(self.w / "wt/f1/code/alpha")})).stdout
        self.assertIn("feature `f1`", json.loads(out)["hookSpecificOutput"]["additionalContext"])
        # made by init and upgrade, not shipped: a checkout of cudl isn't configuration for working on it
        template = CUDL.parent.parent / "template"
        self.assertFalse((template / "CLAUDE.md").exists())
        self.assertFalse((template / ".claude/skills").exists())
        (self.w / "CLAUDE.md").unlink()
        (self.w / ".claude/skills/wrap").unlink()
        self.cudl("upgrade")
        self.assertEqual((self.w / "CLAUDE.md").read_text(), "@AGENTS.md\n")
        self.assertTrue((self.w / ".claude/skills/wrap/SKILL.md").is_file())
        self.assertEqual(os.readlink(self.w / ".claude/skills/wrap"), "../../.agents/skills/wrap")

    def test_install_links(self):
        # a copy of the tool outside any lab: the one under test may be a lab feature's, which doesn't install
        tool = self.root / "tool"
        (tool / "bin").mkdir(parents=True)
        (tool / "bin/cudl").write_bytes(CUDL.read_bytes())
        (tool / "bin/cudl").chmod(0o755)
        self.sh("cp", "-r", str(CUDL.parent.parent / "skills"), str(tool / "skills"))
        out = self.sh(str(tool / "bin/cudl"), "install").stdout
        home = self.root
        self.assertEqual((home / ".local/bin/cudl").resolve(), tool / "bin/cudl")
        for d in (".claude", ".agents"):
            self.assertTrue((home / d / "skills/cudl/SKILL.md").is_file())
        self.assertIn("already linked", self.sh(str(tool / "bin/cudl"), "install").stdout)

    def test_upgrade_restores_tool_files(self):
        (self.w / ".codex/hooks.json").write_text("{}")
        (self.w / "AGENTS.md").write_text("mine\n")
        self.assertIn("note: AGENTS.md was edited here, so it was kept", self.cudl("upgrade").stdout)
        self.assertIn("SessionStart", (self.w / ".codex/hooks.json").read_text())
        self.assertEqual((self.w / "AGENTS.md").read_text(), "mine\n")  # user-owned, kept

    def test_plans_directory_is_relative(self):
        # Claude Code resolves plansDirectory against the project root, so each worktree's settings
        # say "plans"; an absolute path left by an older cudl is rewritten by `cudl setup`.
        local = self.w / ".claude/settings.local.json"
        self.assertEqual(json.loads(local.read_text())["plansDirectory"], "plans")
        data = json.loads(local.read_text())
        data["plansDirectory"] = str(self.w / "plans")
        data["other"] = "kept"
        local.write_text(json.dumps(data))
        self.cudl("setup")
        data = json.loads(local.read_text())
        self.assertEqual(data["plansDirectory"], "plans")
        self.assertEqual(data["other"], "kept")
        self.assertEqual(data["autoMemoryDirectory"], str(self.w / "memory"))
        self.cudl("new", "f1")
        f = self.w / "wt/f1"
        self.assertEqual(json.loads((f / ".claude/settings.local.json").read_text())["plansDirectory"], "plans")
        self.assertTrue((f / "plans").is_dir())

    def test_add_own_integration_branch(self):
        self.cudl("add", "gamma", str(self.upstream("gamma", "main")), "-b", "lab", "--from", "main")
        g = self.w / "code/gamma"
        self.assertEqual(self.branch(g), "lab")
        self.assertIn("branch = lab", (self.w / ".gitmodules").read_text())
        self.assertEqual(self.git(g, "rev-parse", "lab"), self.git(g, "rev-parse", "origin/main"))
        out = self.cudl("push").stdout
        self.assertIn("gamma: lab has no push remote; skipped", out)
        self.assertNotIn("push origin lab", out)
        self.assertIn("push --no-follow-tags origin dev", out)  # alpha tracks origin/dev: listed as before

    def test_upgrade_follows_the_template_while_unedited(self):
        import hashlib
        template = (CUDL.parent.parent / "template/AGENTS.md").read_text()
        self.assertIn("AGENTS.md: up to date", self.cudl("upgrade").stdout)
        marker_file = self.w / ".cudl.json"

        def set_marker(**templates):
            m = json.loads(marker_file.read_text())
            m["templates"] = templates
            marker_file.write_text(json.dumps(m))

        # recorded hash, file unchanged since: it follows the template
        old = "# an older template\n"
        (self.w / "AGENTS.md").write_text(old)
        set_marker(**{"AGENTS.md": hashlib.sha256(old.encode()).hexdigest()})
        self.assertIn("AGENTS.md: updated from the template (unedited)", self.cudl("upgrade").stdout)
        self.assertEqual((self.w / "AGENTS.md").read_text(), template)
        # no record (a lab older than the hash), but it is a past template version: taken too
        tool, older = self.tool_with_history()
        (self.w / "AGENTS.md").write_text(older)
        set_marker()
        self.assertIn("it was an unedited older version", self.sh(str(tool / "bin/cudl"), "upgrade").stdout)
        self.assertEqual((self.w / "AGENTS.md").read_text(), template)
        # edited here: kept, unless asked
        (self.w / "AGENTS.md").write_text(template + "\n- a rule of my own\n")
        out = self.cudl("upgrade").stdout
        self.assertIn("AGENTS.md was edited here, so it was kept", out)
        self.assertIn("a rule of my own", (self.w / "AGENTS.md").read_text())
        self.assertIn("replacing your edits", self.cudl("upgrade", "--take", "agents-md").stdout)
        self.assertEqual((self.w / "AGENTS.md").read_text(), template)
        self.assertIn("cudl sync", out)
        # the same for settings.json
        settings = self.w / ".claude/settings.json"
        settings.write_text(settings.read_text().replace('"hooks"', '"mine": 1, "hooks"'))
        self.assertIn(".claude/settings.json was edited here", self.cudl("upgrade").stdout)
        self.cudl("upgrade", "--take", "settings")
        self.assertNotIn('"mine"', settings.read_text())

    # --- a helper, not a gatekeeper ---

    def test_lab_hooks_keep_pins_of_plain_git_commits(self):
        a = self.w / "code/alpha"
        (a / "x").write_text("x\n")
        self.git(a, "add", "-A")
        self.git(a, "commit", "-q", "-m", "plain git in the code")
        c = self.head(a)
        self.git(self.w, "add", "code/alpha")
        self.git(self.w, "commit", "-q", "-m", "plain git in the lab")  # post-commit hook
        self.assertIn(c, self.keep_refs())
        # an amend that records a newer pin: post-rewrite keeps it
        (a / "y").write_text("y\n")
        self.git(a, "add", "-A")
        self.git(a, "commit", "-q", "-m", "more")
        c2 = self.head(a)
        self.git(self.w, "add", "code/alpha")
        self.git(self.w, "commit", "-q", "--amend", "--no-edit")
        self.assertIn(c2, self.keep_refs())
        self.cudl("keep", "--check")

    def test_lab_hooks_chain_an_existing_hook(self):
        hooks = Path(self.git(self.w, "rev-parse", "--path-format=absolute", "--git-common-dir")) / "hooks"
        mark = self.root / "mine-ran"
        (hooks / "post-commit").write_text(f"#!/bin/sh\ntouch {mark}\n")
        (hooks / "post-commit").chmod(0o755)
        self.cudl("setup")
        self.assertTrue((hooks / "post-commit.pre-cudl").exists())
        (self.w / "notes/n.md").write_text("n\n")
        self.git(self.w, "add", "-A")
        self.git(self.w, "commit", "-q", "-m", "n")
        self.assertTrue(mark.exists())
        self.cudl("setup")  # idempotent: doesn't chain itself
        self.assertIn("managed by cudl", (hooks / "post-commit").read_text())
        self.assertNotIn("managed by cudl", (hooks / "post-commit.pre-cudl").read_text())

    def test_feedback_is_a_file_in_the_main_lab(self):
        self.cudl("new", "f1")
        out = self.cudl("feedback", "sync was confusing", "here", cwd=self.w / "wt/f1").stdout
        files = list((self.w / "notes/cudl-feedback").glob("*-sync-was-confusing-here.md"))
        self.assertEqual(len(files), 1)
        self.assertIn("from f1", files[0].read_text())
        self.assertIn("cudl feedback:", self.git(self.w, "log", "-1", "--format=%s"))
        self.assertTrue(self.clean(self.w))

    # --- a helper, not a gatekeeper: the code review ---

    def test_feedback_never_overwrites(self):
        for _ in range(2):
            self.cudl("feedback", "same words")
        self.assertEqual(len(list((self.w / "notes/cudl-feedback").glob("*-same-words*.md"))), 2)

    def test_chained_rewrite_hook_gets_every_line(self):
        hooks = Path(self.git(self.w, "rev-parse", "--path-format=absolute", "--git-common-dir")) / "hooks"
        seen = self.root / "rewritten"
        (hooks / "post-rewrite.pre-cudl").write_text(f'#!/bin/sh\nwhile read old new; do echo "$old" >> {seen}; done\n')
        (hooks / "post-rewrite.pre-cudl").chmod(0o755)
        (hooks / "post-commit.pre-cudl").write_text("#!/bin/sh\nexit 1\n")  # a failing chained hook
        (hooks / "post-commit.pre-cudl").chmod(0o755)
        a = self.w / "code/alpha"
        (a / "x").write_text("x\n")
        self.git(a, "add", "-A")
        self.git(a, "commit", "-q", "-m", "x")
        self.git(self.w, "add", "code/alpha")
        self.git(self.w, "commit", "-q", "-m", "pin")  # the chained hook fails; retention still runs
        self.assertIn(self.head(a), self.keep_refs())
        self.git(self.w, "commit", "-q", "--amend", "-m", "pin, amended")
        self.assertEqual(len(seen.read_text().splitlines()), 1)

    # --- a helper, not a gatekeeper: the review's second pass ---

    def test_backfill_with_a_lost_pin_keeps_the_others(self):
        self.cudl("new", "f1", "-r", "alpha")
        f = self.w / "wt/f1"
        c1 = self.work(f, "alpha", "a", "1\n", "one")
        a = self.w / "code/alpha"
        self.git(f / "code/alpha", "commit", "-q", "--amend", "-m", "one, amended")  # c1 now only kept by its ref
        c2 = self.work(f, "alpha", "b", "2\n", "two")
        for ref in self.keep_refs():
            self.git(a, "update-ref", "-d", f"refs/cudl/keep/{ref}")
        self.gc()  # c1 is gone; c2 (on the branch) remains
        p = self.cudl("setup", check=False)  # backfill, with a lost pin sorted among the rest
        self.assertIn("no longer in their code repos", p.stderr)
        self.assertIn(c2, self.keep_refs())
        self.assertNotIn(c1, self.keep_refs())

    # --- pin retention, squash, stacked features ---

    def test_pins_survive_a_hand_made_squash_and_gc(self):
        self.cudl("new", "f1", "-r", "alpha")
        f = self.w / "wt/f1"
        c1 = self.work(f, "alpha", "a", "1\n", "one")
        c2 = self.work(f, "alpha", "b", "2\n", "two")
        self.assertTrue({c1, c2} <= self.keep_refs())
        # the pilot's route: reset --soft, then cudl commit
        base = self.git(f / "code/alpha", "merge-base", "f1", "dev")
        self.git(f / "code/alpha", "reset", "-q", "--soft", base)
        self.cudl("commit", "-m", "squashed by hand", cwd=f)
        self.gc()
        lab_commits = self.git(f, "log", "--format=%H", "feat/f1").split()
        pins = []
        for lc in lab_commits:
            r = self.sh("git", "rev-parse", "--verify", "-q", f"{lc}:code/alpha", cwd=f, check=False)
            if r.returncode == 0:
                pins.append(r.stdout.strip())
        self.assertIn(c2, pins)  # the pre-squash head is still in the lab's history...
        for pin in pins:         # ...and every pin still resolves after gc
            self.sh("git", "cat-file", "-e", f"{pin}^{{commit}}", cwd=self.w / "code/alpha")
        self.cudl("keep", "--check")

    def test_the_pins_of_a_repo_removed_by_hand_are_no_one_s_to_keep(self):
        """A repo taken out of the lab by hand (as renaming one takes) leaves its pins in the lab's history:
        keep --check crashed on its missing checkout, and every backfill warned about them."""
        self.work(self.w, "beta", "b", "1\n", "one")
        self.git(self.w, "rm", "-q", "code/beta")
        self.git(self.w, "commit", "-q", "-m", "beta leaves the lab")
        self.sh("rm", "-rf", str(self.w / ".git/modules/beta"))
        self.git(self.w, "config", "--remove-section", "submodule.beta")
        p = self.cudl("keep", "--check")
        self.assertIn("of repos no longer in the lab (code/beta)", p.stdout)
        self.assertNotIn("Traceback", p.stderr)
        self.assertNotIn("warning", self.cudl("setup").stderr)  # its backfill of every pin

    def test_a_registered_repo_without_its_checkout_is_still_the_lab_s(self):
        """The code review of the fix above: its pins can't be checked, and say so."""
        self.sh("rm", "-rf", str(self.w / "code/beta"))  # still in .gitmodules
        p = self.cudl("keep", "--check", check=False)
        self.assertEqual(p.returncode, 1)
        self.assertIn("code/beta", p.stdout)
        self.assertIn("not kept", p.stdout)
        self.assertNotIn("no longer in the lab", p.stdout)
        self.assertNotIn("Traceback", p.stderr)

    def test_a_repo_main_dropped_but_a_feature_still_has_is_still_the_lab_s(self):
        """The code review of the fix above: a live feature's pins of it aren't left out."""
        self.cudl("new", "f1", "-r", "beta")
        self.git(self.w, "rm", "-q", "code/beta")
        self.git(self.w, "commit", "-q", "-m", "beta leaves main")
        p = self.cudl("keep", "--check", check=False)
        self.assertNotIn("no longer in the lab", p.stdout)
        self.assertNotIn("Traceback", p.stderr)

    def test_keep_check_finds_unkept_and_lost_pins(self):
        self.cudl("new", "f1", "-r", "alpha")
        f = self.w / "wt/f1"
        c1 = self.work(f, "alpha", "a", "1\n", "one")
        self.cudl("keep", "--check")
        a = self.w / "code/alpha"
        self.git(a, "update-ref", "-d", f"refs/cudl/keep/{c1}")
        p = self.cudl("keep", "--check", check=False)
        self.assertEqual(p.returncode, 1)
        self.assertIn(f"{c1} not kept", p.stdout)
        self.assertIn("1 newly", self.cudl("keep").stdout)  # backfill
        self.cudl("keep", "--check")
        # a pin whose commit is gone: amend it away, drop its ref, gc
        self.git(f / "code/alpha", "commit", "-q", "--amend", "-m", "one, amended")
        self.git(a, "update-ref", "-d", f"refs/cudl/keep/{c1}")
        self.gc()
        p = self.cudl("keep", "--check", check=False)
        self.assertEqual(p.returncode, 1)
        self.assertIn(f"{c1} lost", p.stdout)

    def test_pins_survive_forced_abandon_after_a_rewrite(self):
        self.cudl("new", "f1", "-r", "alpha")
        f = self.w / "wt/f1"
        c1 = self.work(f, "alpha", "a", "1\n", "one")
        self.git(f / "code/alpha", "commit", "-q", "--amend", "-m", "rewritten, never recorded")
        (f / "code/alpha/dirty").write_text("x\n")
        self.cudl("finish", "f1", "--abandon", "--force")
        self.gc()
        self.sh("git", "cat-file", "-e", f"{c1}^{{commit}}", cwd=self.w / "code/alpha")
        self.cudl("keep", "--check")

    # --- the live checks' fixes ---

    def test_taught_commands_are_allowed(self):
        """Every `.cudl/cudl …` an agent is taught (AGENTS.md, the skills) runs without a prompt, unless
        it is the user's (finish, push, upgrade, open, anything with --force)."""
        perms = json.loads((self.w / ".claude/settings.json").read_text())["permissions"]
        allow = [r[len("Bash("):-1] for r in perms["allow"]]
        self.assertIn("Bash(.cudl/cudl * --force*)", perms["ask"])
        self.assertFalse([r for r in allow if r.startswith(".cudl/cudl open")])  # open --command runs anything

        def allowed(cmd):
            return any(cmd == r or (r.endswith(":*") and (cmd == r[:-2] or cmd.startswith(r[:-2] + " ")))
                       for r in allow)
        docs = [self.w / "AGENTS.md", *sorted((self.w / ".agents/skills").glob("*/SKILL.md"))]
        taught = [" ".join(c.split()) for d in docs for c in re.findall(r"`(\.cudl/cudl [^`]*)`", d.read_text())]
        self.assertGreater(len(taught), 5)
        for cmd in taught:
            sub = cmd.split()[1]
            if sub.startswith("<") or sub in ("finish", "push", "upgrade", "open") or "--force" in cmd:
                continue
            self.assertTrue(allowed(cmd), f"taught, but it would prompt: {cmd}")

    # `claude -w`: Claude Code runs WorktreeCreate, the session starts in the worktree; on exit with
    # "Remove worktree" WorktreeRemove runs first, then SessionEnd, in the main lab

    def test_claude_s_hooks_find_cudl_from_where_they_run(self):
        # after "Remove worktree" a session's end runs in the main lab, and CLAUDE_PROJECT_DIR still
        # names the worktree, which is gone
        hooks = json.loads((self.w / ".claude/settings.json").read_text())["hooks"]
        end = hooks["SessionEnd"][0]["hooks"][0]["command"]
        sid = "c1a0de00-0000-4000-8000-000000000001"
        self.start(sid)
        p = self.sh("sh", "-c", end, cwd=self.w, input=json.dumps({"session_id": sid, "cwd": str(self.w)}),
                    env={"CLAUDE_PROJECT_DIR": str(self.w / "wt/gone"), "CLAUDE_CODE_SESSION_ID": sid})
        self.assertEqual(p.stderr, "")
        self.assertIn("c1a0de00", self.git(self.w, "ls-files", "journal/sessions"))

    def test_a_feature_s_copy_of_cudl_leaves_its_own_lab_alone(self):
        self.cudl("new", "f1", "-r", "alpha")
        copy = self.w / "wt/f1/code/alpha/bin/cudl"  # cudl's code, worked on in a feature of this lab
        copy.parent.mkdir()
        copy.write_bytes(CUDL.read_bytes())
        copy.chmod(0o755)

        def run(*args, cwd=None, input=None, env=None):
            return self.sh(str(copy), *args, cwd=cwd or self.w, check=False, input=input, env=env)

        for args, cwd in ((("commit", "-m", "x"), None), (("-f", "f1", "commit", "-m", "x"), None),
                          (("upgrade",), None), (("install",), self.root), (("init", str(self.w / "scratch/x")), None)):
            p = run(*args, cwd=cwd)
            self.assertEqual(p.returncode, 1, args)
            self.assertIn("unfinished copy of cudl", p.stderr)
        p = run("hook", "session-end", input=json.dumps({"session_id": "e0e0e0e0-1", "cwd": str(self.w)}))
        self.assertIn("unfinished copy of cudl", p.stderr)
        p = run("status")
        self.assertEqual(p.returncode, 0)
        self.assertIn("warning:", p.stderr)
        # any other lab is the copy's to work on
        other = self.root / "other"
        self.cudl("init", str(other), cwd=self.root)
        for args in (("status",), ("commit", "-m", "x")):
            p = run(*args, cwd=other)
            self.assertEqual((p.returncode, p.stderr), (0, ""))
        # a hook acts where its input says, else where CLAUDE_PROJECT_DIR does: the guard looks there too
        p = run("hook", "session-start", cwd=other, input=json.dumps({"session_id": "e1e1e1e1-1"}),
                env={"CLAUDE_PROJECT_DIR": str(self.w)})
        self.assertIn("unfinished copy of cudl", p.stderr)
        p = run("hook", "worktree-remove", cwd=other, input=json.dumps({"path": str(self.w / "wt/f1")}))
        self.assertIn("unfinished copy of cudl", p.stderr)
        self.assertTrue((self.w / "wt/f1").is_dir())


    # --- bin/cudl built from cudl/ ---

    def test_upgrade_replaces_the_copy_in_one_rename(self):
        """A hook that starts during an upgrade runs the old copy or the new one, never half of one: the
        new copy is written beside it and renamed over it, so the old file stays whole for whoever has it
        open, and nothing is left where a commit would take it."""
        copy = self.w / ".cudl/cudl"
        before = copy.stat().st_ino
        self.cudl("upgrade")
        self.assertNotEqual(copy.stat().st_ino, before)  # replaced, not rewritten in place
        self.assertEqual(copy.read_bytes(), CUDL.read_bytes())
        self.assertTrue(os.access(copy, os.X_OK))
        self.assertEqual(sorted(p.name for p in (self.w / ".cudl").iterdir()), ["cudl"])
        self.assertEqual(list((self.w / ".git").glob("cudl-*.tmp")), [])
        self.assertTrue(self.clean(self.w))


if __name__ == "__main__":
    unittest.main()
