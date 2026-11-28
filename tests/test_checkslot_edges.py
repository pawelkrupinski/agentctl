"""checkslot: a slot has its result fields before done(); a `now` of 0 is a time, not unset; a failing sample is logged."""
import os, sys, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402


class CheckSlotEdgesTest(unittest.TestCase):
    def test_a_slot_has_rc_and_out_before_done(self):
        s = A.Slot("x", machine=A.FixedMachine())
        self.assertEqual((s.rc, s.out), (None, ""))
        s.done(3, "o")
        self.assertEqual((s.rc, s.out), (3, "o"))

    def test_now_zero_is_a_time(self):
        seen = {}
        saved = {n: getattr(A, n) for n in ("adjust_cap", "inbox_write", "write_json", "check_runs", "cap_state")}
        os.environ.pop("AGENTCTL_MAX_CHECKS", None)
        try:
            A.cap_state = lambda: {}
            A.check_runs = lambda days, now: seen.setdefault("runs_now", now) and []
            A.adjust_cap = lambda runs, cap, mx, last, now: seen.update(now=now) or (cap, None)
            A.maybe_adjust_cap(36.0, now=0)
        finally:
            for n, v in saved.items():
                setattr(A, n, v)
        self.assertEqual(seen["now"], 0)

    def test_a_failing_sample_is_logged_not_swallowed(self):
        errs = []

        class NoSysctl(A.FixedMachine):
            def memory(self):
                raise OSError("no sysctl")
        saved_err, A.print_error = A.print_error, errs.append
        try:
            A.Slot("x", machine=NoSysctl({"load1": 1.0})).sample(force=True)
        finally:
            A.print_error = saved_err
        self.assertTrue(any("no sysctl" in e for e in errs))


if __name__ == "__main__":
    unittest.main()
