"""THE LAND QUEUE (the owner, 2026-10-27: "agents never wait on a land"): `land` rebases, guards and returns at once with
QUEUED <id>; the processor's checker runs the change's checks in a check tree of its own, within the governor, then
pushes it (green), puts it on `waiting` (red) or rejects it, every verdict to the inbox with the agent's worktree; under
pressure the checks run on ci-runner's check tier and the push carries the remote run for the hook to confirm."""
import contextlib, io, json, os, re, subprocess, sys, tempfile, time, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A, StallSamples, put_change, sh  # noqa: E402
from fake_clock import FakeClock, lagging_clock, on_clock  # noqa: E402
from test_waiting import REPO_HOOK, WaitingTest, assert_queued_line_names_no_verdict  # noqa: E402

PLAN_HOOK = """#!/bin/sh
case "$1" in
    --compile-plan)
        while read -r p; do
            case "$p" in
                common/*.scala) printf 'common/Test/compile\\nworker/Test/compile\\nweb/Test/compile\\n' ;;
                worker/*.scala) echo worker/Test/compile ;;
                web/*.scala)    echo web/Test/compile ;;
            esac
        done
        exit 0 ;;
    --plan) cat > /dev/null; printf 'sbt\\nidentity\\n'; exit 0 ;;
    --lint-plan) cat > /dev/null; [ ! -f scripts/hooks/lint-registry.tsv ] || grep -v '^#' scripts/hooks/lint-registry.tsv; exit 0 ;;
esac
# --verify-only A..B: the combined tree (every test config compiled, the touched modules' unit tests)
"""


class QueueTest(WaitingTest):
    def setUp(self):
        super().setUp()
        os.environ["AGENTCTL_QUEUE"] = "1"
        self.reds = set()
        # the push's hook records what the processor handed it
        hooks = os.path.join(self.tmp.name, "hooks")
        with open(os.path.join(hooks, "pre-push"), "w") as f:
            f.write(f'#!/bin/sh\ncat > /dev/null\n'
                    f'echo "push remote=${{PREPUSH_REMOTE_RUN:-}}${{PREPUSH_SKIP:+ skip=$PREPUSH_SKIP}}" >> "{self.pushes}"\n')
        self.saved_q = A.remote_check
        # the repo's hook answers the dry-run queries the re-check rule reads: its compile plan (common <- worker,
        # web) and its plan of checks
        self.r.write("scripts/hooks/pre-push", PLAN_HOOK + REPO_HOOK.format(calls=self.calls).split("\n", 1)[1])
        os.chmod(os.path.join(self.r.path, "scripts/hooks/pre-push"), 0o755)
        self.r.commit("the hook's plans")
        self.r.push()
        for p in (self.pushes, self.calls):
            if os.path.exists(p):
                os.remove(p)

    def tearDown(self):
        A.remote_check = self.saved_q
        os.environ["AGENTCTL_QUEUE"] = "0"
        super().tearDown()

    def queued(self, wt):
        rc, out = self.land(wt)
        self.assertEqual(rc, 0, out)
        self.assertRegex(out, r"QUEUED \S+-\d+ \([0-9a-f]{10}")
        eid = out.split("QUEUED ", 1)[1].split()[0]
        assert_queued_line_names_no_verdict(self, out)
        self.assertIn("END YOUR TURN", out)
        return eid, out

    def no_batch(self, wt):
        """The change in wt queued with `land --no-batch`: its id."""
        rc, out = self.land(wt, "--no-batch")
        self.assertEqual(rc, 0, out)
        return out.split("QUEUED ", 1)[1].split()[0]

    def check(self, eid, rounds=6):
        """The change to its outcome, as the processor drives it: the executor's checks, then the tick's push
        (step 6: they are two steps now), round after round while main moves. 1 when it ends rejected."""
        d, _c, _w = A.waiting_state(self.r.path)
        for _ in range(rounds):
            e = A.ChangeRecords(A.process_store(), d).get(eid)
            if e["status"] in ("queued", "checking") and not e.get("retry_after", 0) > self.clock.time():
                A.check_entry(self.r.path, eid, *on_clock(self.clock))
            elif e["status"] == "checked":
                A.push_change(self.r.path, d, e, *on_clock(self.clock))
            else:
                break
        A.project_events(A.CurrentStore(A.process_store))  # the processor's projector: the events become the inbox entries
        e = A.ChangeRecords(A.process_store(), d).get(eid)
        return 1 if e["status"] == "rejected" else 0

    def pushed(self):
        return open(self.pushes).read().splitlines() if os.path.exists(self.pushes) else []

    def test_land_submits_only_the_worktree_it_runs_in(self):
        """2026-11-26: an agent's plain `land` was said to have queued two other agents' worktrees too. Land takes the
        checkout of its cwd (or --repo) and nothing else: another worktree holding unlanded commits is never submitted,
        from a worktree, from a subdirectory of it, or when --repo names one."""
        self.reds = set()
        mine, mine_sha = self.agent("mine", "web/src/main/Mine.scala", "mine\n")
        _other, other_sha = self.agent("other", "web/src/main/Other.scala", "other\n")
        _third, third_sha = self.agent("third", "web/src/main/Third.scala", "third\n")
        os.makedirs(os.path.join(mine, "web", "src"), exist_ok=True)
        rc, out = self.run_tool("land", cwd=os.path.join(mine, "web", "src"))
        self.assertEqual(rc, 0, out)
        self.assertEqual({e["head"] for e in self.entries().values()}, {mine_sha}, "only the worktree land ran in")
        self.assertNotIn(other_sha[:10], out)
        self.assertNotIn(third_sha[:10], out)
        rc, out = self.run_tool("land", "--repo", _third, cwd=mine)
        self.assertEqual({e["head"] for e in self.entries().values()} - {mine_sha}, {third_sha}, "--repo names the one it lands")

    def test_land_returns_at_queued_and_the_checker_lands_it_on_green(self):
        wt, sha = self.agent("a", "web/src/main/A.scala", "a\n", msg="a: one")
        eid, out = self.queued(wt)
        self.assertEqual(self.calls_of("--range"), [], "no check ran in land")
        self.assertEqual(self.entries()[eid.rsplit("-", 1)[-1]]["status"], "queued")
        self.assertEqual([e["kind"] for e in self.inbox()], ["QUEUED"])
        self.assertEqual(self.check(eid), 0)
        self.assertEqual(len(self.calls_of("--range")), 1, "its checks ran in the checker")
        self.assertEqual(self.remote_log()[0], "a: one")
        landed = [e for e in self.inbox() if e["kind"] == "LANDED"]
        self.assertEqual(len(landed), 1)
        self.assertEqual(landed[0]["cwd"], wt, "the verdict names the agent's worktree")
        self.assertEqual(len(self.watches), 1, "a detached watch")
        self.assertEqual(self.entries()[eid.rsplit("-", 1)[-1]]["status"], "landed")
        _d, clone, _w = A.waiting_state(self.r.path)
        self.assertTrue(os.path.isdir(f"{clone}-wt-check-1"), "checked in a check tree, kept warm")

    def checked_change(self):
        """(the waiting dir, the id) of one change checked green, not yet pushed."""
        wt, _ = self.agent("a", "web/src/main/A.scala", "a\n", msg="a: one")
        eid, _ = self.queued(wt)
        d, _c, _w = A.waiting_state(self.r.path)
        A.check_entry(self.r.path, eid, *on_clock(self.clock))
        self.assertEqual(self.entry_of(d, eid)["status"], "checked")
        return d, eid

    def entry_of(self, d, eid):
        return A.ChangeRecords(A.process_store(), d).get(eid)

    def push_fails(self, words):
        """The push's own pre-push hook prints words and refuses."""
        with open(os.path.join(self.tmp.name, "hooks", "pre-push"), "a") as f:
            f.write(f'echo "{words}" >&2\nexit 1\n')

    def test_a_failed_push_waits_longer_each_attempt_and_its_last_escalates(self):
        d, eid = self.checked_change()
        self.push_fails("fatal: unable to access the remote")
        st = A.CurrentStore(A.process_store)
        for n in range(1, A.PUSH_ATTEMPTS_MAX + 1):
            self.assertEqual(A.push_change(self.r.path, d, self.entry_of(d, eid), *on_clock(self.clock)), "checked")
            e = self.entry_of(d, eid)
            self.assertEqual((e["status"], e["push_attempts"]), ("checked", n))
            self.assertEqual(e["retry_after"], self.clock.time() + min(60 * 2 ** n, 1800))
            ev = st.item_events("change", eid)[-1]
            self.assertIn(f"push attempt {n}", ev["text"])
            self.assertEqual(ev["severity"], "escalate" if n >= A.PUSH_ATTEMPTS_MAX else "info", n)
        # git still reads main here: the STALLED condition's probe passes, so none opens
        self.assertEqual([c for c in A.open_conditions(st) if c["key"] == f"push-failing-{eid}"], [])
        self.assertNotEqual(self.remote_log()[0], "a: one")

    def test_a_raced_push_is_tried_again_with_no_attempt_counted(self):
        d, eid = self.checked_change()
        self.push_fails(" ! [rejected] HEAD -> main (non-fast-forward)")
        self.assertEqual(A.push_change(self.r.path, d, self.entry_of(d, eid), *on_clock(self.clock)), "checked")
        e = self.entry_of(d, eid)
        self.assertEqual((e["status"], e.get("push_attempts")), ("checked", None))

    def test_a_change_cancelled_since_its_checks_is_not_pushed(self):
        d, eid = self.checked_change()
        e = self.entry_of(d, eid)
        put_change(A.CurrentStore(A.process_store), d, dict(e, status="cancelled"))
        self.assertEqual(A.push_change(self.r.path, d, e, *on_clock(self.clock)), "cancelled")
        self.assertNotEqual(self.remote_log()[0], "a: one")

    def test_a_solo_change_waits_while_the_batch_checks_and_lands_after_it(self):
        """2026-10-30: batch #4 passed its 45-min check, but a solo change had pushed meanwhile, so it was assembled
        again; then the change tick took its freed check tree for another change: the batch never landed. While the
        batch is open, a checked change does not push, and no change takes a tree the batch needs."""
        def entry(eid):
            return A.ChangeRecords(A.process_store(), d).get(eid)

        def tick():
            started = []
            saved = A.spawn_executor
            A.spawn_executor = lambda repo, d_, eid: started.append(eid)
            try:
                return A.tick_changes(self.r.path, d, *on_clock(self.clock)), started
            finally:
                A.spawn_executor = saved
        d, _c, _w = A.waiting_state(self.r.path)
        self.reds = {"7"}
        wa, _ = self.agent("a", "web/src/main/A.scala", "a\n", msg="a: waits")
        ea, _ = self.queued(wa)
        self.check(ea)  # red: it joins `waiting`
        self.reds = set()
        saved = A.spawn_batch_executor
        A.spawn_batch_executor = lambda repo, d_, bid: None  # its executor runs elsewhere
        try:
            self.assertIn("checking", A.tick_batch(self.r.path, d, *on_clock(self.clock))[0])
        finally:
            A.spawn_batch_executor = saved
        wb, _ = self.agent("b", "worker/src/main/B.scala", "b\n", msg="b: solo")
        eb = self.no_batch(wb)  # no push batch takes it with c: it is pushed alone
        A.check_entry(self.r.path, eb, *on_clock(self.clock))
        self.assertEqual(entry(eb)["status"], "checked")
        did, _started = tick()
        self.assertIn(f"{eb}: its push waits for batch", " ".join(did))
        self.assertEqual(entry(eb)["status"], "checked", "no solo push under the checking batch")
        self.assertEqual(self.remote_log()[0], "the hook's plans")
        st = A.process_store()
        bt = self.batch()
        st.transition("batch", bt["id"], "pushing", bt["version"], event={"cause": "agent"})  # passed: its tree is about to be pushed from
        wc, _ = self.agent("c", "worker/src/main/C.scala", "c\n", msg="c: queued")
        ec, _ = self.queued(wc)
        _did, started = tick()
        self.assertNotIn(ec, started, "no change check takes a tree the batch needs")
        self.drive()  # the batch pushes
        self.assertEqual(entry(ea)["status"], "landed")
        self.assertEqual(self.remote_log()[0], "a: waits", "the batch first")
        _did, started = tick()
        self.assertIn(ec, started, "the batch landed: changes go again")
        tick()  # b: main moved by the batch, no file overlap: its combined tree verified, then pushed
        self.assertEqual((entry(eb)["status"], entry(eb)["verify"]), ("queued", True))
        A.check_entry(self.r.path, eb, *on_clock(self.clock))
        tick()
        self.assertEqual(entry(eb)["status"], "landed")

    def test_a_younger_change_never_pushes_under_an_older_ones_combined_tree_verification(self):
        """2026-11-16, workermem: fully checked by 09:15, then verified FOUR times (09:24, 09:36, 09:58, 10:10), each
        on a main that a younger change's push had moved again during the 10-min verification (grinchrows,
        driftthroughput, iosstall, backlogrows), until a hook change forced a full round: 10 h unlanded. While an
        older change verifies its combined tree, a younger checked change waits, so the verification lands; its own
        verification comes after."""
        def entry(eid):
            return A.ChangeRecords(A.process_store(), d).get(eid)

        def tick():
            started = []
            saved = A.spawn_executor
            A.spawn_executor = lambda repo, d_, eid: started.append(eid)
            try:
                return A.tick_changes(self.r.path, d, *on_clock(self.clock)), started
            finally:
                A.spawn_executor = saved
        d, _c, _w = A.waiting_state(self.r.path)
        # each --no-batch: no push batch takes them together (A LONE VERIFICATION HOLDS NO BATCH, queuepush.py); the
        # hold below is for a change no batch takes
        wz, _ = self.agent("z", "web/src/main/Z.scala", "z\n", msg="z: oldest, its full check not begun")
        ez = self.no_batch(wz)
        wa, _ = self.agent("a", "web/src/main/A.scala", "a\n", msg="a: older")
        ea, _ = self.queued(wa)
        A.check_entry(self.r.path, ea, *on_clock(self.clock))
        other, _ = self.agent("m", "docs/notes.md", "n\n", msg="main moves")
        sh(other, "git", "push", "-q", "origin", "HEAD:main")
        tick()  # a: main moved, no file overlap: its combined tree verified
        self.assertEqual((entry(ea)["status"], entry(ea)["verify"]), ("queued", True))
        wb, _ = self.agent("b", "worker/src/main/B.scala", "b\n", msg="b: younger")
        eb = self.no_batch(wb)
        A.check_entry(self.r.path, eb, *on_clock(self.clock))
        self.assertEqual(entry(eb)["status"], "checked")
        did, started = tick()
        self.assertIn(f"{eb}: its push waits for {ea}'s combined-tree verification", " ".join(did))
        self.assertEqual(entry(eb)["status"], "checked", "no push under the older change's verification")
        self.assertEqual(self.remote_log()[0], "main moves")
        self.assertEqual(started[:2], [ea, ez], "the verification takes the next check tree first")
        A.check_entry(self.r.path, ea, *on_clock(self.clock))  # verified on the main it was queued on: it stands
        tick()
        self.assertEqual(entry(ea)["status"], "landed")
        self.assertEqual(self.remote_log()[0], "a: older", "verified once, then pushed")
        self.assertEqual((entry(eb)["status"], entry(eb)["verify"]), ("queued", True), "b verifies after it")
        A.check_entry(self.r.path, eb, *on_clock(self.clock))
        tick()
        self.assertEqual(entry(eb)["status"], "landed")
        self.assertEqual(self.remote_log()[:2], ["b: younger", "a: older"])

    def test_red_main_the_checked_change_joins_waiting(self):
        self.reds = {"7"}
        wt, sha = self.agent("a", "web/src/main/A.scala", "a\n")
        eid, _ = self.queued(wt)
        self.assertEqual(self.check(eid), 0)
        self.assertEqual(len(self.calls_of("--checks-only")), 1)
        es = self.entries()
        waiting = [e for e in es.values() if e["status"] == "waiting"]
        self.assertEqual([e["id"] for e in waiting], [eid], "the queued change itself waits: no entry of its own")
        self.assertEqual(self.pushed(), [])
        self.assertIn("WAITING", [e["kind"] for e in self.inbox()])

    def test_a_held_change_is_one_item_counted_once_and_one_batch_member(self):
        """2026-11-18 07:15Z: three checked changes held behind a red gate read `waiting: 6 — movies-sentry,
        movies-tg-inapp, flapmem, movies-sentry, movies-tg-inapp, flapmem`: each joined `waiting` as a waiting entry
        with an id of its own, which the server tick projected into a second open item beside the queued one. One
        change is one item; waiting is a state of it; the batch at green carries each change once, in one push."""
        d, _c, _w = A.waiting_state(self.r.path)
        self.reds = {"7"}
        wts, ids = [], []
        for n in ("a", "b", "c"):
            wt, _ = self.agent(n, f"web/src/main/{n.upper()}.scala", f"{n}\n", msg=f"{n}: held")
            eid, _ = self.queued(wt)
            self.assertEqual(self.check(eid), 0)
            wts.append(wt)
            ids.append(eid)
        rows = A.change_rows(A.CurrentStore(A.process_store))
        self.assertEqual([(r["id"], r["state"]) for r in rows], [(i, "waiting") for i in ids], "one item per change")
        A.write_snapshot(d, A.CurrentStore(A.process_store))
        line = A.status_line()
        self.assertIn("waiting: 3 — ", line)
        for wt in wts:
            self.assertEqual(line.count(re.sub(r"^.*-wt-", "", os.path.basename(wt))), 1, line)
        self.reds = set()
        saved = A.spawn_batch_executor
        A.spawn_batch_executor = lambda repo, d_, bid: None
        try:
            self.assertIn("checking", A.tick_batch(self.r.path, d, *on_clock(self.clock))[0])
        finally:
            A.spawn_batch_executor = saved
        self.assertEqual(self.batch()["data"]["members"], ids, "the batch's members are the changes, each once")
        self.drive()
        self.assertEqual([self.entries()[i.rsplit("-", 1)[-1]]["status"] for i in ids], ["landed"] * 3)
        self.assertEqual(len(self.pushed()), 1, "one push")
        self.assertEqual(self.remote_log()[:3], ["c: held", "b: held", "a: held"])
        self.assertEqual(A.change_rows(A.CurrentStore(A.process_store)), [])

    def test_failing_checks_are_not_landed_in_the_inbox(self):
        wt, _ = self.agent("a", "BROKEN", "x\n")
        eid, _ = self.queued(wt)
        self.assertEqual(self.check(eid), 1)
        bad = [e for e in self.inbox() if e["kind"] == "NOT_LANDED"]
        self.assertEqual(len(bad), 1)
        self.assertIn("pre-push checks failed", bad[0]["verdict"])
        self.assertEqual(bad[0]["cwd"], wt)
        self.assertEqual(self.pushed(), [])

    def test_main_moving_after_queued_is_rebased_in_the_check_tree(self):
        wt, _ = self.agent("a", "web/src/main/A.scala", "a\n", msg="a: one")
        eid, _ = self.queued(wt)
        other, _ = self.agent("b", "worker/src/main/B.scala", "b\n", msg="b: two")
        sh(other, "git", "push", "-q", "origin", "HEAD:main")
        self.assertEqual(self.check(eid), 0)
        self.assertEqual(self.remote_log()[:2], ["a: one", "b: two"])

    def test_a_main_that_keeps_moving_is_checked_again_each_round_then_lands(self):
        """Step 6: main moving with an overlap is a new round (queued), never an attempt; no check runs in the push."""
        wt, _ = self.agent("a", "web/src/main/A.scala", "a\n", msg="a: one")
        eid, _ = self.queued(wt)
        real, n = A.run_entry_checks, []

        def moving(repo, d, e, tree, base, head, mode, cls, log_path, clock, st, own=None):
            n.append(1)
            if len(n) <= 2:  # the same sbt module (web): an overlap, so each move means a new round
                other, _ = self.agent(f"m{len(n)}", f"web/src/main/M{len(n)}.scala", "m\n", msg=f"main moves {len(n)}")
                sh(other, "git", "push", "-q", "origin", "HEAD:main")
            return real(repo, d, e, tree, base, head, mode, cls, log_path, clock, st, own)
        A.run_entry_checks = moving
        self.addCleanup(setattr, A, "run_entry_checks", real)
        self.assertEqual(self.check(eid), 0)
        self.assertEqual(len(n), 3, "checked once per round")
        self.assertEqual(self.remote_log()[:2], ["a: one", "main moves 2"])
        self.assertEqual(self.entries()[eid.rsplit("-", 1)[-1]]["rounds"], 2)
        self.assertNotIn("by the push's hook", self.landed_verdict())

    def moves_once(self, *moves):
        """Main moves during the first round's checks: one commit per (path, text) landed by another agent."""
        real, n = A.run_entry_checks, []

        def moving(repo, d, e, tree, base, head, mode, cls, log_path, clock, st, own=None):
            n.append(1)
            if len(n) == 1:
                for i, (path, text) in enumerate(moves):
                    other, _ = self.agent(f"m{i}", path, text, msg=f"main moves {i}")
                    sh(other, "git", "push", "-q", "origin", "HEAD:main")
            return real(repo, d, e, tree, base, head, mode, cls, log_path, clock, st, own)
        A.run_entry_checks = moving
        self.addCleanup(setattr, A, "run_entry_checks", real)
        return n

    def landed_verdict(self):
        return [e for e in self.inbox() if e["kind"] == "LANDED"][0]["verdict"]

    def verified(self):
        """The checks' calls as (mode, range) pairs."""
        return [tuple(l.split()[:2]) for l in open(self.calls).read().splitlines()] if os.path.exists(self.calls) else []

    def test_disjoint_files_landing_meanwhile_verify_the_combined_tree_then_push_without_a_full_recheck(self):
        """The owner, 2026-11-16: file granularity, "but always recompile and unit test"."""
        wt, _ = self.agent("a", "web/src/main/A.scala", "a\n", msg="a: one")
        eid, _ = self.queued(wt)
        full_base = sh(self.r.root, "git", "--git-dir", self.r.bare, "rev-parse", "main").strip()
        rounds = self.moves_once(("worker/src/main/W.scala", "w\n"), ("docs/notes.md", "n\n"))
        self.assertEqual(self.check(eid), 0)
        self.assertEqual(len(rounds), 2, "the full check, then the combined tree's verification")
        tip = sh(self.r.root, "git", "--git-dir", self.r.bare, "rev-parse", "main").strip()
        self.assertEqual([m for m, _r in self.verified()], ["--range", "--verify-only"])
        self.assertEqual(self.verified()[1][1], f"{full_base}..{tip}",
                         "verified over what landed since the full check and the change, together")
        self.assertEqual(self.remote_log()[0], "a: one", "rebased onto the moved main and pushed")
        self.assertIn("no file overlap, the combined tree verified", self.landed_verdict())
        self.assertEqual(self.pushed()[-1], "push remote= skip=sbt identity", "the push skips the checks already passed")
        self.assertFalse(self.entries()[eid.rsplit("-", 1)[-1]].get("verify"))

    def test_the_same_sbt_module_without_a_shared_file_is_verified_not_fully_rechecked(self):
        """2026-11-15: rev-scripts was fully checked five times over `sbt modules overlap (worker and worker)`."""
        wt, _ = self.agent("a", "worker/src/test/scala/ASpec.scala", "a\n", msg="a: one")
        eid, _ = self.queued(wt)
        self.moves_once(("worker/src/test/scala/BSpec.scala", "b\n"), ("common/src/main/C.scala", "c\n"))
        self.assertEqual(self.check(eid), 0)
        self.assertEqual([m for m, _r in self.verified()], ["--range", "--verify-only"])
        self.assertEqual(self.pushed()[-1], "push remote= skip=sbt identity")

    def test_a_failing_combined_tree_is_not_landed(self):
        wt, _ = self.agent("a", "web/src/main/PartA.scala", "a\n", msg="a: one")
        eid, _ = self.queued(wt)
        self.moves_once(("worker/src/main/PartB.scala", "b\n"))  # the fake hook fails on PartA beside PartB
        self.assertEqual(self.check(eid), 1)
        self.assertEqual([m for m, _r in self.verified()], ["--range", "--verify-only"])
        self.assertNotIn("a: one", self.remote_log())
        self.assertIn("pre-push checks failed", [e for e in self.inbox() if e["kind"] == "NOT_LANDED"][0]["verdict"])

    def test_a_first_network_shaped_check_failure_is_an_infra_requeue_never_the_changes_rejection(self):
        """2026-11-22 (packet loss on the tunnel to ci-runner): a change's first check that failed reaching the network
        -- no test failed, the remote did not answer -- went to judge_failure, which filed it "(no failing test named)"
        against the change: NOT LANDED for the network's blip. Only a change that had been re-queued for the network
        before was re-queued again. Now any network-shaped failure with no test runner's failure line is infra."""
        hook = os.path.join(self.r.path, "scripts/hooks/pre-push")
        flag = os.path.join(self.tmp.name, "net-failed-once")
        self.r.write("scripts/hooks/pre-push", open(hook).read().replace(
            "exit 0\n", f"if git ls-files | grep -q NetFlake && [ ! -e {flag} ]; then touch {flag}; "
            "echo \"fatal: unable to access 'https://github.com/o/g.git/': Could not resolve host: github.com\"; "
            "echo '! pre-push sbt FAILED (3s)'; exit 1; fi\nexit 0\n", 1))
        self.r.commit("a hook whose first run cannot reach the network")
        self.r.push()
        wt, _ = self.agent("a", "web/src/main/NetFlake.scala", "a\n", msg="a: one")
        eid, _ = self.queued(wt)
        A.check_entry(self.r.path, eid, *on_clock(self.clock))
        A.project_events(A.CurrentStore(A.process_store))
        self.assertTrue(os.path.exists(flag), "its check ran, and failed on the network")
        e = self.entries()[eid.rsplit("-", 1)[-1]]
        self.assertEqual((e["status"], e.get("infra_reason")), ("queued", A.NETWORK_REASON))
        self.assertEqual([x for x in self.inbox() if x["kind"] == "NOT_LANDED"], [], "the network's blip is no verdict")

    def test_a_test_failure_beside_a_network_line_is_still_the_changes(self):
        hook = os.path.join(self.r.path, "scripts/hooks/pre-push")
        self.r.write("scripts/hooks/pre-push", open(hook).read().replace(
            "exit 0\n", "if git ls-files | grep -q NetFlake; then echo 'Connection refused'; "
            "echo '[info] - should parse *** FAILED ***'; echo '[error] Failed tests: a.ASpec'; exit 1; fi\nexit 0\n", 1))
        self.r.commit("a hook whose test fails beside a network line")
        self.r.push()
        wt, _ = self.agent("a", "web/src/main/NetFlake.scala", "a\n", msg="a: one")
        eid, _ = self.queued(wt)
        self.assertEqual(self.check(eid), 1)
        self.assertIn("pre-push checks failed", [x for x in self.inbox() if x["kind"] == "NOT_LANDED"][0]["verdict"])

    def test_a_hook_without_the_verification_gets_the_full_round(self):
        hook = os.path.join(self.r.path, "scripts/hooks/pre-push")
        self.r.write("scripts/hooks/pre-push", open(hook).read().replace("--verify-only", "--other"))
        self.r.commit("a hook from before the verification")
        self.r.push()
        for f in (self.pushes, self.calls):
            if os.path.exists(f):
                os.remove(f)
        wt, _ = self.agent("a", "web/src/main/A.scala", "a\n", msg="a: one")
        eid, _ = self.queued(wt)
        self.moves_once(("worker/src/main/W.scala", "w\n"))
        self.assertEqual(self.check(eid), 0)
        self.assertEqual([m for m, _r in self.verified()], ["--range", "--range"])
        self.assertNotIn("without re-check", self.landed_verdict())
        self.assertEqual(self.pushed()[-1], "push remote=")

    def deleted_meanwhile(self, mine_path, mine_text):
        self.r.write("scripts/data/venues.tsv", "a\tb\n")
        self.r.commit("venues")
        self.r.push()
        os.remove(self.pushes)
        wt, _ = self.agent("a", mine_path, mine_text, msg="a: one")
        eid, _ = self.queued(wt)
        real, n = A.run_entry_checks, []

        def moving(repo, d, e, tree, base, head, mode, cls, log_path, clock, st, own=None):
            n.append(1)
            if len(n) == 1:
                other = self.r.worktree("wt-del", branch="del")
                sh(other, "git", "rm", "-q", "scripts/data/venues.tsv")
                sh(other, "git", "commit", "-q", "-m", "venues gone")
                sh(other, "git", "push", "-q", "origin", "HEAD:main")
            return real(repo, d, e, tree, base, head, mode, cls, log_path, clock, st, own)
        A.run_entry_checks = moving
        self.addCleanup(setattr, A, "run_entry_checks", real)
        self.assertEqual(self.check(eid), 0)
        return [m for m, _r in self.verified()]

    def test_a_deleted_file_the_change_names_is_a_full_recheck(self):
        self.assertEqual(self.deleted_meanwhile("scripts/venue-tool.sh", "cut -f1 scripts/data/venues.tsv\n"),
                         ["--range", "--range"])
        self.assertNotIn("without re-check", self.landed_verdict())

    def test_a_deleted_file_the_change_does_not_name_is_only_verified(self):
        self.assertEqual(self.deleted_meanwhile("scripts/other-tool.sh", "echo hi\n"), ["--range", "--verify-only"])

    def test_main_moving_over_the_changes_file_before_its_verification_is_a_full_round(self):
        """The rule is judged again by the checker, against the FULL check's base: a verification queued on one main
        is a full round when what landed before it starts touches the change's file."""
        self.r.write("docs/notes.md", "".join(f"line {i}\n" for i in range(12)))
        self.r.commit("notes")
        self.r.push()
        for f in (self.pushes, self.calls):
            if os.path.exists(f):
                os.remove(f)
        wt, _ = self.agent("a", "docs/notes.md", "mine\n" + "".join(f"line {i}\n" for i in range(1, 12)), msg="a: one")
        eid, _ = self.queued(wt)
        d, _c, _w = A.waiting_state(self.r.path)
        self.moves_once(("worker/src/main/W.scala", "w\n"))
        A.check_entry(self.r.path, eid, *on_clock(self.clock))  # the full check, main moving under it
        A.push_change(self.r.path, d, A.ChangeRecords(A.process_store(), d).get(eid), *on_clock(self.clock))
        e = A.ChangeRecords(A.process_store(), d).get(eid)
        self.assertEqual((e["status"], e.get("verify")), ("queued", True), "no overlap yet: queued to be verified")
        other, _ = self.agent("t", "docs/notes.md", "".join(f"line {i}\n" for i in range(11)) + "theirs\n")
        sh(other, "git", "push", "-q", "origin", "HEAD:main")
        self.assertEqual(self.check(eid), 0)
        self.assertEqual([m for m, _r in self.verified()], ["--range", "--range"], "fully checked on the newest main")
        self.assertEqual(self.pushed()[-1], "push remote=", "and pushed as fully checked")

    def test_the_combined_trees_check_on_ci_runner_names_its_full_base_in_a_check_verify_branch(self):
        head = sh(self.r.path, "git", "rev-parse", "HEAD").strip()
        full = "f" * 40
        calls = self.fake_github("success", head, ref=f"check/verify-{full}-x")
        ok, out = A.remote_check(self.r.path, {"id": "x", "full_base": full}, "base", head, "--verify-only", self.clock, A.CurrentStore(A.process_store))
        self.assertTrue(ok, out)
        self.assertIn(("DELETE", f"git/refs/heads/check/verify-{full}-x"), calls)

    def test_a_gate_red_fixs_check_on_ci_runner_asks_for_the_red_fix_check_lane(self):
        """2026-11-17 10:42Z: a red fix (Fixes-Red 2564) still checking 41 min after it was queued: first in this
        queue, its precommit-check jobs then waited for `movies-check` runners with every other landing's. Its branch
        is check/red-<...>, which precommit-check.yml runs on movies-check-red-fix, the lane ci-runner serves first."""
        head = sh(self.r.path, "git", "rev-parse", "HEAD").strip()
        calls = self.fake_github("success", head, ref="check/red-x")
        ok, out = A.remote_check(self.r.path, {"id": "x"}, "base", head, "--checks-only", self.clock, A.CurrentStore(A.process_store), red=True)
        self.assertTrue(ok, out)
        self.assertIn(("DELETE", "git/refs/heads/check/red-x"), calls)
        full = "f" * 40
        calls = self.fake_github("success", head, ref=f"check/red-verify-{full}-x")
        ok, out = A.remote_check(self.r.path, {"id": "x", "full_base": full}, "base", head, "--verify-only", self.clock, A.CurrentStore(A.process_store), red=True)
        self.assertTrue(ok, out)
        self.assertIn(("DELETE", f"git/refs/heads/check/red-verify-{full}-x"), calls)

    def test_a_file_both_touch_rechecks(self):
        self.r.write("docs/notes.md", "".join(f"line {i}\n" for i in range(12)))
        self.r.commit("notes")
        self.r.push()
        os.remove(self.pushes)
        wt, _ = self.agent("a", "docs/notes.md", "mine\n" + "".join(f"line {i}\n" for i in range(1, 12)), msg="a: one")
        eid, _ = self.queued(wt)
        rounds = self.moves_once(("docs/notes.md", "".join(f"line {i}\n" for i in range(11)) + "theirs\n"))
        self.assertEqual(self.check(eid), 0)
        self.assertEqual(len(rounds), 2, "checked again on the moved main")
        self.assertEqual(self.remote_log()[0], "a: one")
        self.assertNotIn("without re-check", self.landed_verdict())
        self.assertEqual(self.pushed()[-1], "push remote=", "the push's hook skips nothing")

    def rechecked_when_landing(self, path, text):
        wt, _ = self.agent("a", "docs/A.md", "a\n", msg="a: one")
        eid, _ = self.queued(wt)
        rounds = self.moves_once((path, text))
        self.assertEqual(self.check(eid), 0)
        self.assertEqual(len(rounds), 2, f"{path} landed meanwhile: checked again")
        self.assertEqual(self.remote_log()[0], "a: one")
        self.assertNotIn("without re-check", self.landed_verdict())

    def test_a_change_to_build_sbt_rechecks(self):
        """No build.sbt at the full check's tip: the build's view cannot be compared, so a full round."""
        self.rechecked_when_landing("build.sbt", "lazy val x = 1\n")

    BUILD = ('lazy val common = (project in file("common"))\n'
             'lazy val web = (project in file("web")).dependsOn(common)\n'
             'lazy val worker = (project in file("worker")).dependsOn(common).settings(Test / fork := true)\n')

    def build_moves_under(self, path, old, new):
        """A change to path queued on a main with a build; main moves by replacing old with new in build.sbt meanwhile."""
        self.r.write("build.sbt", self.BUILD)
        self.r.commit("a build")
        self.r.push()
        for f in (self.pushes, self.calls):
            if os.path.exists(f):
                os.remove(f)
        wt, _ = self.agent("a", path, "a\n", msg="a: one")
        eid, _ = self.queued(wt)
        self.moves_once(("build.sbt", self.BUILD.replace(old, new)))
        self.assertEqual(self.check(eid), 0)
        return [m for m, _r in self.verified()]

    def test_a_build_change_the_changes_modules_never_see_gets_the_verify_round(self):
        """BUILD-AWARE (the owner, 2026-11-17: 6 full rounds of ~22 min that day for build churn): worker's settings
        are nothing web builds with."""
        self.assertEqual(self.build_moves_under("web/src/main/A.scala", "Test / fork := true", "Test / fork := false"),
                         ["--range", "--verify-only"])
        self.assertIn("the combined tree verified", self.landed_verdict())

    def test_a_build_change_to_the_changes_module_rechecks(self):
        self.assertEqual(self.build_moves_under("worker/src/main/W.scala", "Test / fork := true", "Test / fork := false"),
                         ["--range", "--range"])

    def test_a_build_change_a_changed_script_reads_by_name_rechecks(self):
        """A script reading build.sbt (the hook's worker_rows) sees what no module view does: a full round."""
        self.r.write("build.sbt", self.BUILD)
        self.r.commit("a build")
        self.r.push()
        for f in (self.pushes, self.calls):
            if os.path.exists(f):
                os.remove(f)
        wt, _ = self.agent("a", "scripts/rows.sh", "grep HeavyRows build.sbt\n", msg="a: one")
        eid, _ = self.queued(wt)
        self.moves_once(("build.sbt", self.BUILD.replace("Test / fork := true", "Test / fork := false")))
        self.assertEqual(self.check(eid), 0)
        self.assertEqual([m for m, _r in self.verified()], ["--range", "--range"])

    READER_NAMES = 'object ReaderNames {\n  def settings = Seq()\n  val names = "repochecks"\n}\n'

    def reader_names_move_under(self, *paths, build=None):
        """2026-11-21 08:42Z, shared-followups-7be7dc: a change to the mobile apps only, queued on a main whose worker
        builds with project/ReaderNames.scala; main moves ReaderNames (21 commits) during its checks. Its `sbt` plan
        selects local-tests.txt's basic lines, worker's among them, so worker's view of the build moved: two FULL rounds
        (13.5 and 16 min) for a change no check of which reads the build."""
        self.r.write("build.sbt", build or self.BUILD.replace("settings(Test / fork := true)",
                                                              "settings(ReaderNames.settings)"))
        self.r.write("project/ReaderNames.scala", self.READER_NAMES)
        self.r.write("scripts/hooks/local-tests.txt", "basic sbt worker/testOnly deploy.*\n")
        self.r.commit("a build whose worker reads ReaderNames")
        self.r.push()
        for f in (self.pushes, self.calls):
            if os.path.exists(f):
                os.remove(f)
        wt, _ = self.agent("a", paths[0], "a\n", msg="a: one")
        for p in paths[1:]:
            self.r.write(p, "a\n", at=wt)
        if paths[1:]:
            self.r.commit("a: two", at=wt)
        eid, _ = self.queued(wt)
        self.moves_once(("project/ReaderNames.scala", self.READER_NAMES.replace("Seq()", "Seq(Test / fork := true)")))
        self.assertEqual(self.check(eid), 0)
        return [m for m, _r in self.verified()]

    def test_a_build_change_under_a_change_out_of_the_builds_reach_gets_the_verify_round(self):
        """OUT OF THE BUILD'S REACH (queuerecheck.py; the owner, 2026-11-21): no path of the change is one the build compiles
        or reads, so only the checks reading its files can see the new build -- the verify round's."""
        self.assertEqual(self.reader_names_move_under("android/app/src/main/java/Auth.kt", "ios/App/List.swift"),
                         ["--range", "--verify-only"])
        self.assertIn("the combined tree verified", self.landed_verdict())

    def test_a_build_change_under_a_change_the_build_names_rechecks(self):
        """A file of the change the build names (a resource a setting reads) is in its reach: the full round."""
        build = self.BUILD.replace("settings(Test / fork := true)", "settings(ReaderNames.settings)") \
            + 'lazy val inputs = file("infra/image-inputs.txt")\n'
        self.assertEqual(self.reader_names_move_under("infra/image-inputs.txt", build=build), ["--range", "--range"])

    def test_a_build_change_under_a_change_to_shared_resources_rechecks(self):
        self.assertEqual(self.reader_names_move_under("test/resources/fixtures/a.tsv"), ["--range", "--range"])

    def test_a_build_change_to_an_upstream_module_rechecks(self):
        self.assertEqual(self.build_moves_under("web/src/main/A.scala", 'file("common"))',
                                                'file("common")).settings(Test / fork := true)'),
                         ["--range", "--range"])

    def test_a_hook_change_that_moves_this_changes_selection_rechecks(self):
        """SELECTION-AWARE (the owner, 2026-11-17): the hook's plan for the change's paths differs at the old and the new
        hook (a check added): a full round."""
        hook = open(os.path.join(self.r.path, "scripts/hooks/pre-push")).read()
        self.rechecked_when_landing("scripts/hooks/pre-push", hook.replace("'sbt\\nidentity\\n'", "'sbt\\nidentity\\npage\\n'"))
        self.assertEqual([m for m, _r in self.verified()], ["--range", "--range"], "a full round, not a verification")

    def test_a_hook_change_that_keeps_this_changes_selection_gets_the_verify_round(self):
        """17 full rechecks of ~40 min in a day, nearly all for a hook or registry change selecting nothing new for the
        change (2026-11-16..17): the same plan, compile plan, unit plan and lint plan at both hooks is a verify round."""
        wt, _ = self.agent("a", "docs/A.md", "a\n", msg="a: one")
        eid, _ = self.queued(wt)
        hook = open(os.path.join(self.r.path, "scripts/hooks/pre-push")).read()
        self.moves_once(("scripts/hooks/pre-push", hook + "# a change to the hook\n"))
        self.assertEqual(self.check(eid), 0)
        self.assertEqual([m for m, _r in self.verified()], ["--range", "--verify-only"])
        self.assertIn("the combined tree verified", self.landed_verdict())

    def test_a_lint_registry_row_this_change_selects_gets_the_verify_round_which_runs_it(self):
        """The verify round runs the new registry's lint plan over full_base..tip, a superset of the change's paths: the
        added spec runs there, no full round needed."""
        wt, _ = self.agent("a", "docs/A.md", "a\n", msg="a: one")
        eid, _ = self.queued(wt)
        self.moves_once(("scripts/hooks/lint-registry.tsv", "common:tools.NewLintSpec\t*\n"))
        self.assertEqual(self.check(eid), 0)
        self.assertEqual([m for m, _r in self.verified()], ["--range", "--verify-only"])

    def test_a_lint_registry_change_selecting_nothing_new_gets_the_verify_round(self):
        wt, _ = self.agent("a", "docs/A.md", "a\n", msg="a: one")
        eid, _ = self.queued(wt)
        self.moves_once(("scripts/hooks/lint-registry.tsv", "# a comment row\n"))
        self.assertEqual(self.check(eid), 0)
        self.assertEqual([m for m, _r in self.verified()], ["--range", "--verify-only"])

    def test_a_selection_path_beside_another_forcing_path_still_rechecks(self):
        """Only the hook, its registry and local-tests.txt are judged by selection; build.sbt stays a full round (for a
        change the build reaches: OUT OF THE BUILD'S REACH)."""
        wt, _ = self.agent("a", "web/src/main/A.scala", "a\n", msg="a: one")
        eid, _ = self.queued(wt)
        self.moves_once(("scripts/hooks/lint-registry.tsv", "# a comment row\n"), ("build.sbt", "lazy val x = 1\n"))
        self.assertEqual(self.check(eid), 0)
        self.assertEqual([m for m, _r in self.verified()], ["--range", "--range"])

    def test_a_local_tests_change_to_a_selected_area_rechecks(self):
        """--plan names the `identity` area; what it runs is local-tests.txt's identity lines: changed, a full round."""
        self.rechecked_when_landing("scripts/hooks/local-tests.txt", "identity sbt identity/test\n")
        self.assertEqual([m for m, _r in self.verified()], ["--range", "--range"])

    SHARE = ('#!/bin/sh\n# verifyareas-<full>-<id>: the combined tree verified with the area tests\n'
             '[ "$1" = checks ] || exit 0\n'
             'case "$2" in plan) echo "sbt identity" ;; rest) echo sbt ;; identity) echo identity ;; *) echo ;; esac\n')

    def with_share(self):
        """The repo's scripts/ci/precommit-share.sh (a fake answering `checks <share>`), on main before the change."""
        self.r.write("scripts/ci/precommit-share.sh", self.SHARE)
        os.chmod(os.path.join(self.r.path, "scripts/ci/precommit-share.sh"), 0o755)
        self.r.commit("the check's shares")
        self.r.push()
        for f in (self.pushes, self.calls):
            if os.path.exists(f):
                os.remove(f)

    def share_moves_under(self, text):
        self.with_share()
        wt, _ = self.agent("a", "docs/A.md", "a\n", msg="a: one")
        eid, _ = self.queued(wt)
        rounds = self.moves_once(("scripts/ci/precommit-share.sh", text))
        self.assertEqual(self.check(eid), 0)
        self.assertEqual(len(rounds), 2)
        return [m for m, _r in self.verified()]

    def test_a_share_change_selecting_nothing_new_gets_the_verify_round(self):
        """The owner, 2026-11-18 (item 6): precommit-share.sh landing meanwhile was a full round whatever it changed
        (2026-11-17: 5 full rounds, flux-receiver 79 min); now SELECTION-AWARE like the hook: what its shares run for
        the change, at the full check's tip and the rebased tip -- nothing added, the verify round."""
        self.assertEqual(self.share_moves_under(self.SHARE + "# a comment\n"), ["--range", "--verify-only"])
        self.assertIn("the combined tree verified", self.landed_verdict())

    def test_a_share_change_adding_a_check_for_the_change_rechecks(self):
        self.assertEqual(self.share_moves_under(self.SHARE.replace('echo "sbt identity"', 'echo "sbt identity itrest"')),
                         ["--range", "--range"])
        self.assertNotIn("the combined tree verified", self.landed_verdict())

    def test_the_share_selection_asks_only_the_shares_the_check_has(self):
        """2026-11-21: the landing check's `oldrefs` share went (the old-ref compile runs on Main, after landing) and
        precommit-share.sh refuses a share it does not know, as it refuses any (exit 2): asking it made every share
        selection unreadable, and every rebase a full round."""
        strict = self.SHARE.replace("*) echo ;;", "*) exit 2 ;;")
        self.SHARE = strict
        self.assertEqual(self.share_moves_under(strict + "# a comment\n"), ["--range", "--verify-only"])

    def test_an_unreadable_share_selection_rechecks(self):
        self.assertEqual(self.share_moves_under("#!/bin/sh\n# verifyareas-\nexit 3\n"), ["--range", "--range"])

    def test_a_module_both_sides_touch_verifies_with_the_area_tests(self):
        """The owner, 2026-11-18 (item 6): module overlap is the verify round plus the overlapping modules' unit and
        AREA tests; the tree's precommit-share.sh runs the area share of a check/verifyareas- branch."""
        self.with_share()
        wt, _ = self.agent("a", "worker/src/test/scala/ASpec.scala", "a\n", msg="a: one")
        eid, _ = self.queued(wt)
        seen = []
        real = A.run_entry_checks

        def recording(repo, d, e, tree, base, head, mode, cls, log_path, clock, st, own=None):
            seen.append((mode, e.get("verify_areas")))
            return real(repo, d, e, tree, base, head, mode, cls, log_path, clock, st, own)
        A.run_entry_checks = recording
        self.addCleanup(setattr, A, "run_entry_checks", real)
        self.moves_once(("worker/src/test/scala/BSpec.scala", "b\n"), ("docs/n.md", "n\n"))
        self.assertEqual(self.check(eid), 0)
        self.assertEqual(seen, [("--range", None), ("--verify-only", ["worker"])])
        self.assertIn("worker", self.landed_verdict())
        self.assertFalse(self.entries()[eid.rsplit("-", 1)[-1]].get("verify_areas"), "cleared after its round")

    def test_disjoint_modules_verify_without_the_area_tests(self):
        self.with_share()
        wt, _ = self.agent("a", "web/src/main/A.scala", "a\n", msg="a: one")
        eid, _ = self.queued(wt)
        seen = []
        real = A.run_entry_checks

        def recording(repo, d, e, tree, base, head, mode, cls, log_path, clock, st, own=None):
            seen.append((mode, e.get("verify_areas")))
            return real(repo, d, e, tree, base, head, mode, cls, log_path, clock, st, own)
        A.run_entry_checks = recording
        self.addCleanup(setattr, A, "run_entry_checks", real)
        self.moves_once(("worker/src/main/W.scala", "w\n"))
        self.assertEqual(self.check(eid), 0)
        self.assertEqual(seen, [("--range", None), ("--verify-only", None)])

    def test_a_share_without_the_area_verification_verifies_a_module_overlap_as_before(self):
        self.SHARE = self.SHARE.replace("verifyareas", "verify")
        self.with_share()
        wt, _ = self.agent("a", "worker/src/test/scala/ASpec.scala", "a\n", msg="a: one")
        eid, _ = self.queued(wt)
        self.moves_once(("worker/src/test/scala/BSpec.scala", "b\n"))
        self.assertEqual(self.check(eid), 0)
        self.assertEqual([m for m, _r in self.verified()], ["--range", "--verify-only"])
        self.assertNotIn("area tests", self.landed_verdict())

    def test_the_area_verification_on_ci_runner_is_a_check_verifyareas_branch(self):
        head = sh(self.r.path, "git", "rev-parse", "HEAD").strip()
        full = "f" * 40
        calls = self.fake_github("success", head, ref=f"check/verifyareas-{full}-x")
        ok, out = A.remote_check(self.r.path, {"id": "x", "full_base": full, "verify_areas": ["worker"]}, "base", head,
                                 "--verify-only", self.clock, A.CurrentStore(A.process_store))
        self.assertTrue(ok, out)
        self.assertIn(("DELETE", f"git/refs/heads/check/verifyareas-{full}-x"), calls)

    def test_without_the_full_checks_tip_a_hook_change_rechecks(self):
        """An entry fully checked before full_tip was recorded: its old selection cannot be read, so a full round."""
        wt, _ = self.agent("a", "docs/A.md", "a\n", msg="a: one")
        eid, _ = self.queued(wt)
        real = A.set_change

        def forgetting(st, d, e, status, **kw):
            kw.pop("full_tip", None)
            return real(st, d, e, status, **kw)
        A.set_change = forgetting
        self.addCleanup(setattr, A, "set_change", real)
        hook = open(os.path.join(self.r.path, "scripts/hooks/pre-push")).read()
        self.moves_once(("scripts/hooks/pre-push", hook + "# a change to the hook\n"))
        self.assertEqual(self.check(eid), 0)
        self.assertEqual([m for m, _r in self.verified()], ["--range", "--range"])

    def test_only_the_hooks_own_self_tests_landing_meanwhile_get_the_verify_round(self):
        """The owner, 2026-11-16: the hook's self-tests never steer the landing check, so they force no full round."""
        wt, _ = self.agent("a", "docs/A.md", "a\n", msg="a: one")
        eid, _ = self.queued(wt)
        rounds = self.moves_once(("scripts/hooks/pre-push-test.sh", "# t\n"),
                                 ("scripts/hooks/pre-push-test-lib.sh", "# l\n"),
                                 ("scripts/hooks/lint-registry-test.sh", "# r\n"))
        self.assertEqual(self.check(eid), 0)
        self.assertEqual(len(rounds), 2)
        self.assertEqual([m for m, _r in self.verified()], ["--range", "--verify-only"])
        self.assertIn("no file overlap, the combined tree verified", self.landed_verdict())

    def test_a_self_test_landing_beside_a_hook_change_still_rechecks(self):
        wt, _ = self.agent("a", "docs/A.md", "a\n", msg="a: one")
        eid, _ = self.queued(wt)
        self.moves_once(("scripts/hooks/pre-commit-test.sh", "# t\n"), ("scripts/hooks/local-tests.txt", "basic x\n"))
        self.assertEqual(self.check(eid), 0)
        self.assertEqual([m for m, _r in self.verified()], ["--range", "--range"])
        self.assertNotIn("without re-check", self.landed_verdict())

    def test_which_paths_force_a_full_round(self):
        full = ["scripts/hooks/pre-push", "scripts/hooks/pre-commit", "scripts/hooks/local-tests.txt",
                "scripts/hooks/lint-registry.tsv", "scripts/hooks/lint-registry-missing", "build.sbt",
                "project/plugins.sbt", "scripts/hooks-path.sh", "scripts/tool-conventions.sh",
                "scripts/ci/precommit-share.sh"]
        not_full = ["scripts/hooks/pre-push-test.sh", "scripts/hooks/pre-push-test-lib.sh",
                    "scripts/hooks/pre-push-gate-test.sh", "scripts/hooks/pre-commit-test.sh",
                    "scripts/hooks/lint-registry-test.sh", "scripts/hooks/plan_test.py",
                    "scripts/ci/precommit-share-test.sh", "worker/src/main/W.scala"]
        self.assertEqual([p for p in full if not A.forces_full_recheck(p)], [])
        self.assertEqual([p for p in not_full if A.forces_full_recheck(p)], [])

    def test_a_conflict_with_the_moved_main_is_not_landed(self):
        wt, _ = self.agent("a", "web/src/main/Shared.scala", "mine\n")
        eid, _ = self.queued(wt)
        other, _ = self.agent("b", "web/src/main/Shared.scala", "theirs\n")
        sh(other, "git", "push", "-q", "origin", "HEAD:main")
        self.assertEqual(self.check(eid), 1)
        self.assertIn("no longer applies", [e for e in self.inbox() if e["kind"] == "NOT_LANDED"][0]["verdict"])

    def test_the_checks_run_on_the_ci_tier_and_the_push_names_the_run(self):
        self.r.write(".github/workflows/precommit-check.yml", "name: Pre-commit check\n")
        self.r.commit("the remote check workflow")
        self.r.push()
        os.remove(self.pushes)
        wt, _ = self.agent("a", "web/src/main/A.scala", "a\n")
        eid, _ = self.queued(wt)
        seen = []

        def remote(repo, e, base, head, mode, clock, st, own=None, red=False):
            seen.append((base, head, mode))
            e["remote_run"] = 4242
            return True, "precommit-check.yml run 4242 (#77): success"
        A.remote_check = remote
        self.assertEqual(self.check(eid), 0)
        self.assertEqual(len(seen), 1)
        self.assertEqual(self.calls_of("--range"), [], "nothing checked on the laptop")
        self.assertEqual(self.pushed(), ["push remote=4242"])
        self.assertIn("on ci-runner (the CI tier", [e for e in self.inbox() if e["kind"] == "LANDED"][0]["verdict"])

    def test_on_the_laptop_a_remote_check_without_a_verdict_is_checked_locally(self):
        self.r.write(".github/workflows/precommit-check.yml", "name: Pre-commit check\n")
        self.r.commit("the remote check workflow")
        self.r.push()
        os.remove(self.pushes)
        wt, _ = self.agent("a", "web/src/main/A.scala", "a\n")
        eid, _ = self.queued(wt)
        A.remote_check = lambda repo, e, base, head, mode, clock, st, own=None, red=False: (None, "no run started")
        saved = A.checks_here
        A.checks_here = lambda: True  # the laptop: its own tools
        self.addCleanup(setattr, A, "checks_here", saved)
        self.assertEqual(self.check(eid), 0)
        self.assertEqual(len(self.calls_of("--range")), 1)
        self.assertEqual(self.pushed(), ["push remote="])

    def test_on_ci_runner_a_remote_check_without_a_verdict_is_never_checked_in_a_local_tree(self):
        """ci-runner's check trees have none of the hook's tools: no verdict there is re-queued, never run locally."""
        self.r.write(".github/workflows/precommit-check.yml", "name: Pre-commit check\n")
        self.r.commit("the remote check workflow")
        self.r.push()
        os.remove(self.pushes)
        wt, _ = self.agent("a", "web/src/main/A.scala", "a\n")
        eid, _ = self.queued(wt)
        A.remote_check = lambda repo, e, base, head, mode, clock, st, own=None, red=False: (None, "push of check/x failed: remote: boom")
        self.check(eid, rounds=1)  # the tests run as ci-runner (AGENTCTL_ROLE=server)
        self.assertEqual(self.calls_of("--range"), [], "nothing checked in a ci-runner tree")
        self.assertEqual(self.pushed(), [])
        d, _c, _w = A.waiting_state(self.r.path)
        e = A.ChangeRecords(A.process_store(), d).get(eid)
        # what stalled it is kept (2026-11-17: two 13-s "stalls" whose cause no record named)
        self.assertEqual(e["why"], "infra: its checks stalled (on ci-runner (no verdict): push of check/x failed: "
                                   "remote: boom)")
        self.assertEqual(e["infra_reason"], "its checks stalled")
        # ITS PLACE KEPT (2026-11-17, ref-srp-54ebae: a 13-s stall's 5-min backoff let younger changes take every check
        # tree, and it queued another 15+ min): the stalled change is due at once, first by age for the freed tree
        self.assertLessEqual(e.get("retry_after", 0), self.clock.time())
        for _ in range(A.STALL_KEEP_PLACE):  # stalling on and on: backed off like any infra cause
            self.check(eid, rounds=1)
        e = A.ChangeRecords(A.process_store(), d).get(eid)
        self.assertGreater(e["retry_after"], self.clock.time() + 60)

    def fake_github(self, status, head, ref="check/e1"):
        calls = []

        def api(o_r, method, path, params=None, body=None):
            calls.append((method, path))
            if path.startswith("actions/workflows/") and path.endswith("/runs"):
                return {"workflow_runs": [{"id": 99, "head_sha": head, "head_branch": ref}]}
            if path == "actions/runs/99":
                return {"id": 99, "run_number": 7, "status": "completed", "conclusion": status,
                        "html_url": "http://f/runs/7"}
            return None
        saved = (A.api, A.fetch_failed_log, A.slug)
        A.api, A.slug = api, (lambda repo: "o/movies")
        A.fetch_failed_log = lambda o_r, rid: "check\trun\t[error] BROKEN is in the tree\n"
        self.addCleanup(lambda: setattr(A, "api", saved[0]) or setattr(A, "fetch_failed_log", saved[1])
                        or setattr(A, "slug", saved[2]))
        return calls

    def test_remote_check_pushes_a_check_branch_reads_the_verdict_and_deletes_the_branch(self):
        head = sh(self.r.path, "git", "rev-parse", "HEAD").strip()
        calls = self.fake_github("success", head)
        ok, out = A.remote_check(self.r.path, {"id": "e1"}, "base", head, "--checks-only", self.clock, A.CurrentStore(A.process_store))
        self.assertTrue(ok, out)
        self.assertIn("check/e1", sh(self.r.root, "git", "--git-dir", self.r.bare, "branch", "--list", "check/*"),
                      "pushed for the run (deleted through the API, faked here)")
        self.assertIn(("DELETE", "git/refs/heads/check/e1"), calls)
        calls = self.fake_github("failure", head)
        ok, out = A.remote_check(self.r.path, {"id": "e1"}, "base", head, "--checks-only", self.clock, A.CurrentStore(A.process_store))
        self.assertFalse(ok)
        self.assertIn("[error] BROKEN is in the tree", out)
        self.assertIn(("DELETE", "git/refs/heads/check/e1"), calls)

    def test_a_fix_that_must_still_wait_is_blocked_red_and_holds_no_solo_change(self):
        """2026-10-31 06:44-07:50Z: Main #2877 red; the only waiting change named it (Fixes-Red: 2877 on one commit) but
        carried a production commit with no trailer, so it must wait for green: the assembling batch built nothing,
        said nothing, and its deadline set it assembling again every 5 min -- while three checked changes that could land
        on red waited behind it ("its push waits for batch #5 (assembling)"). A batch with nothing that may land on red
        is blocked-red (it holds no solo change), and stays so until the gate or its changes let it build."""
        def entry(eid):
            return A.ChangeRecords(A.process_store(), d).get(eid)
        d, _c, _w = A.waiting_state(self.r.path)
        self.reds = {"7"}
        wa, _ = self.agent("a", "web/src/main/A.scala", "a\n", msg="a: the fix\n\nFixes-Red: 7")
        self.r.write("web/src/main/A2.scala", "a2\n", at=wa)
        self.r.commit("a: beside it, a production change naming no red", at=wa)
        ea, _ = self.queued(wa)
        self.check(ea)
        self.assertEqual(entry(ea)["fixes"], ["7"], "the entry names the red")
        for _ in range(2):  # the second tick: it stays blocked, never flips back to assembling
            self.drive()  # (THE BATCH IS THE MECHANISM: its red pre-check runs, then it is blocked again)
            self.assertEqual(self.batch()["state"], "blocked-red", "nothing in it may land on red")
        self.assertIsNone(A.batch_goes_first(A.process_store(), d))
        wb, _ = self.agent("b", "scripts/b.sh", "echo b\n", msg="b: tools only")
        eb, _ = self.queued(wb)
        self.assertEqual(self.check(eb), 0)
        self.assertEqual(entry(eb)["status"], "landed", "a change that may land on red is not held by the batch")
        self.assertEqual(self.remote_log()[0], "b: tools only")
        self.reds = set()  # green: the batch builds
        self.drive()
        self.assertEqual(entry(ea)["status"], "landed")

    def test_a_fix_naming_an_older_run_of_its_gate_red_lands_alone_never_waiting_with_a_non_fix(self):
        """2026-11-25, batch #124: Main #2898 red; the fix named #2896, an older run of the SAME red (the red item's
        names, kind gate). The gate's own set (gate_reds: the newest runs) lacked 2896, so must_wait kept the fix on
        `waiting` -- beside a non-fix production change -- while the classifier already called it `red fix of 2896
        (gate)`: the batch, assembling only what may land on red, found nothing and the fix waited for the green only it
        could bring. A fix of an open gate red names it by any of the red's names; the batch splits and the fix lands
        alone, the non-fix change staying behind."""
        def entry(eid):
            return A.ChangeRecords(A.process_store(), d).get(eid)
        d, _c, _w = A.waiting_state(self.r.path)
        self.reds = {"8", "98000"}  # the gate reads Main #8; the red item below also names the older run 7
        wb, _ = self.agent("b", "web/src/main/B.scala", "b\n", msg="b: a production change naming no red")
        eb, _ = self.queued(wb)
        self.check(eb)
        wa, _ = self.agent("a", "web/src/main/A.scala", "a\n", msg="a: the fix\n\nFixes-Red: 7\nFix-Replay: none \u2014 a fixture with no command that replays it")
        ea, _ = self.queued(wa)
        self.check(ea)
        self.assertEqual(entry(ea)["status"], "waiting", "before the red is open in the store, nothing says it fixes one")
        A.process_store().put_item("red", "k7", "leased", {"key": "k7", "workflow": "Main", "gate": True,
                                                           "runs": ["7", "8"], "last_run": {"number": 8}})
        self.assertFalse(A.must_wait(self.r.path, entry(ea)["commits"], self.reds, A.process_store()))
        self.assertTrue(A.must_wait(self.r.path, entry(eb)["commits"], self.reds, A.process_store()))
        did = self.drive()
        self.check(ea)  # the heal sent it back to the queue (release_red_fixes): its own check and push, first and alone
        self.assertEqual(entry(ea)["status"], "landed", f"the fix goes ahead alone: {did} {self.batch()} {entry(ea).get('why')}")
        self.assertEqual(entry(eb)["status"], "waiting", "the non-fix change waits for green")
        self.assertEqual(self.remote_log()[0], "a: the fix\n\nFixes-Red: 7"[:len("a: the fix")])

    def test_wr_heals_a_waiting_fix_the_open_red_names_and_says_what_holds_one_it_cannot_release(self):
        """2026-11-25, batch #124: a fix sat `waiting` for the green of the red it fixed; WR said so and nobody acted.
        Both fixes below joined `waiting` before any red item named them. Now the open gate red names #7 (its
        episode): the tick sends that fix back to the queue while the gate is still red, first and alone. The other
        names a red OUTSIDE the gate -- the rule still holds it, and says which rule on its item."""
        def entry(eid):
            return A.ChangeRecords(A.process_store(), d).get(eid)
        d, _c, _w = A.waiting_state(self.r.path)
        self.reds = {"8", "98000"}
        fixes = {}
        for name, run in (("gate", "7"), ("out", "6")):
            wt, _ = self.agent(name, f"web/src/main/{name}.scala", "x\n", msg=f"{name}: a fix\n\nFixes-Red: {run}")
            fixes[name], _ = self.queued(wt)
            self.check(fixes[name])
            self.assertEqual(entry(fixes[name])["status"], "waiting", "no red item names it yet")
        st = A.process_store()
        st.put_item("red", "k7", "leased", {"key": "k7", "workflow": "Main", "gate": True, "runs": ["7", "8"],
                                            "last_run": {"number": 8}})
        st.put_item("red", "k6", "leased", {"key": "k6", "workflow": "Alert", "gate": False, "red_kind": "outside",
                                            "runs": ["6"]})
        A.waiting_after_release(self.r.path, d, st, self.clock.time(), None, self.reds)
        self.assertEqual(entry(fixes["gate"])["status"], "queued", "the open red names it: back to the queue, red or not")
        self.assertEqual(entry(fixes["out"])["status"], "waiting", "no gate red names it: the rule holds it")
        why = (st.item("change", fixes["out"])["data"] or {}).get("wait_rule") or ""
        self.assertIn("names 6, no run of the open gate red", why)

    def test_the_batchs_check_on_ci_runner_is_the_fast_only_set_on_a_check_fast_branch(self):
        head = sh(self.r.path, "git", "rev-parse", "HEAD").strip()
        calls = self.fake_github("success", head, ref="check/fast-batch-x")
        ok, out = A.remote_check(self.r.path, {"id": "batch-x"}, "base", head, "--fast-only", self.clock, A.CurrentStore(A.process_store))
        self.assertTrue(ok, out)
        self.assertIn(("DELETE", "git/refs/heads/check/fast-batch-x"), calls)  # the branch names the mode for the workflow
        seen, saved = [], (A.remote_check, A.remote_checks_supported, A.precheck_hook, A.batch_mode)
        A.remote_check = lambda repo, e, base, tip, mode, clock, st, own=None, red=False: seen.append((e["id"], mode)) or (True, "ok")
        A.remote_checks_supported = lambda tree: True
        A.precheck_hook = lambda tree: "/hook"
        A.batch_mode = lambda hook: "--fast-only"
        try:  # the server role (the harness's): its batch check goes to the CI tier
            self.assertEqual(A.batch_check(self.r.path, self.r.path, self.r.path, "b" * 40, "c" * 40, self.clock, A.CurrentStore(A.process_store)), (True, "ok", False))
        finally:
            A.remote_check, A.remote_checks_supported, A.precheck_hook, A.batch_mode = saved
        self.assertEqual(seen, [("batch-cccccccccc", "--fast-only")])

    def test_a_red_batchs_check_on_ci_runner_takes_the_red_fix_lane(self):
        """Review 2026-11-18: a batch carrying a gate red's fix (cls red: a fix whose production commit names no red waits
        in it) was checked on check/fast-batch-<tip>, the ordinary lane: behind every queued check on saturated
        runners, where a lone fix's check/red-<...> is served first."""
        seen, saved = [], (A.remote_check, A.remote_checks_supported, A.precheck_hook, A.batch_mode)
        self.addCleanup(lambda: [setattr(A, n, v) for n, v in zip(
            ("remote_check", "remote_checks_supported", "precheck_hook", "batch_mode"), saved)])
        A.remote_check = lambda repo, e, base, tip, mode, clock, st, own=None, red=False: seen.append((e["id"], red)) or (True, "ok")
        A.remote_checks_supported = lambda tree: True
        A.precheck_hook = lambda tree: "/hook"
        A.batch_mode = lambda hook: "--fast-only"
        A.batch_check(self.r.path, self.r.path, self.r.path, "b" * 40, "c" * 40, self.clock, A.CurrentStore(A.process_store), cls="red")
        A.batch_check(self.r.path, self.r.path, self.r.path, "b" * 40, "d" * 40, self.clock, A.CurrentStore(A.process_store))
        self.assertEqual(seen, [("batch-cccccccccc", True), ("batch-dddddddddd", False)])
        lanes, real = [], A.batch_check
        self.addCleanup(setattr, A, "batch_check", real)
        A.batch_check = lambda *a, cls="check", **k: lanes.append(cls) or (False, "boom", False)
        A.bisect(self.r.path, self.r.path, [{"id": "a", "tip": "a" * 40}, {"id": "b", "tip": "b" * 40}], "c" * 40,
                 self.r.path, self.clock, A.CurrentStore(A.process_store), cls="red")
        self.assertEqual(set(lanes), {"red"}, "and so is each check of its bisect")

    def test_a_check_the_laptop_failed_is_queued_again_not_rejected(self):
        # 2026-11-23 ('queued', 2) under load: check() read the infra backoff the executor stamped on this test's clock
        # against the wall's time, and a test 5+ min into a loaded run checked the change again at once
        self.clock = lagging_clock()
        wt, _ = self.agent("a", "web/src/main/A.scala", "a\n")
        eid, _ = self.queued(wt)
        real = A.run_entry_checks
        A.run_entry_checks = lambda *a, **k: (False, "sbt server did not start within 300 seconds\n[error] failed to "
                                                      "connect to server", "locally")
        self.addCleanup(setattr, A, "run_entry_checks", real)
        self.assertEqual(self.check(eid), 0)
        e = self.entries()[eid.rsplit("-", 1)[-1]]
        self.assertEqual((e["status"], e["infra_retries"]), ("queued", 1))
        self.assertGreater(e["retry_after"], self.clock.time())
        self.assertEqual([x for x in self.inbox() if x["kind"] == "NOT_LANDED"], [])
        d, _c, _w = A.waiting_state(self.r.path)
        for _ in range(A.QUEUE_INFRA_RETRIES):
            e = A.ChangeRecords(A.process_store(), d).get(eid)
            e["retry_after"] = 0
            put_change(A.CurrentStore(A.process_store), d, e)
            self.check(eid, rounds=1)
        e = self.entries()[eid.rsplit("-", 1)[-1]]
        self.assertEqual((e["status"], e["infra_retries"]), ("queued", 1 + A.QUEUE_INFRA_RETRIES),
                         "infra never rejects a change (design §5.1)")
        evs = [x for x in A.process_store().events() if x["item_id"] == eid and x["severity"] == "escalate"]
        self.assertTrue(evs, "from the third infra re-queue on, each one escalates")

    def test_a_check_whose_environment_lacked_the_gitops_manifests_is_an_infra_retry_not_a_rejection(self):
        """2026-11-22, landing checks 44984605717 / 44998261508: Identity takes #349's fix was REJECTED twice because
        the check's own copy of the GitOps manifests (another repository's, fetched by the check) was missing. That is
        the check's environment: an infra retry of the check (never a rerun-to-green of a test), queued again."""
        wt, _ = self.agent("a", "web/src/main/A.scala", "a\n")
        eid, _ = self.queued(wt)
        real = A.run_entry_checks
        A.run_entry_checks = lambda *a, **k: (False, "- should hold G1's heap-sizing ergonomics *** FAILED ***\n"
                                                      "  java.lang.AssertionError: infra/kubernetes/web/base/all.yaml is "
                                                      "missing because the GitOps manifests are no longer in this "
                                                      "repository.\n", "on ci-runner")
        self.addCleanup(setattr, A, "run_entry_checks", real)
        self.assertEqual(self.check(eid), 0)
        e = self.entries()[eid.rsplit("-", 1)[-1]]
        self.assertEqual((e["status"], e["infra_retries"]), ("queued", 1))
        self.assertIn("GitOps manifests", e["why"])
        self.assertEqual([x for x in self.inbox() if x["kind"] == "NOT_LANDED"], [])

    def test_the_hook_is_given_the_remotes_ref_so_it_reads_its_gate(self):
        wt, _ = self.agent("a", "web/src/main/A.scala", "a\n")
        eid, _ = self.queued(wt)
        self.check(eid)
        self.assertRegex(self.calls_of("--range")[0], r"^--range origin/main\.\.[0-9a-f]{40}$")

    def test_follow_keeps_the_in_process_land(self):
        wt, sha = self.agent("a", "web/src/main/A.scala", "a\n")
        rc, out = self.land(wt, "--follow")
        self.assertIn(f"LANDED {sha}", out)
        self.assertNotIn("QUEUED", out)

    def test_cancel_drops_a_queued_change(self):
        wt, _ = self.agent("a", "web/src/main/A.scala", "a\n")
        eid, _ = self.queued(wt)
        rc, out = self.run_tool("waiting", "cancel", eid, cwd=self.r.path)
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.check(eid), 0)
        self.assertEqual(self.pushed(), [], "a cancelled change is never checked or pushed")


    def remote_refs(self):
        out = sh(self.r.path, "git", "ls-remote", "--heads", "origin", "refs/heads/land/*")
        return [l.split()[-1] for l in out.splitlines() if l.strip()]

    def test_a_queued_change_is_pushed_to_its_land_ref_and_the_ref_goes_when_it_is_decided(self):
        saved = A.land_refs_supported
        A.land_refs_supported = lambda repo: True
        self.addCleanup(setattr, A, "land_refs_supported", saved)
        wt, sha = self.agent("a", "web/src/main/A.scala", "a\n")
        eid, _out = self.queued(wt)
        self.assertEqual(self.remote_refs(), [f"refs/heads/land/{eid}"])
        self.assertEqual(self.entries()[eid.rsplit("-", 1)[-1]]["ref"], f"refs/heads/land/{eid}")
        d, _c, _w = A.waiting_state(self.r.path)
        A._last_ref_sweep[0] = 0
        self.assertEqual(A.sweep_land_refs(self.r.path, d, A.CurrentStore(A.process_store)), [], "queued: its ref stays")
        self.assertEqual(self.check(eid), 0)  # landed
        A._last_ref_sweep[0] = 0
        self.assertEqual(A.sweep_land_refs(self.r.path, d, A.CurrentStore(A.process_store)), [f"refs/heads/land/{eid}: deleted"])
        self.assertEqual(self.remote_refs(), [])

    def test_sweep_keeps_the_ref_of_a_change_only_the_store_holds_open(self):
        """Phase 2: the change lives in the store, not in the queue's records -- its land ref must survive the sweep."""
        saved = A.land_refs_supported
        A.land_refs_supported = lambda repo: True
        self.addCleanup(setattr, A, "land_refs_supported", saved)
        wt, sha = self.agent("a", "web/src/main/A.scala", "a\n")
        eid, _out = self.queued(wt)
        d, _c, _w = A.waiting_state(self.r.path)
        A.process_store().merge_item("change", eid, {"record": None})  # held by the store alone: no queue record
        A._last_ref_sweep[0] = 0
        self.assertEqual(A.sweep_land_refs(self.r.path, d, A.CurrentStore(A.process_store)), [], "open in the store: its ref stays")
        self.assertEqual(self.remote_refs(), [f"refs/heads/land/{eid}"])
        # unknown to both: kept while young, deleted (and logged) past 24 h. ONE frozen clock for the sweep and the
        # expected age (2026-11-26, Red #273: the expectation read the wall, the sweep a clock 400 s behind it: an hour apart at a boundary)
        from agentctl import clock as clock_module
        self.addCleanup(clock_module.wire_clock, clock_module._PROCESS_CLOCK[0])
        clock_module.wire_clock(FakeClock(time.time()))
        sh(self.r.path, "git", "push", "--quiet", "origin", f"{sha}:refs/heads/land/20200101-000000-x-aaaaaa",
           f"{sha}:refs/heads/land/{time.strftime('%Y%m%d-%H%M%S')}-y-bbbbbb")
        A._last_ref_sweep[0] = 0
        self.assertEqual(A.sweep_land_refs(self.r.path, d, A.CurrentStore(A.process_store)),
                         ["refs/heads/land/20200101-000000-x-aaaaaa: unknown to entries and store, %d h old" % (
                             A.entry_id_age("20200101-000000", A.epoch_now()) // 3600),
                          "refs/heads/land/20200101-000000-x-aaaaaa: deleted"])

    def test_the_sweep_ages_an_unknown_ref_on_the_process_clock_that_stamped_its_id(self):
        """Entry ids are stamped by the process clock, not by git or a file: the sweep's age of one is that clock's."""
        from agentctl import clock as clock_module
        saved = A.land_refs_supported
        A.land_refs_supported = lambda repo: True
        self.addCleanup(setattr, A, "land_refs_supported", saved)
        self.addCleanup(clock_module.wire_clock, clock_module._PROCESS_CLOCK[0])
        wt, sha = self.agent("a", "web/src/main/A.scala", "a\n")
        d, _c, _w = A.waiting_state(self.r.path)
        sh(self.r.path, "git", "push", "--quiet", "origin", f"{sha}:refs/heads/land/20200101-000000-x-aaaaaa")
        stamp = A.entry_stamp_epoch("20200101-000000")  # an entry id's stamp is UTC (ids.stamp)
        clock_module.wire_clock(FakeClock(stamp + 30 * 3600 + 600))
        A._last_ref_sweep[0] = 0
        self.assertEqual(A.sweep_land_refs(self.r.path, d, A.CurrentStore(A.process_store)),
                         ["refs/heads/land/20200101-000000-x-aaaaaa: unknown to entries and store, 30 h old",
                          "refs/heads/land/20200101-000000-x-aaaaaa: deleted"])

    def test_no_land_ref_while_the_repositorys_hook_would_check_it(self):
        wt, _sha = self.agent("a", "web/src/main/A.scala", "a\n")
        self.queued(wt)
        self.assertEqual(self.remote_refs(), [], "the hook at origin/main knows no land/ refs: nothing pushed")


class FastPrecheckTest(QueueTest):
    """THE FAST PRECHECK (review 2026-11-06 P1; the owner: landing must not take longer): a repository with an
    executable scripts/land-precheck has it run ALONGSIDE the queue -- land still prints QUEUED at once -- and a refusal
    cancels the queued change with a NOT LANDED in the inbox minutes before the landing check would have said so."""
    def setUp(self):
        super().setUp()
        self.spawned = []
        self.saved_spawn = A.fast_precheck_spawn
        A.fast_precheck_spawn = lambda argv, **kw: self.spawned.append(argv)

    def tearDown(self):
        A.fast_precheck_spawn = self.saved_spawn
        super().tearDown()

    def agent_with_precheck(self, name, rc, line):
        wt, _ = self.agent(name, "web/src/main/A.scala", "a\n")
        self.r.write("scripts/land-precheck", f"#!/bin/sh\necho 'precheck lints FAIL (3 s)'\necho '{line}'\nexit {rc}\n", at=wt)
        os.chmod(os.path.join(wt, "scripts/land-precheck"), 0o755)
        return wt, self.r.commit(f"{name}: precheck", at=wt)

    def run_spawned(self, wt):
        self.assertEqual(len(self.spawned), 1, self.spawned)
        argv = self.spawned[0]
        self.assertEqual(argv[2], "land-precheck")
        return self.run_tool(*argv[2:], cwd=wt)

    def test_land_queues_at_once_and_starts_the_precheck_alongside(self):
        wt, sha = self.agent_with_precheck("a", 0, "PRECHECK PASS (3 s)")
        eid, out = self.queued(wt)
        self.assertIn("fast precheck", out)
        self.assertEqual(self.spawned[0][3], eid)
        self.assertIn(sha, self.spawned[0])

    def test_a_passing_precheck_leaves_the_change_queued_and_it_lands(self):
        wt, sha = self.agent_with_precheck("a", 0, "PRECHECK PASS (3 s)")
        eid, _ = self.queued(wt)
        rc, out = self.run_spawned(wt)
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.check(eid), 0)
        self.assertEqual(len(self.pushed()), 1, "the change still lands")

    def test_a_failing_precheck_cancels_the_queued_change_and_says_why_in_the_inbox(self):
        wt, _ = self.agent_with_precheck("a", 1, "PRECHECK FAIL: lints (3 s)")
        eid, _ = self.queued(wt)
        rc, out = self.run_spawned(wt)
        self.assertEqual(rc, 1, out)
        self.assertEqual(self.pushed(), [], "a change the precheck refused is never pushed")
        verdicts = [e for e in A.inbox_entries() if "fast precheck" in (e.get("verdict") or "")]
        self.assertEqual(len(verdicts), 1, [e.get("verdict") for e in A.inbox_entries()])
        self.assertRegex(verdicts[0]["verdict"], rf"^NOT LANDED: the fast precheck refused {eid}.*PRECHECK FAIL: lints")
        self.assertIn("precheck lints FAIL (3 s)", verdicts[0]["tail"])

    def test_a_refusing_steps_own_reason_is_in_the_not_landed_line(self):
        """ISSUE 20261116T225455-04c3c1: the NOT LANDED line named only `PRECHECK FAIL: rules` while the rule's refusal
        (commit_rules.py: the commit and why) sat in the step's detail lines: the fixer re-ran the rule to learn why."""
        wt, _ = self.agent("a", "web/src/main/A.scala", "a\n")
        self.r.write("scripts/land-precheck", "#!/bin/sh\necho 'precheck rebase PASS (0 s)'\n"
                     "echo 'precheck rules FAIL (0 s)'\n"
                     "echo '    driftguards: 1a2b3c4d5e fixes a drift/flip red but the push commits no drift slice'\n"
                     "echo '    ! blocked by: driftguards (0s). Fix, or bypass'\n"
                     "echo 'PRECHECK FAIL: rules (0 s)'\nexit 1\n", at=wt)
        os.chmod(os.path.join(wt, "scripts/land-precheck"), 0o755)
        self.r.commit("a: precheck", at=wt)
        self.queued(wt)
        rc, out = self.run_spawned(wt)
        self.assertEqual(rc, 1, out)
        [v] = [e["verdict"] for e in A.inbox_entries() if "fast precheck" in (e.get("verdict") or "")]
        self.assertIn("PRECHECK FAIL: rules", v)
        self.assertIn("fixes a drift/flip red but the push commits no drift slice", v)

    # The 2026-11-19 refusal of 20261119-172743-...-f900cc, as movies' land-precheck printed it: the NOT LANDED line
    # named two tests that PASSED ("lints: [info] - should name that file ...") and never the suite that failed.
    SCALATEST_FAIL = ["precheck rebase PASS (0 s)", "precheck rules PASS (12 s)", "precheck lints FAIL (296 s)",
                      "    [info] - should name that file for the app it belongs to",
                      "    [info] - should keep the exec so the JVM stays PID-adjacent",
                      "    [info] - should take temp directories and files through the helper that deletes them *** FAILED ***",
                      "    [info] - should name only files that are tests and still offend",
                      "    [error] Failed tests:", "    [error] \tdeploy.SharedMachineStateLintSpec",
                      "    [error] (repochecks / Test / testOnly) sbt.TestsFailedException: Tests unsuccessful",
                      "PRECHECK FAIL: lints (296 s)"]

    def test_a_failed_spec_is_named_by_its_suite_and_failing_test_never_by_the_tests_that_passed(self):
        why = A.precheck_reason(self.SCALATEST_FAIL)
        self.assertIn("deploy.SharedMachineStateLintSpec", why)
        self.assertIn("should take temp directories and files through the helper that deletes them", why)
        self.assertNotIn("should name that file", why)
        self.assertNotIn("PID-adjacent", why)

    def test_a_failed_specs_first_assertion_line_follows_its_name(self):
        lines = list(self.SCALATEST_FAIL)
        at = next(i for i, l in enumerate(lines) if "*** FAILED ***" in l) + 1
        lines[at:at] = ['    [info]   List("scripts/x.sh:3: mktemp -d") was not empty (SharedMachineStateLintSpec.scala:88)',
                        '    [info]   at more.stack(Frame.scala:1)']
        why = A.precheck_reason(lines)
        self.assertIn('List("scripts/x.sh:3: mktemp -d") was not empty (SharedMachineStateLintSpec.scala:88)', why)
        self.assertNotIn("more.stack", why)
        self.assertLess(why.index("SharedMachineStateLintSpec:"), why.index("was not empty"), why)

    def test_a_failed_specs_assertion_of_several_lines_is_kept_whole(self):
        """2026-11-23, movies 20261123-031245-...-b4be01: a withClue's list sits on the lines under its first, and
        the NOT LANDED line kept only "...or allowlist the line with why:" -- a refusal naming no offender."""
        lines = list(self.SCALATEST_FAIL)
        at = next(i for i, l in enumerate(lines) if "*** FAILED ***" in l) + 1
        lines[at:at] = ["    [info]   These lines sleep to wait; or allowlist the line with why:",
                        "    [info]   android/scripts/x_test.py:106: time.sleep(0.05)",
                        '    [info]   Vector("android/scripts/x_test.py:106: time.sleep(0.05)") was not empty (S.scala:211)',
                        "    [info]   at more.stack(Frame.scala:1)"]
        why = A.precheck_reason(lines)
        self.assertIn("with why: android/scripts/x_test.py:106: time.sleep(0.05)", why)
        self.assertNotIn("more.stack", why)

    def test_a_failed_unittest_is_named_with_its_assertion(self):
        why = A.precheck_reason(["precheck rules FAIL (3 s)", "    ok: test_a (m.T)",
                                 "    FAIL: test_b (scripts.ci.m_test.T.test_b)", "    Traceback (most recent call last):",
                                 '      File "m_test.py", line 9, in test_b', "    AssertionError: 1 != 2",
                                 "PRECHECK FAIL: rules (3 s)"])
        self.assertIn("scripts.ci.m_test.T.test_b", why)
        self.assertIn("AssertionError: 1 != 2", why)
        self.assertNotIn("test_a", why)

    def test_a_refusal_after_the_change_landed_never_says_not_landed(self):
        """Review 2026-11-07: the precheck runs beside the queue, so the landing check can decide first. Its refusal
        then cancels nothing -- `land cancel` finds no open change -- and must not tell the author NOT LANDED (and
        "its queue entry is cancelled") for a change that LANDED."""
        wt, _ = self.agent_with_precheck("a", 1, "PRECHECK FAIL: lints (3 s)")
        eid, _ = self.queued(wt)
        self.assertEqual(self.check(eid), 0)
        self.assertEqual(len(self.pushed()), 1, "landed before the precheck ended")
        rc, out = self.run_spawned(wt)
        self.assertEqual(rc, 0, out)
        self.assertEqual([e.get("verdict") for e in A.inbox_entries() if "fast precheck" in (e.get("verdict") or "")],
                         [], "no NOT LANDED for a landed change")
        self.assertIn("already decided", out)

    def test_a_refusal_that_could_not_cancel_says_so_never_not_landed(self):
        """The store unreachable: the change is still queued and its landing check still judges it -- the author is
        told the precheck refused and how to drop it, never NOT LANDED."""
        wt, _ = self.agent_with_precheck("a", 1, "PRECHECK FAIL: lints (3 s)")
        eid, _ = self.queued(wt)
        saved = A.queue_command

        def unreachable(a, store):
            raise A.StoreUnavailable("POST /v1/changes: Connection refused")
        A.queue_command = unreachable
        try:
            rc, out = self.run_spawned(wt)
        finally:
            A.queue_command = saved
        self.assertEqual(rc, 1, out)
        verdicts = [e["verdict"] for e in A.inbox_entries() if eid in (e.get("verdict") or "")
                    and "precheck" in (e.get("verdict") or "").lower()]
        self.assertEqual(len(verdicts), 1, verdicts)
        self.assertRegex(verdicts[0], rf"^PRECHECK REFUSED {eid} .*could not be cancelled.*land cancel {eid}")
        e = next(e for e in A.inbox_entries() if e.get("verdict") == verdicts[0])
        self.assertEqual(A.alert_audience(e), "author", "the author's to act on, like a NOT LANDED")

    def test_a_precheck_past_its_bound_is_ended_with_its_children_and_leaves_the_change_queued(self):
        """Review 2026-11-07: on its bound only the precheck script itself was killed; what it started (sbt, a test JVM)
        lived on, and holding the output pipe kept the precheck waiting too. The precheck runs in its own process
        group, ended whole at the bound; the change is left to its landing check.
        The bound is judged by the stall clock the test drives (StallSamples): it stands still until the precheck has
        started its child and written the child's pid (atomically), so at laptop load ~300 the bound can no longer end
        the precheck before it wrote its pid file (2026-11-08: FileNotFoundError on grandchild.pid)."""
        wt, _ = self.agent("a", "web/src/main/A.scala", "a\n")
        pidfile = os.path.join(wt, "grandchild.pid")
        StallSamples(hung=pidfile).install(self)
        self.r.write("scripts/land-precheck",
                     f"#!/bin/sh\nsleep 30 &\necho $! > {pidfile}.tmp\nmv {pidfile}.tmp {pidfile}\nwait\n", at=wt)
        os.chmod(os.path.join(wt, "scripts/land-precheck"), 0o755)
        self.r.commit("a: a precheck that hangs", at=wt)
        eid, _ = self.queued(wt)
        saved = A.FAST_PRECHECK_BOUND
        A.FAST_PRECHECK_BOUND = 1
        try:
            started = time.time()
            rc, out = self.run_spawned(wt)
        finally:
            A.FAST_PRECHECK_BOUND = saved
        self.assertLess(time.time() - started, 15, "ended at its bound, not when its child ended")
        self.assertEqual(rc, 0, out)
        self.assertIn("could not check", out)
        pid = int(open(pidfile).read())
        for _ in range(50):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            # a zombie is ended: in a container with no init (python:3.14 run plainly) nobody reaps the orphan
            if subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True,
                              text=True).stdout.strip().startswith("Z"):
                break
            time.sleep(0.1)
        else:
            self.fail(f"the precheck's child {pid} outlived its bound")
        self.assertEqual(self.check(eid), 0, "the landing check still judges it")

    def test_a_precheck_that_could_not_run_leaves_the_change_queued(self):
        wt, sha = self.agent_with_precheck("a", 2, "PRECHECK UNCHECKED: lints (300 s)")
        eid, _ = self.queued(wt)
        rc, out = self.run_spawned(wt)
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.check(eid), 0)

    def test_no_fast_precheck_and_a_repo_without_one_start_nothing(self):
        wt, _ = self.agent_with_precheck("a", 1, "PRECHECK FAIL: lints (3 s)")
        rc, out = self.land(wt, "--no-fast-precheck")
        self.assertIn("QUEUED", out)
        wt2, _ = self.agent("b", "web/src/main/B.scala", "b\n")
        self.queued(wt2)
        self.assertEqual(self.spawned, [])


class Died(BaseException):
    """A checker that dies mid-wait (SIGKILL: no handler runs past this point in the wait loop)."""


class RemotePickupTest(QueueTest):
    """2026-10-27: entry -45's remote run 3344 finished green yet the entry showed `checking ... remote run 3344` for an
    hour: the run belonged to the checker process (cancelled or orphaned when it ended), a replacement re-dispatched from
    scratch, and the saved remote_run was never cleared. Now the run is the ENTRY's: saved at once, adopted by a
    replacement checker, re-derived by the processor from GitHub every cycle."""

    def setUp(self):
        super().setUp()
        self.r.write(".github/workflows/precommit-check.yml", "name: Pre-commit check\n")
        self.r.commit("the remote check workflow")
        self.r.push()
        os.remove(self.pushes)
        self.state = {"status": "running", "polls": 0, "branch_pushes": [], "cancelled": [], "fail_api": False}
        saved = (A.api, A.slug, A.git, A.cancel_run, A.fetch_failed_log)
        self.addCleanup(lambda: setattr(A, "api", saved[0]) or setattr(A, "slug", saved[1])
                        or setattr(A, "git", saved[2]) or setattr(A, "cancel_run", saved[3])
                        or setattr(A, "fetch_failed_log", saved[4]))
        real_git = A.git
        st = self.state

        def git(repo, *args, **kw):
            if args[:1] == ("push",) and any("refs/heads/check/" in a for a in args):
                ref = next(a for a in args if "refs/heads/check/" in a)
                st["branch_pushes"].append(ref)
            return real_git(repo, *args, **kw)

        def api(o_r, method, path, params=None, body=None):
            if st["fail_api"]:
                raise RuntimeError("Connection refused")
            if path.startswith("actions/workflows/") and path.endswith("/runs"):
                return {"workflow_runs": [{"id": 99, "head_sha": ref.split(":")[0],
                                           "head_branch": ref.split("refs/heads/")[1]} for ref in st["branch_pushes"]]}
            if path == "actions/runs/99":
                st["polls"] += 1
                done = st["status"] not in ("running", "waiting")
                return {"id": 99, "run_number": 7, "status": "completed" if done else "in_progress",
                        "conclusion": st["status"] if done else None, "html_url": "http://f/runs/7"}
            return None
        A.api, A.slug, A.git = api, (lambda repo: "o/movies"), git
        A.cancel_run = lambda o_r, rid, why: st["cancelled"].append(rid)
        A.fetch_failed_log = lambda o_r, rid: ""

    def die_on_sleep(self, exc):
        def sleep(s):
            raise exc
        self.clock.on_sleep = sleep

    def entry(self, eid):
        return self.entries()[eid.rsplit("-", 1)[-1]]

    def test_a_checker_killed_mid_wait_strands_nothing_its_replacement_adopts_the_run(self):
        wt, _ = self.agent("a", "web/src/main/A.scala", "a\n")
        eid, _ = self.queued(wt)
        self.die_on_sleep(Died())
        with self.assertRaises(Died):
            self.check(eid)
        e = self.entry(eid)
        self.assertEqual((e["status"], e["remote"]["run"], e["remote_run"]), ("checking", 99, 99),
                         "the run is saved in the entry the moment it is found")
        self.assertEqual(self.state["cancelled"], [], "a dead checker's run is the entry's: never cancelled")
        self.state["status"] = "success"  # it finishes while no checker runs
        self.clock.on_sleep = None
        self.assertEqual(self.check(eid), 0)
        self.assertEqual(len(self.state["branch_pushes"]), 1, "adopted: no second check/ push, no second run")
        self.assertEqual(self.pushed(), ["push remote=", "push remote=99"], "the check/ branch, then main with the run")
        self.assertEqual(self.calls_of("--range"), [], "nothing checked on the laptop")
        self.assertEqual(self.entry(eid)["status"], "landed")

    def test_a_reexec_mid_wait_keeps_waiting_on_the_same_run(self):
        wt, _ = self.agent("a", "web/src/main/A.scala", "a\n")
        eid, _ = self.queued(wt)
        self.die_on_sleep(A.Terminated(143))  # a SIGTERM (re-exec, service restart) while the run is still running
        with self.assertRaises(SystemExit):
            self.check(eid)
        self.assertEqual(self.state["cancelled"], [])
        polls = []

        def sleep(s):  # the new process waits on the same run, which goes green after a while
            polls.append(s)
            if len(polls) == 3:
                self.state["status"] = "success"
        self.clock.on_sleep = sleep
        self.assertEqual(self.check(eid), 0)
        self.assertEqual(len(polls), 3)
        self.assertEqual(len(self.state["branch_pushes"]), 1)
        self.assertEqual(self.pushed(), ["push remote=", "push remote=99"], "the check/ branch, then main with the run")

    def test_a_terminate_is_never_swallowed_by_the_outage_handler(self):
        wt, _ = self.agent("a", "web/src/main/A.scala", "a\n")
        eid, _ = self.queued(wt)
        real = A.remote_poll

        def poll(o_r, rem, now):
            raise A.Terminated(143)
        A.remote_poll = poll
        self.addCleanup(setattr, A, "remote_poll", real)
        with self.assertRaises(SystemExit):
            self.check(eid)

    def checking_entry(self, since_min=0):
        wt, _ = self.agent("a", "web/src/main/A.scala", "a\n")
        eid, _ = self.queued(wt)
        self.die_on_sleep(Died())
        with self.assertRaises(Died):
            self.check(eid)
        self.clock.on_sleep = None
        d, _c, _w = A.waiting_state(self.r.path)
        e = self.entry(eid)
        if since_min:
            stamp = A.dt.datetime.fromtimestamp(time.time() - since_min * 60, A.dt.timezone.utc)
            e["round_since"] = stamp.strftime("%Y-%m-%dT%H:%M:%SZ")
            put_change(A.CurrentStore(A.process_store), d, e)
        return d, e

    def tick(self, d):
        started = []
        saved = A.spawn_executor
        A.spawn_executor = lambda repo, d_, eid: started.append(eid)
        try:
            return A.tick_changes(self.r.path, d, *on_clock(self.clock)), started
        finally:
            A.spawn_executor = saved

    def test_a_checker_that_stops_progressing_loses_its_lease_and_the_tick_starts_one_that_adopts_its_run(self):
        """Step 6 (reconcile_checking deleted): a wedged checker stops renewing check/<entry>; once the lease lapses
        the tick starts a replacement, which adopts the decided run."""
        d, e = self.checking_entry()
        st = A.process_store()
        got = st.acquire(f"check/{e['id']}", "a wedged checker", 600, 3600)
        self.assertEqual(self.tick(d)[1], [], "a live checker: nothing started")
        st.release(f"check/{e['id']}", got["epoch"], got["secret"])  # its lease lapsed (here at once)
        self.assertEqual(self.tick(d)[1], [e["id"]])
        self.state["status"] = "success"
        self.assertEqual(self.check(e["id"]), 0, "the replacement adopts the verdict")
        self.assertEqual(self.pushed(), ["push remote=", "push remote=99"], "the check/ branch, then main with the run")

    def test_checking_past_its_deadline_with_no_checker_is_queued_again_as_infra(self):
        d, e = self.checking_entry()
        st = A.process_store()
        st.put_item("change", e["id"], "checking")
        self.tick(d)  # in time: unchanged
        self.assertEqual(self.entry(e["id"])["status"], "checking")
        saved = A.spawn_executor
        A.spawn_executor = lambda *a: None
        try:
            A.tick_changes(self.r.path, d, *on_clock(FakeClock(time.time() + 3 * 3600)))
        finally:
            A.spawn_executor = saved
        e2 = self.entry(e["id"])
        self.assertEqual((e2["status"], e2["infra_retries"]), ("queued", 1), "infra, never a rejection")

    def test_superseding_an_entry_cancels_its_run(self):
        d, e = self.checking_entry()
        A.supersede(A.CurrentStore(A.process_store), d, e, "newer", "queued again")
        self.assertEqual(self.state["cancelled"], [99])

    def test_a_replacement_after_main_moved_adopts_the_run_on_its_own_base(self):
        """2026-11-15 22:42Z: ci-runner's reconciler restarted on a new agentctl pin (KillMode=mixed: its detached
        checkers died with it). The replacements came once the leases lapsed, after main had moved, and adopted only a
        run on the CURRENT main: rev-identity's half-done run 43770050132 was cancelled, ci-alerts-fix's GREEN run
        43770333199 thrown away, each a new 20-minute round on two runners. A surviving checker would have reported
        those runs checked on their own base, and the push would have judged the move (re-check on overlap). The
        replacement does the same now: the run is adopted on the base it checked."""
        d, e = self.checking_entry()
        old_base = e["remote"]["base"]
        other, _ = self.agent("b", "worker/src/main/B.scala", "b\n", msg="b: two")
        sh(other, "git", "push", "-q", "origin", "HEAD:main")
        self.state["status"] = "success"  # it went green while no checker ran
        A.check_entry(self.r.path, e["id"], *on_clock(self.clock))
        e2 = self.entry(e["id"])
        self.assertEqual(self.state["cancelled"], [], "the dead checker's run is adopted, never cancelled")
        self.assertEqual(len(self.state["branch_pushes"]), 1, "no second check/ push, no second run")
        self.assertEqual((e2["status"], e2["checked_base"], e2["tip"]), ("checked", old_base, e["remote"]["tip"]),
                         "checked on the base its run checked: the push judges what landed since")
        self.assertEqual(self.check(e["id"]), 0)
        self.assertEqual(self.entry(e["id"])["status"], "landed")
        self.assertEqual(len(self.state["branch_pushes"]), 2, "no file overlap with what landed: no full round, only "
                                                              "the combined tree's verification")
        self.assertRegex(self.state["branch_pushes"][1], rf"check/verify-{old_base}-{e['id']}$")

    def test_a_replacement_never_adopts_a_run_whose_base_main_no_longer_holds(self):
        d, e = self.checking_entry()
        e["remote"]["base"] = "0" * 40  # not main's ancestor (nor any commit here): no evidence about this main
        put_change(A.CurrentStore(A.process_store), d, e)
        self.state["status"] = "success"
        A.check_entry(self.r.path, e["id"], *on_clock(self.clock))
        self.assertEqual(self.state["cancelled"], [99], "the stray run is cancelled")
        self.assertEqual(len(self.state["branch_pushes"]), 2, "a new round on the current main")

    def test_a_new_round_clears_the_old_runs_record(self):
        d, e = self.checking_entry()
        e["remote"].update(status="success", collected_at=time.time())  # the last round's run: collected, over
        put_change(A.CurrentStore(A.process_store), d, e)
        other, _ = self.agent("b", "worker/src/main/B.scala", "b\n", msg="b: two")
        sh(other, "git", "push", "-q", "origin", "HEAD:main")
        saved_rc = A.remote_check
        A.remote_check = lambda repo, e_, base, head, mode, clock, st, own=None, red=False: (True, "precommit-check.yml: success")
        self.addCleanup(setattr, A, "remote_check", saved_rc)
        seen = []
        real = A.run_entry_checks

        def spy(repo, d_, e_, *a, **k):
            seen.append(A.ChangeRecords(A.process_store(), d_).get(e_['id']))
            return real(repo, d_, e_, *a, **k)
        A.run_entry_checks = spy
        self.addCleanup(setattr, A, "run_entry_checks", real)
        self.assertEqual(self.check(e["id"]), 0)
        self.assertNotIn("remote_run", seen[0], "main moved: the old run's record is gone from the saved entry")
        self.assertEqual(self.state["cancelled"], [], "a decided run is never cancelled")
        self.assertEqual(self.pushed()[-1], "push remote=")


def own_tests_only(cls, *bases):
    """A fixture-borrowing class runs ONLY its own test methods, however it is loaded. load_tests (below) covers
    discovery, but `python3 -m unittest tests.test_queue.RemotePickupTest` loads the class by name and bypasses it:
    RemotePickupTest then ran QueueTest's inherited tests under its own mocks (the remote run is always `running`,
    the laptop always under pressure, so every check goes remote) and they polled that run forever, one core each
    (2026-10-29: two such runs spun 48 min on an overloaded laptop). The inherited names are set to None, which the
    loader skips (not callable)."""
    for base in bases:
        for n in dir(base):
            if n.startswith("test_") and n not in vars(cls):
                setattr(cls, n, None)
    return cls


own_tests_only(QueueTest, WaitingTest)
own_tests_only(RemotePickupTest, QueueTest, WaitingTest)
own_tests_only(FastPrecheckTest, QueueTest, WaitingTest)


class CheckPushBlipTest(unittest.TestCase):
    """2026-11-17: deploy-speed and ref-scrapers re-queued "its checks stalled" 13 s into a round, no check/ branch
    created on GitHub: the push failed once, and each lost 20 min to the backoff and the slot queue."""

    def test_a_failed_check_branch_push_is_tried_again_and_its_words_kept(self):
        real, calls, slept = A.git, [], []

        def flaky(repo, *args, check=True, input=None):
            if args[:1] == ("push",):
                calls.append(args)
                ok = len(calls) == 3
                return subprocess.CompletedProcess(args, 0 if ok else 1, "", "" if ok else "remote: Internal error")
            return real(repo, *args, check=check, input=input)
        A.git = flaky
        self.addCleanup(setattr, A, "git", real)
        r = A.check_branch_push("/r", "abc", "check/x", slept.append)
        self.assertEqual((r.returncode, len(calls), slept), (0, 3, list(A.CHECK_PUSH_DELAYS)))
        calls.clear()
        A.git = lambda repo, *args, check=True, input=None: subprocess.CompletedProcess(args, 1, "", "denied")
        self.assertEqual(A.check_branch_push("/r", "abc", "check/x", slept.append).stderr, "denied")


class CredentialFailureTest(unittest.TestCase):
    """2026-11-22: ci-runner's check-branch pushes all failed `fatal: could not read User` (its App credential helper
    had died) and were retried as network blips; land's own push failing (DNS) said `agentctl env --check`, which said
    the movies vault was locked, and its fixer asked the owner for an unlock no push reads."""

    def test_a_credential_failure_is_its_own_never_the_networks(self):
        for text in ("fatal: could not read Username for 'https://github.com': terminal prompts disabled",
                     "remote: Invalid username or token.\nfatal: Authentication failed for 'https://github.com/o/r/'",
                     "fatal: unable to access 'https://github.com/o/r.git/': The requested URL returned error: 403"):
            self.assertTrue(A.credential_failure(text), text)
        self.assertFalse(A.credential_failure("fatal: unable to access 'https://github.com/o/r.git/': Could not "
                                              "resolve host: github.com"))

    def test_a_check_branch_push_refused_its_credential_is_not_retried_and_names_the_helpers_error(self):
        calls, slept = [], []
        stderr = ("Traceback (most recent call last):\n  File \"/x/lib/agentctl/ghapp.py\", line 12, in <module>\n"
                  "ModuleNotFoundError: No module named 'agentctl'\n"
                  "fatal: could not read Username for 'https://github.com': terminal prompts disabled")
        real = A.git
        A.git = lambda repo, *args, check=True, input=None: calls.append(args) or \
            subprocess.CompletedProcess(args, 128, "", stderr)
        self.addCleanup(setattr, A, "git", real)
        r = A.check_branch_push("/r", "abc", "check/x", slept.append)
        self.assertEqual((len(calls), slept), (1, []), "no wait brings a credential back")
        last = A.check_push_failure("check/x", r.stderr, "ci-runner").splitlines()[-1]
        self.assertIn("GitHub credential failed on ci-runner", last)
        self.assertIn("No module named 'agentctl'", last)

    def test_no_credential_reaches_what_agentctl_says(self):
        for text, secret in (("password=hunter2", "hunter2"), ("token: ghs_abcdefabcdefabcdefabcd", "ghs_"),
                             ("fatal: unable to access 'https://x-access-token:s3cr3t@github.com/o/r/'", "s3cr3t"),
                             ("github_pat_11ABCDEF_xyz rejected", "github_pat_")):
            self.assertNotIn(secret, A.redact_credentials(text), text)
            self.assertNotIn(secret, A.push_failure_why(text), text)
            self.assertNotIn(secret, A.check_push_failure("check/x", "fatal: Authentication failed\n" + text, "laptop"))
        self.assertEqual(A.redact_credentials("fatal: Authentication failed for 'https://github.com/o/r/'"),
                         "fatal: Authentication failed for 'https://github.com/o/r/'")

    def test_lands_not_landed_says_what_git_said_and_never_points_at_the_vault(self):
        dns = "fatal: unable to access 'https://github.com/o/r.git/': Could not resolve host: github.com"
        why = A.push_failure_why(dns)
        self.assertIn("Could not resolve host", why)
        self.assertIn("the network's", why)
        self.assertNotIn("env --check", why)
        self.assertIn("gh auth status", A.push_failure_why("fatal: Authentication failed for 'https://github.com/'"))


class RemoteRunProgressTest(unittest.TestCase):
    """2026-11-17, ref-srp-54ebae: every check passed by 14:44, `check (page)` waited 23 min for a saturated check runner
    and was 14 min into its work when the 2-h bound, counted from the push at 13:19, cancelled the run -- a run making
    progress thrown away, the change queued and checked again from scratch. The bound judges progress: a run whose jobs
    still move runs on (up to REMOTE_TIMEOUT_MAX); one idle REMOTE_IDLE past REMOTE_TIMEOUT is cancelled."""

    def check(self, idle_s, age_s=2.5 * 3600):
        tmp = tempfile.mkdtemp()
        sh(tmp, "git", "init", "-q")
        t0 = time.time()
        rem = {"branch": "check/x", "head": "h", "base": "b", "tip": "t", "mode": "--checks-only", "run": 7,
               "pushed_at": t0 - age_s, "status": None, "progress_at": t0 - idle_s}
        e = {"id": "x", "head": "h", "remote": dict(rem)}
        polls, cancelled = [], []

        def poll(o_r, r, now):
            polls.append(1)
            return {**r, "status": "success", "out": "ok"} if len(polls) > 1 else dict(r)
        saved = (A.remote_poll, A.cancel_run, A.slug, A.waiting_state, A.delete_check_branch)
        A.remote_poll, A.slug, A.waiting_state = poll, (lambda repo: "o/movies"), (lambda repo: (None, None, None))
        A.cancel_run = lambda o_r, run, why: cancelled.append(why)
        A.delete_check_branch = lambda o_r, b: None
        try:
            return A.remote_check(tmp, e, "b", "t", "--checks-only", FakeClock(t0), A.CurrentStore(A.process_store)), cancelled
        finally:
            (A.remote_poll, A.cancel_run, A.slug, A.waiting_state, A.delete_check_branch) = saved

    def test_a_run_past_the_bound_whose_jobs_still_move_is_waited_for(self):
        (ok, _out), cancelled = self.check(idle_s=60)
        self.assertEqual((ok, cancelled), (True, []))

    def test_a_run_idle_past_the_bound_is_cancelled(self):
        (ok, out), cancelled = self.check(idle_s=A.REMOTE_IDLE + 60)
        self.assertIsNone(ok)
        self.assertEqual(len(cancelled), 1)
        self.assertIn("no progress", out)

    def test_a_run_past_the_hard_cap_is_cancelled_however_it_moves(self):
        (ok, _out), cancelled = self.check(idle_s=60, age_s=A.REMOTE_TIMEOUT_MAX + 60)
        self.assertIsNone(ok)
        self.assertEqual(len(cancelled), 1)

    def test_the_poll_marks_progress_when_the_runs_jobs_move(self):
        r1 = A.remote_progress({"run": 7}, {"updated_at": "a"}, [{"name": "j", "status": "queued"}], 100.0)
        r2 = A.remote_progress(r1, {"updated_at": "a"}, [{"name": "j", "status": "queued"}], 200.0)
        r3 = A.remote_progress(r2, {"updated_at": "a"}, [{"name": "j", "status": "in_progress"}], 300.0)
        self.assertEqual((r1["progress_at"], r2["progress_at"], r3["progress_at"]), (100.0, 100.0, 300.0))


class RemoteJobExpectedTest(unittest.TestCase):
    """2026-11-18: a healthy run whose last job is long (one job > 30 min into its work, the others done) read as idle:
    nothing in the job list moved, so past 2 h it was cancelled. A running job still inside its own expected duration
    -- its timeout-minutes, else its recorded p90, else REMOTE_JOB_CAP -- is progress."""

    FLOW = """name: precommit-check
jobs:
  plan:
    runs-on: x
    timeout-minutes: 10
  heavy:
    name: heavy rows (itrest)
    timeout-minutes: 90
  identity:
    name: check (identity ${{ matrix.shard }}/2)
    runs-on: x
  report:
    runs-on: x
"""

    def job(self, name, started, status="in_progress"):
        return {"name": name, "status": status, "started_at": A.iso_at(started)}

    def test_the_workflow_declares_each_jobs_timeout(self):
        self.assertEqual(A.workflow_job_timeouts(self.FLOW), {"plan": 600, "heavy rows (itrest)": 5400,
                                                              "check (identity ${{ matrix.shard }}/2)": None,
                                                              "report": None})

    def test_a_long_job_inside_its_timeout_is_progress(self):
        t = 10_000.0
        jobs = [self.job("plan", t - 7000, "completed"), self.job("heavy rows (itrest)", t - 50 * 60)]
        rem = {"run": 7, "progress_sig": None, "progress_at": t - 3600}
        rem = A.remote_progress(rem, {"updated_at": "a"}, jobs, t - 1800, bounds={"heavy rows (itrest)": 5400})
        again = A.remote_progress(rem, {"updated_at": "a"}, jobs, t, bounds={"heavy rows (itrest)": 5400})
        self.assertEqual(again["progress_at"], t, "50 min into a 90-min job: alive, though nothing in the list moved")

    def test_a_job_past_its_own_bound_is_no_progress(self):
        t = 10_000.0
        jobs = [self.job("heavy rows (itrest)", t - 100 * 60)]
        rem = A.remote_progress({"run": 7}, {"updated_at": "a"}, jobs, t - 600, bounds={"heavy rows (itrest)": 5400})
        self.assertEqual(A.remote_progress(rem, {"updated_at": "a"}, jobs, t, bounds={"heavy rows (itrest)": 5400})
                         ["progress_at"], t - 600)

    def test_a_job_with_no_timeout_uses_its_p90_then_the_cap(self):
        flow = A.workflow_job_timeouts(self.FLOW)
        b = A.remote_job_bounds(flow, {"check (identity 1/2)": 1200.0})
        self.assertEqual(A.remote_job_expected(b, "heavy rows (itrest)"), 5400)
        self.assertEqual(A.remote_job_expected(b, "check (identity 1/2)"), 1200.0 * A.REMOTE_P90_HEADROOM)
        self.assertEqual(A.remote_job_expected(b, "check (identity 2/2)"), A.REMOTE_JOB_CAP)
        self.assertEqual(A.remote_job_expected(b, "report"), A.REMOTE_JOB_CAP)


# the repo's hook as movies' answers what its checks read (MINIMAL RECHECKS): its plan by path (code -> sbt, identity/ ->
# the identity area, scripts/ -> the script tests, docs -> nothing), its compile plan (common <- worker, web), the
# whole-tree ratchets (production code), and a lint plan with one standing lint
READS_HOOK = """#!/bin/sh
case "$1" in
    --compile-plan)
        while read -r p; do
            case "$p" in
                common/*.scala) printf 'common/Test/compile\\nworker/Test/compile\\nweb/Test/compile\\n' ;;
                worker/*.scala) echo worker/Test/compile ;;
                web/*.scala)    echo web/Test/compile ;;
            esac
        done
        exit 0 ;;
    --plan)
        while read -r p; do
            case "$p" in *.scala) echo sbt ;; esac
            case "$p" in identity/*) echo identity ;; esac
            case "$p" in scripts/*) printf 'scripttests\\nshellcheck\\n' ;; esac
        done | sort -u
        exit 0 ;;
    --coupling-plan) grep '/src/main/' || true; exit 0 ;;
    --quality-plan) grep '/src/main/' || true; exit 0 ;;
    --lint-plan) echo standing:Lint; grep -q '^scripts/' && echo repo:ScriptsLint; exit 0 ;;
esac
# --verify-only A..B: the combined tree (every test config compiled, the touched modules' unit tests)
"""


class MinimalRecheckTest(QueueTest):
    """MINIMAL RECHECKS (the owner, 2026-11-20: "so that rechecks are kept to a minimum"): main moving under a checked
    change re-checks it only when a check its paths select reads what main changed; otherwise its verdict is kept --
    rebased and pushed, no round -- and `land status <id>` says why each round ran, or did not."""

    def setUp(self):
        super().setUp()
        self.r.write("scripts/hooks/pre-push", READS_HOOK + REPO_HOOK.format(calls=self.calls).split("\n", 1)[1])
        os.chmod(os.path.join(self.r.path, "scripts/hooks/pre-push"), 0o755)
        self.r.commit("the hook's read plans")
        self.r.push()
        for p in (self.pushes, self.calls):
            if os.path.exists(p):
                os.remove(p)

    def status_of(self, eid):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            A.land_status_entry(A.process_store(), eid, time.time())
        return out.getvalue()

    def test_a_main_move_outside_what_its_checks_read_keeps_the_verdict(self):
        wt, _ = self.agent("a", "web/src/main/A.scala", "a\n", msg="a: one")
        eid, _ = self.queued(wt)
        rounds = self.moves_once(("docs/notes.md", "n\n"), ("scripts/tool.sh", "t\n"))
        self.assertEqual(self.check(eid), 0)
        self.assertEqual(len(rounds), 1, "the full check only: no verify round")
        self.assertEqual([m for m, _r in self.verified()], ["--range"])
        self.assertEqual(self.remote_log()[0], "a: one", "rebased onto the moved main and pushed")
        self.assertIn("no check of it reading what changed: its verdict kept", self.landed_verdict())
        self.assertEqual(self.pushed()[-1], "push remote= skip=sbt", "the push skips the plan its checks passed")
        self.assertRegex(self.status_of(eid), r"recheck: \d\d:\d\dZ kept on [0-9a-f]{10}: main moved by 2 commits")

    def e2e_downstream(self):
        """The hook's compile plan as movies' is: e2e downstream of worker and of web (compile_plan's `[ $worker = 1 ] &&
        e2e=1`, `[ $web = 1 ] && e2e=1`)."""
        hook = os.path.join(self.r.path, "scripts/hooks/pre-push")
        text = open(hook).read()
        for m in ("worker", "web"):
            text = text.replace(f"echo {m}/Test/compile ;;", f"printf '{m}/Test/compile\\ne2e/Test/compile\\n' ;;")
        self.r.write("scripts/hooks/pre-push", text)
        self.r.commit("e2e downstream of worker and web")
        self.r.push()

    def test_two_changes_meeting_only_in_a_module_downstream_of_both_keep_the_verdict(self):
        """ONE SIDE COMPILED AGAINST THE OTHER (2026-11-21): in 11-19..21 no verdict was kept; 24 verify rounds named the
        compile plan first, most of them `e2e/Test/compile`, which every two code changes' plans hold. A worker test
        change and a web change meet only in e2e, whose own sources neither touches: neither side's code is compiled
        against the other's."""
        self.e2e_downstream()
        wt, _ = self.agent("a", "worker/src/test/scala/WSpec.scala", "a\n", msg="a: one")
        eid, _ = self.queued(wt)
        self.moves_once(("web/src/main/W.scala", "w\n"))
        self.assertEqual(self.check(eid), 0)
        self.assertEqual([m for m, _r in self.verified()], ["--range"], "no verify round: e2e compiles both, neither the other")
        self.assertIn("its verdict kept", self.landed_verdict())

    def test_a_change_compiled_against_what_main_changed_still_verifies(self):
        self.e2e_downstream()
        wt, _ = self.agent("a", "worker/src/test/scala/WSpec.scala", "a\n", msg="a: one")
        eid, _ = self.queued(wt)
        self.moves_once(("worker/src/main/W.scala", "w\n"))
        self.assertEqual(self.check(eid), 0)
        self.assertEqual([m for m, _r in self.verified()], ["--range", "--verify-only"], "worker's tests compile its main")
        self.assertIn("its compile-plan reads what main changed: worker/Test/compile", self.status_of(eid))

    def test_a_main_move_its_compile_plan_reads_forces_the_verify_round(self):
        wt, _ = self.agent("a", "web/src/main/A.scala", "a\n", msg="a: one")
        eid, _ = self.queued(wt)
        self.moves_once(("common/src/main/C.scala", "c\n"))
        self.assertEqual(self.check(eid), 0)
        self.assertEqual([m for m, _r in self.verified()], ["--range", "--verify-only"], "verified: web compiles common")
        self.assertIn("recheck: ", self.status_of(eid))
        self.assertIn("verify on", self.status_of(eid))
        self.assertIn("its compile-plan reads what main changed: web/Test/compile", self.status_of(eid))

    def test_a_check_both_sides_select_forces_the_verify_round(self):
        wt, _ = self.agent("a", "scripts/a.sh", "a\n", msg="a: one")
        eid, _ = self.queued(wt)
        self.moves_once(("scripts/b.sh", "b\n"))
        self.assertEqual(self.check(eid), 0)
        self.assertEqual([m for m, _r in self.verified()], ["--range", "--verify-only"])
        self.assertIn("its plan reads what main changed: scripttests", self.status_of(eid))

    def test_a_per_file_check_or_a_standing_lint_both_select_reads_nothing_shared(self):
        hook = os.path.join(self.r.path, "scripts/hooks/pre-push")
        self.r.write("scripts/hooks/pre-push", open(hook).read().replace("scripttests\\n", "")
                     .replace("grep -q '^scripts/' && echo repo:ScriptsLint", "cat > /dev/null"))
        self.r.commit("no script tests, no repo lint")
        self.r.push()
        wt, _ = self.agent("a", "scripts/a.sh", "a\n", msg="a: one")
        eid, _ = self.queued(wt)
        self.moves_once(("scripts/b.sh", "b\n"))
        self.assertEqual(self.check(eid), 0)
        self.assertEqual([m for m, _r in self.verified()], ["--range"], "shellcheck reads only its own files")

    def per_file_hook(self):
        """The hook with no script self-tests, whose ScriptsLint judges each script alone (--per-file-lints): selected by
        a script, or by its own code (repochecks/src/test/), and named per-file only for the scripts."""
        hook = os.path.join(self.r.path, "scripts/hooks/pre-push")
        self.r.write("scripts/hooks/pre-push", open(hook).read().replace("scripttests\\n", "")
                     .replace("grep -q '^scripts/' && echo repo:ScriptsLint",
                              "grep -qE '^(scripts/|repochecks/src/test/)' && echo repo:ScriptsLint")
                     .replace("    --coupling-plan)",
                              "    --per-file-lints) grep -q '^scripts/' && ! grep -q '^repochecks/' && echo repo:ScriptsLint; exit 0 ;;\n"
                              "    --coupling-plan)"))
        self.r.commit("a per-file script lint")
        self.r.push()

    def test_two_changes_meeting_only_in_a_per_file_lint_keep_the_verdict(self):
        """PER-FILE LINTS (movies, 2026-11-21): NoSettingFallbackSpec reads every script but judges each alone, so two
        changes to different scripts meet in no verdict of it."""
        self.per_file_hook()
        wt, _ = self.agent("a", "scripts/a.sh", "a\n", msg="a: one")
        eid, _ = self.queued(wt)
        self.moves_once(("scripts/b.sh", "b\n"))
        self.assertEqual(self.check(eid), 0)
        self.assertEqual([m for m, _r in self.verified()], ["--range"], "the per-file lint judged each script alone")
        self.assertIn("its verdict kept", self.landed_verdict())

    def test_a_per_file_lint_whose_code_one_side_changes_still_verifies(self):
        """...but a side changing the lint itself (its rule, its allowlist) is judged against the other's scripts."""
        self.per_file_hook()
        wt, _ = self.agent("a", "scripts/a.sh", "a\n", msg="a: one")
        eid, _ = self.queued(wt)
        self.moves_once(("repochecks/src/test/ScriptsLintSpec.scala", "s\n"))
        self.assertEqual(self.check(eid), 0)
        self.assertEqual([m for m, _r in self.verified()], ["--range", "--verify-only"])
        self.assertIn("its lint-plan reads what main changed: repo:ScriptsLint", self.status_of(eid))

    def test_a_production_change_on_each_side_shares_the_whole_tree_ratchets(self):
        wt, _ = self.agent("a", "web/src/main/A.scala", "a\n", msg="a: one")
        eid, _ = self.queued(wt)
        self.moves_once(("worker/src/main/W.scala", "w\n"))  # no compile plan in common: the ratchets read both
        self.assertEqual(self.check(eid), 0)
        self.assertEqual([m for m, _r in self.verified()], ["--range", "--verify-only"])
        self.assertIn("its coupling-plan reads what main changed: the whole production tree", self.status_of(eid))

    def per_test_hook(self):
        """The hook naming the script self-tests a path selects (--script-test-plan): a-test.sh reads a.sh and lib.sh,
        b-test.sh reads b.sh and lib.sh."""
        hook = os.path.join(self.r.path, "scripts/hooks/pre-push")
        self.r.write("scripts/hooks/pre-push", open(hook).read().replace(
            "    --coupling-plan)", "    --script-test-plan)\n        while read -r p; do case \"$p\" in "
            "scripts/a.sh) echo scripts/a-test.sh ;; scripts/b.sh) echo scripts/b-test.sh ;; "
            "scripts/lib.sh) printf 'scripts/a-test.sh\\nscripts/b-test.sh\\n' ;; esac; done | sort -u; exit 0 ;;\n"
            "    --coupling-plan)", 1).replace("grep -q '^scripts/' && echo repo:ScriptsLint", "cat > /dev/null"))
        self.r.commit("the hook's script-test plan")
        self.r.push()

    def test_two_script_changes_whose_self_tests_differ_keep_the_verdict(self):
        """PER TEST (2026-11-20): the one `scripttests` word made every two script changes share a check."""
        self.per_test_hook()
        wt, _ = self.agent("a", "scripts/a.sh", "a\n", msg="a: one")
        eid, _ = self.queued(wt)
        self.moves_once(("scripts/b.sh", "b\n"))
        self.assertEqual(self.check(eid), 0)
        self.assertEqual([m for m, _r in self.verified()], ["--range"], "no self-test reads both: its verdict kept")
        self.assertIn("its verdict kept", self.landed_verdict())

    def test_a_self_test_reading_both_sides_forces_the_verify_round(self):
        self.per_test_hook()
        wt, _ = self.agent("a", "scripts/a.sh", "a\n", msg="a: one")
        eid, _ = self.queued(wt)
        self.moves_once(("scripts/lib.sh", "l\n"))
        self.assertEqual(self.check(eid), 0)
        self.assertEqual([m for m, _r in self.verified()], ["--range", "--verify-only"])
        self.assertIn("its script-test-plan reads what main changed: scripts/a-test.sh", self.status_of(eid))

    def test_a_hook_that_cannot_say_what_its_checks_read_verifies_as_before(self):
        hook = os.path.join(self.r.path, "scripts/hooks/pre-push")
        self.r.write("scripts/hooks/pre-push", open(hook).read().replace("--compile-plan)", "--other-plan)"))
        self.r.commit("no compile plan")
        self.r.push()
        wt, _ = self.agent("a", "web/src/main/A.scala", "a\n", msg="a: one")
        eid, _ = self.queued(wt)
        self.moves_once(("docs/notes.md", "n\n"))
        self.assertEqual(self.check(eid), 0)
        self.assertEqual([m for m, _r in self.verified()], ["--range", "--verify-only"])

    def test_the_rebase_changing_the_changes_own_diff_is_seen(self):
        base = sh(self.r.root, "git", "--git-dir", self.r.bare, "rev-parse", "main").strip()
        wt, tip = self.agent("a", "web/src/main/A.scala", "a\n", msg="a: one")
        other, _ = self.agent("b", "docs/b.md", "b\n", msg="b")
        moved = sh(other, "git", "rev-parse", "HEAD").strip()
        sh(wt, "git", "rebase", "-q", moved)
        rebased = sh(wt, "git", "rev-parse", "HEAD").strip()
        self.assertFalse(A.own_diff_changed(wt, base, tip, moved, rebased), "the same patch on another base")
        self.r.write("web/src/main/A.scala", "a\nmore\n", at=wt)
        self.r.commit("a: more", at=wt)
        grown = sh(wt, "git", "rev-parse", "HEAD").strip()
        self.assertTrue(A.own_diff_changed(wt, base, tip, moved, grown), "its diff grew in the rebase")


class LandedInboxTest(QueueTest):
    """EVERY WAY A CHANGE LANDS TELLS ITS AUTHOR ONCE (review round 7). LANDED verdicts went missing twice: 20261116-184811-
movies-r2-ci-ac68b8 (a batch landing) and 2b8927680 (a verify-round push; its author's `inbox wait --audience author
--kinds LANDED` timed out after 3000 s though the commit was on origin/main). Each landing -- a solo push, a batch, by
content, the verify round's push -- writes exactly ONE unread author-audience LANDED inbox entry, under the queued
entry's own id, in its author's worktree."""

    def assert_told_once(self, eid, wt):
        """Exactly one unread author LANDED entry for wt, and it is eid's."""
        landed = [e for e in self.inbox() if e.get("kind") == "LANDED" and not e["read"]
                  and os.path.realpath(e.get("cwd") or "") == os.path.realpath(wt)]
        self.assertEqual([(e.get("entry"), A.alert_audience(e)) for e in landed], [(eid, "author")],
                         [(e["id"], e.get("audience"), e.get("verdict")) for e in landed])
        self.assertEqual(A.inbox_wait("LANDED", timeout=0.01, audience="author"), 0,
                         "the author's `inbox wait --audience author --kinds LANDED` wakes on it")

    def test_a_solo_push(self):
        wt, _ = self.agent("a", "web/src/main/A.scala", "a\n", msg="a: one")
        eid, _ = self.queued(wt)
        self.assertEqual(self.check(eid), 0)
        self.assertEqual(self.remote_log()[0], "a: one")
        self.assert_told_once(eid, wt)

    def test_the_verify_rounds_push(self):
        wt, _ = self.agent("a", "web/src/main/A.scala", "a\n", msg="a: one")
        eid, _ = self.queued(wt)
        self.moves_once(("worker/src/main/W.scala", "w\n"))
        self.assertEqual(self.check(eid), 0)
        self.assertIn("the combined tree verified", self.landed_verdict())
        self.assert_told_once(eid, wt)

    def test_a_batch(self):
        self.reds = {"7"}
        wa, _ = self.agent("a", "web/src/main/A.scala", "a\n", msg="a: waits")
        wb, _ = self.agent("b", "worker/src/main/B.scala", "b\n", msg="b: waits")
        ea, _ = self.queued(wa)
        self.check(ea)
        eb, _ = self.queued(wb)
        self.check(eb)
        self.reds = set()
        self.drive()
        self.assertEqual(set(self.remote_log()[:2]), {"a: waits", "b: waits"})
        self.assert_told_once(ea, wa)
        self.assert_told_once(eb, wb)

    def test_by_content_a_queue_entry(self):
        wt, head = self.agent("a", "web/src/main/A.scala", "a\n", msg="a: by hand")
        eid, _ = self.queued(wt)
        sh(wt, "git", "push", "-q", "origin", "HEAD:main")  # landed meanwhile by hand
        A._landed_looked.clear()
        self.drive()
        self.assertEqual(self.entries()[eid.rsplit("-", 1)[-1]]["status"], "landed")
        self.assert_told_once(eid, wt)

    def test_by_content_a_store_item_no_queue_carries(self):
        """The laptop's look: a change item the store holds, closed by content (close_item_landed)."""
        wt, head = self.agent("a", "web/src/main/A.scala", "a\n", msg="a: by hand")
        st = A.process_store()
        st.put_item("change", "x-95", "waiting", {"worktree": wt, "head": head})
        sh(wt, "git", "push", "-q", "origin", "HEAD:main")
        A._landed_looked.clear()
        self.assertEqual(A.close_landed_changes(self.r.path, A.Clock(), store=st),
                         ["x-95: its commits are on main (by content): landed"])
        self.assert_told_once("x-95", wt)


def load_tests(loader, tests, pattern):
    """Only this file's own cases (QueueTest borrows WaitingTest's fixture, not its tests): every TestCase class DEFINED
    here, each with only its own test methods (2026-11-17: a hand-kept list left CheckPushBlipTest and
    RemoteRunProgressTest out of every run)."""
    here = [c for c in globals().values() if isinstance(c, type) and issubclass(c, unittest.TestCase)
            and c.__module__ == __name__]
    return unittest.TestSuite([c(n) for c in sorted(here, key=lambda c: c.__name__) for n in sorted(vars(c))
                               if n.startswith("test_") and callable(vars(c)[n])])


class StopCheckerTest(unittest.TestCase):
    """Review 2026-11-26 (A B3): `land cancel` stopped a queued entry's checker with a bare os.kill -- no guard against
    its own pid (supersede had one) and no OSError caught: a checker ending between the lock read and the kill made
    the cancel die with ProcessLookupError after the entry was already cancelled. One stop_checker for both."""

    def test_a_checker_gone_before_the_kill_is_no_crash_and_a_cancel_never_stops_itself(self):
        dead = subprocess.Popen(["true"])
        dead.wait()
        names = ("set_change", "cancel_entry_remote", "lock_held", "read_record")
        saved = {n: getattr(A, n) for n in names}
        killed = []
        real_kill = os.kill
        try:
            A.set_change = lambda *a, **k: None
            A.cancel_entry_remote = lambda *a, **k: None
            A.lock_held = lambda path: True
            for pid in (dead.pid, os.getpid()):
                A.read_record = lambda path, pid=pid: {"pid": pid}
                os.kill = lambda p, sig: (killed.append(p), real_kill(p, sig))
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(A.cancel_queued(None, "/nowhere", {"id": "e-1"}), 0)
        finally:
            os.kill = real_kill
            for n, v in saved.items():
                setattr(A, n, v)
        self.assertEqual(killed, [dead.pid])


if __name__ == "__main__":
    unittest.main()
