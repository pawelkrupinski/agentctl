"""THE CULPRIT OF A GATE RED (lib/agentctl/blame.py). The owner, 2026-11-01: "when a build fails: revert the offending
change and revive the landing agent to fix it", and "only if it can't be determined spinup a new agent". A change is
the culprit only when its parent passes the SAME check (same inputs) and its end fails; the parent failing too is
pre-existing (the normal fixer flow); infra, flakes and bots' changes are never blamed. Fake gh, fake claude, and the
CI probes faked at their one seam (blame_probe_start)."""
import json, os, stat, sys, time, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A, sh  # noqa: E402
from test_red import run, FAIL_LINE, MOVIES  # noqa: E402
from test_reds import RedsBase  # noqa: E402

CONV = "Identity model convergence"
DRIFT_JOB = "takes-drift-1 (poland)"
DRIFT_LINE = "[info] - IdentityDriftSpec should end where a whole resolve does *** FAILED ***"


class BlameBase(RedsBase):
    def setUp(self):
        super().setUp()
        self.verdicts = {}  # boundary commit -> pass | fail | none: what "CI" says when probed
        self.probed = []
        self.saved_probe = A.blame_probe_start

        def fake_probe(repo, o_r, d, b, i):
            commit = A.blame_boundary(b, i)
            self.probed.append((b["kind"], commit))
            out = os.path.join(self.tmp.name, f"probe-{len(self.probed)}.json")
            v = self.verdicts.get(commit)
            if v:  # a drift probe's verdict proves the red's own seeds (probe_drift's `seeds`), as the real one does
                A.write_json(out, {"verdict": v, "evidence": f"fake CI: {v} on {commit[:10]}", "kind": b["kind"],
                                   **({"seeds": True} if b["kind"] == "drift" and v != "none" else {})})
            me = os.getpid()  # no verdict scripted: a probe still running on CI (a live pid)
            return {"boundary": i, "commit": commit, "out": out, "pid": None if v else me,
                    "start": None if v else A.process_start(me), "at": A.epoch_now(), "kind": b["kind"]}
        A.blame_probe_start = fake_probe

    def tearDown(self):
        A.blame_probe_start = self.saved_probe
        super().tearDown()

    def land(self, name, files, lander=True, author=None):
        """One change landed on main: its commit, and (lander) its change item as `agentctl land` records it."""
        base = sh(self.r.path, "git", "rev-parse", "HEAD")
        for rel, text in files.items():
            self.r.write(rel, text)
        sh(self.r.path, "git", "add", "-A")
        sh(self.r.path, "git", "commit", "-q", "-m", f"{name}: change", *(["--author", author] if author else []))
        sha = sh(self.r.path, "git", "rev-parse", "HEAD")
        if lander:
            wt = os.path.join(self.tmp.name, f"repo-wt-{name}")
            os.makedirs(wt, exist_ok=True)
            self.st.put_item("change", f"ch-{name}", "landed", {
                "worktree": wt, "branch": name, "commit_keys": A.commit_keys(self.r.path, base, sha),
                "lander": {"session": f"sess-{name}", "worktree": wt, "branch": name, "repo": A.RED_PRIMARY,
                           "transcript": None}})
        return sha

    def red_at(self, green, head, wf="Main", wfile="main.yml", job="test (rest)", line=FAIL_LINE):
        self.r.push()
        self.put("runs.json", [run(40, 20, wf, wfile, head, conclusion="failure"),
                               run(30, 15, wf, wfile, green, created="2026-10-27T06:00:00Z")]
                 + ([run(39, 19, CONV, "identity-model-convergence.yml", green)] if wf == "Main" else
                    [run(29, 14, "Main", "main.yml", head)]))
        self.put("run-40.json", {"jobs": [{"name": "build", "status": "completed", "conclusion": "success"},
                                          {"name": job, "status": "completed", "conclusion": "failure",
                                           "steps": [{"name": "Run tests", "conclusion": "failure"}]}],
                                 "headSha": head, "url": "u", "number": 20, "status": "completed",
                                 "conclusion": "failure", "workflowName": wf})
        self.put("run-40.log", f"{job}\tRun tests\t2026-10-27T08:00:00Z {line}\n")

    def blame(self):
        return self.only()["data"].get("blame") or {}


class SingleChangeTest(BlameBase):
    def test_main_red_after_one_change_whose_parent_main_run_is_green_blames_it_at_once(self):
        g = self.sha
        h = self.land("a", {"a.txt": "a\n"})
        self.red_at(g, h)
        self.tick()
        b = self.blame()
        self.assertEqual(b["state"], "confirmed", b)
        self.assertEqual(b["groups"][b["culprit"]]["change"], "ch-a")
        self.assertEqual(b["parent"], g)
        self.assertEqual(self.probed, [])  # the parent's own green Main run is the same check: no CI probe
        self.assertEqual(A.blame_decision(b)[0], "lander")
        self.assertEqual(self.only()["data"]["tier"], "lander")
        self.assertTrue(any("RED BLAME" in e["verdict"] and "ch-a" in e["verdict"] for e in A.inbox_entries()))

    def test_a_change_landed_by_hand_is_blamed_on_the_owner(self):
        g = self.sha
        h = self.land("hand", {"h.txt": "h\n"}, lander=False)
        self.red_at(g, h)
        self.tick()
        b = self.blame()
        self.assertEqual((b["state"], b["groups"][0]["change"]), ("confirmed", None))
        self.assertEqual(A.blame_decision(b)[0], "owner")
        self.assertEqual(self.only()["data"]["tier"], "lander")  # reverted all the same; the owner told (revert piece)

    def test_a_bots_change_is_never_blamed(self):
        g = self.sha
        h = self.land("bump", {"deps.txt": "2\n"}, author="dependabot[bot] <49699333+dependabot[bot]@users.noreply.github.com>")
        self.red_at(g, h)
        self.tick()
        b = self.blame()
        self.assertEqual(b["state"], "undetermined")
        self.assertIn("bot", b["why"])
        self.assertEqual(A.blame_decision(b)[0], "fixer")
        self.assertNotIn(self.only()["data"]["tier"], ("lander", "blame"))

    def test_an_infra_red_is_never_blamed(self):
        g = self.sha
        h = self.land("i", {"i.txt": "i\n"})
        self.r.push()
        self.main_red(40, 20, kind="infra")
        self.put("run-40.json", dict(json.load(open(os.path.join(self.d, "run-40.json"))), headSha=h))
        self.tick()
        self.assertEqual(self.blame()["state"], "none")
        self.assertEqual(self.only()["data"]["tier"], "infra")
        self.assertTrue(g)


class DriftTest(BlameBase):
    def test_a_drift_red_is_judged_on_the_parent_with_the_red_runs_own_seeds_and_a_failing_parent_is_pre_existing(self):
        g = self.sha
        h = self.land("d", {"identity/x.scala": "x\n"})
        self.red_at(g, h, wf=CONV, wfile="identity-model-convergence.yml", job=DRIFT_JOB, line=DRIFT_LINE)
        self.verdicts[g] = "fail"  # the parent fails the same drift rows on the same seeds: not this change's doing
        self.tick()
        self.assertEqual(self.probed, [("drift", g)])  # never the parent's green run: its seeds were others
        self.tick()
        b = self.blame()
        self.assertEqual(b["state"], "undetermined", b)
        self.assertIn("pre-existing", b["why"])
        self.assertEqual(A.blame_decision(b)[0], "fixer")
        self.assertNotIn(self.only()["data"]["tier"], ("lander", "blame"))

    def test_a_drift_red_whose_parent_passes_on_its_seeds_blames_the_change(self):
        g = self.sha
        h = self.land("d", {"identity/x.scala": "x\n"})
        self.red_at(g, h, wf=CONV, wfile="identity-model-convergence.yml", job=DRIFT_JOB, line=DRIFT_LINE)
        self.verdicts[g] = "pass"
        self.tick()
        self.tick()
        b = self.blame()
        self.assertEqual((b["state"], b["groups"][b["culprit"]]["change"]), ("confirmed", "ch-d"))

    def test_an_identity_takes_drift_red_is_judged_like_a_convergence_one(self):
        """2026-11-08: the takes-drift rows moved to Identity takes: its drift red is probed with the red run's own
        seeds (never precommit-check), and a parent passing on them blames the change."""
        g = self.sha
        h = self.land("d", {"identity/x.scala": "x\n"})
        self.red_at(g, h, wf="Identity takes", wfile="identity-takes.yml", job=DRIFT_JOB, line=DRIFT_LINE)
        self.verdicts[g] = "pass"
        self.tick()
        self.assertEqual(self.probed, [("drift", g)])
        self.tick()
        b = self.blame()
        self.assertEqual((b["state"], b["groups"][b["culprit"]]["change"]), ("confirmed", "ch-d"))

    def test_while_the_culprit_is_decided_no_fixer_is_dispatched_and_the_offer_says_why(self):
        g = self.sha
        h = self.land("d", {"identity/x.scala": "x\n"})
        self.red_at(g, h, wf=CONV, wfile="identity-model-convergence.yml", job=DRIFT_JOB, line=DRIFT_LINE)
        self.tick(dispatch=True)  # the probe never answers (no verdict yet)
        self.tick(dispatch=True, advance=30)
        it = self.only()
        self.assertEqual((it["state"], it["data"]["tier"]), ("offered", "blame"))
        self.assertEqual(A.fixer_records(), [])
        self.assertIn("deciding the culprit", self.st.offer_of(f"red:{it['id']}")["reason"])
        os.environ["AGENTCTL_BLAME_MAX"] = "60"  # undecided past its bound: a fresh fixer's
        try:
            self.tick(advance=120)
        finally:
            os.environ.pop("AGENTCTL_BLAME_MAX")
        b = self.blame()
        self.assertEqual(b["state"], "undetermined")
        self.assertIn("no decision", b["why"])

    def drift_script(self):
        script = os.path.join(self.tmp.name, "drift-ci")
        with open(script, "w") as f:  # identity-drift-ci's shape: its pins on stderr, each row's seed, the verdict last
            f.write('#!/bin/sh\necho "$@" > "$0.args"\n'
                    'echo "identity-drift-ci: run 20\'s red drift rows: takes-drift-1 (poland) -> countries pl, seeds 7, '
                    '(the red\'s own)" >&2\n'
                    'echo "takes-drift-1 (poland) [seed $FAKE_SEED]: failure -- 0 divergence(s)"\necho "$FAKE_DRIFT"\n')
        os.chmod(script, os.stat(script).st_mode | stat.S_IEXEC)
        return script

    def old_parent(self):
        """An old commit, then main's CI changed after it (its drift script ignores a forced seed)."""
        old = self.sha
        self.r.write("scripts/ci/drift_seeds.py", "honours forced seeds\n")
        self.r.write(".github/workflows/identity-model-convergence.yml", "drift-seeds input\n")
        self.r.commit("ci: forced seeds")
        self.r.push()
        return old

    def test_the_drift_probe_runs_the_parents_code_under_mains_ci_with_the_red_runs_seeds(self):
        old = self.old_parent()
        os.environ.update(AGENTCTL_DRIFT_CI=self.drift_script(), FAKE_SEED="7")
        try:
            for last, want in (("DRIFT CLEAN", "pass"), ("DRIFT RED: takes-drift-1 (poland)", "fail"),
                               ("DRIFT PROGRESS: fewer flips", "fail"), ("NO VERDICT: gh down", "none")):
                os.environ["FAKE_DRIFT"] = last
                self.assertEqual(A.probe_drift(self.r.path, old, 20, 600)["verdict"], want, last)
            args = open(os.path.join(self.tmp.name, "drift-ci.args")).read().split()
            probe = args[0]
            self.assertEqual(args[1:], ["--for-red", "20", "--timeout", "10"])
            self.assertNotEqual(probe, old)  # never the old commit itself
            self.assertEqual(sh(self.r.path, "git", "rev-parse", f"{probe}^"), old)
            self.assertEqual(sh(self.r.path, "git", "show", f"{probe}:scripts/ci/drift_seeds.py"), "honours forced seeds")
            self.assertEqual(sh(self.r.path, "git", "diff", "--name-only", old, probe).split(),
                             [".github/workflows/identity-model-convergence.yml", "scripts/ci/drift_seeds.py"])
        finally:
            for k in ("AGENTCTL_DRIFT_CI", "FAKE_DRIFT", "FAKE_SEED"):
                os.environ.pop(k, None)

    def test_the_overlay_works_outside_a_checkout_of_the_old_commit(self):
        """`git rm --cached` refuses a path whose staged content differs from both the file and HEAD -- every path
        under scripts/ci/ main changed since the old commit, when the clone is not checked out at it (ci-runner's
        and the laptop's clones never are): it needs -f, else the overlay was None and the probe no verdict."""
        self.r.write("scripts/ci/drift_seeds.py", "ignores forced seeds\n")
        self.r.write(".github/workflows/identity-model-convergence.yml", "no drift-seeds input\n")
        self.r.commit("ci: the old drift script")
        old = sh(self.r.path, "git", "rev-parse", "HEAD")
        self.r.write("scripts/ci/drift_seeds.py", "honours forced seeds\n")
        self.r.write(".github/workflows/identity-model-convergence.yml", "drift-seeds input\n")
        self.r.write("identity/x.scala", "main's code\n")
        self.r.commit("ci: forced seeds")
        self.r.push()
        self.r.write("scripts/ci/drift_seeds.py", "a dirty worktree edit\n")  # neither old, HEAD nor main
        probe = A.drift_overlay(self.r.path, old)
        self.assertIsNotNone(probe)
        self.assertEqual(sh(self.r.path, "git", "rev-parse", f"{probe}^"), old)
        self.assertEqual(sh(self.r.path, "git", "show", f"{probe}:scripts/ci/drift_seeds.py"), "honours forced seeds")
        self.assertEqual(sh(self.r.path, "git", "show", f"{probe}:.github/workflows/identity-model-convergence.yml"),
                         "drift-seeds input")
        self.assertNotIn("identity/x.scala", sh(self.r.path, "git", "ls-tree", "-r", "--name-only", probe))  # old code

    def test_a_row_that_replayed_another_seed_is_no_verdict_never_a_pass(self):
        old = self.old_parent()
        os.environ.update(AGENTCTL_DRIFT_CI=self.drift_script(), FAKE_SEED="1-2", FAKE_DRIFT="DRIFT CLEAN")
        try:
            v = A.probe_drift(self.r.path, old, 20, 600)
        finally:
            for k in ("AGENTCTL_DRIFT_CI", "FAKE_DRIFT", "FAKE_SEED"):
                os.environ.pop(k, None)
        self.assertEqual(v["verdict"], "none")
        self.assertIn("row 1 replayed seed 1-2, not 7", v["evidence"])
        self.assertEqual(A.drift_seed_check("no pins here\ntakes-drift-1 (poland) [seed 7]: ok"),
                         "the run never said which seeds it pinned")


class Convergence629Test(BlameBase):
    """THE FIRST LIVE DECISION, WRONG (2026-11-01): convergence #629 failed `leg (united-kingdom) / takes-drift-2
    (united-kingdom)` on seed 42 (a Keswick Alhambra flip). DRIFT_JOB (`^takes-drift`) missed GitHub's prefixed job
    name, the kind came out None, and blame_free took #628 -- green on the parent 8dfe177522 with OTHER rotating seeds
    -- as the parent passing: change ...-retained-input was reverted and its lander revived. The flip was pre-existing
    (the parent, re-run with seed 42 forced, is red). Replayed here: never a revert on that evidence."""
    JOB = "leg (united-kingdom) / takes-drift-2 (united-kingdom)"
    LINE = ("##[error]0 divergence(s) from a whole resolve and 1 flip(s) on the way, beyond the allow-lists and 0 known "
            "flip(s) -- seed 42 -- each named in the step log; reproduce: IdentityDrift uk --seed 42")

    def replay(self, job=JOB):
        parent = self.sha
        head = self.land("retained-input", {"identity/ListingRetention.scala": "restored\n"})
        self.r.push()
        wf, wfile = CONV, "identity-model-convergence.yml"
        self.put("runs.json", [run(41341105019, 629, wf, wfile, head, conclusion="failure",
                                   created="2026-11-01T19:18:54Z"),
                               run(41330000628, 628, wf, wfile, parent, created="2026-11-01T18:30:00Z"),
                               run(41330000001, 300, "Main", "main.yml", head, created="2026-11-01T19:10:00Z")])
        self.put("run-41341105019.json", {
            "jobs": [{"name": "preflight", "status": "completed", "conclusion": "success"},
                     {"name": "leg (united-kingdom) / takes-drift-1 (united-kingdom)", "status": "completed",
                      "conclusion": "success"},
                     {"name": job, "status": "completed", "conclusion": "failure",
                      "steps": [{"name": "Check that the incremental state ends where a whole resolve does",
                                 "conclusion": "failure"}]}],
            "headSha": head, "url": "u", "number": 629, "status": "completed", "conclusion": "failure",
            "workflowName": wf})
        self.put("run-41341105019.log", f"{job}\tCheck that the incremental state ends where a whole resolve does\t"
                                        f"2026-11-01T19:50:00Z {self.LINE}\n")
        return parent, head

    def assert_never_reverted(self):
        b = self.blame()
        self.assertNotEqual(b.get("state"), "confirmed", b)
        self.assertNotIn("revert", b)
        self.assertNotEqual(A.blame_decision(b)[0], "lander")
        self.assertNotIn(self.only()["data"]["tier"], ("lander", "owner"))
        for p in b.get("probes") or []:
            self.assertNotIn("#628", str(p.get("evidence")), "a green run on other seeds is never evidence")

    def test_the_real_job_name_is_a_drift_row_and_the_parent_is_replayed_with_the_reds_seeds(self):
        self.assertTrue(A.is_drift_job(self.JOB))
        self.assertTrue(A.is_drift_job("takes-drift-1 (germany)"))
        self.assertFalse(A.is_drift_job("leg (united-kingdom) / takes (united-kingdom)"))
        parent, _ = self.replay()
        self.tick()
        self.assertEqual(self.blame()["kind"], "drift")
        self.assertEqual(self.probed, [("drift", parent)])  # never #628's green: the parent re-run on seed 42
        self.assert_never_reverted()

    def test_the_parent_red_on_the_reds_own_seed_is_pre_existing_never_a_revert(self):
        parent, _ = self.replay()
        self.verdicts[parent] = "fail"  # run 41345615165: the parent with seed 42 forced is red with the same flip
        self.tick()
        self.tick()
        b = self.blame()
        self.assertEqual(b["state"], "undetermined", b)
        self.assertIn("pre-existing", b["why"])
        self.assert_never_reverted()

    def test_an_unknown_kind_is_undetermined_at_once_never_a_revert(self):
        self.replay(job="leg (united-kingdom) / takes (united-kingdom)")  # a leg no CI check repeats
        self.tick()
        self.tick()
        b = self.blame()
        self.assertEqual((b["state"], b["kind"]), ("undetermined", None), b)
        self.assertEqual(self.probed, [])
        self.assertEqual(A.blame_decision(b)[0], "fixer")
        self.assert_never_reverted()

    def test_a_pass_that_did_not_prove_the_reds_seeds_never_confirms(self):
        b = {"state": "probing", "kind": "drift", "base": "p" * 40, "lo": 0, "hi": 1,
             "groups": [{"change": "ch", "commits": ["h" * 40], "end": "h" * 40}],
             "probes": [{"boundary": 0, "verdict": "pass", "evidence": "Identity model convergence #628 green on pppp"}]}
        d = {"workflow": CONV}
        self.assertIn("never re-run with the red's own seeds", A.blame_unproven(d, b))
        b["probes"][0].update(ci=True, kind="drift", seeds=True)
        self.assertIsNone(A.blame_unproven(d, b))
        self.assertIn("not a takes-drift row", A.blame_unproven(d, dict(b, kind=None)))
        self.assertIsNone(A.blame_free("o/r", d, dict(b, kind="run"), 0, [run(1, 628, CONV, "x", "p" * 40)]))


class BatchTest(BlameBase):
    def test_a_batch_is_bisected_from_the_failing_tests_change_and_its_parent_judged_on_ci(self):
        g = self.sha
        a = self.land("a", {"a.txt": "a\n"})
        b_ = self.land("b", {"web/src/test/scala/FooSpec.scala": "class FooSpec\n"})
        c = self.land("c", {"c.txt": "c\n"})
        self.red_at(g, c)
        self.verdicts.update({a: "pass", b_: "fail"})  # b's parent passes, its end fails
        for _ in range(4):
            self.tick()
        b = self.blame()
        self.assertEqual(b["hint"], 1)  # FooSpec's change
        self.assertEqual(self.probed, [("checks", a), ("checks", b_)])
        self.assertEqual((b["state"], b["groups"][b["culprit"]]["change"]), ("confirmed", "ch-b"), b)

    def test_a_probe_with_no_verdict_leaves_it_undetermined(self):
        g = self.sha
        a = self.land("a", {"a.txt": "a\n"})
        c = self.land("c", {"c.txt": "c\n"})
        self.red_at(g, c)
        self.verdicts[a] = "none"
        for _ in range(3):
            self.tick()
        b = self.blame()
        self.assertEqual(b["state"], "undetermined")
        self.assertIn("no verdict", b["why"])


if __name__ == "__main__":
    unittest.main()


class RevertTest(BlameBase):
    """Piece 3: a determined culprit is reverted (Reverts-Change + Fixes-Red), landed as a gate fix."""

    def setUp(self):
        super().setUp()
        self.lands = []
        self.saved_land = A.spawn_land
        A.spawn_land = lambda wt, env, log: self.lands.append(wt) or 4242
        os.environ["FAKE_CLAUDE_SLEEP"] = "30"

    def tearDown(self):
        A.spawn_land = self.saved_land
        os.environ["FAKE_CLAUDE_SLEEP"] = "0"
        super().tearDown()

    def red_one(self, lander=True):
        g = self.sha
        h = self.land("a", {"a.txt": "a\n"}, lander=lander)
        self.red_at(g, h)
        return h

    def test_the_culprit_is_reverted_with_its_trailers_and_landed_through_land(self):
        h = self.red_one()
        self.tick(dispatch=True)
        [wt] = self.lands
        msg = sh(wt, "git", "log", "-1", "--format=%B")
        self.assertIn("Reverts-Change: ch-a", msg)
        self.assertIn("Fixes-Red: 20", msg)
        self.assertIn(f"This reverts commit {h}.", msg)  # what prepush-check's revert exemption reads
        self.assertIn("a.txt", A.reverted_commits(wt, "origin/main", "HEAD"))  # prepush-check exempts it
        self.assertEqual(sh(wt, "git", "rev-parse", "HEAD^"), h)  # on main, the one revert commit
        self.assertFalse(os.path.exists(os.path.join(wt, "a.txt")))
        it = self.only()
        self.assertTrue(it["data"]["lander_tried"])
        self.assertTrue(it["data"]["blame"]["revert"]["ok"])
        self.assertTrue(any(e["verdict"].startswith("RED REVERT") and "ch-a" in e["verdict"] for e in A.inbox_entries()))
        self.assertIn("reverted ch-a", A.red_view(self.st, it)[1])  # the status line says so

    def test_the_reverts_fixes_red_passes_the_gates_fix_replay_rule(self):
        """2026-11-21 Main #2806-#2808: blame's revert of the culprit was refused by FIX REPLAY ("its Fixes-Red needs
        `Fix-Replay: <command>`"), and main stayed red ~1 h until a hand revert carrying one landed."""
        self.red_one()
        self.tick(dispatch=True)
        [wt] = self.lands
        self.assertTrue(self.only()["data"].get("gate"), "Main's red is a gate red: FIX REPLAY is enforced for it")
        commits = A.waiting_commits(wt, "origin/main", "HEAD")
        self.assertIsNone(A.fix_replay_refusal(wt, commits, self.st))
        [reason] = A.fix_replay_trailers(wt, commits)[3]
        self.assertIsNone(A.fix_replay_none_lint(reason), reason)
        self.assertIn("ch-a", reason)  # specific: the owner's weekly list names the change it reverted

    def test_an_owners_change_is_reverted_and_the_owner_told_no_agent_started(self):
        self.red_one(lander=False)
        self.tick(dispatch=True)
        self.assertEqual(len(self.lands), 1)
        self.assertEqual(A.fixer_records(), [])
        self.assertTrue(any("OWNER: it was yours" in e["verdict"] for e in A.inbox_entries()))

    def test_a_revert_that_conflicts_falls_back_to_a_fresh_fixer_briefed_with_the_change(self):
        self.red_one()
        self.r.write("a.txt", "a changed since\n")
        self.r.commit("later: touch a.txt")
        self.r.push()
        self.tick(dispatch=True)
        self.assertEqual(self.lands, [])
        [rec] = A.fixer_records()
        self.assertIn("THE OFFENDING CHANGE is change ch-a", rec["blame_note"])
        self.assertIn("could not be landed", rec["blame_note"])
        self.assertFalse(self.only()["data"]["blame"]["revert"]["ok"])

    def test_the_revived_landers_lease_survives_the_revert_its_fix_but_not_a_refuted_revert(self):
        self.st.acquire("red/k1", "laptop:fixer:x", 900, 3600, meta={"kind": "fixer", "tier": "lander"})
        self.assertIsNone(A.red_stop_holder(self.st, "k1", "its fix is on its way", "fix submitted"))
        self.assertTrue(A.lease_is_live(self.st.get("red/k1"), self.t[0]))
        self.assertEqual(A.red_stop_holder(self.st, "k1", "refuted", "fix refuted", force=True), "laptop:fixer:x")
        self.assertFalse(A.lease_is_live(self.st.get("red/k1"), A.epoch_now()))

    def test_a_refuted_revert_relands_the_change_before_the_normal_fixer_starts(self):
        self.red_one()
        self.tick(dispatch=True)
        [wt] = self.lands
        self.r.push(at=wt)  # the revert landed
        rv = sh(self.r.path, "git", "rev-parse", "origin/main")
        b = self.only()["data"]["blame"]
        self.assertIsNone(A.blame_refuted(self.r.path, b, self.sha))  # not its revert: no reland
        nb = A.blame_refuted(self.r.path, b, rv)
        self.assertEqual((nb["state"], nb["reland_due"]), ("refuted", rv))
        self.assertEqual(A.blame_decision(nb)[0], "fixer")  # the normal fixer flow
        it = self.only()
        self.st.update_item("red", it["id"], it["version"], {"blame": nb, "refuted": [rv]})  # as redpending.FixPending does
        it = self.st.item("red", it["id"])
        A.red_offer(self.st, it, "refuted")
        self.lands.clear()
        off = self.st.offer_of(f"red:{it['id']}")
        for row in self.st.list("red/"):  # its first holder gone
            self.st.revoke(row["name"], "test")
        took = self.st.take(off["id"], "laptop:fixer:test", 900, 3600, meta={"kind": "fixer", "tier": off["tier"]})
        A.red_spawn(self.st, MOVIES, self.r.path, off, took)  # what the dispatcher starts for the next fixer
        [rl] = self.lands
        self.assertIn(f"Relands-Change: ch-a", sh(rl, "git", "log", "-1", "--format=%B"))
        self.assertTrue(os.path.exists(os.path.join(rl, "a.txt")))  # the change is back
        self.assertTrue(any(e["verdict"].startswith("RED RELAND") for e in A.inbox_entries()))


class ReviveBase(BlameBase):
    """Piece 4: the lander's own session revived -- resumed (a top-level session) or continued from its transcript (a
    subagent) -- in its worktree; a fresh fixer only when it cannot be (no session, transcript gone, launch refused)."""

    def setUp(self):
        super().setUp()
        self.lands = []
        self.saved_land = A.spawn_land
        A.spawn_land = lambda wt, env, log: self.lands.append(wt) or 4242
        os.environ["FAKE_CLAUDE_SLEEP"] = "30"
        self.projects = os.environ["AGENTCTL_CLAUDE_PROJECTS"]

    def tearDown(self):
        A.spawn_land = self.saved_land
        os.environ["FAKE_CLAUDE_SLEEP"] = "0"
        os.environ.pop("FAKE_CLAUDE_BG_FAIL", None)
        super().tearDown()

    def red_landed_by(self, agent=None, transcript=True):
        """A Main red whose one change was landed by session sess-a (or its subagent `agent`) from a real worktree."""
        g = self.sha
        wt = self.r.worktree("repo-wt-lander", branch="lander")
        h = self.land("a", {"a.txt": "a\n"})
        tr = os.path.join(self.projects, "-proj", "sess-a", "subagents", f"agent-{agent}.jsonl") if agent else \
            os.path.join(self.projects, "-proj", "sess-a.jsonl")
        if transcript:
            os.makedirs(os.path.dirname(tr), exist_ok=True)
            with open(tr, "w") as f:
                f.write(json.dumps({"cwd": wt, "type": "user"}) + "\n")
        it = self.st.item("change", "ch-a")
        self.st.update_item("change", "ch-a", it["version"], {"lander": {
            "session": "sess-a", "agent": agent, "transcript": tr, "session_cwd": wt, "worktree": wt, "branch": "lander",
            "repo": A.RED_PRIMARY}})
        self.red_at(g, h)
        return wt, tr

    def wait(self, cond, limit=30):
        t0 = time.time()
        while not cond() and time.time() - t0 < limit:
            time.sleep(0.1)
        return cond()

    def session_argv(self):
        """The launched session's record. fake_claude renames it (os.replace) when it refuses a resume, so a name listed
        may be gone when opened (push of 2026-11-26: FileNotFoundError on sess-a.json): the listing is read again."""
        out, got = os.environ["FAKE_CLAUDE_OUT"], []

        def read():
            names = [n for n in os.listdir(out) if n.endswith(".json")] if os.path.isdir(out) else []
            try:
                got[:] = [json.load(open(os.path.join(out, n))) for n in names]
            except FileNotFoundError:  # renamed between the listing and the open: look again
                got[:] = []
            return len(got) == 1
        self.assertTrue(self.wait(read))
        return got[0]



class ReviveTest(ReviveBase):
    def test_a_top_level_landers_own_session_is_resumed_in_its_cwd_and_holds_the_reds_lease(self):
        wt, _tr = self.red_landed_by()
        self.tick(dispatch=True)
        self.assertEqual(len(self.lands), 1)  # reverted first
        [rec] = A.fixer_records()
        self.assertEqual((rec["resumed_from"], rec["resume"], rec["worktree"]), ("sess-a", True, wt))
        launched = self.session_argv()
        argv = launched["argv"]
        self.assertEqual(argv[argv.index("--resume") + 1], "sess-a")
        self.assertEqual(os.path.realpath(launched["cwd"]), os.path.realpath(wt))
        prompt = argv[argv.index("--bg") + 1]
        self.assertIn("agentctl REVIVED YOU: your change ch-a", prompt)
        self.assertIn("IT WAS REVERTED", prompt)
        self.assertIn("land again", prompt)
        self.assertIn("test (rest)", prompt)  # the failing step
        it = self.only()
        row = self.st.get(A.red_lease(it["data"]["key"]))
        self.assertEqual(A.lease_meta(row)["tier"], "lander")
        self.assertTrue(it["data"]["blame"]["revived"])
        self.assertTrue(any(e["verdict"].startswith("LANDER REVIVED") for e in A.inbox_entries()))
        self.assertIn("lander revived", A.red_view(self.st, it)[1])

    def test_a_subagent_lander_continues_from_its_transcript_in_its_worktree(self):
        wt, tr = self.red_landed_by(agent="abc123")
        self.tick(dispatch=True)
        [rec] = A.fixer_records()
        self.assertFalse(rec["resume"])  # a subagent has no session of its own to resume
        argv = self.session_argv()["argv"]
        self.assertNotIn("--resume", argv)
        prompt = argv[argv.index("--bg") + 1]
        self.assertIn(f"continued from your transcript {tr}", prompt)
        self.assertEqual(rec["worktree"], wt)

    def test_a_lander_whose_transcript_is_gone_gets_a_fresh_fixer_briefed_with_the_change(self):
        self.red_landed_by(transcript=False)
        self.tick(dispatch=True)
        [rec] = A.fixer_records()
        self.assertIsNone(rec.get("lander"))
        self.assertIn("transcript is gone", rec["blame_note"])
        self.assertIn("It was REVERTED", rec["blame_note"])

    def test_a_refused_launch_is_said_once_and_a_fresh_fixer_follows_at_once(self):
        self.red_landed_by()
        os.environ["FAKE_CLAUDE_BG_FAIL"] = "Workspace not trusted. Run `claude` in the repo once"
        self.tick(dispatch=True)
        self.assertTrue(self.wait(lambda: (A.fixer_records() or [{}])[0].get("ended")))
        os.environ.pop("FAKE_CLAUDE_BG_FAIL")
        fails = [e for e in A.inbox_entries() if e["verdict"].startswith("LAUNCH FAILED")]
        self.assertEqual(len(fails), 1)
        self.assertIn("Workspace not trusted", fails[0]["verdict"])
        self.t[0] = A.epoch_now()
        self.tick(dispatch=True)
        it = self.only()
        self.assertIn("Workspace not trusted", it["data"]["blame"]["revive_failed"])
        recs = sorted(A.fixer_records(), key=lambda r: r["path"])
        self.assertEqual(len(recs), 2, [r.get("why") for r in recs])
        fresh = [r for r in recs if not r.get("lander")]
        self.assertEqual(len(fresh), 1)
        self.assertIn("could not be revived", fresh[0]["blame_note"])
        self.assertEqual(len(self.lands), 1)  # reverted once, never again


class ConflictReviveTest(ReviveBase):
    def test_a_revert_that_conflicts_revives_the_lander_to_fix_forward(self):
        self.red_landed_by()
        self.r.write("a.txt", "a changed since\n")
        self.r.commit("later: touch a.txt")
        self.r.push()
        self.tick(dispatch=True)
        self.assertEqual(self.lands, [])  # nothing reverted
        [rec] = A.fixer_records()
        self.assertTrue(rec["resume"])
        prompt = self.session_argv()["argv"]
        prompt = prompt[prompt.index("--bg") + 1]
        self.assertIn("COULD NOT BE REVERTED", prompt)
        self.assertIn("Fixes-Red: 20", prompt)
        self.assertTrue(any("could not be reverted" in e["verdict"] for e in A.inbox_entries()
                            if e["verdict"].startswith("LANDER REVIVED")))


class QuietTest(BlameBase):
    def test_deciding_the_culprit_is_no_escalation(self):
        g = self.sha
        h = self.land("d", {"identity/x.scala": "x\n"})
        self.red_at(g, h, wf=CONV, wfile="identity-model-convergence.yml", job=DRIFT_JOB, line=DRIFT_LINE)
        self.verdicts[g] = "pass"
        self.tick()
        self.tick()
        it = self.only()
        self.assertEqual((it["data"]["tier"], it["level"]), ("lander", 0))
        evs = [e for e in self.st.events(after=0) if e["item_type"] == "red"]
        self.assertFalse([e for e in evs if e["severity"] == "escalate"], evs)
