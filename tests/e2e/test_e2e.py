"""THE END-TO-END SCENARIOS (the owner, 2026-11-27): the real store server, the real reconciler tick and sync, the laptop's
real CLI and a real git remote, together (tests/e2e/e2e_harness.py); GitHub is the stub (tests/e2e/ghstub.py). Each
scenario is one test, and each FAILS when its behaviour is broken (tests/e2e/mutations.py proves it per scenario)."""
import glob, io, json, os, re, shutil, socket, subprocess, sys, tarfile, threading, time, unittest

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
            self.assertTrue(e["actor"], f"an event with no actor: {e}")  # the opening's included
        r = self.w.sync()
        self.assertEqual(r.returncode, 0, r.stderr[-3000:])
        self.assertEqual([ln for ln in r.stderr.splitlines() if re.match(r"agentctl: [\w ]+ failed: ", ln)], [],
                         "every step of the sync ran (a machine without the claude CLI included)")
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


class RedFixerWorld(E2ETest):
    """A RED FIXER IS DISPATCHED, ENDS AND LEAVES ITS WORKTREE (edbf355, 2026-11-28): Main goes red (the stub's failed
    run), the server's tick loop opens the red and offers it, the laptop's sync dispatches a fixer -- a detached wrapper
    (`red fixer`) that launches a STUB claude (claude_stub.py) in a fresh worktree off main -- and the wrapper judges the
    session's final answer, releases the red's lease with it and ends the worktree (worktreelife.end_fixer_tree)."""

    def setUp(self):
        super().setUp()
        w = self.w
        w.write(".claude/settings.json", '{"permissions": {"defaultMode": "auto"}}', at=w.home)
        w.write("families.json", '{"families": []}', at=w.root)  # a red in no family is its run's own: one short-lived fixer
        self.claude = os.path.join(w.root, "claude")
        w.extra_env.update(AGENTCTL_CLAUDE=os.path.join(H.HERE, "claude_stub.py"), E2E_CLAUDE_DIR=self.claude,
                           AGENTCTL_RED_FAMILIES=os.path.join(w.root, "families.json"), AGENTCTL_RED_TICK="0")
        # Main was green once (a red check is tracked), at a commit no clone holds (so there is no culprit to blame)
        w.gh.add_run("main.yml", "0" * 40, jobs=[("test", "success")])
        self.make_main_red()

    def reds(self):
        return {r["id"]: dict(r, data=json.loads(r["data"] or "{}")) for r in self.w.db("SELECT id, state, data FROM items WHERE type='red'")}

    def server_ticks_until(self, cond, what):
        """The server's tick loop (the reconciler as ci-runner runs it: it reads the gate and opens, offers and judges the reds) until cond()."""
        loop = self.w.spawn("server-loop", [self.w.server_tool, "tick"], dict(self.w.server_env(), AGENTCTL_TICK="0.3"))
        try:
            return self.w.until(cond, what, 60)
        finally:
            loop.stop()
            self.w.procs.remove(loop)

    def fixers(self):
        out = []
        paths = [x for x in glob.glob(os.path.join(self.w.root, "laptop", "reds", "*", "fixer*.json")) if re.search(r"fixer(-\d+)?\.json$", x)]
        for p in sorted(paths, key=lambda x: int((re.search(r"fixer-(\d+)", x) or [0, 1])[1])):  # fixer.json, fixer-2.json, ...
            with open(p) as f:
                out.append(json.load(f))
        return out

    def dispatch_a_fixer(self, n=1, **session):
        """The laptop's sync dispatches fixer n of the red (the stub's session does `session`: E2E_CLAUDE_VERDICT / _COMMIT); its record once its wrapper has ended."""
        self.w.extra_env.update(**session)
        self.w.sync()
        self.w.until(lambda: len(self.fixers()) >= n, f"fixer {n} dispatched")
        self.w.until(lambda: self.fixers()[n - 1].get("ended") and not self.alive(self.fixers()[n - 1].get("pid")),
                     f"fixer {n}'s wrapper ended")
        return self.fixers()[n - 1]

    @staticmethod
    def alive(pid):
        if not pid:
            return False
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False

    def fixer_inbox(self):
        return [str(e.get("verdict")) for e in self.w.inbox() if str(e.get("verdict")).startswith("RED FIXER ")]

    def archives(self):
        return glob.glob(os.path.join(self.w.root, "laptop", "state", "fixer-archive", "*", "untracked", "agent-notes.txt"))

    def offered(self):
        self.server_ticks_until(lambda: any(r["state"] == "offered" for r in self.reds().values()), "the red offered")


class RedFixerLandedTest(RedFixerWorld):
    def test_a_fixer_that_landed_ends_ok_and_its_worktree_is_not_archived_or_retired(self):
        self.offered()
        rec = self.dispatch_a_fixer(E2E_CLAUDE_VERDICT=f"LANDED {self.w.main_sha()[:10]}")
        self.assertEqual((rec["rc"], rec["verdict"][:6]), (0, "LANDED"), rec)
        self.assertEqual(self.fixer_inbox(), [f"RED FIXER Main #2: {rec['verdict']}"])
        self.assertTrue(os.path.isdir(rec["worktree"]), "a landed fixer's worktree is no give-up's: not retired")
        self.assertEqual(self.archives(), [])


class RedFixerGaveUpTest(RedFixerWorld):
    def test_a_fixer_that_gave_up_has_its_worktree_archived_and_retired_with_its_branch(self):
        self.offered()
        rec = self.dispatch_a_fixer(E2E_CLAUDE_VERDICT="GAVE UP: cannot reproduce")
        self.assertEqual((rec["rc"], rec["verdict"]), (1, "GAVE UP: cannot reproduce"), rec)
        self.assertEqual(self.fixer_inbox(), ["RED FIXER Main #2: GAVE UP: cannot reproduce"])
        self.assertFalse(os.path.exists(rec["worktree"]), "its worktree is removed")
        self.assertNotIn("red-2", H.sh(self.w.clone, "git", "branch", "--list"), "and its branch")
        [archived] = self.archives()
        self.assertEqual(open(archived).read(), "uncommitted notes\n", "its uncommitted work is archived first")


class RedFixerUnlandedTest(RedFixerWorld):
    def test_a_fixer_that_gave_up_with_unlanded_commits_keeps_its_worktree(self):
        self.offered()
        rec = self.dispatch_a_fixer(E2E_CLAUDE_VERDICT="GAVE UP: cannot reproduce", E2E_CLAUDE_COMMIT="work.txt=a fix in progress")
        self.assertEqual(rec["rc"], 1)
        self.assertTrue(os.path.exists(os.path.join(rec["worktree"], "work.txt")), "kept: it holds a commit not on main")
        self.assertEqual(self.archives(), [], "nothing archived: the tree goes on")


class RedFixerContinuesTest(RedFixerWorld):
    def test_the_next_fixer_continues_in_the_worktree_holding_the_unlanded_commits(self):
        """Fixer 2 is the ladder's RESUME (its session and tree); fixer 3 a FRESH one, which still goes on in the tree
        that holds unlanded commits (FIXER CONTINUITY, fixerland.previous_fixer_tree) instead of starting off main."""
        self.offered()
        first = self.dispatch_a_fixer(E2E_CLAUDE_VERDICT="GAVE UP: cannot reproduce", E2E_CLAUDE_COMMIT="work.txt=a fix in progress")
        for n, commit in ((2, "more.txt=more"), (3, "third.txt=third")):
            self.server_ticks_until(lambda: self.reds() and all(r["state"] == "offered" and r["data"]["attempts"] == n - 1
                                                                for r in self.reds().values()), f"the red offered for fixer {n}")
            last = self.dispatch_a_fixer(n, E2E_CLAUDE_COMMIT=commit, E2E_CLAUDE_VERDICT="GAVE UP: still cannot reproduce")
        self.assertEqual(last["worktree"], first["worktree"], "the same tree, never a fresh one")
        self.assertEqual(last["continued_from"], first["worktree"], "a fresh fixer going on in the previous one's tree")
        self.assertEqual([c["cwd"] for c in self.claude_calls("--bg")], [first["worktree"]] * 3)
        for f in ("work.txt", "more.txt", "third.txt"):
            self.assertTrue(os.path.exists(os.path.join(first["worktree"], f)), f"{f}: the commits pile up in one tree")

    def claude_calls(self, verb):
        with open(os.path.join(self.claude, "calls.jsonl")) as f:
            return [c for c in map(json.loads, f) if c["argv"][:1] == [verb]]


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


def vendor_table():
    return os.path.join("devtools", "design_rules", "VENDORED.tsv")


class VendorDriftWorld(E2ETest):
    """AN AGENT-ROUTED CONDITION GOES TO AN AGENT, NOT THE OWNER (2026-11-27, condition vendor-drift: the laptop's
    dispatcher started its agent at 20:08:28Z and the server's escalation told the owner "ESCALATED: VENDOR_DRIFT open
    for 10 min" five seconds later). The laptop's sync opens a VENDOR_DRIFT condition (a movies checkout -- here a temp
    one -- ahead of one vendored copy), its escalation dispatcher offers, leases and launches it through a STUB claude
    (tests/e2e/claude_stub.py, $AGENTCTL_CLAUDE: never the real one) in a fresh worktree of a toy agentctl clone, the
    server's tick holds the owner's escalation while the agent works, and the condition's own probe closes it."""

    def setUp(self):
        super().setUp()
        w = self.w
        bare, self.bin = os.path.join(w.root, "github", "bin.git"), os.path.join(w.root, "bin")
        H.sh(w.root, "git", "init", "-q", "--bare", "-b", "main", bare)
        H.sh(w.root, "git", "init", "-q", "-b", "main", self.bin)
        H.sh(self.bin, "git", "remote", "add", "origin", bare)
        self.toy_agentctl()
        w.commit("init", at=self.bin)
        H.sh(self.bin, "git", "push", "-q", "origin", "HEAD:main")
        with open(os.path.join(H.BIN, "devtools", "design_rules", "VENDORED.tsv")) as f:
            copy, source, _ = next(x for x in f if x.strip() and not x.startswith("#")).rstrip("\n").split("\t")
        movies = os.path.join(w.root, "movies-ahead")
        theirs = os.path.join(movies, source)
        os.makedirs(os.path.dirname(theirs))
        with open(os.path.join(H.BIN, copy)) as f, open(theirs, "w") as g:
            g.write(f.read() + "# movies moved ahead\n")
        w.write(".claude/settings.json", '{"permissions": {"defaultMode": "auto"}}', at=w.home)
        self.claude = os.path.join(w.root, "claude")
        w.extra_env.update(AGENTCTL_CLAUDE=os.path.join(H.HERE, "claude_stub.py"), E2E_CLAUDE_DIR=self.claude,
                           AGENTCTL_BIN_REPO=self.bin, MOVIES_ROOT=movies,
                           E2E_CLAUDE_FIX_FROM=os.path.join(H.BIN, copy), E2E_CLAUDE_FIX_TO=theirs)

    def toy_agentctl(self):
        """The toy agentctl clone: the real sync tools (devtools/vendor*.py, selfpush and the lib they import), ONE vendored
        copy and its table, and a test suite and ratchet that are stubs -- the toy suite FAILS (a breaking drift, the
        agent's) until `toy_suite(True)`."""
        w = self.w
        for rel in ("devtools/vendor.py", "devtools/vendorscope.py", "devtools/vendorsync.py", "devtools/selfpush.py",
                    "devtools/lib/tool_git.py", "devtools/lib/tool_process.py"):
            os.makedirs(os.path.dirname(os.path.join(self.bin, rel)), exist_ok=True)
            shutil.copyfile(os.path.join(H.BIN, rel), os.path.join(self.bin, rel))
        shutil.copytree(os.path.join(H.BIN, "lib"), os.path.join(self.bin, "lib"), ignore=shutil.ignore_patterns("__pycache__"))
        with open(os.path.join(H.BIN, "devtools", "design_rules", "VENDORED.tsv")) as f:
            copy, source, digest = next(x for x in f if x.strip() and not x.startswith("#")).rstrip("\n").split("\t")
        os.makedirs(os.path.join(self.bin, "devtools", "design_rules"))
        shutil.copyfile(os.path.join(H.BIN, copy), os.path.join(self.bin, copy))
        w.write(vendor_table(), f"# copies\n{copy}\t{source}\t{digest}\n", at=self.bin)
        w.write("quality/tools.py", "print('python3')\n", at=self.bin)
        w.write("quality/ratchet.py", "", at=self.bin)
        w.write("tests/test_vendored.py", "", at=self.bin)
        self.toy_suite(False, commit=False)

    def toy_suite(self, ok, commit=True):
        self.w.write("tests/run.py", "import sys\n" + ("" if ok else "print('FAIL test_vendored: the new API'); sys.exit(1)\n"), at=self.bin)
        if commit:
            self.w.commit("the toy suite " + ("passes" if ok else "fails"), at=self.bin)
            H.sh(self.bin, "git", "push", "-q", "origin", "HEAD:main")

    def autosync_verdict(self):
        path = os.path.join(self.w.root, "laptop", "state", "vendor-autosync.json")
        if not os.path.exists(path):
            return None
        with open(path) as f:
            return json.load(f)

    def until_autosync(self, state):
        return self.w.until(lambda: (self.autosync_verdict() or {}).get("state") == state, f"the automatic sync {state}")

    def sync(self):
        r = self.w.sync()
        no_traceback(self, r, "the sync")
        return r

    def condition(self):
        rows = self.w.db("SELECT state, data FROM items WHERE type='condition' AND id='vendor-drift'")
        return dict(rows[0], data=json.loads(rows[0]["data"] or "{}")) if rows else None

    def claude_calls(self, verb):
        p = os.path.join(self.claude, "calls.jsonl")
        calls = [json.loads(x) for x in open(p)] if os.path.exists(p) else []
        return [c for c in calls if c["argv"][:1] == [verb]]

    def owner_escalations(self):
        return [e["text"] for e in self.w.events("vendor-drift") if str(e.get("text") or "").startswith("ESCALATED")]

    def inbox_escalations(self):
        return [str(e.get("verdict")) for e in self.w.inbox() if "ESCALAT" in str(e.get("verdict"))]

    def open_past_its_bound(self):
        self.sync()  # the first look starts the automatic sync, and no condition yet
        self.assertIsNone(self.condition())
        self.until_autosync("failed")  # the toy suite fails: a breaking drift, an agent's
        self.w.forget_meta("vendor_drift:checked")
        self.sync()
        self.assertEqual((self.condition() or {}).get("state"), "open", self.w.diagnosis())
        self.w.backdate("condition", "vendor-drift", 11 * 60, ("opened_at",))  # 11 min open: past the dispatch bound

class EscalationTest(VendorDriftWorld):
    def test_an_agent_routed_condition_is_dispatched_and_clears_with_no_owner_escalation(self):
        self.open_past_its_bound()
        self.sync()  # the escalation dispatcher: the offer, its lease, the launch
        bg = self.claude_calls("--bg")
        self.assertEqual(len(bg), 1, self.w.diagnosis())
        self.assertTrue(bg[0]["cwd"].startswith(self.bin + "-wt-escalation-vendor-drift"), bg[0]["cwd"])
        self.assertIn("escalation · VENDOR_DRIFT", bg[0]["argv"])
        self.assertIn("VENDOR DRIFT", bg[0]["argv"][1])  # the brief carries the condition
        self.assertEqual(self.w.db("SELECT holder FROM leases WHERE name='escalation/VENDOR_DRIFT'")[0]["holder"][:8],
                         "laptop:e")
        self.assertEqual(self.w.tick().returncode, 0)  # the server's look, past the deadline, while the agent works
        self.assertEqual(self.owner_escalations(), [], "an agent works on it: the owner is not told")
        self.w.backdate("condition", "vendor-drift", 3600, ("next_check",))  # its probe due: the stub's fix is in
        self.sync()
        self.assertEqual(self.condition()["state"], "closed", self.w.diagnosis())
        self.sync()  # the dispatcher: CLEARED, its session stopped
        with open(os.path.join(self.claude, "sessions.json")) as f:
            started = json.load(f)[0]["id"]
        self.assertEqual([c["argv"][1] for c in self.claude_calls("stop")], [started])
        self.assertFalse(glob.glob(self.bin + "-wt-escalation-*"), "the stopped fixer's worktree is removed")
        self.assertNotIn("escalation-vendor-drift", H.sh(self.bin, "git", "branch", "--list"))
        [archived] = glob.glob(os.path.join(self.w.root, "laptop", "state", "fixer-archive", "*", "untracked", "agent-notes.txt"))
        self.assertEqual(open(archived).read(), "uncommitted notes\n", "its uncommitted work is archived first")
        self.assertEqual(self.w.tick().returncode, 0)
        self.assertEqual(self.owner_escalations(), [])
        self.assertEqual(self.inbox_escalations(), [])

    def test_a_failed_launch_tells_the_owner_why_and_spent_tries_hand_it_to_him(self):
        self.w.extra_env["E2E_CLAUDE_BG_FAIL"] = "Error: this workspace is not trusted"
        self.open_past_its_bound()
        self.sync()  # try 1: the launch fails
        self.assertEqual(self.w.tick().returncode, 0)
        told = self.owner_escalations()
        self.assertEqual(len(told), 1, self.w.diagnosis())
        self.assertIn("no agent took it: the launch failed: NOT STARTED: `claude --bg` exit 1: Error: this workspace "
                      "is not trusted", told[0])
        self.sync()  # the event reaches the laptop's inbox
        self.assertTrue(any("the launch failed" in x for x in self.inbox_escalations()), self.inbox_escalations())
        for _ in range(2):  # tries 2 and 3, each once its backoff is over
            self.w.backdate("condition", "vendor-drift", 3 * 3600, ("escalation.next_after",))
            self.sync()
        self.assertEqual(len(self.claude_calls("--bg")), 3)
        self.sync()  # the tries are spent: the owner is handed it, once
        gave_up = [x for x in self.inbox_escalations() if "ESCALATION GAVE UP" in x]
        self.assertEqual(len(gave_up), 1, self.inbox_escalations())
        self.assertIn("WAITING ON YOU", gave_up[0])
        self.sync()
        self.assertEqual(len(self.claude_calls("--bg")), 3, "never past its tries")


class VendorAutoSyncTest(VendorDriftWorld):
    def test_a_drift_is_synced_with_no_agent_while_a_breaking_one_dispatches_one(self):
        """2026-11-27, condition vendor-drift: open ~3 h, two agents each committed the correct sync and ran out of wall
        time. A drift whose scoped tests pass is copied, committed and pushed by the laptop's own script (no condition, no
        agent); one whose tests fail -- the other tests of this class -- is the agent's, with the failure in its text."""
        self.toy_suite(True)
        self.sync()
        self.until_autosync("done")
        self.assertIsNone(self.condition(), "no condition opens for a mechanical sync")
        self.assertEqual(self.claude_calls("--bg"), [], "and no agent is dispatched")
        with open(os.path.join(self.bin, vendor_table())) as f:
            copy = next(x for x in f if x.strip() and not x.startswith("#")).split("\t")[0]
        pushed = H.sh(self.w.root, "git", "--git-dir", os.path.join(self.w.root, "github", "bin.git"), "show", f"main:{copy}")
        self.assertTrue(pushed.endswith("# movies moved ahead"), "the movies file's bytes reached origin/main")
        self.assertIn("Vendor sync", H.sh(self.w.root, "git", "--git-dir", os.path.join(self.w.root, "github", "bin.git"),
                                          "log", "-1", "--format=%s", "main"))
        self.assertEqual(self.w.tick().returncode, 0)
        self.assertEqual(self.owner_escalations(), [])


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
