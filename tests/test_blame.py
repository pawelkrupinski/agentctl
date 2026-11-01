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
            if v:
                A.write_json(out, {"verdict": v, "evidence": f"fake CI: {v} on {commit[:10]}"})
            me = os.getpid()  # no verdict scripted: a probe still running on CI (a live pid)
            return {"boundary": i, "commit": commit, "out": out, "pid": None if v else me,
                    "start": None if v else A.process_start(me), "at": time.time(), "kind": b["kind"]}
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

    def test_the_drift_probe_reads_identity_drift_ci_with_the_red_runs_seeds(self):
        script = os.path.join(self.tmp.name, "drift-ci")
        with open(script, "w") as f:
            f.write('#!/bin/sh\necho "$@" > "$0.args"\necho "row 1: ..."\necho "$FAKE_DRIFT"\n')
        os.chmod(script, os.stat(script).st_mode | stat.S_IEXEC)
        os.environ["AGENTCTL_DRIFT_CI"] = script
        try:
            for last, want in (("DRIFT CLEAN", "pass"), ("DRIFT RED: takes-drift-1 (poland)", "fail"),
                               ("DRIFT PROGRESS: fewer flips", "fail"), ("NO VERDICT: gh down", "none")):
                os.environ["FAKE_DRIFT"] = last
                self.assertEqual(A.probe_drift(self.r.path, self.sha, 20, 600)["verdict"], want, last)
            self.assertEqual(open(script + ".args").read().split(), [self.sha, "--for-red", "20", "--timeout", "10"])
        finally:
            os.environ.pop("AGENTCTL_DRIFT_CI")
            os.environ.pop("FAKE_DRIFT")


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
        self.assertFalse(A.lease_is_live(self.st.get("red/k1"), time.time()))

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
        self.st.update_item("red", it["id"], it["version"], {"blame": nb, "refuted": [rv]})  # as red_fix_pending does
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
