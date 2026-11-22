# agentctl, part `fixerlife`: one fixer lifecycle: the wrapper a dispatched fixer and a family owner share.
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import json
import os
import re
import signal
import time
from agentctl.core import env_num, fmt_age
from agentctl.timefmt import ts
from agentctl.statefile import read_record, write_json
from agentctl.store import StoreAuthFailed, StoreUnavailable, open_store
from agentctl.procs import process_start, socket_host
from agentctl.repos import red_repo_of
from agentctl.leases import lease_meta
from agentctl.inboxentry import inbox_write
from agentctl.redrecord import lease_is_live, red_lease
from agentctl.claudefiles import bg_transcript, fixer_verdict
from agentctl.fixersession import (
    BG_POLL, FIXER_CLAIM_WITHIN, RED_STALL, bg_failure, bg_final_text, bg_row_brief, bg_session, bg_start, bg_stop,
    bg_turn, bg_turn_over, bg_wait_mark, fixer_label, store_reauth)
from agentctl.worktrees import mark_internal
from agentctl.inbox import INBOX_TAIL
from agentctl.redreplay import sync_brief
from agentctl.redrepos import red_owner_only
from agentctl.fixerland import fixer_mark, fixer_meta_sent
from agentctl.fixer import FIXER_IDENTITY_ENV, OWNER_ONLY_DENIED, fixer_prompt
from agentctl.red import RED_FIXER_MAX, RED_FIXER_TTL, RED_FIXER_WALL, RED_RELEASE_ASKS, RED_RENEW_EVERY
from agentctl.branches import claude_cli, interactive_mode
from agentctl.redclaim import bind_fixer_session
from agentctl.redcrash import keep_awake, verdict_cut


# ONE FIXER LIFECYCLE (2026-11-04): a dispatched fixer's wrapper (red_fixer) and a family owner's (owner.red_owner) keep
# their leases and judge their session's progress with the SAME code below, so the two can never drift again. On
# 2026-11-03 the owners learnt to renew by activity and to take a merely lapsed heartbeat again (aaf0082..057ba94); the
# red fixers did not, and on 2026-11-04 13 of them ended "STOPPED: the red's lease was lost" -- each a RED FIXER failure
# in the owner's inbox and a fresh dispatch. Most were no failure at all: their red had CLEARED (the reconciler revokes
# the lease with `red cleared`) while they worked. A lost lease is now read for what it is (lease_loss):
#   - LAPSED (its clock ran out -- the wrapper or its machine silent, the store out of reach -- and nobody took,
#     revoked or released it): the living wrapper takes it again (lease_retake) and goes on;
#   - DONE (revoked because the red needs no fixer any more: cleared, verified, retired, a fix submitted): the work is
#     over, the wrapper ends quietly -- no failure, no attempt;
#   - LOST (re-taken by another, released, revoked otherwise, past its max hold): the wrapper stops, as before.
LEASE_DONE = re.compile(r"(red (cleared|verified|retired)|fix submitted)\b")
LEASE_RETAKE_DROP = ("outcome", "notes", "released_at", "revoked", "paused")


def lease_loss(row, epoch, holder, t):
    """('lapsed', None), ('done', outcome) or ('lost', why) for a lease held at `epoch` by `holder` that reads lost."""
    m = lease_meta(row)
    if not row or row.get("epoch") != epoch:
        return "lost", "taken by another holder"
    out = str(m.get("outcome") or "")
    if m.get("revoked") and LEASE_DONE.match(out):
        return "done", out
    if out or m.get("revoked") or (row.get("holder") and row["holder"] != holder):
        return "lost", out or "taken by another holder"
    if t >= float(row.get("max_until") or 0):
        return "lost", "past its max hold"
    return "lapsed", None


def lease_retake(st, name, epoch, holder, ttl, progress):
    """A LAPSED lease taken again by its living holder: the same item, meta and max hold. {'epoch', 'secret'}, or None
    when it is no longer to be had (somebody took, revoked or released it meanwhile)."""
    row = st.get(name)
    now = st.clock()
    if lease_loss(row, epoch, holder, now)[0] != "lapsed":
        return None
    meta = {k: v for k, v in lease_meta(row).items() if k not in LEASE_RETAKE_DROP}
    got = st.acquire(name, holder, ttl, max(60.0, float(row["max_until"]) - now), progress=progress,
                     item=row.get("item"), meta=meta)
    return None if "held" in got else got


class HeldLease:
    """One lease a wrapper holds (ONE FIXER LIFECYCLE): renew() with each beat, check() at a poll. Each answers None
    while it is held -- a lapsed one taken again on the way (`retaken` counts them) -- else (kind, why): 'done' or
    'lost' (lease_loss). StoreUnavailable / StoreAuthFailed reach the caller (asked again at the next poll)."""

    def __init__(self, name, epoch, secret, holder, ttl):
        self.name, self.epoch, self.secret, self.holder, self.ttl, self.retaken = name, epoch, secret, holder, ttl, 0

    def triple(self):
        return self.name, self.epoch, self.secret

    def check(self, st, row=None, progress="retaken"):
        row = st.get(self.name) if row is None else row
        now = st.clock()
        if self.holder is None and row and row.get("epoch") == self.epoch:  # its holder learnt from its own epoch
            self.holder = row.get("holder") or None
        if row and row.get("epoch") == self.epoch and row.get("holder") == self.holder and lease_is_live(row, now):
            return None
        kind, why = lease_loss(row, self.epoch, self.holder, now)
        if kind == "lapsed":
            got = lease_retake(st, self.name, self.epoch, self.holder, self.ttl, progress)
            if got:
                self.epoch, self.secret, self.retaken = got["epoch"], got["secret"], self.retaken + 1
                print(f"{ts()} {self.name} had lapsed (asleep or out of the store's reach): taken again", flush=True)
                return None
            kind, why = "lost", "taken by another holder after it lapsed"
        return kind, why

    def renew(self, st, progress, note=None):
        if st.renew(self.name, self.epoch, self.secret, progress, self.ttl, note=note) != "lost":
            return None
        return self.check(st, progress=progress)


class TurnProgress:
    """THE WRAPPER JUDGES PROGRESS (RED_STALL) of a turn, for both kinds: the mark (fixer_mark + bg_wait_mark) moving is
    progress; a turn whose mark stood still past `stall` is stalled. The leases renew with each beat of a turn not
    stalled (2026-11-03: renewed only on a new mark, a 15 min lease lapsed inside the 30 min stall budget)."""

    def __init__(self, stall, now):
        self.stall, self.mark, self.at = stall, None, now

    def judge(self, log, bid, wt, ent, transcript, now, every):
        """(mark, moved, stalled) at this beat."""
        mark = fixer_mark({"log": log or "", "bg_id": bid}, wt) + bg_wait_mark(ent, transcript, now, every)
        if mark != self.mark:
            self.mark, self.at = mark, now
            return mark, True, False
        return mark, False, now - self.at > self.stall


def lease_stop_line(kind, why, what="red's"):
    """The wrapper's stop reason for a lease no longer its own (HeldLease)."""
    if kind == "done":
        return f"ENDED: {why} (its lease was taken back: nothing left to fix)"
    if str(why).startswith("BLOCKED ON OWNER"):
        return str(why)
    return f"STOPPED: the {what} lease was lost ({why})"


def fixer_quiet(verdict):
    """A verdict the reconciler acts on itself (red_lease_ended), never a RED FIXER failure in the owner's inbox: the red
    ended under the fixer (ENDED), it is already fixed (verified by its probe), or it needs the owner's decision (one
    WAITING ON YOU, from the reconciler)."""
    v = str(verdict or "")
    return v.startswith("ENDED: ") or bool(GAVE_UP_FIXED.match(v) or GAVE_UP_DECIDE.match(v))


class CurrentStore:
    """open_store() resolved again at every call, so a long-running process follows `agentctl store use URL` (2026-10-29
    cutover: the gate fixer's wrapper, started on the laptop's then store, renewed its leases there for hours while
    ci-runner's copies expired and its reds were offered again)."""

    def __getattr__(self, name):
        return getattr(open_store(), name)


def red_fixer(a):
    """The dispatched fixer's wrapper (detached): start its background claude session under the red's lease, renew the
    lease by its progress every RED_RENEW_EVERY s, `claude stop` it when the lease is lost (RED-STOP.md), release the
    lease with the verdict from the session's final answer."""
    rec = read_record(a.record)
    keep_awake()  # the laptop must not idle-sleep past the lease's TTL while the fixer holds it (host_slept)
    num, wt = rec["run"], rec["worktree"]
    session = rec.get("session")  # known once `claude agents` lists the session (--bg picks its id)
    me = os.getpid()
    who = f"red-fixer-{num}"
    st = CurrentStore()
    name, epoch = rec.get("lease"), rec.get("epoch")
    secret = os.environ.pop("AGENTCTL_LEASE_SECRET", None)
    extra = [(x["name"], x["epoch"], json.loads(os.environ.get("AGENTCTL_LEASE_SECRETS") or "{}").get(x["name"]))
             for x in rec.get("extra_leases") or []]
    os.environ.pop("AGENTCTL_LEASE_SECRETS", None)
    if not name:  # an orphan's fixer (no offer): it takes a lease of its own
        name = red_lease(f"orphan-{os.path.basename(wt)}")
        took = st.acquire(name, f"laptop:fixer:{who}", RED_FIXER_TTL, RED_FIXER_MAX, progress="start",
                          meta={"kind": "fixer", "worktree": wt, "gate": False})
        if "held" in took:
            rec.update(pid=me, ended=ts(), rc=0, verdict=f"DUPLICATE: {took['held']['holder']} is on it")
            write_json(a.record, rec)
            print(f"RED FIXER {rec['workflow']} #{num}: DUPLICATE")
            return 0
        epoch, secret = took["epoch"], took["secret"]
    # ONE FIXER LIFECYCLE: its leases kept as an owner's are (HeldLease: a lapse taken again, a red that ended is no
    # failure); the holder is the dispatcher's, read from the lease's own epoch
    own = HeldLease(name, epoch, secret, rec.get("holder"), RED_FIXER_TTL)
    held_ = [own] + [HeldLease(n_, e_, s_, rec.get("holder"), RED_FIXER_TTL) for n_, e_, s_ in extra]

    def leases():
        return [h.triple() for h in held_]
    meta, meta_due = {"worktree": wt, "session": session, "pid": me, "log": rec["log"], "host": socket_host(),
                      "wrapper_log": os.path.join(os.path.dirname(os.path.abspath(a.record)), "fixer.log")}, False
    try:
        row = st.get(name)
        if row and row["epoch"] == epoch and row["secret"] == secret:
            own.holder = own.holder or row.get("holder") or None
            for h in held_:
                h.holder = h.holder or own.holder
            rec["holder"] = own.holder
            st.note(name, epoch, secret, f"dispatched: {rec.get('why')}")
            meta_due = not fixer_meta_sent(st, leases(), meta)
    except StoreUnavailable as e:  # its lease unread: the meta is sent at the polls below
        print(f"{ts()} the store did not answer ({e}): the leases' meta is sent again", flush=True)
        meta_due = True
    rec.update(pid=me, process_start=process_start(me), session=session, lease=name, epoch=epoch)
    write_json(a.record, rec)
    u = {"workflow": rec["workflow"], "job": rec.get("job"), "jobs": rec.get("jobs"), "kind": rec.get("kind") or "test",
         "line": rec.get("line"), "flake": rec.get("flake"), "diagnosis": rec.get("diagnosis"), "orphan": rec.get("orphan"),
         "refuted": rec.get("refuted"), "predicts": rec.get("predicts"), "prior_work": rec.get("prior_work"),
         "run": {"number": num, "databaseId": rec.get("run_id")}, "repo": rec.get("repo"),
         "blame_note": rec.get("blame_note"), "lander": rec.get("lander"), "decision": rec.get("decision")}
    u["record"] = a.record
    prompt = fixer_prompt(u, wt, sync_brief(), rec.get("resume_note"))
    cli = claude_cli()
    label = fixer_label(rec)
    argv = [cli, "--bg", prompt, "--permission-mode", interactive_mode(), "--permission-prompts", "none", "-n", label,
            "--disallowedTools", "Bash(git push:*)", "Bash(*--no-verify*)", "Bash(*PREPUSH_SKIP*)"]
    if red_owner_only(red_repo_of(rec)):  # an owner-only repo's fixer: no landing, no push, no deploy (red_owner_rules)
        argv += list(OWNER_ONLY_DENIED)
    if rec.get("resume") and rec.get("resumed_from"):
        argv += ["--resume", rec["resumed_from"]]
    if rec.get("effort"):  # a deep investigation
        argv += ["--effort", rec["effort"]]
    # NO FIXER IDENTITY IN THE SESSION'S ENVIRONMENT: `claude --bg` may start the claude daemon with it, and every
    # later background session -- other fixers', the owner's -- inherits the daemon's (2026-10-29: the daemon spawned by
    # #2473 fixer-5's wrapper named them all 'red-fixer-2473'). The prompt names the record; the session binds to it.
    env = {k: v for k, v in os.environ.items() if k not in FIXER_IDENTITY_ENV and k != "AGENTCTL_DETACHED"}  # an agent
    if rec.get("session"):
        bind_fixer_session(rec["session"], a.record)
    wall = env_num("AGENTCTL_RED_FIXER_WALL", RED_FIXER_WALL)
    every = env_num("AGENTCTL_RED_RENEW", RED_RENEW_EVERY)
    poll = env_num("AGENTCTL_BG_POLL", BG_POLL)
    t0, verdict, last_renew = time.time(), None, time.time()
    launch = rec.get("launch_cwd") if rec.get("launch_cwd") and os.path.isdir(rec["launch_cwd"]) else wt
    bid, why = bg_start(argv, launch, env)  # a revived lander: in its session's own cwd, where `--resume` finds it
    if not bid:
        verdict = f"NOT STARTED: `claude --bg` {why}"
        if rec.get("lander"):  # said, never looped: the red's next dispatch is a fresh fixer (red_lease_ended)
            line = f"LAUNCH FAILED: {why[:400]} (reviving the lander of {rec['lander'].get('change')}, {rec['workflow']} #{num})"
            inbox_write("red fixer", line, [line], [], 1, cwd=wt)
    else:
        rec.update(bg_id=bid, label=label)
        write_json(a.record, rec)

        def cancelled(signum, _frame):  # the wrapper is stopped: its session goes with it
            bg_stop(cli, bid)
            for n_, e_, s_ in leases():
                try:
                    st.release(n_, e_, s_, outcome="STOPPED: the fixer's wrapper was stopped")
                except StoreUnavailable:  # it lapses by its TTL instead
                    pass
            os._exit(143)
        for sg in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
            signal.signal(sg, cancelled)
        missing, unclaimed_told = 0, False
        stall = env_num("AGENTCTL_RED_STALL", RED_STALL)
        prog = TurnProgress(stall, time.time())
        claim_within = env_num("AGENTCTL_FIXER_CLAIM_WITHIN", FIXER_CLAIM_WITHIN)
        try:
            while verdict is None:
                time.sleep(poll)
                ent = bg_session(cli, bid)
                if ent and ent.get("sessionId") and not rec.get("transcript"):
                    rec.update(session=ent["sessionId"], transcript=bg_transcript(ent["sessionId"]))
                    write_json(a.record, rec)
                    bind_fixer_session(rec["session"], a.record)
                    if rec["transcript"]:
                        rec["log"] = rec["transcript"]
                    meta.update(session=rec["session"], log=rec["log"], bg_id=bid)
                    meta_due = not fixer_meta_sent(st, leases(), meta)
                    write_json(a.record, rec)
                missing = missing + 1 if ent is None else 0
                if bg_turn_over(ent, rec.get("transcript")) or missing >= 3:  # finished its turn (done, stopped,
                    # blocked; or waiting with its verdict said, bg_turn_over), or gone from the list
                    break
                stop = None
                late = time.time() - t0 > claim_within
                if late and not rec.get("transcript") and not (read_record(a.record) or {}).get("note"):
                    # NEVER CLAIMED (2026-10-31): a session that in claim_within neither started its transcript nor ran
                    # its first `red claim` never got going -- ended with what `claude agents` says of it, never a
                    # silent "never got going"
                    stop = (f"NOT STARTED: session {bid} never claimed in {fmt_age(claim_within)} (`claude agents`: "
                            f"{bg_row_brief(ent)})")
                elif late and not unclaimed_told and not (read_record(a.record) or {}).get("note"):
                    unclaimed_told = True  # it works but never ran `red claim`: said once, on its lease and the inbox
                    tail = " | ".join(l.strip() for l in bg_final_text(rec.get("transcript")).splitlines()[-3:] if l.strip())
                    line = (f"RED FIXER {rec['workflow']} #{num}: session {bid} has not claimed in "
                            f"{fmt_age(claim_within)} (`claude agents`: {bg_row_brief(ent)}); its last words: {tail[:300]}")
                    inbox_write("red fixer", line, [line], [], 1, cwd=wt)
                    try:
                        st.note(*own.triple(), line[:300])
                    except StoreUnavailable:
                        pass
                if stop:
                    pass
                elif time.time() - t0 > wall:
                    stop = f"TIMED OUT after {int(wall // 60)} min (maximum wall time)"
                elif os.path.exists(os.path.join(wt, "RED-STOP.md")):
                    stop = "STOPPED: " + open(os.path.join(wt, "RED-STOP.md")).readline().strip()
                else:
                    # A store that does not answer decides nothing: it is asked again at the next poll, and a lease that
                    # lapsed meanwhile reads as lost once it answers. 2026-10-29, #2517: `POST /v1/store/get: Connection
                    # reset by peer` (ci-runner's store, through the tunnel) ended the wrapper a minute in; nothing renewed
                    # its lease, and a second fixer was dispatched onto the red its session was still working on.
                    try:
                        if meta_due:
                            meta_due = not fixer_meta_sent(st, leases(), meta)
                        gone_ = own.check(st)  # revoked (the red cleared, a release, its own block): stop at once;
                        if gone_:              # lapsed (asleep, the store out of reach): taken again, going on
                            stop = lease_stop_line(*gone_)
                        elif time.time() - last_renew >= every:
                            note = (read_record(a.record) or {}).get("note")
                            mark, _moved, stalled = prog.judge(rec["log"], rec.get("bg_id"), wt, ent,
                                                               rec.get("transcript"), time.time(), every)
                            if stalled:  # PROOF OF WORK: a living wrapper is not one
                                stop = f"STALLED: no progress (transcript, worktree) for {fmt_age(stall)}"
                            beat = f"{mark} beat {int(time.time())}"
                            for h in held_[1:] if not stop else ():  # the run's other reds: renewed with it
                                h.renew(st, beat, note=note)
                            gone_ = None if stop else own.renew(st, beat, note=note)
                            if gone_:
                                stop = lease_stop_line(*gone_)
                            last_renew = time.time()
                        if own.epoch != rec.get("epoch"):  # taken again after a lapse: the record names its epoch
                            rec["epoch"] = own.epoch
                            write_json(a.record, rec)
                    except StoreAuthFailed as e:  # never "asked again" forever: a fresh token once, else the end
                        if store_reauth():
                            env["AGENTCTL_STORE_TOKEN"] = os.environ["AGENTCTL_STORE_TOKEN"]
                            print(f"{ts()} the store refused the token ({e}): a fresh store token taken", flush=True)
                        else:
                            stop = f"store auth failed: {e}"
                    except StoreUnavailable as e:
                        print(f"{ts()} the store did not answer ({e}): asked again at the next poll", flush=True)
                if stop:
                    verdict = stop
        except Exception as ex:  # never a dead wrapper with a live session and its leases held
            verdict = f"STOPPED: the fixer's wrapper failed ({type(ex).__name__}: {str(ex)[:200]})"
        bg_stop(cli, bid)  # done or stopped: the session ends (`claude agents --all` keeps it, `claude attach` reopens it)
    if not rec.get("transcript") and rec.get("session"):
        rec["transcript"] = bg_transcript(rec["session"])
    text = bg_turn(rec.get("transcript"))["final"]  # THIS turn's: a resumed session's transcript holds earlier ones
    failure = bg_failure(rec.get("bg_id"))  # claude's own word on a failed session: the crash's actual error
    verdict = verdict or fixer_verdict(text) or ("ended with no LANDED / GAVE UP line"
                                                 + (f" (session {rec['bg_id']}: failed: {failure})" if failure else ""))
    line = f"RED FIXER {rec['workflow']} #{num}: {verdict[:300]}"
    tail = [l[:300] for l in text.splitlines()[-INBOX_TAIL:]]
    if not fixer_quiet(verdict):  # the red ended under it, already fixed, or the owner's decision: the reconciler's
        inbox_write("red fixer", line, [line], tail, 0 if verdict.startswith("LANDED") else 1, cwd=wt)
    if meta_due:  # never sent while the session ran: before the release, so a resume finds its session
        fixer_meta_sent(st, leases(), meta)
    for n_, e_, s_ in leases():
        for left in range(RED_RELEASE_ASKS, 0, -1):  # a store that does not answer is asked again, a poll apart
            try:
                st.release(n_, e_, s_, outcome=verdict_cut(verdict), notes="\n".join(tail[-8:]))
                break
            except StoreAuthFailed as e:  # refused, not unanswered: the laptop's tick revokes it (red_reap_orphans)
                print(f"{ts()} could not release {n_} ({e}): the tick revokes it", flush=True)
                break
            except StoreUnavailable as e:  # never answered: the lease lapses by its TTL, and the red is offered again
                print(f"{ts()} could not release {n_} ({e}){'' if left > 1 else ': it lapses by its TTL'}", flush=True)
                time.sleep(poll)
    rec.update(ended=ts(), rc=0 if verdict.startswith("LANDED") else 1, verdict=verdict)
    write_json(a.record, rec)
    if not rec.get("orphan"):  # an orphan's tree is its author's, never litter
        mark_internal(wt)  # its fixer ended: the tree is agentctl's litter now, never a fixer or owner
    print(line)
    return 0 if verdict.startswith("LANDED") else 1


# AN ATTEMPT IS A FIXER THAT TRIED AND FAILED TO FIX (2026-11-04: RED ESCALATE 261 and FIXER FAILED 204 in a week; #728
# escalated 58 times on 11-02, each owner turn's "GAVE UP: ... the approved flip-unification stage 5 (a resolver rule
# change, outside red fixing)" counted as one more attempt at the deep tier). Not an attempt:
#   - a dispatched fixer's or owner's lease LOST TO INFRA (lapsed: the laptop asleep, the store out of reach, a wrapper
#     gone; or orphaned): offered again quietly -- a living wrapper takes its lapsed lease again itself
#     (fixer.HeldLease), and the crash-loop breaker still bounds launches that keep ending that way;
#   - "GAVE UP: already fixed": checked by the red's own probe (a newer decided run no longer failing this way) -- the
#     red is closed when it passes, else it waits in fix-pending (verify) for the run that decides it; a claim the run
#     refutes is an attempt then;
#   - "GAVE UP: needs the owner's decision / a design change": the red goes onto the owner ONCE, as the existing WAITING
#     ON YOU (red_decision_block) with the fixer's words as its diagnosis -- never offered again meanwhile.
GAVE_UP_FIXED = re.compile(r"\W*GAVE UP:.*?(\balready (?:fixed|fixes|landed|on (?:origin/)?main)\b|\bfixed upstream\b|"
                           r"\bno fix needed\b|\bnothing (?:left )?to (?:fix|land)\b)", re.I | re.S)


GAVE_UP_DECIDE = re.compile(r"\W*GAVE UP:.*?(\bneeds? (?:the |an? )?owner|\bowner'?s (?:choice|decision|call|approval)|"
                            r"\boutside red fixing\b|\bdesign (?:change|call|decision)\b|\bresolver (?:rule|design)\b)",
                            re.I | re.S)
