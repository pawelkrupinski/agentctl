#!/usr/bin/env python3
"""tests/stress.py [--range A..B] [--repeat K] [--budget S]: THE STRESS LANE of the pre-push hook (2026-11-25: an
intermittent test slipped through one green run and failed on main, a fixer's lease meta read None). The tests the push
touches -- the test files it changes, and the tests of every lib/agentctl module it changes (lib/agentctl/<m>.py ->
tests/test_<m>.py) -- run K more times each, in a fresh shuffled order (the seed is printed, tests/run.py --seed replays
it), under a test clock that starts AGENTCTL_TEST_CLOCK_LAG s behind the wall, raised; a newly flaky test fails the
push, not main. Bounded: tests/run.py --budget stops the dispatch after S seconds (the rest is no failure)."""
import argparse, os, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
# s, against the default 400: past every fixture's own window (a block lease's 10 min, the queue's 300-s deadlines);
# test_reds' fixtures once failed from 600 s because they read the wall, not the test clock (2026-11-25).
STRESS_CLOCK_LAG = "900"


def changed_paths(rng):
    r = subprocess.run(["git", "diff", "--name-only", rng], capture_output=True, text=True, cwd=ROOT)
    return [p for p in r.stdout.split() if p] if r.returncode == 0 else []


def patterns_for(paths, tests_dir=HERE):
    """The run.py patterns of the tests `paths` touch: `test_<m>.` for a changed tests/test_<m>.py and for the test
    module of a changed lib/agentctl/<m>.py when it exists."""
    out = []
    for p in paths:
        d, name = os.path.split(p)
        if not name.endswith(".py"):
            continue
        stem = name[:-3]
        test = stem if d == "tests" and stem.startswith("test_") else f"test_{stem}" if d == "lib/agentctl" else None
        if test and os.path.exists(os.path.join(tests_dir, test + ".py")) and f"{test}." not in out:
            out.append(f"{test}.")
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--range", default="HEAD~1..HEAD")
    ap.add_argument("--repeat", type=int, default=5)
    ap.add_argument("--budget", type=float, default=45)
    ap.add_argument("--print-patterns", action="store_true")
    a = ap.parse_args(argv)
    pats = patterns_for(changed_paths(a.range))
    if a.print_patterns or not pats:
        print(" ".join(pats) if pats else "stress: no touched tests")
        return 0
    env = {**os.environ, "AGENTCTL_TEST_CLOCK_LAG": os.environ.get("AGENTCTL_STRESS_CLOCK_LAG") or STRESS_CLOCK_LAG}
    cmd = [sys.executable, os.path.join(HERE, "run.py"), "--repeat", str(a.repeat), "--budget", str(a.budget), *pats]
    print(f"stress: {' '.join(pats)} x{a.repeat}, clock lag {env['AGENTCTL_TEST_CLOCK_LAG']} s, budget {a.budget:.0f} s", flush=True)
    return subprocess.run(cmd, env=env, cwd=ROOT).returncode


if __name__ == "__main__":
    sys.exit(main())
