# agentctl, part `ask`: what a red waiting on the owner asks, how the owner is reminded, and the owner's answer.
# Runs inside agentctl's one namespace, after the parts before it in lib/agentctl/__init__.py's PARTS (it uses their names).

# ---------------------------------------------------------------- WAITING ON YOU (the owner, 2026-11-03)
# "make it so blocked always communicates with me and reminds me of the decision". Before: convergence #728 read
# `BLOCKED: owner-decision down` for 15 h -- the owner had decided, the decided land failed its checks, and nobody said
# so; Nix update #1 sat BLOCKED ON OWNER (an App's access to grant) with no reminder, the question buried in ci-why
# text and hundreds of repeating RED OWNER / RED ESCALATE entries. Now:
#   - EVERY OWNER-BLOCKED RED HAS A QUESTION (red_ask): what to decide or do, the exact command or the answer to give,
#     and the red it blocks. The status line and ci-why show `<red> → WAITING ON YOU: <question>`, never a bare cause.
#   - THE OWNER IS TOLD ONCE (red_owner_reminders, the primary's tick): one escalate event -- the laptop's projector
#     writes ONE inbox entry (kind BLOCKED) and its notifier one macOS notification -- then REMINDED at
#     ASK_REMIND_FIRST, doubling up to ASK_REMIND_MAX, by a notification only (no inbox entry: one live reminder per
#     red, never one per tick or per fixer attempt), until the red leaves the block.
#   - THE OWNER ANSWERS with `agentctl decide <red> "<answer>"`: the answer is stored, the block lifted, the red reads
#     IN PROGRESS (decided: <answer>) and is offered again -- its fixer is briefed with the answer (fixer_prompt). If the
#     decided action fails (NOT LANDED, GAVE UP, DIAGNOSED, its fix change rejected), the red goes back on the owner
#     with "you decided X; the action failed: Y" (red_decision_failed): never silently left as it was.
#   - It clears itself as before: the block's own probe (a newer run no longer failing this way, the workflow green),
#     the red verified or cleared; a cleared red is no longer blocked-external, so its reminders stop with it.
ASK_REMIND_FIRST = 3600       # the first reminder this long after the owner was told ($AGENTCTL_ASK_REMIND_FIRST)
ASK_REMIND_MAX = 4 * 3600     # ...then doubling up to this ($AGENTCTL_ASK_REMIND_MAX)
DECISION_FAILED = re.compile(r"\W*(NOT LANDED|GAVE UP|DIAGNOSED)\b")


def decide_cmd(key, answer="<your answer>"):
    return f'~/bin/agentctl decide {key} "{answer}"'


def red_ask(it, b):
    """{q, how, until} of the owner's question for red `it` held by block b (red_block_of), or None when the block
    waits on nothing the owner does (a spent quota resets on its own)."""
    if not b:
        return None
    m, d = b.get("meta") or {}, it["data"]
    if m.get("wait_cause"):
        return None
    key, until = d.get("key"), m.get("until")
    q = m.get("question")
    if m.get("decision"):
        q = q or m.get("decide") or "decide how this red is fixed"
        how = f"answer: `! {decide_cmd(key)}` (look: `{m.get('cmd') or '~/bin/agentctl red status -v'}`)"
    elif m.get("crash_loop"):
        q = q or f"the fixers crash on {red_what(d)}: repair the fixer harness"
        how = f"look: `! {m.get('cmd')}`; then `! {decide_cmd(key, '<what you changed>')}`"
    elif str(m.get("cmd") or "").strip():
        q = q or (f"run `! {m['cmd']}` ({OWNER_CAUSES.get(m.get('owner_cause'), 'an action only you can take')})")
        how = f"it clears itself when {until}; or once done: `! {decide_cmd(key, 'done')}`"
    else:
        return None
    return {"q": " ".join(str(q).split()).replace(HOME, "~"), "how": how.replace(HOME, "~"), "until": until}


def red_decision(d):
    """The owner's decision on red d while it is being acted on, else None."""
    dec = d.get("decision") or {}
    return dec if dec.get("status") == "in-progress" and dec.get("answer") else None


def red_decided_view(d, text, short):
    """(text, short) of a red whose owner's decision is being acted on: IN PROGRESS (decided: <answer>) first."""
    dec = red_decision(d)
    if not dec:
        return text, short
    head = f"IN PROGRESS (decided: {str(dec['answer'])[:80]})"
    return f"{head} -- {text}", f"{head}; {short}"


def ask_interval(n):
    """The wait before reminder n+1 (n reminders so far, the first telling included): 1 h, 2 h, 4 h, 4 h, ..."""
    first = env_num("AGENTCTL_ASK_REMIND_FIRST", ASK_REMIND_FIRST)
    return min(first * 2 ** max(0, int(n) - 1), env_num("AGENTCTL_ASK_REMIND_MAX", ASK_REMIND_MAX))


def red_ask_line(d, ask, n=0):
    what = red_what(d)
    head = f"REMINDER {n}: " if n else ""
    return (f"RED BLOCKED ON OWNER: {head}WAITING ON YOU: {what}: {ask['q'][:400]} -- {ask['how']}"
            + (f" -- auto-clears when {ask['until']}" if ask.get("until") else ""))


def red_ask_mark(store, it, t):
    """The red just moved onto the owner with an escalation of its own (its inbox entry and notification): that is the
    owner told for this spell -- the reminders count from here, never a second telling."""
    if not it or it["state"] != "blocked-external":
        return
    ask = red_ask(it, red_block_of(it, store))
    if ask:
        store.update_item("red", it["id"], it["version"], {"ask": {"q": ask["q"], "since": it["entered_at"],
                                                                  "first_at": t, "last_at": t, "n": 1}})


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
        it, ask = members[0]
        d, a = it["data"], it["data"].get("ask") or {}
        spell = min(m[0]["entered_at"] for m in members)
        fresh = a.get("since") != spell or a.get("q") != ask["q"]
        n = 1 if fresh else int(a.get("n") or 1) + 1
        if not fresh and t - float(a.get("last_at") or 0) < ask_interval(n - 1):
            continue
        line = red_ask_line(d, ask, 0 if fresh else n - 1)
        if len(members) > 1:
            line = line.replace(f"WAITING ON YOU: {red_what(d)}:",
                                f"WAITING ON YOU: {', '.join(red_what(m[0]['data']) for m in members)}:", 1)
        new = {"q": ask["q"], "since": spell, "first_at": t if fresh else a.get("first_at"), "last_at": t, "n": n}
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


def red_decision_failed(store, it, upd, why, t):
    """The owner's decided action failed: the red goes back on the owner with the answer and the failure, a decision
    block like any other (its probe clears it; reminded until it is answered again)."""
    d = {**it["data"], **upd}
    dec = red_decision(d) or {}
    why = " ".join(str(why).split())[:400]
    upd = {**upd, "decision": {**dec, "status": "failed", "failure": why, "failed_at": t}}
    decide = (f"you decided {str(dec.get('answer'))[:200]!r}; the action failed: {why}. Decide again (the same answer "
              "once its cause is fixed, or another)")
    return red_decision_block(store, it, upd, decide, t)


def red_decide(store, o_r, target, answer):
    """`agentctl decide <red> "<answer>"`: the owner's answer to a red waiting on them. Stored on the red, its block
    lifted (only this red leaves a block that holds others): the next tick offers it again, its fixer briefed with it."""
    answer = " ".join(str(answer or "").split())
    if not answer:
        return 2, ["NOT DECIDED: the answer is empty"]
    items = red_targets(store, o_r, target, create=False)
    if not items:
        return 1, [f"NOT DECIDED: {target} names no open red (`~/bin/agentctl red status`)"]
    t, rc, out = store.clock(), 1, []
    for it in items:
        d = it["data"]
        b = red_block_of(it, store)
        ask = red_ask(it, b)
        if not ask:
            out.append(f"NOT DECIDED: {red_what(d)} is not waiting on you ({red_view(store, it, t)[1]})")
            continue
        brow = store.get(b["name"])
        keys = [k for k in b["meta"].get("keys") or [] if k != d["key"]]
        if keys:
            store.set_meta(b["name"], brow["epoch"], brow["secret"], {"keys": keys})
        else:
            store.revoke(b["name"], f"RELEASED: DECIDED by the owner: {answer[:200]}")
        cur = store.item("red", it["id"])
        store.update_item("red", cur["id"], cur["version"],
                          {"decision": {"answer": answer[:600], "question": ask["q"][:600], "at": t,
                                        "status": "in-progress"}, "dispatch_after": 0})
        rc = 0
        out.append(f"DECIDED: {red_what(d)}: {answer[:200]} -- IN PROGRESS: offered again at the next tick, its fixer "
                   "briefed with your answer; if the action fails you are told")
    return rc, out


def cmd_decide(a):
    o_r = slug(a.repo) if getattr(a, "repo", None) else default_repo()
    rc, lines = red_decide(Store(), o_r, a.red, a.answer)
    print("\n".join(lines))
    return rc
