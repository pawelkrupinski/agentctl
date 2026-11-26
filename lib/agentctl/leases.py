# agentctl, part `leases`: the leases this process holds (LeaseHold): acquired, renewed together on progress (lock_beat), noted (lock_note), the landing lease among them; `agentctl lease list`
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from agentctl.clock import epoch_now
import os
from typing import Any, Optional
import re
from agentctl.core import process_cache
from agentctl.statefile import json_object
from agentctl.timefmt import ts
from agentctl.store import LeaseLost
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

    def __init__(self, name: str, ttl: float, max_hold: float, meta: dict[str, Any] | None = None, *, store: Any):
        self.name, self.ttl, self.max_hold, self.meta = name, ttl, max_hold, dict(meta or {}, pid=os.getpid())
        self.store = store
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


def lease_live(name: str, st: Any) -> dict[str, Any] | None:
    """The live lease `name` (its row, meta decoded), or None."""
    r: dict[str, Any] | None = st.get(name)
    if lease_is_live(r, st.clock()):
        assert r is not None
        r["meta"] = json_object(r["meta"])
        return r
    return None


def lease_meta(row: Optional[dict[str, Any]]) -> dict[str, Any]:
    try:
        return json_object((row or {}).get("meta"))
    except ValueError:
        return {}


class LeaseKinds:
    """WHICH KINDS OF A `red/` LEASE COUNT AS WHAT (the lease meta's `kind`): the one place the filters read it."""
    FIXER, SIBLING, CLAIM, OWNED = "fixer", "sibling", "claim", "owned"
    SESSIONS = (FIXER, SIBLING, OWNED)  # a session agentctl runs (or an owner's), working a red
    JOINED = (CLAIM, SIBLING)  # a red joined to its family holder's lease: released when it shows no progress

    @staticmethod
    def of(row: Optional[dict[str, Any]]) -> Optional[str]:
        kind: Optional[str] = lease_meta(row).get("kind")
        return kind

    @classmethod
    def is_session(cls, row: Optional[dict[str, Any]]) -> bool:
        return cls.of(row) in cls.SESSIONS

    @classmethod
    def is_joined(cls, row: Optional[dict[str, Any]]) -> bool:
        return cls.of(row) in cls.JOINED

    @classmethod
    def is_owned(cls, row: Optional[dict[str, Any]]) -> bool:
        return cls.of(row) == cls.OWNED

    @classmethod
    def is_claim(cls, row: Optional[dict[str, Any]]) -> bool:
        return cls.of(row) == cls.CLAIM

    @classmethod
    def holds_family(cls, row: Optional[dict[str, Any]]) -> bool:
        """A dispatched fixer's own lease, or a CLAIM made inside one (never an interactive session's)."""
        return cls.of(row) == cls.FIXER or (cls.is_claim(row) and bool(lease_meta(row).get("fixer")))


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


def _lease_recent(r: dict[str, Any], now_: float) -> bool:
    """Live, or free for no longer than LEASE_LIST_RECENT."""
    return lease_is_live(r, now_) or now_ - r["expires_at"] <= LEASE_LIST_RECENT


def _lease_line(r: dict[str, Any], now_: float) -> str:
    meta = json_object(r["meta"])
    return (f"{r['name']}  {'LIVE' if lease_is_live(r, now_) else 'free'}  {r['holder'] or '-'}  epoch {r['epoch']}  "
            f"expires in {int(r['expires_at'] - now_)} s  max {int(r['max_until'] - now_)} s"
            + (f"  -- {meta.get('note')}" if meta.get("note") else ""))


def cmd_lease(a, st):
    """`agentctl lease list [prefix] [--all]` (debugging; the leases' holders, epochs, expiry, notes): the live ones and
    those free for under an hour; --all every one still in the table."""
    now_ = epoch_now()
    rows = st.list(a.prefix or "")
    shown = rows if a.all else [r for r in rows if _lease_recent(r, now_)]
    for r in shown:
        print(_lease_line(r, now_))
    if not shown:
        print("no leases")
    if len(rows) > len(shown):
        print(f"({len(rows) - len(shown)} free for over {LEASE_LIST_RECENT // 60} min not shown: --all)")
    return 0
