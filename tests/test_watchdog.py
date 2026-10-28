"""The invariant watchdog (design §10, phase 1 step 2): read-only over today's files; a violation is an INVARIANT
condition (its probe the invariant itself) plus a notification, closed when the invariant holds again; W5 kickstarts a
silent processor, rate-limited."""
import contextlib, io, json, os, sys, tempfile, time, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402


class WatchdogTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.env = {"AGENTCTL_STATE_ROOT": self.root, "AGENTCTL_RECOVERY_DIR": os.path.join(self.root, "rec"),
                    "AGENTCTL_INBOX_DIR": os.path.join(self.root, "inbox"),
                    "AGENTCTL_GATE_SUMMARY": os.path.join(self.root, "gate-summary.txt"),
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
        self.assertEqual([c["key"] for c in A.open_conditions()], ["invariant-W5"])
        self.assertEqual(len(self.kicks), 1)
        self.run_wd()
        self.assertEqual(len(self.kicks), 1)  # rate-limited
        self.assertEqual(len([e for e in A.inbox_entries() if e.get("kind") == "INVARIANT"]), 1)  # one per spell
        A.write_beat("processor")
        rc, out = self.run_wd()
        self.assertEqual(rc, 0)
        self.assertEqual(A.open_conditions(), [])  # the invariant holds again: closed, its entry acked
        self.assertTrue(all(e["read"] for e in A.inbox_entries() if e.get("kind") == "INVARIANT"))

    def test_w1_and_w2_from_the_queue_and_the_published_gate(self):
        d, _c, _w = A.waiting_state(self.env["AGENTCTL_MOVIES_REPO"])
        os.makedirs(os.path.join(d, "entries"), exist_ok=True)
        old = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 3 * 3600))
        A.write_json(os.path.join(d, "entries", "e1.json"), {"id": "e1", "seq": 1, "status": "queued", "created": old})
        self.assertFalse(A.inv_w1()[0])
        A.write_json(os.path.join(d, "entries", "e1.json"), {"id": "e1", "seq": 1, "status": "landed", "created": old})
        self.assertTrue(A.inv_w1()[0])
        with open(self.env["AGENTCTL_GATE_SUMMARY"], "w") as f:
            f.write(f"{int(time.time())}\tpawelkrupinski/movies\tgate RED (main #9 → queued for a fixer (#1); convergence green)\n")
        self.assertTrue(A.inv_w2()[0])  # first seen now
        st = A.watchdog_state()
        A.write_json(A.state_root("watchdog.json"), {**st, "w2_since": time.time() - 11 * 60})
        ok, detail = A.inv_w2()
        self.assertFalse(ok)
        self.assertIn("main #9", detail)
        with open(self.env["AGENTCTL_GATE_SUMMARY"], "w") as f:
            f.write(f"{int(time.time())}\tpawelkrupinski/movies\tgate RED (main #9 → agent-x claimed; convergence green)\n")
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
