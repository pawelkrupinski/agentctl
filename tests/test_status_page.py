"""The status page (http://agentctl.localhost/): its data assembly from a mocked store and filesystem, the bounds that
keep one failing source to its own section, the log route's path restriction, the Host check, the server, the port
fallback and `status --open`."""
import contextlib, io, json, os, socket, subprocess, sys, tempfile, time, unittest, urllib.error, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402

T = 1_800_000_000.0


def lease(name, holder, meta, expires, **k):
    return {"name": name, "holder": holder, "epoch": 2, "secret": "SECRET-must-not-leak", "acquired_at": T - 600,
            "renewed_at": T - 30, "expires_at": expires, "max_until": T + 3600, "progress": "p", "item": None,
            "meta": json.dumps(meta), **k}


def item(typ, iid, state, data, entered=T - 900, deadline=T + 600, level=0):
    return {"type": typ, "id": iid, "state": state, "version": 1, "entered_at": entered, "deadline": deadline,
            "level": level, "data": data}


class FakeStore:
    """The Store methods the page reads, over plain lists."""
    def __init__(self, items=(), leases=(), events=(), meta=None, fail=()):
        self._items, self._leases, self._events = list(items), list(leases), list(events)
        self._meta, self.fail = dict(meta or {}), set(fail)

    def _f(self, m):
        if m in self.fail:
            raise A.StoreUnavailable(f"{m}: the tunnel is down")

    def clock(self):
        return T

    def items(self, typ, states=None):
        self._f("items")
        return [i for i in self._items if i["type"] == typ and (states is None or i["state"] in states)]

    def list(self, prefix=""):
        self._f("list")
        return [r for r in self._leases if r["name"].startswith(prefix)]

    def get(self, name):
        return next((r for r in self._leases if r["name"] == name), None)

    def offer_of(self, _key):
        return {"reason": "awaiting a taker"}

    def meta_get(self, k, default=None):
        self._f("meta_get")
        return self._meta.get(k, default)

    def events(self, after=0, limit=500):
        self._f("events")
        return [e for e in self._events if e["seq"] > after][:limit]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.realpath(self.tmp.name)
        self.env = {"AGENTCTL_STATE_ROOT": os.path.join(self.root, "state"),
                    "AGENTCTL_REDS_DIR": os.path.join(self.root, "state", "reds"),
                    "AGENTCTL_STATE_FILE": os.path.join(self.root, "state", "state.json"),
                    "AGENTCTL_TUNNEL_STATE": os.path.join(self.root, "state", "tunnel-health.json"),
                    "AGENTCTL_CLAUDE_PROJECTS": os.path.join(self.root, "projects")}
        self.saved = {k: os.environ.get(k) for k in list(self.env) + ["AGENTCTL_STORE_URL", "AGENTCTL_STATUS_PORT",
                                                                      "AGENTCTL_OPEN"]}
        os.environ.update(self.env)
        os.environ.pop("AGENTCTL_STORE_URL", None)
        os.makedirs(self.env["AGENTCTL_REDS_DIR"])
        self.patched = {n: getattr(A, n) for n in ("cached_dispatch_blocker", "port_open", "proc_alive")}
        A.cached_dispatch_blocker = lambda ttl=60: None
        A.port_open = lambda port, host="127.0.0.1", timeout=2.0: True
        A.proc_alive = lambda pid, start=None: pid == 4242
        for c in (A._status_wt, A._status_wt_reads, A._status_said, A._status_last, A._status_reading):
            c.clear()

    def tearDown(self):
        for n, f in self.patched.items():
            setattr(A, n, f)
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp.cleanup()

    def git(self, wt, *args):
        subprocess.run(["git", "-C", wt] + list(args), check=True, capture_output=True,
                       env=dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t",
                                GIT_COMMITTER_EMAIL="t@t"))

    def world(self):
        """A red leased by a live fixer (pid 4242) with a worktree, logs and a transcript; an offered red; changes (one
        past its deadline) and a batch; a condition; 80 events."""
        wt = os.path.join(self.root, "movies-wt-red-2473-5")
        os.makedirs(wt)
        self.git(wt, "init", "-q")
        for i in range(4):
            with open(os.path.join(wt, f"f{i}.scala"), "w") as f:
                f.write(str(i))
            self.git(wt, "add", ".")
            self.git(wt, "commit", "-qm", f"step {i}")
        with open(os.path.join(wt, "dirty.txt"), "w") as f:
            f.write("x")
        run_dir = os.path.join(self.env["AGENTCTL_REDS_DIR"], "2473")
        os.makedirs(run_dir)
        with open(os.path.join(run_dir, "fixer.log"), "w") as f:
            f.write("".join(f"line {i}\n" for i in range(100)))
        transcript = os.path.join(self.root, "projects", "t.jsonl")  # under the Claude projects root (Base)
        os.makedirs(os.path.dirname(transcript), exist_ok=True)
        with open(transcript, "w") as f:
            f.write(json.dumps({"type": "assistant", "timestamp": "2026-10-29T12:00:00Z",
                                "message": {"content": [{"type": "text", "text": "bisecting the takes drift"}]}}) + "\n")
            f.write(json.dumps({"type": "user", "message": {"content": "ok"}}) + "\n")
        with open(os.path.join(run_dir, "fixer-5.json"), "w") as f:
            json.dump({"pid": 4242, "run": 2473, "workflow": "Identity model convergence", "worktree": wt,
                       "started": "2026-10-29T11:00:00Z", "why": "tier deep", "transcript": transcript,
                       "label": "red fixer #2473"}, f)
        with open(os.path.join(run_dir, "fixer-1.json"), "w") as f:  # ended long ago: not on the page
            json.dump({"pid": 1111, "run": 2473, "worktree": wt, "started": "2020-01-01T00:00:00Z",
                       "ended": "2020-01-01T01:00:00Z"}, f)
        with open(self.env["AGENTCTL_STATE_FILE"], "w") as f:
            json.dump({"tick_at": T - 7, "version": "abc", "summary": {"at": T - 5, "line": "gate GREEN"},
                       "ci": {"repo": "pawelkrupinski/movies", "gate": [
                           {"name": "Main", "decided": {"number": 2515, "conclusion": "success"}, "newer": None},
                           {"name": "Identity model convergence", "decided": {"number": 2473, "databaseId": 9473, "conclusion": "failure"},
                            "newer": {"number": 2480, "status": "in_progress"}}]}}, f)
        reds = [item("red", "k1", "leased", {"key": "k1", "workflow": "Identity model convergence", "gate": True,
                                              "jobs": ["leg (poland)"], "last_run": {"number": 2473}, "opened_at": T - 7200,
                                              "history": [{"at": "x", "holder": "h", "outcome": "refuted"}]}),
                item("red", "k2", "offered", {"key": "k2", "workflow": "Archive recording", "job": "report",
                                               "last_run": {"number": 823, "databaseId": 9823}, "tier": "infra"}, level=2),
                item("red", "k3", "verified", {"key": "k3", "workflow": "Main", "last_run": {"number": 1}})]
        changes = [item("change", "c-late", "waiting", {"worktree": "/w/a", "head": "a" * 40}, deadline=T - 60),
                   item("change", "c-ok", "checking", {"worktree": "/w/b", "head": "b" * 40}),
                   item("change", "c-done", "landed", {"worktree": "/w/c"}),
                   item("batch", "b1", "blocked-red", {}, deadline=T + 60)]
        cond = item("condition", "cond1", "open", {"kind": "TUNNEL", "key": "tunnel", "text": "the tunnel is down",
                                                    "probe": {"kind": "port_open", "args": {}}, "until": "it answers",
                                                    "last_result": {"ok": False, "detail": "refused"}})
        leases = [lease("red/k1", "laptop:fixer:red-k1-5", {"kind": "fixer", "gate": True, "pid": 4242, "worktree": wt,
                                                            "tier": "deep", "run": 2473, "note": "found the drift",
                                                            "log": transcript}, T + 900),
                  lease("red/old", "laptop:fixer:gone", {"kind": "fixer"}, T - 10)]
        events = [{"seq": i, "at": T - 100 + i, "kind": "red.offered", "severity": "info", "item_type": "red",
                   "item_id": "k2", "from_state": "red", "to_state": "offered", "text": f"event {i}"} for i in range(1, 81)]
        return FakeStore(reds + changes + [cond], leases, events, {"projector_seq": 70}), wt


class DataTest(Base):
    def test_a_fix_pending_reds_note_is_never_a_holders_word_from_before_its_fix(self):
        """Order independence #74 (2026-11-15): both reds fix-pending on 09e482c6a, no holder, read "ORPHANED:
        laptop:owner:outside-gate holds it with no work" -- the 11:34 revoke, from before they went fix-pending."""
        h = [{"at": "2026-11-15T11:34:52Z", "outcome": "ORPHANED: laptop:owner:outside-gate holds it with no work"}]
        fp = {"state": "fix-pending", "entered_at": A.parse_time("2026-11-15T11:39:23Z").timestamp(),
              "data": {"history": h}}
        self.assertIsNone(A.red_history_note(fp))
        self.assertEqual(A.red_history_note({**fp, "entered_at": 0.0}), h[0]["outcome"], "written since: its note")
        self.assertEqual(A.red_history_note({**fp, "state": "offered"}), h[0]["outcome"], "why it was offered again")

    def test_every_section_from_the_store_and_the_filesystem(self):
        st, wt = self.world()
        d = A.status_data(store=st, now_=T)
        self.assertNotIn("SECRET", json.dumps(d, default=str), "a lease's secret never leaves the process")
        h = d["header"]
        self.assertTrue(h["gate_red"])
        self.assertEqual(h["tick_age"], 7)
        self.assertEqual(h["gate"][1]["url"], "https://github.com/pawelkrupinski/movies/actions/runs/9473")
        self.assertEqual(d["store"]["reachable"], True)
        self.assertEqual((d["dispatcher"]["alive"], d["dispatcher"]["cap"]), (1, A.RED_FIXERS))
        self.assertEqual([e["id"] for e in d["dispatcher"]["escalated"]], ["k2"])
        reds = d["reds"]
        self.assertEqual([r["key"] for r in reds], ["k1", "k2"], "open reds only, gate first")
        self.assertEqual(reds[0]["lease"]["holder"], "laptop:fixer:red-k1-5")
        self.assertEqual(reds[0]["note"], "found the drift")
        self.assertEqual(reds[0]["age"], 7200)
        self.assertEqual(reds[1]["run_url"], "https://github.com/pawelkrupinski/movies/actions/runs/9823")
        self.assertEqual(reds[1]["jobs"], ["report"])
        self.assertIsNone(reds[1]["lease"])
        [a] = d["agents"]
        self.assertEqual((a["pid"], a["alive"], a["tier"], a["worktree"]), (4242, True, "deep", wt))
        self.assertEqual(a["last_progress"], T - 30)
        self.assertEqual([c["subject"] for c in a["commits"]], ["step 3", "step 2", "step 1"])
        self.assertEqual(a["dirty"], ["?? dirty.txt"])
        self.assertEqual(a["recent_files"], ["f3.scala", "f2.scala", "f1.scala"])
        self.assertEqual(a["said"], "bisecting the takes drift")
        [log] = a["logs"]
        self.assertEqual(log["name"], "fixer.log")
        self.assertEqual(log["tail"].splitlines()[-1], "line 99")
        self.assertEqual(len(log["tail"].splitlines()), A.STATUS_LOG_TAIL)
        ch = {c["id"]: c for c in d["landing"]["changes"]}
        self.assertEqual(set(ch), {"c-late", "c-ok"})
        self.assertTrue(ch["c-late"]["past_deadline"])
        self.assertFalse(ch["c-ok"]["past_deadline"])
        self.assertEqual([b["id"] for b in d["landing"]["batch"]], ["b1"])
        self.assertEqual(d["conditions"][0]["kind"], "TUNNEL")
        self.assertEqual([e["seq"] for e in d["events"]][:3], [80, 79, 78])
        self.assertEqual(len(d["events"]), A.STATUS_EVENTS)

    def test_a_failing_source_is_an_error_in_its_own_section_only(self):
        st, _wt = self.world()
        st.fail = {"items"}
        d = A.status_data(store=st, now_=T)
        for k in ("reds", "landing", "conditions"):
            self.assertIn("tunnel is down", d[k]["error"])
        self.assertIsInstance(d["agents"], list)
        self.assertIsInstance(d["events"], list)
        self.assertTrue(d["header"]["gate_red"])

    def test_a_hanging_source_is_cut_at_its_bound(self):
        st, _wt = self.world()
        orig = st.events
        hang = __import__("threading").Event()  # set at its end: the read ends with the test (harness: no stray thread)
        self.addCleanup(hang.set)
        st.events = lambda after=0, limit=500: hang.wait(3) or orig(after, limit)
        t0 = time.time()
        d = A.status_data(store=st, now_=T, bound=0.5)
        self.assertLess(time.time() - t0, 2.5)
        self.assertIn("no answer within", d["events"]["error"])
        self.assertIsInstance(d["reds"], list)

    def test_the_servers_reads_show_a_slow_source_from_its_last_read(self):
        st, _wt = self.world()
        d = A.status_data(store=st, now_=T, bound=2, keep=True)
        self.assertEqual(len(d["events"]), A.STATUS_EVENTS)
        self.assertEqual(d["stale"], {})
        self.assertIn("events", d["took"])
        gate = __import__("threading").Event()
        orig = st.events
        st.events = lambda after=0, limit=500: gate.wait(10) and orig(after, limit)
        t0 = time.time()
        d = A.status_data(store=st, now_=T, bound=0.5, keep=True)
        self.assertLess(time.time() - t0, 2.5)
        self.assertEqual(len(d["events"]), A.STATUS_EVENTS, "the last finished read, not an error")
        self.assertIn("still reading", d["stale"]["events"])
        d = A.status_data(store=st, now_=T, bound=0.5, keep=True)
        self.assertIn("events", d["stale"], "the read still going is not started twice")
        gate.set()
        A._status_reading["events"].join(10)
        d = A.status_data(store=st, now_=T, bound=2, keep=True)
        self.assertNotIn("events", d["stale"])

    def test_old_fixer_records_are_not_opened(self):
        _st, _wt = self.world()
        old = os.path.join(self.env["AGENTCTL_REDS_DIR"], "2473", "fixer-1.json")
        young = os.path.join(self.env["AGENTCTL_REDS_DIR"], "2473", "fixer-5.json")
        os.utime(old, (T - 9 * 3600, T - 9 * 3600))
        os.utime(young, (T - 60, T - 60))
        recs = {r["path"]: r for r in A.status_fixer_records(T)}
        self.assertEqual(list(recs), [young])
        self.assertTrue(recs[young]["alive"])

    def test_an_unreachable_remote_store(self):
        os.environ["AGENTCTL_STORE_URL"] = "http://127.0.0.1:9"
        try:
            d = A.status_data(store=A.RemoteStore("http://127.0.0.1:9", tok="x", timeout=1), now_=T)
        finally:
            os.environ.pop("AGENTCTL_STORE_URL", None)
        self.assertFalse(d["store"]["reachable"])
        self.assertIn("error", d["reds"])
        self.assertIn("line", d["header"])


class AgentDetailTest(Base):
    """The agents' details: one agent's slow worktree degrades its own row only; HEAD-keyed cache; HEAD without git."""
    def repo(self, name, commits=2):
        wt = os.path.join(self.root, name)
        os.makedirs(wt)
        self.git(wt, "init", "-q")
        for i in range(commits):
            self.commit(wt, i)
        return wt

    def commit(self, wt, i):
        with open(os.path.join(wt, f"f{i}.txt"), "w") as f:
            f.write(str(i))
        self.git(wt, "add", ".")
        self.git(wt, "commit", "-qm", f"c{i}")

    def store(self, *wts):
        return FakeStore(leases=[lease(f"red/k{i}", f"laptop:fixer:k{i}", {"kind": "fixer", "pid": 4242 + i, "worktree": wt,
                                                                          "run": 100 + i}, T + 900)
                                 for i, wt in enumerate(wts)])

    def test_a_slow_worktree_degrades_its_own_row_only(self):
        """No wall-clock bound (2026-11-08: `< 2.5 s` failed at laptop load ~300 -- real git in the fast worktree took
        4 s): git's answers are read up front and served at once, the slow worktree's held until released, and the test
        asserts the work done -- the page answered while the slow read was still held, the fast row complete."""
        fast, slow = self.repo("fast"), self.repo("slow")
        real = A.status_git
        release = __import__("threading").Event()
        asks = (("log", "-3", "--format=%h\x1f%cr\x1f%s"), ("log", "-3", "--name-only", "--format="),
                ("status", "--porcelain"))
        answers = {(wt, a): real(wt, *a) for wt in (fast, slow) for a in asks}
        held = []

        def git(wt, *args, **k):
            if wt == slow:
                held.append(args)
                release.wait(30)
            return answers[(wt, args)]
        A.status_git = git
        os.environ["AGENTCTL_STATUS_AGENT_BOUND"] = "1"
        try:
            rows = {a["worktree"]: a for a in A.status_agents(self.store(fast, slow), T, records=[])}
            self.assertFalse(release.is_set())
            self.assertTrue(held and A._status_wt_reads[slow].is_alive(),
                            "the page answered while the slow worktree's read was still held: it never waits for it")
            self.assertEqual([c["subject"] for c in rows[fast]["commits"]], ["c1", "c0"])
            self.assertNotIn("git_stale", rows[fast])
            self.assertIn("git still reading", rows[slow]["git_stale"])
            self.assertNotIn("commits", rows[slow])
            self.assertEqual(rows[slow]["pid"], 4243, "the slow row keeps everything but its git details")
            release.set()
            A._status_wt_reads[slow].join(10)
            rows = {a["worktree"]: a for a in A.status_agents(self.store(fast, slow), T, records=[])}
            self.assertEqual([c["subject"] for c in rows[slow]["commits"]], ["c1", "c0"],
                             "the background read filled the cache for the next refresh")
        finally:
            release.set()
            A.status_git = real
            os.environ.pop("AGENTCTL_STATUS_AGENT_BOUND", None)

    def test_git_log_runs_again_only_when_head_moves(self):
        wt = self.repo("wt")
        real, calls = A.status_git, []
        A.status_git = lambda w, *a, **k: calls.append(a[0]) or real(w, *a, **k)
        try:
            A.status_worktree(wt, 5)
            self.assertEqual(sorted(calls), ["log", "log", "status"])
            calls.clear()
            A.status_worktree(wt, 5)
            self.assertEqual(calls, [], "HEAD unchanged, status young: all from the cache")
            self.commit(wt, 7)
            out = A.status_worktree(wt, 5)
            self.assertEqual(calls, ["log", "log"])
            self.assertEqual(out["commits"][0]["subject"], "c7")
        finally:
            A.status_git = real

    def test_head_from_the_files(self):
        wt = self.repo("main-repo")
        self.git(wt, "worktree", "add", "-q", "-b", "side", os.path.join(self.root, "linked"))
        linked = os.path.join(self.root, "linked")
        self.commit(linked, 9)
        self.git(wt, "pack-refs", "--all")
        for w in (wt, linked):
            want = subprocess.run(["git", "-C", w, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
            self.assertEqual(A.status_head(w), want, w)
        self.assertIsNone(A.status_head(os.path.join(self.root, "nope")))


class LogPathTest(Base):
    def test_only_text_logs_under_the_state_root(self):
        ok = os.path.join(self.env["AGENTCTL_REDS_DIR"], "2473", "claude-1.log")
        os.makedirs(os.path.dirname(ok))
        for p in (ok, os.path.join(self.env["AGENTCTL_STATE_ROOT"], "store.db"), os.path.join(self.root, "outside.log")):
            with open(p, "w") as f:
                f.write("x")
        os.symlink(os.path.join(self.root, "outside.log"), os.path.join(self.env["AGENTCTL_STATE_ROOT"], "escape.log"))
        self.assertEqual(A.status_log_path(ok), ok)
        for bad in (os.path.join(self.env["AGENTCTL_STATE_ROOT"], "store.db"), os.path.join(self.root, "outside.log"),
                    os.path.join(self.env["AGENTCTL_REDS_DIR"], "..", "..", "outside.log"),
                    os.path.join(self.env["AGENTCTL_STATE_ROOT"], "escape.log"), "reds/2473/claude-1.log", "",
                    ok + "\0", "/etc/passwd", os.path.join(self.env["AGENTCTL_STATE_ROOT"], "missing.log")):
            self.assertIsNone(A.status_log_path(bad), bad)

    def test_host_names(self):
        for h in ("agentctl.localhost", "agentctl.localhost:3015", "127.0.0.1", "127.0.0.1:80", "localhost:3015",
                  "AGENTCTL.localhost"):
            self.assertTrue(A.status_host_ok(h), h)
        for h in (None, "", "evil.com", "agentctl.localhost.evil.com", "evil.localhost", "[::1]:80", "127.0.0.1:x",
                  "localhost:80:80"):
            self.assertFalse(A.status_host_ok(h), h)


class ServerTest(Base):
    def setUp(self):
        super().setUp()
        self.saved_cached = A.status_cached
        A.status_cached = lambda: {"at": T, "reds": []}
        self.srv = A.status_page_server("127.0.0.1", 0)
        self.port = self.srv.server_address[1]
        import threading
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        A.status_cached = self.saved_cached
        super().tearDown()

    def get(self, path, host="agentctl.localhost"):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", headers={"Host": host})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, r.read().decode(), r.headers
        except urllib.error.HTTPError as e:
            with e:
                return e.code, e.read().decode(), e.headers

    def test_routes(self):
        code, body, hd = self.get("/")
        self.assertEqual(code, 200)
        self.assertIn("/api/status", body)
        self.assertIn("prefers-color-scheme:dark", body)
        self.assertIn("default-src 'none'", hd["Content-Security-Policy"])
        code, body, _ = self.get("/api/status", host=f"127.0.0.1:{self.port}")
        self.assertEqual((code, json.loads(body)["at"]), (200, T))
        self.assertEqual(self.get("/nope")[0], 404)

    def request(self, method, path, headers=None):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", method=method,
                                     headers={"Host": "agentctl.localhost", **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, r.read().decode(), r.headers
        except urllib.error.HTTPError as e:
            with e:
                return e.code, e.read().decode(), e.headers

    def test_every_other_method_is_refused_and_head_has_no_body(self):
        for m in ("POST", "PUT", "DELETE", "PATCH"):
            code, body, _ = self.request(m, "/api/status")
            self.assertEqual((code, body), (405, "read-only: GET only\n"), m)
        code, body, hd = self.request("HEAD", "/")
        self.assertEqual((code, body, hd["Content-Type"]), (200, "", "text/html; charset=utf-8"))

    def test_agent_and_stream_routes_name_what_is_wrong(self):
        saved = A.live_lookup
        A.live_lookup = lambda ident: {"id": ident} if ident == "77" else None
        try:
            self.assertEqual(self.get("/agent/nobody")[0:2], (404, "no such agent: an id from the page's Agents section\n"))
            self.assertEqual(self.get("/agent/77")[0], 200)
            self.assertEqual(self.get("/stream?agent=nobody")[0], 404)
            self.assertEqual(self.get("/stream?agent=77&from=x1")[0:2], (400, "from: a byte offset\n"))
            code, body, hd = self.request("HEAD", "/stream?agent=77")
            self.assertEqual((code, body, hd["Content-Type"]), (200, "", "text/event-stream"))
        finally:
            A.live_lookup = saved

    def test_a_failing_route_is_that_requests_500_alone(self):
        saved = A.status_cached
        A.status_cached = lambda: 1 / 0
        try:
            self.assertEqual(self.get("/api/status")[0:2], (500, "error: ZeroDivisionError: division by zero\n"))
        finally:
            A.status_cached = saved
        self.assertEqual(self.get("/api/status")[0], 200)

    def test_other_hosts_are_refused(self):
        self.assertEqual(self.get("/api/status", host="rebind.example.com")[0], 403)

    def test_log_route(self):
        p = os.path.join(self.env["AGENTCTL_REDS_DIR"], "fixer.log")
        with open(p, "w") as f:
            f.write("hello fixer\n")
        code, body, _ = self.get("/log?path=" + urllib.request.quote(p))
        self.assertEqual(code, 200)
        self.assertIn("hello fixer", body)
        self.assertEqual(self.get("/log?path=/etc/passwd")[0], 403)
        self.assertEqual(self.get("/log?path=" + urllib.request.quote(p + "/../../../../etc/hosts"))[0], 403)

    def test_log_route_masks_credentials_like_the_stream(self):
        p = os.path.join(self.env["AGENTCTL_REDS_DIR"], "claude-1.log")
        with open(p, "w") as f:
            f.write("export AGENTCTL_STORE_TOKEN=abc123def456ghi789\nAuthorization: Bearer ghp_" + "a1" * 20 + "\n")
        code, body, _ = self.get("/log?path=" + urllib.request.quote(p))
        self.assertEqual(code, 200)
        self.assertNotIn("abc123def456ghi789", body)
        self.assertNotIn("a1a1a1a1", body)
        self.assertIn(A.LIVE_MASKED, body)


class AgentMetaPathTest(Base):
    """An agent row's run and transcript come from a lease's meta (any store user's): never a path of their own."""

    def test_logs_and_said_are_masked_and_a_crafted_run_or_transcript_reads_nothing(self):
        d = os.path.join(self.env["AGENTCTL_REDS_DIR"], "77")
        os.makedirs(d)
        with open(os.path.join(d, "fixer.log"), "w") as f:
            f.write("password=s3cretValue12345\n")
        logs = A.status_agent_logs("77")
        self.assertEqual(len(logs), 1)
        self.assertNotIn("s3cretValue12345", logs[0]["tail"])
        with open(os.path.join(self.root, "state", "x.log"), "w") as f:
            f.write("outside the run\n")
        self.assertEqual(A.status_agent_logs(".."), [])
        self.assertEqual(A.status_agent_logs("../.."), [])
        outside = os.path.join(self.root, "elsewhere.jsonl")
        with open(outside, "w") as f:
            f.write('{"type": "assistant", "message": {"content": "leaked"}}\n')
        a = A.status_agent_enrich({"transcript": outside, "run": None}, bound=1)
        self.assertNotIn("said", a)


class StartTest(Base):
    def setUp(self):
        super().setUp()
        self.saved_ports = A.STATUS_PAGE_PORT, A.STATUS_PAGE_FALLBACK, dict(A._status_page)
        A._status_page.clear()
        A.port_open = self.patched["port_open"]
        self.saved_warm = A.status_warm
        A.status_warm = lambda: None

    def tearDown(self):
        srv = A._status_page.get("server")
        if srv:
            srv.shutdown()
            srv.server_close()
        A.STATUS_PAGE_PORT, A.STATUS_PAGE_FALLBACK, saved = self.saved_ports
        A._status_page.clear()
        A._status_page.update(saved)
        A.status_warm = self.saved_warm
        super().tearDown()

    def free_port(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    def test_a_taken_first_port_falls_back_and_open_names_it(self):
        taken = socket.socket()
        taken.bind(("127.0.0.1", 0))
        taken.listen(1)
        A.STATUS_PAGE_PORT, A.STATUS_PAGE_FALLBACK = taken.getsockname()[1], self.free_port()
        try:
            with contextlib.redirect_stderr(io.StringIO()) as e:
                url = A.status_page_start()
        finally:
            taken.close()
        self.assertEqual(url, f"http://agentctl.localhost:{A.STATUS_PAGE_FALLBACK}/")
        self.assertIn("already listens", e.getvalue())
        self.assertEqual(A.status_page_start(), url, "once per process")
        self.assertEqual(A.status_page_url(), url)
        opened = os.path.join(self.root, "opened")
        os.environ["AGENTCTL_OPEN"] = os.path.join(self.root, "fake-open")
        with open(os.environ["AGENTCTL_OPEN"], "w") as f:
            f.write(f"#!/bin/sh\necho \"$1\" > {opened}\n")
        os.chmod(os.environ["AGENTCTL_OPEN"], 0o755)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(A.main(["status", "--open"]), 0)
        self.assertEqual(out.getvalue().strip(), url)
        with open(opened) as f:
            self.assertEqual(f.read().strip(), url)

    def test_switched_off(self):
        os.environ["AGENTCTL_STATUS_PAGE"] = "off"
        try:
            self.assertIsNone(A.status_page_start())
        finally:
            os.environ.pop("AGENTCTL_STATUS_PAGE")


if __name__ == "__main__":
    unittest.main()
