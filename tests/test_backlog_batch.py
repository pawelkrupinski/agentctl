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
from test_agentctl import A  # noqa: E402
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


for _name in dir(test_queue.QueueTest):  # its setUp only, not its tests
    if _name.startswith("test_") and _name not in BacklogBatchTest.__dict__:
        setattr(BacklogBatchTest, _name, None)

if __name__ == "__main__":
    unittest.main()
