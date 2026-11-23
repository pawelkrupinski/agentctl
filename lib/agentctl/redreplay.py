# agentctl, part `redreplay`: a fixer's brief and FIX REPLAY: the replay trailers, agentctl's own replays, the refusal.
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import os
import re
import subprocess
from agentctl.procrunner import process_runner
from agentctl.core import env_num, err, git, remote, state_root
from agentctl.store import open_store
from agentctl.repos import red_repo_conf, red_repo_of
from agentctl.trailers import commit_trailers, trailer_values
from agentctl.land import tool_env
from agentctl.storetoken import no_vault
from agentctl.redrecord import RED_TERMINAL, red_names, red_what
from agentctl.fixerenv import BRIEF
from agentctl.redworkflows import RED_WORKFLOWS_FILE, red_workflow_policy, red_workflows_on_main


def sync_brief():
    """The movies agents' preamble a fixer reads first (~/bin/briefs/movies-agent-preamble.md, $AGENTCTL_BRIEF). The
    fixer's own rules are docs/fixer-brief.md, in its prompt (fixer_brief); nothing is copied from a scratchpad."""
    return os.environ.get("AGENTCTL_BRIEF", BRIEF)


# FIX REPLAY (2026-11-19, Alert backtest #1-#5, 5254bffda9d2): seven fixes landed and were refuted, one per run of the
# backtest -- each fixer landed a guess the nightly then refuted, though the backtest replays cheaply over ci-runner's
# Prometheus snapshot. A workflow whose RED_WORKFLOWS_FILE row names a `replay` command (`{run}`: the red's failing run
# id) holds its fixers to it: a `Fixes-Red:` landing for such a red must carry, in one of its commits, the trailer
# `Fix-Replay: <that command> base=FAIL head=PASS` -- the replay run at origin/main and at the fix. Outside the gate
# that is a WARNING only (policy `fix-replay-outside`: the owner, 2026-11-20, "enforce for gate reds").
#
# GATE REDS (the owner, 2026-11-20: "3 approve and enforce for gate reds"; policy `fix-replay`): a `Fixes-Red:` naming
# a red of the gate (Main, Identity model convergence, Identity takes: the red's `gate`) must carry `Fix-Replay:
# <command>` -- the narrowest command that fails where the red fails -- which AGENTCTL RUNS, at origin/main and at the
# rebased fix, each in a throwaway detached worktree (run_fix_replays, the laptop's land: where the JVM is): it counts
# only when it sees base=FAIL head=PASS, and the proof rides with the submission (`replays`) for the server's check.
# The escape hatch, for a red that genuinely cannot replay (infra, a CI-only race): `Fix-Replay: none — <why>`, a
# SPECIFIC reason (30+ characters, no "n/a", "flaky" or "can't"), in the message's final trailer block (where git reads
# it), listed in the owner's weekly report like the coupling exemptions (movies scripts/ci/fix_replay_exemptions.py,
# the Coupling exemptions workflow).
FIX_REPLAY_TRAILER = re.compile(r"(?i)^Fix-Replay:\s*(.+?)\s+base=(FAIL|PASS)\s+head=(FAIL|PASS)\s*$")
FIX_REPLAY_ANY = re.compile(r"(?i)^Fix-Replay:\s*(.*?)\s*$")
FIX_REPLAY_NONE = re.compile(r"(?i)^none\b\s*(?:—|–|--|-|:)\s*(.*)$")
FIX_REPLAY_NONE_MIN = 30  # characters of a `Fix-Replay: none — <why>` reason
FIX_REPLAY_VAGUE = re.compile(r"(?i)(?<![\w/])n/?a(?![\w/])|flak|can'?t|cannot")
FIX_REPLAY_TIMEOUT = 30 * 60  # s one side of agentctl's replay may run ($AGENTCTL_FIX_REPLAY_TIMEOUT)


def agentctl_revert_fix_replay(reason):
    """The `Fix-Replay: none — <why>` line agentctl's OWN revert of a culprit carries (blame's revert, the fixer's revert
    fallback; 2026-11-21 Main #2806-#2808: blame's revert carried none, FIX REPLAY refused it, main stayed red ~1 h). A
    revert puts main back to code that passed: there is no fix to replay, and the reason names the reverted change and
    the red, so the owner's weekly list reads it like any other."""
    return f"Fix-Replay: none — {reason}\n"


def red_replay_command(repo, d):
    """The replay command for red data d (its run id in place of `{run}`), or None."""
    cmd = red_workflow_policy(repo, d.get("workflow"))["replay"]
    run = (d.get("last_run") or {}).get("databaseId")
    return cmd.replace("{run}", str(run)) if cmd and run else cmd


def fix_replay_trailers(repo, commits):
    """(the names Fixes-Red: gives, [(command, base, head)] of claimed results, [command] agentctl runs, [none
    reasons]) over commits' trailers."""
    named, claimed, bare, nones = set(), [], [], []
    for c in commits or []:
        msg = git(repo, "log", "-1", "--format=%B", c, check=False).stdout
        paras = [p for p in re.split(r"\n\s*\n", msg.strip()) if p.strip()]
        final = set(commit_trailers("-\n\n" + paras[-1])) if len(paras) > 1 else set()
        for ln in commit_trailers(msg):
            k, _, v = ln.partition(":")
            if k.strip().lower() == "fixes-red":
                named |= trailer_values(v)
            m = FIX_REPLAY_TRAILER.match(ln)
            if m:
                claimed.append((m.group(1).strip(), m.group(2).upper(), m.group(3).upper()))
                continue
            m = FIX_REPLAY_ANY.match(ln)
            if not m or not m.group(1):
                continue
            none = FIX_REPLAY_NONE.match(m.group(1))
            if none or m.group(1).lower() == "none":
                if ln in final:  # the escape hatch counts only where git reads it, as the weekly list does
                    nones.append((none.group(1) if none else "").strip())
            elif m.group(1) not in bare:
                bare.append(m.group(1))
    return named, claimed, bare, nones


def fix_replay_none_reasons(repo, commits):
    """The reasons of commits' `Fix-Replay: none — <why>` trailers."""
    return fix_replay_trailers(repo, commits)[3]


def fix_replay_none_lint(reason):
    """Why a `Fix-Replay: none` reason is no reason (too short, or vague), or None."""
    if len(reason or "") < FIX_REPLAY_NONE_MIN:
        return f"{len(reason or '')} characters, under {FIX_REPLAY_NONE_MIN}"
    m = FIX_REPLAY_VAGUE.search(reason)
    return f"it says {m.group(0)!r}: name what cannot replay and why" if m else None


def run_fix_replays(repo, base, head, commits):
    """AGENTCTL'S REPLAY (above): each bare `Fix-Replay: <command>` of a Fixes-Red push, run at base and at head, each in
    a throwaway detached worktree of repo: [{cmd, base, head, base_sha, head_sha}], a side PASS (exit 0), FAIL or
    TIMEOUT. Nothing to run (no Fixes-Red:, no bare command): []."""
    named, _claimed, bare, _nones = fix_replay_trailers(repo, commits)
    if not named or not bare:
        return []
    out = []
    for cmd in bare:
        rec = {"cmd": cmd, "base_sha": base, "head_sha": head}
        for side, sha in (("base", base), ("head", head)):
            rec[side] = fix_replay_once(repo, sha, cmd, side)
        err(f"fix replay: `{cmd}` base={rec['base']} head={rec['head']}")
        out.append(rec)
    return out


def fix_replay_once(repo, sha, cmd, side):
    """`cmd` run (bash) in a throwaway detached worktree of repo at sha: PASS, FAIL or TIMEOUT; its log kept under the
    state root (fix-replay/<sha>-<side>.log)."""
    import shutil, tempfile
    logs = state_root("fix-replay")
    os.makedirs(logs, exist_ok=True)
    wt = tempfile.mkdtemp(prefix=f"replay-{side}-", dir=logs)
    log = os.path.join(logs, f"{sha[:12]}-{side}.log")
    try:
        git(repo, "worktree", "add", "-q", "--detach", wt, sha)
        err(f"fix replay: `{cmd}` at {side} {sha[:10]} (log: {log})")
        with open(log, "w") as f:
            try:
                rc = process_runner().run(["bash", "-c", cmd], cwd=wt, stdin=subprocess.DEVNULL, stdout=f,
                                    stderr=subprocess.STDOUT, env=tool_env(),
                                    timeout=env_num("AGENTCTL_FIX_REPLAY_TIMEOUT", FIX_REPLAY_TIMEOUT)).returncode
            except subprocess.TimeoutExpired:
                return "TIMEOUT"
        return "PASS" if rc == 0 else "FAIL"
    finally:
        git(repo, "worktree", "remove", "--force", wt, check=False)
        shutil.rmtree(wt, ignore_errors=True)
        git(repo, "worktree", "prune", check=False)


def gate_fix_replay_refusal(d, claimed, bare, nones, proofs):
    """Why a Fixes-Red: for gate red d is refused under policy `fix-replay` (above), or None."""
    what = red_what(d)
    if nones:
        bad = [(r, fix_replay_none_lint(r)) for r in nones]
        bad = [(r, why) for r, why in bad if why]
        if not bad:
            return None
        return (f"{what} is a gate red: `Fix-Replay: none — {bad[0][0]}` is no reason ({bad[0][1]}); name, in "
                f"{FIX_REPLAY_NONE_MIN}+ characters, why this red cannot replay (infra, a CI-only race), or give the "
                "command that fails at origin/main and passes at your fix")
    cmds = bare + [c for c, _b, _h in claimed if c not in bare]
    if not cmds:
        return (f"{what} is a gate red: its Fixes-Red needs `Fix-Replay: <command>` -- the narrowest command that "
                "fails at origin/main and passes at your fix; agentctl runs it at both before the change counts as the "
                "fix -- or `Fix-Replay: none — <why>` when it genuinely cannot replay (a specific reason, "
                f"{FIX_REPLAY_NONE_MIN}+ characters; the owner reads them weekly)")
    seen = {p.get("cmd"): p for p in proofs or [] if p.get("cmd")}
    if any((seen.get(c) or {}).get("base") == "FAIL" and (seen.get(c) or {}).get("head") == "PASS" for c in cmds):
        return None
    ran = [c for c in cmds if c in seen]
    if ran:
        p = seen[ran[0]]
        return (f"{what} is a gate red: agentctl ran `{ran[0]}`: base={p.get('base')} head={p.get('head')} -- it must "
                "fail at origin/main and pass at your fix (head=FAIL: the fix is refuted before it lands; base=PASS: "
                "the command does not reach the red)")
    return (f"{what} is a gate red: agentctl has not run `{cmds[0]}` (a bare `Fix-Replay: <command>`, no "
            "base=/head= of your own: `agentctl land` from the laptop runs it at origin/main and at your fix)")


def fix_replay_refusal(repo, commits, store=None, proofs=None):
    """(policy id, why) when the Fixes-Red: of commits is refused by FIX REPLAY (above) -- `fix-replay` for a gate red,
    enforced; `fix-replay-outside` for a red outside the gate whose workflow names a replay, warn-only -- or None.
    proofs: agentctl's own replays (run_fix_replays; the submission's `replays`). An unread store refuses nothing (the
    red's own verification still judges); a registry missing from main refuses an outside red of a repo that keeps one
    (red-repos.json `red_workflows`: Alert backtest #7's fix landed unreplayed because agentctl enforced five hours
    before the registry reached main)."""
    named, claimed, bare, nones = fix_replay_trailers(repo, commits)
    if not named:
        return None
    try:
        with no_vault():  # THE LAPTOP'S LAND NEEDS NO VAULT: never `secrets` for this read (no token: no refusal)
            items = [it for it in (store or open_store()).items("red") if it["data"].get("key")
                     and it["state"] not in RED_TERMINAL]
    except (Exception, SystemExit):
        return None
    outside = None
    for it in sorted(items, key=lambda it: not it["data"].get("gate")):  # the gate's first: enforced
        d = it["data"]
        if not named & {str(x) for x in red_names(d) if x is not None}:
            continue
        if d.get("gate"):
            why = gate_fix_replay_refusal(d, claimed, bare, nones, proofs)
            if why:
                return "fix-replay", why
            continue
        if outside:
            continue
        if not red_workflows_on_main(repo) and (red_repo_conf(red_repo_of(d)) or {}).get("red_workflows"):
            outside = (f"{RED_WORKFLOWS_FILE} is missing on {remote()}/main, though red-repos.json says "
                       f"{red_repo_of(d)} keeps one: no Fixes-Red lands for {red_what(d)} until it is back (its rows "
                       "say which reds need a `Fix-Replay:`)")
            continue
        cmd = red_replay_command(repo, d)
        if not cmd:
            continue
        stem = red_workflow_policy(repo, d.get("workflow"))["replay"].split("{run}")[0].strip()
        ok = [r for r in claimed if r[0].startswith(stem)]
        if not any(r[1:] == ("FAIL", "PASS") for r in ok):
            said = f" (this push says base={ok[0][1]} head={ok[0][2]})" if ok else ""
            outside = (f"{red_what(d)} has a replay: run `{cmd}` at origin/main and at your fix, see base=FAIL "
                       f"head=PASS, and record it as the trailer `Fix-Replay: {cmd} base=FAIL head=PASS`{said}")
    return ("fix-replay-outside", outside) if outside else None
