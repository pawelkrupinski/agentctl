# agentctl, part `statusreds`: the status model's reds section: each open red with its lease, view and history
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from typing import Any
from agentctl.timefmt import parse_time
from agentctl.redrepos import red_family
from agentctl.redrecord import red_active, red_lease, red_what
from agentctl.statusagents import status_strip_lease
from agentctl.redvault import block_verdict_note
from agentctl.redview import red_view
from agentctl.statusheader import status_run_url


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


def status_reds(st: Any, t: Any, repo: Any = None) -> Any:
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


def red_history_note(it: Any) -> Any:
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
