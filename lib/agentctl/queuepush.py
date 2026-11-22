# agentctl, part `queuepush`: pushing a checked change: the oldest verification first, a red fix's verification kept.
# Runs inside agentctl's one namespace, after the parts before it in lib/agentctl/__init__.py's PARTS (it uses their names).

# THE OLDEST VERIFICATION PUSHES FIRST (2026-11-16, workermem: fully checked by 09:15, then its combined tree verified
# four times, each ~10 min, each on a main a YOUNGER change had pushed onto meanwhile, until a hook change forced a full
# round; 10 h unlanded). Main moving under a verification sends it round again, so a change verifies until no push
# lands during its verification -- with a steady stream of younger changes, never. While a change verifies (a round
# queued or checking with `verify`, not in an infra backoff), every younger checked change holds its push (a red's fix
# excepted) and the verification takes the next free check tree: it lands after ONE more round, and the younger ones
# verify after it.
def verifying(e, t):
    """Whether entry e is in a combined-tree verification round that can run now."""
    return bool(e.get("verify")) and e.get("status") in ("queued", "checking") and not e.get("retry_after", 0) > t


# A RED FIX'S VERIFICATION IS NEVER THROWN AWAY BY AN ORDINARY PUSH (2026-11-19: 08010c, `Fixes-Red: 6`, verified its
# combined tree 09:34-10:18Z; at 10:11Z ratchet-diff -- queued BEFORE it, so not held by the age rule -- pushed, main
# moved, and the fix verified again until 10:36Z, holding 100213 and 102400 behind it the whole time). A red fix goes
# first (changekind.py PRIORITY): while its verification runs, every ordinary checked change holds its push, older or
# younger. Tests: tests/test_backlog_batch.py PushBatchTest ..._never_pushes_under_a_red_fixs_verification.
def older_verifying(todo, e, t, kinds=None):
    """The entry whose combined-tree verification runs now (verifying) and that e's push waits for: one queued before
    e, or a red fix (its kind in kinds, classify_queue) queued at any time; the oldest such, or None."""
    red = lambda o: bool(((kinds or {}).get(o["id"]) or {}).get("red_fix"))
    return next((o for o in sorted(todo, key=lambda o: o["seq"])
                 if o["id"] != e["id"] and (o["seq"] < e["seq"] or red(o)) and verifying(o, t)), None)


def tick_changes(repo, d, now_=None):
    """One tick over the change items: re-queueing re-derived (only the newest entry per worktree and branch runs), an
    executor started for each queued or lease-less checking change (red fixes first, while a slot is free), a
    checking change past its deadline back to queued (infra), a queued one past its deadline escalated, and each
    checked change pushed. Returns what it did."""
    t = now_ if now_ is not None else time.time()
    st = open_store()
    try:
        reds = gate_reds(repo)
    except Exception:
        reds = set()
    facts = queue_facts(reds, st)  # A FIX OF ANY OPEN RED GOES FIRST (above): each change's KIND, recorded on its item
    entries = queued_entries(d)
    kinds = classify_queue(repo, d, entries, facts, t, st)
    todo = sorted(entries, key=lambda e: kind_order(kinds, e))  # THE PRIORITY ORDER (changekind.py PRIORITY)
    newest = {}
    for e in sorted(todo, key=lambda e: e["seq"]):  # entries queued before re-queueing replaced: the newest one runs
        for k, older in list(newest.items()):
            if same_change_source(older, e["worktree"], e.get("branch") or ""):
                supersede(d, older, e["id"], f"{e['worktree']} queued again; only the newest entry runs")
                del newest[k]
        newest[e["id"]] = e
    did = []
    # PUSHES FIRST, THEN THE HOLDS (queue simulator, 2026-11-18): the holds below were read before this tick's pushes,
    # and a checked change's push due holds every backlog batch back (backlog_candidates) -- so the tree a finished
    # check had just freed went to the oldest lone check, never to the batch: under load (every tree busy, a tree
    # freed only by a check ending) no backlog batch ever formed (seed 0 of the simulator's load profile: 40 changes,
    # 40 lone checks, 0 batches). The checked changes are pushed in a first pass; the holds are read again for the
    # second, which starts checks.
    for phase in ("push", "check"):
        held = backlog_held(repo, d, reds, st, t, facts)
        pheld = push_held(repo, d, reds, st, t, facts)
        # THE BATCH KEEPS ITS AGE-ORDER PLACE (review 2026-11-18): this tick runs before the batch tick, and the
        # changes held for a backlog or push batch take no tree, so a tree freed went to the next YOUNGER lone change
        # and the batch tick found none: with a steady stream of lone changes (identity changes, --no-batch) the batch
        # never formed. A lone change younger than the oldest held one never takes the LAST free tree: that is the
        # batch's (a red fix excepted).
        batch_seq = min((x["seq"] for x in todo if x["id"] in held | pheld), default=None)
        did += _tick_changes_pass(repo, d, st, t, facts, kinds, phase, [e for e in todo if e["id"] in newest], todo,
                                  held, pheld, batch_seq)
    return did


def _tick_changes_pass(repo, d, st, t, facts, kinds, phase, mine, todo, held, pheld, batch_seq):
    """One pass of tick_changes over the newest entries: `push` (the checked and interrupted pushes) or `check` (the
    rest: executors started, deadlines). facts: queue_facts; kinds: each entry's kind (classify_queue)."""
    did = []
    for e in mine:
        if e.get("backlog_batch"):  # a backlog batch's member: the batch checks and pushes it (backlog_reconcile)
            continue
        if e["status"] not in (("checked", "pushing") if phase == "push" else ("queued", "checking")):
            continue  # (a push this tick made it landed, waiting or rejected: no check for it)
        it = st.put_item("change", e["id"], ENTRY_STATE.get(e["status"], "queued"), change_item_fields(e))
        state = it["state"]
        if state == "pushing":  # a tick that died mid-push: verified next, never pushed twice blindly
            set_change(d, e, "checked", event={"text": f"{e['id']}: its push was interrupted: verified, then tried again"})
            state = "checked"
        kind = kinds.get(e["id"]) or classify_change(e, facts, t)
        record_change_kind(st, e, kind)  # on its item, now that it surely has one (a no-op when unchanged)
        fix = kind["red_fix"]
        if state == "checked":
            if e.get("retry_after", 0) > t:
                note_hold(st, e, "retry", f"its push is retried at {hhmmz(e['retry_after'])}", t)
                continue
            did += _push_checked(repo, d, st, t, e, fix, todo, pheld, kinds)
            continue
        alive = checker_alive(e["id"])
        if state == "checking" and not alive and t > it["deadline"]:
            requeue_infra(d, e, "its check passed its deadline with no live checker")
            did.append(f"{e['id']}: re-queued (checking past its deadline)")
            continue
        if state == "queued":  # past its deadline: said, re-deadlined -- an escalation only when its hold is stuck
            lap_change_deadline(st, e, "queued", t)  # (A HELD WAIT IS NO ESCALATION, below)
        if state == "queued" and not alive:
            gone = main_conflict(repo, e)
            if gone:  # it no longer applies on main itself: said now, never after a wait for a tree or an older change
                # policy: no-longer-applies
                queue_verdict(d, e, "rejected", f"NOT LANDED: the change no longer applies on {remote()}/main "
                              f"{gone[0][:10]} (main moved since QUEUED{conflict_cause(repo, e.get('base'), gone[0], gone[1])}"
                              f"): rebase onto {remote()}/main and land again (it keeps its place in the queue)",
                              conflict_files=conflict_paths(gone[1]), decided_on=gone[0])
                did.append(f"{e['id']}: rejected (no longer applies on main)")
                continue
            hold = overlap_hold(repo, d, e, todo, facts, t)
            if hold and fix:  # a red fix never waits: its author is told of a conflict, and it goes on
                did += hold_for_overlap(st, d, e, *hold, waits=False, repo=repo)
                hold = None
            if hold:  # OVERLAPPING CHANGES ARE CHECKED AS ONE (below)
                did += hold_for_overlap(st, d, e, *hold, repo=repo)
                o, shared = hold[0], hold[1]
                note_hold(st, e, "overlap", f"its check waits for {o['id']} ({o.get('status')}): both change "
                          f"{', '.join(shared[:3])}" + (f" (+{len(shared) - 3} more)" if len(shared) > 3 else ""), t,
                          blocker=o["id"], files=shared[:10])
                continue
        if e.get("regen_hold") and regen_hold_overdue(st, d, e, t):
            continue
        if alive or e.get("retry_after", 0) > t:
            if not alive:  # said on its item: `land status` shows every wait (changekind.py, no held change silent)
                note_hold(st, e, *retry_hold(e), t)
            continue
        first = None if fix else batch_goes_first(st, d, ("assembling", "pushing"))
        if first:  # the next free check tree is the batch's: to assemble it, or to push from its own tree
            did += behind_batch(e, first, "check")
            note_hold(st, e, "batch", f"its check waits for batch {first['id']} ({first['state']})", t,
                      batch=first["id"])
            continue
        if not fix and e["id"] in pheld:  # THE PUSH BATCH verifies it with others
            if not _behind_said.get((e["id"], "push batch")):
                _behind_said[(e["id"], "push batch")] = True
                did.append(f"{e['id']}: its verify round waits for the next push batch")
            note_hold(st, e, "push-batch", "its verify round waits for the next push batch", t)
            continue
        if not fix and e["id"] in held:  # THE BACKLOG BATCH checks it with others
            if not _behind_said.get((e["id"], "backlog")):
                _behind_said[(e["id"], "backlog")] = True
                did.append(f"{e['id']}: its check waits for the next backlog batch")
            note_hold(st, e, "backlog-batch", "its check waits for the next backlog batch", t)
            continue
        slot = check_tree_slot(repo, d, fix=fix)
        if not slot:
            note_hold(st, e, "trees", "every check tree is busy (a check slot)", t)
            continue
        if not fix and batch_seq is not None and batch_seq < e["seq"] and not check_tree_slot_free(repo, d) \
                and policy_blocks("older-batch", st, e["id"], "the last free check tree kept for the older batch"):
            slot[1].release()  # AGE ORDER: the last free tree is the older changes' batch's
            if not _behind_said.get((e["id"], "older batch")):
                _behind_said[(e["id"], "older batch")] = True
                did.append(f"{e['id']}: its check waits: the last free tree goes to the older changes' batch")
            note_hold(st, e, "older-batch", "the last free check tree goes to the older changes' batch", t)
            continue
        slot[1].release()  # the executor takes it again itself
        note_hold(st, e, "starting", "its executor is starting its check", t)
        spawn_executor(repo, d, e["id"])
        did.append(f"{e['id']}: executor started")
    return did


def retry_hold(e):
    """(kind, why) of a queued change waiting for its retry_after: an infra re-queue's retry, or -- a REGEN NEEDED
    hold (regen_hold) -- its agent's regeneration, which no time brings (2026-11-20, phase8-units: `held: its check is
    retried at 12:03Z (an infra failure)`, read as a retry long overdue: it was the regen hold's end, the next day)."""
    hold = e.get("regen_hold") or {}
    if hold:
        wt = e.get("worktree") or ""
        gone = "" if not wt or os.path.isdir(wt) or host_role() == "server" else f"; its worktree {wt} is gone"
        return "regen", (f"its check waits for its agent's regeneration of {', '.join(hold.get('files') or [])}: "
                         f"`{hold.get('cmd')}` (run again at {day_hhmmz(e['retry_after'])} "
                         f"if unanswered; never rejected for it){gone}")
    why = re.sub(r"^infra:\s*", "", str(e.get("why") or "")).strip()
    return "retry", f"its check is retried at {hhmmz(e['retry_after'])} (an infra failure{': ' + why[:200] if why else ''})"


def _push_checked(repo, d, st, t, e, fix, todo, pheld, kinds=None):
    """The change tick's step for checked entry e: pushed, unless its push is held -- before an open batch it is not
    in (THE BATCH GOES FIRST), for the next push batch, or behind an older change's combined-tree verification; a red
    fix (fix) is held by none of them. The hold is recorded on its item (`land status` prints it) and its deadline laps
    quietly while it is live; a checked change past its deadline that nothing holds escalates (lap_change_deadline)."""
    did = []
    first = None if fix else batch_goes_first(st, d)  # a red fix goes before the batch
    older = None if fix or first or e["id"] in pheld else older_verifying(todo, e, t, kinds)
    if first:  # a solo push now moves main under the batch, and its whole check is thrown away
        did += behind_batch(e, first, "push")
        note_hold(st, e, "batch", f"its push waits for batch {first['id']} ({first['state']}), which it is not in", t,
                  batch=first["id"])
    elif e["id"] in pheld:  # THE PUSH BATCH verifies and pushes it with others (never a red fix: push_candidates)
        if not _behind_said.get((e["id"], "push batch")):
            _behind_said[(e["id"], "push batch")] = True
            did.append(f"{e['id']}: its push waits for the next push batch")
        note_hold(st, e, "push-batch", "its push waits for the next push batch", t)
    elif older:
        did.append(f"{e['id']}: its push waits for {older['id']}'s combined-tree verification")
        note_hold(st, e, "verify", f"its push waits for {older['id']}'s combined-tree verification", t,
                  blocker=older["id"])
    if first or older or e["id"] in pheld:
        lap_change_deadline(st, e, "checked", t)
        return did
    # THE PUSH IS ITS HOLD WHILE IT RUNS (issue 20261119T123823-f7d6c9): clearing the hold before push_change -- which
    # may fetch, find main moved and re-queue the change for a verification -- left a checked change past its deadline
    # with nothing recorded for the seconds it ran, and the watchdog's pass filed it as unheld
    note_hold(st, e, "push", "its push is under way (re-checked first if main moved)", t)
    out = push_change(repo, d, e)
    if out in ("checked", "busy"):  # raced, met the red gate, or the landing lock / push tree was busy: tried again
        note_hold(st, e, "push", "its push waits for the landing lock or a push tree" if out == "busy" else
                  "its last push did not land (a race or the red gate): tried again at the next tick", t)
    else:  # another answer, still checked: nothing holds it now (a new state's set_change cleared it already)
        note_hold(st, e, None, None, t)
    lap_change_deadline(st, e, "checked", t, f"its last push: {out}")
    return did + [f"{e['id']}: {out}"]


def lap_change_deadline(st, e, state, t, note=None):
    """A queued or checked change past its deadline: said and re-deadlined -- quietly while its hold is live, an
    escalation when it is stuck (change_hold_stuck: nothing holds it, its blocker is gone, held too long). note: what
    else the line says (a checked change's last push)."""
    it = st.item("change", e["id"])
    if not it or it["state"] != state or t <= it["deadline"]:
        return
    hold = change_hold_visible(st, it, e, t)
    stuck = change_hold_stuck(st, hold, it, t)
    if stuck and stuck_recovery(hold, stuck) == "rederive":
        # SELF-HEALING (2026-11-20, THE HOLD TABLE): a hold that describes a world that moved -- its blocker ended, or
        # its kind's bound passed -- is dropped, and the tick decides afresh (it records the hold again if one still
        # applies, its clock restarted); said quietly on the change's events, never a page
        if state == "queued":  # its holds are derived after this lap, in the same tick: from nothing
            note_hold(st, e, None, None, t)
        else:  # a checked change's were derived just before it, this tick: their clock restarts
            restart_hold(st, e, t)
        try:
            it = st.item("change", e["id"]) or it
            st.transition("change", e["id"], state, it["version"],
                          event={"kind": "change.rederived", "severity": "info",
                                 "text": f"{e['id']}: {state} for {fmt_age(t - it['entered_at'])}; its hold "
                                         f"({hold.get('why')}) was stuck -- {stuck} -- so it is decided afresh"})
        except StaleVersion:
            pass
        return
    issue = None if hold else file_unheld_change(st, it, t, "agentctl (the change tick)")  # agentctl's bug: an issue,
    # A HELD LAP IS NO INBOX ENTRY (2026-11-19 08:39Z, 08:55Z: movies-fix-replay's "queued for 15 min / 30 min, held:
    # ..." reached the inbox every 15 min -- 93 of the day's 442 entries -- because the lap's event named the worktree
    # as its audience, and the projector writes every event with one). A live hold's lap is an event with no audience:
    # `land status` and the change's events say it; only a stuck or unheld lap goes to its author.
    quiet = bool(hold) and not stuck
    try:  # never an owner page (changekind.py, A CHANGE PAST ITS DEADLINE WITH NO RECORDED HOLD)
        it = st.item("change", e["id"]) or it
        st.transition("change", e["id"], state, it["version"],
                      event={"text": f"{e['id']}: {state} for {fmt_age(t - it['entered_at'])}, "
                                     + (f"held: {hold['why']}" if hold else "nothing holds it")
                                     + (f" -- STUCK: {stuck}" if stuck else "") + (f" ({note})" if note else "")
                                     + (f" -- filed as agentctl issue {issue}" if issue else ""),
                             **({} if quiet else {"audience": {"worktree": e.get("worktree")}}),
                             **({} if stuck and not issue else {"severity": "info"})})
    except StaleVersion:
        pass  # the next tick says it
