# agentctl, part `remotestore`: the store over HTTP: RemoteStore, the phase-2 API's client, its retries of an unsent call, and the machine its calls speak for
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import json
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable
from agentctl.statefile import read_json_file, read_text_or, write_json_file
from agentctl.storecalls import STORE_CALLS, StoreCall
from agentctl.clock import epoch_now
from agentctl.storeknobs import knob_store_retries, knob_store_retry_base, knob_store_url
from agentctl.core import SHORT, attempt, host_role
from agentctl.store import StoreAuthFailed, StoreLacksCall, StoreUnavailable, store_errors
from agentctl.storetoken import token
from agentctl.laptoprole import laptop_role_holder
from agentctl.paths import state_root


def contact_machine() -> str | None:
    """The machine this process's store calls speak for (A SILENT MACHINE'S LEASE CLOCKS PAUSE): `laptop` for the
    laptop's agentctl (the sync role on macOS: its holders are `laptop:...`), else None. ($AGENTCTL_MACHINE is the
    tests' fixed machine, JSON: machine.machine_probe; it once named this too, so every test's store call spoke for a
    machine named by that JSON.)"""
    return "laptop" if host_role() == "sync" and sys.platform == "darwin" else None


class _StoreTransient(Exception):
    """One failed attempt of a store call; unsent: it never reached the server (safe to send again)."""

    def __init__(self, msg, unsent: bool = False) -> None:
        super().__init__(msg)
        self.unsent = unsent


def http_client_errors():
    import http.client
    return http.client.HTTPException


def open_within(req: urllib.request.Request, timeout: float) -> bytes:
    """The answer's body, read within `timeout` s IN ALL. urlopen's timeout bounds each socket read, so a link
    trickling a byte now and then never timed out (2026-11-27, the e2e slow link: `status` over a throttled proxy was
    still reading after 45 s against its 15 s bound). The read runs in a daemon thread the caller waits on for
    `timeout`: past it the call fails as a timeout like any other (the thread ends with its connection, or the
    process). Its errors are the caller's, as raised."""
    got: dict[str, Any] = {}

    def fetch() -> None:
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                got["body"] = r.read()
        except BaseException as e:  # handed to the caller, raised there
            got["error"] = e
    reader = threading.Thread(target=fetch, name="store-call", daemon=True)
    reader.start()
    reader.join(timeout)
    if reader.is_alive():
        raise TimeoutError(f"no whole answer within {timeout:.0f} s")
    if "error" in got:
        raise got["error"]
    return bytes(got["body"])


LACKING_RECHECK = 600  # s a call the store said it lacks is answered from memory (StoreLacksCall) before it is asked again


def lacking_path() -> str:
    """{"<store url> <path>": when the store said it has no such call}: shared by this host's processes (the sync, a
    `land`), so none of them re-sends a call the store just refused as unknown."""
    return state_root("store-lacking.json")


def lacks_call(e_code: int, path: str, b: dict[str, Any]) -> bool:
    """Whether a 404 says the store has no such CALL (a newer client's, version skew) rather than no such item: the
    server's `UnknownCall`, or the KeyError of a server before it, whose message is the method's name quoted."""
    if e_code != 404 or not path.startswith("/v1/store/"):
        return False
    name = path[len("/v1/store/"):]
    return b.get("error") == "UnknownCall" or b.get("message") == repr(name)


def lacking_text(url: str, path: str) -> str:
    return (f"the store at {url} has no `{path.rsplit('/', 1)[-1]}`: it runs an older agentctl than this client "
            f"(version skew: ci-runner's pin behind ~/bin); kept, asked again in {LACKING_RECHECK // 60} min")


def _raise_http_error(e: urllib.error.HTTPError, method: str, path: str, url: str = "", t: float = 0.0) -> None:
    """The store's answer to a failed call, as its exception: an auth failure, the store's own error class, a call the
    store lacks (remembered), a transient one (retried by `call`), else unavailable."""
    b: Any = attempt(lambda: json.loads(e.read() or b"{}"), {}, catch=(ValueError,))
    said = f"{method} {path}: HTTP {e.code} {b.get('message') or e.reason}"
    if e.code in (401, 403):
        raise StoreAuthFailed(said) from None
    if lacks_call(e.code, path, b if isinstance(b, dict) else {}):
        write_json_file(lacking_path(), {**read_json_file(lacking_path()), f"{url} {path}": t})
        raise StoreLacksCall(lacking_text(url, path)) from None
    cls = store_errors().get(b.get("error"))
    if cls:
        raise cls(b.get("message") or "")
    if e.code in (500, 502, 503, 504):  # the tunnel, the service between restarts, a busy database (a 500: retried
        # only when the call is safe -- `database is locked` after the 30 s busy timeout)
        raise _StoreTransient(said, e.code == 503) from None
    raise StoreUnavailable(said) from None


def _remote_call(call: StoreCall) -> Callable[..., object]:
    """RemoteStore's method for one call of the table: Store's method of that name, over the API."""
    def method(self: "RemoteStore", *args: object, **kwargs: object) -> object:
        return self._rpc(call, args, kwargs)
    method.__name__ = method.__qualname__ = call.name
    method.__doc__ = f"Store.{call.name}({call.args.replace(' ', ', ')}) over the API ({call.kind}, since v{call.since})."
    return method


# THE STORE'S CALLS AS METHODS, made from the table where the client is defined (storecalls.py): a process importing
# this module alone has every one (2026-11-26: the server module set them on the class, so it had no `events`)
RemoteCalls = type("RemoteCalls", (), {c.name: _remote_call(c) for c in STORE_CALLS})


class RemoteStore(RemoteCalls):  # type: ignore[misc,valid-type]
    """Store's methods over the phase-2 API (`POST /v1/store/<method>`: every call of storecalls.STORE_CALLS), plus the
    §6.3 calls the sync needs. The token is the caller's own store token ($AGENTCTL_STORE_TOKEN, else the one agentctl
    reads for movies)."""

    needs_token = True  # a call goes out with the caller's token (the file store reads a local database: Store.needs_token)

    def __init__(self, url, tok = None, clock = None, timeout: int = 60, retries = None,
                 machine: str | None = None) -> None:
        """machine: the machine the calls speak for (X-Agentctl-Machine), None for this host's (contact_machine)."""
        self.url, self._tok, self.clock, self.timeout = url.rstrip("/"), tok, clock or epoch_now, timeout
        self.retries, self.machine = retries, machine
        self.path = self.url

    def _token(self):
        return self._tok or os.environ.get("AGENTCTL_STORE_TOKEN") or token(SHORT["movies"])

    def call(self, method, path, body = None, timeout = None, safe = None):
        """One API call, retried through a transient failure (a reset, a refusal, a timeout, a 502/503/504) up to
        STORE_RETRIES times with backoff. A call that may have taken effect (a mutation whose request was sent) is
        retried only when it never reached the server (refused). What is still failing after that raises
        StoreUnavailable: no verdict either way -- never "lost"."""
        self._refuse_lacking(path)
        safe = (method == "GET") if safe is None else safe
        tries = int(knob_store_retries()) if self.retries is None else self.retries
        for attempt in range(tries + 1):
            try:
                return self._call_once(method, path, body, timeout)
            except _StoreTransient as e:
                if attempt == tries or not (safe or e.unsent):
                    raise StoreUnavailable(str(e)) from None
                # wall: waits on and times real processes and threads
                time.sleep(knob_store_retry_base() * (2 ** attempt))

    def _refuse_lacking(self, path: str) -> None:
        """StoreLacksCall, without the network, for a call the store said it lacks within LACKING_RECHECK."""
        said = read_json_file(lacking_path()).get(f"{self.url} {path}")
        if said is not None and 0 <= self.clock() - float(said) < LACKING_RECHECK:
            raise StoreLacksCall(lacking_text(self.url, path), remembered=True)

    def _headers(self):
        hdrs = {"Authorization": f"token {self._token()}", "Content-Type": "application/json"}
        machine = self.machine or contact_machine()
        if machine:
            hdrs["X-Agentctl-Machine"] = machine
        if host_role() == "sync":  # which machine this laptop is (laptop_role_refusal): every sync client names it
            hdrs["X-Agentctl-Role"] = laptop_role_holder()
        return hdrs

    def _call_once(self, method, path, body = None, timeout = None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.url + path, data=data, method=method, headers=self._headers())
        try:
            return json.loads(open_within(req, timeout or self.timeout) or b"null")
        except urllib.error.HTTPError as e:
            _raise_http_error(e, method, path, self.url, self.clock())
        except (urllib.error.URLError, OSError, ValueError, http_client_errors()) as e:
            reason = getattr(e, "reason", e)
            raise _StoreTransient(f"{method} {path}: {e}", isinstance(reason, ConnectionRefusedError)) from None

    def _rpc(self, call: StoreCall, args: tuple[object, ...], kwargs: dict[str, object]) -> object:
        return self.call("POST", f"/v1/store/{call.name}", {"args": list(args), "kwargs": kwargs},
                         safe=call.retry_safe)["result"]

    def state(self):
        return self.call("GET", "/v1/state")

    def submit(self, entry):
        return self.call("POST", "/v1/changes", entry, safe=bool(entry.get("id")))  # an id: the same change twice

    def cancel_change(self, eid, how: str = "cancel"):
        return self.call("POST", f"/v1/changes/{urllib.parse.quote(eid, safe='')}:{how}", {})

    def nudge(self):
        return self.call("POST", "/v1/tick", {}, safe=True)


STORE_PORT = 3014


STORE_WAITED_TIMEOUT = 15  # s one store call of a command someone waits on (status, land status): never retried


def store_unreachable_text(e: BaseException) -> str:
    """ONE LINE for a command the store did not answer in its bound: which store, through what, and the next step
    (2026-11-26: `status` and `land status` hung past 90 s on a tunnel whose ssh was up and whose link was saturated)."""
    url = store_url() or default_store_url()
    via = (" through the laptop's ssh tunnel to ci-runner's store (launchd net.bitcashier.forgejo-tunnel)"
           if url == default_store_url() else "")
    return (f"agentctl: STORE UNREACHABLE: {url}{via} did not answer: {str(e)[:200]}; `agentctl env --check` says "
            f"whether the tunnel is up but unresponsive")


DEFAULT_STORE_URL = f"http://localhost:{STORE_PORT}"  # ci-runner's store through the laptop's tunnel


def store_url() -> str | None:
    """The store a sync process uses: $AGENTCTL_STORE_URL, else the one `agentctl store use URL` wrote (state root's
    store.url, so every agentctl process -- agents' `land` and `red claim` included -- agrees), else ci-runner's through
    the tunnel. None on the server: its database is in-process."""
    if host_role() == "server":
        return None
    if knob_store_url():
        return knob_store_url()
    return read_text_or(state_root("store.url"), "").strip() or default_store_url()


def default_store_url() -> str:
    """ci-runner's store through the tunnel; $AGENTCTL_STORE_DEFAULT_URL overrides (the tests: a closed port)."""
    return os.environ.get("AGENTCTL_STORE_DEFAULT_URL") or DEFAULT_STORE_URL
