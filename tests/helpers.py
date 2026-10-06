"""End-to-end tests for cudl: real git repositories in a temporary directory. The setup and
helpers every test file shares."""

import json
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path


CUDL = Path(__file__).resolve().parent.parent / "bin" / "cudl"


def read_fm(text):
    """A session log's frontmatter, as cudl writes it: `key: value` lines between the `---`."""
    head = text.split("---\n", 2)[1]
    return dict((l.split(": ", 1) if ": " in l else (l.rstrip(":"), "")) for l in head.splitlines())


class CudlTest(unittest.TestCase):
    """A lab in a temporary directory, and the helpers the tests share. No tests of its own: a
    subclass would run them all again."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        gitconfig = self.root / "gitconfig"
        gitconfig.write_text(
            "[user]\n\tname = Test\n\temail = test@example.org\n"
            "[commit]\n\tgpgsign = false\n[init]\n\tdefaultBranch = main\n"
            "[protocol \"file\"]\n\tallow = always\n"
        )
        self.env = dict(os.environ, GIT_CONFIG_GLOBAL=str(gitconfig), GIT_CONFIG_NOSYSTEM="1",
                        HOME=str(self.root))
        self.env.pop("TMUX", None)
        for var in ("CLAUDE_PROJECT_DIR", "CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID", "CUDL_SESSION_ID",
                    "CLAUDE_ENV_FILE"):
            self.env.pop(var, None)  # the session running the tests must not look like a parent, nor be written to
        self.up_a = self.upstream("alpha", "dev")
        self.up_b = self.upstream("beta", "main")
        self.w = self.root / "wrap"
        self.cudl("init", str(self.w), cwd=self.root)
        self.cudl("add", "alpha", str(self.up_a), "-b", "dev")
        self.cudl("add", "beta", str(self.up_b))

    def tearDown(self):
        self.tmp.cleanup()

    def sh(self, *cmd, cwd=None, check=True, input=None, env=None):
        p = subprocess.run(cmd, cwd=cwd or self.w, env=dict(self.env, **(env or {})), text=True,
                           capture_output=True, input=input)
        if check and p.returncode != 0:
            self.fail(f"{' '.join(map(str, cmd))} failed ({p.returncode}):\n{p.stdout}\n{p.stderr}")
        return p

    def cudl(self, *args, cwd=None, check=True, input=None, env=None):
        return self.sh(str(CUDL), *args, cwd=cwd, check=check, input=input, env=env)

    def git(self, cwd, *args):
        return self.sh("git", *args, cwd=cwd).stdout.strip()

    def upstream(self, name, branch):
        d = self.root / "up" / name
        d.mkdir(parents=True)
        self.git(d, "init", "-q", "-b", branch)
        (d / "README").write_text(f"{name}\n")
        self.git(d, "add", "-A")
        self.git(d, "commit", "-q", "-m", f"{name}: first")
        return d

    def branch(self, d):
        return self.sh("git", "symbolic-ref", "-q", "--short", "HEAD", cwd=d, check=False).stdout.strip() or None

    def head(self, d):
        return self.git(d, "rev-parse", "HEAD")

    def pin(self, top, path):
        return self.git(top, "rev-parse", f"HEAD:{path}")

    def clean(self, top):
        return self.git(top, "status", "--porcelain", "--ignore-submodules=dirty") == ""

    def tool_with_history(self):
        """A copy of the tool whose history holds an older template/AGENTS.md, then the current one.
        `upgrade` recognises past template versions by the tool's history, which a fresh or shallow
        clone of cudl doesn't have; returns the copy and the older AGENTS.md."""
        tool, src = self.root / "tool", CUDL.parent.parent
        tool.mkdir()
        for d in ("bin", "template", "skills"):
            self.sh("cp", "-r", str(src / d), str(tool / d))
        self.git(tool, "init", "-q")
        current, older = (tool / "template/AGENTS.md").read_text(), "# AGENTS.md as an older cudl wrote it\n"
        (tool / "template/AGENTS.md").write_text(older)
        self.git(tool, "add", "-A")
        self.git(tool, "commit", "-q", "-m", "an older template")
        (tool / "template/AGENTS.md").write_text(current)
        self.git(tool, "commit", "-q", "-am", "the current template")
        return tool, older

    def fake_history(self):
        """Claude Code and Codex transcripts for /x/client, a worktree below it, and a sibling."""
        projects, rollouts = self.root / "claude-projects", self.root / "codex-sessions"

        def claude(cwd, sid, records):
            d = projects / re.sub(r"[^A-Za-z0-9]", "-", cwd)
            d.mkdir(parents=True, exist_ok=True)
            base = {"cwd": cwd, "sessionId": sid, "gitBranch": "dial-fix"}
            lines = [{"type": "mode", "sessionId": sid}] + [dict(base, **r) for r in records]
            (d / f"{sid}.jsonl").write_text("".join(json.dumps(l) + "\n" for l in lines))

        def user(text, ts, **kw):
            return {"type": "user", "timestamp": ts, "message": {"role": "user", "content": text}, **kw}

        claude("/x/client", "aaaa1111-0000", [
            user("<command-name>/model</command-name>", "2026-08-01T10:00:00Z"),
            user("hidden", "2026-08-01T10:00:01Z", isMeta=True),
            user("Bound the dial deadline on the dial-fix branch", "2026-08-01T10:00:02Z"),
            {"type": "assistant", "timestamp": "2026-08-01T12:30:00Z"},
        ])
        claude("/x/client/.claude/worktrees/hedge", "bbbb2222-0000", [
            user("This session is being continued from a previous conversation that ran out of context.",
                 "2026-08-02T09:00:00Z"),
        ])
        claude("/x/client-old", "cccc3333-0000", [user("not this one", "2026-08-03T09:00:00Z")])

        def codex(cwd, sid, prompt, day):
            d = rollouts / "2026" / "08" / day
            d.mkdir(parents=True, exist_ok=True)
            lines = [
                {"type": "session_meta", "timestamp": f"2026-08-{day}T07:00:00Z",
                 "payload": {"id": sid, "cwd": cwd, "git": {"branch": "variant-a"}}},
                {"type": "response_item", "timestamp": f"2026-08-{day}T07:00:01Z",
                 "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "<environment_context>…"}]}},
                {"type": "response_item", "timestamp": f"2026-08-{day}T07:00:02Z",
                 "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": prompt}]}},
                {"type": "event_msg", "timestamp": f"2026-08-{day}T07:20:00Z", "payload": {"type": "task_complete"}},
            ]
            (d / f"rollout-2026-08-{day}T07-00-00-{sid}.jsonl").write_text("".join(json.dumps(l) + "\n" for l in lines))

        codex("/x/client", "dddd4444-0000-0000-0000-000000000000", "Review the draft post in notes/", "04")
        codex("/x/clientele", "eeee5555-0000-0000-0000-000000000000", "not this one either", "05")
        return ["--claude-projects", str(projects), "--codex-sessions", str(rollouts)]

    def mixed_history(self):
        """In code/alpha (branch dev): code commits interleaved with notes/ commits, a merge, a
        deletion and a rename. Returns the original commits that touched notes/, oldest first."""
        a = self.w / "code/alpha"
        n = [0]

        def commit(msg, files=(), remove=()):
            n[0] += 1
            for name, text in files:
                (a / name).parent.mkdir(parents=True, exist_ok=True)
                (a / name).write_text(text)
            for name in remove:
                self.git(a, "rm", "-q", name)
            self.git(a, "add", "-A")
            env = dict(self.env, GIT_AUTHOR_DATE=f"2026-08-{n[0]:02d}T10:00:00Z",
                       GIT_AUTHOR_NAME="Author", GIT_AUTHOR_EMAIL="author@example.org")
            subprocess.run(["git", "commit", "-q", "-m", msg], cwd=a, env=env, check=True)
            return self.head(a)

        notes = []
        commit("code: one", [("src.go", "1\n")])
        notes.append(commit("notes: first finding", [("notes/a.md", "a1\n")]))
        commit("code: two", [("src.go", "2\n")])
        notes.append(commit("code and notes: three", [("src.go", "3\n"), ("notes/a.md", "a2\n")]))
        self.git(a, "switch", "-q", "-c", "side")
        notes.append(commit("notes: side result", [("notes/b.md", "b\n")]))
        self.git(a, "switch", "-q", "dev")
        commit("code: four", [("src.go", "4\n")])
        notes.append(commit("notes: meanwhile on dev", [("notes/c.md", "c\n")]))
        self.git(a, "merge", "-q", "--no-ff", "-m", "merge side", "side")
        notes.append(self.head(a))
        notes.append(commit("notes: drop b", remove=["notes/b.md"]))
        notes.append(commit("notes: move a", [("notes/sub/a.md", "a2\n")], remove=["notes/a.md"]))
        commit("code: five", [("src.go", "5\n")])
        self.cudl("commit", "-m", "pin alpha")  # the lab records alpha's current HEAD
        return notes

    def split_commits(self, ref):
        """(subject, Split-from, pin of code/alpha) for each lab commit that came from the split."""
        out = []
        for sha in self.git(self.w, "log", "--format=%H", ref).split():
            body = self.git(self.w, "log", "-1", "--format=%B", sha)
            m = re.search(r"^Split-from: ([0-9a-f]{40})$", body, re.M)
            if m:
                pin = self.git(self.w, "rev-parse", f"{sha}:code/alpha")
                out.append((body.splitlines()[0], m.group(1), pin))
        return out[::-1]

    # --- a helper, not a gatekeeper ---

    def session(self, event, sid, top, **kw):
        return self.cudl("hook", event, input=json.dumps({"session_id": sid, "cwd": str(top), **kw}),
                         cwd=top, env={"CLAUDE_CODE_SESSION_ID": sid})

    # --- pin retention, squash, stacked features ---

    def work(self, top, repo, name, text, msg):
        (top / "code" / repo / name).write_text(text)
        self.cudl("commit", "-A", "-m", msg, cwd=top)
        return self.head(top / "code" / repo)

    def gc(self, repo="alpha"):
        """Drop everything unreachable from refs in the shared code repo, reflogs included."""
        d = self.w / "code" / repo
        self.git(d, "reflog", "expire", "--expire=now", "--all")
        self.git(d, "gc", "-q", "--prune=now")

    def keep_refs(self, repo="alpha"):
        return set(self.git(self.w / "code" / repo, "for-each-ref", "--format=%(refname:lstrip=3)",
                            "refs/cudl/keep/").split())

    # rewritten parents: sync --rebase, precise parent state, finish --stack

    def stack(self, *names, repo="alpha"):
        """A chain of features, each stacked on the one before, each with one commit of its own."""
        tips = {}
        for i, n in enumerate(names):
            self.cudl("new", n, *(["--from", names[i - 1]] if i else ["-r", repo]))
            tips[n] = self.work(self.w / "wt" / n, repo, f"{n}1", "1\n", f"{n} one")
        return tips

    # rewritten parents: the code review

    def fail_commits(self, repo_dir, flag):
        """A commit-msg hook in repo_dir's git dir that fails while `flag` exists."""
        hooks = Path(self.git(repo_dir, "rev-parse", "--path-format=absolute", "--git-path", "hooks"))
        hooks.mkdir(exist_ok=True)
        (hooks / "commit-msg").write_text(f"#!/bin/sh\n[ -e '{flag}' ] && exit 1\nexit 0\n")
        (hooks / "commit-msg").chmod(0o755)
        flag.write_text("")

    # --- the live checks' fixes ---

    def start(self, sid, top=None, agent="claude", **env):
        """A session start in `top` with these session variables in its environment."""
        args = ["hook", "session-start"] + (["--agent", agent] if agent != "claude" else [])
        return self.cudl(*args, input=json.dumps({"session_id": sid, "cwd": str(top or self.w)}), env=env)

    def logged(self, sid, top=None):
        return read_fm(next(((top or self.w) / "journal/sessions").glob(f"*-{sid[:8]}*.md")).read_text())

    # --- worktree exits, notices, the memory index ---

    def fake_signer(self, works=True):
        """git config (as GIT_CONFIG_* variables) that signs commits with a stand-in for ssh-keygen."""
        signer = self.root / ("signer" if works else "broken-signer")
        signer.write_text("#!/bin/sh\nfor last; do :; done\n"
                          "printf -- '-----BEGIN SSH SIGNATURE-----\\nZmFrZQ==\\n-----END SSH SIGNATURE-----\\n' "
                          "> \"$last.sig\"\n" if works else "#!/bin/sh\necho 'no agent' >&2\nexit 1\n")
        signer.chmod(0o755)
        cfg = {"commit.gpgsign": "true", "gpg.format": "ssh", "gpg.ssh.program": str(signer),
               "user.signingkey": "key::ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGZha2VmYWtlZmFrZWZha2VmYWtlZmFrZWZha2VmYWtl t"}
        env = {"GIT_CONFIG_COUNT": str(len(cfg))}
        for i, (k, v) in enumerate(cfg.items()):
            env[f"GIT_CONFIG_KEY_{i}"], env[f"GIT_CONFIG_VALUE_{i}"] = k, v
        return env

    # `claude -w`: Claude Code runs WorktreeCreate, the session starts in the worktree; on exit with
    # "Remove worktree" WorktreeRemove runs first, then SessionEnd, in the main lab

    def exchange(self, sid, key="-launch"):
        """A transcript where Claude Code keeps it (by id, under the fake HOME), showing an exchange."""
        t = self.root / ".claude/projects" / key / f"{sid}.jsonl"
        t.parent.mkdir(parents=True, exist_ok=True)
        t.write_text(json.dumps({"type": "user", "message": {"role": "user", "content": "work"}}) + "\n")
        return t

    def claude_w(self, name, sid, **env):
        out = self.cudl("hook", "worktree-create", input=json.dumps(
            {"session_id": sid, "worktree_name": name, "cwd": str(self.w)}),
            env={"CLAUDE_CODE_SESSION_ID": sid, **env}).stdout
        top = Path(out.strip())
        self.cudl("hook", "session-start", input=json.dumps({"session_id": sid, "cwd": str(top)}), cwd=top,
                  env={"CLAUDE_CODE_SESSION_ID": sid, **env})
        return top

    def remove_worktree(self, sid, top, check=True, **kw):
        return self.cudl("hook", "worktree-remove", input=json.dumps(
            {"session_id": sid, "worktree_path": str(top), "cwd": str(top), **kw}),
            env={"CLAUDE_CODE_SESSION_ID": sid}, check=check)

    def abandoned(self, d, feat):
        return self.git(d, "branch", "--list", f"abandoned/{feat}-*", "--format=%(refname:short)")

    def notices(self):
        return self.start("0b0b0000-0000-4000-8000-000000000000").stdout

    # --- what the session design review found ---

    def compacted_transcript(self, sid, n, key="-launch"):
        """A Claude transcript with an exchange, n compactions, and a prompt that only quotes one."""
        t = self.exchange(sid, key)
        with open(t, "a") as f:
            for _ in range(n):
                f.write(json.dumps({"type": "system", "subtype": "compact_boundary",
                                    "compactMetadata": {"trigger": "auto"}}) + "\n")
                f.write(json.dumps({"type": "user", "isCompactSummary": True,
                                    "message": {"role": "user", "content": "This session is being continued"}}) + "\n")
            f.write(json.dumps({"type": "user", "message": {
                "role": "user", "content": '{"type": "system", "subtype": "compact_boundary"} is how one looks'}}) + "\n")
        return t

    def session_in_a_finished_feature(self, s, name="f"):
        """Session s runs in main and works in feature `name` with -f; the user finishes the feature."""
        env = {"CLAUDE_CODE_SESSION_ID": s}
        self.start(s, CLAUDE_CODE_SESSION_ID=s)
        self.cudl("new", name, "-r", "alpha", env=env)
        (self.w / f"wt/{name}/code/alpha/x").write_text("x\n")
        self.cudl("commit", "-f", name, "-A", "-m", "work", env=env)
        return self.cudl("finish", name)
