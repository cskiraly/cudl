---
name: commit
description: Commit work in a cudl — code repos and lab artifacts together, with the lab's code pins updated. Use when committing in a directory that has .cudl/cudl.
argument-hint: "[message]"
---

`.cudl/cudl commit` commits every code repo with changes, then the lab with their new pins. Plain
git works too (the lab records pins at session end); this is the one-step way.

1. `.cudl/cudl status` (or `.cudl/cudl status -f <feature>`) shows what changed.
2. Check that no notes, plans or scratch files are inside `code/*`; move them to the lab.
3. Messages: `-m "<message>"` for everything, or `-m <repo>="…"` and `-m lab="…"` separately.
   Code messages describe the code only.
4. Staging in code repos: `-a` modified tracked files, `-A` everything, neither = what's already
   staged. The lab side stages all its changes, or only `--lab <path>` (repeatable) if given.

   `.cudl/cudl commit -a -m "…"`

5. A read-only (pinned) repo with changes gets the feature's branch on the spot. A commit that
   fails says why (signing problems are diagnosed); fix and rerun: repos already committed aren't
   committed again.

$ARGUMENTS
