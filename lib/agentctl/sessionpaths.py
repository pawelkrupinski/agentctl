# agentctl, part `sessionpaths`: the worktrees the live Claude sessions work in, read from their transcripts' tool calls
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from agentctl.clock import wall_epoch
import glob
import json
import os
import re
import time
from contextlib import suppress
from typing import Any, Iterator
from agentctl.core import HOME, attempt, hashlib_sha1
from agentctl.statefile import read_record, write_json
from agentctl.claudefiles import claude_projects_root
from agentctl.live import live_slug
from agentctl.conditions import recovery_dir
from agentctl.fixerknobs import knob_owner_active


SESSION_PATHS = "session-paths.json"


SESSION_PATH_DEPTH = 10  # path components kept per named path (a worktree is never deeper)


NAMED_PATH = re.compile(r"(?<![\w.~/$-])(~|\$HOME|\$\{HOME\})?(/[^\s\"'`;|&<>(){}\\,*?\[\]]+)")


def tool_call_paths(text: Any) -> set[str]:
    """The absolute paths a tool call's text names (`~/` and `$HOME/` expanded), each cut to SESSION_PATH_DEPTH parts."""
    out = set()
    for m in NAMED_PATH.finditer(str(text or "")):
        p = (HOME if m.group(1) else "") + m.group(2)
        p = p.rstrip(":.")
        parts = p.split("/")
        if len(parts) > 1 and parts[1]:
            out.add("/".join(parts[:SESSION_PATH_DEPTH + 1]))
    return out


def tool_input_strings(x: Any) -> Iterator[str]:
    if isinstance(x, str):
        yield x
    elif isinstance(x, dict):
        for v in x.values():
            yield from tool_input_strings(v)
    elif isinstance(x, list):
        for v in x:
            yield from tool_input_strings(v)


def transcript_session_key(path: str) -> str:
    """The session a transcript belongs to: its own path less `.jsonl`, or the session dir above its `subagents/`."""
    return path[:-len(".jsonl")] if "subagents" not in path else os.path.dirname(os.path.dirname(path))


def session_transcripts(t=None, active=None):
    """{session key: [(path, mtime)]} of every LIVE session's transcripts (its own and its subagents'): a session is live
    while any of them was written within `active` s."""
    t = wall_epoch() if t is None else t  # against transcript mtimes
    active = knob_owner_active() if active is None else active
    root = claude_projects_root()
    groups = {}
    for g in (os.path.join(glob.escape(root), "*", "*.jsonl"), os.path.join(glob.escape(root), "*", "*", "subagents", "*.jsonl")):
        for p in glob.glob(g):
            m = attempt(lambda: os.stat(p).st_mtime, None, catch=(OSError,))
            if m is None:
                continue
            groups.setdefault(transcript_session_key(p), []).append((p, m))
    return {k: v for k, v in groups.items() if any(t - m <= active for _p, m in v)}


def _transcript_tail(path: str, cache: dict[str, Any]) -> tuple[dict[str, Any], bytes]:
    """(the cache record of transcript `path` to extend, the bytes it has not read yet): a rewritten file is read whole."""
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        head = hashlib_sha1(f.read(512).decode("utf-8", "replace"))
        rec = cache.get(path)
        if not rec or rec.get("head") != head or int(rec.get("offset") or 0) > size:
            rec = {"head": head, "offset": 0, "paths": []}
        f.seek(int(rec["offset"]))
        return rec, f.read()


def _assistant_tool_calls(line: bytes) -> list[dict[str, Any]]:
    """The tool calls of one transcript line (an assistant line naming a tool call; else none)."""
    if b'"tool_use"' not in line or b"/" not in line:
        return []
    try:
        r = json.loads(line)
    except ValueError:
        return []
    if r.get("type") != "assistant":
        return []
    return [c for c in (r.get("message") or {}).get("content") or [] if isinstance(c, dict) and c.get("type") == "tool_use"]


def transcript_paths(path, cache):
    """The paths the tool calls of transcript `path` named, read on from where cache[path] stopped (re-read whole when
    the file was rewritten). Updates cache in place."""
    try:
        rec, data = _transcript_tail(path, cache)
    except OSError:
        return set((cache.get(path) or {}).get("paths") or [])
    end = data.rfind(b"\n") + 1  # a line still being written is read next time
    paths = set(rec["paths"])
    for line in data[:end].split(b"\n"):
        for call in _assistant_tool_calls(line):
            for text in tool_input_strings(call.get("input")):
                paths |= tool_call_paths(text)
    rec.update(offset=int(rec["offset"]) + end, paths=sorted(paths))
    cache[path] = rec
    return paths


def _worktree_forms(wts: Any) -> dict[str, set[str]]:
    """{realpath: the spellings a tool call may name it by} of the worktrees wts (the non-empty strings)."""
    want = {}
    for w in wts:
        if isinstance(w, str) and w:
            want[os.path.realpath(w)] = {w.rstrip("/"), os.path.realpath(w)}
    return want


def _transcript_ids(path: str) -> tuple[str, str]:
    """(the session id, the project directory name) of a transcript, its own or a subagent's."""
    proj_dir = os.path.dirname(path) if "subagents" not in path else os.path.dirname(os.path.dirname(os.path.dirname(path)))
    return os.path.basename(path)[:-len(".jsonl")], os.path.basename(proj_dir)


def _claim_worktrees(want: dict[str, set[str]], out: dict[str, str], sid: str, proj: str, paths: set[str]) -> None:
    """Adds to out each worktree of want (not yet owned) the transcript's project is, or whose path its tool calls named."""
    for rp, forms in want.items():
        if rp not in out and (proj == live_slug(rp) or any(p == f or p.startswith(f + "/") for p in paths for f in forms)):
            out[rp] = f"session {sid[:12]}"


def session_worktrees(wts, t=None, active=None):
    """{realpath of a worktree of wts: the session that owns it} -- see OWNERSHIP FOLLOWS ACTIVITY."""
    want = _worktree_forms(wts)
    if not want:
        return {}
    cache_path = recovery_dir(SESSION_PATHS)
    cache = read_record(cache_path) or {}
    seen, out = {}, {}
    for _key, files in session_transcripts(t, active).items():
        for path, _m in sorted(files):
            sid, proj = _transcript_ids(path)
            paths = transcript_paths(path, cache)
            seen[path] = cache.get(path)
            _claim_worktrees(want, out, sid, proj, paths)
    with suppress(OSError):
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        write_json(cache_path, {k: v for k, v in seen.items() if v})  # only live sessions' transcripts are kept
    return out
