# agentctl, part `statsrecords`: the statistics' records: one structured record per finished change, watch, red, owner decision, condition and gate run
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import re
from typing import Any, Iterable, Optional, Sequence, cast
from agentctl.timefmt import iso_at
from agentctl.statemachines import is_terminal, terminal_states

Rec = dict[str, Any]  # an item, an event, a record: JSON-shaped
Span = tuple[str, float, Optional[float], Optional[Rec]]  # (state, entered, left or None, the event that left it)
Wait = tuple[str, float, Optional[float]]  # (change id, waiting entered, left or None)


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


def _num(v: Any) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _r(x: Any) -> Optional[float]:
    return None if x is None else round(float(x), 1)


def classify(text: Optional[str], classes: Sequence[tuple[str, re.Pattern[str]]], default: str) -> str:
    for name, rx in classes:
        if rx.search(text or ""):
            return name
    return default


def recheck_class(text: Optional[str]) -> str:
    """A round's reason class from the re-queue event that started it."""
    text = text or ""
    if "QUEUED AGAIN" in text or ": infra (" in text:
        m = re.search(r"infra \(([^():;]+)", text)
        return "infra:" + re.sub(r"[^a-z]+", "-", (m.group(1) if m else "other").lower()).strip("-")[:40]
    if "waiting -> queued" in text:
        return "from-waiting"
    tail = text.split("since its checks, ", 1)[1] if "since its checks, " in text else text
    return classify(tail, RECHECK_CLASSES, "other")


def path_modules(paths: Iterable[str]) -> tuple[list[str], list[str]]:
    """(top-level dirs, sbt modules: the dirs with a src/ tree) of changed paths."""
    tops = sorted({p.split("/", 1)[0] if "/" in p else "(root)" for p in paths})
    sbt = sorted({p.split("/", 1)[0] for p in paths if re.match(r"[^/]+/src/", p)})
    return tops, sbt


def job_area(name: Optional[str]) -> Optional[str]:
    """A precommit-check job's area: `check (identity 2/3)` -> identity, `heavy rows (itrest)` -> heavy:itrest."""
    m = re.match(r"\s*(check|heavy rows)\s*\(([^)]*)\)", name or "")
    if not m:
        return None
    word = m.group(2).split()[0] if m.group(2).split() else m.group(2)
    return f"heavy:{word}" if m.group(1) == "heavy rows" else word


def state_spans(events: Iterable[Rec], start_at: float, first_state: str = "queued") -> list[Span]:
    """[(state, entered, left)] of an item from its events in order: the item starts in first_state at start_at."""
    spans: list[Span] = []
    state, since = first_state, start_at
    for ev in events:
        if ev.get("from_state") is None or ev.get("to_state") is None:
            continue
        if ev["to_state"] == state and ev["from_state"] == state:  # a self-transition (an escalation): no new span
            continue
        spans.append((state, since, ev["at"], ev))
        state, since = ev["to_state"], ev["at"]
    spans.append((state, since, None, None))
    return spans


def _sum(spans: Iterable[Span], state: str, until: Optional[float] = None) -> float:
    return sum(((left if left is not None else until or entered) - entered) for s, entered, left, _e in spans
               if s == state)


def _either(a: Rec, b: Rec, key: str) -> Any:
    """a's key, else b's."""
    return a.get(key) or b.get(key)


def _text(v: Any, n: int) -> str:
    return str(v or "")[:n]


def _repo(d: Rec) -> str:
    return d.get("repo") or "pawelkrupinski/movies"


def _iso(at: Optional[float]) -> Optional[str]:
    return iso_at(at) if at else None


def _total(rows: Iterable[Rec], key: str) -> Optional[float]:
    """The rows' key summed (an unreadable value counts 0), rounded."""
    return _r(sum(_num(r.get(key)) or 0 for r in rows))


def _peak(rows: Iterable[Rec], key: str) -> Optional[float]:
    return max((_num(r.get(key)) or 0 for r in rows), default=None)


def item_span(item: Rec, events: Iterable[Rec], item_type: str, start_key: str,
              end_states: Sequence[str]) -> tuple[list[Rec], float, Optional[Rec], float]:
    """(the item's own events, its start, the event that ended it or None, its end): it starts at data[start_key], else
    at its first event; it ends at the newest event entering one of end_states, else when it entered its state."""
    d = item["data"] or {}
    evs = [e for e in events if e.get("item_type", item_type) == item_type]
    start = _num(d.get(start_key)) or (evs[0]["at"] if evs else item["entered_at"])
    end_ev = next((e for e in reversed(evs) if e.get("to_state") in end_states), None)
    return evs, start, end_ev, end_ev["at"] if end_ev else item["entered_at"]


def landing_records(item: Rec, events: Iterable[Rec], entry: Optional[Rec] = None, runs: Iterable[Rec] = (),
                    paths: Optional[list[str]] = None, since: Optional[float] = None) -> list[Rec]:
    """(the agentctl.landing record, its agentctl.landing_round records) of a change item that exited. events: its own,
    in order; entry: its queue entry (or None); runs: its precommit-check runs as {run_id, kind run row, jobs: [rows]}
    (from source:ci); paths: its changed files (None: unknown); since: the first stats pass (older exits: backfill)."""
    d, entry = item["data"] or {}, entry or {}
    evs, start, exit_ev, end = item_span(item, events, "change", "submitted_at", terminal_states("change"))
    spans = state_spans(evs, start)
    ordered = sorted(runs, key=lambda r: r.get("start") or 0)
    rounds = check_rounds(spans, evs, ordered)
    outcome, why = change_exit(item, d, exit_ev)
    reason_class = exit_class(outcome, why, d)
    rec = {"record": "agentctl.landing", "change_id": item["id"], **_landing_who(d, entry),
           "landed_sha": d.get("landed_sha") if outcome == "landed" else None, "outcome": outcome,
           "reason_class": reason_class, "reason": why.splitlines()[0][:300] if why else "",
           **_landing_phases(spans, start, end, outcome), **_rounds_summary(rounds, d), **_fix_kind(d, entry),
           **_landing_size(d, entry, paths), **_landing_checks(ordered, [j for r in runs for j in r.get("jobs") or []]),
           **_precheck_and_batch(d, entry)}
    rec["_msg"] = (f"agentctl.landing {item['id']}: {outcome} ({reason_class}) after {mins_of(rec['total_s'])}, "
                   f"{len(rounds)} round(s), queue {mins_of(rec['queue_wait_s'])}")
    rec["_time"] = iso_at(end)
    return [stats_common(rec, end, since)] + [stats_common(_round_record(item, rec, rd), end, since) for rd in rounds]


def check_rounds(spans: list[Span], evs: list[Rec], runs: Iterable[Rec]) -> list[Rec]:
    """The change's check rounds, one a `checking` span, each with the run that started in it."""
    rounds: list[Rec] = []
    for state, entered, left, ev in spans:
        if state == "checking":
            rounds.append(check_round(len(rounds) + 1, entered, left, ev, spans, evs))
    for run in runs:  # a run belongs to the round it started in (an adopted run: the round before it)
        rd = _round_of(rounds, run.get("start") or 0)
        if rd is not None and not rd.get("run_id"):
            rd.update(run_id=run.get("run_id"), run_queue_s=_num(run.get("path_queue_s")),
                      run_work_s=_num(run.get("path_work_s")), run_wall_s=_num(run.get("wall_s")))
    return rounds


def _round_of(rounds: list[Rec], at: float) -> Optional[Rec]:
    """The newest round a run started at `at` belongs to (up to a minute before it began), or None."""
    return next((x for x in reversed(rounds) if x["started"] - 60 <= at and (x["ended"] is None or at <= x["ended"])),
                None)


def check_round(n: int, entered: float, left: Optional[float], result_ev: Optional[Rec], spans: list[Span],
                evs: list[Rec]) -> Rec:
    """Round n, the checking span entered..left that result_ev ended: why it ran (the re-queue before it), how, result."""
    reason_ev = _requeue_before(evs, entered)
    result_ev = result_ev or {}
    result = result_ev.get("to_state") or "unfinished"
    reason = "first" if n == 1 or reason_ev is None else recheck_class(reason_ev.get("text"))
    return {"round": n, "mode": round_mode(result_ev.get("text") or "", result, reason), "reason_class": reason,
            "reason": _text((reason_ev or {}).get("text"), 300) if n > 1 else "",
            "started": entered, "ended": left, "check_s": None if left is None else left - entered,
            "queued_before_s": _queued_before(spans, entered), "result": "requeued" if result == "queued" else result}


def _requeue_before(evs: list[Rec], at: float) -> Optional[Rec]:
    """The newest event before `at` that queued the change again (from another state), or None."""
    return next((e for e in reversed(evs) if e["at"] <= at and e.get("to_state") == "queued"
                 and e.get("from_state") != "queued"), None)


def _queued_before(spans: list[Span], at: float) -> Optional[float]:
    """The time from the start of the newest queued span ended by `at` to `at`, or None."""
    queued = [s for s in spans if s[0] == "queued" and s[2] is not None and s[2] <= at]
    return (at - queued[-1][1]) if queued else None


def round_mode(text: str, result: str, reason: str) -> str:
    """verify (the combined tree's checks) or full."""
    if re.search(r": verified ", text):
        return "verify"
    if result == "checked":
        return "full"
    return "verify" if reason == "verify" else "full"


def change_exit(item: Rec, d: Rec, exit_ev: Optional[Rec]) -> tuple[Optional[str], str]:
    """(how the change exited, the words saying why)."""
    outcome = item["state"] if is_terminal("change", item["state"]) else (exit_ev or {}).get("to_state")
    return outcome, str(d.get("why") or (exit_ev or {}).get("text") or "")


def exit_class(outcome: Optional[str], why: str, d: Rec) -> Optional[str]:
    if outcome == "rejected":
        return classify(why, REJECT_CLASSES, "other")
    if outcome == "cancelled":
        refused = (d.get("precheck") or {}).get("outcome") == "refused" or "fast precheck" in why
        return "precheck" if refused else "cancelled"
    return {"landed": "landed", "superseded": "superseded"}.get(outcome or "")


def _landing_who(d: Rec, entry: Rec) -> Rec:
    lander = _either(d, entry, "lander") or {}
    return {"repo": _repo(lander), "lander_agent": lander.get("agent"),
            "lander_session": _either(lander, d, "session"), "lander_host": lander.get("host"),
            "worktree": _either(d, entry, "worktree"), "branch": _either(lander, entry, "branch"),
            "head_sha": _either(d, entry, "head"), "base_sha": entry.get("base"),
            "checked_base": entry.get("checked_base") or d.get("base")}


def _landing_phases(spans: list[Span], start: float, end: float, outcome: Optional[str]) -> Rec:
    first_check = next((s[1] for s in spans if s[0] == "checking"), None)
    pushing = [s for s in spans if s[0] == "pushing" and s[2] is not None]
    return {"submitted_at": iso_at(start), "exited_at": iso_at(end), "total_s": end - start,
            "queue_wait_s": (first_check - start) if first_check else None,
            "queued_s": _sum(spans, "queued", end), "checking_s": _sum(spans, "checking", end),
            "checked_wait_s": _sum(spans, "checked", end), "pushing_s": _sum(spans, "pushing", end),
            "push_to_landed_s": (cast(float, pushing[-1][2]) - pushing[-1][1]) if pushing and outcome == "landed" else None,
            "waiting_s": _sum(spans, "waiting", end)}


def _rounds_summary(rounds: list[Rec], d: Rec) -> Rec:
    full = sum(1 for r in rounds if r["mode"] == "full")
    return {"rounds": len(rounds), "full_rounds": full, "verify_rounds": len(rounds) - full,
            "full_rechecks": max(0, full - 1),
            "infra_requeues": sum(1 for r in rounds if r["reason_class"].startswith("infra:")),
            "recheck_reasons": ",".join(r["reason_class"] for r in rounds[1:]),
            # MINIMAL RECHECKS (queuerecheck.py): the times main moved under it and its verdict was kept, no round run
            "kept_verdicts": sum(1 for r in d.get("rechecks") or [] if r.get("kind") == "kept") or None}


def _fix_kind(d: Rec, entry: Rec) -> Rec:
    """Its KIND as the queue last classified it (changekind.py): a fix of an open red, and of which kinds; a change from
    before the classifier, by its trailer alone."""
    fixes = sorted(_either(d, entry, "fixes") or [])
    kind = d.get("change_kind") or {}
    return {"red_fix": bool(kind["red_fix"]) if "red_fix" in kind else bool(fixes), "fixes": ",".join(fixes),
            "fix_kinds": ",".join(sorted({r["kind"] for r in kind.get("reds") or [] if r.get("kind")})) or None,
            "priority": kind.get("priority")}


def _landing_size(d: Rec, entry: Rec, paths: Optional[list[str]]) -> Rec:
    tops, sbt = path_modules(paths or [])
    return {"commits_n": len(d.get("commit_keys") or entry.get("commits") or []) or None,
            "files_n": None if paths is None else len(paths), "modules": ",".join(tops), "sbt_modules": ",".join(sbt)}


def _landing_checks(runs: list[Rec], jobs: list[Rec]) -> Rec:
    """Its precommit-check runs (in start order) and their jobs."""
    return {"areas": ",".join(sorted({a for a in (job_area(j.get("job")) for j in jobs) if a})),
            "check_runs": ",".join(str(r.get("run_id")) for r in runs),
            "check_runner_wait_s": _total(runs, "path_queue_s") if runs else None,
            "check_run_s": _total(runs, "path_work_s") if runs else None,
            "check_jobs_n": len(jobs) or None, "check_job_queue_max_s": _peak(jobs, "queue_s"),
            "check_job_queue_sum_s": _total(jobs, "queue_s") if jobs else None,
            "check_job_run_sum_s": _total(jobs, "duration_s") if jobs else None}


def _precheck_and_batch(d: Rec, entry: Rec) -> Rec:
    pre = d.get("precheck") or {}
    return {"precheck_s": _num(pre.get("duration_s")), "precheck_outcome": pre.get("outcome"),
            "precheck_step": pre.get("step"), "batch": _either(d, entry, "batch"),
            "batch_id": _either(d, entry, "batch_id"), "superseded_by": _either(d, entry, "superseded_by")}


def _round_record(item: Rec, rec: Rec, rd: Rec) -> Rec:
    r = {"record": "agentctl.landing_round", "change_id": item["id"], "repo": rec["repo"], "outcome": rec["outcome"],
         "lander_agent": rec["lander_agent"], **rd, "started": iso_at(rd["started"]), "ended": _iso(rd["ended"])}
    r["_msg"] = (f"agentctl.landing_round {item['id']} #{rd['round']}: {rd['mode']} ({rd['reason_class']}), "
                 f"{mins_of(rd['check_s'])} -> {rd['result']}")
    r["_time"] = iso_at(rd["started"])
    return r


def mins_of(s: Optional[float]) -> str:
    return "?" if s is None else f"{s / 60:.0f} min"


def stats_common(rec: Rec, at: float, since: Optional[float]) -> Rec:
    """The fields every record has, None values dropped, durations rounded."""
    out = {"source": "agentctl", "schema": STATS_SCHEMA, "backfill": bool(since is not None and at < since)}
    for k, v in rec.items():
        if v is None or v == "":
            continue
        out[k] = round(v, 1) if isinstance(v, float) else v
    return out


def watch_record(item: Rec, change_id: Optional[str] = None, since: Optional[float] = None) -> Rec:
    """agentctl.landing_watch of a decided watch item (keyed by the landed sha)."""
    d = item["data"] or {}
    pushed, at = _num(d.get("pushed_at")), item["entered_at"]
    rec = {"record": "agentctl.landing_watch", "landed_sha": item["id"], "change_id": change_id,
           "repo": d.get("o_r"), "verdict": item["state"], "verdict_line": str(d.get("verdict") or "")[:300],
           "pushed_at": iso_at(pushed) if pushed else None, "watch_s": (at - pushed) if pushed else None,
           "_time": iso_at(at)}
    rec["_msg"] = f"agentctl.landing_watch {item['id'][:10]}: {item['state']} after {mins_of(rec['watch_s'])}"
    return stats_common(rec, at, since)


