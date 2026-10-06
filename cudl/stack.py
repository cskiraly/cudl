# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Csaba Kiraly
"""Stacked features: parents, finish marks, rewritten parents, rehearsing a rebase."""

import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from cudl.core import (
    CudlError, GIT, git, git_ok, head_branch, in_progress, is_ancestor, is_checkout, lab_lock, run,
    short, signing_diagnosis,
)
from cudl.lab import KEEP, Repo, base_of


# `cudl new <child> --from <parent>`: the child's lab branch starts at feat/<parent>, its code
# branches at the parent's. The lab config records branch.feat/<f>.cudlid (each feature's identity,
# since names can be reused), branch.feat/<child>.cudlparent and .cudlparentid; each code repo
# records branch.<child>.cudlbase = <parent> and branch.<child>.cudlparenttip = the last parent
# commit the child took in, so a rewrite of the parent after any sync is detected. When a parent
# ends, its children get branch.feat/<child>.cudlparentdone = "finished <commit>" or "abandoned
# <branch>" (never overwritten); a feature finished with --keep gets branch.feat/<f>.cudlfinished.

def lab_parent(lab, feat):
    return git(lab.main, "config", f"branch.feat/{feat}.cudlparent", check=False) or None


def lab_cfg(lab, feat, key):
    return git(lab.main, "config", f"branch.feat/{feat}.{key}", check=False) or None


def lab_children(lab, feat):
    """The features stacked on the live feature `feat` (not on an earlier feature by that name)."""
    out = git(lab.main, "config", "--get-regexp", r"^branch\.feat/.*\.cudlparent$", check=False)
    me = lab_cfg(lab, feat, "cudlid")
    kids = []
    for line in out.splitlines():
        key, _, value = line.partition(" ")
        child = key[len("branch.feat/"):-len(".cudlparent")]
        pid = lab_cfg(lab, child, "cudlparentid")
        if value == feat and not lab_cfg(lab, child, "cudlparentdone") and (not pid or pid == me) \
                and git_ok(lab.main, "rev-parse", "-q", "--verify", f"refs/heads/feat/{child}"):
            kids.append(child)
    return sorted(kids)


def feature_finished(lab, name):
    return lab_cfg(lab, name, "cudlfinished")


def feature_live(lab, name):
    return ((lab.main / "wt" / name).is_dir() and not feature_finished(lab, name)
            and git_ok(lab.main, "rev-parse", "-q", "--verify", f"refs/heads/feat/{name}"))


def legacy_finish(lab, child, parent, live):
    """A finish of the child's parent from before cudl marked children: a `cudl: finish <parent>`
    merge on main whose merged side holds the commit the child started from (and, while a feature
    by that name exists, one that feature hasn't merged back since: then it's that one, kept)."""
    m = lab.main
    start = None
    want = f"cudl: start feature {child} from {parent}"
    for line in git(m, "log", "--format=%P%x09%s", "--fixed-strings", "--grep", want, f"refs/heads/feat/{child}",
                    check=False).splitlines():
        parents, _, subject = line.partition("\t")
        if subject == want and parents:
            start = parents.split()[0]
    out = git(m, "log", "--merges", "--format=%H %P%x09%s", "--fixed-strings", "--grep", f"cudl: finish {parent}",
              head_branch(m) or "HEAD", check=False)
    for line in out.splitlines():
        shas, _, subject = line.partition("\t")
        f, *ps = shas.split()
        if subject != f"cudl: finish {parent}" or len(ps) < 2 or (start and not is_ancestor(m, start, ps[1])):
            continue
        if not start and live and is_ancestor(m, f, f"refs/heads/feat/{parent}"):
            continue  # no start commit to go by, and that feature started after this finish: a namesake
        return f
    return None


def parent_end(lab, child):
    """(state, finish commit) of the feature `child` is stacked on: ('live', None), ('completed', F)
    (finished into main, even with --keep), ('abandoned', None), or (None, None) if not stacked."""
    parent = lab_parent(lab, child)
    if not parent:
        return None, None
    m = lab.main
    done = lab_cfg(lab, child, "cudlparentdone")
    if done:
        kind, _, ref = done.partition(" ")
        if kind == "finished" and is_ancestor(m, ref, head_branch(m) or "HEAD"):
            return "completed", ref
        return "abandoned", None  # given up, or its finish commit is no longer on main
    pid = lab_cfg(lab, child, "cudlparentid")
    same = not pid or lab_cfg(lab, parent, "cudlid") == pid
    if same and feature_live(lab, parent):
        f = None if pid else legacy_finish(lab, child, parent, live=True)
        return ("completed", f) if f else ("live", None)
    if same and feature_finished(lab, parent):
        return "completed", feature_finished(lab, parent)
    if pid:  # made with ids, so its parent's end would have been marked: it went without a finish
        return "abandoned", None
    f = legacy_finish(lab, child, parent, live=git_ok(m, "rev-parse", "-q", "--verify", f"refs/heads/feat/{parent}"))
    return ("completed", f) if f else ("abandoned", None)


def parent_state(lab, child):
    return parent_end(lab, child)[0]


def adopt_legacy(lab):
    """Give features made before ids one, and their children the parent's while it is live for them,
    so from now on a parent's end is known from its marks rather than inferred from main's history
    (which can't tell a merged snapshot from a finished feature)."""
    with lab_lock(lab):  # one adoption at a time (sync and finish both call this)
        feats = lab.features()
        judged = {}
        for f in feats:
            if lab_parent(lab, f) and not lab_cfg(lab, f, "cudlparentid") and not lab_cfg(lab, f, "cudlparentdone"):
                judged[f] = parent_end(lab, f)  # the old way, once
        for f in feats:
            if not lab_cfg(lab, f, "cudlid") and git_ok(lab.main, "rev-parse", "-q", "--verify", f"refs/heads/feat/{f}"):
                git(lab.main, "config", f"branch.feat/{f}.cudlid", os.urandom(6).hex())
        for f, (state, done) in judged.items():
            pid = lab_cfg(lab, lab_parent(lab, f), "cudlid")
            if state == "live" and pid:
                git(lab.main, "config", f"branch.feat/{f}.cudlparentid", pid)
            elif state == "completed" and done:  # so a later feature by the parent's name can't claim it
                git(lab.main, "config", f"branch.feat/{f}.cudlparentdone", f"finished {done}")
            elif state == "abandoned":
                git(lab.main, "config", f"branch.feat/{f}.cudlparentdone", "abandoned (before ids)")


def mark_parent_done(lab, feat, what):
    """Record on feat's children that feat is gone: 'finished <commit>' or 'abandoned <branch>'."""
    kids = lab_children(lab, feat)
    for c in kids:
        git(lab.main, "config", f"branch.feat/{c}.cudlparentdone", what)
    return kids


def finish_destinations(m, commit):
    """{repo: branch} a finish commit merged into, from its `Code:` trailers."""
    if not commit:
        return {}
    out = git(m, "log", "-1", "--format=%(trailers:key=Code,valueonly,separator=%x0A)", commit, check=False)
    return {p[0]: p[1] for p in (l.split() for l in out.splitlines()) if len(p) >= 2}


def follow_base(lab, feat, r, d):
    """The branch feat's own commits in r are counted from now: its base, or, once a parent it was
    based on is gone, where that parent was merged."""
    base = base_of(d, feat, r)
    parent = lab_parent(lab, feat)
    if parent and base == parent:
        state, done = parent_end(lab, feat)
        if state != "live":
            return finish_destinations(lab.main, done).get(r.name) or r.branch
    return base


@dataclass
class Stacked:
    """One code repo where a feature is stacked on its parent."""
    repo: Repo
    dir: Path
    last: str      # the parent commit the child last took in, or None
    onto: str      # the branch the child follows: the parent's while it's live, else where it was merged
    onto_sha: str  # that branch's tip, resolved once
    state: str     # ok | rebased | rewritten | tangled
    target: str = None  # rebased: the commit the child was moved onto


def holds_obsolete(d, last, target, tip):
    """Does the child still hold a parent commit that the rewritten parent dropped?"""
    gone = set(git(d, "rev-list", last, "--not", target).split())
    return bool(gone & set(git(d, "rev-list", tip, "--not", target).split()))


def rewrite_state(d, feat, onto_sha, last, pending=None):
    """(state, target): ok: onto still contains what the child took (or nothing was recorded);
    rewritten: the child still holds commits that onto no longer has; rebased: the child was moved
    onto target (onto, or the target of a --rebase that stopped and was continued) and holds none of
    the dropped commits; tangled: anything else."""
    tip = f"refs/heads/{feat}"
    if not last or not onto_sha or is_ancestor(d, last, onto_sha):
        return "ok", None
    if is_ancestor(d, last, tip):
        return "rewritten", None
    mb = run(GIT + ["merge-base", onto_sha, tip], d, check=False).stdout.strip()
    for target in dict.fromkeys(t for t in (pending, onto_sha, mb) if t):
        if is_ancestor(d, target, tip) and not holds_obsolete(d, last, target, tip):
            return "rebased", target
    return "tangled", None


def stacked_repos(lab, feat, parent, state, done=None):
    """The code repos where feat is stacked on parent, compared with what it follows now."""
    dests = finish_destinations(lab.main, done) if state == "completed" else {}
    out = []
    for r in lab.repos().values():
        d = lab.top / r.path
        if not is_checkout(d) or base_of(d, feat, r) != parent \
                or not git_ok(d, "rev-parse", "-q", "--verify", f"refs/heads/{feat}"):
            continue
        last = git(d, "config", f"branch.{feat}.cudlparenttip", check=False) or None
        onto = parent if state == "live" else dests.get(r.name) or r.branch
        onto_sha = run(GIT + ["rev-parse", "-q", "--verify", f"refs/heads/{onto}^{{commit}}"], d,
                       check=False).stdout.strip() or None
        if state == "abandoned":
            out.append(Stacked(r, d, last, onto, onto_sha, "ok"))
            continue
        pending = git(d, "config", f"branch.{feat}.cudlrebaseonto", check=False) or None
        st, target = rewrite_state(d, feat, onto_sha, last, pending)
        if st == "rebased" and target != onto_sha:
            # a --rebase onto an older tip, continued by hand: from there, the parent may have moved on
            st2, _ = rewrite_state(d, feat, onto_sha, target)
            out.append(Stacked(r, d, target, onto, onto_sha, "rebased" if st2 == "ok" else st2, target))
        else:
            out.append(Stacked(r, d, last, onto, onto_sha, st, target))
    return out


def rewrite_advice(feat, parent, s):
    what = f"{parent}" if s.onto == parent else f"{s.onto} (where {parent} was merged)"
    if s.state == "rewritten":
        return (f"{s.repo.name}: {feat} took {short(s.last)} from {parent}, which {what} no longer contains: "
                f"`cudl sync -f {feat} --rebase` moves {feat}'s own commits onto it (or by hand: git -C "
                f"{s.repo.path} rebase --onto {s.onto} {short(s.last)} {feat}, then cudl sync)")
    return (f"{s.repo.name}: {feat} doesn't follow from what it took from {parent} ({short(s.last)}), and isn't "
            f"cleanly on {what} either (it may still hold commits {parent} dropped): move its own commits onto "
            f"{s.onto} by hand (git -C {s.repo.path} rebase --onto {s.onto} <{feat}'s first commit>^ {feat}), "
            f"then cudl sync")


def clean_merge(d, sha, parents):
    """A merge whose tree is exactly git's automatic merge of its two parents: it added nothing of its
    own (no conflict resolution, no extra change), so flattening it away loses nothing."""
    if len(parents) != 2:
        return False
    p = run(GIT + ["merge-tree", "--write-tree", "--no-messages", *parents], d, check=False)
    return p.returncode == 0 and p.stdout.split()[:1] == [git(d, "rev-parse", f"{sha}^{{tree}}")]


def child_remote_refs(d, feat):
    """Remote-tracking branches that are feat's own: same name on any remote, or its upstream."""
    refs = set(git(d, "for-each-ref", "--format=%(refname)", f"refs/remotes/*/{feat}").split())
    up = run(GIT + ["rev-parse", "-q", "--symbolic-full-name", f"{feat}@{{upstream}}"], d, check=False).stdout.strip()
    if up.startswith("refs/remotes/"):
        refs.add(up)
    return sorted(refs)


def published_commits(s, feat):
    """What a rebase of feat (its own commits, s.last..feat) would rewrite that others may have: commits
    on any remote-tracking branch, and feat's own remote branches the result won't contain."""
    rng = f"{s.last}..refs/heads/{feat}"
    local = set(git(s.dir, "rev-list", rng, "--not", "--remotes").split())
    published = [c for c in git(s.dir, "rev-list", rng).split() if c not in local]
    refs = set(git(s.dir, "for-each-ref", "--format=%(refname)", "--contains", published[-1],
                   "refs/remotes").split()) if published else set()
    refs |= {r for r in child_remote_refs(s.dir, feat) if not is_ancestor(s.dir, r, s.onto_sha)}
    return published, sorted(x.removeprefix("refs/remotes/") for x in refs)


def rebase_checks(s, feat, force):
    """Why `sync --rebase` can't move feat's own commits (s.last..feat) onto s.onto, or None."""
    d, rng = s.dir, f"{s.last}..refs/heads/{feat}"
    if head_branch(d) != feat:
        return (f"{s.repo.name}: its worktree is on {head_branch(d) or 'a detached HEAD'}, not {feat}: switch back "
                f"(git -C {d} switch {feat}) first")
    for m in git(d, "rev-list", "--merges", "--parents", rng).splitlines():
        sha, *parents = m.split()
        if any(not is_ancestor(d, o, s.last) for o in parents[1:]) or not clean_merge(d, sha, parents):
            # a merge of the parent's older states that git did alone flattens away; one with a
            # resolution or a change of its own would be lost, and anything else isn't ours to drop
            return (f"{s.repo.name}: {feat} has a merge ({short(sha)}) that a rebase would drop with what it "
                    f"changed (a conflict resolution, or a merge of something other than {s.onto}'s older "
                    f"commits): rebase by hand, e.g. git -C {s.repo.path} rebase --rebase-merges --onto "
                    f"{s.onto} {short(s.last)} {feat}, then cudl sync")
    published, refs = published_commits(s, feat)
    if (published or refs) and not force:
        return (f"{s.repo.name}: {feat} is published ({', '.join(refs) or 'a remote'}): rebasing rewrites "
                f"what others may have, and a force-push follows; --force if that's intended")
    return None


def net_result(d, last, onto, old):
    """The tree that `onto` plus the child's net change since `last` gives (a three-way merge with
    `last` as its base), or None when that merge conflicts."""
    p = run(GIT + ["merge-tree", "--write-tree", "--no-messages", "--merge-base", last, onto, old], d, check=False)
    return p.stdout.split()[0] if p.returncode == 0 and p.stdout.split() else None


def rehearse_rebase(s, old):
    """Replay feat's own commits onto s.onto_sha in a throwaway worktree (no hooks, no signing):
    the resulting tree, or None if the replay stops (a conflict). Nothing of the user's is touched."""
    tmp = Path(tempfile.mkdtemp(prefix="cudl-rebase-"))
    wt = tmp / "wt"
    quiet = GIT + ["-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false"]
    try:
        run(quiet + ["worktree", "add", "-q", "--detach", str(wt), old], s.dir)
        p = run(quiet + ["rebase", "--no-update-refs", "--no-rebase-merges", "--no-autosquash", "--no-autostash",
                         "--onto", s.onto_sha, s.last], wt, check=False)
        if p.returncode != 0:
            run(quiet + ["rebase", "--abort"], wt, check=False)
            return None
        return git(wt, "rev-parse", "HEAD^{tree}")
    finally:
        run(quiet + ["worktree", "remove", "--force", str(wt)], s.dir, check=False)
        shutil.rmtree(tmp, ignore_errors=True)


def rebase_verdict(s, feat):
    """The rehearsal of feat's rebase in one repo, checked: its tree, None if it stops on a conflict
    (the real rebase will stop for the user), or a refusal. A rebase replays commits one by one, so
    its result can differ from the parent plus the child's net change (a commit that became empty on
    the new parent, then a later revert of it, undoes the parent's change): only a result equal to
    the three-way merge of the parent with the child's net change since `cudlparenttip` goes ahead."""
    d = s.dir
    old = git(d, "rev-parse", f"refs/heads/{feat}")
    rehearsed = rehearse_rebase(s, old)
    if rehearsed is not None:
        expected = net_result(d, s.last, s.onto_sha, old)
        if expected is None:
            raise CudlError(f"nothing rebased: in {s.repo.name}, the rebase of {feat} onto {s.onto} goes through, but "
                            f"it can't be checked: {s.onto} and {feat}'s net change since {short(s.last)} conflict as a "
                            f"merge (did {s.onto} take over one of {feat}'s commits?). Rebase by hand and compare the "
                            f"result with what you meant: git -C {s.repo.path} rebase --onto {s.onto} {short(s.last)} "
                            f"{feat}; git -C {s.repo.path} diff {short(old)} {feat}; then cudl sync")
        if expected != rehearsed:
            raise CudlError(f"nothing rebased: in {s.repo.name}, replaying {feat}'s commits one by one onto {s.onto} "
                            f"gives a different result than {s.onto} plus {feat}'s net change (a commit that became "
                            f"empty, then a later revert of it, say). Rebase by hand and check the result: "
                            f"git -C {s.repo.path} rebase --onto {s.onto} {short(s.last)} {feat}")
    return rehearsed


def rebase_stacked(s, feat, rehearsed):
    """Move feat's own commits onto s.onto_sha in one code repo, after rebase_verdict; returns the old
    tip. The old tip gets a keep ref; the target and old tip are recorded (cudlrebaseonto,
    cudlrebasefrom) so a stopped rebase is recognised later; the user's rebase config (updateRefs,
    rebaseMerges, autoSquash, autoStash) is overridden."""
    d = s.dir
    old = git(d, "rev-parse", f"refs/heads/{feat}")
    run(GIT + ["update-ref", "--stdin"], d, input=f"update {KEEP}{old} {old}\n")
    git(d, "config", f"branch.{feat}.cudlrebaseonto", s.onto_sha)
    git(d, "config", f"branch.{feat}.cudlrebasefrom", old)
    p = run(GIT + ["rebase", "--no-update-refs", "--no-rebase-merges", "--no-autosquash", "--no-autostash",
                   "--onto", s.onto_sha, s.last, feat], d, check=False)
    if p.returncode != 0:
        out = "\n".join(x for x in (p.stdout.strip(), p.stderr.strip()) if x)
        if "sign" in out:
            out += "\n" + signing_diagnosis(d)
        if in_progress(d):
            raise CudlError(f"the rebase of {feat} onto {s.onto} stopped in {s.repo.path}:\n{out}\n"
                            f"resolve it (git -C {d} status), `git -C {d} rebase --continue` (or --abort), "
                            f"then run cudl sync again")
        forget_rebase(d, feat)
        raise CudlError(f"`git rebase --onto {s.onto} {short(s.last)} {feat}` in {d} failed:\n{out}")
    if rehearsed and git(d, "rev-parse", f"refs/heads/{feat}^{{tree}}") != rehearsed:
        print(f"note: {s.repo.name}: the rebase gave a different result than its rehearsal (a hook?); "
              f"worth a look: git -C {d} diff {short(old)} {feat}")
    return old


def forget_rebase(d, feat):
    for key in ("cudlrebaseonto", "cudlrebasefrom"):
        git(d, "config", "--unset", f"branch.{feat}.{key}", check=False)
