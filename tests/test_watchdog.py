"""The invariant watchdog (design §10, phase 1 step 2): read-only over today's files; a violation is an INVARIANT
condition (its probe the invariant itself) plus a notification, closed when the invariant holds again; W5 kickstarts a
silent processor, rate-limited."""
import contextlib, io, json, os, sys, tempfile, time, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402


class FakeProcessor:
    """The processor's end of the token socket (TokenDrop's protocol): `?token` asks, anything else is a hand-over."""

    def __init__(self, path, tok):
        import socket, threading
        self.tok, self.path = tok, path
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
            rc = A.cmd_watchdog(None)
        return rc, out.getvalue()

    def test_w5_a_silent_processor_is_a_condition_a_notification_and_one_kick(self):
        open(os.path.join(self.env["AGENTCTL_LAUNCH_AGENTS"], f"{A.SERVICE_LABEL}.plist"), "w").close()
        with open(A.beat_path("processor"), "w") as f:
            f.write(f"{time.time() - 3600:.0f} 1\n")
        rc, out = self.run_wd()
        self.assertEqual(rc, 1)
        self.assertIn("W5: VIOLATED", out)
        self.assertEqual(A.open_conditions(), [], "hysteresis: one missed beat opens nothing (THE ALERT MODEL)")
        self.assertEqual(len(self.kicks), 1)
        self.run_wd()
        self.assertEqual([c["key"] for c in A.open_conditions()], ["invariant-W5"], "the second miss in a row opens it")
        self.assertEqual(len(self.kicks), 1)  # rate-limited
        self.run_wd()
        self.assertEqual(len([e for e in A.inbox_entries() if e.get("kind") == "INVARIANT"]), 1)  # one per spell
        A.write_beat("processor")
        rc, out = self.run_wd()
        self.assertEqual(rc, 0)
        self.assertEqual(A.open_conditions(), [])  # the invariant holds again: closed, its entry acked
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
        st_ = A.Store()
        st_.put_item("change", "old-1", "checking", {"head": "0" * 40, "submitted_at": time.time() - 5 * 3600})
        st_.put_item("change", "old-2", "queued", {"head": "1" * 40, "submitted_at": time.time() - 4 * 3600})
        self.assertIs(A.inv_w1()[0], False, "a green main's W1 is violated, never 0.0 (neither held nor violated)")
        self.run_wd()
        [c] = [c for c in A.open_conditions() if c["key"] == "invariant-W1"]
        self.assertIn("old-1", c["text"])
        it = st_.item("change", "old-1")
        st_.transition("change", "old-1", "landed", it["version"])
        self.run_wd()
        [c] = [c for c in A.open_conditions() if c["key"] == "invariant-W1"]
        self.assertIn("oldest unlanded change old-2 (queued", c["text"])
        self.assertNotIn("old-1", c["text"])
        self.assertEqual(len([e for e in A.inbox_entries() if e.get("kind") == "INVARIANT"]), 1, "one entry per spell")
        it = st_.item("change", "old-2")
        st_.transition("change", "old-2", "landed", it["version"])
        self.run_wd()
        self.assertEqual([c for c in A.open_conditions() if c["key"] == "invariant-W1"], [])

    def test_w1_from_the_store_and_w2_from_the_store(self):
        st_ = A.Store()  # W1: the one queue, the store's open change items, by their submission time
        st_.put_item("change", "e1", "waiting", {"head": "0" * 40, "submitted_at": time.time() - 3 * 3600})
        ok, detail = A.inv_w1()
        self.assertFalse(ok)
        self.assertIn("e1 (waiting: waiting for main to turn green) is 3 h", detail)
        it = st_.item("change", "e1")
        st_.transition("change", "e1", "landed", it["version"])
        self.assertTrue(A.inv_w1()[0])  # W2 (step 10): a red with no live lease, open offer, fix or block -- read from the store
        st_.put_item("red", "k9", "leased", {"key": "k9", "workflow": "Main", "last_run": {"number": 9}, "runs": ["9"]})
        self.assertTrue(A.inv_w2()[0])  # first seen now
        st = A.watchdog_state()
        A.write_json(A.state_root("watchdog.json"), {**st, "w2_since": time.time() - 11 * 60})
        ok, detail = A.inv_w2()
        self.assertFalse(ok)
        self.assertIn("Main #9", detail)
        st_.acquire("red/k9", "laptop:claim:x@/w", 600, 3600)
        self.assertTrue(A.inv_w2()[0])
        self.assertIsNone(A.watchdog_state().get("w2_since"))

    def test_maintain_and_the_watchdog_watch_each_other(self):
        open(os.path.join(self.env["AGENTCTL_LAUNCH_AGENTS"], "net.bitcashier.agentctl-maintain.plist"), "w").close()
        with open(A.beat_path("maintain"), "w") as f:
            f.write(f"{time.time() - 3600:.0f} 1\n")
        self.assertFalse(A.inv_wm()[0])
        A.write_beat("maintain")
        self.assertTrue(A.inv_wm()[0])


if __name__ == "__main__":
    unittest.main()
