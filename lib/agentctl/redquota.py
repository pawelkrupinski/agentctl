# agentctl, part `redquota`: spent quotas as external waits: the reset time, its probe, the waits and their redispatch
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import re
import time
from agentctl.timefmt import dt, parse_time
from agentctl.core import SHORT
from agentctl.classify import QUOTA_LOG
from agentctl.forge import RUN_FIELDS, gh_json
from agentctl.inboxentry import inbox_write
from agentctl.redrecord import lease_is_live, red_lease, red_what
from agentctl.leases import block_lease, lease_meta, red_block_of
from agentctl.red import red_active_of
from agentctl.rundispatch import red_verify_dispatch
from agentctl.stuckruns import run_jobs
from agentctl.redblock import BLOCK_MAX, BLOCK_TTL


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


def probe_quota_reset(a):
    """The key accepted again: a run of a['workflow'] on main created after a['since'] got past its first job
    (a['job'], the preflight) -- or the whole run is green."""
    o_r = a.get("repo") or SHORT["movies"]
    # listing: runs created since the block; none listed keeps the wait (a probe that cannot tell never clears it)
    runs = gh_json(o_r, "run", "list", "--workflow", a["workflow"], "--branch", "main", "--json", RUN_FIELDS, "-L", "10")
    since = float(a.get("since") or 0)
    for r in runs or []:
        try:
            if parse_time(r["createdAt"]).timestamp() <= since:
                continue
        except (KeyError, ValueError, TypeError):
            continue
        if r.get("conclusion") == "success":
            return True, f"{a['workflow']} #{r.get('number')} green"
        jobs = run_jobs(o_r, r) or []
        first = next((j for j in jobs if j.get("name") == a.get("job")), None)
        if first and first.get("conclusion") == "success":
            return True, f"{a['workflow']} #{r.get('number')}: {a.get('job')} passed (the key is accepted again)"
        if first and first.get("conclusion") in ("failure", "timed_out"):
            return False, f"{a['workflow']} #{r.get('number')}: {a.get('job')} failed again (still spent)"
    return False, (f"no run of {a['workflow']} since the quota was spent; the next window opens "
                   f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(next_quota_reset(since)))}")


def red_quota_waits(store, o_r, repo, t):
    """Each open red whose line says a quota is spent, held by nobody (no fixer lease, no block): put on the WAIT lease
    of its service; each wait past its window's reset with no run since: its workflow dispatched once per window."""
    out = []
    for it in red_active_of(store, o_r).values():
        d = it["data"]
        if not QUOTA_LOG.search(str(d.get("line") or "")) or d.get("kind") == "test":
            continue
        if lease_is_live(store.get(red_lease(d["key"])), t) or red_block_of(it, store):
            continue
        svc = quota_service(d.get("line"))
        lname = block_lease(cause=f"quota-{svc.lower()}")
        row = store.get(lname)
        if lease_is_live(row, t):
            m = lease_meta(row)
            store.set_meta(lname, row["epoch"], row["secret"], {"keys": sorted(set(m.get("keys") or []) | {d["key"]})})
        else:
            probe = {"kind": "quota_reset", "args": {"repo": o_r, "workflow": d["workflow"], "since": t, "job": d.get("job")
                                                     if d.get("job") else "preflight"}}
            until = f"{svc}'s quota resets and {d['workflow']}'s {probe['args']['job']} passes"
            store.acquire(lname, "wait:quota", BLOCK_TTL, BLOCK_MAX, progress=f"spent@{int(t)}",
                          meta={"kind": "block", "wait_cause": "quota", "cause": f"quota-{svc.lower()}", "probe": probe,
                                "until": until, "keys": [d["key"]], "probed_at": t, "redispatch": d["workflow"],
                                "text": f"WAITING: {svc}'s quota for the run's key is spent; it resets on its own "
                                        f"(auto-clears when {until})"})
            line = f"RED WAITING: {red_what(d)}: {svc}'s quota is spent -- an external wait, not the owner's; " \
                   f"{d['workflow']} is dispatched again after the reset"
            inbox_write("red", line, [line], [], 0)
        cur = store.item("red", it["id"])
        store.update_item("red", it["id"], cur["version"], {"block": lname})
        out.append(f"{red_what(d)}: waits on {svc}'s quota ({lname})")
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
