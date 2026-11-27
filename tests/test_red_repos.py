"""The red machine watches more than movies (the owner, 2026-10-30, ci-failures recommendation 4: infra's Stage NixOS
closures was red 29 h and core's Main Pipeline 21 h with no fixer). red-repos.json names the repos, their gate
workflows, their family and their push policy; infra is owner-only -- its fixer never pushes or deploys, it files the
owner a one-liner (`red owner-push`)."""
import argparse, json, os, shutil, sys, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A, sh  # noqa: E402
from harness import case_context  # noqa: E402
from fake_probes import registry  # noqa: E402
from test_red import run, MOVIES  # noqa: E402
from test_reds import RedsBase  # noqa: E402

INFRA, CORE, AGENTCTL = "bitcashier/infra", "bitcashier/core", "pawelkrupinski/agentctl"
STAGE = "Stage NixOS closures"
NIXBLD = "error: the user 'nixbld1' in the group 'nixbld' does not exist"


class RedReposBase(RedsBase):
    def setUp(self):
        super().setUp()
        t = os.path.realpath(self.tmp.name)
        # an infra clone whose remote slugs as bitcashier/infra (a copy of the movies-like fixture repo)
        os.makedirs(os.path.join(t, "bitcashier"))
        self.infra_bare = os.path.join(t, "bitcashier", "infra.git")
        shutil.copytree(self.r.bare, self.infra_bare, symlinks=True)
        self.infra = os.path.join(t, "infra")
        shutil.copytree(self.r.path, self.infra, symlinks=True)
        sh(self.infra, "git", "remote", "set-url", "origin", self.infra_bare)
        self.repos_file = os.path.join(t, "red-repos.json")
        with open(self.repos_file, "w") as f:
            json.dump({"repos": [
                {"repo": MOVIES, "gate": [["Main", "main.yml"], ["Identity model convergence",
                                                                "identity-model-convergence.yml"]]},
                {"repo": INFRA, "clone": self.infra, "gate": [[STAGE, "nix-stage-closures.yaml"]],
                 "family": "infra-repo", "push": "owner-only"},
                {"repo": CORE, "clone": os.path.join(t, "no-core-clone"), "gate": [["Main Pipeline", "main-pipeline.yml"]],
                 "family": "core-repo", "push": "land"}]}, f)
        self.saved_repos = os.environ.get("AGENTCTL_RED_REPOS")
        os.environ["AGENTCTL_RED_REPOS"] = self.repos_file
        A._red_repos_cache.clear()
        self.infra_red(9001, 3455)

    def tearDown(self):
        if self.saved_repos is None:
            os.environ.pop("AGENTCTL_RED_REPOS", None)
        else:
            os.environ["AGENTCTL_RED_REPOS"] = self.saved_repos
        A._red_repos_cache.clear()
        super().tearDown()

    def infra_red(self, rid, num, line=NIXBLD):
        """Infra's Stage NixOS closures #num failing with `line`, beside the movies runs runs.json already holds."""
        self.put("runs-bitcashier_infra.json", [
            run(rid, num, STAGE, "nix-stage-closures.yaml", self.sha, conclusion="failure"),
            run(rid - 10, num - 5, STAGE, "nix-stage-closures.yaml", self.sha, created="2026-10-27T06:00:00Z")])
        self.put(f"run-{rid}.json", {"jobs": [{"name": "stage", "status": "completed", "conclusion": "failure",
                                               "steps": [{"name": "Stage every host", "conclusion": "failure"}]}],
                                     "headSha": self.sha, "url": "u", "number": num, "status": "completed",
                                     "conclusion": "failure", "workflowName": STAGE})
        self.put(f"run-{rid}.log", f"stage\tStage every host\t2026-10-28T07:14:00Z {line}\n")

    def infra_green(self, rid, num):
        runs = json.load(open(os.path.join(self.d, "runs-bitcashier_infra.json")))
        self.put("runs-bitcashier_infra.json", [run(rid, num, STAGE, "nix-stage-closures.yaml", self.sha)] + runs)

    def others(self, advance=0):
        self.t[0] += advance
        return A.red_tick_others(self.st, registry(A), now_=self.t[0], ctx=case_context())

    def by_repo(self):
        out = {}
        for it in self.st.items("red"):
            if not A.is_terminal("red", it["state"]):
                out.setdefault(A.red_repo_of(it["data"]), []).append(it)
        return out


class ConfigTest(RedReposBase):
    def test_the_file_names_the_repos_and_the_primary_keeps_its_gate(self):
        self.assertEqual([c["repo"] for c in A.red_repos()], [MOVIES, INFRA, CORE])
        self.assertEqual(A.red_gate_flows(MOVIES), A.gate_flows(MOVIES))
        self.assertEqual([f for _n, f in A.red_gate_flows(MOVIES)],
                         ["main.yml", "identity-model-convergence.yml", "identity-takes.yml"], "the registry's rows")
        self.assertEqual(A.red_gate_flows(INFRA), [(STAGE, "nix-stage-closures.yaml")])
        self.assertTrue(A.red_owner_only(INFRA))
        self.assertFalse(A.red_owner_only(CORE))
        self.assertFalse(A.red_owner_only(MOVIES))
        self.assertFalse(A.gated_repo(INFRA), "watching a repo's reds puts no red gate in front of its landings")

    def test_a_malformed_entry_is_refused_and_only_the_primary_is_watched(self):
        for bad in ({"repos": [{"repo": INFRA}]}, {"repos": [{"repo": INFRA, "gate": [["x"]]}]},
                    {"repos": [{"repo": INFRA, "gate": [["x", "x.yml"]], "push": "yolo"}]},
                    {"repos": [{"repo": "not a repo", "gate": [["x", "x.yml"]]}]}):
            with self.assertRaises(ValueError, msg=bad):
                A.red_repos_parse(bad)
        with open(self.repos_file, "w") as f:
            f.write("{not json")
        A._red_repos_cache.clear()
        self.assertEqual([c["repo"] for c in A.red_repos()], [MOVIES])

    def test_the_primary_is_always_watched_first(self):
        repos = A.red_repos_parse({"repos": [{"repo": INFRA, "gate": [[STAGE, "s.yaml"]]}]})
        self.assertEqual([c["repo"] for c in repos], [MOVIES, INFRA])

    def test_the_tracked_file_parses_with_the_real_gate_workflows(self):
        with open(os.path.join(os.path.dirname(A.TOOL_PATH), "red-repos.json")) as f:
            repos = {c["repo"]: c for c in A.red_repos_parse(json.load(f))}
        self.assertEqual(repos[INFRA]["gate"], [(STAGE, "nix-stage-closures.yaml")])
        self.assertEqual(repos[INFRA]["push"], "owner-only")
        self.assertEqual(repos[CORE]["gate"], [("Main Pipeline", "main-pipeline.yml")])
        self.assertEqual((repos[AGENTCTL]["gate"], repos[AGENTCTL]["push"]), ([("tests", "tests.yml")], "selfpush"))
        self.assertEqual(repos[AGENTCTL]["family"], "agentctl-repo")
        self.assertEqual(repos[MOVIES]["gate"], [], "movies' gate is its gating checks registry, never a list here")

    def test_run_numbers_and_keys_are_per_repo(self):
        self.assertEqual(A.red_repo_tag(MOVIES, 3455), "3455")
        self.assertEqual(A.red_repo_tag(INFRA, 3455), "infra-3455")
        self.assertEqual(A.red_repo_key(MOVIES, "abc"), "abc")
        self.assertNotEqual(A.red_repo_key(INFRA, "abc"), A.red_repo_key(CORE, "abc"))
        self.assertEqual(A.red_repo_tag("o/r", 7), "7", "an unconfigured repo is the primary, as before")


class TickTest(RedReposBase):
    def test_an_infra_red_is_opened_in_its_repo_and_family_and_no_movies_tick_touches_it(self):
        # movies has a `Stage NixOS closures` workflow too, and its newest run on main is GREEN
        runs = json.load(open(os.path.join(self.d, "runs.json")))
        self.put("runs.json", runs + [run(9500, 3600, STAGE, "nix-stage-closures.yaml", self.sha,
                                          created="2026-10-29T06:00:00Z")])
        self.tick()  # movies: its own Main red (RedBase's)
        acts = self.others()
        reds = self.by_repo()
        self.assertEqual(len(reds[MOVIES]), 1)
        self.assertNotIn("repo", reds[MOVIES][0]["data"])
        (it,) = reds[INFRA]
        d = it["data"]
        self.assertEqual(d["repo"], INFRA)
        self.assertEqual(d["family"], "infra-repo")
        self.assertEqual(d["workflow"], STAGE)
        self.assertIn(NIXBLD, d["line"])
        self.assertEqual(d["key"], A.red_repo_key(INFRA, A.red_key(STAGE, "stage", d["line"])))
        self.assertEqual(it["state"], "offered", acts)
        self.assertTrue(A.red_what(d).startswith(f"infra {STAGE} #3455"), A.red_what(d))
        self.assertEqual(A.red_queue_family(d), "infra-repo")
        self.assertEqual(self.st.offer_of(f"red:{it['id']}")["picture"]["repo"], INFRA)
        for _ in range(3):  # the primary's observation does not hold the infra red: it never clears it
            self.tick(advance=60)
        self.assertEqual([i["id"] for i in self.by_repo()[INFRA]], [it["id"]])

    def test_a_green_gate_run_of_its_own_repo_clears_it(self):
        self.others()
        self.tick()
        self.infra_green(9002, 3456)
        self.others(advance=60)
        self.assertNotIn(INFRA, self.by_repo())
        self.assertIn(MOVIES, self.by_repo(), "movies' red is its own repo's")
        (done,) = [i for i in self.st.items("red") if A.red_repo_of(i["data"]) == INFRA]
        self.assertEqual(done["state"], "cleared")

    def test_a_red_on_a_workflow_outside_its_repos_gate_clears_on_its_own_newer_green_run(self):
        """core Dependabot Updates #154 (2026-11-02): a red on a workflow that is not core's gate never cleared -- the
        other repos' snapshot read the gate's runs only, so green #155/#156 were never seen."""
        dep = "Dependabot Updates"
        data = {"key": A.red_repo_key(INFRA, A.red_key(dep, "Dependabot", "update_files failed")), "workflow": dep,
                "jobs": ["Dependabot"], "job": "Dependabot", "kind": "test", "line": "update_files failed",
                "red_kind": "outside", "gate": False, "runs": ["#154"], "repo": INFRA, "attempts": 0, "history": [],
                "last_run": {"databaseId": 8000, "number": 154}, "opened_at": self.t[0]}
        it = self.st.put_item("red", data["key"], "red", data)
        runs = json.load(open(os.path.join(self.d, "runs-bitcashier_infra.json")))
        self.others()
        self.assertNotIn(self.st.item("red", it["id"])["state"], A.terminal_states("red"), "no newer run yet: it stays")
        self.put("runs-bitcashier_infra.json", [run(8100, 156, dep, "dynamic/dependabot/dependabot-updates", self.sha)] + runs)
        self.assertIn(dep, A.red_gate_snap(INFRA, self.st, ctx=case_context())["latest"])
        self.others(advance=60)
        self.assertEqual(self.st.item("red", it["id"])["state"], "cleared")

    def test_another_repos_snapshot_never_shows_through_the_primarys_run_lists(self):
        """Review 2026-11-26 (B F1): the run lists were process globals keyed by workflow NAME; red_gate_snap filled
        them with another repo's runs and put the primary's back in a `finally`. Meanwhile -- the gate-publish thread,
        or this tick's own reads -- the primary's `Main #5` named the other repo's run. One MainRuns per repository."""
        names = ("red_open_flows", "red_gate_flows")
        saved = {n: getattr(A, n) for n in names}
        seen = []
        sha = self.sha

        class Runs(A.CiRuns):
            def main_runs(self, o_r, wfile, limit=30, status=None, fresh_for=0):
                return [{"databaseId": 100 if o_r == MOVIES else 200, "number": 5, "status": "completed",
                         "conclusion": "failure", "workflowName": "Main", "headSha": sha}]
        ctx = A.Context(runs=Runs())
        try:
            A.red_gate_flows = lambda o_r: [("Main", "main.yml")]
            A.red_open_flows = lambda o_r, store: seen.append(A.resolve_run(MOVIES, "5", ctx=ctx)[1]["databaseId"]) or []
            A.gate_runs(MOVIES, [("Main", "main.yml")], ctx=ctx)
            A.red_gate_snap(INFRA, self.st, ctx=ctx)
        finally:
            for n, v in saved.items():
                setattr(A, n, v)
        self.assertEqual(seen, [100])

    def test_an_unreadable_repo_is_skipped_and_the_others_still_tick(self):
        saved = A.gate_runs

        def gate_runs(o_r, flows=None, *, ctx):
            if o_r == CORE:
                raise SystemExit("agentctl: HTTP 403 on bitcashier/core")
            return saved(o_r, flows, ctx=ctx)
        A.gate_runs = gate_runs
        try:
            self.others()
        finally:
            A.gate_runs = saved
        self.assertIn(INFRA, self.by_repo())

    def on_ci_runner(self):
        """The reconciler's environment: role server, no reader App configured yet (reader_app() configures it)."""
        saved = A.on_ci_runner, A.ghapp_enabled, A.ghapp_config, A.ghapp_token
        A.on_ci_runner = lambda: True
        self.apps = set()
        A.ghapp_enabled = lambda: bool(self.apps)
        A.ghapp_config = lambda role: ("1", "key") if role in self.apps else None
        A.ghapp_token = lambda o_r: "reader-tok"

        def restore():
            A.on_ci_runner, A.ghapp_enabled, A.ghapp_config, A.ghapp_token = saved
        self.addCleanup(restore)

    def test_ci_runner_without_the_reader_app_says_no_read_access_once_as_a_condition(self):
        """2026-10-30: every tick logged `red tick: bitcashier/infra: skipped (... failed to get runs)`: the lander can
        read movies only. Now: one condition naming the App it needs, no line per tick, cleared once it reads."""
        import contextlib, io
        self.on_ci_runner()
        with self.assertRaises(A.NoReadAccess):
            A.token(INFRA)
        errb = io.StringIO()
        with contextlib.redirect_stderr(errb):
            for _ in range(3):
                self.others(advance=60)
        self.assertNotIn("skipped", errb.getvalue())
        self.assertNotIn(INFRA, self.by_repo())
        conds = [c for c in A.open_conditions(A.CurrentStore(A.process_store)) if c["key"] == "red-read-access"]
        self.assertEqual(len(conds), 1)
        self.assertIn("needs the agentctl-reader App", conds[0]["text"])
        self.assertIn(INFRA, conds[0]["text"])
        self.assertIn(CORE, conds[0]["text"])
        self.apps.add("reader")  # the App configured on the host
        self.assertIsNone(A.red_read_token(INFRA))
        A.recovery_tick(A.CurrentStore(A.process_store), registry(A), force=True)
        self.assertEqual([c for c in A.open_conditions(A.CurrentStore(A.process_store)) if c["key"] == "red-read-access"], [])
        self.others(advance=60)
        self.assertIn(INFRA, self.by_repo())

    def test_a_repo_that_keeps_failing_is_said_on_a_new_reason_or_every_ten_minutes(self):
        A._others_said.clear()
        self.assertIsNotNone(A.red_others_skipped(CORE, "HTTP 500", now_=1000))
        self.assertIsNone(A.red_others_skipped(CORE, "HTTP 500", now_=1060))
        self.assertIsNotNone(A.red_others_skipped(CORE, "HTTP 502", now_=1070))
        self.assertIn("(still)", A.red_others_skipped(CORE, "HTTP 502", now_=1070 + A.RED_SKIP_SAY))

    def test_another_repos_tick_leaves_the_primarys_run_lists_alone(self):
        case_context().runs.main_runs_of(MOVIES).gate["Main"] = ["movies"]
        self.others()
        self.assertEqual(case_context().runs.main_runs_of(MOVIES).gate, {"Main": ["movies"]})


class DispatchTest(RedReposBase):
    def test_an_infra_offer_is_spawned_in_the_infra_clone_and_a_movies_offer_in_movies(self):
        spawned = []
        saved = A.red_spawn, A.cached_dispatch_blocker, A.red_capacity
        A.red_spawn = lambda store, o_r, repo, off, took, extra=(): spawned.append((o_r, repo)) or (True, "ok")
        A.cached_dispatch_blocker = lambda: None
        A.red_capacity = lambda *a, **k: (True, None)
        try:
            self.tick()
            self.others()
            A.red_dispatcher(self.st, MOVIES, self.r.path, self.t[0])
        finally:
            A.red_spawn, A.cached_dispatch_blocker, A.red_capacity = saved
        self.assertEqual(sorted(spawned), sorted([(MOVIES, self.r.path), (INFRA, self.infra)]))

    def test_a_repo_with_no_clone_here_is_offered_with_the_reason(self):
        saved = A.red_spawn, A.cached_dispatch_blocker, A.red_capacity
        A.red_spawn = lambda *a, **k: self.fail("no clone: no spawn")
        A.cached_dispatch_blocker = lambda: None
        A.red_capacity = lambda *a, **k: (True, None)
        try:
            self.infra_green(9002, 3456)
            self.put("runs-bitcashier_core.json",
                     [run(9101, 801, "Main Pipeline", "main-pipeline.yml", self.sha, conclusion="failure")])
            self.put("run-9101.json", {"jobs": [{"name": "test", "status": "completed", "conclusion": "failure",
                                                 "steps": [{"name": "sbt test", "conclusion": "failure"}]}],
                                       "headSha": self.sha, "url": "u", "number": 801, "status": "completed",
                                       "conclusion": "failure", "workflowName": "Main Pipeline"})
            self.put("run-9101.log", "test\tsbt test\t2026-10-28T07:14:00Z [info] - ApiSpec should x *** FAILED ***\n")
            self.others()
            A.red_dispatcher(self.st, MOVIES, self.r.path, self.t[0])
        finally:
            A.red_spawn, A.cached_dispatch_blocker, A.red_capacity = saved
        (it,) = self.by_repo()[CORE]
        self.assertEqual(it["data"]["family"], "core-repo")
        self.assertIn("no clone of bitcashier/core", self.st.offer_of(f"red:{it['id']}")["reason"])


class BriefTest(RedReposBase):
    U = {"workflow": STAGE, "run": {"number": 3455, "databaseId": 9001}, "job": "stage", "kind": "test", "line": NIXBLD}

    def test_a_red_gets_only_its_own_repos_section_of_the_brief(self):
        infra = A.fixer_prompt(dict(self.U, repo=INFRA), "/wt", "/brief")
        movies = A.fixer_prompt(dict(self.U, workflow="Main"), "/wt", "/brief")
        self.assertIn("### REPO bitcashier/infra", infra)
        self.assertNotIn("### REPO bitcashier/core", infra)
        self.assertNotIn("### REPO pawelkrupinski/movies", infra)
        self.assertIn("### REPO pawelkrupinski/movies", movies)
        self.assertNotIn("### REPO bitcashier/infra", movies)
        self.assertIn("## Bounded commands and memory", infra, "the general rules stay")
        self.assertIn("YOUR RED IS IN bitcashier/infra", infra)
        self.assertNotIn("YOUR RED IS IN", movies)

    def test_an_owner_only_repos_task_never_says_land_it_last(self):
        p = A.fixer_prompt(dict(self.U, repo=INFRA), "/wt", "/brief")
        task = p.split("=== END OF THE FIXER BRIEF ===", 1)[1]
        self.assertIn("OWNER-ONLY REPO (bitcashier/infra)", task)
        self.assertIn("red owner-push 3455", task)
        self.assertGreater(task.index("OWNER-ONLY REPO"), task.index("land it with"), "the override comes after")
        core = A.fixer_prompt(dict(self.U, repo=CORE, workflow="Main Pipeline"), "/wt", "/brief")
        self.assertNotIn("OWNER-ONLY", core)

    def test_an_agentctl_fixer_is_briefed_with_its_runs_replay_command_and_told_never_to_rerun(self):
        # 2026-11-25: agentctl's own tests.yml red got an alert (WA) and no fixer; the red machine now watches the repo
        # (red-repos.json) and the fixer's task carries the failing run's REPLAY line from its log
        with open(os.path.join(os.path.dirname(A.TOOL_PATH), "red-repos.json")) as f:
            tracked = json.load(f)
        with open(self.repos_file, "w") as f:
            json.dump({"repos": [r for r in tracked["repos"] if r["repo"] in (MOVIES, AGENTCTL)]}, f)
        A._red_repos_cache.clear()
        log = ("suite\tstep\tFAIL: test_reds.BackgroundSessionTest.test_x\nsuite\tstep\tseed: 4242\n"
               "suite\tstep\tREPLAY: python3 tests/run.py --seed 4242 test_reds.BackgroundSessionTest\n")
        asked = []
        note = A.red_repo_note(AGENTCTL, {"number": 9, "databaseId": 77}, lambda o_r, rid: asked.append((o_r, rid)) or log)
        self.assertEqual(asked, [(AGENTCTL, 77)])
        u = dict(self.U, repo=AGENTCTL, workflow="tests", repo_note=note)
        task = A.fixer_prompt(u, "/wt", "/brief").split("=== END OF THE FIXER BRIEF ===", 1)[1]
        for text in ("python3 devtools/selfpush.py", "never `agentctl land`", "replay: python3 tests/run.py --seed 4242 "
                     "test_reds.BackgroundSessionTest", "NEVER rerun the run to green", "never quarantine"):
            self.assertIn(text, task)
        self.assertGreater(task.index("REPO RULES"), task.index("land it with"), "the override comes after")
        self.assertEqual(A.red_repo_note(CORE, {"databaseId": 1}, lambda *a: self.fail("no note: no log read")), "")

    def test_the_brief_filter_keeps_text_after_the_last_section(self):
        text = "# B\nintro\n## Per-repo rules\n### REPO a/a\nA rules\n### REPO b/b\nB rules\n## Tail\ntail text\n"
        self.assertEqual(A.fixer_brief_for(text, "a/a"),
                         "# B\nintro\n## Per-repo rules\n### REPO a/a\nA rules\n## Tail\ntail text")


class OwnerPushTest(RedReposBase):
    def fixer_tree(self):
        wt = os.path.join(os.path.realpath(self.tmp.name), "infra-wt-red-3455")
        sh(self.infra, "git", "fetch", "-q", "origin")
        sh(self.infra, "git", "worktree", "add", "-q", "-b", "red-3455", wt, "origin/main")
        with open(os.path.join(wt, "bin-nix-container"), "w") as f:
            f.write("ro\n")
        sh(wt, "git", "add", "-A")
        sh(wt, "git", "commit", "-q", "-m", "ci: the store read-only\n\nFixes-Red: 3455")
        return wt

    def infra_item(self):
        (it,) = self.by_repo()[INFRA]
        return self.st.item("red", it["id"])

    def test_the_fix_is_filed_with_the_owner_and_a_green_gate_run_verifies_it(self):
        self.others()
        wt = self.fixer_tree()
        rc, lines = A.red_owner_push(self.st, INFRA, "3455", "the store read-only; restarts nothing", wt, ctx=case_context())
        self.assertEqual(rc, 0, lines)
        cmd = f"git -C {wt} pull --rebase origin main && git -C {wt} push origin HEAD:main"
        self.assertEqual(lines[-1], f"BLOCKED ON OWNER: {cmd}")
        it = self.infra_item()
        self.assertEqual(it["state"], "fix-pending")
        self.assertEqual(it["data"]["fix"]["status"], "owner")
        self.assertEqual(it["data"]["fix"]["cmd"], cmd)
        esc = [e for e in self.inbox() if str(e.get("verdict")).startswith("RED OWNER PUSH")]
        self.assertEqual(len(esc), 1, self.inbox())
        self.assertNotIn(it["id"], [u["id"] for u in self.unheld()], "a filed owner push is a fix on its way")
        self.others(advance=60)  # still red, nothing proves it yet: it waits
        self.assertEqual(self.infra_item()["state"], "fix-pending")
        self.infra_green(9002, 3456)
        self.others(advance=60)
        self.assertEqual(self.st.item("red", it["id"])["state"], "verified")

    def test_an_unproven_owner_push_is_offered_again_after_the_giveup(self):
        self.others()
        rc, _lines = A.red_owner_push(self.st, INFRA, "3455", "", self.fixer_tree(), ctx=case_context())
        self.assertEqual(rc, 0)
        self.others(advance=A.RED_FIX_GIVEUP + 60)
        it = self.infra_item()
        self.assertEqual(it["state"], "offered")
        self.assertIn("owner push", it["data"]["history"][-1]["outcome"])

    def test_it_is_refused_for_a_repo_that_lands_and_without_commits(self):
        self.others()
        rc, lines = A.red_owner_push(self.st, CORE, "3455", "", self.r.path, ctx=case_context())
        self.assertEqual(rc, 2)
        self.assertIn("not owner-only", lines[0])
        wt = os.path.join(os.path.realpath(self.tmp.name), "infra-wt-empty")
        sh(self.infra, "git", "fetch", "-q", "origin")
        sh(self.infra, "git", "worktree", "add", "-q", "-b", "empty", wt, "origin/main")
        rc, lines = A.red_owner_push(self.st, INFRA, "3455", "", wt, ctx=case_context())
        self.assertEqual(rc, 1)
        self.assertIn("nothing in", lines[0])

    def test_land_refuses_an_infra_fixer_and_its_session_denies_push_and_deploy(self):
        wt = self.fixer_tree()
        os.environ["AGENTCTL_RED_FIXER"] = "3455"
        saved_sid = os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
        try:
            rc, out = self.tool("land", cwd=wt)
        finally:
            os.environ.pop("AGENTCTL_RED_FIXER", None)
            if saved_sid is not None:
                os.environ["CLAUDE_CODE_SESSION_ID"] = saved_sid
        self.assertEqual(rc, 1, out)
        self.assertIn("NOT LANDED: bitcashier/infra is owner-only", out)
        for cmd in ("agentctl land", "bin/deploy", "nixos-rebuild", "git*push"):
            self.assertTrue(any(cmd in d for d in A.OWNER_ONLY_DENIED), cmd)


if __name__ == "__main__":
    unittest.main()
