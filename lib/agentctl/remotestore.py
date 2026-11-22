# agentctl, part `remotestore`: the store over HTTP: RemoteStore, the phase-2 API's client, its retries of an unsent call, and the machine its calls speak for
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from agentctl.core import SHORT, env_num, host_role
from agentctl.store import StoreAuthFailed, StoreUnavailable, store_errors
from agentctl.storetoken import token
from agentctl.laptoprole import laptop_role_holder


def contact_machine():
    """The machine this process's store calls speak for (A SILENT MACHINE'S LEASE CLOCKS PAUSE): $AGENTCTL_MACHINE, else
    `laptop` for the laptop's agentctl (the sync role on macOS: its holders are `laptop:...`), else None."""
    m = os.environ.get("AGENTCTL_MACHINE")
    if m is not None:
        return m or None
    return "laptop" if host_role() == "sync" and sys.platform == "darwin" else None


class _StoreTransient(Exception):
    """One failed attempt of a store call; unsent: it never reached the server (safe to send again)."""

    def __init__(self, msg, unsent=False):
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


class RemoteStore:
    """Store's methods over the phase-2 API (`POST /v1/store/<method>`), plus the §6.3 calls the sync needs. The token
    is the caller's own store token ($AGENTCTL_STORE_TOKEN, else the one agentctl reads for movies)."""

    def __init__(self, url, tok=None, clock=None, timeout=60, retries=None):
        self.url, self._tok, self.clock, self.timeout = url.rstrip("/"), tok, clock or time.time, timeout
        self.retries = retries
        self.path = self.url

    def _token(self):
        return self._tok or os.environ.get("AGENTCTL_STORE_TOKEN") or token(SHORT["movies"])

    def call(self, method, path, body=None, timeout=None, safe=None):
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
                time.sleep(env_num("AGENTCTL_STORE_RETRY_BASE", STORE_RETRY_BASE) * (2 ** attempt))

    def _call_once(self, method, path, body=None, timeout=None):
        data = json.dumps(body).encode() if body is not None else None
        hdrs = {"Authorization": f"token {self._token()}", "Content-Type": "application/json"}
        if contact_machine():
            hdrs["X-Agentctl-Machine"] = contact_machine()
        if host_role() == "sync":  # which machine this laptop is (laptop_role_refusal): every sync client names it
            hdrs["X-Agentctl-Role"] = laptop_role_holder()
        req = urllib.request.Request(self.url + path, data=data, method=method, headers=hdrs)
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as r:
                return json.loads(r.read() or b"null")
        except urllib.error.HTTPError as e:
            try:
                b = json.loads(e.read() or b"{}")
            except ValueError:
                b = {}
            if e.code in (401, 403):
                raise StoreAuthFailed(f"{method} {path}: HTTP {e.code} {b.get('message') or e.reason}") from None
            cls = store_errors().get(b.get("error"))
            if cls:
                raise cls(b.get("message") or "")
            if e.code in (500, 502, 503, 504):  # the tunnel, the service between restarts, a busy database (a 500:
                # retried only when the call is safe -- `database is locked` after the 30 s busy timeout)
                raise _StoreTransient(f"{method} {path}: HTTP {e.code} {b.get('message') or e.reason}", e.code == 503) \
                    from None
            raise StoreUnavailable(f"{method} {path}: HTTP {e.code} {b.get('message') or e.reason}") from None
        except (urllib.error.URLError, OSError, ValueError, http_client_errors()) as e:
            reason = getattr(e, "reason", e)
            raise _StoreTransient(f"{method} {path}: {e}", isinstance(reason, ConnectionRefusedError)) from None

    def _rpc(self, m, args, kwargs):
        return self.call("POST", f"/v1/store/{m}", {"args": list(args), "kwargs": kwargs}, safe=m in STORE_RPC_SAFE)["result"]

    def state(self):
        return self.call("GET", "/v1/state")

    def submit(self, entry):
        return self.call("POST", "/v1/changes", entry, safe=bool(entry.get("id")))  # an id: the same change twice

    def cancel_change(self, eid, how="cancel"):
        return self.call("POST", f"/v1/changes/{urllib.parse.quote(eid, safe='')}:{how}", {})

    def nudge(self):
        return self.call("POST", "/v1/tick", {}, safe=True)
