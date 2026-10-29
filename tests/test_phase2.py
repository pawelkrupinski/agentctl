"""Phase 2 (design §6.3, §13 phase 2): the store over HTTP with Forgejo token auth, RemoteStore, the laptop's sync, the
reconciler's entry point on ci-runner -- against a local server on a temp port, with a fake Forgejo for the auth."""
import json, os, socket, subprocess, sys, tempfile, threading, time, unittest, urllib.request
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
                                                    "AGENTCTL_STORE_RETRY_BASE")}
        os.environ["AGENTCTL_STORE_RETRY_BASE"] = "0.01"  # the retries' backoff, short here
        os.environ["AGENTCTL_STATE_ROOT"] = os.path.join(self.tmp, "state")
        for k in ("AGENTCTL_STORE_URL", "AGENTCTL_STORE_TOKEN", "AGENTCTL_ROLE", "NOTIFY_SOCKET"):
            os.environ.pop(k, None)
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
        os.environ["AGENTCTL_STORE_URL"] = self.url
        os.environ["AGENTCTL_STORE_TOKEN"] = "tok-pawel"
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

    def test_store_use_switches_every_process_here_and_local_switches_back(self):
        os.environ["AGENTCTL_STORE_TOKEN"] = "tok-pawel"
        self.assertEqual(A.main(["store", "use", self.url]), 0)
        self.assertIsInstance(A.Store(), A.RemoteStore)
        self.assertEqual(A.host_role(), "sync")
        self.assertEqual(A.main(["store", "use", "--local"]), 0)
        self.assertIsInstance(A.Store(), A.Store)
        self.assertEqual(A.host_role(), "local")
        with self.assertRaises(A.StoreUnavailable):  # a store that does not answer is never switched to
            A.main(["store", "use", "http://127.0.0.1:9"])
        self.assertIsInstance(A.Store(), A.Store)

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
        self.assertEqual(A.host_role(), "local")
        os.environ["AGENTCTL_STORE_URL"] = self.url
        self.assertEqual(A.host_role(), "sync")
        os.environ.pop("AGENTCTL_STORE_URL")
        os.environ["AGENTCTL_ROLE"] = "server"
        self.assertEqual(A.host_role(), "server")
        names = [n for n, _ in A.sync_steps(self.repo, self.tmp, "o/r")]
        self.assertEqual(names, ["outbox", "state mirror", "children", "events", "notifier", "landed worktrees", "claims", "dispatcher",
                                 "network watch", "tunnel probe", "upkeep"])
        self.assertTrue(set(A.LAPTOP_ONLY_STEPS) <= set(names))

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
        os.environ["AGENTCTL_STORE_URL"] = self.url
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
    def test_a_live_fixer_renewing_the_laptops_store_is_adopted_on_ci_runners_and_no_second_fixer_goes(self):
        # 2026-10-29: the gate fixer's wrapper (pid 31738), started before `store use`, renewed its lease on the
        # laptop's store.db; ci-runner's imported copy expired and its reds were offered again
        os.environ.pop("AGENTCTL_REDS_DIR", None)
        os.environ["AGENTCTL_STATE_ROOT"] = os.path.join(self.tmp, "laptop")  # (one process plays both hosts here)
        t = time.time()
        laptop = A.Store(path=A.state_root("store.db"))
        took = laptop.acquire("red/k1", "laptop:fixer:red-k1-7", 900, 3600, meta={"kind": "fixer", "run": 20})
        row = dict(laptop.get("red/k1"), expires_at=t - 60)  # ci-runner's copy: imported, then lapsed
        self.st.import_lease(row)
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
        self.st.import_lease({"name": "red/k1", "holder": "laptop:fixer:red-k1-8", "epoch": 3, "secret": "s",
                              "acquired_at": t - 900, "renewed_at": t - 700, "expires_at": t - 100, "max_until": t + 3600,
                              "progress": "", "item": None, "meta": "{}"})  # -8's: its watcher died, the lease lapsed
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




class CurrentStoreTest(Phase2Base):
    def test_a_long_running_holder_follows_store_use(self):
        os.environ["AGENTCTL_STATE_ROOT"] = os.path.join(self.tmp, "laptop")
        cs = A.CurrentStore()
        took = cs.acquire("red/w", "laptop:fixer:w", 600, 3600)  # on the laptop's store
        self.assertIsNone(self.st.get("red/w"))
        self.st.import_lease(A.Store(path=A.state_root("store.db")).get("red/w"))  # the import copies it
        os.environ.update(AGENTCTL_STORE_URL=self.url, AGENTCTL_STORE_TOKEN="tok-pawel")  # `store use`
        self.assertEqual(cs.renew("red/w", took["epoch"], took["secret"], "moved", 600), "ok")
        self.assertEqual(self.st.get("red/w")["progress"], "moved")  # renewed on ci-runner's store


class ImportTest(Phase2Base):
    def test_the_one_shot_import_moves_live_leases_open_items_and_the_queue(self):
        local = A.Store(path=os.path.join(self.tmp, "laptop.db"))
        took = local.acquire("red/k9", "laptop:fixer:red-k9-1", 900, 3600, meta={"kind": "fixer"})
        gone = local.acquire("red/old", "x", 900, 3600)
        local.release("red/old", gone["epoch"], gone["secret"])
        local.put_item("red", "k9", "leased", {"key": "k9"})
        lrepo = os.path.join(self.tmp, "laptop-movies")
        subprocess.run(["git", "init", "-q", lrepo], check=True)
        d = A.waiting_state(lrepo)[0]
        e, _ = A.queue_change(d, {"worktree": "/w/z", "branch": "z", "head": "e" * 40, "commits": ["e" * 40]})
        e["ref"] = f"refs/heads/land/{e['id']}"
        A.save_entry(d, e)
        n = A.store_import(local, self.remote, lrepo)
        self.assertEqual(n, {"leases": 1, "leases_there": 0, "items": 1, "merged": 0, "skipped": 0, "changes": 1})
        row = self.st.get("red/k9")
        self.assertEqual((row["epoch"], row["secret"]), (took["epoch"], took["secret"]))  # its holder renews it there
        self.assertEqual(self.remote.renew("red/k9", took["epoch"], took["secret"], "moved", 900), "ok")
        self.assertIsNone(self.st.get("red/old"))
        self.assertEqual(self.st.item("red", "k9")["state"], "leased")
        self.assertEqual([x["id"] for x in A.queued_entries(A.waiting_state(self.repo)[0])], [e["id"]])
        again = A.store_import(local, self.remote, lrepo)  # twice is harmless
        self.assertEqual((again["leases"], again["leases_there"], again["items"]), (0, 1, 0))
        self.assertEqual(len([x for x in self.st.items("red") if x["state"] == "leased"]), 1)

    def test_an_import_after_the_reconciler_ticked_merges_each_red_by_its_key_and_keeps_its_fix_link(self):
        # 2026-10-29: ci-runner's reconciler opened its own items for the laptop's reds before the import finished;
        # the import left them as they were -- #2493 lost run 2406 from its names, and with it its Fixes-Red: 2406 fix
        self.st.put_item("red", "k7", "offered", {"key": "k7", "runs": ["2493"], "last_run": {"databaseId": 93},
                                                 "jobs": ["verify"], "tier": "fresh", "attempts": 0})
        self.st.put_item("red", "k8", "offered", {"key": "k8", "runs": ["2473"]})      # the reconciler's spell ...
        self.st.put_item("red", "k8.2", "offered", {"key": "k8", "runs": ["2473"]})    # ... and an earlier import's
        local = A.Store(path=os.path.join(self.tmp, "laptop2.db"))
        local.put_item("red", "k7", "fix-pending", {"key": "k7", "runs": ["2406", "2493"], "jobs": ["convergence"],
                                                     "last_run": {"databaseId": 90}, "attempts": 2,
                                                     "fix": {"entry": "e-2406", "status": "waiting"},
                                                     "history": [{"holder": "laptop:fixer:a"}]})
        local.put_item("red", "k8.2", "offered", {"key": "k8", "runs": ["2470", "2473"], "attempts": 1})
        local.put_item("red", "k6", "fix-pending", {"key": "k6", "runs": ["2406"]})
        cleared = local.put_item("red", "k5", "offered", {"key": "k5", "runs": ["1"]})
        local.transition("red", "k5", "cleared", cleared["version"])
        lrepo = os.path.join(self.tmp, "laptop-movies2")
        subprocess.run(["git", "init", "-q", lrepo], check=True)
        d = A.waiting_state(lrepo)[0]
        e, _ = A.queue_change(d, {"worktree": "/w/fix", "branch": "f", "head": "f" * 40, "commits": ["f" * 40],
                                  "fixes": ["2406"]})
        e.update(status="waiting", ref=f"refs/heads/land/{e['id']}")
        A.save_entry(d, e)
        order = []
        real_submit, real_put = self.remote.submit, self.remote.put_item
        self.remote.submit = lambda x: order.append("change") or real_submit(x)
        self.remote.put_item = lambda *a, **k: order.append("item") or real_put(*a, **k)
        n = A.store_import(local, self.remote, lrepo)
        self.assertEqual(order, ["change", "item"])  # the Fixes-Red change is there before the red that rests on it
        self.assertEqual((n["items"], n["merged"], n["changes"]), (1, 2, 1))
        k7 = self.st.item("red", "k7")
        self.assertEqual(k7["state"], "offered")  # the state is the tick's to derive
        self.assertEqual(k7["data"]["runs"], ["2406", "2493"])
        self.assertEqual((k7["data"]["attempts"], k7["data"]["last_run"]["databaseId"]), (2, 93))
        self.assertEqual(k7["data"]["history"], [{"holder": "laptop:fixer:a"}])
        self.assertNotIn("fix", k7["data"])
        facts = {"entries": [dict(x, status="waiting") for x in A.waiting_entries(A.waiting_state(self.repo)[0],
                                                                                  open_only=False)]}
        self.assertEqual(A.red_fix_of(k7, facts)["entry"], e["id"])  # the tick's link: its names meet Fixes-Red: 2406
        self.assertEqual(self.st.item("red", "k8.2")["data"]["runs"], ["2470", "2473"])
        self.assertEqual(self.st.item("red", "k8")["state"], "cleared")  # one open red per key
        self.assertEqual(self.st.item("red", "k8")["data"]["merged_into"], "k8.2")
        self.assertEqual(self.st.item("red", "k6")["state"], "fix-pending")
        self.assertIsNone(self.st.item("red", "k5"))
        self.assertEqual(A.store_import(local, self.remote, lrepo)["merged"], 3)  # again: nothing new
        self.assertEqual(self.st.item("red", "k7")["version"], k7["version"])


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
        self.assertEqual(A.inv_w7()[0], True)  # local store: nothing to lag
        os.environ.update(AGENTCTL_STORE_URL=self.url, AGENTCTL_STORE_TOKEN="tok-pawel")
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
        self.assertEqual(got[:2], [b"READY=1", b"WATCHDOG=1"])
        url = subprocess.run(["git", "-C", os.path.join(tmp, "movies"), "remote", "get-url", "forgejo"],
                             capture_output=True, text=True).stdout.strip()
        self.assertEqual(url, origin)
        self.assertNotIn("events failed", p.stderr)  # the laptop's steps never run there
        self.assertTrue(os.path.exists(os.path.join(tmp, "state", "state.json")))


if __name__ == "__main__":
    unittest.main()
