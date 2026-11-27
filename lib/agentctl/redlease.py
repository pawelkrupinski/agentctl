# agentctl, part `redlease`: a red fixer's lease that ended: its attempt, the owner's ask, the waits and the fix already landed
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import os
import re
from typing import Any
from agentctl.core import remote
from agentctl.onmain import landed_state
from agentctl.repos import RED_PRIMARY, red_repo_of
from agentctl.claudefiles import claude_limit, claude_limit_reset
from agentctl.allowedpaths import allowed_path
from agentctl.redrecord import red_closed_of, red_what
from agentctl.leases import lease_meta
from agentctl.redfixes import RED_LANDED_VERDICT, red_fix_of
from agentctl.redask import red_decision
from agentctl.red import red_close, red_move
from agentctl.redopen import red_history, red_offer
from agentctl.redcrash import (
    AGENTCTL_DEFECT_CAUSE, crash_cause, probe_red_newer_verdict, red_ask_route, red_blamed_issue, red_crash_loop,
    red_crash_streak, RED_DECIDE, red_decision_block, red_decision_needed, red_harness_block, red_newer_probe)
from agentctl.escalationtable import OWNER_SIGNALS
from agentctl.fixerlife import GAVE_UP_DECIDE, GAVE_UP_FIXED
from agentctl.redvault import red_vault_block, vault_ask_ns, vault_ask_unfounded
from agentctl.ask import DECISION_FAILED, red_decision_failed
from agentctl.redwaits import RED_WAITS_ON, red_wait_cycle
from agentctl.redknobs import knob_red_crash_loop


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


def red_vault_ask(store: Any, it: Any, upd: dict[str, Any], ns: str, out: str, t: float, o_r: str | None) -> Any:
    """A fixer's vault ask: the red waits on vault ns (red_vault_block) -- unless it names no secret read that failed
    in a repo that does not sign (redvault.py vault_ask_unfounded): then it goes back to its fixer, saying why."""
    unfounded = vault_ask_unfounded(o_r or RED_PRIMARY, out, ns)
    if unfounded:
        return red_ask_route(store, it, upd, unfounded, out, t)
    return red_vault_block(store, it, upd, ns, out, t)


def red_end_outcome(h: dict[str, Any], m: dict[str, Any], row: Any, t: float) -> float | None:
    """The ended lease's outcome `h` as the ladder reads it: a launch on claude's spent usage limit is LIMITED (no
    attempt, no crash: the time it resets is returned, the red's next dispatch), a quick end with no verdict NOT STARTED."""
    end = float(m.get("released_at") or t)
    limit = claude_limit(f"{h['outcome']}\n{h.get('notes') or ''}")
    if limit:  # THE ACCOUNT'S USAGE LIMIT IS SPENT: dispatched again once it resets
        h["outcome"] = f"LIMITED: claude's usage limit is spent ({limit}); the fixer launches again when it resets"
        return claude_limit_reset(limit, end)
    quick = end - float((row or {}).get("acquired_at") or 0) < RED_QUICK_EXIT
    if quick and re.match(r"(exit -?\d+|ended) with no|NOT STARTED", str(h["outcome"])):  # it never got going (a crash at
        h["outcome"] = re.sub(r"^(NOT STARTED: )*", "NOT STARTED: ", str(h["outcome"]))  # start): not an attempt, nor an
    return None  # instant re-dispatch


class LeaseEnd:
    """A red's holder's lease that ended (released with a verdict, expired without progress, revoked): one attempt (not
    for a release by hand, a fixer that never started, a lease lost to infra, a red found already fixed, or one that
    needs the owner's decision: AN ATTEMPT IS A FIXER THAT TRIED AND FAILED TO FIX), the verdict in its history, offered
    again up the ladder. Each stage answers what the tick did, or None for the next."""

    def __init__(self, store: Any, it: dict[str, Any], row: Any, block: Any, t: float, repo: Any) -> None:
        self.store, self.it, self.block, self.t, self.repo = store, it, block, t, repo
        self.d = it["data"]
        self.h = red_history(row, t)
        self.m = lease_meta(row)
        reset = red_end_outcome(self.h, self.m, row, t)
        self.out = str(self.h["outcome"])
        # a wrapper's lease renews by its beat: a lapse is the wrapper or its machine gone (an interactive claim lapses
        # by its own inactivity: that one counts)
        infra = bool(self.h.get("dispatched") and LEASE_INFRA.match(self.out))
        self.counted = not (self.out.startswith(("NOT STARTED", "RELEASED", "LIMITED")) or infra
                            or GAVE_UP_FIXED.match(self.out) or GAVE_UP_DECIDE.match(self.out))
        self.upd = self.attempt_update(row, reset)

    def attempt_update(self, row: Any, reset: float | None) -> dict[str, Any]:
        """What the ended lease changes on the red: its history, the attempt counted, the next dispatch, the diagnosis,
        the work its worktree left."""
        d, h, out = self.d, self.h, self.out
        upd = {"history": (d.get("history") or []) + [h], "holder": None,
               "attempts": int(d.get("attempts") or 0) + (1 if self.counted else 0),
               "ended_lease": (row or {}).get("epoch")}
        upd.update(self.dispatch_update(reset))
        if out.startswith("DIAGNOSED:"):
            upd["diagnosis"] = h["outcome"][len("DIAGNOSED:"):].strip()[:600]
        prior = self.prior_work(h.get("worktree"))
        if prior:
            upd["prior_work"] = prior
        return upd

    def dispatch_update(self, reset: float | None) -> dict[str, Any]:
        """When the red is dispatched next: after the usage limit resets, at once for a lander that could not be revived,
        after a growing backoff for a fixer that never started, at once after an attempt."""
        d, out = self.d, self.out
        if reset is not None:
            return {"dispatch_after": reset}
        if out.startswith("NOT STARTED") and self.m.get("tier") == "lander" and d.get("blame"):
            # THE LANDER COULD NOT BE REVIVED (LAUNCH FAILED): a fresh fixer at once, briefed with the change -- no backoff
            return {"blame": dict(d["blame"], revive_failed=out[:300])}
        if out.startswith("NOT STARTED"):
            fails = int(d.get("start_failures") or 0) + 1
            return {"start_failures": fails, "dispatch_after": self.t + min(RED_START_BACKOFF * 2 ** (fails - 1), 3600)}
        return {"start_failures": 0, "dispatch_after": 0} if self.counted else {}

    def prior_work(self, wt: Any) -> dict[str, Any] | None:
        """Its unlanded commits are never lost: the next fixer starts in the worktree that has them."""
        if not (wt and os.path.isdir(wt) and allowed_path(wt)):
            return None
        state = landed_state(wt, f"refs/remotes/{remote()}/main", "HEAD")
        left = [c[:10] for c in state.left]  # unknown (git cannot compare): no prior work is claimed
        return {"worktree": wt, "commits": left[:20]} if left else None

    def hold_fix_pending(self, why: str, line: str, data: dict[str, Any]) -> None:
        """Park the red in fix-pending, its owner block (if any) answered by what it waits for."""
        lifted = red_lift_block(self.store, self.it, self.block, why)
        self.store.close_offer(f"red:{self.it['id']}", "fix-pending")
        red_move(self.store, self.it, "fix-pending", line + (f" ({lifted})" if lifted else ""), data=data)

    def fixer_landed(self) -> list[str] | None:
        """ITS FIXER LANDED ITS FIX (784badc4406e.4, 2026-11-13: `LANDED 8d06b1a5ab` was offered again, then put on the
        owner): the red waits for the run that proves it."""
        fix = red_fix_of({"data": {**self.d, **self.upd}}, {"repo": self.repo}) if RED_LANDED_VERDICT.match(self.out) else None
        if not fix:
            return None
        sha, what = fix["sha"][:10], red_what(self.d)
        self.hold_fix_pending(f"its fixer landed {sha}", f"{what}: {self.h['holder']} landed its fix {sha}: verifying",
                              {**self.upd, "fix": {**fix, "verify_since": self.t}, "cause": None, "block": None, "ask": None})
        return [f"fix-pending {what} (its fixer landed {sha})"]

    def waits_on_fix(self) -> list[str] | None:
        """A HOLD ON ANOTHER RED'S FIX (red_waits_on_of): a state agentctl resolves itself, never a failed action."""
        waits = red_waits_on_of(self.out, self.d.get("key"), self.store)
        if not waits:
            return None
        sha, what, dec = waits["sha"][:10], red_what(self.d), self.d.get("decision")
        self.hold_fix_pending(f"it waits on red {waits['red']}'s fix {sha}", f"{what}: waits on red {waits['red']}'s fix {sha}",
                              {**self.upd, "fix": {"entry": None, "status": RED_WAITS_ON, "at": self.t, **waits},
                               "cause": None, "block": None, "ask": None,
                               **({"decision": {**dec, "status": "waiting"}} if dec else {})})
        return [f"fix-pending {what} (waits on red {waits['red']}'s {sha})"]

    def blocked_by_owner(self) -> list[str] | None:
        block = self.block
        if not block:
            return None
        red_move(self.store, self.it, "blocked-external", f"{red_what(self.d)}: {block['text']}",
                 data={**self.upd, "cause": block.get("cause"), "block": block["name"]})
        return [f"blocked {red_what(self.d)}"]

    def already_fixed(self) -> list[str] | None:
        """ALREADY FIXED: the red's probe decides, never the fixer's word alone."""
        return red_already_fixed(self.store, self.it, self.upd, self.out, self.t) if GAVE_UP_FIXED.match(self.out) else None

    def needs_owner(self) -> list[str] | None:
        """THE FIXER NEEDS THE OWNER: its one ask is a vault unlock (a wait agentctl checks itself), the owner's decided
        action failed (told), or a decision (one WAITING ON YOU, never offered to the next fixer meanwhile)."""
        h, out, d = self.h, self.out, self.d
        decide = red_decision_needed(d, h) if out.startswith("DIAGNOSED:") else None
        if not decide and GAVE_UP_DECIDE.match(out):
            decide = self.gave_up_ask()
        vault = vault_ask_ns(decide or (out if out.startswith("BLOCKED ON OWNER") else ""))
        if vault:  # ITS ONE ASK IS A VAULT UNLOCK
            return red_vault_ask(self.store, self.it, self.upd, vault, out, self.t, d.get("repo"))
        if red_decision(d) and DECISION_FAILED.match(str(h["outcome"])):  # THE OWNER'S DECIDED ACTION FAILED: told
            return red_decision_failed(self.store, self.it, self.upd, h["outcome"], self.t)
        return red_decision_block(self.store, self.it, self.upd, decide, self.t) if decide else None

    def gave_up_ask(self) -> str:
        """The owner's ask in a GAVE UP verdict, its own words (and the category it names); its words before the ask are
        the diagnosis the owner reads. Routed back (red_ask_route), it counts as the attempt it was."""
        out, upd = self.out, self.upd
        m = RED_DECIDE.search(out)
        upd["gave_up_ask"] = True
        if not m:
            return re.sub(r"^\W*GAVE UP:\s*", "", out).strip()
        upd["diagnosis"] = re.sub(r"^\W*GAVE UP:\s*", "", out[:m.start(1) if m.group(1) else m.start(2)])
        upd["diagnosis"] = re.sub(r"[;\s]*decide\s*$", "", upd["diagnosis"]).strip()[:600]
        return (m.group(1) or "") + m.group(2).strip()

    def blamed_issue(self) -> list[str] | None:
        issue = red_blamed_issue(self.store, self.out)
        if not issue:
            return None
        d, out = self.d, self.out
        red_harness_block(self.store, self.it, self.upd, issue, AGENTCTL_DEFECT_CAUSE, f"~/bin/agentctl issue --list",
                          f"a newer {d.get('workflow')} run no longer fails this way",
                          f"its fixer blamed agentctl: open agentctl issue {issue} ({out[:200]}). Offered again when the "
                          "issue is closed or a newer run decides it", self.t)
        return [f"blocked {red_what(d)} on agentctl issue {issue}"]

    def crash_loop(self) -> list[str] | None:
        streak = red_crash_streak({**self.d, **self.upd})
        if len(streak) < knob_red_crash_loop():
            return None
        cause = crash_cause(streak)
        if cause and OWNER_SIGNALS.search(cause):
            return self.owner_crash_loop(streak, cause)
        return red_crash_loop(self.store, self.it, self.upd, streak, self.t, same_cause=cause)

    def owner_crash_loop(self, streak: list[dict[str, Any]], cause: str) -> list[str]:
        """EVERY LAUNCH DIED AT LAUNCH OF AN OWNER-ONLY CAUSE (the vault, Touch ID): no tier and no harness fix helps, so no
        fixer is launched again -- the red waits on the vault's one lease and the owner is told once (`decide vault-<ns>`)."""
        ns = red_repo_of(self.d).rsplit("/", 1)[-1]
        upd = {**self.upd, "crash_loop": {"stage": "owner", "mark": len(self.d.get("history") or []) + 1, "at": self.t,
                                           "from": self.d.get("tier"), "launches": len(streak), "cause": cause}}
        return red_vault_block(self.store, self.it, upd, ns, f"{len(streak)} fixer launches in a row died at launch: {cause}", self.t)

    def offer_again(self) -> list[str]:
        h, what = self.h, red_what(self.d)
        red_offer(self.store, self.it, f"{what}: {h['holder']} ended ({h['outcome'][:120]}): offered again", data=self.upd,
                  quiet=not self.counted)
        return [f"re-offered {what} after {h['outcome'][:60]}"]

    def run(self) -> list[str]:
        for stage in (self.fixer_landed, self.waits_on_fix, self.blocked_by_owner, self.already_fixed, self.needs_owner,
                      self.blamed_issue, self.crash_loop):
            done = stage()
            if done is not None:
                return done
        return self.offer_again()


def red_lease_ended(store: Any, it: dict[str, Any], row: Any, block: Any, t: float, repo: Any = None) -> list[str]:
    """Its holder's lease ended: LeaseEnd's ladder."""
    return LeaseEnd(store, it, row, block, t, repo).run()


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
    try:
        ok, why = probe_red_newer_verdict(red_newer_probe(it, d)["args"], store)
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
