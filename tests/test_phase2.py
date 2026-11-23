"""Phase 2 (design §6.3, §13 phase 2): the store over HTTP with token auth, RemoteStore, the laptop's sync, the
reconciler's entry point on ci-runner -- against a local server on a temp port (USERS stands in for the store's own
tokens; StoreTokensTest checks the real ones)."""
import argparse, contextlib, io, json, os, re, signal, socket, subprocess, sys, tempfile, threading, time, unittest, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A, seed_gating_checks  # noqa: E402
from fake_clock import FakeClock, on_clock  # noqa: E402

USERS = {"tok-pawel": "pawel", "tok-metrics": "ci-metrics", "tok-other": "mallory"}


class Phase2Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="agentctl-p2-")
        self.saved = {k: os.environ.get(k) for k in ("AGENTCTL_STATE_ROOT", "AGENTCTL_STORE_URL", "AGENTCTL_STORE_TOKEN",
                                                    "AGENTCTL_ROLE", "AGENTCTL_MOVIES_REPO", "NOTIFY_SOCKET",
                                                    "AGENTCTL_STORE_RETRY_BASE", "AGENTCTL_LAPTOP_PRESSURE")}
        os.environ["AGENTCTL_STORE_RETRY_BASE"] = "0.01"  # the retries' backoff, short here
        os.environ["AGENTCTL_LAPTOP_PRESSURE"] = "-"  # the laptop running the tests is never the one under test
        os.environ["AGENTCTL_STATE_ROOT"] = os.path.join(self.tmp, "state")
        for k in ("AGENTCTL_STORE_URL", "AGENTCTL_STORE_TOKEN", "NOTIFY_SOCKET"):
            os.environ.pop(k, None)
        # This process is ci-runner (the store's thread, the tick); a test playing the laptop's sync says so (sync())
        os.environ["AGENTCTL_ROLE"] = "server"
        self.st = A.Store(path=os.path.join(self.tmp, "state", "store.db"))
        self.repo = os.path.join(self.tmp, "movies")
        subprocess.run(["git", "init", "-q", self.repo], check=True)
        self.api = A.StoreAPI(self.st, ["pawel"], ["ci-metrics"], user_of=USERS.get, repo=self.repo)
        self.srv = A.store_server(self.api, "127.0.0.1", 0)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_port}"
        self.remote = A.RemoteStore(self.url, "tok-pawel")

    def tearDown(self):
        self.srv.shutdown()
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def lapsed_lease(self, name, holder, epoch, t, max_until=None):
        """A lease row on ci-runner's store held by `holder` at `epoch`, expired a minute ago."""
        self.st.acquire(name, holder, 600, 3600)
        c = self.st.db()
        c.execute("UPDATE leases SET epoch=?, expires_at=?, max_until=? WHERE name=?",
                  (epoch, t - 60, max_until or t + 3600, name))
        c.close()

    def sync(self):
        """This process plays the laptop's sync from here on: open_store() is RemoteStore(self.url) -- the server thread's
        own open_store() calls included, which reach the same database through the API."""
        os.environ.pop("AGENTCTL_ROLE", None)
        os.environ.update(AGENTCTL_STORE_URL=self.url, AGENTCTL_STORE_TOKEN="tok-pawel")

    def get(self, path, tok=None):
        req = urllib.request.Request(self.url + path, headers={"Authorization": f"token {tok}"} if tok else {})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()


class AuthTest(Phase2Base):
    def test_a_tokens_login_decides_what_it_may_call(self):
        self.assertEqual(self.get("/v1/state")[0], 401)                    # no token
        self.assertEqual(self.get("/v1/state", "tok-unknown")[0], 403)     # the store does not know it
        self.assertEqual(self.get("/v1/state", "tok-other")[0], 403)       # a real user, not allowed
        self.assertEqual(self.get("/v1/state", "tok-metrics")[0], 403)     # the metrics identity: /metrics only
        code, body = self.get("/metrics", "tok-metrics")
        self.assertEqual(code, 200)
        for g in ("agentctl_reconciler_tick_timestamp", "agentctl_oldest_change_age_seconds",
                  "agentctl_red_unleased_seconds", "agentctl_leases_expired_unreaped", "agentctl_items_past_deadline"):
            self.assertIn(f"\n{g} ", "\n" + body)
        self.assertEqual(self.get("/v1/state", "tok-pawel")[0], 200)


class HardeningTest(Phase2Base):
    def raw(self, headers, body=b""):
        import http.client
        c = http.client.HTTPConnection("127.0.0.1", self.srv.server_port, timeout=10)
        c.putrequest("POST", "/v1/store/get")
        for k, v in headers.items():
            c.putheader(k, v)
        c.endheaders()
        if body:
            c.send(body)
        r = c.getresponse()
        return r.status, r.read()

    def test_no_token_or_a_huge_body_is_answered_without_reading_the_body(self):
        self.assertEqual(self.raw({"Content-Length": str(1 << 30)})[0], 401)  # 1 GiB announced, never read
        self.assertEqual(self.raw({"Content-Length": str(A.STORE_BODY_MAX + 1), "Authorization": "token tok-pawel"})[0],
                         413)

    def test_a_safe_call_rides_out_a_500_and_an_unsafe_one_does_not(self):
        calls = []
        real = A.urllib.request.urlopen

        def flaky(req, timeout=None):
            if "/v1/store/" not in req.full_url:  # anything but the store call under test
                return real(req, timeout=timeout)
            calls.append(req.full_url)
            if len(calls) == 1:
                raise A.urllib.error.HTTPError(req.full_url, 500, "database is locked", {}, io.BytesIO(b"{}"))
            return real(req, timeout=timeout)
        A.urllib.request.urlopen = flaky
        os.environ["AGENTCTL_STORE_RETRY_BASE"] = "0"
        try:
            self.assertIsNone(self.remote.get("red/nothing"))
            self.assertEqual(len(calls), 2, calls)
            calls.clear()
            with self.assertRaises(A.StoreUnavailable):
                self.remote.revoke("red/nothing", "x")
        finally:
            A.urllib.request.urlopen = real
            os.environ.pop("AGENTCTL_STORE_RETRY_BASE", None)


class RemoteStoreTest(Phase2Base):
    def test_remote_store_is_the_store_over_http(self):
        r = self.remote
        took = r.acquire("red/x", "laptop:fixer:a", 600, 3600, progress="p0", meta={"kind": "fixer"})
        self.assertIn("held", r.acquire("red/x", "laptop:fixer:b", 600, 3600))
        self.assertEqual(r.renew("red/x", took["epoch"], took["secret"], "p1", 600), "ok")
        self.assertEqual(r.renew("red/x", took["epoch"], took["secret"], "p1", 600), "no_progress")
        self.assertEqual(r.get("red/x")["holder"], "laptop:fixer:a")
        self.assertEqual(self.st.get("red/x")["epoch"], took["epoch"])  # the server's database
        self.assertEqual(r.release("red/x", took["epoch"], took["secret"], outcome="LANDED abc"), "ok")
        it = r.put_item("condition", "c1", "open", {"kind": "X"})
        r.transition("condition", "c1", "closed", it["version"], event={"text": "closed"})
        with self.assertRaises(A.StaleVersion):  # the store's exceptions cross the wire as themselves
            r.transition("condition", "c1", "open", it["version"])
        oid = r.offer("fixer", "red:1", "red/y", "fresh", 1, time.time() + 600, {"workflow": "Main"})
        self.assertEqual([o["id"] for o in r.offers("fixer")], [oid])
        got = r.take(oid, "laptop:fixer:c", 900, 3600)
        self.assertEqual(got["picture"]["workflow"], "Main")
        self.assertEqual(r.offers("fixer"), [])
        evs = r.events(after=0)
        self.assertTrue(any(e["kind"] == "condition.closed" for e in evs))

    def test_open_store_returns_a_remote_store_when_the_url_is_set_and_every_method_goes_through_the_rpc(self):
        self.sync()
        s = A.open_store()  # the composition root's opener (cli wires process_store)
        self.assertIsInstance(s, A.RemoteStore)
        self.assertNotIsInstance(A.Store(), A.RemoteStore)  # the class is the file store, never another one
        self.assertIsInstance(A.Store(path=os.path.join(self.tmp, "x.db")), A.Store)
        took = s.acquire("land/movies", "h", 60, 120)  # every Store method through POST /v1/store/<method>
        self.assertEqual(s.renew("land/movies", took["epoch"], took["secret"], "a", 60), "ok")
        self.assertEqual([x["name"] for x in s.list("land/")], ["land/movies"])
        s.release("land/movies", took["epoch"], took["secret"])
        self.assertFalse(s.get("land/movies")["holder"])
        self.assertEqual(self.status_of("GET", "/v1/leases"), 404, "the REST lease routes are gone: one transport")

    def test_store_use_switches_every_process_here_and_there_is_no_local_store(self):
        os.environ.pop("AGENTCTL_ROLE")
        os.environ["AGENTCTL_STORE_TOKEN"] = "tok-pawel"
        self.assertEqual(A.host_role(), "sync")  # the laptop: always the sync, never a store of its own
        self.assertEqual(A.open_store().url, "http://127.0.0.1:9")  # the default (ci-runner's; the tests' closed port)
        self.assertEqual(A.main(["store", "use", self.url]), 0)
        self.assertIsInstance(A.open_store(), A.RemoteStore)
        self.assertEqual(A.open_store().url, self.url)
        with self.assertRaises(A.StoreUnavailable):  # a store that does not answer is never switched to
            A.main(["store", "use", "http://127.0.0.1:9"])
        self.assertEqual(A.open_store().url, self.url)
        with self.assertRaises(SystemExit):
            A.main(["store", "use", "--local"])  # gone with the laptop's store
        with self.assertRaises(SystemExit):
            A.main(["store", "import", "--to", self.url])  # gone: a lost store is rebuilt on ci-runner (design §16)

    def status_of(self, method, path):
        try:
            self.remote.call(method, path)
        except A.StoreUnavailable as e:
            return int(re.search(r"HTTP (\d+)", str(e)).group(1))
        return 200



class SubmitTest(Phase2Base):
    def test_a_submitted_change_is_a_queued_entry_of_the_servers_queue_and_can_be_cancelled(self):
        head = "a" * 40
        r = self.remote.submit({"id": "20261029-0100-movies-wt-x-ab12cd", "worktree": "/Users/p/projects/movies-wt-x",
                                "branch": "x", "head": head, "commits": [head], "fixes": [], "base": "b" * 40})
        self.assertEqual((r["id"], r["seq"], r["ahead"]), ("20261029-0100-movies-wt-x-ab12cd", 1, 0))
        d = A.waiting_state(self.repo)[0]
        [e] = A.queued_entries(d)
        self.assertEqual((e["status"], e["worktree"], e["head"]), ("queued", "/Users/p/projects/movies-wt-x", head))
        again = self.remote.submit({"id": r["id"], "worktree": e["worktree"], "head": head, "commits": [head]})
        self.assertEqual(again["seq"], 1)  # a resubmission (the outbox retried) is the same change
        r2 = self.remote.submit({"worktree": "/Users/p/projects/movies-wt-x", "branch": "x", "head": "c" * 40,
                                 "commits": ["c" * 40]})
        self.assertEqual([x["id"] for x in A.queued_entries(d)], [r2["id"]])  # re-queueing replaces
        with self.assertRaises(A.StoreUnavailable):
            self.remote.submit({"worktree": "w", "head": "not-a-sha", "commits": ["x"]})
        self.remote.cancel_change(r2["id"])
        self.assertEqual(A.queued_entries(d), [])

    def test_land_keeps_a_change_in_the_outbox_while_the_store_is_down_and_the_sync_sends_it(self):
        down = A.RemoteStore("http://127.0.0.1:9", "tok-pawel", timeout=2)
        e = {"id": "20261029-0101-wt-y-0000aa", "worktree": "/w/y", "head": "d" * 40, "commits": ["d" * 40]}
        self.assertTrue(A.submit_remote(down, e).get("outbox"))
        self.assertEqual(A.flush_outbox(down), 0)
        self.assertEqual(A.flush_outbox(self.remote), 1)
        self.assertEqual([x["id"] for x in A.queued_entries(A.waiting_state(self.repo)[0])], [e["id"]])
        self.assertEqual(os.listdir(A.sync_outbox_dir()), [])


class RetryTest(Phase2Base):
    def flaky_proxy(self, fail_first):
        """A TCP proxy to the store that resets its first `fail_first` connections after reading the request."""
        import socketserver
        seen = []
        target = ("127.0.0.1", self.srv.server_port)

        class H(socketserver.BaseRequestHandler):
            def handle(self):
                seen.append(1)
                if len(seen) <= fail_first:
                    self.request.recv(65536)
                    self.request.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, b"\x01\x00\x00\x00\x00\x00\x00\x00")
                    self.request.close()  # RST: "Connection reset by peer"
                    return
                up = socket.create_connection(target)
                self.request.settimeout(5)
                up.settimeout(5)
                data = self.request.recv(65536)
                while b"\r\n\r\n" in data and len(data) < int((data.split(b"Content-Length: ")[1].split(b"\r\n")[0]
                                                                  if b"Content-Length: " in data else b"0")) + \
                        data.index(b"\r\n\r\n") + 4:
                    data += self.request.recv(65536)
                up.sendall(data)
                out = b""
                while True:
                    try:
                        chunk = up.recv(65536)
                    except OSError:
                        break
                    if not chunk:
                        break
                    out += chunk
                    if b"\r\n\r\n" in out:
                        head, body = out.split(b"\r\n\r\n", 1)
                        n = int(head.split(b"Content-Length: ")[1].split(b"\r\n")[0]) if b"Content-Length: " in head else 0
                        if len(body) >= n:
                            break
                self.request.sendall(out)
                up.close()
        srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), H)
        srv.daemon_threads = True
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        return f"http://127.0.0.1:{srv.server_address[1]}", seen

    def test_a_transient_reset_is_retried_and_a_persistent_outage_is_no_verdict(self):
        took = self.st.acquire("red/r", "laptop:fixer:r", 600, 3600)
        url, seen = self.flaky_proxy(2)
        r = A.RemoteStore(url, "tok-pawel")
        self.assertEqual(r.get("red/r")["holder"], "laptop:fixer:r")  # two resets, then an answer
        self.assertEqual(len(seen), 3)
        url, seen = self.flaky_proxy(1)
        self.assertEqual(A.RemoteStore(url, "tok-pawel").renew("red/r", took["epoch"], took["secret"], "p1", 600), "ok")
        # a mutation that may have reached the server is not sent twice
        url, seen = self.flaky_proxy(1)
        with self.assertRaises(A.StoreUnavailable):
            A.RemoteStore(url, "tok-pawel").acquire("red/s", "h", 600, 3600)
        self.assertEqual(len(seen), 1)
        # refused (never reached the server): sent again, and a store that stays down raises after the bounded retries
        t0 = time.time()
        with self.assertRaises(A.StoreUnavailable):
            A.RemoteStore("http://127.0.0.1:9", "tok-pawel").acquire("red/s", "h", 600, 3600)
        self.assertLess(time.time() - t0, 5)


class InternalErrorTest(Phase2Base):
    def test_an_unexpected_error_is_a_500_with_a_json_error_never_a_dropped_connection(self):
        saved = A.submit_change

        def boom(body, repo=None):
            raise FileNotFoundError(2, "No such file or directory", "git")
        A.submit_change = boom
        try:
            with self.assertRaises(A.StoreUnavailable) as cm:
                self.remote.submit({"worktree": "/w", "head": "a" * 40, "commits": ["a" * 40]})
        finally:
            A.submit_change = saved
        self.assertIn("HTTP 500", str(cm.exception))
        self.assertIn("FileNotFoundError", str(cm.exception))
        self.assertEqual(self.remote.state(), A.state_read())  # the server goes on answering


class ServerSideTest(Phase2Base):
    def test_sd_notify_speaks_systemds_protocol(self):
        path = os.path.join(self.tmp, "notify.sock")
        s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        s.bind(path)
        os.environ["NOTIFY_SOCKET"] = path
        self.assertTrue(A.sd_notify("READY=1"))
        self.assertEqual(s.recv(100), b"READY=1")
        s.close()
        os.environ.pop("NOTIFY_SOCKET")
        self.assertFalse(A.sd_notify("WATCHDOG=1"))

    def test_store_downtime_extends_every_live_lease(self):
        t = [1000.0]
        st = A.Store(path=os.path.join(self.tmp, "g.db"), clock=lambda: t[0])
        st.meta_set("last_alive", 1000.0)
        st.acquire("red/a", "h", 120, 600)
        t[0] = 1100.0  # the store was down 100 s; the lease would have 20 s left
        self.assertEqual(A.store_grace(st), 100)
        self.assertAlmostEqual(st.get("red/a")["expires_at"], 1220.0)
        self.assertEqual(A.store_grace(st), 0)

    def test_the_oldest_change_age_leaves_out_the_time_main_was_red(self):
        t = [10_000.0]
        st = A.Store(path=os.path.join(self.tmp, "m.db"), clock=lambda: t[0])
        A.track_red_time(st, False)
        st.put_item("change", "e1", "queued", {"submitted_at": t[0], "red_accum0": 0.0})
        for _ in range(6):  # main red for 6 x 100 s
            t[0] += 100
            A.track_red_time(st, True)
        t[0] += 300  # then green for 300 s
        A.track_red_time(st, False)
        line = [x for x in A.metrics_text(st).splitlines() if x.startswith("agentctl_oldest_change_age_seconds ")]
        self.assertEqual(line, ["agentctl_oldest_change_age_seconds 300"])

    def test_roles_split_the_loop(self):
        self.assertEqual(A.host_role(), "server")
        os.environ.pop("AGENTCTL_ROLE")
        self.assertEqual(A.host_role(), "sync")  # no third role: there is no local store
        os.environ["AGENTCTL_HOST"] = "ci-runner"
        try:
            self.assertEqual(A.host_role(), "server")
            self.assertIsNone(A.store_url())
        finally:
            os.environ.pop("AGENTCTL_HOST")
        names = [n for n, _ in A.sync_steps(self.repo, self.tmp, "o/r", *on_clock(A.Clock()))]
        self.assertEqual(names, ["laptop role", "outbox", "state mirror", "children", "events", "notifier", "alerts", "workspace trust", "landed worktrees", "local queue", "landed changes", "regen holds", "claims", "dispatcher", "pins", "drift explorer", "revert notes", "fixer landing",
                                 "network watch", "tunnel probe", "red status snapshot", "upkeep", "root checkout", "recovery"])
        server = [n for n, _ in A.server_steps(self.repo, self.tmp, lambda f: f, *on_clock(A.Clock()))]
        self.assertEqual(server, ["clone health", "change items", "landed changes", "stranded changes", "change tick", "land hold", "batch tick", "watches", "land refs",
                                  "lease archive", "children", "gate summary", "red time", "red tick", "recovery", "issues",
                                  "reverts", "quiet deadlines", "stats"])
        laptop_only = {"events", "notifier", "alerts", "network watch", "tunnel probe", "red status snapshot", "upkeep", "fixer landing", "dispatcher", "regen holds",
                       "claims"}
        self.assertEqual(laptop_only & set(server), set())

    def test_the_sync_mirrors_ci_runners_state(self):
        A.state_update(tick_at=1234.0, changes=[{"id": "e1"}])
        snap = self.remote.state()
        os.environ["AGENTCTL_STATE_ROOT"] = os.path.join(self.tmp, "laptop")  # (one process plays both hosts here)
        A.mirror_state(type("Fetched", (), {"state": lambda self: snap})())
        mine = A.state_read()
        self.assertEqual((mine["tick_at"], mine["changes"]), (snap["tick_at"], [{"id": "e1"}]))
        self.assertIn("fetched_at", mine)


class RemoteStatusLineTest(Phase2Base):
    def line_with(self, answer, now_):
        class Fake:
            def __init__(self, url, tok=None, clock=None, timeout=60, retries=None):
                pass

            def state(self):
                if isinstance(answer, Exception):
                    raise answer
                return answer
        saved = A.RemoteStore
        A.RemoteStore = Fake
        try:
            return A.status_line(now_)
        finally:
            A.RemoteStore = saved

    def test_the_line_reads_ci_runners_tick_and_gate_and_says_store_unreachable_when_it_does_not_answer(self):
        t = time.time()
        self.sync()
        # the laptop's own state.json: its last local tick and gate line, from before the cutover
        A.state_update(tick_at=t - 600, summary={"at": int(t - 600), "repo": "o/r", "line": "gate RED (main #1)"})
        remote = {"tick_at": t - 5, "summary": {"at": int(t - 10), "repo": "o/r", "line": "gate green"}}
        line = self.line_with(remote, t)
        self.assertNotIn("STALLED", line)
        self.assertTrue(line.startswith("gate green"), line)
        # ci-runner's store stops answering: the mirror ages, and the line says so -- never RECONCILER STALLED
        down = A.StoreUnavailable("GET /v1/state: <urlopen error [Errno 61] Connection refused>")
        line = self.line_with(down, t + 120)
        self.assertTrue(line.startswith("STORE UNREACHABLE since "), line)
        self.assertIn("Connection refused", line)
        self.assertNotIn("STALLED", line)
        self.assertNotIn("UNREAD", line)
        # it answers again, but ci-runner's reconciler has not ticked for 5 min: that IS a stall, named as ci-runner's
        line = self.line_with({"tick_at": t - 300, "summary": remote["summary"]}, t + 200)
        self.assertTrue(line.startswith("RECONCILER STALLED since "), line)
        self.assertIn("(ci-runner)", line)
        self.assertIsNone(A.state_read().get("store_error"))


class DispatcherRemoteTest(Phase2Base):
    def test_the_laptops_dispatcher_takes_ci_runners_offer_over_the_remote_store(self):
        it = self.st.put_item("red", "k1", "offered", {"key": "k1", "workflow": "Main"})
        pic = {"key": "k1", "workflow": "Main", "job": "test", "run": {"number": 20, "databaseId": 40}, "kind": "test"}
        oid = self.st.offer("fixer", f"red:{it['id']}", A.red_lease("k1"), "fresh", 1, time.time() + 600, pic)
        spawned, saved = [], (A.red_spawn, A.cached_dispatch_blocker, A.fixer_records)
        A.red_spawn = lambda store, o_r, repo, off, took, extra=(): spawned.append((off["id"], took["epoch"])) or (True, "ok")
        A.cached_dispatch_blocker = lambda: None
        A.fixer_records = lambda: []
        try:
            acts = A.red_dispatcher(self.remote, "pawelkrupinski/movies", self.repo, time.time())
        finally:
            A.red_spawn, A.cached_dispatch_blocker, A.fixer_records = saved
        self.assertEqual(acts, ["ok"])
        self.assertEqual([x[0] for x in spawned], [oid])
        row = self.st.get(A.red_lease("k1"))  # held on ci-runner's store, by the laptop's fixer
        self.assertTrue(row["holder"].startswith("laptop:fixer:red-k1-"))
        self.assertEqual(self.st.offers("fixer"), [])


def git_tree(tmp, name):
    """A clone with refs/remotes/origin/main and a worktree `name` off it: (clone, worktree)."""
    clone = os.path.join(tmp, "clone")
    if not os.path.isdir(clone):
        subprocess.run(["git", "init", "-q", "-b", "main", clone], check=True)
        sh = lambda *a: subprocess.run(["git", "-C", clone, "-c", "user.name=t", "-c", "user.email=t@t", *a], check=True,
                                       capture_output=True)
        sh("commit", "-q", "--allow-empty", "-m", "base")
        sh("update-ref", "refs/remotes/origin/main", "HEAD")
    wt = os.path.join(tmp, name)
    subprocess.run(["git", "-C", clone, "worktree", "add", "-q", "-b", name, wt, "origin/main"], check=True,
                   capture_output=True)
    return clone, wt


def commit(wt, msg, at=None):
    env = dict(os.environ, **({"GIT_AUTHOR_DATE": f"@{int(at)} +0000"} if at else {}))
    with open(os.path.join(wt, msg.replace(" ", "-")), "w") as f:
        f.write(msg)
    subprocess.run(["git", "-C", wt, "add", "-A"], check=True)
    subprocess.run(["git", "-C", wt, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", msg], check=True,
                   env=env)


class FixerContinuityTest(Phase2Base):
    def setUp(self):
        super().setUp()
        os.environ.pop("AGENTCTL_REDS_DIR", None)
        os.environ["AGENTCTL_STATE_ROOT"] = os.path.join(self.tmp, "laptop")

    def record(self, run, key, wt, alive, n=1, **kw):
        rec = {"workflow": "Identity model convergence", "run": run, "key": key, "worktree": wt,
               "lease": f"red/{key}", "epoch": 1, "log": os.path.join(self.tmp, f"log-{run}-{n}"), **kw}
        if alive:
            rec.update(pid=os.getpid(), process_start=A.process_start(os.getpid()))
        else:
            rec.update(pid=999999, ended="2026-10-29T12:00:00Z")
        A.write_json(A.reds_dir(str(run), f"fixer-{n}.json"), rec)
        return rec

    def test_a_new_fixer_continues_the_previous_fixers_worktree_and_its_unlanded_commits(self):
        _clone, old = git_tree(self.tmp, "movies-wt-red-2473-5")
        commit(old, "step one")
        commit(old, "step two")
        _c, other = git_tree(self.tmp, "movies-wt-red-2400")
        commit(other, "older attempt", at=time.time() - 86400)
        _c, empty = git_tree(self.tmp, "movies-wt-red-2380")
        self.record(2473, "k3", old, alive=False, n=7)
        self.record(2400, "k3", other, alive=False, n=1)
        self.record(2380, "k3", empty, alive=False, n=1)
        u = {"key": "k3", "workflow": "Identity model convergence", "run": {"number": 2517}}
        got = A.previous_fixer_tree(u)
        self.assertEqual(got["worktree"], old)
        self.assertIn("CONTINUE, NEVER START OVER", got["note"])
        self.assertIn("2 commit(s)", got["note"])
        self.assertIn(other, got["note"])  # the other unlanded attempt: cherry-pick what belongs
        self.assertNotIn(empty, got["note"])
        self.assertIsNone(A.previous_fixer_tree({"key": "zz", "workflow": "W", "run": {"number": 1}}))


class RedFamilyTest(Phase2Base):
    """The owner, 2026-10-29: "give all convergence red work to one agent"."""

    def setUp(self):
        super().setUp()
        os.environ.pop("AGENTCTL_REDS_DIR", None)
        os.environ["AGENTCTL_STATE_ROOT"] = os.path.join(self.tmp, "laptop")
        # its own inbox: the harness's is one per process, so an earlier test's verdicts would be counted with its own
        self.addCleanup(os.environ.__setitem__, "AGENTCTL_INBOX_DIR", os.environ["AGENTCTL_INBOX_DIR"])
        os.environ["AGENTCTL_INBOX_DIR"] = os.path.join(self.tmp, "inbox")
        seed_gating_checks()  # no clone, no forge: the gate's workflows as last read
        self.spawned, self.fns = [], (A.red_spawn, A.cached_dispatch_blocker)
        A.red_spawn = lambda store, o_r, repo, off, took, extra=(): self.spawned.append((off, extra)) or (True, "ok")
        A.cached_dispatch_blocker = lambda: None

    def tearDown(self):
        A.red_spawn, A.cached_dispatch_blocker = self.fns
        super().tearDown()

    def red(self, key, workflow, job, run):
        it = self.st.put_item("red", key, "offered", {"key": key, "workflow": workflow, "job": job, "jobs": [job],
                                                      "last_run": {"number": run, "databaseId": run + 1000}})
        pic = {"key": key, "workflow": workflow, "job": job, "jobs": [job], "run": {"number": run, "databaseId": run + 1000},
               "kind": "test", "line": f"{job} failed"}
        return self.st.offer("fixer", f"red:{it['id']}", A.red_lease(key), "fresh", 1, time.time() + 600, pic)

    def test_the_family_comes_from_config_by_workflow_or_by_a_failing_job(self):
        self.assertEqual(A.red_family({"workflow": "Identity model convergence", "jobs": ["leg (poland)"]}), "convergence")
        self.assertEqual(A.red_family({"workflow": "Verify scrape recording", "jobs": ["convergence (germany)"]}),
                         "convergence")
        self.assertIsNone(A.red_family({"workflow": "Verify scrape recording", "jobs": ["verify (germany)"]}))
        other = os.path.join(self.tmp, "fams.json")
        with open(other, "w") as f:
            json.dump({"families": [{"name": "scrapes", "workflows": ["Verify scrape recording"]}]}, f)
        self.addCleanup(os.environ.__setitem__, "AGENTCTL_RED_FAMILIES", os.environ["AGENTCTL_RED_FAMILIES"])
        os.environ["AGENTCTL_RED_FAMILIES"] = other
        self.assertEqual(A.red_family({"workflow": "Verify scrape recording", "jobs": ["verify (germany)"]}), "scrapes")
        self.assertIsNone(A.red_family({"workflow": "Identity model convergence", "jobs": ["leg (poland)"]}))

    def test_the_shipped_families_put_a_failing_main_test_step_in_main_tests_never_infra(self):
        """2026-10-30: Main #2647's e2e (rest) failed in step `sbt e2eRest` with no FAILED line (kind unclear) and went
        to the infra family. A test step failure is main-tests'; infra is runner and setup failures."""
        self.addCleanup(os.environ.__setitem__, "AGENTCTL_RED_FAMILIES", os.environ["AGENTCTL_RED_FAMILIES"])
        os.environ["AGENTCTL_RED_FAMILIES"] = os.path.join(os.path.dirname(HERE), "red-families.json")
        fam = lambda wf, job, kind: A.red_family({"workflow": wf, "job": job, "jobs": [job], "kind": kind})
        self.assertEqual(fam("Main", "e2e (rest)", "unclear"), "main-tests")
        self.assertEqual(fam("Main", "unit-test", "test"), "main-tests")
        self.assertEqual(fam("Main", "ci", "infra"), "infra")  # a setup / runner failure
        self.assertEqual(fam("Report run timings", "judge", "unclear"), "outside-gate")  # no gate workflow (2026-11-07)
        self.assertEqual(fam("Local checks", "pre-push", "infra"), "infra")  # the landing gate's own
        self.assertEqual(fam("Identity model convergence", "leg (pl)", "unclear"), "convergence")
        # the real #2647 shape, kind from its failing step (classify_job), family from the one mapping
        e2e = {"name": "e2e (rest)", "conclusion": "failure",
               "steps": [{"name": "Set up job", "conclusion": "success"}, {"name": "sbt e2eRest", "conclusion": "failure"}]}
        kind, _why = A.classify_job(e2e, [], (9947, ["[info] Resolving ...", "##[error]Process completed with exit code 1."]))
        self.assertEqual(fam("Main", "e2e (rest)", kind), "main-tests")
        setup = {"name": "e2e (rest)", "conclusion": "failure", "steps": [{"name": "Set up job", "conclusion": "failure"}]}
        kind, _why = A.classify_job(setup, [], (0, []))
        self.assertEqual((kind, fam("Main", "e2e (rest)", kind)), ("infra", "infra"))

    def test_the_shipped_families_put_every_red_outside_the_gate_in_one_family(self):
        """The owner, 2026-11-07: "move all these to single agent" -- Identity rule ablation #1, Identity family explorer
        #5 and Order independence #46 each had a fixer of their own. The gate set is red_gate_flows (the gating checks registry), never a
        list in the config: a gate red keeps its family, a non-gate red of any kind is outside-gate's."""
        self.addCleanup(os.environ.__setitem__, "AGENTCTL_RED_FAMILIES", os.environ["AGENTCTL_RED_FAMILIES"])
        os.environ["AGENTCTL_RED_FAMILIES"] = os.path.join(os.path.dirname(HERE), "red-families.json")
        fam = lambda wf, job, kind: A.red_family({"workflow": wf, "job": job, "jobs": [job], "kind": kind})
        self.assertEqual(fam("Order independence", "scala (shuffled)", "test"), "outside-gate")
        self.assertEqual(fam("Identity rule ablation", "ablate (0)", "unclear"), "outside-gate")
        self.assertEqual(fam("Identity family explorer", "family walk (de 1/11)", "infra"), "outside-gate")  # exit 137
        self.assertEqual(fam("Main", "test (worker)", "test"), "main-tests")      # the gate's reds keep their families
        self.assertEqual(fam("Main", "ci", "infra"), "infra")
        self.assertEqual(fam("Identity model convergence", "leg (pl)", "infra"), "convergence")
        self.assertEqual(fam("Verify scrape recording", "verify (germany) / convergence (germany)", "test"), "outside-gate")
        self.assertEqual(fam("Identity model convergence", "convergence (germany)", "test"), "convergence")  # the gate's
        # 2026-11-08: the takes rows moved to Identity takes, a gate workflow; its reds stay the convergence family's
        self.assertEqual(fam("Identity takes", "leg (poland) / takes-drift-1 (poland)", "test"), "convergence")
        self.assertEqual(fam("Identity takes", "takes (germany)", "infra"), "convergence")
        self.assertEqual(fam("Verify scrape recording", "verify (germany) / order-independence-p01 (germany)", "test"),
                         "outside-gate")
        self.assertIsNone(A.red_family({"jobs": ["x"], "kind": "test"}))  # no workflow named: cannot tell
        self.assertTrue(A.family_owned("outside-gate"))
        # the gate moves, the family follows: read from the gating checks registry (gate_flows), not a list of its own
        real = A.gate_flows
        self.addCleanup(setattr, A, "gate_flows", real)
        A.gate_flows = lambda o_r: [("Main", "main.yml"), ("Order independence", "order-independence.yml")]
        self.assertIsNone(fam("Order independence", "scala (shuffled)", "test"))

    def outside_gate_owned(self):
        """An owned outside-gate family; owner_spawn recorded (returns the list of starts)."""
        fams = os.path.join(self.tmp, "fams-og.json")
        with open(fams, "w") as f:
            json.dump({"families": [{"name": "outside-gate", "owner": True, "outside_gate": True}]}, f)
        self.addCleanup(os.environ.__setitem__, "AGENTCTL_RED_FAMILIES", os.environ["AGENTCTL_RED_FAMILIES"])
        os.environ["AGENTCTL_RED_FAMILIES"] = fams
        owners = []
        saved = A.owner_spawn
        A.owner_spawn = lambda *a, **k: owners.append(a) or (True, "owner started")
        self.addCleanup(setattr, A, "owner_spawn", saved)
        return owners

    def claimed_ablation(self, **meta):
        """Red ab1 (outside the gate) held by a `red claim` in a worktree of its own: (holder, worktree)."""
        wt = os.path.join(self.tmp, "movies-wt-nongate")
        os.makedirs(wt, exist_ok=True)
        self.st.put_item("red", "ab1", "leased", {"key": "ab1", "workflow": "Identity rule ablation", "job": "collect",
                                                  "jobs": ["collect"], "last_run": {"number": 1}})
        claimer = f"laptop:claim:session 05c1b7f5@{wt}"
        self.st.acquire("red/ab1", claimer, 1800, 8 * 3600, meta={"kind": "claim", "worktree": wt, "who": "session 05c1b7f5",
                                                                  "note": "ablation collect: OOM at 7 GB", **meta})
        return claimer, wt

    def test_an_interactive_claim_takes_only_its_red_never_its_family(self):
        """The owner, 2026-11-08: a manual `red claim` from an interactive session takes ONLY that red -- it never makes
        the session its family's holder, so a new red of the family is not joined to its lease; family holding is for
        the sessions agentctl itself starts (a fixer, the family's owner)."""
        owners = self.outside_gate_owned()
        claimer, wt = self.claimed_ablation()
        self.red("oi1", "Order independence", "scala (reverse)", 46)
        acts = A.red_dispatcher(self.st, "o/r", self.repo, time.time())
        self.assertFalse(any("joined" in a for a in acts), acts)
        self.assertEqual(len(owners), 1, "the family's owner takes the new red")
        self.assertNotEqual(self.st.get("red/oi1")["holder"], claimer)
        self.assertEqual(self.st.get("red/ab1")["holder"], claimer, "its own red stays its")
        self.assertFalse(os.path.exists(os.path.join(wt, "RED-INBOX.md")))

    def test_a_dispatched_fixers_claim_holding_an_owned_familys_red_is_its_holder_no_second_owner_starts(self):
        """2026-11-07: a consolidated fixer had `red claim`ed the non-gate reds; the family's owner was not running, so a
        new red of the family would have started a SECOND session (an owner) beside it. A claim made INSIDE a dispatched
        fixer (its record on the lease) is the holder: the new red is claimed for it, in its worktree, and it is told."""
        owners = self.outside_gate_owned()
        t = time.time()
        claimer, wt = self.claimed_ablation(fixer=os.path.join(self.tmp, "fixer-5.json"))
        self.red("oi1", "Order independence", "scala (reverse)", 46)
        acts = A.red_dispatcher(self.st, "o/r", self.repo, t)
        self.assertEqual((owners, self.spawned), ([], []), "no second session beside the live holder")
        self.assertTrue(any("joined its family outside-gate's fixer" in a for a in acts), acts)
        row = self.st.get("red/oi1")
        self.assertEqual((row["holder"], A.lease_meta(row)["kind"], A.lease_meta(row)["worktree"]), (claimer, "claim", wt))
        # the holder's note is about ITS red: the joined red has none of its own yet (the status page shows a lease's note)
        self.assertIsNone(A.lease_meta(row).get("note"))
        with open(os.path.join(wt, "RED-INBOX.md")) as f:
            self.assertIn("RED FAMILY outside-gate: Order independence #46", f.read())
        A.red_renew_holders(self.st, t)  # renewed by the worktree's activity, like the claim it joined
        self.assertTrue(A.lease_is_live(self.st.get("red/oi1"), t))
        # with nobody holding it, the family's owner starts (one session, every red of it)
        self.st.release("red/ab1", self.st.get("red/ab1")["epoch"], self.st.get("red/ab1")["secret"], outcome="RELEASED")
        self.st.release("red/oi1", row["epoch"], row["secret"], outcome="RELEASED")
        self.red("oi2", "Order independence", "scala (shuffled)", 47)
        A.red_dispatcher(self.st, "o/r", self.repo, t)
        self.assertEqual(len(owners), 1)

    def test_a_new_convergence_red_goes_under_the_live_family_fixers_lease_and_it_is_told(self):
        t = time.time()
        wt = os.path.join(self.tmp, "movies-wt-red-2527")
        os.makedirs(wt)
        self.st.put_item("red", "a07", "leased", {"key": "a07", "workflow": "Identity model convergence", "job": "leg (uk)",
                                                  "jobs": ["leg (uk)"], "last_run": {"number": 2527}})
        self.st.acquire("red/a07", "laptop:fixer:red-a07-5", 900, 3600, meta={"kind": "fixer", "worktree": wt})
        self.red("v1", "Verify scrape recording", "convergence (poland)", 2530)  # convergence work in another workflow
        self.red("v2", "Verify scrape recording", "verify (poland)", 2531)      # not convergence
        acts = A.red_dispatcher(self.st, "o/r", self.repo, t)
        self.assertTrue(any("joined its family convergence's fixer laptop:fixer:red-a07-5" in a for a in acts), acts)
        row = self.st.get("red/v1")
        self.assertEqual((row["holder"], A.lease_meta(row)["kind"], A.lease_meta(row)["family"]),
                         ("laptop:fixer:red-a07-5", "sibling", "convergence"))
        with open(os.path.join(wt, "RED-INBOX.md")) as f:
            self.assertIn("RED FAMILY convergence: Verify scrape recording #2530 [convergence (poland)]", f.read())
        self.assertEqual([o["picture"]["key"] for o, _x in self.spawned], ["v2"])  # the rest gets its own fixer
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            A.red_status(self.st)
        self.assertIn("family convergence: 2 open red(s), ONE fixer: laptop:fixer:red-a07-5", out.getvalue())
        self.assertIn("[convergence]", out.getvalue())
        self.assertEqual(A.status_reds(self.st, t)[0]["family"], "convergence")

    def joined_to_a_family_fixer(self, idle_min):
        """Red v1 joined to convergence's live fixer (a07, in a git worktree), joined idle_min minutes ago: (t, wt)."""
        t = time.time()
        wt = os.path.join(self.tmp, "movies-wt-red-2527")
        subprocess.run(["git", "init", "-q", wt], check=True)
        subprocess.run(["git", "-C", wt, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty",
                        "-m", "its own red's step\n\nFixes-Red: 2527"], check=True)
        self.st.put_item("red", "a07", "leased", {"key": "a07", "workflow": "Identity model convergence", "job": "leg (uk)",
                                                  "jobs": ["leg (uk)"], "last_run": {"number": 2527}})
        self.st.acquire("red/a07", "laptop:fixer:red-a07-5", 900, 3600, meta={"kind": "fixer", "worktree": wt})
        self.red("v1", "Verify scrape recording", "convergence (poland)", 2530)
        self.st.update_item("red", "v1", self.st.item("red", "v1")["version"], {"runs": ["2530"]})
        A.red_dispatcher(self.st, "o/r", self.repo, t)
        row = self.st.get("red/v1")
        self.assertEqual(row["holder"], "laptop:fixer:red-a07-5")
        self.st.set_meta("red/v1", row["epoch"], row["secret"], {"joined_at": t - idle_min * 60})
        return t, wt

    def test_a_joined_red_with_no_progress_in_30_min_is_released_and_offered_to_another_fixer(self):
        """The owner, 2026-11-08: a red joined to a family holder's lease that shows no progress on it within ~30 min (no
        commit, land or note for THAT red) is released and offered again -- the holder may be deep in its own red."""
        t, wt = self.joined_to_a_family_fixer(31)
        A.red_renew_holders(self.st, t)
        row = self.st.get("red/v1")
        self.assertFalse(A.lease_is_live(row, t), row)
        self.assertRegex(A.lease_meta(row)["outcome"], r"^RELEASED: no progress on it .*laptop:fixer:red-a07-5")
        self.assertTrue(A.lease_is_live(self.st.get("red/a07"), t), "the holder keeps its own red")
        it = self.st.item("red", "v1")
        self.assertEqual(it["data"]["family_released"], ["laptop:fixer:red-a07-5"])
        A.red_lease_ended(self.st, it, row, None, t)  # the tick: offered again, no attempt counted
        self.assertEqual(self.st.item("red", "v1")["data"]["attempts"], 0)
        self.spawned.clear()
        acts = A.red_dispatcher(self.st, "o/r", self.repo, t)
        self.assertFalse(any("joined" in a for a in acts), "never joined to the same holder again")
        self.assertEqual([o["picture"]["key"] for o, _x in self.spawned], ["v1"], "a fixer of its own")

    def test_a_joined_red_within_its_30_min_or_with_progress_on_it_stays_joined(self):
        t, _wt = self.joined_to_a_family_fixer(10)
        A.red_renew_holders(self.st, t)
        self.assertTrue(A.lease_is_live(self.st.get("red/v1"), t), "within its window")

    def test_a_commit_for_the_joined_red_in_the_holders_worktree_is_progress(self):
        t, wt = self.joined_to_a_family_fixer(31)
        subprocess.run(["git", "-C", wt, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty",
                        "-m", "v1's step\n\nFixes-Red: 2530"], check=True)
        A.red_renew_holders(self.st, t)
        row = self.st.get("red/v1")
        self.assertTrue(A.lease_is_live(row, t), A.lease_meta(row))
        self.assertIn("commit", A.lease_meta(row)["progressed"])
        self.st.set_meta("red/v1", row["epoch"], row["secret"], {"joined_at": t - 3 * 3600})
        A.red_renew_holders(self.st, t)  # progress once shown, the backstop is done with it
        self.assertTrue(A.lease_is_live(self.st.get("red/v1"), t))

    def test_a_note_on_the_joined_red_is_progress(self):
        t, _wt = self.joined_to_a_family_fixer(31)
        row = self.st.get("red/v1")
        A.red_joined_progress(self.st, row, "note: the poland leg's seed")  # `red claim 2530 --note` inside the fixer
        A.red_renew_holders(self.st, t)
        self.assertTrue(A.lease_is_live(self.st.get("red/v1"), t))

    def test_a_fix_on_its_way_for_the_joined_red_is_progress(self):
        t, _wt = self.joined_to_a_family_fixer(31)
        self.st.update_item("red", "v1", self.st.item("red", "v1")["version"], {"fix": {"entry": "q12", "status": "queued"}})
        A.red_renew_holders(self.st, t)
        self.assertTrue(A.lease_is_live(self.st.get("red/v1"), t))

    def owner_held(self, held_min, history=(), key="ab21"):
        """Alert backtest #21 (red ab21) in the outside-gate owner's hand for held_min minutes, the owner's worktree a git
        repo: (t, wt)."""
        wt = os.path.join(self.tmp, f"movies-wt-owner-{key}")
        subprocess.run(["git", "init", "-q", wt], check=True)
        self.st.put_item("red", key, "leased", {"key": key, "workflow": "Alert backtest", "job": "backtest",
                                                   "jobs": ["backtest"], "runs": ["21"], "last_run": {"number": 21},
                                                   "history": list(history)})
        self.st.acquire(f"red/{key}", "laptop:owner:outside-gate", 4 * 3600, 48 * 3600, item=f"red:{key}",
                        meta={"kind": "owned", "family": "outside-gate", "worktree": wt})
        return time.time() + held_min * 60, wt

    def commit(self, wt, msg, at):
        env = {**os.environ, "GIT_COMMITTER_DATE": f"@{int(at)} +0000", "GIT_AUTHOR_DATE": f"@{int(at)} +0000"}
        subprocess.run(["git", "-C", wt, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty",
                        "-m", msg], check=True, env=env)

    def test_20261122_alert_backtest_21_a_red_in_its_owners_hand_with_no_progress_of_its_own_is_released(self):
        """5254bffda9d2.4: held by the outside-gate owner from 16:05Z, its lease renewed by every beat of turns working
        its other red -- no DIAGNOSED line or commit for it in ~3 h. Past RED_HELD_IDLE (45 min) with no progress of
        its own, it is released STALLED (an attempt), the reason in the inbox."""
        t, wt = self.owner_held(46, history=[{"at": "2026-11-22T13:30:48Z", "outcome": "DIAGNOSED: an older spell's"}])
        self.commit(wt, "the other red's fix\n\nFixes-Red: 488", t - 60)  # progress on its OTHER red is none of its own
        A.red_renew_holders(self.st, t)
        row = self.st.get("red/ab21")
        self.assertFalse(A.lease_is_live(row, t), A.lease_meta(row))
        self.assertRegex(A.lease_meta(row)["outcome"],
                         r"^STALLED: no progress on Alert backtest #21 .* in laptop:owner:outside-gate's hand")
        said = [e["verdict"] for e in A.inbox_entries() if str(e.get("verdict")).startswith("RED NO PROGRESS")]
        self.assertEqual(len(said), 1, said)
        self.assertIn("no DIAGNOSED line, commit naming it or note on it within 45 min", said[0])
        A.red_lease_ended(self.st, self.st.item("red", "ab21"), row, None, t)
        it = self.st.item("red", "ab21")
        self.assertEqual((it["state"], it["data"]["attempts"]), ("offered", 1), "an attempt: on up the ladder")

    def test_a_red_in_its_owners_hand_within_45_min_or_with_its_own_progress_stays_held(self):
        t, _wt = self.owner_held(30)
        A.red_renew_holders(self.st, t)
        self.assertTrue(A.lease_is_live(self.st.get("red/ab21"), t), "within its window")
        by = time.strftime("%H:%M", time.localtime(self.st.get("red/ab21")["acquired_at"] + 45 * 60))
        self.assertIn(f"lapses {by} without progress", A.red_view(self.st, self.st.item("red", "ab21"), t)[0],
                      "its own progress deadline, never the beat-renewed expiry")
        recent = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 170 * 60))
        t, _wt = self.owner_held(180, history=[{"at": recent, "outcome": "DIAGNOSED: the young-alert verdict"}], key="ab22")
        A.red_renew_holders(self.st, t)
        self.assertTrue(A.lease_is_live(self.st.get("red/ab22"), t), "DIAGNOSED 10 min ago")

    def test_a_commit_naming_the_owner_held_red_or_a_note_on_it_is_its_progress(self):
        t, wt = self.owner_held(180)
        self.commit(wt, "the backtest's evidence\n\nDiagnoses-Red: 21", t - 600)
        A.red_renew_holders(self.st, t)
        self.assertTrue(A.lease_is_live(self.st.get("red/ab21"), t), "a Diagnoses-Red commit 10 min ago")
        A.red_renew_holders(self.st, t + 3600)
        self.assertFalse(A.lease_is_live(self.st.get("red/ab21"), t + 3600), "an hour later, nothing since")
        _t, _wt = self.owner_held(180, key="ab22")
        row = self.st.get("red/ab22")
        A.red_joined_progress(self.st, row, "note: bisecting the young alerts")  # `red claim 21 --note` in the owner
        A.red_renew_holders(self.st, time.time() + 600)
        self.assertTrue(A.lease_is_live(self.st.get("red/ab22"), time.time() + 600), "noted now: within its window")
        A.red_renew_holders(self.st, time.time() + 46 * 60)
        self.assertFalse(A.lease_is_live(self.st.get("red/ab22"), time.time() + 46 * 60), "46 min after the note")

    def test_with_no_family_fixer_at_work_one_fixer_takes_every_open_red_of_the_family(self):
        t = time.time()
        self.red("c1", "Identity model convergence", "leg (uk)", 2527)
        self.red("c2", "Verify scrape recording", "convergence (germany)", 2530)
        A.red_dispatcher(self.st, "o/r", self.repo, t)
        self.assertEqual(len(self.spawned), 1)
        off, extra = self.spawned[0]
        self.assertEqual(sorted([off["picture"]["key"]] + [x["picture"]["key"] for x in extra]), ["c1", "c2"])

    def family_fixer(self, alive=True, key="c2527"):
        log = os.path.join(self.tmp, "fixer-2527.log")
        with open(log, "w") as f:
            f.write("working\n")
        wt = os.path.join(self.tmp, "movies-wt-red-2527")
        os.makedirs(wt, exist_ok=True)
        rec = {"workflow": "Identity model convergence", "run": 2527, "key": key, "job": "leg (uk)", "jobs": ["leg (uk)"],
               "worktree": wt, "log": log, "bg_id": "abc46699"}
        rec.update({"pid": os.getpid(), "process_start": A.process_start(os.getpid())} if alive else {"pid": 999999})
        A.write_json(A.reds_dir("2527", "fixer-5.json"), rec)

    def own_red(self):
        return self.st.put_item("red", "c2527", "offered", {"key": "c2527", "workflow": "Identity model convergence",
                                                            "job": "leg (uk)", "last_run": {"number": 2527}})

    def test_a_live_family_fixer_without_a_live_lease_holds_nothing(self):
        """A holder is ONLY a live lease: a family fixer process alive on its own open red, its lease lapsed, holds
        nothing (its wrapper stops itself at its next poll); the family's reds get a fixer of their own."""
        self.family_fixer()
        self.own_red()
        self.red("c3", "Verify scrape recording", "convergence (spain)", 2531)
        A.red_dispatcher(self.st, "o/r", self.repo, time.time())
        self.assertEqual(len(self.spawned), 1)

    def test_a_family_fixer_with_a_live_lease_takes_a_new_red_of_its_family(self):
        self.family_fixer()
        self.own_red()
        self.st.acquire("red/c2527", "laptop:fixer:red-c2527-5", 900, 3600, meta={"kind": "fixer"})
        self.red("c3", "Verify scrape recording", "convergence (spain)", 2531)
        t = time.time()
        acts = A.red_dispatcher(self.st, "o/r", self.repo, t)
        self.assertEqual(self.spawned, [])
        self.assertTrue(any("joined its family convergence's fixer" in a for a in acts), acts)
        self.assertEqual(A.lease_meta(self.st.get("red/c3"))["kind"], "sibling")

    def test_a_fixer_of_a_closed_red_never_holds_a_new_red_of_its_family(self):
        """2026-10-30: Main #2647 sat offered, "its previous fixer pid 20496 is still at work on it: it stays the
        holder" -- 20496 was dead, the fixer of the CLOSED red #38, matched by family; `red release --any` found no
        lease. Dead or alive, a fixer whose own red is closed holds nothing: the new red is dispatched."""
        self.closed_reds_fixer(alive=False)

    def test_a_live_fixer_of_a_closed_red_never_holds_a_new_red_of_its_family(self):
        self.closed_reds_fixer(alive=True)

    def closed_reds_fixer(self, alive):
        self.family_fixer(alive=alive)  # its own red, c2527, is not open
        self.red("c3", "Verify scrape recording", "convergence (spain)", 2531)
        t = time.time()
        A.red_dispatcher(self.st, "o/r", self.repo, t)
        self.assertEqual([o["picture"]["key"] for o, _x in self.spawned], ["c3"])

    def test_invariant_an_offered_red_whose_stated_holder_has_no_live_lease_is_offered_again(self):
        """The watchdog's invariant: an offer whose reason names a holder with no live lease is a violation, and it
        recovers itself: the offer's reason is cleared and the dispatcher takes it again."""
        off = self.red("c4", "Verify scrape recording", "convergence (spain)", 2532)
        self.st.offer_reason(off, "its previous fixer pid 20496 (~/x) is still at work on it: it stays the holder")
        t = time.time() + 300
        self.assertEqual([it["id"] for it in A.red_unheld(self.st, t)], ["c4"])
        acts = A.red_enforce(self.st, None, t)
        self.assertTrue(any("no live lease" in a for a in acts), acts)
        self.assertIsNone(self.st.offers("fixer")[0]["reason"])
        self.assertEqual(A.red_unheld(self.st, t), [])


class LandAsYouGoTest(Phase2Base):
    def test_commits_unlanded_45_min_get_a_nudge_then_an_escalation_and_both_clear_once_landed(self):
        os.environ.pop("AGENTCTL_REDS_DIR", None)
        os.environ["AGENTCTL_STATE_ROOT"] = os.path.join(self.tmp, "laptop")
        os.environ["AGENTCTL_INBOX_DIR"] = os.path.join(self.tmp, "inbox")
        self.addCleanup(os.environ.pop, "AGENTCTL_INBOX_DIR", None)
        clone, wt = git_tree(self.tmp, "movies-wt-red-2473-5")
        t = time.time()
        commit(wt, "a step", at=t - 30 * 60)
        A.write_json(A.reds_dir("2473", "fixer-7.json"), {"workflow": "W", "run": 2473, "worktree": wt, "job": "leg",
                                                         "pid": os.getpid(), "process_start": A.process_start(os.getpid())})
        self.assertEqual(A.fixers_land_watch(t, force=True), [])  # 30 min: nothing yet
        self.assertIn("nudged", A.fixers_land_watch(t + 20 * 60, force=True)[0])
        self.assertTrue(os.path.exists(os.path.join(wt, "RED-NUDGE.md")))
        self.assertEqual(A.fixers_land_watch(t + 30 * 60, force=True), [])  # once
        esc = A.fixers_land_watch(t + 65 * 60, force=True)
        self.assertTrue(esc[0].startswith("RED ESCALATE: "), esc)
        self.assertEqual(A.fixers_land_watch(t + 70 * 60, force=True), [])
        subprocess.run(["git", "-C", clone, "update-ref", "refs/remotes/origin/main",
                        subprocess.run(["git", "-C", wt, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()],
                       check=True)  # landed
        self.assertIn("everything landed", A.fixers_land_watch(t + 75 * 60, force=True)[0])
        self.assertFalse(os.path.exists(os.path.join(wt, "RED-NUDGE.md")))
        kinds = []
        for f in os.listdir(os.path.join(self.tmp, "inbox")):
            if f.endswith(".json"):
                with open(os.path.join(self.tmp, "inbox", f)) as fh:
                    kinds.append(A.inbox_kind(json.load(fh)["verdict"]))
        kinds.sort()
        self.assertEqual(kinds, ["RECOVERED", "RED_ESCALATE"])

    def test_the_fixer_prompt_says_land_as_you_go(self):
        p = A.fixer_prompt({"workflow": "Main", "run": {"number": 20, "databaseId": 40}, "job": "j", "kind": "test"},
                           "/wt", "/brief")
        self.assertIn("LAND AS YOU GO", p)
        self.assertIn("RED-NUDGE.md", p)

    def test_the_fixer_prompt_says_fast_reproducer_first(self):
        """The owner's standing rule (2026-10-29): a fast reproducer, failing before the fix and passing after."""
        p = A.fixer_prompt({"workflow": "Main", "run": {"number": 20, "databaseId": 40}, "job": "j", "kind": "test"},
                           "/wt", "/brief")
        self.assertIn("FAST REPRODUCER FIRST", p)
        self.assertIn("scripts/identity-drift-ci --for-red <run>", p)
        self.assertIn("worker/testOnly scripts.IdentityDriftSpec", p)
        self.assertIn("FAIL before your fix and PASS after", p)


    def test_the_fixer_brief_is_the_tracked_file_and_the_env_var_only_overrides_it(self):
        u = {"workflow": "Main", "run": {"number": 20, "databaseId": 40}, "job": "j", "kind": "test"}
        self.assertTrue(A.FIXER_BRIEF.endswith(os.path.join("docs", "fixer-brief.md")), A.FIXER_BRIEF)
        with open(A.FIXER_BRIEF) as f:
            tracked = A.fixer_brief_for(f.read().strip(), A.RED_PRIMARY)  # the primary's sections
        for rule in ("FAST REPRODUCER FIRST", "MECHANISM ONLY", "identity-drift-allow.tsv", "LAND AS YOU GO",
                     "Fixes-Red", "agentctl issue", "never edit ~/bin", "timeout -s KILL", "ONE sbt"):
            self.assertIn(rule, tracked)
        saved = os.environ.pop("AGENTCTL_BRIEF_SRC", None)
        try:
            self.assertIn(tracked, A.fixer_prompt(u, "/wt", "/brief"))
            other = os.path.join(self.tmp, "trial-brief.md")
            with open(other, "w") as f:
                f.write("TRIAL WORDING\n")
            os.environ["AGENTCTL_BRIEF_SRC"] = other
            p = A.fixer_prompt(u, "/wt", "/brief")
            self.assertIn("TRIAL WORDING", p)
            self.assertNotIn(tracked, p)
            os.environ["AGENTCTL_BRIEF_SRC"] = os.path.join(self.tmp, "gone.md")  # unreadable: the tracked file
            self.assertIn(tracked, A.fixer_prompt(u, "/wt", "/brief"))
        finally:
            os.environ.pop("AGENTCTL_BRIEF_SRC", None)
            if saved is not None:
                os.environ["AGENTCTL_BRIEF_SRC"] = saved


class LandHoldTest(Phase2Base):
    """SERVER MODE (2026-10-30: `land hold` on the laptop wrote a PAUSED file ci-runner's reconciler never read): the
    laptop's hold and release go through the store, and the reconciler's tick honours them."""

    def setUp(self):
        super().setUp()
        self.saved_spawn = A.spawn_waiting_processor, A.offer_token
        A.spawn_waiting_processor, A.offer_token = (lambda repo: None), (lambda repo: False)

    def tearDown(self):
        A.spawn_waiting_processor, A.offer_token = self.saved_spawn
        super().tearDown()

    def land(self, cmd, **kw):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = A.cmd_waiting(argparse.Namespace(wcmd=cmd, repo=self.repo, entry=None, renamed=True, **kw), *on_clock(FakeClock()))
        return rc, out.getvalue()

    def test_a_hold_made_on_the_laptop_holds_ci_runners_batch_until_released(self):
        self.sync()  # the laptop
        rc, out = self.land("pause", hold_for=45, hold_why="infra deploy")
        self.assertEqual(rc, 0, out)
        [h] = self.st.items("hold", ["held"])  # ci-runner's own database has it
        self.assertEqual(h["data"]["why"], "infra deploy")
        os.environ["AGENTCTL_ROLE"] = "server"  # ci-runner's reconciler
        os.environ.pop("AGENTCTL_STORE_URL")
        d, _c, _w = A.waiting_state(self.repo)
        self.assertIsNotNone(A.land_hold(A.open_store()))
        saved = A.waiting_entries, A.current_batch
        A.waiting_entries = lambda d_, **kw: [{"id": "e1"}]  # a change waits: only the hold stops the batch
        try:
            self.assertEqual(A.tick_batch(self.repo, d, *on_clock(A.Clock())), ["held"])
        finally:
            A.waiting_entries, A.current_batch = saved
        A.write_snapshot(d)
        self.sync()
        st = A.state_read()
        self.assertEqual(st["hold"]["why"], "infra deploy")
        line = A.status_header(time.time(), d=st, probes=False)["line"]
        self.assertIn("LAND HELD until", line)
        rc, out = self.land("status")
        self.assertIn("HELD until", out)
        rc, out = self.land("resume")
        self.assertIn(h["id"], out)
        self.assertEqual(self.st.items("hold", ["held"]), [])
        self.assertEqual(self.st.item("hold", h["id"])["state"], "released")

    def test_the_reconcilers_step_expires_a_forgotten_hold(self):
        self.sync()
        self.land("pause", hold_for=1)
        os.environ["AGENTCTL_ROLE"] = "server"
        os.environ.pop("AGENTCTL_STORE_URL")
        self.assertIsNotNone(A.land_hold(self.st))
        self.assertIsNone(A.land_hold(self.st, time.time() + 120))
        self.assertEqual(self.st.items("hold", ["held"]), [])
        self.assertTrue(any(e["kind"] == "hold.expired" for e in self.st.events()))
        ticked = []
        saved = A.tick_land_hold
        A.tick_land_hold = ticked.append
        try:
            dict(A.server_steps(self.repo, "/d", lambda f: f, A.Clock(), self.st))["land hold"]()
        finally:
            A.tick_land_hold = saved
        self.assertEqual(ticked, [self.st], "the reconciler's step ticks the hold on the store it was handed")

class CurrentStoreTest(Phase2Base):
    def test_a_long_running_holder_follows_store_use(self):
        os.environ.pop("AGENTCTL_ROLE")
        os.environ["AGENTCTL_STORE_TOKEN"] = "tok-pawel"
        cs = A.CurrentStore()
        with self.assertRaises(A.StoreUnavailable):
            cs.acquire("red/w", "laptop:fixer:w", 600, 3600)  # the default store does not answer here
        self.assertEqual(A.main(["store", "use", self.url]), 0)
        took = cs.acquire("red/w", "laptop:fixer:w", 600, 3600)  # the same object, now on ci-runner's store
        self.assertEqual(cs.renew("red/w", took["epoch"], took["secret"], "moved", 600), "ok")
        self.assertEqual(self.st.get("red/w")["progress"], "moved")


class CloseAfterWatchTest(Phase2Base):
    def test_the_sync_closes_the_laptop_worktree_a_watch_ok_on_ci_runner_names(self):
        wt = os.path.join(self.tmp, "movies-wt-done")
        os.makedirs(wt)
        closed, saved = [], A.close_worktree
        A.close_worktree = lambda p: closed.append(p) or f"closed worktree {p}"
        try:
            for sha, to, close in (("a" * 40, "ok", wt), ("b" * 40, "red", wt), ("c" * 40, "ok", "/gone/elsewhere")):
                it = self.st.put_item("watch", sha, "watching", {"close": close, "worktree": close})
                self.st.transition("watch", sha, to, it["version"], event=A.watch_event(it, f"WATCH {to.upper()}"))
            self.assertEqual(A.close_landed_worktrees(self.remote), [f"closed worktree {wt}"])
            self.assertEqual(A.close_landed_worktrees(self.remote), [])  # each event once
        finally:
            A.close_worktree = saved
        self.assertEqual(closed, [wt])


class W7Test(Phase2Base):
    def test_w7_flags_a_sync_behind_the_event_feed_and_clears_once_it_projects(self):
        self.assertEqual(A.inv_w7(A.Clock(), A.CurrentStore())[0], True)  # the server: no sync here to lag
        self.sync()
        self.assertEqual(A.inv_w7(A.Clock(), A.CurrentStore())[0], True)  # no events
        it = self.st.put_item("condition", "w7", "open", {})
        self.st.transition("condition", "w7", "closed", it["version"], event={"text": "x"})
        seq = max(e["seq"] for e in self.st.events())
        c = self.st.db()
        c.execute("UPDATE events SET at=? WHERE seq=?", (time.time() - 3600, seq))
        c.close()
        ok, why = A.inv_w7(A.Clock(), A.CurrentStore())
        self.assertEqual(ok, False)
        self.assertIn("behind ci-runner's events", why)
        self.st.meta_set("projector_seq", seq)  # the sync projected it
        self.assertEqual(A.inv_w7(A.Clock(), A.CurrentStore())[0], True)
        os.environ["AGENTCTL_STORE_URL"] = "http://127.0.0.1:9"
        self.assertIsNone(A.inv_w7(A.Clock(), A.CurrentStore())[0])  # the store down: no verdict
        self.assertIn("W7", A.INVARIANTS)


class TickEntryTest(unittest.TestCase):
    TICK_BOUND = 40  # under the harness's per-test timeout, so a hung tick FAILS saying where it hung

    def run_tick(self, env, tmp):
        """`agentctl tick --once`; when it does not end within TICK_BOUND, every thread's stack (faulthandler, on
        SIGABRT), its stderr and its logs in the failure, not a bare harness timeout."""
        p = subprocess.Popen([A.TOOL_PATH, "tick", "--once"], env=dict(env, PYTHONFAULTHANDLER="1"),
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
        try:
            out, errs = p.communicate(timeout=self.TICK_BOUND)
        except subprocess.TimeoutExpired:
            alive = p.poll() is None
            os.killpg(p.pid, signal.SIGABRT)
            try:
                out, errs = p.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(p.pid, signal.SIGKILL)
                out, errs = p.communicate()
            logs = []
            for root, _dirs, files in os.walk(tmp):
                for f in files:
                    if f.endswith(".log"):
                        with open(os.path.join(root, f), errors="replace") as fh:
                            logs.append(f"--- {os.path.relpath(os.path.join(root, f), tmp)}\n{fh.read()[-3000:]}")
            self.fail(f"tick --once still {'running' if alive else 'holding its pipes (a child)'} after "
                      f"{self.TICK_BOUND} s\nstderr:\n{errs[-6000:]}\n" + "\n".join(logs))
        return subprocess.CompletedProcess(p.args, p.returncode, out, errs)

    def test_tick_on_ci_runner_clones_notifies_ready_and_a_watchdog_per_tick(self):
        tmp = tempfile.mkdtemp(prefix="agentctl-tick-")
        origin = os.path.join(tmp, "origin.git")
        subprocess.run(["git", "init", "-q", "--bare", origin], check=True)
        seed = os.path.join(tmp, "seed")
        subprocess.run(["git", "clone", "-q", origin, seed], check=True, capture_output=True)
        subprocess.run(["git", "-C", seed, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty",
                        "-m", "init"], check=True)
        subprocess.run(["git", "-C", seed, "push", "-q", "origin", "HEAD:main"], check=True, capture_output=True)
        sock = os.path.join(tmp, "n.sock")
        s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        s.bind(sock)
        s.settimeout(0.2)
        # READ AS SYSTEMD DOES, while the tick runs: an unread datagram socket queues only net.unix.max_dgram_qlen
        # messages -- 10 in a fresh network namespace (ci-runner's job containers; systemd raises the host's to 512)
        # -- and the tick sends a WATCHDOG=1 per step, so a reader waiting for the tick to end blocked its 11th send.
        got, done = [], threading.Event()

        def drain():  # until the tick has ended and its last message is read
            while True:
                try:
                    got.append(s.recv(100))
                except socket.timeout:
                    if done.is_set():
                        return
        reader = threading.Thread(target=drain, daemon=True)
        reader.start()
        env = dict(os.environ, AGENTCTL_ROLE="server", AGENTCTL_HOST="ci-runner", NOTIFY_SOCKET=sock,
                   AGENTCTL_STATE_ROOT=os.path.join(tmp, "state"), AGENTCTL_MOVIES_REPO=os.path.join(tmp, "movies"),
                   AGENTCTL_MOVIES_REMOTE=origin, AGENTCTL_OWNERS_DIR=os.path.join(tmp, "owners"),
                   AGENTCTL_GH_CLI="/usr/bin/false", GH_TOKEN="x", AGENTCTL_WAITING_DIR=os.path.join(tmp, "waiting"),
                   AGENTCTL_INBOX_DIR=os.path.join(tmp, "inbox"), AGENTCTL_REDS_DIR=os.path.join(tmp, "reds"),
                   AGENTCTL_RECOVERY_DIR=os.path.join(tmp, "recovery"), AGENTCTL_NOTIFY="true")
        env.pop("AGENTCTL_STORE_URL", None)
        try:
            p = self.run_tick(env, tmp)
        finally:
            done.set()
            reader.join(5)
            s.close()
        self.assertEqual(p.returncode, 0, p.stderr[-2000:])
        self.assertTrue(got and got[0].startswith(b"EXTEND_TIMEOUT_USEC="), got[:1])  # the clone, before READY
        got = [m for m in got if not m.startswith(b"EXTEND_TIMEOUT_USEC=")]
        self.assertEqual(got[:2], [b"READY=1", b"WATCHDOG=1"])
        url = subprocess.run(["git", "-C", os.path.join(tmp, "movies"), "remote", "get-url", "origin"],
                             capture_output=True, text=True).stdout.strip()
        self.assertEqual(url, origin)
        self.assertNotIn("events failed", p.stderr)  # the laptop's steps never run there
        self.assertTrue(os.path.exists(os.path.join(tmp, "state", "state.json")))


if __name__ == "__main__":
    unittest.main()


class StoreTokensTest(Phase2Base):
    """`store serve` (github-return W5b): the store's own HMAC tokens, no forge asked."""

    def setUp(self):
        super().setUp()
        self.saved_secret = os.environ.get("AGENTCTL_STORE_SECRET")
        os.environ["AGENTCTL_STORE_SECRET"] = os.path.join(self.tmp, "secret", "store-secret")
        self.srv.shutdown()
        self.api = A.StoreAPI(self.st, ["pawel"], ["ci-metrics"], repo=self.repo)
        self.srv = A.store_server(self.api, "127.0.0.1", 0)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_port}"

    def tearDown(self):
        if self.saved_secret is None:
            os.environ.pop("AGENTCTL_STORE_SECRET", None)
        else:
            os.environ["AGENTCTL_STORE_SECRET"] = self.saved_secret
        super().tearDown()

    def test_issued_tokens_decide_what_a_login_may_call_and_a_revoke_counts_at_once(self):
        tid, pawel = A.store_token_issue("pawel", "laptop")
        _, metrics = A.store_token_issue("ci-metrics", "scrape")
        _, other = A.store_token_issue("someone")
        self.assertEqual(os.stat(A.store_secret_path()).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(A.store_tokens_path()).st_mode & 0o777, 0o600)
        self.assertTrue(A.TOKEN_SHAPE.fullmatch(pawel))  # the processor's socket carries it
        self.assertEqual(self.get("/v1/state")[0], 401)
        self.assertEqual(self.get("/v1/state", "tok-pawel")[0], 403)  # a token the store did not issue means nothing
        self.assertEqual(self.get("/v1/state", other)[0], 403)
        self.assertEqual(self.get("/v1/state", metrics)[0], 403)
        self.assertEqual(self.get("/metrics", metrics)[0], 200)
        self.assertEqual(self.get("/v1/state", pawel)[0], 200)
        forged = pawel.rsplit(".", 1)[0] + "." + "0" * 64
        self.assertEqual(self.get("/v1/state", forged)[0], 403)
        renamed = ".".join(["ast", tid, "someone", pawel.rsplit(".", 1)[1]])  # pawel's mac under another login
        self.assertEqual(self.get("/v1/state", renamed)[0], 403)
        A.store_token_revoke(tid)
        self.assertEqual(self.get("/v1/state", pawel)[0], 403)

    def test_the_cli_issues_lists_and_revokes(self):
        import contextlib, io
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(A.main(["store", "token", "issue", "--user", "pawel", "--name", "laptop"]), 0)
        tok = out.getvalue().strip()
        self.assertEqual(A.store_token_user(tok), "pawel")
        tid = tok.split(".")[1]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            A.main(["store", "token", "list"])
        self.assertIn(f"{tid}  pawel", out.getvalue())
        with contextlib.redirect_stdout(io.StringIO()):
            A.main(["store", "token", "revoke", tid])
        self.assertIsNone(A.store_token_user(tok))
        with self.assertRaises(SystemExit):
            A.store_token_issue("no spaces please")

    def test_the_laptop_sends_the_store_token_never_a_gh_token(self):
        _, pawel = A.store_token_issue("pawel")
        saved = {k: os.environ.get(k) for k in ("GH_TOKEN",)}
        try:
            os.environ.update(GH_TOKEN="gho_the_owners", AGENTCTL_STORE_TOKEN=pawel)
            self.assertEqual(A.token(A.SHORT["movies"]), pawel)
            self.assertEqual(A.token_env_name(A.SHORT["movies"]), "AGENTCTL_STORE_TOKEN")
            self.assertEqual(A.token_vault_key(A.SHORT["movies"]), ("bitcashier", "AGENTCTL_STORE_TOKEN"))
            self.assertEqual(A.RemoteStore(self.url)._token(), pawel)
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
            os.environ.pop("AGENTCTL_STORE_TOKEN", None)
