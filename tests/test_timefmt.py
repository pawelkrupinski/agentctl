"""THE TIME FORMATS (timefmt.py), one copy each (2026-11-22: the ISO-stamp-to-epoch parse was written out eight
times, four of them strict `%Y-%m-%dT%H:%M:%SZ` strptime and four fromisoformat; HH:MMZ and the day-and-minute
stamp were inline in four places)."""
import os, sys, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402


class TimeFormatsTest(unittest.TestCase):
    def test_an_iso_stamp_reads_as_its_epoch_and_anything_else_as_none(self):
        self.assertEqual(A.iso_epoch("1970-01-01T00:01:40Z"), 100.0)
        self.assertEqual(A.iso_epoch("1970-01-01T00:01:40+00:00"), 100.0)
        self.assertEqual(A.iso_epoch("1970-01-01T00:01:40"), 100.0)  # no zone: UTC, never the laptop's local time
        for bad in (None, "", "?", "yesterday", 12):
            self.assertIsNone(A.iso_epoch(bad), bad)

    def test_the_stamps_round_trip(self):
        self.assertEqual(A.iso_at(100.4), "1970-01-01T00:01:40Z")
        self.assertEqual(A.iso_epoch(A.iso_at(86400 * 365)), 86400 * 365)
        self.assertEqual(A.hhmmz(3600 * 13 + 60 * 5), "13:05Z")
        self.assertEqual(A.day_hhmmz("90061"), "1970-01-02 01:01Z")


if __name__ == "__main__":
    unittest.main()
