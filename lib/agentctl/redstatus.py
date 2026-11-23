# agentctl, part `redstatus`: `agentctl red status`: its text from the status model, and the snapshot the tick writes of it
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import time
from typing import Any
from agentctl.core import HOME
from agentctl.statefile import read_record, write_json
from agentctl.repos import RED_PRIMARY, red_repo_short
from agentctl.reverts import revert_line
from agentctl.redview import RED_STATUS_FRESH, red_status_snapshot_path
from agentctl.status import status_data, status_store


def render_red_status(m, verbose=False):
    """`agentctl red status` from the model's reds, families and dispatcher sections."""
    reds = m["reds"]
    if isinstance(reds, dict) and "error" in reds:
        return f"reds: unread ({reds['error']})"
    out = [f"reds ({len(reds)} open):" if reds else "reds: none open"]
    for r in reds:
        out.append(f"  {r['state']:<16} {r['what']} {r['key']}" + (f" [{r['family']}]" if r.get("family") else "")
                   + f": {r['text']}")
        if verbose:
            out += [f"      {h.get('at')} {h.get('holder')}: {str(h.get('outcome'))[:140]}" for h in r["history"]]
    rv = m.get("reverts")
    if isinstance(rv, dict) and "error" in rv:
        out.append(f"reverts: unread ({rv['error']})")
    elif rv:  # a reverted change its lander has not acknowledged (reverts.py)
        out.append(f"reverts ({len(rv)} unacked: `agentctl revert ack <id> --rework|--drop`):")
        out += [revert_line(r, time.time()) for r in rv]
    unread = {k: m[k]["error"] for k in ("families", "owners") if isinstance(m.get(k), dict) and "error" in m[k]}
    out += [f"{k}: unread ({e})" for k, e in unread.items()]  # a timed-out section (status_sources) is no list
    never = m.get("untracked")
    if verbose and isinstance(never, list) and never:  # for information only: no red, no fixer, no alert (reduntracked.py)
        out.append(f"untracked (never green) ({len(never)}):")
        for u in never:
            short = "" if u.get("repo") == RED_PRIMARY else f"{red_repo_short(u.get('repo') or '')} "
            out.append(f"  {short}{u.get('workflow')} #{(u.get('run') or {}).get('number')}"
                       + (f" [{', '.join(u.get('jobs') or [])}]" if u.get("jobs") else "") + f": {u.get('why')}")
    for f in [] if "families" in unread else m.get("families") or []:
        if f.get("holder") and f.get("at_work") is False:  # never "leased ... none at work": orphaned, re-offered
            who = f"ORPHANED LEASE ({f['holder']}: {f.get('work')}) -> revoked and re-offered at the next tick"
        elif f.get("holder"):
            who = (f"{f['holder']} ({str(f.get('worktree') or '?').replace(HOME, '~')})"
                   + (f": {f['work']}" if f.get("at_work") else ""))
        else:
            who = "none at work"
        out.append(f"family {f['family']}: {f['open']} open red(s), ONE fixer: {who}")
    for o in [] if "owners" in unread else m.get("owners") or []:
        if o.get("worktree"):
            out.append(f"owner {o['family']}: {'ALIVE' if o['alive'] else 'not running'}, turn {o.get('turn') or 0}, "
                       f"{o.get('reds') or 0} red(s) in hand, {o.get('restarts') or 0} restart(s); "
                       f"{str(o['worktree']).replace(HOME, '~')}" + (f" (ended: {o['verdict']})" if o.get("ended") else ""))
    dp = m.get("dispatcher") or {}
    if "error" in dp:
        out.append(f"dispatcher: unread ({dp['error']})")
    else:
        out.append(f"dispatcher: {'OFF -- ' + dp['blocker'] if dp.get('blocker') else 'ON'}; fixers alive: "
                   f"{dp['alive']}/{dp['cap']}; gate reds never wait (no slot cap, no laptop pressure)")
        for r, rec in sorted((dp.get("repos_off") or {}).items()):
            out.append(f"dispatch off for {r} (owner) since {rec.get('at')}: its reds recorded, never offered or asked")
        if dp.get("pressure"):
            out.append(f"laptop: {dp['pressure']} (holds other work only, never a red fix)")
    return "\n".join(out)


def red_status_text(store: Any, verbose: bool = False) -> str:
    """`agentctl red status`'s text, from the status model (its reds, families and dispatcher sections)."""
    t = store.clock() if hasattr(store, "clock") else time.time()
    return render_red_status(status_data(store=store, now_=t, only=("reds", "families", "owners", "dispatcher",
                                                                    "untracked", "reverts")), verbose)


def write_red_status_snapshot(store: Any) -> None:
    """The sync's step: the rendered red status, for `red status` to print at once."""
    write_json(red_status_snapshot_path(), {"at": time.time(), "text": red_status_text(store or status_store())})


def red_status(store: Any, verbose: bool = False, live: bool = False) -> int:
    """`agentctl red status`: the sync's fresh snapshot, else read now."""
    snap = {} if verbose or live else (read_record(red_status_snapshot_path()) or {})
    age = time.time() - float(snap.get("at") or 0)
    if snap.get("text") and 0 <= age <= RED_STATUS_FRESH:
        print(f"{snap['text']}\n(as of {age:.0f} s ago: the sync's snapshot; `agentctl red status --live` reads the store)")
        return 0
    print(red_status_text(store, verbose))
    return 0
