"""Review 2 leftovers: ONE reader of the Claude session id (B 15), ONE reading of a red's job names (B 3)."""
import os, sys, unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402


class SessionIdTest(unittest.TestCase):
    def test_the_session_is_the_environments_and_an_empty_one_is_none(self):
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "abc"}):
            self.assertEqual(A.claude_session_id(), "abc")
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": ""}):
            self.assertIsNone(A.claude_session_id())



class JobNamesTest(unittest.TestCase):
    def test_jobs_before_job_and_the_what_line_agrees(self):
        self.assertEqual(A.red_job_names({"jobs": ["a", "b"], "job": "c"}), ["a", "b"])
        self.assertEqual(A.red_job_names({"job": "c"}), ["c"])
        self.assertEqual(A.red_job_names({}), [])
        self.assertIn("[a, b]", A.red_what({"workflow": "W", "jobs": ["a", "b"], "last_run": {"number": 3}}))

    def test_what_a_red_has_seen_has_its_own_name(self):
        self.assertEqual(A.red_seen_job_names({"jobs_seen": ["x", "?"], "jobs": ["y"]}), {"x"})


if __name__ == "__main__":
    unittest.main()
