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

TICK = 60  # s of simulated time per reconciler tick
ROOT = "0" * 40
HOOK_TEXT = """#!/bin/sh
# the simulated repo's hook: never run (the checks are the simulator's); its text tells agentctl what it supports:
# --checks-only --fast-only --verify-only
case "$1" in --plan) ;; esac
exit 0
"""
SBT_MODULES = ("web", "worker", "common", "scrapers", "geodata", "identity")


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
        for q in A.Store.SCHEMA:
            self.c.execute(q)

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
        self.patches, self.commits = {}, {ROOT: (None, None)}
        self._anc = {}
        self.main = ROOT
        self.heads = {}
        self.changes, self.by_eid = [], {}
        self.reds, self.red_run, self.red_since, self.red_clears_at, self.red_fix_due = set(), 100, None, None, None
        self.pushes = []  # (tick, sha, [patches])
        self.pushed_patch_count = {}
        self.tasks = []
        self.local = threading.local()
        self.done_at = {}  # change cid -> the tick its entry was decided
        self.batch_seen = {}  # batch id -> ticks spent in assembling / checking / pushing
        self.stats = []
        self.next_cid = 0
        self.rejected_handled = set()
        self.arrivals = self._plan_arrivals()
        self.n_lone_red = 0

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
        raise Violation(f"(sim) git {' '.join(a)} is not modelled: model it in queue_sim.SimGit")

    def range_paths(self, cwd, a, b, deleted=False):
        if deleted:
            return []
        x, y = self.resolve(cwd, a), self.resolve(cwd, b)
        if not x or not y:
            return []
        return sorted({p for pid in self.patches_of(y) - self.patches_of(x) for p in self.patches[pid].paths})

    # ---------------------------------------------------------------- the fakes
    def apply_entry(self, d, wt, e, base):
        wt = os.path.realpath(wt)
        head = self.heads[wt]
        on_base, on_head = self.patches_of(base), self.patches_of(head)
        new, gone, dup = head, [], []
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
            new = self.child(new, pid)
        if gone:
            dropped = {c for c, _v in gone}
            e["commits"] = [c for c in e["commits"] if c not in dropped]
            A.save_entry(d, e)
            if not e["commits"]:
                return "landed", gone[-1][1]
        if dup and len(dup) == len(e["commits"]):
            return "duplicate", dup[-1][1]
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
            return ["sbt"] + (["identity"] if any(p.startswith("identity/") for p in paths) else [])
        return []

    def commit_fixes(self, repo, sha, store=None):
        pid = self.commits.get(sha, (None, None))[1]
        return set(self.patches[pid].fixes) if pid else set()

    def verdict(self, tip, where):
        """(ok, output) of a check of tip: a breaking patch fails it with its own named test; an infra failure is
        drawn now and then."""
        if not self.calm and self.rng.random() < self.profile.get("infra", 0.03):
            return False, "[error] failed to connect to server\n"
        bad = sorted(pid for pid in self.patches_of(tip) - self.patches_of(ROOT) if self.patches[pid].bad)
        if bad:
            lines = [f"[info] - sim {letters(self.patches[p].cid)} works *** FAILED ***" for p in bad]
            return False, "\n".join(lines + ["! pre-push sbt FAILED (3s)"]) + "\n"
        return True, f"checks passed {where}\n"

    def run_entry_checks(self, repo, d, e, tree, base, head, mode, cls, log_path, own=None):
        reds = set(self.reds)
        if A.red_fix(e, reds) and not tree.endswith("-wt-check-redfix"):
            holder = A.lease_live(f"slot/check/{os.path.basename(self.d.rstrip('/'))}/{A.RED_FIX_TREE}")
            other = next((t.name for t in self.tasks if holder and not t.done and str(t.pid) in holder["holder"]), None)
            if not (other and other.startswith("check-") and A.red_fix(self.entry_by_id(other[6:]) or {}, reds)):
                self.violate(f"red fix {e['id']} checked in {os.path.basename(tree)}, not the red-fix tree (the wrong "
                             f"lane; that tree held by {other or (holder or {}).get('holder')})")
        self.local.task.block(self.rng.randint(1, 2) if mode == A.VERIFY_MODE else self.rng.randint(2, 4))
        ok, out = self.verdict(head, f"{mode} {head[:8]}")
        return ok, out, "in the simulation"

    def batch_check(self, repo, d, tree, base, tip, cls="check", depth="fast", runs=None, range_base=None):
        self.local.task.block(self.rng.randint(1, 2) if depth == "verify" else self.rng.randint(2, 5))
        ok, out = self.verdict(tip, f"batch {depth} {tip[:8]}")
        return ok, out, False

    def run_watched(self, argv, cwd, env, log_path, step, **kw):
        if step != "push":
            raise Violation(f"(sim) run_watched step {step} is not modelled")
        sha = argv[3].split(":", 1)[0]
        commits = self.chain(self.main, sha)
        if self.reds and A.must_wait(self.repo, commits, self.reds):
            return 1, "pre-push: main is red: only a fix may land\n"
        if self.main not in self.anc(sha):
            return 1, " ! [rejected] main -> main (non-fast-forward)\n"
        self.forge_push(sha, "queue")
        return 0, "pushed\n"

    def forge_push(self, sha, who):
        new = [self.commits[c][1] for c in self.chain(self.main, sha)]
        for pid in new:
            self.pushed_patch_count[pid] = self.pushed_patch_count.get(pid, 0) + 1
            if self.pushed_patch_count[pid] > 1:
                self.violate(f"patch {pid} of change {self.patches[pid].cid} pushed to main twice")
        self.pushes.append((self.tick, sha, new, who))
        self.main = sha
        if self.reds and any(self.patches[p].fixes & self.reds for p in new):
            self.red_clears_at = self.tick + self.rng.randint(1, 3)  # the fix's Main run goes green

    def spawn_executor(self, repo, d, eid):
        e = self.entry_by_id(eid) or {}
        if e.get("status") not in ("queued", "checking"):
            self.violate(f"an executor started for {eid}, which is {e.get('status')}")
        self._spawn(f"check-{eid}", lambda: A.check_entry(repo, eid))

    def spawn_batch_executor(self, repo, d, bid):
        self._spawn(f"batch-{bid}", lambda: A.check_batch(repo, bid))

    def _spawn(self, name, fn):
        t = Task(self, name, fn)
        self.tasks.append(t)
        t.thread.start()
        t.step()

    def start_watch(self, repo, sha, pushed_at, **kw):
        return None

    def violate(self, what):
        self.violations.append(f"seed {self.seed} tick {self.tick}: {what}")

    # ---------------------------------------------------------------- set up / tear down
    FAKES = ("git", "git_ok", "fetch_main", "apply_entry", "check_tree", "range_paths", "named_reader", "cherry_landed",
             "change_landed", "hook_query", "commit_fixes", "gate_reds", "gov_class_of", "blame_land_refusal",
             "run_entry_checks", "batch_check", "batch_drift_check", "run_watched", "start_watch", "mark_gate_dirty",
             "spawn_executor", "spawn_batch_executor", "precheck_hook", "verify_areas_supported", "remote_matches",
             "remote_checks_supported", "slug", "cancel_entry_remote", "stats_post", "check_runs_of", "gate_runs_pass",
             "clone_shared", "spawn_waiting_processor", "offer_token", "tool_env", "stats_echo")

    def install(self):
        import contextlib
        self.saved = {n: getattr(A, n) for n in self.FAKES + ("os",)}
        self.saved_clock = (A.time.__dict__.get("time"), A.time.__dict__.get("sleep"))
        self.env_saved = dict(os.environ)
        self.tmp = tempfile.mkdtemp(prefix="agentctl-sim-")
        self.repo = os.path.realpath(os.path.join(self.tmp, "movies"))
        os.makedirs(os.path.join(self.repo, "scripts", "hooks"))
        self.hook = os.path.join(self.repo, "scripts", "hooks", "pre-push")
        with open(self.hook, "w") as f:
            f.write(HOOK_TEXT)
        os.chmod(self.hook, 0o755)
        self.heads[self.repo] = ROOT
        os.environ.update(AGENTCTL_STATE_ROOT=os.path.join(self.tmp, "state"), AGENTCTL_ROLE="server",
                          AGENTCTL_CHECK_TREES=str(self.trees), AGENTCTL_CHECK_LEASE_TTL="600",
                          AGENTCTL_STATS_URL="http://sim", AGENTCTL_STATS_SETTLE="0",
                          AGENTCTL_INBOX_DIR=os.path.join(self.tmp, "inbox"),
                          AGENTCTL_CHECKS_DIR=os.path.join(self.tmp, "checks"))
        self.saved_wdir = A.WAITING_DIR
        A.WAITING_DIR = os.path.join(self.tmp, "waiting")
        for c in A.PROCESS_CACHES:
            c.clear()
        clock = self.clock = [1_800_000_000.0]
        A.time.time = lambda: clock[0]
        A.time.sleep = lambda s: None
        A.os = SimOs(self)
        A.git, A.git_ok = self.git, self.git_ok
        A.fetch_main = lambda repo: (True, "")
        A.apply_entry = self.apply_entry
        A.check_tree = self.check_tree
        A.range_paths = self.range_paths
        A.named_reader = lambda *a, **k: None
        A.cherry_landed = self.cherry_landed
        A.change_landed = self.change_landed
        A.hook_query = self.hook_query
        A.commit_fixes = self.commit_fixes
        A.gate_reds = lambda repo: set(self.reds)
        A.gov_class_of = lambda repo, commits, reds: "check"
        A.blame_land_refusal = lambda *a, **k: None
        A.run_entry_checks = self.run_entry_checks
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
        A.stats_echo = lambda recs, out=None: None
        self.d = A.waiting_state(self.repo)[0]
        self.saved_db = A.Store.db
        conns = {}
        A.Store.db = lambda st: conns.setdefault(st.path, None) or conns.__setitem__(st.path, KeptConnection(st.path)) \
            or conns[st.path]
        self.conns = conns

    def uninstall(self):
        for t in self.tasks:  # an executor still blocked: unwound, its leases released
            if not t.done:
                t.abort = True
                t.go.set()
                t.thread.join(10)
        for n, v in self.saved.items():
            setattr(A, n, v)
        A.Store.db = self.saved_db
        for k in self.conns.values():
            k.c.close()
        A.WAITING_DIR = self.saved_wdir
        for name, v in zip(("time", "sleep"), self.saved_clock):
            if v is None:
                A.time.__dict__.pop(name, None)
            else:
                setattr(A.time, name, v)
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
            kind = "identity" if r < ident else "nobatch" if r < ident + 0.1 else "plain"
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
        fixes = {str(self.red_run)} if kind == "redfix" and self.reds else set()
        bad = spec.get("bad")
        if bad is None:
            bad = not self.calm and self.rng.random() < self.profile.get("bad", 0.12)
        conflicts = set()
        if spec.get("conflict") or (not self.calm and spec.get("conflict") is None
                                    and self.rng.random() < self.profile.get("conflict", 0.1)):
            # it conflicts with whatever lands next that touches its module (picked at its arrival: a later patch)
            conflicts = {"*future*"}
        base = self.main
        pids, head = [], base
        for k in range(self.rng.choice((1, 1, 2))):
            pid = f"p{cid}{letters(k)}"
            self.patches[pid] = Patch(pid, cid, paths if k == 0 else [paths[0]], fixes if k == 0 else (),
                                      bad=bad and k == 0, conflicts=conflicts)
            pids.append(pid)
            head = self.child(head, pid)
        ch = Change(cid, worktree, kind, pids, base, head, self.chain(base, head), self.tick,
                    no_batch=kind == "nobatch")
        ch.outcome = spec.get("outcome")
        self.changes.append(ch)
        return ch

    def submit(self, ch):
        fields = {"worktree": ch.worktree, "branch": os.path.basename(ch.worktree), "head": ch.head,
                  "commits": list(ch.commits), "fixes": sorted(set().union(*(self.patches[p].fixes for p in ch.patches))),
                  "keep_worktree": False, "no_batch": ch.no_batch, "claude_pid": None, "session": f"s-{ch.worktree}",
                  "base": ch.base, "host": "sim", "lander": {}, "commit_keys": {}}
        e, _ahead = A.queue_change(self.d, fields)
        ch.eid = e["id"]
        self.by_eid[e["id"]] = ch

    def agents(self):
        """This tick's arrivals and agent reactions: re-lands after NOT LANDED, supersedes, cancels."""
        while self.arrivals and self.arrivals[0][0] <= self.tick:
            _t, spec = self.arrivals.pop(0)
            if spec.get("kind") == "redfix" and not self.reds:
                self.open_red()
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
                                             hold_why=None))

    def entry_by_id(self, eid):
        return A.read_record(os.path.join(self.d, "entries", f"{eid}.json"))

    def entry(self, ch):
        return A.read_record(os.path.join(self.d, "entries", f"{ch.eid}.json")) if ch.eid else None

    # ---------------------------------------------------------------- the forge
    def open_red(self):
        self.red_run += 1
        self.reds = {str(self.red_run), f"9{self.red_run}000"}
        self.red_since = self.tick
        self.red_clears_at = None

    def forge(self):
        if self.red_clears_at is not None and self.tick >= self.red_clears_at:
            self.reds, self.red_since, self.red_clears_at = set(), None, None
        if self.tick >= self.drain_from and self.reds and self.red_clears_at is None:
            self.red_clears_at = self.tick + 1  # the drain: the gate goes green
        if self.calm or self.tick >= self.drain_from:
            return
        if self.script is None and not self.reds and self.rng.random() < self.profile.get("red", 0.02):
            self.open_red()
            if self.rng.random() < 0.8:  # its fixer lands a fix
                self.arrivals.append((self.tick + self.rng.randint(1, 4), {"kind": "redfix"}))
                self.arrivals.sort(key=lambda x: x[0])
            else:  # a later run goes green by itself (an infra red)
                self.red_clears_at = self.tick + self.rng.randint(10, 30)
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
                if q.cid != p.cid and mods & {x.split("/")[0] for x in q.paths}:
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
        before = {e["id"]: dict(e) for e in A.queued_entries(self.d)}
        self.known_entries = {e["id"] for e in A.waiting_entries(self.d, open_only=False)}
        reds = set(self.reds)
        A.project_changes(self.d)
        A.close_landed_changes(self.repo, self.d)
        for x in A.tick_changes(self.repo, self.d):
            A.wlog(self.d, f"tick {self.tick}: {x}")
        for x in A.tick_batch(self.repo, self.d):
            A.wlog(self.d, f"tick {self.tick}: {x}")
        if self.tick % 10 == 0:
            A.stats_pass(self.repo, self.d)
        self.check_invariants(before, reds)
        self.tick += 1
        self.clock[0] += TICK

    keep = os.environ.get("AGENTCTL_TEST_SIM_KEEP")

    def run(self, max_ticks=None, drain=400):
        """The arrivals, then a drain (the gate green, no new arrivals) until every change is decided or the bound."""
        self.install()
        try:
            last = max([t for t, _s in self.arrivals] + [0])
            self.drain_from = last + 1
            limit = (max_ticks or last + 1) + drain
            while self.tick < limit and not self.violations:
                self.step()
                if self.tick > self.drain_from and self.quiet():
                    break
            if not self.violations:
                self.final_checks()
        finally:
            self.uninstall()
        return self

    def quiet(self):
        return not self.reds and not [e for e in A.waiting_entries(self.d, open_only=False)
                                      if e.get("status") not in A.ENTRY_FINAL] and not self.arrivals

    # ---------------------------------------------------------------- the invariants
    def check_invariants(self, before, reds):
        st = A.Store()
        items = st.items("change")
        open_ = [it for it in items if not A.machine_state("change", it["state"]).terminal]
        # 1. ONE OPEN ITEM PER CHANGE (worktree + head): the head as the change (its last patch), whichever sha a
        # rebase gave it
        seen = {}
        for it in open_:
            dd = it.get("data") or {}
            k = (dd.get("worktree"), self.commits.get(dd.get("head"), (None, dd.get("head")))[1])
            if k in seen:
                self.violate(f"two open items for one change {os.path.basename(k[0] or '?')} ({k[1]}): "
                             f"{seen[k]} and {it['id']}")
            seen[k] = it["id"]
        # 2. NEVER TWO PUSHES OF ONE CHANGE: forge_push (a patch twice on main)
        # 3. A RED FIX IS NEVER BEHIND A BATCH OR A NON-RED CHANGE
        entries = {e["id"]: e for e in A.waiting_entries(self.d, open_only=False)}
        redfix_holder = A.lease_live(f"slot/check/{os.path.basename(self.d.rstrip('/'))}/{A.RED_FIX_TREE}")
        for eid, e in entries.items():
            if e.get("status") in A.ENTRY_FINAL or not A.red_fix(e, reds) or reds != self.reds:
                continue
            if e.get("backlog_batch"):
                self.violate(f"red fix {eid} is a member of batch {e['backlog_batch']}")
            due = not float(e.get("retry_after") or 0) > A.time.time()
            if e["status"] == "queued" and due and not A.checker_alive(eid) \
                    and before.get(eid, {}).get("status") == "queued" \
                    and not float(before[eid].get("retry_after") or 0) > A.time.time() - TICK:
                other = redfix_holder and eid not in json.dumps(redfix_holder.get("meta") or {})
                if not other:
                    self.violate(f"red fix {eid} queued and not started: held behind other work")
            if e["status"] == "checked" and due and before.get(eid, {}).get("status") == "checked":
                self.violate(f"red fix {eid} checked and not pushed: its push held behind other work")
        # 4. A BATCH REACHES LANDED, BISECTING OR HELD WITHIN N TICKS (counted in its assembling/checking/pushing ticks)
        for bt in st.items("batch"):
            if A.machine_state("batch", bt["state"]).terminal or bt["state"] in ("bisecting", "held", "blocked-red"):
                continue
            n = self.batch_seen[bt["id"]] = self.batch_seen.get(bt["id"], 0) + 1
            if n > self.profile.get("batch_bound", 40):
                self.violate(f"batch {bt['id']} {bt['state']} for {n} ticks without landing, bisecting or holding")
        for ch in self.changes:
            if ch.cid not in self.done_at and (self.entry(ch) or {}).get("status") in A.ENTRY_FINAL:
                self.done_at[ch.cid] = self.tick
        # 5. THE OLDEST CHANGE LANDS WITHIN A BOUND WHEN NOTHING FAILS (a calm seed)
        if self.calm:
            bound = self.profile.get("land_bound", 60)
            for ch in self.changes:
                e = self.entry(ch)
                if e and e.get("status") not in A.ENTRY_FINAL and self.tick - ch.arrived > bound:
                    self.violate(f"change {ch.eid} ({ch.kind}) unlanded {self.tick - ch.arrived} ticks after it arrived "
                                 f"with nothing failing (bound {bound})")
                    break
        # 6. NO CHANGE LOST: every entry has its item, every item its entry, and they agree on decided states
        by_id = {it["id"]: it for it in items}
        for eid, e in entries.items():
            it = by_id.get(eid)
            if e.get("status") == "joined":
                continue
            if it is None:
                if eid in self.known_entries:  # one written this tick gets its item at the next projection
                    self.violate(f"entry {eid} ({e.get('status')}) has no store item")
                continue
            if e.get("status") in A.ENTRY_FINAL and not A.machine_state("change", it["state"]).terminal:
                self.violate(f"entry {eid} is {e['status']} but its item is {it['state']}")
            if A.machine_state("change", it["state"]).terminal and e.get("status") not in A.ENTRY_FINAL \
                    and A.ENTRY_STATE.get(e.get("status")) != it["state"]:
                self.violate(f"item {eid} is {it['state']} but its entry is {e.get('status')}")
        for it in items:
            if it["id"] not in entries:
                self.violate(f"item {it['id']} ({it['state']}) has no queue entry: stranded")
        for ch in self.changes:
            e = self.entry(ch)
            if e and e.get("status") == "landed":
                on = self.patches_of(self.main)
                if not set(ch.patches) <= on:
                    self.violate(f"change {ch.eid} LANDED but {sorted(set(ch.patches) - on)} not on main: lost")

    def final_checks(self):
        st = A.Store()
        for ch in self.changes:
            e = self.entry(ch)
            if e is None:
                self.violate(f"change {ch.cid} was never queued")
                continue
            if e.get("status") not in A.ENTRY_FINAL:
                self.violate(f"change {ch.eid} still {e.get('status')} after the drain (gate green, nothing arriving)")
            if e.get("status") == "cancelled" and not ch.cancelled:
                self.violate(f"change {ch.eid} cancelled, but no agent cancelled it")
            if e.get("status") == "superseded" and not any(o.worktree == ch.worktree and o.cid > ch.cid for o in self.changes):
                self.violate(f"change {ch.eid} superseded by nothing newer")
        # 7. STATS: exactly one landing record per decided change, `landed` for each landed one
        for _ in range(3):
            A.stats_pass(self.repo, self.d)
        recs = {}
        for r in self.stats:
            if r.get("record") == "agentctl.landing":
                recs.setdefault(r["change_id"], []).append(r)
        for it in st.items("change"):
            n = len(recs.get(it["id"], []))
            if A.machine_state("change", it["state"]).terminal and it["state"] in A.CHANGE_EXITS and n != 1:
                self.violate(f"change {it['id']} ({it['state']}): {n} landing records, not one")
            if it["state"] == "landed" and n == 1 and recs[it["id"]][0].get("outcome") != "landed":
                self.violate(f"change {it['id']} landed, its record says {recs[it['id']][0].get('outcome')}")
        landed_entries = {ch.eid for ch in self.changes if (self.entry(ch) or {}).get("status") == "landed"}
        landed_items = {it["id"] for it in st.items("change") if it["state"] == "landed"}
        if landed_entries != landed_items:
            self.violate(f"landed entries {sorted(landed_entries ^ landed_items)} disagree with their items")


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


def replay_days():
    with open(os.path.join(HERE, "fixtures", "landing-replay-7d.json")) as f:
        return json.load(f)["days"]
