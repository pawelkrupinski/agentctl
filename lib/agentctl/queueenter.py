# agentctl, part `queueenter`: entering the land queue: enqueue, the server submit, the precheck and land-until-done
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import argparse
import datetime as dt
import os
import subprocess
import time
from agentctl.procrunner import process_runner
from agentctl.core import (
    MAX_FOREIGN, NO_SIGN, branch_of, cherry_landed, env_num, err, fmt_age, git, git_ok, host_role, process_cache,
    remote, scratch)
from agentctl.policy import policy_blocks
from agentctl.trailers import do_not_land, held_commit
from agentctl.waitingdir import waiting_state
from agentctl.storetoken import no_vault
from agentctl.lander import commit_keys, lander_record
from agentctl.inboxentry import inbox_write
from agentctl.land import landed, run_watched, tool_env
from agentctl.worktrees import WorktreeGuard
from agentctl.conflicts import conflict_resolve
from agentctl.redfixes import fixes_of
from agentctl.redreplay import fix_replay_refusal, run_fix_replays
from agentctl.queueuntil import (
    UNTIL_DONE_AGAIN, UNTIL_DONE_DEADLINE, next_command, own_change_item, until_done_finish)
from agentctl.waiting import diagnoses_of, waiting_commits
from agentctl.queuesubmit import (
    FAST_PRECHECK, FAST_PRECHECK_BOUND, await_queued, await_verdict, change_decided, landed_sha_of, note_precheck,
    precheck_reason, push_failure_why, queued_tail, start_fast_precheck, submit_ref, take_delivery,
    verdict_still_holds)
from agentctl.dispatchblock import in_fixer
from agentctl.blamerecords import blame_land_refusal, blame_records_quiet
from agentctl.tools import check_range
from agentctl.server import queue_change
from agentctl.syncsteps import submit_remote
from agentctl.processorlink import offer_token, spawn_waiting_processor
from agentctl.landqueue import queue_command
from agentctl.statemachines import machine_state


def change_fields(repo, main_ref, head, a):
    """A queue entry's fields for the change at head in repo (land's own submission)."""
    commits = waiting_commits(repo, f"{remote()}/main", head)
    return {"worktree": repo, "branch": branch_of(repo), "head": head, "commits": commits,
            "fixes": sorted(fixes_of(repo, commits)), "diagnoses": sorted(diagnoses_of(repo, commits)) or None,
            "keep_worktree": bool(getattr(a, "keep_worktree", False)),
            "no_batch": bool(getattr(a, "no_batch", False)),
            "claude_pid": int(os.environ["CLAUDE_PID"]) if (os.environ.get("CLAUDE_PID") or "").isdigit() else None,
            "session": os.environ.get("CLAUDE_CODE_SESSION_ID") or None,
            "base": git(repo, "rev-parse", main_ref).strip(), "host": os.uname().nodename,
            # THE LANDER (lander.py): who landed it, and each commit's keys to find it on main after the rebase
            "lander": lander_record(repo), "commit_keys": commit_keys(repo, main_ref, head),
            # FIX REPLAY (redreplay.py): agentctl's own replays of the push, for the server's check
            "replays": fix_replay_proofs(repo, head) or None}


def submit_to_server(repo, d, fields, store, note="", after_queued=None, waits=False):
    """THE LAPTOP'S ONLY WAY INTO THE QUEUE (phase 2; it holds no queue of its own): the change pushed to
    refs/heads/land/<id>, then submitted to ci-runner's queue (the store unreachable, or no token here: the outbox,
    which the sync sends). Prints QUEUED / QUEUED-LOCAL / NOT LANDED; returns land's exit code. waits: this land then
    waits for the verdict itself (a red fixer's, fixer_land)."""
    head = fields["head"]
    e = {**fields, "id": f"{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}-{os.path.basename(repo)}-{os.urandom(3).hex()}"}
    if not submit_ref(repo, d, e, to_server=True):
        print(f"NOT LANDED: could not push the change to refs/heads/land/{e['id']} (ci-runner checks it from there): "
              f"{push_failure_why(e.get('push_error'))}: land again")
        return 1
    r = submit_remote(store, e)
    offer_token(repo)  # this agent's token to the laptop processor, which holds none after a restart
    if r.get("outbox"):
        why = ("no store token in this process (none in its environment, the processor's socket held none; land "
               "never runs `secrets`): the sync submits it with its own token" if "AGENTCTL_STORE_TOKEN" in r["outbox"]
               else f"the store is unreachable ({r['outbox']}); the sync submits it when it answers")
        print(f"QUEUED-LOCAL {e['id']} ({head[:10]}){note}: {why}; {queued_tail(waits)}", flush=True)
        if after_queued:
            after_queued(e["id"])
        return 0
    ahead = r.get("ahead") or 0
    print(f"QUEUED {r['id']} ({head[:10]}{f', {ahead} ahead' if ahead > 0 else ''}){note}: its checks run on ci-runner; "
          f"{queued_tail(waits)}", flush=True)
    if after_queued:
        after_queued(r["id"])
    return 0


def cmd_land_precheck(a, store):
    """The detached half of start_fast_precheck: run the repo's precheck, and on a refusal cancel the queued change."""
    # Its own process group, ended WHOLE at the bound (review 2026-11-07: subprocess.run killed only the script, so what
    # it started -- sbt, a test JVM -- lived on, and kept the output pipe, and so this wait, open): run_watched.
    t0 = time.time()
    rc, out = run_watched([os.path.join(a.repo, FAST_PRECHECK), "--base", a.base, "--head", a.head], a.repo, None,
                          os.path.join(scratch(), f"agentctl-precheck-{a.entry}"), "precheck",
                          stall=FAST_PRECHECK_BOUND, wall=FAST_PRECHECK_BOUND)
    note_precheck(a.entry, time.time() - t0, rc, out, store)
    if rc is None:
        print(f"{out or ''}PRECHECK UNCHECKED: past its {FAST_PRECHECK_BOUND} s bound, ended with its children: "
              "could not check, its landing check judges it", flush=True)
        return 0
    lines = (out or "").splitlines()
    print("\n".join(lines), flush=True)
    if rc != 1:
        return 0
    last = next((l for l in reversed(lines) if l.startswith("PRECHECK ")), lines[-1] if lines else "exit 1")
    why = precheck_reason(lines)
    last = f"{last} -- {why}" if why else last
    cancel = argparse.Namespace(wcmd="cancel", repo=a.repo, entry=a.entry, renamed=True, hold_for=None, hold_why=None)
    # NOT LANDED only when the cancel took: the landing check runs alongside and may have decided first (review
    # 2026-11-07: a change that LANDED was told NOT LANDED, "its queue entry is cancelled").
    try:
        rc = queue_command(cancel, store)
    except (Exception, SystemExit) as e:  # the store unreachable: the change is still queued, its check judges it
        err(f"could not cancel {a.entry}: {e}")
        verdict = (f"PRECHECK REFUSED {a.entry} ({a.head[:10]}): {last}; it could not be cancelled ({str(e)[:120]}), so "
                   f"the landing check still judges it -- `agentctl land cancel {a.entry}` to drop it, fix it in {a.repo}")
        inbox_write("land", verdict, [verdict], [l for l in lines if l.strip()][-40:], 1, cwd=a.repo)
        print(verdict, flush=True)
        return 1
    if rc != 0:
        print(f"the fast precheck refused {a.entry}, but it is already decided (no open change to cancel): its own "
              "verdict stands", flush=True)
        return 0
    verdict = (f"NOT LANDED: the fast precheck refused {a.entry} ({a.head[:10]}): {last}; its queue entry is cancelled. "
               f"Fix it in {a.repo}, then land again")
    inbox_write("land", verdict, [verdict], [l for l in lines if l.strip()][-40:], 1, cwd=a.repo)
    print(verdict, flush=True)
    return 1


def enqueue(repo, main_ref, sh, a, clock, store):
    """Rebase, squash guard, then the change becomes a queued entry: prints QUEUED and starts the processor. A red
    fixer's land then waits for the verdict (fixer_land)."""
    rc, head = sync(repo, main_ref, sh, clock, getattr(a, "release_hold", False))
    if rc is not None:
        return rc
    d, _clone, _wt = waiting_state(repo)
    fixer = in_fixer()
    waits = fixer or bool(getattr(a, "until_done", False))  # this land waits for the verdict itself
    a.queued_id = None

    def after_queued(eid):
        a.queued_id = eid
        if not waits:  # one that waits runs it in the foreground (fixer_land, land_until_done)
            start_fast_precheck(repo, main_ref, head, eid, a)
    if host_role() == "sync":  # THE LAPTOP'S LAND NEEDS NO VAULT (see submit_to_server)
        with no_vault():
            rc = submit_to_server(repo, d, change_fields(repo, main_ref, head, a), store, after_queued=after_queued,
                                  waits=waits)
    else:
        fields = change_fields(repo, main_ref, head, a)
        e, ahead = queue_change(d, fields)
        submit_ref(repo, d, e)
        print(f"QUEUED {e['id']} ({head[:10]}{f', {ahead} ahead' if ahead > 0 else ''}): its checks run in the "
              f"background; {queued_tail(waits)}", flush=True)
        after_queued(e["id"])
        spawn_waiting_processor(repo)
        offer_token(repo)
        rc = 0
    if rc != 0 or not waits or not a.queued_id:
        return rc
    if not fixer:  # land --until-done: its caller waits (land_until_done)
        return foreground_precheck(repo, main_ref, head, a.queued_id, a, store)
    return fixer_land(repo, main_ref, head, a.queued_id, a, clock, store)


def foreground_precheck(repo, main_ref, head, eid, a, store):
    """The fast precheck of queued change eid in THIS process (a land that waits for its verdict): 1 when it refused
    (its NOT LANDED or PRECHECK REFUSED line the last printed), else 0."""
    if not getattr(a, "no_fast_precheck", False) and os.access(os.path.join(repo, FAST_PRECHECK), os.X_OK):
        base = git(repo, "rev-parse", main_ref).strip()
        if cmd_land_precheck(argparse.Namespace(entry=eid, repo=repo, base=base, head=head), store) == 1:
            return 1
    return 0


def fixer_land(repo, main_ref, head, eid, a, clock, store):
    if foreground_precheck(repo, main_ref, head, eid, a, store) == 1:
        return 1
    return await_queued(eid, clock, store)


def land_until_done(repo, main_ref, sh, a, clock, store):
    """`land --until-done` (LAND --UNTIL-DONE above)."""
    t0 = clock.time()
    bound = a.deadline if a.deadline is not None else env_num("AGENTCTL_UNTIL_DONE_DEADLINE", UNTIL_DONE_DEADLINE)
    head = git(repo, "rev-parse", "HEAD").strip()
    it = own_change_item(repo, head, store)
    if it and it["state"] == "landed":
        eid = it["id"]
    elif it and it["state"] == "rejected" and verdict_still_holds(repo, main_ref, it):
        # A VERDICT IS DURABLE (above): decided while no call waited, on this very main, said now
        take_delivery(it, store)
        why = str((it.get("data") or {}).get("why") or f"{it['id']} rejected")
        print(f"DECIDED {it['id']} ({head[:10]}) while no --until-done waited:", flush=True)
        print(why if why.startswith("NOT LANDED") else f"NOT LANDED: {why}", flush=True)
        print(next_command(repo, why) + f" (to queue {head[:10]} again unchanged: ~/bin/agentctl land)", flush=True)
        return 1
    elif it and not machine_state("change", it["state"]).terminal:
        eid = it["id"]
        print(f"RESUMING {eid} ({head[:10]}, {it['state']}): waiting for its verdict", flush=True)
    else:
        git(repo, "fetch", "--quiet", remote(), "main", check=False)
        commits, left = cherry_landed(repo, main_ref, "HEAD")
        if commits is not None and not left and git_ok(repo, "rev-parse", "--verify", "-q", main_ref):
            # nothing of its own left to land (it reached main another way, or there never was a commit)
            return until_done_finish(repo, "-", git(repo, "rev-parse", main_ref).strip(), a)
        a.until_done = True
        rc = enqueue(repo, main_ref, sh, a, clock, store)
        eid = getattr(a, "queued_id", None)
        if rc != 0 or not eid:
            print(next_command(repo, ""), flush=True)
            return 1
    state, data, _waited = await_verdict(eid, max(0.0, bound - (clock.time() - t0)), clock, store)
    if state == "landed":
        sha = data.get("landed_sha") or landed_sha_of(eid, data)
        print(f"LANDED {sha} (queued {eid})", flush=True)
        return until_done_finish(repo, eid, sha, a, (it if it and it["id"] == eid else None)
                                 or store.item("change", eid))
    if change_decided(state):
        why = str(data.get("why") or f"{eid} {state}")
        print(why if why.startswith("NOT LANDED") else f"NOT LANDED: {why}", flush=True)
        print(next_command(repo, why), flush=True)
        return 1
    print(f"NOT DONE: {eid} is {state} after {fmt_age(clock.time() - t0)} (the --deadline); it stays queued -- run "
          f"{UNTIL_DONE_AGAIN} again from {repo}: it resumes this entry", flush=True)
    return 2


def rebase_in_progress(repo):
    """The worktree's rebase-merge / rebase-apply directory when a rebase is in progress there, else None."""
    for name in ("rebase-merge", "rebase-apply"):
        p = git(repo, "rev-parse", "--path-format=absolute", "--git-path", name, check=False).stdout.strip()
        if p and os.path.isdir(p):
            return p
    return None


# EVERY NOT LANDED LEAVES NO REBASE BEHIND (issue 20261119T162652-494caa, movies-wt-owner-outside-gate 2026-11-19: a land
# ended NOT LANDED with its rebase onto origin/main in progress; later commits went onto the rebase's detached HEAD, and
# the next land's `rebase --abort` reset the branch to the pre-rebase commit, dropping them). A land that finds a rebase
# already in progress refuses -- it is the agent's to finish or abort, never land's to discard -- and a rebase land
# started is aborted on whatever path land leaves it unfinished.
def sync(repo, main_ref, sh, clock, release_hold=False):
    """sync_onto_main, never leaving a rebase it started in progress; refused when one is in progress already."""
    pending = rebase_in_progress(repo)
    if pending:
        print(f"NOT LANDED: a rebase is in progress in this worktree ({pending}): finish it (`git rebase --continue`) "
              "or abort it (`git rebase --abort`) yourself -- commits made since it started are on its detached HEAD, "
              "and an abort resets the branch -- then land again")
        return 1, None
    with WorktreeGuard(repo):
        return sync_onto_main(repo, main_ref, sh, clock, release_hold)


def sync_onto_main(repo, main_ref, sh, clock, release_hold=False):
    """Fetch, rebase onto origin/main, the squash guard: (rc, None) when land is decided here, else (None, head).
    A commit with a `Hold: <why>` trailer is refused unless release_hold (`land --release-hold`: the owner's go)."""
    r = sh("git", "fetch", "--quiet", remote(), "main")
    if r.returncode:  # a tunnel restart is seconds long: one more try before giving up
        err(f"git fetch failed ({r.stderr.strip()[:120]}); retrying once in 15 s")
        clock.sleep(15)
        r = sh("git", "fetch", "--quiet", remote(), "main")
    if r.returncode:
        print(f"NOT LANDED: git fetch {remote()} main failed: {r.stderr.strip()[:300]} (tunnel? `agentctl env --check`)")
        return 1, None
    head = git(repo, "rev-parse", "HEAD").strip()
    if landed(repo, head, main_ref):
        print(f"LANDED {head} (already on {remote()}/main)")
        return 0, None
    why = do_not_land(repo, waiting_commits(repo, f"{remote()}/main", head), branch_of(repo))
    if why:
        print(f"NOT LANDED: {why}. Throwaway work never lands; remove the marker (rename the branch, reword the commit, "
              "drop the Do-Not-Land: trailer) only if the owner wants it landed.")
        return 1, None
    # policy: hold-trailer
    why = None if release_hold else held_commit(repo, waiting_commits(repo, f"{remote()}/main", head))
    if why:
        print(f"NOT LANDED: {why}. Held work lands only on the owner's go: `agentctl land --release-hold` then (or "
              "drop the trailer).")
        return 1, None
    # policy: blame-dead
    why = blame_land_refusal(repo, waiting_commits(repo, f"{remote()}/main", head), repo,
                             records=blame_records_quiet())
    if why:
        print(f"NOT LANDED: {why}. A withdrawn or refuted blame revert is dead: drop this worktree "
              "(`git worktree remove`), never land it.")
        return 1, None
    # agentctl's own rewrites never sign (issue 20261115T112749-d27361): main's commits are unsigned, and a global
    # commit.gpgsign=true with an empty ssh-agent (an unattended fixer) failed every rebase behind main
    r = sh("git", *NO_SIGN, "rebase", "--quiet", f"{remote()}/main")
    for _ in range(50):  # each stop whose conflicts are docs or generated files only: resolved (conflict_resolve), and on
        if not r.returncode or not conflict_resolve(repo):
            break
        r = sh("git", *NO_SIGN, "-c", "core.editor=true", "rebase", "--continue")
    if r.returncode:
        conflicts = git(repo, "diff", "--name-only", "--diff-filter=U", check=False).stdout.split()
        sh("git", "rebase", "--abort")
        if conflicts:
            print(f"NOT LANDED: rebase onto {remote()}/main conflicts in: {', '.join(conflicts)}. "
                  f"Resolve by hand (git rebase {remote()}/main), then land again.")
        else:  # no conflicted file: git itself failed (a hook, signing, a lock), never told as a conflict
            print(f"NOT LANDED: rebase onto {remote()}/main failed (no conflict): {r.stderr.strip()[:300]}")
        return 1, None
    head = git(repo, "rev-parse", "HEAD").strip()
    if landed(repo, head, main_ref):
        print(f"LANDED {head} (the rebase left nothing: already on {remote()}/main)")
        return 0, None
    head = land_before_queue(repo, head)
    if fix_replay_blocks(repo, head):
        return 1, None
    problems = check_range(repo, f"{remote()}/main", head, [], int(env_num("AGENTCTL_MAX_FOREIGN", MAX_FOREIGN)))
    if problems:
        print("NOT LANDED: prepush-check refused:\n  " + "\n  ".join(problems))
        return 1, None
    return None, head


# THE REPO'S OWN STEP AFTER THE REBASE (2026-11-20, the movies stamp carry: a proven-equivalent identity change keeps its
# stamps only if a lane remembers to write the carry rows, and 3-7 of 37 stamp-moving deploys would have): a repo's
# executable LAND_BEFORE_QUEUE runs with <origin/main> <head> on the rebased change, and may commit onto it -- the commit
# then rides the same push, judged by the same checks. It never refuses a land: a failure or a timeout is said and the
# head stays as it was.
LAND_BEFORE_QUEUE = "scripts/land-before-queue"


LAND_BEFORE_QUEUE_SECONDS = 1200


def land_before_queue(repo, head):
    """The head after the repo's LAND_BEFORE_QUEUE step: head itself, or a descendant the step committed onto it."""
    tool = os.path.join(repo, LAND_BEFORE_QUEUE)
    if not os.access(tool, os.X_OK):
        return head
    base = git(repo, "rev-parse", f"{remote()}/main").strip()
    branch = git(repo, "symbolic-ref", "-q", "--short", "HEAD", check=False).stdout.strip()
    try:
        r = process_runner().run([tool, base, head], cwd=repo, capture_output=True, text=True, env=tool_env(),
                           timeout=env_num("AGENTCTL_LAND_BEFORE_QUEUE_SECONDS", LAND_BEFORE_QUEUE_SECONDS))
        for line in (r.stdout + r.stderr).strip().splitlines()[-12:]:
            err(f"{LAND_BEFORE_QUEUE}: {line}")
    except subprocess.TimeoutExpired:
        err(f"{LAND_BEFORE_QUEUE}: timed out; the change lands as it was")
    now = git(repo, "rev-parse", "HEAD").strip()
    if now != head and git(repo, "merge-base", "--is-ancestor", head, now, check=False).returncode != 0:
        err(f"{LAND_BEFORE_QUEUE}: left HEAD off the change ({now[:10]}): put back on {head[:10]}")
        if branch:
            git(repo, "checkout", "-q", "--force", branch)
        git(repo, "reset", "-q", "--hard", head)
        return head
    if now != head:
        err(f"{LAND_BEFORE_QUEUE}: added {git(repo, 'rev-list', '--count', f'{head}..{now}').strip()} commit(s) to the change")
    return now


# FIX REPLAY at land (redreplay.py): on the rebased head, agentctl runs each bare `Fix-Replay: <command>` of a Fixes-Red push
# at origin/main and at head (once per base, head and command), keeps the proof for the submission (change_fields'
# `replays`), and refuses a gate red's fix it does not prove; a red outside the gate only warns.
_fix_replay_proofs = process_cache({})  # (base, head) -> [proof]


def fix_replay_proofs(repo, head):
    """agentctl's replays of the push origin/main..head (run_fix_replays), run once per base and head."""
    base = git(repo, "rev-parse", f"{remote()}/main").strip()
    if (base, head) not in _fix_replay_proofs:
        _fix_replay_proofs[(base, head)] = run_fix_replays(repo, base, head, waiting_commits(repo, base, head))
    return _fix_replay_proofs[(base, head)]


def fix_replay_blocks(repo, head):
    """True (its NOT LANDED printed) when FIX REPLAY refuses the push origin/main..head."""
    commits = waiting_commits(repo, f"{remote()}/main", head)
    verdict = fix_replay_refusal(repo, commits, proofs=fix_replay_proofs(repo, head))
    if not verdict:
        return False
    pid, why = verdict
    if pid == "fix-replay":
        blocks = policy_blocks("fix-replay", None, None, why[:160])  # policy: fix-replay
    else:
        blocks = policy_blocks("fix-replay-outside", None, None, why[:160])  # policy: fix-replay-outside
    if blocks:
        print(f"NOT LANDED: {why}. A red fix lands only once a replay proved it (or says why none can).")
    return blocks
