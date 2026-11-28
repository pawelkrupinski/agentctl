"""RESTRUCTURE ROUND 11: each function split into named stages keeps its behaviour. The digests (fixtures/
restructure11-digests.json, recorded from the code BEFORE the split with `R4_RECORD=1`) cover thousands of seeded cases
each, collaborators faked (golden_cases.py): every call, its order and the result."""
import base64, glob, os, subprocess, sys, types, unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402
from harness import case_context  # noqa: E402


def change_state_of(ok, via=None):
    """The LandState a drawn (landed, via) stands for: True landed, None unknown, any other falsy not landed."""
    return A.LandState(A.LANDED if ok else A.UNKNOWN if ok is None else A.NOT_LANDED, via=via if ok else None)
from golden_cases import check as _check  # noqa: E402

DIGESTS = os.path.join(HERE, "fixtures", "restructure11-digests.json")


def check(test, name, build, fn_of, n=3000):
    _check(test, A, name, build, fn_of, n, DIGESTS)


def proc(rng, code=0, out="", error=""):
    return types.SimpleNamespace(returncode=code, stdout=out, stderr=error)


def with_state(fn, *names):
    """fn's result (or its exception) with the named module globals after it: the state it left."""
    def run(*args):
        try:
            result = ["ok", fn(*args)]
        except BaseException as e:  # noqa: BLE001 -- an exception IS an outcome
            result = ["raised", type(e).__name__, str(e)[:60]]
        return result, [dict(getattr(A, n)) for n in names]
    return run


def clock(case, start=1_000_000.0):
    t = [start]

    def now(rng):
        t[0] += rng.choice([0, 0.5, 10, 100, 1000])
        return t[0]
    case.fake("epoch_now", now)


# ------------------------------------------------------------------------------------------------ storetoken.token
class TokenTest(unittest.TestCase):
    def build(self, case):
        r = case.rng
        o_r = r.choice(["pawelkrupinski/movies"] * 4 + ["o/other"])
        env = {k: "e-" + k for k in ("GH_TOKEN", "AGENTCTL_STORE_TOKEN") if r.random() < 0.3}
        case.put("os", types.SimpleNamespace(**{**vars(os), "environ": env, "path": os.path}))
        case.put("_tokens", r.choice([{}, {}, {"movies": "cached"}, {"bitcashier": "c2"}]))
        case.put("_token_missing", r.choice([{}, {"movies": {"reads": 1}}]))

        def red_read(rng, o):
            if rng.random() < 0.1:
                raise A.NoReadAccess("no read")
        case.fake("red_read_token", red_read)
        case.fake("token_from_processor", lambda rng, o: rng.choice([None, "", "ptok"]))
        case.fake("secrets_bin", lambda rng: rng.choice([None, "/s/secrets", "/s/secrets"]))
        case.fake("interactive_foreground", lambda rng: rng.random() < 0.6)

        def run_cmd(rng, cmd, **k):
            x = rng.random()
            if x < 0.1:
                raise OSError(2, "gone")
            return proc(rng, rng.choice([0, 0, 1, 124]), rng.choice(["vtok\n", "", "  "]))
        case.fake("run_cmd", run_cmd)
        case.fake("seed_processor", lambda rng, tok: None)
        case.fake("token_missing", lambda rng, ns, why: None)
        case.fake("token_back", lambda rng, ns: None)
        return (o_r,)

    def test_token(self):
        check(self, "token", self.build, lambda A: with_state(A.token, "_tokens"), 6000)


# ------------------------------------------------------------------------------------------------ gating
GEN_ROWS = ["kind\tworkflow\tname\tproven\tnote", "# c", "workflow\tmain.yml\tMain\t#12\tn", "workflow\tid.yml\tId\taggregate",
            "job\tmain.yml\tunit\t#3", "short\tx", "", "workflow\tnew.yml\tNew\tunproven\twhy\textra", "  "]


def a_rows_text(r):
    return "\n".join(r.choice(GEN_ROWS) for _ in range(r.randint(0, 5)))


def a_pre_rows(r, repo):
    rows = [{"kind": "workflow", "workflow": "w.yml", "name": "W", "proven": "#1", "note": ""}]
    cache = {}
    if r.random() < 0.3:
        cache[(repo, "m" * 40)] = rows
    if r.random() < 0.3:
        cache[("api", "o/r")] = (r.choice([999_000.0, 999_700.0, 1_000_000.0, 1_000_001.0]), rows)
    if r.random() < 0.3:
        cache[("names", "o/r")] = (r.choice([999_000.0, 999_700.0, 1_000_000.0]), {"w.yml": "W", "id.yml": "IdApi"})
    return cache


def fake_store(case, r, o_r="o/r"):
    """read_record / write_json over a kept registry file: what is read is drawn, what is written is recorded."""
    kept = r.choice([None, {}, {o_r: {"rows": [{"kind": "workflow", "workflow": "k.yml", "name": "K", "proven": "#2", "note": ""}],
                                      "names": {"k.yml": "Kept", "w.yml": "Old"}}}, {o_r: None}])
    case.fake("read_record", lambda rng, path: kept)

    def write(rng, path, data):
        if rng.random() < 0.15:
            raise OSError("ro")
    case.fake("write_json", write)
    case.fake("gating_checks_path", lambda rng: "/state/gating-checks.json")
    case.fake("ts", lambda rng: "T")


def api_gh(rng, o_r, *a):
    x = rng.random()
    if x < 0.2:
        raise RuntimeError("api down")
    if x < 0.3:
        raise SystemExit("no token")
    if "workflows" in a[1]:
        return rng.choice([{"workflows": [{"path": ".github/workflows/w.yml", "name": "W"}, {"path": "x/y.yml", "name": ""},
                                          {"path": None, "name": "Z"}, {"path": "a/new.yml", "name": "New"}]}, {}, {"workflows": None}])
    return {"content": base64.b64encode(a_rows_text(rng).encode()).decode()}


class GatingChecksTest(unittest.TestCase):
    def build(self, case):
        r = case.rng
        repo = r.choice(["/clone", "/clone", None, ""])
        clock(case)
        case.put("_gating_rows", a_pre_rows(r, repo))
        case.fake("scan_repo_for", lambda rng, o: repo)
        case.fake("remote", lambda rng: "origin")
        case.patch(os.path, "isdir", lambda rng, p: rng.random() < 0.85)

        def git(rng, rp, *a, **k):
            if "rev-parse" in a:
                return proc(rng, 0, rng.choice(["m" * 40, "m" * 40, "n" * 40, ""]) + "\n")
            return proc(rng, rng.choice([0, 0, 1]), a_rows_text(rng))
        case.fake("git", git)
        case.fake("gh_json", api_gh)
        fake_store(case, r)
        return ("o/r",)

    def test_gating_checks(self):
        check(self, "gating_checks", self.build, lambda A: with_state(A.gating_checks, "_gating_rows"), 6000)


class GateFlowNamesTest(unittest.TestCase):
    def build(self, case):
        r = case.rng
        clock(case)
        case.put("_gating_rows", a_pre_rows(r, "/clone"))
        case.fake("main_workflow_files", lambda rng, o: rng.choice([{}, {"W": "w.yml"}, {"W": "w.yml", "Id": "id.yml"}, {"Wmain": "w.yml"}]))
        case.fake("gh_json", api_gh)
        fake_store(case, r)
        return ("o/r", r.choice([(), ("w.yml",), ("w.yml", "id.yml"), ["k.yml", "new.yml"], ("zzz.yml", "w.yml")]))

    def test_gate_flow_names(self):
        check(self, "gate_flow_names", self.build, lambda A: with_state(A.gate_flow_names, "_gating_rows"), 6000)


WF_TEXTS = ["name: Main\non: push\n", "name: 'Quoted one'\non:\n  workflow_call:\n", "on: push\n", "name: \"Dq\"  \n",
            "name: Caller\njobs:\n  a:\n    uses: ./.github/workflows/lib.yml\n  b:\n    - uses: ./.github/workflows/other.yml\n",
            "name: Caller2\njobs:\n  a:\n    uses: ./.github/workflows/lib.yml\n", ""]


class MainWorkflowFilesTest(unittest.TestCase):
    def build(self, case):
        r = case.rng
        repo = r.choice(["/clone", "/clone", None])
        case.put("_flow_files", r.choice([{}, {}, {"m" * 40: {"Cached": "c.yml"}}]))
        case.put("_flow_meta", r.choice([{}, {"m" * 40: {"callable": set()}}]))
        case.fake("scan_repo_for", lambda rng, o: repo)
        case.fake("remote", lambda rng: "origin")
        case.patch(os.path, "isdir", lambda rng, p: rng.random() < 0.9)

        def git(rng, rp, *a, **k):
            if "rev-parse" in a:
                return proc(rng, 0, rng.choice(["m" * 40, "m" * 40, "n" * 40, ""]) + "\n")
            if "ls-tree" in a:
                return proc(rng, 0, " ".join(rng.choice([".github/workflows/a.yml", ".github/workflows/b.yaml", ".github/workflows/c.txt",
                                                         ".github/workflows/d.yml"]) for _ in range(rng.randint(0, 4))))
            return proc(rng, 0, rng.choice(WF_TEXTS))
        case.fake("git", git)
        case.fake("workflow_triggers", lambda rng, txt: rng.choice([{"workflow_call"}, {"push"}, set(), {"workflow_call", "push"}]))
        return ("o/r",)

    def test_main_workflow_files(self):
        check(self, "main_workflow_files", self.build, lambda A: with_state(A.main_workflow_files, "_flow_files", "_flow_meta"), 4000)


# ------------------------------------------------------------------------------------------------ land._watch_group
class FakeProc:
    pid = 7

    def __init__(self, wait, poll):
        self.wait, self.poll = wait, poll

    def __repr__(self):
        return "<proc 7>"


class WatchGroupTest(unittest.TestCase):
    def build(self, case):
        r = case.rng
        calls = [0]
        limit = r.choice([0, 1, 3, 8, 20, 60])

        def wait(rng, timeout=None):
            calls[0] += 1
            if calls[0] > limit:
                return 0
            raise subprocess.TimeoutExpired("p", timeout)
        p = FakeProc(case.recorded("p.wait", wait), case.recorded("p.poll", lambda rng: rng.choice([None, None, None, 0])))
        t = [100.0]

        def now(rng):
            t[0] += rng.choice([0, 0.1, 1, 5, 30, 200])
            return t[0]
        sizes = [0]

        def size(rng, path):
            sizes[0] += rng.choice([0, 0, 0, 1, 50])
            return sizes[0]
        case.patch(os.path, "getsize", size)
        case.fake("env_number", lambda rng, name, default: rng.choice([1, 1, 0.5, default]))

        def beat(rng, mark):
            x = rng.random()
            if x < 0.08:
                raise A.LeaseLost("lost")
            if x < 0.15:
                raise A.StoreUnavailable("down")
        case.fake("lock_beat", beat)
        case.fake("print_error", lambda rng, *a, **k: None)
        case.fake("_end_group", lambda rng, p: None)
        cpu = [0.0]

        def cpu_of(rng, pid):
            cpu[0] += rng.choice([0, 0, 0.2, 1, 3])
            return rng.choice([cpu[0], None]) if rng.random() < 0.05 else cpu[0]
        hook_stall = r.choice([None, None, 40, 300])
        return (p, "/out", "step", r.choice([4, 60, 600]), r.choice([None, case.recorded("sampler", lambda rng: None)]), cpu_of,
                case.recorded("clock", now), r.choice([None, None, 100, 1000]), r.random() < 0.2, hook_stall,
                case.recorded("hooks", lambda rng, pid: rng.random() < 0.4))

    def test_watch_group(self):
        def run(A):
            def go(p, out, step, stall, sampler, cpu_of, clk, wall, quiet, hook_stall, hooks):
                return A._watch_group(p, out, step, stall, sampler, cpu_of, clk, wall, quiet, hook_stall, hooks)
            return go

        def build(case):
            args = self.build(case)
            return args[:5] + (case.recorded("cpu_of", args[5]),) + args[6:]
        check(self, "watch_group", build, run, 5000)


# ------------------------------------------------------------------------------------------------ park
def a_mark(r, i):
    until = r.choice([None, {}, {"kind": "date", "at": "D", "epoch": r.choice([0, 1_000_000.0, 2_000_000.0, None, "5"])},
                      {"kind": "landed", "change": r.choice([None, "c1"]), "commit": r.choice([None, "abcdef" * 7])},
                      {"kind": "owner"}])
    m = {"state": r.choice(["parked"] * 5 + ["released"]), "reason": "why", "at": r.choice([None, 1_000_000.0, 100.0, 900_000.0]),
         "until": until, "digest_at": r.choice([None, 1_000_000.0, 0])}
    if r.random() < 0.2:
        m["repo"] = "/other"
    if r.random() < 0.1:
        m["until"] = None
    return f"/w/p{i}", m


class ParkReleaseWhyTest(unittest.TestCase):
    def build(self, case):
        r = case.rng
        clock(case, 1_500_000.0)

        def chain(rng, store, change):
            x = rng.random()
            if x < 0.15:
                raise RuntimeError("x")
            if x < 0.25:
                raise SystemExit(1)
            return rng.choice([None, {"state": "landed", "id": "c9"}, {"state": "open", "id": "c8"}])
        case.fake("change_chain_end", chain)
        case.fake("change_state", lambda rng, repo, commit: change_state_of(rng.random() < 0.5, rng.choice([None, "", commit, "f" * 40])))
        return (a_mark(r, 0)[1], "/repo", "store", r.choice([None, 1_500_000.0, 5.0, 2_100_000.0]))

    def test_park_release_why(self):
        check(self, "park_release_why", self.build, lambda A: with_state(A.park_release_why), 6000)


class ParkDutyTest(unittest.TestCase):
    def build(self, case):
        r = case.rng
        clock(case, 1_500_000.0)
        marks = dict(a_mark(r, i) for i in range(r.choice([0, 1, 2, 3, 5])))
        case.put("PARK_DIGEST_DAYS", r.choice([14, 1, 1000]))
        case.fake("parked_marks", lambda rng: marks)
        case.fake("parked_path", lambda rng: "/parked.json")
        case.fake("ts", lambda rng: "T")
        case.fake("tilde", lambda rng, p: p)
        case.patch(os.path, "isdir", lambda rng, p: rng.random() < 0.8)
        case.fake("park_release_why", lambda rng, m, repo, store, t: rng.choice([None, None, "met", "its date passed"]))
        case.fake("drop_orphan_records", lambda rng, wt: None)
        case.fake("inbox_write", lambda rng, *a, **k: None)
        case.fake("write_json", lambda rng, path, data: None)
        case.fake("env_number", lambda rng, name, default: default)
        return ("/repo", "store", r.random() < 0.3, r.choice([None, 1_500_000.0, 1_500_000.0, 3_000_000.0]))

    def test_park_duty(self):
        def run(A):
            def go(*args):
                marks = A.parked_marks()
                try:
                    out = ["ok", A.park_duty(*args)]
                except BaseException as e:  # noqa: BLE001
                    out = ["raised", type(e).__name__]
                return out, marks
            return go
        check(self, "park_duty", self.build, run, 6000)


# ------------------------------------------------------------------------------------------------ dispatches
class OrphanBranchesTest(unittest.TestCase):
    def build(self, case):
        r = case.rng
        n = r.choice([0, 1, 2, 3, 4])
        heads = [f"{'a' * 38}{i:02d} refs/heads/{r.choice(['measure/u/abc1234-x', 'feature/x', 'main', 'check/c1', 'measure/u/dead567-y'])}-{i}".replace("-" + str(i), "")
                 for i in range(n)]
        case.fake("remote", lambda rng: "origin")
        case.fake("git", lambda rng, repo, *a, **k: proc(rng, 0, "\n".join(heads)))
        case.fake("dispatch_records", lambda rng: [{"branch": r.choice(["feature/x", None, "check/c1"])} for _ in range(r.randint(0, 2))])
        case.fake("tracked_branch", lambda rng, b, rec: rng.random() < 0.8)
        case.put("SWEEP_KEEP_REMOTE", r.choice([(), ("main",)]))

        def runs(rng, *a, **k):
            x = rng.random()
            if x < 0.1:
                raise RuntimeError("gh broke")
            if x < 0.15:
                raise SystemExit("no token")
            return [{"status": rng.choice(["completed"] * 5 + ["in_progress"]), "databaseId": 10 + i} for i in range(rng.choice([0, 1, 2, 3]))]
        case.fake("gh_json", runs)
        rec = lambda rng: rng.choice([None, {"worktree": "/w/x", "agent": "a"}, {"pid": 5}])
        case.fake("owner_of", lambda rng, repo, st, b, run: rec(rng))
        case.fake("dispatch_for", lambda rng, b, *a: rec(rng))
        case.fake("owner_gone", lambda rng, rc: rng.choice([None, True, False]))
        case.fake("orphaned_red", lambda rng, st, o_r, runs: rng.choice([None, None, "no owner", ""]))
        case.fake("owner_text", lambda rng, rc, short=False: "who")

        def delete(rng, *a):
            if rng.random() < 0.3:
                raise rng.choice([RuntimeError("no"), OSError("no")])
        case.fake("delete_remote_branch", delete)
        case.fake("log_deletion", lambda rng, *a: None)
        rf = r.choice([None, case.recorded("runs_for", runs)])
        return ("/repo", "o/r", r.random() < 0.4, [], "store", rf)

    def test_orphan_branches(self):
        def run(A):
            def go(repo, o_r, dry, out, store, rf):
                return A.orphan_branches(repo, o_r, dry, out, store, rf), out
            return go
        check(self, "orphan_branches", self.build, run, 5000)


class OwnerOfTest(unittest.TestCase):
    def build(self, case):
        r = case.rng
        clock(case, 1_000_000.0)
        branch = r.choice(["measure/u/abcdef1-x", "check/red-fast-" + "a" * 8, "check/verify-" + "b" * 40 + "-q1", "feature/x", "check/q2"])
        run = r.choice([{"databaseId": 5, "headSha": "c" * 40, "workflowName": "W"}, {"databaseId": None, "headSha": None},
                        {"databaseId": 6}])
        rec = r.choice([None, None, {"worktree": "/w/x", "pid": 3}, {"agent": "a"}, {"worktree": "/w/gone"}])
        old = r.choice([{}, {"source": "unmatched", "epoch": 1_000_000.0 - r.choice([10, 3600, 4000])}, {"source": "matched"}])
        case.fake("dispatch_for", lambda rng, b, rid=None: rec)
        case.fake("owner_gone", lambda rng, rc: rng.choice([None, True, False]))
        case.fake("commit_on_main", lambda rng, repo, base: rng.choice([None, True, False, False]))
        case.fake("worktree_holding", lambda rng, repo, sha: rng.choice([None, "/w/hold"]))
        case.fake("owner_text", lambda rng, rc, short=False: "who")
        case.fake("ts", lambda rng: "T")
        case.fake("dispatches_dir", lambda rng, name: "/d/" + name)
        case.patch(os.path, "exists", lambda rng, p: rng.random() < 0.5)
        case.fake("write_json", lambda rng, path, data: None)
        case.fake("read_record", lambda rng, path: old)
        case.fake("queue_entry_worktree", lambda rng, st, eid: rng.choice([None, "/w/q"]))
        case.fake("save_dispatch", lambda rng, rec_, key=None: rec_)
        return (r.choice(["/repo", None, ""]), "store", branch, run)

    def test_owner_of(self):
        check(self, "owner_of", self.build, lambda A: with_state(A.owner_of), 8000)


# ------------------------------------------------------------------------------------------------ runsigs
class NameCausesTest(unittest.TestCase):
    def build(self, case):
        r = case.rng
        recs = [{"run": r.choice([None, 11, 12]), "number": r.choice([None, 3, 4]), "workflow": "W",
                 "sigs": r.choice([None, [], [{"job": "j", "line": r.choice(["boom", "", None])}]])} for _ in range(r.randint(0, 3))]
        case.patch(glob, "glob", lambda rng, pat: [f"/r/{i}.json" for i in range(len(recs))])
        case.fake("reds_dir", lambda rng, *parts: "/reds/" + "/".join(parts))
        known = r.choice([{}, {"/reds/unknown-runs.json": {"77": "T"}}, None])

        def read(rng, path):
            if path.startswith("/r/"):
                return rng.choice([recs[int(path[3:-5])], recs[int(path[3:-5])], None])
            return (known or {}).get(path) if known is not None else None
        case.fake("read_record", read)

        def resolve(rng, o_r, v):
            return "Wf", rng.choice([None, {"status": "completed", "conclusion": "failure", "number": int(v), "databaseId": 900 + int(v)},
                                     {"status": "in_progress", "conclusion": None}, {"status": "completed", "conclusion": "success"}])
        case.fake("resolve_run", resolve)

        def sigs(rng, o_r, run, name):
            x = rng.random()
            if x < 0.2:
                raise RuntimeError("no log")
            if x < 0.3:
                raise SystemExit(1)
            return [{"job": "j", "line": rng.choice(["boom", "", "x"])}]
        case.fake("run_sigs", sigs)
        case.fake("write_json", lambda rng, path, data: None if rng.random() < 0.85 else (_ for _ in ()).throw(OSError("ro")))
        case.fake("ts", lambda rng: "T")
        values = r.choice([(), ("3",), ("77", "78"), ("12", "x", "5"), ("99",)])
        return (r.choice([None, "o/r", "o/r"]), values)

    def test_name_causes(self):
        check(self, "name_causes", self.build, lambda A: with_state(lambda *a: sorted(A.name_causes(*a, ctx=case_context()).items())), 6000)


if __name__ == "__main__":
    unittest.main()
