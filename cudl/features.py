# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Csaba Kiraly
"""Working in a feature: new, take, commit, squash, sync, open, push, the WorktreeCreate hook."""

import argparse
import datetime
import os
import shlex
import sys
from pathlib import Path

from cudl.core import (
    AGENTS, CudlError, GIT, busy, changed_paths, conflicted, git, git_ok, git_retry, has_staged,
    head_branch, head_sha, in_progress, is_ancestor, is_checkout, lab_changes, lab_lock, pinned,
    pinned_head, porcelain, run, short,
)
from cudl.lab import (
    KEEP, Lab, base_of, base_problem, keep_since, lab_tops, require_feature, setup_local,
    validate_name,
)
from cudl.journal import (
    build_commit_index, build_index, commit_ended_logs, commit_memory, ended_leftovers, session_file,
    update_frontmatter,
)
from cudl.stack import (
    adopt_legacy, feature_live, finish_destinations, follow_base, forget_rebase, lab_cfg,
    lab_children, lab_parent, net_result, parent_end, published_commits, rebase_checks,
    rebase_stacked, rebase_verdict, rewrite_advice, stacked_repos,
)
from cudl.sessions import (
    PARENT_ENV, SESSION_ID_RE, current_session, hook_input, hook_target, link_session,
    pins_to_record,
)


def seed_handoff(lab, src_text, src_name, feat, goal):
    sid = current_session(lab)
    who = f", by session {sid}" if sid and any(session_file(t, sid) for t in lab_tops(lab)) else ""
    today = datetime.date.today().isoformat()
    return (f"# {feat}\n\nGoal: {goal or '(set me)'}\n\nState: just started, from {src_name}.\n\n"
            f"## Seeded from {src_name} ({today}{who})\n\n{src_text.strip()}\n")


def cmd_new(a, quiet=False):
    w = Lab.find()
    feat = a.feature
    validate_name(feat)
    main = w.main
    top = main / "wt" / feat
    repos = Lab(main).repos()
    parent = getattr(a, "from_", None)
    ptop = main / "wt" / parent if parent else None
    parent_owned = []
    if parent:
        if not feature_live(w, parent):
            raise CudlError(f"--from {parent}: no live feature {parent}")
        dirty_code = [r.name for r in repos.values() if is_checkout(ptop / r.path) and porcelain(ptop / r.path)]
        if lab_changes(ptop) or dirty_code:
            print(f"note: {parent} has uncommitted changes" + (f" (code: {', '.join(dirty_code)})" if dirty_code else "")
                  + f"; {feat} starts from its last commits (commit them in {parent}, then "
                  f"`cudl sync -f {feat}` to bring them in)", file=sys.stderr)
        parent_owned = [r.name for r in repos.values() if head_branch(ptop / r.path) == parent]
    sel = [x for item in (a.repo or []) for x in item.split(",") if x]
    if a.all:
        sel = list(repos)
    elif parent and not sel:
        sel = list(parent_owned)
    for name in sel:
        if name not in repos:
            raise CudlError(f"unknown repo {name}; known: {', '.join(repos) or 'none'}")
    bases = {}
    for spec in a.base or []:
        name, _, branch = spec.partition("=")
        if name not in sel or not branch:
            raise CudlError(f"--base {spec}: use <repo>=<branch> for a repo in the feature")
        bases[name] = branch
    if top.exists():
        raise CudlError(f"{top} already exists")
    if git_ok(main, "rev-parse", "-q", "--verify", f"refs/heads/feat/{feat}"):
        raise CudlError(f"lab branch feat/{feat} already exists")
    for r in repos.values():
        src = main / r.path
        if not is_checkout(src):
            raise CudlError(f"{r.path} is not checked out in the main lab: git submodule update --init")
        if r.name in sel and git_ok(src, "rev-parse", "-q", "--verify", f"refs/heads/{feat}"):
            raise CudlError(f"branch {feat} already exists in {r.name}")
    for name, base in bases.items():
        why = base_problem(main / repos[name].path, repos[name], base)
        if why:
            raise CudlError(f"--base {name}={base}: {why}")
    handoff_from = getattr(a, "handoff_from", None)
    handoff_file = Path(handoff_from).resolve() if handoff_from and Path(handoff_from).is_file() else None
    if handoff_file and not handoff_file.is_relative_to(main.resolve()):  # where a link points counts
        raise CudlError(f"--handoff-from {handoff_from}: that file is outside the lab; copy it in first")
    seed, seeded = handoff_from or parent, None  # what the handoff starts from, read before any write
    if handoff_file:
        seeded = handoff_file.read_text(), handoff_from
    elif seed:  # the committed handoff, like the rest of what the new feature starts from
        ref = "HEAD" if seed == "main" else f"feat/{seed}"
        got = run(GIT + ["show", f"{ref}:handoff/{seed}.md"], main, check=False)
        if got.returncode != 0:
            raise CudlError(f"--handoff-from {seed}: no such file, and no feature with a committed "
                            f"handoff/{seed}.md" if handoff_from else f"--from {seed}: no committed handoff/{seed}.md")
        seeded = got.stdout, f"handoff/{seed}.md"

    git(main, "worktree", "add", "-q", "-b", f"feat/{feat}", str(top), f"feat/{parent}" if parent else "HEAD")
    git(main, "config", f"branch.feat/{feat}.cudlid", os.urandom(6).hex())
    if parent:
        git(main, "config", f"branch.feat/{feat}.cudlparent", parent)
        if lab_cfg(w, parent, "cudlid"):
            git(main, "config", f"branch.feat/{feat}.cudlparentid", lab_cfg(w, parent, "cudlid"))
    for r in repos.values():
        target = top / r.path
        if target.is_dir() and not any(target.iterdir()):
            target.rmdir()
        src = main / r.path
        if r.name in sel:
            base = bases.get(r.name) or (parent if r.name in parent_owned else r.branch)
            git(src, "worktree", "add", "-q", "-b", feat, str(target), base)
            git(src, "config", f"branch.{feat}.cudlbase", base)
            if base == parent:
                git(src, "config", f"branch.{feat}.cudlparenttip", git(src, "rev-parse", parent))
        else:
            check_out_pinned(w, top, r)
    fw = Lab(top)
    setup_local(fw)
    handoff = top / "handoff" / f"{feat}.md"
    if not handoff.exists():
        handoff.parent.mkdir(exist_ok=True)
        if seeded:
            handoff.write_text(seed_handoff(w, *seeded, feat, a.goal))
        else:
            handoff.write_text(f"# {feat}\n\nGoal: {a.goal or '(set me)'}\n\nState: just started.\n")
    old = pinned_head(top)
    git(top, "add", "-A")
    if has_staged(top):
        git(top, "commit", "-q", "-m", f"cudl: start feature {feat}" + (f" from {parent}" if parent else ""))
    keep_since(fw, old)
    link_session(fw, quiet=True)
    if not quiet:
        owned = ", ".join(sel) or "none (all repos pinned; `cudl take <repo>` to work on one)"
        print(f"feature {feat} at {top}" + (f", stacked on {parent}" if parent else "")
              + f"\n  branches: feat/{feat} in the lab, {feat} in: {owned}\n  open a session: cudl open {feat}")
    return top


def check_out_pinned(w, top, r):
    """r's worktree in the feature lab at top, detached at the commit that lab pins, as a feature has
    each repo it doesn't own. Made from main's checkout of r; returns the commit."""
    src, target = w.main / r.path, top / r.path
    if not is_checkout(src):
        raise CudlError(f"{r.path} is not checked out in the main lab: git submodule update --init")
    if target.is_dir() and not any(target.iterdir()):
        target.rmdir()
    pin = pinned(top, r.path) or head_sha(src)
    git(src, "worktree", "add", "-q", "--detach", str(target), pin)
    return pin


def take_repo(w, r, base=None):
    """Give a pinned repo the feature's branch, at its pinned commit."""
    d = w.top / r.path
    if head_branch(d):
        raise CudlError(f"{r.name} is already on branch {head_branch(d)}")
    if git_ok(d, "rev-parse", "-q", "--verify", f"refs/heads/{w.feature}"):
        raise CudlError(f"branch {w.feature} already exists in {r.name}")
    feat = w.feature
    parent = lab_parent(w, feat)
    state, done = parent_end(w, feat)
    head = head_sha(d)
    pin = parent_pin(w, feat, r, state, done, head)
    boundary = pin if pin and is_ancestor(d, pin, head) else None  # the parent's commit this came from
    if not base:
        dest = finish_destinations(w.main, done).get(r.name) if state == "completed" else None
        if state == "live" and head_branch(w.main / "wt" / parent / r.path) == parent:
            base = parent
        elif dest and boundary and not is_ancestor(d, boundary, f"refs/heads/{dest}"):
            base = parent  # it holds the parent's commits from before a rewrite: `sync --rebase` sorts that out
        else:
            base = dest or r.branch
    tip = None
    if base == parent:
        tip = boundary or run(GIT + ["merge-base", head, f"refs/heads/{parent}"], d, check=False).stdout.strip()
    git(d, "switch", "-q", "-c", feat)
    git(d, "config", f"branch.{feat}.cudlbase", base)
    if tip:
        git(d, "config", f"branch.{feat}.cudlparenttip", tip)
    return base


def parent_pin(w, feat, r, state, done, head):
    """The commit of r that feat took from its parent, from the lab: the newest pin of r in the
    parent's lab history up to the last parent commit feat merged in, among those `head` contains
    (feat's own pin may be a commit made on the detached checkout; a sync conflict may have kept it
    over a newer parent pin)."""
    parent = lab_parent(w, feat)
    ref = f"refs/heads/feat/{parent}" if state == "live" else f"{done}^2" if done else None
    if not ref:
        return None
    point = run(GIT + ["merge-base", f"refs/heads/feat/{feat}", ref], w.main, check=False).stdout.strip()
    if not point:
        return None
    d = w.top / r.path
    seen = set()
    for c in git(w.main, "log", "--format=%H", point, "--", r.path, check=False).split() + [point]:
        pin = run(GIT + ["rev-parse", "-q", "--verify", f"{c}:{r.path}"], w.main, check=False).stdout.strip()
        if pin and pin not in seen:
            seen.add(pin)
            if is_ancestor(d, pin, head):
                return pin
    return None


def cmd_take(a):
    w = Lab.find()
    require_feature(w)
    repos = w.repos()
    if a.repo not in repos:
        raise CudlError(f"unknown repo {a.repo}")
    r = repos[a.repo]
    if not is_checkout(w.top / r.path):  # a repo main added later, left empty by the sync of an older cudl
        check_out_pinned(w, w.top, r)  # first: git in an empty directory would find the lab around it
    if a.base:
        why = base_problem(w.top / r.path, r, a.base)
        if why:
            raise CudlError(f"--base {a.base}: {why}")
    link_session(w)
    base = take_repo(w, r, a.base)
    print(f"{a.repo}: on new branch {w.feature} (base {base})")


def parse_messages(msgs, repos):
    default, per = None, {}
    for m in msgs or []:
        name, sep, rest = m.partition("=")
        if sep and (name in repos or name == "lab"):
            per[name] = rest
        elif default is None:
            default = m
        else:
            default += "\n\n" + m
    return default, per


def lab_path_known(top, p, head=True):
    """A lab path or pathspec that names something: a pattern (git judges it), a file or directory, a
    path in the index, or (head) one in HEAD, as a path `git rm` took out is."""
    return (any(c in p for c in "*?[") or (Path(top) / p).exists()
            or git_ok(top, "ls-files", "--error-unmatch", "--", p)
            or (head and bool(git(top, "ls-tree", "--name-only", "HEAD", "--", p, check=False))))


def cmd_commit(a):
    w = Lab.find()
    repos = w.repos()
    default, per = parse_messages(a.message, repos)
    pending, unstaged = [], []
    for r in repos.values():
        d = w.top / r.path
        if not is_checkout(d):
            continue
        if a.all_files:
            has = bool(porcelain(d))
        elif a.all:
            has = bool(porcelain(d, untracked=False))
        else:
            has = has_staged(d)
        if has:
            pending.append(r)
        elif porcelain(d):
            unstaged.append(r.name)

    problems, to_take = [], []
    for r in pending:
        d = w.top / r.path
        b = head_branch(d)
        expected = w.feature or r.branch
        if not b:
            if not w.feature:
                problems.append(f"{r.name} is detached in the main lab: switch it to {r.branch} first")
            elif git_ok(d, "rev-parse", "-q", "--verify", f"refs/heads/{w.feature}"):
                problems.append(f"{r.name} is pinned here and a branch {w.feature} already exists in it")
            else:
                to_take.append(r)  # a pinned repo with work in it: give it the feature's branch
        elif b != expected and not a.any_branch:
            problems.append(f"{r.name} is on {b}, expected {expected} (--any-branch to allow)")
        what = in_progress(d)
        if what:
            problems.append(f"{r.name} has {what} in progress")
        if conflicted(d):
            problems.append(f"{r.name} has unresolved conflicts")
        if not (per.get(r.name) or default):
            problems.append(f"no message for {r.name}: -m <msg> or -m {r.name}=<msg>")
    problems += [f"--lab {p}: no such path in the lab" for p in a.lab or [] if not lab_path_known(w.top, p)]
    if problems:
        raise CudlError("nothing committed:\n  " + "\n  ".join(problems))
    link_session(w)
    for r in to_take:
        print(f"{r.name}: was pinned; now on branch {w.feature} (base {take_repo(w, r)})")

    done = []
    for r in pending:
        d = w.top / r.path
        if a.all_files:
            git(d, "add", "-A")
        elif a.all:
            git(d, "add", "-u")
        try:
            git(d, "commit", "-q", "-m", per.get(r.name) or default)
        except CudlError as e:
            raise CudlError(f"{e}\ncommitted so far: {', '.join(done) or 'none'}; fix and rerun cudl commit")
        done.append(r.name)
        print(f"{r.name}: {short(head_sha(d))} on {head_branch(d)}")

    with lab_lock(w):  # against concurrent session-end and feedback commits
        if a.lab:  # only these paths: the index lists the logs the commit holds, not a running session's
            build_commit_index(w.top, [p for p in changed_paths(w.top, *a.lab, literal=False)
                                       if p.startswith("journal/sessions/")])
        else:
            build_index(w.top)
        old = pinned_head(w.top)
        moved = [r.path for r in repos.values() if is_checkout(w.top / r.path) and pinned(w.top, r.path) != head_sha(w.top / r.path)
                 and git_ok(w.top, "diff", "--cached", "--quiet", "--", r.path)]  # a pin staged by hand stays as staged
        only = [*a.lab, *moved, "journal/INDEX.md"] if a.lab else None  # only these lab paths, plus the pins
        if only:
            # a path `git rm` took out is in neither the index nor the tree: nothing to add, and `git
            # add` would fail on it; the pathspec commit records its deletion all the same
            there = [p for p in only if lab_path_known(w.top, p, head=False)]
            if there:
                git(w.top, "add", "--", *there)
        else:
            git(w.top, "add", "-A")
        if (only and not git_ok(w.top, "diff", "--cached", "--quiet", "--", *only)) or (not only and has_staged(w.top)):
            trailers = []
            for r in repos.values():
                d = w.top / r.path
                if is_checkout(d) and pinned(w.top, r.path) != head_sha(d):
                    trailers += ["--trailer", f"Code: {r.name} {head_branch(d) or 'detached'} {short(head_sha(d))}"]
            msg = per.get("lab") or default
            if not msg:
                raise CudlError("lab has changes: pass -m <msg>")
            git(w.top, "commit", "-q", "-m", msg, *trailers, *(["--", *only] if only else []))
            keep_since(w, old)
            print(f"lab: {short(head_sha(w.top))} on {head_branch(w.top)}")
        elif not done:
            print("nothing to commit")
    if w.feature and not a.no_memory and commit_memory(w):
        print("memory: committed on main")
    if unstaged:
        print(f"note: unstaged changes left in {', '.join(unstaged)} (-a: tracked files, -A: everything)")


def cmd_squash(a):
    """Fold a feature's commits in each code repo it owns into one, through the normal commit path."""
    w = Lab.find()
    if not w.feature:
        raise CudlError("squash works in a feature: the integration branches are never rewritten by cudl")
    feat = w.feature
    repos = w.repos()
    default, per = parse_messages(a.message, repos)
    names = [x for item in (a.repo or []) for x in item.split(",") if x] or \
        [r.name for r in repos.values() if is_checkout(w.top / r.path) and head_branch(w.top / r.path) == feat]
    parent = lab_parent(w, feat)
    state, done = parent_end(w, feat)
    if state and state != "abandoned" and any(s.state in ("rewritten", "tangled")
                                              for s in stacked_repos(w, feat, parent, state, done)):
        raise CudlError(f"{parent} was rewritten since {feat} took it in: run `cudl sync` for the options first")
    plan, problems = [], []
    for name in names:
        if name not in repos:
            problems.append(f"unknown repo {name}")
            continue
        r = repos[name]
        d = w.top / r.path
        if head_branch(d) != feat:
            problems.append(f"{name} is not on {feat} in this feature")
            continue
        if porcelain(d) or in_progress(d) or conflicted(d):
            problems.append(f"{name} has uncommitted changes or an operation in progress")
            continue
        base = git(d, "merge-base", feat, follow_base(w, feat, r, d))
        commits = git(d, "rev-list", "--reverse", f"{base}..{feat}").split()
        if len(commits) < 2:
            continue
        published = git(d, "for-each-ref", "--format=%(refname:short)", "--contains", commits[0], "refs/remotes")
        if published and not a.force:
            problems.append(f"{name}: these commits are already on {', '.join(published.split())}; squashing "
                            f"rewrites what others may have (--force if that's intended)")
            continue
        subjects = [git(d, "log", "-1", "--format=%s", c) for c in commits]
        msg = per.get(name) or default or (subjects[0] + "\n\nSquashed:\n" + "".join(f"- {t}\n" for t in subjects))
        plan.append((r, d, base, commits, msg))
    if problems:
        raise CudlError("nothing squashed:\n  " + "\n  ".join(problems))
    link_session(w)
    if not plan:
        print("nothing to squash (at most one commit per repo since its base)")
        return
    for r, d, base, commits, msg in plan:
        # keep the old tip, then fold everything since the base into the index
        run(GIT + ["update-ref", "--stdin"], d, input=f"create {KEEP}{commits[-1]} {commits[-1]}\n", check=False)
        git(d, "reset", "-q", "--soft", base)
    cmd_commit(argparse.Namespace(
        message=[f"{r.name}={m}" for r, _, _, _, m in plan] + [f"lab={per.get('lab') or 'cudl: squash ' + ', '.join(r.name for r, *_ in plan) + ' in ' + feat}"],
        all=False, all_files=False, any_branch=False, no_memory=False, lab=None))
    kids = lab_children(w, feat)
    if kids:
        print(f"note: {', '.join(kids)} {'is' if len(kids) == 1 else 'are'} stacked on {feat}, whose commits were just "
              f"replaced: `cudl sync -f <child> --rebase` moves a child's own commits onto them")


def resolve_generated(top, conflicts, repos, gitlinks=True):
    """Resolve the conflicts cudl owns: submodule pointers and the generated index."""
    paths = {r.path for r in repos.values()}
    remaining = []
    for c in conflicts:
        if c in paths and gitlinks:
            git(top, "add", c)
        elif c == "journal/INDEX.md":
            build_index(top)
            git(top, "add", c)
        else:
            remaining.append(c)
    return remaining


def check_clean(fw, what, ignore=()):
    """Nothing uncommitted in a feature worktree but the lab paths in `ignore`."""
    problems = []
    for r in fw.repos().values():
        d = fw.top / r.path
        if is_checkout(d) and porcelain(d):
            problems.append(f"{r.name} has uncommitted changes")
    if [l for l in lab_changes(fw.top) if l[3:] not in ignore]:
        problems.append("the lab has uncommitted changes (or unrecorded code commits)")
    if problems:
        raise CudlError(f"{what}: commit first (cudl commit):\n  " + "\n  ".join(problems))


def record_moved_pins(w, why):
    """Record, in a commit of just those gitlinks, the code repos whose HEAD moved on this worktree's
    own branch (the session end's rule): they're commits already, only the lab lags behind. A
    pathspec commit, nothing staged first: if it fails, the index is as it was."""
    with lab_lock(w):
        if busy(w.top):
            return []
        record = pins_to_record(w)
        if not record:
            return []
        paths = [r.path for r in record]
        trailers = [x for r in record for x in
                    ("--trailer", f"Code: {r.name} {head_branch(w.top / r.path)} {short(head_sha(w.top / r.path))}")]
        old = pinned_head(w.top)
        git_retry(w.top, "commit", "-q", "-m", f"cudl: pins {', '.join(r.name for r in record)} (before {why})",
                  *trailers, "--", *paths)
        keep_since(w, old)
    print(f"lab: recorded the pins of {', '.join(r.name for r in record)}")
    return record


def clean_problems(fw, skip=()):
    """What stops a sync or finish: uncommitted work, an operation in progress. Moved pins that
    `record_moved_pins` would record don't count, nor the paths in `skip` (ended_leftovers: the caller
    commits those)."""
    problems = []
    for r in fw.repos().values():
        d = fw.top / r.path
        if not is_checkout(d):
            continue
        what = busy(d)
        if what:
            problems.append(f"{r.name} has {what} in progress (finish or abort it: git -C {d} status)")
        elif porcelain(d):
            problems.append(f"{r.name} has uncommitted changes")
    recordable = {r.path for r in pins_to_record(fw)}
    what = busy(fw.top)
    if what:
        problems.append(f"the lab has {what} in progress")
    else:
        changed = [l[3:] for l in lab_changes(fw.top) if l[3:] not in recordable and l[3:] not in skip]
        if changed:
            problems.append(f"the lab has uncommitted changes: {', '.join(changed[:4])}" + (" …" if len(changed) > 4 else "")
                            + (" (a session still running there?)" if any(c.startswith("journal/sessions/") for c in changed) else ""))
    return problems


def cmd_sync(a):
    w = Lab.find()
    require_feature(w)
    sync_feature(w, rebase=a.rebase, force=a.force, reparent_abandoned=a.reparent)


def sync_feature(w, rebase=False, force=False, reparent_abandoned=False, link=True):
    """Bring a feature up to date with what it follows: its parent while that's live, else the
    integration branches and main. Every check comes before the first write; with `rebase`, a
    child of a rewritten parent has its own commits moved onto it (see rebase_checks)."""
    feat = w.feature
    repos = w.repos()
    problems = clean_problems(w, skip=ended_leftovers(w))
    if problems:
        raise CudlError("sync: commit first (cudl commit):\n  " + "\n  ".join(problems))
    parent = lab_parent(w, feat)
    state, done = parent_end(w, feat)
    if state == "abandoned" and not reparent_abandoned:
        raise CudlError(f"{parent}, which {feat} is stacked on, was abandoned (or its finish is no longer on "
                        f"main). {feat} still contains its commits. To go on without it: cudl sync --reparent "
                        f"(then drop its commits by hand if they shouldn't be merged), or abandon {feat} too")
    stacked = stacked_repos(w, feat, parent, state, done) if parent else []
    bad = [s for s in stacked if s.state == "tangled" or (s.state == "rewritten" and not rebase)]
    if bad:
        raise CudlError(f"nothing synced: {parent} was rewritten since {feat} last took it in\n  "
                        + "\n  ".join(rewrite_advice(feat, parent, s) for s in bad))
    todo = [s for s in stacked if s.state == "rewritten"]
    problems = [p for p in (rebase_checks(s, feat, force) for s in todo) if p]
    if problems:
        raise CudlError("nothing rebased:\n  " + "\n  ".join(problems))
    rehearsed = {s.repo.name: rebase_verdict(s, feat) for s in todo}  # every verdict before any write
    targets = {}  # what to merge, resolved once: a branch moving meanwhile doesn't change what's recorded
    for r in repos.values():
        d = w.top / r.path
        if is_checkout(d) and head_branch(d) == feat:
            s = next((s for s in stacked if s.repo.name == r.name), None)
            base = s.onto if s else base_of(d, feat, r)
            why = base_problem(d, r, base)
            if why:
                raise CudlError(f"nothing synced: {r.name}'s base: {why}; then git -C {d} config "
                                f"branch.{feat}.cudlbase <branch>")
            # the parent's tip as classified above: what the rebase went onto is what gets recorded
            sha = (s.onto_sha if s else None) or run(GIT + ["rev-parse", "-q", "--verify",
                                                           f"refs/heads/{base}^{{commit}}"], d, check=False).stdout.strip()
            if not sha:
                raise CudlError(f"nothing synced: {r.name}'s base {base} isn't a commit")
            targets[r.name] = (base, sha)

    # writes from here on
    adopt_legacy(Lab(w.main))  # freezes the judgements made above (ids for features made before them)
    if link:
        link_session(w)
    commit_ended_logs(w, "syncing")
    record_moved_pins(w, "sync")
    old = pinned_head(w.top)
    for s in todo:
        _, refs = published_commits(s, feat)
        leases = {ref: git(s.dir, "rev-parse", f"refs/remotes/{ref}") for ref in refs}
        old_tip = rebase_stacked(s, feat, rehearsed[s.repo.name])
        s.state, s.target = "rebased", s.onto_sha
        print(f"{s.repo.name}: rebased {feat}'s own commits onto {s.onto} (old tip {short(old_tip)} kept)")
        for ref, sha in leases.items():
            remote, _, branch = ref.partition("/")
            if branch == feat:
                print(f"  {ref} has the old commits; to replace them (yours to run): "
                      f"git -C {s.dir} push --force-with-lease={feat}:{sha} {remote} {feat}")
            else:
                print(f"  {ref} also has the old commits")
    for s in stacked:
        if s.state == "rebased":  # by --rebase just now, by hand, or by a --rebase that stopped and was continued
            was = git(s.dir, "config", f"branch.{feat}.cudlrebasefrom", check=False)
            onto = git(s.dir, "config", f"branch.{feat}.cudlrebaseonto", check=False)
            last = git(s.dir, "config", f"branch.{feat}.cudlparenttip", check=False)
            if s not in todo and was and onto == s.target and last:  # a --rebase that stopped, continued by hand
                expected = net_result(s.dir, last, onto, was)
                if expected and expected != git(s.dir, "rev-parse", f"refs/heads/{feat}^{{tree}}"):
                    print(f"note: {s.repo.name}: the continued rebase gives a different result than {s.onto} plus "
                          f"{feat}'s earlier changes; worth a look: git -C {s.dir} diff {short(expected)} {feat}")
            git(s.dir, "config", f"branch.{feat}.cudlparenttip", s.target)
            forget_rebase(s.dir, feat)
    if parent and state != "live":
        # the parent is finished (or given up): every stacked repo passed the checks above
        for s in stacked:
            git(s.dir, "config", f"branch.{feat}.cudlbase", s.onto)
            git(s.dir, "config", "--unset", f"branch.{feat}.cudlparenttip", check=False)
        for key in ("cudlparent", "cudlparentid", "cudlparentdone"):
            git(w.main, "config", "--unset", f"branch.feat/{feat}.{key}", check=False)
        print(f"{parent} is {'finished' if state == 'completed' else 'abandoned'}: "
              f"{feat} now follows {', '.join(sorted({s.onto for s in stacked})) or 'the integration branches'} and main")
        parent = None
    kids = lab_children(w, feat)
    if todo and kids:
        print(f"note: {', '.join(kids)} {'is' if len(kids) == 1 else 'are'} stacked on {feat}, which was just "
              f"rebased: `cudl sync -f <child> --rebase` there")
    for r in repos.values():
        if r.name not in targets:
            continue
        d = w.top / r.path
        base, sha = targets[r.name]
        if not is_ancestor(d, sha, feat):
            p = run(GIT + ["merge", "--no-edit", "-m", f"Merge branch '{base}' into {feat}", sha], d, check=False)
            if p.returncode != 0:
                raise CudlError(f"merge conflict in {r.path} ({base} into {feat}): resolve it there, "
                                f"then `cudl commit -a -m ...` and rerun cudl sync")
            print(f"{r.name}: merged {base}")
        if parent and base == parent:
            git(d, "config", f"branch.{feat}.cudlparenttip", sha)
    main_branch = f"feat/{parent}" if parent else (head_branch(w.main) or "main")
    with lab_lock(w):
        if not is_ancestor(w.top, main_branch, "HEAD"):
            p = run(GIT + ["merge", "--no-edit", main_branch], w.top, check=False)
            if p.returncode != 0:
                remaining = resolve_generated(w.top, conflicted(w.top), repos)
                if remaining:
                    raise CudlError(f"lab merge conflicts in: {', '.join(remaining)}\n"
                                    f"resolve them, then `cudl commit -m ...`")
                git(w.top, "commit", "-q", "--no-edit")
            print(f"lab: merged {main_branch}")
        for r in w.repos().values():  # read again: the merge brings a repo main added since
            if not is_checkout(w.top / r.path):
                try:
                    pin = check_out_pinned(w, w.top, r)
                except CudlError as e:
                    print(f"note: {r.name} isn't checked out here: {e}", file=sys.stderr)
                    continue
                print(f"{r.name}: checked out at {short(pin)}, pinned (`cudl take {r.name}` to work on it)")
        for r in repos.values():
            d = w.top / r.path
            if is_checkout(d) and not head_branch(d):
                pin = pinned(w.top, r.path)
                if pin and pin != head_sha(d):
                    git(d, "checkout", "-q", "--detach", pin)
                    print(f"{r.name}: pinned at {short(pin)}")
        build_index(w.top)
        git(w.top, "add", "-A")
        if has_staged(w.top):
            git(w.top, "commit", "-q", "-m", f"cudl: sync {feat} with {main_branch}")
    keep_since(w, old)
    print("in sync")


def cmd_open(a):
    w = Lab.find()
    top = w.feature_top(a.feature)
    if not top.is_dir():
        raise CudlError(f"no feature {a.feature}")
    cmd = a.command or AGENTS[a.agent]
    # a new session, not a subagent of the one this may run in: no session ids in its environment
    if os.environ.get("TMUX") and not a.print:
        clear = [x for v in PARENT_ENV for x in ("-e", f"{v}=")]
        run(["tmux", "new-window", *clear, "-n", a.feature, "-c", str(top), cmd], top)
        print(f"opened tmux window {a.feature}")
    else:
        # the whole command under the cleared environment (it can be several, or a builtin of your shell)
        print(f"cd {shlex.quote(str(top))} && env {' '.join(f'-u {v}' for v in PARENT_ENV)} "
              f'"${{SHELL:-/bin/sh}}" -c {shlex.quote(cmd)}')


def push_remote(d, branch, override=None):
    """Where a branch goes: only where it says (or you say); a branch with no remote isn't pushed,
    so a branch of your own can't land on upstream by default."""
    return override or (git(d, "config", f"branch.{branch}.pushRemote", check=False)
                        or git(d, "config", f"branch.{branch}.remote", check=False)
                        or git(d, "config", "remote.pushDefault", check=False))


def cmd_push(a):
    w = Lab.find()
    main = Lab(w.main)
    if a.feature:
        top = main.feature_top(a.feature)  # a name, not a path
        if a.feature == "main" or not top.is_dir():
            raise CudlError(f"no feature {a.feature}")
        items = [(r, top / r.path, a.feature) for r in main.repos().values()
                 if head_branch(top / r.path) == a.feature]
        print(f"feature {a.feature}: its code branches (the lab stays local)")
    else:
        items = [(r, main.top / r.path, r.branch) for r in main.repos().values()]
        print("the integration branches (feature branches: cudl push --feature <name>; the lab stays local)")
    for r, d, branch in items:
        remote = push_remote(d, branch, a.remote)
        if not remote:
            print(f"  {r.name}: {branch} has no push remote; skipped (--remote <name>, or "
                  f"git -C {d} config branch.{branch}.pushRemote <remote>)")
            continue
        cmd = ["git", "-C", str(d), "push", "--no-follow-tags", remote, branch]  # branches only, never tags or keep refs
        print("  " + " ".join(cmd))
        if a.yes:
            run(cmd, d)
    if not a.yes:
        print("(dry run: nothing pushed; pass --yes to push these)")


def hook_worktree_create(a):
    data = hook_input()
    name = data.get("name") or data.get("worktree_name")
    if not name:
        raise CudlError("WorktreeCreate: no worktree name in the hook input")
    os.chdir(hook_target(data))
    ns = argparse.Namespace(feature=name, repo=[], all=False, base=[], goal=None)
    top = cmd_new(ns, quiet=True)
    sid = data.get("session_id") or ""
    sf = session_file(top, sid, Lab(top).label) if SESSION_ID_RE.match(sid) else None
    if sf:  # a running session moves in (EnterWorktree; `claude -w` has no log yet): it works from here now
        with lab_lock(Lab(top)):
            update_frontmatter(sf, running_since=datetime.datetime.now().astimezone().isoformat(timespec="seconds"))
    print(top)
