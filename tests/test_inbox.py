"""Every final verdict of land / watch / wait / waiting await goes to a durable inbox, and land's WAITING line too, so
the orchestrating session reads it whether or not the agent that ran the command is still alive."""
import contextlib, io, json, os, signal, subprocess, sys, tempfile, time, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A, TOOL  # noqa: E402


class InboxTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.inbox = os.path.join(self.tmp.name, "inbox")
        self.saved = {k: os.environ.get(k) for k in ("AGENTCTL_INBOX_DIR", "AGENTCTL_OWNERS_DIR", "AGENTCTL_SCRATCH")}
        os.environ.update(AGENTCTL_INBOX_DIR=self.inbox, AGENTCTL_OWNERS_DIR=os.path.join(self.tmp.name, "owners"),
                          AGENTCTL_SCRATCH=os.path.join(self.tmp.name, "s"))

    def tearDown(self):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp.cleanup()

    def entries(self):
        return A.inbox_entries()

    def run_main(self, argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = A.main(argv)
        return code, out.getvalue()

    def test_a_wait_writes_its_verdict(self):
        flag = os.path.join(self.tmp.name, "done.flag")
        open(flag, "w").write("ok\n")
        code, out = self.run_main(["wait", "file", flag, "--timeout", "5"])
        self.assertEqual(code, 0)
        [e] = self.entries()
        self.assertEqual(e["command"], "wait")
        self.assertEqual(e["exit"], 0)
        self.assertTrue(e["final"])
        self.assertTrue(e["verdict"].startswith(f"file {flag}: present"), e["verdict"])
        self.assertFalse(e["read"])

    def test_a_refused_land_writes_not_landed(self):
        os.environ["PREPUSH_SKIP"] = "x"
        try:
            code, out = self.run_main(["land"])
        finally:
            del os.environ["PREPUSH_SKIP"]
        self.assertEqual(code, 1)
        [e] = self.entries()
        self.assertEqual((e["command"], e["exit"]), ("land", 1))
        self.assertTrue(e["verdict"].startswith("NOT LANDED: PREPUSH_SKIP"), e["verdict"])

    def test_waiting_is_written_at_once_and_the_final_verdict_later(self):
        def handler(a):
            print("WAITING abc123 (1 change queued on waiting; entry e1)")
            self.assertEqual([e["final"] for e in self.entries()], [False])  # there before the command ends
            print("LANDED abc123")
            print("WATCH OK: Main #5 green, convergence #6 green")
            return 0
        with contextlib.redirect_stdout(io.StringIO()):
            A.with_inbox("land", handler, None)
        waiting, final = self.entries()
        self.assertEqual(waiting["verdict"], "WAITING abc123 (1 change queued on waiting; entry e1)")
        self.assertEqual(final["verdict"], "WATCH OK: Main #5 green, convergence #6 green")
        self.assertEqual(final["verdicts"][-2:], ["LANDED abc123", "WATCH OK: Main #5 green, convergence #6 green"])

    def test_a_land_that_ends_at_waiting_writes_it_once(self):
        def handler(a):
            print("WAITING abc123 (entry e1, 2 in batch)")
            return 0
        with contextlib.redirect_stdout(io.StringIO()):
            A.with_inbox("land", handler, None)
        self.assertEqual([e["verdict"] for e in self.entries()], ["WAITING abc123 (entry e1, 2 in batch)"])

    def test_a_crash_is_a_verdict_too(self):
        def handler(a):
            print("LANDED abc123")
            raise RuntimeError("boom")
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(RuntimeError):
            A.with_inbox("watch", handler, None)
        [e] = self.entries()
        self.assertEqual((e["verdict"], e["exit"]), ("crashed: RuntimeError: boom", 1))

    def test_a_signal_ends_with_an_entry(self):
        flag = os.path.join(self.tmp.name, "never.flag")
        p = subprocess.Popen([sys.executable, TOOL, "wait", "file", flag, "--timeout", "60"], env=dict(os.environ),
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        owners, deadline = os.environ["AGENTCTL_OWNERS_DIR"], time.time() + 20
        while time.time() < deadline and not (os.path.isdir(owners) and any(n.endswith(".json") for n in os.listdir(owners))):
            time.sleep(0.05)
        p.send_signal(signal.SIGTERM)
        p.communicate(timeout=20)
        [e] = self.entries()
        self.assertEqual((e["command"], e["verdict"], e["exit"]), ("wait", "ended by signal 15", 143))

    def test_inbox_lists_unread_and_acks(self):
        A.inbox_write("land", "WATCH RED: Main #9 test", [], [], 3)
        A.inbox_write("wait", "run o/r#1: success", [], [], 0)
        code, out = self.run_main(["inbox", "--unread"])
        self.assertEqual(code, 0)
        self.assertEqual(len(out.splitlines()), 2)
        self.assertIn("UNREAD land", out)
        self.assertIn("exit 3: WATCH RED: Main #9 test", out)
        self.run_main(["inbox", "--unread", "--ack"])
        _, out = self.run_main(["inbox", "--unread"])
        self.assertIn("inbox: no unread entries", out)
        _, out = self.run_main(["inbox"])
        self.assertEqual(out.count("read  "), 2)

    def test_other_commands_write_nothing(self):
        self.run_main(["inbox"])
        self.run_main(["state", "demo"])
        self.assertEqual(self.entries(), [])


if __name__ == "__main__":
    unittest.main()
