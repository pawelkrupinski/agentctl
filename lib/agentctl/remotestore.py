# agentctl, part `remotestore`: the store over HTTP: RemoteStore, the phase-2 API's client, its retries of an unsent call, and the machine its calls speak for
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from agentctl.clock import epoch_now
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any
from agentctl.statefile import read_text_or
from agentctl.core import SHORT, attempt, env_num, host_role
from agentctl.store import StoreAuthFailed, StoreUnavailable, store_errors
from agentctl.storetoken import token
from agentctl.laptoprole import laptop_role_holder
from agentctl.core import state_root


def contact_machine():
    """The machine this process's store calls speak for (A SILENT MACHINE'S LEASE CLOCKS PAUSE): $AGENTCTL_MACHINE, else
    `laptop` for the laptop's agentctl (the sync role on macOS: its holders are `laptop:...`), else None."""
    m = os.environ.get("AGENTCTL_MACHINE")
    if m is not None:
        return m or None
    return "laptop" if host_role() == "sync" and sys.platform == "darwin" else None


class _StoreTransient(Exception):
    """One failed attempt of a store call; unsent: it never reached the server (safe to send again)."""

    def __init__(self, msg, unsent: bool = False) -> None:
        super().__init__(msg)
        self.unsent = unsent


def http_client_errors():
    import http.client
    return http.client.HTTPException


# TRANSIENT STORE FAILURES ARE RETRIED (2026-10-29: fixer -8's watcher died on one `POST /v1/store/get: [Errno 54]
# Connection reset by peer`). Reads and the calls that are harmless twice (a renew answers no_progress, a release of a
# released lease is a no-op) retry on any transient failure; the rest only when the request never left (refused).
STORE_RETRIES = 4


STORE_RETRY_BASE = 0.5   # s; doubled each attempt: 0.5 + 1 + 2 + 4 = 7.5 s at most


STORE_RPC_SAFE = ("get", "item", "items", "offers", "offer_of", "meta_get", "events", "item_events", "list", "renew", "note",
                  "set_meta", "release", "close_offer", "offer_reason", "mark_notified", "meta_set")


def _raise_http_error(e, method, path) -> None:
    """The store's answer to a failed call, as its exception: an auth failure, the store's own error class, a transient
    one (retried by `call`), else unavailable."""
    b: Any = attempt(lambda: json.loads(e.read() or b"{}"), {}, catch=(ValueError,))
    said = f"{method} {path}: HTTP {e.code} {b.get('message') or e.reason}"
    if e.code in (401, 403):
        raise StoreAuthFailed(said) from None
    cls = store_errors().get(b.get("error"))
    if cls:
        raise cls(b.get("message") or "")
    if e.code in (500, 502, 503, 504):  # the tunnel, the service between restarts, a busy database (a 500: retried
        # only when the call is safe -- `database is locked` after the 30 s busy timeout)
        raise _StoreTransient(said, e.code == 503) from None
    raise StoreUnavailable(said) from None


class RemoteStore:
    """Store's methods over the phase-2 API (`POST /v1/store/<method>`), plus the §6.3 calls the sync needs. The token
    is the caller's own store token ($AGENTCTL_STORE_TOKEN, else the one agentctl reads for movies)."""

    needs_token = True  # a call goes out with the caller's token (the file store reads a local database: Store.needs_token)

    def __init__(self, url, tok = None, clock = None, timeout: int = 60, retries = None) -> None:
        self.url, self._tok, self.clock, self.timeout = url.rstrip("/"), tok, clock or epoch_now, timeout
        self.retries = retries
        self.path = self.url

    def _token(self):
        return self._tok or os.environ.get("AGENTCTL_STORE_TOKEN") or token(SHORT["movies"])

    def call(self, method, path, body = None, timeout = None, safe = None):
        """One API call, retried through a transient failure (a reset, a refusal, a timeout, a 502/503/504) up to
        STORE_RETRIES times with backoff. A call that may have taken effect (a mutation whose request was sent) is
        retried only when it never reached the server (refused). What is still failing after that raises
        StoreUnavailable: no verdict either way -- never "lost"."""
        safe = (method == "GET") if safe is None else safe
        tries = int(env_num("AGENTCTL_STORE_RETRIES", STORE_RETRIES)) if self.retries is None else self.retries
        for attempt in range(tries + 1):
            try:
                return self._call_once(method, path, body, timeout)
            except _StoreTransient as e:
                if attempt == tries or not (safe or e.unsent):
                    raise StoreUnavailable(str(e)) from None
                # wall: waits on and times real processes and threads
                time.sleep(env_num("AGENTCTL_STORE_RETRY_BASE", STORE_RETRY_BASE) * (2 ** attempt))

    def _headers(self):
        hdrs = {"Authorization": f"token {self._token()}", "Content-Type": "application/json"}
        if contact_machine():
            hdrs["X-Agentctl-Machine"] = contact_machine()
        if host_role() == "sync":  # which machine this laptop is (laptop_role_refusal): every sync client names it
            hdrs["X-Agentctl-Role"] = laptop_role_holder()
        return hdrs

    def _call_once(self, method, path, body = None, timeout = None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.url + path, data=data, method=method, headers=self._headers())
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as r:
                return json.loads(r.read() or b"null")
        except urllib.error.HTTPError as e:
            _raise_http_error(e, method, path)
        except (urllib.error.URLError, OSError, ValueError, http_client_errors()) as e:
            reason = getattr(e, "reason", e)
            raise _StoreTransient(f"{method} {path}: {e}", isinstance(reason, ConnectionRefusedError)) from None

    def _rpc(self, m, args, kwargs):
        return self.call("POST", f"/v1/store/{m}", {"args": list(args), "kwargs": kwargs}, safe=m in STORE_RPC_SAFE)["result"]

    def state(self):
        return self.call("GET", "/v1/state")

    def submit(self, entry):
        return self.call("POST", "/v1/changes", entry, safe=bool(entry.get("id")))  # an id: the same change twice

    def cancel_change(self, eid, how: str = "cancel"):
        return self.call("POST", f"/v1/changes/{urllib.parse.quote(eid, safe='')}:{how}", {})

    def nudge(self):
        return self.call("POST", "/v1/tick", {}, safe=True)


STORE_PORT = 3014


DEFAULT_STORE_URL = f"http://localhost:{STORE_PORT}"  # ci-runner's store through the laptop's tunnel


def store_url() -> str | None:
    """The store a sync process uses: $AGENTCTL_STORE_URL, else the one `agentctl store use URL` wrote (state root's
    store.url, so every agentctl process -- agents' `land` and `red claim` included -- agrees), else ci-runner's through
    the tunnel. None on the server: its database is in-process."""
    if host_role() == "server":
        return None
    if os.environ.get("AGENTCTL_STORE_URL"):
        return os.environ["AGENTCTL_STORE_URL"]
    return read_text_or(state_root("store.url"), "").strip() or default_store_url()


def default_store_url() -> str:
    """ci-runner's store through the tunnel; $AGENTCTL_STORE_DEFAULT_URL overrides (the tests: a closed port)."""
    return os.environ.get("AGENTCTL_STORE_DEFAULT_URL") or DEFAULT_STORE_URL
