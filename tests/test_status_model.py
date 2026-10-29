"""One status model (docs/simplification-2026-10.md item 4): `status --line`, `status`, `red status`, `ci-why --summary
--cached` and the status page all render from status_data(), so they can never disagree."""
import contextlib, io, os, sys, tempfile, time, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402


class StatusModelTest(unittest.TestCase):
    def setUp(self):
        self.saved = os.environ.get("AGENTCTL_STATE_ROOT")
        os.environ["AGENTCTL_STATE_ROOT"] = tempfile.mkdtemp()
        self.addCleanup(lambda: os.environ.__setitem__("AGENTCTL_STATE_ROOT", self.saved) if self.saved
                        else os.environ.pop("AGENTCTL_STATE_ROOT", None))
        self.t = time.time()

    def out(self, argv):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = A.main(argv)
        return rc, buf.getvalue().strip()

    def test_the_line_the_page_and_ci_why_say_the_same_gate(self):
        A.state_update(tick_at=self.t - 3, waiting=["/x/movies-wt-a"], queued=[],
                       summary={"at": int(self.t - 10), "repo": A.SHORT["movies"], "line": "gate RED (main #9 -> NOBODY)"})
        h = A.status_data(only=("header",), now_=self.t)["header"]
        self.assertEqual(h["gate_text"], "gate RED (main #9 -> NOBODY)")
        self.assertEqual(A.status_line(self.t), h["line"])
        self.assertEqual(self.out(["status", "--line"])[1], A.status_line())
        rc, text = self.out(["ci-why", "--summary", "--cached", "--repo", "movies"])
        self.assertEqual((rc, text), (1, h["gate_text"]))
        self.assertTrue(self.out(["status"])[1].startswith(h["line"]))

    def test_a_stale_or_missing_gate_reads_unread_everywhere(self):
        self.assertEqual(A.status_gate_text({}, self.t), "gate UNREAD (as of never: no fresh published gate)")
        A.state_update(tick_at=self.t, summary={"at": int(self.t - 3600), "repo": A.SHORT["movies"], "line": "gate green"})
        h = A.status_header(self.t, probes=False)
        self.assertTrue(h["gate_text"].startswith("gate UNREAD (as of "), h["gate_text"])
        self.assertIn(h["gate_text"], h["line"])
        rc, text = self.out(["ci-why", "--summary", "--cached", "--repo", "movies"])
        self.assertEqual(rc, 2)
        self.assertTrue(text.startswith("gate UNREAD (as of "), text)

    def test_red_status_is_the_models_reds_families_and_dispatcher(self):
        st = A.Store()
        st.put_item("red", "k1", "offered", {"key": "k1", "workflow": "Main", "last_run": {"number": 20}, "jobs": ["test"],
                                             "gate": True})
        saved = A.cached_dispatch_blocker
        A.cached_dispatch_blocker = lambda: None
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                A.red_status(st)
            m = A.status_data(store=st, now_=st.clock(), only=("reds", "families", "owners", "dispatcher"))
        finally:
            A.cached_dispatch_blocker = saved
        self.assertEqual(buf.getvalue().strip(), A.render_red_status(m))
        self.assertIn("Main #20 [test] k1: offered", buf.getvalue())
        self.assertIn("dispatcher: ON; fixers alive: 0/", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
