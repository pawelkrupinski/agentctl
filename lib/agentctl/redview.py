# agentctl, part `redview`: how a red reads (ci-why, the status line, `red status`), and dispatch off per repo.
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import json
import time
from typing import Any
from agentctl.core import HOME, env_num, state_root
from agentctl.timefmt import hhmmz, ts
from agentctl.statefile import read_record
from agentctl.leases import lease_meta, red_block_of
from agentctl.redrecord import RED_RECURRING, red_active, red_lease, red_names
from agentctl.redfixread import FIX_OPEN
from agentctl.redfixes import red_refuted
from agentctl.cistate import snapshot_gate_reds
from agentctl.owners import owner_record_path
from agentctl.redask import red_ask, red_decision
from agentctl.red import RED_FIX_GIVEUP
from agentctl.redmove import red_settled_runs
from agentctl.branches import run_names, short_flow
from agentctl.redseen import red_obs_of_units
from agentctl.ask import red_decided_view, trim_words
from agentctl.redpending import RED_WAITS_ON
from agentctl.blame import blame_status
from agentctl.reddispatch import RED_HELD_IDLE, REPO_DISPATCH_OFF, red_owned_progress_at, repo_dispatch_off_all


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
    if lease_meta(row).get("kind") == "owned":
        return max(float(row.get("acquired_at") or t), red_owned_progress_at(row, d)) + float(env_num(
            "AGENTCTL_RED_HELD_IDLE", RED_HELD_IDLE))
    return float(row.get("expires_at") or t)


def red_view_state(store, it, t=None):
    """(text, short) of one red's state."""
    t = t if t is not None else store.clock()
    d, st = it["data"], it["state"]
    at = time.strftime("%H:%M", time.localtime(it["deadline"]))
    rec = f" -- recurring: {len(red_refuted(d))} fixes refuted" if len(red_refuted(d)) >= RED_RECURRING else ""
    if st == "red":
        return "observed: offered at the next tick", "offering"
    if st == "offered":
        off = store.offer_of(f"red:{it['id']}") or {}
        tier = d.get("tier") or "fresh"
        reason = off.get("reason") or "awaiting a taker"
        late = " (ESCALATED)" if it.get("level") else ""
        return (f"offered{late}: {tier} tier, {reason}; escalates {at} (`agentctl red claim {d['key']} --worktree .`){rec}",
                f"offered{late}: {tier}, {reason}")
    if st == "leased":
        row = store.get(red_lease(d["key"])) or {}
        m = lease_meta(row)
        who = m.get("who") or str(row.get("holder") or "?").split(":")[-1]
        wt = m.get("worktree")
        if not wt and m.get("kind") == "owned" and m.get("family"):  # an owner's red: the owner's ONE worktree
            wt = (read_record(owner_record_path(m["family"])) or {}).get("worktree")
        wt = str(wt or "?").replace(HOME, "~")
        exp = time.strftime("%H:%M", time.localtime(red_lease_lapses(row, d, t)))
        prim = str(m.get("primary") or "").split("/")[-1]  # a sibling's primary red; an owner's red has none
        fam = f" [family {m['family']}{', with ' + prim if prim else ''}]" if m.get("family") else ""
        return (f"{who} ({wt}, leased{fam}; lapses {exp} without progress)" + (f" -- {m['note']}" if m.get("note") else "")
                + rec,
                f"{who.replace('movies-wt-', '').replace('red-fixer-', 'fixer ')} leased")
    if st == "fix-pending" and (d.get("fix") or {}).get("unproven"):  # A FIX WITH NO REPLAY KEEPS ITS FIXER (redmove.py)
        f = d["fix"]
        return (f"fix-unproven: {f.get('entry') or str(f.get('sha'))[:9]} ({f.get('status')}) carries no replay; "
                f"{d.get('holder') or 'its holder'} keeps it until a {d.get('workflow')} run decides it{rec}",
                f"fix-unproven {str(f.get('sha') or f.get('entry') or '')[:7]}")
    if st == "fix-pending":
        f = d.get("fix") or {}
        if f.get("status") == "landed" and str(f.get("verify") or "").startswith("dispatched") \
                and t < float(it["deadline"]):  # VERIFY PROMPTLY BY DEFAULT (red.py): its dispatched run decides it,
            at = str(f.get("verify_dispatched") or "")[11:16]  # never the cron slot (2026-11-21, Order independence
            return (f"fix landed in {str(f.get('sha'))[:9]}, verify run dispatched {at}Z ({d.get('workflow')}; "  # #99)
                    f"verified by {hhmmz(f.get('verify_by') or it['deadline'])}){rec}",
                    f"fix {str(f.get('sha'))[:7]} verify run dispatched {at}Z")
        if f.get("status") == "landed" and f.get("next_run_at") and t < float(it["deadline"]):  # red_verify_plan
            return (f"fix landed in {str(f.get('sha'))[:9]}, awaiting next scheduled run at {hhmmz(f['next_run_at'])} "
                    f"({d.get('workflow')}; verified by {hhmmz(f.get('verify_by') or it['deadline'])}){rec}",
                    f"fix {str(f.get('sha'))[:7]} awaiting next scheduled run at {hhmmz(f['next_run_at'])}")
        if f.get("status") == "landed":
            return (f"fix landed in {str(f.get('sha'))[:9]}, awaiting a {d.get('workflow')} run containing it{rec}",
                    f"fix {str(f.get('sha'))[:7]} awaiting its run")
        if f.get("status") == RED_WAITS_ON:
            return (f"waits on red {f.get('red')}'s fix {str(f.get('sha'))[:10]}: verified against the next "
                    f"{d.get('workflow')} run once main has it{rec}", f"waits on red {f.get('red')}'s fix {str(f.get('sha'))[:10]}")
        if f.get("status") == "verify":
            return (f"verifying: the next decided {d.get('workflow')} run says whether it is fixed ({f.get('verify') or 'awaiting it'}); "
                    f"offered again if none by {time.strftime('%H:%M', time.localtime(it['entered_at'] + env_num('AGENTCTL_RED_FIX_GIVEUP', RED_FIX_GIVEUP)))}{rec}",
                    "verifying")
        if f.get("status") in FIX_OPEN:  # queued, not landed: nothing has tested it yet -- it holds the red
            # THE FIX OF THE GATE'S OWN RED lands on red (2026-10-29 19:20: convergence #2550's fix, Fixes-Red: 2527,
            # read "blocked by gate convergence #2550" -- blocked by the red it fixes)
            own = gate_own_red_label(d)
            if own:
                return (f"fix {f.get('entry')} landing on red (Fixes-Red), {f.get('status')}: it fixes the gate's own "
                        f"red ({own}); verified once it lands{rec}",
                        f"landing on red (Fixes-Red), {f.get('status')}")
            by = gate_red_now_label()
            return (f"fix {f.get('entry')} waiting to land ({f.get('status')}" + (f"; blocked by gate {by}" if by else "")
                    + f"): verified once it lands (Fixes-Red){rec}",
                    "fix waiting to land" + (f" (blocked by gate {by})" if by else ""))
        return f"fix {f.get('entry')} {f.get('status')} (Fixes-Red){rec}", f"fix {f.get('status')}"
    if st == "blocked-external":
        b = red_block_of(it, store) or {}
        ask = red_ask(it, b)
        if ask:  # WAITING ON YOU (ask.py): the question itself, never a bare cause
            more = "" if str(b.get("text") or "").startswith("WAITING ON YOU") else f" -- {b.get('text')}"
            return (f"WAITING ON YOU: {ask['q']} -- {ask['how']}" + (f"; auto-clears when {ask['until']}" if ask.get("until")
                                                                     else "") + more,
                    f"WAITING ON YOU: {trim_words(ask['q'])} -- {ask['how']}")
        if red_decision(d):  # answered: its block is lifted, offered again at the next tick
            return "the owner's answer is in: offered again at the next tick", "re-dispatching"
        if not b:  # its block lapsed (the tick offers it again): never a stale BLOCKED ON OWNER on the status line
            return "no block holds it now: offered again at the next tick", "re-offering"
        if (b.get("meta") or {}).get("harness"):  # agentctl's own fault (redcrash.py red_harness_issue): its maintainer's
            return b.get("text"), f"HARNESS FAULT: agentctl issue {(b.get('meta') or {}).get('issue')}"
        return b.get("text") or "BLOCKED ON OWNER", (f"BLOCKED: {d.get('cause')} down" if d.get("cause") else "BLOCKED ON OWNER")
    return st, st


def red_view_of_units(store, units):
    """{(workflow, run id): {state, text, short, cause}} for ci-why's red rows, from the red items their keys name."""
    out, active, settled = {}, red_active(store), None
    groups = {}
    for key, ob in red_obs_of_units(units).items():
        groups.setdefault((ob["workflow"], ob["run"].get("databaseId")), []).append(key)
    for k, keys in groups.items():
        its = [active[x] for x in keys if x in active]
        if not its:
            settled = red_settled_runs(store) if settled is None else settled
            done = [settled.get((x, str(k[1]))) for x in keys]
            if all(done):  # its newest run for good, proven fixed by its callers' runs: never offered again
                proof = done[0]["data"].get("proof") or "proven fixed by its callers' runs"
                out[k] = {"state": "verified", "text": f"verified: {proof}", "short": "verified", "cause": None}
            else:
                out[k] = {"state": "red", "text": "observed: offered at the next tick", "short": "offering", "cause": None}
            continue
        rank = {"leased": 0, "fix-pending": 1, "blocked-external": 2, "offered": 3, "red": 4}
        it = min(its, key=lambda x: rank.get(x["state"], 5))
        text, short = red_view(store, it)
        more = f" (+{len(its) - 1} more red{'s' if len(its) > 2 else ''} in this run)" if len(its) > 1 else ""
        blk = red_block_of(it, store) if it["state"] == "blocked-external" else None
        ask = red_ask(it, blk)
        out[k] = {"state": it["state"], "text": text + more, "short": short, "cause": it["data"].get("cause"),
                  "until": (blk or {}).get("until"), "ask": ask["q"] if ask else None, "ask_how": ask["how"] if ask else None}
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


