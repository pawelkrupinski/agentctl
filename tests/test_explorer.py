"""The flip explorer's filing: a new flip -> one item + one change; the same again -> nothing; one the backlog names ->
nothing; an unreadable asset -> nothing."""
import os, subprocess, sys, tempfile, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402

HEAD = "venue\trawTitle\tcountry\tseed\tfirstSeen\tslice\ttakes\n"
ROW = "Proof Kino\tProof Flip\tde\t8\t2026-11-04\tde:proof\t2\n"


class ExplorerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="agentctl-expl-")
        self.st = A.Store(path=os.path.join(self.tmp, "store.db"))
        self.repo = os.path.join(self.tmp, "movies")
        subprocess.run(["git", "init", "-q", self.repo], check=True)
        self.backlog = ""
        self.filed = []
        for n in ("explorer_backlog", "git"):
            self.addCleanup(setattr, A, n, getattr(A, n))
        A.explorer_backlog = lambda repo: self.backlog
        real = A.git
        A.git = lambda repo, *a, **k: (subprocess.CompletedProcess(a, 0, "", "") if a[:1] == ("fetch",) else real(repo, *a, **k))
        self.asset = (HEAD + ROW).encode()
        self.t = 1_000_000.0

    def filer(self, o_r, repo, rows, now_):
        self.filed.append(rows)
        return "/wt"

    def tick(self, fetch=None):
        self.t += 7200
        return A.explorer_tick("o/movies", self.repo, st=self.st, now_=self.t, filer=self.filer,
                               fetch=fetch or (lambda o, tag, name: self.asset))

    def test_new_flip_files_one_item_and_one_change(self):
        self.tick()
        self.assertEqual(len(self.filed), 1)
        its = self.st.items("explorer-flip")
        self.assertEqual([i["id"] for i in its], ["de:Proof Kino|Proof Flip"])
        self.assertEqual(its[0]["data"]["seed"], "8")

    def test_the_same_flip_again_files_nothing(self):
        self.tick()
        self.tick()
        self.assertEqual(len(self.filed), 1)
        self.assertEqual(len(self.st.items("explorer-flip")), 1)

    def test_an_already_backlogged_flip_files_nothing(self):
        self.backlog = "drift\tde\tProof Kino\tProof Flip\n"
        self.tick()
        self.assertEqual(self.filed, [])
        self.assertEqual(self.st.items("explorer-flip"), [])

    def test_an_open_item_closes_when_main_names_it(self):
        self.tick()
        self.backlog = "drift\tde\tProof Kino\tProof Flip\n"
        self.tick()
        self.assertEqual(self.st.items("explorer-flip", ["open"]), [])
        self.assertEqual(len(self.filed), 1)

    def test_unreadable_asset_does_nothing(self):
        def boom(o, tag, name):
            raise A.ForgeHTTPError("gh release download: not found")
        out = self.tick(boom)
        self.assertEqual(self.filed, [])
        self.assertIn("nothing filed", out[0])
        self.asset = b"\xff\xfe garbage"
        self.tick()
        self.asset = b"<html>nope</html>"
        self.tick()
        self.assertEqual(self.filed, [])
        self.assertEqual(self.st.items("explorer-flip"), [])

    def test_header_only_is_nothing_and_hourly_throttle(self):
        self.asset = HEAD.encode()
        self.tick()
        self.assertEqual(self.filed, [])
        r = A.explorer_tick("o/movies", self.repo, st=self.st, now_=self.t + 10, filer=self.filer,
                            fetch=lambda *a: (HEAD + ROW).encode())
        self.assertEqual((r, self.filed), ([], []))


if __name__ == "__main__":
    unittest.main()
