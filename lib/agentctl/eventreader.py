# agentctl, part `eventreader`: THE ONE EVENT READER -- an EventQuery's rows from the store's indexed query, an older store's whole feed and backfill merged here, this host's undrained offline log merged in
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.
#
# `history`, `analyze` and the weekly summary read events here (eventtrace.py, THE EVENT MODEL). A store of API version
# 2 (storecalls.py) answers the query itself, through its indexes: one item's rows, or a type's items over a window --
# not its whole feed plus its whole backfill, 22 MB over the laptop's tunnel for every analyze on the 2026-11-26 copy.
# A store older than the call (ci-runner's pin may hold an older revision) refuses it as one it lacks: then the reader
# takes the old path, the whole feed paged and the backfill, and merges them here -- the same rows.

from typing import Any, Iterable

from agentctl.checkrows import change_runs, with_fetched
from agentctl.eventtrace import EventQuery, merge, with_file_rows
from agentctl.store import StoreLacksCall


def read_events(store: Any, q: EventQuery, pending: Iterable[dict[str, Any]] = ()) -> list[dict[str, Any]]:
    """The rows q reads, merged by provenance: the store's (own, exact, inferred, none) and `pending`, this host's
    offline-log lines the store has not taken yet (file; filelog.undrained_rows), those q admits."""
    try:
        rows = store.trace_events(q.wire())
    except StoreLacksCall:  # a store before the call: its whole feed and backfill, merged here
        rows = older_store_rows(store, q)
    return with_file_rows(rows, [r for r in pending if q.admits(r)])


def older_store_rows(store: Any, q: EventQuery) -> list[dict[str, Any]]:
    """q's rows from a store with no trace_events: one item's events or the whole feed, the backfill merged in."""
    derived = derived_of(store)
    if q.item_id:
        mine = [d for d in derived.get("derived") or () if (d["item_type"], d["item_id"]) == (q.item_type, q.item_id)]
        return merge(store.item_events(q.item_type, q.item_id), {"backfill": derived.get("backfill") or [], "derived": mine})
    return [r for r in merge(all_events(store), derived) if q.admits(r)]


def all_events(store: Any, page: int = 5000) -> list[dict[str, Any]]:
    """Every event of the store, paged by its cursor."""
    out: list[dict[str, Any]] = []
    while True:
        rows = store.events(after=out[-1]["seq"] if out else 0, limit=page)
        out += rows
        if len(rows) < page:
            return out


def derived_of(store: Any) -> dict[str, Any]:
    """The backfill's rows; none from a store older than them (its API has no `derived`). A store that does not answer
    raises (review 2026-11-26: it read as "no backfill", and analyze reported a wrong coverage, not an error)."""
    try:
        return store.derived() or {}
    except StoreLacksCall:  # an older store server: the own columns alone
        return {}


def old_check_runs(store: Any) -> dict[str, Any]:
    """The old check runs' fetched rows (backfill.py), by run: the store's own call, else its whole backfill's."""
    try:
        return store.derived_check_runs() or {}
    except StoreLacksCall:
        return derived_of(store).get("check_runs") or {}


def check_runs_since(store: Any, since: float) -> list[tuple[str, dict[str, Any]]]:
    """(change id, run record) of the check runs decided at or after `since`, and the old runs the backfill fetched: the
    store's own call (the runs alone, 4 MB of change items not read), else a store older than it gives its items."""
    try:
        held = [(change, run) for change, run in store.change_check_runs(since)]
    except StoreLacksCall:
        return change_runs(store.items("change"), old_check_runs(store))
    return with_fetched(held, old_check_runs(store))
