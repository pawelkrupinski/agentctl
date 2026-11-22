# agentctl, part `ask`: what a red waiting on the owner asks, how the owner is reminded, and the owner's answer.
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import re
import time
from agentctl.core import slug
from agentctl.timefmt import hhmmz
from agentctl.store import StaleVersion, open_store
from agentctl.leases import red_block_of
from agentctl.redrecord import RED_TERMINAL, red_closed_of, red_what
from agentctl.owned import default_repo
from agentctl.redask import ask_held_until, ask_interval, ask_refusal, ask_split, red_ask, red_decision
from agentctl.alerts import alert_close_naming
from agentctl.redmove import red_move
from agentctl.redclaim import red_targets
from agentctl.redcrash import RED_DECIDE, red_ask_route, red_decision_block
from agentctl.redvault import VAULT_TARGET, red_vault_decide


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


def red_owner_reminders(store, t):
    """Each owner block's question: told once when its spell (or its question) starts -- one escalate event, which the
    laptop's projector makes ONE inbox entry and its notifier one notification -- then reminded at ask_interval by a
    notification alone, until it lapses (its probe, the owner's answer, the red verified or cleared). ONE per block: a
    cause holding several reds asks once, naming them all (on its first red; the others carry the same record)."""
    out, groups = [], {}
    for it in store.items("red", ["blocked-external"]):
        b = red_block_of(it, store)
        ask = red_ask(it, b)
        if ask:
            groups.setdefault(b["name"], []).append((it, ask))
    for _name, members in groups.items():
        members.sort(key=lambda x: x[0]["id"])
        # THE MEMBER THAT CARRIES THE TELLING speaks for the block: a red joining it later (sorting first) is no second
        # telling (2026-11-16: ONE owner item for N reds waiting on the locked vault)
        it, ask = max(members, key=lambda x: float((x[0]["data"].get("ask") or {}).get("last_at") or 0))
        d, a = it["data"], it["data"].get("ask") or {}
        spell = min(m[0]["entered_at"] for m in members)
        fresh = a.get("since") != spell or a.get("q") != ask["q"] or int(a.get("n") or 0) == 0  # never told yet
        if fresh and ask_held_until(members, spell, t, store) is not None:  # recorded, the owner told when the hold ends
            if a.get("since") != spell or a.get("q") != ask["q"]:
                try:
                    store.update_item("red", it["id"], it["version"], {"ask": {"q": ask["q"], "since": spell,
                                                                               "held_at": t, "last_at": t, "n": 0}})
                except StaleVersion:
                    pass
            continue
        n = 1 if fresh else int(a.get("n") or 1) + 1
        if not fresh and t - float(a.get("last_at") or 0) < ask_interval(n - 1):
            continue
        line = red_ask_line(d, ask, 0 if fresh else n - 1)
        if len(members) > 1:
            line = line.replace(f"WAITING ON YOU: {red_what(d)}:",
                                f"WAITING ON YOU: {', '.join(red_what(m[0]['data']) for m in members)}:", 1)
        new = {"q": ask["q"], "since": spell, "first_at": t if fresh else a.get("first_at"), "last_at": t, "n": n,
               "category": ask.get("category")}
        try:
            red_move(store, it, "blocked-external", line, data={"ask": new}, severity="escalate",
                     kind="red.ask" if fresh else "red.remind",
                     refs={"key": d.get("key"), "run": (d.get("last_run") or {}).get("number"),
                           "workflow": d.get("workflow"), "verdict": line, "command": "red",
                           **({} if fresh else {"no_inbox": True})})
        except StaleVersion:
            continue  # moved meanwhile: the next tick
        out.append(("told the owner: " if fresh else f"reminded the owner ({n - 1}): ") + red_what(d))
    return out


def owner_asks(store):
    """THE OWNER'S PENDING DECISIONS, structured (state.json `asks`, written by every tick's snapshot): one row per
    owner block whose question has been TOLD (n >= 1; a held question is not the owner's yet) -- {block, keys, reds,
    category, q, how, until, since}. Read from the reds and their block leases, never from text: an inbox entry or an
    issue QUOTING "WAITING ON YOU" is no decision (2026-11-19: the movies owner-decisions hook put an old issue's quote
    of a red's question to the owner as a pending decision). Only an ask naming an OWNER_ONLY category (red_ask)."""
    groups = {}
    for it in store.items("red", ["blocked-external"]):
        b = red_block_of(it, store)
        ask = red_ask(it, b)
        if ask:
            groups.setdefault(b["name"], []).append((it, ask))
    out = []
    for name, members in sorted(groups.items()):
        members.sort(key=lambda x: x[0]["id"])
        if not any(int((m[0]["data"].get("ask") or {}).get("n") or 0) >= 1 for m in members):
            continue
        ask = members[0][1]
        out.append({"block": name, "keys": [m[0]["data"].get("key") for m in members],
                    "reds": [red_what(m[0]["data"]) for m in members], "category": ask.get("category"),
                    "q": ask["q"], "how": ask["how"], "until": ask.get("until"),
                    "since": min(float(m[0]["entered_at"]) for m in members)})
    return out


def red_decision_failed(store, it, upd, why, t):
    """The owner's decided action failed. Back on the owner only when the failure is itself owner-only: a DIAGNOSED
    naming its own owner-only `decide[<category>]`, or a NOT LANDED / GAVE UP of a decision asked in an owner-only
    category. A failure whose cause is ordinary -- core #1053, 2026-11-19: the owner answered "852acad" and the fixer
    then DIAGNOSED a plain test race, which the standing rules decide -- goes back to the fixer to proceed
    (red_ask_route), the owner never re-asked for it."""
    d = {**it["data"], **upd}
    dec = red_decision(d) or {}
    why = " ".join(str(why).split())[:400]
    own = RED_DECIDE.search(why) if why.startswith("DIAGNOSED") else None
    category = ask_split((own.group(1) or "") if own else "")[0] if own else \
        (None if why.startswith("DIAGNOSED") else dec.get("category"))
    asked = own.group(2) if own else f"{why} (asked: {dec.get('question') or ''})"
    if ask_refusal(category, asked):
        done = {**upd, "decision": {**dec, "status": "done", "failure": why, "failed_at": t}}
        return red_ask_route(store, it, done, "the decided action's new cause is ordinary: the standing rules decide it"
                             if why.startswith("DIAGNOSED") else ask_refusal(category, asked), why, t)
    if own:  # its own owner-only question, asked as it is
        upd = {**upd, "decision": {**dec, "status": "failed", "failure": why, "failed_at": t}}
        return red_decision_block(store, it, upd, f"[{category}] {own.group(2).strip()}", t)
    upd = {**upd, "decision": {**dec, "status": "failed", "failure": why, "failed_at": t}}
    said = (f"agentctl resumed it ({str(dec.get('answer'))[:200]!r})" if dec.get("by") == "agentctl"
            else f"you decided {str(dec.get('answer'))[:200]!r}")
    decide = (f"{said}; the action failed: {why}. Decide again (the same answer once its cause is fixed, or another; "
              f"you were asked: {str(dec.get('question') or '')[:300]})")
    return red_decision_block(store, it, upd, decide, t, category=category)


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
        if closed:  # 2026-11-06: the owner answered a question whose red had cleared days before: no refusal, resolved
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(float(closed.get("entered_at") or 0)))
            why = f"already resolved (red closed {when}, {closed['state']})"
            n = alert_close_naming(re.compile(r"\bdecide " + re.escape(closed["data"].get("key") or target) + r"\b"),
                                   f"{why}; the owner answered: {answer[:200]}")
            return 0, [f"{red_what(closed['data'])}: {why}: nothing to decide"
                       + (f"; its alert{'s' if n > 1 else ''} acknowledged" if n else "")]
        return 1, [f"NOT DECIDED: {target} names no open red (`~/bin/agentctl red status`)"]
    t, rc, out = store.clock(), 1, []
    for it in items:
        d = it["data"]
        b = red_block_of(it, store)
        # THE OWNER'S ANSWER IS ALWAYS ACCEPTED while the red is open, whatever its state (a newer run moved it off
        # WAITING ON YOU to "offered": the question was still the owner's to answer)
        ask = red_ask(it, b) if b else None
        if b:
            brow = store.get(b["name"])
            keys = [k for k in b["meta"].get("keys") or [] if k != d["key"]]
            if keys:
                store.set_meta(b["name"], brow["epoch"], brow["secret"], {"keys": keys})
            else:
                store.revoke(b["name"], f"RELEASED: DECIDED by the owner: {answer[:200]}")
        question = (ask or {}).get("q") or (d.get("ask") or {}).get("q") or (d.get("decision") or {}).get("question") \
            or ((d.get("history") or [{}])[-1].get("outcome") or "")
        for _try in range(10):  # StaleVersion: the store ticks meanwhile -- re-read and re-apply, never a traceback
            cur = store.item("red", it["id"])
            if cur["state"] in RED_TERMINAL:
                break
            try:
                store.update_item("red", cur["id"], cur["version"],
                                  {"decision": {"answer": answer[:600], "question": str(question)[:600], "at": t,
                                                "status": "in-progress",
                                                "category": (ask or {}).get("category")
                                                or (d.get("ask") or {}).get("category")},
                                   "dispatch_after": 0, "ask": None})
                break
            except StaleVersion:
                continue
        else:
            out.append(f"NOT DECIDED: {red_what(d)}: the store kept moving it; run the same command again")
            continue
        if cur["state"] in RED_TERMINAL:
            out.append(f"NOT DECIDED: {red_what(d)} is already {cur['state']}")
            continue
        rc = 0
        out.append(f"DECIDED: {red_what(d)}: {answer[:200]} -- IN PROGRESS: offered again at the next tick, its fixer "
                   "briefed with your answer; if the action fails you are told")
    return rc, out


def cmd_decide(a):
    o_r = slug(a.repo) if getattr(a, "repo", None) else default_repo()
    rc, lines = red_decide(open_store(), o_r, a.red, a.answer)
    print("\n".join(lines))
    return rc
