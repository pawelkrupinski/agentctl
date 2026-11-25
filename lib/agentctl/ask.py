# agentctl, part `ask`: what a red waiting on the owner asks, how the owner is reminded, and the owner's answer.
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import re
import time
from contextlib import suppress
from agentctl.core import slug
from agentctl.timefmt import hhmmz
from agentctl.redrecord import RED_TERMINAL, red_closed_of, red_what
from agentctl.leases import red_block_of
from agentctl.owned import default_repo
from agentctl.redask import ask_held_until, ask_interval, ask_refusal, ask_split, red_ask, red_decision
from agentctl.alerts import alert_close_naming
from agentctl.redmove import red_move
from agentctl.redclaim import red_targets
from agentctl.redcrash import RED_DECIDE, red_ask_route, red_decision_block
from agentctl.redvault import VAULT_TARGET, red_vault_decide
from agentctl.statemachines import StaleVersion


DECISION_FAILED = re.compile(r"\W*(NOT LANDED|GAVE UP|DIAGNOSED)\b")


def trim_words(text, n=160):
    """text shortened at a word boundary with an ellipsis (whole when it fits n): never cut mid-word."""
    text = str(text)
    if len(text) <= n:
        return text
    return text[:n].rsplit(" ", 1)[0].rstrip(" ,;:-") + "\u2026"


def clean_cut(text, n=60):
    """text at most n characters, cut at a word boundary with an ellipsis, never mid-word."""
    text = " ".join(str(text or "").split())
    if len(text) <= n:
        return text
    cut = text[:n].rsplit(" ", 1)[0].rstrip(" ,;:.(-")
    return (cut or text[:n]) + "…"


def resolved_red_reply(closed, target, answer):
    """The reply to an answer for a red that had cleared days before (2026-11-06): no refusal, resolved; its alerts
    acknowledged."""
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(float(closed.get("entered_at") or 0)))
    why = f"already resolved (red closed {when}, {closed['state']})"
    n = alert_close_naming(re.compile(r"\bdecide " + re.escape(closed["data"].get("key") or target) + r"\b"),
                           f"{why}; the owner answered: {answer[:200]}")
    return 0, [f"{red_what(closed['data'])}: {why}: nothing to decide"
               + (f"; its alert{'s' if n > 1 else ''} acknowledged" if n else "")]


def release_block_for(store, b, d, answer):
    """Lifts the red's key from its block (the whole block when it was the only red it held)."""
    brow = store.get(b["name"])
    keys = [k for k in b["meta"].get("keys") or [] if k != d["key"]]
    if keys:
        store.set_meta(b["name"], brow["epoch"], brow["secret"], {"keys": keys})
    else:
        store.revoke(b["name"], f"RELEASED: DECIDED by the owner: {answer[:200]}")


def asked_question(d, ask):
    """The question a red's owner answer replies to."""
    return (ask or {}).get("q") or (d.get("ask") or {}).get("q") or (d.get("decision") or {}).get("question") \
        or ((d.get("history") or [{}])[-1].get("outcome") or "")


def store_decision(store, it, d, ask, answer, t):
    """None once the owner's answer is stored on the red, else why it was not (the red already ended, or the store kept
    moving it: StaleVersion, the store ticks meanwhile -- re-read and re-apply, never a traceback)."""
    decision = {"answer": answer[:600], "question": str(asked_question(d, ask))[:600], "at": t, "status": "in-progress",
                "category": (ask or {}).get("category") or (d.get("ask") or {}).get("category")}
    for _try in range(10):
        cur = store.item("red", it["id"])
        if cur["state"] in RED_TERMINAL:
            return f"NOT DECIDED: {red_what(d)} is already {cur['state']}"
        with suppress(StaleVersion):
            store.update_item("red", cur["id"], cur["version"], {"decision": decision, "dispatch_after": 0, "ask": None})
            return None
    return f"NOT DECIDED: {red_what(d)}: the store kept moving it; run the same command again"


def decide_one(store, it, answer, t):
    """(decided, line) of the owner's answer to one red."""
    d = it["data"]
    b = red_block_of(it, store)
    # THE OWNER'S ANSWER IS ALWAYS ACCEPTED while the red is open, whatever its state (a newer run moved it off
    # WAITING ON YOU to "offered": the question was still the owner's to answer)
    ask = red_ask(it, b) if b else None
    if b:
        release_block_for(store, b, d, answer)
    refused = store_decision(store, it, d, ask, answer, t)
    if refused:
        return False, refused
    return True, (f"DECIDED: {red_what(d)}: {answer[:200]} -- IN PROGRESS: offered again at the next tick, its fixer "
                  "briefed with your answer; if the action fails you are told")


def red_decide(store, o_r, target, answer):
    """`agentctl decide <red> "<answer>"`: the owner's answer to a red waiting on them. Stored on the red, its block
    lifted (only this red leaves a block that holds others): the next tick offers it again, its fixer briefed with it."""
    answer = " ".join(str(answer or "").split())
    if not answer:
        return 2, ["NOT DECIDED: the answer is empty"]
    vault = VAULT_TARGET.fullmatch(str(target))
    if vault:  # `decide vault-<ns>`: the owner at the machine for every red on that vault's one lease
        return red_vault_decide(store, vault.group(1), answer)
    items = red_targets(store, o_r, target, create=False)
    if not items:
        closed = red_closed_of(store, str(target).lstrip("#"))
        if closed:
            return resolved_red_reply(closed, target, answer)
        return 1, [f"NOT DECIDED: {target} names no open red (`~/bin/agentctl red status`)"]
    t = store.clock()
    results = [decide_one(store, it, answer, t) for it in items]
    return (0 if any(ok for ok, _ in results) else 1), [line for _, line in results]


def cmd_decide(a, store):
    o_r = slug(a.repo) if getattr(a, "repo", None) else default_repo()
    rc, lines = red_decide(store, o_r, a.red, a.answer)
    print("\n".join(lines))
    return rc
