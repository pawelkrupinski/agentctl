"""RESTRUCTURE ROUND 4: each function split into named stages keeps its behaviour. The digests (fixtures/
restructure4-digests.json, recorded from the code BEFORE the split with `R4_RECORD=1`) cover thousands of seeded cases
each, collaborators faked (golden_cases.py): every call, its order and the result."""
import types
import os, sys, unittest
from functools import partial

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402
from harness import case_context, context_with  # noqa: E402
from golden_cases import Probe, check  # noqa: E402


def entry(rng, i):
    e = {"id": f"e{i}", "seq": rng.randint(1, 9), "status": rng.choice(["queued", "checking", "checked", "pushing", "landed"]),
         "worktree": rng.choice(["w1", "w2"]), "branch": rng.choice(["b", ""])}
    for key, gen in (("backlog_batch", lambda: True), ("retry_after", lambda: rng.randint(0, 100)),
                     ("regen_hold", lambda: {"files": ["f"], "cmd": "c"}), ("why", lambda: "infra: x")):
        if rng.random() < 0.25:
            e[key] = gen()
    return e


def maybe(rng, value, p=0.5):
    return value if rng.random() < p else None


class TickChangesPassTest(unittest.TestCase):
    def build(self, case):
        r = case.rng
        A._behind_said.clear()
        entries = [entry(r, i) for i in range(r.randint(1, 4))]
        st = Probe(case, "st", put_item=lambda rng, *a: {"state": rng.choice(["queued", "checking", "checked", "pushing"]),
                                                          "deadline": rng.randint(0, 100)})
        slot = lambda rng, *a, **k: maybe(rng, (1, Probe(case, "slot")), 0.7)  # noqa: E731
        batch = lambda rng, *a: maybe(rng, {"id": "bt", "state": "assembling"}, 0.3)  # noqa: E731
        for name, gen in (
                ("change_item_fields", lambda rng, e: {"f": e["id"]}), ("set_change", lambda rng, *a, **k: None),
                ("classify_change", lambda rng, *a: {"red_fix": rng.random() < 0.3}),
                ("record_change_kind", lambda rng, *a: None), ("note_hold", lambda rng, *a, **k: None),
                ("_push_checked", lambda rng, *a: [f"pushed{rng.randint(0, 9)}"] * rng.randint(0, 2)),
                ("checker_alive", lambda rng, *a: rng.random() < 0.3), ("requeue_infra", lambda rng, *a: None),
                ("lap_change_deadline", lambda rng, *a: None),
                ("main_conflict", lambda rng, *a: maybe(rng, ("a" * 12, ["p"]), 0.25)),
                ("queue_verdict", lambda rng, *a, **k: None), ("conflict_cause", lambda rng, *a: " c"),
                ("conflict_paths", lambda rng, *a: ["p"]), ("remote", lambda rng: "origin"),
                ("queued_waiting_on_changes", lambda rng, *a: ([f"w{rng.randint(0, 3)}"] * rng.randint(0, 1), rng.random() < 0.4)),
                ("regen_hold_overdue", lambda rng, *a: rng.random() < 0.5),
                ("retry_hold", lambda rng, e: ("retry", "why")), ("batch_goes_first", batch),
                ("behind_batch", lambda rng, *a: [f"bb{rng.randint(0, 3)}"] * rng.randint(0, 1)),
                ("check_tree_slot", slot), ("check_tree_slot_free", lambda rng, *a: rng.random() < 0.5),
                ("policy_blocks", lambda rng, *a: rng.random() < 0.6), ("spawn_executor", lambda rng, *a: None),
                ("hhmmz", lambda rng, x: f"h{x}")):
            case.fake(name, gen)
        ids = [e["id"] for e in entries]
        pick = lambda: {i for i in ids if r.random() < 0.3}  # noqa: E731
        kinds = {e["id"]: {"red_fix": r.random() < 0.3} for e in entries if r.random() < 0.5}
        return ("repo", "d", st, "clock", r.randint(0, 100), {}, kinds, r.choice(["push", "check"]), entries, entries,
                pick(), pick(), r.choice([None, 3, 8]), {})

    def test_tick_changes_pass(self):
        check(self, A, "_tick_changes_pass", self.build, lambda A: partial(A._tick_changes_pass, ctx=case_context()))


def item(rng, i, states):
    return {"id": f"i{i}", "state": rng.choice(states), "entered_at": rng.randint(3000, 5000),
            "data": rng.choice([None, {}, {"landed_sha": f"s{rng.randint(0, 3)}", "submitted_at": rng.randint(2000, 4000)}])}


def raising(rng, p, value):
    if rng.random() < p:
        raise RuntimeError("down")
    return value


class StatsPassTest(unittest.TestCase):
    def build(self, case):
        r, ticks = case.rng, [0]

        def monotonic(rng):
            ticks[0] += rng.randint(0, 6)
            return ticks[0]
        clock = Probe(case, "clock", time=lambda rng: 5000, monotonic=monotonic)
        items = lambda rng, typ, states=None: [item(rng, i, states or ["landed", "queued", "rejected", "ok", "red", "verified"])  # noqa: E731
                                              for i in range(rng.randint(0, 3))]
        st = Probe(case, "st", meta_get=lambda rng, k, default=None: maybe(rng, rng.randint(1000, 4000), 0.4) if k == "stats_since" else maybe(rng, 1, 0.2),
                   meta_set=lambda rng, *a: None, items=items)
        for name, gen in (
                ("stats_url", lambda rng: maybe(rng, "http://u", 0.85)),
                ("env_number", lambda rng, name, default: rng.choice([1, 3, 20, 900]) if "SETTLE" in name else rng.choice([2, 4, 150]) if "PER_PASS" in name else rng.choice([5, 40])),
                ("waiting_entries", lambda rng, d, open_only=True: [{"id": f"i{rng.randint(0, 3)}"} for _ in range(rng.randint(0, 2))]),
                ("item_events", lambda rng, st, typ, iid: [{"to_state": "x"}]),
                ("check_runs_of", lambda rng, *a: raising(rng, 0.2, [{"run_id": rng.randint(0, 2)}])),
                ("change_paths", lambda rng, *a: ["p"]),
                ("landing_records", lambda rng, *a: [{"record": "landing", "backfill": rng.random() < 0.5}] * rng.randint(1, 2)),
                ("watch_record", lambda rng, *a: {"record": "watch", "backfill": rng.random() < 0.5}),
                ("red_record", lambda rng, *a: {"record": "red", "backfill": False}),
                ("owner_decision_record", lambda rng, *a: maybe(rng, {"record": "od", "backfill": True}, 0.4)),
                ("condition_record", lambda rng, *a: {"record": "cond", "backfill": False}),
                ("stats_post", lambda rng, url, recs: raising(rng, 0.15, None)),
                ("stats_echo", lambda rng, *a: None), ("change_waits", lambda rng, *a: [("i1", 1, 2)]),
                ("gate_runs_pass", lambda rng, *a: rng.randint(0, 3)), ("print_error", lambda rng, m: None)):
            case.fake(name, gen)
        return (clock, st, "repo", maybe(r, "dir", 0.8), maybe(r, "http://given", 0.5), None)

    def test_stats_pass(self):
        check(self, A, "stats_pass", self.build, lambda A: A.stats_pass)


NAMES = ["build", "test", "test (a, b)", "test (c)", "deploy / test", "x / build (a)", "lint", "test (a, ...", "Build ${{ x }}", "gone job"]


def job_text(rng, key):
    """A workflow job's block, the way fix_workflow_jobs hands it over, with every section optional."""
    lines = [f"  {key}:"]
    if rng.random() < 0.7:
        lines.append(f"    name: {rng.choice(['build', 'test', 'Test ${{ matrix.os }}', repr('quoted'), 'lint # c'])}")
    if rng.random() < 0.6:
        lines += ["    strategy:", "      matrix:" + rng.choice(["", " ${{ fromJson(x) }}"])]
        for _ in range(rng.randint(0, 3)):
            lines.append(rng.choice(["        os: [a, b]", "        os: c", "        - a", "        include:", "          - os: d",
                                     "        k: ${{ x }}", "        v: |", "        # note", "        x: [a, , b]"]))
    if rng.random() < 0.7:
        lines.append("    steps:")
        for _ in range(rng.randint(0, 3)):
            lines += rng.choice([["      - name: s1", "        if: inputs.x"], ["      - run: y", "        name: s2", "        if: matrix.os == 'a'"],
                                 ["      - uses: z"], ["      - name: s3", "        run: |", "          if: no"],
                                 ["      -   name: s4", "          if: github.event.inputs.q"]])
    if rng.random() < 0.15:
        lines = [f"  {key}:", "    # only a comment"]
    return "\n".join(lines)


def workflow_text(rng):
    if rng.random() < 0.1:
        return rng.choice(["", "name: x\non: push\n", "jobs:\n"])
    return "name: w\non: push\njobs:\n" + "\n".join(job_text(rng, k) for k in rng.sample(["build", "test", "deploy", "lint"], rng.randint(1, 3)))


class RedRetiredTest(unittest.TestCase):
    def test_retire_jobs_of(self):
        check(self, A, "retire_jobs_of", lambda case: (workflow_text(case.rng),), lambda A: A.retire_jobs_of, 4000)

    def jobs_of(self, rng):
        return A.retire_jobs_of(workflow_text(rng))

    def test_red_job_retired(self):
        def build(case):
            r = case.rng
            cur = self.jobs_of(r) or {"x": {"name": None, "matrix": None, "steps": []}}
            old = r.choice([None, self.jobs_of(r), cur])
            if old and r.random() < 0.4:  # the job as it was: its steps now fewer, its names the run's
                cur = {k: {**j, "steps": j["steps"][:r.randint(0, 1)]} for k, j in old.items()}
            steps = r.choice([None, [{"name": "s1", "conclusion": "success"}], [{"name": "s2", "conclusion": "failure"}], [{"name": r.choice(["s1", "s2", "s3", "s4"]), "conclusion": r.choice(["success", "failure", "skipped"])}
                                     for _ in range(r.randint(0, 3))]])
            return (r.choice(NAMES + ["build", "test", "lint"]), cur, old, steps)
        check(self, A, "red_job_retired", build, lambda A: A.red_job_retired, 4000)

    def test_red_run_retired(self):
        def build(case):
            r = case.rng
            A._retire_cache.clear()
            date = lambda: f"2026-11-{r.randint(1, 25):02d}T10:00:00Z"  # noqa: E731
            texts = [workflow_text(r) for _ in range(2)]
            for name, gen in (
                    ("retire_main", lambda rng, o_r: raising(rng, 0.05, ("a" * 12, date()))),
                    ("retire_flows", lambda rng, o_r, sha: raising(rng, 0.03, {
                        "W": (rng.choice([".github/workflows/w.yml", ".github/workflows/v.yml"]), texts[0]),
                        "V": (".github/workflows/v.yml", rng.choice([texts[1], "name: v\non:\n  workflow_call:\njobs:\n  a:\n    steps: []"]))})),
                    ("retire_run", lambda rng, o_r, rid: {"path": rng.choice([".github/workflows/w.yml@main", ".github/workflows/v.yml", "dynamic/x", ""]),
                                                          "created_at": date(), "head_sha": rng.choice(["h", "a" * 12, None])}),
                    ("retire_newest", lambda rng, runs, o_r, path: maybe(rng, {"created_at": date(), "head_sha": rng.choice(["h", "a" * 12])}, 0.7)),
                    ("retire_file", lambda rng, o_r, sha, path: maybe(rng, rng.choice(texts), 0.8)),
                    ("retire_jobs", lambda rng, o_r, rid: [{"name": rng.choice(NAMES), "conclusion": rng.choice(["failure", "success", "timed_out"]),
                                                            "steps": [{"name": "s1", "conclusion": "failure"}]} for _ in range(rng.randint(0, 3))]),
                    ("env_number", lambda rng, name, default: rng.choice([1, 14, 100]))):
                case.fake(name, gen)
            run = r.choice([{}, {"databaseId": 7, "headSha": "h", "number": 3}, {"id": 8}, None])
            return (r.choice(["o/r", "p/q"]), r.choice(["W", "V", "Z", None]), run,
                    r.choice([None, [], ["build"], ["test (a, b)", "?"], ["gone job", None]]), 1_795_000_000 + r.randint(0, 40) + r.choice([0, 86400 * 30]))

        def wrapped(A):
            def call(*args):
                out = A.red_run_retired(*args, ctx=case_context())
                why = A._retire_cache.get(("why", args[0]))
                return out, why and why[1], ("fail", args[0]) in A._retire_cache
            return call
        check(self, A, "red_run_retired", build, wrapped, 4000)


TREE = ["scripts/a.py", "scripts/b.sh", "scripts/lib/util.py", "scripts/lib.sh", "scripts/x_test.py", "scripts/y-test.sh",
        ".github/workflows/w.yml", ".github/workflows/v.yml", ".github/actions/act/action.yml", ".github/actions/two/action.yaml",
        "android/app/build.gradle", "android/src/m.kt", "ios/Package.swift", "ios/scripts/t.sh", "docs/readme.md", "Makefile"]
SCRIPT_LINES = ["import util", "from lib import util", "source scripts/lib.sh", ". ./lib.sh", "run scripts/a.py now", "uses: ./.github/actions/act",
                "uses: ./.github/actions/two/", "for t in '*_test.py'; do", "find scripts -name '*-test.sh'", "cd android", "--package-path ios",
                "sbt test", "sbt Foo/testOnly BarSpec", "lint .github/workflows/v.yml", "cat scripts/a.py scripts/b.sh", "x ios/scripts/t.sh", "see docs/readme.md", "shellcheck source=lib.sh", "echo Hello", ""]


def script_text(rng):
    return "\n".join(rng.choice(SCRIPT_LINES) for _ in range(rng.randint(0, 6)))


def workflow_with_steps(rng):
    steps = ["    steps:"]
    for i in range(rng.randint(1, 4)):
        steps += [f"      - name: step{i}", f"        run: {rng.choice(SCRIPT_LINES) or 'true'}"] + rng.choice([[], ["        if: inputs.x"]])
    return "name: w\non: push\njobs:\n  job1:\n    name: Job One\n" + "\n".join(steps) + "\n  job2:\n    steps:\n      - run: sbt test\n"


class RedFixReadTest(unittest.TestCase):
    def test_red_job_reads(self):
        def build(case):
            r = case.rng
            texts = {}

            def show_many(rng, repo, sha, paths):
                return {p: texts.setdefault(p, script_text(rng)) for p in paths if rng.random() < 0.85}
            case.fake("fix_tree_files", lambda rng, repo, sha: rng.sample(TREE, rng.randint(0, len(TREE))))
            case.fake("fix_show", lambda rng, repo, sha, path: maybe(rng, workflow_with_steps(rng), 0.9))
            case.fake("fix_show_many", show_many)
            case.fake("red_failed_steps", lambda rng, d: rng.choice([[], ["step1"], ["step0", "step2"], ["Run sbt test"]]))
            d = {"workflow": r.choice(["w", "w.yml", "x"]), "jobs": r.choice([["job1"], ["Job One (a)"], ["job2 / x"], []]),
                 "line": r.choice(["", "FooSpec failed"])}
            return ("repo", "sha", d)
        check(self, A, "red_job_reads", build, lambda A: partial(A.red_job_reads, ctx=case_context()), 3000)

    def test_fix_job_steps(self):
        def build(case):
            r = case.rng
            lines = ["    name: n", "    runs-on: x", "    steps:", "      - name: one", "        run: a", "      - uses: z", "        name: two",
                     "        with:", "          name: not-a-step", "      - run: b", "        if: c", "    env:", "      A: b", "    # comment", "",
                     "      -   name: odd", "        - name: nested", "    timeout-minutes: 1"]
            return ("\n".join(r.choice(lines) for _ in range(r.randint(0, 12))),)
        check(self, A, "fix_job_steps", build, lambda A: A.fix_job_steps, 4000)

    def test_fix_check(self):
        def build(case):
            r = case.rng
            case.fake("fix_cache", lambda rng, sha: ("path", {v: rng.choice([{"ok": True}, {"ok": False, "why": "w"}, {"ok": False}])
                                                              for v in ["1", "2", "3"] if rng.random() < 0.3}))
            case.fake("fix_changed", lambda rng, repo, sha: ["p"])
            case.fake("fix_red_verdict", lambda rng, repo, sha, d: rng.choice([{"ok": True}, {"ok": False, "why": "no"}]))
            case.fake("fix_cache_put", lambda rng, *a: None)
            case.fake("fix_not_counted_tell", lambda rng, *a: None)
            items = [{"data": {"key": f"k{i}", "runs": r.sample(["1", "2", "3", 4], r.randint(0, 3)),
                               "workflow": r.choice(["w", "Local checks"])}} for i in range(r.randint(0, 3))]
            items += [{"data": {}}] * r.randint(0, 1)
            store = Probe(case, "store", items=lambda rng, kind: items)
            if r.random() < 0.1:
                store = Probe(case, "store", items=lambda rng, kind: 1 / 0)
            return ("repo", "sha", r.sample(["1", "2", "3", "local", " 5", "#2", ""], r.randint(0, 5)), store) if r.random() < 0.95 else (None, "sha", ["1"], store)
        check(self, A, "fix_check", build, lambda A: partial(A.fix_check, ctx=case_context()), 4000)


NOW = 1_795_000_000.0
ISO = ["2026-11-20T10:00:00Z", "2026-11-01T10:00:00Z", "2026-10-01T10:00:00Z", None]


class RedFixesTest(unittest.TestCase):
    @staticmethod
    def clock(case):
        import types
        case.put("time", types.SimpleNamespace(time=lambda: NOW))
        case.fake("ts", lambda rng: "2026-11-24T10:00:00Z")

    def red_item(self, r, i):
        return {"id": f"r{i}", "data": {"key": r.choice([None, f"k{i}", "k1"]), "runs": r.sample(["1", "2", 3], r.randint(0, 2)),
                                        "refuted": r.sample(["sa", "sb", "sc"], r.randint(0, 2)), "unverified": r.sample(["sd", "sa"], r.randint(0, 1))}}

    def test_red_fix_facts(self):
        import types

        def build(case):
            r = case.rng
            self.clock(case)
            entries = [{"id": f"e{i}", "status": r.choice(["queued", "landed", "waiting", "rejected"]), "landed_at": r.choice(ISO),
                        "updated": r.choice(ISO), "created": "2026-11-02T10:00:00Z", "fixes": r.choice([[], ["1"]]),
                        "landed_sha": r.choice([None, "sa", "sb", "se"]), "commits": ["c"]} for i in range(r.randint(0, 4))]
            reds = [self.red_item(r, i) for i in range(r.randint(0, 3))]

            def state(rng, repo):
                if rng.random() < 0.15:
                    raise SystemExit("no token")
                return ("d", "c", "w")
            case.fake("waiting_state", state)
            case.fake("waiting_entries", lambda rng, d, open_only=True: entries)
            case.fake("entry_fixes", lambda rng, repo, e, store: rng.choice([[], ["1"], ["2", "3"]]))
            case.fake("red_unrelated", lambda rng, repo, e: rng.random() < 0.3)
            case.fake("landed_fix_commits", lambda rng, store, repo: raising(rng, 0.15, [("sx", 5.0, {"1"}, "s")] * rng.randint(0, 2)))
            case.put("patch_id", lambda repo, sha: None if sha.endswith("b") else 1 / 0 if sha.endswith("c") else f"p{sha}")
            case.fake("git", lambda rng, repo, *a, **k: types.SimpleNamespace(returncode=rng.choice([0, 0, 1]), stdout=rng.choice(["123\n", "x", ""])))
            store = Probe(case, "store", items=lambda rng, kind: raising(rng, 0.1, reds))
            return (maybe(r, "repo", 0.85), maybe(r, store, 0.9))
        def facts_of(A):  # the patches dict's order follows a set's: compared sorted
            def call(*args):
                out = A.red_fix_facts(*args, ctx=case_context())
                return {**out, "patches": sorted(out["patches"].items())}
            return call
        check(self, A, "red_fix_facts", build, facts_of, 3000)

    def test_red_fix_candidates(self):
        def build(case):
            r = case.rng
            self.clock(case)
            case.fake("fix_reaches_red", lambda rng, repo, sha, d: rng.random() < 0.6)
            case.fake("red_change_link", lambda rng, repo, d, commits: maybe(rng, "linked", 0.5))
            ents = lambda: [{"id": f"e{i}", "status": r.choice(["queued", "checking", "landed", "rejected"]), "created": r.choice(ISO),  # noqa: E731
                             "landed_at": r.choice(ISO), "updated": r.choice(ISO), "fixes": r.sample(["1", "2", "#3", "9"], r.randint(0, 3)),
                             "diagnoses": r.choice([[], ["2"]]), "commits": r.choice([[], ["c1", "c2"]]), "landed_sha": r.choice([None, "sa", "sb"]),
                             "worktree": "w", "session": "s"} for i in range(r.randint(0, 3))]
            facts = {"entries": ents(), "untrailered": ents(), "landed": [(f"s{i}", r.choice([0.0, 50.0, 2e9, -3600.0, 1e9 - 3600]), set(r.sample(["1", "2", "k"], r.randint(0, 2))), "s")
                                                                         for i in range(r.randint(0, 3))],
                     "key_runs": {"k": r.sample(["1", "4"], r.randint(0, 2))}, "landed_at": r.choice([{}, {"sa": 1e9}, {"sa": 3e9}]), "repo": r.choice([None, "repo"])}
            d = {"key": r.choice(["k", "z"]), "runs": ["1", 2], "opened_at": r.choice([0, 1e9, 2e9]), "gate": r.choice([None, True]),
                 "history": [{"outcome": r.choice(["LANDED abcdef12", "x", None]), "at": r.choice(["2026-11-20T10:00:00Z", None, "junk"])}
                             for _ in range(r.randint(0, 3))]}
            return (d, facts)
        check(self, A, "red_fix_candidates", build, lambda A: lambda d, facts: list(A.red_fix_candidates(d, facts, ctx=case_context())), 4000)

    def test_red_run_on_way(self):
        import types

        def build(case):
            r = case.rng
            case.fake("git", lambda rng, repo, *a, **k: types.SimpleNamespace(returncode=rng.choice([0, 0, 1]), stdout=" ".join(f"c{i}" for i in range(rng.randint(0, 3)))))
            case.fake("fix_reaches_red", lambda rng, repo, sha, d: rng.random() < 0.3)
            run = lambda: {"status": r.choice(["completed", "in_progress", "queued", "queued"]), "headBranch": r.choice(["main"] * 9 + ["x"]),  # noqa: E731
                           "headSha": r.choice([None, "h1", "h2", "h2", "base"]), "databaseId": r.randint(0, 9)}
            snap = {"gate": [{"name": "w", "runs": [run() for _ in range(r.randint(0, 4))]}], "outside": [{"name": "w", "runs": [run()]}]}
            snap = r.choice([None, {}] + [snap] * 8)
            d = {"last_run": r.choice([None, {"headSha": "base", "databaseId": r.randint(0, 5)}, {"headSha": "base", "databaseId": 2}, {"databaseId": 2}]),
                 "workflow": "w"}
            return (d, snap, r.choice([None, "repo"]))
        check(self, A, "red_run_on_way", build, lambda A: partial(A.red_run_on_way, ctx=case_context()), 3000)


class ConflictsTest(unittest.TestCase):
    def test_hunks_resolve(self):
        import types
        hunk = ["<<<<<<< ours\n", "x\n", "||||||| base\n", "y\n", "=======\n", "z\n", ">>>>>>> theirs\n"]
        odd = ["<<<<<<< stray\n", "=======\n", "||||||| base\n", ">>>>>>> theirs\n", "last"]

        def diff3(rng):
            lines = []
            for _ in range(rng.randint(0, 4)):
                lines += rng.choice([["a\n"], ["b\n"], hunk, hunk, hunk[:3] + hunk[4:], hunk[:5] + ["=======\n"] + hunk[5:], [rng.choice(odd)]])
            return "".join(lines)

        def build(case):
            r = case.rng
            case.fake("merge_diff3", lambda rng, base, ours, theirs: types.SimpleNamespace(
                returncode=rng.choice([0, 1, 1, 1, 2, -1, 200]), stdout=diff3(rng)))
            case.fake("hunk_take", lambda rng, o, b, t: maybe(rng, ([f"took{rng.randint(0, 2)}\n"] * rng.randint(0, 2), "what"), 0.8))
            return ("base", r.choice(["ours\n", "ours", ""]), "theirs")
        check(self, A, "hunks_resolve", build, lambda A: A.hunks_resolve, 5000)

    def test_pick_commits(self):
        import types

        def git(rng, wt, *args, check=True, **k):
            word = next((a for a in args if a in ("rev-parse", "cherry-pick", "reset", "commit", "diff")), "x")
            out = f"sha{rng.randint(0, 2)}" if word == "rev-parse" else ""
            if check:
                return out
            return types.SimpleNamespace(returncode=rng.choice([0, 0, 1]) if word != "reset" else 0, stdout=out, stderr=rng.choice(["boom", ""]))

        def build(case):
            r = case.rng
            case.fake("git", git)
            case.fake("upstream_patch_ids", lambda rng, wt, c, start: {f"p{i}": f"v{i}" for i in range(2) if rng.random() < 0.5})
            case.fake("patch_id", lambda rng, wt, c: maybe(rng, f"p{rng.randint(0, 2)}", 0.7))
            case.fake("deliberate_empty", lambda rng, wt, c: rng.random() < 0.2)
            case.fake("conflict_resolve", lambda rng, wt, regen, notes, regenerated, provisional, mine: (
                notes.extend([(["g1"], "why")] * rng.randint(0, 1)), mine.extend([{"file": "f", "hunks": ["h"]}] * rng.randint(0, 1)) if mine is not None else 0,
                rng.random() < 0.6)[2])
            case.fake("snapshots_both_changed", lambda rng, wt, c, before: set(rng.sample(["s1", "s2"], rng.randint(0, 2))))
            case.fake("regenerate", lambda rng, wt, files, regenerated, provisional, sides=None: maybe(rng, "failed", 0.3))
            case.fake("unmerged", lambda rng, wt: rng.sample(["f1", "f2", "g1"], rng.randint(0, 3)))
            case.fake("equivalent_on", lambda rng, wt, c, before: maybe(rng, "eq", 0.4))
            case.fake("is_empty_commit", lambda rng, wt, ref: rng.random() < 0.3)
            commits = [f"c{i}" for i in range(r.randint(0, 3))]
            dropped, resolved = r.choice([None, []]), r.choice([None, []])
            regen = r.choice([True, True, False])
            return ("wt", commits, dropped, regen, r.choice([None, []]), r.choice([None, []]), resolved)

        def call(A):
            def run(*args):
                out = A.pick_commits(*args)
                return out, args[2], args[4], args[5], args[6]
            return run
        check(self, A, "pick_commits", build, call, 5000)


class SmallOnesTest(unittest.TestCase):
    def test_own_inbox_entry(self):
        def build(case):
            r = case.rng
            paths = ["/w/a", "/w/a/sub", "/w/ab", "/w", "rel/x", "", None, "/"]
            e = {k: v for k, v in (("owner", r.choice([None, "s1", "s2", "x@/w/a", "x@rel", "y@/w/a/sub", "z@"])), ("agent", r.choice([None, "n1", "n2"])),
                                   ("cwd", r.choice(paths))) if r.random() < 0.8}
            who = r.choice([None, ("s1", "/w/a", "n1"), (None, "/w/a", None), ("s9", None, "n9"), (None, "/w/a/", "n2"), (None, None, None)])
            case.fake("caller_identity", lambda rng: (rng.choice([None, "s2"]), rng.choice([None, "/w/a"]), rng.choice([None, "n2"])))
            return (e, who)
        check(self, A, "own_inbox_entry", build, lambda A: A.own_inbox_entry, 4000)

    def test_alert_migrate(self):
        import types

        def build(case):
            r = case.rng
            case.put("time", types.SimpleNamespace(time=lambda: NOW))
            entries = [{"id": f"a{i}", "read": r.random() < 0.3, "at": r.choice([None, 1, 2, 3]), "audience": r.choice([None, "x", "info"]),
                        "item": r.choice([None, "i1", "i2"]), "count": r.choice([None, 2]), "surfaced": r.choice([None, 0]), "first_at": r.choice([None, 1])}
                       for i in range(r.randint(0, 6))]
            case.fake("inbox_entries", lambda rng: entries)
            case.fake("alert_route", lambda rng, e: (rng.choice(["a", "info"]), rng.choice(["i1", "i2", "i3"])))
            case.fake("write_json", lambda rng, path, rec: None)
            case.fake("alert_ack", lambda rng, d, eid: None)
            case.fake("alert_index_save", lambda rng, d, idx: None)
            return ("dir",)
        check(self, A, "alert_migrate", build, lambda A: A.alert_migrate, 3000)

    def test_watch_look(self):
        ctx = []  # the case's Context: its CI reader's main_runs recorded as the module function it was

        def build(case):
            r = case.rng
            case.fake("env_number", lambda rng, name, default: rng.choice([10, 100]))
            ctx[:] = [context_with(types.SimpleNamespace(main_runs=case.recorded("main_runs", lambda rng, o_r, wfile: [wfile])))]
            run = lambda rng: maybe(rng, {"databaseId": rng.randint(1, 3), "status": rng.choice(["completed", "queued"]),  # noqa: E731
                                          "updatedAt": "t", "conclusion": rng.choice(["success", "failure"])}, 0.7)
            case.fake("pick_main_run", lambda rng, repo, sha, runs: run(rng))
            case.fake("pick_convergence_run", lambda rng, repo, sha, runs, pushed_at, o_r: run(rng))
            case.fake("is_cancelled", lambda rng, run: rng.random() < 0.2)
            flows = {k: (k.upper(), f"{k}.yml") for k in r.sample(["main", "conv", "drift"], r.randint(0, 3))}
            done = {k: (None, None) for k in flows if r.random() < 0.25}
            on_run = case.recorded("on_run", lambda rng, *a: None) if r.random() < 0.7 else None
            return ("repo", "sha", "o/r", flows, done, 0, r.choice([5, 50, 500]), 200, on_run)

        def call(A):
            def run(*args):
                return A.watch_look(*args, ctx=ctx[0]), args[4]
            return run
        check(self, A, "watch_look", build, call, 4000)


class Round4BackfillTest(unittest.TestCase):
    """Proofs the golden digests alone left loose (round 6's backfill): the failure memo's expiry and the cut message."""

    def test_red_run_retired_failure_memo_expires_and_message_is_cut(self):
        import time
        calls = []

        def main(o_r):
            calls.append(o_r)
            raise RuntimeError("x" * 300)
        saved = A.retire_main
        A.retire_main = main
        try:
            A._retire_cache.clear()
            args = ("o/rb", "W", {"databaseId": 7}, ["build"], 1_795_000_000)
            self.assertIsNone(A.red_run_retired(*args, ctx=case_context()))
            self.assertEqual(len(A._retire_cache[("why", "o/rb")][1]), len("W #None: ") + 200)
            self.assertIsNone(A.red_run_retired(*args, ctx=case_context()))  # within the TTL: not asked again
            self.assertEqual(len(calls), 1)
            A._retire_cache[("fail", "o/rb")] = (A.epoch_now() - A.RETIRE_FAIL_TTL - 1, 0)
            A.red_run_retired(*args, ctx=case_context())  # expired: asked again
            self.assertEqual(len(calls), 2)
            A._retire_cache[("fail", "o/rb")] = (A.epoch_now() - A.RETIRE_FAIL_TTL + 5, 0)
            A.red_run_retired(*args, ctx=case_context())  # still inside
            self.assertEqual(len(calls), 2)
        finally:
            A.retire_main = saved
            A._retire_cache.clear()

    def test_retire_jobs_of_keeps_every_job(self):
        text = "name: w\non: push\njobs:\n" + "".join(f"  j{i}:\n    name: n{i}\n" for i in range(6))
        self.assertEqual(list(A.retire_jobs_of(text)), [f"j{i}" for i in range(6)])

    def test_own_inbox_entry_inside_worktree_only_by_path(self):
        who = (None, "/w/a", None)
        self.assertTrue(A.own_inbox_entry({"cwd": "/w/a/sub"}, who))
        self.assertTrue(A.own_inbox_entry({"owner": "x@/w/a"}, who))
        self.assertFalse(A.own_inbox_entry({"cwd": "/w/ab"}, who))
        self.assertFalse(A.own_inbox_entry({"cwd": "/w/a"}, (None, None, None)))


if __name__ == "__main__":
    unittest.main()
