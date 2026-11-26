# agentctl, part `claudefiles`: Claude Code's own files: its projects (transcripts) and background jobs roots, a background session's transcript, a fixer's final verdict.
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import datetime as dt
import glob
import json
import os
import re
from zoneinfo import ZoneInfo
from agentctl.core import HOME
from agentctl.fixerknobs import knob_claude_jobs, knob_claude_projects


# -- 1. the lander: who ran `agentctl land`
def claude_projects_root() -> str:
    return knob_claude_projects() or os.path.join(HOME, ".claude", "projects")


FIXER_VERDICT = re.compile(r"^\W*(LANDED [0-9a-f]{7,40}|WAITS ON [0-9a-f]{7,40}.*|NOT LANDED\b.*|GAVE UP:.*|"
                           r"BLOCKED ON OWNER:.*|DIAGNOSED:.*)", re.M)


def _stream_result(text: str) -> str | None:
    """The `result` of the last stream-json result line of text, else None."""
    final = None
    for line in text.splitlines():
        if line.startswith("{"):
            try:
                o = json.loads(line)
            except ValueError:
                continue
            if o.get("type") == "result":
                final = o.get("result") or ""
    return final


def fixer_verdict(text: str) -> str | None:
    """The fixer's verdict: the last verdict line of its final answer (stream-json's `result`), else of its output."""
    final = _stream_result(text)
    for t in ([final] if final else []) + [text]:
        found = FIXER_VERDICT.findall(t)
        if found:
            return str(found[-1]).strip()
    return None


def claude_jobs_root() -> str:
    return knob_claude_jobs() or os.path.join(HOME, ".claude", "jobs")


def bg_transcript(session: str) -> str | None:
    hits = glob.glob(os.path.join(claude_projects_root(), "*", f"{session}.jsonl"))
    return hits[0] if hits else None


# -- 2. the account's usage limit: claude refuses every turn until it resets
# THE FIXERS "CRASHED" ON A SPENT ACCOUNT LIMIT (2026-11-23..24, Main #2877, Tool self-tests #533, Alert backtest #26):
# each launch ended at once with `You've hit your weekly limit · resets Nov 25 at 2am`, three in a row were counted as a
# crash loop, and each red was held on a HARNESS FAULT issue for a day after the limit had reset. A spent limit is an
# outside wait that ends on its own at the time claude names: no attempt, no crash, the red dispatched again after it.
CLAUDE_LIMIT = re.compile(r"(?:You've hit your\b[^\n]*?\blimit|(?:usage|rate) limit (?:reached|exceeded))[^\n]*", re.I)
CLAUDE_LIMIT_RESET = re.compile(r"resets\s+(?:(?:on\s+)?(?P<mon>[A-Z][a-z]{2})[a-z]*\s+(?P<day>\d{1,2}),?\s+(?:at\s+)?)?"
                                r"(?:at\s+)?(?P<h>\d{1,2})(?::(?P<m>\d{2}))?\s*(?P<ap>am|pm)?(?:\s*\((?P<tz>[\w/+-]+)\))?", re.I)
CLAUDE_LIMIT_FALLBACK = 3600   # s: a limit naming no time it resets is tried again an hour on


def claude_limit(text: object) -> str | None:
    """Claude's own line saying the account's usage limit is spent (`You've hit your weekly limit · resets ...`), None
    when the text says no such thing."""
    m = CLAUDE_LIMIT.search(str(text or ""))
    return " ".join(m.group(0).split())[:200] if m else None


def limit_zone(name: str | None) -> dt.tzinfo | None:
    """The zone a limit line names (`(Europe/Warsaw)`, `(UTC)`), None for none or one unknown here (local time)."""
    try:
        return ZoneInfo("UTC" if (name or "").upper() == "UTC" else name) if name else None
    except (ValueError, KeyError, OSError):
        return None


def limit_hour(m: re.Match[str]) -> int:
    """The 24-hour clock hour of a `resets H[am|pm]`."""
    h = int(m["h"])
    return h if not m["ap"] else h % 12 + (12 if m["ap"].lower() == "pm" else 0)


def claude_limit_reset(line: str, now: float) -> float:
    """When the limit `line` names resets (its `resets [Mon D at] H[:MM]am|pm [(Zone)]`, in that zone or else local
    time), the first such moment after `now`; CLAUDE_LIMIT_FALLBACK after `now` when it names none."""
    m = CLAUDE_LIMIT_RESET.search(line or "")
    if not m:
        return now + CLAUDE_LIMIT_FALLBACK
    tz = limit_zone(m["tz"])
    base = dt.datetime.fromtimestamp(now, tz) if tz else dt.datetime.fromtimestamp(now).astimezone()
    at = base.replace(hour=limit_hour(m) % 24, minute=int(m["m"] or 0), second=0, microsecond=0)
    if not m["mon"]:
        return (at if at.timestamp() > now else at + dt.timedelta(days=1)).timestamp()
    try:
        at = at.replace(month=dt.datetime.strptime(m["mon"][:3].title(), "%b").month, day=int(m["day"]))
    except ValueError:
        return now + CLAUDE_LIMIT_FALLBACK
    return (at.replace(year=at.year + 1) if at.timestamp() < now - 180 * 86400 else at).timestamp()  # "Jan 2" read in Dec
