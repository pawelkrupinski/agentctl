"""RESTRUCTURE ROUND 7: each function split into named stages keeps its behaviour. The digests (fixtures/
restructure7-digests.json, recorded from the code BEFORE the split with `R4_RECORD=1`) cover thousands of seeded cases
each, collaborators faked (golden_cases.py): every call, its order and the result. (redblock's three are round 6's.)"""
import contextlib, io, os, sys, types, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402
from harness import case_context  # noqa: E402
from golden_cases import Probe, check as _check  # noqa: E402

DIGESTS = os.path.join(HERE, "fixtures", "restructure7-digests.json")


def check(test, name, build, fn_of, n=3000):
    _check(test, A, name, build, fn_of, n, DIGESTS)


def maybe(rng, value, p=0.5):
    return value if rng.random() < p else None


def proc(rng, code=0, out="", err=""):
    return types.SimpleNamespace(returncode=code, stdout=out, stderr=err)


class FakeWaiter:
    """A Waiter whose every step is a recorded fake drawing from the case's stream."""

    def __init__(self, case, key):
        r = case.rng
        self.key, self.owned, self.result = key, False, None
        self.holder = {"pid": r.randint(1, 9), "started": "t0"}
        self.own = types.SimpleNamespace(name=key)
        self.acquire = case.recorded(f"{key}.acquire", self._acquire)
        self.poll_owned = case.recorded(f"{key}.poll_owned", self._poll_owned)
        self.poll_attached = case.recorded(f"{key}.poll_attached", self._poll_attached)
        self.release = case.recorded(f"{key}.release", lambda rng: None)

    def _acquire(self, rng):
        self.owned = rng.random() < 0.6
        return self.owned

    def _poll_owned(self, rng):
        if rng.random() < 0.3:
            self.result = (rng.random() < 0.7, f"verdict {self.key}")
        return rng.choice(["still going", "waiting on ci", "check failed: x"])

    def _poll_attached(self, rng):
        if rng.random() < 0.25:
            self.result = (rng.random() < 0.7, f"attached verdict {self.key}")


class OwnedTest(unittest.TestCase):
    def test_wait_all(self):
        def build(case):
            r = case.rng
            now = [r.choice([1000.0, 1000.0, 0.0, 0.5])]

            def sleep(rng, s):
                now[0] += s
            case.put("time", types.SimpleNamespace(time=lambda: now[0], sleep=case.recorded("sleep", sleep)))
            case.fake("env_number", lambda rng, name, default: rng.choice([3, 15]))
            case.fake("print_error", lambda rng, m: None)
            case.fake("restart_if_changed", lambda rng, owned: None)
            waiters = [FakeWaiter(case, f"w{i}") for i in range(r.randint(0, 3))]
            return (waiters, r.choice([2, 9, 40, 200]), r.choice([1, 5, 20]), r.choice([5, 30, 60]))

        def call(A):
            def run(*args):
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    try:
                        rc = A.wait_all(*args, case_context().caches.code_restart)
                    except BaseException as e:
                        rc = type(e).__name__
                return rc, out.getvalue()
            return run
        check(self, "wait_all", build, call, 4000)


def replay_rows(r):
    rows = []
    for i in range(r.randint(0, 12)):
        row = {"id": f"a{i}", "label": r.choice(["what-if x", "identity-slice y", "sbt test", "", None, "lint"]),
               "class": r.choice(["agent", "measure", "check", "red", None]), "kind": r.choice(["job", "check", None])}
        if r.random() < 0.92:
            row["at"] = r.choice([100, 200, 300, 400, 1500]) + r.choice([0, 0, 10, 30])
        if r.random() < 0.6:
            row["waited"] = r.choice([0, 0, 30, 100, None])
        if r.random() < 0.8:
            row["cores"] = r.choice([0, 1, 2.5, 3, 4, 6, 8, None])
        rows.append(row)
        if r.random() < 0.3:
            rows.append({"id": f"a{i}" if r.random() < 0.9 else "zz", "end": r.choice([200, 800, 1700, 3000, 5000])})
    return rows


class GovRouteTest(unittest.TestCase):
    def test_gov_replay(self):
        def build(case):
            r = case.rng
            args = (replay_rows(r), r.choice([2, 4, 6, 8, 16]), r.choice([100, 200, 300, 310, 400, 410, 500, 1500, 1800, 2000, 4000]))
            return args + r.choice([(), (60,), (1800,), (5000,)])
        check(self, "gov_replay", build, lambda A: A.gov_replay, 8000)


def blame_group(r, p=1.0):
    groups = [{"commits": [f"c{i}a", f"c{i}b"], **({"change": f"ch{i}"} if r.random() < 0.6 else {})} for i in range(2)]
    return {"groups": groups, "culprit": r.choice([0, 1, None])} if r.random() < p else {}


def blame_of(r):
    b = blame_group(r, 0.85)
    if r.random() < 0.8:
        b["run"] = {"number": r.choice([629, 630, 12, None])}
    b["state"] = r.choice(["confirmed", "confirmed", "refuted", "undetermined", None])
    b["why"] = r.choice([None, "x" * 200, "no longer"])
    if r.random() < 0.7:
        b["revert"] = {"worktree": r.choice([None, "/w/rev1", "/w/rev2"]), "sha": r.choice([None, "abcdef0123456789"]), "ok": r.random() < 0.5}
    if r.random() < 0.3:
        b["withdrawn"] = r.choice([True, {}, {"why": "w" * 200, "change": "chw"}, {"revert": {"change": "chr", "worktree": "/w/rev1", "sha": "ff"}},
                                   {"revert": {"commits": ["cx"]}}, {"revert": {}, "why": None}])
    return b


class BlameRecordsTest(unittest.TestCase):
    def test_blame_records(self):
        def build(case):
            r = case.rng
            items = []
            for i in range(r.randint(0, 5)):
                it = {"state": r.choice(["verified", "cleared", "open", "offered", None]), "id": f"i{i}"}
                if r.random() < 0.9:
                    it["data"] = {"blame": blame_of(r), "fix": r.choice([None, {"sha": "9" * 12}, {}])}
                    if r.random() < 0.1:
                        it["data"]["blame"] = None
                items.append(it)

            def got(rng, kind):
                if rng.random() < 0.06:
                    raise rng.choice([RuntimeError("x"), SystemExit(3)])
                return items
            return (Probe(case, "store", items=got),)
        check(self, "blame_records", build, lambda A: A.blame_records, 6000)

    def test_blame_land_refusal(self):
        def build(case):
            r = case.rng
            chs = ["ch0", "ch1", "chw"]
            msgs = ["", "Reverts-Change: ch0\nFixes-Red: 12", "Reverts-Change: ch1\nReverts-Change: ch0\nFixes-Red: 629, 12", "plain",
                    "Reverts-Change: chw\nFixes-Red: 5", "Reverts-Change: zz"]
            case.fake("git", lambda rng, repo, *a, **k: proc(rng, 0, rng.choice(msgs)))
            dead = {ch: [{"run": "12", "why": f"why {ch}", "verdict": "NOT THE CULPRIT", "worktree": r.choice([None, "/w/rev1", "/w/rev2"])}
                         for _ in range(r.randint(1, 2))] for ch in chs if r.random() < 0.6}
            live = {"runs": {ch: set(r.sample(["12", "629", "5"], r.randint(0, 2))) for ch in chs if r.random() < 0.5},
                    "worktrees": set(r.sample(["/w/rev1", "/w/rev2", "/w/other"], r.randint(0, 2)))}
            case.fake("blame_records", lambda rng, st: (dead, live))
            records = r.choice([(dead, live), (dead, live), None, ({}, live)])
            commits = r.choice([None, [], ["abcdef0123456789"], ["a" * 12, "b" * 12, "c" * 12]])
            return (r.choice([None, "/repo"]), commits, Probe(case, "store"), r.choice([None, "/w/rev1", "/w/rev2", "/w/rev3", os.path.expanduser("~/x")]), records)
        check(self, "blame_land_refusal", build, lambda A: A.blame_land_refusal, 8000)

    def test_blame_withdraw(self):
        def build(case):
            r = case.rng
            case.fake("movies_repo_path", lambda rng: rng.choice([None, "/m/repo"]))
            case.fake("git", lambda rng, repo, *a, **k: proc(rng, 0, rng.choice(["", "deadbeefcafe0123 b"])))
            case.fake("remote", lambda rng: "origin")
            case.fake("red_stop_holder", lambda rng, *a, **k: None)
            case.fake("blame_drop_revert_worktree", lambda rng, *a: "dropped it")
            case.fake("blame_change_name", lambda rng, g: f"name-{g['commits'][0]}")
            case.fake("inbox_write", lambda rng, *a: None)
            case.fake("dispatch_log", lambda rng, *a: None)
            case.fake("ts", lambda rng: "T")
            case.fake("red_what", lambda rng, d: f"red-{d['key']}")
            case.fake("machine_state", lambda rng, kind, state: types.SimpleNamespace(terminal=rng.random() < 0.4))
            case.fake("red_offer", lambda rng, *a, **k: None)
            b = blame_of(r)
            if r.random() < 0.7:
                b.update(groups=[{"commits": ["c0a"]}, {"commits": ["c1a"], "change": "ch1"}], culprit=r.choice([0, 1]), state="confirmed")
            b.update({"revived": r.choice([None, "rev"]), "parent": 1, "evidence": 2, "culprit_extra": 3})
            d = {"key": "k1", "workflow": "wf", "last_run": r.choice([None, {"number": 4}]), "job": "j", "blame": r.choice([b, b, b, None, {}])}
            it = {"id": "i1", "state": "open", "version": 2, "data": d}
            st = Probe(case, "store", item=lambda rng, kind, iid: maybe(rng, {"id": "i1", "version": 3, "state": rng.choice(["open", "cleared"])}, 0.7))
            real = os.path.isdir
            os.path.isdir = lambda p: p != "/m/gone" and (p == "/m/repo" or real(p))

            def run():
                try:
                    return A.blame_withdraw(st, it, r.choice(["wrong", "stale"]), r.choice([None, "/m/repo", "/m/gone"]))
                finally:
                    os.path.isdir = real
            return (run,)
        check(self, "blame_withdraw", build, lambda A: lambda run: run(), 6000)


PATHS = ["build.sbt", "project/Foo.scala", "scripts/hooks/pre-push", "docs/a.md", "web/src/main/A.scala", "scripts/x.sh", "test/resources/a.tsv",
         "common/src/test/B.scala", "worker/src/it/C.scala", "README.md", ".github/workflows/m.yml", "scripts/hooks/local-tests.txt"]
WORDS = ["sbt", "identity", "shellcheck", "eslint", "scripttests", "drift", "alpha", "beta", "web/Test/compile", "e2e/Test/compile",
         "common/Test/compile", "worker/It/compile", "actionlint"]


class QueueRecheckTest(unittest.TestCase):
    def test_recheck_reason(self):
        def build(case):
            r = case.rng
            case.fake("git", lambda rng, *a, **k: proc(rng, 0, " ".join(f"s{i}" for i in range(rng.randint(0, 5)))))
            case.fake("range_paths", lambda rng, tree, a, b, deleted=False: rng.sample(PATHS, rng.randint(0, 5)))
            case.fake("forces_full_recheck", lambda rng, p: rng.random() < 0.5)
            case.fake("selection_at", lambda rng, *a: rng.choice(["new1", "new2", None]))
            case.fake("selection_change", lambda rng, *a, **k: maybe(rng, "its selection moves", 0.3))
            case.fake("named_reader", lambda rng, *a: maybe(rng, ("rd.sh", "n.sh"), 0.35))
            case.fake("out_of_build_reach", lambda rng, *a: rng.random() < 0.4)
            case.fake("build_view_change", lambda rng, *a: maybe(rng, "its build view moves", 0.4))
            case.fake("without_generated", lambda rng, tree, ps: [p for p in ps if rng.random() < 0.7])
            return ("tree", "base", "moved", "tip", r.sample(PATHS, r.randint(0, 5)), r.choice([None, "ftip"]))
        check(self, "recheck_reason", build, lambda A: A.recheck_reason, 8000)

    def test_read_overlap(self):
        def build(case):
            r = case.rng
            case.fake("precheck_hook", lambda rng, tree: rng.choice(["/h/pre-push", None]))
            case.fake("hook_modes", lambda rng, hook: [m for m in A.READ_MODES if rng.random() < 0.96])
            case.fake("hook_text_modes", lambda rng, hook, flags=(A.PER_FILE_MODE,): [m for m in flags if rng.random() < 0.7])
            case.fake("hook_query", lambda rng, hook, tree, mode, paths: None if rng.random() < 0.06 else rng.sample(WORDS, rng.randint(0, 5)))
            return ("tree", maybe(r, r.sample(PATHS, r.randint(1, 5)), 0.93) or [], maybe(r, r.sample(PATHS, r.randint(1, 5)), 0.93) or [])
        check(self, "read_overlap", build, lambda A: A.read_overlap, 10000)


class NetWatchTest(unittest.TestCase):
    def test_tunnel_watch(self):
        def build(case):
            r = case.rng
            case.put("time", types.SimpleNamespace(time=lambda: 1000.0))
            state = {}
            for k, gen in (("fails", lambda: r.choice([0, 1, 2, 5])), ("pending_since", lambda: r.choice([None, 10.0, 880.0, 930.0, 990.0, 1080.0])),
                           ("network", lambda: r.random() < 0.5), ("escalated", lambda: r.random() < 0.3), ("last_kick", lambda: r.choice([0, 500.0, 990.0]))):
                if r.random() < 0.6:
                    state[k] = gen()
            case.fake("read_json_file", lambda rng, path: dict(state))
            case.fake("write_json_file", lambda rng, path, st: None)
            case.fake("tunnel_state_path", lambda rng: "/s/tunnel.json")
            case.fake("tunnel_ports", lambda rng: [(1234, 22)])
            case.fake("state_read", lambda rng: rng.choice([{}, {"network": {"at": 5}}, {"network": {}}]))
            case.fake("state_update", lambda rng, **k: None)
            case.fake("mark_gate_dirty", lambda rng: None)
            case.fake("print_error", lambda rng, m: None)
            case.fake("open_outage", lambda rng, *a, **k: None)
            probed = case.recorded("run_probe", lambda rng, kind, *a: types.SimpleNamespace(detail=rng.choice(["tunnel ok", "tunnel 5s", "x down", "the tunnel is not answering 5s", "ok"]), ok=True))
            case.put("run_probe", lambda check, *a: probed(A.probe_kind(check), *a))  # recorded by kind, as before the opener ran the function it imports
            probe = r.choice([None, lambda: r.choice(["ok", "5s", "down"])])
            probe = None if probe is None else case.recorded("probe", lambda rng: rng.choice(["ok", "5s", "down"]))
            kick = case.recorded("kick", lambda rng: None)
            uplink = r.choice([None, case.recorded("uplink", lambda rng: rng.choice([0.0, 0.05, 0.1, 0.5, 1.0]))])
            case.fake("uplink_loss", lambda rng: rng.choice([0.0, 0.1, 1.0]))
            case.fake("launchctl_kick", lambda rng: None)
            return ("store", r.choice([None, 1000.0, 1050.0, 1200.0, 60.5, 61.0]), probe, r.choice([None, kick]), uplink)

        check(self, "tunnel_watch", build, lambda A: A.tunnel_watch, 10000)


if __name__ == "__main__":
    unittest.main()
