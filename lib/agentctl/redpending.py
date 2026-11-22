# agentctl, part `redpending`: a pending fix: following its work to main and judging it (red_fix_pending).
# Runs inside agentctl's one namespace, after the parts before it in lib/agentctl/__init__.py's PARTS (it uses their names).

# A FIX FOLLOWS ITS WORK, NOT ITS CHANGE ID (2026-10-31, Main #2877): "fix 20261031-073114-movies-wt-flakes-9f6bc4
# waiting to land (joined)" for hours -- the change was CANCELLED (the store's item said so; the entry the facts read
# still said `waiting`) and the same worktree re-queued as ...-ed6118, which LANDED d677b1bb6. The store's change item
# is the truth about a change; a fix whose change ended without landing follows its worktree to the later change that
# carries its work (open: waiting on it; landed: verifying from its landing), or its own commits found on main by
# content; with neither, it is offered again once RED_FIX_REQUEUE_GRACE passed since the cancel (a re-queue is quick).
CHANGE_ENDED = ("cancelled", "superseded", "rejected")
RED_FIX_REQUEUE_GRACE = 900  # $AGENTCTL_RED_FIX_REQUEUE_GRACE


def red_change(store, eid):
    return red_change_read(store, eid)[0]  # an unread store decides nothing: the facts stand


def red_change_read(store, eid):
    """(the change's store item or None, why it could not be read or None): a caller that must tell "no such change"
    from "the store did not answer" (red_fix_held) reads both."""
    try:
        return (store.item("change", eid) if eid else None), None
    except Exception as e:
        return None, str(e) or type(e).__name__


def red_fix_fresh(store, fix):
    """The fix red_fix_of read from the facts, corrected by its change's store item: None when that change ended
    without landing, landed when the store says so; as read when the store has no word on it."""
    if not fix or not fix.get("entry") or fix.get("status") == "landed":
        return fix
    ch = red_change(store, fix["entry"])
    if not ch:
        return fix
    if ch["state"] in CHANGE_ENDED:
        return None
    if ch["state"] == "landed":
        sha = ch["data"].get("landed_sha") or ch["data"].get("tip")
        return {**fix, "status": "landed", "sha": sha, "at": float(ch.get("entered_at") or store.clock())} if sha else fix
    return {**fix, "status": ENTRY_STATUS.get(ch["state"], fix.get("status"))}


def red_fix_current(store, it, facts):
    """THE fix on its way for this red: red_fix_of's, corrected by the store (red_fix_fresh) -- and, once the store has
    turned it into a landed sha, never one the red refuted or set aside. red_fix_of can only judge the shas it reads; an
    entry its file still calls open is upgraded to its landed sha afterwards. 2026-11-11, Verify scrape recording #23
    (784badc4406e.3): a stale `waiting` entry came back as the refuted 547709c1e2 every tick, offered -> fix-pending ->
    offered 593 times, and no fixer was ever offered the red. Each candidate is judged in turn (red_fix_of): one set
    aside never hides the next."""
    return red_fix_of(it, facts, store)


# A FOLLOWED FIX IS A LATER CHANGE (2026-11-19, Alert backtest #4, 5254bffda9d2): its fix ...-8b34c0 was cancelled and
# its worktree re-queued a minute later, but the red followed it to ...-130, a change that worktree (one owner's, reused
# for days) landed on 11-11 -- the change items carried no `submitted_at`, so "later than the cancelled one" was "later
# than 0", the oldest first. Its phantom tip b6d3d8235 was no fix for this spell (red_fix_of), so the same tick's
# invariant set it aside and escalated it: "UNVERIFIED: INVARIANT: no fix on its way for 0 min in fix-pending". A
# change's submission is its `submitted_at`, else its id's stamp; one with neither follows nothing.
def change_submitted_at(ch):
    """When change item ch was submitted (epoch): its `submitted_at`, else its id's YYYYMMDD-HHMMSS stamp; None."""
    v = (ch.get("data") or {}).get("submitted_at")
    if v:
        return float(v)
    age = entry_id_age(str(ch.get("id") or ""), 0.0)
    return -age if age is not None else None


def red_fix_follow(store, f, repo, t):
    """(fix, its change's state) for a not-yet-landed fix f whose change ENDED without landing: the change that carries
    its work since -- the earliest later change of the same worktree (landed, or still open) -- or f's own commits
    landed on main by content; (None, state) when there is none; (None, None) when its change has not ended."""
    ch = red_change(store, f.get("entry"))
    if not ch or ch["state"] not in CHANGE_ENDED:
        return None, None
    cd = ch["data"] or {}
    wt, since = cd.get("worktree"), change_submitted_at(ch)
    later = []
    try:
        later = sorted((o for o in store.items("change") if o["id"] != ch["id"] and wt and o["data"].get("worktree") == wt
                        and since is not None and (change_submitted_at(o) or 0) >= since
                        and o["state"] not in CHANGE_ENDED),
                       key=lambda o: change_submitted_at(o) or 0)
    except Exception:
        pass
    for o in later:
        od = o["data"]
        sha = od.get("landed_sha") or od.get("tip")
        if o["state"] == "landed" and sha:
            return {"entry": o["id"], "status": "landed", "sha": sha, "at": float(o.get("entered_at") or t),
                    "followed": f.get("entry"), "author": f.get("author")}, ch["state"]
        if o["state"] != "landed":
            return {"entry": o["id"], "status": ENTRY_STATUS.get(o["state"], o["state"]), "followed": f.get("entry"),
                    "author": f.get("author")}, ch["state"]
    head, base = cd.get("head"), cd.get("base")
    if repo and head:
        commits, left = cherry_landed(repo, f"refs/remotes/{remote()}/main", head, *((base,) if base else ()))
        if commits and left == []:
            return {"entry": f.get("entry"), "status": "landed", "sha": head, "at": t, "followed": f.get("entry"),
                    "by_content": True, "author": f.get("author")}, ch["state"]
    return None, ch["state"]


def red_fix_pending(store, it, fix, gone, obs, snap, facts, repo, o_r, t):
    d = it["data"]
    f = d.get("fix") or {}
    if f.get("status") == RED_WAITS_ON and not (obs is not None and gone):
        return red_waits_on_step(store, it, f, repo, t)
    if f.get("sha") and red_fix_is_set_aside(f["sha"], red_set_aside(d, facts), facts):
        # PENDING ON A FIX IT ALREADY REFUTED (or set aside): no run can verify it now; back to a fixer
        if fix and fix.get("sha") != f["sha"]:  # another fix is on its way: it is the one verified now
            line = (f"{red_what(d)}: its fix {f['sha'][:10]} was already set aside; fix "
                    f"{fix.get('entry') or str(fix.get('sha'))[:10]} {fix['status']}")
            red_move(store, it, "fix-pending", line, data={"fix": {**fix, "verify_since": t}
                                                           if fix["status"] == "landed" else fix})
            return [line]
        line = f"{red_what(d)}: its fix {f['sha'][:10]} was already refuted: offered again"
        red_offer(store, it, line, data={"fix": None})
        return [f"re-offered {red_what(d)} (its fix {f['sha'][:10]} already refuted)"]
    if f.get("status") == "landed" and f.get("sha"):  # A LANDED FIX IS A COMMIT ON MAIN (above red_main_equivalent)
        on = red_fix_on_main(f, repo)
        if on is None and not (fix and fix.get("sha") != f["sha"]):
            h = {"at": iso_at(t), "holder": f.get("entry") or f["sha"][:10],
                 "outcome": f"its fix {f['sha'][:10]} is not on main (nor its content): offered again"}
            red_offer(store, it, f"{red_what(d)}: {h['outcome']}",
                      data={"fix": None, "history": (d.get("history") or []) + [h]})
            return [f"re-offered {red_what(d)} (its fix {f['sha'][:10]} is not on main)"]
        early = on is not None and f.get("at") is not None and \
            float(f["at"]) < float(d.get("opened_at") or 0) - 3600 and not (fix and fix.get("sha") != f["sha"])
        if early:  # landed before this spell opened: the run that opened it already contained it (red_fix_of)
            h = {"at": iso_at(t), "holder": f.get("entry") or f["sha"][:10],
                 "outcome": f"its fix {on['sha'][:10]} landed before this red opened (its failing run contained it): "
                            f"offered again"}
            red_offer(store, it, f"{red_what(d)}: {h['outcome']}",
                      data={"fix": None, "history": (d.get("history") or []) + [h]})
            return [f"re-offered {red_what(d)} (its fix {on['sha'][:10]} predates it)"]
        if on is not None and on["sha"] != f["sha"]:
            line = f"{red_what(d)}: its fix {f['sha'][:10]} landed as {on['sha'][:10]}: verifying that"
            it = red_move(store, it, "fix-pending", line, data={"fix": on})
            d, f = it["data"], it["data"]["fix"]
    if f.get("sha") and not fix and not f.get("waited_on") and not fix_reaches_red(repo, f["sha"], d):
        # PENDING ON ANOTHER RED'S FIX (PER RED, PER FAILING STEP: Order independence #74's swift red on 09e482c6a, the
        # ios red's fix): no run of it can prove this red fixed -- offered again, never parked until the give-up. Not
        # a red whose fixer said its fix IS that red's commit (WAITS ON, red_waits_on_step: `waited_on`): the commit on
        # main is the verification it waited for, whatever the reading rules make of its failing job (review 2026-11-16)
        line = f"{red_what(d)}: its fix {f['sha'][:10]} touches nothing its failing job reads: offered again"
        red_offer(store, it, line, data={"fix": None})
        return [f"re-offered {red_what(d)} (its fix {f['sha'][:10]} is another red's)"]
    if fix and not fix.get("entry") and f.get("entry"):  # its own change landed that very commit: that change's fix
        cur = f if f.get("status") == "landed" else red_fix_fresh(store, f)
        if cur and cur.get("status") == "landed" and cur.get("sha") == fix.get("sha"):
            fix = cur
    if fix and (fix.get("entry"), fix.get("sha"), fix["status"]) != (f.get("entry"), f.get("sha"), f.get("status")):
        if fix["status"] == "landed" and f.get("status") != "landed":
            # VERIFICATION STARTS WHEN THE FIX LANDS: the state's deadline (and red_fix_since) run from here, never
            # from the hours it waited in the queue behind a red gate
            it = red_move(store, it, "fix-pending", f"{red_what(d)}: fix {fix.get('entry') or fix.get('sha')} landed "
                                                    f"({str(fix.get('sha'))[:10]}): verifying",
                          data={"fix": {**f, **fix, "verify_since": t} if f.get("status") in FIX_OPEN else {**f, **fix}})
        else:  # another change is the fix now: its own record, never the last one's fields (a stale `linked`)
            store.update_item("red", it["id"], it["version"],
                              {"fix": {**f, **fix} if fix.get("entry") == f.get("entry") else fix})
            it = store.item("red", it["id"])
        d, f = it["data"], it["data"]["fix"]
    if f.get("status") == "owner":  # an owner-only repo's fix, filed with the owner (red owner-push): a green run proves it
        if obs is not None and gone:
            line = f"FIX OK: {red_what(d)} cleared after its fix went to the owner ({f.get('entry')})"
            inbox_write("red", line, [line], [], 0)
            store.close_offer(f"red:{it['id']}", "verified")
            red_move(store, it, "verified", line)
            return [line]
        if t - red_fix_since(it) >= env_num("AGENTCTL_RED_FIX_GIVEUP", RED_FIX_GIVEUP):
            return red_fix_unverifiable(store, it, f"its owner push ({f.get('entry')}: `{f.get('cmd')}`) is unproven "
                                                   f"{fmt_age(t - red_fix_since(it))} after it was filed", t)
        if t >= it["deadline"]:  # the owner has not pushed yet, or no gate run has decided since: said again
            line = f"RED OWNER PUSH WAITING: {red_what(d)}: `! {f.get('cmd')}` ({fmt_age(t - red_fix_since(it))} since filed)"
            inbox_write("red", line, [line], [], 1)
            red_move(store, it, "fix-pending", line, severity="escalate")
            return [line]
        return []
    if f.get("status") == "verify":  # an owner block's check passed: the next decided run says whether it is fixed
        last = ((snap or {}).get("latest") or {}).get(d.get("workflow")) or {}
        after = bool(last.get("updatedAt")) and parse_time(last["updatedAt"]).timestamp() > float(f.get("at") or 0)
        if obs is not None and gone:
            line = f"FIX OK: {red_what(d)} cleared after its owner block's check passed"
            inbox_write("red", line, [line], [], 0)
            red_move(store, it, "verified", line)
            return [line]
        if obs is not None and after and d["key"] in obs:
            if f.get("claimed"):  # its fixer said "already fixed" and the run refutes it: that fixer's attempt failed
                red_offer(store, it, f"{red_what(d)}: still red after its fixer found it already fixed: offered again",
                          data={"fix": None, "attempts": int(d.get("attempts") or 0) + 1})
                return [f"re-offered {red_what(d)} (not fixed after all)"]
            if f.get("awaiting"):  # the run it awaited still fails this way: nobody's fix was refuted
                red_offer(store, it, f"{red_what(d)}: still red on #{f['awaiting']}, the run it awaited: offered again",
                          data={"fix": None})
                return [f"re-offered {red_what(d)} (still red on #{f['awaiting']})"]
            red_offer(store, it, f"{red_what(d)}: still red after its block cleared: offered again", data={"fix": None})
            return [f"re-offered {red_what(d)} (still red after its block)"]
        if not d.get("gate") and not f.get("verify_dispatched"):
            line = red_verify_dispatch(o_r, repo, d.get("workflow"), "", (d.get("last_run") or {}).get("databaseId"))
            store.update_item("red", it["id"], it["version"], {"fix": {**f, "verify_dispatched": ts(), "verify": line}})
            if line.startswith("dispatched"):
                return [line]
            it = store.item("red", it["id"])
            f = it["data"]["fix"]
        # NO VERIFY CAN COME: its verifying dispatch failed, or no decided run came by the deadline -- a fix-pending with
        # no fix on its way never rests (Archive recording #823, 2026-10-29: the dispatch was refused with HTTP 400, its
        # workflow runs only on demand, and the red sat "fix None verify" for 19 h, escalating every 3 h, no fixer)
        failed = str(f.get("verify") or "").startswith("FIX UNVERIFIED:")  # refused; "until its next scheduled run" waits
        if failed or t - it["entered_at"] >= env_num("AGENTCTL_RED_FIX_GIVEUP", RED_FIX_GIVEUP):
            return red_fix_unverifiable(store, it, f"its verifying run could not start ({str(f.get('verify'))[:120]})"
                                        if failed else f"no decided {d.get('workflow')} run "
                                                       f"{fmt_age(t - it['entered_at'])} after it entered verify", t)
        if t >= it["deadline"]:
            red_move(store, it, "fix-pending", f"RED {red_what(d)}: unverified {fmt_age(t - it['entered_at'])} after its block",
                     severity="escalate")
            return [f"escalated {red_what(d)}: unverified"]
        return []
    if f.get("status") != "landed":
        status = red_entry_status(facts, f.get("entry"))
        cur = red_fix_fresh(store, f)
        if cur is not f and cur:  # the store's word on its change outranks the entry the facts read
            if cur.get("status") == "landed":
                line = f"{red_what(d)}: fix {f.get('entry')} landed ({str(cur['sha'])[:10]}): verifying"
                red_move(store, it, "fix-pending", line, data={"fix": {**cur, "verify_since": t}})
                return [line]
            status = cur.get("status")
        moved, ended = red_fix_follow(store, f, repo, t)
        if moved and moved["status"] == "landed":
            line = (f"{red_what(d)}: its fix {f.get('entry')} ended {ended}; its work landed in {moved['entry']} "
                    f"({str(moved['sha'])[:10]}): verifying")
            red_move(store, it, "fix-pending", line, data={"fix": {**moved, "verify_since": t}})
            return [line]
        if moved:
            if moved["entry"] != f.get("entry"):
                store.update_item("red", it["id"], it["version"], {"fix": moved})
            return []
        if ended:
            ch = red_change(store, f.get("entry")) or {}
            if t - float(ch.get("entered_at") or 0) < env_num("AGENTCTL_RED_FIX_REQUEUE_GRACE", RED_FIX_REQUEUE_GRACE):
                return []  # a re-queue of its worktree may follow the cancel
            status = ended
        if status in FIX_OPEN and f.get("linked") and not (fix and fix.get("entry") == f.get("entry")):
            # A LINK THAT NO LONGER HOLDS IS NO FIX (2026-11-20, Order independence #89: parked on metrics-packages,
            # linked through a script by a rule since dropped, while its owner held the lease): back to its holder
            why = f"{f.get('entry')} no longer links it (no Fixes-Red, nor its workflow file)"
            row = store.get(red_lease(d["key"]))
            if lease_is_live(row, t):
                red_move(store, it, "leased", f"{red_what(d)}: {why}: its holder {row['holder']} keeps it",
                         data={"fix": None, "holder": row["holder"]})
                return [f"leased {red_what(d)} ({why})"]
            red_offer(store, it, f"{red_what(d)}: {why}: offered again", data={"fix": None})
            return [f"re-offered {red_what(d)} ({why})"]
        if status in FIX_OPEN:
            return []
        if not fix:  # its change was rejected, cancelled or superseded, and no other fix is on its way
            # WHY IT ENDED, where the owner and the next fixer read (2026-11-22, #349: "ended rejected" in red status
            # and the inbox, the reason only in the change item's own `why`)
            why = " ".join(str((red_change(store, f.get("entry")) or {}).get("data", {}).get("why") or "").split())[:300]
            h = {"at": iso_at(t), "holder": f.get("entry"),
                 "outcome": f"its fix change {f.get('entry')} ended {status}" + (f": {why}" if why else "")}
            if red_decision(d):  # the owner's decided action: its change did not land -- the owner is told
                return red_decision_failed(store, it, {"attempts": int(d.get("attempts") or 0) + 1, "fix": None,
                                                       "history": (d.get("history") or []) + [h]},
                                           f"NOT LANDED: {h['outcome']}", t)
            red_offer(store, it, f"{red_what(d)}: {h['outcome']}: offered again",
                      data={"attempts": int(d.get("attempts") or 0) + 1, "history": (d.get("history") or []) + [h],
                            "fix": None})
            return [f"re-offered {red_what(d)} ({h['outcome']})"]
        return []
    sha = f.get("sha") or ""
    last = ((snap or {}).get("latest") or {}).get(d.get("workflow")) or {}
    inside = contains_local(repo, sha, last.get("headSha")) if sha and last else False
    holds = bool(inside) or (inside is None and bool(last.get("updatedAt"))
                             and parse_time(last["updatedAt"]).timestamp() > float(f.get("at") or 0) + WATCH_SLACK)
    if d.get("workflow") == LOCAL_FLOW:
        holds = gone
    if obs is not None and gone and (holds or d.get("red_kind") in ("local", "flake", "stuck-run", "predicts")):
        if d.get("red_kind") == "flake" and not holds:
            return []
        line = f"FIX OK: {red_what(d)} cleared by {sha[:10]}" + (f" (#{last.get('number')})" if last else "")
        inbox_write("red", line, [line], [], 0)
        store.close_offer(f"red:{it['id']}", "verified")
        red_stop_holder(store, d["key"], f"verified by {sha[:10]}", "red verified")
        red_move(store, it, "verified", line)
        return [line]
    if obs is not None and holds and d["key"] in obs and str(obs[d["key"]]["run"].get("databaseId")) == str(last.get("databaseId")):
        # THE FIX DID NOT FIX IT: refuted, its holder's lease dropped, offered again with the failed fix in the picture --
        # an UNPROVEN fix's holder keeps the red (A FIX WITH NO REPLAY KEEPS ITS FIXER): back to it, leased
        row = store.get(red_lease(d["key"]))
        if f.get("unproven") and lease_is_live(row, t):
            line = f"FIX FAILED: {red_what(d)}: #{last.get('number')} still fails this way (fix {sha[:10]})"
            inbox_write("red", line, [line], [d.get("line") or ""], 1)
            red_keep_holder(row, f"{line}: the red is still yours -- fix it again, with a `Fix-Replay:` this time")
            h = {"at": iso_at(t), "holder": f.get("entry") or sha[:10],
                 "outcome": f"fix {sha[:10]} refuted by #{last.get('number')} (unproven; its holder keeps it)"}
            red_move(store, it, "leased", f"{line}: back to {row['holder']}",
                     data={"attempts": int(d.get("attempts") or 0) + 1, "fix": None, "holder": row["holder"],
                           "refuted": red_refuted({"refuted": (d.get("refuted") or []) + [sha]}),
                           "history": (d.get("history") or []) + [h]})
            return [line]
        red_stop_holder(store, d["key"], f"your fix {sha[:10]} is refuted by #{last.get('number')}", "fix refuted",
                        force=True)
        refuted_blame = blame_refuted(repo, d.get("blame"), sha)  # the red's own revert: the culprit was wrong
        line = f"FIX FAILED: {red_what(d)}: #{last.get('number')} still fails this way (fix {sha[:10]})"
        inbox_write("red", line, [line], [d.get("line") or ""], 1)
        refuted = red_refuted({"refuted": (d.get("refuted") or []) + [sha]})
        h = {"at": iso_at(t), "holder": f.get("entry") or sha[:10], "outcome": f"fix {sha[:10]} refuted by #{last.get('number')}",
             "session": f.get("session"), "worktree": f.get("worktree")}
        red_offer(store, it, line, data={"attempts": int(d.get("attempts") or 0) + 1, "refuted": refuted, "fix": None,
                                         "history": (d.get("history") or []) + [h],
                                         "recurring": len(refuted) >= RED_RECURRING,
                                         **({"blame": refuted_blame} if refuted_blame else {})})
        if refuted_blame:
            line2 = (f"RED REVERT REFUTED: {red_what(d)}: reverting {sha[:10]} did not clear it, so its change was not "
                     "the culprit: it is relanded (its revert reverted) and the red goes to the normal fixer")
            inbox_write("red", line2, [line2], [], 1)
            line = f"{line}\n{line2}"
        return [line]
    if obs is not None and gone and not holds:
        red_close(store, it, "cleared", f"a run before its fix {sha[:10]} no longer fails this way")
        return [f"cleared {red_what(d)}"]
    if not holds and red_waits_for_its_run(d) and "next_run_at" not in f and d.get("workflow") != LOCAL_FLOW:
        plan = red_verify_plan(o_r, repo, d.get("workflow"), float(f.get("verify_since") or f.get("at") or t))
        if plan:  # A SCHEDULED WORKFLOW'S FIX WAITS FOR ITS SLOT (red_verify_plan)
            said = red_verify_await(store, it, f, plan, t)
        else:
            store.update_item("red", it["id"], it["version"], {"fix": {**f, "next_run_at": None}})  # no schedule
        it = store.item("red", it["id"])
        d, f = it["data"], it["data"]["fix"]
        if plan and not verify_dispatchable(repo, d.get("workflow")):
            return said  # a listed one goes on to its verify dispatch in this same tick
    if not holds and not d.get("gate") and not f.get("verify_dispatched") \
            and (t - float(f.get("at") or t) > RED_FIX_RUN_START or f.get("next_run_at")) \
            and d.get("red_kind") in ("outside",) \
            and not (f.get("next_run_at") and not verify_dispatchable(repo, d.get("workflow"))):  # its slot decides it
        line = red_verify_dispatch(o_r, repo, d.get("workflow"), sha, (d.get("last_run") or {}).get("databaseId"))
        if line.startswith("FIX UNVERIFIED: no workflow file"):  # NO RUN CAN EVER PROVE IT: offered now, never parked
            return red_fix_unverifiable(store, it, f"its fix {sha[:10]}: {line[len('FIX UNVERIFIED: '):][:160]}", t)
        if line.startswith("FIX UNVERIFIED:"):  # the failing run's inputs cannot be dispatched again: said so, and the
            line += (f"; the newest decided {d.get('workflow')} run on main containing {sha[:10]} decides it "  # next
                     f"(offered again if none by {red_fix_giveup(d) // 3600} h after it landed)")  # natural run decides
        store.update_item("red", it["id"], it["version"], {"fix": {**f, "verify_dispatched": ts(), "verify": line}})
        return [line]
    if t - red_fix_since(it) >= red_fix_giveup(d):
        return red_fix_unverifiable(store, it, f"its fix {sha[:10] or f.get('entry')} unverified for "
                                               f"{fmt_age(t - red_fix_since(it))} after it landed (no run of "
                                               f"{d.get('workflow')} proves it)", t)
    if t >= it["deadline"] and f.get("verify_by") and not holds:
        return red_verify_scheduled(store, it, f, sha, o_r, repo, t)
    if t >= it["deadline"] and not holds and str(f.get("verify") or "").startswith("dispatched") \
            and not red_verify_run_on_way(o_r, repo, f):  # A MISSED DEADLINE IS NEVER SILENT: its dispatched run never
        return red_fix_unverifiable(store, it, f"fix {sha[:10]} unverified {fmt_age(t - red_fix_since(it))} after "  # came
                                               f"its verifying dispatch ({str(f.get('verify'))[:100]})", t)
    if t >= it["deadline"]:
        # awaiting its workflow's next run: no one's to act on
        awaiting = red_fix_giveup(d) > env_num("AGENTCTL_RED_FIX_GIVEUP", RED_FIX_GIVEUP)
        red_move(store, it, "fix-pending", f"RED {red_what(d)}: fix {sha[:10] or f.get('entry')} unverified for "
                                           f"{fmt_age(t - it['entered_at'])}"
                 + (f" (awaiting {d.get('workflow')}'s next run)" if awaiting else ""),
                 severity="info" if awaiting else "escalate")
        return [f"{'re-deadlined' if awaiting else 'escalated'} {red_what(d)}: fix unverified"]
    return []
