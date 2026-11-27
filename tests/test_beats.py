"""The beat files: a loop's last sign of life and its age."""
import gc, os, sys, unittest, warnings

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402


class BeatAgeTest(unittest.TestCase):
    def test_a_written_beat_has_an_age_and_a_missing_one_none(self):
        A.write_beat("beats-test")
        self.assertIsNotNone(A.beat_age("beats-test"))
        self.assertIsNone(A.beat_age("beats-never-written"))

    def test_reading_the_age_leaves_no_file_open(self):
        A.write_beat("beats-test")
        with warnings.catch_warnings(record=True) as seen:
            warnings.simplefilter("always")
            A.beat_age("beats-test")
            gc.collect()
        self.assertEqual([w for w in seen if issubclass(w.category, ResourceWarning)], [])


if __name__ == "__main__":
    unittest.main()
