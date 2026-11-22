# agentctl, part `queueenter`: entering the land queue: enqueue, the server submit, the precheck and land-until-done
# Runs inside agentctl's one namespace, after the parts before it in lib/agentctl/__init__.py's PARTS (it uses their names).


def submit_ref(repo, d, e, to_server=False):
    """Push the change to refs/heads/land/<entry> (bounded); the entry records it. Never fatal in phase 1 (the queue does
    not need it); in phase 2 (to_server: ci-runner fetches the change from it) the caller refuses without it."""
    if not land_refs_supported(repo):
        e["push_error"] = f"not pushed: {remote()}/main's scripts/hooks/pre-push takes no refs/heads/land/ ref"
        return None
    ref = f"refs/heads/land/{e['id']}"

    def push():  # the same sha to the change's own ref: idempotent, so tried again through a blip (net_retry)
        rc_, out_ = run_watched(["git", "push", "--quiet", remote(), f"{e['head']}:{ref}"], repo, tool_env(),
                                os.path.join(d, f"submit-{e['id']}"), "submit", **push_bounds())
        if rc_ != 0 and is_transient_network(out_) and not credential_failure(out_):
            raise RuntimeError(tail_of(out_ or "", 2)[:200])
        return rc_, out_
    try:
        rc, out = net_retry(f"submit: the push of {ref}", push)
    except TransientNetwork as ex:
        rc, out = 1, str(ex)
    if rc == 0:
        e["ref"] = ref
        if not to_server:
            save_entry(d, e)
        return ref
    e["push_error"] = tail_of(out or "", 2).strip()[:300]
    err(f"submit: could not push {ref} ({e['push_error'][:160]}); the change is queued all the same")
    return None


def change_fields(repo, main_ref, head, a):
    """A queue entry's fields for the change at head in repo (land's own submission)."""
    commits = waiting_commits(repo, f"{remote()}/main", head)
    return {"worktree": repo, "branch": branch_of(repo), "head": head, "commits": commits,
            "fixes": sorted(fixes_of(repo, commits)), "diagnoses": sorted(diagnoses_of(repo, commits)) or None,
            "keep_worktree": bool(getattr(a, "keep_worktree", False)),
            "no_batch": bool(getattr(a, "no_batch", False)),
            "claude_pid": int(os.environ["CLAUDE_PID"]) if (os.environ.get("CLAUDE_PID") or "").isdigit() else None,
            "session": os.environ.get("CLAUDE_CODE_SESSION_ID") or None,
            "base": git(repo, "rev-parse", main_ref).strip(), "host": os.uname().nodename,
            # THE LANDER (lander.py): who landed it, and each commit's keys to find it on main after the rebase
            "lander": lander_record(repo), "commit_keys": commit_keys(repo, main_ref, head),
            # FIX REPLAY (redreplay.py): agentctl's own replays of the push, for the server's check
            "replays": fix_replay_proofs(repo, head) or None}


def submit_to_server(repo, d, fields, note="", after_queued=None, waits=False):
    """THE LAPTOP'S ONLY WAY INTO THE QUEUE (phase 2; it holds no queue of its own): the change pushed to
    refs/heads/land/<id>, then submitted to ci-runner's queue (the store unreachable, or no token here: the outbox,
    which the sync sends). Prints QUEUED / QUEUED-LOCAL / NOT LANDED; returns land's exit code. waits: this land then
    waits for the verdict itself (a red fixer's, fixer_land)."""
    head = fields["head"]
    e = {**fields, "id": f"{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}-{os.path.basename(repo)}-{os.urandom(3).hex()}"}
    if not submit_ref(repo, d, e, to_server=True):
        print(f"NOT LANDED: could not push the change to refs/heads/land/{e['id']} (ci-runner checks it from there): "
              f"{push_failure_why(e.get('push_error'))}: land again")
        return 1
    r = submit_remote(open_store(), e)
    offer_token(repo)  # this agent's token to the laptop processor, which holds none after a restart
    if r.get("outbox"):
        why = ("no store token in this process (none in its environment, the processor's socket held none; land "
               "never runs `secrets`): the sync submits it with its own token" if "AGENTCTL_STORE_TOKEN" in r["outbox"]
               else f"the store is unreachable ({r['outbox']}); the sync submits it when it answers")
        print(f"QUEUED-LOCAL {e['id']} ({head[:10]}){note}: {why}; {queued_tail(waits)}", flush=True)
        if after_queued:
            after_queued(e["id"])
        return 0
    ahead = r.get("ahead") or 0
    print(f"QUEUED {r['id']} ({head[:10]}{f', {ahead} ahead' if ahead > 0 else ''}){note}: its checks run on ci-runner; "
          f"{queued_tail(waits)}", flush=True)
    if after_queued:
        after_queued(r["id"])
    return 0


def cmd_land_precheck(a):
    """The detached half of start_fast_precheck: run the repo's precheck, and on a refusal cancel the queued change."""
    # Its own process group, ended WHOLE at the bound (review 2026-11-07: subprocess.run killed only the script, so what
    # it started -- sbt, a test JVM -- lived on, and kept the output pipe, and so this wait, open): run_watched.
    t0 = time.time()
    rc, out = run_watched([os.path.join(a.repo, FAST_PRECHECK), "--base", a.base, "--head", a.head], a.repo, None,
                          os.path.join(scratch(), f"agentctl-precheck-{a.entry}"), "precheck",
                          stall=FAST_PRECHECK_BOUND, wall=FAST_PRECHECK_BOUND)
    note_precheck(a.entry, time.time() - t0, rc, out)
    if rc is None:
        print(f"{out or ''}PRECHECK UNCHECKED: past its {FAST_PRECHECK_BOUND} s bound, ended with its children: "
              "could not check, its landing check judges it", flush=True)
        return 0
    lines = (out or "").splitlines()
    print("\n".join(lines), flush=True)
    if rc != 1:
        return 0
    last = next((l for l in reversed(lines) if l.startswith("PRECHECK ")), lines[-1] if lines else "exit 1")
    why = precheck_reason(lines)
    last = f"{last} -- {why}" if why else last
    cancel = argparse.Namespace(wcmd="cancel", repo=a.repo, entry=a.entry, renamed=True, hold_for=None, hold_why=None)
    # NOT LANDED only when the cancel took: the landing check runs alongside and may have decided first (review
    # 2026-11-07: a change that LANDED was told NOT LANDED, "its queue entry is cancelled").
    try:
        rc = cmd_waiting(cancel)
    except (Exception, SystemExit) as e:  # the store unreachable: the change is still queued, its check judges it
        err(f"could not cancel {a.entry}: {e}")
        verdict = (f"PRECHECK REFUSED {a.entry} ({a.head[:10]}): {last}; it could not be cancelled ({str(e)[:120]}), so "
                   f"the landing check still judges it -- `agentctl land cancel {a.entry}` to drop it, fix it in {a.repo}")
        inbox_write("land", verdict, [verdict], [l for l in lines if l.strip()][-40:], 1, cwd=a.repo)
        print(verdict, flush=True)
        return 1
    if rc != 0:
        print(f"the fast precheck refused {a.entry}, but it is already decided (no open change to cancel): its own "
              "verdict stands", flush=True)
        return 0
    verdict = (f"NOT LANDED: the fast precheck refused {a.entry} ({a.head[:10]}): {last}; its queue entry is cancelled. "
               f"Fix it in {a.repo}, then land again")
    inbox_write("land", verdict, [verdict], [l for l in lines if l.strip()][-40:], 1, cwd=a.repo)
    print(verdict, flush=True)
    return 1


def enqueue(repo, main_ref, sh, a):
    """Rebase, squash guard, then the change becomes a queued entry: prints QUEUED and starts the processor. A red
    fixer's land then waits for the verdict (fixer_land)."""
    rc, head = sync(repo, main_ref, sh, getattr(a, "release_hold", False))
    if rc is not None:
        return rc
    d, _clone, _wt = waiting_state(repo)
    fixer = in_fixer()
    waits = fixer or bool(getattr(a, "until_done", False))  # this land waits for the verdict itself
    a.queued_id = None

    def after_queued(eid):
        a.queued_id = eid
        if not waits:  # one that waits runs it in the foreground (fixer_land, land_until_done)
            start_fast_precheck(repo, main_ref, head, eid, a)
    if host_role() == "sync":  # THE LAPTOP'S LAND NEEDS NO VAULT (see submit_to_server)
        with no_vault():
            rc = submit_to_server(repo, d, change_fields(repo, main_ref, head, a), after_queued=after_queued,
                                  waits=waits)
    else:
        fields = change_fields(repo, main_ref, head, a)
        e, ahead = queue_change(d, fields)
        submit_ref(repo, d, e)
        print(f"QUEUED {e['id']} ({head[:10]}{f', {ahead} ahead' if ahead > 0 else ''}): its checks run in the "
              f"background; {queued_tail(waits)}", flush=True)
        after_queued(e["id"])
        spawn_waiting_processor(repo)
        offer_token(repo)
        rc = 0
    if rc != 0 or not waits or not a.queued_id:
        return rc
    if not fixer:  # land --until-done: its caller waits (land_until_done)
        return foreground_precheck(repo, main_ref, head, a.queued_id, a)
    return fixer_land(repo, main_ref, head, a.queued_id, a)


def foreground_precheck(repo, main_ref, head, eid, a):
    """The fast precheck of queued change eid in THIS process (a land that waits for its verdict): 1 when it refused
    (its NOT LANDED or PRECHECK REFUSED line the last printed), else 0."""
    if not getattr(a, "no_fast_precheck", False) and os.access(os.path.join(repo, FAST_PRECHECK), os.X_OK):
        base = git(repo, "rev-parse", main_ref).strip()
        if cmd_land_precheck(argparse.Namespace(entry=eid, repo=repo, base=base, head=head)) == 1:
            return 1
    return 0


def fixer_land(repo, main_ref, head, eid, a):
    if foreground_precheck(repo, main_ref, head, eid, a) == 1:
        return 1
    return await_queued(eid)


def land_until_done(repo, main_ref, sh, a):
    """`land --until-done` (LAND --UNTIL-DONE above)."""
    t0 = time.time()
    bound = a.deadline if a.deadline is not None else env_num("AGENTCTL_UNTIL_DONE_DEADLINE", UNTIL_DONE_DEADLINE)
    head = git(repo, "rev-parse", "HEAD").strip()
    it = own_change_item(repo, head)
    if it and it["state"] == "landed":
        eid = it["id"]
    elif it and it["state"] == "rejected" and verdict_still_holds(repo, main_ref, it):
        # A VERDICT IS DURABLE (above): decided while no call waited, on this very main, said now
        take_delivery(it)
        why = str((it.get("data") or {}).get("why") or f"{it['id']} rejected")
        print(f"DECIDED {it['id']} ({head[:10]}) while no --until-done waited:", flush=True)
        print(why if why.startswith("NOT LANDED") else f"NOT LANDED: {why}", flush=True)
        print(next_command(repo, why) + f" (to queue {head[:10]} again unchanged: ~/bin/agentctl land)", flush=True)
        return 1
    elif it and not machine_state("change", it["state"]).terminal:
        eid = it["id"]
        print(f"RESUMING {eid} ({head[:10]}, {it['state']}): waiting for its verdict", flush=True)
    else:
        git(repo, "fetch", "--quiet", remote(), "main", check=False)
        commits, left = cherry_landed(repo, main_ref, "HEAD")
        if commits is not None and not left and git_ok(repo, "rev-parse", "--verify", "-q", main_ref):
            # nothing of its own left to land (it reached main another way, or there never was a commit)
            return until_done_finish(repo, "-", git(repo, "rev-parse", main_ref).strip(), a)
        a.until_done = True
        rc = enqueue(repo, main_ref, sh, a)
        eid = getattr(a, "queued_id", None)
        if rc != 0 or not eid:
            print(next_command(repo, ""), flush=True)
            return 1
    state, data, _waited = await_verdict(eid, max(0.0, bound - (time.time() - t0)))
    if state == "landed":
        sha = data.get("landed_sha") or landed_sha_of(eid, data)
        print(f"LANDED {sha} (queued {eid})", flush=True)
        return until_done_finish(repo, eid, sha, a, (it if it and it["id"] == eid else None)
                                 or open_store().item("change", eid))
    if change_decided(state):
        why = str(data.get("why") or f"{eid} {state}")
        print(why if why.startswith("NOT LANDED") else f"NOT LANDED: {why}", flush=True)
        print(next_command(repo, why), flush=True)
        return 1
    print(f"NOT DONE: {eid} is {state} after {fmt_age(time.time() - t0)} (the --deadline); it stays queued -- run "
          f"{UNTIL_DONE_AGAIN} again from {repo}: it resumes this entry", flush=True)
    return 2
