"""THE END-TO-END HARNESS (the owner, 2026-11-27: "no test runs the real pieces together"; the store hang, the
`record_events` 404 version skew and the "bad object" dead tick were all caught live).

ONE temp root per test holds every piece, each the real one:
- a REAL `agentctl store serve` subprocess on a free local port, with its own state root, secret and token;
- a REAL bare git remote, the laptop's clone and an agent's worktree of a toy repo (its own `scripts/hooks/pre-push`);
- a STUB GitHub API (tests/e2e/ghstub.py) that agentctl's GitHub client -- the gh CLI, `$AGENTCTL_GH_CLI` -- reaches
  through tests/e2e/gh_shim.py. Never the real GitHub;
- the REAL reconciler tick (`agentctl tick --once`, the server role, ci-runner's) and the laptop's sync (`tick --once`,
  the sync role), each a subprocess, driven step by step: `until(...)` polls to a deadline, no sleeps;
- the laptop's CLI (`land`, `status`, `history`, `inbox`) as subprocesses against that store.
HOME, every state dir and the clones are inside the root (tests/harness.py's hermetic environment first), so the live
~/.cache/agentctl, ci-runner and GitHub are never touched. Every process this harness starts is stopped by its pid."""
import json, os, shutil, signal, socket, subprocess, sys, tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__))
TESTS = os.path.dirname(HERE)
BIN = os.path.dirname(TESTS)
sys.path.insert(0, TESTS)
sys.path.insert(0, HERE)
import harness  # noqa: E402,F401  -- the hermetic HOME, PATH and git config of this process, before anything runs
import ghstub  # noqa: E402

TOOL = os.path.join(BIN, "agentctl")
USER = "e2e"
STEP_BOUND = float(os.environ.get("AGENTCTL_E2E_STEP_BOUND") or 45)  # s one subprocess step may take before it fails
GATE_FILES = (("main.yml", "Main"), ("identity-model-convergence.yml", "Identity model convergence"),
              ("identity-takes.yml", "Identity takes"))
TOY_HOOK = """#!/bin/sh
# the toy repo's pre-push hook: the checks agentctl's executor runs (`--range`, `--checks-only`, ...). A tree holding a
# file RED fails `the toy check` (what main's red gate fails), one holding BROKEN fails `the broken check` (a change's
# own failure), each named as scalatest names a failed test; every other tree passes. As git's hook it checks nothing
# for a refs/heads/land/ or refs/heads/check/ ref (movies' hook: a queued change's refs).
[ $# -eq 0 ] && { cat > /dev/null; exit 0; }
case "$1" in --plan|--compile-plan|--lint-plan|--unit-plan|--per-file-lints|--script-test-plan) cat > /dev/null; exit 0 ;; esac
rc=0
if git ls-files | grep -qE "(^|/)RED$"; then echo "[info] - the toy check *** FAILED ***"; rc=1; fi
if git ls-files | grep -qE "(^|/)BROKEN$"; then echo "[info] - the broken check *** FAILED ***"; rc=1; fi
[ $rc -eq 0 ] || echo "pre-push sbt FAILED"
exit $rc
"""


def free_port():
    """A free local port (bound, read, released)."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def sh(cwd, *argv, env=None, check=True):
    r = subprocess.run(list(argv), cwd=cwd, capture_output=True, text=True, env=env, timeout=60)
    if check and r.returncode != 0:
        raise RuntimeError(f"{' '.join(argv)} in {cwd}: {r.returncode}\n{r.stdout}\n{r.stderr}")
    return r.stdout.strip()


class Proc:
    """A process the harness started: stopped by its pid (its own process group), never by a pattern."""

    def __init__(self, argv, env, log, cwd=None):
        self.log = log
        self.fh = open(log, "ab")
        self.p = subprocess.Popen(argv, env=env, cwd=cwd, stdout=self.fh, stderr=subprocess.STDOUT,
                                  start_new_session=True)

    def alive(self):
        return self.p.poll() is None

    def stop(self, sig=signal.SIGTERM):
        if self.alive():
            try:
                os.killpg(self.p.pid, sig)
            except ProcessLookupError:
                pass
            try:
                self.p.wait(10)
            except subprocess.TimeoutExpired:
                os.killpg(self.p.pid, signal.SIGKILL)
                self.p.wait(10)
        self.fh.close()

    def text(self):
        with open(self.log, errors="replace") as f:
            return f.read()


class World:
    """One end-to-end world in one temp root (see the module's doc)."""

    def __init__(self, tool=TOOL, server_tool=None):
        self.root = os.path.realpath(tempfile.mkdtemp(prefix="agentctl-e2e-"))
        self.tool, self.server_tool = tool, server_tool or TOOL
        self.procs, self.logs, self.gh_srv = [], os.path.join(self.root, "logs"), None
        self.extra_env = {}  # a scenario's own variables, in every process's environment (the stub claude's, ...)
        os.makedirs(self.logs)
        try:
            self._build()
        except BaseException:
            self.close()  # a world that could not start leaves no process or server behind
            raise

    def _build(self):
        self.home = os.path.join(self.root, "home")
        os.makedirs(self.home)
        self.gh = ghstub.GitHub()  # its repository's files are read from the bare remote (set below)
        for f, n in GATE_FILES:
            self.gh.add_workflow(f, n)
        self.gh_srv, self.gh_url = ghstub.serve(self.gh)
        self._toy_repo()
        self.gh.bare = self.bare
        self.port = free_port()
        self.store_url = f"http://127.0.0.1:{self.port}"
        self.secret = os.path.join(self.root, "secret", "store-secret")
        self.token = self.run_server_cli("store", "token", "issue", "--user", USER, "--name", "e2e").stdout.strip()
        self.store = None
        self.start_store()

    # --- the toy repository: a bare remote, the laptop's clone, an agent's worktrees
    def _toy_repo(self):
        # named as GitHub names it, so the repository's slug (owner/name, read off the remote) is movies' own
        self.bare = os.path.join(self.root, "github", "pawelkrupinski", "movies.git")
        os.makedirs(os.path.dirname(self.bare))
        self.clone = os.path.join(self.root, "movies")  # the laptop's movies checkout
        sh(self.root, "git", "init", "-q", "--bare", "-b", "main", self.bare)
        sh(self.root, "git", "init", "-q", "-b", "main", self.clone)
        sh(self.clone, "git", "remote", "add", "origin", self.bare)
        self.write("README.md", "toy\n")
        self.write("scripts/hooks/pre-push", TOY_HOOK)
        os.chmod(os.path.join(self.clone, "scripts/hooks/pre-push"), 0o755)
        self.write(".github/red-workflows.tsv", "# workflow\tverify_dispatch\treplay\twhy\n")
        self.write(".github/gating-checks.tsv", "kind\tworkflow\tname\tproven\tnote\n" + "".join(
            f"workflow\t{f}\t-\t1\tTHE GATE\n" for f, _n in GATE_FILES) + "job\tmain.yml\ttest\t1\t\n")
        for f, n in GATE_FILES:
            self.write(f".github/workflows/{f}", f"name: {n}\non: push\njobs:\n  test:\n    runs-on: x\n")
        self.write("web/src/main/App.scala", "object App\n")
        self.write("scripts/tool.sh", "echo tool\n")
        self.commit("init")
        sh(self.clone, "git", "push", "-q", "origin", "HEAD:main")
        sh(self.clone, "git", "fetch", "-q", "origin")

    def write(self, rel, text, at=None):
        p = os.path.join(at or self.clone, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write(text)

    def commit(self, msg, at=None):
        sh(at or self.clone, "git", "add", "-A")
        sh(at or self.clone, "git", "commit", "-q", "-m", msg)
        return sh(at or self.clone, "git", "rev-parse", "HEAD")

    def main_sha(self):
        return sh(self.root, "git", "--git-dir", self.bare, "rev-parse", "main")

    def main_log(self):
        return sh(self.root, "git", "--git-dir", self.bare, "log", "--format=%s", "main").splitlines()

    def push_main(self, msg, rel, text):
        """Someone else's commit lands on main directly (main moves)."""
        sh(self.clone, "git", "checkout", "-q", "main")
        sh(self.clone, "git", "pull", "-q", "--ff-only", "origin", "main")
        self.write(rel, text)
        sha = self.commit(msg)
        sh(self.clone, "git", "push", "-q", "origin", "HEAD:main")
        return sha

    def agent(self, name, files, msg):
        """An agent's worktree off origin/main with one commit of `files` {path: text}: (worktree, sha)."""
        wt = os.path.join(self.root, f"wt-{name}")
        sh(self.clone, "git", "fetch", "-q", "origin")
        sh(self.clone, "git", "worktree", "add", "-q", "-b", name, wt, "origin/main")
        for rel, text in files.items():
            self.write(rel, text, at=wt)
        return wt, self.commit(msg, at=wt)

    # --- the environments
    def base_env(self):
        env = dict(os.environ)
        for k in list(env):
            if k.startswith("AGENTCTL_") and k not in ("AGENTCTL_MACHINE", "AGENTCTL_LAPTOP_PRESSURE"):
                del env[k]
        env.update(HOME=self.home, AGENTCTL_GH_CLI=os.path.join(HERE, "gh_shim.py"), E2E_GH_URL=self.gh_url,
                   GH_TOKEN="e2e-not-a-token", AGENTCTL_STORE_SECRET=self.secret, AGENTCTL_NOTIFY="true",
                   AGENTCTL_SOCK_DIR=os.path.join(self.root, "sock"), AGENTCTL_GITHUBSTATUS_URL=self.gh_url + "/status",
                   AGENTCTL_STATS_URL="", AGENTCTL_HOSTNAME="e2e-laptop", AGENTCTL_MACHINE_ID="e2e-machine",
                   AGENTCTL_TEST_HOME=self.home, PYTHONDONTWRITEBYTECODE="1")
        env.update(self.extra_env)
        return env

    def server_env(self):
        s = os.path.join(self.root, "server")
        return dict(self.base_env(), AGENTCTL_ROLE="server", AGENTCTL_HOST="ci-runner", AGENTCTL_STORE_TOKEN=getattr(self, "token", ""),
                    AGENTCTL_STATE_ROOT=os.path.join(s, "state"), AGENTCTL_MOVIES_REPO=os.path.join(s, "movies"),
                    AGENTCTL_MOVIES_REMOTE=self.bare, AGENTCTL_OWNERS_DIR=os.path.join(s, "owners"),
                    AGENTCTL_WAITING_DIR=os.path.join(s, "waiting"), AGENTCTL_INBOX_DIR=os.path.join(s, "inbox"),
                    AGENTCTL_REDS_DIR=os.path.join(s, "reds"), AGENTCTL_RECOVERY_DIR=os.path.join(s, "recovery"),
                    AGENTCTL_CHECKS_DIR=os.path.join(s, "checks"), AGENTCTL_SCRATCH=os.path.join(s, "scratch"))

    def laptop_env(self, url=None):
        s = os.path.join(self.root, "laptop")
        return dict(self.base_env(), AGENTCTL_STATE_ROOT=os.path.join(s, "state"), AGENTCTL_MOVIES_REPO=self.clone,
                    AGENTCTL_STORE_URL=url or self.store_url, AGENTCTL_STORE_TOKEN=getattr(self, "token", ""),
                    AGENTCTL_OWNERS_DIR=os.path.join(s, "owners"), AGENTCTL_WAITING_DIR=os.path.join(s, "waiting"),
                    AGENTCTL_INBOX_DIR=os.path.join(s, "inbox"), AGENTCTL_REDS_DIR=os.path.join(s, "reds"),
                    AGENTCTL_RECOVERY_DIR=os.path.join(s, "recovery"), AGENTCTL_CHECKS_DIR=os.path.join(s, "checks"),
                    AGENTCTL_SCRATCH=os.path.join(s, "scratch"))

    # --- the pieces
    def start_store(self):
        self.store = self.spawn("store", [self.server_tool, "store", "serve", "--listen", f"127.0.0.1:{self.port}",
                                          "--allow-user", USER], self.server_env())
        self.until(self.store_answers, "the store answers")

    def stop_store(self):
        self.store.stop()
        self.procs.remove(self.store)

    def store_answers(self):
        try:
            with socket.create_connection(("127.0.0.1", self.port), timeout=1):
                return True
        except OSError:
            if not self.store.alive():
                raise AssertionError(f"store serve died:\n{self.store.text()[-3000:]}")
            return False

    def spawn(self, name, argv, env, cwd=None):
        p = Proc(argv, env, os.path.join(self.logs, f"{name}-{len(self.procs)}.log"), cwd=cwd or self.root)
        self.procs.append(p)
        return p

    def run(self, argv, env, cwd=None, bound=STEP_BOUND, name="cli"):
        """One subprocess step to its end, within `bound` s (else its process group is ended and the step FAILS)."""
        p = subprocess.Popen(argv, env=env, cwd=cwd or self.root, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True, start_new_session=True)
        t0 = time.monotonic()
        try:
            out, errs = p.communicate(timeout=bound)
        except subprocess.TimeoutExpired:
            os.killpg(p.pid, signal.SIGKILL)
            out, errs = p.communicate()
            raise AssertionError(f"{name} {' '.join(argv[1:])} still running after {bound} s\n{out[-2000:]}\n{errs[-4000:]}")
        r = subprocess.CompletedProcess(argv, p.returncode, out, errs)
        r.seconds = time.monotonic() - t0
        with open(os.path.join(self.logs, "steps.log"), "a") as f:
            f.write(f"=== {name} {' '.join(argv[1:])} rc={p.returncode} {r.seconds:.1f}s\n{out}\n{errs}\n")
        return r

    def run_server_cli(self, *args, bound=STEP_BOUND):
        return self.run([self.server_tool, *args], self.server_env(), bound=bound, name="server")

    def cli(self, *args, cwd=None, url=None, bound=STEP_BOUND, tool=None):
        """The laptop's `agentctl <args>` (the sync role) against the store."""
        return self.run([tool or self.tool, *args], self.laptop_env(url), cwd=cwd, bound=bound, name="laptop")

    def tick(self, bound=STEP_BOUND):
        """ONE reconciler tick on the server (`tick --once`: it ticks until every open change is decided)."""
        return self.run([self.server_tool, "tick", "--once"], dict(self.server_env(), AGENTCTL_TICK="0.2"),
                        bound=bound, name="tick")

    def start_reconciler(self):
        """The reconciler as ci-runner runs it: `tick --once` ticks until every open change is decided -- a change
        waiting behind a red keeps it ticking, so a scenario reads the store while it runs (`until`) and stops it."""
        self.reconciler = self.spawn("reconciler", [self.server_tool, "tick", "--once"],
                                     dict(self.server_env(), AGENTCTL_TICK="0.2"))
        return self.reconciler

    def stop_reconciler(self):
        self.reconciler.stop()
        self.procs.remove(self.reconciler)

    def states(self):
        return {k: v["state"] for k, v in self.changes().items()}

    def until_state(self, eid, *states, deadline=STEP_BOUND):
        """Poll the store until change eid is in one of `states` (the reconciler running meanwhile): its row."""
        return self.until(lambda: self.changes().get(eid) if self.states().get(eid) in states else None,
                          f"change {eid} in {'/'.join(states)} (now {self.states().get(eid)})", deadline)

    def sync(self, url=None, bound=STEP_BOUND, tool=None):
        """ONE pass of the laptop's sync (`tick --once` in the sync role, from the laptop's clone)."""
        return self.run([tool or self.tool, "tick", "--once", "--repo", self.clone], self.laptop_env(url),
                        bound=bound, name="sync")

    def until(self, cond, what, deadline=STEP_BOUND, step=None):
        """Poll cond() (running `step` between looks, when given) until it is truthy or the deadline passes."""
        end = time.monotonic() + deadline
        while True:
            got = cond()
            if got:
                return got
            if time.monotonic() > end:
                raise AssertionError(f"timed out waiting for {what} ({deadline} s)\n{self.diagnosis()}")
            if step:
                step()
            else:
                time.sleep(0.05)  # a poll's own interval, never a wait for a result

    # --- what the store holds (read from the server's database, as ci-runner reads it)
    def db(self, sql, *args):
        import sqlite3
        c = sqlite3.connect(os.path.join(self.root, "server", "state", "store.db"), timeout=30)
        try:
            c.row_factory = sqlite3.Row
            return [dict(r) for r in c.execute(sql, args)]
        finally:
            c.close()

    def backdate(self, typ, iid, seconds, data_fields=()):
        """Item (typ, iid) as if it entered its state `seconds` earlier: its deadline and each of `data_fields` (epoch
        times in its data, `a.b` one nested in a) moved back -- the wall clock the real processes read is not the
        scenario's to turn."""
        import sqlite3
        c = sqlite3.connect(os.path.join(self.root, "server", "state", "store.db"), timeout=30)
        try:
            row = c.execute("SELECT data FROM items WHERE type=? AND id=?", (typ, iid)).fetchone()
            data = json.loads(row[0] or "{}")
            for path in data_fields:
                *outer, k = path.split(".")
                d = data
                for o in outer:
                    d = d.get(o) or {}
                if d.get(k) is not None:
                    d[k] = float(d[k]) - seconds
            c.execute("UPDATE items SET deadline=deadline-?, entered_at=entered_at-?, data=? WHERE type=? AND id=?",
                      (seconds, seconds, json.dumps(data), typ, iid))
            c.commit()
        finally:
            c.close()

    def changes(self):
        rows = self.db("SELECT id, state, data FROM items WHERE type='change'")
        return {r["id"]: dict(r, data=json.loads(r["data"] or "{}")) for r in rows}

    def events(self, item=None):
        rows = self.db("SELECT * FROM events" + (" WHERE item_id=?" if item else "") + " ORDER BY seq",
                       *([item] if item else []))
        for r in rows:
            for k in ("data",):
                if isinstance(r.get(k), str):
                    try:
                        r[k] = json.loads(r[k])
                    except ValueError:
                        pass
        return rows

    def inbox(self):
        d = os.path.join(self.root, "laptop", "inbox")
        out = []
        for root, _d, files in os.walk(d):
            for f in sorted(files):
                if f.endswith(".json"):
                    with open(os.path.join(root, f)) as fh:
                        try:
                            out.append(json.load(fh))
                        except ValueError:
                            pass
        return out

    def diagnosis(self):
        tail = []
        for p in self.procs:
            tail.append(f"--- {os.path.basename(p.log)} ({'alive' if p.alive() else p.p.returncode})\n{p.text()[-1500:]}")
        steps = os.path.join(self.logs, "steps.log")
        if os.path.exists(steps):
            with open(steps, errors="replace") as f:
                tail.append("--- steps.log\n" + f.read()[-5000:])
        return "\n".join(tail)

    def close(self):
        for p in reversed(self.procs):
            p.stop()
        self.procs.clear()
        if self.gh_srv:
            self.gh_srv.shutdown()
            self.gh_srv.server_close()
        if os.environ.get("AGENTCTL_E2E_KEEP"):
            print(f"e2e: kept {self.root}", file=sys.stderr)
        else:
            shutil.rmtree(self.root, ignore_errors=True)
