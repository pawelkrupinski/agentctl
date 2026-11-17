# agentctl, part `stats`: the pipeline's statistics, one structured record per finished thing, in VictoriaLogs.
# Runs inside agentctl's one namespace, after the parts before it in lib/agentctl/__init__.py's PARTS (it uses their names).

# ---------------------------------------------------------------- landing stats (the owner, 2026-11-17)
# "keep landing stats -- timings, time between landing and landed in victoria logs, modules touched etc -- for easy
# analysis", widened the same day to "escalations, gate builds red etc". The reconciler (ci-runner, the server role)
# writes ONE flat JSON record per finished thing into victoria-logs-ci (the CI metrics store: 400 days, the same store
# and Grafana datasource the CI dashboard reads), `source:agentctl`, `record:<name>`:
#   agentctl.landing         a change's exit (landed, rejected, superseded, cancelled): its phases, rounds, files, runs
#   agentctl.landing_round   each check round of a change: full or verify, why, its precommit-check run's queue/work
#   agentctl.landing_watch   a landed sha's post-push watch verdict (ok / red / unknown) and how long it took
#   agentctl.red             a red episode (one red item: open -> verified / cleared): fixers, fix, waits, owner block
#   agentctl.condition       a condition spell (an INVARIANT W1/W2/..., NETWORK, STALLED, ...): opened -> closed
#   agentctl.owner_decision  a question put to the owner (a red's BLOCKED ON OWNER / WAITING ON YOU) and its answer
#   agentctl.gate_run        a completed gate run (Main, Identity model convergence, Identity takes) on main
# THE RECORDS ARE DERIVED, never kept: each builder below reads only the store (items, their events) and, for the run
# figures, the CI collector's own `source:ci` events in the same store (ci-metrics-collector: one per run and job of
# every workflow). So the history is rebuilt by the same code: a record whose exit is older than the first stats pass
# (meta `stats_since`) carries `backfill: true` -- the backfill IS the first passes, through the same path. Each record
# is sent once (meta `stats:<record>:<id>`); a pass sends at most STATS_PER_PASS, every STATS_EVERY s. A live record is
# also printed on the reconciler's stdout (its journal), one `agentctl.<name> {json}` line. Field names are stable and
# documented in AGENTCTL.md, "stats"; `schema` says which version of them a record has.
STATS_SCHEMA = 1
STATS_EVERY = 60            # s between passes ($AGENTCTL_STATS_EVERY)
STATS_PER_PASS = 150        # records a pass sends at most (the backfill spreads over passes)
STATS_SETTLE = 15 * 60      # s after an exit before its record is built: the collector exports a run <= 5 min after it ends
STATS_PASS_BOUND = 40       # s a pass works at most (a tick step)
STATS_GATE_LOOKBACK = 3 * 86400   # s of gate runs each pass looks at (markers dedupe)
STATS_GATE_HISTORY = 120 * 86400  # s of gate runs the first passes walk back (the collector backfilled 90 days)
STATS_URL_SERVER = "http://victoria-logs-ci.service.consul:9429"
STATS_GATE_WORKFLOWS = ("Main", "Identity model convergence", "Identity takes")
CI_FIELDS = ("kind, repo, workflow, run_id, run_number, run_attempt, sha, event, run_conclusion, job, conclusion, "
             "queue_s, duration_s, start, end, wall_s, path_queue_s, path_work_s, critical_path, runner_min")
CHANGE_EXITS = ("landed", "rejected", "superseded", "cancelled")
RECHECK_CLASSES = (  # (class, pattern) of the text after "since its checks, " / of a re-queue's reason, first match wins
    ("verify", re.compile(r"no file overlap")),
    ("selection", re.compile(r", and its .* adds")),
    ("hook", re.compile(r"they change (scripts/hooks/|scripts/ci/lint|\.github/)")),
    ("build", re.compile(r"they change (build\.sbt|project/)")),
    ("build", re.compile(r"they change ")),
    ("module-overlap", re.compile(r"sbt modules overlap")),
    ("overlap", re.compile(r"both touch")),
    ("deletion", re.compile(r"(they|it) deletes? ")),
    ("plan", re.compile(r"plan could not be read")),
)
REJECT_CLASSES = (
    ("precheck", re.compile(r"fast precheck refused")),
    ("checks-failed", re.compile(r"pre-push checks failed")),
    ("conflict", re.compile(r"no longer applies")),
    ("too-slow", re.compile(r"ran past their time cap")),
    ("blame", re.compile(r"blame|reverted", re.I)),
)


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


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _r(x):
    return None if x is None else round(float(x), 1)


def classify(text, classes, default):
    for name, rx in classes:
        if rx.search(text or ""):
            return name
    return default


def recheck_class(text):
    """A round's reason class from the re-queue event that started it."""
    text = text or ""
    if "QUEUED AGAIN" in text or ": infra (" in text:
        m = re.search(r"infra \(([^():;]+)", text)
        return "infra:" + re.sub(r"[^a-z]+", "-", (m.group(1) if m else "other").lower()).strip("-")[:40]
    if "waiting -> queued" in text:
        return "from-waiting"
    tail = text.split("since its checks, ", 1)[1] if "since its checks, " in text else text
    return classify(tail, RECHECK_CLASSES, "other")


def path_modules(paths):
    """(top-level dirs, sbt modules: the dirs with a src/ tree) of changed paths."""
    tops = sorted({p.split("/", 1)[0] if "/" in p else "(root)" for p in paths})
    sbt = sorted({p.split("/", 1)[0] for p in paths if re.match(r"[^/]+/src/", p)})
    return tops, sbt


def job_area(name):
    """A precommit-check job's area: `check (identity 2/3)` -> identity, `heavy rows (itrest)` -> heavy:itrest."""
    m = re.match(r"\s*(check|heavy rows)\s*\(([^)]*)\)", name or "")
    if not m:
        return None
    word = m.group(2).split()[0] if m.group(2).split() else m.group(2)
    return f"heavy:{word}" if m.group(1) == "heavy rows" else word


def state_spans(events, start_at, first_state="queued"):
    """[(state, entered, left)] of an item from its events in order: the item starts in first_state at start_at."""
    spans, state, since = [], first_state, start_at
    for ev in events:
        if ev.get("from_state") is None or ev.get("to_state") is None:
            continue
        if ev["to_state"] == state and ev["from_state"] == state:  # a self-transition (an escalation): no new span
            continue
        spans.append((state, since, ev["at"], ev))
        state, since = ev["to_state"], ev["at"]
    spans.append((state, since, None, None))
    return spans


def _sum(spans, state, until=None):
    return sum(((left if left is not None else until or entered) - entered) for s, entered, left, _e in spans
               if s == state)


def landing_records(item, events, entry=None, runs=(), paths=None, since=None):
    """(the agentctl.landing record, its agentctl.landing_round records) of a change item that exited. events: its own,
    in order; entry: its queue entry (or None); runs: its precommit-check runs as {run_id, kind run row, jobs: [rows]}
    (from source:ci); paths: its changed files (None: unknown); since: the first stats pass (older exits: backfill)."""
    d = item["data"] or {}
    entry = entry or {}
    evs = [e for e in events if e.get("item_type", "change") == "change"]
    start = _num(d.get("submitted_at")) or (evs[0]["at"] if evs else item["entered_at"])
    exit_ev = next((e for e in reversed(evs) if e.get("to_state") in CHANGE_EXITS), None)
    end = exit_ev["at"] if exit_ev else item["entered_at"]
    spans = state_spans(evs, start)
    rounds = []
    ordered_runs = sorted(runs, key=lambda r: r.get("start") or 0)
    for state, entered, left, ev in spans:
        if state != "checking":
            continue
        n = len(rounds) + 1
        reason_ev = next((e for e in reversed(evs) if e["at"] <= entered and e.get("to_state") == "queued"
                          and e.get("from_state") != "queued"), None)
        result_ev = ev
        result = (result_ev or {}).get("to_state") or "unfinished"
        text = (result_ev or {}).get("text") or ""
        mode = "verify" if re.search(r": verified ", text) else ("full" if result == "checked" else None)
        reason = "first" if n == 1 or reason_ev is None else recheck_class(reason_ev.get("text"))
        if mode is None:
            mode = "verify" if reason == "verify" else "full"
        if result == "queued":
            result = "requeued"
        queued_before = [s for s in spans if s[0] == "queued" and s[2] is not None and s[2] <= entered]
        rounds.append({"round": n, "mode": mode, "reason_class": reason,
                       "reason": ((reason_ev or {}).get("text") or "")[:300] if n > 1 else "",
                       "started": entered, "ended": left, "check_s": None if left is None else left - entered,
                       "queued_before_s": (entered - queued_before[-1][1]) if queued_before else None,
                       "result": result})
    for run in ordered_runs:  # a run belongs to the round it started in (an adopted run: the round before it)
        at = run.get("start") or 0
        rd = next((x for x in reversed(rounds) if x["started"] - 60 <= at and (x["ended"] is None or at <= x["ended"])),
                  None)
        if rd is None or rd.get("run_id"):
            continue
        rd.update(run_id=run.get("run_id"), run_queue_s=_num(run.get("path_queue_s")),
                  run_work_s=_num(run.get("path_work_s")), run_wall_s=_num(run.get("wall_s")))
    first_check = next((s[1] for s in spans if s[0] == "checking"), None)
    pushing = [s for s in spans if s[0] == "pushing" and s[2] is not None]
    lander = d.get("lander") or entry.get("lander") or {}
    outcome = item["state"] if item["state"] in CHANGE_EXITS else (exit_ev or {}).get("to_state")
    why = str(d.get("why") or (exit_ev or {}).get("text") or "")
    reason_class = {"landed": "landed", "superseded": "superseded"}.get(outcome)
    if outcome == "rejected":
        reason_class = classify(why, REJECT_CLASSES, "other")
    elif outcome == "cancelled":
        reason_class = "precheck" if (d.get("precheck") or {}).get("outcome") == "refused" or \
            "fast precheck" in why else "cancelled"
    jobs = [j for r in runs for j in r.get("jobs") or []]
    areas = sorted({a for a in (job_area(j.get("job")) for j in jobs) if a})
    tops, sbt = path_modules(paths or [])
    fixes = sorted(d.get("fixes") or entry.get("fixes") or [])
    full = sum(1 for r in rounds if r["mode"] == "full")
    pre = d.get("precheck") or {}
    rec = {"record": "agentctl.landing", "change_id": item["id"], "repo": lander.get("repo") or "pawelkrupinski/movies",
           "lander_agent": lander.get("agent"), "lander_session": lander.get("session") or d.get("session"),
           "lander_host": lander.get("host"), "worktree": d.get("worktree") or entry.get("worktree"),
           "branch": lander.get("branch") or entry.get("branch"), "head_sha": d.get("head") or entry.get("head"),
           "base_sha": entry.get("base"), "checked_base": entry.get("checked_base") or d.get("base"),
           "landed_sha": d.get("landed_sha") if outcome == "landed" else None, "outcome": outcome,
           "reason_class": reason_class, "reason": why.splitlines()[0][:300] if why else "",
           "submitted_at": iso_at(start), "exited_at": iso_at(end), "total_s": end - start,
           "queue_wait_s": (first_check - start) if first_check else None,
           "queued_s": _sum(spans, "queued", end), "checking_s": _sum(spans, "checking", end),
           "checked_wait_s": _sum(spans, "checked", end), "pushing_s": _sum(spans, "pushing", end),
           "push_to_landed_s": (pushing[-1][2] - pushing[-1][1]) if pushing and outcome == "landed" else None,
           "waiting_s": _sum(spans, "waiting", end), "rounds": len(rounds), "full_rounds": full,
           "verify_rounds": len(rounds) - full, "full_rechecks": max(0, full - 1),
           "infra_requeues": sum(1 for r in rounds if r["reason_class"].startswith("infra:")),
           "recheck_reasons": ",".join(r["reason_class"] for r in rounds[1:]),
           "red_fix": bool(fixes), "fixes": ",".join(fixes),
           "commits_n": len(d.get("commit_keys") or entry.get("commits") or []) or None,
           "files_n": None if paths is None else len(paths), "modules": ",".join(tops), "sbt_modules": ",".join(sbt),
           "areas": ",".join(areas), "check_runs": ",".join(str(r.get("run_id")) for r in ordered_runs),
           "check_runner_wait_s": _r(sum(_num(r.get("path_queue_s")) or 0 for r in ordered_runs)) if runs else None,
           "check_run_s": _r(sum(_num(r.get("path_work_s")) or 0 for r in ordered_runs)) if runs else None,
           "check_jobs_n": len(jobs) or None,
           "check_job_queue_max_s": max((_num(j.get("queue_s")) or 0 for j in jobs), default=None),
           "check_job_queue_sum_s": _r(sum(_num(j.get("queue_s")) or 0 for j in jobs)) if jobs else None,
           "check_job_run_sum_s": _r(sum(_num(j.get("duration_s")) or 0 for j in jobs)) if jobs else None,
           "precheck_s": _num(pre.get("duration_s")), "precheck_outcome": pre.get("outcome"),
           "precheck_step": pre.get("step"), "batch": d.get("batch") or entry.get("batch"),
           "superseded_by": d.get("superseded_by") or entry.get("superseded_by")}
    rec["_msg"] = (f"agentctl.landing {item['id']}: {outcome} ({reason_class}) after {mins_of(rec['total_s'])}, "
                   f"{len(rounds)} round(s), queue {mins_of(rec['queue_wait_s'])}")
    rec["_time"] = iso_at(end)
    out = [stats_common(rec, end, since)]
    for rd in rounds:
        r = {"record": "agentctl.landing_round", "change_id": item["id"], "repo": rec["repo"], "outcome": outcome,
             "lander_agent": rec["lander_agent"], **rd, "started": iso_at(rd["started"]),
             "ended": iso_at(rd["ended"]) if rd["ended"] else None}
        r["_msg"] = (f"agentctl.landing_round {item['id']} #{rd['round']}: {rd['mode']} ({rd['reason_class']}), "
                     f"{mins_of(rd['check_s'])} -> {rd['result']}")
        r["_time"] = iso_at(rd["started"])
        out.append(stats_common(r, end, since))
    return out


def mins_of(s):
    return "?" if s is None else f"{s / 60:.0f} min"


def stats_common(rec, at, since):
    """The fields every record has, None values dropped, durations rounded."""
    out = {"source": "agentctl", "schema": STATS_SCHEMA, "backfill": bool(since is not None and at < since)}
    for k, v in rec.items():
        if v is None or v == "":
            continue
        out[k] = round(v, 1) if isinstance(v, float) else v
    return out


def watch_record(item, change_id=None, since=None):
    """agentctl.landing_watch of a decided watch item (keyed by the landed sha)."""
    d = item["data"] or {}
    pushed, at = _num(d.get("pushed_at")), item["entered_at"]
    rec = {"record": "agentctl.landing_watch", "landed_sha": item["id"], "change_id": change_id,
           "repo": d.get("o_r"), "verdict": item["state"], "verdict_line": str(d.get("verdict") or "")[:300],
           "pushed_at": iso_at(pushed) if pushed else None, "watch_s": (at - pushed) if pushed else None,
           "_time": iso_at(at)}
    rec["_msg"] = f"agentctl.landing_watch {item['id'][:10]}: {item['state']} after {mins_of(rec['watch_s'])}"
    return stats_common(rec, at, since)


RED_VERDICT_CLASS = {"test": "test-failure", "infra": "infra-shaped", "stuck-run": "infra-shaped",
                     "unclear": "no-verdict"}
HOLDER_RX = re.compile(r"leased by (\S+)")


def red_record(item, events, waits=(), since=None):
    """agentctl.red of a closed red item (an episode). waits: [(change id, waiting entered, left)] of every change, for
    how many waited on a gate red and how long (their waiting time inside the episode)."""
    d = item["data"] or {}
    evs = [e for e in events if e.get("item_type", "red") == "red"]
    opened = _num(d.get("opened_at")) or (evs[0]["at"] if evs else item["entered_at"])
    close_ev = next((e for e in reversed(evs) if e.get("to_state") in ("verified", "cleared")), None)
    closed = close_ev["at"] if close_ev else item["entered_at"]
    spans = state_spans(evs, opened, "red")
    leased = [e for e in evs if e.get("to_state") == "leased" and e.get("from_state") != "leased"]
    holders = [m.group(1) for m in (HOLDER_RX.search(e.get("text") or "") for e in leased) if m]
    fix = d.get("fix") or {}
    fix_at = _num(fix.get("at")) if fix.get("status") in ("landed", "verified") or fix.get("sha") else None
    blocked = _sum(spans, "blocked-external", closed)
    w = [(cid, max(a, opened), min(b if b is not None else closed, closed)) for cid, a, b in waits] if d.get("gate") else []
    w = [(cid, a, b) for cid, a, b in w if b > a]
    dec = d.get("decision") or {}
    first_block = next((e["at"] for e in evs if e.get("to_state") == "blocked-external"), None)
    runs = sorted((int(x) if str(x).isdigit() else x for x in d.get("runs") or []), key=lambda x: (len(str(x)), str(x)))
    last = d.get("last_run") or {}
    rec = {"record": "agentctl.red", "red_id": item["id"], "key": d.get("key"), "repo": d.get("repo") or "pawelkrupinski/movies",
           "workflow": d.get("workflow"), "gate": bool(d.get("gate")), "red_kind": d.get("red_kind"),
           "verdict_class": RED_VERDICT_CLASS.get(d.get("kind"), d.get("kind")), "family": d.get("family"),
           "jobs": ",".join(d.get("jobs") or [])[:600], "signature": str(d.get("signature") or d.get("line") or "")[:200],
           "first_run": runs[0] if runs else None, "last_run": last.get("number") or (runs[-1] if runs else None),
           "runs_n": len(runs), "opened_at": iso_at(opened), "closed_at": iso_at(closed),
           "duration_s": closed - opened, "outcome": item["state"], "closed_by": (close_ev or {}).get("text", "")[:200],
           "fixers_n": len(leased), "fixer_restarts": max(0, len(leased) - 1), "fixer_holders": ",".join(sorted(set(holders))),
           "first_fixer_at": iso_at(leased[0]["at"]) if leased else None,
           "time_to_fixer_s": (leased[0]["at"] - opened) if leased else None,
           "leased_s": _sum(spans, "leased", closed), "offered_s": _sum(spans, "offered", closed),
           "fix_pending_s": _sum(spans, "fix-pending", closed),
           "fix_sha": fix.get("sha"), "fix_entry": fix.get("entry"), "fix_status": fix.get("status"),
           "fix_landed_at": iso_at(fix_at) if fix_at else None,
           "red_to_fix_landed_s": (fix_at - opened) if fix_at and fix_at >= opened else None,
           "red_to_green_s": (closed - opened) if item["state"] == "verified" else None,
           "waited_changes_n": len({c for c, _a, _b in w}), "waited_changes_s": sum(b - a for _c, a, b in w),
           "owner_blocked": bool(first_block or dec), "blocked_s": blocked,
           "decision_wait_s": (_num(dec.get("at")) - first_block) if dec.get("at") and first_block else None,
           "escalations_n": sum(1 for e in evs if e.get("severity") == "escalate"),
           "notifications_n": sum(1 for e in evs if e.get("severity") == "escalate" and e.get("notified_at")),
           "attempts": d.get("attempts"), "_time": iso_at(closed)}
    rec["_msg"] = (f"agentctl.red {item['id']}: {d.get('workflow')} {rec['verdict_class']} {item['state']} after "
                   f"{mins_of(rec['duration_s'])}, {len(leased)} fixer(s)")
    return stats_common(rec, closed, since)


def owner_decision_record(item, events, since=None):
    """agentctl.owner_decision of a closed red that asked the owner (its BLOCKED ON OWNER spell), else None."""
    d = item["data"] or {}
    evs = [e for e in events if e.get("item_type", "red") == "red"]
    ask, dec = d.get("ask") or {}, d.get("decision") or {}
    blocks = [e["at"] for e in evs if e.get("to_state") == "blocked-external" and e.get("from_state") != "blocked-external"]
    asked = (blocks[0] if blocks else None) or _num(ask.get("first_at")) or _num(ask.get("since"))
    if not asked:
        return None
    close_ev = next((e for e in reversed(evs) if e.get("to_state") in ("verified", "cleared")), None)
    closed = close_ev["at"] if close_ev else item["entered_at"]
    answered = _num(dec.get("at"))
    path = "decided" if answered else ("stale" if closed - asked >= 7 * 86400 else "auto-resolved")
    end = answered or closed
    rec = {"record": "agentctl.owner_decision", "red_id": item["id"], "belongs_to": f"red:{item['id']}",
           "workflow": (d.get("workflow")), "gate": bool(d.get("gate")), "repo": d.get("repo") or "pawelkrupinski/movies",
           "question": str(dec.get("question") or ask.get("q") or "")[:400], "answer": str(dec.get("answer") or "")[:400],
           "asked_at": iso_at(asked), "answered_at": iso_at(answered) if answered else None,
           "wait_s": end - asked, "answer_path": path, "red_closed_at": iso_at(closed), "red_outcome": item["state"],
           "reminders_n": sum(1 for e in evs if e.get("kind") == "red.remind"), "asks_n": max(1, len(blocks)),
           "decision_failed": str(ask.get("q") or "").startswith("you decided"), "_time": iso_at(end)}
    rec["_msg"] = f"agentctl.owner_decision red {item['id']}: {path} after {mins_of(rec['wait_s'])}"
    return stats_common(rec, closed, since)


def condition_record(item, events, since=None):
    """agentctl.condition of a closed condition spell."""
    d = item["data"] or {}
    evs = [e for e in events if e.get("item_type", "condition") == "condition"]
    opened = _num(d.get("opened_at")) or (evs[0]["at"] if evs else item["entered_at"])
    close_ev = next((e for e in reversed(evs) if e.get("to_state") == "closed"), None)
    closed = close_ev["at"] if close_ev else item["entered_at"]
    esc = [e for e in evs if e.get("severity") == "escalate"]
    key = d.get("key") or item["id"]
    inv = re.match(r"invariant-(\w+)", key) or re.match(r"INVARIANT (W\w*):", str(d.get("text") or ""))
    rec = {"record": "agentctl.condition", "condition_id": item["id"], "key": key, "kind": d.get("kind"),
           "invariant": inv.group(1) if inv else None, "host": d.get("host"), "opened_at": iso_at(opened),
           "escalated_at": iso_at(esc[0]["at"]) if esc else None, "closed_at": iso_at(closed),
           "duration_s": closed - opened, "violation": str(d.get("text") or "")[:400],
           "until": str(d.get("until") or "")[:200], "closed_by": str(d.get("closed_by") or "")[:200],
           "manual_close": str(d.get("closed_by") or "").startswith(("closed by hand", "INVALID:")),
           "escalations_n": len(esc), "notifications_n": sum(1 for e in esc if e.get("notified_at")),
           "_time": iso_at(closed)}
    rec["_msg"] = f"agentctl.condition {key}: {d.get('kind')} open {mins_of(rec['duration_s'])}, {len(esc)} escalation(s)"
    return stats_common(rec, closed, since)


def gate_run_record(run, jobs, since=None):
    """agentctl.gate_run of a completed gate run, from the collector's source:ci kind:run row and its kind:job rows."""
    jobs = [j for j in jobs if _num(j.get("duration_s")) is not None]
    end = _num(run.get("end")) or 0
    hops = [h.strip() for h in str(run.get("critical_path") or "").split("→") if h.strip()]
    crit = next((j for j in jobs if hops and j.get("job") == hops[-1]), None) or \
        max(jobs, key=lambda j: _num(j.get("end")) or 0, default=None)
    slow = max(jobs, key=lambda j: _num(j.get("duration_s")) or 0, default=None)
    rec = {"record": "agentctl.gate_run", "workflow": run.get("workflow"), "repo": run.get("repo"),
           "run_id": run.get("run_id"), "run_number": run.get("run_number"), "run_attempt": run.get("run_attempt"),
           "sha": run.get("sha"), "event": run.get("event"), "conclusion": run.get("run_conclusion"),
           "started_at": iso_at(_num(run.get("start"))) if _num(run.get("start")) else None,
           "ended_at": iso_at(end) if end else None, "duration_s": _num(run.get("wall_s")),
           "path_queue_s": _num(run.get("path_queue_s")), "path_work_s": _num(run.get("path_work_s")),
           "critical_path": str(run.get("critical_path") or "")[:600],
           "critical_job": (crit or {}).get("job"), "critical_job_s": _num((crit or {}).get("duration_s")),
           "critical_job_queue_s": _num((crit or {}).get("queue_s")),
           "slowest_job": (slow or {}).get("job"), "slowest_job_s": _num((slow or {}).get("duration_s")),
           "jobs_n": len(jobs), "runner_min": _num(run.get("runner_min")),
           "runner_wait_s": _r(sum(_num(j.get("queue_s")) or 0 for j in jobs)),
           "run_s": _r(sum(_num(j.get("duration_s")) or 0 for j in jobs)),
           "job_queue_max_s": max((_num(j.get("queue_s")) or 0 for j in jobs), default=None),
           "failed_jobs": ",".join(sorted(j.get("job") for j in jobs if j.get("conclusion") == "failure"))[:600],
           "_time": iso_at(end or time.time())}
    rec["_msg"] = (f"agentctl.gate_run {run.get('workflow')} #{run.get('run_number')}: {run.get('run_conclusion')} "
                   f"in {mins_of(rec['duration_s'])}, critical {rec['critical_job']}")
    return stats_common(rec, end, since)


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
    rows = stats_query(url, f'source:ci workflow_file:"precommit-check.yml" (kind:run OR kind:job) '
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


def stats_pass(repo=None, d=None, store=None, url=None, now_=None, out=None):
    """One pass: the records due (an exit older than STATS_SETTLE, not sent yet), sent; returns how many."""
    st = store or Store()
    url = url or stats_url()
    if not url:
        return 0
    t = time.time() if now_ is None else now_
    since = _num(st.meta_get("stats_since"))
    if since is None:
        since = t
        st.meta_set("stats_since", since)
    deadline = time.monotonic() + env_num("AGENTCTL_STATS_PASS_BOUND", STATS_PASS_BOUND)
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
        if sent >= cap or time.monotonic() > deadline:
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
                waits = change_waits(st, changes)
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
    if time.monotonic() <= deadline and sent < cap:
        sent += gate_runs_pass(st, url, t, since, cap - sent, out)
    return sent


def stats_echo(recs, out=None):
    """A live record on stdout (the reconciler's journal): `agentctl.<name> {json}`; a backfilled one is not."""
    for r in recs:
        if not r.get("backfill"):
            print(f"{r['record']} {json.dumps(r, sort_keys=True)}", file=out or sys.stdout, flush=True)


def change_waits(st, changes):
    """[(change id, entered waiting, left)] over every change item's events."""
    out = []
    for c in changes:
        evs = item_events(st, "change", c["id"])
        if not any(e.get("to_state") == "waiting" for e in evs):
            continue
        for s, a, b, _e in state_spans(evs, _num((c["data"] or {}).get("submitted_at")) or c["entered_at"]):
            if s == "waiting":
                out.append((c["id"], a, b if b is not None else time.time()))
    return out


def gate_runs_pass(st, url, t, since, cap, out=None):
    """The gate runs the collector has exported (source:ci kind:run on main) and no record names yet: one record each.
    The first passes walk back STATS_GATE_HISTORY in 7-day windows (meta stats_gate_from); then the last 3 days."""
    frm = _num(st.meta_get("stats_gate_from"))
    if frm is None:
        frm = t - STATS_GATE_HISTORY
    walking = frm < t - STATS_GATE_LOOKBACK
    lo, hi = (frm, min(frm + 7 * 86400, t)) if walking else (t - STATS_GATE_LOOKBACK, t)
    flows = " OR ".join(f'workflow:"{w}"' for w in STATS_GATE_WORKFLOWS)
    try:
        rows = stats_query(url, f'source:ci kind:run branch:main ({flows}) | fields {CI_FIELDS}', start=lo, end=hi)
    except Exception as ex:
        err(f"stats: the gate runs could not be read ({str(ex)[:160]})")
        return 0
    n = 0
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
        st.meta_set("stats_gate_from", hi)
    return n


def maybe_stats_pass(repo, d):
    """The reconciler's step: a pass every STATS_EVERY s when there is a store to write to."""
    if not stats_url():
        return 0
    st = Store()
    if time.time() - float(st.meta_get("stats_at", 0) or 0) < env_num("AGENTCTL_STATS_EVERY", STATS_EVERY):
        return 0
    st.meta_set("stats_at", time.time())
    n = stats_pass(repo, d, st)
    if n:
        wlog(d, f"tick: stats: {n} record(s) sent to VictoriaLogs")
    return n
