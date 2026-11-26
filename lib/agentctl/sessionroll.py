# agentctl, part `sessionroll`: a long-lived session past its context limit starts fresh, with a handover from agentctl's state
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import json
import os
from typing import Any, BinaryIO, Optional
from agentctl.configfile import env_num
from agentctl.core import git, tilde
from agentctl.timefmt import ts
from agentctl.claudefiles import bg_transcript
from agentctl.fixersession import turn_entry
from agentctl.redrecord import red_active, red_what
from agentctl.issues import status_issues


# A SESSION PAST ITS CONTEXT LIMIT STARTS FRESH (the owner, 2026-11-25: "fix"). Every turn of a family owner resumed the
# SAME session: outside-gate reached ~985k tokens of context by turn 134, so each resume reloaded close to a million
# tokens (a large share of the weekly limit) and one took 32 min to write its first transcript line. Before a resume
# agentctl reads the session's context from its transcript -- the last assistant turn's input + cache-read +
# cache-creation tokens -- and past CONTEXT_FRESH it starts a fresh session instead, its first prompt carrying a short
# handover built from agentctl's own store and records (never asked of the old session): the reds in hand, the
# worktree's branch and dirty files, the last verdicts, the open issues and the old session's last answers. An
# unreadable transcript, or one with no usage figures, resumes as before.
CONTEXT_FRESH = 300000       # tokens: a session whose last turn's context is past this is not resumed but started fresh
HANDOVER_MAX = 7000          # characters (~2k tokens): the handover's budget
TAIL_CHUNK = 1 << 18         # bytes read from a transcript's end at a time, doubled until the figures are found


def tail_entries(path: Optional[str]) -> list[dict[str, Any]]:
    """The transcript's entries, read from its end: the last chunk first, the whole file only when the chunk held no
    assistant entry with usage figures. [] for a missing or unreadable file."""
    try:
        size = os.path.getsize(path or "")
        with open(path or "", "rb") as f:
            n = TAIL_CHUNK
            while True:
                out = chunk_entries(f, size, n)
                if n >= size or any(usage_of(e) for e in out):
                    return out
                n *= 2
    except (OSError, TypeError):
        return []


def chunk_entries(f: BinaryIO, size: int, n: int) -> list[dict[str, Any]]:
    """The entries of the file's last `n` bytes (its first line, cut by the chunk, dropped unless the file starts it)."""
    f.seek(max(0, size - n))
    lines = f.read().decode("utf-8", "replace").splitlines()[(1 if n < size else 0):]
    return [e for e in (entry(l) for l in lines) if e is not None]


def entry(line: str) -> Optional[dict[str, Any]]:
    try:
        e = json.loads(line)
    except ValueError:
        return None
    return e if isinstance(e, dict) else None


def usage_of(e: dict[str, Any]) -> Optional[dict[str, Any]]:
    """An assistant entry's usage figures (the session's own, never a sidechain's: a subagent's context is its own)."""
    m = e.get("message")
    u = m.get("usage") if isinstance(m, dict) and e.get("type") == "assistant" and not e.get("isSidechain") else None
    return u if isinstance(u, dict) else None


def context_tokens(path: Optional[str]) -> Optional[int]:
    """The session's context at its last assistant turn: input + cache-read + cache-creation tokens. None when the
    transcript is missing, unreadable or carries no usage figures."""
    for e in reversed(tail_entries(path)):
        u = usage_of(e)
        if u:
            return sum(int(u.get(k) or 0) for k in ("input_tokens", "cache_read_input_tokens",
                                                     "cache_creation_input_tokens"))
    return None


def last_answers(path: Optional[str], n: int = 3, cut: int = 600) -> list[str]:
    """The session's last `n` answers (its final messages), each its last `cut` characters."""
    texts = [t for k, t in (turn_entry(e) for e in tail_entries(path)) if k == "answer"]
    return [t.strip()[-cut:] for t in texts[-n:]]


def fresh_due(path: Optional[str]) -> tuple[bool, Optional[int]]:
    """(the session is past the context limit, its context in tokens or None)."""
    tokens = context_tokens(path)
    return tokens is not None and tokens > env_num("AGENTCTL_CONTEXT_FRESH", CONTEXT_FRESH), tokens


def worktree_lines(wt: Optional[str]) -> list[str]:
    """The worktree, its branch and HEAD, and its dirty files (at most 15)."""
    if not wt or not os.path.isdir(wt):
        return [f"worktree: {wt} (gone)"]
    head = str(git(wt, "log", "-1", "--format=%h %s", check=False).stdout).strip()[:120]
    branch = str(git(wt, "symbolic-ref", "--short", "-q", "HEAD", check=False).stdout).strip() or "(detached)"
    dirty = [l for l in str(git(wt, "status", "--porcelain", check=False).stdout).splitlines() if l.strip()]
    return ([f"worktree: {tilde(wt)}, branch {branch}, HEAD {head}",
             f"dirty files ({len(dirty)}):" if dirty else "dirty files: none"] + [f"  {l}" for l in dirty[:15]])


def held_red_lines(store: Any, names: list[str]) -> list[str]:
    """Each red in hand (lease `red/<key>`): what it is, its state and its failure line."""
    active = red_active(store)
    out = []
    for n in names:
        it = active.get(n.split("/", 1)[-1])
        d = (it or {}).get("data") or {}
        out.append(f"- {red_what(d)} (red {d.get('key')}, {(it or {}).get('state')}): {str(d.get('line') or '')[:160]}"
                   if it else f"- {n}: no longer open")
    return out


def issue_lines(store: Any, wt: Optional[str], words: tuple[str, ...]) -> list[str]:
    """The open issues filed from the worktree or naming one of `words` (at most 5)."""
    hits = [i for i in status_issues(store, 0) if (wt and i.get("cwd") == wt)
            or any(w and w in str(i.get("text")) for w in words)]
    return [f"- {i['id']}: {str(i.get('text'))[:200]}" for i in hits[:5]]


def handover_text(old: str, tokens: Optional[int], parts: dict[str, list[str]]) -> str:
    """The fresh session's handover: why it is fresh, then each part's lines, cut to HANDOVER_MAX characters."""
    out = [f"A FRESH SESSION, HANDED OVER (agentctl, {ts()}): the previous session {old[:8]} had {tokens} tokens of "
           f"context, past the {int(env_num('AGENTCTL_CONTEXT_FRESH', CONTEXT_FRESH))} limit, so you start fresh. "
           "What it left, from agentctl's records:"]
    for title, lines in parts.items():
        out += [f"{title}:"] + (lines or ["  (none)"])
    text = "\n".join(out)
    return text if len(text) <= HANDOVER_MAX else text[:HANDOVER_MAX - 30] + "\n... (handover cut)"


def rollover_entry(old: str, tokens: Optional[int]) -> dict[str, Any]:
    """The record of one rollover; `new` is filled when the fresh session's id is first seen (session_seen)."""
    return {"at": ts(), "old": old, "new": None, "tokens": tokens}


def session_seen(rec: dict[str, Any], sid: str) -> None:
    """The turn's session id (and its transcript) into the record; a rollover's fresh session named in it."""
    last = (rec.get("rollovers") or [{}])[-1]
    if last and last.get("new") is None and last.get("old") != sid:
        last["new"] = sid
    rec.update(session=sid, transcript=bg_transcript(sid))


def owner_rollover(store: Any, fam: str, rec: dict[str, Any], names: list[str]) -> Optional[str]:
    """Before an owner's turn resumes rec["session"]: past the context limit, the session is dropped (the turn starts
    fresh), the rollover recorded, and the handover returned; else None (resumed, its context recorded)."""
    old = rec.get("session")
    transcript = rec.get("transcript") or bg_transcript(old or "")
    due, tokens = fresh_due(transcript) if old else (False, None)
    rec["context_tokens"] = tokens
    if not old or not due:
        return None
    text = handover_text(old, tokens, {
        "the reds in hand (yours, this turn)": held_red_lines(store, names),
        "your worktree": worktree_lines(rec.get("worktree")),
        "your notebook (read it first)": [f"  {rec.get('notebook')}"],
        "the last verdicts": [f"- {v}" for v in (rec.get("verdicts") or [])[-5:]]
        + [f"- last wrapper end: {rec.get('last_end')}"],
        "open issues": issue_lines(store, rec.get("worktree"), (fam,)),
        "the previous session's last messages (its decisions)": [f"--- {a}" for a in last_answers(transcript)]})
    rec["rollovers"] = (rec.get("rollovers") or [])[-9:] + [rollover_entry(old, tokens)]
    rec.update(session=None, transcript=None)
    rec.pop("resumable", None)  # never fall back to the session just retired
    return text


def owner_turn_line(rec: dict[str, Any], fam: str, turn: int, outcome: str) -> str:
    """The turn's verdict line, kept in the record (its last five) for a fresh session's handover."""
    line = f"RED OWNER {fam} turn {turn}: {outcome[:300]}"
    rec["verdicts"] = (rec.get("verdicts") or [])[-4:] + [f"turn {turn}: {outcome[:200]}"]
    return line


def fixer_rollover(store: Any, rec: dict[str, Any]) -> Optional[str]:
    """Before a fixer resumes rec["resumed_from"] (a revived lander, a resumable fixer): past the context limit, no
    resume -- a fresh session whose resume note carries the handover -- and the rollover recorded. The resume note."""
    old = rec.get("resumed_from") if rec.get("resume") else None
    transcript = bg_transcript(old) if old else None
    due, tokens = fresh_due(transcript)
    note: Optional[str] = rec.get("resume_note")
    if not old or not due:
        return note
    note = "\n\n".join(x for x in (note, handover_text(old, tokens, fixer_handover(store, rec, transcript))) if x)
    rec.update(resume=False, resume_note=note, rollover=rollover_entry(old, tokens))
    return note


def fixer_handover(store: Any, rec: dict[str, Any], transcript: Optional[str]) -> dict[str, list[str]]:
    """A fixer's handover: its red, its worktree, the open issues of either, its old session's last messages."""
    return {
        "the red": [f"- {rec.get('workflow')} #{rec.get('run')} {rec.get('job') or ''}: {str(rec.get('line'))[:160]}"],
        "the worktree": worktree_lines(rec.get("worktree")),
        "open issues": issue_lines(store, rec.get("worktree"), (str(rec.get("key") or ""),)),
        "the previous session's last messages (its decisions)": [f"--- {a}" for a in last_answers(transcript)]}
