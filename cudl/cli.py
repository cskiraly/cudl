# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Csaba Kiraly
"""Command line: main, the hook table, status, ls, index, feedback, the unfinished-copy guard."""

import argparse
import datetime
import os
import re
import sys
from pathlib import Path

from cudl.core import (
    AGENTS, CudlError, MARKER, git_retry, head_branch, lab_changes, lab_lock, one_line,
)
from cudl.lab import LAB_HOOKS, Lab, cmd_keep, repo_line
from cudl.journal import build_index, last_session
from cudl.stack import feature_finished, lab_parent, parent_state
from cudl.sessions import (
    current_session, hook_input, hook_session_end, hook_session_start,
    hook_target, pending_notices,
)
from cudl.features import (
    cmd_commit, cmd_new, cmd_open, cmd_push, cmd_squash, cmd_sync, cmd_take, hook_worktree_create,
)
from cudl.finish import cmd_finish, hook_worktree_remove, pending_finish
from cudl.setup import USER_FILES, cmd_add, cmd_init, cmd_install, cmd_setup, cmd_upgrade
from cudl.migrate import cmd_clean_branch, cmd_import_sessions, cmd_split


def cmd_status(a):
    w = Lab.find()
    print(f"{w.label}: {w.top}  [lab branch {head_branch(w.top) or 'detached'}]")
    for r in w.repos().values():
        print("  " + repo_line(w.top, r))
    changes = [l for l in lab_changes(w.top) if not any(l[3:] == r.path for r in w.repos().values())]
    if changes:
        print(f"  lab: {len(changes)} changed")
        for l in changes[:20]:
            print("    " + l)


def cmd_ls(a):
    w = Lab.find()
    for label in ["main"] + w.features():
        top = w.feature_top(label)
        fw = Lab(top)
        changes = [l for l in lab_changes(top) if not any(l[3:] == r.path for r in fw.repos().values())]
        state = f"{len(changes)} changed" if changes else "clean"
        parent = lab_parent(w, label) if label != "main" else None
        pstate = parent_state(w, label) if parent else None
        print(f"{label:<20} {head_branch(top) or 'detached':<24} lab {state}"
              + (f"  ⇐ stacked on {parent}" if parent else "")
              + (f" ({pstate}: `cudl sync -f {label}` to follow the integration branches)" if pstate == "completed" else
                 f" (abandoned: `cudl sync -f {label} --reparent`)" if pstate == "abandoned" else "")
              + ("  (finished, kept)" if label != "main" and feature_finished(w, label) else ""))
        for r in fw.repos().values():
            print("    " + repo_line(top, r))
        last = last_session(top, label)
        if last:
            print(f"    last session: {last}")
    pending = pending_finish(w)
    if pending:
        print(f"\nfinish: {pending}")
    told = pending_notices(w)
    if told:
        print("\nnotices (the next session start shows them):")
        for n in told:
            print(f"  {n.get('time', '?')}: " + str(n.get("text", "")).replace("\n", "\n    "))


def cmd_feedback(a):
    """A dated note about cudl itself, as its own file in the main lab (no shared file to diverge)."""
    w = Lab.find()
    text = " ".join(a.text).strip() or (sys.stdin.read().strip() if not sys.stdin.isatty() else "")
    if not text:
        raise CudlError('what? cudl feedback "<text>"')
    now = datetime.datetime.now().astimezone()
    title = a.title or one_line(text.split("\n")[0], 60)
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:40] or "note"
    d = w.main / "notes" / "cudl-feedback"
    d.mkdir(parents=True, exist_ok=True)
    sid = current_session(w)
    where = w.label + (f", session {sid}" if sid else "")
    for n in range(1, 1000):
        f = d / (f"{now:%Y-%m-%d-%H%M%S}-{slug}" + (f"-{n}" if n > 1 else "") + ".md")
        try:
            with open(f, "x") as out:  # exclusive: a concurrent or repeated entry gets its own file
                out.write(f"# {title}\n\n{now:%Y-%m-%d %H:%M} · from {where}\n\n{text}\n")
            break
        except FileExistsError:
            continue
    rel = str(f.relative_to(w.main))
    with lab_lock(w):
        git_retry(w.main, "add", "--", rel)
        git_retry(w.main, "commit", "-q", "-m", f"cudl feedback: {title}", "--", rel)
    print(f"recorded {rel} in the main lab")


def cmd_index(a):
    w = Lab.find()
    lines = build_index(w.top)
    print(f"journal/INDEX.md: {len(lines)} sessions")


HOOKS = {
    "session-start": hook_session_start,
    "session-end": hook_session_end,
    "worktree-create": hook_worktree_create,
    "worktree-remove": hook_worktree_remove,
}


def cmd_hook(a):
    if a.event not in HOOKS:  # settings that name an event an older cudl had (pre-compact): exit 2 would
        print(f"cudl: no hook {a.event}, nothing to do", file=sys.stderr)  # block a compaction
        return
    try:
        HOOKS[a.event](a)
    except Exception as e:  # a failing hook must not break the session
        print(f"cudl hook {a.event}: {e}", file=sys.stderr)
        sys.exit(1 if a.event.startswith("worktree") else 0)


# cudl acts on the lab of the working directory, so the copy in a feature of the lab that develops cudl
# (wt/<feature>/code/cudl/bin/cudl) would act on that lab with unfinished code. It doesn't: that lab's
# commands and hooks are its own .cudl/cudl's. It acts on any other lab (scratch labs, the tests').


def own_lab():
    """The main lab in whose feature worktree, under code/, this script is: an unfinished copy."""
    me = Path(__file__).resolve()
    for d in me.parents:
        if (d / MARKER).is_file() and (d / ".git").exists():
            try:
                lab = Lab(d)
            except CudlError:
                return None
            return lab.main if lab.feature and me.is_relative_to(d / "code") else None
    return None


def lab_main_of(start=None):
    try:
        return Lab.find(start).main
    except CudlError:
        return None


def guard_own_lab(a):
    own = own_lab()
    if not own:
        return
    if a.cmd == "install":
        target = own  # it changes the global links, wherever it runs
    elif a.cmd == "init":
        target = own if Path(a.dir).resolve().is_relative_to(own) else None
    elif a.cmd == "upgrade":
        target = lab_main_of(a.dir)
    elif a.cmd == "hook":
        target = lab_main_of(hook_target(hook_input(), a.event))
    else:
        target = lab_main_of()
    if target != own:
        return
    me = Path(__file__).resolve()
    if a.cmd in ("status", "ls"):
        print(f"warning: {me} is a feature's unfinished copy of cudl; this lab's own is .cudl/cudl", file=sys.stderr)
        return
    raise CudlError(f"{me} is a feature's unfinished copy of cudl: it doesn't act on {own}, whose commands and "
                    f"hooks are its .cudl/cudl's" + ("" if a.cmd == "install" else
                    f"; try it in a scratch lab: cd <scratch lab> && python3 {me} {a.cmd} …"))


def main(argv=None):
    # no abbreviated options: the agents' permission rules match the command's text (`* --force*`)
    ap = argparse.ArgumentParser(prog="cudl", description=__doc__.split("\n\n")[0], allow_abbrev=False)
    ap.add_argument("-f", "--feature", dest="in_feature", metavar="FEATURE",
                    help="run the command in this feature's worktree (or `main`), from anywhere in the lab")
    sub = ap.add_subparsers(dest="cmd", required=True)
    # the same, after the command: the form the allow rules match (`Bash(.cudl/cudl sync:*)`); its own
    # dest, unset unless given, so a subcommand's default can't overwrite the -f before the command
    here = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    here.add_argument("-f", "--feature", dest="sub_feature", metavar="FEATURE", default=argparse.SUPPRESS,
                      help="run in this feature's worktree (or `main`), from anywhere in the lab")

    def command(name, here_too=False, **kw):
        return sub.add_parser(name, allow_abbrev=False, parents=[here] if here_too else [], **kw)

    p = command("init", help="create a cudl")
    p.add_argument("dir")
    p.set_defaults(fn=cmd_init)

    p = command("upgrade", help="refresh .cudl/cudl, the skills and an unedited AGENTS.md from the tool "
                       "repository (yours to run: it rewrites the agents' instructions)")
    p.add_argument("dir", nargs="?")
    p.add_argument("--take", action="append", choices=sorted(USER_FILES), help="replace this file with the "
                   "template's even if it was edited (repeatable)")
    p.set_defaults(fn=cmd_upgrade)

    p = command("install", help="link cudl into ~/.local/bin and the cudl skill for Claude Code and Codex")
    p.set_defaults(fn=cmd_install)

    p = command("setup", here_too=True, help="rewrite this worktree's settings.local.json (after a clone or move)")
    p.set_defaults(fn=cmd_setup)

    p = command("add", help="add a code repository as code/<name>")
    p.add_argument("name")
    p.add_argument("url")
    p.add_argument("-b", "--branch", help="integration branch (default: the checked-out one)")
    p.add_argument("--from", dest="from_", help="create the -b branch from this upstream branch "
                   "(e.g. -b lab --from main), so your merges don't land on upstream's branch")
    p.set_defaults(fn=cmd_add)

    p = command("new", help="start a feature: lab and code worktrees under wt/<feature>")
    p.add_argument("feature")
    p.add_argument("-r", "--repo", action="append", help="repo(s) the feature changes (comma list or repeat)")
    p.add_argument("--all", action="store_true", help="branch every repo")
    p.add_argument("--base", action="append", help="<repo>=<branch>: a local branch to start from, sync from and "
                   "finish into, instead of the integration branch")
    p.add_argument("--goal", help="first line of the feature's handoff")
    p.add_argument("--from", dest="from_", help="stack on this feature: start from its lab branch and its code branches")
    p.add_argument("--handoff-from", help="seed the handoff from this feature's handoff, or a file (default with --from: the parent's)")
    p.set_defaults(fn=cmd_new)

    p = command("take", here_too=True, help="in a feature, start a branch in a pinned repo")
    p.add_argument("repo")
    p.add_argument("--base", help="a local branch to sync from and finish into (default: the integration branch)")
    p.set_defaults(fn=cmd_take)

    p = command("status", here_too=True, help="this worktree: repos, branches, changes")
    p.set_defaults(fn=cmd_status)

    p = command("ls", help="all features")
    p.set_defaults(fn=cmd_ls)

    p = command("commit", here_too=True, help="commit code repos, then the lab with the new pins")
    p.add_argument("-m", "--message", action="append",
                   help="message for all; <repo>=<msg> or lab=<msg> for one (repeatable)")
    p.add_argument("-a", "--all", action="store_true", help="stage modified tracked files in code repos")
    p.add_argument("-A", "--all-files", action="store_true", help="stage everything in code repos, untracked too")
    p.add_argument("--any-branch", action="store_true", help="allow code repos on unexpected branches")
    p.add_argument("--no-memory", action="store_true", help="from a feature, don't commit memory/ on main")
    p.add_argument("--lab", action="append", metavar="PATH", help="stage only these lab paths (repeatable) "
                   "instead of every lab change")
    p.set_defaults(fn=cmd_commit)

    p = command("sync", here_too=True, help="in a feature, merge the integration branches and main's lab in (the parent's, if stacked)")
    p.add_argument("--reparent", action="store_true", help="the parent was abandoned: follow the integration branches instead")
    p.add_argument("--rebase", action="store_true", help="the parent was rewritten (squash, amend, rebase): move this "
                   "feature's own commits onto it")
    p.add_argument("--force", action="store_true", help="with --rebase: even commits that are on a remote-tracking branch")
    p.set_defaults(fn=cmd_sync)

    p = command("finish", help="merge a feature back and remove its worktrees")
    p.add_argument("feature", nargs="?")
    p.add_argument("--stack", metavar="TOP", help="finish TOP and the features it is stacked on, bottom-up, syncing "
                   "each one first; rerun after a stop to go on")
    p.add_argument("--abandon", action="store_true", help="don't merge; keep branches as abandoned/<feature>-<time>")
    p.add_argument("--force", action="store_true", help="with --abandon: discard uncommitted changes")
    p.add_argument("--no-ff", action="store_true", help="merge code branches with a merge commit")
    p.add_argument("--keep", action="store_true", help="merge but keep the worktrees and branches")
    p.set_defaults(fn=cmd_finish)

    p = command("squash", here_too=True, help="in a feature, fold its commits in each owned code repo into one")
    p.add_argument("-r", "--repo", action="append", help="repo(s) to squash (default: every repo the feature owns)")
    p.add_argument("-m", "--message", action="append", help="message for all; <repo>=<msg> or lab=<msg> for one")
    p.add_argument("--force", action="store_true", help="squash even commits already on a remote-tracking branch")
    p.set_defaults(fn=cmd_squash)

    p = command("keep", here_too=True, help="give every commit the lab's history pins a keep ref in its code repo")
    p.add_argument("--check", action="store_true", help="only check; exit 1 if a pin is not kept or lost")
    p.add_argument("--hook", choices=LAB_HOOKS, help=argparse.SUPPRESS)
    p.add_argument("args", nargs="*", help=argparse.SUPPRESS)
    p.set_defaults(fn=cmd_keep)

    p = command("open", help="open a Claude session for a feature (tmux window)")
    p.add_argument("feature")
    p.add_argument("-a", "--agent", choices=sorted(AGENTS), default="claude", help="agent to start (default: claude)")
    p.add_argument("-c", "--command", help="command to run instead of the agent")
    p.add_argument("--print", action="store_true", help="print the command instead")
    p.set_defaults(fn=cmd_open)

    p = command("push", help="push the code repos' integration branches, or a feature's branches with "
                       "--feature (dry run by default; never tags or keep refs; the lab stays local)")
    p.add_argument("--feature", help="push this feature's code branches instead")
    p.add_argument("--remote", help="push to this remote instead of each branch's configured one")
    p.add_argument("--yes", action="store_true")
    p.set_defaults(fn=cmd_push)

    p = command("split", help="move a code repo's artifact history (e.g. notes/) into the lab, pinned to the code it described")
    p.add_argument("repo")
    p.add_argument("--path", action="append", required=True, help="artifact path in the code repo (repeatable)")
    p.add_argument("--rename", action="append", help="OLD=NEW: where an artifact path goes in the lab")
    p.add_argument("--branch", action="append", help="CODE[=LAB]: code branch to import (default: the integration "
                   "branch), as lab branch LAB (default: split/<repo>/<branch>)")
    p.add_argument("--merge", help="imported branch to merge into lab main (default: the integration branch)")
    p.add_argument("--no-merge", action="store_true", help="keep every import as its own lab branch")
    p.add_argument("--dry-run", action="store_true", help="report what would be imported; write nothing")
    p.set_defaults(fn=cmd_split)

    p = command("clean-branch", help="make a copy of a code branch with the artifact paths removed from every commit")
    p.add_argument("repo")
    p.add_argument("branch")
    p.add_argument("--path", action="append", help="artifact path to remove (default: the paths recorded by split)")
    p.add_argument("--as", dest="as_", help="name of the new branch (default: <branch>-clean)")
    p.add_argument("--dry-run", action="store_true", help="report what would change; write nothing")
    p.set_defaults(fn=cmd_clean_branch)

    p = command("import-sessions", help="index Claude Code and Codex sessions from before the lab, as legacy journal entries")
    p.add_argument("path", nargs="+", help="directories the old sessions ran in (worktrees below them are included)")
    p.add_argument("--dry-run", action="store_true", help="list what would be imported")
    p.add_argument("--claude-projects", help="Claude Code projects directory (default: ~/.claude/projects)")
    p.add_argument("--codex-sessions", help="Codex sessions directory (default: ~/.codex/sessions)")
    p.set_defaults(fn=cmd_import_sessions)

    p = command("feedback", here_too=True, help="record a note about cudl itself (a file in the main lab's notes/cudl-feedback/)")
    p.add_argument("text", nargs="*")
    p.add_argument("--title")
    p.set_defaults(fn=cmd_feedback)

    p = command("index", here_too=True, help="rebuild journal/INDEX.md")
    p.set_defaults(fn=cmd_index)

    p = command("hook", help="Claude Code hook entry points")
    p.add_argument("event", help=f"{', '.join(sorted(HOOKS))}; any other does nothing")
    p.add_argument("--agent", choices=sorted(AGENTS), default="claude", help="the agent whose hook this is")
    p.set_defaults(fn=cmd_hook)

    a = ap.parse_args(argv)
    sub_feature = getattr(a, "sub_feature", None)
    if sub_feature and a.in_feature and sub_feature != a.in_feature:
        ap.error(f"-f {a.in_feature} and -f {sub_feature} name different features")
    a.in_feature = a.in_feature or sub_feature
    try:
        if a.in_feature:
            top = Lab.find().feature_top(a.in_feature)
            if not top.is_dir():
                raise CudlError(f"-f {a.in_feature}: no such feature")
            os.chdir(top)
        guard_own_lab(a)
        a.fn(a)
    except CudlError as e:
        print(f"cudl: {e}", file=sys.stderr)
        sys.exit(1)
