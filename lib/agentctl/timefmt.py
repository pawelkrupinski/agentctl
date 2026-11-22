# agentctl, part `timefmt`: the clock's formats, one copy each: now, the ISO stamp, its parse and epoch, HH:MMZ.
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's only.

import datetime as dt
import time
from typing import Any


def now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


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
