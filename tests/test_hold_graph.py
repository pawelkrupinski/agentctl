"""THE QUEUE'S HOLD GRAPH (holdgraph.py; 2026-11-23, 22 movies changes unchanged for over an hour). py-gitops-specs
(checked) held its push for landing-check-py's combined-tree verification; landing-check-py's check waited for
kick-convergence-py (both change 51 files); kick-convergence-py's check waited for py-gitops-specs. kick-convergence-py
was STACKED on landing-check-py (its branch carried landing-check-py's commits) and kept an older place in the queue.
A STACKED CHANGE WAITS FOR ITS PARENT: it is a dependency, never an overlapping peer. A HOLD CYCLE IS BROKEN, NEVER
WAITED OUT: whatever closes one, its oldest change proceeds and `status` says so."""
import os, sys, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A, sh, put_change  # noqa: E402
from fake_clock import on_clock  # noqa: E402
import test_backlog_batch  # noqa: E402

SHARED = "shared/S.scala"
LINES = "".join(f"line {i}\n" for i in range(1, 11))

GITOPS, LANDING, KICK = ("20261123-014151-movies-py-gitops-specs-fdc3d2", "20261123-013100-movies-landing-check-py-dc9f7b",
                         "20261123-013113-movies-kick-convergence-py-3086b1")


class HoldGraphTest(unittest.TestCase):
    def test_the_three_change_cycle_of_2026_11_23_is_found_and_its_oldest_proceeds(self):
        edges = {GITOPS: LANDING, LANDING: KICK, KICK: GITOPS, "other": GITOPS}
        self.assertEqual(A.hold_cycles(edges), [[LANDING, KICK, GITOPS]], "one cycle, the waiter outside it no part")
        seq = {GITOPS: 1639, LANDING: 1636, KICK: 1590}
        self.assertEqual(A.cycle_breaker([LANDING, KICK, GITOPS], seq), KICK, "the queue's oldest place")
        text = A.cycle_text([LANDING, KICK, GITOPS], KICK, {KICK: "its check waits for gitops"})
        self.assertIn(f"{KICK}, the oldest, proceeds (its wait on {GITOPS} dropped)", text)

    def test_no_cycle_in_a_chain(self):
        self.assertEqual(A.hold_cycles({"a": "b", "b": "c"}), [])
        self.assertEqual(A.hold_cycles({"a": "a"}), [["a"]])

    def test_a_branch_carrying_another_changes_commits_is_stacked_on_the_nearest(self):
        p = {"id": "p", "seq": 2, "status": "queued", "commits": ["1", "2"], "worktree": "/p"}
        g = {"id": "g", "seq": 1, "status": "landed", "commits": ["1"], "worktree": "/g"}
        k = {"id": "k", "seq": 1, "status": "queued", "commits": ["1", "2", "3"], "worktree": "/k"}
        self.assertIs(A.stacked_on(k, [g, p, k]), p, "the nearest parent: the most of its commits")
        self.assertEqual(A.own_commits(k, p), ["3"])
        self.assertIsNone(A.stacked_on(p, [k]), "a parent is never stacked on its own dependent")
        self.assertIsNone(A.stacked_on(k, [{**p, "status": "rejected"}]), "a rejected parent: its commits are k's own")
        self.assertIsNone(A.stacked_on(k, [{**p, "worktree": "/k"}]), "its own older version is no parent")


class StackedQueueTest(test_backlog_batch.BacklogBatchTest):
    def setUp(self):
        super().setUp()
        self.r.write(SHARED, LINES)
        self.r.commit("a source two lanes share")
        self.r.push()

    def hold_of(self, eid):
        return (A.process_store().item("change", eid)["data"] or {}).get("hold") or {}

    def the_live_cycle(self):
        """The queue of 2026-11-23 rebuilt: landing (queued, verifying its combined tree), kick (stacked on landing,
        an older place kept), gitops (checked, younger than landing, sharing a source with kick)."""
        wl = self.r.worktree("wt-landing", branch="landing-check")
        self.r.write("scripts/landing_check.py", "landing\n", at=wl)
        self.r.commit("landing-check: in Python", at=wl)
        el = self.queued(wl)[0]
        wk = self.r.worktree("wt-kick", base="landing-check", branch="kick-convergence")
        self.r.write(SHARED, LINES.replace("line 2\n", "line 2 kick\n"), at=wk)
        self.r.commit("kick-convergence: in Python, stacked on landing-check", at=wk)
        ek = self.queued(wk)[0]
        wg = self.r.worktree("wt-gitops", branch="gitops-specs")
        self.r.write(SHARED, LINES.replace("line 9\n", "line 9 gitops\n"), at=wg)
        self.r.commit("gitops-specs: in Python", at=wg)
        eg = self.queued(wg)[0]
        A.check_entry(self.r.path, eg, *on_clock(self.clock))
        self.assertEqual(self.entry(eg)["status"], "checked")
        e = self.entry(el)
        A.set_change(A.CurrentStore(A.process_store), self.d, e, "queued", event={"text": "verify"}, verify=True, full_base=e["base"],
                     full_tip=e["head"], retry_after=0, cause="agent")
        k = self.entry(ek)
        k["seq"] = self.entry(el)["seq"] - 1  # its kept place: older than its parent
        put_change(A.CurrentStore(A.process_store), self.d, k)
        return el, ek, eg

    def drive_to_rest(self, ids, rounds=12):
        for _ in range(rounds):
            for x in ids:
                e = self.entry(x)
                if e["status"] in ("queued", "checking") and x in self.started and not e.get("retry_after", 0) > self.clock.time():
                    self.started.remove(x)
                    A.check_entry(self.r.path, x, *on_clock(self.clock))
            A.tick_changes(self.r.path, self.d, *on_clock(A.Clock()))
            self.drive()  # the batch tick: queued changes that meet may land as one backlog batch
            if all(A.entry_final(self.entry(x)["status"]) for x in ids):
                break
        return {x: self.entry(x)["status"] for x in ids}

    def test_a_stacked_change_waits_for_its_parent_and_the_queue_never_deadlocks(self):
        el, ek, eg = self.the_live_cycle()
        A.tick_changes(self.r.path, self.d, *on_clock(A.Clock()))
        self.assertEqual((self.hold_of(ek).get("kind"), self.hold_of(ek).get("blocker")), ("stacked", el),
                         "kick waits for the change its branch carries")
        self.assertIn(el, self.started, f"landing verifies now, never held behind its own dependent: {self.hold_of(el)}")
        self.assertEqual(self.hold_of(eg).get("blocker"), el, "gitops pushes after landing's verification")
        self.assertEqual(self.drive_to_rest([el, ek, eg]), {el: "landed", ek: "landed", eg: "landed"})

    def test_a_verification_held_behind_another_change_holds_no_push_and_no_batch(self):
        """A HELD VERIFICATION HOLDS NOTHING (overlapcheck.py; `batch: 0` on 2026-11-23): landing's verify round waits
        behind an older change in flight it overlaps, so it verifies nothing now -- gitops' checked push goes, and
        two queued lanes form their backlog batch meanwhile."""
        wo = self.r.worktree("wt-older", branch="older")
        self.r.write("scripts/landing_check.py", "older\n", at=wo)
        self.r.commit("older: the same script", at=wo)
        eo = self.queued(wo)[0]
        el, ek, eg = self.the_live_cycle()
        o = self.entry(eo)
        o["seq"] = self.entry(ek)["seq"] - 1
        put_change(A.CurrentStore(A.process_store), self.d, o)
        A.set_change(A.CurrentStore(A.process_store), self.d, o, "checking", cause="agent")
        A.tick_changes(self.r.path, self.d, *on_clock(A.Clock()))
        self.assertEqual(self.hold_of(el).get("blocker"), eo, "landing waits for the older change in flight")
        A.tick_changes(self.r.path, self.d, *on_clock(A.Clock()))
        self.assertEqual(self.entry(eg)["status"], "landed", f"gitops pushes: {self.hold_of(eg)}")
        ea = self.queue("a", "web/src/main/A.scala")
        eb = self.queue("b", "worker/src/main/B.scala")
        self.assertEqual([c["id"] for c in A.backlog_candidates(self.r.path, self.d, set(), A.CurrentStore(A.process_store))], [ea, eb])

    def test_the_exact_cycle_a_stack_unseen_closes_is_broken_by_its_oldest_change(self):
        """With no stack seen (an entry from before the rule, a parent's commits re-cut), the three holds close the
        exact cycle of 2026-11-23. The tick finds it, kick -- the oldest place -- drops its wait on gitops, and
        `status` names the cycle."""
        saved = A.stack_map
        A.stack_map = lambda todo, entries: {}
        self.addCleanup(setattr, A, "stack_map", saved)
        el, ek, eg = self.the_live_cycle()
        A.tick_changes(self.r.path, self.d, *on_clock(A.Clock()))
        self.assertEqual([self.hold_of(x).get("blocker") for x in (el, ek, eg)], [ek, eg, el], "the cycle, as live")
        did = A.tick_changes(self.r.path, self.d, *on_clock(A.Clock()))
        line = next((x for x in did if x.startswith("HOLD CYCLE")), None)
        self.assertIsNotNone(line, did)
        for x in (el, ek, eg):
            self.assertIn(x, line)
        self.assertIn(f"{ek}, the oldest, proceeds (its wait on {eg} dropped)", line)
        self.assertTrue(ek in self.started or self.hold_of(ek).get("kind") == "push-batch",
                        f"the oldest change proceeds: checked now, or in the next push batch ({self.hold_of(ek)})")
        self.assertEqual(self.entry(ek)["cycle_ignore"], [eg])
        rows = A.status_landing(A.process_store(), self.clock.time())["changes"]
        self.assertIn("HOLD CYCLE", next(r for r in rows if r["id"] == ek)["cycle"] or "", "status says so")
        self.assertNotIn("HOLD CYCLE", " ".join(A.tick_changes(self.r.path, self.d, *on_clock(A.Clock()))), "broken once, not every tick")
        final = self.drive_to_rest([el, ek, eg])
        self.assertEqual((final[ek], final[eg]), ("landed", "landed"), final)
        # landing's commits are kick's too: landed as itself, or -- the two in one batch -- superseded by kick, which
        # carries them onto main
        self.assertIn(final[el], ("landed", "superseded"), final)
        self.assertIn("landing-check: in Python", self.remote_log())


for _name in dir(test_backlog_batch.BacklogBatchTest):
    if _name.startswith("test_") and _name not in StackedQueueTest.__dict__:
        setattr(StackedQueueTest, _name, None)

if __name__ == "__main__":
    unittest.main()
