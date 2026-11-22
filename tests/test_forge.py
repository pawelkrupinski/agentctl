"""The forge (lib/agentctl/forge.py): GitHub, through the real gh and `gh api`, against tests/fake_gh.py (GitHub's CLI
and REST shapes)."""
import contextlib, io, json, os, sys, tempfile, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402

FAKE_GH = os.path.join(HERE, "fake_gh.py")
O_R = "pawelkrupinski/movies"


class GithubForgeTest(unittest.TestCase):
    """`serve_*` put one fact in GitHub's own shape."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = os.path.join(self.tmp.name, "gh")
        os.makedirs(self.d)
        self.env = {"FAKE_GH_DIR": self.d, "GH_TOKEN": "dummy",
                    "AGENTCTL_SCRATCH": os.path.join(self.tmp.name, "scratch"),
                    "AGENTCTL_DISPATCH_DIR": os.path.join(self.tmp.name, "dispatches"),
                    "AGENTCTL_DISPATCH_FIND": "1", "AGENTCTL_DISPATCH_FIND_POLL": "0"}
        self.saved_env = {k: os.environ.get(k) for k in self.env}
        os.environ.update(self.env)
        self.saved_gh, A.GH_CLI = A.GH_CLI, FAKE_GH
        A._workflow_names.clear()  # the workflows' names another test's runs listed

    def tearDown(self):
        A.GH_CLI = self.saved_gh
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

    # -- the scenarios
    def test_run_list_is_one_conditional_rest_read(self):
        # 2026-11-10: `gh run list` sent no ETag and cost 2-3 requests (a workflows lookup, a workflows page to name
        # the runs, the runs) of the App installation's 5,000 an hour; the gate read five of them every 45 s.
        self.put("runs.json", [{"databaseId": 9, "number": 3, "status": "in_progress", "conclusion": "",
                                "headBranch": "main", "workflowName": "Main", "workflowFile": "main.yml"},
                               {"databaseId": 8, "number": 2, "status": "completed", "conclusion": "success",
                                "headBranch": "land/x", "workflowName": "Main", "workflowFile": "main.yml"}])
        runs = A.gh_json(O_R, "run", "list", "--workflow", "main.yml", "--branch", "main", "--limit", "5",
                         "--json", A.RUN_FIELDS)
        self.assertEqual([(r["databaseId"], r["number"], r["status"], r["conclusion"], r["workflowName"])
                          for r in runs], [(9, 3, "in_progress", "", "Main")])
        self.assertEqual(sorted(runs[0]), sorted(A.RUN_FIELDS.split(",")))
        calls = self.calls()
        self.assertEqual(calls[0], f"api -i -X GET -H Accept: application/vnd.github+json repos/{O_R}/actions/"
                                   f"workflows/main.yml/runs?exclude_pull_requests=true&branch=main&per_page=5&page=1")
        # and the workflows' names: once per WORKFLOW_NAMES_TTL, conditional (a free 304 while they stand)
        self.assertEqual(len(calls), 2)
        self.assertTrue(calls[1].endswith(f"repos/{O_R}/actions/workflows?per_page=100"), calls[1])
        A.gh_json(O_R, "run", "list", "--workflow", "main.yml", "--branch", "main", "--limit", "5", "--json", A.RUN_FIELDS)
        self.assertEqual(len(self.calls()), 3, "a second list reads only the runs")

    def test_a_run_with_a_run_name_is_listed_under_its_workflows_name(self):
        # 2026-11-11..12: GitHub's REST `name` is a run's RUN-NAME. Read as workflowName, every Identity takes run
        # ("Identity takes (convergence run <id>)") was a workflow of its own: outside the gate, a red keyed per run that
        # no later run could verify or clear (b059f05c70fa), and stuck-run limits read for a name with no history.
        self.put("runs.json", [{"databaseId": 9, "number": 56, "status": "completed", "conclusion": "success",
                                "headBranch": "main", "workflowName": "Identity takes", "workflowFile": "identity-takes.yml",
                                "runName": "Identity takes (convergence run 43111958761)"},
                               {"databaseId": 8, "number": 7, "status": "completed", "conclusion": "success",
                                "headBranch": "main", "workflowName": "Main", "workflowFile": "main.yml"}])
        runs = A.gh_json(O_R, "run", "list", "--branch", "main", "--limit", "5", "--json", A.RUN_FIELDS)
        self.assertEqual([(r["workflowName"], r["displayTitle"]) for r in runs],
                         [("Identity takes", "Identity takes (convergence run 43111958761)"), ("Main", None)])
        A.gh_json(O_R, "run", "list", "--branch", "main", "--limit", "5", "--json", A.RUN_FIELDS)
        self.assertEqual(sum("actions/workflows?" in c for c in self.calls()), 1, "the workflows' names are read once")

    def test_run_list_pages_past_a_hundred(self):
        self.put("runs.json", [{"databaseId": i, "status": "completed", "conclusion": "success", "headBranch": "main",
                                "workflowName": "Main"} for i in range(250, 0, -1)])
        runs = A.gh_json(O_R, "run", "list", "--branch", "main", "--limit", "150", "--json", "databaseId")
        self.assertEqual([r["databaseId"] for r in runs], list(range(250, 100, -1)))
        self.assertEqual(len(self.calls()), 2)

    def test_run_list_of_a_workflow_not_on_main_says_what_gh_says(self):
        self.put("runs.json", [])
        self.put("workflows-absent", "identity-takes.yml\n")
        with self.assertRaises(RuntimeError) as e:
            A.gh_json(O_R, "run", "list", "--workflow", "identity-takes.yml", "--branch", "main", "--json", "databaseId")
        self.assertRegex(str(e.exception), A.WORKFLOW_NOT_ON_MAIN)

    def test_a_run_list_rest_cannot_answer_exactly_goes_to_gh(self):
        self.put("runs.json", [{"databaseId": 9, "headBranch": "main", "workflowName": "Main"}])
        A.gh_json(O_R, "run", "list", "--workflow", "Main", "--json", "databaseId")  # a NAME: gh's lookup
        A.gh_json(O_R, "run", "list", "--user", "me", "--json", "databaseId")
        A.gh_json(O_R, "run", "list", "--json", "databaseId,jobs")
        self.assertEqual([c.split(" ", 2)[:2] for c in self.calls()], [["run", "list"]] * 3)

    def test_failed_log_and_takes_artifacts(self):
        self.put("run-5.log", "takes (pl)\tRun\t2026-10-30T10:00:00.0Z *** FAILED *** boom\n")
        self.assertIn("boom", A.relevant_lines(A.fetch_failed_log(O_R, 5), 5)["takes (pl)"][0])
        self.gh_api(f"repos/{O_R}/actions/runs/5/artifacts", {"artifacts": [{"name": "identity-takes-pl"}, {"name": "x"}]})
        self.put("art-5-identity-takes-pl.txt", "OK 12 takes")
        self.assertEqual(A.fetch_takes_verdicts(O_R, 5), {"identity-takes-pl": "OK 12 takes"})
        self.assertIn(f"run download 5 -n identity-takes-pl", " ".join(self.calls()))

    def blipping_gh(self, fails, line="gh: Bad Gateway (HTTP 502)"):
        """A gh that fails the network's way `fails` times, then is the fake GitHub."""
        path, count = os.path.join(self.tmp.name, "blip-gh"), os.path.join(self.tmp.name, "blips")
        with open(path, "w") as f:
            f.write(f"#!/bin/sh\nn=$(cat {count} 2>/dev/null || echo 0)\n"
                    f"if [ \"$n\" -lt {fails} ]; then echo $((n + 1)) > {count}; echo '{line}' >&2; exit 1; fi\n"
                    f"exec {sys.executable} {FAKE_GH} \"$@\"\n")
        os.chmod(path, 0o755)
        A.GH_CLI = path

    def test_a_get_through_a_network_blip_is_tried_again_and_answers(self):
        """2026-11-22: one `gh api` 502 or timeout over the tunnel was the read's answer -- gate_reds' GATE_UNREAD, a
        red to every caller. A GET is tried again (net_run) and the second try's answer is the read's."""
        self.gh_api(f"repos/{O_R}/actions/runs/5", {"id": 5, "status": "completed"})
        self.blipping_gh(2)
        saved, A.NET_RETRY_DELAYS = A.NET_RETRY_DELAYS, (0, 0, 0)
        try:
            self.assertEqual(A.api(O_R, "GET", "actions/runs/5")["id"], 5)
        finally:
            A.NET_RETRY_DELAYS = saved

    def test_a_refusal_is_never_tried_again(self):
        self.blipping_gh(1, "gh: Not Found (HTTP 404)")
        saved, A.NET_RETRY_DELAYS = A.NET_RETRY_DELAYS, (0, 0, 0)
        try:
            with self.assertRaises(A.ForgeHTTPError):
                A.api(O_R, "GET", "actions/runs/5")
        finally:
            A.NET_RETRY_DELAYS = saved
        self.assertEqual(open(os.path.join(self.tmp.name, "blips")).read().strip(), "1", "one try: a 404 is an answer")

    def test_a_mutation_is_never_tried_again_after_a_blip(self):
        self.blipping_gh(1)
        with self.assertRaises(A.ForgeHTTPError):
            A.api(O_R, "POST", "actions/runs/5/cancel")


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

    def test_actions_runs_read_as_run_records(self):
        self.serve_runs([dict(id=101, number=12, sha="c" * 40, branch="check/7", done=True, ok=False),
                         dict(id=100, number=11, sha="d" * 40, branch="main", done=False, ok=None)])
        runs = A.forge_actions_runs(O_R, workflow="precommit-check.yml")
        self.assertEqual([(r["id"], str(r["run_number"]), r["head_sha"], r["head_branch"], r["status"]) for r in runs],
                         [(101, "12", "c" * 40, "check/7", "failure"), (100, "11", "d" * 40, "main", "running")])
        self.assertEqual(A.run_id_of_number(O_R, 12), 101)

    def test_remote_poll_finds_and_decides_the_check_run(self):
        self.serve_runs([dict(id=101, number=12, sha="c" * 40, branch="check/7", done=True, ok=True)])
        rem = A.remote_poll(O_R, {"tip": "c" * 40, "branch": "check/7"})
        self.assertEqual((rem["run"], rem["status"]), (101, "success"))

    # -- the verdict is the check jobs': a report job still queued holds nothing (batch #20, 2026-11-07)
    CHECK_JOBS = [("plan", "completed", "success"), ("check (rest)", "completed", "success"),
                  ("heavy rows (${{ matrix.rows }})", "completed", "skipped"), ("check (identity 1/3)", "completed", "success"),
                  ("check (page)", "completed", "skipped")]

    # the workflow's jobs, as precommit-check.yml lays them out (a step's `- name:` is no job's name)
    WORKFLOW_JOBS = ("jobs:\n    # what the change selects\n    plan:\n        runs-on: x\n        steps:\n"
                     "            - name: What the change selects\n    check:\n        name: check (rest)\n"
                     "    check-identity:\n        name: check (identity ${{ matrix.shard }}/3)\n        needs: [ plan ]\n"
                     "    check-page:\n        name: \"check (page)\"  # needs every other check\n"
                     "        needs: [ plan, check, check-identity, heavy ]\n"
                     "    heavy:\n        name: heavy rows (${{ matrix.rows }})\n"
                     "    timings:\n        name: timings\n        uses: ./.github/workflows/timings.yml\n")

    def serve_check_run(self, jobs, declares=True):
        import base64
        self.serve_runs([dict(id=101, number=12, sha="c" * 40, branch="check/7", done=False, ok=None)])
        wf = "env:\n    LANG: C.UTF-8\n" + ("    VERDICT_JOBS: plan, check (*), heavy rows (*)\n" if declares else "") \
            + "\n" + self.WORKFLOW_JOBS
        self.gh_api(f"repos/{O_R}/contents/.github/workflows/precommit-check.yml?ref={'c' * 40}",
                    {"content": base64.b64encode(wf.encode()).decode()})
        self.gh_api(f"repos/{O_R}/actions/runs/101/jobs?per_page=100",
                    {"jobs": [{"id": 900 + i, "name": n, "status": s, "conclusion": c} for i, (n, s, c) in enumerate(jobs)]})

    def test_remote_poll_is_green_once_every_verdict_job_is_while_a_report_still_queues(self):
        self.serve_check_run(self.CHECK_JOBS + [("timings / report (timings)", "queued", None)])
        rem = A.remote_poll(O_R, {"tip": "c" * 40, "branch": "check/7"})
        self.assertEqual((rem["run"], rem["run_status"], rem["status"]), (101, "running", "success"))
        self.assertIn("not waited for: timings / report (timings)", rem["out"])

    def test_remote_poll_fails_on_a_failed_check_job_while_the_run_goes_on(self):
        jobs = self.CHECK_JOBS[:1] + [("check (rest)", "completed", "failure")] + [("timings / report (timings)", "queued", None)]
        self.serve_check_run(jobs)
        self.put("run-101-job-901.log", "check (rest)\tRun the hook\t2026-11-07T15:00:00.0Z ::error::shellcheck: SC2086\n")
        rem = A.remote_poll(O_R, {"tip": "c" * 40, "branch": "check/7"})
        self.assertEqual(rem["status"], "failure")
        self.assertIn("-- check (rest)", rem["out"])
        self.assertIn("SC2086", rem["out"])

    def test_remote_poll_waits_for_a_verdict_job_still_running(self):
        self.serve_check_run(self.CHECK_JOBS + [("check (page)", "in_progress", None)])
        rem = A.remote_poll(O_R, {"tip": "c" * 40, "branch": "check/7"})
        self.assertIsNone(rem.get("status"))
        self.assertEqual(rem["verdict_jobs"], ["plan", "check (*)", "heavy rows (*)"])

    # GitHub lists a job only once it is created, and `check (page)` (it needs every other check) is created a second
    # after the last of them concludes (precommit-check run 42463101933, 2026-11-08): in that second every listed verdict
    # job had concluded, and the verdict was a success without the page checks.
    def test_remote_poll_waits_for_a_verdict_job_github_has_not_created_yet(self):
        self.serve_check_run(self.CHECK_JOBS[:-1])
        rem = A.remote_poll(O_R, {"tip": "c" * 40, "branch": "check/7"})
        self.assertIsNone(rem.get("status"))
        self.assertIn("check (page)", rem["verdict_expect"])

    def test_the_verdict_jobs_are_the_workflows_jobs_the_globs_name(self):
        self.assertEqual(A.workflow_job_names(self.WORKFLOW_JOBS),
                         ["plan", "check (rest)", "check (identity ${{ matrix.shard }}/3)", "check (page)",
                          "heavy rows (${{ matrix.rows }})", "timings"])
        expect = ["plan", "check (identity ${{ matrix.shard }}/3)", "timings"]
        done = lambda *names: [{"name": n, "status": "completed", "conclusion": "success"} for n in names]
        # a matrix job is there by any of its rows, or by its skipped template; a reusable workflow's by its inner jobs
        self.assertEqual(A.jobs_verdict(done("plan", "check (identity 2/3)", "timings / report"), ["plan"], expect)[0],
                         "success")
        self.assertEqual(A.jobs_verdict(done("plan", "check (identity ${{ matrix.shard }}/3)", "timings"), ["plan"],
                                        expect)[0], "success")
        self.assertIsNone(A.jobs_verdict(done("plan", "timings / report"), ["plan"], expect)[0])

    def test_remote_poll_without_a_declaration_waits_for_the_run(self):
        self.serve_check_run(self.CHECK_JOBS + [("timings / report (timings)", "queued", None)], declares=False)
        rem = A.remote_poll(O_R, {"tip": "c" * 40, "branch": "check/7"})
        self.assertIsNone(rem.get("status"))
        self.assertEqual(rem["verdict_jobs"], [])

    def serve_queued_check_run(self, listed):
        """Run 101 queued, its plan done and its check jobs never started; GitHub's queued listing names it or not."""
        rs = [{"id": 101, "run_number": 12, "head_sha": "c" * 40, "head_branch": "check/7", "status": "queued",
               "conclusion": None, "path": ".github/workflows/precommit-check.yml"}]
        self.serve_found(rs)
        self.gh_api(f"repos/{O_R}/actions/runs/101", rs[0])
        self.gh_api(f"repos/{O_R}/actions/runs/101/jobs?per_page=100", {"jobs": [
            {"id": 900, "name": "plan", "status": "completed", "conclusion": "success", "started_at": "2026-11-20T05:41:02Z"},
            {"id": 901, "name": "check (rest)", "status": "queued", "conclusion": None}]})
        self.gh_api(f"repos/{O_R}/actions/runs?status=queued&per_page=100&page=1", {"workflow_runs": rs if listed else []})
        self.gh_api(f"repos/{O_R}/actions/runs?status=in_progress&per_page=100&page=1", {"workflow_runs": []})

    # 2026-11-20, phase7-houses: run 44682642375 sat queued 90 min, its check jobs never started -- GitHub's queued-run
    # listing (what ci-runner's runner scheduler reads) left it out, so no runner was ever started for it
    def test_a_queued_run_github_lists_nowhere_is_given_up_after_its_bound_and_one_it_lists_is_waited_for(self):
        import time
        self.serve_queued_check_run(listed=False)
        rem = A.remote_poll(O_R, {"tip": "c" * 40, "branch": "check/7", "run": 101, "job_bounds": {}, "verdict_jobs": [], "verdict_expect": []})
        self.assertIsNone(rem.get("status"), "unlisted only now: waited for (GitHub's listing may lag)")
        self.assertTrue(rem.get("unlisted_at"))
        rem["unlisted_at"] = time.time() - A.REMOTE_GHOST - 1
        rem = A.remote_poll(O_R, rem)
        self.assertEqual((rem["status"], rem.get("ghost")), ("abandoned", True))
        self.assertIn("listing omits it", rem["out"])
        self.serve_queued_check_run(listed=True)
        rem = A.remote_poll(O_R, {"tip": "c" * 40, "branch": "check/7", "run": 101, "job_bounds": {}, "verdict_jobs": [], "verdict_expect": [],
                                  "unlisted_at": time.time() - 3600})
        self.assertIsNone(rem.get("status"), "a listed queued run waits for its runner")
        self.assertNotIn("unlisted_at", rem)
        lines = A.remote_check_lines({**rem, "pushed_at": time.time() - 600}, time.time())
        self.assertIn("run 101", lines[0])
        self.assertIn("  check (rest): waiting for a runner slot", lines)
        self.assertIn("  plan: success", lines)

    def test_run_url(self):
        url = A.forge_run_url(O_R, 12, {"databaseId": 101})
        self.assertEqual(url, f"https://github.com/{O_R}/actions/runs/101")


    def serve_branches(self, bs):
        self.gh_api(f"repos/{O_R}/branches?per_page=100&page=1", [{"name": n, "commit": {"sha": s}} for n, s, _ in bs])
        for _n, s, t in bs:
            self.gh_api(f"repos/{O_R}/commits/{s}", {"sha": s, "commit": {"committer": {"date": t}}})

    def serve_found(self, rs):
        """GitHub's filtered answer to forge_find_run (the check run found by its workflow, branch and tip)."""
        import urllib.parse
        for r in rs:
            q = urllib.parse.urlencode({"branch": r["head_branch"], "head_sha": r["head_sha"], "per_page": 10})
            self.gh_api(f"repos/{O_R}/actions/workflows/precommit-check.yml/runs?{q}", {"workflow_runs": [r]})

    def serve_runs(self, runs):
        rs = [{"id": r["id"], "run_number": r["number"], "head_sha": r["sha"], "head_branch": r["branch"],
               "status": "completed" if r["done"] else "in_progress",
               "conclusion": ("success" if r["ok"] else "failure") if r["done"] else None,
               "html_url": f"https://github.com/{O_R}/actions/runs/{r['id']}",
               "path": ".github/workflows/precommit-check.yml"} for r in runs]
        self.gh_api(f"repos/{O_R}/actions/workflows/precommit-check.yml/runs?per_page=50", {"workflow_runs": rs})
        self.serve_found(rs)
        self.gh_api(f"repos/{O_R}/actions/runs?per_page=100", {"workflow_runs": rs})
        for r in rs:
            self.gh_api(f"repos/{O_R}/actions/runs/{r['id']}", r)

    def test_gh_api_takes_no_repo_flag(self):
        self.gh_api(f"repos/{O_R}/git/commits/abc", {"message": "m"})
        self.assertEqual(A.gh_json(O_R, "api", f"repos/{O_R}/git/commits/abc"), {"message": "m"})
        self.assertNotIn("-R", self.calls()[-1].split())
        self.assertTrue(self.calls()[-1].endswith(f" repos/{O_R}/git/commits/abc"))

    def test_a_missing_branch_is_a_404(self):
        self.assertFalse(A.branch_exists(O_R, "measure/gone"))

    def test_github_status_folds_into_one_word(self):
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
        self.assertEqual(self.calls(), [])

    def test_release_pins_download_through_gh(self):
        self.gh_api(f"repos/{O_R}/releases/tags/fixtures", {"assets": [{"name": "hermetic-pl.txt"}, {"name": "x.bin"}]})
        self.put("release-fixtures-hermetic-pl.txt", "4242 recorded\n")
        self.assertEqual(A.read_pins(O_R, "fixtures"), {"pl": "4242"})
        self.assertIn(f"release download fixtures -p hermetic-pl.txt -O - -R {O_R}", self.calls())

    def test_no_token_of_agentctl_s_own_for_gh(self):
        os.environ.pop("GH_TOKEN")
        self.assertEqual(A.token_env(O_R, {}), {})
        self.assertNotIn("GH_TOKEN", A.forge_env(O_R, {}))


class NetRetryTest(unittest.TestCase):
    """net_retry: the one bounded retry every network call goes through (core.py)."""
    def test_a_transient_failure_is_tried_again_with_growing_delays(self):
        n, slept = [0], []

        def call():
            n[0] += 1
            if n[0] < 3:
                raise RuntimeError("fatal: unable to access 'https://github.com/o/r/': Connection reset by peer")
            return "ok"
        self.assertEqual(A.net_retry("a read", call, delays=(2, 5, 10), sleep=slept.append, clock=lambda: 0.0), "ok")
        self.assertEqual(slept, [2, 5])

    def test_every_try_failing_is_transient_network_never_the_last_error(self):
        def call():
            raise RuntimeError("ssh: connect to host ci-runner port 22: Operation timed out")
        with self.assertRaises(A.TransientNetwork) as cm:
            A.net_retry("a read", call, delays=(1, 1), sleep=lambda s: None, clock=lambda: 0.0)
        self.assertIn("3 tries", str(cm.exception))

    def test_a_real_error_is_raised_at_once(self):
        n = [0]

        def call():
            n[0] += 1
            raise RuntimeError("HTTP 404: Not Found")
        with self.assertRaises(RuntimeError):
            A.net_retry("a read", call, delays=(1, 1), sleep=lambda s: None, clock=lambda: 0.0)
        self.assertEqual(n[0], 1)

    def test_no_retry_starts_past_the_budget(self):
        t, slept = [0.0], []

        def call():
            t[0] += 90  # a call that hangs to its own 90-s timeout
            raise A.subprocess.TimeoutExpired("gh", 90)
        with self.assertRaises(A.TransientNetwork):
            A.net_retry("gh run list", call, delays=(2, 5, 10, 20), sleep=slept.append, clock=lambda: t[0], budget=120)
        self.assertEqual(slept, [2], "tried twice, not five times")

    def test_net_run_returns_the_last_run_and_says_a_timeout_as_124(self):
        r = A.net_run("a sleep", ["sleep", "5"], delays=(), timeout=0.2, capture_output=True, text=True)
        self.assertEqual(r.returncode, 124)
        self.assertTrue(A.is_transient_network(r.stderr), r.stderr)
        r = A.net_run("a refusal", ["sh", "-c", "echo 'rejected: not allowed' >&2; exit 1"], delays=(0, 0),
                      capture_output=True, text=True)
        self.assertEqual(r.returncode, 1)


if __name__ == "__main__":
    unittest.main()


class ConditionalGetTest(GithubForgeTest):
    """Item 4 (simplification-2026-10-31): a GET GitHub answers 304 costs no rate budget; the forge sends the ETag it
    was last given and reads an unchanged resource from its own copy."""

    def setUp(self):
        super().setUp()
        self.saved_root = os.environ.get("AGENTCTL_STATE_ROOT")
        os.environ["AGENTCTL_STATE_ROOT"] = os.path.join(self.tmp.name, "state")

    def tearDown(self):
        if self.saved_root is None:
            os.environ.pop("AGENTCTL_STATE_ROOT", None)
        else:
            os.environ["AGENTCTL_STATE_ROOT"] = self.saved_root
        super().tearDown()

    def test_an_unchanged_resource_is_read_again_with_its_etag_and_answered_from_the_copy(self):
        self.gh_api(f"repos/{O_R}/actions/runs/7", {"id": 7, "status": "completed"})
        self.assertEqual(A.api(O_R, "GET", "actions/runs/7"), {"id": 7, "status": "completed"})
        self.assertEqual(A.api(O_R, "GET", "actions/runs/7"), {"id": 7, "status": "completed"})
        second = self.calls()[-1]
        self.assertIn("If-None-Match: W/", second, "the second read is conditional")

    def test_an_unchanged_run_list_is_a_free_304(self):
        self.put("runs.json", [{"databaseId": 9, "status": "completed", "conclusion": "failure", "headBranch": "main",
                                "workflowName": "Main", "workflowFile": "main.yml"}])
        args = ("run", "list", "--workflow", "main.yml", "--branch", "main", "--limit", "30", "--json", A.RUN_FIELDS)
        first = A.gh_json(O_R, *args)
        self.assertEqual(A.gh_json(O_R, *args), first)
        self.assertIn("If-None-Match: W/", self.calls()[-1], "the second list is conditional")

    def test_every_gh_process_is_tallied_by_its_caller_and_the_tally_printed_on_schedule(self):
        # 2026-11-10: the lander installation spent ~1,500 requests an hour nobody could attribute (ci-runner hides
        # other users' processes, and no call was logged); each process now prints its own tally
        A._gh_tally_state.update(since=A.time.time(), calls={}, free=0)
        saved, A.GH_TALLY_SECONDS = A.GH_TALLY_SECONDS, 10 ** 9
        self.addCleanup(setattr, A, "GH_TALLY_SECONDS", saved)
        self.gh_api(f"repos/{O_R}/actions/runs/7", {"id": 7, "status": "completed"})

        def read_the_run():
            return A.api(O_R, "GET", "actions/runs/7")

        read_the_run()
        read_the_run()  # unchanged: a 304
        self.assertEqual(A._gh_tally_state["calls"],
                         {"movies test_every_gh_process_is_tallied_by_its_caller_and_the_tally_printed_on_schedule>"
                          "read_the_run: api GET actions/runs/N": 2})
        self.assertEqual(A._gh_tally_state["free"], 1)
        A.GH_TALLY_SECONDS = 300
        errb = io.StringIO()
        with contextlib.redirect_stderr(errb):
            A.gh_tally("gh run view", now_=A._gh_tally_state["since"] + 301)
        line = errb.getvalue()
        self.assertIn("(pid", line)
        self.assertIn("3 gh processes (movies 2, ? 1), 1 of them 304 (free): 2 movies ", line)
        self.assertIn("read_the_run: api GET actions/runs/N", line)
        self.assertEqual(A._gh_tally_state["calls"], {}, "a printed tally starts again")

    def test_a_bare_gh_api_read_is_a_conditional_get(self):
        # 2026-11-10's tally: branch_exists, the retire reads and ci-why's commit reads went through `gh api` with no
        # If-None-Match, charged every time though the answer had not changed
        self.gh_api(f"repos/{O_R}/commits/main", {"sha": "abc"})
        self.assertEqual(A.gh_json(O_R, "api", f"repos/{O_R}/commits/main"), {"sha": "abc"})
        self.assertEqual(A.gh_json(O_R, "api", f"repos/{O_R}/commits/main"), {"sha": "abc"})
        self.assertIn("If-None-Match: W/", self.calls()[-1], "the second read is conditional")

    def test_a_changed_resource_is_read_afresh(self):
        self.gh_api(f"repos/{O_R}/actions/runs/7", {"id": 7, "status": "in_progress"})
        A.api(O_R, "GET", "actions/runs/7")
        self.gh_api(f"repos/{O_R}/actions/runs/7", {"id": 7, "status": "completed"})
        self.assertEqual(A.api(O_R, "GET", "actions/runs/7")["status"], "completed")

    def test_a_missing_resource_still_fails(self):
        with self.assertRaises(A.ForgeHTTPError):
            A.api(O_R, "GET", "actions/runs/404")
