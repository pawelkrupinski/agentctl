"""Throwaway work never lands (2026-11-01: `agentctl land` from a prototype worktree, branch measure/chain-settle-proto,
commit "PROTOTYPE: ... measure only, do not land", queued twice): land, every re-land and a laptop submission refuse."""
import os, sys, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A, sh  # noqa: E402
from test_land import LandBase  # noqa: E402


class DoNotLandTest(LandBase):
    def refused(self, msg, branch=None):
        self.hook("exit 0")
        if branch:
            sh(self.r.path, "git", "switch", "-q", "-c", branch)
        self.r.write("proto.txt", "x\n")
        self.r.commit(msg)
        rc, out = self.land()
        self.assertEqual(rc, 1, out)
        self.assertIn("NOT LANDED", out)
        self.assertNotIn("QUEUED", out)
        return out

    def test_measure_branch_is_refused(self):
        self.assertIn("measure/", self.refused("a fine change", "measure/chain-settle-proto"))

    def test_experiment_branch_is_refused(self):
        self.assertIn("experiment/", self.refused("a fine change", "experiment/x"))

    def test_prototype_subject_is_refused_in_any_case(self):
        self.assertIn("not for landing", self.refused("prototype: measure only, do not land"))

    def test_wip_and_do_not_land_subjects_are_refused(self):
        self.assertIn("not for landing", self.refused("WIP half done"))

    def test_trailer_is_refused(self):
        self.assertIn("Do-Not-Land", self.refused("a fine change\n\nDo-Not-Land: measuring"))

    def test_a_normal_change_is_not_refused_by_the_guard(self):
        self.r.write("ok.txt", "x\n")
        sha = self.r.commit("Prototype-free change: wipe the cache")  # 'wipe' is no WIP
        self.assertIsNone(A.do_not_land(self.r.path, [sha], "wt-feature"))

    def test_laptop_submission_is_refused(self):
        self.r.write("p.txt", "x\n")
        sha = self.r.commit("PROTOTYPE: measure only")
        with self.assertRaises(ValueError) as c:
            A.submit_change({"id": "n-1", "worktree": self.r.path, "head": sha, "commits": [sha]}, repo=self.r.path)
        self.assertIn("NOT LANDED", str(c.exception))
        with self.assertRaises(ValueError):
            A.submit_change({"id": "n-2", "worktree": self.r.path, "head": sha, "commits": ["x"],
                             "branch": "measure/y"}, repo=self.r.path)


if __name__ == "__main__":
    unittest.main()
