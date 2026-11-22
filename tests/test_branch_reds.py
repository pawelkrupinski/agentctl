"""Branch runs (the owner, 2026-10-27: "red not captured, make a structural fix" -- convergence #2116 on a measure/
branch failed and nobody was told). Owner records for every dispatch (dispatch run / record, matched after the fact by
branch name), branch runs in the snapshot, BRANCH RED routed once to the owner, PREDICTS MAIN RED (commit on main AND
main's inputs) claimed like a main red, the status line's `branch reds:`, and the sweep of branches whose owner is gone."""
import json, os, shutil, sys, time, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A, sh  # noqa: E402
from test_red import RedBase, run, MOVIES, FAIL_LINE  # noqa: E402

CONV, CONV_FILE = "Identity model convergence", "identity-model-convergence.yml"
MAIN_INPUTS = {"profile": "false", "takes-only": "false", "takes-skip": "shadow", "tier": "gate-lane"}


def brun(rid, num, sha, branch, conclusion="failure", created="2026-10-27T12:44:32Z", wf=CONV, wfile=CONV_FILE,
         event="workflow_dispatch"):
    r = run(rid, num, wf, wfile, sha, conclusion=conclusion, created=created)
    r.update(headBranch=branch, event=event)
    return r


class BranchBase(RedBase):
    def setUp(self):
        super().setUp()
        os.environ["AGENTCTL_DISPATCH_DIR"] = os.path.join(self.tmp.name, "dispatches")
        self.addCleanup(os.environ.pop, "AGENTCTL_DISPATCH_DIR", None)
        self.saved_exists = A.branch_exists
        self.addCleanup(setattr, A, "branch_exists", self.saved_exists)
        A.branch_exists = lambda o_r, b: True  # every branch is alive (GitHub serves it) unless a test deletes it
        # an agent's worktree: it committed X, dispatched a measurement of X, and moved on (X now only in its reflog)
        self.wt = self.r.worktree("movies-wt-drift", branch="drift")
        self.r.write("drift.txt", "x\n", at=self.wt)
        self.x = self.r.commit("Live drift: each drifted listing", at=self.wt)
        self.r.write("drift.txt", "y\n", at=self.wt)
        self.r.commit("later work", at=self.wt)
        self.branch = f"measure/pawel/{self.x[:9]}-0b04a4"
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 600))
        self.put("runs.json", [brun(64, 2116, self.x, self.branch, created=now),
                               dict(run(40, 20, "Main", "main.yml", self.sha), event="push"),
                               dict(run(39, 19, CONV, CONV_FILE, self.sha), event="workflow_dispatch")])
        self.put("run-64.json", {"jobs": [{"name": "convergence (germany)", "status": "completed", "conclusion": "failure",
                                           "steps": [{"name": "Run suite", "conclusion": "failure"}]}],
                                 "headSha": self.x, "url": "u", "number": 2116, "status": "completed",
                                 "conclusion": "failure", "workflowName": CONV})
        self.put("run-64.log", f"convergence (germany)\tRun suite\t2026-10-27T13:00:00Z {FAIL_LINE}\n")
        self.inputs(39, MAIN_INPUTS)
        self.inputs(64, dict(MAIN_INPUTS, tier="", **{"takes-base": "ac65cbb", "takes-countries": "pl,uk"}))

    def inputs(self, rid, d):
        A.write_json(A.dispatches_dir("inputs", f"{rid}.json"), d)

    def land_x(self):
        sh(self.wt, "git", "push", "-q", "origin", f"{self.x}:main")
        sh(self.r.path, "git", "fetch", "-q", "origin")

    def snap(self):
        A._gate_lists.clear()
        A._outside_lists.clear()
        return A.ci_state_refresh(MOVIES)


class OwnerTest(BranchBase):
    def test_a_measure_branch_is_matched_to_the_worktree_whose_reflog_holds_its_commit_and_recorded(self):
        e = self.snap()["branches"][0]
        self.assertTrue(e["red"])
        self.assertEqual(os.path.realpath(e["owner"]["worktree"]), os.path.realpath(self.wt))
        rec = A.dispatch_for(self.branch)
        self.assertEqual(rec["source"], "matched")
        self.assertFalse(A.owner_gone(rec))

    def test_dispatch_record_and_run_write_the_owner(self):
        os.environ["AGENTCTL_AGENT_NAME"] = "agent-7"
        self.addCleanup(os.environ.pop, "AGENTCTL_AGENT_NAME", None)
        rc, out = self.tool("dispatch", "record", "--branch", "measure/x", "--purpose", "drift check", "--repo", "movies",
                            cwd=self.wt)
        self.assertEqual(rc, 0, out)
        rec = A.dispatch_for("measure/x")
        self.assertEqual((rec["agent"], rec["purpose"]), ("agent-7", "drift check"))
        self.assertEqual(os.path.realpath(rec["worktree"]), os.path.realpath(self.wt))
        os.environ.update(AGENTCTL_DISPATCH_FIND="1", AGENTCTL_DISPATCH_FIND_POLL="0")
        self.addCleanup(lambda: [os.environ.pop(k, None) for k in ("AGENTCTL_DISPATCH_FIND", "AGENTCTL_DISPATCH_FIND_POLL")])
        fresh = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self.put("runs.json", [brun(77, 2200, self.x, "measure/y", conclusion="success", created=fresh)])
        rc, out = self.tool("dispatch", "run", CONV_FILE, "--ref", "measure/y", "-f", "recording=3166", "--repo", "movies",
                            cwd=self.wt)
        self.assertEqual(rc, 0, out)
        self.assertIn("run 77", out)
        rec = A.dispatch_for("measure/y", 77)
        self.assertEqual((rec["run_id"], rec["inputs"]), (77, {"recording": "3166"}))
        self.assertIn("workflow run identity-model-convergence.yml --ref measure/y -f recording=3166",
                      open(os.path.join(self.d, "calls.log")).read())
        self.assertIn("measure/y", self.tool("dispatch", "list", "--repo", "movies")[1])


class BranchRedTest(BranchBase):
    def test_predicts_main_red_when_its_commit_landed_with_main_inputs_routed_once_and_offered_like_a_main_red(self):
        self.land_x()
        snap = self.snap()
        e = A.branch_reds(snap)[0]
        self.assertTrue(e["predicts"], e)
        lines = A.route_branch_reds(MOVIES, snap)
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith(f"BRANCH RED: {CONV} #2116 on {self.branch} → owner "), lines[0])
        self.assertIn("PREDICTS MAIN RED", lines[0])
        ent = [x for x in A.inbox_entries() if x["kind"] == "BRANCH_RED"]
        self.assertEqual(len(ent), 1)
        self.assertTrue(any("FooSpec" in l for l in ent[0]["tail"]), ent[0]["tail"])
        self.assertEqual(A.route_branch_reds(MOVIES, snap), [])  # one entry per run
        units = A.predict_units(MOVIES, snap)
        self.assertTrue(units and all(u["gate"] and u["predicts"] == self.branch for u in units))
        st = A.Store(path=os.path.join(self.tmp.name, "store.db"))
        A.red_tick(st, MOVIES, self.r.path, snap, dispatch=False)  # a red item before main fails: offered like a gate red
        its = [i for i in st.items("red") if i["data"]["red_kind"] == "predicts"]
        self.assertTrue(its and all(i["state"] == "offered" and i["data"]["predicts"] == self.branch for i in its), its)
        self.assertIn("main will fail the same way next", A.fixer_prompt(units[0], "/wt", "/brief"))
        red, line = A.ci_why(MOVIES, summary=True, snap=A.ci_state_load(MOVIES), repo=self.r.path)
        self.assertFalse(red)
        self.assertIn("branch reds: 1 → movies-wt-drift (convergence #2116 PREDICTS MAIN RED)", line)
        self.assertNotIn("no reds", line)

    def pin(self, pins, tag="convergence-fixtures"):
        """main's convergence workflow names the pins' release; the release holds hermetic-<code>.txt per country."""
        if A.pin_release_tag(self.r.path) != tag:
            os.makedirs(os.path.join(self.r.path, ".github", "workflows"), exist_ok=True)
            self.r.write(A.CONVERGENCE_WORKFLOW, f"jobs:\n  legs:\n    env:\n      FIXTURE_RELEASE_TAG: {tag}\n")
            self.r.commit("convergence workflow")
            sh(self.r.path, "git", "push", "-q", "origin", "HEAD:main")
            sh(self.r.path, "git", "fetch", "-q", "origin")
        saved = A.read_pins
        self.addCleanup(setattr, A, "read_pins", saved)
        seen = []
        A.read_pins = lambda o_r, tag_: seen.append(tag_) or dict(pins)
        A.refresh_pinned_recording(MOVIES, self.r.path, force=True, role="sync")  # the laptop reads the release
        self.assertEqual(seen, [tag])

    def test_the_pinned_recording_is_read_from_the_movies_repo_never_hard_coded(self):
        self.pin({"pl": "528", "de": "528"})
        self.assertEqual(A.pinned_recording(), "528")
        self.assertEqual(A.diff_text([("recording", "573", "")]), "recording 573 ≠ main's 528")
        self.assertEqual(A.newer_recording({"recording": "573"}, {"recording": ""}), "573")
        self.pin({"pl": "528", "de": "600"})
        self.assertEqual(A.pinned_recording(), "528/600")
        self.assertIsNone(A.newer_recording({"recording": "573"}, {"recording": ""}))

    def test_differing_inputs_are_a_plain_branch_red_naming_the_input_and_a_newer_recording_blocks_promoting(self):
        A.write_json(A.pinned_path(), {"at": time.time(), "tag": "convergence-fixtures", "pins": {"pl": "573", "de": "573"}})
        self.land_x()
        self.inputs(64, dict(MAIN_INPUTS, recording="3166"))
        snap = self.snap()
        e = A.branch_reds(snap)[0]
        self.assertFalse(e["predicts"])
        self.assertEqual(e["why"], "recording 3166 ≠ main's 573")
        line = A.route_branch_reds(MOVIES, snap)[0]
        self.assertNotIn("PREDICTS", line)
        self.assertIn("(recording 3166 ≠ main's 573); blocks promoting recording 3166", line)
        self.assertEqual(A.predict_units(MOVIES, snap), [])
        summary = A.ci_why(MOVIES, summary=True, snap=snap, repo=self.r.path)[1]
        self.assertIn("branch reds: 1 → movies-wt-drift (convergence #2116)", summary)

    def test_a_commit_not_on_main_predicts_nothing(self):
        e = A.branch_reds(self.snap())[0]
        self.assertFalse(e["predicts"])
        self.assertIn("is not on main", e["why"])

    def test_unread_inputs_predict_nothing(self):
        self.land_x()
        os.unlink(A.dispatches_dir("inputs", "64.json"))
        e = A.branch_reds(self.snap())[0]
        self.assertFalse(e["predicts"])
        self.assertIn("unread", e["why"])

    def test_a_branch_red_drops_out_when_its_branch_is_deleted_or_a_newer_run_on_it_is_green(self):
        self.assertEqual(len(A.branch_reds(self.snap())), 1)
        A.branch_exists = lambda o_r, b: False
        self.assertEqual(A.branch_reds(self.snap()), [])
        self.assertEqual(A.ci_why(MOVIES, summary=True, snap=A.ci_state_load(MOVIES), repo=self.r.path)[1],
                         "gate green · no reds")
        A.branch_exists = lambda o_r, b: True
        runs = json.load(open(os.path.join(self.d, "runs.json")))
        self.put("runs.json", [brun(66, 2118, self.x, self.branch, conclusion="success")] + runs)
        self.assertEqual(A.branch_reds(self.snap()), [])

    def test_a_failure_on_a_deleted_branch_is_dropped_silently(self):
        A.branch_exists = lambda o_r, b: False  # its owner deleted it after finishing (measure/de-speed-conf-1)
        snap = self.snap()
        self.assertFalse(snap["branches"][0]["alive"])
        self.assertEqual(A.route_branch_reds(MOVIES, snap), [])
        self.assertEqual([x for x in A.inbox_entries() if x["kind"] == "BRANCH_RED"], [])
        self.assertEqual(A.branch_reds(snap), [])

    def test_branch_exists_reads_a_404_as_deleted(self):
        A.branch_exists = self.saved_exists
        self.put(f"api-repos_pawelkrupinski_movies_branches_{self.branch.replace('/', '%2F')}.json", {"name": self.branch})
        self.assertTrue(A.branch_exists(MOVIES, self.branch))
        saved = A.gh_json
        A.gh_json = lambda *a: (_ for _ in ()).throw(RuntimeError("gh api: gh: Not Found (HTTP 404)"))
        self.addCleanup(setattr, A, "gh_json", saved)
        self.assertFalse(A.branch_exists(MOVIES, "measure/de-speed-conf-1"))

    def test_an_old_failure_is_recorded_not_routed(self):
        snap = self.snap()
        self.assertEqual(A.route_branch_reds(MOVIES, snap, now_=time.time() + 2 * A.BRANCH_ROUTE_MAX_AGE), [])
        self.assertEqual([x for x in A.inbox_entries() if x["kind"] == "BRANCH_RED"], [])

    def test_inbox_wait_wakes_on_a_branch_red(self):
        self.assertEqual(A.inbox_kind("BRANCH RED: X #1 on measure/a → owner ~/w"), "BRANCH_RED")
        self.assertIn("BRANCH_RED", A.WAIT_KINDS_DEFAULT.split(","))


IDRUN, IDRUN_FILE = "Identity run", "identity-run.yml"


class OrphanBranchRedTest(BranchBase):
    """The three real shapes (status page, 2026-10-29: `branch reds: 3 → NOBODY`): identity run #2365 on
    measure/pawel/3757d1ecc-b148ea (its commit here, not on main, its worktree removed) and #2370 / #2371 on
    measure/pawel/3f34bbab5-d458ee / -f16735 (one commit, on main; main runs no Identity run)."""

    def setUp(self):
        super().setUp()
        gone_wt = self.r.worktree("movies-wt-removed", branch="removed")
        self.r.write("removed.txt", "x\n", at=gone_wt)
        self.off = self.r.commit("measured, never landed", at=gone_wt)
        sh(self.r.path, "git", "worktree", "remove", "--force", gone_wt)
        landed = self.r.worktree("movies-wt-landed", branch="landed")  # landed on main by a worktree since removed
        self.r.write("landed.txt", "x\n", at=landed)
        self.on = self.r.commit("measured and landed", at=landed)
        sh(landed, "git", "push", "-q", "origin", f"{self.on}:main")
        sh(self.r.path, "git", "fetch", "-q", "origin")
        sh(self.r.path, "git", "worktree", "remove", "--force", landed)
        self.b65 = f"measure/pawel/{self.off[:9]}-b148ea"
        self.b70, self.b71 = f"measure/pawel/{self.on[:9]}-d458ee", f"measure/pawel/{self.on[:9]}-f16735"
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 600))
        self.put("runs.json", [brun(3548, 2365, self.off, self.b65, created=now, wf=IDRUN, wfile=IDRUN_FILE),
                               brun(3553, 2370, self.on, self.b70, created=now, wf=IDRUN, wfile=IDRUN_FILE),
                               brun(3554, 2371, self.on, self.b71, created=now, wf=IDRUN, wfile=IDRUN_FILE),
                               dict(run(40, 20, "Main", "main.yml", self.sha), event="push"),
                               dict(run(39, 19, CONV, CONV_FILE, self.sha), event="workflow_dispatch")])
        self.store = A.open_store()

    def reds(self, snap):
        return {e["run"]["number"]: e for e in snap["branches"] if e.get("red")}

    def test_the_three_shapes_close_themselves_with_an_event_saying_why_and_leave_the_status_line(self):
        snap = self.snap()
        e = self.reds(snap)
        self.assertIn("is not on main", e[2365]["orphan"])
        self.assertIn("main runs no such workflow", e[2370]["orphan"])
        self.assertIn("main runs no such workflow", e[2371]["orphan"])
        self.assertEqual(A.branch_reds(snap), [])
        texts = A.settle_branch_reds(MOVIES, snap, self.store)
        self.assertEqual(len(texts), 3, texts)
        t65 = next(t for t in texts if "#2365" in t)
        self.assertTrue(t65.startswith(f"branch red closed itself: {IDRUN} #2365 on {self.b65}: no owner record"), t65)
        self.assertIn("`agentctl cleanup`", t65)
        evs = [x for x in self.store.events() if x["kind"] == "branch-red.orphaned"]
        self.assertEqual(sorted(x["refs"]["run"] for x in evs), [2365, 2370, 2371])
        self.assertEqual(A.settle_branch_reds(MOVIES, snap, self.store), [])  # once
        self.assertEqual(A.route_branch_reds(MOVIES, snap), [])
        self.assertEqual([x for x in A.inbox_entries() if x["kind"] == "BRANCH_RED"], [])
        line = A.ci_why(MOVIES, summary=True, snap=A.ci_state_load(MOVIES), repo=self.r.path)[1]
        self.assertNotIn("NOBODY", line)
        self.assertNotIn("branch reds", line)

    def test_a_commit_on_main_whose_workflow_main_runs_keeps_today_s_behaviour(self):
        b = f"measure/pawel/{self.on[:9]}-0c0c0c"
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 600))
        self.put("runs.json", [brun(3600, 2400, self.on, b, created=now),
                               dict(run(39, 19, CONV, CONV_FILE, self.sha), event="workflow_dispatch")])
        e = self.reds(self.snap())[2400]
        self.assertIsNone(e["orphan"])
        self.assertIsNone(e["owner"])
        self.assertEqual(len(A.branch_reds(self.snap())), 1)

    def test_main_s_own_newer_run_supersedes_an_ownerless_red_on_a_main_commit(self):
        """2026-11-02: `branch reds: 5 → NOBODY (convergence #592); ... #558; #557` -- drift runs on commits now on
        main, long after main's own convergence #691 ran: they predict nothing any more."""
        b = f"measure/pawel/{self.on[:9]}-0c0c0c"
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 600))
        self.put("runs.json", [dict(run(4000, 691, CONV, CONV_FILE, self.sha), event="workflow_dispatch"),
                               brun(3600, 592, self.on, b, created=now)])
        e = self.reds(self.snap())[592]
        self.assertIn("main's own run #691 came since", e["orphan"])
        self.assertEqual(A.branch_reds(self.snap()), [])

    def test_a_dead_agent_s_red_stays_with_its_worktree_while_it_holds_the_commit(self):
        A.save_dispatch({"branch": self.b65, "run_id": 3548, "worktree": self.wt, "pid": 999999, "source": "registered"})
        sh(self.wt, "git", "update-ref", "refs/heads/keep-off", self.off)  # the worktree's repo holds it...
        holder = self.r.worktree("movies-wt-holder", base=self.off, branch="holder")  # ...and a worktree has it checked out
        e = self.reds(self.snap())[2365]
        self.assertIsNone(e["orphan"])
        self.assertFalse(e["owner_gone"])

    def test_it_reopens_routed_to_the_owner_when_its_commit_turns_up_in_a_worktree_or_an_owner_record(self):
        A.settle_branch_reds(MOVIES, self.snap(), self.store)
        saved = A.OWNER_REMATCH
        A.OWNER_REMATCH = 0  # the hourly re-match, now
        self.addCleanup(setattr, A, "OWNER_REMATCH", saved)
        back = self.r.worktree("movies-wt-back", base=self.off, branch="back")  # its commit turns up in a worktree
        A.save_dispatch({"branch": self.b70, "run_id": 3553, "worktree": self.wt, "source": "registered"})
        snap = self.snap()
        texts = A.settle_branch_reds(MOVIES, snap, self.store)
        self.assertEqual(sorted(t.split(" on ")[0] for t in texts),
                         [f"branch red reopened: {IDRUN} #2365", f"branch red reopened: {IDRUN} #2370"])
        self.assertEqual(self.store.item("branch-red", A.branch_red_id(MOVIES, 3553))["state"], "open")
        self.assertEqual(self.store.item("branch-red", A.branch_red_id(MOVIES, 3554))["state"], "orphaned")
        lines = A.route_branch_reds(MOVIES, snap, now_=time.time() + 2 * A.BRANCH_ROUTE_MAX_AGE)  # old, still routed
        self.assertEqual(len(lines), 2, lines)
        cwds = {os.path.realpath(x["cwd"]) for x in A.inbox_entries() if x["kind"] == "BRANCH_RED"}
        self.assertEqual(cwds, {os.path.realpath(back), os.path.realpath(self.wt)})
        self.assertEqual(len(A.branch_reds(snap)), 2)

    def test_an_owner_record_whose_worktree_was_removed_is_no_owner(self):
        """The status line, 2026-11-02: `branch reds: 8 → movies-wt-simplify (pre-commit check #29); ...` -- each red
        had an owner record naming a worktree removed days before, so it never closed as an orphan."""
        A.save_dispatch({"branch": self.b65, "run_id": 3548, "worktree": os.path.join(self.tmp.name, "movies-wt-gone"),
                         "source": "matched"})
        e = self.reds(self.snap())
        self.assertTrue(e[2365]["owner_gone"])
        self.assertIn("movies-wt-gone is gone (its worktree removed)", e[2365]["orphan"])
        self.assertIn("is not on main", e[2365]["orphan"])
        self.assertEqual(A.branch_reds(self.snap()), [])

    def test_a_gone_owner_s_red_moves_to_a_live_worktree_holding_its_commit(self):
        A.save_dispatch({"branch": self.b65, "run_id": 3548, "worktree": os.path.join(self.tmp.name, "movies-wt-gone"),
                         "source": "matched"})
        back = self.r.worktree("movies-wt-heir", base=self.off, branch="heir")
        e = self.reds(self.snap())[2365]
        self.assertIsNone(e["orphan"])
        self.assertEqual(os.path.realpath(e["owner"]["worktree"]), os.path.realpath(back))
        self.assertEqual(len(A.branch_reds(self.snap())), 1)

    def test_it_is_gone_when_its_branch_is_deleted(self):
        A.settle_branch_reds(MOVIES, self.snap(), self.store)
        A.branch_exists = lambda o_r, b: b != self.b65
        texts = A.settle_branch_reds(MOVIES, self.snap(), self.store)
        self.assertEqual(texts, [f"branch red gone: {IDRUN} #2365 on {self.b65} (its branch deleted or green)"])
        self.assertEqual(self.store.item("branch-red", A.branch_red_id(MOVIES, 3548))["state"], "gone")

    def test_its_finished_measure_branch_is_deleted_by_cleanup_an_open_one_is_not(self):
        for b in (self.b65, self.b70):
            sh(self.r.path, "git", "push", "-q", "origin", f"HEAD:refs/heads/{b}")
        A.settle_branch_reds(MOVIES, self.snap(), self.store)
        A.save_dispatch({"branch": self.b70, "run_id": 3553, "worktree": self.wt, "source": "registered"})
        A.settle_branch_reds(MOVIES, self.snap(), self.store)  # #2370 reopened: its owner is back
        deleted = []
        saved = A.delete_remote_branch
        A.delete_remote_branch = lambda repo, o_r, name, sha: deleted.append(name)
        self.addCleanup(setattr, A, "delete_remote_branch", saved)
        ids = {self.b65: 3548, self.b70: 3553}
        out = []
        A.orphan_branches(self.r.path, MOVIES, False, out,
                          lambda b: [dict(brun(ids.get(b, 1), 1, self.sha, b, wf=IDRUN, wfile=IDRUN_FILE))],
                          store=self.store)
        self.assertEqual(deleted, [self.b65], out)
        self.assertTrue(any("its red closed as an orphan" in l for l in out), out)


class SweepOrphansTest(BranchBase):
    def test_a_finished_measure_branch_whose_owner_is_gone_is_deleted_others_listed_or_kept(self):
        for b in ("measure/pawel/aaaaaaaaa-1", "check/20261027-1-movies-wt-gone-1", "measure/pawel/bbbbbbbbb-2",
                  "measure/pawel/ccccccccc-3"):
            sh(self.r.path, "git", "push", "-q", "origin", f"HEAD:refs/heads/{b}")
        gone = os.path.join(self.tmp.name, "removed-wt")
        A.save_dispatch({"branch": "measure/pawel/aaaaaaaaa-1", "worktree": gone, "source": "registered"})
        A.save_dispatch({"branch": "check/20261027-1-movies-wt-gone-1", "worktree": gone, "source": "registered"})
        A.save_dispatch({"branch": "measure/pawel/bbbbbbbbb-2", "worktree": self.wt, "source": "registered"})
        A.save_dispatch({"branch": "measure/pawel/ccccccccc-3", "worktree": gone, "source": "registered"})
        deleted = []
        saved = A.delete_remote_branch
        A.delete_remote_branch = lambda repo, o_r, name, sha: deleted.append(name)
        self.addCleanup(setattr, A, "delete_remote_branch", saved)

        def runs_for(b):
            st = "in_progress" if b.endswith("-3") else "completed"
            return [dict(brun(90, 1, self.sha, b), status=st)]
        out = []
        found = A.orphan_branches(self.r.path, MOVIES, False, out, runs_for)
        self.assertEqual(deleted, ["measure/pawel/aaaaaaaaa-1"])
        self.assertEqual(sorted(found), ["check/20261027-1-movies-wt-gone-1", "measure/pawel/aaaaaaaaa-1"])
        self.assertTrue(any("check/20261027-1-movies-wt-gone-1" in l and "listed, kept" in l for l in out), out)
        out2, deleted[:] = [], []
        A.orphan_branches(self.r.path, MOVIES, True, out2, runs_for)
        self.assertEqual(deleted, [])
        self.assertTrue(any(l.startswith("  would delete OWNER GONE branch measure/pawel/aaaaaaaaa-1") for l in out2), out2)


if __name__ == "__main__":
    unittest.main()
