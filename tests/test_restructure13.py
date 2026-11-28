"""RESTRUCTURE ROUND 13: each function split into named stages keeps its behaviour. The digests (fixtures/
restructure13-digests.json, recorded from the code BEFORE the split with `R4_RECORD=1`) cover thousands of seeded cases
each, collaborators faked (golden_cases.py): every call, its order and the result. Every case is bounded."""
import contextlib, dataclasses, io, json, os, shutil, sys, tempfile, unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402
from harness import case_context, context_with  # noqa: E402
from golden_cases import Probe, check as _check  # noqa: E402
from test_restructure12 import NOW, maybe, printing  # noqa: E402

DIGESTS = os.path.join(HERE, "fixtures", "restructure13-digests.json")


def check(test, name, build, fn_of, n=3000):
    _check(test, A, name, build, fn_of, n, DIGESTS)


def pick(r, parts, lo=0, hi=6):
    return [r.choice(parts) for _ in range(r.randint(lo, hi))]


# ------------------------------------------------------------------------------------------------ sessionpaths
TOOL_LINES = [
    {"type": "assistant", "message": {"content": [{"type": "tool_use", "input": {"command": "cd /w/a && ls ~/x/y"}}]}},
    {"type": "assistant", "message": {"content": [{"type": "tool_use", "input": {"p": ["/w/b/c", {"q": "$HOME/z"}]}}]}},
    {"type": "assistant", "message": {"content": [{"type": "text", "text": "/w/no"}, {"type": "tool_use", "input": "/w/d"}]}},
    {"type": "user", "message": {"content": [{"type": "tool_use", "input": {"p": "/w/user"}}]}},
    {"type": "assistant", "message": {"content": "tool_use /w/str"}},
    {"type": "assistant", "message": None},
    {"type": "assistant", "message": {"content": [{"type": "tool_use", "input": {"n": 5}}, "tool_use /w/x", {"type": "tool_use", "input": "no slash"}]}},
]


def a_transcript_line(r):
    shape = r.random()
    if shape < 0.55:
        return json.dumps(r.choice(TOOL_LINES))
    if shape < 0.7:
        return '{"tool_use" /w/broken'
    if shape < 0.8:
        return "plain /w/text line"
    return json.dumps({"type": "assistant", "message": {"content": [{"type": "tool_use", "input": {"p": f"/w/p{r.randint(0, 5)}/f"}}]}})


class TranscriptPathsTest(unittest.TestCase):
    def build(self, case):
        r = case.rng
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        path = os.path.join(d, "t.jsonl")
        case.put("HOME", "/home/u")
        text = "\n".join(a_transcript_line(r) for _ in range(r.randint(0, 8)))
        text += r.choice(["", "\n", "\n" + a_transcript_line(r)])
        if r.random() < 0.9:
            with open(path, "w") as f:
                f.write(text)
        cache = {}
        if r.random() < 0.6:
            head = A.hashlib_sha1(text.encode()[:512].decode("utf-8", "replace"))
            cache[path] = {"head": r.choice([head, head, "other"]), "offset": r.choice([0, 0, len(text.encode()) // 2, len(text.encode()) + 5]),
                           "paths": r.choice([[], ["/w/old"], ["/w/b/c"]])}
        return (path, cache)

    def test_transcript_paths(self):
        def run(A):
            def go(path, cache):
                got = A.transcript_paths(path, cache)
                return sorted(got), {k: v for k, v in cache.items()}
            return lambda path, cache: self.normal(go(path, cache), path)
        check(self, "transcript_paths", self.build, run, 2500)

    @staticmethod
    def normal(result, path):
        paths, cache = result
        return paths, {("t" if k == path else k): v for k, v in cache.items()}


class SessionWorktreesTest(unittest.TestCase):
    def build(self, case):
        r = case.rng
        names = ["/w/a", "/w/a/", "/w/b", "/w/c", "/w/s1", "/w/"]
        wts = [r.choice(names + [None, "", 5]) for _ in range(r.randint(0, 4))]
        files = {}
        for k in range(r.randint(0, 3)):
            files[f"key{k}"] = [(r.choice([f"/p/-w-a/sid{j}abcdefghijklmnop.jsonl", f"/p/proj/sess{j}/subagents/sub{j}0123456789abcdef.jsonl",
                                           "/p/-w-b/zz0123456789abcdef.jsonl"]), 1.0) for j in range(r.randint(0, 3))]
        case.fake("recovery_dir", lambda rng, name: "/c/recovery/" + name)
        case.fake("read_record", lambda rng, p: maybe(rng, {"/p/-w-a/sid0abcdefghijklmnop.jsonl": {"offset": 1}, "/p/gone.jsonl": {"offset": 2}}, 0.5))
        case.fake("session_transcripts", lambda rng, t, active: files)
        case.fake("live_slug", lambda rng, rp: rng.choice(["-w-a", "-w-b", "proj", "nomatch"]))
        paths_of = [{"/w/a/f", "/w/b", "/w/c/x/y", "/other"}, set(), {"/w/s1"}, {"/w/ab"}]
        case.fake("transcript_paths", lambda rng, path, cache: rng.choice(paths_of))
        case.fake("write_json", lambda rng, p, data: raising_os(rng))
        case.patch(os, "makedirs", lambda rng, p, exist_ok=False: raising_os(rng))
        case.patch(os.path, "realpath", lambda rng, p: p.rstrip("/") if len(p) > 1 else p)
        return (wts, r.choice([None, NOW]), r.choice([None, 600]))

    def test_session_worktrees(self):
        check(self, "session_worktrees", self.build, lambda A: A.session_worktrees, 4000)


def raising_os(rng):
    if rng.random() < 0.2:
        raise OSError("disk")


# ------------------------------------------------------------------------------------------------ outsideflows
JOB_TEXT_LINES = ["jobs:", "jobs:  ", "  build:", "  test:", "    timeout-minutes: 10", "    timeout-minutes: 5  ", "    timeout-minutes: x",
                  "    uses: ./a.yml", "    runs-on: x", "    steps:", "      timeout-minutes: 3", "        - run: y", "name: W", "on: push", "# c",
                  "  # c", "", "      - name: s", "   timeout-minutes: 2", "    needs: a", "  lint:", "\tbad", "    uses: org/r/.github/w.yml@v1",
                  "  deploy:", "    timeout-minutes: 90", "jobsx:", "    timeout-minutes: 0"]


class WorkflowTimeoutBoundTest(unittest.TestCase):
    def test_workflow_timeout_bound(self):
        def build(case):
            r = case.rng
            lines = pick(r, ["name: W", "on: push", "# c", ""], 0, 2)
            if r.random() < 0.93:
                lines.append("jobs:")
            lines += pick(r, JOB_TEXT_LINES, 0, 14)
            return ("\n".join(lines) + r.choice(["", "\n"]),)
        check(self, "workflow_timeout_bound", build, lambda A: A.workflow_timeout_bound, 12000)


class OutsideGateTest(unittest.TestCase):
    def build(self, case):
        r = case.rng
        flows = ["Main", "Alpha", "Beta", "Gamma", "Delta", "Eps"]
        state = [NOW]
        case.fake("epoch_now", lambda rng: state[0])
        case.fake("red_gate_flows", lambda rng, o_r: [(n, "f.yml") for n in r.sample(flows[:2], r.randint(0, 2))])

        def run(name):
            return {"workflowName": name, "status": r.choice(["completed", "completed", "in_progress"]),
                    "conclusion": r.choice(["success", "failure", "cancelled", None, "timed_out"]), "updatedAt": f"2026-11-{r.randint(10, 25)}"}
        top = [run(r.choice(flows + [None])) for _ in range(r.randint(0, 8))]

        def gh(rng, o_r, *a):
            if "--workflow" in a:
                if rng.random() < 0.25:
                    raise RuntimeError("gh down")
                return [run(a[a.index("--workflow") + 1]) for _ in range(rng.randint(0, 3))]
            return top
        case.fake("gh_json", gh)
        case.fake("main_workflow_files", lambda rng, o_r: {n: n.lower() + ".yml" for n in r.sample(flows, r.randint(0, 6))})
        case.fake("err", lambda rng, m: None)
        mem = {}
        for n in r.sample(flows, r.randint(0, 3)):
            mem[n.lower() + ".yml"] = (NOW - r.choice([10, 5000]), r.choice([None, [run(n)], []]))
        # the workflow files' own listings, kept by the reader (CiRuns, review 2026-11-26 B F2: this was outsideflows'
        # own _flow_runs, keyed by file alone); a None was "never listed"
        runs = A.CiRuns()
        runs._listed.update({("o/r", f, 30, None): v for f, v in mem.items() if v[1] is not None})
        self.ctx = context_with(runs)

        def kept():
            listed = {k[1]: v for k, v in runs._listed.items() if k[0] == "o/r" and k[2:] == (30, None)}
            return {**{f: listed.get(f, v) for f, v in mem.items()}, **listed}  # the old memo's order: seeded first
        return (lambda: (kept(), {n: rs for n, rs in runs._main.get("o/r", A.MainRuns()).outside.items()}),)

    def test_outside_gate(self):
        def run(A):
            def go(snap):
                got = A.outside_gate("o/r", ctx=self.ctx)
                mem, lists = snap()
                return got, mem, lists
            return go
        check(self, "outside_gate", self.build, run, 6000)


# ------------------------------------------------------------------------------------------------ reduntracked
class JobEverGreenTest(unittest.TestCase):
    def build(self, case):
        r = case.rng
        names = ["build", "test", "lint", "x / build", "Build"]

        def run(i):
            return {"databaseId": r.choice([None, 100 + i]), "status": "completed", "conclusion": r.choice(["success", "failure", "cancelled"])}
        runs = tuple(run(i) for i in range(r.randint(0, 6))) if r.random() < 0.85 else ()
        known = {"jobs": {"wf": {k: 1 for k in r.sample(names, r.randint(0, 2))}}} if r.random() < 0.6 else {}
        case.fake("epoch_now", lambda rng: NOW)
        case.fake("ever_green_record", lambda rng, store: {"o/r": known} if known else {})
        case.fake("job_matches", lambda rng, a, b: a == b)
        case.fake("newest_green_main", lambda rng, o_r, wf: r.choice([None, {"databaseId": 7, "conclusion": "success"}, RuntimeError("x")]) if False else
                  raise_or(rng, {"databaseId": 7, "conclusion": "success"}))

        def run_jobs(rng, o_r, rid):
            if rng.random() < 0.2:
                raise rng.choice([RuntimeError("down"), SystemExit(2)])
            return [{"name": rng.choice(names), "conclusion": rng.choice(["success", "failure"])} for _ in range(rng.randint(0, 3))]
        case.fake("decided_run_jobs", run_jobs)
        case.fake("ever_green_note", lambda rng, *a: None)
        mem = {}
        if r.random() < 0.4:
            mem[("o/r", "wf", "build")] = r.choice([NOW - 10, NOW - 99999])
        return (Probe(case, "st"), "o/r", "wf", r.choice(names), runs, r.choice([None, NOW]), mem)

    def test_job_ever_green(self):
        def run(A):
            def go(st, o_r, wf, job, runs, now_, mem):
                try:
                    got = A.job_ever_green(st, o_r, wf, job, runs, now_, ctx=dataclasses.replace(case_context(), caches=A.Caches(never_green_at=mem)))
                except BaseException as e:
                    got = ("raised", type(e).__name__)
                return got, dict(mem)
            return go
        check(self, "job_ever_green", self.build, run, 6000)


def raise_or(rng, value):
    x = rng.random()
    if x < 0.15:
        raise RuntimeError("no green")
    if x < 0.2:
        raise SystemExit(3)
    return value if x < 0.7 else None


# ------------------------------------------------------------------------------------------------ pressure
class PressureReadingTest(unittest.TestCase):
    """Re-recorded 2026-11-28 (R5, the machine probe): the reading's seams (sysctl text, meminfo, a passed load and cores)
    and its ambient reads (machine_cores, run_queue(s), machine_fixed, read_meminfo, pressure_sysctl) are ONE machine
    probe now; the case draws that machine's raw reads (its load averages, cores and kernel memory reading) as the old
    one drew the readers, and the busy cores and swap growth as before."""

    def build(self, case):
        r = case.rng
        case.fake("epoch_now", lambda rng: NOW)
        case.fake("gov_busy_cores", lambda rng: rng.choice([0.5, 3, 7.25]))
        case.fake("swap_growth", lambda rng, mb, now_: (None if mb is None else mb - 10, 120))
        load = r.choice([None, (2, 3), (9, 40)])
        fields = {"cores": r.choice([8, 18]), "free_pct": r.choice([5, 60, None]), "level": r.choice([1, 4, None]),
                  "swap_used_mb": r.choice([0, 2048.5, None])}
        if load:
            fields.update(load1=load[0], load5=load[1])
        return A.FixedMachine(fields), r.choice([None, NOW + 5])

    def test_pressure_reading(self):
        check(self, "pressure_reading", self.build, lambda A: A.pressure_reading, 6000)


# ------------------------------------------------------------------------------------------------ redfamilies
class RedFamilyTest(unittest.TestCase):
    def build(self, case):
        r = case.rng
        import re
        fams = []
        for i in range(r.randint(0, 4)):
            f = {"name": f"fam{i}", "_jobs": [re.compile(p) for p in r.sample(["^build", "test", "x$"], r.randint(0, 2))]}
            for key, gen in (("workflows", lambda: r.sample(["Main", "Alpha", "Beta"], r.randint(0, 2))), ("outside_gate", lambda: r.choice([True, False, None])),
                             ("kinds", lambda: r.sample(["drift", "checks", "local"], r.randint(0, 2)))):
                if r.random() < 0.6:
                    f[key] = gen()
            fams.append(f)
        o_r = r.choice([A.RED_PRIMARY, A.RED_PRIMARY, A.RED_PRIMARY, "other/repo"])
        case.fake("red_repo_of", lambda rng, d: o_r)
        case.fake("red_repo_conf", lambda rng, o: maybe(rng, {"family": rng.choice(["rf", "", None])}, 0.7))
        case.fake("red_families", lambda rng: fams)
        case.fake("red_outside_gate", lambda rng, d: rng.random() < 0.5)
        d = {}
        for key, gen in (("workflow", lambda: r.choice(["Main", "Alpha", "Zed", None])), ("job", lambda: r.choice(["build x", "test", "other", None, ""])),
                         ("jobs", lambda: [r.choice(["build y", "ztest", None]) for _ in range(r.randint(0, 2))]),
                         ("kind", lambda: r.choice(["drift", "checks", "local", None]))):
            if r.random() < 0.7:
                d[key] = gen()
        return (r.choice([d, d, d, {}, None]),)

    def test_red_family(self):
        check(self, "red_family", self.build, lambda A: A.red_family, 12000)


# ------------------------------------------------------------------------------------------------ config
KNOB_TEXTS = [
    'X = 5  # five things\nY = X * 2\nA = env_num("AGENTCTL_ONE", X)\n',
    'B = os.environ.get("AGENTCTL_TWO", "dflt")  # the two\nC = env_num( "AGENTCTL_THREE" , 3 )\n',
    'env_num("AGENTCTL_ONE", 9)  # later doc ($AGENTCTL_ONE); more ;\n',
    'os.environ.get("AGENTCTL_NODEF")\nenv_num("AGENTCTL_AGENT_NAME", 1)\nos.environ.get("AGENTCTL_ROLE")\n',
    'Z = 2 << 3  # shifts\nq = env_num("AGENTCTL_Z", Z)  # reads\nr = env_num("AGENTCTL_UNKNOWN", SOMETHING)\n',
    'K = "text"  # a K\ns = os.environ.get("AGENTCTL_KEY", K)\nt = env_num("AGENTCTL_TWO", 4)\n',
    "nothing here\n",
    'env_num("AGENTCTL_PIPE", 1)  # a | b\nenv_num("AGENTCTL_NEG", -1)\nenv_num("AGENTCTL_LONG", 1.5)  # c ; \n',
]


class ConfigKnobsTest(unittest.TestCase):
    def build(self, case):
        r = case.rng
        texts = [(f"lib/f{i}.py", "".join(pick(r, KNOB_TEXTS, 1, 3))) for i in range(r.randint(0, 4))]
        case.fake("config_source_text", lambda rng: texts)
        return ()

    def test_config_knobs(self):
        check(self, "config_knobs", self.build, lambda A: A.config_knobs, 4000)


class CmdConfigTest(unittest.TestCase):
    def build(self, case):
        r = case.rng
        holder = {"case": case}
        knobs = [{"name": f"AGENTCTL_K{i}", "key": f"k{i}", "default": r.choice([None, 3, "x", 1.5]), "doc": "d"} for i in range(r.randint(0, 4))]
        case.fake("config_defaults_text", lambda rng: "DEFAULTS\n")
        case.fake("config_doc_text", lambda rng: "DOC")
        case.fake("config_doc_write", lambda rng, path=None: rng.random() < 0.5)
        case.fake("config_files", lambda rng: [r.choice(["/c/a.toml", "/c/b.toml"]) for _ in range(r.randint(0, 2))])
        case.fake("config_knobs", lambda rng: knobs)
        case.patch(os.path, "exists", lambda rng, p: rng.random() < 0.5)
        case.put("CONFIG_ERRORS", [f"err{i}" for i in range(r.randint(0, 2))])
        case.put("CONFIG_SOURCE", {k["name"]: "file" for k in knobs if r.random() < 0.4})
        env = {k["name"]: "7" for k in knobs if r.random() < 0.4}
        a = type("Args", (), {"ccmd": r.choice(["defaults", "doc", "show", None]), "write": r.random() < 0.5, "all": r.random() < 0.5})()
        return (holder, a, env)

    def test_cmd_config(self):
        def run(A):
            def go(holder, a, env):
                with mock.patch.dict(os.environ, env):
                    return printing(holder, A.cmd_config)(a)
            return go
        check(self, "cmd_config", self.build, run, 5000)


# ------------------------------------------------------------------------------------------------ stats
class GateRunsPassTest(unittest.TestCase):
    def build(self, case):
        r = case.rng
        holder = {"case": case}
        meta = {A.STATS_GATE_WALK: r.choice([None, NOW - 40 * 86400, NOW - 2 * 86400, NOW - 10 * 86400])}

        def flows(rng, repo):
            if rng.random() < 0.15:
                raise rng.choice([RuntimeError("x"), SystemExit("y")])
            return [("Main", "main.yml"), ("Conv", "conv.yml")]

        def query(rng, url, q, start=None, end=None):
            if rng.random() < 0.12:
                raise RuntimeError("down")
            if "kind:run " in q:
                return [{"run_id": rng.randint(1, 4), "run_attempt": rng.choice([None, 1, 2]), "repo": rng.choice([A.STATS_GATE_REPO, A.STATS_GATE_REPO, "o/x"]),
                         "workflow_file": rng.choice(["main.yml", "conv.yml", "zz.yml"]), "workflow": "n", "end": rng.choice([None, 5, 3, "7"])}
                        for _ in range(rng.randint(0, 5))]
            return [{"run_attempt": rng.choice([None, 1, 2]), "n": rng.randint(0, 9)} for _ in range(rng.randint(0, 3))]
        case.fake("gate_flows", flows)
        case.fake("stats_query", query)
        case.fake("gate_run_record", lambda rng, run, jobs, since: {"record": "gate_run", "run": run.get("run_id"), "jobs": len(jobs)})
        case.fake("stats_post", lambda rng, url, recs: raise_if(rng, 0.12))
        case.fake("stats_sent", lambda rng, st, kind, rid: rng.random() < 0.3)
        case.fake("stats_echo", lambda rng, recs, out=None: None)
        case.fake("err", lambda rng, m: None)
        st = Probe(case, "st", meta_get=lambda rng, k: meta.get(k))
        return (holder, st, "http://u", NOW, r.choice([None, 12.0]), r.choice([0, 1, 3, 99]), r.choice([None, "out"]))

    def test_gate_runs_pass(self):
        def run(A):
            def go(holder, st, url, t, since, cap, out):
                return A.gate_runs_pass(st, url, t, since, cap, out)
            return go
        check(self, "gate_runs_pass", self.build, run, 6000)


def raise_if(rng, p):
    if rng.random() < p:
        raise RuntimeError("post down")


# ------------------------------------------------------------------------------------------------ blame
class BlameStepTest(unittest.TestCase):
    def build(self, case):
        r = case.rng
        state = r.choice(["probing", "probing", "probing", "done", None])
        b = {"state": state, "since": r.choice([NOW - 10, NOW - 10**7, None, NOW]), "groups": [1, 2, 3], "kind": r.choice(["drift", "checks", None, "other"]),
             "runs": r.choice([None, {}, {"a": 1}]), "probes": [{"ci": r.random() < 0.5} for _ in range(r.randint(0, 5))]}
        if r.random() < 0.5:
            b["probe"] = {"boundary": r.randint(0, 3), "pid": 5}
        d = {"blame": b, "workflow": r.choice(["Main", None]), "last_run": r.choice([None, {"n": 1}]), "job": "j"}
        it = {"data": d, "id": "red:1", "version": 3}
        if r.random() < 0.05:
            it["data"] = {}
        for name in ("blame_probe_stop", "env_num_unused"):
            pass
        case.fake("blame_probe_stop", lambda rng, p: None)
        case.fake("blame_finish", lambda rng, store, it_, b_, why, t: [f"finish {why}"])
        case.fake("fmt_age", lambda rng, s: f"{s:.0f}s")
        case.fake("blame_probe_result", lambda rng, p, t: rng.choice([None, {"verdict": "pass"}, {"verdict": "fail"}]))
        counter = [0]

        def apply(rng, b_, i, v):
            return dict(b_, probe=None, seen=b_.get("seen", 0) + 1), rng.choice([None, None, "decided"])
        case.fake("blame_apply", apply)

        def nxt(rng, b_):
            counter[0] += 1
            return None if counter[0] > 3 or rng.random() < 0.2 else counter[0]
        case.fake("blame_next", nxt)
        case.fake("blame_free", lambda rng, o_r, d_, b_, i, runs: rng.choice([None, None, {"verdict": "pass"}]))
        case.fake("blame_boundary", lambda rng, b_, i: "c" * 12 + str(i))
        case.fake("blame_probe_start", lambda rng, repo, o_r, d_, b_, i: {"boundary": i, "pid": 9})
        case.fake("red_what", lambda rng, d_: "RED w")
        case.fake("dispatch_log", lambda rng, ctx, line: None)
        return (Probe(case, "store"), it, "/repo", "o/r", r.choice([NOW, NOW + 1]))

    def test_blame_step(self):
        def run(A):
            def go(store, it, repo, o_r, t):
                return A.blame_step(store, it, repo, o_r, t)
            return go
        check(self, "blame_step", self.build, run, 8000)


if __name__ == "__main__":
    unittest.main()
