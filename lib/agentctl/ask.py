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
#   - THE QUESTION IS HELD AN HOUR (the owner, 2026-11-17: 28 of 42 questions resolved themselves before he answered,
#     p50 83 min): it is RECORDED at once (the red's blocked-external move, its `ask` with n 0), but the owner is told
#     only ASK_HOLD after its spell began -- unless the red is a GATE red (it holds every production push) or its
#     deadline falls inside the hold: those are told at once. A red that leaves the block meanwhile was never asked.
ASK_HOLD = 3600               # s an owner question is held (recorded, not told) unless it blocks a gate red ($AGENTCTL_ASK_HOLD)
ASK_REMIND_FIRST = 3600       # the first reminder this long after the owner was told ($AGENTCTL_ASK_REMIND_FIRST)
ASK_REMIND_MAX = 4 * 3600     # ...then doubling up to this ($AGENTCTL_ASK_REMIND_MAX)
DECISION_FAILED = re.compile(r"\W*(NOT LANDED|GAVE UP|DIAGNOSED)\b")


def decide_cmd(key, answer="<your answer>"):
    return f'~/bin/agentctl decide {key} "{answer}"'


def trim_words(text, n=160):
    """text shortened at a word boundary with an ellipsis (whole when it fits n): never cut mid-word."""
    text = str(text)
    if len(text) <= n:
        return text
    return text[:n].rsplit(" ", 1)[0].rstrip(" ,;:-") + "\u2026"


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
    elif m.get("vault"):  # red_vault_block: ONE item per vault, resumed by one line (an unlock elsewhere is not theirs)
        how = (f"at the machine: `! {vault_decide_cmd(m['vault'])}` resumes every red on it; each fixer's first "
               "signature then asks Touch ID for its OWN session (your `secrets unlock` never unlocks theirs): answer it")
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
    return (f"RED BLOCKED ON OWNER: {head}WAITING ON YOU: {what}: {ask['q']} -- {ask['how']}"
            + (f" -- auto-clears when {ask['until']}" if ask.get("until") else ""))


def ask_held_until(members, spell, t):
    """Until when the owner's question for these reds ((item, ask) pairs) is held (THE QUESTION IS HELD, above), or
    None: tell now (no hold, a gate red, a deadline inside the hold, or the hold over)."""
    hold = env_num("AGENTCTL_ASK_HOLD", ASK_HOLD)
    until = float(spell) + hold
    if hold <= 0 or t >= until or any(m[0]["data"].get("gate") for m in members):
        return None
    if any(float(m[0].get("deadline") or until + 1) <= until for m in members):
        return None
    return until


def red_owner_block(store, it, line, t, **ev):
    """Move red it onto the owner (blocked-external) with its question: told at once (an escalation: its inbox entry and
    notification) or, held (ask_held_until), recorded with an info event -- the tick tells it when the hold ends."""
    held = ask_held_until([({"data": {**it["data"], **(ev.get("data") or {})}}, None)], t, t) is not None
    moved = red_move(store, it, "blocked-external", line, severity="info" if held else "escalate", **ev)
    red_ask_mark(store, moved, t, told=not held)
    return moved


def red_ask_mark(store, it, t, told=True):
    """The red just moved onto the owner. told: with an escalation of its own (its inbox entry and notification): that
    is the owner told for this spell -- the reminders count from here, never a second telling; else the question is
    held (n 0) and red_owner_reminders tells it when the hold ends."""
    if not it or it["state"] != "blocked-external":
        return
    ask = red_ask(it, red_block_of(it, store))
    if ask:
        rec = {"q": ask["q"], "since": it["entered_at"], "first_at": t, "last_at": t, "n": 1} if told else \
            {"q": ask["q"], "since": it["entered_at"], "held_at": t, "last_at": t, "n": 0}
        store.update_item("red", it["id"], it["version"], {"ask": rec})


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
        if fresh and ask_held_until(members, spell, t) is not None:  # recorded, the owner told when the hold ends
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
    said = (f"agentctl resumed it ({str(dec.get('answer'))[:200]!r})" if dec.get("by") == "agentctl"
            else f"you decided {str(dec.get('answer'))[:200]!r}")
    decide = f"{said}; the action failed: {why}. Decide again (the same answer once its cause is fixed, or another)"
    return red_decision_block(store, it, upd, decide, t)


def red_closed_of(store, key):
    """The newest closed (terminal) red item of a red key or id -- `key`, `key.2`, ... -- or None."""
    base = re.sub(r"\.\d+$", "", key)
    last, n = None, 1
    while n < 200:
        it = store.item("red", base if n == 1 else f"{base}.{n}")
        if it is None:
            break
        last, n = it, n + 1
    if last is None or not machine_state("red", last["state"]).terminal:
        return None
    return last


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
                                                "status": "in-progress"}, "dispatch_after": 0, "ask": None})
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
    rc, lines = red_decide(Store(), o_r, a.red, a.answer)
    print("\n".join(lines))
    return rc
