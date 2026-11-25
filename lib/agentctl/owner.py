# agentctl, part `owner`: long-lived red family owners: an owner spawned in its worktree, its turns and their prompts, its heartbeat
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import json
import os
import re
import signal
import subprocess
import sys
import time
from typing import Any
from agentctl.core import HOME, env_num, fmt_age, git, git_ok, remote, spawn_detached
from agentctl.timefmt import ts
from agentctl.statefile import read_record, write_json
from agentctl.selfcode import TOOL_PATH
from agentctl.waitingdir import waiting_state
from agentctl.claudefiles import bg_transcript, fixer_verdict
from agentctl.procs import proc_alive, process_start, socket_host
from agentctl.inboxentry import inbox_write
from agentctl.fixersession import (
    BG_POLL, RED_STALL, bg_failure, bg_resumable, bg_session, bg_start, bg_stop, bg_turn, bg_turn_over,
    store_reauth)
from agentctl.worktrees import worktree_exclude
from agentctl.fixerenv import fixer_env
from agentctl.inbox import INBOX_TAIL
from agentctl.dispatchblock import claude_cli, interactive_mode
from agentctl.store import StoreAuthFailed, StoreUnavailable
from agentctl.redrecord import red_active, red_what
from agentctl.leases import lease_meta
from agentctl.redreplay import sync_brief
from agentctl.owners import OWNER_TTL, owner_dir, owner_lease, owner_record_path
from agentctl.redask import red_decision
from agentctl.laptoprole import laptop_role_standby
from agentctl.fixer import FIXER_IDENTITY_ENV, fixer_prompt
from agentctl.red import RED_FIXER_WALL, RED_RENEW_EVERY
from agentctl.redclaim import bind_fixer_session
from agentctl.redcrash import keep_awake, verdict_cut
from agentctl.fixerlife import HeldLease, TurnProgress
from agentctl.ownerleases import owner_drop, owner_hand, owner_holder, owner_leases, owner_tell_leases
from agentctl.sessionroll import owner_rollover, owner_turn_line, session_seen


OWNER_MAX = 7 * 86400        # ...and is held at most this long in one spell (a restart takes it again)
OWNER_IDLE = 6 * 3600        # no red held this long: the owner ends ($AGENTCTL_OWNER_IDLE)
OWNER_EMPTY_TURNS = 2        # turns in a row ending with no verdict line: the owner ends (its reds are offered again)
OWNER_NOTEBOOK = "RED-NOTEBOOK.md"


def owner_worktree(repo, fam, rec):
    """The owner's ONE worktree: the recorded one while it exists, else <clone>-wt-owner-<family> (created off
    origin/main, or its branch taken up again). The notebook and RED-INBOX.md are excluded from commits. It is LOCKED
    (`git worktree lock`), which `cleanup` keeps: an owner's tree is clean and its HEAD an ancestor of main between
    commits, so upkeep's hourly cleanup removed it -- once mid-turn (outside-gate turn 67, 2026-11-22T04:01Z: "my
    worktree was deleted mid-turn"), and at 11-21 12:39Z, 19:46Z, 11-22 00:12Z and 04:27Z between turns."""
    if rec.get("worktree") and os.path.isdir(rec["worktree"]):
        lock_owner_worktree(repo, fam, rec["worktree"])
        return rec["worktree"]
    _d, clone, _w = waiting_state(repo)
    slug_ = re.sub(r"[^A-Za-z0-9_.-]", "-", fam)
    wt, branch = f"{clone}-wt-owner-{slug_}", f"owner-{slug_}"
    if not os.path.isdir(wt):
        git(repo, "worktree", "prune", check=False)
        git(repo, "fetch", "--quiet", remote(), "main", check=False)
        has = git_ok(repo, "rev-parse", "--verify", "-q", f"refs/heads/{branch}")
        r = git(repo, "worktree", "add", "-q", *([wt, branch] if has else ["-b", branch, wt, f"{remote()}/main"]),
                check=False)
        if r.returncode != 0:
            return None
    lock_owner_worktree(repo, fam, wt)
    try:
        worktree_exclude(wt, (OWNER_NOTEBOOK, "RED-INBOX.md", "RED-NUDGE.md", "RED-STOP.md"))
    except OSError:
        pass
    return wt


def lock_owner_worktree(repo, fam, wt):
    """Lock the owner's worktree against `git worktree remove` / prune (idempotent: an already locked tree refuses
    the lock, which is fine)."""
    git(repo, "worktree", "lock", "--reason", f"agentctl red owner {fam}: its one worktree", wt, check=False)


# ONE START PER FAMILY (2026-11-21 22:12:33Z and 22:12:36Z): the convergence owner, ended ORPHANED, was started twice
# 3 s apart (pids 6921, 7139) -- two of its reds re-offered in two dispatcher passes, each finding no live heartbeat
# because the first wrapper had not taken it yet; the second wrapper ended DUPLICATE. The dispatcher now takes the
# owner's heartbeat lease ITSELF before it starts the wrapper, atomically in the store, and hands it over
# ($AGENTCTL_OWNER_HEARTBEAT): a later pass finds the owner alive and hands it the red; a pass that loses the take is a
# no-op that hands its leases to the owner starting.
OWNER_HEARTBEAT_ENV = "AGENTCTL_OWNER_HEARTBEAT"


def owner_meta(fam, wt, pid, notebook):
    return {"kind": "owner", "family": fam, "worktree": wt, "pid": pid, "notebook": notebook, "host": socket_host()}


def owner_spawn(store, o_r, repo, fam, off, took, extra=()):
    """Start (or restart, after it died) the family's owner: its worktree and notebook kept, the taken leases handed
    to it, its heartbeat taken for it (ONE START PER FAMILY), its wrapper detached. (ok, message)."""
    rec = read_record(owner_record_path(fam)) or {"family": fam, "restarts": -1}
    wt = owner_worktree(repo, fam, rec)
    if not wt:
        return False, f"RED OWNER {fam}: NOT STARTED: no worktree"
    for x in [{"name": off["lease"], **took}] + [dict(x) for x in extra]:
        owner_hand(fam, x["name"], x)
    nb = os.path.join(wt, OWNER_NOTEBOOK)
    hb = store.acquire(owner_lease(fam), owner_holder(fam), OWNER_TTL, OWNER_MAX, progress=f"beat {int(time.time())}",
                       meta=owner_meta(fam, wt, None, nb))
    if "held" in hb:  # another pass started it a moment ago: the red is the starting owner's next turn
        return True, f"RED OWNER {fam}: already starting ({hb['held']['holder']}): red {off['lease']} handed to it"
    if not os.path.exists(nb):
        with open(nb, "w") as f:
            f.write(f"# {fam} red family: the owner's notebook\n\nKept by the family's owner session across reds and "
                    "restarts: reproducers, hypotheses tried (and how they ended), what is left.\n")
    restarted = bool(rec.get("started"))
    # A RESTART CONTINUES (2026-11-03): the turn count, the session (rec["session"], resumed or adopted by the wrapper),
    # the worktree and the notebook go on; the last spell's end is kept for the resumed session's first prompt
    rec.update(family=fam, worktree=wt, notebook=nb, repo=repo, o_r=o_r, started=ts(), ended=None, verdict=None,
               last_end=rec.get("verdict") or rec.get("last_end"), turn=int(rec.get("turn") or 0), bg_id=None,
               pid=None, in_turn=False, progress_at=None,
               restarts=int(rec.get("restarts", -1)) + 1, log=os.path.join(owner_dir(fam), "owner.log"))
    write_json(owner_record_path(fam), rec)
    try:  # a STOP left by an earlier spell would stop this one at its first pass
        os.unlink(os.path.join(wt, "RED-STOP.md"))
    except OSError:
        pass
    env = fixer_env(o_r, f"owner-{fam}")
    env[OWNER_HEARTBEAT_ENV] = json.dumps({"epoch": hb["epoch"], "secret": hb["secret"]})
    try:
        with open(rec["log"], "a") as logf:
            p = spawn_detached([sys.executable, TOOL_PATH, "red", "owner", "--family", fam, "--repo", repo],
                               stdin=subprocess.DEVNULL, stdout=logf, stderr=logf, start_new_session=True, cwd=wt,
                               env=env)
    except OSError as e:  # never a heartbeat held for a wrapper that never ran
        store.release(owner_lease(fam), hb["epoch"], hb["secret"], outcome=f"NOT STARTED: {e}")
        return False, f"RED OWNER {fam}: NOT STARTED: {e}"
    rec.update(pid=p.pid, process_start=process_start(p.pid))
    write_json(owner_record_path(fam), rec)
    pic = off["picture"]
    line = (f"RED OWNER {fam}: {'RESTARTED (it had died)' if restarted else 'STARTED'} pid {p.pid} in "
            f"{wt.replace(HOME, '~')} for {pic.get('workflow')} #{(pic.get('run') or {}).get('number')}")
    inbox_write("red", line, [], [], 0, cwd=wt)
    return True, line


def owner_resume_target(rec):
    """The session the owner's next turn resumes: its recorded one when that holds a conversation (fixer.bg_resumable),
    else the last one that did (`resumable`, kept here), else None -- a fresh session, never a resume claude refuses."""
    for sid in (rec.get("session"), rec.get("resumable")):
        if sid and bg_resumable(sid):
            rec["resumable"] = sid
            return sid
    rec.pop("resumable", None)
    return None


def owner_turn_prompt(fam, rec, st, names, first, resumed=False, handover=None):
    """The turn's prompt: the first red's task (fixer_prompt) in the owner's voice, every red of the turn listed. A
    resumed session (its wrapper restarted) is told to continue, never to start over; a fresh one past its predecessor's
    context limit (sessionroll) gets the handover instead."""
    active = red_active(st)
    its = [active[n.split("/", 1)[1]] for n in names if n.split("/", 1)[1] in active]
    reds = [f"{red_what(i['data'])} (red {i['data'].get('key')}): {str(i['data'].get('line') or '')[:160]}" for i in its]
    if not its:
        return None
    d = its[0]["data"]
    u = {"workflow": d.get("workflow"), "job": d.get("job") or (d.get("jobs") or ["?"])[0], "jobs": d.get("jobs"),
         "kind": d.get("kind") or "test", "line": d.get("line"), "run": d.get("last_run") or {}, "key": d.get("key"),
         "refuted": d.get("refuted"), "predicts": d.get("predicts"), "flake": d.get("red_kind") == "flake",
         "diagnosis": d.get("diagnosis"), "prior_work": d.get("prior_work"), "old_note": d.get("old_note"),
         "decision": red_decision(d)}
    return fixer_prompt(u, rec["worktree"], sync_brief(), handover or owner_turn_note(rec, first, resumed),
                        owner={"family": fam, "notebook": rec["notebook"], "reds": reds})


def owner_turn_note(rec: dict[str, Any], first: bool, resumed: bool) -> str | None:
    """The prompt's note: a resumed session continues, a first turn has none, a next turn reads its inbox."""
    if resumed:
        return ("RESUMED AFTER A RESTART, NEVER START OVER: your wrapper restarted (" + str(rec.get("last_end") or
                "it had died")[:200] + "); you are the SAME session, in the same worktree, with your notebook and "
                "your context: continue from where you left off (your notebook's `what is left`, your commits, the CI "
                "runs you started). The reds above are yours again; read RED-INBOX.md, then go on.")
    if first:
        return None
    return ("NEXT TURN: the reds above are new or offered to you again (a previous verdict did not "
            "clear them): read RED-INBOX.md and your notebook, then go on.")


OWNER_HEARTBEAT_LOST = "STOPPED: the owner's heartbeat lease was lost"


def in_turn_of(s, transcript):
    """The wrapper's turn's session is still at work (its last `claude agents` row, and its transcript, not over)."""
    ent = s.get("ent")
    return bool(ent and ent.get("id") and not bg_turn_over(ent, transcript))


def red_owner(a, st):
    """THE OWNER'S WRAPPER (detached; `agentctl red owner --family F`): its heartbeat lease, its reds' leases renewed
    by the turn's progress, one session resumed turn after turn, each turn's leases released with the turn's verdict.
    A turn is bounded as a fixer's is: RED_FIXER_WALL, and a session missing from `claude agents` three polls running
    has ended. Whatever raises, the session is stopped and every lease released (never a live session nobody holds)."""
    fam = a.family
    path = owner_record_path(fam)
    rec = read_record(path) or {}
    wt = rec.get("worktree")
    me = os.getpid()
    standby = laptop_role_standby(st)
    if standby:  # ONE MACHINE HOLDS THE LAPTOP ROLE (server.py): a standby machine runs no owner
        print(f"RED OWNER {fam}: NOT STARTED ({standby})")
        return 0
    holder = owner_holder(fam)
    hb = owner_take_heartbeat(st, fam, holder, owner_meta(fam, wt, me, rec.get("notebook")))
    if "held" in hb:
        print(f"RED OWNER {fam}: DUPLICATE ({hb['held']['holder']} is alive)")
        return 0
    if "lost" in hb:
        print(f"RED OWNER {fam}: NOT STARTED: the heartbeat taken for it was lost ({str(hb['lost'])[:300]})")
        return 0
    rec.update(pid=me, process_start=process_start(me))
    write_json(path, rec)
    keep_awake()  # the laptop must not idle-sleep past OWNER_TTL under a held heartbeat (host_slept)
    cli = claude_cli()
    prev = bg_session(cli, rec["session"][:8]) if rec.get("session") else None
    # A RESTART ADOPTS A SESSION STILL AT WORK (2026-11-03): the dead wrapper's session, mid-turn (a bisect, a measure
    # it waits on), goes on under this wrapper -- never stopped and started over. One whose turn is over but whose
    # process lingers is stopped: `--resume` of a running session starts a copy beside it; stopped, it goes on under
    # its own id.
    adopt = prev if (prev and prev.get("id") and not bg_turn_over(prev, rec.get("transcript"))
                     and owner_leases(fam)) else None
    if prev and prev.get("id") and prev.get("pid") and not adopt:
        bg_stop(cli, prev["id"])
    env = {k: v for k, v in os.environ.items() if k not in FIXER_IDENTITY_ENV and k != "AGENTCTL_DETACHED"}  # an agent
    every = env_num("AGENTCTL_RED_RENEW", RED_RENEW_EVERY)
    poll = env_num("AGENTCTL_BG_POLL", BG_POLL)
    idle_max = env_num("AGENTCTL_OWNER_IDLE", OWNER_IDLE)
    wall = env_num("AGENTCTL_RED_FIXER_WALL", RED_FIXER_WALL)
    stall = env_num("AGENTCTL_RED_STALL", RED_STALL)
    s = {"bid": None, "turn": int(rec.get("turn") or 0), "names": [], "empty": 0, "beat": 0.0,
         "idle_since": time.time(), "first": not rec.get("session"), "resumed": bool(rec.get("session")),
         "missing": 0, "turn_at": 0.0, "prog": TurnProgress(stall, 0.0), "hb": hb, "meta": {},
         "hbl": HeldLease(owner_lease(fam), hb["epoch"], hb["secret"], holder, OWNER_TTL)}  # ONE FIXER LIFECYCLE

    def turn_state(in_turn, now):
        """What the laptop's tick reads to tell an owner at work from an orphan (red_reap_orphans)."""
        rec.update(in_turn=in_turn, progress_at=s["prog"].at if in_turn else now)
        write_json(path, rec)

    def release(names, outcome, notes=""):
        leases = owner_leases(fam)
        for n in names:
            x = leases.get(n)
            if x:
                try:
                    st.release(n, x["epoch"], x["secret"], outcome=verdict_cut(outcome), notes=notes)
                except StoreUnavailable:  # it lapses by its TTL instead: the red is offered again
                    pass
        owner_drop(fam, names)

    def cancelled(signum, _frame):
        if s["bid"]:
            bg_stop(cli, s["bid"])
        release(list(owner_leases(fam)), "STOPPED: the owner's wrapper was stopped")
        try:
            st.release(owner_lease(fam), s["hb"]["epoch"], s["hb"]["secret"], outcome="STOPPED")
        except StoreUnavailable:
            pass
        os._exit(143)
    for sg in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        signal.signal(sg, cancelled)

    def end_turn(outcome_if_none, text=None):
        if text is None:  # THIS turn's answer: a resumed session's transcript also holds the turns before it
            text = bg_turn(rec.get("transcript") or bg_transcript(rec.get("session") or ""))["final"]
        verdict = fixer_verdict(text)
        tail = [l[:300] for l in text.splitlines()[-INBOX_TAIL:]]
        s["empty"] = 0 if verdict else s["empty"] + 1
        outcome = verdict or outcome_if_none
        line = owner_turn_line(rec, fam, s["turn"], outcome)  # kept: a fresh session's handover names it
        inbox_write("red fixer", line, [line], tail, 0 if outcome.startswith("LANDED") else 1, cwd=wt)
        release(s["names"], outcome, "\n".join(tail[-8:]))
        if s["bid"]:  # THE TURN'S SESSION STOPPED (bg_spent): claude keeps its process on as a spare, and the next
            bg_stop(cli, s["bid"])  # turn's `--resume` of a running session starts a COPY -- one more per turn
        s["bid"], s["missing"], s["idle_since"] = None, 0, time.time()  # OWNER_IDLE counts from the turn's END: from
        # its start, a turn longer than OWNER_IDLE left the owner "idle" the moment it ended
        turn_state(False, time.time())
        return f"ENDED: {s['empty']} turns in a row with no verdict" if s["empty"] >= OWNER_EMPTY_TURNS else None

    def step(now):
        """One pass; returns the stop reason, or None to go on. PROOF OF WORK FIRST: a turn whose session ended (done,
        stopped, blocked, gone) or made no progress for RED_STALL ends -- its reds' leases released with that -- before
        any lease is renewed: a living wrapper alone never holds a red."""
        if s["bid"] is not None:
            ent = bg_session(cli, s["bid"])
            if ent and ent.get("sessionId") and rec.get("session") != ent["sessionId"]:
                session_seen(rec, ent["sessionId"])  # its id, and a rollover's new session named
                write_json(path, rec)
                bind_fixer_session(rec["session"], path)  # the session is a fixer's: ~/bin refuses its commits
            s["missing"] = s["missing"] + 1 if ent is None else 0
            s["ent"] = ent
            if bg_turn_over(ent, rec.get("transcript"), now) or s["missing"] >= 3:  # the turn ended (done, stopped,
                # blocked; or waiting with its verdict said, bg_turn_over) or the session is gone
                failure = bg_failure(s["bid"])
                why = (f"ended with no LANDED / GAVE UP line (session {s['bid']}: "
                       f"{(ent or {}).get('state') or 'gone from `claude agents`'}"
                       + (f": {failure}" if failure else "") + ")")
                return end_turn(why)
            if now - s["turn_at"] > wall:
                bg_stop(cli, s["bid"])
                return end_turn(f"TIMED OUT after {int(wall // 60)} min (maximum wall time of a turn)")
        if now - s["beat"] >= every:  # THE HEARTBEAT: the owner's lease; its reds' leases by the turn's progress
            try:
                if s["bid"] is not None:
                    _mark, moved, stalled = s["prog"].judge(rec.get("transcript") or rec.get("log"), s["bid"], wt,
                                                            s.get("ent"), rec.get("transcript"), now, every)
                    if moved:
                        turn_state(True, now)
                    elif stalled:
                        bg_stop(cli, s["bid"])
                        return end_turn(f"STALLED: no progress (transcript, worktree) for {fmt_age(stall)}")
                # THE HEARTBEAT, kept as every lease of either kind is (HeldLease): one that merely LAPSED -- the laptop
                # asleep, the store out of reach past OWNER_TTL -- and that nobody took is taken again by this living
                # wrapper; a revoked (orphaned), released or re-taken one ends it
                gone = s["hbl"].renew(st, f"beat {int(now)}")
                s["hb"] = dict(s["hb"], epoch=s["hbl"].epoch, secret=s["hbl"].secret)
                if gone:  # why it was lost, from the lease itself (2026-11-19: the bare line hid pawel-mbp's revokes)
                    s["handover"] = True
                    return f"{OWNER_HEARTBEAT_LOST} ({str(gone[1])[:300]})"
                # THE WRAPPER JUDGES PROGRESS (RED_STALL, above), so its reds' leases renew with each beat of a turn
                # not stalled (and between turns). Renewed only on a new mark, they lapsed after OWNER_TTL (15 min)
                # while the turn was inside its 30 min stall budget: "lease expired" every 15-30 min (2026-11-03,
                # convergence red 8fbdbe0d5a86), each a crash for the breaker and a re-offer
                beat = f"{s['prog'].mark or 'held'} beat {int(now)}"
                leases = owner_leases(fam)
                lost = []
                for n, x in leases.items():  # a lapsed one taken again (HeldLease); cleared, released, revoked: dropped
                    hl = HeldLease(n, x["epoch"], x["secret"], holder, OWNER_TTL)
                    if hl.renew(st, beat):
                        lost.append(n)
                    elif hl.retaken:
                        owner_hand(fam, n, {"epoch": hl.epoch, "secret": hl.secret})
                        leases[n] = dict(x, epoch=hl.epoch, secret=hl.secret)
                if lost:  # no longer the owner's
                    owner_drop(fam, lost)
                owner_tell_leases(st, {n: x for n, x in leases.items() if n not in lost}, s, wt, me, rec)
                s["beat"] = now
            except StoreAuthFailed as e:  # never "asked again" forever: a fresh store token once, else the owner ends
                if not store_reauth():
                    return f"store auth failed: {e}"
                env["AGENTCTL_STORE_TOKEN"] = os.environ["AGENTCTL_STORE_TOKEN"]
                print(f"{ts()} the store refused the token ({e}): a fresh store token taken", flush=True)
            except StoreUnavailable as e:
                print(f"{ts()} the store did not answer ({e}): asked again at the next beat", flush=True)
        stop_file = os.path.join(wt, "RED-STOP.md")
        if os.path.exists(stop_file):
            try:
                with open(stop_file) as f:
                    return "STOPPED: " + f.readline().strip()
            except OSError:
                pass
        held = list(owner_leases(fam))
        if s["bid"] is None:  # between turns: a new turn when the owner holds reds, else idle
            if not held:
                return f"IDLE: no red of {fam} for {fmt_age(idle_max)}" if now - s["idle_since"] >= idle_max else None
            s["idle_since"] = now
            resume = owner_resume_target(rec)
            if resume != rec.get("session"):  # A SESSION WITH NO CONVERSATION CANNOT BE RESUMED (fixer.bg_resumable)
                print(f"{ts()} RED OWNER {fam}: session {str(rec.get('session'))[:8]} holds no conversation: "
                      + (f"resuming {resume[:8]}, the last one that does" if resume else "a fresh session"), flush=True)
                rec.update(session=resume, transcript=bg_transcript(resume) if resume else None)
                s["first"], s["resumed"] = s["first"] or not resume, s["resumed"] and bool(resume)
            prompt = owner_turn_prompt(fam, rec, st, held, s["first"], s["resumed"], owner_rollover(st, fam, rec, held))
            if prompt is None:  # its reds closed meanwhile
                owner_drop(fam, held)
                return None
            argv = [cli, "--bg", prompt, "--permission-mode", interactive_mode(), "--permission-prompts", "none",
                    "-n", f"red owner · {fam}", "--disallowedTools", "Bash(git push:*)", "Bash(*--no-verify*)",
                    "Bash(*PREPUSH_SKIP*)"]
            if rec.get("session"):
                argv += ["--resume", rec["session"]]
            bid, why = bg_start(argv, wt, env)
            if not bid:
                release(held, f"NOT STARTED: `claude --bg` {why}")
                return f"NOT STARTED: `claude --bg` {why}"
            s.update(bid=bid, turn=s["turn"] + 1, names=held, first=False, resumed=False, turn_at=now, missing=0,
                     prog=TurnProgress(stall, now))
            rec.update(bg_id=bid, turn=s["turn"])
            turn_state(True, now)
            return None
        return None

    if adopt:  # the dead wrapper's turn goes on under this one (A RESTART ADOPTS A SESSION STILL AT WORK)
        t0 = time.time()
        s.update(bid=adopt["id"], names=list(owner_leases(fam)), first=False, resumed=False, turn_at=t0,
                 prog=TurnProgress(stall, t0), turn=max(s["turn"], 1))
        rec.update(bg_id=adopt["id"], turn=s["turn"])
        turn_state(True, t0)
        print(f"{ts()} RED OWNER {fam}: adopted its session {adopt['id']}, still at work (turn {s['turn']})", flush=True)
    handed = rec.pop("handed_turn", None)
    text = bg_turn(rec.get("transcript") or bg_transcript(rec.get("session") or ""))["final"] \
        if handed and not adopt and owner_leases(fam) else ""
    if fixer_verdict(text):  # the turn handed over ended meanwhile with a verdict: its reds (offered to this owner
        s["names"] = list(owner_leases(fam))  # again) end their leases with it, never a new turn over the same ground
        end_turn("", text)
        print(f"{ts()} RED OWNER {fam}: turn {handed.get('turn')}, handed over, ended: its verdict to its reds",
              flush=True)
    write_json(path, rec)
    stop = None
    try:
        while stop is None:
            stop = step(time.time())
            if stop is None:
                time.sleep(poll)
    except StoreAuthFailed as ex:
        stop = f"store auth failed: {str(ex)[:300]}"
    except Exception as ex:  # never a dead wrapper with a live session and held leases
        stop = f"STOPPED: the owner's wrapper failed ({type(ex).__name__}: {str(ex)[:200]})"
    handed = bool(s["bid"] and s.get("handover") and in_turn_of(s, rec.get("transcript")))
    if s["bid"] and not handed:  # A LOST HEARTBEAT HANDS THE TURN OVER: its session
        bg_stop(cli, s["bid"])  # at work goes on, and the family's next owner adopts it (never started over)
    if handed:  # ... AND ITS VERDICT (issue 20261116T162652-930aa9: the turn's DIAGNOSED, said after this wrapper ended,
        # reached no red, and the same diagnosis was offered again): the next owner adopts the session still at work,
        # or -- its turn over by then -- reads that turn's verdict and ends its reds' leases with it
        rec["handed_turn"] = {"bid": s["bid"], "turn": s["turn"], "at": ts()}
    release(list(owner_leases(fam)), stop)
    try:
        st.release(owner_lease(fam), s["hb"]["epoch"], s["hb"]["secret"], outcome=stop)
    except StoreUnavailable:
        pass
    rec.update(ended=ts(), verdict=stop, in_turn=False)
    if (read_record(path) or {}).get("pid") in (None, me):  # a restarted owner's record is its own, never overwritten
        write_json(path, rec)
    if stop.startswith("store auth failed"):
        inbox_write("red", f"RED OWNER {fam}: {stop[:300]} (its reds offered again; the next owner starts with a fresh "
                    "store token)", [], [], 1, cwd=wt)
    print(f"RED OWNER {fam}: {stop}")
    return 0


def owner_take_heartbeat(st, fam, holder, meta):
    """The wrapper's heartbeat lease: the one its dispatcher took for it ($AGENTCTL_OWNER_HEARTBEAT, ONE START PER
    FAMILY), still held, its meta made the wrapper's -- else {'lost': why}; with none handed over (`red owner` run by
    hand), taken now -- {'held': ...} when another owner holds it."""
    handed = os.environ.pop(OWNER_HEARTBEAT_ENV, None)
    if not handed:
        return st.acquire(owner_lease(fam), holder, OWNER_TTL, OWNER_MAX, progress=f"beat {int(time.time())}",
                          meta=meta)
    x = json.loads(handed)
    hl = HeldLease(owner_lease(fam), x["epoch"], x["secret"], holder, OWNER_TTL)
    gone = hl.renew(st, f"beat {int(time.time())}")
    if gone:
        return {"lost": gone[1]}
    st.set_meta(owner_lease(fam), hl.epoch, hl.secret, meta)
    return {"epoch": hl.epoch, "secret": hl.secret}


def owner_still_ending(store, fam, t):
    """Why the family's next owner waits, or None: its last wrapper lost its heartbeat less than OWNER_TTL ago and its
    process still runs (it exits at its next beat; a new one now would race it for the record and the leases)."""
    rec = read_record(owner_record_path(fam)) or {}
    if not rec.get("pid") or rec.get("ended") or not proc_alive(rec["pid"], rec.get("process_start")):
        return None
    gone = float(lease_meta(store.get(owner_lease(fam))).get("released_at") or 0)
    if not gone or t - gone > OWNER_TTL:
        return None
    return f"its owner's last wrapper (pid {rec['pid']}) lost its heartbeat and is ending: the next owner starts after it"
