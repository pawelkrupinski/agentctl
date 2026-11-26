# agentctl, part `redcrash`: the crash-loop breaker, harness faults, decisions for the owner, old runs.
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import os
import re
from typing import Any
from agentctl.claudefiles import claude_limit
from agentctl.core import SHORT, fmt_age, remote, tilde
from agentctl.timefmt import parse_time, ts
from agentctl.repos import RED_PRIMARY, red_repo_of
from agentctl.workflows import workflow_file_of
from agentctl.leases import block_lease, lease_meta
from agentctl.redrecord import red_what
from agentctl.issues import issue_open
from agentctl.redfamilies import red_family
from agentctl.redask import ask_held_until, red_ask_mark
from agentctl.redmove import red_move
from agentctl.ciwhy import ci_state_load
from agentctl.redblock import BLOCK_TTL
from agentctl.rundispatch import red_verify_dispatch
from agentctl.redopen import red_offer
from agentctl.redknobs import knob_red_crash_block_max, knob_red_old_run
from agentctl.hostsleep import lapse_in_sleep


RED_CRASH_OUTCOME = re.compile(r"(NOT STARTED|ended with no|exit -?\d+ with no|TIMED OUT|lease expired|"
                               r"STOPPED: the red's lease was lost|STALLED|ORPHANED|store auth failed)")
RED_CRASH_NEUTRAL = re.compile(r"(RELEASED|STOPPED: the fixer's wrapper was stopped|LIMITED: )")
CRASH_LOOP_CAUSE = "fixer-crash-loop"


def launch_neutral(h: dict[str, Any]) -> bool:
    """True for a history entry that is no launch for the crash streak: a neutral end (a release by hand, a stopped
    wrapper, LIMITED), a launch on claude's spent usage limit, or a lease lapse while the host slept."""
    o = str(h.get("outcome") or "")
    return bool(RED_CRASH_NEUTRAL.match(o) or claude_limit(f"{o}\n{h.get('notes') or ''}")
                or (h.get("dispatched") and lapse_in_sleep(h)))


def red_crash_streak(d):
    """The newest fixer launches (newest first) since the red's last crash-loop step that ended with no result -- up to
    the first launch (or fix) with one. A lease lapse while the host slept is no launch (host_slept), nor one that ended
    on the account's spent usage limit (claude_limit: an outside wait, never the harness)."""
    hist = d.get("history") or []
    out = []
    for h in reversed(hist[int((d.get("crash_loop") or {}).get("mark") or 0):]):
        o = str(h.get("outcome") or "")
        if launch_neutral(h):
            continue
        if not h.get("dispatched") or not RED_CRASH_OUTCOME.match(o):
            break
        out.append(h)
    return out


LAUNCH_FAILURE = re.compile(r"^(?:NOT STARTED: )+`claude --bg` (.*)$", re.S)


def launch_failure(outcome):
    """A launch `claude --bg` refused (fixer.bg_start: `exit N: <its stderr>`, or `failed (<error>)`): that text, which
    is the whole story -- the session never started, so there is no log to read. None for any other outcome."""
    m = LAUNCH_FAILURE.match(str(outcome or ""))
    return " ".join(m.group(1).split()) if m else None


def red_crash_evidence(streak):
    """The escalation's tail: each crashed launch's outcome, then the last lines of its log."""
    tail = []
    for h in reversed(streak):
        failed = launch_failure(h.get("outcome"))
        if failed is not None:
            tail.append(f"{h.get('at')} {h.get('holder')}: LAUNCH FAILED: {failed[:400]}")
            continue
        tail.append(f"{h.get('at')} {h.get('holder')}: {str(h.get('outcome'))[:200]}"
                    + (f" (log: {tilde(str(h['log']))})" if h.get("log") else ""))
        tail += [f"    {l[:200]}" for l in str(h.get("notes") or "").splitlines()[-4:] if l.strip()]
    return tail[-30:]


def _crash_evidence(streak: Any) -> Any:
    """The first log or worktree of the streak's launches that exists on disk, else None."""
    for h in streak:
        for p in (h.get("log"), h.get("worktree")):
            if p and os.path.exists(p):
                return p
    return None


def red_crash_cmd(streak):
    """The owner's look at the crashes: `red status -v`, then the newest crashed launch's log (its wrapper's stderr or its
    transcript), else its worktree -- never a `less ?`, nor a log that was never written (a launch that failed)."""
    p = _crash_evidence(streak)
    if not p:
        return "~/bin/agentctl red status -v"
    return tilde(f"~/bin/agentctl red status -v; " + (f"less {p}" if not os.path.isdir(p) else f"cd {p}"))


def red_crash_launches(streak):
    """Each crashed launch, oldest first: when, and how it ended (its stop reason, or its log's last line; a launch
    `claude --bg` refused: LAUNCH FAILED and its stderr)."""
    out = []
    for h in reversed(streak):
        failed = launch_failure(h.get("outcome"))
        if failed is not None:
            why = f"LAUNCH FAILED: {failed[:300]}"
        else:
            last = [l.strip() for l in str(h.get("notes") or "").splitlines() if l.strip()]
            why = str(h.get("outcome"))[:400] + (f" -- {last[-1][:100]}" if last else "")  # a dead wrapper's last words
        out.append(f"{str(h.get('at'))[11:16]}Z {why}")
    return "; ".join(out)


def red_crash_loop(store, it, upd, streak, t):
    """RED_CRASH_LOOP launches in a row with no result: escalated once, with the evidence, and moved up -- deep tier,
    or (the deep tier crash-looped too) an owner block that clears itself."""
    d = {**it["data"], **upd}
    key, n = d["key"], len(streak)
    tail = red_crash_evidence(streak)
    mark = len(d.get("history") or [])
    tier = it["data"].get("tier")
    run = (d.get("last_run") or {}).get("number")
    if not (d.get("crash_loop") or {}).get("stage") and tier != "deep":
        verdict = (f"RED ESCALATE: {red_what(d)}: CRASH LOOP -- {n} fixer launches in a row ({tier}) ended with no "
                   f"LANDED / GAVE UP; not dispatched that way again: moved up to the deep tier "
                   f"(`~/bin/agentctl red status -v`)")
        upd = {**upd, "crash_loop": {"stage": "deep", "mark": mark, "at": t, "from": tier, "launches": n},
               "start_failures": 0, "dispatch_after": 0}
        red_offer(store, it, verdict, data=upd, escalate=True, verdict=verdict, tail=tail)
        return [f"crash loop on {red_what(d)}: moved up to deep"]
    until = f"a newer {d.get('workflow')} run than #{run} no longer fails this way"
    cmd = red_crash_cmd(streak)
    issue = red_harness_issue(store, d, n, tier, red_crash_launches(streak), cmd)
    text = (f"HARNESS FAULT: the fixers crash on {red_what(d)}: {n} launches in a row at the {tier} tier ended with "
            f"no LANDED / GAVE UP ({red_crash_launches(streak)}). The fixer harness, not the code, is what fails: "
            f"agentctl's own bug, filed for its maintainer as issue {issue} (logs: `{cmd}`). Auto-clears when {until}, "
            f"when the issue is closed, or in {fmt_age(knob_red_crash_block_max())} "
            "(offered again)")
    upd = {**upd, "crash_loop": {"stage": "harness", "mark": mark, "at": t, "from": tier, "launches": n}}
    red_harness_block(store, it, upd, issue, CRASH_LOOP_CAUSE, cmd, until, text, t, tail=tail)
    return [f"crash loop on {red_what(d)}: a harness fault, filed as agentctl issue {issue}"]


def _block_taken(store: Any, it: dict[str, Any], lname: str, got: dict[str, Any], key: str) -> None:
    """Red `it`'s block lease `lname` was asked for (`got`): a block already holding the red stands and the red joins its keys; the red's open offer closed as blocked."""
    if "held" in got:  # somebody's block already holds the red: it stands, the red joins it
        row = store.get(lname)
        store.set_meta(lname, row["epoch"], row["secret"], {"keys": sorted(set(lease_meta(row).get("keys") or []) | {key})})
    store.close_offer(f"red:{it['id']}", "blocked")


def red_harness_block(store, it, upd, issue, cause, cmd, until, text, t, tail=None):
    """Red `it` waits on agentctl's own issue `issue` (a harness fault, never the owner's): a block lease the issue's
    close, a newer run's verdict or its max hold lifts (block_invalid, red_renew_blocks), the red blocked-external."""
    d = {**it["data"], **upd}
    key, run = d["key"], (d.get("last_run") or {}).get("number")
    lname = block_lease(key)
    probe = red_newer_probe(it, d)
    got = store.acquire(lname, f"harness:{cause}", BLOCK_TTL,
                        knob_red_crash_block_max(), progress=f"{cause}@{int(t)}",
                        meta={"kind": "block", "crash_loop": True, "harness": True, "issue": issue,
                              "cause": cause, "cmd": cmd, "probe": probe, "until": until, "keys": [key],
                              "text": text, "details": text, "probed_at": t})
    _block_taken(store, it, lname, got, key)
    line = f"RED HARNESS FAULT: {red_what(d)}: {text}"
    red_move(store, it, "blocked-external", line, data={**upd, "cause": cause, "block": lname}, severity="info",
             refs={"key": key, "run": run, "workflow": d.get("workflow"), "verdict": line, "tail": tail,
                   "command": "red", "final": False})


# A RED BLAMED ON AN OPEN agentctl ISSUE WAITS ON IT (issue 20261101T030050-695594: the false stuck red 50dd506d93c9 was
# offered back to its owner 3 times in 2 minutes, each turn ending GAVE UP naming the filed agentctl issue: no fix for it
# can land in the red's repo). A verdict that names an OPEN agentctl issue as the cause holds the red on a harness block
# until that issue closes (or a newer run decides it, or the max hold): agentctl's maintainer fixes it, not a fixer.
RED_ISSUE_ID = re.compile(r"\b(\d{8}T\d{6}-[0-9a-f]{6})\b")
RED_ISSUE_BLAME = re.compile(r"\bagentctl\b.*\b(defect|bug|issue)\b|\b(defect|bug|issue)\b.*\bagentctl\b", re.I | re.S)
AGENTCTL_DEFECT_CAUSE = "agentctl-defect"


def red_blamed_issue(store, out):
    """The open agentctl issue a fixer's verdict blames its red on, or None."""
    if not RED_ISSUE_BLAME.search(out) or not re.match(r"\W*(GAVE UP|DIAGNOSED|BLOCKED)", out):
        return None
    for iid in RED_ISSUE_ID.findall(out):
        iss = store.item("issue", iid)
        if iss and iss["state"] != "closed":
            return iid
    return None


# A HARNESS FAULT IS agentctl's OWN BUG (the owner, 2026-11-19: core Main Pipeline #1053 read "BLOCKED ON OWNER: the
# fixers crash" -- a cross-host lease misjudgement and a re-counted lease end, agentctl's bugs fixed in 852acad). The
# fixers crash-looping at the deep tier is the harness failing, never the owner's to repair: ONE agentctl issue per red
# (its maintainer's queue: the status page, `agentctl issue --list`, its weekly escalation), and the red waits on a
# harness block, never an owner ask (red_ask: no owner-only category) -- lifted by a newer run's verdict, the issue
# closed (red_renew_blocks), or its max hold.
def red_harness_issue(store, d, n, tier, launches, cmd):
    """The id of the open agentctl issue for this red's harness fault, filed once (an open one is reused)."""
    tag = f"[harness-fault {d.get('key')}]"
    for i in store.items("issue", ("open",)):
        if str((i.get("data") or {}).get("text") or "").startswith(tag):
            return i["id"]
    text = (f"{tag} the fixers crash on {red_what(d)}: {n} launches in a row at the {tier} tier ended with no LANDED / "
            f"GAVE UP ({launches}). Diagnose agentctl (orphaned leases, wrapper deaths, lease ends counted twice, launch "
            f"refusals) in a ~/bin-wt-<name> worktree, test-first, and close this issue with the fix's sha; its red is "
            f"offered again once it is closed. Logs: `{cmd}`")
    return issue_open(store, text, "agentctl (harness fault)")["id"]


def red_newer_probe(it, d):
    """The probe a harness or decision block of red `it` (data d) lifts on: a newer decided run of its workflow, read
    in the red's OWN repo's snapshot (issue 20261102T085329-930a4a: a core red's block read movies')."""
    return {"kind": "red_newer_verdict", "args": {"workflow": d.get("workflow"), "item": it["id"],
                                                  "run_id": (d.get("last_run") or {}).get("databaseId"),
                                                  "run": (d.get("last_run") or {}).get("number"), "repo": d.get("repo")}}


def _red_latest_run(a: Any) -> Any:
    """The newest decided run of the red's workflow on main in the CI snapshot, or None."""
    snap = ci_state_load(a.get("repo") or SHORT["movies"])
    return ((snap or {}).get("latest") or {}).get(a.get("workflow"))


def _red_still_fails(a: Any, last: Any, store: Any) -> Any:
    """The red's own last run is `last`: it still fails the way the red does."""
    it = store.item("red", a.get("item")) if a.get("item") else None
    return bool(it and str((it["data"].get("last_run") or {}).get("databaseId")) == str(last.get("databaseId")))


def probe_red_newer_verdict(a, store):
    """A newer decided run of the red's workflow on main changed its verdict: green, or no longer attached to the red
    (a run failing the same way is attached to it by the tick's observation, before the blocks are probed)."""
    last = _red_latest_run(a)
    if not last:
        return None, f"no decided run of {a.get('workflow')} on main in the snapshot"
    if int(last.get("databaseId") or 0) <= int(a.get("run_id") or 0):
        return False, f"no {a.get('workflow')} run newer than #{a.get('run')}"
    if last.get("conclusion") == "success":
        return True, f"{a.get('workflow')} #{last.get('number')} green"
    if _red_still_fails(a, last, store):
        return False, f"{a.get('workflow')} #{last.get('number')} still fails this way"
    return True, f"{a.get('workflow')} #{last.get('number')} {last.get('conclusion')}, no longer this failure"


def red_old_run(d, t):
    """The age of an outside red's newest failing run when it is older than RED_OLD_RUN and not yet re-checked, else 0."""
    if d.get("red_kind") != "outside" or d.get("old_checked"):
        return 0
    try:
        age = t - parse_time((d.get("last_run") or {}).get("createdAt")).timestamp()
    except (ValueError, TypeError, AttributeError):
        return 0
    return age if age > knob_red_old_run() else 0


def red_resolve_old(store, it, age, o_r, repo, t):
    """A dispatchable workflow is run again on main (fix-pending: verify) -- green verifies it, the same failure makes it
    an ordinary red. One that no fresh run can check (no workflow_dispatch, its file gone from main, or the dispatch
    refused) returns None: it stays an ordinary red, offered and dispatched like any other. It is never RETIRED:
    "Report run timings" #38 (2026-10-29, a workflow_call-only workflow) was retired, re-opened by the next tick's same
    observation and retired again 811 times in 19 h, never offered, while ci-why showed it "offering" for 9 days."""
    d = it["data"]
    path, txt = workflow_file_of(repo, f"{remote()}/main", d.get("workflow")) if repo else (None, "")
    if path and "workflow_dispatch" in txt:
        line = red_verify_dispatch(o_r, repo, d.get("workflow"), "", (d.get("last_run") or {}).get("databaseId"))
        if line.startswith("dispatched"):
            store.close_offer(f"red:{it['id']}", "old run")
            red_move(store, it, "fix-pending", f"{red_what(d)}: its newest run is {fmt_age(age)} old: {line}",
                     data={"old_checked": True, "fix": {"status": "verify", "at": t, "entry": None, "sha": "",
                                                        "verify_dispatched": ts(), "verify": line}})
            return [f"old red {red_what(d)}: {line}"]
        why = line
    else:
        why = (f"{d.get('workflow')} has not run for {fmt_age(age)}" +
               (" and has no workflow_dispatch" if path else " and its workflow file is gone from main"))
    store.update_item("red", it["id"], it["version"], {"old_checked": True,
                                                      "old_note": f"no fresh run can check it ({why[:200]}): a fixer decides"})
    return None


def red_queue_family(d):
    """The queue family of a red (its data, its picture, a fixer record): the configured family, else its run."""
    fam = red_family(d)
    if fam:
        return fam
    run = d.get("run") if d.get("run") is not None else d.get("last_run")
    num = run.get("number") if isinstance(run, dict) else run
    o_r = red_repo_of(d)
    return f"run:{d.get('workflow')}#{num}" if o_r == RED_PRIMARY else f"run:{o_r}:{d.get('workflow')}#{num}"


def red_owner_block(store, it, line, t, **ev):
    """Move red it onto the owner (blocked-external) with its question: told at once (an escalation: its inbox entry and
    notification) or, held (ask_held_until), recorded with an info event -- the tick tells it when the hold ends."""
    held = ask_held_until([({"data": {**it["data"], **(ev.get("data") or {})}}, None)], t, t, store) is not None
    moved = red_move(store, it, "blocked-external", line, severity="info" if held else "escalate", **ev)
    red_ask_mark(store, moved, t, told=not held)
    return moved
