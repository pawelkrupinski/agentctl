# agentctl, part `leases`: the leases this process holds (LeaseHold): acquired, renewed together on progress (lock_beat), noted (lock_note), the landing lease among them; `agentctl lease list`
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import os
from typing import Any, Optional
import json
import time
import re
from agentctl.core import process_cache
from agentctl.timefmt import ts
from agentctl.store import LeaseLost, open_store
from agentctl.procs import socket_host
from agentctl.redrecord import lease_is_live


_landing: list["LeaseHold | None"] = [None]  # the LeaseHold of the landing lease this process holds (land_lock)


def lock_note(step: str) -> None:
    """Say in the held landing lease what the holder is doing now (waiters print it)."""
    h = _landing[0]
    if h and h.lease:
        h.store.note(h.name, h.lease["epoch"], h.lease["secret"], f"{step} since {ts()}")


def lock_beat(progress: Any = None) -> None:
    """The holder at work renews every lease this process holds -- the landing lease, a check slot, a change's check --
    with its progress mark (run_watched: output bytes and CPU). Raises LeaseLost when one is gone: the work must stop."""
    if progress is None:
        return
    for h in list(_held_leases):
        if h.renew(progress) == "lost":
            raise LeaseLost(f"the {'landing ' if h is _landing[0] else ''}lease {h.name} (epoch {(h.lease or {}).get('epoch')}) "
                            "was lost")


_held_leases: list["LeaseHold"] = process_cache([])  # the LeaseHold objects this process holds (renewed together on progress)


class LeaseHold:
    """One lease this process holds: acquired (or not: held elsewhere), renewed on progress, released at the end."""

    def __init__(self, name: str, ttl: float, max_hold: float, meta: dict[str, Any] | None = None, store: Any = None):
        self.name, self.ttl, self.max_hold, self.meta = name, ttl, max_hold, dict(meta or {}, pid=os.getpid())
        self.store = store or open_store()
        self.lease: dict[str, Any] | None = None
        self.held: Any = None

    def acquire(self) -> bool:
        got = self.store.acquire(self.name, f"{socket_host()}:{self.name.split('/')[0]}:{os.getpid()}", self.ttl,
                                 self.max_hold, progress="acquired", meta=self.meta)
        if "held" in got:
            self.held = got["held"]
            return False
        self.lease = got
        _held_leases.append(self)
        return True

    def renew(self, progress: Any) -> str:
        if not self.lease:
            return "lost"
        return str(self.store.renew(self.name, self.lease["epoch"], self.lease["secret"], progress, self.ttl))

    def release(self) -> None:
        if self in _held_leases:
            _held_leases.remove(self)
        if self.lease:
            self.store.release(self.name, self.lease["epoch"], self.lease["secret"])
            self.lease = None


def lease_live(name: str, store: Any = None) -> dict[str, Any] | None:
    """The live lease `name` (its row, meta decoded), or None."""
    st = store or open_store()
    r: dict[str, Any] | None = st.get(name)
    if r and r["holder"] and r["expires_at"] > st.clock():
        r["meta"] = json.loads(r["meta"] or "{}")
        return r
    return None


def lease_meta(row: Optional[dict[str, Any]]) -> dict[str, Any]:
    try:
        meta: dict[str, Any] = json.loads((row or {}).get("meta") or "{}")
        return meta
    except ValueError:
        return {}


def block_lease(key: Optional[str] = None, cause: Optional[str] = None) -> str:
    return ("block/cause/" + re.sub(r'[^\w.-]+', '_', cause)) if cause else f"block/{key}"


def red_block_of(it: dict[str, Any], store: Any) -> Optional[dict[str, Any]]:
    """{name, text, cause, until, meta} of the live block lease holding this red, or None."""
    st = store
    d = it["data"]
    for row in st.list("block/"):
        if lease_is_live(row, st.clock()):
            m = lease_meta(row)
            if d.get("key") in (m.get("keys") or []):
                return {"name": row["name"], "text": m.get("text") or "BLOCKED ON OWNER", "cause": m.get("cause"),
                        "until": m.get("until"), "meta": m}
    return None


LEASE_LIST_RECENT = 3600         # `lease list` shows the live leases and those free for less than this (--all: every one)


def cmd_lease(a, st):
    """`agentctl lease list [prefix] [--all]` (debugging; the leases' holders, epochs, expiry, notes): the live ones and
    those free for under an hour; --all every one still in the table."""
    now_ = time.time()
    rows = st.list(a.prefix or "")
    hidden = 0 if a.all else sum(1 for r in rows if not (r["holder"] and r["expires_at"] > now_)
                                 and now_ - r["expires_at"] > LEASE_LIST_RECENT)
    if not a.all:
        rows = [r for r in rows if (r["holder"] and r["expires_at"] > now_) or now_ - r["expires_at"] <= LEASE_LIST_RECENT]
    for r in rows:
        live = r["holder"] and r["expires_at"] > now_
        meta = json.loads(r["meta"] or "{}")
        print(f"{r['name']}  {'LIVE' if live else 'free'}  {r['holder'] or '-'}  epoch {r['epoch']}  "
              f"expires in {int(r['expires_at'] - now_)} s  max {int(r['max_until'] - now_)} s"
              + (f"  -- {meta.get('note')}" if meta.get("note") else ""))
    if not rows:
        print("no leases")
    if hidden:
        print(f"({hidden} free for over {LEASE_LIST_RECENT // 60} min not shown: --all)")
    return 0
