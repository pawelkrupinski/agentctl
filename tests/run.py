#!/usr/bin/env python3
"""agentctl's test runner: the whole suite in parallel workers, every test under a timeout.

  python3 tests/run.py [-j N] [--timeout S] [--durations K] [PATTERN...]

- PARALLEL BY TEST. N long-lived workers (default: min(cpus, 8)) each take the next test off one queue, longest
  first by the last run's timings (~/.cache/agentctl-tests/durations.json, or the module order when there are none).
  Each worker is its own process with its own temp root and state root (test_agentctl sets them per process), so no
  two workers share a file.
- A PER-TEST TIMEOUT (default 60 s, $AGENTCTL_TEST_TIMEOUT): SIGALRM raises TestTimeout inside the test (a blocked
  syscall included), so the test FAILS with its traceback instead of hanging the suite; a test that does not come back
  from that within 30 s more has its worker killed (faulthandler dumps every thread first), and the test is reported
  as WORKER DIED. The suite as a whole is bounded too (--suite-timeout, default 600 s).
- PATTERN: only tests whose id (module.Class.test) contains one of the patterns.
Exit 0 when every test passed, 1 otherwise. The hermetic harness (HOME, PATH, git config) is test_agentctl's."""
import argparse, json, os, signal, subprocess, sys, threading, time, traceback, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
DURATIONS = os.path.join(os.path.expanduser("~"), ".cache", "agentctl-tests", "durations.json")
GRACE = 30


class TestTimeout(Exception):
    pass


def _alarm(signum, frame):
    raise TestTimeout(f"test exceeded the per-test timeout ({os.environ.get('AGENTCTL_TEST_TIMEOUT')} s)")


class TimedResult(unittest.TestResult):
    """Arms the alarm per test; a hard backstop (faulthandler + exit) for a test that ignores the exception."""

    def __init__(self, timeout, *a, **kw):
        super().__init__(*a, **kw)
        self.timeout, self.times = timeout, {}

    def startTest(self, test):
        import faulthandler
        self._t0 = time.monotonic()
        signal.alarm(self.timeout)
        faulthandler.dump_traceback_later(self.timeout + GRACE, exit=True, file=sys.stderr)
        super().startTest(test)

    def stopTest(self, test):
        import faulthandler
        signal.alarm(0)
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
    signal.signal(signal.SIGALRM, _alarm)
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


def load_durations():
    try:
        with open(DURATIONS) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_durations(d):
    try:
        os.makedirs(os.path.dirname(DURATIONS), exist_ok=True)
        with open(DURATIONS + ".tmp", "w") as f:
            json.dump(d, f)
        os.replace(DURATIONS + ".tmp", DURATIONS)
    except OSError:
        pass


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-j", "--jobs", type=int, default=int(os.environ.get("AGENTCTL_TEST_JOBS") or min(os.cpu_count() or 2, 8)))
    ap.add_argument("--timeout", type=int, default=int(os.environ.get("AGENTCTL_TEST_TIMEOUT") or 60))
    ap.add_argument("--suite-timeout", type=int, default=600)
    ap.add_argument("--durations", type=int, default=0, metavar="K", help="print the K slowest tests")
    ap.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--list", action="store_true", help="print the test ids and exit")
    ap.add_argument("patterns", nargs="*")
    a = ap.parse_args()
    os.environ["AGENTCTL_TEST_TIMEOUT"] = str(a.timeout)
    if a.worker:
        return worker(a.timeout)
    # The parent never imports the tests (their import sets the per-process roots), it asks a child for the list.
    env = {k: v for k, v in os.environ.items() if k not in ("AGENTCTL_TEST_RUN_TMP", "AGENTCTL_STATE_ROOT", "TMPDIR")}
    listing = subprocess.run([sys.executable, "-c", f"import sys; sys.argv=['run.py']; sys.path.insert(0, {HERE!r}); "
                              f"import run; print('\\n'.join(run.list_tests({a.patterns!r})))"],
                             capture_output=True, text=True, env=env, cwd=HERE, timeout=120)
    if listing.returncode:
        sys.stderr.write(listing.stderr)
        return 1
    classes = [c for c in listing.stdout.split() if c]  # test ids
    if a.list:
        print("\n".join(classes))
        return 0
    past = load_durations()
    order = sorted(classes, key=lambda c: -past.get(c, 5.0))
    lock, todo, results, t0 = threading.Lock(), list(order), [], time.monotonic()
    procs = []

    def drive(i):
        p = subprocess.Popen([sys.executable, os.path.abspath(__file__), "--worker", "--timeout", str(a.timeout)],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=open(os.devnull, "w")
                             if not os.environ.get("AGENTCTL_TEST_VERBOSE") else None, text=True, env=env, cwd=HERE)
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
                p = subprocess.Popen(p.args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, text=True, env=env, cwd=HERE)
                procs.append(p)
                continue
            r = json.loads(line)
            with lock:
                results.append(r)
                mark = "." if not r["failures"] else "F"
                sys.stdout.write(mark)
                sys.stdout.flush()
        p.stdin.close()
        p.wait()

    threads = [threading.Thread(target=drive, args=(i,), daemon=True) for i in range(max(1, a.jobs))]
    for t in threads:
        t.start()
    deadline = t0 + a.suite_timeout
    for t in threads:
        t.join(max(0, deadline - time.monotonic()))
    timed_out = any(t.is_alive() for t in threads)
    if timed_out:
        for p in procs:
            try:
                p.kill()
            except OSError:
                pass
    print()
    nrun = sum(r["run"] for r in results)
    fails = [f for r in results for f in r["failures"]]
    for tid, tb in fails:
        print("=" * 70 + f"\nFAIL: {tid}\n" + "-" * 70 + f"\n{tb}")
    if timed_out:
        print(f"SUITE TIMEOUT after {a.suite_timeout} s; not run: {', '.join(todo) or '-'}")
    if not a.patterns:
        save_durations({r["cls"]: r["secs"] for r in results})
    if a.durations:
        times = sorted(((s, t) for r in results for t, s in r["times"].items()), reverse=True)[:a.durations]
        print("slowest:\n" + "\n".join(f"  {s:6.1f}s {t}" for s, t in times))
    took = time.monotonic() - t0
    skipped = sum(r["skipped"] for r in results)
    print(f"Ran {nrun} tests in {took:.1f}s ({a.jobs} workers)" +
          (f", {skipped} skipped" if skipped else ""))
    ok = not fails and not timed_out and len(results) == len(classes)
    print("OK" if ok else f"FAILED (failures={len(fails)})")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
