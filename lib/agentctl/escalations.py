# agentctl, part `escalations`: THE ESCALATION DISPATCHER: an escalated condition the table gives an agent is offered, leased and started as a `claude --bg` fixer, watched, and closed by its condition's own probe -- on the red dispatcher's offers and leases
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.
#
# THE OWNER (2026-11-26): "can agentctl just kick off an agent when sth escalates?" The laptop's sync runs one pass a
# cycle (syncsteps.py `escalation dispatcher`, beside the red dispatcher), on the SAME machinery as reds, no second one:
# - OFFER: an open condition past its kind's bound (escalationtable.py) whose route is an agent gets ONE open offer
#   (the store's offers table, kind `escalation`, item `condition:<id>`: one offer per (kind, subject)). An owner-only
#   kind is never offered, leased or started.
# - LEASE: the offer names the lease `escalation/<kind>`: taking it is atomic with closing the offer, and a held lease
#   refuses a second taker, so ONE LIVE FIXER PER KIND; at most ESCALATION_FIXERS (2) alive over every kind.
# - DISPATCH: a fresh worktree of the row's repo and a `claude --bg` session with the row's model, its brief the
#   condition's text, its history (`agentctl history <id>`) and the rules. A repo claude does not trust is NOT
#   DISPATCHABLE: said on the offer (no try counted, no retry storm); the owner is asked once by fixersession.py
#   trust_asks, which reads this table's repos too.
# - LIVENESS AND CLOSURE: the lease is renewed while its session works, stopped at the row's wall time. The escalation
#   closes WHEN ITS CONDITION CLOSES (its own probe), never on the agent's word: a session that ends with the condition
#   still open, times out, never starts or loses its lease is a FAILED TRY; tries back off (RETRY_BACKOFF doubled) and,
#   once the row's tries are spent, ONE owner entry says what was tried (WAITING ON YOU, under the condition's item).
# - EVENTS: every offer, dispatch, take, failed try, give-up and clear is a move of the file-held item `escalation`
#   (`<kind>/<condition id>`, filelog.py: drained into the store), with its actor and cause, so `agentctl analyze
#   --type escalation` measures the time to clear per kind.
# The launcher (escalationsession.py: worktrees, `claude --bg`, `claude agents`, trust) is passed in by the
# composition root; the tests pass a fake one.

from typing import Any, Callable, Optional, Protocol
from agentctl.escalationtable import (
    ESCALATION_FIXERS, RETRY_BACKOFF, TO_AGENT, EscalationRoute, escalation_kind, escalation_route)
from agentctl.statefile import json_object
from agentctl.statemachines import StaleVersion
from agentctl.filelog import file_event, note_events
from agentctl.inboxentry import inbox_write

LEASE_PREFIX = "escalation/"
OFFER_KIND = "escalation"
ITEM_TYPE = "escalation"
LEASE_TTL = 15 * 60  # s: renewed every sync cycle while the session works; a laptop gone silent lets it lapse
OFFER_PRIORITY = 3


class EscalationLauncher(Protocol):
    """What the dispatcher needs from the world (escalationsession.py's SessionLauncher; the tests' fake)."""

    def host(self) -> str: ...

    def blocker(self) -> Optional[str]: ...

    def repo_path(self, repo: str) -> Optional[str]: ...

    def trust_gap(self, path: str) -> Optional[str]: ...

    def start(self, route: EscalationRoute, kind: str, condition: dict[str, Any], prompt: str,
              repo: str) -> tuple[Optional[dict[str, Any]], str]: ...

    def session_over(self, session: str) -> Optional[bool]: ...

    def stop(self, session: str) -> None: ...


def escalation_pass(store: Any, t: float, launcher: EscalationLauncher) -> list[str]:
    """ONE PASS of the escalation dispatcher: the fixers watched (cleared, failed, renewed), the escalations offered or
    handed to the owner, the offers dispatched. Returns what it did."""
    acts = watch_fixers(store, t, launcher, open_conditions_by_id(store))
    conditions = open_conditions_by_id(store)  # read again: a try that just ended wrote its record
    acts += offer_escalations(store, t, conditions)
    acts += dispatch_offers(store, t, launcher, conditions)
    return acts


def open_conditions_by_id(store: Any) -> dict[str, Any]:
    return {it["id"]: it for it in store.items("condition", ["open"])}


# ---------------------------------------------------------------- the events
def say(item: str, frm: Optional[str], to: str, cause: str, t: float, detail: str, actor: Optional[str] = None) -> None:
    """One move of escalation `item` (`<kind>/<condition id>`) as an event of the offline log."""
    row = file_event(ITEM_TYPE, item, frm, to, cause, at=t, detail=detail, text=f"escalation {item}: {detail}"[:300])
    if actor:
        row["actor"] = actor
    note_events([row])


def meta_of(row: dict[str, Any]) -> dict[str, Any]:
    """A lease row's meta (a JSON text from the file store, an object over the wire)."""
    m = row.get("meta")
    return json_object(m) if isinstance(m, str) or m is None else dict(m)


def item_of(kind: str, cid: str) -> str:
    return f"{kind}/{cid}"


# ---------------------------------------------------------------- the condition's escalation record
def record_of(it: dict[str, Any]) -> dict[str, Any]:
    """The escalation record on a condition's data: its tries, the next try's time, the owner told, its last state."""
    return dict((it.get("data") or {}).get("escalation") or {})


def write_record(store: Any, cid: str, change: Callable[[dict[str, Any]], dict[str, Any]]) -> Optional[dict[str, Any]]:
    """change(record) applied to condition cid's escalation record, compare-and-set (re-read once when another writer
    moved it). The record written, or None when the condition is gone or kept moving."""
    for _ in range(3):
        it = store.item("condition", cid)
        if not it or it["state"] != "open":
            return None
        rec = change(record_of(it))
        try:
            store.update_item("condition", cid, it["version"], {"escalation": rec})
            return rec
        except StaleVersion:
            continue
    return None


def moved_to(state: str, **fields: Any) -> Callable[[dict[str, Any]], dict[str, Any]]:
    return lambda rec: {**rec, **fields, "state": state}


# ---------------------------------------------------------------- watching the fixers
def watch_fixers(store: Any, t: float, launcher: EscalationLauncher, conditions: dict[str, Any]) -> list[str]:
    """Every escalation lease: its condition closed -> CLEARED (the session stopped, the lease released); its session
    over, its wall time spent or its lease lapsed with the condition open -> a FAILED TRY; else renewed."""
    acts = []
    for row in store.list(LEASE_PREFIX):
        seen = watched(row, t, launcher.host())
        line = watch_one(store, t, launcher, conditions, row, *seen) if seen else None
        if line:
            acts.append(line)
    return acts


def watched(row: Optional[dict[str, Any]], t: float, host: str) -> Optional[tuple[dict[str, Any], bool]]:
    """(its meta, live?) of a lease this machine judges -- a try not yet ended, live here or lapsed -- else None."""
    if not row:
        return None
    m = meta_of(row)
    if not m.get("condition") or m.get("outcome") or m.get("ended"):
        return None
    live = bool(row.get("holder")) and float(row["expires_at"]) > t
    return None if live and m.get("host") not in (None, host) else (m, live)  # another machine's: judged there


def watch_one(store: Any, t: float, launcher: EscalationLauncher, conditions: dict[str, Any], row: dict[str, Any],
              m: dict[str, Any], live: bool) -> Optional[str]:
    over = launcher.session_over(m["session"]) if live and m.get("session") and m["condition"] in conditions else None
    ended = try_ended(t, conditions, m, live, over)
    if ended:
        return end_try(store, t, launcher, row, m, *ended)
    if over is False:
        store.renew(row["name"], row["epoch"], row["secret"], f"working {int(t // 60)}", LEASE_TTL)
        if not m.get("taken"):
            taken(store, t, row, m)
    return None


def try_ended(t: float, conditions: dict[str, Any], m: dict[str, Any], live: bool,
              over: Optional[bool]) -> Optional[tuple[str, str]]:
    """(outcome, why) when the try is over -- its condition closed, its lease lapsed, its wall time spent, its session
    ended (over: its session's state) -- else None (at work, or `claude agents` unreadable: no verdict)."""
    if m["condition"] not in conditions:
        return "CLEARED", "its condition closed (its probe passed)"
    if not live:
        return "LAPSED", "its lease lapsed with the condition open"
    if t - float(m.get("started") or t) >= float(m.get("runtime") or 0):
        return "TIMED OUT", f"its {int(m['runtime']) // 60} min wall time is spent"
    if not m.get("session") or over:
        return "ENDED", "its session ended with the condition still open"
    return None


def taken(store: Any, t: float, row: dict[str, Any], m: dict[str, Any]) -> None:
    """The session is at work: the take, said once, by the session itself."""
    store.set_meta(row["name"], row["epoch"], row["secret"], {"taken": t})
    say(item_of(m["kind"], m["condition"]), "dispatched", "working", "agent", t, f"session {m['session']} took it",
        actor=f"session:{m['session']}")
    write_record(store, m["condition"], moved_to("working"))


def end_try(store: Any, t: float, launcher: EscalationLauncher, row: dict[str, Any], m: dict[str, Any], outcome: str,
            why: str) -> str:
    """A try ends: its session stopped, its lease released (or marked ended, when it lapsed), and -- unless its condition
    cleared -- counted as failed, the next one backed off."""
    cid, kind, session = m["condition"], m["kind"], m.get("session")
    if session:
        launcher.stop(session)
    if row.get("holder"):
        store.release(row["name"], row["epoch"], row["secret"], outcome=f"{outcome}: {why}")
    else:
        store.set_meta(row["name"], row["epoch"], row["secret"], {"ended": f"{outcome}: {why}"})
    item, frm = item_of(kind, cid), "working" if m.get("taken") else "dispatched"
    if outcome == "CLEARED":
        say(item, frm, "cleared", "probe", t, f"try {m.get('try')}: {why}")
        return f"escalation {kind} ({m.get('key')}): CLEARED after try {m.get('try')}"
    tried = {"try": m.get("try"), "session": session, "worktree": m.get("worktree"), "started": m.get("started"),
             "ended": t, "outcome": f"{outcome}: {why}"}
    rec = write_record(store, cid, lambda r: failed_try(r, tried, t))
    say(item, frm, "offered", "work-ended", t, f"try {m.get('try')} failed: {outcome}: {why}")
    return (f"escalation {kind} ({m.get('key')}): try {m.get('try')} FAILED ({outcome}: {why}); "
            f"next not before {int(float((rec or {}).get('next_after') or t) - t) // 60} min")


def failed_try(rec: dict[str, Any], tried: dict[str, Any], t: float) -> dict[str, Any]:
    tries = list(rec.get("tries") or []) + [tried]
    return {**rec, "tries": tries, "state": "offered", "next_after": t + RETRY_BACKOFF * 2 ** (len(tries) - 1)}


# ---------------------------------------------------------------- offering
def escalated(it: dict[str, Any], route: EscalationRoute, t: float) -> bool:
    """A condition is an escalation once open past its route's bound."""
    d = it.get("data") or {}
    return t - float(d.get("opened_at") or it.get("entered_at") or t) >= route.after


def offer_escalations(store: Any, t: float, conditions: dict[str, Any]) -> list[str]:
    """Every escalated condition an agent may take gets its one open offer (or, its tries spent, goes to the owner);
    an offer whose condition closed or turned the owner's is closed."""
    acts: list[str] = []
    offers = {o["item"]: o for o in store.offers(OFFER_KIND)}
    for item, off in offers.items():
        cid = item.split(":", 1)[1]
        it = conditions.get(cid)
        if it is None:
            store.close_offer(item, "cleared")
            say(item_of(off["tier"], cid), (off.get("picture") or {}).get("state") or "offered", "cleared", "probe", t,
                "its condition closed before a fixer took it")
            acts.append(f"escalation {off['tier']}: cleared while offered")
        elif escalation_route(it["data"]).mode != TO_AGENT:
            store.close_offer(item, "owner-only")
    for cid, it in conditions.items():
        line = offer_one(store, t, it, offers.get(f"condition:{cid}"))
        if line:
            acts.append(line)
    return acts


def offer_one(store: Any, t: float, it: dict[str, Any], offer: Optional[dict[str, Any]]) -> Optional[str]:
    d, cid = it.get("data") or {}, it["id"]
    route, kind, rec = escalation_route(d), escalation_kind(d), record_of(it)
    if not offerable(store, t, it, route, rec):
        return None
    tries = rec.get("tries") or []
    if len(tries) >= route.tries:
        return give_to_owner(store, t, it, route, kind, rec, offer)
    picture = {"kind": kind, "key": d.get("key"), "condition": cid, "try": len(tries) + 1,
               "next_after": rec.get("next_after"), "state": rec.get("state") or "offered"}
    if not offer or offer.get("picture") != picture:
        store.offer(OFFER_KIND, f"condition:{cid}", LEASE_PREFIX + kind, kind, OFFER_PRIORITY, t + route.runtime, picture)
    if rec.get("state"):
        return None
    write_record(store, cid, moved_to("offered", offered_at=t))
    say(item_of(kind, cid), None, "offered", "timeout", t, f"{d.get('kind')} open past its bound: {d.get('text')}")
    return f"escalation {kind} ({d.get('key')}): offered to an agent"


def offerable(store: Any, t: float, it: dict[str, Any], route: EscalationRoute, rec: dict[str, Any]) -> bool:
    """An agent's kind, escalated, not yet the owner's, and no fixer of it at work on it now."""
    return route.mode == TO_AGENT and not rec.get("owner_told") and escalated(it, route, t) \
        and not held_for(store, escalation_kind(it.get("data") or {}), it["id"], t)


def fixer_live(lease: Optional[dict[str, Any]], t: float) -> bool:
    return bool(lease and lease.get("holder") and float(lease["expires_at"]) > t)


def held_for(store: Any, kind: str, cid: str, t: float) -> bool:
    """Its kind's fixer works on this condition now."""
    lease = store.get(LEASE_PREFIX + kind)
    return fixer_live(lease, t) and meta_of(lease).get("condition") == cid


def give_to_owner(store: Any, t: float, it: dict[str, Any], route: EscalationRoute, kind: str, rec: dict[str, Any],
                  offer: Optional[dict[str, Any]]) -> str:
    """The tries are spent: ONE owner entry saying what was tried, under the condition's own alert item (it closes
    with the condition), the offer closed, never dispatched again this spell."""
    d, cid = it.get("data") or {}, it["id"]
    tried = "; ".join(f"try {x.get('try')}: {x.get('outcome')} ({x.get('worktree') or 'no worktree'})"
                      for x in rec.get("tries") or [])
    line = (f"ESCALATION GAVE UP: {kind} {d.get('key')}: WAITING ON YOU: {route.tries} agent tries did not clear it "
            f"-- {tried}. The condition: {str(d.get('text'))[:300]}")
    inbox_write("escalation", line, [line], [], 1, item=f"condition:{d.get('key')}")
    if offer:
        store.close_offer(offer["item"], "owner")
    write_record(store, cid, moved_to("owner", owner_told=t))
    say(item_of(kind, cid), rec.get("state") or "offered", "owner", "timeout", t,
        f"{route.tries} tries spent, handed to the owner")
    return line


# ---------------------------------------------------------------- dispatching
def live_fixers(store: Any, t: float) -> list[dict[str, Any]]:
    return [r for r in store.list(LEASE_PREFIX) if fixer_live(r, t)]


def dispatch_offers(store: Any, t: float, launcher: EscalationLauncher, conditions: dict[str, Any]) -> list[str]:
    """Each open escalation offer, oldest first: started when its kind has no live fixer, a slot is free, its backoff
    is over and claude may launch in its repo -- else its reason written on it."""
    acts: list[str] = []
    blocker = launcher.blocker()
    for off in store.offers(OFFER_KIND):
        it = conditions.get(off["item"].split(":", 1)[1])
        if it is None:
            continue
        why = offer_waits(store, t, launcher, off, escalation_route(it["data"]), blocker)
        if why:
            if why != off.get("reason"):
                store.offer_reason(off["id"], why)
            continue
        acts.append(dispatch_one(store, t, launcher, off, it))
    return acts


def offer_waits(store: Any, t: float, launcher: EscalationLauncher, off: dict[str, Any], route: EscalationRoute,
                blocker: Optional[str]) -> Optional[str]:
    """Why the offer cannot be taken now -- dispatch blocked, its backoff, its kind's fixer, the cap, its repo -- or
    None."""
    pic, kind = off.get("picture") or {}, off["tier"]
    lease = store.get(LEASE_PREFIX + kind)
    alive = len(live_fixers(store, t))
    if blocker:
        return blocker
    if float(pic.get("next_after") or 0) > t:
        return f"try {pic.get('try')} backs off for {int(float(pic['next_after']) - t) // 60 + 1} min"
    if fixer_live(lease, t):
        return f"one {kind} fixer at a time: {lease['holder']} works on {meta_of(lease).get('key')}"
    if alive >= ESCALATION_FIXERS:
        return f"capacity {alive}/{ESCALATION_FIXERS} escalation fixers"
    return repo_waits(launcher, route)


def repo_waits(launcher: EscalationLauncher, route: EscalationRoute) -> Optional[str]:
    """Why no session can start in the route's repo here: no clone, or claude does not trust it (NOT DISPATCHABLE:
    said on the offer, no try counted; the owner is asked once, fixersession.py trust_asks)."""
    path = launcher.repo_path(route.repo)
    if not path:
        return f"no clone of {route.repo} on this machine"
    gap = launcher.trust_gap(path)
    return f"NOT DISPATCHABLE: {gap}" if gap else None


def dispatch_one(store: Any, t: float, launcher: EscalationLauncher, off: dict[str, Any], it: dict[str, Any]) -> str:
    """Take the offer (atomic with its kind's lease) and start the session; a start that failed is a failed try."""
    d, cid, kind = it["data"], it["id"], off["tier"]
    route, n = escalation_route(d), int((off.get("picture") or {}).get("try") or 1)
    meta = {"condition": cid, "kind": kind, "key": d.get("key"), "try": n, "started": t, "runtime": route.runtime,
            "model": route.model, "host": launcher.host()}
    took = store.take(off["id"], f"laptop:escalation:{kind}-{n}", LEASE_TTL, route.runtime + LEASE_TTL, meta=meta)
    if not took or "epoch" not in took:
        return f"escalation {kind} ({d.get('key')}): not taken ({took})"
    repo = launcher.repo_path(route.repo) or ""
    session, why = launcher.start(route, kind, it, escalation_brief(store, route, kind, it, repo), repo)
    row = {"name": LEASE_PREFIX + kind, "holder": "taken", **took}
    if not session:
        return end_try(store, t, launcher, row, meta, "NOT STARTED", why)
    store.set_meta(row["name"], took["epoch"], took["secret"], session)
    write_record(store, cid, moved_to("dispatched"))
    say(item_of(kind, cid), "offered", "dispatched", "agent", t,
        f"try {n}/{route.tries}: {route.model} session {session.get('session')} in {session.get('worktree')}")
    return f"escalation {kind} ({d.get('key')}): DISPATCHED try {n}/{route.tries}, {route.model}, {why}"


# ---------------------------------------------------------------- the brief
REPO_RULES = {
    "bin": ("agentctl is LIVE: read ~/bin/CLAUDE.md and ~/bin/AGENTCTL.md first. Work ONLY in your worktree (never "
            "edit ~/bin itself); push with `python3 devtools/selfpush.py` from it, which runs the full suite and the "
            "simulator gate once; then fast-forward ~/bin (`git -C ~/bin merge --ff-only origin/main`)."),
    "movies": ("Read ~/projects/movies/CLAUDE.md and ~/bin/briefs/movies-agent-preamble.md first. Work ONLY in your "
               "worktree; land with `~/bin/agentctl land` (run_in_background), never `git push`."),
}
RULES = ("THE RULES: the commit gate (a test that fails before your change and passes after it, written first); no "
         "bypass of any check (no --no-verify, no PREPUSH_SKIP, no trailer you cannot justify); never pkill / "
         "killall (`scripts/agent-procs kill --mine`); never rerun a red to green; never touch the vault, Touch ID, "
         "signing, security settings, the repository's visibility or a force push -- that is the owner's (say "
         "`BLOCKED ON OWNER: <what>` and stop). THIS ESCALATION CLOSES ONLY WHEN ITS CONDITION'S OWN PROBE PASSES: "
         "your word does not close it, and ending with it still open counts as a failed try. End with ONE line: "
         "`CLEARED: <what you changed>`, `GAVE UP: <why>` or `BLOCKED ON OWNER: <what he must do>`.")


def condition_history(store: Any, cid: str, limit: int = 30) -> list[str]:
    """The condition's own events, newest last (what `agentctl history <id>` prints the moves of)."""
    out = []
    for e in list(store.item_events("condition", cid) or [])[-limit:]:
        out.append(f"  {e.get('at')} {e.get('kind')} [{e.get('cause') or '-'} by {e.get('actor') or '-'}] "
                   f"{str(e.get('text') or '')[:200]}")
    return out


def escalation_brief(store: Any, route: EscalationRoute, kind: str, it: dict[str, Any], repo: str) -> str:
    """The session's prompt: the escalation, what earlier tries did, its history, the task and the rules."""
    d, cid = it["data"], it["id"]
    tries = record_of(it).get("tries") or []
    tried = "\n".join(f"  try {x.get('try')}: {x.get('outcome')} (worktree {x.get('worktree')})" for x in tries)
    return "\n".join([
        f"You are agentctl's escalation fixer for {kind} (condition {cid}, key {d.get('key')}), dispatched because "
        f"it stayed open past its bound. Your worktree is the current directory, off {repo}'s main.",
        f"THE ESCALATION: {d.get('text')}",
        f"ITS PROBE: {(d.get('probe') or {}).get('kind')} {(d.get('probe') or {}).get('args')}; last result: "
        f"{d.get('last_result')}",
        "EARLIER TRIES:\n" + (tried or "  none"),
        f"ITS HISTORY (`~/bin/agentctl history {cid}` for the whole timeline):\n"
        + ("\n".join(condition_history(store, cid)) or "  none"),
        f"YOUR TASK: {route.task} Find the root cause, fix it with its test, land it, and check the condition "
        f"clears (`~/bin/agentctl recover status`).",
        REPO_RULES.get(route.repo, ""), RULES])
