"""RESTRUCTURE ROUND 6: each function split into named stages keeps its behaviour. The digests (fixtures/
restructure6-digests.json, recorded from the code BEFORE the split with `R4_RECORD=1`) cover thousands of seeded cases
each, collaborators faked (golden_cases.py): every call, its order and the result."""
import os, sys, types, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402
from golden_cases import Probe, cherry_state, check as _check  # noqa: E402

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
                until = args[-1]  # a Stopwatch now: one whose left() is the old `until - time.monotonic()`, same reads
                left = None if until is None else types.SimpleNamespace(left=lambda: until - A.time.monotonic())
                try:
                    return A.plan_worktrees(*args[:-1], left)
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
            case.fake("landed_state", lambda rng, wt, main, head: cherry_state(A, (["c"], rng.choice([None, [], ["a"], ["a", "b"]]))))
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
            case.fake("sbt_serving", lambda rng: {rng.choice(["/a", "/b"]): (1, None, "x")} if rng.random() < 0.5 else {})
            case.fake("pid_alive", lambda rng, pid: rng.random() < 0.7)
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


FRAGMENTS = ["rm -rf /run/github-runner/b-1/core/docker", "rm /a/b/c/d", "rm -rf /tmp", "rm -rf /a/*", "rm -rf $X/y", "rm -rf ~", "rm -rf ~/a/b/c", "rm -f .",
             "sudo rm -rf /var/lib/x/y/z", "nice -n 5 rm /q/r/s", "docker system prune -af", "docker rm c1", "podman rmi x", "kubectl delete pod x", "kubectl delete pods -A",
             "kubectl delete ns y --all", "find /a/b/c -delete", "find /a -name x", "git clean -fdx", "git reset --hard", "git status", "if [ -d /a/b/c ]; then",
             "test -d /a/b/c", "pgrep -f x >/dev/null", "echo hi", "mongo --eval db.dropDatabase()", "psql -c 'DROP TABLE x'", "TRUNCATE TABLE y", "Runner.Worker",
             "rm -rf /actions-runner/x/_work/y/z", "ls", "fi", "docker ps", "while true", "kubectl get pods", "rm -rf '/a/b/c'", "exec rm /q/w/e", "rm -rf ${V}", "rm -rf `x`/a/b",
             "rm -rf $V", "rm -rf $V/", "rm -rf ..", "rm -", "rm -rf $HOME/a", "rm -rf $(cat x)/a/b/c", "rm -rf -- /a/b/c/d", "rm -rf ~/x", "find / -name x -delete --with-a-very-long-argument-list one two three four five six seven eight"]
JOINERS = [" && ", " || ", "; ", " | ", "\n", " ; do ", " then ", " else "]


class RedBlockTest(unittest.TestCase):
    def test_owner_cmd_unsafe(self):
        def build(case):
            r = case.rng
            if r.random() < 0.05:
                return (r.choice([None, "", 0]),)
            return ("".join(r.choice(FRAGMENTS) + (r.choice(JOINERS) if i else "") for i in range(r.randint(1, 4))),)
        check(self, "owner_cmd_unsafe", build, lambda A: A.owner_cmd_unsafe, 8000)

    def test_block_invalid(self):
        def build(case):
            r = case.rng
            case.fake("lease_is_live", lambda rng, row, t: row is not None and rng.random() < 0.4)
            case.fake("red_lease", lambda rng, key: f"red/{key}")
            case.fake("ask_refusal", lambda rng, cat, q: maybe(rng, "refused", 0.4))
            case.fake("ask_category", lambda rng, m: "cat")
            case.put("OWNER_CAUSES", {"disk": "full disk", "perm": "a permission"})
            case.put("WAIT_CAUSES", {"quota": "a quota"})
            iss = r.choice([None, {"state": "closed", "data": {"note": "n" * 150}}, {"state": "open"}, {"state": "closed", "data": None}])
            st = Probe(case, "store", get=lambda rng, name: maybe(rng, {"holder": "h1"}, 0.6), item=lambda rng, kind, iid: iss)
            m = {}
            for k, gen in (("vault", lambda: True), ("crash_loop", lambda: True), ("decision", lambda: True), ("harness", lambda: True), ("issue", lambda: "i1"),
                           ("wait_cause", lambda: r.choice(["quota", "other"])), ("owner_cause", lambda: r.choice(["disk", "perm", "zz"])),
                           ("cmd", lambda: r.choice(["", " ", "rm /a/b/c/d", "rm -rf /a/b/c/d", "echo ok", "if [ -d /x/y/z ]; then rm /x/y/z; fi"])),
                           ("details", lambda: r.choice(["Request limit reached", "fine"])), ("text", lambda: r.choice(["quota exceeded", ""])),
                           ("decide", lambda: "d"), ("question", lambda: "q")):
                if r.random() < (0.12 if k in ("vault", "crash_loop", "decision") else 0.5):
                    m[k] = gen()
            d = {"key": "k1", **{k: v for k, v in (("kind", r.choice(["test", "infra", "x"])), ("line", r.choice(["HTTP 401: Request limit reached", "boom", "", "quota spent"]))) if r.random() < 0.8}}
            return (st, {"data": d}, m, 5.0)
        check(self, "block_invalid", build, lambda A: A.block_invalid, 8000)

    def test_red_renew_blocks(self):
        def build(case):
            r = case.rng
            keys = [f"k{i}" for i in range(4)]
            case.fake("red_active", lambda rng, st: {i: {"data": {"key": k}} for i, k in enumerate(keys) if rng.random() < 0.8})
            case.fake("lease_is_live", lambda rng, row, t: rng.random() < 0.8)
            case.fake("block_invalid", lambda rng, st, it, m, t: maybe(rng, rng.choice(["why1", "why2"]), 0.35))
            case.fake("red_what", lambda rng, d: f"red-{d['key']}")
            case.fake("run_probe", lambda rng, kind, args: maybe(rng, types.SimpleNamespace(ok=rng.random() < 0.4, detail=rng.choice(["d1", "d2"])), 0.7))
            case.fake("inbox_write", lambda rng, *a: None)
            case.put("PROBE_EVERY", {"p1", "p2"})
            case.fake("lease_meta", lambda rng, row: row["meta"])
            rows = []
            for i in range(r.randint(0, 3)):
                meta = {"keys": r.sample(keys, r.randint(0, 3)), "probed_at": r.choice([0, 40, 95, 99.5]), "until": "u", "text": "t" * 250}
                if r.random() < 0.7:
                    meta["probe"] = {"kind": r.choice(["p1", "p3", None]), "args": {"a": 1}}
                rows.append({"name": f"block/b{i}", "epoch": i, "secret": "s", "meta": meta})
            st = Probe(case, "store", list=lambda rng, prefix: rows)
            return (st, r.choice([100.0, 100.0, 30.0, 60.5]))
        # the registry re-runs a block's probe through run_probe's fake, by kind: the calls recorded before the registry
        registry = types.SimpleNamespace(check=lambda kind: kind)
        check(self, "red_renew_blocks", build, lambda A: lambda st, t: A.red_renew_blocks(st, t, registry), 8000)


class Stamp:
    """A parsed time with a stable repr."""

    def __init__(self, value):
        self.value = value

    def timestamp(self):
        return self.value

    def __repr__(self):
        return f"Stamp({self.value})"


class RedOpenTest(unittest.TestCase):
    def test_red_offer(self):
        def build(case):
            r = case.rng
            tiers = ["author", "revert", "blame", "deep", "fixer", "x"]
            case.fake("red_tier", lambda rng, d, culprit, t: rng.choice(tiers))
            case.fake("decided_revert", lambda rng, d, t: maybe(rng, "c" * 12, 0.6))
            case.fake("red_offer_class", lambda rng, d: rng.choice(["code", "infra"]))
            case.fake("red_decision", lambda rng, d: rng.random() < 0.3)
            case.fake("red_what", lambda rng, d: f"red-{d.get('key')}")
            case.fake("env_num", lambda rng, name, default: rng.choice([default, 5, 100]))
            case.fake("red_move", lambda rng, store, it, state, text, **kw: {**it, "moved": [state, text]})
            case.fake("red_put_offer", lambda rng, store, it: None)
            case.put("RED_OFFER", {"author": 600, "code": 1800, "infra": 3600})
            case.put("BLAME_MAX", 900)
            case.put("HOME", "/home/x")
            st = Probe(case, "store", clock=lambda rng: r.choice([1000.0, 5000.0]))
            d = {"key": "k1"}
            for k, gen in (("culprit", lambda: r.choice(["", "abc"])), ("blame", lambda: {"since": r.choice([0, 500, 4000])}),
                           ("author", lambda: {"worktree": r.choice(["/home/x/wt", "/w"]), "session": "s", "shas": r.choice([[], ["a" * 40, "b" * 40]]), "extra": 1}),
                           ("last_run", lambda: {"number": 7}), ("workflow", lambda: "W"), ("line", lambda: r.choice(["", "boom"])),
                           ("deep_told", lambda: r.choice([0, 1, 2, 3])), ("attempts", lambda: r.choice([None, 0, 2, 3]))):
                if r.random() < 0.6:
                    d[k] = gen()
            data = maybe(r, {"x": 1, **({"culprit": "dc"} if r.random() < 0.3 else {})}, 0.5)
            return (st, {"id": "i1", "data": d}, "text", data, "repo", r.random() < 0.3, maybe(r, "V", 0.3), maybe(r, ["t1"], 0.3), r.random() < 0.3)
        check(self, "red_offer", build, lambda A: A.red_offer, 8000)

    def test_red_diagnosis_step(self):
        def build(case):
            r = case.rng
            case.fake("red_names", lambda rng, d: [1, "2", None])
            case.fake("red_what", lambda rng, d: "R")
            case.fake("red_verify_dispatch", lambda rng, *a: rng.choice(["dispatched x", "none: no", "dispatched y"]))
            case.fake("red_keep_holder", lambda rng, row, text: None)
            case.fake("parse_time", lambda rng, s: Stamp(float(s)))
            changes = [{"id": f"c{i}", "entered_at": r.choice([0, 500, 9000]), "data": maybe(r, {"diagnoses": r.choice([[1], ["2"], [3], None]),
                        **({"landed_sha": "s"} if r.random() < 0.5 else {"tip": "t"} if r.random() < 0.5 else {})}, 0.9)} for i in range(r.randint(0, 3))]

            def items(rng, kind, states):
                if rng.random() < 0.1:
                    raise RuntimeError("store down")
                return changes
            st = Probe(case, "store", items=items, update_item=lambda rng, *a: None)
            done = {f"c{i}": {"at": r.choice([50, 5000]), "dispatch": r.choice(["dispatched z", "none", None]), **({"told": 1} if r.random() < 0.2 else {})}
                    for i in range(3) if r.random() < 0.4}
            d = {"workflow": "W", "opened_at": r.choice([0, 9000]), "last_run": {"databaseId": 5}, **({"diagnosis_runs": done} if done or r.random() < 0.5 else {})}
            snap = maybe(r, {"latest": {"W": maybe(r, {k: v for k, v in (("updatedAt", str(r.choice([100, 6000]))), ("status", r.choice(["completed", "queued"])),
                                                                           ("number", 3), ("conclusion", r.choice([None, "success"])), ("databaseId", 9)) if r.random() < 0.9}, 0.9)}}, 0.8)
            return (st, {"id": "i1", "version": 2, "data": d}, {"row": 1}, snap, "repo", "o/r", r.choice([100, 7000]))

        def call(A):
            def run(*args):
                out = A.red_diagnosis_step(*args)
                return out, args[1]["data"].get("diagnosis_runs")
            return run
        check(self, "red_diagnosis_step", build, call, 8000)


class RedHoldTest(unittest.TestCase):
    def test_red_fix_held(self):
        def build(case):
            r = case.rng
            case.fake("red_change_read", lambda rng, store, entry: (maybe(rng, {"state": rng.choice(["cancelled", "queued", "landed", "checking", "rejected"]), "entered_at": rng.choice([None, 100, 990])}, 0.7),
                                                                    rng.choice([None, None, None, "store down " * 12])))
            case.fake("red_fix_of", lambda rng, it, facts: maybe(rng, {"status": rng.choice(["queued", "landed", "verify"]), "entry": "fe"}, 0.6))
            case.fake("env_num", lambda rng, name, default: rng.choice([default, 5, 50]))
            case.fake("red_fix_since", lambda rng, it: rng.choice([0, 500, 990]))
            case.fake("red_fix_giveup", lambda rng, d: rng.choice([10, 600, 5000]))
            fix = {}
            for k, gen in (("status", lambda: r.choice(["waits-on", "queued", "checking", "verify", "owner", "landed", "gone", None])), ("red", lambda: "other"),
                           ("sha", lambda: "a" * 40), ("entry", lambda: "e1"), ("waited_on", lambda: True),
                           ("verify", lambda: r.choice(["FIX UNVERIFIED: x", "ok"]))):
                if r.random() < 0.6:
                    fix[k] = gen()
            d = {"fix": fix} if r.random() < 0.9 else {}
            facts = r.choice([None, {}, {"queue_unread": "q" * 100}, {"queue_unread": None}])
            return ({"data": d}, 1000.0, facts, r.choice([None, "store"]))
        check(self, "red_fix_held", build, lambda A: A.red_fix_held, 10000)


class RevertsTest(unittest.TestCase):
    def test_tick_reverts(self):
        def build(case):
            r = case.rng
            case.fake("git", lambda rng, repo, *a, **k: types.SimpleNamespace(stdout=rng.choice(["", "m" * 40 + "\n", "n" * 40 + "\n"]), returncode=0))
            case.fake("git_ok", lambda rng, *a: rng.random() < 0.6)
            case.fake("remote", lambda rng: "origin")
            commits = [{"sha": f"{r.randint(0, 9)}" * 14, "reverts": r.choice([[], [], ["a1"], ["a1", "b2"]])} for _ in range(r.randint(0, 4))]
            case.fake("revert_scan", lambda rng, repo, cur, main, t: maybe(rng, commits, 0.85))
            case.fake("revert_closer", lambda rng, it, c: maybe(rng, "how", 0.3))
            case.fake("revert_close", lambda rng, st, it, how, sha: rng.random() < 0.6)
            case.fake("lander_records", lambda rng, st, o_r: ["rec"])
            case.fake("lander_index", lambda rng, recs: "idx")
            case.fake("revert_landings", lambda rng, repo, c, idx: [(maybe(rng, {"lander": maybe(rng, {"name": "l"}, 0.6)}, 0.7), [{"sha": "r" * 14}, {"sha": "q" * 14}][:rng.randint(1, 2)]) for _ in range(rng.randint(0, 2))])
            case.fake("revert_open", lambda rng, st, c, rec, reverted, t: maybe(rng, f"iid{rng.randint(0, 9)}", 0.7))
            case.fake("lander_name", lambda rng, lander: "LN")
            case.fake("revert_deadlines", lambda rng, st, t: [f"dl{i}" for i in range(rng.randint(0, 2))])
            case.put("RED_PRIMARY", "o/primary")
            st = Probe(case, "st", clock=lambda rng: 77.0, meta_get=lambda rng, key: maybe(rng, rng.choice(["m" * 40, "old"]), 0.6),
                       meta_set=lambda rng, k, v: None, items=lambda rng, kind, states=None: [{"id": f"it{i}"} for i in range(rng.randint(0, 2))])
            return (r.choice(["/repo"] * 8 + ["", None]), st, r.choice([None, 5.0]), r.choice([None, "o/r"]))
        check(self, "tick_reverts", build, lambda A: A.tick_reverts, 8000)


def raising(rng, p, value=None):
    if rng.random() < p:
        raise RuntimeError("boom")
    return value


if __name__ == "__main__":
    unittest.main()
