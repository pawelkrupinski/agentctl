"""FIX REPLAY (2026-11-19, Alert backtest 5254bffda9d2: seven fixes landed and each was refuted by the next backtest run).
A workflow whose `.github/red-workflows.tsv` row names a replay holds its fixers to it: a Fixes-Red: landing for its
red must carry `Fix-Replay: <command> base=FAIL head=PASS` -- outside the gate a WARNING only (fix-replay-outside).
FOR GATE REDS (the owner, 2026-11-20: "3 approve and enforce for gate reds"): a Fixes-Red: for a red of Main, Identity
model convergence or Identity takes must carry `Fix-Replay: <command>`, which agentctl runs at origin/main and at the
fix (base=FAIL head=PASS), or `Fix-Replay: none — <a specific reason>`; land refuses it otherwise."""
import os, sys, tempfile, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A, Repo, approved  # noqa: E402
from fake_clock import FakeClock, on_clock  # noqa: E402

REGISTRY = ("# workflow\tverify_dispatch\treplay\twhy\n"
            "Alert backtest\tyes\tinfra/bin/backtest-alerts-replay --from-run {run}\t~30 min on ci-runner\n"
            "Record scrape fixtures\tno\t-\tpaid proxies\n")
CMD = "infra/bin/backtest-alerts-replay --from-run 44459271435"


class FixReplayTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        t = os.path.realpath(self.tmp.name)
        saved = os.environ.get("AGENTCTL_STATE_ROOT")
        os.environ["AGENTCTL_STATE_ROOT"] = os.path.join(t, "state")
        self.addCleanup(lambda: os.environ.pop("AGENTCTL_STATE_ROOT") if saved is None
                        else os.environ.__setitem__("AGENTCTL_STATE_ROOT", saved))
        self.r = Repo(t)
        self.r.write(A.RED_WORKFLOWS_FILE, REGISTRY)
        self.r.commit("the registry")
        self.r.push()
        self.base = A.git(self.r.path, "rev-parse", "HEAD").strip()
        self.st = A.Store(path=os.path.join(t, "store.db"))
        self.st.put_item("red", "5254bffda9d2", "offered", {
            "key": "5254bffda9d2", "workflow": "Alert backtest", "jobs": ["backtest"], "runs": ["5", "44459271435"],
            "last_run": {"number": 5, "databaseId": 44459271435}, "red_kind": "outside"})

    def fix(self, trailers):
        self.r.write("infra/test/alert-backtest-allowlist.yml", f"{trailers}\n")
        self.r.commit("backtest: judge each alert from its rollout\n\n" + trailers)
        return A.waiting_commits(self.r.path, self.base, "HEAD")

    def test_the_registry_is_one_file_for_dispatch_and_replay(self):
        p = A.red_workflow_policies(self.r.path)
        self.assertEqual(p["Alert backtest"]["replay"], "infra/bin/backtest-alerts-replay --from-run {run}")
        self.assertEqual((p["Record scrape fixtures"]["replay"], p["Record scrape fixtures"]["verify_dispatch"]), (None, False))
        self.assertTrue(A.verify_dispatchable(self.r.path, "Alert backtest"))
        self.assertFalse(A.verify_dispatchable(self.r.path, "Record scrape fixtures"))
        self.assertEqual(A.red_replay_command(self.r.path, self.st.item("red", "5254bffda9d2")["data"]), CMD)

    def test_a_fixes_red_landing_without_its_replay_is_refused_naming_the_command(self):
        # d6bfac055, Alert backtest #5's 7th fix: Fixes-Red: 5 and no replay
        pid, why = A.fix_replay_refusal(self.r.path, self.fix("Fixes-Red: 5"), self.st)
        self.assertEqual(pid, "fix-replay-outside", "a red outside the gate: warn-only")
        self.assertIn(f"run `{CMD}` at origin/main and at your fix", why)
        self.assertIn(f"`Fix-Replay: {CMD} base=FAIL head=PASS`", why)

    def test_a_replay_that_did_not_pass_at_head_is_refused(self):
        _pid, why = A.fix_replay_refusal(self.r.path, self.fix(f"Fixes-Red: 5\nFix-Replay: {CMD} base=FAIL head=FAIL"),
                                         self.st)
        self.assertIn("this push says base=FAIL head=FAIL", why)

    def test_base_fail_head_pass_lands(self):
        self.assertIsNone(A.fix_replay_refusal(self.r.path, self.fix(f"Fixes-Red: 5\nFix-Replay: {CMD} base=FAIL head=PASS"),
                                               self.st))

    def test_a_red_with_no_replay_or_no_fixes_red_needs_none(self):
        self.st.put_item("red", "50bc15cd7369", "fix-pending", {
            "key": "50bc15cd7369", "workflow": "Record scrape fixtures", "runs": ["198"], "last_run": {"number": 198}})
        self.assertIsNone(A.fix_replay_refusal(self.r.path, self.fix("Fixes-Red: 198"), self.st))
        self.assertIsNone(A.fix_replay_refusal(self.r.path, self.fix("Refs: none"), self.st))

    def drop_registry(self):
        A.git(self.r.path, "rm", "-q", A.RED_WORKFLOWS_FILE)
        self.r.commit("main without the registry")
        self.r.push()
        self.base = A.git(self.r.path, "rev-parse", "HEAD").strip()

    def test_a_registry_missing_from_main_refuses_a_fixes_red_landing(self):
        # 8cc38bce6, Alert backtest #7's fix: landed 13:39Z with Fixes-Red: 7 and no replay, 12 minutes before the
        # registry reached main (f1a5e431d) -- agentctl had enforced since 08:15Z against a file main did not have yet,
        # and an absent file read as "no workflow has a replay"
        self.drop_registry()
        _pid, why = A.fix_replay_refusal(self.r.path, self.fix("Fixes-Red: 5"), self.st) or (None, None)
        self.assertIn(f"{A.RED_WORKFLOWS_FILE} is missing on origin/main", why or "")

    def test_a_repo_that_keeps_no_registry_needs_none(self):
        conf = os.path.join(self.tmp.name, "red-repos.json")
        with open(conf, "w") as f:
            f.write('{"repos": [{"repo": "pawelkrupinski/movies", "gate": [["Main", "main.yml"]]}]}')
        saved = os.environ.get("AGENTCTL_RED_REPOS")
        os.environ["AGENTCTL_RED_REPOS"] = conf
        self.addCleanup(lambda: os.environ.pop("AGENTCTL_RED_REPOS") if saved is None
                        else os.environ.__setitem__("AGENTCTL_RED_REPOS", saved))
        self.drop_registry()
        self.assertIsNone(A.fix_replay_refusal(self.r.path, self.fix("Fixes-Red: 5"), self.st))

    def test_land_refuses_it_before_queueing(self):
        self.fix("Fixes-Red: 5")
        sh = lambda *args: A.subprocess.run(args, cwd=self.r.path, capture_output=True, text=True)  # noqa: E731
        import io, contextlib
        out, errs = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(errs):
            rc, head = A.sync(self.r.path, "origin/main", sh, FakeClock(), self.st)
        # a red OUTSIDE the gate: warn-only (the owner, 2026-11-20) -- it warns, never blocks
        self.assertIsNone(rc)
        self.assertIn("POLICY UNAPPROVED fix-replay-outside", errs.getvalue())
        with approved("fix-replay-outside"):  # were it ever approved, it refuses as it was written to
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc, head = A.sync(self.r.path, "origin/main", sh, FakeClock(), self.st)
        self.assertEqual((rc, head), (1, None))
        self.assertIn("NOT LANDED: Alert backtest #5 [backtest] has a replay", out.getvalue())

    def test_a_land_never_leaves_a_rebase_in_progress_and_refuses_one_it_finds(self):
        """Issue 20261119T162652-494caa: NOT LANDED with the rebase onto origin/main left in progress; the next land's
        abort reset the branch, dropping the commits made on the rebase's detached HEAD."""
        import io, contextlib
        sh = lambda *args: A.subprocess.run(args, cwd=self.r.path, capture_output=True, text=True)  # noqa: E731
        self.r.write("conflict.txt", "mine\n")
        self.r.commit("mine")
        mine = A.git(self.r.path, "rev-parse", "HEAD").strip()
        sh("git", "checkout", "-q", "-b", "other", self.base)
        self.r.write("conflict.txt", "theirs\n")
        self.r.commit("theirs")
        sh("git", "rebase", "-q", mine)  # stops on the conflict: a rebase in progress
        self.assertTrue(A.rebase_in_progress(self.r.path))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc, _ = A.sync(self.r.path, "origin/main", sh, FakeClock(), self.st)
        self.assertEqual(rc, 1)
        self.assertIn("a rebase is in progress", out.getvalue())
        self.assertTrue(A.rebase_in_progress(self.r.path), "never land's to abort: it would reset the branch")
        sh("git", "rebase", "--abort")
        real = A.sync_onto_main

        def leaves_one(repo, main_ref, sh_, clock, store, release_hold=False):  # a path that stops mid-rebase
            sh_("git", "rebase", "-q", mine)
            return 1, None
        A.sync_onto_main = leaves_one
        self.addCleanup(setattr, A, "sync_onto_main", real)
        with contextlib.redirect_stdout(io.StringIO()):
            A.sync(self.r.path, "origin/main", sh, FakeClock(), self.st)
        self.assertIsNone(A.rebase_in_progress(self.r.path), "a rebase land started is aborted on its way out")

    # ---------------------------------------------------------------- gate reds (the owner, 2026-11-20)
    def gate_red(self):
        self.st.put_item("red", "9d1e2a7c4b10", "offered", {
            "key": "9d1e2a7c4b10", "workflow": "Main", "jobs": ["unit (web)"], "runs": ["2600", "44470000001"],
            "last_run": {"number": 2600, "databaseId": 44470000001}, "red_kind": "gate", "gate": True})

    def test_a_gate_reds_fixes_red_without_a_fix_replay_is_refused(self):
        self.gate_red()
        pid, why = A.fix_replay_refusal(self.r.path, self.fix("Fixes-Red: 2600"), self.st)
        self.assertEqual(pid, "fix-replay")
        self.assertTrue(A.POLICIES["fix-replay"].approved, "the owner approved it for gate reds")
        self.assertIn("Main #2600 [unit (web)] is a gate red", why)
        self.assertIn("`Fix-Replay: <command>`", why)
        self.assertIn("`Fix-Replay: none — <why>`", why)

    def test_fix_replay_none_with_a_specific_reason_passes(self):
        self.gate_red()
        reason = "the runner's disk filled mid-job (infra); nothing in the code replays it"
        self.assertIsNone(A.fix_replay_refusal(self.r.path, self.fix(f"Fixes-Red: 2600\nFix-Replay: none — {reason}"),
                                               self.st))
        self.assertEqual(set(A.fix_replay_none_reasons(self.r.path, self.fix(f"Fixes-Red: 2600\nFix-Replay: none -- {reason}"))),
                         {reason}, "an ASCII dash too")

    def test_fix_replay_none_outside_the_final_trailer_block_is_no_escape(self):
        self.gate_red()
        reason = "the runner's disk filled mid-job (infra); nothing in the code replays it"
        pid, why = A.fix_replay_refusal(self.r.path, self.fix(f"Fix-Replay: none — {reason}\n\nFixes-Red: 2600\n"
                                                              "Co-Authored-By: A <a@b.c>"), self.st)
        self.assertEqual(pid, "fix-replay", "the weekly list reads git's trailers: a misplaced one would be unlisted")

    def test_fix_replay_none_with_a_vague_reason_is_refused(self):
        self.gate_red()
        for reason in ("too slow", "flaky test on the hosted runner, nothing to replay",
                       "n/a -- the failure was on the runner, not in code", "we can't replay this one on the laptop at all"):
            pid, why = A.fix_replay_refusal(self.r.path, self.fix(f"Fixes-Red: 2600\nFix-Replay: none — {reason}"),
                                            self.st)
            self.assertEqual(pid, "fix-replay", reason)
            self.assertIn("is no reason", why)

    def test_the_revert_fallbacks_fixes_red_passes_the_gates_fix_replay_rule(self):
        """The fixer's revert fallback (red_revert) is agentctl's own revert too: like blame's (test_blame, Main
        #2806-#2808) it carries a specific `Fix-Replay: none` so FIX REPLAY lets it land."""
        self.gate_red()
        self.r.write("web/broken.txt", "broken\n")
        self.r.commit("web: the culprit")
        self.r.push()
        culprit = A.git(self.r.path, "rev-parse", "HEAD").strip()
        lands = []
        saved = A.spawn_land
        A.spawn_land = lambda wt, env, log: lands.append(wt) or 4242
        self.addCleanup(setattr, A, "spawn_land", saved)
        ok, msg = A.red_revert("o/r", self.r.path, {"workflow": "Main", "run": {"number": 2600}}, culprit)
        self.assertTrue(ok, msg)
        [wt] = lands
        self.addCleanup(A.git, self.r.path, "worktree", "remove", "--force", wt, check=False)
        commits = A.waiting_commits(wt, "origin/main", "HEAD")
        self.assertIsNone(A.fix_replay_refusal(wt, commits, self.st))
        [reason] = A.fix_replay_none_reasons(wt, commits)
        self.assertIsNone(A.fix_replay_none_lint(reason), reason)
        self.assertIn(culprit[:10], reason)

    def test_agentctl_runs_the_replay_at_base_and_head(self):
        self.gate_red()
        self.r.write("marker.txt", "broken\n")
        self.r.commit("main: broken")
        self.r.push()
        base = A.git(self.r.path, "rev-parse", "HEAD").strip()
        self.r.write("marker.txt", "fixed\n")
        self.r.commit("the fix\n\nFixes-Red: 2600\nFix-Replay: grep -q fixed marker.txt")
        head = A.git(self.r.path, "rev-parse", "HEAD").strip()
        commits = A.waiting_commits(self.r.path, base, head)
        proofs = A.run_fix_replays(self.r.path, base, head, commits)
        self.assertEqual([(p["cmd"], p["base"], p["head"]) for p in proofs],
                         [("grep -q fixed marker.txt", "FAIL", "PASS")])
        self.assertIsNone(A.fix_replay_refusal(self.r.path, commits, self.st, proofs=proofs))
        self.assertEqual(A.git(self.r.path, "worktree", "list", "--porcelain").count("worktree "), 1,
                         "its throwaway worktrees are gone")
        # no proof at all (the server's check of a change the laptop never replayed): refused
        pid, why = A.fix_replay_refusal(self.r.path, commits, self.st, proofs=[])
        self.assertEqual(pid, "fix-replay")
        self.assertIn("agentctl has not run `grep -q fixed marker.txt`", why)
        # a replay that passes at base proves nothing
        self.r.write("marker.txt", "fixed already\n")
        self.r.commit("the fix\n\nFixes-Red: 2600\nFix-Replay: grep -q fixed marker.txt")
        again = A.waiting_commits(self.r.path, head, "HEAD")
        proofs = A.run_fix_replays(self.r.path, head, A.git(self.r.path, "rev-parse", "HEAD").strip(), again)
        pid, why = A.fix_replay_refusal(self.r.path, again, self.st, proofs=proofs)
        self.assertEqual(pid, "fix-replay")
        self.assertIn("agentctl ran `grep -q fixed marker.txt`: base=PASS head=PASS", why)

    def test_land_refuses_a_gate_fix_without_its_replay_and_lands_a_proven_one(self):
        self.gate_red()
        sh = lambda *args: A.subprocess.run(args, cwd=self.r.path, capture_output=True, text=True)  # noqa: E731
        import io, contextlib
        self.fix("Fixes-Red: 2600")
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            rc, _head = A.sync(self.r.path, "origin/main", sh, FakeClock(), self.st)
        self.assertEqual(rc, 1)
        self.assertIn("NOT LANDED: Main #2600 [unit (web)] is a gate red", out.getvalue())
        A.git(self.r.path, "reset", "-q", "--hard", self.base)
        self.r.write("marker.txt", "fixed\n")
        self.r.commit("the fix\n\nFixes-Red: 2600\nFix-Replay: grep -q fixed marker.txt")
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            rc, head = A.sync(self.r.path, "origin/main", sh, FakeClock(), self.st)
        self.assertIsNone(rc)
        fields = A.change_fields(self.r.path, "origin/main", head, A.argparse.Namespace(), self.st)
        self.assertEqual([(p["cmd"], p["base"], p["head"]) for p in fields["replays"]],
                         [("grep -q fixed marker.txt", "FAIL", "PASS")], "the proof rides with the submission")
        self.assertIn("replays", A.SUBMIT_FIELDS)

    def test_a_red_outside_the_gate_only_warns(self):
        self.st.put_item("red", "50bc15cd7369", "fix-pending", {
            "key": "50bc15cd7369", "workflow": "Alert backtest", "runs": ["9"], "last_run": {"number": 9},
            "red_kind": "outside"})
        pid, _why = A.fix_replay_refusal(self.r.path, self.fix("Fixes-Red: 5"), self.st)
        self.assertEqual(pid, "fix-replay-outside")
        self.assertFalse(A.POLICIES["fix-replay-outside"].approved, "warn-only (the owner, 2026-11-20)")

    def test_a_gate_fixer_is_told_its_fix_replay(self):
        u = {"key": "9d1e2a7c4b10", "workflow": "Main", "job": "unit (web)", "jobs": ["unit (web)"], "gate": True,
             "red_kind": "gate", "run": {"number": 2600}, "last_run": {"number": 2600}, "line": "1 failed",
             "kind": "test"}
        saved = A.red_repo_clone
        A.red_repo_clone = lambda o_r: self.r.path
        self.addCleanup(setattr, A, "red_repo_clone", saved)
        prompt = A.fixer_prompt(u, "/tmp/wt", "the brief")
        self.assertIn("REPLAY YOUR GATE FIX: beside Fixes-Red, add the trailer `Fix-Replay: <command>`", prompt)
        self.assertIn("`Fix-Replay: none — <why>`", prompt)

    def test_the_fixer_is_told_to_replay(self):
        u = {**self.st.item("red", "5254bffda9d2")["data"], "run": {"number": 5, "databaseId": 44459271435},
             "workflow": "Alert backtest", "job": "backtest", "line": "FAILED -- 19 problem(s)", "kind": "test"}
        saved = A.red_repo_clone
        A.red_repo_clone = lambda o_r: self.r.path
        self.addCleanup(setattr, A, "red_repo_clone", saved)
        self.assertIn(f"REPLAY BEFORE LANDING: this red's check replays cheaply: `{CMD}`",
                      A.fixer_prompt(u, "/tmp/wt", "the brief"))


import test_backlog_batch  # noqa: E402


class ServerRefusesUnreplayedFixTest(test_backlog_batch.BacklogBatchTest):
    """The laptop's land often holds no store token (the sync submits it): the server's check refuses it too."""

    def test_the_check_refuses_a_queued_fix_with_no_replay(self):
        self.r.write(A.RED_WORKFLOWS_FILE, REGISTRY)
        self.r.commit("the registry")
        self.r.push()
        wt = self.r.worktree("wt-guess", branch="guess")
        self.r.write("infra/test/alert-backtest-allowlist.yml", "a guess\n", at=wt)
        self.r.commit("backtest: a guess\n\nFixes-Red: 5", at=wt)
        eid = self.queued(wt)[0]  # no red known when it queued: the laptop could not tell
        A.process_store().put_item("red", "5254bffda9d2", "offered", {
            "key": "5254bffda9d2", "workflow": "Alert backtest", "jobs": ["backtest"], "runs": ["5", "44459271435"],
            "last_run": {"number": 5, "databaseId": 44459271435}, "red_kind": "outside"})
        with approved("fix-replay-outside"):  # outside the gate it only warns, unless approved (test_policy.py)
            A.check_entry(self.r.path, eid, *on_clock(self.clock))
        e = self.entry(eid)
        self.assertEqual(e["status"], "rejected")
        self.assertIn(f"`Fix-Replay: {CMD} base=FAIL head=PASS`", e["why"])


class ServerRefusesUnreplayedGateFixTest(test_backlog_batch.BacklogBatchTest):
    def test_the_check_refuses_a_queued_gate_fix_with_no_replay(self):
        wt = self.r.worktree("wt-gate", branch="gate")
        self.r.write("web/src/main/Fix.scala", "a fix\n", at=wt)
        self.r.commit("web: a fix\n\nFixes-Red: 2600", at=wt)
        eid = self.queued(wt)[0]
        A.process_store().put_item("red", "9d1e2a7c4b10", "offered", {
            "key": "9d1e2a7c4b10", "workflow": "Main", "jobs": ["unit (web)"], "runs": ["2600"],
            "last_run": {"number": 2600}, "red_kind": "gate", "gate": True})
        A.check_entry(self.r.path, eid, *on_clock(self.clock))
        e = self.entry(eid)
        self.assertEqual(e["status"], "rejected")
        self.assertIn("is a gate red", e["why"])


for _cls in (ServerRefusesUnreplayedFixTest, ServerRefusesUnreplayedGateFixTest):
    for _n in dir(test_backlog_batch.BacklogBatchTest):  # its base's own tests run in their module, not again here
        if _n.startswith("test_") and _n not in _cls.__dict__:
            setattr(_cls, _n, None)


if __name__ == "__main__":
    unittest.main()
