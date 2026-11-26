# agentctl, part `server`: the store over HTTP (`agentctl store serve`): its API, the change submissions it takes, its metrics
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from agentctl.ids import stamp
from agentctl.clock import epoch_now
import fcntl
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import re
import signal
import sys
import threading
import time
import urllib.parse
from typing import Any
from agentctl.core import err, git, remote, state_root
from agentctl.timefmt import iso_epoch, now, ts
from agentctl.store import Store, store_errors
from agentctl.repos import server_repo
from agentctl.trailers import do_not_land
from agentctl.waitingdir import save_entry, waiting_entries, waiting_state
from agentctl.redrecord import red_lease
from agentctl.queueslots import entry_queued_since
from agentctl.conflicts import CONFLICT_PLACE_KEPT
from agentctl.queuerecheck import queued_entries, same_change_source
from agentctl.cistate import state_read
from agentctl.laptoprole import laptop_role_refusal, laptop_role_speaks
from agentctl.waiting import cancel_submitted, supersede
from agentctl.queuesubmit import SUBMIT_FIELDS
from agentctl.remotestore import RemoteStore, STORE_PORT, store_url
from agentctl.storeauth import (
    store_secret, store_secret_path, store_token_issue, store_token_revoke, store_token_user, store_tokens_load)
from agentctl.blamerecords import blame_land_refusal
from agentctl.redhold import red_unheld
from agentctl.statemachines import MACHINES, StaleVersion, TerminalState, machine_state
from agentctl.knobs import knob_tick


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
STORE_RPC = ("acquire", "renew", "note", "release", "revoke", "set_meta", "offer", "offers", "offer_of", "offer_reason",
             "close_offer", "take", "get", "item", "items", "put_item", "transition", "update_item", "mark_notified",
             "meta_get", "meta_set", "next_seq", "events", "item_events", "archive_free_leases", "list")
STORE_BODY_MAX = 4 << 20     # bytes: a larger request body is refused unread (413)


for _m in STORE_RPC:
    setattr(RemoteStore, _m, (lambda m: lambda self, *a, **k: self._rpc(m, a, k))(_m))


def process_store(clock=None):
    """The store this process uses (cli hands it to CurrentStore as its opener): the remote store when a store URL is set
    (store_url: every sync process), else the file store (the server's own database)."""
    url = store_url()
    return RemoteStore(url, clock=clock) if url else Store(clock=clock)


def store_grace(st, now_=None):
    """§6.2 'Store downtime grace': at the service's start every live lease is extended by the time the store was down
    (meta last_alive), so a store restart never expires everyone at once. Returns the gap applied."""
    now_ = now_ if now_ is not None else st.clock()
    last = float(st.meta_get("last_alive", 0) or 0)
    gap = now_ - last if last else 0
    if gap > 60:
        c = st.db()
        try:
            c.execute("UPDATE leases SET expires_at=expires_at+?, max_until=max_until+? WHERE holder!='' AND expires_at>?",
                      (gap, gap, last))
        finally:
            c.close()
    st.meta_set("last_alive", now_)
    return gap if gap > 60 else 0


def _conflict_rejected_at(last: Any) -> Any:
    """Epoch of the entry's rejection for a conflict; None when it was not rejected for one (or is no entry)."""
    if not last or last.get("status") != "rejected" or last.get("conflict_files") is None:
        return None
    return iso_epoch(last.get("decided"))


def conflict_bounced(every, e, now_=None):
    """The newest entry of e's worktree and branch, when it was rejected for a conflict within CONFLICT_PLACE_KEPT s
    (and nothing of that source was queued since), or is still held for its regeneration (queueclose.py regen_hold): the
    place e takes. None otherwise."""
    mine = [x for x in every if x["id"] != e["id"] and same_change_source(x, e["worktree"], e.get("branch"))]
    last = max(mine, key=lambda x: x["seq"], default=None)
    if last and last.get("regen_hold") and last.get("status") == "queued":
        return last
    at = _conflict_rejected_at(last)
    if at is None:
        return None
    return last if (now_ or now().timestamp()) - at <= CONFLICT_PLACE_KEPT else None


def supersede_older(st: Any, d: Any, e: Any) -> None:
    """RE-QUEUEING REPLACES: only the newest entry per worktree and branch runs."""
    for old in queued_entries(d):
        if old["id"] != e["id"] and same_change_source(old, e["worktree"], e.get("branch")):
            supersede(st, d, old, e["id"], f"{e['worktree']} queued again ({e['head'][:10]}); only the newest entry runs")


def queue_change(st, d, fields, entry_id=None):
    """A submitted change as a queued entry (seq under seq.lock); re-queueing from the same source supersedes the older
    one. Returns (entry, n ahead). Shared by `land` (phase 1) and POST /v1/changes (phase 2)."""
    with open(os.path.join(d, "seq.lock"), "a+") as seq_lock:  # a unique seq; never the landing lock (a push holds it)
        fcntl.flock(seq_lock, fcntl.LOCK_EX)
        every = waiting_entries(d, open_only=False)
        # THE COUNTER NEVER GOES BACK (queue simulator, replay 2026-11-17, seed 1006): an entry that took a bounced
        # entry's place carries an old seq, so max(seq) + 1 handed the NEXT submission the number it had just used --
        # two submissions of one worktree in the same second got one id, and the second was answered with the first
        # entry: lost. The id's number is the highest ever drawn (`drawn`), whatever seq an entry ends up with.
        seq = max([max(int(e["seq"]), int(e.get("drawn") or 0)) for e in every] + [0]) + 1
        eid = entry_id or f"{stamp('entry')}-{os.path.basename(fields['worktree'])}-{seq}"
        if any(x["id"] == eid for x in every):
            e = next(x for x in every if x["id"] == eid)
            return e, max(len(queued_entries(d)) - 1, 0)
        e = {**fields, "id": eid, "seq": seq, "drawn": seq, "status": "queued", "created": ts()}
        bounced = conflict_bounced(every, e)
        if bounced:  # A RESOLVED CONFLICT KEEPS ITS PLACE -- and its queue age (the store item's submitted_at)
            e.update(seq=bounced["seq"], requeued_from=bounced["id"],
                     queued_since=entry_queued_since(bounced) or entry_queued_since(e))
        save_entry(d, e)
        supersede_older(st, d, e)
    return e, max(len(queued_entries(d)) - 1, 0)


def check_submission(repo: str, body: dict[str, Any]) -> None:
    """Raise ValueError unless body is a well-formed change submission and repo is cloned."""
    if not os.path.isdir(repo):
        raise ValueError(f"the reconciler has not cloned {repo} yet")
    missing = [k for k in ("worktree", "head", "commits") if not body.get(k)]
    if missing or not re.fullmatch(r"[0-9a-f]{40}", str(body.get("head"))):
        raise ValueError(f"a change needs worktree, head (a full sha) and commits; missing or bad: {missing or ['head']}")
    if body.get("id") and not re.fullmatch(r"[\w.-]{1,120}", body["id"]):
        raise ValueError("a bad change id")


def submit_change(st, body, repo=None):
    """POST /v1/changes: a change the laptop pushed to refs/heads/land/<id> becomes a queued entry of the server's queue."""
    repo = repo or server_repo()
    check_submission(repo, body)
    commits = body["commits"] if isinstance(body["commits"], list) else str(body["commits"]).split()
    why = do_not_land(repo, commits, body.get("branch") or "") or blame_land_refusal(repo, commits, st, body.get("worktree"))
    if why:
        raise ValueError(f"NOT LANDED: {why}")
    d = waiting_state(repo)[0]
    e, ahead = queue_change(st, d, {k: body.get(k) for k in SUBMIT_FIELDS}, body.get("id"))
    supersede_stranded(d, e, st)
    return {"id": e["id"], "seq": e["seq"], "ahead": ahead}


def supersede_stranded(d, e, st):
    """A submission replaces every older open item of the same worktree that no entry carries (queue_change supersedes
    the entries; an item mirrored from another host's queue has none): SUPERSEDED, never left waiting."""
    carried = {x["id"] for x in waiting_entries(d, open_only=False)}
    for it in st.items("change"):
        dd = it.get("data") or {}
        if it["id"] in carried or machine_state("change", it["state"]).terminal or \
                os.path.realpath(dd.get("worktree") or "") != os.path.realpath(e.get("worktree") or ""):
            continue
        try:
            st.transition("change", it["id"], "superseded", it["version"], data={"superseded_by": e["id"]},
                          event={"text": f"SUPERSEDED {it['id']} by {e['id']}: {e['worktree']} submitted again",
                                 "refs": {"entry": it["id"]}})
        except (StaleVersion, TerminalState):
            pass


def metrics_text(st, now_=None):
    """The Prometheus text the module's agentctl-metrics copies to node-exporter (agentctl.rules' gauges)."""
    t = now_ if now_ is not None else st.clock()
    two = 2 * knob_tick()
    rows = [("agentctl_reconciler_tick_timestamp", "unix time the last tick finished", float(state_read().get("tick_at") or 0)),
            ("agentctl_oldest_change_age_seconds", "age of the oldest non-terminal change, not counting red main time",
             oldest_change_age(st, t)),
            ("agentctl_red_unleased_seconds", "longest a red on main has had no lease, fix, block or offer",
             longest_unleased_red(st, t)),
            ("agentctl_leases_expired_unreaped", "leases past their TTL by more than 2 ticks with their item unmoved",
             unreaped_leases(st, t, two)),
            ("agentctl_items_past_deadline", "items past their state's deadline by more than 2 ticks",
             items_past_deadline(st, t, two))]
    out = []
    for name, help_, v in rows:
        out += [f"# HELP {name} {help_}.", f"# TYPE {name} gauge", f"{name} {float(v):.0f}"]
    return "\n".join(out) + "\n"


def oldest_change_age(st: Any, t: float) -> float:
    """The age of the oldest non-terminal change, not counting the time main was red (the red accumulator's growth)."""
    acc = float(st.meta_get("red_accum", 0) or 0)
    ages = []
    for it in st.items("change"):
        if machine_state("change", it["state"]).terminal:
            continue
        dd = it.get("data") or {}
        since = float(dd.get("submitted_at") or it["entered_at"])
        acc0 = dd.get("red_accum0")
        ages.append(max(0.0, (t - since) - max(0.0, acc - float(acc if acc0 is None else acc0))))
    return max(ages) if ages else 0


def longest_unleased_red(st: Any, t: float) -> float:
    """The longest a red on main has had no lease, fix, block or offer (0 when the store cannot say)."""
    unleased = 0.0
    try:
        for it in red_unheld(st):
            unleased = max(unleased, t - float(it.get("entered_at") or t))
    except Exception:
        pass
    return unleased


def unreaped_leases(st: Any, t: float, two: float) -> int:
    """How many leased reds and checking changes hold a lease that expired more than `two` s ago with the item unmoved."""
    unreaped = 0
    for typ, state, lease in (("red", "leased", lambda i: red_lease(i["data"]["key"])),
                              ("change", "checking", lambda i: f"check/{i['id']}")):
        for it in st.items(typ, [state]):
            try:
                row = st.get(lease(it))
            except (KeyError, TypeError):
                continue
            if row and row["expires_at"] < t - two and row["expires_at"] > it["entered_at"]:
                unreaped += 1
    return unreaped


def items_past_deadline(st: Any, t: float, two: float) -> int:
    """How many non-terminal items are past their state's deadline by more than `two` s."""
    return sum(1 for typ in MACHINES for it in st.items(typ)
               if not machine_state(typ, it["state"]).terminal and it["deadline"] < t - two)


class StoreAPI:
    """The HTTP API's logic, apart from the socket (tests call handle() directly too)."""

    def __init__(self, store, allow=(), metrics=(), user_of=None, repo=None):
        self.store = store
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
            return 200, "text/plain; version=0.0.4", metrics_text(st).encode()
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
            if m not in STORE_RPC:
                raise KeyError(m)
            return self._json(200, {"result": getattr(st, m)(*b.get("args", []), **b.get("kwargs", {}))})
        raise KeyError(f"POST {p}")


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


def cmd_store(a):
    """`agentctl store serve --listen ADDR:PORT --allow-user U... --metrics-user U...` (ci-runner);
    `agentctl store use URL` (the laptop: which store its sync and agents use)."""
    if a.scmd == "token":
        return store_token_command(a)
    if a.scmd == "use":
        return store_use_command(a)
    return store_serve_command(a)


def store_token_command(a: Any) -> int:
    """`agentctl store token issue|revoke|list`: the store's tokens, the issued one printed on stdout."""
    if a.tcmd == "issue":
        tid, tok = store_token_issue(a.user, a.name)
        err(f"store token {tid} for {a.user} issued (revoke: agentctl store token revoke {tid})")
        print(tok)
    elif a.tcmd == "revoke":
        rec = store_token_revoke(a.id)
        print(f"revoked {a.id} ({rec.get('user')} {rec.get('name') or ''})".rstrip())
    else:
        for tid, rec in sorted(store_tokens_load().items(), key=lambda x: x[1].get("issued", "")):
            print(f"{tid}  {rec.get('user'):<14} {rec.get('name') or '-':<14} issued {rec.get('issued')}"
                  + (f"  REVOKED {rec['revoked']}" if rec.get("revoked") else ""))
    return 0


def store_use_command(a: Any) -> int:
    """`agentctl store use URL`: this machine's store from now on, once it answers with this token."""
    RemoteStore(a.url).state()  # it answers, with this token, before anything relies on it
    os.makedirs(state_root(), exist_ok=True)
    with open(state_root("store.url"), "w") as f:
        f.write(a.url + "\n")
    print(f"store: {a.url} (every agentctl process here; the sync re-reads it at its next start)")
    return 0


def store_serve_command(a: Any) -> int:
    """`agentctl store serve`: the file store behind its API, its leases extended by the downtime, until stopped."""
    host, _, port = a.listen.rpartition(":")
    st = Store(path=state_root("store.db"))
    gap = store_grace(st)
    if gap:
        err(f"store: down {int(gap)} s; every live lease extended by that")
    if not store_secret():
        raise SystemExit(f"agentctl store serve: no secret at {store_secret_path()} yet "
                         "(`agentctl store token issue --user <login>` makes it)")
    api = StoreAPI(st, a.allow_user or [], a.metrics_user or [])
    srv = store_server(api, host or "127.0.0.1", int(port or STORE_PORT))
    threading.Thread(target=store_alive_beat, args=(st,), daemon=True).start()
    signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=srv.shutdown, daemon=True).start())
    err(f"store: serving {state_root('store.db')} on {host}:{port}, auth {a.auth} (allow {','.join(api.allow) or '-'}; metrics "
        f"{','.join(api.metrics) or '-'})")
    srv.serve_forever()
    return 0


def store_alive_beat(st: Any) -> None:
    """Every 30 s the store's `last_alive` (store_grace reads it at the next start to extend the leases by the downtime)."""
    while True:
        time.sleep(30)  # wall: waits on and times real processes and threads
        try:
            st.meta_set("last_alive", epoch_now())
        except Exception as e:
            err(f"store: last_alive not written ({e})")


# A REVOKE NEEDS NO SECRET, SO THE ROLE GUARDS IT TOO (2026-11-19: all night every family owner ended "STOPPED: the
# owner's heartbeat lease was lost" and was restarted into the same end -- outside-gate 23 times, convergence 5,
# main-tests 7 -- and an owner's red fixer with it. Each owner lease was revoked seconds after a good renewal,
# "ORPHANED: ... its wrapper ended (IDLE: no red of convergence for 6 h)": a verdict this laptop's convergence owner
# never wrote. pawel-mbp, its tunnel up all night, ran an agentctl from before the role, and its orphan reaper judged
# Pro-2's leases by its own owner records and its own processes. The store refused it acquire and take, never revoke.)
# A client naming no role -- an agentctl from before it -- is refused for good once a role was ever held, the holder
# asleep or not; one naming another machine is refused while the holder's role lives (past it, it takes the role).


