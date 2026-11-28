"""THE SHARED TEST HARNESS: imported first by test_agentctl (which every test module imports), before agentctl loads.

No test depends on the machine it runs on (docs/simplification-2026-10.md, Agent B item 2):
- HOME is a fresh temp dir per process (agentctl's HOME, SDKMAN_BINS, ~/.cache, ~/.claude, ~/projects all derive from
  it); the machine's real home is REAL_HOME, for the guards that check nothing leaked into it.
- PATH is fixed: a harness bin dir holding only `python3` (this interpreter), `git` and `node` (the ones this process
  found), then the system's /usr/bin:/bin:/usr/sbin:/sbin. No sdkman, Homebrew, nix profile or ~/bin on it.
- No sdkman, Java, tokens or git identity from the environment: SDKMAN_*, JAVA_HOME, GH_TOKEN and
  every GIT_* variable are dropped; git reads the harness's own gitconfig (identity, init.defaultBranch=main) and no
  system config.
- A GLOBAL PER-TEST TIMEOUT ($AGENTCTL_TEST_TIMEOUT, default 60 s): SIGALRM raises TestTimeout inside the test, so a
  hang (RemotePickupTest once spun for 50 min) fails fast with its traceback, under any runner.
- THE CLOCK AND THE PROCESS RUNNER ARE PASSED IN (lib/agentctl/clock.py, procrunner.py): a test hands agentctl a
  tests/fake_clock.py FakeClock (or a store on a fake clock) and wires a tests/fake_runner.py FakeRunner, never patches
  the time or subprocess module (a faked stdlib sleep once reached subprocess's own wait: 82fdc89). Every test starts
  and ends with the real runner wired (a forgotten restore cannot leak into the next test), and a test that leaves the
  stdlib's time or subprocess patched fails. agentctl's process-wide flags (_IN_PROCESSOR, the token cache) are reset
  per test too: a leaked processor flag failed test_token at random.
- ONE ENVIRONMENT: every test ends with os.environ as it found it, and a test that changed a token variable
  ($GH_TOKEN, $AGENTCTL_STORE_TOKEN, any *_TOKEN) fails, naming it.
- NO THREAD OUTLIVES ITS TEST: a test that leaves a thread it started running (past THREAD_GRACE s after it ends)
  fails, naming it. 2026-11-26 (tests #289 / #285): status reads left sleeping by test_real_elapsed called epoch_now()
  seconds later inside the next test in the worker, a golden faking it, and its digest moved."""
import json, os, shutil, signal, subprocess, sys, tempfile, threading, time, types, unittest

REAL_HOME = os.path.expanduser("~")
# python3 is the interpreter running the tests (agentctl's `#!/usr/bin/env python3` children run on it too); node: the
# status page's JS test
_REAL_WHICH = {"python3": sys.executable, "git": shutil.which("git"), "node": shutil.which("node")}
TIMEOUT = int(os.environ.get("AGENTCTL_TEST_TIMEOUT") or 60)
SYSTEM_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"
_DROP_PREFIXES = ("SDKMAN_", "GIT_")
_DROP = {"JAVA_HOME", "GH_TOKEN", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME",
         "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SESSION_ID", "AGENTCTL_INTERACTIVE", "AGENTCTL_NO_SECRETS",
         "AGENTCTL_DETACHED"}


class TestTimeout(Exception):
    pass


# THE TESTS' MACHINE (machine.FixedMachine's fields): an idle 12-core 36 GB machine, 64 GB of it free, with 500 GB of disk.
HARNESS_MACHINE = {"ram_gb": 36, "free_gb": 64, "pressure": 10, "swap_used_mb": 0, "swap_total_mb": 0, "load1": 1.0,
                   "cores": 12, "free_pct": 60, "level": 1, "disk_free_gb": 500}


def fixed_machine(**fields):
    """The tests' machine (HARNESS_MACHINE) with `fields` changed: a FixedMachine a test hands to a reader."""
    return sys.modules["agentctl.machine"].FixedMachine({**HARNESS_MACHINE, **fields})


class MachineFile:
    """The tests' machine (HARNESS_MACHINE, `fields` changed) in a JSON file: `probe` reads it again at every reading,
    and so does every process the test starts once its environment names it (env_value: $AGENTCTL_MACHINE)."""

    def __init__(self, path, **fields):
        self.path, self.fields = path, {**HARNESS_MACHINE, **fields}
        self.write()
        self.probe = sys.modules["agentctl.machine"].FixedMachine(path=path)

    def update(self, **fields):
        self.fields.update(fields)
        self.write()
        return self

    def write(self):
        with open(self.path + ".tmp", "w") as f:
            json.dump(self.fields, f)
        os.replace(self.path + ".tmp", self.path)

    def env_value(self):
        return "@" + self.path


def _hermetic_environment():
    if os.environ.get("AGENTCTL_TEST_HOME"):  # this process's harness is already in place (a re-import)
        return
    root = tempfile.mkdtemp(prefix="agentctl-home-")
    home, hbin = os.path.join(root, "home"), os.path.join(root, "bin")
    os.makedirs(home)
    os.makedirs(hbin)
    for name, real in _REAL_WHICH.items():
        if real:
            os.symlink(real, os.path.join(hbin, name))
    gitconfig = os.path.join(root, "gitconfig")
    with open(gitconfig, "w") as f:
        f.write("[user]\n\tname = t\n\temail = t@t\n[init]\n\tdefaultBranch = main\n"
                "[advice]\n\tdetachedHead = false\n[protocol \"file\"]\n\tallow = always\n"
                # NO DETACHED MAINTENANCE (2026-11-20, a full-suite run's QueueTest setUp): git >= 2.4x starts `git
                # maintenance run --auto --detach` after a commit or fetch, holding objects/maintenance.lock a moment
                # after the command returned (35 of 40 commits, measured), and the repo template's copytree read the
                # lock as it vanished: "No such file or directory: .../objects/maintenance.lock"
                "[maintenance]\n\tauto = false\n[gc]\n\tauto = 0\n")
    for k in list(os.environ):
        if k in _DROP or k.startswith(_DROP_PREFIXES) or (k.startswith("AGENTCTL_") and not k.startswith("AGENTCTL_TEST")
                                                                  and k != "AGENTCTL_GOV_WIDTH"):
            del os.environ[k]  # every agentctl knob of the shell running the tests too: AGENTCTL_STORE_URL, AGENTCTL_HOST,
            # AGENTCTL_MOVIES_REPO... would point the suite at a real store or clone (the tests set their own); never
            # AGENTCTL_GOV_WIDTH: a worker's one core of the suite's ticket (govpool.py), which a pool in a test rides
    # AGENTCTL_LAPTOP_PRESSURE "-": the machine running the tests is never the one under test (2026-10-30: ci-runner
    # at load 95 on 32 cores held every fixer and owner dispatch back -- the owner-restart test failed 3 runs in a row)
    # AGENTCTL_MACHINE: an idle 12-core 36 GB machine with 500 GB of disk in place of the runner's own (machine.FixedMachine):
    # a 4-core GitHub runner or a loaded laptop must not change what the governor, the check cap or land decide.
    os.environ.update(HOME=home, PATH=f"{hbin}:{SYSTEM_PATH}", GIT_CONFIG_GLOBAL=gitconfig, GIT_CONFIG_NOSYSTEM="1",
                      AGENTCTL_TEST_HOME=home, AGENTCTL_LAPTOP_PRESSURE="-", AGENTCTL_MACHINE=json.dumps(HARNESS_MACHINE))


_hermetic_environment()


# --- the stdlib rule -------------------------------------------------------------------------------------------------

_GUARDED = {"time": time, "subprocess": subprocess}  # a test leaving one of these patched fails
_STDLIB = {(m, n): getattr(mod, n) for m, mod in _GUARDED.items()
           for n in ("sleep", "time", "monotonic", "run", "Popen", "check_output", "call", "check_call")
           if hasattr(mod, n)}
_AGENTCTL_MODULES = []  # registered by test_agentctl: the loaded agentctl (and, after the split, its modules)


def register(module):
    """agentctl's module(s): what the harness resets before and after every test."""
    _AGENTCTL_MODULES.append(module)


def _agentctl_modules():
    mods = list(_AGENTCTL_MODULES)
    mods += [m for n, m in list(sys.modules.items())
             if (n == "agentctl" or n.startswith("agentctl.")) and m is not None and m not in mods]
    return mods


def _stdlib_patched():
    """The stdlib clock / subprocess attributes a test replaced (and puts them back)."""
    bad = []
    for (m, n), real in _STDLIB.items():
        if getattr(_GUARDED[m], n) is not real:
            bad.append(f"{m}.{n}")
            setattr(_GUARDED[m], n, real)
    return bad


# --- the process-wide default clock ----------------------------------------------------------------------------------
# 2026-11-25, the third flake of one family (81d50c0, 3fdf8a8, 5490672): a test stamped deadlines with a frozen FakeClock
# while a code path read the wall (`Clock()`, `time.time()`), so it failed only when the run was slow. Now no code under
# test can read the wall: the real Clock's methods and every agentctl module's `time.time` / `time.monotonic`
# answer from ONE test clock, a FakeClock that starts TEST_CLOCK_LAG s behind the wall ($AGENTCTL_TEST_CLOCK_LAG:
# the stress run's knob; a slow run's frozen clock reads late, so mixing the test's clock with the default one fails at
# once on any machine). A test that stamps with its own clock and reads the default one sees them 400 s apart.
TEST_CLOCK_LAG = float(os.environ.get("AGENTCTL_TEST_CLOCK_LAG") or 400)
_REAL_TIME = {n: getattr(time, n) for n in ("time", "monotonic", "sleep")}
DEFAULT_CLOCK = []  # [the current test's default FakeClock]
WALL_READS = []  # every `clock.wall_epoch()` (the wall, for a file's mtime) an agentctl module read: counted, per process


def default_clock():
    """The process-wide default clock of the running test (a fresh lagging FakeClock per test)."""
    return DEFAULT_CLOCK[0]


class _TestTime(types.ModuleType):
    """What an agentctl module's `time` is under test: the default test clock's time (so a test's `A.time.time()` is
    what the code reads), the stdlib's everything else (a real sleep: a wait on a subprocess or thread must really wait)."""

    def __getattr__(self, name):
        return getattr(time, name)

    def time(self):
        return default_clock().time()


_TEST_TIME = _TestTime("time")


class _WallTime(types.ModuleType):
    """The `time` of agentctl.clock under test: `wall_epoch()`'s read is the wall, counted; the rest the stdlib's."""

    def __getattr__(self, name):
        return getattr(time, name)

    def time(self):
        WALL_READS.append("time")
        return _REAL_TIME["time"]()


_WALL_TIME = _WallTime("time")


def _install_default_clock():
    from fake_clock import FakeClock
    from agentctl.clock import Clock
    DEFAULT_CLOCK[:] = [FakeClock(_REAL_TIME["time"]() - TEST_CLOCK_LAG)]
    Clock.time = lambda self: default_clock().time()
    Clock.monotonic = lambda self: default_clock().monotonic()
    Clock.sleep = lambda self, secs: default_clock().sleep(secs)
    for mod in _agentctl_modules():
        if mod.__dict__.get("time") in (time, _TEST_TIME, _WALL_TIME):
            mod.time = _WALL_TIME if mod.__name__ == "agentctl.clock" else _TEST_TIME


# THE CASE'S CONTEXT (lib/agentctl/context.py): what cli.main builds for a process, one per test -- a fresh CiRuns, so
# what one test read of CI runs never answers the next (_reset_process_state forgets it). A test hands it as `ctx=` to
# what it calls directly; main builds its own.
_CASE_CONTEXT = []


class WiredClock:
    """The case's clock: whatever clock the test has wired (clock.wire_clock: the default test clock, a FakeClock it
    wired, a simulation's), read at each call, so a context reader sees the time every ambient `epoch_now()` reader
    does -- a test wiring its clock after its context was made included."""

    @staticmethod
    def _wired():
        return sys.modules["agentctl.clock"]._PROCESS_CLOCK[0]

    def time(self):
        return sys.modules["agentctl.clock"].epoch_now()

    def monotonic(self):
        return self._wired().monotonic()

    def sleep(self, secs):
        return self._wired().sleep(secs)


class WiredRunner:
    """The case's process runner: whatever runner the test has wired (procrunner.wire_runner: the real one, a
    FakeRunner), asked at each call, as every ambient `process_runner()` reader asks it."""

    def run(self, argv, **kw):
        return sys.modules["agentctl.procrunner"].process_runner().run(argv, **kw)

    def popen(self, argv, **kw):
        return sys.modules["agentctl.procrunner"].process_runner().popen(argv, **kw)

    @property
    def detached(self):
        return sys.modules["agentctl.procrunner"].process_runner().detached


class WiredInbox:
    """The case's inbox writer: inboxentry's inbox_write, looked up at each call, so what a test wired or faked
    (`A.inbox_write = ...`) takes a context writer's entries as it takes every ambient writer's."""

    def write(self, *args, **kw):
        return sys.modules["agentctl.inboxentry"].inbox_write(*args, **kw)


class WiredMachine:
    """The case's machine: the fixed machine $AGENTCTL_MACHINE names (machine.machine_probe), read at each call, so a
    test that pins its own machine in the environment reaches a context's readers too."""

    def __getattr__(self, name):
        machine = sys.modules["agentctl.machine"]
        return getattr(machine.machine_probe(os.environ.get("AGENTCTL_MACHINE", "")), name)


def context_with(runs):
    """A Context over `runs` (a test's own CI reader, or a fake of it) and the case's wired collaborators."""
    return sys.modules["agentctl.context"].Context(runs=runs, clock=WiredClock(), runner=WiredRunner(),
                                                   inbox=WiredInbox(), machine=WiredMachine(),
                                                   caches=sys.modules["agentctl.caches"].Caches())


def case_context():
    """The running test's Context (made on first ask)."""
    if not _CASE_CONTEXT:
        _CASE_CONTEXT.append(context_with(sys.modules["agentctl.ciruns"].CiRuns()))
    return _CASE_CONTEXT[0]


def _reset_process_state():
    """agentctl's process-wide state a test may leave set (a processor run marks the process as the processor; a
    token read caches the token; the registered caches; a wired FakeRunner; main's wiring of a test's clock): every test
    starts as a fresh, non-processor process with no cached token, empty caches, the real process runner, a fresh
    default clock (above) and the import-time wiring (the default clock, the process's store)."""
    _install_default_clock()
    _CASE_CONTEXT.clear()
    for mod in _agentctl_modules():
        for name in ("_IN_PROCESSOR", "_NO_VAULT"):
            flag = mod.__dict__.get(name)
            if isinstance(flag, list) and flag:
                flag[0] = False
        toks = mod.__dict__.get("_tokens")
        if isinstance(toks, dict):
            toks.clear()
        # every part's process-wide cache (core.process_cache), for every test -- not only Base's: a FakeForge test once
        # met test_caches' deliberate `_main_runs["leak"] = 1` when both ran in one worker (agentctl CI #44108162628)
        for cache in mod.__dict__.get("PROCESS_CACHES") or []:
            cache.clear()
        # the process runner (procrunner.wire_runner): the real one, whatever FakeRunner a test wired
        runner, real = mod.__dict__.get("_runner"), mod.__dict__.get("ProcessRunner")
        if isinstance(runner, list) and real is not None and mod.__name__ == "agentctl.procrunner":
            runner[:] = [real()]
        # the composition root's wiring (cli.wire_collaborators: the inbox's clock and writer, the probes reading the
        # clock and store): what main wired with a test's FakeClock ends with that test. 2026-11-25 (agentctl CI red
        # since e4d02a4): a test's standing clock stamped the next test's inbox entries 400 s behind the wall, and
        # test_orphans' infra retry read its verdict as older than its submit -- in CI's worker order only.
        if mod.__name__ == "agentctl.cli":
            mod.wire_collaborators(mod.Clock(), mod.CurrentStore(mod.process_store))


# --- the per-test timeout -------------------------------------------------------------------------------------------

def _alarm(signum, frame):
    raise TestTimeout(f"test exceeded the per-test timeout ({TIMEOUT} s, $AGENTCTL_TEST_TIMEOUT)")


_real_run = unittest.TestCase.run


TOKEN_KEYS = ("GH_TOKEN", "GITHUB_TOKEN", "AGENTCTL_STORE_TOKEN")


def _restore_environment(before):
    """Put os.environ back as the test found it; the token variables it changed (a token one test leaves set is the
    next test's credential: 2026-11-16, test_token's served drop wrote $AGENTCTL_STORE_TOKEN after its tearDown and
    test_forge, next in the worker, read it)."""
    after = dict(os.environ)
    leaked = sorted(k for k in set(before) | set(after)
                    if before.get(k) != after.get(k) and (k in TOKEN_KEYS or k.endswith("_TOKEN")))
    os.environ.clear()
    os.environ.update(before)
    return leaked


def _patchable(mod):
    """{name: object} of the module's functions and classes: what a test fakes by assignment (`A.ci_state_load = ...`)."""
    return {n: v for n, v in mod.__dict__.items() if isinstance(v, (types.FunctionType, type))}


def _restore_patches(before):
    """Put back every agentctl function or class a test replaced and did not restore; the names, for its failure.
    2026-11-18: test_red_crash_loop's RecoveryTest faked ci_state_load twice in one test, so its tearDown "restored"
    the first fake: the next test in the worker (test_cistate's snapshot test) read a snapshot without a gate and
    failed with KeyError 'gate', once in a suite run, depending on which worker ran what."""
    leaked = []
    for mod, funcs in before:
        for n, v in funcs.items():
            if mod.__dict__.get(n) is not v:
                leaked.append(n)
                setattr(mod, n, v)
    return sorted(set(leaked))


THREAD_GRACE = 2.0  # s a test's threads get to end once it has (a join's own bound: real time)


def _threads_left(before):
    """The threads started during a test still running THREAD_GRACE s after it: their names."""
    deadline = time.monotonic() + THREAD_GRACE  # wall: a join's bound is real time
    left = [t for t in threading.enumerate() if t not in before and t is not threading.current_thread()]
    for t in left:
        t.join(max(0.0, deadline - time.monotonic()))
    return sorted(t.name for t in left if t.is_alive())


def _run(self, result=None):
    main = threading.current_thread() is threading.main_thread()
    threads_before = set(threading.enumerate())
    env_before = dict(os.environ)
    patches_before = [(m, _patchable(m)) for m in _agentctl_modules()]
    _reset_process_state()
    if main:
        prev = signal.signal(signal.SIGALRM, _alarm)
        signal.alarm(TIMEOUT)
    try:
        out = _real_run(self, result)
    finally:
        if main:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, prev)
    _reset_process_state()  # a fake runner the test did not restore ends with it
    leaked = _restore_environment(env_before)  # every test ends with the environment it started with
    if leaked and result is not None:
        try:
            raise AssertionError(f"the test left {', '.join(leaked)} changed in os.environ (restored now): restore "
                                 f"what it sets, and close a served token drop before restoring")
        except AssertionError:
            result.addFailure(self, sys.exc_info())
    patched = _restore_patches(patches_before)
    if patched and result is not None:
        try:
            raise AssertionError(f"the test left agentctl's {', '.join(patched)} replaced (restored now): restore what "
                                 "it fakes -- the next test in this worker would run against the fake")
        except AssertionError:
            result.addFailure(self, sys.exc_info())
    stray = _threads_left(threads_before) if main else []
    if stray and result is not None:
        try:
            raise AssertionError(f"the test left thread(s) {', '.join(stray)} running: end what it starts with it (an "
                                 "Event its cleanup sets, then a join) -- a stray thread's calls land in the next test")
        except AssertionError:
            result.addFailure(self, sys.exc_info())
    bad = _stdlib_patched()
    if bad and result is not None:
        try:
            raise AssertionError(f"the test left the stdlib's {', '.join(bad)} faked: hand agentctl a "
                                 f"FakeClock (tests/fake_clock.py) and wire a FakeRunner (tests/fake_runner.py), never "
                                 f"the stdlib's -- a real subprocess would wait under the fake")
        except AssertionError:
            result.addFailure(self, sys.exc_info())
    return out


unittest.TestCase.run = _run
