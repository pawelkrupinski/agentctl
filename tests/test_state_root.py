"""ONE state root (the owner, 2026-10-28): every agentctl path resolves under $AGENTCTL_STATE_ROOT at each use, so a
test (and its child processes) never touches the real ~/.cache/agentctl; test_agentctl's atexit guard fails a run
that leaks into it anyway."""
import contextlib, io, os, re, sys, tempfile, time, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A, TOOL  # noqa: E402


class StateRootTest(unittest.TestCase):
    def test_no_path_bypasses_the_state_root(self):
        with open(TOOL) as f:
            lines = f.read().splitlines()
        start = next(i for i, l in enumerate(lines) if l.startswith("def state_root"))
        end = next(i for i in range(start + 1, len(lines)) if lines[i].startswith("def "))
        offenders = [i + 1 for i, l in enumerate(lines) if '".cache", "agentctl"' in l and not start <= i < end]
        self.assertEqual(offenders, [], "build agentctl paths with state_root()")

    def test_every_accessor_follows_the_root_at_each_use(self):
        saved = os.environ.get("AGENTCTL_STATE_ROOT")
        root = tempfile.mkdtemp()
        os.environ["AGENTCTL_STATE_ROOT"] = root
        try:
            for path in (A.waiting_dir(), A.Store().path, A.sweep_dir(), A.reap_log_path(), A.gate_dirty_path(),
                         A.state_root("maintain.json")):
                self.assertTrue(path.startswith(root), path)
        finally:
            os.environ["AGENTCTL_STATE_ROOT"] = saved

    def test_the_test_run_itself_is_under_a_temp_root(self):
        self.assertNotEqual(os.path.realpath(A.state_root()),
                            os.path.realpath(os.path.join(os.path.expanduser("~"), ".cache", "agentctl")))


class CachedSummaryTest(unittest.TestCase):
    """`ci-why --summary --cached` (the status line's, every 15 s) does no live work: no subprocess, no Forgejo."""
    def run_cached(self, content):
        path = os.path.join(tempfile.mkdtemp(), "gate-summary.txt")
        with open(path, "w") as f:
            f.write(content)
        saved = (os.environ.get("AGENTCTL_GATE_SUMMARY"), A.subprocess.run, A.subprocess.Popen, A.ci_why)

        def boom(*a, **k):
            raise AssertionError("live work on the cached path")
        os.environ["AGENTCTL_GATE_SUMMARY"] = path
        A.subprocess.run = A.subprocess.Popen = A.ci_why = boom
        out = io.StringIO()
        try:
            t0 = time.monotonic()
            with contextlib.redirect_stdout(out):
                rc = A.main(["ci-why", "--summary", "--cached", "--repo", "movies"])
            took = time.monotonic() - t0
        finally:
            A.subprocess.run, A.subprocess.Popen, A.ci_why = saved[1], saved[2], saved[3]
            if saved[0] is None:
                os.environ.pop("AGENTCTL_GATE_SUMMARY")
            else:
                os.environ["AGENTCTL_GATE_SUMMARY"] = saved[0]
        self.assertLess(took, 0.5)
        return rc, out.getvalue()

    def test_fresh_and_stale_lines_come_from_the_file_only(self):
        rc, out = self.run_cached(f"{int(time.time())}\tpawelkrupinski/movies\tgate green · no reds\n")
        self.assertEqual((rc, out.strip()), (0, "gate green · no reds"))
        rc, out = self.run_cached(f"{int(time.time()) - 3600}\tpawelkrupinski/movies\tgate RED (main #1 → x)\n")
        self.assertEqual(rc, 2)
        self.assertTrue(out.startswith("gate UNREAD (as of "), out)
        self.assertIn("RED (main #1 → x)", out)


if __name__ == "__main__":
    unittest.main()
