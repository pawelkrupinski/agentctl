"""Phase 2 (design §6.3, §13 phase 2): the store over HTTP with Forgejo token auth, RemoteStore, the laptop's sync, the
reconciler's entry point on ci-runner -- against a local server on a temp port, with a fake Forgejo for the auth."""
import contextlib, io, json, os, socket, subprocess, sys, tempfile, threading, time, unittest, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402

USERS = {"tok-pawel": "pawel", "tok-metrics": "ci-metrics", "tok-other": "mallory"}


def fake_forgejo():
    """GET /api/v1/user: the login of a known token, 401 otherwise (counts its calls)."""
    calls = []

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            calls.append(self.path)
            tok = (self.headers.get("Authorization") or "").split()[-1]
            if self.path == "/api/v1/user" and tok in USERS:
                body = json.dumps({"login": USERS[tok]}).encode()
                self.send_response(200)
            else:
                body = b'{"message":"token is required"}'
                self.send_response(401)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, calls


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
        self.fj, self.fj_calls = fake_forgejo()
        self.st = A.Store(path=os.path.join(self.tmp, "state", "store.db"))
        self.repo = os.path.join(self.tmp, "movies")
        subprocess.run(["git", "init", "-q", self.repo], check=True)
        self.api = A.StoreAPI(self.st, f"http://127.0.0.1:{self.fj.server_port}", ["pawel"], ["ci-metrics"],
                              repo=self.repo)
        self.srv = A.store_server(self.api, "127.0.0.1", 0)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_port}"
        self.remote = A.RemoteStore(self.url, "tok-pawel")

    def tearDown(self):
        self.srv.shutdown()
        self.fj.shutdown()
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
        """This process plays the laptop's sync from here on: Store() is RemoteStore(self.url) -- the server thread's
        own Store() calls included, which reach the same database through the API."""
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
    def test_a_token_is_checked_with_forgejo_and_its_login_decides_what_it_may_call(self):
        self.assertEqual(self.get("/v1/state")[0], 401)                    # no token
        self.assertEqual(self.get("/v1/state", "tok-unknown")[0], 403)     # Forgejo does not know it
        self.assertEqual(self.get("/v1/state", "tok-other")[0], 403)       # a real user, not allowed
        self.assertEqual(self.get("/v1/state", "tok-metrics")[0], 403)     # the metrics identity: /metrics only
        code, body = self.get("/metrics", "tok-metrics")
        self.assertEqual(code, 200)
        for g in ("agentctl_reconciler_tick_timestamp", "agentctl_oldest_change_age_seconds",
                  "agentctl_red_unleased_seconds", "agentctl_leases_expired_unreaped", "agentctl_items_past_deadline"):
            self.assertIn(f"\n{g} ", "\n" + body)
        self.assertEqual(self.get("/v1/state", "tok-pawel")[0], 200)
        n = len(self.fj_calls)
        self.get("/v1/state", "tok-pawel")
        self.assertEqual(len(self.fj_calls), n)  # cached: Forgejo asked once per token per 5 min


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

    def test_store_returns_a_remote_store_when_the_url_is_set_and_the_rest_routes_answer(self):
        self.sync()
        s = A.Store()
        self.assertIsInstance(s, A.RemoteStore)
        self.assertIsInstance(A.Store(path=os.path.join(self.tmp, "x.db")), A.Store)  # a path: always local
        took = s.call("POST", "/v1/leases/land/movies:acquire", {"holder": "h", "ttl_s": 60, "max_hold_s": 120})["result"]
        self.assertEqual(s.call("POST", "/v1/leases/land/movies:renew",
                                {"epoch": took["epoch"], "secret": took["secret"], "progress": "a", "ttl_s": 60})["result"],
                         "ok")
        self.assertEqual([x["name"] for x in s.call("GET", "/v1/leases?prefix=land/")["leases"]], ["land/movies"])
        self.assertEqual(s.call("POST", "/v1/leases/land/movies:release",
                                {"epoch": took["epoch"], "secret": took["secret"]})["result"], "ok")

    def test_store_use_switches_every_process_here_and_there_is_no_local_store(self):
        os.environ.pop("AGENTCTL_ROLE")
        os.environ["AGENTCTL_STORE_TOKEN"] = "tok-pawel"
        self.assertEqual(A.host_role(), "sync")  # the laptop: always the sync, never a store of its own
        self.assertEqual(A.Store().url, "http://127.0.0.1:9")  # the default (ci-runner's; the tests' closed port)
        self.assertEqual(A.main(["store", "use", self.url]), 0)
        self.assertIsInstance(A.Store(), A.RemoteStore)
        self.assertEqual(A.Store().url, self.url)
        with self.assertRaises(A.StoreUnavailable):  # a store that does not answer is never switched to
            A.main(["store", "use", "http://127.0.0.1:9"])
        self.assertEqual(A.Store().url, self.url)
        with self.assertRaises(SystemExit):
            A.main(["store", "use", "--local"])  # gone with the laptop's store
        with self.assertRaises(SystemExit):
            A.main(["store", "import", "--to", self.url])  # gone: a lost store is rebuilt on ci-runner (design §16)

    def test_events_long_poll_wakes_on_a_new_event_and_acks(self):
        it = self.st.put_item("condition", "c2", "open", {})
        got = []
        th = threading.Thread(target=lambda: got.extend(self.remote.wait_events(after=999999, wait=5)))
        t0 = time.time()
        th.start()
        th.join()
        self.assertEqual(got, [])
        self.assertGreaterEqual(time.time() - t0, 4)
        self.st.transition("condition", "c2", "closed", it["version"], event={"text": "x", "severity": "escalate"})
        seq = max(e["seq"] for e in self.st.events())
        [ev] = self.remote.wait_events(after=seq - 1, wait=5)
        self.remote.call("POST", f"/v1/events/{ev['seq']}:ack", {})
        self.assertTrue(self.st.events(after=seq - 1)[0]["notified_at"])


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
        names = [n for n, _ in A.sync_steps(self.repo, self.tmp, "o/r")]
        self.assertEqual(names, ["outbox", "state mirror", "children", "events", "notifier", "landed worktrees", "claims", "dispatcher", "pins", "fixer landing",
                                 "network watch", "tunnel probe", "upkeep"])
        server = [n for n, _ in A.server_steps(self.repo, self.tmp, lambda f: f)]
        self.assertEqual(server, ["clone health", "change items", "change tick", "batch tick", "watches", "land refs",
                                  "lease archive", "children", "gate summary", "red time", "red tick", "recovery"])
        laptop_only = {"events", "notifier", "network watch", "tunnel probe", "upkeep", "fixer landing", "dispatcher",
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


class StaleStoreFixerTest(Phase2Base):
    def test_a_live_fixer_whose_lease_lapsed_is_adopted_and_no_second_fixer_goes(self):
        # 2026-10-29: the gate fixer's wrapper (pid 31738) renewed its lease on another store; ci-runner's copy
        # expired and its reds were offered again
        os.environ.pop("AGENTCTL_REDS_DIR", None)
        os.environ["AGENTCTL_STATE_ROOT"] = os.path.join(self.tmp, "laptop")  # (one process plays both hosts here)
        t = time.time()
        self.lapsed_lease("red/k1", "laptop:fixer:red-k1-7", 1, t)
        took = {"epoch": 1}
        it = self.st.put_item("red", "k1", "offered", {"key": "k1", "workflow": "Main", "last_run": {"number": 20}})
        pic = {"key": "k1", "workflow": "Main", "job": "test", "run": {"number": 20, "databaseId": 40}, "kind": "test"}
        self.st.offer("fixer", f"red:{it['id']}", A.red_lease("k1"), "fresh", 1, t + 600, pic)
        log = os.path.join(self.tmp, "fixer.log")
        with open(log, "w") as f:
            f.write("working\n")
        A.write_json(A.reds_dir("20", "fixer-1.json"),
                     {"workflow": "Main", "run": 20, "pid": os.getpid(), "process_start": A.process_start(os.getpid()),
                      "worktree": self.tmp, "log": log, "lease": "red/k1", "epoch": took["epoch"]})
        acts = A.red_adopt_fixers(self.remote, A.red_active(self.remote), t)
        self.assertEqual(len(acts), 1, acts)
        held = self.st.get("red/k1")
        self.assertTrue(held["holder"].startswith(f"laptop:fixer:pid-{os.getpid()}@"))
        self.assertEqual(A.lease_meta(held)["kind"], "legacy-fixer")
        self.assertTrue(A.lease_is_live(held, t))
        A.red_renew_holders(self.remote, t)  # renewed by its log's growth and its worktree's activity
        spawned, saved = [], (A.red_spawn, A.cached_dispatch_blocker)
        A.red_spawn = lambda *a, **k: spawned.append(a) or (True, "ok")
        A.cached_dispatch_blocker = lambda: None
        try:
            A.red_dispatcher(self.remote, "o/r", self.repo, t)
        finally:
            A.red_spawn, A.cached_dispatch_blocker = saved
        self.assertEqual(spawned, [])  # its run's fixer is alive: no duplicate
        # a fixer whose lease IS held on the store in use is its own wrapper's: never adopted
        self.assertEqual(A.red_adopt_fixers(self.remote, A.red_active(self.remote), t), [])


def git_tree(tmp, name):
    """A clone with refs/remotes/forgejo/main and a worktree `name` off it: (clone, worktree)."""
    clone = os.path.join(tmp, "clone")
    if not os.path.isdir(clone):
        subprocess.run(["git", "init", "-q", "-b", "main", clone], check=True)
        sh = lambda *a: subprocess.run(["git", "-C", clone, "-c", "user.name=t", "-c", "user.email=t@t", *a], check=True,
                                       capture_output=True)
        sh("commit", "-q", "--allow-empty", "-m", "base")
        sh("update-ref", "refs/remotes/forgejo/main", "HEAD")
    wt = os.path.join(tmp, name)
    subprocess.run(["git", "-C", clone, "worktree", "add", "-q", "-b", name, wt, "forgejo/main"], check=True,
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

    def test_a_live_fixer_keeps_its_red_on_a_newer_run_and_no_second_fixer_is_dispatched(self):
        # 2026-10-29: fixer -7 worked on #2473's red; the red moved on to run #2517 and -8 was dispatched beside it
        t = time.time()
        self.record(2473, "k1", self.tmp, alive=True)
        it = self.st.put_item("red", "k1", "offered", {"key": "k1", "workflow": "Identity model convergence",
                                                       "last_run": {"number": 2517}})
        self.lapsed_lease("red/k1", "laptop:fixer:red-k1-8", 3, t)  # -8's: its watcher died, the lease lapsed
        pic = {"key": "k1", "workflow": "Identity model convergence", "job": "leg", "run": {"number": 2517, "databaseId": 9},
               "kind": "test"}
        self.st.offer("fixer", f"red:{it['id']}", "red/k1", "fresh", 1, t + 600, pic)
        spawned, saved = [], (A.red_spawn, A.cached_dispatch_blocker)
        A.red_spawn = lambda *a, **k: spawned.append(a) or (True, "ok")
        A.cached_dispatch_blocker = lambda: None
        try:
            A.red_dispatcher(self.remote, "o/r", self.repo, t + 5)
        finally:
            A.red_spawn, A.cached_dispatch_blocker = saved
        self.assertEqual(spawned, [])
        self.assertIn("previous fixer", self.st.offers("fixer")[0]["reason"])
        acts = A.red_adopt_fixers(self.remote, A.red_active(self.remote), t + 5)  # it is the holder again
        self.assertEqual(len(acts), 1, acts)
        self.assertTrue(self.st.get("red/k1")["holder"].startswith(f"laptop:fixer:pid-{os.getpid()}@"))

    def test_a_lease_ended_on_purpose_is_not_taken_back(self):
        t = time.time()
        took = self.st.acquire("red/k2", "laptop:fixer:red-k2-1", 600, 3600)
        self.record(2473, "k2", self.tmp, alive=True, epoch=took["epoch"])
        self.st.put_item("red", "k2", "fix-pending", {"key": "k2", "workflow": "Identity model convergence",
                                                      "last_run": {"number": 2473}})
        self.st.revoke("red/k2", "fix submitted")
        self.assertEqual(A.red_adopt_fixers(self.remote, A.red_active(self.remote), t), [])

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
        os.environ["AGENTCTL_RED_FAMILIES"] = other
        self.addCleanup(os.environ.pop, "AGENTCTL_RED_FAMILIES", None)
        self.assertEqual(A.red_family({"workflow": "Verify scrape recording", "jobs": ["verify (germany)"]}), "scrapes")
        self.assertIsNone(A.red_family({"workflow": "Identity model convergence", "jobs": ["leg (poland)"]}))

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

    def test_with_no_family_fixer_at_work_one_fixer_takes_every_open_red_of_the_family(self):
        t = time.time()
        self.red("c1", "Identity model convergence", "leg (uk)", 2527)
        self.red("c2", "Verify scrape recording", "convergence (germany)", 2530)
        A.red_dispatcher(self.st, "o/r", self.repo, t)
        self.assertEqual(len(self.spawned), 1)
        off, extra = self.spawned[0]
        self.assertEqual(sorted([off["picture"]["key"]] + [x["picture"]["key"] for x in extra]), ["c1", "c2"])

    def test_a_family_fixer_alive_without_a_lease_means_no_second_fixer(self):
        A.write_json(A.reds_dir("2527", "fixer-5.json"),
                     {"workflow": "Identity model convergence", "run": 2527, "job": "leg (uk)", "jobs": ["leg (uk)"],
                      "worktree": self.tmp, "pid": os.getpid(), "process_start": A.process_start(os.getpid())})
        self.red("c3", "Verify scrape recording", "convergence (spain)", 2531)
        A.red_dispatcher(self.st, "o/r", self.repo, time.time())
        self.assertEqual(self.spawned, [])
        self.assertIn("previous fixer", self.st.offers("fixer")[0]["reason"])


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
        subprocess.run(["git", "-C", clone, "update-ref", "refs/remotes/forgejo/main",
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
        self.assertEqual(A.inv_w7()[0], True)  # the server: no sync here to lag
        self.sync()
        self.assertEqual(A.inv_w7()[0], True)  # no events
        it = self.st.put_item("condition", "w7", "open", {})
        self.st.transition("condition", "w7", "closed", it["version"], event={"text": "x"})
        seq = max(e["seq"] for e in self.st.events())
        c = self.st.db()
        c.execute("UPDATE events SET at=? WHERE seq=?", (time.time() - 3600, seq))
        c.close()
        ok, why = A.inv_w7()
        self.assertEqual(ok, False)
        self.assertIn("behind ci-runner's events", why)
        self.st.meta_set("projector_seq", seq)  # the sync projected it
        self.assertEqual(A.inv_w7()[0], True)
        os.environ["AGENTCTL_STORE_URL"] = "http://127.0.0.1:9"
        self.assertIsNone(A.inv_w7()[0])  # the store down: no verdict
        self.assertIn("W7", A.INVARIANTS)


class TickEntryTest(unittest.TestCase):
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
        s.settimeout(120)
        env = dict(os.environ, AGENTCTL_ROLE="server", AGENTCTL_HOST="ci-runner", NOTIFY_SOCKET=sock,
                   AGENTCTL_STATE_ROOT=os.path.join(tmp, "state"), AGENTCTL_MOVIES_REPO=os.path.join(tmp, "movies"),
                   AGENTCTL_MOVIES_REMOTE=origin, AGENTCTL_OWNERS_DIR=os.path.join(tmp, "owners"),
                   AGENTCTL_FORGEJO_GH="/usr/bin/false", GH_TOKEN="x", AGENTCTL_WAITING_DIR=os.path.join(tmp, "waiting"),
                   AGENTCTL_INBOX_DIR=os.path.join(tmp, "inbox"), AGENTCTL_REDS_DIR=os.path.join(tmp, "reds"),
                   AGENTCTL_RECOVERY_DIR=os.path.join(tmp, "recovery"), AGENTCTL_NOTIFY="true")
        env.pop("AGENTCTL_STORE_URL", None)
        p = subprocess.run([A.TOOL_PATH, "tick", "--once"], env=env, capture_output=True, text=True, timeout=300)
        got = []
        try:
            while True:
                s.settimeout(1)
                got.append(s.recv(100))
        except OSError:
            pass
        s.close()
        self.assertEqual(p.returncode, 0, p.stderr[-2000:])
        self.assertTrue(got and got[0].startswith(b"EXTEND_TIMEOUT_USEC="), got[:1])  # the clone, before READY
        got = [m for m in got if not m.startswith(b"EXTEND_TIMEOUT_USEC=")]
        self.assertEqual(got[:2], [b"READY=1", b"WATCHDOG=1"])
        url = subprocess.run(["git", "-C", os.path.join(tmp, "movies"), "remote", "get-url", "forgejo"],
                             capture_output=True, text=True).stdout.strip()
        self.assertEqual(url, origin)
        self.assertNotIn("events failed", p.stderr)  # the laptop's steps never run there
        self.assertTrue(os.path.exists(os.path.join(tmp, "state", "state.json")))


if __name__ == "__main__":
    unittest.main()
