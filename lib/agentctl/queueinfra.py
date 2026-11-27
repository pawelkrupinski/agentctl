# agentctl, part `queueinfra`: requeue of a change after an infrastructure failure (a blip, not a verdict) and the probes of the conditions it opens
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import os
import re
from functools import partial
from typing import Any
from agentctl.statemachines import change_event
from agentctl.clock import epoch_now
from agentctl.timefmt import iso_at
from agentctl.queueknobs import knob_infra_same_max
from agentctl.core import err, host_role
from agentctl.repos import repo_of_waiting
from agentctl.networksignatures import credential_failure, redact_credentials
from agentctl.waitingdir import ChangeRecords, wlog
from agentctl.conditions import open_condition, run_probe
from agentctl.serverclone import probe_clone_health
from agentctl.queueslots import set_change
from agentctl.agewords import fmt_age


# a backlog batch (batch.py, THE BACKLOG BATCH) is up to BACKLOG_BATCH_MAX of them checked in ONE tree.
# 4, not 3 (2026-11-17): ci-runner's check runners were 3 in effect (a lane bug let borrowed checks hold the check
# lane: movies runner_scheduler.py), up to 2 lane + 1 CPU-admitted + 2 borrowed since; 4 is a first step, re-measure.
# A check the LAPTOP failed, not the change: sbt's server never came up or was lost, a script test cut off by its
# time bound, a stall. Queued again after INFRA_BACKOFF's delay, at most QUEUE_INFRA_RETRIES times, then judged as it is.
# THE CHECK'S ENVIRONMENT LACKED THE GITOPS MANIFESTS (2026-11-22, landing checks 44984605717 and 44998261508: the
# deploy specs failed "infra/kubernetes/... is missing because the GitOps manifests are no longer in this repository",
# the beside script tests having moved the fetched manifests aside; Identity takes #349's fix was REJECTED twice and the
# gate stayed red ~7 h). The manifests are another repository's, fetched by the check itself (infra/bin/fetch-gitops):
# their absence, or the fetch failing, is the check's environment, never the change. This is an INFRA RETRY of the
# check, NOT a rerun-to-green of a test: no test result is discarded, the check never ran the specs against their
# input. Re-queued like any infra failure (backed off, escalated from the third, a condition opened past
# QUEUE_INFRA_RETRIES), so a change that really breaks the fetch is still seen.
GITOPS_MISSING = (r"GitOps manifests are no longer in this repository|fetch-gitops could not fetch|"
                  r"GitOps manifests STALE")


INFRA_CHECK_FAILURE = re.compile(r"failed to connect to server|sbt server did not start within \d+ seconds|"
                                 r"sbt server disconnected|^TIMEOUT +\d+s|STALLED:|" + GITOPS_MISSING, re.M)


QUEUE_INFRA_RETRIES = 3


INFRA_BACKOFF = (300, 600, 1800, 3600)  # infra re-queues back off; from the third, each one escalates


def requeue_infra(st: Any, d: str, e: dict[str, Any], why: str, now: float, delay: float | None = None,
                  reason: str | None = None, detail: str | None = None, repo: str | None = None) -> Any:
    """Back to queued for a later look: infra NEVER rejects a change (design §5.1). The delay grows 5 -> 60 min; from
    QUEUE_INFRA_RETRIES on, each re-queue is an escalation (its event's severity). NEVER SILENTLY FOREVER (2026-10-29:
    a corrupt clone's fetch of main re-queued every change for hours, and nobody was told): re-queued for the same
    reason (reason: why without its details) QUEUE_INFRA_RETRIES times or for INFRA_SAME_MAX, it opens a condition
    with the exact error (detail), which its own probe clears."""
    n = e.get("infra_retries", 0) + 1
    wait = delay if delay is not None else INFRA_BACKOFF[min(n - 1, len(INFRA_BACKOFF) - 1)]
    sev = "escalate" if n >= QUEUE_INFRA_RETRIES else "info"
    t = now
    reason = reason or infra_reason_of(why)
    same = e.get("infra_reason") == reason
    n_same = int(e.get("infra_same") or 0) + 1 if same else 1
    since = float(e.get("infra_reason_since") or t) if same else t
    set_change(st, d, e, "queued", cause="check-failed-infra", data={"infra_attempts": n},
               event=change_event(e, f"QUEUED AGAIN {e['id']}: infra ({why}); checked again in {int(wait // 60)} min",
                                  code=None, final=False, severity=sev),
               infra_retries=n, retry_after=t + wait, why=f"infra: {why}", waiting_on=None,
               infra_reason=reason, infra_same=n_same, infra_reason_since=since)
    wlog(d, f"{e['id']}: {why}: queued again in {int(wait // 60)} min (infra retry {n}"
            + (", escalated" if sev == "escalate" else "") + ")")
    requeue_condition(st, d, e, why, reason, n_same, t - since, detail, repo)


def requeue_condition(st: Any, d: str, e: dict[str, Any], why: str, reason: str, n_same: int, age: float, detail: str | None,
                      repo: str | None) -> None:
    """The condition a re-queue opens, if any. A push credential failure: ONE for the clone, at once (no retry brings it
    back). Any other reason: the infra one, once the same reason repeated QUEUE_INFRA_RETRIES times or for
    INFRA_SAME_MAX."""
    try:
        if credential_failure(why) and push_credential_condition(st, d, why, detail, repo):
            return
        if n_same >= QUEUE_INFRA_RETRIES or age >= knob_infra_same_max():
            infra_requeue_condition(st, d, e, why, reason, n_same, age, detail, repo)
    except (Exception, SystemExit) as x:
        err(f"{e['id']}: re-queued {n_same} times for {reason}, and its condition could not be opened ({str(x)[:160]})")


STALLED_REASON = "its checks stalled"


STALL_KEEP_PLACE = 2  # stalls in a row re-queued with no backoff (the change keeps its age-order place), then backed off


FETCH_MAIN_FAILED = "its fetch of main failed"


def infra_reason_of(why: str) -> str:
    """An infra re-queue's reason without its details (what differs between two re-queues for the same cause)."""
    return re.sub(r"\s*\(.*?\)", "", why).split(":")[0].strip()


def infra_requeue_condition(st: Any, d: str, e: dict[str, Any], why: str, reason: str, n: int, age: float, detail: str | None,
                            repo: str | None) -> Any:
    """The condition of a change re-queued n times for the same infra reason: main's fetch failing is the clone's
    (one per clone, cleared when it verifies and fetches main again); any other reason the change's own (cleared when
    it gets past it)."""
    text = (f"STALLED: {e['id']} re-queued {n} times in {fmt_age(age)} for the same infra reason ({reason}); it lands "
            f"only once that clears. Last: {why}")
    tail = (detail or why).strip().splitlines()[-15:]
    if reason == FETCH_MAIN_FAILED:
        repo = repo or repo_of_waiting(d)
        return open_condition("STALLED", f"{text}. Every change's check fetches main: the landing is blocked",
                              run_probe(probe_clone_health, {"repo": repo}), [],
                              key=f"main-fetch-{os.path.basename(os.path.abspath(repo))}", command="land", tail=tail, store=st)
    return open_condition("STALLED", text, run_probe(partial(probe_infra_requeue, st=st), {"d": d, "eid": e["id"], "reason": reason}), [],
                          key=f"infra-requeue-{e['id']}", command="land", cwd=e.get("worktree"), tail=tail, store=st)


def push_credential_condition(st: Any, d: str, why: str, detail: str | None, repo: str | None) -> Any:
    """GitHub never got, or refused, this clone's push credential: ONE condition per clone, opened at the first such
    re-queue and naming every change it holds, cleared by the next push that succeeds (probe_push_credential) -- not an
    "infra failure" retried on a timer per change (2026-11-22: four changes held for hours by a dead helper)."""
    repo = repo or repo_of_waiting(d)
    held = credential_held(ChangeRecords(st, d).every())
    text = (f"PUSH CREDENTIAL: GitHub refused {host_role()}'s push credential for {os.path.basename(repo)}; "
            f"{len(held)} change(s) wait on it ({', '.join(held[:6])}). Not the vault's and not the changes': "
            f"Its credential helper there must work again (the App helper on ci-runner, gh's login on the laptop). "
            f"Cleared by the next check-branch push that succeeds. Last: {redact_credentials(why)}")
    tail = redact_credentials(detail or why).strip().splitlines()[-15:]
    probe = run_probe(partial(probe_push_credential, st=st), {"d": d, "since": epoch_now(), "host": host_role()})
    return open_condition("STALLED", text, probe, [], key=f"push-credential-{os.path.basename(os.path.abspath(repo))}",
                          command="land", tail=tail, grace=0, store=st)


def credential_held(entries: list[dict[str, Any]]) -> list[str]:
    """The ids of the changes waiting on a push credential: queued or checking, re-queued for a credential failure."""
    return [str(x.get("id")) for x in entries if x.get("status") in ("queued", "checking")
            and credential_failure(x.get("why"))]


def probe_push_credential(a: dict[str, Any], st: Any) -> tuple[bool | None, str]:
    """A check-branch push of the clone's queue succeeded since the condition opened (save_remote's pushed_at), or no
    change waits on a credential any more. Read from the queue's own records ONLY: never a credential helper (the
    GitHub App's mints a token per call, 2026-11-22 review), and only on the host whose push failed (a["host"]):
    another host's clone and helper say nothing about it, so there it cannot tell."""
    if a.get("host") != host_role():
        return None, f"read on {a.get('host')} only, where the push failed"
    entries = ChangeRecords(st, a["d"]).every()
    pushed = max((float((x.get("remote") or {}).get("pushed_at") or 0) for x in entries), default=0.0)
    if pushed > float(a.get("since") or 0):
        return True, f"a check-branch push succeeded at {iso_at(pushed)}"
    held = credential_held(entries)
    return not held, f"{len(held)} change(s) wait on it" if held else "no change waits on it"


def probe_infra_requeue(a: dict[str, Any], st: Any) -> tuple[bool, str]:
    """Change a['eid'] got past a['reason']: decided, checked, gone, or re-queued for another reason."""
    e = ChangeRecords(st, a["d"]).get(a["eid"]) or {}
    stuck = e.get("status") in ("queued", "checking") and e.get("infra_reason") == a.get("reason")
    return not stuck, f"entry {a['eid']}: {e.get('status') or 'gone'}" + (f" ({e.get('why')})" if stuck else "")
