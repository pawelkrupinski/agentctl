"""The forge switch (lib/agentctl/forge.py): forge = forgejo (the default, the forgejo-gh shim and /api/v1) or github
(the real gh and `gh api`). The same scenarios run against both forges' fakes -- tests/fake_forgejo_gh.py (plus a
Forgejo-shaped /api/v1 served in-process) and tests/fake_gh.py (GitHub's CLI and REST shapes) -- and must read the
same: both forges stay tested until Phase 4."""
import contextlib, io, json, os, sys, tempfile, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402

FAKE_FORGEJO = os.path.join(HERE, "fake_forgejo_gh.py")
FAKE_GH = os.path.join(HERE, "fake_gh.py")
O_R = "pawelkrupinski/movies"


class ForgeCase:
    """A scenario base: FORGE names the forge, the fakes serve both, `serve_*` put one fact in the forge's own shape."""
    FORGE = None

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = os.path.join(self.tmp.name, "gh")
        os.makedirs(self.d)
        self.env = {"AGENTCTL_FORGE": self.FORGE, "FAKE_GH_DIR": self.d, "GH_TOKEN": "dummy",
                    "AGENTCTL_SCRATCH": os.path.join(self.tmp.name, "scratch"),
                    "AGENTCTL_DISPATCH_DIR": os.path.join(self.tmp.name, "dispatches"),
                    "AGENTCTL_DISPATCH_FIND": "1", "AGENTCTL_DISPATCH_FIND_POLL": "0"}
        self.saved_env = {k: os.environ.get(k) for k in self.env}
        os.environ.update(self.env)
        self.saved = {k: getattr(A, k) for k in ("FORGEJO_GH", "GH_CLI", "_api_once")}
        A.FORGEJO_GH, A.GH_CLI = FAKE_FORGEJO, FAKE_GH
        self.rest, self.rest_calls = {}, []  # Forgejo's /api/v1, in-process: (method, path) -> answer

        def forgejo_api(o_r, method, path, params=None, body=None):
            self.rest_calls.append((method, path, dict(params or {}), body))
            key = (method, path, tuple(sorted((params or {}).items())))
            if key in self.rest:
                return self.rest[key]
            if (method, path) in self.rest:
                return self.rest[(method, path)]
            if method == "GET":
                raise RuntimeError(f"HTTP 404: {path}")
            return None
        A._api_once = forgejo_api

    def tearDown(self):
        for k, v in self.saved.items():
            setattr(A, k, v)
        for k, v in self.saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp.cleanup()

    # -- the fakes' data
    def put(self, name, obj):
        with open(os.path.join(self.d, name), "w") as f:
            f.write(obj if isinstance(obj, str) else json.dumps(obj))

    def calls(self):
        p = os.path.join(self.d, "calls.log")
        return [l.split(" ", 1)[1] for l in open(p).read().splitlines()] if os.path.exists(p) else []

    def gh_api(self, path, obj):
        import re
        self.put("api-" + re.sub(r"[/?&=]", "_", path) + ".json", obj)

    def tool(self, *argv):
        out, errb = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(errb):
            rc = A.main(list(argv))
        return rc, out.getvalue(), errb.getvalue()

    # -- the scenarios: the same answer from either forge
    def test_the_switch_names_this_forge_and_its_cli(self):
        self.assertEqual(A.forge(), self.FORGE)
        self.assertEqual(A.forge_cli(), FAKE_GH if self.FORGE == "github" else FAKE_FORGEJO)

    def test_run_list_goes_through_this_forges_cli(self):
        self.put("runs.json", [{"databaseId": 9, "number": 3, "status": "completed", "conclusion": "success",
                                "headBranch": "main", "workflowName": "Main"}])
        runs = A.gh_json(O_R, "run", "list", "--branch", "main", "--limit", "5", "--json", A.RUN_FIELDS)
        self.assertEqual([r["databaseId"] for r in runs], [9])
        self.assertEqual(self.calls(), [f"run list --branch main --limit 5 --json {A.RUN_FIELDS} -R {O_R}"])
        self.assertEqual(os.path.exists(os.path.join(self.d, "gh-calls.log")), self.FORGE == "github")

    def test_failed_log_and_takes_artifacts(self):
        self.put("run-5.log", "takes (pl)\tRun\t2026-10-30T10:00:00.0Z *** FAILED *** boom\n")
        self.assertIn("boom", A.relevant_lines(A.fetch_failed_log(O_R, 5), 5)["takes (pl)"][0])
        self.gh_api(f"repos/{O_R}/actions/runs/5/artifacts", {"artifacts": [{"name": "identity-takes-pl"}, {"name": "x"}]})
        self.put("art-5-identity-takes-pl.txt", "OK 12 takes")
        self.assertEqual(A.fetch_takes_verdicts(O_R, 5), {"identity-takes-pl": "OK 12 takes"})
        self.assertIn(f"run download 5 -n identity-takes-pl", " ".join(self.calls()))

    def test_cancel(self):
        A.cancel_run(O_R, 77, "superseded")
        self.assertIn(f"run cancel 77 -R {O_R}", self.calls())

    def test_dispatch_run_is_recorded_with_its_run_and_inputs(self):
        self.put("runs.json", [{"databaseId": 41, "number": 7, "status": "queued", "conclusion": "", "headBranch": "measure/x",
                                "workflowName": "conv", "workflowFile": "conv.yml", "createdAt": "2099-01-01T00:00:00Z"}])
        rc, out, _ = self.tool("dispatch", "run", "conv.yml", "--ref", "measure/x", "-f", "runs=3", "--repo", O_R,
                               "--purpose", "t")
        self.assertEqual(rc, 0, out)
        self.assertIn("run 41", out)
        self.assertIn(f"workflow run conv.yml --ref measure/x -f runs=3 -R {O_R}", self.calls())
        self.assertEqual(A.read_record(A.dispatches_dir("inputs", "41.json")), {"runs": "3"})

    def test_measure_branches_read_as_name_sha_time(self):
        self.serve_branches([("main", "a" * 40, "2026-10-30T09:00:00Z"), ("measure/p/1-x", "b" * 40, "2026-10-30T10:00:00Z")])
        got = A.forge_branches(O_R, "measure/")
        self.assertEqual([(n, s, w.isoformat()) for n, s, w in got], [("measure/p/1-x", "b" * 40, "2026-10-30T10:00:00+00:00")])

    def test_actions_runs_read_in_forgejos_shape(self):
        self.serve_runs([dict(id=101, number=12, sha="c" * 40, branch="check/7", done=True, ok=False),
                         dict(id=100, number=11, sha="d" * 40, branch="main", done=False, ok=None)])
        runs = A.forge_actions_runs(O_R, workflow="precommit-check.yml")
        self.assertEqual([(r["id"], str(r["index_in_repo"]), r["commit_sha"], r["prettyref"].replace("refs/heads/", ""),
                           r["status"]) for r in runs],
                         [(101, "12", "c" * 40, "check/7", "failure"), (100, "11", "d" * 40, "main", "running")])
        self.assertEqual(A.run_id_of_number(O_R, 12), 101)

    def test_remote_poll_finds_and_decides_the_check_run(self):
        self.serve_runs([dict(id=101, number=12, sha="c" * 40, branch="check/7", done=True, ok=True)])
        rem = A.remote_poll(O_R, {"tip": "c" * 40, "branch": "check/7"})
        self.assertEqual((rem["run"], rem["status"]), (101, "success"))

    def test_run_url(self):
        url = A.forge_run_url(O_R, 12, {"databaseId": 101})
        self.assertEqual(url, f"https://github.com/{O_R}/actions/runs/101" if self.FORGE == "github"
                         else f"http://localhost:3000/{O_R}/actions/runs/12")


class ForgejoForgeTest(ForgeCase, unittest.TestCase):
    FORGE = "forgejo"

    def serve_branches(self, bs):
        self.rest[("GET", "branches")] = [{"name": n, "commit": {"id": s, "timestamp": t}} for n, s, t in bs]

    def serve_runs(self, runs):
        rs = [{"id": r["id"], "index_in_repo": r["number"], "commit_sha": r["sha"], "prettyref": r["branch"],
               "status": ("success" if r["ok"] else "failure") if r["done"] else "running", "html_url": "u"} for r in runs]
        self.rest[("GET", "actions/runs")] = {"workflow_runs": rs}
        for r in rs:
            self.rest[("GET", f"actions/runs/{r['id']}")] = r

    def test_the_default_is_github(self):
        # Forgejo was switched off on 2026-10-31: with no setting, agentctl reads github.com.
        os.environ.pop("AGENTCTL_FORGE")
        self.assertEqual(A.forge(), "github")

    def test_forgejo_api_paths_are_unchanged(self):
        self.serve_runs([dict(id=5, number=1, sha="e" * 40, branch="main", done=True, ok=True)])
        A.forge_actions_runs(O_R, workflow="precommit-check.yml")
        A.forge_delete_branch(O_R, "measure/a/b")
        self.assertEqual([c[:3] for c in self.rest_calls],
                         [("GET", "actions/runs", {"workflow_id": "precommit-check.yml", "limit": 50}),
                          ("DELETE", "branches/measure%2Fa%2Fb", {})])

    def test_inputs_come_from_the_event_payload(self):
        self.rest[("GET", "actions/runs/9")] = {"id": 9, "event_payload": json.dumps({"inputs": {"runs": 2}})}
        self.assertEqual(A.run_dispatch_inputs(O_R, 9), {"runs": "2"})

    def test_a_forgejo_token_is_handed_to_detached_children(self):
        self.assertEqual(A.token_env(O_R, {}).get("FORGEJO_API_TOKEN"), "dummy")


class GithubForgeTest(ForgeCase, unittest.TestCase):
    FORGE = "github"

    def serve_branches(self, bs):
        self.gh_api(f"repos/{O_R}/branches?per_page=100&page=1", [{"name": n, "commit": {"sha": s}} for n, s, _ in bs])
        for _n, s, t in bs:
            self.gh_api(f"repos/{O_R}/commits/{s}", {"sha": s, "commit": {"committer": {"date": t}}})

    def serve_runs(self, runs):
        rs = [{"id": r["id"], "run_number": r["number"], "head_sha": r["sha"], "head_branch": r["branch"],
               "status": "completed" if r["done"] else "in_progress",
               "conclusion": ("success" if r["ok"] else "failure") if r["done"] else None,
               "html_url": f"https://github.com/{O_R}/actions/runs/{r['id']}",
               "path": ".github/workflows/precommit-check.yml"} for r in runs]
        self.gh_api(f"repos/{O_R}/actions/workflows/precommit-check.yml/runs?per_page=50", {"workflow_runs": rs})
        self.gh_api(f"repos/{O_R}/actions/runs?per_page=100", {"workflow_runs": rs})
        for r in rs:
            self.gh_api(f"repos/{O_R}/actions/runs/{r['id']}", r)

    def test_gh_api_takes_no_repo_flag(self):
        self.gh_api(f"repos/{O_R}/git/commits/abc", {"message": "m"})
        self.assertEqual(A.gh_json(O_R, "api", f"repos/{O_R}/git/commits/abc"), {"message": "m"})
        self.assertEqual(self.calls(), [f"api repos/{O_R}/git/commits/abc"])

    def test_a_missing_branch_is_a_404(self):
        self.assertFalse(A.branch_exists(O_R, "measure/gone"))

    def test_github_status_folds_into_forgejos(self):
        f = A.github_run_status
        self.assertEqual([f("queued", None), f("in_progress", None), f("completed", "success"), f("completed", "timed_out"),
                          f("completed", "cancelled"), f("completed", "skipped"), f("completed", "startup_failure")],
                         ["waiting", "running", "success", "failure", "cancelled", "skipped", "failure"])

    def test_branch_delete_deletes_the_ref_after_reading_its_sha(self):
        self.gh_api(f"repos/{O_R}/branches/measure%2Fa%2Fb", {"name": "measure/a/b", "commit": {"sha": "f" * 40}})
        A.delete_remote_branch(None, O_R, "measure/a/b", "f" * 40)
        self.assertIn(f"api -X DELETE -H Accept: application/vnd.github+json repos/{O_R}/git/refs/heads/measure/a/b",
                      self.calls())
        with self.assertRaises(RuntimeError):
            A.delete_remote_branch(None, O_R, "measure/a/b", "0" * 40)

    def test_a_verify_dispatch_posts_the_body_through_gh_api(self):
        A.api(O_R, "POST", "actions/workflows/conv.yml/dispatches", body={"ref": "main", "inputs": {"runs": "2"}})
        calls = self.calls()
        self.assertIn(f"api -X POST -H Accept: application/vnd.github+json repos/{O_R}/actions/workflows/conv.yml/dispatches"
                      " --input -", calls)
        self.assertEqual(json.loads(calls[-1][len("BODY "):]), {"ref": "main", "inputs": {"runs": "2"}})

    def test_inputs_come_from_agentctls_own_dispatch_record(self):
        A.write_json(A.dispatches_dir("inputs", "9.json"), {"runs": 2})
        self.assertEqual(A.run_dispatch_inputs(O_R, 9), {"runs": "2"})
        self.assertEqual(A.run_dispatch_inputs(O_R, 10), {})
        self.assertEqual(A.db_inputs([9, 10]), {})  # never Forgejo's DB
        self.assertEqual(self.calls(), [])

    def test_the_workflow_file_of_a_run_view(self):
        self.gh_api(f"repos/{O_R}/actions/workflows/555", {"id": 555, "path": ".github/workflows/main.yml"})
        self.assertEqual(A.forge_workflow_file(O_R, {"workflowDatabaseId": 555}), "main.yml")

    def test_release_pins_download_through_gh(self):
        self.gh_api(f"repos/{O_R}/releases/tags/fixtures", {"assets": [{"name": "hermetic-pl.txt"}, {"name": "x.bin"}]})
        self.put("release-fixtures-hermetic-pl.txt", "4242 recorded\n")
        self.assertEqual(A.read_pins(O_R, "fixtures"), {"pl": "4242"})
        self.assertIn(f"release download fixtures -p hermetic-pl.txt -O - -R {O_R}", self.calls())

    def test_no_forgejo_token_on_github(self):
        os.environ.pop("GH_TOKEN")
        self.assertEqual(A.token_env(O_R, {}), {})
        self.assertNotIn("GH_TOKEN", A.forge_env(O_R, {}))


class ForgeConfigTest(unittest.TestCase):
    def test_agentctl_toml_sets_the_forge_and_a_bad_name_stops(self):
        with tempfile.TemporaryDirectory() as t:
            p = os.path.join(t, "agentctl.toml")
            with open(p, "w") as f:
                f.write('forge = "github"\n')
            saved = {k: os.environ.get(k) for k in ("AGENTCTL_CONFIG", "AGENTCTL_FORGE", "AGENTCTL_CONFIG_SET")}
            try:
                os.environ.pop("AGENTCTL_FORGE", None)
                os.environ["AGENTCTL_CONFIG"] = p
                A.config_load()
                self.assertEqual(A.forge(), "github")
                os.environ["AGENTCTL_FORGE"] = "gitlab"
                with self.assertRaises(SystemExit):
                    A.forge()
            finally:
                for k, v in saved.items():
                    if v is None:
                        os.environ.pop(k, None)
                    else:
                        os.environ[k] = v
                A.CONFIG_SOURCE.pop("AGENTCTL_FORGE", None)


if __name__ == "__main__":
    unittest.main()
