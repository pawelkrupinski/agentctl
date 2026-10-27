"""The stale-JVM rules folded in from reap-stale-jvms.sh (dev.kinowo.jvm-reaper, retired 2026-10-27): the same fixtures
as movies' scripts/reap-stale-jvms-test.sh, plus rules A and B, the guards, and `maintain`'s once-a-day sweep."""
import argparse, contextlib, io, os, subprocess, sys, tempfile, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402

SERVER = "/usr/bin/java -jar /x/sbt-launch.jar --detach-stdio"
ONESHOT = "/usr/bin/java -jar /x/sbt-launch.jar -batch test"
WORKER = "/usr/bin/java -cp x sbt.internal.worker1.WorkerMain"


def rules(rows, cwd=None, kept=(), ports=(), isdir=None):
    got = A.jvm_candidates(rows, cwd or {}, set(kept), set(ports), 0, isdir or os.path.isdir)
    return {pid: rule for pid, _pp, _a, _c, _p, rule, _args in got}


class JvmRulesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.t = self.tmp.name
        for d in ("live", "final", "shared", "kept", "unkept"):
            os.makedirs(os.path.join(self.t, d))

    def tearDown(self):
        self.tmp.cleanup()

    def p(self, d):
        return os.path.join(self.t, d)

    def test_rule_c_the_movies_spec_fixture(self):
        rows = [(101, 1, "??", "05:00:00", "00:00:10", SERVER), (102, 1, "??", "02:00:00", "00:00:10", SERVER),
                (103, 1, "??", "02:00:00", "00:00:10", ONESHOT), (104, 1, "??", "03:00:00", "00:00:10", ONESHOT),
                (105, 1, "??", "03:00:00", "00:00:10", SERVER), (106, 1, "??", "03:00:00", "00:00:10", SERVER)]
        cwd = {101: self.p("live"), 102: self.p("live"), 103: self.p("shared"), 104: self.p("shared"),
               105: self.p("final") + ".part", 106: self.p("removed")}
        # only the older duplicate and the server of a removed dir; never a one-shot run; a renamed .part is not gone
        self.assertEqual(rules(rows, cwd), {101: "dup-sbt-server", 106: "dup-sbt-gone-dir"})

    def test_rule_b_spares_a_kept_server_not_an_unkept_one_or_a_one_shot_run(self):
        rows = [(201, 1, "??", "02:00:00", "00:40:00", SERVER), (202, 1, "??", "02:00:00", "00:40:00", SERVER),
                (203, 1, "??", "02:00:00", "00:40:00", ONESHOT)]
        cwd = {201: self.p("kept"), 202: self.p("unkept"), 203: self.p("kept")}
        self.assertEqual(rules(rows, cwd, kept=[self.p("kept")]), {202: "hog", 203: "hog"})

    def test_rule_a_orphan_workers_after_ten_minutes_whatever_their_cpu(self):
        rows = [(301, 1, "??", "11:00", "0:00.5", WORKER), (302, 1, "??", "09:00", "0:00.5", WORKER),
                (303, 4242, "??", "05:00:00", "0:00.5", WORKER)]
        self.assertEqual(rules(rows), {301: "orphan"})

    def test_hogs_need_age_and_lifetime_cpu_and_no_tty(self):
        java = "/usr/bin/java -cp x Main"
        rows = [(401, 50, "??", "02:00:00", "00:10:00", java),     # 8.3%: hog
                (402, 50, "??", "01:00:00", "00:30:00", java),     # too young
                (403, 50, "??", "1-00:00:00", "30:00", java),      # 2%: lifetime average under the bar
                (404, 50, "ttys001", "02:00:00", "00:10:00", java)]  # a human's terminal
        self.assertEqual(rules(rows), {401: "hog"})

    def test_protected_names_and_a_quiet_listener_are_spared(self):
        rows = [(501, 50, "??", "3-00:00:00", "20:00:00", "/Applications/IntelliJ IDEA.app/jbr/bin/java idea"),
                (502, 50, "??", "02:00:00", "00:10:00", "/usr/bin/java -cp x Api"),
                (503, 50, "??", "02:00:00", "01:30:00", "/usr/bin/java -cp x Api"),
                (504, 50, "??", "02:00:00", "01:30:00", "/bin/zsh -c sleep")]  # out of scope
        self.assertEqual(rules(rows, ports=[502, 503]), {503: "hog"})  # 503 spins at 75%: no longer serving

    def test_times_parse_both_ps_layouts(self):
        self.assertEqual(A.tosec("69-23:47:44"), 69 * 86400 + 23 * 3600 + 47 * 60 + 44)
        self.assertEqual(A.tosec("17537:13.68"), 17537 * 60 + 13.68)

    def test_another_agents_working_process_is_never_killed(self):
        procs = [(10, 1, "", "/Users/x/.local/bin/claude"), (11, 10, "", "/bin/zsh -c sbt"), (12, 11, "", "java sbt"),
                 (20, 1, "", "/usr/bin/python3 /Users/x/bin/agentctl land"), (21, 20, "", "java sbt"),
                 (30, 1, "", "java orphan")]
        self.assertIn("claude", A.agent_work(12, procs))
        self.assertIn("agentctl land", A.agent_work(21, procs))
        self.assertIsNone(A.agent_work(30, procs))

    def test_orphans_lists_the_jvms_and_a_dry_run_kills_nothing(self):
        ps = os.path.join(self.t, "ps")
        with open(ps, "w") as f:
            f.write(f" 99999 1 ?? 11:00 0:00.5 {WORKER}\n")
        open(os.path.join(self.t, "cwd"), "w").close()
        open(os.path.join(self.t, "keptfile"), "w").close()
        env = {"AGENTCTL_JVM_PS": ps, "AGENTCTL_JVM_CWD": os.path.join(self.t, "cwd"),
               "AGENTCTL_JVM_KEPT": os.path.join(self.t, "keptfile")}
        os.environ.update(env)
        # the fixture's table only: the live machine's processes (busy under load, other agents' loops and servers,
        # some ending mid-scan) decide nothing here
        saved = (A.ps, A.process_cwds, A.owner_orphans, A.sbt_serving)
        A.ps, A.process_cwds, A.owner_orphans, A.sbt_serving = (lambda: []), (lambda: {}), (lambda kill: []), (lambda: {})
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out):
                A.cmd_orphans(argparse.Namespace(kill=False, safe=True))
        finally:
            A.ps, A.process_cwds, A.owner_orphans, A.sbt_serving = saved
            for k in env:
                os.environ.pop(k)
        self.assertIn("jvm       pid=99999 ppid=1 rule=orphan", out.getvalue())
        self.assertNotIn("killed", out.getvalue())


class MaintainTest(unittest.TestCase):
    def test_the_sweep_runs_once_a_day_from_its_hour(self):
        runs, saved = [], (A.cmd_orphans, A.cmd_sweep, A.HOME)
        tmp = tempfile.mkdtemp()
        A.cmd_orphans = lambda a: runs.append(("reap", a.kill, a.safe)) or 0
        A.cmd_sweep = lambda a: runs.append(("sweep", a.dry_run)) or 0
        A.HOME = tmp
        os.environ["AGENTCTL_SWEEP_HOUR"] = "0"
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                for _ in range(3):
                    A.cmd_maintain(argparse.Namespace(dry_run=False, sweep=False))
        finally:
            A.cmd_orphans, A.cmd_sweep, A.HOME = saved
            os.environ.pop("AGENTCTL_SWEEP_HOUR")
        self.assertEqual(runs.count(("reap", True, True)), 3)
        self.assertEqual(runs.count(("sweep", False)), 1)


if __name__ == "__main__":
    unittest.main()
