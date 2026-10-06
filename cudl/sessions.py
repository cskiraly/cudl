# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Csaba Kiraly
"""Sessions: which one runs, its logs, transcripts, the session hooks, notices, the memory index."""

import datetime
import json
import os
import re
import shlex
import sys
import time
from pathlib import Path

from cudl.core import (
    CudlError, HANDOFF_LIMIT, MARKER, MEMORY_INDEX_LIMIT, RECENT_SESSIONS, busy, common_dir,
    conflicted, git, git_ok, head_branch, head_sha, is_checkout, lab_changes, lab_lock, one_line,
    pinned, pinned_head, short, status_entries,
)
from cudl.lab import Lab, keep_since, lab_tops, read_marker, repo_line
from cudl.journal import (
    build_index, commit_memory, commit_with_index, create_log, drop_frontmatter, parse_frontmatter,
    read_frontmatter, readable_frontmatter, select_paths, session_file, sessions_dir,
    update_frontmatter,
)
from cudl.stack import lab_parent, parent_state


def current_session(lab=None):
    """The session running this command: the innermost one in the environment (inside Codex started by
    a Claude session, Codex's, though the Claude's id is there too)."""
    ids = env_sessions()
    return nearest(lab, ids) if lab is not None else (ids[0] if ids else None)


def session_home(lab, sid):
    """(top, log) where session sid started: its own log, not a linked one. If the feature it
    started in is gone, finished, its log is in main, where that history went. (An abandoned
    feature's log went with its branch; a stacked child's copy of it isn't the session's home.)"""
    for top in lab_tops(lab):
        f = session_file(top, sid, Lab(top).label)
        if f and readable_frontmatter(f).get("role") != "continued":
            return top, f
    f = next((p for p in sorted(sessions_dir(lab.main).glob(f"*-{sid[:8]}*.md"))
              if readable_frontmatter(p).get("session") == sid and readable_frontmatter(p).get("role") != "continued"
              and not (lab.main / "wt" / readable_frontmatter(p).get("feature", "")).is_dir()), None)
    return (lab.main, f) if f else (None, None)


def link_session(w, sid=None, quiet=False):
    """Associate the session with this worktree. A session that started elsewhere in the lab gets a
    linked log here (once), this feature goes into its home log's `features:`, and every visit
    moves the home log's `last_feature:` (so the next start shows the right handoff). Only
    commands that change something call this; looking around doesn't."""
    sid = sid or current_session(w)
    if not sid:
        return None
    home_top, home = session_home(w, sid)
    if not home:
        return None
    hfm, _ = read_frontmatter(home)
    if home_top == w.top and hfm.get("feature", w.label) == w.label:  # (a home a finish brought here isn't)
        drop_frontmatter(home, "ended")  # working here again: a removal must see it (open_sessions)
        if hfm.get("last_feature") and hfm.get("last_feature") != w.label:
            update_frontmatter(home, last_feature=w.label)
        return home
    here = session_file(w.top, sid, w.label)
    if here:
        drop_frontmatter(here, "ended")
    else:
        now = datetime.datetime.now().astimezone()
        here = create_log(w, f"{now:%Y-%m-%d}-{w.label}", sid, {
            "session": sid, "date": f"{now:%Y-%m-%d}", "feature": w.label, "agent": hfm.get("agent", "claude"),
            "role": "continued", "from": f"{Lab(home_top).label}: {home.relative_to(home_top)}",
            **({"parent": hfm["parent"]} if hfm.get("parent") else {}),
            "started": now.isoformat(timespec="seconds"), "goal": "", "outcome": "open", "next": "", "tags": "",
            "lab": short(pinned_head(w.top)), "repos": ", ".join(f"{k}={v}" for k, v in repo_heads(w).items()),
        }, f"Continued from {Lab(home_top).label} (session {sid[:8]}).\n\n## Done\n\n## Learned\n\n## Dead ends\n")
        if not quiet:
            print(f"cudl: session {sid[:8]} (from {Lab(home_top).label}) now also works in {w.label}; "
                  f"log {here.relative_to(w.top)}")
    features = [f for f in (hfm.get("features") or "").split(", ") if f]
    update_frontmatter(home, features=", ".join(features if w.label in features else features + [w.label]),
                       last_feature=w.label)
    return here


def logs_of(lab, sid):
    """[(worktree, log)] of session sid: each live worktree's own log of it (`feature:` that worktree, so
    a stacked feature's inherited copies don't count), and in main every log of it a finish brought there
    (`feature:` a worktree that is gone), home or linked."""
    out = []
    for top in lab_tops(lab):
        f = session_file(top, sid, Lab(top).label)
        if f:
            out.append((Lab(top), f))
    live = {"main", *lab.features()}
    for p in sorted(sessions_dir(lab.main).glob(f"*-{sid[:8]}*.md")):
        fm = readable_frontmatter(p)
        if fm.get("session") == sid and fm.get("feature") not in live:
            out.append((Lab(lab.main), p))
    return out


def open_sessions(w, but=()):
    """The sessions that may still be running in this worktree: its own logs (`feature:` this worktree,
    not a parent feature's inherited ones) without `ended:`, other than `but`'s. A session start clears
    `ended:` from the log it goes on with, so a resumed session counts; a crashed one counts until its
    log is dealt with. `but`: the session ids that don't count."""
    out = []
    for p in sorted(sessions_dir(w.top).glob("*.md")):
        fm = readable_frontmatter(p)
        if fm.get("feature") == w.label and fm.get("session") and fm["session"] not in but and not fm.get("ended"):
            out.append(fm["session"])
    return out


def parse_repo_map(s):
    out = {}
    for part in (s or "").split(","):
        if "=" in part:
            k, v = part.strip().split("=", 1)
            out[k] = v
    return out


def repo_heads(w):
    heads = {}
    for r in w.repos().values():
        d = w.top / r.path
        if is_checkout(d):
            heads[r.name] = short(head_sha(d))
    return heads


# Agents started from another agent's session (Codex run by a Claude skill, `claude -p` from a
# session) inherit that session's id in the environment. Codex exports its own as CODEX_THREAD_ID to
# everything it runs; Claude Code exports its own as CLAUDE_CODE_SESSION_ID, but gives a Claude it
# starts a new one, so cudl's session start also exports CUDL_SESSION_ID to the session's commands
# (through CLAUDE_ENV_FILE), which a Claude started from them inherits. Outer sessions' ids stay in
# the environment too (Codex → Claude A → Claude B: CODEX_THREAD_ID is the outermost), so no fixed
# order of these says which session is the nearest: `nearest` asks the logs.
PARENT_ENV = ("CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID", "CUDL_SESSION_ID")


def env_sessions(skip=None):
    """The session ids in the environment, each once, in PARENT_ENV order."""
    out = []
    for var in PARENT_ENV:
        v = os.environ.get(var)
        if v and v != skip and v not in out:
            out.append(v)
    return out


def around(lab, sid):
    """The sessions session sid's own log records around it at its start: its parent, and every id
    the environment named (`outer`), which includes sessions without a log in this lab."""
    home = session_home(lab, sid)[1]
    if not home:
        return []
    fm = read_frontmatter(home)[0]
    return [s for s in [fm.get("parent")] + (fm.get("outer") or "").split(", ") if s]


def nearest(lab, candidates):
    """The innermost of these sessions: the one none of the others descends from, by the parents their
    logs record (cycle-safe). Unrelated ones (no logs linking them) go by PARENT_ENV order."""
    if len(candidates) < 2:
        return candidates[0] if candidates else None
    ancestors = {}
    for c in candidates:
        seen, todo = {c}, [c]
        while todo:
            for s in around(lab, todo.pop()):
                if s not in seen:
                    seen.add(s)
                    todo.append(s)
        ancestors[c] = seen - {c}
    inner = [c for c in candidates if not any(c in ancestors[o] for o in candidates if o != c)]
    return (inner or candidates)[0]


def parent_session(w, sid):
    """The id of the session this one was started from, if that session has a log in this lab."""
    tops = lab_tops(w)
    return nearest(w, [s for s in env_sessions(skip=sid) if any(session_file(t, s) for t in tops)])


def transcript_prompt(path, agent):
    """The first real prompt of a transcript: what a subagent was asked to do ("" if there's none, or
    the transcript can't be read)."""
    try:
        if not path or not Path(path).is_file():
            return ""
        head, _ = read_head_tail(path)
    except OSError:
        return ""
    if agent == "codex":
        return next((x for x in map(codex_prompt, head) if x), "")
    return next((x for x in map(claude_prompt, head) if x), "")


SESSION_ID_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._-]*\Z")  # \Z: `$` lets a final newline through


def find_transcript(sid, agent, *candidates):
    """Where a session's transcript is now: the first candidate that exists, else the agent's own
    store searched by the session's id (Claude Code keeps a worktree session's transcript at the launch
    directory, and a resumed one where it began, whatever path its hooks are given); None if it is
    nowhere (a run that saved none, or never got going)."""
    def mtime(p):
        try:
            return p.stat().st_mtime if p.is_file() else None
        except (OSError, ValueError):
            return None

    for c in candidates:
        if c and mtime(Path(c)) is not None:
            return str(c)
    if not sid or not SESSION_ID_RE.match(sid):
        return None
    store, pattern = (Path.home() / ".codex" / "sessions", f"*/*/*/rollout-*-{sid}.jsonl") if agent == "codex" \
        else (Path.home() / ".claude" / "projects", f"*/{sid}.jsonl")
    try:
        found = [(t, p) for p in store.glob(pattern) if (t := mtime(p)) is not None]
    except OSError:
        return None
    return str(max(found)[1]) if found else None


def codex_prompt(rec):
    """A Codex rollout record's user prompt, if it is one (not the instructions Codex injects as user
    messages: AGENTS.md, the environment context)."""
    pl = rec.get("payload") or {}
    if rec.get("type") != "response_item" or pl.get("role") != "user":
        return None
    for c in pl.get("content") or []:
        t = c.get("text", "") if isinstance(c, dict) else ""
        if t.strip() and not t.lstrip().startswith("<") and "AGENTS.md" not in t[:300]:
            return t
    return None


def new_session_file(w, sid, transcript, agent="claude", parent=None, outer=()):
    now = datetime.datetime.now().astimezone()
    fm = {
        "session": sid,
        "date": f"{now:%Y-%m-%d}",
        "feature": w.label,
        "agent": agent,
        "role": "subagent" if parent else "",
        "parent": parent or "",
        "outer": ", ".join(outer),  # every session id in the environment: who started whom, for `nearest`
        "started": now.isoformat(timespec="seconds"),
        "running_since": now.isoformat(timespec="seconds"),  # until its end: where it works now (a finish waits)
        "goal": "",
        "outcome": "subagent" if parent else "open",
        "next": "",
        "tags": "",
        "transcript": transcript or "",
        "lab": short(pinned_head(w.top)),
        "repos": ", ".join(f"{k}={v}" for k, v in repo_heads(w).items()),
    }
    return create_log(w, f"{now:%Y-%m-%d}-{w.label}", sid,
                      {k: v for k, v in fm.items() if v != "" or k in ("goal", "next", "tags")}, fresh_body(parent))


def fresh_body(parent):
    """A new log's body, before anyone writes in it."""
    return f"Started by session {parent}; its log is the record.\n" if parent else "## Done\n\n## Learned\n\n## Dead ends\n"


def read_head_tail(p, head_lines=400, tail_bytes=65536):
    """The first records and the last few of a JSONL transcript, without reading all of it."""
    head = []
    with open(p, "rb") as f:
        for i, line in enumerate(f):
            if i >= head_lines:
                break
            head.append(line)
        size = f.seek(0, os.SEEK_END)
        f.seek(max(0, size - tail_bytes))
        tail = f.read().splitlines()[1:] if size > tail_bytes else []

    def parse(lines):
        out = []
        for line in lines:
            try:
                out.append(json.loads(line))
            except (json.JSONDecodeError, UnicodeDecodeError):
                pass
        return out
    return parse(head), parse(tail)


def claude_prompt(rec):
    if rec.get("type") != "user" or rec.get("isMeta"):
        return None
    content = rec.get("message", {}).get("content")
    if isinstance(content, list):
        content = " ".join(c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text")
    if not isinstance(content, str) or not content.strip() or content.lstrip().startswith("<"):
        return None
    if content.startswith("This session is being continued from a previous conversation"):
        return "(continued from an earlier session)"
    return content


# What cudl did where nobody saw it. A WorktreeRemove hook's output is discarded, so what it did, or
# why it didn't, becomes a notice: the next session start in the lab shows it, `cudl ls` lists it.
NOTICES = "cudl-notices.jsonl"


def add_notice(lab, text):
    with lab_lock(lab):
        with open(common_dir(lab) / NOTICES, "a") as f:
            f.write(json.dumps({"time": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
                                "text": text}) + "\n")


def pending_notices(lab, take=False):
    f = common_dir(lab) / NOTICES
    with lab_lock(lab):
        try:
            lines = f.read_text().splitlines()
        except OSError:
            return []
        if take:
            f.unlink(missing_ok=True)
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


MEMORY_GRACE = 60  # seconds: a memory file younger than this may be about to get its line from its writer


def index_memory(lab):
    """memory/MEMORY.md lists every memory. The template ships it (empty), so agents edit an index rather
    than create one: two sessions creating it at once lose one's line. And at a session's end a memory
    file it doesn't link gets a line, from its frontmatter, once the file is a minute old. Appends only,
    never rewrites a line. Eventual: an agent's write can still race a repair; the next end catches it.
    Links are left alone: a line read through one could copy a file from outside the lab into it."""
    d = lab.main / "memory"
    index = d / "MEMORY.md"
    if not d.is_dir() or d.is_symlink() or index.is_symlink():
        return
    with lab_lock(lab):
        try:
            text = index.read_text() if index.exists() else ""
        except (OSError, UnicodeDecodeError):
            return
        lines, now = [], time.time()
        for p in sorted(d.glob("*.md")):
            if p.name == "MEMORY.md" or p.is_symlink() or f"({p.name})" in text or f"/{p.name})" in text:
                continue
            try:
                if now - p.stat().st_mtime < MEMORY_GRACE:
                    continue
                fm, body = parse_frontmatter(p.read_text())
            except (OSError, UnicodeDecodeError):
                continue
            hook = fm.get("description") or next((l.strip() for l in body.splitlines() if l.strip()), "")
            lines.append(f"- [{fm.get('name') or p.stem}]({p.name})" + (f" — {one_line(hook, 150)}" if hook else ""))
        if lines:
            with open(index, "a") as f:  # one append: a concurrent agent's Edit still finds its text
                f.write(("\n" if lines and text and not text.endswith("\n") else "") + "".join(l + "\n" for l in lines))


_hook_data = None


def hook_input():
    """The hook's JSON input (read once)."""
    global _hook_data
    if _hook_data is None:
        _hook_data = read_hook_input()
    return _hook_data


def read_hook_input():
    if sys.stdin.isatty():
        return {}
    raw = sys.stdin.read()
    try:
        data = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def hook_target(data, event=None):
    """The directory a hook acts on, as its handler takes it (the guard against a feature's copy of cudl
    looks at the same one)."""
    if event == "worktree-remove":
        return data.get("worktree_path") or data.get("path")
    return data.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()


def hook_lab(data):
    return Lab.find(hook_target(data))


def subagent_context(w, sf, parent):
    return "\n".join([
        f"cudl: this session was started by session {parent} in the cudl {w.top}"
        + (f" (feature `{w.feature}`)" if w.feature else "") + ", as its subagent.",
        "Do the task you were given and report back. Don't commit, don't run /wrap, and don't edit",
        "handoff/, journal/ or memory/ unless the task says so: the session that started you owns them.",
        "Code is in code/<repo>; notes and plans in the lab; AGENTS.md has the rules.",
        f"This run is logged in {sf.relative_to(w.top)} under its parent.",
    ])


def handoff_text(top, label):
    handoff = top / "handoff" / f"{label}.md"
    if not handoff.exists():
        return None
    text = handoff.read_text()
    return text if len(text) <= HANDOFF_LIMIT else text[:HANDOFF_LIMIT] + "\n[… truncated; read the file for the rest]"


def session_context(w, sf, source, agent="claude", home=None):
    """Facts, not instructions (those are in AGENTS.md): where this is, what's in flight."""
    parent = lab_parent(w, w.feature) if w.feature else None
    pstate = parent_state(w, w.feature) if parent else None
    where = (f"feature `{w.feature}` ({w.top}, lab branch feat/{w.feature}"
             + (f", stacked on {parent}" + (f", which is {pstate}" if pstate != "live" else "") if parent else "")
             + ")") if w.feature else f"the main lab ({w.top})"
    lines = [f"cudl: this session is in {where}. Session log: {sf.relative_to(w.top)}."]
    for r in w.repos().values():
        lines.append("  " + repo_line(w.top, r))
    lines.append(f"Shared memory: {w.main / 'memory'}. Plans: {w.top / 'plans'}.")
    fm, _ = read_frontmatter(home or sf)
    others = [f for f in (fm.get("features") or "").split(", ") if f and f != w.label]
    last = fm.get("last_feature")
    if others:
        lines.append(f"This session has also worked in: {', '.join(others)} (most recent: {last}). "
                     f"Address them with `.cudl/cudl <command> -f <feature>` and paths under {w.main / 'wt'}/<feature>/.")
    if agent != "claude":
        index = w.main / "memory" / "MEMORY.md"
        if index.exists():
            text = index.read_text()
            if len(text) > MEMORY_INDEX_LIMIT:
                text = text[:MEMORY_INDEX_LIMIT] + "\n[… truncated; read the file for the rest]"
            lines += ["", "## Memory index (memory/MEMORY.md; see AGENTS.md › Memory)", text.strip()]
    if source == "compact":
        lines.append(f"(The context was just compacted; the session log is {sf.relative_to(w.top)}.)")
    # the handoff of where the work is now: the most recent feature, for a session that moved
    if last and last != w.label and w.feature_top(last).is_dir():
        text = handoff_text(w.feature_top(last), last)
        if text:
            lines += ["", f"## Handoff of {last}, where this session last worked (wt/{last}/handoff/{last}.md)",
                      text.strip()]
        lines.append(f"(This worktree's own handoff: handoff/{w.label}.md.)")
    else:
        text = handoff_text(w.top, w.label)
        if text:
            lines += ["", f"## Handoff (handoff/{w.label}.md)", text.strip()]
    index = w.top / "journal" / "INDEX.md"
    if index.exists():
        entries = [l for l in index.read_text().splitlines() if l.startswith("- ")]
        if entries:
            lines += ["", "## Recent sessions (journal/INDEX.md)"] + entries[-RECENT_SESSIONS:]
    return "\n".join(lines)


def hook_session_start(a):
    data = hook_input()
    w = hook_lab(data)
    sid = data.get("session_id") or "unknown"
    if not isinstance(sid, str) or not SESSION_ID_RE.match(sid):  # it names the log's file
        raise CudlError(f"no log for a session id that can't name a file: {sid!r}")
    env_file = os.environ.get("CLAUDE_ENV_FILE")
    if a.agent == "claude" and env_file and data.get("session_id"):
        try:  # the session's commands get it, so does a Claude started from them (see PARENT_ENV)
            with open(env_file, "a") as f:
                f.write(f"export CUDL_SESSION_ID={shlex.quote(sid)}\n")
        except OSError as e:
            print(f"cudl: can't write {env_file}: {e}", file=sys.stderr)
    sf = session_file(w.top, sid, w.label)
    if not sf and session_home(w, sid)[1]:
        sf = link_session(w, sid, quiet=True)  # resumed in another worktree: same session, linked here
    if not sf:
        sf = new_session_file(w, sid, find_transcript(sid, a.agent, data.get("transcript_path"))
                              or data.get("transcript_path"), a.agent, parent_session(w, sid), env_sessions(skip=sid))
    else:
        with lab_lock(w):  # running again: its end sets `ended:` anew (open_sessions counts it till then)
            drop_frontmatter(sf, "ended")
            fm = read_frontmatter(sf)[0]
            found = find_transcript(sid, a.agent, fm.get("transcript"), data.get("transcript_path"))
            if fm.get("transcript") and found and found != fm["transcript"]:
                update_frontmatter(sf, transcript=found)  # a resumed session's transcript stayed where it began
            # a session start ran here (a resume, a compaction): it works from here until it ends
            update_frontmatter(sf, running_since=datetime.datetime.now().astimezone().isoformat(timespec="seconds"))
    fm0 = read_frontmatter(sf)[0]
    parent = fm0.get("parent") or (read_frontmatter(session_home(w, sid)[1])[0].get("parent")
                                   if fm0.get("role") == "continued" and session_home(w, sid)[1] else None)
    home = session_home(w, sid)[1] if fm0.get("role") == "continued" else None  # speaks for the whole session
    ctx = subagent_context(w, sf, parent) if parent else session_context(w, sf, data.get("source"), a.agent, home)
    # the context goes to the agent; one line of it is shown to the user
    where = f"feature {w.feature}" if w.feature else "main lab"
    shown = (f"cudl: subagent of {parent[:8]} in the {where}" if parent else
             f"cudl: {where} · log {sf.relative_to(w.top)} · handoff "
             + ("loaded" if (w.top / "handoff" / f"{w.label}.md").exists() else "missing"))
    planted = [r.path for r in w.repos().values() if is_checkout(w.top / r.path) and (  # at its top, or tracked
        any((w.top / r.path / n).exists() for n in (".cudl", MARKER))
        or git(w.top / r.path, "ls-files", "--", ".cudl/cudl", "*/.cudl/cudl", MARKER, f"*/{MARKER}", check=False))]
    if planted:  # a repository can carry any file: there `.cudl/cudl` would be the repository's own
        warn = (f"{', '.join(planted)} has its own .cudl or {MARKER}: run cudl only as this lab's "
                f".cudl/cudl, from the lab's top directory, never from inside {planted[0]}")
        ctx += f"\n\n## Warning\n{warn}"
        shown += f"\ncudl: warning: {warn}"
    told = [] if parent else pending_notices(w, take=True)  # what cudl did where nobody saw it
    if told:
        ctx += "\n\n## cudl notices (what cudl did since the last session start)\n" + \
            "\n".join(f"- {n.get('time', '?')}: {n.get('text', '')}" for n in told)
        shown += "".join(f"\ncudl: {n.get('text', '')}" for n in told[:3]) + \
            (f"\ncudl: {len(told) - 3} more notices in the session's context" if len(told) > 3 else "")
    print(json.dumps({"systemMessage": shown,
                      "hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": ctx}}))


def transcript_compactions(path):
    """How many times a session was compacted, by its transcript: Claude Code's compact_boundary records,
    Codex's compacted ones (None if there's no transcript to read)."""
    try:
        n = 0
        with open(path, "rb") as f:
            for line in f:
                if b"compact" not in line:
                    continue
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if isinstance(r, dict) and (r.get("type") == "compacted"
                                            or (r.get("type") == "system" and r.get("subtype") == "compact_boundary")):
                    n += 1
        return n
    except (OSError, TypeError):
        return None


# What shows a session did something: a Claude transcript's messages; a Codex rollout's turn (its
# context, the start of a task), a reply or a tool call. A user prompt alone counts too.
CODEX_ACTIVITY = ("turn_context", "task_started", "user_message", "agent_message")


def had_exchange(transcript, agent):
    """Whether a transcript shows the session doing anything at all. Reads until the first sign of it
    (a real session shows one within a few records); a transcript that can't be read counts as one,
    none at all (no path, or no file) as none."""
    try:
        if not transcript or not Path(transcript).is_file():
            return False
        with open(transcript, "rb") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                if agent == "codex":
                    pl = r.get("payload") or {}
                    kind = str(pl.get("type") or "")
                    if r.get("type") in CODEX_ACTIVITY or kind in CODEX_ACTIVITY or pl.get("role") == "assistant" \
                            or kind.endswith("call") or kind == "reasoning" or codex_prompt(r):
                        return True
                elif r.get("type") in ("user", "assistant"):
                    return True
    except OSError:
        return True
    return False


def changed_since(top, entries, since):
    """Whether any of these changes (status_entries of top) may be newer than `since`, a time: a file by
    its mtime or ctime (a mode change, or contents copied with their old mtime, move only the ctime), a
    deleted one by its nearest remaining directory's. A directory (a submodule's changes) always
    counts, as does every change when `since` is unknown."""
    for _, _, path in entries:
        p = Path(top) / path
        if since is None or (p.is_dir() and not p.is_symlink()):
            return True
        while not os.path.lexists(p) and p != Path(top):
            p = p.parent
        try:
            st = p.lstat()
        except OSError:
            return True
        if max(st.st_mtime, st.st_ctime) >= since:
            return True
    return False


def idle_log(w, sf):
    """A log that records nothing: untracked, as cudl wrote it at the start, and nothing changed since
    then: not the lab's HEAD nor a code repo's, and no uncommitted change newer than the start, in the
    code, in the lab but its session logs, or (for a session in a feature) in main's shared memory.
    Changes from before the start (an upgrade not yet committed, another session's work) aren't its."""
    rel = str(sf.relative_to(w.top))
    if git_ok(w.top, "ls-files", "--error-unmatch", "--", rel):
        return False  # in the lab's history: not cudl's to drop
    fm, body = read_frontmatter(sf)
    if fm.get("role") == "continued" or body != fresh_body(fm.get("parent") or None):
        return False
    if any(fm.get(k) for k in ("goal", "next", "tags", "ended", "features", "compactions")) \
            or fm.get("outcome") not in ("open", "subagent"):
        return False
    if fm.get("lab", "") != short(pinned_head(w.top)) or parse_repo_map(fm.get("repos")) != repo_heads(w):
        return False
    try:
        since = datetime.datetime.fromisoformat(fm.get("started") or "").timestamp()
    except ValueError:
        since = None
    repos = w.repos().values()
    if any(is_checkout(w.top / r.path)
           and changed_since(w.top / r.path, status_entries(w.top / r.path, submodules="none"), since)
           for r in repos):
        return False
    if w.feature and changed_since(w.main, status_entries(w.main, "memory"), since):
        return False
    # other sessions' logs (a parent's, still running) and the index are bookkeeping, not this one's work;
    # a moved code repo was judged by its head above
    code = {r.path for r in repos}
    return not changed_since(w.top, [e for e in status_entries(w.top)
                                     if not e[2].startswith("journal/") and e[2] not in code], since)


def drop_idle_session(w, sf, transcript, agent):
    """A session that never got going (a `claude -p` that failed on its arguments: its hooks ran, its
    transcript was never written) leaves no log. A missing transcript alone proves nothing (Claude can
    run without saving one), so the log must also show that nothing happened."""
    if had_exchange(transcript, agent):
        return False
    with lab_lock(w):
        if not idle_log(w, sf):
            return False
        if lab_descendants(w, read_frontmatter(sf)[0].get("session") or ""):
            return False  # it started sessions whose logs are there: their parent stays
        sf.unlink()
        build_index(w.top)
    return True


def hook_session_end(a):
    """The session's logs are closed where they are. A session ends where its agent last was: after
    `claude -w` that's the main lab, where it never worked, so the end never links a log (the session
    start and commands that change something do)."""
    data = hook_input()
    w = hook_lab(data)
    sid = data.get("session_id") or "unknown"
    logs = logs_of(w, sid)
    transcript = find_transcript(sid, a.agent, data.get("transcript_path"),
                                 *(readable_frontmatter(f).get("transcript") for _, f in logs))
    home = next(((lab, f) for lab, f in logs if readable_frontmatter(f).get("role") != "continued"), None)
    if home and data.get("transcript_path") and drop_idle_session(*home, transcript, a.agent):
        return
    # No log at all: its start never ran (Codex's comes with its first turn), or its log went with a
    # feature removed at its exit. It gets none; its parent, if any, is the one its start would have seen.
    parent = next((p for p in (readable_frontmatter(f).get("parent") for _, f in logs) if p), None) \
        or (None if logs else parent_session(w, sid))
    if parent:
        # a subagent: record what it was asked and when it ended; the parent commits it
        for _, f in logs:
            fm = read_frontmatter(f)[0]
            goal = fm.get("goal") or one_line(transcript_prompt(transcript or fm.get("transcript"), fm.get("agent")))
            update_frontmatter(f, ended=datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
                               goal=goal or None,
                               transcript=transcript if transcript and fm.get("role") != "continued" else None)
            drop_frontmatter(f, "running_since")
        return
    # before the memory is committed; after the idle check, so the repair never passes for the session's work
    index_memory(w)
    kids = lab_descendants(w, sid)  # its subagents' logs, wherever they are: it commits them
    for lab, f in logs:
        close_log(lab, f, sid, kids.pop(lab.top, []), transcript)
    for top, logs_there in kids.items():
        commit_kids(Lab(top), sid, logs_there)
    if not any(lab.top == lab.main for lab, _ in logs):
        # a session that worked only in features: the shared memory it wrote is on main
        try:
            commit_memory(w, f"memory: from session {sid[:8]} ({w.label})")
        except CudlError as e:
            print(f"cudl: main's memory/ is not committed: {e}", file=sys.stderr)


def pins_to_record(w):
    """Code repos whose HEAD moved past the lab's pin, safe to record without asking: on the branch
    this worktree owns, nothing in progress, and no gitlink staged by hand."""
    if os.environ.get("CUDL_NO_PIN_RECORDING") or read_marker(w.main).get("record_pins") is False:
        return []
    out = []
    for r in w.repos().values():
        d = w.top / r.path
        if not is_checkout(d) or head_branch(d) != (w.feature or r.branch):
            continue
        if busy(d) or conflicted(d) or pinned(w.top, r.path) == head_sha(d):
            continue
        if not git_ok(w.top, "diff", "--cached", "--quiet", "--", r.path):
            continue  # someone staged a pin deliberately; leave it to them
        out.append(r)
    return out


def close_log(w, sf, sid, kids=(), transcript=None):
    """End-of-session bookkeeping for one worktree: the log's facts, then one commit of the log, its
    descendants' logs here (`kids`), the pins the session moved, and what the agent's harness wrote
    as it went (plan mode's plans/, and in main the shared memory/). Nothing else staged or modified
    is touched, and nothing split. All under the lab lock: a child's end may be dropping its log.
    `transcript`, a path that exists, replaces the log's record of it (a moved transcript)."""
    with lab_lock(w):
        fm, _ = read_frontmatter(sf)
        now = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
        home = fm.get("role") != "continued"  # it records the transcript (a linked log's home does)
        if fm.get("feature", w.label) != w.label:
            # a finish brought it here: its worktree is gone, and this one's facts aren't its own. A linked
            # log ended when its feature's work did; the home speaks for the session, which goes on ending
            updates = {"ended": now} if home or not fm.get("ended") else {}
        else:
            wstart = (fm.get("lab") or "").split("..")[0]
            wend = short(pinned_head(w.top))
            starts = {k: v.split("..")[0] for k, v in parse_repo_map(fm.get("repos")).items()}
            ends = repo_heads(w)
            repos = ", ".join(f"{k}={starts.get(k, '-')}..{v}" if starts.get(k, v) != v else f"{k}={v}"
                              for k, v in ends.items())
            touched = set()
            if wstart and wstart != wend:
                touched.update(git(w.top, "diff", "--name-only", wstart, "HEAD", check=False).splitlines())
            touched.update(l[3:] for l in lab_changes(w.top))
            touched = sorted(t for t in touched if not t.startswith("journal/"))
            updates = {
                "ended": now,
                "lab": f"{wstart}..{wend}" if wstart and wstart != wend else wend,
                "repos": repos,
                "touched": ", ".join(touched[:30]) + (" …" if len(touched) > 30 else ""),
            }
        if updates and (fm.get("outcome") or "open") == "open":
            updates["needs_summary"] = "yes"
        if home:
            if transcript and fm.get("transcript") != transcript:
                updates["transcript"] = transcript
            n = transcript_compactions(transcript or fm.get("transcript"))
            if n:
                updates["compactions"] = str(n)
        if updates:
            update_frontmatter(sf, **updates)
        drop_frontmatter(sf, "running_since")
        what = busy(w.top)
        if what:
            print(f"cudl: {w.label} has {what} in progress; its session log is updated but not committed",
                  file=sys.stderr)
            return
        record = pins_to_record(w)
        paths = ([str(sf.relative_to(w.top))] + [str(p.relative_to(w.top)) for p in kids if p.exists()]
                 + [r.path for r in record] + ["plans"] + (["memory"] if w.top == w.main else []))
        take, split, states = select_paths(w.top, paths)
        if split:
            print(f"cudl: left alone, staged and then changed again: {', '.join(split[:5])}", file=sys.stderr)
        pins = [r for r in record if r.path in take]
        dirs = sorted({p.split("/")[0] for p in take if p.split("/")[0] in ("memory", "plans")})
        msg = (f"journal: session {sid[:8]}" + (f"; pins {', '.join(r.name for r in pins)}" if pins else "")
               + (f"; {', '.join(dirs)}" if dirs else ""))
        trailers = [x for r in pins for x in
                    ("--trailer", f"Code: {r.name} {head_branch(w.top / r.path)} {short(head_sha(w.top / r.path))}")]
        old = pinned_head(w.top)
        if commit_with_index(w.top, take, states, msg, trailers):
            keep_since(w, old)


def commit_kids(w, sid, kids):
    """The logs of a session's descendants in a worktree where it has none of its own (a subagent
    started there, or one that worked there), with the plans they wrote there: their own ends leave
    them to it."""
    with lab_lock(w):
        what = busy(w.top)
        if what:
            print(f"cudl: {w.label} has {what} in progress; subagent logs of {sid[:8]} not committed there",
                  file=sys.stderr)
            return
        paths = [str(p.relative_to(w.top)) for p in kids if p.exists()] + ["plans"]
        take, split, states = select_paths(w.top, paths)
        if split:
            print(f"cudl: left alone, staged and then changed again: {', '.join(split[:5])}", file=sys.stderr)
        commit_with_index(w.top, take, states, f"journal: subagents of session {sid[:8]}")


def lab_descendants(lab, sid):
    """{worktree top: [logs]} of the sessions sid started, of those they started, and so on, anywhere
    in the lab: a subagent's home log can be in another worktree than its parent's, and its linked
    (`continued`) logs in others still."""
    logs = [(Path(top).resolve(), p, readable_frontmatter(p)) for top in lab_tops(lab)
            for p in sorted(sessions_dir(top).glob("*.md"))]  # one unreadable log mustn't stop a session's end
    found, seen, frontier = {}, {sid}, {sid}
    while frontier:
        nxt = set()
        for top, p, fm in logs:
            if fm.get("parent") in frontier and p not in found.get(top, []):
                found.setdefault(top, []).append(p)
                if fm.get("session") and fm["session"] not in seen:
                    seen.add(fm["session"])
                    nxt.add(fm["session"])
        frontier = nxt
    return found
