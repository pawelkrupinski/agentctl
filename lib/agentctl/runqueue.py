# agentctl, part `runqueue`: the runner queue explained: a queued run that waits only on labels whose every serving runner is up and full
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from typing import Any
import json
from agentctl.core import attempt, fmt_age


def serving_runners(q: dict[str, Any], want: set[str]) -> dict[str, Any]:
    """{name: runner} of the queue snapshot's runners whose labels include all of `want`; {} when a label cannot be read."""
    runners = ((q.get("limits") or {}).get("runners") or {}).items()
    return attempt(lambda: {n: r for n, r in runners if want <= set(json.loads(r.get("labels") or "[]"))},
                   {}, catch=(ValueError, TypeError))


def slot_usage(q: dict[str, Any], serving: dict[str, Any]) -> tuple[int, int]:
    """(busy slots, capacity) of the `serving` runners in queue snapshot q."""
    cap = sum(int(r.get("capacity") or 0) for r in serving.values())
    return sum(1 for x in q.get("running") or [] if x.get("runner") in serving), cap


def waiting_ahead(waits: list[dict[str, Any]], mine: list[dict[str, Any]], label: str) -> tuple[int, int]:
    """(how many jobs on `label` have waited longer than the run's own, the longest wait) in seconds."""
    waited = [int(w.get("secs") or 0) for w in waits if w.get("label") == label]
    own = max(int(w.get("secs") or 0) for w in mine if w.get("label") == label)
    return sum(1 for x in waited if x > own), max(waited)


def label_full_says(q: dict[str, Any], waits: list[dict[str, Any]], mine: list[dict[str, Any]], label: str) -> str | None:
    """'<labels> full (busy/cap busy, M ahead, oldest Xm)' when every runner serving `label` is up with all its slots
    busy, else None (a free slot, a runner down, or a label that cannot be read). mine: the run's waiting jobs."""
    want = attempt(lambda: set(json.loads(label)), None, catch=(ValueError, TypeError))
    serving = serving_runners(q, want) if want is not None else {}
    if want is None or not serving or not all(r.get("state") == "running" for r in serving.values()):
        return None
    busy, cap = slot_usage(q, serving)
    if busy < cap:
        return None
    ahead, oldest = waiting_ahead(waits, mine, label)
    return f"{', '.join(sorted(want))} full ({busy}/{cap} busy, {ahead} ahead, oldest {fmt_age(oldest)})"


def waiting_jobs(q: dict[str, Any] | None, o_r: Any, number: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(every waiting job of queue snapshot q, those of run #number of o_r)."""
    waits = (q or {}).get("waiting_jobs") or []
    return waits, [w for w in waits if w.get("repo") == o_r and str(w.get("run")) == str(number)] if number is not None else []


def queue_full(o_r: Any, number: Any, q: dict[str, Any] | None) -> str | None:
    """'queued: <label> full (N/N busy, M ahead, oldest Xm)' when run #number of o_r waits only on labels whose every
    serving runner is up with all its slots busy, else None (a free slot, a runner down, or nothing known: stuck)."""
    waits, mine = waiting_jobs(q, o_r, number)
    if not mine:
        return None
    says = [label_full_says(q, waits, mine, label) for label in sorted({str(w.get("label")) for w in mine})]
    return None if None in says else "queued: " + "; ".join(says)
