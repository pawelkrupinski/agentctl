# agentctl, part `timefmt`: the clock's formats, one copy each: now, the ISO stamp, its parse and epoch, HH:MMZ.
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's only.

from agentctl.clock import epoch_now
import argparse
import datetime as dt
import re
import time
from typing import Any


def now() -> dt.datetime:
    return dt.datetime.fromtimestamp(epoch_now(), dt.timezone.utc)


def ts() -> str:
    return now().strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_time(s: str) -> dt.datetime:
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


# THE TIME FORMATS (one copy each): GitHub's and agentctl's ISO stamps are UTC `2026-11-22T10:00:00Z`.
def iso_epoch(s: Any) -> float | None:
    """The epoch of an ISO stamp (a `Z` or an offset; none: UTC), or None when it is missing or no stamp."""
    try:
        t = parse_time(str(s))
    except (TypeError, ValueError):
        return None
    return (t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)).timestamp()


def iso_at(t: float) -> str:
    return dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def hhmmz(t: float | str) -> str:
    return time.strftime("%H:%MZ", time.gmtime(float(t)))


def day_hhmmz(t: float | str) -> str:
    return time.strftime("%Y-%m-%d %H:%MZ", time.gmtime(float(t)))


def age_since(since, now=None):
    """'3d 4h' / '5h 12m' / '7m' since an ISO time, or '?' when it cannot be read."""
    try:
        t = dt.datetime.fromisoformat(str(since).replace("Z", "+00:00"))
    except ValueError:
        return "?"
    if now is None:  # the process's clock (the tests wire theirs; review 2026-11-26: $AGENTCTL_NOW was a second one)
        now = dt.datetime.fromtimestamp(epoch_now(), dt.timezone.utc)
    mins = max(0, int((now - t).total_seconds() // 60))
    d, h, m = mins // 1440, mins % 1440 // 60, mins % 60
    return f"{d}d {h}h" if d else f"{h}h {m}m" if h else f"{m}m"


def parse_duration(text: object) -> float:
    """Seconds of `90`, `90s`, `30m`, `3h`."""
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([smh]?)\s*", str(text))
    if not m:
        raise argparse.ArgumentTypeError(f"not a duration: {text!r} (90, 90s, 30m, 3h)")
    return float(m.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600}[m.group(2)]
