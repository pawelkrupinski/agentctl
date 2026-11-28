"""Real elapsed time (a thread join's bound, a probe's latency, a read's duration) is the wall's, never the process's
injected clock: on a frozen clock a status read used to wait each source's whole bound in turn and report 0 ms."""
import os, sys, threading, time, types, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402
from harness import case_context  # noqa: E402
from fake_probes import registry  # noqa: E402
from fake_clock import FakeClock  # noqa: E402


class FrozenClockTest(unittest.TestCase):
    def setUp(self):
        self.was = A._PROCESS_CLOCK[0]
        A.wire_clock(FakeClock())  # never advances unless slept on

    def tearDown(self):
        A.wire_clock(self.was)

    def test_status_sources_share_one_deadline_and_time_their_reads(self):
        # the slow reads end with the test: one left reading 5 s ran its epoch_now() inside the next test in the worker
        # (2026-11-26, tests #289 / #285: a golden faking epoch_now recorded it and its digest moved)
        done = threading.Event()
        slow = [(f"s{i}", lambda: done.wait(5), 1) for i in range(5)]
        self.addCleanup(lambda: [done.set()] + [t.join(5) for t in threading.enumerate() if t.name.startswith("status:s")])
        t0 = time.monotonic()
        A.status_sources(slow, 0.4, case_context().caches.status)
        self.assertLess(time.monotonic() - t0, 1.2)  # one 0.4 s deadline, not 5 of them in turn
        out: dict = {}
        A.status_read("x", lambda: time.sleep(0.2), out, False, {})
        self.assertGreaterEqual(out["x"][2], 0.15)

    def test_the_store_probe_measures_its_latency(self):
        st = types.SimpleNamespace(meta_get=lambda k, d=None: time.sleep(0.2))
        self.assertGreaterEqual(A.status_store_probe(st, 0)["ms"], 150)

    def test_a_gate_marked_dirty_after_the_last_publish_publishes_whatever_the_injected_clock(self):
        import tempfile
        A.wire_clock(FakeClock(A.wall_epoch() + 10 * 86400))  # ten days ahead of the files' times
        calls = []
        real = A.gate_publish_cycle
        A.gate_publish_cycle = lambda repo, store, registry, ctx=None: calls.append(1)
        try:
            A._last_gate_publish[0] = 0
            A.maybe_publish_gate("r", None, registry(A), ctx=case_context())
            self.assertEqual(len(calls), 1)
            dirty = A.gate_dirty_path()
            os.makedirs(os.path.dirname(dirty), exist_ok=True)
            open(dirty, "w").close()
            os.utime(dirty, (A.wall_epoch() + 5, A.wall_epoch() + 5))  # a push since: dirty
            A.maybe_publish_gate("r", None, registry(A), ctx=case_context())
            self.assertEqual(len(calls), 2)
        finally:
            A.gate_publish_cycle = real


if __name__ == "__main__":
    unittest.main()
