#!/usr/bin/env python3
"""The e2e layer's runner: every scenario of tests/e2e/test_e2e.py in its own process, N at a time, under ONE governor
ticket (THE GOVERNED FAN-OUT, lib/agentctl/govpool.py: under a ticket already -- the pre-push hook's govstep -- it takes
none and runs at most that wide), each bounded.

  python3 tests/e2e/run.py [-j N] [--bound S] [PATTERN...]

Each scenario is one test class (its own World: a store server, a reconciler, a laptop, a remote, a stub GitHub); a
scenario's harness keeps every process it starts and ends them by pid. Exit 0 when every scenario passed."""
import argparse, os, subprocess, sys, time, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(HERE)), "lib"))
from agentctl.govpool import governed, run_main  # noqa: E402
sys.path.insert(0, HERE)
from patience import HARD_FACTOR, Patience, SpawnSlowdown  # noqa: E402

SCENARIO_BOUND = 150  # s of patience one scenario has (patience.py: at the machine's pace; its steps have tighter ones: e2e_harness STEP_BOUND)


def scenarios(patterns):
    """The test ids of test_e2e, by class (one process per class: a skew class unpacks its old revision once)."""
    sys.path.insert(0, HERE)
    import test_e2e
    ids = set()
    for t in unittest.TestLoader().loadTestsFromModule(test_e2e):
        for case in t:
            ids.add(f"test_e2e.{type(case).__name__}")
    return sorted(i for i in ids if not patterns or any(p in i for p in patterns))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("-j", "--jobs", type=int, default=4)
    ap.add_argument("--bound", type=float, default=SCENARIO_BOUND)
    ap.add_argument("patterns", nargs="*")
    a = ap.parse_args()
    todo = scenarios(a.patterns)
    env = dict(os.environ, AGENTCTL_TEST_TIMEOUT=str(int(a.bound * HARD_FACTOR) - 10), PYTHONDONTWRITEBYTECODE="1")
    results = {}
    with governed("e2e", min(a.jobs, len(todo)), f"tests/e2e/run.py {os.path.dirname(os.path.dirname(HERE))}") as pool:
        width, env = max(1, pool.width), pool.env(env)
        t0 = time.monotonic()
        running, queue, slowdown = {}, list(todo), SpawnSlowdown()
        while queue or running:
            while queue and len(running) < width:
                tid = queue.pop(0)
                running[tid] = (subprocess.Popen([sys.executable, "-m", "unittest", "-v", tid], cwd=HERE, env=env,
                                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                                 start_new_session=True), Patience(a.bound, slowdown=slowdown))
            for tid, (p, patience) in list(running.items()):
                try:
                    out, _ = p.communicate(timeout=0.2)
                except subprocess.TimeoutExpired:
                    if not patience.expired():
                        continue
                    os.killpg(p.pid, 9)
                    out, _ = p.communicate()
                    out += f"\nSCENARIO BOUND: {tid} still running after {a.bound:.0f} s of patience (killed)\n"
                    p.returncode = 1
                results[tid] = (p.returncode, out, time.monotonic() - patience.started)
                del running[tid]
                print(f"{'ok  ' if p.returncode == 0 else 'FAIL'} {tid} ({results[tid][2]:.0f} s)", flush=True)
        took = time.monotonic() - t0
    failed = [t for t, (rc, _o, _s) in results.items() if rc != 0]
    for t in failed:
        print("=" * 70 + f"\nFAIL: {t}\n" + "-" * 70 + "\n" + results[t][1][-8000:])
    print(f"Ran {len(results)} e2e scenarios in {took:.1f}s ({width} at a time)")
    print("OK" if not failed and results else f"FAILED ({len(failed)} of {len(results)})")
    return 0 if not failed and results else 1


if __name__ == "__main__":
    sys.exit(run_main(main))
