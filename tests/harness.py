"""THE SHARED TEST HARNESS: imported first by test_agentctl (which every test module imports), before agentctl loads.

No test depends on the machine it runs on (docs/simplification-2026-10.md, Agent B item 2):
- HOME is a fresh temp dir per process (agentctl's HOME, SDKMAN_BINS, ~/.cache, ~/.claude, ~/projects all derive from
  it); the machine's real home is REAL_HOME, for the guards that check nothing leaked into it.
- PATH is fixed: a harness bin dir holding only `python3` (this interpreter), `git` and `node` (the ones this process
  found), then the system's /usr/bin:/bin:/usr/sbin:/sbin. No sdkman, Homebrew, nix profile or ~/bin on it.
- No sdkman, Java, tokens or git identity from the environment: SDKMAN_*, JAVA_HOME, GH_TOKEN, FORGEJO_API_TOKEN and
  every GIT_* variable are dropped; git reads the harness's own gitconfig (identity, init.defaultBranch=main) and no
  system config.
- A GLOBAL PER-TEST TIMEOUT ($AGENTCTL_TEST_TIMEOUT, default 60 s): SIGALRM raises TestTimeout inside the test, so a
  hang (RemotePickupTest once spun for 50 min) fails fast with its traceback, under any runner.
- ONE CLOCK RULE: a test fakes time only through agentctl's own module proxies (`A.time.sleep = ...`,
  `A.subprocess.run = ...`), never the stdlib modules, so no real subprocess ever waits under a faked clock (82fdc89).
  Every test starts and ends with fresh proxies in every agentctl module (a forgotten restore cannot leak into the next
  test), and a test that leaves the stdlib's time or subprocess patched fails. agentctl's process-wide flags
  (_IN_PROCESSOR, the token cache) are reset per test too: a leaked processor flag failed test_token at random."""
import os, shutil, signal, subprocess, sys, tempfile, threading, time, types, unittest

REAL_HOME = os.path.expanduser("~")
# python3 is the interpreter running the tests (agentctl's `#!/usr/bin/env python3` children run on it too); node: the
# status page's JS test
_REAL_WHICH = {"python3": sys.executable, "git": shutil.which("git"), "node": shutil.which("node")}
TIMEOUT = int(os.environ.get("AGENTCTL_TEST_TIMEOUT") or 60)
SYSTEM_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"
_DROP_PREFIXES = ("SDKMAN_", "GIT_")
_DROP = {"JAVA_HOME", "GH_TOKEN", "FORGEJO_API_TOKEN", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME",
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
                "[advice]\n\tdetachedHead = false\n[protocol \"file\"]\n\tallow = always\n")
    for k in list(os.environ):
        if k in _DROP or k.startswith(_DROP_PREFIXES) or (k.startswith("AGENTCTL_") and not k.startswith("AGENTCTL_TEST")):
            del os.environ[k]  # every agentctl knob of the shell running the tests too: AGENTCTL_STORE_URL, AGENTCTL_HOST,
            # AGENTCTL_MOVIES_REPO... would point the suite at a real store or clone (the tests set their own)
    # AGENTCTL_LAPTOP_PRESSURE "-": the machine running the tests is never the one under test (2026-10-30: ci-runner
    # at load 95 on 32 cores held every fixer and owner dispatch back -- the owner-restart test failed 3 runs in a row)
    os.environ.update(HOME=home, PATH=f"{hbin}:{SYSTEM_PATH}", GIT_CONFIG_GLOBAL=gitconfig, GIT_CONFIG_NOSYSTEM="1",
                      AGENTCTL_TEST_HOME=home, AGENTCTL_LAPTOP_PRESSURE="-")


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
    """agentctl's process-wide flags a test may leave set (a processor run marks the process as the processor; a
    token read caches the token): every test starts as a fresh, non-processor process with no cached token."""
    for mod in _agentctl_modules():
        for name in ("_IN_PROCESSOR", "_NO_VAULT"):
            flag = mod.__dict__.get(name)
            if isinstance(flag, list) and flag:
                flag[0] = False
        toks = mod.__dict__.get("_tokens")
        if isinstance(toks, dict):
            toks.clear()


# --- the per-test timeout -------------------------------------------------------------------------------------------

def _alarm(signum, frame):
    raise TestTimeout(f"test exceeded the per-test timeout ({TIMEOUT} s, $AGENTCTL_TEST_TIMEOUT)")


_real_run = unittest.TestCase.run


def _run(self, result=None):
    main = threading.current_thread() is threading.main_thread()
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
