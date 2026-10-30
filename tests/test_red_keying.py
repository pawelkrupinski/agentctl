"""A red's key is its failure, read the same way in every run (2026-10-29: Verify scrape recording's GermanyConvergenceSpec
failure -- Bambi '(1989)' served as a B-Movie -- was three reds: #2406 keyed by a WARN line, #2481 by a
FileNotFoundException, #2493 by its `::error` annotation; the fix waiting under `Fixes-Red: 2406` never held #2493's)."""
import json, os, re, sys, time, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402
from test_red import run, MOVIES  # noqa: E402
from test_reds import RedsBase  # noqa: E402

JOB = "convergence (germany)"
STEP = "Run the germany convergence suite"
WARN = ("19:22:09 WARN  tools.FallbackHttpFetch - FallbackHttpFetch All 2 backends failed for get "
        "https://www.wikidata.org/w/api.php?action=wbsearchentities&search=SURF%20FILM%20NACHT&language=de&uselang=de")
FNF = ("  enrichment-fixtures: FileNotFoundException: No fixture file for https://www.wikidata.org/w/api.php?"
       "action=wbsearchentities&search=SURF%20FILM%20NACHT&language=de&uselang=de&type=item&limit=15&format=json — tried:")
SPEC_A = ("::error title=GermanyConvergenceSpec.the Deutschland read model should serve every archived listing under the one "
          "film that holds it%2C and nothing else,file=e2e/src/test/scala/services/movies/GermanyConvergenceSpec.scala,"
          "line=1,endLine=1::Deutschland's served corpus does not match its archive:%0A1 film(s) served wrong")
SPEC_B = ("::error title=GermanyConvergenceSpec.the Deutschland pipeline should take the next day's listings,file=e2e/src/"
          "test/scala/services/movies/GermanyConvergenceSpec.scala,line=1,endLine=1::the next day differs")
SUMMARY = ("::error title=convergence (germany) FAILED::convergence (germany) FAILED — the other legs keep running and "
           "publish their own recordings")
# (run id, number, created, its failed step's slice) -- the slices as Forgejo served them
RUNS = [(3684, 2493, "2026-10-29T05:50:06Z", ["Cache restored from key: sbt-deps-linux-e9"]),
        (3664, 2481, "2026-10-29T04:17:37Z", [FNF]),
        (3589, 2406, "2026-10-28T19:12:03Z", [WARN, FNF])]
FIX_ENTRY = "20261029-012144-movies-wt-red-2406-112"


class RedKeyingTest(RedsBase):
    def setUp(self):
        super().setUp()
        runs = [run(rid, num, "Main", "main.yml", self.sha, conclusion="failure", created=c) for rid, num, c, _ in RUNS]
        self.put("runs.json", runs + [
            run(3467, 2297, "Main", "main.yml", self.sha, created="2026-10-28T07:54:00Z"),
            run(39, 19, "Identity model convergence", "identity-model-convergence.yml", self.sha)])
        for rid, num, _c, slice_ in RUNS:
            jid = rid * 10
            self.put(f"run-{rid}.json", {"jobs": [{"name": JOB, "status": "completed", "conclusion": "failure",
                                                   "databaseId": jid, "steps": [{"name": STEP, "conclusion": "failure"}]}],
                                         "headSha": self.sha, "url": "u", "number": num, "status": "completed",
                                         "conclusion": "failure", "workflowName": "Main"})
            self.put(f"run-{rid}.log", "".join(f"{JOB}\t{STEP}\t2026-10-28T19:22:09Z {l}\n" for l in slice_))
            self.put(f"run-{rid}-job-{jid}.log",
                     "".join(f"{JOB}\t{STEP}\t2026-10-28T19:22:09Z {l}\n" for l in slice_) +
                     "".join(f"{JOB}\tComplete job\t2026-10-28T19:30:52Z {l}\n" for l in (SPEC_A, SPEC_B, SUMMARY)))

    def stale_records(self):
        """The runsig records on the laptop at 2026-10-29, written by an older classifier."""
        for rid, num, line in ((3589, 2406, WARN), (3664, 2481, f"step failed: {STEP}"), (3684, 2493, SPEC_A)):
            A.write_json(A.reds_dir("runsig", f"{rid}.json"),
                         {"run": rid, "number": num, "workflow": "Main", "classifier": "0fa8d35b6e3b",
                          "sigs": [{"job": JOB, "kind": "test", "line": line, "sig": A.signature("Main", JOB, line)}]})

    def facts(self):
        return {"entries": [{"id": FIX_ENTRY, "status": "waiting", "fixes": ["2406"], "created": "2026-10-29T01:21:44Z"}],
                "landed": [], "key_runs": {}}

    def test_one_failure_reads_as_one_line_in_every_run(self):
        keys = {}
        for rid, num, _c, _s in RUNS:
            _lines, _n, per_job = A.explain_run_jobs(MOVIES, {"databaseId": rid}, 6)
            (job, kind, line), = per_job
            self.assertEqual((job, line), (JOB, SPEC_A[:300]), rid)
            keys[num] = A.red_key("Main", job, line)
        self.assertEqual(len(set(keys.values())), 1, keys)

    def test_a_strong_slice_line_still_wins(self):
        self.put("run-3664.log", f"{JOB}\t{STEP}\t2026-10-29T05:00:00Z [info] - FooSpec should bar *** FAILED ***\n")
        _l, _n, per_job = A.explain_run_jobs(MOVIES, {"databaseId": 3664}, 6)
        self.assertEqual(per_job[0][2], "[info] - FooSpec should bar *** FAILED ***")

    def test_the_classifier_version_covers_the_line_choice(self):
        v = A.classifier_version()
        for name in ("STRONG", "WEAK", "NOISE", "ANNOTATION"):
            old = getattr(A, name)
            try:
                setattr(A, name, re.compile(old.pattern + "|xyzzy"))
                self.assertNotEqual(A.classifier_version(), v, name)
            finally:
                setattr(A, name, old)
        self.assertEqual(A.classifier_version(), v)

    def test_the_streak_is_read_again_and_the_waiting_fix_holds_the_red(self):
        self.stale_records()
        A.red_tick(self.st, MOVIES, self.r.path, self.snap(), now_=self.t[0], dispatch=False, facts=self.facts())
        it = self.only()
        self.assertTrue({"2406", "3589", "2481", "3664", "2493", "3684"} <= set(it["data"]["runs"]), it["data"]["runs"])
        A.red_tick(self.st, MOVIES, self.r.path, self.snap(), now_=self.t[0], dispatch=False, facts=self.facts())
        it = self.only()
        self.assertEqual(it["state"], "fix-pending")
        self.assertEqual(it["data"]["fix"]["entry"], FIX_ENTRY)

    def test_stale_streak_runs_are_read_again_a_few_per_tick(self):
        self.stale_records()
        os.environ["AGENTCTL_STREAK_REREADS"] = "1"
        try:
            A.red_tick(self.st, MOVIES, self.r.path, self.snap(), now_=self.t[0], dispatch=False, facts={})
            first = set(self.only()["data"]["runs"])
            self.assertEqual(first, {"2493", "3684", "2481", "3664"})  # the newest stale one only
            A.red_tick(self.st, MOVIES, self.r.path, self.snap(), now_=self.t[0], dispatch=False, facts={})
            self.assertTrue({"2406", "3589"} <= set(self.only()["data"]["runs"]))
        finally:
            os.environ.pop("AGENTCTL_STREAK_REREADS", None)

    def test_reds_keyed_apart_merge_and_keep_the_fix_link(self):
        # the store at 2026-10-29: #2406's red (its WARN line) held by its waiting fix, #2493's red apart, leased
        self.stale_records()
        old_key = A.red_key("Main", JOB, WARN)
        self.st.put_item("red", old_key, "fix-pending", {
            "key": old_key, "workflow": "Main", "jobs": [JOB], "job": JOB, "kind": "test", "line": WARN,
            "red_kind": "gate", "gate": True, "runs": ["2406", "3589"], "last_run": {"number": 2406, "databaseId": 3589},
            "opened_at": self.t[0] - 36000, "attempts": 1, "history": [{"outcome": "LANDED 16a683a55"}],
            "refuted": ["deadbeef00"], "fix": {"entry": FIX_ENTRY, "status": "waiting"}})
        A.red_tick(self.st, MOVIES, self.r.path, self.snap(), now_=self.t[0], dispatch=False, facts=self.facts())
        reds = self.reds()
        self.assertEqual(reds[old_key]["state"], "cleared")
        new = self.only()
        self.assertNotEqual(new["data"]["key"], old_key)
        self.assertIn(old_key, new["data"]["merged_from"])
        self.assertIn("deadbeef00", new["data"]["refuted"])
        self.assertTrue({"2406", "3589", "2493"} <= set(new["data"]["runs"]))
        self.assertEqual(A.red_fix_of(new, self.facts())["entry"], FIX_ENTRY)
        A.red_tick(self.st, MOVIES, self.r.path, self.snap(), now_=self.t[0], dispatch=False, facts=self.facts())
        self.assertEqual(self.only()["state"], "fix-pending")


class FixerClaimsAnotherRedTest(RedsBase):
    """2026-10-29: inside a dispatched fixer, `red claim <N>` wrote the note on the fixer's own lease and ignored N, so
    the convergence family's fixer could not take #2493 beside its own #2527."""

    def fixer(self, session="s-fixer"):
        from test_reds import OwnerTest
        it, path = OwnerTest.held_by_a_fixer(self, session)
        wt = self.r.worktree("movies-wt-red-2527-2", branch="red-2527-2")
        A.write_json(path, {**A.read_record(path), "worktree": wt})
        os.environ.update(CLAUDE_CODE_SESSION_ID=session)
        return it, path, wt

    def other_red(self, key="d71a1822c3c7"):
        self.st.put_item("red", key, "offered", {
            "key": key, "workflow": "Verify scrape recording", "jobs": [JOB], "job": JOB, "kind": "test", "line": SPEC_A,
            "red_kind": "outside", "gate": False, "runs": ["2493", "3684"], "last_run": {"number": 2493, "databaseId": 3684},
            "opened_at": self.t[0], "attempts": 0, "history": []})
        return key

    def test_a_fixer_claims_a_second_red_by_its_key_or_run(self):
        it, path, wt = self.fixer()
        key = self.other_red()
        rc, out = self.tool("red", "claim", "2493", "--note", "the Bambi row too", "--repo", "movies", cwd=wt)
        self.assertEqual(rc, 0, out)
        self.assertIn("claimed Verify scrape recording #2493", out)
        row = self.st.get(A.red_lease(key))
        self.assertTrue(A.lease_is_live(row, time.time()), row)
        self.assertEqual(os.path.realpath(A.lease_meta(row)["worktree"]), os.path.realpath(wt))
        self.assertIsNone(A.read_record(path).get("note"), "N's claim is no note on the fixer's own lease")

    def test_its_own_red_still_gets_the_note(self):
        it, path, wt = self.fixer()
        rc, out = self.tool("red", "claim", "20", "--note", "on #20", "--repo", "movies", cwd=wt)
        self.assertEqual(rc, 0, out)
        self.assertIn("noted on your lease", out)
        self.assertEqual(A.read_record(path)["note"], "on #20")

    def test_a_family_sibling_under_its_lease_is_its_own(self):
        it, path, wt = self.fixer()
        key = self.other_red()
        primary = self.st.get(A.red_lease(it["data"]["key"]))
        off = self.st.offer_of(f"red:{key}") or A.red_put_offer(self.st, self.st.item("red", key))
        took = self.st.take(self.st.offer_of(f"red:{key}")["id"], primary["holder"], A.RED_FIXER_TTL, A.RED_FIXER_MAX,
                            meta={"kind": "sibling", "family": "convergence", "primary": primary["name"]})
        self.assertIn("epoch", took, off)
        rc, out = self.tool("red", "claim", "2493", "--note", "sibling", "--repo", "movies", cwd=wt)
        self.assertEqual(rc, 0, out)
        self.assertIn("noted on your lease", out)
        self.assertEqual(self.st.get(A.red_lease(key))["holder"], primary["holder"])


class FixerIdentityTest(RedsBase):
    """2026-10-29: the claude daemon (pid 31762) was spawned by #2473 fixer-5's wrapper, and every background session
    since inherits its environment -- AGENTCTL_AGENT_NAME=red-fixer-2473, AGENTCTL_RED_FIXER=2473,
    AGENTCTL_FIXER_RECORD=reds/2473/fixer-5.json -- so reds were 'RELEASED by red-fixer-2473' by whoever released them."""

    def stale_env(self, sid):
        os.makedirs(A.reds_dir("2473"), exist_ok=True)
        rec = A.reds_dir("2473", "fixer-5.json")
        A.write_json(rec, {"run": 2473, "workflow": "Identity model convergence", "session": "s-fixer-5",
                           "started": "2026-10-29T10:28:58Z", "lease": "red/a0762f14066e"})
        os.environ.update(AGENTCTL_AGENT_NAME="red-fixer-2473", AGENTCTL_RED_FIXER="2473", AGENTCTL_FIXER_RECORD=rec,
                          CLAUDE_CODE_SESSION_ID=sid)
        return rec

    def tearDown(self):
        for k in ("AGENTCTL_AGENT_NAME", "AGENTCTL_RED_FIXER", "AGENTCTL_FIXER_RECORD", "CLAUDE_CODE_SESSION_ID"):
            os.environ.pop(k, None)
        super().tearDown()

    def test_an_inherited_fixer_environment_names_nobody(self):
        self.stale_env("s-owner-1")
        self.assertIsNone(A.own_fixer_record_path())
        self.assertEqual(A.claimant(), "session s-owner-")
        self.assertFalse(A.in_fixer())  # its `land` queues like any session's
        self.tick()
        it = self.only()
        wt = self.r.worktree("movies-wt-owner", branch="owner")
        rc, out = self.tool("red", "claim", "20", "--repo", "movies", cwd=wt)
        self.assertEqual(rc, 0, out)
        self.assertIn("for session s-owner-", out)
        rc, out = self.tool("red", "release", "20", "--repo", "movies", cwd=wt)
        self.assertEqual(rc, 0, out)
        row = self.st.get(A.red_lease(it["data"]["key"]))
        self.assertEqual(A.lease_meta(row)["outcome"], "RELEASED by session s-owner-")

    def test_a_fixer_says_who_it_is_and_its_session_is_bound(self):
        self.stale_env("s-new-fixer")
        os.makedirs(A.reds_dir("2527"), exist_ok=True)
        mine = A.reds_dir("2527", "fixer-6.json")  # its wrapper has not yet seen its session id
        A.write_json(mine, {"run": 2527, "workflow": "Identity model convergence", "lease": "red/a0762f14066e",
                            "started": "2026-10-29T16:00:00Z"})
        self.assertIsNone(A.own_fixer_record_path())
        rc, out = self.tool("red", "claim", "2527", "--fixer", mine, "--note", "uk takes-drift", "--repo", "movies")
        self.assertEqual(rc, 0, out)
        self.assertIn("noted on your lease", out)
        self.assertEqual(A.own_fixer_record_path(), os.path.realpath(mine))
        self.assertEqual(A.claimant(), "red-fixer-2527-6")
        self.assertEqual(A.read_record(mine)["note"], "uk takes-drift")

    def test_a_session_cannot_bind_another_sessions_record(self):
        rec = self.stale_env("s-other")
        rc, out = self.tool("red", "claim", "2473", "--fixer", rec, "--repo", "movies")
        self.assertEqual(rc, 2, out)
        self.assertIsNone(A.own_fixer_record_path())

    def test_the_wrapper_starts_its_session_without_a_fixer_environment(self):
        self.stale_env("s-dispatcher")
        os.environ["FAKE_CLAUDE_SLEEP"] = "30"
        try:
            self.tick(dispatch=True)
            recs = lambda: [r for r in A.fixer_records() if r.get("run") == 20]
            t0 = time.time()
            while not (recs() and recs()[0].get("session")
                       and os.path.exists(A.fixer_session_path(recs()[0]["session"]))) and time.time() - t0 < 30:
                time.sleep(0.1)
            [rec] = recs()
            out = os.environ["FAKE_CLAUDE_OUT"]
            [started] = [json.load(open(os.path.join(out, n))) for n in os.listdir(out) if n.endswith(".json")]
            for k in A.FIXER_IDENTITY_ENV:
                self.assertNotIn(k, started["env"], k)
            self.assertIn(f"red claim 20 --fixer {rec['path']}", " ".join(started["argv"]))
            prompt = started["argv"][started["argv"].index("--bg") + 1]
            with open(A.FIXER_BRIEF) as f:
                self.assertIn(A.fixer_brief_for(f.read().strip(), A.RED_PRIMARY), prompt,
                              "the dispatched prompt carries docs/fixer-brief.md word for word (its repo's section only)")
            bound = A.read_record(A.fixer_session_path(rec["session"]))
            self.assertEqual(bound["record"], rec["path"])
        finally:
            os.environ["FAKE_CLAUDE_SLEEP"] = "0"


if __name__ == "__main__":
    unittest.main()
