"""FIX REPLAY (2026-11-19, Alert backtest 5254bffda9d2: seven fixes landed and each was refuted by the next backtest run).
A workflow whose `.github/red-workflows.tsv` row names a replay holds its fixers to it: a Fixes-Red: landing for its
red must carry `Fix-Replay: <command> base=FAIL head=PASS`, or land refuses it, naming the command."""
import os, sys, tempfile, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A, Repo, approved  # noqa: E402

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
        why = A.fix_replay_refusal(self.r.path, self.fix("Fixes-Red: 5"), self.st)
        self.assertIn(f"run `{CMD}` at origin/main and at your fix", why)
        self.assertIn(f"`Fix-Replay: {CMD} base=FAIL head=PASS`", why)

    def test_a_replay_that_did_not_pass_at_head_is_refused(self):
        why = A.fix_replay_refusal(self.r.path, self.fix(f"Fixes-Red: 5\nFix-Replay: {CMD} base=FAIL head=FAIL"), self.st)
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
        why = A.fix_replay_refusal(self.r.path, self.fix("Fixes-Red: 5"), self.st)
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
        saved = A.Store
        A.Store = lambda *a, **k: self.st
        self.addCleanup(setattr, A, "Store", saved)
        self.fix("Fixes-Red: 5")
        sh = lambda *args: A.subprocess.run(args, cwd=self.r.path, capture_output=True, text=True)  # noqa: E731
        import io, contextlib
        out, errs = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(errs):
            rc, head = A.sync(self.r.path, "origin/main", sh)
        # THE POLICY REGISTRY: fix-replay is agent-proposed, never decided by the owner -- it warns, never blocks
        self.assertIsNone(rc)
        self.assertIn("POLICY UNAPPROVED fix-replay", errs.getvalue())
        with approved("fix-replay"):  # once the owner approves it, it refuses as it was written to
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc, head = A.sync(self.r.path, "origin/main", sh)
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
            rc, _ = A.sync(self.r.path, "origin/main", sh)
        self.assertEqual(rc, 1)
        self.assertIn("a rebase is in progress", out.getvalue())
        self.assertTrue(A.rebase_in_progress(self.r.path), "never land's to abort: it would reset the branch")
        sh("git", "rebase", "--abort")
        real = A.sync_onto_main

        def leaves_one(repo, main_ref, sh_, release_hold=False):  # a path that stops mid-rebase
            sh_("git", "rebase", "-q", mine)
            return 1, None
        A.sync_onto_main = leaves_one
        self.addCleanup(setattr, A, "sync_onto_main", real)
        with contextlib.redirect_stdout(io.StringIO()):
            A.sync(self.r.path, "origin/main", sh)
        self.assertIsNone(A.rebase_in_progress(self.r.path), "a rebase land started is aborted on its way out")

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
        A.Store().put_item("red", "5254bffda9d2", "offered", {
            "key": "5254bffda9d2", "workflow": "Alert backtest", "jobs": ["backtest"], "runs": ["5", "44459271435"],
            "last_run": {"number": 5, "databaseId": 44459271435}, "red_kind": "outside"})
        with approved("fix-replay"):  # agent-proposed: it refuses only once the owner approves it (test_policy.py)
            A.check_entry(self.r.path, eid)
        e = self.entry(eid)
        self.assertEqual(e["status"], "rejected")
        self.assertIn(f"`Fix-Replay: {CMD} base=FAIL head=PASS`", e["why"])


for _n in dir(test_backlog_batch.BacklogBatchTest):  # its base's own tests run in their module, not again here
    if _n.startswith("test_") and _n not in ServerRefusesUnreplayedFixTest.__dict__:
        setattr(ServerRefusesUnreplayedFixTest, _n, None)


if __name__ == "__main__":
    unittest.main()
