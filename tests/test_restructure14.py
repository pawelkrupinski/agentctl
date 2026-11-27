"""RESTRUCTURE ROUND 14: each function split into named stages keeps its behaviour. The digests (fixtures/
restructure14-digests.json, recorded from the code BEFORE the split with `R4_RECORD=1`) cover thousands of seeded cases
each, collaborators faked (golden_cases.py): every call, its order and the result. Every case is bounded."""
import contextlib, io, os, sys, types, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402


def change_state_of(ok, via=None):
    """The LandState a drawn (landed, via) stands for: True landed, None unknown, any other falsy not landed."""
    return A.LandState(A.LANDED if ok else A.UNKNOWN if ok is None else A.NOT_LANDED, via=via if ok else None)
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
        case.fake("change_state", lambda rng, repo, head, commits=None: change_state_of(rng.choice([True, False, None, 0]), rng.choice([None, "via1"])))
        case.fake("landed_look_due", lambda rng, cid, t: rng.random() < 0.7)
        case.fake("already_landed", lambda rng, st, d, e, via: None)
        case.fake("close_item_landed", lambda rng, st, cid, why, via=None: rng.random() < 0.6)
        st = Probe(case, "st", items=lambda rng, kind, states=None: [i for i in items if states is None or i["state"] in states])
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
        case.fake("change_state", lambda rng, repo, head, commits=None: change_state_of(rng.choice([True, False, None]), "via1"))
        case.fake("ts", lambda rng: "T")
        case.fake("save_entry", lambda rng, d, e: None)
        case.fake("drop_entry", lambda rng, d, eid: None)
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


# ------------------------------------------------------------------------------------------------ conditions
def a_probe_result(r, ok=None):
    ok = r.choice([True, False, None]) if ok is None else ok
    p = A.ProbeResult(r.choice(["token", "disk_free", "route", "other"]), {"since": 1, "x": r.choice([1, 2])}, ok,
                      r.choice(["fine", "bad", "INVALID: no", "closed by hand: why", "x" * 400]), _made_by_run_probe=A._RUN_PROBE_TOKEN)
    p.at = NOW
    return p


class StableResult:
    """A probe result with a stable repr (run_probe's fake)."""

    def __init__(self, kind, ok, detail):
        self.kind, self.ok, self.detail, self.args = kind, ok, detail, {}

    def __repr__(self):
        return f"<result {self.kind} {self.ok} {self.detail}>"


def an_open_condition(r, i=0):
    d = {"kind": r.choice(["TOKEN", "DISK"]), "text": r.choice(["it is down", None, "t" * 300]), "key": f"k{i}",
         "probe": {"kind": r.choice(["token", "route", "disk_free"]), "args": {"a": 1}}, "fails": r.choice([None, 0, 2, 9, 20]),
         "next_check": r.choice([None, NOW - 5, NOW + 5, 0, NOW]), "escalate_after": r.choice([None, NOW - 1, NOW + 1000, NOW]),
         "opened_at": r.choice([None, NOW - 10 ** 4]), "host": r.choice(["server", "sync", None]), "cwd": r.choice([None, "/wt"])}
    if d["host"] is None:
        del d["host"]
    return {"id": f"c{i}", "version": 2, "state": r.choice(["open", "open", "open", "closed"]), "deadline": r.choice([NOW - 3, NOW + 3, NOW]),
            "level": r.choice([0, 1]), "data": d}


class RecoveryTickTest(unittest.TestCase):
    def build(self, case):
        r = case.rng
        case.fake("host_role", lambda rng: r.choice(["server", "sync"]))
        case.put("epoch_now", lambda: NOW)
        case.put("err", lambda line: None)
        case.fake("migrate_condition_files", lambda rng, st: raising(rng, 0.2, ["migrated x"], SystemExit))
        items = [an_open_condition(r, i) for i in range(r.randint(0, 4))]
        case.fake("run_probe", lambda rng, kind, args: StableResult(kind, rng.choice([True, False, None]), "d"))
        case.fake("close_condition", lambda rng, c, res, store=None: rng.choice([None, "RECOVERED " + c["key"]]))
        case.fake("env_num", lambda rng, n, d: rng.choice([d, 7, 10 ** 9]))

        def update(rng, kind, iid, version, data):
            if rng.random() < 0.2:
                raise A.StaleVersion("stale")
        st = Probe(case, "st", items=lambda rng, kind, states=None: items, update_item=update,
                   item=lambda rng, kind, iid: next((i for i in items if i["id"] == iid), None),
                   transition=lambda rng, *a, **k: raising(rng, 0.2, None, A.StaleVersion))
        return (st, r.choice([None, NOW]), r.choice([False, True]))

    def test_recovery_tick(self):
        check(self, "recovery_tick", self.build, lambda A: A.recovery_tick, 8000)


class CloseConditionTest(unittest.TestCase):
    def build(self, case):
        r = case.rng
        case.fake("ts", lambda rng: "T")
        case.fake("mark_gate_dirty", lambda rng: None)
        case.fake("err", lambda rng, line: None)
        case.fake("inbox_write", lambda rng, *a, **k: None)
        import tempfile, shutil
        root = tempfile.mkdtemp(prefix="r14-")
        self.addCleanup(shutil.rmtree, root, True)
        case.put("inbox_dir", lambda: root)

        def action(name):
            def run(rng, cond, a, st):
                x = rng.random()
                if x < 0.2:
                    raise RuntimeError("no " * 50)
                if x < 0.3:
                    raise SystemExit(2)
                return rng.choice([None, "did " + name, ""])
            return run
        acts = {n: case.recorded("act." + n, action(n)) for n in ("a1", "a2")}
        case.put("RECOVER_ACTIONS", acts)
        d = {"inbox": r.sample(["e1", "e2", "e3"], r.randint(0, 3)), "on_recover": [[n, {}] for n in r.sample(["a1", "a2", "gone"] if False else ["a1", "a2"], r.randint(0, 2))]}
        item = r.choice([None, {"id": "c1", "version": 3, "state": r.choice(["open", "open", "closed"]), "data": d}])
        st = Probe(case, "st", item=lambda rng, kind, iid: item,
                   transition=lambda rng, *a, **k: raising(rng, 0.2, None, r.choice([A.StaleVersion, A.TerminalState])))
        cond = {"id": "c1", "kind": "TOKEN", "key": "k", "text": r.choice(["", "x" * 300, "it"]), "until": "token present",
                "probe": {"kind": "token", "args": {"a": 1}}}
        result = r.choice([a_probe_result(r), "gone fine", "INVALID: wrong", "closed by hand: me"])
        self.root = root
        return (cond, result, st)

    def test_close_condition(self):
        def run(A):
            def go(cond, result, st):
                try:
                    out = ["ok", A.close_condition(cond, result, st)]
                except BaseException as e:  # noqa: BLE001
                    out = ["raised", type(e).__name__]
                acked = os.path.join(self.root, "acked")
                return out, sorted(os.listdir(acked)) if os.path.isdir(acked) else None
            return go

        def build(case):
            args = self.build(case)
            import shutil
            shutil.rmtree(os.path.join(self.root, "acked"), True)
            return args
        check(self, "close_condition", build, run, 3000)


class OpenConditionTest(unittest.TestCase):
    def build(self, case):
        r = case.rng
        case.put("condition_key", lambda kind, probe: "ck")
        cur = maybe(r, {"id": "ck", "version": 4, "data": {"on_recover": [["a1", {"p": 1}]]}}, 0.4)
        case.fake("condition_item_of", lambda rng, st, key: cur)
        case.fake("condition_grace", lambda rng, kind: rng.choice([0, 300]))
        case.fake("condition_pending", lambda rng, st, key, grace, t: rng.random() < 0.3)
        case.fake("probe_words", lambda rng, kind, args: "words")
        case.fake("condition_dict", lambda rng, it: {"dict": it["id"], "state": it.get("state")})
        case.fake("inbox_write", lambda rng, *a, **k: raising(rng, 0.1, rng.choice([None, "eid1"]), SystemExit))
        case.fake("host_role", lambda rng: "server")
        case.fake("ts", lambda rng: "T")
        case.put("RECOVER_ACTIONS", {"a1": None, "a2": None})
        existing = {"ck": 0, "ck.2": 0}
        taken = r.sample(list(existing), r.randint(0, 2))

        def item(rng, kind, iid):
            return {"id": iid, "state": "closed"} if iid in taken or iid == "ck" and cur else None
        st = Probe(case, "st", item=item, clock=lambda rng: NOW, update_item=lambda rng, *a: None,
                   put_item=lambda rng, kind, iid, state, data: {"id": iid, "state": state})
        probe = a_probe_result(r) if r.random() < 0.9 else "notprobe"
        recover = r.choice([[], [("a1", {"p": 1})], [("a2", {"z": 1}), ("a1", {"p": 1})], [("nope", {})]])
        effect = r.choice([None, lambda: {"e": 1}, lambda: None, lambda: {}])
        text = r.choice(["plain", lambda extra: f"called {sorted(extra)}"])
        kwargs = dict(key=r.choice([None, "ck", "custom"]), inbox=r.choice([True, False]), command=r.choice(["recover", "land"]),
                      exit_code=r.choice([1, 0]), cwd=r.choice([None, "/wt"]), tail=r.choice([(), ("l1",)]), effect=effect,
                      grace=r.choice([None, 0, 5]))
        return (r.choice(["TOKEN", "OTHER"]), text, probe, recover, kwargs, st)

    def test_open_condition(self):
        def run(A):
            def go(kind, text, probe, recover, kwargs, st):
                try:
                    out = ["ok", A.open_condition(kind, text, probe, recover, store=st, **kwargs), A._OPENING[0]]
                except BaseException as e:  # noqa: BLE001
                    out = ["raised", type(e).__name__, A._OPENING[0]]
                return out
            return go
        check(self, "open_condition", self.build, run, 6000)


class CmdRecoverTest(unittest.TestCase):
    def build(self, case):
        r = case.rng
        conds = [{"kind": "K", "key": f"k{i}", "opened": "2026-11-01", "level": r.choice([0, 1]), "text": r.choice(["t", "x" * 200, None]),
                  "probe": {"kind": "token", "args": {"since": 5, "a": i}}, "until": "ok",
                  "last_checked": r.choice([None, NOW - 50]), "last_result": r.choice([None, {"ok": None, "detail": "d"}, {"ok": False, "detail": "e"}]),
                  "next_check": r.choice([None, NOW + 40, NOW - 40]), "fails": r.choice([None, 0, 3]),
                  "on_recover": r.choice([None, [], [("a1", {}), ("a2", {})]])} for i in range(r.randint(0, 3))]
        case.fake("open_conditions", lambda rng, st: conds)
        case.fake("recovery_tick", lambda rng, st, force=False: r.choice([[], ["L1", "L2"]]))
        case.fake("epoch_now", lambda rng: NOW)
        return (types.SimpleNamespace(rcmd=r.choice(["tick", "list", None])), Probe(case, "st"))

    def test_cmd_recover(self):
        def run(A):
            def go(a, st):
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    rc = A.cmd_recover(a, st)
                return rc, out.getvalue()
            return go
        check(self, "cmd_recover", self.build, run, 3000)


# ------------------------------------------------------------------------------------------------ gov
class GovFitsTest(unittest.TestCase):
    def build(self, case):
        r = case.rng
        case.put("wall_epoch", lambda: NOW)
        case.fake("env_num", lambda rng, n, d: rng.choice([d, d, d, 0, 1, 2, 3, 6, 10 ** 9]))
        case.fake("checks_cap", lambda rng: rng.choice([0, 1, 2, 4]))
        case.fake("gov_budget", lambda rng: rng.choice([4.0, 10.0, 16.0, 40.0]))
        case.fake("gov_capacity", lambda rng, tickets, m: {"state": rng.choice(["starved", "ok", "ok", "spare"]), "busy": rng.choice([0, 3.5, 9.0]),
                                                           "cores": rng.choice([4, 8, 16]), "charged": rng.choice([0, 0, 2.0, 6.5]), "measured": rng.choice([0.5, 7.25])})
        tickets = [{"class": r.choice(["suite", "agent", "red", "check"]), "kind": r.choice(["job", "check", "check"]), "mem": r.choice([0, 1, 2.5, 4, None, "3"]),
                    "at": r.choice([NOW - 10, NOW - 119, NOW - 120, NOW - 500])} for _ in range(r.randint(0, 5))]
        m = {}
        for k, gen in (("free", lambda: r.choice([0.5, 2.0, 3.0, 8.0, 40.0])), ("pressure", lambda: r.choice([10, 74, 75, 76, 90.0])),
                       ("laptop", lambda: r.choice(["", "swapping", None]))):
            if r.random() < 0.7:
                m[k] = gen()
        return (r.choice(["agent", "agent", "suite", "red", "fixer", "check"]), r.choice([0, 1, 2.5, 4.0, 8.0, 30.0]), tickets, m,
                r.choice(["job", "check"]), r.choice([0.0, 0.0, 1.0, 4.0, 12.0]))

    def test_gov_fits(self):
        check(self, "gov_fits", self.build, lambda A: A.gov_fits, 12000)


# ------------------------------------------------------------------------------------------------ orphanreap
class OwnerWorkTest(unittest.TestCase):
    def build(self, case):
        r = case.rng
        rec = {}
        for k, gen in (("pid", lambda: r.choice([None, 100, 100, "100", 100, 200])), ("ended", lambda: r.choice([None, None, None, False, True])),
                       ("verdict", lambda: r.choice([None, "IDLE: " + "x" * 200])), ("process_start", lambda: "ps1"),
                       ("in_turn", lambda: r.choice([True, True, True, False, None])), ("bg_id", lambda: r.choice([None, "", "bg1"])),
                       ("progress_at", lambda: r.choice([None, 0, NOW - 100, NOW - 10 ** 5, NOW - 400, NOW - 900, NOW - 1200, NOW - 300, NOW - A.RED_STALL - 300])), ("transcript", lambda: r.choice([None, "/t"])),
                       ("turn", lambda: r.choice([None, 3]))):
            if r.random() < 0.9 or k == "pid":
                rec[k] = gen()
        case.fake("owner_record_path", lambda rng, fam: "/owner.json")
        case.fake("read_record", lambda rng, p: rng.choice([rec] * 9 + [None]))
        case.fake("pid_alive", lambda rng, pid: rng.random() < 0.5)
        case.fake("record_alive", lambda rng, rec: rng.random() < 0.85)
        case.fake("mtime", lambda rng, p: rng.choice([0, NOW - 50, NOW - 10 ** 6]))
        case.put("wall_epoch", lambda: NOW)  # a file's age is read on the wall: pinned to the case's NOW, never today's date
        case.fake("env_num", lambda rng, n, d: rng.choice([d, d, 0, 10 ** 9]))
        case.fake("claude_cli", lambda rng: "claude")
        case.fake("bg_session", lambda rng, cli, bid: rng.choice([{}, None, {"state": "idle"}, {"state": None}, {"state": "running"}]))
        case.fake("bg_turn_over", lambda rng, ent, tr: rng.random() < 0.5)
        return ("fam", NOW, r.choice([None, None, 100, "100", 300, 0]))

    def test_owner_work(self):
        check(self, "owner_work", self.build, lambda A: A.owner_work, 10000)


if __name__ == "__main__":
    unittest.main()
