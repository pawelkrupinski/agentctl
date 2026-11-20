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
- ONE CLOCK RULE: a test fakes time only through agentctl's own module proxies (`A.time.sleep = ...`,
  `A.subprocess.run = ...`), never the stdlib modules, so no real subprocess ever waits under a faked clock (82fdc89).
  Every test starts and ends with fresh proxies in every agentctl module (a forgotten restore cannot leak into the next
  test), and a test that leaves the stdlib's time or subprocess patched fails. agentctl's process-wide flags
  (_IN_PROCESSOR, the token cache) are reset per test too: a leaked processor flag failed test_token at random.
- ONE ENVIRONMENT: every test ends with os.environ as it found it, and a test that changed a token variable
  ($GH_TOKEN, $AGENTCTL_STORE_TOKEN, any *_TOKEN) fails, naming it."""
import os, shutil, signal, subprocess, sys, tempfile, threading, time, types, unittest

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
        if k in _DROP or k.startswith(_DROP_PREFIXES) or (k.startswith("AGENTCTL_") and not k.startswith("AGENTCTL_TEST")):
            del os.environ[k]  # every agentctl knob of the shell running the tests too: AGENTCTL_STORE_URL, AGENTCTL_HOST,
            # AGENTCTL_MOVIES_REPO... would point the suite at a real store or clone (the tests set their own)
    # AGENTCTL_LAPTOP_PRESSURE "-": the machine running the tests is never the one under test (2026-10-30: ci-runner
    # at load 95 on 32 cores held every fixer and owner dispatch back -- the owner-restart test failed 3 runs in a row)
    # AGENTCTL_MACHINE: an idle 12-core 36 GB machine with 500 GB of disk in place of the runner's own (lib/agentctl/gov.py machine_fixed):
    # a 4-core GitHub runner or a loaded laptop must not change what the governor, the check cap or land decide.
    os.environ.update(HOME=home, PATH=f"{hbin}:{SYSTEM_PATH}", GIT_CONFIG_GLOBAL=gitconfig, GIT_CONFIG_NOSYSTEM="1",
                      AGENTCTL_TEST_HOME=home, AGENTCTL_LAPTOP_PRESSURE="-",
                      AGENTCTL_MACHINE='{"ram_gb": 36, "free_gb": 20, "pressure": 40, "swap_used_mb": 0, '
                                       '"swap_total_mb": 0, "load1": 1.0, "cores": 12, "free_pct": 60, "level": 1, '
                                       '"disk_free_gb": 500}')


_hermetic_environment()


# --- the clock rule -------------------------------------------------------------------------------------------------

class ModuleProxy(types.ModuleType):
    """A module's attributes, except what a test assigns on the proxy: `A.time.sleep = fake` fakes agentctl's sleep
    alone, while subprocess's own wait(timeout=...) poll keeps the real one."""

    def __init__(self, real):
        super().__init__(real.__name__)
        object.__setattr__(self, "_real", real)

    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, "_real"), name)


_PROXIED = {"time": time, "subprocess": subprocess}
_STDLIB = {(m, n): getattr(mod, n) for m, mod in _PROXIED.items()
           for n in ("sleep", "time", "monotonic", "run", "Popen", "check_output", "call", "check_call")
           if hasattr(mod, n)}
_AGENTCTL_MODULES = []  # registered by test_agentctl: the loaded agentctl (and, after the split, its modules)


def register(module):
    """agentctl's module(s): each gets its own time and subprocess proxy now and fresh ones before every test."""
    _AGENTCTL_MODULES.append(module)
    _fresh_proxies()


def _agentctl_modules():
    mods = list(_AGENTCTL_MODULES)
    mods += [m for n, m in list(sys.modules.items())
             if (n == "agentctl" or n.startswith("agentctl.")) and m is not None and m not in mods]
    return mods


def _fresh_proxies():
    for mod in _agentctl_modules():
        for name, real in _PROXIED.items():
            cur = mod.__dict__.get(name)
            if cur is real or isinstance(cur, ModuleProxy):
                setattr(mod, name, ModuleProxy(real))


def _stdlib_patched():
    """The stdlib clock / subprocess attributes a test replaced (and puts them back)."""
    bad = []
    for (m, n), real in _STDLIB.items():
        if getattr(_PROXIED[m], n) is not real:
            bad.append(f"{m}.{n}")
            setattr(_PROXIED[m], n, real)
    return bad


def _reset_process_state():
    """agentctl's process-wide state a test may leave set (a processor run marks the process as the processor; a
    token read caches the token; the registered caches): every test starts as a fresh, non-processor process with no
    cached token and empty caches."""
    for mod in _agentctl_modules():
        for name in ("_IN_PROCESSOR", "_NO_VAULT"):
            flag = mod.__dict__.get(name)
            if isinstance(flag, list) and flag:
                flag[0] = False
        toks = mod.__dict__.get("_tokens")
        if isinstance(toks, dict):
            toks.clear()
        # every part's process-wide cache (core.process_cache), for every test -- not only Base's: a FakeForge test once
        # met test_caches' deliberate `_gate_lists["leak"] = 1` when both ran in one worker (agentctl CI #44108162628)
        for cache in mod.__dict__.get("PROCESS_CACHES") or []:
            cache.clear()


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


def _run(self, result=None):
    main = threading.current_thread() is threading.main_thread()
    env_before = dict(os.environ)
    patches_before = [(m, _patchable(m)) for m in _agentctl_modules()]
    _fresh_proxies()
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
    _fresh_proxies()  # a fake the test did not restore ends with it
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
    bad = _stdlib_patched()
    if bad and result is not None:
        try:
            raise AssertionError(f"the test left the stdlib's {', '.join(bad)} faked: fake agentctl's own "
                                 f"(A.time.* / A.subprocess.*), never the stdlib's -- a real subprocess would wait "
                                 f"under the fake")
        except AssertionError:
            result.addFailure(self, sys.exc_info())
    return out


unittest.TestCase.run = _run
