"""The best-effort steps tell their failures (statefile.note_failure, unlink_file) and the fixers' landing look is paced
by an object its wiring owns (fixerland.LandWatchPace), not by a module global."""
import contextlib
import io
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402


class UnlinkFileTest(unittest.TestCase):
    def told(self, call):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            result = call()
        return result, err.getvalue()

    def test_a_file_goes_and_a_missing_one_is_quietly_already_gone(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "x")
            open(p, "w").close()
            self.assertEqual(self.told(lambda: A.unlink_file(p)), (True, ""))
            self.assertEqual(self.told(lambda: A.unlink_file(p)), (False, ""))

    def test_a_file_that_cannot_be_removed_is_told_not_raised(self):
        with tempfile.TemporaryDirectory() as d:
            os.mkdir(os.path.join(d, "dir"))  # unlinking a directory is an OSError that is not FileNotFoundError
            gone, said = self.told(lambda: A.unlink_file(os.path.join(d, "dir")))
        self.assertFalse(gone)
        self.assertIn("agentctl: could not remove", said)

    def test_a_state_file_that_cannot_be_written_is_told(self):
        with tempfile.TemporaryDirectory() as d:
            blocker = os.path.join(d, "f")
            open(blocker, "w").close()
            _, said = self.told(lambda: A.write_json_file(os.path.join(blocker, "x.json"), {}))
        self.assertIn("agentctl: could not write", said)


class LandWatchPaceTest(unittest.TestCase):
    def test_a_look_is_due_once_per_interval_and_each_pace_is_its_own(self):
        pace = A.LandWatchPace()
        self.assertTrue(pace.due(1000.0, 60))
        self.assertFalse(pace.due(1030.0, 60))
        self.assertTrue(pace.due(1060.0, 60))
        self.assertTrue(A.LandWatchPace().due(1030.0, 60))  # another loop's pace is not this one's


if __name__ == "__main__":
    unittest.main()
