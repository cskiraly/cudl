---
name: wrap
description: Wrap up a session in a cudl — fill in its session log(s), rewrite the handoff of each feature it worked in, record durable findings, and commit. Use before a session ends or when its context is getting long.
---

Wrap up so the next session can start from the handoffs alone.

1. **Which features.** The session-start context named this session's log; its `features:` line
   lists every other feature the session worked in, each with a linked log
   (`journal/sessions/…` in that feature's worktree). Wrap each of them.
2. **Session logs.** In each log, fill in `goal:`, `outcome:` (`done`, `partial`, `abandoned`),
   `next:` and `tags:`, and under the headings about 20 lines: Done (with commit ids), Learned,
   Dead ends. Leave the fields the hooks keep (`session`, `started`, `lab`, `repos`, `touched`).
3. **Handoffs.** Rewrite `handoff/<feature>.md` of each feature (`handoff/main.md` in the main lab)
   as one page: goal, state, open questions, next steps, pointers to the notes that matter.
4. **Journal and memory.** Durable findings go to `journal.md` (citing the log); anything that
   should change how future sessions work goes to memory.
5. **Commit** the lab side of each feature: `.cudl/cudl commit -f <feature> -m "<summary>"` (and
   without `-f` where the session started). This commits the lab and any code already staged; code
   changes you haven't committed yet deserve their own commit and message first.
6. Tell the user the session is wrapped.
