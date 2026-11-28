"""RESTRUCTURE ROUND 15: each function split into named stages keeps its behaviour. The digests (fixtures/
restructure15-digests.json, recorded from the code BEFORE the split with `R4_RECORD=1`) cover thousands of seeded cases
each, collaborators faked (golden_cases.py): every call, its order and the result. Every case is bounded."""
import contextlib, io, json, os, sys, types, unittest
from functools import partial
from types import SimpleNamespace as NS

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402
from harness import case_context  # noqa: E402
from golden_cases import Probe, check as _check  # noqa: E402
from test_restructure12 import NOW, maybe, printing  # noqa: E402
from test_restructure13 import pick  # noqa: E402

DIGESTS = os.path.join(HERE, "fixtures", "restructure15-digests.json")


def check(test, name, build, fn_of, n=3000):
    _check(test, A, name, build, fn_of, n, DIGESTS)


def proc(rng, out="", rc=0, err=""):
    return NS(returncode=rc, stdout=out, stderr=err)


class Scope(contextlib.AbstractContextManager):
    def __exit__(self, *exc):
        return None

    def __repr__(self):
        return "<scope>"


def raising(rng, p, value, exc=RuntimeError):
    if rng.random() < p:
        raise exc("down")
    return value


# ------------------------------------------------------------------------------------------------ explorer
class ExplorerTickTest(unittest.TestCase):
    def build(self, case):
        r = case.rng
        rows = [{"country": r.choice("ab"), "venue": r.choice("vw"), "rawTitle": f"t{r.randint(0, 3)}", "seed": str(r.randint(0, 3))}
                for _ in range(r.randint(0, 5))]
        case.fake("epoch_now", lambda rng: NOW)
        case.fake("remote", lambda rng: "origin")
        case.fake("git", lambda rng, *a, **k: proc(rng))
        case.fake("explorer_parse", lambda rng, raw: raising(rng, 0.15, rows, ValueError))
        case.fake("explorer_backlog", lambda rng, repo: rng.choice(["", "a v t0", "b w t1 b w t2"]))
        case.fake("explorer_named", lambda rng, backlog, row: rng.random() < 0.3)
        case.fake("explorer_iid", lambda rng, row: f"{row['country']}:{row['venue']}|{row['rawTitle']}")
        case.fake("forge_release_asset", lambda rng, o_r, tag, asset: raising(rng, 0.1, "raw"))
        stored = {f"{c}:{v}|t{n}": {"id": f"{c}:{v}|t{n}", "version": 3, "data": {}} for c in "ab" for v in "vw" for n in range(4)}
        opened = [{"id": r.choice(list(stored)), "version": r.randint(1, 5), "data": r.choice([{}, {"venue": "v", "country": "a", "rawTitle": "t0"}])}
                  for _ in range(r.randint(0, 3))]
        st = Probe(case, "st", meta_get=lambda rng, k, d=None: r.choice([0, 0, NOW - 10, NOW - 7200, None, "5"]),
                   items=lambda rng, typ, states: opened,
                   item=lambda rng, typ, iid: maybe(rng, stored[iid], 0.2),
                   transition=lambda rng, *a: raising(rng, 0.1, None, SystemExit))
        filer = lambda o_r, repo, new, t: raising(r, 0.1, r.choice([None, "/wt/e", ""]))  # noqa: E731
        fetch = r.choice([None, lambda o_r, tag, asset: raising(r, 0.1, "raw", SystemExit)])
        return (case_log_wrap(case, "o/r", "/repo", st, r.choice([None, NOW]), r.random() < 0.3, fetch, filer))

    def test_explorer_tick(self):
        check(self, "explorer_tick", self.build, lambda A: lambda *a: A.explorer_tick(*a), 3000)


def case_log_wrap(case, *args):
    return args


# ------------------------------------------------------------------------------------------------ conflicts
class ConflictResolveTest(unittest.TestCase):
    def build(self, case):
        r = case.rng
        files = r.sample(["a.md", "g.json", "s.py", "b.md", "gen.txt", "t.py"], r.randint(0, 5))
        case.fake("unmerged", lambda rng, wt: files)
        case.fake("generated_entry", lambda rng, wt, f: f in ("g.json", "gen.txt"))
        case.fake("union_safe", lambda rng, f: f.endswith(".md"))
        case.fake("source_resolve", lambda rng, wt, fs, resolved: rng.random() < 0.7)
        case.fake("union_resolve", lambda rng, wt, fs: rng.choice([[], None, ["x"]]))
        case.fake("regenerate", lambda rng, wt, fs, regenerated, provisional: rng.choice([None, "", "why not"]))
        notes = r.choice([None, []])
        return ("/wt", r.random() < 0.6, notes, r.choice([None, []]), r.choice([None, []]), r.choice([None, []]))

    def test_conflict_resolve(self):
        def run(A):
            def go(wt, regen, notes, regenerated, provisional, resolved):
                got = A.conflict_resolve(wt, regen, notes, regenerated, provisional, resolved)
                return got, notes
            return go
        check(self, "conflict_resolve", self.build, run, 4000)


# ------------------------------------------------------------------------------------------------ queuesubmit
class SweepLandRefsTest(unittest.TestCase):
    def build(self, case):
        r = case.rng
        ids = [f"2026110{r.randint(1, 5)}-120000-{k}" for k in range(4)]
        refs = [f"abc\trefs/heads/land/{r.choice(ids)}" for _ in range(r.randint(0, 5))]
        case.fake("epoch_now", lambda rng: NOW)
        case.fake("remote", lambda rng: "origin")
        case.fake("env_number", lambda rng, name, default: rng.choice([default, 5, 100000]))
        case.fake("entry_id_age", lambda rng, eid, t: rng.choice([None, 10, 10 ** 6]))
        case.fake("waiting_entries", lambda rng, d, open_only=True: [{"id": r.choice(ids), "status": r.choice(["queued", "final", "landed"])}
                                                                      for _ in range(r.randint(0, 3))])
        case.fake("entry_final", lambda rng, s: s in ("final", "landed"))
        case.fake("machine_state", lambda rng, m, s: NS(terminal=s in ("landed", "dead")))
        case.fake("git", lambda rng, repo, *a, **k: proc(rng, "\n".join(refs), rng.choice([0, 0, 0, 1])) if a[0] == "ls-remote"
                  else proc(rng, "", rng.choice([0, 1])))
        case.fake("print_error", lambda rng, m: None)
        swept = r.choice([0.0, NOW - 5, NOW - 9999])
        items = [{"id": r.choice(ids), "state": r.choice(["landed", "queued", "dead"])} for _ in range(r.randint(0, 3))]
        st = Probe(case, "store", items=lambda rng, typ: raising(rng, 0.1, items))
        return ("/repo", "/d", st, {"land refs": swept}, r.choice([None, NOW]))

    def test_sweep(self):
        check(self, "sweep_land_refs", self.build, lambda A: A.sweep_land_refs, 4000)


# ------------------------------------------------------------------------------------------------ leases
class CmdLeaseTest(unittest.TestCase):
    def build(self, case):
        r = case.rng
        rows = [{"name": f"l{i}", "holder": r.choice([None, "h", ""]), "epoch": i, "expires_at": NOW + r.choice([-9999, -100, 50, 7000]),
                 "max_until": NOW + r.choice([-5, 100]), "meta": r.choice([None, "", "{}", '{"note": "doing"}', '{"note": ""}'])}
                for i in range(r.randint(0, 5))]
        case.fake("epoch_now", lambda rng: NOW)
        st = Probe(case, "st", list=lambda rng, prefix: rows)
        holder = {}
        return (holder, NS(prefix=r.choice([None, "", "block/"]), all=r.random() < 0.4), st)

    def test_cmd_lease(self):
        holder = {}

        def run(A):
            def go(h, a, st):
                return printing(holder, A.cmd_lease)(a, st)
            return go

        def build(case):
            holder["case"] = case
            return self.build(case)
        check(self, "cmd_lease", build, run, 2500)


# ------------------------------------------------------------------------------------------------ forge
class GhApiOnceTest(unittest.TestCase):
    def build(self, case):
        r = case.rng
        method = r.choice(["GET", "GET", "POST", "PATCH"])
        etag_rec = maybe(r, {"etag": r.choice(["W/1", None, ""]), "body": r.choice([{"cached": 1}, None, [1]])}, 0.5)
        case.fake("etag_path", lambda rng, url: "/etag/" + url[-6:])
        case.fake("read_record", lambda rng, p: etag_rec)
        case.fake("forge_env", lambda rng, o_r: {"E": "1"})
        case.fake("gh_tally_shape", lambda rng, path: path.split("/")[0])
        case.fake("gh_tally_as", lambda rng, label: Scope())
        case.fake("gh_tally", lambda rng, **k: None)
        case.fake("write_json", lambda rng, p, d: raising(rng, 0.2, None, OSError))
        body = r.choice(['{"a": 1}', "", "  ", "null", "[1, 2]", "not json"])

        def run(rng, m, path, argv, env, b):
            head = rng.choice([f"HTTP/2 {s}\r\netag: W/9\r\nx-a: b\r\n\r\n" for s in (200, 304, 404)] + [""]) if m == "GET" else ""
            return proc(rng, head + body, rng.choice([0, 0, 1]), rng.choice(["", "boom"]))
        case.fake("_gh_api_run", run)
        return ("o/r", method, r.choice(["pulls", "x/y"]), r.choice([None, {"a": "b"}]), r.choice([None, {"k": 1}]))

    def test_gh_api_once(self):
        def run(A):
            def go(*a):
                try:
                    return A._gh_api_once(*a)
                except A.ForgeHTTPError as e:
                    return "ForgeHTTPError " + str(e)
            return go
        check(self, "gh_api_once", self.build, run, 4000)


class ForgeRunListTest(unittest.TestCase):
    def build(self, case):
        r = case.rng
        fields = r.sample(["databaseId", "workflowName", "conclusion", "status", "headSha"] + ["bogus"] * (r.random() < 0.1), r.randint(1, 4))
        args = ["run", "list"]
        if r.random() < 0.97:
            args += ["--json", ",".join(fields)]
        args += ["--limit", r.choice(["5", "30", "100", "130", "250", "250", "7"] + ["0", "x"] * (r.random() < 0.1))]
        if r.random() < 0.4:
            args += ["--workflow", r.choice(["a.yml", "a.yml", "A name"])]
        if r.random() < 0.3:
            args += ["--branch", "main", "--status", r.choice(["completed", "completed", "weird"])]

        def api(rng, o_r, method, path, params=None):
            if rng.random() < 0.15:
                raise A.ForgeHTTPError(rng.choice(["HTTP 404 x", "Not Found", "HTTP 500"]))
            n = rng.choice([0, 3, params["per_page"], params["per_page"], params["per_page"] - 1])
            runs = [{"id": rng.randint(1, 9), "workflow_id": rng.choice([1, 2, None]), "path": rng.choice(["p", None]), "name": "rn",
                     "conclusion": rng.choice([None, "success"]), "status": "completed", "head_sha": "s"} for _ in range(n)]
            return rng.choice([{"workflow_runs": runs}, {"workflow_runs": runs}, None, {}])
        case.fake("api", api)
        case.fake("forge_workflow_names", lambda rng, o_r: {1: "One", "p": "Pee"})
        return ("o/r", args)

    def test_forge_run_list(self):
        def run(A):
            def go(*a):
                try:
                    got = A.forge_run_list(*a)
                except RuntimeError as e:
                    return "RuntimeError " + str(e)
                return None if got is None else (list(got), got.complete)
            return go
        check(self, "forge_run_list", self.build, run, 4000)


# ------------------------------------------------------------------------------------------------ redopen
class RedOfferTest(unittest.TestCase):
    def build(self, case):
        r = case.rng
        case.fake("red_tier", lambda rng, d, culprit, now: rng.choice(["author", "deep", "blame", "revert", "fixer", "plain"]))
        case.fake("decided_revert", lambda rng, d, now: rng.choice(["abc", None]))
        case.fake("red_offer_class", lambda rng, d: rng.choice(["fast", "slow"]))
        case.put("RED_OFFER", {"author": 600, "fast": 300, "slow": 1200})
        case.fake("red_decision", lambda rng, d: rng.choice([None, "decided"]))
        case.fake("red_what", lambda rng, d: "WHAT")
        case.fake("env_number", lambda rng, name, default: default)
        case.fake("red_move", lambda rng, store, it, state, text, **kw: dict(it, moved=1))
        case.fake("red_put_offer", lambda rng, store, it: None)
        d = {"key": "k", "workflow": "wf", "attempts": r.choice([0, 1, 2, None]), "deep_told": r.choice([None, 0, 1, 2]), "line": r.choice(["", "ln"]),
             "last_run": r.choice([None, {"number": 4}]), "culprit": r.choice([None, "c1"]),
             "author": r.choice([None, {"worktree": "/w", "session": "s", "shas": ["abcdefghijkl"]}, {}]),
             "blame": r.choice([None, {"since": NOW - 100}])}
        store = Probe(case, "store", clock=lambda rng: NOW)
        return (store, {"id": "i", "data": d}, "text", r.choice([None, {"x": 1}]), None, r.random() < 0.3, r.choice([None, "VERDICT"]),
                r.choice([None, ["t"]]), r.random() < 0.3)

    def test_red_offer(self):
        check(self, "red_offer", self.build, lambda A: A.red_offer, 5000)


class RedFailedStepsTest(unittest.TestCase):
    def build(self, case):
        r = case.rng
        jobs_json = [{"name": r.choice(["a", "b", "c"]), "conclusion": r.choice(["failure", "success", "cancelled", None]),
                      "steps": [{"name": r.choice(["s1", "s2", None, ""]), "conclusion": r.choice(["failure", "success", "timed_out"])} for _ in range(r.randint(0, 3))]}
                     for _ in range(r.randint(0, 3))]
        case.fake("reds_dir", lambda rng, *p: "/reds/" + "/".join(p))
        case.fake("read_record", lambda rng, p: maybe(rng, {"a": ["s1", "s2"], "c": ["x"], "b": None}, 0.4))
        case.fake("red_repo_of", lambda rng, d: "o/r")
        case.fake("gh_json", lambda rng, o_r, *a: raising(rng, 0.15, r.choice([{"jobs": jobs_json}, {}, None]), r.choice([RuntimeError, SystemExit])))
        case.fake("write_json", lambda rng, p, d: raising(rng, 0.2, None, OSError))
        case.patch(os, "makedirs", lambda rng, p, exist_ok=False: raising(rng, 0.2, None, OSError))
        d = {"steps": r.choice([None, None, None, [], ["z", "a", 3]]), "last_run": r.choice([None, {}, {"databaseId": 5}, {"databaseId": 5}, {"databaseId": 6}]),
             "jobs": r.choice([None, ["a"], ["a", "b"], []]), "job": r.choice([None, "c", "a"])}
        return (d,)

    def test_red_failed_steps(self):
        check(self, "red_failed_steps", self.build, lambda A: partial(A.red_failed_steps, ctx=case_context()), 4000)


class RedAttachObsTest(unittest.TestCase):
    def build(self, case):
        r = case.rng
        case.fake("classifier_version", lambda rng: "cv1")
        d = {"runs": r.choice([None, ["1"], ["1", "2"]]), "last_run": r.choice([None, {"databaseId": 5}]), "jobs": r.choice([None, ["a"], ["a", "b"]]),
             "jobs_seen": r.choice([None, ["z"]]), "classifier": r.choice(["cv1", "cv0", None])}
        ob = {"names": r.choice([[], ["1"], ["3"]]), "run": {"databaseId": r.choice([None, 4, 6])}, "jobs": r.choice([["a"], ["b", "a"], []])}
        it = {"id": "i", "version": 2, "data": d}

        def update(rng, typ, iid, version, upd):
            if rng.random() < 0.4:
                raise A.StaleVersion("stale")
        st = Probe(case, "st", update_item=update, item=lambda rng, typ, iid: {"id": iid, "version": rng.randint(1, 9), "data": d})
        return (st, it, ob)

    def test_red_attach_obs(self):
        def run(A):
            def go(st, it, ob):
                try:
                    return A.red_attach_obs(st, it, ob)
                except A.StaleVersion:
                    return "StaleVersion"
            return go
        check(self, "red_attach_obs", self.build, run, 4000)


class RedDiagnosisStepTest(unittest.TestCase):
    def build(self, case):
        r = case.rng
        names = ["1", "2", 3]
        case.fake("red_names", lambda rng, d: names)
        case.fake("red_what", lambda rng, d: "WHAT")
        case.fake("red_verify_dispatch", lambda rng, *a: rng.choice(["dispatched run", "not dispatchable"]))
        case.fake("red_keep_holder", lambda rng, row, text: None)
        changes = [{"id": f"c{i}", "entered_at": r.choice([NOW - 10, NOW - 99999, None]),
                    "data": r.choice([None, {"diagnoses": [r.choice([1, "2", 9])], "landed_sha": r.choice([None, "sha"]), "tip": "tip"}, {}])}
                   for i in range(r.randint(0, 3))]
        st = Probe(case, "st", items=lambda rng, typ, states: raising(rng, 0.1, changes), update_item=lambda rng, *a: None)
        done = {f"c{i}": {"at": r.choice([NOW - 5, NOW + 5]), "dispatch": r.choice(["dispatched x", "no"]), "told": r.choice([None, None, 1])}
                for i in range(r.randint(0, 3))}
        d = {"opened_at": NOW - 5000, "diagnosis_runs": r.choice([None, done]), "workflow": "wf", "last_run": r.choice([None, {"databaseId": 4}])}
        updated = r.choice(["2026-11-01T00:00:00Z", "2026-11-01T00:00:00Z", None])
        snap = r.choice([None, {"latest": {"wf": {"updatedAt": updated, "number": 7, "status": r.choice(["completed", "in_progress", None]), "conclusion": "failure"}}}])
        return (st, {"id": "i", "version": 1, "data": d}, {"holder": "h"}, snap, "/repo", "o/r", r.choice([NOW, 1.0]))

    def test_red_diagnosis_step(self):
        check(self, "red_diagnosis_step", self.build, lambda A: A.red_diagnosis_step, 4000)


class RedCallableEvidenceTest(unittest.TestCase):
    def build(self, case):
        r = case.rng
        case.fake("remote", lambda rng: "origin")
        case.fake("main_flow_meta", lambda rng, o_r: {"paths": r.choice([{"wf": "a/b.yml"}, {}]),
                                                      "callers": {"b.yml": r.sample(["c1", "c2", "c3", "c4", "c5"], r.randint(0, 5))}})
        case.fake("git", lambda rng, repo, *a, **k: proc(rng, "\n".join(f"{x}" * 10 for x in r.sample("abcd", r.randint(0, 4))), rng.choice([0, 0, 1])))
        case.fake("contains_local", lambda rng, repo, first, sha: rng.random() < 0.7)
        latest = {f"c{i}": {"number": i, "conclusion": r.choice(["success", "failure", None]), "headSha": "h"} for i in range(1, 6)}
        d = {"red_kind": r.choice(["outside"] * 6 + ["gate"]), "workflow": r.choice(["wf"] * 6 + ["other"]),
             "last_run": r.choice([None, {}, {"headSha": "h", "number": 2}, {"headSha": "h", "number": 2}, {"headSha": "h", "number": 2}])}
        snap = r.choice([None, {"callable_only": r.choice([["wf"]] * 5 + [[]]), "latest": r.choice([latest, latest, None])}] + [{"callable_only": ["wf"], "latest": latest}] * 4)
        return (d, snap, r.choice([None, "/repo", "/repo", "/repo"]), "o/r")

    def test_red_callable_evidence(self):
        check(self, "red_callable_evidence", self.build, lambda A: A.red_callable_evidence, 4000)


# ------------------------------------------------------------------------------------------------ snapshot
class WriteSnapshotTest(unittest.TestCase):
    def build(self, case):
        r = case.rng
        machines = {"change": 1, "batch": 1}
        for k in ("red", "condition", "revert"):
            if r.random() < 0.7:
                machines[k] = 1
        case.put("MACHINES", machines)
        case.fake("machine_state", lambda rng, typ, s: NS(terminal=s in ("done", "dead")))
        case.fake("epoch_now", lambda rng: NOW)
        case.fake("code_version", lambda rng: "v1")
        case.fake("start_rev", lambda rng: "rev")
        case.fake("revert_brief", lambda rng, i: {"rb": i["id"]})
        case.put("HOLD_SHOWN", ("hold", "waiting"))
        case.fake("hold_row", lambda rng, h: {"h": h})
        case.fake("land_hold", lambda rng, st: "LH")
        case.fake("owner_asks", lambda rng, st: ["ask"])
        rows = [{"worktree": f"/w{i}", "state": r.choice(["waiting", "queued"])} for i in range(r.randint(0, 3))]
        case.fake("change_rows", lambda rng, st: rows)
        case.fake("state_update", lambda rng, **kw: None)

        def item(i):
            data = r.choice([None, {}, {"worktree": "/w"}, {"worktree": "/w", "hold": {"why": "because"}}, {"hold": {}}])
            return {"id": f"i{i}", "state": r.choice(["open", "done", "hold", "waiting", "dead"]), "deadline": r.choice([None, 5]), "level": r.choice([None, 1]), "data": data}
        st = Probe(case, "st", items=lambda rng, typ: [item(i) for i in range(rng.randint(0, 3))])
        return ("/d", st)

    def test_write_snapshot(self):
        check(self, "write_snapshot", self.build, lambda A: A.write_snapshot, 4000)


# ------------------------------------------------------------------------------------------------ workflow job scanners
LINES = ["jobs:", "jobs:  ", "jobs: # c", "jobs:\t", "jobs: x", "  build:", "  test: # c", "  lint:  ", "    timeout-minutes: 10", "    timeout-minutes: 5  ",
         "    timeout-minutes: 7 # why", "    timeout-minutes: x", "\ttimeout-minutes: 4", "    uses: ./a.yml", "    uses: org/r/.github/w.yml@v1", "    runs-on: x",
         "    name: Build ${{ m }}", "    name: 'Quoted n' # c", '    name: "Dq"', "    name: plain # c", "    name:", "    steps:", "      timeout-minutes: 3",
         "        - run: y", "      - name: s", "   timeout-minutes: 2", "    needs: a", "  deploy:", "    timeout-minutes: 90", "  ci-job-2:", "  _x:", "  1bad:",
         "  k: v", "\tbad", "\t  tabbed:", "  # c", "# top", "", "   ", "name: W", "on: push", "jobsx:", "    timeout-minutes: 0", "      sub:", "    - dash:",
         " build:", "    build:", "name: 'X'", "  name: topname"]


def a_workflow_text(r):
    lines = pick(r, ["name: W", "on: push", "# c", ""], 0, 2)
    if r.random() < 0.93:
        lines.append(r.choice(["jobs:", "jobs:", "jobs:", "jobs: # c", "jobs:  "]))
    lines += pick(r, LINES, 0, 16)
    return "\n".join(lines) + r.choice(["", "\n"])


class WorkflowJobsTest(unittest.TestCase):
    def test_workflow_jobs(self):
        check(self, "workflow_jobs", lambda case: (a_workflow_text(case.rng),), lambda A: A.workflow_jobs, 12000)

    def test_workflow_timeout_bound_with_comments_and_tabs(self):
        check(self, "workflow_timeout_bound_edges", lambda case: (a_workflow_text(case.rng),), lambda A: A.workflow_timeout_bound, 12000)


if __name__ == "__main__":
    unittest.main()
