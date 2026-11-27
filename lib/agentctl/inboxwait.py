# agentctl, part `inboxwait`: `agentctl inbox wait`: the entries a caller waits for, read from the alert index and printed as they arrive
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import os
import time
from typing import Any
from agentctl.clock import wall_epoch
from agentctl.watchknobs import knob_inbox_poll
from agentctl.inboxentry import inbox_dir, inbox_entries, inbox_kind
from agentctl.inbox import WAIT_KINDS_DEFAULT, entry_line
from agentctl.alertroute import alert_audience
from agentctl.alerts import alert_index, alert_lock


def inbox_wait(kinds=None, since_ack=False, timeout=0, verbose=False, forever=False, audience="owner"):
    """THE NOTIFIER: block until an unread entry of `kinds` exists, print it (every one there is), exit 0; 2 at the
    timeout. kinds None: WAIT_KINDS_DEFAULT, and every alert addressed to the owner whatever its kind (an issue's
    escalation is OTHER, and it is the owner's to act on).
    UNREAD IS PER ENTRY (2026-11-19: issue 55799d's owner alert sat unread a day): an entry not acknowledged wakes it,
    whatever was acknowledged after it. `--since-ack` once meant "newer than the newest ack", and the orchestrator acks
    one id at a time, so every later-acked entry hid an older unread one; it is accepted and means just that now. So
    every arming prints the whole unread backlog first: a re-armed watcher (a compaction, a restart) never misses it.
    Cheap: one stat() of the inbox directory (and of acked/) a second, the entries read again only when either changed
    (a new entry is an os.replace into the directory, an ack a file in acked/). No forge, no git.
    forever: never exits; streams each such entry once, one line each, as it appears (one background task a session).
    audience (THE ALERT MODEL, alerts.py): only entries addressed to it -- the owner's by default; "all" every one. An
    alert updated in place is streamed again only when it is SURFACED again (it became the owner's, a new tier)."""
    d = inbox_dir()
    os.makedirs(d, exist_ok=True)
    owner_any = kinds is None
    want, auds = wanted_kinds(kinds), wanted_audiences(audience)
    with alert_lock(d):
        alert_index(d)  # the first use migrates the backlog into the model: no flood
    poll = knob_inbox_poll()
    t0, seen, printed = wall_epoch(), None, set()
    while True:
        stamp = inbox_stamp(d)
        if stamp != seen:
            seen = stamp
            hits = unread_hits(want, owner_any, auds)
            if forever:
                stream_hits(hits, printed, verbose)
            elif hits:
                return inbox_routed(hits, verbose)
        if timeout and wall_epoch() - t0 >= timeout and not forever:
            print(f"inbox: no unread {','.join(sorted(want))} entry within {int(timeout)} s")
            return 2
        time.sleep(poll)  # wall: waits on and times real processes and threads


def wanted_kinds(kinds: Any) -> set[str]:
    """The kinds `inbox wait --kinds` names, upper-cased with `_` for `-` (WAIT_KINDS_DEFAULT when none)."""
    return {k.strip().upper().replace("-", "_") for k in (kinds or WAIT_KINDS_DEFAULT).split(",") if k.strip()}


def wanted_audiences(audience: Any) -> set[str] | None:
    """The audiences `--audience` names, None for every one."""
    return None if audience in (None, "", "all") else {x.strip() for x in str(audience).split(",") if x.strip()}


def inbox_routed(hits: Any, verbose: bool) -> int:
    """Print every unread hit, and say what to do with them: the wait's exit 0."""
    for e in hits:
        print_inbox_entry(e, verbose)
    print(f"inbox: {len(hits)} to route; then `~/bin/agentctl inbox <id> --ack` and run `inbox wait` again")
    return 0


def inbox_stamp(d: str) -> tuple[int, int] | None:
    """What changed in the inbox directory: its mtime and its acked/ directory's (None when it cannot be read)."""
    try:
        return (os.stat(d).st_mtime_ns, os.stat(os.path.join(d, "acked")).st_mtime_ns
                if os.path.isdir(os.path.join(d, "acked")) else 0)
    except OSError:
        return None


def unread_hits(want: Any, owner_any: Any, auds: Any) -> list[dict[str, Any]]:
    """The unread entries of the audiences `auds` (None: all) that are of a wanted kind -- or, when any kind is wanted
    (`owner_any`), addressed to the owner."""
    return [e for e in inbox_entries() if not e["read"]
            and (auds is None or alert_audience(e) in auds)
            and ((e.get("kind") or inbox_kind(e.get("verdict"), e.get("final", True))) in want
                 or (owner_any and alert_audience(e) == "owner"))]


def stream_hits(hits: Any, printed: Any, verbose: bool) -> None:
    """Print each hit once, as it appears (`printed`: the (id, surfaced) pairs already said)."""
    for e in hits:
        mark = (e["id"], e.get("surfaced") or 1)
        if mark not in printed:
            printed.add(mark)
            print_inbox_entry(e, verbose, flush=True)


def print_inbox_entry(e: Any, verbose: bool, flush: bool=False) -> None:
    """The entry's line, and its tail under --verbose."""
    print(entry_line(e), flush=flush)
    for line in (e.get("tail") or []) if verbose else []:
        print(f"    {line}", flush=flush)
