"""THE DIFFERENTIAL LANDING CHECK (2026-11-04: credited-memo and G1 retention were NOT LANDED on a stale slice failing on
main itself). A change's failed checks are judged against its base: a failing test main's red gate run fails too, or
another change's check on the same main, is not held against it -- it joins `waiting` with one author note; a test only
the head fails rejects, naming it. A check stopped at its time cap is infra, re-queued; a docs-only conflict keeps both
sides."""
import os, sys, tempfile, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A, sh  # noqa: E402
import test_queue  # noqa: E402

SLICE = "uk-mowlem-theatre-purbeck-film-festival-hen-736301b7"


def run_log(*lines, job="check (rest)"):
    return "".join(f"{job}\tThe pre-push hook's checks\t2026-11-03T10:00:00.0000000Z {l}\n" for l in lines)


class DifferentialCheckTest(test_queue.QueueTest):
    def setUp(self):
        super().setUp()
        self.r.write(".github/workflows/precommit-check.yml", "name: Pre-commit check\n")
        self.r.commit("the remote check workflow")
        self.r.push()
        if os.path.exists(self.pushes):
            os.remove(self.pushes)
        self.logs = {}  # run id -> its failed log
        self.main_runs = []  # main's gate: (workflow, decided run)
        names = ("fetch_failed_log", "gate_runs", "slug", "GATING")
        saved = {n: getattr(A, n) for n in names}
        self.addCleanup(lambda: [setattr(A, n, v) for n, v in saved.items()])
        A.fetch_failed_log = lambda o_r, rid: self.logs[int(rid)]
        A.gate_runs = lambda o_r, flows=None: [(n, r, None, 1) for n, r in self.main_runs]
        A.slug = lambda repo: "o/movies"
        A.GATING = {**saved["GATING"], "o/movies": [("Main", "ci.yml")]}
        os.environ["AGENTCTL_REDS_DIR"] = tempfile.mkdtemp(dir=self.tmp.name)

    def remote_fails(self, run, *lines, then=None):
        """The change's remote check fails with lines as its failed log (run id `run`); then: the next rounds' verdicts."""
        self.logs[run] = run_log(*lines)
        verdicts = [(False, f"precommit-check.yml run {run} (#1) failed: http://x\n-- check (rest)\n" + "\n".join(lines))]
        verdicts += then or []

        def remote(repo, e, base, head, mode, own=None, red=False):
            e["remote_run"] = run
            return verdicts.pop(0) if len(verdicts) > 1 else verdicts[0]
        A.remote_check = remote

    def red_main(self, run, *lines):
        self.logs[run] = run_log(*lines, job="itidentity")
        self.main_runs = [("Main", {"conclusion": "failure", "databaseId": run, "number": 2123})]

    def verdicts(self, kind):
        return [e["verdict"] for e in self.inbox() if e["kind"] == kind]

    def test_a_test_main_fails_too_waits_behind_the_red_with_one_note(self):
        self.reds = {"2123"}
        self.red_main(500, f"[info] - {SLICE} *** FAILED ***", "[info]   the fixture lacks answers this resolve asks")
        wt, _ = self.agent("memo", "web/src/main/Memo.scala", "credited\n", msg="credited-memo")
        eid, _ = self.queued(wt)
        self.remote_fails(41, f"[info] - {SLICE} *** FAILED ***", "[info]   the fixture lacks answers this resolve asks",
                          f"pre-push identity FAILED (300s)")
        self.assertEqual(self.check(eid), 0)
        self.assertEqual(self.verdicts("NOT_LANDED"), [], "never rejected for main's own red")
        waiting = [e for e in self.inbox() if e["verdict"].startswith("WAITING")]
        self.assertEqual(len(waiting), 1, "one author note")
        self.assertIn(SLICE, waiting[0]["verdict"])
        self.assertIn("Main #2123 on main", waiting[0]["verdict"])
        self.assertEqual(waiting[0]["cwd"], wt)
        self.assertEqual(self.pushed(), [], "nothing pushed onto red")

    def test_a_tools_change_failing_only_as_main_fails_waits_even_on_a_green_gate(self):
        """The gate reads green (main's newest run passed a re-run), but another change on the same main failed the same
        test: main's, not this change's -- it waits (the batch takes it), never pushed with a failed check."""
        wa, _ = self.agent("a", "scripts/a.sh", "a\n")
        ea, _ = self.queued(wa)
        self.remote_fails(41, "[info] - never case-map a String *** FAILED ***", "pre-push sbt FAILED (60s)")
        self.check(ea)
        self.assertIn("never case-map a String", self.verdicts("NOT_LANDED")[0], "the first one is judged alone")
        wb, _ = self.agent("b", "scripts/b.sh", "b\n")
        eb, _ = self.queued(wb)
        self.remote_fails(42, "[info] - never case-map a String *** FAILED ***", "pre-push sbt FAILED (60s)")
        self.check(eb)
        self.assertEqual(len(self.verdicts("NOT_LANDED")), 1)
        self.assertEqual(self.entries()[eb.rsplit("-", 1)[-1]]["status"], "joined")
        self.assertEqual(self.pushed(), [])

    def test_a_test_only_the_head_fails_is_rejected_naming_it(self):
        self.reds = {"2123"}
        self.red_main(500, f"[info] - {SLICE} *** FAILED ***")
        wt, _ = self.agent("g1", "web/src/main/G1.scala", "g1\n")
        eid, _ = self.queued(wt)
        self.remote_fails(43, f"[info] - {SLICE} *** FAILED ***", "[info] - G1 retention keeps a week *** FAILED ***",
                          "pre-push identity FAILED (300s)")
        self.assertEqual(self.check(eid), 1)
        v = self.verdicts("NOT_LANDED")[0]
        self.assertIn("failing with this change only: G1 retention keeps a week", v)
        self.assertNotIn(SLICE.split("-")[1], v.split("only:")[1])

    def test_a_check_failing_without_a_test_beside_main_s_red_is_the_change_s(self):
        """One job runs many hook checks: a shellcheck failure must not hide behind a base-red test in the same job."""
        self.reds = {"2123"}
        self.red_main(500, f"[info] - {SLICE} *** FAILED ***")
        wt, _ = self.agent("sc", "web/src/main/Sc.scala", "sc\n")
        eid, _ = self.queued(wt)
        self.remote_fails(44, f"[info] - {SLICE} *** FAILED ***", "pre-push identity FAILED (300s)",
                          "In scripts/x.sh line 3: SC2086", "pre-push shellcheck FAILED (2s)")
        self.assertEqual(self.check(eid), 1)
        self.assertIn("check (rest): shellcheck (no failing test named)", self.verdicts("NOT_LANDED")[0])

    def test_a_check_stopped_at_its_time_cap_is_requeued_then_rejected(self):
        wt, _ = self.agent("slow", "web/src/main/Slow.scala", "s\n")
        eid, _ = self.queued(wt)
        self.remote_fails(45, "::error::the pre-push checks ran past 80 minutes and were stopped")
        d, _c, _w = A.waiting_state(self.r.path)
        for n in (1, 2):
            A.check_entry(self.r.path, eid)
            e = A.read_record(os.path.join(d, "entries", f"{eid}.json"))
            self.assertEqual(e["status"], "queued", f"time cap {n}: infra, re-queued")
            e["retry_after"] = 0
            A.save_entry(d, e)
        self.assertEqual(self.verdicts("NOT_LANDED"), [])
        self.assertEqual(self.check(eid, rounds=1), 1, "the third time it is the change's")
        self.assertIn("time cap", self.verdicts("NOT_LANDED")[0])


for _n in dir(test_queue.QueueTest):  # the queue's own tests run in test_queue, never again here
    if _n.startswith("test_") and _n not in DifferentialCheckTest.__dict__:
        setattr(DifferentialCheckTest, _n, None)


class UnionResolveTest(unittest.TestCase):
    def repo(self):
        t = tempfile.mkdtemp()
        sh(t, "git", "init", "-q", "-b", "main")
        sh(t, "git", "config", "user.email", "t@t")
        sh(t, "git", "config", "user.name", "t")
        return t

    def commit(self, t, path, text, msg):
        os.makedirs(os.path.dirname(os.path.join(t, path)) or t, exist_ok=True)
        with open(os.path.join(t, path), "w") as f:
            f.write(text)
        sh(t, "git", "add", "-A")
        sh(t, "git", "commit", "-q", "-m", msg)
        return sh(t, "git", "rev-parse", "HEAD").strip()

    def both_append(self, path):
        t = self.repo()
        self.commit(t, path, "# Flip unification\n\nintro\n", "base")
        base = sh(t, "git", "rev-parse", "HEAD").strip()
        self.commit(t, path, "# Flip unification\n\nintro\n\n## stage 5 (main)\n", "main's section")
        sh(t, "git", "checkout", "-q", "-b", "change", base)
        mine = self.commit(t, path, "# Flip unification\n\nintro\n\n## retention (change)\n", "the change's section")
        sh(t, "git", "checkout", "-q", "main")
        return t, mine

    def test_a_docs_only_conflict_keeps_both_sides(self):
        t, mine = self.both_append("docs/investigations/2026-11-01-flip-unification.md")
        self.assertIsNone(A.pick(t, [mine]), "resolved, not a conflict")
        text = open(os.path.join(t, "docs/investigations/2026-11-01-flip-unification.md")).read()
        self.assertIn("## stage 5 (main)", text)
        self.assertIn("## retention (change)", text)
        self.assertEqual(sh(t, "git", "log", "-1", "--format=%s").strip(), "the change's section")

    def test_a_code_conflict_is_still_the_author_s(self):
        t, mine = self.both_append("identity/src/main/scala/AgreementStage.scala")
        self.assertEqual(A.pick(t, [mine]), ["identity/src/main/scala/AgreementStage.scala"])

    def test_a_fixture_tsv_is_never_union_merged(self):
        t, mine = self.both_append("test/resources/fixtures/08-06-2026/read-model-derivation-hashes.tsv")
        self.assertEqual(A.pick(t, [mine]), ["test/resources/fixtures/08-06-2026/read-model-derivation-hashes.tsv"])


if __name__ == "__main__":
    unittest.main()
