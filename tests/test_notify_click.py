"""Clickable owner notifications (the owner, 2026-11-03): URL per item kind, and the terminal-notifier command."""
import os, sys, unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402
from harness import case_context  # noqa: E402

JOBS = [{"name": "unit", "databaseId": 11, "status": "completed", "conclusion": "success", "steps": []},
        {"name": "convergence", "databaseId": 22, "status": "completed", "conclusion": "failure",
         "steps": [{"number": 1, "conclusion": "success"}, {"number": 7, "conclusion": "failure"}]}]
RED = {"repo": None, "jobs": ["convergence"], "last_run": {"databaseId": 99, "number": 5}}


class UrlTest(unittest.TestCase):
    def test_red_with_step(self):
        self.assertEqual(A.red_url(RED, lambda: JOBS, ctx=case_context()), f"https://github.com/{A.RED_PRIMARY}/actions/runs/99/job/22#step:7:1")

    def test_red_without_step(self):
        jobs = [{"name": "convergence", "databaseId": 22, "conclusion": "failure", "steps": []}]
        self.assertEqual(A.red_url(RED, lambda: jobs, ctx=case_context()), f"https://github.com/{A.RED_PRIMARY}/actions/runs/99/job/22")

    def test_a_runs_jobs_are_read_once_not_per_notification(self):
        """2026-11-03: a `gh run view` per notification (10-20 s each) put the notifier 3 h behind (W6)."""
        calls = []
        case_context().runs.clear()
        with mock.patch.object(A, "gh_json", lambda *a: calls.append(a) or {"jobs": JOBS}):
            for _ in range(3):
                self.assertTrue(A.red_url(RED, ctx=case_context()).endswith("/job/22#step:7:1"))
        self.assertEqual(len(calls), 1)

    def test_red_jobs_unreadable_falls_back_to_run(self):
        def boom():
            raise RuntimeError("gh down")
        self.assertEqual(A.red_url(RED, boom, ctx=case_context()), f"https://github.com/{A.RED_PRIMARY}/actions/runs/99")

    def test_ask_without_run_is_a_local_page(self):
        with mock.patch.dict(os.environ, {"AGENTCTL_STATE_ROOT": self.enterContext(__import__("tempfile").TemporaryDirectory())}):
            u = A.notification_url({"kind": "red.ask", "refs": {"key": "k1", "verdict": "WAITING ON YOU: x <b>"}}, A.CurrentStore(A.process_store), ctx=case_context())
        self.assertTrue(u.startswith("file://") and u.endswith(".html#question"))
        self.assertIn("&lt;b&gt;", open(u[7:].split("#")[0]).read())

    def test_other_events_have_no_url(self):
        self.assertIsNone(A.notification_url({"kind": "x", "refs": {}}, A.CurrentStore(A.process_store), ctx=case_context()))


class NotifierTest(unittest.TestCase):
    def test_argv(self):
        a = A.notifier_argv("/tn", "t", "m", "https://x/y#step:7:1", "g")
        self.assertEqual(a[a.index("-group") + 1], "g")
        self.assertEqual(a[a.index("-execute") + 1], "open -a 'Google Chrome' 'https://x/y#step:7:1'")

    def test_notify_uses_notifier_then_falls_back(self):
        env = {k: v for k, v in os.environ.items() if k != "AGENTCTL_NOTIFY"}
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(A.sys, "platform", "darwin"), \
                mock.patch.object(A, "notifier_bin", return_value="/tn"), mock.patch.object(A.subprocess, "run") as run:
            run.return_value.returncode = 0
            A.notify("t", "m", url="https://u", group="g")
            self.assertEqual(run.call_count, 1)
            self.assertEqual(run.call_args[0][0][0], "/tn")
            run.reset_mock()
            with mock.patch.object(A, "notifier_bin", return_value=None):
                A.notify("t", "m", url="https://u")
            self.assertEqual(run.call_args[0][0][0], "osascript")


if __name__ == "__main__":
    unittest.main()
