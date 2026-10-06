# cudl: Code Under Development Labs

One repository for the agent's work, with the code inside it. The name follows *device under
test*: a cudl holds the code under development the way a test bench holds the DUT.

## The problem

An agent working on a code repository produces two kinds of output: the code, and everything
around it — plans, the journal, the handoff note, figures, the agent's memory, support documents.
Both need version control, but not the same one:

- the code is published: its branches go upstream, its history should read as code;
- the artifacts are private working state: they describe the code at some commit, often span
  several repositories, and must never leak into a pushed branch.

Where cudl started, on research work in a fork of a large open-source client, the two were mixed,
and it showed:

- The fork carried `notes/` (journal, `HANDOFF.md`, plans, figures) as commits on the code
  branch, so a published snapshot had to be cut by hand as "that commit, without its notes".
- The agent's memory lived in `~/.claude/projects/<the repository's path>/memory`, under
  no version control; plans lived in `~/.claude/plans`, shared by every project. Neither recorded
  which code commit it was written against.
- Worktrees were wherever they landed: siblings of the repository, `.claude/worktrees/` inside it, a
  scratchpad path under `/tmp`.
- One piece of work touched several repos (the client, a library it uses, a repository of
  experiments), but each repo's notes only saw their own.

## The model

One **lab repository** per line of work. It holds the artifacts as ordinary files and the code
as **git submodules**. A lab commit is a consistent snapshot: these notes, this memory, these
plans, written against these code commits.

```
myproject-lab/                    # the lab: a private git repo, never pushed upstream
  CLAUDE.md                       # instructions for the agent working on this line of work
  .claude/settings.json           # memory + plans directories, worktree hook (below)
  memory/                         # the agent's auto-memory, now versioned
  plans/                          # plans, versioned
  handoff/<feature>.md            # one page per feature (main.md for the main lab)
  journal.md, journal/            # curated storyline; session logs and their index
  notes/                          # figures/, reports
  docs/                           # support documents, specs pinned for reference
  code/
    client/                       # submodule → a fork of the upstream client, on the work's branch
    library/                      # submodule → a fork of a library it uses
    experiments/                  # submodule
  wt/<feature>/                   # gitignored: feature worktrees (see Parallel features)
  scratch/                        # gitignored: cell outputs, logs, throwaway
```

Rules of the model:

1. **The code repos hold only code** — plus the agent files the upstream project itself wants
   (`AGENTS.md`, `.agents/skills`, a code-level `CLAUDE.md`). No `notes/` in a code branch.
   Publishing is `git push` of the code branch; there is nothing to strip.
2. **Everything else lives in the lab.** If a file would embarrass a PR, it belongs here.
3. **A lab commit pins code SHAs.** When a note describes new code, commit the note together
   with the submodule pointer bump that it describes (`git add notes/ code/client && git commit`).
   `git log -p code/client` in the lab then reads as "what the code was when we wrote this".
4. **Worktrees are cheap and unversioned.** They live under `wt/`, gitignored. The lab never
   records a worktree, only a SHA on a branch of the submodule.
5. **A publication-pin repo stays separate.** A repository that pins published code per tag (the
   code behind a paper, say) can point at the same SHAs the lab recorded, and it does not need the
   lab.

## Claude Code wiring

Launch `claude` **from the lab root**. That one choice makes most of the wiring work:

- **Project settings** (`.claude/settings.json`) are read from the project root, so the lab's
  settings apply to the whole session.
- **`CLAUDE.md`** files load up the ancestor chain from the working directory, and nested ones load
  when the agent works in that subtree, so `code/client/CLAUDE.md` still reaches the agent when it
  edits the client. The chain stops at the git root. A session started inside `code/client`, its own
  git root, gets none of the lab's: not its `CLAUDE.md`, not its settings or hooks (checked live).
  See "Status" for the guard. The lab's `CLAUDE.md` says where things live: "notes go in `notes/`, never in
  `code/*`; commit pointer bumps with the note that explains them".
- **Memory.** By default the memory directory is keyed by the git repository the session starts in,
  which is now the lab. Make it explicit and versioned instead:

  ```json
  {
    "autoMemoryDirectory": "/home/you/work/myproject-lab/memory",
    "plansDirectory": "plans"
  }
  ```

  `autoMemoryDirectory` must be absolute or start with `~/`, and Claude Code ignores it in the
  checked-in `.claude/settings.json`; so both go in the gitignored `.claude/settings.local.json` of
  each worktree, which `cudl init`, `cudl new` and `cudl setup` write. `plansDirectory` is relative
  to the project root, so `plans` is each worktree's own `plans/`. Plans then stop going to the
  global `~/.claude/plans`.
- **Worktrees.** By default `claude --worktree` / EnterWorktree create `.claude/worktrees/<name>` at
  the repository root. A `WorktreeCreate` hook replaces that: it receives `worktree_name`, runs
  `cudl new <name>` (a feature with every repo pinned), and prints `wt/<name>`. A matching
  `WorktreeRemove` hook runs `cudl finish <name> --abandon`, which keeps the branches.
- **Scope of the agent's view.** `wt/` and `scratch/` are gitignored, so default searches skip
  them; the agent reaches a worktree by path when the task is about it.

Decide per lab: one memory for the whole line of work (the default here, since the lessons
cross repos), or a memory per code repo (`memory/client/`, …, selected by a per-repo setting).
Start with one.

## Codex, and agents in general

`cudl` is agent-neutral; each agent gets a thin adapter of instructions, skills and hooks that
call `.cudl/cudl`. Claude Code and Codex are close enough that most of it is shared.

| need | Claude Code | Codex | in the lab |
| --- | --- | --- | --- |
| instructions | `CLAUDE.md` | `AGENTS.md`, git root → cwd, 32 KiB | `AGENTS.md` is the file; `CLAUDE.md` is `@AGENTS.md` |
| skills | `.claude/skills/` | `.agents/skills/` (cwd up to repo root), same Agent Skills format | skills live in `.agents/skills/`; `.claude/skills/<name>` are symlinks |
| hooks | `.claude/settings.json` | `.codex/hooks.json`, same shape | both call `.cudl/cudl hook <event> --agent <name>` |
| memory | `autoMemoryDirectory` → shared `memory/` | built-in memories are global, no per-project directory | Codex's stay off; its SessionStart context carries `memory/MEMORY.md`, and `AGENTS.md` says how to write memory files |
| plans | `plansDirectory` | no setting | `AGENTS.md`: plans go in `plans/` |
| worktrees | WorktreeCreate hook → `cudl new` | `/worktree` goes to `$CODEX_HOME/worktrees`, no hook | `AGENTS.md`: don't; `cudl new` + `cudl open <feat> --agent codex` |
| trust | trust dialog once per lab | project trust, and each hook trusted by hash in `/hooks` | `cudl init` prints both steps |

So both agents share one memory, one journal and the same handoffs; session logs carry
`agent: claude|codex`, and a Claude session on one feature can run beside a Codex session on
another. Both agents' hook commands walk up to the nearest `.cudl/cudl`. Claude's start from
`CLAUDE_PROJECT_DIR`, the directory the session started in: Claude Code runs hooks wherever the
agent went, which can be a code repo with a `.cudl/cudl` of its own. Codex's start from the working
directory, which for its hooks is always the session's own. Walking up by name also reaches the lab
from a `claude -w` worktree that "Remove worktree" deleted, which `CLAUDE_PROJECT_DIR` still names
when the session's end runs.

Two kinds of skills:

- **In-lab:** `commit`, `feature`, `wrap` (Claude `/commit`, Codex `$commit`). `feature` is
  user-invoked only (`disable-model-invocation` for Claude, `agents/openai.yaml` policy for Codex).
- **Bootstrap, user-level:** `skills/cudl` in this repository, linked by `cudl install` into
  `~/.claude/skills` and `~/.agents/skills` (and `bin/cudl` into `~/.local/bin`). It covers the time
  before a lab exists: wrapping a repository, migrating a repo's notes and memory.

## Version control, in practice

What a scratch experiment showed (a lab with a small repository as `code/netlab`, a worktree
under `wt/netlab/t1`):

- `git submodule add -b <branch>` records the branch in `.gitmodules`; the submodule's git dir lives
  in `lab/.git/modules/code/netlab`, and `code/netlab/.git` is a file pointing there.
- `git -C code/netlab worktree add ../../wt/netlab/t1 -b t1` works. `worktree list` names the main
  worktree as `.git/modules/code/netlab` rather than `code/netlab`, which looks odd but is harmless.
- **A commit in a worktree does not show in the lab** (`git status` clean). Only a commit that
  moves `code/netlab`'s checked-out HEAD shows up, as ` M code/netlab`. So bring worktree results into the
  lab by checking out or merging the branch in `code/<repo>`, then bump the pointer.
- **A lab clone fails if the pinned commit was never pushed** to the submodule's URL:
  `Fetched in submodule path 'code/netlab', but it did not contain 52e0f2c…`. The pin is only as good
  as the code remote. Push code branches before (or with) the lab:
  `git push --recurse-submodules=check`, or set `push.recurseSubmodules=on-demand`.

Settings that take the friction out of submodules (set in the lab's `.git/config` or globally):

| setting | effect |
| --- | --- |
| `submodule.<name>.branch` (via `add -b`) | `git submodule update --remote` follows the branch |
| `submodule.recurse=true` | checkout/pull in the lab also move the submodules (cudl itself runs with it off and moves the submodules explicitly) |
| `diff.submodule=log` | lab diffs show the code commits a bump spans |
| `status.submoduleSummary=true` | lab status lists them too |
| `push.recurseSubmodules=on-demand` | pushing the lab pushes the pinned code commits first |

Two traps to avoid:

- **Detached HEAD.** `git submodule update` checks out the pinned SHA detached. Work in the
  submodule on a branch (`git -C code/client switch <branch>`) or use worktrees under
  `wt/`; never commit on a detached HEAD there.
- **Per-repo tools index per repo.** GitNexus and similar tools index `code/<repo>` or a worktree,
  not the lab; point them there.

## Sessions and the journal

Sessions are short and start from a small, fixed context. What they did is kept as an indexed
log, not in the agent's context.

```
journal/
  INDEX.md                                   # generated: one line per session
  sessions/2026-09-24-dial-fix-a1b2c3.md     # one file per session, never rewritten
journal.md                                   # curated storyline, cites sessions
handoff/<feature>.md                         # one page: current state, rewritten each session
```

A session log is frontmatter plus about 20 lines of prose (done, learned, dead ends):

```yaml
session: a1b2c3…            # Claude session id → raw transcript
date: 2026-09-24
feature: dial-fix
goal: bound the dial deadline
repos: {client: 3994aa2..5e1f0c8, experiments: aa43baf}
touched: [notes/dial-timeouts.md, plans/dial-fix.md]
tags: [dial, timeouts]
outcome: done | partial | abandoned
next: rerun the slow-peer benchmark
```

Three layers, each with one job:

- **Session logs** say what happened. They are append-only, one file per session, so parallel
  sessions never conflict.
- **`journal.md`** says what we now know: findings and decisions, each citing the sessions it came
  from. It is edited on purpose, not appended every session.
- **`handoff/<feature>.md`** says where to pick up: one page, rewritten at every wrap.

`INDEX.md` (and per-tag indexes, if wanted) is built by a script from the frontmatter, never edited
by hand, and rebuilt after every merge, so it cannot drift or conflict.

**Keeping sessions short:**

- A `SessionStart` hook loads only the feature's handoff, the last ~10 `INDEX.md` lines and the lab
  `CLAUDE.md`. Older logs and the journal are read on demand, never preloaded.
- A `/wrap` skill ends a session: it writes the session log, rewrites the handoff, rebuilds the index
  and runs `cudl commit`.
- Compaction: the `wrap` skill asks to be used when the context is getting long, and a session's end
  counts the compactions its transcript records (`compactions:`). (A `PreCompact` hook used to
  count them; before that, the start context after a compaction asked for `/wrap`.)
- The facts don't depend on the agent. A `SessionEnd` hook records the session id, repo SHAs at
  start and end, files touched and the transcript path, even after a crash or a plain quit. A
  session that ended without `/wrap` is marked `needs_summary: yes`, for a summarizer that isn't
  built yet (see "Status").

Raw transcripts (`~/.claude/projects/…/*.jsonl`, several MB each) are referenced by session id and
not committed. A compact extract (prompts plus tool-call summaries) can go in `journal/raw/`,
gitignored or in Git LFS.

**Sessions from before the lab.** Claude Code keeps sessions per working directory
(`~/.claude/projects/<escaped path>/`), so a lab at a new path does not list, or resume, the old
repo's sessions; they stay resumable from the old directory. `cudl import-sessions <old path>…`
indexes them instead: it finds the Claude Code transcripts and Codex rollouts whose recorded
working directory is one of the paths or below it (worktrees included, a sibling like
`client-old` excluded), and writes a legacy log for each: `feature: legacy`, the agent, start
and end, the branch, the first real prompt as the goal, the transcript path, `needs_summary: yes`.
Only the stubs are committed; the transcripts stay where they are. It is idempotent, and a lab's
sessions take it under a second.

Session logs are named by an id prefix, 8 characters unless that name is taken: Codex ids are
time-ordered, so sessions started together share their first characters, and
a log is looked up by the full id in its frontmatter, not by the prefix.

Claude Code deletes transcripts after `cleanupPeriodDays` (default 30); set it high in
`~/.claude/settings.json` before relying on transcripts. Transcripts restored from a backup into
`~/.claude/projects/` (only files that are missing, e.g. `rsync -a --ignore-existing`) are picked
up like any other.

## Lessons from the first pilot

- The start context is for the agent; the user saw nothing and wondered whether it worked. The
  hook now also returns one line for the user (`cudl: main lab · log … · handoff loaded`).
- After a long design discussion in the main lab, nothing was committed and the conclusions lived
  only in the transcript, and the building then started in the main lab too, where the code repo
  was on upstream's `main`. Two rules now, in `AGENTS.md`: build in a feature, proposing
  `cudl new` from the main lab; write findings down when they're settled. (They were in the start
  context too, until it was made to list facts only; `cudl upgrade` keeps an unedited `AGENTS.md`
  in step with the template.)
- The integration branch was upstream's `main`, so merged features would pile onto a local
  `main` that diverges from upstream. `cudl add -b lab --from main` makes an integration branch of
  your own; bring upstream in with `git -C code/<repo> merge origin/main`. `cudl push` now needs a
  push remote configured on the branch and skips it otherwise, so a new branch can't go to
  upstream by default.

- The agent, asked to take all of the recommended steps, ran `cudl upgrade && cp …/AGENTS.md
  AGENTS.md`, and Claude Code's auto mode refused it as self-modification: it rewrites the
  agent's own instructions. That is right, so `upgrade` is now marked as the user's to run
  (`AGENTS.md`, the command's help), and it is one command instead of two: the marker records the
  hash of the `AGENTS.md` cudl last wrote, and an `AGENTS.md` that still has it follows the
  template; an edited one is kept, with a diff command and `--take agents-md`. A feature made
  before an upgrade has the old files on its branch; `cudl sync` in it brings the upgrade in.

## Rewrites, pin retention and stacked features

In the second pilot round a feature needed a squash before publishing, then follow-up features on
top of it. What that exposed, and what cudl does now:

- **Every pin the lab records is kept**, as `refs/cudl/keep/<sha>` in its code repo: flat, since
  pins outlive features. After each lab commit cudl makes, and for every lab commit brought in by
  `sync`, `finish` or `split`, the gitlinks those commits write (`git log -m --raw -- code/`, so
  merges count) get their ref; `finish` keeps the whole feature branch first, so a forced abandon
  after a rewrite loses nothing. Checking only "old pin vs new pin" wouldn't do: it misses merged
  histories, imports and pins recorded before a rewrite. `cudl keep` backfills
  the refs over all lab branches; `cudl keep --check` verifies them (a ref, not mere existence:
  an unreachable object resolves until gc drops it) and lists pins that are unkept or already
  lost; the pins of a repo taken out of the lab are counted apart, with nowhere to be kept.
  `log.excludeDecoration` keeps the refs out of `git log --decorate`. `cudl push` names
  branches, so they stay local; a raw `git push --mirror` would publish them. A clone of the lab
  elsewhere can't restore pins that exist only under local keep refs.
- **`cudl squash`** folds a feature's commits in each owned code repo into one (message: the first
  subject, plus the list of squashed subjects), through `cudl commit`. Only in a feature, only on
  its own branch, clean worktree, and not over commits that are on any remote-tracking branch
  unless `--force` (a branch can be on a fork with no upstream configured, which an
  upstream-only check would miss).
- **`cudl new <child> --from <parent>`** starts the child's lab branch at `feat/<parent>` (plans,
  notes), branches the code repos the parent owns from the parent's branches, and seeds the
  handoff from the parent's (`--handoff-from` for any feature or file). Recorded: the parent
  (`branch.feat/<child>.cudlparent`), and per repo the base and the **last parent commit taken
  in** (`cudlparenttip`, updated on each sync; a creation-time fork point would miss a rewrite
  after a sync). `sync` merges the parent (code and lab) and stops, changing nothing, when the
  parent was rewritten under the child, with the `rebase --onto` to run. `finish` is bottom-up:
  refused while the parent is live; once the parent is finished (a `cudl finish <parent>` commit
  on main, even with `--keep`), the child's `sync` re-points it to the integration branches. An
  abandoned parent stops `sync` until `--reparent`.
- **Who runs what:** a table in `AGENTS.md`. Agents may run `new`, `squash`, `keep`, `sync`,
  `commit`, and `open` when asked; `finish`, `push`, `upgrade`, `split`, `clean-branch` are the
  user's. `cudl open` clears inherited session ids, so the new session is its own, not a subagent.
- Deferred: finishing a stack top-down, cleaning up keep refs, carrying keep refs to a backup
  remote, fixup/autosquash. (Rebasing onto a rewritten parent came later: `sync --rebase`.)

## A helper, not a gatekeeper

After the second pilot round it was plain that before cudl the agent could just do the work, and
now there were manual steps: cudl had answered each problem with a rule or a refusal. The
rethought principles: plain git works everywhere and cudl reconciles through hooks; cudl commands
are shortcuts, not gates; sessions go where the work is; the start context is information. Gates
are kept where history or other people are at stake: integration branches, commits on a remote, operations in progress, pins before anything
destructive, clean worktrees before removal, and a main lab with no pending changes before
`finish` merges into it.

- **Instructions**: `AGENTS.md` is short: artifacts never in `code/*`; never push or rewrite an
  integration branch or pushed commits (`finish`, `push`, `upgrade` are the user's); write
  findings down; `/wrap`. Git is fine everywhere, one session may work in several features. The
  skills say the same (they used to ban `git commit` and moving between features). The start
  context lists facts only; the "restart after compaction" line is gone.
- **Lab git hooks** (`post-commit`, `post-merge`, `post-rewrite`, in the lab's common git dir,
  so every worktree has them) keep the pins of every new lab commit, including ones made with plain
  git. They call the main lab's `.cudl/cudl` (upgraded in place, unlike old worktrees' copies),
  clear inherited `GIT_DIR`/`GIT_INDEX_FILE`, read stdin only in `post-rewrite`, chain an existing
  hook (`<name>.pre-cudl`) and stay out of the way under `core.hooksPath`. Keep refs are written
  with `update-ref update`, idempotent, so concurrent sessions don't race.
- **Pins recorded at session end**: the end hook commits the session log together with the pins
  of code repos that moved, when they're on the worktree's own branch, nothing is in progress and
  no pin was staged by hand; nothing else is staged. Opt out with `CUDL_NO_PIN_RECORDING=1` or
  `"record_pins": false` in `.cudl.json`.
- **Sessions across features**: `cudl <command> -f <feature>` runs anywhere in the lab. The first
  command that changes something in another feature gives the session a linked log there
  (`role: continued`, `from:`) and adds the feature to the home log's `features:`/`last_feature:`;
  reading (`status`) links nothing. After a restart the start context lists those features and
  shows the most recent one's handoff; the end hook closes every linked log in its own worktree;
  `/wrap` covers them all. Logs are looked up by session **and** feature, so a stacked feature's
  inherited copy of its parent's log is never reused.
- **`commit`** gives a pinned repo with work in it the feature's branch on the spot, still refuses
  a repo on some other branch (`finish` recognises work by branch name), stages only `--lab` paths
  if given, and explains signing failures (`commit.gpgsign`, `gpg.format`, key, whether the SSH
  agent socket exists and holds the key). Errors carry git's full output.
- **`upgrade`** decides whether a user-owned file (`AGENTS.md`, `.claude/settings.json`) was
  edited from the hash recorded when cudl last wrote it; without one, from whether it matches any
  past template version in the tool repo's history; else "can't tell", kept. `--take agents-md`,
  `--take settings`.
- **`push`** says what it pushes (integration branches; `--feature <f>` for a feature's branches,
  `--remote` to choose where) and pushes with `--no-follow-tags`: branches only, never tags or keep
  refs. **`feedback`** writes one file per entry under the main lab's `notes/cudl-feedback/` and
  commits it there, so nothing diverges across features. **`new --from`** a parent with
  uncommitted changes starts from its last commit, and says so.
- The code review found, and these were fixed: `upgrade` dropped the old marker's record of an
  edited `AGENTS.md` (then migrated once, a migration dropped since; and a shallow tool history
  counts as unknown); `commit --lab` staged its paths but committed the whole index (now a pathspec commit,
  and a retry still records pins of earlier code commits); a session's `last_feature` stopped
  moving after the first visit, `new` didn't associate the creating session, and a session that
  ended or resumed in another worktree, or worked in the main lab, left logs open or started a
  second home (now one session graph, closed wherever it ends); crashes on a removed parent
  checkout or a repo added after a feature; feedback files could overwrite each other; the
  signing diagnosis could hide git's error; chained `post-rewrite` hooks lost the last line of
  their input and a failing chained hook skipped retention; the session-end commit ran during a
  lab merge; `finish` let a staged pin or an uncommitted session log slip in; `new --from` seeded
  an uncommitted handoff; and a blanket `-f:*` permission pre-approved `-f main push --yes`.
  Installing the hooks now also backfills keep refs, and cudl's own lab commits take a lock.
- A second review pass: the backfill that `upgrade` now runs mis-parsed git's answer for a missing
  pinned commit (two words), shifting every later answer (fixed, with a warning for lost pins); a
  subagent resumed in another worktree became a committing session (the subagent mark now carries
  over); `commit --lab` could overwrite a pin staged by hand; a session ending in a worktree it
  never linked started a second home; a session whose home feature was finished couldn't find its
  log; pin selection ran before the lock and plain `commit` didn't lock; `finish` committed a
  running session's log (its index now lists only committed logs).
- `sync --rebase` and a resumable `finish --stack` came next (next section).

## Rewritten parents and finishing stacks

A pilot's work became a five-level stack, every branch published to a fork, and its next step was
to finish the stack bottom-up: finish, sync, finish, four times over. Squashing a parent stranded
its children: `sync` stopped with a hand `rebase --onto` to run. A review of the plan before
building changed most of the details below.

- **A precise parent state.** `parent_state` used to grep main for `cudl: finish <parent>` as a
  substring: a finished `p2` made a live `p` look finished, and a reused name did the same. Now each
  feature has an id (`branch.feat/<f>.cudlid`, the child keeps `cudlparentid`), and the end of a
  parent is written on its children when it happens: `cudlparentdone = finished <commit>` or
  `abandoned <branch>`, never overwritten, so a later feature by the same name can't take them
  over; a finish is trusted while its commit is on main. `finish --keep` marks the feature itself
  (`cudlfinished`), so it's no longer offered as a parent. Labs from before: a `cudl: finish <p>`
  merge counts only if its merged side holds the commit the child started from.
- **Where a finished parent went.** A child of a finished parent follows the branch the parent
  was merged into (the finish commit's `Code:` trailers), not `.gitmodules`' default: a parent
  made with `--base alpha=release` takes its children to `release`.
- **Four rewrite states per stacked repo**, against the parent's branch while it's live, else where
  it was merged: *ok* (it still contains what the child took); *rewritten* (the child still holds
  commits the parent dropped); *rebased* (the child is on the new parent and holds none of the
  dropped commits: done by hand or by a `--rebase` that stopped, so `sync` just records it);
  *tangled* (anything else, advice for a hand rebase). The case for the "none of the dropped
  commits" condition: a child reset to before the parent's last commit, then merged with the new
  parent, would otherwise pass as rebased while keeping an obsolete commit.
- **The reparent check** (a known bug): after a parent was finished, `sync` re-pointed the child
  at the integration branch and forgot `cudlparenttip` without looking. A parent squashed and then
  finished left the child holding the pre-squash commits, merged in a second time later. The check
  above now runs against where the parent went, and `cudlparenttip` is kept until it passes.
- **`cudl sync --rebase [--force]`** moves a child's own commits (`cudlparenttip..child`) onto the
  rewritten parent, or onto where a finished one went: `git rebase --onto <tip> <last> <child>`
  with the user's rebase config overridden (`--no-update-refs --no-rebase-merges --no-autosquash
  --no-autostash`), the old tip kept first, the target resolved once and recorded
  (`cudlrebaseonto`) so a stopped rebase is recognised after `rebase --continue` even if the parent
  moved on meanwhile. Refused, touching nothing: commits on any remote-tracking branch, or a child
  whose own remote branch the result won't contain (a published child with no commits of its own
  counts), unless `--force`, which prints the `push --force-with-lease` to run and never runs it;
  and a merge in the range unless it is a merge of the parent's older commits *whose tree is
  exactly git's automatic merge of its parents* (`git merge-tree --write-tree`). Not every sync
  merge: one that resolved a conflict is dropped by a rebase without asking again (the child's
  commit replays as empty). A clean sync merge adds nothing of
  its own, so flattening it loses nothing; any other is refused with a hand `--rebase-merges`.
  Features stacked on the rebased one are named: they'll need their own `--rebase`.
- **Checks before writes.** `sync` checks the worktrees, the parent and every rebase before it
  writes anything; then it records pins moved by hand (the session end's rule, as a pathspec
  commit, so a failed commit stages nothing), rebases, updates the metadata, and merges commits
  resolved up front (a parent moving meanwhile doesn't change what's recorded as taken). The lab
  merge and commit run under the lab lock. A rerun after a stopped rebase used to hit
  "unrecorded code commits"; now it records them.
- **`cudl take`** in a child keeps the parent boundary: the lab's pin (a parent commit), not the
  current HEAD, which may hold commits made on the detached pin; and a repo whose pin holds a
  finished parent's pre-rewrite commits stays stacked, so `sync --rebase` sorts it out.
- **`cudl finish --stack <top> [--no-ff]`** (the user's) finishes `<top>` and the live features
  under it, bottom-up, syncing each level first (reparenting it once the level below is merged).
  A read-only preflight covers every level and the main lab first: clean, nothing in progress,
  repos on their branches, no rewritten or tangled pair, the main checkouts where the merges go.
  A stop (a sync conflict, a hook or signing failure) says what's finished and what's left; rerun
  the same command to go on.
- **Resumable finish**, single or stacked: the plan goes into `<lab git dir>/cudl-finish.json`
  before anything is merged, claimed with an exclusive create so two finishes can't both start: the
  feature's lab and code tips (merged by commit id, so a commit made meanwhile isn't half taken),
  the bases' tips (a moved base stops the merge), and an operation id carried by the finish commit
  as `Cudl-op:`. Each step checks what's done: a pending lab merge of exactly that tip is
  concluded; a commit that went through before the state file said so is found by its id; a code
  `--no-ff` merge stopped at its commit is concluded; staged paths beyond the merge are refused;
  if nothing has moved yet, a failure puts everything back and drops the plan. Cleanup removes a
  feature only if its branches are still at the merged tips and its worktrees are clean, and the
  lab worktree is removed without `--force` (empty directories stand in for the removed code
  worktrees), so a session log written after the check stops the removal instead of vanishing.
  `cudl ls` shows a pending finish; other finishes are refused until it's done.
- **Permissions**: `--force` (squash, `sync --rebase`) asks, whatever the order of the options
  (an ask rule `Bash(.cudl/cudl * --force*)`, ahead of the allow rules).
- On the pilot's stack (all parent tips recorded, nothing rewritten) `finish --stack <top>`
  needs neither `--rebase` nor `--force`; checked on a copy built with the
  round-1 tool (no ids, no marks, published to a fake fork), upgraded, then finished in one go.
- The code review's findings, fixed:
  - **A rebase can undo the parent's change silently**, merges or not: a child commit that
    becomes empty on the new parent is dropped, and the child's later revert of it then reverts the
    parent. So the rebase is first rehearsed in a throwaway worktree (no hooks, no signing) and its
    result compared with the three-way merge of the parent with the child's net change since
    `cudlparenttip` (`merge-tree --merge-base`); a mismatch refuses with nothing changed, and so
    does a net merge that conflicts (it can't be checked: typically the parent took over one of the
    child's commits; the message says so and how to compare a hand rebase). A rehearsal that stops
    on a conflict lets the real rebase run and stop for the user; once continued by hand, a result
    that differs gets a note. (The first fix reset the child after the fact, which a second pass
    showed could discard edits made meanwhile.)
  - `take` took the boundary from the child's own pin, which can be a commit made on the detached
    checkout and recorded with `cudl commit`: now from the lab, the newest pin in the
    parent's lab history (up to the last parent commit the child merged) that the checkout
    contains — a sync whose gitlink conflict kept the child's commit over a newer parent pin
    included. `--rebase` refuses a worktree that isn't on the feature's branch.
  - A hand rebase wasn't recognised once the parent had moved on, and a two-repo `--rebase` that
    stopped in the second lost the first one's target: the merge-base of child and parent
    is a candidate target too, and the target is kept until recorded.
  - `sync` resolved the parent twice: the tip classified is the tip rebased onto, merged and
    recorded. The printed force-push lease named the wrong commit: now the remote's.
  - Finish resume: a pending code merge is concluded only if its tree is the feature tip's; the lab
    index must equal git's merge of the recorded tip plus the new pins and the index file, and each
    pin must be the main checkout's HEAD, on its base, holding the feature tip; the main lab must be
    on the recorded branch.
  - Cleanup: a feature that changed after the plan (a session committed, a file appeared) was
    refused forever on every rerun, and its children were already marked. Now cleanup checks
    first, before marking anything; a changed feature stays live, the plan is dropped, and the
    rerun merges the rest. Branches are deleted with `-D` once checked to be at the merged tip
    (`-d` asks the upstream, and refused pushed branches).
  - The journal: completion is written in the save that clears the plan; a leftover plan-less file
    is dropped; the claim is an atomic `link` of complete contents; a separate lock is held for the
    whole finish, so concurrent reruns can't interleave.
  - A legacy child's parent generation is identified by its start commit, whatever a namesake does;
    and the first `sync` or `finish` gives id-less features ids and their children the
    parent's id while it's live for them, since a legacy parent that a finish merged but retained
    (it changed meanwhile) would otherwise look finished from main's history, and a stack retry
    would skip its newer work (the pilot's case). Reading the journal is
    read-only (`ls` could delete a plan just claimed); leftovers go under the finish lock.
    A third pass on these: adoption now also records the legacy verdict for children whose
    parent is already gone (else a namesake's finish could claim them), runs under the lab lock,
    and happens only once every check has passed; every rehearsal verdict comes before any write
    (with two repos, the first was rebased before the second's refusal); the rehearsal runs no hooks
    at all (`worktree add` ran `post-checkout`); the boundary search has no cap; `squash` measured a child of a parent finished into `release` against the default
    branch.

## Live checks inside a lab

When cudl moved into a lab of its own, the move's rehearsal ran the live checks the earlier
rounds had left open, with headless `claude -p` in a scratch lab. Most held: the hooks run
(even in an untrusted workspace, whose project allow rules are ignored until it's trusted), auto
memory and plans land in the lab, `claude -w` makes a cudl feature, and the `--force` ask rule
blocks `sync --rebase --force` although `sync` is allowed. Four didn't. The fixes:

- **The allow rules missed the taught form.** Agents were taught `.cudl/cudl -f <feature> sync`,
  and the rules are prefixes (`Bash(.cudl/cudl sync:*)`), so every feature-addressed command
  prompted. A wider rule is unsafe: `*` spans spaces, so `-f * sync:*` matches
  `-f a finish --stack sync`. Now the commands that act on a worktree (`setup`, `take`, `status`,
  `commit`, `sync`, `squash`, `keep`, `index`, `feedback`) also take `-f <feature>` after the
  command, which the rules match, and the docs and cudl's hints teach that form; the old one still
  works (it prompts). Abbreviated options are off: the ask rule matches text, and `--forc` was
  accepted as `--force`. `open` left the allow list: `open --command` runs anything in a new tmux
  window, `push` included, and opening a session is the user's call anyway; `open --print` now
  prints the launch with the session ids cleared (the whole command, run by your `$SHELL`), as the
  tmux path already did.
- **Memory, plans and session logs blocked a finish.** Auto memory (main's `memory/`) and plan mode
  (`plans/`) write files no one commits, and a session in main keeps updating its committed log, so
  a finish refused ("uncommitted changes that the finish commit would take along"). The session end
  now commits `plans/` and main's `memory/` with its log, new files included. A finish lets tracked
  changes under `memory/`, `plans/`, `journal/` in main through its checks and commits them, after
  every check, as a checkpoint of their own (it stays if the merge then stops); untracked files stay
  out, so a running session's new log isn't taken. Anything else uncommitted in main still stops a
  finish, and so does a "split" path: staged and then changed again, or removed from the index with
  the file still there (`git rm --cached`, which git lists twice). cudl never stages or commits a
  split path, nor any path a session commit wouldn't name: those commits are pathspec commits of
  literal paths (a `*` in a file name is no wildcard), they stage only new files and unstage them
  again if the commit fails. The session index is cudl's generated file: each of these commits (and
  `cudl commit --lab`) writes it to list exactly the logs the commit holds, as the commit holds them
  (HEAD's version of a log it doesn't take), and commits that version, whatever was staged of it.
  And cudl's own writes to a log never make it split: a log staged and unchanged since (a `cudl
  commit` that failed on signing leaves it so) is restaged with the new content. Logs are written
  whole, through a temporary file in the git dir. The feature side is unchanged: a feature's own
  uncommitted log stops `sync`, `squash` and `finish` (a session may be running there).
- **`claude -p` from a session wasn't its subagent**, and inside Codex started by a Claude session
  `cudl` attributed commands to the Claude session. See "Agents started by agents": the session
  start exports `CUDL_SESSION_ID`, and the nearest session is chosen by the parents the logs
  record (and the ids each log saw around it at its start, `outer:`), for the parent and for the
  session running a command alike. A parent's end commits all its descendants' logs, in every
  worktree they are in (a subagent can start in another feature, as a Codex review run with
  `-C wt/<feature>` does, and its commands there leave linked logs), and the index nests them.
- **A failed `claude -p` left an empty log.** Its hooks ran, though its transcript was never
  written. A missing transcript alone proves nothing (`claude -p --no-session-persistence` does
  real work without one), so a log is dropped only when the transcript is missing or shows no
  activity (read to the first sign of it; unreadable counts as activity), the log is untracked and
  as cudl wrote it, and nothing changed since the session started: the lab's HEAD, the code repos'
  heads, uncommitted code, the worktree (other sessions' logs aside: a parent still running), and
  main's shared memory; and it started no session whose log is in the lab. Such a session left
  nothing to point to and nothing to record. (A `--no-session-persistence` run that only talked,
  and changed nothing, leaves no log either.) An uncommitted change counts only if it is newer than
  the session's start: a file by its mtime or ctime (a mode change moves only the ctime), a deleted
  one by its directory's, a code repo's own submodule with changes always. At first any counted, and
  sessions closed before their first prompt kept open, goal-less logs beside an upgrade not yet
  committed, or beside another session's work. A removal at exit commits what is uncommitted in the
  worktree whether or not the session gets a log.

Also seen: Codex clamps a SessionEnd hook's timeout to 3 seconds; `codex exec` run in the
background needs its stdin closed (`< /dev/null`), or it waits for more input.

## Worktree exits, notices and the memory index

The checks still open then ran in scratch labs, with interactive Claude Code sessions driven
through tmux (Claude Code 2.1.288). SessionStart context reaches Codex; `CUDL_SESSION_ID`
is set from a session's start and survives `/compact`; an interactive session that did nothing leaves
no log. What didn't hold, and what changed:

- **"Remove worktree" never removed a feature.** On leaving `claude -w`, Claude Code asks Keep or
  Remove. WorktreeRemove runs before SessionEnd (documented), while the session's own log is still
  untracked there, so the abandon refused; a hook's output is discarded, and a failing hook keeps the
  worktree: every Remove was a silent Keep. Now the session that worked there (its own log, not a
  linked one, is there) gets what it chose, and loses nothing:
  - first a preflight, under the finish lock, before anything is written: no other session may still
    be running there (a log of the worktree's own without `ended:`; a session start, or a command
    that brings a session back into the worktree, clears it from the log it goes on with, so a resumed
    session counts, and a stacked feature's inherited logs don't);
    every checkout on the feature's branch or pinned; nothing in progress in any checkout, clean ones
    included (a rebase stopped at `edit`); no finish pending for it; and the untracked files to commit
    under 100 MiB together;
  - its leftovers committed onto the feature's own branches, two commits per repository so that a
    path staged and then changed again keeps both versions (a pinned checkout with work takes the
    feature's branch first, as `cudl commit` does); a failed commit leaves the index as it was;
  - its log closed there, with the pins it moved, and the feature abandoned, branches kept as
    `abandoned/<feature>-<time>`. A session that did nothing leaves no log.
  Anything that stops this keeps the worktree, with what was committed so far on its branches. A
  worktree that isn't the session's own (a subagent's, which holds its parent's linked log) is
  removed only if nothing in it needs saving; only then is that linked log closed.
- **Abandoning is resumable.** What it will do (the stamp of the new names, the repos the feature owns,
  its stacked children) is written down first (`<git dir>/cudl-abandon/<feature>.json`); the branches
  are renamed before the worktrees go (git renames a branch checked out in a worktree, and the
  worktree follows); a rerun of `cudl finish <feature> --abandon` checks what is left again (it may
  have changed since the stop) and goes on from where it stopped. An intent left by an earlier
  feature of the same name (another lab branch id) is ignored.
- **Removing a worktree kept nothing ignored.** `git worktree remove` deletes ignored files, so every
  finish and abandon lost the feature's `scratch/`. The lab worktree's ignored files (`scratch/`,
  `journal/raw/`) now go to the main lab's `scratch/<feature>-<time>/` first (a second attempt's to
  `…-<time>.2/`, beside the first's); code checkouts' ignored files, their projects' build output, go
  with them as before.
- **Notices.** What cudl did where nobody saw it (a WorktreeRemove: removed, kept and why, or where it
  stopped) goes to `<git dir>/cudl-notices.jsonl`: the next session start in the lab that isn't a
  subagent's shows it to the agent and, briefly, to the user, then removes it; `cudl ls` lists it.
- **Every `claude -w` session left a stray log in main.** After the exit (Keep or Remove) Claude Code
  moves the session back to the launch directory, so SessionEnd runs in the main lab, where the end
  linked a `continued` log. A session end now closes the session's logs where they are, found in
  every worktree without relying on its home (a log of a feature that is gone counts only in main,
  where a finish put it: a stacked child's inherited copy is no one's), and never links: linking is
  for the session start
  (resumed in another worktree) and commands that change something. A session whose log went with a
  removed feature gets no new log at its end, and as a subagent's commits nothing of its parent's:
  first through a marker in the git dir, now because no session end creates a log
  (see "Sessions: what cudl promises"). Lost: work done only by
  plain edits after a `/cd` into another worktree gets no log there at the end.
- **After "Remove worktree" the session's end didn't run at all.** It runs in the main lab, but
  `CLAUDE_PROJECT_DIR` still names the removed worktree, and the hook commands were
  `"${CLAUDE_PROJECT_DIR}"/.cudl/cudl …` (found with a logging hook in a scratch lab). The hook
  commands now walk up by name to the nearest `.cudl/cudl`, which from a removed worktree is the
  lab's (see "Codex, and agents in general" for where they start); `upgrade` brings the new
  commands to an unedited `.claude/settings.json`.
- **Transcripts move.** A worktree session's transcript stays at the launch directory's project key
  (documented), not the one its hooks were told; a resumed session's stays where it began. cudl
  looks a transcript up by its session's id when the given path doesn't exist
  (`~/.claude/projects/*/<id>.jsonl`, `~/.codex/sessions/*/*/*/rollout-*-<id>.jsonl`), for the idle
  check, a subagent's goal and the log's `transcript:`, which a path that doesn't exist never replaces.
- **Two sessions creating `memory/MEMORY.md` at once lost an index line.** Both found none, and the
  second one's Write replaced the first's (Claude Code's Write after a failed Read doesn't notice a
  file created since): the memory was committed but unlisted, so no later session saw it. The
  template now ships an empty index, so agents edit one (`split` treats it as a placeholder, like a
  `.gitkeep`, which an imported `memory/` replaces), and every session end appends a line, from its
  frontmatter, for a memory file the index doesn't link, once it is a minute old (its writer may be
  about to add the line). At the end, after the check for a session that did nothing, so cudl's own
  repair never makes a session look busy. Appends only; the repair is eventual, since agents' writes
  don't take cudl's lock.
- **Smaller ones.** `cudl commit --lab` takes a path `git rm` already removed (and refuses a name that
  is nowhere, before committing anything); `split` signs its merge commit when `commit.gpgsign` is
  on, and checks the signer before writing anything; the template no longer ships `CLAUDE.md` or
  `.claude/skills/` (`init` and `upgrade` make them), so a checkout of cudl isn't configuration for
  a session working on cudl; and a feature's copy of cudl doesn't act on its own lab (see "The `cudl`
  tool").

Known limits, left as they are (cudl defends against what happens with one user and a few
sessions at once): a session that resumes in a worktree in the second or two while that worktree's removal
runs can have it removed under it (what it wrote by then is committed onto the abandoned branch);
work done only by plain edits after a `/cd` into another worktree gets no log there.

## Agents started by agents

A Claude session often runs Codex (a review, a second opinion) through a skill: Codex is its
subagent in all but name. Claude Code exports the session's id as `CLAUDE_CODE_SESSION_ID` to
everything it runs, and that reaches the hooks Codex runs; Codex exports `CODEX_THREAD_ID` in the
same way. A Claude started from a session (`claude -p`) is the exception: Claude Code gives it its
own `CLAUDE_CODE_SESSION_ID`, so cudl's session start also exports `CUDL_SESSION_ID` to the
session's commands (through `CLAUDE_ENV_FILE`), which the child inherits. Outer sessions' ids stay
in the environment too (Codex → Claude A → Claude B leaves Codex's id there), so the parent is the
nearest of the sessions named: the one none of the others descends from, by the parents their logs
record and the ids each log saw around it at its start (`outer:`, which also covers sessions with no
log in this lab). The same rule says which session runs a command (`cudl commit` inside Codex
started by Claude is Codex's). When a session starts and the environment names another session that
has a log in this lab, cudl treats it as that session's subagent:

- its log gets `role: subagent` and `parent: <id>`, and at its end the goal is filled in from the
  first prompt of its transcript (what it was asked);
- its start context is five lines instead of the handoff and recent sessions: whose subagent it
  is, and to report back without committing, wrapping or editing `handoff/`, `journal/`, `memory/`;
- it commits nothing; the parent's session-end hook commits its descendants' logs with its own;
- the index lists children indented under their parent (`↳ codex [id] · <prompt>`), a level
  further for each generation, and the recent-sessions list a new session starts with leaves them
  out.

A parent that has no log in the lab (Codex run from a session elsewhere) makes a normal session.
Checked live: `codex exec` run from a Claude session into a scratch lab logged itself as its
subagent, with its prompt as goal. Claude's own subagents (the Agent tool) fire no session hooks
and stay inside the parent's transcript, so they need nothing. `claude -p` run from a session was
checked live too: the child's hooks saw only its own id, hence `CUDL_SESSION_ID`.
Not checked: Codex from Codex.

## Sessions: what cudl promises

A review of the whole session machinery wrote down what it promises and checked the code against
it with experiments. What holds:

- **Records.** A session that did something has one home log: where it started, in main once that
  feature is finished, or on the abandoned branch with its feature. A session that did nothing leaves
  none: the idle check runs at its end or at a removal, on its home log wherever that is. A session
  that resumes in another worktree, or changes something there through cudl, has a linked log there.
- **Closed at the end.** A session's end gives each of its logs `ended:`, wherever its branch has
  taken it, and commits it there. A log in a live worktree gets that worktree's facts (lab and repo
  ranges, `touched:`); the home also gets the transcript and the number of compactions, counted in
  the transcript (Claude Code's `compact_boundary` records, Codex's `compacted` ones). A log a finish
  brought into main gets no facts computed against main. A session end never creates a log. One that
  finds none (Codex's SessionStart comes with its first turn; a log went with a removed feature) does
  only the bookkeeping that needs none: the memory index, its descendants' logs, main's memory. A
  subagent's does nothing; its parent comes from the environment, as at a start. If the end's commit
  fails (signing, say), the log is left with `ended:`. That session isn't running, so `sync` and
  `finish` commit the log, as the end would have, before they go on. Their checks leave such logs out,
  and the commit comes first among their writes.
- **Only cudl's paths.** A session's end commits its logs, its descendants' logs, the moved pins of the
  worktree's own branch, `plans/`, and in main `memory/`; the shared directories and the pins may hold
  other sessions' work. Every commit cudl makes holds an `INDEX.md` listing exactly its logs.
- **Subagents** commit nothing: their nearest ancestor that isn't one commits their logs. A command run
  inside nested sessions belongs to the innermost one with a log in the lab (`nearest`, with `outer:`).
- **New logs** are named and written under the lab lock: Codex ids share their first characters.
- **Running sessions.** A log without `ended:` means its session may be running there, and nothing
  removes a worktree under one:
  - "Remove worktree" refuses for any other session's open log there. The removing session's own
    descendants don't count: they end with it, or were killed with it.
  - `finish`, `finish --stack` (every level, before anything is merged) and `finish --abandon`
    (without `--force`) refuse for a session that works from there now. That means a session start
    ran there (a startup, a resume, a compaction), or the session moved in with EnterWorktree, and it
    hasn't ended since: its log there has `running_since:`, which its end drops.
  - The session running the command doesn't count, and `finish --keep` removes nothing, so it checks
    nothing.
  - Two cases get a note instead: a session that only worked there with `-f`, and a subagent whose
    parent has ended, since it went with its parent.
- **Hooks never stop a session**, and what WorktreeRemove did is told once, at the next session start.

Known limits:
- An open log is a hint, not a fact. A crash, a kill or a missed end hook leaves it open, and only a
  resume of that session closes it; nothing reconciles it otherwise.
- A child that ends after its parent leaves its own end uncommitted until the next commit in that
  worktree.
- `lab_lock` has no timeout, so an end waiting for another cudl command's commit could be cut off by
  Codex's 3 s clamp.
- Plain edits after a `/cd` into another worktree get no log there.
- A feature's name reused while a session that worked in the earlier one still runs: the new feature
  inherits that session's open log of the old one. That log can hold the new feature's Remove, and the
  session's work in the new feature may land in it.
- A session that left a worktree with ExitWorktree counts as running there until it ends.
- A Claude Code subagent's worktree carries its parent's mark (WorktreeCreate comes with the parent's
  id). So a kept subagent worktree's finish waits for the parent, unless the parent runs it.
- Not checked live: whether a session's end hook can still start once a finish it ran itself removed
  its working directory.

The removal markers and the `PreCompact` hook are gone. `cudl hook` with an event it doesn't have
exits 0 and does nothing (a note on stderr). That covers `pre-compact` in settings that still name it,
where exit 2 would block the compaction, and any event retired later.

Dropped as dead code: `upgrade`'s migration of an older marker record (`agents_md`), and the hidden
`--take-agents-md`. A lab with that older record, never upgraded since, would have its unedited
`AGENTS.md` kept at its next upgrade, with a note; `--take agents-md` takes the template's.

## Parallel features

One feature is a lab worktree on its own branch. Inside it, each code repo the feature touches
is a **worktree of the shared submodule** on the feature's code branch. One object store per repo,
any number of features, one Claude session per feature, all running at once.

```
myproject-lab/                     # lab, branch main (integration)
  code/client                      # submodule, on its integration branch
  memory/                          # shared by all features
  wt/                              # gitignored
    dial-fix/                      # lab worktree, branch feat/dial-fix
      code/client                  # code worktree, branch dial-fix (shares objects)
      code/experiments             # only the repos this feature touches
      journal/sessions/…           # this feature's session logs
      handoff/dial-fix.md          # this feature's state
    hedge/                         # another feature, another session
```

Tested in a scratch copy (lab worktree `wt/x`, code worktree placed at `wt/x/code/netlab`):

- `git worktree add` on the lab leaves `code/netlab` empty and uninitialized. Removing the empty
  directory and running `git -C code/netlab worktree add <abs>/wt/x/code/netlab -b x` works: the
  feature's lab sees it as the submodule, at the pinned SHA.
- A code commit on `x` shows up in the feature's lab as ` M code/netlab`. Committing the bump with a
  note stays on `feat/x`, and the main lab is untouched.
- Merging `feat/x` into main moves main's pointer, but `git submodule update` then leaves main's
  `code/netlab` on a **detached HEAD** instead of merging branch `x` into the integration branch. That
  step belongs in the tool.

What is shared and what is per feature:

| artifact | where | why |
| --- | --- | --- |
| code | per feature: a branch in each touched repo | normal feature work; untouched repos stay at main's pin |
| session logs, plans, feature notes | per feature: the lab branch | merge back on finish; unique file names mean no conflicts |
| handoff | per feature: `handoff/<feature>.md` | each parallel session resumes its own state; one file per feature, so merging never conflicts |
| `journal.md` | per feature, merged on finish | findings land with the work that produced them |
| journal `INDEX.md` | generated | rebuilt after merges, so nothing to conflict |
| memory | shared: one directory in the main worktree | lessons reach every feature at once; one file per memory; Claude Code maintains `MEMORY.md` itself |

Memory is written by parallel sessions straight into the main worktree, so it is visible
immediately but uncommitted; `cudl commit` from any feature also commits `memory/` on main. The
alternative (memory per feature, merged on finish) is safer against concurrent edits but slower to
share, and not worth it while memories are one file each.

Each repo has one integration branch, recorded in `.gitmodules` (`submodule.<name>.branch`);
`cudl new --base <repo>=<branch>` overrides it for one feature, with another local branch.

## The `cudl` tool

A single stdlib-only Python script, with skills on top, for the chores of the two-level git.
Plain git works everywhere, for the user and the agent alike; cudl's commands are shortcuts, and
its hooks reconcile what plain git did (see "A helper, not a gatekeeper").

| command | does |
| --- | --- |
| `cudl new <feat> -r client,experiments [--base …]` | lab worktree and branch `feat/<feat>`, plus a code worktree and branch `<feat>` in each listed repo |
| `cudl open <feat>` | a tmux window in that worktree running `claude`: one command per parallel session |
| `cudl ls` | every feature: branches, dirty state at both levels, ahead/behind, last session and its outcome |
| `cudl commit -m …` | commit each dirty code repo, then the lab with the pointer bumps and artifacts |
| `cudl sync` | in a feature: merge the integration branches into the feature's code branches and main into `feat/<feat>`, move pinned repos to main's pins |
| `cudl finish <feat>` | merge the code branches (fast-forward or merge, never a detached HEAD), merge the lab branch, rebuild indexes, remove the worktrees |
| `cudl push` | push code branches only, after checking them; the lab stays local |
| `cudl take <repo>` | in a feature, start a branch in a repo that was pinned |
| `cudl index` | rebuild `journal/INDEX.md` |
| `cudl import-sessions <path>…` | index older Claude Code and Codex sessions from those directories as legacy journal entries |

`cudl commit` in detail:

- **Code commits carry no trace of the lab**, so they stay clean for upstream.
- **The lab commit** gets a `Code:` trailer, one line per repo: name, branch, SHA.
- **Messages:** the same message goes to every repo by default; `-m client="…" -m lab="…"` sets
  them separately.
- **Checks first, write second:** a code repo on another branch, unresolved conflicts or a code
  repo without a message stop it before anything is committed. A pinned repo with work in it takes
  the feature's branch first. A failing pre-commit hook stops it at that repo, after the ones
  before it, and the lab's commit comes last: a missing message for the lab stops it there.
- **Not atomic across repos** (git can't be): if the lab commit fails after the code commits,
  rerunning picks up from there, since the code commits are already in place.

**Claude integration:**

- Skills `/feature new|open|ls|finish`, `/commit` and `/wrap` call `cudl`.
- The `WorktreeCreate` hook calls `cudl new`, so Claude's own worktree command produces this layout,
  and `WorktreeRemove` commits what the session left and abandons the feature (see "Worktree exits,
  notices and the memory index").
- `SessionStart` and `SessionEnd` hooks drive the journal (above).

**A copy being worked on.** cudl acts on the lab of the working directory. A copy of cudl in a lab
feature's `code/` (developing cudl in a lab: `wt/<feature>/code/cudl/bin/cudl`) refuses to act on that
lab, whose commands and hooks are its own `.cudl/cudl`'s: `status` and `ls` warn, everything else
stops, `upgrade` and `hook` included; `install` (global links) is refused wherever it runs, `init`
inside that lab. It works on any other lab: scratch labs, the tests'. The main lab's checkout (the
installed copy) and `.cudl/cudl` are not such copies.

## Alternatives considered

- **A gitignored nested clone plus a pin file.** `code/` is a plain clone the lab ignores, and a
  hook writes `CODE_PINS` (repo → SHA) on each lab commit. Less git ceremony, but the pin is a
  convention: nothing stops a stale file, and `clone` does not restore the code.
- **An orphan artifacts branch inside the code repo.** It keeps everything in one place, but the
  artifacts share the code's remote and push paths, cannot span repos, and are one careless
  `push --all` from leaking.
- **Symlinking memory and plans into the code repo.** Versioning then depends on gitignore
  discipline in the code repo, which is the problem we started with.

A submodule is the one option where git itself records "these notes ↔ these code commits" and
restores it on clone.

## Splitting a repo: `cudl split`

`cudl split <repo> --path notes/ [--rename OLD=NEW] [--branch CODE[=LAB]] [--dry-run]` moves the
history of artifact paths from a code repo into the lab, as if the lab had existed from the start.
Each imported lab commit keeps the original author, dates and message, holds the artifacts as they
were at that commit (renamed into the lab layout if asked), and **pins `code/<repo>` to the
original code commit**, with a `Split-from: <sha>` trailer. So `git log -p -- code/<repo>` in the lab
shows, for every note change, the code it was written against.

How:

- It runs on a disposable bare repo that fetches only the selected branch tips, with a git
  environment cleaned of anything pointing at another repository. The code repo, its worktrees
  and its remotes are only read; nothing is pushed.
- Two `git filter-repo` passes. The first keeps the artifact paths and applies the renames, prunes
  the commits that don't touch them, and records each commit's origin in the trailer. The second
  adds the gitlink (from the trailer) to every surviving commit and `.gitmodules` to every root,
  with pruning off: added in one pass, the gitlink would change in every commit and nothing
  would be pruned (filter-repo runs the commit callback before its prune check). Both use
  `--preserve-commit-hashes`, so hashes quoted in messages still name code commits. A check then
  verifies that every imported commit's pin equals its `Split-from`.
- Merges survive when the artifacts differ on both sides; a merge that filtering makes degenerate
  (one side only had code) is dropped, as filter-repo intends. Several branches can be imported
  in one run, so shared history maps to the same lab commits.
- Each branch lands as `split/<repo>/<branch>`; the integration branch is merged into lab main.
  The merge's first parent is the imported history and it keeps the imported pin, so plain
  `git log -- code/<repo>` walks the imported history (git follows the first parent that matches,
  and with the lab's side first it would skip the import whenever both sides pin the same
  commit, which is the usual case). A second commit moves the pin to the checked-out code.
- Refused, before anything is written: a lab with uncommitted changes, artifact paths that would
  land on the lab's own files or machinery (`code/`, `.gitmodules`, `.cudl/`, …), and a branch
  already imported (recorded in `.cudl/splits.json`: source, tips, paths, renames, lab refs).
- The dry run uses read-only git queries: commits, files and sizes at the tip and in history, the
  largest version, and a warning when commits carrying artifacts are on no remote-tracking branch
  of the submodule, since a clone of the lab elsewhere can't check out those pins until the code
  is pushed.
- Path filtering doesn't follow renames: list a path's former names with more `--path`.
- The code repo keeps its history; stopping it from carrying the artifacts from now on is one
  `git rm -r` commit, and a clean copy of the history is `clean-branch`'s job (below).

On a copy of a large client repository: the 887 commits that touched `notes/`, imported in about
13 s, each lab commit pinned to its original code commit.

## A clean copy of a code branch: `cudl clean-branch`

`cudl clean-branch <repo> <branch> [--path notes/ …] [--as <name>] [--dry-run]` writes a new branch
(default `<branch>-clean`) into the code repo: the same history with the artifact paths removed
from every commit, commits that only changed them gone, authors and dates kept. It replaces
hand-made cuts: a "without its notes" snapshot behind a publication, a `release-clean` branch, a
branch prepared for an upstream PR.

- **Nothing existing changes.** filter-repo runs on a disposable bare repo holding only the branch
  tip; the result is fetched back as the new branch. The original branch, its SHAs, the lab's pins
  and everything published keep working. Rewriting a repo in place (the dropped `--rewrite-code`)
  is not offered: nothing can prove its commits were never published, and filter-repo's in-place
  mode removes `origin`, expires reflogs and prunes objects the submodule's worktrees share.
- **A map of original to clean commits** goes into the lab (`.cudl/clean/<repo>/<name>.map`,
  recorded in `.cudl/clean.json`). A commit that vanished maps to the image of its first parent,
  which is where filter-repo put its children; filter-repo's own map says only zeros there.
- **The paths default to the ones `split` recorded** for the repo.
- **Publishing is yours**: it prints the push command and runs nothing.

On the same copy: 11,069 of 11,876 commits kept, no commit touches `notes/`, the tip's code tree
is identical to the original's without `notes/`, in about 19 s.

## Implementation

- `bin/cudl`: one stdlib-only Python script (3.12). `cudl init` copies it into the lab as
  `.cudl/cudl`, so hooks and skills call the lab's own copy and features share its version;
  `cudl upgrade` refreshes it and the skills from this repository. The copy is written beside it and
  renamed over it, so a hook that starts meanwhile runs the old copy or the new, never half of one. It
  is written in the lab's git dir, which must be on the same filesystem as `.cudl/`, as it is in any
  lab `cudl init` made.
- `cudl/`: the source of `bin/cudl`, in ten modules, each importing only from the ones before it:
  - `core`: constants, running git, commits of exact paths, the lab lock;
  - `lab`: a lab and its repos, pins, keep refs and the git hooks that keep them;
  - `journal`: session logs on disk and the index;
  - `stack`: stacked features;
  - `sessions`: which session runs, its logs, transcripts, the session hooks;
  - `features`: `new`, `take`, `commit`, `squash`, `sync`, `open`, `push`;
  - `finish`: `finish`, `--stack` and `--abandon`, and leaving a `claude -w` worktree;
  - `setup` and `migrate`: setting a lab up, and moving existing work in;
  - `cli`: the command line.

  `tools/bundle.py` builds `bin/cudl` from them, and the result is committed. Every lab
  carries a copy, and as one file that copy is one diff to review at an upgrade, can't be
  half-updated, and can't mix versions within a run. A package with a launcher in every lab was
  prototyped and dropped: it gave module names in tracebacks, at the cost of a launcher that must find
  its package, `__pycache__` to keep out of labs, a period with two layouts, and imports that could
  span an upgrade. The bundle's banners name the module each part comes from.
- `template/`: what `cudl init` copies into a new lab: `AGENTS.md`, `.claude/settings.json` (hooks
  and permissions), `.codex/hooks.json`, the `commit`, `feature` and `wrap` skills, `.gitignore`,
  `.gitattributes`, the memory index, the empty directories; `init` adds a `CLAUDE.md` importing
  `AGENTS.md`, and `.claude/skills/` links. `cudl upgrade` replaces the tool-owned files
  (`.cudl/cudl`, skills, `.codex/hooks.json`). A user-owned one (`AGENTS.md`,
  `.claude/settings.json`) follows the template while it is unedited: it still has the hash cudl
  recorded when it last wrote it, or matches a past version of the template. An edited one is kept,
  with a note on how to compare and `--take` to replace it.
- `skills/cudl/`: the bootstrap skill (`cudl install`).
- `tests/`: end-to-end tests on real repositories in a temporary directory, one file per area, with
  the shared setup in `helpers.py` (`python3 -m unittest discover tests`). `test_source.py` checks the
  source itself:
  - `bin/cudl` is the bundle of `cudl/`;
  - the modules import only along the allowed graph;
  - every name a module uses is defined or imported there. In the bundle every name shares one
    namespace, so a missing import would show nowhere else.

Decisions made while building:

- **Features own some repos, pin the rest.** `cudl new -r client` branches the client; every other
  repo is checked out detached at the lab's pin. `cudl take <repo>` gives one a feature branch, and
  `cudl commit` does that itself for a pinned repo with work in it.
- **Merges, not rebases.** `cudl sync` merges the integration branch into a feature's code branch,
  so the SHAs the feature's lab history pins stay on its branch. Rebasing is for a rewritten parent
  (`sync --rebase`), and the keep refs hold what it leaves behind.
- **Conflicts are resolved in the feature.** `cudl finish` merges code branches fast-forward only
  (or `--no-ff`), and refuses, touching nothing, when an integration branch has moved on; `cudl sync`
  in the feature brings it up to date first. The lab merge resolves only what cudl owns (code
  pointers, the generated `journal/INDEX.md`, marked `merge=ours` and rebuilt) and aborts on
  anything else.
- **Nothing is lost on abandon.** `cudl finish --abandon` removes the worktrees but renames the
  branches to `abandoned/<feature>-<time>`, so every pinned SHA stays reachable.
- **Hooks never break a session.** Session hooks report errors on stderr and exit 0; only the
  worktree hooks exit non-zero, which makes Claude Code abort the worktree operation.
- **`cudl push` is a dry run** unless given `--yes`, and pushes only the integration branches.

## Status

Checked live, in scratch labs with real Claude Code and Codex sessions (headless, and interactive
ones driven through tmux), and in the labs cudl is used in: the session hooks of both agents (the
start context, logs closed and committed at the end, subagents), auto memory and plans landing in
the lab, `claude -w` with its Keep and Remove exits, and Codex's project hooks once the project and
the hooks are trusted. The sections above say what each check found and what changed.

Open:

- The summarizer for sessions that ended without a wrap: their logs carry `needs_summary: yes`, and
  nothing writes their prose yet. Its best input is in the transcript: the agent's own summary at
  each compaction.
- The hooks' time on a large lab. A session end took 0.33 s on 400 logs in 6 worktrees; Codex clamps
  a SessionEnd hook to 3 s.
- A session started inside `code/<repo>` by mistake loads none of the lab: no instructions, settings,
  hooks or memory, since that repo is its own git root. It was checked live for both agents.
  - The only thing that reaches such a session is user-level: the `cudl` skill that `cudl install`
    links. Its description now says to use it first in a lab's code checkout, and it tells the agent to
    say so and write nothing there. In the live check, Claude's Opus and Sonnet warned the user (after
    doing the small task they were given); Haiku and Codex didn't.
  - A `CLAUDE.local.md` in each code checkout would work for Claude every time, but it puts a file in
    `code/*`. A user-level hook would work for both agents, but it means editing their global settings.
    Neither is worth it while this hasn't happened.
- Maybe a `PreCompact` hook that blocks an automatic compaction (exit 2, or `decision: block`; the
  reason goes to the user, not the agent) and asks for a wrap first. Check first what Claude Code
  does when a full context can't compact.
- A code repo can carry a `.cudl/cudl` of its own. An agent that runs `.cudl/cudl` from inside that
  repo runs the repo's file, and the template's allow rules don't ask first: they match the command's
  text, not the directory it runs in. The session start warns when a code repo has one (at its top,
  or tracked anywhere in it), and `AGENTS.md` says to run cudl from the lab's top. One that arrives
  during a session (a pull, a branch switch) is seen at the next start. A full fix would bind the
  allow rules to the lab's own copy by its full path, which changes how agents call cudl.
- Not checked: Codex started from Codex; whether a running session sees the memories another session
  wrote since it started.
- The known limits in "Sessions: what cudl promises" and "Worktree exits, notices and the memory
  index".

Deferred: finishing a stack top-down; cleaning up keep refs, and carrying them to a backup remote;
fixup/autosquash; attributing plain-git work in other features to a session; a `--rebase-merges`
path for stacked children whose merges carry conflict resolutions (refused today, done by hand).

References: code.claude.com/docs/en/memory, …/settings-reference, …/worktrees, …/hooks, …/skills.
