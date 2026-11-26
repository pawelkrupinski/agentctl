# agentctl, part `redearly`: EARLY REDS -- a failed job of a gate run still in progress, acted on before the run ends.
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import os
import re
from typing import Any, Callable
from agentctl.core import err, fmt_age, reds_dir
from agentctl.timefmt import ts
from agentctl.statefile import read_json_file, write_json_file, note_failure
from agentctl.forge import RED_CONCLUSIONS, api
from agentctl.allowedpaths import allowed_path
from agentctl.inboxentry import inbox_write
from agentctl.redrecord import lease_is_live, red_active_of, red_lease, red_obs_for, red_what
from agentctl.leases import lease_meta
from agentctl.red import red_close
from agentctl.redseen import red_obs_of_units, red_units
from agentctl.redopen import red_culprit_search
from agentctl.runexplain import explain_run_jobs
from agentctl.redknobs import knob_early_max_runs


EARLY_BACKOFF_FIRST = 60      # s the early reads pause after a rate-limit or 5xx answer; doubled each time
EARLY_BACKOFF_MAX = 900
EARLY_JOB_PAGES = 3           # pages of 100 jobs read at most per run
EARLY_TAIL = 16               # lines of the failed jobs' explanation kept for the fixer's brief
EARLY_LIMITED = re.compile(r"HTTP (403|429|5\d\d)|rate limit|abuse detection|secondary rate", re.I)

JobsGet = Callable[[str, str, dict[str, Any]], Any]
Explain = Callable[[str, dict[str, Any], int], Any]
Run = dict[str, Any]


def early_dir(*parts: str) -> str:
    return str(reds_dir("early", *parts))


def snap_runs(snap: dict[str, Any] | None) -> list[tuple[str, list[Run], bool]]:
    """(workflow, its runs on main, gate) of every workflow the snapshot lists, the gate's first."""
    return [(str(g["name"]), list(g.get("runs") or []), part == "gate")
            for part in ("gate", "outside") for g in (snap or {}).get(part) or []]


def early_running(r: Run) -> bool:
    return r.get("status") == "in_progress" and bool(r.get("databaseId")) and r.get("headBranch", "main") == "main"


def early_runs(snap: dict[str, Any] | None) -> list[tuple[str, Run, bool]]:
    """(workflow, run, gate) of every run on main the snapshot shows in progress: the gate's first, at most
    EARLY_MAX_RUNS."""
    out = [(name, r, gate) for name, runs, gate in snap_runs(snap) for r in runs if early_running(r)]
    return out[:int(knob_early_max_runs())]


def snap_run(snap: dict[str, Any] | None, workflow: str | None, rid: Any) -> Run | None:
    """The snapshot's run of `workflow` with id rid, or None when its lists no longer hold it."""
    return next((dict(r) for name, runs, _gate in snap_runs(snap) if name == workflow for r in runs
                 if str(r.get("databaseId")) == str(rid)), None)


class EarlyBackoff:
    """THE RATE-LIMIT BACK-OFF of the early reads (a state file: it outlives the process). A 403 / 429 / 5xx pauses
    every early read for EARLY_BACKOFF_FIRST s, doubling to EARLY_BACKOFF_MAX; a good answer ends it."""

    def __init__(self, path: str) -> None:
        self.path = path

    def paused(self, t: float) -> bool:
        return t < float(read_json_file(self.path).get("until") or 0)

    def hit(self, t: float, why: str) -> float:
        delay = min(EARLY_BACKOFF_MAX, 2 * float(read_json_file(self.path).get("delay") or EARLY_BACKOFF_FIRST / 2))
        write_json_file(self.path, {"until": t + delay, "delay": delay, "why": why[:200]})
        return delay

    def ok(self) -> None:
        if read_json_file(self.path):
            write_json_file(self.path, {})


def early_jobs(o_r: str, rid: Any, get: JobsGet) -> list[dict[str, Any]]:
    """The latest attempt's jobs of run rid (GitHub's REST shape), paged by 100 up to EARLY_JOB_PAGES."""
    jobs: list[dict[str, Any]] = []
    for page in range(1, EARLY_JOB_PAGES + 1):
        d = get(o_r, f"actions/runs/{rid}/jobs", {"filter": "latest", "per_page": 100, "page": page}) or {}
        jobs += d.get("jobs") or []
        if len(jobs) >= int(d.get("total_count") or 0) or not d.get("jobs"):
            break
    return jobs


def early_failed(jobs: list[dict[str, Any]]) -> tuple[list[str], int]:
    """(the ids of the jobs concluded failed, the newest attempt the jobs belong to)."""
    ids = sorted(str(j.get("id")) for j in jobs if j.get("status") == "completed"
                 and j.get("conclusion") in RED_CONCLUSIONS)
    return ids, max([int(j.get("run_attempt") or 1) for j in jobs] or [1])


class EarlyReader:
    """The early reds of the in-progress runs: their jobs read (get: the REST GET, api's conditional one), a changed
    set of failed jobs explained (explain: ci-why's explain_run_jobs), each run's answer kept under reds/early/<id>.json
    so a paused or failed read answers what was last read -- an early red is never withdrawn by a read that did not
    happen."""

    def __init__(self, get: JobsGet | None = None, explain: Explain | None = None) -> None:
        self.get: JobsGet = get or (lambda o_r, path, params: api(o_r, "GET", path, params))
        self.explain: Explain = explain or (lambda o_r, run, tail: explain_run_jobs(o_r, run, tail))
        self.backoff = EarlyBackoff(early_dir("backoff.json"))

    def run_units(self, o_r: str, name: str, run: Run, gate: bool, runs: list[Run], t: float) -> list[dict[str, Any]]:
        """The early red units of one in-progress run: read again only when its failed jobs changed."""
        rid = run["databaseId"]
        rec = read_json_file(early_dir(f"{rid}.json"))
        if self.backoff.paused(t):
            return list(rec.get("units") or [])
        try:
            ids, attempt = early_failed(early_jobs(o_r, rid, self.get))
            units = list(rec.get("units") or []) if (ids, attempt) == (rec.get("failed"), rec.get("attempt")) else \
                self.classify(o_r, name, dict(run, attempt=attempt), gate, runs, t) if ids else []
        except (Exception, SystemExit) as e:
            self.unread(name, run, t, str(e))
            return list(rec.get("units") or [])
        self.backoff.ok()
        write_json_file(early_dir(f"{rid}.json"), {"run": rid, "attempt": attempt, "failed": ids, "units": units, "at": t})
        return units

    def unread(self, name: str, run: Run, t: float, why: str) -> None:
        """A read that failed: a rate limit or a 5xx pauses every early read (EarlyBackoff)."""
        if EARLY_LIMITED.search(why):
            why = f"{why[:120]}: early reads paused {fmt_age(self.backoff.hit(t, why))}"
        err(f"red early: {name} #{run.get('number')}: jobs unread ({why[:200]})")

    def classify(self, o_r: str, name: str, run: Run, gate: bool, runs: list[Run], t: float) -> list[dict[str, Any]]:
        """The run's failed jobs so far as red units, by THE classifier: only the test-shaped ones (infra and unclear
        wait for the run's verdict; a `verdict complete` report is none)."""
        lines, _num, per_job = self.explain(o_r, run, 6)
        sigs = [{"job": j, "kind": k, "line": l or ""} for j, k, l in per_job if k == "test"]
        if not sigs:
            return []
        early = {"run": run.get("databaseId"), "number": run.get("number"), "attempt": run.get("attempt"),
                 "seen_at": t}
        early["brief"] = early_brief(early, [str(x) for x in lines][:EARLY_TAIL])
        return [dict(u, early=early, names=sorted(u["names"]))
                for u in red_units(o_r, name, run, runs, sigs, gate, [0])]

    def observe(self, o_r: str, snap: dict[str, Any] | None, t: float) -> dict[str, Any]:
        """{key: observation} of the early reds now (red_obs_of_units' shape, each with its run's `early`)."""
        lists = {name: runs for name, runs, _gate in snap_runs(snap)}
        units = [u for name, run, gate in early_runs(snap)
                 for u in self.run_units(o_r, name, run, gate, lists.get(name) or [], t)]
        early_of = {u["run"].get("databaseId"): u["early"] for u in units}
        obs: dict[str, Any] = dict(red_obs_of_units(units))
        for ob in obs.values():
            ob["early"] = early_of.get(ob["run"].get("databaseId"))
        return obs


def early_brief(e: dict[str, Any], tail: list[str]) -> str:
    """The fixer brief's words for an early red (fixer_prompt reads them from its picture): the run goes on."""
    lines = "\n".join(f"    {x}" for x in tail)
    return (f" THE RUN IS STILL IN PROGRESS: run #{e.get('number')} (attempt {e.get('attempt') or 1}) had not finished "
            "when this job failed, so other jobs may still fail -- agentctl adds a later failure of it to your red "
            "(RED-INBOX.md), and a STOP (RED-STOP.md) means the failure was withdrawn (the run was cancelled or "
            "superseded, or a rerun cleared it): stand down then. Nothing is blamed or reverted before the run ends. "
            "Fix it the usual way meanwhile (`agentctl ci-why <run id>` reads a running run's failed jobs too)."
            + (f" What ci-why read of the failed job(s):\n{lines}\n" if lines else ""))


# -- the red tick's side (redtick.py calls these; each a no-op for a red that is not early)
def early_observed(store: Any, o_r: str, snap: dict[str, Any] | None, t: float, obs: dict[str, Any] | None,
                   early: EarlyReader | None) -> dict[str, Any] | None:
    """The tick's observations with the early ones added (none while the completed runs are unread): a key a completed
    run observes keeps that observation, and an early one never attaches to a red a completed run opened (its verify,
    refute and clear read completed runs only)."""
    if obs is None:
        return None
    try:
        seen = red_obs_for(o_r, (early or EarlyReader()).observe(o_r, snap, t))
        active = red_active_of(store, o_r)
    except (Exception, SystemExit) as e:
        err(f"red tick: could not read the in-progress runs ({str(e)[:160]}): no early red this tick")
        return obs
    return {**{k: ob for k, ob in seen.items() if k not in active or active[k]["data"].get("early")}, **obs}


def early_opened(it: dict[str, Any], acts: list[str]) -> dict[str, Any]:
    """A red just opened: when early, the owner told (the author's offer tells the lander's session)."""
    d = it["data"]
    if d.get("early"):
        line = (f"RED EARLY: {red_what(d)} failed while run #{d['early'].get('number')} is still in progress: "
                f"{str(d.get('line') or '')[:200]} -- dispatched now; other jobs may still fail, and blame and revert "
                "wait for the run's verdict")
        inbox_write("red", line, [line], [], 0)
        acts.append(line)
    return it


def early_attach(store: Any, it: dict[str, Any], ob: dict[str, Any], repo: str | None, o_r: str, t: float,
                 acts: list[str]) -> dict[str, Any]:
    """An early red observed again: by its run's completed verdict (SETTLED), or by a later job failing the same way
    (one red: red_attach_obs adds the job; its holder told)."""
    d = it["data"]
    if not d.get("early"):
        return it
    if ob.get("early"):
        more = sorted(set(ob.get("jobs") or []) - set(d.get("jobs") or []))
        if more:
            early_tell(store, d, f"also failing the same way now: {', '.join(more)}")
        return it
    acts += early_settle(store, it, ob, repo, o_r, t, f"run #{ob['run'].get('number')} failed so")
    return dict(store.item("red", it["id"]))


def early_unobserved(store: Any, it: dict[str, Any], obs: dict[str, Any] | None, snap: dict[str, Any] | None,
                     repo: str | None, o_r: str, t: float) -> list[str]:
    """An early red this tick does not observe: withdrawn or settled (early_fate); [] for any other red."""
    if obs is None or not it["data"].get("early") or it["data"]["key"] in obs:
        return []
    it = store.item("red", it["id"])
    fate, why = early_fate(it["data"], snap)
    if fate == "settle":
        return early_settle(store, it, {}, repo, o_r, t, why)
    if fate == "withdraw":
        red_close(store, it, "cleared", f"EARLY RED WITHDRAWN: {why}; stand down -- no blame and no revert")
        return [f"withdrew early red {red_what(it['data'])}: {why}"]
    return []


def early_fate(d: dict[str, Any], snap: dict[str, Any] | None) -> tuple[str, str]:
    """What becomes of early red d that this tick does not observe: (keep | withdraw | settle, why)."""
    e = d.get("early") or {}
    run = snap_run(snap, d.get("workflow"), e.get("run"))
    num = f"#{e.get('number')}"
    if run is None:
        return early_fate_gone(d, snap, e)
    if run.get("status") != "completed":
        return early_fate_running(e)
    if run.get("conclusion") not in RED_CONCLUSIONS:
        return "withdraw", f"run {num} ended {run.get('conclusion')} (cancelled, superseded or green)"
    return "settle", f"run {num} failed: its early red stands as the run's verdict"


def early_fate_gone(d: dict[str, Any], snap: dict[str, Any] | None, e: dict[str, Any]) -> tuple[str, str]:
    """Its run no longer in the snapshot's lists: withdrawn once a newer run of the workflow decided, else kept."""
    last = ((snap or {}).get("latest") or {}).get(d.get("workflow")) or {}
    if int(last.get("databaseId") or 0) > int(e.get("run") or 0):
        return "withdraw", f"its run #{e.get('number')} is gone from main's runs and a newer one decided"
    return "keep", ""


def early_fate_running(e: dict[str, Any]) -> tuple[str, str]:
    """Its run still in progress, the failure no longer read: a rerun attempt clearing it (the reader's attempt)."""
    attempt = int(read_json_file(early_dir(f"{e.get('run')}.json")).get("attempt") or 1)
    if attempt > int(e.get("attempt") or 1):
        return "withdraw", f"a rerun (attempt {attempt}) of #{e.get('number')} is clearing it"
    return "withdraw", f"run #{e.get('number')} no longer shows the failure"


def early_settle(store: Any, it: dict[str, Any], ob: dict[str, Any], repo: str | None, o_r: str, t: float,
                 why: str) -> list[str]:
    """The run completed failing: the early red is the run's verdict from now on -- `early` cleared, its culprit
    search opened now from the completed observation (red_culprit_search), its holder told."""
    d = it["data"]
    upd: dict[str, Any] = {"early": None, "early_settled": {**(d.get("early") or {}), "at": t, "why": why}}
    if ob:
        upd.update(red_culprit_search(store, dict(ob, early=None), repo, t, o_r))
    store.update_item("red", it["id"], it["version"], upd)
    early_tell(store, d, f"run #{(d.get('early') or {}).get('number')} completed: {why}")
    return [f"settled early red {red_what(d)}: {why}"]


def early_tell(store: Any, d: dict[str, Any], text: str) -> None:
    """A line in the red holder's RED-INBOX.md (its worktree), when one holds it."""
    row = store.get(red_lease(d["key"]))
    wt = lease_meta(row).get("worktree") if lease_is_live(row, store.clock()) else None
    if not wt or not os.path.isdir(wt) or not allowed_path(wt):
        return
    try:
        with open(os.path.join(wt, "RED-INBOX.md"), "a") as f:
            f.write(f"{ts()} RED EARLY {red_what(d)} (red {d['key']}): {text}\n")
    except OSError as e:
        note_failure("RED-INBOX.md note not written", e)
