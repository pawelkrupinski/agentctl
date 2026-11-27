"""Tests for ~/bin/agentctl: python3 -m unittest discover -s ~/bin/tests -v"""
from unittest import mock
import contextlib, re, datetime as dt, io, json, os, subprocess, sys, tempfile, threading, time, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import harness  # noqa: E402  -- FIRST: HOME, PATH, git config, the per-test timeout, the clock rule (tests/harness.py)

# EVERY TEMP DIR OF A TEST RUN UNDER ONE, removed when the run ends (the owner, 2026-10-29: 3,009 leaked `agentctl-*`
# dirs, a full disk): tempfile's own dir, and TMPDIR for every child process; a killed run's leftovers the processor
# removes after an hour (upkeep). Short, so a socket path inside stays <= 104 bytes. /tmp holds only the socket dirs.
import atexit as _atexit, shutil as _shutil  # noqa: E401
if not os.environ.get("AGENTCTL_TEST_RUN_TMP"):
    os.environ["AGENTCTL_TEST_RUN_TMP"] = tempfile.mkdtemp(prefix="agentctl-t-")
    os.environ["TMPDIR"] = os.environ["AGENTCTL_TEST_RUN_TMP"]
    _atexit.register(_shutil.rmtree, os.environ["AGENTCTL_TEST_RUN_TMP"], True)
tempfile.tempdir = os.environ["AGENTCTL_TEST_RUN_TMP"]
TOOL = os.environ.get("AGENTCTL_UNDER_TEST") or os.path.join(HERE, "..", "agentctl")
os.environ.setdefault("AGENTCTL_CONFIG", "-")  # no config file of the machine's: the code's defaults, then the test's env
from agentctl_view import load as _load_view  # noqa: E402
from fake_clock import FakeClock  # noqa: E402
A = _load_view(TOOL)  # the tests' view of every module (tests/agentctl_view.py): A.<name> reads and patches them all
# THE GH TALLY NEVER PRINTS INTO A TEST (2026-11-19, a042390's CI): a worker process alive past GH_TALLY_SECONDS (a
# suite over 300 s on a slow runner) printed its `agentctl: github calls (pid ...)` tally into a fixer land's captured
# output, and three test_red_fix_lands_fast tests read it as the land's last line. test_forge's tally test sets its own.
A.GH_TALLY_SECONDS = 10 ** 9


# The clock and the process runner are passed in (FakeClock, FakeRunner), never the stdlib modules patched: faking the
# shared stdlib `time.sleep` once also replaced the sleep inside subprocess's `wait(timeout=...)` poll -- a die-on-sleep
# fake killed the checker inside `git checkout` (test_queue's test_superseding_an_entry_cancels_its_run, 2026-10-29).
harness.register(A)


class approved:
    """`with approved("<policy id>"):` -- an agent-proposed rule of policy.py POLICIES as if the owner had approved it,
    so a test reaches what it does once approved (unapproved, it only warns)."""

    def __init__(self, pid):
        self.p = A.policy(pid)

    def __enter__(self):
        self.saved, self.p.decided = self.p.decided, "approved in a test"
        return self.p

    def __exit__(self, *exc):
        self.p.decided = self.saved


def code_source():
    """ALL of agentctl's code as one text: the entry, then lib/agentctl's MODULES in import order (a test that reads
    the source -- "only transition writes a state" -- must see every module)."""
    lib = os.path.join(os.path.dirname(os.path.realpath(TOOL)), "lib", "agentctl")
    texts = [open(TOOL).read()] + [open(os.path.join(lib, f"{p}.py")).read() for p in A.MODULES]
    return "\n".join(texts)

# No test writes to the real inbox (~/.cache/agentctl/inbox): every land / watch / wait a test runs writes its verdict
# here instead (test_inbox sets its own per test).
# THE STATE ROOT (2026-10-28: tests wrote ~200 waiting/ dirs, the sweep reports and 176 checks/runs.jsonl rows into the
# real ~/.cache/agentctl): every agentctl path of every test, and of every child process, lives under a temp root.
os.environ.setdefault("AGENTCTL_STATE_ROOT", tempfile.mkdtemp(prefix="agentctl-state-test-"))
os.environ.setdefault("AGENTCTL_MOVIES_REPO", os.path.join(os.environ["AGENTCTL_STATE_ROOT"], "no-movies-repo"))
os.environ.setdefault("AGENTCTL_CLAUDE_PROJECTS", os.path.join(os.environ["AGENTCTL_STATE_ROOT"], "claude-projects"))  # never the real sessions
os.environ.setdefault("AGENTCTL_UNKNOWN_GRACE", "0")  # an UNKNOWN opens at its first sighting (BlipTest holds the grace)
os.environ.setdefault("AGENTCTL_NOTE_PRECHECK_WAIT", "0")  # a precheck's note is tried once (test_stats retries it)
os.environ.setdefault("AGENTCTL_ASK_HOLD", "0")  # the owner is told at once (OwnerHoldTest holds the question)
os.environ.setdefault("AGENTCTL_BIN_REPO", "-")  # no test makes worktrees of the agentctl repository itself
# THE STORE IS IN-PROCESS (the server role: the only one with a database of its own, since the laptop's local store
# went, docs/simplification-2026-10.md item 2), and a test playing the laptop's sync without a URL of its own meets a
# closed port -- never ci-runner's real store through the tunnel.
os.environ.setdefault("AGENTCTL_ROLE", "server")
os.environ.setdefault("AGENTCTL_STORE_DEFAULT_URL", "http://127.0.0.1:9")
# THE RED FAMILIES ARE THE TESTS' OWN (never ~/bin/red-families.json, which the owner edits): convergence, no owner.
os.environ.setdefault("AGENTCTL_RED_FAMILIES", os.path.join(HERE, "fixtures", "red-families.json"))


def _real_cache_marks():
    """What a test leak into the real cache would add: test-repo dirs under waiting/, test rows in checks/runs.jsonl,
    a test repo in a sweep report."""
    root = os.path.join(harness.REAL_HOME, ".cache", "agentctl")
    temp = ("var_folders", "_tmp_", "agentctl-", "/var/folders/", "/tmp/")
    marks = set()
    w = os.path.join(root, "waiting")
    if os.path.isdir(w):
        marks |= {f"waiting/{d}" for d in os.listdir(w) if any(x in d for x in temp)}
    try:
        with open(os.path.join(root, "checks", "runs.jsonl"), errors="replace") as f:
            marks |= {f"runs.jsonl:{l[:200]}" for l in f if "/var/folders/" in l or "/tmp/" in l
                      or re.search(r'"label": "[a-z]+ repo-wt-', l)}  # the tests' repos are named repo-wt-*
    except OSError:
        pass
    sw = os.path.join(root, "sweep")
    for n in os.listdir(sw) if os.path.isdir(sw) else []:
        try:
            if "/var/folders/" in open(os.path.join(sw, n), errors="replace").read():
                marks.add(f"sweep/{n}")
        except OSError:
            pass
    return marks


_MARKS_BEFORE = _real_cache_marks()


def _guard_real_cache():
    leaked = _real_cache_marks() - _MARKS_BEFORE
    if leaked:
        sys.stderr.write("TEST LEAK into the real ~/.cache/agentctl:\n  " + "\n  ".join(sorted(leaked)[:20]) + "\n")
        sys.stderr.flush()
        os._exit(3)


import atexit  # noqa: E402
atexit.register(_guard_real_cache)

os.environ.setdefault("AGENTCTL_INBOX_DIR", tempfile.mkdtemp(prefix="agentctl-inbox-test-"))
# ...nor reaps this machine's stuck sbt servers (orphans / sweep): test_sbt_reaper drives the rules on fakes.
os.environ.setdefault("AGENTCTL_SBT_REAP", "0")
# ...nor listens on this machine's token hand-off sockets (a short dir: a socket path is <= 104 bytes).
if not os.environ.get("AGENTCTL_TEST_RUN_SOCK"):  # the run's socket dirs, under one short /tmp dir, removed at its end
    os.environ["AGENTCTL_TEST_RUN_SOCK"] = tempfile.mkdtemp(dir="/tmp", prefix="agentctl-sock-t-")
    _atexit.register(_shutil.rmtree, os.environ["AGENTCTL_TEST_RUN_SOCK"], True)
os.environ.setdefault("AGENTCTL_SOCK_DIR", tempfile.mkdtemp(dir=os.environ["AGENTCTL_TEST_RUN_SOCK"], prefix="s-"))
# ...nor reads or writes this machine's red claims and fixers, nor scans its worktrees, nor patrols from a test's
# processor, nor ever finds the real `claude` CLI.
os.environ.setdefault("AGENTCTL_REDS_DIR", tempfile.mkdtemp(prefix="agentctl-reds-test-"))
os.environ.setdefault("AGENTCTL_RED_OLD_RUN", "1e12")  # the fixtures' fixed dates never age into old reds
os.environ.setdefault("AGENTCTL_RED_REPO", "-")
os.environ.setdefault("AGENTCTL_RED_PATROL", "0")
os.environ.setdefault("AGENTCTL_CLAUDE", "/nonexistent/claude")
# ...nor takes this machine's pre-commit check slots, nor writes its measurements or cap.
os.environ.setdefault("AGENTCTL_CHECKS_DIR", tempfile.mkdtemp(prefix="agentctl-checks-test-"))
# the governor of a test is its own: its own tickets, and a machine with room (never this laptop's memory or load)
os.environ.setdefault("AGENTCTL_GOV_DIR", tempfile.mkdtemp(prefix="agentctl-gov-test-"))
os.environ.setdefault("AGENTCTL_GOV_FREE_PROBE", "echo 64")
os.environ.setdefault("AGENTCTL_GOV_PRESSURE_PROBE", "echo 10")
os.environ.setdefault("AGENTCTL_GOV_LOAD_PROBE", "echo 1")
# land's old in-process path, unless a test of the queue asks for it
os.environ.setdefault("AGENTCTL_QUEUE", "0")
os.environ.setdefault("KINOWO_SBT_WARM_STATE", tempfile.mkdtemp(prefix="agentctl-sbt-warm-test-"))

ENV = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
# (GIT_CONFIG_GLOBAL / GIT_CONFIG_NOSYSTEM: the harness's gitconfig, already in os.environ)


class StallSamples:
    """The samples a stall verdict reads (run_watched's CPU and clock), driven by the test, never by the machine's load:
    `busy` adds 5 CPU seconds at every sample (a busy, silent process); otherwise no CPU at all, and the clock stands
    still until `hung` (a file the fake process writes once it hangs) exists, then jumps 60 s at every sample."""

    def __init__(self, busy=False, hung=None):
        self.busy, self.hung, self.cpu_s, self.now = busy, hung, 0.0, 0.0

    def cpu(self, pgid):
        self.cpu_s += 5.0 if self.busy else 0.0
        return self.cpu_s

    def clock(self):
        if self.hung is None or os.path.exists(self.hung):
            self.now += 60.0
        return self.now

    def install(self, case):
        """Patched into agentctl for the test case (run_watched's defaults)."""
        saved = (A.group_cpu_seconds, A.stall_clock)
        A.group_cpu_seconds, A.stall_clock = self.cpu, self.clock
        case.addCleanup(lambda: (setattr(A, "group_cpu_seconds", saved[0]), setattr(A, "stall_clock", saved[1])))
        return self


def sh(cwd, *args, input=None):
    r = subprocess.run(args, cwd=cwd, capture_output=True, text=True, env=ENV, input=input)
    if r.returncode:
        raise AssertionError(f"{args}: {r.stderr}")
    return r.stdout.strip()


_REPO_TEMPLATE = []


def seed_gating_checks():
    """The state root's kept copy of movies' gating checks registry (ciwhy.py gating_checks): for a test with no clone
    and no forge to read it from, the registry as last read -- THE GATE's three workflows, by name."""
    rows = A.gating_checks_parse(GATING_CHECKS_TEMPLATE)
    A.write_json(A.gating_checks_path(), {A.RED_PRIMARY: {"main": "seeded", "rows": rows,
                                                          "names": {f: n for f, n in GATE_WORKFLOW_FILES}}})
GATE_WORKFLOW_FILES = (("main.yml", "Main"), ("identity-model-convergence.yml", "Identity model convergence"),
                       ("identity-takes.yml", "Identity takes"))
# the gate jobs the suite's fixtures fail, each proven: a failed job with no row is unproven (report only), as the hook
# reads it -- a fixture red meant to gate needs its row, as on movies' main
GATE_FIXTURE_JOBS = {
    "main.yml": ("Unit tests", "test", "test (rest)"),
    "identity-model-convergence.yml": (
        "a", "b", "ablate", "check", "preflight", "timings", "convergence (united-kingdom)", "leg (spain)",
        "takes (poland)", "takes (united-kingdom)", "takes-drift-1 (poland)", "takes-drift-2 (germany)",
        "takes-drift-2 (united-kingdom)"),
    "identity-takes.yml": ("takes (poland)", "takes-drift-1 (poland)"),
}
GATING_CHECKS_TEMPLATE = "kind\tworkflow\tname\tproven\tnote\n" + "".join(
    f"workflow\t{f}\t-\t1\tTHE GATE\n" for f, _n in GATE_WORKFLOW_FILES) + "".join(
    f"job\t{f}\t{j}\t1\t\n" for f, jobs in GATE_FIXTURE_JOBS.items() for j in jobs)


def _repo_template():
    """The bare remote and its clone, built ONCE per process (~10 git calls, ~0.4 s on a loaded laptop) and copied for
    every Repo after that: the suite builds a few hundred."""
    if not _REPO_TEMPLATE:
        root = os.path.realpath(tempfile.mkdtemp(prefix="repo-tpl-"))
        bare, path = os.path.join(root, "remote.git"), os.path.join(root, "repo")
        sh(root, "git", "init", "-q", "--bare", "-b", "main", bare)
        sh(root, "git", "init", "-q", "-b", "main", path)
        sh(path, "git", "remote", "add", "origin", bare)
        for i in range(5):
            with open(os.path.join(path, f"f{i}.txt"), "w") as f:
                f.write(f"v1 {i}\n")
        # the primary keeps its red-workflows registry on main (red-repos.json `red_workflows`): no rows, no replays
        os.makedirs(os.path.join(path, ".github"))
        with open(os.path.join(path, ".github", "red-workflows.tsv"), "w") as f:
            f.write("# workflow\tverify_dispatch\treplay\twhy\n")
        # ...and its gating checks registry (ciwhy.py gate_flows): THE GATE's three workflows, each proven
        with open(os.path.join(path, ".github", "gating-checks.tsv"), "w") as f:
            f.write(GATING_CHECKS_TEMPLATE)
        sh(path, "git", "add", "-A")
        sh(path, "git", "commit", "-q", "-m", "init")
        sh(path, "git", "push", "-q", "origin", "HEAD:main")
        sh(path, "git", "fetch", "-q", "origin")
        _REPO_TEMPLATE.append((bare, path))
    return _REPO_TEMPLATE[0]


class Repo:
    """A bare 'origin' remote plus a clone with a few commits on main (a copy of the process's template)."""

    def __init__(self, root):
        self.root = root
        self.bare = os.path.join(root, "remote.git")
        self.path = os.path.join(root, "repo")
        tbare, tpath = _repo_template()
        _shutil.copytree(tbare, self.bare, symlinks=True)
        _shutil.copytree(tpath, self.path, symlinks=True)
        cfg = os.path.join(self.path, ".git", "config")
        with open(cfg) as f:
            text = f.read()
        with open(cfg, "w") as f:
            f.write(text.replace(f"url = {tbare}\n", f"url = {self.bare}\n"))

    def write(self, rel, text, at=None):
        p = os.path.join(at or self.path, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write(text)

    def commit(self, msg, at=None):
        sh(at or self.path, "git", "add", "-A")
        sh(at or self.path, "git", "commit", "-q", "-m", msg)
        return sh(at or self.path, "git", "rev-parse", "HEAD")

    def push(self, at=None, ref="HEAD:main"):
        sh(at or self.path, "git", "push", "-q", "origin", ref)
        sh(self.path, "git", "fetch", "-q", "origin")

    def worktree(self, name, base="origin/main", branch=None):
        p = os.path.join(self.root, name)
        sh(self.path, "git", "worktree", "add", "-q", *(["-b", branch] if branch else ["--detach"]), p, base)
        return p


def make_old(path):
    """Make a worktree look idle (its gitdir files untouched for a day)."""
    gd = subprocess.run(["git", "-C", path, "rev-parse", "--absolute-git-dir"], capture_output=True, text=True).stdout.strip()
    old = A.epoch_now() - 86400
    for f in ("HEAD", "index", "logs/HEAD", "ORIG_HEAD"):
        fp = os.path.join(gd, f)
        if os.path.exists(fp):
            os.utime(fp, (old, old))


def put_change(st, d, e):
    """Change e as the queue would have written it (docs/design/one-change-model.md step 4: the item is the change):
    its record (ChangeRecords.save), its item in e's status -- a fixture standing for the writers' history."""
    A.ChangeRecords(st, d).save(e)
    want = A.change_state_of(e.get("status"))
    if want and st.item("change", e["id"])["state"] != want:
        c = st.db()
        try:
            c.execute("UPDATE items SET state=? WHERE type='change' AND id=?", (want, e["id"]))
        finally:
            c.close()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.scratch = os.path.join(self.tmp.name, "scratch")
        os.environ["AGENTCTL_SCRATCH"] = self.scratch
        os.environ["AGENTCTL_OWNERS_DIR"] = os.path.join(self.tmp.name, "owners")
        for k in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_NOSYSTEM"):
            os.environ[k] = ENV[k]
        self.r = Repo(os.path.realpath(self.tmp.name))
        self.saved_root = os.environ.get("AGENTCTL_STATE_ROOT")  # a store per test: items and leases never leak across
        os.environ["AGENTCTL_STATE_ROOT"] = os.path.join(self.tmp.name, "state")
        for c in A.PROCESS_CACHES:  # every part's process-wide cache: per process, so per test (core.process_cache)
            c.clear()
        self.clock = FakeClock()  # the clock run_tool hands agentctl's main: its sleeps return at once

    def tearDown(self):
        os.environ["AGENTCTL_STATE_ROOT"] = self.saved_root
        self.tmp.cleanup()

    def run_tool(self, *argv, stdin=None, cwd=None, clock=None):
        out, errb = io.StringIO(), io.StringIO()
        old_cwd = os.getcwd()
        old_stdin = sys.stdin
        try:
            if cwd:
                os.chdir(cwd)
            sys.stdin = io.StringIO(stdin or "")
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(errb):
                rc = A.main(list(argv), clock=clock or self.clock)
        finally:
            os.chdir(old_cwd)
            sys.stdin = old_stdin
        return rc, out.getvalue() + errb.getvalue()

    def deleted_log(self):
        p = os.path.join(self.scratch, "agentctl-deleted.log")
        return open(p).read() if os.path.exists(p) else ""  # noqa


class ProcessCwdsTest(Base):
    def test_on_linux_the_cwds_are_read_from_proc_with_no_lsof(self):
        """Review 2026-11-18: 11 tests failed in python:3.14 (Debian, no lsof): `cleanup` and `orphans` crashed with
        FileNotFoundError on a Linux host without lsof. Linux has every process's cwd in /proc/<pid>/cwd."""
        proc = os.path.join(self.tmp.name, "proc")
        wt = self.r.worktree("wt-busy", branch="busy")
        for pid, cwd in ((101, wt), (102, "/etc")):
            os.makedirs(os.path.join(proc, str(pid)))
            os.symlink(cwd, os.path.join(proc, str(pid), "cwd"))
        os.makedirs(os.path.join(proc, "self"))
        saved = A.PROC_ROOT, A.run_cmd
        self.addCleanup(lambda: (setattr(A, "PROC_ROOT", saved[0]), setattr(A, "run_cmd", saved[1])))
        A.PROC_ROOT = proc

        def no_lsof(args, **kw):
            if args[0] == "lsof":
                raise FileNotFoundError(2, "No such file or directory", "lsof")
            return saved[1](args, **kw)
        A.run_cmd = no_lsof
        self.assertEqual(A.lsof_cwds([101, 102, 103]), {101: wt}, "allowed paths only; a gone pid is skipped")


class CleanupTest(Base):
    def cleanup(self, *extra):
        return self.run_tool("cleanup", "--execute", "--repo", self.r.path, "--no-remote", *extra)

    def test_removes_merged_worktree_and_branch(self):
        wt = self.r.worktree("wt-merged", branch="feat")
        self.r.write("f1.txt", "feat\n", at=wt)
        sha = self.r.commit("feat", at=wt)
        self.r.push(at=wt)
        make_old(wt)
        rc, out = self.cleanup()
        self.assertEqual(rc, 0, out)
        self.assertFalse(os.path.exists(wt))
        self.assertNotIn("feat", sh(self.r.path, "git", "branch", "--list", "feat"))
        log = self.deleted_log()
        self.assertIn(f"worktree\trepo\t{wt}\t{sha}", log)
        self.assertIn(f"branch\trepo\tfeat\t{sha}", log)

    def test_removes_patch_equivalent_worktree(self):
        wt = self.r.worktree("wt-cherry", branch="cherry")
        self.r.write("f2.txt", "cherry\n", at=wt)
        sha = self.r.commit("cherry", at=wt)
        # main moves, then the same patch lands there with a different sha
        self.r.write("f4.txt", "other\n")
        self.r.commit("other")
        sh(self.r.path, "git", "cherry-pick", sha)
        self.r.push()
        make_old(wt)
        rc, out = self.cleanup()
        self.assertIn("patch-equivalent", out)
        self.assertFalse(os.path.exists(wt))

    def test_refuses_unmerged_worktree(self):
        """Mutation-style: the same merged setup is deleted; one extra unpushed commit flips it to kept."""
        wt = self.r.worktree("wt-unmerged", branch="work")
        self.r.write("f1.txt", "pushed\n", at=wt)
        self.r.commit("pushed", at=wt)
        self.r.push(at=wt)
        self.r.write("f3.txt", "NOT pushed\n", at=wt)  # the mutation
        sha = self.r.commit("local only", at=wt)
        make_old(wt)
        rc, out = self.cleanup("-v")
        self.assertIn("unmerged (1 commit(s)", out)
        self.assertTrue(os.path.isdir(wt))
        self.assertEqual(sh(self.r.path, "git", "rev-parse", "work"), sha)
        self.assertEqual(self.deleted_log(), "")
        # control: drop the mutation and the same worktree goes
        sh(wt, "git", "reset", "-q", "--hard", "HEAD~1")
        make_old(wt)
        rc, out = self.cleanup()
        self.assertFalse(os.path.exists(wt), out)

    def test_refuses_dirty_and_untracked(self):
        dirty = self.r.worktree("wt-dirty")
        self.r.write("f0.txt", "edited\n", at=dirty)
        untracked = self.r.worktree("wt-untracked")
        self.r.write("new.txt", "x\n", at=untracked)
        make_old(dirty); make_old(untracked)
        rc, out = self.cleanup("-v")
        self.assertTrue(os.path.isdir(dirty) and os.path.isdir(untracked))
        self.assertEqual(out.count("dirty ("), 2, out)
        self.assertEqual(self.deleted_log(), "")

    def test_keeps_recently_active_and_main(self):
        fresh = self.r.worktree("wt-fresh")
        mainwt = self.r.worktree("wt-main-branch", base="origin/main", branch="master")
        make_old(mainwt)
        rc, out = self.cleanup("-v")
        self.assertTrue(os.path.isdir(fresh))
        self.assertTrue(os.path.isdir(mainwt))
        self.assertIn("active", out)
        self.assertIn("on master", out)

    def test_an_owner_worktree_is_locked_and_kept(self):
        """2026-11-22T04:01Z: outside-gate's owner GAVE UP mid-turn, "my worktree was deleted": upkeep's cleanup removed
        it, clean and an ancestor of main between commits. An owner's worktree is locked when made, and a recorded one
        (made before the lock) when taken up again; cleanup keeps a locked tree."""
        made = A.owner_worktree(self.r.path, "fam-new", {})
        old = self.r.worktree("wt-owner-old")  # recorded by an owner from before the lock: unlocked
        self.assertEqual(A.owner_worktree(self.r.path, "fam-old", {"worktree": old}), old)
        make_old(made); make_old(old)
        rc, out = self.cleanup("-v")
        self.assertTrue(os.path.isdir(made), out)
        self.assertTrue(os.path.isdir(old), out)
        self.assertEqual(out.count(": locked"), 2, out)
        self.assertEqual(self.deleted_log(), "")

    def test_a_tree_locked_or_entered_after_planning_is_kept(self):
        """A cleanup run took 30 min on 2026-11-22 (upkeep killed it): its plan's lock / cwd / activity reads went stale
        before the removals. They are read again just before each `git worktree remove`."""
        wt = self.r.worktree("wt-late")
        make_old(wt)
        plan = A.plan_worktrees
        def plan_then_lock(*a, **k):
            out = plan(*a, **k)
            subprocess.run(["git", "-C", self.r.path, "worktree", "lock", wt], check=True)
            return out
        with mock.patch.object(A, "plan_worktrees", plan_then_lock):
            rc, out = self.cleanup()
        self.assertTrue(os.path.isdir(wt), out)
        self.assertIn("locked since planning; kept", out)
        self.assertEqual(self.deleted_log(), "")
        subprocess.run(["git", "-C", self.r.path, "worktree", "unlock", wt], check=True)
        with mock.patch.object(A, "process_cwds", lambda: {4242: wt}), \
                mock.patch.object(A, "plan_worktrees",
                                  lambda *a, **k: [("delete", wt, None, sh(wt, "git", "rev-parse", "HEAD"), "ancestor")]):
            rc, out = self.cleanup()
        self.assertTrue(os.path.isdir(wt), out)
        self.assertIn("in use by pid [4242] since planning; kept", out)

    def test_a_cleanup_near_its_deadline_begins_no_removal_and_says_what_it_left(self):
        # 2026-11-22: the upkeep's cleanup ran past its 1800 s bound at 03:58Z and was killed mid `git worktree remove`.
        wt = self.r.worktree("wt-late", branch="late")
        self.r.write("f1.txt", "late\n", at=wt)
        self.r.commit("late", at=wt)
        self.r.push(at=wt)
        make_old(wt)
        rc, out = self.cleanup("--deadline", "100")  # inside the 300 s removal reserve
        self.assertTrue(os.path.isdir(wt), out)
        self.assertEqual(self.deleted_log(), "")
        self.assertIn("cleanup: stopped at its 100 s deadline: 1 left for the next pass", out)
        rc, out = self.cleanup("--deadline", "0")  # past it: no tree is even looked at
        self.assertTrue(os.path.isdir(wt), out)
        self.assertNotIn("delete worktree", out)
        make_old(wt)  # the reads above refreshed its index
        rc, out = self.cleanup("--deadline", "1200")  # control: time enough, it goes
        self.assertFalse(os.path.exists(wt), out)
        self.assertRegex(out, r"cleanup: \d+ s \(repo \d+ s\)")

    def test_a_tree_whose_git_status_overruns_its_bound_is_kept_undecided(self):
        wt = self.r.worktree("wt-slow", branch="slow")
        self.r.write("f1.txt", "slow\n", at=wt)
        self.r.commit("slow", at=wt)
        self.r.push(at=wt)
        make_old(wt)
        os.environ["AGENTCTL_CLEANUP_ITEM_TIMEOUT"] = "0.000001"
        try:
            rc, out = self.cleanup("-v")
        finally:
            os.environ.pop("AGENTCTL_CLEANUP_ITEM_TIMEOUT", None)
        self.assertTrue(os.path.isdir(wt), out)
        self.assertIn("undecided: git status took over", out)
        self.assertEqual(self.deleted_log(), "")

    def test_a_fetch_that_timed_out_skips_the_other_repos_fetches(self):
        # 2026-11-22 03:28Z: a lossy uplink, https remotes, one fetch after another each waiting out its bound
        other = self.r.worktree("wt-other-repo", branch="other")
        calls, saved = [], A.bounded_fetch
        A.bounded_fetch = lambda repo, *extra, refs=(): calls.append(repo) or "timed out after 180 s"
        try:
            rc, out = self.run_tool("cleanup", "--execute", "--repo", self.r.path, "--repo", other, "--no-remote")
        finally:
            A.bounded_fetch = saved
        self.assertEqual((rc, calls), (1, [self.r.path]), out)
        self.assertIn("not fetched -- repo's fetch timed out after 180 s", out)

    def test_dry_run_deletes_nothing(self):
        wt = self.r.worktree("wt-dry")
        make_old(wt)
        rc, out = self.run_tool("cleanup", "--dry-run", "--repo", self.r.path, "--no-remote")
        self.assertIn("would delete", out)
        self.assertTrue(os.path.isdir(wt))
        self.assertEqual(self.deleted_log(), "")

    def test_measure_decisions(self):
        n = dt.datetime(2026, 10, 23, 12, tzinfo=dt.timezone.utc)
        recent, old = n - dt.timedelta(hours=2), n - dt.timedelta(hours=30)
        d = A.measure_decision
        self.assertEqual(d("measure/a", "s", recent, [{"status": "completed"}], n)[0], "delete")
        self.assertEqual(d("measure/a", "s", old, [{"status": "completed"}, {"status": "in_progress", "databaseId": 9}], n)[0], "skip")
        self.assertEqual(d("measure/a", "s", old, [{"status": "queued", "databaseId": 9}], n)[0], "skip")
        self.assertEqual(d("measure/a", "s", old, [], n)[0], "delete")
        self.assertEqual(d("measure/a", "s", recent, [], n)[0], "skip")
        self.assertEqual(d("feature/a", "s", old, [], n)[0], "skip")


class PrepushTest(Base):
    def check(self, *extra, cwd=None):
        return self.run_tool("prepush-check", *extra, cwd=cwd or self.r.path)

    def test_clean_rebased_change_passes(self):
        self.r.write("f1.txt", "mine\n")
        self.r.commit("mine")
        rc, out = self.check()
        self.assertEqual(rc, 0, out)

    def test_soft_reset_squash_onto_moved_main_is_refused(self):
        wt = self.r.worktree("wt-agent", branch="agent")
        self.r.write("f1.txt", "agent edit\n", at=wt)
        self.r.commit("agent 1", at=wt)
        # meanwhile main moves: others change 3 files and add one
        for i in (2, 3, 4):
            self.r.write(f"f{i}.txt", f"someone else {i}\n")
        self.r.write("added.txt", "new on main\n")
        self.r.commit("others' work")
        self.r.push()
        sh(wt, "git", "fetch", "-q", "origin")
        sh(wt, "git", "reset", "-q", "--soft", "origin/main")  # the bad squash
        sh(wt, "git", "commit", "-q", "-m", "squash")
        rc, out = self.check(cwd=wt)
        self.assertEqual(rc, 1, out)
        for f in ("f2.txt", "f3.txt", "f4.txt", "added.txt"):
            self.assertIn(f, out)
        self.assertNotIn("f1.txt (undoes", out)
        # the right way (rebase) passes
        sh(wt, "git", "reset", "-q", "--hard", "HEAD~1")
        self.r.write("f1.txt", "agent edit\n", at=wt)
        self.r.commit("agent redo", at=wt)
        rc, out = self.check(cwd=wt)
        self.assertEqual(rc, 0, out)

    def test_allowed_set(self):
        self.r.write("src/a.txt", "a\n")
        self.r.write("docs/b.txt", "b\n")
        self.r.commit("two dirs")
        rc, out = self.check("--allow", "src/")
        self.assertEqual(rc, 1)
        self.assertIn("docs/b.txt", out)
        rc, out = self.check("--allow", "src/", "--allow", "docs/*.txt")
        self.assertEqual(rc, 0, out)

    def test_git_revert_is_exempt(self):
        for i in (1, 2, 3):
            self.r.write(f"f{i}.txt", f"bad {i}\n")
        bad = self.r.commit("bad change")
        self.r.push()
        sh(self.r.path, "git", "revert", "--no-edit", bad)
        rc, out = self.check()
        self.assertEqual(rc, 0, out)

    def test_declared_retirement_is_exempt(self):
        for i in (1, 2, 3):
            self.r.write(f"f{i}.txt", f"v2 {i}\n")
        self.r.commit("main edits three files")
        self.r.push()
        wt = self.r.worktree("wt-ret", branch="ret")
        sh(wt, "git", "rm", "-q", "f1.txt", "f2.txt", "f3.txt")
        sh(wt, "git", "commit", "-q", "-m", "retire\n\nRetires: f1.txt\nRetires: f2.txt\nRetires: f3.txt")
        rc, out = self.check(cwd=wt)
        self.assertEqual(rc, 0, out)
        # undeclared deletions still count
        sh(wt, "git", "reset", "-q", "--hard", "origin/main")
        sh(wt, "git", "rm", "-q", "f1.txt", "f2.txt", "f3.txt")
        sh(wt, "git", "commit", "-q", "-m", "retire, one only\n\nRetires: f1.txt")
        rc, out = self.check("--max-foreign", "1", cwd=wt)
        self.assertEqual(rc, 1, out)
        # issue 20261120T025500-5917bb: lane A1 deleting its own scaffolding got past this with an unnamed
        # AGENTCTL_MAX_FOREIGN=6. The refusal names the declared way, per file, and calls the cap a bypass
        self.assertIn("Retires: f2.txt; Retires: f3.txt", out)
        self.assertIn("AGENTCTL_MAX_FOREIGN raises the cap for every file and is a bypass", out)

    def _main_with_files(self, n):
        for i in range(n):
            self.r.write(f"old/m{i}.txt", "".join(f"line {i}.{j}\n" for j in range(20)))
        self.r.commit("main adds files")
        self.r.push()
        return self.r.worktree("wt-mv", branch="mv")

    def test_pure_move_of_many_files_is_not_a_revert(self):
        wt = self._main_with_files(60)
        sh(wt, "git", "mv", "old", "new")
        sh(wt, "git", "commit", "-q", "-m", "move")
        rc, out = self.check(cwd=wt)
        self.assertEqual(rc, 0, out)

    def test_move_with_small_edit_is_not_a_revert(self):
        wt = self._main_with_files(10)
        sh(wt, "git", "mv", "old", "new")
        for i in range(10):
            with open(os.path.join(wt, f"new/m{i}.txt"), "a") as f:
                f.write("one more line\n")
        sh(wt, "git", "commit", "-q", "-am", "move and edit")
        rc, out = self.check(cwd=wt)
        self.assertEqual(rc, 0, out)

    def test_deleting_foreign_files_without_a_matching_add_is_refused(self):
        wt = self._main_with_files(5)
        sh(wt, "git", "rm", "-q", "old/m0.txt", "old/m1.txt", "old/m2.txt")
        sh(wt, "git", "commit", "-q", "-m", "delete")
        rc, out = self.check(cwd=wt)
        self.assertEqual(rc, 1, out)
        self.assertIn("old/m0.txt (undoes", out)

    # 2026-11-19, movies 467e9026 (split WebReadModel into ReadModelStreams + ReadModelUpkeep), replayed from the real
    # diffs: 73aad048 added `threadPools` to WebReadModel and to every spec constructing one, a later commit `sleep`; the
    # split drops both parameters, so four controller specs come back byte-for-byte to their pre-73aad048 selves.
    MODEL_PRE = ("import tools.{DaemonExecutors, ScheduledTick, TickRecorder, TickRun}\n"
                 "class WebReadModel(\n"
                 "    ticks:             TickRecorder = TickRecorder.Unrecorded) extends Stoppable with Logging {\n"
                 "  private val scheduler       = DaemonExecutors.scheduler(\"web-read-model\")\n"
                 "}\n")
    MODEL_POOLS = ("import tools.{ThreadPools, ScheduledTick, TickRecorder, TickRun}\n"
                   "class WebReadModel(\n"
                   "    ticks:             TickRecorder = TickRecorder.Unrecorded,\n"
                   "    threadPools:       ThreadPools) extends Stoppable with Logging {\n"
                   "  private val scheduler       = threadPools.scheduler(\"web-read-model\")\n"
                   "}\n")
    MODEL_SLEEP = MODEL_POOLS.replace(
        "    threadPools:       ThreadPools) extends Stoppable with Logging {\n",
        "    threadPools:       ThreadPools,\n"
        "    sleep:             FiniteDuration => Unit) extends Stoppable with Logging {\n")
    MODEL_SPLIT = ("import tools.{ScheduledTick, TickRecorder, TickRun}\n"
                   "class WebReadModel(\n"
                   "    ticks:             TickRecorder = TickRecorder.Unrecorded) extends Logging {\n"
                   "  // the streams are ReadModelStreams, the schedule ReadModelUpkeep\n"
                   "}\n")
    SPECS = ("FilmLookupIndexSpec", "ScheduleReuseSpec", "ShareCardOgImageSpec", "SitemapRobotsControllerSpec")

    def _spec(self, name, args):
        return (f"class {name} extends AnyFlatSpec with Matchers {{\n"
                f"    val readModel  = new WebReadModel(repository, clock = _root_.tools.SpecClock.Pinned{args})\n"
                f"    readModel.reload()\n}}\n")

    def _thread_pools_then_sleep_on_main(self):
        model = "common/WebReadModel.scala"
        self.r.write(model, self.MODEL_PRE)
        for n in self.SPECS:
            self.r.write(f"web/{n}.scala", self._spec(n, ""))
        self.r.commit("specs and the read model")
        self.r.write(model, self.MODEL_POOLS)
        for n in self.SPECS:
            self.r.write(f"web/{n}.scala", self._spec(n, ", threadPools = tools.ThreadPools.Unmanaged"))
        pools = self.r.commit("Services take their schedulers and threads from the wiring's ThreadPools")
        self.r.write(model, self.MODEL_SLEEP)
        for n in self.SPECS:
            self.r.write(f"web/{n}.scala", self._spec(n, ", threadPools = tools.ThreadPools.Unmanaged, sleep = tools.Sleep.Blocking"))
        self.r.commit("WebReadModel takes the root's sleep")
        self.r.push()
        return model, pools, self.r.worktree("wt-split", branch="split")

    def _specs_back_to_before_pools(self, wt):
        for n in self.SPECS:
            self.r.write(f"web/{n}.scala", self._spec(n, ""), at=wt)

    def test_dropping_arguments_of_a_definition_the_push_changes_is_not_a_revert(self):
        model, _, wt = self._thread_pools_then_sleep_on_main()
        self.r.write(model, self.MODEL_SPLIT, at=wt)
        self.r.write("common/ReadModelUpkeep.scala",
                     "class ReadModelUpkeep(threadPools: ThreadPools) {\n"
                     "  private val scheduler       = threadPools.scheduler(\"web-read-model\")\n}\n", at=wt)
        self._specs_back_to_before_pools(wt)
        self.r.commit("WebReadModel: the upkeep schedule becomes its own class", at=wt)
        rc, out = self.check(cwd=wt)
        self.assertEqual(rc, 0, out)

    def test_the_same_spec_edits_without_the_definition_changing_are_still_a_revert(self):
        _, pools, wt = self._thread_pools_then_sleep_on_main()
        self._specs_back_to_before_pools(wt)
        self.r.commit("specs only", at=wt)
        rc, out = self.check(cwd=wt)
        self.assertEqual(rc, 1, out)
        self.assertIn(f"web/FilmLookupIndexSpec.scala (undoes {pools[:10]})", out)

    def test_restoring_the_definitions_earlier_lines_is_still_a_revert(self):
        model, pools, wt = self._thread_pools_then_sleep_on_main()
        # the squash shape: the definition is edited too, but back to what the commit replaced (plus the agent's line)
        self.r.write(model, self.MODEL_PRE.replace("}\n", "  def agentsOwn = 1\n}\n"), at=wt)
        self._specs_back_to_before_pools(wt)
        self.r.commit("squash onto a moved main", at=wt)
        rc, out = self.check(cwd=wt)
        self.assertEqual(rc, 1, out)
        self.assertIn(f"web/ScheduleReuseSpec.scala (undoes {pools[:10]})", out)

    def test_hook_mode(self):
        wt = self.r.worktree("wt-hook", branch="h")
        self.r.write("f1.txt", "x\n", at=wt)
        self.r.commit("x", at=wt)
        for i in (2, 3, 4):
            self.r.write(f"f{i}.txt", f"main {i}\n")
        self.r.commit("main moves")
        self.r.push()
        sh(wt, "git", "fetch", "-q", "origin")
        sh(wt, "git", "reset", "-q", "--soft", "origin/main")
        head = self.r.commit("squash", at=wt)
        remote = sh(wt, "git", "rev-parse", "origin/main")
        stdin = f"refs/heads/h {head} refs/heads/main {remote}\n"
        rc, out = self.run_tool("prepush-check", "--hook", "origin", "url", stdin=stdin, cwd=wt)
        self.assertEqual(rc, 1, out)
        rc, out = self.run_tool("prepush-check", "--hook", "origin", stdin=f"(delete) {A.ZERO} refs/heads/x {remote}\n", cwd=wt)
        self.assertEqual(rc, 0, out)


class RootCheckoutTest(Base):
    """Issue 20261119T140508-66d831: the movies root checkout sat 23+ commits behind main, its .claude hooks stale."""

    def advance_main(self):
        wt = self.r.worktree("wt-other", branch="other")
        self.r.write("new.txt", "new\n", at=wt)
        sha = self.r.commit("main moves", at=wt)
        self.r.push(at=wt, ref="HEAD:main")
        return sha

    def test_a_clean_root_checkout_on_main_is_fast_forwarded_and_nothing_else_is(self):
        sha = self.advance_main()
        self.assertNotEqual(sh(self.r.path, "git", "rev-parse", "HEAD"), sha)
        out = A.root_checkout_ff(self.r.path)
        self.assertEqual(sh(self.r.path, "git", "rev-parse", "HEAD"), sha, out)
        self.assertIn("fast-forwarded", out[0])
        self.assertEqual(A.root_checkout_ff(self.r.path), [], "at main: nothing to do")
        # dirty (tracked): never touched
        wt = os.path.join(self.r.root, "wt-other")
        self.r.write("new.txt", "newer\n", at=wt)
        moved = self.r.commit("main moves again", at=wt)
        self.r.push(at=wt, ref="HEAD:main")
        self.r.write("new.txt", "local edit\n")
        self.assertEqual(A.root_checkout_ff(self.r.path), [])
        self.assertNotEqual(sh(self.r.path, "git", "rev-parse", "HEAD"), moved)
        sh(self.r.path, "git", "checkout", "-q", "--", "new.txt")
        # another branch: never touched
        sh(self.r.path, "git", "checkout", "-q", "-b", "side")
        self.assertEqual(A.root_checkout_ff(self.r.path), [])
        sh(self.r.path, "git", "checkout", "-q", "main")
        # a linked worktree on main-like state: never touched (it is some agent's)
        self.assertEqual(A.root_checkout_ff(wt), [])
        self.assertTrue(A.root_checkout_ff(self.r.path))
        self.assertEqual(sh(self.r.path, "git", "rev-parse", "HEAD"), moved)


class WaitAndStateTest(Base):
    def test_file_wait_single_waiter_and_attach(self):
        os.environ["AGENTCTL_MIN_INTERVAL"] = "1"
        os.environ["AGENTCTL_ATTACH_POLL"] = "0.2"
        target = os.path.join(self.tmp.name, "done.flag")
        proc = subprocess.Popen([sys.executable, TOOL, "wait", "file", target, "--interval", "1"], stdout=subprocess.PIPE, text=True, env=os.environ)
        t0 = time.time()  # the waiter's start (a cold interpreter on a loaded runner: over 1 s on CI run 3969)
        while not A.owner_records() and time.time() - t0 < 20:
            time.sleep(0.1)
        records = A.owner_records()
        self.assertEqual([(r["kind"], r["alive"]) for r in records], [("wait", True)])
        second = subprocess.Popen([sys.executable, TOOL, "wait", "file", target], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=os.environ)
        # the file appears only once the second has attached (CI run 4194: a fixed 0.8 s, and on a loaded runner the
        # second's cold interpreter first saw the file itself -- nothing attached, nothing said)
        errs = []
        reader = threading.Thread(target=lambda: errs.extend(second.stderr), daemon=True)
        reader.start()
        t1 = A.epoch_now()
        while not any("attached to existing waiter" in l for l in errs) and second.poll() is None and A.epoch_now() - t1 < 60:
            time.sleep(0.1)
        with open(target, "w") as f:
            f.write("ok")
        o1, _ = proc.communicate(timeout=180)
        o2 = second.stdout.read()
        second.wait(timeout=180)
        reader.join(timeout=10)
        e2 = "".join(errs)
        self.assertIn("present", o1)
        self.assertIn("attached to existing waiter", e2)
        self.assertEqual(o1.strip(), o2.strip())

    def test_state_roundtrip(self):
        rc, out = self.run_tool("state", "t1", "--set", "branch=b1", "--add", "run ids=11", "--add", "run ids=12")
        self.assertIn("branch: b1", out)
        self.assertIn("run ids: 11, 12", out)
        rc, out = self.run_tool("state", "t1")
        self.assertIn("run ids: 11, 12", out)

    def test_orphan_classifiers(self):
        loop = "/bin/zsh -c source x && eval 'for id in 1292 1289; do while :; do s=$(gh run view $id -R bitcashier/infra); sleep 60; done; done' < /dev/null"
        self.assertTrue(A.is_wait_loop(loop))
        self.assertEqual(A.loop_targets(A.loop_body(loop)), ("bitcashier/infra", ["1289", "1292"]))
        self.assertFalse(A.is_wait_loop("/bin/zsh -c source x && eval 'sleep 1800; status.sh' < /dev/null"))
        self.assertEqual(A.missing_tree_paths(f"cd {A.HOME}/projects/movies-wt-gone-xyz && sleep 5"), [f"{A.HOME}/projects/movies-wt-gone-xyz"])


class MutationTest(unittest.TestCase):
    def test_unmerged_test_kills_a_mutant_merge_check(self):
        """If merged_reason() wrongly called everything merged, test_refuses_unmerged_worktree must fail."""
        real = A.merged_reason
        A.merged_reason = lambda repo, head, main: "MUTANT"
        try:
            res = unittest.TextTestRunner(stream=io.StringIO(), verbosity=0).run(CleanupTest("test_refuses_unmerged_worktree"))
        finally:
            A.merged_reason = real
        self.assertTrue(res.failures or res.errors, "mutant survived")


if __name__ == "__main__":
    unittest.main()
