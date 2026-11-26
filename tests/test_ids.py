"""ENTRY IDS ARE STAMPED IN UTC (ids.stamp). Seven sites built `YYYYMMDD-HHMMSS` from the machine's LOCAL time: the laptop
(CET) and ci-runner (UTC) minted ids for one queue in two zones. Each is pinned under a non-UTC TZ."""
import glob, os, re, sys, time, unittest
from types import SimpleNamespace as NS
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402

EPOCH = 1795000000.0  # 2026-11-18 11:06:40 UTC
UTC_ENTRY, UTC_FILE = "20261118-110640", "20261118T110640"


class LocalZone:
    """The process in America/New_York (UTC-5 in November) for the block."""

    def __enter__(self):
        self.old = os.environ.get("TZ")
        os.environ["TZ"] = "America/New_York"
        time.tzset()
        return self

    def __exit__(self, *a):
        if self.old is None:
            os.environ.pop("TZ")
        else:
            os.environ["TZ"] = self.old
        time.tzset()


class IdStampTest(unittest.TestCase):
    def test_a_direct_land_record_id_carries_the_utc_stamp(self):
        got = []
        with LocalZone(), mock.patch.object(A, "epoch_now", lambda: EPOCH), mock.patch.object(A, "lander_put", lambda *a, **k: got.append(a)), \
                mock.patch.object(A, "lander_record", lambda repo: {}), mock.patch.object(A, "no_vault", mock.MagicMock()):
            cid = A.lander_note_direct(None, "/r/movies", "a" * 40, [])
        self.assertEqual(cid, f"direct-{UTC_ENTRY}-movies-aaaaaaaaaa")

    def test_a_thread_dump_file_name_carries_the_utc_stamp(self):
        runner = NS(run=lambda *a, **k: NS(returncode=0, stdout="dump", stderr=""))
        with LocalZone(), mock.patch.object(A, "epoch_now", lambda: EPOCH), mock.patch.object(A, "process_runner", lambda: runner), \
                mock.patch.object(A, "state_root", lambda *p: os.path.join(os.environ["HOME"], "dumps")):
            path = A.thread_dump(77, "/w")
        self.assertEqual(os.path.basename(path), f"77-{UTC_FILE}.txt")

    def test_an_entry_id_age_reads_its_stamp_as_utc(self):
        with LocalZone():
            self.assertEqual(A.entry_id_age(f"{UTC_ENTRY}-movies-1", EPOCH + 90), 90)
        self.assertIsNone(A.entry_id_age("direct-nothing", EPOCH))

    def test_the_stamp_formats(self):
        with LocalZone():
            self.assertEqual(A.stamp("entry", EPOCH), UTC_ENTRY)
            self.assertEqual(A.stamp("file", EPOCH), UTC_FILE)
            self.assertEqual(A.stamp("event", EPOCH + 0.5), UTC_FILE + "500000")
            with mock.patch.object(A, "epoch_now", lambda: EPOCH):
                self.assertEqual(A.stamp("entry"), UTC_ENTRY)

    def test_no_module_stamps_an_id_from_the_machines_local_time(self):
        """The seven queue / clone / dump / inbox sites call ids.stamp: no `fromtimestamp(...)` without a zone is left."""
        lib = os.path.join(os.path.dirname(HERE), "lib", "agentctl")
        bad = []
        for path in sorted(glob.glob(os.path.join(lib, "*.py"))):
            with open(path) as f:
                lines = f.readlines()
            for n, line in enumerate(lines, 1):
                if re.search(r"fromtimestamp\([^)]*\)\.strftime", line) and "timezone.utc" not in line:
                    bad.append(f"{os.path.basename(path)}:{n}")
        self.assertEqual(bad, [])


if __name__ == "__main__":
    unittest.main()
