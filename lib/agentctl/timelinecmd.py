# agentctl, part `timelinecmd`: `agentctl history`, `agentctl analyze`, and maintain's backfill and weekly summary
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.
#
# The store and the clock are passed in (cli). On the laptop the store is ci-runner's (RemoteStore): its events, items
# and the backfill's derived rows come over the API; the backfill itself runs there (Store.backfill).

import argparse
import re
from typing import Any, Callable

from agentctl.checkrows import change_rows_lines, change_runs, rows_lines, rows_report
from agentctl.timeline import (analyze, as_json, find_items, history_lines, merge, render_text, render_tsv, top_sinks,
                               traces)

ITEM_TYPES = ("change", "red", "watch", "batch", "condition", "hold", "revert", "issue", "branch-red",
              "gov-ticket", "inbox", "entry", "sbt-server")  # the last four: file-held state (filelog.py)
WEEK = 7 * 86400
HISTORY_MAX = 5  # items one `history` prints at most


def parse_since(text: str) -> float:
    """Seconds of `7d`, `36h`, `90m`, `3600`."""
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([smhd]?)\s*", str(text))
    if not m:
        raise argparse.ArgumentTypeError(f"not a duration: {text!r} (7d, 36h, 90m)")
    return float(m.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]


def all_events(store: Any, page: int = 5000) -> list[dict[str, Any]]:
    """Every event of the store, paged by its cursor."""
    out: list[dict[str, Any]] = []
    while True:
        rows = store.events(after=out[-1]["seq"] if out else 0, limit=page)
        out += rows
        if len(rows) < page:
            return out


def derived_of(store: Any) -> dict[str, list[dict[str, Any]]]:
    """The backfill's rows; none from a store older than them (its API has no `derived`)."""
    try:
        return store.derived() or {}
    except Exception:  # an older store server: the own columns alone
        return {}


def item_trace(store: Any, derived: dict[str, list[dict[str, Any]]], typ: str, iid: str, now: float) -> Any:
    """Item (typ, iid)'s trace: its events, the backfill's rows for it merged in."""
    mine = [d for d in derived.get("derived") or () if (d["item_type"], d["item_id"]) == (typ, iid)]
    evs = merge(store.item_events(typ, iid), {"backfill": derived.get("backfill") or [], "derived": mine})
    return traces(evs, now).get((typ, iid))


def traced(store: Any, hits: list[tuple[str, dict[str, Any]]], now: float) -> list[tuple[dict[str, Any], Any]]:
    """(item, its trace) of each hit that has events."""
    derived = derived_of(store)
    shown = [(it, item_trace(store, derived, typ, it["id"], now)) for typ, it in hits]
    return [(it, tr) for it, tr in shown if tr is not None]


def _history_doc(it: dict[str, Any], tr: Any) -> dict[str, Any]:
    return {"type": tr.item_type, "id": it["id"], "state": it["state"], "events": tr.events,
            "segments": [vars(s) for s in tr.segments]}


def cmd_history(a: argparse.Namespace, store: Any, now: Callable[[], float]) -> int:
    hits = find_items({t: store.items(t) for t in ITEM_TYPES}, a.ref)
    if not hits:
        print(f"history: nothing matches {a.ref!r} (a change, red or watch id, a sha, a branch or worktree)")
        return 1
    t = now()
    shown = traced(store, hits[:HISTORY_MAX], t)
    if a.json:
        print(as_json([_history_doc(it, tr) for it, tr in shown]))
        return 0
    print("\n".join(line for it, tr in shown
                    for line in history_lines(tr, it, t) + change_rows_lines((it.get("data") or {}).get("check_runs") or ())))
    if len(hits) > HISTORY_MAX:
        print(f"  ... and {len(hits) - HISTORY_MAX} more matching items")
    return 0


def cmd_analyze(a: argparse.Namespace, store: Any, now: Callable[[], float]) -> int:
    t = now()
    derived = derived_of(store)
    events = merge(all_events(store), derived)
    since = t - a.since if a.since else 0.0
    if a.tsv:
        trs = traces(events, t, a.type).values()
        print(render_tsv(s for tr in trs for s in tr.segments if s.end >= since), end="")
        return 0
    rep = analyze(events, t, since, a.type)
    if a.type == "change":  # the CI rows the check time went to (checkrows.py)
        rep["check_rows"] = rows_report(change_runs(store.items("change"), derived.get("check_runs")), since)
    print(as_json(rep) if a.json else "\n".join([render_text(rep)] + (
        ["", *rows_lines(rep["check_rows"])] if "check_rows" in rep else [])))
    return 0


BACKFILL_EVERY = 3600  # s between two backfill passes of the server's tick


def timeline_step(store: Any, now: float, write: Callable[[str, list[str]], Any],
                  fetch_jobs: Callable[[str], list[dict[str, Any]]] | None = None,
                  pause: Callable[[float], Any] = lambda s: None) -> list[str]:
    """The server's tick, on the live store in-process: the backfill (idempotent) at most hourly -- with the rows of
    the old check runs (fetch_jobs: a run's jobs from GitHub, paced by `pause`) --, and the weekly summary of the top
    time sinks to the owner (write: the inbox). Its lines."""
    out = []
    if now - float(store.meta_get("backfill_at", 0) or 0) >= BACKFILL_EVERY:
        n = store.backfill()
        store.meta_set("backfill_at", now)
        runs = store.backfill_check_runs(fetch_jobs, now, pause) if fetch_jobs else 0
        out.append(f"backfill: {n.get('attributed', 0)} attribution(s), {n.get('opened', 0)} opening event(s) derived"
                   + (f", {runs} old check run(s)' rows fetched" if runs else ""))
    said = weekly_time_sinks(store, now, write)
    return out + ([said] if said else [])


def weekly_time_sinks(store: Any, now: float, write: Callable[[str, list[str]], Any]) -> str | None:
    """Once a week (the store's meta `time_sinks_at`): the top 3 time sinks of the last 7 days to the owner's inbox."""
    last = float(store.meta_get("time_sinks_at", 0) or 0)
    if now - last < WEEK:
        return None
    rep = analyze(merge(all_events(store), derived_of(store)), now, now - WEEK)
    sinks = top_sinks(rep)
    cov = rep["coverage"]
    line = (f"TIME SINKS (7 d): {'; '.join(sinks) or 'none'} -- attribution {cov['own']:.0%} own, "
            f"{cov['with_derived']:.0%} with derived (`agentctl analyze --since 7d`)")
    write(line, sinks)
    store.meta_set("time_sinks_at", now)
    return line


def add_timeline_commands(sub: Any) -> None:
    h = sub.add_parser("history", help="one item's timeline: each state's duration, cause, actor, its commits")
    h.add_argument("ref", help="a change, red or watch id (or a unique part), a sha, a branch or worktree")
    h.add_argument("--json", action="store_true")
    an = sub.add_parser("analyze", help="the queue's time sinks: time per state, variants, rework, refusals, waiting "
                                        "by cause, throughput, outliers, attribution coverage")
    an.add_argument("--since", type=parse_since, default=parse_since("7d"), help="a window (7d, 36h; 0: all)")
    an.add_argument("--type", default="change", choices=ITEM_TYPES)
    fmt = an.add_mutually_exclusive_group()
    fmt.add_argument("--json", action="store_true")
    fmt.add_argument("--tsv", action="store_true", help="every state segment as a TSV row")
