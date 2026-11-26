# agentctl, part `reddecision`: a red's owner decision: the `decide:` a fixer or owner ends with, routed to the ask and the block it makes
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import re
from typing import Any
from agentctl.statefile import read_record
from agentctl.redknobs import knob_red_decision_block_max
from agentctl.core import fmt_age, tilde
from agentctl.redrecord import red_what
from agentctl.leases import block_lease
from agentctl.owners import owner_record_path
from agentctl.redask import ask_refusal, ask_split, decide_cmd
from agentctl.redblock import BLOCK_TTL
from agentctl.redopen import red_offer
from agentctl.redcrash import (
    RED_CRASH_NEUTRAL, RED_CRASH_OUTCOME, _block_taken, red_newer_probe, red_owner_block, red_queue_family)


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


def decision_look_command(d: Any) -> str:
    """The command that shows the owner a red's state: `red status -v`, and the family's notebook when it has one."""
    nb = (read_record(owner_record_path(red_queue_family(d))) or {}).get("notebook")
    return "~/bin/agentctl red status -v" + (f"; less {nb}" if nb else "")


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
    cmd = decision_look_command(d)
    until = f"a newer {d.get('workflow')} run than #{run} no longer fails this way"
    maxh = knob_red_decision_block_max()
    text = tilde(f"WAITING ON YOU: {decide[:400]} -- {red_what(d)} waits on it. Answer: `! {decide_cmd(key)}` (its fixer "
            f"is dispatched with your answer). " + (f"Why: DIAGNOSED: {diag[:400]}. " if diag else "") +
            f"Look: `{cmd}`. Auto-clears when {until}, or in {fmt_age(maxh)} (offered again)")
    lname = block_lease(key)
    probe = red_newer_probe(it, d)
    got = store.acquire(lname, f"owner:{DECISION_CAUSE}", BLOCK_TTL, maxh, progress=f"decision@{int(t)}",
                        meta={"kind": "block", "decision": True, "cause": DECISION_CAUSE, "cmd": cmd, "probe": probe,
                              "until": until, "keys": [key], "text": text, "details": diag, "decide": decide,
                              "question": decide, "category": category, "probed_at": t})
    _block_taken(store, it, lname, got, key)
    line = f"RED BLOCKED ON OWNER [{category}]: {text}"
    red_owner_block(store, it, line, t, data={**upd, "cause": DECISION_CAUSE, "block": lname},
                    refs={"key": key, "run": run, "workflow": d.get("workflow"), "verdict": line, "command": "red"})
    return [f"decision needed on {red_what(d)}: blocked on the owner"]
