# agentctl, part `changerows`: the queue as everyone reads it: the store's open change items as rows (land status, the status line, the page)
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from typing import Any
from agentctl.statemachines import open_items
from agentctl.changekind import HOLD_SHOWN, kind_text


def _entry_created(i: dict[str, Any]) -> float:
    """When the change's own queue entry was created (a change item from before `entry_created_at`: its queue age)."""
    data = i.get("data") or {}
    return float(data.get("entry_created_at") or data.get("submitted_at") or i.get("entered_at") or 0)


def _change_row(i: dict[str, Any]) -> dict[str, Any]:
    """One open change item as its row of `change_rows`."""
    data = i.get("data") or {}
    return {"id": i["id"], "state": i["state"], "worktree": data.get("worktree"), "head": data.get("head"),
            "deadline": i["deadline"], "level": i["level"],
            "hold": (data.get("hold") or {}).get("why") if i["state"] in HOLD_SHOWN else None,
            "kind": kind_text(data.get("change_kind")) or None,
            "resolved": data.get("autoresolved") if i["state"] == "checking" else None,
            "since": float(data.get("submitted_at") or i.get("entered_at") or 0),
            "created": _entry_created(i)}


def change_rows(store: Any) -> list[dict[str, Any]]:
    """THE QUEUE as everyone reads it (`land status`, the status line, the page): the store's open change items,
    by id, as {id, state, worktree, head, deadline, level, hold, kind (a red fix's, changekind.py), since (submitted: the queue age a kept place
    inherits), created (this entry's own creation)}."""
    rows = [_change_row(i) for i in open_items(store, "change")]
    return sorted(rows, key=lambda r: r["id"])
