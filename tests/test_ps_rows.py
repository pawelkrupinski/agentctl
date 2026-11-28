"""THE PROCESS TABLE, PARSED ONCE (procs.ps_rows): land's group tree, maint's cpu tree, procwatch's rows, the governor's
busy cores, the orphan sweep's rows, jvmreap's busy-duplicate look, core's zombie children and lander's ancestry each
read `ps` output with their own columns and their own tolerance for a short or garbled row. Each reading is pinned on a
fixed `ps` text; they all ride one parser now."""
import os, subprocess, sys, unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402


def ran(text, seen=None):
    def run(args, **kw):
        if seen is not None:
            seen.append(list(args))
        return subprocess.CompletedProcess(args, 0, text, "")
    return run


class PsReadingsTest(unittest.TestCase):
    def test_the_group_tree_rows_keep_a_missing_stat_and_drop_a_short_or_extra_row(self):
        text = "  77 1 77 1-00:00:01 S\n  78 1 78 9:00\n  79 77 79\n  80 1 80 0:01 R extra\n  81 1 81 0:02 Z+\n"
        with mock.patch.object(A, "run_cmd", ran(text)):
            rows = A.ps_rows(("pid", "ppid", "pgid", "time", "stat"))
        self.assertEqual([(r.pid, r.ppid, r.pgid, r.cpu_seconds, r.stat) for r in rows],
                         [(77, 1, 77, 86401.0, "S"), (78, 1, 78, 540.0, ""), (81, 1, 81, 2.0, "Z+")])

    def test_a_group_runs_the_hook_when_a_row_of_it_names_pre_push(self):
        text = "77 git push\n 78 /w/hooks/pre-push origin url\n79 /w/hooks/pre-push-x\n"
        with mock.patch.object(A, "run_cmd", ran(text)):
            self.assertTrue(A.group_runs_hook(78))
            self.assertFalse(A.group_runs_hook(77))
            self.assertFalse(A.group_runs_hook(79))

    def test_the_cpu_tree_sums_each_process_and_groups_children_by_parent(self):
        text = "  10 1 1:00\n  11 10 0:02.5\n  12 10 1-00:00:00\nbad row\n  13 1\n  x 1 0:01\n"
        cpu, kids = A.ps_cpu_tree(A.FixedMachine({"ps": text, "ps_cols": ["pid", "ppid", "time"]}))
        self.assertEqual(cpu, {10: 60.0, 11: 2.5, 12: 86400.0})
        self.assertEqual(kids, {1: [10], 10: [11, 12]})

    def test_the_procwatch_rows_take_the_command_name_and_skip_a_garbled_row(self):
        text = "  10 1 2048 3.5 /usr/bin/java\n  x 1 2 3 foo\n  11 10 100\n  12 10 100 0.0 /a/b c\n"
        self.assertEqual(A.watched_process_rows(A.FixedMachine({"ps": text})), [(10, 1, 2048, 3.5, "java"), (12, 10, 100, 0.0, "b c")])

    def test_the_busy_cores_sum_pcpu_less_waiters_and_idle_claude(self):
        text = "50.0 /bin/java -x\n100 agentctl gov admit --fd 3\n2.0 /usr/bin/claude\nx y\n20.0\n150 node\n"
        with mock.patch.object(A, "run_cmd", ran(text)):
            self.assertAlmostEqual(A.ps_busy_cores(A.HostMachine().process_rows(("pcpu", "args"))), 2.0)

    def test_the_orphan_sweep_rows_have_pid_ppid_etime_command(self):
        text = " 10 1 01:02 /usr/bin/java -Xmx1g\n 11 x 01:02 bad\n 12 10 5-01:02:03 sbt a b\n 13 1 00:01\n"
        with mock.patch.object(A, "run_cmd", ran(text)):
            got = A.ps()
        self.assertEqual(len(got), 2)  # a garbled pid is one skipped row, never a crash of the sweep (as every other reader)
        self.assertEqual(got[0], (10, 1, "01:02", "/usr/bin/java -Xmx1g"))
        self.assertEqual(got[-1], (12, 10, "5-01:02:03", "sbt a b"))

    def test_a_busy_duplicate_reads_one_pids_cpu_percent(self):
        def machine(text):
            return A.FixedMachine({"ps": text, "ps_cols": ["pid", "pcpu"]})
        self.assertIn("busy now", A._busy_duplicate(5, "dup-sbt-server", machine("5 12.5\n")))
        self.assertIsNone(A._busy_duplicate(5, "dup-sbt-server", machine("5 1.0\n")))
        self.assertIsNone(A._busy_duplicate(5, "dup-sbt-server", machine("garbage\n")))

    def test_zombie_children_are_this_processes_children_in_state_z(self):
        text = "  20 7 Z\n  21 7 S\n  22 8 Z+\n  23 7 Z+\n  24 7\n"
        runner = mock.Mock(run=lambda argv, **kw: subprocess.CompletedProcess(argv, 0, text, ""))
        with mock.patch.object(A, "process_runner", lambda: runner):
            self.assertEqual(A.zombie_children(7), [20, 23])

    def test_the_ancestry_walks_parents_by_ppid_and_stops_at_a_bad_row(self):
        table = {"5": "3 bash -c land", "3": "2 zsh", "2": "garbage"}
        runner = mock.Mock(run=lambda argv, **kw: subprocess.CompletedProcess(argv, 0 if argv[-1] in table else 1,
                                                                                 table.get(argv[-1], "") + "\n", ""))
        with mock.patch.object(A, "process_runner", lambda: runner), mock.patch("os.getppid", lambda: 5):
            self.assertEqual(A.process_ancestry(), ["bash -c land", "zsh"])


if __name__ == "__main__":
    unittest.main()
