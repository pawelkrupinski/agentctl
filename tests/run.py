#!/usr/bin/env python3
"""agentctl's test runner: the whole suite in parallel workers, every test under a timeout.

  python3 tests/run.py [-j N] [--timeout S] [--durations K] [PATTERN...]

- PARALLEL BY TEST. N long-lived workers (default: min(cpus, 8)) each take the next test off one queue, longest
  first by the last run's timings (~/.cache/agentctl-tests/durations.json, or the module order when there are none).
  Each worker is its own process with its own temp root and state root (test_agentctl sets them per process), so no
  two workers share a file.
- A PER-TEST TIMEOUT (default 60 s, $AGENTCTL_TEST_TIMEOUT): the harness's (tests/harness.py) SIGALRM raises
  TestTimeout inside the test (a blocked syscall included), so the test FAILS with its traceback instead of hanging the
  suite; a test that does not come back from that within 30 s more has its worker killed here (faulthandler dumps
  every thread first), and the test is reported as WORKER DIED. The suite as a whole is bounded too (--suite-timeout, default 600 s).
- PATTERN: only tests whose id (module.Class.test) contains one of the patterns.
Exit 0 when every test passed, 1 otherwise. The hermetic harness (HOME, PATH, git config, timeout) is tests/harness.py."""
import argparse, json, os, shutil, signal, subprocess, sys, tempfile, threading, time, traceback, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
DURATIONS = os.path.join(os.path.expanduser("~"), ".cache", "agentctl-tests", "durations.json")
SUITE_WALL = os.path.join(os.path.dirname(DURATIONS), "suite-wall.json")  # the last FULL run's wall time: hooks/pre-push's base
GRACE = 30


class TimedResult(unittest.TestResult):
    """Times each test; a hard backstop (faulthandler + exit) for a test that ignores the harness's TestTimeout."""

    def __init__(self, timeout, *a, **kw):
        super().__init__(*a, **kw)
        self.timeout, self.times = timeout, {}

    def startTest(self, test):
        import faulthandler
        self._t0 = time.monotonic()
        faulthandler.dump_traceback_later(self.timeout + GRACE, exit=True, file=sys.stderr)
        super().startTest(test)

    def stopTest(self, test):
        import faulthandler
        faulthandler.cancel_dump_traceback_later()
        self.times[test.id()] = time.monotonic() - self._t0
        super().stopTest(test)


def list_tests(patterns):
    """Every test id once, under the module that DEFINES its class (a test module importing another's TestCase class
    would otherwise run that class's tests twice, as `discover` does)."""
    sys.path.insert(0, HERE)
    loader, out = unittest.TestLoader(), {}
    for fn in sorted(os.listdir(HERE)):
        if not (fn.startswith("test_") and fn.endswith(".py")):
            continue
        mod = fn[:-3]

        def walk(s):
            for t in s:
                if isinstance(t, unittest.TestSuite):
                    walk(t)
                elif type(t).__name__ == "_FailedTest":  # an import error: the module itself, reported as a failure
                    out[mod] = None
                else:
                    cls = type(t)
                    tid = f"{cls.__module__}.{cls.__qualname__}.{t._testMethodName}"
                    if cls.__module__ != "__main__":
                        out[tid] = None
        walk(loader.loadTestsFromName(mod))
    return [t for t in out if not patterns or any(p in t for p in patterns)]


def worker(timeout):
    """Reads test ids from stdin, runs each, writes one JSON line per test to stdout (the protocol's only output)."""
    sys.path.insert(0, HERE)
    proto = os.fdopen(os.dup(1), "w", buffering=1)
    os.dup2(2, 1)  # the tests' own prints go to stderr, never into the protocol
    loader = unittest.TestLoader()
    for line in sys.stdin:
        cid = line.strip()
        if not cid:
            continue
        t0 = time.monotonic()
        res = TimedResult(timeout)
        try:
            loader.loadTestsFromName(cid).run(res)
        except BaseException:  # a load error outside any test
            res.errors.append((cid, traceback.format_exc()))
        proto.write(json.dumps({
            "cls": cid, "run": res.testsRun, "secs": time.monotonic() - t0, "times": res.times,
            "failures": [(t if isinstance(t, str) else t.id(), tb) for t, tb in res.failures + res.errors],
            "skipped": len(res.skipped)}) + "\n")


# EVERY WORKER'S TEMP ROOT IS THE RUNNER'S (2026-11-22: 174 /private/tmp/agentctl-t-* dirs older than 2 h, 197 MB of
# fake repos, fed a runaway fseventsd): a worker's own atexit never ran for a worker killed past its timeout or at the
# suite bound, nor under a hook's `timeout -s KILL`. The runner makes the root (agentctl-t-*, as short as before: a
# socket path inside stays <= 104 bytes), hands it over as AGENTCTL_TEST_RUN_TMP / TMPDIR (test_agentctl then makes
# none of its own), removes it once the worker has ended however it ended, and fails the run if one survives.
def start_worker(timeout, env, stderr):
    """A worker process in a fresh temp root of its own (p.root)."""
    # in /tmp, as the workers' own were (their TMPDIR stripped): a short path, never the runner's $TMPDIR (macOS's
    # /var/folders/.../T/ cut a test's ~/projects/<wt> paths at SESSION_PATH_DEPTH and lengthens every socket path)
    root = tempfile.mkdtemp(prefix="agentctl-t-", dir="/tmp" if os.path.isdir("/tmp") else None)
    with open(os.path.join(root, "owner.pid"), "w") as f:  # upkeep's sweep never removes a live runner's root
        f.write(str(os.getpid()))
    p = subprocess.Popen([sys.executable, os.path.abspath(__file__), "--worker", "--timeout", str(timeout)],
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=stderr, text=True,
                         env={**env, "AGENTCTL_TEST_RUN_TMP": root, "TMPDIR": root}, cwd=HERE)
    p.root = root
    return p


def end_worker(p, grace=0):
    """The worker ended (killed if it still runs after `grace` s: a worker whose input ended exits by itself, its exit
    handlers run -- coverage saves its data there), then its root removed -- tried again briefly, for a detached child
    of a test still writing into it."""
    try:
        p.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        pass
    if p.poll() is None:
        try:
            p.kill()
        except OSError:
            pass
    try:
        p.wait(timeout=30)
    except subprocess.TimeoutExpired:
        pass
    for _ in range(5):
        shutil.rmtree(p.root, ignore_errors=True)
        if not os.path.exists(p.root):
            return
        time.sleep(0.5)


def surviving(procs):
    """The roots of `procs` still on disk."""
    return [p.root for p in procs if os.path.exists(p.root)]


# EVERY SUITE RUN TAKES A GOVERNOR TICKET (2026-11-22: load 131-150 on 18 cores while lanes' `python3 tests/run.py`
# and the pre-push hook's suite ran ticketless, eight workers each): class `suite`, charged its workers' cores, held on
# a descriptor this process keeps open (released when it ends, as movies' jvm-gate.sh gov_hold holds one). None when
# the caller holds one already ($AGENTCTL_SUITE_TICKET: `agentctl suite`), or with no governor here. The wait is
# bounded (SUITE_TICKET_WAIT): past it the suite runs without one, said on stderr.
SUITE_TICKET_WAIT = 900


def hold_ticket(jobs, env=None):
    """(fd, ticket path) of the suite ticket now held, or None."""
    env = os.environ if env is None else env
    gov = env.get("AGENTCTL_SUITE_GOVERNOR") or os.path.join(os.path.dirname(HERE), "agentctl")
    if env.get("AGENTCTL_SUITE_TICKET") or env.get("AGENTCTL_SUITE_GOVERNOR") == "-" or not os.access(gov, os.X_OK):
        return None
    d = env.get("AGENTCTL_GOV_DIR") or os.path.expanduser("~/.cache/agentctl/gov")
    os.makedirs(d, exist_ok=True)
    ticket = os.path.join(d, f"t-suite-{os.getpid()}.lock")
    fd = os.open(ticket, os.O_WRONLY | os.O_CREAT | os.O_APPEND)
    try:
        r = subprocess.run([gov, "gov", "admit", "--fd", str(fd), "--ticket", ticket, "--class", "suite", "--mem", "1",
                            "--cores", str(jobs), "--label", f"tests/run.py {os.path.dirname(HERE)}",
                            "--pid", str(os.getpid())], pass_fds=(fd,), timeout=SUITE_TICKET_WAIT)
        if r.returncode == 0:
            return fd, ticket
    except (OSError, subprocess.TimeoutExpired) as e:
        sys.stderr.write(f"run.py: no governor ticket ({e}); running without one\n")
    release_ticket((fd, ticket))
    return None


def release_ticket(held):
    if held:
        os.close(held[0])
        try:
            os.unlink(held[1])
        except OSError:
            pass


def load_durations():
    try:
        with open(DURATIONS) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_json(path, d):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(f"{path}.{os.getpid()}.tmp", "w") as f:
            json.dump(d, f)
        os.replace(f"{path}.{os.getpid()}.tmp", path)
    except OSError:
        pass


def save_durations(d):
    save_json(DURATIONS, d)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-j", "--jobs", type=int, default=int(os.environ.get("AGENTCTL_TEST_JOBS") or min(os.cpu_count() or 2, 8)))
    ap.add_argument("--timeout", type=int, default=int(os.environ.get("AGENTCTL_TEST_TIMEOUT") or 60))
    ap.add_argument("--suite-timeout", type=int, default=600)
    ap.add_argument("--durations", type=int, default=0, metavar="K", help="print the K slowest tests")
    ap.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--list", action="store_true", help="print the test ids and exit")
    ap.add_argument("--shard", metavar="K/N", help="run only shard K (0-based) of N: a disjoint 1/N of the tests, every "
                    "Nth by id (CI's matrix: the whole suite outgrew one 4-cpu runner's bound)")
    ap.add_argument("patterns", nargs="*")
    a = ap.parse_args()
    shard = None
    if a.shard:
        try:
            shard = tuple(int(x) for x in a.shard.split("/"))
            assert len(shard) == 2 and 0 <= shard[0] < shard[1]
        except (ValueError, AssertionError):
            ap.error(f"--shard {a.shard}: K/N with 0 <= K < N")
    os.environ["AGENTCTL_TEST_TIMEOUT"] = str(a.timeout)
    if a.worker:
        return worker(a.timeout)
    # The parent never imports the tests (their import sets the per-process roots), it asks a child for the list.
    env = {k: v for k, v in os.environ.items()
           if k not in ("AGENTCTL_TEST_RUN_TMP", "AGENTCTL_STATE_ROOT", "TMPDIR", "AGENTCTL_SUITE_TICKET")}
    listing = subprocess.run([sys.executable, "-c", f"import sys; sys.argv=['run.py']; sys.path.insert(0, {HERE!r}); "
                              f"import run; print('\\n'.join(run.list_tests({a.patterns!r})))"],
                             capture_output=True, text=True, env=env, cwd=HERE, timeout=120)
    if listing.returncode:
        sys.stderr.write(listing.stderr)
        return 1
    classes = [c for c in listing.stdout.split() if c]  # test ids
    if shard:
        classes = sorted(classes)[shard[0]::shard[1]]
    if a.list:
        print("\n".join(classes))
        return 0
    past = load_durations()
    order = sorted(classes, key=lambda c: -past.get(c, 5.0))
    lock, todo, results, t0 = threading.Lock(), list(order), [], time.monotonic()
    procs = []

    quiet = None if os.environ.get("AGENTCTL_TEST_VERBOSE") else subprocess.DEVNULL

    def drive(i):
        p = start_worker(a.timeout, env, quiet)
        procs.append(p)
        while True:
            with lock:
                if not todo:
                    break
                cid = todo.pop(0)
            p.stdin.write(cid + "\n")
            p.stdin.flush()
            line = p.stdout.readline()
            if not line:  # the worker died: the hard timeout, or a crash; report it and start a fresh one
                results.append({"cls": cid, "run": 0, "secs": 0, "times": {}, "skipped": 0,
                                "failures": [(cid, f"WORKER DIED (exit {p.wait()}): a test past the per-test timeout "
                                                   f"+ {GRACE} s, or a crash; rerun with AGENTCTL_TEST_VERBOSE=1")]})
                end_worker(p)
                p = start_worker(a.timeout, env, quiet)
                procs.append(p)
                continue
            r = json.loads(line)
            with lock:
                results.append(r)
                mark = "." if not r["failures"] else "F"
                sys.stdout.write(mark)
                sys.stdout.flush()
        p.stdin.close()
        end_worker(p, grace=GRACE)

    # a SIGTERM / SIGHUP (a hook's bound, a closed terminal) ends the run through the finally below, its roots removed
    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, lambda n, _f: sys.exit(128 + n))
    threads = [threading.Thread(target=drive, args=(i,), daemon=True) for i in range(max(1, a.jobs))]
    held = hold_ticket(max(1, a.jobs))
    t0 = time.monotonic()  # the suite's bound starts once it is admitted
    try:
        for t in threads:
            t.start()
        deadline = t0 + a.suite_timeout
        for t in threads:
            t.join(max(0, deadline - time.monotonic()))
        timed_out = any(t.is_alive() for t in threads)
    finally:
        for p in list(procs):
            end_worker(p)
        release_ticket(held)
    leaked = surviving(procs)
    print()
    nrun = sum(r["run"] for r in results)
    fails = [f for r in results for f in r["failures"]]
    for tid, tb in fails:
        print("=" * 70 + f"\nFAIL: {tid}\n" + "-" * 70 + f"\n{tb}")
    if timed_out:
        print(f"SUITE TIMEOUT after {a.suite_timeout} s; not run: {', '.join(todo) or '-'}")
    if leaked:
        print(f"LEAKED TEMP ROOTS (a worker's agentctl-t-* survived its removal): {', '.join(leaked)}")
    if not a.patterns:  # a shard, or a run cut short, keeps the other classes' durations (the longest-first order)
        save_durations({**past, **{r["cls"]: r["secs"] for r in results}})
    if a.durations:
        times = sorted(((s, t) for r in results for t, s in r["times"].items()), reverse=True)[:a.durations]
        print("slowest:\n" + "\n".join(f"  {s:6.1f}s {t}" for s, t in times))
    took = time.monotonic() - t0
    skipped = sum(r["skipped"] for r in results)
    print(f"Ran {nrun} tests in {took:.1f}s ({a.jobs} workers)" +
          (f", {skipped} skipped" if skipped else ""))
    ok = not fails and not timed_out and not leaked and len(results) == len(classes)
    if ok and not a.patterns and not shard:
        save_json(SUITE_WALL, {"wall": round(took, 1), "jobs": a.jobs, "tests": nrun})
    print("OK" if ok else f"FAILED (failures={len(fails)})")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
