# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Csaba Kiraly
"""A lab and its repos: the marker, worktrees, pins, keep refs and the git hooks that keep them."""

import json
import os
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path

from cudl.core import (
    CudlError, GIT, MARKER, NAME_RE, git, git_ok, head_branch, head_sha, is_checkout, pinned,
    porcelain, run, short,
)


@dataclass
class Repo:
    name: str
    path: str
    branch: str  # integration branch


class Lab:
    def __init__(self, top):
        self.top = Path(top).resolve()
        common = Path(git(self.top, "rev-parse", "--path-format=absolute", "--git-common-dir"))
        self.main = common.parent
        self.feature = None if self.top == self.main else self.top.name

    @classmethod
    def find(cls, start=None):
        """The lab around `start`: the nearest directory with the marker and a git repository, except a
        code repo's checkout, which can carry any file: a submodule, when there is a lab further up."""
        p = Path(start or os.getcwd()).resolve()
        tops = [d for d in [p, *p.parents] if (d / MARKER).is_file() and (d / ".git").exists()]
        for d in tops[:-1]:
            if not git(d, "rev-parse", "--show-superproject-working-tree", check=False):
                return cls(d)
        if tops:
            return cls(tops[-1])
        raise CudlError(f"not inside a cudl (no {MARKER} above {p})")

    @property
    def label(self):
        return self.feature or "main"

    def repos(self):
        gm = self.top / ".gitmodules"
        if not gm.exists():
            return {}
        out = git(self.top, "config", "-f", str(gm), "--get-regexp", r"^submodule\..*\.path$", check=False)
        repos = {}
        for line in out.splitlines():
            key, path = line.split(" ", 1)
            name = key[len("submodule."):-len(".path")]
            branch = git(self.top, "config", "-f", str(gm), "--get", f"submodule.{name}.branch", check=False)
            repos[name] = Repo(name, path, branch or None)
        return repos

    def features(self):
        out = git(self.main, "worktree", "list", "--porcelain")
        wt = self.main / "wt"
        paths = [Path(l[len("worktree "):]) for l in out.splitlines() if l.startswith("worktree ")]
        return sorted(p.name for p in paths if p.parent == wt and NAME_RE.match(p.name))  # not one made by hand

    def feature_top(self, feat):
        if feat != "main" and not NAME_RE.match(feat):  # a path given as a name would leave wt/
            raise CudlError(f"{feat!r} is not a feature name")
        return self.main if feat == "main" else self.main / "wt" / feat


def base_of(d, branch, repo):
    return git(d, "config", f"branch.{branch}.cudlbase", check=False) or repo.branch


def base_problem(d, repo, base):
    """Why `base` can't be a feature's base in repo (checked out at d), or None. A base is a local branch:
    the one the feature syncs from and finishes into (round 1 took any name, and `sync` then stopped)."""
    if git_ok(d, "show-ref", "--verify", "--quiet", f"refs/heads/{base}"):  # exactly a branch: not dev~0
        return None
    if git_ok(d, "show-ref", "--verify", "--quiet", f"refs/remotes/{base}"):
        return (f"{base} is a remote-tracking branch, and a feature's base is a local branch, the one it syncs "
                f"from and finishes into (git -C {d} branch <name> {base} makes one)")
    return f"{repo.name} has no branch {base}"


# Every code commit a lab commit pins gets a ref in the code repo, refs/cudl/keep/<sha>, so that no
# rewrite of a code branch (squash, rebase, amend, reset) can let git gc drop a commit that the
# lab's history points at. Refs, not reachability arguments: pins outlive the branches and
# features that made them. `cudl push` names branches, so these never leave the machine.

KEEP = "refs/cudl/keep/"


def pins_in(top, rev_range):
    """{code path: {sha}} for every gitlink under code/ written by the lab commits in rev_range,
    merges included (-m diffs a merge against each parent, so a resolved pin shows up)."""
    out = git(top, "log", "-m", "--raw", "--no-abbrev", "--no-renames", "--format=", *rev_range,
              "--", "code/", check=False)
    pins = {}
    for line in out.splitlines():
        if not line.startswith(":"):
            continue
        meta, _, path = line.partition("\t")
        _, new_mode, _, new_sha, _ = meta[1:].split()
        if new_mode == "160000":
            pins.setdefault(path, set()).add(new_sha)
    return pins


def repo_paths(lab, more=False):
    """The paths of the lab's code repos: main's, and with `more` also those of every live feature (a
    repo taken out of main can still be in one)."""
    main = Lab(lab.main)
    paths = {r.path for r in main.repos().values()}
    for f in main.features() if more else []:
        paths |= {r.path for r in Lab(main.feature_top(f)).repos().values()}
    return paths


def keep_pins(lab, pins, check=False):
    """Make sure each pin has its keep ref in its code repo; returns (kept, new, missing)."""
    kept = new = 0
    missing = []
    paths, every = repo_paths(lab), None
    for path, shas in sorted(pins.items()):
        d = lab.main / path
        if path not in paths:
            every = every or repo_paths(lab, more=True)  # the features' too, only when it's needed
            if path not in every:
                continue  # a repo taken out of the lab: its pins have nowhere to be kept
        if not is_checkout(d):
            missing += [(path, s) for s in shas]
            continue
        if not check and git(d, "config", "--get", "log.excludeDecoration", check=False) != f"{KEEP}*":
            git(d, "config", "log.excludeDecoration", f"{KEEP}*", check=False)  # keep refs out of git log
        have = set(git(d, "for-each-ref", "--format=%(refname:lstrip=3)", KEEP).split())
        todo = sorted(shas - have)
        kept += len(shas) - len(todo)
        if not todo:
            continue
        # one answer per line: the type, or "<sha> missing" for an object that's gone
        found = run(GIT + ["cat-file", "--batch-check=%(objecttype)"], d,
                    input="\n".join(todo) + "\n").stdout.splitlines()
        types = ["missing" if t.endswith(" missing") else t.strip() for t in found]
        present = [s for s, t in zip(todo, types) if t == "commit"]
        missing += [(path, s) for s, t in zip(todo, types) if t != "commit"]
        if present and not check:
            # `update` without an old value is idempotent: two sessions keeping the same pin both succeed
            run(GIT + ["update-ref", "--stdin"], d, input="".join(f"update {KEEP}{s} {s}\n" for s in present))
            new += len(present)
        elif present:
            missing += [(path, s) for s in present]  # --check: no ref yet
    return kept, new, missing


def keep_since(lab, old):
    """Keep the pins of every lab commit made since `old` (the lab's HEAD before a command)."""
    rng = [f"{old}..HEAD"] if old else ["HEAD"]
    _, _, missing = keep_pins(lab, pins_in(lab.top, rng))
    if missing:
        print(f"warning: {len(missing)} pinned commits are not in their code repos "
              f"(cudl keep --check lists them)", file=sys.stderr)


LAB_HOOKS = ("post-commit", "post-merge", "post-rewrite")
HOOK_MARK = "# managed by cudl"


def hooks_dir(lab):
    return Path(git(lab.main, "rev-parse", "--path-format=absolute", "--git-common-dir")) / "hooks"


def install_lab_hooks(lab):
    """Git hooks in the lab's common git dir (shared by every worktree) that keep the pins of every
    new lab commit, however it was made. An existing hook is chained, not replaced."""
    if git(lab.main, "config", "--get", "core.hooksPath", check=False):
        print("note: core.hooksPath is set, so cudl's lab hooks are not active; run `cudl keep` after "
              "committing in the lab with plain git", file=sys.stderr)
        return
    d = hooks_dir(lab)
    d.mkdir(parents=True, exist_ok=True)
    runner = lab.main / ".cudl" / "cudl"  # the main lab's copy: upgraded in place, unlike old worktrees'
    for name in LAB_HOOKS:
        h = d / name
        if h.exists() and HOOK_MARK not in h.read_text(errors="replace"):
            h.rename(d / f"{name}.pre-cudl")
        h.write_text(f"""#!/bin/sh
{HOOK_MARK}: keep every code commit the lab pins (refs/cudl/keep/<sha>). Chains {name}.pre-cudl.
input=$(mktemp) || exit 0
trap 'rm -f "$input"' EXIT
{'cat > "$input"  # the rewritten commits, "old new" per line, passed on byte for byte' if name == "post-rewrite" else ': # this hook gets no stdin'}
status=0
if [ -x "$0.pre-cudl" ]; then "$0.pre-cudl" "$@" < "$input"; status=$?; fi
env -u GIT_DIR -u GIT_WORK_TREE -u GIT_INDEX_FILE -u GIT_PREFIX {shlex.quote(str(runner))} keep --hook {name} "$@" \
    < "$input" > /dev/null 2>&1 || echo "cudl: keeping this commit's pins failed; run cudl keep" >&2
exit $status
""")
        h.chmod(0o755)


def hook_range(name, args, stdin):
    """The lab commits a git hook invocation brought in."""
    if name == "post-rewrite":
        new = [l.split()[1] for l in stdin.splitlines() if len(l.split()) >= 2]
        return ["--no-walk", *new] if new else []
    if name == "post-merge":
        return ["ORIG_HEAD..HEAD"] if git_ok(".", "rev-parse", "-q", "--verify", "ORIG_HEAD") else ["-1", "HEAD"]
    return ["-1", "HEAD"]


def cmd_keep(a):
    w = Lab.find()
    if a.hook:
        rng = hook_range(a.hook, a.args, "" if sys.stdin.isatty() else sys.stdin.read())
        missing = keep_pins(w, pins_in(w.top, rng))[2] if rng else []
        if missing:
            print(f"cudl: this lab commit pins {len(missing)} commits missing from their code repos", file=sys.stderr)
            sys.exit(1)  # the hook turns this into a visible warning
        return
    main = Lab(w.main)
    pins = pins_in(main.top, ["--all"])
    paths = repo_paths(main, more=True)
    gone = sorted(p for p in pins if p not in paths)  # repos taken out of the lab since
    kept, new, missing = keep_pins(main, pins, check=a.check)
    total = sum(len(s) for p, s in pins.items() if p in paths)
    lost = [m for m in missing if is_checkout(main.main / m[0])
            and not git_ok(main.main / m[0], "cat-file", "-e", f"{m[1]}^{{commit}}")]
    unkept = [m for m in missing if m not in lost]
    print(f"{total} pins in the lab's history: {kept + new} kept"
          + (f" ({new} newly)" if new else "")
          + (f", {len(unkept)} without a keep ref" if unkept else "")
          + (f", {len(lost)} lost (no longer in the code repo)" if lost else "")
          + (f"; {sum(len(pins[p]) for p in gone)} more of repos no longer in the lab ({', '.join(gone)})"
             if gone else ""))
    for path, sha in (unkept + lost)[:20]:
        print(f"  {path} {sha} {'lost' if (path, sha) in lost else 'not kept'}")
    if a.check and missing:
        sys.exit(1)


def require_main(w):
    if w.feature:
        raise CudlError(f"run this in the main lab ({w.main}), not in feature {w.feature}")


def require_feature(w):
    if not w.feature:
        raise CudlError("run this inside a feature worktree (wt/<feature>/)")


def validate_name(name):
    if not NAME_RE.match(name) or name == "main":
        raise CudlError(f"bad feature name {name!r}: use letters, digits, . _ - and not 'main'")


def setup_local(w):
    """Per-worktree, per-machine settings for Claude Code, and the merge driver.

    autoMemoryDirectory must be absolute (and is ignored in the checked-in settings.json), so it is
    written here per worktree; plansDirectory is relative to the project root, so "plans" means
    this worktree's own plans/.
    """
    f = w.top / ".claude" / "settings.local.json"
    data = json.loads(f.read_text()) if f.exists() else {}
    data["autoMemoryDirectory"] = str(w.main / "memory")
    data["plansDirectory"] = "plans"
    f.parent.mkdir(exist_ok=True)
    f.write_text(json.dumps(data, indent=2) + "\n")
    git(w.top, "config", "merge.ours.driver", "true")


def lab_tops(lab):
    return [lab.main] + [lab.main / "wt" / f for f in lab.features()]


def repo_line(top, r):
    d = top / r.path
    if not is_checkout(d):
        return f"{r.name}: not checked out"
    b = head_branch(d)
    head = head_sha(d)
    parts = []
    if b:
        parts.append(b)
        base = base_of(d, b, r)
        if b != base and git_ok(d, "rev-parse", "-q", "--verify", base):
            behind, ahead = git(d, "rev-list", "--left-right", "--count", f"{base}...{b}").split()
            parts.append(f"+{ahead}/-{behind} vs {base}")
    else:
        parts.append(f"pinned {short(head)} (read-only)")
    if pinned(top, r.path) != head:
        parts.append("unrecorded commits")
    n = len(porcelain(d))
    if n:
        parts.append(f"{n} changed")
    return f"{r.name}: " + ", ".join(parts)


def read_marker(root):
    return json.loads((root / MARKER).read_text())


def write_marker(root, data):
    (root / MARKER).write_text(json.dumps(data, indent=2) + "\n")
