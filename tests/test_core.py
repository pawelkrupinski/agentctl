"""The core helpers every part shares: one bounded subprocess runner."""
import os, sys, time, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402


class RunCmdTest(unittest.TestCase):
    def test_a_hung_command_reads_as_exit_124_never_a_hung_caller(self):
        t0 = time.monotonic()
        r = A.run_cmd(["sleep", "30"], timeout=0.5)
        self.assertLess(time.monotonic() - t0, 5)
        self.assertEqual((r.returncode, r.stdout), (124, ""))
        self.assertEqual(A.run_cmd(["sleep", "30"], timeout=0.5, text=False).stdout, b"")

    def test_output_is_captured_and_stdin_closed(self):
        r = A.run_cmd(["sh", "-c", "cat; echo out"])
        self.assertEqual((r.returncode, r.stdout), (0, "out\n"))
        self.assertEqual(A.run_cmd(["cat"], input="x").stdout, "x")

    def test_a_hung_gov_probe_never_holds_the_dispatch_gate(self):
        saved = os.environ.get("AGENTCTL_GOV_FREE_PROBE")
        os.environ["AGENTCTL_GOV_FREE_PROBE"] = "sleep 120"
        saved_run = A.run_cmd
        A.run_cmd = lambda a, timeout=60, **kw: saved_run(a, timeout=0.5, **kw)
        try:
            self.assertIsNone(A.free_gb())
        finally:
            A.run_cmd = saved_run
            if saved is None:
                os.environ.pop("AGENTCTL_GOV_FREE_PROBE")
            else:
                os.environ["AGENTCTL_GOV_FREE_PROBE"] = saved



class GitDecodeTest(unittest.TestCase):
    def test_git_output_that_is_not_utf8_never_crashes_a_git_call(self):
        # 2026-11-03: charlie-msi carried a Latin-1 "\xa3"; core.git's strict UTF-8 decode raised
        # UnicodeDecodeError in rebase_in_tree on every executor start (174 times), and the stuck entry held the push slot.
        import subprocess, tempfile
        d = tempfile.mkdtemp()
        subprocess.run(["git", "init", "-q", d], check=True)
        with open(os.path.join(d, "prices.txt"), "wb") as f:
            f.write(b"price \xa3 5\n")
        subprocess.run(["git", "-C", d, "add", "prices.txt"], check=True)
        subprocess.run(["git", "-C", d, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "add"], check=True)
        out = A.git(d, "show", "HEAD:prices.txt")
        self.assertIn("price", out)


class HostDifferencesTest(unittest.TestCase):
    def test_the_upkeep_sweep_never_removes_the_processors_socket_dir_where_the_user_tmp_is_tmp(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            sock = os.path.join(tmp, "agentctl-501")
            other = os.path.join(tmp, "agentctl-test-x")
            os.makedirs(sock)
            os.makedirs(other)
            old = time.time() - 10 * 86400
            for p in (sock, other):
                os.utime(p, (old, old))
            saved = (A.user_tmp_dir, os.environ.get("AGENTCTL_SOCK_DIR"))
            A.user_tmp_dir = lambda: tmp  # Linux: the user temp dir is /tmp, where the socket dir lives
            os.environ["AGENTCTL_SOCK_DIR"] = sock
            try:
                A.sweep_test_tmp()
            finally:
                A.user_tmp_dir = saved[0]
                if saved[1] is None:
                    os.environ.pop("AGENTCTL_SOCK_DIR")
                else:
                    os.environ["AGENTCTL_SOCK_DIR"] = saved[1]
            self.assertTrue(os.path.isdir(sock))
            self.assertFalse(os.path.isdir(other))

    def test_a_garbled_knob_falls_back_to_its_default(self):
        os.environ["AGENTCTL_WATCH_POLL"] = "sixty"
        try:
            self.assertEqual(A.env_num("AGENTCTL_WATCH_POLL", 60), 60)
        finally:
            os.environ.pop("AGENTCTL_WATCH_POLL")
        self.assertTrue(A.TOOL_PATH.endswith("agentctl") and os.path.isfile(A.TOOL_PATH))



class PsTimeTest(unittest.TestCase):
    def test_one_parser_for_ps_times_and_a_day_is_a_day(self):
        self.assertEqual(A.tosec("1-02:03:04"), 86400 + 2 * 3600 + 3 * 60 + 4)
        self.assertEqual(A.tosec("03:04.50"), 184.5)
        saved = A.run_cmd
        # pid ppid pgid time: the group's leader 77, a process of group 78 (another's), 79 the leader's child in a
        # group of its own (counted: the step's tree, issue 20261108T232313-7384d0)
        A.run_cmd = lambda args, **kw: A.subprocess.CompletedProcess(
            args, 0, "  77 1 77 1-00:00:01\n  78 1 78 9:00\n  79 77 79 0:02\n", "")
        try:
            self.assertEqual(A.group_cpu_seconds(77), 86403)
        finally:
            A.run_cmd = saved



class MemStatsTest(unittest.TestCase):
    def test_linux_reads_proc_meminfo(self):
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix="meminfo", delete=False) as f:
            f.write("MemTotal:       33554432 kB\nMemFree:  1000 kB\nMemAvailable:    8388608 kB\n"
                    "SwapTotal:       2097152 kB\nSwapFree:        1048576 kB\n")
        real = A.read_meminfo
        A.read_meminfo = lambda path="/proc/meminfo": real(f.name)
        fixed = os.environ.pop("AGENTCTL_MACHINE", None)  # this test reads the reader itself, not the harness's machine
        try:
            m = A.mem_stats()
            self.assertEqual((m["ram_gb"], m["free_gb"], m["pressure"]), (32.0, 8.0, 75))
            self.assertEqual((m["swap_used_mb"], m["swap_total_mb"]), (1024.0, 2048.0))
            self.assertEqual(A.ram_gb(), 32.0)
        finally:
            A.read_meminfo = real
            if fixed is not None:
                os.environ["AGENTCTL_MACHINE"] = fixed
            os.unlink(f.name)


if __name__ == "__main__":
    unittest.main()
