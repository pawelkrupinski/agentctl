# agentctl, part `dispatches`: every hand-dispatched CI run's owner record, who owns a branch, and the orphan branches whose owner is gone
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import glob
import os
import re
import time
from typing import Any
from agentctl.core import HOME, PROTECTED_BRANCHES, dispatches_dir, git, git_ok, remote
from agentctl.timefmt import ts
from agentctl.statefile import read_record, write_json
from agentctl.forge import RUN_FIELDS, gh_json
from agentctl.procs import proc_alive, process_start, safe_cwd
from agentctl.waitingdir import waiting_dir
from agentctl.fixerrecords import agent_name
from agentctl.worktrees import CACHE_TREES, internal_trees_dir, merged_reason, worktree_of, worktrees
from agentctl.deletions import SWEEP_KEEP_REMOTE, delete_remote_branch, log_deletion


# ---------------------------------------------------------------- branch runs: every dispatch has an owner, every branch red a route
# The owner (2026-10-27): "red not captured, make a structural fix". Identity model convergence #2116 (run 3264) on
# measure/pawel/982f7681f-0b04a4 failed and nobody was told: the snapshot, the status line and the reds covered main only,
# and a branch run (measure/*, check/*, a red fixer's) had no owner and no route.
#
# OWNER RECORDS, ~/.cache/agentctl/dispatches/<key>.json ($AGENTCTL_DISPATCH_DIR): every CI run agentctl or an agent
# dispatches -- the branch and/or run id, the worktree, the session or agent name, the pid, the purpose, the inputs, the
# time. `agentctl dispatch run` (a `gh workflow run` wrapper) and `agentctl dispatch record` write one; so do
# agentctl's own dispatches (the queue's remote checks, the Fixes-Red verification). A branch run with none (the movies
# scripts call gh themselves) is matched after the fact: measure/<user>/<sha>-<x> (else the run's head commit) to
# the worktree whose HEAD is that commit or whose HEAD reflog holds it; check/<entry> to that queue entry's worktree. The
# match is recorded, so it is made once.
#
# THE SNAPSHOT also records the newest completed run of each tracked branch and workflow (a branch with an owner record,
# or measure/*, check/*, red-*). A failed one is ONE inbox entry, `BRANCH RED: <workflow> #N on <branch> → owner <...>`
# with the first error lines (a wake-up kind for `inbox wait`). It stays a branch red until its branch is deleted or a
# newer run of that workflow on the branch is green.
#
# PREDICTS MAIN RED: the failing run is built on commits all on main now (ancestor or patch-equivalent), AND it ran with
# the inputs main's run of that workflow uses (the dispatch inputs, minus INPUTS_NOT_PREDICTIVE). Main will hit it next,
# so it is claimed and dispatched like a main red, before main fails. Inputs that differ make it a plain BRANCH RED, the
# line naming them ("recording 3166 ≠ main's 573": run 3264 replayed the fresh 10-27 recording, main replays the pinned
# one with its fixture fills). A measure on a NEWER recording that fails hermetically (a test failure, not infra) also
# says "blocks promoting recording N": that recording lacks something a promotion of it will need.
DISPATCH_PATTERNS = ("measure/", "check/", "red-")


MEASURE_BRANCH = re.compile(r"^measure/[^/]+/([0-9a-f]{7,40})-[0-9A-Za-z]+$")


OWNER_REMATCH = 3600  # an unmatched branch is matched again after this long


def dispatch_key(branch=None, run_id=None):
    return f"branch-{re.sub(r'[^A-Za-z0-9._-]', '_', branch)}" if branch else f"run-{run_id}"


def save_dispatch(rec, key=None):
    """Write an owner record (merged over an existing one: a later field wins, a None never erases). Returns it."""
    p = dispatches_dir(f"{key or dispatch_key(rec.get('branch'), rec.get('run_id'))}.json")
    old = read_record(p) or {}
    rec = {**old, **{k: v for k, v in rec.items() if v is not None}}
    write_json(p, rec)
    return rec


def _claude_pid() -> Any:
    """The Claude process that made the dispatch ($CLAUDE_PID), else this process's parent."""
    cp = os.environ.get("CLAUDE_PID") or ""
    return int(cp) if cp.isdigit() else os.getppid()


def record_dispatch(branch=None, run_id=None, workflow=None, purpose="", inputs=None, worktree=None, sha=None,
                    agent=None, pid=None, key=None, source="registered"):
    """The owner record of a dispatch made from here: who (agent name, Claude session, pid) and where (worktree)."""
    if not branch and not run_id:
        raise SystemExit("agentctl: a dispatch is recorded by its branch or its run id")
    wt = worktree or worktree_of(safe_cwd())
    pid = pid or _claude_pid()
    return save_dispatch({"branch": branch, "run_id": int(run_id) if str(run_id or "").isdigit() else None,
                          "workflow": workflow, "purpose": purpose, "inputs": inputs, "worktree": wt, "sha": sha,
                          "session": os.environ.get("CLAUDE_CODE_SESSION_ID") or None,
                          "agent": agent or agent_name(), "pid": pid,
                          "process_start": process_start(pid) if pid else None, "at": ts(), "epoch": time.time(),
                          "source": source}, key)


def dispatch_records():
    out = []
    for p in sorted(glob.glob(dispatches_dir("*.json"))):
        r = read_record(p)
        if r:
            out.append(r)
    return out


def dispatch_for(branch, run_id=None):
    """The owner record of a branch run: its run's, else its branch's (None when there is none)."""
    for key in ([dispatch_key(None, run_id)] if run_id else []) + ([dispatch_key(branch)] if branch else []):
        r = read_record(dispatches_dir(f"{key}.json"))
        if r and (r.get("worktree") or r.get("agent") or r.get("session")):
            return r
    return None


def tracked_branch(name, recorded=()):
    return bool(name) and name not in PROTECTED_BRANCHES and (name.startswith(DISPATCH_PATTERNS) or name in recorded)


INTERNAL_TREE = re.compile(r"-wt-(check-\d+|waiting|batch)$")  # legacy fallback: trees made before they were tagged


def internal_tree(path):
    """agentctl's own trees -- tagged at creation (mark_internal): the queue's check trees, the baseline / recheck /
    `waiting` / batch trees, a finished red fixer's tree, the queue's work clone, tool caches -- never an owner, author
    or fixer (2026-10-27: the status line said `main #2146 → check-1 committed` for a fix that had landed; ci-why
    credited movies-wt-baseline-65721). A queued change's owner is its queue entry's source worktree."""
    import hashlib
    p = os.path.realpath(path or "")
    return os.path.exists(internal_trees_dir(hashlib.sha1(p.encode()).hexdigest() + ".json")) \
        or bool(INTERNAL_TREE.search(p.rstrip("/"))) or p.startswith(os.path.realpath(CACHE_TREES).rstrip("/") + "/") \
        or p.startswith(os.path.realpath(waiting_dir()).rstrip("/") + "/")


def worktree_holding(repo, sha):
    """The worktree whose HEAD is sha, else the one whose HEAD reflog holds it (the agent's own commit, since rebased).
    Never one of agentctl's internal trees."""
    try:
        trees = [w for w in worktrees(repo) if isinstance(w.get("worktree"), str) and os.path.isdir(w["worktree"])
                 and not internal_tree(w["worktree"])]
    except (RuntimeError, OSError):
        return None
    trees = trees[1:] + trees[:1]  # an agent's worktree before the root checkout (often on main itself)
    for w in trees:
        if str(w.get("HEAD", "")).startswith(sha):
            return w["worktree"]
    for w in trees:
        r = git(w["worktree"], "reflog", "--format=%H", "HEAD", check=False)
        if r.returncode == 0 and any(h.startswith(sha) for h in r.stdout.split()):
            return w["worktree"]
    return None


def queue_entry_worktree(entry_id):
    for p in glob.glob(os.path.join(waiting_dir(), "*", "entries", f"{entry_id}.json")):
        e = read_record(p) or {}
        if e.get("worktree"):
            return e["worktree"]
    return None


def branch_base(branch, run):
    """The commit a branch run was built on: a measure/<user>/<sha>-<x> branch names it (identity-ci-run pushes main's CI
    laid over it), else the run's head commit."""
    m = MEASURE_BRANCH.match(branch or "")
    return m.group(1) if m else run.get("headSha")


def owner_of(repo, branch, run):
    """The owner record of a branch run, matched after the fact (and recorded) when nobody recorded one."""
    rid = run.get("databaseId")
    rec = dispatch_for(branch, rid)
    if rec and owner_gone(rec):  # its worktree removed / pid dead: a live worktree holding its commit takes it over
        base = branch_base(branch, run)
        wt = repo and base and commit_on_main(repo, base) is False and worktree_holding(repo, base)  # main's are everywhere
        if wt:  # another worktree, or the same one outliving its agent's process: the worktree owns it now
            new = {"branch": branch, "run_id": rid, "workflow": run.get("workflowName"), "worktree": wt, "sha": base,
                   "purpose": f"re-matched: its owner {owner_text(rec)} is gone", "source": "matched", "at": ts(),
                   "epoch": time.time()}
            for key in ([dispatch_key(None, rid)] if rid else []) + [dispatch_key(branch)]:  # REPLACED, not merged:
                if key == dispatch_key(branch) or os.path.exists(dispatches_dir(f"{key}.json")):  # no dead pid stays
                    write_json(dispatches_dir(f"{key}.json"), new)
            return new
        return rec  # nobody holds it: orphan_why closes it, naming the gone owner
    if rec:
        return rec
    key = dispatch_key(branch)
    old = read_record(dispatches_dir(f"{key}.json")) or {}
    if old.get("source") == "unmatched" and time.time() - float(old.get("epoch") or 0) < OWNER_REMATCH:
        return None
    wt, how = None, None
    if branch.startswith("check/"):
        # the lane and mode prefixes off (remote_check: check/red-<...>; remote_mode_prefix: check/fast-<id>,
        # check/verify-<sha>-<id>): the entry's own id
        wt, how = (queue_entry_worktree(re.sub(r"^(red-)?(fast-|verify(areas)?-[0-9a-f]{40}-)?", "", branch[len("check/"):])),
                   "its queue entry")
    base = branch_base(branch, run)
    if not wt and repo and base:
        wt, how = worktree_holding(repo, base), f"the worktree that holds {base[:9]}"
    if not wt:
        save_dispatch({"branch": branch, "run_id": rid, "workflow": run.get("workflowName"), "source": "unmatched",
                       "at": ts(), "epoch": time.time()}, key)
        return None
    return save_dispatch({"branch": branch, "run_id": rid, "workflow": run.get("workflowName"), "worktree": wt,
                          "sha": base, "purpose": f"matched by branch name ({how})", "source": "matched",
                          "at": ts(), "epoch": time.time()}, key)


def owner_gone(rec):
    """True when the owner is gone: its worktree removed, or its recorded pid dead. None: no owner known."""
    if not rec or not (rec.get("worktree") or rec.get("pid")):
        return None
    if rec.get("worktree") and not os.path.isdir(rec["worktree"]):
        return True
    if rec.get("pid") and not proc_alive(rec["pid"], rec.get("process_start")):
        return True
    return False


def commit_on_main(repo, base):
    """True/False: base is on main (ancestor or patch-equivalent); None: unknown here (no repo or no base). A commit
    this clone does not hold is not on main."""
    if not repo or not base:
        return None
    if not git_ok(repo, "cat-file", "-e", f"{base}^{{commit}}"):
        return False
    try:
        return bool(merged_reason(repo, base, f"refs/remotes/{remote()}/main"))
    except (Exception, SystemExit):
        return None


def orphan_why(repo, rec, base, main_runs_it=True, main_since=None):
    """Why a branch red is an ORPHAN, else None: no LIVE owner (no record, or its worktree removed / pid dead -- 2026-11-02:
    eight reds sat on the status line naming worktrees removed days before) and no worktree holding its commit, AND nothing on main
    to compare it with -- its commit is not on main, or it is but main runs no such workflow (it can never PREDICT MAIN
    RED). A commit on main whose workflow main runs keeps today's behaviour; an unknown answer is never an orphan."""
    gone = owner_gone(rec)
    if rec and not gone:  # a live owner (or one we can't judge: no worktree or pid recorded) keeps it
        return None
    on = commit_on_main(repo, base)
    if on is None:
        return None
    sha = str(base)[:9]
    why_gone = ("its worktree removed" if rec and rec.get("worktree") and not os.path.isdir(rec["worktree"])
                else "its process ended")
    who = (f"its owner {owner_text(rec, short=True)} is gone ({why_gone})" if gone else "no owner record") \
        + f", no worktree holds its commit {sha}"
    if not on:
        return f"{who}, and it is not on main"
    if not main_runs_it:
        return f"{who}, and main runs no such workflow"
    if main_since:  # main's own run of the workflow came after it, on a commit holding this one: it predicts nothing
        return f"{who}, and main's own run #{main_since} came since"
    return None


def owner_text(rec, short=False):
    if not rec:
        return "NOBODY (no owner record, no worktree holds its commit)"
    wt = rec.get("worktree") or ""
    who = rec.get("agent") or (f"session {rec['session'][:8]}" if rec.get("session") else None)
    if short:
        return os.path.basename(wt) if wt else (who or "?")
    return wt.replace(HOME, "~") + (f" ({who})" if who and who != os.path.basename(wt) else "") if wt else (who or "?")


# -- an ORPHAN branch red closes itself (the owner, 2026-10-29: `branch reds: 3 → NOBODY` -- identity runs #2365, #2370,
# #2371 on measure/* branches whose worktrees were removed -- sat unassigned forever). The "branch-red" machine holds
# the ones that closed: orphaned (no owner, nothing on main to compare with; its measure/* branch is for `agentctl
# cleanup`) -> open again when its commit turns up in a worktree or an owner record (routed to that owner) -> gone when
# its branch is deleted or a newer run on it is green.
def branch_red_id(o_r, rid):
    return f"{o_r}#{rid}"


def orphaned_red(store, o_r, runs):
    """The why of a run of these whose branch red closed as an orphan (settle_branch_reds), else None."""
    for r in runs or []:
        try:
            it = store and store.item("branch-red", branch_red_id(o_r, r.get("databaseId")))
        except (Exception, SystemExit):
            return None
        if it and it["state"] == "orphaned":
            return it["data"].get("why") or "no owner"
    return None


def orphan_branches(repo, o_r, dry, out, store, runs_for=None):
    """The sweep's part: remote tracked branches whose owner is gone (worktree removed or pid dead), or whose branch red
    closed as an orphan (no owner at all), and whose runs all finished. measure/* ones are deleted (the standing OK: a
    finished measure/* branch); the rest are listed."""
    # listing: one branch's runs; a branch is swept only when every run listed finished (an unlisted older one is done)
    runs_for = runs_for or (lambda b: gh_json(o_r, "run", "list", "--branch", b, "--limit", "20", "--json", RUN_FIELDS))
    recorded = {r.get("branch") for r in dispatch_records() if r.get("branch")}
    found = []
    st = store
    for line in git(repo, "ls-remote", "--heads", remote(), check=False).stdout.splitlines():
        sha, ref = line.split()
        b = ref.removeprefix("refs/heads/")
        if not tracked_branch(b, recorded) or b in SWEEP_KEEP_REMOTE:
            continue
        try:
            runs = runs_for(b)
        except (Exception, SystemExit) as e:
            out.append(f"  branch {b}: its runs unread ({str(e)[:100]}), kept")
            continue
        if any(r.get("status") != "completed" for r in runs):
            continue
        rec = owner_of(repo, b, (runs or [{}])[0] | {"headSha": sha}) if runs else dispatch_for(b)
        gone = owner_gone(rec)
        orphan = None if gone or rec else orphaned_red(st, o_r, runs)
        if not gone and not orphan:
            continue
        found.append(b)
        what = (f"owner {owner_text(rec)} gone" if gone else f"its red closed as an orphan ({orphan})") \
            + f", {len(runs)} run(s) finished"
        if not b.startswith("measure/"):
            out.append(f"  OWNER GONE branch {b} {sha[:10]}: {what} (listed, kept)")
        elif dry:
            out.append(f"  would delete OWNER GONE branch {b} {sha[:10]}: {what}")
        else:
            try:
                delete_remote_branch(repo, o_r, b, sha)
                log_deletion("remote-branch", o_r, b, sha, f"sweep: {what}")
                out.append(f"  deleted OWNER GONE branch {b} {sha[:10]}: {what}")
            except Exception as e:
                out.append(f"  OWNER GONE branch {b} not deleted: {e}")
    return found
