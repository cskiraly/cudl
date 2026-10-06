# cudl: Code Under Development Lab

This directory is a cudl: a private git repository holding the working artifacts of one line of
work, with the code under development as git submodules. `.cudl/cudl` is a helper for the chores
around it; Claude Code and Codex both read this file and the skills in `.agents/skills/`, and share
the memory, journal and handoffs.

## Layout

- `code/<repo>/` — the code, one submodule per repository. Only code goes here.
- `notes/`, `docs/` — notes, reports, figures, pinned specs. `plans/` — plans (write them here).
- `handoff/<feature>.md` — one page: where this line of work stands and what's next
  (`handoff/main.md` for the main lab).
- `journal/sessions/` — one log per session; `journal/INDEX.md` (generated); `journal.md` for
  findings and decisions.
- `memory/` — shared memory, one fact per file, indexed by `memory/MEMORY.md`.
- `wt/<feature>/` — feature worktrees: a full lab on branch `feat/<feature>`, whose `code/<repo>`
  are worktrees of the same repositories, on branch `<feature>`.
- `scratch/` — gitignored.

## The few rules

- **Artifacts never go in `code/*`.** If a file wouldn't belong in an upstream PR, it belongs here.
- **Never push, and never rewrite an integration branch or commits that are on a remote.**
  `finish`, `push` and `upgrade` are the user's to run; suggest them.
- **Write findings and decisions down when they're settled** (`notes/`, `journal.md`), and use the
  `wrap` skill before a session ends.

## Working

- Use git as you normally would, in the lab and in `code/*`. `.cudl/cudl commit` is a shortcut:
  it commits the code repos with changes and then the lab with their new pins, in one step. Pins,
  and the memory and plans a session wrote, are also committed at session end, and every commit
  the lab ever pinned is kept (`refs/cudl/keep/<sha>`), so rebasing or amending a feature's own
  branch is safe.
- Code changes usually belong in a feature: `.cudl/cudl new <name> -r <repo> --goal "…"`, or
  `new <name> --from <feature>` to build on an unfinished one (it brings that feature's plans,
  notes and handoff). One session can create and work in several features.
- Your shell may keep returning to the directory the session started in. Address features
  explicitly: `.cudl/cudl <command> -f <feature>` for cudl (`-f` after the command, the form
  that runs without asking), `git -C wt/<feature>/code/<repo> …` and absolute paths for
  everything else. Run `.cudl/cudl` from the lab's top directory: inside `code/<repo>` that path
  would be a file of the repository's own.
- `.cudl/cudl status` and `.cudl/cudl ls` show where things stand; `.cudl/cudl squash` folds a
  feature's commits into one. When a feature's parent was rewritten, `.cudl/cudl sync -f
  <feature> --rebase` moves the feature's own commits onto it; with `--force` (published commits)
  it's the user's call, like any `--force`.
- Feedback about cudl itself: `.cudl/cudl feedback "…"` (one file per entry, in the main lab).

## Started by another agent

If the session-start context says you are another session's subagent (a Codex review run from a
Claude skill, say): do the task and report back. Don't commit, don't `wrap`, and don't edit
`handoff/`, `journal/` or `memory/` unless the task asks; the session that started you owns them.

## Memory

- Claude Code: auto memory already points at the shared `memory/`; use it as usual.
- Other agents (Codex): don't use built-in memories here. The session-start context includes
  `memory/MEMORY.md`; read a memory file when its line is relevant. To remember something, write
  one file per fact in the shared `memory/` (frontmatter `name`, `description`, `metadata.type`:
  `user`, `feedback`, `project` or `reference`; then the fact) and add a one-line pointer to
  `MEMORY.md`. Update an existing file rather than adding a duplicate.
