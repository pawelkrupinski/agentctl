# agentctl, part `queuesubmit`: submitting a change: by ref, without the vault, the queued line, the fast precheck, a red fix's land.
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from agentctl.selfcode import TOOL_PATH
from agentctl.statemachines import MACHINES, StaleVersion, machine_state
from agentctl.clock import epoch_now
from agentctl.ids import entry_id_age
from agentctl.queueknobs import (
    knob_fixer_land_wait, knob_land_ref_sweep, knob_land_ref_unknown_age, knob_note_precheck_wait)
from agentctl.core import err, git, git_ok, remote, scratch, spawn_detached
from agentctl.networksignatures import credential_failure, is_transient_network, redact_credentials
from agentctl.netretry import TransientNetwork, net_retry
from agentctl.waitingdir import ChangeRecords, entry_final
from agentctl.owned import backoff
from agentctl.store import StoreUnavailable
from agentctl.land import push_bounds, run_watched, tool_env
from agentctl.waiting import tail_of
from agentctl.agewords import fmt_age
import os
import subprocess
import sys
import time
from typing import Any, Optional


_last_ref_sweep = [0.0]


def land_refs_supported(repo):
    """The repository's hook at origin/main checks nothing on a land/ ref (else the push would run its full checks)."""
    hook = git(repo, "show", f"{remote()}/main:scripts/hooks/pre-push", check=False).stdout
    return "refs/heads/land/" in hook


def push_failure_why(text: object) -> str:
    """What a failed push of land/<id> was, for land's NOT LANDED: git's own words and whose failure they are -- never a
    pointer at the vault (2026-11-22, Tool self-tests #480: a DNS failure's NOT LANDED said `agentctl env --check`,
    which said the movies vault was locked, and the fixer asked the owner for an unlock nothing needed)."""
    said = " ".join(redact_credentials(text).split()) or "no output"
    if credential_failure(said):
        whose = ("GitHub refused this machine's credential (gh's login: `gh auth status`), never the vault's or the "
                 "change's")
    elif is_transient_network(said):
        whose = "the network's, never the vault's or the change's"
    else:
        whose = "not the vault's: no push reads it"
    return f"git said `{said[:240]}` -- {whose}"


def _ref_sweep_due(t: float) -> bool:
    """Whether a sweep is due (marked done by this call)."""
    if t - _last_ref_sweep[0] < knob_land_ref_sweep():
        return False
    _last_ref_sweep[0] = t
    return True


def _land_ref_unneeded(eid: str, every: Any, items: Any, t: float) -> Optional[str]:
    """None when land/<eid> must stay (a change either source holds open, or an unknown one not yet old enough); else
    the note on its deletion ("" when its change is decided)."""
    e, it = every.get(eid), items.get(eid)
    if e and not entry_final(e.get("status")):
        return None
    if it and not machine_state("change", it["state"]).terminal:
        return None
    if e or it:
        return ""
    age = entry_id_age(eid, t)
    if age is None or age < knob_land_ref_unknown_age():
        return None
    return f": unknown to entries and store, {int(age // 3600)} h old"


def _change_items(store: Any) -> Any:
    """{id: item} of the store's changes; None (said) when the store is unreachable: decide nothing this tick."""
    try:
        return {it["id"]: it for it in store.items("change")}
    except Exception as ex:
        err(f"land refs: store unreadable ({ex}); no ref deleted")
        return None


def _delete_land_ref(repo: Any, ref: str, note: str) -> list[str]:
    """Delete the remote ref: the lines it did (its note, when it has one, then the deletion's)."""
    dr = git(repo, "push", "--quiet", remote(), f":{ref}", check=False)
    return ([ref + note] if note else []) + [f"{ref}: deleted" if dr.returncode == 0 else f"{ref}: could not delete"]


def sweep_land_refs(repo, d, store, now_=None):
    """Every LAND_REF_SWEEP_EVERY s: each remote land/<entry> ref whose change is decided, deleted. Since phase 2 the
    changes live in ci-runner's STORE, not in entries/ (2026-11-04: every queued change looked unknown and lost its
    ref at the next sweep): a change either source holds open keeps its ref; one NEITHER knows is deleted only past
    LAND_REF_UNKNOWN_AGE, and logged."""
    t = now_ if now_ is not None else epoch_now()
    if not _ref_sweep_due(t):
        return []
    r = git(repo, "ls-remote", "--heads", remote(), "refs/heads/land/*", check=False)
    if r.returncode != 0:
        return []
    every = {e["id"]: e for e in ChangeRecords(store, d).every()}
    items = _change_items(store)
    if items is None:
        return []
    did = []
    for line in r.stdout.splitlines():
        ref = line.split()[-1]
        note = _land_ref_unneeded(ref[len("refs/heads/land/"):], every, items, t)
        if note is not None:
            did += _delete_land_ref(repo, ref, note)
    return did


# THE LAPTOP'S LAND NEEDS NO VAULT (2026-10-31: agents' `land` still died NoToken -- a `Fixes-Red:` trailer made
# change_fields read the reds from ci-runner's store, the token read failed in a session whose vault was locked, and
# fix_check let the NoToken (a SystemExit) through; the 10-30 fix had only let agents run `secrets` themselves, which
# prompts). The submission's git half is the land/<id> push over ssh; its store half takes a token from the
# environment or the processor's socket, never `secrets` (no_vault). With none, the change waits in the outbox and the
# laptop's sync -- which holds the token -- submits it; the reconciler checks the Fixes-Red trailers again at the push.
# THE QUEUED LINE NAMES NO VERDICT IT HAS NOT REACHED (2026-11-16: its old tail, "LANDED / WAITING / NOT LANDED goes to
# `agentctl inbox`", read as LANDED to a watcher matching the word): QUEUED is its only verdict word, and it says what
# the agent does next -- end its turn, or (a red fixer's land) nothing: the land waits for the verdict itself.
def queued_tail(waits=False):
    """The end of land's QUEUED / QUEUED-LOCAL line."""
    return ("not on main yet: this land now waits for its verdict" if waits
            else "not on main yet: END YOUR TURN; its verdict comes to `agentctl inbox`")


# THE FAST PRECHECK (review 2026-11-06 P1; the owner, 11-06: "landing must not take significantly longer"): a repository
# with an executable scripts/land-precheck (movies: the rebase dry run, the hook's commit-rule and linter checks, the
# registry's lints, ~20-60 s warm) has it run ALONGSIDE the queue, never before it: land prints QUEUED as fast as ever,
# and a refusal (exit 1) cancels the queued change and puts NOT LANDED in the inbox, minutes before the landing check
# would have, sparing that check. Exit 0 or 2 (could not check) leaves the change to the landing check.
# `land --no-fast-precheck` starts none.
FAST_PRECHECK = "scripts/land-precheck"
FAST_PRECHECK_BOUND = 900


def fast_precheck_spawn(argv, **kw):
    return spawn_detached(argv, **kw)


def start_fast_precheck(repo, main_ref, head, eid, a):
    """Start the precheck of queued change `eid` beside the queue; prints one line saying so."""
    if getattr(a, "no_fast_precheck", False) or not os.access(os.path.join(repo, FAST_PRECHECK), os.X_OK):
        return
    base = git(repo, "rev-parse", main_ref).strip()
    log = os.path.join(scratch(), f"agentctl-precheck-{eid}.log")
    try:
        with open(log, "w") as logf:
            fast_precheck_spawn([sys.executable, TOOL_PATH, "land-precheck", eid, "--repo", repo, "--base", base,
                                 "--head", head], cwd=repo, stdin=subprocess.DEVNULL, stdout=logf,
                                stderr=subprocess.STDOUT, start_new_session=True)
    except OSError as e:
        err(f"fast precheck not started ({e}); the landing check judges the change")
        return
    print(f"fast precheck of {eid} running alongside the queue ({FAST_PRECHECK}; log {log}): a refusal cancels it "
          "with NOT LANDED in `agentctl inbox`", flush=True)


def precheck_outcome(rc, out):
    """(outcome, the refusing step) of a fast precheck: passed (exit 0) / refused (1) / unchecked (2: a step could not
    run, or ended at its bound -- never `passed`), its `PRECHECK ...` line."""
    lines = (out or "").splitlines()
    last = next((l for l in reversed(lines) if l.startswith("PRECHECK ")), None)
    outcome = "refused" if rc == 1 else "passed" if rc == 0 else "unchecked"
    return outcome, (last or "")[:200] if outcome == "refused" else None


# THE PRECHECK IS NOTED THROUGH A BLIP (2026-11-17: the first live landing records carried no precheck_s -- deploy-nix's
# note died on one `POST /v1/store/item: timed out`, and a change submitted through the outbox has no item yet when its
# 60-s precheck ends): an unreachable store or a missing item is tried again, NOTE_PRECHECK_DELAYS apart.
NOTE_PRECHECK_DELAYS = (5, 15, 30, 60)


def _note_precheck_once(store: Any, eid: Any, rec: Any) -> Any:
    """(True, "") when `rec` was merged onto change eid's item, else (False, why not)."""
    try:
        for _ in range(3):  # a compare-and-set: the reconciler may move the item meanwhile
            it = store.item("change", eid)
            if not it:
                return False, "its item does not exist"
            try:
                store.update_item("change", eid, it["version"], rec)
                return True, ""
            except StaleVersion:
                why = "its item kept moving"
    except (Exception, SystemExit) as e:
        why = str(e)[:120]
    return False, why


def note_precheck(eid, secs, rc, out, store, sleep=time.sleep):  # wall: waits on and times real processes and threads
    """The precheck's duration and outcome on its change item (`precheck`: the landing record's precheck_* fields),
    before a refusal cancels it; never fatal."""
    outcome, step = precheck_outcome(rc, out)
    rec = {"precheck": {"duration_s": round(secs, 1), "outcome": outcome, **({"step": step} if step else {})}}
    cap, delays = knob_note_precheck_wait(), []
    for d in NOTE_PRECHECK_DELAYS:
        if sum(delays) + d <= cap:
            delays.append(d)
    for delay in (*delays, None):
        done, why = _note_precheck_once(store, eid, rec)
        if done:
            return True
        if delay is None:
            break
        sleep(delay)
    if why != "its item does not exist":  # no item: a change the store never held (a fixer's own land) -- nothing to say
        err(f"the precheck of {eid} could not be noted on its item ({why})")
    return False


FIXER_LAND_POLL = (15, 60)  # s: the await's backoff, first and cap


def change_decided(state):
    """Whether `state` is a terminal state of the change machine (an awaiter's `unread` or a store blip is not)."""
    return state in MACHINES["change"] and MACHINES["change"][state].terminal


def landed_sha_of(eid, data):
    return data.get("tip") or next(iter(data.get("landed_commits") or {}), "") or data.get("head")


def _change_or_blip(store: Any, eid: Any) -> Any:
    """(the change's item, None), or (None, why) when the store did not answer."""
    try:
        return store.item("change", eid), None
    except StoreUnavailable as e:
        return None, f"the store did not answer ({str(e)[:80]})"


def await_verdict(eid, bound, clock, store):
    """Block until queued change eid is decided (its store item; through a store blip, asked again), at most `bound` s:
    (state, its item's data, seconds waited); state `landed`, another terminal state, or the last open one read."""
    t0, delays = clock.time(), backoff(*FIXER_LAND_POLL)
    state, data = "unread", {}
    while True:
        it, blip = _change_or_blip(store, eid)
        state = blip or state
        if it:
            state, data = it["state"], it.get("data") or {}
            if machine_state("change", state).terminal:
                take_delivery(it, store)
                return state, data, clock.time() - t0
        if clock.time() - t0 >= bound:
            return state, data, clock.time() - t0
        clock.sleep(next(delays))


def take_delivery(it, store):
    """Record on change item `it` that a waiting call received its verdict (`delivered_at`): a later land never
    replays it as undelivered (verdict_still_holds). Best effort: a store blip or a racing writer only leaves it
    unrecorded, and the next land then checks the change again or replays it, both of them true."""
    if (it.get("data") or {}).get("delivered_at") or it.get("version") is None:  # said already, or a queue entry
        return  # the tick has not made an item of yet (own_change_item): nothing to record it on
    try:
        store.update_item("change", it["id"], it["version"], {"delivered_at": epoch_now()})
    except (StaleVersion, StoreUnavailable):
        pass


def verdict_still_holds(repo, main_ref, it):
    """Whether rejected change item `it` may be replayed to a new `land --until-done` instead of being checked again
    (A VERDICT IS DURABLE, below): nobody has taken delivery of it yet (take_delivery) AND main is still the main it
    was decided on (`decided_on`). Main moved, the verdict said already, or no base recorded: it no longer holds."""
    data = it.get("data") or {}
    if data.get("delivered_at") or not data.get("decided_on"):
        return False
    git(repo, "fetch", "--quiet", remote(), "main", check=False)
    return git_ok(repo, "rev-parse", "--verify", "-q", main_ref) \
        and git(repo, "rev-parse", main_ref).strip() == data["decided_on"]


def await_queued(eid, clock, store):
    """Block until queued change eid is decided, bounded by FIXER_LAND_WAIT: LANDED <sha> (0), NOT LANDED: <why> (1),
    or NOT LANDED (still queued) (1)."""
    state, data, waited = await_verdict(eid, knob_fixer_land_wait(), clock, store)
    if state == "landed":
        print(f"LANDED {landed_sha_of(eid, data)} (queued {eid})", flush=True)
        return 0
    if change_decided(state):
        why = str(data.get("why") or f"{eid} {state}")
        print(why if why.startswith("NOT LANDED") else f"NOT LANDED: {why}", flush=True)
        return 1
    print(f"NOT LANDED (still queued): {eid} is {state} after {fmt_age(waited)}; it stays queued as "
          "its red's fix (the red machine verifies it once it lands) -- `agentctl land status`", flush=True)
    return 1


def submit_ref(repo, d, e):
    """Push the change to refs/heads/land/<entry> (bounded); e records it (`ref`, else `push_error`), its caller saves
    it. Never fatal in phase 1 (the queue does not need it); in phase 2 (ci-runner fetches the change from it) the
    caller refuses without it."""
    if not land_refs_supported(repo):
        e["push_error"] = f"not pushed: {remote()}/main's scripts/hooks/pre-push takes no refs/heads/land/ ref"
        return None
    ref = f"refs/heads/land/{e['id']}"

    def push():  # the same sha to the change's own ref: idempotent, so tried again through a blip (net_retry)
        rc_, out_ = run_watched(["git", "push", "--quiet", remote(), f"{e['head']}:{ref}"], repo, tool_env(),
                                os.path.join(d, f"submit-{e['id']}"), "submit", **push_bounds())
        if rc_ != 0 and is_transient_network(out_) and not credential_failure(out_):
            raise RuntimeError(tail_of(out_ or "", 2)[:200])
        return rc_, out_
    try:
        rc, out = net_retry(f"submit: the push of {ref}", push)
    except TransientNetwork as ex:
        rc, out = 1, str(ex)
    if rc == 0:
        e["ref"] = ref
        return ref
    e["push_error"] = tail_of(out or "", 2).strip()[:300]
    err(f"submit: could not push {ref} ({e['push_error'][:160]}); the change is queued all the same")
    return None


SUBMIT_FIELDS = ("worktree", "branch", "head", "commits", "fixes", "keep_worktree", "claude_pid", "session", "base", "host",
                 "lander", "commit_keys", "no_batch", "replays", "diagnoses", "opened_by")  # lander.py: who landed it, and its commits' keys;
# opened_by: the submitting process's actor, the change's opening event's; no_batch: land --no-batch (never in a backlog batch); replays: agentctl's FIX REPLAY proofs (redland.py run_fix_replays)
