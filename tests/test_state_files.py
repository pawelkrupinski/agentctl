"""THE JSON STATE FILES: one reader (read_record) and one atomic writer (write_json) for every part (2026-11-22: eleven
inline copies, three of them with a fixed `<path>.tmp` two writers could share, and three writing secrets 0600 by
hand)."""
import json, os, stat, sys, tempfile, threading, unittest
from unittest import mock

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

    def test_a_text_file_that_cannot_be_read_reads_as_the_default(self):
        self.assertEqual(A.read_text_or(self.p, "none"), "none")  # missing
        self.assertEqual(A.read_text_or(self.d, ""), "")  # a directory
        os.makedirs(os.path.dirname(self.p))
        open(self.p, "w").write("a\nb")
        self.assertEqual(A.read_text_or(self.p, ""), "a\nb")
        with self.assertRaises(TypeError):  # only OSError is absorbed: no path is a caller's bug
            A.read_text_or(None, "")

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


class JsonStateReadersTest(unittest.TestCase):
    """Every part reading a JSON state file reads it through statefile: what each does on a missing, torn or
    foreign-shaped file is pinned here (the readers that were `json.load(open(...))` inline)."""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.p = os.path.join(self.d, "x.json")
        self.said = []

    def write(self, text):
        with open(self.p, "w") as f:
            f.write(text)

    def test_the_runner_queue_is_none_when_missing_torn_foreign_or_stale(self):
        os.environ["AGENTCTL_RUNNERS_PAGE_CACHE"] = self.p
        try:
            self.assertIsNone(A.runner_queue(1000.0))
            for text in ('{"ok": ', "[1]", "{}", '{"ok": "x"}'):
                self.write(text)
                self.assertIsNone(A.runner_queue(1000.0), text)
            self.write('{"ok": 900, "running": {}}')
            self.assertEqual(A.runner_queue(1000.0), {"ok": 900, "running": {}})
            self.assertIsNone(A.runner_queue(900 + A.RUNNERS_PAGE_MAX_AGE + 1))
        finally:
            del os.environ["AGENTCTL_RUNNERS_PAGE_CACHE"]

    def test_the_claude_trust_gap_is_none_for_a_missing_torn_or_foreign_config(self):
        for text in (None, '{"projects": ', "[1]", '{"projects": 3}'):
            if text is not None:
                self.write(text)
            with mock.patch.object(A, "claude_config_path", lambda: self.p):
                self.assertIsNone(A.claude_trust_gap(self.d), text)

    def test_unreadable_families_apply_none_and_say_why(self):
        with mock.patch.object(A, "err", self.said.append):
            self.assertEqual(A.load_families(self.p), [])
            self.write("{")
            self.assertEqual(A.load_families(self.p), [])
            self.write('{"families": [{"name": "a", "jobs": ["x.*"]}, {"jobs": []}]}')
            got = A.load_families(self.p)
        self.assertEqual([f["name"] for f in got], ["a"])
        self.assertEqual(len(self.said), 2)
        self.assertTrue(all("unreadable" in m and self.p in m for m in self.said))

    def test_an_unreadable_repo_list_says_why_and_watches_the_primary(self):
        with mock.patch.object(A, "err", self.said.append), mock.patch.dict(os.environ, {"AGENTCTL_RED_REPOS": self.p}):
            self.write("{")
            repos = A.red_repos()
        self.assertEqual(len(self.said), 1)
        self.assertIn("unreadable", self.said[0])
        self.assertIsNotNone(repos)

    def test_the_store_token_registry_is_empty_when_missing_and_raises_when_torn(self):
        with mock.patch.object(A, "store_tokens_path", lambda: self.p):
            self.assertEqual(A.store_tokens_load(), {})
            self.write('{"t": 1}')
            self.assertEqual(A.store_tokens_load(), {"t": 1})
            self.write("{")
            with self.assertRaises(ValueError):
                A.store_tokens_load()


if __name__ == "__main__":
    unittest.main()
