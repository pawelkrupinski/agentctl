# agentctl, part `fixerland`: how a fixer lands: its land command, land as you go, real activity as progress.
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from agentctl.clock import wall_epoch
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
from agentctl.onmain import landed_state
from agentctl.core import attempt, git, on_ci_runner, reds_dir, remote, spawn_detached, tilde
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
from agentctl.redrepos import red_family, red_is_other, red_owner_only, red_repo_tag
from agentctl.laptoprole import laptop_role_standby
from agentctl.queueknobs import knob_fixer_land_watch
from agentctl.fixerknobs import knob_claude_tmp, knob_fixer_unlanded_escalate, knob_fixer_unlanded_nudge


# HOW A FIXER LANDS (2026-11-16, Main #2515: the fixer wrapped `agentctl land` in `timeout -s KILL` twice, then ran
# scripts/hooks/pre-push itself; neither verdict came within its 180-min turn).
LAND_BRIEF = (" `~/bin/agentctl land` QUEUES your fix like every change (red fixes go first, checked on the CI tier), runs "
              "the 45-s fast precheck in the foreground -- a trailer or rule it refuses is your NOT LANDED at once: fix "
              "it and land again -- then waits for the queue's verdict itself, bounded, and prints LANDED <sha> or NOT "
              "LANDED: <why>. Run it with run_in_background=true and act on its last line. Never wrap it in `timeout` "
              "(a kill leaves your change queued without you), and never run scripts/hooks/pre-push yourself: the "
              "queue's checks are the landing checks.")


def red_dispatch(o_r: Any, repo: Any, u: Any, why: Any = "by hand", resume: Any = None, effort: Any = None, lease: Any = None,
                 took: Any = None, extra: Any = (), store: Any = None) -> Any:
    """Start a headless fixer for red u under its taken lease: a fresh worktree <clone>-wt-red-<run> off origin/main
    (resume: the same worktree and session), then a detached `agentctl red fixer` that runs `claude -p` there, renews
    the lease by its progress, and releases it with its verdict. (ok, message)."""
    standby = None if on_ci_runner() else laptop_role_standby(store)
    if standby:  # ONE MACHINE HOLDS THE LAPTOP ROLE (storeapi.py): a standby machine starts no fixer
        return False, f"NOT DISPATCHED: {standby}"
    return RedDispatch(o_r, repo, u, why, resume, effort, lease, took, extra, store).run()


class RedDispatch:
    """One dispatch of a fixer for a red (red_dispatch): its directory and record, the worktree it works in (the
    resumed one, the previous fixer's, or a fresh one), the lease it carries and the wrapper that is told of it."""

    def __init__(self, o_r: Any, repo: Any, u: Any, why: Any, resume: Any, effort: Any, lease: Any, took: Any, extra: Any,
                 store: Any) -> None:
        self.o_r, self.repo, self.u, self.why, self.resume = u.get("repo") or o_r, repo, u, why, resume
        self.effort, self.lease, self.took, self.extra, self.store = effort, lease, took or {}, extra, store
        self.num, self.cont, self.refusal = u["run"].get("number"), None, ""

    def run(self) -> Any:
        d = reds_dir(red_repo_tag(self.o_r, self.num))
        os.makedirs(d, exist_ok=True)
        k = 1 + len(glob.glob(os.path.join(d, "fixer*.json")))
        rec_path = os.path.join(d, "fixer.json" if k == 1 else f"fixer-{k}.json")
        wt = self._worktree()
        if wt is None:
            return False, self.refusal
        rec = self._record(d, k, wt)
        if red_is_other(self.o_r):
            rec["repo"] = self.o_r
        p = spawn_fixer(self.repo, d, rec_path, rec, wt, self._env())
        self._name_wrapper(p, d, wt)
        return True, self._announce(rec, p, wt)

    def _worktree(self) -> Any:
        """The worktree the fixer works in, or None (self.refusal says why): a resumed one, else the previous fixer's
        with its unlanded commits (FIXER CONTINUITY, never a fresh start), else a fresh one."""
        resume = self.resume
        self.cont = None if resume else previous_fixer_tree(self.u)
        if resume and resume.get("worktree") and os.path.isdir(resume["worktree"]):
            return resume["worktree"]
        if self.cont:
            self.resume = {"note": self.cont["note"]}
            return self.cont["worktree"]
        wt, why_not = fresh_worktree(self.repo, "red", self.num)
        if wt is None:
            self.refusal = dispatch_log(self.u, f"NOT DISPATCHED: {why_not}")
        return wt

    def _record(self, d: Any, k: Any, wt: Any) -> Any:
        u, resume, took = self.u, self.resume or {}, self.took
        return {"key": u.get("key"), "lease": self.lease, "epoch": took.get("epoch"),
                "jobs": u.get("jobs") or [u.get("job")],
                "run": self.num, "run_id": u["run"].get("databaseId"), "workflow": u["workflow"], "job": u.get("job"),
                "kind": u.get("kind"), "line": u.get("line"), "flake": bool(u.get("flake")), "worktree": wt, "started": ts(),
                "why": self.why, "log": os.path.join(d, f"claude-{k}.log"), "session": None,
                # A RESUME IS A NEW SESSION (issues 20261119T025305-21375d, 20261103T232907-ca63b2): `claude --bg --resume
                # <old>` reports its own id, so the record names the session it resumes apart; `session` is the new one's,
                # written by the wrapper's first poll or bound by the fixer's own first `red claim --fixer`
                "resumed_from": resume.get("session"), "resume": bool(resume.get("session")), "resume_note": resume.get("note"),
                "continued_from": (self.cont or {}).get("worktree"), "diagnosis": u.get("diagnosis"),
                "effort": self.effort, "refuted": u.get("refuted") or [], "predicts": u.get("predicts"), "orphan": u.get("orphan"),
                "extra_leases": [{"name": x["name"], "epoch": x["epoch"]} for x in self.extra], "prior_work": u.get("prior_work"),
                "blame_note": u.get("blame_note"), "lander": u.get("lander"), "decision": u.get("decision"),
                "launch_cwd": resume.get("launch_cwd")}

    def _env(self) -> Any:
        env = fixer_env(self.o_r, self.num)
        if self.took:
            env["AGENTCTL_LEASE_SECRET"] = self.took["secret"]
        if self.extra:
            env["AGENTCTL_LEASE_SECRETS"] = json.dumps({x["name"]: x["secret"] for x in self.extra})
        return env

    def _name_wrapper(self, p: Any, d: Any, wt: Any) -> Any:
        """Its wrapper, named in the lease from the start: one that dies before its first word is judged at once
        (red_reap_orphans), its stderr (fixer.log) the why -- never a silent 15-min lapse (the wrapper sends its own
        meta at its first poll)."""
        took = self.took
        if self.store is not None and self.lease and took and "epoch" in took:
            attempt(lambda: self.store.set_meta(self.lease, took["epoch"], took["secret"],
                                                {"pid": p.pid, "host": socket_host(), "worktree": wt,
                                                 "wrapper_log": os.path.join(d, "fixer.log")}),
                    None, catch=(Exception,), what="red dispatch: the lease's meta was not written")

    def _announce(self, rec: Any, p: Any, wt: Any) -> Any:
        u, num = self.u, self.num
        how = " (resumed)" if rec["resume"] else " (continuing)" if self.cont else ""
        line = dispatch_log(u, f"DISPATCHED{how} pid {p.pid} in {wt} ({self.why}); log {rec['log']}")
        inbox_write("red", f"RED DISPATCHED: {u['workflow']} #{num} [{u.get('job')}] -> fixer session "
                           f"'{fixer_label({**rec, 'run': num})}' (`claude agents`) in {tilde(wt)} ({self.why})",
                    [], [], 0, cwd=wt)
        return line


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


def unlanded_commits(wt: Any) -> Any:
    """[(sha, author time)] of the worktree's commits not on origin/main (landed_state: a rebased copy on main counts as
    landed), oldest first; [] when it is not a worktree or nothing is left; None when git cannot compare (not "all
    landed": a caller leaves what it knows as it is)."""
    if not wt or not os.path.isdir(wt):
        return []
    state = landed_state(wt, f"refs/remotes/{remote()}/main", "HEAD")
    if state.unknown:
        return None
    out = []
    for sha in state.left:
        r = git(wt, "log", "-1", "--format=%at", sha, check=False)
        out.append((sha, int(r.stdout.strip() or 0) if r.returncode == 0 and r.stdout.strip().isdigit() else 0))
    return out


def fixers_of(key, run, fam=None):
    """The fixer records of a red: by its key (any run of it), by its run, or by its family (red-families.json)."""
    return [r for r in fixer_records() if key in fixer_keys(r) or (r.get("workflow"), str(r.get("run"))) == run
            or (fam and red_family(r) == fam)]


def _fixer_trees(u: Any) -> Any:
    """The worktrees a new fixer of red u might go on in, best first: the previous fixers' (newest started first), then
    the red's history and its prior work."""
    run = (u.get("workflow"), str((u.get("run") or {}).get("number")))
    trees = [r.get("worktree") for r in sorted(fixers_of(u.get("key"), run, red_family(u)),
                                                key=lambda r: str(r.get("started") or ""), reverse=True)]
    return trees + [h.get("worktree") for h in u.get("history") or []] + [(u.get("prior_work") or {}).get("worktree")]


def _continuable(wt: Any, found: Any) -> Any:
    """A worktree not yet found that is there, allowed and not stopped (RED-STOP.md)."""
    return bool(wt) and wt not in found and os.path.isdir(wt) and allowed_path(wt) and not os.path.exists(os.path.join(wt, "RED-STOP.md"))


def _continue_note(num: Any, wt: Any, found: Any) -> Any:
    """What a fixer going on in wt is told: read the unlanded commits, rebase, carry on, cherry-pick the others'."""
    note = (f"CONTINUE, NEVER START OVER: {wt} is the previous fixer's worktree of this red, with {len(found[wt])} "
            f"commit(s) not on {remote()}/main ({', '.join(s[:10] for s, _ in found[wt][:10])}). Read them first (`git log -p "
            f"{remote()}/main..HEAD`), `git fetch {remote()} && git rebase {remote()}/main` (never squash), carry the fix on from "
            f"there (add `Fixes-Red: {num}` to what you commit) and land each verified step with `~/bin/agentctl land`.")
    others = {w: c for w, c in found.items() if w != wt}
    if others:
        note += " Other earlier fixers of it left unlanded commits too: " + "; ".join(
            f"{w} ({', '.join(s[:10] for s, _ in c[:10])})" for w, c in others.items()) + \
            " -- cherry-pick what belongs to the fix (never squash), say why you leave the rest."
    return note


def previous_fixer_tree(u: Any) -> Any:
    """FIXER CONTINUITY (the owner, 2026-10-29: fixer -8 of #2473's red started fresh in movies-wt-red-2517 while -7's
    five commits sat unlanded in movies-wt-red-2473-5): a new fixer of the same red -- its key, any run of it -- goes on
    in the previous fixer's worktree when that holds commits not on origin/main (the most recently committed one),
    and is told to cherry-pick any other's. {worktree, note} or None. Read on the laptop at dispatch: the worktrees are
    here, never on ci-runner."""
    found = {}
    for wt in _fixer_trees(u):
        if _continuable(wt, found):
            left = unlanded_commits(wt)
            if left:  # None (git cannot compare): the tree is not offered as the one to continue in
                found[wt] = left
    if not found:
        return None
    wt = max(found, key=lambda w: max(at for _s, at in found[w]))
    return {"worktree": wt, "note": _continue_note((u.get("run") or {}).get("number"), wt, found)}


class LandWatchPace:
    """When the fixers' landing look last ran: one per sync loop, made where the loop's steps are wired (syncsteps.py),
    so no module holds it between loops."""

    def __init__(self, last: float = 0.0) -> None:
        self.last = last

    def __repr__(self) -> str:
        return f"LandWatchPace(last={self.last})"

    def due(self, t: float, interval: float) -> bool:
        """True, and the look is stamped, when `interval` seconds have passed since the last one."""
        if t - self.last < interval:
            return False
        self.last = t
        return True


def fixers_land_watch(now_: Any = None, force: Any = False, *, pace: LandWatchPace) -> Any:
    t = now_ if now_ is not None else wall_epoch()  # against git author times
    if not force and not pace.due(t, knob_fixer_land_watch()):
        return []
    pace.last = t
    out = []
    for r in fixer_records():
        out += FixerLandWatch(r, t).step()
    return out


class FixerLandWatch:
    """One fixer's worktree under LAND AS YOU GO: its unlanded commits, the RED-NUDGE.md at FIXER_UNLANDED_NUDGE, the
    escalation at FIXER_UNLANDED_ESCALATE, the state (`<record>.land.json`) both clear once the commits are on main."""

    def __init__(self, rec: Any, t: Any) -> None:
        self.rec, self.t, self.wt = rec, t, rec.get("worktree")
        self.state_path, self.state, self.label = rec["path"] + ".land.json", {}, ""

    def step(self) -> Any:
        """What this look at the fixer did, as report lines."""
        if not self._watched():
            return []
        self.state = read_record(self.state_path) or {}
        left = unlanded_commits(self.wt)
        self.label = fixer_label(self.rec)
        if left is None:  # git cannot compare: nothing is proved landed, the nudge state and an escalation stand
            return []
        if not left:
            return self._landed()
        oldest = min((at for _s, at in left if at), default=self.t)
        out = self._due(len(left), int((self.t - oldest) // 60), ", ".join(s_[:10] for s_, _ in left[:8]), self.t - oldest)
        write_json(self.state_path, self.state)
        return out

    def _watched(self) -> Any:
        if not self.rec.get("alive") or not self.wt or not os.path.isdir(self.wt):
            return False
        return not red_owner_only(red_repo_of(self.rec))  # its commits are never landed by it: filed with `red owner-push`

    def _landed(self) -> Any:
        """Everything is on main: the state and the nudge go; an escalated fixer is told it recovered."""
        if not self.state:
            return []
        for x in (self.state_path, os.path.join(self.wt, "RED-NUDGE.md")):
            with suppress(OSError):
                os.unlink(x)
        if self.state.get("escalated_at"):
            inbox_write("red fixer", f"RECOVERED: {self.label}: its commits are on main ({tilde(self.wt)})",
                        [], [], 0, cwd=self.wt)
        return [f"{self.label}: everything landed"]

    def _due(self, count: Any, mins: Any, shas: Any, age: Any) -> Any:
        """The nudge once the oldest is FIXER_UNLANDED_NUDGE old, else the escalation after a nudge."""
        st = self.state
        if age >= knob_fixer_unlanded_nudge() and not st.get("nudged_at"):
            return [self._nudge(count, mins, shas)]
        if st.get("nudged_at") and not st.get("escalated_at") and \
                age >= knob_fixer_unlanded_escalate():
            return [self._escalate(count, mins, shas)]
        return []

    def _nudge(self, count: Any, mins: Any, shas: Any) -> Any:
        with open(os.path.join(self.wt, "RED-NUDGE.md"), "w") as f:
            f.write(f"LAND NOW (agentctl, {ts()}): {count} commit(s) here are not on {remote()}/main, the oldest "
                    f"{mins} min old ({shas}). Land each verified step with `~/bin/agentctl land` (Fixes-Red lands on "
                    f"red); if one is not ready, say why in `agentctl red claim {self.rec.get('run')} --note`. Delete this "
                    "file once they are queued.\n")
        self.state["nudged_at"] = self.t
        return f"{self.label}: nudged ({count} commit(s) unlanded for {mins} min)"

    def _escalate(self, count: Any, mins: Any, shas: Any) -> Any:
        line = (f"RED ESCALATE: {self.label}: {count} commit(s) unlanded for {mins} min in {tilde(self.wt)} "
                f"({shas}), nudged at {time.strftime('%H:%M', time.localtime(self.state['nudged_at']))}: land them or stop it")
        inbox_write("red fixer", line, [line], [], 1, cwd=self.wt)
        self.state["escalated_at"] = self.t
        return line


def dispatch_log(u, what):
    """One line to reds/dispatch.log; returns it."""
    line = f"{ts()} {u['workflow']} #{u['run'].get('number')} [{u.get('job')}] red {u.get('key')}: {what}"
    os.makedirs(reds_dir(), exist_ok=True)
    with open(reds_dir("dispatch.log"), "a") as f:
        f.write(line + "\n")
    return line


def red_dispatch_log(d: dict[str, Any], what: str) -> str:
    """One line to reds/dispatch.log for the red whose item data is d (its workflow, last run and job); returns it."""
    return dispatch_log({"workflow": d.get("workflow"), "run": d.get("last_run") or {}, "job": d.get("job")}, what)


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
    env = knob_claude_tmp()
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
