# agentctl, part `redcli`: `agentctl red <sub>`: the router and every subcommand -- block, claim, owner push, dispatch,
# unblame, and probe (a blame probe's checks of one commit, the drift seeds replayed on its overlay)
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import os
from typing import Any
import re
import subprocess
from agentctl.core import attempt, err, git, reds_dir, remote, slug
from agentctl.fixerrecords import agent_name, own_fixer_record_path
from agentctl.owned import backoff, default_repo
from agentctl.fixerblockers import _blocker_cache, dispatch_blocker, in_fixer
from agentctl.fixerlife import red_fixer
from agentctl.ownerwrapper import red_owner
from agentctl.timefmt import ts
from agentctl.repos import scan_repo_for
from agentctl.redrecord import red_what
from agentctl.worktrees import claimant
from agentctl.redmove import red_put_offer
from agentctl.redopen import red_offer
from agentctl.redclaim import bind_fixer_session, red_beat, red_claim, red_owner_push, red_release, red_targets
from agentctl.blamerecords import blame_withdraw
from agentctl.redvault import red_block, red_unblock
from agentctl.reddispatch import repo_dispatch_off_all
from agentctl.offerdispatch import red_dispatcher
from agentctl.redview import repo_dispatch_set
from agentctl.redstatus import red_status
from agentctl.procrunner import process_runner
from agentctl.statefile import write_json
from agentctl.redknobs import knob_drift_ci
from agentctl.blameknobs import knob_blame_probe_wall
from agentctl.forge import GH_JSON_TIMEOUT, forge_argv, forge_env
from agentctl.remotejobs import REMOTE_WORKFLOW
from agentctl.remote import delete_check_branch, remote_poll


def red_am_fixer():
    """`agentctl red am-fixer`: 0 inside a dispatched red fixer -- known by its session's binding or record, else (no
    Claude session) its fixer environment (74e2ec5's in_fixer) -- printing who; 1 for anybody else."""
    if in_fixer():
        print(agent_name() or own_fixer_record_path() or "a red fixer")
        return 0
    return 1


def cmd_red(a, clock, st):
    if a.rcmd == "am-fixer":
        return red_am_fixer()
    if a.rcmd == "probe":
        return cmd_red_probe(a, clock)
    o_r = slug(a.repo) if getattr(a, "repo", None) and a.rcmd not in ("fixer", "owner") else default_repo()
    handlers = {"block": red_cmd_block, "unblock": red_cmd_unblock, "owner-push": red_cmd_owner_push,
                "claim": red_cmd_lease, "beat": red_cmd_lease, "release": red_cmd_lease, "status": red_cmd_status,
                "auto": red_cmd_auto, "owner": lambda a, st, o_r: red_owner(a, st),
                "fixer": lambda a, st, o_r: red_fixer(a, st), "unblame": red_cmd_unblame,
                "dispatch": red_cmd_dispatch}
    return handlers[a.rcmd](a, st, o_r) if a.rcmd in handlers else 2


def red_cmd_block(a: Any, st: Any, o_r: Any) -> Any:
    return red_block(st, o_r, a)


def red_cmd_unblock(a: Any, st: Any, o_r: Any) -> Any:
    rc, lines = red_unblock(st, o_r, a.run, a.why)
    print("\n".join(lines))
    return rc


def red_cmd_owner_push(a: Any, st: Any, o_r: Any) -> Any:
    rc, lines = red_owner_push(st, o_r, a.run, a.note or "", getattr(a, "worktree", None))
    print("\n".join(lines))
    return rc


def red_cmd_lease(a: Any, st: Any, o_r: Any) -> Any:
    """`red claim|beat|release`: the lease's line(s) and exit code."""
    if a.rcmd == "claim":
        if not claimed_by_its_fixer(a):
            return 2
        rc, lines = red_claim(st, o_r, a.run, a.note or "", getattr(a, "worktree", None))
    elif a.rcmd == "beat":
        rc, lines = red_beat(st, o_r, a.run, a.note or "")
    else:
        rc, lines = red_release(st, o_r, a.run, a.any)
    print("\n".join(lines))
    return rc


def claimed_by_its_fixer(a: Any) -> Any:
    """The dispatched fixer says who it is (its prompt): its record must be a live one of this session (bound to it).
    False, said, when it is not; True for a claim naming no fixer."""
    sid = os.environ.get("CLAUDE_CODE_SESSION_ID")
    if getattr(a, "fixer", None) and sid and not bind_fixer_session(sid, os.path.realpath(a.fixer)):
        print(f"NOT CLAIMED: {a.fixer} is no live fixer record of this session ({sid[:8]})")
        return False
    return True


def red_cmd_status(a: Any, st: Any, o_r: Any) -> Any:
    return red_status(st, getattr(a, "verbose", False), getattr(a, "live", False))


def red_cmd_auto(a: Any, st: Any, o_r: Any) -> Any:
    """`red auto [on|off]`: the one repo's dispatch (the owner, 2026-11-19: "no bitcashier builds"), in the store: the
    server's tick reads it; else the automatic dispatch as a whole, a file."""
    if getattr(a, "repo", None):
        if a.state in ("on", "off"):
            repo_dispatch_set(st, o_r, a.state == "off", claimant())
        offs = repo_dispatch_off_all(st)
        print(f"dispatch off for {o_r} (owner, since {offs[o_r].get('at')})" if o_r in offs else
              f"dispatch on for {o_r}")
        return 0
    off = reds_dir("DISPATCH_OFF")
    os.makedirs(reds_dir(), exist_ok=True)
    if a.state == "off":
        open(off, "w").write(ts() + "\n")
    elif a.state == "on" and os.path.exists(off):
        os.unlink(off)
    _blocker_cache.clear()
    b = dispatch_blocker()
    print(f"automatic dispatch: {'ON' if not b else 'OFF -- ' + b}")
    return 0


def red_cmd_unblame(a: Any, st: Any, o_r: Any) -> Any:
    items = red_targets(st, o_r, a.run, create=False)
    if not items:
        print(f"NOT WITHDRAWN: #{a.run} names no red")
        return 1
    rc = 0
    for it in items:
        ok, line = blame_withdraw(st, it, a.why, scan_repo_for(o_r, None))
        print(line)
        rc = rc or (0 if ok else 1)
    return rc


def red_cmd_dispatch(a: Any, st: Any, o_r: Any) -> Any:
    """`red dispatch <run>`: its reds offered at top priority, the dispatcher run now."""
    b = dispatch_blocker()
    if b:
        print(f"NOT DISPATCHED: {b}")
        return 2
    items = red_targets(st, o_r, a.run)
    if not items:
        print(f"NOT DISPATCHED: #{a.run} names no red on main")
        return 1
    for it in items:
        offer_by_hand(st, it)
    repo = scan_repo_for(o_r, getattr(a, "repo", None) and os.path.abspath(a.repo))
    lines = red_dispatcher(st, o_r, repo, st.clock())
    print("\n".join(lines) if lines else "NOT DISPATCHED: " + "; ".join(
        (st.offer_of(f"red:{it['id']}") or {}).get("reason") or it["state"] for it in items))
    return 0 if lines else 1


def offer_by_hand(st: Any, it: Any) -> Any:
    """One red offered at top priority (the dispatcher takes it now)."""
    if it["state"] == "red":
        it = red_offer(st, it, f"{red_what(it['data'])}: dispatched by hand")
    store_it = st.item("red", it["id"])
    st.update_item("red", it["id"], store_it["version"], {"priority": -1, "author_tried": True})
    if it["state"] == "offered":
        red_put_offer(st, st.item("red", it["id"]))


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


def _poll_probe_run(o_r: Any, rem: Any, branch: Any, wall: Any, clock: Any) -> Any:
    """rem (the probe branch's remote state) polled until its run has a status or `wall` s pass; the branch is deleted."""
    t0 = clock.time()
    try:
        for delay in backoff(30, 300):
            try:
                rem = remote_poll(o_r, rem, clock.time())
            except (Exception, SystemExit) as e:
                err(f"blame probe {branch}: {str(e)[:160]}")
            if rem.get("status") or clock.time() - t0 > wall:
                break
            clock.sleep(delay)
    finally:
        delete_check_branch(o_r, branch)
    return rem


def probe_checks(repo, o_r, commit, run, tokens, wall, clock):
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
    rem = _poll_probe_run(o_r, rem, branch, wall, clock)
    st = rem.get("status")
    name = f"{REMOTE_WORKFLOW} run {rem.get('run')} on the tree of {commit[:10]}"
    if st == "failure":
        return {"verdict": "fail", "evidence": f"{name}: failure"}
    if st != "success":
        return {"verdict": "none", "evidence": f"{name}: {st or 'undecided'}"}
    if not tokens:
        return {"verdict": "none", "evidence": f"{name}: success, but the red names no test to look for in its log"}
    log = attempt(lambda: process_runner().run(forge_argv(o_r, "run", "view", str(rem["run"]), "--log"), capture_output=True,
                                               text=True, env=forge_env(o_r), timeout=GH_JSON_TIMEOUT).stdout,
                  "", catch=(OSError, subprocess.TimeoutExpired))
    seen = [t for t in tokens if t in log]
    if not seen:
        return {"verdict": "none", "evidence": f"{name}: success, but it never ran {', '.join(tokens)}"}
    return {"verdict": "pass", "evidence": f"{name}: success, ran {', '.join(seen)}"}


def cmd_red_probe(a, clock):
    """`agentctl red probe`: one boundary's verdict on CI, written to --out (the red tick reads it)."""
    wall = knob_blame_probe_wall()
    try:
        if a.kind == "drift":
            v = probe_drift(a.repo, a.commit, a.run, wall)
        elif a.kind == "checks":
            v = probe_checks(a.repo, slug(a.repo), a.commit, a.run, [t for t in (a.tokens or "").split() if t], wall,
                             clock)
        else:
            v = {"verdict": "none", "evidence": f"no probe of kind {a.kind}"}
    except (Exception, SystemExit) as e:
        v = {"verdict": "none", "evidence": f"{type(e).__name__}: {str(e)[:200]}"}
    write_json(a.out, {**v, "at": ts(), "commit": a.commit, "kind": a.kind})
    print(f"{v['verdict']}: {v.get('evidence')}")
    return 0
