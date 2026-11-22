# agentctl, part `redmove`: moving a red: its transitions, offers, families, opening, attaching, holders, history and closing.
# Runs inside agentctl's one namespace, after the parts before it in lib/agentctl/__init__.py's PARTS (it uses their names).

# -- moving a red
def red_event(it, text, **kw):
    d = it["data"]
    ev = {"text": text, "refs": {"key": d.get("key"), "run": (d.get("last_run") or {}).get("number"),
                                 "workflow": d.get("workflow")}}
    ev.update(kw)
    return ev


# A FIX'S PROGRESS IS NO ESCALATION (2026-11-19, Record scrape fixtures #198): "fix ... landed (123dd6524e): verifying"
# is a fix-pending self-move, and the store raises a self-move's level unless its event says `info` -- so the landing
# of its fix showed the red "escalated" on the owner's board. A fix-pending self-move is quiet unless it says
# severity="escalate" (an unverified fix past its deadline, an owner push still waiting).
def red_move(store, it, to, text, data=None, deadline_s=None, level=None, **ev):
    if to == "fix-pending" and it.get("state") == "fix-pending":
        ev.setdefault("severity", "info")
    kw = {"level": level} if level is not None else {}  # only when said: an older store server takes no `level`
    v = store.transition("red", it["id"], to, it["version"], data=data, event=red_event(it, text, **ev),
                         deadline_s=deadline_s, **kw)
    return store.item("red", it["id"]) if v else None


def red_what(d):
    jobs = [str(j) for j in d.get("jobs") or [d.get("job")] if j]
    o_r = red_repo_of(d)
    return ("" if o_r == RED_PRIMARY else f"{red_repo_short(o_r)} ") + f"{d.get('workflow')} #{(d.get('last_run') or {}).get('number')}" + (f" [{', '.join(jobs)}]" if jobs else "")


def red_offer_class(d):
    return "flake" if d.get("red_kind") == "flake" else "gate" if d.get("gate") else "other"


def red_offer(store, it, text, data=None, repo=None, escalate=False, verdict=None, tail=None, quiet=False):
    """Into `offered`, with its tier from the ladder, its deadline and its offer (the picture a taker gets). quiet: a
    re-offer that is no escalation (its culprit decided: blame.py)."""
    d = {**it["data"], **(data or {})}
    tier = red_tier(d, d.get("culprit"), store.clock())
    if tier == "revert" and not d.get("culprit"):  # the owner's decided revert (decided_revert): its commit is the culprit
        d["culprit"] = decided_revert(d, store.clock())
        data = {**(data or {}), "culprit": d["culprit"]}
    deadline = RED_OFFER["author"] if tier == "author" else RED_OFFER[red_offer_class(d)]
    if tier == "blame":  # waits for its culprit, at most BLAME_MAX: never escalated UNCLAIMED meanwhile
        deadline = max(60, float((d.get("blame") or {}).get("since") or 0) + env_num("AGENTCTL_BLAME_MAX", BLAME_MAX)
                       - store.clock() + 60)
    sev = {"severity": "escalate"} if escalate or tier == "deep" else {"severity": "info"} if quiet else {}
    aud = {"audience": {k: v for k, v in (d.get("author") or {}).items() if k in ("worktree", "session")}} \
        if tier == "author" else {}
    if tier == "author":  # addressed to the change's recorded author (the projector writes it to their inbox)
        a = d["author"]
        verdict = (f"RED AUTHOR: {red_what(d)} contains your {', '.join(s[:9] for s in a.get('shas') or [])} "
                   f"({str(a.get('worktree')).replace(HOME, '~')}): offered to you for {deadline // 60} min -- "
                   f"`~/bin/agentctl red claim {d['key']} --worktree .` and fix it "
                   f"(`Fixes-Red: {(d.get('last_run') or {}).get('number')}`); else a fixer takes it")
    if tier == "deep" and not verdict and not escalate and red_decision(d):  # the owner decided it: no escalation
        sev = {"severity": "info"}
    elif tier == "deep" and not verdict and not escalate and d.get("deep_told") == int(d.get("attempts") or 0):
        sev = {"severity": "info"}  # ESCALATED ONCE PER ATTEMPT: a re-offer with no new attempt (a lease lost to infra,
        # a red back from a block) at the deep tier is no new escalation -- 2026-11-02, #728: 58 RED ESCALATE in a day
    elif tier == "deep":
        verdict = verdict or f"RED ESCALATE: {red_what(d)} still red after {d.get('attempts')} attempts: a deep investigation is offered"
        data = {**(data or {}), "deep_told": int(d.get("attempts") or 0)}
    if verdict:
        sev["refs"] = {"key": d.get("key"), "run": (d.get("last_run") or {}).get("number"), "workflow": d.get("workflow"),
                       "verdict": verdict, "tail": tail or [d.get("line") or ""], "command": "red"}
    it = red_move(store, it, "offered", text, data={**(data or {}), "tier": tier}, deadline_s=deadline, **sev, **aud)
    red_put_offer(store, it)
    return it


# RED FAMILIES (the owner, 2026-10-29: "give all convergence red work to one agent"): the reds of one family -- by
# workflow, or by a failing job's name (Verify scrape recording's `convergence (<cc>)` is convergence work too) -- go to
# ONE fixer. While it is alive, the dispatcher puts a new red of its family under its lease (a sibling it renews with
# its own) and tells it in RED-INBOX.md in its worktree, never a second fixer; when it ends, the next fixer of the
# family goes on in its worktree (previous_fixer_tree). The families are config: red-families.json beside agentctl
# ($AGENTCTL_RED_FAMILIES names another file). `"outside_gate": true` matches every red of a workflow the gate does
# not read (red_outside_gate; the owner, 2026-11-07: every non-gating red to ONE agent).
_families_cache = process_cache({})


def red_families():
    path = os.environ.get("AGENTCTL_RED_FAMILIES") or os.path.join(os.path.dirname(TOOL_PATH), "red-families.json")
    try:
        m = os.path.getmtime(path)
    except OSError:
        return []
    hit = _families_cache.get(path)
    if not hit or hit[0] != m:
        try:
            with open(path) as f:
                fams = json.load(f).get("families") or []
            fams = [dict(x, _jobs=[re.compile(j) for j in x.get("jobs") or []]) for x in fams if x.get("name")]
        except (OSError, ValueError, re.error) as e:
            err(f"red families: {path} unreadable ({e}): none apply")
            fams = []
        hit = _families_cache[path] = (m, fams)
    return hit[1]


def red_family(d):
    """The family of a red (its data, its picture, a fixer record: workflow, job, jobs), or None."""
    if not d:
        return None
    o_r = red_repo_of(d)
    if o_r != RED_PRIMARY:  # ANOTHER WATCHED REPO: its red-repos.json family, every red of it (red-families.json's
        return (red_repo_conf(o_r) or {}).get("family") or None  # families are the primary's)
    jobs = [str(j) for j in (d.get("jobs") or []) + [d.get("job")] if j]
    for fam in red_families():  # the first family that matches: its workflows, jobs or outside_gate (if it names any)
        named = bool(fam.get("workflows") or fam["_jobs"] or fam.get("outside_gate"))  # AND its kinds
        by_name = d.get("workflow") in (fam.get("workflows") or []) or any(rx.search(j) for rx in fam["_jobs"] for j in jobs) \
            or bool(fam.get("outside_gate") and red_outside_gate(d))
        by_kind = not fam.get("kinds") or d.get("kind") in fam["kinds"]
        if (by_name or not named) and by_kind and (named or fam.get("kinds")):
            return fam["name"]
    return None


def red_outside_gate(d):
    """A red of a workflow THE GATE does not read (the owner, 2026-11-07: "move all these to single agent"): the repo's
    gate is red_gate_flows (movies' gating checks registry: Main, Identity model convergence, Identity takes -- the set ci-why's "Outside the gate"
    leaves out); the local checks are the landing gate's own, never outside it. No workflow named: cannot tell, no."""
    wf = d.get("workflow")
    return bool(wf) and wf != LOCAL_FLOW and wf not in {n for n, _f in red_gate_flows(red_repo_of(d))}


def red_family_tell(row, pic, fam):
    """Tell the family's fixer a red was added to its lease: a line in RED-INBOX.md in its worktree."""
    wt = lease_meta(row).get("worktree")
    if not wt or not os.path.isdir(wt) or not allowed_path(wt):
        return False
    jobs = ", ".join(str(j) for j in pic.get("jobs") or [pic.get("job")] if j)
    with open(os.path.join(wt, "RED-INBOX.md"), "a") as f:
        f.write(f"{ts()} RED FAMILY {fam}: {pic.get('workflow')} #{(pic.get('run') or {}).get('number')} [{jobs}] "
                f"(red {pic.get('key')}) is YOURS too, held under your lease: {str(pic.get('line') or '')[:300]} -- "
                f"`agentctl ci-why {(pic.get('run') or {}).get('databaseId')}`; land its fix with `Fixes-Red: "
                f"{(pic.get('run') or {}).get('number')}`. Show progress on it within "
                f"{fmt_age(env_num('AGENTCTL_RED_JOIN_IDLE', RED_JOIN_IDLE))} (a commit carrying that trailer, or "
                f"`agentctl red claim {(pic.get('run') or {}).get('number')} --note \"<what>\"`), or it goes to a fixer "
                f"of its own.\n")
    return True


def red_picture(d):
    """What a taker gets: the failure, every prior attempt with its verdict, the refuted fixes, the diagnosis."""
    return {"key": d.get("key"), "workflow": d.get("workflow"), "job": d.get("job"), "jobs": d.get("jobs"),
            "kind": d.get("kind"), "line": d.get("line"), "run": d.get("last_run"), "gate": bool(d.get("gate")),
            "red_kind": d.get("red_kind"), "attempts": int(d.get("attempts") or 0), "history": (d.get("history") or [])[-6:],
            "refuted": d.get("refuted") or [], "diagnosis": d.get("diagnosis"), "predicts": d.get("predicts"),
            "author": d.get("author"), "culprit": d.get("culprit"), "names": sorted(red_names(d) - {None}),
            "prior_work": d.get("prior_work"), "old_note": d.get("old_note"), "repo": d.get("repo"),
            "blame": d.get("blame"), "lander_tried": d.get("lander_tried"), "decision": red_decision(d)}


def red_put_offer(store, it):
    d = it["data"]
    pri = RED_PRIORITY["flake"] if d.get("red_kind") == "flake" else RED_PRIORITY["gate"] if d.get("gate") else \
        RED_PRIORITY["outside"]
    return store.offer("fixer", f"red:{it['id']}", red_lease(d["key"]), d.get("tier") or "fresh",
                       int(d.get("priority", pri)), it["deadline"], red_picture(d))


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
        try:
            data["culprit"] = culprit_of(repo, {"runs": ob["runs_list"]})
        except (Exception, SystemExit):
            pass
    try:  # THE CULPRIT (blame.py): searched from the red's opening; a gate test red only
        data["blame"] = blame_open(store, repo, o_r or RED_PRIMARY, ob, t)
    except (Exception, SystemExit) as e:
        data["blame"] = {"state": "none", "why": f"its range could not be read ({str(e)[:120]})"}
    return store.put_item("red", iid, "red", data)


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


def red_attach(store, workflow, run, author=None, o_r=None):
    """WATCH RED (the landed watch): the run's failures opened or attached, its author offered first. run: the run (a
    dict), or its number."""
    o_r = o_r or SHORT["movies"]
    if not isinstance(run, dict):
        _name, run = resolve_run(o_r, run)
        if run is None:
            return []
    try:
        sigs = run_sigs(o_r, run, workflow)
    except (Exception, SystemExit):
        sigs = []
    if sigs_no_verdict(sigs):
        return []  # an incomplete run: no verdict, no red to open
    gate = workflow in {g for g, _ in red_gate_flows(o_r)}
    obs = red_obs_for(o_r, red_obs_of_units(red_units(o_r, workflow, run, [run], sigs, gate)))
    active, out = red_active(store), []
    for key, ob in obs.items():
        it = active.get(key) or red_open(store, ob, None, store.clock(), o_r)
        it = red_attach_obs(store, it, ob)
        if author and it["state"] == "red" and not it["data"].get("author"):
            store.update_item("red", it["id"], it["version"], {"author": dict(author, shas=[run.get("headSha")])})
            it = store.item("red", it["id"])
        out.append(it)
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
def red_diagnosis_step(store, it, row, snap, repo, o_r, t):
    """A leased red's landed `Diagnoses-Red:` changes: each dispatched once to collect its evidence, and the first
    decided run after that dispatch told to the holder. The lines it did."""
    d = it["data"]
    names = {str(x) for x in red_names(d) if x is not None}
    done = dict(d.get("diagnosis_runs") or {})
    did = []
    try:
        landed = [c for c in store.items("change", ["landed"]) if names & {str(x) for x in (c.get("data") or {}).get(
            "diagnoses") or []} and float(c.get("entered_at") or 0) >= float(d.get("opened_at") or 0) - 3600]
    except Exception:  # an unread store decides nothing
        return []
    for c in landed:
        if c["id"] in done:
            continue
        sha = (c.get("data") or {}).get("landed_sha") or (c.get("data") or {}).get("tip") or ""
        line = red_verify_dispatch(o_r, repo, d.get("workflow"), sha, (d.get("last_run") or {}).get("databaseId"))
        done[c["id"]] = {"at": t, "dispatch": line}
        red_keep_holder(row, f"your diagnostics {c['id']} landed: {line}; its verdict comes here")
        did.append(f"{red_what(d)}: diagnostics {c['id']}: {line}")
    last = ((snap or {}).get("latest") or {}).get(d.get("workflow")) or {}
    for cid, rec in done.items():
        if rec.get("told") or not last.get("updatedAt") or not str(rec.get("dispatch") or "").startswith("dispatched"):
            continue
        if parse_time(last["updatedAt"]).timestamp() > float(rec["at"]) and last.get("status", "completed") == "completed":
            text = (f"{d.get('workflow')} #{last.get('number')} after your diagnostics {cid}: "
                    f"{last.get('conclusion') or 'decided'} (run {last.get('databaseId')})")
            red_keep_holder(row, text)
            rec["told"] = t
            did.append(f"{red_what(d)}: {text}")
    if did:
        store.update_item("red", it["id"], it["version"], {"diagnosis_runs": done})
    return did


# A FIX WITH NO REPLAY KEEPS ITS FIXER (the owner, 2026-11-21: Order independence #94's `Fixes-Red: 94` commit
# a853b2ee4 was diagnostics only; the red went fix-pending, its fixer was released, and nobody worked it for a day). A
# fix whose commits carry no `Fix-Replay:` that agentctl ran and saw base=FAIL head=PASS (the change's `replays`) is
# UNPROVEN: its red goes fix-pending with `fix.unproven` -- shown as fix-unproven -- and its holder's lease is NOT
# revoked: the red stays in hand until its verification run decides it (green: FIX OK, the holder released; the same
# failure: FIX FAILED, back to that holder, leased). A gate red keeps its own enforcement (FIX REPLAY: refused
# without a replay, or its `none — <why>`): its fix is taken as proven, as before.
def red_fix_proven(store, d, fix):
    """Whether red data d's fix is proven before landing: a gate red's (its own enforcement), else a change whose
    `replays` holds one agentctl ran with base=FAIL head=PASS."""
    if d.get("gate"):
        return True
    ch = red_change(store, (fix or {}).get("entry")) or {}
    return any(r.get("base") == "FAIL" and r.get("head") == "PASS" for r in (ch.get("data") or {}).get("replays") or [])


def red_keep_holder(row, text):
    """Tell a red's live holder something, its lease kept (RED-NOTE.md in its worktree)."""
    wt = lease_meta(row).get("worktree")
    if wt and os.path.isdir(wt) and allowed_path(wt):
        try:
            with open(os.path.join(wt, "RED-NOTE.md"), "a") as f:
                f.write(f"{text} (agentctl red, {ts()}); remove this file with your worktree.\n")
        except OSError:
            pass


def red_stop_holder(store, key, why, outcome, force=False):
    """Revoke the red's lease (its holder's next renewal answers lost) and tell its worktree. A REVIVED LANDER (lease
    tier `lander`, blame.py) is spared unless force: the revert is the red's fix, and the lander's work -- its change
    re-applied with its fix -- goes on after it; only a refuted revert (the culprit was wrong) stops it."""
    row = store.get(red_lease(key))
    if not lease_is_live(row, store.clock()):
        return None
    if lease_meta(row).get("tier") == "lander" and not force:
        return None
    store.revoke(red_lease(key), outcome)
    if lease_meta(row).get("kind") == "owned":  # a family owner's: its next beat drops the lease; never a STOP in its
        return row["holder"]                    # one permanent worktree, which would stop every later turn too
    if any(lease_is_live(r, store.clock()) and r["holder"] == row["holder"] for r in store.list("red/")):
        return None  # it still holds other reds of its run: only this lease goes, never its work
    wt = lease_meta(row).get("worktree")
    if wt and os.path.isdir(wt) and allowed_path(wt):
        try:
            with open(os.path.join(wt, "RED-STOP.md"), "w") as f:
                f.write(f"STOP: {why} (agentctl red, {ts()}); remove this file with your worktree.\n")
        except OSError:
            pass
    return row["holder"]


def tail_lines(path, n):
    """The last n non-empty lines of a text file (its last 8 KiB), [] when unreadable."""
    try:
        with open(path, "rb") as f:
            f.seek(max(0, os.path.getsize(path) - 8192))
            return [l for l in f.read().decode(errors="replace").splitlines() if l.strip()][-n:]
    except (OSError, TypeError):
        return []


def red_history(row, t):
    m = lease_meta(row)
    wt, log = m.get("worktree"), m.get("log")
    if m.get("kind") == "owned" and m.get("family"):  # a family owner's: its worktree and its wrapper's log (stderr)
        rec = read_record(owner_record_path(m["family"])) or {}
        wt, log = wt or rec.get("worktree"), log or rec.get("log")
    notes = m.get("notes") or m.get("note") or ""
    if not notes and log and not str(log).endswith(".jsonl"):  # a lapse says nothing: its wrapper's last words do
        notes = "\n".join(tail_lines(log, 4))
    return {"at": iso_at(t), "holder": row.get("holder") if row else None, "outcome": m.get("outcome") or "lease expired",
            "notes": notes[:600], "session": m.get("session"),
            "worktree": wt, "log": log, "dispatched": m.get("kind") in ("fixer", "owned")}


def red_close(store, it, to, text):
    store.close_offer(f"red:{it['id']}", to)
    holder = red_stop_holder(store, it["data"]["key"], text, f"red {to}")
    line = f"RED {to.upper()}: {red_what(it['data'])}: {text}" + (f"; told to stop: {holder}" if holder else "")
    inbox_write("red", line, [line], [], 0)
    return red_move(store, it, to, text)


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


def red_settled_runs(store):
    """{(key, run id): its verified spell} for the runs red_callable_evidence settled. Such a run stays its workflow's
    newest on main for good (the workflow runs only when called): observed again, it opens no new spell, and it reads
    as verified, never "offering" (Report run timings #38 read "offering" in the status line after its FIX OK)."""
    return {(it["data"].get("key"), str(it["data"]["settled_run"])): it for it in store.items("red")
            if it["state"] == "verified" and it["data"].get("settled_run") is not None}
