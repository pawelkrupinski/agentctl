"""THE LAND QUEUE (the owner, 2026-10-27: "agents never wait on a land"): `land` rebases, guards and returns at once with
QUEUED <id>; the processor's checker runs the change's checks in a check tree of its own, within the governor, then
pushes it (green), puts it on `waiting` (red) or rejects it, every verdict to the inbox with the agent's worktree; under
pressure the checks run on ci-runner's check tier and the push carries the remote run for the hook to confirm."""
import json, os, subprocess, sys, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A, sh  # noqa: E402
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
        self.saved_q = (A.gov_pressure, A.remote_check)
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
        A.gov_pressure, A.remote_check = self.saved_q
        os.environ["AGENTCTL_QUEUE"] = "0"
        super().tearDown()

    def queued(self, wt):
        rc, out = self.land(wt)
        self.assertEqual(rc, 0, out)
        self.assertRegex(out, r"QUEUED \S+-\d+ \([0-9a-f]{10}")
        eid = out.split("QUEUED ", 1)[1].split()[0]
        return eid, out

    def check(self, eid):
        return A.check_entry(self.r.path, eid)

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
        sh(other, "git", "push", "-q", "forgejo", "HEAD:main")
        self.assertEqual(self.check(eid), 0)
        self.assertEqual(self.remote_log()[:2], ["a: one", "b: two"])

    def test_a_main_that_keeps_moving_lands_it_on_the_last_round_through_the_push_hook(self):
        wt, _ = self.agent("a", "web/src/main/A.scala", "a\n", msg="a: one")
        eid, _ = self.queued(wt)
        real, n = A.run_entry_checks, []

        def moving(repo, d, e, tree, base, head, mode, cls, log_path, own=None):
            n.append(1)
            # the same sbt module (web) each time: an overlap, so each move means a re-check
            other, _ = self.agent(f"m{len(n)}", f"web/src/main/M{len(n)}.scala", "m\n", msg=f"main moves {len(n)}")
            sh(other, "git", "push", "-q", "forgejo", "HEAD:main")
            return real(repo, d, e, tree, base, head, mode, cls, log_path, own)
        A.run_entry_checks = moving
        self.addCleanup(setattr, A, "run_entry_checks", real)
        self.assertEqual(self.check(eid), 0)
        self.assertEqual(len(n), A.QUEUE_ROUNDS)
        self.assertEqual(self.remote_log()[:2], ["a: one", f"main moves {A.QUEUE_ROUNDS}"])
        self.assertIn("by the push's hook", [e for e in self.inbox() if e["kind"] == "LANDED"][0]["verdict"])

    def moves_once(self, *moves):
        """Main moves during the first round's checks: one commit per (path, text) landed by another agent."""
        real, n = A.run_entry_checks, []

        def moving(repo, d, e, tree, base, head, mode, cls, log_path, own=None):
            n.append(1)
            if len(n) == 1:
                for i, (path, text) in enumerate(moves):
                    other, _ = self.agent(f"m{i}", path, text, msg=f"main moves {i}")
                    sh(other, "git", "push", "-q", "forgejo", "HEAD:main")
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
        sh(other, "git", "push", "-q", "forgejo", "HEAD:main")
        self.assertEqual(self.check(eid), 1)
        self.assertIn("no longer applies", [e for e in self.inbox() if e["kind"] == "NOT_LANDED"][0]["verdict"])

    def test_under_pressure_the_checks_run_on_ci_runner_and_the_push_names_the_run(self):
        self.r.write(".github/workflows/precommit-check.yml", "name: Pre-commit check\n")
        self.r.commit("the remote check workflow")
        self.r.push()
        os.remove(self.pushes)
        wt, _ = self.agent("a", "web/src/main/A.scala", "a\n")
        eid, _ = self.queued(wt)
        A.gov_pressure = lambda: "memory pressure 82%"
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
        self.assertIn("on ci-runner (memory pressure 82%)", [e for e in self.inbox() if e["kind"] == "LANDED"][0]["verdict"])

    def test_a_remote_check_without_a_verdict_is_checked_locally(self):
        self.r.write(".github/workflows/precommit-check.yml", "name: Pre-commit check\n")
        self.r.commit("the remote check workflow")
        self.r.push()
        os.remove(self.pushes)
        wt, _ = self.agent("a", "web/src/main/A.scala", "a\n")
        eid, _ = self.queued(wt)
        A.gov_pressure = lambda: "load 40 on 14 cores"
        A.remote_check = lambda repo, e, base, head, mode, own=None: (None, "no run started")
        self.assertEqual(self.check(eid), 0)
        self.assertEqual(len(self.calls_of("--range")), 1)
        self.assertEqual(self.pushed(), ["push remote="])

    def fake_forgejo(self, status, head):
        calls = []

        def api(o_r, method, path, params=None):
            calls.append((method, path))
            if path == "actions/runs":
                return {"workflow_runs": [{"id": 99, "commit_sha": head, "prettyref": "check/e1"}]}
            if path == "actions/runs/99":
                return {"id": 99, "index_in_repo": 7, "status": status, "html_url": "http://f/runs/7"}
            return None
        saved = (A.api, A.fetch_failed_log, A.slug)
        A.api, A.slug = api, (lambda repo: "o/movies")
        A.fetch_failed_log = lambda o_r, rid: "check\trun\t[error] BROKEN is in the tree\n"
        self.addCleanup(lambda: setattr(A, "api", saved[0]) or setattr(A, "fetch_failed_log", saved[1])
                        or setattr(A, "slug", saved[2]))
        return calls

    def test_remote_check_pushes_a_check_branch_reads_the_verdict_and_deletes_the_branch(self):
        head = sh(self.r.path, "git", "rev-parse", "HEAD").strip()
        calls = self.fake_forgejo("success", head)
        ok, out = A.remote_check(self.r.path, {"id": "e1"}, "base", head, "--checks-only")
        self.assertTrue(ok, out)
        self.assertIn("check/e1", sh(self.r.root, "git", "--git-dir", self.r.bare, "branch", "--list", "check/*"),
                      "pushed for the run (deleted through the API, faked here)")
        self.assertIn(("DELETE", "branches/check%2Fe1"), calls)
        calls = self.fake_forgejo("failure", head)
        ok, out = A.remote_check(self.r.path, {"id": "e1"}, "base", head, "--checks-only")
        self.assertFalse(ok)
        self.assertIn("[error] BROKEN is in the tree", out)
        self.assertIn(("DELETE", "branches/check%2Fe1"), calls)

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
        for _ in range(A.QUEUE_INFRA_RETRIES):
            self.check(eid)
        self.assertEqual(self.entries()[eid.rsplit("-", 1)[-1]]["status"], "rejected", "judged once retries run out")

    def test_the_hook_is_given_the_remotes_ref_so_it_reads_forgejos_gate(self):
        wt, _ = self.agent("a", "web/src/main/A.scala", "a\n")
        eid, _ = self.queued(wt)
        self.check(eid)
        self.assertRegex(self.calls_of("--range")[0], r"^--range forgejo/main\.\.[0-9a-f]{40}$")

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


class Died(BaseException):
    """A checker that dies mid-wait (SIGKILL: no handler runs past this point in the wait loop)."""


class RemotePickupTest(QueueTest):
    """2026-10-27: entry -45's remote run 3344 finished green yet the entry showed `checking ... remote run 3344` for an
    hour: the run belonged to the checker process (cancelled or orphaned when it ended), a replacement re-dispatched from
    scratch, and the saved remote_run was never cleared. Now the run is the ENTRY's: saved at once, adopted by a
    replacement checker, re-derived by the processor from Forgejo every cycle."""

    def setUp(self):
        super().setUp()
        self.r.write(".github/workflows/precommit-check.yml", "name: Pre-commit check\n")
        self.r.commit("the remote check workflow")
        self.r.push()
        os.remove(self.pushes)
        A.gov_pressure = lambda: "load 99 on 14 cores"
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
            if path == "actions/runs":
                return {"workflow_runs": [{"id": 99, "commit_sha": ref.split(":")[0],
                                           "prettyref": ref.split("refs/heads/")[1]} for ref in st["branch_pushes"]]}
            if path == "actions/runs/99":
                st["polls"] += 1
                return {"id": 99, "index_in_repo": 7, "status": st["status"], "html_url": "http://f/runs/7"}
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
        d, _c, _w = A.waiting_state(self.r.path)
        A.reconcile_checking(self.r.path, d)
        A.time.sleep = lambda s: None
        self.assertEqual(self.check(eid), 0)
        self.assertEqual(len(self.state["branch_pushes"]), 1, "adopted: no second check/ push, no second run")
        self.assertEqual(self.pushed(), ["push remote=", "push remote=99"], "the check/ branch, then main with the run")
        self.assertEqual(self.calls_of("--range"), [], "nothing checked on the laptop")
        e = self.entry(eid)
        self.assertEqual((e["status"], e.get("attempts"), e.get("adoptions")), ("landed", 1, 1))

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

    def test_the_processor_stops_a_checker_that_does_not_collect_a_decided_run(self):
        d, e = self.checking_entry()
        own = A.entry_owned(e["id"])
        holder = subprocess.Popen([sys.executable, "-c", "import fcntl,sys,time; f=open(sys.argv[1],'a+'); "
                                   "fcntl.flock(f, fcntl.LOCK_EX); print('held', flush=True); time.sleep(120)",
                                   own.lockpath], stdout=subprocess.PIPE, text=True)
        self.addCleanup(holder.kill)
        holder.stdout.readline()
        with open(own.record, "w") as f:
            json.dump({"pid": holder.pid}, f)
        st = A.Store()
        lease = st.acquire(f"check/{e['id']}", "the stranded checker", 600, 3600, meta={"pid": holder.pid})
        self.state["status"] = "success"
        t = A.time.time()
        self.assertEqual(A.reconcile_checking(self.r.path, d, now_=t), [], "decided just now: its checker collects it")
        self.assertEqual(A.reconcile_checking(self.r.path, d, now_=t + 60), [])
        self.assertEqual(A.reconcile_checking(self.r.path, d, now_=t + A.REMOTE_COLLECT_GRACE + 1),
                         [("stopped", e["id"])])
        self.assertEqual(holder.wait(10), -15, "the stranded checker got a SIGTERM")
        st.release(f"check/{e['id']}", lease["epoch"], lease["secret"])  # it died: its lease lapses (here at once)
        self.assertEqual(A.reconcile_checking(self.r.path, d, now_=t + A.REMOTE_COLLECT_GRACE + 2), [], "stopped once")
        self.assertEqual(self.check(e["id"]), 0, "the replacement adopts the verdict")
        self.assertEqual(self.pushed(), ["push remote=", "push remote=99"], "the check/ branch, then main with the run")

    def test_overdue_and_unresolvable_escalates_once(self):
        d, e = self.checking_entry(since_min=2 * A.REMOTE_EXPECTED_MIN + 5)
        self.state["fail_api"] = True
        self.assertEqual(A.reconcile_checking(self.r.path, d), [("escalated", e["id"])])
        self.assertEqual(A.reconcile_checking(self.r.path, d), [], "once per round")
        stalled = [x for x in self.inbox() if x["kind"] == "STALLED"]
        self.assertEqual(len(stalled), 1)
        self.assertIn(e["id"], stalled[0]["verdict"])
        self.assertIn("cannot be read", stalled[0]["verdict"])

    def test_overdue_but_progressing_is_not_escalated(self):
        d, e = self.checking_entry(since_min=2 * A.REMOTE_EXPECTED_MIN + 5)
        self.assertEqual(A.reconcile_checking(self.r.path, d), [], "the run is still running and readable")
        self.assertEqual([x for x in self.inbox() if x["kind"] == "STALLED"], [])

    def test_superseding_an_entry_cancels_its_run(self):
        d, e = self.checking_entry()
        A.supersede(d, e, "newer", "queued again")
        self.assertEqual(self.state["cancelled"], [99])

    def test_a_new_round_clears_the_old_runs_record(self):
        d, e = self.checking_entry()
        other, _ = self.agent("b", "worker/src/main/B.scala", "b\n", msg="b: two")
        sh(other, "git", "push", "-q", "forgejo", "HEAD:main")
        A.gov_pressure = lambda: None
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


def load_tests(loader, tests, pattern):
    """Only this file's own cases (QueueTest borrows WaitingTest's fixture, not its tests)."""
    return unittest.TestSuite([QueueTest(n) for n in sorted(vars(QueueTest)) if n.startswith("test_")]
                              + [RemotePickupTest(n) for n in sorted(vars(RemotePickupTest)) if n.startswith("test_")])


if __name__ == "__main__":
    unittest.main()
