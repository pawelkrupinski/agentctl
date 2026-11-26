# agentctl, part `redopen`: opening a red and what it reads: opening, attaching, offering, its author, history, culprit search and diagnosis
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import os
from typing import Any
from agentctl.core import SHORT, attempt, env_num, err, fmt_age, git, remote, tilde
from agentctl.timefmt import iso_at, parse_time, ts
from agentctl.statefile import read_record
from agentctl.repos import RED_PRIMARY
from agentctl.allowedpaths import allowed_path
from agentctl.redrepos import red_gate_flows, red_is_other
from agentctl.redfamilies import red_identity
from agentctl.ancestry import contains_local
from agentctl.ciwhy import resolve_run
from agentctl.runsigs import classifier_version, run_sigs
from agentctl.statemachines import StaleVersion
from agentctl.redrecord import RED_JOIN_IDLE, red_active, red_names, red_what
from agentctl.leases import lease_meta
from agentctl.owners import owner_record_path
from agentctl.redask import red_decision
from agentctl.red import RED_OFFER, decided_revert, red_obs_for, red_tier
from agentctl.redmove import red_keep_holder, red_move, red_offer_class, red_put_offer, tail_lines
from agentctl.rundispatch import red_verify_dispatch
from agentctl.blameopen import BLAME_MAX, blame_open, culprit_of
from agentctl.redseen import red_obs_of_units, red_units, sigs_no_verdict
from agentctl.redauthors import red_authors
from agentctl.outsideflows import main_flow_meta


def _offer_deadline(store: Any, d: dict[str, Any], tier: str) -> Any:
    """Seconds an offer of this tier waits for a taker."""
    deadline = RED_OFFER["author"] if tier == "author" else RED_OFFER[red_offer_class(d)]
    if tier == "blame":  # waits for its culprit, at most BLAME_MAX: never escalated UNCLAIMED meanwhile
        return max(60, float((d.get("blame") or {}).get("since") or 0) + env_num("AGENTCTL_BLAME_MAX", BLAME_MAX)
                   - store.clock() + 60)
    return deadline


def _author_verdict(d: dict[str, Any], deadline: Any) -> str:
    """The line addressed to the change's recorded author (the projector writes it to their inbox)."""
    a = d["author"]
    return (f"RED AUTHOR: {red_what(d)} contains your {', '.join(s[:9] for s in a.get('shas') or [])} "
            f"({tilde(str(a.get('worktree')))}): offered to you for {deadline // 60} min -- "
            f"`~/bin/agentctl red claim {d['key']} --worktree .` and fix it "
            f"(`Fixes-Red: {(d.get('last_run') or {}).get('number')}`); else a fixer takes it")


def _deep_offer(d: dict[str, Any], sev: dict[str, Any], verdict: str | None, escalate: bool,
                data: dict[str, Any] | None) -> tuple[dict[str, Any], str | None, dict[str, Any] | None]:
    """(severity, verdict, data) of an offer at the deep tier: an escalation, unless the owner decided the red or this
    attempt was already told (ESCALATED ONCE PER ATTEMPT: a re-offer with no new attempt -- a lease lost to infra, a red
    back from a block -- is no new escalation; 2026-11-02, #728: 58 RED ESCALATE in a day)."""
    if not verdict and not escalate:
        if red_decision(d) or d.get("deep_told") == int(d.get("attempts") or 0):
            return {"severity": "info"}, verdict, data
    verdict = verdict or f"RED ESCALATE: {red_what(d)} still red after {d.get('attempts')} attempts: a deep investigation is offered"
    return sev, verdict, {**(data or {}), "deep_told": int(d.get("attempts") or 0)}


def red_offer(store, it, text, data=None, repo=None, escalate=False, verdict=None, tail=None, quiet=False):
    """Into `offered`, with its tier from the ladder, its deadline and its offer (the picture a taker gets). quiet: a
    re-offer that is no escalation (its culprit decided: blame.py)."""
    d = {**it["data"], **(data or {})}
    tier = red_tier(d, d.get("culprit"), store.clock())
    if tier == "revert" and not d.get("culprit"):  # the owner's decided revert (decided_revert): its commit is the culprit
        d["culprit"] = decided_revert(d, store.clock())
        data = {**(data or {}), "culprit": d["culprit"]}
    deadline = _offer_deadline(store, d, tier)
    sev = {"severity": "escalate"} if escalate or tier == "deep" else {"severity": "info"} if quiet else {}
    aud = {"audience": {k: v for k, v in (d.get("author") or {}).items() if k in ("worktree", "session")}} \
        if tier == "author" else {}
    if tier == "author":
        verdict = _author_verdict(d, deadline)
    elif tier == "deep":
        sev, verdict, data = _deep_offer(d, sev, verdict, escalate, data)
    if verdict:
        sev["refs"] = {"key": d.get("key"), "run": (d.get("last_run") or {}).get("number"), "workflow": d.get("workflow"),
                       "verdict": verdict, "tail": tail or [d.get("line") or ""], "command": "red"}
    it = red_move(store, it, "offered", text, data={**(data or {}), "tier": tier}, deadline_s=deadline, **sev, **aud)
    red_put_offer(store, it)
    return it


def red_family_tell(row, pic, fam):
    """Tell the family's fixer a red was added to its lease: a line in RED-INBOX.md in its worktree."""
    wt = lease_meta(row).get("worktree")
    if not wt or not os.path.isdir(wt) or not allowed_path(wt):
        return False
    with open(os.path.join(wt, "RED-INBOX.md"), "a") as f:
        f.write(family_tell_text(pic, fam))
    return True


def family_tell_text(pic: Any, fam: Any) -> str:
    """The RED-INBOX.md line telling a family's fixer red `pic` is its too."""
    jobs = ", ".join(str(j) for j in pic.get("jobs") or [pic.get("job")] if j)
    return (f"{ts()} RED FAMILY {fam}: {pic.get('workflow')} #{(pic.get('run') or {}).get('number')} [{jobs}] "
                f"(red {pic.get('key')}) is YOURS too, held under your lease: {str(pic.get('line') or '')[:300]} -- "
                f"`agentctl ci-why {(pic.get('run') or {}).get('databaseId')}`; land its fix with `Fixes-Red: "
                f"{(pic.get('run') or {}).get('number')}`. Show progress on it within "
                f"{fmt_age(env_num('AGENTCTL_RED_JOIN_IDLE', RED_JOIN_IDLE))} (a commit carrying that trailer, or "
                f"`agentctl red claim {(pic.get('run') or {}).get('number')} --note \"<what>\"`), or it goes to a fixer "
                f"of its own.\n")


def red_open(store, ob, repo, t, o_r=None):
    """A new red item (state red) from its first observation; its author recorded from the change that landed it. o_r:
    its repo (red_repo_key already in ob's key); the primary's reds carry none."""
    key = ob["key"]
    iid, n = key, 1
    while store.item("red", iid) is not None:  # an earlier spell of the same red is terminal: a new item
        n += 1
        iid = f"{key}.{n}"
    data = {"key": key, "workflow": ob["workflow"], "jobs": ob["jobs"], "job": ob["job"], "kind": ob["kind"],
            "line": ob["line"], "red_kind": ob["red_kind"], "gate": ob["gate"], "predicts": ob.get("predicts"),
            "runs": sorted(ob["names"]), "last_run": ob["run"], "opened_at": t, "attempts": 0, "history": [],
            "classifier": classifier_version()}
    if o_r and red_is_other(o_r):
        data["repo"] = o_r
    data.update(red_identity(data))
    if ob["kind"] == "test" and ob["red_kind"] in ("gate", "outside"):
        try:
            data["author"] = red_author(repo, ob, t)
        except (Exception, SystemExit) as e:
            err(f"red: no author for {key} ({str(e)[:120]})")
    data.update(red_culprit_search(store, ob, repo, t, o_r))
    return store.put_item("red", iid, "red", data)


def red_culprit_search(store, ob, repo, t, o_r):
    """{culprit, blame} of a red opened from observation ob: THE CULPRIT (blame.py) searched from a completed run's
    verdict -- a gate test red only. An EARLY red (redearly.py: its run still in progress) gets none: it carries its
    `early`, and early_settle opens the search when its run completes failing."""
    if ob.get("early"):
        return {"early": ob["early"], "culprit": None, "blame": None}
    out = {}
    try:
        if ob["kind"] == "test" and ob["red_kind"] in ("gate", "outside"):
            out["culprit"] = culprit_of(repo, {"runs": ob["runs_list"]})
    except (Exception, SystemExit):
        pass
    try:
        out["blame"] = blame_open(store, repo, o_r or RED_PRIMARY, ob, t)
    except (Exception, SystemExit) as e:
        out["blame"] = {"state": "none", "why": f"its range could not be read ({str(e)[:120]})"}
    return out


def red_attach_obs(store, it, ob):
    """A newer run failing the same way: attached to the red (its runs, its newest run)."""
    d = it["data"]
    runs = sorted(set(d.get("runs") or []) | set(ob["names"]))
    newer = int(ob["run"].get("databaseId") or 0) > int((d.get("last_run") or {}).get("databaseId") or 0)
    cv = classifier_version()
    if runs == d.get("runs") and not newer and sorted(ob["jobs"]) == sorted(d.get("jobs") or []) \
            and d.get("classifier") == cv:
        return it
    # THE JOBS FAILING NOW (issue 20261104T040527-1b4212: convergence #834's prompt named the German leg, which failed
    # in #818 and passed in #834): a newer run's failing jobs replace the red's; every job it was ever seen failing stays
    # in jobs_seen, which the re-keyed merge matches on
    seen = sorted(set(d.get("jobs_seen") or d.get("jobs") or []) | set(ob["jobs"]))
    upd = {"runs": runs, "jobs": sorted(ob["jobs"]) if newer else sorted(set(d.get("jobs") or []) | set(ob["jobs"])),
           "jobs_seen": seen, "classifier": cv}  # observed under this classifier's key
    if newer:
        upd["last_run"] = ob["run"]
    for attempt in range(5):  # a ticking writer moved it (StaleVersion): re-read and re-apply, never lose the attach --
        try:  # an unattached newer run reads as "no longer this failure" to a block's probe and discards its question
            store.update_item("red", it["id"], it["version"], upd)
            break
        except StaleVersion:
            if attempt == 4:
                raise
            it = store.item("red", it["id"])
    return store.item("red", it["id"])


def stamp_author(store: Any, it: Any, author: Any, run: Any) -> Any:
    """The red item `it`, its author recorded from the run's head when it is a red one without any."""
    if not (author and it["state"] == "red" and not it["data"].get("author")):
        return it
    store.update_item("red", it["id"], it["version"], {"author": dict(author, shas=[run.get("headSha")])})
    return store.item("red", it["id"])


def red_attach(store, workflow, run, author=None, o_r=None):
    """WATCH RED (the landed watch): the run's failures opened or attached, its author offered first. run: the run (a
    dict), or its number."""
    o_r = o_r or SHORT["movies"]
    if not isinstance(run, dict):
        _name, run = resolve_run(o_r, run)
        if run is None:
            return []
    sigs: list[Any] = attempt(lambda: run_sigs(o_r, run, workflow), [], catch=(Exception, SystemExit))
    if sigs_no_verdict(sigs):
        return []  # an incomplete run: no verdict, no red to open
    gate = workflow in {g for g, _ in red_gate_flows(o_r)}
    obs = red_obs_for(o_r, red_obs_of_units(red_units(o_r, workflow, run, [run], sigs, gate)))
    active, out = red_active(store), []
    for key, ob in obs.items():
        it = active.get(key) or red_open(store, ob, None, store.clock(), o_r)
        it = red_attach_obs(store, it, ob)
        out.append(stamp_author(store, it, author, run))
    return out


def red_author(repo, ob, t):
    """The live author of the red run's newest commit, from the change that landed it (its recorded session and
    worktree; audit #15: never an inherited environment), live = an agent process in that worktree, active lately."""
    u = {"run": ob["run"], "runs": ob["runs_list"]}
    for a in red_authors(repo, u, t):
        if a["live"] and a.get("session"):  # a change item's recorded author, never a worktree that merely holds the commit
            return {"worktree": a["worktree"], "session": a.get("session"), "shas": a["shas"][:5]}
    return None


# -- the red step: one item, one small idempotent step per tick
# DIAGNOSTICS ARE NO FIX (the owner, 2026-11-21: a853b2ee4 said `Fixes-Red: 94` and only added diagnostics). A commit
# that only collects evidence says `Diagnoses-Red: <run>`: it never links its red as a fix (no fix-pending, its fixer
# kept). Once it lands, its red's workflow -- when dispatchable (workflow_dispatch, no `verify_dispatch no` row) -- is
# dispatched on main to collect the new evidence, once per change, and the run's verdict goes back to the same holder
# (RED-NOTE.md in its worktree), never to a new fixer.
def _dispatch_diagnoses(landed: list[Any], done: dict[str, Any], d: dict[str, Any], row: Any, repo: Any, o_r: Any,
                        t: float) -> list[str]:
    """Dispatch each landed diagnosing change not yet in `done` once (recorded in it), the holder told."""
    did = []
    for c in landed:
        if c["id"] in done:
            continue
        sha = (c.get("data") or {}).get("landed_sha") or (c.get("data") or {}).get("tip") or ""
        line = red_verify_dispatch(o_r, repo, d.get("workflow"), sha, (d.get("last_run") or {}).get("databaseId"))
        done[c["id"]] = {"at": t, "dispatch": line}
        red_keep_holder(row, f"your diagnostics {c['id']} landed: {line}; its verdict comes here")
        did.append(f"{red_what(d)}: diagnostics {c['id']}: {line}")
    return did


def _awaits_verdict(rec: dict[str, Any], last: dict[str, Any]) -> bool:
    """A dispatched, untold diagnosis whose workflow has a decided run since the dispatch."""
    if rec.get("told") or not last.get("updatedAt") or not str(rec.get("dispatch") or "").startswith("dispatched"):
        return False
    return bool(parse_time(last["updatedAt"]).timestamp() > float(rec["at"]) and last.get("status", "completed") == "completed")


def _tell_verdicts(done: dict[str, Any], d: dict[str, Any], row: Any, snap: Any, t: float) -> list[str]:
    """Tell the holder the first decided run after each diagnosis' dispatch (marking the diagnosis told)."""
    last = ((snap or {}).get("latest") or {}).get(d.get("workflow")) or {}
    did = []
    for cid, rec in done.items():
        if not _awaits_verdict(rec, last):
            continue
        text = (f"{d.get('workflow')} #{last.get('number')} after your diagnostics {cid}: "
                f"{last.get('conclusion') or 'decided'} (run {last.get('databaseId')})")
        red_keep_holder(row, text)
        rec["told"] = t
        did.append(f"{red_what(d)}: {text}")
    return did


def red_diagnosis_step(store, it, row, snap, repo, o_r, t):
    """A leased red's landed `Diagnoses-Red:` changes: each dispatched once to collect its evidence, and the first
    decided run after that dispatch told to the holder. The lines it did."""
    d = it["data"]
    names = {str(x) for x in red_names(d) if x is not None}
    done = dict(d.get("diagnosis_runs") or {})
    try:
        landed = [c for c in store.items("change", ["landed"]) if names & {str(x) for x in (c.get("data") or {}).get(
            "diagnoses") or []} and float(c.get("entered_at") or 0) >= float(d.get("opened_at") or 0) - 3600]
    except Exception:  # an unread store decides nothing
        return []
    did = _dispatch_diagnoses(landed, done, d, row, repo, o_r, t) + _tell_verdicts(done, d, row, snap, t)
    if did:
        store.update_item("red", it["id"], it["version"], {"diagnosis_runs": done})
    return did


def lease_worktree_and_log(m: Any) -> tuple[Any, Any]:
    """(worktree, log) of a lease's meta m; a family owner's, from its record when the lease names none."""
    wt, log = m.get("worktree"), m.get("log")
    if m.get("kind") == "owned" and m.get("family"):  # a family owner's: its worktree and its wrapper's log (stderr)
        rec = read_record(owner_record_path(m["family"])) or {}
        wt, log = wt or rec.get("worktree"), log or rec.get("log")
    return wt, log


def red_history(row, t):
    m = lease_meta(row)
    wt, log = lease_worktree_and_log(m)
    notes = m.get("notes") or m.get("note") or ""
    if not notes and log and not str(log).endswith(".jsonl"):  # a lapse says nothing: its wrapper's last words do
        notes = "\n".join(tail_lines(log, 4))
    return {"at": iso_at(t), "holder": row.get("holder") if row else None, "outcome": m.get("outcome") or "lease expired",
            "notes": notes[:600], "session": m.get("session"),
            "worktree": wt, "log": log, "dispatched": m.get("kind") in ("fixer", "owned")}


def red_callable_evidence(d, snap, repo, o_r):
    """For an outside red of a workflow that on main runs only when called (snap's callable_only): the line proving it
    fixed, else None. Proof is BOTH: its file changed on main since the failing run (the first such commit C), and a
    caller's newest decided run on main that contains C passed -- the forge expands the called file into the caller's
    run, so a green one says the forge parses and runs the new file. A red caller vetoes nothing: it fails for reasons of
    its own (Report run timings has 36 callers, some red at any hour; a "none failed" rule never let #38 clear).
    "Report run timings" #38 (2026-10-19, a `workflow_call.secrets` block Forgejo 15 rejected; 32d1805de fixed it that
    night) stayed red for 10 days: no run of its own could come to clear it. Never closed on the trigger alone:
    5cf3f69's 811 re-opened retirements."""
    if not repo or d.get("red_kind") != "outside" or d.get("workflow") not in ((snap or {}).get("callable_only") or ()):
        return None
    run = d.get("last_run") or {}
    main = f"refs/remotes/{remote()}/main"
    meta = main_flow_meta(o_r)
    path = meta["paths"].get(d.get("workflow"))
    if not path or not run.get("headSha"):
        return None
    log = git(repo, "log", "--format=%H", f"{run['headSha']}..{main}", "--", path, check=False)
    changes = log.stdout.split() if log.returncode == 0 else []
    if not changes:
        return None
    first = changes[-1]
    latest = (snap or {}).get("latest") or {}
    passed = sorted(f"{name} #{last.get('number')}" for name, last in
                    ((n, latest.get(n) or {}) for n in meta["callers"].get(os.path.basename(path), []))
                    if last.get("conclusion") == "success" and contains_local(repo, first, last.get("headSha")))
    if not passed:
        return None
    return (f"{d.get('workflow')} runs only when called: its file changed on main since #{run.get('number')} "
            f"({first[:10]}), and its callers' runs since passed: {', '.join(passed[:3])}"
            + (f" and {len(passed) - 3} more" if len(passed) > 3 else ""))
