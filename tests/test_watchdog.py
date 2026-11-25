"""The invariant watchdog (design §10, phase 1 step 2): read-only over today's files; a violation is an INVARIANT
condition (its probe the invariant itself) plus a notification, closed when the invariant holds again; W5 kickstarts a
silent processor, rate-limited."""
import contextlib, io, json, os, sys, tempfile, time, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402
from fake_clock import FakeClock  # noqa: E402


class FakeProcessor:
    """The processor's end of the token socket (TokenDrop's protocol): `?token` asks, anything else is a hand-over."""

    def __init__(self, path, tok):
        import socket, threading
        self.tok, self.path = tok, path
        self.handed = threading.Event()  # a hand-over is taken on the serving thread, after the sender has returned
        if os.path.exists(path):
            os.unlink(path)
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(path)
        self.sock.listen(4)
        threading.Thread(target=self.serve, daemon=True).start()

    def serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            with conn:
                got = conn.recv(4096).decode().strip()
                if got == A.TOKEN_REQUEST.decode().strip():
                    conn.sendall(self.tok.encode())
                elif got:
                    self.tok = got
                    self.handed.set()

    def close(self):
        self.sock.close()


class WatchdogTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.env = {"AGENTCTL_STATE_ROOT": self.root, "AGENTCTL_RECOVERY_DIR": os.path.join(self.root, "rec"),
                    "AGENTCTL_INBOX_DIR": os.path.join(self.root, "inbox"),
                    "AGENTCTL_STATE_FILE": os.path.join(self.root, "state.json"),
                    "AGENTCTL_LAUNCH_AGENTS": os.path.join(self.root, "agents"),
                    "AGENTCTL_MOVIES_REPO": os.path.join(self.root, "movies"),
                    "AGENTCTL_NOTIFY": "true"}
        self.saved = {k: os.environ.get(k) for k in self.env}
        os.environ.update(self.env)
        os.makedirs(self.env["AGENTCTL_LAUNCH_AGENTS"])
        import subprocess
        subprocess.run(["git", "init", "-q", self.env["AGENTCTL_MOVIES_REPO"]], check=True)
        self.kicks = []
        self.saved_lc, self.saved_wd = A.launchctl, A.WAITING_DIR
        A.launchctl = lambda *a: self.kicks.append(a)
        A.WAITING_DIR = os.path.join(self.root, "waiting")

    def tearDown(self):
        A.launchctl, A.WAITING_DIR = self.saved_lc, self.saved_wd
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def run_wd(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            rc = A.cmd_watchdog(None, FakeClock(), A.CurrentStore(A.process_store))
        return rc, out.getvalue()

    def test_w5_a_silent_processor_is_a_condition_a_notification_and_one_kick(self):
        open(os.path.join(self.env["AGENTCTL_LAUNCH_AGENTS"], f"{A.SERVICE_LABEL}.plist"), "w").close()
        with open(A.beat_path("processor"), "w") as f:
            f.write(f"{time.time() - 3600:.0f} 1\n")
        rc, out = self.run_wd()
        self.assertEqual(rc, 1)
        self.assertIn("W5: VIOLATED", out)
        self.assertEqual(A.open_conditions(A.CurrentStore(A.process_store)), [], "hysteresis: one missed beat opens nothing (THE ALERT MODEL)")
        self.assertEqual(len(self.kicks), 1)
        self.run_wd()
        self.assertEqual([c["key"] for c in A.open_conditions(A.CurrentStore(A.process_store))], ["invariant-W5"], "the second miss in a row opens it")
        self.assertEqual(len(self.kicks), 1)  # rate-limited
        self.run_wd()
        self.assertEqual(len([e for e in A.inbox_entries() if e.get("kind") == "INVARIANT"]), 1)  # one per spell
        A.write_beat("processor")
        rc, out = self.run_wd()
        self.assertEqual(rc, 0)
        self.assertEqual(A.open_conditions(A.CurrentStore(A.process_store)), [])  # the invariant holds again: closed, its entry acked
        self.assertTrue(all(e["read"] for e in A.inbox_entries() if e.get("kind") == "INVARIANT"))

    def test_w5_a_kick_hands_the_old_processors_token_to_its_successor(self):
        """2026-11-15 16:58: W5 kickstarted the processor (launchd's last exit 143) and the new one held no store
        token: nothing landed and the gate read UNREAD until 18:36, when the owner handed it over by hand. The kick
        now asks the old processor for its token first and hands it to the one launchd starts."""
        open(os.path.join(self.env["AGENTCTL_LAUNCH_AGENTS"], f"{A.SERVICE_LABEL}.plist"), "w").close()
        sock_dir = tempfile.mkdtemp(dir=os.environ.get("AGENTCTL_TEST_RUN_SOCK") or "/tmp", prefix="s-")
        os.environ["AGENTCTL_SOCK_DIR"] = sock_dir
        self.addCleanup(os.environ.pop, "AGENTCTL_SOCK_DIR", None)
        path = A.processor_sock()
        import subprocess
        gone = subprocess.Popen(["true"])
        gone.wait()
        with open(A.beat_path("processor"), "w") as f:
            f.write(f"{time.time() - 3600:.0f} {gone.pid}\n")
        procs = [FakeProcessor(path, "tok-0123456789abcdef")]
        self.addCleanup(lambda: procs[-1].close())

        def kick(*a):
            self.kicks.append(a)
            procs[0].close()
            procs.append(FakeProcessor(path, ""))  # launchd's fresh process: no token
        A.launchctl = kick
        self.run_wd()
        self.assertEqual(len(self.kicks), 1)
        # hand_token returns once its bytes are sent; the new processor reads them on its own thread (python:3.14,
        # full suite, 2026-11-18: read before that thread ran, '' != the token)
        self.assertTrue(procs[-1].handed.wait(10), "no hand-over reached the new processor")
        self.assertEqual(procs[-1].tok, "tok-0123456789abcdef")

    def test_w5_counts_a_processors_silence_over_awake_time_only(self):
        """16:42-16:58 the laptop slept; at the 16:58:26 dark wake the watchdog found the processor's beat 16 min old
        and kicked a processor that had only been asleep. Silence the watchdog slept through is no silence."""
        open(os.path.join(self.env["AGENTCTL_LAUNCH_AGENTS"], f"{A.SERVICE_LABEL}.plist"), "w").close()
        with open(A.beat_path("watchdog"), "w") as f:
            f.write(f"{time.time() - 16 * 60:.0f} 1\n")
        with open(A.beat_path("processor"), "w") as f:
            f.write(f"{time.time() - 16 * 60:.0f} 1\n")
        self.run_wd()
        self.assertEqual(self.kicks, [], "the first pass after a wake")
        self.run_wd()
        self.assertEqual(self.kicks, [], "awake for seconds: the processor has had no time to beat")
        st = A.watchdog_state()
        A.write_json(A.state_root("watchdog.json"), {**st, "awake_since": time.time() - 3 * A.W5_MAX - 60})
        self.run_wd()
        self.assertEqual(len(self.kicks), 1, "silent for 3 x W5_MAX awake: kicked")

    def test_an_open_invariant_condition_says_what_violates_it_now_not_what_did_when_it_opened(self):
        """2026-11-16: invariant-W1.6 opened at 05:10 naming nongate; nongate landed at 06:06, but W1 stayed violated
        by workermem (10 h old) and the condition, its escalations and the owner's ESCALATED line named the landed
        nongate until noon. With main green W1 returned 0.0 (`age < lim or (red_since and ...)`), which the watchdog
        neither refreshed nor closed on (`r.ok is False` / `r.ok`). Each violated pass re-describes the open
        condition (one inbox entry, one notification per spell still); the invariant holding closes it."""
        os.environ["AGENTCTL_UNHELD_GRACE"] = "0"  # old-2's lack of a hold is stuck at once (UNHELD_GRACE passed)
        self.addCleanup(os.environ.pop, "AGENTCTL_UNHELD_GRACE", None)
        st_ = A.process_store()
        st_.put_item("change", "old-1", "checking", {"head": "0" * 40, "submitted_at": time.time() - 5 * 3600})
        st_.put_item("change", "old-2", "queued", {"head": "1" * 40, "submitted_at": time.time() - 4 * 3600})
        self.assertIs(A.inv_w1(FakeClock(), A.CurrentStore(A.process_store))[0], False, "a green main's W1 is violated, never 0.0 (neither held nor violated)")
        self.run_wd()
        [c] = [c for c in A.open_conditions(A.CurrentStore(A.process_store)) if c["key"] == "invariant-W1"]
        self.assertIn("old-1", c["text"])
        it = st_.item("change", "old-1")
        st_.transition("change", "old-1", "landed", it["version"])
        self.run_wd()
        [c] = [c for c in A.open_conditions(A.CurrentStore(A.process_store)) if c["key"] == "invariant-W1"]
        self.assertIn("unlanded change old-2 (queued", c["text"])
        self.assertNotIn("old-1", c["text"])
        self.assertEqual(len([e for e in A.inbox_entries() if e.get("kind") == "INVARIANT"]), 1, "one entry per spell")
        it = st_.item("change", "old-2")
        st_.transition("change", "old-2", "landed", it["version"])
        self.run_wd()
        self.assertEqual([c for c in A.open_conditions(A.CurrentStore(A.process_store)) if c["key"] == "invariant-W1"], [])

    def test_w1_reads_the_gate_from_its_snapshot_and_names_the_red_row_holding_a_waiting_change(self):
        """2026-11-22: the gate was red on Identity takes #349 (Main green) and W1 said "waiting for green while main
        is green": it read the gate from the published summary line, unread past 180 s on a starved laptop."""
        st_ = A.process_store()
        st_.put_item("change", "impact", "waiting", {"head": "0" * 40, "submitted_at": time.time() - 4 * 3600})
        def snap(takes_since):
            iso = lambda t: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))
            return {"ci": {"repo": A.SHORT["movies"], "at": time.time(), "gate": [
                {"name": "Main", "decided": {"conclusion": "success", "number": 2816}, "since": None},
                {"name": "Identity takes", "decided": {"conclusion": "failure", "number": 349},
                 "since": iso(time.time() - takes_since)}]}}
        real = A.state_read
        self.addCleanup(setattr, A, "state_read", real)
        A.state_read = lambda: snap(7 * 3600)
        ok, detail = A.inv_w1(FakeClock(), A.CurrentStore(A.process_store))
        self.assertTrue(ok, "a change waiting behind a 7-h red is within the red's own start + its bound: " + detail)
        A.state_read = lambda: snap(3600)  # red for 1 h, the change 4 h old: past red + bound, named with its row
        ok, detail = A.inv_w1(FakeClock(), A.CurrentStore(A.process_store))
        self.assertFalse(ok)
        self.assertNotIn("main is green", detail)
        self.assertIn("held by the red gate: Identity takes #349 since", detail)
        A.state_read = lambda: {}
        ok, detail = A.inv_w1(FakeClock(), A.CurrentStore(A.process_store))
        self.assertIn("the gate unread", detail)

    def test_a_waiting_change_its_batch_carries_is_the_batchs_within_the_batchs_deadline(self):
        """2026-11-22 09:56Z: the gate went green and batch #94 left blocked-red within a tick, checking
        impact-same-file and film-codec on the new main; W1 said of both "waiting for green while the gate is green"."""
        st_ = A.process_store()
        st_.put_item("change", "impact", "waiting", {"head": "0" * 40, "submitted_at": time.time() - 9 * 3600})
        st_.put_item("batch", "movies#94", "checking", {"members": ["impact", "codec"], "tip": "2d1beab02b"})
        ok, detail = A.inv_w1(FakeClock(), A.CurrentStore(A.process_store))
        self.assertTrue(ok, detail)
        b = st_.item("batch", "movies#94")  # its batch past ITS deadline: named, never "waiting for green"
        st_.transition("batch", "movies#94", "checking", b["version"], deadline_s=-60)
        ok, detail = A.inv_w1(FakeClock(), A.CurrentStore(A.process_store))
        self.assertFalse(ok)
        self.assertIn("carried by batch movies#94 (checking, past its deadline)", detail)

    def test_a_checking_change_its_backlog_batch_carries_is_the_batchs_never_no_live_checker(self):
        """2026-11-23 03:2xZ: ci-row-scope-py, 3 h old, sat `checking` as a member of backlog batch #107 (03:02-03:48Z),
        the batch's executor its checker; W1 said "its checks run with no live checker" (no lease of its own) and
        `land status` "no remote check recorded" -- read as a dead check. Within the batch's deadline it is the batch's;
        past it, named by its batch."""
        st_ = A.process_store()
        st_.put_item("change", "scope", "checking", {"head": "0" * 40, "submitted_at": time.time() - 3 * 3600})
        st_.put_item("batch", "movies#107", "checking", {"members": ["scope", "other"], "tip": "bdfcdb5142"})
        ok, detail = A.inv_w1(FakeClock(), A.CurrentStore(A.process_store))
        self.assertTrue(ok, detail)
        b = st_.item("batch", "movies#107")
        st_.transition("batch", "movies#107", "checking", b["version"], deadline_s=-60)
        ok, detail = A.inv_w1(FakeClock(), A.CurrentStore(A.process_store))
        self.assertFalse(ok)
        self.assertIn("carried by batch movies#107 (checking, past its deadline)", detail)
        self.assertNotIn("no live checker", detail)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            A.land_status_entry(st_, "scope", time.time())
        self.assertIn("check: in batch movies#107 (checking): the batch's check is its check", out.getvalue())

    def test_a_change_just_checked_with_no_hold_yet_is_between_ticks_never_unheld(self):
        """2026-11-23 04:19Z/04:44Z: py-restore-corpus, 4 h old, was checked and pushed by the next tick (40 s, 27 s
        later); W1 read it in between as "checked: nothing holds it, yet it has not pushed". No hold is stuck only once
        it outlives the grace a tick needs to push it or record what holds it (UNHELD_GRACE); then it is named."""
        st_ = A.process_store()
        st_.put_item("change", "corpus", "checked", {"head": "0" * 40, "submitted_at": time.time() - 4 * 3600})
        ok, detail = A.inv_w1(FakeClock(), A.CurrentStore(A.process_store))
        self.assertTrue(ok, detail)
        ok, detail = A.inv_w1(FakeClock(time.time() + A.UNHELD_GRACE + 60), A.CurrentStore(A.process_store))
        self.assertFalse(ok)
        self.assertIn("corpus (checked: nothing holds it, yet it has not pushed", detail)

    def test_a_lone_checking_change_with_no_live_checker_is_still_named(self):
        st_ = A.process_store()
        st_.put_item("change", "lone", "checking", {"head": "0" * 40, "submitted_at": time.time() - 3 * 3600})
        ok, detail = A.inv_w1(FakeClock(), A.CurrentStore(A.process_store))
        self.assertFalse(ok)
        self.assertIn("its checks run with no live checker", detail)

    def test_w1_from_the_store_and_w2_from_the_store(self):
        st_ = A.process_store()  # W1: the one queue, the store's open change items, by their submission time
        st_.put_item("change", "e1", "waiting", {"head": "0" * 40, "submitted_at": time.time() - 3 * 3600})
        ok, detail = A.inv_w1(FakeClock(), A.CurrentStore(A.process_store))
        self.assertFalse(ok)
        self.assertIn("e1 (waiting: waiting for green", detail)
        it = st_.item("change", "e1")
        st_.transition("change", "e1", "landed", it["version"])
        self.assertTrue(A.inv_w1(FakeClock(), A.CurrentStore(A.process_store))[0])  # W2 (step 10): a red with no live lease, open offer, fix or block -- read from the store
        st_.put_item("red", "k9", "leased", {"key": "k9", "workflow": "Main", "last_run": {"number": 9}, "runs": ["9"]})
        self.assertTrue(A.inv_w2(FakeClock(), A.CurrentStore(A.process_store))[0])  # first seen now
        st = A.watchdog_state()
        A.write_json(A.state_root("watchdog.json"), {**st, "w2_since": time.time() - 11 * 60})
        ok, detail = A.inv_w2(FakeClock(), A.CurrentStore(A.process_store))
        self.assertFalse(ok)
        self.assertIn("Main #9", detail)
        st_.acquire("red/k9", "laptop:claim:x@/w", 600, 3600)
        self.assertTrue(A.inv_w2(FakeClock(), A.CurrentStore(A.process_store))[0])
        self.assertIsNone(A.watchdog_state().get("w2_since"))

    def test_maintain_and_the_watchdog_watch_each_other(self):
        open(os.path.join(self.env["AGENTCTL_LAUNCH_AGENTS"], "net.bitcashier.agentctl-maintain.plist"), "w").close()
        with open(A.beat_path("maintain"), "w") as f:
            f.write(f"{time.time() - 3600:.0f} 1\n")
        self.assertFalse(A.inv_wm(FakeClock(), A.CurrentStore(A.process_store))[0])
        A.write_beat("maintain")
        self.assertTrue(A.inv_wm(FakeClock(), A.CurrentStore(A.process_store))[0])


class FileTableTest(unittest.TestCase):
    """WF: the Mac's system-wide file table (kern.num_files of kern.maxfiles) under WF_PCT, its top holder named past
    it (2026-11-20: Rancher Desktop's VM held ~200k virtiofs descriptors, the table overflowed, ENFILE everywhere)."""
    LSOF = "p1\ncshell\nfcwd\nf1\np39093\nccom.apple.Virtualization.VirtualMachine\n" + "f7\n" * 5 + "p7\ncjava\nf1\nf2\n"

    def test_under_the_bound_it_holds_and_says_how_full(self):
        ok, detail = A.inv_wf(FakeClock(), A.CurrentStore(A.process_store), reading=(217_000, 491_520), lsof_out="")
        self.assertTrue(ok)
        self.assertIn("217000 of 491520 open (44%)", detail)

    def test_past_the_bound_it_names_the_top_holders(self):
        ok, detail = A.inv_wf(FakeClock(), A.CurrentStore(A.process_store), reading=(400_000, 491_520), lsof_out=self.LSOF)
        self.assertFalse(ok)
        self.assertIn("400000 of 491520 open (81%", detail)
        self.assertIn("top: pid 39093 com.apple.Virtualization.VirtualMachine (5)", detail)
        self.assertLess(detail.index("39093"), detail.index("pid 7 java"))

    def test_the_vm_on_top_says_how_to_release_its_cache(self):
        _ok, detail = A.inv_wf(FakeClock(), A.CurrentStore(A.process_store), reading=(400_000, 491_520), lsof_out=self.LSOF)
        self.assertIn("rdctl shell -- sudo sh -c 'sync; echo 2 > /proc/sys/vm/drop_caches'", detail)
        _ok, other = A.inv_wf(FakeClock(), A.CurrentStore(A.process_store), reading=(400_000, 491_520), lsof_out="p7\ncjava\nf1\n")
        self.assertNotIn("drop_caches", other)

    def test_the_bound_is_configurable_and_no_reading_is_no_violation(self):
        os.environ["AGENTCTL_WF_PCT"] = "40"
        try:
            self.assertFalse(A.inv_wf(FakeClock(), A.CurrentStore(A.process_store), reading=(217_000, 491_520), lsof_out="")[0])
        finally:
            del os.environ["AGENTCTL_WF_PCT"]
        self.assertEqual((True, "no kern.num_files here"), A.inv_wf(FakeClock(), A.CurrentStore(A.process_store), reading=None))

    def test_it_is_a_watchdog_invariant(self):
        self.assertIn("WF", A.INVARIANTS)


class TableJudgedInvariantsTest(unittest.TestCase):
    """W1 AND WR READ THE TABLES (2026-11-20: W1 fired 18 times in three weeks, 13 on a change whose checks simply ran
    past 2 h with a live checker): a change is judged by its state's table -- a live checker, a hold within its kind's
    bound -- and violates only outside it, or past W1_HARD_FACTOR x the bound whatever it says."""

    def setUp(self):
        self.st = A.process_store()
        for it in self.st.items("change"):
            if not A.machine_state("change", it["state"]).terminal:
                self.st.transition("change", it["id"], "cancelled", it["version"])

    def checker(self, eid):
        self.st.acquire(f"check/{eid}", f"laptop:check:{eid}", 600, 4 * 3600, progress="x")

    def test_a_long_check_with_a_live_checker_is_no_w1_violation_until_the_hard_bound(self):
        self.st.put_item("change", "long-1", "checking", {"head": "0" * 40, "submitted_at": time.time() - 3 * 3600})
        self.checker("long-1")
        ok, why = A.inv_w1(FakeClock(), A.CurrentStore(A.process_store))
        self.assertTrue(ok, why)
        it = self.st.item("change", "long-1")
        self.st.update_item("change", "long-1", it["version"], {"submitted_at": time.time() - 7 * 3600})
        ok, why = A.inv_w1(FakeClock(), A.CurrentStore(A.process_store))
        self.assertFalse(ok, why)
        self.assertIn("past 3 x its bound", why)

    def test_a_check_with_no_live_checker_violates_w1(self):
        self.st.put_item("change", "dead-1", "checking", {"head": "1" * 40, "submitted_at": time.time() - 3 * 3600})
        ok, why = A.inv_w1(FakeClock(), A.CurrentStore(A.process_store))
        self.assertFalse(ok, why)
        self.assertIn("no live checker", why)

    def test_a_held_change_within_its_holds_bound_is_no_w1_violation(self):
        self.st.put_item("change", "older", "checking", {"head": "2" * 40, "submitted_at": time.time() - 60})
        self.checker("older")
        self.st.put_item("change", "held-1", "queued", {
            "head": "3" * 40, "submitted_at": time.time() - 3 * 3600,
            "hold": {"kind": "overlap", "why": "its check waits for older", "blocker": "older", "since": time.time() - 600}})
        it = self.st.item("change", "held-1")
        self.st.transition("change", "held-1", "queued", it["version"])  # entered now: the 6-h cap is not hit
        ok, why = A.inv_w1(FakeClock(), A.CurrentStore(A.process_store))
        self.assertTrue(ok, why)

    def test_wr_a_red_fix_whose_own_check_runs_is_no_wait_but_one_held_behind_work_is(self):
        self.st.put_item("red", "r7", "offered", {"key": "r7", "runs": ["7"], "gate": True})
        self.st.put_item("change", "fix-run", "checking", {"fixes": ["7"], "submitted_at": time.time() - 53 * 60})
        self.checker("fix-run")
        ok, why = A.inv_wr(FakeClock(), st=self.st, reds={"7"})
        self.assertTrue(ok, why)
        self.st.put_item("change", "fix-held", "queued", {
            "fixes": ["7"], "submitted_at": time.time() - 60,
            "hold": {"kind": "overlap", "why": "its check waits for older", "blocker": "older", "since": time.time()}})
        ok, why = A.inv_wr(FakeClock(), st=self.st, reds={"7"})
        self.assertFalse(ok, why)
        self.assertIn("fix-held", why)
        self.assertIn("held behind other work", why)
        self.assertNotIn("fix-run", why)


class RunawayProcessTest(unittest.TestCase):
    """WP / WT (2026-11-22): fseventsd at 21.5 GB and over 100% CPU for days, mds_stores hot beside it, fed by 174
    leaked test roots -- and no invariant said so. WP watches every process from samples kept between passes."""

    def setUp(self):
        import tempfile
        self.d = tempfile.mkdtemp()
        self.state = os.path.join(self.d, "procwatch.json")

    def ps(self, *rows):
        """ps's lines of (pid, ppid, rss KB, %cpu, command) rows."""
        return "\n".join(f"{pid} {ppid} {rss_kb} {cpu} {name}" for pid, ppid, rss_kb, cpu, name in rows)

    def run_passes(self, samples, pid=322, name="/usr/sbin/fseventsd", ppid=1, cpu=40.0, **kw):
        """One watchdog pass a minute over `samples` [(t, rss GB)]: [(t, ok, detail)]."""
        out = []
        for t, gb in samples:
            ok, detail = A.inv_wp(self.ps((pid, ppid, int(gb * 1024 * 1024), cpu, name)), now_=float(t),
                                  state_path=self.state, **kw)
            out.append((t, ok, detail))
        return out

    @staticmethod
    def climb(start_gb, end_gb, minutes, t0=0.0):
        """A steady climb, sampled every minute."""
        return [(t0 + 60 * m, start_gb + (end_gb - start_gb) * m / minutes) for m in range(minutes + 1)]

    def test_a_daemon_growing_for_hours_past_its_rss_bound_is_named_with_the_owners_fix(self):
        """The case WP was built for: fseventsd, no -Xmx, climbing to 21.5 GB."""
        passes = self.run_passes(self.climb(20.0, 21.5, 60))
        early = [ok for t, ok, _ in passes if t < A.WP_PERSIST_MIN * 60]
        self.assertTrue(all(early), "growth not yet held for WP_PERSIST_MIN: no alert")
        t, ok, detail = passes[-1]
        self.assertFalse(ok, "an hour of growth past the bound")
        self.assertIn("fseventsd pid 322: RSS 21.5 GB (from 20.", detail)
        self.assertIn("growing for 60 min", detail)
        self.assertIn("`sudo killall fseventsd`", detail)
        self.assertIn("Spotlight", detail)

    def test_one_jump_between_two_samples_is_no_runaway(self):
        passes = self.run_passes([(0.0, 1.0)] + [(60.0 * m, 6.0) for m in range(1, 61)], name="mds_stores")
        self.assertTrue(all(ok for _, ok, _ in passes), [d for _, ok, d in passes if not ok][:1])

    def test_a_jvm_warming_toward_its_own_xmx_is_no_runaway(self):
        """2026-11-22 16:15Z, a false alarm: a bitcashier fail-before sbt (-Xms1500M -Xmx6000M), pid 6811, went from
        0.5 GB to 4.6 GB in a minute and on toward its -Xmx: a JVM's normal warm-up."""
        args = "/usr/bin/java -Xms1500M -Xmx6000M -Xss4M -jar sbt-launch.jar testOnly"
        samples = [(0.0, 0.5), (60.0, 4.6)] + self.climb(4.6, 6.8, 60, t0=60.0)[1:]
        passes = self.run_passes(samples, pid=6811, name="java", cpu=60.0, args_of=lambda pid: args)
        self.assertTrue(all(ok for _, ok, _ in passes), [d for _, ok, d in passes if not ok][:1])

    def test_a_jvm_past_its_xmx_and_margin_still_climbing_is_a_runaway(self):
        args = "/usr/bin/java -Xmx2g -cp x Main"
        passes = self.run_passes(self.climb(2.0, 6.0, 40), pid=501, name="java", cpu=30.0, args_of=lambda pid: args,
                                 cwd_of=lambda pid: os.path.join(A.HOME, "projects", "movies-wt-x"))
        t, ok, detail = passes[-1]
        self.assertFalse(ok)
        self.assertIn("java pid 501: RSS 6.0 GB", detail)
        self.assertIn("past its -Xmx 2.0 GB", detail)
        self.assertIn("ours: it runs in ~/projects/movies-wt-x", detail)

    def mongod_conf(self, body):
        conf = os.path.join(self.d, "mongod.conf")
        with open(conf, "w") as f:
            f.write(body)
        return f"/opt/homebrew/opt/mongodb-community/bin/mongod --config {conf}"

    def test_a_mongod_warming_its_default_wiredtiger_cache_is_no_runaway(self):
        """2026-11-22 17:10Z, a false alarm: "mongod pid 65521: RSS 9.2 GB (from 9.1 GB at 16:25Z), past 4 GB, growing
        for 45 min" -- the local prod mirror, no cacheSizeGB set: WiredTiger's default cache is 50% of (RAM - 1 GB),
        ~68 GB on this 137 GB Mac."""
        args = self.mongod_conf("net:\n  port: 28017\nstorage:\n  dbPath: /opt/homebrew/var/mongodb\n")
        passes = self.run_passes(self.climb(9.1, 9.2, 45), pid=65521, name="mongod", cpu=5.0,
                                 args_of=lambda pid: args, ram=137.0)
        self.assertTrue(all(ok for _, ok, _ in passes), [d for _, ok, d in passes if not ok][:1])
        self.assertEqual(int(A.mongod_cache_kb(args, 137.0) / 1024 / 1024), 68)

    def test_a_mongod_past_its_cache_and_margin_still_climbing_is_a_runaway(self):
        args = self.mongod_conf("storage:\n  wiredTiger:\n    engineConfig:\n      cacheSizeGB: 2\n")
        passes = self.run_passes(self.climb(3.0, 8.0, 40), pid=700, name="mongod", cpu=5.0,
                                 args_of=lambda pid: args, ram=137.0)
        self.assertFalse(passes[-1][1])
        self.assertIn("mongod pid 700: RSS 8.0 GB", passes[-1][2])
        self.assertIn("past its WiredTiger cache 2.0 GB + margin", passes[-1][2])

    def lima_vm(self, memory):
        conf = os.path.join(self.d, "Library", "Application Support", "rancher-desktop", "lima", "0", "lima.yaml")
        os.makedirs(os.path.dirname(conf))
        with open(conf, "w") as f:
            f.write(f"cpus: 2\nmemory: {memory}\n")

    def test_a_vm_filling_its_configured_guest_memory_is_no_runaway(self):
        """2026-11-22, a false alarm: "com.apple.Virtualization.VirtualMachine pid 39093: RSS 4.7 GB (from 3.5 GB at
        16:25Z), past 4 GB, growing for 68 min" -- Rancher Desktop's Linux VM (6 GiB guest) touching its pages."""
        self.lima_vm(6442450944)
        passes = self.run_passes(self.climb(3.5, 4.7, 68), pid=39093, name="com.apple.Virtualization.VirtualMachine",
                                 cpu=20.0, home=self.d)
        self.assertTrue(all(ok for _, ok, _ in passes), [d for _, ok, d in passes if not ok][:1])

    def test_a_vm_past_its_guest_memory_and_margin_still_climbing_is_a_runaway(self):
        self.lima_vm('"2GiB"')
        passes = self.run_passes(self.climb(3.0, 6.0, 40), pid=39093, name="com.apple.Virtualization.VirtualMachine",
                                 cpu=20.0, home=self.d)
        self.assertFalse(passes[-1][1])
        self.assertIn("past its guest memory 2.0 GB + margin", passes[-1][2])

    def test_idea_is_held_to_its_own_8_gb_limit_and_others_to_4(self):
        """2026-11-25: IntelliJ's `idea` JVM sits at 4.1-4.2 GB and WP escalated it; WP_RSS_BY_NAME gives it 8 GB."""
        idea = lambda a, b: self.run_passes(self.climb(a, b, 60), pid=1246, name="idea", cpu=5.0)  # noqa: E731
        self.assertTrue(idea(4.0, 5.0)[-1][1], "idea at 5 GB, growing: under its 8 GB")
        last = idea(8.0, 9.0)[-1]
        self.assertFalse(last[1], "idea at 9 GB, growing: past 8 GB")
        self.assertIn("past 8 GB", last[2])
        self.assertFalse(self.run_passes(self.climb(4.0, 5.0, 60), pid=77, name="other", cpu=5.0)[-1][1])

    def test_each_known_kind_reads_its_own_ceiling_and_an_unknown_one_has_none(self):
        self.assertEqual(A.proc_ceiling("java", "java -Xmx512m x", 16.0, self.d), (512 * 1024, "its -Xmx 0.5 GB"))
        self.assertEqual(A.proc_ceiling("mongod", "mongod --wiredTigerCacheSizeGB 3", 16.0, self.d)[0], 3 * 1024 ** 2)
        self.assertIsNone(A.proc_ceiling("com.apple.Virtualization.VirtualMachine", "", 16.0, self.d), "no VM config")
        self.assertIsNone(A.proc_ceiling("fseventsd", "", 16.0, self.d))

    def test_a_big_server_creeping_by_a_sliver_is_no_runaway(self):
        """300 MB over 45 min on a 9 GB process is no trend: growth must reach 5% or 500 MB over the window."""
        passes = self.run_passes(self.climb(9.1, 9.4, 45), pid=900, name="postgres", cpu=5.0)
        self.assertTrue(all(ok for _, ok, _ in passes), [d for _, ok, d in passes if not ok][:1])

    def test_a_process_within_its_governor_tickets_charge_is_no_runaway(self):
        """sbt-warm, gov-run, fail-before: the governor admitted them with the memory they were charged."""
        tickets = [{"pid": 400, "mem": 12, "cores": 4, "label": "fail-before"}]
        passes = self.run_passes(self.climb(4.0, 11.0, 60), pid=401, ppid=400, name="node", cpu=350.0,
                                 tickets=tickets)
        self.assertTrue(all(ok for _, ok, _ in passes), [d for _, ok, d in passes if not ok][:1])
        passes = self.run_passes(self.climb(11.0, 16.0, 40, t0=3660.0), pid=401, ppid=400, name="node",
                                 cpu=50.0, tickets=tickets)
        self.assertFalse(passes[-1][1], "past its charge and climbing: a runaway after all")

    def test_a_big_process_that_holds_steady_is_no_runaway(self):
        gb = 1024 * 1024
        for t, kb in ((0.0, 8 * gb), (600.0, 8 * gb + 300), (1200.0, 8 * gb + 900), (2400.0, 8 * gb + 1500)):
            ok, detail = A.inv_wp(self.ps((719, 1, kb, 9.0, "mds_stores")), now_=t, state_path=self.state)
            self.assertTrue(ok, detail)

    def test_a_process_hot_for_twenty_minutes_is_a_runaway_and_a_cool_one_is_not(self):
        rows = lambda cpu: self.ps((88, 1, 50_000, cpu, "mds_stores"))  # noqa: E731
        for t in (0, 600, 1199):
            ok, _ = A.inv_wp(rows(130.0), now_=float(t), state_path=self.state)
            self.assertTrue(ok, f"hot for {t} s only")
        ok, detail = A.inv_wp(rows(130.0), now_=1200.0, state_path=self.state)
        self.assertFalse(ok)
        self.assertIn("mds_stores pid 88: CPU 130%, over 90% for 20 min", detail)
        ok, _ = A.inv_wp(rows(5.0), now_=1300.0, state_path=self.state)
        self.assertTrue(ok, "it cooled: the episode ended")

    def test_one_of_ours_is_named_by_the_worktree_it_runs_in(self):
        for t in (0.0, 1300.0):
            ok, detail = A.inv_wp(self.ps((501, 1, 90_000, 99.0, "java")), now_=t, state_path=self.state,
                                  cwd_of=lambda pid: os.path.join(A.HOME, "projects", "movies-wt-x"),
                                  args_of=lambda pid: "java -cp x Main")
        self.assertFalse(ok)
        self.assertIn("ours: it runs in ~/projects/movies-wt-x", detail)
        self.assertNotIn("killall", detail)

    def test_wt_counts_the_stale_test_roots_and_the_worktrees_holding_build_output(self):
        import tempfile, time as _t
        tmp, projects = tempfile.mkdtemp(), tempfile.mkdtemp()
        old = _t.time() - 3 * A.TEST_TMP_MAX_AGE
        for n in range(A.WT_STALE_MAX + 1):
            p = os.path.join(tmp, f"agentctl-t-{n}")
            os.makedirs(p)
            os.utime(p, (old, old))
        os.makedirs(os.path.join(projects, "movies-wt-a", "target"))
        os.makedirs(os.path.join(projects, "movies-wt-b"))
        ok, detail = A.inv_wt(tmp_root=tmp, projects=projects)
        self.assertFalse(ok)
        self.assertIn(f"{A.WT_STALE_MAX + 1} test roots", detail)
        self.assertIn("1 worktrees hold build output", detail)
        self.assertIn("WP", A.INVARIANTS)
        self.assertIn("WT", A.INVARIANTS)


if __name__ == "__main__":
    unittest.main()
