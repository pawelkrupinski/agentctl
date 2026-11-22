# agentctl, part `batchcheck`: the batch's executor (`waiting check-batch`): its red pre-check, its check, the drift check and split, an infra failure retried
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import contextlib
import os
import sys
from typing import Any
from agentctl.core import env_num, err, git, git_ok, slug, state_root
from agentctl.timefmt import ts
from agentctl.classify import INFRA_LOG, _test_line
from agentctl.store import StaleVersion, machine_state, open_store
from agentctl.forge import forge_env
from agentctl.waitingdir import save_entry, waiting_state, wlog
from agentctl.leases import LeaseHold
from agentctl.land import REDGATE, run_watched, tool_env
from agentctl.queueslots import CHECK_LEASE_TTL, queue_verdict, tree_slot
from agentctl.queue import INFRA_CHECK_FAILURE
from agentctl.identitychange import identity_model_commits
from agentctl.waiting import tail_of, waiting_commits
from agentctl.remote import batch_check, bisect
from agentctl.queuediff import CHECK_TIMEOUT, judge_failure
from agentctl.recovery import batch_move
from agentctl.queuepush import DEEP_BATCHES
from agentctl.batchitem import batch_fail, batch_members


BATCH_LEASE_MAX = 6 * 3600  # the batch executor's leases: its check (2 h) and its bisect (4 h), its machine's bounds


def red_prechecked(d, st, it, members, tip, base, how, failed=None):
    """A red pre-check passed for members (the whole batch, or the prefix before a culprit; none: nothing passed): the
    verdict kept on the batch and on each member, the batch blocked by the red again. failed: the main and members it
    failed for as they are (not pre-checked again until either changes)."""
    for e in members:
        e.update(prechecked=it["id"], checked_base=base)
        save_entry(d, e)
    pre = {"members": [e["id"] for e in members], "base": base, "tip": tip} if members else None
    batch_move(st, it, "blocked-red", {"prechecked": pre, "precheck_failed": failed},
               text=f"batch {it['id']}: {how}" + (f": pre-checked {len(members)} change(s) as one at {tip[:10]}"
                                                   if members else ""))


DRIFT_CI = "scripts/identity-drift-ci"


DRIFT_STALL = 30 * 60  # s with no output before the drift check is ended (no verdict: the batch goes on)


# THE DRIFT CHECK'S TOOLS (2026-10-30, batch #4: "could not push measure/ci-lander/f3b716998-drift-aed681"). It was no
# permission: the lander has write on movies and only main is protected, the same as the check/<id> branches it pushes
# for every change. The reconciler's systemd PATH has no python3 and no gh. movies' lock_take (jvm-gate.sh) flocks
# through `python3`, so identity_push_measure_branch waited 3600 s on a lock that could never be taken and gave up
# before it pushed. forge-gh, the script's `gh`, is python3 too. drift_env gives the script both: python3 (a shim to
# this interpreter, on the PATH only when none is there) and the repository's App token on ci-runner (forge_env), so
# the push and the dispatch run as the lander with the rights it already has.
def drift_env(repo, env=None):
    env = tool_env() if env is None else dict(env)
    try:
        env = forge_env(slug(repo), env)  # forge-gh is the real gh (KINOWO_FORGE), with the repo's App token on ci-runner
        env["KINOWO_FORGE"] = "github"
    except (Exception, SystemExit) as ex:  # no token: the script says so itself (NO VERDICT)
        err(f"drift check: no forge token for the script ({str(ex)[:120]})")
    parts = [p for p in env.get("PATH", "").split(":") if p]
    if not any(os.access(os.path.join(p, "python3"), os.X_OK) for p in parts):
        shim = state_root("tool-bin")
        link = os.path.join(shim, "python3")
        try:
            os.makedirs(shim, exist_ok=True)
            if os.path.realpath(link) != os.path.realpath(sys.executable):
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(link)
                os.symlink(sys.executable, link)
            env["PATH"] = ":".join(parts + [shim])
        except OSError as ex:
            err(f"drift check: no python3 for the script ({ex})")
    return env


def batch_drift_check(repo, d, tree, base, tip, against_main=False):
    """(verdict, output): True clean, False DRIFT RED, None no check (one identity commit or none, no script at the tip,
    or no verdict)."""
    commits = waiting_commits(tree, base, tip)
    n = len(identity_model_commits(tree, commits))
    if n <= 1:
        return None, ""
    if not git_ok(tree, "cat-file", "-e", f"{tip}:{DRIFT_CI}"):
        wlog(d, f"the batch {tip[:10]} holds {n} identity-model commits; no {DRIFT_CI} at its tip: no drift check")
        return None, ""
    git(tree, "checkout", "-q", "--force", "--detach", tip, check=False)
    argv = [os.path.join(tree, DRIFT_CI), tip] + (["--against", "main"] if against_main else [])
    wlog(d, f"the batch {tip[:10]} holds {n} identity-model commits: drift check of its tip ({' '.join(argv[1:])})")
    rc, out = run_watched(argv, tree, drift_env(repo), os.path.join(d, f"drift-{tip[:10]}"), "drift",
                          stall=env_num("AGENTCTL_DRIFT_STALL", DRIFT_STALL), quiet_ok=True)
    with open(os.path.join(d, "checks.log"), "a") as f:
        f.write(f"=== drift {ts()} {base[:10]}..{tip[:10]} rc {rc}\n{out}\n")
    last = ([l for l in (out or "").splitlines() if l.strip()] or ["(no output)"])[-1]
    if rc == 0:
        wlog(d, f"the drift check of {tip[:10]}: {last[:200]}")
        return True, out
    if rc == 1:
        wlog(d, f"the drift check of {tip[:10]} is RED: {last[:200]}")
        return False, out
    wlog(d, f"the drift check of {tip[:10]} gave no verdict (rc {rc}: {last[:200]}): the batch goes on")
    return None, out


def batch_drift_split(repo: str, d: str, st: Any, it: dict[str, Any], members: list[dict[str, Any]], tree: str,
                      out: str) -> int:
    """DRIFT RED on the batch tip: a lone identity change confirmed red is rejected; else every identity change is marked
    and the batch assembled again, one identity change per batch (drift_serialize)."""
    ident = [e for e in members if identity_model_commits(tree, e.get("commits") or [])]
    bid = it["id"]
    if len(ident) == 1:
        e = ident[0]
        ok, out2 = batch_drift_check(repo, d, tree, it["data"]["base"], e["tip"], it["data"].get("cls") == "red")
        it = st.item("batch", bid)
        if ok is False:
            # policy: drift-check
            queue_verdict(d, e, "rejected", f"NOT LANDED: the drift check (scripts/identity-drift-ci) is red with your "
                                            f"identity-model change in the batch, twice (entry {e['id']})"
                                            f"\n  {tail_of(out2 or out)}", (out2 or out).splitlines()[-25:])
            batch_move(st, it, "assembling", text=f"batch {bid}: {e['id']} rejected (drift red)", severity="escalate")
            return 1
        batch_move(st, it, "pushing", text=f"batch {bid}: its lone identity change's drift check passed when run again")
        return 0
    for e in ident:
        e["drift_split"] = bid
        save_entry(d, e)
    batch_move(st, it, "assembling", text=f"batch {bid}: its drift check is red with {len(ident)} identity-model "
                                          f"changes in it: split, one identity change per batch")
    return 1


def check_batch(repo: str, bid: str) -> int:
    """THE BATCH EXECUTOR (`waiting check-batch <id>`): holding check/batch-<id> and its tree's slot, it checks the
    assembled tip; pass -> pushing (the tick pushes); fail -> bisecting -> a confirmed culprit rejected and the passing
    prefix -> pushing (or assembling), every prefix passing -> assembling again (counted); stalled twice -> held."""
    d, _c, _w = waiting_state(repo)
    st = open_store()
    it = st.item("batch", bid)
    if not it or it["state"] not in ("checking", "bisecting"):
        return 0
    # ONE EXECUTOR HOLDS THE BATCH THROUGH ITS CHECK AND ITS WHOLE BISECT (several checks of up to 2 h each): under a
    # change's CHECK_LEASE_MAX (3 h) the bisect lost its lease mid-way, read as a stall, and blamed "flaky" -- the culprit
    # was never rejected. Its machine bounds it (checking 2 h + bisecting 4 h); the leases' max hold matches.
    lease = LeaseHold(f"check/batch-{bid}", env_num("AGENTCTL_CHECK_LEASE_TTL", CHECK_LEASE_TTL), BATCH_LEASE_MAX)
    if not lease.acquire():
        return 0
    i = it["data"]["tree"]
    slot = tree_slot(d, i, BATCH_LEASE_MAX)
    try:
        if not slot.acquire():
            return 0
        _d, clone, _w = waiting_state(repo)
        tree = f"{clone}-wt-check-{i}"
        base, tip, members = it["data"]["base"], it["data"]["tip"], batch_members(d, it)
        if not members:
            batch_move(st, it, "assembling", text=f"batch {bid}: its changes are gone")
            return 0
        depth, runs = it["data"].get("depth") or "fast", dict(it["data"].get("runs") or {})
        range_base = it["data"].get("range_base")
        if it["state"] == "checking":
            for attempt in range(BATCH_INFRA_RETRIES + 1):  # AN INFRA FAILURE IS NO BISECT (batch_infra_failure)
                ok, out, stalled = batch_check(repo, d, tree, base, tip, it["data"].get("cls") or "check", depth,
                                               runs, range_base=range_base)
                infra = None if ok or stalled else batch_infra_failure(out)
                if not infra or attempt == BATCH_INFRA_RETRIES:
                    break
                wlog(d, f"batch {bid}: {tip[:10]} failed on infra ({infra}): checked again, not bisected")
            it = st.item("batch", bid)
            if stalled:
                text = (f"HELD: the batch check of {tip[:10]} ({len(members)} change(s)) stalled twice on a fresh sbt "
                        "server; checked again in 30 min")
                batch_move(st, it, "held", {"why": "stalled"}, text=text, severity="escalate")
                return 0
            if ok and depth == "red":  # pre-checked as one while red: the verdict kept, blocked by the red again
                red_prechecked(d, st, it, members, tip, base, "its red pre-check passed")
                return 0
            if ok:
                drift, dout = batch_drift_check(repo, d, tree, base, tip, it["data"].get("cls") == "red")
                it = st.item("batch", bid)
                if drift is False:
                    return batch_drift_split(repo, d, st, it, members, tree, dout)
                batch_move(st, it, "pushing", {"runs": runs}, text=f"batch {bid}: {tip[:10]} passed its check"
                                                                  + (" and its drift check" if drift else ""))
                return 0
            if REDGATE.search(out or ""):
                batch_move(st, it, "assembling", text=f"batch {bid}: the gate went red")
                return 0
            it = batch_move(st, it, "bisecting", {"failed_out": (out or "")[-20000:]},
                            text=f"batch {bid}: {tip[:10]} failed its check; bisecting")
        culprit, cout = bisect(repo, d, members, base, tree, depth, runs, it["data"].get("failed_out"),
                               range_base=range_base, cls=it["data"].get("cls") or "check")
        it = st.item("batch", bid)
        if culprit is None and depth == "red":  # every prefix passed: no verdict, pre-checked again at the next change
            red_prechecked(d, st, it, [], None, base, "its red pre-check failed, then every prefix passed (flaky or "
                           "infra)", failed=[base] + [e["id"] for e in members])
            return 0
        if culprit is None:
            batch_fail(st, d, it, "it failed, then every prefix passed on its own (flaky or infra)", len(members))
            return 0
        bad = members[culprit]
        if depth == "red" and not culprit_own_failure(repo, d, bad, base, cout):
            # failing only as main fails (the red itself, judged as a lone change's check is): no culprit; the prefix
            # before it pre-checked, the rest checked again at green (the fast check, as before)
            red_prechecked(d, st, it, members[:culprit], members[culprit - 1]["tip"] if culprit else None, base,
                           f"{bad['id']} on fail only where main fails too", failed=[base] + [e["id"] for e in members])
            return 0
        # a member whose generated files the batch resolved (GENERATED FILES ARE NO OVERLAP, waiting.py) failing is
        # the resolution's as much as its own: its own check, on main, judges it
        # and one whose source conflict the batch auto-resolved (A SOURCE CONFLICT IS RESOLVED IN THE BATCH): the
        # resolution is the suspect -- named, and the member held behind the change it conflicts with, never rejected
        regenerated, resolved = bad.get("regenerated"), bad.get("autoresolved")
        alone = depth in DEEP_BATCHES and (bool(regenerated) or bool(resolved)
                                           or not culprit_own_failure(repo, d, bad, base, cout))
        if alone:  # not shown to be its own: its own check judges it (a base red, a time cap, an infra failure)
            bad["batch_alone"] = "resolve" if resolved else "regen" if regenerated else bid
            bad.pop("autoresolved", None)
            save_entry(d, bad)
            said = (f"batch {bid}: {bad['id']}'s prefix fails, but not shown to be by its own fault"
                    + (f" (its {', '.join(regenerated)} resolved in the batch)" if regenerated else "")
                    + (f" -- the batch's AUTO-RESOLUTION of {', '.join(m['file'] for m in resolved)} is the culprit "
                       f"({'; '.join(h for m in resolved for h in m['hunks'])}): held behind the change it "
                       "conflicts with, as before" if resolved else "") + ": checked alone")
            wlog(d, said)
            if resolved:  # its author hears it (the batch says its auto-resolution failed)
                cur = st.item("change", bad["id"])
                if cur and not machine_state("change", cur["state"]).terminal:
                    try:
                        st.transition("change", bad["id"], cur["state"], cur["version"], event={
                            "text": said, "audience": {"worktree": bad.get("worktree")}, "severity": "info"})
                    except StaleVersion:
                        pass  # said in the log; the hold says the rest
        else:
            # policy: batch-checks-failed
            queue_verdict(d, bad, "rejected", f"NOT LANDED: the checks failed with your change in the batch (entry "
                                              f"{bad['id']})\n  {tail_of(cout)}", cout.splitlines()[-25:])
        what = ("checked alone" + (" (its auto-resolution failed the batch)" if alone and resolved else "")) \
            if alone else "rejected"
        if depth == "red":  # the prefix before it pre-checked; the rest are pre-checked again at the next tick
            red_prechecked(d, st, it, members[:culprit], members[culprit - 1]["tip"] if culprit else None, base,
                           f"{bad['id']} {what}")
            return 0 if alone else 1
        if culprit > 0:  # the prefix before it passed its own check: it lands now; the rest join the next batch
            prefix = members[:culprit]
            batch_move(st, it, "pushing", {"members": [e["id"] for e in prefix], "tip": prefix[-1]["tip"], "runs": runs},
                       text=f"batch {bid}: {bad['id']} {what}; the {culprit} change(s) before it land")
        else:
            batch_move(st, it, "assembling", text=f"batch {bid}: {bad['id']} {what}")
        return 0 if alone else 1
    finally:
        slot.release()
        lease.release()


# AN INFRA FAILURE IS NO BISECT (2026-11-19, review of batch #48's bisect): a batch's failed check went straight to its
# bisect whatever failed it -- only the red gate was excepted -- while a lone change's infra-shaped failure is re-queued
# (queue.py INFRA_CHECK_FAILURE). A lost sbt server or runner under a batch cost one prefix check per member, then
# "every prefix passed (flaky or infra)" and a whole new round. Now the batch's own check runs once more first; a second
# infra-shaped failure bisects as before (a member may be what loses the server: its own check then judges it, alone).
# (#48 itself failed for real: the combined tree's coupling ratchet, `services.movies: distance D 40 past its frozen
# row 39` -- a test failure, bisected rightly.)
BATCH_INFRA_RETRIES = 1


def batch_infra_failure(out):
    """The infra signature of a failed batch check's output (an sbt server lost, a runner or network failure with no
    test line beside it: classify.py's INFRA_LOG), else None."""
    m = INFRA_CHECK_FAILURE.search(out or "")
    if m:
        return m.group(0)
    lines = (out or "").splitlines()
    hit = next((line for line in lines if INFRA_LOG.search(line)), None)
    return hit.strip()[:160] if hit and not _test_line(lines) else None


def culprit_own_failure(repo, d, e, base, out):
    """Whether a backlog batch's confirmed culprit failed by its own fault, as a change's own check judges it
    (judge_failure): no infra failure, no time cap, a failing test that main's red runs and the queue's record do not
    share."""
    if INFRA_CHECK_FAILURE.search(out or "") or CHECK_TIMEOUT.search(out or ""):
        return False
    return judge_failure(repo, d, e, base, out)[0] == "head"
