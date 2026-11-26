# agentctl, part `ownerwrapper`: a family owner's wrapper: its heartbeat, its turns, its reds' leases
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from agentctl.clock import epoch_now
import os
import signal
import time
from typing import Any
from agentctl.core import fmt_age
from agentctl.timefmt import ts
from agentctl.statefile import read_record, write_json
from agentctl.claudefiles import bg_transcript, fixer_verdict
from agentctl.procs import process_start
from agentctl.inboxentry import inbox_write
from agentctl.fixersession import bg_failure, bg_session, bg_start, bg_stop, bg_turn, bg_turn_over
from agentctl.redknobs import RED_STALL
from agentctl.inbox import INBOX_TAIL
from agentctl.fixerblockers import claude_cli, interactive_mode
from agentctl.store import StoreAuthFailed, StoreUnavailable
from agentctl.owners import OWNER_TTL, owner_lease, owner_record_path
from agentctl.laptoprole import laptop_role_standby
from agentctl.fixer import FIXER_IDENTITY_ENV
from agentctl.redknobs import RED_FIXER_WALL
from agentctl.redclaim import bind_fixer_session
from agentctl.hostsleep import keep_awake
from agentctl.reddecision import verdict_cut
from agentctl.ownerleases import owner_drop, owner_hand, owner_holder, owner_leases, owner_tell_leases
from agentctl.sessionroll import owner_rollover, owner_turn_line, session_seen
from agentctl.fixerlife import HeldLease, TurnProgress, store_failure_stop
from agentctl.owner import owner_meta, owner_resume_target, owner_take_heartbeat, owner_turn_prompt
from agentctl.fixerknobs import knob_bg_poll
from agentctl.redknobs import knob_red_fixer_wall, knob_red_renew, knob_red_stall
from agentctl.fixerknobs import knob_owner_idle


OWNER_EMPTY_TURNS = 2        # turns in a row ending with no verdict line: the owner ends (its reds are offered again)


OWNER_HEARTBEAT_LOST = "STOPPED: the owner's heartbeat lease was lost"


def in_turn_of(ent: Any, transcript: Any) -> bool:
    """The wrapper's turn's session is still at work (its last `claude agents` row, and its transcript, not over)."""
    return bool(ent and ent.get("id") and not bg_turn_over(ent, transcript))


GO_ON = object()  # a step's piece that stopped nothing: the pass goes on (a stop reason, or None, ends it)


def red_owner(a: Any, st: Any) -> int:
    """THE OWNER'S WRAPPER (detached; `agentctl red owner --family F`): see OwnerWrapper."""
    fam = a.family
    rec = read_record(owner_record_path(fam)) or {}
    hb = owner_claim_heartbeat(st, fam, rec)
    if hb is None:
        return 0
    OwnerWrapper(st, fam, rec, hb).run()
    return 0


def owner_claim_heartbeat(st: Any, fam: Any, rec: Any) -> Any:
    """The wrapper's heartbeat lease, or None (the refusal printed): a standby machine runs no owner (ONE MACHINE HOLDS
    THE LAPTOP ROLE, server.py), a live owner holds it (DUPLICATE), or the one handed over was lost."""
    standby = laptop_role_standby(st)
    if standby:
        print(f"RED OWNER {fam}: NOT STARTED ({standby})")
        return None
    hb = owner_take_heartbeat(st, fam, owner_holder(fam),
                              owner_meta(fam, rec.get("worktree"), os.getpid(), rec.get("notebook")))
    if "held" in hb:
        print(f"RED OWNER {fam}: DUPLICATE ({hb['held']['holder']} is alive)")
        return None
    if "lost" in hb:
        print(f"RED OWNER {fam}: NOT STARTED: the heartbeat taken for it was lost ({str(hb['lost'])[:300]})")
        return None
    return hb


class OwnerWrapper:
    """The owner's wrapper: its heartbeat lease, its reds' leases renewed by the turn's progress, one session resumed
    turn after turn, each turn's leases released with the turn's verdict. A turn is bounded as a fixer's is:
    RED_FIXER_WALL, and a session missing from `claude agents` three polls running has ended. Whatever raises, the
    session is stopped and every lease released (never a live session nobody holds). Its state: the turn's session
    (`bid`) and its reds (`names`), the missed polls, the empty turns in a row, the beats and the idle clock."""

    def __init__(self, st: Any, fam: Any, rec: Any, hb: Any) -> None:
        self.st, self.fam, self.rec, self.hb = st, fam, rec, hb
        self.path = owner_record_path(fam)
        self.wt = rec.get("worktree")
        self.me = os.getpid()
        self.holder = owner_holder(fam)
        self.cli = claude_cli()
        self.env = {k: v for k, v in os.environ.items() if k not in FIXER_IDENTITY_ENV and k != "AGENTCTL_DETACHED"}
        self.every = knob_red_renew()
        self.poll = knob_bg_poll()
        self.idle_max = knob_owner_idle()
        self.wall = knob_red_fixer_wall()
        self.stall = knob_red_stall()
        self.bid = None
        self.ent = None
        self.turn = int(rec.get("turn") or 0)
        self.names: list[str] = []
        self.empty = 0
        self.beat = 0.0
        self.idle_since = epoch_now()
        self.first = not rec.get("session")
        self.resumed = bool(rec.get("session"))
        self.missing = 0
        self.turn_at = 0.0
        self.handover = False
        self.told: dict[str, Any] = {}  # each red lease's (epoch, session, turn session) last told to the store (owner_tell_leases)
        self.progress = TurnProgress(self.stall, 0.0)
        self.heartbeat = HeldLease(owner_lease(fam), hb["epoch"], hb["secret"], self.holder, OWNER_TTL)  # ONE FIXER LIFECYCLE

    def run(self) -> None:
        """The wrapper's life: its record and signals, the session it adopts, the passes until a stop, the wind-down."""
        self.rec.update(pid=self.me, process_start=process_start(self.me))
        write_json(self.path, self.rec)
        keep_awake()  # the laptop must not idle-sleep past OWNER_TTL under a held heartbeat (host_slept)
        adopt = self._reclaim_session()
        for sg in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
            signal.signal(sg, self._cancelled)
        if adopt:  # the dead wrapper's turn goes on under this one (A RESTART ADOPTS A SESSION STILL AT WORK)
            self._adopt(adopt)
        self._end_handed_turn(adopt)
        write_json(self.path, self.rec)
        self._wind_down(self._loop())

    def _reclaim_session(self) -> Any:
        """A RESTART ADOPTS A SESSION STILL AT WORK (2026-11-03): the dead wrapper's session, mid-turn (a bisect, a
        measure it waits on), goes on under this wrapper -- never stopped and started over (returned). One whose turn is
        over but whose process lingers is stopped: `--resume` of a running session starts a copy beside it; stopped, it
        goes on under its own id."""
        rec = self.rec
        prev = bg_session(self.cli, rec["session"][:8]) if rec.get("session") else None
        adopt = prev if (prev and prev.get("id") and not bg_turn_over(prev, rec.get("transcript"))
                         and owner_leases(self.fam)) else None
        if prev and prev.get("id") and prev.get("pid") and not adopt:
            bg_stop(self.cli, prev["id"])
        return adopt

    def _adopt(self, adopt: Any) -> None:
        t0 = epoch_now()
        self.bid, self.names, self.first, self.resumed, self.turn_at = adopt["id"], list(owner_leases(self.fam)), False, False, t0
        self.progress = TurnProgress(self.stall, t0)
        self.turn = max(self.turn, 1)
        self.rec.update(bg_id=adopt["id"], turn=self.turn)
        self._turn_state(True, t0)
        print(f"{ts()} RED OWNER {self.fam}: adopted its session {adopt['id']}, still at work (turn {self.turn})",
              flush=True)

    def _end_handed_turn(self, adopt: Any) -> None:
        """A turn handed over by a wrapper that lost its heartbeat, ended meanwhile with a verdict: its reds (offered to
        this owner again) end their leases with it, never a new turn over the same ground."""
        handed = self.rec.pop("handed_turn", None)
        text = bg_turn(self.rec.get("transcript") or bg_transcript(self.rec.get("session") or ""))["final"] \
            if handed and not adopt and owner_leases(self.fam) else ""
        if fixer_verdict(text):
            self.names = list(owner_leases(self.fam))
            self._end_turn("", text)
            print(f"{ts()} RED OWNER {self.fam}: turn {handed.get('turn')}, handed over, ended: its verdict to its reds",
                  flush=True)

    def _loop(self) -> str:
        """The passes until one stops the owner: its reason."""
        try:
            stop = None
            while stop is None:
                stop = self._step(epoch_now())
                if stop is None:
                    time.sleep(self.poll)  # wall: waits on and times real processes and threads
            return stop
        except StoreAuthFailed as ex:
            return f"store auth failed: {str(ex)[:300]}"
        except Exception as ex:  # never a dead wrapper with a live session and held leases
            return f"STOPPED: the owner's wrapper failed ({type(ex).__name__}: {str(ex)[:200]})"

    def _wind_down(self, stop: Any) -> None:
        """Its session stopped (or handed over), every lease released, its record ended."""
        fam, rec = self.fam, self.rec
        handed = bool(self.bid and self.handover and in_turn_of(self.ent, rec.get("transcript")))
        if self.bid and not handed:  # A LOST HEARTBEAT HANDS THE TURN OVER: its session
            bg_stop(self.cli, self.bid)  # at work goes on, and the family's next owner adopts it (never started over)
        if handed:  # ... AND ITS VERDICT (issue 20261116T162652-930aa9: the turn's DIAGNOSED, said after this wrapper
            # ended, reached no red, and the same diagnosis was offered again): the next owner adopts the session still
            # at work, or -- its turn over by then -- reads that turn's verdict and ends its reds' leases with it
            rec["handed_turn"] = {"bid": self.bid, "turn": self.turn, "at": ts()}
        self._release(list(owner_leases(fam)), stop)
        self._release_heartbeat(stop)
        rec.update(ended=ts(), verdict=stop, in_turn=False)
        if (read_record(self.path) or {}).get("pid") in (None, self.me):  # a restarted owner's record is its own
            write_json(self.path, rec)
        if stop.startswith("store auth failed"):
            inbox_write("red", f"RED OWNER {fam}: {stop[:300]} (its reds offered again; the next owner starts with a "
                        "fresh store token)", [], [], 1, cwd=self.wt)
        print(f"RED OWNER {fam}: {stop}")

    def _release_heartbeat(self, outcome: Any) -> None:
        try:
            self.st.release(owner_lease(self.fam), self.hb["epoch"], self.hb["secret"], outcome=outcome)
        except StoreUnavailable:
            pass

    def _turn_state(self, in_turn: Any, now: Any) -> None:
        """What the laptop's tick reads to tell an owner at work from an orphan (red_reap_orphans)."""
        self.rec.update(in_turn=in_turn, progress_at=self.progress.at if in_turn else now)
        write_json(self.path, self.rec)

    def _release(self, names: Any, outcome: Any, notes: Any = "") -> None:
        leases = owner_leases(self.fam)
        for n in names:
            x = leases.get(n)
            if x:
                try:
                    self.st.release(n, x["epoch"], x["secret"], outcome=verdict_cut(outcome), notes=notes)
                except StoreUnavailable:  # it lapses by its TTL instead: the red is offered again
                    pass
        owner_drop(self.fam, names)

    def _cancelled(self, signum: Any, _frame: Any) -> None:
        if self.bid:
            bg_stop(self.cli, self.bid)
        self._release(list(owner_leases(self.fam)), "STOPPED: the owner's wrapper was stopped")
        self._release_heartbeat("STOPPED")
        os._exit(143)

    def _end_turn(self, outcome_if_none: Any, text: Any = None) -> str | None:
        """The turn's verdict: told in the inbox, its reds' leases released with it, its session stopped. The stop
        reason when the owner has had OWNER_EMPTY_TURNS turns in a row with none, else None."""
        rec = self.rec
        if text is None:  # THIS turn's answer: a resumed session's transcript also holds the turns before it
            text = bg_turn(rec.get("transcript") or bg_transcript(rec.get("session") or ""))["final"]
        verdict = fixer_verdict(text)
        tail = [l[:300] for l in text.splitlines()[-INBOX_TAIL:]]
        self.empty = 0 if verdict else self.empty + 1
        outcome = verdict or outcome_if_none
        line = owner_turn_line(rec, self.fam, self.turn, outcome)  # kept: a fresh session's handover names it
        inbox_write("red fixer", line, [line], tail, 0 if outcome.startswith("LANDED") else 1, cwd=self.wt)
        self._release(self.names, outcome, "\n".join(tail[-8:]))
        if self.bid:  # THE TURN'S SESSION STOPPED (bg_spent): claude keeps its process on as a spare, and the next
            bg_stop(self.cli, self.bid)  # turn's `--resume` of a running session starts a COPY -- one more per turn
        self.bid, self.missing, self.idle_since = None, 0, epoch_now()  # OWNER_IDLE counts from the turn's END: from
        # its start, a turn longer than OWNER_IDLE left the owner "idle" the moment it ended
        self._turn_state(False, epoch_now())
        return f"ENDED: {self.empty} turns in a row with no verdict" if self.empty >= OWNER_EMPTY_TURNS else None

    def _step(self, now: Any) -> Any:
        """One pass; returns the stop reason, or None to go on. PROOF OF WORK FIRST: a turn whose session ended (done,
        stopped, blocked, gone) or made no progress for RED_STALL ends -- its reds' leases released with that -- before
        any lease is renewed: a living wrapper alone never holds a red."""
        if self.bid is not None:
            outcome = self._watch_turn(now)
            if outcome is not GO_ON:
                return outcome
        if now - self.beat >= self.every:  # THE HEARTBEAT: the owner's lease; its reds' leases by the turn's progress
            outcome = self._beat(now)
            if outcome is not GO_ON:
                return outcome
        stopped = self._stop_file_reason()
        if stopped or self.bid is not None:
            return stopped
        return self._between_turns(now)

    def _watch_turn(self, now: Any) -> Any:
        """The turn's session read: its id and rollovers noted; GO_ON, or the turn's end (done, gone, timed out)."""
        rec = self.rec
        ent = bg_session(self.cli, self.bid)
        self._note_session(ent)
        self.missing = self.missing + 1 if ent is None else 0
        self.ent = ent
        if bg_turn_over(ent, rec.get("transcript")) or self.missing >= 3:  # the turn ended (done, stopped,
            # blocked; or waiting with its verdict said, bg_turn_over) or the session is gone
            failure = bg_failure(self.bid)
            return self._end_turn(f"ended with no LANDED / GAVE UP line (session {self.bid}: "
                                  f"{(ent or {}).get('state') or 'gone from `claude agents`'}"
                                  + (f": {failure}" if failure else "") + ")")
        if now - self.turn_at > self.wall:
            bg_stop(self.cli, self.bid)
            return self._end_turn(f"TIMED OUT after {int(self.wall // 60)} min (maximum wall time of a turn)")
        return GO_ON

    def _note_session(self, ent: Any) -> None:
        """The turn's session id, and a rollover's new session, named in the record."""
        rec = self.rec
        if ent and ent.get("sessionId") and rec.get("session") != ent["sessionId"]:
            session_seen(rec, ent["sessionId"])
            write_json(self.path, rec)
            bind_fixer_session(rec["session"], self.path)  # the session is a fixer's: ~/bin refuses its commits

    def _beat(self, now: Any) -> Any:
        """THE HEARTBEAT, once: the turn's progress judged, the owner's lease and its reds' leases renewed. GO_ON, or the
        owner's end; a store that refuses the token is given a fresh one once, one that does not answer is asked again."""
        try:
            stalled = self._judge_progress(now) if self.bid is not None else GO_ON
            if stalled is not GO_ON:
                return stalled
            lost = self._renew_heartbeat(now)
            if lost is not GO_ON:
                return lost
            self._renew_red_leases(now)
            self.beat = now
        except (StoreAuthFailed, StoreUnavailable) as e:
            return store_failure_stop(e, self.env, "beat") or GO_ON
        return GO_ON

    def _judge_progress(self, now: Any) -> Any:
        rec = self.rec
        _mark, moved, stalled = self.progress.judge(rec.get("transcript") or rec.get("log"), self.bid, self.wt,
                                                    self.ent, rec.get("transcript"), now, self.every)
        if moved:
            self._turn_state(True, now)
        elif stalled:
            bg_stop(self.cli, self.bid)
            return self._end_turn(f"STALLED: no progress (transcript, worktree) for {fmt_age(self.stall)}")
        return GO_ON

    def _renew_heartbeat(self, now: Any) -> Any:
        """THE HEARTBEAT, kept as every lease of either kind is (HeldLease): one that merely LAPSED -- the laptop
        asleep, the store out of reach past OWNER_TTL -- and that nobody took is taken again by this living wrapper; a
        revoked (orphaned), released or re-taken one ends it (why, from the lease itself: 2026-11-19, the bare line
        hid pawel-mbp's revokes)."""
        gone = self.heartbeat.renew(self.st, f"beat {int(now)}")
        self.hb = dict(self.hb, epoch=self.heartbeat.epoch, secret=self.heartbeat.secret)
        if not gone:
            return GO_ON
        self.handover = True
        return f"{OWNER_HEARTBEAT_LOST} ({str(gone[1])[:300]})"

    def _renew_red_leases(self, now: Any) -> None:
        """THE WRAPPER JUDGES PROGRESS (RED_STALL), so its reds' leases renew with each beat of a turn not stalled (and
        between turns). Renewed only on a new mark, they lapsed after OWNER_TTL (15 min) while the turn was inside its
        30 min stall budget: "lease expired" every 15-30 min (2026-11-03, convergence red 8fbdbe0d5a86), each a crash
        for the breaker and a re-offer."""
        beat = f"{self.progress.mark or 'held'} beat {int(now)}"
        leases = owner_leases(self.fam)
        lost = []
        for n, x in leases.items():  # a lapsed one taken again (HeldLease); cleared, released, revoked: dropped
            hl = HeldLease(n, x["epoch"], x["secret"], self.holder, OWNER_TTL)
            if hl.renew(self.st, beat):
                lost.append(n)
            elif hl.retaken:
                owner_hand(self.fam, n, {"epoch": hl.epoch, "secret": hl.secret})
                leases[n] = dict(x, epoch=hl.epoch, secret=hl.secret)
        if lost:  # no longer the owner's
            owner_drop(self.fam, lost)
        owner_tell_leases(self.st, {n: x for n, x in leases.items() if n not in lost}, self.told, self.bid, self.wt,
                          self.me, self.rec)

    def _stop_file_reason(self) -> str | None:
        """RED-STOP.md in the worktree stops the owner: its first line, else None."""
        stop_file = os.path.join(self.wt, "RED-STOP.md")
        if not os.path.exists(stop_file):
            return None
        try:
            with open(stop_file) as f:
                return "STOPPED: " + f.readline().strip()
        except OSError:
            return None

    def _between_turns(self, now: Any) -> str | None:
        """Between turns: a new turn when the owner holds reds, else idle (the stop reason once it has been idle long
        enough). None to go on."""
        held = list(owner_leases(self.fam))
        if not held:
            return f"IDLE: no red of {self.fam} for {fmt_age(self.idle_max)}" if now - self.idle_since >= self.idle_max else None
        self.idle_since = now
        self._pick_session()
        prompt = owner_turn_prompt(self.fam, self.rec, self.st, held, self.first, self.resumed,
                                   owner_rollover(self.st, self.fam, self.rec, held))
        if prompt is None:  # its reds closed meanwhile
            owner_drop(self.fam, held)
            return None
        return self._start_turn(held, prompt, now)

    def _pick_session(self) -> None:
        """A SESSION WITH NO CONVERSATION CANNOT BE RESUMED (fixer.bg_resumable): the last one that holds one, else a
        fresh session."""
        rec = self.rec
        resume = owner_resume_target(rec)
        if resume == rec.get("session"):
            return
        print(f"{ts()} RED OWNER {self.fam}: session {str(rec.get('session'))[:8]} holds no conversation: "
              + (f"resuming {resume[:8]}, the last one that does" if resume else "a fresh session"), flush=True)
        rec.update(session=resume, transcript=bg_transcript(resume) if resume else None)
        self.first, self.resumed = self.first or not resume, self.resumed and bool(resume)

    def _start_turn(self, held: Any, prompt: Any, now: Any) -> str | None:
        """`claude --bg` started on the prompt (resuming the owner's session): the turn begun, or the stop reason."""
        argv = [self.cli, "--bg", prompt, "--permission-mode", interactive_mode(), "--permission-prompts", "none",
                "-n", f"red owner · {self.fam}", "--disallowedTools", "Bash(git push:*)", "Bash(*--no-verify*)",
                "Bash(*PREPUSH_SKIP*)"]
        if self.rec.get("session"):
            argv += ["--resume", self.rec["session"]]
        bid, why = bg_start(argv, self.wt, self.env)
        if not bid:
            self._release(held, f"NOT STARTED: `claude --bg` {why}")
            return f"NOT STARTED: `claude --bg` {why}"
        self.bid, self.turn, self.names, self.first, self.resumed = bid, self.turn + 1, held, False, False
        self.turn_at, self.missing, self.progress = now, 0, TurnProgress(self.stall, now)
        self.rec.update(bg_id=bid, turn=self.turn)
        self._turn_state(True, now)
        return None
