"""The golden cases' own rules (golden_cases.py): what may never move a digest."""
import os, sys, threading, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402
from golden_cases import digest  # noqa: E402


class ForeignThreadTest(unittest.TestCase):
    """#289 / #285 (2026-11-26): park_duty's and refresh_pinned_recording's digests moved in one suite run and held in
    the next. A golden records EVERY call to the names it fakes (epoch_now, env_num, write_json, err, ...), from any
    thread, and each call draws from the case's random stream: a thread an earlier test left running in the worker
    (status.py's and core.py's bounded reads outlive their join on purpose) that read a knob or the clock mid-digest
    was recorded into the case, and shifted every value drawn after it. Only the case's own thread is the case."""

    @staticmethod
    def build(case):
        case.fake("env_num", lambda rng, name, default: rng.random())
        return ()

    def test_a_call_from_another_thread_is_not_the_cases(self):
        def run(stray):
            def go():
                if stray:  # another test's leftover thread reading a knob while the case runs
                    t = threading.Thread(target=A.knob_park_digest_days)
                    t.start()
                    t.join()
                return A.knob_park_digest_days(), A.knob_park_digest_days()
            return lambda A: go
        self.assertEqual(digest(A, "quiet", self.build, run(False), n=50), digest(A, "stray", self.build, run(True), n=50))

    def test_the_stray_thread_reads_the_real_collaborator(self):
        got = []

        def go():
            t = threading.Thread(target=lambda: got.append(A.knob_park_digest_days()))
            t.start()
            t.join()
            return None
        digest(A, "stray", self.build, lambda A: go, n=3)
        self.assertEqual(got, [A.PARK_DIGEST_DAYS] * 3)


if __name__ == "__main__":
    unittest.main()
