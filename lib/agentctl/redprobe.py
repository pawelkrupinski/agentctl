# agentctl, part `redprobe`: `agentctl red probe`: a blame probe's checks of one commit, the drift seeds replayed on its overlay
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import os
import re
import subprocess
import time
from agentctl.core import env_num, err, git, remote, slug
from agentctl.timefmt import ts
from agentctl.statefile import write_json
from agentctl.forge import GH_JSON_TIMEOUT, forge_argv, forge_env
from agentctl.owned import backoff
from agentctl.remote import delete_check_branch, remote_poll
from agentctl.remotejobs import REMOTE_WORKFLOW


BLAME_PROBE_WALL = 2 * 3600      # s one probe may run before it is no verdict ($AGENTCTL_BLAME_PROBE_WALL)


def drift_ci_path(repo):
    return os.environ.get("AGENTCTL_DRIFT_CI") or os.path.join(repo, "scripts", "identity-drift-ci")


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
        return subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, env=env)
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


def probe_drift(repo, commit, run, wall):
    """The red run's failing drift rows replayed on `commit` with THE RED RUN'S OWN SEEDS (identity-drift-ci
    --for-red), on drift_overlay's commit (main's CI over its code). The replayed seed is checked against the pinned one
    on every row (drift_seed_check): a mismatch is NO VERDICT, never a pass. DRIFT CLEAN is a pass, DRIFT RED or DRIFT
    PROGRESS (fewer flips, still some) a fail, anything else no verdict."""
    probe = drift_overlay(repo, commit)
    if not probe:
        return {"verdict": "none", "evidence": f"could not lay {remote()}/main's CI over {commit[:10]}"}
    try:
        r = subprocess.run([drift_ci_path(repo), probe, "--for-red", str(run), "--timeout", str(int(wall // 60))],
                           cwd=repo, capture_output=True, text=True, timeout=wall + 120, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as e:
        return {"verdict": "none", "evidence": f"identity-drift-ci: {str(e)[:160]}"}
    last = (r.stdout.strip().splitlines() or [""])[-1].strip()
    v = "pass" if last.startswith("DRIFT CLEAN") else "fail" if last.startswith(("DRIFT RED", "DRIFT PROGRESS")) else "none"
    said = f"identity-drift-ci {probe[:10]} ({commit[:10]} + main's CI) --for-red {run}: {last[:200] or f'exit {r.returncode}'}"
    if v != "none":
        wrong = drift_seed_check((r.stdout or "") + "\n" + (r.stderr or ""))
        if wrong:
            return {"verdict": "none", "evidence": f"{said}; NOT THE RED'S SEEDS: {wrong}"}
        return {"verdict": v, "evidence": said, "seeds": True}  # every row replayed the seed it pinned
    return {"verdict": v, "evidence": said}


def probe_checks(repo, o_r, commit, run, tokens, wall):
    """The boundary's TREE through precommit-check.yml on CI: a commit holding it on top of <remote>/main pushed as
    `check/blame-<run>-<sha>` (the change's paths -- everything after the boundary, undone -- select the checks, as
    the known-red baseline does). A pass counts only when the run's log names the failing test (else the check never
    ran it: no verdict); a failure is a fail (the parent fails: never reverted on it)."""
    git(repo, "fetch", "--quiet", remote(), "main", check=False)
    tree = git(repo, "rev-parse", f"{commit}^{{tree}}", check=False).stdout.strip()
    if not tree:
        return {"verdict": "none", "evidence": f"{commit[:10]} unknown here"}
    tip = git(repo, "commit-tree", tree, "-p", f"{remote()}/main", "-m",
              f"blame probe: the tree of {commit} (red run {run})", check=False).stdout.strip()
    branch = f"check/blame-{run}-{commit[:10]}"
    r = git(repo, "push", "--quiet", "--force", remote(), f"{tip}:refs/heads/{branch}", check=False)
    if r.returncode != 0:
        return {"verdict": "none", "evidence": f"push of {branch} failed: {r.stderr.strip()[:200]}"}
    rem = {"branch": branch, "tip": tip, "run": None, "status": None}
    t0 = time.time()
    try:
        for delay in backoff(30, 300):
            try:
                rem = remote_poll(o_r, rem)
            except (Exception, SystemExit) as e:
                err(f"blame probe {branch}: {str(e)[:160]}")
            if rem.get("status") or time.time() - t0 > wall:
                break
            time.sleep(delay)
    finally:
        delete_check_branch(o_r, branch)
    st = rem.get("status")
    name = f"{REMOTE_WORKFLOW} run {rem.get('run')} on the tree of {commit[:10]}"
    if st == "failure":
        return {"verdict": "fail", "evidence": f"{name}: failure"}
    if st != "success":
        return {"verdict": "none", "evidence": f"{name}: {st or 'undecided'}"}
    if not tokens:
        return {"verdict": "none", "evidence": f"{name}: success, but the red names no test to look for in its log"}
    try:
        log = subprocess.run(forge_argv(o_r, "run", "view", str(rem["run"]), "--log"), capture_output=True, text=True,
                             env=forge_env(o_r), timeout=GH_JSON_TIMEOUT).stdout
    except (OSError, subprocess.TimeoutExpired):
        log = ""
    seen = [t for t in tokens if t in log]
    if not seen:
        return {"verdict": "none", "evidence": f"{name}: success, but it never ran {', '.join(tokens)}"}
    return {"verdict": "pass", "evidence": f"{name}: success, ran {', '.join(seen)}"}


def cmd_red_probe(a):
    """`agentctl red probe`: one boundary's verdict on CI, written to --out (the red tick reads it)."""
    wall = env_num("AGENTCTL_BLAME_PROBE_WALL", BLAME_PROBE_WALL)
    try:
        if a.kind == "drift":
            v = probe_drift(a.repo, a.commit, a.run, wall)
        elif a.kind == "checks":
            v = probe_checks(a.repo, slug(a.repo), a.commit, a.run, [t for t in (a.tokens or "").split() if t], wall)
        else:
            v = {"verdict": "none", "evidence": f"no probe of kind {a.kind}"}
    except (Exception, SystemExit) as e:
        v = {"verdict": "none", "evidence": f"{type(e).__name__}: {str(e)[:200]}"}
    write_json(a.out, {**v, "at": ts(), "commit": a.commit, "kind": a.kind})
    print(f"{v['verdict']}: {v.get('evidence')}")
    return 0
