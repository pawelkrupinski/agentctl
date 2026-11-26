# agentctl, part `owner`: long-lived red family owners: an owner spawned in its worktree, its turns and their prompts, its heartbeat
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from agentctl.clock import epoch_now
import json
import os
import re
import subprocess
import sys
from typing import Any
from contextlib import suppress
from agentctl.core import git, git_ok, remote, spawn_detached, tilde
from agentctl.timefmt import ts
from agentctl.statefile import read_record, write_json, note_failure
from agentctl.selfcode import TOOL_PATH
from agentctl.waitingdir import waiting_state
from agentctl.procs import process_start, record_alive, socket_host
from agentctl.inboxentry import inbox_write
from agentctl.fixersession import bg_resumable
from agentctl.worktrees import worktree_exclude
from agentctl.fixerenv import fixer_env
from agentctl.redrecord import red_active, red_what
from agentctl.leases import lease_meta
from agentctl.redland import sync_brief
from agentctl.owners import OWNER_TTL, owner_dir, owner_lease, owner_record_path
from agentctl.redask import red_decision
from agentctl.fixerprompt import fixer_prompt
from agentctl.fixerlife import HeldLease
from agentctl.ownerleases import owner_hand, owner_holder


OWNER_MAX = 7 * 86400        # ...and is held at most this long in one spell (a restart takes it again)
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
    except OSError as e:
        note_failure("worktree exclude not written", e)
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


def _ensure_notebook(nb: Any, fam: Any) -> None:
    """The family's notebook file, created once."""
    if not os.path.exists(nb):
        with open(nb, "w") as f:
            f.write(f"# {fam} red family: the owner's notebook\n\nKept by the family's owner session across reds and "
                    "restarts: reproducers, hypotheses tried (and how they ended), what is left.\n")


def _owner_started_line(fam: Any, restarted: Any, pid: Any, wt: Any, pic: Any) -> str:
    return (f"RED OWNER {fam}: {'RESTARTED (it had died)' if restarted else 'STARTED'} pid {pid} in "
            f"{tilde(wt)} for {pic.get('workflow')} #{(pic.get('run') or {}).get('number')}")


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
    hb = store.acquire(owner_lease(fam), owner_holder(fam), OWNER_TTL, OWNER_MAX, progress=f"beat {int(epoch_now())}",
                       meta=owner_meta(fam, wt, None, nb))
    if "held" in hb:  # another pass started it a moment ago: the red is the starting owner's next turn
        return True, f"RED OWNER {fam}: already starting ({hb['held']['holder']}): red {off['lease']} handed to it"
    _ensure_notebook(nb, fam)
    restarted = bool(rec.get("started"))
    # A RESTART CONTINUES (2026-11-03): the turn count, the session (rec["session"], resumed or adopted by the wrapper),
    # the worktree and the notebook go on; the last spell's end is kept for the resumed session's first prompt
    rec.update(family=fam, worktree=wt, notebook=nb, repo=repo, o_r=o_r, started=ts(), ended=None, verdict=None,
               last_end=rec.get("verdict") or rec.get("last_end"), turn=int(rec.get("turn") or 0), bg_id=None,
               pid=None, in_turn=False, progress_at=None,
               restarts=int(rec.get("restarts", -1)) + 1, log=os.path.join(owner_dir(fam), "owner.log"))
    write_json(owner_record_path(fam), rec)
    with suppress(OSError):  # a STOP left by an earlier spell would stop this one at its first pass
        os.unlink(os.path.join(wt, "RED-STOP.md"))
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
    line = _owner_started_line(fam, restarted, p.pid, wt, off["picture"])
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


def _red_brief(d: Any) -> Any:
    """The fixer_prompt fields of a red's data."""
    return {"workflow": d.get("workflow"), "job": d.get("job") or (d.get("jobs") or ["?"])[0], "jobs": d.get("jobs"),
            "kind": d.get("kind") or "test", "line": d.get("line"), "run": d.get("last_run") or {}, "key": d.get("key"),
            "refuted": d.get("refuted"), "predicts": d.get("predicts"), "flake": d.get("red_kind") == "flake",
            "diagnosis": d.get("diagnosis"), "prior_work": d.get("prior_work"), "old_note": d.get("old_note"),
            "decision": red_decision(d)}


def owner_turn_prompt(fam, rec, st, names, first, resumed=False, handover=None):
    """The turn's prompt: the first red's task (fixer_prompt) in the owner's voice, every red of the turn listed. A
    resumed session (its wrapper restarted) is told to continue, never to start over; a fresh one past its predecessor's
    context limit (sessionroll) gets the handover instead."""
    active = red_active(st)
    its = [active[n.split("/", 1)[1]] for n in names if n.split("/", 1)[1] in active]
    reds = [f"{red_what(i['data'])} (red {i['data'].get('key')}): {str(i['data'].get('line') or '')[:160]}" for i in its]
    if not its:
        return None
    u = _red_brief(its[0]["data"])
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


def owner_take_heartbeat(st, fam, holder, meta):
    """The wrapper's heartbeat lease: the one its dispatcher took for it ($AGENTCTL_OWNER_HEARTBEAT, ONE START PER
    FAMILY), still held, its meta made the wrapper's -- else {'lost': why}; with none handed over (`red owner` run by
    hand), taken now -- {'held': ...} when another owner holds it."""
    handed = os.environ.pop(OWNER_HEARTBEAT_ENV, None)
    if not handed:
        return st.acquire(owner_lease(fam), holder, OWNER_TTL, OWNER_MAX, progress=f"beat {int(epoch_now())}",
                          meta=meta)
    x = json.loads(handed)
    hl = HeldLease(owner_lease(fam), x["epoch"], x["secret"], holder, OWNER_TTL)
    gone = hl.renew(st, f"beat {int(epoch_now())}")
    if gone:
        return {"lost": gone[1]}
    st.set_meta(owner_lease(fam), hl.epoch, hl.secret, meta)
    return {"epoch": hl.epoch, "secret": hl.secret}


def owner_still_ending(store, fam, t):
    """Why the family's next owner waits, or None: its last wrapper lost its heartbeat less than OWNER_TTL ago and its
    process still runs (it exits at its next beat; a new one now would race it for the record and the leases)."""
    rec = read_record(owner_record_path(fam)) or {}
    if not rec.get("pid") or rec.get("ended") or not record_alive(rec):
        return None
    gone = float(lease_meta(store.get(owner_lease(fam))).get("released_at") or 0)
    if not gone or t - gone > OWNER_TTL:
        return None
    return f"its owner's last wrapper (pid {rec['pid']}) lost its heartbeat and is ending: the next owner starts after it"
