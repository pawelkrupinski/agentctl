"""RESTRUCTURE ROUND 16: the 24 functions cut into named steps keep their behaviour. The digests (fixtures/
restructure16-digests.json, recorded from the code BEFORE the cut -- 01e78e1's parent -- with `R4_RECORD=1`) cover
thousands of seeded cases each, collaborators faked (golden_cases.py): every call, its order and the result.
Every case is bounded."""
import builtins, contextlib, io, json, os, subprocess, sys, threading, unittest, urllib.error, urllib.request
from types import SimpleNamespace as NS

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402
from golden_cases import Probe, check as _check, stable  # noqa: E402
from test_restructure12 import NOW, maybe, printing  # noqa: E402

DIGESTS = os.path.join(HERE, "fixtures", "restructure16-digests.json")


def check(test, name, build, fn_of, n=2000):
    _check(test, A, name, build, fn_of, n, DIGESTS)


def check_out(test, name, build, call, n=2000):
    """check, with what the function prints in the case's log: call(A, *args)."""
    holder = {}

    def build_held(case):
        holder["case"] = case
        return build(case)
    _check(test, A, name, build_held, lambda A: printing(holder, lambda *a: call(A, *a)), n, DIGESTS)


def proc(rng, out="", rc=0, err=""):
    return NS(returncode=rc, stdout=out, stderr=err)


def raising(rng, p, value, exc=RuntimeError):
    if rng.random() < p:
        raise exc("down")
    return value


def raw(case, obj, attr, fn):
    """obj.attr replaced by fn (no recording: its arguments hold addresses), restored afterwards."""
    case.attrs.append((obj, attr, getattr(obj, attr)))
    setattr(obj, attr, fn)


class Fn:
    """A recorded function with a stable repr (a log line never holds an address)."""

    def __init__(self, case, name, gen):
        self.name, self.fn = name, case.recorded(name, gen)

    def __call__(self, *a, **k):
        return self.fn(*a, **k)

    def __repr__(self):
        return f"<{self.name}>"


def unrecorded(case, name, fn):
    """The agentctl name replaced by fn, its call logged by fn itself (its arguments hold closures), restored afterwards."""
    case.saved.setdefault(name, getattr(A, name))
    setattr(A, name, fn)


def pure(case, name, fn):
    """A read-only collaborator (an env var, a record read, `git cat-file`): its answer a function of its arguments alone,
    its calls not logged -- a refactor may read it once instead of twice, or later; what it writes and returns may not move."""
    unrecorded(case, name, fn)


class Slot(contextlib.AbstractContextManager):
    """check_slot's context: a slot whose env, sample and done are recorded."""

    def __init__(self, case):
        self.case = case
        self.env = Fn(case, "slot.env", lambda rng, e: {**e, "SLOT": "1"})
        self.sample = Fn(case, "slot.sample", lambda rng: None)
        self.done = Fn(case, "slot.done", lambda rng, rc, out: None)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    def __repr__(self):
        return "<slot>"


# ------------------------------------------------------------------------------------------------ maint
class OwnerOrphansTest(unittest.TestCase):
    def build(self, case):
        r = case.rng
        recs = [{"alive": r.random() < 0.5, "path": f"/o/{i}.json", "kind": r.choice(["red", "fix"]), "key": f"k{i}",
                 "owner": "o", "started": "s", "pid": r.choice([None, 0, 123, "456"]),
                 "code": r.choice([None, "", "aaaa1111bbbb", "cccc2222dddd"]), "cwd": r.choice([None, "/gone", "/here"])}
                for i in range(r.randint(0, 5))]
        case.fake("code_version", lambda rng: rng.choice([None, "", "aaaa1111bbbb", "eeee5555ffff"]))
        case.fake("owner_records", lambda rng: recs)
        case.patch(os.path, "isdir", lambda rng, p: p == "/here" or rng.random() < 0.3)
        case.patch(os, "unlink", lambda rng, p: raising(rng, 0.3, None, FileNotFoundError))
        case.patch(os, "kill", lambda rng, pid, sig: raising(rng, 0.3, None, ProcessLookupError))
        return (r.random() < 0.5,)

    def test_owner_orphans(self):
        check_out(self, "owner_orphans", self.build, lambda A, kill: A.owner_orphans(kill), 3000)


class SpotlightExcludeTest(unittest.TestCase):
    def build(self, case):
        r = case.rng
        repos = [f"/r{i}" for i in range(r.randint(0, 3))]
        trees = [f"/w{r.randint(0, 6)}" for _ in range(r.randint(0, 4))]
        case.fake("spotlight_info_exclude", lambda rng, repo: rng.random() < 0.8)
        case.fake("git", lambda rng, repo, *a, **k: proc(rng, "\n".join(["worktree " + t for t in trees] + ["bare", "HEAD abc"])))
        case.patch(os.path, "isdir", lambda rng, p: rng.random() < 0.8)
        case.patch(os.path, "exists", lambda rng, p: rng.random() < 0.4)
        case.patch(os.path, "realpath", lambda rng, p: p)
        case.patch(builtins, "open", lambda rng, p, mode="r": raising(rng, 0.2, Writer(case), OSError))
        cwds = Fn(case, "cwds", lambda rng: {i: rng.choice(["/w1", "/w2/x", "/elsewhere", "/w3"]) for i in range(rng.randint(0, 3))})
        return (r.choice([None, repos]), cwds)

    def test_spotlight_exclude(self):
        check(self, "spotlight_exclude", lambda case: self.build_fixed(case), lambda A: A.spotlight_exclude, 3000)

    def build_fixed(self, case):
        case.put("REPOS", ["/main1", "/main2"])
        return self.build(case)


# ------------------------------------------------------------------------------------------------ land
class Proc:
    pid = 777

    def __init__(self, wait, poll):
        self.wait, self.poll = wait, poll

    def __repr__(self):
        return "<proc>"


class WatchGroupTest(unittest.TestCase):
    def build(self, case):
        r = case.rng
        total = r.randint(0, 6)
        left = [total]

        def wait(rng, timeout):
            left[0] -= 1
            if left[0] < 0:
                return 0
            raise subprocess.TimeoutExpired("x", timeout)
        p = Proc(Fn(case, "p.wait", wait), Fn(case, "p.poll", lambda rng: rng.choice([None, None, None, 0])))
        t = [1000.0]

        def clk(rng):
            t[0] += rng.choice([0.5, 3, 20, 200, 1000])
            return t[0]
        sizes = [0]

        def size(rng, path):
            sizes[0] += rng.choice([0, 0, 10, 500])
            return sizes[0]
        cpu = [0.0]

        def cpu_of(rng, pid):
            cpu[0] += rng.choice([0, 0.1, 5, 40])
            return cpu[0]
        case.fake("env_num", lambda rng, name, default: rng.choice([0.5, 1, default]))
        case.fake("err", lambda rng, m: None)
        case.fake("watch_lease_loss", lambda rng, step, size, cpu, polls, quiet: rng.choice([None, None, None, "lease lost"]))
        case.fake("_end_group", lambda rng, p: None)
        case.patch(os.path, "getsize", size)
        sampler = r.choice([None, Fn(case, "sampler", lambda rng: None)])
        hooks = Fn(case, "hooks", lambda rng, pid: rng.random() < 0.4)
        return (p, "/out", "step", r.choice([4, 60, 1200]), sampler, Fn(case, "cpu_of", cpu_of), Fn(case, "clock", clk),
                r.choice([None, 100, 10 ** 4]), r.random() < 0.3, r.choice([None, 30, 300]), hooks)

    def test_watch_group(self):
        check(self, "watch_group", self.build, lambda A: A._watch_group, 3000)


# ------------------------------------------------------------------------------------------------ redhold
def red_items(r, n=4):
    out = {}
    for i in range(r.randint(0, n)):
        out[i] = {"id": f"r{i}", "state": r.choice(["red", "fix-pending", "blocked-external", "dispatch-off", "offered", "leased", "x"]),
                  "entered_at": NOW - r.choice([10, 100, 500, 9000]), "data": {"key": f"k{i}"}}
    return out


class RedUnheldTest(unittest.TestCase):
    def build(self, case):
        r = case.rng
        items = red_items(r)
        case.fake("red_active", lambda rng, store: items)
        case.fake("lease_is_live", lambda rng, row, t: rng.random() < 0.3)
        case.fake("red_lease", lambda rng, key: "red/" + key)
        case.fake("red_fix_held", lambda rng, it, t, facts, store: rng.random() < 0.5)
        case.fake("red_block_of", lambda rng, it, store: rng.choice([None, {"cause": "x"}]))
        case.fake("repo_dispatch_off", lambda rng, store, repo: rng.random() < 0.5)
        case.fake("red_repo_of", lambda rng, data: "o/r")
        offers = [None, {"id": "o1", "reason": "x"}, {"id": "o2", "reason": "its previous fixer stays the holder"}, {"id": "o3"}]
        st = Probe(case, "st", get=lambda rng, name: rng.choice([None, {"holder": "h"}]), clock=lambda rng: NOW,
                   offer_of=lambda rng, k: rng.choice(offers))
        return (st, r.choice([None, NOW]), r.choice([None, {"f": 1}]))

    def test_red_unheld(self):
        check(self, "red_unheld", self.build, lambda A: A.red_unheld, 3000)


# ------------------------------------------------------------------------------------------------ queueslots
class SetChangeTest(unittest.TestCase):
    def build(self, case):
        r = case.rng
        terminal = ("landed", "rejected", "superseded", "cancelled")
        states = ["queued", "checking", "checked", "pushing", "waiting"] + list(terminal)
        case.fake("joined_changes", lambda rng, d, e, st: [({"id": "q1"}, {"id": "q1"})] * rng.randint(0, 2))
        case.fake("machine_state", lambda rng, kind, s: NS(terminal=s in terminal))
        case.fake("change_item_fields", lambda rng, e: {"head": e.get("head")})
        case.fake("save_entry", lambda rng, d, e: None)
        case.fake("joined_verdicts", lambda rng, st, e, joined, to, event, data: None)

        def put_item(rng, kind, eid, state, data):
            return {"state": rng.choice(states), "version": rng.randint(1, 4), "data": rng.choice([None, {}, {"hold": {"why": "w"}}])}

        def transition(rng, kind, eid, to, version, data=None, event=None):
            if rng.random() < 0.3:
                raise A.StaleVersion("stale")
        st = Probe(case, "st", put_item=put_item, transition=transition,
                   item=lambda rng, kind, eid: {"state": rng.choice(states), "version": 9, "data": {}})
        e = {"id": "e1", "status": r.choice(["queued", "checking", "waiting", "", "joined", "landed"]), "head": "h"}
        fields = r.choice([{}, {"why": "w" * 700}, {"landed_sha": "s", "pushed_at": 5}, {"batch": None, "decided_on": "d"}, {"superseded_by": "x", "why": ""}])
        return (st, "/d", e, r.choice(states), r.choice([None, {"a": 1}]), r.choice([None, {"text": "t", "refs": {"r": 1}}]), fields)

    def test_set_change(self):
        def run(A):
            def go(st, d, e, to, data, event, fields):
                try:
                    got = A.set_change(st, d, e, to, data, event, **fields)
                except A.StaleVersion as ex:
                    got = "stale " + str(ex)
                return got, e
            return go
        check(self, "set_change", self.build, run, 3000)


# ------------------------------------------------------------------------------------------------ batchrun
class AssembleTest(unittest.TestCase):
    def build(self, case):
        r = case.rng
        entries = [{"id": f"e{i}", "commits": [f"c{i}"], "base": "b"} for i in range(r.randint(0, 5))]
        case.fake("git", lambda rng, wt, *a, **k: proc(rng, "", rng.choice([0, 1])) if "check" in k else f"tip{rng.randint(0, 9)}\n")

        def apply_entry(rng, d, wt, e, base, autoresolve=False):
            how = rng.choice(["ok", "ok", "conflict", "landed", "duplicate"])
            if how == "conflict":
                return how, rng.choice([["a.py"], ["a.py (generated: x)", "b"], []])
            return how, rng.choice([None, "twin", "via"])
        case.fake("apply_entry", apply_entry)
        for name in ("save_entry", "wlog", "already_landed"):
            case.fake(name, lambda rng, *a, **k: None)
        case.fake("queue_verdict", lambda rng, *a, **k: None)
        case.fake("conflict_cause", lambda rng, wt, ebase, base, files: rng.choice(["; because", "why"]))
        case.fake("conflict_paths", lambda rng, files: sorted(files))
        case.fake("remote", lambda rng: "origin")
        case.fake("supersede", lambda rng, *a: None)
        return (None, "/repo", "/d", "/wt", entries, "base", r.random() < 0.4, r.random() < 0.5)

    def test_assemble(self):
        def run(A):
            def go(st, repo, d, wt, entries, base, alone, autoresolve):
                return A.assemble(st, repo, d, wt, entries, base, alone, autoresolve), entries
            return go
        check(self, "assemble", self.build, run, 3000)


class BatchCheckTest(unittest.TestCase):
    def build(self, case):
        r = case.rng
        case.fake("precheck_hook", lambda rng, tree: rng.choice([None, "/h/hook", "/h/hook"]))
        case.fake("read_text_or", lambda rng, p, d: rng.choice(["--fast-only", "x", ""]))
        case.fake("host_role", lambda rng: rng.choice(["server", "sync", "local"]))
        case.fake("remote_checks_supported", lambda rng, tree: rng.random() < 0.7)

        def remote_check(rng, repo, e, base, tip, mode, clock, st, red=False):
            e["remote_run"] = rng.choice([None, 55])
            return rng.choice([True, False, None]), "out"
        case.fake("remote_check", remote_check)
        case.fake("git", lambda rng, *a, **k: "")
        case.fake("sbt_stop", lambda rng, tree: None)
        case.fake("wlog", lambda rng, d, m: None)
        case.fake("ts", lambda rng: "T")
        case.fake("tool_env", lambda rng: {"E": "1"})
        case.fake("check_slot", lambda rng, repo, label, cls: Slot(case))
        case.fake("run_watched", lambda rng, argv, cwd, env, log, step, sampler=None: (rng.choice([0, 0, 1, 75, None]),
                                                                                        rng.choice(["ok", "STALLED: x", "", None])))
        case.patch(builtins, "open", lambda rng, p, mode="r": Writer(case))
        runs = r.choice([None, {}])
        depth = r.choice(["fast", "full", "verify", "red", "x"])
        return ("/repo", "/d", "/tree", "base", "tip1234567890", Fn(case, "clock", lambda rng: NOW), None,
                r.choice(["check", "red"]), depth, runs, r.choice([None, "rb"]))

    def test_batch_check(self):
        def run(A):
            def go(repo, d, tree, base, tip, clock, st, cls, depth, runs, rb):
                return A.batch_check(repo, d, tree, base, tip, clock, st, cls=cls, depth=depth, runs=runs, range_base=rb), runs
            return go
        check(self, "batch_check", self.build, run, 3000)


class Writer(contextlib.AbstractContextManager):
    def __init__(self, case):
        self.write = Fn(case, "file.write", lambda rng, s: None)
        self.close = Fn(case, "file.close", lambda rng: None)

    def __repr__(self):
        return "<file>"

    def __exit__(self, *exc):
        return None


# ------------------------------------------------------------------------------------------------ live
class LiveStateTest(unittest.TestCase):
    def build(self, case):
        r = case.rng
        case.fake("epoch_now", lambda rng: NOW)
        case.fake("proc_alive", lambda rng, pid, start: raising(rng, 0.2, rng.choice([True, False, None]), rng.choice([RuntimeError, SystemExit])))
        e = {k: v for k, v in {"ended": r.choice([None, 1]), "rc": r.choice([None, 0, 1]), "verdict": r.choice([None, "v"]),
                               "pid": r.choice([None, 0, 12]), "process_start": r.choice([None, 5]), "finished": r.choice([None, True])}.items()
             if r.random() < 0.8}
        info = {k: v for k, v in {"stop": r.choice([None, "end_turn", "tool_use"]), "api_error": r.choice([False, False, True]),
                                  "mtime": r.choice([None, NOW - 10, NOW - 9999, NOW - A.LIVE_STALL])}.items() if r.random() < 0.9}
        return (e, info, r.choice([None, NOW, NOW + 5]))

    def test_live_state(self):
        check(self, "live_state", self.build, lambda A: A.live_state, 3000)


# ------------------------------------------------------------------------------------------------ laptoprole
def lease_row(r):
    if r.random() < 0.2:
        return None
    return {"name": "laptop-role", "holder": r.choice([None, "", "me", "other", "laptop:x", "laptop:x"]), "epoch": r.choice([1, 2]), "secret": "s",
            "expires_at": NOW + r.choice([-100, 0, 500, 500]), "acquired_at": NOW - 5,
            "meta": r.choice([None, "", "{}", '{"host": "h", "since": 5}'])}


class LaptopRoleTest(unittest.TestCase):
    def build(self, case):
        r = case.rng
        case.fake("laptop_role_holder", lambda rng: "me")
        case.fake("role_host", lambda rng: "host")
        case.fake("machine_id", lambda rng: "mid")
        pure(case, "env_num", lambda n, d: d)
        pure(case, "laptop_role_path", lambda: "/state/laptop-role.json")
        mine = r.choice([None, {}, {"epoch": 1}, {"epoch": 2, "secret": "z"}])
        pure(case, "read_record", lambda p: mine)
        case.fake("write_json", lambda rng, p, d: None)

        def acquire(rng, name, me, ttl, mx, progress=None, meta=None):
            return rng.choice([{"held": {"holder": "o"}}, {"epoch": 3, "secret": "q"}])
        st = Probe(case, "st", clock=lambda rng: NOW, get=lambda rng, name: lease_row(rng), acquire=acquire,
                   renew=lambda rng, *a: rng.choice(["ok", "no_progress", "lost"]), revoke=lambda rng, *a: None)
        return (st, r.choice([None, NOW]), r.random() < 0.5)

    def test_laptop_role(self):
        check(self, "laptop_role", self.build, lambda A: A.laptop_role, 3000)


class LaptopRoleRefusalTest(unittest.TestCase):
    def build(self, case):
        r = case.rng
        case.fake("role_host", lambda rng: "host")
        st = Probe(case, "st", get=lambda rng, name: lease_row(rng), clock=lambda rng: NOW)
        method = r.choice(["acquire", "take", "revoke", "get", "renew"])
        path = r.choice(["/v1/store/" + method] * 6 + ["/v1/other"])
        holder = r.choice(["laptop:fix", "laptop:claim:x", "laptop:fix", "laptop:y", "red:1", None, "me", 5])
        args = r.choice([[], ["n"], ["n", holder], ["n", holder], ["n", holder], [holder], ["laptop-role"], ["laptop-role"]])
        kw = r.choice([{}, {"holder": holder}, {"name": "n"}, {"name": r.choice(["laptop:red", 5])}, {"name": "laptop:r", "holder": holder}])
        body = r.choice([{"args": args, "kwargs": kw}, {"args": args}, {"kwargs": kw}, {}, None, [1]])
        return (st, path, body, r.choice([None, "me", "other", "laptop:x"]))

    def test_laptop_role_refusal(self):
        check(self, "laptop_role_refusal", self.build, lambda A: A.laptop_role_refusal, 4000)


# ------------------------------------------------------------------------------------------------ projector
class ProjectChangesTest(unittest.TestCase):
    def build(self, case):
        r = case.rng
        every = [{"id": f"e{i}", "status": r.choice(["queued", "checking", "waiting", "joined", "bogus", "landed"]),
                  "waiting_id": r.choice([None, "e0", "e1", "zz"])} for i in range(r.randint(0, 5))]
        case.fake("waiting_entries", lambda rng, d, open_only=True: every)
        case.fake("change_item_fields", lambda rng, e: {"head": e["id"]})
        case.fake("machine_state", lambda rng, k, s: NS(terminal=s in ("landed", "rejected")))
        case.fake("projected_transition", lambda rng, e, w, cur, state: ({"d": 1}, {"kind": "k"}))
        states = ["queued", "checking", "waiting", "landed", "rejected"]

        def transition(rng, *a, **k):
            if rng.random() < 0.3:
                raise rng.choice([A.StaleVersion, A.TerminalState])("x")
        st = Probe(case, "st", put_item=lambda rng, kind, eid, state, data: {"id": eid, "state": rng.choice(states + [state]), "version": 2},
                   clock=lambda rng: NOW, meta_get=lambda rng, k, d=None: rng.choice([0, None, "3.5"]),
                   transition=transition, item=lambda rng, kind, eid: {"id": eid, "state": "queued", "version": 3})
        return ("/d", st)

    def test_project_changes(self):
        check(self, "project_changes", self.build, lambda A: A.project_changes, 3000)


# ------------------------------------------------------------------------------------------------ remotestore
class CallOnceTest(unittest.TestCase):
    def build(self, case):
        r = case.rng
        case.fake("contact_machine", lambda rng: rng.choice([None, "m1"]))
        case.fake("host_role", lambda rng: rng.choice(["sync", "server"]))
        case.fake("laptop_role_holder", lambda rng: "me")
        kind = r.choice(["ok", "ok", "empty", "badjson", "http", "urlerr", "oserr", "valerr", "httpexc"])
        code = r.choice([400, 401, 403, 404, 409, 500, 502, 503, 504])
        body = r.choice([b"", b"{}", b'{"message": "m"}', b'{"error": "StaleVersion", "message": "s"}', b'{"error": "nope"}', b"not json"])
        reason = r.choice([ConnectionRefusedError("r"), ConnectionResetError("x"), "timed out"])

        def urlopen(req, timeout=None):
            case.log.append(["urlopen", req.full_url, req.get_method(), stable(req.data), sorted(req.header_items()), timeout])
            if kind == "http":
                raise urllib.error.HTTPError(req.full_url, code, "reason", {}, io.BytesIO(body))
            if kind == "urlerr":
                raise urllib.error.URLError(reason)
            if kind == "oserr":
                raise OSError("os")
            if kind == "valerr":
                raise ValueError("v")
            if kind == "httpexc":
                import http.client
                raise http.client.BadStatusLine("b")
            payload = {"ok": b'{"result": [1]}', "empty": b"", "badjson": b"{{"}[kind]
            return Reader(payload)
        raw(case, urllib.request, "urlopen", urlopen)
        store = A.RemoteStore("http://s/", tok="tok")
        return (store, r.choice(["GET", "POST"]), r.choice(["/v1/state", "/v1/x:y"]), r.choice([None, {"a": [1]}]), r.choice([None, 5]))

    def test_call_once(self):
        def run(A):
            def go(store, method, path, body, timeout):
                try:
                    return store._call_once(method, path, body, timeout)
                except Exception as e:
                    return type(e).__name__, str(e), getattr(e, "unsent", None)
            return go
        check(self, "call_once", self.build, run, 4000)


class Reader(contextlib.AbstractContextManager):
    def __init__(self, payload):
        self.payload = payload

    def read(self):
        return self.payload

    def __exit__(self, *exc):
        return None


# ------------------------------------------------------------------------------------------------ syncsteps
class SyncStepsTest(unittest.TestCase):
    def build(self, case):
        r = case.rng
        for name in ("red_renew_holders", "red_reap_orphans", "reap_children", "project_events", "notify_escalations",
                     "reconcile_alerts", "network_watch", "tunnel_watch", "maybe_recovery_tick", "mirror_state", "flush_outbox"):
            case.fake(name, lambda rng, *a, **k: rng.choice([None, 1]))
        for name in ("trust_asks", "close_landed_worktrees", "migrate_local_queue", "close_landed_changes", "answer_regen_holds",
                     "revert_notes", "fixers_land_watch", "maybe_upkeep", "root_checkout_ff", "explorer_tick"):
            case.fake(name, lambda rng, *a, **k: [f"x{i}" for i in range(rng.randint(0, 2))])
        case.fake("red_dispatcher", lambda rng, *a: rng.choice([[], ["d"]]))
        case.fake("laptop_role_step", lambda rng, store: rng.choice([True, False, True]))
        case.fake("refresh_pinned_recording", lambda rng, *a, **k: None)
        case.fake("write_red_status_snapshot", lambda rng, store: None)

        def run_bounded(name, fn, bound):
            case.log.append(["run_bounded", name, bound])
            fn()
            return case.rng.random() < 0.8
        unrecorded(case, "run_bounded", run_bounded)
        case.fake("err", lambda rng, m: None)
        case.fake("env_num", lambda rng, n, d: rng.choice([0, d]))
        case.fake("reds_dir", lambda rng, *a: "/reds/" + "/".join(a))
        case.patch(os.path, "exists", lambda rng, p: rng.random() < 0.4)
        t = [100.0]

        def tick(rng):
            t[0] += rng.choice([0, 1, 5000])
            return t[0]
        clock = NS(time=Fn(case, "clock.time", tick))
        return ("/repo", "/d", "o/r", clock, Probe(case, "store"), [r.randint(0, 40) for _ in range(r.randint(1, 30))])

    def test_sync_steps(self):
        def run(A):
            def go(repo, d, o_r, clock, store, picks):
                steps = list(A.sync_steps(repo, d, o_r, clock, store))
                out = [name for name, _fn in steps]
                for k in picks:
                    name, fn = steps[k % len(steps)]
                    out.append((name, stable(fn())))
                return out
            return go
        check(self, "sync_steps", self.build, run, 2500)


# ------------------------------------------------------------------------------------------------ statuspage
class StatusPageStartTest(unittest.TestCase):
    def build(self, case):
        r = case.rng
        case.put("_status_page", r.choice([{}, {}, {"url": "http://already/"}]))
        env = {}
        if r.random() < 0.3:
            env["AGENTCTL_STATUS_PORT"] = r.choice(["3015", "8080", "80"])
        if r.random() < 0.2:
            env["AGENTCTL_STATUS_PAGE"] = r.choice(["off", "NO", "on", "0"])
        raw(case, os, "environ", env)
        case.fake("port_open", lambda rng, port, timeout=0.5: rng.random() < 0.4)
        case.fake("status_page_server", lambda rng, host, port: raising(rng, 0.3, Probe(case, "srv"), rng.choice([OSError, ValueError])))
        case.fake("err", lambda rng, m: None)
        case.fake("write_json", lambda rng, p, d: raising(rng, 0.2, None, OSError))
        case.fake("status_page_file", lambda rng: "/state/status-page.json")
        case.patch(os, "getpid", lambda rng: 4242)
        case.fake("epoch_now", lambda rng: NOW)

        class FakeThread:
            def __init__(self, target=None, daemon=None, name=None):
                case.log.append(["Thread", name, daemon])

            def start(self):
                case.log.append(["start"])
        raw(case, threading, "Thread", FakeThread)
        return ()

    def test_status_page_start(self):
        def run(A):
            def go():
                got = A.status_page_start()
                return got, A._status_page.get("url"), sorted(A._status_page)
            return go
        check(self, "status_page_start", self.build, run, 3000)


# ------------------------------------------------------------------------------------------------ issues
class CmdIssueTest(unittest.TestCase):
    def build(self, case):
        r = case.rng
        case.fake("claimant", lambda rng, *a: "me")
        case.fake("safe_cwd", lambda rng: "/cwd")
        case.fake("issue_close", lambda rng, st, iid, note, by: rng.choice([None, {"id": iid, "state": "closed"}]))
        case.fake("issue_open", lambda rng, st, text, by, cwd=None: raising(rng, 0.3, {"id": "i1"}, rng.choice([ValueError, A.StoreUnavailable]))
                  if rng.random() < 0.5 else {"id": "i1"})
        rows = [{"id": f"i{k}", "state": r.choice(["open", "closed"]), "data": {"by": r.choice([None, "b"]), "text": "t" * r.choice([1, 400])}}
                for k in range(r.randint(0, 3))]
        st = Probe(case, "st", items=lambda rng, kind, states=None: rows)
        a = NS(close=r.choice([None, "i1"]), note=r.choice([None, "n"]), list=r.random() < 0.3, all=r.random() < 0.3,
               text=r.choice([[], ["a", "b"], None]))
        return (a, st)

    def test_cmd_issue(self):
        check_out(self, "cmd_issue", self.build, lambda A, a, st: A.cmd_issue(a, st), 3000)


# ------------------------------------------------------------------------------------------------ localrecheck
class RunLocalRecheckTest(unittest.TestCase):
    def build(self, case):
        r = case.rng
        checks = {f"c{i}": {"first_main": r.choice([None, "", "f1", "f2", "f3"]), "lines": ["l"]} for i in range(r.randint(0, 3))}
        case.fake("local_red", lambda rng: {"checks": checks})
        case.fake("precheck_hook", lambda rng, repo: rng.choice([None, "/repo/hooks/pp", "/repo/hooks/pp"]))
        pure(case, "git_ok", lambda repo, *a: a[-1].startswith(("f1", "f3")))
        case.fake("git", lambda rng, repo, *a, **k: proc(rng, "", rng.choice([0, 0, 1])) if "check" in k else f"sha{rng.randint(0, 5)}\n")
        case.fake("remote", lambda rng: "origin")
        case.fake("waiting_state", lambda rng, repo: ("/d", "/clone", None))
        case.fake("worktrees", lambda rng, repo: [{"worktree": "/repo"}] + [{"worktree": f"/clone-wt-recheck-{rng.randint(1, 3)}"}
                                                                          for _ in range(rng.randint(0, 3))])
        pure(case, "pid_alive", lambda pid: pid % 2 == 0)
        case.fake("mark_internal", lambda rng, t: t)
        case.fake("reds_dir", lambda rng, *a: "/reds/" + "/".join(a))
        case.fake("err", lambda rng, m: None)
        case.fake("hook_checks", lambda rng, h: ["c0", "c1", "other"])
        case.fake("tool_env", lambda rng: {"E": "1"})
        case.fake("check_slot", lambda rng, repo, label, cls: Slot(case))
        case.fake("run_watched", lambda rng, argv, cwd, env, log, step, sampler=None: (rng.choice([0, 1, None]), "out"))
        case.fake("apply_local_recheck", lambda rng, repo, main, rc, out: [f"applied {rc}"])
        case.patch(os, "makedirs", lambda rng, p, exist_ok=False: None)
        case.patch(os, "getpid", lambda rng: 4242)
        return ("/repo", "main1234567890")

    def test_run_local_recheck(self):
        check(self, "run_local_recheck", self.build, lambda A: A.run_local_recheck, 3000)


# ------------------------------------------------------------------------------------------------ orphanreap
class BgSpentTest(unittest.TestCase):
    def build(self, case):
        r = case.rng
        names = ["red owner · a", "red fixer #3", "orphan · x", "lander · y", "mine", None]
        rows = [r.choice([{"kind": r.choice(["background", "x"]), "name": r.choice(names), "id": r.choice(["", "b1", "b2", None]),
                           "sessionId": r.choice([None, "b1xyz", "q"]), "startedAt": r.choice([None, 0, (NOW - 50) * 1000, (NOW - 9000) * 1000, (NOW - A.BG_SPENT_GRACE) * 1000])},
                          "junk", None]) for _ in range(r.randint(0, 6))]
        case.fake("env_num", lambda rng, n, d: rng.choice([d, 0, 99999]))
        case.fake("bg_waiting", lambda rng, x: rng.random() < 0.6)
        return (rows, set(r.sample(["b1", "b2", "b3"], r.randint(0, 2))), NOW)

    def test_bg_spent(self):
        check(self, "bg_spent", self.build, lambda A: A.bg_spent, 4000)


# ------------------------------------------------------------------------------------------------ recordingpins
class RefreshPinnedTest(unittest.TestCase):
    def build(self, case):
        r = case.rng
        case.fake("epoch_now", lambda rng: NOW)
        case.fake("pinned_path", lambda rng: "/state/pins.json")
        case.fake("read_record", lambda rng, p: rng.choice([None, {}, {"at": NOW - 10, "pins": {"uk": "5"}, "stored": True},
                                                            {"at": NOW - 99999, "pins": {"uk": "5"}}, {"at": NOW - 99999, "pins": {"uk": "5"}, "stored": True},
                                                            {"at": NOW - 99999, "pins": {"uk": "7"}, "tag": "t", "stored": True}]))
        case.fake("host_role", lambda rng: rng.choice(["server", "sync", "local"]))
        case.fake("write_json", lambda rng, p, d: None)
        case.fake("pin_release_tag", lambda rng, repo: rng.choice([None, "tag1"]))
        case.fake("read_pins", lambda rng, o_r, tag: raising(rng, 0.2, rng.choice([None, {}, {"uk": "9"}]), rng.choice([RuntimeError, SystemExit])))
        case.fake("err", lambda rng, m: None)
        case.put("_pin_tried", [r.choice([0.0, NOW - 10, NOW - 99999])])
        stored = [None, "", json.dumps({"at": 1, "repo": "o", "tag": "t", "pins": {"uk": "5"}}), json.dumps({"at": 2, "tag": "t", "pins": {"uk": "6"}, "repo": "o"}),
                  "{{", json.dumps({"pins": {}})]
        st = Probe(case, "st", meta_get=lambda rng, k, d=None: raising(rng, 0.1, rng.choice(stored)),
                   meta_set=lambda rng, k, v: raising(rng, 0.2, None))
        return ("o/r", "/repo", st, r.choice([None, NOW]), r.random() < 0.3, r.choice([None, "server", "sync"]))

    def test_refresh_pinned_recording(self):
        def run(A):
            def go(*a):
                return A.refresh_pinned_recording(*a), A._pin_tried[0]
            return go
        check(self, "refresh_pinned_recording", self.build, run, 4000)


# ------------------------------------------------------------------------------------------------ waiting
class ApplyEntryTest(unittest.TestCase):
    def build(self, case):
        r = case.rng
        commits = [f"c{i}" for i in range(r.randint(0, 4))]
        case.fake("git", lambda rng, wt, *a, **k: rng.choice(["base", "other"]) + "\n")

        def pick(rng, wt, cs, dropped, regen, regenerated, provisional, resolved):
            for c in cs:
                if rng.random() < 0.4:
                    dropped.append((c, rng.choice([None, "via1"])))
            if rng.random() < 0.3:
                regenerated.append("g.json")
            if rng.random() < 0.3:
                provisional.append("p.tsv")
            if resolved is not None and rng.random() < 0.3:
                resolved.append({"file": "f", "hunks": ["1"]})
            return rng.choice([[], [], ["x.py"]])
        case.fake("pick", pick)
        case.fake("wlog", lambda rng, d, m: None)
        case.fake("dropped_on_main", lambda rng, wt, dropped, base, first: rng.choice([[], [("c0", "v")]]))
        case.fake("drop_landed", lambda rng, d, e, on_main: rng.choice([None, ("landed", "sha")]))
        e = {"id": "e1", "commits": commits, "autoresolved": "old", "regenerated": ["z"], "provisional": ["q"]}
        return ("/d", "/wt", e, "base", r.random() < 0.5, r.random() < 0.5)

    def test_apply_entry(self):
        def run(A):
            def go(d, wt, e, base, regen, autoresolve):
                return A.apply_entry(d, wt, e, base, regen, autoresolve), e
            return go
        check(self, "apply_entry", self.build, run, 4000)


# ------------------------------------------------------------------------------------------------ queueclose
class RegenHoldOverdueTest(unittest.TestCase):
    def build(self, case):
        r = case.rng
        case.fake("env_num", lambda rng, n, d: rng.choice([d, 100]))
        case.fake("save_entry", lambda rng, d, e: None)
        case.fake("wlog", lambda rng, d, m: None)
        case.fake("machine_state", lambda rng, k, s: NS(terminal=s in ("landed", "rejected")))
        case.fake("fmt_age", lambda rng, s: f"{s:.0f}s")
        data = [None, {}, {"regen_round": 1}, {"regen_round": 2, "regen_answered": 2}, {"regen_answered": 1}]

        def item(rng, kind, eid):
            return rng.choice([None, {"id": eid, "state": rng.choice(["queued", "landed"]), "version": 3, "data": rng.choice(data)}])

        def maybe_stale(rng, *a, **k):
            if rng.random() < 0.3:
                raise A.StaleVersion("s")
        st = Probe(case, "st", item=item, update_item=maybe_stale, transition=maybe_stale)
        since = NOW - r.choice([10, A.CONFLICT_PLACE_KEPT + 5, A.CONFLICT_PLACE_KEPT * 3])
        h = r.choice([None, {}, {"since": since, "files": ["g.json"], "cmd": "regen"},
                      {"since": since, "files": [], "cmd": None, "recovery": {"at": NOW - 50, "round": 1}},
                      {"since": since, "files": ["a"], "cmd": "c", "recovery": {"at": NOW - 99999, "round": 2}},
                      {"since": since, "files": ["a"], "recovery": {"at": NOW - 99999, "escalated": 5}}])
        e = {"id": "e1", **({"regen_hold": h} if h is not None else {})}
        return (st, "/d", e, NOW)

    def test_regen_hold_overdue(self):
        def run(A):
            def go(st, d, e, t):
                return A.regen_hold_overdue(st, d, e, t), e
            return go
        check(self, "regen_hold_overdue", self.build, run, 4000)


# ------------------------------------------------------------------------------------------------ buildview
class BuildViewChangeTest(unittest.TestCase):
    def build(self, case):
        r = case.rng
        mods = ["worker", "web", "common", "."]
        case.fake("git_ok", lambda rng, tree, *a: rng.random() < 0.8)
        case.fake("read_build", lambda rng, tree, tip: rng.choice([None, {"projects": {m: f"p{m}" for m in mods}}]))
        keys = ["def:worker", "global:x", "member:O:m", "file:f", "header:h", "weird"]

        def build_view(rng, tree, tip, modules):
            if rng.random() < 0.15:
                return None
            return {k: rng.choice(["a", "b"]) for k in keys if rng.random() < 0.7}
        case.fake("build_view", build_view)
        paths = r.choice([[], ["worker/a.scala"], ["build.sbt"], ["web/x", "docs/y", "project/z.sbt"], ["README"]])
        selection = r.choice([None, {}, {"--compile-plan": ["worker/x"], "--x": ["web/y common/z"]}, {"--a": None}])
        return ("/tree", r.choice([None, "", "full1"]), "tip", paths, selection)

    def test_build_view_change(self):
        check(self, "build_view_change", self.build, lambda A: A.build_view_change, 4000)


# ------------------------------------------------------------------------------------------------ redclaim
class RedClaimTest(unittest.TestCase):
    def build(self, case):
        r = case.rng
        case.fake("own_fixer_record_path", lambda rng: rng.choice([None, "/fixer/rec.json"]))
        case.fake("safe_cwd", lambda rng: "/cwd")
        case.fake("read_record", lambda rng, p: rng.choice([{}, {"run": 5, "lease": "red/k1", "note": "n", "worktree": "/fixer-wt"},
                                                            {"run": 6, "lease": "red/k2"}, None]))
        case.fake("write_json", lambda rng, p, d: None)
        items = [{"id": f"r{i}", "data": {"key": f"k{i}", "gate": r.random() < 0.5}} for i in range(r.randint(0, 2))]
        case.fake("red_targets", lambda rng, store, o_r, target, create=True: items)
        case.fake("red_noted", lambda rng, store, its, note: None)
        case.fake("worktree_of", lambda rng, here: rng.choice([None, "/wt-of-here"]))
        case.fake("root_checkout", lambda rng, wt, o_r: rng.random() < 0.2)
        case.fake("claimant", lambda rng, *a: "me")
        case.fake("tilde", lambda rng, p: p)
        case.fake("red_what", lambda rng, d: "red " + d["key"])
        case.fake("red_lease", lambda rng, key: "red/" + key)
        case.fake("red_wt_mark", lambda rng, wt: "mark")
        case.fake("lease_is_live", lambda rng, row, t: bool(row) and rng.random() < 0.5)
        case.fake("epoch_now", lambda rng: NOW)
        case.fake("lease_meta", lambda rng, row: {})
        case.patch(os.path, "realpath", lambda rng, p: p)
        case.patch(os.path, "isdir", lambda rng, p: rng.random() < 0.8)

        def get(rng, name):
            return rng.choice([None, {"holder": "laptop:claim:me@/wt-of-here", "name": name, "epoch": 1, "secret": "s"},
                               {"holder": "other", "name": "red/k1", "epoch": 1, "secret": "s"}])

        def acquire(rng, name, holder, ttl, mx, progress=None, item=None, meta=None):
            return rng.choice([{"ok": 1}, {"held": {"holder": "o", "expires_at": 86400 * 3}}])
        st = Probe(case, "st", get=get, clock=lambda rng: NOW, renew=lambda rng, *a, **k: "ok", acquire=acquire, close_offer=lambda rng, *a: None)
        return (st, "o/r", r.choice(["#5", "5", "k1"]), r.choice(["", "note"]), r.choice([None, "/given-wt"]))

    def test_red_claim(self):
        check(self, "red_claim", self.build, lambda A: A.red_claim, 4000)


# ------------------------------------------------------------------------------------------------ gating
class GateFlowNamesTest(unittest.TestCase):
    def build(self, case):
        r = case.rng
        files = r.sample(["a.yml", "b.yml", "c.yml", "d.yml"], r.randint(0, 4))
        case.fake("main_workflow_files", lambda rng, o_r: {rng.choice(["A", "B"]): rng.choice(["a.yml", "b.yml"]), "C": "c.yml"})
        case.fake("api_workflow_names", lambda rng, o_r: rng.choice([{}, {"a.yml": "ApiA", "d.yml": "D"}]))
        kept = {"o/r": {"names": {"a.yml": "KeptA", "c.yml": "C", "d.yml": "KeptD"}}}
        case.fake("read_record", lambda rng, p: rng.choice([None, {}, kept, {"o/r": {}}]))
        case.fake("write_json", lambda rng, p, d: raising(rng, 0.2, None, OSError))
        case.fake("gating_checks_path", lambda rng: "/state/gc.json")
        return ("o/r", r.choice([files, tuple(files)]))

    def test_gate_flow_names(self):
        check(self, "gate_flow_names", self.build, lambda A: A.gate_flow_names, 4000)


# ------------------------------------------------------------------------------------------------ remotejobs
class JobsVerdictTest(unittest.TestCase):
    def build(self, case):
        r = case.rng
        names = ["check (a)", "check (b)", "report", "publish", "wf / inner", ""]
        jobs = [{k: v for k, v in {"name": r.choice(names), "status": r.choice(["completed", "queued", "in_progress", None]),
                                   "conclusion": r.choice(["success", "failure", "skipped", "neutral", "timed_out", "cancelled", None])}.items()
                 if r.random() < 0.95} for _ in range(r.randint(0, 6))]
        globs = r.sample(["check (*)", "report", "wf*", "x"], r.randint(0, 3))
        expect = r.sample(["check (a)", "check (${{ m }})", "wf", "missing"], r.randint(0, 3))
        return (jobs, globs, r.choice([expect, tuple(expect)]))

    def test_jobs_verdict(self):
        check(self, "jobs_verdict", self.build, lambda A: A.jobs_verdict, 5000)


if __name__ == "__main__":
    unittest.main()
