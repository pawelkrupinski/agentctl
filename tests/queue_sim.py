"""THE QUEUE SIMULATOR (the owner, 2026-11-18): a deterministic, seeded simulation of the land queue over the REAL queue,
batch and push code -- tick_changes, tick_batch / batch_step, check_entry / _check_entry, check_batch and bisect,
push_change / _push_change, push_batch_item, backlog_reconcile, project_changes, close_landed_changes, queue_change,
the store with its items, leases and events -- with only the world around them faked, at the seams the other tests
fake too:

- GIT is a model (SimGit): commits are (parent, patch) pairs, a patch a change's commit (its paths, the reds it fixes,
  whether it breaks the checks, the patches it conflicts with). The few git calls the queue code makes inline
  (rev-parse, cat-file, merge-base, rev-list, diff-tree, checkout, reset, fetch, worktree, diff) are answered from it;
  the heavier helpers (apply_entry's pick, range_paths, cherry_landed, change_landed, the hook's queries) are replaced
  by the model's own answers.
- THE FORGE: main is the model's ref; the push (run_watched of push_argv) fast-forwards it or is refused (a race, the
  hook's red gate); gate_reds is the simulation's red episode; other pushes land on main from outside.
- CI: a change's or a batch's check (run_entry_checks, batch_check) passes unless the tip carries a breaking patch
  (named by a failing test of its own) or an infra failure is drawn. A check takes ticks: the executor (check_entry,
  check_batch) runs in a COOPERATIVE thread that blocks inside the faked check, holding its real leases, until its
  tick comes -- one thread runs at a time, so a seed replays exactly.
- AGENTS: arrivals (plain, identity, red fix, --no-batch), re-lands after a conflict or a failure, supersedes, cancels.

After every tick the invariants (Sim.check_invariants) are asserted; a violation names the seed, the tick and what.
"""
import hashlib, json, os, random, re, shutil, subprocess, sys, tempfile, threading, types

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402
from fake_clock import FakeClock, on_clock  # noqa: E402

TICK = 60  # s of simulated time per reconciler tick
# THE INVARIANT KINDS (tests/sim-invariants.tsv: each one's version, definition fingerprint and violation baseline;
# tests/sim_gate.py holds every agentctl change to them). The numbers are the module docstring's of test_queue_sim.py.
INVARIANT_KINDS = {
    "I1": "one open item per change (worktree + head)",
    "I2": "never two pushes of one change; never a decided change's commit pushed",
    "I3": "a red fix is never behind a batch or a non-red change (its own tree, started and pushed when it may go)",
    "I4": "a batch reaches landed, bisecting or held within its bound",
    "I5": "the oldest change lands within a bound when nothing fails",
    "I6": "no change lost (entries and items agree, LANDED is on main, every change decided after the drain)",
    "I7": "exactly one agentctl.landing record per decided change",
    "I8": "a change that no longer applies was told at queue time and never checked meanwhile",
    "I9": "no full recheck for a file overlap with a change open when it was queued",
    "I10": "no change held behind a queued one for generated files alone",
    "I11": "no unverified combined tree reaches main",
    "Ib": "every queued or checked change past its deadline has a recorded hold",
    "Ic": "no change past its deadline is silent in `land status`",
    "Id": "executors start in the documented priority order within a tick",
    "IX": "no executor for a decided change",
    "IW": "a red fix of an open gate red the store names never waits for green past N ticks",
    "IK": "a tools-only change failing only as the open gate red fails never waits for green past N ticks",
    "IR": "the queue never lands on a red gate a production commit naming no run of the open red",
    "IH": "two holders reading no hold at once make exactly one hold, both report it and both moves are said",
    "IT": "no overlap hold outlives its TTL while its holder is not in flight",
    "IE": "never two escalation fixers of one kind, nor more than the cap in all",
    "IO": "an owner-only escalation kind never dispatches",
    "IG": "every dispatched escalation ends cleared or handed to the owner within its budget",
    "IL": "a red whose fixer launches all died at launch of one cause is blocked, never offered to another fixer",
    "IS": "a change reads one state whichever reader asks: its entry's status is its item's state after every tick",
    "IC": "the item carries the record: an open change's item holds its entry but its status after every tick",
}
MAIN_FAILS_LINE = "[info] - main already fails *** FAILED ***"  # the test the open gate red's run fails (main_fails)
VIOLATIONS_MAX = 50  # lines one scenario keeps in the gate's count mode
ROOT = "0" * 40
HOOK_TEXT = """#!/bin/sh
# the simulated repo's hook: never run (the checks are the simulator's); its text tells agentctl what it supports:
# --checks-only --fast-only --verify-only
case "$1" in --plan) ;; --compile-plan) ;; --lint-plan) ;; --per-file-lints) ;; --coupling-plan) ;; --quality-plan) ;; esac
exit 0
"""
SBT_MODULES = ("web", "worker", "common", "scrapers", "geodata", "identity")
# the simulated build's module graph, downstream: a module's test configs and those of every module depending on it
# compile when it changes (movies' compile_plan), all of them meeting in e2e
DOWNSTREAM = {"geodata": ("common",), "common": ("identity", "web"), "identity": ("scrapers",), "scrapers": ("worker",),
              "worker": ("e2e",), "web": ("e2e",), "e2e": ()}


def compile_plan(paths):
    """The simulated hook's --compile-plan: every module downstream of a changed one (MINIMAL RECHECKS reads it)."""
    out, todo = set(), [p.split("/")[0] for p in paths if p.split("/")[0] in SBT_MODULES]
    while todo:
        m = todo.pop()
        if m not in out:
            out.add(m)
            todo.extend(DOWNSTREAM.get(m, ()))
    return sorted(f"{m}/Test/compile" for m in out)


def plan_checks(paths):
    """The simulated hook's --plan: `sbt` for code, scripts and workflows, the identity area, the script tests."""
    out = set()
    for p in paths:
        top = p.split("/")[0]
        if top in SBT_MODULES or top in ("scripts", ".github", "test"):
            out.add("sbt")
        if top == "identity":
            out.add("identity")
        if top == "scripts":
            out.add("scripttests")
    return sorted(out)


# MOVIES' LINT PLAN, AS ITS REGISTRY IS (tests/fixtures/movies-lint-plan.json): for each simulated path's kind (its
# top-level directory), the path-selected lint specs -- `--lint-plan` less the standing `*` rows -- that a real path of
# that kind selects, read off movies' hook (the table's `probes`). Until 2026-11-21 the registry's 175 `@repo` rows were
# selected by any script, workflow, skill, test-tree or build path and its `*.scala` rows by any Scala file, so the
# 11-19..21 replay kept no verdict (ad1c702); the registry now names what each spec opens (movies' landing-speed
# proposal 1): a production source selects the source scans, a workflow the workflow specs, and the two meet only where
# one spec reads both. LINT_PLAN_FIXTURE names another table (the measurement's before / after).
LINT_PLAN_FIXTURE = "movies-lint-plan.json"
_lint_tables = {}


def lint_table(fixture=None, key="lints"):
    name = fixture or LINT_PLAN_FIXTURE
    if name not in _lint_tables:
        with open(os.path.join(HERE, "fixtures", name)) as f:
            table = json.load(f)
        _lint_tables[name] = {k: {kind: tuple(v) for kind, v in table.get(k, {}).items()} for k in ("lints", "per_file")}
    return _lint_tables[name][key]


def lint_plan(paths):
    """The simulated hook's --lint-plan: one standing lint, and the specs each path's kind selects (lint_table)."""
    table = lint_table()
    return ["standing"] + sorted({s for p in paths for s in table.get(p.split("/")[0], ())})


def per_file_lints(paths):
    """The simulated hook's --per-file-lints: of those, the `@per-file` specs (judging each file alone) a kind selects by
    what they read (the table's `per_file`; none in a table from before the mark)."""
    table = lint_table(key="per_file")
    return sorted({s for p in paths for s in table.get(p.split("/")[0], ())})


def ratchet_plan(paths):
    """The simulated hook's --coupling-plan / --quality-plan: production code and the ratchets' baselines (movies'
    coupling_paths / quality_paths), never the rest of test/resources/."""
    return [p for p in paths if "/src/main/" in p or p in BASELINES]


def reads_of(paths):
    """What the simulated hook's checks read for paths, but its compiles (compiles_against): MINIMAL RECHECKS, the
    simulation's own truth."""
    return ({("plan", x) for x in plan_checks(paths) if x != "sbt"}
            | {("lint", x) for x in set(lint_plan(paths)) - {"standing"} - set(per_file_lints(paths))}
            | ({("ratchets",)} if ratchet_plan(paths) else set()))


def compiles_against(a, b):
    """Whether one side's code is compiled against the other's (the simulation's truth): a config of one side's compile
    plan compiles a module the other side changes -- never a module merely downstream of both (e2e)."""
    own = lambda ps: {f"{p.split('/')[0]}/Test/compile" for p in ps if p.split("/")[0] in SBT_MODULES}
    return bool(set(compile_plan(a)) & own(b) or set(compile_plan(b)) & own(a))


def interact(a, b):
    """Whether a check of one side reads what the other changed (MINIMAL RECHECKS, the simulation's truth)."""
    return bool(reads_of(a) & reads_of(b)) or compiles_against(a, b)
# the simulated repo's GENERATED files (red-repos.json `generated`; waiting.py GENERATED FILES ARE NO OVERLAP)
BASELINES = ("test/resources/coupling-baseline.tsv", "test/resources/complexity-baseline.tsv",
             "test/resources/length-baseline.tsv", "test/resources/duplication-baseline.tsv")
# and its SNAPSHOTS (`merge: regen`: waiting.py SNAPSHOTS ARE REGENERATED, NEVER MERGED), movies' page snapshots
SNAPSHOTS = tuple(f"test/resources/fixtures/08-06-2026/expected-{n}.html" for n in ("index", "warszawa-index",
                                                                                  "wroclaw-index"))
GENERATED = BASELINES + SNAPSHOTS


class KeptConnection:
    """The store's sqlite connection, opened once per seed (Store.db opens one per call: a connect, WAL and the schema,
    ~1 ms each, most of a seed's time); close() only ends an open transaction, as closing would. One thread runs at a
    time (the cooperative executors), so one connection serves them all."""

    def __init__(self, path):
        import sqlite3
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.c = sqlite3.connect(path, timeout=30, isolation_level=None, check_same_thread=False)
        self.c.row_factory = sqlite3.Row
        self.c.execute("PRAGMA journal_mode=WAL")
        self.c.execute("PRAGMA synchronous=OFF")
        A.Store.prepare(self.c)

    def __getattr__(self, name):
        return getattr(self.c, name)

    def close(self):
        if self.c.in_transaction:
            self.c.execute("ROLLBACK")


class SimOs(types.ModuleType):
    """agentctl's `os` in the simulation: each executor thread is a process of its own (getpid), and a signal to it
    (supersede's and `land cancel`'s SIGTERM to the change's checker) ends that thread as the signal would end the
    process: Owned's handler raises Terminated in a checker, so its finally blocks release its leases."""

    def __init__(self, sim):
        super().__init__("os")
        object.__setattr__(self, "_sim", sim)

    def __getattr__(self, name):
        return getattr(os, name)

    def getpid(self):
        task = getattr(self._sim.local, "task", None)
        return task.pid if task else os.getpid()

    def kill(self, pid, sig):
        task = next((t for t in self._sim.tasks if t.pid == pid), None)
        if task is None:
            if pid == os.getpid():
                raise Violation(f"(sim) agentctl signalled its own process ({sig})")
            raise ProcessLookupError(pid)
        if task.done:
            raise ProcessLookupError(pid)
        if sig == 0:
            return
        if task is getattr(self._sim.local, "task", None):
            raise Violation(f"(sim) executor {task.name} signalled itself")
        task.abort = True  # the process ends: its thread unwinds (its leases released, as Owned's handler does)
        task.step()


class SimClock(FakeClock):
    """The simulated clock as agentctl's Clock (lib/agentctl/clock.py: THE CLOCK IS PASSED IN): its time is the
    simulation's cell (the run moves it a tick at a time), its sleeps return at once and move nothing."""

    def __init__(self, cell):
        super().__init__(cell[0])
        self.cell = cell

    def time(self):
        return self.cell[0]

    def monotonic(self):
        return self.cell[0]

    def advance(self, secs):
        pass


def sim_datetime(clock):
    """agentctl's `dt` (the datetime module) in the simulation: now() reads the simulated clock, so entry ids
    (YYYYMMDD-HHMMSS-...) and stamps never depend on the wall clock -- ids crossing a real second boundary reordered a
    seed's queue from one run to the next."""
    import datetime as real

    class Datetime(real.datetime):
        @classmethod
        def now(cls, tz=None):
            return real.datetime.fromtimestamp(clock[0], tz)

        @classmethod
        def utcnow(cls):
            return real.datetime.fromtimestamp(clock[0], real.timezone.utc).replace(tzinfo=None)
    mod = types.ModuleType("datetime")
    mod.__dict__.update({k: v for k, v in real.__dict__.items() if not k.startswith("__")})
    mod.datetime = Datetime
    return mod


class SimAbort(BaseException):
    """Unwinds an executor thread still blocked at the end of a seed (its finally blocks release its leases)."""


class Violation(AssertionError):
    pass


def letters(n):
    """A name with no digits (the failure keys normalize numbers away: two changes' tests must not read as one)."""
    return "".join(chr(97 + int(ch)) for ch in str(n))


class Patch:
    def __init__(self, pid, cid, paths, fixes=(), bad=False, conflicts=()):
        self.pid, self.cid, self.paths, self.fixes = pid, cid, list(paths), set(fixes)
        self.bad, self.conflicts = bad, set(conflicts)
        self.gen_conflicts = set()  # the patches it conflicts with textually in GENERATED files only (adjacent rows)
        self.regen_fails = False  # its generated files' resolution fails (no JVM where the queue runs, a timeout)
        self.resolvable = set()  # the patches it conflicts with in source a batch's assembly resolves (both only add)
        self.resolve_bad = False  # resolved, the combined tree fails its checks (the resolution is wrong)
        self.main_fails = False  # while the gate is red its check fails ONLY in the test main's red run fails too


class Change:
    """One submitted version of an agent's change (a worktree's head)."""

    def __init__(self, cid, worktree, kind, patches, base, head, commits, arrived, no_batch=False):
        self.cid, self.worktree, self.kind, self.patches = cid, worktree, kind, patches
        self.base, self.head, self.commits, self.arrived, self.no_batch = base, head, commits, arrived, no_batch
        self.eid = None
        self.cancelled = False


# ----------------------------------------------------------------------------------------------------- cooperative executors
class Task:
    def __init__(self, sim, name, fn):
        self.sim, self.name, self.fn = sim, name, fn
        self.go, self.back = threading.Event(), threading.Event()
        self.due, self.done, self.exc, self.abort = None, False, None, False
        self.pid = 4_000_000 + len(sim.tasks)  # its own process id (SimOs)
        self.thread = threading.Thread(target=self._body, name=f"sim-{name}", daemon=True)

    def _body(self):
        self.go.wait()
        self.go.clear()
        self.sim.local.task = self
        try:
            self.fn()
        except SimAbort:
            pass
        except BaseException as e:  # noqa: B902 -- an executor's crash is a finding: re-raised in the main thread
            self.exc = e
            import traceback
            self.trace = traceback.format_exc()
        finally:
            self.done = True
            self.back.set()

    def step(self):
        """Run the thread until it blocks again or ends (the main thread waits meanwhile: one runs at a time)."""
        self.back.clear()
        self.go.set()
        if not self.back.wait(30):
            raise Violation(f"executor {self.name} neither blocked nor ended within 30 s")
        if self.exc:
            raise Violation(f"executor {self.name} crashed: {self.exc!r}\n{getattr(self, 'trace', '')}")

    def block(self, ticks):
        """Inside the thread: give the main thread back control until tick now + ticks."""
        self.due = self.sim.tick + max(1, ticks)
        self.back.set()
        self.go.wait()
        self.go.clear()
        if self.abort:
            raise SimAbort()
        A.lock_beat(f"sim tick {self.sim.tick}")  # its check's progress renews the leases it holds


# ----------------------------------------------------------------------------------------------------- the simulation
class Sim:
    """One seed's world. Build, run(), then read .violations (empty: every invariant held)."""

    def __init__(self, seed, trees=4, calm=False, script=None, n_changes=None, profile=None):
        self.seed, self.rng = seed, random.Random(seed)
        self.calm = calm
        self.trees = trees
        self.profile = profile or {}
        self.script = script  # a replay: [(tick, spec)] instead of random arrivals
        self.n_changes = n_changes if n_changes is not None else self.rng.randint(4, 10)
        self.tick, self.violations, self.log = 0, [], []
        self.kinds_seen = {}  # invariant kind -> violations of it (tests/sim_gate.py)
        self.patches, self.commits = {}, {ROOT: (None, None)}
        self._anc = {}
        self.main = ROOT
        self.heads = {}
        self.changes, self.by_eid = [], {}
        self.reds, self.red_run, self.red_since, self.red_clears_at, self.red_fix_due = set(), 100, None, None, None
        # SEVERAL RUNS OF ONE RED (2026-11-25, batch #124): the gate reads the newest runs (reds); an OLDER run of the
        # same red is named only by its red item in the store (red_item_runs: what the store holds, learned later by
        # `learn-run`), and a run of another failure by no item at all
        self.red_older, self.red_item, self.red_item_runs, self.fix_waits = None, None, set(), {}
        self.known_red_waits = {}  # ticks each tools-only change failing only as the red fails has waited (IK)
        self.green_ticks = []  # the ticks the gate went green (the forge's)
        self.episode_runs, self.episode_snap = set(), None  # the open red's runs only its episode names (stale_episode)
        self.outside, self.outside_run, self.outside_clears = {}, 5000, {}  # reds outside the gate: {run: red id}
        # their own draws (a stream of their own: a seed without one replays exactly as before they were modelled)
        self.outside_rng = random.Random(f"outside-{seed}")
        self.spawned = []  # (tick, entry id, its priority rank) of every executor started (invariant d)
        self.pushes = []  # (tick, sha, [patches])
        self.pushed_patch_count = {}
        self.tasks = []
        self.local = threading.local()
        self.done_at = {}  # change cid -> the tick its entry was decided
        self.open_at_arrival = {}  # change cid -> the cids of the changes open (queued .. waiting) when it arrived
        self.waiting_at_arrival = {}  # change cid -> the changes `waiting` for green when it arrived or its check began
        self.checks_of = {}  # entry id -> its own check rounds run (run_entry_checks)
        self.red_fix_checks = {}  # entry id -> whether its latest check began as a red fix's (the simulation's truth)
        self.batch_seen = {}  # batch id -> ticks spent in assembling / checking / pushing
        self.batch_tips = {}  # batch id -> the tip it was last seen checking
        self.stats = []
        self.next_cid = 0
        self.rejected_handled = set()
        self.arrivals = self._plan_arrivals()
        self.n_lone_red = 0
        self.holds = {}  # entry id -> [(tick, hold kind, blocking entry, files)]
        self.verified = set()  # every tree (its patches) a check or a batch check passed (INVARIANT 11)
        self.kept_pushes = 0  # pushes of a tree whose checks' verdict was kept (MINIMAL RECHECKS)
        self.batch_checks = 0  # batch check rounds run (a bisect's prefixes included)
        self.autoresolved = []  # entry ids a batch's assembly auto-resolved a source conflict of (THE BATCH TIERS)
        self.escalations = None  # the escalation world (SimEscalations), made by the first `escalate` arrival
        self.launch_deaths = {}  # red key -> {"owner": the cause is the vault's, "said": IL was reported}

    # ---------------------------------------------------------------- the model's git
    def anc(self, sha):
        """The commits of sha's history, sha included (a frozenset, memoized)."""
        if sha in self._anc:
            return self._anc[sha]
        chain, x = [], sha
        while x is not None and x not in self._anc:
            chain.append(x)
            x = self.commits[x][0]
        acc = self._anc[x] if x is not None else frozenset()
        for c in reversed(chain):
            acc = acc | {c}
            self._anc[c] = acc
        return self._anc[sha]

    def patches_of(self, sha):
        return {self.commits[c][1] for c in self.anc(sha) if self.commits[c][1]}

    def child(self, parent, pid):
        sha = hashlib.sha1(f"{parent}:{pid}".encode()).hexdigest()
        self.commits.setdefault(sha, (parent, pid))
        return sha

    def chain(self, base, head):
        """head's commits not in base's history, oldest first."""
        out, x, stop = [], head, self.anc(base)
        while x is not None and x not in stop:
            out.append(x)
            x = self.commits[x][0]
        return list(reversed(out))

    def carrier(self, upstream, pid):
        return next((c for c in self.chain(ROOT, upstream) if self.commits[c][1] == pid), None)

    def resolve(self, path, ref):
        ref = ref.replace("^{commit}", "")
        if ref in ("HEAD",):
            return self.heads.get(os.path.realpath(path))
        if ref.endswith("origin/main") or ref == "main" or ref == "refs/heads/main":
            return self.main
        if ref.startswith("refs/heads/land/"):
            return None
        return ref if ref in self.commits else None

    def git(self, repo, *args, check=True, input=None):
        rc, out = self._git(os.path.realpath(repo), list(args))
        if check:
            if rc:
                raise RuntimeError(f"git {' '.join(args)} in {repo}: (sim) {out}")
            return out
        return subprocess.CompletedProcess(["git", *args], rc, out if rc == 0 else "", "" if rc == 0 else out)

    def git_ok(self, repo, *args):
        return self._git(os.path.realpath(repo), list(args))[0] == 0

    def _git(self, path, a):
        cmd, rest = a[0], a[1:]
        if cmd == "rev-parse":
            if "--git-common-dir" in rest:
                return 0, os.path.join(self.repo, ".git") + "\n"
            if "--show-toplevel" in rest:
                return 0, path + "\n"
            refs = [r for r in rest if not r.startswith("-")]
            sha = self.resolve(path, refs[-1]) if refs else None
            return (0, sha + "\n") if sha else (128, f"unknown revision {refs}")
        if cmd == "cat-file":
            ref = rest[-1]
            return (0, "") if self.resolve(path, ref) else (1, "missing")
        if cmd == "merge-base":
            if rest[0] == "--is-ancestor":
                x, y = self.resolve(path, rest[1]), self.resolve(path, rest[2])
                return (0, "") if x and y and x in self.anc(y) else (1, "")
            shas = [self.resolve(path, r) for r in rest if not r.startswith("-")]
            if not shas or None in shas:
                return 128, "usage: git merge-base ..."
            common = set.intersection(*(set(self.anc(s)) for s in shas))
            best = max(common, key=lambda c: len(self.anc(c)))
            return 0, best + "\n"
        if cmd in ("fetch", "clean", "prune"):
            return 0, ""
        if cmd == "worktree":
            if rest[0] == "add":
                self.heads[os.path.realpath(rest[-2])] = self.resolve(path, rest[-1])
            return 0, ""
        if cmd in ("checkout", "reset"):
            refs = [r for r in rest if not r.startswith("-")]
            if refs:
                self.heads[path] = self.resolve(path, refs[-1])
            return 0, ""
        if cmd == "rev-list":
            rng = next(r for r in rest if not r.startswith("-") and not r.isdigit())
            a_, _, b_ = rng.partition("..")
            out = self.chain(self.resolve(path, a_), self.resolve(path, b_)) if b_ else self.chain(ROOT, self.resolve(path, a_))
            if "--reverse" not in rest:
                out = list(reversed(out))
            return 0, "".join(c + "\n" for c in out)
        if cmd == "diff-tree":
            c = self.resolve(path, rest[-1])
            pid = self.commits.get(c, (None, None))[1]
            return 0, "".join(p + "\n" for p in (self.patches[pid].paths if pid else []))
        if cmd == "diff" and "--name-only" in rest:
            refs = [r for r in rest if not r.startswith("-")]
            if len(refs) == 2:
                return 0, "".join(p + "\n" for p in self.range_paths(path, refs[0], refs[1]))
            return 0, ""
        if cmd == "symbolic-ref":
            return 1, ""
        if cmd == "log" and "--" in rest:  # a bounce's cause (queue.py conflict_cause): the sim keeps no subjects
            return 0, ""
        if cmd == "log" and any(r.startswith("--format=%(trailers:") for r in rest):  # batch.py pure_move_claimed:
            return 0, ""  # the sim's patches carry no trailers
        raise Violation(f"(sim) git {' '.join(a)} is not modelled: model it in queue_sim.SimGit")

    def range_paths(self, cwd, a, b, deleted=False):
        if deleted:
            return []
        x, y = self.resolve(cwd, a), self.resolve(cwd, b)
        if not x or not y:
            return []
        return sorted({p for pid in self.patches_of(y) - self.patches_of(x) for p in self.patches[pid].paths})

    # ---------------------------------------------------------------- the fakes
    def apply_entry(self, st, d, wt, e, base, regen=True, autoresolve=False):
        wt = os.path.realpath(wt)
        head = self.heads[wt]
        on_base, on_head = self.patches_of(base), self.patches_of(head)
        new, gone, dup, regenerated, resolved = head, [], [], set(), []
        for c in e["commits"]:
            pid = self.commits[c][1]
            if pid in on_base:
                gone.append((c, self.carrier(base, pid)))
                continue
            if pid in on_head:
                dup.append((c, self.carrier(head, pid)))
                continue
            p = self.patches[pid]
            hit = (p.conflicts & self.patches_of(new)) - self.patches_of(self.commits[c][0])
            if hit:
                return "conflict", sorted({x for h in hit for x in self.patches[h].paths} & set(p.paths)) or p.paths
            res = (p.resolvable & self.patches_of(new)) - self.patches_of(self.commits[c][0])
            if res:  # the real pick's source resolution (waiting.py source_resolve): in a backlog batch's assembly only
                files = sorted({x for h in res for x in self.patches[h].paths} & set(p.paths)) or p.paths
                if not autoresolve or self.profile.get("before_tiers"):  # before THE BATCH TIERS: no resolution
                    return "conflict", files
                resolved += [{"commit": c, "file": f, "hunks": ["line 1: kept both sides' added lines"]} for f in files]
            gen = (p.gen_conflicts & self.patches_of(new)) - self.patches_of(self.commits[c][0])
            if gen:  # the real pick's resolution (waiting.py conflict_resolve), where the code has it
                files = sorted({x for h in gen for x in self.patches[h].paths} & set(p.paths) & set(GENERATED))
                known = getattr(A, "generated_entry", None)
                if not (regen and known and all(known(wt, f) for f in files)):
                    return "conflict", files
                if p.regen_fails:
                    return "conflict", [f"{', '.join(files)} (generated: a row both sides changed, and the regen "
                                        "command could not run)"]
                regenerated |= set(files)
            new = self.child(new, pid)
        if gone:
            dropped = {c for c, _v in gone}
            e["commits"] = [c for c in e["commits"] if c not in dropped]
            A.ChangeRecords(st, d).save(e)
            if not e["commits"]:
                return "landed", gone[-1][1]
        if dup and len(dup) == len(e["commits"]):
            return "duplicate", dup[-1][1]
        if regenerated:
            e["regenerated"] = sorted(regenerated)
        else:
            e.pop("regenerated", None)
        if resolved:
            e["autoresolved"] = resolved
            self.autoresolved.append(e["id"])
        else:
            e.pop("autoresolved", None)
        self.heads[wt] = new
        return "ok", None

    def check_tree(self, repo, i, sha):
        tree = f"{self.repo}-wt-check-{i}"
        os.makedirs(tree, exist_ok=True)
        self.heads[os.path.realpath(tree)] = sha
        return tree

    def cherry_landed(self, repo, main, head="HEAD", *limit):
        m, h = self.resolve(repo, main), self.resolve(repo, head)
        if not m or not h:
            return None, None
        commits = self.chain(m, h)
        on = self.patches_of(m)
        return commits, [c for c in commits if self.commits[c][1] not in on]

    def change_landed(self, repo, head, commits=None):
        if not head or head not in self.commits:
            return None, None
        if head in self.anc(self.main):
            return True, head
        ch = self.chain(self.main, head)
        on = self.patches_of(self.main)
        if all(self.commits[c][1] in on for c in ch):
            return True, self.carrier(self.main, self.commits[ch[-1]][1]) if ch else head
        return False, None

    def hook_query(self, hook, tree, flag, paths):
        if flag == "--plan":
            return ["sbt"] + (["identity"] if any(p.startswith("identity/") for p in paths) else []) \
                + (["scripttests"] if any(p.startswith("scripts/") for p in paths) else [])
        return {"--compile-plan": compile_plan, "--lint-plan": lint_plan, "--per-file-lints": per_file_lints,
                "--coupling-plan": ratchet_plan,
                "--quality-plan": ratchet_plan}.get(flag, lambda p: [])(paths)

    def commit_fixes(self, repo, sha, store):
        pid = self.commits.get(sha, (None, None))[1]
        return set(self.patches[pid].fixes) if pid else set()

    def pair_conflict(self, repo, older, younger):
        """The files the two queued changes conflict in when assembled together (older first), or None: the model's
        mutual conflicts."""
        a, b = older.get("head"), younger.get("head")
        if a not in self.commits or b not in self.commits:
            return None
        common = max(self.anc(a) & self.anc(b), key=lambda c: len(self.anc(c)))  # git merge-tree: their merge base
        mine, theirs = self.patches_of(b) - self.patches_of(common), self.patches_of(a) - self.patches_of(common)
        both = sorted({x for p in mine for x in self.patches[p].paths} & {x for p in theirs for x in self.patches[p].paths})
        if any(self.patches[p].conflicts & theirs for p in mine):
            return both or ["?"]
        if any(self.patches[p].gen_conflicts & theirs for p in mine):  # git merge-tree names the generated files
            return [x for x in both if x in GENERATED] or ["?"]
        if any(self.patches[p].resolvable & theirs for p in mine):
            return both or ["?"]
        return None

    def own_commits_conflict(self, repo, onto, commits):
        """conflicts.py own_commits_conflict in the model: the change's patches main lacks against main's since their
        merge base -- a mutual source conflict names its files; a generated file's is the rebase's to regenerate."""
        b = commits[-1] if commits else None
        if b not in self.commits or onto not in self.commits:
            return None
        common = max(self.anc(onto) & self.anc(b), key=lambda c: len(self.anc(c)))
        mine, theirs = self.patches_of(b) - self.patches_of(onto), self.patches_of(onto) - self.patches_of(common)
        both = sorted({x for p in mine for x in self.patches[p].paths} & {x for p in theirs for x in self.patches[p].paths})
        if any(self.patches[p].conflicts & theirs for p in mine):
            return [x for x in both if x not in GENERATED] or ["?"]
        return None

    def pair_resolvable(self, repo, older, younger, files):
        """The conflicting files a backlog batch's assembly resolves: all of them when the two conflict only in source
        of additions on both sides (the model's `resolvable`), else none."""
        a, b = older.get("head"), younger.get("head")
        common = max(self.anc(a) & self.anc(b), key=lambda c: len(self.anc(c)))
        mine, theirs = self.patches_of(b) - self.patches_of(common), self.patches_of(a) - self.patches_of(common)
        if any(self.patches[p].conflicts & theirs for p in mine) or self.profile.get("before_tiers"):
            return []
        return list(files) if any(self.patches[p].resolvable & theirs for p in mine) else []

    def change_of_sha(self, sha):
        pid = self.commits.get(sha, (None, None))[1]
        return next((c for c in self.changes if pid in c.patches), None) if pid else None

    def saw_recheck(self, base, moved, tip, why):
        """INVARIANT 9: no FULL round for a file overlap with a change that was open when this one was queued (the two
        are batched, or the younger waits for the older and is checked once on top of it)."""
        if not why or not why.startswith("both touch") or moved != self.main:
            return
        me = self.change_of_sha(tip)
        if not me or me.kind == "redfix":  # a red fix never waits for an older change
            return
        mine = {x for p in me.patches for x in self.patches[p].paths}
        for c in self.chain(base, moved):
            other = self.change_of_sha(c)
            # A WAITING CHANGE HOLDS NOTHING IT ASSEMBLES WITH (queue.py, 2026-11-20): the younger is checked at once and
            # meets it in the batch at green -- or, green first, is rechecked once that batch lands it: accepted, for a
            # change waiting when the younger arrived or when its check began (waiting_at_arrival)
            if other and other.cid in self.open_at_arrival.get(me.cid, ()) \
                    and other.cid not in self.waiting_at_arrival.get(me.cid, ()) \
                    and mine & {x for p in other.patches for x in self.patches[p].paths}:
                self.violate("I9", f"change {me.eid} rechecked in full ({why}) for {other.eid}, open when it was queued")
                return

    def verdict(self, tip, where):
        """(ok, output) of a check of tip: a breaking patch fails it with its own named test; an infra failure is
        drawn now and then."""
        if not self.calm and self.rng.random() < self.profile.get("infra", 0.03):
            return False, "[error] failed to connect to server\n"
        on = self.patches_of(tip)
        bad = sorted(pid for pid in on - self.patches_of(ROOT) if self.patches[pid].bad
                     or (self.patches[pid].resolve_bad and self.patches[pid].resolvable & on))
        if bad:
            lines = [f"[info] - sim {letters(self.patches[p].cid)} works *** FAILED ***" for p in bad]
            return False, "\n".join(lines + ["! pre-push sbt FAILED (3s)"]) + "\n"
        if self.reds and any(self.patches[p].main_fails for p in on - self.patches_of(ROOT)):
            self.verified.add(frozenset(on))  # its failure is main's own: judged known, the checks' verdict stands
            return False, MAIN_FAILS_LINE + "\n! pre-push sbt FAILED (3s)\n"  # the red's own failing test, nothing else
        self.verified.add(frozenset(self.patches_of(tip)))
        return True, f"checks passed {where}\n"

    def base_failure_keys(self, st, repo, d, e, base, now):
        """What fails on the change's base: the open gate red's own test, in the gate's newest run (queuefailure)."""
        return {k: f"Main #{self.red_run}{A.GATE_RUN_WHERE}" for k in A.test_keys([MAIN_FAILS_LINE])} if self.reds else {}

    def run_entry_checks(self, repo, d, e, tree, base, head, mode, cls, log_path, clock, st, own=None):
        open_ = set(self.reds) | set(self.outside)
        red_fix = lambda x: bool({str(f) for f in x.get("fixes") or []} & open_)  # the simulation's truth
        if red_fix(e) and not tree.endswith("-wt-check-redfix"):
            holder = A.lease_live(f"slot/check/{os.path.basename(self.d.rstrip('/'))}/{A.RED_FIX_TREE}", self.store)
            other = next((t.name for t in self.tasks if holder and not t.done and str(t.pid) in holder["holder"]), None)
            # the holder a red fix when its own check began counts: its red may have closed since (a second red's
            # fix arriving while the first's fix still checks takes another tree at once -- the right call)
            if not (other and other.startswith("check-") and (red_fix(self.entry_by_id(other[6:]) or {})
                                                               or self.red_fix_checks.get(other[6:]))):
                self.violate("I3", f"red fix {e['id']} checked in {os.path.basename(tree)}, not the red-fix tree (the wrong "
                             f"lane; that tree held by {other or (holder or {}).get('holder')})")
        self.red_fix_checks[e["id"]] = red_fix(e)
        self.checks_of[e["id"]] = self.checks_of.get(e["id"], 0) + 1
        self.local.task.block(self.rng.randint(*self.profile.get("verify_ticks", (1, 2))) if mode == A.VERIFY_MODE
                              else self.rng.randint(*self.profile.get("check_ticks", (2, 4))))
        ok, out = self.verdict(head, f"{mode} {head[:8]}")
        return ok, out, "in the simulation"

    def batch_check(self, repo, d, tree, base, tip, clock, st, cls="check", depth="fast", runs=None, range_base=None):
        self.batch_checks += 1
        self.local.task.block(self.rng.randint(*self.profile.get("verify_ticks", (1, 2))) if depth == "verify"
                              else self.rng.randint(*self.profile.get("batch_ticks", (2, 5))))
        ok, out = self.verdict(tip, f"batch {depth} {tip[:8]}")
        return ok, out, False

    def run_watched(self, argv, cwd, env, log_path, step, **kw):
        if step != "push":
            raise Violation(f"(sim) run_watched step {step} is not modelled")
        sha = argv[3].split(":", 1)[0]
        commits = self.chain(self.main, sha)
        if self.reds and A.must_wait(self.repo, commits, self.reds, self.store):
            return 1, "pre-push: main is red: only a fix may land\n"
        if self.main not in self.anc(sha):
            return 1, " ! [rejected] main -> main (non-fast-forward)\n"
        self.forge_push(sha, "queue")
        return 0, "pushed\n"

    def check_red_landing(self, commits):
        """R. THE RED-LANDING RULE, by the simulation's own truth (never the rule under test, redland.py, which the
        simulated hook applies): while the gate is red, every production commit the queue LANDS names a run of the open
        red -- the gate's or its item's. (A push the hook refuses -- the gate turned red after the queue decided -- lands
        nothing and is no violation.)"""
        known = set(self.reds) | self.red_item_runs | self.episode_runs
        for c in commits if self.reds else ():
            p = self.patches.get(self.commits.get(c, (None, None))[1])
            if p and any(sim_production(x) for x in p.paths) and not p.fixes & known:
                self.violate("IR", f"patch {p.pid} of change {p.cid} pushed while the gate is red {sorted(self.reds)}: "
                             f"a production commit naming {sorted(p.fixes) or 'no red'}")

    def forge_push(self, sha, who):
        new = [self.commits[c][1] for c in self.chain(self.main, sha)]
        for pid in new if who == "queue" else ():  # NEVER A DECIDED CHANGE PUSHED (superseded, cancelled, rejected)
            ch = next((c for c in self.changes if pid in c.patches), None)
            status = (self.entry(ch) or {}).get("status") if ch else None
            if status in ("superseded", "cancelled", "rejected"):
                self.violate("I2", f"change {ch.eid}, {status}, pushed to main (patch {pid})")
        for pid in new:
            self.pushed_patch_count[pid] = self.pushed_patch_count.get(pid, 0) + 1
            if self.pushed_patch_count[pid] > 1:
                self.violate("I2", f"patch {pid} of change {self.patches[pid].cid} pushed to main twice")
        if who == "queue":
            self.check_red_landing(self.chain(self.main, sha))
        if who == "queue" and not self.tree_verified(sha, new):  # INVARIANT 11
            self.violate("I11", f"an unverified combined tree reached main: {sha[:10]} (patches {sorted(new)}) passed no check")
        self.pushes.append((self.tick, sha, new, who))
        self.main = sha
        if self.reds and any(self.patches[p].fixes & self.reds for p in new):
            self.red_clears_at = self.tick + self.rng.randint(1, 3)  # the fix's Main run goes green
        for p in new:  # a red outside the gate: its own workflow's next run verifies the fix a few ticks later
            for name in self.patches[p].fixes & set(self.outside):
                self.outside_clears.setdefault(name, self.tick + self.outside_rng.randint(1, 3))

    def tree_verified(self, sha, new):
        """INVARIANT 11: the pushed tree passed a check -- or (MINIMAL RECHECKS) a check passed a tree holding every
        patch it pushes, and what else the pushed tree holds shares no file and no read check with them (the
        simulation's own read model, interact)."""
        tree = frozenset(self.patches_of(sha))
        if tree in self.verified:
            return True
        paths = lambda pids: [x for p in pids for x in self.patches[p].paths]
        mine = paths(new)
        for v in self.verified:
            if set(new) <= v <= tree:
                extra = paths(tree - v)
                if not (set(mine) & set(extra) - set(GENERATED)) and not interact(mine, extra):
                    self.kept_pushes += 1
                    return True
        return False

    def spawn_executor(self, repo, d, eid):
        e = self.entry_by_id(eid) or {}
        k = (A.process_store().item("change", eid) or {}).get("data", {}).get("change_kind") or {}
        self.spawned.append((self.tick, eid, k.get("rank")))
        me = next((c for c in self.changes if c.eid == eid), None)
        if me:  # who was `waiting` for green when its check began (A WAITING CHANGE HOLDS NOTHING, invariant 9)
            self.waiting_at_arrival.setdefault(me.cid, set()).update(
                c.cid for c in self.changes if (self.entry(c) or {}).get("status") == "waiting")
        if e.get("status") not in ("queued", "checking"):
            self.violate("IX", f"an executor started for {eid}, which is {e.get('status')}")
        self._spawn(f"check-{eid}", lambda: A.check_entry(repo, eid, *on_clock(self.sim_clock)))

    def spawn_batch_executor(self, repo, d, bid):
        self._spawn(f"batch-{bid}", lambda: A.check_batch(repo, bid, *on_clock(self.sim_clock)))

    def _spawn(self, name, fn):
        t = Task(self, name, fn)
        self.tasks.append(t)
        t.thread.start()
        t.step()

    def start_watch(self, st, repo, sha, pushed_at, **kw):
        return None

    def violate(self, kind, what):
        """A violation of invariant `kind` (tests/sim-invariants.tsv names each; tests/sim_gate.py counts them)."""
        assert kind in INVARIANT_KINDS, kind
        self.kinds_seen[kind] = self.kinds_seen.get(kind, 0) + 1
        self.violations.append(f"seed {self.seed} tick {self.tick}: [{kind}] {what}")

    # ---------------------------------------------------------------- set up / tear down
    FAKES = ("git", "git_ok", "fetch_main", "apply_entry", "check_tree", "range_paths", "named_reader", "cherry_landed",
             "change_landed", "hook_query", "commit_fixes", "gate_reds", "gov_class_of", "blame_land_refusal",
             "fix_replay_refusal", "run_entry_checks", "base_failure_keys", "batch_check", "batch_drift_check", "run_watched", "start_watch", "mark_gate_dirty",
             "spawn_executor", "spawn_batch_executor", "precheck_hook", "verify_areas_supported", "remote_matches",
             "remote_checks_supported", "slug", "cancel_entry_remote", "stats_post", "check_runs_of", "gate_runs_pass",
             "clone_shared", "spawn_waiting_processor", "offer_token", "tool_env", "stats_echo", "pair_conflict", "pair_resolvable",
             "own_commits_conflict", "recheck_reason", "waiting_entries", "process_start", "code_version", "dt", "own_diff_changed")

    def install(self):
        import contextlib
        self.saved = {n: getattr(A, n, None) for n in self.FAKES + ("os",)}
        self.env_saved = dict(os.environ)
        self.tmp = tempfile.mkdtemp(prefix="agentctl-sim-")
        self.repo = os.path.realpath(os.path.join(self.tmp, "movies"))
        os.makedirs(os.path.join(self.repo, "scripts", "hooks"))
        self.hook = os.path.join(self.repo, "scripts", "hooks", "pre-push")
        with open(self.hook, "w") as f:
            f.write(HOOK_TEXT)
        os.chmod(self.hook, 0o755)
        self.heads[self.repo] = ROOT
        repos = os.path.join(self.tmp, "red-repos.json")
        with open(repos, "w") as f:
            json.dump({"repos": [{"repo": "sim/movies", "gate": [["Main", "main.yml"]], "push": "land",
                                  # order-sensitive, as movies declares them (generated.py ONLY A VOIDED CHECK HOLDS)
                                  "generated": [{"paths": list(BASELINES), "regen": "true", "order_sensitive": True}] + (
                                      [] if self.profile.get("before_tiers") else
                                      [{"paths": list(SNAPSHOTS), "regen": "true", "merge": "regen",
                                        "order_sensitive": True}])}]}, f)
        os.environ.update(AGENTCTL_RED_REPOS=repos)
        os.environ.update(AGENTCTL_STATE_ROOT=os.path.join(self.tmp, "state"), AGENTCTL_ROLE="server",
                          AGENTCTL_CHECK_TREES=str(self.trees),
                          AGENTCTL_CHECK_LEASE_TTL=str(self.profile.get("lease_ttl", 600)),
                          AGENTCTL_STATS_URL="http://sim", AGENTCTL_STATS_SETTLE="0",
                          AGENTCTL_INBOX_DIR=os.path.join(self.tmp, "inbox"),
                          AGENTCTL_CHECKS_DIR=os.path.join(self.tmp, "checks"))
        self.saved_wdir = A.WAITING_DIR
        A.WAITING_DIR = os.path.join(self.tmp, "waiting")
        for c in A.PROCESS_CACHES:
            c.clear()
        clock = self.clock = [1_800_000_000.0]
        self.sim_clock = SimClock(clock)
        # the store's clock is the simulation's, as cli hands the process's store its clock (THE CLOCK IS PASSED IN:
        # the ticks and the checkers get self.sim_clock, and with it this store)
        self.store = on_clock(self.sim_clock)[1]
        A.dt = sim_datetime(clock)  # entry ids and stamps from the simulated clock: a seed replays exactly
        A.os = SimOs(self)
        A.git, A.git_ok = self.git, self.git_ok
        A.fetch_main = lambda repo: (True, "")
        A.apply_entry = self.apply_entry
        A.check_tree = self.check_tree
        A.range_paths = self.range_paths
        A.named_reader = lambda *a, **k: None
        A.own_diff_changed = lambda *a, **k: False  # a simulated patch is the same patch on any base
        A.cherry_landed = self.cherry_landed
        A.change_landed = self.change_landed
        A.hook_query = self.hook_query
        A.commit_fixes = self.commit_fixes
        A.gate_reds = lambda repo: set(self.reds)
        A.gov_class_of = lambda repo, commits, reds: "check"
        A.blame_land_refusal = lambda *a, **k: None
        A.fix_replay_refusal = lambda *a, **k: None  # the sim's reds register no replay (red-workflows.tsv)
        A.run_entry_checks = self.run_entry_checks
        A.base_failure_keys = self.base_failure_keys
        A.batch_check = self.batch_check
        A.batch_drift_check = lambda *a, **k: (None, "")
        A.run_watched = self.run_watched
        A.start_watch = self.start_watch
        A.mark_gate_dirty = lambda: None
        A.spawn_executor = self.spawn_executor
        A.spawn_batch_executor = self.spawn_batch_executor
        A.precheck_hook = lambda repo: self.hook
        A.verify_areas_supported = lambda tree: False
        A.remote_matches = lambda *a, **k: False
        A.remote_checks_supported = lambda tree: False
        A.slug = lambda repo: "sim/movies"
        A.cancel_entry_remote = lambda *a, **k: None
        A.stats_post = lambda url, recs: self.stats.extend(recs)
        A.check_runs_of = lambda url, cid, around: []
        A.gate_runs_pass = lambda *a, **k: 0
        A.clone_shared = lambda repo: contextlib.nullcontext()
        A.spawn_waiting_processor = lambda repo: None
        A.offer_token = lambda repo: None
        A.tool_env = lambda: dict(os.environ)
        A.pair_conflict = self.pair_conflict
        A.pair_resolvable = self.pair_resolvable
        A.own_commits_conflict = self.own_commits_conflict
        A.process_start = lambda pid: ""  # Owned's `ps` per record: no process of the simulation is one
        A.code_version = lambda path=None: "sim"
        parsed = {}

        def waiting_entries(d, open_only=True):
            """waiting.py's, each entry file parsed once per version on disk (its mtime and size): most of a seed's time
            was these reads."""
            out = []
            with os.scandir(os.path.join(d, "entries")) as it:
                for de in it:
                    if not de.name.endswith(".json"):
                        continue
                    stv = de.stat()
                    key = (stv.st_mtime_ns, stv.st_size, stv.st_ino)
                    hit = parsed.get(de.path)
                    if not hit or hit[0] != key:
                        try:
                            with open(de.path) as f:
                                hit = parsed[de.path] = (key, f.read())
                        except OSError:
                            continue
                    try:
                        e = json.loads(hit[1])
                    except ValueError:
                        continue
                    if not open_only or e.get("status") == "waiting":
                        out.append(e)
            return sorted(out, key=lambda e: e["seq"])
        A.waiting_entries = waiting_entries
        real_recheck = self.saved["recheck_reason"]

        def recheck_reason(tree, base, moved, tip, change_paths, full_tip=None):
            n, why = real_recheck(tree, base, moved, tip, change_paths, full_tip)
            self.saw_recheck(base, moved, tip, why)
            return n, why
        A.recheck_reason = recheck_reason
        A.stats_echo = lambda recs, out=None: None
        self.d = A.waiting_state(self.repo)[0]
        self.saved_db = A.Store.db
        conns = {}
        A.Store.db = lambda st: conns.setdefault(st.path, None) or conns.__setitem__(st.path, KeptConnection(st.path)) \
            or conns[st.path]
        self.conns = conns

    def uninstall(self):
        if self.escalations:
            self.escalations.close()
        for t in self.tasks:  # an executor still blocked: unwound, its leases released
            if not t.done:
                t.abort = True
                t.go.set()
                t.thread.join(10)
        for n, v in self.saved.items():
            if v is None:  # a seam the code under test does not have (a mutant copy from before it)
                A.__dict__.pop(n, None)
            else:
                setattr(A, n, v)
        A.Store.db = self.saved_db
        for k in self.conns.values():
            k.c.close()
        A.WAITING_DIR = self.saved_wdir
        os.environ.clear()
        os.environ.update(self.env_saved)
        for c in A.PROCESS_CACHES:
            c.clear()
        if self.keep:  # $AGENTCTL_TEST_SIM_KEEP: a directory the seed's processor log is copied into
            os.makedirs(self.keep, exist_ok=True)
            dst = os.path.join(self.keep, f"seed-{self.seed}.log")
            shutil.copy(os.path.join(self.d, "processor.log"), dst)
            print(f"(sim) seed {self.seed}: its processor log is {dst}")
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---------------------------------------------------------------- the agents
    def _plan_arrivals(self):
        if self.script is not None:
            return list(self.script)
        out, t = [], 0
        for _ in range(self.n_changes):
            t += self.rng.choice(self.profile.get("gaps", (0, 0, 1, 1, 2, 3, 5, 8)))
            r = self.rng.random()
            ident = self.profile.get("identity", 0.25)
            kind = "identity" if r < ident else "nobatch" if r < ident + self.profile.get("nobatch", 0.1) else "plain"
            out.append((t, {"kind": kind}))
        return out

    def make_change(self, spec, worktree=None):
        """A new version of an agent's change on the current main: its patches, commits, and queue fields."""
        self.next_cid += 1
        cid = self.next_cid
        kind = spec.get("kind", "plain")
        worktree = worktree or os.path.join(self.tmp, f"movies-wt-{spec.get('worktree') or 'a' + letters(cid)}")
        mods = spec.get("modules") or [self.rng.choice(("web", "worker", "common", "scrapers", "docs", "scripts", ".github"))]
        if kind == "identity" and "identity" not in mods:
            mods = ["identity"] + mods
        paths = []
        for m in mods:
            m = {"(root)": "rootfiles", "project": "projectfiles"}.get(m, m)
            if self.rng.random() < self.profile.get("shared_path", 0.15):
                name = "Shared"  # the same file as other changes: main moving under it forces a full round
            else:
                name = f"F{letters(cid)}"
            paths.append(f"{m}/src/main/{name}.scala" if m in SBT_MODULES else f"{m}/{name}.txt")
        fixes = {str(self.red_run)} if kind == "redfix" and self.reds else \
            {self.red_older} if kind == "redfix-older" and self.reds else \
            {f"{self.red_run}1"} if kind == "redfix-other" and self.reds else \
            {max(self.outside, key=int)} if kind == "outside-fix" and self.outside else set()
        bad = spec.get("bad")
        if bad is None:
            bad = not self.calm and self.rng.random() < self.profile.get("bad", 0.12)
        if spec.get("paths"):
            paths = list(spec["paths"])
        conflicts = set()
        # A TEXTUAL CONFLICT needs a file both change, and is mutual: whichever lands second no longer applies. A change
        # marked to conflict conflicts with the youngest open change sharing a module (a file of it added to its own
        # paths), else ("*future*") with the next change to arrive in one of its modules -- or with an outside push.
        opened = [c for c in self.changes if not A.entry_final((self.entry(c) or {}).get("status", "queued"))
                  and c.worktree != worktree]  # its own older version is no collision
        self.open_at_arrival[cid] = {c.cid for c in opened}
        self.waiting_at_arrival[cid] = {c.cid for c in opened if (self.entry(c) or {}).get("status") == "waiting"}
        mods_of = lambda ps: {x.split("/")[0] for x in ps}
        if spec.get("conflicts_with"):
            target = next(c for c in reversed(opened + self.changes) if c.label == spec["conflicts_with"])
        elif spec.get("conflict") or (not self.calm and spec.get("conflict") is None
                                      and self.rng.random() < self.profile.get("conflict", 0.1)):
            target = next((c for c in reversed(opened) if mods_of(self.patches[c.patches[0]].paths) & mods_of(paths)), None)
            conflicts = set() if target else {"*future*"}
        else:
            target = None
        waiting_for_me = [] if target else \
            [c for c in opened if "*future*" in self.patches[c.patches[0]].conflicts
             and mods_of(self.patches[c.patches[0]].paths) & mods_of(paths)][:1]
        for c in ([target] if target else waiting_for_me):
            theirs = self.patches[c.patches[0]]
            if theirs.paths[0] not in paths:
                paths.append(theirs.paths[0])
        base = self.main
        pids, head = [], base
        for k in range(2 if spec.get("two_commits") else 1 if spec.get("one_commit") else self.rng.choice((1, 1, 2))):
            pid = f"p{cid}{letters(k)}"
            self.patches[pid] = Patch(pid, cid, paths if k == 0 else [paths[0]], fixes if k == 0 else (),
                                      bad=bad and k == 0, conflicts=conflicts)
            self.patches[pid].main_fails = bool(spec.get("main_fails")) and k == 0
            pids.append(pid)
            head = self.child(head, pid)
        for c in ([target] if target else waiting_for_me):
            for q in c.patches:
                self.patches[q].conflicts = (self.patches[q].conflicts - {"*future*"}) | set(pids)
            for q in pids:
                self.patches[q].conflicts |= set(c.patches)
        for label in spec.get("gen_conflicts_with") or ():  # adjacent rows of a generated file: a textual conflict there
            other = next(c for c in reversed(self.changes) if c.label == label)
            for q in other.patches:
                self.patches[q].gen_conflicts |= set(pids)
            for q in pids:
                self.patches[q].gen_conflicts |= set(other.patches)
        for q in pids:
            self.patches[q].regen_fails = bool(spec.get("regen_fails"))
        for label in spec.get("resolvable_with") or ():  # a source conflict of additions on both sides (THE BATCH TIERS)
            other = next(c for c in reversed(self.changes) if c.label == label)
            for q in other.patches:
                self.patches[q].resolvable |= set(pids)
            for q in pids:
                self.patches[q].resolvable |= set(other.patches)
                self.patches[q].resolve_bad = bool(spec.get("resolve_bad"))
        ch = Change(cid, worktree, kind, pids, base, head, self.chain(base, head), self.tick,
                    no_batch=kind == "nobatch")
        ch.outcome = spec.get("outcome")
        ch.label = spec.get("label") or spec.get("worktree")
        self.changes.append(ch)
        return ch

    def submit(self, ch):
        fields = {"worktree": ch.worktree, "branch": os.path.basename(ch.worktree), "head": ch.head,
                  "commits": list(ch.commits), "fixes": sorted(set().union(*(self.patches[p].fixes for p in ch.patches))),
                  "keep_worktree": False, "no_batch": ch.no_batch, "claude_pid": None, "session": f"s-{ch.worktree}",
                  "base": ch.base, "host": "sim", "lander": {}, "commit_keys": {}}
        e, _ahead = A.queue_change(self.store, self.d, fields)
        ch.eid = e["id"]
        self.by_eid[e["id"]] = ch

    def agents(self):
        """This tick's arrivals and agent reactions: re-lands after NOT LANDED, supersedes, cancels."""
        while self.arrivals and self.arrivals[0][0] <= self.tick:
            _t, spec = self.arrivals.pop(0)
            if spec.get("kind") == "open-outside-red":
                self.open_outside_red()
                continue
            if spec.get("kind") == "push":  # a push to main from outside the queue (a scripted one)
                self.next_cid += 1
                pid = f"o{self.next_cid}"
                self.patches[pid] = Patch(pid, -self.next_cid, list(spec["paths"]))
                self.forge_push(self.child(self.main, pid), "outside")
                continue
            if spec.get("kind") == "open-red":  # a gate red with its item in the store (and an older run of it)
                self.open_red(item=True, older_known=spec.get("older_known", True))
                continue
            if spec.get("kind") == "stale-episode":
                self.stale_episode()
                continue
            if spec.get("kind") == "two-holders":
                self.two_holders(spec["order"])
                continue
            if spec.get("kind") in ("escalate", "trust"):
                self.escalation_arrives(spec)
                continue
            if spec.get("kind") == "launch-deaths":
                self.launch_deaths_arrive(spec)
                continue
            if spec.get("kind") == "learn-run":
                self.name_older_run()
                continue
            if spec.get("kind") in ("redfix-older", "redfix-other") and not self.reds:
                self.open_red(item=True)
            if spec.get("kind") == "redfix" and not self.reds:
                self.open_red()
            if spec.get("kind") == "outside-fix" and not self.outside:
                self.open_outside_red()
            self.submit(self.make_change(spec))
        if self.calm:
            return
        for ch in list(self.changes):
            e = self.entry(ch)
            if not e or ch.cid in self.rejected_handled:
                continue
            st = e.get("status")
            if st == "rejected":
                self.rejected_handled.add(ch.cid)
                if self.rng.random() < 0.6 and self.tick < self.drain_from:  # the agent fixes / rebases and lands again
                    self.submit(self.make_change({"kind": ch.kind, "bad": False, "conflict": False,
                                                  "modules": sorted({p.split("/")[0] for p in self.patches[ch.patches[0]].paths})},
                                                 worktree=ch.worktree))
            elif st in ("queued", "checking", "checked") and self.tick < self.drain_from:
                if getattr(ch, "outcome", None) == "superseded" and self.tick >= ch.arrived + 1:
                    ch.outcome = None
                    self.submit(self.make_change({"kind": ch.kind, "bad": False, "conflict": False}, worktree=ch.worktree))
                elif getattr(ch, "outcome", None) in ("cancelled", "precheck") and self.tick >= ch.arrived + 1:
                    self.cancel(ch)
                elif ch.outcome is None and self.script is None and self.rng.random() < 0.01:
                    self.submit(self.make_change({"kind": ch.kind}, worktree=ch.worktree))  # amended and landed again
                elif ch.outcome is None and self.script is None and self.rng.random() < 0.005:
                    self.cancel(ch)

    def cancel(self, ch):
        ch.outcome, ch.cancelled = None, True
        import argparse, contextlib, io
        with contextlib.redirect_stdout(io.StringIO()):
            A.cmd_waiting(argparse.Namespace(wcmd="cancel", repo=self.repo, entry=ch.eid, renamed=True, hold_for=None,
                                             hold_why=None), *on_clock(self.sim_clock))  # a cancel never waits

    def entry_by_id(self, eid):
        return A.read_record(os.path.join(self.d, "entries", f"{eid}.json"))

    def entry(self, ch):
        return A.read_record(os.path.join(self.d, "entries", f"{ch.eid}.json")) if ch.eid else None

    # ---------------------------------------------------------------- the forge
    def open_red(self, item=False, older_known=True):
        """item: also the red's item in the store, naming the gate's newest runs and (older_known) an older run of the
        same red; one not known yet is named by `learn-run` later."""
        self.red_run += 1
        self.reds = {str(self.red_run), f"9{self.red_run}000"}
        self.red_since = self.tick
        self.red_clears_at = None
        self.red_older = f"{self.red_run}0"
        if item:
            self.red_item = f"sim-gate-red-{self.red_run}"
            self.red_item_runs = set(self.reds) | ({self.red_older} if older_known else set())
            A.process_store().put_item("red", self.red_item, "leased", {
                "key": self.red_item, "workflow": "Main", "gate": True, "red_kind": "test",
                "runs": sorted(self.red_item_runs), "last_run": {"number": self.red_run}})

    def two_holders(self, order):
        """TWO HOLDERS AT ONCE (2026-11-26, tests #289 / #292): the laptop and ci-runner run `land hold --for` while no
        hold is in force, interleaved at the store's seams as ORDER says -- `apart` (one after the other), `extend-first`
        (the other extends between the maker's making and its expiry move), `move-between` (both read the hold, the
        maker moves, then the other: its read is stale), `expire-between` (the other finds the maker's hold, which expires
        before its extension is written: review 2026-11-26). IH: exactly one hold in force and neither holder raises;
        both return it and both moves -- the maker's expiry, the other's extension -- are said (two hold.held events);
        after `expire-between`, the one in force is the other's, made anew."""
        st, got, errors = self.store, {}, {}
        h1_read, h0_moved = threading.Event(), threading.Event()

        def hold(name, secs, store):
            try:
                got[name] = A.land_hold_set(store, secs, f"sim {name}", name)
            except Exception as e:  # a holder's failure is the violation, not the run's end
                errors[name] = f"{type(e).__name__}: {e}"

        def the_other():
            hold("h1", 120, SeamStore(st, before_move=lambda: (h1_read.set(), h0_moved.wait(10))))
        h1 = threading.Thread(target=the_other, name="sim-holder-h1", daemon=True)
        after_open = {"apart": None, "extend-first": lambda: hold("h1", 120, st),
                      "move-between": lambda: (h1.start(), h1_read.wait(10)), "expire-between": None}[order]
        was = A._PROCESS_CLOCK[0]
        A.wire_clock(self.sim_clock)  # the holders' "now" is the simulation's, as the store's is
        try:
            hold("h0", 60, SeamStore(st, after_open=after_open))
            h0_moved.set()
            if order == "apart":
                hold("h1", 120, st)
            elif order == "expire-between":
                hold("h1", 120, SeamStore(st, after_open=self._expire_once(st)))
            elif order == "move-between":
                h1.join(10)
        finally:
            A.wire_clock(was)
        held = [h["id"] for h in A.land_holds(st)]
        said = [e["kind"] for i in held for e in st.item_events("hold", i) if e["from_state"] is not None]
        if order == "expire-between":
            bad = errors or len(held) != 1 or (got.get("h1") or {}).get("id") != held[0]
        else:
            bad = errors or len(held) != 1 or {h["id"] for h in got.values()} != set(held) or said.count("hold.held") != 2
        if bad:
            self.violate("IH", f"two holders ({order}): holds in force {held}, returned "
                         f"{ {k: v['id'] for k, v in got.items()} }, raised {errors}, moves said {said}")

    @staticmethod
    def _expire_once(st):
        """The first open's hold found is past its deadline and the other holder's tick expires it (the second open
        finds none: once)."""
        done = []

        def expire():
            if not done:
                done.append(1)
                st.db().execute("UPDATE items SET deadline=0 WHERE type='hold' AND state='held'")  # autocommit
                A.land_hold(st)
        return expire

    def stale_episode(self):
        """The open red's EPISODE holds its older run only through the CI snapshot: the gate's streak lists it, and its
        cached signature -- written by an older classifier -- names the red's job and line (redepisode.py's fallback
        order counts a stale record). The red's item names its newest runs only."""
        st = A.process_store()
        it = st.item("red", self.red_item)
        st.update_item("red", self.red_item, it["version"], {"job": "Unit tests", "line": "Failed: sim"})
        runs = [{"number": int(n), "databaseId": 50000 + int(n), "status": "completed", "conclusion": "failure",
                 "headBranch": "main"} for n in (self.red_run, self.red_older)]
        for r, version in zip(runs, (A.classifier_version(), "older-classifier")):
            A.write_json(A.reds_dir("runsig", f"{r['databaseId']}.json"), {
                "classifier": version, "sigs": [{"job": "Unit tests", "kind": "test", "line": "Failed: sim"}]})
        self.episode_runs = {self.red_older, str(runs[1]["databaseId"])}
        self.episode_snap = {"repo": A.SHORT["movies"], "gate": [{"name": "Main", "decided": runs[0], "runs": runs}]}
        self.refresh_episode_snap()

    def refresh_episode_snap(self):
        if self.episode_snap and self.reds:
            A.state_update(ci={**self.episode_snap, "at": A.epoch_now()})

    def escalation_arrives(self, spec):
        """An `escalate` arrival opens a condition the escalation dispatcher sees (its agent's behaviour scripted);
        a `trust` one sets whether claude trusts the repo."""
        if self.escalations is None:
            self.escalations = SimEscalations(self)
        if spec["kind"] == "trust":
            self.escalations.gap = None if spec.get("trusted") else "claude does not trust ~/bin (sim)"
        else:
            self.escalations.open_condition(spec)

    def launch_deaths_arrive(self, spec):
        """A red whose fixers die at launch, one `cause` per launch (a list: one each): the real red_lease_ended reads each
        death off the simulation's store and clock, as the red tick does for a dead wrapper's lease."""
        st, key = A.process_store(), spec["key"]
        causes = spec["causes"]
        it = st.put_item("red", key, "leased", {"key": key, "workflow": "Sim workflow", "job": "j", "kind": "test", "line": "x",
                                                "gate": False, "red_kind": "outside", "runs": ["1"], "attempts": 0,
                                                "history": [], "tier": "fresh", "last_run": {"number": 1, "databaseId": 1}})
        for n, cause in enumerate(causes):
            took = st.acquire(A.red_lease(key), f"laptop:fixer:red-{key}-{n}", A.RED_FIXER_TTL, A.RED_FIXER_MAX,
                              meta={"kind": "fixer", "worktree": f"/w/{key}-{n}"})
            st.release(A.red_lease(key), took["epoch"], took["secret"], outcome="ended with no LANDED / GAVE UP (session s: blocked)",
                       notes=cause)
            it = st.item("red", it["id"])
            if it["state"] != "leased":
                it = A.red_move(st, it, "leased", "taken again")
            A.red_lease_ended(st, it, st.get(A.red_lease(key)), None, self.clock[0])
        self.launch_deaths[key] = {"same": len(set(causes)) == 1, "said": False}

    def check_launch_deaths(self):
        """L. IL: the launches of a red all died at launch of one cause: the red waits blocked (on the vault's lease for an
        owner-only cause, else on the harness), never offered or leased to another fixer."""
        st = A.process_store()
        for key, x in self.launch_deaths.items():
            it = st.item("red", key)
            if x["same"] and not x["said"] and it["state"] != "blocked-external":
                x["said"] = True
                self.violate("IL", f"red {key}: every fixer launch died of one cause, yet the red is {it['state']}")

    def check_escalations(self):
        """E. THE ESCALATION DISPATCHER'S INVARIANTS, by the simulation's own truth (the sessions it ran):
        IE one live fixer per kind and at most ESCALATION_FIXERS in all; IO an owner-only kind never started; IG every
        dispatched escalation cleared or handed to the owner within its budget (its tries' wall times and backoffs)."""
        w = self.escalations
        alive = [x for x in w.sessions.values() if x["alive"]]
        kinds = [x["kind"] for x in alive]
        for k in set(kinds):
            if kinds.count(k) > 1:
                self.violate("IE", f"{kinds.count(k)} escalation fixers of {k} at once")
        if len(alive) > A.ESCALATION_FIXERS:
            self.violate("IE", f"{len(alive)} escalation fixers alive, past the cap {A.ESCALATION_FIXERS}")
        for x in w.sessions.values():
            if x["owner"] and not x.get("io_said"):
                x["io_said"] = True
                self.violate("IO", f"owner-only {x['kind']} ({x['key']}) dispatched")
        for key, c in w.conditions.items():
            if c["first_dispatch"] is None or c.get("ig_said") or w.settled(key):
                continue
            if self.clock[0] - c["first_dispatch"] > w.budget(c["route"]):
                c["ig_said"] = True
                self.violate("IG", f"escalation {c['kind']} ({key}) neither cleared nor handed to the owner "
                             f"{int(self.clock[0] - c['first_dispatch']) // TICK} ticks after its first dispatch")

    def name_older_run(self):
        """The red's item names its older run now (a tick attached it)."""
        st = A.process_store()
        self.red_item_runs.add(self.red_older)
        st.update_item("red", self.red_item, st.item("red", self.red_item)["version"], {"runs": sorted(self.red_item_runs)})

    def close_red_item(self):
        if self.red_item:
            st = A.process_store()
            it = st.item("red", self.red_item)
            if it and it["state"] not in ("verified", "cleared"):
                st.transition("red", it["id"], "verified", it["version"], event={"cause": "ci-verdict", "text": "sim: the gate went green"})
            self.red_item, self.red_item_runs = None, set()

    def open_outside_red(self):
        """A red OUTSIDE the gate (08010c's Alert backtest #6): a store item, gate False; the gate stays green."""
        self.outside_run += 1
        name = str(self.outside_run)
        rid = f"sim-outside-{name}"
        A.process_store().put_item("red", rid, "offered", {"key": rid, "workflow": "Alert backtest", "gate": False,
                                                   "red_kind": "outside", "runs": [name]})
        self.outside[name] = rid
        return name

    def close_outside_reds(self):
        for name, at in list(self.outside_clears.items()):
            if self.tick >= at:
                st = A.process_store()
                it = st.item("red", self.outside[name])
                st.transition("red", it["id"], "verified", it["version"], event={"cause": "ci-verdict", "text": "sim: fix verified"})
                del self.outside[name], self.outside_clears[name]

    def forge(self):
        self.close_outside_reds()
        self.refresh_episode_snap()
        if self.red_clears_at is not None and self.tick >= self.red_clears_at:
            self.reds, self.red_since, self.red_clears_at = set(), None, None
            self.green_ticks.append(self.tick)
            self.close_red_item()
        if self.tick >= self.drain_from and self.reds and self.red_clears_at is None:
            self.red_clears_at = self.tick + 1  # the drain: the gate goes green
        if self.tick >= self.drain_from:  # and every red outside it no open fix is on its way for clears (a later run)
            named = {str(f) for e in A.waiting_entries(self.d, open_only=False) if not A.entry_final(e.get("status"))
                     for f in e.get("fixes") or []}
            for name in set(self.outside) - named:
                self.outside_clears.setdefault(name, self.tick + 1)
        if self.calm or self.tick >= self.drain_from:
            return
        if self.script is None and not self.reds and self.rng.random() < self.profile.get("red", 0.02):
            self.open_red()
            if self.rng.random() < 0.8:  # its fixer lands a fix
                self.arrivals.append((self.tick + self.rng.randint(1, 4), {"kind": "redfix"}))
                self.arrivals.sort(key=lambda x: x[0])
            else:  # a later run goes green by itself (an infra red)
                self.red_clears_at = self.tick + self.rng.randint(10, 30)
        if self.script is None and self.outside_rng.random() < self.profile.get("outside_red", 0.02):
            self.open_outside_red()
            if self.outside_rng.random() < 0.9:  # its fixer lands a fix
                self.arrivals.append((self.tick + self.outside_rng.randint(1, 4), {"kind": "outside-fix"}))
                self.arrivals.sort(key=lambda x: x[0])
        if self.reds and self.red_clears_at is None and self.tick - self.red_since > 60:
            self.red_clears_at = self.tick + 1  # never red for ever: the owner's hand
        if self.rng.random() < self.profile.get("outside", 0.03):  # a push to main from outside the queue
            self.next_cid += 1
            pid = f"o{self.next_cid}"
            self.patches[pid] = Patch(pid, -self.next_cid, [self.rng.choice(("docs/Shared.txt", f"docs/o{letters(self.next_cid)}.md"))])
            self.forge_push(self.child(self.main, pid), "outside")

    def bind_conflicts(self):
        """A change marked to conflict conflicts with the first patch landing after it that touches one of its
        modules -- known only once main moves."""
        for pid, p in self.patches.items():
            if "*future*" not in p.conflicts:
                continue
            mods = {x.split("/")[0] for x in p.paths}
            base = self.changes_by_cid(p.cid).base
            for c in self.chain(base, self.main):
                q = self.patches[self.commits[c][1]]
                if q.cid < 0 and mods & {x.split("/")[0] for x in q.paths}:  # an outside push (a queued change: at arrival)
                    p.conflicts = {q.pid}
                    break

    def changes_by_cid(self, cid):
        return next(c for c in self.changes if c.cid == cid)

    # ---------------------------------------------------------------- the run
    def step(self):
        self.agents()
        self.forge()
        self.bind_conflicts()
        for t in list(self.tasks):
            if not t.done and t.due is not None and t.due <= self.tick:
                t.step()
        before = {e["id"]: dict(e) for e in A.waiting_entries(self.d, open_only=False) if e.get("status") in A.QUEUED}
        self.open_before = set(self.reds) | set(self.outside)
        self.known_entries = {e["id"] for e in A.waiting_entries(self.d, open_only=False)}
        reds = set(self.reds)
        A.project_changes(self.d, self.store)
        A.close_landed_changes(self.repo, self.sim_clock, self.store, self.d)
        for x in A.tick_changes(self.repo, self.d, *on_clock(self.sim_clock)):
            A.wlog(self.d, f"tick {self.tick}: {x}")
        for x in A.tick_batch(self.repo, self.d, *on_clock(self.sim_clock)):
            A.wlog(self.d, f"tick {self.tick}: {x}")
        if self.tick % 10 == 0:
            A.stats_pass(self.sim_clock, self.store, self.repo, self.d)
        for it in A.process_store().items("change"):  # what held each change, tick by tick (the overlap tests read it)
            hold = (it.get("data") or {}).get("hold")
            if hold and it["state"] in ("queued", "checked"):
                self.holds.setdefault(it["id"], []).append((self.tick, hold.get("kind"), hold.get("blocker"),
                                                            tuple(hold.get("files") or ())))
                # 10. GENERATED FILES ARE NO OVERLAP: no change held behind a QUEUED one for generated files alone
                # (but one whose own generated files could not be resolved, held as before; and A GENERATED OVERLAP
                # WAITS FOR A CHECK IN FLIGHT, queue.py)
                entry = self.entry_by_id(it["id"]) or {}
                blocker = self.entry_by_id(hold.get("blocker") or "") or {}
                if hold.get("kind") == "overlap" and hold.get("files") and set(hold["files"]) <= set(GENERATED) \
                        and entry.get("batch_alone") != "regen" and blocker.get("status") not in A.IN_FLIGHT + ("landed",) and not (
                            self.profile.get("before_tiers") and set(hold["files"]) <= set(SNAPSHOTS)):
                    self.violate("I10", f"change {it['id']} held behind {hold.get('blocker')} for generated files alone: "
                                 f"{', '.join(hold['files'])}")
                # T. AN IDLE HOLDER'S HOLD EXPIRES (queuehold.py): an overlap hold on a change not in flight lasts at
                # most HOLD_IDLE_TTL (a tick of slack for the tick that lets go of it)
                if hold.get("kind") == "overlap" and blocker.get("status") not in A.HOLDER_IN_FLIGHT \
                        and self.clock[0] - float(hold.get("since") or self.clock[0]) > A.knob_hold_idle_ttl() + 2 * TICK:
                    self.violate("IT", f"change {it['id']} held {int((self.clock[0] - float(hold['since'])) // TICK)} "
                                 f"ticks behind {hold.get('blocker')} ({blocker.get('status')}), which is not in flight")
        if self.escalations:
            self.escalations.tick()
            self.check_escalations()
        if self.launch_deaths:
            self.check_launch_deaths()
        self.check_invariants(before, reds)
        self.check_records()
        self.tick += 1
        self.clock[0] += TICK

    keep = os.environ.get("AGENTCTL_TEST_SIM_KEEP")

    def run(self, max_ticks=None, drain=400, stop=True):
        """The arrivals, then a drain (the gate green, no new arrivals) until every change is decided or the bound.
        stop False (the gate's count mode, tests/sim_gate.py): runs on past a violation, so every invariant kind a
        scenario breaks is counted, not only the first (at most VIOLATIONS_MAX lines kept)."""
        self.install()
        go = lambda: not self.violations if stop else len(self.violations) < VIOLATIONS_MAX
        try:
            last = max([t for t, _s in self.arrivals] + [0])
            self.drain_from = last + 1
            limit = (max_ticks or last + 1) + drain
            while self.tick < limit and go():
                self.step()
                if self.tick > self.drain_from and self.quiet():
                    break
            if go():
                self.final_checks()
        finally:
            self.uninstall()
        return self

    def quiet(self):
        return not self.reds and not self.outside and not [e for e in A.waiting_entries(self.d, open_only=False)
                                      if not A.entry_final(e.get("status"))] and not self.arrivals \
            and not (self.escalations and self.escalations.open())

    # ---------------------------------------------------------------- the invariants
    def check_invariants(self, before, reds):
        st = A.process_store()
        items = st.items("change")
        open_ = [it for it in items if not A.machine_state("change", it["state"]).terminal]
        # 1. ONE OPEN ITEM PER CHANGE (worktree + head): the head as the change (its last patch), whichever sha a
        # rebase gave it
        seen = {}
        for it in open_:
            dd = it.get("data") or {}
            k = (dd.get("worktree"), self.commits.get(dd.get("head"), (None, dd.get("head")))[1])
            if k in seen:
                self.violate("I1", f"two open items for one change {os.path.basename(k[0] or '?')} ({k[1]}): "
                             f"{seen[k]} and {it['id']}")
            seen[k] = it["id"]
        # 2. NEVER TWO PUSHES OF ONE CHANGE: forge_push (a patch twice on main)
        # 3. A RED FIX IS NEVER BEHIND A BATCH OR A NON-RED CHANGE -- (a) a fix of ANY open red, the gate's or one
        # outside it (08010c): its KIND, as the queue classifies it (changekind.py)
        entries = {e["id"]: e for e in A.waiting_entries(self.d, open_only=False)}
        redfix_holder = A.lease_live(f"slot/check/{os.path.basename(self.d.rstrip('/'))}/{A.RED_FIX_TREE}", self.store)
        # the simulation's own truth, never the classifier under test: its Fixes-Red names a red open before and after
        # this tick (the gate's episode, or one outside the gate)
        open_now = set(self.reds) | set(self.outside)
        for eid, e in entries.items():
            named = {str(x) for x in e.get("fixes") or []}
            if A.entry_final(e.get("status")) or not named & open_now & self.open_before or reds != self.reds:
                continue
            if e.get("backlog_batch"):
                self.violate("I3", f"red fix {eid} is a member of batch {e['backlog_batch']}")
            due = not float(e.get("retry_after") or 0) > self.clock[0]
            if e["status"] == "queued" and due and not A.checker_alive(self.store, eid) \
                    and before.get(eid, {}).get("status") == "queued" \
                    and not float(before[eid].get("retry_after") or 0) > self.clock[0] - TICK:
                other = redfix_holder and eid not in json.dumps(redfix_holder.get("meta") or {})
                if not other:
                    self.violate("I3", f"red fix {eid} queued and not started: held behind other work")
            if e["status"] == "checked" and due and before.get(eid, {}).get("status") == "checked":
                self.violate("I3", f"red fix {eid} checked and not pushed: its push held behind other work")
        self.check_hold_invariants(st, entries)
        # W. A RED FIX OF AN OPEN GATE RED THE STORE NAMES NEVER WAITS FOR GREEN (2026-11-25, batch #124: Main #2898
        # red, the fix named #2896 -- a run of the same red its item named -- and waited for the green only it could
        # bring): the simulation's own truth, the names the gate reads and its item holds
        known = set(self.reds) | self.red_item_runs | (self.episode_runs if self.reds else set())
        for eid, e in entries.items():
            if e.get("status") == "waiting" and self.reds and self.names_the_red(eid, known):
                n = self.fix_waits[eid] = self.fix_waits.get(eid, 0) + 1
                if n > self.profile.get("heal_bound", 3):
                    self.violate("IW", f"red fix {eid} (names {sorted(e.get('fixes') or [])}) waiting for green {n} ticks "
                                 f"though the store names it a run of the open red {sorted(known)}")
            else:
                self.fix_waits.pop(eid, None)
        # K. A TOOLS-ONLY CHANGE FAILING ONLY AS THE GATE RED FAILS NEVER WAITS FOR GREEN (the owner, 2026-11-26: "let it
        # through"): the simulation's own truth, the patches' paths and flag, never the rule under test (redland.py)
        for eid, e in entries.items():
            ch = self.by_eid.get(eid)
            if e.get("status") == "waiting" and self.reds and ch and self.lets_through(ch):
                n = self.known_red_waits[eid] = self.known_red_waits.get(eid, 0) + 1
                if n > self.profile.get("heal_bound", 3):
                    self.violate("IK", f"tools-only change {eid} failing only as the red {sorted(self.reds)} fails waiting "
                                 f"for green {n} ticks")
            else:
                self.known_red_waits.pop(eid, None)
        # 4. A BATCH REACHES LANDED, BISECTING OR HELD WITHIN N TICKS (counted in its assembling/checking/pushing ticks)
        for bt in st.items("batch"):
            # a red pre-check round ended (batch.py THE BATCH IS THE MECHANISM): the bound counts the way to landing
            # from green, not the red's length -- seen blocked at a tick's end, or assembled anew within one
            tip = (bt.get("data") or {}).get("tip")
            if bt["state"] == "blocked-red" or ((bt.get("data") or {}).get("depth") == "red"
                                                and self.batch_tips.get(bt["id"]) != tip):
                self.batch_seen[bt["id"]] = 0
            self.batch_tips[bt["id"]] = tip
            if A.machine_state("batch", bt["state"]).terminal or bt["state"] in ("bisecting", "held", "blocked-red"):
                continue
            n = self.batch_seen[bt["id"]] = self.batch_seen.get(bt["id"], 0) + 1
            if n > self.profile.get("batch_bound", 40):
                self.violate("I4", f"batch {bt['id']} {bt['state']} for {n} ticks without landing, bisecting or holding")
        for ch in self.changes:
            e = self.entry(ch) or {}
            if ch.cid not in self.done_at and A.entry_final(e.get("status")):
                self.done_at[ch.cid] = self.tick
                if e.get("status") == "rejected" and "no longer applies" in (e.get("why") or ""):
                    self.saw_conflict_rejection(st, ch, e)
        # 5. THE OLDEST CHANGE LANDS WITHIN A BOUND WHEN NOTHING FAILS (a calm seed)
        if self.calm:
            bound = self.profile.get("land_bound", 60)
            for ch in self.changes:
                e = self.entry(ch)
                if e and not A.entry_final(e.get("status")) and self.tick - ch.arrived > bound:
                    self.violate("I5", f"change {ch.eid} ({ch.kind}) unlanded {self.tick - ch.arrived} ticks after it arrived "
                                 f"with nothing failing (bound {bound})")
                    break
        # 6. NO CHANGE LOST: every entry has its item, every item its entry, and they agree on decided states
        by_id = {it["id"]: it for it in items}
        for eid, e in entries.items():
            it = by_id.get(eid)
            if it is None:
                if eid in self.known_entries:  # one written this tick gets its item at the next projection
                    self.violate("I6", f"entry {eid} ({e.get('status')}) has no store item")
                continue
            if A.entry_final(e.get("status")) and not A.machine_state("change", it["state"]).terminal:
                self.violate("I6", f"entry {eid} is {e['status']} but its item is {it['state']}")
            if A.machine_state("change", it["state"]).terminal and not A.entry_final(e.get("status")) \
                    and A.change_state_of(e.get("status")) != it["state"]:
                self.violate("I6", f"item {eid} is {it['state']} but its entry is {e.get('status')}")
        for it in items:
            if it["id"] not in entries:
                self.violate("I6", f"item {it['id']} ({it['state']}) has no queue entry: stranded")
        # S. ONE STATE PER CHANGE (docs/design/one-change-model.md): the entry and the item a change has both say the same
        for eid, e in entries.items():
            it = by_id.get(eid)
            if it is not None and A.change_state_of(e.get("status")) != it["state"]:
                self.violate("IS", f"change {eid}: its entry says {e.get('status')}, its item {it['state']}")
        for ch in self.changes:
            e = self.entry(ch)
            if e and e.get("status") == "landed":
                on = self.patches_of(self.main)
                if not set(ch.patches) <= on:
                    self.violate("I6", f"change {ch.eid} LANDED but {sorted(set(ch.patches) - on)} not on main: lost")

    def lets_through(self, ch):
        """Whether change ch is tools-only and its checks fail only in the red's own test (the simulation's truth)."""
        ps = [self.patches[p] for p in ch.patches]
        return any(p.main_fails for p in ps) and not any(sim_production(x) for p in ps for x in p.paths)

    def names_the_red(self, eid, known):
        """Whether EVERY commit of change eid carries a Fixes-Red naming one of `known` (the hook's rule: a production
        commit naming no red waits, so a change with one commit that does not is no fix of the red)."""
        ch = self.by_eid.get(eid)
        return bool(ch) and all(self.patches[p].fixes & known for p in ch.patches)

    def check_records(self):
        """C. THE ITEM CARRIES THE RECORD (docs/design/one-change-model.md, step 3): after every tick an open change's
        item holds its entry, every key but its status, under waitingdir.RECORD_KEY."""
        st = self.store
        for e in A.waiting_entries(self.d, open_only=False):
            eid = e["id"]
            if A.entry_final(e.get("status")):
                continue
            it = st.item("change", eid)
            if it is not None and (it.get("data") or {}).get("record") != {k: v for k, v in e.items() if k != "status"}:
                self.violate("IC", f"change {eid} ({e.get('status')}): its item's record is not its entry")

    def check_hold_invariants(self, st, entries):
        """(b) EVERY CHANGE PAST ITS DEADLINE HAS A RECORDED HOLD, (c) NO CHANGE IS SILENT: every held change's
        `land status` line says why, (d) THE PRIORITY ORDER: executors start in PRIORITY's order within a tick."""
        t = self.clock[0]
        for it, _e in A.unheld_changes(st, entries, t):
            self.violate("Ib", f"change {it['id']} {it['state']} {int((t - it['entered_at']) // TICK)} ticks, past its "
                         f"deadline, and nothing recorded holding it (hold {(it['data'] or {}).get('hold')})")
        bt = A.current_batch(st, self.d)
        rows = A.change_rows(st)
        in_batch = A.batch_row_ids(bt, rows) if bt else set()
        for r in rows:
            it = st.item("change", r["id"])
            if not it or not A.change_past_deadline(it, t):
                continue
            line = A.land_status_line(r, bt, in_batch, t)
            e = entries.get(r["id"]) or {}
            if "held:" not in line and "(in batch" not in line and not A.checker_alive(self.store, r["id"]):
                self.violate("Ic", f"change {r['id']} {r['state']} past its deadline, silent in `land status`: {line.strip()}"
                             + (f" (a member of {e['backlog_batch']})" if e.get("backlog_batch") else ""))
        now = [x for x in self.spawned if x[0] == self.tick and x[2] is not None]
        for (_t, a, ra), (_t2, b, rb) in zip(now, now[1:]):
            if rb < ra:
                self.violate("Id", f"{b} ({A.PRIORITY[rb][0]}) started before {a} ({A.PRIORITY[ra][0]}) in one tick: not "
                             "the documented priority order")

    def saw_conflict_rejection(self, st, ch, e):
        """INVARIANT 8: a change that no longer applies because of a change open when it was queued was told so at
        queue time (a CONFLICTS event within a tick of its arrival) and never checked meanwhile."""
        mine = set().union(*(self.patches[p].conflicts for p in ch.patches))
        culprit = next((c for c in self.changes if c.cid in self.open_at_arrival.get(ch.cid, ())
                        and set(c.patches) & mine and set(c.patches) <= self.patches_of(self.main)), None)
        if not culprit or e.get("fixes"):  # a red fix never waits: told, and checked all the same
            return
        told = [ev for ev in st.item_events("change", ch.eid) if (ev.get("text") or "").startswith("CONFLICTS")]
        when = min((ev["at"] for ev in told), default=None)
        arrived_at = self.clock[0] - (self.tick - ch.arrived) * TICK
        if self.tick <= ch.arrived + 1 and not self.checks_of.get(ch.eid):
            return  # NOT LANDED at its first tick, no check spent: that is being told at queue time
        if when is None or when > arrived_at + TICK:
            self.violate("I8", f"change {ch.eid} no longer applies after {culprit.eid} landed (open when it was queued), and "
                         f"was not told at queue time")
        elif self.checks_of.get(ch.eid):
            self.violate("I8", f"change {ch.eid} was checked {self.checks_of[ch.eid]} time(s) before it no longer applied "
                         f"after {culprit.eid}, which it was told it conflicts with")

    def final_checks(self):
        st = A.process_store()
        for ch in self.changes:
            e = self.entry(ch)
            if e is None:
                self.violate("I6", f"change {ch.cid} was never queued")
                continue
            if not A.entry_final(e.get("status")):
                self.violate("I6", f"change {ch.eid} still {e.get('status')} after the drain (gate green, nothing arriving)")
            if e.get("status") == "cancelled" and not ch.cancelled:
                self.violate("I6", f"change {ch.eid} cancelled, but no agent cancelled it")
            if e.get("status") == "superseded" and not any(o.worktree == ch.worktree and o.cid > ch.cid for o in self.changes):
                self.violate("I6", f"change {ch.eid} superseded by nothing newer")
        # 7. STATS: exactly one landing record per decided change, `landed` for each landed one
        for _ in range(3):
            A.stats_pass(self.sim_clock, self.store, self.repo, self.d)
        recs = {}
        for r in self.stats:
            if r.get("record") == "agentctl.landing":
                recs.setdefault(r["change_id"], []).append(r)
        for it in st.items("change"):
            n = len(recs.get(it["id"], []))
            if A.is_terminal("change", it["state"]) and n != 1:
                self.violate("I7", f"change {it['id']} ({it['state']}): {n} landing records, not one")
            if it["state"] == "landed" and n == 1 and recs[it["id"]][0].get("outcome") != "landed":
                self.violate("I7", f"change {it['id']} landed, its record says {recs[it['id']][0].get('outcome')}")
        landed_entries = {ch.eid for ch in self.changes if (self.entry(ch) or {}).get("status") == "landed"}
        landed_items = {it["id"] for it in st.items("change") if it["state"] == "landed"}
        if landed_entries != landed_items:
            self.violate("I7", f"landed entries {sorted(landed_entries ^ landed_items)} disagree with their items")


# --------------------------------------------------------------------------------------------------------- replays
def replay_script(day):
    """A day of real landings (tests/fixtures/landing-replay-7d.json, from victoria-logs-ci) as a fixed arrival
    script: the gaps between submissions compressed (at most 6 ticks), each change's modules, identity area, red fix
    and outcome kept (checks-failed: a breaking patch; conflict: it conflicts with what lands next in its module;
    superseded: its worktree lands again; cancelled / precheck: its agent cancels it)."""
    out, t, prev = [], 0, None
    for c in day["changes"]:
        if prev is not None:
            t += min(6, max(0, round((c["at"] - prev) / 300)))  # 5 real minutes a tick, gaps capped
        prev = c["at"]
        kind = "redfix" if c["red_fix"] else "identity" if c["identity"] else "plain"
        out.append((t, {"kind": kind, "modules": c["modules"][:3] or ["docs"], "worktree": c["worktree"],
                        "bad": c["outcome"] == "checks-failed", "conflict": c["outcome"] == "conflict",
                        "outcome": c["outcome"] if c["outcome"] in ("superseded", "cancelled", "precheck") else None}))
    return out


# THE THREE COLLISIONS OF 2026-11-17 (victoria-logs-ci agentctl.landing, reason_class conflict): refactor/scraper-catalog
# (18:04) and refactor/break-cycles (18:17) NOT LANDED at 19:17 when refactor/scrapers (17:50) landed; a CI-workflows
# change NOT LANDED when another .github change landed first ("B1"). Each cost a check, a NOT LANDED, a rebase and a full
# recheck. Here: the scrapers refactor (an identity change: checked alone) and a CI-workflows change in flight; the
# catalog shares a file with the scrapers refactor and applies on top of it; B1 conflicts textually with the
# CI-workflows change; the cycle-breaking change shares files with both and conflicts textually with the scrapers
# refactor.
COLLISIONS_2026_11_17 = [
    (0, {"label": "scrapers", "worktree": "ref-scrapers", "kind": "identity",
         "paths": ["scrapers/src/main/Catalog.scala", "common/src/main/Scrape.scala", "identity/src/main/Ids.scala"]}),
    (1, {"label": "ci-workflows", "worktree": "ci-workflows",
         "paths": [".github/workflows/main.yml", "scripts/ci/caches.sh"]}),
    (1, {"label": "catalog", "worktree": "scraper-catalog",
         "paths": ["scrapers/src/main/Catalog.scala", "docs/catalog.md"]}),
    (2, {"label": "b1", "worktree": "b1", "paths": [".github/workflows/main.yml"], "conflicts_with": "ci-workflows"}),
    (2, {"label": "break-cycles", "worktree": "break-cycles",
         "paths": ["common/src/main/Scrape.scala", "web/src/main/Cycle.scala", ".github/workflows/main.yml"],
         "conflicts_with": "scrapers"}),
]


REPLAY_11_19 = "landing-replay-11-19.json"  # 2026-11-19..21's arrivals (CHEAPER RECHECKS' before and after)


def replay_days(fixture="landing-replay-7d.json"):
    with open(os.path.join(HERE, "fixtures", fixture)) as f:
        return json.load(f)["days"]


# THE BASELINE QUEUE OF 2026-11-19 (~06:05Z): six changes from parallel refactoring lanes, each editing
# coupling-baseline.tsv (several complexity- and length-baseline.tsv too), each `held: its check waits for <prev>`
# behind the one queued before it; f2 and enrichment2 also share a real source, TestWiring.scala. The resolver lanes
# and enrichment2 are identity changes (checked alone). GENERATED FILES ARE NO OVERLAP (waiting.py): only the
# TestWiring.scala overlap still holds.
COUPLING, COMPLEXITY, LENGTH = GENERATED[:3]
TEST_WIRING = "worker/src/fixtures/scala/tools/TestWiring.scala"
BASELINES_2026_11_19 = [
    (0, {"label": "agreement", "worktree": "phase3-agreement",
         "paths": ["web/src/main/Agreement.scala", COUPLING, COMPLEXITY]}),
    (0, {"label": "f2", "worktree": "phase3-f2", "paths": ["web/src/main/F2.scala", TEST_WIRING, COUPLING, LENGTH]}),
    (1, {"label": "datalayer3", "worktree": "phase3-datalayer3",
         "paths": ["common/src/main/DataLayer.scala", COUPLING, COMPLEXITY, LENGTH]}),
    (1, {"label": "resolver2-b1", "worktree": "phase3-resolver2-b1", "kind": "identity",
         "paths": ["identity/src/main/ResolverB1.scala", COUPLING]}),
    (2, {"label": "enrichment2", "worktree": "phase3-enrichment2", "kind": "identity",
         "paths": ["identity/src/main/Enrichment.scala", TEST_WIRING, COUPLING, COMPLEXITY]}),
    (2, {"label": "resolver2", "worktree": "phase3-resolver2", "kind": "identity",
         "paths": ["identity/src/main/Resolver.scala", COUPLING, LENGTH]}),
]


# THE TWO HOLDS OF 2026-11-21 (~07:40Z, `agentctl land status`): split-shared-js `held: its check waits for scrapers-2
# (checking)`, both changing the page snapshots expected-{index,warszawa-index,wroclaw-index}.html; worker-rest-2 held
# behind review-common, both changing RepositoryReadSwallows.scala, a hand-written Scala Map. Replayed with the four
# QUEUED TOGETHER (HOLDS_2026_11_21: the snapshots conflict textually; the two appends to the Map conflict, each having
# given its last entry a comma) and AS OBSERVED (HOLDS_2026_11_21_IN_FLIGHT: the younger two queued once the older two
# were checking in a batch).
SWALLOWS = "worker/src/test/scala/tools/RepositoryReadSwallows.scala"
HOLDS_2026_11_21 = [
    (0, {"label": "review-common", "worktree": "review-common", "paths": ["common/src/main/Common.scala", SWALLOWS]}),
    (0, {"label": "scrapers-2", "worktree": "review-scrapers-2", "paths": ["scrapers/src/main/Scrapers.scala", *SNAPSHOTS]}),
    (0, {"label": "worker-rest-2", "worktree": "review-worker-rest-2", "paths": ["worker/src/main/Rest.scala", SWALLOWS],
         "resolvable_with": ["review-common"]}),
    (0, {"label": "split-shared-js", "worktree": "split-shared-js",
         "paths": ["web/src/main/assets/js/shared.js", *SNAPSHOTS], "gen_conflicts_with": ["scrapers-2"]}),
]
# as observed, each younger merges cleanly with its older (git merge-tree, 2026-11-21: 80b5396d7c, 13ee018167)
HOLDS_2026_11_21_IN_FLIGHT = [(0, s) for _t, s in HOLDS_2026_11_21[:2]] + [
    (1, {k: v for k, v in s.items() if k not in ("resolvable_with", "gen_conflicts_with")}) for _t, s in HOLDS_2026_11_21[2:]]


# THE 08010c TIMELINE OF 2026-11-19 (~07:40-09:50Z; c74b64f): a red OUTSIDE the gate open (Alert backtest #6, red
# 5254bffda9d2), the gate green. Two ordinary changes checked; main moved under them (no file overlap): each owed a
# combined-tree verification, and the two made push batch #47. Its fix (`Fixes-Red: 6`, infra/bin/backtest-alerts)
# arrived meanwhile and was checked at 08:32Z -- then waited 22 min behind the verifications, was left out of the
# batch, and waited behind its whole verify round, `checked` past its deadline with nothing said. fix-replay, changing
# the same file, queued behind it. Every invariant must hold, and the fix must land within FIX_08010C_BOUND ticks.
TIMELINE_08010C = [
    (0, {"kind": "open-outside-red"}),
    (0, {"label": "a", "worktree": "phase3-a", "paths": ["web/src/main/A.scala"]}),
    (0, {"label": "b", "worktree": "phase3-b", "paths": ["worker/src/main/B.scala"]}),
    (3, {"kind": "push", "paths": ["docs/moved.md"]}),
    (4, {"label": "fix", "worktree": "backtest-fix", "kind": "outside-fix", "paths": ["infra/bin/backtest-alerts"]}),
    (5, {"label": "replay", "worktree": "fix-replay", "paths": ["infra/bin/backtest-alerts", "docs/replay.md"]}),
    (6, {"kind": "push", "paths": ["docs/moved-again.md"]}),
]
# SEVERAL RUNS OF ONE RED (2026-11-25, batch #124: Main #2898 red, the fix named #2896, an older run of the same red).
# The fix naming an older run its red item holds lands on the red beside an ordinary change; the fix naming a run the
# item only learns of a few ticks later (a tick attached it) is let go when it does (WR self-heals); the fix of ANOTHER
# failure -- a run no item names -- waits for green, as the rule says.
REDFIX_OLDER_RUN = [
    (0, {"kind": "open-red"}),
    (0, {"label": "a", "worktree": "a", "paths": ["web/src/main/A.scala"]}),
    (1, {"label": "fix", "worktree": "fix", "kind": "redfix-older", "one_commit": True, "paths": ["worker/src/main/Fix.scala"]}),
    (1, {"label": "b", "worktree": "b", "paths": ["common/src/main/B.scala"]}),
    (20, {"label": "late", "worktree": "late", "paths": ["docs/late.md"]}),
]
REDFIX_HEALS = [
    (0, {"kind": "open-red", "older_known": False}),
    (0, {"label": "a", "worktree": "a", "paths": ["web/src/main/A.scala"]}),
    (1, {"label": "fix", "worktree": "fix", "kind": "redfix-older", "one_commit": True, "paths": ["worker/src/main/Fix.scala"]}),
    (8, {"kind": "learn-run"}),
    (30, {"label": "late", "worktree": "late", "paths": ["docs/late.md"]}),
]
REDFIX_OTHER_FAILURE = [
    (0, {"kind": "open-red"}),
    (0, {"label": "a", "worktree": "a", "paths": ["web/src/main/A.scala"]}),
    (1, {"label": "fix", "worktree": "fix", "kind": "redfix-other", "one_commit": True, "paths": ["worker/src/main/Fix.scala"]}),
    (20, {"label": "late", "worktree": "late", "paths": ["docs/late.md"]}),
]
# A FIX BESIDE A BARE PRODUCTION COMMIT (2026-11-26, the red-landing rule; 2026-10-31, Main #2877): the fix's second
# commit touches production and names no red, so the change may not land on the red -- it waits for green, while a
# tools-only change lands on the red at once; it is no gate fix, so nothing waits for it (IR, IW, I3).
REDFIX_BESIDE_BARE = [
    (0, {"kind": "open-red"}),
    (1, {"label": "fix", "worktree": "fix", "kind": "redfix-older", "two_commits": True, "paths": ["worker/src/main/Fix.scala"]}),
    (2, {"label": "tools", "worktree": "tools", "paths": ["scripts/tools.sh"]}),
    (3, {"label": "a", "worktree": "a", "paths": ["web/src/main/A.scala"]}),
    (20, {"label": "late", "worktree": "late", "paths": ["docs/late.md"]}),
]
# A TOOLS-ONLY CHANGE FAILING ONLY AS THE GATE RED FAILS (2026-11-26, the owner's "let it through"): its checks fail in
# the red's own test only, so it lands on the red at once (IK); a production change failing the same way still waits for
# green and never lands on the red without naming it (IR).
KNOWN_RED_TOOLS = [
    (0, {"kind": "open-red"}),
    (1, {"label": "tools", "worktree": "tools", "one_commit": True, "main_fails": True, "paths": ["scripts/tools.sh"]}),
    (2, {"label": "prod", "worktree": "prod", "one_commit": True, "main_fails": True, "paths": ["web/src/main/A.scala"]}),
    (3, {"label": "docs", "worktree": "docs", "one_commit": True, "main_fails": True, "paths": [".github/ci.yml"]}),
    (20, {"label": "late", "worktree": "late", "paths": ["docs/late.md"]}),
]
# AN EPISODE READ FROM A STALE SIGNATURE (2026-11-26, one RedEpisode): the fix names the red's older run, which only the
# gate's streak and that run's signature cached by an OLDER classifier say is the same failure. The gate's copy
# counted the stale record, the queue facts' did not: the fix waited for the green only it could bring (IW).
REDFIX_STALE_EPISODE = [
    (0, {"kind": "open-red", "older_known": False}),
    (0, {"kind": "stale-episode"}),
    (0, {"label": "a", "worktree": "a", "paths": ["web/src/main/A.scala"]}),
    (1, {"label": "fix", "worktree": "fix", "kind": "redfix-older", "one_commit": True, "paths": ["worker/src/main/Fix.scala"]}),
    (20, {"label": "late", "worktree": "late", "paths": ["docs/late.md"]}),
]

class SeamStore:
    """The store a simulated holder writes through, running `after_open` once its open_singleton returns and
    `before_move` before each transition: where another holder's step lands (Sim.two_holders)."""

    def __init__(self, store, after_open=None, before_move=None):
        self.store, self.after_open, self.before_move = store, after_open, before_move

    def __getattr__(self, name):
        return getattr(self.store, name)

    def open_singleton(self, *a, **k):
        got = self.store.open_singleton(*a, **k)
        if self.after_open:
            self.after_open()
        return got

    def transition(self, *a, **k):
        if self.before_move:
            self.before_move()
        return self.store.transition(*a, **k)


def two_holders_script(order):
    """Two holders at once (ORDER: Sim.two_holders), then ordinary changes the short holds delay a tick or two."""
    return [(0, {"kind": "two-holders", "order": order}),
            (0, {"label": "a", "worktree": "a", "paths": ["web/src/main/A.scala"]}),
            (3, {"label": "late", "worktree": "late", "paths": ["docs/late.md"]})]


TWO_HOLDERS_APART = two_holders_script("apart")
TWO_HOLDERS_EXTEND_FIRST = two_holders_script("extend-first")
TWO_HOLDERS_MOVE_BETWEEN = two_holders_script("move-between")
TWO_HOLDERS_EXPIRE_BETWEEN = two_holders_script("expire-between")
# A HOLD ON A PARKED CHANGE (2026-11-23..25, py-reuse-git behind py-under-bar): the gate red, `a` checked and parked on
# `waiting`; `b`, conflicting with it, queued behind it. The red stays open past HOLD_IDLE_TTL: b's hold on the parked
# change expires (IT), and b is checked meanwhile.
HOLD_ON_PARKED = [
    (0, {"kind": "open-red"}),
    (0, {"label": "a", "worktree": "under-bar", "paths": ["web/src/main/A.scala"]}),
    (1, {"label": "b", "worktree": "reuse-git", "paths": ["web/src/main/B.scala"], "conflicts_with": "a"}),
    (100, {"label": "late", "worktree": "late", "paths": ["docs/late.md"]}),
]
FIX_08010C_BOUND = 8  # ticks from its arrival to LANDED (a check is 2-4 ticks; nothing may hold it)


def sim_production(path):
    """The simulation's own production paths (an sbt module's src/main, as movies' pre-push says)."""
    return path.split("/")[0] in SBT_MODULES and "/src/main/" in path


def landing_waits(sim):
    """{label: ticks from its arrival to its decision} of every scripted change that landed."""
    on_main = sim.patches_of(sim.main)
    return {c.label: sim.done_at[c.cid] - c.arrived for c in sim.changes
            if c.cid in sim.done_at and set(c.patches) <= on_main}


class SimEscalations:
    """THE ESCALATION WORLD (2026-11-26, the escalation dispatcher): conditions opened by `escalate` arrivals, probed by
    the simulation's own probe, and the agents the real dispatcher (escalations.py escalation_pass, each tick, over the
    simulation's store and clock) starts through this launcher. A scripted agent `clears` its condition after its
    work, `fails` (its session ends, the condition open), or `hangs` (works until it is stopped); `clears_on` N clears
    on its Nth try only; `owner` marks a kind the scenario says is the owner's alone. The sessions and those marks are
    the simulation's truth for the invariants (Sim.check_escalations), never the table under test."""

    def __init__(self, sim):
        self.sim, self.rng = sim, random.Random(f"escalations-{sim.seed}")
        self.conditions, self.sessions, self.failing, self.gap = {}, {}, set(), None
        self.saved = dict(A.PROBES), dict(A.PROBE_EVERY)
        A.PROBES["sim_escalation"] = lambda a: (a["key"] not in self.failing, f"sim probe of {a['key']}")
        A.PROBE_EVERY["sim_escalation"] = 60
        self.repo = os.path.join(sim.tmp, "bin")

    def close(self):
        A.PROBES.clear()
        A.PROBES.update(self.saved[0])
        A.PROBE_EVERY.clear()
        A.PROBE_EVERY.update(self.saved[1])

    def open_condition(self, spec):
        key = spec["key"]
        self.failing.add(key)
        c = A.open_condition(spec["condition"], spec["text"], A.run_probe("sim_escalation", {"key": key}), [], key=key,
                             store=self.sim.store, grace=0)
        self.conditions[key] = {"id": c["id"], "kind": A.escalation_kind(c), "route": A.escalation_route(c),
                                "agent": spec.get("agent", "clears"), "clears_on": spec.get("clears_on", 1),
                                "owner": bool(spec.get("owner")),
                                "first_dispatch": None, "tries": 0}

    def budget(self, route):
        """The longest a dispatched escalation may take to end: every try's wall time and lease, every backoff, and a
        few ticks of slack."""
        return sum(route.runtime + A.LEASE_TTL + A.RETRY_BACKOFF * 2 ** i for i in range(route.tries)) + 5 * TICK

    def settled(self, key):
        st = self.sim.store.item("condition", self.conditions[key]["id"])
        return st["state"] == "closed" or bool(((st.get("data") or {}).get("escalation") or {}).get("owner_told"))

    def open(self):
        return any(not self.settled(k) and not v["owner"] for k, v in self.conditions.items())

    def tick(self):
        """The dispatcher's pass, then each live agent's work this tick."""
        A.escalation_pass(self.sim.store, self.sim.clock[0], self)
        for sid, x in self.sessions.items():
            if not x["alive"]:
                continue
            x["left"] -= 1
            if x["left"] > 0 or x["behaviour"] == "hangs":
                continue
            if x["behaviour"] == "clears" and x["key"] in self.failing:
                self.failing.discard(x["key"])
                c = next(c for c in A.open_conditions(self.sim.store) if c["key"] == x["key"])
                A.close_condition(c, A.run_probe("sim_escalation", {"key": x["key"]}), self.sim.store)
            x["over"] = True

    # the launcher (escalations.py EscalationLauncher)
    def host(self):
        return "sim"

    def blocker(self):
        return None

    def repo_path(self, repo):
        return self.repo

    def trust_gap(self, path):
        return self.gap

    def start(self, route, kind, condition, prompt, repo):
        key = condition["data"]["key"]
        c = self.conditions[key]
        c["tries"] += 1
        c["first_dispatch"] = c["first_dispatch"] or self.sim.clock[0]
        behaviour = c["agent"] if c["tries"] >= c["clears_on"] else "fails"
        sid = f"esc{len(self.sessions) + 1}"
        self.sessions[sid] = {"kind": kind, "key": key, "owner": c["owner"], "alive": True,
                              "over": False, "behaviour": behaviour, "left": self.rng.randint(2, 12)}
        return {"session": sid, "worktree": f"{repo}-wt-escalation-{sid}"}, f"sim session {sid}"

    def session_over(self, session):
        x = self.sessions.get(session)
        return True if x is None else x["over"]

    def stop(self, session):
        x = self.sessions.get(session)
        if x:
            x["alive"], x["over"] = False, True


# THE ESCALATIONS (2026-11-26): agent kinds whose fixer clears, fails then clears, never clears (handed to the owner),
# hangs past its wall time; two subjects of one kind; more agent kinds than the cap; owner-only kinds beside them
ESCALATIONS = [
    (0, {"kind": "escalate", "condition": "INVARIANT", "key": "invariant-W2", "text": "INVARIANT W2 violated: red 7"}),
    (0, {"kind": "escalate", "condition": "INVARIANT", "key": "invariant-WB", "text": "INVARIANT WB violated: edits",
         "owner": True}),
    (0, {"kind": "escalate", "condition": "NETWORK", "key": "route", "text": "NETWORK: offline", "owner": True}),
    (0, {"label": "a", "worktree": "a", "paths": ["web/src/main/A.scala"]}),
    (1, {"kind": "escalate", "condition": "STALLED", "key": "queue-stall", "text": "STALLED: queue", "clears_on": 2}),
    (2, {"kind": "escalate", "condition": "STALLED", "key": "batch-x", "text": "STALLED: batch", "agent": "fails"}),
    (3, {"kind": "escalate", "condition": "CLONE", "key": "clone-movies", "text": "CLONE: unhealthy", "agent": "hangs"}),
    (4, {"kind": "escalate", "condition": "INVARIANT", "key": "invariant-WT", "text": "INVARIANT WT violated: roots"}),
    (5, {"kind": "escalate", "condition": "INVARIANT", "key": "invariant-WP", "text": "INVARIANT WP violated: Google "
         "Chrome pid 9: 12 GB -- the owner's to look at (agentctl never stops a process it did not start)", "owner": True}),
    (6, {"label": "b", "worktree": "b", "paths": ["docs/b.md"]}),
]
# THE CRASH LOOPS (2026-11-26): fixers dying at launch of the vault (the owner's), of one other cause, and of a different
# cause each time (climbs the tiers as before)
CRASH_LOOPS = [
    (0, {"kind": "launch-deaths", "key": "loop-vault", "causes": ["waiting for Touch ID: secrets unlock movies"] * 3}),
    (1, {"kind": "launch-deaths", "key": "loop-same", "causes": ["claude: model overloaded"] * 3}),
    (2, {"kind": "launch-deaths", "key": "loop-mixed", "causes": ["claude: model overloaded", "disk full", "bad config"]}),
    (0, {"label": "a", "worktree": "a", "paths": ["web/src/main/A.scala"]}),
]
# the repo untrusted for the first 40 ticks: NOT DISPATCHABLE, no try counted, dispatched once trusted
ESCALATIONS_UNTRUSTED = [(0, {"kind": "trust", "trusted": False})] + ESCALATIONS + [(40, {"kind": "trust", "trusted": True})]
