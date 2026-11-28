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


class AttemptTest(unittest.TestCase):
    """THE TRY-DEFAULT SHAPE (2026-11-25): a call's result, or a default on exactly the exceptions the call site names."""

    def test_the_result_or_the_default_on_a_named_exception(self):
        self.assertEqual(A.attempt(lambda: int("7"), 0, catch=(ValueError,)), 7)
        self.assertEqual(A.attempt(lambda: int("x"), 0, catch=(ValueError,)), 0)
        self.assertIsNone(A.attempt(lambda: {}["k"], None, catch=(KeyError, ValueError)))

    def test_an_exception_the_site_did_not_name_propagates(self):
        with self.assertRaises(KeyError):
            A.attempt(lambda: {}["k"], None, catch=(ValueError,))
        with self.assertRaises(SystemExit):  # Exception alone never swallows a NoToken
            A.attempt(lambda: sys.exit(3), None, catch=(Exception,))
        self.assertEqual(A.attempt(lambda: sys.exit(3), "no", catch=(Exception, SystemExit)), "no")

    def test_what_tells_the_failure_and_only_the_failure(self):
        import contextlib, io
        out = io.StringIO()
        with contextlib.redirect_stderr(out):
            self.assertEqual(A.attempt(lambda: 1, 0, catch=(OSError,), what="the cache"), 1)
            self.assertEqual(out.getvalue(), "")
            A.attempt(lambda: open("/nonexistent/x"), None, catch=(OSError,), what="the cache was not read")
            A.attempt(lambda: int("y" * 300), None, catch=(ValueError,), what="long")
        lines = out.getvalue().splitlines()
        self.assertRegex(lines[0], r"^agentctl: the cache was not read \(\[Errno 2\] No such file .*\)$")
        self.assertEqual(len(lines[1]), len("agentctl: long ()") + 160)


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
            old = A.epoch_now() - 10 * 86400
            for p in (sock, other):
                os.utime(p, (old, old))
            saved = (A.user_temp_dir, os.environ.get("AGENTCTL_SOCK_DIR"))
            A.user_temp_dir = lambda: tmp  # Linux: the user temp dir is /tmp, where the socket dir lives
            os.environ["AGENTCTL_SOCK_DIR"] = sock
            try:
                A.sweep_test_temp()
            finally:
                A.user_temp_dir = saved[0]
                if saved[1] is None:
                    os.environ.pop("AGENTCTL_SOCK_DIR")
                else:
                    os.environ["AGENTCTL_SOCK_DIR"] = saved[1]
            self.assertTrue(os.path.isdir(sock))
            self.assertFalse(os.path.isdir(other))

    def test_the_sweep_removes_tmps_dead_suite_roots_and_never_a_live_ones(self):
        """2026-11-22: 174 /private/tmp/agentctl-t-* roots older than 2 h (197 MB) fed a runaway fseventsd: the sweep
        read only the user temp dir and /tmp/agentctl-sock-*. Now /tmp's suite roots too -- never one whose runner is
        alive (its owner.pid), nor one any of whose entries changed within the bound."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            dead, live, busy = (os.path.join(tmp, f"agentctl-t-{n}") for n in ("dead", "live", "busy"))
            for p in (dead, live, busy):
                os.makedirs(os.path.join(p, "repo", "remote.git"))
            with open(os.path.join(live, "owner.pid"), "w") as f:
                f.write(str(os.getpid()))
            old = A.epoch_now() - 3 * 3600
            for p in (dead, live, busy, os.path.join(dead, "repo"), os.path.join(live, "repo"),
                      os.path.join(live, "owner.pid")):
                os.utime(p, (old, old))
            os.utime(busy, (old, old))  # its own mtime old, an entry of it written now: in use
            saved = A.user_temp_dir
            A.user_temp_dir = lambda: os.path.join(tmp, "user")
            try:
                n = A.sweep_test_temp(tmp_root=tmp)
            finally:
                A.user_temp_dir = saved
            self.assertEqual((os.path.isdir(dead), os.path.isdir(live), os.path.isdir(busy)), (False, True, True))
            self.assertEqual(n, 1)

    def test_a_garbled_knob_falls_back_to_its_default(self):
        os.environ["AGENTCTL_WATCH_POLL"] = "sixty"
        try:
            self.assertEqual(A.env_number("AGENTCTL_WATCH_POLL", 60), 60)
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



if __name__ == "__main__":
    unittest.main()
