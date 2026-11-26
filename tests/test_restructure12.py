"""RESTRUCTURE ROUND 12: each function split into named stages keeps its behaviour. The digests (fixtures/
restructure12-digests.json, recorded from the code BEFORE the split with `R4_RECORD=1`) cover thousands of seeded cases
each, collaborators faked (golden_cases.py): every call, its order and the result. Every case is bounded."""
import contextlib, io, os, sys, types, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402
from golden_cases import Probe, check as _check  # noqa: E402

DIGESTS = os.path.join(HERE, "fixtures", "restructure12-digests.json")
NOW = 1_795_000_000.0


def check(test, name, build, fn_of, n=3000):
    _check(test, A, name, build, fn_of, n, DIGESTS)


def maybe(rng, value, p=0.5):
    return value if rng.random() < p else None


def raising(rng, p, value, exc=RuntimeError):
    if rng.random() < p:
        raise exc("down")
    return value


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


class Quiet(contextlib.nullcontext):
    """A context manager with a stable repr (an object's repr holds its address): every call is the case's log."""

    def __repr__(self):
        return "<quiet>"


def captured(holder, fn):
    def run(*args):
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
                return fn(*args)
        finally:
            holder["case"].log.append(["output", out.getvalue()])
    return run


# ------------------------------------------------------------------------------------------------ workflows
def a_pick(r, parts, lo=0, hi=6):
    return [r.choice(parts) for _ in range(r.randint(lo, hi))]


ON_LINES = ["on:", "on: push", "on: [push, pull_request]", "\"on\":", "'on': {push: {branches: [a]}, schedule: x}", "on: { push: {a: 1}, pull_request: {} }",
            "on: pull_request  # c", "on:  # why", "on: {workflow_dispatch: {}}", "on: [a, {b: c}]", "on: {push: {branches: [main]}}", "ON:", "name: x", "on: {a: {b: {c: 1}}, d: 2}"]
SUB_LINES = ["  push:", "  pull_request:", "    branches: [main]", "  - push", "  - workflow_dispatch", "", "  # note", "  schedule:", "    - cron: '0 1 * * *'",
             "  'workflow_call':", "\twrong:", "      deeper:", "jobs:", "name: z", "  workflow_run:", "  - \"release\"", "   odd:", "  push", "  - "]


def a_workflow(r):
    lines = a_pick(r, ["name: W", "# c", "env:", "  A: 1", "", "permissions: read-all"], 0, 2)
    if r.random() < 0.85:
        lines.append(r.choice(ON_LINES))
        lines += a_pick(r, SUB_LINES, 0, 7)
    lines += a_pick(r, ["name: W", "jobs:", "  build:", "    name: Build", "    runs-on: x"], 0, 3)
    return "\n".join(lines) + r.choice(["", "\n"])


class WorkflowTriggersTest(unittest.TestCase):
    def test_workflow_triggers(self):
        check(self, "workflow_triggers", lambda case: (a_workflow(case.rng),), lambda A: A.workflow_triggers, 6000)


JOB_STARTS = ["jobs:", "jobs:  # c", "jobs: x", " jobs:", "jobs:\t"]
JOB_LINES = ["  build:", "  test-1:", "  lint:  # why", "  9bad:", "    name: Build (${{ matrix.x }})", "    name: 'Quoted name' # c", "    name: \"Dq\"",
             "    name: plain # c", "    name:", "    timeout-minutes: 30", "    timeout-minutes: 5 # c", "    timeout-minutes: x", "    runs-on: ubuntu",
             "    steps:", "      - name: step", "        timeout-minutes: 9", "      name: deeper", "", "  # note", "    # c", "      timeout-minutes: 4",
             "   name: shallow", "\tname: tab", "    needs: [a]", "name: top", "on: push", "    name: Build & Test", "  deploy:", "    uses: ./x.yml", "    name: ${{ inputs.n }}", "    name: dbl  # c", "    name: 'q' 'r'", "    name:   spaced   "]


def a_jobs_workflow(r):
    lines = a_pick(r, ["name: W", "on: push", "# c"], 0, 2)
    if r.random() < 0.92:
        lines.append(r.choice(JOB_STARTS))
        lines += a_pick(r, JOB_LINES, 0, 12)
    lines += a_pick(r, ["", "name: after", "  x: 1"], 0, 2)
    return "\n".join(lines) + r.choice(["", "\n"])


class WorkflowJobsTest(unittest.TestCase):
    def test_workflow_jobs(self):
        check(self, "workflow_jobs", lambda case: (a_jobs_workflow(case.rng),), lambda A: A.workflow_jobs, 8000)


# ------------------------------------------------------------------------------------------------ wait
def a_job(r, i):
    j = {}
    for key, gen in (("name", lambda: r.choice([f"job{i}", f"job{i}", "x / takes (pl)", None, "dup"])),
                     ("conclusion", lambda: r.choice(["failure", "success", "cancelled", "timed_out", "skipped", None])),
                     ("status", lambda: r.choice(["completed", "in_progress", "queued"])),
                     ("databaseId", lambda: r.choice([None, 0, 100 + i])),
                     ("steps", lambda: [{"conclusion": r.choice(["failure", "success", "timed_out"])} for _ in range(r.randint(0, 2))])):
        if r.random() < 0.85:
            j[key] = gen()
    return j


class FailedJobsLogTest(unittest.TestCase):
    def build(self, case):
        r = case.rng
        jobs = [a_job(r, i) for i in range(r.randint(0, 8))]
        names = [j.get("name") for j in jobs] + ["other", "dup"]

        def fetch(rng, o_r, run_id, attempt):
            if rng.random() < 0.15:
                raise RuntimeError("gh down")
            lines = []
            for _ in range(rng.randint(0, 6)):
                shape = rng.random()
                nm = rng.choice(names)
                if shape < 0.1:
                    lines.append("untabbed line")
                elif shape < 0.15:
                    lines.append(f"{nm}\tonly")
                else:
                    lines.append(f"{nm}\t{rng.choice(['UNKNOWN STEP', 'Build', 'Test'])}\tline {rng.randint(0, 9)}")
            return "\n".join(lines)

        def slice_of(rng, o_r, j):
            if rng.random() < 0.25:
                raise RuntimeError("rest down")
            return [f"{j.get('name')}\tS\tl{k}" for k in range(rng.randint(0, 2))], maybe(rng, "a note", 0.4)
        case.fake("fetch_failed_log", fetch)
        case.fake("job_failed_slice", slice_of)
        case.put("MAX_JOB_LOG_FETCHES", r.choice([0, 1, 2, 5]))
        notes = [] if r.random() < 0.7 else None
        return ("o/r", 7, jobs, r.choice([None, 2]), notes)

    def test_failed_jobs_log(self):
        def run(A):
            def go(*args):
                try:
                    return A.failed_jobs_log(*args), args[4]
                except Exception as e:
                    return "raised", str(e), args[4]
            return go
        check(self, "failed_jobs_log", self.build, run, 6000)


class RunVerdictTest(unittest.TestCase):
    def build(self, case):
        r = case.rng
        n = r.randint(0, 5)
        jobs = [{"name": r.choice([f"leg / takes (p{i})", f"takes {i}", f"j{i}", f"leg / build {i}"]),
                 "status": r.choice(["completed", "completed", "in_progress"]),
                 "conclusion": r.choice(["success", "failure", "skipped", "cancelled", "timed_out", None])} for i in range(n)]
        d = {"status": r.choice(["completed", "queued", "in_progress"]), "conclusion": r.choice(["success", "failure", "neutral", "skipped", None, "cancelled"]),
             "workflowName": r.choice(["Identity takes", "Identity model convergence", "Main", None]),
             "headBranch": r.choice(["measure/x/y", "main", None, "measure/"])}
        if r.random() < 0.9:
            d["jobs"] = jobs
        elif r.random() < 0.5:
            d["jobs"] = None
        case.fake("gh_json", lambda rng, o_r, *a: d)

        def verdicts(rng, o_r, rid):
            raising(rng, 0.2, None)
            return {f"identity-takes-{c}": rng.choice(["ok\nline2", "", "bad"]) for c in rng.sample(["pl", "uk", "de"], rng.randint(0, 3))}
        case.fake("fetch_takes_verdicts", verdicts)
        case.fake("failure_detail", lambda rng, o_r, rid, js, tail: [f"detail {len(js)} {tail}"] * rng.randint(0, 2))
        return ("o/r", 9, r.choice(["done", "landable", "landable"]), r.choice([0, 3]))

    def test_run_verdict(self):
        check(self, "run_verdict", self.build, lambda A: A.run_verdict, 8000)


# ------------------------------------------------------------------------------------------------ landqueue
class LandStatusEntryTest(unittest.TestCase):
    def build(self, case):
        r = case.rng
        items = []
        for i in range(r.choice([0, 1, 2, 3, 4, 4, 10, 11])):
            d = {}
            for key, gen in (("worktree", lambda: r.choice(["/w/a", None])), ("head", lambda: r.choice(["abcdef0123456789", None, ""])),
                             ("landed_sha", lambda: r.choice([None, "ff00ff00ff00"])),
                             ("why", lambda: r.choice(["NOT LANDED: x", "SUPERSEDED by y", "plain", "", None])),
                             ("hold", lambda: r.choice([None, {}, {"why": "waiting"}])),
                             ("rechecks", lambda: [{"at": r.choice([0, 5, 100]), "kind": r.choice(["rebase", None]), "main": r.choice(["m1", None]),
                                                    "why": "w"} for _ in range(r.randint(0, 2))]),
                             ("check", lambda: {"x": 1})):
                if r.random() < 0.7:
                    d[key] = gen()
            it = {"id": r.choice([f"c-{i}", f"c-{i}-b", "c-1"]), "state": r.choice(["queued", "checking", "landed", "rejected", "held", "waiting"]),
                  "entered_at": r.choice([NOW - 100, None, 0]), "deadline": NOW + r.choice([-500, 600, 3000])}
            if r.random() < 0.9:
                it["data"] = d
            items.append(it)
        case.fake("machine_state", lambda rng, typ, state: types.SimpleNamespace(terminal=state in ("landed", "rejected")))
        case.fake("landed_sha_of", lambda rng, eid, data: "sha-of")
        case.fake("check_lines", lambda rng, st, it, t: ["check line a", "check line b"][:rng.randint(1, 2)])
        holder = {"case": case}
        return (holder, Probe(case, "st", items=lambda rng, typ: items), r.choice(["c-1", "c-0", "c-", "zz", "c-2-b", "c-3"]), NOW)

    def test_land_status_entry(self):
        def run(A):
            def go(holder, st, entry, t):
                return printing(holder, A.land_status_entry)(st, entry, t)
            return go
        check(self, "land_status_entry", self.build, run, 8000)


# ------------------------------------------------------------------------------------------------ culpritfix
class LanderReviveTest(unittest.TestCase):
    def build(self, case):
        r = case.rng
        lander = {}
        for key, gen in (("session", lambda: r.choice(["s1234567890", None, ""])), ("transcript", lambda: r.choice(["/t/a.jsonl", None, ""])),
                         ("agent", lambda: r.choice([None, "sub", ""])), ("worktree", lambda: r.choice(["/w/l", None, "/w/gone"])),
                         ("session_cwd", lambda: r.choice(["/w/s", None, "/w/nodir"]))):
            if r.random() < 0.8:
                lander[key] = gen()
        g = {"commits": ["c" * 12, "d" * 12], "change": "chg", "subject": "subj"}
        if r.random() < 0.9:
            g["lander"] = lander
        b = {"culprit": 0, "groups": [g], "evidence": r.choice([None, "ev"])}
        if r.random() < 0.7:
            b["revert"] = {"ok": r.random() < 0.6, "msg": "rm"}
        u = {"run": {"number": r.choice([5, None]), "databaseId": r.choice([77, None, 0])}}
        off = {"lease": "L", "item": r.choice(["red:abc", "red:def"])}
        case.patch(os.path, "exists", lambda rng, p: rng.random() < 0.6)
        case.patch(os.path, "isdir", lambda rng, p: rng.random() < 0.6)
        case.fake("bg_transcript", lambda rng, sid: maybe(rng, "/t/bg.jsonl", 0.5))
        case.fake("allowed_path", lambda rng, p: rng.random() < 0.7)
        case.fake("fresh_worktree", lambda rng, repo, kind, num: ("/w/fresh", None) if rng.random() < 0.6 else (None, "no space"))
        case.fake("blame_change_key", lambda rng, g_: "chg-key")
        case.fake("lander_name", lambda rng, l: "the lander")
        case.fake("lander_ci_why", lambda rng, rid: "ci why text")
        case.fake("lander_fallback", lambda rng, store, o_r, repo, off_, took, u_, b_, why: (False, f"fb: {why} {sorted(b_)}"))
        case.fake("red_dispatch", lambda rng, o_r, repo, u_, **k: (rng.random() < 0.6, "dispatch msg " + "x" * rng.choice([0, 150])))
        case.fake("red_what", lambda rng, u_: "RED w")
        case.fake("tilde", lambda rng, p: "~" + str(p))
        case.fake("ts", lambda rng: "T")
        case.fake("inbox_write", lambda rng, *a, **k: None)
        store = Probe(case, "store", item=lambda rng, typ, eid: maybe(rng, {"id": eid, "version": 4}, 0.7))
        return (store, "o/r", "/repo", off, 3, u, b)

    def test_lander_revive(self):
        check(self, "lander_revive", self.build, lambda A: A.lander_revive, 8000)


# ------------------------------------------------------------------------------------------------ redreplay
COMMIT_MSGS = ["subject\n\nFixes-Red: 12\nFix-Replay: sbt t base=FAIL head=PASS\n", "s\n\nbody\n\nFix-Replay: sbt t\n\nCo-Authored-By: x\n",
               "s\n\nFix-Replay: none — a long enough reason that explains\nCo-Authored-By: x", "s\n\nFix-Replay: none — a reason\n\nCo-Authored-By: x",
               "s\n\nFix-Replay: none: x\n\nFixes-Red: 3\nFix-Replay: none\n", "s\nFixes-Red: 7, #8\n", "no trailers at all", "",
               "s\n\nFix-Replay: sbt t\nFix-Replay: sbt t\nFix-Replay: other\n", "s\n\nFix-Replay:\nFixes-Red:\n",
               "s\n\nfixes-red: 5\nfix-replay: cmd BASE=fail HEAD=pass\n", "s\n\nFix-Replay: none -- two dashes here are a reason\n\nFixes-Red: 9\n",
               "s\n\nFix-Replay: none - hyphen\nFixes-Red: 1\n\nCo-Authored-By: x\n", "s\n\nFix-Replay: nonexistent thing\n", "s\n\nFix-Replay: \nFixes-Red: 2\n", "s\n\nFix-Replay: none \nFixes-Red: 2\n"]


class FixReplayTrailersTest(unittest.TestCase):
    def build(self, case):
        r = case.rng
        msgs = {}
        commits = [f"c{i}" for i in range(r.randint(0, 4))]
        for c in commits:
            msgs[c] = r.choice(COMMIT_MSGS) if r.random() < 0.8 else r.choice(COMMIT_MSGS) + r.choice(COMMIT_MSGS)
        case.fake("git", lambda rng, repo, *a, **k: types.SimpleNamespace(stdout=msgs[a[3]]))
        return ("/repo", r.choice([commits, commits, None]))

    def test_fix_replay_trailers(self):
        check(self, "fix_replay_trailers", self.build, lambda A: A.fix_replay_trailers, 8000)


class FixReplayRefusalTest(unittest.TestCase):
    def build(self, case):
        r = case.rng
        named = set(r.sample(["1", "2", "3", "None"], r.randint(0, 3)))
        cmds = ["sbt t", "bt run", "other cmd"]
        claimed = [(r.choice(cmds), r.choice(["FAIL", "PASS"]), r.choice(["FAIL", "PASS"])) for _ in range(r.randint(0, 2))]
        bare = r.sample(cmds, r.randint(0, 2))
        nones = r.choice([[], [], [""], ["short"], ["a long reason that is specific enough to pass the lint"], ["it is flaky and long enough for the lint ok"],
                          ["short", "n/a"]])
        case.fake("fix_replay_trailers", lambda rng, repo, commits: (named, claimed, bare, nones))
        items = []
        for i in range(r.randint(0, 4)):
            d = {"key": r.choice(["k", None, "k2"]), "gate": r.choice([True, False, None]), "workflow": r.choice(["W", "Alert backtest", None]),
                 "ids": r.sample(["1", "2", "3", "4"], r.randint(0, 2))}
            items.append({"id": f"i{i}", "state": r.choice(["open", "verified", "fixing", "cleared"]), "data": d})
        case.fake("red_names", lambda rng, d: d["ids"] + [None])
        case.fake("red_what", lambda rng, d: f"RED {d['key']}")
        case.fake("red_workflows_on_main", lambda rng, repo: rng.random() < 0.7)
        case.fake("red_repo_conf", lambda rng, rep: rng.choice([None, {}, {"red_workflows": True}]))
        case.fake("red_repo_of", lambda rng, d: "o/r")
        case.fake("remote", lambda rng: "origin")
        case.fake("red_workflow_policy", lambda rng, repo, wf: {"replay": rng.choice(["", None, "bt run {run}", "sbt t", "x {run} y"])})
        case.fake("no_vault", lambda rng: Quiet())
        down = r.random() < 0.1
        state = types.SimpleNamespace(items=lambda typ: raising(case.rng, 1.0 if down else 0, items, r.choice([RuntimeError, SystemExit])))
        proofs = r.choice([None, [], [{"cmd": c, "base": r.choice(["FAIL", "PASS", "TIMEOUT"]), "head": r.choice(["FAIL", "PASS"])} for c in r.sample(cmds, r.randint(1, 3))]])
        return ("/repo", ["c1"], state, proofs)

    def test_fix_replay_refusal(self):
        check(self, "fix_replay_refusal", self.build, lambda A: A.fix_replay_refusal, 8000)


# ------------------------------------------------------------------------------------------------ alerts
class ReconcileAlertsTest(unittest.TestCase):
    def build(self, case):
        r = case.rng
        n = r.randint(0, 5)
        idx = {"items": {f"it{i}": f"e{i}" for i in range(n)}, "probed": {f"it{i}": 1 for i in range(r.randint(0, 7))}}
        if r.random() < 0.1:
            idx.pop("items")
        if r.random() < 0.1:
            idx.pop("probed")
        t = NOW
        case.fake("epoch_now", lambda rng: t)
        case.fake("inbox_dir", lambda rng: "/inbox")
        case.fake("alert_index_path", lambda rng, d: "/inbox/idx.json")
        case.fake("alert_lock", lambda rng, d: Quiet())
        case.fake("alert_index", lambda rng, d: idx)
        case.fake("env_num", lambda rng, name, default: rng.choice([default, 0, 10 ** 9]))

        def read(rng, p):
            if p.endswith("idx.json"):
                return rng.choice([None, {}, {"reconciled_at": t - 5}, {"reconciled_at": "bad"}, {"reconciled_at": t - 10 ** 6}])
            return maybe(rng, {"at": t - rng.choice([0, 100, 10 ** 6]), "audience": rng.choice(["owner", "fixer", None]),
                               "surfaced": rng.choice([None, 0, 2])}, 0.8)
        case.fake("read_record", read)
        case.fake("alert_acked", lambda rng, d, eid: rng.random() < 0.4)

        def probe(rng, st, item, e, now, ctx):
            ctx["probed"][item] = rng.randint(0, 3)
            x = rng.random()
            if x < 0.15:
                raise RuntimeError("cannot tell " * 10)
            return (rng.choice([None, "cleared"]), rng.choice([None, "owner", "fixer", "author"]))
        case.fake("alert_probe", probe)
        case.fake("alert_close", lambda rng, d, ix, item, why: ix["items"].pop(item, None))
        case.fake("write_json", lambda rng, p, e: None)
        case.fake("alert_unack", lambda rng, d, eid: None)
        case.fake("alert_index_save", lambda rng, d, ix: None)
        case.fake("err", lambda rng, s: None)
        holder = {"case": case, "idx": idx}
        return (holder, Probe(case, "store"), r.choice([False, True]))

    def test_reconcile_alerts(self):
        def run(A):
            def go(holder, store, force):
                result = A.reconcile_alerts(store, force)
                return result, holder["idx"]
            return go
        check(self, "reconcile_alerts", self.build, run, 8000)


if __name__ == "__main__":
    unittest.main()
