"""The pre-commit check slots (the owner, 2026-10-27: "limit the precommit checks to a reasonable number", "keep measuring
to adjust that number"): N flocks a check holds for its run, a waiter's position and the holders, a dead holder's slot
freed by the kernel, every run measured, and the cap adjusted from the data."""
import contextlib, io, json, os, subprocess, sys, tempfile, time, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402

DAY = 86400


def runs(n, running, duration, waited=0, pressure=30, swap=(100, 100), at=None):
    at = at or time.time()
    return [{"at": at - i, "duration": duration, "waited": waited, "running": running, "cap": running,
             "pressure": pressure, "swap_mb_start": swap[0], "swap_mb": swap[1]} for i in range(n)]


class AdjustCapTest(unittest.TestCase):
    now = 10 * DAY

    def test_common_slot_waits_and_low_memory_raise_it(self):
        cap, why = A.adjust_cap(runs(6, 3, 300, waited=120, at=self.now), 3, 4, 0, self.now)
        self.assertEqual(cap, 4)
        self.assertIn("waited over a minute", why)

    def test_never_above_the_jvm_slots(self):
        self.assertEqual(A.adjust_cap(runs(6, 4, 300, waited=120, at=self.now), 4, 4, 0, self.now), (4, None))

    def test_memory_pressure_or_swap_growth_lower_it(self):
        self.assertEqual(A.adjust_cap(runs(6, 3, 300, pressure=80, at=self.now), 3, 4, 0, self.now)[0], 2)
        self.assertEqual(A.adjust_cap(runs(6, 3, 300, swap=(100, 900), at=self.now), 3, 4, 0, self.now)[0], 2)

    def test_never_below_one(self):
        self.assertEqual(A.adjust_cap(runs(6, 1, 300, pressure=90, at=self.now), 1, 4, 0, self.now), (1, None))

    def test_a_slower_p90_at_the_current_level_than_below_lowers_it(self):
        data = runs(6, 3, 500, at=self.now) + runs(6, 2, 300, at=self.now)
        cap, why = A.adjust_cap(data, 3, 4, 0, self.now)
        self.assertEqual(cap, 2)
        self.assertIn("1.5x", why)
        self.assertEqual(A.adjust_cap(runs(6, 3, 400, at=self.now) + runs(6, 2, 300, at=self.now), 3, 4, 0, self.now),
                         (3, None), "1.33x is no reason")

    def test_at_most_one_change_a_day(self):
        self.assertEqual(A.adjust_cap(runs(6, 3, 300, pressure=90, at=self.now), 3, 4, self.now - DAY / 2, self.now),
                         (3, None))

    def test_a_change_is_logged_and_sent_to_the_inbox_and_the_env_overrides(self):
        with tempfile.TemporaryDirectory() as d:
            env = {"AGENTCTL_CHECKS_DIR": os.path.join(d, "checks"), "AGENTCTL_INBOX_DIR": os.path.join(d, "inbox")}
            saved = {k: os.environ.get(k) for k in list(env) + ["AGENTCTL_MAX_CHECKS"]}
            os.environ.update(env)
            os.environ.pop("AGENTCTL_MAX_CHECKS", None)
            try:
                with open(os.path.join(A.checks_dir(), "runs.jsonl"), "w") as f:
                    for r in runs(6, 3, 300, pressure=85):
                        f.write(json.dumps(r) + "\n")
                line = A.maybe_adjust_cap()
                self.assertIn("CHECK CAP 3 -> 2", line)
                self.assertEqual(A.checks_cap(), 2)
                self.assertIn("CHECK CAP 3 -> 2", open(os.path.join(A.checks_dir(), "adjust.log")).read())
                self.assertEqual([e["command"] for e in A.inbox_entries()], ["checks"])
                self.assertIsNone(A.maybe_adjust_cap(), "at most once a day")
                os.environ["AGENTCTL_MAX_CHECKS"] = "4"
                self.assertEqual(A.checks_cap(), 4)
            finally:
                for k, v in saved.items():
                    os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)


class SlotTest(unittest.TestCase):
    """A check is admitted by the governor as a check ticket; the check cap is one of its limits."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        keys = ("AGENTCTL_CHECKS_DIR", "AGENTCTL_MAX_CHECKS", "AGENTCTL_GOV_DIR", "AGENTCTL_GOV_FREE_PROBE",
                "AGENTCTL_GOV_PRESSURE_PROBE", "AGENTCTL_GOV_LOAD_PROBE", "AGENTCTL_GOV_POLL", "KINOWO_SBT_WARM_STATE")
        self.saved = {k: os.environ.get(k) for k in keys}
        os.environ.update(AGENTCTL_CHECKS_DIR=os.path.join(self.tmp.name, "checks"), AGENTCTL_MAX_CHECKS="1",
                          AGENTCTL_GOV_DIR=os.path.join(self.tmp.name, "gov"), AGENTCTL_GOV_FREE_PROBE="echo 30",
                          AGENTCTL_GOV_PRESSURE_PROBE="echo 20", AGENTCTL_GOV_LOAD_PROBE="echo 1",
                          AGENTCTL_GOV_POLL="0.2", KINOWO_SBT_WARM_STATE=os.path.join(self.tmp.name, "sbt-warm"))
        self.procs = []

    def tearDown(self):
        for p in self.procs:
            if p.poll() is None:
                p.kill()
                p.wait()
        for k, v in self.saved.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
        self.tmp.cleanup()

    def holder(self, seconds):
        """Another process holding a check ticket for `seconds`."""
        ready = os.path.join(self.tmp.name, "ready")
        code = ("import sys, time, importlib.machinery, importlib.util\n"
                "l = importlib.machinery.SourceFileLoader('agentctl', sys.argv[1])\n"
                "s = importlib.util.spec_from_loader('agentctl', l); A = importlib.util.module_from_spec(s); l.exec_module(A)\n"
                "t = A.gov_admit('check', 1.0, 'land movies-wt-x abc', kind='check')\n"
                "open(sys.argv[2], 'w').close(); time.sleep(float(sys.argv[3]))")
        p = subprocess.Popen([sys.executable, "-c", code, A.__file__, ready, str(seconds)])
        self.procs.append(p)
        while not os.path.exists(ready):
            time.sleep(0.05)
        return p

    def take(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            with A.check_slot("/repo", "land test") as slot:
                slot.done(0, "")
        return err.getvalue()

    def test_a_waiter_says_why_then_runs_when_the_check_ends(self):
        self.holder(2.5)
        t0 = time.time()
        out = self.take()
        self.assertGreater(time.time() - t0, 1.5, "it waited for the holder")
        self.assertIn("waiting for the governor (check, 5 GB, land test): 1/1 checks running (the check cap)", out)
        rec = [json.loads(l) for l in open(os.path.join(A.checks_dir(), "runs.jsonl"))][-1]
        self.assertGreater(rec["waited"], 1.5)
        self.assertEqual((rec["cap"], rec["running"], rec["label"]), (1, 1, "land test"))
        for k in ("duration", "load1", "cpu", "pressure", "swap_mb"):
            self.assertIn(k, rec)

    def test_a_killed_holders_ticket_is_freed_by_the_kernel(self):
        p = self.holder(60)
        p.kill()
        p.wait()
        t0 = time.time()
        self.take()
        self.assertLess(time.time() - t0, 5)

    def test_a_red_fix_skips_the_check_cap(self):
        self.holder(30)
        t0 = time.time()
        with A.check_slot("/repo", "land fix", "red") as slot:
            self.assertEqual(slot.env({})["KINOWO_GOV_CLASS"], "red")
        self.assertLess(time.time() - t0, 5)

    def test_stats_per_concurrency_level(self):
        with open(os.path.join(A.checks_dir(), "runs.jsonl"), "w") as f:
            for r in runs(4, 1, 100, waited=0) + runs(4, 2, 200, waited=30):
                f.write(json.dumps(r) + "\n")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            A.main(["checks", "stats"])
        lines = out.getvalue().splitlines()
        self.assertIn("cap 1 ($AGENTCTL_MAX_CHECKS)", lines[0])
        self.assertRegex(out.getvalue(), r"\n\s+1\s+4\s+100s\s+100s\s+0s\s+0s")
        self.assertRegex(out.getvalue(), r"\n\s+2\s+4\s+200s\s+200s\s+30s\s+30s")


class StallTest(unittest.TestCase):
    def test_a_trickle_of_cpu_is_not_progress(self):
        # the hook idle in `wait` with only its watchdog polling: a few ms of CPU each poll, no output
        with tempfile.TemporaryDirectory() as d:
            code = "import time\nwhile True:\n    time.sleep(0.05); sum(range(200))"
            rc, out = A.run_watched([sys.executable, "-c", code], d, dict(os.environ), os.path.join(d, "log"), "x",
                                    stall=1.5)
        self.assertIsNone(rc, "stalled")

    def test_real_cpu_is_progress(self):
        with tempfile.TemporaryDirectory() as d:
            code = "import time\nt=time.time()\nwhile time.time()-t<4: pass"
            rc, out = A.run_watched([sys.executable, "-c", code], d, dict(os.environ), os.path.join(d, "log"), "x",
                                    stall=1.5)
        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
