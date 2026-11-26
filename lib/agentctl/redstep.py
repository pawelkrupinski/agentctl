# agentctl, part `redstep`: the red step: one item, one small idempotent step per tick; holds, lapsed leases, waits on another red.
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import re
from typing import Any
from agentctl.timefmt import ts
from agentctl.core import fmt_age
from agentctl.repos import red_repo_of
from agentctl.redretired import red_retired
from agentctl.inboxentry import inbox_write
from agentctl.redrecord import RED_TERMINAL, lease_is_live, red_lease, red_what
from agentctl.leases import block_lease, lease_meta, red_block_of
from agentctl.redfixes import red_fix_of, red_run_on_way
from agentctl.redask import red_decision
from agentctl.redmove import red_close, red_fix_proven, red_keep_holder, red_move, red_put_offer, red_stop_holder
from agentctl.redseen import red_gone, red_gone_why
from agentctl.redopen import red_callable_evidence, red_diagnosis_step, red_offer
from agentctl.redverify import red_flake_check, red_main_verify
from agentctl.redcrash import red_old_run, red_resolve_old
from agentctl.reddispatch import repo_dispatch_off
from agentctl.blame import blame_step
from agentctl.redlease import red_lease_ended, red_lift_block
from agentctl.redpending import red_fix_pending
from agentctl.timefmt import iso_at
from agentctl.core import attempt
from agentctl.forge import forge_actions_run
from agentctl.workflows import LOCAL_FLOW
from agentctl.gating import main_workflow_files


class RedStep:
    """One red item's step of one tick: what its state, lease, linked fix, block and the run's observation say it does
    next. `run` returns the actions taken (the early answers first, then the state's own handler)."""

    def __init__(self, store: Any, it: Any, obs: Any, snap: Any, facts: Any, repo: Any, o_r: Any, t: Any) -> None:
        self.store, self.obs, self.snap, self.facts, self.repo, self.o_r, self.t = store, obs, snap, facts, repo, o_r, t
        it = store.item("red", it["id"])
        if it["state"] not in RED_TERMINAL:
            it = red_workflow_named(store, it, o_r)
        self.d, self.st, self.key = it["data"], it["state"], it["data"]["key"]
        self.row = store.get(red_lease(self.key))
        self.live = lease_is_live(self.row, t)
        self.gone = obs is not None and red_gone(it, obs, snap)
        # THE fix on its way for this red: red_fix_of's, corrected by the store (red_fix_fresh). Each candidate is judged in
        # turn: one the red refuted or set aside never hides the next (2026-11-11, Verify scrape recording #23: a stale
        # `waiting` entry came back as the refuted 547709c1e2 every tick, offered 593 times, no fixer ever offered the red)
        self.it, self.fix = red_linked_kept(store, it, self.row, red_fix_of(it, facts, store), self.live)
        self.block = red_block_of(self.it, store)
        self.acts = []

    def reread(self) -> Any:
        self.it = self.store.item("red", self.it["id"])
        self.d = self.it["data"]

    def cleared(self) -> Any:
        red_close(self.store, self.it, "cleared", red_gone_why(self.d, self.obs))
        return [f"cleared {red_what(self.d)}"]

    def fix_name(self) -> Any:
        return self.fix.get("entry") or self.fix.get("sha")

    def fix_pending(self) -> Any:
        """Its linked fix is on its way: the red waits for the run that proves it."""
        red_move(self.store, self.it, "fix-pending",
                 f"{red_what(self.d)}: fix {self.fix_name()} {self.fix['status']}", data={"fix": self.fix})
        return [f"fix-pending {red_what(self.d)}"]

    def run(self) -> Any:
        for early in (self.retired, self.proven, self.held_off):
            done = early()
            if done is not None:
                return done
        self.acts += red_main_verify(self.store, self.it, self.repo, self.o_r, self.t) or []
        if self.acts:  # its version moved: read again
            self.reread()
        for early in (self.dispatch_on_again, self.old_run):
            done = early()
            if done is not None:
                return done
        handler = {"red": self.red, "offered": self.offered, "leased": self.leased,
                   "fix-pending": self.pending, "blocked-external": self.blocked_external}.get(self.st)
        return handler() if handler else self.acts

    def retired(self) -> Any:
        if self.st in RED_TERMINAL:
            return None
        why = red_retired(self.o_r, self.d, self.t)
        if not why:
            return None
        red_close(self.store, self.it, "cleared", why)  # its check is gone from main: no run can ever clear it
        return [f"cleared {red_what(self.d)}: {why}"]

    def proven(self) -> Any:
        """Its callers' runs prove it fixed -- fix-pending too: the usual end of such a red is its fixer's own
        `Fixes-Red:` landing, and no dispatch can verify a workflow_call-only workflow -- it waited RED_FIX_GIVEUP (6 h)
        there unread, then went back to a fixer. Recorded as a verified fix is: its own event, the holder told."""
        d = self.d
        if self.st not in ("red", "offered", "leased", "blocked-external", "fix-pending"):
            return None
        proof = red_callable_evidence(d, self.snap, self.repo, self.o_r)
        if not proof:
            return None
        line = f"FIX OK: {red_what(d)}: {proof}"
        inbox_write("red", line, [line], [], 0)
        self.store.close_offer(f"red:{self.it['id']}", "verified")
        red_stop_holder(self.store, self.key, "its callers' runs prove it fixed", "red verified")
        red_move(self.store, self.it, "verified", line,
                 data={"settled_run": (d.get("last_run") or {}).get("databaseId"), "proof": proof})
        return [line]

    def held_off(self) -> Any:
        if self.st in ("red", "offered", "leased", "blocked-external", "dispatch-off") \
                and repo_dispatch_off(self.store, red_repo_of(self.d)):
            return red_hold_off(self.store, self.it, self.row, self.block, self.gone, self.obs, self.t)
        return None

    def dispatch_on_again(self) -> Any:
        if self.st != "dispatch-off":
            return None
        red_offer(self.store, self.it, f"{red_what(self.d)}: dispatch on again for {red_repo_of(self.d)}: offered")
        return [f"offered {red_what(self.d)} (dispatch on again)"]

    def old_run(self) -> Any:
        """AN OLD RED SEEN ONLY NOW (audit #7 surfaces a rarely run workflow): verified by a fresh run, or fixed."""
        if not (self.st in ("red", "offered") and not self.live and not self.fix and not self.block):
            return None
        old = red_old_run(self.d, self.t)
        if not old:
            return None
        done = red_resolve_old(self.store, self.it, old, self.o_r, self.repo, self.t)
        if done is None:
            self.reread()  # no fresh run can check it: an ordinary red, offered below
        return done

    def red(self) -> Any:
        self.it = red_offer(self.store, self.it, f"{red_what(self.d)} is red: offered")
        acts = [f"offered {red_what(self.d)} ({self.it['data']['tier']})"]
        if self.it["data"]["tier"] == "blame":  # a culprit decided by what is known already (Main's green parent): now
            acts += blame_step(self.store, self.it, self.repo, self.o_r, self.t) or []
        return acts

    def run_on_way(self) -> Any:
        """A RUN ON THE WAY MAY HAVE FIXED IT (issue 20261107T224109-5c81e4): wait for it, never a fixer meanwhile."""
        way = None if self.live or self.block else red_run_on_way(self.d, self.snap, self.repo)
        if not way:
            return None
        run, sha = way
        self.store.close_offer(f"red:{self.it['id']}", "fix-pending")
        line = (f"{red_what(self.d)}: {sha[:10]} on main reaches its failing job (no Fixes-Red): awaiting "
                f"#{run.get('number')}")
        red_move(self.store, self.it, "fix-pending", line, data={"fix": {
            "status": "verify", "at": self.t, "entry": None, "sha": "", "awaiting": run.get("number"),
            "verify_dispatched": ts(), "verify": f"awaiting #{run.get('number')}, which contains {sha[:10]}"}})
        return [line]

    def offered_taken(self) -> Any:
        """What happened to an offered red since the last tick: cleared, fixed, leased, ended between ticks, blocked."""
        store, it, d, row = self.store, self.it, self.d, self.row
        if self.gone and not self.fix:
            return self.cleared() + red_flake_check(store, it, self.snap, self.repo, self.facts)
        if self.fix:
            store.close_offer(f"red:{it['id']}", "fix-pending")
            return self.fix_pending()
        way = self.run_on_way()
        if way is not None:
            return way
        if self.live:
            store.close_offer(f"red:{it['id']}", "taken")
            red_move(store, it, "leased", f"{red_what(d)}: leased by {row['holder']}", data={"holder": row["holder"]})
            return [f"leased {red_what(d)} to {row['holder']}"]
        return self.offered_ended()

    def offered_ended(self) -> Any:
        """An offered red nobody holds: its lease taken AND ended between two ticks (its attempt -- once, red_lease_read:
        a re-offer keeps entered_at), or an external block on it; else None."""
        store, it, d, row = self.store, self.it, self.d, self.row
        if row and float(lease_meta(row).get("released_at") or 0) > it["entered_at"] and \
                float(row.get("acquired_at") or 0) >= it["entered_at"] and not red_lease_read(d, row):
            return red_lease_ended(store, it, row, self.block, self.t, self.repo)
        if self.block:
            store.close_offer(f"red:{it['id']}", "blocked")
            red_move(store, it, "blocked-external", f"{red_what(d)}: {self.block['text']}",
                     data={"cause": self.block.get("cause"), "block": self.block["name"]})
            return [f"blocked {red_what(d)}"]
        return None

    def offered(self) -> Any:
        taken = self.offered_taken()
        if taken is not None:
            return taken
        store, it, d = self.store, self.it, self.d
        if (d.get("blame") or {}).get("state") == "probing":  # its culprit is being decided: no escalation meanwhile
            if not store.offer_of(f"red:{it['id']}"):
                red_put_offer(store, it)
            return blame_step(store, it, self.repo, self.o_r, self.t) or []
        return self.offered_waiting()

    def offered_waiting(self) -> Any:
        """An offered red nobody has taken: escalated when it starved or its deadline passed, else repaired."""
        store, it, d, t = self.store, self.it, self.d, self.t
        off = store.offer_of(f"red:{it['id']}") or {}
        if str(off.get("reason") or "").startswith("STARVED") and d.get("starved") != off.get("id"):
            return self.escalate_starved(off)
        if t >= it["deadline"]:  # no taker in time: escalate (the state's self-transition), widen, offer again
            return self.escalate_unclaimed(off)
        if not off:  # repair: an offered red always has its open offer
            red_put_offer(store, it)
        return self.acts

    def claim_hint(self) -> Any:
        return f" -- `~/bin/agentctl red claim {self.key} --worktree <yours>` to take it"

    def escalate_starved(self, off: Any) -> Any:
        """Waited past RED_STARVE with no free fixer slot: escalated NOW (then every cycle, in escalate_unclaimed)."""
        text = (f"RED STARVED: {red_what(self.d)}: no fixer slot for "
                f"{fmt_age(self.t - float(off.get('opened_at') or self.t))} ({off['reason']})")
        red_offer(self.store, self.it, text, data={"starved": off.get("id")}, escalate=True,
                  verdict=text + self.claim_hint())
        return [text]

    def escalate_unclaimed(self, off: Any) -> Any:
        if lease_is_live(self.store.get(red_lease(self.key)), self.t):  # taken since this step read its lease: never
            return self.acts  # UNCLAIMED with a holder; the next step moves it to leased
        d, mins = self.d, int((self.t - self.it["entered_at"]) // 60)
        text = f"RED UNCLAIMED: {red_what(d)}: no taker for {mins} min ({off.get('reason') or 'no taker'})"
        red_offer(self.store, self.it, text, data={"author_tried": True} if d.get("tier") == "author" else None,
                  verdict=text + self.claim_hint())
        return [text]

    def leased(self) -> Any:
        store, it, d, fix, row = self.store, self.it, self.d, self.fix, self.row
        if fix and self.live and not red_fix_proven(store, d, fix):  # A FIX WITH NO REPLAY KEEPS ITS FIXER
            red_keep_holder(row, f"your fix {fix.get('entry') or str(fix.get('sha'))[:10]} carries no Fix-Replay "
                                 f"agentctl ran (base=FAIL head=PASS): the red stays yours, fix-unproven, until its "
                                 f"verification run decides it -- FIX OK or FIX FAILED reaches your inbox")
            red_move(store, it, "fix-pending", f"{red_what(d)}: fix {self.fix_name()} {fix['status']}"
                     f", unproven: {row['holder']} keeps it", data={"fix": {**fix, "unproven": True}})
            return [f"fix-unproven {red_what(d)} ({row['holder']} keeps it)"]
        if fix:
            red_stop_holder(store, self.key, f"its fix {self.fix_name()} is on its way", "fix submitted")
            return self.fix_pending()
        if self.gone:
            return self.cleared()
        if not self.live:
            return red_lease_ended(store, it, row, self.block, self.t, self.repo)
        if row["holder"] != d.get("holder"):
            store.update_item("red", it["id"], it["version"], {"holder": row["holder"]})
        return self.acts + red_diagnosis_step(store, store.item("red", it["id"]), row, self.snap, self.repo, self.o_r,
                                              self.t)

    def pending(self) -> Any:
        return red_fix_pending(self.store, self.it, self.fix, self.gone, self.obs, self.snap, self.facts, self.repo,
                               self.o_r, self.t)

    def blocked_external(self) -> Any:
        store, it, d, fix, row, block, t = self.store, self.it, self.d, self.fix, self.row, self.block, self.t
        if self.live:
            red_move(store, it, "leased", f"{red_what(d)}: a live fixer outranks the block", data={"holder": row["holder"]})
            return [f"leased {red_what(d)} (over its block)"]
        if self.gone:
            return self.cleared()
        if fix:  # A LINKED FIX ANSWERS THE BLOCK (58f3db9e8a3d, 2026-11-12: on the owner only because the fix was not
            # linked): the red waits for the run that proves it; the owner's question and the block go
            lifted = red_lift_block(store, it, block, f"its fix {fix.get('entry') or str(fix.get('sha'))[:10]} is on its way")
            red_move(store, it, "fix-pending", f"{red_what(d)}: fix {self.fix_name()}"
                     + (f" ({lifted})" if lifted else ""), data={"fix": fix, "cause": None, "block": None, "ask": None})
            return [f"fix-pending {red_what(d)}"]
        return self.acts if block else self.block_lapsed()

    def block_lapsed(self) -> Any:
        """Its block lapsed: the probe passed (verify it), or its justification or max hold is gone."""
        store, it, d, t = self.store, self.it, self.d, self.t
        out = str(lease_meta(store.get(d.get("block") or block_lease(self.key))).get("outcome") or "expired")
        if out.startswith("PROBE PASSED"):
            red_move(store, it, "fix-pending", f"{red_what(d)}: its block's check passes ({out[14:120]}): verifying",
                     data={"fix": {"status": "verify", "at": t, "entry": None, "sha": ""}, "block": None})
            return [f"verifying {red_what(d)} (its block cleared)"]
        red_offer(store, it, f"{red_what(d)}: its block ended ({out[:120]}): offered again",
                  data={"cause": None, "block": None},
                  escalate=not (out.startswith(("INVALID", "RELEASED")) or red_decision(d)))
        return [f"re-offered {red_what(d)} (block ended: {out[:60]})"]


def red_step(store, it, obs, snap, facts, repo, o_r, t):
    return RedStep(store, it, obs, snap, facts, repo, o_r, t).run()


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


# ONE END, ONE ATTEMPT (2026-11-18 23:42-23:49Z, core Main Pipeline #1053, 0c8eea91b712): one fixer lease, revoked
# ORPHANED once, was read into the red's history five times a minute apart -- the `offered` step matched the same
# ended lease at every tick, since the re-offer (a self-transition) keeps the item's entered_at -- and "3 launches in
# a row" twice made a CRASH LOOP and then BLOCKED ON OWNER, with no fixer started after the first. The epoch of the
# lease whose end the red read is kept (`ended_lease`); that end is never read again.
def red_lease_read(d, row):
    """True when the red's history already holds the end of this lease (its epoch)."""
    return bool(row) and row.get("epoch") is not None and d.get("ended_lease") == row.get("epoch")


# A RED IS NAMED BY ITS WORKFLOW (2026-11-12, b059f05c70fa): opened while the forge read a run's run-name for its
# workflow's name, the red "Identity takes (convergence run 43013487979)" #40 named a workflow no other run is in: no
# newer run of it could verify, refute or clear it, its verify dispatch found "no workflow file named" it, and it sat in
# fix-pending 6 h at a time. Such a red takes its workflow's name from its failing run's workflow file, once.
def workflow_settled(d: Any) -> bool:
    """Whether red data d needs no naming: named already, or a local or predicted red (no run of its own workflow)."""
    return bool(d.get("workflow_named") or d.get("workflow") == LOCAL_FLOW or d.get("red_kind") in ("local", "predicts"))


def workflow_rename(d: Any, name: Any, run: Any, at: Any) -> dict[str, Any]:
    """The update marking red data d named: renamed to `name` (the run's workflow file's) with a history line when
    that differs from the workflow it carries."""
    upd = {"workflow_named": True}
    if name and name != d.get("workflow"):
        upd["workflow"] = name
        upd["history"] = (d.get("history") or []) + [{"at": at, "holder": None,
                                                     "outcome": f"RENAMED: {d.get('workflow')!r} is a run-name; its "
                                                                f"workflow is {name!r} ({run.get('workflow_id')})"}]
    return upd


def red_workflow_named(store, it, o_r):
    """The red under its workflow's name (renamed in place when its run says another); it unchanged otherwise."""
    d = it["data"]
    rid = (d.get("last_run") or {}).get("databaseId")
    if not rid or workflow_settled(d):
        return it
    files = main_workflow_files(o_r)
    if not files or d.get("workflow") in files:
        return it
    run = attempt(lambda: forge_actions_run(o_r, rid), None, catch=(Exception, SystemExit))
    if not run:
        return it  # unread now: tried again next tick
    name = {f: n for n, f in files.items()}.get(run.get("workflow_id"))
    store.update_item("red", it["id"], it["version"], workflow_rename(d, name, run, iso_at(store.clock())))
    return store.item("red", it["id"])
