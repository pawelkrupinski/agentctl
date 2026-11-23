# agentctl, part `stats`: the pipeline's statistics, one structured record per finished thing, in VictoriaLogs: the pass that sends them, the gate runs walked, the store queried back
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import json
import os
import re
import sys
from agentctl.core import env_num, err, git, host_role
from agentctl.timefmt import iso_at
from agentctl.waitingdir import waiting_entries, wlog
from agentctl.gating import gate_flows
from agentctl.store import open_store
from agentctl.statsrecords import (
    CHANGE_EXITS, _num, condition_record, gate_run_record, landing_records, owner_decision_record, red_record,
    state_spans, watch_record)


STATS_EVERY = 60            # s between passes ($AGENTCTL_STATS_EVERY)
STATS_PER_PASS = 150        # records a pass sends at most (the backfill spreads over passes)
STATS_SETTLE = 15 * 60      # s after an exit before its record is built: the collector exports a run <= 5 min after it ends
STATS_PASS_BOUND = 40       # s a pass works at most (a tick step)
STATS_GATE_LOOKBACK = 3 * 86400   # s of gate runs each pass looks at (markers dedupe)
STATS_GATE_HISTORY = 120 * 86400  # s of gate runs the first passes walk back (the collector backfilled 90 days)
STATS_URL_SERVER = "http://victoria-logs-ci.service.consul:9429"
# BY WORKFLOW FILE, NOT RUN NAME (2026-11-17: 8 Identity takes records in all, the last on 11-14): a run's `workflow`
# is its run-name -- every convergence-dispatched Identity takes run reads "Identity takes (convergence run <id>)" --
# so the gate runs are matched by their file, the record named by the gate workflow's own name. THE GATE is movies'
# gating checks registry (ciwhy.py gate_flows), read when a pass runs.
STATS_GATE_WALK = "stats_gate_from2"  # meta: the history walk's cursor (2: walked again for the runs the name missed)
STATS_GATE_REPO = "pawelkrupinski/movies"
CI_FIELDS = ("kind, repo, workflow, run_id, run_number, run_attempt, sha, event, run_conclusion, job, conclusion, "
             "queue_s, duration_s, start, end, wall_s, path_queue_s, path_work_s, critical_path, runner_min")


def stats_url():
    """Where the records go: $AGENTCTL_STATS_URL ('off' disables), else victoria-logs-ci on ci-runner; None elsewhere."""
    u = os.environ.get("AGENTCTL_STATS_URL")
    if u:
        return None if u == "off" else u.rstrip("/")
    return STATS_URL_SERVER if host_role() == "server" else None


def stats_post(url, records):
    """POST records to VictoriaLogs' JSON-line insert API (historical _time is kept). Raises on a failure."""
    import urllib.request
    body = "\n".join(json.dumps(r, sort_keys=True) for r in records).encode()
    q = "_stream_fields=source,record&_time_field=_time&_msg_field=_msg"
    req = urllib.request.Request(f"{url}/insert/jsonline?{q}", data=body, method="POST",
                                 headers={"Content-Type": "application/stream+json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        r.read()


def stats_query(url, query, start=None, end=None, limit=5000):
    """A LogsQL query's rows (dicts); raises on a failure."""
    import urllib.parse, urllib.request
    form = {"query": query, "limit": str(limit)}
    if start is not None:
        form["start"] = iso_at(start)
    if end is not None:
        form["end"] = iso_at(end)
    req = urllib.request.Request(f"{url}/select/logsql/query", data=urllib.parse.urlencode(form).encode(), method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return [json.loads(line) for line in r.read().decode().splitlines() if line.strip()]


# ---------------------------------------------------------------- the pass (the reconciler's `stats` step)
def item_events(st, typ, iid):
    """The item's events, in order."""
    if hasattr(st, "item_events"):
        return st.item_events(typ, iid)
    return [e for e in st.events(after=0, limit=10 ** 7) if e.get("item_type") == typ and e.get("item_id") == iid]


def change_paths(repo, entry, d):
    """The change's files, from the clone (its base..tip); None when its commits are not here."""
    base = entry.get("checked_base") or entry.get("base") or d.get("base")
    tip = d.get("landed_sha") or entry.get("tip") or d.get("tip") or entry.get("head") or d.get("head")
    if not (repo and base and tip and os.path.isdir(repo)):
        return None
    r = git(repo, "diff", "--name-only", base, tip, check=False)
    return [p for p in r.stdout.splitlines() if p] if getattr(r, "returncode", 1) == 0 else None


def check_runs_of(url, change_id, around):
    """The change's precommit-check runs (source:ci): [{run_id, path_queue_s, path_work_s, wall_s, start, jobs}]."""
    if not re.fullmatch(r"[A-Za-z0-9_-]+", change_id):  # a regexp without metacharacters: no escaping in LogsQL
        return []
    rows = stats_query(url, f'source:ci workflow_file:="precommit-check.yml" (kind:run OR kind:job) '
                            f'branch:~"{change_id}$" | fields {CI_FIELDS}', start=around[0] - 3600, end=around[1] + 3 * 3600)
    runs = {}
    for r in rows:
        x = runs.setdefault(str(r.get("run_id")), {"run_id": r.get("run_id"), "jobs": []})
        if r.get("kind") == "run":
            x.update({k: r.get(k) for k in ("path_queue_s", "path_work_s", "wall_s", "start", "run_conclusion")})
            x["start"] = _num(r.get("start"))
        elif r.get("kind") == "job":
            x["jobs"].append(r)
    return list(runs.values())


def stats_sent(st, name, iid):
    return st.meta_get(f"stats:{name}:{iid}") is not None


def stats_pass(clock, repo=None, d=None, store=None, url=None, out=None):
    """One pass: the records due (an exit older than STATS_SETTLE, not sent yet), sent; returns how many."""
    st = store or open_store()
    url = url or stats_url()
    if not url:
        return 0
    t = clock.time()
    since = _num(st.meta_get("stats_since"))
    if since is None:
        since = t
        st.meta_set("stats_since", since)
    deadline = clock.monotonic() + env_num("AGENTCTL_STATS_PASS_BOUND", STATS_PASS_BOUND)
    entries = {}
    if d:
        for e in waiting_entries(d, open_only=False):
            entries[e["id"]] = e
    due = []  # (marker name, id, builder)
    settle = env_num("AGENTCTL_STATS_SETTLE", STATS_SETTLE)
    changes = st.items("change")
    for it in changes:
        if it["state"] in CHANGE_EXITS and it["entered_at"] <= t - settle and not stats_sent(st, "landing", it["id"]):
            due.append(("landing", it["id"], it))
    for it in st.items("watch", ["ok", "red", "unknown"]):
        if it["entered_at"] <= t - 60 and not stats_sent(st, "landing_watch", it["id"]):
            due.append(("landing_watch", it["id"], it))
    for it in st.items("red", ["verified", "cleared"]):
        if it["entered_at"] <= t - 60 and not stats_sent(st, "red", it["id"]):
            due.append(("red", it["id"], it))
    for it in st.items("condition", ["closed"]):
        if it["entered_at"] <= t - 60 and not stats_sent(st, "condition", it["id"]):
            due.append(("condition", it["id"], it))
    due.sort(key=lambda x: x[2]["entered_at"])
    waits = None
    by_sha = {(c["data"] or {}).get("landed_sha"): c["id"] for c in changes if (c["data"] or {}).get("landed_sha")}
    sent = 0
    cap = int(env_num("AGENTCTL_STATS_PER_PASS", STATS_PER_PASS))
    for name, iid, it in due:
        if sent >= cap or clock.monotonic() > deadline:
            break
        if name == "landing":
            evs = item_events(st, "change", iid)
            e = entries.get(iid) or {}
            start = _num((it["data"] or {}).get("submitted_at")) or it["entered_at"]
            try:
                runs = check_runs_of(url, iid, (start, it["entered_at"]))
            except Exception as ex:  # the collector's rows unread: wait for the next pass, never a record without them
                err(f"stats: the check runs of {iid} could not be read ({str(ex)[:160]})")
                break
            recs = landing_records(it, evs, e, runs, change_paths(repo, e, it["data"] or {}), since)
        elif name == "landing_watch":
            recs = [watch_record(it, by_sha.get(iid), since)]
        elif name == "red":
            if waits is None:
                waits = change_waits(st, changes, t)
            evs = item_events(st, "red", iid)
            recs = [red_record(it, evs, waits, since)]
            od = owner_decision_record(it, evs, since)
            recs += [od] if od else []
        else:
            recs = [condition_record(it, item_events(st, "condition", iid), since)]
        try:
            stats_post(url, recs)
        except Exception as ex:
            err(f"stats: VictoriaLogs at {url} refused the records ({str(ex)[:160]}): the next pass sends them")
            break
        st.meta_set(f"stats:{name}:{iid}", t)
        stats_echo(recs, out)
        sent += len(recs)
    if clock.monotonic() <= deadline and sent < cap:
        sent += gate_runs_pass(st, url, t, since, cap - sent, out)
    return sent


def stats_echo(recs, out=None):
    """A live record on stdout (the reconciler's journal): `agentctl.<name> {json}`; a backfilled one is not."""
    for r in recs:
        if not r.get("backfill"):
            print(f"{r['record']} {json.dumps(r, sort_keys=True)}", file=out or sys.stdout, flush=True)


def change_waits(st, changes, now):
    """[(change id, entered waiting, left)] over every change item's events."""
    out = []
    for c in changes:
        evs = item_events(st, "change", c["id"])
        if not any(e.get("to_state") == "waiting" for e in evs):
            continue
        for s, a, b, _e in state_spans(evs, _num((c["data"] or {}).get("submitted_at")) or c["entered_at"]):
            if s == "waiting":
                out.append((c["id"], a, b if b is not None else now))
    return out


def gate_runs_pass(st, url, t, since, cap, out=None):
    """The gate runs the collector has exported (source:ci kind:run on main) and no record names yet: one record each.
    The first passes walk back STATS_GATE_HISTORY in 7-day windows (meta stats_gate_from); then the last 3 days."""
    frm = _num(st.meta_get(STATS_GATE_WALK))
    if frm is None:
        frm = t - STATS_GATE_HISTORY
    walking = frm < t - STATS_GATE_LOOKBACK
    lo, hi = (frm, min(frm + 7 * 86400, t)) if walking else (t - STATS_GATE_LOOKBACK, t)
    try:
        gate_files = {f: w for w, f in gate_flows(STATS_GATE_REPO)}
    except (Exception, SystemExit) as ex:
        err(f"stats: the gate's workflows could not be read ({str(ex)[:160]})")
        return 0
    files = ", ".join(f'"{f}"' for f in gate_files)  # exact: a phrase filter `workflow:"Main"` also matched
    # bitcashier/core's "Main Pipeline" (2026-11-17, the first backfill)
    try:
        rows = stats_query(url, f'source:ci kind:run branch:="main" repo:="{STATS_GATE_REPO}" workflow_file:in({files}) '
                                f'| fields {CI_FIELDS}, workflow_file', start=lo, end=hi)
    except Exception as ex:
        err(f"stats: the gate runs could not be read ({str(ex)[:160]})")
        return 0
    n = 0
    rows = [{**r, "run_name": r.get("workflow"), "workflow": gate_files[r["workflow_file"]]} for r in rows
            if r.get("repo") == STATS_GATE_REPO and r.get("workflow_file") in gate_files]
    for run in sorted(rows, key=lambda r: _num(r.get("end")) or 0):
        rid = f"{run.get('run_id')}.{run.get('run_attempt') or 1}"
        if stats_sent(st, "gate_run", rid):
            continue
        if n >= cap:
            return n
        try:
            jobs = stats_query(url, f'source:ci kind:job run_id:"{run.get("run_id")}" | fields {CI_FIELDS}', start=lo - 86400, end=hi + 86400)
            jobs = [j for j in jobs if str(j.get("run_attempt") or 1) == str(run.get("run_attempt") or 1)]
            rec = gate_run_record(run, jobs, since)
            stats_post(url, [rec])
        except Exception as ex:
            err(f"stats: gate run {rid} not recorded ({str(ex)[:160]}): the next pass tries again")
            return n
        st.meta_set(f"stats:gate_run:{rid}", t)
        stats_echo([rec], out)
        n += 1
    if walking:
        st.meta_set(STATS_GATE_WALK, hi)
    return n


def maybe_stats_pass(repo, d, clock):
    """The reconciler's step: a pass every STATS_EVERY s when there is a store to write to."""
    if not stats_url():
        return 0
    st = open_store(clock.time)
    if clock.time() - float(st.meta_get("stats_at", 0) or 0) < env_num("AGENTCTL_STATS_EVERY", STATS_EVERY):
        return 0
    st.meta_set("stats_at", clock.time())
    n = stats_pass(clock, repo, d, st)
    if n:
        wlog(d, f"tick: stats: {n} record(s) sent to VictoriaLogs")
    return n
