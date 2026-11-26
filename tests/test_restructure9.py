"""RESTRUCTURE ROUND 9: each function split into named stages keeps its behaviour. The digests (fixtures/
restructure9-digests.json, recorded from the code BEFORE the split with `R4_RECORD=1`) cover thousands of seeded cases
each, collaborators faked (golden_cases.py): every call, its order and the result. The clock is the case's own, and
every case is bounded (a loop's clock moves with each sleep)."""
import datetime as dt, glob as glob_module, json, os, sys, types, unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402
from golden_cases import Probe, check as _check  # noqa: E402
from test_restructure8 import NOW, captured, fixed_time, maybe, raising  # noqa: E402

DIGESTS = os.path.join(HERE, "fixtures", "restructure9-digests.json")


def check(test, name, build, fn_of, n=2000):
    _check(test, A, name, build, fn_of, n, DIGESTS)


def iso(secs):
    return dt.datetime.fromtimestamp(secs, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ------------------------------------------------------------------------------------------------ alertroute
KINDS = ["BLOCKED", "RED_BUDGET", "UNLANDED_ORPHAN", "ORPHAN_SUBMITTED", "REVERT_NOTICE", "NOT_LANDED", "PRECHECK_REFUSED", "RED",
         "RED_AUTHOR", "FIX_FAILED", "LANDED", "SUPERSEDED", "FIXER_FAILED", "DIAGNOSED", "RED_DISPATCHED", "NETWORK", "RED_UNCLAIMED",
         "RED_STARVED", "RED_ESCALATE", "INVARIANT", "STALLED", "UNKNOWN", "ENDED", "USAGE", "OK", "WAITING", "QUEUED", "HELD", "OTHER",
         "RED_FLAKE", "FIX_UNVERIFIED", "REVERTED", None]
VERDICTS = ["", None, "INVARIANT W5 heartbeat gap", "INVARIANT W1 stuck", "INVARIANT", "RED: Main #2123 failed", "RED: core Main Pipeline #699 x",
            "NOT LANDED: it did not apply", "UNLANDED ORPHAN ~/w/a: 2 commits", "UNLANDED ORPHAN nothing here", "UNLANDED ORPHAN ~/w/c;: gone", "UNLANDED ORPHAN /w/d,: gone", "WAITING ON YOU: decide",
            "FIX FAILED: Main #12 again", "DIAGNOSED: Identity takes #7", "text with abc1234def and 42 numbers\nsecond line",
            "crashed: boom", "exit 3 with no verdict", "ended by signal 9", "OTHER: just a note", "BLOCKED: Main #3"]


def a_record(r):
    rec = {}
    for key, gen in (("alert_item", lambda: r.choice(["x:y", ""])), ("item_type", lambda: r.choice(["red", "change", "condition", None])),
                     ("item_id", lambda: r.choice(["i1", "", None])), ("kind", lambda: r.choice(KINDS)),
                     ("verdict", lambda: r.choice(VERDICTS)), ("cwd", lambda: r.choice(["/w/a", "", None, "rel/w"])),
                     ("command", lambda: r.choice(["land", "watch", "wait", "red", None])), ("entry", lambda: r.choice(["e1", None])),
                     ("reply", lambda: r.choice([True, False])), ("final", lambda: r.choice([True, False])),
                     ("audience", lambda: r.choice(["owner", "fixer", None])), ("first_at", lambda: r.choice([NOW - 5000, NOW])),
                     ("at", lambda: NOW - 100)):
        if r.random() < (0.97 if key in ("kind", "verdict") else 0.5):
            rec[key] = gen()
    return rec


class AlertItemTest(unittest.TestCase):
    def build(self, case):
        return (a_record(case.rng),)

    def test_alert_item(self):
        check(self, "alert_item", self.build, lambda A: A.alert_item, 6000)


class AlertRouteTest(unittest.TestCase):
    def build(self, case):
        r = case.rng
        fixed_time(case)
        case.fake("lease_is_live", lambda rng, row, t: rng.random() < 0.4)
        case.fake("red_family", lambda rng, d: maybe(rng, "fam", 0.5))
        case.fake("owner_alive", lambda rng, st, fam, t: rng.random() < 0.4)
        case.fake("env_num", lambda rng, name, default: rng.choice([default, 1, 3]))
        states = list(A.MACHINES["red"]) + ["bogus"]
        red = maybe(r, {"id": "r1", "state": r.choice(states), "level": r.choice([None, 0, 1, 2, 3]),
                        "data": maybe(r, {"key": r.choice([None, "k"])}, 0.7)}, 0.5)
        st = maybe(r, Probe(case, "st", item=lambda rng, typ, i: raising(rng, 0.2, maybe(rng, {"id": i, "state": rng.choice(states),
                                                                                           "level": rng.choice([0, 2]), "data": {}}, 0.8),
                                                                       RuntimeError), get=lambda rng, n: None), 0.6)
        rec = a_record(r)
        if r.random() < 0.3 and rec.get("kind") in ("RED_UNCLAIMED", "RED_STARVED", "RED_ESCALATE"):
            rec["item_type"], rec["item_id"] = "red", "r9"
        cond = maybe(r, {"opened_at": r.choice([NOW - 5000, NOW - 10, None, NOW - 4000]), "text": "t"}, 0.5)
        args = (rec, st, maybe(r, NOW + r.choice([0, 100, 4000]), 0.7), red, cond)
        return args[:r.choice([1, 5, 5, 5])]

    def test_alert_route(self):
        check(self, "alert_route", self.build, lambda A: A.alert_route, 6000)


# ------------------------------------------------------------------------------------------------ statusagents
class StatusAgentsTest(unittest.TestCase):
    def build(self, case):
        r = case.rng
        paths = [None, "/w/a", "/w/b", ""]
        rows = []
        for i in range(r.randint(0, 5)):
            meta = {k: g() for k, g in (("kind", lambda: r.choice(["claim", "fixer", "owned"])), ("pid", lambda: r.choice([None, 11, 12, 0])),
                                        ("tier", lambda: r.choice(["a", "b"])), ("run", lambda: r.choice([1, 2])), ("workflow", lambda: "W"),
                                        ("worktree", lambda: r.choice(paths)), ("log", lambda: r.choice([None, "/l/1"]))) if r.random() < 0.7}
            rows.append({"name": f"red/{i}", "holder": r.choice(["h1", "h2", None]), "meta": json.dumps(meta),
                         "acquired_at": r.choice([None, 100.0, 50.0]), "renewed_at": r.choice([None, 200.0, 300.0]), "epoch": 1})
        recs = [{"path": f"/r/{i}.json", "pid": r.choice([11, 12, 13, None]), "alive": r.random() < 0.5,
                 "ended": r.choice([None, iso(NOW - 60), iso(NOW - 4 * 3600), "bad"]), "workflow": r.choice([None, "W2"]),
                 "run": r.choice([None, 3]), "worktree": r.choice(paths), "transcript": r.choice([None, "/t"]),
                 "why": r.choice([None, "tier deep", "x"]), "started": r.choice([None, 150.0, "2026-11-01T00:00:00Z", 10.0]),
                 "rc": r.choice([None, 0]), "verdict": r.choice([None, "v"]), "label": "l", "key": "k", "jobs": ["j"]}
                for i in range(r.randint(0, 5))]
        case.fake("lease_is_live", lambda rng, row, t: rng.random() < 0.7)
        case.fake("status_fixer_records", lambda rng, t: recs)
        case.fake("status_enrich_all", lambda rng, agents: agents)
        case.fake("live_annotate", lambda rng, out, t: raising(rng, 0.2, list(reversed(out))))
        case.fake("err", lambda rng, m: None)
        case.put("STATUS_AGENTS_MAX", r.choice([2, 16]))
        st = Probe(case, "st", list=lambda rng, prefix: rows)
        return (st, NOW, maybe(r, recs, 0.4))[:r.choice([2, 3, 3])]

    def test_status_agents(self):
        check(self, "status_agents", self.build, lambda A: A.status_agents, 4000)


class StatusWorktreeTest(unittest.TestCase):
    def build(self, case):
        r = case.rng
        state = fixed_time(case)
        wt = "/w/a"
        head = r.choice([None, "h1", "h2"])
        c = maybe(r, {"head": r.choice(["h1", "h2"]), "commits": maybe(r, ["c"], 0.8) or None, "recent_files": ["f"], "dirty": ["d"],
                      "dirty_at": r.choice([state[0] - 10, state[0] - 100, 0]), "at": state[0] - 5, "error": r.choice([None, "boom"])}, 0.8)
        if c is not None and r.random() < 0.15:
            c.pop("commits")
        case.put("_status_wt", {wt: c} if c else {})
        alive = r.random() < 0.4

        def thread(**kw):
            case.log.append(["Thread", sorted(kw)])
            return types.SimpleNamespace(is_alive=case.recorded("is_alive", lambda rng: alive), start=case.recorded("start", lambda rng: None),
                                         join=case.recorded("join", lambda rng, b: None))
        case.put("_status_wt_reads", {wt: thread()} if r.random() < 0.3 else {})
        case.fake("status_head", lambda rng, w: head)
        case.put("threading", types.SimpleNamespace(Thread=thread, Lock=__import__("threading").Lock))
        return (wt, r.choice([0.5, 3.0, 12.0]))

    def test_status_worktree(self):
        check(self, "status_worktree", self.build, lambda A: A.status_worktree, 4000)


# ------------------------------------------------------------------------------------------------ processorlink
class AwaitEntryTest(unittest.TestCase):
    def build(self, case):
        r = case.rng
        clock = {"now": NOW}

        def sleep(secs):
            clock["now"] += secs
        case.fake("env_num", lambda rng, name, default: rng.choice([15, 30, 200, 900]))
        case.fake("spawn_waiting_processor", lambda rng, repo: None)
        case.fake("restart_if_changed", lambda rng, owned: None)
        case.fake("err", lambda rng, m: None)
        states = ["queued", "checking", "checked", "waiting", "pushing", "landed", "rejected", "superseded", "cancelled", "bogus"]

        def item(rng, typ, eid):
            if rng.random() < 0.15:
                raise A.StoreUnavailable("down " * rng.randint(1, 30))
            if rng.random() < 0.15:
                return None
            data = {k: g() for k, g in (("landed_sha", lambda: "sha1"), ("tip", lambda: "tip"), ("head", lambda: "hd"), ("batch", lambda: 3),
                                        ("pushed_at", lambda: 77.0), ("why", lambda: rng.choice(["", "SUPERSEDED by x", "SUPERSEDED", "SUPERFLUOUS", "NOT LANDED: y", "NOT SO: y", "z"])),
                                        ("superseded_by", lambda: "e9")) if rng.random() < 0.5}
            return {"state": rng.choice(states) if rng.random() < 0.6 else rng.choice(["queued", "checking", "waiting"]),
                    "data": maybe(rng, data, 0.9), "entered_at": 5.0}
        store = Probe(case, "store", item=item)
        clock_p = types.SimpleNamespace(time=case.recorded("clock.time", lambda rng: clock["now"]), sleep=case.recorded("clock.sleep", lambda rng, s: sleep(s)))
        holder = {"case": case}
        return (("/repo", "e1", Probe(case, "own"), clock_p, store), holder)

    def test_await_entry(self):
        def run(A):
            def inner(packed):
                args, holder = packed
                return captured(holder, A._await_entry)(*args)
            return inner
        check(self, "_await_entry", lambda case: (self.build(case),), run, 1500)


# ------------------------------------------------------------------------------------------------ lander
def a_land_call(r, i):
    cmd = r.choice(["agentctl land", "x; agentctl land --range", "git status", "agentctl land status", "cd /w/a && agentctl land", "", "cd /w/b; land"])
    odd = [[{"type": "text"}], "str", None, [{"type": "tool_use", "input": None}], [{"type": "tool_use", "input": {"command": 5}}]]
    return {"message": {"content": [{"type": "tool_use", "input": {"command": cmd}}] if r.random() < 0.75 else r.choice(odd)},
            "timestamp": r.choice([None, f"2026-11-0{i % 9 + 1}T00:00:00Z"])}


class LandCallsTest(unittest.TestCase):
    def build(self, case):
        r = case.rng
        lines = [a_land_call(r, i) if r.random() < 0.85 else r.choice(["junk", 5, None]) for i in range(r.randint(0, 6))]
        case.fake("_tail_lines", lambda rng, path: lines)
        return ("/p",)

    def test_land_calls(self):
        check(self, "land_calls", self.build, lambda A: A.land_calls, 3000)


class LanderTranscriptTest(unittest.TestCase):
    def build(self, case):
        r = case.rng
        subs = [f"/proj/x/s/subagents/agent-{i}.jsonl" for i in range(r.randint(0, 3))]
        main = [f"/proj/x/s.jsonl"] if r.random() < 0.7 else []
        case.fake("claude_projects_root", lambda rng: "/proj")
        case.patch(glob_module, "glob", lambda rng, pat: subs if "subagents" in pat else main)
        case.fake("mtime", lambda rng, p: NOW - rng.choice([0, 10, 100, 599, 601, 5000]))
        calls = lambda rng, p: [(rng.choice(["", "2026-11-01T00", "2026-11-02T00"]), rng.choice(["agentctl land", "cd /w/a && agentctl land", "ctl /w/b land", " "]))
                                for _ in range(rng.randint(0, 3))]
        case.fake("land_calls", calls)
        case.fake("process_ancestry", lambda rng, stop: ["bash -c agentctl land", "zsh"][:rng.randint(0, 2)])
        sid = r.choice(["s"] * 8 + ["", None])
        args = (sid, maybe(r, NOW, 0.6), maybe(r, r.choice(["/w/a", "/w/b", "", "/w/c"]), 0.7), maybe(r, r.choice([[], ["cd /w/a && agentctl land", "x"]]), 0.5))
        return args[:r.choice([1, 4, 4])], {"pid": r.choice([None, "", "123", "x"])}

    def test_lander_transcript(self):
        def run(A):
            def inner(packed):
                args, env = packed
                with mock.patch.dict(os.environ, {}, clear=False):
                    os.environ.pop("CLAUDE_PID", None)
                    if env["pid"] is not None:
                        os.environ["CLAUDE_PID"] = env["pid"]
                    return A.lander_transcript(*args)
            return inner
        check(self, "lander_transcript", lambda case: (self.build(case),), run, 4000)


class LanderRecordsTest(unittest.TestCase):
    def build(self, case):
        r = case.rng

        def items(rng, typ):
            if rng.random() < 0.15:
                raise RuntimeError("down")
            return [{"id": f"{typ}{i}", "state": rng.choice(["landed", "queued"]),
                     "data": maybe(rng, {k: g() for k, g in (("lander", lambda: maybe(rng, {"repo": rng.choice(["o/a", "o/b", None])}, 0.7)),
                                                                ("commit_keys", lambda: rng.choice([[], [{"sha": "s"}], None])),
                                                                ("repo", lambda: rng.choice(["o/a", "o/b", None])), ("change", lambda: "c"),
                                                                ("landed_commits", lambda: {"s": "p"}), ("worktree", lambda: "/w"),
                                                                ("branch", lambda: "b")) if rng.random() < 0.6}, 0.9)} for i in range(rng.randint(0, 3))]
        case.fake("err", lambda rng, m: None)
        return (Probe(case, "store", items=items), r.choice([None, "o/a", "o/b"]))[:r.choice([1, 2, 2])]

    def test_lander_records(self):
        check(self, "lander_records", self.build, lambda A: A.lander_records, 3000)


# ------------------------------------------------------------------------------------------------ changekind
class ClassifyChangeTest(unittest.TestCase):
    def build(self, case):
        r = case.rng
        fixed_time(case)
        names = ["1", "2", "3", "4"]
        case.fake("fixes_named", lambda rng, e: set(rng.sample(names, rng.randint(0, 3))))
        case.fake("identity_change", lambda rng, repo, e: rng.choice([None, True, False]))
        e = {k: g() for k, g in (("id", lambda: "c1"), ("status", lambda: r.choice(["queued", "checking", "checked"])), ("no_batch", lambda: r.choice([True, False])),
                                 ("batch_alone", lambda: r.choice(["why", "", None])), ("push_alone", lambda: r.choice(["pa", None])),
                                 ("known_red", lambda: r.choice([True, False])), ("verify", lambda: r.choice([True, False])),
                                 ("retry_after", lambda: r.choice([0, None, NOW - 5, NOW + 50]))) if r.random() < 0.6}
        facts = maybe(r, {"gate": maybe(r, set(r.sample(["1", "2"], r.randint(0, 2))), 0.8),
                          "open": maybe(r, {n: ["r" + n, r.choice(["outside", "flake"])] for n in r.sample(["2", "3"], r.randint(0, 2))}, 0.8)}, 0.9) or {}
        args = (e, facts, maybe(r, NOW, 0.5), maybe(r, "/repo", 0.6))
        return args[:r.choice([2, 3, 4, 4])]

    def test_classify_change(self):
        check(self, "classify_change", self.build, lambda A: A.classify_change, 6000)


# ------------------------------------------------------------------------------------------------ reddispatch
class RedJoinedIdleTest(unittest.TestCase):
    def build(self, case):
        r = case.rng
        case.fake("red_joined_progress", lambda rng, store, row, why: None)
        case.fake("red_fixes_red_commits", lambda rng, wt, names: rng.choice([None, "a" * 40]))
        case.fake("env_num", lambda rng, name, default: rng.choice([default, 100, 5000]))
        meta = {k: g() for k, g in (("joined_at", lambda: r.choice([NOW - 10, NOW - 100, NOW - 500, NOW - 4000, NOW - 5000, 0])), ("progressed", lambda: r.choice([True, False])),
                                    ("worktree", lambda: "/w")) if r.random() < 0.85}
        row = {"item": r.choice(["red:r1", "", None, "r1"]), "holder": "h", "meta": json.dumps(meta)}

        def item(rng, typ, i):
            return maybe(rng, {"id": i, "state": rng.choice(["red", "fix-pending", "verified", "offered"]), "version": 3,
                               "data": maybe(rng, {k: g() for k, g in (("fix", lambda: rng.choice([{"entry": "e"}, {}, None])),
                                                                          ("family_released", lambda: ["x"])) if rng.random() < 0.5}, 0.9)}, 0.85)

        def update(rng, *a):
            if rng.random() < 0.2:
                raise A.StaleVersion()
        return (Probe(case, "store", item=item, update_item=update), row, NOW)

    def test_red_joined_idle(self):
        check(self, "red_joined_idle", self.build, lambda A: A.red_joined_idle, 4000)


class RedSpawnTest(unittest.TestCase):
    def build(self, case):
        r = case.rng
        fixed_time(case)
        case.fake("red_repo_note", lambda rng, o_r, run: "note")
        case.fake("lander_spawn", lambda rng, *a: (True, "lander spawned"))
        case.fake("blame_reland", lambda rng, o_r, repo, u, b: {"ok": rng.random() < 0.5, "msg": "relanded"})
        case.fake("inbox_write", lambda rng, *a, **k: None)
        case.fake("blame_note", lambda rng, b: "bn")
        case.fake("red_revert", lambda rng, o_r, repo, u, culprit: (rng.random() < 0.5, "reverted"))
        case.fake("red_dispatch", lambda rng, *a, **k: (True, "dispatched"))
        case.fake("ts", lambda rng: "T")
        case.fake("env_num", lambda rng, n, d: d)
        case.fake("red_what", lambda rng, pic: "what")
        pic = {k: g() for k, g in (
            ("workflow", lambda: "W"), ("key", lambda: "k"), ("job", lambda: r.choice([None, "j"])), ("jobs", lambda: ["a"]), ("kind", lambda: r.choice([None, "test"])),
            ("line", lambda: "l"), ("run", lambda: r.choice([{}, {"number": 5}, None])), ("refuted", lambda: ["x" * 12]), ("predicts", lambda: "p"),
            ("red_kind", lambda: r.choice(["flake", "outside"])), ("diagnosis", lambda: "d"), ("names", lambda: ["n1"]), ("prior_work", lambda: {"w": 1}),
            ("old_note", lambda: "o"), ("history", lambda: [{"at": 1, "holder": "h", "outcome": "no", "worktree": "/w"}] * r.randint(0, 2)),
            ("repo", lambda: "o/r"), ("decision", lambda: r.choice([{}, {"answer": "revert abcdef1234"}, {"answer": "revert abc1234 " + "w" * 200}, {"answer": "x"}, None])),
            ("attempts", lambda: 3), ("culprit", lambda: r.choice([None, "c" * 40])),
            ("blame", lambda: r.choice([{}, {"culprit": "c" * 40}, {"reland_due": True}, {"reland_due": True, "reland": {"ok": True}}, {"culprit": "cu", "reland_due": True}, None]))) if r.random() < 0.8}
        pic.setdefault("workflow", "W")
        pic.setdefault("key", "k")
        if r.random() < 0.8:
            pic.setdefault("run", {"number": 5})
        elif "run" in pic and pic["run"] is None:
            pic["run"] = {}
        off = {"picture": pic, "tier": r.choice(["lander", "revert", "resume", "deep", "fix", "other"]), "item": "red:r1", "lease": "L"}

        def item(rng, typ, i):
            return maybe(rng, {"id": i, "version": 2, "state": "red"}, 0.8)
        store = Probe(case, "store", item=item, update_item=lambda rng, *a: None, note=lambda rng, *a: None)
        took = {"epoch": 3, "secret": "s"}
        extra = r.choice([(), ({"picture": {"jobs": ["z", "b"]}},), ({"picture": {}},)])
        return (store, "o/r", "/repo", off, took, extra)[:r.choice([5, 6, 6])]

    def test_red_spawn(self):
        check(self, "red_spawn", self.build, lambda A: A.red_spawn, 4000)


if __name__ == "__main__":
    unittest.main()
