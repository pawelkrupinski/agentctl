# agentctl, part `redstatus`: `agentctl red status`: its text from the status model, and the snapshot the tick writes of it
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from agentctl.clock import epoch_now
from typing import Any
from agentctl.core import tilde
from agentctl.statefile import read_record, write_json
from agentctl.repos import RED_PRIMARY, red_repo_short
from agentctl.reverts import revert_line
from agentctl.redview import RED_STATUS_FRESH, red_status_snapshot_path
from agentctl.status import status_data, status_store
from agentctl.redhold import red_fix_liveness


def red_lines(reds: list[dict[str, Any]], verbose: bool) -> list[str]:
    """The open reds, each with its history when verbose."""
    out = [f"reds ({len(reds)} open):" if reds else "reds: none open"]
    for r in reds:
        out.append(f"  {r['state']:<16} {r['what']} {r['key']}" + (f" [{r['family']}]" if r.get("family") else "")
                   + f": {r['text']}")
        if verbose:
            out += [f"      {h.get('at')} {h.get('holder')}: {str(h.get('outcome'))[:140]}" for h in r["history"]]
    return out


def revert_lines(rv: Any) -> list[str]:
    """The reverted changes their landers have not acknowledged (reverts.py), or why they are unread."""
    if isinstance(rv, dict) and "error" in rv:
        return [f"reverts: unread ({rv['error']})"]
    if not rv:
        return []
    return [f"reverts ({len(rv)} unacked: `agentctl revert ack <id> --rework|--drop`):"] + [revert_line(r, epoch_now()) for r in rv]


def untracked_lines(never: Any) -> list[str]:
    """The workflows never green, for information only: no red, no fixer, no alert (reduntracked.py)."""
    if not (isinstance(never, list) and never):
        return []
    out = [f"untracked (never green) ({len(never)}):"]
    for u in never:
        short = "" if u.get("repo") == RED_PRIMARY else f"{red_repo_short(u.get('repo') or '')} "
        out.append(f"  {short}{u.get('workflow')} #{(u.get('run') or {}).get('number')}"
                   + (f" [{', '.join(u.get('jobs') or [])}]" if u.get("jobs") else "") + f": {u.get('why')}")
    return out


def family_line(f: dict[str, Any]) -> str:
    """A red family's line: its ONE fixer, an orphaned lease revoked, or none at work."""
    if f.get("holder") and f.get("at_work") is False:  # never "leased ... none at work": orphaned, re-offered
        who = f"ORPHANED LEASE ({f['holder']}: {f.get('work')}) -> revoked and re-offered at the next tick"
    elif f.get("holder"):
        who = (f"{f['holder']} ({tilde(str(f.get('worktree') or '?'))})"
               + (f": {f['work']}" if f.get("at_work") else ""))
    else:
        who = "none at work"
    return f"family {f['family']}: {f['open']} open red(s), ONE fixer: {who}"


def dispatcher_lines(dp: dict[str, Any]) -> list[str]:
    """The dispatcher: on or off and why, the fixers alive, the repos switched off, the laptop's pressure."""
    if "error" in dp:
        return [f"dispatcher: unread ({dp['error']})"]
    out = [f"dispatcher: {'OFF -- ' + dp['blocker'] if dp.get('blocker') else 'ON'}; fixers alive: "
           f"{dp['alive']}/{dp['cap']}; gate reds never wait (no slot cap, no laptop pressure)"]
    for r, rec in sorted((dp.get("repos_off") or {}).items()):
        out.append(f"dispatch off for {r} (owner) since {rec.get('at')}: its reds recorded, never offered or asked")
    if dp.get("pressure"):
        out.append(f"laptop: {dp['pressure']} (holds other work only, never a red fix)")
    return out


def unread_sections(m: dict[str, Any]) -> dict[str, Any]:
    """The families and owners sections that came back as an error (a timed-out section, status_sources, is no list)."""
    return {k: m[k]["error"] for k in ("families", "owners") if isinstance(m.get(k), dict) and "error" in m[k]}


def holder_lines(m: dict[str, Any], unread: dict[str, Any], verbose: bool) -> list[str]:
    """The families' ONE fixers and the owners' lines, each section left out when it was unread."""
    out = [] if "families" in unread else [family_line(f) for f in m.get("families") or []]
    for o in [] if "owners" in unread else m.get("owners") or []:
        out += owner_status_lines(o, verbose) if o.get("worktree") else []
    return out


def render_red_status(m: dict[str, Any], verbose: bool = False) -> str:
    """`agentctl red status` from the model's reds, families and dispatcher sections."""
    reds = m["reds"]
    if isinstance(reds, dict) and "error" in reds:
        return f"reds: unread ({reds['error']})"
    unread = unread_sections(m)
    out = red_lines(reds, verbose) + revert_lines(m.get("reverts")) + [f"{k}: unread ({e})" for k, e in unread.items()]
    out += untracked_lines(m.get("untracked")) if verbose else []
    return "\n".join(out + holder_lines(m, unread, verbose) + dispatcher_lines(m.get("dispatcher") or {}))


def owner_status_lines(o: dict[str, Any], verbose: bool) -> list[str]:
    """An owner's line; verbose, its session's context and its last rollovers to a fresh session (sessionroll)."""
    line = (f"owner {o['family']}: {'ALIVE' if o['alive'] else 'not running'}, turn {o.get('turn') or 0}, "
            f"{o.get('reds') or 0} red(s) in hand, {o.get('restarts') or 0} restart(s); "
            f"{tilde(str(o['worktree']))}" + (f" (ended: {o['verdict']})" if o.get("ended") else ""))
    return [line] + (owner_context_lines(o) if verbose else [])


def owner_context_lines(o: dict[str, Any]) -> list[str]:
    """The owner's session context at its last resume check, and its last three rollovers."""
    return ([f"      context at its last resume check: {o.get('context') or 'unread'} tokens"]
            + [f"      rollover {r.get('at')}: {str(r.get('old'))[:8]} -> {str(r.get('new') or 'starting')[:8]} "
               f"({r.get('tokens')} tokens)" for r in (o.get("rollovers") or [])[-3:]])


def red_status_text(store: Any, verbose: bool = False) -> str:
    """`agentctl red status`'s text, from the status model (its reds, families and dispatcher sections)."""
    t = store.clock() if hasattr(store, "clock") else epoch_now()
    try:
        stalled = red_fix_liveness(store, t)
    except (Exception, SystemExit):
        stalled = None
    return (f"{stalled}\n" if stalled else "") + render_red_status(status_data(store=store, now_=t, only=("reds", "families", "owners", "dispatcher",
                                                                    "untracked", "reverts")), verbose)


def write_red_status_snapshot(store: Any) -> None:
    """The sync's step: the rendered red status, for `red status` to print at once."""
    write_json(red_status_snapshot_path(), {"at": epoch_now(), "text": red_status_text(store or status_store())})


def red_status(store: Any, verbose: bool = False, live: bool = False) -> int:
    """`agentctl red status`: the sync's fresh snapshot, else read now."""
    snap = {} if verbose or live else (read_record(red_status_snapshot_path()) or {})
    age = epoch_now() - float(snap.get("at") or 0)
    if snap.get("text") and 0 <= age <= RED_STATUS_FRESH:
        print(f"{snap['text']}\n(as of {age:.0f} s ago: the sync's snapshot; `agentctl red status --live` reads the store)")
        return 0
    print(red_status_text(store, verbose))
    return 0
