"""THE LAND QUEUE (the owner, 2026-10-27: "agents never wait on a land"): `land` rebases, guards and returns at once with
QUEUED <id>; the processor's checker runs the change's checks in a check tree of its own, within the governor, then
pushes it (green), puts it on `waiting` (red) or rejects it, every verdict to the inbox with the agent's worktree; under
pressure the checks run on ci-runner's check tier and the push carries the remote run for the hook to confirm."""
import json, os, subprocess, sys, time, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A, StallSamples, sh  # noqa: E402
from test_waiting import REPO_HOOK, WaitingTest  # noqa: E402

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
esac
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
        return eid, out

    def check(self, eid, rounds=6):
        """The change to its outcome, as the processor drives it: the executor's checks, then the tick's push
        (step 6: they are two steps now), round after round while main moves. 1 when it ends rejected."""
        d, _c, _w = A.waiting_state(self.r.path)
        for _ in range(rounds):
            e = A.read_record(os.path.join(d, "entries", f"{eid}.json"))
            if e["status"] in ("queued", "checking") and not e.get("retry_after", 0) > A.time.time():
                A.check_entry(self.r.path, eid)
            elif e["status"] == "checked":
                A.push_change(self.r.path, d, e)
            else:
                break
        A.project_events()  # the processor's projector: the events become the inbox entries
        e = A.read_record(os.path.join(d, "entries", f"{eid}.json"))
        return 1 if e["status"] == "rejected" else 0

    def pushed(self):
        return open(self.pushes).read().splitlines() if os.path.exists(self.pushes) else []

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

    def test_a_solo_change_waits_while_the_batch_checks_and_lands_after_it(self):
        """2026-10-30: batch #4 passed its 45-min check, but a solo change had pushed meanwhile, so it was assembled
        again; then the change tick took its freed check tree for another change: the batch never landed. While the
        batch is open, a checked change does not push, and no change takes a tree the batch needs."""
        def entry(eid):
            return A.read_record(os.path.join(d, "entries", f"{eid}.json"))

        def tick():
            started = []
            saved = A.spawn_executor
            A.spawn_executor = lambda repo, d_, eid: started.append(eid)
            try:
                return A.tick_changes(self.r.path, d), started
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
            self.assertIn("checking", A.tick_batch(self.r.path, d)[0])
        finally:
            A.spawn_batch_executor = saved
        wb, _ = self.agent("b", "worker/src/main/B.scala", "b\n", msg="b: solo")
        eb, _ = self.queued(wb)
        A.check_entry(self.r.path, eb)
        self.assertEqual(entry(eb)["status"], "checked")
        did, _started = tick()
        self.assertIn(f"{eb}: its push waits for batch", " ".join(did))
        self.assertEqual(entry(eb)["status"], "checked", "no solo push under the checking batch")
        self.assertEqual(self.remote_log()[0], "the hook's plans")
        st = A.Store()
        bt = self.batch()
        st.transition("batch", bt["id"], "pushing", bt["version"])  # passed: its tree is about to be pushed from
        wc, _ = self.agent("c", "worker/src/main/C.scala", "c\n", msg="c: queued")
        ec, _ = self.queued(wc)
        _did, started = tick()
        self.assertNotIn(ec, started, "no change check takes a tree the batch needs")
        self.drive()  # the batch pushes
        self.assertEqual(entry(entry(ea)["waiting_id"])["status"], "landed")
        self.assertEqual(self.remote_log()[0], "a: waits", "the batch first")
        _did, started = tick()
        self.assertIn(ec, started, "the batch landed: changes go again")
        tick()  # b: main moved by the batch, no overlap: pushed
        self.assertEqual(entry(eb)["status"], "landed")

    def test_red_main_the_checked_change_joins_waiting(self):
        self.reds = {"7"}
        wt, sha = self.agent("a", "web/src/main/A.scala", "a\n")
        eid, _ = self.queued(wt)
        self.assertEqual(self.check(eid), 0)
        self.assertEqual(len(self.calls_of("--checks-only")), 1)
        es = self.entries()
        self.assertEqual(es[eid.rsplit("-", 1)[-1]]["status"], "joined")
        waiting = [e for e in es.values() if e["status"] == "waiting"]
        self.assertEqual([e["worktree"] for e in waiting], [wt], "the waiting entry is the agent's")
        self.assertEqual(self.pushed(), [])
        self.assertIn("WAITING", [e["kind"] for e in self.inbox()])

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

        def moving(repo, d, e, tree, base, head, mode, cls, log_path, own=None):
            n.append(1)
            if len(n) <= 2:  # the same sbt module (web): an overlap, so each move means a new round
                other, _ = self.agent(f"m{len(n)}", f"web/src/main/M{len(n)}.scala", "m\n", msg=f"main moves {len(n)}")
                sh(other, "git", "push", "-q", "origin", "HEAD:main")
            return real(repo, d, e, tree, base, head, mode, cls, log_path, own)
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

        def moving(repo, d, e, tree, base, head, mode, cls, log_path, own=None):
            n.append(1)
            if len(n) == 1:
                for i, (path, text) in enumerate(moves):
                    other, _ = self.agent(f"m{i}", path, text, msg=f"main moves {i}")
                    sh(other, "git", "push", "-q", "origin", "HEAD:main")
            return real(repo, d, e, tree, base, head, mode, cls, log_path, own)
        A.run_entry_checks = moving
        self.addCleanup(setattr, A, "run_entry_checks", real)
        return n

    def landed_verdict(self):
        return [e for e in self.inbox() if e["kind"] == "LANDED"][0]["verdict"]

    def test_disjoint_commits_landing_meanwhile_push_without_a_recheck(self):
        wt, _ = self.agent("a", "web/src/main/A.scala", "a\n", msg="a: one")
        eid, _ = self.queued(wt)
        rounds = self.moves_once(("worker/src/main/W.scala", "w\n"), ("docs/notes.md", "n\n"))
        self.assertEqual(self.check(eid), 0)
        self.assertEqual(len(rounds), 1, "checked once")
        self.assertEqual(len(self.calls_of("--range")), 1)
        self.assertEqual(self.remote_log()[0], "a: one", "rebased onto the moved main and pushed")
        self.assertIn("main moved by 2 commits, no overlap: pushed without re-check", self.landed_verdict())
        self.assertEqual(self.pushed()[-1], "push remote= skip=sbt identity", "the push skips the checks already passed")

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

    def test_a_module_the_change_depends_on_rechecks(self):
        wt, _ = self.agent("a", "worker/src/main/A.scala", "a\n", msg="a: one")
        eid, _ = self.queued(wt)
        rounds = self.moves_once(("common/src/main/C.scala", "c\n"))  # worker depends on common
        self.assertEqual(self.check(eid), 0)
        self.assertEqual(len(rounds), 2)
        self.assertEqual(self.pushed()[-1], "push remote=")

    def rechecked_when_landing(self, path, text):
        wt, _ = self.agent("a", "docs/A.md", "a\n", msg="a: one")
        eid, _ = self.queued(wt)
        rounds = self.moves_once((path, text))
        self.assertEqual(self.check(eid), 0)
        self.assertEqual(len(rounds), 2, f"{path} landed meanwhile: checked again")
        self.assertEqual(self.remote_log()[0], "a: one")
        self.assertNotIn("without re-check", self.landed_verdict())

    def test_a_change_to_build_sbt_rechecks(self):
        self.rechecked_when_landing("build.sbt", "lazy val x = 1\n")

    def test_a_change_to_the_hook_rechecks(self):
        hook = open(os.path.join(self.r.path, "scripts/hooks/pre-push")).read()
        self.rechecked_when_landing("scripts/hooks/pre-push", hook + "# a change to the hook\n")

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

        def remote(repo, e, base, head, mode, own=None):
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
        A.remote_check = lambda repo, e, base, head, mode, own=None: (None, "no run started")
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
        A.remote_check = lambda repo, e, base, head, mode, own=None: (None, "no run started")
        self.check(eid, rounds=1)  # the tests run as ci-runner (AGENTCTL_ROLE=server)
        self.assertEqual(self.calls_of("--range"), [], "nothing checked in a ci-runner tree")
        self.assertEqual(self.pushed(), [])

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
        ok, out = A.remote_check(self.r.path, {"id": "e1"}, "base", head, "--checks-only")
        self.assertTrue(ok, out)
        self.assertIn("check/e1", sh(self.r.root, "git", "--git-dir", self.r.bare, "branch", "--list", "check/*"),
                      "pushed for the run (deleted through the API, faked here)")
        self.assertIn(("DELETE", "git/refs/heads/check/e1"), calls)
        calls = self.fake_github("failure", head)
        ok, out = A.remote_check(self.r.path, {"id": "e1"}, "base", head, "--checks-only")
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
            return A.read_record(os.path.join(d, "entries", f"{eid}.json"))
        d, _c, _w = A.waiting_state(self.r.path)
        self.reds = {"7"}
        wa, _ = self.agent("a", "web/src/main/A.scala", "a\n", msg="a: the fix\n\nFixes-Red: 7")
        self.r.write("web/src/main/A2.scala", "a2\n", at=wa)
        self.r.commit("a: beside it, a production change naming no red", at=wa)
        ea, _ = self.queued(wa)
        self.check(ea)
        wid = entry(ea)["waiting_id"]
        self.assertEqual(entry(wid)["fixes"], ["7"], "the entry names the red")
        for _ in range(2):  # the second tick: it stays blocked, never flips back to assembling
            A.tick_batch(self.r.path, d)
            self.assertEqual(self.batch()["state"], "blocked-red", "nothing in it may land on red")
        self.assertIsNone(A.batch_goes_first(A.Store(), d))
        wb, _ = self.agent("b", "scripts/b.sh", "echo b\n", msg="b: tools only")
        eb, _ = self.queued(wb)
        self.assertEqual(self.check(eb), 0)
        self.assertEqual(entry(eb)["status"], "landed", "a change that may land on red is not held by the batch")
        self.assertEqual(self.remote_log()[0], "b: tools only")
        self.reds = set()  # green: the batch builds
        self.drive()
        self.assertEqual(entry(wid)["status"], "landed")

    def test_the_batchs_check_on_ci_runner_is_the_fast_only_set_on_a_check_fast_branch(self):
        head = sh(self.r.path, "git", "rev-parse", "HEAD").strip()
        calls = self.fake_github("success", head, ref="check/fast-batch-x")
        ok, out = A.remote_check(self.r.path, {"id": "batch-x"}, "base", head, "--fast-only")
        self.assertTrue(ok, out)
        self.assertIn(("DELETE", "git/refs/heads/check/fast-batch-x"), calls)  # the branch names the mode for the workflow
        seen, saved = [], (A.remote_check, A.remote_checks_supported, A.precheck_hook, A.batch_mode)
        A.remote_check = lambda repo, e, base, tip, mode, own=None: seen.append((e["id"], mode)) or (True, "ok")
        A.remote_checks_supported = lambda tree: True
        A.precheck_hook = lambda tree: "/hook"
        A.batch_mode = lambda hook: "--fast-only"
        try:  # the server role (the harness's): its batch check goes to the CI tier
            self.assertEqual(A.batch_check(self.r.path, self.r.path, self.r.path, "b" * 40, "c" * 40), (True, "ok", False))
        finally:
            A.remote_check, A.remote_checks_supported, A.precheck_hook, A.batch_mode = saved
        self.assertEqual(seen, [("batch-cccccccccc", "--fast-only")])

    def test_a_check_the_laptop_failed_is_queued_again_not_rejected(self):
        wt, _ = self.agent("a", "web/src/main/A.scala", "a\n")
        eid, _ = self.queued(wt)
        real = A.run_entry_checks
        A.run_entry_checks = lambda *a, **k: (False, "sbt server did not start within 300 seconds\n[error] failed to "
                                                      "connect to server", "locally")
        self.addCleanup(setattr, A, "run_entry_checks", real)
        self.assertEqual(self.check(eid), 0)
        e = self.entries()[eid.rsplit("-", 1)[-1]]
        self.assertEqual((e["status"], e["infra_retries"]), ("queued", 1))
        self.assertGreater(e["retry_after"], A.time.time())
        self.assertEqual([x for x in self.inbox() if x["kind"] == "NOT_LANDED"], [])
        d, _c, _w = A.waiting_state(self.r.path)
        for _ in range(A.QUEUE_INFRA_RETRIES):
            e = A.read_record(os.path.join(d, "entries", f"{eid}.json"))
            e["retry_after"] = 0
            A.save_entry(d, e)
            self.check(eid, rounds=1)
        e = self.entries()[eid.rsplit("-", 1)[-1]]
        self.assertEqual((e["status"], e["infra_retries"]), ("queued", 1 + A.QUEUE_INFRA_RETRIES),
                         "infra never rejects a change (design §5.1)")
        evs = [x for x in A.Store().events() if x["item_id"] == eid and x["severity"] == "escalate"]
        self.assertTrue(evs, "from the third infra re-queue on, each one escalates")

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
        self.assertEqual(A.sweep_land_refs(self.r.path, d), [], "queued: its ref stays")
        self.assertEqual(self.check(eid), 0)  # landed
        A._last_ref_sweep[0] = 0
        self.assertEqual(A.sweep_land_refs(self.r.path, d), [f"refs/heads/land/{eid}: deleted"])
        self.assertEqual(self.remote_refs(), [])

    def test_sweep_keeps_the_ref_of_a_change_only_the_store_holds_open(self):
        """Phase 2: the change lives in the store, not in entries/ -- its land ref must survive the sweep."""
        saved = A.land_refs_supported
        A.land_refs_supported = lambda repo: True
        self.addCleanup(setattr, A, "land_refs_supported", saved)
        wt, sha = self.agent("a", "web/src/main/A.scala", "a\n")
        eid, _out = self.queued(wt)
        d, _c, _w = A.waiting_state(self.r.path)
        os.remove(os.path.join(d, "entries", f"{eid}.json"))  # held by the store alone
        A.Store().put_item("change", eid, "queued", {"worktree": wt, "head": sha})
        A._last_ref_sweep[0] = 0
        self.assertEqual(A.sweep_land_refs(self.r.path, d), [], "open in the store: its ref stays")
        self.assertEqual(self.remote_refs(), [f"refs/heads/land/{eid}"])
        # unknown to both: kept while young, deleted (and logged) past 24 h
        sh(self.r.path, "git", "push", "--quiet", "origin", f"{sha}:refs/heads/land/20200101-000000-x-aaaaaa",
           f"{sha}:refs/heads/land/{time.strftime('%Y%m%d-%H%M%S')}-y-bbbbbb")
        A._last_ref_sweep[0] = 0
        self.assertEqual(A.sweep_land_refs(self.r.path, d),
                         ["refs/heads/land/20200101-000000-x-aaaaaa: unknown to entries and store, %d h old" % (
                             A.entry_id_age("20200101-000000", time.time()) // 3600),
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
        saved = A.cmd_waiting

        def unreachable(a):
            raise A.StoreUnavailable("POST /v1/changes: Connection refused")
        A.cmd_waiting = unreachable
        try:
            rc, out = self.run_spawned(wt)
        finally:
            A.cmd_waiting = saved
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
        A.time.sleep = sleep

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
        A.time.sleep = lambda s: None
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
        A.time.sleep = sleep
        self.assertEqual(self.check(eid), 0)
        self.assertEqual(len(polls), 3)
        self.assertEqual(len(self.state["branch_pushes"]), 1)
        self.assertEqual(self.pushed(), ["push remote=", "push remote=99"], "the check/ branch, then main with the run")

    def test_a_terminate_is_never_swallowed_by_the_outage_handler(self):
        wt, _ = self.agent("a", "web/src/main/A.scala", "a\n")
        eid, _ = self.queued(wt)
        real = A.remote_poll

        def poll(o_r, rem):
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
        A.time.sleep = lambda s: None
        d, _c, _w = A.waiting_state(self.r.path)
        e = self.entry(eid)
        if since_min:
            stamp = A.dt.datetime.fromtimestamp(A.time.time() - since_min * 60, A.dt.timezone.utc)
            e["round_since"] = stamp.strftime("%Y-%m-%dT%H:%M:%SZ")
            A.save_entry(d, e)
        return d, e

    def tick(self, d):
        started = []
        saved = A.spawn_executor
        A.spawn_executor = lambda repo, d_, eid: started.append(eid)
        try:
            return A.tick_changes(self.r.path, d), started
        finally:
            A.spawn_executor = saved

    def test_a_checker_that_stops_progressing_loses_its_lease_and_the_tick_starts_one_that_adopts_its_run(self):
        """Step 6 (reconcile_checking deleted): a wedged checker stops renewing check/<entry>; once the lease lapses
        the tick starts a replacement, which adopts the decided run."""
        d, e = self.checking_entry()
        st = A.Store()
        got = st.acquire(f"check/{e['id']}", "a wedged checker", 600, 3600)
        self.assertEqual(self.tick(d)[1], [], "a live checker: nothing started")
        st.release(f"check/{e['id']}", got["epoch"], got["secret"])  # its lease lapsed (here at once)
        self.assertEqual(self.tick(d)[1], [e["id"]])
        self.state["status"] = "success"
        self.assertEqual(self.check(e["id"]), 0, "the replacement adopts the verdict")
        self.assertEqual(self.pushed(), ["push remote=", "push remote=99"], "the check/ branch, then main with the run")

    def test_checking_past_its_deadline_with_no_checker_is_queued_again_as_infra(self):
        d, e = self.checking_entry()
        st = A.Store()
        st.put_item("change", e["id"], "checking")
        self.tick(d)  # in time: unchanged
        self.assertEqual(self.entry(e["id"])["status"], "checking")
        saved = A.spawn_executor
        A.spawn_executor = lambda *a: None
        try:
            A.tick_changes(self.r.path, d, now_=A.time.time() + 3 * 3600)
        finally:
            A.spawn_executor = saved
        e2 = self.entry(e["id"])
        self.assertEqual((e2["status"], e2["infra_retries"]), ("queued", 1), "infra, never a rejection")

    def test_superseding_an_entry_cancels_its_run(self):
        d, e = self.checking_entry()
        A.supersede(d, e, "newer", "queued again")
        self.assertEqual(self.state["cancelled"], [99])

    def test_a_new_round_clears_the_old_runs_record(self):
        d, e = self.checking_entry()
        other, _ = self.agent("b", "worker/src/main/B.scala", "b\n", msg="b: two")
        sh(other, "git", "push", "-q", "origin", "HEAD:main")
        saved_rc = A.remote_check
        A.remote_check = lambda repo, e_, base, head, mode, own=None: (True, "precommit-check.yml: success")
        self.addCleanup(setattr, A, "remote_check", saved_rc)
        seen = []
        real = A.run_entry_checks

        def spy(repo, d_, e_, *a, **k):
            seen.append(A.read_record(os.path.join(d_, "entries", f"{e_['id']}.json")))
            return real(repo, d_, e_, *a, **k)
        A.run_entry_checks = spy
        self.addCleanup(setattr, A, "run_entry_checks", real)
        self.assertEqual(self.check(e["id"]), 0)
        self.assertNotIn("remote_run", seen[0], "main moved: the old run's record is gone from the saved entry")
        self.assertEqual(self.state["cancelled"], [99], "the old, undecided run is cancelled")
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


def load_tests(loader, tests, pattern):
    """Only this file's own cases (QueueTest borrows WaitingTest's fixture, not its tests)."""
    return unittest.TestSuite([QueueTest(n) for n in sorted(vars(QueueTest)) if n.startswith("test_") and vars(QueueTest)[n]]
                              + [RemotePickupTest(n) for n in sorted(vars(RemotePickupTest))
                                 if n.startswith("test_") and vars(RemotePickupTest)[n]]
                              + [FastPrecheckTest(n) for n in sorted(vars(FastPrecheckTest))
                                 if n.startswith("test_") and vars(FastPrecheckTest)[n]])


if __name__ == "__main__":
    unittest.main()
