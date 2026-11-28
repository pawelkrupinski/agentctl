# agentctl, part `statusheader`: the status model's header section: the gate, the tick and the store's state
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from typing import Any
from agentctl.statefile import read_json_file
from agentctl.core import SHORT
from agentctl.forge import RED_CONCLUSIONS, forge_run_url
from agentctl.cistate import state_read
from agentctl.recordingpins import pins_status
from agentctl.remotestore import store_url
from agentctl.tunnel import tunnel_ports, tunnel_state_path
from agentctl.storestate import STORE_UNREACHABLE_AFTER, port_open, remote_status_state
from agentctl.statusline import render_status_line, status_gate_text


def status_run_url(repo: Any, number: Any, run: Any = None) -> Any:
    """A run's page on the forge (GitHub links by the run's id: `run`, the run's record, carries it)."""
    return forge_run_url(repo or SHORT["movies"], number, run)


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


def status_header(t: Any, d: Any = None, probes: bool = True) -> Any:
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
