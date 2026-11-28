"""ONE governor directory: $AGENTCTL_GOV_DIR, else `gov` under $AGENTCTL_STATE_ROOT -- for the fan-out (govpool), the
tickets (govtickets) and the cpu sample alike (govpool used ~/.cache/agentctl/gov whatever the state root said, so a
test's runner took tickets in the real governor)."""
import os, sys, tempfile, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402


class GovDirTest(unittest.TestCase):
    def test_the_state_root_decides_unless_the_gov_dir_is_set(self):
        root = tempfile.mkdtemp()
        self.assertEqual(A.gov_dir_path({"AGENTCTL_STATE_ROOT": root}), os.path.join(root, "gov"))
        self.assertEqual(A.gov_dir_path({"AGENTCTL_STATE_ROOT": root, "AGENTCTL_GOV_DIR": "/x/g"}), "/x/g")

    def test_tickets_and_cpu_sample_share_it(self):
        old = os.environ.pop("AGENTCTL_GOV_DIR", None)
        try:
            self.assertEqual(os.path.dirname(A.cpu_sample_path()), A.gov_dir())
            self.assertEqual(A.gov_dir(), A.gov_dir_path(os.environ))
            self.assertTrue(A.gov_dir().startswith(os.environ["AGENTCTL_STATE_ROOT"]))
        finally:
            if old is not None:
                os.environ["AGENTCTL_GOV_DIR"] = old


class LoadSeamTest(unittest.TestCase):
    def test_the_run_queue_reads_the_pinned_machine_like_every_other_load(self):
        m = A.machine_probe('{"load1": 7.5, "cores": 4}')
        self.assertEqual(m.load1(), 7.5)
        self.assertEqual(m.run_queue(), 7.5)  # was os.getloadavg(): the host's, whatever the test pinned


if __name__ == "__main__":
    unittest.main()
