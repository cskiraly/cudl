---
name: cudl
description: Set up or migrate to a cudl — a private git repo holding agent artifacts (memory, plans, notes, session journal) with the code as submodules, managed by the cudl tool. Use when the user wants to wrap a repository, move notes or memory out of a code repo, start parallel features across repos, or asks whether the current directory is in a cudl. Also use it first, before any other work, when the working directory is inside a code checkout of a cudl lab (a path like <lab>/code/<repo> or <lab>/wt/<feature>/code/<repo>, with a .cudl.json in <lab>): the session was started in the wrong place.
---

A cudl keeps the code repositories clean: everything that would not belong in an upstream
PR lives in the lab, and each lab commit pins the code commits it describes. `cudl` does
the two-level git; the design is in DESIGN.md next to the `cudl` tool (`cudl --help` for commands).

**First, find out where you are.** If a `.cudl.json` exists here or in a parent directory, you are
in a lab.
- If the working directory is inside the lab's `code/<repo>` or `wt/<feature>/code/<repo>`, the
  session was started in the wrong place. A code repository is its own git root, so the lab's hooks
  don't run (no session log), memory doesn't go to the lab, and the lab's `AGENTS.md` wasn't loaded.
  Before the task, tell the user: "This session started inside <path>, a code repository of the cudl
  lab at <lab>: restart it from <lab> (or <lab>/wt/<feature> for a feature)." Until then, write no
  notes, plans or memory in the code repository. If you can't read the parent directory to check, go
  by the path.
- Anywhere else in a lab, follow its `AGENTS.md` and its own skills (`commit`, `feature`, `wrap`), and
  stop here.

If `cudl` is not on PATH, tell the user to run `cudl install` from the cudl tool repository.

## Wrap a repository (new lab)

1. Agree on a name and location with the user, e.g. `~/work/<topic>-lab`, and on which repos go in
   and each one's integration branch.
2. `cudl init <dir>`, then in it, per repo: `cudl add <name> <url-or-local-path> -b <branch>`.
   For an upstream you don't own, prefer an integration branch of your own:
   `cudl add <name> <url> -b lab --from main`.
   A local path makes the existing clone the submodule's remote; a real URL makes the submodule
   independent of it. Ask which the user wants.
3. Tell the user the one-time trust steps that `cudl init` printed (Claude trust dialog; Codex
   project trust and `/hooks`).
4. Fill in `handoff/main.md` with the goal and state of the work.

## Migrate an existing repo's artifacts (do not touch the original without asking)

Work on copies and show the user each step before it changes anything outside the new lab.

- **Notes committed in the code repo** (e.g. `notes/`): after `cudl add`, run
  `cudl split <repo> --path notes/ --dry-run` and show the user the report (commit count, sizes,
  unpushed-pin warning), then without `--dry-run`. It imports the history into the lab, each commit
  pinned to the code commit it came from; the code repo is only read. Add `--path` for former
  names of the paths, `--rename OLD=NEW` for a different lab layout, `--branch` for other
  branches. Stopping the code repo from carrying the paths (`git rm -r notes/`) is the user's
  decision.
- **A publishable copy without the artifacts** (a release, an upstream PR):
  `cudl clean-branch <repo> <branch> --dry-run`, then without it. It adds `<branch>-clean` to the
  code repo and changes nothing else; pushing it is the user's call.
- **Claude Code memory**: copy `~/.claude/projects/<escaped-path>/memory/*` into the lab's
  `memory/`, keep `MEMORY.md`, and commit with `cudl commit -m "memory: import from <repo>"`.
- **Plans** from `~/.claude/plans/` that belong to this work: copy them into `plans/`.
- **Old sessions** (Claude Code and Codex) that ran in the repo or its worktrees:
  `cudl import-sessions <repo path> [<worktree or sibling paths>…] --dry-run`, show the user the
  list, then run it without `--dry-run`. It indexes them as legacy journal entries pointing at
  their transcripts; nothing is copied or moved.
- **Existing worktrees** of the code repo: list them, check each for uncommitted work, and propose
  `cudl new <feature> -r <repo>` for the ones still active; the user removes the old ones.

## Never

Push anything, remove the user's existing clones or worktrees, or rewrite published history.
