"""THE JSON STATE FILES: one reader (read_record) and one atomic writer (write_json) for every part (2026-11-22: eleven
inline copies, three of them with a fixed `<path>.tmp` two writers could share, and three writing secrets 0600 by
hand)."""
import json, os, stat, sys, tempfile, threading, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402


class StateFilesTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.p = os.path.join(self.d, "sub", "x.json")

    def test_a_missing_or_torn_file_reads_as_the_default(self):
        self.assertIsNone(A.read_record(self.p))
        self.assertEqual(A.read_record(self.p, {"cap": 3}), {"cap": 3})
        os.makedirs(os.path.dirname(self.p))
        open(self.p, "w").write('{"torn": ')
        self.assertEqual(A.read_record(self.p, {}), {})
        open(self.p, "w").write("[1, 2]")
        self.assertEqual(A.read_record(self.p, {}), [1, 2])
        self.assertEqual(A.read_json_file(self.p), {})  # an object reader: a list is no object

    def test_a_write_is_whole_leaves_no_temporary_and_keeps_a_secret_private(self):
        A.write_json(self.p, {"a": 1})
        self.assertEqual(json.load(open(self.p)), {"a": 1})
        self.assertEqual(os.listdir(os.path.dirname(self.p)), ["x.json"])
        secret = os.path.join(self.d, "s.json")
        A.write_json(secret, {"token": "t"}, mode=0o600, sort_keys=True)
        self.assertEqual(stat.S_IMODE(os.stat(secret).st_mode), 0o600)

    def test_concurrent_writers_never_share_a_temporary(self):
        errors = []

        def write(n):
            try:
                for _ in range(50):
                    A.write_json(self.p, {"n": n, "pad": "x" * 2000})
            except OSError as e:
                errors.append(e)

        threads = [threading.Thread(target=write, args=(n,)) for n in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertIn(json.load(open(self.p))["n"], range(4))

    def test_the_best_effort_writer_swallows_an_unwritable_path(self):
        blocker = os.path.join(self.d, "file")
        open(blocker, "w").write("")
        A.write_json_file(os.path.join(blocker, "x.json"), {"a": 1})  # its directory is a file: nothing written, no raise


if __name__ == "__main__":
    unittest.main()
