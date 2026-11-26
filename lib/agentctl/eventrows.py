# agentctl, part `eventrows`: the store's event rows -- their schema migration, one row's insert, a move's event
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.
#
# THE ATTRIBUTION COLUMNS (2026-11-26, causes.py): `actor` (who moved it), `cause` (why, one word of causes.CAUSES) and
# `detail` (a short qualifier) were added to `events` by ALTER TABLE, so a store older than them migrates in place and
# its old rows keep NULL there (back-compatible: no row is rewritten). A move between two states needs a cause
# (checked_cause); a self-move (a reminder, an escalation) and a row no move wrote (an item opened, a lease archived)
# may carry one.

import inspect
import json
import sqlite3
from typing import Any, Callable

from agentctl.causes import checked_cause

ATTRIBUTION_COLUMNS = ("actor", "cause", "detail")
# the cause of an item's opening event (opened_event), by its type; a type not listed opens without one
OPENED_CAUSE = {"change": "submitted", "condition": "probe", "watch": "landed", "red": "ci-verdict",
                "branch-red": "ci-verdict", "batch": "batch", "hold": "owner"}


def migrate_events(c: sqlite3.Connection) -> None:
    """Add the attribution columns a store older than them lacks (each once; old rows stay NULL there)."""
    have = {r[1] for r in c.execute("PRAGMA table_info(events)")}
    for col in ATTRIBUTION_COLUMNS:
        if col not in have:
            try:
                c.execute(f"ALTER TABLE events ADD COLUMN {col} TEXT")
            except sqlite3.OperationalError as e:  # a second connection added it first
                if "duplicate column" not in str(e):
                    raise


def insert_event(c: sqlite3.Connection, ev: dict[str, Any]) -> None:
    """One row of the event feed (INSERT OR IGNORE: a repeated dedupe key says nothing twice). ev: at, kind, item_type,
    item_id, from_state, to_state, severity, audience, text, refs, dedupe, and optionally actor, cause, detail."""
    c.execute("INSERT OR IGNORE INTO events (at, kind, item_type, item_id, from_state, to_state, severity, audience, "
              "text, refs, dedupe, actor, cause, detail) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
              (ev["at"], ev["kind"], ev.get("item_type"), ev.get("item_id"), ev.get("from_state"), ev.get("to_state"),
               ev.get("severity") or "info", json.dumps(ev.get("audience") or {}), ev.get("text") or "",
               json.dumps(ev.get("refs") or {}), ev["dedupe"], ev.get("actor"), ev.get("cause"),
               (str(ev["detail"])[:300] if ev.get("detail") else None)))


def move_event(typ: str, iid: str, frm: str, to: str, version: int, escalate: bool, ev: dict[str, Any]) -> dict[str, Any]:
    """The row a move of item (typ, iid) from `frm` to `to` (its new version) writes: the caller's event completed with
    its kind, severity and dedupe defaults. A move between two states without a cause of CAUSES is MissingCause."""
    same = frm == to
    if not same or ev.get("cause"):
        checked_cause(ev.get("cause"))
    return {"kind": f"{typ}.{to}", "severity": "escalate" if same and escalate else "info",
            "dedupe": f"{typ}:{iid}:{to}:{version}", "audience": {}, "text": "", "refs": {},
            **{k: v for k, v in ev.items() if v}, "item_type": typ, "item_id": iid, "from_state": frm, "to_state": to}


def opened_event(typ: str, iid: str, state: str, at: float) -> dict[str, Any]:
    """The row an item's creation writes (`<type>.<state>`, from no state): its entry in the timeline, so a stay in its
    first state starts at an event (2026-11-26: a change's first `queued` stay, a condition's open, a watch's start
    were in no event). Info and no audience: it reaches no inbox."""
    return {"kind": f"{typ}.{state}", "severity": "info", "dedupe": f"{typ}:{iid}:opened", "audience": {},
            "text": f"{typ} {iid} opened: {state}", "refs": {}, "cause": OPENED_CAUSE.get(typ), "at": at,
            "item_type": typ, "item_id": iid, "from_state": None, "to_state": state}


def legacy_cause(method: str, args: list[Any], kwargs: dict[str, Any],
                 target: Callable[..., Any]) -> tuple[list[Any], dict[str, Any]]:
    """A transition from a client older than causes.py (no cause on its event) is written as cause `unattributed`
    rather than refused: during an upgrade the laptop and ci-runner run different revisions for minutes. This code's
    own moves always name one (tests/test_causes.py). `target`: the store method the call goes to; its own signature
    says where `event` is (review 2026-11-26: the shim wrote args[5], silently wrong once transition's order moved)."""
    if method != "transition":
        return args, kwargs
    args, kwargs = list(args), dict(kwargs)
    at = list(inspect.signature(target).parameters).index("event")
    ev = dict((args[at] if len(args) > at else kwargs.get("event")) or {})
    if not ev.get("cause"):
        ev["cause"] = "unattributed"
    if len(args) > at:
        args[at] = ev
    else:
        kwargs["event"] = ev
    return args, kwargs


def recorded_row(ev: dict[str, Any]) -> dict[str, Any]:
    """A file-held state's event (filelog.py) checked for the feed: its item, kind, dedupe and time are required, a move
    between two states names a cause of CAUSES (MissingCause otherwise), severity info (it reaches no inbox)."""
    for k in ("at", "kind", "item_type", "item_id", "to_state", "dedupe"):
        if ev.get(k) in (None, ""):
            raise ValueError(f"a recorded event needs {k}: {ev!r:.200}")
    if ev.get("from_state") != ev.get("to_state") or ev.get("cause"):
        checked_cause(ev.get("cause"))
    return {**ev, "severity": "info", "audience": {}, "refs": ev.get("refs") or {}}


def record_rows(open_db: Callable[[], sqlite3.Connection], rows: list[dict[str, Any]]) -> int:
    """File-held state's moves (filelog.py's offline log) into the feed in one transaction, idempotent by dedupe;
    the rows new to it. A row the feed refuses (recorded_row) refuses the batch: nothing written."""
    checked = [recorded_row(r) for r in rows]
    c = open_db()
    try:
        c.execute("BEGIN IMMEDIATE")
        before = c.total_changes
        for r in checked:
            insert_event(c, r)
        n = c.total_changes - before
        c.execute("COMMIT")
        return n
    finally:
        c.close()
