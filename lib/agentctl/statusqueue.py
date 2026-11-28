# agentctl, part `statusqueue`: the status model's landing, conditions and events sections
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from agentctl.landhold import hold_row, land_hold
from agentctl.conditions import open_conditions
from agentctl.changekind import HOLD_SHOWN


STATUS_EVENTS = 50


STATUS_CHANGE_OPEN = ("queued", "checking", "checked", "pushing", "waiting")


STATUS_BATCH_OPEN = ("blocked-red", "assembling", "checking", "pushing", "bisecting", "held")


def status_landing(st: Any, t: Any) -> Any:
    """The open change items and the batch, each with its deadline (and whether it is past)."""
    def row(it: Any) -> Any:
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


def status_conditions(st: Any, t: Any) -> Any:
    out = []
    for c in open_conditions(st):
        lr = c.get("last_result") or {}
        out.append({"kind": c.get("kind"), "key": c.get("key"), "text": str(c.get("text") or "")[:400],
                    "opened": c.get("opened"), "level": c.get("level"), "until": c.get("until"),
                    "probe": (c.get("probe") or {}).get("kind"), "ok": lr.get("ok"), "detail": lr.get("detail"),
                    "last_checked": c.get("last_checked"), "next_check": c.get("next_check"), "fails": c.get("fails")})
    return out


def status_events(st: Any, n: int = STATUS_EVENTS) -> Any:
    """The last n events of the feed (from just before the projector's cursor on, a few pages at most)."""
    after = max(0, int(st.meta_get("projector_seq", 0) or 0) - n)
    evs: list[Any] = []
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
