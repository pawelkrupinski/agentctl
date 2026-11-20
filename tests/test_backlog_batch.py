"""THE BACKLOG BATCH (the owner, 2026-11-17: "batch whenever there's a backlog, not only on red"). With 6-8 changes queued,
each paid its own 30-40 min full check, mostly waiting for a ci-runner check slot. Now, at green, two or more queued
non-identity changes that no checker has taken are assembled into ONE batch and checked ONCE with the full landing check
(not --fast-only: no member had a check of its own), in one check tree: pass, one push, every member LANDED with its own
record (the batch's id and size); fail, bisected by prefix, the culprit confirmed before it is rejected, the passing
prefix landed at once, the rest queued again. Identity changes (the hook's `identity` area, G5's identity-model globs),
a change landed with --no-batch and a red fix are checked alone, beside the batch."""
import json, os, sys, time, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A, sh  # noqa: E402
import test_queue  # noqa: E402 (a module, so the loader runs QueueTest's own tests there only)
from test_waiting import REPO_HOOK  # noqa: E402

# the repo's hook, as movies' (2026-11-19): its --plan runs the `identity` area for the identity paths and for common/
# (some identity specs read it), while --identity-change names only what can move takes (identity/)
AREA_HOOK = """#!/bin/sh
case "$1" in
    --plan) plan=sbt; while read -r p; do case "$p" in identity/*|common/*) plan="sbt identity" ;; esac; done
            printf '%s\\n' $plan; exit 0 ;;
    --identity-change) while read -r p; do case "$p" in identity/*) echo identity; exit 0 ;; esac; done; exit 0 ;;
esac
if git ls-files | grep -q SbtLost; then echo "[error] sbt server disconnected"; exit 1; fi
if [ -n "$TEST_INFRA_ONCE" ] && [ ! -e "$TEST_INFRA_ONCE" ]; then : > "$TEST_INFRA_ONCE"; echo "[error] sbt server disconnected"; exit 1; fi
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

    def test_queued_changes_identity_ones_included_are_checked_as_one_batch_round(self):
        """EVERY CHANGE IS A MEMBER (the owner, 2026-11-20: "batching ... structurally"): identity changes join the
        batch like any other, their identity area in its one check."""
        ea = self.queue("a", "web/src/main/A.scala")
        eb = self.queue("b", "worker/src/main/B.scala")
        ei = self.queue("i", "identity/src/main/I.scala")
        ej = self.queue("j", "identity/src/main/J.scala")
        ec = self.queue("c", "scripts/c.sh")
        did = A.tick_changes(self.r.path, self.d)
        self.assertEqual(self.started, [], f"nothing is checked alone: {did}")
        out = self.drive()
        self.assertIn("checking", " ".join(out))
        self.assertEqual(len(self.calls_of("--range")), 1, "ONE full check for the five changes")
        self.assertEqual(self.calls_of("--fast-only"), [], "the full landing check, never the fast one")
        self.assertEqual(len(self.pushed()), 1, "one push")
        self.assertEqual(self.remote_log()[:5], ["c: scripts/c.sh", "j: identity/src/main/J.scala",
                                                 "i: identity/src/main/I.scala", "b: worker/src/main/B.scala",
                                                 "a: web/src/main/A.scala"])
        bid = None
        for eid in (ea, eb, ei, ej, ec):
            e = self.entry(eid)
            self.assertEqual(e["status"], "landed", eid)
            it = A.Store().item("change", eid)
            self.assertEqual(it["state"], "landed")
            self.assertEqual(it["data"]["batch"], 5, "its record names the batch's size")
            bid = bid or it["data"]["batch_id"]
            self.assertEqual(it["data"]["batch_id"], bid, "and the one batch")
        self.assertEqual(len(self.watches), 5, "each member its own watch")
        landed = [x for x in self.inbox() if x["kind"] == "LANDED"]
        self.assertEqual(len(landed), 5, "each author told")
        st = A.Store()
        rec = A.landing_records(st.item("change", ea), st.item_events("change", ea), self.entry(ea))[0]
        self.assertEqual((rec["batch"], rec["batch_id"], rec["outcome"], rec["rounds"]), (5, bid, "landed", 1))

    def test_a_change_that_runs_identity_specs_but_moves_no_take_joins_the_batch(self):
        """2026-11-19 (movies' `--identity-change`): a common/ change runs the hook's identity area -- the specs reading
        what it changed -- but moves no take, so it is no identity change checked alone: it joins the batch."""
        eb = self.queue("b", "common/src/main/B.scala")
        ea = self.queue("a", "web/src/main/A.scala")
        self.assertIs(A.identity_change(self.r.path, self.entry(eb)), False)
        A.tick_changes(self.r.path, self.d)
        self.assertEqual(self.started, [], "nothing is checked alone")
        self.drive()
        for eid in (ea, eb):
            self.assertEqual(self.entry(eid)["status"], "landed", eid)
            self.assertEqual(A.Store().item("change", eid)["data"]["batch"], 2, "b and a land as one batch")

    def test_a_verified_pure_move_of_identity_code_joins_the_batch(self):
        """2026-11-19 ("make it simpler and faster"): a commit saying `Pure-Move: verified` moves no take -- the hook's
        puremove check proves the move and leaves the identity area to the commits without the claim -- so its identity
        paths no longer make it an identity change checked alone; one unclaimed commit on an identity path still does."""
        wt, _ = self.agent("m", "identity/src/main/Moved.scala", "m\n",
                           msg="m: identity/src/main/Moved.scala moves\n\nPure-Move: verified")
        em = self.queued(wt)[0]
        wt, _ = self.agent("n", "identity/src/main/Changed.scala", "n\n",
                           msg="n: identity/src/main/Changed.scala\n\nPure-Move: maybe")
        en = self.queued(wt)[0]
        ea = self.queue("a", "web/src/main/A.scala")
        wt, _ = self.agent("o", "identity/src/main/Other.scala", "o\n", msg="o: identity/src/main/Other.scala")
        eo = self.queued(wt)[0]
        A.tick_changes(self.r.path, self.d)
        self.assertEqual(self.started, [], "every one batches")
        self.assertIs(A.identity_change(self.r.path, self.entry(em)), False)
        self.assertIs(A.identity_change(self.r.path, self.entry(en)), True)
        self.drive()
        for eid in (em, en, ea, eo):
            self.assertEqual(self.entry(eid)["status"], "landed", eid)
            self.assertEqual(A.Store().item("change", eid)["data"]["batch"], 4, "the four land as one batch")

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

    def test_a_batch_failing_once_on_infra_is_checked_again_not_bisected(self):
        """2026-11-19: a batch's failed check went straight to its bisect whatever failed it (only the red gate was
        excepted), while a lone change's infra-shaped failure is re-queued -- an sbt server lost under one batch cost a
        prefix check per member. The batch's own check runs again first; only a second failure bisects."""
        once = os.path.join(self.r.root, "infra-once")
        os.environ["TEST_INFRA_ONCE"] = once
        self.addCleanup(os.environ.pop, "TEST_INFRA_ONCE", None)
        ea = self.queue("a", "web/src/main/A.scala")
        eb = self.queue("b", "worker/src/main/B.scala")
        A.tick_changes(self.r.path, self.d)
        self.drive()
        self.assertTrue(os.path.exists(once), "the first check failed on infra")
        self.assertEqual([self.entry(e)["status"] for e in (ea, eb)], ["landed", "landed"])
        # the hook logs a call only past its infra line: the one logged is the batch's second check (a bisect logged 3)
        self.assertEqual(len(self.calls_of("--range")), 1, "the batch's check once again: no prefix checks")
        bid = A.Store().item("change", ea)["data"]["batch_id"]
        states = [ev["to_state"] for ev in A.Store().item_events("batch", bid)]
        self.assertNotIn("bisecting", states)

    def test_a_change_landed_with_no_batch_and_a_lone_change_are_checked_alone(self):
        ea = self.queue("a", "web/src/main/A.scala", "--no-batch")
        self.assertTrue(self.entry(ea)["no_batch"])
        eb = self.queue("b", "worker/src/main/B.scala")
        A.tick_changes(self.r.path, self.d)
        self.assertEqual(sorted(self.started), sorted([ea, eb]), "one batchable change is no backlog")
        self.assertEqual(self.drive(), [])

    def test_a_younger_lone_change_never_takes_the_tree_an_older_backlog_waits_for(self):
        """Review 2026-11-18: the change tick runs before the batch tick, and the held candidates take no tree, so the
        tree one check freed went to the next YOUNGER lone change (an identity change, say) and the batch tick found
        none free: with a steady stream of lone changes the backlog batch never formed, its members held for ever."""
        os.environ["AGENTCTL_CHECK_TREES"] = "1"
        self.addCleanup(os.environ.pop, "AGENTCTL_CHECK_TREES", None)
        held = []
        A.spawn_executor = lambda repo, d_, eid: (self.started.append(eid), held.append(A.tree_slot(d_, 1)),
                                                  held[-1].acquire())
        ei1 = self.queue("i1", "identity/src/main/I1.scala")
        A.tick_changes(self.r.path, self.d)
        self.assertEqual(self.started, [ei1], "the identity change takes the one tree")
        ea = self.queue("a", "web/src/main/A.scala")
        eb = self.queue("b", "worker/src/main/B.scala")
        ei2 = self.queue("i2", "identity/src/main/I2.scala", "--no-batch")  # a lone change younger than the backlog
        self.assertEqual(A.tick_batch(self.r.path, self.d), [], "no tree free: no batch yet")
        held.pop().release()  # i1's check ends, its tree free
        A.set_change(self.d, self.entry(ei1), "cancelled", event={"text": "done"})
        A.tick_changes(self.r.path, self.d)
        self.assertNotIn(ei2, self.started, "the younger lone change waits for the older backlog's batch")
        saved = A.spawn_batch_executor
        A.spawn_batch_executor = lambda repo, d_, bid: None
        try:
            did = A.tick_batch(self.r.path, self.d)
        finally:
            A.spawn_batch_executor = saved
        self.assertIn("checking", " ".join(did), "the backlog batch takes the freed tree")
        self.assertEqual(self.batch()["data"]["members"], [ea, eb])

    def test_an_older_verification_pushes_before_a_backlog_batch_forms(self):
        """Review 2026-11-18 (THE OLDEST VERIFICATION PUSHES FIRST, queue.py): a backlog batch formed while an older
        change verified its combined tree, so that change's push waited behind the batch's 30-40 min check (THE BATCH
        GOES FIRST), the batch's push moved main under it, and it verified again -- behind the next backlog batch, and
        the next: workermem's 10 h again. A verification in flight forms no backlog batch until it pushed."""
        wv, _ = self.agent("v", "scripts/v.sh", "v\n", msg="v: scripts/v.sh")
        ev = self.queued(wv)[0]
        A.check_entry(self.r.path, ev)
        self.assertEqual(self.entry(ev)["status"], "checked")
        other, _ = self.agent("m", "docs/notes.md", "n\n", msg="main moves")
        sh(other, "git", "push", "-q", "origin", "HEAD:main")
        e = self.entry(ev)  # its push found main moved: a verify round of its own (THE RULE; this hook has none)
        A.set_change(self.d, e, "queued", event={"text": "verify"}, verify=True, full_base=e["checked_base"],
                     full_tip=e["tip"], retry_after=0)
        ea = self.queue("a", "web/src/main/A.scala")
        eb = self.queue("b", "worker/src/main/B.scala")
        self.assertEqual(A.backlog_candidates(self.r.path, self.d, set()), [], "no batch while v verifies")
        self.assertEqual(self.drive(), [])
        self.assertIsNone(self.batch())
        A.check_entry(self.r.path, ev)  # its verify round, ended between the change tick and the batch tick
        self.assertEqual(self.entry(ev)["status"], "checked")
        self.assertEqual(self.drive(), [], "nor while its push is due")
        A.tick_changes(self.r.path, self.d)
        self.assertEqual(self.entry(ev)["status"], "landed", "the older verification pushes first")
        self.drive()
        for eid in (ea, eb):
            self.assertEqual(self.entry(eid)["status"], "landed", "then the backlog batch, on the main it moved")

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

    # A FIX OF ANY OPEN RED GOES FIRST (2026-11-19, 08010c: `Fixes-Red: 6`, Alert backtest #6, red 5254bffda9d2 -- a
    # red outside the gate, so the gate was green and red_fix, reading the GATE's reds only, called it ordinary).
    # Checked at 08:32Z, it waited 22 min behind two combined-tree verifications, was left out of push batch #47
    # (push_alone), then sat behind the batch's ~110-min verify round (BATCH_FIRST), `checked` and past its deadline at
    # level 0 with nothing said; fix-replay, overlapping it on infra/bin/backtest-alerts, waited behind it.
    def open_outside_red(self):
        A.Store().put_item("red", "5254bffda9d2", "fix-pending",
                           {"key": "5254bffda9d2", "workflow": "Alert backtest", "gate": False, "red_kind": "outside",
                            "runs": ["44479918396", "5", "6"]})

    def test_an_older_ordinary_change_never_pushes_under_a_red_fixs_verification(self):
        """2026-11-19: 08010c (`Fixes-Red: 6`) verified its combined tree 09:34-10:18Z; at 10:11Z ratchet-diff, queued
        BEFORE it (so the age rule held it not), pushed, and the fix verified again until 10:36Z -- 100213 and 102400
        held behind it all the while. A red fix goes first: an ordinary push, older or younger, waits for its round."""
        self.open_outside_red()
        A.spawn_executor = lambda repo, d_, eid: None  # each check run below, by hand
        wo, _ = self.agent("old", "web/src/main/Old.scala", "old\n", msg="old: web/src/main/Old.scala")
        eo, _ = self.queued(wo)  # queued first, checked later (held, then a tree)
        ef = self.checked("fix", "infra/bin/backtest-alerts", msg="backtest-alerts: the fix\n\nFixes-Red: 6")
        self.main_moves()
        did = A.tick_changes(self.r.path, self.d)
        self.assertEqual((self.entry(ef)["status"], self.entry(ef)["verify"]), ("queued", True), did)
        A.check_entry(self.r.path, eo)
        self.assertEqual(self.entry(eo)["status"], "checked")
        did = A.tick_changes(self.r.path, self.d)
        self.assertIn(f"{eo}: its push waits for {ef}'s combined-tree verification", " ".join(did))
        self.assertEqual(self.entry(eo)["status"], "checked", "no push under the red fix's verification")
        hold = A.Store().item("change", eo)["data"]["hold"]
        self.assertEqual((hold["kind"], hold["blocker"]), ("verify", ef))
        A.check_entry(self.r.path, ef)
        A.tick_changes(self.r.path, self.d)
        self.assertEqual(self.entry(ef)["status"], "landed", "verified once, then pushed")
        self.assertEqual(self.remote_log()[0], "backtest-alerts: the fix")

    def test_a_fix_of_an_open_red_outside_the_gate_pushes_before_a_batch_it_is_not_in(self):
        """The queue shape of 2026-11-19 ~09:30Z replayed: a push batch verifying, the red fix checked beside it, a
        change overlapping the fix held behind it."""
        self.open_outside_red()
        ea = self.checked("a", "web/src/main/A.scala")
        eb = self.checked("b", "worker/src/main/B.scala")
        self.main_moves()
        A.spawn_batch_executor = lambda repo, d_, bid: None  # the batch's verify round runs elsewhere
        self.run_queue([ea, eb], n=3)
        bt = A.current_batch(A.Store(), self.d)
        self.assertEqual((bt["state"], bt["data"]["depth"]), ("checking", "verify"))
        ef = self.checked("fix", "infra/bin/backtest-alerts", msg="backtest-alerts: the fix\n\nFixes-Red: 6")
        wx, _ = self.agent("replay", "infra/bin/backtest-alerts", "replay\n", msg="fix replay: backtest-alerts")
        ex, _ = self.queued(wx)
        did = A.tick_changes(self.r.path, self.d)
        self.assertNotIn(f"{ef}: its push waits", " ".join(did))
        self.assertEqual(self.entry(ef)["status"], "landed", f"the red fix lands while the batch verifies: {did}")
        self.assertEqual(self.remote_log()[0], "backtest-alerts: the fix")
        self.assertNotIn(ef, A.current_batch(A.Store(), self.d)["data"]["members"])
        A.tick_changes(self.r.path, self.d)
        self.assertEqual(self.entry(ex)["status"], "rejected", "the overlapping change no longer waits on the fix: it "
                         "conflicts with what landed, so its author rebases now")
        self.assertIn("no longer applies", self.entry(ex)["why"])

    def test_a_fix_of_an_open_red_outside_the_gate_is_no_push_batch_member_and_none_forms_under_it(self):
        """NO PUSH BATCH UNDER A RED FIX IN FLIGHT (batch.py; 2026-11-19 10:37:14Z: push batch #48 formed 13 s before
        08010c, a red fix just verified, pushed; its verify round passed at 10:46 and was thrown away)."""
        self.open_outside_red()
        ea = self.checked("a", "web/src/main/A.scala")
        eb = self.checked("b", "worker/src/main/B.scala")
        ef = self.checked("fix", "infra/bin/backtest-alerts", msg="backtest-alerts: the fix\n\nFixes-Red: 6")
        self.main_moves()
        self.assertEqual(A.push_candidates(self.r.path, self.d, set()), [], "the red fix goes first")
        e = self.entry(ef)
        e["status"] = "landed"
        A.save_entry(self.d, e)
        self.assertEqual([e["id"] for e in A.push_candidates(self.r.path, self.d, set(), facts=A.queue_facts(set()))],
                         [ea, eb])
        self.assertEqual([e["id"] for e in A.push_candidates(self.r.path, self.d, set())], [ea, eb],
                         "the open reds read from the store when not given")

    def test_a_checked_change_held_behind_a_batch_says_so_and_its_deadline_laps_quietly(self):
        """`checked`, PAST ITS DEADLINE (level 0) and no reason: the push's hold is recorded and shown as a queued
        change's is (A HELD WAIT IS NO ESCALATION)."""
        ea = self.checked("a", "web/src/main/A.scala")
        eb = self.checked("b", "worker/src/main/B.scala")
        self.main_moves()
        A.spawn_batch_executor = lambda repo, d_, bid: None
        self.run_queue([ea, eb], n=3)
        bid = A.current_batch(A.Store(), self.d)["id"]
        ec = self.checked("c", "scripts/c.sh")
        A.tick_changes(self.r.path, self.d)
        row = next(r for r in A.change_rows(A.Store()) if r["id"] == ec)
        self.assertEqual(row["state"], "checked")
        self.assertIn(f"its push waits for batch {bid}", row["hold"] or "", "land status names the hold")
        A.tick_changes(self.r.path, self.d, now_=time.time() + 20 * 60)  # past its 5-min deadline
        st = A.Store()
        it = st.item("change", ec)
        self.assertEqual((it["state"], it["level"]), ("checked", 0), "a held push is no escalation")
        evs = st.item_events("change", ec)
        self.assertEqual([e for e in evs if e["severity"] == "escalate"], [], "nothing reaches the owner")
        lap = [e for e in evs if "checked for" in str(e.get("text"))]
        self.assertTrue(lap and f"held: its push waits for batch {bid}" in lap[-1]["text"], lap)

    def test_a_checked_change_whose_push_is_under_way_is_held_by_it(self):
        """ISSUE 20261119T123823-f7d6c9: lane-policy-redfix, checked 33 min behind another change's verification, was
        filed as unheld by the watchdog in the seconds its push ran: the tick cleared its hold BEFORE push_change, which
        then found main moved and re-queued it. The push itself is the hold while it runs."""
        ec = self.checked("c", "scripts/c.sh")
        seen = []
        saved = A.push_change

        def pushing(repo, d, e):
            later = time.time() + 40 * 60  # the watchdog's pass, mid-push, past the change's deadline
            seen.append([it["id"] for it, _e in A.unheld_changes(A.Store(), {e["id"]: e}, later)])
            return "the landing lease is busy"
        A.push_change = pushing
        try:
            A.tick_changes(self.r.path, self.d)
        finally:
            A.push_change = saved
        self.assertEqual(seen, [[]], f"a change whose push runs is held by that push ({ec})")

    def test_a_checked_change_past_its_deadline_that_nothing_holds_is_filed_as_an_agentctl_issue_once(self):
        """A CHANGE PAST ITS DEADLINE WITH NO RECORDED HOLD IS agentctl's BUG (changekind.py): one agentctl issue, never
        an owner page; the lap's line still says nothing holds it and what its last push answered."""
        ec = self.checked("c", "scripts/c.sh")
        saved = A.push_change
        A.push_change = lambda repo, d, e: "the landing lease is busy"
        try:
            A.tick_changes(self.r.path, self.d, now_=time.time() + 20 * 60)
            A.tick_changes(self.r.path, self.d, now_=time.time() + 40 * 60)
        finally:
            A.push_change = saved
        st = A.Store()
        it = st.item("change", ec)
        self.assertEqual((it["state"], it["level"]), ("checked", 0), "no escalation: the owner is not paged")
        issues = [i for i in st.items("issue", ("open",)) if i["data"]["text"].startswith(f"[change-unheld {ec}]")]
        self.assertEqual(len(issues), 1, "filed once")
        self.assertEqual(it["data"].get("unheld_issue"), issues[0]["id"])
        evs = st.item_events("change", ec)
        self.assertEqual([e for e in evs if e["severity"] == "escalate"], [])
        ev = next(e for e in evs if "nothing holds it" in str(e.get("text")))
        self.assertIn("the landing lease is busy", ev["text"], "and its last push's answer said")
        self.assertIn(f"agentctl issue {issues[0]['id']}", ev["text"])


class OverlapTest(BacklogBatchTest):
    """OVERLAPPING CHANGES ARE CHECKED AS ONE (queue.py; the coordinator, 2026-11-18: B1 against the CI-workflows change,
    the scraper catalog against the scrapers refactor, the cycle-breaking change against both -- each checked alone,
    then NOT LANDED "no longer applies", a rebase and a full recheck)."""

    def setUp(self):
        super().setUp()
        self.r.write("web/src/main/Ten.scala", "".join(f"line {i}\n" for i in range(1, 11)))
        self.r.commit("a ten-line file")
        self.r.push()

    def edit(self, name, path, line, text, extra=None, alone=False):
        """A change editing one line of path (and writing extra); alone: landed with --no-batch, checked alone."""
        wt = self.r.worktree(f"wt-{name}", branch=name)
        with open(os.path.join(wt, path)) as f:
            lines = f.read().splitlines(True)
        lines[line] = text + "\n"
        self.r.write(path, "".join(lines), at=wt)
        if extra:
            self.r.write(extra, f"{name}\n", at=wt)
        self.r.commit(f"{name}: {path}", at=wt)
        if alone:
            rc, out = self.land(wt, "--no-batch")
            self.assertEqual(rc, 0, out)
            return out.split("QUEUED ", 1)[1].split()[0]
        return self.queued(wt)[0]

    def test_a_younger_change_that_cannot_be_assembled_with_an_older_one_is_told_at_once_and_not_checked(self):
        eo = self.edit("older", "web/src/main/Ten.scala", 4, "older's line 5", extra="identity/src/main/I.scala")
        ey = self.edit("younger", "web/src/main/Ten.scala", 4, "younger's line 5")
        did = A.tick_changes(self.r.path, self.d)
        self.assertEqual(self.started, [eo], f"the older (an identity change) is checked alone, the younger waits: {did}")
        told = [x for x in self.inbox() if x["kind"] != "QUEUED" and "CONFLICTS" in json.dumps(x)]
        self.assertEqual(len(told), 1, "its author told at its first tick, before any check")
        self.assertIn("web/src/main/Ten.scala", json.dumps(told[0]))
        self.assertEqual(told[0]["cwd"], self.entry(ey)["worktree"])
        A.tick_changes(self.r.path, self.d)
        self.assertEqual(len([x for x in self.inbox() if "CONFLICTS" in json.dumps(x)]), 1, "told once")
        self.assertNotIn(ey, self.started, "never checked while the older is in flight")

    def test_a_younger_change_sharing_a_file_waits_and_is_checked_once_on_top_of_the_older(self):
        eo = self.edit("older", "web/src/main/Ten.scala", 0, "older's line 1", extra="identity/src/main/I.scala",
                       alone=True)
        ey = self.edit("younger", "web/src/main/Ten.scala", 9, "younger's line 10")
        A.tick_changes(self.r.path, self.d)
        self.assertEqual(self.started, [eo], "the younger waits for the older it shares a file with")
        self.assertEqual([x for x in self.inbox() if "CONFLICTS" in json.dumps(x)], [], "no conflict to tell")
        A.check_entry(self.r.path, eo)
        A.tick_changes(self.r.path, self.d)  # the older pushed, then the younger's check starts on top of it
        self.assertEqual(self.entry(eo)["status"], "landed")
        self.assertEqual(self.started, [eo, ey])
        A.check_entry(self.r.path, ey)
        A.tick_changes(self.r.path, self.d)
        e = self.entry(ey)
        self.assertEqual(e["status"], "landed", "checked once, on top of the older one: no recheck")
        self.assertFalse(e.get("rounds"), "no second round")
        self.assertEqual(len(self.calls_of("--range")), 2, "one check each")

    def test_a_younger_change_sharing_a_file_with_an_older_red_fix_waits_for_it(self):
        """ONE CLASSIFIER (changekind.py): an older fix of an open red is in no batch, so it is no backlog-batch partner
        either. Before, the overlap hold asked backlog_batchable, which never asked whether a change is a red fix: the
        younger change sharing the fix's file was taken to be checked with it in the next batch, so it was not held --
        and the batch left the fix out, so the two were checked apart and the younger rechecked in full once the fix
        landed."""
        self.r.write("infra/bin/backtest-alerts", "".join(f"line {i}\n" for i in range(1, 11)))
        self.r.commit("the backtest")
        self.r.push()
        A.Store().put_item("red", "5254bffda9d2", "fix-pending",
                           {"key": "5254bffda9d2", "workflow": "Alert backtest", "gate": False, "red_kind": "outside",
                            "runs": ["44479918396", "5", "6"]})
        wf = self.r.worktree("wt-fix", branch="fix")
        self.r.write("infra/bin/backtest-alerts", "the fix\n" + "".join(f"line {i}\n" for i in range(2, 11)), at=wf)
        self.r.commit("backtest-alerts: the fix\n\nFixes-Red: 6", at=wf)
        ef = self.queued(wf)[0]
        ey = self.edit("younger", "infra/bin/backtest-alerts", 9, "younger's line 10", extra="docs/y.md")
        A.tick_changes(self.r.path, self.d)
        self.assertEqual(self.started, [ef], "the fix goes; the younger waits for it")
        st = A.Store()
        self.assertEqual(st.item("change", ef)["data"]["change_kind"]["reds"],
                         [{"name": "6", "red": "5254bffda9d2", "kind": "outside"}])
        hold = st.item("change", ey)["data"]["hold"]
        self.assertEqual((hold["kind"], hold["blocker"]), ("overlap", ef))

    def test_overlap_held_changes_wait_quietly_naming_their_blocker_and_file(self):
        """2026-11-19 02:43-03:14Z: phase3-f2 (held behind phase3-agreement on coupling-baseline.tsv) and
        phase3-enrichment2 (an identity change, held behind f2 on TestWiring.scala) each escalated to the owner every
        15 min, "queued for 45 min, waiting on a check slot". A legitimate hold is a quiet wait that names itself."""
        self.r.write("web/src/main/TestWiring.scala", "".join(f"wire {i}\n" for i in range(1, 11)))
        self.r.commit("the test wiring")
        self.r.push()
        ea = self.edit("agreement", "web/src/main/Ten.scala", 0, "agreement's line", extra="identity/src/main/I.scala",
                       alone=True)
        ef = self.edit("f2", "web/src/main/Ten.scala", 9, "f2's line", extra="web/src/main/TestWiring.scala")
        wt = self.r.worktree("wt-e2", branch="e2")  # the identity change, sharing TestWiring.scala with f2
        self.r.write("web/src/main/TestWiring.scala", "e2\n", at=wt)
        self.r.write("identity/src/main/E.scala", "e2\n", at=wt)
        self.r.commit("e2", at=wt)
        rc, out = self.land(wt, "--no-batch")  # checked alone, as an identity change was before it could batch
        self.assertEqual(rc, 0, out)
        ee = out.split("QUEUED ", 1)[1].split()[0]
        A.tick_changes(self.r.path, self.d)
        self.assertEqual(self.started, [ea])
        A.tick_changes(self.r.path, self.d, now_=time.time() + 16 * 60)  # each past its 15-min deadline
        st = A.Store()
        for eid, blocker, path in ((ef, ea, "web/src/main/Ten.scala"), (ee, ef, "web/src/main/TestWiring.scala")):
            it = st.item("change", eid)
            self.assertEqual((it["state"], it["level"]), ("queued", 0), "a held wait is never a level up")
            hold = it["data"]["hold"]
            self.assertEqual((hold["kind"], hold["blocker"]), ("overlap", blocker))
            self.assertIn(path, hold["why"])
            evs = st.item_events("change", eid)
            self.assertEqual([e for e in evs if e["severity"] == "escalate"], [], "nothing reaches the owner")
            lap = [e for e in evs if "queued for" in str(e.get("text"))]
            self.assertTrue(lap and f"held: its check waits for {blocker}" in lap[-1]["text"], lap)
            self.assertNotIn("a check slot", lap[-1]["text"])
            # 2026-11-19 08:39Z/08:55Z: movies-fix-replay's held laps ("queued for 15 min, held: ...") still landed in
            # the inbox every 15 min (an audience made the projector write each one). A quiet lap is no inbox entry.
            self.assertEqual([e["audience"] for e in lap if e["audience"]], [], "a held lap has no audience")
            row = next(r for r in A.change_rows(st) if r["id"] == eid)
            self.assertIn(f"waits for {blocker}", row["hold"], "land status names the real hold")
        self.assertIsNone(A.change_hold_stuck(st, st.item("change", ef)["data"]["hold"], st.item("change", ef),
                                              time.time()))
        it = st.item("change", ee)
        self.assertIn("nothing holds it", A.change_hold_stuck(st, None, it, time.time()))
        self.assertIn("held for over", A.change_hold_stuck(st, it["data"]["hold"], it, time.time() + 7 * 3600))

    def test_a_change_waiting_for_a_green_gate_holds_no_younger_change_sharing_its_file(self):
        """2026-11-20, Main #2716 red: metrics-packages and phase7-houses sat `queued ... held: its check waits for
        movies-misc-cx (waiting)` -- a change that lands only at green, so they were held until green (and a red fix so
        held, a cycle); cancelling misc-cx moved the hold to the next waiting change. A change is held only behind one
        checked or landed before it: the waiting ones and it meet in the batch at green."""
        eo = self.edit("older", "web/src/main/Ten.scala", 0, "older's line 1", extra="identity/src/main/I.scala")
        e = self.entry(eo)
        e["status"] = "waiting"  # checked, waiting for the gate to turn green
        A.save_entry(self.d, e)
        ey = self.edit("younger", "web/src/main/Ten.scala", 9, "younger's line 10", extra="identity/src/main/Y.scala")
        A.tick_changes(self.r.path, self.d)
        self.assertEqual(self.started, [ey], "checked now, never held behind a change waiting on the gate")
        self.assertNotEqual(((A.Store().item("change", ey)["data"] or {}).get("hold") or {}).get("kind"), "overlap")

    def test_a_kept_place_never_jumps_an_overlapping_check_in_flight(self):
        """The simulated 2026-11-19 seed 0: a change queued again after a conflict took its first entry's seq and so
        was "older" than a change it overlaps that was already checking: checked beside it, then rechecked in full."""
        ea = self.edit("a", "web/src/main/Ten.scala", 0, "a's line 1", alone=True)
        A.tick_changes(self.r.path, self.d)
        self.assertEqual(self.started, [ea])
        A.set_change(self.d, self.entry(ea), "checking")  # its check under way
        eb = self.edit("b", "web/src/main/Ten.scala", 9, "b's line 10")
        e = self.entry(eb)
        e["seq"] = self.entry(ea)["seq"] - 1  # its kept place: older by seq than the change checking now
        A.save_entry(self.d, e)
        A.tick_changes(self.r.path, self.d)
        self.assertNotIn(eb, self.started, "b waits for the check in flight")
        self.assertEqual(A.Store().item("change", eb)["data"]["hold"]["blocker"], ea)

    def test_cancelling_a_blocker_clears_the_holds_that_name_it(self):
        eo = self.edit("older", "web/src/main/Ten.scala", 0, "older's line 1", extra="identity/src/main/I.scala",
                       alone=True)
        ey = self.edit("younger", "web/src/main/Ten.scala", 9, "younger's line 10", extra="identity/src/main/Y.scala")
        A.tick_changes(self.r.path, self.d)
        st = A.Store()
        self.assertEqual(st.item("change", ey)["data"]["hold"]["blocker"], eo)
        A.cancel_submitted(eo, repo=self.r.path)
        self.assertIsNone(st.item("change", ey)["data"].get("hold"),
                          "no hold names a cancelled change: `land status` never shows a phantom blocker")

    def test_land_status_of_a_checking_change_names_its_remote_run_and_jobs(self):
        """2026-11-20: five changes `checking`, and `land status <id>` said only "OPEN: checking for 25 min"."""
        import contextlib, io
        st, t = A.Store(), time.time()
        st.put_item("change", "20261120-055534-movies-x-1", "checking", {"worktree": "/w", "head": "a" * 40})
        A.remote_mirror("20261120-055534-movies-x-1", {
            "branch": "check/x-1", "run": 44682642375, "run_status": "waiting", "pushed_at": t - 5400,
            "progress_at": t - 5300, "unlisted_at": t - 900, "jobs": [
                {"name": "plan", "status": "completed", "conclusion": "success"},
                {"name": "check (identity 1/2)", "status": "in_progress", "started_at": A.iso_at(t - 960)},
                {"name": "check (rest)", "status": "queued"}]})
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            A.land_status_entry(st, "movies-x-1", t)
        text = out.getvalue()
        self.assertIn("run 44682642375 on check/x-1, pushed 1 h ago", text)
        self.assertIn("not in GitHub's run listing", text)
        self.assertIn("check (identity 1/2): running 16 min", text)
        self.assertIn("check (rest): waiting for a runner slot", text)

    def test_two_queued_changes_sharing_a_file_that_assemble_go_in_one_backlog_batch(self):
        ea = self.edit("a", "web/src/main/Ten.scala", 0, "a's line 1")
        eb = self.edit("b", "web/src/main/Ten.scala", 9, "b's line 10")
        A.tick_changes(self.r.path, self.d)
        self.assertEqual(self.started, [], "both wait for the backlog batch")
        self.drive()
        self.assertEqual([self.entry(x)["status"] for x in (ea, eb)], ["landed", "landed"])
        self.assertEqual(len(self.calls_of("--range")), 1, "checked as one tree")


class RedPrecheckTest(BacklogBatchTest):
    """THE BATCH IS THE MECHANISM (batch.py; the owner, 2026-11-20): while the gate is red, the waiting changes (each
    checked on its own first) are pre-checked as ONE tree; at green they land in ONE push, checked again only as MINIMAL
    RECHECKS says (the repo's hook answers what its checks read: test_queue.READS_HOOK)."""

    def setUp(self):
        super().setUp()
        self.r.write("scripts/hooks/pre-push", test_queue.READS_HOOK + REPO_HOOK.format(calls=self.calls).split("\n", 1)[1])
        os.chmod(os.path.join(self.r.path, "scripts/hooks/pre-push"), 0o755)
        self.r.commit("the hook's read plans")
        self.r.push()
        for p in (self.pushes, self.calls):
            if os.path.exists(p):
                os.remove(p)
        self.reds = {"7"}

    def lands_on_main(self, path):
        other, _ = self.agent("fix", path, "fix\n", msg=f"the red's fix: {path}")
        sh(other, "git", "push", "-q", "origin", "HEAD:main")

    def red_batch(self, *changes):
        ids = [self.queue(n, p) for n, p in changes]
        for x in ids:  # each checked on its own (in parallel, in the free trees), then waiting for green
            self.check(x)
        self.assertEqual([self.entry(x)["status"] for x in ids], ["waiting"] * len(ids), "each joins the waiting batch")
        before = len(self.calls_of("--checks-only"))
        self.drive()
        self.pre_checks = len(self.calls_of("--checks-only")) - before
        return ids

    def test_waiting_changes_are_pre_checked_as_one_and_land_in_one_push_with_no_check_again(self):
        ea, eb = self.red_batch(("a", "web/src/main/A.scala"), ("b", "worker/src/main/B.scala"))
        self.assertEqual(self.pre_checks, 1, "ONE pre-check of the two as one tree, while red")
        self.assertEqual(self.batch()["data"]["prechecked"]["members"], [ea, eb])
        self.reds = set()
        self.lands_on_main("docs/fix.md")  # the red's fix: read by no check of theirs
        before = len(self.pushed())
        self.drive()
        self.assertEqual([self.entry(x)["status"] for x in (ea, eb)], ["landed", "landed"])
        self.assertEqual(len(self.pushed()) - before, 1, "one push")
        self.assertIn("skip=sbt", self.pushed()[-1], "it skips the plan its pre-check passed")
        self.assertEqual([c.split()[0] for c in open(self.calls).read().splitlines()], ["--checks-only"] * 3,
                         "their own checks and the pre-check; no check again at green: its verdict kept")

    def test_a_fix_their_checks_read_gets_the_fast_check_at_green(self):
        ea, eb = self.red_batch(("a", "web/src/main/A.scala"), ("b", "worker/src/main/B.scala"))
        self.reds = set()
        self.lands_on_main("common/src/main/Fix.scala")  # compiled under both
        self.drive()
        self.assertEqual([self.entry(x)["status"] for x in (ea, eb)], ["landed", "landed"])
        self.assertEqual([c.split()[0] for c in open(self.calls).read().splitlines()], ["--checks-only"] * 3 + ["--fast-only"])

    def test_a_failing_member_is_bisected_out_and_the_rest_land_pre_checked(self):
        """PartA and PartB pass alone and fail together (the fixture's hook): only the combined pre-check sees it."""
        ea, eb = self.red_batch(("a", "web/src/main/PartA.scala"), ("b", "worker/src/main/PartB.scala"))
        self.assertEqual(self.entry(eb)["status"], "rejected", "the culprit, its failure its own")
        self.assertIn("NOT LANDED", A.Store().item("change", eb)["data"]["why"])
        self.assertEqual(self.batch()["data"]["prechecked"]["members"], [ea])
        self.reds = set()
        self.lands_on_main("docs/fix.md")
        self.drive()
        self.assertEqual(self.entry(ea)["status"], "landed")
        self.assertNotIn("--fast-only", " ".join(open(self.calls).read().split()))

    def test_a_change_joining_after_the_pre_check_is_pre_checked_again_with_the_others(self):
        ea, = self.red_batch(("a", "web/src/main/A.scala"))
        eb, = self.red_batch(("b", "worker/src/main/B.scala"))
        self.assertEqual(self.pre_checks, 1, "pre-checked again, with the others")
        self.assertEqual(self.batch()["data"]["prechecked"]["members"], [ea, eb])

    def test_a_red_fix_still_goes_alone_first(self):
        wf = self.r.worktree("wt-fix", branch="fix")
        self.r.write("web/src/main/Fix.scala", "fix\n", at=wf)
        self.r.commit("the fix\n\nFixes-Red: 7", at=wf)
        ef = self.queued(wf)[0]
        ea, = self.red_batch(("a", "worker/src/main/A.scala"))
        self.assertEqual(self.check(ef), 0)
        self.assertEqual(self.entry(ef)["status"], "landed", "the fix lands alone, at once, the batch waiting")
        self.assertEqual(self.entry(ea)["status"], "waiting")


for _name in dir(BacklogBatchTest):  # its setUp only, not its tests
    for _cls in (OverlapTest, RedPrecheckTest):
        if _name.startswith("test_") and _name not in _cls.__dict__:
            setattr(_cls, _name, None)
for _name in dir(test_queue.QueueTest):  # its setUp only, not its tests
    for _cls in (BacklogBatchTest, PushBatchTest):
        if _name.startswith("test_") and _name not in _cls.__dict__:
            setattr(_cls, _name, None)

if __name__ == "__main__":
    unittest.main()
