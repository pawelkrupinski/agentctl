"""THE STUB GITHUB API of the e2e layer: a tiny local HTTP server answering the REST reads agentctl makes (through
tests/e2e/gh_shim.py, its `gh`) from a SCRIPT the scenario writes -- the runs, their jobs and failed logs, the
workflows -- and recording every request. Never the real GitHub.

A run is GitHub's REST shape (id, run_number, name, path, head_sha, head_branch, status, conclusion, event, ...);
`GitHub.add_run(...)` fills the rest. A request the stub has no answer for is a 404, kept in `unanswered`."""
import json, re, threading, urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

GH_TO_REST = {"databaseId": "id", "number": "run_number", "status": "status", "conclusion": "conclusion",
              "headSha": "head_sha", "url": "html_url", "displayTitle": "display_title", "workflowName": "name",
              "createdAt": "created_at", "updatedAt": "updated_at", "startedAt": "run_started_at", "event": "event",
              "headBranch": "head_branch", "attempt": "run_attempt", "workflowDatabaseId": "workflow_id"}


class GitHub:
    """The scenario's script: what GitHub holds. Scenarios change it between steps (`add_run`, `jobs`, `logs`)."""

    def __init__(self, repo="pawelkrupinski/movies"):
        self.repo, self.bare = repo, None  # bare: the remote whose files `contents/` serves
        self.runs, self.jobs, self.logs = [], {}, {}
        self.workflows = {}  # file -> name
        self.calls, self.unanswered = [], []
        self.lock = threading.Lock()
        self._next = 1000

    def add_workflow(self, file, name):
        self.workflows[file] = name

    def add_run(self, file, head_sha, conclusion="success", status="completed", branch="main", jobs=(), log=""):
        """A run of workflow `file` on `branch` at `head_sha`; `jobs` [(name, conclusion)]; its failed log."""
        with self.lock:
            self._next += 1
            rid = self._next
            n = sum(1 for r in self.runs if r["path"].endswith("/" + file)) + 1
            run = {"id": rid, "run_number": n, "name": self.workflows.get(file, file), "display_title": "e2e",
                   "path": f".github/workflows/{file}", "head_sha": head_sha, "head_branch": branch,
                   "status": status, "conclusion": conclusion if status == "completed" else None, "event": "push",
                   "run_attempt": 1, "workflow_id": file, "html_url": f"https://github.invalid/{self.repo}/actions/runs/{rid}",
                   "created_at": "2026-11-27T10:00:00Z", "updated_at": "2026-11-27T10:20:00Z",
                   "run_started_at": "2026-11-27T10:00:00Z"}
            self.runs.insert(0, run)
            self.jobs[rid] = [{"id": rid * 10 + i, "name": j, "status": "completed", "conclusion": c,
                               "started_at": "2026-11-27T10:00:00Z", "completed_at": "2026-11-27T10:10:00Z", "steps": []}
                              for i, (j, c) in enumerate(jobs)]
            self.logs[rid] = log
            return run

    # --- the REST side
    def rest(self, method, path, query, body):
        """(status, json) for one REST request under repos/<repo>/."""
        prefix = f"repos/{self.repo}/"
        if not path.startswith(prefix):
            return self.other_repo(path)
        p = path[len(prefix):]
        m = re.fullmatch(r"contents/(.+)", p)
        if m and method == "GET":
            return self.contents(urllib.parse.unquote(m.group(1)), (query.get("ref") or ["main"])[0])
        if method != "GET":
            return 204, None  # a dispatch, a cancel: recorded only
        m = re.fullmatch(r"actions/workflows/([^/]+)/runs", p)
        if p == "actions/runs" or m:
            runs = [r for r in self.runs if not m or r["path"].endswith("/" + urllib.parse.unquote(m.group(1)))]
            if m and urllib.parse.unquote(m.group(1)) not in self.workflows:
                return 404, {"message": "Not Found"}
            return 200, self._page(runs, query)
        m = re.fullmatch(r"actions/runs/(\d+)", p)
        if m:
            r = self._run(int(m.group(1)))
            return (200, r) if r else (404, {"message": "Not Found"})
        m = re.fullmatch(r"actions/runs/(\d+)(?:/attempts/\d+)?/jobs", p)
        if m:
            js = self.jobs.get(int(m.group(1)), [])
            return 200, {"total_count": len(js), "jobs": js}
        if p == "actions/workflows":
            flows = [{"id": i + 1, "name": n, "path": f".github/workflows/{f}", "state": "active"}
                     for i, (f, n) in enumerate(sorted(self.workflows.items()))]
            return 200, {"total_count": len(flows), "workflows": flows}
        return None

    def other_repo(self, path):
        """Another repository's reads (agentctl's own tests.yml, ...): GitHub holds no runs of it here."""
        if re.fullmatch(r"repos/[^/]+/[^/]+/actions/(runs|workflows/[^/]+/runs)", path):
            return 200, {"total_count": 0, "workflow_runs": []}
        return None

    def contents(self, rel, ref):
        """GET contents/<path>?ref=: the file at ref in the bare remote, base64 as GitHub sends it."""
        import base64, subprocess
        r = subprocess.run(["git", "--git-dir", self.bare, "show", f"{ref}:{rel}"], capture_output=True)
        if r.returncode != 0:
            return 404, {"message": "Not Found"}
        return 200, {"type": "file", "path": rel, "encoding": "base64", "content": base64.b64encode(r.stdout).decode()}

    def _run(self, rid):
        return next((r for r in self.runs if r["id"] == rid), None)

    def _page(self, runs, q):
        def one(k):
            return (q.get(k) or [None])[0]
        if one("branch"):
            runs = [r for r in runs if r["head_branch"] == one("branch")]
        if one("head_sha"):
            runs = [r for r in runs if r["head_sha"] == one("head_sha")]
        st = one("status")
        if st:
            runs = [r for r in runs if st in (r["status"], r["conclusion"])]
        per, page = int(one("per_page") or 30), int(one("page") or 1)
        return {"total_count": len(runs), "workflow_runs": runs[(page - 1) * per:page * per]}

    # --- the CLI side (gh run view / list / ...)
    def cli(self, argv):
        if argv[:2] == ["run", "view"] and len(argv) > 2:
            r = self._run(int(argv[2]))
            if r is None:
                return 1, "", f"could not find any workflow run with ID {argv[2]}\n"
            if "--log-failed" in argv or "--log" in argv:
                return 0, self.logs.get(r["id"], ""), ""
            if "--json" in argv:
                fields = argv[argv.index("--json") + 1].split(",")
                out = {f: r.get(GH_TO_REST.get(f, f)) for f in fields}
                if "jobs" in fields:
                    out["jobs"] = [{"name": j["name"], "status": j["status"], "conclusion": j["conclusion"],
                                    "databaseId": j["id"]} for j in self.jobs.get(r["id"], [])]
                if "workflowName" in fields:
                    out["workflowName"] = r["name"]
                return 0, json.dumps(out), ""
        if argv[:2] == ["run", "list"]:
            fields = argv[argv.index("--json") + 1].split(",") if "--json" in argv else []
            runs = self.runs
            if "--branch" in argv:
                runs = [r for r in runs if r["head_branch"] == argv[argv.index("--branch") + 1]]
            if "--workflow" in argv:
                w = argv[argv.index("--workflow") + 1]
                runs = [r for r in runs if r["path"].endswith("/" + w) or r["name"] == w]
            return 0, json.dumps([{f: r.get(GH_TO_REST.get(f, f)) for f in fields} for r in runs]), ""
        return 0, "", ""  # a cancel, a rerun, a dispatch: recorded only


class StubHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _answer(self, status, obj, headers=()):
        data = b"" if obj is None else json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _handle(self):
        gh = self.server.gh
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else b""
        u = urllib.parse.urlsplit(self.path)
        path, query = u.path.lstrip("/"), urllib.parse.parse_qs(u.query)
        if path == "cli":
            argv = json.loads(body)["argv"]
            with gh.lock:
                gh.calls.append(("cli", " ".join(argv)))
                rc, out, errs = gh.cli(argv)
            return self._answer(200, {"rc": rc, "stdout": out, "stderr": errs})
        with gh.lock:
            gh.calls.append((self.command, self.path))
            got = gh.rest(self.command, path, query, body)
            if got is None:
                gh.unanswered.append((self.command, self.path))
        status, obj = got or (404, {"message": "Not Found"})
        self._answer(status, obj)

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _handle


def serve(gh):
    """The stub on a free local port, served from a daemon thread: (server, url)."""
    srv = ThreadingHTTPServer(("127.0.0.1", 0), StubHandler)
    srv.daemon_threads = True
    srv.gh = gh
    threading.Thread(target=srv.serve_forever, name="e2e-ghstub", daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_port}"
