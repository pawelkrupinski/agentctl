"""WA: agentctl's own main CI red past 30 min, a green run left unpinned, or no decided run for the head, opens an INVARIANT condition
(2026-11-25: tests.yml red 5 h, the pin stuck on f90ba0e, nobody told)."""
import os, sys, time, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402
from harness import case_context  # noqa: E402
from fake_clock import FakeClock  # noqa: E402

NOW = 1_800_000_000.0


def stamp(ago_min):
    import datetime as dt
    return dt.datetime.fromtimestamp(NOW - ago_min * 60, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha(number):
    return f"{number:x}".ljust(40, "0")


def run(number, conclusion, ago_min, status="completed", done_min=None):
    return {"number": number, "status": status, "conclusion": conclusion, "headSha": sha(number),
            "createdAt": stamp(ago_min), "updatedAt": stamp(ago_min if done_min is None else done_min)}


class SelfCiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = __import__("tempfile").mkdtemp()
        for k, v in {"AGENTCTL_STATE_ROOT": self.tmp, "AGENTCTL_ROLE": "sync", "AGENTCTL_HOST": "laptop"}.items():
            old = os.environ.get(k)
            os.environ[k] = v
            self.addCleanup(lambda k=k, old=old: os.environ.pop(k, None) if old is None else os.environ.__setitem__(k, old))
        self.clock = FakeClock(NOW)

    def check(self, runs, pin=None, behind=0):
        newest_green = next((r["headSha"] for r in runs or [] if r["conclusion"] == "success"), "a" * 40)
        pin = pin or newest_green
        for f in ("selfci-runs.json", "selfci-pin.json", "selfci-why.json"):
            p = A.state_root(f)
            if os.path.exists(p):
                os.unlink(p)
        return A.inv_wa(self.clock, None, runs=lambda: runs, pin=lambda: pin, behind=lambda rev: behind, failure=lambda rid: None, ctx=case_context())

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

    def test_a_big_push_with_ci_in_progress_is_no_alert(self):
        pin = sha(9)
        runs = [run(11, "", 3, status="in_progress"), run(10, "success", 40, done_min=30), run(9, "success", 90)]
        ok, detail = self.check(runs[:1] + runs[2:], pin=pin, behind=12)
        self.assertTrue(ok, detail)
        self.assertIn("12 behind main", detail)

    def test_a_green_run_left_unpinned_for_fifteen_minutes_alerts(self):
        runs = [run(10, "success", 40, done_min=20), run(9, "success", 90)]
        ok, detail = self.check(runs, pin=sha(9))
        self.assertFalse(ok)
        self.assertIn("auto-pin is stuck", detail)
        self.assertTrue(self.check([run(10, "success", 20, done_min=10), run(9, "success", 90)], pin=sha(9))[0])
        self.assertTrue(self.check(runs, pin=sha(10)[:12])[0])

    def test_no_decided_run_for_the_head_for_an_hour_alerts(self):
        ok, detail = self.check([run(10, "", 70, status="queued"), run(9, "success", 200)], pin=sha(9))
        self.assertFalse(ok)
        self.assertIn("CI is stuck or queued", detail)
        self.assertTrue(self.check([run(10, "", 50, status="queued"), run(9, "success", 200)], pin=sha(9))[0])

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
        return A.inv_wa(self.clock, None, runs=lambda: [run(9, "success", 5)], behind=lambda rev: 0, **kw, ctx=case_context())

    def test_the_pin_is_read_from_the_reconcilers_reported_rev_with_no_ssh(self):
        calls = []
        self.wire("c" * 12, calls)
        self.assertEqual(A.readings(NOW, lambda: [], None, ctx=case_context())[1], "c" * 12)
        self.assertTrue(self.read()[0])
        self.assertEqual(calls, [])

    def test_without_a_reported_rev_ssh_reuses_the_master_and_is_kept_thirty_minutes(self):
        calls = []
        self.wire(None, calls)
        self.assertEqual(A.readings(NOW, lambda: [], None, ctx=case_context())[1], "b" * 40)
        self.assertNotIn("ControlPath=none", " ".join(calls[0]))
        self.assertNotIn("ControlMaster=no", " ".join(calls[0]))
        A.readings(NOW + 1500, lambda: [], None, ctx=case_context())
        self.assertEqual(len(calls), 1)
        A.readings(NOW + 1900, lambda: [], None, ctx=case_context())
        self.assertEqual(len(calls), 2)

    def test_a_red_names_the_failing_test_the_seed_and_the_replay_command(self):
        log = ("suite\tstep\t2026 FAIL: test_reds.BackgroundSessionTest.test_x\nsuite\tstep\tseed: 4242 (replay)\n"
               "suite\tstep\tREPLAY: python3 tests/run.py --seed 4242 --shard 0/2\n")
        for f in ("selfci-runs.json", "selfci-pin.json", "selfci-why.json"):
            p = A.state_root(f)
            if os.path.exists(p):
                os.unlink(p)
        seen = []
        ok, detail = A.inv_wa(self.clock, None, runs=lambda: [dict(run(9, "failure", 100), databaseId=77)], pin=lambda: sha(9),
                              behind=lambda rev: 0, failure=lambda rid: seen.append(rid) or log, ctx=case_context())
        self.assertFalse(ok)
        self.assertIn("failing: test_reds.BackgroundSessionTest.test_x", detail)
        self.assertIn("seed 4242", detail)
        self.assertIn("replay: python3 tests/run.py --seed 4242 --shard 0/2", detail)
        self.assertIn("never rerun", detail)
        self.assertEqual(seen, [77])

    def test_the_watchdog_registers_it(self):
        self.assertIn("WA", A.INVARIANTS)


if __name__ == "__main__":
    unittest.main()
