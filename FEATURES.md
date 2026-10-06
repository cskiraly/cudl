# What cudl does

Everything cudl does, area by area. [README.md](README.md) introduces cudl and how you work with
it; [DESIGN.md](DESIGN.md) explains why it is built this way.

## The lab

- A private git repo per line of work. It holds memory, plans, notes, docs, handoffs and a session journal, with the code repos as submodules in `code/<repo>`.
- Each lab commit pins the code commits it describes, with a `Code:` line per repo in the commit message.
- Code repos stay code-only, so publishing is a plain push of the code branch with nothing to strip.
- `cudl init`, `cudl add <repo> <url> -b <branch>`, plus `cudl setup` and `cudl upgrade` for maintenance. `upgrade` is yours to run (agent sandboxes refuse it, since it rewrites the agents' instructions); it also updates `AGENTS.md` and `.claude/settings.json` while you haven't edited them, and otherwise keeps them and shows how to compare (`--take agents-md` or `--take settings` replaces one).
- `cudl add <repo> <url> -b lab --from main` gives a repo an integration branch of your own, started from upstream's, so merged features don't pile onto upstream `main`. `cudl push` skips a branch that has no push remote configured, so it never lands on upstream by default.
- `cudl split <repo> --path notes/` moves an existing repo's artifact history into the lab: each imported commit keeps its author, date and message and pins the code commit it came from, so `git log -p -- code/<repo>` shows what the code was for every note. The code repo is only read; `--dry-run` reports commits, sizes and pins not yet pushed.
- `cudl clean-branch <repo> <branch>` writes `<branch>-clean`: the same history without the artifact paths, for a release or an upstream PR. The original branch and every published SHA stay as they are; a map of original to clean commits goes into the lab.

## Parallel features

- `cudl new <feat> -r <repos>` creates a lab worktree on branch `feat/<feat>`. Each listed repo's `code/<repo>` is a worktree of the shared submodule, so all features share one object store per repo.
- Repos a feature doesn't own are checked out read-only at the lab's pinned commit. `cudl take <repo>` gives one a feature branch. A repo main adds later comes in with `sync`, checked out the same way (`take` checks out one an older cudl's sync left empty).
- A feature follows a base in each repo it owns: the integration branch, or another local branch given with `new --base <repo>=<branch>` (or `take --base`). A remote-tracking branch is refused, with the command that makes a local one; a feature recorded with one (older cudl took it) gets the repair from `sync` and `finish`.
- `cudl open <feat> [--agent claude|codex]` starts an agent session in a tmux window.
- `cudl ls` and `cudl status` show every feature at both levels: branches, ahead/behind, uncommitted changes, code commits the lab hasn't recorded, and the last session.
- `cudl sync` merges the integration branches and the main lab into a feature. It merges rather than rebases, so pinned commits stay reachable.
- `cudl finish <feat>` fast-forwards the code branches, merges the lab branch, never leaves a detached HEAD, and removes the worktrees. It refuses and changes nothing when a feature isn't clean or up to date, or a session still works from it (its latest start ran there, or it moved in with EnterWorktree; one that only worked there with `-f` gets a note), and if it stops half-way (a hook, signing), rerunning it picks up where it stopped.
- `cudl finish --abandon` removes the worktrees but keeps the branches as `abandoned/<feat>-<time>`, waiting, as `finish` does, for a session that still works from it unless `--force`; rerun after a stop, it goes on where it stopped.
- Both keep the feature's ignored lab files (`scratch/`, `journal/raw/`) in the main lab's `scratch/<feat>-<time>/`: removing a worktree would delete them.

## Rewrites and stacked features

- Every code commit the lab's history pins is kept (`refs/cudl/keep/<sha>`), so squashing, rebasing or amending a feature branch never breaks old lab commits; `cudl keep --check` audits it.
- `cudl squash` folds a feature's commits into one; it refuses commits already on any remote.
- `cudl new <child> --from <feature>` stacks a feature on an unfinished one: its plans, notes, handoff and code branches. `sync` follows the parent, and once the parent is finished, wherever the parent was merged.
- When the parent was rewritten (squashed, amended), `sync` stops and `cudl sync --rebase` moves the child's own commits onto it. It refuses published commits without `--force` and merges that a rebase would lose, and a rebase finished by hand is recognised.
- `cudl finish --stack <top>` finishes a whole stack bottom-up, syncing each level first. It checks every level before it starts and is resumable after a stop.

## Working the way you already do

- Plain git works in the lab and in `code/*`: lab git hooks keep every pinned commit, and the session-end hook records code commits nobody pinned yet.
- One session can work across features: `cudl <command> -f <feature>` from anywhere (`-f` after the command: the form the agents' permission rules allow); the session gets a linked log in each feature where it changes something through cudl, or resumes, and `/wrap` covers them all.
- `cudl feedback "…"` records a note about cudl as its own file in the main lab.
- `cudl push --feature <f> [--remote fork]` pushes a feature's code branches; pushes are branches only, never tags.

## Committing at both levels

- `cudl commit` commits every code repo with changes, then the lab with the new code pointers.
- One message for all, or separate ones with `-m <repo>=…` and `-m lab=…`.
- It checks everything before committing anything: read-only repos, wrong branches, rebases in progress, conflicts.
- If a commit fails partway, rerunning picks up where it stopped.
- Memory written from a feature is committed on the main lab.

## Sessions and journal

- One log per session in `journal/sessions/`, with the agent's summary on top and done, learned and dead-end sections below.
- `journal/INDEX.md` is generated from the logs and rebuilt after merges, so it never conflicts.
- `journal.md` holds curated findings; `handoff/<feature>.md` is a one-page state per feature.
- **Small starting context:** a session starts with only the handoff, the last 10 index entries and where things stand.
- **Hooks:**
  - session start creates the log and shows what cudl did since the last start where nobody saw it (a worktree removed or kept at an exit);
  - session end records commits, touched files and the transcript path (found by the session's id if it moved), then commits the session's logs, wherever they are, with the memory and plans the session wrote (a session that never got going, like a `claude -p` that failed on its arguments or a session closed before its first prompt, leaves no log, whatever was left uncommitted before it started);
  - session end gives the memory index a line for any memory file it doesn't list (two sessions writing the index at once can lose one).
  - a session's end counts its compactions in its transcript; the `wrap` skill asks to be used when the context is getting long.
  - a session whose end couldn't commit its log (a commit that failed on signing, say) leaves it with `ended:`; `sync` and `finish` commit it, as the end would have, instead of stopping.
- A `/wrap` skill fills in the log, rewrites the handoff and commits.
- `cudl import-sessions <paths>` indexes older Claude Code and Codex sessions as legacy entries. It finds them by recorded working directory, worktrees included; it's idempotent and handles ids that share a prefix.

## Agents

- **Claude Code:**
  - shared memory via `autoMemoryDirectory`, and plans in each worktree's own `plans/`;
  - hooks for session start and session end;
  - Claude's own worktree command creates cudl features; "Remove worktree" on leaving `claude -w` commits what the session left onto the feature's branches and abandons it (branches kept). Whatever stops that (another session still there, a rebase in progress, a checkout on another branch) keeps the worktree, and the next session start says why.
- **Codex:**
  - `AGENTS.md` instructions, shared with Claude through `CLAUDE.md` = `@AGENTS.md`;
  - skills in `.agents/skills/`, symlinked into `.claude/skills/` for Claude;
  - the same hooks via `.codex/hooks.json`;
  - its session context includes the memory index.
- Session logs record which agent ran them.
- `AGENTS.md` tells the agent to build in a feature (proposing `cudl new` from the main lab) and to write findings down when they're settled, not only at `/wrap`. `cudl upgrade` keeps it in step with the template while you haven't edited it; once you have, it keeps yours and tells you how to compare.
- Agents started by agents: Codex run from a Claude session (a review through a skill, say) is logged as that session's subagent, with its prompt; it gets a short context telling it not to commit or wrap, commits nothing itself, and is listed under its parent in the index.
- Skills: `commit`, `feature` (you start it, not the agent), `wrap`, plus a user-level `cudl` bootstrap skill for wrapping or migrating a repo.

## Safety

- The agent never pushes: `cudl push` is a dry run unless given `--yes`, and `cudl finish` is left for you to run.
- Hooks never break a session.
- A code repo under development is not trusted for more than its code. Claude's hooks find the lab's `cudl` from the directory the session started in, not wherever the agent went; a repo's own `.cudl.json` doesn't make it a lab; a repo that ships a `.cudl` gets a warning at session start, since inside it `.cudl/cudl` is the repo's file.
- What an agent passes is checked before cudl uses it as a path: `-f` takes only a feature name, `new --handoff-from` a feature or a file inside the lab, and a session id has to be fit for a file name. `import-sessions` skips transcripts whose ids aren't, and names logs by the day only. The memory index doesn't read through links, and the lab's git hooks quote its path.
- `cudl install` puts `cudl` on your PATH and installs the bootstrap skill for both agents.
- Developing cudl in a lab: the copy in a feature's `code/` doesn't act on that lab (its `.cudl/cudl` does); try it in a scratch lab.
- End-to-end tests run on real git repos.

## Not built or not yet verified

- a summarizer for sessions that ended without `/wrap`.
