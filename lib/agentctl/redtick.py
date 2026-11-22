# agentctl, part `redtick`: the tick's red step (the processor; ci-why never acts).
# Runs inside agentctl's one namespace, after the parts before it in lib/agentctl/__init__.py's PARTS (it uses their names).

# -- the tick's red step (the processor; ci-why never acts)
_red_lock = threading.Lock()
_last_red_tick = [0.0]


def red_tick(store, o_r, repo, snap, now_=None, dispatch=False, facts=None, sync=False, early=None):
    """One level-triggered pass: observe (unless the gate is unread: nothing opens or clears on an unknown; early reds
    too, redearly.py), each red one step (per item, inside try: one bad item never stops the tick), the claims renewed, the dispatcher. Returns what it
    did."""
    t = now_ if now_ is not None else store.clock()
    acts = []
    obs = None
    # THE PRIMARY'S TICK also runs the store-wide passes (the migration, the blocks, the invariant); another watched
    # repo's tick only observes, opens and steps ITS OWN reds -- the primary's observation never clears another repo's
    # red (it is simply absent from it), nor the other way round
    primary = not red_is_other(o_r)
    if snap:
        try:
            obs = red_obs_for(o_r, red_observe(o_r, repo, snap) if primary else red_observe_gate(o_r, snap))
        except (Exception, SystemExit) as e:
            err(f"red tick: could not observe ({str(e)[:160]}): nothing opens or clears this tick")
    obs = early_observed(store, o_r, snap, t, obs, early)  # EARLY REDS (redearly.py): failed jobs of running runs
    untracked = {}
    if obs is not None:  # A CHECK NEVER GREEN ON MAIN IS NOT TRACKED (reduntracked.py): set apart, listed for red status -v
        obs, untracked = red_split_untracked(store, o_r, obs, t)
        try:
            red_note_untracked(store, o_r, untracked)
        except (Exception, SystemExit) as e:
            err(f"red tick: could not note the untracked reds ({str(e)[:160]})")
    try:
        n = red_migrate(store) if primary else 0
        if n:
            acts.append(f"red model {RED_MODEL}: {n} open red(s) migrated in place")
    except (Exception, SystemExit) as e:
        err(f"red tick: the red model migration failed ({str(e)[:160]}); tried again next tick")
    if facts is None and primary:
        try:
            red_main_current(repo, snap)
        except (Exception, SystemExit) as e:
            err(f"red tick: could not bring main up to date ({str(e)[:160]})")
    facts = facts if facts is not None else red_fix_facts(repo, store)
    if primary:
        try:  # THE LANDER (lander.py): each landed change's commits on main, noted while its keys still find them
            acts += lander_note_landed(store, repo, o_r, t)
        except (Exception, SystemExit) as e:
            err(f"red tick: noting the landed changes' commits failed ({str(e)[:160]})")
    active, settled = red_active_of(store, o_r), None
    for key, ob in (obs or {}).items():
        try:
            it = active.get(key)
            if it:
                active[key] = red_attach_obs(store, early_attach(store, it, ob, repo, o_r, t, acts), ob)
                continue
            settled = red_settled_runs(store) if settled is None else settled
            if (key, str(ob["run"].get("databaseId"))) not in settled \
                    and not red_retired(o_r, {"red_kind": ob["red_kind"], "workflow": ob["workflow"],
                                              "last_run": ob["run"], "jobs": ob["jobs"]}, t):
                active[key] = early_opened(red_open(store, ob, repo, t, o_r), acts)  # a retired check's run opens no red
        except (Exception, SystemExit) as e:
            err(f"red tick: {key}: {str(e)[:160]}")
    try:
        acts += red_merge_rekeyed(store, active, obs)
    except (Exception, SystemExit) as e:
        err(f"red tick: merging re-keyed reds failed ({str(e)[:160]})")
    try:
        acts += red_renew_blocks(store, t) if primary else []
    except (Exception, SystemExit) as e:
        err(f"red tick: renewing the blocks failed ({str(e)[:160]})")
    try:
        acts += red_quota_waits(store, o_r, repo, t)
    except (Exception, SystemExit) as e:
        err(f"red tick: the quota waits failed ({str(e)[:160]})")
    for key, it in list(active.items()):
        try:
            why = red_active_untracked(store, o_r, it, untracked, obs, t)
            if why:
                red_untrack(store, it, why)
                acts.append(f"untracked {red_what(it['data'])}: {why}")
                continue
            acts += early_unobserved(store, it, obs, snap, repo, o_r, t)  # its run ended, or a rerun cleared it
            acts += red_step(store, it, obs, snap, facts, repo, o_r, t)
        except StaleVersion:
            pass  # another writer moved it: the next tick
        except (Exception, SystemExit) as e:
            err(f"red tick: {key}: {type(e).__name__}: {str(e)[:200]}")
    try:  # WAITING ON YOU: each red on the owner told once, then reminded (ask.py)
        acts += red_owner_reminders(store, t) if primary else []
    except (Exception, SystemExit) as e:
        err(f"red tick: the owner's reminders failed ({str(e)[:160]})")
    try:
        acts += red_enforce(store, facts, t) if primary else []
    except (Exception, SystemExit) as e:
        err(f"red tick: the invariant pass failed ({str(e)[:160]})")
    if sync or dispatch:  # the sync's half on the same store (claims renewed; with dispatch its dispatcher): the tests'
        try:  # one-process stand-in for the two hosts. In production the laptop's sync runs these (sync_steps)
            red_renew_holders(store, t)
            acts += red_reap_orphans(store, t)
        except (Exception, SystemExit) as e:
            err(f"red tick: renewing the claims failed ({str(e)[:160]})")
    if dispatch:  # fixers run on the laptop (owner decision 8): its sync dispatches
        try:
            acts += red_dispatcher(store, o_r, repo, t)
        except (Exception, SystemExit) as e:
            err(f"red tick: the dispatcher failed ({str(e)[:160]})")
    return acts


def red_tick_repo(repo, snap=None, dispatch=False):
    o_r = slug(repo)
    if not gated_repo(o_r):
        return []
    with _red_lock:
        _last_red_tick[0] = time.time()
        recheck_local(repo, wait=False)
        acts = red_tick(open_store(), o_r, repo, snap or ci_state_load(o_r), dispatch=dispatch)
    for line in acts:
        err(f"red: {line}")
    return acts


def red_open_flows(o_r, store=None):
    """The workflows of o_r's open reds that are not its gate's: their newest decided run on main must be read too, or
    such a red can never clear (core Dependabot Updates #154, 2026-11-02: green #155/#156 came, nothing read them)."""
    try:
        its = (store or open_store()).items("red")
    except (Exception, SystemExit):
        return []
    gate = {n for n, _f in red_gate_flows(o_r)}
    return sorted({it["data"].get("workflow") for it in its if it["state"] not in RED_TERMINAL
                   and it["data"].get("repo") == o_r and it["data"].get("workflow")} - gate)


def newest_decided_main(o_r, workflow):
    """The newest decided (success / failure / timed_out) run of a workflow on main, or None."""
    # listing: None here means none decided among the newest 10 -- the callers treat it as unread, not green
    runs = gh_json(o_r, "run", "list", "--workflow", workflow, "--branch", "main", "--limit", "10", "--json", RUN_FIELDS)
    return next((r for r in runs if r.get("status") == "completed"
                 and r.get("conclusion") in ("success", "failure", "timed_out")), None)


def red_gate_snap(o_r, store=None):
    """Another watched repo's snapshot: its gate workflows only (red-repos.json), read once. The shared run lists the
    primary's snapshot fills (_gate_lists, _outside_lists) are put back as they were: workflow names repeat across
    repos (movies has a `Stage NixOS closures` too)."""
    saved = (dict(_gate_lists), dict(_outside_lists), dict(_gate_no_verdict))
    try:
        gate = gate_runs(o_r, red_gate_flows(o_r))
        fields = ("number", "databaseId", "conclusion", "headSha", "updatedAt")
        latest = {n: {k: d.get(k) for k in fields} for n, d, _nw, _st in gate if d}
        for wf in red_open_flows(o_r, store):  # an open red's own workflow, outside the gate: read so it can clear
            try:
                r = newest_decided_main(o_r, wf)
            except (Exception, SystemExit):
                r = None
            if r:
                latest[wf] = {k: r.get(k) for k in fields}
        return {"at": time.time(), "repo": o_r, "outside": [], "outside_error": None,
                "gate": [{"name": n, "decided": d, "newer": nw, "since": st[0], "count": st[1],
                          "runs": _gate_lists.get(n, []), "no_verdict": _gate_no_verdict.get(n)} for n, d, nw, st in gate],
                "latest": latest}
    finally:
        for live, old in zip((_gate_lists, _outside_lists, _gate_no_verdict), saved):
            live.clear()
            live.update(old)


def red_observe_gate(o_r, snap):
    """{key: observation} of another watched repo: its gate workflows' red runs (no outside workflows, local checks,
    branch predictions or convergence kicks -- those are the primary's), and its stuck gate runs. The shared run lists
    are restored afterwards (red_gate_snap)."""
    saved = (dict(_gate_lists), dict(_outside_lists), dict(_gate_no_verdict))
    try:
        rows = [(n, d, _gate_lists.get(n, []), True) for n, d, _nw, _s in gate_from_snap(snap)]
        obs = red_obs_of_units(gate_red_units(o_r, rows))
        obs.update(stuck_obs(snap))
        return obs
    finally:
        for live, old in zip((_gate_lists, _outside_lists, _gate_no_verdict), saved):
            live.clear()
            live.update(old)


RED_OTHERS_EVERY = 60        # seconds between the other watched repos' red ticks: one gate read each ($AGENTCTL_RED_OTHERS_EVERY)
_last_others_tick = [0.0]


def red_tick_others(store=None, now_=None, dispatch=False, force=True):
    """The red tick of every watched repo but the primary (red-repos.json): its gate read, its reds observed and
    stepped, each repo inside its own try (one unreadable repo never stops the others or the primary). Its clone is
    this machine's when there is one (Fixes-Red trailers, authors); without one (ci-runner) its reds open and clear
    by its gate runs alone."""
    acts = []
    if not force and time.time() - _last_others_tick[0] < env_num("AGENTCTL_RED_OTHERS_EVERY", RED_OTHERS_EVERY):
        return acts
    _last_others_tick[0] = time.time()
    unread = []
    for c in red_repos():
        o_r = c["repo"]
        if o_r == RED_PRIMARY:
            continue
        try:
            red_read_token(o_r)
        except NoReadAccess:
            unread.append(o_r)  # one condition for all of them, below: no line per tick
            continue
        try:
            snap = red_gate_snap(o_r, store)
            clone = red_repo_clone(o_r)
            with _red_lock:
                acts += [f"{red_repo_short(o_r)}: {x}" for x in
                         red_tick(store or open_store(), o_r, clone, snap, now_=now_, dispatch=dispatch)]
            _others_said.pop(o_r, None)
        except (Exception, SystemExit) as e:
            red_others_skipped(o_r, str(e).replace('agentctl: ', '')[:200])
    if unread:
        try:
            red_no_read_access(unread, store)
        except (Exception, SystemExit) as e:
            err(f"red tick: could not note the missing read access ({str(e)[:120]})")
    for line in acts:
        err(f"red: {line}")
    return acts


_others_said = process_cache({})  # repo -> (why, when said): a skipped repo's line is said when it changes, else every RED_SKIP_SAY s
RED_SKIP_SAY = 600  # s between the lines of another repo whose tick keeps failing for the same reason


def red_others_skipped(o_r, why, now_=None):
    """Another repo's tick failed: said on a new reason, else at most every RED_SKIP_SAY s (it was every tick)."""
    t = now_ if now_ is not None else time.time()
    last = _others_said.get(o_r)
    if last and last[0] == why and t - last[1] < env_num("AGENTCTL_RED_SKIP_SAY", RED_SKIP_SAY):
        return None
    _others_said[o_r] = (why, t)
    line = f"red tick: {o_r}: skipped ({why})" + (" (still)" if last and last[0] == why else "")
    err(line)
    return line


def red_no_read_access(repos, store=None):
    """ONE condition while ci-runner cannot read the other watched repos: cleared by the red_read probe (the reader App
    configured on the host, and their runs read)."""
    return open_condition("UNKNOWN", f"NO READ ACCESS: {', '.join(repos)}: the reconciler cannot read their runs -- "
                                     "the lander App is push-only on movies (needs the agentctl-reader App); their "
                                     "reds are not watched until then",
                          run_probe("red_read", {"repos": list(repos)}), [], key="red-read-access", command="red",
                          exit_code=2, store=store)


def probe_red_read(a):
    """Every repo in a["repos"] has a reader token and its runs read."""
    for o_r in a.get("repos") or []:
        try:
            red_read_token(o_r)
            # listing: a read probe only (any answer proves the token reads)
            gh_json(o_r, "run", "list", "--limit", "1", "--json", "databaseId")
        except (Exception, SystemExit) as e:
            return False, f"{o_r}: {str(e).replace('agentctl: ', '')[:160]}"
    return True, "every watched repo's runs read"


def maybe_red_tick(repo):
    """At most every RED_TICK_INTERVAL s, from the processor's duties. Bounded; never fatal."""
    if time.time() - _last_red_tick[0] < env_num("AGENTCTL_RED_TICK", RED_TICK_INTERVAL):
        return

    def work():
        try:
            red_tick_repo(repo)
        except (Exception, SystemExit) as e:
            err(f"red tick: skipped ({str(e).replace('agentctl: ', '')[:200]})")
            try:
                gate_unread_spell(slug(repo), str(e)[:200])
            except (Exception, SystemExit):
                pass
        red_tick_others(force=False)  # the other watched repos (red-repos.json); never fatal, never the primary's gate spell
    if not run_bounded("red-tick", work, env_num("AGENTCTL_STEP_BOUND", 120)):
        err("red tick: still running from an earlier cycle (the loop goes on)")


def red_am_fixer():
    """`agentctl red am-fixer`: 0 inside a dispatched red fixer -- known by its session's binding or record, else (no
    Claude session) its fixer environment (74e2ec5's in_fixer) -- printing who; 1 for anybody else."""
    if in_fixer():
        print(agent_name() or own_fixer_record_path() or "a red fixer")
        return 0
    return 1


def cmd_red(a):
    if a.rcmd == "am-fixer":
        return red_am_fixer()
    if a.rcmd == "probe":
        return cmd_red_probe(a)
    o_r = slug(a.repo) if getattr(a, "repo", None) and a.rcmd not in ("fixer", "owner") else default_repo()
    st = open_store()
    if a.rcmd == "block":
        return red_block(o_r, a)
    if a.rcmd == "unblock":
        rc, lines = red_unblock(st, o_r, a.run, a.why)
        print("\n".join(lines))
        return rc
    if a.rcmd == "owner-push":
        rc, lines = red_owner_push(st, o_r, a.run, a.note or "", getattr(a, "worktree", None))
        print("\n".join(lines))
        return rc
    if a.rcmd in ("claim", "beat", "release"):
        if a.rcmd == "claim":
            if getattr(a, "fixer", None):  # the dispatched fixer says who it is (its prompt): bound to its session
                sid = os.environ.get("CLAUDE_CODE_SESSION_ID")
                if sid and not bind_fixer_session(sid, os.path.realpath(a.fixer)):
                    print(f"NOT CLAIMED: {a.fixer} is no live fixer record of this session ({sid[:8]})")
                    return 2
            rc, lines = red_claim(st, o_r, a.run, a.note or "", getattr(a, "worktree", None))
        elif a.rcmd == "beat":
            rc, lines = red_beat(st, o_r, a.run, a.note or "")
        else:
            rc, lines = red_release(st, o_r, a.run, a.any)
        print("\n".join(lines))
        return rc
    if a.rcmd == "status":
        if a.rcmd != "status":
            print(f"(`red {a.rcmd}` is now `red status`: the tick acts on every red)")
        return red_status(st, getattr(a, "verbose", False), getattr(a, "live", False))
    if a.rcmd == "auto" and getattr(a, "repo", None):  # ONE REPO's dispatch (the owner, 2026-11-19: "no bitcashier
        # builds"), in the store: the server's tick reads it
        if a.state in ("on", "off"):
            repo_dispatch_set(st, o_r, a.state == "off", claimant())
        offs = repo_dispatch_off_all(st)
        print(f"dispatch off for {o_r} (owner, since {offs[o_r].get('at')})" if o_r in offs else
              f"dispatch on for {o_r}")
        return 0
    if a.rcmd == "auto":
        off = reds_dir("DISPATCH_OFF")
        os.makedirs(reds_dir(), exist_ok=True)
        if a.state == "off":
            open(off, "w").write(ts() + "\n")
        elif a.state == "on" and os.path.exists(off):
            os.unlink(off)
        _blocker_cache.clear()
        b = dispatch_blocker()
        print(f"automatic dispatch: {'ON' if not b else 'OFF -- ' + b}")
        return 0
    if a.rcmd == "owner":
        return red_owner(a)
    if a.rcmd == "fixer":
        return red_fixer(a)
    if a.rcmd == "unblame":
        items = red_targets(st, o_r, a.run, create=False)
        if not items:
            print(f"NOT WITHDRAWN: #{a.run} names no red")
            return 1
        rc = 0
        for it in items:
            ok, line = blame_withdraw(st, it, a.why, scan_repo_for(o_r, None))
            print(line)
            rc = rc or (0 if ok else 1)
        return rc
    if a.rcmd == "dispatch":
        b = dispatch_blocker()
        if b:
            print(f"NOT DISPATCHED: {b}")
            return 2
        items = red_targets(st, o_r, a.run)
        if not items:
            print(f"NOT DISPATCHED: #{a.run} names no red on main")
            return 1
        for it in items:  # its offer at top priority: the dispatcher takes it now
            if it["state"] == "red":
                it = red_offer(st, it, f"{red_what(it['data'])}: dispatched by hand")
            store_it = st.item("red", it["id"])
            st.update_item("red", it["id"], store_it["version"], {"priority": -1, "author_tried": True})
            if it["state"] == "offered":
                red_put_offer(st, st.item("red", it["id"]))
        repo = scan_repo_for(o_r, getattr(a, "repo", None) and os.path.abspath(a.repo))
        lines = red_dispatcher(st, o_r, repo, st.clock())
        print("\n".join(lines) if lines else "NOT DISPATCHED: " + "; ".join(
            (st.offer_of(f"red:{it['id']}") or {}).get("reason") or it["state"] for it in items))
        return 0 if lines else 1
    return 2


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
