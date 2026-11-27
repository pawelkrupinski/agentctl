# agentctl, part `ask`: what a red waiting on the owner asks, how the owner is reminded, and the owner's answer.
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import re
import time
from typing import Any
from contextlib import suppress
from agentctl.core import slug
from agentctl.timefmt import hhmmz
from agentctl.redrecord import red_closed_of, red_what
from agentctl.leases import red_block_of
from agentctl.owned import default_repo
from agentctl.redask import ask_held_until, ask_interval, ask_refusal, ask_split, red_ask, red_decision
from agentctl.alerts import alert_close_naming
from agentctl.red import red_move
from agentctl.redclaim import red_targets
from agentctl.redcrash import RED_DECIDE, red_ask_route, red_decision_block
from agentctl.redvault import VAULT_TARGET, red_vault_decide
from agentctl.statemachines import StaleVersion, is_terminal


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


def red_decided_view(d, text, short):
    """(text, short) of a red whose owner's decision is being acted on: ITS STATE FIRST, the decision after it as a
    clean summary (2026-11-22, Order independence #105: "IN PROGRESS (decided: the owner is at the machine (`decide
    vault-movies`: here). No other session's un" led its status hours after that vault block had cleared, cut
    mid-word, and read to the owner as a new question for him). A vault decision only let the fixer use the vault:
    "vault released HH:MMZ"."""
    dec = red_decision(d)
    if not dec:
        return text, short
    at = f" {hhmmz(dec['at'])}" if dec.get("at") else ""
    tail = (f"vault released{at}" if dec.get("category") == "vault"
            else f"decided{at}: {clean_cut(dec['answer'])}")
    return f"{text} ({tail})", f"{short} ({tail})"


def red_ask_line(d, ask, n=0):
    what = red_what(d)
    head = f"REMINDER {n}: " if n else ""
    return (f"RED BLOCKED ON OWNER [{ask.get('category')}]: {head}WAITING ON YOU: {what}: {ask['q']} -- {ask['how']}"
            + (f" -- auto-clears when {ask['until']}" if ask.get("until") else ""))


def ask_groups(store: Any) -> dict[str, list[Any]]:
    """{block name: [(red item, its ask)] by item id} of the owner blocks holding a red with an owner-only question."""
    groups = {}
    for it in store.items("red", ["blocked-external"]):
        b = red_block_of(it, store)
        ask = red_ask(it, b)
        if ask:
            groups.setdefault(b["name"], []).append((it, ask))
    for members in groups.values():
        members.sort(key=lambda x: x[0]["id"])
    return groups


def block_ask_line(d: Any, ask: Any, members: Any, n: int) -> str:
    """red_ask_line of a block's telling, naming every red of the block when it holds several."""
    line = red_ask_line(d, ask, n)
    if len(members) > 1:
        line = line.replace(f"WAITING ON YOU: {red_what(d)}:",
                            f"WAITING ON YOU: {', '.join(red_what(m[0]['data']) for m in members)}:", 1)
    return line


def record_held_ask(store: Any, it: Any, ask: Any, spell: Any, t: float) -> None:
    """Records a held question on the red (the owner is told when the hold ends); a moved red is the next tick's."""
    with suppress(StaleVersion):
        store.update_item("red", it["id"], it["version"], {"ask": {"q": ask["q"], "since": spell,
                                                                   "held_at": t, "last_at": t, "n": 0}})


def send_ask(store: Any, it: Any, line: str, new: Any, fresh: bool) -> bool:
    """Moves the red with its telling (one escalate event; a reminder is a notification alone): False when it moved
    meanwhile (the next tick)."""
    d = it["data"]
    try:
        red_move(store, it, "blocked-external", line, data={"ask": new}, severity="escalate",
                 kind="red.ask" if fresh else "red.remind",
                 refs={"key": d.get("key"), "run": (d.get("last_run") or {}).get("number"),
                       "workflow": d.get("workflow"), "verdict": line, "command": "red",
                       **({} if fresh else {"no_inbox": True})})
    except StaleVersion:
        return False
    return True


def ask_moved(a: Any, ask: Any, spell: Any) -> bool:
    """True when the block's recorded ask is of another spell or another question."""
    return a.get("since") != spell or a.get("q") != ask["q"]


def tell_block(store: Any, members: Any, it: Any, ask: Any, spell: Any, n: int, t: float) -> str | None:
    """Tells (n == 1) or reminds the owner of a block, through its speaking red: the line told, or None when the red
    moved meanwhile."""
    fresh, a, d = n == 1, it["data"].get("ask") or {}, it["data"]
    line = block_ask_line(d, ask, members, 0 if fresh else n - 1)
    new = {"q": ask["q"], "since": spell, "first_at": t if fresh else a.get("first_at"), "last_at": t, "n": n,
           "category": ask.get("category")}
    if not send_ask(store, it, line, new, fresh):
        return None
    return ("told the owner: " if fresh else f"reminded the owner ({n - 1}): ") + red_what(d)


def telling_due(store: Any, members: Any, it: Any, ask: Any, spell: Any, t: float) -> int | None:
    """The number of the telling due now -- 1 the owner is told, n > 1 reminded -- or None: a held question is recorded
    (the owner told when the hold ends), a reminder waits for its interval."""
    a = it["data"].get("ask") or {}
    moved = ask_moved(a, ask, spell)
    if not (moved or int(a.get("n") or 0) == 0):  # told before: a reminder
        n = int(a.get("n") or 1) + 1
        return n if t - float(a.get("last_at") or 0) >= ask_interval(n - 1) else None
    if ask_held_until(members, spell, t, store) is None:  # never told yet
        return 1
    if moved:
        record_held_ask(store, it, ask, spell, t)
    return None


def remind_block(store: Any, members: Any, t: float) -> str | None:
    """What one owner block's tick tells the owner (told, or reminded), or None when nothing is due."""
    # THE MEMBER THAT CARRIES THE TELLING speaks for the block: a red joining it later (sorting first) is no second
    # telling (2026-11-16: ONE owner item for N reds waiting on the locked vault)
    it, ask = max(members, key=lambda x: float((x[0]["data"].get("ask") or {}).get("last_at") or 0))
    spell = min(m[0]["entered_at"] for m in members)
    n = telling_due(store, members, it, ask, spell, t)
    return tell_block(store, members, it, ask, spell, n, t) if n else None


def red_owner_reminders(store, t):
    """Each owner block's question: told once when its spell (or its question) starts -- one escalate event, which the
    laptop's projector makes ONE inbox entry and its notifier one notification -- then reminded at ask_interval by a
    notification alone, until it lapses (its probe, the owner's answer, the red verified or cleared). ONE per block: a
    cause holding several reds asks once, naming them all (on its first red; the others carry the same record)."""
    told = (remind_block(store, members, t) for members in ask_groups(store).values())
    return [line for line in told if line]


def owner_asks(store):
    """THE OWNER'S PENDING DECISIONS, structured (state.json `asks`, written by every tick's snapshot): one row per
    owner block whose question has been TOLD (n >= 1; a held question is not the owner's yet) -- {block, keys, reds,
    category, q, how, until, since}. Read from the reds and their block leases, never from text: an inbox entry or an
    issue QUOTING "WAITING ON YOU" is no decision (2026-11-19: the movies owner-decisions hook put an old issue's quote
    of a red's question to the owner as a pending decision). Only an ask naming an OWNER_ONLY category (red_ask)."""
    out = []
    for name, members in sorted(ask_groups(store).items()):
        if not any(int((m[0]["data"].get("ask") or {}).get("n") or 0) >= 1 for m in members):
            continue
        ask = members[0][1]
        out.append({"block": name, "keys": [m[0]["data"].get("key") for m in members],
                    "reds": [red_what(m[0]["data"]) for m in members], "category": ask.get("category"),
                    "q": ask["q"], "how": ask["how"], "until": ask.get("until"),
                    "since": min(float(m[0]["entered_at"]) for m in members)})
    return out


def failed_decision_question(why: str, dec: Any) -> tuple[Any, Any, Any]:
    """(own, category, asked) of a failed decision: `own` the DIAGNOSED's own owner-only `decide[<category>]` match, else
    the category the decision was asked in and what was asked."""
    diagnosed = why.startswith("DIAGNOSED")
    own = RED_DECIDE.search(why) if diagnosed else None
    if own:
        return own, ask_split(own.group(1) or "")[0], own.group(2)
    return None, None if diagnosed else dec.get("category"), f"{why} (asked: {dec.get('question') or ''})"


def red_decision_failed(store, it, upd, why, t):
    """The owner's decided action failed. Back on the owner only when the failure is itself owner-only: a DIAGNOSED
    naming its own owner-only `decide[<category>]`, or a NOT LANDED / GAVE UP of a decision asked in an owner-only
    category. A failure whose cause is ordinary -- core #1053, 2026-11-19: the owner answered "852acad" and the fixer
    then DIAGNOSED a plain test race, which the standing rules decide -- goes back to the fixer to proceed
    (red_ask_route), the owner never re-asked for it."""
    dec = red_decision({**it["data"], **upd}) or {}
    why = " ".join(str(why).split())[:400]
    own, category, asked = failed_decision_question(why, dec)
    if ask_refusal(category, asked):
        done = {**upd, "decision": {**dec, "status": "done", "failure": why, "failed_at": t}}
        return red_ask_route(store, it, done, "the decided action's new cause is ordinary: the standing rules decide it"
                             if why.startswith("DIAGNOSED") else ask_refusal(category, asked), why, t)
    upd = {**upd, "decision": {**dec, "status": "failed", "failure": why, "failed_at": t}}
    if own:  # its own owner-only question, asked as it is
        return red_decision_block(store, it, upd, f"[{category}] {own.group(2).strip()}", t)
    said = (f"agentctl resumed it ({str(dec.get('answer'))[:200]!r})" if dec.get("by") == "agentctl"
            else f"you decided {str(dec.get('answer'))[:200]!r}")
    decide = (f"{said}; the action failed: {why}. Decide again (the same answer once its cause is fixed, or another; "
              f"you were asked: {str(dec.get('question') or '')[:300]})")
    return red_decision_block(store, it, upd, decide, t, category=category)


def resolved_red_reply(closed: Any, target: Any, answer: str) -> tuple[int, list[str]]:
    """The reply to an answer for a red that had cleared days before (2026-11-06): no refusal, resolved; its alerts
    acknowledged."""
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(float(closed.get("entered_at") or 0)))
    why = f"already resolved (red closed {when}, {closed['state']})"
    n = alert_close_naming(re.compile(r"\bdecide " + re.escape(closed["data"].get("key") or target) + r"\b"),
                           f"{why}; the owner answered: {answer[:200]}")
    return 0, [f"{red_what(closed['data'])}: {why}: nothing to decide"
               + (f"; its alert{'s' if n > 1 else ''} acknowledged" if n else "")]


def release_block_for(store: Any, b: Any, d: Any, answer: str) -> None:
    """Lifts the red's key from its block (the whole block when it was the only red it held)."""
    brow = store.get(b["name"])
    keys = [k for k in b["meta"].get("keys") or [] if k != d["key"]]
    if keys:
        store.set_meta(b["name"], brow["epoch"], brow["secret"], {"keys": keys})
    else:
        store.revoke(b["name"], f"RELEASED: DECIDED by the owner: {answer[:200]}")


def asked_question(d: Any, ask: Any) -> Any:
    """The question a red's owner answer replies to."""
    return (ask or {}).get("q") or (d.get("ask") or {}).get("q") or (d.get("decision") or {}).get("question") \
        or ((d.get("history") or [{}])[-1].get("outcome") or "")


def store_decision(store: Any, it: Any, d: Any, ask: Any, answer: str, t: float) -> str | None:
    """None once the owner's answer is stored on the red, else why it was not (the red already ended, or the store kept
    moving it: StaleVersion, the store ticks meanwhile -- re-read and re-apply, never a traceback)."""
    decision = {"answer": answer[:600], "question": str(asked_question(d, ask))[:600], "at": t, "status": "in-progress",
                "category": (ask or {}).get("category") or (d.get("ask") or {}).get("category")}
    for _try in range(10):
        cur = store.item("red", it["id"])
        if is_terminal("red", cur["state"]):
            return f"NOT DECIDED: {red_what(d)} is already {cur['state']}"
        with suppress(StaleVersion):
            store.update_item("red", cur["id"], cur["version"], {"decision": decision, "dispatch_after": 0, "ask": None})
            return None
    return f"NOT DECIDED: {red_what(d)}: the store kept moving it; run the same command again"


def decide_one(store: Any, it: Any, answer: str, t: float) -> tuple[bool, str]:
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


def red_decide(store, o_r, target, answer, *, ctx):
    """`agentctl decide <red> "<answer>"`: the owner's answer to a red waiting on them. Stored on the red, its block
    lifted (only this red leaves a block that holds others): the next tick offers it again, its fixer briefed with it."""
    answer = " ".join(str(answer or "").split())
    if not answer:
        return 2, ["NOT DECIDED: the answer is empty"]
    vault = VAULT_TARGET.fullmatch(str(target))
    if vault:  # `decide vault-<ns>`: the owner at the machine for every red on that vault's one lease
        return red_vault_decide(store, vault.group(1), answer)
    items = red_targets(store, o_r, target, create=False, ctx=ctx)
    if not items:
        closed = red_closed_of(store, str(target).lstrip("#"))
        if closed:
            return resolved_red_reply(closed, target, answer)
        return 1, [f"NOT DECIDED: {target} names no open red (`~/bin/agentctl red status`)"]
    t = store.clock()
    results = [decide_one(store, it, answer, t) for it in items]
    return (0 if any(ok for ok, _ in results) else 1), [line for _, line in results]


def cmd_decide(a, store, *, ctx):
    o_r = slug(a.repo) if getattr(a, "repo", None) else default_repo()
    rc, lines = red_decide(store, o_r, a.red, a.answer, ctx=ctx)
    print("\n".join(lines))
    return rc
