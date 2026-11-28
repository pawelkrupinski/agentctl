# agentctl, part `queuecheck`: a change's check round's pieces: main fetched, its commits, the run adopted, the rebase, the refusals, the mode, the failure judged
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import time
from typing import Any
from agentctl.statemachines import change_event
from agentctl.clock import Clock, epoch_now
from agentctl.timefmt import ts
from agentctl.core import git, remote
from agentctl.repos import precheck_hook
from agentctl.clones import fetch_main, note_clone_suspect
from agentctl.policy import policy_blocks
from agentctl.generated import generated_entry
from agentctl.networksignatures import network_failure
from agentctl.classify import network_only
from agentctl.waitingdir import ChangeRecords, wlog
from agentctl.context import Context
from agentctl.queuefailure import CHECK_TIMEOUT, TIMEOUT_RETRIES, judge_failure, keys_text, on_gate_run
from agentctl.conflicts import CONFLICT_PLACE_KEPT, conflict_cause, conflict_paths
from agentctl.queueslots import check_tree, queue_verdict, set_change
from agentctl.queue import VERIFY_MODE, module_overlap, range_paths, verify_areas_supported, verify_supported
from agentctl.queuerecheck import recheck_reason
from agentctl.redland import fix_replay_refusal
from agentctl.waiting import already_landed, cancel_entry_remote
from agentctl.blamerecords import blame_land_refusal
from agentctl.queuediff import rebase_in_tree
from agentctl.queueinfra import (
    FETCH_MAIN_FAILED, INFRA_CHECK_FAILURE, STALLED_REASON, STALL_KEEP_PLACE, requeue_infra)
from agentctl.gitrepo import commit_known, is_ancestor


def regen_commands(tree: Any, files: Any) -> Any:
    """The regen commands of the generated files the check tree kept main's rows of."""
    return sorted({" ".join(g["regen"]) for g in (generated_entry(tree, f) for f in files) if g and g.get("regen")})


def provisional_hint(tree, e):
    """`; its F kept main's rows ...: rebase, run `regen`, land again` for a change whose generated files the check
    tree merged keeping main's side of a row both changed (MAIN'S ROW, PROVISIONALLY, waiting.py); '' otherwise."""
    files = e.get("provisional") or []
    cmds = regen_commands(tree, files)
    return (f"; its {', '.join(files)} kept {remote()}/main's rows where both sides changed them (no regen command "
            f"runs where the queue does): rebase onto {remote()}/main, run {' and '.join(f'`{c}`' for c in cmds) or 'the regen command'}, "
            "commit and land again") if files else ""


# A ROW ONLY A REGEN DECIDES IS NO BOUNCE (2026-11-19, lane F2b): main re-measured the coupling-baseline rows the change
# changed too; ci-runner runs no JVM, so the check tree kept main's rows (MAIN'S ROW, PROVISIONALLY, waiting.py) and the
# checks' ratchet failed over them. That is no defect of the change, only a regeneration this host cannot run: the
# change is HELD in its place (queued, not checked again) and its agent told ONE command -- `agentctl land` in its
# worktree, which rebases onto main and runs the regen where the JVM is (the laptop's conflict_resolve) -- and the
# change queued again from that worktree takes the held entry's place and queue age (changesubmit.py queue_change). A hold
# nobody answers within CONFLICT_PLACE_KEPT is NEVER rejected (the owner, 2026-11-20: "2 drop the reject"): it RECOVERS
# (regen_hold_overdue, below).
def regen_hold(st, d, e, tree, failing, out):
    """Hold change e for its agent's regeneration (above): queued with retry_after at the hold's end, `regen_hold` on
    its entry, its author told the one command (the event's inbox entry). Returns 1 (nothing pushed)."""
    files = e.get("provisional") or []
    cmds = regen_commands(tree, files)
    cmd = f"cd {e['worktree']} && ~/bin/agentctl land"
    t = epoch_now()
    why = (f"REGEN NEEDED {e['id']}: its {', '.join(files)} kept {remote()}/main's rows where both sides changed them "
           f"(ci-runner runs no {' / '.join(cmds) or 'regen command'}) and its checks failed over them ({failing}); it "
           f"keeps its place in the queue: run `{cmd}` (rebases onto {remote()}/main and runs "
           f"{' and '.join(f'`{c}`' for c in cmds) or 'the regen'} on this machine)")
    set_change(st, d, e, "queued", cause="regen-hold", event=change_event(e, why, (out or "").splitlines()[-40:], code=None, final=False),
               why=why, retry_after=t + CONFLICT_PLACE_KEPT,
               regen_hold={"since": t, "cmd": cmd, "files": files, "head": e.get("head")})
    wlog(d, f"{e['id']}: held for its regeneration ({', '.join(files)}): `{cmd}`")
    return 1


def round_ended(st: Any, d: str, e: dict[str, Any], tree: Any, base: str, head: Any, via: Any,
                conflicts: list[Any]) -> int | None:
    """The round over before its checks: the change landed already (0), or no longer applies on main (rejected, 1);
    None when it applies."""
    if via:
        already_landed(st, d, e, via if via != "an equivalent commit" else None)
        return 0
    if not head:
        # policy: no-longer-applies
        queue_verdict(st, d, e, "rejected", f"NOT LANDED: the change no longer applies on {remote()}/main {base[:10]} "
                      f"(main moved since QUEUED{conflict_cause(tree, e.get('base'), base, conflicts)}): rebase onto "
                      f"{remote()}/main and land again (it keeps its place in the queue)",
                      conflict_files=conflict_paths(conflicts), decided_on=base)
        return 1
    return None


def fetched_main(st: Any, repo: str, d: str, e: dict[str, Any], clock: Clock) -> str | None:
    """origin/main's sha after a fetch; None when it failed (the change requeued: never checked on a stale main)."""
    fetched, ferr = fetch_main(repo)  # a corruption signature marks the clone for repair (clone_heal_tick)
    if fetched:
        return git(repo, "rev-parse", f"{remote()}/main").strip()
    last = (ferr.strip().splitlines() or ["?"])[-1][:240]  # git's own words say why (2026-10-29: "Could not read 409b8bb...")
    requeue_infra(st, d, e, f"{FETCH_MAIN_FAILED}: {last}", clock.time(), reason=FETCH_MAIN_FAILED, detail=ferr,
                  repo=repo)
    return None


def commits_here(st: Any, repo: str, d: str, e: dict[str, Any], clock: Clock) -> bool:
    """The change's commits are in the clone (phase 2: on the remote as land/<id>, fetched here); else it is requeued."""
    if commit_known(repo, e['head']):
        return True
    lr = git(repo, "fetch", "--quiet", remote(), f"refs/heads/land/{e['id']}", check=False)
    if commit_known(repo, e['head']):
        return True
    lerr = (lr.stderr or "").strip()
    note_clone_suspect(repo, lerr)
    requeue_infra(st, d, e, f"its commits are not here and refs/heads/land/{e['id']} gave none"
                  + (f": {lerr.splitlines()[-1][:240]}" if lerr else ""), clock.time(), detail=lerr, repo=repo)
    return False


def adopted_run(repo: str, e: dict[str, Any], rem: dict[str, Any], base: str) -> bool:
    """ON ITS OWN BASE (2026-11-15 22:42Z: the reconciler's restart on a new pin killed its detached checkers; their
    replacements, after main had moved, cancelled one half-done run and threw a green one away -- this adoption once
    demanded the CURRENT main, from before the push learned to judge a moved main, e6a5b93). The run is adopted on
    the base it checked, while main still holds that base; the push then judges what landed since, exactly as for a
    checker that had survived: re-checked when it overlaps, pushed without re-check when it does not. A replacement
    checker (the last one lost its lease, died or re-exec'd mid-wait) goes on with the same tip, its run."""
    return bool(rem.get("head") == e["head"] and rem.get("base") and not rem.get("collected_at")
                and rem.get("status") not in ("cancelled", "skipped", "abandoned")
                and commit_known(repo, rem.get('tip'))
                and is_ancestor(repo, rem["base"], base))


def round_head(st: Any, d: str, e: dict[str, Any], repo: str, i: int, base: str, rem: dict[str, Any],
               adopt: bool) -> tuple[Any, Any, Any, str, list[Any]]:
    """(check tree, the change's head on it, what it landed as already (via), the base, the rebase's conflicts): the
    adopted run's tip and base, or a new round: the change rebased onto main in a check tree."""
    if adopt:
        return check_tree(repo, i, rem["tip"]), rem["tip"], None, rem["base"], []
    if rem.get("run") is not None and not rem.get("status"):
        cancel_entry_remote(st, e, "superseded by a new round of its checks")
    e.pop("remote", None)
    e.pop("remote_run", None)
    e["round_since"] = ts()
    ChangeRecords(st, d).save(e)
    tree = check_tree(repo, i, base)
    conflicts: list[Any] = []
    head, via = rebase_in_tree(st, d, tree, e, base, conflicts=conflicts)
    return tree, head, via, base, conflicts


def tip_refused(st: Any, repo: str, d: str, e: dict[str, Any], tree: Any, commits: Any, base: str,
                said: dict[Any, Any]) -> bool:
    """The change rejected for its blame (queued before it was withdrawn or refuted: 2026-11-01, revert-629 re-queued
    by orphan duty) or an unreplayed fix (redreplay.py: the laptop's land may hold no token; its replays ride with the
    submission, `replays`)."""
    dead = blame_land_refusal(tree, commits, st, e.get("worktree"))
    if dead:
        # policy: blame-dead
        queue_verdict(st, d, e, "rejected", f"NOT LANDED: {dead}", decided_on=base)
        return True
    unreplayed = fix_replay_refusal(repo, commits, st, proofs=e.get("replays"))
    if unreplayed and unreplayed[0] == "fix-replay" and policy_blocks("fix-replay", None, e["id"], unreplayed[1][:160], said=said):
        queue_verdict(st, d, e, "rejected", f"NOT LANDED: {unreplayed[1]}", decided_on=base)  # policy: fix-replay
        return True
    if unreplayed and unreplayed[0] == "fix-replay-outside" \
            and policy_blocks("fix-replay-outside", None, e["id"], unreplayed[1][:160], said=said):
        queue_verdict(st, d, e, "rejected", f"NOT LANDED: {unreplayed[1]}", decided_on=base)  # policy: fix-replay-outside
        return True
    return False


def check_mode(d: str, e: dict[str, Any], tree: Any, base: str, head: str, rem: dict[str, Any], joining: bool,
               hook: Any) -> str:
    """The hook mode the round runs: its own (--checks-only joining a red, else --range), or -- an adopted verify run's,
    or when THE RULE (recheck_reason) again, on the main this round checks on, lets the full check stand -- the verify
    mode, with the sbt modules both sides touch (verify_areas)."""
    mode = "--checks-only" if joining else "--range"
    if rem and rem.get("mode") == VERIFY_MODE:
        mode = VERIFY_MODE
    elif e.get("verify") and hook:
        mode = verify_mode_or_full(d, e, tree, base, head) or mode
    if mode != VERIFY_MODE:
        e.pop("verify_areas", None)
    return mode


def verify_mode_or_full(d: str, e: dict[str, Any], tree: Any, base: str, head: str) -> str | None:
    """VERIFY_MODE when the full check of an earlier base stands (and the hook can verify), its areas noted; else None,
    the reason logged: fully checked, not only verified."""
    full_base = e.get("full_base") or e.get("checked_base") or base
    _n, why = recheck_reason(tree, full_base, base, head, range_paths(tree, base, head), full_tip=e.get("full_tip"))
    if why is None and verify_supported(precheck_hook(tree)):
        areas = module_overlap(range_paths(tree, full_base, base), range_paths(tree, base, head)) \
            if verify_areas_supported(tree) else []
        e["verify_areas"] = areas or None
        if areas:
            wlog(d, f"{e['id']}: sbt modules both sides touch ({', '.join(areas)}): verified with the area tests")
        return VERIFY_MODE
    wlog(d, f"{e['id']}: {why or f'the hook has no {VERIFY_MODE}'}: fully checked, not only verified")
    return None


def requeued_for_infra(st: Any, repo: str, d: str, e: dict[str, Any], ok: Any, out: str, where: str,
                       clock: Clock) -> bool:
    """The checks' outcome says the laptop failed, not the change (stalled; infra, network, time cap): the change
    requeued, True."""
    if ok is None:
        requeue_stalled(st, repo, d, e, out, where, clock)
        return True
    return not ok and requeued_for_laptop_failure(st, repo, d, e, out, where, clock)


def requeue_stalled(st: Any, repo: str, d: str, e: dict[str, Any], out: str, where: str, clock: Clock) -> None:
    """A stall: the laptop's, not the change's -- and what stalled it is said (2026-11-17: two 13-s "stalls" whose
    cause no record kept). ITS PLACE KEPT (2026-11-17, ref-srp-54ebae 2 h old: a 13-s stall's 5-min backoff let
    younger changes take every check tree, and it waited 15+ min more for one): the first STALL_KEEP_PLACE stalls in a
    row are due at once -- the tree it just freed goes to it, first by age; then the infra backoff as for any infra
    cause."""
    last = ((out or "").strip().splitlines() or [""])[-1].strip()[:240]
    same = int(e.get("infra_same") or 0) if e.get("infra_reason") == STALLED_REASON else 0
    requeue_infra(st, d, e, f"its checks stalled ({where}{': ' + last if last else ''})", clock.time(),
                  delay=0 if same < STALL_KEEP_PLACE else None, reason=STALLED_REASON, detail=out, repo=repo)


def requeued_for_laptop_failure(st: Any, repo: str, d: str, e: dict[str, Any], out: str, where: str,
                                clock: Clock) -> bool:
    """Failed checks that say the laptop failed (infra signature, network only, past the time cap with retries left):
    the change requeued, True."""
    if INFRA_CHECK_FAILURE.search(out or ""):
        requeue_infra(st, d, e, f"its checks failed on the laptop, not on the change "
                         f"({INFRA_CHECK_FAILURE.search(out).group(0)})", clock.time())
    elif network_only(out):  # no verdict, on its first round too (judge_failure: the change's)
        requeue_infra(st, d, e, f"{NETWORK_REASON} ({network_failure(out)})", clock.time(), reason=NETWORK_REASON,
                      detail=out, repo=repo)
    elif CHECK_TIMEOUT.search(out or "") and int(e.get("timeout_retries") or 0) < TIMEOUT_RETRIES:
        e["timeout_retries"] = int(e.get("timeout_retries") or 0) + 1
        requeue_infra(st, d, e, f"its checks ran past their time cap ({where}; retry {e['timeout_retries']} of "
                            f"{TIMEOUT_RETRIES})", clock.time(), reason="its checks ran past their time cap", detail=out,
                      repo=repo)
    else:
        return False
    return True


def judge_check_failure(st: Any, repo: str, d: str, e: dict[str, Any], tree: Any, base: str, out: str,
                        where: str, now: float, *, ctx: Context) -> tuple[int | None, str, Any]:
    """Failed checks judged: the change's own failure rejects it (1; a provisional one waits for its regen), one only
    as its base fails too waits behind that red (known_red)."""
    side, what = judge_failure(st, repo, d, e, base, out, now, own_paths(tree, base, e), ctx=ctx)
    timed_out = bool(CHECK_TIMEOUT.search(out or ""))
    if side == "head" and e.get("provisional") and not timed_out:
        return regen_hold(st, d, e, tree, keys_text(what), out), where, None  # A ROW ONLY A REGEN DECIDES IS NO BOUNCE
    if side == "head":
        timed = f" (and they ran past their time cap, {TIMEOUT_RETRIES + 1} times)" if timed_out else ""
        # policy: checks-failed
        queue_verdict(st, d, e, "rejected", f"NOT LANDED: the pre-push checks failed ({where}){timed}; failing "
                      f"with this change only: {keys_text(what)}{provisional_hint(tree, e)}; the change "
                      f"{e['head'][:10]} stays in {e['worktree']}", (out or "").splitlines(), decided_on=base)
        return 1, where, None
    if side == "unrun":
        reject_unrun(st, d, e, base, out, where, what)
        return 1, where, None
    srcs = sorted(set(what.values()))
    wlog(d, f"{e['id']}: its checks failed only where its base fails too ({keys_text(what)}; {'; '.join(srcs)})"
            ": it waits behind that red")
    return None, where, {"keys": sorted(what), "where": srcs, "base": base,
                         "gate_only": all(on_gate_run(w) for w in what.values())}


def own_paths(tree: Any, base: str, e: dict[str, Any]) -> set[str] | None:
    """The paths the change's own commits touch (its head against its merge base with the main checked on), None
    when unread."""
    r = git(tree, "diff", "--name-only", "--no-renames", f"{base}...{e.get('head')}", check=False)
    return {p for p in r.stdout.splitlines() if p} if r.returncode == 0 else None


def reject_unrun(st: Any, d: str, e: dict[str, Any], base: str, out: str, where: str, what: list[str]) -> None:
    """A TEST ITS BASE NEVER RAN IS NOBODY'S YET (queuefailure.py): rejected, said to be unattributed."""
    # policy: checks-failed
    queue_verdict(st, d, e, "rejected", f"NOT LANDED: the pre-push checks failed ({where}) in tests NOT RUN ON ITS "
                  f"BASE -- unattributed, not shown to be this change's: {keys_text(what)} (neither in its diff nor "
                  f"selected on {base[:10]}; main's own tree may fail them: run them on origin/main, e.g. "
                  f"`scripts/run-script-tests {' '.join(w.split(' ')[0] for w in what[:3])}`, before changing "
                  f"anything); the change {e['head'][:10]} stays in {e['worktree']}", (out or "").splitlines(),
                  decided_on=base)


NETWORK_REASON = "its checks could not reach the network"
