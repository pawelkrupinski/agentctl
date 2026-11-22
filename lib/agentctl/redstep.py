# agentctl, part `redstep`: the red step: one item, one small idempotent step per tick; holds, lapsed leases, waits on another red.
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import os
import re
from typing import Any
from agentctl.core import cherry_landed, env_num, fmt_age, remote
from agentctl.timefmt import iso_at, ts
from agentctl.forge import forge_actions_run
from agentctl.repos import RED_PRIMARY, red_repo_of
from agentctl.workflows import LOCAL_FLOW
from agentctl.leases import block_lease, lease_meta, red_block_of
from agentctl.inboxentry import inbox_write
from agentctl.redretired import red_retired
from agentctl.redrecord import RED_TERMINAL, lease_is_live, red_closed_of, red_lease, red_what
from agentctl.gating import main_workflow_files
from agentctl.allowedpaths import allowed_path
from agentctl.redfixes import RED_LANDED_VERDICT, red_fix_of, red_run_on_way
from agentctl.redask import red_decision
from agentctl.redmove import red_close, red_fix_proven, red_keep_holder, red_move, red_put_offer, red_stop_holder
from agentctl.redseen import red_gone, red_gone_why
from agentctl.redopen import red_callable_evidence, red_diagnosis_step, red_history, red_offer
from agentctl.redverify import red_flake_check, red_main_verify
from agentctl.redcrash import (
    AGENTCTL_DEFECT_CAUSE, RED_CRASH_LOOP, RED_DECIDE, probe_red_newer_verdict, red_ask_route, red_blamed_issue,
    red_crash_loop, red_crash_streak, red_decision_block, red_decision_needed, red_harness_block, red_old_run,
    red_resolve_old)
from agentctl.fixerlife import GAVE_UP_DECIDE, GAVE_UP_FIXED
from agentctl.redvault import red_vault_block, vault_ask_ns, vault_ask_unfounded
from agentctl.ask import DECISION_FAILED, red_decision_failed
from agentctl.redpending import RED_WAITS_ON, red_fix_current, red_fix_pending, red_wait_cycle
from agentctl.blame import blame_step
from agentctl.reddispatch import repo_dispatch_off


# A RED IS NAMED BY ITS WORKFLOW (2026-11-12, b059f05c70fa): opened while the forge read a run's run-name for its
# workflow's name, the red "Identity takes (convergence run 43013487979)" #40 named a workflow no other run is in: no
# newer run of it could verify, refute or clear it, its verify dispatch found "no workflow file named" it, and it sat in
# fix-pending 6 h at a time. Such a red takes its workflow's name from its failing run's workflow file, once.
def red_workflow_named(store, it, o_r):
    """The red under its workflow's name (renamed in place when its run says another); it unchanged otherwise."""
    d = it["data"]
    rid = (d.get("last_run") or {}).get("databaseId")
    if not rid or d.get("workflow_named") or d.get("workflow") == LOCAL_FLOW or d.get("red_kind") in ("local", "predicts"):
        return it
    files = main_workflow_files(o_r)
    if not files or d.get("workflow") in files:
        return it
    try:
        run = forge_actions_run(o_r, rid)
    except (Exception, SystemExit):
        run = None
    if not run:
        return it  # unread now: tried again next tick
    name = {f: n for n, f in files.items()}.get(run.get("workflow_id"))
    upd = {"workflow_named": True}
    if name and name != d.get("workflow"):
        upd["workflow"] = name
        upd["history"] = (d.get("history") or []) + [{"at": iso_at(store.clock()), "holder": None,
                                                     "outcome": f"RENAMED: {d.get('workflow')!r} is a run-name; its "
                                                                f"workflow is {name!r} ({run.get('workflow_id')})"}]
    store.update_item("red", it["id"], it["version"], upd)
    return store.item("red", it["id"])


def red_step(store, it, obs, snap, facts, repo, o_r, t):
    it = store.item("red", it["id"])
    if it["state"] not in RED_TERMINAL:
        it = red_workflow_named(store, it, o_r)
    d, st, key = it["data"], it["state"], it["data"]["key"]
    row = store.get(red_lease(key))
    live = lease_is_live(row, t)
    gone = obs is not None and red_gone(it, obs, snap)
    it, fix = red_linked_kept(store, it, row, red_fix_current(store, it, facts), live)
    block = red_block_of(it, store)
    acts = []
    if st not in RED_TERMINAL:
        why = red_retired(o_r, d, t)
        if why:  # its check is gone from main: no run can ever clear it, and no fixer is offered it
            red_close(store, it, "cleared", why)
            return [f"cleared {red_what(d)}: {why}"]
    # fix-pending too: the usual end of such a red is its fixer's own `Fixes-Red:` landing, and no dispatch can verify a
    # workflow_call-only workflow -- it waited RED_FIX_GIVEUP (6 h) there unread, then went back to a fixer
    proof = red_callable_evidence(d, snap, repo, o_r) if st in ("red", "offered", "leased", "blocked-external",
                                                                "fix-pending") else None
    if proof:  # recorded as a verified fix is: its own event, the holder told, the run settled
        line = f"FIX OK: {red_what(d)}: {proof}"
        inbox_write("red", line, [line], [], 0)
        store.close_offer(f"red:{it['id']}", "verified")
        red_stop_holder(store, key, "its callers' runs prove it fixed", "red verified")
        red_move(store, it, "verified", line, data={"settled_run": (d.get("last_run") or {}).get("databaseId"),
                                                    "proof": proof})
        return [line]
    off = repo_dispatch_off(store, red_repo_of(d))
    if off and st in ("red", "offered", "leased", "blocked-external", "dispatch-off"):
        return red_hold_off(store, it, row, block, gone, obs, t)
    verified = red_main_verify(store, it, repo, o_r, t)
    if verified:  # its version moved: read again
        it = store.item("red", it["id"])
        d = it["data"]
        acts += verified
    if st == "dispatch-off":  # its repo's dispatch is on again: offered
        red_offer(store, it, f"{red_what(d)}: dispatch on again for {red_repo_of(d)}: offered")
        return [f"offered {red_what(d)} (dispatch on again)"]
    if st in ("red", "offered") and not live and not fix and not block:
        old = red_old_run(d, t)
        if old:  # AN OLD RED SEEN ONLY NOW (audit #7 surfaces a rarely run workflow): verified by a fresh run, or fixed
            done = red_resolve_old(store, it, old, o_r, repo, t)
            if done is not None:
                return done
            it = store.item("red", it["id"])  # no fresh run can check it: an ordinary red, offered below
            d = it["data"]
    if st == "red":
        it = red_offer(store, it, f"{red_what(d)} is red: offered")
        acts = [f"offered {red_what(d)} ({it['data']['tier']})"]
        if it["data"]["tier"] == "blame":  # a culprit decided by what is known already (Main's green parent): now
            acts += blame_step(store, it, repo, o_r, t) or []
        return acts
    if st == "offered":
        if gone and not fix:
            red_close(store, it, "cleared", red_gone_why(d, obs))
            return [f"cleared {red_what(d)}"] + red_flake_check(store, it, snap, repo, facts)
        if fix:
            store.close_offer(f"red:{it['id']}", "fix-pending")
            red_move(store, it, "fix-pending", f"{red_what(d)}: fix {fix.get('entry') or fix.get('sha')} {fix['status']}",
                     data={"fix": fix})
            return [f"fix-pending {red_what(d)}"]
        way = None if live or block else red_run_on_way(d, snap, repo)
        if way:  # A RUN ON THE WAY MAY HAVE FIXED IT (issue 20261107T224109-5c81e4): wait for it, never a fixer meanwhile
            run, sha = way
            store.close_offer(f"red:{it['id']}", "fix-pending")
            line = (f"{red_what(d)}: {sha[:10]} on main reaches its failing job (no Fixes-Red): awaiting "
                    f"#{run.get('number')}")
            red_move(store, it, "fix-pending", line, data={"fix": {
                "status": "verify", "at": t, "entry": None, "sha": "", "awaiting": run.get("number"),
                "verify_dispatched": ts(), "verify": f"awaiting #{run.get('number')}, which contains {sha[:10]}"}})
            return [line]
        if live:
            store.close_offer(f"red:{it['id']}", "taken")
            red_move(store, it, "leased", f"{red_what(d)}: leased by {row['holder']}", data={"holder": row["holder"]})
            return [f"leased {red_what(d)} to {row['holder']}"]
        if row and float(lease_meta(row).get("released_at") or 0) > it["entered_at"] and \
                float(row.get("acquired_at") or 0) >= it["entered_at"] and not red_lease_read(d, row):
            # taken AND ended between two ticks: its attempt -- once (red_lease_read: a re-offer keeps entered_at)
            return red_lease_ended(store, it, row, block, t, repo)
        if block:
            store.close_offer(f"red:{it['id']}", "blocked")
            red_move(store, it, "blocked-external", f"{red_what(d)}: {block['text']}",
                     data={"cause": block.get("cause"), "block": block["name"]})
            return [f"blocked {red_what(d)}"]
        if (d.get("blame") or {}).get("state") == "probing":  # its culprit is being decided: no escalation meanwhile
            if not store.offer_of(f"red:{it['id']}"):
                red_put_offer(store, it)
            return blame_step(store, it, repo, o_r, t) or []
        off = store.offer_of(f"red:{it['id']}") or {}
        if str(off.get("reason") or "").startswith("STARVED") and d.get("starved") != off.get("id"):
            # waited past RED_STARVE with no free fixer slot: escalated NOW (then every cycle, below)
            text = f"RED STARVED: {red_what(d)}: no fixer slot for {fmt_age(t - float(off.get('opened_at') or t))} ({off['reason']})"
            red_offer(store, it, text, data={"starved": off.get("id")}, escalate=True,
                      verdict=text + f" -- `~/bin/agentctl red claim {key} --worktree <yours>` to take it")
            return [text]
        if t >= it["deadline"]:  # no taker in time: escalate (the state's self-transition), widen, offer again
            if lease_is_live(store.get(red_lease(key)), t):  # taken since this step read its lease: never UNCLAIMED
                return acts  # with a holder; the next step moves it to leased
            why = off.get("reason") or "no taker"
            mins = int((t - it["entered_at"]) // 60)
            text = f"RED UNCLAIMED: {red_what(d)}: no taker for {mins} min ({why})"
            red_offer(store, it, text, data={"author_tried": True} if d.get("tier") == "author" else None,
                      verdict=text + f" -- `~/bin/agentctl red claim {key} --worktree <yours>` to take it")
            return [text]
        if not store.offer_of(f"red:{it['id']}"):  # repair: an offered red always has its open offer
            red_put_offer(store, it)
        return acts
    if st == "leased":
        if fix and live and not red_fix_proven(store, d, fix):  # A FIX WITH NO REPLAY KEEPS ITS FIXER
            red_keep_holder(row, f"your fix {fix.get('entry') or str(fix.get('sha'))[:10]} carries no Fix-Replay "
                                 f"agentctl ran (base=FAIL head=PASS): the red stays yours, fix-unproven, until its "
                                 f"verification run decides it -- FIX OK or FIX FAILED reaches your inbox")
            red_move(store, it, "fix-pending", f"{red_what(d)}: fix {fix.get('entry') or fix.get('sha')} {fix['status']}"
                     f", unproven: {row['holder']} keeps it", data={"fix": {**fix, "unproven": True}})
            return [f"fix-unproven {red_what(d)} ({row['holder']} keeps it)"]
        if fix:
            red_stop_holder(store, key, f"its fix {fix.get('entry') or fix.get('sha')} is on its way", "fix submitted")
            red_move(store, it, "fix-pending", f"{red_what(d)}: fix {fix.get('entry') or fix.get('sha')} {fix['status']}",
                     data={"fix": fix})
            return [f"fix-pending {red_what(d)}"]
        if gone:
            red_close(store, it, "cleared", red_gone_why(d, obs))
            return [f"cleared {red_what(d)}"]
        if live:
            if row["holder"] != d.get("holder"):
                store.update_item("red", it["id"], it["version"], {"holder": row["holder"]})
            return acts + red_diagnosis_step(store, store.item("red", it["id"]), row, snap, repo, o_r, t)
        return red_lease_ended(store, it, row, block, t, repo)
    if st == "fix-pending":
        return red_fix_pending(store, it, fix, gone, obs, snap, facts, repo, o_r, t)
    if st == "blocked-external":
        if live:
            red_move(store, it, "leased", f"{red_what(d)}: a live fixer outranks the block", data={"holder": row["holder"]})
            return [f"leased {red_what(d)} (over its block)"]
        if gone:
            red_close(store, it, "cleared", red_gone_why(d, obs))
            return [f"cleared {red_what(d)}"]
        if fix:  # A LINKED FIX ANSWERS THE BLOCK (58f3db9e8a3d, 2026-11-12: on the owner only because the fix was not
            # linked): the red waits for the run that proves it; the owner's question and the block go
            lifted = red_lift_block(store, it, block, f"its fix {fix.get('entry') or str(fix.get('sha'))[:10]} is on its way")
            red_move(store, it, "fix-pending", f"{red_what(d)}: fix {fix.get('entry') or fix.get('sha')}"
                     + (f" ({lifted})" if lifted else ""), data={"fix": fix, "cause": None, "block": None, "ask": None})
            return [f"fix-pending {red_what(d)}"]
        if not block:  # its block lapsed: the probe passed (verify it), or its justification or max hold is gone
            brow = store.get(d.get("block") or block_lease(key))
            out = str(lease_meta(brow).get("outcome") or "expired")
            if out.startswith("PROBE PASSED"):
                red_move(store, it, "fix-pending", f"{red_what(d)}: its block's check passes ({out[14:120]}): verifying",
                         data={"fix": {"status": "verify", "at": t, "entry": None, "sha": ""}, "block": None})
                return [f"verifying {red_what(d)} (its block cleared)"]
            red_offer(store, it, f"{red_what(d)}: its block ended ({out[:120]}): offered again",
                      data={"cause": None, "block": None},
                      escalate=not (out.startswith(("INVALID", "RELEASED")) or red_decision(d)))
            return [f"re-offered {red_what(d)} (block ended: {out[:60]})"]
        return acts
    return acts


def red_linked_kept(store: Any, it: Any, row: Any, fix: Any, live: bool) -> tuple[Any, Any]:
    """(it, fix) for a leased red: A LINKED CHANGE NEVER STOPS THE RED'S FIXER (redlink.py) -- with a live holder, a
    change linked only by what it edits is no fix here (None): the holder is told once (RED-NOTE.md) and keeps it."""
    if not (fix and live and fix.get("linked") and it["state"] == "leased"):
        return it, fix
    if it["data"].get("linked_told") != fix["entry"]:
        red_keep_holder(row, f"{fix['entry']} (no Fixes-Red) changes {fix['linked']}: check it is no fix before yours; "
                             f"the red stays yours")
        store.update_item("red", it["id"], it["version"], {"linked_told": fix["entry"]})
        it = store.item("red", it["id"])
    return it, None


def red_hold_off(store, it, row, block, gone, obs, t):
    """Red `it` of a repo whose dispatch the owner turned off: recorded, never offered or asked -- its offer closed, a
    live lease released, its owner block (a vault ask included) lifted, the red held in `dispatch-off`; a newer run
    still clears it."""
    d = it["data"]
    repo, acts = red_repo_of(d), []
    if gone and obs is not None:
        red_close(store, it, "cleared", red_gone_why(d, obs))
        return [f"cleared {red_what(d)}"]
    store.close_offer(f"red:{it['id']}", "dispatch-off")
    if lease_is_live(row, t):
        store.revoke(row["name"], f"RELEASED: dispatch off for {repo} (owner)")
        acts.append(f"released {row['name']} ({row['holder']}): dispatch off for {repo}")
    if block:
        lifted = red_lift_block(store, it, block, f"dispatch off for {repo} (owner)")
        if lifted:
            acts.append(lifted)
    if it["state"] != "dispatch-off":
        red_move(store, store.item("red", it["id"]), "dispatch-off",
                 f"{red_what(d)}: dispatch off for {repo} (owner): recorded, not offered",
                 data={"holder": None, "block": None, "cause": None, "ask": None}, severity="info")
        acts.append(f"held {red_what(d)}: dispatch off for {repo}")
    return acts


def red_lift_block(store, it, block, why):
    """Release red it from its live owner block (the block itself when it holds only this red, else this red leaves its
    keys). Returns what it did, or None when no block holds it."""
    if not block:
        return None
    key = it["data"]["key"]
    keys = [k for k in (block.get("meta") or {}).get("keys") or [] if k != key]
    if keys:
        row = store.get(block["name"])
        store.set_meta(block["name"], row["epoch"], row["secret"], {"keys": keys})
        return f"left the block {block['name']}"
    store.revoke(block["name"], f"LIFTED: {why}")
    return f"its block {block['name']} lifted"


RED_QUICK_EXIT = 120       # a fixer ending this soon with no verdict never got going
RED_START_BACKOFF = 600    # ...and its red waits this long (doubling) before the next dispatch

LEASE_INFRA = re.compile(r"(lease expired|ORPHANED|STOPPED: the red's lease was lost)")


# ONE END, ONE ATTEMPT (2026-11-18 23:42-23:49Z, core Main Pipeline #1053, 0c8eea91b712): one fixer lease, revoked
# ORPHANED once, was read into the red's history five times a minute apart -- the `offered` step matched the same
# ended lease at every tick, since the re-offer (a self-transition) keeps the item's entered_at -- and "3 launches in
# a row" twice made a CRASH LOOP and then BLOCKED ON OWNER, with no fixer started after the first. The epoch of the
# lease whose end the red read is kept (`ended_lease`); that end is never read again.
def red_lease_read(d, row):
    """True when the red's history already holds the end of this lease (its epoch)."""
    return bool(row) and row.get("epoch") is not None and d.get("ended_lease") == row.get("epoch")


def red_vault_ask(store: Any, it: Any, upd: dict[str, Any], ns: str, out: str, t: float, o_r: str | None) -> Any:
    """A fixer's vault ask: the red waits on vault ns (red_vault_block) -- unless it names no secret read that failed
    in a repo that does not sign (redvault.py vault_ask_unfounded): then it goes back to its fixer, saying why."""
    unfounded = vault_ask_unfounded(o_r or RED_PRIMARY, out, ns)
    if unfounded:
        return red_ask_route(store, it, upd, unfounded, out, t)
    return red_vault_block(store, it, upd, ns, out, t)


def red_lease_ended(store, it, row, block, t, repo=None):
    """Its holder's lease ended (released with a verdict, expired without progress, revoked): one attempt (not for a
    release by hand, a fixer that never started, a lease lost to infra, a red found already fixed, or one that needs the
    owner's decision: AN ATTEMPT IS A FIXER THAT TRIED AND FAILED TO FIX), the verdict in its history, offered again up
    the ladder."""
    d = it["data"]
    h = red_history(row, t)
    m = lease_meta(row)
    quick = float(m.get("released_at") or t) - float((row or {}).get("acquired_at") or 0) < RED_QUICK_EXIT
    if quick and re.match(r"(exit -?\d+|ended) with no|NOT STARTED", str(h["outcome"])):  # it never got going (a crash at start): not an
        h["outcome"] = re.sub(r"^(NOT STARTED: )*", "NOT STARTED: ", str(h["outcome"]))  # attempt, nor an instant re-dispatch
    out = str(h["outcome"])
    infra = bool(h.get("dispatched") and LEASE_INFRA.match(out))  # a wrapper's lease renews by its beat: a lapse is
    # the wrapper or its machine gone (an interactive claim lapses by its own inactivity: that one counts)
    counted = not (out.startswith(("NOT STARTED", "RELEASED")) or infra or GAVE_UP_FIXED.match(out)
                   or GAVE_UP_DECIDE.match(out))
    upd = {"history": (d.get("history") or []) + [h], "holder": None,
           "attempts": int(d.get("attempts") or 0) + (1 if counted else 0),
           "ended_lease": (row or {}).get("epoch")}
    if str(h["outcome"]).startswith("NOT STARTED") and m.get("tier") == "lander" and d.get("blame"):
        # THE LANDER COULD NOT BE REVIVED (LAUNCH FAILED): a fresh fixer at once, briefed with the change -- no backoff
        upd["blame"] = dict(d["blame"], revive_failed=str(h["outcome"])[:300])
    elif str(h["outcome"]).startswith("NOT STARTED"):
        fails = int(d.get("start_failures") or 0) + 1
        upd.update(start_failures=fails, dispatch_after=t + min(RED_START_BACKOFF * 2 ** (fails - 1), 3600))
    elif counted:
        upd.update(start_failures=0, dispatch_after=0)
    if str(h["outcome"]).startswith("DIAGNOSED:"):
        upd["diagnosis"] = h["outcome"][len("DIAGNOSED:"):].strip()[:600]
    wt = h.get("worktree")
    if wt and os.path.isdir(wt) and allowed_path(wt):  # its unlanded commits are never lost: the next fixer starts there
        left = [c[:10] for c in cherry_landed(wt, f"refs/remotes/{remote()}/main", "HEAD")[1] or []]
        if left:
            upd["prior_work"] = {"worktree": wt, "commits": left[:20]}
    fix = red_fix_of({"data": {**d, **upd}}, {"repo": repo}) if RED_LANDED_VERDICT.match(out) else None
    if fix:  # ITS FIXER LANDED ITS FIX (784badc4406e.4, 2026-11-13: `LANDED 8d06b1a5ab` was offered again, then put on
        # the owner): the red waits for the run that proves it, its owner block (if any) answered by the fix
        lifted = red_lift_block(store, it, block, f"its fixer landed {fix['sha'][:10]}")
        store.close_offer(f"red:{it['id']}", "fix-pending")
        red_move(store, it, "fix-pending", f"{red_what(d)}: {h['holder']} landed its fix {fix['sha'][:10]}: verifying"
                 + (f" ({lifted})" if lifted else ""),
                 data={**upd, "fix": {**fix, "verify_since": t}, "cause": None, "block": None, "ask": None})
        return [f"fix-pending {red_what(d)} (its fixer landed {fix['sha'][:10]})"]
    waits = red_waits_on_of(out, d.get("key"), store)
    if waits:  # A HOLD ON ANOTHER RED'S FIX (red_waits_on_of): a state agentctl resolves itself, never a failed action
        lifted = red_lift_block(store, it, block, f"it waits on red {waits['red']}'s fix {waits['sha'][:10]}")
        store.close_offer(f"red:{it['id']}", "fix-pending")
        dec = d.get("decision")
        red_move(store, it, "fix-pending", f"{red_what(d)}: waits on red {waits['red']}'s fix {waits['sha'][:10]}"
                 + (f" ({lifted})" if lifted else ""),
                 data={**upd, "fix": {"entry": None, "status": RED_WAITS_ON, "at": t, **waits}, "cause": None,
                       "block": None, "ask": None, **({"decision": {**dec, "status": "waiting"}} if dec else {})})
        return [f"fix-pending {red_what(d)} (waits on red {waits['red']}'s {waits['sha'][:10]})"]
    if block:
        red_move(store, it, "blocked-external", f"{red_what(d)}: {block['text']}",
                 data={**upd, "cause": block.get("cause"), "block": block["name"]})
        return [f"blocked {red_what(d)}"]
    if GAVE_UP_FIXED.match(out):  # ALREADY FIXED: the red's probe decides, never the fixer's word alone
        return red_already_fixed(store, it, upd, out, t)
    decide = red_decision_needed(d, h) if str(h["outcome"]).startswith("DIAGNOSED:") else None
    if not decide and GAVE_UP_DECIDE.match(out):  # THE FIXER NEEDS THE OWNER'S DECISION: one WAITING ON YOU, with its
        m = RED_DECIDE.search(out)  # own words (and the category it names), never offered to the next fixer meanwhile
        decide = ((m.group(1) or "") + m.group(2).strip()) if m else re.sub(r"^\W*GAVE UP:\s*", "", out).strip()
        upd["gave_up_ask"] = True  # routed back (red_ask_route), it counts as the attempt it was
        if m:  # its words before the ask are the diagnosis the owner reads
            upd["diagnosis"] = re.sub(r"^\W*GAVE UP:\s*", "", out[:m.start(1) if m.group(1) else m.start(2)])
            upd["diagnosis"] = re.sub(r"[;\s]*decide\s*$", "", upd["diagnosis"]).strip()[:600]
    vault = vault_ask_ns(decide or (out if out.startswith("BLOCKED ON OWNER") else ""))
    if vault:  # ITS ONE ASK IS A VAULT UNLOCK: a wait agentctl checks itself (red_vault_ask), never a decision
        return red_vault_ask(store, it, upd, vault, out, t, d.get("repo"))
    if red_decision(d) and DECISION_FAILED.match(str(h["outcome"])):  # THE OWNER'S DECIDED ACTION FAILED: told
        return red_decision_failed(store, it, upd, h["outcome"], t)
    if decide:
        return red_decision_block(store, it, upd, decide, t)
    issue = red_blamed_issue(store, out)
    if issue:
        red_harness_block(store, it, upd, issue, AGENTCTL_DEFECT_CAUSE, f"~/bin/agentctl issue --list",
                          f"a newer {d.get('workflow')} run no longer fails this way",
                          f"its fixer blamed agentctl: open agentctl issue {issue} ({out[:200]}). Offered again when the "
                          "issue is closed or a newer run decides it", t)
        return [f"blocked {red_what(d)} on agentctl issue {issue}"]
    streak = red_crash_streak({**d, **upd})
    if len(streak) >= env_num("AGENTCTL_RED_CRASH_LOOP", RED_CRASH_LOOP):
        return red_crash_loop(store, it, upd, streak, t)
    red_offer(store, it, f"{red_what(d)}: {h['holder']} ended ({h['outcome'][:120]}): offered again", data=upd,
              quiet=not counted)
    return [f"re-offered {red_what(d)} after {h['outcome'][:60]}"]


RED_WAITS_ON_VERDICT = re.compile(r"^\W*WAITS ON ([0-9a-f]{7,40})\s*\(red ([0-9a-f]{12}(?:\.\d+)?|[\w.-]+)\)")
RED_WAITS_ON_WORDS = re.compile(r"^\W*(?:NOT LANDED|GAVE UP)\b.*?\b([0-9a-f]{7,40})\s*\([^()]*?\bred ([\w.-]+)\)",
                                re.S)


def red_waits_on_of(outcome, own_key, store):
    """{sha, red} when a fixer's verdict says this red waits on another red's commit, else None."""
    out = str(outcome or "")
    m = RED_WAITS_ON_VERDICT.match(out) or RED_WAITS_ON_WORDS.match(out)
    if not m or m.group(2) == own_key:
        return None
    key = re.sub(r"\.\d+$", "", m.group(2))
    if key == own_key or not (red_closed_of(store, key) or store.item("red", key)):
        return None  # names no red agentctl knows: no state to wait on
    if red_wait_cycle(store, own_key, key):
        return None  # that red waits on this one in turn: no wait, a fixer's work (red_wait_cycle)
    return {"sha": m.group(1), "red": key}


def red_already_fixed(store, it, upd, outcome, t):
    """Its fixer found the red already fixed on main: the red's probe (a newer decided run of its workflow no longer
    failing this way) closes it when it passes; else it waits in fix-pending (verify) for the run that decides it --
    cleared by it, or offered again, the claim then counted as an attempt (red_fix_pending)."""
    d = {**it["data"], **upd}
    args = {"repo": d.get("repo") or None, "workflow": d.get("workflow"), "item": it["id"],
            "run_id": (d.get("last_run") or {}).get("databaseId"), "run": (d.get("last_run") or {}).get("number")}
    if not args["repo"]:
        args.pop("repo")
    try:
        ok, why = probe_red_newer_verdict(args)
    except Exception as e:  # an unreadable snapshot decides nothing: the verify state waits for the run
        ok, why = None, f"probe failed ({type(e).__name__})"
    store.update_item("red", it["id"], it["version"], upd)
    it = store.item("red", it["id"])
    if ok:
        red_close(store, it, "cleared", f"its fixer found it already fixed, and {why}")
        return [f"cleared {red_what(d)} (already fixed: {why})"]
    store.close_offer(f"red:{it['id']}", "fix-pending")
    red_move(store, it, "fix-pending", f"{red_what(d)}: its fixer found it already fixed ({outcome[:160]}): verifying "
                                       f"({why})",
             data={"fix": {"status": "verify", "at": t, "entry": None, "sha": "", "claimed": outcome[:300]}})
    return [f"verifying {red_what(d)} (already fixed, says its fixer)"]
