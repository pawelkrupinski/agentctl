"""RESTRUCTURE ROUND 10: each function split into named stages keeps its behaviour. The digests (fixtures/
restructure10-digests.json, recorded from the code BEFORE the split with `R4_RECORD=1`) cover thousands of seeded cases
each, collaborators faked (golden_cases.py): every call, its order and the result."""
import builtins, contextlib, io, os, sys, types, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402
from golden_cases import Probe, check as _check  # noqa: E402

DIGESTS = os.path.join(HERE, "fixtures", "restructure10-digests.json")
NOW = 1_795_000_000.0


def check(test, name, build, fn_of, n=3000):
    _check(test, A, name, build, fn_of, n, DIGESTS)


def maybe(rng, value, p=0.5):
    return value if rng.random() < p else None


def pick(r, *values):
    return r.choice(values)


def proc(rng, code=0, out="", error=""):
    return types.SimpleNamespace(returncode=code, stdout=out, stderr=error)


class Named:
    """A callable fake with a stable repr (a function's repr holds its address): every call is the case's log."""

    def __init__(self, case, name, gen):
        self.name, self.call = name, case.recorded(name, gen)

    def __call__(self, *a, **k):
        return self.call(*a, **k)

    def __repr__(self):
        return f"<{self.name}>"


# ------------------------------------------------------------------------------------------------ repos
GEN_PATHS = [["a/b.tsv"], ["x", "y/*.json"], [], None, ["/abs"], [""], [1], "str", ["ok", ""]]
GEN_REGEN = [None, None, "make gen", "  ", "", 5, "scripts/regen --flag 'a b'"]


def a_generated(r):
    g = {}
    for key, gen in (("paths", lambda: r.choice(GEN_PATHS)), ("regen", lambda: r.choice(GEN_REGEN)),
                     ("timeout", lambda: r.choice([None, 5, 0, -1, 2.5, "9", True])),
                     ("merge", lambda: r.choice(["rows", "regen", "derived", "bogus", None])),
                     ("block", lambda: r.choice([["# begin", "# end"], ["a"], "x", [1, 2], [], None, ["a", "b", "c"]])),
                     ("order_sensitive", lambda: r.choice([True, False, 1, "yes"])),
                     ("holds", lambda: r.choice([True, []]))):
        if r.random() < (0.9 if key in ("paths",) else 0.35 if key != "holds" else 0.05):
            g[key] = gen()
    return g


class GeneratedParseTest(unittest.TestCase):
    def build(self, case):
        r = case.rng
        raw = r.choice([None, [], [a_generated(r) for _ in range(r.randint(1, 3))]])
        if raw and r.random() < 0.05:
            raw.append(None)
        return ("o/r", raw)

    def test_generated_parse(self):
        check(self, "generated_parse", self.build, lambda A: A.generated_parse, 4000)


def a_repo_entry(r):
    x = {}
    for key, gen in (("repo", lambda: r.choice(["o/n", "pawelkrupinski/movies", "bad", "a/b/c", 5, "x.y/z-w", ""])),
                     ("gate", lambda: r.choice([[["W", "w.yml"]], [["W"]], [["W", ""]], [], None, "s", [["W", "f"], ["X", "g"]], [("W", "f")], [["W", "f", "g"]]])),
                     ("push", lambda: r.choice(["land", "owner-only", "selfpush", "bogus", None, ""])),
                     ("clone", lambda: r.choice(["/home/c", "/x/y", None, ""])),
                     ("red_workflows", lambda: r.choice([True, False, 1, "y"])),
                     ("signed", lambda: r.choice([True, False, 0, None])),
                     ("family", lambda: r.choice(["f", None])), ("note", lambda: "n"),
                     ("generated", lambda: r.choice([None, [a_generated(r)], [a_generated(r), a_generated(r)]]))):
        if key == "repo" or r.random() < 0.6:
            x[key] = gen()
    return x


class RedReposParseTest(unittest.TestCase):
    def build(self, case):
        r = case.rng
        n = r.choice([0, 1, 1, 2, 3])
        entries = [a_repo_entry(r) for _ in range(n)]
        if r.random() < 0.3 and entries:
            entries.append(dict(entries[0]))
        if r.random() < 0.3:
            entries.append({"repo": "pawelkrupinski/movies", **{k: v for k, v in a_repo_entry(r).items() if k != "repo"}})
        return (r.choice([None, {}, {"repos": entries}, {"repos": entries}, {"repos": None}]),)

    def test_red_repos_parse(self):
        check(self, "red_repos_parse", self.build, lambda A: A.red_repos_parse, 6000)


# ------------------------------------------------------------------------------------------------ blameopen
def a_run(r, conclusion, n):
    run = {"number": n, "databaseId": 1000 + n, "status": r.choice(["completed"] * 49 + ["in_progress"]),
           "conclusion": conclusion, "headSha": r.choice(["a" * 40, "a" * 40, "b" * 40, "d" * 40, "e" * 40, "f" * 40, "g" * 40, None, "c" * 12])}
    if r.random() < 0.1:
        del run["headSha"]
    return run


def a_blame_runs(r):
    reds = ["failure", "failure", "failure", "cancelled", "timed_out", "skipped"]
    runs = [a_run(r, r.choice(reds), 90 - i) for i in range(r.choice([0, 1, 2, 3, 4]))]
    if r.random() < 0.08:  # a streak past the 30 runs the state keeps
        runs = [a_run(r, "failure", 90 - i) for i in range(r.choice([30, 31, 32]))]
    runs += [a_run(r, r.choice(["success", "success", "success", "failure", "cancelled"]), 80 - i) for i in range(r.choice([0, 1, 1, 2, 3]))]
    return runs


class BlameOpenTest(unittest.TestCase):
    def build(self, case):
        r = case.rng
        runs = r.choice([None, a_blame_runs(r), a_blame_runs(r), a_blame_runs(r), a_blame_runs(r)])
        ob = {"kind": r.choice(["test"] * 9 + ["infra"]), "red_kind": r.choice(["gate"] * 9 + ["outside"]),
              "runs_list": runs, "workflow": r.choice(["Main", "Identity takes", "Identity model convergence"]),
              "jobs": r.choice([None, ["takes-drift-1 (uk)"], ["leg (pl) / takes-drift-2 (pl)"], ["unit", "SomeSpec"], []]),
              "job": r.choice([None, "takes-drift-1 (x)", "j"]), "line": r.choice([None, "FooSpec failed in a/b/Bar.scala", "none"])}
        ok_sha = lambda: r.random() < 0.85
        case.fake("git_ok", lambda rng, repo, *a: rng.random() < 0.93)
        case.fake("remote", lambda rng: "origin")

        def git_(rng, repo, *a, **k):
            if "diff-tree" in a:
                return proc(rng, rng.choice([0, 0, 1]), "\n".join(rng.choice(["a/Bar.scala", "x.py", "FooSpec.scala", "y"]) for _ in range(rng.randint(0, 3))))
            if "log" in a:
                return proc(rng, 0, rng.choice(["A <a@x>", "dependabot <d@x>", "R <renovate@x>"]) + "\n")
            return proc(rng, 0, "")
        case.fake("git", git_)
        n = r.choice([0, 1, 2, 3, 3, 5, 5, 5, 200, 201, 205])

        def keys(rng, repo, base, head):
            return [{"sha": f"s{i}" * 4, "patch_id": f"p{i}", "subject": f"sub {i}", "at": i} for i in range(n)]
        case.fake("commit_keys", keys)
        case.fake("lander_records", lambda rng, store, o_r: [{"change": f"c{i}"} for i in range(rng.randint(0, 3))])
        case.fake("lander_index", lambda rng, recs: {"n": len(recs)})

        def of_commit(rng, idx, k):
            if rng.random() < 0.3:
                return None
            c = rng.choice(["c0", "c0", "c1", None])
            return rng.choice([{"change": c, "lander": {"worktree": "/w/x"}, "worktree": rng.choice([None, "/w/y"])},
                               {"change": c, "lander": rng.choice([None, {"worktree": "/w/z"}, {}])}])
        case.fake("lander_of_commit", of_commit)
        return (Probe(case, "store"), r.choice([None, ""] + ["/repo"] * 18), r.choice(["pawelkrupinski/movies", "o/other"]), ob, r.choice([None, "T0"]))

    def test_blame_open(self):
        check(self, "blame_open", self.build, lambda A: A.blame_open, 6000)


# ------------------------------------------------------------------------------------------------ inbox
LINES = ["WAITING abc123", "QUEUED q1", "LANDED deadbeef", "NOT LANDED", "WATCH RED x", "WATCH OK", "just a line", "", "  ",
         "agentctl: oops", "run w#12: red", "file f.py: bad", "info"]


class WithInboxTest(unittest.TestCase):
    def build(self, case):
        r = case.rng
        case.fake("inbox_write", lambda rng, *a, **k: None)
        script = []
        for _ in range(r.randint(0, 6)):
            stream = r.choice(["out", "out", "out", "err"])
            text = r.choice(LINES) + r.choice(["\n", "\n", "\n", ""]) + (r.choice(LINES) + "\n" if r.random() < 0.15 else "")
            script.append((stream, text))
        end = r.choice([("return", None), ("return", 0), ("return", 0), ("return", 1), ("return", 2), ("exit", None), ("exit", 0), ("exit", 0),
                        ("exit", 1), ("exit", 3), ("exit", 130), ("exit", 137), ("exit", 128), ("exit", "agentctl: no clone"),
                        ("exit", "plain message"), ("raise", RuntimeError("boom")), ("raise", ValueError("")), ("raise", KeyboardInterrupt())])
        a = types.SimpleNamespace(**r.choice([{}, {"inbox_cwd": "/w/x"}, {"inbox_cwd": None}]))
        return (r.choice(["land", "waiting wait", "red"]), (script, end), a)

    def test_with_inbox(self):
        def run(A):
            def scenario(command, plan, a):
                script, (how, value) = plan
                saved = sys.stdout, sys.stderr
                outer = io.StringIO()
                errors = io.StringIO()
                sys.stdout, sys.stderr = outer, errors

                def handler(arg):
                    for stream, text in script:
                        (sys.stdout if stream == "out" else sys.stderr).write(text)
                    if how == "return":
                        return value
                    if how == "exit":
                        raise SystemExit(value)
                    raise value
                try:
                    try:
                        result = ["ok", A.with_inbox(command, handler, a)]
                    except BaseException as e:
                        result = ["raised", type(e).__name__, str(e.code) if isinstance(e, SystemExit) else str(e)]
                    return [result, outer.getvalue(), errors.getvalue(), sys.stdout is outer, sys.stderr is errors]
                finally:
                    sys.stdout, sys.stderr = saved
            return lambda args: scenario(*args)
        check(self, "with_inbox", lambda case: (self.build(case),), run, 5000)


if __name__ == "__main__":
    unittest.main()
