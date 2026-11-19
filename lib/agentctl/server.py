# agentctl, part `server`: the store over HTTP, the server clone, the sync.
# Runs inside agentctl's one namespace, after the parts before it in lib/agentctl/__init__.py's PARTS (it uses their names).

# ---------------------------------------------------------------- phase 2: the store over HTTP (design §6.3, §13 phase 2)
# `agentctl store serve` puts the store behind a small HTTP API on ci-runner (:3014, reached through the laptop's
# tunnel; nix/hosts/ci-runner/agentctl.nix runs it). Every request carries `Authorization: token <a store token>`,
# checked by the store itself (THE STORE'S OWN TOKENS below): an --allow-user login gets the whole API, a
# --metrics-user login GET /metrics alone, anyone else 403, no token 401. The reconciler (`agentctl tick` there) opens
# the same database in-process: two units, one WAL file, so a reconciler crash leaves leases, submissions and the event
# feed up. RemoteStore is Store's twin over the API: on the laptop `Store()` IS one, so the laptop's sync, its
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
             "meta_get", "meta_set", "events", "item_events", "archive_free_leases", "list")
STORE_BODY_MAX = 4 << 20     # bytes: a larger request body is refused unread (413)
STORE_PORT = 3014
SYNC_MIRROR_EVERY = 10   # s between the laptop's copies of /v1/state into state.json


DEFAULT_STORE_URL = f"http://localhost:{STORE_PORT}"  # ci-runner's store through the laptop's tunnel


def store_url():
    """The store a sync process uses: $AGENTCTL_STORE_URL, else the one `agentctl store use URL` wrote (state root's
    store.url, so every agentctl process -- agents' `land` and `red claim` included -- agrees), else ci-runner's through
    the tunnel. None on the server: its database is in-process."""
    if host_role() == "server":
        return None
    if os.environ.get("AGENTCTL_STORE_URL"):
        return os.environ["AGENTCTL_STORE_URL"]
    try:
        with open(state_root("store.url")) as f:
            return f.read().strip() or default_store_url()
    except OSError:
        return default_store_url()


def default_store_url():
    """ci-runner's store through the tunnel; $AGENTCTL_STORE_DEFAULT_URL overrides (the tests: a closed port)."""
    return os.environ.get("AGENTCTL_STORE_DEFAULT_URL") or DEFAULT_STORE_URL


def host_role():
    """"server" on ci-runner (AGENTCTL_HOST=ci-runner, or AGENTCTL_ROLE=server), else "sync". There is no third role."""
    if os.environ.get("AGENTCTL_ROLE") == "server" or os.environ.get("AGENTCTL_HOST") == "ci-runner":
        return "server"
    return "sync"


def contact_machine():
    """The machine this process's store calls speak for (A SILENT MACHINE'S LEASE CLOCKS PAUSE): $AGENTCTL_MACHINE, else
    `laptop` for the laptop's agentctl (the sync role on macOS: its holders are `laptop:...`), else None."""
    m = os.environ.get("AGENTCTL_MACHINE")
    if m is not None:
        return m or None
    return "laptop" if host_role() == "sync" and sys.platform == "darwin" else None


def store_errors():
    return {"StaleVersion": StaleVersion, "TerminalState": TerminalState, "LeaseLost": LeaseLost}


class StoreUnavailable(RuntimeError):
    """The remote store did not answer (the tunnel, ci-runner, the service): no verdict either way."""


class StoreAuthFailed(StoreUnavailable):
    """The store refused the caller's credential (HTTP 401/403): never transient. A wrapper holding leases takes a fresh
    store token once and, refused again, ends at once -- its leases released (or revoked by the tick) with `store auth
    failed: <detail>` -- instead of 'asking again at the next beat' forever (2026-10-31: an owner session started
    before the store-only-token switch got 403s while its wrapper kept its reds leased)."""


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
            hdrs["X-Agentctl-Role"] = laptop_role_holder()  # which machine this laptop is (laptop_role_refusal)
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


for _m in STORE_RPC:
    setattr(RemoteStore, _m, (lambda m: lambda self, *a, **k: self._rpc(m, a, k))(_m))


def _store_new(cls, path=None, clock=None):
    url = store_url() if cls is Store and path is None else None
    if url:
        return RemoteStore(url, clock=clock)
    return object.__new__(cls)


Store.__new__ = _store_new


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


def server_repo():
    """The reconciler's own clone on ci-runner (movies_repo_path; `tick` clones it from $AGENTCTL_MOVIES_REMOTE as
    remote()'s name)."""
    return movies_repo_path()


# THE SERVER CLONE IS ATOMIC (2026-10-29: the reconciler crash-looped at 11:33 mid-clone; the half clone's .git -- an
# incomplete object store, a stray shallow_* temp file -- passed for a clone, and every landing check's fetch of main
# failed for hours). A clone goes into a temp directory beside the target, is verified (origin/main resolves, its
# commits and its tree are all there) and only then renamed into place: a clone cut off anywhere leaves a temp
# directory (removed at the next start), never a target that looks complete.
CLONE_TMP = ".clone-tmp-"


def clone_tmp_dirs(repo):
    """The temp clones beside repo: [(path, pid of their cloner)]."""
    parent, base = os.path.split(os.path.abspath(repo))
    out = []
    for p in glob.glob(os.path.join(parent, f".{base}{CLONE_TMP}*")):
        m = re.search(re.escape(CLONE_TMP) + r"(\d+)-", os.path.basename(p))
        out.append((p, int(m.group(1)) if m else 0))
    return out


def clean_clone_tmps(repo):
    """Remove the temp clones an interrupted clone left (their cloner is gone). Returns what it removed."""
    import shutil
    gone = []
    for p, pid in clone_tmp_dirs(repo):
        if pid and pid != os.getpid() and pid_alive(pid):
            continue
        shutil.rmtree(p, ignore_errors=True)
        gone.append(p)
    return gone


def clone_verify(path):
    """(ok, detail): the clone at path is usable: origin/main resolves, every commit behind it and its tip's tree are
    readable (the cheap connectivity check: commits, not every blob of history)."""
    for args in (("rev-parse", "--verify", "-q", f"{remote()}/main^{{commit}}"),
                 ("rev-list", "--quiet", f"{remote()}/main"),
                 ("rev-list", "--quiet", "--objects", "--no-walk", f"{remote()}/main")):
        r = git(path, *args, check=False)
        if r.returncode != 0:
            return False, f"git {' '.join(args)}: exit {r.returncode} {(r.stderr or '').strip()[-300:]}"
    return True, f"{remote()}/main resolves and is connected"


# A CLONE NEVER TRIPS SYSTEMD (2026-10-29: the reconciler is Type=notify, TimeoutStartSec=300, WatchdogSec=120; movies
# is a ~650 MB pack). While a clone runs, every CLONE_KEEPALIVE s: before READY, EXTEND_TIMEOUT_USEC (the start
# timeout pushed out past the next keepalive); after it, WATCHDOG=1; both with a STATUS= line. The clone itself is
# bounded by CLONE_TIMEOUT, so the extensions end.
SD_READY = [False]
CLONE_KEEPALIVE = 20
CLONE_TIMEOUT = 1800


def clone_keepalive(what, started):
    """One keepalive for a clone running since started (monotonic)."""
    status = f"STATUS={what} ({int(time.monotonic() - started)} s)"
    if SD_READY[0]:
        return sd_notify(f"WATCHDOG=1\n{status}")
    every = env_num("AGENTCTL_CLONE_KEEPALIVE", CLONE_KEEPALIVE)
    return sd_notify(f"EXTEND_TIMEOUT_USEC={int(max(every * 4, 60) * 1_000_000)}\n{status}")


def run_clone(cmd, what):
    """The clone, bounded by CLONE_TIMEOUT, with systemd keepalives meanwhile: CompletedProcess; raises
    subprocess.TimeoutExpired (the clone killed) past the bound."""
    bound = env_num("AGENTCTL_CLONE_TIMEOUT", CLONE_TIMEOUT)
    every = env_num("AGENTCTL_CLONE_KEEPALIVE", CLONE_KEEPALIVE)
    started = time.monotonic()
    got = {}

    def work():  # subprocess.run (bounded, waited for): the keepalives go on in this thread meanwhile
        try:
            got["r"] = subprocess.run(cmd, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=bound,
                                      env=dict(os.environ, GIT_TERMINAL_PROMPT="0"))
        except (subprocess.TimeoutExpired, OSError) as x:
            got["x"] = x
    t = threading.Thread(target=work, name="clone", daemon=True)
    clone_keepalive(what, started)
    t.start()
    while True:
        t.join(every)
        if not t.is_alive():
            break
        clone_keepalive(what, started)
    if "x" in got:
        raise got["x"]
    return got["r"]


def clone_into_tmp(repo, url):
    """A fresh, verified clone of url in a temp directory beside repo (never repo itself). Returns its path; raises
    SystemExit (the temp directory removed) when the clone or its verification fails."""
    import shutil
    parent, base = os.path.split(os.path.abspath(repo))
    os.makedirs(parent, exist_ok=True)
    tmp = os.path.join(parent, f".{base}{CLONE_TMP}{os.getpid()}-{int(time.time() * 1000)}")
    try:
        r = run_clone(["git", "clone", "--quiet", "--origin", remote(), url, tmp], f"cloning {url} into {tmp}")
    except subprocess.TimeoutExpired as x:
        shutil.rmtree(tmp, ignore_errors=True)
        raise SystemExit(f"agentctl tick: git clone {url} timed out after {x.timeout:.0f} s")
    if r.returncode != 0:
        shutil.rmtree(tmp, ignore_errors=True)
        raise SystemExit(f"agentctl tick: git clone {url} failed: {r.stderr.strip()[:300]}")
    ok, detail = clone_verify(tmp)
    if not ok:
        shutil.rmtree(tmp, ignore_errors=True)
        raise SystemExit(f"agentctl tick: the fresh clone of {url} does not verify: {detail}")
    return tmp


def ensure_server_clone(repo=None):
    repo = repo or server_repo()
    clean_clone_tmps(repo)
    if not os.path.isdir(os.path.join(repo, ".git")):
        url = os.environ.get("AGENTCTL_MOVIES_REMOTE")
        if not url:
            raise SystemExit(f"agentctl tick: no clone at {repo} and no $AGENTCTL_MOVIES_REMOTE to clone it from")
        tmp = clone_into_tmp(repo, url)
        if os.path.isdir(repo) and not os.listdir(repo):
            os.rmdir(repo)
        elif os.path.lexists(repo):  # something that is no clone: kept aside, never overwritten
            os.rename(repo, f"{repo}.broken-{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}")
        os.rename(tmp, repo)
    else:
        server_clone_remote(repo)
        if heal_enabled(repo):  # at startup: a clone that does not verify is repaired before the first tick
            ok, detail = clone_verify(repo)
            if not ok:
                repair_clone(repo, f"at startup the clone did not verify: {detail}")
    return repo


def server_clone_remote(repo):
    """The clone's remote() must exist and point at $AGENTCTL_MOVIES_REMOTE: added (and fetched once) or re-pointed
    here (a clone made under another remote name keeps working without a re-clone)."""
    url = os.environ.get("AGENTCTL_MOVIES_REMOTE")
    if not url:
        return
    have = git(repo, "remote", "get-url", remote(), check=False)
    if have.returncode != 0:
        git(repo, "remote", "add", remote(), url)
        git(repo, "fetch", "--quiet", remote(), "main", check=False)
    elif have.stdout.strip() != url:
        git(repo, "remote", "set-url", remote(), url)


# THE CLONE HEALS ITSELF (2026-10-29: `git fetch --refetch` could not repair the half clone either: "shallow roots
# are not allowed to be updated"). A fetch of main failing with a corruption signature marks the clone suspect; the
# processor's "clone health" step re-probes it (clone_health: verified, and a fetch of main works) and, when it is
# broken, clones afresh into a temp directory and swaps it in under the clone lock (exclusive: every step and checker
# that uses the clone holds it shared). The broken copy is kept as <repo>.broken-<ts> (the newest one only, older ones
# deleted after a successful swap); the check and waiting worktrees are re-linked to the new clone. A CLONE condition
# says so, and the same probe, passing, closes it. Only the server's own clone ($AGENTCTL_MOVIES_REMOTE set): never a
# person's checkout.
CORRUPT_GIT = re.compile(r"did not send all necessary objects|Could not read [0-9a-f]{7,}|missing (blob|tree|commit|tag) "
                         r"object|is missing|failed to traverse parents|shallow roots are not allowed|bad object|"
                         r"bad tree object|unable to read [0-9a-f]{7,}|is corrupt|object file .* is empty|"
                         r"inflate: data stream error|invalid object|unable to parse object", re.I)
CLONE_REPAIR_RETRY = 15 * 60  # a repair that failed is tried again after this
CLONE_LOCK_WAIT = 300         # the swap waits this long for the clone's users to finish, then tries again next tick
_clone_lock_depth = threading.local()
_clone_repairing = threading.Lock()
_clone_repair_at = process_cache({})


def heal_enabled(repo):
    return bool(os.environ.get("AGENTCTL_MOVIES_REMOTE")) and \
        os.path.realpath(repo) == os.path.realpath(server_repo())


def clone_side(repo, what):
    parent, base = os.path.split(os.path.abspath(repo))
    return os.path.join(parent, f".{base}.{what}")


@contextlib.contextmanager
def clone_shared(repo):
    """Use the clone: shared under its lock (the swap takes it exclusive). A no-op where it never heals, and nested."""
    depth = getattr(_clone_lock_depth, "n", 0)
    if depth or not heal_enabled(repo):
        _clone_lock_depth.n = depth + 1
        try:
            yield
        finally:
            _clone_lock_depth.n = depth
        return
    os.makedirs(os.path.dirname(clone_side(repo, "x")), exist_ok=True)
    with open(clone_side(repo, "clone-gate.lock"), "a+") as gate, open(clone_side(repo, "clone.lock"), "a+") as lk:
        fcntl.flock(gate, fcntl.LOCK_EX)  # a swap waiting for the lock holds the gate: no new user starves it
        try:
            fcntl.flock(lk, fcntl.LOCK_SH)
        finally:
            fcntl.flock(gate, fcntl.LOCK_UN)
        _clone_lock_depth.n = 1
        try:
            yield
        finally:
            _clone_lock_depth.n = 0
            fcntl.flock(lk, fcntl.LOCK_UN)


@contextlib.contextmanager
def clone_exclusive(repo, wait=None):
    """The swap's hold: the gate (no new user), then the lock once every user let go. Yields False after wait s."""
    wait = env_num("AGENTCTL_CLONE_LOCK_WAIT", CLONE_LOCK_WAIT) if wait is None else wait
    os.makedirs(os.path.dirname(clone_side(repo, "x")), exist_ok=True)
    with open(clone_side(repo, "clone-gate.lock"), "a+") as gate, open(clone_side(repo, "clone.lock"), "a+") as lk:
        fcntl.flock(gate, fcntl.LOCK_EX)
        try:
            deadline = time.monotonic() + wait
            while True:
                try:
                    fcntl.flock(lk, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        yield False
                        return
                    time.sleep(0.2)
            try:
                yield True
            finally:
                fcntl.flock(lk, fcntl.LOCK_UN)
        finally:
            fcntl.flock(gate, fcntl.LOCK_UN)


def note_clone_suspect(repo, error):
    """A git error on the clone that looks like corruption: the clone health step looks at it next tick."""
    if heal_enabled(repo) and CORRUPT_GIT.search(error or ""):
        write_json(clone_side(repo, "suspect.json"), {"at": time.time(), "error": (error or "").strip()[-2000:]})
        return True
    return False


def fetch_main(repo):
    """git fetch <the forge remote> main in repo: (ok, git's stderr). A corruption signature marks the clone suspect."""
    r = git(repo, "fetch", "--quiet", remote(), "main", check=False)
    out = (r.stderr or "").strip()
    if r.returncode == 0 and git_ok(repo, "rev-parse", "--verify", "-q", f"{remote()}/main"):
        return True, out
    if not out:
        out = f"exit {r.returncode}" + ("" if r.returncode else f"; {remote()}/main does not resolve")
    note_clone_suspect(repo, out)
    return False, out


def probe_clone_health(a):
    """The clone verifies and a fetch of main works. A fetch failing without a corruption signature (the network,
    GitHub) cannot tell (None)."""
    repo = a.get("repo") or server_repo()
    with clone_shared(repo):
        if not os.path.isdir(os.path.join(repo, ".git")):
            return False, f"no clone at {repo}"
        ok, detail = clone_verify(repo)
        if not ok:
            return False, detail
        r = git(repo, "fetch", "--quiet", remote(), "main", check=False)
        err_ = (r.stderr or "").strip()
        if r.returncode != 0:
            return (False if CORRUPT_GIT.search(err_) else None), f"git fetch {remote()} main: {err_[-240:]}"
        ok, detail = clone_verify(repo)
        return ok, ("fetched and verified: " if ok else "") + detail


def relink_worktrees(repo, broken):
    """The broken clone's worktrees (the check and waiting trees) as worktrees of the new clone: their admin dirs made
    anew (the same paths: the clone's path did not change), HEAD on their branch when the new clone has it (carried
    over from the broken copy where readable), else detached at main, and their index rebuilt. Returns their paths."""
    out = []
    main_sha = git(repo, "rev-parse", f"{remote()}/main").strip()
    for b in git(broken, "for-each-ref", "--format=%(refname:short)", "refs/heads/", check=False).stdout.split():
        if b != "main":
            git(repo, "fetch", "--quiet", "--no-tags", broken, f"+refs/heads/{b}:refs/heads/{b}", check=False)
    admin_root = os.path.join(broken, ".git", "worktrees")
    for name in sorted(os.listdir(admin_root)) if os.path.isdir(admin_root) else []:
        old = os.path.join(admin_root, name)
        try:
            with open(os.path.join(old, "gitdir")) as f:
                gitfile = f.read().strip()
            with open(os.path.join(old, "HEAD")) as f:
                head = f.read().strip()
        except OSError:
            continue
        tree = os.path.dirname(gitfile)
        if not os.path.isfile(gitfile):
            continue
        new = os.path.join(repo, ".git", "worktrees", name)
        os.makedirs(new, exist_ok=True)
        with open(os.path.join(new, "gitdir"), "w") as f:
            f.write(gitfile + "\n")
        with open(os.path.join(new, "commondir"), "w") as f:
            f.write("../..\n")
        ref = head[5:].strip() if head.startswith("ref: ") else None
        with open(os.path.join(new, "HEAD"), "w") as f:
            f.write((f"ref: {ref}" if ref and git_ok(repo, "rev-parse", "--verify", "-q", ref) else main_sha) + "\n")
        with open(gitfile, "w") as f:
            f.write(f"gitdir: {new}\n")
        git(tree, "reset", "-q", check=False)  # the index from the new HEAD; the tree's files (its build) stay
        out.append(tree)
    return out


def prune_broken(repo, keep):
    """Every <repo>.broken-* but keep (at most one broken copy is kept)."""
    import shutil
    gone = []
    for p in glob.glob(f"{glob.escape(os.path.abspath(repo))}.broken-*"):
        if os.path.realpath(p) != os.path.realpath(keep):
            shutil.rmtree(p, ignore_errors=True)
            gone.append(p)
    return gone


def clone_condition(repo, text, probe, tail=()):
    return open_condition("CLONE", text, probe, [], key=f"clone-{os.path.basename(os.path.abspath(repo))}",
                          command="tick", tail=list(tail))


def repair_clone(repo, why):
    """Clone afresh into a temp directory, swap it in under the clone lock, keep the broken copy (one only), re-link the
    worktrees. Opens the CLONE condition (with git's error) first; its probe, passing after the swap, closes it.
    Returns what it did, or None when another repair runs or the users never let go (tried again next tick)."""
    import shutil
    if not _clone_repairing.acquire(blocking=False):
        return None
    try:
        _clone_repair_at[os.path.realpath(repo)] = time.time()
        url = os.environ.get("AGENTCTL_MOVIES_REMOTE")
        if not url:
            raise SystemExit(f"agentctl: cannot repair {repo}: no $AGENTCTL_MOVIES_REMOTE")
        try:
            clone_condition(repo, f"CLONE BROKEN: {repo}: {why.splitlines()[0][:300]}; re-cloning it from {url}",
                            run_probe("clone_health", {"repo": repo}), tail=why.splitlines()[-15:])
        except (Exception, SystemExit) as e:
            err(f"clone repair: the CLONE condition could not be opened ({str(e)[:160]})")
        err(f"clone repair: {repo} is broken ({why.splitlines()[0][:200]}): cloning {url} afresh")
        tmp = clone_into_tmp(repo, url)
        broken = f"{repo}.broken-{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}"
        with clone_exclusive(repo) as held:
            if not held:
                shutil.rmtree(tmp, ignore_errors=True)
                err(f"clone repair: {repo} stayed in use for "
                    f"{env_num('AGENTCTL_CLONE_LOCK_WAIT', CLONE_LOCK_WAIT):.0f} s; tried again next tick")
                _clone_repair_at.pop(os.path.realpath(repo), None)
                return None
            os.rename(repo, broken)
            os.rename(tmp, repo)
            try:
                trees = relink_worktrees(repo, broken)
            except (Exception, SystemExit) as e:
                trees = []
                err(f"clone repair: re-linking the worktrees failed ({str(e)[:160]}); check_tree makes them again")
        gone = prune_broken(repo, broken)
        try:
            os.unlink(clone_side(repo, "suspect.json"))
        except OSError:
            pass
        line = (f"clone repair: {repo} re-cloned and swapped in; the broken copy is {broken}"
                + (f"; {len(trees)} worktree(s) re-linked" if trees else "")
                + (f"; removed {', '.join(os.path.basename(g) for g in gone)}" if gone else ""))
        err(line)
        recovery_recheck("clone_health")
        return line
    finally:
        _clone_repairing.release()


def clone_heal_tick(repo):
    """The processor's "clone health" step: a suspect clone (a fetch failed with a corruption signature) is probed;
    broken, it is repaired (at most every CLONE_REPAIR_RETRY); healthy, the mark goes (it was something else)."""
    sus = read_record(clone_side(repo, "suspect.json"))
    if not sus:
        return None
    if time.time() - _clone_repair_at.get(os.path.realpath(repo), 0) < env_num("AGENTCTL_CLONE_REPAIR_RETRY",
                                                                               CLONE_REPAIR_RETRY):
        return None
    r = run_probe("clone_health", {"repo": repo})
    if r.ok:
        try:
            os.unlink(clone_side(repo, "suspect.json"))
        except OSError:
            pass
        return f"clone health: {repo} is fine ({r.detail})"
    if r.ok is None:
        return None  # cannot tell now (the network): looked at again next tick
    return repair_clone(repo, f"{r.detail}\n{sus.get('error') or ''}".strip())


# A RESOLVED CONFLICT KEEPS ITS PLACE (2026-11-19: lane F2b, bounced for a conflict main had made, rebased and queued
# again at the back, behind every change queued while it was out): a change queued from the same worktree and branch
# within CONFLICT_PLACE_KEPT s of its last entry's rejection for a conflict (`conflict_files`: no longer applies on
# main) takes that entry's seq -- the queue's age order -- instead of a new one. The rejected entry is final, so no two
# live entries share a seq. Tests: tests/test_generated.py.
CONFLICT_PLACE_KEPT = 24 * 3600


def conflict_bounced(every, e, now_=None):
    """The newest entry of e's worktree and branch, when it was rejected for a conflict within CONFLICT_PLACE_KEPT s
    (and nothing of that source was queued since): the place e takes. None otherwise."""
    mine = [x for x in every if x["id"] != e["id"] and same_change_source(x, e["worktree"], e.get("branch"))]
    last = max(mine, key=lambda x: x["seq"], default=None)
    if not last or last.get("status") != "rejected" or last.get("conflict_files") is None:
        return None
    try:
        at = dt.datetime.strptime(last.get("decided") or "", "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
    except ValueError:
        return None
    return last if (now_ or now().timestamp()) - at.timestamp() <= CONFLICT_PLACE_KEPT else None


def queue_change(d, fields, entry_id=None):
    """A submitted change as a queued entry (seq under seq.lock); re-queueing from the same source supersedes the older
    one. Returns (entry, n ahead). Shared by `land` (phase 1) and POST /v1/changes (phase 2)."""
    with open(os.path.join(d, "seq.lock"), "a+") as seq_lock:  # a unique seq; never the landing lock (a push holds it)
        fcntl.flock(seq_lock, fcntl.LOCK_EX)
        every = waiting_entries(d, open_only=False)
        seq = max([e["seq"] for e in every] + [0]) + 1
        eid = entry_id or f"{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}-{os.path.basename(fields['worktree'])}-{seq}"
        if any(x["id"] == eid for x in every):
            e = next(x for x in every if x["id"] == eid)
            return e, max(len(queued_entries(d)) - 1, 0)
        e = {**fields, "id": eid, "seq": seq, "status": "queued", "created": ts()}
        bounced = conflict_bounced(every, e)
        if bounced:  # A RESOLVED CONFLICT KEEPS ITS PLACE
            e.update(seq=bounced["seq"], requeued_from=bounced["id"])
        save_entry(d, e)
        for old in queued_entries(d):  # RE-QUEUEING REPLACES: only the newest entry per worktree and branch runs
            if old["id"] != e["id"] and same_change_source(old, e["worktree"], e.get("branch")):
                supersede(d, old, e["id"], f"{e['worktree']} queued again ({e['head'][:10]}); only the newest entry runs")
    return e, max(len(queued_entries(d)) - 1, 0)


SUBMIT_FIELDS = ("worktree", "branch", "head", "commits", "fixes", "keep_worktree", "claude_pid", "session", "base", "host",
                 "lander", "commit_keys", "no_batch")  # lander.py: who landed it, and its commits' keys; no_batch: land
# --no-batch (never in a backlog batch)


def submit_change(body, repo=None):
    """POST /v1/changes: a change the laptop pushed to refs/heads/land/<id> becomes a queued entry of the server's queue."""
    repo = repo or server_repo()
    if not os.path.isdir(repo):
        raise ValueError(f"the reconciler has not cloned {repo} yet")
    missing = [k for k in ("worktree", "head", "commits") if not body.get(k)]
    if missing or not re.fullmatch(r"[0-9a-f]{40}", str(body.get("head"))):
        raise ValueError(f"a change needs worktree, head (a full sha) and commits; missing or bad: {missing or ['head']}")
    if body.get("id") and not re.fullmatch(r"[\w.-]{1,120}", body["id"]):
        raise ValueError("a bad change id")
    why = do_not_land(repo, body["commits"] if isinstance(body["commits"], list) else str(body["commits"]).split(),
                      body.get("branch") or "")
    if why:
        raise ValueError(f"NOT LANDED: {why}")
    why = blame_land_refusal(repo, body["commits"] if isinstance(body["commits"], list) else
                             str(body["commits"]).split(), body.get("worktree"))
    if why:
        raise ValueError(f"NOT LANDED: {why}")
    d = waiting_state(repo)[0]
    e, ahead = queue_change(d, {k: body.get(k) for k in SUBMIT_FIELDS}, body.get("id"))
    supersede_stranded(d, e)
    return {"id": e["id"], "seq": e["seq"], "ahead": ahead}


def supersede_stranded(d, e, store=None):
    """A submission replaces every older open item of the same worktree that no entry carries (queue_change supersedes
    the entries; an item mirrored from another host's queue has none): SUPERSEDED, never left waiting."""
    st = store or Store()
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


def cancel_submitted(eid, how="cancel", repo=None):
    d = waiting_state(repo or server_repo())[0]
    hits = [e for e in queued_entries(d) + waiting_entries(d) if e["id"] == eid]
    if not hits:  # an item no entry carries (stranded): the item alone
        st = Store()
        it = st.item("change", eid)
        if not it or machine_state("change", it["state"]).terminal:
            raise KeyError(eid)
        st.transition("change", eid, "cancelled", it["version"], data={"why": f"{how}led through the store API"},
                      event={"text": f"{eid}: {how}led", "refs": {"entry": eid}})
        return {"id": eid, "status": "cancelled"}
    e = hits[0]
    set_change(d, e, "cancelled", event={"text": f"{e['id']}: {how}led"}, why=f"{how}led through the store API",
               decided=ts())
    cancel_entry_remote(e, f"its queue entry was {how}led")
    return {"id": eid, "status": "cancelled"}


def gate_is_red_now():
    ci = state_read().get("ci") or {}
    return any((g.get("conclusion") or "") in ("failure", "timed_out") for g in ci.get("gate") or [])


def track_red_time(st, red, now_=None):
    """meta red_accum: the seconds main has been red, summed (the W1 gauge leaves them out of a change's age)."""
    now_ = now_ if now_ is not None else st.clock()
    last = float(st.meta_get("red_accum_at", 0) or 0)
    acc = float(st.meta_get("red_accum", 0) or 0)
    if red and last:
        acc += max(0.0, min(now_ - last, 600))
        st.meta_set("red_accum", acc)
    st.meta_set("red_accum_at", now_)
    return acc


def metrics_text(st, now_=None):
    """The Prometheus text the module's agentctl-metrics copies to node-exporter (agentctl.rules' gauges)."""
    t = now_ if now_ is not None else st.clock()
    snap = state_read()
    tick = float(snap.get("tick_at") or 0)
    acc = float(st.meta_get("red_accum", 0) or 0)
    ages = []
    for it in st.items("change"):
        if machine_state("change", it["state"]).terminal:
            continue
        dd = it.get("data") or {}
        since = float(dd.get("submitted_at") or it["entered_at"])
        acc0 = dd.get("red_accum0")
        ages.append(max(0.0, (t - since) - max(0.0, acc - float(acc if acc0 is None else acc0))))
    unleased = 0.0
    try:
        for it in red_unheld(st):
            unleased = max(unleased, t - float(it.get("entered_at") or t))
    except Exception:
        pass
    two = 2 * env_num("AGENTCTL_TICK", TICK)
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
    past = sum(1 for typ in MACHINES for it in st.items(typ)
               if not machine_state(typ, it["state"]).terminal and it["deadline"] < t - two)
    rows = [("agentctl_reconciler_tick_timestamp", "unix time the last tick finished", tick),
            ("agentctl_oldest_change_age_seconds", "age of the oldest non-terminal change, not counting red main time",
             max(ages) if ages else 0),
            ("agentctl_red_unleased_seconds", "longest a red on main has had no lease, fix, block or offer", unleased),
            ("agentctl_leases_expired_unreaped", "leases past their TTL by more than 2 ticks with their item unmoved",
             unreaped),
            ("agentctl_items_past_deadline", "items past their state's deadline by more than 2 ticks", past)]
    out = []
    for name, help_, v in rows:
        out += [f"# HELP {name} {help_}.", f"# TYPE {name} gauge", f"{name} {float(v):.0f}"]
    return "\n".join(out) + "\n"


# THE STORE'S OWN TOKENS (github-return plan §4.1 W5b): `store serve` checks each caller's token itself -- no forge in
# the way. A token is `ast.<id>.<user>.<mac>`, mac = HMAC-SHA256 of
# "<id>.<user>" under the host's secret ($AGENTCTL_STORE_SECRET, default /var/lib/agentctl/store-secret: 32 random
# bytes, 0600, made by the first `store token issue`), and counts while <id> is in the registry beside the secret
# (store-tokens.json, 0600) unrevoked. Issued and revoked on the host as the store's user:
#   agentctl store token issue --user pawel [--name laptop]   (prints the token once; the laptop keeps it in the vault
#                                                              as bitcashier/AGENTCTL_STORE_TOKEN)
#   agentctl store token revoke <id>   /   agentctl store token list
# The check is a file read and an HMAC, so it is never cached: a revoke counts at the next request.
STORE_SECRET = "/var/lib/agentctl/store-secret"
STORE_AUTHS = ("store-tokens",)  # `store serve --auth`: the one way the store checks a token


def store_secret_path():
    return os.environ.get("AGENTCTL_STORE_SECRET") or STORE_SECRET


def store_tokens_path():
    return os.path.join(os.path.dirname(store_secret_path()), "store-tokens.json")


def store_secret(create=False):
    """The HMAC secret's bytes; made (32 random bytes, 0600, never overwritten) when create and absent."""
    path = store_secret_path()
    try:
        with open(path, "rb") as f:
            return f.read()
    except FileNotFoundError:
        if not create:
            return None
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(os.urandom(32))
    except FileExistsError:
        pass
    with open(path, "rb") as f:
        return f.read()


def store_token_mac(secret, tid, user):
    import hashlib, hmac
    return hmac.new(secret, f"{tid}.{user}".encode(), hashlib.sha256).hexdigest()


def store_tokens_load():
    try:
        with open(store_tokens_path()) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def store_tokens_save(reg):
    path = store_tokens_path()
    fd = os.open(path + ".new", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(reg, f, indent=1, sort_keys=True)
    os.replace(path + ".new", path)


def store_token_issue(user, name=""):
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", user or ""):
        raise SystemExit(f"agentctl store token: {user!r} is not a user name (letters, digits, _ and -)")
    secret = store_secret(create=True)
    import secrets as _secrets
    tid = _secrets.token_hex(6)
    reg = store_tokens_load()
    reg[tid] = {"user": user, "name": name or "", "issued": ts()}
    store_tokens_save(reg)
    return tid, f"ast.{tid}.{user}.{store_token_mac(secret, tid, user)}"


def store_token_revoke(tid):
    reg = store_tokens_load()
    if tid not in reg:
        raise SystemExit(f"agentctl store token: no token {tid}")
    reg[tid]["revoked"] = ts()
    store_tokens_save(reg)
    return reg[tid]


def store_token_user(tok):
    """The user a store token names, or None (malformed, a wrong mac, unknown, revoked, or no secret here)."""
    import hmac
    parts = (tok or "").split(".")
    if len(parts) != 4 or parts[0] != "ast":
        return None
    _, tid, user, mac = parts
    secret = store_secret()
    if not secret or not hmac.compare_digest(mac, store_token_mac(secret, tid, user)):
        return None
    try:
        rec = store_tokens_load().get(tid)
    except (OSError, ValueError):
        return None
    if not rec or rec.get("revoked") or rec.get("user") != user:
        return None
    return user


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
        auth = headers.get("Authorization") or headers.get("authorization") or ""
        m = re.match(r"(?i)(?:token|bearer)\s+(\S+)$", auth.strip())
        if not m:
            return self._json(401, {"error": "Unauthorized",
                                    "message": "Authorization: token <an agentctl store token> required"})
        try:
            who = self.login(m.group(1))
        except Exception as e:
            return self._json(503, {"error": "Unavailable", "message": f"the token could not be checked: {e}"})
        if who not in self.allow and not (who in self.metrics and method == "GET" and u.path == "/metrics"):
            return self._json(403, {"error": "Forbidden", "message": f"{who or 'this token'} may not call {u.path}"})
        try:
            mach = next((v for k, v in headers.items() if k.lower() == "x-agentctl-machine"), None)
            if mach and u.path.startswith("/v1/store/"):  # its leases' clocks run again (Store.contact)
                gap = self.store.contact(mach.strip())
                if gap:
                    sys.stderr.write(f"agentctl-store: {mach.strip()} back after its silence: its leases paused "
                                     f"{int(gap)} s\n")
            if body:
                args = json.loads(body)
            else:
                args = {}
            refused = laptop_role_refusal(self.store, u.path, args,
                                          next((v for k, v in headers.items() if k.lower() == "x-agentctl-role"), None))
            if refused:
                return self._json(409, {"error": "Standby", "message": refused})
            return self.route(method, u.path, q, args)
        except tuple(store_errors().values()) as e:
            return self._json(409, {"error": type(e).__name__, "message": str(e)})
        except KeyError as e:
            return self._json(404, {"error": "NotFound", "message": str(e)})
        except (ValueError, TypeError) as e:
            return self._json(400, {"error": "BadRequest", "message": str(e)})
        except Exception as e:  # ANY OTHER ERROR IS A 500 WITH ITS TRACEBACK IN THE JOURNAL, never a dropped connection
            return self.internal_error(method, u.path, e)

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
        if method == "POST" and p == "/v1/changes":
            return self._json(200, submit_change(b, self.repo))
        if method == "POST" and re.fullmatch(r"/v1/changes/[^/]+:(cancel|abandon)", p):
            eid, how = urllib.parse.unquote(p[len("/v1/changes/"):]).rsplit(":", 1)
            return self._json(200, cancel_submitted(eid, how, self.repo))
        if method == "POST" and p == "/v1/tick":
            with open(state_root("tick.nudge"), "a"):
                os.utime(state_root("tick.nudge"))
            return self._json(200, {"result": "ok"})
        if method == "POST" and p.startswith("/v1/store/"):
            m = p[len("/v1/store/"):]
            if m not in STORE_RPC:
                raise KeyError(m)
            return self._json(200, {"result": getattr(st, m)(*b.get("args", []), **b.get("kwargs", {}))})
        raise KeyError(f"{method} {p}")


def store_server(api, host, port):
    """A ThreadingHTTPServer serving api (returns it; serve_forever() is the caller's)."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _go(self, method):
            try:
                n = int(self.headers.get("Content-Length") or 0)
                if not (self.headers.get("Authorization") or "").strip():  # no token: answered before any body is read
                    code, ctype, out = api.handle(method, self.path, dict(self.headers), b"")
                    self.close_connection = True
                elif n > STORE_BODY_MAX:
                    code, ctype, out = api._json(413, {"error": "TooLarge", "message": f"a body over {STORE_BODY_MAX} bytes"})
                    self.close_connection = True
                else:
                    body = self.rfile.read(n) if n else b""
                    code, ctype, out = api.handle(method, self.path, dict(self.headers), body)
            except Exception as e:  # the last resort (handle() answers its own errors): a 500, never a dropped connection
                code, ctype, out = api.internal_error(method, self.path, e)
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def do_GET(self):
            self._go("GET")

        def do_POST(self):
            self._go("POST")

        def log_message(self, fmt, *args):  # the journal gets one line per non-2xx answer only
            if args and str(args[1] if len(args) > 1 else "").startswith(("4", "5")):
                sys.stderr.write("agentctl-store: %s\n" % (fmt % args))

    srv = ThreadingHTTPServer((host, port), Handler)
    srv.daemon_threads = True
    return srv


def cmd_store(a):
    """`agentctl store serve --listen ADDR:PORT --allow-user U... --metrics-user U...` (ci-runner);
    `agentctl store use URL` (the laptop: which store its sync and agents use)."""
    if a.scmd == "token":
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
    if a.scmd == "use":
        RemoteStore(a.url).state()  # it answers, with this token, before anything relies on it
        os.makedirs(state_root(), exist_ok=True)
        with open(state_root("store.url"), "w") as f:
            f.write(a.url + "\n")
        print(f"store: {a.url} (every agentctl process here; the sync re-reads it at its next start)")
        return 0
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

    def alive():
        while True:
            time.sleep(30)
            try:
                st.meta_set("last_alive", time.time())
            except Exception as e:
                err(f"store: last_alive not written ({e})")
    threading.Thread(target=alive, daemon=True).start()
    signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=srv.shutdown, daemon=True).start())
    err(f"store: serving {state_root('store.db')} on {host}:{port}, auth {a.auth} (allow {','.join(api.allow) or '-'}; metrics "
        f"{','.join(api.metrics) or '-'})")
    srv.serve_forever()
    return 0


RED_DERIVED_FIELDS = ("holder", "fix", "block", "cause", "until", "tier", "priority")  # the tick's to set, from the state


def red_import_merge(into, other):
    """One red's data from two spells of it: `other`'s history kept (fix, refuted, attempts, author, ...), the runs and
    jobs united, the newer last run and the earlier opening -- the red's NAMES are what links a Fixes-Red change to it."""
    by_id = lambda r: int((r or {}).get("databaseId") or 0)
    m = {**into, **{k: v for k, v in other.items() if v not in (None, [], {}, "") and not into.get(k)
                    and k not in RED_DERIVED_FIELDS}}
    m["runs"] = sorted(set(map(str, into.get("runs") or [])) | set(map(str, other.get("runs") or [])))
    m["jobs"] = sorted(set(into.get("jobs") or []) | set(other.get("jobs") or []))
    m["last_run"] = max((into.get("last_run"), other.get("last_run")), key=by_id) or into.get("last_run")
    opened = [float(x) for x in (into.get("opened_at"), other.get("opened_at")) if x]
    if opened:
        m["opened_at"] = min(opened)
    m["attempts"] = max(int(into.get("attempts") or 0), int(other.get("attempts") or 0))
    m["history"] = (other.get("history") or []) + [h for h in into.get("history") or [] if h not in (other.get("history") or [])]
    return m


def sd_notify(msg):
    """systemd's notify protocol ($NOTIFY_SOCKET; '@' an abstract socket): READY=1, WATCHDOG=1. A no-op elsewhere."""
    path = os.environ.get("NOTIFY_SOCKET")
    if not path:
        return False
    import socket
    addr = "\0" + path[1:] if path.startswith("@") else path
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.connect(addr)
            s.sendall(msg.encode())
        return True
    except OSError as e:
        err(f"sd_notify {msg}: {e}")
        return False


# ---- the laptop's sync (role "sync")
def sync_outbox_dir():
    return state_root("outbox")


def submit_remote(st, e):
    """Submit a change to the remote store; unreachable: kept in the outbox (QUEUED-LOCAL) for the sync to send."""
    try:
        return st.submit(e)
    except (StoreUnavailable, NoToken) as ex:  # no token here: the sync (the laptop processor) sends it with its own
        os.makedirs(sync_outbox_dir(), exist_ok=True)
        write_json(os.path.join(sync_outbox_dir(), f"{e['id']}.json"), e)
        return {"id": e["id"], "outbox": str(ex)[:200]}


def flush_outbox(st=None):
    """The changes `land` could not submit (the store down): sent now, oldest first. Returns how many went."""
    st = st or Store()
    n = 0
    for p in sorted(glob.glob(os.path.join(sync_outbox_dir(), "*.json"))):
        e = read_record(p)
        if not e:
            continue
        try:
            st.submit(e)
        except StoreUnavailable:
            break
        except Exception as ex:  # refused (bad request): said once, dropped
            inbox_write("land", f"NOT LANDED: {e.get('id')}: the store refused it ({str(ex)[:200]})", [], [], 1,
                        cwd=e.get("worktree") or safe_cwd())
        os.unlink(p)
        n += 1
    return n


def mirror_state(st=None, now_=None):
    """ci-runner's /v1/state copied into the laptop's state.json (with fetched_at): the status line reads it."""
    st = st or Store()
    try:
        snap = st.state()
    except StoreUnavailable as e:
        note_store_error(e, now_)
        raise
    state_update(**{**snap, "fetched_at": now_ if now_ is not None else time.time(), "store_error": None})
    return snap


# WHO MOVES EACH MACHINE PAST ITS DEADLINE (2026-11-12, issue 20261111T124954-ec04bd: `issue` and `lander` had a
# deadline and no tick, so AgentctlItemsPastDeadline fired for days). Every machine is named here with the step that
# fires its on_deadline; test_machines refuses a machine that is not. QUIET_DEADLINE_TYPES are the machines whose
# deadline exit is a quiet retirement nobody else ticks: tick_quiet_deadlines moves them.
DEADLINE_TICKS = {"change": "tick_changes", "batch": "tick_batch", "watch": "tick_watches", "hold": "tick_land_hold",
                  "red": "red_tick", "branch-red": "settle_branch_reds", "condition": "recovery_tick",
                  "issue": "tick_issues", "lander": "tick_quiet_deadlines", "explorer-flip": "tick_quiet_deadlines"}
QUIET_DEADLINE_TYPES = tuple(k for k, v in DEADLINE_TICKS.items() if v == "tick_quiet_deadlines")


def tick_quiet_deadlines(st=None, now_=None):
    """The reconciler's step: each lander record past its 14 days goes `gone` (blame reads landings that recent) and
    each forgotten explorer flip `closed`, an info event apiece (no inbox entry, no notification). Returns what it did."""
    st = st or Store()
    t = now_ if now_ is not None else st.clock()
    did = []
    for typ in QUIET_DEADLINE_TYPES:
        for it in st.items(typ):
            spec = machine_state(typ, it["state"])
            if spec.terminal or it["deadline"] > t:
                continue
            try:
                st.transition(typ, it["id"], spec.on_deadline, it["version"],
                              event={"text": f"{typ} {it['id']}: {it['state']} past its deadline -> {spec.on_deadline}",
                                     "severity": "info"})
            except (StaleVersion, TerminalState):
                continue
            did.append(f"{typ} {it['id']}: {spec.on_deadline}")
    return did


def server_steps(repo, d, shared):
    """The reconciler's tick on ci-runner (the one writer): the changes, the batch, the watches, the gate, the reds,
    the recovery registry. Never the laptop's duties (events -> inbox, the notifier, the network and tunnel watches, the
    fixers, upkeep): those are sync_steps."""
    return (("clone health", lambda: [wlog(d, f"tick: {x}") for x in [clone_heal_tick(repo)] if x]
             if heal_enabled(repo) else None),
            ("change items", lambda: project_changes(d)),
            ("landed changes", shared(lambda: [wlog(d, f"tick: {x}") for x in close_landed_changes(repo, d)])),
            ("stranded changes", lambda: stranded_changes(repo, d)),
            ("change tick", shared(lambda: [wlog(d, f"tick: {x}") for x in tick_changes(repo, d)])),
            ("land hold", tick_land_hold),
            ("batch tick", shared(lambda: say_batch(d, tick_batch(repo, d)))),
            ("watches", lambda: [wlog(d, f"tick: {x}") for x in tick_watches()]),
            ("land refs", shared(lambda: [wlog(d, f"tick: {x}") for x in sweep_land_refs(repo, d)])),
            ("lease archive", lambda: maybe_archive_leases(d)),
            ("children", reap_children),
            ("gate summary", lambda: maybe_publish_gate(repo)),
            ("red time", lambda: track_red_time(Store(), gate_is_red_now())),
            ("red tick", lambda: maybe_red_tick(repo)),
            ("recovery", maybe_recovery_tick),
            ("issues", lambda: [wlog(d, f"tick: {x}") for x in tick_issues()]),
            ("quiet deadlines", lambda: [wlog(d, f"tick: {x}") for x in tick_quiet_deadlines()]),
            ("stats", shared(lambda: maybe_stats_pass(repo, d))))


def sync_steps(repo, d, o_r):
    """The laptop's loop once the store is remote: it never ticks."""
    last_mirror = [0.0]

    def mirror():
        if time.time() - last_mirror[0] >= env_num("AGENTCTL_SYNC_MIRROR", SYNC_MIRROR_EVERY):
            last_mirror[0] = time.time()
            mirror_state()

    def sync_claims():
        """The laptop's holders on ci-runner's store: every claim renewed by its worktree's activity; every fixer and
        owner lease held by no work revoked (red_reap_orphans)."""
        st = Store()
        red_renew_holders(st, time.time())
        red_reap_orphans(st, time.time())

    def pins():
        """The convergence pins read with the owner's token the processor holds and written into the store: the
        reconciler on ci-runner reads them from there (ci-lander may not read releases). Bounded: a network read."""
        if not run_bounded("pins", lambda: refresh_pinned_recording(o_r, repo, role="sync"),
                           env_num("AGENTCTL_STEP_BOUND", 120)):
            err("pinned recording: still reading from an earlier cycle")

    def explorer():
        if not run_bounded("drift explorer", lambda: [err(x) for x in explorer_tick(o_r, repo)],
                           env_num("AGENTCTL_STEP_BOUND", 120)):
            err("drift explorer: still running from an earlier cycle")

    def dispatch():
        if os.path.exists(reds_dir("DISPATCH_OFF")):
            return []
        return red_dispatcher(Store(), o_r, repo, time.time())
    role = [False]

    def claim_role():
        role[0] = laptop_role_step()

    def duty(fn):  # ONE MACHINE HOLDS THE LAPTOP ROLE: a standby machine runs none of the laptop's duties
        return lambda: fn() if role[0] else None
    return (("laptop role", claim_role),
            ("outbox", flush_outbox), ("state mirror", mirror), ("children", reap_children),
            ("events", project_events), ("notifier", duty(notify_escalations)), ("alerts", duty(reconcile_alerts)),
            ("workspace trust", lambda: [err(x) for x in trust_asks()]),
            ("landed worktrees", lambda: [err(x) for x in close_landed_worktrees()]),
            ("local queue", lambda: [err(f"land: {x}") for x in migrate_local_queue(repo)]),
            ("landed changes", lambda: [err(f"land: {x}") for x in close_landed_changes(repo)]),
            ("claims", duty(sync_claims)),
            ("dispatcher", duty(dispatch)),
            ("pins", duty(pins)), ("drift explorer", duty(explorer)),
            ("fixer landing", lambda: [err(f"red: {x}") for x in fixers_land_watch()]),
            ("network watch", network_watch), ("tunnel probe", tunnel_watch),
            ("upkeep", lambda: [err(f"upkeep: {x}") for x in maybe_upkeep()]),
            ("recovery", maybe_recovery_tick))


def close_landed_worktrees(store=None):
    """The sync's half of WATCH OK while the reconciler runs on ci-runner: the worktree a change was landed from is on
    this laptop, so each `watch.ok` event's `close` worktree is closed here (close_worktree: only when proven merged and
    clean). Its own cursor (meta closer_seq). Returns what it said."""
    st = store or Store()
    out = []
    for ev in st.events(after=int(st.meta_get("closer_seq", 0) or 0)):
        wt = (ev.get("refs") or {}).get("close")
        if ev["kind"] == "watch.ok" and wt and os.path.isdir(wt):
            out.append(close_worktree(wt))  # raising: the cursor stays before it, tried again next cycle
        st.meta_set("closer_seq", ev["seq"])
    return out


# ---------------------------------------------------------------- the laptop role: ONE machine holds it
# ONE MACHINE HOLDS THE LAPTOP ROLE (2026-11-18 23:41Z, core Main Pipeline #1053, red 0c8eea91b712): a second Mac
# (`pawel-mbp`, its store tunnel key `store-tunnel@pawel-mbp`) still ran agentctl's sync loop against ci-runner's store,
# so both machines acted as "the laptop": it dispatched fixer red-0c8eea91b712-1 (pid 83138, session 3acddaae), this
# machine (Pawes-MacBook-Pro-2) found no such pid among its own processes and revoked it, and the two took turns in a
# crash loop. host_role() says only "sync"; both were it. Now the role is a LEASE in the shared store, `role/laptop`,
# held by `role:<host>:<machine id>` (macOS IOPlatformUUID) and renewed by every sync pass. A machine that finds it held
# by another live holder is STANDBY: it refuses every laptop duty (the dispatcher, red_dispatch, owners, the notifier,
# the alerts, the orphan reaper, the pins, the explorer) and tells its owner so ONCE. `agentctl laptop take` hands the
# role over (confirmed); a holder silent past LAPTOP_ROLE_TTL is taken over by the next machine that asks. The holder
# names no `laptop:` prefix: its clock never pauses with the laptop's contact (A SILENT MACHINE'S LEASE CLOCKS PAUSE),
# so a dead machine's role lapses. A fresh store with no role lease standing refuses nothing (one machine, tests).
LAPTOP_ROLE_LEASE = "role/laptop"
LAPTOP_ROLE_TTL = 10 * 60          # s a silent holder keeps the role ($AGENTCTL_LAPTOP_ROLE_TTL)
LAPTOP_ROLE_MAX = 100 * 365 * 86400
_laptop_role_seen = process_cache({})


def machine_id():
    """This machine's stable id: $AGENTCTL_MACHINE_ID, else macOS's IOPlatformUUID, else its hostname."""
    if os.environ.get("AGENTCTL_MACHINE_ID"):
        return os.environ["AGENTCTL_MACHINE_ID"]
    if "id" not in _laptop_role_seen:
        uid = None
        try:
            out = subprocess.run(["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"], capture_output=True, text=True,
                                 timeout=10).stdout
            m = re.search(r'"IOPlatformUUID"\s*=\s*"([^"]+)"', out)
            uid = m.group(1) if m else None
        except (OSError, subprocess.SubprocessError):
            pass
        _laptop_role_seen["id"] = uid or socket_host()
    return _laptop_role_seen["id"]


def role_host():
    return os.environ.get("AGENTCTL_HOSTNAME") or socket_host()


def laptop_role_holder():
    return f"role:{role_host()}:{machine_id()}"


def laptop_role_path():
    return state_root("laptop-role.json")


def laptop_role(store, t=None, take=False):
    """This machine's claim on the laptop role: (True, its lease row) when it holds it (acquired, or renewed by this
    pass), else (False, the other holder's live row). take: the role taken over from a live holder (`laptop take`)."""
    t = store.clock() if t is None else t
    me = laptop_role_holder()
    ttl = env_num("AGENTCTL_LAPTOP_ROLE_TTL", LAPTOP_ROLE_TTL)
    row = store.get(LAPTOP_ROLE_LEASE)
    live = bool(row and row.get("holder") and float(row.get("expires_at") or 0) > t)
    mine = read_record(laptop_role_path()) or {}
    if live and row["holder"] != me and not take:
        return False, row
    if live and row["holder"] == me and mine.get("epoch") == row.get("epoch"):
        r = store.renew(LAPTOP_ROLE_LEASE, mine["epoch"], mine["secret"], f"beat {t}", ttl)
        if r in ("ok", "no_progress"):
            return True, store.get(LAPTOP_ROLE_LEASE)
    if live:  # taken over (`laptop take`), or ours from a state file this machine lost: the lease is begun again
        store.revoke(LAPTOP_ROLE_LEASE, f"taken by {me}" if row["holder"] != me else "renewed after a restart")
    got = store.acquire(LAPTOP_ROLE_LEASE, me, ttl, LAPTOP_ROLE_MAX, progress=f"beat {t}",
                        meta={"kind": "laptop-role", "host": role_host(), "machine_id": machine_id(),
                              "since": t, "took_from": (row or {}).get("holder")})
    if "held" in got:
        return False, store.get(LAPTOP_ROLE_LEASE)
    write_json(laptop_role_path(), {"epoch": got["epoch"], "secret": got["secret"], "holder": me, "since": t})
    return True, store.get(LAPTOP_ROLE_LEASE)


def laptop_role_standby(store=None, t=None):
    """None when this machine may do laptop duties (it holds the role, or no live holder stands); else the standby
    line naming the holder. Read only: never acquires."""
    try:
        st = store or Store()
        t = st.clock() if t is None else t
        row = st.get(LAPTOP_ROLE_LEASE)
    except Exception:  # an unread store decides nothing here: the duty's own store calls fail on their own
        return None
    if not row or not row.get("holder") or float(row.get("expires_at") or 0) <= t or row["holder"] == laptop_role_holder():
        return None
    return laptop_role_line(row)


def laptop_role_line(row):
    m = lease_meta(row)
    since = m.get("since") or row.get("acquired_at")
    when = time.strftime("%Y-%m-%d %H:%MZ", time.gmtime(float(since))) if since else "?"
    return (f"laptop role held by {m.get('host') or row.get('holder')} since {when}; this machine "
            f"({role_host()}) is standby -- `agentctl laptop take` hands the role to it")


def laptop_role_step(store=None):
    """The sync loop's first step: the role claimed or renewed. Standby: said to this machine's owner ONCE per holder
    (its inbox), then only in `agentctl status`. Returns True when this machine holds the role."""
    st = store or Store()
    held, row = laptop_role(st)
    rec = read_record(laptop_role_path()) or {}
    if held:
        if rec.get("told"):
            rec.pop("told", None)
            write_json(laptop_role_path(), rec)
        return True
    if rec.get("told") != (row or {}).get("holder"):
        line = f"LAPTOP STANDBY: {laptop_role_line(row)}"
        inbox_write("laptop", line, [line], [], 1)
        err(line)
        write_json(laptop_role_path(), {**rec, "told": (row or {}).get("holder")})
    return False


def cmd_laptop(a):
    """`agentctl laptop status|take [--yes]`: who holds the laptop role; take it for this machine."""
    st = Store()
    if a.lcmd == "status":
        row = st.get(LAPTOP_ROLE_LEASE)
        line = laptop_role_standby(st)
        if line:
            print(line)
        elif row and row.get("holder"):
            print(f"laptop role held by this machine ({role_host()}, {machine_id()}) since "
                  f"{time.strftime('%Y-%m-%d %H:%MZ', time.gmtime(float(lease_meta(row).get('since') or 0)))}")
        else:
            print("laptop role: free (the next sync pass takes it)")
        return 0
    line = laptop_role_standby(st)
    if line and not a.yes:
        if not sys.stdin.isatty():
            print(f"{line}\nNOT TAKEN: confirm with `agentctl laptop take --yes`")
            return 1
        if input(f"{line}\nTake the laptop role for {role_host()}? Type yes: ").strip() != "yes":
            print("NOT TAKEN")
            return 1
    held, row = laptop_role(st, take=bool(line))
    print(f"TAKEN: laptop role held by {role_host()} ({machine_id()})" if held else f"NOT TAKEN: {laptop_role_line(row)}")
    return 0 if held else 1


LAPTOP_HOLDER_CALLS = ("acquire", "take")


def laptop_role_refusal(store, path, body, role):
    """THE STORE ENFORCES THE ROLE: a `laptop:` lease (a fixer, an owner, a claim) is acquired only by the machine that
    holds the laptop role -- a client naming another holder in X-Agentctl-Role, or none (an agentctl from before the
    role, like pawel-mbp's on 2026-11-18), is refused while a live holder stands. The refusal's text, or None."""
    m = path[len("/v1/store/"):] if path.startswith("/v1/store/") else ""
    if m not in LAPTOP_HOLDER_CALLS or not isinstance(body, dict):
        return None
    args, kw = body.get("args") or [], body.get("kwargs") or {}
    holder = kw.get("holder") if "holder" in kw else (args[1] if len(args) > 1 else None)
    if not str(holder or "").startswith("laptop:"):
        return None
    row = store.get(LAPTOP_ROLE_LEASE)
    if not row or not row.get("holder") or float(row.get("expires_at") or 0) <= store.clock() or row["holder"] == role:
        return None
    return (f"STANDBY: {holder} refused: {laptop_role_line(row).split(';')[0]}, and this client is "
            f"{role or 'an agentctl from before the laptop role'}")
