"""Every tick step is bounded (2026-10-29: RECONCILER STALLED 05:37-05:41, a tunnel hang held the gate's reads)."""
import os, sys, threading, time, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A, code_source  # noqa: E402


class StepBoundTest(unittest.TestCase):
    def setUp(self):
        os.environ["AGENTCTL_STEP_JOIN"] = "0.3"

    def tearDown(self):
        os.environ.pop("AGENTCTL_STEP_JOIN", None)

    def test_a_hung_step_holds_the_tick_for_its_bound_only_and_is_not_started_twice(self):
        gate, runs = threading.Event(), []

        def hung():
            runs.append(1)
            gate.wait(10)
        t0 = time.time()
        self.assertIsNone(A.run_step("t-hung", hung))
        self.assertLess(time.time() - t0, 2)
        self.assertIsNone(A.run_step("t-hung", hung))  # still running: not again, and no wait at all
        self.assertEqual(runs, [1])
        gate.set()
        A._bounded["step:t-hung"].join(5)
        self.assertTrue(A.run_step("t-hung", lambda: None))

    def test_a_steps_error_is_reported_now_or_at_the_next_look(self):
        def boom():
            raise RuntimeError("github said no")
        self.assertFalse(A.run_step("t-boom", boom))
        gate = threading.Event()

        def late():
            gate.wait(5)
            raise RuntimeError("late")
        self.assertIsNone(A.run_step("t-late", late))
        gate.set()
        A._bounded["step:t-late"].join(5)
        self.assertTrue(A.run_step("t-late", lambda: None))  # its old error said once, then this run is good
        self.assertNotIn("t-late", {k for k, v in A._step_errors.items() if v})

    def test_no_external_call_of_the_tick_is_unbounded(self):
        src = code_source()
        self.assertIn("def git(repo, *args, check=True, input=None, timeout=600):", src)  # bounded by default...
        self.assertIn("timeout=timeout)  # every git call is bounded", src)  # ...and always passed on
        self.assertIn('capture_output=True, timeout=120).returncode == 0', src)


if __name__ == "__main__":
    unittest.main()


class StoreDownNeverEndsTheLoopTest(unittest.TestCase):
    """2026-10-29 22:31: the network dropped, every step failed for ROUND_ERRORS_MAX rounds, and the STALLED condition's
    own call to the (remote) store raised out of the loop: the processor died, launchd restarted it, and a restarted
    processor holds no token. The loop must outlive its store; a re-exec onto new code must keep the token."""

    def test_failing_rounds_and_an_unreachable_store_never_end_the_processor(self):
        from test_agentctl import Repo
        import tempfile
        r = Repo(tempfile.mkdtemp())
        rounds, opened = [], []
        saved = (A.server_steps, A.open_condition, A.TokenDrop.nap, A.restart_if_changed, A.write_snapshot)

        def down(*a, **k):
            opened.append(1)
            raise A.StoreUnavailable("POST /v1/store/items: <urlopen error [Errno 61] Connection refused>")

        def nap(self, secs):
            rounds.append(1)
            if len(rounds) >= A.ROUND_ERRORS_MAX + 2:
                A.TICK_STOP[0] = True
        A.server_steps = lambda repo, d, shared: (("boom", lambda: (_ for _ in ()).throw(RuntimeError("down"))),)
        A.open_condition, A.TokenDrop.nap, A.restart_if_changed = down, nap, lambda *a, **k: None
        A.write_snapshot = lambda *a, **k: None
        try:
            self.assertEqual(A.waiting_run(r.path), 0)  # ended by TICK_STOP, never by the store's exception
        finally:
            (A.server_steps, A.open_condition, A.TokenDrop.nap, A.restart_if_changed, A.write_snapshot) = saved
            A.TICK_STOP[0] = False
        self.assertEqual(len(rounds), A.ROUND_ERRORS_MAX + 2, "every round ran")
        self.assertEqual(len(opened), 3, "the STALLED note is tried again each failing round until it lands")

    def test_a_re_exec_onto_new_code_keeps_the_token_in_the_environment(self):
        seen = {}
        saved = (A.os.execv, A.START_CODE, A.restart_gate, A.code_version, os.environ.get("AGENTCTL_STORE_TOKEN"))
        A.os.execv = lambda exe, argv: seen.update(argv=argv, tok=os.environ.get("AGENTCTL_STORE_TOKEN"))
        A.START_CODE, A.restart_gate, A.code_version = "old", (lambda: (True, "")), (lambda *a: "new")
        A._last_code_check[0] = 0
        os.environ["AGENTCTL_STORE_TOKEN"] = "tok-handed-over"
        try:
            A.restart_if_changed(argv=["waiting", "run", "--service"])
        finally:
            A.os.execv, A.START_CODE, A.restart_gate, A.code_version, tok = saved
            if tok is None:
                os.environ.pop("AGENTCTL_STORE_TOKEN", None)
            else:
                os.environ["AGENTCTL_STORE_TOKEN"] = tok
        # execv (never execve with a filtered env): the new image inherits this environment, the token with it
        self.assertEqual(seen["tok"], "tok-handed-over")
        self.assertEqual(seen["argv"][-3:], ["waiting", "run", "--service"])
