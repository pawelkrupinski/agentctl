"""RESTRUCTURE ROUND 10: each function split into named stages keeps its behaviour. The digests (fixtures/
restructure10-digests.json, recorded from the code BEFORE the split with `R4_RECORD=1`) cover thousands of seeded cases
each, collaborators faked (golden_cases.py): every call, its order and the result."""
import builtins, contextlib, io, os, sys, types, unittest
from functools import partial

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402
from harness import case_context  # noqa: E402
from golden_cases import Probe, cherry_state, check as _check  # noqa: E402

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


# ------------------------------------------------------------------------------------------------ waiting
class ApplyEntryTest(unittest.TestCase):
    def build(self, case):
        r = case.rng
        commits = [f"c{i}" * 6 for i in range(r.randint(0, 4))]
        e = {"id": "e1", "commits": list(commits)}
        for key, value in (("autoresolved", ["old"]), ("regenerated", ["old"]), ("provisional", ["old"]), ("dropped", [["x", "y"]])):
            if r.random() < 0.25:
                e[key] = value
        autoresolve = r.choice([False, True, True])
        via = lambda rng: rng.choice([None, "v1" * 6, "v2" * 6, "v3" * 6])

        def pick(rng, wt, cs, dropped, regen, regenerated, provisional, resolved):
            for c in cs:
                if rng.random() < 0.3:
                    dropped.append((c, via(rng)))
            for _ in range(rng.choice([0, 0, 1, 2])):
                regenerated.append(rng.choice(["b.tsv", "a.tsv", "c.html"]))
            for _ in range(rng.choice([0, 0, 1, 2, 2])):
                provisional.append(rng.choice(["p.tsv", "o.tsv", "p.tsv"]))
            if resolved is not None:
                for _ in range(rng.choice([0, 1, 2])):
                    resolved.append({"file": rng.choice(["f.py", "g.py"]), "hunks": [rng.choice(["1-2", "5"]) for _ in range(rng.randint(1, 2))]})
            if rng.random() < 0.2:
                dropped.append(("extra" * 3, via(rng)))
            return rng.choice([None, None, None, ["f.py"], ["f.py", "g.tsv (generated: no regen)"]])
        case.fake("pick", pick)
        case.fake("git_ok", lambda rng, repo, *a: rng.choice([0, 1]) == 0)  # the ancestor test, once drawn from `git`'s fake

        def git_(rng, repo, *a, **k):
            if "rev-parse" in a:
                return rng.choice(["base0", "other", "base0"]) + "\n"
            return proc(rng, rng.choice([0, 1]), "")
        case.fake("git", git_)
        case.fake("wlog", lambda rng, d, line: None)
        case.fake("save_entry", lambda rng, d, entry: None)
        case.fake("remote", lambda rng: "origin")
        return (None, "/d", "/wt", e, "base0", r.choice([True, False]), autoresolve)

    def test_apply_entry(self):
        def run(A):
            def apply(*args):
                result = A.apply_entry(*args)
                return result, args[3]
            return apply
        check(self, "apply_entry", self.build, run, 6000)


# ------------------------------------------------------------------------------------------------ queueenter
def printing(holder, fn):
    """fn with its stdout in the case's log (the case is holder["case"])."""
    def run(*args):
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out):
                return fn(*args)
        finally:
            holder["case"].log.append(["output", out.getvalue()])
    return run


class SyncOntoMainTest(unittest.TestCase):
    def build(self, case):
        r = case.rng
        holder = {"case": case}
        for name, gen in (("remote", lambda rng: "origin"), ("print_error", lambda rng, m: None), ("branch_of", lambda rng, repo: "br"),
                          ("waiting_commits", lambda rng, repo, base, head: ["w1", "w2"][:rng.randint(0, 2)]),
                          ("blame_records_quiet", lambda rng, st: ["rec"]),
                          ("land_before_queue", lambda rng, repo, head: rng.choice([head, head + "+"])),
                          ("env_number", lambda rng, name, default: default),
                          ("landed_state", lambda rng, repo, main_ref, head: A.LandState(A.LANDED if rng.random() < 0.12 else A.NOT_LANDED)),
                          ("do_not_land", lambda rng, repo, commits, branch: maybe(rng, "marked", 0.1)),
                          ("held_commit", lambda rng, repo, commits: maybe(rng, "held", 0.1)),
                          ("blame_land_refusal", lambda rng, *a, **k: maybe(rng, "dead blame", 0.1)),
                          ("conflict_resolve", lambda rng, repo: rng.random() < 0.6),
                          ("fix_replay_blocks", lambda rng, repo, head, store, known: rng.random() < 0.1),
                          ("check_range", lambda rng, *a: rng.choice([[], [], ["p1"], ["p1", "p2"]]))):
            case.fake(name, gen)
        case.fake("git", lambda rng, repo, *a, **k: proc(rng, 0, rng.choice(["f.py g.py", "f.py", ""]) if "diff" in a else "h" + str(rng.randint(1, 3)) + "\n")
                  if k.get("check") is False else "h" + str(rng.randint(1, 3)) + "\n")
        bad = r.choice([0.0, 0.2, 0.5])

        def sh(*argv, **k):
            return proc(r, 1 if r.random() < bad else 0, "", r.choice(["", "fatal: x", "e" * 400]))
        sh_probe = Named(case, "sh", lambda rng, *a, **k: sh(*a, **k))
        return ((r.choice(["/repo"]), "origin/main", sh_probe, Probe(case, "clock"), Probe(case, "store"), {}, r.choice([False, True])), holder)

    def test_sync_onto_main(self):
        def run(A):
            return lambda args, holder: printing(holder, A.sync_onto_main)(*args)
        check(self, "sync_onto_main", self.build, run, 6000)


class LandUntilDoneTest(unittest.TestCase):
    STATES = ["landed", "rejected", "queued", "running", "waiting", "cancelled", "superseded"]

    def build(self, case):
        r = case.rng
        holder = {"case": case}
        item = lambda rng: maybe(rng, {"id": "e9", "state": rng.choice(self.STATES), "data": rng.choice([None, {}, {"why": "boom"}, {"why": "NOT LANDED: x"}, {"why": "NOT DONE: z"}])}, 0.7)
        existing = item(r)
        case.fake("git", lambda rng, repo, *a, **k: "h" + str(rng.randint(1, 3)) + "abcdefghijkl\n")
        case.fake("git_ok", lambda rng, *a, **k: rng.random() < 0.7)
        case.fake("remote", lambda rng: "origin")
        case.fake("env_number", lambda rng, name, default: default)
        case.fake("fmt_age", lambda rng, secs: f"{int(secs)}s")
        case.fake("own_change_item", lambda rng, repo, head, store: existing)
        case.fake("verdict_still_holds", lambda rng, repo, main_ref, it: rng.random() < 0.5)
        case.fake("take_delivery", lambda rng, it, store: None)
        case.fake("next_command", lambda rng, repo, why: f"NEXT {why}")
        case.fake("machine_state", lambda rng, typ, state: types.SimpleNamespace(terminal=state in ("landed", "rejected", "cancelled", "superseded")))
        case.fake("landed_state", lambda rng, repo, main, head: cherry_state(A, rng.choice([(None, None), ([], []), (["c"], []), (["c"], ["d"])])))
        case.fake("until_done_finish", lambda rng, *a: rng.choice([0, 1]))

        def enqueue(rng, repo, main_ref, sh, a, clock, store):
            a.queued_id = rng.choice([None, "e5", "e5"])
            return rng.choice([0, 0, 1])
        case.fake("enqueue", enqueue)
        case.fake("await_verdict", lambda rng, eid, bound, clock, store: (rng.choice(["landed", "rejected", "queued", "running"]),
                                                                            rng.choice([{}, {"landed_sha": "s1"}, {"why": "w"}, {"why": "NOT LANDED: w"}]), 1.0))
        case.fake("landed_sha_of", lambda rng, eid, data: "sha-of")
        case.fake("change_decided", lambda rng, state: state in ("rejected", "cancelled"))
        clock = Probe(case, "clock", time=lambda rng: 1000.0 + rng.randint(0, 5000))
        store = Probe(case, "store", item=lambda rng, typ, eid: maybe(rng, {"id": eid}, 0.5))
        a = types.SimpleNamespace(deadline=r.choice([None, 0, 600]))
        return ("/repo", "origin/main", None, a, clock, store), holder

    def test_land_until_done(self):
        def run(A):
            def go(args, holder):
                result = printing(holder, partial(A.land_until_done, ctx=case_context()))(*args)
                holder["case"].log.append(["a", sorted(vars(args[3]).items())])
                return result
            return go
        check(self, "land_until_done", self.build, run, 6000)


# ------------------------------------------------------------------------------------------------ queuebatch
def an_entry(r, i):
    return {"id": f"e{i}", "seq": r.choice([i, i, 10 - i, 3]), "status": r.choice(["queued", "queued", "waiting", "checking", "checked", "pushing", "joined"]),
            "worktree": r.choice(["/w/a", "/w/b", None]), "branch": r.choice(["b1", "b2", None, ""]),
            "backlog_batch": r.choice([None, None, None, "bb"]), "batch_alone": r.choice([None, None, "regen", "resolve", "x"]),
            "cycle_ignore": r.choice([None, [], ["e1"]]), "conflict_told": r.choice([None, None, "e1", "e2"])}


class WaitingView:
    """A queue's records reading `waiting` (waitingdir.ChangeRecords' reader), with a stable repr for the digest."""

    def __init__(self, waiting):
        self.list = waiting

    def waiting(self):
        return list(self.list)

    def __repr__(self):
        return f"WaitingView({self.list!r})"


class OverlapHoldTest(unittest.TestCase):
    def build(self, case):
        r = case.rng
        entries = [an_entry(r, i) for i in range(r.randint(0, 5))]
        e = an_entry(r, 9) if r.random() < 0.8 else dict(entries[0] if entries else an_entry(r, 8))
        if r.random() < 0.3:
            e["id"] = "e1"
        waiting = [an_entry(r, i) for i in range(r.randint(0, 3))]
        case.fake("ChangeRecords", lambda rng, st, d: WaitingView(waiting))
        case.fake("overlap_candidate", lambda rng, o, e_, stacks: rng.random() < 0.7)
        case.fake("as_own", lambda rng, x, stacks: {"own": x["id"]})

        def overlap_of(rng, repo, theirs, mine, strict=False):
            shared = rng.choice([[], ["a.tsv"], ["a.tsv", "b.py"]])
            return shared, rng.choice([None, None, ["c.py"]]), rng.choice([None, None, ["r.py"], []])
        case.fake("overlap_of", overlap_of)
        case.fake("ordered_overlap", lambda rng, repo, theirs, mine: rng.choice([[], [], ["o.tsv"]]))
        case.fake("backlog_batchable", lambda rng, st, repo, d, x, t, facts: rng.random() < 0.6)
        return (Probe(case, "st"), "/repo", "/d", e, entries, {"f": 1}, 5.0, r.choice([None, {}, {"e1": "e2"}]))

    def test_overlap_hold(self):
        check(self, "overlap_hold", self.build, lambda A: partial(A.overlap_hold, ctx=case_context()), 6000)


class HoldForOverlapTest(unittest.TestCase):
    def build(self, case):
        r = case.rng
        case.put("_behind_said", A.process_cache({}) if r.random() < 0.5 else {("e9", "overlap"): r.choice(["e1", "e2"])})
        case.fake("remote", lambda rng: "origin")
        case.fake("conflict_hunks", lambda rng, repo, o, e, conflict: rng.choice([[], ["h1 a.py:3"], ["h1", "h2"]]))
        case.fake("machine_state", lambda rng, typ, state: types.SimpleNamespace(terminal=state in ("done", "landed")))
        case.fake("save_entry", lambda rng, d, e: None)
        case.fake("wlog", lambda rng, d, line: None)

        def transition(rng, *a, **k):
            if rng.random() < 0.25:
                raise A.StaleVersion("stale")
        e = {"id": "e9", "worktree": r.choice(["/w/a", None]), "conflict_told": r.choice([None, "e1", "e2"])}
        o = {"id": r.choice(["e1", "e2"]), "worktree": r.choice(["/w/o", None]), "branch": r.choice(["ob", None, ""]), "status": r.choice(["queued", "checking"])}
        st = Probe(case, "st", item=lambda rng, typ, eid: maybe(rng, {"id": eid, "state": rng.choice(["queued", "done", "waiting"]), "version": 3}, 0.8),
                   transition=transition)
        return (st, "/d", e, o, r.choice([["a.tsv"], ["a.tsv", "b.tsv"], ["x"]]), r.choice([None, None, ["c.py"], ["c.py", "d.py", "e.py", "f.py"]]),
                r.choice([True, False]), r.choice([None, "/repo"]))

    def test_hold_for_overlap(self):
        def run(A):
            def go(*args):
                result = A.hold_for_overlap(*args)
                return result, args[2], dict(A._behind_said)
            return go
        check(self, "hold_for_overlap", self.build, run, 6000)


if __name__ == "__main__":
    unittest.main()
