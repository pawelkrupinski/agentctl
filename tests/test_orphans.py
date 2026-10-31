"""Orphaned work (the owner, 2026-10-28: "review and fix landing code ... so things don't get missed or stalled"): a
worktree whose commits are not on main and whose author is gone gets ONE re-land (or a fixer for a real failure), then
UNLANDED ORPHAN, a condition that clears when its commits land or somebody works in it again. Nothing is deleted."""
import contextlib, io, os, sys, time, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A, sh  # noqa: E402
from test_red import RedBase  # noqa: E402


class OrphanTest(RedBase):
    def setUp(self):
        super().setUp()
        os.environ["AGENTCTL_ORPHAN_IDLE"] = "0"
        self.saved_o = A.process_cwds, A.reland_worktree
        A.process_cwds = lambda: {}
        self.relands = []
        A.reland_worktree = lambda c, a: self.relands.append(a["worktree"]) or "landing again"

    def tearDown(self):
        A.process_cwds, A.reland_worktree = self.saved_o
        os.environ.pop("AGENTCTL_ORPHAN_IDLE", None)
        super().tearDown()

    def run_tool(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            rc = A.main(list(argv))
        return rc, out.getvalue()

    def deleted_log(self):
        p = os.path.join(os.environ["AGENTCTL_SCRATCH"], "agentctl-deleted.log")
        return open(p).read() if os.path.exists(p) else ""

    def test_an_orphan_is_relanded_once_then_escalated_and_clears_when_it_lands(self):
        wt, sha = self.worktree_commit("repo-wt-flipflop", "flipflop: a change nobody landed")
        self.assertEqual(A.orphan_duty(self.r.path), ["orphan repo-wt-flipflop: submit (never landed)"])
        self.assertEqual(self.relands, [wt])
        self.assertEqual(A.orphan_duty(self.r.path), ["orphan repo-wt-flipflop: escalated UNLANDED ORPHAN"])
        self.assertEqual(A.orphan_duty(self.r.path), [])  # once
        self.assertEqual(self.relands, [wt])  # one re-land, never a loop
        [e] = self.inbox("UNLANDED_ORPHAN")
        self.assertIn("repo-wt-flipflop: 1 commit(s) not on forgejo/main", e["verdict"])
        self.assertEqual([c["kind"] for c in A.open_conditions()], ["UNLANDED_ORPHAN"])  # an item: it escalates by its machine
        self.assertEqual(A.recovery_tick(force=True), [])  # still not on main
        sh(wt, "git", "push", "-q", "forgejo", "HEAD:main")  # it lands
        sh(self.r.path, "git", "fetch", "-q", "forgejo")
        self.assertEqual(len(A.recovery_tick(force=True)), 1)
        self.assertTrue(all(x["read"] for x in self.inbox("UNLANDED_ORPHAN")))
        self.assertTrue(os.path.isdir(wt))  # nothing deleted

    def test_a_real_failure_is_submitted_once_too_and_a_live_worktree_is_no_orphan(self):
        """Step 14: an orphan is submitted once whatever its last verdict (its checks decide; a rejection is its red's to
        rescue): no orphan fixer."""
        wt, _sha = self.worktree_commit("repo-wt-prune", "workflow prune")
        A.inbox_write("land", "NOT LANDED: the pre-push checks failed (locally, check tree x-wt-check-1)", [], [], 1, cwd=wt)
        A.process_cwds = lambda: {4242: wt}  # somebody works in it: no orphan
        self.assertEqual(A.orphan_duty(self.r.path), [])
        A.process_cwds = lambda: {}
        self.assertEqual(A.orphan_duty(self.r.path), ["orphan repo-wt-prune: submit (NOT LANDED: the pre-push checks "
                                                      "failed (locally, check tree x-wt-check-1))"])
        self.assertEqual(self.relands, [wt])

    def test_an_orphan_an_open_change_already_carries_is_not_submitted(self):
        """ISSUE 20261030T165514-e5f431: maintain re-submitted movies-wt-review with no session behind it while the open
        change movies-wt-ci-5 already carried its commits -- a duplicate in the queue."""
        wt, sha = self.worktree_commit("repo-wt-review", "review: a fix")
        ci5 = self.r.worktree("repo-wt-ci-5", base="forgejo/main", branch="ci-5")
        sh(ci5, "git", "cherry-pick", sha)  # the same change, another sha, plus ci-5's own
        self.r.write("ci5.txt", "ci-5\n", at=ci5)
        head = self.r.commit("ci-5: its own", at=ci5)
        st = A.Store()
        st.put_item("change", "20261030-154005-repo-wt-ci-5-819355", "checking", {"worktree": ci5, "head": head})
        self.assertEqual(sorted(A.orphan_duty(self.r.path)),  # ci-5 too: its own open change carries it
                         [f"orphan {n}: carried by open change 20261030-154005-repo-wt-ci-5-819355: not submitted"
                          for n in ("repo-wt-ci-5", "repo-wt-review")])
        self.assertEqual(A.orphan_duty(self.r.path), [], "said once")
        self.assertEqual(self.relands, [])
        self.assertEqual(A.open_conditions(), [], "nothing for a person to do: ci-5's verdict is its author's")
        it = st.item("change", "20261030-154005-repo-wt-ci-5-819355")
        st.transition("change", it["id"], "rejected", it["version"])  # ci-5 did not land: nothing carries it now
        self.assertEqual(sorted(A.orphan_duty(self.r.path)), ["orphan repo-wt-ci-5: submit (never landed)",
                                                              "orphan repo-wt-review: submit (never landed)"])
        self.assertEqual(sorted(self.relands), sorted([wt, ci5]))

    def test_an_escalated_orphan_clears_while_an_open_change_carries_it_and_reopens_when_that_closes_unlanded(self):
        """2026-10-30: orphan-movies-wt-ci-1..3, escalated before 588ffd9, stayed ESCALATED while the open change
        …-ci-5-45426e carried all their commits: the probe ignored the rule orphan_duty already used."""
        wt, sha = self.worktree_commit("repo-wt-ci-1", "ci-1: a fix")
        self.assertEqual(A.orphan_duty(self.r.path), ["orphan repo-wt-ci-1: submit (never landed)"])
        self.assertEqual(A.orphan_duty(self.r.path), ["orphan repo-wt-ci-1: escalated UNLANDED ORPHAN"])
        self.assertEqual(len(A.open_conditions()), 1)
        ci5 = self.r.worktree("repo-wt-ci-5", base="forgejo/main", branch="ci-5")
        sh(ci5, "git", "cherry-pick", sha)
        self.r.write("ci5.txt", "ci-5\n", at=ci5)
        head = self.r.commit("ci-5: its own", at=ci5)
        st = A.Store()
        cid = "20261030-175250-repo-wt-ci-5-45426e"
        st.put_item("change", cid, "checking", {"worktree": ci5, "head": head})
        self.assertEqual(len(A.recovery_tick(force=True)), 1)  # the probe: covered by the open change
        self.assertEqual(A.open_conditions(), [])
        self.assertIn(f"orphan repo-wt-ci-1: carried by open change {cid}: not submitted", A.orphan_duty(self.r.path))
        self.assertEqual(A.open_conditions(), [])
        it = st.item("change", cid)
        st.transition("change", cid, "rejected", it["version"])  # ci-5 did not land: the orphan reopens
        did = A.orphan_duty(self.r.path)
        self.assertIn("orphan repo-wt-ci-1: escalated UNLANDED ORPHAN", did)
        [c] = A.open_conditions()
        self.assertIn(f"{cid} closed unlanded", c["text"])
        self.assertEqual(self.relands.count(wt), 1, "never re-submitted (ci-5's own tree gets its one submit)")

    def test_an_orphan_whose_last_change_was_cancelled_goes_to_a_person_not_the_queue(self):
        wt, _sha = self.worktree_commit("repo-wt-dropped", "dropped: cancelled on purpose")
        st = A.Store()
        it = st.put_item("change", "20261030-120000-repo-wt-dropped-1", "queued", {"worktree": wt, "head": _sha})
        st.transition("change", it["id"], "cancelled", it["version"])
        self.assertEqual(A.orphan_duty(self.r.path), ["orphan repo-wt-dropped: escalated UNLANDED ORPHAN"])
        self.assertEqual(self.relands, [], "never re-submitted")
        [c] = A.open_conditions()
        self.assertIn("its last change 20261030-120000-repo-wt-dropped-1 was cancelled", c["text"])
        self.assertEqual(A.orphan_duty(self.r.path), [])

    def test_a_superseded_worktree_is_no_orphan_and_closes_itself_when_its_successor_lands(self):
        """2026-10-30: movies-wt-ci-1/2/3 and -review were carried into ci-5 with real conflict resolution (no patch-id,
        no content twin) and escalated UNLANDED ORPHAN until removed by hand. Marked SUPERSEDED BY ci-5: the condition
        clears, nothing is submitted or escalated, and once ci-5 lands the worktree and its branch are closed."""
        wt, sha = self.worktree_commit("repo-wt-ci-1", "ci-1: a fix")
        self.assertEqual(A.orphan_duty(self.r.path), ["orphan repo-wt-ci-1: submit (never landed)"])
        self.assertEqual(A.orphan_duty(self.r.path), ["orphan repo-wt-ci-1: escalated UNLANDED ORPHAN"])
        ci5 = self.r.worktree("repo-wt-ci-5", base="forgejo/main", branch="ci-5")
        self.r.write("fix.txt", "repo-wt-ci-1, resolved against ci-5\n", at=ci5)  # carried, its lines changed
        self.r.write("ci5.txt", "ci-5\n", at=ci5)
        self.r.commit("ci-5: ci-1's fix and its own", at=ci5)
        rc, out = self.run_tool("supersede", wt, "--by", "repo-wt-ci-5")
        self.assertEqual(rc, 0, out)
        self.assertIn("SUPERSEDED BY repo-wt-ci-5", out)
        self.assertEqual(len(A.recovery_tick(force=True)), 1)  # the probe that raised it clears it
        self.assertEqual(A.open_conditions(), [])
        did = A.orphan_duty(self.r.path)
        self.assertFalse([x for x in did if "repo-wt-ci-1" in x], did)  # no submit, no escalation
        self.assertEqual(self.relands.count(wt), 1)
        rc, out = self.run_tool("supersede")
        self.assertIn("repo-wt-ci-1: SUPERSEDED BY repo-wt-ci-5 (open", out)
        sh(ci5, "git", "push", "-q", "forgejo", "HEAD:main")  # the successor lands
        sh(self.r.path, "git", "fetch", "-q", "forgejo")
        did = A.orphan_duty(self.r.path)
        [line] = [x for x in did if "repo-wt-ci-1" in x]
        self.assertIn("superseded repo-wt-ci-1: repo-wt-ci-5 landed", line)
        self.assertIn("and branch repo-wt-ci-1", line)
        self.assertFalse(os.path.exists(wt))
        self.assertEqual(sh(self.r.path, "git", "branch", "--list", "repo-wt-ci-1"), "")
        self.assertIn(f"worktree\trepo\t{os.path.realpath(wt)}\t{sha}\tsuperseded: by repo-wt-ci-5", self.deleted_log())
        self.assertTrue([e for e in self.inbox() if e["verdict"].startswith("CLOSED ") and "repo-wt-ci-1" in e["verdict"]])
        self.assertEqual(A.orphan_duty(self.r.path), [], "closed once")
        rc, out = self.run_tool("supersede", "--all")
        self.assertIn("(closed", out)

    def test_a_mark_by_change_id_follows_resubmissions_and_lapses_when_it_ends_unlanded(self):
        wt, _sha = self.worktree_commit("repo-wt-review", "review: a fix")
        st = A.Store()
        first, second = "20261030-154005-repo-wt-ci-5-819355", "20261030-175250-repo-wt-ci-5-45426e"
        gone = os.path.join(self.tmp.name, "repo-wt-ci-5")  # the successor's tree is not here (another host's)
        it = st.put_item("change", first, "queued", {"worktree": gone, "head": "a" * 40})
        rc, out = self.run_tool("supersede", wt, "--by", "819355")
        self.assertEqual(rc, 0, out)
        self.assertEqual(A.orphan_duty(self.r.path), [], "carried: no submit")
        st.put_item("change", second, "queued", {"worktree": gone, "head": "b" * 40})
        st.transition("change", first, "superseded", it["version"], data={"superseded_by": second})
        self.assertEqual(A.successor_state(A.superseded_mark(wt), self.r.path)[0], "open", "re-submitted: still on its way")
        it2 = st.item("change", second)
        st.transition("change", second, "rejected", it2["version"])
        third = st.put_item("change", "20261030-190000-repo-wt-ci-5-aa11bb", "queued", {"worktree": gone, "head": "c" * 40})
        self.assertEqual(A.successor_state(A.superseded_mark(wt), self.r.path)[0], "open", "landed again from its tree")
        st.transition("change", third["id"], "cancelled", third["version"])
        did = A.orphan_duty(self.r.path)
        self.assertIn(f"superseded repo-wt-review: the mark lapsed (its change {second} ended rejected, unlanded); "
                      "the orphan rules apply again", did)
        self.assertIn("orphan repo-wt-review: submit (never landed)", did)
        self.assertIsNone(A.superseded_mark(wt))
        self.assertTrue(os.path.isdir(wt), "a lapsed mark deletes nothing")

    def test_a_successor_that_carries_nothing_yet_is_refused(self):
        wt, _sha = self.worktree_commit("repo-wt-a", "a")
        empty = self.r.worktree("repo-wt-b", base="forgejo/main", branch="b")
        rc, out = self.run_tool("supersede", wt, "--by", empty)
        self.assertEqual(rc, 2, out)
        self.assertIn("carries nothing that is not on main yet", out)
        rc, out = self.run_tool("supersede", wt, "--by", "no-such-change")
        self.assertEqual(rc, 2, out)
        self.assertEqual(A.superseded_marks(), {})

    def test_the_change_key_survives_a_rebase(self):
        wt, _sha = self.worktree_commit("repo-wt-x", "x")
        _c, k1 = A.orphan_key(self.r.path, wt)
        self.r.write("other.txt", "main moves\n")
        self.r.commit("main moves")
        self.r.push()
        sh(self.r.path, "git", "fetch", "-q", "forgejo")
        sh(wt, "git", "rebase", "-q", "forgejo/main")
        _c, k2 = A.orphan_key(self.r.path, wt)
        self.assertEqual(k1, k2)


if __name__ == "__main__":
    unittest.main()
