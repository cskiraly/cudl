# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Csaba Kiraly
"""How a feature ends: finish, --stack, --abandon, the resumable state, the WorktreeRemove hook."""

import datetime
import fcntl
import io
import json
import os
import shutil
from contextlib import contextmanager, redirect_stdout
from pathlib import Path

from cudl.core import (
    CudlError, GIT, busy, changed_paths, common_dir, conflicted, git, git_ok, has_staged,
    head_branch, head_sha, is_ancestor, is_checkout, lab_lock, mib, pinned_head, porcelain, run,
    short, split_path, tree,
)
from cudl.lab import Lab, base_of, base_problem, keep_pins, keep_since, pins_in
from cudl.journal import (
    INDEX, SESSION_DIRS, build_index, commit_ended_logs, commit_session_dirs, ended_leftovers,
    readable_frontmatter, session_file, sessions_dir,
)
from cudl.stack import (
    adopt_legacy, feature_finished, feature_live, lab_cfg, lab_children, lab_parent,
    mark_parent_done, parent_end, parent_state, rewrite_advice, stacked_repos,
)
from cudl.sessions import (
    SESSION_ID_RE, add_notice, close_log, current_session, drop_idle_session, find_transcript,
    hook_input, hook_target, lab_descendants, open_sessions, session_home,
)
from cudl.features import (
    check_clean, clean_problems, record_moved_pins, resolve_generated, sync_feature, take_repo,
)


def remove_feature(main, fw, force=False, stamp=None):
    for r in fw.repos().values():
        t = fw.top / r.path
        if is_checkout(t):
            args = ["worktree", "remove"] + (["--force"] if force else []) + [str(t)]
            git(main / r.path, *args)
            t.mkdir(exist_ok=True)  # an empty directory: the gitlink then reads as an unpopulated submodule
    keep_ignored(main, fw, stamp)
    # without --force, git refuses if anything appeared since the clean check (a new session log, say)
    git(main, "worktree", "remove", *(["--force"] if force else []), str(fw.top))


def keep_ignored(main, fw, stamp=None):
    """`git worktree remove` deletes ignored files, so the lab worktree's (scratch/, journal/raw/: the
    lab's working material) first go to the main lab's scratch/<feature>-<stamp>/. Its settings.local.json,
    which `cudl setup` writes, doesn't count. Code checkouts' ignored files, their projects' build
    output, go with them as with any worktree removal."""
    out = run(GIT + ["status", "--porcelain=v1", "-z", "--ignored=matching", "--ignore-submodules=all"],
              fw.top).stdout
    paths = [e[3:].rstrip("/") for e in out.split("\0") if e.startswith("!! ")]
    paths = [p for p in paths if p != ".claude/settings.local.json" and p.split("/")[0] != "code"]
    if not paths:
        return None
    base = Path(main) / "scratch" / f"{fw.feature}-{stamp or datetime.datetime.now().strftime('%Y%m%d-%H%M%S')}"
    dest, n = base, 1
    while dest.exists():  # an earlier attempt's (a removal that stopped): this one's go beside it
        n += 1
        dest = base.with_name(f"{base.name}.{n}")
    for p in paths:
        (dest / p).parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(fw.top / p), str(dest / p))
    print(f"kept {fw.feature}'s ignored files ({', '.join(paths[:3])}{' …' if len(paths) > 3 else ''}) "
          f"in {dest.relative_to(main)}/")
    return dest


def sessions_in_the_way(main, feat, notes=True):
    """What stops removing a feature's worktree: sessions that may still be running there, as their logs
    there tell: a session start ran there and they haven't ended since (`running_since:`). The session
    running this command doesn't count. A note for one that only worked there from elsewhere (`-f`): the
    removal doesn't strand it; and for a subagent whose parent has ended: it was killed with it."""
    ftop = main.main / "wt" / feat
    if not ftop.is_dir():
        return []
    me, problems = current_session(main), []
    for p in sorted(sessions_dir(ftop).glob("*.md")):
        fm = readable_frontmatter(p)
        s = fm.get("session")
        if fm.get("feature") != feat or not s or s == me or fm.get("ended"):
            continue
        if not fm.get("running_since"):
            if notes:
                print(f"note: session {s[:8]} worked in {feat} from {(fm.get('from') or 'elsewhere').split(':')[0]} "
                      f"and may still be running; its end closes its log of {feat} wherever it is")
            continue
        if fm.get("parent") and ended_session(main, fm["parent"]):
            if notes:
                print(f"note: session {s[:8]}, started by session {fm['parent'][:8]}, which has ended, has no end in "
                      f"{feat}: it went with its parent; its log goes to main as it is")
            continue
        resume = f"codex resume {s}" if fm.get("agent") == "codex" else f"claude --resume {s}"
        problems.append(f"session {s[:8]} may still be running in {feat} (its log {p.relative_to(ftop)} has no "
                        f"end): quit it first; if it isn't running any more, resume it there and quit (`{resume}`)")
    return problems


def ended_session(lab, sid):
    """Whether session sid's home log says it has ended, and no start ran since."""
    home = session_home(lab, sid)[1]
    fm = readable_frontmatter(home) if home else {}
    return bool(fm.get("ended")) and not fm.get("running_since")


def busy_problems(fw):
    """Operations in progress in a worktree's lab or code checkouts, whatever their status shows: a
    clean checkout can be in the middle of a rebase, and removing it would lose that."""
    out = [f"the lab has {what} in progress" for what in [busy(fw.top)] if what]
    for r in fw.repos().values():
        d = fw.top / r.path
        what = busy(d) if is_checkout(d) else None
        if what:
            out.append(f"{r.name} has {what} in progress (finish or abort it: git -C {d} status)")
    return out


class NothingMerged(CudlError):
    pass


class Retained(CudlError):
    """Merged up to the plan's snapshot, but the feature changed since: it stays, still live."""


# A finish is resumable: its plan goes into <lab git dir>/cudl-finish.json before anything is
# merged ("current": the feature, its lab and code tips, the bases' tips, the destination lab
# branch, an operation id carried by the finish commit as `Cudl-op:`), and each step after that
# checks what's done already. A stack finish keeps its top and the levels done there too. One finish
# at a time: a lock held for the whole command, and the state file is claimed atomically.

FINISH_STATE = "cudl-finish.json"


def finish_state_file(lab):
    return Path(git(lab.main, "rev-parse", "--path-format=absolute", "--git-common-dir")) / FINISH_STATE


@contextmanager
def finish_lock(lab):
    """Held by a finish for its whole run (a separate lock from lab_lock, which its commits take)."""
    f = open(finish_state_file(lab).with_name("cudl-finish.lock"), "w")
    try:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise CudlError("another cudl finish is running in this lab; wait for it")
        yield
    finally:
        f.close()


def load_finish_state(lab):
    f = finish_state_file(lab)
    if not f.exists():
        return None
    try:
        st = json.loads(f.read_text())
    except ValueError:
        raise CudlError(f"{f} is unreadable; check the main lab and the features it names, then remove it")
    if not st.get("stack") and not st.get("current"):
        return None  # a single finish that got as far as clearing its plan: done (cmd_finish removes it)
    return st


def save_finish_state(lab, st, claim=False):
    """Write the state atomically (complete contents or nothing); with claim, only if no other finish
    holds it."""
    f = finish_state_file(lab)
    if st is None:
        f.unlink(missing_ok=True)
        return
    tmp = f.with_name(f"{f.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(st, indent=2) + "\n")
    if not claim:
        tmp.replace(f)
        return
    try:
        os.link(tmp, f)  # atomic, and fails if it exists
    except FileExistsError:
        raise CudlError(f"another finish started meanwhile: {pending_finish(lab)}")
    finally:
        tmp.unlink(missing_ok=True)


def pending_finish(lab):
    st = load_finish_state(lab)
    if not st:
        return None
    cur = (st.get("current") or {}).get("feature")
    return f"`{st['command']}` stopped before it was done" + (f" (in the middle of {cur})" if cur else "") \
        + "; rerun it to go on"


def refuse_pending(main):
    raise CudlError(f"{pending_finish(main)}\n(to drop it instead, once the main lab and the features are as "
                    f"you want them: rm {finish_state_file(main)})")


def cmd_finish(a):
    w = Lab.find()
    main = Lab(w.main)
    with finish_lock(main):
        st = load_finish_state(main)
        if st is None:
            finish_state_file(main).unlink(missing_ok=True)  # nothing, or a plan-less leftover
        if a.stack:
            if a.feature or a.abandon or a.keep:
                raise CudlError("--stack takes the top feature alone (no other feature, --abandon or --keep)")
            return finish_stack(main, a.stack, a.no_ff, st)
        feat = a.feature or w.feature
        if not feat:
            raise CudlError("which feature? cudl finish <feature>")
        if st:
            cur = (st.get("current") or {}).get("feature")
            top = (st.get("stack") or {}).get("top")
            if a.abandon:
                if feat == cur or (top and feature_live(main, top) and feat in stack_chain(main, top)):
                    refuse_pending(main)
            elif top or cur != feat:
                refuse_pending(main)
        if a.abandon:
            # here, not in abandon_feature, which Remove runs after its own check
            problems = [] if a.force else sessions_in_the_way(main, feat)
            if problems:
                raise CudlError("nothing abandoned:\n  " + "\n  ".join(problems))
            return abandon_feature(main, feat, a.force)
        if st:
            cur = st["current"]
            if (a.no_ff, a.keep) != (cur["no_ff"], cur["keep"]):
                print(f"note: going on as it started: {st['command']}")
        fresh = st is None
        st = st or {"command": f"cudl finish {feat}" + (" --no-ff" if a.no_ff else "") + (" --keep" if a.keep else ""),
                    "stack": None, "current": None}
        finish_feature(main, feat, a.no_ff, a.keep, st, claim=fresh)


ABANDON = "cudl-abandon"


def abandon_feature(main, feat, force):
    """Abandon a feature, resumably. What it will do (the stamp of the new branch names, the repos the
    feature owns, its stacked children) is written down before anything moves, and a rerun goes on
    from there, whatever is left. Names first: a stop halfway leaves everything under its final name."""
    intent_f = common_dir(main) / ABANDON / f"{feat}.json"
    try:
        intent = json.loads(intent_f.read_text())
    except (OSError, ValueError):
        intent = None
    ftop = main.main / "wt" / feat
    if intent and git_ok(main.main, "rev-parse", "-q", "--verify", f"refs/heads/feat/{feat}") \
            and lab_cfg(main, feat, "cudlid") != intent.get("id"):
        intent = None  # left by an earlier feature of the same name: this one starts afresh
    if ftop.is_dir() and not force:  # a rerun too: what is left may have changed since the stop
        fw = Lab(ftop)
        check_clean(fw, "finish")
        problems = busy_problems(fw)
        if problems:
            raise CudlError("finish: " + ("nothing abandoned" if intent is None else f"abandoning {feat} is not done")
                            + ":\n  " + "\n  ".join(problems))
    if intent is None:
        if not ftop.is_dir():
            raise CudlError(f"no feature {feat} at {ftop}")
        fw = Lab(ftop)
        keep_pins(main, pins_in(main.main, [f"feat/{feat}"]))  # whatever happens next, its pins stay
        intent = {"stamp": datetime.datetime.now().strftime("%Y%m%d-%H%M%S"), "id": lab_cfg(main, feat, "cudlid"),
                  "owned": [r.name for r in fw.repos().values()
                            if is_checkout(ftop / r.path) and head_branch(ftop / r.path) == feat],
                  "kids": lab_children(main, feat)}  # before the rename takes feat's id along with its branch
        intent_f.parent.mkdir(exist_ok=True)
        tmp = intent_f.with_name(f"{intent_f.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(intent) + "\n")
        tmp.replace(intent_f)
    else:
        print(f"going on with abandoning {feat}, as it started at {intent['stamp']}")
    stamp, kids = intent["stamp"], intent["kids"]
    new = f"abandoned/{feat}-{stamp}"
    repos = Lab(main.main).repos()
    for name in intent["owned"]:
        d = main.main / repos[name].path if name in repos else None
        if d and git_ok(d, "rev-parse", "-q", "--verify", f"refs/heads/{feat}"):
            git(d, "branch", "-m", feat, new)  # a worktree on it follows the rename
    if git_ok(main.main, "rev-parse", "-q", "--verify", f"refs/heads/feat/{feat}"):
        git(main.main, "branch", "-m", f"feat/{feat}", new)
    if ftop.is_dir():
        remove_feature(main.main, Lab(ftop), force=force, stamp=stamp)
    for c in kids:
        git(main.main, "config", f"branch.feat/{c}.cudlparentdone", f"abandoned {new}")
    intent_f.unlink(missing_ok=True)
    print(f"abandoned {feat}: branches kept as {new}")
    if kids:
        print(f"note: {', '.join(kids)} {'was' if len(kids) == 1 else 'were'} stacked on it: `cudl sync -f <child> "
              f"--reparent` to go on without it")


def main_problems(main, checks):
    """The main lab and its code checkouts, before a finish merges into them. checks: [(repo, base)]."""
    m = main.main
    problems = []
    if not head_branch(m):
        problems.append("the main lab is on a detached HEAD")
    what = busy(m)
    if what:
        problems.append(f"the main lab has {what} in progress")
    code_paths = {r.path for r in main.repos().values()}
    pending = []
    for path, states in changed_paths(m).items():
        if states == [("?", "?")]:
            continue  # untracked files aren't taken along (a running session's new log among them)
        if path in code_paths:
            if any(x != " " for x, _ in states):
                pending.append(path)  # a pin staged by hand
        elif path == INDEX:
            continue  # generated: the finish writes its own
        elif path.split("/")[0] not in SESSION_DIRS or split_path(states):
            pending.append(path)  # session files are committed first, by plan_finish; a split one can't be
    pending.sort()
    if pending and not what:
        problems.append("the main lab has uncommitted changes that the finish commit would take along: "
                        + ", ".join(pending[:5]) + " (commit or stash them)")
    for r, base in checks:
        src = m / r.path
        if head_branch(src) != base:
            problems.append(f"main's {r.path} is on {head_branch(src) or 'detached'}, expected {base}")
        elif busy(src) or porcelain(src):
            problems.append(f"main's {r.path} has uncommitted changes or an operation in progress")
    return problems


def wrong_branches(fw, feat):
    """Code repos in a feature on some other branch: finish merges work by branch name, so theirs would
    be left out."""
    return [f"{r.name} is on {b} in {feat}, not {feat}: its commits wouldn't be merged"
            for r in fw.repos().values() if (b := head_branch(fw.top / r.path)) and b != feat]


def plan_finish(main, feat, no_ff, keep):
    """Every check of a finish, then what it will merge: the feature's lab and code tips as they are
    now, and the tips of what they merge into."""
    ftop = main.main / "wt" / feat
    if not ftop.is_dir():
        raise NothingMerged(f"no feature {feat} at {ftop}")
    fw = Lab(ftop)
    state = parent_state(main, feat)
    if state:
        parent = lab_parent(main, feat)
        raise NothingMerged(f"nothing merged: {feat} is stacked on {parent}, which is {state}; "
                            + (f"finish {parent} first, or the whole stack: cudl finish --stack {feat}" if state == "live" else
                               f"run `cudl sync` in {feat} first, so it follows the integration branches "
                               f"(or `cudl finish --stack {feat}`, which does)"))
    problems = clean_problems(fw, skip=ended_leftovers(fw)) + wrong_branches(fw, feat) + \
        ([] if keep else sessions_in_the_way(main, feat))
    owned = [r for r in fw.repos().values() if head_branch(ftop / r.path) == feat]
    why_not = {r.name: base_problem(main.main / r.path, r, base_of(main.main / r.path, feat, r)) for r in owned}
    problems += [f"{r.name}'s base: {why_not[r.name]}; then git -C {main.main / r.path} config "
                 f"branch.{feat}.cudlbase <branch>" for r in owned if why_not[r.name]]
    based = [r for r in owned if not why_not[r.name]]
    problems += main_problems(main, [(r, base_of(main.main / r.path, feat, r)) for r in based])
    for r in based:
        src = main.main / r.path
        base = base_of(src, feat, r)
        if head_branch(src) == base and not is_ancestor(src, base, feat):
            problems.append(f"{r.name}: {base} has moved on; run `cudl sync` in {feat} first")
    if problems:
        raise NothingMerged("nothing merged:\n  " + "\n  ".join(problems))
    adopt_legacy(main)  # before the first write: a legacy parent retained by this finish stays live
    commit_ended_logs(fw, f"finishing {feat}")
    record_moved_pins(fw, "finish")
    keep_pins(main, pins_in(main.main, [f"feat/{feat}"]))  # whatever happens next, its pins stay
    # what main_problems let through, the sessions' tracked files, in a commit of their own: a checkpoint
    # that stays if the merge stops later (untracked ones, a running session's new log, stay out)
    commit_session_dirs(main, main.main, SESSION_DIRS,
                        f"lab: memory, plans and session logs before finishing {feat}", untracked=False)
    m = main.main
    return {"feature": feat, "op": os.urandom(6).hex(), "lab_tip": git(m, "rev-parse", f"refs/heads/feat/{feat}"),
            "main_branch": head_branch(m), "main_before": head_sha(m), "no_ff": no_ff, "keep": keep,
            "step": "merging",
            "code": {r.name: {"path": r.path, "base": base_of(m / r.path, feat, r),
                              "before": git(m / r.path, "rev-parse", f"refs/heads/{base_of(m / r.path, feat, r)}"),
                              "tip": git(m / r.path, "rev-parse", f"refs/heads/{feat}")} for r in owned}}


def finished_commit(m, cur):
    """The commit this finish made, found by its operation id (a rerun after the commit succeeded
    but before the state file said so)."""
    out = git(m, "log", "--format=%H%x09%(trailers:key=Cudl-op,valueonly,separator=%x2C)",
              f"{cur['main_before']}..refs/heads/{cur['main_branch']}", check=False)
    return next((l.split("\t")[0] for l in out.splitlines() if l.split("\t")[-1].strip() == cur["op"]), None)


def merged_by(m, branch, feat, tip):
    """The latest finish of feat on branch that merged exactly this lab tip (an earlier run's)."""
    out = git(m, "log", "--merges", "--format=%H %P%x09%s", "--fixed-strings", "--grep", f"cudl: finish {feat}",
              f"refs/heads/{branch}", check=False)
    for line in out.splitlines():
        shas, _, subject = line.partition("\t")
        if subject == f"cudl: finish {feat}" and shas.split()[2:3] == [tip]:
            return shas.split()[0]
    return None


def merge_code(src, name, c, feat, no_ff):
    """Merge one feature tip into its base in the main lab's checkout; True if it merged now. The
    base can't have moved (checked), so the result's tree is the feature tip's."""
    if head_branch(src) != c["base"]:
        raise CudlError(f"main's {c['path']} is on {head_branch(src) or 'detached'}, expected {c['base']}: "
                        f"switch it back (git -C {src} switch {c['base']}) and rerun")
    if is_ancestor(src, c["tip"], c["base"]):
        return False
    if git(src, "rev-parse", f"refs/heads/{c['base']}") != c["before"]:
        raise CudlError(f"{name}: {c['base']} moved since this finish started; run `cudl sync` in {feat}")
    mh = run(GIT + ["rev-parse", "-q", "--verify", "MERGE_HEAD"], src, check=False).stdout.strip()
    if mh:
        # our --no-ff merge, stopped at its commit (a hook, signing): conclude it if it is still exactly that
        if mh != c["tip"] or conflicted(src) or run(GIT + ["write-tree"], src, check=False).stdout.strip() != tree(src, c["tip"]):
            raise CudlError(f"main's {c['path']} has a merge in progress that isn't exactly {feat}'s "
                            f"({short(mh)}): `git -C {src} merge --abort`, then rerun")
        git(src, "commit", "-q", "--no-edit")
    else:
        if busy(src):
            raise CudlError(f"main's {c['path']} has {busy(src)} in progress")
        git(src, "merge", "-q", *(["--no-ff", "-m", f"Merge branch '{feat}' into {c['base']}"] if no_ff
                                  else ["--ff-only"]), c["tip"])
    if tree(src, "HEAD") != tree(src, c["tip"]):
        raise CudlError(f"internal: {c['base']} after the merge differs from {feat}'s tip in {c['path']}")
    return True


def merge_finish(main, cur):
    """Merge what plan_finish recorded: the lab tip into main, the code tips into their bases, one
    commit. Safe to rerun: what's merged already is left as it is; what's pending is checked to be
    exactly this finish's before it is concluded."""
    m, feat, tip = main.main, cur["feature"], cur["lab_tip"]
    with lab_lock(main):
        if head_branch(m) != cur["main_branch"]:
            raise CudlError(f"the main lab is on {head_branch(m) or 'a detached HEAD'}; this finish merges into "
                            f"{cur['main_branch']}: switch back (git -C {m} switch {cur['main_branch']}) and rerun")
        mh = run(GIT + ["rev-parse", "-q", "--verify", "MERGE_HEAD"], m, check=False).stdout.strip()
        if mh and mh != tip:
            raise CudlError(f"the main lab is in the middle of merging {short(mh)}, not feat/{feat}: finish or abort that first")
        done = None if mh else finished_commit(m, cur)
        if done:
            return done  # committed already
        code_todo = [n for n, c in cur["code"].items() if not is_ancestor(m / c["path"], c["tip"], c["base"])]
        if not mh and is_ancestor(m, tip, "HEAD") and not code_todo:
            # merged before (a kept finish, or one that stopped at its cleanup): that run's commit
            return merged_by(m, cur["main_branch"], feat, tip) or feature_finished(main, feat) or head_sha(m)
        old = pinned_head(m)
        started = False
        if not mh and not is_ancestor(m, tip, "HEAD"):
            p = run(GIT + ["merge", "--no-ff", "--no-commit", tip], m, check=False)
            if p.returncode != 0:
                remaining = resolve_generated(m, conflicted(m), main.repos(), gitlinks=False)
                remaining += [c for c in conflicted(m) if c not in remaining]
                if remaining or "CONFLICT" not in p.stdout + p.stderr:
                    run(GIT + ["merge", "--abort"], m, check=False)
                    detail = ", ".join(remaining) or (p.stderr.strip() or p.stdout.strip())
                    raise NothingMerged(f"nothing merged: lab merge of feat/{feat} failed ({detail}); "
                                        f"run `cudl sync` in {feat} first")
            started = True
        merged = []
        for name, c in cur["code"].items():
            src = m / c["path"]
            try:
                if merge_code(src, name, c, feat, cur["no_ff"]):
                    merged.append(name)
                    print(f"{name}: {c['base']} now at {short(head_sha(src))}")
            except CudlError as e:
                if started and not merged and not any(n not in code_todo for n in cur["code"]):
                    # nothing moved yet: put the code checkout and the lab back as they were
                    if run(GIT + ["rev-parse", "-q", "--verify", "MERGE_HEAD"], src, check=False).stdout.strip() == c["tip"]:
                        run(GIT + ["merge", "--abort"], src, check=False)
                    run(GIT + ["merge", "--abort"], m, check=False)
                    raise NothingMerged(f"nothing merged: {e}")
                raise
            git(m, "add", c["path"])
            staged = git(m, "ls-files", "-s", "--", c["path"]).split()
            if len(staged) < 2 or staged[1] != head_sha(src) or not is_ancestor(src, c["tip"], staged[1]):
                raise CudlError(f"the main lab's pin of {c['path']} would not hold {feat}'s tip {short(c['tip'])}; "
                                f"check main's {c['path']} and rerun")
        # the index lists only logs that are in the lab's history: a session running in main keeps its log
        build_index(m, tracked_only=True)
        git(m, "add", "journal/INDEX.md")
        if conflicted(m):
            raise CudlError(f"the main lab has unresolved conflicts: {', '.join(conflicted(m)[:5])}; "
                            f"`git -C {m} merge --abort`, then rerun")
        # the index must be exactly the merge (as git computes it) plus the new pins and the index file
        merging = git_ok(m, "rev-parse", "-q", "--verify", "MERGE_HEAD")
        expected = run(GIT + ["merge-tree", "--write-tree", "--no-messages", "HEAD", tip], m, check=False).stdout.split()[:1] \
            if merging else [tree(m, "HEAD")]
        allowed = {c["path"] for c in cur["code"].values()} | {"journal/INDEX.md"}
        unexpected = sorted(set(git(m, "diff-index", "--cached", "--name-only", expected[0]).splitlines()) - allowed) \
            if expected else ["(the merge can't be recomputed)"]
        if unexpected:
            raise CudlError("the main lab's index has changes that aren't part of this finish: "
                            + ", ".join(unexpected[:5]) + "; unstage or restore them (git restore --staged …), "
                            "then rerun")
        trailers = [x for n, c in cur["code"].items()
                    for x in ("--trailer", f"Code: {n} {c['base']} {short(head_sha(m / c['path']))}")]
        trailers += ["--trailer", f"Cudl-op: {cur['op']}"]
        if merging or has_staged(m):
            git(m, "commit", "-q", "-m", f"cudl: finish {feat}", *trailers)
    keep_since(main, old)
    print(f"lab: merged feat/{feat} into {cur['main_branch']}")
    return head_sha(m)


def cleanup_finish(main, cur, command):
    """After the finish commit. First: is the feature still exactly what was merged? If it changed
    since the plan (a session committed, a file appeared), it stays, live, and a rerun merges the
    rest. Only then: keep the finish's pins, mark the children, remove the worktrees and branches."""
    m, feat, done = main.main, cur["feature"], cur["commit"]
    if not is_ancestor(m, done, f"refs/heads/{cur['main_branch']}"):
        raise CudlError(f"the finish commit {short(done)} is no longer on {cur['main_branch']}; check the main lab")
    newer = []
    lab_now = run(GIT + ["rev-parse", "-q", "--verify", f"refs/heads/feat/{feat}"], m, check=False).stdout.strip()
    if lab_now and lab_now != cur["lab_tip"]:
        newer.append(f"lab branch feat/{feat} has new commits")
    for name, c in cur["code"].items():
        now = run(GIT + ["rev-parse", "-q", "--verify", f"refs/heads/{feat}"], m / c["path"], check=False).stdout.strip()
        if now and now != c["tip"]:
            newer.append(f"{name} branch {feat} has new commits")
    ftop = m / "wt" / feat
    problems = (clean_problems(Lab(ftop)) + ([] if cur.get("keep") else sessions_in_the_way(main, feat, notes=False))
                if ftop.is_dir() else [])
    if newer or problems:
        raise Retained(f"{feat} is merged as it was when the finish started ({short(done)}), but it changed since, "
                       f"so it stays as it is: " + "; ".join(newer + problems)
                       + f"\ncommit or remove what's new there, then rerun `{command}` to merge the rest and remove it")
    keep_pins(main, pins_in(m, ["-1", done]))
    kids = mark_parent_done(main, feat, f"finished {done}")
    for c in kids:
        ctop = m / "wt" / c
        if not ctop.is_dir():
            continue
        stale = [s for s in stacked_repos(Lab(ctop), c, feat, "completed", done) if s.state in ("rewritten", "tangled")]
        if stale or not command.startswith("cudl finish --stack"):
            print(f"note: {c} was stacked on {feat}; `cudl sync -f {c}` makes it follow where {feat} went"
                  + (f" (with --rebase: it holds commits of {feat} from before a rewrite, in "
                     f"{', '.join(s.repo.name for s in stale)})" if stale else ""))
    if cur["keep"]:
        git(m, "config", f"branch.feat/{feat}.cudlfinished", done)
        return
    if ftop.is_dir():
        remove_feature(m, Lab(ftop))
    for name, c in cur["code"].items():
        src = m / c["path"]
        if git_ok(src, "rev-parse", "-q", "--verify", f"refs/heads/{feat}") and is_ancestor(src, c["tip"], c["base"]):
            git(src, "branch", "-q", "-D", feat)  # at the merged tip (checked above); -d would ask its upstream
    if lab_now and is_ancestor(m, cur["lab_tip"], f"refs/heads/{cur['main_branch']}"):
        git(m, "branch", "-q", "-D", f"feat/{feat}")
    print(f"removed wt/{feat} and its branches")


def finish_feature(main, feat, no_ff, keep, st, claim=False):
    """Finish one feature, resumably: the plan goes into the state file before anything is merged,
    and each step after it can be rerun. Its completion is written with the plan's removal, in one
    save."""
    cur = st.get("current")
    if cur and cur["feature"] != feat:
        raise CudlError(f"internal: the state file is finishing {cur['feature']}, not {feat}")
    if not cur:
        cur = plan_finish(main, feat, no_ff, keep)
        st["current"] = cur
        save_finish_state(main, st, claim=claim)
    if cur["step"] == "merging":
        try:
            cur["commit"] = merge_finish(main, cur)
        except NothingMerged:
            st["current"] = None  # the main lab is as it was; a rerun plans again
            save_finish_state(main, st if st.get("stack") else None)
            raise
        cur["step"] = "cleanup"
        save_finish_state(main, st)
    try:
        cleanup_finish(main, cur, st["command"])
    except Retained:
        st["current"] = None  # merged as far as planned; a rerun plans the rest
        save_finish_state(main, st if st.get("stack") else None)
        raise
    st["current"] = None
    if st.get("stack"):
        st["stack"]["done"][feat] = cur["commit"]
    save_finish_state(main, st if st.get("stack") else None)
    return cur["commit"]


def stack_chain(main, top):
    """[root, …, top]: top and the live features under it, bottom-up."""
    chain, feat = [top], top
    while parent_state(main, feat) == "live":
        feat = lab_parent(main, feat)
        if feat in chain:
            raise CudlError(f"the stack under {top} loops at {feat}")
        chain.append(feat)
    return chain[::-1]


def preflight_stack(main, chain):
    """Everything a stack finish needs, checked before anything is written."""
    problems, checks = [], {}
    for feat in chain:
        ftop = main.main / "wt" / feat
        if not feature_live(main, feat):
            problems.append(f"{feat}: no live feature at {ftop}")
            continue
        fw = Lab(ftop)
        problems += [f"{feat}: {p}" for p in clean_problems(fw, skip=ended_leftovers(fw)) + wrong_branches(fw, feat)
                     + sessions_in_the_way(main, feat, notes=False)]
        parent = lab_parent(main, feat)
        state, done = parent_end(main, feat)
        if state == "abandoned":
            problems.append(f"{feat}: stacked on {parent}, which was abandoned: `cudl sync -f {feat} --reparent` first")
        stacked = stacked_repos(fw, feat, parent, state, done) if state and state != "abandoned" else []
        problems += [f"{feat}: {rewrite_advice(feat, parent, s)}" for s in stacked if s.state in ("rewritten", "tangled")]
        for r in fw.repos().values():
            if head_branch(ftop / r.path) != feat:
                continue
            s = next((s for s in stacked if s.repo.name == r.name), None)
            if s and state == "live":
                continue  # it ends up where the parent's goes, checked at the parent's level
            base = s.onto if s else base_of(main.main / r.path, feat, r)
            why = base_problem(main.main / r.path, r, base)  # every level's: checks keeps one base per repo
            if why:
                problems.append(f"{feat}: {r.name}'s base: {why}; then git -C {main.main / r.path} config "
                                f"branch.{feat}.cudlbase <branch>")
                continue
            checks.setdefault(r.name, (r, base))
    problems += main_problems(main, list(checks.values()))
    if problems:
        raise CudlError("nothing finished:\n  " + "\n  ".join(problems))


def finish_stack(main, top, no_ff, st):
    """Finish top and the live features it is stacked on, bottom-up, each synced first."""
    if st and (st.get("stack") or {}).get("top") != top:
        refuse_pending(main)
    fresh = st is None
    if st:
        if no_ff != st["stack"]["no_ff"]:
            print(f"note: going on as it started: {st['command']}")
        no_ff = st["stack"]["no_ff"]
    else:
        st = {"command": f"cudl finish --stack {top}" + (" --no-ff" if no_ff else ""),
              "stack": {"top": top, "no_ff": no_ff, "done": {}}, "current": None}
    done = st["stack"]["done"]
    chain = []

    def stopped(e, feat):
        left = [f for f in chain if f not in done] or [feat]
        return CudlError(f"{e}\n\nthe stack finish stopped at {feat} (finished: {', '.join(done) or 'none'}; "
                         f"left: {', '.join(left)}). Once that's resolved, rerun `{st['command']}`.")

    if st.get("current"):  # a level that stopped half-way goes first
        feat = st["current"]["feature"]
        try:
            finish_feature(main, feat, no_ff, False, st)
        except CudlError as e:
            raise stopped(e, feat)
    if top not in done:
        chain = stack_chain(main, top)
        preflight_stack(main, chain)
        adopt_legacy(main)
        print(f"finishing {' → '.join(chain)}" + (f" ({', '.join(done)} done before)" if done else ""))
        if fresh:
            save_finish_state(main, st, claim=True)
    for feat in chain:
        try:
            print(f"[{feat}]")
            sync_feature(Lab(main.main / "wt" / feat), link=False)
            finish_feature(main, feat, no_ff, False, st)
        except CudlError as e:
            raise stopped(e, feat)
    save_finish_state(main, None)
    print(f"finished the stack: {', '.join(done)}")


SNAPSHOT_LIMIT = 100 * 1024 * 1024  # untracked files a removal commits, all repos together


def branch_problems(w):
    """A feature worktree whose checkouts are not on its branches: a snapshot would land elsewhere."""
    feat, out = w.feature, []
    if head_branch(w.top) != f"feat/{feat}":
        out.append(f"the lab worktree is on {head_branch(w.top) or 'a detached HEAD'}, not feat/{feat}")
    for r in w.repos().values():
        d = w.top / r.path
        if not is_checkout(d):
            continue
        b = head_branch(d)
        if b and b != feat:
            out.append(f"{r.name} is on {b}, not {feat}")
        elif not b and porcelain(d) and git_ok(d, "rev-parse", "-q", "--verify", f"refs/heads/{feat}"):
            out.append(f"{r.name} is pinned, has changes, and a branch {feat} already exists in it")
    return out


def untracked_size(w):
    """(total bytes, [(bytes, path)] biggest first) of the untracked, not ignored files in a worktree's
    lab and code checkouts: what a snapshot would put into history."""
    files = []
    for d in [w.top] + [w.top / r.path for r in w.repos().values() if is_checkout(w.top / r.path)]:
        out = run(GIT + ["ls-files", "--others", "--exclude-standard", "-z"], d, check=False).stdout
        for rel in filter(None, out.split("\0")):
            try:
                files.append((os.lstat(d / rel).st_size, str((d / rel).relative_to(w.top))))
            except OSError:
                continue
    files.sort(reverse=True)
    return sum(s for s, _ in files), files


def snapshot(w, sid, leave=()):
    """Commit what's uncommitted in a feature worktree onto its own branches, two commits per repository,
    so that a path staged and then changed again keeps both versions: what is staged, then the rest,
    untracked files included (ignored ones stay ignored, see keep_ignored). A pinned code checkout with
    work in it takes the feature's branch first, as `cudl commit` does. The lab's own `leave` paths (the
    session's log, which close_log commits), the index and the pins (close_log records them) are left.
    Returns the repositories it committed in; on an error, says which it had committed in already."""
    tag = f"at the removal of {w.feature} (session {sid[:8]})"
    done = []

    def commit_both(d, name, *add):
        """What's staged, then the rest, each commit noted in `done` as it's made; if the second fails,
        the index is as the first left it."""
        if has_staged(d):
            git(d, "commit", "-q", "-m", f"cudl: staged {tag}")
            done.append(name)
        git(d, "add", "-A", *add)
        if has_staged(d):
            try:
                git(d, "commit", "-q", "-m", f"cudl: uncommitted {tag}")
            except CudlError:
                run(GIT + ["reset", "-q"], d, check=False)  # the index is HEAD plus what `add` staged
                raise
            if name not in done:
                done.append(name)

    try:
        for r in w.repos().values():
            d = w.top / r.path
            if not is_checkout(d) or not porcelain(d):
                continue
            if not head_branch(d):
                take_repo(w, r)
            commit_both(d, r.name)
        with lab_lock(w):
            commit_both(w.top, "the lab", "--", ".", *[f":(exclude){p}" for p in (*leave, INDEX, "code")])
    except CudlError as e:
        raise CudlError(f"{e}\n(committed before this: {', '.join(done) or 'nothing'}; the rest is uncommitted)")
    return done


def remove_at_exit(main, w, data, agent):
    """What WorktreeRemove does (see hook_worktree_remove); returns the notice, or raises with why the
    worktree stays."""
    feat, sid, reason = w.feature, data.get("session_id") or "", data.get("reason")
    own = session_file(w.top, sid, feat) if SESSION_ID_RE.match(sid) else None
    if own and (readable_frontmatter(own).get("role") == "continued" or reason == "subagent_end"):
        own = None  # a link (cmd_new links the session that made the worktree) is no ownership
    with finish_lock(main):
        st = load_finish_state(main)
        if st:
            cur, top = (st.get("current") or {}).get("feature"), (st.get("stack") or {}).get("top")
            if feat == cur or (top and feature_live(main, top) and feat in stack_chain(main, top)):
                raise CudlError(pending_finish(main))
        # its own descendants end with it, or were killed with it (a background review): they don't count
        mine = {sid} | ({readable_frontmatter(p).get("session") for ps in lab_descendants(w, sid).values() for p in ps}
                        if SESSION_ID_RE.match(sid) else set())
        problems = [f"session {s[:8]} may still be running there (its log has no end)" for s in open_sessions(w, but=mine)]
        problems += busy_problems(w) + branch_problems(w)
        if own:
            total, files = untracked_size(w)
            if total > SNAPSHOT_LIMIT:
                problems.append(f"its untracked files come to {mib(total)} (biggest: "
                                + ", ".join(f"{p} {mib(s)}" for s, p in files[:3]) + "), too much to commit")
        if problems:
            raise CudlError("; ".join(problems))
        out = io.StringIO()
        if not own:  # a subagent's worktree, or someone else's: removed only if nothing needs saving
            linked = session_file(w.top, sid, feat) if SESSION_ID_RE.match(sid) else None
            with redirect_stdout(out):
                # the session that made the worktree is linked to it: cudl's own file, closed only once
                # nothing else stops the removal
                check_clean(w, "removal", ignore=[str(linked.relative_to(w.top))] if linked else [])
                if linked:
                    close_log(w, linked, sid)
                abandon_feature(main, feat, force=False)
            return (f"wt/{feat} was removed by Claude Code ({reason or 'no reason given'}): "
                    + " ".join(l for l in out.getvalue().splitlines() if l))
        fm = readable_frontmatter(own)
        transcript = find_transcript(sid, agent, data.get("transcript_path"), fm.get("transcript"))
        kids = lab_descendants(w, sid).get(w.top, [])
        saved = []
        try:
            with redirect_stdout(out):
                # what's uncommitted is saved either way: a session that did nothing gets no log, but older
                # work can be there
                idle = drop_idle_session(w, own, transcript, agent)
                saved = snapshot(w, sid, leave=[str(p.relative_to(w.top)) for p in [own, *kids]])
                if not idle:
                    close_log(w, own, sid, kids, transcript)
                abandon_feature(main, feat, force=False)
        except CudlError as e:
            raise CudlError(f"{e}" + (f"\n(uncommitted work was committed first, in {', '.join(saved)}, on "
                                      f"the feature's branches)" if saved else ""))
    return (f"wt/{feat} was removed when session {sid[:8]} exited with Remove worktree"
            + (f"; uncommitted work committed first, in {', '.join(saved)}" if saved else "") + ": "
            + " ".join(l for l in out.getvalue().splitlines() if l))


def hook_worktree_remove(a):
    """Claude Code's "Remove worktree" on leaving `claude -w` (or a subagent's worktree it cleans up).
    It runs before the session's end, its output is discarded, and a non-zero exit keeps the worktree.
    The session that worked here gets what it asked for and loses nothing: its leftovers are committed
    onto the feature's branches, its log is closed, and the feature is abandoned (branches kept as
    abandoned/<feature>-<time>). Whatever stops that keeps the worktree. Either way a notice says so."""
    data = hook_input()
    path = hook_target(data, "worktree-remove")
    if not path:
        raise CudlError("WorktreeRemove: no worktree path in the hook input")
    w = Lab(path)
    main = Lab(w.main)
    if not w.feature:
        raise CudlError(f"WorktreeRemove: {path} is not a feature worktree")
    os.chdir(main.main)  # not the worktree that is about to go
    try:
        add_notice(main, remove_at_exit(main, w, data, a.agent))
    except Exception as e:
        sid = (data.get("session_id") or "")[:8]
        add_notice(main, f"wt/{w.feature} was kept when {'session ' + sid if sid else 'Claude Code'} asked to remove "
                         f"it: {e}\nTo remove it once that's dealt with: `cudl finish {w.feature} --abandon`")
        raise
