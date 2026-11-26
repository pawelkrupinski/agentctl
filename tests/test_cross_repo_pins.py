"""Where agentctl and the movies repository each hold a copy of one concept (a ps reading, an atomic JSON write), the
two must agree on what they both do. Each pin imports the movies side only where ~/projects/movies has it (MOVIES_ROOT)
and says it skipped otherwise: agentctl's CI stays standalone. The differences that remain are written down in the
assertions' comments, for a movies lane to take (agentctl never edits movies).

The files that ARE byte-equal copies (the baseline ratchet engine, the trailer placement rule, the design rule core) are
tests/test_vendored.py's. `lib/agentctl/trailers.py` is not the movies `scripts/ci/trailers.py`'s counterpart: it reads
a landing commit's trailers (Hold:, Fixes-Red:), where movies' judges a baseline's growth (quality/ratchet.py's engine now
vendors that one)."""
import importlib.util, json, os, sys, tempfile, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402,F401  (the harness: HOME, PATH, git config)

ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "devtools"))
import vendor  # noqa: E402
from agentctl import psparse, statefile  # noqa: E402


def movies_module(name):
    """scripts/lib/<name>.py of the movies checkout, imported; skips the test where it is not there."""
    movies = vendor.movies_root()
    path = os.path.join(movies, "scripts", "lib", name + ".py") if movies else ""
    if not os.path.isfile(path):
        raise unittest.SkipTest(f"movies has no scripts/lib/{name}.py here (no checkout, or not landed yet): pin not checked")
    sys.path.insert(0, os.path.dirname(path))
    spec = importlib.util.spec_from_file_location(f"movies_{name}", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class AtomicWriteTest(unittest.TestCase):
    def test_a_failed_write_leaves_the_old_file_and_no_temporary(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "state.json")
            statefile.write_json(path, {"a": 1})
            with self.assertRaises(TypeError):
                statefile.write_json(path, {"a": object()})
            self.assertEqual({"a": 1}, json.load(open(path)))
            self.assertEqual(["state.json"], os.listdir(d))

    def test_both_repos_write_the_same_bytes_and_clean_up_alike(self):
        atomic = movies_module("atomic")
        with tempfile.TemporaryDirectory() as d:
            ours, theirs = os.path.join(d, "ours.json"), os.path.join(d, "theirs.json")
            value = {"b": [1, 2], "a": {"x": None}}
            statefile.write_json(ours, value, indent=1, sort_keys=True)
            atomic.write_json(theirs, value, indent=1, sort_keys=True)
            self.assertEqual(open(theirs).read(), open(ours).read())
            with self.assertRaises(TypeError):
                atomic.write_json(theirs, {"a": object()})
            self.assertEqual(["ours.json", "theirs.json"], sorted(os.listdir(d)))


class PsTableTest(unittest.TestCase):
    CLOCKS = ("05:30", "12:34:56", "1-02:03:04", "00:00.50", "10-00:00:01")
    TEXT = "  101   1 /bin/sh -c  run it\n102 101 python3 x.py\nnot a process\n103 abc bad\n"

    def test_both_read_ps_clocks_alike(self):
        ps_table = movies_module("ps_table")
        for clock in self.CLOCKS:
            self.assertAlmostEqual(ps_table.clock_seconds(clock), psparse.tosec(clock), msg=clock)
        # they differ on garbage: movies' raises ValueError, agentctl's reads 0.0 (a ps line racing its process must not
        # end a watchdog tick) -- movies should take agentctl's tolerant reading
        self.assertEqual(0.0, psparse.tosec("garbled"))

    def test_both_read_a_process_listing_alike(self):
        ps_table = movies_module("ps_table")
        theirs = {p.pid: (p.ppid, p.args) for p in ps_table.parse(self.TEXT).values()}
        ours = {r.pid: (r.ppid, " ".join(r.args.split())) for r in psparse.parse_ps(self.TEXT, ("pid", "ppid", "args"))}
        self.assertEqual(theirs, ours)  # agentctl keeps an argument line's own spacing; movies collapses it


if __name__ == "__main__":
    unittest.main()
