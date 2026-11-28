# agentctl, part `redwaits`: a red waiting outside itself: on another red's commit (its wait chain, and the step that
# resolves it), or on a spent quota (the reset time, its probe, the waits and their redispatch)
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from typing import Any
import re
import time
from agentctl.ciruns import CiRuns
from agentctl.statemachines import is_terminal
from agentctl.timefmt import iso_at, parse_time
from agentctl.redrecord import lease_is_live, red_active_of, red_lease, red_of_key, red_what
from agentctl.onmain import MainContainment
from agentctl.red import red_move
from agentctl.redopen import red_offer
from agentctl.ids import dt
from agentctl.core import SHORT, attempt
from agentctl.leases import block_lease, lease_meta, red_block_of
from agentctl.classify import QUOTA_LOG
from agentctl.inboxentry import inbox_write
from agentctl.workflowdispatch import red_verify_dispatch
from agentctl.stuckruns import run_jobs
from agentctl.redblock import BLOCK_MAX, BLOCK_TTL



# A RED WHOSE FIX IS ANOTHER RED'S COMMIT (2026-11-16, Order independence #78, e948278d144a): its fix was the main-tests
# fixer's 715ab5a1a for red e1be6bd9fd46, not on main yet. The owner answered "hold until 715ab5a1a is on main"; the
# fixer held as decided, and its `NOT LANDED: holding ...` read as THE DECIDED ACTION FAILED -- a fresh WAITING ON YOU,
# twice (ev10700, ev10709). Now that hold is a state: fix-pending with a fix of status `waits-on` {sha, red}, from the
# verdict `WAITS ON <sha> (red <key>)` -- or a NOT LANDED / GAVE UP naming `<sha> (... red <key>)` of another red --
# and the reconciler resolves it itself (red_waits_on_step): the sha, or its rebased copy, or the other red's own
# landed fix on main -> a landed fix verified against the next run; the sha set aside by the other red, or that red
# closed without it on main -> offered again. It waits as long as the other red is open: that red has its own holder.
RED_WAITS_ON = "waits-on"


# A WAIT CYCLE HAS NO EXIT (review 2026-11-16): red A waiting on red B's commit while B waits (directly or through others)
# on A's -- neither lands first, each one's holder is "the red it waits on" (red_fix_held), so W2 never fires. A wait
# that would close a cycle is refused at the verdict (red_waits_on_of), and one found standing is offered again.
RED_WAIT_CHAIN_MAX = 20


def _waits_on(store: Any, key: Any) -> Any:
    """The red key that red `key` waits on, None when it is gone, closed or waits on nothing."""
    o = red_of_key(store, key)
    f = ((o or {}).get("data") or {}).get("fix") or {}
    if not o or is_terminal("red", o["state"]) or f.get("status") != RED_WAITS_ON:
        return None
    return f.get("red")


def red_wait_cycle(store, own_key, other):
    """The keys from `other` back to own_key when other waits (through its waits-on chain) on own_key, else None."""
    chain, cur = [], other
    while cur and len(chain) < RED_WAIT_CHAIN_MAX:
        chain.append(cur)
        cur = _waits_on(store, cur)
        if cur == own_key:
            return chain
        if cur in chain:
            return None  # a cycle among others: theirs to break
    return None


def _waits_on_landed(store: Any, it: dict[str, Any], sha: str, other: Any, ofix: dict[str, Any], repo: Any, t: float) -> Any:
    """The fix it waited on (or the other red's own landed one) is on main: verified against the next run; else None."""
    d, main = it["data"], MainContainment(repo)
    for cand in [sha] + ([ofix["sha"]] if ofix.get("status") == "landed" and ofix.get("sha") else []):
        on = main.carrying(cand)
        if on and main.contains(on) is True:
            line = f"{red_what(d)}: red {other}'s fix {cand[:10]} is on main ({on[:10]}): verifying"
            red_move(store, it, "fix-pending", line, data={"fix": {"entry": None, "status": "landed", "sha": on,
                                                                    "at": t, "verify_since": t, "waited_on": other}})
            return [line]
    return None


def _waits_on_aside(od: dict[str, Any], sha: str) -> bool:
    """The other red set the fix `sha` aside (refuted, or unverified), or a rebased copy of it."""
    aside = set(od.get("refuted") or []) | set(od.get("unverified") or [])
    return any(x.startswith(sha) or sha.startswith(x) for x in aside if x and sha)


def _waits_on_ended(store: Any, d: dict[str, Any], o: Any, other: Any, sha: str) -> str | None:
    """Why the wait is over (the other red is gone, waits on this one in turn, set the fix aside, or closed without it on
    main), None while it stands."""
    cycle = red_wait_cycle(store, d.get("key"), other)
    if not o:
        return f"red {other}, whose fix {sha[:10]} it waited on, is gone"
    if cycle:
        return (f"red {other} waits on this red in turn ({' -> '.join(cycle + [str(d.get('key'))])}), a wait cycle neither "
                f"side can end: land the fix here, or say why not")
    if _waits_on_aside(o.get("data") or {}, sha):
        return f"red {other} set aside the fix {sha[:10]} it waited on"
    return _waits_on_closed(o, other, sha)


def _waits_on_closed(o: dict[str, Any], other: Any, sha: str) -> str | None:
    if is_terminal("red", o["state"]):
        return f"red {other} closed ({o['state']}) without the fix {sha[:10]} it waited on reaching main"
    return None


def red_waits_on_step(store: Any, it: dict[str, Any], f: dict[str, Any], repo: Any, t: float) -> Any:
    """A fix-pending red waiting on another red's commit (RED_WAITS_ON, above): resolved, or left waiting."""
    d = it["data"]
    sha, other = f.get("sha") or "", f.get("red")
    o = red_of_key(store, other)
    landed = _waits_on_landed(store, it, sha, other, ((o or {}).get("data") or {}).get("fix") or {}, repo, t)
    if landed:
        return landed
    why = _waits_on_ended(store, d, o, other, sha)
    if why:
        h = {"at": iso_at(t), "holder": f"waits-on:{other}", "outcome": f"{why}: offered again"}
        red_offer(store, it, f"{red_what(d)}: {h['outcome']}", data={"fix": None, "history": (d.get("history") or []) + [h]})
        return [f"re-offered {red_what(d)} ({why[:80]})"]
    if t >= it["deadline"]:  # still waiting while the other red is open: its own holder works on it; said quietly
        red_move(store, it, "fix-pending", f"{red_what(d)}: waits on red {other}'s fix {sha[:10]} (not on main yet)",
                 severity="info")
    return []


QUOTA_RESET_HOUR_UTC = 0


def quota_service(line):
    m = re.search(r"::error[^:]*::(\w+) refuses the key", line or "") or re.search(r"\b(OMDb|TMDB|Decodo)\b", line or "", re.I)
    return m.group(1) if m else "the service"


def next_quota_reset(after):
    """The first quota window reset (QUOTA_RESET_HOUR_UTC, daily) after unix time `after`."""
    d = dt.datetime.fromtimestamp(after, dt.timezone.utc)
    r = d.replace(hour=QUOTA_RESET_HOUR_UTC, minute=0, second=0, microsecond=0)
    if r <= d:
        r += dt.timedelta(days=1)
    return r.timestamp()


def created_after(run: Any, since: float) -> bool:
    """True when the run was created after unix time `since` (a run whose time does not read is not)."""
    return attempt(lambda: parse_time(run["createdAt"]).timestamp() > since, False,
                   catch=(KeyError, ValueError, TypeError))


def quota_run_verdict(o_r: str, a: Any, r: Any, *, runs: CiRuns) -> tuple[bool, str] | None:
    """(cleared, why) of one run since the quota was spent: past its first job (a['job'], the preflight) or green clears
    it, that job failing again keeps it; None when the run says nothing yet."""
    label = f"{a['workflow']} #{r.get('number')}"
    if r.get("conclusion") == "success":
        return True, f"{label} green"
    first = next((j for j in run_jobs(o_r, r, runs=runs) or [] if j.get("name") == a.get("job")), None)
    conclusion = first.get("conclusion") if first else None
    if conclusion == "success":
        return True, f"{label}: {a.get('job')} passed (the key is accepted again)"
    if conclusion in ("failure", "timed_out"):
        return False, f"{label}: {a.get('job')} failed again (still spent)"
    return None


def probe_quota_reset(a: Any, *, runs: CiRuns) -> tuple[bool, str]:
    """The key accepted again: a run of a['workflow'] on main created after a['since'] got past its first job
    (a['job'], the preflight) -- or the whole run is green."""
    o_r = a.get("repo") or SHORT["movies"]
    # listing: runs created since the block; none listed keeps the wait (a probe that cannot tell never clears it)
    listed = runs.main_runs(o_r, a["workflow"], 10)
    since = float(a.get("since") or 0)
    for r in listed or []:
        verdict = created_after(r, since) and quota_run_verdict(o_r, a, r, runs=runs)
        if verdict:
            return verdict
    return False, (f"no run of {a['workflow']} since the quota was spent; the next window opens "
                   f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(next_quota_reset(since)))}")


def waits_on_quota(store: Any, it: Any, t: float) -> bool:
    """True when the red's line says a quota is spent and nobody holds it (no fixer lease, no block)."""
    d = it["data"]
    return bool(QUOTA_LOG.search(str(d.get("line") or ""))) and d.get("kind") != "test" \
        and not lease_is_live(store.get(red_lease(d["key"])), t) and not red_block_of(it, store)


def open_quota_wait(store: Any, o_r: str, d: Any, svc: str, lname: str, t: float) -> None:
    """The WAIT lease of a service's spent quota, opened for the red `d`, and the owner-visible line that says it waits."""
    job = d.get("job") if d.get("job") else "preflight"
    probe = {"kind": "quota_reset", "args": {"repo": o_r, "workflow": d["workflow"], "since": t, "job": job}}
    until = f"{svc}'s quota resets and {d['workflow']}'s {job} passes"
    store.acquire(lname, "wait:quota", BLOCK_TTL, BLOCK_MAX, progress=f"spent@{int(t)}",
                  meta={"kind": "block", "wait_cause": "quota", "cause": f"quota-{svc.lower()}", "probe": probe,
                        "until": until, "keys": [d["key"]], "probed_at": t, "redispatch": d["workflow"],
                        "text": f"WAITING: {svc}'s quota for the run's key is spent; it resets on its own "
                                f"(auto-clears when {until})"})
    line = f"RED WAITING: {red_what(d)}: {svc}'s quota is spent -- an external wait, not the owner's; " \
           f"{d['workflow']} is dispatched again after the reset"
    inbox_write("red", line, [line], [], 0)


def put_on_quota_wait(store: Any, o_r: str, it: Any, t: float) -> str:
    """Puts one red on the WAIT lease of its service's quota (joining it when live, else opening it)."""
    d = it["data"]
    svc = quota_service(d.get("line"))
    lname = block_lease(cause=f"quota-{svc.lower()}")
    row = store.get(lname)
    if lease_is_live(row, t):
        m = lease_meta(row)
        store.set_meta(lname, row["epoch"], row["secret"], {"keys": sorted(set(m.get("keys") or []) | {d["key"]})})
    else:
        open_quota_wait(store, o_r, d, svc, lname, t)
    cur = store.item("red", it["id"])
    store.update_item("red", it["id"], cur["version"], {"block": lname})
    return f"{red_what(d)}: waits on {svc}'s quota ({lname})"


def redispatch_quota_waits(store: Any, o_r: str, repo: Any, t: float) -> list[str]:
    """Each live quota wait past its window's reset with no run since: its workflow dispatched once per window."""
    out = []
    for row in store.list("block/cause/quota-"):
        m = lease_meta(row)
        if not lease_is_live(row, t) or not m.get("redispatch"):
            continue
        since = float((m.get("probe") or {}).get("args", {}).get("since") or t)
        last = float(m.get("dispatched_at") or since)
        if t >= next_quota_reset(last) + 300:
            msg = red_verify_dispatch(o_r, repo, m["redispatch"], "quota-reset")
            out.append(f"{m['redispatch']} after {m.get('cause')}'s reset (re-checks the key): {msg}")
            store.set_meta(row["name"], row["epoch"], row["secret"], {"dispatched_at": t})
    return out


def red_quota_waits(store, o_r, repo, t):
    """Each open red whose line says a quota is spent, held by nobody (no fixer lease, no block): put on the WAIT lease
    of its service; each wait past its window's reset with no run since: its workflow dispatched once per window."""
    out = [put_on_quota_wait(store, o_r, it, t) for it in red_active_of(store, o_r).values()
           if waits_on_quota(store, it, t)]
    return out + redispatch_quota_waits(store, o_r, repo, t)
