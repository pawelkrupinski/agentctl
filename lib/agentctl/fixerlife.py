# agentctl, part `fixerlife`: one fixer lifecycle: the wrapper a dispatched fixer and a family owner share.
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import json
import os
import re
import signal
import time
from typing import Any
from agentctl.core import env_num, fmt_age
from agentctl.timefmt import ts
from agentctl.statefile import read_record, write_json
from agentctl.store import StoreAuthFailed, StoreUnavailable
from agentctl.repos import red_repo_of
from agentctl.redrecord import lease_is_live, red_lease
from agentctl.claudefiles import bg_transcript, fixer_verdict
from agentctl.procs import process_start, socket_host
from agentctl.leases import lease_meta
from agentctl.inboxentry import inbox_write
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
from agentctl.redclaim import bind_fixer_session
from agentctl.redcrash import keep_awake, verdict_cut
from agentctl.dispatchblock import claude_cli, interactive_mode
from agentctl.sessionroll import fixer_rollover


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


def _lease_gone(row: Any, m: Any, out: Any, holder: Any) -> Any:
    """The lease was ended or taken: an outcome, a revoke, or another holder."""
    return bool(out or m.get("revoked") or (row.get("holder") and row["holder"] != holder))


def lease_loss(row, epoch, holder, t):
    """('lapsed', None), ('done', outcome) or ('lost', why) for a lease held at `epoch` by `holder` that reads lost."""
    m = lease_meta(row)
    if not row or row.get("epoch") != epoch:
        return "lost", "taken by another holder"
    out = str(m.get("outcome") or "")
    if m.get("revoked") and LEASE_DONE.match(out):
        return "done", out
    if _lease_gone(row, m, out, holder):
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

    def holds(self, row: Any, now: float) -> bool:
        """Whether `row` is this lease, live, under the same epoch and holder."""
        return bool(row and row.get("epoch") == self.epoch and row.get("holder") == self.holder and lease_is_live(row, now))

    def check(self, st, row=None, progress="retaken"):
        row = st.get(self.name) if row is None else row
        now = st.clock()
        if self.holder is None and row and row.get("epoch") == self.epoch:  # its holder learnt from its own epoch
            self.holder = row.get("holder") or None
        if self.holds(row, now):
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


def store_failure_stop(e: Any, env: Any, again: Any) -> Any:
    """A store call failed in a poll. A refused token is never "asked again" forever: a fresh one once (put in `env`
    too), else the end -- the stop reason. A store that does not answer decides nothing: asked again at the next
    `again`. None: go on."""
    if not isinstance(e, StoreAuthFailed):
        print(f"{ts()} the store did not answer ({e}): asked again at the next {again}", flush=True)
        return None
    if not store_reauth():
        return f"store auth failed: {e}"
    env["AGENTCTL_STORE_TOKEN"] = os.environ["AGENTCTL_STORE_TOKEN"]
    print(f"{ts()} the store refused the token ({e}): a fresh store token taken", flush=True)
    return None


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


class RedFixerWrapper:
    """The dispatched fixer's wrapper (detached), one run: its leases taken and kept as an owner's are (HeldLease), its
    background claude session started and judged poll by poll, its verdict from the session's final answer."""

    def __init__(self, a: Any, st: Any) -> None:
        self.a, self.st = a, st
        self.rec = read_record(a.record)
        self.num, self.wt = self.rec["run"], self.rec["worktree"]
        self.verdict, self.meta_due, self.bid, self.env = None, False, None, {}
        self.poll = env_num("AGENTCTL_BG_POLL", BG_POLL)
        self.every = env_num("AGENTCTL_RED_RENEW", RED_RENEW_EVERY)
        self.cli = claude_cli()
        self.label = fixer_label(self.rec)
        self.t0 = self.last_renew = time.time()
        self.missing, self.unclaimed_told = 0, False

    def leases(self) -> Any:
        return [h.triple() for h in self.held]

    def save(self) -> Any:
        write_json(self.a.record, self.rec)

    def take_leases(self) -> Any:
        """Its leases (an orphan's fixer, with no offer, takes one of its own). False: another fixer is on the red."""
        rec = self.rec
        name, epoch = rec.get("lease"), rec.get("epoch")
        secret = os.environ.pop("AGENTCTL_LEASE_SECRET", None)
        secrets = json.loads(os.environ.pop("AGENTCTL_LEASE_SECRETS", None) or "{}")
        extra = [(x["name"], x["epoch"], secrets.get(x["name"])) for x in rec.get("extra_leases") or []]
        if not name:
            name = red_lease(f"orphan-{os.path.basename(self.wt)}")
            took = self.st.acquire(name, f"laptop:fixer:red-fixer-{self.num}", RED_FIXER_TTL, RED_FIXER_MAX,
                                   progress="start", meta={"kind": "fixer", "worktree": self.wt, "gate": False})
            if "held" in took:
                rec.update(pid=os.getpid(), ended=ts(), rc=0, verdict=f"DUPLICATE: {took['held']['holder']} is on it")
                self.save()
                print(f"RED FIXER {rec['workflow']} #{self.num}: DUPLICATE")
                return False
            epoch, secret = took["epoch"], took["secret"]
        # ONE FIXER LIFECYCLE: its leases kept as an owner's are (HeldLease: a lapse taken again, a red that ended is no
        # failure); the holder is the dispatcher's, read from the lease's own epoch
        self.own = HeldLease(name, epoch, secret, rec.get("holder"), RED_FIXER_TTL)
        self.held = [self.own] + [HeldLease(n_, e_, s_, rec.get("holder"), RED_FIXER_TTL) for n_, e_, s_ in extra]
        return True

    def register(self) -> Any:
        """Its record and lease meta say who runs it (the meta is sent at the polls when the store did not answer)."""
        rec, own = self.rec, self.own
        session, me = rec.get("session"), os.getpid()  # session: known once `claude agents` lists it (--bg picks its id)
        self.meta = {"worktree": self.wt, "session": session, "pid": me, "log": rec["log"], "host": socket_host(),
                     "wrapper_log": os.path.join(os.path.dirname(os.path.abspath(self.a.record)), "fixer.log")}
        try:
            row = self.st.get(own.name)
            if row and row["epoch"] == own.epoch and row["secret"] == own.secret:
                own.holder = own.holder or row.get("holder") or None
                for h in self.held:
                    h.holder = h.holder or own.holder
                rec["holder"] = own.holder
                self.st.note(*own.triple(), f"dispatched: {rec.get('why')}")
                self.meta_due = not fixer_meta_sent(self.st, self.leases(), self.meta)
        except StoreUnavailable as e:  # its lease unread: the meta is sent at the polls below
            print(f"{ts()} the store did not answer ({e}): the leases' meta is sent again", flush=True)
            self.meta_due = True
        rec.update(pid=me, process_start=process_start(me), session=session, lease=own.name, epoch=own.epoch)
        self.save()

    def command(self) -> Any:
        """The `claude --bg` argv and the environment it starts in."""
        rec = self.rec
        u = {"workflow": rec["workflow"], "job": rec.get("job"), "jobs": rec.get("jobs"), "kind": rec.get("kind") or "test",
             "line": rec.get("line"), "flake": rec.get("flake"), "diagnosis": rec.get("diagnosis"),
             "orphan": rec.get("orphan"), "refuted": rec.get("refuted"), "predicts": rec.get("predicts"),
             "prior_work": rec.get("prior_work"), "run": {"number": self.num, "databaseId": rec.get("run_id")},
             "repo": rec.get("repo"), "blame_note": rec.get("blame_note"), "lander": rec.get("lander"),
             "decision": rec.get("decision"), "record": self.a.record}
        prompt = fixer_prompt(u, self.wt, sync_brief(), fixer_rollover(self.st, rec))  # its resumed session past its context: fresh
        argv = [self.cli, "--bg", prompt, "--permission-mode", interactive_mode(), "--permission-prompts", "none",
                "-n", self.label, "--disallowedTools", "Bash(git push:*)", "Bash(*--no-verify*)", "Bash(*PREPUSH_SKIP*)"]
        if red_owner_only(red_repo_of(rec)):  # an owner-only repo's fixer: no landing, no push, no deploy (red_owner_rules)
            argv += list(OWNER_ONLY_DENIED)
        if rec.get("resume") and rec.get("resumed_from"):
            argv += ["--resume", rec["resumed_from"]]
        if rec.get("effort"):  # a deep investigation
            argv += ["--effort", rec["effort"]]
        # NO FIXER IDENTITY IN THE SESSION'S ENVIRONMENT: `claude --bg` may start the claude daemon with it, and every
        # later background session -- other fixers', the owner's -- inherits the daemon's (2026-10-29: the daemon spawned
        # by #2473 fixer-5's wrapper named them all 'red-fixer-2473'). The prompt names the record; the session binds to it.
        env = {k: v for k, v in os.environ.items() if k not in FIXER_IDENTITY_ENV and k != "AGENTCTL_DETACHED"}  # an agent
        return argv, env

    def start(self) -> Any:
        """Start the session and watch it; self.verdict says why when it did not start or was stopped."""
        rec = self.rec
        argv, self.env = self.command()
        if rec.get("session"):
            bind_fixer_session(rec["session"], self.a.record)
        self.t0 = self.last_renew = time.time()
        launch = rec.get("launch_cwd") if rec.get("launch_cwd") and os.path.isdir(rec["launch_cwd"]) else self.wt
        bid, why = bg_start(argv, launch, self.env)  # a revived lander: in its session's own cwd, where `--resume` finds it
        if not bid:
            self.verdict = f"NOT STARTED: `claude --bg` {why}"
            if rec.get("lander"):  # said, never looped: the red's next dispatch is a fresh fixer (red_lease_ended)
                line = (f"LAUNCH FAILED: {why[:400]} (reviving the lander of {rec['lander'].get('change')}, "
                        f"{rec['workflow']} #{self.num})")
                inbox_write("red fixer", line, [line], [], 1, cwd=self.wt)
            return
        self.bid = bid
        rec.update(bg_id=bid, label=self.label)
        self.save()
        self.watch()
        bg_stop(self.cli, bid)  # done or stopped: the session ends (`claude agents --all` keeps it, `claude attach` reopens it)

    def cancelled(self, signum: Any, _frame: Any) -> Any:  # the wrapper is stopped: its session goes with it
        bg_stop(self.cli, self.bid)
        for n_, e_, s_ in self.leases():
            try:
                self.st.release(n_, e_, s_, outcome="STOPPED: the fixer's wrapper was stopped")
            except StoreUnavailable:  # it lapses by its TTL instead
                pass
        os._exit(143)

    def watch(self) -> Any:
        for sg in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
            signal.signal(sg, self.cancelled)
        self.stall = env_num("AGENTCTL_RED_STALL", RED_STALL)
        self.prog = TurnProgress(self.stall, time.time())
        try:
            while self.verdict is None and self.step():
                pass
        except Exception as ex:  # never a dead wrapper with a live session and its leases held
            self.verdict = f"STOPPED: the fixer's wrapper failed ({type(ex).__name__}: {str(ex)[:200]})"

    def step(self) -> Any:
        """One poll: False when the session's turn is over (or it is gone from the list), else True with self.verdict
        set when the wrapper must stop it."""
        time.sleep(self.poll)
        ent = bg_session(self.cli, self.bid)
        self.adopt_transcript(ent)
        self.missing = self.missing + 1 if ent is None else 0
        if bg_turn_over(ent, self.rec.get("transcript")) or self.missing >= 3:  # finished its turn (done, stopped,
            return False  # blocked; or waiting with its verdict said, bg_turn_over), or gone from the list
        self.verdict = self.unclaimed_stop(ent) or self.limit_stop() or self.lease_stop(ent)
        return True

    def adopt_transcript(self, ent: Any) -> Any:
        """The session's id and transcript, once `claude agents` lists them: the record, the binding and the meta."""
        rec = self.rec
        if not (ent and ent.get("sessionId") and not rec.get("transcript")):
            return
        session, transcript = ent["sessionId"], bg_transcript(ent["sessionId"])
        # the lease's meta first, the record after: a record that names the session means the lease carries it too
        # (2026-11-25 run 45702726773: a reader of the record found the lease's session None)
        self.meta.update(session=session, log=transcript or rec["log"], bg_id=self.bid)
        self.meta_due = not fixer_meta_sent(self.st, self.leases(), self.meta)
        rec.update(session=session, transcript=transcript)
        if transcript:
            rec["log"] = transcript
        self.save()
        bind_fixer_session(session, self.a.record)

    def unclaimed_stop(self, ent: Any) -> Any:
        """NEVER CLAIMED (2026-10-31): a session that in claim_within neither started its transcript nor ran its first
        `red claim` never got going -- ended with what `claude agents` says of it, never a silent "never got going".
        One that works but never ran `red claim` is said once, on its lease and the inbox."""
        rec, claim_within = self.rec, env_num("AGENTCTL_FIXER_CLAIM_WITHIN", FIXER_CLAIM_WITHIN)
        if time.time() - self.t0 <= claim_within or (read_record(self.a.record) or {}).get("note"):
            return None
        if not rec.get("transcript"):
            return (f"NOT STARTED: session {self.bid} never claimed in {fmt_age(claim_within)} (`claude agents`: "
                    f"{bg_row_brief(ent)})")
        if not self.unclaimed_told:
            self.unclaimed_told = True
            tail = " | ".join(l.strip() for l in bg_final_text(rec.get("transcript")).splitlines()[-3:] if l.strip())
            line = (f"RED FIXER {rec['workflow']} #{self.num}: session {self.bid} has not claimed in "
                    f"{fmt_age(claim_within)} (`claude agents`: {bg_row_brief(ent)}); its last words: {tail[:300]}")
            inbox_write("red fixer", line, [line], [], 1, cwd=self.wt)
            try:
                self.st.note(*self.own.triple(), line[:300])
            except StoreUnavailable:
                pass
        return None

    def limit_stop(self) -> Any:
        """TIMED OUT past the wall time, or STOPPED by the worktree's RED-STOP.md."""
        wall = env_num("AGENTCTL_RED_FIXER_WALL", RED_FIXER_WALL)
        if time.time() - self.t0 > wall:
            return f"TIMED OUT after {int(wall // 60)} min (maximum wall time)"
        stop_file = os.path.join(self.wt, "RED-STOP.md")
        return "STOPPED: " + open(stop_file).readline().strip() if os.path.exists(stop_file) else None

    def lease_stop(self, ent: Any) -> Any:
        """Why the leases say stop, else None. A store that does not answer decides nothing: it is asked again at the
        next poll, and a lease that lapsed meanwhile reads as lost once it answers. 2026-10-29, #2517: `POST
        /v1/store/get: Connection reset by peer` (ci-runner's store, through the tunnel) ended the wrapper a minute in;
        nothing renewed its lease, and a second fixer was dispatched onto the red its session was still working on."""
        rec, own = self.rec, self.own
        try:
            if self.meta_due:
                self.meta_due = not fixer_meta_sent(self.st, self.leases(), self.meta)
            gone = own.check(self.st)  # revoked (the red cleared, a release, its own block): stop at once;
            stop = lease_stop_line(*gone) if gone else self.renew_due(ent)  # lapsed: taken again, going on
            if own.epoch != rec.get("epoch"):  # taken again after a lapse: the record names its epoch
                rec["epoch"] = own.epoch
                self.save()
            return stop
        except (StoreAuthFailed, StoreUnavailable) as e:
            return store_failure_stop(e, self.env, "poll")

    def renew_due(self, ent: Any) -> Any:
        """Each beat, when due: judge the session's progress (PROOF OF WORK: a living wrapper is not one) and renew the
        leases by it -- the run's other reds with the first. Why it must stop, else None."""
        if time.time() - self.last_renew < self.every:
            return None
        rec = self.rec
        note = (read_record(self.a.record) or {}).get("note")
        mark, _moved, stalled = self.prog.judge(rec["log"], rec.get("bg_id"), self.wt, ent, rec.get("transcript"),
                                                time.time(), self.every)
        stop = f"STALLED: no progress (transcript, worktree) for {fmt_age(self.stall)}" if stalled else None
        if not stop:
            beat = f"{mark} beat {int(time.time())}"
            for h in self.held[1:]:
                h.renew(self.st, beat, note=note)
            gone = self.own.renew(self.st, beat, note=note)
            stop = lease_stop_line(*gone) if gone else None
        self.last_renew = time.time()
        return stop

    def release_lease(self, lease: Any, verdict: Any, tail: Any) -> Any:
        """Release one lease with the verdict: a store that does not answer is asked again, a poll apart."""
        n_, e_, s_ = lease
        for left in range(RED_RELEASE_ASKS, 0, -1):
            try:
                self.st.release(n_, e_, s_, outcome=verdict_cut(verdict), notes="\n".join(tail[-8:]))
                return
            except StoreAuthFailed as e:  # refused, not unanswered: the laptop's tick revokes it (red_reap_orphans)
                print(f"{ts()} could not release {n_} ({e}): the tick revokes it", flush=True)
                return
            except StoreUnavailable as e:  # never answered: the lease lapses by its TTL, and the red is offered again
                print(f"{ts()} could not release {n_} ({e}){'' if left > 1 else ': it lapses by its TTL'}", flush=True)
                time.sleep(self.poll)

    def session_verdict(self, text: Any) -> Any:
        """The wrapper's verdict if it stopped the session, else the session's own final line, else why it has none."""
        failure = bg_failure(self.rec.get("bg_id"))  # claude's own word on a failed session: the crash's actual error
        return self.verdict or fixer_verdict(text) or (
            "ended with no LANDED / GAVE UP line"
            + (f" (session {self.rec['bg_id']}: failed: {failure})" if failure else ""))

    def finish(self) -> Any:
        """The verdict (the session's final answer), told to the inbox, the leases released with it; the exit code."""
        rec = self.rec
        if not rec.get("transcript") and rec.get("session"):
            rec["transcript"] = bg_transcript(rec["session"])
        text = bg_turn(rec.get("transcript"))["final"]  # THIS turn's: a resumed session's transcript holds earlier ones
        verdict = self.session_verdict(text)
        line = f"RED FIXER {rec['workflow']} #{self.num}: {verdict[:300]}"
        tail = [l[:300] for l in text.splitlines()[-INBOX_TAIL:]]
        if not fixer_quiet(verdict):  # the red ended under it, already fixed, or the owner's decision: the reconciler's
            inbox_write("red fixer", line, [line], tail, 0 if verdict.startswith("LANDED") else 1, cwd=self.wt)
        if self.meta_due:  # never sent while the session ran: before the release, so a resume finds its session
            fixer_meta_sent(self.st, self.leases(), self.meta)
        for lease in self.leases():
            self.release_lease(lease, verdict, tail)
        rc = 0 if verdict.startswith("LANDED") else 1
        rec.update(ended=ts(), rc=rc, verdict=verdict)
        self.save()
        if not rec.get("orphan"):  # an orphan's tree is its author's, never litter
            mark_internal(self.wt)  # its fixer ended: the tree is agentctl's litter now, never a fixer or owner
        print(line)
        return rc


def red_fixer(a, st):
    """The dispatched fixer's wrapper (detached): start its background claude session under the red's lease, renew the
    lease by its progress every RED_RENEW_EVERY s, `claude stop` it when the lease is lost (RED-STOP.md), release the
    lease with the verdict from the session's final answer."""
    wrapper = RedFixerWrapper(a, st)
    keep_awake()  # the laptop must not idle-sleep past the lease's TTL while the fixer holds it (host_slept)
    if not wrapper.take_leases():
        return 0
    wrapper.register()
    wrapper.start()
    return wrapper.finish()


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
