# agentctl, part `crashloop`: THE FIXER CRASH LOOP as a condition: N launches of one red's or one escalation's fixer that each died within M s of starting, of one cause, open CRASH_LOOP -- routed by the escalation table, holding that fixer's relaunches while open
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.
#
# 2026-11-24 (Main #2877, Alert backtest #26, Tool self-tests #533; the transition analysis' item 6): each launched three
# fixers that died at launch into a locked vault. e01f564 made the red ladder read a one-cause loop (redcrash.py
# crash_cause) and hand it on -- but only as the red's own block and an agentctl issue nobody was sent to. Now the
# server's tick reads the fixer LAUNCH RECORDS -- a red's history (each dispatched lease's start and end, redopen.py
# red_history) and an escalation's tries (escalations.py, the condition's `escalation.tries`) -- and a subject whose
# newest CRASH_LOOP_LAUNCHES launches all died within CRASH_LOOP_QUICK s of starting, of ONE cause (redcrash.py
# launch_cause), opens ONE condition, CRASH_LOOP (key `crash-loop-<red|condition>-<id>`), its text the cause and its
# evidence each launch's end and log. THE ESCALATION TABLE ROUTES IT (escalationtable.py): an owner-only cause (the
# vault, Touch ID, a credential: OWNER_SIGNALS) is the owner's -- its one inbox entry is his ask --, any other goes to an
# Opus agent briefed with the launches and the history. WHILE IT IS OPEN THAT FIXER IS NOT LAUNCHED AGAIN (crash_loop_hold:
# the red dispatcher's and the escalation dispatcher's offers wait, saying why). Its probe closes it when the subject
# clears, a launch survives past M, agentctl's code changed since it opened (a fix deployed: ONE launch is let through to
# judge it), the red was offered again after it opened (its block lifted: the owner's answer or its bound) or the
# escalation went to the owner. A launch that dies the same way again opens it again at once -- one launch per fix or
# answer, never a storm. A subject's loop opens once per new launch (the meta mark `crash_loop:<subject>`, the newest
# launch's end it was opened for).

import re
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Optional

from agentctl.knobread import env_num
from agentctl.timefmt import iso_epoch
from agentctl.statemachines import machine_state
from agentctl.conditions import open_condition, run_probe
from agentctl.escalationtable import CRASH_LOOP_KIND, OWNER_SIGNALS, crash_loop_key
from agentctl.redcrash import RED_CRASH_OUTCOME, launch_cause, launch_neutral

CRASH_LOOP_LAUNCHES = 3   # launches in a row dying quickly of one cause ($AGENTCTL_CRASH_LOOP_LAUNCHES)
CRASH_LOOP_QUICK = 120    # s: a launch ending this soon after its start never got going ($AGENTCTL_CRASH_LOOP_QUICK)
EVIDENCE_LINES = 40       # lines of launch evidence the condition keeps for its agent's brief


def knob_crash_loop_launches() -> int:
    return int(env_num("AGENTCTL_CRASH_LOOP_LAUNCHES", CRASH_LOOP_LAUNCHES))


def knob_crash_loop_quick() -> float:
    return env_num("AGENTCTL_CRASH_LOOP_QUICK", CRASH_LOOP_QUICK)


@dataclass(frozen=True)
class Launch:
    """One fixer launch as the crash loop reads it: its start and end (epoch s; start None when not recorded), how it
    ended, its cause line (launch_cause), its log and its wrapper's last words."""
    started: Optional[float]
    ended: float
    outcome: str
    cause: str
    log: str = ""
    notes: str = ""
    crashed: bool = True

    def quick(self, within: float) -> bool:
        """It died with no result within `within` s of its start (no start recorded: the red ladder's own NOT STARTED
        mark of it)."""
        if not self.crashed:
            return False
        if self.started is None:
            return self.outcome.startswith("NOT STARTED")
        return self.ended - self.started < within


def red_launches(d: dict[str, Any]) -> list[Launch]:
    """A red's fixer launches, oldest first: its history's dispatched leases (a neutral end -- a release by hand, the
    usage limit, a lapse in sleep -- is no launch); one with a result (LANDED, GAVE UP ...) is a launch that lived."""
    return [red_launch(h) for h in d.get("history") or [] if h.get("dispatched") and not launch_neutral(h)]


def red_launch(h: dict[str, Any]) -> Launch:
    """One history entry as a launch: its lease's start (redopen.py red_history), its end, how it ended."""
    outcome = str(h.get("outcome") or "")
    return Launch(float(h["started"]) if h.get("started") else None, iso_epoch(h.get("at")) or 0.0, outcome,
                  launch_cause(h), str(h.get("log") or ""), str(h.get("notes") or ""),
                  crashed=bool(RED_CRASH_OUTCOME.match(outcome)))


def escalation_launches(d: dict[str, Any]) -> list[Launch]:
    """An escalation's fixer launches, oldest first: its record's failed tries (escalations.py failed_try)."""
    out = []
    for x in (d.get("escalation") or {}).get("tries") or []:
        outcome = str(x.get("outcome") or "")
        out.append(Launch(float(x["started"]) if x.get("started") else None, float(x.get("ended") or 0), outcome,
                          launch_cause({"outcome": outcome}), str(x.get("worktree") or "")))
    return out


def crash_loop_of(launches: list[Launch], n: int, quick: float) -> Optional[tuple[str, list[Launch]]]:
    """(the one cause, the launches) when the newest n launches all died within `quick` s of one cause, else None."""
    newest = launches[-n:] if n > 0 else []
    if len(newest) < n or not all(x.quick(quick) for x in newest):
        return None
    causes = {x.cause for x in newest}
    return (causes.pop(), newest) if len(causes) == 1 else None


def owner_cause(cause: str) -> bool:
    """The cause is the owner's alone to clear (the vault, Touch ID, a credential ...: OWNER_SIGNALS, the one list)."""
    return bool(OWNER_SIGNALS.search(cause))


# ---------------------------------------------------------------- the subjects
def subject_launches(store: Any, subject: str) -> Optional[tuple[dict[str, Any], list[Launch]]]:
    """(the subject's item, its launches) while it is live -- a red not decided, an escalation's condition open and not
    yet the owner's -- else None (it cleared)."""
    typ, iid = subject.split(":", 1)
    if typ == "red":
        it = store.item("red", iid)
        if not it or machine_state("red", it["state"]).terminal:
            return None
        return it, red_launches(it["data"])
    it = store.item("condition", iid)
    if not it or it["state"] != "open" or ((it["data"].get("escalation") or {}).get("owner_told")):
        return None
    return it, escalation_launches(it["data"])


def subjects(store: Any) -> Iterable[str]:
    """Every live subject a fixer launches for: the open reds, and the open conditions an escalation fixer works on (but
    a CRASH_LOOP condition itself: its own fixer's tries end at the owner)."""
    for it in store.items("red"):
        if not machine_state("red", it["state"]).terminal and (it["data"].get("history") or []):
            yield f"red:{it['id']}"
    for it in store.items("condition", ["open"]):
        if it["data"].get("kind") != CRASH_LOOP_KIND and (it["data"].get("escalation") or {}).get("tries"):
            yield f"condition:{it['id']}"


def subject_words(subject: str, it: dict[str, Any]) -> str:
    d = it.get("data") or {}
    if subject.startswith("red:"):
        return f"red {it['id']} ({d.get('workflow')} #{(d.get('last_run') or {}).get('number')} [{d.get('job')}])"
    return f"the escalation of condition {it['id']} ({d.get('kind')} {d.get('key')})"


def evidence(launches: list[Launch]) -> list[str]:
    """Each launch, oldest first: when it started and ended, how, its log, its wrapper's last lines."""
    out = []
    for x in launches:
        lived = f"{int(x.ended - x.started)} s" if x.started is not None else "at launch"
        out.append(f"{x.outcome[:300]} (lived {lived}" + (f"; log {x.log}" if x.log else "") + ")")
        out += [f"    {line[:200]}" for line in x.notes.splitlines()[-4:] if line.strip()]
    return out[-EVIDENCE_LINES:]


def crash_loop_text(subject: str, it: dict[str, Any], cause: str, launches: list[Launch], quick: float) -> str:
    who = (" It is the owner's to clear (WAITING ON YOU): no agent can launch past it." if owner_cause(cause)
           else " The fixer harness fails, not the code: an agent finds why the launch dies.")
    return (f"CRASH LOOP: {subject_words(subject, it)}: {len(launches)} fixer launches in a row died within "
            f"{int(quick)} s of starting, each of: {cause}. Not launched again while this is open.{who}")


# ---------------------------------------------------------------- the probe and the step
def probe_fixer_crash_loop(a: dict[str, Any], store: Any, code: Callable[[], str]) -> tuple[bool, str]:
    """The subject's fixer no longer crash-loops: it cleared, a launch lived, or it moved on since the loop opened (a
    fix deployed, its block lifted: moved_on). code: agentctl's code version (selfcode.code_version), stamped on the
    args at the look that opens the loop, so the same probe, rerun, sees a deploy."""
    subject = a["subject"]
    a.setdefault("code", code())
    live = subject_launches(store, subject)
    if live is None:
        return True, f"{subject} cleared (or went to the owner)"
    it, launches = live
    moved = moved_on(a, it, code(), subject.startswith("red:"))
    if moved:
        return True, f"{subject}: {moved}: one launch judges it"
    quick = float(a.get("quick") or CRASH_LOOP_QUICK)
    loop = crash_loop_of(launches, int(a.get("n") or CRASH_LOOP_LAUNCHES), quick)
    if loop is None:
        return True, f"{subject}: a launch lived past {int(quick)} s, or the causes differ"
    return False, crash_loop_text(subject, it, loop[0], loop[1], quick)


def moved_on(a: dict[str, Any], it: dict[str, Any], code: str, red: bool) -> Optional[str]:
    """What changed since the loop opened (at a["at"], under code a["code"]) that may let a launch live: agentctl's code
    (a fix deployed), or the red offered again (its block lifted: the owner's answer, or its bound); else None."""
    if code not in ("", a["code"]):
        return f"agentctl's code changed since the loop opened ({str(a['code'])[:8]} -> {code[:8]})"
    if red and it["state"] == "offered" and float(it.get("entered_at") or 0) > float(a.get("at") or 0):
        return "offered again after the loop opened (its block lifted)"
    return None


MARK = "crash_loop:"


def crash_loop_step(store: Any, now: Callable[[], float], check: Callable[[dict[str, Any]], tuple[bool, str]]) -> list[str]:
    """The server tick's look: each subject newly crash-looping opens its CRASH_LOOP condition -- an alert routed by
    the escalation table, whose being open holds the fixer (escalationtable.py crash_loop_hold); its lines. check: the
    registry's probe_fixer_crash_loop, its store and code bound."""
    n, quick, out = knob_crash_loop_launches(), knob_crash_loop_quick(), []
    for subject in list(subjects(store)):
        live = subject_launches(store, subject)
        loop = crash_loop_of(live[1], n, quick) if live else None
        if loop is not None and float(store.meta_get(MARK + subject) or 0) < loop[1][-1].ended:
            line = open_loop(store, subject, loop[1], check, {"subject": subject, "at": now(), "n": n, "quick": quick})
            out += [line] if line else []
    return out


def open_loop(store: Any, subject: str, launches: list[Launch], check: Callable[[dict[str, Any]], tuple[bool, str]],
              args: dict[str, Any]) -> Optional[str]:
    """The loop's condition opened, with its evidence, and the newest launch marked as opened for (closed since, it opens
    again only after one more launch); its text, or None when the probe passes now."""
    probe = run_probe(check, args)
    if probe.ok is not False:
        return None
    c = open_condition(CRASH_LOOP_KIND, probe.detail, probe, [], key=crash_loop_key(subject), command="red",
                       store=store, grace=0)
    store.meta_set(MARK + subject, str(launches[-1].ended))
    if c:
        store.update_item("condition", c["id"], c["version"], {"evidence": evidence(launches), "subject": subject})
    return probe.detail
