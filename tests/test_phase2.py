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
                                                    "AGENTCTL_ROLE", "AGENTCTL_MOVIES_REPO", "NOTIFY_SOCKET")}
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
        self.assertEqual(names, ["outbox", "state mirror", "events", "notifier", "landed worktrees", "claims", "dispatcher",
                                 "network watch", "tunnel probe"])
        self.assertTrue(set(A.LAPTOP_ONLY_STEPS) <= set(names))

    def test_the_sync_mirrors_ci_runners_state(self):
        A.state_update(tick_at=1234.0, changes=[{"id": "e1"}])
        snap = self.remote.state()
        os.environ["AGENTCTL_STATE_ROOT"] = os.path.join(self.tmp, "laptop")  # (one process plays both hosts here)
        A.mirror_state(type("Fetched", (), {"state": lambda self: snap})())
        mine = A.state_read()
        self.assertEqual((mine["tick_at"], mine["changes"]), (snap["tick_at"], [{"id": "e1"}]))
        self.assertIn("fetched_at", mine)


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
        self.assertEqual(n, {"leases": 1, "items": 1, "changes": 1})
        row = self.st.get("red/k9")
        self.assertEqual((row["epoch"], row["secret"]), (took["epoch"], took["secret"]))  # its holder renews it there
        self.assertEqual(self.remote.renew("red/k9", took["epoch"], took["secret"], "moved", 900), "ok")
        self.assertIsNone(self.st.get("red/old"))
        self.assertEqual(self.st.item("red", "k9")["state"], "leased")
        self.assertEqual([x["id"] for x in A.queued_entries(A.waiting_state(self.repo)[0])], [e["id"]])
        self.assertEqual(A.store_import(local, self.remote, lrepo)["leases"], 0)  # twice is harmless


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
