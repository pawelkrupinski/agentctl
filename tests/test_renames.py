"""Design §14.5: the queue's commands under `land`, the processor under `tick`; the old `waiting` names answer for a
week, each naming its new one."""
import argparse, contextlib, io, os, subprocess, sys, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A, TOOL  # noqa: E402
from fake_clock import FakeClock, on_clock  # noqa: E402


class RenameTest(unittest.TestCase):
    def setUp(self):
        self.calls, self.saved = [], (A.cmd_waiting, A.queue_command)
        A.cmd_waiting = lambda w, clock, store: self.calls.append(w) or 0  # the processor's
        A.queue_command = lambda w, store: self.calls.append(w) or 0  # the queue's

    def tearDown(self):
        A.cmd_waiting, A.queue_command = self.saved

    def test_land_subcommands_are_the_queues_and_tick_is_the_processor(self):
        saved = os.environ.pop("AGENTCTL_ROLE", None)  # the laptop's `tick` (the sync); ci-runner's has its own clone
        self.addCleanup(os.environ.__setitem__, "AGENTCTL_ROLE", saved or "server")
        for argv, want in ((["land", "status"], "status"), (["land", "hold"], "pause"), (["land", "release"], "resume"),
                           (["land", "cancel", "x-12"], "cancel"), (["tick", "--once", "--repo", "/r"], "run"),
                           (["tick", "service", "status", "--repo", "/r"], "service")):
            self.calls.clear()
            self.assertEqual(A.main(argv), 0, argv)
            [w] = self.calls
            self.assertEqual((w.wcmd, w.renamed), (want, True), argv)
        self.assertEqual(self.calls[0].action, "status")

    def test_land_cancel_needs_the_id(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(A.main(["land", "cancel"]), 2)
        self.assertEqual(self.calls, [])

    def test_land_status_works_outside_any_git_checkout(self):
        import tempfile
        A.cmd_waiting, A.queue_command = self.saved
        seen, saved = [], (A.waiting_state, A.main_clone, A.movies_repo_path)
        A.movies_repo_path = lambda: "/the/movies/clone"
        A.main_clone = lambda p: p
        A.waiting_state = lambda repo: seen.append(repo) or (_ for _ in ()).throw(RuntimeError("stop"))
        try:
            with self.assertRaises(RuntimeError):
                A.cmd_waiting(argparse.Namespace(wcmd="status", repo=tempfile.gettempdir(), entry=None, renamed=True), *on_clock(FakeClock()))
        finally:
            A.waiting_state, A.main_clone, A.movies_repo_path = saved
        self.assertEqual(seen, ["/the/movies/clone"])

    def test_an_old_name_says_its_new_one(self):
        A.cmd_waiting, A.queue_command = self.saved
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            try:
                A.cmd_waiting(argparse.Namespace(wcmd="pause", repo="/nonexistent", entry=None), *on_clock(FakeClock()))
            except (Exception, SystemExit):
                pass
        self.assertIn("is now `agentctl land hold`", err.getvalue())


if __name__ == "__main__":
    unittest.main()


class HelpTest(unittest.TestCase):
    def test_help_is_the_usage_text(self):
        """`agentctl --help` opens with the usage text (2026-11-22: cli became a real module, and the parser's
        description, its module's __doc__, was None: the help lost every command's usage)."""
        r = subprocess.run([sys.executable, TOOL, "--help"], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("agentctl wait run <id>...", r.stdout)
        self.assertIn("Full reference, examples and the standard agent workflow: ~/bin/AGENTCTL.md", r.stdout)
