"""THE BACKLOG BATCH (the owner, 2026-11-17: "batch whenever there's a backlog, not only on red"). With 6-8 changes queued,
each paid its own 30-40 min full check, mostly waiting for a ci-runner check slot. Now, at green, two or more queued
non-identity changes that no checker has taken are assembled into ONE batch and checked ONCE with the full landing check
(not --fast-only: no member had a check of its own), in one check tree: pass, one push, every member LANDED with its own
record (the batch's id and size); fail, bisected by prefix, the culprit confirmed before it is rejected, the passing
prefix landed at once, the rest queued again. Identity changes (the hook's `identity` area, G5's identity-model globs),
a change landed with --no-batch and a red fix are checked alone, beside the batch."""
import os, sys, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A, sh  # noqa: E402
import test_queue  # noqa: E402 (a module, so the loader runs QueueTest's own tests there only)
from test_waiting import REPO_HOOK  # noqa: E402

# the repo's hook: its --plan selects the `identity` area for identity paths only (as movies' does)
AREA_HOOK = """#!/bin/sh
case "$1" in
    --plan) plan=sbt; while read -r p; do case "$p" in identity/*) plan="sbt identity" ;; esac; done
            printf '%s\\n' $plan; exit 0 ;;
esac
if git ls-files | grep -q SbtLost; then echo "[error] sbt server disconnected"; exit 1; fi
"""


class BacklogBatchTest(test_queue.QueueTest):
    def setUp(self):
        super().setUp()
        self.r.write("scripts/hooks/pre-push", AREA_HOOK + REPO_HOOK.format(calls=self.calls).split("\n", 1)[1])
        os.chmod(os.path.join(self.r.path, "scripts/hooks/pre-push"), 0o755)
        self.r.commit("the hook's area plan")
        self.r.push()
        for p in (self.pushes, self.calls):
            if os.path.exists(p):
                os.remove(p)
        self.reds = set()
        self.d = A.waiting_state(self.r.path)[0]
        self.started = []
        self.saved_spawn = A.spawn_executor
        A.spawn_executor = lambda repo, d_, eid: self.started.append(eid)

    def tearDown(self):
        A.spawn_executor = self.saved_spawn
        super().tearDown()

    def entry(self, eid):
        return A.read_record(os.path.join(self.d, "entries", f"{eid}.json"))

    def queue(self, name, path, *extra):
        wt, _ = self.agent(name, path, f"{name}\n", msg=f"{name}: {path}")
        if extra:
            rc, out = self.land(wt, *extra)
            self.assertEqual(rc, 0, out)
            return out.split("QUEUED ", 1)[1].split()[0]
        return self.queued(wt)[0]

    def test_three_queued_non_identity_changes_are_checked_as_one_batch_round(self):
        ea = self.queue("a", "web/src/main/A.scala")
        eb = self.queue("b", "worker/src/main/B.scala")
        ec = self.queue("c", "scripts/c.sh")
        ei = self.queue("i", "identity/src/main/I.scala")
        did = A.tick_changes(self.r.path, self.d)
        self.assertEqual(self.started, [ei], f"only the identity change is checked alone: {did}")
        out = self.drive()
        self.assertIn("checking", " ".join(out))
        self.assertEqual(len(self.calls_of("--range")), 1, "ONE full check for the three changes")
        self.assertEqual(self.calls_of("--fast-only"), [], "the full landing check, never the fast one")
        self.assertEqual(len(self.pushed()), 1, "one push")
        self.assertEqual(self.remote_log()[:3], ["c: scripts/c.sh", "b: worker/src/main/B.scala", "a: web/src/main/A.scala"])
        bid = None
        for eid in (ea, eb, ec):
            e = self.entry(eid)
            self.assertEqual(e["status"], "landed", eid)
            it = A.Store().item("change", eid)
            self.assertEqual(it["state"], "landed")
            self.assertEqual(it["data"]["batch"], 3, "its record names the batch's size")
            bid = bid or it["data"]["batch_id"]
            self.assertEqual(it["data"]["batch_id"], bid, "and the one batch")
        self.assertEqual(self.entry(ei)["status"], "queued", "the identity change was never in the batch")
        self.assertEqual(len(self.watches), 3, "each member its own watch")
        landed = [x for x in self.inbox() if x["kind"] == "LANDED"]
        self.assertEqual(len(landed), 3, "each author told")
        st = A.Store()
        rec = A.landing_records(st.item("change", ea), st.item_events("change", ea), self.entry(ea))[0]
        self.assertEqual((rec["batch"], rec["batch_id"], rec["outcome"], rec["rounds"]), (3, bid, "landed", 1))

    def test_a_failing_batch_lands_its_passing_prefix_and_queues_the_rest_again(self):
        ea = self.queue("a", "web/src/main/A.scala")
        eb = self.queue("b", "web/src/main/BROKEN.scala")
        ec = self.queue("c", "worker/src/main/C.scala")
        A.tick_changes(self.r.path, self.d)
        self.assertEqual(self.started, [])
        self.drive()
        self.assertEqual(self.entry(ea)["status"], "landed", "the passing prefix lands at once")
        self.assertEqual(self.entry(eb)["status"], "rejected", "the confirmed culprit")
        self.assertIn("NOT LANDED", self.entry(eb)["why"])
        e = self.entry(ec)
        self.assertEqual(e["status"], "queued", "the rest queued again for the next batch")
        self.assertFalse(e.get("backlog_batch"))
        self.assertEqual(A.Store().item("change", ec)["state"], "queued")
        # the whole batch, the prefixes through b (fails) and a (passes), b's prefix confirmed: 3 extra rounds
        self.assertEqual(len(self.calls_of("--range")), 4)
        self.assertEqual(self.remote_log()[0], "a: web/src/main/A.scala")

    def test_a_culprit_failing_on_infra_is_checked_alone_not_rejected(self):
        ea = self.queue("a", "web/src/main/A.scala")
        eb = self.queue("b", "web/src/main/SbtLost.scala")
        A.tick_changes(self.r.path, self.d)
        self.drive()
        self.assertEqual(self.entry(ea)["status"], "landed")
        e = self.entry(eb)
        self.assertEqual(e["status"], "queued", "an infra-shaped failure is no verdict on the change")
        self.assertTrue(e["batch_alone"])
        self.assertFalse(e.get("backlog_batch"))
        A.tick_changes(self.r.path, self.d)
        self.assertEqual(self.started, [eb], "its own check judges it")

    def test_a_change_landed_with_no_batch_and_a_lone_change_are_checked_alone(self):
        ea = self.queue("a", "web/src/main/A.scala", "--no-batch")
        self.assertTrue(self.entry(ea)["no_batch"])
        eb = self.queue("b", "worker/src/main/B.scala")
        A.tick_changes(self.r.path, self.d)
        self.assertEqual(sorted(self.started), sorted([ea, eb]), "one batchable change is no backlog")
        self.assertEqual(self.drive(), [])

    def test_a_red_fix_is_never_delayed_behind_a_checking_batch(self):
        self.queue("a", "web/src/main/A.scala")
        self.queue("b", "worker/src/main/B.scala")
        saved = A.spawn_batch_executor
        A.spawn_batch_executor = lambda repo, d_, bid: None  # the batch's check runs elsewhere
        try:
            self.assertIn("checking", " ".join(A.tick_batch(self.r.path, self.d)))
        finally:
            A.spawn_batch_executor = saved
        self.assertEqual(self.batch()["data"]["depth"], "full")
        self.reds = {"7"}
        wf, _ = self.agent("fix", "web/src/main/Fix.scala", "fix\n", msg="fix: the red\n\nFixes-Red: 7")
        ef = self.queued(wf)[0]
        self.assertEqual(self.entry(ef)["fixes"], ["7"])
        A.check_entry(self.r.path, ef)
        self.assertEqual(self.entry(ef)["status"], "checked")
        did = A.tick_changes(self.r.path, self.d)
        self.assertNotIn("waits for batch", " ".join(did))
        self.assertEqual(self.entry(ef)["status"], "landed", "the red fix lands alone while the batch checks")


class PushBatchTest(test_queue.QueueTest):
    """THE PUSH BATCH (the owner, 2026-11-17: "combine them instead of landing them one by one"). ref-scrapers verified
    its combined tree while pr274 and postlanding, both checked, held their pushes behind it (THE OLDEST VERIFICATION
    PUSHES FIRST), each to verify again after it pushed: N checked changes landed serially, each paying a check-runner
    wait plus a ~10-min verify round. Now two or more checked (or verify-pending) changes that would each need a verify
    round on the current main are assembled into ONE tree on it, verified ONCE (--verify-only over the union) and pushed
    ONCE. The fixture's hook plans `identity` for every path, so no backlog batch forms: each change is checked alone."""

    def setUp(self):
        super().setUp()
        self.d = A.waiting_state(self.r.path)[0]
        for name in ("spawn_executor", "spawn_batch_executor"):
            self.addCleanup(setattr, A, name, getattr(A, name))
        A.spawn_executor = lambda repo, d_, eid: A.check_entry(repo, eid)  # the executors run in-process
        A.spawn_batch_executor = lambda repo, d_, bid: A.check_batch(repo, bid)

    def entry(self, eid):
        return A.read_record(os.path.join(self.d, "entries", f"{eid}.json"))

    def checked(self, name, path, text=None, msg=None):
        wt, _ = self.agent(name, path, text or f"{name}\n", msg=msg or f"{name}: {path}")
        eid, _ = self.queued(wt)
        A.check_entry(self.r.path, eid)
        self.assertEqual(self.entry(eid)["status"], "checked", eid)
        return eid

    def main_moves(self, path="docs/notes.md", text="n\n", name="m"):
        other, _ = self.agent(name, path, text, msg=f"main moves: {path}")
        sh(other, "git", "push", "-q", "origin", "HEAD:main")
        for p in (self.pushes, self.calls):  # what the checks and pushes did before main moved
            if os.path.exists(p):
                os.remove(p)

    def run_queue(self, eids, n=12):
        """The processor's ticks (the change tick, then the batch tick) until every change in eids is decided."""
        out = []
        for _ in range(n):
            out += A.tick_changes(self.r.path, self.d) + A.close_landed_changes(self.r.path, self.d) \
                + A.tick_batch(self.r.path, self.d)
            if all(self.entry(e)["status"] in A.ENTRY_FINAL for e in eids):
                break
        A.project_events()
        return out

    def test_three_checked_changes_on_a_moved_main_are_verified_once_and_pushed_once(self):
        ea = self.checked("a", "web/src/main/A.scala")
        eb = self.checked("b", "worker/src/main/B.scala")
        ec = self.checked("c", "scripts/c.sh")
        full_base = sh(self.r.root, "git", "--git-dir", self.r.bare, "rev-parse", "main").strip()
        self.main_moves()
        did = self.run_queue([ea, eb, ec])
        self.assertEqual(len(self.calls_of("--verify-only")), 1, f"ONE verify round for the three: {did}")
        self.assertEqual(self.calls_of("--range"), [], "no full round")
        tip = sh(self.r.root, "git", "--git-dir", self.r.bare, "rev-parse", "main").strip()
        self.assertEqual(self.verified()[0], ("--verify-only", f"{full_base}..{tip}"),
                         "over the union: what landed since the members' full checks, and the members")
        self.assertEqual(self.pushed(), ["push remote= skip=sbt identity"],
                         "one push, skipping the checks already passed")
        self.assertEqual(self.remote_log()[:4], ["c: scripts/c.sh", "b: worker/src/main/B.scala",
                                                 "a: web/src/main/A.scala", "main moves: docs/notes.md"])
        st, bid = A.Store(), None
        for eid in (ea, eb, ec):
            self.assertEqual(self.entry(eid)["status"], "landed", eid)
            it = st.item("change", eid)
            bid = bid or it["data"]["batch_id"]
            self.assertEqual((it["data"]["batch"], it["data"]["batch_id"]), (3, bid), "its record names the batch")
            rec = A.landing_records(it, st.item_events("change", eid), self.entry(eid))[0]
            self.assertEqual((rec["batch"], rec["batch_id"], rec["outcome"], rec["rounds"], rec["verify_rounds"]),
                             (3, bid, "landed", 2, 1), f"{eid}: its full check and the batch's one verify round")
        self.assertEqual(st.item("batch", bid)["data"]["depth"], "verify")
        self.assertEqual(len(self.watches), 3, "each member its own watch")
        self.assertEqual(len([x for x in self.inbox() if x["kind"] == "LANDED"]), 3, "each author told")

    def test_a_member_that_needs_a_full_round_is_left_out_and_checked_again_alone(self):
        self.r.write("docs/notes.md", "".join(f"line {i}\n" for i in range(12)))
        self.r.commit("notes")
        self.r.push()
        ea = self.checked("a", "docs/notes.md", "mine\n" + "".join(f"line {i}\n" for i in range(1, 12)))
        eb = self.checked("b", "web/src/main/B.scala")
        ec = self.checked("c", "worker/src/main/C.scala")
        self.main_moves("docs/notes.md", "".join(f"line {i}\n" for i in range(11)) + "theirs\n")  # a's file too
        self.run_queue([ea, eb, ec])
        st = A.Store()
        for eid in (eb, ec):
            self.assertEqual((self.entry(eid)["status"], st.item("change", eid)["data"].get("batch")), ("landed", 2), eid)
        self.assertEqual(self.entry(ea)["status"], "landed")
        self.assertIsNone(st.item("change", ea)["data"].get("batch"), "a landed alone")
        modes = [m for m, _r in self.verified()]
        self.assertEqual(sorted(modes), ["--range", "--verify-only"],
                         "the batch's verify round, and a's own full round (both touch docs/notes.md)")

    def test_a_failing_push_batch_lands_its_passing_prefix_and_verifies_the_rest_again(self):
        ea = self.checked("a", "web/src/main/A.scala")
        eb = self.checked("b", "web/src/main/PartA.scala")
        ec = self.checked("c", "worker/src/main/C.scala")
        self.main_moves("worker/src/main/PartB.scala", "b\n")  # the fake hook fails on PartA beside PartB
        self.run_queue([ea, eb, ec])
        self.assertEqual(self.entry(ea)["status"], "landed", "the passing prefix lands")
        self.assertEqual(A.Store().item("change", ea)["data"].get("batch"), 1)
        self.assertEqual(self.entry(eb)["status"], "rejected", "the confirmed culprit")
        self.assertIn("NOT LANDED", self.entry(eb)["why"])
        self.assertEqual(self.entry(ec)["status"], "landed", "the rest verified again, then landed")
        # the whole batch fails, then the prefixes through b (fails) and a (passes), b's confirmed; then c alone
        self.assertEqual([m for m, _r in self.verified()], ["--verify-only"] * 5)
        self.assertEqual(self.remote_log()[:2], ["c: worker/src/main/C.scala", "a: web/src/main/A.scala"])

    def test_a_red_fix_pushes_alone_and_first_while_a_push_batch_verifies(self):
        ea = self.checked("a", "web/src/main/A.scala")
        eb = self.checked("b", "worker/src/main/B.scala")
        self.main_moves()
        A.spawn_batch_executor = lambda repo, d_, bid: None  # the batch's verify round runs elsewhere
        self.run_queue([ea, eb], n=3)
        it = A.current_batch(A.Store(), self.d)
        self.assertEqual((it["state"], it["data"]["depth"], it["data"]["members"]), ("checking", "verify", [ea, eb]))
        self.reds = {"7"}
        ef = self.checked("fix", "web/src/main/Fix.scala", msg="fix: the red\n\nFixes-Red: 7")
        did = A.tick_changes(self.r.path, self.d)
        self.assertEqual(self.entry(ef)["status"], "landed", f"the red fix lands alone while the batch verifies: {did}")
        self.assertEqual(self.remote_log()[0], "fix: the red")
        self.assertNotIn(ef, A.current_batch(A.Store(), self.d)["data"]["members"])

    def test_a_change_checked_while_a_push_batch_verifies_joins_the_next_one(self):
        ea = self.checked("a", "web/src/main/A.scala")
        eb = self.checked("b", "worker/src/main/B.scala")
        self.main_moves()
        real = A.spawn_batch_executor
        A.spawn_batch_executor = lambda repo, d_, bid: None
        self.run_queue([ea, eb], n=3)
        bid = A.current_batch(A.Store(), self.d)["id"]
        ec = self.checked("c", "scripts/c.sh")  # checked on the main the batch verifies on
        ed = self.checked("d", "scripts/d.sh")
        did = A.tick_changes(self.r.path, self.d)
        self.assertIn(f"{ec}: its push waits for batch {bid}", " ".join(did))
        self.assertEqual(self.entry(ec)["status"], "checked", "no push under the batch")
        A.spawn_batch_executor = real
        self.run_queue([ea, eb, ec, ed])
        st = A.Store()
        self.assertEqual(st.item("batch", bid)["data"]["members"], [ea, eb], "the running batch neither held nor restarted")
        self.assertEqual({st.item("change", e)["data"].get("batch_id") for e in (ea, eb)}, {bid})
        nxt = {st.item("change", e)["data"].get("batch_id") for e in (ec, ed)}
        self.assertEqual(len(nxt), 1, "c and d: the next push batch")
        self.assertNotIn(bid, nxt)
        self.assertNotIn(None, nxt)
        self.assertEqual(len(self.pushed()), 2, "two pushes for four changes")


for _name in dir(test_queue.QueueTest):  # its setUp only, not its tests
    for _cls in (BacklogBatchTest, PushBatchTest):
        if _name.startswith("test_") and _name not in _cls.__dict__:
            setattr(_cls, _name, None)

if __name__ == "__main__":
    unittest.main()
