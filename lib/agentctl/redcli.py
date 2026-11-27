# agentctl, part `redcli`: `agentctl red <sub>`: the router and every subcommand -- status (its text from the status
# model, and the snapshot the tick writes of it), block, claim, owner push, dispatch, unblame, and probe (a blame probe's
# checks of one commit, the drift seeds replayed on its overlay)
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from functools import partial
import os
from typing import Any, Callable
import re
import subprocess
from agentctl.context import Context
from agentctl.core import attempt, err, git, reds_dir, remote, slug, tilde
from agentctl.fixerrecords import agent_name, own_fixer_record_path
from agentctl.owned import backoff, default_repo
from agentctl.fixerblockers import _blocker_cache, dispatch_blocker, in_fixer
from agentctl.fixerlife import red_fixer
from agentctl.ownerwrapper import red_owner
from agentctl.timefmt import ts
from agentctl.repos import RED_PRIMARY, red_repo_short, scan_repo_for
from agentctl.redrecord import red_what
from agentctl.worktrees import claimant
from agentctl.red import red_put_offer
from agentctl.redopen import red_offer
from agentctl.redclaim import bind_fixer_session, red_beat, red_claim, red_owner_push, red_release, red_targets
from agentctl.blamerecords import blame_withdraw
from agentctl.redvault import red_block, red_unblock
from agentctl.reddispatch import repo_dispatch_off_all
from agentctl.offerdispatch import red_dispatcher
from agentctl.redview import RED_STATUS_FRESH, red_status_snapshot_path, repo_dispatch_set
from agentctl.procrunner import process_runner
from agentctl.statefile import read_record, write_json
from agentctl.redknobs import knob_drift_ci
from agentctl.blameknobs import knob_blame_probe_wall
from agentctl.forge import GH_JSON_TIMEOUT, forge_argv, forge_env
from agentctl.remotejobs import REMOTE_WORKFLOW
from agentctl.remote import delete_check_branch, remote_poll
from agentctl.clock import epoch_now
from agentctl.reverts import revert_line
from agentctl.status import status_data, status_store
from agentctl.redhold import red_fix_liveness


def red_am_fixer():
    """`agentctl red am-fixer`: 0 inside a dispatched red fixer -- known by its session's binding or record, else (no
    Claude session) its fixer environment (74e2ec5's in_fixer) -- printing who; 1 for anybody else."""
    if in_fixer():
        print(agent_name() or own_fixer_record_path() or "a red fixer")
        return 0
    return 1


def cmd_red(a, clock, st, registry, *, ctx):
    if a.rcmd == "am-fixer":
        return red_am_fixer()
    if a.rcmd == "probe":
        return cmd_red_probe(a, clock)
    o_r = slug(a.repo) if getattr(a, "repo", None) and a.rcmd not in ("fixer", "owner") else default_repo()
    handlers: dict[str, Callable[..., Any]] = {"block": lambda a, st, o_r: red_block(st, o_r, a, registry, ctx=ctx),
                "unblock": partial(red_cmd_unblock, ctx=ctx), "owner-push": partial(red_cmd_owner_push, ctx=ctx),
                "claim": partial(red_cmd_lease, ctx=ctx), "beat": partial(red_cmd_lease, ctx=ctx),
                "release": partial(red_cmd_lease, ctx=ctx), "status": partial(red_cmd_status, ctx=ctx),
                "auto": red_cmd_auto, "owner": lambda a, st, o_r: red_owner(a, st),
                "fixer": lambda a, st, o_r: red_fixer(a, st), "unblame": partial(red_cmd_unblame, ctx=ctx),
                "dispatch": partial(red_cmd_dispatch, ctx=ctx)}
    return handlers[a.rcmd](a, st, o_r) if a.rcmd in handlers else 2


def red_cmd_unblock(a: Any, st: Any, o_r: Any, *, ctx: Context) -> Any:
    rc, lines = red_unblock(st, o_r, a.run, a.why, ctx=ctx)
    print("\n".join(lines))
    return rc


def red_cmd_owner_push(a: Any, st: Any, o_r: Any, *, ctx: Context) -> Any:
    rc, lines = red_owner_push(st, o_r, a.run, a.note or "", getattr(a, "worktree", None), ctx=ctx)
    print("\n".join(lines))
    return rc


def red_cmd_lease(a: Any, st: Any, o_r: Any, *, ctx: Context) -> Any:
    """`red claim|beat|release`: the lease's line(s) and exit code."""
    if a.rcmd == "claim":
        if not claimed_by_its_fixer(a):
            return 2
        rc, lines = red_claim(st, o_r, a.run, a.note or "", getattr(a, "worktree", None), ctx=ctx)
    elif a.rcmd == "beat":
        rc, lines = red_beat(st, o_r, a.run, a.note or "", ctx=ctx)
    else:
        rc, lines = red_release(st, o_r, a.run, a.any, ctx=ctx)
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


def red_cmd_status(a: Any, st: Any, o_r: Any, *, ctx: Context) -> Any:
    return red_status(st, getattr(a, "verbose", False), getattr(a, "live", False), ctx=ctx)


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


def red_cmd_unblame(a: Any, st: Any, o_r: Any, *, ctx: Context) -> Any:
    items = red_targets(st, o_r, a.run, create=False, ctx=ctx)
    if not items:
        print(f"NOT WITHDRAWN: #{a.run} names no red")
        return 1
    rc = 0
    for it in items:
        ok, line = blame_withdraw(st, it, a.why, scan_repo_for(o_r, None))
        print(line)
        rc = rc or (0 if ok else 1)
    return rc


def red_cmd_dispatch(a: Any, st: Any, o_r: Any, *, ctx: Context) -> Any:
    """`red dispatch <run>`: its reds offered at top priority, the dispatcher run now."""
    b = dispatch_blocker()
    if b:
        print(f"NOT DISPATCHED: {b}")
        return 2
    items = red_targets(st, o_r, a.run, ctx=ctx)
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


def red_lines(reds: list[dict[str, Any]], verbose: bool) -> list[str]:
    """The open reds, each with its history when verbose."""
    out = [f"reds ({len(reds)} open):" if reds else "reds: none open"]
    for r in reds:
        out.append(f"  {r['state']:<16} {r['what']} {r['key']}" + (f" [{r['family']}]" if r.get("family") else "")
                   + f": {r['text']}")
        if verbose:
            out += [f"      {h.get('at')} {h.get('holder')}: {str(h.get('outcome'))[:140]}" for h in r["history"]]
    return out


def revert_lines(rv: Any) -> list[str]:
    """The reverted changes their landers have not acknowledged (reverts.py), or why they are unread."""
    if isinstance(rv, dict) and "error" in rv:
        return [f"reverts: unread ({rv['error']})"]
    if not rv:
        return []
    return [f"reverts ({len(rv)} unacked: `agentctl revert ack <id> --rework|--drop`):"] + [revert_line(r, epoch_now()) for r in rv]


def untracked_lines(never: Any) -> list[str]:
    """The workflows never green, for information only: no red, no fixer, no alert (reduntracked.py)."""
    if not (isinstance(never, list) and never):
        return []
    out = [f"untracked (never green) ({len(never)}):"]
    for u in never:
        short = "" if u.get("repo") == RED_PRIMARY else f"{red_repo_short(u.get('repo') or '')} "
        out.append(f"  {short}{u.get('workflow')} #{(u.get('run') or {}).get('number')}"
                   + (f" [{', '.join(u.get('jobs') or [])}]" if u.get("jobs") else "") + f": {u.get('why')}")
    return out


def family_line(f: dict[str, Any]) -> str:
    """A red family's line: its ONE fixer, an orphaned lease revoked, or none at work."""
    if f.get("holder") and f.get("at_work") is False:  # never "leased ... none at work": orphaned, re-offered
        who = f"ORPHANED LEASE ({f['holder']}: {f.get('work')}) -> revoked and re-offered at the next tick"
    elif f.get("holder"):
        who = (f"{f['holder']} ({tilde(str(f.get('worktree') or '?'))})"
               + (f": {f['work']}" if f.get("at_work") else ""))
    else:
        who = "none at work"
    return f"family {f['family']}: {f['open']} open red(s), ONE fixer: {who}"


def dispatcher_lines(dp: dict[str, Any]) -> list[str]:
    """The dispatcher: on or off and why, the fixers alive, the repos switched off, the laptop's pressure."""
    if "error" in dp:
        return [f"dispatcher: unread ({dp['error']})"]
    out = [f"dispatcher: {'OFF -- ' + dp['blocker'] if dp.get('blocker') else 'ON'}; fixers alive: "
           f"{dp['alive']}/{dp['cap']}; gate reds never wait (no slot cap, no laptop pressure)"]
    for r, rec in sorted((dp.get("repos_off") or {}).items()):
        out.append(f"dispatch off for {r} (owner) since {rec.get('at')}: its reds recorded, never offered or asked")
    if dp.get("pressure"):
        out.append(f"laptop: {dp['pressure']} (holds other work only, never a red fix)")
    return out


def unread_sections(m: dict[str, Any]) -> dict[str, Any]:
    """The families and owners sections that came back as an error (a timed-out section, status_sources, is no list)."""
    return {k: m[k]["error"] for k in ("families", "owners") if isinstance(m.get(k), dict) and "error" in m[k]}


def holder_lines(m: dict[str, Any], unread: dict[str, Any], verbose: bool) -> list[str]:
    """The families' ONE fixers and the owners' lines, each section left out when it was unread."""
    out = [] if "families" in unread else [family_line(f) for f in m.get("families") or []]
    for o in [] if "owners" in unread else m.get("owners") or []:
        out += owner_status_lines(o, verbose) if o.get("worktree") else []
    return out


def render_red_status(m: dict[str, Any], verbose: bool = False) -> str:
    """`agentctl red status` from the model's reds, families and dispatcher sections."""
    reds = m["reds"]
    if isinstance(reds, dict) and "error" in reds:
        return f"reds: unread ({reds['error']})"
    unread = unread_sections(m)
    out = red_lines(reds, verbose) + revert_lines(m.get("reverts")) + [f"{k}: unread ({e})" for k, e in unread.items()]
    out += untracked_lines(m.get("untracked")) if verbose else []
    return "\n".join(out + holder_lines(m, unread, verbose) + dispatcher_lines(m.get("dispatcher") or {}))


def owner_status_lines(o: dict[str, Any], verbose: bool) -> list[str]:
    """An owner's line; verbose, its session's context and its last rollovers to a fresh session (sessionroll)."""
    line = (f"owner {o['family']}: {'ALIVE' if o['alive'] else 'not running'}, turn {o.get('turn') or 0}, "
            f"{o.get('reds') or 0} red(s) in hand, {o.get('restarts') or 0} restart(s); "
            f"{tilde(str(o['worktree']))}" + (f" (ended: {o['verdict']})" if o.get("ended") else ""))
    return [line] + (owner_context_lines(o) if verbose else [])


def owner_context_lines(o: dict[str, Any]) -> list[str]:
    """The owner's session context at its last resume check, and its last three rollovers."""
    return ([f"      context at its last resume check: {o.get('context') or 'unread'} tokens"]
            + [f"      rollover {r.get('at')}: {str(r.get('old'))[:8]} -> {str(r.get('new') or 'starting')[:8]} "
               f"({r.get('tokens')} tokens)" for r in (o.get("rollovers") or [])[-3:]])


def red_status_text(store: Any, verbose: bool = False, *, ctx: Context) -> str:
    """`agentctl red status`'s text, from the status model (its reds, families and dispatcher sections)."""
    t = store.clock() if hasattr(store, "clock") else epoch_now()
    try:
        stalled = red_fix_liveness(store, t, ctx=ctx)
    except (Exception, SystemExit):
        stalled = None
    return (f"{stalled}\n" if stalled else "") + render_red_status(status_data(store=store, now_=t, only=("reds", "families", "owners", "dispatcher",
                                                                    "untracked", "reverts")), verbose)


def write_red_status_snapshot(store: Any, *, ctx: Context) -> None:
    """The sync's step: the rendered red status, for `red status` to print at once."""
    write_json(red_status_snapshot_path(), {"at": epoch_now(), "text": red_status_text(store or status_store(), ctx=ctx)})


def red_status(store: Any, verbose: bool = False, live: bool = False, *, ctx: Context) -> int:
    """`agentctl red status`: the sync's fresh snapshot, else read now."""
    snap = {} if verbose or live else (read_record(red_status_snapshot_path()) or {})
    age = epoch_now() - float(snap.get("at") or 0)
    if snap.get("text") and 0 <= age <= RED_STATUS_FRESH:
        print(f"{snap['text']}\n(as of {age:.0f} s ago: the sync's snapshot; `agentctl red status --live` reads the store)")
        return 0
    print(red_status_text(store, verbose, ctx=ctx))
    return 0
