# agentctl, part `redcrash`: the crash-loop breaker, harness faults, decisions for the owner, old runs.
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import datetime as dt
import os
import re
import subprocess
import time
from typing import Any
from agentctl.core import HOME, SHORT, env_num, fmt_age, remote, spawn_detached
from agentctl.timefmt import iso_epoch, parse_time, ts
from agentctl.statefile import read_record
from agentctl.store import open_store
from agentctl.repos import RED_PRIMARY, red_repo_of
from agentctl.workflows import workflow_file_of
from agentctl.leases import block_lease, lease_meta
from agentctl.redrecord import red_what
from agentctl.issues import issue_open
from agentctl.redfamilies import red_family
from agentctl.owners import OWNER_TTL, owner_record_path
from agentctl.redask import ask_held_until, ask_refusal, ask_split, decide_cmd, red_ask_mark
from agentctl.redmove import red_move
from agentctl.ciwhy import ci_state_load
from agentctl.redblock import BLOCK_TTL
from agentctl.rundispatch import red_verify_dispatch
from agentctl.redopen import red_offer


# THE CRASH-LOOP BREAKER (G9, convergence-failures-2026-10-29 §4: in E9 fixers were launched 52 times over 9 reds, #2335
# 18 and #2375 16 times, mostly `exit 1 with no LANDED / GAVE UP line`): RED_CRASH_LOOP dispatched fixers in a row that
# ended with NO RESULT (a crash, an exit with no verdict line, a lease lost or expired without progress, a time-out, a
# start that never got going) and the same tier is not dispatched blindly again. ONE escalation with the evidence (each
# launch's outcome and log tail), and the red moves UP: to the deep tier (crash_loop.stage `deep`) -- or, when the deep
# tier itself crash-looped, onto an OWNER BLOCK (block/<key>, cause `fixer-crash-loop`): the fixers, not the code, are
# what fails, so this is the one owner block a test red may have. It recovers by itself: its probe passes when a newer
# decided run of the red's workflow no longer fails this way (or the run is green), and it lapses after
# RED_CRASH_BLOCK_MAX (offered again, the streak counted afresh from crash_loop.mark). A launch with a result (LANDED,
# NOT LANDED, GAVE UP, BLOCKED ON OWNER, DIAGNOSED, a refuted fix) ends the streak; a release by hand is no launch.
RED_CRASH_LOOP = 3                 # $AGENTCTL_RED_CRASH_LOOP
RED_CRASH_BLOCK_MAX = 24 * 3600    # $AGENTCTL_RED_CRASH_BLOCK_MAX
RED_CRASH_OUTCOME = re.compile(r"(NOT STARTED|ended with no|exit -?\d+ with no|TIMED OUT|lease expired|"
                               r"STOPPED: the red's lease was lost|STALLED|ORPHANED|store auth failed)")
RED_CRASH_NEUTRAL = re.compile(r"(RELEASED|STOPPED: the fixer's wrapper was stopped)")
CRASH_LOOP_CAUSE = "fixer-crash-loop"


# THE HOST SLEEPING IS NO CRASH (2026-11-01, core/movies main #2046 `fixer-crash-loop`: both family owners' wrappers froze
# in a macOS maintenance sleep -- 03:05:44 to 03:21:11, longer than OWNER_TTL -- so their heartbeat leases lapsed
# ("lease expired"), the wrappers woke to "heartbeat lease was lost" and exited, the dispatcher restarted them, and three
# such cycles blocked the red on the owner with the harness' "cause" being nothing but a closed lid). A wrapper now holds
# a `caffeinate -s` assertion for its life (keep_awake), and a lease lapse that overlaps a host sleep is neutral to the
# breaker (host_slept) and says so in the evidence. `-s` holds only on AC power (owner 2026-11-01: "prevent laptop going
# to sleep when agentctl is running and power supply is on"); on battery the laptop sleeps and host_slept covers it.
# The service (waiting run --service) holds the same assertion, so a plugged-in laptop never sleeps under agentctl.
LEASE_LAPSE = re.compile(r"(lease expired|ORPHANED|STOPPED: the (red's|owner's) (heartbeat )?lease was lost)")
SLEEP_MIN = 120      # s of sleep inside a lapse's window that count as "the host was asleep" ($AGENTCTL_SLEEP_MIN)
_SLEEP_CACHE = [0.0, []]


def keep_awake(pid: int | None = None) -> "subprocess.Popen[Any] | None":
    """Hold a no-system-sleep assertion, valid on AC power only, for `pid` (default: this process) so a held lease is
    never starved by the laptop's sleep. Once per pid: an exec'd restart keeps the assertion it already has. Best
    effort: the Popen, or None (already held, no caffeinate, $AGENTCTL_CAFFEINATE empty)."""
    exe = os.environ.get("AGENTCTL_CAFFEINATE", "/usr/bin/caffeinate")
    who = str(pid or os.getpid())
    if not exe or not os.access(exe, os.X_OK) or os.environ.get("AGENTCTL_AWAKE_PID") == who:
        return None
    os.environ["AGENTCTL_AWAKE_PID"] = who
    try:
        return spawn_detached([exe, "-s", "-w", who], stdin=subprocess.DEVNULL,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError:
        return None


def sleep_intervals():
    """[(start, end)] epoch seconds of the host's recent sleeps, from `pmset -g log` (or $AGENTCTL_SLEEP_LOG, a file in
    its format). [] when unreadable. Cached for a minute."""
    now = time.time()
    if now - _SLEEP_CACHE[0] < 60 and not os.environ.get("AGENTCTL_SLEEP_LOG"):
        return _SLEEP_CACHE[1]
    try:
        path = os.environ.get("AGENTCTL_SLEEP_LOG")
        if path:
            text = open(path).read()
        else:
            text = subprocess.run(["pmset", "-g", "log"], capture_output=True, text=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        text = ""
    out, start = [], None
    for m in re.finditer(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d [+-]\d{4}) (Sleep|Wake|DarkWake)\b", text, re.M):
        try:
            at = dt.datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S %z").timestamp()
        except ValueError:
            continue
        if m.group(2) == "Sleep":
            start = at
        elif start is not None:
            out.append((start, at))
            start = None
    if not os.environ.get("AGENTCTL_SLEEP_LOG"):
        _SLEEP_CACHE[:] = [now, out]
    return out


def host_slept(end, window):
    """Seconds the host slept in [end - window, end] (end: epoch seconds)."""
    lo = end - window
    return sum(max(0.0, min(b, end) - max(a, lo)) for a, b in sleep_intervals())


def lapse_in_sleep(h):
    """True for a launch whose lease merely lapsed (no work seen) while the host slept: not a crash."""
    o = str(h.get("outcome") or "")
    if not LEASE_LAPSE.search(o):
        return False
    end = iso_epoch(h.get("at"))
    if end is None:
        return False
    return host_slept(end, OWNER_TTL + 300) >= env_num("AGENTCTL_SLEEP_MIN", SLEEP_MIN)


def red_crash_streak(d):
    """The newest fixer launches (newest first) since the red's last crash-loop step that ended with no result -- up to
    the first launch (or fix) with one. A lease lapse while the host slept is no launch (host_slept)."""
    hist = d.get("history") or []
    out = []
    for h in reversed(hist[int((d.get("crash_loop") or {}).get("mark") or 0):]):
        o = str(h.get("outcome") or "")
        if RED_CRASH_NEUTRAL.match(o) or (h.get("dispatched") and lapse_in_sleep(h)):
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
                    + (f" (log: {str(h['log']).replace(HOME, '~')})" if h.get("log") else ""))
        tail += [f"    {l[:200]}" for l in str(h.get("notes") or "").splitlines()[-4:] if l.strip()]
    return tail[-30:]


def red_crash_cmd(streak):
    """The owner's look at the crashes: `red status -v`, then the newest crashed launch's log (its wrapper's stderr or its
    transcript), else its worktree -- never a `less ?`, nor a log that was never written (a launch that failed)."""
    for h in streak:
        for p in (h.get("log"), h.get("worktree")):
            if p and os.path.exists(p):
                return (f"~/bin/agentctl red status -v; " + (f"less {p}" if not os.path.isdir(p) else f"cd {p}")
                        ).replace(HOME, "~")
    return "~/bin/agentctl red status -v"


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
            f"when the issue is closed, or in {fmt_age(env_num('AGENTCTL_RED_CRASH_BLOCK_MAX', RED_CRASH_BLOCK_MAX))} "
            "(offered again)")
    upd = {**upd, "crash_loop": {"stage": "harness", "mark": mark, "at": t, "from": tier, "launches": n}}
    red_harness_block(store, it, upd, issue, CRASH_LOOP_CAUSE, cmd, until, text, t, tail=tail)
    return [f"crash loop on {red_what(d)}: a harness fault, filed as agentctl issue {issue}"]


def red_harness_block(store, it, upd, issue, cause, cmd, until, text, t, tail=None):
    """Red `it` waits on agentctl's own issue `issue` (a harness fault, never the owner's): a block lease the issue's
    close, a newer run's verdict or its max hold lifts (block_invalid, red_renew_blocks), the red blocked-external."""
    d = {**it["data"], **upd}
    key, run = d["key"], (d.get("last_run") or {}).get("number")
    lname = block_lease(key)
    probe = red_newer_probe(it, d)
    got = store.acquire(lname, f"harness:{cause}", BLOCK_TTL,
                        env_num("AGENTCTL_RED_CRASH_BLOCK_MAX", RED_CRASH_BLOCK_MAX), progress=f"{cause}@{int(t)}",
                        meta={"kind": "block", "crash_loop": True, "harness": True, "issue": issue,
                              "cause": cause, "cmd": cmd, "probe": probe, "until": until, "keys": [key],
                              "text": text, "details": text, "probed_at": t})
    if "held" in got:  # somebody's block already holds the red: it stands, the red joins it
        row = store.get(lname)
        store.set_meta(lname, row["epoch"], row["secret"], {"keys": sorted(set(lease_meta(row).get("keys") or []) | {key})})
    line = f"RED HARNESS FAULT: {red_what(d)}: {text}"
    store.close_offer(f"red:{it['id']}", "blocked")
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


# A DIAGNOSIS THAT NEEDS THE OWNER (2026-11-01, convergence #579: "resolver design ... The gate needs your design
# call"): DIAGNOSED was no end -- the red went back to its fixer (the family's owner), who diagnosed it again, turn after
# turn. A DIAGNOSED verdict that names the owner's decision (`DIAGNOSED: <cause>; decide: <the decision>`), or a second
# DIAGNOSED in a row (the implementer briefed with the first could not make the fix either: crashes and lapses between
# them are skipped), puts the red on an OWNER-DECISION BLOCK (block/<key>, cause `owner-decision`) whose text carries
# the diagnosis and the decision -- never a crash, never the crash-loop breaker's. Like the crash-loop block it is one
# a test red may hold, and it clears itself: a newer run of the red's workflow no longer failing this way, or
# RED_DECISION_BLOCK_MAX (offered again); the owner lifts it at once with `agentctl red release <key> --any`.
RED_DECISION_BLOCK_MAX = 7 * 86400   # an owner-decision block lapses after this: the red offered again (2026-11-03: was
# 24 h -- each lapse re-offered the red to fixers who could not decide it, and the RED ESCALATE / GAVE UP entries
# piled up; the owner is reminded instead (ask.py), and the block's probe still clears it)
DECISION_CAUSE = "owner-decision"
RED_DECIDE = re.compile(r"(?:;|^\W*GAVE UP:.*?)\s*decide(\[[\w-]+\])?:\s*(.+)$", re.S | re.I)


VERDICT_MAX = 600  # chars of a fixer's or owner's verdict kept as its lease's outcome


def verdict_cut(text, n=VERDICT_MAX):
    """A verdict cut to n chars that keeps its `decide[...]: ...` clause (issue 20261101T082701-ab1154: a long cause
    before `; decide:` cut the decision off, and the red, whose decision is parsed from this outcome, went back to its
    owner every turn): the cause is shortened, never the decision (itself cut only past n)."""
    text = str(text or "")
    m = RED_DECIDE.search(text) if len(text) > n else None
    if not m:
        return text[:n]
    at = text.rfind("decide", 0, m.start(2))
    clause = "; " + text[at:]
    head = text[:at].rstrip().rstrip(";").rstrip()
    return (head[:max(0, n - len(clause) - 4)].rstrip() + " ..." + clause)[:max(n, len(clause) + 40)]


def red_decision_needed(d, h):
    """The decision the owner must make on red d, whose launch h ended DIAGNOSED, or None (an implementer goes on)."""
    m = RED_DECIDE.search(str(h["outcome"]))
    if m:
        return (m.group(1) or "") + m.group(2).strip()
    for p in reversed(d.get("history") or []):
        o = str(p.get("outcome") or "")
        if RED_CRASH_OUTCOME.match(o) or RED_CRASH_NEUTRAL.match(o):
            continue
        if o.startswith("DIAGNOSED:"):
            return ("it was DIAGNOSED twice in a row and its fixers cannot fix it within the red rules: decide how it "
                    "is fixed (a design change, a known flip to accept, or the fix a fixer may make)")
        return None
    return None


def red_ask_route(store: Any, it: Any, upd: dict[str, Any], why: str, words: object, t: float) -> list[str]:
    """An ask outside the owner-only categories (ask.py OWNER_ONLY) never reaches the owner: the red goes back to its
    fixers, briefed to proceed under the repo's standing rules (CLAUDE.md: root cause, test-first, never a rerun) and its
    checked-in policies, with the words it asked in as the diagnosis -- an attempt like any other, so the ladder climbs."""
    d = {**it["data"], **upd}
    note = (f"{str(words)[:400]} -- NOT AN OWNER QUESTION ({why}). Proceed: make the fix the repo's standing rules "
            "dictate (CLAUDE.md: root cause, test-first, never rerun to green) and the checked-in policies answer (e.g. "
            "movies scripts/ci/lane-policy.tsv for a ci-runner lane); ask the owner only in an owner-only category, "
            "`decide[<category>]: ...` (the fixer brief)")
    extra = {"attempts": int(d.get("attempts") or 0) + 1} if upd.get("gave_up_ask") else {}
    store.close_offer(f"red:{it['id']}", "routed")
    red_offer(store, it, f"{red_what(d)}: its ask is no owner's ({why}): offered again to proceed",
              data={**upd, **extra, "gave_up_ask": None, "diagnosis": note[:900], "cause": None, "block": None,
                    "ask": None}, quiet=True)
    return [f"routed {red_what(d)}'s ask back to its fixer: {why[:120]}"]


def red_decision_block(store, it, upd, decide, t, category=None):
    """The red waits on the owner's decision: its block carries the diagnosis and the decision, clears itself. Only
    in an owner-only category (`[<category>] <decision>`, ask_split): any other ask is routed back (red_ask_route)."""
    cat, question = ask_split(decide)
    category = category or cat
    why = ask_refusal(category, question)
    if why:
        return red_ask_route(store, it, upd, why, question, t)
    decide = question
    upd = {k: v for k, v in upd.items() if k != "gave_up_ask"}
    d = {**it["data"], **upd}
    key, run = d["key"], (d.get("last_run") or {}).get("number")
    diag = RED_DECIDE.sub("", str(d.get("diagnosis") or "")).strip()
    nb = (read_record(owner_record_path(red_queue_family(d))) or {}).get("notebook")
    cmd = f"~/bin/agentctl red status -v" + (f"; less {nb}" if nb else "")
    until = f"a newer {d.get('workflow')} run than #{run} no longer fails this way"
    maxh = env_num("AGENTCTL_RED_DECISION_BLOCK_MAX", RED_DECISION_BLOCK_MAX)
    text = (f"WAITING ON YOU: {decide[:400]} -- {red_what(d)} waits on it. Answer: `! {decide_cmd(key)}` (its fixer "
            f"is dispatched with your answer). " + (f"Why: DIAGNOSED: {diag[:400]}. " if diag else "") +
            f"Look: `{cmd}`. Auto-clears when {until}, or in {fmt_age(maxh)} (offered again)").replace(HOME, "~")
    lname = block_lease(key)
    probe = red_newer_probe(it, d)
    got = store.acquire(lname, f"owner:{DECISION_CAUSE}", BLOCK_TTL, maxh, progress=f"decision@{int(t)}",
                        meta={"kind": "block", "decision": True, "cause": DECISION_CAUSE, "cmd": cmd, "probe": probe,
                              "until": until, "keys": [key], "text": text, "details": diag, "decide": decide,
                              "question": decide, "category": category, "probed_at": t})
    if "held" in got:  # somebody's block already holds the red: it stands, the red joins it
        row = store.get(lname)
        store.set_meta(lname, row["epoch"], row["secret"], {"keys": sorted(set(lease_meta(row).get("keys") or []) | {key})})
    line = f"RED BLOCKED ON OWNER [{category}]: {text}"
    store.close_offer(f"red:{it['id']}", "blocked")
    red_owner_block(store, it, line, t, data={**upd, "cause": DECISION_CAUSE, "block": lname},
                    refs={"key": key, "run": run, "workflow": d.get("workflow"), "verdict": line, "command": "red"})
    return [f"decision needed on {red_what(d)}: blocked on the owner"]


def red_newer_probe(it, d):
    """The probe a harness or decision block of red `it` (data d) lifts on: a newer decided run of its workflow, read
    in the red's OWN repo's snapshot (issue 20261102T085329-930a4a: a core red's block read movies')."""
    return {"kind": "red_newer_verdict", "args": {"workflow": d.get("workflow"), "item": it["id"],
                                                  "run_id": (d.get("last_run") or {}).get("databaseId"),
                                                  "run": (d.get("last_run") or {}).get("number"), "repo": d.get("repo")}}


def probe_red_newer_verdict(a):
    """A newer decided run of the red's workflow on main changed its verdict: green, or no longer attached to the red
    (a run failing the same way is attached to it by the tick's observation, before the blocks are probed)."""
    snap = ci_state_load(a.get("repo") or SHORT["movies"])
    last = ((snap or {}).get("latest") or {}).get(a.get("workflow"))
    if not last:
        return None, f"no decided run of {a.get('workflow')} on main in the snapshot"
    if int(last.get("databaseId") or 0) <= int(a.get("run_id") or 0):
        return False, f"no {a.get('workflow')} run newer than #{a.get('run')}"
    if last.get("conclusion") == "success":
        return True, f"{a.get('workflow')} #{last.get('number')} green"
    it = open_store().item("red", a.get("item")) if a.get("item") else None
    if it and str((it["data"].get("last_run") or {}).get("databaseId")) == str(last.get("databaseId")):
        return False, f"{a.get('workflow')} #{last.get('number')} still fails this way"
    return True, f"{a.get('workflow')} #{last.get('number')} {last.get('conclusion')}, no longer this failure"


RED_OLD_RUN = 2 * 86400  # an outside red whose newest run is older than this is checked by a fresh run before it is fixed


def red_old_run(d, t):
    """The age of an outside red's newest failing run when it is older than RED_OLD_RUN and not yet re-checked, else 0."""
    if d.get("red_kind") != "outside" or d.get("old_checked"):
        return 0
    try:
        age = t - parse_time((d.get("last_run") or {}).get("createdAt")).timestamp()
    except (ValueError, TypeError, AttributeError):
        return 0
    return age if age > env_num("AGENTCTL_RED_OLD_RUN", RED_OLD_RUN) else 0


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
