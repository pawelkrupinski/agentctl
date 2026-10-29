"""Design §14.5: the queue's commands under `land`, the processor under `tick`; the old `waiting` names answer for a
week, each naming its new one."""
import argparse, contextlib, io, os, sys, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402


class RenameTest(unittest.TestCase):
    def setUp(self):
        self.calls, self.saved = [], A.cmd_waiting
        A.cmd_waiting = lambda w: self.calls.append(w) or 0

    def tearDown(self):
        A.cmd_waiting = self.saved

    def test_land_subcommands_are_the_queues_and_tick_is_the_processor(self):
        for argv, want in ((["land", "status"], "status"), (["land", "hold"], "pause"), (["land", "release"], "resume"),
                           (["land", "cancel", "x-12"], "cancel"), (["tick", "--once", "--repo", "/r"], "run"),
                           (["tick", "service", "status", "--repo", "/r"], "service")):
            self.calls.clear()
            self.assertEqual(A.main(argv), 0, argv)
            [w] = self.calls
            self.assertEqual((w.wcmd, w.renamed), (want, True), argv)
        self.assertEqual(self.calls[0].action, "status")

    def test_land_cancel_needs_the_id(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(A.main(["land", "cancel"]), 2)
        self.assertEqual(self.calls, [])

    def test_an_old_name_says_its_new_one(self):
        A.cmd_waiting = self.saved
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            try:
                A.cmd_waiting(argparse.Namespace(wcmd="pause", repo="/nonexistent", entry=None))
            except (Exception, SystemExit):
                pass
        self.assertIn("is now `agentctl land hold`", err.getvalue())


if __name__ == "__main__":
    unittest.main()
