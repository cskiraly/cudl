# cudl

The source is the package `cudl/`: ten modules, each importing only from the ones before it
(`core`, `lab`, `journal`, `stack`, `sessions`, `features`, `finish`, `setup`, `migrate`, `cli`; the
allowed imports are in `tests/test_source.py`). What runs is `bin/cudl`, one stdlib Python script built
from the modules and committed: every lab keeps a copy of it as `.cudl/cudl`. Edit a module, run
`python3 tools/bundle.py`, then test. Never edit `bin/cudl` by hand: a test fails until it is the
bundle of `cudl/` again. In `bin/cudl` every name shares one namespace, so:
- a module imports another only as `from cudl.<module> import <name>`, with no `as`, no relative
  import and no module object;
- no name is defined in two modules, or imported in one and defined in another;
- a module has no `__future__` import and no `if __name__` block.

`tools/bundle.py` refuses anything else. An import a module is missing would go unnoticed in the
bundle, so `tests/test_source.py` checks for it, with the import graph.

The lab template is `template/`. The end-to-end tests are in `tests/`, one file per area, with the
shared setup in `tests/helpers.py`: `python3 -m unittest discover tests` (about 6 minutes), one area
with `python3 -m unittest tests.test_stacks`, one test with `discover tests -k <name>`. `README.md`
introduces cudl for people: what it is and how you work with it, so it changes only when that does.
What cudl does is listed in `FEATURES.md` (new behaviour goes there), the design is in `DESIGN.md`.

- `template/` is copied into other labs. Its `AGENTS.md`, `.agents/` and `.claude/` are not
  instructions for working on this repository (it ships no `CLAUDE.md` or `.claude/skills/`, so
  Claude Code doesn't load them as such; `init` and `upgrade` make those).
- If this checkout sits inside a cudl lab (`code/cudl`, `wt/<feature>/code/cudl`), never run this
  `bin/cudl` against that lab: cudl acts on the lab of the working directory (a feature's copy
  refuses to). Use the lab's `.cudl/cudl` for the lab, and try changes in a scratch lab.
