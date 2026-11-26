"""ci-runners-page (http://runners.localhost:8790): the polite GitHub client (cache, ETags, pacing, the budget), the
page's data from GitHub's runners and jobs, ci-runner's probe, the last-good shape agentctl's queue_full reads, and
the server's Host check. No network: GitHub and ci-runner are faked."""
import importlib.machinery, importlib.util, json, os, sys, tempfile, threading, unittest, urllib.error, urllib.request
from http.server import ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402  (the harness: hermetic HOME, PATH, timeout)

_loader = importlib.machinery.SourceFileLoader("ci_runners_page", os.path.join(HERE, "..", "ci-runners-page"))
_spec = importlib.util.spec_from_loader("ci_runners_page", _loader)
P = importlib.util.module_from_spec(_spec)
_loader.exec_module(P)

T = 1_800_000_000.0


def iso(t):
    import datetime
    return datetime.datetime.fromtimestamp(t, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Clock:
    def __init__(self, t=T):
        self.t, self.slept = t, []

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.slept.append(round(s, 3))
        self.t += s


class FakeGitHub:
    """`gh api -i` by path: a fixed body per path, ETag "e-<n>" that changes when the body does."""
    def __init__(self, bodies, remaining=4000):
        self.bodies, self.remaining, self.asked = dict(bodies), remaining, []

    def __call__(self, path, etag=None):
        self.asked.append((path, etag))
        if path not in self.bodies:
            return 404, {}, '{"message":"Not Found"}'
        body = json.dumps(self.bodies[path])
        tag = f'"e-{abs(hash(body)) % 10**8}"'
        hdr = {"etag": tag, "x-ratelimit-remaining": str(self.remaining), "x-ratelimit-limit": "5000",
               "x-ratelimit-reset": str(int(T) + 3600)}
        return (304, hdr, "") if etag == tag else (200, hdr, body)


class ParseTest(unittest.TestCase):
    def test_gh_api_i_output(self):
        status, h, body = P.parse_http('HTTP/2.0 304 Not Modified\r\nEtag: "x"\r\nX-Ratelimit-Remaining: 12\r\n\r\n')
        self.assertEqual((status, h["etag"], h["x-ratelimit-remaining"], body), (304, '"x"', "12", ""))
        status, _, body = P.parse_http('HTTP/2.0 200 OK\nEtag: W/"y"\n\n{"a": 1}')
        self.assertEqual((status, json.loads(body)), (200, {"a": 1}))
        with self.assertRaises(RuntimeError):
            P.parse_http("gh: not logged in")


class GitHubClientTest(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.fake = FakeGitHub({"a": {"n": 1}, "b": {"n": 2}})
        self.gh = P.GitHub(raw=self.fake, clock=self.clock, sleep=self.clock.sleep)

    def test_cached_within_its_age_then_asked_with_its_etag(self):
        self.assertEqual(self.gh.get("a"), {"n": 1})
        self.clock.t += 5
        self.assertEqual(self.gh.get("a"), {"n": 1})
        self.assertEqual(len(self.fake.asked), 1, "a fresh answer is not asked again")
        self.clock.t += P.EVERY
        self.assertEqual(self.gh.get("a"), {"n": 1})
        self.assertEqual(len(self.fake.asked), 2)
        self.assertIsNotNone(self.fake.asked[1][1], "re-asked with its ETag")
        self.assertEqual(self.gh.budget()["not_modified"], 1)
        self.fake.bodies["a"] = {"n": 9}
        self.clock.t += P.EVERY
        self.assertEqual(self.gh.get("a"), {"n": 9}, "a changed answer replaces the cached one")

    def test_at_most_one_request_a_second_and_slower_when_the_budget_runs_low(self):
        self.gh.get("a")
        self.gh.get("b")
        self.assertEqual(self.clock.slept, [P.MIN_GAP])
        self.fake.remaining = P.LOW_REMAINING - 1
        self.clock.t += P.EVERY
        self.gh.get("a")  # learns the budget is low
        self.clock.t += 0.5
        self.fake.bodies["c"] = {}
        self.gh.get("c")
        self.assertAlmostEqual(self.clock.slept[-1], P.LOW_GAP - 0.5)

    def test_a_spent_budget_asks_nothing_until_the_reset(self):
        self.gh.get("a")
        self.gh.remaining, self.gh.reset = 0, int(T) + 600
        self.clock.t += P.EVERY
        n = len(self.fake.asked)
        self.assertEqual(self.gh.get("a"), {"n": 1}, "the last answer stands")
        with self.assertRaises(RuntimeError):
            self.gh.get("b")
        self.assertEqual(len(self.fake.asked), n)

    def test_errors_and_pruning(self):
        with self.assertRaisesRegex(RuntimeError, "HTTP 404"):
            self.gh.get("missing")
        self.gh.get("a")
        self.clock.t += 3601
        self.gh.prune()
        self.assertEqual(self.gh.cache, {})


def runner(name, label, busy=False, status="online"):
    return {"name": name, "status": status, "busy": busy,
            "labels": [{"name": x} for x in ("self-hosted", "Linux", "X64", label)]}


def gh_world(now):
    run = lambda i, n, repo, st: {"id": i, "run_number": n, "name": "Tests", "status": st, "head_branch": "main",
                                  "head_sha": "abcdef1234567", "html_url": f"https://github.com/{repo}/actions/runs/{i}"}
    job = lambda i, name, st, labels, runner_="", started=now - 120: {
        "id": i, "name": name, "status": st, "labels": labels, "runner_name": runner_, "created_at": iso(now - 300),
        "started_at": iso(started), "html_url": f"https://github.com/x/y/actions/runs/1/job/{i}"}
    return {
        "orgs/bitcashier/actions/runners?per_page=100": {"runners": [
            runner("bitcashier-gh-builder-1", "builder", busy=True), runner("bitcashier-gh-builder-2", "builder"),
            runner("bitcashier-gh-deployer-1", "deployer", status="offline")]},
        "orgs/bitcashier/repos?per_page=100&type=all": [
            {"name": "core", "pushed_at": iso(now - 3600)}, {"name": "old", "pushed_at": iso(now - 60 * 86400)},
            {"name": "gone", "archived": True, "pushed_at": iso(now)}],
        "repos/bitcashier/core/actions/runs?per_page=40": {"workflow_runs": [
            run(11, 501, "bitcashier/core", "in_progress"), run(10, 500, "bitcashier/core", "completed")]},
        "repos/bitcashier/core/actions/runs/11/jobs?per_page=100": {"jobs": [
            job(1, "build", "in_progress", ["self-hosted", "builder"], "bitcashier-gh-builder-1"),
            job(2, "deploy", "queued", ["self-hosted", "deployer"]),
            job(3, "done", "completed", ["self-hosted", "builder"], "bitcashier-gh-builder-2")]},
        "repos/pawelkrupinski/movies/actions/runs?per_page=40": {"workflow_runs": [
            run(21, 900, "pawelkrupinski/movies", "in_progress")]},
        "repos/pawelkrupinski/movies/actions/runs/21/jobs?per_page=100": {"jobs": [
            job(4, "gates", "in_progress", ["ubuntu-22.04"], "GitHub Actions 7"),
            job(5, "leg", "queued", ["ubuntu-22.04"])]},
        **{f"repos/pawelkrupinski/{n}/actions/runs?per_page=40": {"workflow_runs": []}
           for n in ("agentctl", "recommend", "movies-gitops")},
        # every repo's live runs by status (an older live run is not among the 40 newest)
        **{f"repos/{o_r}/actions/runs?status={st}&per_page=100&page=1": {"workflow_runs": []}
           for o_r in ("bitcashier/core", "pawelkrupinski/movies", "pawelkrupinski/agentctl",
                       "pawelkrupinski/recommend", "pawelkrupinski/movies-gitops") for st in P.LIVE_STATUSES},
    }


HOST_OUT = "\n".join([
    "H\t0.50 0.40 0.30\t32",
    "M\tMemTotal:\t1000", "M\tMemAvailable:\t400",
    "D\t/\t2000\t500",
    "T\tcheck\t300\t\t5000000\t1073741824\tmax\t1.50",
    "R\tbitcashier-gh-builder-1\tcheck\t1000000\trunning",
    "R\tbitcashier-gh-builder-2\tcheck\t2000000\trunning",
    "T\tdeploy\t10000\t0-3\t100\t0\tmax\t0.00",
    "R\tbitcashier-gh-deployer-1\tdeploy\t100\tinactive",
    "P\tcpu 100 0 100 700 100 0 0 0",
])


class DataTest(unittest.TestCase):
    def collect(self):
        clock = Clock()
        gh = P.GitHub(raw=FakeGitHub(gh_world(T)), clock=clock, sleep=clock.sleep)
        active = set()
        runners, jobs, errors = P.collect(gh, T, active)
        return gh, runners, jobs, errors, active

    def test_collects_the_active_jobs_of_recent_repos(self):
        gh, runners, jobs, errors, active = self.collect()
        self.assertEqual(errors, [])
        self.assertEqual(sorted(j["job"] for j in jobs), ["build", "deploy", "gates", "leg"], "completed jobs drop out")
        self.assertEqual(active, {"bitcashier/core", "pawelkrupinski/movies"})
        asked = {p for p, _ in gh.raw.asked}
        self.assertNotIn("repos/bitcashier/old/actions/runs?per_page=40", asked, "a repo idle 60 days is not watched")
        self.assertNotIn("repos/bitcashier/gone/actions/runs?per_page=40", asked, "nor an archived one")
        self.assertNotIn("repos/bitcashier/core/actions/runs/10/jobs?per_page=100", asked, "nor a finished run's jobs")

    def test_a_queued_job_of_a_run_older_than_the_newest_40_is_counted(self):
        # 2026-11-16: Identity takes 43816825023 queued six drift rows for an hour while the page read 0 waiting --
        # forty newer movies runs had pushed it off `runs?per_page=40`
        world = gh_world(T)
        older = {"id": 7, "run_number": 300, "name": "Identity takes", "status": "queued", "head_branch": "measure/x",
                 "head_sha": "1234567abcdef", "html_url": "https://github.com/pawelkrupinski/movies/actions/runs/7"}
        page = [dict(older, id=1000 + n) for n in range(100)]  # a full first page: the listing goes on to page 2
        world["repos/pawelkrupinski/movies/actions/runs?status=queued&per_page=100&page=1"] = {"workflow_runs": page}
        world["repos/pawelkrupinski/movies/actions/runs?status=queued&per_page=100&page=2"] = {"workflow_runs": [older]}
        for n in range(100):
            world[f"repos/pawelkrupinski/movies/actions/runs/{1000 + n}/jobs?per_page=100"] = {"jobs": []}
        world["repos/pawelkrupinski/movies/actions/runs/7/jobs?per_page=100"] = {"jobs": [
            {"id": 70, "name": "leg (germany) / takes-drift-1 (germany)", "status": "queued", "labels": ["movies-measure"],
             "runner_name": "", "created_at": iso(T - 3600), "html_url": "https://github.com/x/y/actions/runs/7/job/70"}]}
        # a live run among the newest 40 that the status listing also names is counted once
        world["repos/pawelkrupinski/movies/actions/runs?status=in_progress&per_page=100&page=1"] = {"workflow_runs": [
            {"id": 21, "run_number": 900, "name": "Tests", "status": "in_progress", "head_branch": "main",
             "head_sha": "abcdef1234567", "html_url": "https://github.com/pawelkrupinski/movies/actions/runs/21"}]}
        clock = Clock()
        gh = P.GitHub(raw=FakeGitHub(world), clock=clock, sleep=clock.sleep)
        _, jobs, errors = P.collect(gh, T, set())
        self.assertEqual(errors, [])
        movies = sorted(j["job"] for j in jobs if j["repo"] == "pawelkrupinski/movies")
        self.assertEqual(movies, ["gates", "leg", "leg (germany) / takes-drift-1 (germany)"])
        [drift] = [j for j in jobs if j["run_id"] == 7]
        self.assertEqual((drift["status"], drift["secs"]), ("queued", 3600))

    def test_one_failing_repo_is_its_own_line(self):
        clock = Clock()
        world = gh_world(T)
        del world["repos/pawelkrupinski/movies/actions/runs?per_page=40"]
        gh = P.GitHub(raw=FakeGitHub(world), clock=clock, sleep=clock.sleep)
        _, jobs, errors = P.collect(gh, T, set())
        self.assertEqual(len(errors), 1)
        self.assertIn("pawelkrupinski/movies", errors[0])
        self.assertEqual(sorted(j["job"] for j in jobs), ["build", "deploy"])

    def test_assembled_page_data(self):
        _, runners, jobs, _, _ = self.collect()
        host = P.parse_host(HOST_OUT, T)
        d = P.assemble(runners, jobs, host)
        by = {r["name"]: r for r in d["runners"]}
        self.assertEqual(by["bitcashier-gh-builder-1"]["tier"], "check")
        self.assertEqual(by["bitcashier-gh-deployer-1"]["unit"], "inactive")
        self.assertEqual([(j["runner"], j["job"], j["run"], j["secs"]) for j in d["running"]],
                         [("bitcashier-gh-builder-1", "build", 501, 120)])
        self.assertEqual([(w["label"], w["jobs"], w["oldest"]) for w in d["waiting"]],
                         [('["self-hosted", "deployer"]', 1, 300)])
        self.assertEqual([j["job"] for j in d["hosted"]["running"]], ["gates"])
        self.assertEqual([j["job"] for j in d["hosted"]["queued"]], ["leg"])
        self.assertEqual(d["hosted"]["used"], {"pawelkrupinski": 1})
        self.assertEqual(d["limits"]["host"]["mem"], {"MemTotal": 1024000, "MemAvailable": 409600})
        self.assertEqual(d["limits"]["tiers"]["check"]["max"], "max")
        self.assertTrue(d["running"][0]["url"].startswith("https://github.com/"))

    def test_queue_full_reads_the_last_good_shape(self):
        """agentctl's red.queue_full: a run waiting on deployer is not 'queued' while a deployer is offline, and is
        once every deployer is up and busy."""
        _, runners, jobs, _, _ = self.collect()
        d = P.assemble(runners, jobs)
        self.assertIsNone(A.queue_full("bitcashier/core", 501, d), "the only deployer is offline: stuck, not queued")
        for r in runners:
            if r["name"] == "bitcashier-gh-deployer-1":
                r["status"], r["busy"] = "online", True
        jobs.append({**jobs[0], "runner": "bitcashier-gh-deployer-1", "job": "other", "run": 77})
        d = P.assemble(runners, jobs)
        self.assertRegex(A.queue_full("bitcashier/core", 501, d) or "", r"queued: deployer, self-hosted full \(1/1 busy")

    def test_host_rates_and_peaks(self):
        rates = P.Rates()
        a = P.parse_host(HOST_OUT, T)
        self.assertIsNone(rates.update(a, {}))
        b = P.parse_host(HOST_OUT.replace("\t1000000\trunning", "\t3000000\trunning")
                         .replace("cpu 100 0 100 700 100", "cpu 600 0 100 1200 100"), T + 10)
        cpu = rates.update(b, {"bitcashier-gh-builder-1": 1})
        self.assertEqual(cpu["runners"]["bitcashier-gh-builder-1"], 20)  # 2 s of CPU in 10 s = 0.2 core
        self.assertEqual(cpu["peaks"], {"bitcashier-gh-builder-1": 20})
        self.assertEqual(cpu["host"], 1600)  # 500 busy of 1000 jiffies on 32 threads
        self.assertEqual(len(rates.history), 1)
        self.assertEqual(P.cpu_count("0-3,8", 32), 5)
        self.assertEqual(P.cpu_count("", 32), 32)


class ServerTest(unittest.TestCase):
    def setUp(self):
        P.beat()  # /healthz judges the poller by its last beat: the module's import stamp is minutes old in a loaded suite run
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), P.Handler)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.port = self.srv.server_address[1]

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()

    def get(self, path, host=None, method="GET"):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", method=method,
                                     headers={"Host": host or f"runners.localhost:{self.port}"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()

    def test_routes_and_host_check(self):
        status, body = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn("<title>CI runners</title>", body)
        status, body = self.get("/data.json", host="localhost")
        self.assertEqual(status, 200)
        self.assertIn("runners", json.loads(body))
        self.assertEqual(self.get("/healthz")[0], 200)
        self.assertEqual(self.get("/", host="evil.example:8790")[0], 403)
        self.assertEqual(self.get("/", host="runners.localhost.evil.example")[0], 403)
        self.assertEqual(self.get("/data.json", method="POST")[0], 405)
        self.assertEqual(self.get("/nope")[0], 404)

    def test_last_good_round_trip(self):
        with tempfile.TemporaryDirectory() as d:
            saved, P.SAVED = P.SAVED, P.Path(d) / "last-good.json"
            try:
                with P.LOCK:
                    P.STATE.update(at="12:00:00", ok=T, running=[{"runner": "r", "repo": "o/r", "run": 1}])
                P.save_good()
                self.assertEqual(json.loads(P.SAVED.read_text())["running"][0]["runner"], "r")
                P.load_good(P.Rates())
                self.assertIn("restart", P.snapshot()["error"])
            finally:
                P.SAVED = saved


if __name__ == "__main__":
    unittest.main()
