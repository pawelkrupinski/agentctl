"""RESTRUCTURE ROUND 14: each function split into named stages keeps its behaviour. The digests (fixtures/
restructure14-digests.json, recorded from the code BEFORE the split with `R4_RECORD=1`) cover thousands of seeded cases
each, collaborators faked (golden_cases.py): every call, its order and the result. Every case is bounded."""
import contextlib, io, os, sys, types, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402
from golden_cases import Probe, check as _check  # noqa: E402

DIGESTS = os.path.join(HERE, "fixtures", "restructure14-digests.json")
NOW = 1_795_000_000.0


def check(test, name, build, fn_of, n=3000):
    _check(test, A, name, build, fn_of, n, DIGESTS)


def maybe(rng, value, p=0.5):
    return value if rng.random() < p else None


def raising(rng, p, value, exc=RuntimeError):
    if rng.random() < p:
        raise exc("down")
    return value


def proc(rng, code=0, out=""):
    return types.SimpleNamespace(returncode=code, stdout=out, stderr="")


class Quiet(contextlib.nullcontext):
    def __repr__(self):
        return "<quiet>"


# ------------------------------------------------------------------------------------------------ redauthors
class RedAuthorsTest(unittest.TestCase):
    def build(self, case):
        r = case.rng
        shas = [f"{r.randint(0, 9)}{c}{c}{c}{c}" for c in "abcdefg"][:r.randint(0, 7)]
        case.fake("red_commits", lambda rng, repo, u: list(shas))
        wts = [f"/wt/{i}" for i in range(4)]

        def heads(rng, repo):
            out = {}
            for s in shas:
                if rng.random() < 0.3:
                    out[rng.choice([s, s[:3], s + "x"])] = {"worktree": rng.choice(wts + [None, "/not/allowed"]),
                                                            "session": rng.choice([None, "s1", "s2"])}
            return out
        case.fake("landed_heads", heads)
        case.fake("worktree_holding", lambda rng, repo, c: maybe(rng, rng.choice(wts), 0.5))
        case.fake("allowed_path", lambda rng, p: p != "/not/allowed" and rng.random() < 0.9)
        case.patch(os.path, "isdir", lambda rng, p: rng.random() < 0.9)
        entries = [{"at": r.choice([0, NOW - 10, NOW - 10 ** 5]), "cwd": r.choice(wts + [None])} for _ in range(r.randint(0, 3))]
        case.fake("inbox_entries", lambda rng: raising(rng, 0.1, entries, OSError))
        case.fake("agent_pids_in", lambda rng, ws: {w: rng.sample([11, 12, 13], rng.randint(0, 2)) for w in ws if rng.random() < 0.7})
        case.fake("env_num", lambda rng, n, d: rng.choice([d, 0, 10 ** 9]))
        case.fake("worktree_activity", lambda rng, repo, wt, ents: rng.choice([0.0, NOW - 60, NOW - 900, NOW, NOW - 10 ** 5]))
        case.fake("proc_alive", lambda rng, p: rng.random() < 0.6)
        case.put("wall_epoch", lambda: NOW)
        return ("/repo" if r.random() < 0.95 else "", {"run": {}}, r.choice([None, NOW, NOW + 100]))

    def test_red_authors(self):
        check(self, "red_authors", self.build, lambda A: A.red_authors, 6000)


# ------------------------------------------------------------------------------------------------ redverify
class FixBetweenTest(unittest.TestCase):
    def build(self, case):
        r = case.rng
        causes = {"v1": {"c1"}, "v2": {"c2"}}
        workflows = {"v1": "W", "v3": "W", "v4": "Other"}
        cmd = r.choice([0] * 9 + [1])
        chunks = []
        for i in range(r.randint(0, 4)):
            vals = ",".join(r.sample(["v1", "v2", "v3", "v4", "7"], r.randint(0, 3)))
            files = [r.choice(["a/b.py", "x.py", ".github/workflows/w.yml", ".github/actions/a", "doc.md", "lib/c.py", "b.py", "c.py"]) for _ in range(r.randint(0, 3))]
            chunks.append(f"\0{'s' + str(i)}\t{r.choice(['', 'x', ' '])}\t{vals}\n" + "\n".join(files))
        out = "".join(chunks) + r.choice(["", "\0"])
        case.fake("git", lambda rng, *a, **k: proc(rng, cmd, out))
        rec = {"runs": r.sample([1, "2", 3, "v4"], r.randint(0, 3)), "job": r.choice([None, "j1", "Build"]),
               "line": r.choice([None, "fail in a/b.py:3", "boom x.py", "c1 failed", "no path"]), "workflow": r.choice(["W", "Other", None])}
        causes = r.choice([causes, {}])
        heads = ["h1", "h2", "h2", "h2", "h3", "h1", None, ""]
        return (r.choice(["/repo"] * 9 + ["", None]), r.choice([rec] * 9 + [{}]), r.choice(heads), r.choice(heads), causes, workflows)

    def test_fix_between(self):
        check(self, "fix_between", self.build, lambda A: A.fix_between, 8000)


def an_item(r, state=None):
    d = {"gate": r.choice([False] * 8 + [True, None]), "fix": r.choice([None] * 5 + [{}, {"status": "landed"}, {"status": "x"}]),
         "main_verify": r.choice([None, {}, {"at": NOW - 10, "main": "m1"}, {"at": NOW - 10 ** 6, "main": "m1"}, {"at": NOW - 10 ** 6}, {"at": NOW - 10800},
                                  {"at": NOW - 10 ** 6}, {"at": NOW - 10 ** 6}]),
         "last_run": r.choice([None, {}, {"headSha": "h1", "databaseId": 5}, {"headSha": "m1"}, {"headSha": "h1"}, {"headSha": "h1"}]),
         "workflow": r.choice(["W", None]), "key": "k"}
    return {"id": "r1", "version": 3, "state": state or r.choice(["offered", "leased", "fix-pending", "blocked-external"] * 3 + ["open", "verified"]), "data": d}


class RedMainVerifyTest(unittest.TestCase):
    def build(self, case):
        r = case.rng
        case.fake("env_num", lambda rng, n, d: rng.choice([d, 0, 10 ** 9]))
        case.fake("remote", lambda rng: "origin")
        mains = ["m1", "m2", "h1", ""]

        def git(rng, repo, *args, **k):
            if args[0] == "rev-parse":
                return proc(rng, rng.choice([0, 0, 1]), rng.choice(mains) + "\n")
            return proc(rng, rng.choice([0, 0, 1]), " ".join(rng.sample(["s1", "s2", "s3"], rng.randint(0, 3))))
        case.fake("git", git)
        case.fake("fix_reaches_red", lambda rng, repo, x, d: rng.random() < 0.4)
        case.fake("red_verify_dispatch", lambda rng, o_r, repo, wf, main, run: "dispatched " + str(main))
        case.fake("red_what", lambda rng, d: "RED " + str(d.get("key")))
        case.fake("inbox_write", lambda rng, *a, **k: None)
        return (Probe(case, "st"), an_item(r), r.choice(["/repo"] * 9 + ["", None]), "o/r", NOW)

    def test_red_main_verify(self):
        check(self, "red_main_verify", self.build, lambda A: A.red_main_verify, 8000)


class RedFlakeCheckTest(unittest.TestCase):
    def build(self, case):
        r = case.rng
        case.fake("fix_between", lambda rng, repo, rec, rh, gh, causes, wfs: (rng.choice([None, None, "sha1"]), rng.choice([True, True, False])))
        case.fake("name_causes", lambda rng, a, vals: {"v": list(vals)})
        case.fake("run_workflows", lambda rng: {"1": "W"})
        case.fake("red_open", lambda rng, store, ob, x, now: maybe(rng, {"id": "flake1", "ob": sorted(ob)}, 0.6))
        case.fake("red_what", lambda rng, d: "RED " + str(d.get("key")))
        case.fake("inbox_write", lambda rng, *a, **k: None)
        d = {"kind": r.choice(["test"] * 6 + ["build"]), "red_kind": r.choice(["gate", "outside", "outside", "gate", "flake", None]), "workflow": "W",
             "runs": r.choice([None, [1, 2]]), "job": "j", "line": r.choice([None, "l"]), "key": "k", "jobs": ["j"],
             "last_run": r.choice([None, {"headSha": "h"}])}
        it = {"data": d}
        snap = r.choice([None, {}, {"latest": {"W": {"conclusion": "success", "headSha": "g", "number": 4}}}] * 4
                        + [{"latest": {"W": {"conclusion": "failure"}}}])
        facts = r.choice([{}, {"landed": []}, {"landed": [(1, 2, {"v1", "v2"}, 4), (1, 2, {"v3"}, 5)]}])
        st = Probe(case, "st", clock=lambda rng: NOW)
        return (st, it, snap, "/repo", facts)

    def test_red_flake_check(self):
        check(self, "red_flake_check", self.build, lambda A: A.red_flake_check, 8000)


# ------------------------------------------------------------------------------------------------ queueclose
def with_state(fn, *state_of):
    """fn's result (or its exception) with state_of(args) after it: what it left in the objects it was given."""
    def run(*args):
        try:
            result = ["ok", fn(*args)]
        except BaseException as e:  # noqa: BLE001 -- an exception IS an outcome
            result = ["raised", type(e).__name__, str(e)[:60]]
        return result, [get(*args) for get in state_of]
    return run


STATES = ["queued", "checking", "waiting", "landed", "pushing", "rejected", "cancelled"]


class CloseLandedChangesTest(unittest.TestCase):
    def build(self, case):
        r = case.rng
        entries = [{"id": f"e{i}", "status": r.choice(["waiting", "checking", "pushing", "landed", "rejected", "queued"]),
                    "head": f"h{i}", "commits": r.choice([None, ["c"]])} for i in range(r.randint(0, 4))]
        case.fake("waiting_entries", lambda rng, d, open_only=True: entries)
        items = [{"id": r.choice([f"e{i}", f"i{i}"]), "state": r.choice(STATES), "data": r.choice([None, {"head": f"h{i}"}, {}])} for i in range(r.randint(0, 5))]
        case.fake("change_landed", lambda rng, repo, head, commits=None: (rng.choice([True, False, None, 0]), rng.choice([None, "via1"])))
        case.fake("landed_look_due", lambda rng, cid, t: rng.random() < 0.7)
        case.fake("already_landed", lambda rng, st, d, e, via: None)
        case.fake("close_item_landed", lambda rng, st, cid, why, via=None: rng.random() < 0.6)
        st = Probe(case, "st", items=lambda rng, kind: items)
        clock = Probe(case, "clock", time=lambda rng: NOW)
        return ("/repo", clock, st, r.choice([None, "/d", ""]))

    def test_close_landed_changes(self):
        check(self, "close_landed_changes", self.build, lambda A: A.close_landed_changes, 8000)


def an_entry(r, i=0):
    since = r.choice([NOW - 10, NOW - 10 ** 6, NOW - 10 ** 6, NOW - 10 ** 6, NOW - 10 ** 6 - 5, NOW - A.CONFLICT_PLACE_KEPT])
    rec = r.choice([None, {}, {"at": NOW - 100, "round": 2}, {"at": NOW - 10 ** 5, "round": 1}, {"at": NOW - 10 ** 5, "round": 3},
                    {"at": NOW - 10 ** 5, "escalated": NOW - 5}, {"at": NOW - 10 ** 5}, {"at": NOW - 7200, "round": 1}])
    h = {"since": since, "files": r.choice([[], ["a.txt", "b.txt"]]), "cmd": r.choice([None, "regen x"])}
    if rec is not None:
        h["recovery"] = rec
    return {"id": f"e{i}", "worktree": "/wt", "regen_hold": r.choice([h, h, h, h, {}, None])}


class RegenHoldOverdueTest(unittest.TestCase):
    def build(self, case):
        r = case.rng
        case.fake("env_num", lambda rng, n, d: rng.choice([d, d, 10 ** 9, 0]))
        case.fake("save_entry", lambda rng, d, e: None)
        case.fake("wlog", lambda rng, d, text: None)
        item = {"state": r.choice(STATES), "version": 4, "data": r.choice([None, {}, {"regen_round": 2, "regen_answered": 2}, {"regen_answered": 0}, {"regen_round": "1", "regen_answered": 5}])}

        def get(rng, kind, cid):
            return rng.choice([item, item, item, None])

        def transition(rng, *a, **k):
            if rng.random() < 0.2:
                raise A.StaleVersion("stale")
        e = an_entry(r)
        st = Probe(case, "st", item=get, transition=transition, update_item=lambda rng, *a: raising(rng, 0.15, None, A.StaleVersion))
        return (st, "/d", e, NOW)

    def test_regen_hold_overdue(self):
        check(self, "regen_hold_overdue", self.build, lambda A: with_state(A.regen_hold_overdue, lambda st, d, e, t: e), 8000)


class LapChangeDeadlineTest(unittest.TestCase):
    def build(self, case):
        r = case.rng
        state = r.choice(["queued", "checking"])
        item = {"id": "c1", "state": state if r.random() < 0.9 else "queued", "deadline": r.choice([NOW - 100, NOW + 100, NOW]), "entered_at": NOW - 1000, "version": 2}

        def get(rng, kind, cid):
            return rng.choice([item, item, item, None])
        hold = r.choice([None, {}, {"why": "waiting for X"}, {"why": "w2"}])
        case.fake("change_hold_visible", lambda rng, st, it, e, t: hold)
        case.fake("change_hold_stuck", lambda rng, st, h, it, t: rng.choice([None, "no blocker", "too long"]))
        case.fake("stuck_recovery", lambda rng, hold, stuck: rng.choice(["rederive", "page", None]))
        case.fake("restart_hold", lambda rng, st, e, t: None)
        case.fake("file_unheld_change", lambda rng, st, it, t, who: rng.choice([None, 12]))
        st = Probe(case, "st", item=get, transition=lambda rng, *a, **k: raising(rng, 0.2, None, A.StaleVersion))
        return (st, {"id": "c1", "worktree": r.choice([None, "/wt"])}, state, NOW, r.choice([None, "last push"]))

    def test_lap_change_deadline(self):
        check(self, "lap_change_deadline", self.build, lambda A: A.lap_change_deadline, 8000)


class MigrateLocalQueueTest(unittest.TestCase):
    def build(self, case):
        r = case.rng
        case.fake("host_role", lambda rng: r.choice(["sync"] * 5 + ["server"]))
        case.patch(os.path, "isdir", lambda rng, p: rng.random() < 0.9)
        case.fake("waiting_state", lambda rng, repo: ("/d", 1, 2))
        entries = [{"id": f"e{i}", "status": r.choice(["waiting", "waiting", "landed", "queued"]), "head": f"h{i}", "worktree": r.choice([None, "/wt"]),
                    "migrate_told_at": r.choice([None, NOW - 10, NOW - 10 ** 5, NOW - 3600])} for i in range(r.randint(0, 4))]
        case.fake("waiting_entries", lambda rng, d, open_only=True: entries)
        case.fake("change_landed", lambda rng, repo, head, commits=None: (rng.choice([True, False, None]), "via1"))
        case.fake("ts", lambda rng: "T")
        case.fake("save_entry", lambda rng, d, e: None)
        case.fake("close_item_landed", lambda rng, st, cid, why, via=None: True)
        case.fake("submit_ref", lambda rng, repo, d, e, to_server=False: rng.random() < 0.7)
        case.fake("epoch_now", lambda rng: NOW)
        case.fake("inbox_write", lambda rng, *a, **k: None)
        case.fake("safe_cwd", lambda rng: "/safe")
        st = Probe(case, "st", submit=lambda rng, x: raising(rng, 0.25, None, r.choice([RuntimeError, SystemExit])))
        return ("/repo", st, entries)

    def test_migrate_local_queue(self):
        def run(A):
            def go(repo, st, entries):
                return with_state(A.migrate_local_queue, lambda repo, st: entries)(repo, st)
            return go
        check(self, "migrate_local_queue", self.build, run, 8000)


if __name__ == "__main__":
    unittest.main()
