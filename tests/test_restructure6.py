"""RESTRUCTURE ROUND 6: each function split into named stages keeps its behaviour. The digests (fixtures/
restructure6-digests.json, recorded from the code BEFORE the split with `R4_RECORD=1`) cover thousands of seeded cases
each, collaborators faked (golden_cases.py): every call, its order and the result."""
import os, sys, types, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402
from golden_cases import Probe, check as _check  # noqa: E402

DIGESTS = os.path.join(HERE, "fixtures", "restructure6-digests.json")


def check(test, name, build, fn_of, n=3000):
    _check(test, A, name, build, fn_of, n, DIGESTS)


def maybe(rng, value, p=0.5):
    return value if rng.random() < p else None


def clock(case, now=1_795_000_000.0):
    """time.time / time.monotonic of the case: monotonic climbs by a random step per read."""
    ticks = [100.0]

    def monotonic():
        ticks[0] += case.rng.choice([0, 0, 1, 20, 40])
        return ticks[0]
    case.put("time", types.SimpleNamespace(time=lambda: now, monotonic=monotonic, sleep=lambda s: None))


def proc(rng, code=0, out="", err=""):
    return types.SimpleNamespace(returncode=code, stdout=out, stderr=err)


class CleanupTest(unittest.TestCase):
    def test_plan_worktrees(self):
        def build(case):
            r = case.rng
            clock(case)
            case.put("CACHE_TREES", "/h/.cache/")
            paths = [f"/w/t{i}" for i in range(5)] + ["/h/.cache/c1", "/w/gone"]
            wts = [{"worktree": "/root", "HEAD": "r"}]
            for i in range(r.randint(0, 5)):
                w = {"worktree": r.choice(paths), "HEAD": f"s{i}"}
                if r.random() < 0.8:
                    w["branch"] = "refs/heads/" + r.choice(["feat", "main", "master", "x"])
                for key, p in (("locked", 0.1), ("prunable", 0.1)):
                    if r.random() < p:
                        w[key] = True
                wts.append(w)
            case.fake("worktrees", lambda rng, repo: wts)
            case.fake("env_num", lambda rng, name, default: rng.choice([3, 120]))
            case.fake("remote", lambda rng: "origin")
            case.fake("err", lambda rng, m: None)
            case.fake("gitdir_activity", lambda rng, repo, path: maybe(rng, 1_795_000_000 - rng.choice([60, 7200, 400000]), 0.6))
            case.fake("merged_reason", lambda rng, repo, sha, main: maybe(rng, "ancestor", 0.5))
            case.fake("git", lambda rng, *a, **k: proc(rng, rng.choice([0, 0, 0, 1, 124]), rng.choice(["", "", " M a\n?? b\n"])) if "status" in a
                      else proc(rng, 0, rng.choice(["2\n", "\n"])))
            cwds = {1: "/w/t1", 2: "/w/t2/sub", 3: "/elsewhere", 4: "/w/t10"}
            until = r.choice([None, 100.0 + r.randint(0, 120), 1.0])
            return ("repo", "refs/remotes/origin/main", r.choice([1, 24]), cwds, r.random() < 0.5, until)

        def call(A):
            real = os.path.isdir
            os.path.isdir = lambda p: p != "/w/gone"

            def run(*args):
                try:
                    return A.plan_worktrees(*args)
                finally:
                    os.path.isdir = real
            return run
        check(self, "plan_worktrees", build, call)

    def test_close_worktree(self):
        def build(case):
            r = case.rng
            wt = r.choice(["/w/a", "/w/b", "/root"])
            trees = [{"worktree": "/root", "branch": "refs/heads/main"}]
            for p in ("/w/a", "/w/b"):
                if r.random() < 0.85:
                    t = {"worktree": p, "branch": "refs/heads/" + r.choice(["feat", "main", "other"])}
                    if r.random() < 0.1:
                        t["locked"] = True
                    if r.random() < 0.15:
                        del t["branch"]
                    trees.append(t)
            def worktrees_(rng, repo):
                if rng.random() < 0.05:
                    raise RuntimeError("not a repo")
                return trees if rng.random() < 0.9 else trees[:1] + [{"worktree": "/w/a", "branch": "refs/heads/feat"}]
            case.fake("worktrees", worktrees_)
            case.fake("process_cwds", lambda rng: {p: rng.choice(["/w/a", "/w/b/x", "/z", "/root"]) for p in rng.sample(range(10), rng.randint(0, 3))})
            case.fake("own_ancestry", lambda rng: set(rng.sample(range(10), rng.randint(0, 3))))
            case.fake("remote", lambda rng: "origin")
            def git_(rng, *a, **k):
                if "status" in a:
                    return proc(rng, rng.choice([0, 0, 0, 1]), rng.choice(["", "", " M x\n"]))
                if "worktree" in a:
                    return proc(rng, rng.choice([0, 0, 1]), "", "refused " * 40)
                if "update-ref" in a:
                    return proc(rng, rng.choice([0, 1]))
                if "rev-parse" in a:
                    return "headsha\n"
                return proc(rng)
            case.fake("git", git_)
            case.fake("cherry_landed", lambda rng, wt, main, head: (["c"], rng.choice([None, [], ["a"], ["a", "b"]])))
            case.fake("merged_reason", lambda rng, root, head, main: maybe(rng, "ancestor of main", 0.6))
            case.fake("log_deletion", lambda rng, *a: None)
            case.put("os", types.SimpleNamespace(**{k: getattr(os, k) for k in dir(os) if not k.startswith("__")}))
            A.os.path = types.SimpleNamespace(**{k: getattr(os.path, k) for k in dir(os.path) if not k.startswith("__")})
            A.os.path.realpath = lambda p: p
            return (wt,)
        check(self, "close_worktree", build, lambda A: A.close_worktree, 4000)

    def test_sweep_repo(self):
        def build(case):
            r = case.rng
            clock(case)
            case.put("CACHE_TREES", "/h/.cache/")
            names = ["/w/a", "/w/b", "/h/.cache/c"]
            wts = [{"worktree": "/root"}] + [{"worktree": p, "HEAD": f"h{i}", **({"branch": "refs/heads/b" + str(i)} if r.random() < 0.7 else {})}
                                             for i, p in enumerate(r.sample(names, r.randint(0, 3)))]
            case.fake("worktrees", lambda rng, repo: wts)
            case.fake("remote", lambda rng: "origin")
            case.fake("bounded_fetch", lambda rng, repo, *a, **k: r.choice([True, True, "timed out"]))
            case.fake("merged_reason", lambda rng, repo, sha, main: maybe(rng, "ancestor", 0.4))
            case.fake("gitdir_activity", lambda rng, repo, path: maybe(rng, 1_795_000_000 - rng.choice([60, 7200, 900000]), 0.7))
            case.fake("git_ok", lambda rng, *a: rng.random() < 0.7)
            case.fake("slug", lambda rng, repo: "o/r")
            case.fake("delete_remote_branch", lambda rng, *a: raising(rng, 0.2))
            case.fake("log_deletion", lambda rng, *a: None)
            case.fake("orphan_branches", lambda rng, repo, o_r, dry, out, store: raising(rng, 0.15, rng.random() < 0.5) if rng.random() < 0.8 else (_ for _ in ()).throw(SystemExit(2)))

            def git_(rng, repo, *a, **k):
                if "rev-parse" in a:
                    return "mainsha1234567\n"
                if "log" in a:
                    return proc(rng, 0, rng.choice(["", "1794000000\n", "1790000000\n"]))
                if "rev-list" in a:
                    return "a b c"
                if "for-each-ref" in a:
                    return "\n".join(f"{rng.choice(['main', 'b0', 'b1', 'lone', 'old'])} s{i} {rng.choice([1794999000, 1700000000])}" for i in range(rng.randint(0, 4)))
                if "ls-remote" in a:
                    return "\n".join(f"sha{i:03d} refs/heads/{rng.choice(['main', 'x', 'measure/q', 'y', A.SWEEP_KEEP_REMOTE and sorted(A.SWEEP_KEEP_REMOTE)[0]])}" for i in range(rng.randint(0, 4)))
                return proc(rng)
            case.fake("git", git_)
            out = []
            return ("/p/repo", r.choice([1, 3]), r.random() < 0.4, out, Probe(case, "store"))

        def call(A):
            def run(*args):
                out = args[3]
                A.sweep_repo(*args)
                return out
            return run
        check(self, "sweep_repo", build, call, 4000)


class JvmReapTest(unittest.TestCase):
    def test_sbt_stuck_reasons(self):
        def build(case):
            r = case.rng
            case.fake("pid_alive", lambda rng, pid: rng.random() < 0.5)
            case.fake("env_num", lambda rng, name, default: rng.choice([default, 1, 5, 30]))
            now = r.choice([0, 600, 5000, 90000])
            state = {}
            for pid in ("7", "8"):
                if r.random() < 0.7:
                    state[pid] = {k: v for k, v in (("cpu", r.choice([0.0, 5.0, 100.0, 400.0])), ("at", now - r.choice([-60, 0, 30, 300, 1200, 5000])),
                                                    ("since", now - r.choice([0, 60, 900, 1800, 7200]))) if k == "since" or r.random() < 0.9}
            serving = {"/wt": r.choice([(5, 6, "sbt compile"), (5, None, "cmd"), (5, 6, "x" * 90)])} if r.random() < 0.4 else {}
            clients = {"/wt": [42, 43]} if r.random() < 0.5 else {}
            kwargs = {"ppid": r.choice([None, 1, 1, 9]), "kept": r.choice([(), ("/wt",), {"/z"}]), "swap_free_mb": r.choice([None, 100, 9000])}
            args = (r.choice([7, 8, 9]), r.choice(["/wt", "/other"]), r.choice([0.0, 100.0, 100.3, 401.0, 50.0]), clients, serving, state, now)

            return args + (kwargs["ppid"], kwargs["kept"], kwargs["swap_free_mb"])

        def call(A):
            def run(*args):
                state = args[5]
                before = {k: dict(v) for k, v in state.items()}
                out = A.sbt_stuck_reasons(*args)
                return out, state, before
            return run
        check(self, "sbt_stuck_reasons", build, call, 5000)

    def test_jvm_candidates(self):
        def build(case):
            r = case.rng
            case.fake("jvm_knob", lambda rng, name, default: rng.choice([default, default, "1", "30", "0"]) if name in ("HOG_MIN_AGE_SECONDS", "HOG_MIN_CPU_PERCENT", "ORPHAN_MIN_AGE_SECONDS", "HOG_REQUIRE_NO_TTY", "PROTECTED_PORT_MAX_CPU_PERCENT") else default)
            argss = ["java -jar sbt-launch.jar --detach-stdio", "java sbt.ForkMain 1", "java -cp x sbt.internal.worker.WorkerMain", "java Foo", "/usr/bin/java IntelliJ x",
                     "sbt-launch.jar --detach-stdio -Dx", "bash java", "java org.scala.sbt.ForkMain", "python x"]
            rows = [(r.choice([1, 2, 3, 4, 5, 6, 7, 8, 9]), r.choice([1, 1, 50]), r.choice(["??", "ttys001", "-"]), r.choice(["02:00", "11:00", "01:30:00", "2-03:00:00", "x"]),
                     r.choice(["00:10", "01:00:00", "00:00", "00:01:30", "1-00:00:00"]), r.choice(argss)) for _ in range(r.randint(0, 7))]
            cwd = {p: r.choice(["/a", "/b", "/a.part", "/g"]) for p in range(1, 10) if r.random() < 0.7}
            kept = {d for d in ("/a", "/b") if r.random() < 0.3}
            ports = {p for p in range(1, 10) if r.random() < 0.15}
            isdir = lambda d: d in r.choice([("/a", "/b"), ("/a",), ("/b", "/a.part"), ()])  # noqa: E731
            return (rows, cwd, kept, ports, r.choice([0, 5]), isdir)
        check(self, "jvm_candidates", build, lambda A: A.jvm_candidates, 6000)

    def test_jvm_scan(self):
        def build(case):
            r = case.rng
            files = {}
            env = {}

            def make(name, lines):
                path = f"/f/{name}"
                files[path] = "\n".join(lines)
                env[name] = path
            if r.random() < 0.5:
                make("AGENTCTL_JVM_PS", [r.choice(["10 1 ?? 01:00 00:10 java -jar sbt-launch.jar --detach-stdio", "11 10 ttys0 00:20 00:01 sbt-warm --keep x", "x y z", "12 1 ?? 1 2 bash",
                                                   "13 1 ?? 02:00 00:30 /usr/bin/java Foo"]) for _ in range(r.randint(0, 5))])
            if r.random() < 0.4:
                make("AGENTCTL_JVM_CWD", [r.choice(["10 /a", "13 /b", "x /c", "11 /not-allowed", "10"]) for _ in range(r.randint(0, 3))])
            if r.random() < 0.4:
                make("AGENTCTL_JVM_KEPT", [r.choice(["/a", " /b ", "", "/not-allowed"]) for _ in range(r.randint(0, 3))])
            case.put("os", types.SimpleNamespace(environ=env, path=os.path, getpgid=os.getpgid, makedirs=os.makedirs, kill=os.kill, getpid=os.getpid))
            case.fake("run_cmd", lambda rng, cmd, **kw: types.SimpleNamespace(stdout=rng.choice(["10 1 ?? 01:00 00:10 java -jar x\n11 2 ?? 1 2 java y\nbad\n", "", "5\n6\nx\n"]), returncode=0))
            case.fake("lsof_cwds", lambda rng, pids: {p: rng.choice(["/a", "/b", "/zz"]) for p in pids if rng.random() < 0.7})
            case.fake("allowed_path", lambda rng, p: p != "/not-allowed")
            case.fake("jvm_knob", lambda rng, name, default: rng.choice([default, "9000 9001", ""]))
            return (files,)

        def call(A):
            import io
            from unittest import mock

            def run(files):
                with mock.patch("builtins.open", lambda path, *a, **k: io.StringIO(files[path]) if path in files else open_real(path, *a, **k)):
                    rows, cwd, kept, ports = A.jvm_scan()
                return rows, cwd, kept, ports
            open_real = open
            return run
        check(self, "jvm_scan", build, call, 4000)

    def test_jvm_reap(self):
        def build(case):
            r = case.rng
            alive = {}
            sig = []
            case.fake("jvm_knob", lambda rng, name, default: rng.choice([default, "0.6", "5"]))
            case.fake("pid_alive", lambda rng, pid: alive.setdefault((pid, len(sig)), rng.random() < 0.6) if rng.random() < 0.3 else rng.random() < 0.6)
            case.fake("run_cmd", lambda rng, cmd, **kw: types.SimpleNamespace(stdout=rng.choice(["1.0\n", "50\n", "x\n", ""]), returncode=0))
            case.fake("agent_work", lambda rng, pid, procs: maybe(rng, f"pid 3 ({'c' * 70})", 0.3))
            case.fake("thread_dump", lambda rng, pid, cwd: f"/dump/{pid}")
            case.fake("reap_log", lambda rng, line: None)

            def getpgid(pid):
                v = r.choice([0, 1, 2, "gone"])
                if v == "gone":
                    raise OSError("no")
                return v

            def kill(pid, s):
                sig.append((pid, s))
                if r.random() < 0.15:
                    raise ProcessLookupError()
            case.put("os", types.SimpleNamespace(getpgid=lambda pid: 0 if pid == 0 else getpgid(pid), kill=kill, path=os.path))
            case.put("time", types.SimpleNamespace(sleep=lambda s: None, time=lambda: 1.0))
            cands = [(r.choice([5, 6, 7, 8]), 1, r.choice([4000, 90000]), 10.0, r.choice([1.0, 55.5]), r.choice(["orphan", "hog", "dup-sbt-server", "dup-sbt-gone-dir"]),
                      r.choice(["java sbt-launch.jar", "java -Duser.country=PL -Duser.language=pl x " + "y" * 200, "java xsbt.boot", "java Foo"])) for _ in range(r.randint(0, 4))]
            return (cands, r.choice([True, True, False]), [(1, 0, "e", "c")])

        def call(A):
            import contextlib, io

            def run(*args):
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    out = A.jvm_reap(*args)
                return out, buf.getvalue()
            return run
        check(self, "jvm_reap", build, call, 5000)


def raising(rng, p, value=None):
    if rng.random() < p:
        raise RuntimeError("boom")
    return value


if __name__ == "__main__":
    unittest.main()
