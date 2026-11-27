# agentctl, part `branches`: branch runs: their inputs against main's, their state, branch reds and their route; `agentctl dispatch`
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from agentctl.clock import epoch_now
import datetime as dt
import json
import os
import re
import sys
import time
import urllib.parse
from typing import Any
from agentctl.procrunner import process_runner
from agentctl.core import dispatches_dir, err, git_ok, remote, slug
from agentctl.timefmt import ts
from agentctl.statefile import read_record, write_json, unlink_file
from agentctl.forge import RED_CONCLUSIONS, RUN_FIELDS, api, forge_argv, forge_env, gh_json
from agentctl.inboxentry import inbox_write
from agentctl.owned import default_repo
from agentctl.worktrees import merged_reason
from agentctl.inbox import INBOX_TAIL
from agentctl.ciruns import main_runs_of
from agentctl.runsigs import save_run_sigs
from agentctl.gatesummary import SUMMARY_MAX_REDS
from agentctl.fixerblockers import short_flow
from agentctl.runowners import branch_base, branch_red_id, dispatch_key, dispatch_records, orphan_why, owner_gone, owner_of, owner_text, record_dispatch, save_dispatch, tracked_branch
from agentctl.recordingpins import pinned_recording, pinned_runs
from agentctl.runexplain import explain_run_jobs
from agentctl.tickknobs import knob_dispatch_find, knob_dispatch_find_poll


# inputs that change where or how a run runs, or how much of it, never what main's run of the same workflow would decide
INPUTS_NOT_PREDICTIVE = frozenset({"tier", "runner", "profile", "jvm-options", "heap", "reuse-run", "takes-base",
                                   "takes-countries", "countries"})
BRANCH_RUNS_LIMIT = 150
BRANCH_ROUTE_MAX_AGE = 24 * 3600  # a failure first seen older than this is recorded, not routed (no flood on upgrade)


def run_inputs(runs, recs=None):
    """{run id: inputs or None (unread)} for runs: {} for a run not dispatched by hand (a push), else the owner record's,
    the cache (GitHub serves no run's inputs)."""
    out = {}
    for run in runs:
        rid = run.get("databaseId")
        rec = (recs or {}).get(rid)
        cached = read_record(dispatches_dir("inputs", f"{rid}.json"))
        if run.get("event") and run["event"] != "workflow_dispatch":
            out[rid] = {}
        elif rec and rec.get("inputs") is not None and rec.get("run_id") in (None, rid):
            out[rid] = rec["inputs"]
        elif cached is not None:
            out[rid] = cached
        else:
            out[rid] = None
    return out


def norm_input(v):
    v = "" if v is None else str(v).strip()
    return "" if v.lower() in ("false", "") else v


def input_diff(inputs, main_inputs):
    """[(key, the branch run's value, main's)] of the predictive inputs that differ."""
    keys = sorted((set(inputs) | set(main_inputs)) - INPUTS_NOT_PREDICTIVE)
    return [(k, norm_input(inputs.get(k)), norm_input(main_inputs.get(k))) for k in keys
            if norm_input(inputs.get(k)) != norm_input(main_inputs.get(k))]


def diff_text(diff):
    parts = []
    for k, mine, theirs in diff[:3]:
        if k == "recording" and not theirs:
            theirs = pinned_recording()
        parts.append(f"{k} {mine or '(none)'} ≠ main's {theirs or '(none)'}")
    return ", ".join(parts) + (f" (+{len(diff) - 3} more)" if len(diff) > 3 else "")


def newer_recording(inputs, main_inputs):
    """The branch run's recording when it is newer than the one main replays, else None."""
    mine, theirs = norm_input(inputs.get("recording")), norm_input(main_inputs.get("recording"))
    ref = [int(theirs)] if theirs.isdigit() else [int(x) for x in pinned_runs() if x.isdigit()] if not theirs else []
    return mine if mine.isdigit() and ref and int(mine) > max(ref) else None


def predicts_main(repo, base, main_run, inputs, main_inputs):
    """(predicts, why not / '') for a failed branch run."""
    if not repo or not base:
        return False, "its commit is unknown here"
    if not git_ok(repo, "cat-file", "-e", f"{base}^{{commit}}"):
        return False, f"its commit {base[:9]} is not here"
    if not merged_reason(repo, base, f"refs/remotes/{remote()}/main"):
        return False, f"its commit {base[:9]} is not on main"
    if not main_run:
        return False, "main runs no such workflow"
    if inputs is None or main_inputs is None:
        return False, "its inputs or main's are unread (`agentctl ci-why` from a terminal reads them)"
    diff = input_diff(inputs, main_inputs)
    if diff:
        return False, diff_text(diff)
    return True, ""


def branch_exists(o_r, name):
    """False only when the forge says the branch is gone (404); True otherwise (an unread answer keeps it)."""
    try:
        gh_json(o_r, "api", f"repos/{o_r}/branches/{urllib.parse.quote(name, safe='')}")
        return True
    except (Exception, SystemExit) as e:
        return not re.search(r"HTTP 404|Not Found", str(e))


def branch_state(o_r, repo, store, prev=None):
    """[entry] per tracked (branch, workflow): the newest completed run, its owner, whether its branch is alive, and for
    a red one whether it PREDICTS MAIN RED (why not). A red no longer listed carries over from prev until its branch
    is gone."""
    latest, newest_main = newest_branch_runs(o_r)
    carry_over_reds(latest, prev)
    reds = {k: r for k, r in latest.items() if r.get("conclusion") in RED_CONCLUSIONS}
    recs = {r["databaseId"]: owner_of(repo, store, b, r) for (b, _wf), r in reds.items()}
    mains = {wf: newest_main.get(wf) for (_b, wf) in reds}
    inputs = run_inputs(list(reds.values()) + [m for m in mains.values() if m], recs)
    out = []
    for (b, wf), r in sorted(latest.items()):
        e = {"branch": b, "workflow": wf, "run": {k: r.get(k) for k in ("databaseId", "number", "conclusion", "headSha",
                                                                          "updatedAt", "url", "event")},
             "red": (b, wf) in reds}
        if e["red"]:
            e.update(red_branch_facts(o_r, repo, b, r, recs.get(r["databaseId"]), mains.get(wf), inputs))
        out.append(e)
    return out


def carry_over_reds(latest: Any, prev: Any) -> Any:
    """A red past the listed window stays one (put into latest) until its branch goes or turns green."""
    for e in prev or []:
        k = (e["branch"], e["workflow"])
        if e.get("red") and k not in latest and e.get("alive"):
            latest[k] = dict(e["run"], headBranch=e["branch"], workflowName=e["workflow"])


def newest_branch_runs(o_r: Any) -> Any:
    """({(branch, workflow): its newest completed run}, {workflow: main's newest completed run}) from the newest
    BRANCH_RUNS_LIMIT runs listed and the gate and outside workflows' lists."""
    # listing: the newest BRANCH_RUNS_LIMIT; a red no longer listed carries over from prev until its branch is gone
    runs = gh_json(o_r, "run", "list", "--limit", str(BRANCH_RUNS_LIMIT), "--json", RUN_FIELDS + ",headBranch,event")
    recorded = {r.get("branch") for r in dispatch_records() if r.get("branch")}
    latest, newest_main = {}, {}
    for r in runs:  # newest first
        b, wf = r.get("headBranch") or "", r.get("workflowName")
        if r.get("status") != "completed":
            continue
        if b == "main":
            newest_main.setdefault(wf, r)
        elif tracked_branch(b, recorded):
            latest.setdefault((b, wf), r)
    newer_gate_mains(o_r, newest_main)
    return latest, newest_main


def newer_gate_mains(o_r: str, newest_main: Any) -> Any:
    """The gate and outside workflows' own lists of main's runs, a newer completed one replacing the listing's."""
    for wf, rs in main_runs_of(o_r).lists():
        m = next((r for r in rs if r.get("status") == "completed"), None)
        if m and (wf not in newest_main or m["databaseId"] > newest_main[wf]["databaseId"]):
            newest_main[wf] = m


def red_branch_facts(o_r: Any, repo: Any, b: Any, r: Any, rec: Any, m: Any, inputs: Any) -> Any:
    """What a red branch run adds to its entry: its owner, base, whether it predicts main red (and why), whether it
    is an orphan, main's run and whether the recording is newer."""
    base = branch_base(b, r)
    mine, theirs = inputs.get(r["databaseId"]), inputs.get(m["databaseId"]) if m else None
    pred, why = predicts_main(repo, base, m, mine, theirs)
    main_since = m.get("number") if m and m["databaseId"] > r["databaseId"] else None
    return {"owner": rec, "owner_gone": owner_gone(rec), "base": base, "alive": branch_exists(o_r, b), "predicts": pred,
            "why": why, "orphan": orphan_why(repo, rec, base, main_runs_it=bool(m), main_since=main_since),
            "main_run": m and {"databaseId": m["databaseId"], "number": m.get("number")},
            "newer_recording": newer_recording(mine, theirs) if mine is not None and theirs is not None else None}


def branch_reds(snap):
    """The snapshot's live branch reds: failed, branch still there (newest run of that workflow on it), not an orphan
    (an orphan closed itself: settle_branch_reds)."""
    return [e for e in (snap or {}).get("branches") or [] if e.get("red") and e.get("alive") and not e.get("orphan")]


def settle_branch_reds(o_r, snap, store):
    """Close the orphan branch reds (event: why), reopen the ones that found an owner (marked `reopened` in the snapshot
    so route_branch_reds tells that owner), retire the ones gone from the snapshot. Returns the event texts."""
    st = store
    out, live = [], set()
    for e in (snap or {}).get("branches") or []:
        if not e.get("red") or not e.get("alive"):
            continue
        iid = branch_red_id(o_r, e["run"]["databaseId"])
        live.add(iid)
        text = settle_branch_red(st, e, iid, st.item("branch-red", iid))
        if text:
            out.append(text)
    return out + retire_branch_reds(st, o_r, live)


def settle_branch_red(st: Any, e: Any, iid: Any, it: Any) -> Any:
    """One live red branch run: closed when it is an orphan, reopened when it found an owner; the event text, or None."""
    r = e["run"]
    what = f"{e['workflow']} #{r.get('number')} on {e['branch']}"
    refs = {"run": r.get("number"), "workflow": e["workflow"], "branch": e["branch"]}
    if e.get("orphan"):
        if it and it["state"] == "orphaned":
            return None
        it = it or st.put_item("branch-red", iid, "open", {"branch": e["branch"], "workflow": e["workflow"], "run": r})
        cleanup = "; its finished branch is for `agentctl cleanup`" if e["branch"].startswith("measure/") else ""
        text = f"branch red closed itself: {what}: {e['orphan']}{cleanup}"
        st.transition("branch-red", iid, "orphaned", it["version"], data={"why": e["orphan"]},
                      event={"cause": "ci-verdict", "text": text, "refs": refs})
        return text
    if not (it and it["state"] == "orphaned"):
        return None
    text = f"branch red reopened: {what} → owner {owner_text(e.get('owner'))}"
    st.transition("branch-red", iid, "open", it["version"], data={"why": None}, event={"cause": "ci-verdict", "text": text, "refs": refs})
    unlink_file(routed_path(r["databaseId"]))
    e["reopened"] = True
    return text


def retire_branch_reds(st: Any, o_r: Any, live: Any) -> Any:
    """The repo's open or orphaned branch reds gone from the snapshot (its branch deleted or green) or past their
    deadline: retired, the event texts."""
    out = []
    t = st.clock()
    for it in st.items("branch-red", ["open", "orphaned"]):
        if not it["id"].startswith(f"{o_r}#"):
            continue
        gone = it["id"] not in live
        if gone or it["deadline"] < t:
            d = it["data"]
            text = (f"branch red gone: {d.get('workflow')} #{(d.get('run') or {}).get('number')} on {d.get('branch')}"
                    + (" (its branch deleted or green)" if gone else " (past its deadline)"))
            st.transition("branch-red", it["id"], "gone", it["version"], event={"cause": "ci-verdict", "text": text})
            out.append(text)
    return out


def routed_path(rid):
    return dispatches_dir("routed", f"{rid}.json")


def entry_predicts(e):
    """PREDICTS MAIN RED, unless the routed failure is infra-shaped only (a runner's trouble predicts nothing)."""
    return bool(e.get("predicts")) and not (read_record(routed_path(e["run"]["databaseId"])) or {}).get("infra")


def branch_red_line(e):
    r, rec = e["run"], e.get("owner")
    line = f"BRANCH RED: {e['workflow']} #{r.get('number')} on {e['branch']} → owner {owner_text(rec)}"
    if entry_predicts(e):
        line += f"; PREDICTS MAIN RED (its commit {str(e.get('base'))[:9]} is on main, main's inputs)"
    elif e.get("why"):
        line += f" ({e['why']})"
    rt = read_record(routed_path(r["databaseId"])) or {}
    if rt.get("blocks_promoting"):
        line += f"; blocks promoting recording {rt['blocks_promoting']}"
    return line


def branch_red_unrouted(e: Any, t: Any) -> Any:
    """Whether this branch entry's red run is still to be routed. A run on a deleted branch is dropped silently (its
    owner finished with it); an orphan closed itself (settle_branch_reds), routed if it reopens; a run past the route
    window is recorded unrouted."""
    r = e["run"]
    if not e.get("red") or not e.get("alive") or os.path.exists(routed_path(r["databaseId"])) or e.get("orphan"):
        return False
    try:
        done = dt.datetime.fromisoformat(str(r.get("updatedAt")).replace("Z", "+00:00")).timestamp()
    except ValueError:
        done = t
    if t - done > BRANCH_ROUTE_MAX_AGE and not e.get("reopened"):
        write_json(routed_path(r["databaseId"]), {"at": ts(), "routed": False, "why": "older than the route window"})
        return False
    return True


def route_branch_red(o_r: Any, e: Any) -> Any:
    """Write the inbox entry of one failed branch run, ci-why's first error lines under it, its signatures saved."""
    r = e["run"]
    try:
        lines, _num, per_job = explain_run_jobs(o_r, dict(r, workflowName=e["workflow"]), 4)
        save_run_sigs(o_r, r, e["workflow"], per_job)
    except (Exception, SystemExit) as ex:
        lines, per_job = [f"  (could not read the run: {str(ex)[:160]})"], []
    kinds = {p[1] for p in per_job} - {"incomplete"}
    rec = {"at": ts(), "routed": True, "infra": bool(kinds) and kinds <= {"infra"},
           "blocks_promoting": e.get("newer_recording") if "test" in kinds else None}
    write_json(routed_path(r["databaseId"]), rec)
    line = branch_red_line(e)
    tail = [l for l in lines if l.startswith("      ") or l.startswith("  [")][:INBOX_TAIL] or lines[:INBOX_TAIL]
    inbox_write("branch", line, [line], tail, 1, cwd=(e.get("owner") or {}).get("worktree"))
    return line


def route_branch_reds(o_r, snap, now_=None):
    """ONE inbox entry per failed branch run (BRANCH RED) whose branch still exists, with ci-why's first error lines;
    signatures saved, so a PREDICTS MAIN RED run is a red unit. Returns the lines written."""
    t = now_ if now_ is not None else epoch_now()
    return [route_branch_red(o_r, e) for e in (snap or {}).get("branches") or [] if branch_red_unrouted(e, t)]


def branch_lines(snap):
    """ci-why's section on the branch reds."""
    reds = branch_reds(snap)
    if not reds:
        return []
    out = [f"Branch reds (runs on measure/check/fixer branches; {len(reds)}):"]
    for e in reds:
        out.append("  " + branch_red_line(e).removeprefix("BRANCH RED: ")
                   + f" -- `agentctl ci-why {e['run']['databaseId']}`")
    return out


def branch_summary(snap):
    """`branch reds: N → owner (workflow #n[ PREDICTS MAIN RED]); ...` for the status line, or ''."""
    reds = branch_reds(snap)
    if not reds:
        return ""
    parts = [f"{owner_text(e.get('owner'), short=True)} ({short_flow(e['workflow'])} #{e['run'].get('number')}"
             + (" PREDICTS MAIN RED" if entry_predicts(e) else "") + ")" for e in reds]
    if len(parts) > SUMMARY_MAX_REDS:
        parts = parts[:SUMMARY_MAX_REDS] + [f"+{len(parts) - SUMMARY_MAX_REDS} more"]
    return f"branch reds: {len(reds)} → " + "; ".join(parts)


DISPATCH_HELP = """Every CI run dispatched by hand has an owner record (~/.cache/agentctl/dispatches/): its failures are
routed to that owner (BRANCH RED in the inbox), and a run predicting main's next red is claimed and fixed like one.

  agentctl dispatch run <workflow> --ref <branch> [-f key=value ...] [--purpose "..."]
      `gh workflow run`, recorded: the worktree (the cwd's), CLAUDE_CODE_SESSION_ID / $AGENTCTL_AGENT_NAME,
      $CLAUDE_PID, the inputs, the run id once it shows.
  agentctl dispatch record --branch <branch> [--run <id>] [--workflow W] [-f key=value ...] [--purpose "..."]
      register a dispatch made another way (a script's own gh call).
  agentctl dispatch list
      every record: branch / run, owner, alive or gone, purpose.
Without a record, a measure/<user>/<sha>-<x> branch is matched to the worktree that holds <sha> (HEAD or reflog), a
check/<entry> branch to its queue entry's worktree."""


def parse_fields(fields):
    out = {}
    for kv in fields or []:
        k, sep, v = kv.partition("=")
        if not sep:
            raise SystemExit(f"agentctl: -f takes key=value (got {kv!r})")
        out[k] = v
    return out


def cmd_dispatch(a):
    o_r = slug(a.repo) if getattr(a, "repo", None) else default_repo()
    if a.dcmd == "list":
        return dispatch_list()
    inputs = parse_fields(a.field)
    if a.dcmd == "record":
        rec = record_dispatch(branch=a.branch, run_id=a.run, workflow=a.workflow, purpose=a.purpose, inputs=inputs or None)
        print(f"dispatch: recorded {rec.get('branch') or ''} run {rec.get('run_id') or '?'} → owner {owner_text(rec)}")
        return 0
    return dispatch_run(a, o_r, inputs)


def dispatch_list() -> int:
    """`agentctl dispatch list`: every recorded dispatch, newest last, with whether its owner is still there."""
    recs = dispatch_records()
    if not recs:
        print(f"dispatch: no records ({dispatches_dir()})")
    for r in recs:
        unmatched = r.get("source") == "unmatched"
        print(f"{r.get('at', '?')} {r.get('branch') or ''} run {r.get('run_id') or '?'} {r.get('workflow') or ''}: "
              f"{'-' if unmatched else owner_text(r)} [{dispatch_owner_state(r, unmatched)}] {r.get('purpose') or ''}")
    return 0


def dispatch_owner_state(r: Any, unmatched: bool) -> str:
    """Where a recorded dispatch's owner stands: unmatched (no owner to find), gone, alive, or none recorded."""
    if unmatched:
        return "unmatched"
    gone = owner_gone(r)
    return "owner gone" if gone else "owner alive" if gone is False else "no owner"


def dispatch_run(a: Any, o_r: str, inputs: Any) -> int:
    """`agentctl dispatch run`: start the workflow on a.ref, record the dispatch, and find the run it created."""
    t0 = dt.datetime.fromtimestamp(epoch_now(), dt.timezone.utc) - dt.timedelta(seconds=60)
    args = ["workflow", "run", a.workflow, "--ref", a.ref]
    for k, v in inputs.items():
        args += ["-f", f"{k}={v}"]
    r = process_runner().run(forge_argv(o_r, *args), capture_output=True, text=True, env=forge_env(o_r), timeout=300)
    sys.stdout.write(r.stdout)
    sys.stderr.write(r.stderr)
    if r.returncode != 0:
        return r.returncode
    rec = record_dispatch(branch=a.ref, run_id=None, workflow=a.workflow,
                          purpose=a.purpose, inputs=inputs,
                          key=None if a.ref != "main" else f"main-{re.sub(r'[^A-Za-z0-9._-]', '_', a.workflow)}-{int(epoch_now())}")
    rid = dispatched_run_id(o_r, a, t0)
    if rid:
        rec = save_dispatch(dict(rec, run_id=rid), None if a.ref != "main" else dispatch_key(None, rid))
        write_json(dispatches_dir("inputs", f"{rid}.json"), inputs)
    print(f"dispatched {a.workflow} on {a.ref}: run {rid or '(not listed yet)'} → owner {owner_text(rec)}")
    return 0


def dispatched_run_id(o_r: str, a: Any, t0: Any) -> Any:
    """The newest run of a.workflow on a.ref created since t0, or None once DISPATCH_FIND polls found none yet."""
    for _ in range(int(knob_dispatch_find())):
        try:
            # listing: the run just dispatched, created since t0; not listed yet is retried (DISPATCH_FIND), never 'none'
            runs = gh_json(o_r, "run", "list", "--workflow", a.workflow, "--branch", a.ref, "--limit", "5", "--json", RUN_FIELDS)
            fresh = [x for x in runs if str(x.get("createdAt", "")) >= t0.strftime("%Y-%m-%dT%H:%M:%SZ")]
            if fresh:
                return max(x["databaseId"] for x in fresh)
        except (Exception, SystemExit) as e:  # not listed yet is retried: say why the listing failed
            err(f"dispatch find: the run list failed ({str(e)[:160]}); retried")
        # wall: waits on and times real processes and threads
        time.sleep(knob_dispatch_find_poll())
    return None