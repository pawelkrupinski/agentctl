"""WA: agentctl's own main CI red past 30 min, or ci-runner's pin far behind origin/main, opens an INVARIANT condition
(2026-11-25: tests.yml red 5 h, the pin stuck on f90ba0e, nobody told)."""
import os, sys, time, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402
from fake_clock import FakeClock  # noqa: E402

NOW = 1_800_000_000.0


def stamp(ago_min):
    import datetime as dt
    return dt.datetime.fromtimestamp(NOW - ago_min * 60, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def run(number, conclusion, ago_min, status="completed"):
    return {"number": number, "status": status, "conclusion": conclusion, "headSha": f"{number:040x}", "createdAt": stamp(ago_min)}


class SelfCiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = __import__("tempfile").mkdtemp()
        for k, v in {"AGENTCTL_STATE_ROOT": self.tmp, "AGENTCTL_ROLE": "sync", "AGENTCTL_HOST": "laptop"}.items():
            old = os.environ.get(k)
            os.environ[k] = v
            self.addCleanup(lambda k=k, old=old: os.environ.pop(k, None) if old is None else os.environ.__setitem__(k, old))
        self.clock = FakeClock(NOW)

    def check(self, runs, pin="a" * 40, behind=0):
        for f in ("selfci-runs.json", "selfci-pin.json"):
            p = A.state_root(f)
            if os.path.exists(p):
                os.unlink(p)
        return A.inv_wa(self.clock, None, runs=lambda: runs, pin=lambda: pin, behind=lambda rev: behind)

    def test_green_ci_and_a_current_pin_hold(self):
        ok, detail = self.check([run(9, "success", 5), run(8, "failure", 60)])
        self.assertTrue(ok, detail)

    def test_red_past_thirty_minutes_is_counted_from_the_streaks_first_run(self):
        ok, detail = self.check([run(9, "failure", 5), run(8, "failure", 100), run(7, "success", 200)])
        self.assertFalse(ok)
        self.assertIn("red on main for", detail)
        self.assertIn("#9", detail)

    def test_a_fresh_red_and_an_undecided_run_are_no_violation(self):
        self.assertTrue(self.check([run(9, "failure", 5), run(7, "success", 200)])[0])
        self.assertTrue(self.check([run(9, "", 1, status="in_progress"), run(8, "success", 90)])[0])

    def test_a_pin_far_behind_main_violates(self):
        ok, detail = self.check([run(9, "success", 5)], behind=11)
        self.assertFalse(ok)
        self.assertIn("11 commits behind", detail)
        self.assertTrue(self.check([run(9, "success", 5)], behind=10)[0])

    def test_unreadable_runs_cannot_tell_and_the_server_is_never_asked(self):
        self.assertIsNone(self.check(None)[0])
        os.environ["AGENTCTL_ROLE"] = "server"
        self.assertTrue(self.check([run(9, "failure", 500)])[0])

    def wire(self, rev, calls):
        sf = os.path.join(self.tmp, "state.json")
        with open(sf, "w") as f:
            f.write(__import__("json").dumps({"rev": rev} if rev is not None else {}))
        os.environ["AGENTCTL_STATE_FILE"] = sf
        self.addCleanup(os.environ.pop, "AGENTCTL_STATE_FILE", None)

        def fake(argv, **kw):
            calls.append(argv)
            return A.subprocess.CompletedProcess(argv, 0, "/var/lib/agentctl-src/" + "b" * 40 + "\n", "")
        from fake_runner import FakeRunner
        A.wire_runner(FakeRunner(run=fake))

    def read(self, **kw):
        return A.inv_wa(self.clock, None, runs=lambda: [run(9, "success", 5)], behind=lambda rev: 0, **kw)

    def test_the_pin_is_read_from_the_reconcilers_reported_rev_with_no_ssh(self):
        calls = []
        self.wire("c" * 12, calls)
        self.assertEqual(A.readings(NOW, lambda: [], None)[1], "c" * 12)
        self.assertTrue(self.read()[0])
        self.assertEqual(calls, [])

    def test_without_a_reported_rev_ssh_reuses_the_master_and_is_kept_thirty_minutes(self):
        calls = []
        self.wire(None, calls)
        self.assertEqual(A.readings(NOW, lambda: [], None)[1], "b" * 40)
        self.assertNotIn("ControlPath=none", " ".join(calls[0]))
        self.assertNotIn("ControlMaster=no", " ".join(calls[0]))
        A.readings(NOW + 1500, lambda: [], None)
        self.assertEqual(len(calls), 1)
        A.readings(NOW + 1900, lambda: [], None)
        self.assertEqual(len(calls), 2)

    def test_the_watchdog_registers_it(self):
        self.assertIn("WA", A.INVARIANTS)


if __name__ == "__main__":
    unittest.main()
