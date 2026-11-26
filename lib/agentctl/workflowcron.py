# agentctl, part `workflowcron`: a workflow's cron schedule: its slots, the next one after a time
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import re


CRON_HORIZON_DAYS = 32


CRON_RANGES = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))


def cron_fields(cron):
    """A cron's five fields as sets of values (None for `*`), or None when it cannot be read."""
    parts = str(cron or "").split()
    if len(parts) != 5:
        return None
    out = []
    try:
        for text, (low, high) in zip(parts, CRON_RANGES):
            out.append(None if text == "*" else cron_values(text, low, high))
    except ValueError:
        return None
    if out[4] is not None:
        out[4] = {v % 7 for v in out[4]}  # 7 and 0 are both Sunday
    return out


def cron_values(text, low, high):
    vals = set()
    for item in text.split(","):
        rng, _, step = item.partition("/")
        if rng == "*":
            a, b = low, high
        elif "-" in rng:
            a, b = map(int, rng.split("-"))
        else:
            a = int(rng)
            b = high if step else a
        vals |= set(range(a, b + 1, int(step) if step else 1))
    return vals


def cron_hit(vals, v):
    return vals is None or v in vals


def cron_matches(fields, at):
    minutes, hours, days, months, weekdays = fields
    weekday = (at.weekday() + 1) % 7  # Python's Monday=0 -> cron's Sunday=0
    if days is not None and weekdays is not None:  # cron ORs a restricted day of month and day of week
        day = at.day in days or weekday in weekdays
    else:
        day = cron_hit(days, at.day) and cron_hit(weekdays, weekday)
    return cron_hit(minutes, at.minute) and cron_hit(hours, at.hour) and cron_hit(months, at.month) and day


def cron_next(crons, after):
    """The first whole minute (epoch, UTC) after `after` that any of the crons fires at; None when none does within
    CRON_HORIZON_DAYS or none can be read."""
    import datetime as _dt
    fields = [f for f in (cron_fields(c) for c in crons) if f]
    if not fields:
        return None
    at = _dt.datetime.fromtimestamp(int(float(after) // 60) * 60 + 60, _dt.timezone.utc)
    end = at + _dt.timedelta(days=CRON_HORIZON_DAYS)
    while at < end:
        if not any(cron_hit(f[1], at.hour) for f in fields):  # no cron fires in this hour: the next one
            at = at.replace(minute=0) + _dt.timedelta(hours=1)
            continue
        if any(cron_matches(f, at) for f in fields):
            return at.timestamp()
        at += _dt.timedelta(minutes=1)
    return None


def workflow_crons(text):
    """The `on.schedule` crons of a workflow file's text."""
    return [m.group(2) for m in re.finditer(r"^\s*-\s*cron:\s*(['\"])(.+?)\1", text or "", re.M)]
