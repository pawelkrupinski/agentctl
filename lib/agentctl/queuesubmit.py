# agentctl, part `queuesubmit`: submitting a change: by ref, without the vault, the queued line, the fast precheck, a red fix's land.
# Runs inside agentctl's one namespace, after the parts before it in lib/agentctl/__init__.py's PARTS (it uses their names).

# SUBMISSION BY REF (design §13 step 14, §14.2): a queued change is also pushed to the remote as refs/heads/land/<entry>
# (its own ref: the movies hook checks nothing on it, no workflow runs on it), so the change no longer lives only in a
# worktree -- an author that ends, a worktree removed, leave it reachable. The tick deletes every land/* ref whose change
# is decided (or unknown). Checks rebuild from the commits, never from the worktree.
LAND_REF_SWEEP_EVERY = 600
_last_ref_sweep = [0.0]


def land_refs_supported(repo):
    """The repository's hook at origin/main checks nothing on a land/ ref (else the push would run its full checks)."""
    hook = git(repo, "show", f"{remote()}/main:scripts/hooks/pre-push", check=False).stdout
    return "refs/heads/land/" in hook


def submit_ref(repo, d, e, to_server=False):
    """Push the change to refs/heads/land/<entry> (bounded); the entry records it. Never fatal in phase 1 (the queue does
    not need it); in phase 2 (to_server: ci-runner fetches the change from it) the caller refuses without it."""
    if not land_refs_supported(repo):
        return None
    ref = f"refs/heads/land/{e['id']}"
    rc, out = run_watched(["git", "push", "--quiet", remote(), f"{e['head']}:{ref}"], repo, tool_env(),
                          os.path.join(d, f"submit-{e['id']}"), "submit", **push_bounds())
    if rc == 0:
        e["ref"] = ref
        if not to_server:
            save_entry(d, e)
        return ref
    err(f"submit: could not push {ref} ({tail_of(out or '', 2)[:160]}); the change is queued all the same")
    return None


LAND_REF_UNKNOWN_AGE = 24 * 3600  # s: a ref of a change neither the entries nor the store know is deleted only past this


def entry_id_age(eid, t):
    """Seconds since the entry id's YYYYMMDD-HHMMSS stamp (local time), or None when the id carries none."""
    try:
        return t - time.mktime(time.strptime(eid[:15], "%Y%m%d-%H%M%S"))
    except (ValueError, OverflowError):
        return None


def sweep_land_refs(repo, d, now_=None, store=None):
    """Every LAND_REF_SWEEP_EVERY s: each remote land/<entry> ref whose change is decided, deleted. Since phase 2 the
    changes live in ci-runner's STORE, not in entries/ (2026-11-04: every queued change looked unknown and lost its
    ref at the next sweep): a change either source holds open keeps its ref; one NEITHER knows is deleted only past
    LAND_REF_UNKNOWN_AGE, and logged."""
    t = now_ if now_ is not None else time.time()
    if t - _last_ref_sweep[0] < env_num("AGENTCTL_LAND_REF_SWEEP", LAND_REF_SWEEP_EVERY):
        return []
    _last_ref_sweep[0] = t
    r = git(repo, "ls-remote", "--heads", remote(), "refs/heads/land/*", check=False)
    if r.returncode != 0:
        return []
    every = {e["id"]: e for e in waiting_entries(d, open_only=False)}
    try:
        items = {it["id"]: it for it in (store or Store()).items("change")}
    except Exception as ex:  # the store unreachable: decide nothing this tick
        err(f"land refs: store unreadable ({ex}); no ref deleted")
        return []
    did = []
    for line in r.stdout.splitlines():
        ref = line.split()[-1]
        eid = ref[len("refs/heads/land/"):]
        e, it = every.get(eid), items.get(eid)
        if e and e.get("status") not in ENTRY_FINAL:
            continue
        if it and not machine_state("change", it["state"]).terminal:
            continue
        if not e and not it:
            age = entry_id_age(eid, t)
            if age is None or age < env_num("AGENTCTL_LAND_REF_UNKNOWN_AGE", LAND_REF_UNKNOWN_AGE):
                continue
            did.append(f"{ref}: unknown to entries and store, {int(age // 3600)} h old")
        dr = git(repo, "push", "--quiet", remote(), f":{ref}", check=False)
        did.append(f"{ref}: deleted" if dr.returncode == 0 else f"{ref}: could not delete")
    return did


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


# THE LAPTOP'S LAND NEEDS NO VAULT (2026-10-31: agents' `land` still died NoToken -- a `Fixes-Red:` trailer made
# change_fields read the reds from ci-runner's store, the token read failed in a session whose vault was locked, and
# fix_check let the NoToken (a SystemExit) through; the 10-30 fix had only let agents run `secrets` themselves, which
# prompts). The submission's git half is the land/<id> push over ssh; its store half takes a token from the
# environment or the processor's socket, never `secrets` (no_vault). With none, the change waits in the outbox and the
# laptop's sync -- which holds the token -- submits it; the reconciler checks the Fixes-Red trailers again at the push.
# THE QUEUED LINE NAMES NO VERDICT IT HAS NOT REACHED (2026-11-16: its old tail, "LANDED / WAITING / NOT LANDED goes to
# `agentctl inbox`", read as LANDED to a watcher matching the word): QUEUED is its only verdict word, and it says what
# the agent does next -- end its turn, or (a red fixer's land) nothing: the land waits for the verdict itself.
def queued_tail(waits=False):
    """The end of land's QUEUED / QUEUED-LOCAL line."""
    return ("not on main yet: this land now waits for its verdict" if waits
            else "not on main yet: END YOUR TURN; its verdict comes to `agentctl inbox`")


def submit_to_server(repo, d, fields, note="", after_queued=None, waits=False):
    """THE LAPTOP'S ONLY WAY INTO THE QUEUE (phase 2; it holds no queue of its own): the change pushed to
    refs/heads/land/<id>, then submitted to ci-runner's queue (the store unreachable, or no token here: the outbox,
    which the sync sends). Prints QUEUED / QUEUED-LOCAL / NOT LANDED; returns land's exit code. waits: this land then
    waits for the verdict itself (a red fixer's, fixer_land)."""
    head = fields["head"]
    e = {**fields, "id": f"{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}-{os.path.basename(repo)}-{os.urandom(3).hex()}"}
    if not submit_ref(repo, d, e, to_server=True):
        print(f"NOT LANDED: could not push the change to refs/heads/land/{e['id']} (ci-runner checks it from there): "
              "`agentctl env --check`, then land again")
        return 1
    r = submit_remote(Store(), e)
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


# THE FAST PRECHECK (review 2026-11-06 P1; the owner, 11-06: "landing must not take significantly longer"): a repository
# with an executable scripts/land-precheck (movies: the rebase dry run, the hook's commit-rule and linter checks, the
# registry's lints, ~20-60 s warm) has it run ALONGSIDE the queue, never before it: land prints QUEUED as fast as ever,
# and a refusal (exit 1) cancels the queued change and puts NOT LANDED in the inbox, minutes before the landing check
# would have, sparing that check. Exit 0 or 2 (could not check) leaves the change to the landing check.
# `land --no-fast-precheck` starts none.
FAST_PRECHECK = "scripts/land-precheck"
FAST_PRECHECK_BOUND = 900


def fast_precheck_spawn(argv, **kw):
    return spawn_detached(argv, **kw)


def start_fast_precheck(repo, main_ref, head, eid, a):
    """Start the precheck of queued change `eid` beside the queue; prints one line saying so."""
    if getattr(a, "no_fast_precheck", False) or not os.access(os.path.join(repo, FAST_PRECHECK), os.X_OK):
        return
    base = git(repo, "rev-parse", main_ref).strip()
    log = os.path.join(scratch(), f"agentctl-precheck-{eid}.log")
    try:
        with open(log, "w") as logf:
            fast_precheck_spawn([sys.executable, TOOL_PATH, "land-precheck", eid, "--repo", repo, "--base", base,
                                 "--head", head], cwd=repo, stdin=subprocess.DEVNULL, stdout=logf,
                                stderr=subprocess.STDOUT, start_new_session=True)
    except OSError as e:
        err(f"fast precheck not started ({e}); the landing check judges the change")
        return
    print(f"fast precheck of {eid} running alongside the queue ({FAST_PRECHECK}; log {log}): a refusal cancels it "
          "with NOT LANDED in `agentctl inbox`", flush=True)


def precheck_outcome(rc, out):
    """(outcome, the refusing step) of a fast precheck: passed / refused / unchecked, its `PRECHECK ...` line."""
    lines = (out or "").splitlines()
    last = next((l for l in reversed(lines) if l.startswith("PRECHECK ")), None)
    outcome = "unchecked" if rc is None else "refused" if rc == 1 else "passed"
    return outcome, (last or "")[:200] if outcome == "refused" else None


# THE PRECHECK IS NOTED THROUGH A BLIP (2026-11-17: the first live landing records carried no precheck_s -- deploy-nix's
# note died on one `POST /v1/store/item: timed out`, and a change submitted through the outbox has no item yet when its
# 60-s precheck ends): an unreachable store or a missing item is tried again, NOTE_PRECHECK_DELAYS apart.
NOTE_PRECHECK_DELAYS = (5, 15, 30, 60)
NOTE_PRECHECK_WAIT = 110  # s a precheck's note waits at most for the store or its item (the delays above, cut at this)


def note_precheck(eid, secs, rc, out, store=None, sleep=time.sleep):
    """The precheck's duration and outcome on its change item (`precheck`: the landing record's precheck_* fields),
    before a refusal cancels it; never fatal."""
    outcome, step = precheck_outcome(rc, out)
    rec = {"precheck": {"duration_s": round(secs, 1), "outcome": outcome, **({"step": step} if step else {})}}
    why, cap, delays = "its item does not exist", env_num("AGENTCTL_NOTE_PRECHECK_WAIT", NOTE_PRECHECK_WAIT), []
    for d in NOTE_PRECHECK_DELAYS:
        if sum(delays) + d <= cap:
            delays.append(d)
    for delay in (*delays, None):
        try:
            st = store or Store()
            for _ in range(3):  # a compare-and-set: the reconciler may move the item meanwhile
                it = st.item("change", eid)
                if not it:
                    why = "its item does not exist"
                    break
                try:
                    st.update_item("change", eid, it["version"], rec)
                    return True
                except StaleVersion:
                    why = "its item kept moving"
        except (Exception, SystemExit) as e:
            why = str(e)[:120]
        if delay is None:
            break
        sleep(delay)
    if why != "its item does not exist":  # no item: a change the store never held (a fixer's own land) -- nothing to say
        err(f"the precheck of {eid} could not be noted on its item ({why})")
    return False


PRECHECK_STEP_FAIL = re.compile(r"^precheck (\S+) FAIL\b")
PRECHECK_NOISE = re.compile(r"blocked by:|^!?\s*pre-push\S* \S+ (ok|FAILED)|^\W*$")


def precheck_reason(lines):
    """The refusing steps' own first reason lines (the detail the precheck indents under `precheck <step> FAIL`), for
    the NOT LANDED line (issue 20261116T225455-04c3c1: it named only `PRECHECK FAIL: rules`)."""
    out, step = [], None
    for line in lines:
        m = PRECHECK_STEP_FAIL.match(line)
        if m:
            step = m.group(1)
            continue
        if step and line.startswith((" ", "\t")):
            text = line.strip()
            if not PRECHECK_NOISE.search(text) and len(out) < 2:
                out.append(f"{step}: {text}" if not text.startswith(f"{step}:") else text)
        elif not line.startswith((" ", "\t")):
            step = None
    return "; ".join(out)[:400]


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


# A RED FIX LANDS THROUGH THE QUEUE (2026-11-16, Main #2515: a fixer's land, in-process since the queue began, ran the
# whole pre-push hook on the laptop -- its identitydrift step waited 75 min on the starved measure queue -- while its
# fixer's own `timeout` killed it twice and its 180-min turn ran out; the queue's 45-s precheck would have refused the
# missing Drift-Slice: trailer at once). A fixer's change is queued like every change -- the CI tier, red fixes first
# -- and its land runs the fast precheck in the FOREGROUND (its refusal is the fixer's NOT LANDED in seconds), then
# waits for the change's verdict, bounded: LANDED <sha> / NOT LANDED: <why>, the lines its wrapper reads.
FIXER_LAND_WAIT = 2 * 3600  # s a fixer's land waits for its queued change's verdict ($AGENTCTL_FIXER_LAND_WAIT)
FIXER_LAND_POLL = (15, 60)  # s: the await's backoff, first and cap


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


def change_decided(state):
    """Whether `state` is a terminal state of the change machine (an awaiter's `unread` or a store blip is not)."""
    return state in MACHINES["change"] and MACHINES["change"][state].terminal


def landed_sha_of(eid, data):
    return data.get("tip") or next(iter(data.get("landed_commits") or {}), "") or data.get("head")


def await_verdict(eid, bound, store=None):
    """Block until queued change eid is decided (its store item; through a store blip, asked again), at most `bound` s:
    (state, its item's data, seconds waited); state `landed`, another terminal state, or the last open one read."""
    t0, delays = time.time(), backoff(*FIXER_LAND_POLL)
    state, data = "unread", {}
    while True:
        try:
            it = (store or Store()).item("change", eid)
        except StoreUnavailable as e:
            it, state = None, f"the store did not answer ({str(e)[:80]})"
        if it:
            state, data = it["state"], it.get("data") or {}
            if machine_state("change", state).terminal:
                take_delivery(it, store)
                return state, data, time.time() - t0
        if time.time() - t0 >= bound:
            return state, data, time.time() - t0
        time.sleep(next(delays))


def take_delivery(it, store=None):
    """Record on change item `it` that a waiting call received its verdict (`delivered_at`): a later land never
    replays it as undelivered (verdict_still_holds). Best effort: a store blip or a racing writer only leaves it
    unrecorded, and the next land then checks the change again or replays it, both of them true."""
    if (it.get("data") or {}).get("delivered_at") or it.get("version") is None:  # said already, or a queue entry
        return  # the tick has not made an item of yet (own_change_item): nothing to record it on
    try:
        (store or Store()).update_item("change", it["id"], it["version"], {"delivered_at": time.time()})
    except (StaleVersion, StoreUnavailable):
        pass


def verdict_still_holds(repo, main_ref, it):
    """Whether rejected change item `it` may be replayed to a new `land --until-done` instead of being checked again
    (A VERDICT IS DURABLE, below): nobody has taken delivery of it yet (take_delivery) AND main is still the main it
    was decided on (`decided_on`). Main moved, the verdict said already, or no base recorded: it no longer holds."""
    data = it.get("data") or {}
    if data.get("delivered_at") or not data.get("decided_on"):
        return False
    git(repo, "fetch", "--quiet", remote(), "main", check=False)
    return git_ok(repo, "rev-parse", "--verify", "-q", main_ref) \
        and git(repo, "rev-parse", main_ref).strip() == data["decided_on"]


def await_queued(eid, store=None):
    """Block until queued change eid is decided, bounded by FIXER_LAND_WAIT: LANDED <sha> (0), NOT LANDED: <why> (1),
    or NOT LANDED (still queued) (1)."""
    state, data, waited = await_verdict(eid, env_num("AGENTCTL_FIXER_LAND_WAIT", FIXER_LAND_WAIT), store)
    if state == "landed":
        print(f"LANDED {landed_sha_of(eid, data)} (queued {eid})", flush=True)
        return 0
    if change_decided(state):
        why = str(data.get("why") or f"{eid} {state}")
        print(why if why.startswith("NOT LANDED") else f"NOT LANDED: {why}", flush=True)
        return 1
    print(f"NOT LANDED (still queued): {eid} is {state} after {fmt_age(waited)}; it stays queued as "
          "its red's fix (the red machine verifies it once it lands) -- `agentctl land status`", flush=True)
    return 1
