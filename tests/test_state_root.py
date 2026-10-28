"""ONE state root (the owner, 2026-10-28): every agentctl path resolves under $AGENTCTL_STATE_ROOT at each use, so a
test (and its child processes) never touches the real ~/.cache/agentctl; test_agentctl's atexit guard fails a run
that leaks into it anyway."""
import os, re, sys, tempfile, unittest

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
            for path in (A.waiting_dir(), A.land_lock_dir(), A.sweep_dir(), A.reap_log_path(), A.gate_dirty_path(),
                         A.state_root("maintain.json")):
                self.assertTrue(path.startswith(root), path)
        finally:
            os.environ["AGENTCTL_STATE_ROOT"] = saved

    def test_the_test_run_itself_is_under_a_temp_root(self):
        self.assertNotEqual(os.path.realpath(A.state_root()),
                            os.path.realpath(os.path.join(os.path.expanduser("~"), ".cache", "agentctl")))


if __name__ == "__main__":
    unittest.main()
