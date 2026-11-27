"""THE END-TO-END SCENARIOS (the owner, 2026-11-27): the real store server, the real reconciler tick and sync, the laptop's
real CLI and a real git remote, together (tests/e2e/e2e_harness.py); GitHub is the stub (tests/e2e/ghstub.py). Each
scenario is one test, and each FAILS when its behaviour is broken (tests/e2e/mutations.py proves it per scenario)."""
import os, shutil, socket, subprocess, sys, tarfile, io, threading, time, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import e2e_harness as H  # noqa: E402  (first: tests/harness.py's hermetic environment)

DECIDED = ("landed", "waiting", "rejected", "cancelled", "superseded")
# THE OLDER REVISIONS (the skew scenarios), fixed shas of this repository's history:
# - a laptop from before the event model (56641c4: actor and cause on every move), 2026-11-26 14:07;
# - a ci-runner pinned the day before the event model's indexed reads (trace_events, derived_check_runs: API 2) and the
#   window's check runs (API 3) -- with the bad-object fix (32bc48a), without which an old reconciler's tick dies
OLD_CLIENT_REV = "12cecce"
OLD_SERVER_REV = "32bc48a"
RED_LOG = "test\tRun tests\t2026-11-27T10:05:00.0000000Z [info] - the toy check *** FAILED ***\n"


def queued_id(test, r):
    test.assertEqual(r.returncode, 0, r.stdout + r.stderr)
    line = next((x for x in r.stdout.splitlines() if x.startswith(("QUEUED ", "QUEUED-LOCAL "))), None)
    test.assertIsNotNone(line, r.stdout + r.stderr)
    return line.split()[1]


def no_traceback(test, r, what):
    test.assertNotIn("Traceback (most recent call last)", r.stdout + r.stderr, f"{what} crashed:\n{r.stdout}\n{r.stderr}")


class E2ETest(unittest.TestCase):
    TOOL = H.TOOL
    SERVER_TOOL = None

    def setUp(self):
        self.w = H.World(tool=self.TOOL, server_tool=self.SERVER_TOOL)
        self.addCleanup(self.w.close)
        r = self.w.tick()  # ci-runner's first tick: its clone of the remote (a submission needs it)
        self.assertEqual(r.returncode, 0, r.stderr[-3000:])

    def land(self, wt, **kw):
        return queued_id(self, self.w.cli("land", cwd=wt, **kw))

    def make_main_red(self):
        """Main's gate goes red: a commit that fails `the toy check`, and the stub's red Main run on it (its siblings
        green), its failed log naming that test."""
        sha = self.w.push_main("main goes red", "web/src/main/RED", "red\n")
        run = self.w.gh.add_run("main.yml", sha, conclusion="failure", jobs=[("test", "failure")], log=RED_LOG)
        for f in ("identity-model-convergence.yml", "identity-takes.yml"):
            self.w.gh.add_run(f, sha)
        return run

    def decided(self, *ids, deadline=60):
        """The reconciler runs until every change of ids is decided; their states."""
        self.w.start_reconciler()
        try:
            self.w.until(lambda: all(self.w.states().get(i) in DECIDED for i in ids),
                         f"{', '.join(ids)} decided", deadline)
        finally:
            self.w.stop_reconciler()
        return self.w.states()


class HappyPathTest(E2ETest):
    def test_a_change_is_queued_checked_landed_on_the_bare_main_and_its_verdict_reaches_the_inbox(self):
        wt, sha = self.w.agent("a", {"web/src/main/A.scala": "object A\n"}, "a: one")
        eid = self.land(wt)
        self.assertEqual(self.w.states()[eid], "queued")
        self.assertEqual(self.decided(eid)[eid], "landed")
        self.assertEqual(self.w.main_log()[0], "a: one", "on the bare remote's main")
        moves = [(e["from_state"], e["to_state"]) for e in self.w.events(eid)]
        self.assertEqual(moves, [(None, "queued"), ("queued", "checking"), ("checking", "checked"),
                                 ("checked", "pushing"), ("pushing", "landed")])
        for e in self.w.events(eid):
            self.assertTrue(e["cause"], f"an event with no cause: {e}")
            if e["from_state"] is not None:  # a move (the opening is the submission's)
                self.assertTrue(e["actor"], f"a move with no actor: {e}")
        r = self.w.sync()
        self.assertEqual(r.returncode, 0, r.stderr[-3000:])
        landed = [i for i in self.w.inbox() if i.get("kind") == "LANDED"]
        self.assertEqual(len(landed), 1, self.w.inbox())
        self.assertEqual(landed[0].get("cwd"), wt, "the verdict names the agent's worktree")
        r = self.w.cli("history", eid)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(f"change {eid}: now landed", r.stdout)
        for frm, to, cause in (("-", "queued", "submitted"), ("queued", "checking", "check-running"),
                               ("checking", "checked", "check-passed"), ("checked", "pushing", "pushing"),
                               ("pushing", "landed", "landed")):
            self.assertRegex(r.stdout, rf"{frm} -> {to}\s.*{cause}", "the timeline shows every move and its cause")


class RedGateTest(E2ETest):
    def test_tools_only_known_red_lands_a_production_change_waits_a_replayed_fix_lands(self):
        run = self.make_main_red()
        tw, _ = self.w.agent("tools", {"scripts/tool.sh": "echo tool 2\n"}, "tools: a script")
        tools = self.land(tw)
        pw, _ = self.w.agent("prod", {"web/src/main/P.scala": "object P\n"}, "prod: a source")
        prod = self.land(pw)
        states = self.decided(tools, prod)
        self.assertEqual(states[tools], "landed")
        self.assertIn("tools-only-known-red", [e["cause"] for e in self.w.events(tools)])
        self.assertEqual(states[prod], "waiting", "a production change waits behind the red")
        # the fix: a production change naming the red, its replay failing on main and passing at the fix
        fw = os.path.join(self.w.root, "wt-fix")
        H.sh(self.w.clone, "git", "worktree", "add", "-q", "-b", "fix", fw, "origin/main")
        H.sh(fw, "git", "rm", "-q", "web/src/main/RED")
        H.sh(fw, "git", "commit", "-q", "-m", f"fix: main green again\n\nFixes-Red: {run['id']}\n"
                                              "Fix-Replay: test ! -f web/src/main/RED\nCo-Authored-By: e2e <e2e@e2e>")
        r = self.w.cli("land", cwd=fw)
        self.assertIn("base=FAIL head=PASS", r.stderr)
        fix = queued_id(self, r)
        states = self.decided(fix)
        self.assertEqual(states[fix], "landed", self.w.changes()[fix]["data"].get("why"))
        self.assertEqual(self.w.changes()[fix]["data"]["replays"][0]["base"], "FAIL")
        self.assertEqual(self.w.main_log()[0], "fix: main green again")
        self.assertEqual(self.w.states()[prod], "waiting", "the waiting change stays held until the gate is green")


class StoreOutageTest(E2ETest):
    def test_a_change_queued_while_the_store_is_down_is_kept_resubmitted_and_lands_once(self):
        self.w.stop_store()
        wt, _ = self.w.agent("a", {"web/src/main/A.scala": "object A\n"}, "a: through an outage")
        r = self.w.cli("land", cwd=wt)
        self.assertIn("QUEUED-LOCAL", r.stdout, r.stdout + r.stderr)
        eid = queued_id(self, r)
        outbox = os.path.join(self.w.root, "laptop", "state", "outbox", f"{eid}.json")
        self.assertTrue(os.path.exists(outbox), "kept in the outbox")
        with open(outbox) as f:
            kept = f.read()
        self.w.start_store()
        self.assertNotIn(eid, self.w.changes(), "not in the store before the sync sends it")
        r = self.w.sync()
        self.assertEqual(r.returncode, 0, r.stderr[-3000:])
        self.assertEqual(self.w.states().get(eid), "queued", "the sync re-submitted it")
        self.assertFalse(os.path.exists(outbox))
        # the outbox's copy sent again (a submit whose answer was lost): still one change
        with open(outbox, "w") as f:
            f.write(kept)
        self.w.sync()
        self.assertEqual(self.decided(eid)[eid], "landed")
        self.w.sync()
        self.assertEqual([c for c in self.w.changes() if c == eid], [eid])
        self.assertEqual(len(self.w.changes()), 1, "one change item, never a second submission")
        self.assertEqual(self.w.main_log().count("a: through an outage"), 1, "landed once, not twice")


class SkewOldClientTest(E2ETest):
    """An older laptop (OLD_CLIENT_REV) against today's store server and reconciler."""

    @classmethod
    def setUpClass(cls):
        cls.old = old_checkout(OLD_CLIENT_REV)
        cls.TOOL = os.path.join(cls.old, "agentctl")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(os.path.dirname(cls.old), ignore_errors=True)

    def test_an_older_client_lands_and_reads_status_against_the_new_server(self):
        r = self.w.cli("status", bound=30)
        no_traceback(self, r, "the old status")
        self.assertEqual(r.returncode, 0, r.stderr[-2000:])
        wt, _ = self.w.agent("a", {"web/src/main/A.scala": "object A\n"}, "a: from an older laptop")
        r = self.w.cli("land", cwd=wt, bound=30)
        no_traceback(self, r, "the old land")
        eid = queued_id(self, r)
        self.assertEqual(self.decided(eid)[eid], "landed")
        r = self.w.sync(bound=30)
        no_traceback(self, r, "the old sync")
        self.assertEqual(self.w.main_log()[0], "a: from an older laptop")
        self.assertIn("LANDED", [i.get("kind") for i in self.w.inbox()], "the old sync projects the verdict")


class SkewOldServerTest(E2ETest):
    """Today's laptop against an older store server and reconciler (a pinned ci-runner)."""

    @classmethod
    def setUpClass(cls):
        cls.old = old_checkout(OLD_SERVER_REV)
        cls.SERVER_TOOL = os.path.join(cls.old, "agentctl")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(os.path.dirname(cls.old), ignore_errors=True)

    def test_a_new_client_degrades_cleanly_against_an_older_server(self):
        r = self.w.cli("status", bound=30)
        no_traceback(self, r, "status")
        self.assertEqual(r.returncode, 0, r.stderr[-2000:])
        wt, _ = self.w.agent("a", {"web/src/main/A.scala": "object A\n"}, "a: to an older server")
        r = self.w.cli("land", cwd=wt, bound=30)
        no_traceback(self, r, "land")
        eid = queued_id(self, r)
        self.assertEqual(self.decided(eid)[eid], "landed")
        # the sync's new calls (record_events, ...) the old store lacks: said as version skew, never a crash or a hang
        for _ in range(2):
            r = self.w.sync(bound=30)
            no_traceback(self, r, "the sync")
            self.assertEqual(r.returncode, 0, r.stderr[-2000:])
        # history and analyze read through calls the old store lacks (API 2, 3): its older reads instead
        r = self.w.cli("history", eid, bound=30)
        no_traceback(self, r, "history")
        self.assertEqual(r.returncode, 0, r.stderr[-2000:])
        self.assertRegex(r.stdout, r"pushing -> landed\s.*landed", "the timeline from the older store's feed")
        r = self.w.cli("analyze", bound=30)
        no_traceback(self, r, "analyze")
        self.assertEqual(r.returncode, 0, r.stderr[-2000:])


class MissingCommitTest(E2ETest):
    def test_a_change_whose_commit_the_server_cannot_read_never_stops_the_tick(self):
        self.make_main_red()  # the red rule reads every queued change's commits (the live dead tick's path)
        lw, _ = self.w.agent("lost", {"scripts/lost.sh": "echo lost\n"}, "lost: its commit never reaches ci-runner")
        lost = self.land(lw)
        tw, _ = self.w.agent("tools", {"scripts/tool.sh": "echo tool 2\n"}, "tools: a script")
        tools = self.land(tw)
        # its land ref and objects gone from the remote before ci-runner fetched them: `bad object` there
        bare = self.w.bare
        H.sh(self.w.root, "git", "--git-dir", bare, "update-ref", "-d", f"refs/heads/land/{lost}")
        H.sh(self.w.root, "git", "--git-dir", bare, "reflog", "expire", "--expire=now", "--all")
        H.sh(self.w.root, "git", "--git-dir", bare, "gc", "-q", "--prune=now")
        self.w.start_reconciler()
        try:
            self.w.until(lambda: self.w.states().get(tools) == "landed", "the other change landed", 60)
            self.w.until(lambda: "its commits are not here" in str(self.w.changes()[lost]["data"].get("why")),
                         "the lost change marked unreadable", 30)
            self.assertTrue(self.w.reconciler.alive(), "the tick goes on")
        finally:
            self.w.stop_reconciler()
        self.assertNotIn("Traceback (most recent call last)", self.w.reconciler.text())
        self.assertNotEqual(self.w.states()[lost], "landed")
        self.assertNotIn("lost: its commit never reaches ci-runner", self.w.main_log())


class Throttle:
    """A TCP proxy in front of the store that passes the request on and trickles the answer back at `rate` bytes/s
    (a slow uplink: the 2026-11-26 tunnel's ~40 KB/s, and slower), each connection a thread ended with it."""

    def __init__(self, port, rate):
        self.target, self.rate, self.stop = ("127.0.0.1", port), rate, threading.Event()
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(16)
        self.sock.settimeout(0.2)
        self.url = f"http://127.0.0.1:{self.sock.getsockname()[1]}"
        self.threads = [threading.Thread(target=self.accept, name="e2e-throttle", daemon=True)]
        self.threads[0].start()

    def accept(self):
        while not self.stop.is_set():
            try:
                c, _ = self.sock.accept()
            except (socket.timeout, OSError):
                continue
            t = threading.Thread(target=self.relay, args=(c,), name="e2e-throttle-conn", daemon=True)
            self.threads.append(t)
            t.start()

    def relay(self, c):
        up = socket.create_connection(self.target)
        for s in (c, up):
            s.settimeout(0.2)
        try:
            while not self.stop.is_set():
                for src, dst, slow in ((c, up, False), (up, c, True)):
                    try:
                        data = src.recv(1 if slow else 65536)
                    except socket.timeout:
                        continue
                    if not data:
                        return
                    if slow:
                        self.stop.wait(1.0 / self.rate)
                    dst.sendall(data)
        except OSError:
            return
        finally:
            c.close()
            up.close()

    def close(self):
        self.stop.set()
        self.sock.close()
        for t in self.threads:
            t.join(5)


class SlowLinkTest(E2ETest):
    STATUS_BOUND = 25  # s: the store call's own bound (remotestore STORE_WAITED_TIMEOUT, 15 s) and the CLI's start

    def test_status_over_a_throttled_link_returns_within_its_bound_naming_the_store(self):
        proxy = Throttle(self.w.port, rate=20)
        self.addCleanup(proxy.close)
        t0 = time.monotonic()
        r = self.w.cli("status", url=proxy.url, bound=self.STATUS_BOUND + 20)
        took = time.monotonic() - t0
        no_traceback(self, r, "status")
        self.assertLess(took, self.STATUS_BOUND, f"status took {took:.0f} s over the slow link")
        self.assertEqual(r.returncode, 0, r.stderr[-2000:])
        self.assertIn("STORE UNREACHABLE", r.stderr)
        self.assertIn(proxy.url, r.stderr, "it names the store")
        self.assertTrue(r.stdout.strip(), "the status from the snapshot all the same")


def old_checkout(rev):
    """Revision rev's agentctl, unpacked into a temp dir of its own (git archive of this checkout's history)."""
    root = os.path.realpath(H.tempfile.mkdtemp(prefix="agentctl-e2e-old-"))
    history = os.environ.get("AGENTCTL_E2E_HISTORY") or H.BIN  # a mutated copy (mutations.py) has no .git of its own
    data = subprocess.run(["git", "-C", history, "archive", rev], capture_output=True, check=True, timeout=60).stdout
    tarfile.open(fileobj=io.BytesIO(data)).extractall(os.path.join(root, "agentctl"), filter="data")
    return os.path.join(root, "agentctl")


if __name__ == "__main__":
    unittest.main()
