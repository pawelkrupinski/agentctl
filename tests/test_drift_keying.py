"""A takes-drift row's red is its flipped LISTINGS, not its seed or count (2026-11-01: #624, a Ladykillers flip, shared the
seed line of #595's Dracula flip, so it "refuted" the fixes landed for the Dracula flip and the #549 slice)."""
import json, os, sys, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402
from test_red import run, MOVIES  # noqa: E402
from test_reds import RedsBase  # noqa: E402

JOB = "takes-drift-1 (united-kingdom)"
STEP = "Run the drift check"
DRACULA = "Curzon Soho | Dracula (1931)"
LADY = "Everyman | The Ladykillers"


def seed_line(seed, n=1):
    return (f"::error title=identity drift (uk)::0 divergence(s) from a whole resolve and {n} flip(s) on the way, beyond "
            f"the allow-lists and 0 known flip(s); seed {seed}; reproduce: IdentityDrift uk --seed {seed}")


def job_log(listings, seed):
    out = ["2026-11-01T10:00:00.0000000Z ## uk: 900 listings, 2 seed(s), 0 divergence(s) (0 listings), 0 allow-listed",
           f"2026-11-01T10:00:01.0000000Z ## uk flips: {len(listings)} flip(s) ({len(listings)} listings), 0 allow-listed"]
    for l in listings:
        out += ["2026-11-01T10:00:02.0000000Z   1 listing(s)  flip: back to a film it left",
                f"2026-11-01T10:00:03.0000000Z       {l}: tmdb:11 [3 scrape: OwnMatch: own] -> tmdb:22 [4 answer] [seed {seed}]"]
    out.append("2026-11-01T10:00:04.0000000Z ## uk known flips: 2 known flip(s) remaining (2 listings in the baseline), 0 fixed")
    out.append("2026-11-01T10:00:05.0000000Z       Kino Known | Baselined: tmdb:1 [seed 1]")
    return "\n".join(out) + "\n"


class DriftKeyBase(RedsBase):
    def drift_run(self, rid, num, listings, seed, log=True, older=(), also=None, sha=None):
        """A failed run on main: Main's `takes-drift` rows, each job's log naming its listings. `also`: a second failed
        job's listings (takes-drift-2 (united-states))."""
        jobs = [(JOB, "uk", listings)] + ([("takes-drift-2 (united-states)", "us", also)] if also is not None else [])
        self.put("runs.json", [run(rid, num, "Main", "main.yml", sha or self.sha, conclusion="failure")] + list(older) + [
            run(30, 15, "Main", "main.yml", self.sha, created="2026-10-27T06:00:00Z"),
            run(39, 19, "Identity model convergence", "identity-model-convergence.yml", self.sha)])
        self.put(f"run-{rid}.json", {"jobs": [{"name": n, "status": "completed", "conclusion": "failure",
                                               "databaseId": rid * 10 + i, "steps": [{"name": STEP, "conclusion": "failure"}]}
                                              for i, (n, _cc, _l) in enumerate(jobs)],
                                     "headSha": sha or self.sha, "url": "u", "number": num, "status": "completed",
                                     "conclusion": "failure", "workflowName": "Main"})
        self.put(f"run-{rid}.log", "".join(f"{n}\t{STEP}\t2026-11-01T10:00:00Z {seed_line(seed, len(l))}\n"
                                           for n, _cc, l in jobs))
        for i, (n, cc, l) in enumerate(jobs):
            jid = rid * 10 + i
            self.put(f"run-{rid}-job-{jid}.log", "")
            if log:
                self.put(f"api-repos_pawelkrupinski_movies_actions_jobs_{jid}_logs.json", job_log(l, seed).replace("## uk", f"## {cc}"))

    def key_of(self, rid):
        _l, _n, per_job = A.explain_run_jobs(MOVIES, {"databaseId": rid}, 6)
        (job, _kind, line), *_ = per_job
        return A.red_key("Main", job, line), line

    def open_reds(self):
        return [it for it in self.st.items("red") if it["state"] not in A.RED_TERMINAL]

    def listed(self, word):
        return [it for it in self.st.items("red") if word in str(it["data"].get("line"))]


class DriftKeyTest(DriftKeyBase):
    def test_two_runs_with_different_flipped_listings_are_two_reds(self):
        self.drift_run(40, 20, [DRACULA], seed=1, also=[LADY])
        self.drift_run(44, 22, [LADY], seed=2)
        self.assertNotEqual(self.key_of(40)[0], self.key_of(44)[0])  # Dracula's key is not the Ladykillers'
        self.drift_run(40, 20, [DRACULA], seed=1, also=[LADY])
        self.tick()
        self.assertEqual(len(self.open_reds()), 2, [i["data"]["line"] for i in self.open_reds()])  # one red per listing

    def test_the_same_listing_on_different_seeds_is_one_red(self):
        self.drift_run(40, 20, [DRACULA], seed=1)
        self.drift_run(44, 22, [DRACULA], seed=9, older=[run(40, 20, "Main", "main.yml", self.sha, conclusion="failure")])
        k1, l1 = self.key_of(40)
        k2, l2 = self.key_of(44)
        self.assertEqual(k1, k2, (l1, l2))
        self.assertIn("uk:curzon soho|dracula", l1)
        self.assertNotIn("baselined", l1)  # the known-flips baseline's rows are no failure
        self.tick()
        it = self.only()
        self.assertTrue({"20", "22"} <= set(it["data"]["runs"]), it["data"]["runs"])

    def test_the_listing_set_is_order_free_and_a_big_set_is_a_stable_hash(self):
        a, b = [DRACULA, LADY], [LADY, DRACULA]
        self.drift_run(40, 20, a, seed=1)
        self.drift_run(44, 22, b, seed=2)
        self.assertEqual(self.key_of(40)[0], self.key_of(44)[0])
        many = [f"Venue {chr(97 + i)} | Film {chr(97 + i)}" for i in range(A.FLIP_KEY_MAX + 3)]
        self.drift_run(50, 24, many, seed=1)
        self.drift_run(54, 26, list(reversed(many)), seed=7)
        (k1, l1), (k2, l2) = self.key_of(50), self.key_of(54)
        self.assertEqual(k1, k2)
        self.assertLessEqual(len(l1), 80)
        self.drift_run(58, 28, many[:-1], seed=7)
        self.assertNotEqual(k1, self.key_of(58)[0])

    def fix_landed(self):
        wt = self.r.worktree("repo-wt-fix", branch="repo-wt-fix")
        self.r.write("fix.txt", "dracula\n", at=wt)
        sha = self.r.commit("fix the Dracula flip\n\nFixes-Red: 20", at=wt)
        self.r.push(at=wt, ref=f"{sha}:main")
        self.tick()
        self.assertEqual(self.only()["state"], "fix-pending")
        return sha

    def test_a_landed_fix_is_not_refuted_by_a_failure_of_another_listing(self):
        self.drift_run(40, 20, [DRACULA], seed=1)
        self.tick()
        sha = self.fix_landed()
        # main #23 contains the fix and fails on ANOTHER listing only
        self.drift_run(51, 23, [LADY], seed=5, older=[run(40, 20, "Main", "main.yml", self.sha, conclusion="failure")], sha=sha)
        self.tick()
        [dracula] = self.listed("dracula")
        self.assertEqual(dracula["data"].get("refuted") or [], [])
        self.assertEqual(dracula["state"], "verified")  # it no longer fails: the fix worked
        [lady] = [i for i in self.open_reds() if "ladykillers" in str(i["data"]["line"])]  # its own red, its own fixer
        self.assertEqual(lady["data"].get("refuted") or [], [])

    def test_a_landed_fix_is_refuted_by_a_failure_of_its_own_listing(self):
        self.drift_run(40, 20, [DRACULA], seed=1)
        self.tick()
        sha = self.fix_landed()
        self.drift_run(51, 23, [DRACULA], seed=6, also=[LADY],
                       older=[run(40, 20, "Main", "main.yml", self.sha, conclusion="failure")], sha=sha)
        self.tick()
        [dracula] = self.listed("dracula")
        self.assertEqual(dracula["data"]["refuted"], [sha])
        [lady] = self.listed("ladykillers")
        self.assertEqual(lady["data"].get("refuted") or [], [])

    def test_with_no_readable_log_the_key_falls_back_to_the_seed_line_and_says_so(self):
        self.drift_run(40, 20, [DRACULA], seed=1, log=False)
        lines, _n, per_job = A.explain_run_jobs(MOVIES, {"databaseId": 40}, 6)
        (_job, _kind, line), = per_job
        self.assertTrue(line.startswith("::error title=identity drift"), line)
        self.assertEqual(A.red_key("Main", JOB, line), A.red_key("Main", JOB, seed_line(1)))
        self.assertTrue(any("LOG UNREADABLE" in l and "flipped listings" in l for l in lines), lines)

    def test_a_log_naming_no_listing_falls_back_and_says_so(self):
        self.drift_run(40, 20, [], seed=1)
        lines, _n, per_job = A.explain_run_jobs(MOVIES, {"databaseId": 40}, 6)
        self.assertTrue(per_job[0][2].startswith("::error"), per_job)
        self.assertTrue(any("no flipped listing named" in l for l in lines), lines)

    def test_the_listings_are_a_sorted_set(self):
        self.assertEqual(A.flip_listings(job_log([LADY, DRACULA, LADY], 1)),
                         ["uk:curzon soho|dracula (1931)", "uk:everyman|the ladykillers"])

    def test_an_open_red_keyed_the_old_way_is_merged_into_the_listing_red_with_its_runs(self):
        self.drift_run(44, 22, [DRACULA], seed=3)
        old_key = A.red_key("Main", JOB, seed_line(1))
        self.st.put_item("red", old_key, "offered", {
            "key": old_key, "workflow": "Main", "job": JOB, "jobs": [JOB], "kind": "test", "line": seed_line(1)[:300],
            "runs": ["19", "22"], "classifier": "0ldc1a551f1e", "gate": True, "last_run": {"number": 22, "databaseId": 44}})
        self.tick()
        self.tick(advance=60)
        live = self.open_reds()
        self.assertEqual(len(live), 1, [(i["data"]["key"], i["data"]["line"]) for i in live])
        self.assertIn("dracula", live[0]["data"]["line"])
        self.assertTrue({"19", "22", "44"} <= set(live[0]["data"]["runs"]), live[0]["data"]["runs"])
        self.assertEqual(self.unheld(), [])  # no orphaned offer or lease

    def test_a_job_that_is_no_drift_row_is_keyed_as_ever(self):
        self.main_red(40, 20)
        _l, _n, per_job = A.explain_run_jobs(MOVIES, {"databaseId": 40}, 6)
        self.assertNotIn("flips:", per_job[0][2])

    def main_red(self, *a, **k):
        from test_reds import IdentityTest
        return IdentityTest.main_red(self, *a, **k)


if __name__ == "__main__":
    unittest.main()
