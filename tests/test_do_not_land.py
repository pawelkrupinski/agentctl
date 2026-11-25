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

    def test_a_hold_trailer_is_refused_until_released(self):
        """2026-11-06 es-gate-check: a gate re-enable the owner was holding was queued by the orphan re-lander. A
        `Hold: <why>` trailer says so on the commit itself: land refuses it unless --release-hold."""
        out = self.refused("Gate re-enable\n\nHold: the owner holds this until the drift check is clean")
        self.assertIn("Hold:", out)
        self.assertIn("--release-hold", out)
        head = sh(self.r.path, "git", "rev-parse", "HEAD")
        self.assertEqual(A.held_commit(self.r.path, [head]),
                         f"commit {head[:10]} carries Hold: the owner holds this until the drift check is clean")
        rc, out = self.land("--release-hold")
        self.assertEqual(rc, 0, out)
        self.assertNotIn("NOT LANDED", out)

    def test_hold_in_a_body_line_is_no_trailer(self):
        self.r.write("ok.txt", "x\n")
        sha = self.r.commit("Gate: say why\n\nHold: on, this line is prose\nand the paragraph goes on.\n\nSigned-off-by: x")
        self.assertIsNone(A.held_commit(self.r.path, [sha]))

    def test_a_hold_trailer_folded_or_in_its_own_trailer_block_is_still_held(self):
        """Review 2026-11-07: a Hold: that git itself reads as a trailer -- its value folded onto a continuation line,
        or a trailer block of its own above the attribution block -- was no trailer to held_commit (every line of the
        LAST paragraph had to be `Token: value`): the held change landed."""
        for msg in ("Gate re-enable\n\nHold: the owner holds this\n  until the drift check is clean\n"
                    "Co-Authored-By: Claude <noreply@anthropic.com>",
                    "Gate re-enable\n\nWhy it waits.\n\nHold: the owner holds this\n\n"
                    "Co-Authored-By: Claude <noreply@anthropic.com>"):
            self.r.write("ok.txt", msg)
            sha = self.r.commit(msg)
            self.assertRegex(A.held_commit(self.r.path, [sha]) or "", r"carries Hold: the owner holds this", msg)

    def test_laptop_submission_is_refused(self):
        self.r.write("p.txt", "x\n")
        sha = self.r.commit("PROTOTYPE: measure only")
        with self.assertRaises(ValueError) as c:
            A.submit_change(A.CurrentStore(A.process_store), {"id": "n-1", "worktree": self.r.path, "head": sha, "commits": [sha]}, repo=self.r.path)
        self.assertIn("NOT LANDED", str(c.exception))
        with self.assertRaises(ValueError):
            A.submit_change(A.CurrentStore(A.process_store), {"id": "n-2", "worktree": self.r.path, "head": sha, "commits": ["x"],
                             "branch": "measure/y"}, repo=self.r.path)


if __name__ == "__main__":
    unittest.main()
