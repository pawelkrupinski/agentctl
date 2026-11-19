"""agentctl land (the owner, 2026-10-27): every change runs its OWN checks first, in parallel within the check slots;
then, main green, it pushes; main red, a production change joins ONE `waiting` branch by cherry-pick and land returns at
WAITING. At green the processor rebuilds `waiting` on main, runs ONE fast check of the batch (compile + fast tests),
pushes it in one push, bisects a failing batch by prefix, and sends every verdict to the inbox."""
import json, os, stat, sys, time, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A, Base, StallSamples, sh  # noqa: E402

REPO_HOOK = """#!/bin/sh
# supports --checks-only (the checks without the red gate) and --fast-only (compile and the fast tests)
echo "$*" >> "{calls}"
if git ls-files | grep -q BROKEN; then echo "[error] BROKEN is in the tree"; exit 1; fi
if git ls-files | grep -q PartA && git ls-files | grep -q PartB; then echo "[error] PartA and PartB disagree"; exit 1; fi
exit 0
"""


def assert_queued_line_names_no_verdict(test, out):
    """land's QUEUED / QUEUED-LOCAL line names no verdict it has not reached (2026-11-16: its "LANDED / WAITING / NOT
    LANDED goes to `agentctl inbox`" read as LANDED to a watcher matching the word): the only verdict word is QUEUED."""
    [line] = [x for x in out.splitlines() if x.startswith(("QUEUED ", "QUEUED-LOCAL "))]
    test.assertNotRegex(line.split(" ", 1)[1], r"\b(LANDED|WAITING|NOT LANDED)\b", line)


class WaitingTest(Base):
    def setUp(self):
        super().setUp()
        t = self.tmp.name
        self.saved = (A.WAITING_DIR, A.gate_reds, A.spawn_waiting_processor, A.await_entry,
                      A.time.sleep, A.watch_landed)
        A.WAITING_DIR = os.path.join(t, "waiting-state")
        self.reds = {"7"}
        A.gate_reds = lambda repo: set(self.reds)
        A.spawn_waiting_processor = lambda repo: None
        self.watches = []
        self.saved_start_watch = A.start_watch
        A.start_watch = lambda repo, sha, pushed_at, **kw: self.watches.append(sha)
        os.environ["AGENTCTL_CHECKS_DIR"] = os.path.join(t, "checks")
        self.saved_inbox = os.environ.get("AGENTCTL_INBOX_DIR")
        os.environ["AGENTCTL_INBOX_DIR"] = os.path.join(t, "inbox")
        self.awaited = []
        A.await_entry = lambda repo, eid, close=None: (self.awaited.append(eid), (9, None, None))[1]
        A.time.sleep = lambda s: None
        A.watch_landed = lambda repo, sha, pushed_at, o_r=None, close=None, fixes=(): 0
        os.environ.pop("PREPUSH_SKIP", None)
        self.calls, self.pushes = os.path.join(t, "hook-calls"), os.path.join(t, "pushes")
        hooks = os.path.join(t, "hooks")
        os.makedirs(hooks)
        with open(os.path.join(hooks, "pre-push"), "w") as f:
            f.write(f'#!/bin/sh\ncat > /dev/null\necho push >> "{self.pushes}"\n')
        os.chmod(os.path.join(hooks, "pre-push"), 0o755)
        sh(self.r.path, "git", "config", "core.hooksPath", hooks)
        self.r.write("scripts/hooks/pre-push", REPO_HOOK.format(calls=self.calls))
        os.chmod(os.path.join(self.r.path, "scripts/hooks/pre-push"), 0o755)
        self.r.write("web/src/main/Shared.scala", "line 1\n")
        self.r.commit("the repo's hook and a shared source")
        self.r.push()
        for p in (self.pushes, self.calls):
            if os.path.exists(p):
                os.remove(p)

    def tearDown(self):
        (A.WAITING_DIR, A.gate_reds, A.spawn_waiting_processor, A.await_entry, A.time.sleep,
         A.watch_landed) = self.saved
        A.start_watch = self.saved_start_watch
        os.environ.pop("AGENTCTL_CHECKS_DIR", None)
        if self.saved_inbox is None:
            os.environ.pop("AGENTCTL_INBOX_DIR", None)
        else:
            os.environ["AGENTCTL_INBOX_DIR"] = self.saved_inbox
        for k in ("AGENTCTL_LAND_STALL_MIN",):
            os.environ.pop(k, None)
        super().tearDown()

    def agent(self, name, path, text, msg=None):
        wt = self.r.worktree(f"wt-{name}", branch=name)
        self.r.write(path, text, at=wt)
        return wt, self.r.commit(msg or f"{name}: {path}", at=wt)

    def land(self, wt, *extra):
        return self.run_tool("land", *extra, cwd=wt)

    def remote_log(self):
        return sh(self.r.root, "git", "--git-dir", self.r.bare, "log", "--format=%s", "main").splitlines()

    def pushes_count(self):
        return len(open(self.pushes).read().split()) if os.path.exists(self.pushes) else 0

    def inbox(self):
        A.project_events()  # as the processor's loop does each cycle (step 7: the events are the inbox's source)
        return A.inbox_entries()

    def entries(self):
        d, _c, _w = A.waiting_state(self.r.path)
        return {e["id"].rsplit("-", 1)[-1]: e for e in A.waiting_entries(d, open_only=False)}

    def drive(self, n=10):
        """The batch to rest, as the processor's ticks drive it, its executors run in-process (step 8)."""
        d, _c, _w = A.waiting_state(self.r.path)
        saved = A.spawn_batch_executor
        A.spawn_batch_executor = lambda repo, d_, bid: A.check_batch(repo, bid)
        out = []
        try:
            for _ in range(n):
                did = A.close_landed_changes(self.r.path, d) + A.tick_batch(self.r.path, d)  # the server's order
                out += did
                if not did or did == ["held"]:
                    break
        finally:
            A.spawn_batch_executor = saved
        A.project_events()
        return out

    def batch(self):
        d, _c, _w = A.waiting_state(self.r.path)
        return A.current_batch(A.Store(), d)

    def calls_of(self, mode):
        return [l for l in open(self.calls).read().splitlines() if l.startswith(mode)] if os.path.exists(self.calls) else []

    def test_network_shaped_failure_on_change_and_main_is_infra_requeue_not_a_known_red(self):
        """2026-10-29 ~18:50 (#823): fetch-gitops reached neither Forgejo nor GitHub, on the change and on plain main;
        land recorded "Local checks" as a KNOWN RED. Now: no reds/local.json, the change re-queued as infra."""
        self.reds = set()
        self.r.write("scripts/hooks/pre-push",
                     "#!/bin/sh\n# --checks-only\n"
                     "echo '    fetch-gitops: git@github.com:o/movies-gitops.git unreachable'\n"
                     "echo \"    fatal: unable to access 'https://github.com/o/g.git/': Could not resolve host: github.com\"\n"
                     "echo '! pre-push GitOps manifests STALE: infra/bin/fetch-gitops could not fetch movies-gitops main'\n"
                     "echo '! pre-push sbt FAILED (3s)'\nexit 1\n")
        os.chmod(os.path.join(self.r.path, "scripts/hooks/pre-push"), 0o755)
        self.r.commit("a hook that cannot reach the network")
        self.r.push()
        wt, sha = self.agent("net", "web/src/main/N.scala", "n\n")
        before = sh(self.r.root, "git", "--git-dir", self.r.bare, "rev-parse", "main")
        outs = lambda: set(os.listdir(A.reds_dir("baseline-out"))) if os.path.isdir(A.reds_dir("baseline-out")) else set()  # noqa: E731
        had = outs()  # the worker's state root outlives a test: another land test's baseline may be there already
        t0 = time.time()
        rc, out = self.land(wt)
        self.assertEqual(rc, 0, out)
        self.assertNotIn("KNOWN RED", out)
        self.assertRegex(out, r"QUEUED \S+ \([0-9a-f]{10}\): infra, not a known red")
        assert_queued_line_names_no_verdict(self, out)
        self.assertFalse(os.path.exists(A.reds_dir("local.json")), "a network failure is never main's known red")
        self.assertEqual(outs() - had, set(), "no baseline of a network failure")
        (e,) = self.entries().values()
        self.assertEqual((e["status"], e["head"], e["infra_reason"], e["infra_same"]),
                         ("queued", sha, A.NETWORK_REASON, 1))
        # judged from before the land, never from now: the test's own duration under load is no part of the backoff
        self.assertGreaterEqual(e["retry_after"], t0 + A.INFRA_BACKOFF[0], "checked again later, not now")
        self.assertEqual(sh(self.r.root, "git", "--git-dir", self.r.bare, "rev-parse", "main"), before, "nothing landed")

    def test_green_main_lands_directly_after_its_own_checks(self):
        self.reds = set()
        wt, sha = self.agent("a", "web/src/main/A.scala", "a\n")
        rc, out = self.land(wt)
        self.assertEqual(rc, 0, out)
        self.assertIn(f"LANDED {sha}", out)
        self.assertEqual(len(self.calls_of("--range")), 1)
        self.assertEqual(self.watches, [sha], "the watch is detached")
        self.assertFalse(self.entries())

    def test_red_main_own_checks_first_then_join_by_cherry_pick_and_one_fast_check_and_push_at_green(self):
        wt1, s1 = self.agent("a", "web/src/main/A.scala", "a\n")
        wt2, s2 = self.agent("b", "worker/src/main/B.scala", "b\n")
        rc, out = self.land(wt1)
        self.assertEqual(rc, 0, out)
        self.assertRegex(out, r"WAITING [0-9a-f]{40} \(entry \S+-1, 1 waiting\)")
        rc, out = self.land(wt2)
        self.assertRegex(out, r"WAITING [0-9a-f]{40} \(entry \S+-2, 2 waiting\)")
        self.assertEqual(len(self.calls_of("--checks-only")), 2, "each change's own checks ran first, without the gate")
        self.assertEqual(self.pushes_count(), 0)
        self.drive()  # still red: nothing moves
        self.assertEqual(self.calls_of("--fast-only"), [])
        self.reds = set()
        self.drive()
        self.assertEqual(len(self.calls_of("--fast-only")), 1, "ONE fast check of the rebuilt batch")
        self.assertEqual(self.calls_of("--range"), [], "no full check of the batch: each passed its own")
        self.assertEqual(self.pushes_count(), 1, "ONE push")
        self.assertEqual(self.remote_log()[:2], ["b: worker/src/main/B.scala", "a: web/src/main/A.scala"])
        e = self.entries()
        self.assertEqual([e["1"]["status"], e["2"]["status"]], ["landed", "landed"])
        self.assertEqual(sorted(x["cwd"] for x in self.inbox() if x["verdict"].startswith("LANDED")), sorted([wt1, wt2]))
        self.assertEqual(len(self.watches), 2)

    def test_a_failing_own_check_never_joins(self):
        wt, _ = self.agent("b", "web/src/main/BROKEN.scala", "b\n")
        rc, out = self.land(wt)
        self.assertEqual(rc, 1, out)
        self.assertIn("NOT LANDED: the pre-push checks failed", out)
        self.assertFalse(self.entries())

    def test_tools_only_and_a_fix_land_directly_while_red(self):
        wt, sha = self.agent("t", "scripts/x.sh", "echo\n")
        rc, out = self.land(wt)
        self.assertIn(f"LANDED {sha}", out)
        wf, fsha = self.agent("f", "web/src/main/Fix.scala", "fix\n", msg="f: fix\n\nFixes-Red: 7")
        rc, out = self.land(wf)
        self.assertIn(f"LANDED {fsha}", out)
        self.assertFalse(self.entries())

    def test_fixes_red_entries_go_first_in_the_batch(self):
        wa, _ = self.agent("a", "web/src/main/A.scala", "a\n")
        self.land(wa)
        d, _c, wt = A.waiting_state(self.r.path)
        e = {"id": "x-9", "seq": 9, "worktree": wa, "head": "", "commits": [], "fixes": ["7"], "status": "waiting"}
        self.assertEqual([x["id"] for x in A.batch_order(self.r.path, A.waiting_entries(d) + [e], {"7"})][0], "x-9")

    def test_a_conflict_is_found_when_the_batch_is_assembled_and_rejects_that_change_alone(self):
        """Step 8: waiting is no branch; a change that no longer applies is rejected when the batch is assembled."""
        wt1, _ = self.agent("a", "web/src/main/Shared.scala", "line 1 by a\n")
        wt2, _ = self.agent("b", "web/src/main/Shared.scala", "line 1 by b\n")
        self.land(wt1)
        rc, out = self.land(wt2)
        self.assertEqual(rc, 0, out)  # it waits; nothing is cherry-picked at join
        self.reds = set()
        self.drive()
        e = self.entries()
        self.assertEqual([e["1"]["status"], e["2"]["status"]], ["landed", "rejected"])
        self.assertIn("no longer applies", e["2"]["why"])
        self.assertEqual([x["cwd"] for x in self.inbox() if x["verdict"].startswith("NOT LANDED")], [wt2])
        self.assertFalse(sh(self.r.path, "git", "branch", "--list", "waiting"), "no waiting branch")

    def test_a_failing_batch_rejects_only_the_culprit_by_prefix_and_lands_the_rest(self):
        wts = [self.agent("a", "web/src/main/PartA.scala", "a\n")[0], self.agent("b", "web/src/main/PartB.scala", "b\n")[0],
               self.agent("c", "web/src/main/C.scala", "c\n")[0]]
        for wt in wts:
            rc, out = self.land(wt)
            self.assertEqual(rc, 0, out)  # each passes its own checks
        self.reds = set()
        self.drive()
        e = self.entries()
        self.assertEqual([e["1"]["status"], e["2"]["status"], e["3"]["status"]], ["landed", "rejected", "landed"],
                         "the prefix lands, the culprit is rejected, the rest lands with the next batch")
        self.assertIn("PartA and PartB disagree", e["2"]["why"])
        self.assertEqual(self.remote_log()[:2], ["c: web/src/main/C.scala", "a: web/src/main/PartA.scala"])
        bad = [x for x in self.inbox() if x["verdict"].startswith("NOT LANDED")]
        self.assertEqual([x["cwd"] for x in bad], [wts[1]])

    def test_a_join_during_the_check_waits_for_the_next_batch(self):
        wa, _ = self.agent("a", "web/src/main/A.scala", "a\n")
        wb, _ = self.agent("b", "web/src/main/B.scala", "b\n")
        self.land(wa)
        self.reds = set()
        check, joined = A.batch_check, []

        def joining(repo, d, tree, base, tip, cls="check", *depth_runs, **kw):
            if not joined:
                self.reds = {"7"}  # red again for a moment: b joins while a's batch is being checked
                joined.append(self.land(wb))
                self.reds = set()
            return check(repo, d, tree, base, tip, cls, *depth_runs, **kw)
        A.batch_check = joining
        try:
            self.drive()
        finally:
            A.batch_check = check
        self.assertEqual(joined[0][0], 0, joined[0][1])
        self.assertEqual(self.remote_log()[:2], ["b: web/src/main/B.scala", "a: web/src/main/A.scala"],
                         "a's batch landed alone; b, joined during its check, landed with the next batch")
        self.assertEqual(self.pushes_count(), 2)

    def hung_sbt(self, hook_body):
        """The repo's hook calls scripts/sbt-warm, a fake whose command hangs like the sbtn of 2026-10-27; --stop logs;
        --seed (a cold check tree's seed) returns at once: a hanging seed cost each test its full 60 s sleep."""
        self.stops, hung = os.path.join(self.tmp.name, "sbt-stops"), os.path.join(self.tmp.name, "sbt-hung")
        # hung: the stall clock runs only while a hung sbt is there (a --stop ends it), never on the machine's load
        self.r.write("scripts/sbt-warm", f'#!/bin/sh\n[ "$1" = --stop ] && {{ echo stop >> "{self.stops}"; rm -f "{hung}"; '
                                         f'exit 0; }}\n[ "$1" = --seed ] && exit 0\ntouch "{hung}"\nsleep 60\n')
        StallSamples(hung=hung).install(self)
        os.chmod(os.path.join(self.r.path, "scripts/sbt-warm"), 0o755)
        self.r.write("scripts/hooks/pre-push", REPO_HOOK.format(calls=self.calls).replace("exit 0\n", hook_body + "\nexit 0\n"))
        os.chmod(os.path.join(self.r.path, "scripts/hooks/pre-push"), 0o755)
        self.r.commit("a hanging sbt")
        self.r.push()
        os.remove(self.pushes)

    def test_a_hung_sbt_in_the_batch_check_is_retried_on_a_fresh_server_then_held(self):
        self.hung_sbt('case "$1" in --fast-only) scripts/sbt-warm compile ;; esac')
        wa, _ = self.agent("a", "web/src/main/A.scala", "a\n")
        self.land(wa)
        self.reds = set()
        os.environ["AGENTCTL_LAND_STALL_MIN"] = "0.02"  # 1.2 s
        self.drive()
        self.assertEqual(len(self.calls_of("--fast-only")), 2, "a stalled check is retried once")
        self.assertEqual(len(open(self.stops).read().split()), 4, "on a FRESH server: stopped before and after each")
        self.assertEqual(self.entries()["1"]["status"], "waiting", "held, not rejected")
        self.assertEqual(self.pushes_count(), 0)
        self.assertEqual(self.batch()["state"], "held")
        self.assertTrue([x for x in A.Store().events() if x["kind"] == "batch.held" and x["severity"] == "escalate"])
        self.assertEqual(self.drive(), [], "held: nothing moves until its deadline")
        self.run_tool("waiting", "resume", cwd=self.r.path)
        self.assertEqual(self.batch()["state"], "assembling")

    def test_the_hooks_stall_signal_counts_as_a_stall(self):
        self.hung_sbt('case "$1" in --fast-only) echo "pre-push STALLED: sbt (no progress for 1200 s)"; exit 75 ;; esac')
        wa, _ = self.agent("a", "web/src/main/A.scala", "a\n")
        self.land(wa)
        self.reds = set()
        self.drive()
        self.assertEqual(len(self.calls_of("--fast-only")), 2)
        self.assertEqual(self.batch()["state"], "held")

    def test_a_batch_lands_with_no_land_process_alive_and_its_watch_verdict_reaches_the_inbox(self):
        A.start_watch = self.saved_start_watch  # the real watch item
        wa, _ = self.agent("a", "web/src/main/A.scala", "a\n")
        rc, out = self.land(wa)  # the agent's turn ends here: no land process waits on the entry
        self.assertEqual(rc, 0)
        self.reds = set()
        self.drive()  # the processor's ticks
        sha = self.entries()["1"]["landed_sha"]
        verdicts = {x["command"]: x for x in self.inbox() if x.get("final", True)}
        self.assertTrue(verdicts["land"]["verdict"].startswith(f"LANDED {sha}"))
        self.assertEqual(verdicts["land"]["cwd"], wa, "its author's inbox, no land process alive")

    def test_changes_waiting_at_green_with_no_batch_open_a_stalled_condition_that_clears_itself(self):
        """2026-10-30: two flips changes sat `waiting` at green with nothing said anywhere. Waiting at green while no
        batch checks, bisects, pushes or is held is counted per tick; past the limit a STALLED condition opens, and
        the same count clears it once the batch moves."""
        os.environ["AGENTCTL_WAITING_GREEN_TICKS"] = "3"
        self.addCleanup(os.environ.pop, "AGENTCTL_WAITING_GREEN_TICKS", None)
        wa, _ = self.agent("a", "web/src/main/A.scala", "a\n")
        self.land(wa)  # red: it waits
        d, _c, _w = A.waiting_state(self.r.path)
        self.reds = set()
        saved = A.check_tree_slot
        A.check_tree_slot = lambda repo, d_: None  # every check tree busy: the batch cannot be assembled
        try:
            for _ in range(2):
                A.tick_batch(self.r.path, d)
            self.assertEqual([c for c in A.open_conditions() if c["key"].startswith("waiting-green")], [])
            A.tick_batch(self.r.path, d)
            conds = [c for c in A.open_conditions() if c["key"].startswith("waiting-green")]
            self.assertEqual(len(conds), 1)
            self.assertIn("waiting while the gate is green for 3 ticks", conds[0]["text"])
            self.assertIn("waiting for a check tree", conds[0]["text"])
            self.assertFalse(A.run_probe("batch_progress", {"d": d}).ok)
        finally:
            A.check_tree_slot = saved
        self.drive()
        self.assertEqual(self.entries()["1"]["status"], "landed")
        self.assertTrue(A.run_probe("batch_progress", {"d": d}).ok)
        A.recovery_tick(force=True)
        self.assertEqual([c for c in A.open_conditions() if c["key"].startswith("waiting-green")], [])

    def test_waiting_while_red_or_while_the_batch_checks_is_no_stall(self):
        os.environ["AGENTCTL_WAITING_GREEN_TICKS"] = "2"
        self.addCleanup(os.environ.pop, "AGENTCTL_WAITING_GREEN_TICKS", None)
        wa, _ = self.agent("a", "web/src/main/A.scala", "a\n")
        self.land(wa)
        d, _c, _w = A.waiting_state(self.r.path)
        for _ in range(4):  # red: waiting is right
            A.tick_batch(self.r.path, d)
        self.reds = set()
        saved = A.spawn_batch_executor
        A.spawn_batch_executor = lambda repo, d_, bid: None  # its executor runs elsewhere: the batch stays checking
        try:
            for _ in range(4):
                A.tick_batch(self.r.path, d)
        finally:
            A.spawn_batch_executor = saved
        self.assertEqual(self.batch()["state"], "checking")
        self.assertEqual([c for c in A.open_conditions() if c["key"].startswith("waiting-green")], [])

    def test_status_on_the_laptop_shows_the_batch_ci_runner_keys_by_its_own_waiting_dir(self):
        """2026-10-30: batch var_lib_agentctl_movies_.git#4 checked for an hour; the laptop's `land status` looked
        under its own key and showed no batch, and its members as plain `waiting`."""
        wa, _ = self.agent("a", "web/src/main/A.scala", "a\n")
        self.land(wa)
        d, _c, _w = A.waiting_state(self.r.path)
        eid = A.waiting_entries(d)[0]["id"]
        A.project_changes(d)
        st = A.Store()
        it = st.put_item("batch", "var_lib_agentctl_movies_.git#4", "checking", {"retries": 0})
        st.update_item("batch", it["id"], it["version"], {"members": [eid], "tip": "340057e4d3c7", "base": "24e95ec1a217"})
        rc, out = self.run_tool("land", "status", cwd=self.r.path)
        self.assertIn("batch var_lib_agentctl_movies_.git#4: checking, 1 change(s) at 340057e4d3", out)
        self.assertIn(f"{eid}  waiting", out)
        self.assertIn("(in batch var_lib_agentctl_movies_.git#4: checking)", out)

    def test_the_tick_stays_cheap_while_nothing_waits(self):
        d, _c, _w = A.waiting_state(self.r.path)
        saved = A.gate_reds
        A.gate_reds = lambda repo: self.fail("an empty batch reads no gate")
        try:
            self.assertEqual(A.tick_batch(self.r.path, d), [])
        finally:
            A.gate_reds = saved

    def test_land_follow_still_waits_and_watches(self):
        A.await_entry = lambda repo, eid, close=None: (print("LANDED abc (with …)"), (0, "abc", 1.0))[1]
        watched = []
        A.watch_landed = lambda repo, sha, pushed_at, o_r=None, close=None, fixes=(): (watched.append(sha), 3)[1]
        wt, _ = self.agent("a", "web/src/main/A.scala", "a\n")
        rc, out = self.land(wt, "--follow")
        self.assertEqual(rc, 3, out)
        self.assertEqual(watched, ["abc"])

    def test_status_pause_and_cancel(self):
        wt1, _ = self.agent("a", "web/src/main/A.scala", "a\n")
        self.land(wt1)
        self.run_tool("land", "hold", "--why", "deploy window", cwd=self.r.path)
        self.assertEqual(self.drive(), ["held"])
        rc, out = self.run_tool("land", "status", cwd=self.r.path)
        self.assertIn("HELD until", out)
        self.assertIn("deploy window", out)
        self.run_tool("land", "release", cwd=self.r.path)
        self.assertIsNone(A.land_hold(A.Store()))
        rc, out = self.run_tool("land", "cancel", "-1", cwd=self.r.path)
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.entries()["1"]["status"], "cancelled")

    def test_a_hold_is_the_stores_and_expires_by_itself(self):
        """LOCAL MODE (this process is the reconciler): `land hold` writes the store's hold item, never a file; the tick
        honours it; past its --for it expires (an event says so) and the batch moves again with no release."""
        wt1, _ = self.agent("a", "web/src/main/A.scala", "a\n")
        self.land(wt1)
        d, _c, _w = A.waiting_state(self.r.path)
        rc, out = self.run_tool("land", "hold", "--for", "30", "--why", "infra deploy", cwd=self.r.path)
        self.assertEqual(rc, 0, out)
        self.assertFalse(os.path.exists(os.path.join(d, "PAUSED")), "no local file: the store holds it")
        [h] = A.Store().items("hold", ["held"])
        self.assertEqual(h["data"]["why"], "infra deploy")
        self.assertAlmostEqual(h["deadline"] - time.time(), 30 * 60, delta=60)
        self.assertEqual(self.drive(), ["held"])
        A.write_snapshot(d)
        line = A.status_line()
        self.assertIn("LAND HELD until", line)
        self.assertIn("infra deploy", line)
        self.assertIn("infra deploy", A.status_landing(A.Store(), time.time())["hold"]["why"])
        # a second hold extends the one in force (one hold, never two)
        self.run_tool("land", "hold", "--for", "60", cwd=self.r.path)
        self.assertEqual(len(A.Store().items("hold", ["held"])), 1)
        # past its deadline: the next reader expires it, with an event; the tick moves the batch again
        later = time.time() + 61 * 60
        self.assertIsNone(A.land_hold(A.Store(), later))
        self.assertEqual(A.Store().items("hold", ["held"]), [])
        self.assertTrue([e for e in A.Store().events() if e["kind"] == "hold.expired"])
        self.assertNotEqual(self.drive(), ["held"])
        A.write_snapshot(d)
        self.assertNotIn("HELD", A.status_line())

    def test_an_unread_gate_holds_the_batch_and_says_so_once(self):
        # 08:05Z 10-27: the processor could not read the gate (no token under launchd), took it for green, and pushed
        wt1, _ = self.agent("a", "web/src/main/A.scala", "a\n")
        self.land(wt1)
        self.reds = {A.GATE_UNREAD}
        A.gate_unread_why[0] = "could not read the movies AGENTCTL_STORE_TOKEN from the vault"
        self.drive()
        self.drive()
        self.assertEqual(self.pushes_count(), 0, "an unread gate is never green")
        self.assertEqual(self.calls_of("--fast-only"), [])
        notes = [e for e in self.inbox() if e["verdict"].startswith("UNKNOWN: the waiting processor cannot read the gate")]
        self.assertEqual(len(notes), 1, "said once per unread spell")
        self.assertEqual(notes[0]["kind"], "UNKNOWN")
        rc, out = self.run_tool("waiting", "status", cwd=self.r.path)
        self.assertIn("gate: UNREAD, taken as red", out)
        self.assertNotIn("green", out)
        self.reds = set()
        self.drive()
        self.assertEqual(self.pushes_count(), 1, "read green again: the batch lands")

    def test_land_with_an_unread_gate_joins_waiting(self):
        self.reds = {A.GATE_UNREAD}
        wt, _ = self.agent("a", "web/src/main/A.scala", "a\n")
        rc, out = self.land(wt)
        self.assertEqual(rc, 0, out)
        self.assertIn("WAITING", out)
        self.assertEqual(self.pushes_count(), 0)

    def test_an_agents_land_on_the_laptop_needs_no_token_and_never_runs_secrets(self):
        """2026-10-31: agents' `land` still died NoToken: a `Fixes-Red:` trailer read the reds from ci-runner's store,
        the vault was locked for the agent's session, and the NoToken escaped fix_check. The laptop's land never runs
        `secrets` (even in a Claude session, which may elsewhere); with no token the change goes to the outbox, and the
        sync, which holds one, submits it."""
        import threading
        srv_state = os.path.join(self.tmp.name, "ci-runner")
        server_store = A.Store(path=os.path.join(srv_state, "store.db"))
        server_repo = os.path.join(srv_state, "movies")
        sh(self.tmp.name, "git", "clone", "-q", self.r.bare, server_repo)
        api = A.StoreAPI(server_store, ["pawel"], [], user_of={"tok-pawel": "pawel"}.get, repo=server_repo)
        srv = A.store_server(api, "127.0.0.1", 0)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        url = f"http://127.0.0.1:{srv.server_port}"
        marker = os.path.join(self.tmp.name, "secrets-ran")
        fake = os.path.join(self.tmp.name, "secrets")
        with open(fake, "w") as f:
            f.write(f"#!/bin/sh\ntouch {marker}\necho vault-token-0123456789\n")
        os.chmod(fake, 0o755)
        keys = ("AGENTCTL_STORE_URL", "AGENTCTL_STORE_TOKEN", "AGENTCTL_QUEUE", "AGENTCTL_ROLE", "CLAUDECODE",
                "CLAUDE_CODE_SESSION_ID", "GH_TOKEN", "AGENTCTL_STORE_TOKEN", "AGENTCTL_SOCK_DIR", "AGENTCTL_MOVIES_REPO")
        saved, saved_fb = {k: os.environ.get(k) for k in keys}, A.SECRETS_FALLBACKS

        def restore():
            A.SECRETS_FALLBACKS = saved_fb
            for k, v in saved.items():
                os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
        self.addCleanup(restore)
        for k in ("AGENTCTL_STORE_TOKEN", "AGENTCTL_ROLE", "GH_TOKEN", "AGENTCTL_STORE_TOKEN"):
            os.environ.pop(k, None)
        A.SECRETS_FALLBACKS = (fake,)  # an agent's session would find `secrets`: land must not run it
        import tempfile
        os.environ.update(AGENTCTL_STORE_URL=url, AGENTCTL_QUEUE="1", CLAUDECODE="1", AGENTCTL_MOVIES_REPO=self.r.path,
                          CLAUDE_CODE_SESSION_ID="5b1e0c3a-agent-session",
                          AGENTCTL_SOCK_DIR=tempfile.mkdtemp(dir=os.environ["AGENTCTL_TEST_RUN_SOCK"], prefix="s-"))
        self.r.write("scripts/hooks/pre-push", REPO_HOOK.format(calls=self.calls) + "# nothing on refs/heads/land/*\n")
        self.r.commit("the hook skips land refs")
        self.r.push()
        sh(server_repo, "git", "pull", "-q")
        wt, sha = self.agent("fixer", "web/src/main/F.scala", "f\n", msg="f: the fix\n\nFixes-Red: 7")
        rc, out = self.land(wt)
        self.assertEqual(rc, 0, out)
        self.assertIn("QUEUED-LOCAL", out)
        assert_queued_line_names_no_verdict(self, out)
        self.assertIn("the sync submits it with its own token", out)
        self.assertNotIn("NoToken", out)
        self.assertFalse(os.path.exists(marker), "the laptop's land never runs `secrets`")
        [p] = os.listdir(A.sync_outbox_dir())
        self.assertEqual(json.load(open(os.path.join(A.sync_outbox_dir(), p)))["fixes"], ["7"],
                         "unchecked here (no store): the reconciler checks the trailer again at the push")
        os.environ["AGENTCTL_STORE_TOKEN"] = "tok-pawel"  # the sync's own token (the server thread's Store() too)
        self.assertEqual(A.flush_outbox(), 1)
        [e] = A.queued_entries(A.waiting_state(server_repo)[0])
        self.assertEqual((e["worktree"], e["commits"]), (wt, [sha]))

    def test_the_token_is_handed_to_a_listening_processor(self):
        import tempfile
        sockdir = tempfile.mkdtemp(dir=os.environ["AGENTCTL_TEST_RUN_SOCK"], prefix="s-")
        saved = {k: os.environ.get(k) for k in ("AGENTCTL_SOCK_DIR", "AGENTCTL_STORE_TOKEN", "GH_TOKEN")}
        os.environ["AGENTCTL_SOCK_DIR"] = sockdir
        os.environ.pop("GH_TOKEN", None)
        os.environ.pop("AGENTCTL_STORE_TOKEN", None)
        saved_slug, saved_token = A.slug, A.token
        A.slug = lambda repo: "pawelkrupinski/movies"
        try:
            d, _c, _w = A.waiting_state(self.r.path)
            drop = A.TokenDrop(d, "pawelkrupinski/movies").open()
            self.assertEqual(oct(os.stat(drop.path).st_mode & 0o777), "0o600")
            A.token = lambda o_r: "tok-0123456789abcdef"
            self.assertTrue(A.offer_token(self.r.path))
            drop.nap(0.5)
            self.assertEqual(os.environ.get("AGENTCTL_STORE_TOKEN"), "tok-0123456789abcdef")
            drop.close()
            self.assertFalse(os.path.exists(drop.path))
            self.assertFalse(A.offer_token(self.r.path), "nobody listens: nothing handed over")
        finally:
            A.slug, A.token = saved_slug, saved_token
            for k, v in saved.items():
                os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)

    def test_a_detached_child_is_handed_the_token_in_its_environment(self):
        saved = A.token
        A.token = lambda o_r: "tok-abcdef0123456789"
        try:
            env = A.token_env("pawelkrupinski/movies", {})
            self.assertEqual(env, {"AGENTCTL_STORE_TOKEN": "tok-abcdef0123456789"})

            def none(o_r):
                raise A.NoToken("locked")
            A.token = none
            self.assertEqual(A.token_env("pawelkrupinski/movies", {"X": "1"}), {"X": "1"})
        finally:
            A.token = saved

    def test_service_install_status_uninstall_with_a_fake_launchctl(self):
        t = self.tmp.name
        log = os.path.join(t, "launchctl.log")
        fake = os.path.join(t, "launchctl")
        with open(fake, "w") as f:
            f.write(f'#!/bin/sh\necho "$*" >> {log}\n[ "$1" = print ] && {{ echo "    state = running"; echo "    pid = 4242"; }}\nexit 0\n')
        os.chmod(fake, 0o755)
        os.environ.update(AGENTCTL_LAUNCHCTL=fake, AGENTCTL_LAUNCH_AGENTS=os.path.join(t, "LaunchAgents"))
        try:
            rc, out = self.run_tool("waiting", "service", "install", cwd=self.r.path)
            self.assertEqual(rc, 0, out)
            plist = open(os.path.join(t, "LaunchAgents", "net.bitcashier.agentctl-waiting.plist")).read()
            for needle in ("<key>KeepAlive</key>", "<key>RunAtLoad</key>", "<string>--service</string>", self.r.path,
                           "service.log"):
                self.assertIn(needle, plist)
            self.assertIn("bootstrap gui/", open(log).read())
            rc, out = self.run_tool("waiting", "service", "status", cwd=self.r.path)
            self.assertIn("running, pid 4242", out)
            rc, out = self.run_tool("waiting", "service", "uninstall", cwd=self.r.path)
            self.assertFalse(os.path.exists(os.path.join(t, "LaunchAgents", "net.bitcashier.agentctl-waiting.plist")))
            self.assertIn("bootout gui/", open(log).read())
        finally:
            for k in ("AGENTCTL_LAUNCHCTL", "AGENTCTL_LAUNCH_AGENTS"):
                os.environ.pop(k, None)

    def test_the_queue_points_at_land(self):
        rc, out = self.run_tool("queue", "add", cwd=self.r.path)
        self.assertEqual(rc, 2)
        self.assertIn("agentctl land status", out)

    def test_await_reads_the_store_so_an_entry_ci_runner_runs_is_awaited_on_the_laptop(self):
        """2026-11-16 (round 4): `agentctl waiting await <entry>` on the laptop crashed with FileNotFoundError on an
        entry ci-runner's queue runs: it read the entry's JSON from this host's waiting dir, which only the host that
        runs the queue has. It reads the change's store item now (a RemoteStore on the laptop), whatever host holds
        the entry: the landed sha and push time its verdict wrote, or a NOT LANDED with its why."""
        import contextlib, io
        await_entry = self.saved[3]
        st = A.Store()
        d = A.waiting_state(self.r.path)[0]
        for eid, verdict, status, fields in (
                ("e-landed", "LANDED abc123 (batch: 1 change(s) in one push)", "landed",
                 {"landed_sha": "abc123", "pushed_at": 1234.5}),
                ("e-rejected", "NOT LANDED: the checks failed with your change in the batch", "rejected", {})):
            e = {"id": eid, "seq": 1, "status": "waiting", "worktree": self.r.path}
            st.put_item("change", eid, "waiting", A.change_item_fields(e))  # the queue's host made it
            ticks = []

            def decide(_s, e=e, verdict=verdict, status=status, fields=fields):  # the queue's host decides it ...
                ticks.append(1)
                A.queue_verdict(d, dict(e), status, verdict, code=0 if status == "landed" else 1, **fields)
                os.unlink(os.path.join(d, "entries", f"{e['id']}.json"))  # ... its entry on ITS disk, not this one
            A.time.sleep = decide
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                got = await_entry(self.r.path, eid)
            self.assertEqual(ticks, [1])
            if status == "landed":
                self.assertEqual(got, (0, "abc123", 1234.5))
                self.assertTrue(buf.getvalue().startswith("LANDED abc123"), buf.getvalue())
            else:
                self.assertEqual(got, (1, None, None))
                self.assertIn("NOT LANDED: the checks failed with your change in the batch", buf.getvalue())

    def test_await_on_a_superseded_change_says_superseded_and_exits_0(self):
        """Review round 6: the superseded change's event says SUPERSEDED with exit 0 (its newer entry lands the change),
        but `waiting await` printed `NOT LANDED: <bare reason>` and exited 1, so a `land --follow` read it as a failure."""
        import contextlib, io
        await_entry = self.saved[3]
        st = A.Store()
        d = A.waiting_state(self.r.path)[0]
        e = {"id": "e-old", "seq": 1, "status": "waiting", "worktree": self.r.path}
        st.put_item("change", "e-old", "waiting", A.change_item_fields(e))
        A.time.sleep = lambda _s: A.supersede(d, dict(e), "e-new", "a newer land of the same worktree")
        A.cancel_entry_remote, saved = (lambda *a, **k: None), A.cancel_entry_remote
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                got = await_entry(self.r.path, "e-old")
        finally:
            A.cancel_entry_remote = saved
        self.assertEqual(got, (0, None, None))
        self.assertTrue(buf.getvalue().startswith("SUPERSEDED by e-new: a newer land of the same worktree"),
                        buf.getvalue())

    def test_await_says_what_it_waits_on_when_the_store_does_not_answer(self):
        """Review round 6: a store outage was polled silently for up to 24 h; each change of what it waits on (the
        store not answering, answering again, the change's state) is said once, as it happens."""
        import contextlib, io
        await_entry = self.saved[3]
        d = A.waiting_state(self.r.path)[0]
        e = {"id": "e-blip", "seq": 1, "status": "waiting", "worktree": self.r.path}
        A.Store().put_item("change", "e-blip", "waiting", A.change_item_fields(e))
        real, calls = A.Store, []

        class Flaky:
            def __init__(self, *a, **k):
                self.st = real(*a, **k)

            def __getattr__(self, name):
                return getattr(self.st, name)

            def item(self, typ, iid):
                calls.append(1)
                if len(calls) in (2, 3):  # the awaiter's 2nd and 3rd looks
                    raise A.StoreUnavailable("connection refused")
                return self.st.item(typ, iid)
        polls = []

        def tick(_s):
            polls.append(1)
            if len(polls) == 4:
                A.queue_verdict(d, dict(e), "landed", "LANDED abc123 (batch: 1 change(s) in one push)", code=0,
                                landed_sha="abc123", pushed_at=1.0)
        A.time.sleep = tick
        A.Store = Flaky
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
                got = await_entry(self.r.path, "e-blip")
        finally:
            A.Store = real
        self.assertEqual(got, (0, "abc123", 1.0))
        out = buf.getvalue()
        self.assertEqual(out.count("the store did not answer"), 1, out)
        self.assertIn("connection refused", out)
        self.assertIn("e-blip: it is waiting", out)
        self.assertTrue(out.rstrip().splitlines()[-1].startswith("LANDED abc123"), out)


if __name__ == "__main__":
    unittest.main()
