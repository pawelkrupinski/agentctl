"""Repros of open red re-offer / re-dispatch issues (classification, 2026-11-19)."""
import json, os, sys, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402
from test_red import run  # noqa: E402
from test_reds import RedsBase  # noqa: E402
from test_agentctl import sh  # noqa: E402
from test_red import RedBase  # noqa: E402
from test_red_repos import RedReposBase, CORE  # noqa: E402


class ReOfferLoopTest(RedsBase):
    def land(self, msg, name="repo-wt-fix"):
        wt = self.r.worktree(name, branch=name)
        self.r.write("fix.txt", f"{name}\n", at=wt)
        sha = self.r.commit(msg, at=wt)
        self.r.push(at=wt, ref=f"{sha}:main")
        return wt, sha

    def queue_run(self, sha, status="queued"):
        self.put("runs.json", [run(50, 23, "Main", "main.yml", sha, status=status, created=A.iso_at(self.t[0]))]
                 + json.load(open(os.path.join(self.d, "runs.json"))))


    def test_20261107T224109_5c81e4_an_untrailered_fix_on_main_with_its_proving_run_queued_holds_the_red(self):
        self.tick()
        _wt, sha = self.land("fix FooSpec, no trailer")  # landed by another agent; no Fixes-Red, no fixer verdict
        self.queue_run(sha, "queued")
        for _ in range(3):
            self.tick(advance=120)
        it = self.only()
        self.assertEqual(it["state"], "fix-pending", it["data"].get("history")[-1:])

    def outside_capture_red(self):
        """Identity capture #2104 red on main (an outside red: no gate run ever proves its fix)."""
        self.put("runs.json", [run(40, 20, "Main", "main.yml", self.sha), run(39, 19, "Identity model convergence",
                                                                             "identity-model-convergence.yml", self.sha),
                               run(70, 2104, "Identity capture", "identity-capture.yml", self.sha, conclusion="failure")])
        self.put("run-70.json", {"jobs": [{"name": "capture (uk)", "status": "completed", "conclusion": "failure",
                                           "steps": [{"name": "Run", "conclusion": "failure"}]}], "headSha": self.sha,
                                 "url": "u", "number": 2104, "status": "completed", "conclusion": "failure",
                                 "workflowName": "Identity capture"})
        self.put("run-70.log", "capture (uk)\tRun\t2026-10-27T08:00:00Z [info] - CaptureSpec *** FAILED ***\n")
        self.r.write(".github/workflows/identity-capture.yml",
                     "name: Identity capture\non:\n  schedule:\n    - cron: '0 3 * * 1'\n  workflow_dispatch:\n")
        self.r.commit("the workflow")
        self.r.push()
        self.tick()
        return self.only()

    def queued_entry(self, fixes_msg, status, path="capture.txt"):
        wt = self.r.worktree("repo-wt-queued", branch="repo-wt-queued")
        self.r.write(path, "queued\n", at=wt)
        sha = self.r.commit(fixes_msg, at=wt)
        d = A.waiting_state(self.r.path)[0]
        e, _ = A.queue_change(d, {"worktree": wt, "branch": "repo-wt-queued", "head": sha, "commits": [sha]})
        e["status"] = status
        A.save_entry(d, e)
        return e


    def test_20261112T164528_14afee_a_reds_own_run_rerun_green_clears_it_never_re_offers_it(self):
        self.tick()
        self.assertEqual(self.only()["state"], "offered")
        # GitHub's rerun of run 40 (#20): same databaseId and number, attempt 2 concluded success
        runs = json.load(open(os.path.join(self.d, "runs.json")))
        self.put("runs.json", [{**r, "conclusion": "success"} if r["databaseId"] == 40 else r for r in runs])
        self.tick(advance=120)
        live = [i for i in self.st.items("red") if i["state"] not in A.RED_TERMINAL]
        self.assertEqual([i["data"].get("red_kind") for i in live], ["flake"],
                         "the failing run's own rerun is green: the red is over, not offered again -- and, no commit "
                         "between, a flake of its own (never rerun to green)")

    def test_20261106T102105_327f4c_a_queued_change_touching_the_red_workflows_own_files_holds_the_red(self):
        self.outside_capture_red()
        e = self.queued_entry("shard the capture workflow, no trailer", "waiting", path=".github/workflows/identity-capture.yml")
        self.assertFalse(e.get("fixes"))
        for status in ("waiting", "checking"):
            d = A.waiting_state(self.r.path)[0]
            e["status"] = status
            A.save_entry(d, e)
            self.tick(advance=120)
            it = self.only()
            self.assertEqual(it["state"], "fix-pending",
                             f"{status}: a queued change editing the red's own workflow file is its fix on the way, not "
                             f"a reason to send another fixer ({it['data'].get('history')[-1:]})")

    def test_a_queued_untrailered_change_editing_a_script_the_gate_reds_job_runs_is_no_fix(self):
        """2026-11-20, Main #2716: metrics-packages (no Fixes-Red) edited scripts/ci/coupling_ratchet.py, which Main's
        failing job reads, and was taken as the gate red's fix on its way -- "landing on red (Fixes-Red), queued" -- so
        the red's real fixer was stopped. A gate red's fix carries `Fixes-Red:` (nothing else lands while it is red):
        an untrailered change waiting on that very gate is never its fix."""
        self.r.write(".github/workflows/main.yml", "name: Main\non: push\njobs:\n  test:\n    name: test (rest)\n"
                     "    steps:\n      - name: Run tests\n        run: scripts/ci/run-tests.sh\n")
        self.r.write("scripts/ci/run-tests.sh", "#!/bin/sh\nsbt test\n")
        self.r.commit("the workflow")
        self.r.push()
        self.tick()
        e = self.queued_entry("tidy the test runner, no trailer", "queued", path="scripts/ci/run-tests.sh")
        self.assertFalse(e.get("fixes"))
        reads = A.red_job_reads(self.r.path, sh(self.r.path, "git", "rev-parse", "HEAD"), self.only()["data"])
        self.assertIn("scripts/ci/run-tests.sh", reads["files"], "the change does edit a file the failing job reads")
        for _ in range(2):
            self.tick(advance=120)
        it = self.only()
        self.assertNotEqual(it["state"], "fix-pending", f"{it['data'].get('fix')}")
        self.assertIsNone(it["data"].get("fix"))

    def test_a_fixer_lapsing_with_its_fixes_red_commit_unlanded_is_offered_again_to_land_it_despite_an_untrailered_change(self):
        """Main #2716: its owner's lease lapsed with 256d2571c (`Fixes-Red: 2716`) committed and unlanded, while the
        red read "fix ... landing on red" for an untrailered change -- nobody chased the real fix. The red goes back to a
        fixer, briefed with the commits left in that worktree, never parked on a change that is no fix."""
        self.r.write(".github/workflows/main.yml", "name: Main\non: push\njobs:\n  test:\n    name: test (rest)\n"
                     "    steps:\n      - name: Run tests\n        run: scripts/ci/run-tests.sh\n")
        self.r.write("scripts/ci/run-tests.sh", "#!/bin/sh\nsbt test\n")
        self.r.commit("the workflow")
        self.r.push()
        self.tick()
        key = self.only()["data"]["key"]
        wt = self.r.worktree("repo-wt-fixer", branch="repo-wt-fixer")
        self.st.acquire(A.red_lease(key), "laptop:claim:fixer@" + wt, 1800, 3600, meta={"kind": "claim", "worktree": wt})
        self.tick(advance=5)
        self.assertEqual(self.only()["state"], "leased")
        self.r.write("FooSpec.scala", "fixed\n", at=wt)
        fix = self.r.commit("FooSpec: the fix\n\nFixes-Red: 20", at=wt)
        self.queued_entry("tidy the test runner, no trailer", "queued", path="scripts/ci/run-tests.sh")
        self.tick(advance=60)
        self.assertEqual(self.only()["state"], "leased", "an untrailered change never stops the red's fixer")
        self.tick(advance=3700)  # the lease lapses (past its max hold) with the fix committed, unlanded
        it = self.only()
        self.assertEqual(it["state"], "offered", it["data"].get("history", [])[-1:])
        self.assertEqual(it["data"]["prior_work"]["commits"], [fix[:10]], "the next fixer is pointed at the fix")

    def test_a_queued_untrailered_change_editing_only_a_script_an_outside_reds_workflow_runs_is_no_fix(self):
        """The same red (Order independence #89) took metrics-packages for its fix through
        scripts/ci/coupling_ratchet.py, a script many jobs read. Without a trailer, only an edit of the red's OWN
        workflow file links a change to it (20261106T102105-327f4c)."""
        self.r.write("scripts/ci/capture.sh", "#!/bin/sh\ncapture\n")
        self.r.commit("the capture script")
        self.r.push()
        self.outside_capture_red()
        self.r.write(".github/workflows/identity-capture.yml",
                     "name: Identity capture\non:\n  schedule:\n    - cron: '0 3 * * 1'\n  workflow_dispatch:\n"
                     "jobs:\n  capture:\n    steps:\n      - name: Run\n        run: scripts/ci/capture.sh\n")
        self.r.commit("the workflow runs the script")
        self.r.push()
        self.tick(advance=60)
        self.queued_entry("tidy the capture script, no trailer", "waiting", path="scripts/ci/capture.sh")
        for _ in range(2):
            self.tick(advance=120)
        it = self.only()
        self.assertNotEqual(it["state"], "fix-pending", f"{it['data'].get('fix')}")

    def test_a_red_parked_on_an_untrailered_change_that_no_longer_links_it_goes_back_to_its_holder(self):
        """Order independence #89 sat fix-pending on metrics-packages (linked through a script before that link was
        dropped) while its family owner still held the lease: ci-why kept naming that change as its fix. A pending fix
        whose change no longer links the red is no fix: the red is its live holder's again (offered with none)."""
        self.r.write("scripts/ci/capture.sh", "#!/bin/sh\ncapture\n")
        self.r.commit("the capture script")
        self.r.push()
        it = self.outside_capture_red()
        wt = self.r.worktree("repo-wt-owner", branch="repo-wt-owner")
        self.st.acquire(A.red_lease(it["data"]["key"]), "laptop:claim:owner@" + wt, 1800, 3600,
                        meta={"kind": "claim", "worktree": wt})
        e = self.queued_entry("tidy the capture script, no trailer", "waiting", path="scripts/ci/capture.sh")
        self.st.put_item("change", e["id"], "waiting", {"worktree": e["worktree"], "head": e["head"]})
        A.red_move(self.st, self.only(), "fix-pending", "parked by the old rule", data={"fix": {
            "entry": e["id"], "status": "waiting", "linked": "its workflow's own files"}})
        self.tick(advance=60)
        it = self.only()
        self.assertEqual(it["state"], "leased", it["data"].get("fix"))
        self.assertIsNone(it["data"].get("fix"))

    def test_20261101T030050_695594_a_fixers_agentctl_defect_verdict_waits_on_the_issue_never_goes_back_to_a_fixer(self):
        self.tick()
        key = self.only()["data"]["key"]
        issue = A.issue_open(self.st, "[red c62653] a defect in agentctl, found by the fixer", "agentctl")["id"]
        wt = self.r.worktree("repo-wt-fixer", branch="repo-wt-fixer")
        lease = self.st.acquire(A.red_lease(key), "laptop:claim:fixer@" + wt, 1800, 3600,
                                meta={"kind": "claim", "worktree": wt})
        self.tick(advance=5)
        self.assertEqual(self.only()["state"], "leased")
        self.st.release(A.red_lease(key), lease["epoch"], lease["secret"],
                        outcome=f"GAVE UP: this is an agentctl defect, filed as issue {issue}; no fix for it can land in movies")
        self.tick(advance=5)
        it = self.only()
        self.assertEqual(it["state"], "blocked-external",
                         f"a red blamed on an open agentctl issue waits on that issue; got {it['state']}: "
                         f"{it['data']['history'][-1]['outcome'][:80]}")


    def test_20261103T232907_ca63b2_a_resumed_fixers_first_claim_binds_its_new_session_before_the_wrapper_has(self):
        # A resumed fixer's record carries the session it resumes (red_dispatch: `session` = resume.session); the new
        # `claude --bg --resume` session reports its own id, and until the wrapper's poll rewrites the record the
        # fixer's `red claim --fixer <record>` -- the first command of its prompt -- is refused.
        os.makedirs(A.reds_dir("2473"), exist_ok=True)
        rec = A.reds_dir("2473", "fixer-5.json")
        A.write_json(rec, {"run": 2473, "workflow": "Main", "session": None, "resumed_from": "s-resumed-from",
                           "resume": True,
                           "started": "2026-10-29T10:28:58Z", "lease": "red/a0762f14066e"})
        os.environ.update(AGENTCTL_AGENT_NAME="red-fixer-2473", AGENTCTL_RED_FIXER="2473", AGENTCTL_FIXER_RECORD=rec,
                          CLAUDE_CODE_SESSION_ID="s-the-resumed-sessions-own-id")
        try:
            rc, out = self.tool("red", "claim", "2473", "--fixer", rec, "--repo", "movies")
        finally:
            for k in ("AGENTCTL_AGENT_NAME", "AGENTCTL_RED_FIXER", "AGENTCTL_FIXER_RECORD", "CLAUDE_CODE_SESSION_ID"):
                os.environ.pop(k, None)
        self.assertNotIn("no live fixer record", out)
        self.assertEqual(rc, 0, out)

    def test_20261102T085329_930a4a_another_repos_owner_block_probes_that_repos_snapshot_not_movies(self):
        self.tick()
        it = self.only()
        self.st.update_item("red", it["id"], it["version"], {"repo": "bitcashier/core"})
        it = self.only()
        A.red_decision_block(self.st, it, {}, "hold the red as fixed by its landed fix; rule: x", self.t[0],
                             category="policy-no-rule")
        row = self.st.get(A.block_lease(it["data"]["key"]))
        probe = A.lease_meta(row)["probe"]
        self.assertEqual(probe["kind"], "red_newer_verdict")
        self.assertEqual(probe["args"].get("repo"), "bitcashier/core",
                         "probe_red_newer_verdict reads ci_state_load(a['repo'] or movies): a core red's block must carry it")


class OrphanByContentTest(RedBase):
    def test_20261104T002213_cc3b4a_a_commit_landed_under_a_new_sha_differing_only_by_a_moved_blank_line_is_landed(self):
        # movies-wt-flip-pop: 4 of 5 commits were on main by subject, author and author date; a blank line had moved in
        # the doc hunk, so patch-ids differed AND content_twin's identical-+/- lines failed: an orphan fixer was sent
        # and landing would have reverted the back-out
        self.r.write("doc.md", "head\n")
        self.r.commit("doc.md")
        self.r.push()
        wt = self.r.worktree("repo-wt-flip-pop", branch="flip-pop")
        self.r.write("doc.md", "head\nfirst para\n\nsecond para\n", at=wt)
        sha = self.r.commit("doc: the flip population", at=wt)
        self.r.write("doc.md", "head\nfirst para\nsecond para\n\n")  # the same words, the blank line moved
        date = sh(wt, "git", "log", "-1", "--format=%ad", "--date=raw", sha)
        sh(self.r.path, "git", "add", "-A")
        sh(self.r.path, "git", "commit", "-q", "-m", "doc: the flip population", f"--date={date}")
        self.r.push()
        sh(wt, "git", "fetch", "-q", "origin")
        self.assertTrue(sh(wt, "git", "cherry", "origin/main", "HEAD").startswith("+ "))
        self.assertEqual(A.cherry_landed(wt, "refs/remotes/origin/main", "HEAD"), ([sha], []))
        self.assertEqual(A.orphan_key(self.r.path, wt), ([], None))


class CloneLessRepoTest(RedReposBase):
    def test_20261102T085329_930a4a_a_clone_less_repos_red_whose_fixer_landed_is_not_offered_again(self):
        # ci-runner has no clone of bitcashier/core: red_fix_facts(None) reads no Fixes-Red, red_fix_of cannot see the
        # landed sha on main (red_sha_on_main: no repo), so every fixer's LANDED re-offers the red
        self.put("runs-bitcashier_core.json",
                 [run(9101, 801, "Main Pipeline", "main-pipeline.yml", self.sha, conclusion="failure")])
        self.put("run-9101.json", {"jobs": [{"name": "test", "status": "completed", "conclusion": "failure",
                                             "steps": [{"name": "sbt test", "conclusion": "failure"}]}],
                                   "headSha": self.sha, "url": "u", "number": 801, "status": "completed",
                                   "conclusion": "failure", "workflowName": "Main Pipeline"})
        self.put("run-9101.log", "test\tsbt test\t2026-10-28T07:14:00Z [info] - ApiSpec should x *** FAILED ***\n")
        self.others()
        (it,) = self.by_repo()[CORE]
        key = it["data"]["key"]
        lease = self.st.acquire(A.red_lease(key), "laptop:claim:fixer@/x", 1800, 3600, meta={"kind": "claim"})
        self.others(advance=5)
        self.assertEqual(self.st.item("red", it["id"])["state"], "leased")
        landed = "8f3f57201" + "0" * 31
        self.put(f"api-repos_bitcashier_core_compare_{landed}...main.json", {"status": "ahead", "ahead_by": 1})
        self.st.release(A.red_lease(key), lease["epoch"], lease["secret"], outcome=f"LANDED {landed}")
        self.others(advance=5)
        it = self.st.item("red", it["id"])
        self.assertEqual(it["state"], "fix-pending", f"{it['state']}: {it['data']['history'][-1:]}")


if __name__ == "__main__":
    unittest.main()
