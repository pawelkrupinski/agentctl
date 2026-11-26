# agentctl, part `timeline`: `agentctl history` (one item's timeline) and `agentctl analyze` (the queue's time sinks)
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.
#
# The process-mining analysis of 2026-11-26 (store.db copy, 20,289 events) as tested code over the store's events, the
# backfill's derived rows merged in (backfill.py: each carries its source and confidence). Time in a state is the gap
# between consecutive REAL moves of an item (from != to); a self-move (a reminder, an escalation) splits a stay into
# SEGMENTS, each attributed to the cause last said for it -- the cause it entered the state for, then each reminder's
# (a held change's hold kind). ATTRIBUTION COVERAGE is the share of that time with a cause: own (the event's column)
# or derived (the backfill's), shown both ways. The clock is injected (`now`): an open stay ends at it.

import collections
import json
import math
from dataclasses import replace
from typing import Any, Iterable, Optional

from agentctl.timefmt import iso_at
from agentctl.eventtrace import ItemTrace, Segment, ends_item, traces


def nearest_rank(xs: list[float], p: float) -> float:
    if not xs:
        return float("nan")
    s = sorted(xs)
    return s[min(len(s) - 1, max(0, math.ceil(p / 100 * len(s)) - 1))]


def dist(xs: list[float]) -> dict[str, float]:
    return {"n": len(xs), "p50": nearest_rank(xs, 50), "p90": nearest_rank(xs, 90), "max": max(xs) if xs else float("nan"),
            "total": sum(xs)}


def analyze(events: list[dict[str, Any]], now: float, since: float = 0.0, item_type: str = "change") -> dict[str, Any]:
    """The report: time per state, path variants, rework loops, refusals ranked, waiting by cause, throughput per day,
    outliers with ids, attribution coverage (own, and with the derived rows)."""
    trs, segs = _window(events, now, since, item_type)
    return {"items": len(trs), "since": since, "now": now,
            "states": _by(segs, lambda s: s.state),
            "variants": _variants(trs), "rework": _rework(trs), "refusals": _refusals(trs),
            "waiting_by_cause": _by([s for s in segs if s.state in ("queued", "checked", "waiting")],
                                    lambda s: f"{s.state}: {s.cause or 'unattributed'}"),
            "throughput": _throughput(trs), "outliers": _outliers(segs), "coverage": coverage(segs),
            "to_end_by_kind": _to_end_by_kind(trs)}


def _to_end_by_kind(trs: list[ItemTrace]) -> dict[str, dict[str, float]]:
    """For items named `<kind>/<id>` (an escalation: escalations.py), the time from the first event to the terminal
    one, by kind and how it ended (`INVARIANT:W2 -> cleared`): an escalation's time to clear, per kind."""
    groups: dict[str, list[float]] = collections.defaultdict(list)
    for t in trs:
        if "/" in t.item_id and t.path and ends_item(t.item_type, t.path[-1]) and t.events:
            groups[f"{t.item_id.rsplit('/', 1)[0]} -> {t.path[-1]}"].append(
                float(t.events[-1]["at"]) - float(t.events[0]["at"]))
    return dict(sorted((k, dist(v)) for k, v in groups.items()))


def _window(events: list[dict[str, Any]], now: float, since: float, item_type: str) -> tuple[list[ItemTrace], list[Segment]]:
    """The items active in [since, now] (a move or a stay in it) and their segments, clipped to it."""
    trs = [t for t in traces(events, now, item_type).values()
           if float(t.events[-1]["at"]) >= since or any(s.end >= since for s in t.segments)]
    return trs, [replace(s, start=max(s.start, since)) for t in trs for s in t.segments if s.end >= since]


def _by(segs: list[Segment], key: Any) -> dict[str, dict[str, float]]:
    groups: dict[str, list[float]] = collections.defaultdict(list)
    for s in segs:
        groups[key(s)].append(s.length)
    return dict(sorted(((k, dist(v)) for k, v in groups.items()), key=lambda kv: -kv[1]["total"]))


def _variants(trs: list[ItemTrace]) -> list[dict[str, Any]]:
    c = collections.Counter(" > ".join(t.path) for t in trs if t.path and ends_item(t.item_type, t.path[-1]))
    ex: dict[str, str] = {}
    for t in trs:
        ex.setdefault(" > ".join(t.path), t.item_id)
    return [{"path": p, "n": n, "example": ex[p]} for p, n in c.most_common(15)]


def _rework(trs: list[ItemTrace]) -> dict[str, int]:
    c: collections.Counter[str] = collections.Counter()
    for t in trs:
        for a, b in zip(t.path, t.path[1:]):
            if b in ("queued", "checking") and a in ("checking", "checked", "waiting"):
                c[f"{a} > {b}"] += 1
    return dict(c.most_common())


def _refusals(trs: list[ItemTrace]) -> list[dict[str, Any]]:
    c: collections.Counter[str] = collections.Counter()
    ex: dict[str, str] = {}
    for t in trs:
        for ev in t.events:
            if ev.get("to_state") == "rejected" and ev.get("from_state") != "rejected":
                k = ev.get("cause") or "unattributed"
                c[k] += 1
                ex.setdefault(k, t.item_id)
    return [{"cause": k, "n": n, "example": ex[k]} for k, n in c.most_common()]


def _throughput(trs: list[ItemTrace]) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = collections.defaultdict(lambda: collections.Counter())
    for t in trs:
        last = t.events[-1]
        if ends_item(t.item_type, str(last.get("to_state") or "")):
            out[iso_at(float(last["at"]))[:10]][last["to_state"]] += 1
    return {d: dict(v) for d, v in sorted(out.items())}


def _outliers(segs: list[Segment], k: int = 10) -> list[dict[str, Any]]:
    return [{"item": s.item_id, "state": s.state, "length": s.length, "cause": s.cause, "start": s.start}
            for s in sorted(segs, key=lambda s: -s.length)[:k]]


def coverage(segs: list[Segment]) -> dict[str, float]:
    """The share of segment time with a cause: `own` (written with the event, or with an offline-log line the store has
    not taken yet), `own_or_exact` (and the backfill's exact rows) and `with_derived` (its inferred ones too)."""
    total = sum(s.length for s in segs) or 1.0
    tiers = {"own": ("own", "file"), "own_or_exact": ("own", "file", "exact"),
             "with_derived": ("own", "file", "exact", "inferred")}  # file: an undrained line's own cause
    out = {k: sum(s.length for s in segs if s.cause and s.origin in v) / total for k, v in tiers.items()}
    return {**out, "seconds": total}


def hum(s: float) -> str:
    if s != s:
        return "-"
    return f"{s:.0f}s" if s < 90 else f"{s / 60:.1f}m" if s < 5400 else f"{s / 3600:.1f}h" if s < 172800 else f"{s / 86400:.1f}d"


def render_text(rep: dict[str, Any]) -> str:
    """The report as text tables."""
    cov = rep["coverage"]
    lines = [f"{rep['items']} items; attribution coverage: {cov['own']:.0%} own, {cov['own_or_exact']:.0%} own or "
             f"exact-derived, {cov['with_derived']:.0%} with inferred ({hum(cov['seconds'])} of state time)", "",
             "time per state:  state  n  p50  p90  max  total"]
    lines += [f"  {k}  {d['n']}  {hum(d['p50'])}  {hum(d['p90'])}  {hum(d['max'])}  {hum(d['total'])}"
              for k, d in rep["states"].items()]
    lines += ["", "waiting by cause:"] + [f"  {k}  {d['n']}  p50 {hum(d['p50'])}  total {hum(d['total'])}"
                                          for k, d in list(rep["waiting_by_cause"].items())[:15]]
    lines += ["", "refusals:"] + [f"  {r['n']}  {r['cause']}  e.g. {r['example']}" for r in rep["refusals"]]
    lines += ["", "rework:"] + [f"  {k}  {n}" for k, n in rep["rework"].items()]
    lines += ["", "path variants:"] + [f"  {v['n']}  {v['path']}  e.g. {v['example']}" for v in rep["variants"][:10]]
    lines += ["", "throughput per day:"] + [f"  {d}  " + " ".join(f"{k} {n}" for k, n in sorted(v.items()))
                                            for d, v in rep["throughput"].items()]
    lines += _to_end_lines(rep.get("to_end_by_kind"))
    lines += ["", "outliers:"] + [f"  {hum(o['length'])}  {o['state']}  {o['cause'] or 'unattributed'}  {o['item']}"
                                  for o in rep["outliers"]]
    return "\n".join(lines)


def _to_end_lines(by_kind: Optional[dict[str, dict[str, float]]]) -> list[str]:
    """The escalations' time to end per kind (none: no lines)."""
    if not by_kind:
        return []
    return ["", "time to end by kind:  kind -> end  n  p50  p90  max"] + [
        f"  {k}  {d['n']}  {hum(d['p50'])}  {hum(d['p90'])}  {hum(d['max'])}" for k, d in by_kind.items()]


def render_tsv(trs_segments: Iterable[Segment]) -> str:
    """Every segment as a TSV row (the analysis' raw export)."""
    rows = ["item_type\titem_id\tstate\tstart\tend\tseconds\tcause\tactor\torigin"]
    rows += [f"{s.item_type}\t{s.item_id}\t{s.state}\t{iso_at(s.start)}\t{iso_at(s.end)}\t{s.length:.0f}\t"
             f"{s.cause or ''}\t{s.actor or ''}\t{s.origin}" for s in trs_segments]
    return "\n".join(rows) + "\n"


def top_sinks(rep: dict[str, Any], k: int = 3) -> list[str]:
    """The k biggest time sinks (state: cause, by total time), as lines for the owner's weekly summary."""
    sinks = sorted(rep["waiting_by_cause"].items(), key=lambda kv: -kv[1]["total"])[:k]
    return [f"{name}: {hum(d['total'])} over {d['n']} stays (p90 {hum(d['p90'])})" for name, d in sinks]


def event_line(ev: dict[str, Any], prev: Optional[float]) -> str:
    """One event of a timeline: when, the move, the time spent in the state it left, why (and how it is known), who."""
    at = float(ev["at"])
    took = f"after {hum(at - prev)}" if prev is not None else ""
    move = f"{ev.get('from_state') or '-'} -> {ev.get('to_state') or '-'}"
    why = (ev.get("cause") or "?") + ("" if ev.get("origin") in (None, "own") else f" ({ev['origin']})")
    return (f"  {iso_at(at)}  {move:<24} {took:<12} {why:<26} {ev.get('actor') or '':<20} "
            f"{str(ev.get('text') or '')[:120]}")


def history_lines(tr: ItemTrace, item: Optional[dict[str, Any]], now: float) -> list[str]:
    """One item's timeline: each event with the time spent in the state it left, its cause, actor and text; then the
    commits its data links and the stay still open."""
    out = [f"{tr.item_type} {tr.item_id}" + (f": now {item['state']}" if item else "")]
    out += [event_line(ev, float(tr.events[i - 1]["at"]) if i else None) for i, ev in enumerate(tr.events)]
    return out + _commit_lines((item or {}).get("data") or {}) + _open_lines(tr, now)


def _commit_lines(d: dict[str, Any]) -> list[str]:
    shas = [f"{k} {str(d[k])[:12]}" for k in ("head", "tip", "landed_sha", "base") if d.get(k)]
    return ["  commits: " + ", ".join(shas)] if shas else []


def _open_lines(tr: ItemTrace, now: float) -> list[str]:
    open_seg = [s for s in tr.segments if s.end == now]
    return [f"  open: {open_seg[-1].state} for {hum(open_seg[-1].length)}"] if open_seg else []


def names(it: dict[str, Any], ref: str) -> bool:
    """Whether item `it` is what `ref` names: its id, branch, worktree or a sha (head, tip, landed), or part of one."""
    d = it.get("data") or {}
    vals = [it["id"]] + [d.get(k) for k in ("branch", "worktree", "head", "tip", "landed_sha")]
    return any(isinstance(v, str) and ref in v for v in vals if v)


def find_items(items: dict[str, list[dict[str, Any]]], ref: str) -> list[tuple[str, dict[str, Any]]]:
    """The items a reference names (names); the one whose id it is exactly, when there is one."""
    hits = [(typ, it) for typ, its in items.items() for it in its if names(it, ref)]
    return [h for h in hits if h[1]["id"] == ref] or hits


def as_json(rep: Any) -> str:
    return json.dumps(rep, indent=1, default=str, allow_nan=True)
