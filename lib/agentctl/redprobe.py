# agentctl, part `redprobe`: `red probe`: replaying a red's drift seeds and checks at a commit
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import os
import subprocess
from typing import Any
from agentctl.procrunner import process_runner
from agentctl.escalationtable import re
from agentctl.statefile import write_json
from agentctl.timefmt import ts
from agentctl.redknobs import knob_drift_ci
from agentctl.blameknobs import knob_blame_probe_wall
from agentctl.core import remote
from agentctl.repos import slug
from agentctl.redprobechecks import probe_checks


def drift_ci_path(repo):
    return knob_drift_ci() or os.path.join(repo, "scripts", "identity-drift-ci")


DRIFT_OVERLAY = (".github", "scripts/ci")  # main's CI laid over an old commit, so its drift rows honour forced seeds


DRIFT_PINS = re.compile(r"seeds (\d*,\d*) \(the red's own\)")


DRIFT_ROW = re.compile(r"^takes-drift-(\d+) \([^)]*\) \[seed ([^\]]*)\]:", re.M)


def drift_overlay(repo, commit):
    """A commit holding `commit`'s code with <remote>/main's .github/ and scripts/ci/ laid over it (its parent: commit),
    or None. An old commit's drift script ignores a forced seed and silently replays its default (2026-11-01: four
    "parent" runs on 73f1dcfcbb replayed seed 2 and proved nothing); with main's CI it replays the seed asked for."""
    import shutil, tempfile
    idx = os.path.join(tempfile.mkdtemp(prefix="agentctl-overlay-"), "index")
    env = {**os.environ, "GIT_INDEX_FILE": idx}

    def g(*args):
        return process_runner().run(["git", "-C", repo, *args], capture_output=True, text=True, env=env)
    try:
        if g("read-tree", commit).returncode != 0:
            return None
        for d in DRIFT_OVERLAY:
            # -f: outside a checkout of `commit` its files differ from the worktree and HEAD, and a plain
            # `rm --cached` refuses ("staged content different from both the file and the HEAD")
            if g("rm", "-r", "-q", "-f", "--cached", "--ignore-unmatch", d).returncode != 0:
                return None
            if g("cat-file", "-e", f"{remote()}/main:{d}").returncode == 0 and \
                    g("read-tree", f"--prefix={d}/", f"{remote()}/main:{d}").returncode != 0:
                return None
        tree = g("write-tree").stdout.strip()
        r = g("commit-tree", tree, "-p", commit, "-m", f"blame probe: {commit} with {remote()}/main's "
              f"{' and '.join(DRIFT_OVERLAY)} (its drift rows honour the forced seed)")
        return r.stdout.strip() if r.returncode == 0 and tree else None
    finally:
        shutil.rmtree(os.path.dirname(idx), ignore_errors=True)


def drift_seed_check(out):
    """None when every row identity-drift-ci reports replayed the seed it pinned (`seeds S1,S2 (the red's own)`; a row
    names its seed on its verdict line, `takes-drift-N (<country>) [seed S]: ...`), else why not."""
    m = DRIFT_PINS.search(out)
    rows = DRIFT_ROW.findall(out)
    if not m:
        return "the run never said which seeds it pinned"
    if not rows:
        return "no drift row named the seed it replayed"
    pins = m.group(1).split(",")
    bad = [f"row {n} replayed seed {seed}, not {pins[int(n) - 1]}" for n, seed in rows
           if int(n) - 1 >= len(pins) or not pins[int(n) - 1] or seed.strip() != pins[int(n) - 1]]
    return "; ".join(bad) or None


def _seed_checked(v: Any, said: Any, r: Any) -> Any:
    """The probe's verdict dict: a pass or fail stands only when every row replayed the seed it pinned."""
    if v == "none":
        return {"verdict": v, "evidence": said}
    wrong = drift_seed_check((r.stdout or "") + "\n" + (r.stderr or ""))
    if wrong:
        return {"verdict": "none", "evidence": f"{said}; NOT THE RED'S SEEDS: {wrong}"}
    return {"verdict": v, "evidence": said, "seeds": True}


def probe_drift(repo, commit, run, wall):
    """The red run's failing drift rows replayed on `commit` with THE RED RUN'S OWN SEEDS (identity-drift-ci
    --for-red), on drift_overlay's commit (main's CI over its code). The replayed seed is checked against the pinned one
    on every row (drift_seed_check): a mismatch is NO VERDICT, never a pass. DRIFT CLEAN is a pass, DRIFT RED or DRIFT
    PROGRESS (fewer flips, still some) a fail, anything else no verdict."""
    probe = drift_overlay(repo, commit)
    if not probe:
        return {"verdict": "none", "evidence": f"could not lay {remote()}/main's CI over {commit[:10]}"}
    try:
        r = process_runner().run([drift_ci_path(repo), probe, "--for-red", str(run), "--timeout", str(int(wall // 60))],
                           cwd=repo, capture_output=True, text=True, timeout=wall + 120, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as e:
        return {"verdict": "none", "evidence": f"identity-drift-ci: {str(e)[:160]}"}
    last = (r.stdout.strip().splitlines() or [""])[-1].strip()
    v = "pass" if last.startswith("DRIFT CLEAN") else "fail" if last.startswith(("DRIFT RED", "DRIFT PROGRESS")) else "none"
    said = f"identity-drift-ci {probe[:10]} ({commit[:10]} + main's CI) --for-red {run}: {last[:200] or f'exit {r.returncode}'}"
    return _seed_checked(v, said, r)


def cmd_red_probe(a, clock, runner):
    """`agentctl red probe`: one boundary's verdict on CI, written to --out (the red tick reads it)."""
    wall = knob_blame_probe_wall()
    try:
        if a.kind == "drift":
            v = probe_drift(a.repo, a.commit, a.run, wall)
        elif a.kind == "checks":
            v = probe_checks(a.repo, slug(a.repo), a.commit, a.run, [t for t in (a.tokens or "").split() if t], wall,
                             clock, runner)
        else:
            v = {"verdict": "none", "evidence": f"no probe of kind {a.kind}"}
    except (Exception, SystemExit) as e:
        v = {"verdict": "none", "evidence": f"{type(e).__name__}: {str(e)[:200]}"}
    write_json(a.out, {**v, "at": ts(), "commit": a.commit, "kind": a.kind})
    print(f"{v['verdict']}: {v.get('evidence')}")
    return 0
