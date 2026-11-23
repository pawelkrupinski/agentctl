# agentctl, part `storestate`: the store as the status line sees it: its last unanswered call, ci-runner's state mirrored to the laptop, the remote mode's line state, a port's probe
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import time
from agentctl.store import StoreUnavailable
from agentctl.cistate import state_read, state_update
from agentctl.remotestore import RemoteStore


NETWORK_SHOWN_FOR = 600  # a lossy-uplink reading this recent turns the status line's UNREAD into NETWORK


STATUS_LIVE_AFTER = 30   # s: a mirror of ci-runner's /v1/state older than this is read live by the status line


STORE_UNREACHABLE_AFTER = 60  # s without a good read of ci-runner's /v1/state: the line says STORE UNREACHABLE


def note_store_error(e, t=None):
    """The remote store did not answer: kept in state.json (since when, why) for the status line."""
    t = t if t is not None else time.time()
    prev = state_read().get("store_error") or {}
    state_update(store_error={"since": prev.get("since") or t, "at": t, "why": str(e).replace("agentctl: ", "")[:200]})


def remote_status_state(url, t):
    """THE STATUS LINE IN REMOTE MODE (2026-10-29 cutover: the line said RECONCILER STALLED since 13:01 while ci-runner's
    reconciler ticked fine -- the laptop's state.json is only a mirror, and the mirror had stopped). state.json as the
    sync mirrored it, read live from GET /v1/state when the mirror is over STATUS_LIVE_AFTER s old (at most once per
    STATUS_LIVE_AFTER s while the store does not answer); a failed read is recorded as store_error."""
    d = state_read()
    last_try = max(float(d.get("fetched_at") or 0), float((d.get("store_error") or {}).get("at") or 0))
    if t - last_try > STATUS_LIVE_AFTER:
        try:
            mirror_state(RemoteStore(url, timeout=5, retries=0), t)  # the status line never waits out a retry
        except (Exception, SystemExit) as e:
            note_store_error(e, t)
        d = state_read()
    return d


def port_open(port, host="127.0.0.1", timeout=2.0):
    import socket
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def mirror_state(st, now_=None):
    """ci-runner's /v1/state copied into the laptop's state.json (with fetched_at): the status line reads it."""
    try:
        snap = st.state()
    except StoreUnavailable as e:
        note_store_error(e, now_)
        raise
    state_update(**{**snap, "fetched_at": now_ if now_ is not None else time.time(), "store_error": None})
    return snap
