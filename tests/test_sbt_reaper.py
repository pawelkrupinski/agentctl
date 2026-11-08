"""Stuck sbt servers (2026-10-27): an orphaned serving command, or a running command with no CPU for 20 min, is reaped
by orphans / sweep, after a thread dump; the rules on fakes."""
import argparse, contextlib, io, os, sys, tempfile, types, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402

DEAD = 999999


PASS_MIN = 15  # agentctl maintain runs the reaper every 15 min (launchd StartInterval 900)


def passes_until(state, until_min, cpu, clients, **kwargs):
    """Sample an unchanging CPU total at the real cadence from 0 up to until_min (a gap longer than
    SBT_SAMPLE_MAX_GAP_MIN would start the server over); the reasons from the last pass."""
    times = list(range(0, until_min, PASS_MIN)) + [until_min]
    return [A.sbt_stuck_reasons(7, "/wt", cpu, clients, {}, state, t * 60, **kwargs) for t in times][-1]


class SbtReaperTest(unittest.TestCase):
    def test_a_running_command_with_no_cpu_for_twenty_minutes_is_stuck(self):
        state, clients = {}, {"/wt": [42]}
        self.assertEqual(A.sbt_stuck_reasons(7, "/wt", 100.0, clients, {}, state, 0), [])  # first sight
        self.assertEqual(A.sbt_stuck_reasons(7, "/wt", 100.2, clients, {}, state, 19 * 60), [])  # under 20 min
        why = A.sbt_stuck_reasons(7, "/wt", 100.3, clients, {}, state, 21 * 60)
        self.assertEqual(len(why), 1)
        self.assertIn("no CPU for 21 min", why[0])

    def test_cpu_is_progress_and_an_idle_server_without_a_command_is_not_stuck(self):
        state = {}
        A.sbt_stuck_reasons(7, "/wt", 100.0, {"/wt": [42]}, {}, state, 0)
        self.assertEqual(A.sbt_stuck_reasons(7, "/wt", 160.0, {"/wt": [42]}, {}, state, 30 * 60), [])  # it worked
        self.assertEqual(A.sbt_stuck_reasons(7, "/wt", 160.0, {}, {}, state, 90 * 60), [])  # idle: no client

    def test_a_detached_server_idle_for_two_hours_with_no_client_is_reaped(self):
        # 2026-11-07: two lone detached servers (core-wt-red-822/824, -Xmx6000M) sat idle for 3 h while swap was at
        # 40.1/40.96 GB; no rule matched (not a duplicate, under the hog CPU floor, no command running).
        state = {}
        self.assertEqual(passes_until(state, 119, 100.2, {}, ppid=1, kept=set(), swap_free_mb=10_000), [])
        why = A.sbt_stuck_reasons(7, "/wt", 100.3, {}, {}, state, 121 * 60, ppid=1, kept=set(), swap_free_mb=10_000)
        self.assertEqual(len(why), 1)
        self.assertIn("idle for 121 min", why[0])

    def test_an_idle_server_is_spared_when_kept_attached_or_in_use(self):
        later = 300
        for kwargs, clients in (
            ({"ppid": 1, "kept": {"/wt"}}, {}),  # sbt-warm --keep keeps it warm on purpose
            ({"ppid": 4242, "kept": set()}, {}),  # still attached to the client that started it
            ({"ppid": 1, "kept": set()}, {"/wt": [42]}),  # a command is connected (the stall rule judges that)
        ):
            why = passes_until({}, later, 100.0, clients, swap_free_mb=10_000, **kwargs)
            self.assertFalse(any("idle for" in w for w in why), (kwargs, clients, why))

    def test_low_swap_shortens_the_idle_limit(self):
        state = {}
        A.sbt_stuck_reasons(7, "/wt", 100.0, {}, {}, state, 0, ppid=1, kept=set(), swap_free_mb=500)
        self.assertEqual(
            A.sbt_stuck_reasons(7, "/wt", 100.0, {}, {}, state, 29 * 60, ppid=1, kept=set(), swap_free_mb=500), [])
        why = A.sbt_stuck_reasons(7, "/wt", 100.0, {}, {}, state, 31 * 60, ppid=1, kept=set(), swap_free_mb=500)
        self.assertEqual(len(why), 1)
        self.assertIn("swap free 500 MB", why[0])

    def test_unreadable_swap_uses_the_normal_limit(self):
        self.assertEqual(passes_until({}, 60, 100.0, {}, ppid=1, kept=set(), swap_free_mb=None), [])

    def test_swap_free_is_read_from_sysctl(self):
        self.assertEqual(A.parse_swap_free_mb("vm.swapusage: total = 40960.00M  used = 40105.06M  free = 854.94M  (encrypted)"), 854.94)
        self.assertIsNone(A.parse_swap_free_mb(""))
        self.assertIsNone(A.parse_swap_free_mb("garbage"))

    # 2026-11-07, measured: idle sbt server JVMs burn 0.04-0.12 CPU s/min (5 servers, 30 one-minute samples), busy ones
    # 5-155 s/min. The 15-min maintain pass therefore saw ~1.5 s per pass from an idle server, over the old 0.5 s
    # absolute tolerance, so "since" restarted on every pass and neither rule below could ever fire.
    IDLE_PER_MIN, BUSY_PER_MIN, PASS = 0.1, 5.0, 15 * 60

    def run_passes(self, minutes, per_min, **kwargs):
        state, why, cpu = {}, [], 100.0
        for t in range(0, minutes * 60 + 1, self.PASS):
            why = A.sbt_stuck_reasons(7, "/wt", cpu + per_min * t / 60, kwargs.pop("clients", None) or self.clients,
                                      {}, state, t, **kwargs)
        return why

    def test_real_idle_background_cpu_still_counts_as_idle_for_the_idle_rule(self):
        self.clients = {}
        why = self.run_passes(135, self.IDLE_PER_MIN, ppid=1, kept=set(), swap_free_mb=10_000)
        self.assertEqual(len(why), 1, why)
        self.assertIn("idle for 135 min", why[0])

    def test_real_idle_background_cpu_still_counts_as_no_cpu_for_the_stall_rule(self):
        self.clients = {"/wt": [42]}
        why = self.run_passes(30, self.IDLE_PER_MIN)
        self.assertEqual(len(why), 1, why)
        self.assertIn("no CPU for 30 min", why[0])

    def test_a_busy_server_is_never_idle(self):
        for clients, kwargs in (({}, {"ppid": 1, "kept": set(), "swap_free_mb": 10_000}), ({"/wt": [42]}, {})):
            self.clients = clients
            self.assertEqual(self.run_passes(300, self.BUSY_PER_MIN, **kwargs), [])

    # The laptop sleeps (pmset: 1565 sleep/wakes since boot on 2026-11-07): wall time runs on while the JVM is frozen,
    # so the first pass after a long sleep divides the CPU of a few busy minutes by hours and reads a working server as
    # one with no CPU since before the sleep. A gap that long is not a sample: the server starts over.
    def test_a_busy_server_across_a_laptop_sleep_starts_over(self):
        for clients, kwargs in (({}, {"ppid": 1, "kept": set(), "swap_free_mb": 10_000}), ({"/wt": [42]}, {})):
            state = {}
            A.sbt_stuck_reasons(7, "/wt", 100.0, clients, {}, state, 0, **kwargs)
            # busy for 5 min (5 s/min), then asleep for 8 h; the next pass runs on wake
            wake = (5 + 8 * 60) * 60
            self.assertEqual(A.sbt_stuck_reasons(7, "/wt", 125.0, clients, {}, state, wake, **kwargs), [])
            self.assertEqual(state["7"]["since"], wake)

    # A sleep under SBT_SAMPLE_MAX_GAP_MIN is still a gap: a pass, 25 min of sleep, the pass on wake. On the wall clock a
    # busy command's minute of CPU spread over 26 min reads as no CPU for 26 min, over the 20-min stall limit. The
    # reaper's clock is the awake one (CLOCK_UPTIME_RAW stops while the laptop sleeps), so on wake one minute passed.
    def orphans_pass(self, wt, wall, awake, cpu):
        procs = [(7, 1, "1:00:00", "/usr/bin/java -jar sbt-launch.jar"), (8, 99, "1:00", "sbtn compile")]
        saved = {k: getattr(A, k) for k in ("ps", "process_cwds", "sbt_serving", "run_cmd", "awake_now")}
        A.ps, A.process_cwds, A.sbt_serving = (lambda: procs), (lambda: {7: wt, 8: wt}), (lambda: {})
        A.run_cmd = lambda args, **kw: types.SimpleNamespace(
            returncode=0, stdout=f"7 1 {int(cpu // 60)}:{cpu % 60:05.2f}\n" if args[0] == "ps" else "")
        A.awake_now, A.time.time = (lambda: awake), (lambda: wall)
        os.environ["AGENTCTL_SBT_REAP"] = "1"
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out):
                A.cmd_orphans(argparse.Namespace(kill=False, safe=True, sbt_only=True))
        finally:
            os.environ["AGENTCTL_SBT_REAP"] = "0"
            for k, v in saved.items():
                setattr(A, k, v)
        return out.getvalue()

    def test_a_busy_command_across_a_short_sleep_is_not_stuck(self):
        with tempfile.TemporaryDirectory() as wt:
            os.mkdir(os.path.join(wt, ".git"))
            self.assertNotIn("no CPU", self.orphans_pass(wt, 1_000_000, 5_000, 600.0))
            # one awake minute at 5 s/min, then 25 min asleep
            self.assertNotIn("no CPU", self.orphans_pass(wt, 1_000_000 + 26 * 60, 5_000 + 60, 605.0))

    def test_a_clock_that_went_back_starts_over(self):
        state = {}
        A.sbt_stuck_reasons(7, "/wt", 100.0, {"/wt": [42]}, {}, state, 100 * 60)
        self.assertEqual(A.sbt_stuck_reasons(7, "/wt", 100.0, {"/wt": [42]}, {}, state, 10 * 60), [])
        self.assertEqual(state["7"]["since"], 10 * 60)

    def test_a_restarted_pid_with_less_cpu_starts_over(self):
        state = {}
        A.sbt_stuck_reasons(7, "/wt", 500.0, {}, {}, state, 0, ppid=1, kept=set(), swap_free_mb=10_000)
        self.assertEqual(
            A.sbt_stuck_reasons(7, "/wt", 3.0, {}, {}, state, 200 * 60, ppid=1, kept=set(), swap_free_mb=10_000), [])

    def test_a_serving_command_whose_owner_died_is_orphaned(self):
        serving = {"/wt": (os.getpid(), DEAD, "web/PageTest/runMain tools.FixtureServerMain")}
        why = A.sbt_stuck_reasons(7, "/wt", 1.0, {}, serving, {}, 0)
        self.assertEqual(len(why), 1)
        self.assertIn("FixtureServerMain", why[0])
        self.assertEqual(A.sbt_stuck_reasons(7, "/wt", 1.0, {}, {"/wt": (os.getpid(), os.getpid(), "x")}, {}, 0), [])

    def test_sbt_warm_serving_marks_are_read(self):
        with tempfile.TemporaryDirectory() as d:
            open(os.path.join(d, "abc.serving"), "w").write("123 - web/PageTest/runMain tools.FixtureServerMain /p\n")
            open(os.path.join(d, "abc.root"), "w").write("/wt\n")
            os.environ["KINOWO_SBT_WARM_STATE"] = d
            try:
                self.assertEqual(A.sbt_serving(), {"/wt": (123, None, "web/PageTest/runMain tools.FixtureServerMain /p")})
            finally:
                del os.environ["KINOWO_SBT_WARM_STATE"]

    def test_tree_cpu_counts_the_forked_test_jvm(self):
        self.assertEqual(A.tree_cpu(1, {1: 10.0, 2: 5.0, 3: 1.0}, {1: [2], 2: [3]}), 16.0)


if __name__ == "__main__":
    unittest.main()
