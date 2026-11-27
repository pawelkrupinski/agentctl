# agentctl, part `processstore`: the process's store: the remote store when a store URL is set, else the file store
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from typing import Any
from agentctl.store import Store
from agentctl.remotestore import RemoteStore, STORE_WAITED_TIMEOUT, store_url


def process_store(clock: Any = None, waited_on: bool = False) -> Any:
    """The store this process uses (cli hands it to CurrentStore as its opener): the remote store when a store URL is set
    (store_url: every sync process), else the file store (the server's own database). waited_on (a command someone
    waits on, cli waited_on): each call bounded by STORE_WAITED_TIMEOUT and never retried, so it fails fast."""
    url = store_url()
    if not url:
        return Store(clock=clock)
    return RemoteStore(url, clock=clock, timeout=STORE_WAITED_TIMEOUT, retries=0) if waited_on else RemoteStore(url, clock=clock)
