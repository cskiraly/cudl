---
name: feature
description: Work with features in a cudl — start one (optionally on top of another), list them, bring one up to date, squash its commits, or prepare finishing it.
argument-hint: "new <name> [repos] | ls | sync | squash | finish <name>"
disable-model-invocation: true
---

A feature is a lab worktree `wt/<name>/` on branch `feat/<name>`, with a branch `<name>` in each
code repo it changes; other repos are pinned. One session can create and work in several
features: address them with `.cudl/cudl <command> -f <name>` and
`git -C wt/<name>/code/<repo> …`.

Requested: $ARGUMENTS

- **new** — `.cudl/cudl new <name> -r <repo>[,<repo>…] --goal "<one line>"`; to build on an
  unfinished feature, `--from <feature>` (brings its plans, notes, handoff and code branches). Then
  work in it here, or suggest `.cudl/cudl open <name>` to the user for a parallel session.
- **ls** — `.cudl/cudl ls`: every feature, its stack, what's unrecorded, each one's last session.
- **sync** — `.cudl/cudl sync -f <name>` takes in the integration branches (or the parent, for a
  stacked feature) and main's lab. On a conflict, resolve it in the repo it names, commit, rerun.
  If the parent was rewritten, `sync --rebase` moves the feature's own commits onto it (published
  ones only with the user's `--force`).
- **squash** — `.cudl/cudl squash -f <name>` folds the feature's commits into one per repo.
- **finish** — the user runs it. Check with `ls` that the feature is committed and in sync, then
  give them `.cudl/cudl finish <name>`, or for a stacked feature `.cudl/cudl finish --stack <name>`
  (it and everything under it, bottom-up; rerun it after a stop).
