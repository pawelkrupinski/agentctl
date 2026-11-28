# agentctl, part `statusrender`: the status model rendered as the page's text
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from typing import Any, Dict
from agentctl.reverts import revert_line
from agentctl.park import parked_lines


def code_text(h: dict[str, Any]) -> str:
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


def render_status(h: dict[str, Any], t: float) -> str:
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
