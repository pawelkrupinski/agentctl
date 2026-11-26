"""RESTRUCTURE ROUND 8: each function split into named stages keeps its behaviour. The digests (fixtures/
restructure8-digests.json, recorded from the code BEFORE the split with `R4_RECORD=1`) cover thousands of seeded cases
each, collaborators faked (golden_cases.py): every call, its order and the result. The clock is the case's own."""
import builtins, contextlib, glob as glob_module, inspect, io, os, shutil, sys, tempfile, types, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402


def change_state_of(ok, via=None):
    """The LandState a drawn (landed, via) stands for: True landed, None unknown, any other falsy not landed."""
    return A.LandState(A.LANDED if ok else A.UNKNOWN if ok is None else A.NOT_LANDED, via=via if ok else None)
from golden_cases import Probe, check as _check  # noqa: E402

DIGESTS = os.path.join(HERE, "fixtures", "restructure8-digests.json")
NOW = 1_795_000_000.0


def check(test, name, build, fn_of, n=3000):
    _check(test, A, name, build, fn_of, n, DIGESTS)


def maybe(rng, value, p=0.5):
    return value if rng.random() < p else None


def fixed_time(case, now=NOW):
    """time of the case: the clock reads `now`, a sleep moves it (nothing else does)."""
    state = [now]

    def sleep(secs):
        state[0] += secs
    case.put("time", types.SimpleNamespace(time=lambda: state[0], monotonic=lambda: state[0], sleep=sleep,
                                           strftime=lambda f, t: f"{f}@{int(t)}", localtime=lambda t: t))
    return state


class Sink:
    """A file the code under test opens: every open and write is the case's log."""

    def __init__(self, case, path, mode):
        self.case, self.path, self.mode = case, path, mode

    def __enter__(self):
        self.case.log.append(["open", self.path, self.mode])
        return self

    def __exit__(self, *exc):
        return False

    def write(self, text):
        self.case.log.append(["write", self.path, text])
        return len(text)


def fake_open(case):
    case.attrs.append((builtins, "open", builtins.open))
    builtins.open = lambda path, mode="r", *a, **k: Sink(case, str(path), mode)


def captured(holder, fn):
    """fn with its stdout and stderr in the case's log (the case is holder["case"], set by build)."""
    def run(*args):
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
                return fn(*args)
        finally:
            holder["case"].log.append(["output", out.getvalue()])
    return run


def raising(rng, p, value, exc=RuntimeError):
    if rng.random() < p:
        raise exc("down")
    return value


# ------------------------------------------------------------------------------------------------ fixerland
TREES = ["/w/a", "/w/b", "/w/c", "/h/w/d", "/w/gone", None, ""]


def a_red(r):
    u = {"key": r.choice([None, "k1", "k2"]), "run": {"number": r.choice([None, 11, 12]), "databaseId": r.choice([None, 5])},
         "workflow": "W", "job": r.choice([None, "j"])}
    for key, gen in (("repo", lambda: "o/other"), ("jobs", lambda: ["a", "b"]), ("kind", lambda: "test"), ("line", lambda: "l"),
                     ("flake", lambda: r.choice([0, 1])), ("diagnosis", lambda: "d"), ("refuted", lambda: ["x"]),
                     ("predicts", lambda: "p"), ("orphan", lambda: True), ("prior_work", lambda: {"worktree": r.choice(TREES)}),
                     ("blame_note", lambda: "b"), ("lander", lambda: "l1"), ("decision", lambda: "dec"),
                     ("history", lambda: [{"worktree": r.choice(TREES)} for _ in range(r.randint(0, 2))])):
        if r.random() < 0.4:
            u[key] = gen()
    return u


def trees_exist(case):
    """os.path.isdir / exists of the case: a tree exists unless it is /w/gone, empty or none; the home is /h."""
    case.put("HOME", "/h")
    case.patch(os.path, "isdir", lambda rng, p: p not in (None, "", "/w/gone") and rng.random() < 0.9)
    case.patch(os.path, "exists", lambda rng, p: rng.random() < 0.2)


class RedDispatchTest(unittest.TestCase):
    def build(self, case):
        r = case.rng
        trees_exist(case)
        case.patch(os, "makedirs", lambda rng, *a, **k: None)
        case.patch(glob_module, "glob", lambda rng, pat: [f"f{i}" for i in range(rng.randint(0, 2))])
        for name, gen in (
                ("on_ci_runner", lambda rng: rng.random() < 0.2),
                ("laptop_role_standby", lambda rng, st: maybe(rng, "a standby machine", 0.15)),
                ("reds_dir", lambda rng, *a: "/r/" + "/".join(str(x) for x in a)),
                ("red_repo_tag", lambda rng, o_r, num: f"{o_r}#{num}"),
                ("previous_fixer_tree", lambda rng, u: maybe(rng, {"worktree": rng.choice(TREES[:4]), "note": "continue"}, 0.4)),
                ("fresh_worktree", lambda rng, repo, kind, num: maybe(rng, "/w/fresh", 0.8) and ("/w/fresh", None) or (None, "no room")),
                ("dispatch_log", lambda rng, u, what: "LOG " + what),
                ("ts", lambda rng: "T"),
                ("red_is_other", lambda rng, o_r: rng.random() < 0.3),
                ("fixer_env", lambda rng, o_r, num: {"E": str(num)}),
                ("spawn_fixer", lambda rng, *a: types.SimpleNamespace(pid=rng.randint(100, 999))),
                ("socket_host", lambda rng: "host1"),
                ("inbox_write", lambda rng, *a, **k: None),
                ("fixer_label", lambda rng, rec: f"label-{rec.get('run')}"), ("err", lambda rng, m: None)):
            case.fake(name, gen)
        resume = maybe(r, {"worktree": r.choice(TREES), "session": r.choice([None, "s0"]), "note": r.choice([None, "n"]),
                           "launch_cwd": r.choice([None, "/l"])}, 0.35)
        took = r.choice([None, {"epoch": 3, "secret": "sec"}, {"secret": "sec2"}])
        extra = r.choice([(), ({"name": "n1", "epoch": 1, "secret": "s1"},), ({"name": "n1", "epoch": 1, "secret": "s1"}, {"name": "n2", "epoch": 2, "secret": "s2"})])
        store = maybe(r, Probe(case, "store", set_meta=lambda rng, *a: raising(rng, 0.2, None)), 0.7)
        args = ("o/r", "/repo", a_red(r), r.choice(["by hand", "auto"]), resume, r.choice([None, "high"]),
                r.choice([None, "L"]), took, extra, store)
        return args[:r.choice([3, 10, 10, 10])]  # the first three: every default

    def test_red_dispatch(self):
        check(self, "red_dispatch", self.build, lambda A: A.red_dispatch)


class PreviousFixerTreeTest(unittest.TestCase):
    def build(self, case):
        r = case.rng
        trees_exist(case)
        fixers = [{"worktree": r.choice(TREES), "started": r.choice([None, "2026-11-01", "2026-11-02", "2026-11-03"])} for _ in range(r.randint(0, 4))]
        case.fake("fixers_of", lambda rng, key, run, fam=None: list(fixers))
        case.fake("red_family", lambda rng, x: maybe(rng, "fam", 0.5))
        case.fake("allowed_path", lambda rng, p: rng.random() < 0.85)
        case.fake("remote", lambda rng: "origin")
        case.fake("unlanded_commits", lambda rng, wt: [(f"sha{rng.randint(0, 9)}{'x' * 12}", rng.choice([0, 100, 200, 300])) for _ in range(rng.choice([0, 0, 1, 2, 3, 12]))])
        return (a_red(r),)

    def test_previous_fixer_tree(self):
        check(self, "previous_fixer_tree", self.build, lambda A: A.previous_fixer_tree, 4000)


class FixersLandWatchTest(unittest.TestCase):
    def build(self, case):
        r = case.rng
        trees_exist(case)
        fake_open(case)
        fixed_time(case)
        self.pace = A.LandWatchPace(r.choice([0.0, NOW - 10, NOW - 100]))
        recs = [{"path": f"/r/f{i}.json", "worktree": r.choice(TREES), "alive": r.random() < 0.8, "run": r.choice([None, 3])}
                for i in range(r.randint(0, 3))]
        times = [NOW - 60 * m for m in (0, 10, 44, 45, 46, 89, 90, 91, 300)]
        case.fake("fixer_records", lambda rng: recs)
        case.fake("env_num", lambda rng, name, default: rng.choice([default, default, 60, 600, 5400]))
        case.fake("red_owner_only", lambda rng, repo: rng.random() < 0.15)
        case.fake("red_repo_of", lambda rng, rec: "o/r")
        case.fake("read_record", lambda rng, p: maybe(rng, rng.choice([{}, {"nudged_at": NOW - 3000}, {"nudged_at": NOW - 100, "escalated_at": NOW - 50},
                                                                       {"escalated_at": NOW}]), 0.7))
        case.fake("unlanded_commits", lambda rng, wt: [(f"sha{i}{'y' * 12}", rng.choice(times + [0])) for i in range(rng.choice([0, 0, 1, 2, 9]))])
        case.fake("fixer_label", lambda rng, rec: f"lbl{rec['path'][-6:-5]}")
        case.fake("inbox_write", lambda rng, *a, **k: None)
        case.fake("write_json", lambda rng, p, st: None)
        case.fake("ts", lambda rng: "T")
        case.patch(os, "unlink", lambda rng, p: raising(rng, 0.3, None, OSError))
        args = (maybe(r, NOW + r.choice([0, 1, 30, 61, 4000]), 0.7), r.random() < 0.3)
        return args[:r.choice([0, 1, 2, 2, 2])]  # none: both defaults

    def test_fixers_land_watch(self):
        check(self, "fixers_land_watch", self.build, lambda A: lambda *a: A.fixers_land_watch(*a, pace=self.pace), 4000)


# ------------------------------------------------------------------------------------------------ classify
STEPS = ["Set up job", "actions/checkout@v4", "Checkout code", "Run actions/upload-artifact@v4", "Run actions/cache@v4",
         "docker/login-action@v3", "Log in to registry", "Runner preflight", "Run foo/setup-java@v4", "Run tests", "Build",
         "create-github-app-token", "azure/login", "Post job cleanup", "sbt testUnit"]
LOG_LINES = ["*** FAILED ***", "[error] Test failed", "##[error]Process completed with exit code 1", "::error::boom",
             "::error::UNKNOWN: proxy unreachable", "::warning::UNKNOWN: x", "DECODO DOWN: yes", "::error::Acme refuses the key",
             "::error::Acme refuses the key: request limit reached", "daily limit hit", "too many requests",
             "Connection reset by peer", "ENOENT: no such file", "No space left on device", "The operation was canceled",
             "all good", "  ", "FAILED rows are fine", "  1) [chromium] › tests/x.spec.ts:1:1", "  2 failed", "Killed",
             "Resource not accessible by integration", "exit code 137", "assert failed", "Could not resolve host: x",
             "::error::UNKNOWN: proxy unreachable " + "u" * 200, "::error::Acme refuses the key " + "k" * 200,
             "::error::Acme refuses the key: quota exceeded " + "q" * 200, "ENOENT " + "e" * 200, "x " * 100 + "ENOENT",
             "##[error]" + "m" * 200]


class ClassifyJobTest(unittest.TestCase):
    def build(self, case):
        r = case.rng
        steps = [{"name": r.choice(STEPS), "conclusion": r.choice(["success", "failure", "failure", "timed_out", None])} for _ in range(r.randint(0, 4))]
        job = r.choice([{}, {"steps": None}]) if r.random() < 0.1 else {"steps": steps}
        lines = [r.choice(LOG_LINES) for _ in range(r.choice([0, 0, 1, 2, 3, 5]))]
        raw = r.choice([(0, []), (0, ["t"]), (7, []), (5, ["tail one", "tail two"]), (3, ["x"])])
        return (job, lines, raw, r.random() < 0.3) if r.random() < 0.9 else (job, lines)

    def test_classify_job(self):
        check(self, "classify_job", self.build, lambda A: A.classify_job, 6000)


# ------------------------------------------------------------------------------------------------ checkslot
class AdjustCapTest(unittest.TestCase):
    PRESSURE = {"calm": [None, 10, 20, 49], "mid": [None, 49, 50, 60, 74], "high": [None, 10, 74, 75, 90]}
    SWAP = {"calm": [None, 0, 30, 63, 64], "mid": [None, 0, 100, 300], "high": [None, 0, 511, 512, 600]}

    def run_of(self, r, cap, profile, at, durations):
        running = r.choice([cap, cap, cap - 1, cap - 1, cap + 1, 0])
        run = {"duration": durations.get(running, 5) * (3 if r.random() < 0.15 else 1), "running": running, "at": at}
        for key, pool in (("pressure", self.PRESSURE[profile[0]]), ("swap_mb", self.SWAP[profile[1]]), ("swap_mb_start", [None, 0, 0, 50])):
            if r.random() < 0.85:
                run[key] = r.choice(pool)
        if r.random() < 0.03:
            del run["at"]
        return run

    def build(self, case):
        r = case.rng
        cap = r.randint(1, 4)
        profile = (r.choice(["calm", "calm", "calm", "mid", "high"]), r.choice(["calm", "calm", "calm", "mid", "high"]))
        below = r.choice([10, 20, 30])
        durations = {cap: int(below * r.choice([1, 1.5, 1.6, 3])), cap - 1: below}
        day = [self.run_of(r, cap, profile, NOW - r.choice([60, 4000, 86400, 86399]), durations) for _ in range(r.choice([0, 3, 4, 5, 6, 10, 25]))]
        for run in day:
            run["waited"] = r.choice([0, 30, 60])
        for run in r.sample(day, r.randint(0, len(day))):
            run["waited"] = r.choice([61, 120])
        old = [self.run_of(r, cap, profile, NOW - r.choice([86401, 90000, 200000]), durations) for _ in range(r.choice([0, 0, 3, 6]))]
        runs = day + old
        r.shuffle(runs)
        return (runs, cap, r.randint(1, 5), maybe(r, NOW - r.choice([100, 86399, 86400, 86401, 500000]), 0.8), NOW)

    def test_adjust_cap(self):
        check(self, "adjust_cap", self.build, lambda A: A.adjust_cap, 8000)


# ------------------------------------------------------------------------------------------------ superseded
def proc(rng, code=0, out="", error=""):
    return types.SimpleNamespace(returncode=code, stdout=out, stderr=error)


PATHS = ["/w/a", "/w/b", "/w/c", "/w/gone", "/h/w/e"]


class SuccessorStateTest(unittest.TestCase):
    def build(self, case):
        r = case.rng
        trees_exist(case)
        case.patch(os.path, "isdir", lambda rng, p: rng.random() < 0.5)
        case.fake("change_state", lambda rng, repo, head: change_state_of(*rng.choice([(None, None), (True, "s" * 12), (True, None), (False, None)])))
        states = ["queued", "checking", "checked", "pushing", "landed", "rejected", "superseded", "cancelled", "waiting"]

        def item(rng, typ, cid):
            return maybe(rng, {"id": rng.choice([cid, cid, "c9"]), "state": rng.choice(states),
                               "data": {"superseded_by": rng.choice([None, "c2", "c3", cid])}}, 0.8)

        m_tree = r.choice(PATHS)

        def items(rng, typ, only=None):
            return [{"id": f"c{i}", "state": rng.choice(states) if not only else only[0], "entered_at": rng.choice([None, 50, 150, 250]),
                     "data": {"worktree": rng.choice(PATHS + [m_tree, m_tree, None, ""])}} for i in range(rng.randint(0, 3))]
        store = Probe(case, "store", item=item, items=lambda rng, typ, only=None: raising(rng, 0.05, items(rng, typ, only)))
        m = {"by": r.choice(["ci-5", "ci-6"]), "at": r.choice([None, 0, 1, 100, 200.0])}
        for key, gen in (("change", lambda: r.choice(["c1", "c2"])), ("worktree", lambda: m_tree), ("head", lambda: "h" * 40)):
            if r.random() < 0.5:
                m[key] = gen()
        return (m, "/repo", store)

    def test_successor_state(self):
        check(self, "successor_state", self.build, lambda A: A.successor_state, 6000)


class CloseSupersededTest(unittest.TestCase):
    def build(self, case):
        r = case.rng
        trees_exist(case)
        trees = [{"worktree": "/root", "branch": "refs/heads/main"}]
        for p in PATHS[:3] + ["/h/w/e"]:
            if r.random() < 0.85:
                t = {"worktree": p, "branch": "refs/heads/" + r.choice(["feat", "main", "other"])}
                if r.random() < 0.1:
                    t["locked"] = True
                if r.random() < 0.2:
                    del t["branch"]
                trees.append(t)
        case.fake("worktrees", lambda rng, repo: raising(rng, 0.03, trees if rng.random() < 0.9 else trees[:1]))
        case.fake("process_cwds", lambda rng: raising(rng, 0.05, {p: rng.choice(["/w/a", "/w/b/x", "/z", "/root"]) for p in rng.sample(range(10), rng.randint(0, 6))},
                                                      SystemExit))

        def git_(rng, repo, *a, **k):
            if "status" in a:
                return proc(rng, rng.choice([0, 0, 0, 1]), rng.choice(["", "", " M x\n?? y\n"]))
            if "worktree" in a:
                return proc(rng, rng.choice([0, 0, 1]), "", "refused " * 40)
            if "update-ref" in a:
                return proc(rng, rng.choice([0, 1]))
            return "h" * 40 + "\n"
        case.fake("git", git_)
        case.fake("log_deletion", lambda rng, *a: None)
        return ("/repo", r.choice(["/w/a", "/w/b", "/w/c", "/root", "/w/gone", "/h/w/e"]), r.choice(["by x", "y"]))

    def test_close_superseded(self):
        check(self, "close_superseded", self.build, lambda A: A.close_superseded, 6000)


class CmdSupersedeTest(unittest.TestCase):
    def build(self, case):
        r = case.rng
        trees_exist(case)
        fixed_time(case)
        marks = {p: {"by": f"by{i}", "state": r.choice(["open", "landed", "closed", "lapsed"]), "why": r.choice([None, "w"])}
                 for i, p in enumerate(r.sample(PATHS, r.randint(0, 3)))}
        case.fake("superseded_marks", lambda rng: marks)
        case.fake("superseded_path", lambda rng: "/s.json")
        case.fake("write_json", lambda rng, p, m: None)
        case.fake("ts", lambda rng: "T")

        def git_(rng, repo, *a, **k):
            if "--show-toplevel" in a:
                return proc(rng, 0, rng.choice(["/w/a\n", "/w/b\n", "/h/w/e\n", "", "/w/gone\n"]))
            return rng.choice(["/w/a/.git\n", "/w/bare\n"])
        case.fake("git", git_)

        def resolve(rng, by, wt, store):
            v = rng.random()
            if v < 0.15:
                raise ValueError("agentctl: no such")
            if v < 0.2:
                raise SystemExit("agentctl: NoToken")
            return (rng.choice(["ci-5", "c1"]), rng.choice([None, "c1"]), rng.choice([None, "/w/a", "/w/c"]), rng.choice([None, "h1"]))
        case.fake("resolve_successor", resolve)
        a = types.SimpleNamespace(worktree=r.choice([None, "/w/a", "/w/b", "/w/c", "/w/gone", "/h/w/e"]), all=r.random() < 0.4,
                                  undo=r.random() < 0.25, by=r.choice([None, "ci-5", "c1"]))
        holder = {"case": case}
        return (a, "store"), holder

    def test_cmd_supersede(self):
        def run(A):
            def inner(args, holder):
                with contextlib.redirect_stdout(io.StringIO()) as out:
                    try:
                        rc = A.cmd_supersede(*args)
                    finally:
                        holder["case"].log.append(["output", out.getvalue()])
                return rc, A.superseded_marks()
            return lambda packed, holder=None: inner(*packed)
        check(self, "cmd_supersede", lambda case: (self.build(case),), run, 6000)


class SupersedeOneTest(unittest.TestCase):
    """_supersede_one: one mark's pass (the successor read, its worktree closed, the line said)."""

    def build(self, case):
        r = case.rng
        trees_exist(case)
        case.fake("ts", lambda rng: "T")
        case.fake("successor_state", lambda rng, m, repo, store: raising(rng, 0.15, (rng.choice(["open", "landed", "lapsed"]), "why"), rng.choice([RuntimeError, SystemExit])))
        case.fake("close_superseded", lambda rng, repo, wt, why: (rng.random() < 0.5, rng.choice(["closed x", "kept: locked"])))
        case.fake("inbox_write", lambda rng, *a, **k: None)
        m = {"by": "ci-5", "state": r.choice(["open", "landed"])}
        if r.random() < 0.4:
            m["said"] = r.choice(["kept: locked", "other"])
        did = []
        return ("/repo", "store", r.choice(["/w/a", "/h/w/e"]), m, r.random() < 0.2, did), did

    def test_supersede_one(self):
        def run(A):
            def inner(args, did):
                result = A._supersede_one(*args)
                return result, did, args[3]
            return lambda packed: inner(*packed)
        check(self, "_supersede_one", lambda case: (self.build(case),), run, 4000)


# ------------------------------------------------------------------------------------------------ livestream
class Named:
    """A callable fake with a stable repr (a function's repr holds its address): every call is the case's log."""

    def __init__(self, case, name, gen):
        self.name, self.call = name, case.recorded(name, gen)

    def __call__(self, *a, **k):
        return self.call(*a, **k)

    def __repr__(self):
        return f"<{self.name}>"


class LiveStreamTest(unittest.TestCase):
    def build(self, case):
        r = case.rng
        clock = fixed_time(case)
        ents = [None, {"type": "user"}, {"type": "assistant", "x": 1}, {"type": "raw", "text": "t"}]
        case.fake("env_num", lambda rng, name, default: rng.choice([0.5, 1.0]))
        case.fake("live_entry", lambda rng, line: rng.choice(ents))
        case.fake("live_tail_info", lambda rng, path: {"stop": rng.choice([None, "end_turn"]), "last_ts": "t0", "api_error": rng.random() < 0.2,
                                                       "size": 5, "mtime": 3})

        def track(rng, info, ent):
            if rng.random() < 0.5:
                info["stop"] = rng.choice([None, "end_turn", "tool_use"])
            info["last_ts"] = rng.choice(["t1", "t2"])
        case.fake("live_track", track)
        case.fake("live_registry", lambda rng: raising(rng, 0.2, {"a1": {"id": "a1", "transcript": "/t/y.jsonl", "state": rng.choice(["x", "y"])}},
                                                        rng.choice([RuntimeError, SystemExit])))
        case.fake("mtime", lambda rng, path: rng.choice([None, 3.0, 400.0]))
        case.fake("live_state", lambda rng, e, info: rng.choice([("working", "w"), ("done", "d"), ("working", "w"), ("failed", "f")]))

        class Tail:
            def __init__(self, path, start):
                self.offset = start
                case.log.append(["LiveTail", path, start])
                self.poll = Named(case, "tail.poll", self.poll_gen)
                self.close = Named(case, "tail.close", lambda rng: None)

            def poll_gen(self, rng):
                clock[0] += rng.choice([0, 0, 1, 3, 6, 20])
                lines = []
                for _ in range(rng.choice([0, 0, 1, 2, 4])):
                    self.offset += rng.randint(1, 30)
                    lines.append((self.offset, rng.choice([b'{"type": "user"}', b"plain", 999, b"x\ny"])))
                return lines, rng.choice([None, None, None, "truncated", "rotated"]), rng.random() < 0.4
        case.put("LiveTail", Tail)
        stops = [0]

        def closed(rng):
            stops[0] += 1
            return stops[0] > 40 or rng.random() < 0.1

        def write(rng, data):
            return raising(rng, 0.04, None, BrokenPipeError)
        e = {"id": "a1", "transcript": "/t/x.jsonl"}
        for key in ("kind", "label"):
            if r.random() < 0.6:
                e[key] = key.upper()
        default = inspect.signature(A.live_stream).parameters["closed"].default
        unbounded = r.random() < 0.15  # the default `closed`: only max_secs ends it
        return (e, r.choice([0, 0, 120]), Named(case, "write", write), default if unbounded else Named(case, "closed", closed),
                r.choice([None, 0.25, 2]), r.choice([5, 15]), r.choice([4, 30] if unbounded else [4, 30, 3000]))

    def test_live_stream(self):
        check(self, "live_stream", self.build, lambda A: A.live_stream, 3000)


class LiveTailPollTest(unittest.TestCase):
    """LiveTail.poll over a real file in a temp dir: appends (whole lines, partial ones, over-long ones), truncations, rotations."""

    def build(self, case):
        r = case.rng
        ops = []
        for _ in range(r.randint(3, 14)):
            kind = r.choice(["append", "append", "append", "poll", "poll", "truncate", "rotate", "remove"])
            if kind == "append":
                ops.append((kind, b"".join(bytes(r.choice(b"abcdefgh") for _ in range(r.randint(0, 28))) + r.choice([b"\n", b"\n", b""]) for _ in range(r.randint(1, 4)))))
            elif kind == "truncate":
                ops.append((kind, r.randint(0, 20)))
            elif kind == "rotate":
                ops.append((kind, b"".join(bytes(r.choice(b"xyz") for _ in range(r.randint(1, 12))) + b"\n" for _ in range(r.randint(0, 3)))))
            else:
                ops.append((kind, None))
        return (ops, r.random() < 0.7, r.choice([0, 0, 3, 10]), r.choice([8, 16, 64]), r.choice([10, 20, 1000]))

    def test_poll(self):
        def run(A):
            def scenario(ops, exists, offset, chunk, line_max):
                d = tempfile.mkdtemp(prefix="r8-tail-")
                path = os.path.join(d, "t.jsonl")
                out = []
                try:
                    if exists:
                        open(path, "wb").close()
                    tail = A.LiveTail(path, offset, chunk, line_max)
                    for kind, arg in ops:
                        if kind == "append":
                            with open(path, "ab") as f:
                                f.write(arg)
                        elif kind == "truncate" and os.path.exists(path):
                            os.truncate(path, arg)
                        elif kind == "rotate":
                            with open(path + ".new", "wb") as f:
                                f.write(arg)
                            os.replace(path + ".new", path)
                        elif kind == "remove" and os.path.exists(path):
                            os.unlink(path)
                        elif kind == "poll":
                            out.append((tail.poll(), tail.offset, tail.buf, tail.skip))
                    out.append((tail.poll(), tail.offset, tail.buf, tail.skip))
                    tail.close()
                finally:
                    shutil.rmtree(d, ignore_errors=True)
                return out
            return scenario
        check(self, "LiveTail.poll", self.build, run, 4000)


# ------------------------------------------------------------------------------------------------ landdryrun
class DryRunTest(unittest.TestCase):
    OUTS = ["", "all passed", "main is red: only a fix lands", "x failed", "carries no Fixes-Red", "Could not resolve host: x"]

    def build(self, case):
        r = case.rng
        fake_open(case)
        holder = {"case": case, "baseline": r.choice([None, "0", "1", "1"])}
        case.patch(os.path, "exists", lambda rng, p: rng.random() < 0.4)
        case.fake("remote", lambda rng: "origin")
        case.fake("err", lambda rng, m: None)
        case.fake("lock_note", lambda rng, m: None)
        case.fake("tool_env", lambda rng: {"T": "1"})
        case.fake("ts", lambda rng: "T")
        case.fake("reds_dir", lambda rng, *a: "/r/" + "/".join(a))
        case.fake("local_red", lambda rng: rng.choice([{}, {"checks": ["c"]}]))
        case.fake("check_passes", lambda rng, out: ["p"])
        case.fake("note_local_red", lambda rng, *a: None)
        case.fake("local_cleared", lambda rng, repo, main: raising(rng, 0.3, None, rng.choice([RuntimeError, SystemExit])))
        case.fake("stalled", lambda rng, *a: 1)
        case.fake("refused", lambda rng, *a: 1)
        case.fake("network_requeue", lambda rng, *a: rng.choice([0, 1]))

        def git_(rng, repo, *a, **k):
            main = rng.choice(["m1", "m2"])
            return proc(rng, 0, main + "\n") if k.get("check") is False else main + "\n"
        case.fake("git", git_)

        def judge(rng, *a):
            return rng.choice([(None, {}), ({"a"}, {"b"}), (set(), {"b"}), (set(), {"b", "c"}), (None, A.NETWORK_SHAPED), ({"a"}, set())])
        case.fake("judge_against_baseline", judge)
        slot = types.SimpleNamespace(env=Named(case, "slot.env", lambda rng, env: {"S": 1}), done=Named(case, "slot.done", lambda rng, rc, out: None),
                                     sample=Named(case, "slot.sample", lambda rng: None))

        class Held:
            def __repr__(self):
                return "<held>"

            def __enter__(self):
                if case.rng.random() < 0.1:
                    raise A.GovTimeout("the machine is busy")
                return slot

            def __exit__(self, *exc):
                return False
        case.fake("check_slot", lambda rng, repo, label, cls: Held())
        case.fake("run_watched", lambda rng, *a, **k: (rng.choice([0, 0, 1, 1, None]), rng.choice(self.OUTS)))
        args = ("/repo", "/repo/scripts/hooks/pre-push", "h" * 40, "/log", "before the lock", Probe(case, "clock"), Probe(case, "store"),
                r.choice(["--range", "--checks-only"]), r.choice([False, 0, 1, 2, 3]), r.choice(["check", "red"]), r.choice([None, "a"]),
                r.choice([None, "m1", "m2"]))
        return args[:r.choice([7, 7, 12, 12, 12])], holder  # the first seven: every default

    def test_dry_run(self):
        def run(A):
            def inner(args, holder):
                saved = os.environ.get("AGENTCTL_BASELINE")
                if holder["baseline"] is None:
                    os.environ.pop("AGENTCTL_BASELINE", None)
                else:
                    os.environ["AGENTCTL_BASELINE"] = holder["baseline"]
                try:
                    with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()):
                        try:
                            return A.dry_run(*args)
                        finally:
                            holder["case"].log.append(["output", out.getvalue()])
                finally:
                    if saved is None:
                        os.environ.pop("AGENTCTL_BASELINE", None)
                    else:
                        os.environ["AGENTCTL_BASELINE"] = saved
            return lambda packed: inner(*packed)
        check(self, "dry_run", lambda case: (self.build(case),), run, 6000)


if __name__ == "__main__":
    unittest.main()
