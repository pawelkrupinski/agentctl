# agentctl, part `redpending`: a pending fix: following its work to main and judging it (red_fix_pending).
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.


from typing import Any
from agentctl.timefmt import iso_at, parse_time, ts
from agentctl.core import env_num, fmt_age
from agentctl.workflows import LOCAL_FLOW
from agentctl.inboxentry import inbox_write
from agentctl.redworkflows import verify_dispatchable
from agentctl.ancestry import contains_local
from agentctl.redrecord import RED_RECURRING, lease_is_live, red_lease, red_what
from agentctl.redfixread import FIX_OPEN
from agentctl.fixchange import red_change, red_fix_fresh
from agentctl.redfixes import (
    red_entry_status, red_fix_is_set_aside, red_fix_of, red_fix_on_main, red_main_equivalent, red_refuted_with,
    fix_failed_said, red_set_aside)
from agentctl.redask import red_decision
from agentctl.red import (
    RED_FIX_GIVEUP, RED_FIX_RUN_START, red_fix_giveup, red_verify_plan, red_verify_run_on_way,
    red_waits_for_its_run)
from agentctl.redmove import red_close, red_keep_holder, red_move, red_stop_holder
from agentctl.landwatch import WATCH_SLACK
from agentctl.rundispatch import red_verify_dispatch
from agentctl.redopen import red_offer
from agentctl.redverify import red_fix_since, red_fix_unverifiable, red_verify_await, red_verify_scheduled
from agentctl.ask import red_decision_failed
from agentctl.blame import blame_refuted
from agentctl.redwaits import RED_WAITS_ON, red_waits_on_step
from agentctl.redfollow import red_fix_follow, red_fix_requeue_wait
from agentctl.redfixcheck import fix_reaches_red


class FixPending:
    """A fix-pending red judged on one tick: its fix followed to main, its verification, its give-up. Each stage answers
    what the tick did (a list of lines), or None for the next stage; a stage may move the red on (refresh)."""

    def __init__(self, store: Any, it: dict[str, Any], fix: Any, gone: Any, obs: Any, snap: Any, facts: Any, repo: Any,
                 o_r: Any, t: float) -> None:
        self.store, self.fix, self.gone, self.obs, self.snap = store, fix, gone, obs, snap
        self.facts, self.repo, self.o_r, self.t = facts, repo, o_r, t
        self.it = it
        self.d = it["data"]
        self.f = self.d.get("fix") or {}
        self.sha, self.last, self.holds = "", {}, False

    def refresh(self, it: dict[str, Any]) -> None:
        """Read the red again, as the store has it now."""
        self.it = it
        self.d, self.f = it["data"], it["data"]["fix"]

    def reload(self) -> None:
        self.refresh(self.store.item("red", self.it["id"]))

    @property
    def what(self) -> str:
        return str(red_what(self.d))

    def other_fix(self) -> bool:
        """Another fix than this red's own is on its way: it is the one verified now."""
        return bool(self.fix and self.fix.get("sha") != self.f["sha"])

    def cleared(self) -> bool:
        return bool(self.obs is not None and self.gone)

    def give_up_after(self) -> float:
        return float(env_num("AGENTCTL_RED_FIX_GIVEUP", RED_FIX_GIVEUP))

    def history_with(self, h: dict[str, Any]) -> list[Any]:
        return (self.d.get("history") or []) + [h]

    def entry(self, holder: Any, outcome: str, **extra: Any) -> dict[str, Any]:
        return {"at": iso_at(self.t), "holder": holder, "outcome": outcome, **extra}

    def attempts(self) -> int:
        return int(self.d.get("attempts") or 0) + 1

    def offered_again(self, line: str, said: str, **data: Any) -> list[str]:
        """The red goes back to a fixer: `line` in its history, `said` for the tick."""
        red_offer(self.store, self.it, line, data={"fix": None, **data})
        return [said]

    def offered_again_with(self, h: dict[str, Any], said: str) -> list[str]:
        return self.offered_again(f"{self.what}: {h['outcome']}", said, history=self.history_with(h))

    def fix_ok(self, line: str, close: bool = True, stop: tuple[str, str] | None = None) -> list[str]:
        """FIX OK: the red is verified and the owner told."""
        inbox_write("red", line, [line], [], 0)
        if close:
            self.store.close_offer(f"red:{self.it['id']}", "verified")
        if stop:
            red_stop_holder(self.store, self.d["key"], *stop)
        red_move(self.store, self.it, "verified", line)
        return [line]

    def unverifiable(self, why: str) -> Any:
        return red_fix_unverifiable(self.store, self.it, why, self.t)

    def run(self) -> Any:
        for stage in (self.waits_on, self.set_aside, self.landed_on_main, self.other_reds_fix, self.adopt_fix, self.owner_fix,
                      self.verify_fix, self.ended_fix):
            done = stage()
            if done is not None:
                return done
        return self.verification()

    # -- the stages of a fix that is not (yet) verified
    def waits_on(self) -> Any:
        if self.f.get("status") == RED_WAITS_ON and not self.cleared():
            return red_waits_on_step(self.store, self.it, self.f, self.repo, self.t)
        return None

    def set_aside(self) -> list[str] | None:
        """PENDING ON A FIX IT ALREADY REFUTED (or set aside): no run can verify it now; back to a fixer."""
        d, f, fix = self.d, self.f, self.fix
        if not (f.get("sha") and red_fix_is_set_aside(f["sha"], red_set_aside(d, self.facts), self.facts)):
            return None
        if self.other_fix():  # another fix is on its way: it is the one verified now
            line = (f"{self.what}: its fix {f['sha'][:10]} was already set aside; fix "
                    f"{fix.get('entry') or str(fix.get('sha'))[:10]} {fix['status']}")
            red_move(self.store, self.it, "fix-pending", line,
                     data={"fix": {**fix, "verify_since": self.t} if fix["status"] == "landed" else fix})
            return [line]
        return self.offered_again(f"{self.what}: its fix {f['sha'][:10]} was already refuted: offered again",
                                  f"re-offered {self.what} (its fix {f['sha'][:10]} already refuted)")

    def landed_on_main(self) -> list[str] | None:
        """A LANDED FIX IS A COMMIT ON MAIN (above red_main_equivalent): else, or if it landed before this spell opened,
        offered again; landed as another sha, verified as that one."""
        f = self.f
        if not (f.get("status") == "landed" and f.get("sha")):
            return None
        on = red_fix_on_main(f, self.repo)
        done = self.not_on_main(on) or self.predates_spell(on)
        if done:
            return done
        if on is not None and on["sha"] != f["sha"]:
            line = f"{self.what}: its fix {f['sha'][:10]} landed as {on['sha'][:10]}: verifying that"
            self.refresh(red_move(self.store, self.it, "fix-pending", line, data={"fix": on}))
        return None

    def not_on_main(self, on: Any) -> list[str] | None:
        f = self.f
        if on is not None or self.other_fix():
            return None
        h = self.entry(f.get("entry") or f["sha"][:10], f"its fix {f['sha'][:10]} is not on main (nor its content): offered again")
        return self.offered_again_with(h, f"re-offered {self.what} (its fix {f['sha'][:10]} is not on main)")

    def predates_spell(self, on: Any) -> list[str] | None:
        """It landed before this spell opened: the run that opened it already contained it (red_fix_of)."""
        f = self.f
        if not (on is not None and self.landed_before_spell() and not self.other_fix()):
            return None
        h = self.entry(f.get("entry") or f["sha"][:10], f"its fix {on['sha'][:10]} landed before this red opened (its "
                                                        f"failing run contained it): offered again")
        return self.offered_again_with(h, f"re-offered {self.what} (its fix {on['sha'][:10]} predates it)")

    def landed_before_spell(self) -> bool:
        at = self.f.get("at")
        return at is not None and float(at) < float(self.d.get("opened_at") or 0) - 3600

    def other_reds_fix(self) -> list[str] | None:
        """PENDING ON ANOTHER RED'S FIX (PER RED, PER FAILING STEP: Order independence #74's swift red on 09e482c6a, the
        ios red's fix): no run of it can prove this red fixed -- offered again, never parked until the give-up. Not a red
        whose fixer said its fix IS that red's commit (WAITS ON, red_waits_on_step: `waited_on`): the commit on main is
        the verification it waited for, whatever the reading rules make of its failing job (review 2026-11-16)."""
        f = self.f
        if not (f.get("sha") and not self.fix and not f.get("waited_on") and not fix_reaches_red(self.repo, f["sha"], self.d)):
            return None
        return self.offered_again(f"{self.what}: its fix {f['sha'][:10]} touches nothing its failing job reads: offered again",
                                  f"re-offered {self.what} (its fix {f['sha'][:10]} is another red's)")

    def adopt_fix(self) -> None:
        """Another change is the fix now (or its own change landed that very commit): its record, never the last one's."""
        f = self.f
        if self.fix and not self.fix.get("entry") and f.get("entry"):  # its own change landed that very commit
            cur = f if f.get("status") == "landed" else red_fix_fresh(self.store, f)
            if cur and cur.get("status") == "landed" and cur.get("sha") == self.fix.get("sha"):
                self.fix = cur
        fix = self.fix
        if fix and (fix.get("entry"), fix.get("sha"), fix["status"]) != (f.get("entry"), f.get("sha"), f.get("status")):
            self.follow_fix(fix)
            self.refresh(self.it)

    def follow_fix(self, fix: dict[str, Any]) -> None:
        d, f, it = self.d, self.f, self.it
        if fix["status"] == "landed" and f.get("status") != "landed":
            # VERIFICATION STARTS WHEN THE FIX LANDS: the state's deadline (and red_fix_since) run from here, never
            # from the hours it waited in the queue behind a red gate
            landed = {**f, **fix, "verify_since": self.t} if f.get("status") in FIX_OPEN else {**f, **fix}
            self.it = red_move(self.store, it, "fix-pending", f"{self.what}: fix {fix.get('entry') or fix.get('sha')} landed "
                                                              f"({str(fix.get('sha'))[:10]}): verifying", data={"fix": landed})
        else:  # another change is the fix now: its own record, never the last one's fields (a stale `linked`)
            self.store.update_item("red", it["id"], it["version"],
                                   {"fix": {**f, **fix} if fix.get("entry") == f.get("entry") else fix})
            self.it = self.store.item("red", it["id"])

    def owner_fix(self) -> list[str] | None:
        """An owner-only repo's fix, filed with the owner (red owner-push): a green run proves it."""
        d, f, it, t = self.d, self.f, self.it, self.t
        if f.get("status") != "owner":
            return None
        if self.cleared():
            return self.fix_ok(f"FIX OK: {self.what} cleared after its fix went to the owner ({f.get('entry')})")
        if t - red_fix_since(it) >= self.give_up_after():
            return self.unverifiable(f"its owner push ({f.get('entry')}: `{f.get('cmd')}`) is unproven "
                                     f"{fmt_age(t - red_fix_since(it))} after it was filed")
        if t >= it["deadline"]:  # the owner has not pushed yet, or no gate run has decided since: said again
            line = f"RED OWNER PUSH WAITING: {self.what}: `! {f.get('cmd')}` ({fmt_age(t - red_fix_since(it))} since filed)"
            inbox_write("red", line, [line], [], 1)
            red_move(self.store, it, "fix-pending", line, severity="escalate")
            return [line]
        return []

    def latest_run(self) -> dict[str, Any]:
        return ((self.snap or {}).get("latest") or {}).get(self.d.get("workflow")) or {}

    def run_after_fix(self, last: dict[str, Any], slack: float = 0) -> bool:
        """The latest decided run `last` of the red's workflow is newer than its fix."""
        return bool(last.get("updatedAt")) and parse_time(last["updatedAt"]).timestamp() > float(self.f.get("at") or 0) + slack

    def verify_fix(self) -> list[str] | None:
        """An owner block's check passed: the next decided run says whether it is fixed."""
        d, f, it, t = self.d, self.f, self.it, self.t
        if f.get("status") != "verify":
            return None
        after = self.run_after_fix(self.latest_run())
        if self.cleared():
            return self.fix_ok(f"FIX OK: {self.what} cleared after its owner block's check passed", close=False)
        if self.obs is not None and after and d["key"] in self.obs:
            return self.verify_refuted()
        if not d.get("gate") and not f.get("verify_dispatched"):
            line = red_verify_dispatch(self.o_r, self.repo, d.get("workflow"), "", (d.get("last_run") or {}).get("databaseId"))
            self.store.update_item("red", it["id"], it["version"], {"fix": {**f, "verify_dispatched": ts(), "verify": line}})
            if line.startswith("dispatched"):
                return [line]
            self.reload()
        return self.verify_unfinished()

    def verify_refuted(self) -> list[str]:
        """The run after its block cleared still fails: offered again."""
        f, d = self.f, self.d
        if f.get("claimed"):  # its fixer said "already fixed" and the run refutes it: that fixer's attempt failed
            return self.offered_again(f"{self.what}: still red after its fixer found it already fixed: offered again",
                                      f"re-offered {self.what} (not fixed after all)", attempts=int(d.get("attempts") or 0) + 1)
        if f.get("awaiting"):  # the run it awaited still fails this way: nobody's fix was refuted
            return self.offered_again(f"{self.what}: still red on #{f['awaiting']}, the run it awaited: offered again",
                                      f"re-offered {self.what} (still red on #{f['awaiting']})")
        return self.offered_again(f"{self.what}: still red after its block cleared: offered again",
                                  f"re-offered {self.what} (still red after its block)")

    def verify_unfinished(self) -> list[str]:
        """NO VERIFY CAN COME: its verifying dispatch failed, or no decided run came by the deadline -- a fix-pending with
        no fix on its way never rests (Archive recording #823, 2026-10-29: the dispatch was refused with HTTP 400, its
        workflow runs only on demand, and the red sat "fix None verify" for 19 h, escalating every 3 h, no fixer)."""
        d, f, it, t = self.d, self.f, self.it, self.t
        failed = str(f.get("verify") or "").startswith("FIX UNVERIFIED:")  # refused; "until its next scheduled run" waits
        if failed or t - it["entered_at"] >= self.give_up_after():
            return self.unverifiable(f"its verifying run could not start ({str(f.get('verify'))[:120]})"
                                     if failed else f"no decided {d.get('workflow')} run "
                                                    f"{fmt_age(t - it['entered_at'])} after it entered verify")
        if t >= it["deadline"]:
            red_move(self.store, it, "fix-pending", f"RED {self.what}: unverified {fmt_age(t - it['entered_at'])} after its block",
                     severity="escalate")
            return [f"escalated {self.what}: unverified"]
        return []

    def ended_fix(self) -> list[str] | None:
        """A fix not landed: its change ended (followed to the change that carries its work), its link no longer holds,
        or it was rejected, cancelled or superseded."""
        f = self.f
        if f.get("status") == "landed":
            return None
        status, done = self.status_by_store()
        if done is not None:
            return done
        moved, ended = red_fix_follow(self.store, f, self.repo, self.t)
        if moved:
            return self.followed(moved, ended)
        if ended and red_fix_requeue_wait(self.store, f, self.t):
            return []
        return self.change_state(ended or status)

    def change_state(self, status: Any) -> list[str]:
        """What a fix whose change is in `status` (not landed) comes to: waiting, its link lost, or ended."""
        f = self.f
        if status in FIX_OPEN:
            return self.link_lost() if f.get("linked") and not (self.fix and self.fix.get("entry") == f.get("entry")) else []
        return [] if self.fix else self.change_ended(status)

    def status_by_store(self) -> tuple[Any, list[str] | None]:
        """(its change's status, None) -- the store's word on it outranks the entry the facts read; (_, lines) when the
        store says it landed."""
        f = self.f
        status = red_entry_status(self.facts, f.get("entry"))
        cur = red_fix_fresh(self.store, f)
        if cur is f or not cur:
            return status, None
        if cur.get("status") == "landed":
            line = f"{self.what}: fix {f.get('entry')} landed ({str(cur['sha'])[:10]}): verifying"
            red_move(self.store, self.it, "fix-pending", line, data={"fix": {**cur, "verify_since": self.t}})
            return status, [line]
        return cur.get("status"), None

    def followed(self, moved: dict[str, Any], ended: Any) -> list[str]:
        """Its change ended; the change that carries its work since landed (verifying that), or is still open (followed)."""
        if moved["status"] == "landed":
            line = (f"{self.what}: its fix {self.f.get('entry')} ended {ended}; its work landed in {moved['entry']} "
                    f"({str(moved['sha'])[:10]}): verifying")
            red_move(self.store, self.it, "fix-pending", line, data={"fix": {**moved, "verify_since": self.t}})
            return [line]
        if moved["entry"] != self.f.get("entry"):
            self.store.update_item("red", self.it["id"], self.it["version"], {"fix": moved})
        return []

    def link_lost(self) -> list[str]:
        """A LINK THAT NO LONGER HOLDS IS NO FIX (2026-11-20, Order independence #89: parked on metrics-packages, linked
        through a script by a rule since dropped, while its owner held the lease): back to its holder."""
        d, t = self.d, self.t
        why = f"{self.f.get('entry')} no longer links it (no Fixes-Red, nor a change to its failing step)"
        row = self.store.get(red_lease(d["key"]))
        if lease_is_live(row, t):
            red_move(self.store, self.it, "leased", f"{self.what}: {why}: its holder {row['holder']} keeps it",
                     data={"fix": None, "holder": row["holder"]})
            return [f"leased {self.what} ({why})"]
        return self.offered_again(f"{self.what}: {why}: offered again", f"re-offered {self.what} ({why})")

    def change_ended(self, status: Any) -> list[str]:
        """Its change was rejected, cancelled or superseded, and no other fix is on its way. WHY IT ENDED, where the owner
        and the next fixer read (2026-11-22, #349: "ended rejected" in red status and the inbox, the reason only in the
        change item's own `why`)."""
        f, d = self.f, self.d
        why = " ".join(str((red_change(self.store, f.get("entry")) or {}).get("data", {}).get("why") or "").split())[:300]
        h = self.entry(f.get("entry"), f"its fix change {f.get('entry')} ended {status}" + (f": {why}" if why else ""))
        if red_decision(d):  # the owner's decided action: its change did not land -- the owner is told
            return red_decision_failed(self.store, self.it, {"attempts": self.attempts(), "fix": None,
                                                             "history": self.history_with(h)},
                                       f"NOT LANDED: {h['outcome']}", self.t)
        red_offer(self.store, self.it, f"{self.what}: {h['outcome']}: offered again",
                  data={"attempts": self.attempts(), "history": self.history_with(h), "fix": None})
        return [f"re-offered {self.what} ({h['outcome']})"]

    # -- a landed fix: the run that proves it
    def read_run(self) -> None:
        """The fix's sha, the latest decided run of the workflow, and whether that run holds the fix (contains it)."""
        f = self.f
        self.sha = f.get("sha") or ""
        self.last = self.latest_run()
        sha, last = self.sha, self.last
        inside = contains_local(self.repo, sha, last.get("headSha")) if sha and last else False
        self.holds = bool(inside) or (inside is None and self.run_after_fix(last, WATCH_SLACK))
        if self.d.get("workflow") == LOCAL_FLOW:
            self.holds = self.gone

    def verification(self) -> Any:
        self.read_run()
        for stage in (self.run_cleared, self.run_refuted, self.run_before_fix, self.slot_wait, self.dispatch_outside,
                      self.give_up, self.verify_by_deadline, self.dispatch_missed, self.deadline):
            done = stage()
            if done is not None:
                return done
        return []

    def run_cleared(self) -> list[str] | None:
        d, last, sha = self.d, self.last, self.sha
        if not (self.cleared() and (self.holds or d.get("red_kind") in ("local", "flake", "stuck-run", "predicts"))):
            return None
        if d.get("red_kind") == "flake" and not self.holds:
            return []
        line = f"FIX OK: {self.what} cleared by {sha[:10]}" + (f" (#{last.get('number')})" if last else "")
        return self.fix_ok(line, stop=(f"verified by {sha[:10]}", "red verified"))

    def run_refuted(self) -> list[str] | None:
        """THE FIX DID NOT FIX IT: refuted, its holder's lease dropped, offered again with the failed fix in the picture --
        an UNPROVEN fix's holder keeps the red (A FIX WITH NO REPLAY KEEPS ITS FIXER): back to it, leased."""
        d, last = self.d, self.last
        if not (self.obs is not None and self.holds and d["key"] in self.obs
                and str(self.obs[d["key"]]["run"].get("databaseId")) == str(last.get("databaseId"))):
            return None
        row = self.store.get(red_lease(d["key"]))
        if self.f.get("unproven") and lease_is_live(row, self.t):
            return self.refuted_unproven(row)
        return self.refuted_fix()

    def refuted_unproven(self, row: dict[str, Any]) -> list[str]:
        d, f, sha, last = self.d, self.f, self.sha, self.last
        line = fix_failed_said(d, sha, last)
        red_keep_holder(row, f"{line}: the red is still yours -- fix it again, with a `Fix-Replay:` this time")
        h = self.entry(f.get("entry") or sha[:10], f"fix {sha[:10]} refuted by #{last.get('number')} (unproven; its holder keeps it)")
        red_move(self.store, self.it, "leased", f"{line}: back to {row['holder']}",
                 data={"attempts": self.attempts(), "fix": None, "holder": row["holder"],
                       "refuted": red_refuted_with(d, sha),
                       "history": self.history_with(h)})
        return [line]

    def refuted_fix(self) -> list[str]:
        d, f, sha, last = self.d, self.f, self.sha, self.last
        red_stop_holder(self.store, d["key"], f"your fix {sha[:10]} is refuted by #{last.get('number')}", "fix refuted",
                        force=True)
        refuted_blame = blame_refuted(self.repo, d.get("blame"), sha)  # the red's own revert: the culprit was wrong
        line = fix_failed_said(d, sha, last)
        refuted = red_refuted_with(d, sha)
        h = self.entry(f.get("entry") or sha[:10], f"fix {sha[:10]} refuted by #{last.get('number')}",
                       session=f.get("session"), worktree=f.get("worktree"))
        red_offer(self.store, self.it, line, data={"attempts": self.attempts(), "refuted": refuted, "fix": None,
                                                   "history": self.history_with(h),
                                                   "recurring": len(refuted) >= RED_RECURRING,
                                                   **({"blame": refuted_blame} if refuted_blame else {})})
        if not refuted_blame:
            return [line]
        line2 = (f"RED REVERT REFUTED: {self.what}: reverting {sha[:10]} did not clear it, so its change was not "
                 "the culprit: it is relanded (its revert reverted) and the red goes to the normal fixer")
        inbox_write("red", line2, [line2], [], 1)
        return [f"{line}\n{line2}"]

    def run_before_fix(self) -> list[str] | None:
        if not (self.cleared() and not self.holds):
            return None
        red_close(self.store, self.it, "cleared", f"a run before its fix {self.sha[:10]} no longer fails this way")
        return [f"cleared {self.what}"]

    def slot_wait(self) -> list[str] | None:
        """A SCHEDULED WORKFLOW'S FIX WAITS FOR ITS SLOT (red_verify_plan): a listed workflow goes on to its verify dispatch
        in this same tick, an unlisted one's tick ends here."""
        d, f = self.d, self.f
        if not (not self.holds and red_waits_for_its_run(d) and "next_run_at" not in f and d.get("workflow") != LOCAL_FLOW):
            return None
        plan = red_verify_plan(self.o_r, self.repo, d.get("workflow"), float(f.get("verify_since") or f.get("at") or self.t))
        said = None
        if plan:
            said = red_verify_await(self.store, self.it, f, plan, self.t)
        else:
            self.store.update_item("red", self.it["id"], self.it["version"], {"fix": {**f, "next_run_at": None}})  # no schedule
        self.reload()
        return said if plan and not verify_dispatchable(self.repo, self.d.get("workflow")) else None

    def dispatch_due(self) -> bool:
        d, f = self.d, self.f
        return bool(not self.holds and not d.get("gate") and not f.get("verify_dispatched")
                    and (self.t - float(f.get("at") or self.t) > RED_FIX_RUN_START or f.get("next_run_at"))
                    and d.get("red_kind") in ("outside",)
                    and not (f.get("next_run_at") and not verify_dispatchable(self.repo, d.get("workflow"))))  # its slot decides

    def dispatch_outside(self) -> list[str] | None:
        if not self.dispatch_due():
            return None
        d, f, sha = self.d, self.f, self.sha
        line = red_verify_dispatch(self.o_r, self.repo, d.get("workflow"), sha, (d.get("last_run") or {}).get("databaseId"))
        if line.startswith("FIX UNVERIFIED: no workflow file"):  # NO RUN CAN EVER PROVE IT: offered now, never parked
            return self.unverifiable(f"its fix {sha[:10]}: {line[len('FIX UNVERIFIED: '):][:160]}")
        if line.startswith("FIX UNVERIFIED:"):  # the failing run's inputs cannot be dispatched again: said so, and the
            line += (f"; the newest decided {d.get('workflow')} run on main containing {sha[:10]} decides it "  # next
                     f"(offered again if none by {red_fix_giveup(d) // 3600} h after it landed)")  # natural run decides
        self.store.update_item("red", self.it["id"], self.it["version"], {"fix": {**f, "verify_dispatched": ts(), "verify": line}})
        return [line]

    def give_up(self) -> Any:
        t, it, d, f, sha = self.t, self.it, self.d, self.f, self.sha
        if t - red_fix_since(it) < red_fix_giveup(d):
            return None
        return self.unverifiable(f"its fix {sha[:10] or f.get('entry')} unverified for "
                                 f"{fmt_age(t - red_fix_since(it))} after it landed (no run of "
                                 f"{d.get('workflow')} proves it)")

    def verify_by_deadline(self) -> Any:
        f = self.f
        if self.t >= self.it["deadline"] and f.get("verify_by") and not self.holds:
            return red_verify_scheduled(self.store, self.it, f, self.sha, self.o_r, self.repo, self.t)
        return None

    def dispatch_missed(self) -> Any:
        """A MISSED DEADLINE IS NEVER SILENT: its dispatched run never came."""
        f, t = self.f, self.t
        if not (t >= self.it["deadline"] and not self.holds and str(f.get("verify") or "").startswith("dispatched")
                and not red_verify_run_on_way(self.o_r, self.repo, f)):
            return None
        return self.unverifiable(f"fix {self.sha[:10]} unverified {fmt_age(t - red_fix_since(self.it))} after "
                                 f"its verifying dispatch ({str(f.get('verify'))[:100]})")

    def deadline(self) -> list[str] | None:
        """Awaiting its workflow's next run: no one's to act on (said quietly); else escalated."""
        t, it, d, f, sha = self.t, self.it, self.d, self.f, self.sha
        if t < it["deadline"]:
            return None
        awaiting = red_fix_giveup(d) > self.give_up_after()
        red_move(self.store, it, "fix-pending", f"RED {self.what}: fix {sha[:10] or f.get('entry')} unverified for "
                                                f"{fmt_age(t - it['entered_at'])}"
                 + (f" (awaiting {d.get('workflow')}'s next run)" if awaiting else ""),
                 severity="info" if awaiting else "escalate")
        return [f"{'re-deadlined' if awaiting else 'escalated'} {self.what}: fix unverified"]


def red_fix_pending(store: Any, it: dict[str, Any], fix: Any, gone: Any, obs: Any, snap: Any, facts: Any, repo: Any, o_r: Any,
                    t: float) -> Any:
    """Judge a fix-pending red on this tick (FixPending's stages): the lines of what it did."""
    return FixPending(store, it, fix, gone, obs, snap, facts, repo, o_r, t).run()


