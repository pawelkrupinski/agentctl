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
import re
from dataclasses import dataclass, field, replace
from typing import Any, Iterable, Optional

from agentctl.timefmt import iso_at

TERMINAL = {"landed", "rejected", "cancelled", "superseded", "verified", "cleared", "ok", "red", "unknown", "closed",
            "released", "acked", "evicted"}  # the last three: file-held state (filelog.py)
SKIP_KINDS = re.compile(r"\.illegal$|^policy\.unapproved$")


@dataclass
class Segment:
    """A span of one item in one state: [start, end), why it was there (cause), who put it there, own or derived."""
    item_type: str
    item_id: str
    state: str
    start: float
    end: float
    cause: Optional[str]
    actor: Optional[str]
    origin: str  # own | exact | inferred | none

    @property
    def length(self) -> float:
        return max(0.0, self.end - self.start)


@dataclass
class ItemTrace:
    """One item's merged events and the segments they make."""
    item_type: str
    item_id: str
    events: list[dict[str, Any]] = field(default_factory=list)
    segments: list[Segment] = field(default_factory=list)
    path: list[str] = field(default_factory=list)


def merge(events: Iterable[dict[str, Any]], derived: Optional[dict[str, list[dict[str, Any]]]] = None) -> list[dict[str, Any]]:
    """The events with the backfill merged in: an event without its own cause takes the derived one (origin = its
    confidence), the derived opening events added; every row's `origin` says which (own | exact | inferred | none)."""
    derived = derived or {}
    back = {r["seq"]: r for r in derived.get("backfill") or ()}
    out = [_attributed(dict(ev), back.get(ev.get("seq"))) for ev in events]
    out += [{**d, "seq": None, "severity": "info", "text": f"(derived from {d['source']})", "refs": {},
             "origin": d["confidence"]} for d in derived.get("derived") or ()]
    out.sort(key=lambda e: (e.get("at") or 0, e.get("seq") or 0))
    return out


def _attributed(ev: dict[str, Any], b: Optional[dict[str, Any]]) -> dict[str, Any]:
    """Event ev with its origin, and the backfill row b's cause and actor where its own are missing."""
    b = b or {}
    if ev.get("cause"):
        ev["origin"] = "own"
    elif b.get("cause"):
        ev.update(cause=b["cause"], origin=b["confidence"], source=b["source"])
    else:
        ev["origin"] = "none"
    ev["actor"] = ev.get("actor") or b.get("actor")
    return ev


def traces(events: Iterable[dict[str, Any]], now: float, item_type: Optional[str] = None) -> dict[tuple[str, str], ItemTrace]:
    """Each item's trace from merged events (merge), its open stay ended at `now`."""
    by: dict[tuple[str, str], ItemTrace] = {}
    for ev in events:
        typ, iid = ev.get("item_type"), ev.get("item_id")
        if not typ or not iid or (item_type and typ != item_type) or SKIP_KINDS.search(ev.get("kind") or ""):
            continue
        by.setdefault((typ, iid), ItemTrace(typ, iid)).events.append(ev)
    for tr in by.values():
        _segments(tr, now)
    return by


class _Walk:
    """One item's events walked into its segments and path: the state it is in, since when, and the stay's cause."""

    def __init__(self, tr: ItemTrace) -> None:
        self.tr, self.cur, self.start = tr, "", 0.0
        self.said: tuple[Optional[str], Optional[str], str] = (None, None, "none")

    def first(self, ev: dict[str, Any]) -> None:
        """The first event: its from-state began then (an opening: its own state, and what it says)."""
        frm, to = ev.get("from_state"), str(ev.get("to_state") or "")
        self.cur, self.start = (to if frm is None else str(frm)), float(ev["at"])
        self.tr.path.append(self.cur)
        if frm is None:
            self.said = (ev.get("cause"), ev.get("actor"), ev.get("origin", "none"))
        else:
            self.step(ev)

    def step(self, ev: dict[str, Any]) -> None:
        """A later event: the stay so far a segment; a move a new state; a reminder with a cause a new attribution."""
        frm, to, at = ev.get("from_state"), str(ev.get("to_state") or ""), float(ev["at"])
        if at > self.start:
            self.tr.segments.append(Segment(self.tr.item_type, self.tr.item_id, self.cur, self.start, at, *self.said))
        if to != self.cur:
            self.tr.path.append(to)
        self.cur, self.start = to, at
        if ev.get("cause") or to != frm:  # a reminder without a cause keeps the stay's cause
            self.said = (ev.get("cause"), ev.get("actor"), ev.get("origin", "none"))

    def end(self, now: float) -> None:
        if self.cur and self.cur not in TERMINAL and now > self.start:
            self.tr.segments.append(Segment(self.tr.item_type, self.tr.item_id, self.cur, self.start, now, *self.said))


def _segments(tr: ItemTrace, now: float) -> None:
    w = _Walk(tr)
    for ev in tr.events:
        if not w.cur:
            w.first(ev)
        elif ev.get("from_state") is not None:  # an opening after the item began says nothing of its states
            w.step(ev)
    w.end(now)


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
            "throughput": _throughput(trs), "outliers": _outliers(segs), "coverage": coverage(segs)}


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
    c = collections.Counter(" > ".join(t.path) for t in trs if t.path and t.path[-1] in TERMINAL)
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
        if last.get("to_state") in TERMINAL:
            out[iso_at(float(last["at"]))[:10]][last["to_state"]] += 1
    return {d: dict(v) for d, v in sorted(out.items())}


def _outliers(segs: list[Segment], k: int = 10) -> list[dict[str, Any]]:
    return [{"item": s.item_id, "state": s.state, "length": s.length, "cause": s.cause, "start": s.start}
            for s in sorted(segs, key=lambda s: -s.length)[:k]]


def coverage(segs: list[Segment]) -> dict[str, float]:
    """The share of segment time with a cause: `own` (written with the event), `own_or_exact` (and the backfill's
    exact rows) and `with_derived` (its inferred ones too)."""
    total = sum(s.length for s in segs) or 1.0
    tiers = {"own": ("own",), "own_or_exact": ("own", "exact"), "with_derived": ("own", "exact", "inferred")}
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
    lines += ["", "outliers:"] + [f"  {hum(o['length'])}  {o['state']}  {o['cause'] or 'unattributed'}  {o['item']}"
                                  for o in rep["outliers"]]
    return "\n".join(lines)


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
