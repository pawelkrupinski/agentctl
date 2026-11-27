# agentctl, part `redview`: how a red reads (ci-why, the status line, `red status`), and dispatch off per repo.
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import json
import time
from typing import Any
from agentctl.statefile import read_record
from agentctl.timefmt import hhmmz, ts
from agentctl.redknobs import RED_HELD_IDLE, knob_red_fix_giveup, knob_red_held_idle
from agentctl.core import state_root, tilde
from agentctl.cistate import snapshot_gate_reds
from agentctl.fixerblockers import short_flow
from agentctl.redrecord import RED_RECURRING, red_active, red_lease, red_names
from agentctl.leases import LeaseKinds, lease_meta, red_block_of
from agentctl.redfixread import FIX_OPEN
from agentctl.owners import owner_record_path
from agentctl.redask import red_ask, red_decision
from agentctl.culpritfix import blame_status
from agentctl.runsigs import run_names
from agentctl.redfixes import red_refuted
from agentctl.red import red_settled_runs
from agentctl.redseen import red_obs_of_units
from agentctl.ask import clean_cut, red_decided_view
from agentctl.reddispatch import REPO_DISPATCH_OFF, red_owned_progress_at, repo_dispatch_off_all
from agentctl.redwaits import RED_WAITS_ON


# -- how a red reads (ci-why, the status line, `red status`): its state, holder or offer, deadline. Never NOBODY.
def gate_red_runs():
    """[(workflow name, decided run)] of the gate's red workflow(s) in the snapshot."""
    return [(g.get("name"), g["decided"]) for g in snapshot_gate_reds()]


def gate_red_now_label():
    """'convergence #2473' for the gate's red workflow(s) in the snapshot, else None."""
    return ", ".join(f"{short_flow(n)} #{dd.get('number')}" for n, dd in gate_red_runs()) or None


def gate_own_red_label(d):
    """'convergence #2550' when red d IS the gate's red (a run of the gate's red workflow carries its key), else None:
    its Fixes-Red fix lands on red (whichever run of the key it names), never blocked by that gate."""
    names = set(map(str, red_names(d)))
    own = [f"{short_flow(n)} #{dd.get('number')}" for n, dd in gate_red_runs()
           if n == d.get("workflow") and run_names(dd) & names]
    return ", ".join(own) or None


def red_view(store, it, t=None):
    """(text, short) of one red, its blame (a revert, its lander revived: blame.py) said with it."""
    text, short = red_decided_view(it["data"], *red_view_state(store, it, t))
    bl = blame_status(it["data"])
    return (f"{text} -- {bl[0]}", f"{short}; {bl[1]}") if bl else (text, short)


def red_lease_lapses(row: dict[str, Any], d: dict[str, Any], t: float) -> float:
    """When red d's live lease row lapses with no progress: its expiry -- or, held by an owner (renewed by its beat), its
    RED_HELD_IDLE past the red's own progress (reddispatch.py)."""
    if LeaseKinds.is_owned(row):
        return max(float(row.get("acquired_at") or t), red_owned_progress_at(row, d)) + float(knob_red_held_idle())
    return float(row.get("expires_at") or t)


class RedViewer:
    """How one red reads (its state, holder or offer, deadline): (text, short), a method per state of the red."""

    def __init__(self, store: Any, it: dict[str, Any], t: float) -> None:
        self.store, self.it, self.t = store, it, t
        self.d = it["data"]
        self.at = time.strftime("%H:%M", time.localtime(it["deadline"]))
        refuted = len(red_refuted(self.d))
        self.rec = f" -- recurring: {refuted} fixes refuted" if refuted >= RED_RECURRING else ""

    def read(self) -> tuple[Any, Any]:
        st = self.it["state"]
        if st == "red":
            return "observed: offered at the next tick", "offering"
        if st == "fix-pending":
            return self.fix_pending()
        reader = {"offered": self.offered, "leased": self.leased, "blocked-external": self.blocked}.get(st)
        return reader() if reader else (st, st)

    def offered(self) -> tuple[str, str]:
        d, it = self.d, self.it
        off = self.store.offer_of(f"red:{it['id']}") or {}
        tier = d.get("tier") or "fresh"
        reason = off.get("reason") or "awaiting a taker"
        late = " (ESCALATED)" if it.get("level") else ""
        return (f"offered{late}: {tier} tier, {reason}; escalates {self.at} (`agentctl red claim {d['key']} --worktree .`){self.rec}",
                f"offered{late}: {tier}, {reason}")

    def leased(self) -> tuple[str, str]:
        row = self.store.get(red_lease(self.d["key"])) or {}
        m = lease_meta(row)
        who = m.get("who") or str(row.get("holder") or "?").split(":")[-1]
        exp = time.strftime("%H:%M", time.localtime(red_lease_lapses(row, self.d, self.t)))
        return (f"{who} ({self.lease_worktree(m)}, leased{self.lease_family(m)}; lapses {exp} without progress)"
                + (f" -- {m['note']}" if m.get("note") else "") + self.rec,
                f"{who.replace('movies-wt-', '').replace('red-fixer-', 'fixer ')} leased")

    def lease_worktree(self, m: dict[str, Any]) -> str:
        wt = m.get("worktree")
        if not wt and m.get("kind") == "owned" and m.get("family"):  # an owner's red: the owner's ONE worktree
            wt = (read_record(owner_record_path(m["family"])) or {}).get("worktree")
        return tilde(str(wt or "?"))

    def lease_family(self, m: dict[str, Any]) -> str:
        prim = str(m.get("primary") or "").split("/")[-1]  # a sibling's primary red; an owner's red has none
        return f" [family {m['family']}{', with ' + prim if prim else ''}]" if m.get("family") else ""

    def fix_pending(self) -> tuple[str, str]:
        d, rec = self.d, self.rec
        f = d.get("fix") or {}
        if f.get("unproven"):  # A FIX WITH NO REPLAY KEEPS ITS FIXER (red.py)
            return (f"fix-unproven: {f.get('entry') or str(f.get('sha'))[:9]} ({f.get('status')}) carries no replay; "
                    f"{d.get('holder') or 'its holder'} keeps it until a {d.get('workflow')} run decides it{rec}",
                    f"fix-unproven {str(f.get('sha') or f.get('entry') or '')[:7]}")
        status = f.get("status")
        reader = {"landed": self.fix_landed, RED_WAITS_ON: self.fix_waits_on, "verify": self.fix_verifying}.get(status)
        if reader:
            return reader(f)
        if status in FIX_OPEN:  # queued, not landed: nothing has tested it yet -- it holds the red
            return self.fix_queued(f)
        return f"fix {f.get('entry')} {status} (Fixes-Red){rec}", f"fix {status}"

    def fix_waits_on(self, f: dict[str, Any]) -> tuple[str, str]:
        return (f"waits on red {f.get('red')}'s fix {str(f.get('sha'))[:10]}: verified against the next "
                f"{self.d.get('workflow')} run once main has it{self.rec}",
                f"waits on red {f.get('red')}'s fix {str(f.get('sha'))[:10]}")

    def fix_verifying(self, f: dict[str, Any]) -> tuple[str, str]:
        giveup = time.strftime("%H:%M", time.localtime(self.it["entered_at"] + knob_red_fix_giveup()))
        return (f"verifying: the next decided {self.d.get('workflow')} run says whether it is fixed ({f.get('verify') or 'awaiting it'}); "
                f"offered again if none by {giveup}{self.rec}", "verifying")

    def fix_landed(self, f: dict[str, Any]) -> tuple[str, str]:
        d, it, rec = self.d, self.it, self.rec
        sha, before_deadline = str(f.get("sha")), self.t < float(it["deadline"])
        if str(f.get("verify") or "").startswith("dispatched") and before_deadline:
            # VERIFY PROMPTLY BY DEFAULT (redverify.py): its dispatched run decides it, never the cron slot (2026-11-21, #99)
            at = str(f.get("verify_dispatched") or "")[11:16]
            return (f"fix landed in {sha[:9]}, verify run dispatched {at}Z ({d.get('workflow')}; verified by {self.verify_by(f)}){rec}",
                    f"fix {sha[:7]} verify run dispatched {at}Z")
        if f.get("next_run_at") and before_deadline:  # red_verify_plan
            return (f"fix landed in {sha[:9]}, awaiting next scheduled run at {hhmmz(f['next_run_at'])} "
                    f"({d.get('workflow')}; verified by {self.verify_by(f)}){rec}",
                    f"fix {sha[:7]} awaiting next scheduled run at {hhmmz(f['next_run_at'])}")
        return (f"fix landed in {sha[:9]}, awaiting a {d.get('workflow')} run containing it{rec}",
                f"fix {sha[:7]} awaiting its run")

    def verify_by(self, f: dict[str, Any]) -> str:
        return hhmmz(f.get("verify_by") or self.it["deadline"])

    def fix_queued(self, f: dict[str, Any]) -> tuple[str, str]:
        # THE FIX OF THE GATE'S OWN RED lands on red (2026-10-29 19:20: convergence #2550's fix, Fixes-Red: 2527,
        # read "blocked by gate convergence #2550" -- blocked by the red it fixes)
        own = gate_own_red_label(self.d)
        if own:
            return (f"fix {f.get('entry')} landing on red (Fixes-Red), {f.get('status')}: it fixes the gate's own "
                    f"red ({own}); verified once it lands{self.rec}",
                    f"landing on red (Fixes-Red), {f.get('status')}")
        by = gate_red_now_label()
        return (f"fix {f.get('entry')} waiting to land ({f.get('status')}" + (f"; blocked by gate {by}" if by else "")
                + f"): verified once it lands (Fixes-Red){self.rec}",
                "fix waiting to land" + (f" (blocked by gate {by})" if by else ""))

    def blocked(self) -> tuple[Any, Any]:
        b = red_block_of(self.it, self.store) or {}
        ask = red_ask(self.it, b)
        if ask:  # WAITING ON YOU (ask.py): the question itself, never a bare cause
            return self.waiting_on_you(b, ask)
        if red_decision(self.d):  # answered: its block is lifted, offered again at the next tick
            return "the owner's answer is in: offered again at the next tick", "re-dispatching"
        if not b:  # its block lapsed (the tick offers it again): never a stale BLOCKED ON OWNER on the status line
            return "no block holds it now: offered again at the next tick", "re-offering"
        if (b.get("meta") or {}).get("harness"):  # agentctl's own fault (redcrash.py red_harness_issue): its maintainer's
            return b.get("text"), f"HARNESS FAULT: agentctl issue {(b.get('meta') or {}).get('issue')}"
        cause = self.d.get("cause")
        return b.get("text") or "BLOCKED ON OWNER", (f"BLOCKED: {cause} down" if cause else "BLOCKED ON OWNER")

    def waiting_on_you(self, b: dict[str, Any], ask: dict[str, Any]) -> tuple[str, str]:
        more = "" if str(b.get("text") or "").startswith("WAITING ON YOU") else f" -- {b.get('text')}"
        until = f"; auto-clears when {ask['until']}" if ask.get("until") else ""
        return (f"WAITING ON YOU: {ask['q']} -- {ask['how']}{until}{more}",
                f"WAITING ON YOU: {clean_cut(ask['q'], 160)} -- {ask['how']}")


def red_view_state(store: Any, it: dict[str, Any], t: float | None = None) -> tuple[Any, Any]:
    """(text, short) of one red's state."""
    return RedViewer(store, it, t if t is not None else store.clock()).read()


def _settled_view(settled: dict[Any, Any], keys: list[Any], run_key: tuple[Any, Any]) -> dict[str, Any]:
    """A run none of whose reds is active: proven fixed by its callers' runs (never offered again), else observed."""
    done = [settled.get((x, str(run_key[1]))) for x in keys]
    if all(done):  # its newest run for good, proven fixed by its callers' runs
        proof = done[0]["data"].get("proof") or "proven fixed by its callers' runs"
        return {"state": "verified", "text": f"verified: {proof}", "short": "verified", "cause": None}
    return {"state": "red", "text": "observed: offered at the next tick", "short": "offering", "cause": None}


RED_STATE_RANK = {"leased": 0, "fix-pending": 1, "blocked-external": 2, "offered": 3, "red": 4}


def _active_view(store: Any, its: list[dict[str, Any]]) -> dict[str, Any]:
    """A run with active reds: the one furthest along reads, the others counted."""
    it = min(its, key=lambda x: RED_STATE_RANK.get(x["state"], 5))
    text, short = red_view(store, it)
    more = f" (+{len(its) - 1} more red{'s' if len(its) > 2 else ''} in this run)" if len(its) > 1 else ""
    blk = red_block_of(it, store) if it["state"] == "blocked-external" else None
    ask = red_ask(it, blk)
    return {"state": it["state"], "text": text + more, "short": short, "cause": it["data"].get("cause"),
            "until": (blk or {}).get("until"), "ask": ask["q"] if ask else None, "ask_how": ask["how"] if ask else None}


def red_view_of_units(store: Any, units: Any) -> dict[Any, dict[str, Any]]:
    """{(workflow, run id): {state, text, short, cause}} for ci-why's red rows, from the red items their keys name."""
    out, active, settled = {}, red_active(store), None
    groups: dict[Any, list[Any]] = {}
    for key, ob in red_obs_of_units(units).items():
        groups.setdefault((ob["workflow"], ob["run"].get("databaseId")), []).append(key)
    for k, keys in groups.items():
        its = [active[x] for x in keys if x in active]
        if its:
            out[k] = _active_view(store, its)
            continue
        settled = red_settled_runs(store) if settled is None else settled
        out[k] = _settled_view(settled, keys, k)
    return out


def red_lines(st, indent="  "):
    return [f"{indent}fixing: {st['text']}"] if st else []


def repo_dispatch_set(store, o_r, off, by):
    cur = repo_dispatch_off_all(store)
    if off:
        cur[o_r] = {"at": ts(), "by": by}
    else:
        cur.pop(o_r, None)
    store.meta_set(REPO_DISPATCH_OFF, json.dumps(cur, sort_keys=True))


# RED STATUS READS THE SYNC'S SNAPSHOT (2026-11-22: `red status` timed out at 8 s under load -- each call read
# ci-runner's store over the tunnel and ran git per red): the laptop's sync renders it each cycle into red-status.json
# (write_red_status_snapshot, bounded in the background), and a call prints that while it is RED_STATUS_FRESH s old,
# saying its age; --verbose, --live or a stale snapshot reads the store.
RED_STATUS_FRESH = 180


def red_status_snapshot_path() -> str:
    return state_root("red-status.json")


