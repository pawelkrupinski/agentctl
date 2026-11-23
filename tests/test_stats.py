"""The pipeline statistics (stats.py, the owner 2026-11-17: "keep landing stats ... in victoria logs ... for easy
analysis", widened to reds, conditions, owner decisions and gate runs): one flat record per finished thing, DERIVED from
the store's items and events (and the CI collector's source:ci rows), sent once each to VictoriaLogs, the history
included (backfill: true)."""
import io, os, sys, tempfile, unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A, seed_gating_checks  # noqa: E402
from fake_clock import FakeClock, on_clock  # noqa: E402

T0 = 1_794_900_000.0


def store(t):
    return A.Store(path=os.path.join(tempfile.mkdtemp(), "store.db"), clock=lambda: t[0])


def step(st, t, typ, iid, to, secs, text="", **kw):
    t[0] += secs
    it = st.item(typ, iid)
    st.transition(typ, iid, to, it["version"], event={"text": text, **kw.pop("event", {})}, **kw)


def landed_change(st, t, iid="20261117-083612-movies-x-4a6494"):
    """A change's real path: a full round, main moves (build.sbt) -> a second full round, main moves again (no
    overlap) -> a verify round, then pushed and landed."""
    st.put_item("change", iid, "queued", {"submitted_at": t[0], "worktree": "/w", "head": "h1", "fixes": ["abc.1"],
                                          "lander": {"agent": "ag1", "session": "s1", "host": "mac", "branch": "b",
                                                     "repo": "pawelkrupinski/movies"},
                                          "commit_keys": [{"sha": "c1"}, {"sha": "c2"}]})
    step(st, t, "change", iid, "queued", 600, f"{iid}: queued for 10 min, waiting on a check slot")  # an escalation
    step(st, t, "change", iid, "checking", 100, f"{iid}: checking")
    step(st, t, "change", iid, "checked", 1200, f"{iid}: checked 11252d354d on dd16257623 (on ci-runner)")
    step(st, t, "change", iid, "queued", 5, f"{iid}: main moved by 2 commits since its checks, they change build.sbt: "
                                            "checked again")
    step(st, t, "change", iid, "checking", 30, f"{iid}: checking")
    step(st, t, "change", iid, "checked", 1000, f"{iid}: checked 20096ffa8e on 6900fa4c80 (on ci-runner)")
    step(st, t, "change", iid, "queued", 5, f"{iid}: main moved by 5 commits since its checks, no file overlap: its "
                                            "combined tree verified")
    step(st, t, "change", iid, "checking", 300, f"{iid}: checking")
    step(st, t, "change", iid, "checked", 600, f"{iid}: verified 4486939 on e67d38 (fully checked on 6900fa4c80)")
    step(st, t, "change", iid, "pushing", 20, f"{iid}: pushing 4486939")
    step(st, t, "change", iid, "landed", 3, "LANDED 4486939", data={"landed_sha": "4486939", "why": "LANDED 4486939"})
    return st.item("change", iid)


class LandingRecordTest(unittest.TestCase):
    def test_a_landed_change_gives_its_phases_rounds_with_reasons_runs_files_and_areas(self):
        t = [T0]
        st = store(t)
        it = landed_change(st, t)
        sub = T0
        runs = [{"run_id": "r1", "start": sub + 700 + 30, "path_queue_s": "300", "path_work_s": "800", "wall_s": "1100",
                 "jobs": [{"job": "plan", "queue_s": "5", "duration_s": "20"},
                          {"job": "check (identity 2/3)", "queue_s": "290", "duration_s": "700"},
                          {"job": "heavy rows (itrest)", "queue_s": "100", "duration_s": "300"}]},
                {"run_id": "r3", "start": sub + 3240 + 10, "path_queue_s": "60", "path_work_s": "500", "wall_s": "560",
                 "jobs": [{"job": "check (rest)", "queue_s": "60", "duration_s": "500"}]}]
        recs = A.landing_records(it, st.item_events("change", it["id"]), {"base": "b0", "checked_base": "e67d38"}, runs,
                                 ["web/src/main/a.scala", "common/src/x", "docs/y.md", "build.sbt"], since=T0 + 10 ** 6)
        land, rounds = recs[0], recs[1:]
        self.assertEqual(land["record"], "agentctl.landing")
        self.assertEqual((land["outcome"], land["reason_class"], land["landed_sha"]), ("landed", "landed", "4486939"))
        self.assertEqual(land["queue_wait_s"], 700.0)                  # submitted -> first checking, the escalation inside
        self.assertEqual(land["total_s"], 700 + 1200 + 5 + 30 + 1000 + 5 + 300 + 600 + 20 + 3)
        self.assertEqual(land["checking_s"], 1200 + 1000 + 600)
        self.assertEqual(land["push_to_landed_s"], 3.0)
        self.assertEqual((land["rounds"], land["full_rounds"], land["verify_rounds"], land["full_rechecks"]), (3, 2, 1, 1))
        self.assertEqual(land["recheck_reasons"], "build,verify")
        self.assertTrue(land["red_fix"])
        self.assertEqual(land["fixes"], "abc.1")
        self.assertEqual((land["files_n"], land["commits_n"]), (4, 2))
        self.assertEqual(land["modules"], "(root),common,docs,web")
        self.assertEqual(land["sbt_modules"], "common,web")
        self.assertEqual(land["areas"], "heavy:itrest,identity,rest")
        self.assertEqual((land["check_runs"], land["check_runner_wait_s"], land["check_run_s"]), ("r1,r3", 360.0, 1300.0))
        self.assertEqual((land["check_job_queue_max_s"], land["check_jobs_n"]), (290.0, 4))
        self.assertEqual((land["lander_agent"], land["lander_session"], land["lander_host"]), ("ag1", "s1", "mac"))
        self.assertTrue(land["backfill"])
        self.assertEqual([(r["round"], r["mode"], r["reason_class"], r.get("run_id")) for r in rounds],
                         [(1, "full", "first", "r1"), (2, "full", "build", None), (3, "verify", "verify", "r3")])
        self.assertEqual(rounds[1]["check_s"], 1000.0)
        self.assertEqual(rounds[0]["run_queue_s"], 300.0)

    def test_exit_reasons_are_classed_and_a_refused_precheck_carries_its_step(self):
        t = [T0]
        st = store(t)
        st.put_item("change", "c1", "queued", {"submitted_at": t[0]})
        step(st, t, "change", "c1", "checking", 50)
        step(st, t, "change", "c1", "rejected", 900, data={"why": "NOT LANDED: the pre-push checks failed (on ci-runner)"})
        rec = A.landing_records(st.item("change", "c1"), st.item_events("change", "c1"))[0]
        self.assertEqual((rec["outcome"], rec["reason_class"], rec["rounds"]), ("rejected", "checks-failed", 1))
        self.assertFalse(rec["backfill"])
        st.put_item("change", "c2", "queued", {"submitted_at": t[0]})
        A.note_precheck("c2", 41.25, 1, "x\nPRECHECK REFUSED: drift slice missing\n", store=st)
        step(st, t, "change", "c2", "cancelled", 45, data={"why": "cancelled through the store API"})
        rec = A.landing_records(st.item("change", "c2"), st.item_events("change", "c2"))[0]
        self.assertEqual((rec["reason_class"], rec["precheck_outcome"], rec["precheck_s"]), ("precheck", "refused", 41.2))
        self.assertEqual(rec["precheck_step"], "PRECHECK REFUSED: drift slice missing")

    def test_a_precheck_that_could_not_check_is_unchecked_never_passed(self):
        """2026-11-22: exit 2 (a step could not run -- the lints past their 300-s bound) was recorded `passed`, so the
        landing records showed no precheck ever reaching its bound (45 of 497 had)."""
        self.assertEqual(A.precheck_outcome(2, "PRECHECK UNCHECKED: lints (309 s)\n"), ("unchecked", None))
        self.assertEqual(A.precheck_outcome(0, "PRECHECK PASS (9 s)\n"), ("passed", None))
        self.assertEqual(A.precheck_outcome(None, ""), ("unchecked", None))

    def test_a_precheck_is_noted_through_a_store_blip_and_on_an_item_that_comes_late(self):
        """2026-11-17: deploy-nix's note died on one `POST /v1/store/item: timed out`; an outbox submit's item comes
        after its precheck ends."""
        t = [T0]
        st = store(t)
        real, calls = st.item, []

        def flaky(typ, iid):
            calls.append(iid)
            if len(calls) == 1:
                raise A.StoreUnavailable("POST /v1/store/item: timed out")
            if len(calls) == 2:
                return None  # not submitted yet
            return real(typ, iid)
        st.item = flaky
        st.put_item("change", "c3", "queued", {"submitted_at": t[0]})
        slept = []
        os.environ["AGENTCTL_NOTE_PRECHECK_WAIT"] = "110"
        self.addCleanup(os.environ.__setitem__, "AGENTCTL_NOTE_PRECHECK_WAIT", "0")
        self.assertTrue(A.note_precheck("c3", 60.0, 0, "PRECHECK PASS (60 s)\n", store=st, sleep=slept.append))
        self.assertEqual(slept, [5, 15])
        self.assertEqual(real("change", "c3")["data"]["precheck"], {"duration_s": 60.0, "outcome": "passed"})

    def test_recheck_classes(self):
        for text, cls in [("x: main moved by 3 commits since its checks, sbt modules overlap (worker and common): "
                           "checked again", "module-overlap"),
                          ("x: main moved by 1 commit since its checks, they change scripts/hooks/pre-push: checked "
                           "again", "hook"),
                          ("x: main moved by 1 commit since its checks, both touch a/b: checked again", "overlap"),
                          ("QUEUED AGAIN x: infra (its fetch of main failed); checked again in 5 min",
                           "infra:its-fetch-of-main-failed"),
                          ("x: waiting -> queued", "from-waiting")]:
            self.assertEqual(A.recheck_class(text), cls, text)


class RedConditionDecisionTest(unittest.TestCase):
    def test_a_red_episode_its_fixers_fix_waiting_changes_and_owner_decision(self):
        t = [T0]
        st = store(t)
        st.put_item("red", "k1.2", "red", {"key": "k1", "workflow": "Main", "gate": True, "kind": "test",
                                           "red_kind": "gate", "jobs": ["ci / test (worker)"], "runs": ["2564", "2563"],
                                           "last_run": {"number": 2565}, "opened_at": T0, "family": "main-tests"})
        step(st, t, "red", "k1.2", "offered", 60, "Main #2564 is red: offered")
        step(st, t, "red", "k1.2", "leased", 600, "Main #2564: leased by laptop:owner:main-tests")
        step(st, t, "red", "k1.2", "offered", 900, "lapsed", event={"severity": "escalate"})
        step(st, t, "red", "k1.2", "leased", 60, "Main #2564: leased by laptop:fixer:red-k1-2")
        step(st, t, "red", "k1.2", "blocked-external", 300, "RED BLOCKED ON OWNER: ...")
        step(st, t, "red", "k1.2", "fix-pending", 3600, "decided", data={
            "decision": {"answer": "yes", "question": "q?", "at": t[0] + 3600},
            "fix": {"status": "landed", "sha": "f1", "entry": "e9", "at": t[0] + 3600 + 1800}})
        step(st, t, "red", "k1.2", "verified", 3600 + 2400, "FIX OK")
        it = st.item("red", "k1.2")
        evs = st.item_events("red", "k1.2")
        waits = [("c1", T0 + 100, T0 + 2000), ("c2", T0 - 500, None), ("c3", T0 - 900, T0 - 100)]
        rec = A.red_record(it, evs, waits, since=T0)
        self.assertEqual((rec["workflow"], rec["gate"], rec["verdict_class"], rec["outcome"]),
                         ("Main", True, "test-failure", "verified"))
        self.assertEqual((rec["first_run"], rec["last_run"], rec["runs_n"]), (2563, 2565, 2))
        self.assertEqual((rec["fixers_n"], rec["fixer_restarts"]), (2, 1))
        self.assertEqual(rec["fixer_holders"], "laptop:fixer:red-k1-2,laptop:owner:main-tests")
        self.assertEqual(rec["time_to_fixer_s"], 660.0)
        self.assertEqual(rec["duration_s"], t[0] - T0)
        self.assertEqual(rec["red_to_green_s"], t[0] - T0)
        self.assertEqual(rec["fix_sha"], "f1")
        self.assertEqual((rec["waited_changes_n"], round(rec["waited_changes_s"])), (2, 1900 + (t[0] - T0)))
        self.assertTrue(rec["owner_blocked"])
        self.assertEqual(rec["blocked_s"], 3600.0)
        self.assertEqual(rec["escalations_n"], 1)
        self.assertFalse(rec["backfill"])
        od = A.owner_decision_record(it, evs)
        self.assertEqual((od["answer_path"], od["answer"], od["belongs_to"], od["wait_s"]),
                         ("decided", "yes", "red:k1.2", 3600.0))  # blocked -> decided

    def test_a_red_that_never_asked_the_owner_has_no_decision_record(self):
        t = [T0]
        st = store(t)
        st.put_item("red", "k2", "red", {"key": "k2", "workflow": "Nightly", "kind": "infra", "opened_at": T0})
        step(st, t, "red", "k2", "cleared", 100, "a newer decided run on main does not fail this way")
        it, evs = st.item("red", "k2"), st.item_events("red", "k2")
        self.assertIsNone(A.owner_decision_record(it, evs))
        rec = A.red_record(it, evs, [("c1", T0, T0 + 50)])
        self.assertEqual((rec["verdict_class"], rec["waited_changes_n"], rec["outcome"]), ("infra-shaped", 0, "cleared"))
        self.assertNotIn("red_to_green_s", rec)

    def test_a_condition_spell(self):
        t = [T0]
        st = store(t)
        st.put_item("condition", "invariant-W1", "open", {"key": "invariant-W1", "kind": "INVARIANT", "opened_at": T0,
                                                          "text": "INVARIANT W1: the oldest change is 3 h old"})
        step(st, t, "condition", "invariant-W1", "open", 600, "W1 still", event={"severity": "escalate"})
        st.mark_notified(st.item_events("condition", "invariant-W1")[-1]["seq"])
        step(st, t, "condition", "invariant-W1", "open", 7200, "W1 still", event={"severity": "escalate"})
        step(st, t, "condition", "invariant-W1", "closed", 300, "closed", data={"closed_by": "no change waits"})
        rec = A.condition_record(st.item("condition", "invariant-W1"), st.item_events("condition", "invariant-W1"))
        self.assertEqual((rec["invariant"], rec["kind"], rec["duration_s"]), ("W1", "INVARIANT", 8100.0))
        self.assertEqual((rec["escalations_n"], rec["notifications_n"]), (2, 1))
        self.assertEqual(rec["escalated_at"], A.iso_at(T0 + 600))
        self.assertIn("oldest change", rec["violation"])


class GateRunTest(unittest.TestCase):
    def test_a_gate_run_from_the_collectors_rows(self):
        run = {"workflow": "Main", "run_id": "44", "run_number": "2565", "run_conclusion": "success", "wall_s": "1500",
               "start": str(T0), "end": str(T0 + 1500), "critical_path": "plan → ci / test (worker)",
               "path_queue_s": "100", "path_work_s": "1300"}
        jobs = [{"job": "plan", "queue_s": "3", "duration_s": "20", "end": str(T0 + 30), "conclusion": "success"},
                {"job": "ci / test (worker)", "queue_s": "97", "duration_s": "1280", "end": str(T0 + 1500),
                 "conclusion": "success"},
                {"job": "ci / lint", "queue_s": "400", "duration_s": "60", "end": str(T0 + 500), "conclusion": "failure"}]
        rec = A.gate_run_record(run, jobs)
        self.assertEqual((rec["critical_job"], rec["critical_job_s"], rec["critical_job_queue_s"]),
                         ("ci / test (worker)", 1280.0, 97.0))
        self.assertEqual((rec["runner_wait_s"], rec["run_s"], rec["job_queue_max_s"]), (500.0, 1360.0, 400.0))
        self.assertEqual((rec["duration_s"], rec["conclusion"], rec["failed_jobs"]), (1500.0, "success", "ci / lint"))
        self.assertEqual(rec["_time"], A.iso_at(T0 + 1500))


class StatsPassTest(unittest.TestCase):
    def setUp(self):
        self.t = [T0]
        self.st = store(self.t)
        self.sent, self.fail = [], [False]

        def post(url, recs):
            if self.fail[0]:
                raise OSError("connection refused")
            self.sent.extend(recs)

        def query(url, q, start=None, end=None, limit=5000):
            return []
        self.patches = [mock.patch.object(A, "stats_post", post), mock.patch.object(A, "stats_query", query)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def test_each_finished_item_is_sent_once_after_it_settles_history_as_backfill_live_ones_echoed(self):
        st, t = self.st, self.t
        landed_change(st, t, "old")
        t[0] += 3600
        out = io.StringIO()
        self.assertGreater(A.stats_pass(FakeClock(t[0]), store=st, url="http://vl", out=out), 0)  # the first pass: since=now
        self.assertEqual({r["record"] for r in self.sent}, {"agentctl.landing", "agentctl.landing_round"})
        self.assertTrue(all(r["backfill"] for r in self.sent))
        self.assertEqual(out.getvalue(), "")  # backfill never floods the journal
        n = len(self.sent)
        A.stats_pass(FakeClock(t[0] + 60), store=st, url="http://vl", out=out)
        self.assertEqual(len(self.sent), n)  # sent once
        landed_change(st, t, "new")
        A.stats_pass(FakeClock(t[0] + 60), store=st, url="http://vl", out=out)
        self.assertEqual(len(self.sent), n)  # not settled yet: the collector's rows may still be coming
        self.fail[0] = True
        A.stats_pass(FakeClock(t[0] + A.STATS_SETTLE + 1), store=st, url="http://vl", out=out)
        self.assertEqual(len(self.sent), n)  # refused: unsent, tried again
        self.fail[0] = False
        A.stats_pass(FakeClock(t[0] + A.STATS_SETTLE + 2), store=st, url="http://vl", out=out)
        live = [r for r in self.sent[n:] if r["record"] == "agentctl.landing"]
        self.assertEqual([(r["change_id"], r["backfill"]) for r in live], [("new", False)])
        self.assertIn('agentctl.landing {"', out.getvalue())

    def test_gate_runs_walk_the_history_then_the_last_days_once_each(self):
        st, t = self.st, self.t
        seed_gating_checks()  # THE GATE's workflows: movies' registry as last read (no clone, no forge here)
        at = {"start": str(T0 - 100 * 86400), "end": str(T0 - 100 * 86400 + 60), "run_conclusion": "success"}
        rows = {"run": [{"kind": "run", "repo": "pawelkrupinski/movies", "workflow": "Main", "workflow_file": "main.yml",
                         "run_id": "7", "run_attempt": "1", "wall_s": "60", **at},
                        # a convergence-dispatched Identity takes run: its run-name is no workflow name (2026-11-17:
                        # 8 records in all, the last on 11-14), its file is
                        {"kind": "run", "repo": "pawelkrupinski/movies", "workflow": "Identity takes (convergence run 6)",
                         "workflow_file": "identity-takes.yml", "run_id": "9", "run_attempt": "1", **at},
                        # bitcashier/core's "Main Pipeline": no gate run (a phrase filter once let it in)
                        {"kind": "run", "repo": "bitcashier/core", "workflow": "Main Pipeline",
                         "workflow_file": "main.yml", "run_id": "8", **at}]}
        calls = []

        def query(url, q, start=None, end=None, limit=5000):
            calls.append((q, start, end))
            if "kind:run" in q and start <= T0 - 100 * 86400 <= end:
                return rows["run"]
            return []
        with mock.patch.object(A, "stats_query", query):
            for i in range(20):
                A.stats_pass(FakeClock(T0 + i), store=st, url="http://vl")
        gates = [r for r in self.sent if r["record"] == "agentctl.gate_run"]
        self.assertIn('repo:="pawelkrupinski/movies" workflow_file:in("main.yml", ', calls[0][0])
        self.assertEqual(sorted((g["run_id"], g["workflow"], g["backfill"]) for g in gates),
                         [("7", "Main", True), ("9", "Identity takes", True)])
        walked = T0 - A.STATS_GATE_HISTORY + 17 * 7 * 86400  # 17 weekly windows reach the last 3 days
        self.assertEqual(float(st.meta_get(A.STATS_GATE_WALK)), walked)
        self.assertEqual(calls[-1][1], T0 + 19 - A.STATS_GATE_LOOKBACK)

    def test_no_url_no_pass_and_the_reconciler_runs_the_step(self):
        with mock.patch.dict(os.environ, {"AGENTCTL_STATS_URL": "off"}):
            self.assertIsNone(A.stats_url())
            self.assertEqual(A.maybe_stats_pass("/r", "/d", *on_clock(A.Clock())), 0)
        with mock.patch.dict(os.environ, {"AGENTCTL_HOST": "ci-runner", "AGENTCTL_STATS_URL": ""}):
            self.assertEqual(A.stats_url(), A.STATS_URL_SERVER)
        names = [n for n, _f in A.server_steps("/r", "/d", lambda f: f, *on_clock(A.Clock()))]
        self.assertIn("stats", names)


if __name__ == "__main__":
    unittest.main()
