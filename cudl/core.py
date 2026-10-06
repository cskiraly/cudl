# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Csaba Kiraly
"""Constants, errors, running git, commits of exact paths, the lab lock, small helpers."""

import fcntl
import os
import re
import shutil
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path


MARKER = ".cudl.json"
# cudl runs only as one file, bin/cudl or a lab's copy of it (.cudl/cudl), which tools/bundle.py builds
# from these modules: in any of them, __file__ is that file
TOOL_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = TOOL_ROOT / "template"
BOOTSTRAP_SKILL = TOOL_ROOT / "skills" / "cudl"
AGENTS = {"claude": "claude", "codex": "codex"}
GIT = ["git", "-c", "protocol.file.allow=always", "-c", "submodule.recurse=false"]
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


HANDOFF_LIMIT = 6000
MEMORY_INDEX_LIMIT = 4000
RECENT_SESSIONS = 10


class CudlError(Exception):
    pass


def run(cmd, cwd, check=True, input=None, env=None):
    p = subprocess.run(cmd, cwd=cwd, input=input, text=True, capture_output=True, env=env)
    if check and p.returncode != 0:
        msg = with_signing_diagnosis(cmd, cwd, "\n".join(x for x in (p.stdout.strip(), p.stderr.strip()) if x))
        raise CudlError(f"`{' '.join(cmd[len(GIT) - 1:] if cmd[:len(GIT)] == GIT else cmd)}` in {cwd}:\n{msg}")
    return p


def with_signing_diagnosis(cmd, cwd, msg):
    """git's message and, for a commit that couldn't be written, why signing failed."""
    if "commit" in cmd and ("failed to write commit object" in msg or "failed to sign" in msg):
        msg += "\n" + signing_diagnosis(cwd)
    return msg


def signing_diagnosis(cwd):
    try:
        return _signing_diagnosis(cwd)
    except Exception as e:  # a diagnosis must never hide git's own error
        return f"(couldn't diagnose commit signing: {e})"


def _signing_diagnosis(cwd):
    """Why a commit couldn't be written, when commit signing is on."""
    def cfg(key):
        return subprocess.run(["git", "config", "--get", key], cwd=cwd, text=True, capture_output=True,
                              timeout=5).stdout.strip()
    if cfg("commit.gpgsign").lower() not in ("true", "yes", "on", "1"):
        return "(commit signing is off, so it isn't signing)"
    fmt, key = cfg("gpg.format") or "openpgp", cfg("user.signingkey")
    if fmt != "ssh":
        return f"commit signing is on ({fmt}, key {key or 'default'}): check that gpg can sign (gpg-agent running, key available)"
    sock = os.environ.get("SSH_AUTH_SOCK")
    if not sock:
        return f"commit signing is on (ssh, key {key}) but SSH_AUTH_SOCK is not set: start an agent or ssh-add the key"
    if not Path(sock).exists():
        return (f"commit signing is on (ssh, key {key}) but the SSH agent socket {sock} no longer exists "
                f"(a forwarded agent went away?): point SSH_AUTH_SOCK at the live one, or ssh-add the key")
    if not shutil.which("ssh-add"):
        return f"commit signing is on (ssh, key {key}); ssh-add isn't installed, so the agent can't be checked"
    listed = subprocess.run(["ssh-add", "-L"], text=True, capture_output=True, timeout=5)
    if listed.returncode == 2:
        return f"commit signing is on (ssh, key {key}) but the agent at {sock} doesn't answer"
    if listed.returncode == 1:
        return f"commit signing is on (ssh, key {key}) but the agent at {sock} holds no keys: ssh-add the key"
    inline = key.removeprefix("key::") if key.startswith(("ssh-", "key::")) else None
    pub = Path(os.path.expanduser(key)) if key and not inline else None
    text = inline if inline else (pub.read_text() if pub and pub.is_file() else "")
    wanted = text.split()[1] if len(text.split()) > 1 else None
    if wanted and wanted not in listed.stdout:
        return f"commit signing is on (ssh) but the agent at {sock} doesn't hold the key {key}: ssh-add it"
    return f"commit signing is on (ssh, key {key}); the agent at {sock} answers, so the signing program itself failed"


def signing_on(cwd):
    return run(GIT + ["config", "--type=bool", "--get", "commit.gpgsign"], cwd, check=False).stdout.strip() == "true"


def check_signing(cwd, what):
    """With commit signing on, make sure the signer works before a command that writes in several steps
    and can't stop halfway gracefully: a probe commit of the empty tree (a dangling object)."""
    if not signing_on(cwd):
        return
    empty = run(GIT + ["hash-object", "-t", "tree", "/dev/null"], cwd).stdout.strip()
    p = run(GIT + ["commit-tree", "-S", empty, "-m", "cudl: signing probe"], cwd, check=False)
    if p.returncode != 0:
        raise CudlError(f"commit signing doesn't work, so {what}:\n{p.stderr.strip()}\n{signing_diagnosis(cwd)}")


def git(cwd, *args, check=True):
    return run(GIT + list(args), cwd, check).stdout.rstrip("\n")


def git_ok(cwd, *args):
    return run(GIT + list(args), cwd, check=False).returncode == 0


def git_retry(cwd, *args, tries=10):
    """Run a git command that may race another session for the index lock."""
    for i in range(tries):
        p = run(GIT + list(args), cwd, check=False)
        if p.returncode == 0:
            return p.stdout
        if "index.lock" not in p.stderr or i == tries - 1:
            raise CudlError(f"`git {' '.join(args)}` in {cwd}:\n{with_signing_diagnosis(args, cwd, p.stderr.strip())}")
        time.sleep(0.3)


def is_checkout(d):
    return (Path(d) / ".git").exists()


def head_branch(d):
    if not Path(d).is_dir():
        return None
    return run(GIT + ["symbolic-ref", "-q", "--short", "HEAD"], d, check=False).stdout.strip() or None


def head_sha(d):
    return git(d, "rev-parse", "HEAD")


def short(sha):
    return sha[:10] if sha else "-"


def porcelain(d, untracked=True):
    return git(d, "status", "--porcelain", "-uall" if untracked else "-uno").splitlines()


def lab_changes(top):
    """Lab changes, counting a submodule only when its HEAD moved, not when it is dirty."""
    return git(top, "status", "--porcelain", "-uall", "--ignore-submodules=dirty").splitlines()


def status_entries(top, *paths, literal=True, submodules="dirty"):
    """(X, Y, path) per changed path, as `git status` gives them: NUL-separated, so a path is never
    quoted; paths taken literally (a `*` in a file name is no wildcard) unless they are a user's
    pathspecs (literal=False); renames as their two sides, so each side is judged where it is. A
    submodule counts when its HEAD moved, or (submodules="none") when it has any change."""
    out = run(GIT + (["--literal-pathspecs"] if literal else []) + [
        "status", "--porcelain=v1", "-z", "--no-renames", "-uall", f"--ignore-submodules={submodules}", "--",
        *paths], top).stdout
    return [(e[0], e[1], e[3:]) for e in out.split("\0") if e]


def changed_paths(top, *paths, literal=True):
    """{path: [(X, Y), …]} for the changed paths among these files or directories. A path git lists
    twice is a staged deletion whose file is back as untracked (`git rm --cached`)."""
    out = {}
    for x, y, path in status_entries(top, *paths, literal=literal):
        out.setdefault(path, []).append((x, y))
    return out


def split_path(states):
    """The index differs from HEAD and from the working tree (staged, then changed again; or removed
    from the index with the file still there). Committing the path would lose one of the two, so
    cudl never stages or commits it: it's for whoever staged it."""
    return len(states) > 1 or (states[0][0] not in " ?" and states[0][1] not in " ?")


def commit_selected(top, take, states, msg, extra=()):
    """A commit of exactly these paths, nothing else that is staged: a pathspec commit takes their
    content from the working tree, so only new files are staged first, and unstaged again if the
    commit fails (a hook, signing). Returns whether it committed."""
    if not take:
        return False
    new = [p for p in take if states[p][0][0] == "?"]
    if new:
        git_retry(top, "--literal-pathspecs", "add", "--", *new)
    try:
        if not new and git_ok(top, "--literal-pathspecs", "diff", "--quiet", "HEAD", "--", *take):
            return False  # nothing a pathspec commit would record
        git_retry(top, "--literal-pathspecs", "commit", "-q", "-m", msg, *extra, "--", *take)
    except CudlError:
        if new:
            run(GIT + ["--literal-pathspecs", "reset", "-q", "--", *new], top, check=False)
        raise
    return True


def has_staged(d):
    return not git_ok(d, "diff", "--cached", "--quiet")


def pinned(top, path):
    return run(GIT + ["rev-parse", f"HEAD:{path}"], top, check=False).stdout.strip() or None


def is_ancestor(d, a, b):
    return git_ok(d, "merge-base", "--is-ancestor", a, b)


def in_progress(d):
    gd = Path(git(d, "rev-parse", "--path-format=absolute", "--git-dir"))
    for name, what in (("rebase-merge", "a rebase"), ("rebase-apply", "a rebase"),
                       ("CHERRY_PICK_HEAD", "a cherry-pick")):
        if (gd / name).exists():
            return what
    return None


def busy(d):
    """Any git operation in progress, a merge or revert included (in_progress leaves merges alone,
    since `cudl commit` concludes a merge)."""
    gd = Path(git(d, "rev-parse", "--path-format=absolute", "--git-dir"))
    return in_progress(d) or next((w for n, w in (("MERGE_HEAD", "a merge"), ("REVERT_HEAD", "a revert"))
                                   if (gd / n).exists()), None)


@contextmanager
def lab_lock(lab):
    """Serialise cudl's own commits in a lab (hooks and commands of concurrent sessions)."""
    f = open(Path(git(lab.main, "rev-parse", "--path-format=absolute", "--git-common-dir")) / "cudl.lock", "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(f, fcntl.LOCK_UN)
        f.close()


def conflicted(d):
    return git(d, "diff", "--name-only", "--diff-filter=U").splitlines()


def pinned_head(top):
    return run(GIT + ["rev-parse", "-q", "--verify", "HEAD"], top, check=False).stdout.strip()


def tree(d, rev):
    return git(d, "rev-parse", f"{rev}^{{tree}}")


def mib(n):
    return f"{n / 2**20:.1f} MiB"


def one_line(text, limit=150):
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def under(cwd, roots):
    return any(cwd == r or cwd.startswith(r + "/") for r in roots)


def common_dir(lab):
    return Path(git(lab.main, "rev-parse", "--path-format=absolute", "--git-common-dir"))
