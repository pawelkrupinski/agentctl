# agentctl, part `cireport`: the CI report: ci_why's explanation of main's runs and who holds each red, and the CI-state snapshot's refresh
# Runs inside agentctl's one namespace, after the parts before it in lib/agentctl/__init__.py's PARTS (it uses their names).


def ci_why(o_r, tail=6, run_id=None, repo=None, summary=False, snap=None):
    """(red, text). run_id: explain that one run; else the gating workflows' newest decided runs on main, each red row
    followed by who holds it (`fixing: <its state, holder or offer>`; never NOBODY). summary: text is the status line's one line."""
    if run_id:
        rid, said = run_target(o_r, run_id)
        try:
            lines, _ = explain_run(o_r, {"databaseId": int(rid)}, tail, repo)
        except RuntimeError as e:
            if "HTTP 404" not in str(e):
                raise
            lines = [f"  no such run: {str(e)[:200]}"]
        return True, f"run {o_r}#{rid}\n" + "".join(f"  {l}\n" for l in said) + "\n".join(lines)
    out, red, rows, units_of, parts, untracked = [], False, [], {}, [], {}
    gate = gate_from_snap(snap) if snap else gate_runs(o_r)
    others, others_err = [], None
    if snap:
        others, others_err = outside_from_snap(snap), snap.get("outside_error")
    elif gated_repo(o_r):
        try:
            others = outside_gate(o_r)
        except Exception as e:  # the gate's verdict stands without them
            others_err = e
    lrepo = scan_repo_for(o_r, repo)
    lstale = local_stale(lrepo)
    bsnap = snap or ci_state_load(o_r)  # the branch reds come from the snapshot only (the processor writes it)
    explained = {}
    for name, decided, newer, _streak in gate:
        if decided and decided.get("conclusion") in RED_CONCLUSIONS:
            if summary:  # THE ONE-LINE SUMMARY NEVER RE-READS A DECIDED RUN'S LOGS (2026-10-29: ci-runner's gate summary
                # read convergence #2473's failed log and five job logs, ~8 s each, every 45 s -- "gate summary: still
                # running after 20 s" at every tick). Its line shows no log line; the run's signatures are cached once
                # per run and classifier (run_sigs), which is all the red units below read.
                run_sigs(o_r, decided, name)
                continue
            lines, num, per_job = explain_run_jobs(o_r, decided, tail, repo)
            explained[name] = (lines, num)
            if not any("LOG UNREADABLE" in l for l in lines):
                save_run_sigs(o_r, decided, name, per_job)
    states, units, flakes_ = {}, [], []
    try:  # who holds each red: its item in the store (the tick acts; ci-why only renders)
        rows = [(n, d, _gate_lists.get(n, []), not gate_report_only(o_r, n, d)[0]) for n, d, _nw, _s in gate]
        units = gate_red_units(o_r, rows, [(n, r, _outside_lists.get(n, []), False) for n, r, _s, _c in others],
                               rereads=0 if summary else None)
        units += local_units(lrepo) + predict_units(o_r, bsnap)
        for u in units:
            units_of.setdefault((u["workflow"], u["run"].get("databaseId")), []).append(u)
        store = open_store()
        states = red_view_of_units(store, units)
        flakes_ = [it for it in red_active(store).values() if it["data"].get("red_kind") == "flake"]
        untracked = red_untracked_runs(store, o_r, units)
    except (Exception, SystemExit) as e:  # who is fixing is information: the gate's verdict stands without it
        err(f"red: could not tell who is fixing ({str(e)[:160]})")

    def fixing(name, run, indent="  "):
        st = states.get((name, run.get("databaseId")))
        return red_lines(st, indent), st

    def short(st):
        return st["short"] if st else "?"

    # A RUN ITS CALLERS PROVED FIXED is no red, though it stays its callable-only workflow's newest run on main for good
    proven = [o for o in others if (fixing(o[0], o[1])[1] or {}).get("state") == "verified"]
    others = [o for o in others if o not in proven and not red_run_retired(o_r, o[0], o[1])]
    never = [o for o in others if (o[0], o[1].get("databaseId")) in untracked]  # never green on main: untracked
    others = [o for o in others if o not in never]

    for name, decided, newer, (since, count) in gate:
        pending = f"; newer run {newer['databaseId']} (#{newer.get('number')}) {newer.get('status')} may clear it" if newer else ""
        nov = _gate_no_verdict.get(name)
        if nov and not summary:
            out.append(f"{name}: newest run {nov['databaseId']} #{nov.get('number')}: NO VERDICT (incomplete: only "
                       "`verdict complete` failed -- a takes row skipped, cancelled or missing; no red, no fixer); the "
                       "gate reads the newest complete run, and the next complete run decides")
        if not decided:
            out.append(f"{name}: no decided run on main")
            parts.append(f"{short_flow(name)} ?")
            continue
        if decided["conclusion"] == "success":
            out.append(f"{name}: GREEN (run {decided['databaseId']} #{decided.get('number')})")
            parts.append(f"{short_flow(name)} green")
            continue
        retired = red_run_retired(o_r, name, decided)
        if retired:  # its check is gone from main: no run can clear it, it gates nothing and no fixer is offered it
            out.append(f"{name}: RETIRED (run {decided['databaseId']} #{decided.get('number')}, {decided['conclusion']}): "
                       f"{retired} -- no red")
            parts.append(f"{short_flow(name)} retired")
            continue
        report, why = gate_report_only(o_r, name, decided)
        if report:  # red only in checks the registry calls unproven: it reports, it holds no push
            out.append(f"{name}: RED, REPORT ONLY (run {decided['databaseId']} #{decided.get('number')}, "
                       f"{decided['conclusion']}): {why} ({GATING_CHECKS_FILE}) -- holds no push")
            out += fixing(name, decided)[0]
            parts.append(f"{short_flow(name)} report-only")
            continue
        red = True
        streak = f"; red for {red_for(since)} ({count} failed run{'s' if count != 1 else ''})" if count else ""
        out.append(f"{name}: RED (run {decided['databaseId']} #{decided.get('number')}, {decided['conclusion']}{streak}){pending}")
        flines, finfos = fixing(name, decided)
        out += flines
        if summary:
            parts.append(f"{short_flow(name)} #{decided.get('number')} \u2192 {short(finfos)}")
            continue
        lines, num = explained.get(name) or explain_run(o_r, decided, tail, repo)
        out += lines
        out.append(f"  clears when: a later {name} run on main is green, or every pushed commit touching a production path "
                   f"carries `Fixes-Red: {num}` (or `Fixes-Red: {decided['databaseId']}`) or names another run of this red episode "
                   f"failing the same way; tools-only commits land while red")
        parts.append(f"{short_flow(name)} #{decided.get('number')} \u2192 {short(finfos)}")
    lus = local_units(lrepo)
    local = [u for u in units_of.get((LOCAL_FLOW, (lus or [{"run": {}}])[0]["run"].get("databaseId")), [])]
    if lstale:
        out.append(f"{LOCAL_FLOW}: stale, rechecking (recorded on {str(local_red().get('main'))[:10]}; "
                   f"{remote()}/main is {lstale[:10]} now)")
        parts.append("local \u2192 stale, rechecking")
    if local:
        lr = local_red()
        main = str(lr.get("main"))[:10]
        out.append(f"{LOCAL_FLOW}: RED on plain {remote()}/main {main} ({', '.join(u['job'] for u in local)}: pre-push fails "
                   f"there without any change) -- claim: `agentctl red claim local`")
        flines, finfos = fixing(LOCAL_FLOW, local[0]["run"])
        out += flines
        for u in local:
            out.append(f"  [{u['job']}] {u['line'][:160]}")
        parts.append(f"local checks \u2192 {short(finfos)}")
    if flakes_:
        out.append("Flaky (passed on the next run with no fix; a red until a fix lands and a run proves it):")
        for it in flakes_:
            out.append(f"  {red_what(it['data'])}: {str(it['data'].get('line'))[:120]}")
            out.append(f"    fixing: {red_view(open_store(), it)[0]}")
    head = f"ci-why {o_r}: " + ("main is RED for the gate" if red else "main is green for the gate")
    if others_err is not None:
        out.append(f"outside the gate: could not list main's runs ({others_err})")
    if others:
        out.append(f"Outside the gate (red, not blocking a push; oldest first):")
        for name, r, since, count in others:
            out.append(f"  {name}: RED for {red_for(since)} since {since} ({count} failed run{'s' if count != 1 else ''}; "
                       f"newest run {r['databaseId']} #{r.get('number')}) -- `agentctl ci-why {r['databaseId']}`")
            out += fixing(name, r, "    ")[0]
    if never:
        out.append("Untracked (never green on main: no red, no fixer, no alert until its first green run):")
        for name, r, _since, _count in never:
            out.append(f"  {name}: newest run {r['databaseId']} #{r.get('number')}: "
                       f"{untracked[(name, r.get('databaseId'))]}")
    if proven:
        out.append("Proven fixed outside the gate (no red; it runs only when called, so its newest run on main stays red):")
        for name, r, _since, _count in proven:
            out.append(f"  {name}: newest run {r['databaseId']} #{r.get('number')} -- `agentctl ci-why {r['databaseId']}`")
            out += fixing(name, r, "    ")[0]
    blines = branch_lines(bsnap)
    if blines:
        out += blines
        for u in units_of.values():
            for x in u:
                if x.get("predicts"):
                    out += fixing(x["workflow"], x["run"], "    ")[0]
                    break
    if summary:
        line = "gate RED" if red else "gate green"
        if red:
            line += " (" + "; ".join(p_ for p_ in parts if not p_.startswith("local")) + ")"
        reds_, causes = [], {}
        for name, r, _since, _count in others:
            st = fixing(name, r)[1]
            cause = (st or {}).get("cause") if (st or {}).get("state") == "blocked-external" else None
            if cause and cause not in (DECISION_CAUSE, CRASH_LOOP_CAUSE):  # ONE line per outage, naming the reds it
                # blocks; a decision or the fixers' crash loop is no outage: its red reads WAITING ON YOU: <question>
                causes.setdefault(cause, (st.get("until"), [], (st.get("ask"), st.get("ask_how"))))[1].append(name)
                continue
            reds_.append(f"{name} #{r.get('number')} \u2192 {short(st)}")
        reds_ = [f"{c} down \u2192 blocks: {', '.join(ns)}" + (f" (auto-clears when {u_})" if u_ else "")
                 + (f" -- WAITING ON YOU: {trim_words(q_[0])} -- {q_[1]}" if q_ and q_[0] else "") for c, (u_, ns, q_) in causes.items()] + reds_
        reds_ += [p_ for p_ in parts if p_.startswith("local")]
        if len(reds_) > SUMMARY_MAX_REDS:
            reds_ = reds_[:SUMMARY_MAX_REDS] + [f"+{len(reds_) - SUMMARY_MAX_REDS} more"]
        if reds_:
            line += " \u00b7 reds: " + "; ".join(reds_)
        bsum = branch_summary(bsnap)
        if not reds_ and not bsum and not red and others_err is None:
            line += " \u00b7 no reds"
        if bsum:
            line += " \u00b7 " + bsum
        return red, line
    return red, "\n".join([head] + out)


def ci_state_refresh(o_r, write=True):
    """Read the gate and every other workflow on main once, write the snapshot (unless write=False: ci_state_write
    later), return it. Raises when the gate cannot be read (nothing is written: the old snapshot ages out)."""
    gate = gate_runs(o_r)
    snap = {"at": time.time(), "repo": o_r, "outside_error": None, "outside": [],
            "gate": [{"name": n, "decided": d, "newer": nw, "since": st[0], "count": st[1],
                      "runs": _gate_lists.get(n, []), "no_verdict": _gate_no_verdict.get(n)} for n, d, nw, st in gate]}
    if gated_repo(o_r):
        try:
            snap["outside"] = [{"name": n, "decided": r, "since": since, "count": count,
                                "runs": _outside_lists.get(n, [])} for n, r, since, count in outside_gate(o_r)]
        except Exception as e:
            snap["outside_error"] = str(e)[:200]
        snap["callable_only"] = sorted(main_callable_flows(o_r))  # their reds wait for red_callable_evidence
    try:  # the branch runs: measure/*, check/*, red fixers' and every recorded dispatch's (after the main lists)
        prev = (state_read().get("ci") or {}).get("branches") or []
        snap["branches"] = branch_state(o_r, scan_repo_for(o_r), prev)
    except (Exception, SystemExit) as e:
        snap["branches_error"] = str(e)[:200]
    latest = {}  # each workflow's newest decided run on main: what workflow_green probes read
    for name, runs in list(_outside_lists.items()) + list(_gate_lists.items()):
        d = next((r for r in runs if r.get("status") == "completed" and not is_cancelled(r)
                  and not (name in NO_VERDICT_FLOWS and run_no_verdict(o_r, r))), None)
        if d:
            latest[name] = {k: d.get(k) for k in ("number", "databaseId", "conclusion", "headSha", "updatedAt")}
    snap["latest"] = latest
    snap["stuck"] = stuck_runs({**_outside_lists, **_gate_lists}, o_r=o_r)  # audit #8: undecided far past their workflow's p90
    if write:
        ci_state_write(snap)
    return snap


def maybe_publish_gate(repo):
    """The processor's loop, at most every GATE_PUBLISH_INTERVAL s: read the gate (it holds the token) and publish the
    summary line for the status line. Never fatal; a failed read leaves the old file to age out (fail-closed)."""
    try:  # a push to main since the last publish (mark_gate_dirty): publish now, not at the 45 s mark
        marked = os.path.getmtime(gate_dirty_path()) > _last_gate_publish[0]
    except OSError:
        marked = False
    if not marked and time.time() - _last_gate_publish[0] < env_num("AGENTCTL_GATE_PUBLISH", 45):
        return
    _last_gate_publish[0] = time.time()

    def work():
        try:
            o_r = slug(repo)
            snap = ci_state_refresh(o_r, write=False)  # ONE read of GitHub per cycle; everything below reads it
            refresh_pinned_recording(o_r, repo)  # what PREDICTS MAIN RED compares a branch run's recording with
            ci_state_write(snap)
            # THE RED TICK ON THIS SNAPSHOT before its line is published: every red it shows is an item, offered or
            # held (never NOBODY). Bounded: a dispatch's worktree checkout finishes on the tick's own thread
            if not run_bounded("red-tick", lambda: red_tick_repo(repo, snap), env_num("AGENTCTL_ASSIGN_BOUND", 20)):
                err("red tick: still running; the gate line is published meanwhile")
            _red, line = ci_why(o_r, summary=True, snap=snap, repo=repo)
            publish_gate_summary(o_r, line.splitlines()[-1], epoch=snap["at"])
        except (Exception, SystemExit) as e:
            err(f"gate summary: skipped ({str(e).replace('agentctl: ', '')[:200]})")
            return
        try:
            for text in settle_branch_reds(o_r, snap):
                err(f"branch reds: {text}")
        except (Exception, SystemExit) as e:
            err(f"branch reds: not settled ({str(e).replace('agentctl: ', '')[:200]})")
        try:
            for routed in route_branch_reds(o_r, snap):
                err(f"branch reds: {routed}")
        except (Exception, SystemExit) as e:
            err(f"branch reds: not routed ({str(e).replace('agentctl: ', '')[:200]})")
    if not run_bounded("gate-publish", work, env_num("AGENTCTL_STEP_BOUND", 120)):
        err("gate summary: still reading from an earlier cycle (the loop goes on)")


def cmd_ci_why(a):
    o_r = slug(a.repo) if a.repo else default_repo()
    target = None if a.target in (None, "main") else a.target.lstrip("#")
    if target is not None and not target.isdigit():
        raise SystemExit("agentctl: ci-why takes a run id, a run number (#N) or `main`")
    if getattr(a, "cached", False):
        if not getattr(a, "summary", False) or target:
            raise SystemExit("agentctl: ci-why --cached goes with --summary and main")
        # NEVER LIVE WORK (2026-10-28: a stale file sent the status line's 15 s refresh into a 60 s live read): the
        # processor's published line, or -- stale or missing -- what it last said, marked with its age. Live reads
        # belong to the processor's publish step only.
        text = status_gate_text(state_read(), time.time(), o_r)  # the status model's gate text, for this repo
        print(text)
        return 1 if text.startswith("gate RED") else 0 if GATE_LINE.match(text) else 2  # 2: UNREAD / ? / NETWORK
    red, text = ci_why(o_r, a.tail, target, summary=getattr(a, "summary", False))
    print(text)
    return 1 if red else 0
