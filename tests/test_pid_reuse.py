"""A fixer's wrapper is judged by its pid AND its start time (a reused pid is not the wrapper): the lease meta carries
process_start, `_wrapper_gone` reads it, and every record's liveness is ONE function, procs.record_alive."""
import os, sys, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402

OTHER_START = "Mon Jan  1 00:00:00 2001"  # not when this test process started


class PidReuseTest(unittest.TestCase):
    def test_a_live_pid_with_another_start_is_not_the_wrapper(self):
        me = os.getpid()
        self.assertIsNotNone(A._wrapper_gone({"pid": me, "process_start": OTHER_START}))
        self.assertIsNone(A._wrapper_gone({"pid": me, "process_start": A.process_start(me)}))
        self.assertIsNone(A._wrapper_gone({"pid": me}))  # a meta from before the start was recorded: the pid decides

    def test_record_alive_is_pid_and_start(self):
        me = os.getpid()
        self.assertTrue(A.record_alive({"pid": me, "process_start": A.process_start(me)}))
        self.assertFalse(A.record_alive({"pid": me, "process_start": OTHER_START}))
        self.assertFalse(A.record_alive({"pid": None}))
        self.assertFalse(A.record_alive({}))

    def test_a_blame_probe_whose_pid_was_reused_has_ended(self):
        me = os.getpid()
        p = {"pid": me, "process_start": OTHER_START, "out": "/nonexistent/x.json", "at": A.epoch_now()}
        self.assertEqual(A.blame_probe_result(p, A.epoch_now())["verdict"], "none")  # not still running

    def test_the_lease_meta_names_the_wrapper_start(self):
        import types
        w = object.__new__(A.RedFixerWrapper)
        w.rec = {"log": "/x/y.log", "why": "w"}
        w.own = types.SimpleNamespace(name="n", epoch=1, secret="s", holder=None, triple=lambda: ("n", 1, "s"))
        w.held, w.wt, w.a = [], "/wt", types.SimpleNamespace(record="/x/record.json")
        w.st = types.SimpleNamespace(get=lambda n: (_ for _ in ()).throw(A.StoreUnavailable("down")))
        w.save = lambda: None
        w.register()
        self.assertEqual(w.meta["process_start"], A.process_start(os.getpid()))


    def test_a_session_row_whose_process_is_a_zombie_is_not_waiting(self):
        import subprocess, time
        child = subprocess.Popen(["true"])
        for _ in range(100):  # ended, never reaped: still a pid kill(0) answers for
            if A.run_cmd(["ps", "-o", "stat=", "-p", str(child.pid)]).stdout.strip().startswith("Z"):
                break
            time.sleep(0.05)
        try:
            row = {"state": "idle", "pid": child.pid}
            self.assertFalse(A.bg_waiting(row))
            self.assertTrue(A.bg_ended(row))
        finally:
            child.wait()


class FileTimesAreRealTimeTest(unittest.TestCase):
    """A file's mtime is the wall's time: the idle a transcript shows is read against the wall, whatever the process's
    clock says (a frozen or far-ahead injected clock no longer makes a fresh transcript a day idle)."""

    def test_idle_of_a_fresh_file_is_small_on_a_clock_far_from_the_wall(self):
        import tempfile
        f = tempfile.NamedTemporaryFile()
        far = A.wall_epoch() + 10 * 86400  # the process's clock, ten days ahead
        self.assertLess(A._idle(far, None, f.name), 60)
        self.assertGreater(A._idle(far, far - 500, "/nonexistent"), 499)  # a stamp is read on the clock that made it
        self.assertEqual(A._idle(far, far - 500, f.name) < 60, True)       # the newer of the two


if __name__ == "__main__":
    unittest.main()
