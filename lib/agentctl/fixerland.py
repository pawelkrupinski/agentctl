# agentctl, part `fixerland`: how a fixer lands: its land command, land as you go, real activity as progress.
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import glob
import itertools
import json
import os
import re
import subprocess
import sys
import time
from typing import Any
from contextlib import suppress
from agentctl.core import HOME, attempt, cherry_landed, env_num, err, git, on_ci_runner, reds_dir, remote, spawn_detached
from agentctl.timefmt import ts
from agentctl.statefile import read_record, write_json
from agentctl.store import StoreUnavailable
from agentctl.procs import process_start, socket_host
from agentctl.selfcode import TOOL_PATH
from agentctl.repos import red_repo_of
from agentctl.fixerrecords import fixer_keys, fixer_records
from agentctl.inboxentry import inbox_write
from agentctl.claudefiles import claude_jobs_root
from agentctl.allowedpaths import allowed_path
from agentctl.fixersession import fixer_label
from agentctl.worktrees import fresh_worktree
from agentctl.fixerenv import fixer_env
from agentctl.redrepos import red_is_other, red_owner_only, red_repo_tag
from agentctl.redfamilies import red_family
from agentctl.laptoprole import laptop_role_standby


# HOW A FIXER LANDS (2026-11-16, Main #2515: the fixer wrapped `agentctl land` in `timeout -s KILL` twice, then ran
# scripts/hooks/pre-push itself; neither verdict came within its 180-min turn).
LAND_BRIEF = (" `~/bin/agentctl land` QUEUES your fix like every change (red fixes go first, checked on the CI tier), runs "
              "the 45-s fast precheck in the foreground -- a trailer or rule it refuses is your NOT LANDED at once: fix "
              "it and land again -- then waits for the queue's verdict itself, bounded, and prints LANDED <sha> or NOT "
              "LANDED: <why>. Run it with run_in_background=true and act on its last line. Never wrap it in `timeout` "
              "(a kill leaves your change queued without you), and never run scripts/hooks/pre-push yourself: the "
              "queue's checks are the landing checks.")


# THE agentctl REPOSITORY (~/bin): a fixer never edits it (`agentctl issue`); watchdog WB checks its checkout stays clean.
# $AGENTCTL_BIN_REPO ('-': none, the tests').
def bin_repo() -> str | None:
    r = os.environ.get("AGENTCTL_BIN_REPO") or os.path.dirname(TOOL_PATH)
    return None if r == "-" or not os.path.isdir(os.path.join(r, ".git")) else r


def red_dispatch(o_r, repo, u, why="by hand", resume=None, effort=None, lease=None, took=None, extra=(), store=None):
    """Start a headless fixer for red u under its taken lease: a fresh worktree <clone>-wt-red-<run> off origin/main
    (resume: the same worktree and session), then a detached `agentctl red fixer` that runs `claude -p` there, renews
    the lease by its progress, and releases it with its verdict. (ok, message)."""
    standby = None if on_ci_runner() else laptop_role_standby(store)
    if standby:  # ONE MACHINE HOLDS THE LAPTOP ROLE (server.py): a standby machine starts no fixer
        return False, f"NOT DISPATCHED: {standby}"
    num = u["run"].get("number")
    o_r = u.get("repo") or o_r
    d = reds_dir(red_repo_tag(o_r, num))
    os.makedirs(d, exist_ok=True)
    k = 1 + len(glob.glob(os.path.join(d, "fixer*.json")))
    rec_path = os.path.join(d, "fixer.json" if k == 1 else f"fixer-{k}.json")
    cont = None if resume else previous_fixer_tree(u)
    if resume and resume.get("worktree") and os.path.isdir(resume["worktree"]):
        wt = resume["worktree"]
    elif cont:  # FIXER CONTINUITY: the previous fixer's worktree and its unlanded commits, never a fresh start
        wt = cont["worktree"]
        resume = {"note": cont["note"]}
    else:
        wt, why_not = fresh_worktree(repo, "red", num)
        if wt is None:
            return False, dispatch_log(u, f"NOT DISPATCHED: {why_not}")
    rec = {"key": u.get("key"), "lease": lease, "epoch": (took or {}).get("epoch"),
           "jobs": u.get("jobs") or [u.get("job")],
           "run": num, "run_id": u["run"].get("databaseId"), "workflow": u["workflow"], "job": u.get("job"),
           "kind": u.get("kind"), "line": u.get("line"), "flake": bool(u.get("flake")), "worktree": wt, "started": ts(),
           "why": why, "log": os.path.join(d, f"claude-{k}.log"), "session": None,
           # A RESUME IS A NEW SESSION (issues 20261119T025305-21375d, 20261103T232907-ca63b2): `claude --bg --resume
           # <old>` reports its own id, so the record names the session it resumes apart; `session` is the new one's,
           # written by the wrapper's first poll or bound by the fixer's own first `red claim --fixer`
           "resumed_from": resume.get("session") if resume else None,
           "resume": bool(resume and resume.get("session")), "resume_note": resume.get("note") if resume else None,
           "continued_from": (cont or {}).get("worktree"), "diagnosis": u.get("diagnosis"),
           "effort": effort, "refuted": u.get("refuted") or [], "predicts": u.get("predicts"), "orphan": u.get("orphan"),
           "extra_leases": [{"name": x["name"], "epoch": x["epoch"]} for x in extra], "prior_work": u.get("prior_work"),
           "blame_note": u.get("blame_note"), "lander": u.get("lander"), "decision": u.get("decision"),
           "launch_cwd": (resume or {}).get("launch_cwd")}
    if red_is_other(o_r):
        rec["repo"] = o_r
    env = fixer_env(o_r, num)
    if took:
        env["AGENTCTL_LEASE_SECRET"] = took["secret"]
    if extra:
        env["AGENTCTL_LEASE_SECRETS"] = json.dumps({x["name"]: x["secret"] for x in extra})
    p = spawn_fixer(repo, d, rec_path, rec, wt, env)
    if store is not None and lease and took and "epoch" in took:
        # its wrapper, named in the lease from the start: one that dies before its first word is judged at once
        # (red_reap_orphans), its stderr (fixer.log) the why -- never a silent 15-min lapse
        # (the wrapper sends its own meta at its first poll)
        attempt(lambda: store.set_meta(lease, took["epoch"], took["secret"],
                                       {"pid": p.pid, "host": socket_host(), "worktree": wt,
                                        "wrapper_log": os.path.join(d, "fixer.log")}),
                None, catch=(Exception,), what="red dispatch: the lease's meta was not written")
    line = dispatch_log(u, f"DISPATCHED{' (resumed)' if rec['resume'] else ' (continuing)' if cont else ''} pid {p.pid} "
                           f"in {wt} ({why}); log {rec['log']}")
    inbox_write("red", f"RED DISPATCHED: {u['workflow']} #{num} [{u.get('job')}] -> fixer session "
                       f"'{fixer_label({**rec, 'run': num})}' (`claude agents`) in {wt.replace(HOME, '~')} ({why})", [], [], 0, cwd=wt)
    return True, line


def spawn_fixer(repo: str, d: str, rec_path: str, rec: dict[str, Any], wt: str, env: dict[str, str]) -> Any:
    """A background `red fixer` in wt for the record at rec_path (written before and, with its pid, after), its
    stderr and stdout appended to d/fixer.log. The process."""
    write_json(rec_path, rec)
    with open(os.path.join(d, "fixer.log"), "a") as logf:
        p = spawn_detached([sys.executable, TOOL_PATH, "red", "fixer", "--repo", repo, "--record", rec_path],
                           stdin=subprocess.DEVNULL, stdout=logf, stderr=logf, start_new_session=True, cwd=wt, env=env)
    rec.update(pid=p.pid, process_start=process_start(p.pid))
    write_json(rec_path, rec)
    return p


def unlanded_commits(wt):
    """[(sha, author time)] of the worktree's commits not on origin/main (cherry_landed: a rebased copy on main counts as
    landed), oldest first; [] when it is not a worktree."""
    if not wt or not os.path.isdir(wt):
        return []
    shas = cherry_landed(wt, f"refs/remotes/{remote()}/main", "HEAD")[1] or []
    out = []
    for sha in shas:
        r = git(wt, "log", "-1", "--format=%at", sha, check=False)
        out.append((sha, int(r.stdout.strip() or 0) if r.returncode == 0 and r.stdout.strip().isdigit() else 0))
    return out


def fixers_of(key, run, fam=None):
    """The fixer records of a red: by its key (any run of it), by its run, or by its family (red-families.json)."""
    return [r for r in fixer_records() if key in fixer_keys(r) or (r.get("workflow"), str(r.get("run"))) == run
            or (fam and red_family(r) == fam)]


def previous_fixer_tree(u):
    """FIXER CONTINUITY (the owner, 2026-10-29: fixer -8 of #2473's red started fresh in movies-wt-red-2517 while -7's
    five commits sat unlanded in movies-wt-red-2473-5): a new fixer of the same red -- its key, any run of it -- goes on
    in the previous fixer's worktree when that holds commits not on origin/main (the most recently committed one),
    and is told to cherry-pick any other's. {worktree, note} or None. Read on the laptop at dispatch: the worktrees are
    here, never on ci-runner."""
    run = (u.get("workflow"), str((u.get("run") or {}).get("number")))
    trees = [r.get("worktree") for r in sorted(fixers_of(u.get("key"), run, red_family(u)),
                                                key=lambda r: str(r.get("started") or ""), reverse=True)]
    trees += [h.get("worktree") for h in u.get("history") or []] + [(u.get("prior_work") or {}).get("worktree")]
    found = {}
    for wt in trees:
        if wt and wt not in found and os.path.isdir(wt) and allowed_path(wt) and not os.path.exists(os.path.join(wt, "RED-STOP.md")):
            left = unlanded_commits(wt)
            if left:
                found[wt] = left
    if not found:
        return None
    wt = max(found, key=lambda w: max(at for _s, at in found[w]))
    others = {w: c for w, c in found.items() if w != wt}
    num = (u.get("run") or {}).get("number")
    note = (f"CONTINUE, NEVER START OVER: {wt} is the previous fixer's worktree of this red, with {len(found[wt])} "
            f"commit(s) not on {remote()}/main ({', '.join(s[:10] for s, _ in found[wt][:10])}). Read them first (`git log -p "
            f"{remote()}/main..HEAD`), `git fetch {remote()} && git rebase {remote()}/main` (never squash), carry the fix on from "
            f"there (add `Fixes-Red: {num}` to what you commit) and land each verified step with `~/bin/agentctl land`.")
    if others:
        note += " Other earlier fixers of it left unlanded commits too: " + "; ".join(
            f"{w} ({', '.join(s[:10] for s, _ in c[:10])})" for w, c in others.items()) + \
            " -- cherry-pick what belongs to the fix (never squash), say why you leave the rest."
    return {"worktree": wt, "note": note}


# LAND AS YOU GO (the owner, 2026-10-29: fixer -7 of #2473 sat on five unlanded commits for hours, and the import that
# took its lease away left them stranded). The laptop watches every working fixer's worktree: commits not on
# origin/main for FIXER_UNLANDED_NUDGE (by author time: a rebase keeps it) get a RED-NUDGE.md there, which its prompt
# tells it to act on; still unlanded at FIXER_UNLANDED_ESCALATE, one RED ESCALATE inbox entry. Both clear themselves
# once the commits are on main (RECOVERED after an escalation).
FIXER_UNLANDED_NUDGE = 45 * 60
FIXER_UNLANDED_ESCALATE = 90 * 60
FIXER_LAND_WATCH_EVERY = 60
_last_land_watch = [0.0]


def fixers_land_watch(now_=None, force=False):
    t = now_ if now_ is not None else time.time()
    if not force and t - _last_land_watch[0] < env_num("AGENTCTL_FIXER_LAND_WATCH", FIXER_LAND_WATCH_EVERY):
        return []
    _last_land_watch[0] = t
    out = []
    for r in fixer_records():
        wt = r.get("worktree")
        if not r.get("alive") or not wt or not os.path.isdir(wt):
            continue
        if red_owner_only(red_repo_of(r)):  # its commits are never landed by it: filed with `red owner-push`
            continue
        sp, nudge = r["path"] + ".land.json", os.path.join(wt, "RED-NUDGE.md")
        st = read_record(sp) or {}
        left = unlanded_commits(wt)
        label = fixer_label(r)
        if not left:
            if st:
                for x in (sp, nudge):
                    with suppress(OSError):
                        os.unlink(x)
                if st.get("escalated_at"):
                    inbox_write("red fixer", f"RECOVERED: {label}: its commits are on main ({wt.replace(HOME, '~')})",
                                [], [], 0, cwd=wt)
                out.append(f"{label}: everything landed")
            continue
        oldest = min((at for _s, at in left if at), default=t)
        mins = int((t - oldest) // 60)
        shas = ", ".join(s_[:10] for s_, _ in left[:8])
        if t - oldest >= env_num("AGENTCTL_FIXER_UNLANDED_NUDGE", FIXER_UNLANDED_NUDGE) and not st.get("nudged_at"):
            with open(nudge, "w") as f:
                f.write(f"LAND NOW (agentctl, {ts()}): {len(left)} commit(s) here are not on {remote()}/main, the oldest "
                        f"{mins} min old ({shas}). Land each verified step with `~/bin/agentctl land` (Fixes-Red lands on "
                        f"red); if one is not ready, say why in `agentctl red claim {r.get('run')} --note`. Delete this "
                        "file once they are queued.\n")
            st["nudged_at"] = t
            out.append(f"{label}: nudged ({len(left)} commit(s) unlanded for {mins} min)")
        elif st.get("nudged_at") and not st.get("escalated_at") and \
                t - oldest >= env_num("AGENTCTL_FIXER_UNLANDED_ESCALATE", FIXER_UNLANDED_ESCALATE):
            line = (f"RED ESCALATE: {label}: {len(left)} commit(s) unlanded for {mins} min in {wt.replace(HOME, '~')} "
                    f"({shas}), nudged at {time.strftime('%H:%M', time.localtime(st['nudged_at']))}: land them or stop it")
            inbox_write("red fixer", line, [line], [], 1, cwd=wt)
            st["escalated_at"] = t
            out.append(line)
        write_json(sp, st)
    return out


def dispatch_log(u, what):
    """One line to reds/dispatch.log; returns it."""
    line = f"{ts()} {u['workflow']} #{u['run'].get('number')} [{u.get('job')}] red {u.get('key')}: {what}"
    os.makedirs(reds_dir(), exist_ok=True)
    with open(reds_dir("dispatch.log"), "a") as f:
        f.write(line + "\n")
    return line


def fixer_mark(rec, wt):
    """A dispatched fixer's (or an owner's) progress mark: its transcript's bytes, its worktree's HEAD and `git status`,
    and the session's other real activity (session_activity: its sub-agents' transcripts, its commands' output, its job
    directory's files)."""
    import hashlib
    size = os.path.getsize(rec["log"]) if os.path.exists(rec["log"]) else 0
    head = git(wt, "rev-parse", "HEAD", check=False).stdout.strip()[:12] if os.path.isdir(wt) else ""
    st = git(wt, "status", "--porcelain", check=False).stdout if os.path.isdir(wt) else ""
    return f"{size}:{head}:{hashlib.sha1(st.encode()).hexdigest()[:10]}" + session_activity(rec["log"], rec.get("bg_id"))


# REAL ACTIVITY IS PROGRESS (2026-11-03, convergence red 8fbdbe0d5a86: ~8 h through several owners, "lease expired" every
# 15-30 min and "STALLED: no progress (transcript, worktree) for 30 min"). The mark read the session's own transcript
# and the worktree only, and a session at work is often silent in both: a sub-agent it delegated to writes ITS
# transcript (<session>/subagents/*.jsonl), a long command writes its output (claude's tmp <project>/<session>/tasks/),
# a bisect or a measure collector writes under $CLAUDE_JOB_DIR (~/.claude/jobs/<id>/tmp) -- 09:20 to 09:51 that day
# the owner was waiting on its own CI measure, 22:28 on, a local bisect wrote bisect.log every few seconds while the
# owner's red lease counted down to "lapses without progress". Each of them now moves the mark.
SESSION_ACTIVITY_FILES = 2000  # files looked at per session at most (a job dir full of logs stays cheap)


def claude_tmp_root():
    """claude's per-user tmp root (<root>/<project dir>/<session>/tasks/<task>.output), $AGENTCTL_CLAUDE_TMP."""
    env = os.environ.get("AGENTCTL_CLAUDE_TMP")
    if env:
        return env
    for d in (f"/private/tmp/claude-{os.getuid()}", f"/tmp/claude-{os.getuid()}"):
        if os.path.isdir(d):
            return d
    return f"/tmp/claude-{os.getuid()}"


def session_activity_dirs(transcript, bid=None):
    """The directories a session writes to besides its transcript (see REAL ACTIVITY IS PROGRESS)."""
    if not transcript or not str(transcript).endswith(".jsonl"):
        return []
    base = str(transcript)[:-len(".jsonl")]
    session, project = os.path.basename(base), os.path.basename(os.path.dirname(base))
    out = [os.path.join(base, "subagents"), os.path.join(claude_tmp_root(), project, session, "tasks")]
    jid = str(bid or session[:8])
    if re.fullmatch(r"[0-9a-f]{6,}", jid):
        out.append(os.path.join(claude_jobs_root(), jid, "tmp"))
    return out


def _activity_files(transcript: Any, bid: Any) -> Any:
    for d in session_activity_dirs(transcript, bid):
        for root, _dirs, files in os.walk(d):
            for fn in files:
                yield os.path.join(root, fn)


def _activity_stats(transcript: Any, bid: Any) -> Any:
    """os.stat of the first SESSION_ACTIVITY_FILES files under session_activity_dirs that can still be read."""
    stats = []
    for path in itertools.islice(_activity_files(transcript, bid), SESSION_ACTIVITY_FILES):
        with suppress(OSError):
            stats.append(os.stat(path))
    return stats


def session_activity(transcript, bid=None):
    """':act <bytes>@<newest mtime>' over session_activity_dirs, '' when there is none: a mark part that moves while
    the session's sub-agents, commands or job files grow."""
    stats = _activity_stats(transcript, bid)
    newest = max((s.st_mtime for s in stats), default=0.0)
    return f":act {sum(s.st_size for s in stats)}@{int(newest)}" if newest else ""


def fixer_meta_sent(st, leases, meta):
    """Merge a fixer's meta (worktree, session, pid, log: a resume finds its session by it) into its leases, the store
    keeping only its holder's: True, or False when the store did not answer and the wrapper must send it again."""
    try:
        for n_, e_, s_ in leases:
            st.set_meta(n_, e_, s_, meta)
        return True
    except StoreUnavailable as e:
        print(f"{ts()} the store did not answer ({e}): the leases' meta is sent again", flush=True)
        return False
