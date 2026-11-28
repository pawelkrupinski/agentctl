# agentctl, part `status`: the status model: each source read under a bound, assembled and cached; the status line
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from typing import Any
import os
import threading
import time
from agentctl.caches import StatusMemos
from agentctl.clock import epoch_now
from agentctl.watchknobs import knob_status_bound
from agentctl.core import HOME, SHORT
from agentctl.cistate import state_read
from agentctl.machine import MachineProbe
from agentctl.store import Store
from agentctl.paths import reds_dir
from agentctl.remotestore import RemoteStore, store_url
from agentctl.statussections import status_section_sources
from agentctl.statusmachine import status_dispatcher


STATUS_STORE_TIMEOUT = 5        # s per remote store call
STATUS_CACHE = 5                # s an assembled status is served again


def status_store():
    """The store the page reads: the remote one with short timeouts when the laptop is the sync, else the local one."""
    url = store_url()
    return RemoteStore(url, timeout=STATUS_STORE_TIMEOUT) if url else Store()


def status_error(e):
    return {"error": f"{type(e).__name__}: {str(e).replace('agentctl: ', '')[:300]}"}


def status_sources(sources, bound, memos: StatusMemos, keep=False):
    """({name: what fn returned, or {"error": ...}}, {"stale": {name: why}, "took": {name: s}}): every (name, fn, factor)
    in a daemon thread of its own, joined against one deadline (bound * factor s each). keep (the server's): a source
    still reading then shows its LAST finished read, `stale` saying since when, and its read goes on in the background
    for the next refresh (one read per source at a time) -- a slow source never empties its section; else it is an
    error. memos: the status reads' memory (the last read, the read under way, of each source)."""
    out, threads = {}, []
    start = time.monotonic()  # wall: a thread join's bound is real time, whatever the injected clock says
    for name, fn, factor in sources:
        th = memos.reading.get(name) if keep else None
        if not (th and th.is_alive()):
            th = threading.Thread(target=status_read, args=(name, fn, out, keep, memos.last), daemon=True,
                                  name=f"status:{name}")
            th.start()
            if keep:
                memos.reading[name] = th
        threads.append((name, th, bound * factor))
    for _name, th, b in threads:
        th.join(max(0.0, start + b - time.monotonic()))  # wall: as above
    return status_answers(threads, out, keep, memos.last)


def status_read(name: Any, fn: Any, out: Any, keep: Any, last: dict[str, Any]) -> Any:
    """One source read in its thread: its answer (or its error) and how long it took, into out (and, keep, the last)."""
    t0 = time.monotonic()  # wall: how long the read really took
    try:
        v = fn()
    except (Exception, SystemExit) as e:
        v = status_error(e)
    out[name] = (epoch_now(), v, round(time.monotonic() - t0, 2))  # wall: as above
    if keep:
        last[name] = out[name]


def status_answers(threads: Any, out: Any, keep: Any, last: dict[str, Any]) -> Any:
    """Each source's answer once the deadline passed: its read, else (keep) its last finished one marked stale, else an
    error."""
    res, stale, took = {}, {}, {}
    for name, th, b in threads:
        got = out.get(name) or (last.get(name) if keep and not th.is_alive() else None)
        if got:
            res[name], took[name] = got[1], got[2]
        elif keep and name in last:
            at, res[name], _took = last[name]
            stale[name] = f"still reading after {b:.0f} s; shown as of {time.strftime('%H:%M:%S', time.localtime(at))}"
        else:
            res[name] = {"error": f"no answer within {b:.0f} s"}
    return res, {"stale": stale, "took": took}


def status_store_probe(st, t):
    """Is the store answering, and the reconciler's own tick (the remote /v1/state's tick_at) when it is remote."""
    t0 = time.monotonic()  # wall: the store's real latency
    if isinstance(st, RemoteStore):
        snap = st.state() or {}
        tick = snap.get("tick_at")
        return {"remote": st.url, "reachable": True, "ms": int((time.monotonic() - t0) * 1000),  # wall: as above
                "tick_at": tick,
                "tick_age": t - float(tick) if tick else None, "code": snap.get("version")}
    st.meta_get("last_alive", 0)
    return {"remote": None, "reachable": True, "ms": int((time.monotonic() - t0) * 1000)}  # wall: as above


def open_status_store(store: Any, only: Any) -> tuple[Any, Any]:
    """(the store the sections read, None) or (None, the error that kept it from opening); no store for a header-only read."""
    if only and not set(only) - {"header"}:
        return None, None
    try:
        return (store if store is not None else status_store()), None
    except (Exception, SystemExit) as e:
        return None, status_error(e)


def status_data(store=None, now_=None, bound=None, keep=False, only=None, *, machine, memos):
    """THE STATUS MODEL (/api/status): every section from its own bounded source; a failing one is {"error": ...} in
    its place. only: just these sections (a renderer's). memos: the status reads' memory (StatusMemos)."""
    t = now_ if now_ is not None else epoch_now()
    b = bound if bound is not None else knob_status_bound()
    d = state_read()
    repo = (d.get("ci") or {}).get("repo") or SHORT["movies"]
    st, st_err = open_status_store(store, only)

    def need():
        if st is None:
            raise RuntimeError(st_err["error"])
        return st
    sources = status_section_sources(t, repo, need, memos, store_probe=status_store_probe,
                                       dispatcher=lambda st, now: status_dispatcher(st, now, machine, memos.blocker,
                                                                           off_flag=os.path.exists(reds_dir("DISPATCH_OFF"))))
    res, meta = status_sources(tuple(x for x in sources if not only or x[0] in only), b, memos, keep=keep)
    if "error" in res.get("store", {}):
        res["store"] = {**res["store"], "remote": store_url(), "reachable": False}
    return {"at": t, "repo": repo, "home": HOME, **res, **meta}


_status_cache_lock = threading.Lock()


def status_cached(machine: MachineProbe, memos: StatusMemos):
    """The status model, assembled again past STATUS_CACHE (memos.assembled: the last one)."""
    model = memos.assembled
    with _status_cache_lock:  # one assembly at a time: refreshing tabs share it
        if model.get("data") is None or epoch_now() - model.get("at", 0.0) >= STATUS_CACHE:
            model["data"] = status_data(keep=True, machine=machine, memos=memos)
            model["at"] = epoch_now()
        return model["data"]


