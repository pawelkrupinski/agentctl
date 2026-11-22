"""A FIXES-RED TRAILER MUST TOUCH WHAT THE RED READS (G9, convergence-failures-2026-10-29 §4: d21ac5e81 and 1548eb448,
unit-test speed-ups, carried `Fixes-Red: 2390;2375;2392` while touching nothing those reds' jobs read). A trailer on a
commit that plainly touches none of the paths the red's failing job reads does not count: the red is not moved to
fix-pending, the change lands as any other (waits while the gate is red), and its author is told why. Unsure: it counts."""
import os, sys, tempfile, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A, Repo, sh  # noqa: E402

CONV = """name: Identity model convergence
on: workflow_dispatch
jobs:
    preflight:
        runs-on: x
        steps:
            - run: scripts/ci/preflight.sh
    legs:
        uses: ./.github/workflows/leg.yml
"""
LEG = """name: leg
on: workflow_call
jobs:
    drift:
        runs-on: x
        steps:
            - run: sbt "worker/testOnly services.identity.IdentityDriftSpec"
            - run: scripts/ci/identity-drift-check.sh
"""
UNIT = """name: Main
on: push
jobs:
    unit:
        name: Unit tests
        runs-on: x
        steps:
            - run: sbt test
"""
SPEC = "worker/src/test/scala/services/identity/"


class FixCheckBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = os.path.realpath(self.tmp.name)
        self.saved = {k: os.environ.get(k) for k in ("AGENTCTL_STATE_ROOT", "AGENTCTL_REDS_DIR", "AGENTCTL_INBOX_DIR")}
        os.environ["AGENTCTL_STATE_ROOT"] = os.path.join(t, "state")
        os.environ["AGENTCTL_REDS_DIR"] = os.path.join(t, "reds")
        os.environ["AGENTCTL_INBOX_DIR"] = os.path.join(t, "inbox")
        self.r = Repo(t)
        self.r.write(".github/workflows/identity-model-convergence.yml", CONV)
        self.r.write(".github/workflows/leg.yml", LEG)
        self.r.write(".github/workflows/main.yml", UNIT)
        self.r.write("scripts/ci/preflight.sh", "echo ok\n")
        self.r.write("scripts/ci/identity-drift-check.sh", "python3 scripts/ci/drift_report.py\n")
        self.r.write("scripts/ci/drift_report.py", "print(1)\n")
        self.r.write(SPEC + "IdentityDriftSpec.scala", "class IdentityDriftSpec\n")
        self.r.write(SPEC + "IdentitySliceCasesSpec.scala", "class IdentitySliceCasesSpec\n")
        self.r.write(SPEC + "IdentityTestSupport.scala", "object IdentityTestSupport\n")
        self.r.write("worker/src/main/scala/identity/Model.scala", "object Model\n")
        self.r.write("docs/notes.md", "notes\n")
        self.r.write("android/app.kt", "x\n")
        self.r.commit("tree")
        self.r.push()
        self.st = A.Store(path=os.path.join(t, "store.db"))
        self.st.put_item("red", "drift", "offered", {"key": "drift", "workflow": "Identity model convergence",
                                                     "job": "takes-drift-1 (pl)", "jobs": ["takes-drift-1 (pl)"],
                                                     "kind": "test", "line": "drift: 3 flips", "runs": ["2375", "9375"],
                                                     "last_run": {"number": 2375, "databaseId": 9375}})
        self.st.put_item("red", "unit", "offered", {"key": "unit", "workflow": "Main", "job": "Unit tests",
                                                    "kind": "test", "line": "FooSpec failed", "runs": ["2390"]})
        self.st.put_item("red", "infra", "offered", {"key": "infra", "workflow": "Identity model convergence",
                                                     "job": "preflight", "kind": "infra", "line": "runner lost",
                                                     "runs": ["2392"]})

    def tearDown(self):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp.cleanup()

    def change(self, files, fixes):
        for p in files:
            self.r.write(p, f"changed {p}\n")
        trailers = "".join(f"\nFixes-Red: {f}" for f in fixes)
        sh(self.r.path, "git", "add", "-A")
        sh(self.r.path, "git", "commit", "-q", "-m", f"change\n{trailers}")
        return sh(self.r.path, "git", "rev-parse", "HEAD")

    def check(self, files, fixes=("2375",)):
        sha = self.change(files, fixes)
        return A.fix_check(self.r.path, sha, set(fixes), self.st)


class FixCheckTest(FixCheckBase):
    def test_another_spec_while_the_job_runs_only_named_specs_does_not_count(self):
        ok, bad = self.check([SPEC + "IdentitySliceCasesSpec.scala"])
        self.assertEqual(ok, set())
        self.assertIn("touches nothing", bad["2375"])

    def test_the_failing_jobs_named_spec_counts(self):
        self.assertEqual(self.check([SPEC + "IdentityDriftSpec.scala"])[0], {"2375"})

    def test_a_test_helper_main_source_or_a_script_the_job_runs_counts(self):
        self.assertEqual(self.check([SPEC + "IdentityTestSupport.scala"])[0], {"2375"})
        self.assertEqual(self.check(["worker/src/main/scala/identity/Model.scala"])[0], {"2375"})
        self.assertEqual(self.check(["scripts/ci/drift_report.py"])[0], {"2375"})  # two levels down
        self.assertEqual(self.check([".github/workflows/leg.yml"])[0], {"2375"})  # its reusable workflow

    def test_docs_other_workflows_and_the_mobile_apps_do_not_count(self):
        ok, bad = self.check(["docs/notes.md", ".github/workflows/main.yml", "android/app.kt"])
        self.assertEqual(ok, set())

    def test_a_job_running_the_whole_test_task_reads_every_spec(self):
        self.assertEqual(self.check([SPEC + "IdentitySliceCasesSpec.scala"], ("2390",))[0], {"2390"})

    def test_unsure_counts_an_unknown_red_an_infra_red_and_local(self):
        self.assertEqual(self.check(["docs/notes.md"], ("9999", "2392", "local"))[0], {"9999", "2392", "local"})

    def test_mixed_one_counted_path_is_enough(self):
        self.assertEqual(self.check(["docs/notes.md", SPEC + "IdentityDriftSpec.scala"])[0], {"2375"})

    def test_the_author_is_told_once_and_the_verdict_is_cached(self):
        sha = self.change([SPEC + "IdentitySliceCasesSpec.scala"], ("2375",))
        A.fix_check(self.r.path, sha, {"2375"}, self.st)
        A.fix_check(self.r.path, sha, {"2375"}, self.st)
        told = [A.read_record(os.path.join(os.environ["AGENTCTL_INBOX_DIR"], f))
                for f in os.listdir(os.environ["AGENTCTL_INBOX_DIR"]) if f.endswith(".json")]
        told = [e for e in told if "FIX NOT COUNTED" in e["verdict"]]
        self.assertEqual(len(told), 1)
        self.assertEqual(told[0]["kind"], "FIX_NOT_COUNTED")
        self.assertIn("lands as an ordinary one", told[0]["verdict"])


class LandingTest(FixCheckBase):
    def with_store(self):
        saved = A.open_store
        A.open_store = lambda *a, **k: self.st
        self.addCleanup(lambda: setattr(A, "open_store", saved))

    def test_a_trailer_that_does_not_count_waits_on_a_red_gate_and_names_no_fix(self):
        self.with_store()
        saved = A.is_production_path
        A.is_production_path = lambda p: True
        self.addCleanup(lambda: setattr(A, "is_production_path", saved))
        bad = self.change([SPEC + "IdentitySliceCasesSpec.scala"], ("2375",))
        self.assertTrue(A.must_wait(self.r.path, [bad], {"2375", "9375"}))
        self.assertEqual(A.fixes_of(self.r.path, [bad]), set())
        good = self.change([SPEC + "IdentityDriftSpec.scala"], ("2375",))
        self.assertFalse(A.must_wait(self.r.path, [good], {"2375", "9375"}))
        self.assertEqual(A.fixes_of(self.r.path, [good]), {"2375"})

    def test_a_landed_commit_whose_trailer_does_not_count_is_no_landed_fix(self):
        self.with_store()
        bad = self.change([SPEC + "IdentitySliceCasesSpec.scala"], ("2375",))
        good = self.change([SPEC + "IdentityDriftSpec.scala"], ("2375",))
        self.r.push()
        shas = [x[0] for x in A.landed_fix_commits(self.r.path)]
        self.assertIn(good, shas)
        self.assertNotIn(bad, shas)
        it = self.st.item("red", "drift")
        self.assertEqual(A.red_fix_of(it, {"entries": [], "landed": [x for x in A.landed_fix_commits(self.r.path)
                                                                     if x[0] == bad]}), None)


ORDER = """name: Order independence
on: workflow_dispatch
jobs:
    swift:
        runs-on: macos-latest
        steps:
            - uses: actions/checkout@v5
            - name: Every swift test case alone
              run: ios/scripts/swift-test-shuffled.sh "$SEED"
            - name: StateSyncModelTests at 2000 seeds
              run: swift test --package-path ios --filter StateSyncModelTests
            - name: GNU timeout and shellcheck
              run: |
                  brew install coreutils shellcheck
                  echo "$RUNNER_TEMP/gnu-timeout" >> "$GITHUB_PATH"
            - name: Script self-tests under macOS /bin/bash 3.2
              run: scripts/run-script-tests --bash /bin/bash
    ios:
        runs-on: macos-latest
        steps:
            - name: Build for testing
              run: xcodebuild build-for-testing -project ios/Kinowo.xcodeproj -scheme Kinowo
            - name: Every XCUITest class alone
              run: scripts/ci/xcuitest-each-class-alone.sh
"""


class PerRedTest(FixCheckBase):
    """PER RED, PER FAILING STEP (2026-11-15, Order independence #74): 09e482c6a, an ios/KinowoUITests change with
    `Fixes-Red: 74`, fixed the run's ios red and put its swift red -- the script self-tests failing under bash 3.2 -- in
    fix-pending too: the value counted for the run, and every red of the run took it."""

    def setUp(self):
        super().setUp()
        self.r.write(".github/workflows/order-independence.yml", ORDER)
        self.r.write("scripts/run-script-tests", 'Roots="scripts ios/scripts"\n')
        self.r.write("scripts/ci/xcuitest-each-class-alone.sh", 'dir="${XCUITEST_DIR:-ios/KinowoUITests}"\n')
        self.r.write("ios/scripts/swift-test-shuffled.sh", 'swift test --package-path "$package"\n')
        self.r.write("ios/KinowoUITests/CardShareMenuUITests.swift", "class CardShareMenuUITests\n")
        self.r.commit("order independence")
        self.r.push()
        run = {"number": 74, "databaseId": 43702576373}
        for key, job, step in (("swift74", "swift", "Script self-tests under macOS /bin/bash 3.2"),
                               ("ios74", "ios", "Every XCUITest class alone")):
            self.st.put_item("red", key, "offered", {"key": key, "workflow": "Order independence", "job": job,
                                                     "jobs": [job], "kind": "test", "line": f"{job} failed",
                                                     "runs": ["74", "43702576373"], "last_run": run, "steps": [step],
                                                     "opened_at": 0})

    def facts(self):
        return {"entries": [], "landed": A.landed_fix_commits(self.r.path), "repo": self.r.path}

    def ios_fix(self):
        saved = A.open_store
        A.open_store = lambda *a, **k: self.st
        self.addCleanup(lambda: setattr(A, "open_store", saved))
        sha = self.change(["ios/KinowoUITests/CardShareMenuUITests.swift"], ("74",))
        self.r.push()
        return sha

    def test_a_run_level_trailer_is_the_fix_only_of_the_reds_whose_failing_step_reads_it(self):
        sha = self.ios_fix()
        self.assertEqual(A.fix_check(self.r.path, sha, {"74"}, self.st)[0], {"74"}, "it fixes a red of #74: it counts")
        facts = self.facts()
        self.assertEqual((A.red_fix_of(self.st.item("red", "ios74"), facts) or {}).get("sha"), sha)
        self.assertIsNone(A.red_fix_of(self.st.item("red", "swift74"), facts),
                          "the self-test step reads ios/scripts alone, never ios/KinowoUITests")
        self.assertFalse(A.fix_reaches_red(self.r.path, sha, self.st.item("red", "swift74")["data"]))

    def test_a_red_named_by_its_key_takes_the_fix_whatever_it_touches(self):
        sha = self.ios_fix()
        it = self.st.item("red", "swift74")
        self.assertEqual(A.red_fix_of(it, {"entries": [], "landed": [(sha, 1e10, {"swift74"}, "s")],
                                           "repo": self.r.path})["sha"], sha)

    def test_a_job_failing_in_a_step_that_builds_the_app_or_unknown_steps_reads_all_of_it(self):
        sha = self.ios_fix()
        d = dict(self.st.item("red", "swift74")["data"])
        for steps in (["Every swift test case alone"], ["StateSyncModelTests at 2000 seeds"], None):
            self.assertTrue(A.fix_red_verdict(self.r.path, sha, {**d, "steps": steps, "last_run": {}},
                                              A.fix_changed(self.r.path, sha))["ok"], steps)

    def test_a_fix_pending_red_on_another_reds_fix_is_offered_again(self):
        sha = self.ios_fix()
        it = self.st.item("red", "swift74")
        self.st.transition("red", "swift74", "fix-pending", it["version"],
                           data={"fix": {"entry": None, "status": "landed", "sha": sha, "at": 1.0}})
        it = self.st.item("red", "swift74")
        facts = self.facts()
        out = A.red_fix_pending(self.st, it, A.red_fix_of(it, facts), False, None, {}, facts, self.r.path,
                                A.RED_PRIMARY, 2.0)
        self.assertIn("another red's", " ".join(out))
        it = self.st.item("red", "swift74")
        self.assertEqual((it["state"], it["data"].get("fix")), ("offered", None))

    def test_a_red_that_waited_on_another_reds_fix_verifies_it_whatever_its_job_reads(self):
        # its fixer said the fix is THAT red's commit (WAITS ON, red_waits_on_step): the commit reaching main is the
        # verification it waited for, never a re-offer for touching nothing its failing job reads
        sha = self.ios_fix()
        it = self.st.item("red", "swift74")
        self.st.transition("red", "swift74", "fix-pending", it["version"], data={"fix": {
            "entry": None, "status": "landed", "sha": sha, "at": 1.0, "verify_since": 1.0, "waited_on": "ios74"}})
        it = self.st.item("red", "swift74")
        facts = self.facts()
        out = A.red_fix_pending(self.st, it, A.red_fix_of(it, facts), False, None, {}, facts, self.r.path,
                                A.RED_PRIMARY, 2.0)
        self.assertNotIn("another red's", " ".join(out))
        it = self.st.item("red", "swift74")
        self.assertEqual((it["state"], (it["data"].get("fix") or {}).get("sha")), ("fix-pending", sha))

    def test_a_per_red_verdict_holds_where_the_cache_cannot_be_written(self):
        sha = self.ios_fix()
        d = self.st.item("red", "swift74")["data"]
        saved = A.fix_cache_put
        A.fix_cache_put = lambda *a, **k: None  # a reds dir it cannot write: the verdict is still this call's
        self.addCleanup(lambda: setattr(A, "fix_cache_put", saved))
        self.assertFalse(A.fix_reaches_red(self.r.path, sha, d))


SELF_TESTS = """name: Tool self-tests
on: push
jobs:
    script-tests:
        name: script self-tests
        runs-on: x
        steps:
            - uses: actions/checkout@v5
            - name: Every script self-test
              run: scripts/run-script-tests
"""


class SelfTestReadsTest(FixCheckBase):
    """WHAT A SELF-TEST READS (2026-11-15, Tool self-tests #263, red 7ef28908cfd3): f872ecbe3, a
    .github/workflows/verify-scrape-recording.yml change with `Fixes-Red: 263`, was FIX NOT COUNTED, though the red's
    step runs scripts/run-script-tests, which finds download_artifact_layout_test.py by name, which lints every
    .github/workflows/*.yml through the workflow_steps module it imports. A pure ios/ commit still does not count."""

    def setUp(self):
        super().setUp()
        self.r.write(".github/workflows/tool-self-tests.yml", SELF_TESTS)
        self.r.write(".github/workflows/verify-scrape-recording.yml", "name: Verify scrape recording\n")
        self.r.write("scripts/run-script-tests", 'Roots="scripts .github/scripts ios/scripts"\n'
                     "find $Roots -type f \\( -name '*-test.sh' -o -name '*_test.py' \\) -print\n")
        self.r.write("scripts/ci/download_artifact_layout_test.py",
                     "import sys\nfrom workflow_steps import FILES, action_steps\n")
        self.r.write("scripts/ci/workflow_steps.py",
                     'FILES = sorted(glob.glob(os.path.join(ROOT, ".github/workflows/*.yml")))\n')
        self.r.write("scripts/ios-compile-check-test.sh",  # a fake app laid out in a temp dir: data, never ios/ whole
                     'mkdir -p "$work/ios/Kinowo.xcodeproj"; printf x > "$work/ios/Package.swift"\n')
        self.r.write("ios/scripts/swift-test-shuffled-test.sh", "echo ok\n")
        self.r.write("ios/KinowoUITests/CardShareMenuUITests.swift", "class CardShareMenuUITests\n")
        self.r.commit("self-tests")
        self.r.push()
        self.st.put_item("red", "selftests", "offered", {
            "key": "selftests", "workflow": "Tool self-tests", "job": "script self-tests", "jobs": ["script self-tests"],
            "kind": "test", "line": "download_artifact_layout_test.py failed", "runs": ["263", "43740000000"],
            "last_run": {"number": 263, "databaseId": 43740000000}, "steps": ["Every script self-test"]})

    def test_a_workflow_file_a_found_self_test_lints_counts(self):
        self.assertEqual(self.check([".github/workflows/verify-scrape-recording.yml"], ("263",))[0], {"263"})

    def test_a_found_self_test_and_what_it_imports_count(self):
        self.assertEqual(self.check(["scripts/ci/workflow_steps.py"], ("263",))[0], {"263"})
        self.assertEqual(self.check(["ios/scripts/swift-test-shuffled-test.sh"], ("263",))[0], {"263"})

    def test_a_pure_ios_commit_still_does_not_count(self):
        ok, bad = self.check(["ios/KinowoUITests/CardShareMenuUITests.swift"], ("263",))
        self.assertEqual(ok, set())
        self.assertIn("touches nothing", bad["263"])

    def test_a_workflow_file_no_found_test_reads_does_not_count(self):
        self.r.write("scripts/ci/workflow_steps.py", "FILES = []\n")
        self.r.commit("no lint of the workflows")
        ok, bad = self.check([".github/workflows/verify-scrape-recording.yml"], ("263",))
        self.assertEqual(ok, set())
        self.assertIn("touches nothing", bad["263"])


if __name__ == "__main__":
    unittest.main()
