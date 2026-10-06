# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Csaba Kiraly
"""Moving existing work in: import-sessions, split, clean-branch."""

import datetime
import json
import os
import re
import shutil
import tempfile
from pathlib import Path

from cudl.core import (
    CudlError, GIT, MARKER, check_signing, conflicted, git, git_ok, git_retry, head_sha,
    is_checkout, lab_changes, mib, one_line, run, short, signing_on, under,
)
from cudl.lab import Lab, keep_pins, pins_in, require_main
from cudl.journal import build_index, session_file, session_path, sessions_dir, write_frontmatter
from cudl.sessions import SESSION_ID_RE, claude_prompt, read_head_tail


SPLITS = Path(".cudl") / "splits.json"
SPLIT_TRAILER = "Split-from"
# Destinations an import must never write: the lab's own machinery
RESERVED = (".gitmodules", ".git", ".cudl/", "code/", "wt/", ".claude/", ".codex/", ".agents/",
            "journal/INDEX.md", MARKER)


def clean_env():
    """The environment for git in a scratch repo: nothing that points git at another repository."""
    env = dict(os.environ)
    for k in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY", "GIT_COMMON_DIR",
              "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_NAMESPACE", "GIT_PREFIX"):
        env.pop(k, None)
    return env


def xgit(cwd, *args, check=True):
    """git in a scratch repository, with a clean environment."""
    return run(GIT + list(args), cwd, check, env=clean_env()).stdout.rstrip("\n")


def parse_pairs(items, what):
    pairs = []
    for item in items or []:
        old, sep, new = item.partition("=")
        if not sep or not old:
            raise CudlError(f"{what} {item!r}: use OLD=NEW")
        pairs.append((old, new))
    return pairs


def renamed(path, renames):
    for old, new in renames:
        if path == old.rstrip("/") or path.startswith(old if old.endswith("/") else old + "/"):
            return new + path[len(old):]
    return path


def in_paths(path, paths):
    return any(path == p.rstrip("/") or path.startswith(p.rstrip("/") + "/") for p in paths)


def load_splits(top):
    f = top / SPLITS
    return json.loads(f.read_text()) if f.exists() else {"imports": []}


def placeholders(top, files):
    """Lab files that only hold a place, which an import may replace: `.gitkeep`s, and the empty memory
    index the template ships."""
    held = {f for f in files if f.endswith("/.gitkeep")}
    if "memory/MEMORY.md" in files and git(top, "cat-file", "-s", "HEAD:memory/MEMORY.md", check=False) == "0":
        held.add("memory/MEMORY.md")
    return held


def split_survey(src, branch, paths, renames, lab_files):
    """Read-only facts about one branch's artifact history."""
    tip = git(src, "rev-parse", f"refs/heads/{branch}")
    commits = git(src, "rev-list", tip, "--", *paths).splitlines()
    files = []
    for line in git(src, "ls-tree", "-r", "-l", tip, "--", *paths).splitlines():
        meta, name = line.split("\t", 1)
        size = meta.split()[3]
        files.append((name, int(size) if size.isdigit() else 0))
    blobs = {}
    for line in git(src, "rev-list", "--objects", tip, "--", *paths).splitlines():
        sha, _, name = line.partition(" ")
        if name and in_paths(name, paths):
            blobs[sha] = name
    sizes = run(GIT + ["cat-file", "--batch-check=%(objecttype) %(objectsize)"], src,
                input="\n".join(blobs) + "\n").stdout.split("\n") if blobs else []
    blob_sizes = [int(s.split()[1]) for s in sizes if s.startswith("blob ")]
    unpushed = git(src, "rev-list", "--count", tip, "--not", "--remotes", "--", *paths)
    dests = {renamed(n, renames) for n, _ in files}
    collisions = sorted(d for d in dests if d in lab_files)
    reserved = sorted(d for d in dests if any(d == r.rstrip("/") or d.startswith(r) for r in RESERVED))
    dates = git(src, "log", "--format=%ad", "--date=short", tip, "--", *paths).splitlines() if commits else []
    return {
        "branch": branch, "tip": tip, "commits": len(commits), "files": len(files),
        "tip_bytes": sum(s for _, s in files), "versions": len(blob_sizes),
        "history_bytes": sum(blob_sizes), "largest": max(blob_sizes, default=0),
        "unpushed": int(unpushed), "collisions": collisions, "reserved": reserved,
        "first": dates[-1] if dates else "", "last": dates[0] if dates else "",
    }


PASS1 = """
msg = commit.message.rstrip(b"\\n")
commit.message = msg + (b"\\n\\n" if msg else b"") + b"%s: " + commit.original_id + b"\\n"
""" % SPLIT_TRAILER

PASS2 = """
import re
m = re.search(rb"^%s: ([0-9a-f]{40})$", commit.message, re.M)
if m:
    commit.file_changes.append(FileChange(b"M", b"%%s", m.group(1), b"160000"))
if not commit.parents:
    commit.file_changes.append(FileChange(b"M", b".gitmodules", b"%%s", b"100644"))
""" % SPLIT_TRAILER


def cmd_split(a):
    w = Lab.find()
    require_main(w)
    repos = w.repos()
    if a.repo not in repos:
        raise CudlError(f"unknown repo {a.repo}; known: {', '.join(repos) or 'none'}")
    r = repos[a.repo]
    src = w.top / r.path
    if not is_checkout(src):
        raise CudlError(f"{r.path} is not checked out")
    paths = [p.rstrip("/") + ("/" if p.endswith("/") else "") for p in a.path]
    renames = parse_pairs(a.rename, "--rename")
    branches = parse_pairs([b if "=" in b else f"{b}=" for b in (a.branch or [r.branch])], "--branch")
    branches = [(cb, lb or (f"split/{r.name}/{cb}")) for cb, lb in branches]
    merge = a.merge or r.branch
    if merge not in [cb for cb, _ in branches] and not a.no_merge:
        raise CudlError(f"--merge {merge} is not among the imported branches")
    done = [(i["repo"], b) for i in load_splits(w.top)["imports"] for b in i["branches"]]
    for cb, lb in branches:
        if (r.name, cb) in done:
            raise CudlError(f"{r.name} {cb} was already split into this lab (see {SPLITS})")
        if not git_ok(src, "rev-parse", "-q", "--verify", f"refs/heads/{cb}"):
            raise CudlError(f"no branch {cb} in {r.path}")
        if git_ok(w.top, "rev-parse", "-q", "--verify", f"refs/heads/{lb}"):
            raise CudlError(f"lab branch {lb} already exists")
    if lab_changes(w.top):
        raise CudlError("the lab has uncommitted changes: commit first (cudl commit)")

    lab_files = set(git(w.top, "ls-tree", "-r", "--name-only", "HEAD").splitlines())
    held = placeholders(w.top, lab_files)
    lab_files -= held
    surveys = [split_survey(src, cb, paths, renames, lab_files) for cb, _ in branches]
    for s, (cb, lb) in zip(surveys, branches):
        target = "lab main" if cb == merge and not a.no_merge else f"lab branch {lb}"
        print(f"{r.name} {cb} @ {short(s['tip'])} → {target}")
        print(f"  {s['commits']} commits touch {', '.join(paths)} ({s['first']} … {s['last']}); "
              f"{s['files']} files, {mib(s['tip_bytes'])} at the tip; "
              f"{s['versions']} versions, {mib(s['history_bytes'])} in all, largest {mib(s['largest'])}")
        if s["unpushed"]:
            print(f"  warning: {s['unpushed']} of these commits are on no remote-tracking branch: a clone "
                  f"of the lab can't check out their pins until {r.name} is pushed")
    problems = sorted({f"lands on the lab's {d}" for s in surveys for d in s["reserved"]} |
                      {f"collides with the lab's {d}" for s in surveys for d in s["collisions"]})
    if problems:
        raise CudlError("nothing imported: artifact paths\n  " + "\n  ".join(problems[:20]) +
                        "\nuse --rename to move them")
    if not any(s["commits"] for s in surveys):
        raise CudlError(f"no commits touch {', '.join(paths)}")
    if a.dry_run:
        print("(dry run: nothing written)")
        return

    if not shutil.which("git-filter-repo"):
        raise CudlError("git-filter-repo is needed for split (apt install git-filter-repo)")
    check_signing(w.top, "nothing was imported")
    scratch = Path(tempfile.mkdtemp(prefix="cudl-split-"))
    try:
        tmp = scratch / "repo.git"
        xgit(scratch, "init", "-q", "--bare", str(tmp))
        refspecs = [f"{s['tip']}:refs/heads/{cb}" for s, (cb, _) in zip(surveys, branches)]
        xgit(tmp, "fetch", "-q", "--no-tags", str(src), *refspecs)
        common = ["filter-repo", "--force", "--quiet", "--preserve-commit-hashes",
                  "--replace-refs", "delete-no-add"]
        pass1 = common + [x for p in paths for x in ("--path", p)]
        pass1 += [x for old, new in renames for x in ("--path-rename", f"{old}:{new}")]
        xgit(tmp, *pass1, "--commit-callback", PASS1)
        gitmodules = xgit(w.top, "show", "HEAD:.gitmodules")
        blob = run(GIT + ["hash-object", "-w", "--stdin"], tmp, input=gitmodules + "\n",
                   env=clean_env()).stdout.strip()
        callback = PASS2 % (r.path, blob)
        xgit(tmp, *common, "--prune-empty", "never", "--prune-degenerate", "never",
             "--commit-callback", callback)
        # every imported commit pins exactly the code commit it came from
        for cb, _ in branches:
            for line in xgit(tmp, "log", "--format=%H", cb).splitlines():
                msg = xgit(tmp, "log", "-1", "--format=%B", line)
                origin = re.search(rf"^{SPLIT_TRAILER}: ([0-9a-f]{{40}})$", msg, re.M)
                pin = xgit(tmp, "rev-parse", f"{line}:{r.path}", check=False)
                if not origin or pin != origin.group(1):
                    raise CudlError(f"split check failed at {line}: pin {pin} vs {origin and origin.group(1)}")
        fetch = [f"refs/heads/{cb}:refs/heads/{lb}" for cb, lb in branches]
        xgit(w.top, "fetch", "-q", "--no-tags", str(tmp), *fetch)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)

    record = {
        "repo": r.name, "source": str(src), "url": git(w.top, "config", "-f", ".gitmodules",
                                                     f"submodule.{r.name}.url", check=False),
        "date": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "paths": paths, "renames": [f"{o}={n}" for o, n in renames],
        "branches": {cb: {"tip": s["tip"], "lab_ref": lb, "lab_tip": head_sha_of(w.top, lb),
                          "commits": int(git(w.top, "rev-list", "--count", lb))}
                     for s, (cb, lb) in zip(surveys, branches)},
    }
    if not a.no_merge:
        lb = dict(branches)[merge]
        imported_pin = git(w.top, "rev-parse", f"{lb}:{r.path}")
        p = run(GIT + ["merge", "--no-ff", "--no-commit", "--allow-unrelated-histories", lb], w.top, check=False)
        for c in [c for c in conflicted(w.top) if c in held]:  # the import's file replaces a placeholder
            git(w.top, "checkout", "--theirs", "--", c)
            git(w.top, "add", "--", c)
        others = [c for c in conflicted(w.top) if c not in (".gitmodules", r.path)]
        if others or (p.returncode != 0 and "CONFLICT" not in p.stdout + p.stderr):
            run(GIT + ["merge", "--abort"], w.top, check=False)
            raise CudlError(f"merging {lb} into the lab failed: {', '.join(others) or p.stderr.strip()}\n"
                            f"the imported history is kept as branch {lb}")
        git(w.top, "checkout", "HEAD", "--", ".gitmodules")
        # The merge keeps the imported pin, and the imported history is its first parent: history
        # simplification follows the first parent that matches, so a plain `git log -- code/<repo>`
        # walks the imported history even when both sides pin the same commit. The next commit
        # moves the pin to the checked-out code.
        git(w.top, "update-index", "--cacheinfo", f"160000,{imported_pin},{r.path}")
        tree = git(w.top, "write-tree")
        msg = (f"cudl: split {r.name} {merge} history into the lab\n\n"
               f"Imported {record['branches'][merge]['commits']} commits touching {', '.join(paths)}.\n")
        sign = ["-S"] if signing_on(w.top) else []  # commit-tree doesn't read commit.gpgsign itself
        merged = run(GIT + ["commit-tree", *sign, tree, "-p", lb, "-p", "HEAD", "-F", "-"], w.top, input=msg).stdout.strip()
        git(w.top, "merge", "--quit")
        git(w.top, "update-ref", "-m", f"cudl split {r.name}", "HEAD", merged)
    splits = load_splits(w.top)
    splits["imports"].append(record)
    (w.top / SPLITS).write_text(json.dumps(splits, indent=2) + "\n")
    git(w.top, "update-index", "--cacheinfo", f"160000,{head_sha(src)},{r.path}")
    git(w.top, "add", str(SPLITS))
    git(w.top, "commit", "-q", "-m", f"cudl: record the {r.name} split; pin {r.name} at its checkout")
    keep_pins(w, pins_in(w.top, [lb for _, lb in branches] + ["HEAD"]))
    for cb, lb in branches:
        print(f"{r.name} {cb}: {record['branches'][cb]['commits']} commits as {lb}"
              + (", merged into main" if cb == merge and not a.no_merge else ""))
    print(f"recorded in {SPLITS}; the pins over time: git log -p -- {r.path}")


def head_sha_of(top, ref):
    return git(top, "rev-parse", ref)


CLEANS = Path(".cudl") / "clean.json"
ZERO = "0" * 40


def cmd_clean_branch(a):
    w = Lab.find()
    require_main(w)
    repos = w.repos()
    if a.repo not in repos:
        raise CudlError(f"unknown repo {a.repo}; known: {', '.join(repos) or 'none'}")
    r = repos[a.repo]
    src = w.top / r.path
    paths = a.path or sorted({p for i in load_splits(w.top)["imports"] if i["repo"] == r.name for p in i["paths"]})
    if not paths:
        raise CudlError(f"which paths? --path, or split {r.name} first so its paths are recorded")
    branch = a.branch
    target = a.as_ or f"{branch}-clean"
    if not git_ok(src, "rev-parse", "-q", "--verify", f"refs/heads/{branch}"):
        raise CudlError(f"no branch {branch} in {r.path}")
    if git_ok(src, "rev-parse", "-q", "--verify", f"refs/heads/{target}"):
        raise CudlError(f"branch {target} already exists in {r.path}")
    tip = git(src, "rev-parse", f"refs/heads/{branch}")

    # read-only survey: commits whose every change is under the paths vanish
    total, vanish, current, touched = 0, 0, None, False
    changed = []
    for line in git(src, "log", "--format=%x00%H", "--name-only", "--no-renames", tip).splitlines():
        if line.startswith("\0"):
            if current:
                changed.append((current, touched))
            current, touched = line[1:], []
        elif line:
            touched.append(line)
    if current:
        changed.append((current, touched))
    for sha, files in changed:
        total += 1
        if files and all(in_paths(f, paths) for f in files):
            vanish += 1
    at_tip = git(src, "ls-tree", "-r", "--name-only", tip, "--", *paths).splitlines()
    print(f"{r.name} {branch} @ {short(tip)} → {target}")
    print(f"  {total} commits; about {vanish} change only {', '.join(paths)} and vanish; "
          f"{len(at_tip)} files under those paths at the tip are removed from every commit")
    print(f"  {branch} itself is not changed")
    if a.dry_run:
        print("(dry run: nothing written)")
        return

    if not shutil.which("git-filter-repo"):
        raise CudlError("git-filter-repo is needed for clean-branch (apt install git-filter-repo)")
    scratch = Path(tempfile.mkdtemp(prefix="cudl-clean-"))
    try:
        tmp = scratch / "repo.git"
        xgit(scratch, "init", "-q", "--bare", str(tmp))
        xgit(tmp, "fetch", "-q", "--no-tags", str(src), f"{tip}:refs/heads/{branch}")
        xgit(tmp, "filter-repo", "--force", "--quiet", "--invert-paths", "--replace-refs", "delete-no-add",
             *[x for p in paths for x in ("--path", p)])
        clean_tip = xgit(tmp, "rev-parse", f"refs/heads/{branch}")
        leftover = xgit(tmp, "log", "--format=%H", clean_tip, "--", *paths)
        if leftover:
            raise CudlError(f"clean-branch check failed: {len(leftover.split())} commits still touch the paths")
        commit_map = {}
        for line in (tmp / "filter-repo" / "commit-map").read_text().splitlines()[1:]:
            old, new = line.split()
            commit_map[old] = new
        # a vanished commit maps to what filter-repo put in its place: its first parent's image
        mapping = {}
        for line in git(src, "rev-list", "--reverse", "--topo-order", "--parents", tip).splitlines():
            sha, *parents = line.split()
            new = commit_map.get(sha, ZERO)
            if new == ZERO:
                new = mapping.get(parents[0]) if parents else None
            mapping[sha] = new
        git(src, "fetch", "-q", "--no-tags", str(tmp), f"refs/heads/{branch}:refs/heads/{target}")
    finally:
        shutil.rmtree(scratch, ignore_errors=True)

    map_rel = Path(".cudl") / "clean" / r.name / f"{target}.map"
    map_file = w.top / map_rel
    map_file.parent.mkdir(parents=True, exist_ok=True)
    map_file.write_text("# original clean ('-': nothing before it survives)\n" +
                        "".join(f"{o} {n or '-'}\n" for o, n in mapping.items()))
    cleans = json.loads((w.top / CLEANS).read_text()) if (w.top / CLEANS).exists() else {"branches": []}
    kept = int(git(src, "rev-list", "--count", target))
    cleans["branches"].append({
        "repo": r.name, "branch": branch, "tip": tip, "clean": target, "clean_tip": mapping[tip],
        "paths": paths, "commits": total, "kept": kept, "map": str(map_rel),
        "date": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
    })
    (w.top / CLEANS).write_text(json.dumps(cleans, indent=2) + "\n")
    rel = [str(CLEANS), str(map_rel)]
    git(w.top, "add", "--", *rel)
    git(w.top, "commit", "-q", "-m", f"cudl: clean-branch {r.name} {branch} → {target}", "--", *rel)
    print(f"{r.name}: {target} at {short(mapping[tip])}, {kept} of {total} commits; map in {map_rel}")
    remote = git(src, "config", f"branch.{branch}.remote", check=False) or "origin"
    print(f"to publish it (your call): git -C {src} push {remote} {target}")


def claude_sessions(projects, roots):
    """Claude Code transcripts whose recorded cwd is one of roots or below it."""
    prefixes = {re.sub(r"[^A-Za-z0-9]", "-", r) for r in roots}
    for d in sorted(projects.glob("*")) if projects.is_dir() else []:
        if not d.is_dir() or not any(d.name.startswith(p) for p in prefixes):
            continue
        for p in sorted(d.glob("*.jsonl")):
            head, tail = read_head_tail(p)
            recs = [r for r in head if r.get("cwd")]
            if not recs or not under(recs[0]["cwd"], roots):
                continue
            stamps = [r["timestamp"] for r in head + tail if r.get("timestamp")]
            prompt = next((x for x in map(claude_prompt, head) if x), "")
            yield {
                "session": recs[0].get("sessionId") or p.stem,
                "agent": "claude",
                "source": recs[0]["cwd"],
                "branch": recs[0].get("gitBranch", ""),
                "started": min(stamps) if stamps else "",
                "ended": max(stamps) if stamps else "",
                "goal": prompt,
                "transcript": str(p),
            }


def codex_sessions(sessions, roots):
    """Codex rollouts whose session cwd is one of roots or below it."""
    for p in sorted(sessions.glob("*/*/*/rollout-*.jsonl")) if sessions.is_dir() else []:
        head, tail = read_head_tail(p)
        meta = next((r.get("payload", {}) for r in head if r.get("type") == "session_meta"), None)
        if not meta or not under(meta.get("cwd", ""), roots):
            continue
        prompt = ""
        for r in head:
            pl = r.get("payload", {})
            if r.get("type") == "response_item" and pl.get("role") == "user":
                for c in pl.get("content", []):
                    t = c.get("text", "") if isinstance(c, dict) else ""
                    if t.strip() and not t.lstrip().startswith("<") and "AGENTS.md" not in t[:300]:
                        prompt = t
                        break
            if prompt:
                break
        stamps = [r["timestamp"] for r in head + tail if r.get("timestamp")]
        yield {
            "session": meta.get("id") or p.stem[-36:],
            "agent": "codex",
            "source": meta["cwd"],
            "branch": (meta.get("git") or {}).get("branch", ""),
            "started": min(stamps) if stamps else "",
            "ended": max(stamps) if stamps else "",
            "goal": prompt,
            "transcript": str(p),
        }


def cmd_import_sessions(a):
    w = Lab.find()
    roots = [str(Path(r).expanduser().resolve()) for r in a.path]
    claude_home = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    codex_home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    projects = Path(a.claude_projects).expanduser() if a.claude_projects else claude_home / "projects"
    rollouts = Path(a.codex_sessions).expanduser() if a.codex_sessions else codex_home / "sessions"
    found = list(claude_sessions(projects, roots)) + list(codex_sessions(rollouts, roots))
    # a log is named by the session's id and the day it started, and each value is one frontmatter
    # line: a transcript that holds anything else there isn't imported as it is
    bad = [s for s in found if not isinstance(s["session"], str) or not SESSION_ID_RE.match(s["session"])]
    found = [s for s in found if s not in bad]
    for s in found:
        for k in ("started", "ended", "source", "branch"):
            s[k] = str(s[k]).replace("\r", " ").replace("\n", " ")
        day = s["started"][:10]
        s["date"] = day if re.fullmatch(r"\d{4}-\d{2}-\d{2}", day) else "unknown"
    if bad:
        print(f"skipped {len(bad)} with a session id that can't name a file: "
              + ", ".join(s["transcript"] for s in bad))
    new, known = [], 0
    for s in found:
        if session_file(w.top, s["session"]):
            known += 1
        else:
            new.append(s)
    for s in sorted(new, key=lambda s: s["started"]):
        date = s["date"]
        print(f"{date} {s['agent']:<6} {s['session'][:8]} {one_line(s['goal'], 70) or '(no prompt)'}")
    print(f"{len(new)} to import, {known} already in the journal "
          f"(searched {projects} and {rollouts} for {', '.join(roots)})")
    if a.dry_run or not new:
        return
    d = sessions_dir(w.top)
    d.mkdir(parents=True, exist_ok=True)
    written = []
    for s in new:
        date = s["date"]
        p = session_path(w.top, f"{date}-legacy", s["session"])
        fm = {
            "session": s["session"],
            "date": date,
            "feature": "legacy",
            "agent": s["agent"],
            "started": s["started"],
            "ended": s["ended"],
            "goal": one_line(s["goal"]) or "(no prompt)",
            "outcome": "legacy",
            "source": s["source"],
            "branch": s["branch"],
            "transcript": s["transcript"],
            "needs_summary": "yes",
        }
        body = ("Imported by `cudl import-sessions` from before this lab existed. The transcript is the "
                "record; read it when this session matters, and summarize it here.\n")
        write_frontmatter(p, fm, body)
        written.append(str(p.relative_to(w.top)))
    build_index(w.top)
    paths = written + ["journal/INDEX.md"]
    git_retry(w.top, "add", "--", *paths)
    git_retry(w.top, "commit", "-q", "-m", f"journal: import {len(written)} legacy sessions", "--", *paths)
    print(f"imported {len(written)} sessions into journal/sessions/ and committed")
