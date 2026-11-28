# agentctl, part `status`: the status model: each source read under a bound, assembled and cached; the status line
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from typing import Any, Dict
import json
import os
import threading
import time
from agentctl.caches import StatusMemos
from agentctl.statefile import read_json_file, read_record
from agentctl.clock import epoch_now
from agentctl.timefmt import parse_time
from agentctl.watchknobs import knob_status_bound
from agentctl.core import HOME, SHORT
from agentctl.forge import RED_CONCLUSIONS, forge_run_url
from agentctl.redrepos import red_family
from agentctl.cistate import state_read
from agentctl.pressure import pressure_report
from agentctl.machine import MachineProbe
from agentctl.store import Store
from agentctl.landhold import hold_row, land_hold
from agentctl.fixerblockers import cached_dispatch_blocker
from agentctl.conditions import open_conditions
from agentctl.recordingpins import pins_status
from agentctl.redrecord import lease_is_live, red_active, red_lease, red_what
from agentctl.leases import lease_meta
from agentctl.owners import owner_record_path
from agentctl.statusagents import status_agents, status_strip_lease
from agentctl.ownerleases import owners_status
from agentctl.remotestore import RemoteStore, store_url
from agentctl.tunnel import tunnel_ports, tunnel_state_path
from agentctl.storestate import STORE_UNREACHABLE_AFTER, port_open, remote_status_state
from agentctl.statusline import render_status_line, status_gate_text
from agentctl.reverts import REVERT_OPEN, revert_brief, revert_line
from agentctl.issues import status_issues
from agentctl.orphanreap import holder_work
from agentctl.changekind import HOLD_SHOWN
from agentctl.reduntracked import red_untracked_list
from agentctl.redvault import block_verdict_note
from agentctl.reddispatch import red_cap, red_family_holder, red_fixers_alive, repo_dispatch_off_all
from agentctl.redview import red_view
from agentctl.inboxcli import escalated_now
from agentctl.park import parked_lines
from agentctl.paths import reds_dir


STATUS_STORE_TIMEOUT = 5        # s per remote store call
STATUS_CACHE = 5                # s an assembled status is served again
STATUS_EVENTS = 50
STATUS_CHANGE_OPEN = ("queued", "checking", "checked", "pushing", "waiting")
STATUS_BATCH_OPEN = ("blocked-red", "assembling", "checking", "pushing", "bisecting", "held")


def status_store():
    """The store the page reads: the remote one with short timeouts when the laptop is the sync, else the local one."""
    url = store_url()
    return RemoteStore(url, timeout=STATUS_STORE_TIMEOUT) if url else Store()


def status_run_url(repo, number, run=None):
    """A run's page on the forge (GitHub links by the run's id: `run`, the run's record, carries it)."""
    return forge_run_url(repo or SHORT["movies"], number, run)


def status_error(e):
    return {"error": f"{type(e).__name__}: {str(e).replace('agentctl: ', '')[:300]}"}




def status_sources(sources, bound, memos: StatusMemos, keep=False):
    """({name: what fn returned, or {"error": ...}}, {"stale": {name: why}, "took": {name: s}}): every (name, fn, factor)
    in a daemon thread of its own, joined against one deadline (bound * factor s each). keep (the server's): a source
    still reading then shows its LAST finished read, `stale` saying since when, and its read goes on in the background
    for the next refresh (one read per source at a time) -- a slow source never empties its section; else it is an
    error. memos: the status reads' memory (the last read, the read under way, of each source)."""
    out, threads = {}, []
    start = time.monotonic()  # wall: a thread join's bound is real time, whatever the injected clock says
    for name, fn, factor in sources:
        th = memos.reading.get(name) if keep else None
        if not (th and th.is_alive()):
            th = threading.Thread(target=status_read, args=(name, fn, out, keep, memos.last), daemon=True,
                                  name=f"status:{name}")
            th.start()
            if keep:
                memos.reading[name] = th
        threads.append((name, th, bound * factor))
    for _name, th, b in threads:
        th.join(max(0.0, start + b - time.monotonic()))  # wall: as above
    return status_answers(threads, out, keep, memos.last)


def status_read(name: Any, fn: Any, out: Any, keep: Any, last: dict[str, Any]) -> Any:
    """One source read in its thread: its answer (or its error) and how long it took, into out (and, keep, the last)."""
    t0 = time.monotonic()  # wall: how long the read really took
    try:
        v = fn()
    except (Exception, SystemExit) as e:
        v = status_error(e)
    out[name] = (epoch_now(), v, round(time.monotonic() - t0, 2))  # wall: as above
    if keep:
        last[name] = out[name]


def status_answers(threads: Any, out: Any, keep: Any, last: dict[str, Any]) -> Any:
    """Each source's answer once the deadline passed: its read, else (keep) its last finished one marked stale, else an
    error."""
    res, stale, took = {}, {}, {}
    for name, th, b in threads:
        got = out.get(name) or (last.get(name) if keep and not th.is_alive() else None)
        if got:
            res[name], took[name] = got[1], got[2]
        elif keep and name in last:
            at, res[name], _took = last[name]
            stale[name] = f"still reading after {b:.0f} s; shown as of {time.strftime('%H:%M:%S', time.localtime(at))}"
        else:
            res[name] = {"error": f"no answer within {b:.0f} s"}
    return res, {"stale": stale, "took": took}


def status_gate_rows(ci: Any) -> Any:
    """The gate's rows for the page: each workflow's decided run and the newer one in flight."""
    rows = []
    for g in ci.get("gate") or []:
        dec, new = g.get("decided") or {}, g.get("newer") or {}
        rows.append({"name": g.get("name"), "number": dec.get("number"), "conclusion": dec.get("conclusion"),
                     "url": status_run_url(ci.get("repo"), dec.get("number"), dec), "title": dec.get("displayTitle"),
                     "newer": new.get("number"), "newer_status": new.get("status"),
                     "newer_url": status_run_url(ci.get("repo"), new.get("number"), new), "since": g.get("since"),
                     "count": g.get("count")})
    return rows


def status_tick_and_store(d: Any, url: Any, t: Any) -> Any:
    """The reconciler's tick and the store's reachability, as the header's fields."""
    tick = d.get("tick_at")
    fetched = float(d.get("fetched_at") or 0) if url else None
    return {"tick_at": tick, "tick_age": t - float(tick) if tick else None, "fetched_at": d.get("fetched_at"),
            "store_url": url, "store_unreachable": bool(url) and t - fetched > STORE_UNREACHABLE_AFTER,
            "store_error": d.get("store_error")}


def status_state_fields(d: Any) -> Any:
    """The header's fields read straight from state.json: the published summary, the code, the queue, the open items."""
    summary = d.get("summary") or {}
    return {"summary": summary.get("line"), "summary_at": summary.get("at"), "summary_repo": summary.get("repo"),
            "code": d.get("version"), "rev": d.get("rev"), "network": d.get("network"),
            "waiting": d.get("waiting") or [], "queued": d.get("queued") or [], "hold": d.get("hold"),
            "items": {k: d.get(k) or [] for k in ("changes", "batch", "reds", "conditions")},
            "reverts": d.get("reverts") or []}


def status_header(t, d=None, probes=True):
    """The cheap section every renderer starts from: state.json (on the sync refreshed from ci-runner's /v1/state when
    its mirror is old, at most one GET), the gate text, the reconciler's tick, the store's reachability, the waiting and
    checking changes, the open items, the pins. probes: also the tunnel's state and the store port it forwards (the page's)."""
    url = store_url()
    if d is None:
        d = remote_status_state(url, t) if url else state_read()
    ci = d.get("ci") or {}
    gate = status_gate_rows(ci)
    store = status_tick_and_store(d, url, t)
    h = {"gate": gate, "gate_red": any(x["conclusion"] in RED_CONCLUSIONS for x in gate),
         "gate_text": status_gate_text(d, t, unreachable=store["store_unreachable"]),
         "ci_at": ci.get("at"), "repo": ci.get("repo") or SHORT["movies"], **store, **status_state_fields(d),
         "pins": pins_status(now_=t)}
    if probes:
        tunnel = read_json_file(tunnel_state_path())
        h["tunnel"] = {k: tunnel.get(k) for k in ("fails", "last_kick", "pending_since", "escalated", "network")}
        h["store_port"] = port_open(tunnel_ports()[0][0], timeout=1.0)
    h["line"] = render_status_line(h, t)
    return h


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


def family_holder_row(st: Any, f: Any, t: Any, active: Any, leases: Any) -> Any:
    """The live lease holding a family: its primary one, else an owner's (or any other) on one of its open reds."""
    row = red_family_holder(st, f, t, active=active)
    if row:
        return row
    return next((r for r in leases if (active.get(r["name"].split("/", 1)[1]) or {}).get("data")
                 and red_family(active[r["name"].split("/", 1)[1]]["data"]) == f), None)


def family_holder_worktree(row: Any, f: Any) -> Any:
    """The holder's worktree: its lease's, an owner's ONE worktree from its record."""
    wt = lease_meta(row).get("worktree")
    if not wt and lease_meta(row).get("kind") in ("owned", "owner"):
        wt = (read_record(owner_record_path(lease_meta(row).get("family") or f)) or {}).get("worktree")
    return wt


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
        row = family_holder_row(st, f, t, active, leases)
        work, why = holder_work(row, t, cache) if row else (None, None)
        out.append({"family": f, "open": fams[f], "holder": row["holder"] if row else None,
                    "worktree": family_holder_worktree(row, f) if row else None, "at_work": work, "work": why})
    return out


def status_store_probe(st, t):
    """Is the store answering, and the reconciler's own tick (the remote /v1/state's tick_at) when it is remote."""
    t0 = time.monotonic()  # wall: the store's real latency
    if isinstance(st, RemoteStore):
        snap = st.state() or {}
        tick = snap.get("tick_at")
        return {"remote": st.url, "reachable": True, "ms": int((time.monotonic() - t0) * 1000),  # wall: as above
                "tick_at": tick,
                "tick_age": t - float(tick) if tick else None, "code": snap.get("version")}
    st.meta_get("last_alive", 0)
    return {"remote": None, "reachable": True, "ms": int((time.monotonic() - t0) * 1000)}  # wall: as above


def status_pressure(machine: MachineProbe):
    """The laptop's pressure reading, with its numbers (why it holds other work, else the plain reading)."""
    try:
        why, summary = pressure_report(machine)
    except Exception as e:
        return f"unread ({e})"
    return f"UNDER PRESSURE: {why} [{summary}]" if why else summary


def status_dispatcher(st, t, machine):
    return {"blocker": cached_dispatch_blocker(), "off_flag": os.path.exists(reds_dir("DISPATCH_OFF")),
            "repos_off": repo_dispatch_off_all(st),
            "alive": len(red_fixers_alive(st, t)), "cap": red_cap(), "pressure": status_pressure(machine),
            "escalated": [{"type": typ, "id": it["id"], "words": words} for typ, it, words in escalated_now(st)]}


def _red_view_text(st: Any, it: Any, t: Any) -> Any:
    """(text, short) of a red's view; a view that fails says so."""
    try:
        return red_view(st, it, t)
    except (Exception, SystemExit) as e:
        return f"(view failed: {str(e)[:120]})", it["state"]


def _history_tail(d: Any) -> Any:
    """A red's last 5 history entries, their outcomes cut."""
    return [{"at": h.get("at"), "holder": h.get("holder"), "outcome": str(h.get("outcome"))[:300]}
            for h in (d.get("history") or [])[-5:]]


def _red_note(st: Any, it: Any, t: Any, lease: Any) -> Any:
    return (lease or {}).get("note") or block_verdict_note(it, t, st) or red_history_note(it)


def status_reds(st, t, repo=None):
    """Every open red: its machine state, its run, its holder and when the hold lapses, its last note."""
    leases = {r["name"]: r for r in st.list("red/")}
    out = []
    for it in sorted(red_active(st).values(), key=lambda i: (not i["data"].get("gate"), i["entered_at"])):
        d = it["data"]
        key = d.get("key") or it["id"]
        text, short = _red_view_text(st, it, t)
        lease = status_strip_lease(leases.get(red_lease(key)), t)
        lr = d.get("last_run") or {}
        opened = d.get("opened_at") or it["entered_at"]
        out.append({"key": key, "id": it["id"], "state": it["state"], "short": short, "text": text,
                    "workflow": d.get("workflow"), "run": lr.get("number"), "run_url": status_run_url(repo, lr.get("number"), lr),
                    "jobs": [str(j) for j in d.get("jobs") or [d.get("job")] if j], "line": d.get("line"),
                    "gate": bool(d.get("gate")), "kind": d.get("kind"), "red_kind": d.get("red_kind"), "tier": d.get("tier"),
                    "opened_at": opened, "age": t - float(opened), "entered_at": it["entered_at"],
                    "deadline": it["deadline"], "level": it.get("level"), "lease": lease,
                    "fix": d.get("fix"), "attempts": d.get("attempts"), "family": red_family(d),
                    "note": _red_note(st, it, t, lease),
                    "what": red_what(d),
                    "history": _history_tail(d)})
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


def open_status_store(store: Any, only: Any) -> tuple[Any, Any]:
    """(the store the sections read, None) or (None, the error that kept it from opening); no store for a header-only read."""
    if only and not set(only) - {"header"}:
        return None, None
    try:
        return (store if store is not None else status_store()), None
    except (Exception, SystemExit) as e:
        return None, status_error(e)


def status_section_sources(t: float, repo: str, need: Any, machine: MachineProbe,
                           memos: StatusMemos) -> tuple[Any, ...]:
    """The status model's sections: (name, producer, weight) each, every producer reading the store through `need`."""
    return (("header", lambda: status_header(t), 1),
               ("store", lambda: status_store_probe(need(), t), 1),
               ("dispatcher", lambda: status_dispatcher(need(), t, machine), 1),
               ("reds", lambda: status_reds(need(), t, repo), 1),
               ("families", lambda: status_families(need(), t), 1),
               ("untracked", lambda: [{"repo": o_r, **r} for o_r, r in red_untracked_list(need())], 1),
               ("owners", lambda: owners_status(need(), t), 1),
               ("agents", lambda: status_agents(need(), t, memos), 2),
               ("landing", lambda: status_landing(need(), t), 1),
               ("conditions", lambda: status_conditions(need(), t), 1),
               ("issues", lambda: status_issues(need(), t), 1),
               ("reverts", lambda: [revert_brief(i) for i in need().items("revert", REVERT_OPEN)], 1),
               ("events", lambda: status_events(need()), 1))


def status_data(store=None, now_=None, bound=None, keep=False, only=None, *, machine, memos):
    """THE STATUS MODEL (/api/status): every section from its own bounded source; a failing one is {"error": ...} in
    its place. only: just these sections (a renderer's). memos: the status reads' memory (StatusMemos)."""
    t = now_ if now_ is not None else epoch_now()
    b = bound if bound is not None else knob_status_bound()
    d = state_read()
    repo = (d.get("ci") or {}).get("repo") or SHORT["movies"]
    st, st_err = open_status_store(store, only)

    def need():
        if st is None:
            raise RuntimeError(st_err["error"])
        return st
    sources = status_section_sources(t, repo, need, machine, memos)
    res, meta = status_sources(tuple(x for x in sources if not only or x[0] in only), b, memos, keep=keep)
    if "error" in res.get("store", {}):
        res["store"] = {**res["store"], "remote": store_url(), "reachable": False}
    return {"at": t, "repo": repo, "home": HOME, **res, **meta}


_status_cache_lock = threading.Lock()


def status_cached(machine: MachineProbe, memos: StatusMemos):
    """The status model, assembled again past STATUS_CACHE (memos.assembled: the last one)."""
    model = memos.assembled
    with _status_cache_lock:  # one assembly at a time: refreshing tabs share it
        if model.get("data") is None or epoch_now() - model.get("at", 0.0) >= STATUS_CACHE:
            model["data"] = status_data(keep=True, machine=machine, memos=memos)
            model["at"] = epoch_now()
        return model["data"]


