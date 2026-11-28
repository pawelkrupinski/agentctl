# agentctl, part `statsepisodes`: the statistics' episode records: a red episode, its owner decision, a condition spell and a gate run
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from agentctl.clock import epoch_now
import re
import time
from typing import Any, Iterable, Optional, cast
from agentctl.timefmt import iso_at
from agentctl.statemachines import terminal_states
from agentctl.statsrecords import (
    Rec, Span, Wait, _iso, _number, _peak, _repo, _sum, _text, _total, item_span, mins_of, state_spans, stats_common)


RED_VERDICT_CLASS = {"test": "test-failure", "infra": "infra-shaped", "stuck-run": "infra-shaped",
                     "unclear": "no-verdict"}


HOLDER_RX = re.compile(r"leased by (\S+)")




def red_record(item: Rec, events: Iterable[Rec], waits: Iterable[Wait] = (), since: Optional[float] = None) -> Rec:
    """agentctl.red of a closed red item (an episode). waits: [(change id, waiting entered, left)] of every change, for
    how many waited on a gate red and how long (their waiting time inside the episode)."""
    d = item["data"] or {}
    evs, opened, close_ev, closed = item_span(item, events, "red", "opened_at", terminal_states("red"))
    spans = state_spans(evs, opened, "red")
    rec = {"record": "agentctl.red", "red_id": item["id"], **_red_what(d), **_red_runs(d),
           "opened_at": iso_at(opened), "closed_at": iso_at(closed), "duration_s": closed - opened,
           "outcome": item["state"], "closed_by": (close_ev or {}).get("text", "")[:200],
           **_red_fixers(evs, spans, opened, closed), **_red_fix(d, spans, opened, closed, item["state"]),
           **_red_waits(d, waits, opened, closed), **_red_owner(d, evs, spans, closed),
           **_escalation_counts(_escalated(evs)), "attempts": d.get("attempts"), "_time": iso_at(closed)}
    rec["_msg"] = (f"agentctl.red {item['id']}: {d.get('workflow')} {rec['verdict_class']} {item['state']} after "
                   f"{mins_of(rec['duration_s'])}, {rec['fixers_n']} fixer(s)")
    return stats_common(rec, closed, since)


def _red_what(d: Rec) -> Rec:
    kind: Any = d.get("kind")
    return {"key": d.get("key"), "repo": _repo(d), "workflow": d.get("workflow"),
            "gate": bool(d.get("gate")), "red_kind": d.get("red_kind"),
            "verdict_class": RED_VERDICT_CLASS.get(kind, kind), "family": d.get("family"),
            "jobs": ",".join(d.get("jobs") or [])[:600], "signature": _text(d.get("signature") or d.get("line"), 200)}


def _red_runs(d: Rec) -> Rec:
    """Its failed runs, numbers in numeric order."""
    runs = sorted((int(x) if str(x).isdigit() else x for x in d.get("runs") or []), key=lambda x: (len(str(x)), str(x)))
    last = d.get("last_run") or {}
    return {"first_run": runs[0] if runs else None, "last_run": last.get("number") or (runs[-1] if runs else None),
            "runs_n": len(runs)}


def _red_fixers(evs: list[Rec], spans: list[Span], opened: float, closed: float) -> Rec:
    """The leases its fixers took (a re-lease after a lapse: a restart) and who held them."""
    leased = [e for e in evs if e.get("to_state") == "leased" and e.get("from_state") != "leased"]
    first: Any = leased[0]["at"] if leased else None
    return {"fixers_n": len(leased), "fixer_restarts": max(0, len(leased) - 1),
            "fixer_holders": ",".join(_holders(leased)), "first_fixer_at": iso_at(first) if leased else None,
            "time_to_fixer_s": (first - opened) if leased else None,
            "leased_s": _sum(spans, "leased", closed), "offered_s": _sum(spans, "offered", closed)}


def _holders(leased: list[Rec]) -> list[str]:
    """Who took the leases, by name, each once."""
    return sorted({m.group(1) for m in (HOLDER_RX.search(e.get("text") or "") for e in leased) if m})


def _red_fix(d: Rec, spans: list[Span], opened: float, closed: float, outcome: str) -> Rec:
    fix = d.get("fix") or {}
    fix_at = _number(fix.get("at")) if fix.get("status") in ("landed", "verified") or fix.get("sha") else None
    return {"fix_pending_s": _sum(spans, "fix-pending", closed),
            "fix_sha": fix.get("sha"), "fix_entry": fix.get("entry"), "fix_status": fix.get("status"),
            "fix_landed_at": _iso(fix_at), "red_to_fix_landed_s": (fix_at - opened) if fix_at and fix_at >= opened else None,
            "red_to_green_s": (closed - opened) if outcome == "verified" else None}


def _red_waits(d: Rec, waits: Iterable[Wait], opened: float, closed: float) -> Rec:
    """The changes that waited on a gate red, and their waiting inside the episode."""
    w = [(cid, max(a, opened), min(b if b is not None else closed, closed)) for cid, a, b in waits] if d.get("gate") else []
    w = [(cid, a, b) for cid, a, b in w if b > a]
    return {"waited_changes_n": len({c for c, _a, _b in w}), "waited_changes_s": sum(b - a for _c, a, b in w)}


def _red_owner(d: Rec, evs: list[Rec], spans: list[Span], closed: float) -> Rec:
    dec = d.get("decision") or {}
    first_block = next((e["at"] for e in evs if e.get("to_state") == "blocked-external"), None)
    return {"owner_blocked": bool(first_block or dec), "blocked_s": _sum(spans, "blocked-external", closed),
            "decision_wait_s": (_number(dec.get("at")) - first_block) if dec.get("at") and first_block else None}


def _escalated(evs: list[Rec]) -> list[Rec]:
    return [e for e in evs if e.get("severity") == "escalate"]


def _escalation_counts(esc: list[Rec]) -> Rec:
    """How many times it escalated, and how many of those notified somebody."""
    return {"escalations_n": len(esc), "notifications_n": sum(1 for e in esc if e.get("notified_at"))}


def owner_decision_record(item: Rec, events: Iterable[Rec], since: Optional[float] = None) -> Optional[Rec]:
    """agentctl.owner_decision of a closed red that asked the owner (its BLOCKED ON OWNER spell), else None."""
    d = item["data"] or {}
    evs, _opened, _close_ev, closed = item_span(item, events, "red", "opened_at", terminal_states("red"))
    ask, dec = d.get("ask") or {}, d.get("decision") or {}
    blocks, asked = owner_asked(evs, ask)
    if not asked:
        return None
    answered = _number(dec.get("at"))
    end = answered or closed
    rec = {"record": "agentctl.owner_decision", "red_id": item["id"], "belongs_to": f"red:{item['id']}",
           "workflow": (d.get("workflow")), "gate": bool(d.get("gate")), "repo": _repo(d),
           "question": _text(dec.get("question") or ask.get("q"), 400), "answer": _text(dec.get("answer"), 400),
           "asked_at": iso_at(asked), "answered_at": _iso(answered), "wait_s": end - asked,
           "answer_path": answer_path(asked, answered, closed), "red_closed_at": iso_at(closed),
           "red_outcome": item["state"], "reminders_n": sum(1 for e in evs if e.get("kind") == "red.remind"),
           "asks_n": max(1, len(blocks)), "decision_failed": str(ask.get("q") or "").startswith("you decided"),
           "_time": iso_at(end)}
    rec["_msg"] = f"agentctl.owner_decision red {item['id']}: {rec['answer_path']} after {mins_of(rec['wait_s'])}"
    return stats_common(rec, closed, since)


def owner_asked(evs: list[Rec], ask: Rec) -> tuple[list[float], Optional[float]]:
    """(the times the red was blocked on the owner, when it first asked), the ask's own record when no block shows."""
    blocks = [e["at"] for e in evs if e.get("to_state") == "blocked-external" and e.get("from_state") != "blocked-external"]
    return blocks, (blocks[0] if blocks else None) or _number(ask.get("first_at")) or _number(ask.get("since"))


def answer_path(asked: float, answered: Optional[float], closed: float) -> str:
    """decided (the owner answered), else stale (a week unanswered) or auto-resolved (the red closed first)."""
    if answered:
        return "decided"
    return "stale" if closed - asked >= 7 * 86400 else "auto-resolved"


def condition_record(item: Rec, events: Iterable[Rec], since: Optional[float] = None) -> Rec:
    """agentctl.condition of a closed condition spell."""
    d = item["data"] or {}
    evs, opened, _close_ev, closed = item_span(item, events, "condition", "opened_at", ("closed",))
    esc = _escalated(evs)
    key = d.get("key") or item["id"]
    closed_by = _text(d.get("closed_by"), 200)
    rec = {"record": "agentctl.condition", "condition_id": item["id"], "key": key, "kind": d.get("kind"),
           "invariant": invariant_of(key, d), "host": d.get("host"), "opened_at": iso_at(opened),
           "escalated_at": iso_at(esc[0]["at"]) if esc else None, "closed_at": iso_at(closed),
           "duration_s": closed - opened, "violation": _text(d.get("text"), 400),
           "until": _text(d.get("until"), 200), "closed_by": closed_by,
           "manual_close": closed_by.startswith(("closed by hand", "INVALID:")), **_escalation_counts(esc),
           "_time": iso_at(closed)}
    rec["_msg"] = f"agentctl.condition {key}: {d.get('kind')} open {mins_of(rec['duration_s'])}, {len(esc)} escalation(s)"
    return stats_common(rec, closed, since)


def invariant_of(key: str, d: Rec) -> Optional[str]:
    """The invariant (W1, ...) a condition spell is of, by its key or its text, or None."""
    inv = re.match(r"invariant-(\w+)", key) or re.match(r"INVARIANT (W\w*):", str(d.get("text") or ""))
    return inv.group(1) if inv else None


GATE_RUN_FIELDS = ("workflow", "repo", "run_id", "run_number", "run_attempt", "sha", "event")  # the collector's, as is


def gate_run_record(run: Rec, jobs: Iterable[Rec], since: Optional[float] = None) -> Rec:
    """agentctl.gate_run of a completed gate run, from the collector's source:ci kind:run row and its kind:job rows."""
    jobs = [j for j in jobs if _number(j.get("duration_s")) is not None]
    end = _number(run.get("end")) or 0
    rec = {"record": "agentctl.gate_run", **{k: run.get(k) for k in GATE_RUN_FIELDS},
           "conclusion": run.get("run_conclusion"), "started_at": _iso(_number(run.get("start"))), "ended_at": _iso(end),
           "duration_s": _number(run.get("wall_s")), "path_queue_s": _number(run.get("path_queue_s")),
           "path_work_s": _number(run.get("path_work_s")), "critical_path": _text(run.get("critical_path"), 600),
           **_critical_job(run, jobs), **_slowest_job(jobs), "jobs_n": len(jobs), "runner_min": _number(run.get("runner_min")),
           "runner_wait_s": _total(jobs, "queue_s"), "run_s": _total(jobs, "duration_s"),
           "job_queue_max_s": _peak(jobs, "queue_s"),
           "failed_jobs": ",".join(sorted(cast(str, j.get("job")) for j in jobs if j.get("conclusion") == "failure"))[:600],
           "_time": iso_at(end or epoch_now())}
    rec["_msg"] = (f"agentctl.gate_run {run.get('workflow')} #{run.get('run_number')}: {run.get('run_conclusion')} "
                   f"in {mins_of(rec['duration_s'])}, critical {rec['critical_job']}")
    return stats_common(rec, end, since)


def _critical_job(run: Rec, jobs: list[Rec]) -> Rec:
    """The critical path's last job (the collector's), else the job that ended last."""
    hops = [h.strip() for h in str(run.get("critical_path") or "").split("→") if h.strip()]
    crit = next((j for j in jobs if hops and j.get("job") == hops[-1]), None) or \
        max(jobs, key=lambda j: _number(j.get("end")) or 0, default=None)
    crit = crit or {}
    return {"critical_job": crit.get("job"), "critical_job_s": _number(crit.get("duration_s")),
            "critical_job_queue_s": _number(crit.get("queue_s"))}


def _slowest_job(jobs: list[Rec]) -> Rec:
    slow = max(jobs, key=lambda j: _number(j.get("duration_s")) or 0, default=None) or {}
    return {"slowest_job": slow.get("job"), "slowest_job_s": _number(slow.get("duration_s"))}
