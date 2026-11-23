# agentctl, part `status`: the status model: each source read under a bound, assembled and cached; the status line
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from typing import Any, Dict
import json
import os
import re
import threading
import time
from agentctl.core import HOME, SHORT, env_num, process_cache, reds_dir, slug
from agentctl.timefmt import parse_time
from agentctl.statefile import read_json_file, read_record
from agentctl.forge import RED_CONCLUSIONS, forge_run_url
from agentctl.owned import default_repo
from agentctl.redfamilies import red_family
from agentctl.cistate import state_read
from agentctl.gatesummary import GATE_LINE, GATE_SUMMARY_MAX_AGE
from agentctl.dispatchblock import cached_dispatch_blocker
from agentctl.store import open_store
from agentctl.redrecord import lease_is_live, red_active, red_lease, red_what
from agentctl.leases import lease_meta
from agentctl.reverts import REVERT_OPEN, revert_brief, revert_line
from agentctl.conditions import open_conditions
from agentctl.queue import hold_row, hold_text, land_hold
from agentctl.issues import status_issues
from agentctl.changekind import HOLD_SHOWN
from agentctl.owners import owner_record_path
from agentctl.remotestore import RemoteStore, store_url
from agentctl.tunnel import tunnel_ports, tunnel_state_path
from agentctl.storestate import NETWORK_SHOWN_FOR, STORE_UNREACHABLE_AFTER, port_open, remote_status_state
from agentctl.recordingpins import pins_status
from agentctl.reduntracked import red_untracked_list
from agentctl.redvault import block_verdict_note
from agentctl.statusagents import status_agents, status_strip_lease
from agentctl.pressure import pressure_report
from agentctl.orphanreap import holder_work
from agentctl.reddispatch import red_cap, red_family_holder, red_fixers_alive, repo_dispatch_off_all
from agentctl.park import parked_lines
from agentctl.redview import red_view
from agentctl.cireport import ci_why
from agentctl.recovery import escalated_now
from agentctl.ownerleases import owners_status


STATUS_SOURCE_BOUND = 8         # s a source may take ($AGENTCTL_STATUS_BOUND); the agents' source twice that
STATUS_STORE_TIMEOUT = 5        # s per remote store call
STATUS_CACHE = 5                # s an assembled status is served again
STATUS_EVENTS = 50
STATUS_CHANGE_OPEN = ("queued", "checking", "checked", "pushing", "waiting")
STATUS_BATCH_OPEN = ("blocked-red", "assembling", "checking", "pushing", "bisecting", "held")


def status_store():
    """The store the page reads: the remote one with short timeouts when the laptop is the sync, else the local one."""
    url = store_url()
    return RemoteStore(url, timeout=STATUS_STORE_TIMEOUT) if url else open_store()


def status_run_url(repo, number, run=None):
    """A run's page on the forge (GitHub links by the run's id: `run`, the run's record, carries it)."""
    return forge_run_url(repo or SHORT["movies"], number, run)


def status_err(e):
    return {"error": f"{type(e).__name__}: {str(e).replace('agentctl: ', '')[:300]}"}


_status_last = process_cache({})      # source name -> (finished at, result, took s) of its last read (the server's own reads only)
_status_reading = process_cache({})   # source name -> the thread reading it


def status_sources(sources, bound, keep=False):
    """({name: what fn returned, or {"error": ...}}, {"stale": {name: why}, "took": {name: s}}): every (name, fn, factor)
    in a daemon thread of its own, joined against one deadline (bound * factor s each). keep (the server's): a source
    still reading then shows its LAST finished read, `stale` saying since when, and its read goes on in the background
    for the next refresh (one read per source at a time) -- a slow source never empties its section; else it is an
    error."""
    out, threads = {}, []

    def work(name, fn):
        t0 = time.time()
        try:
            v = fn()
        except (Exception, SystemExit) as e:
            v = status_err(e)
        out[name] = (time.time(), v, round(time.time() - t0, 2))
        if keep:
            _status_last[name] = out[name]
    start = time.time()
    for name, fn, factor in sources:
        th = _status_reading.get(name) if keep else None
        if not (th and th.is_alive()):
            th = threading.Thread(target=work, args=(name, fn), daemon=True, name=f"status:{name}")
            th.start()
            if keep:
                _status_reading[name] = th
        threads.append((name, th, bound * factor))
    for _name, th, b in threads:
        th.join(max(0.0, start + b - time.time()))
    res, stale, took = {}, {}, {}
    for name, th, b in threads:
        got = out.get(name) or (_status_last.get(name) if keep and not th.is_alive() else None)
        if got:
            res[name], took[name] = got[1], got[2]
        elif keep and name in _status_last:
            at, res[name], _took = _status_last[name]
            stale[name] = f"still reading after {b:.0f} s; shown as of {time.strftime('%H:%M:%S', time.localtime(at))}"
        else:
            res[name] = {"error": f"no answer within {b:.0f} s"}
    return res, {"stale": stale, "took": took}


# ---- ONE STATUS MODEL (docs/simplification-2026-10.md item 4): status_data() is THE assembled status (what /api/status
# returns); `status --line`, `status`, `red status`, `ci-why --summary --cached` and the page render from it (or from
# the sections they need: status_data(only=...)). No second formatter reads state.json or the store for itself.
def status_gate_text(d, t, o_r=None, unreachable=False):
    """THE gate text, from the published summary in state.json d: the line itself while fresh; stale or missing, what
    it last said marked with its age -- `gate ?` when ci-runner's store does not answer (no verdict), `gate NETWORK`
    on a lossy uplink, else `gate UNREAD`. o_r: only that repository's line counts."""
    sm = d.get("summary") or {}
    try:
        epoch = int(sm["at"])
    except (KeyError, TypeError, ValueError):
        epoch = None
    line = sm.get("line") if epoch and GATE_LINE.match(sm.get("line") or "") and (not o_r or sm.get("repo") == o_r) else None
    age = t - epoch if epoch else None
    if line and -5 <= age <= GATE_SUMMARY_MAX_AGE:
        return line
    net = d.get("network") or {}
    lossy = net.get("at") and t - float(net["at"]) < NETWORK_SHOWN_FOR
    asof = time.strftime("%H:%M", time.localtime(epoch)) if epoch else "never"
    rest = line[5:] if line else None
    if unreachable:
        return f"gate ? (as of {asof}: {rest})" if rest else "gate ?"
    if lossy:
        return (f"gate NETWORK (lossy uplink: {int(float(net.get('loss') or 0) * 100)}% loss"
                + (f"; as of {asof}: {rest})" if rest else ")"))
    return f"gate UNREAD (as of {asof}: {rest or 'no fresh published gate'})"


def status_header(t, d=None, probes=True):
    """The cheap section every renderer starts from: state.json (on the sync refreshed from ci-runner's /v1/state when
    its mirror is old, at most one GET), the gate text, the reconciler's tick, the store's reachability, the waiting and
    checking changes, the open items, the pins. probes: also the tunnel's state and the store port it forwards (the page's)."""
    url = store_url()
    if d is None:
        d = remote_status_state(url, t) if url else state_read()
    ci = d.get("ci") or {}
    gate = []
    for g in ci.get("gate") or []:
        dec, new = g.get("decided") or {}, g.get("newer") or {}
        gate.append({"name": g.get("name"), "number": dec.get("number"), "conclusion": dec.get("conclusion"),
                     "url": status_run_url(ci.get("repo"), dec.get("number"), dec), "title": dec.get("displayTitle"),
                     "newer": new.get("number"), "newer_status": new.get("status"),
                     "newer_url": status_run_url(ci.get("repo"), new.get("number"), new), "since": g.get("since"),
                     "count": g.get("count")})
    tick = d.get("tick_at")
    fetched = float(d.get("fetched_at") or 0) if url else None
    unreachable = bool(url) and t - fetched > STORE_UNREACHABLE_AFTER
    h = {"gate": gate, "gate_red": any(x["conclusion"] in RED_CONCLUSIONS for x in gate),
         "gate_text": status_gate_text(d, t, unreachable=unreachable),
         "summary": (d.get("summary") or {}).get("line"), "summary_at": (d.get("summary") or {}).get("at"),
         "summary_repo": (d.get("summary") or {}).get("repo"),
         "ci_at": ci.get("at"), "repo": ci.get("repo") or SHORT["movies"],
         "tick_at": tick, "tick_age": t - float(tick) if tick else None, "fetched_at": d.get("fetched_at"),
         "store_url": url, "store_unreachable": unreachable, "store_error": d.get("store_error"),
         "code": d.get("version"), "rev": d.get("rev"), "network": d.get("network"), "waiting": d.get("waiting") or [],
         "queued": d.get("queued") or [], "hold": d.get("hold"),
         "items": {k: d.get(k) or [] for k in ("changes", "batch", "reds", "conditions")},
         "reverts": d.get("reverts") or [],
         "pins": pins_status(now_=t)}
    if probes:
        tunnel = read_json_file(tunnel_state_path())
        h["tunnel"] = {k: tunnel.get(k) for k in ("fails", "last_kick", "pending_since", "escalated", "network")}
        h["store_port"] = port_open(tunnel_ports()[0][0], timeout=1.0)
    h["line"] = render_status_line(h, t)
    return h


def render_status_line(h, t):
    """The status line: the gate text, the waiting and checking changes; `RECONCILER STALLED since HH:MM` when the
    tick is over 2 min old, `STORE UNREACHABLE since HH:MM` when ci-runner's store has not answered for a minute."""
    short = lambda wts: ", ".join(re.sub(r"^.*-wt-", "", os.path.basename(w or "")) for w in wts)
    hm = lambda x: time.strftime('%H:%M', time.localtime(float(x)))
    parts, tick, url = [h["gate_text"]], h["tick_at"], h["store_url"]
    if h["store_unreachable"]:  # no verdict on ci-runner's reconciler: its store did not answer (never STALLED)
        se = h.get("store_error") or {}
        parts.insert(0, f"STORE UNREACHABLE since {hm(float(h.get('fetched_at') or 0) or se.get('since') or t)} ({url}"
                        + (f": {se['why'][:80]}" if se.get("why") else "") + ")"
                        + (f"; ci-runner ticked at {hm(tick)}" if tick else ""))
    elif not tick or t - float(tick) > 120:
        parts.insert(0, (f"RECONCILER STALLED since {hm(tick)}" if tick else "RECONCILER not running")
                     + (" (ci-runner)" if url else ""))
    hold = h.get("hold")
    if hold and float(hold.get("until") or 0) > t:  # the store's land hold (expired ones lift at the next tick)
        parts.append("LAND " + hold_text(hold))
    if h.get("reverts"):  # a reverted change its lander has not taken back up (reverts.py)
        parts.append(f"REVERTED unacked: {len(h['reverts'])}")
    waiting, queued = h["waiting"], h["queued"]
    parts.append(f"waiting: {len(waiting)}" + (f" — {short(waiting)}" if waiting else "")
                 + (f" · checking: {len(queued)} — {short(queued)}" if queued else ""))
    return " · ".join(parts)


def status_line(now_=None):
    """`status --line`: the header's line (no git, no GitHub; on the sync at most one GET /v1/state)."""
    t = now_ if now_ is not None else time.time()
    return status_header(t, probes=False)["line"]


def code_text(h):
    """The reconciler's code: its git revision first (`4d0fc921c9e6 (code d1677f40)`), the hash alone from a tick
    that predates `rev`."""
    code = str(h.get("code") or "?")
    return f"{h['rev']} (code {code[:8]})" if h.get("rev") else code


def item_line(r: Dict[str, Any], t: float) -> str:
    """`status`'s line for an open item row: its state, deadline, level, hold and a broken hold cycle."""
    return (f"  {r['id']}  {r['state']}  deadline in {int((r['deadline'] - t) // 60)} min"
            + (f"  (level {r['level']})" if r.get("level") else "")
            + (f"  held: {r['hold']}" if r.get("hold") else "")
            + (f"  {r['cycle']}" if r.get("cycle") else ""))


def render_status(h, t):
    """`agentctl status`: the line, the open items with their states and deadlines, the tick, the pins."""
    out = [h["line"]]
    for key in ("changes", "batch", "reds", "conditions"):
        rows = h["items"][key]
        out.append(f"{key}: {len(rows)}")
        out += [item_line(r, t) for r in rows]
    out.append(f"reverts unacked: {len(h.get('reverts') or [])}")
    out += [revert_line(r, t) for r in h.get("reverts") or []]
    try:  # parked worktrees: shown once here, never escalated (watchdog.py, PARKED)
        out += parked_lines()
    except (Exception, SystemExit) as e:
        out.append(f"parked: unread ({str(e)[:120]})")
    tick = h["tick_at"]
    out.append(f"tick: {int(t - tick)} s ago, code {code_text(h)}" if tick else "tick: never")
    out.append(h["pins"])
    return "\n".join(out)


def status_families(st, t):
    """Each family with open reds: how many, and its ONE fixer (the live primary lease), if any."""
    active = red_active(st)
    fams = {}
    for it in active.values():
        f = red_family(it["data"])
        if f:
            fams[f] = fams.get(f, 0) + 1
    out = []
    leases = [r for r in st.list("red/") if lease_is_live(r, t)]
    cache = {}
    for f in sorted(fams):
        row = red_family_holder(st, f, t, active=active)
        if not row:  # an owner's (or any other) live lease on a red of the family is its holder too
            row = next((r for r in leases if (active.get(r["name"].split("/", 1)[1]) or {}).get("data")
                        and red_family(active[r["name"].split("/", 1)[1]]["data"]) == f), None)
        work, why = holder_work(row, t, cache) if row else (None, None)
        wt = lease_meta(row).get("worktree") if row else None
        if row and not wt and lease_meta(row).get("kind") in ("owned", "owner"):  # an owner's: its ONE worktree
            wt = (read_record(owner_record_path(lease_meta(row).get("family") or f)) or {}).get("worktree")
        out.append({"family": f, "open": fams[f], "holder": row["holder"] if row else None, "worktree": wt,
                    "at_work": work, "work": why})
    return out


def status_store_probe(st, t):
    """Is the store answering, and the reconciler's own tick (the remote /v1/state's tick_at) when it is remote."""
    t0 = time.time()
    if isinstance(st, RemoteStore):
        snap = st.state() or {}
        tick = snap.get("tick_at")
        return {"remote": st.url, "reachable": True, "ms": int((time.time() - t0) * 1000), "tick_at": tick,
                "tick_age": t - float(tick) if tick else None, "code": snap.get("version")}
    st.meta_get("last_alive", 0)
    return {"remote": None, "reachable": True, "ms": int((time.time() - t0) * 1000)}


def status_pressure():
    """The laptop's pressure reading, with its numbers (why it holds other work, else the plain reading)."""
    try:
        why, summary = pressure_report()
    except Exception as e:
        return f"unread ({e})"
    return f"UNDER PRESSURE: {why} [{summary}]" if why else summary


def status_dispatcher(st, t):
    return {"blocker": cached_dispatch_blocker(), "off_flag": os.path.exists(reds_dir("DISPATCH_OFF")),
            "repos_off": repo_dispatch_off_all(st),
            "alive": len(red_fixers_alive(st, t)), "cap": red_cap(), "pressure": status_pressure(),
            "escalated": [{"type": typ, "id": it["id"], "words": words} for typ, it, words in escalated_now(st)]}


def status_reds(st, t, repo=None):
    """Every open red: its machine state, its run, its holder and when the hold lapses, its last note."""
    leases = {r["name"]: r for r in st.list("red/")}
    out = []
    for it in sorted(red_active(st).values(), key=lambda i: (not i["data"].get("gate"), i["entered_at"])):
        d = it["data"]
        key = d.get("key") or it["id"]
        try:
            text, short = red_view(st, it, t)
        except (Exception, SystemExit) as e:
            text, short = f"(view failed: {str(e)[:120]})", it["state"]
        lease = status_strip_lease(leases.get(red_lease(key)), t)
        hist = d.get("history") or []
        lr = d.get("last_run") or {}
        opened = d.get("opened_at") or it["entered_at"]
        out.append({"key": key, "id": it["id"], "state": it["state"], "short": short, "text": text,
                    "workflow": d.get("workflow"), "run": lr.get("number"), "run_url": status_run_url(repo, lr.get("number"), lr),
                    "jobs": [str(j) for j in d.get("jobs") or [d.get("job")] if j], "line": d.get("line"),
                    "gate": bool(d.get("gate")), "kind": d.get("kind"), "red_kind": d.get("red_kind"), "tier": d.get("tier"),
                    "opened_at": opened, "age": t - float(opened), "entered_at": it["entered_at"],
                    "deadline": it["deadline"], "level": it.get("level"), "lease": lease,
                    "fix": d.get("fix"), "attempts": d.get("attempts"), "family": red_family(d),
                    "note": (lease or {}).get("note") or block_verdict_note(it, t, st) or red_history_note(it),
                    "what": red_what(d),
                    "history": [{"at": h.get("at"), "holder": h.get("holder"), "outcome": str(h.get("outcome"))[:300]}
                                for h in hist[-5:]]})
    return out


def red_history_note(it):
    """A red's last history entry as its note -- but for a fix-pending red, only one written since it went fix-pending:
    an older one is a holder's word from before the fix (2026-11-15, Order independence #74: both reds, fix-pending on
    09e482c6a with no holder, read "ORPHANED: laptop:owner:outside-gate holds it with no work" from 11:34)."""
    hist = it["data"].get("history") or []
    if not hist:
        return None
    if it["state"] == "fix-pending":
        try:
            if parse_time(hist[-1].get("at")).timestamp() < float(it["entered_at"]):
                return None
        except (ValueError, TypeError, AttributeError):
            return None
    return str(hist[-1].get("outcome"))[:400]


def status_landing(st, t):
    """The open change items and the batch, each with its deadline (and whether it is past)."""
    def row(it):
        d = it["data"]
        return {"id": it["id"], "state": it["state"], "entered_at": it["entered_at"], "deadline": it["deadline"],
                "past_deadline": it["deadline"] < t, "level": it.get("level"), "worktree": d.get("worktree"),
                "branch": d.get("branch"), "head": str(d.get("head") or "")[:10] or None,
                "submitted_at": d.get("submitted_at"), "why": d.get("why") or d.get("reason"),
                "hold": (d.get("hold") or {}).get("why") if it["state"] in HOLD_SHOWN else None,
                "cycle": d.get("cycle"), "changes": d.get("changes") or d.get("entries")}
    return {"changes": [row(it) for it in st.items("change", list(STATUS_CHANGE_OPEN))],
            "batch": [row(it) for it in st.items("batch", list(STATUS_BATCH_OPEN))],
            "hold": hold_row(land_hold(st, t, expire=False))}


def status_conditions(st, t):
    out = []
    for c in open_conditions(st):
        lr = c.get("last_result") or {}
        out.append({"kind": c.get("kind"), "key": c.get("key"), "text": str(c.get("text") or "")[:400],
                    "opened": c.get("opened"), "level": c.get("level"), "until": c.get("until"),
                    "probe": (c.get("probe") or {}).get("kind"), "ok": lr.get("ok"), "detail": lr.get("detail"),
                    "last_checked": c.get("last_checked"), "next_check": c.get("next_check"), "fails": c.get("fails")})
    return out


def status_events(st, n=STATUS_EVENTS):
    """The last n events of the feed (from just before the projector's cursor on, a few pages at most)."""
    after = max(0, int(st.meta_get("projector_seq", 0) or 0) - n)
    evs = []
    for _page in range(5):
        got = st.events(after=after, limit=500)
        evs = (evs + got)[-n:]
        if len(got) < 500:
            break
        after = got[-1]["seq"]
    return [{"seq": e.get("seq"), "at": e.get("at"), "kind": e.get("kind"), "severity": e.get("severity"),
             "item": f"{e.get('item_type')}:{e.get('item_id')}" if e.get("item_type") else None,
             "from": e.get("from_state"), "to": e.get("to_state"), "text": str(e.get("text") or "")[:400]}
            for e in reversed(evs)]


def status_data(store=None, now_=None, bound=None, keep=False, only=None):
    """THE STATUS MODEL (/api/status): every section from its own bounded source; a failing one is {"error": ...} in
    its place. only: just these sections (a renderer's)."""
    t = now_ if now_ is not None else time.time()
    b = bound if bound is not None else env_num("AGENTCTL_STATUS_BOUND", STATUS_SOURCE_BOUND)
    d = state_read()
    repo = (d.get("ci") or {}).get("repo") or SHORT["movies"]
    st, st_err = None, None
    if not only or set(only) - {"header"}:
        try:
            st = store if store is not None else status_store()
        except (Exception, SystemExit) as e:
            st_err = status_err(e)

    def need():
        if st is None:
            raise RuntimeError(st_err["error"])
        return st
    sources = (("header", lambda: status_header(t), 1),
               ("store", lambda: status_store_probe(need(), t), 1),
               ("dispatcher", lambda: status_dispatcher(need(), t), 1),
               ("reds", lambda: status_reds(need(), t, repo), 1),
               ("families", lambda: status_families(need(), t), 1),
               ("untracked", lambda: [{"repo": o_r, **r} for o_r, r in red_untracked_list(need())], 1),
               ("owners", lambda: owners_status(need(), t), 1),
               ("agents", lambda: status_agents(need(), t), 2),
               ("landing", lambda: status_landing(need(), t), 1),
               ("conditions", lambda: status_conditions(need(), t), 1),
               ("issues", lambda: status_issues(need(), t), 1),
               ("reverts", lambda: [revert_brief(i) for i in need().items("revert", REVERT_OPEN)], 1),
               ("events", lambda: status_events(need()), 1))
    res, meta = status_sources(tuple(x for x in sources if not only or x[0] in only), b, keep=keep)
    if "error" in res.get("store", {}):
        res["store"] = {**res["store"], "remote": store_url(), "reachable": False}
    return {"at": t, "repo": repo, "home": HOME, **res, **meta}


_status_cache = {"at": 0.0, "data": None}
_status_cache_lock = threading.Lock()


def status_cached():
    with _status_cache_lock:  # one assembly at a time: refreshing tabs share it
        if _status_cache["data"] is None or time.time() - _status_cache["at"] >= STATUS_CACHE:
            _status_cache["data"] = status_data(keep=True)
            _status_cache["at"] = time.time()
        return _status_cache["data"]


def cmd_ci_why(a):
    o_r = slug(a.repo) if a.repo else default_repo()
    target = None if a.target in (None, "main") else a.target.lstrip("#")
    if target is not None and not target.isdigit():
        raise SystemExit("agentctl: ci-why takes a run id, a run number (#N) or `main`")
    if getattr(a, "cached", False):
        if not getattr(a, "summary", False) or target:
            raise SystemExit("agentctl: ci-why --cached goes with --summary and main")
        # NEVER LIVE WORK (2026-10-28: a stale file sent the status line's 15 s refresh into a 60 s live read): the
        # processor's published line, or -- stale or missing -- what it last said, marked with its age. Live reads
        # belong to the processor's publish step only.
        text = status_gate_text(state_read(), time.time(), o_r)  # the status model's gate text, for this repo
        print(text)
        return 1 if text.startswith("gate RED") else 0 if GATE_LINE.match(text) else 2  # 2: UNREAD / ? / NETWORK
    red, text = ci_why(o_r, a.tail, target, summary=getattr(a, "summary", False))
    print(text)
    return 1 if red else 0
