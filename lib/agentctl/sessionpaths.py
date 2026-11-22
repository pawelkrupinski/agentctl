# agentctl, part `sessionpaths`: the worktrees the live Claude sessions work in, read from their transcripts' tool calls
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import glob
import json
import os
import re
import time
from agentctl.core import HOME, env_num, hashlib_sha1
from agentctl.statefile import read_record, write_json
from agentctl.claudefiles import claude_projects_root
from agentctl.live import live_slug
from agentctl.conditions import recovery_dir


# OWNERSHIP FOLLOWS ACTIVITY (the owner, 2026-11-03: movies-wt-unmatched-imdbslip escalated UNLANDED ORPHAN four times
# while an agent was landing it -- the agent's Claude session runs from another directory and works in the worktree by
# absolute path, so no process had its cwd there). A LIVE Claude session -- any transcript of it (the session's own or
# one of its subagents') written within OWNER_ACTIVE -- owns every worktree a tool call of it EVER named (or whose
# project directory is the worktree's): no orphan action, and an open UNLANDED ORPHAN clears ("a live session works in
# it"). Ownership lapses when the session goes quiet.
# 2026-11-06 22:51Z (es-gate-check): the rule used to ask for a tool call naming the worktree within the last 30 min, in
# the transcript's last 256 KB. The subagent that made ~/projects/movies-wt/es-gate-check had named it last 104 min
# before while it waited on CI from a sibling worktree, its transcript written minutes before: alive, and its worktree
# was taken for an orphan, rebased and queued. Every transcript of a live session is now read whole -- incrementally,
# from where the last pass stopped (SESSION_PATHS, a cache of the paths each one named) -- and `~/` / `$HOME/` paths
# count as the absolute paths they are.
OWNER_ACTIVE = 2 * 3600


SESSION_PATHS = "session-paths.json"


SESSION_PATH_DEPTH = 10  # path components kept per named path (a worktree is never deeper)


NAMED_PATH = re.compile(r"(?<![\w.~/$-])(~|\$HOME|\$\{HOME\})?(/[^\s\"'`;|&<>(){}\\,*?\[\]]+)")


def tool_call_paths(text):
    """The absolute paths a tool call's text names (`~/` and `$HOME/` expanded), each cut to SESSION_PATH_DEPTH parts."""
    out = set()
    for m in NAMED_PATH.finditer(str(text or "")):
        p = (HOME if m.group(1) else "") + m.group(2)
        p = p.rstrip(":.")
        parts = p.split("/")
        if len(parts) > 1 and parts[1]:
            out.add("/".join(parts[:SESSION_PATH_DEPTH + 1]))
    return out


def tool_input_strings(x):
    if isinstance(x, str):
        yield x
    elif isinstance(x, dict):
        for v in x.values():
            yield from tool_input_strings(v)
    elif isinstance(x, list):
        for v in x:
            yield from tool_input_strings(v)


def session_transcripts(t=None, active=None):
    """{session key: [(path, mtime)]} of every LIVE session's transcripts (its own and its subagents'): a session is live
    while any of them was written within `active` s."""
    t = time.time() if t is None else t
    active = env_num("AGENTCTL_OWNER_ACTIVE", OWNER_ACTIVE) if active is None else active
    root = claude_projects_root()
    groups = {}
    for g in (os.path.join(glob.escape(root), "*", "*.jsonl"), os.path.join(glob.escape(root), "*", "*", "subagents", "*.jsonl")):
        for p in glob.glob(g):
            try:
                m = os.stat(p).st_mtime
            except OSError:
                continue
            key = p[:-len(".jsonl")] if "subagents" not in p else os.path.dirname(os.path.dirname(p))
            groups.setdefault(key, []).append((p, m))
    return {k: v for k, v in groups.items() if any(t - m <= active for _p, m in v)}


def transcript_paths(path, cache):
    """The paths the tool calls of transcript `path` named, read on from where cache[path] stopped (re-read whole when
    the file was rewritten). Updates cache in place."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            head = hashlib_sha1(f.read(512).decode("utf-8", "replace"))
            rec = cache.get(path)
            if not rec or rec.get("head") != head or int(rec.get("offset") or 0) > size:
                rec = {"head": head, "offset": 0, "paths": []}
            f.seek(int(rec["offset"]))
            data = f.read()
    except OSError:
        return set((cache.get(path) or {}).get("paths") or [])
    end = data.rfind(b"\n") + 1  # a line still being written is read next time
    paths = set(rec["paths"])
    for line in data[:end].split(b"\n"):
        if b'"tool_use"' not in line or b"/" not in line:
            continue
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if r.get("type") != "assistant":
            continue
        for c in (r.get("message") or {}).get("content") or []:
            if isinstance(c, dict) and c.get("type") == "tool_use":
                for text in tool_input_strings(c.get("input")):
                    paths |= tool_call_paths(text)
    rec.update(offset=int(rec["offset"]) + end, paths=sorted(paths))
    cache[path] = rec
    return paths


def session_worktrees(wts, t=None, active=None):
    """{realpath of a worktree of wts: the session that owns it} -- see OWNERSHIP FOLLOWS ACTIVITY."""
    want = {}
    for w in wts:
        if isinstance(w, str) and w:
            want[os.path.realpath(w)] = {w.rstrip("/"), os.path.realpath(w)}
    if not want:
        return {}
    cache_path = recovery_dir(SESSION_PATHS)
    cache = read_record(cache_path) or {}
    seen, out = {}, {}
    for _key, files in session_transcripts(t, active).items():
        for path, _m in sorted(files):
            sid = os.path.basename(path)[:-len(".jsonl")]
            proj = os.path.basename(os.path.dirname(path) if "subagents" not in path else
                                    os.path.dirname(os.path.dirname(os.path.dirname(path))))
            paths = transcript_paths(path, cache)
            seen[path] = cache.get(path)
            for rp, forms in want.items():
                if rp in out:
                    continue
                if proj == live_slug(rp) or any(p == f or p.startswith(f + "/") for p in paths for f in forms):
                    out[rp] = f"session {sid[:12]}"
    try:
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        write_json(cache_path, {k: v for k, v in seen.items() if v})  # only live sessions' transcripts are kept
    except OSError:
        pass
    return out
