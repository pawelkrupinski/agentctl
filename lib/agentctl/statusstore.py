# agentctl, part `statusstore`: the status model's store: opened under short timeouts, probed for its latency
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from typing import Any
from agentctl.store import Store
from agentctl.remotestore import RemoteStore, store_url


STATUS_STORE_TIMEOUT = 5        # s per remote store call


def status_store() -> Store | RemoteStore:
    """The store the page reads: the remote one with short timeouts when the laptop is the sync, else the local one."""
    url = store_url()
    return RemoteStore(url, timeout=STATUS_STORE_TIMEOUT) if url else Store()


def status_error(e: BaseException) -> dict[str, str]:
    return {"error": f"{type(e).__name__}: {str(e).replace('agentctl: ', '')[:300]}"}


def open_status_store(store: Store | RemoteStore | None, only: Any) -> tuple[Store | RemoteStore | None, dict[str, str] | None]:
    """(the store the sections read, None) or (None, the error that kept it from opening); no store for a header-only read."""
    if only and not set(only) - {"header"}:
        return None, None
    try:
        return (store if store is not None else status_store()), None
    except (Exception, SystemExit) as e:
        return None, status_error(e)


def status_store_failed(section: dict[str, Any]) -> dict[str, Any]:
    """The store section as read: a read that failed also says which store it was and that it did not answer."""
    return {**section, "remote": store_url(), "reachable": False} if "error" in section else section
