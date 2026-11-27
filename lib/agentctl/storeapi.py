# agentctl, part `storeapi`: the store over HTTP: its API and the request handler that serves it
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import os
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from agentctl.govpool import json
from agentctl.storecalls import served_calls
from agentctl.storeauth import store_token_user
from agentctl.core import state_root
from agentctl.ghapp import urllib
from agentctl.eventrows import legacy_cause
from agentctl.cistate import state_read
from agentctl.store import store_errors
from agentctl.laptoprole import laptop_role_refusal, laptop_role_speaks
from agentctl.waiting import cancel_submitted
from agentctl.changesubmit import submit_change
from agentctl.storemetrics import metrics_text


# ---------------------------------------------------------------- phase 2: the store over HTTP (design §6.3, §13 phase 2)
# `agentctl store serve` puts the store behind a small HTTP API on ci-runner (:3014, reached through the laptop's
# tunnel; nix/hosts/ci-runner/agentctl.nix runs it). Every request carries `Authorization: token <a store token>`,
# checked by the store itself (THE STORE'S OWN TOKENS below): an --allow-user login gets the whole API, a
# --metrics-user login GET /metrics alone, anyone else 403, no token 401. The reconciler (`agentctl tick` there) opens
# the same database in-process: two units, one WAL file, so a reconciler crash leaves leases, submissions and the event
# feed up. RemoteStore is Store's twin over the API: on the laptop the process store IS one, so the laptop's sync, its
# dispatcher, `red claim` and the fixers' wrappers work against ci-runner's store unchanged.
#
# TWO ROLES, ONE WRITER (owner decision 7, 2026-10-29: no laptop standby reconciler; the laptop's own store and its
# phase-1 tick were deleted in the simplification, docs/simplification-2026-10.md item 2): "server" (`agentctl tick`
# and `store serve` on ci-runner, AGENTCTL_HOST=ci-runner or AGENTCTL_ROLE=server: the database in-process, the tick,
# never the laptop's duties) and "sync" (everywhere else: submit, events -> inbox, the notifier, the state mirror, the
# claims' activity, the fixer dispatcher; it never ticks and has no database of its own). DISASTER RECOVERY needs no
# laptop copy: a lost store.db is rebuilt from the land/* refs and GitHub's runs (design §16), the backup timer keeps
# 24 hourly copies on ci-runner.
STORE_BODY_MAX = 4 << 20     # bytes: a larger request body is refused unread (413)


class StoreAPI:
    """The HTTP API's logic, apart from the socket (tests call handle() directly too)."""

    def __init__(self, store, allow=(), metrics=(), user_of=None, repo=None, calls=None, *, ctx):
        self.ctx = ctx
        self.store = store
        self.calls = served_calls() if calls is None else calls  # what it serves (storecalls.py; a test: an older store's)
        self.allow, self.metrics = set(allow), set(metrics)
        self.user_of = user_of or store_token_user
        self.repo = repo

    def login(self, tok):
        """The token's login (None: unknown or revoked). A local check: never cached, so a revoke counts at once."""
        return self.user_of(tok)

    @staticmethod
    def _json(code, obj):
        return code, "application/json", json.dumps(obj).encode()

    def handle(self, method, path, headers, body=b""):
        u = urllib.parse.urlsplit(path)
        q = {k: v[-1] for k, v in urllib.parse.parse_qs(u.query).items()}
        refused = self._unauthorized(method, u.path, headers)
        if refused:
            return refused
        try:
            return self._dispatch(method, u.path, q, headers, body)
        except Exception as e:
            return self.error_answer(method, u.path, e)

    def _dispatch(self, method: Any, path: Any, q: Any, headers: Any, body: Any) -> Any:
        role = next((v for k, v in headers.items() if k.lower() == "x-agentctl-role"), None)
        self._note_contact(path, headers, role)
        args = json.loads(body) if body else {}
        standby = laptop_role_refusal(self.store, path, args, role)
        if standby:
            return self._json(409, {"error": "Standby", "message": standby})
        return self.route(method, path, q, args)

    def error_answer(self, method: Any, path: Any, e: Any) -> Any:
        """The answer to an error out of a request: 409 a store refusal, 404 a name unknown, 400 a bad request, any
        other a 500 with its traceback in the journal, never a dropped connection."""
        if isinstance(e, tuple(store_errors().values())):
            return self._json(409, {"error": type(e).__name__, "message": str(e)})
        if isinstance(e, UnknownCall):  # a newer client's call (version skew): said as such (remotestore.py StoreLacksCall)
            return self._json(404, {"error": "UnknownCall", "message": e.args[0]})
        if isinstance(e, KeyError):
            return self._json(404, {"error": "NotFound", "message": str(e)})
        if isinstance(e, (ValueError, TypeError)):
            return self._json(400, {"error": "BadRequest", "message": str(e)})
        return self.internal_error(method, path, e)

    def _unauthorized(self, method: Any, path: Any, headers: Any) -> Any:
        """The 401 / 503 / 403 answer to a request whose token is missing, unchecked or not allowed (None: allowed)."""
        auth = headers.get("Authorization") or headers.get("authorization") or ""
        m = re.match(r"(?i)(?:token|bearer)\s+(\S+)$", auth.strip())
        if not m:
            return self._json(401, {"error": "Unauthorized",
                                    "message": "Authorization: token <an agentctl store token> required"})
        try:
            who = self.login(m.group(1))
        except Exception as e:
            return self._json(503, {"error": "Unavailable", "message": f"the token could not be checked: {e}"})
        if who not in self.allow and not (who in self.metrics and method == "GET" and path == "/metrics"):
            return self._json(403, {"error": "Forbidden", "message": f"{who or 'this token'} may not call {path}"})
        return None

    def _note_contact(self, path: Any, headers: Any, role: Any) -> Any:
        """A store call from a machine: its leases' clocks run again (Store.contact) -- only for the machine that holds
        the laptop role: a pre-role machine's calls kept "laptop" in contact while the holder slept
        (laptop_role_speaks)."""
        mach = next((v for k, v in headers.items() if k.lower() == "x-agentctl-machine"), None)
        if not mach or not path.startswith("/v1/store/"):
            return
        if mach.strip() == "laptop" and laptop_role_speaks(self.store, role):
            return
        gap = self.store.contact(mach.strip())
        if gap:
            sys.stderr.write(f"agentctl-store: {mach.strip()} back after its silence: its leases paused "
                             f"{int(gap)} s\n")

    def internal_error(self, method, path, e):
        """2026-10-29 cutover: `git` missing from the store unit's PATH raised FileNotFoundError out of POST /v1/changes,
        the handler thread died and the client saw only "Remote end closed connection without response". Now the
        traceback goes to the journal and the client gets a 500 naming the error."""
        import traceback
        sys.stderr.write(f"agentctl-store: {method} {path} failed:\n{traceback.format_exc()}")
        sys.stderr.flush()
        return self._json(500, {"error": "Internal", "message": f"{type(e).__name__}: {str(e)[:300]}"})

    def route(self, method, p, q, b):
        st = self.store
        if method == "GET" and p == "/metrics":
            return 200, "text/plain; version=0.0.4", metrics_text(st, ctx=self.ctx).encode()
        if method == "GET" and p == "/v1/state":
            return self._json(200, state_read())
        if method == "POST":
            return self._route_post(p, b)
        raise KeyError(f"{method} {p}")

    def _route_post(self, p: Any, b: Any) -> Any:
        st = self.store
        if p == "/v1/changes":
            return self._json(200, submit_change(st, b, self.repo))
        if re.fullmatch(r"/v1/changes/[^/]+:(cancel|abandon)", p):
            eid, how = urllib.parse.unquote(p[len("/v1/changes/"):]).rsplit(":", 1)
            return self._json(200, cancel_submitted(st, eid, how, self.repo))
        if p == "/v1/tick":
            with open(state_root("tick.nudge"), "a"):
                os.utime(state_root("tick.nudge"))
            return self._json(200, {"result": "ok"})
        if p.startswith("/v1/store/"):
            m = p[len("/v1/store/"):]
            if m not in self.calls:
                raise UnknownCall(m)
            args, kwargs = legacy_cause(m, b.get("args", []), b.get("kwargs", {}), getattr(st, m))
            return self._json(200, {"result": getattr(st, m)(*args, **kwargs)})
        raise KeyError(f"POST {p}")


class UnknownCall(KeyError):
    """A store call this server does not serve (storecalls.served_calls): a newer client's (404 `UnknownCall`)."""


class StoreRequestHandler(BaseHTTPRequestHandler):
    """One HTTP request of the store's API; `api` (a StoreAPI) is set on the subclass store_server makes."""
    protocol_version = "HTTP/1.1"
    api: Any = None

    def _answer(self, method: Any) -> Any:
        """(code, content type, body) of the request: no token is answered before any body is read, a body over
        STORE_BODY_MAX unread (413)."""
        n = int(self.headers.get("Content-Length") or 0)
        if not (self.headers.get("Authorization") or "").strip():
            self.close_connection = True
            return self.api.handle(method, self.path, dict(self.headers), b"")
        if n > STORE_BODY_MAX:
            self.close_connection = True
            return self.api._json(413, {"error": "TooLarge", "message": f"a body over {STORE_BODY_MAX} bytes"})
        body = self.rfile.read(n) if n else b""
        return self.api.handle(method, self.path, dict(self.headers), body)

    def _go(self, method: Any) -> Any:
        try:
            code, ctype, out = self._answer(method)
        except Exception as e:  # the last resort (handle() answers its own errors): a 500, never a dropped connection
            code, ctype, out = self.api.internal_error(method, self.path, e)
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def do_GET(self) -> Any:
        self._go("GET")

    def do_POST(self) -> Any:
        self._go("POST")

    def log_message(self, fmt: Any, *args: Any) -> Any:  # the journal gets one line per non-2xx answer only
        if args and str(args[1] if len(args) > 1 else "").startswith(("4", "5")):
            sys.stderr.write("agentctl-store: %s\n" % (fmt % args))


def store_server(api, host, port):
    """A ThreadingHTTPServer serving api (returns it; serve_forever() is the caller's)."""
    srv = ThreadingHTTPServer((host, port), type("Handler", (StoreRequestHandler,), {"api": api}))
    srv.daemon_threads = True
    return srv


# A REVOKE NEEDS NO SECRET, SO THE ROLE GUARDS IT TOO (2026-11-19: all night every family owner ended "STOPPED: the
# owner's heartbeat lease was lost" and was restarted into the same end -- outside-gate 23 times, convergence 5,
# main-tests 7 -- and an owner's red fixer with it. Each owner lease was revoked seconds after a good renewal,
# "ORPHANED: ... its wrapper ended (IDLE: no red of convergence for 6 h)": a verdict this laptop's convergence owner
# never wrote. pawel-mbp, its tunnel up all night, ran an agentctl from before the role, and its orphan reaper judged
# Pro-2's leases by its own owner records and its own processes. The store refused it acquire and take, never revoke.)
# A client naming no role -- an agentctl from before it -- is refused for good once a role was ever held, the holder
# asleep or not; one naming another machine is refused while the holder's role lives (past it, it takes the role).
