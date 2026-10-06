# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Csaba Kiraly
"""Setting a lab up: init, add, install, setup, upgrade, the template's user-owned files."""

import hashlib
import os
import shutil
import sys
import tempfile
from pathlib import Path

from cudl.core import (
    BOOTSTRAP_SKILL, CudlError, GIT, MARKER, TEMPLATE, TOOL_ROOT, git, head_branch, pinned_head,
    run,
)
from cudl.lab import (
    Lab, install_lab_hooks, keep_pins, keep_since, pins_in, read_marker, require_main, setup_local,
    validate_name, write_marker,
)
from cudl.journal import build_index


def install_tool(root):
    """Copy the running cudl into the lab as .cudl/cudl, by a rename: a hook that starts meanwhile runs the
    old copy or the new one, never half of one."""
    dst = root / ".cudl" / "cudl"
    dst.parent.mkdir(exist_ok=True)
    git_dir = git(root, "rev-parse", "--path-format=absolute", "--git-dir")  # no commit looks there
    fd, tmp = tempfile.mkstemp(prefix="cudl-", suffix=".tmp", dir=git_dir)  # its own, if two upgrades overlap
    os.close(fd)
    try:
        shutil.copyfile(Path(__file__).resolve(), tmp)
        os.chmod(tmp, 0o755)
        os.replace(tmp, dst)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def cmd_init(a):
    root = Path(a.dir).resolve()
    if (root / MARKER).exists():
        raise CudlError(f"{root} is already a cudl")
    if not TEMPLATE.is_dir():
        raise CudlError(f"template not found at {TEMPLATE}: run init from the cudl tool repository")
    root.mkdir(parents=True, exist_ok=True)
    if not (root / ".git").exists():
        git(root, "init", "-q", "-b", "main")
    shutil.copytree(TEMPLATE, root, symlinks=True, dirs_exist_ok=True)
    claude_files(root)
    install_tool(root)
    write_marker(root, {"version": 1, "templates": {rel: file_hash(root / rel) for rel in USER_FILES.values()}})
    w = Lab(root)
    setup_local(w)
    install_lab_hooks(w)
    build_index(root)
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "cudl: init cudl")
    print(f"cudl at {root}\n"
          f"next: cd {root} && cudl add <name> <url> [-b <branch>]\n"
          f"once per lab, so the agents load its hooks and settings:\n"
          f"  claude: run `claude` in {root} and accept the trust dialog (`claude -w` refuses until then)\n"
          f"  codex:  run `codex` in {root} and trust the project, then review and trust\n"
          f"          the two cudl hooks in /hooks (again after `cudl upgrade` changes them)")


def claude_files(top):
    """What Claude Code reads as configuration, made here rather than shipped in the template, so that a
    checkout of cudl doesn't load the template's instructions and skills into a session working on cudl
    itself: CLAUDE.md (pointing at AGENTS.md, unless the lab has its own) and a link in .claude/skills/
    for each skill in .agents/skills/, where Codex reads them."""
    if not (top / "CLAUDE.md").exists():
        (top / "CLAUDE.md").write_text("@AGENTS.md\n")
    claude_skills = top / ".claude" / "skills"
    claude_skills.mkdir(parents=True, exist_ok=True)
    for src in sorted((TEMPLATE / ".agents" / "skills").iterdir()):
        if not src.is_dir():
            continue
        dst = claude_skills / src.name
        if dst.is_symlink() or dst.is_file():
            dst.unlink()
        elif dst.is_dir():  # a copied skill from before the skills moved to .agents/
            shutil.rmtree(dst)
        dst.symlink_to(Path("..") / ".." / ".agents" / "skills" / src.name)


def file_hash(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest() if Path(p).is_file() else None


USER_FILES = {"agents-md": "AGENTS.md", "settings": ".claude/settings.json"}


def blob_id(path):
    return run(GIT + ["hash-object", str(path)], Path(path).parent).stdout.strip()


def past_templates(rel):
    """Blob ids of every version of template/<rel> in the tool repo's history (None: no history)."""
    if not (TOOL_ROOT / ".git").exists():
        return None
    if run(GIT + ["rev-parse", "--is-shallow-repository"], TOOL_ROOT, check=False).stdout.strip() == "true":
        return None  # a shallow history can't say a version never existed
    out = run(GIT + ["log", "--format=%H", "--", f"template/{rel}"], TOOL_ROOT, check=False)
    if out.returncode != 0:
        return None
    ids = set()
    for c in out.stdout.split():
        b = run(GIT + ["rev-parse", "-q", "--verify", f"{c}:template/{rel}"], TOOL_ROOT, check=False).stdout.strip()
        if b:
            ids.add(b)
    return ids


def upgrade_user_file(w, rel, take):
    """A user-owned file from the template follows the template while nobody has edited it.

    Provenance, in order: the hash recorded when cudl last wrote the file (authoritative); else
    whether it matches any past template version (the tool repo's history); else unknown, and
    it is kept. An edited file is replaced only with --take.
    """
    mine, tmpl = w.top / rel, TEMPLATE / rel
    marker = read_marker(w.top)
    recorded = (marker.get("templates") or {}).get(rel)
    current, template = file_hash(mine), file_hash(tmpl)
    flag = next(k for k, v in USER_FILES.items() if v == rel)
    if current == template:
        state = "up to date"
    elif take or current is None:
        shutil.copy2(tmpl, mine)
        state = "updated from the template" + (" (replacing your edits)" if current and take else "")
    elif recorded:
        if current != recorded:
            print(f"note: {rel} was edited here, so it was kept.\n  the difference: diff -u {rel} {tmpl}\n"
                  f"  take the template's: cudl upgrade --take {flag}")
            return
        shutil.copy2(tmpl, mine)
        state = "updated from the template (unedited)"
    else:
        known = past_templates(rel)
        if known is not None and blob_id(mine) in known:
            shutil.copy2(tmpl, mine)
            state = "updated from the template (it was an unedited older version)"
        else:
            why = "differs from every past template, so it was edited here" if known is not None else \
                "can't tell whether it was edited (no template history)"
            print(f"note: {rel} {why}; kept.\n  the difference: diff -u {rel} {tmpl}\n"
                  f"  take the template's: cudl upgrade --take {flag}")
            return
    marker.setdefault("templates", {})[rel] = template
    write_marker(w.top, marker)
    print(f"{rel}: {state}")


def cmd_upgrade(a):
    w = Lab.find(a.dir)
    require_main(w)
    if not TEMPLATE.is_dir():
        raise CudlError("run upgrade from the cudl tool repository")
    install_tool(w.top)
    install_lab_hooks(w)
    backfill_keeps(w)
    # tool-owned files are replaced; user-owned ones (AGENTS.md, settings, handoffs) only created
    shutil.copytree(TEMPLATE / ".agents" / "skills", w.top / ".agents" / "skills", dirs_exist_ok=True)
    claude_files(w.top)
    (w.top / ".codex").mkdir(exist_ok=True)
    shutil.copy2(TEMPLATE / ".codex" / "hooks.json", w.top / ".codex" / "hooks.json")
    for src in TEMPLATE.rglob("*"):
        dst = w.top / src.relative_to(TEMPLATE)
        if src.is_dir() and not src.is_symlink():
            dst.mkdir(exist_ok=True)
        elif not dst.exists() and not dst.is_symlink():
            shutil.copy2(src, dst, follow_symlinks=False)
    take = set(a.take or [])
    for flag, rel in USER_FILES.items():
        upgrade_user_file(w, rel, flag in take)
    print("updated .cudl/cudl, the skills and .codex/hooks.json; review with git diff, then cudl commit\n"
          "features made earlier get the upgrade with `cudl sync` in each feature\n"
          "codex: re-trust changed hooks in /hooks")


def link(src, dst):
    dst = Path(dst).expanduser()
    if dst.is_symlink() and dst.resolve() == src.resolve():
        return f"{dst}: already linked"
    if dst.exists() or dst.is_symlink():
        raise CudlError(f"{dst} exists and is not a link to {src}; move it away first")
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.symlink_to(src)
    return f"{dst} -> {src}"


def cmd_install(a):
    """Put cudl on PATH and the cudl skill where Claude Code and Codex find user skills."""
    if not BOOTSTRAP_SKILL.is_dir():
        raise CudlError("run install from the cudl tool repository")
    home = Path.home()
    targets = [(Path(__file__).resolve(), home / ".local" / "bin" / "cudl")]
    targets += [(BOOTSTRAP_SKILL, home / d / "skills" / "cudl") for d in (".claude", ".agents")]
    for src, dst in targets:
        print(link(src, dst))


def backfill_keeps(w):
    main = Lab(w.main)
    _, new, missing = keep_pins(main, pins_in(main.top, ["--all"]))
    if new:
        print(f"kept {new} pinned commits that had no keep ref yet")
    if missing:
        print(f"warning: {len(missing)} commits the lab's history pins are no longer in their code repos "
              f"(cudl keep --check lists them)", file=sys.stderr)


def cmd_setup(a):
    w = Lab.find()
    setup_local(w)
    install_lab_hooks(w)
    backfill_keeps(w)
    print(f"wrote {w.top / '.claude' / 'settings.local.json'}")


def cmd_add(a):
    w = Lab.find()
    require_main(w)
    validate_name(a.name)
    if a.name in w.repos():
        raise CudlError(f"{a.name} is already in the lab")
    path = f"code/{a.name}"
    args = ["submodule", "add", "-q", "--name", a.name]
    if a.from_ and not a.branch:
        raise CudlError("--from needs -b <new integration branch>")
    clone_branch = a.from_ or a.branch
    if clone_branch:
        args += ["-b", clone_branch]
    git(w.top, *args, "--", a.url, path)  # a URL that starts with `-` is still the URL
    d = w.top / path
    if a.from_:
        # an integration branch of your own, so feature merges don't pile onto the upstream branch
        git(d, "switch", "-q", "-c", a.branch, "--no-track")
    branch = a.branch or head_branch(d)
    if not branch:
        raise CudlError(f"{path} is on a detached HEAD: pass -b <integration branch>")
    git(w.top, "config", "-f", ".gitmodules", f"submodule.{a.name}.branch", branch)
    if head_branch(d) != branch:
        git(d, "switch", "-q", branch)
    old = pinned_head(w.top)
    git(w.top, "add", ".gitmodules", path)
    git(w.top, "commit", "-q", "-m", f"cudl: add {a.name} ({branch})", "--", ".gitmodules", path)
    keep_since(w, old)
    print(f"added {path}, integration branch {branch}")
