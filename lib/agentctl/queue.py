# agentctl, part `queue`: the land queue and submission.
# Runs inside agentctl's one namespace, after the parts before it in lib/agentctl/__init__.py's PARTS (it uses their names).

# ---------------------------------------------------------------- the land queue: agents never wait on a land
# The owner (2026-10-27): "agents never wait on a land". `land` rebases the change onto forgejo/main, runs the squash
# guard, enqueues it and returns at once: `QUEUED <id> (<sha>)`. The always-on `waiting` processor (launchd
# net.bitcashier.agentctl-waiting) runs each queued change's checks in a CHECK TREE of its own (a pool of detached
# worktrees <clone>-wt-check-<n>, kept warm between checks, seeded from the shared build cache when cold), admitted by
# the governor (a check; red for a Fixes-Red fix of a red run, which goes first) -- or on ci-runner's check tier when the
# governor reports pressure (see "remote checks") -- then, under the landing lock: main green (or a change that may land
# while red), the checked tip is pushed (`LANDED`, and a detached watch); main red, it joins `waiting` (`WAITING`). Main
# moved meanwhile: rebased in the tree and checked again. Every verdict goes to the inbox with the agent's worktree.
# `land --follow` keeps the old in-process land (checks, push, watch) for scripts and red fixers.
QUEUE_POOL = 3          # check trees, so at most this many queued changes are checked at once ($AGENTCTL_CHECK_TREES)
# A check the LAPTOP failed, not the change: sbt's server never came up or was lost, a script test cut off by its
# time bound, a stall. Queued again after QUEUE_INFRA_DELAY, at most QUEUE_INFRA_RETRIES times, then judged as it is.
INFRA_CHECK_FAILURE = re.compile(r"failed to connect to server|sbt server did not start within \d+ seconds|"
                                 r"sbt server disconnected|^TIMEOUT +\d+s|STALLED:", re.M)
QUEUE_INFRA_RETRIES = 3
QUEUE_INFRA_DELAY = 300
# A PUSH, FETCH OR TUNNEL BLIP IS NO VERDICT ON THE CHANGE (audit #6, 2026-10-28: entry 67 "the push failed (pre-push
# hook or remote)" was a tunnel error in its tail; a fixer GAVE UP on it): such a failure, a stalled push, a lock that
# never came free and main moving under every round re-queue the entry (retry_after, at most QUEUE_INFRA_RETRIES),
# its `attempts` reset (a checker restart after an infra re-queue is no failure of the change).
TRANSIENT_PUSH = re.compile(r"Connection (reset|refused|timed out|closed)|closed by remote host|Could not resolve host|ssh: connect to host|"
                            r"kex_exchange_identification|[Bb]roken pipe|early EOF|remote end hung up|RPC failed|"
                            r"unable to access|HTTP (502|503|504)|timed out|Operation timed out|tunnel", re.I)


INFRA_BACKOFF = (300, 600, 1800, 3600)  # infra re-queues back off; from the third, each one escalates


def requeue_infra(d, e, why, delay=None, reason=None, detail=None, repo=None):
    """Back to queued for a later look: infra NEVER rejects a change (design §5.1). The delay grows 5 -> 60 min; from
    QUEUE_INFRA_RETRIES on, each re-queue is an escalation (its event's severity). NEVER SILENTLY FOREVER (2026-10-29:
    a corrupt clone's fetch of main re-queued every change for hours, and nobody was told): re-queued for the same
    reason (reason: why without its details) QUEUE_INFRA_RETRIES times or for INFRA_SAME_MAX, it opens a condition
    with the exact error (detail), which its own probe clears."""
    n = e.get("infra_retries", 0) + 1
    wait = delay if delay is not None else INFRA_BACKOFF[min(n - 1, len(INFRA_BACKOFF) - 1)]
    sev = "escalate" if n >= QUEUE_INFRA_RETRIES else "info"
    t = time.time()
    reason = reason or infra_reason_of(why)
    same = e.get("infra_reason") == reason
    n_same = int(e.get("infra_same") or 0) + 1 if same else 1
    since = float(e.get("infra_reason_since") or t) if same else t
    set_change(d, e, "queued", data={"infra_attempts": n},
               event=change_event(e, f"QUEUED AGAIN {e['id']}: infra ({why}); checked again in {int(wait // 60)} min",
                                  code=None, final=False, severity=sev),
               infra_retries=n, retry_after=t + wait, why=f"infra: {why}", waiting_on=None,
               infra_reason=reason, infra_same=n_same, infra_reason_since=since)
    wlog(d, f"{e['id']}: {why}: queued again in {int(wait // 60)} min (infra retry {n}"
            + (", escalated" if sev == "escalate" else "") + ")")
    if n_same >= QUEUE_INFRA_RETRIES or t - since >= env_num("AGENTCTL_INFRA_SAME_MAX", INFRA_SAME_MAX):
        try:
            infra_requeue_condition(d, e, why, reason, n_same, t - since, detail, repo)
        except (Exception, SystemExit) as x:
            err(f"{e['id']}: re-queued {n_same} times for {reason}, and its condition could not be opened ({str(x)[:160]})")


INFRA_SAME_MAX = 30 * 60
FETCH_MAIN_FAILED = "its fetch of main failed"


def infra_reason_of(why):
    """An infra re-queue's reason without its details (what differs between two re-queues for the same cause)."""
    return re.sub(r"\s*\(.*?\)", "", why).split(":")[0].strip()


def infra_requeue_condition(d, e, why, reason, n, age, detail, repo):
    """The condition of a change re-queued n times for the same infra reason: main's fetch failing is the clone's
    (one per clone, cleared when it verifies and fetches main again); any other reason the change's own (cleared when
    it gets past it)."""
    text = (f"STALLED: {e['id']} re-queued {n} times in {fmt_age(age)} for the same infra reason ({reason}); it lands "
            f"only once that clears. Last: {why}")
    tail = (detail or why).strip().splitlines()[-15:]
    if reason == FETCH_MAIN_FAILED:
        repo = repo or repo_of_waiting(d)
        return open_condition("STALLED", f"{text}. Every change's check fetches main: the landing is blocked",
                              run_probe("clone_health", {"repo": repo}), [],
                              key=f"main-fetch-{os.path.basename(os.path.abspath(repo))}", command="land", tail=tail)
    return open_condition("STALLED", text, run_probe("infra_requeue", {"d": d, "eid": e["id"], "reason": reason}), [],
                          key=f"infra-requeue-{e['id']}", command="land", cwd=e.get("worktree"), tail=tail)


def probe_infra_requeue(a):
    """Change a['eid'] got past a['reason']: decided, checked, gone, or re-queued for another reason."""
    e = read_record(os.path.join(a["d"], "entries", f"{a['eid']}.json")) or {}
    stuck = e.get("status") in ("queued", "checking") and e.get("infra_reason") == a.get("reason")
    return not stuck, f"entry {a['eid']}: {e.get('status') or 'gone'}" + (f" ({e.get('why')})" if stuck else "")


# Main moved between a change's check and its push: re-checked only when what landed meanwhile can change the verdict
# (the owner, 2026-10-27: memo-key -41 passed runs 3341 and 3350, then main moved and it was checked a third time) --
# a path both touch, an sbt module one touches that the other's module is (or depends on: the hook's own compile plan,
# `pre-push --compile-plan`, build.sbt's dependsOn), or the build or the hook itself. Otherwise rebased and pushed with
# the change's checks skipped at the push (PREPUSH_SKIP: the hook's own plan for its paths; the red gate still runs);
# main's CI tests the combination. A rebase conflict is NOT LANDED, as ever.
RECHECK_ALWAYS = re.compile(r"^(build\.sbt|project/.*|scripts/hooks/.*|scripts/hooks-path\.sh|scripts/tool-conventions\.sh)$")


def range_paths(cwd, a, b):
    """The paths a..b changes (both sides of a rename)."""
    return [p for p in git(cwd, "diff", "--name-only", "--no-renames", a, b).splitlines() if p]


def hook_query(hook, tree, flag, paths):
    """The hook's dry-run answer for paths on stdin (`--plan`, `--compile-plan`), as words; None when unreadable."""
    try:
        r = subprocess.run([hook, flag], cwd=tree, input="".join(f"{p}\n" for p in paths), capture_output=True,
                           text=True, timeout=120, env=tool_env())
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout.split() if r.returncode == 0 else None


def sbt_modules(hook, tree, paths):
    """[(module, {module and its dependents})] per path that compiles into one, from the hook's compile plan (its first
    task is the path's own module); None when the plan cannot be read. The plan reads a path's directory and extension
    only, so one query per (directory, extension)."""
    reps = {}
    for p in paths:
        reps.setdefault((os.path.dirname(p), os.path.splitext(p)[1]), p)
    out = []
    for p in reps.values():
        tasks = hook_query(hook, tree, "--compile-plan", [p])
        if tasks is None:
            return None
        mods = [t.split("/", 1)[0] for t in tasks]
        if mods:
            out.append((mods[0], set(mods)))
    return out


def recheck_reason(hook, tree, base, moved, change_paths):
    """(commits main moved by, why the change must be checked again or None)."""
    n = len(git(tree, "rev-list", f"{base}..{moved}", check=False).stdout.split())
    landed_paths = range_paths(tree, base, moved)
    build = sorted(p for p in landed_paths if RECHECK_ALWAYS.match(p))
    if build:
        return n, f"they change {build[0]}"
    same = sorted(set(landed_paths) & set(change_paths))
    if same:
        return n, f"both touch {same[0]}" + (f" (+{len(same) - 1} more)" if len(same) > 1 else "")
    if not hook:
        return n, None
    mine, theirs = sbt_modules(hook, tree, change_paths), sbt_modules(hook, tree, landed_paths)
    if mine is None or theirs is None:
        return n, "the hook's compile plan could not be read"
    for m, m_deps in mine:
        for t, t_deps in theirs:
            if m in t_deps or t in m_deps:
                return n, f"sbt modules overlap ({m} and {t})"
    return n, None


def branch_of(wt):
    """The worktree's checked-out branch ('' when detached or gone)."""
    if not os.path.isdir(wt or ""):
        return ""
    return git(wt, "symbolic-ref", "--short", "-q", "HEAD", check=False).stdout.strip()


def same_change_source(e, worktree, branch):
    """Whether entry e came from this worktree and branch (an entry from before branches were recorded: the worktree)."""
    if os.path.realpath(e.get("worktree") or "") != os.path.realpath(worktree or ""):
        return False
    return not e.get("branch") or not branch or e["branch"] == branch


def queued_entries(d):
    return [e for e in waiting_entries(d, open_only=False) if e.get("status") in ("queued", "checking", "checked", "pushing")]


def queue_mode(repo, a):
    """Whether land queues (QUEUED, returning at once): a repo whose hook can check a change apart (movies), unless
    --follow / --no-watch (scripts), a dispatched red fixer (its wrapper reads LANDED), or AGENTCTL_QUEUE=0."""
    if getattr(a, "follow", False) or getattr(a, "no_watch", False) or in_fixer():
        return False
    return os.environ.get("AGENTCTL_QUEUE", "1") != "0" and batching(repo)


# SUBMISSION BY REF (design §13 step 14, §14.2): a queued change is also pushed to Forgejo as refs/heads/land/<entry>
# (its own ref: the movies hook checks nothing on it, no workflow runs on it), so the change no longer lives only in a
# worktree -- an author that ends, a worktree removed, leave it reachable. The tick deletes every land/* ref whose change
# is decided (or unknown). Checks rebuild from the commits, never from the worktree.
LAND_REF_SWEEP_EVERY = 600
_last_ref_sweep = [0.0]


def land_refs_supported(repo):
    """The repository's hook at forgejo/main checks nothing on a land/ ref (else the push would run its full checks)."""
    hook = git(repo, "show", f"{REMOTE}/main:scripts/hooks/pre-push", check=False).stdout
    return "refs/heads/land/" in hook


def submit_ref(repo, d, e, remote=False):
    """Push the change to refs/heads/land/<entry> (bounded); the entry records it. Never fatal in phase 1 (the queue does
    not need it); in phase 2 (remote: ci-runner fetches the change from it) the caller refuses without it."""
    if not land_refs_supported(repo):
        return None
    ref = f"refs/heads/land/{e['id']}"
    rc, out = run_watched(["git", "push", "--quiet", REMOTE, f"{e['head']}:{ref}"], repo, tool_env(),
                          os.path.join(d, f"submit-{e['id']}"), "submit", **push_bounds())
    if rc == 0:
        e["ref"] = ref
        if not remote:
            save_entry(d, e)
        return ref
    err(f"submit: could not push {ref} ({tail_of(out or '', 2)[:160]}); the change is queued all the same")
    return None


def sweep_land_refs(repo, d, now_=None):
    """Every LAND_REF_SWEEP_EVERY s: each remote land/<entry> ref whose change is decided, or unknown here, deleted."""
    t = now_ if now_ is not None else time.time()
    if t - _last_ref_sweep[0] < env_num("AGENTCTL_LAND_REF_SWEEP", LAND_REF_SWEEP_EVERY):
        return []
    _last_ref_sweep[0] = t
    r = git(repo, "ls-remote", "--heads", REMOTE, "refs/heads/land/*", check=False)
    if r.returncode != 0:
        return []
    every = {e["id"]: e for e in waiting_entries(d, open_only=False)}
    did = []
    for line in r.stdout.splitlines():
        ref = line.split()[-1]
        eid = ref[len("refs/heads/land/"):]
        e = every.get(eid)
        if e and e.get("status") not in ENTRY_FINAL:
            continue
        dr = git(repo, "push", "--quiet", REMOTE, f":{ref}", check=False)
        did.append(f"{ref}: deleted" if dr.returncode == 0 else f"{ref}: could not delete")
    return did


def change_fields(repo, main_ref, head, a):
    """A queue entry's fields for the change at head in repo (land's own submission)."""
    commits = waiting_commits(repo, f"{REMOTE}/main", head)
    return {"worktree": repo, "branch": branch_of(repo), "head": head, "commits": commits,
            "fixes": sorted(fixes_of(repo, commits)), "keep_worktree": bool(getattr(a, "keep_worktree", False)),
            "claude_pid": int(os.environ["CLAUDE_PID"]) if (os.environ.get("CLAUDE_PID") or "").isdigit() else None,
            "session": os.environ.get("CLAUDE_CODE_SESSION_ID") or None,
            "base": git(repo, "rev-parse", main_ref).strip(), "host": os.uname().nodename}


# THE LAPTOP'S LAND NEEDS NO VAULT (2026-10-31: agents' `land` still died NoToken -- a `Fixes-Red:` trailer made
# change_fields read the reds from ci-runner's store, the token read failed in a session whose vault was locked, and
# fix_check let the NoToken (a SystemExit) through; the 10-30 fix had only let agents run `secrets` themselves, which
# prompts). The submission's git half is the land/<id> push over ssh; its store half takes a token from the
# environment or the processor's socket, never `secrets` (no_vault). With none, the change waits in the outbox and the
# laptop's sync -- which holds the token -- submits it; the reconciler checks the Fixes-Red trailers again at the push.
def submit_to_server(repo, d, fields, note=""):
    """THE LAPTOP'S ONLY WAY INTO THE QUEUE (phase 2; it holds no queue of its own): the change pushed to
    refs/heads/land/<id>, then submitted to ci-runner's queue (the store unreachable, or no token here: the outbox,
    which the sync sends). Prints QUEUED / QUEUED-LOCAL / NOT LANDED; returns land's exit code."""
    head = fields["head"]
    e = {**fields, "id": f"{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}-{os.path.basename(repo)}-{os.urandom(3).hex()}"}
    if not submit_ref(repo, d, e, remote=True):
        print(f"NOT LANDED: could not push the change to refs/heads/land/{e['id']} (ci-runner checks it from there): "
              "`agentctl env --check`, then land again")
        return 1
    r = submit_remote(Store(), e)
    offer_token(repo)  # this agent's token to the laptop processor, which holds none after a restart
    if r.get("outbox"):
        why = ("no Forgejo token in this process (none in its environment, the processor's socket held none; land "
               "never runs `secrets`): the sync submits it with its own token" if "FORGEJO_API_TOKEN" in r["outbox"]
               else f"the store is unreachable ({r['outbox']}); the sync submits it when it answers")
        print(f"QUEUED-LOCAL {e['id']} ({head[:10]}){note}: {why}. LANDED / WAITING / NOT LANDED goes to "
              "`agentctl inbox`", flush=True)
        return 0
    ahead = r.get("ahead") or 0
    print(f"QUEUED {r['id']} ({head[:10]}{f', {ahead} ahead' if ahead > 0 else ''}){note}: its checks run on ci-runner; "
          "LANDED / WAITING / NOT LANDED goes to `agentctl inbox`", flush=True)
    return 0


def enqueue(repo, main_ref, sh, a):
    """Rebase, squash guard, then the change becomes a queued entry: prints QUEUED and starts the processor."""
    rc, head = sync(repo, main_ref, sh)
    if rc is not None:
        return rc
    d, _clone, _wt = waiting_state(repo)
    if host_role() == "sync":  # THE LAPTOP'S LAND NEEDS NO VAULT (see submit_to_server)
        with no_vault():
            return submit_to_server(repo, d, change_fields(repo, main_ref, head, a))
    fields = change_fields(repo, main_ref, head, a)
    e, ahead = queue_change(d, fields)
    submit_ref(repo, d, e)
    print(f"QUEUED {e['id']} ({head[:10]}{f', {ahead} ahead' if ahead > 0 else ''}): its checks run in the background; "
          "LANDED / WAITING / NOT LANDED goes to `agentctl inbox`", flush=True)
    spawn_waiting_processor(repo)
    offer_token(repo)
    return 0


# CHECK SLOTS AND A CHANGE'S CHECK ARE LEASES (design §6, §7.1; phase 1 step 4): `slot/check/<repo>/<i>` (one per
# check tree) and `check/<entry>` (its checker), TTL CHECK_LEASE_TTL renewed on the check's real progress
# (run_watched's output and CPU; a remote check's run status), max hold CHECK_LEASE_MAX. A checker that stops
# progressing loses both: the slot frees, and the entry's next checker adopts its remote run. No lock files.
CHECK_LEASE_TTL = 10 * 60
CHECK_LEASE_MAX = 3 * 3600


def tree_slot(d, i, max_hold=None):
    """THE lease of check tree i: whoever touches the tree -- a checker, the batch, a push -- holds it."""
    return LeaseHold(f"slot/check/{os.path.basename(d.rstrip('/'))}/{i}",
                     env_num("AGENTCTL_CHECK_LEASE_TTL", CHECK_LEASE_TTL), max_hold or CHECK_LEASE_MAX)


def check_tree_slot(repo, d):
    """(index, LeaseHold) of a free check tree slot, or None when all QUEUE_POOL are busy."""
    for i in range(1, int(env_num("AGENTCTL_CHECK_TREES", QUEUE_POOL)) + 1):
        h = tree_slot(d, i)
        if h.acquire():
            return i, h
    return None


def checker_lease(eid):
    return LeaseHold(f"check/{eid}", env_num("AGENTCTL_CHECK_LEASE_TTL", CHECK_LEASE_TTL), CHECK_LEASE_MAX,
                     meta={"entry": eid})


def checker_alive(eid):
    """The live checker lease of entry eid (meta: its pid), or None."""
    return lease_live(f"check/{eid}")


def check_tree(repo, i, sha):
    """The detached worktree <clone>-wt-check-<i> at sha (kept between checks: its build stays warm)."""
    _d, clone, _w = waiting_state(repo)
    tree = mark_internal(f"{clone}-wt-check-{i}")
    if not os.path.isdir(tree):
        git(repo, "worktree", "prune", check=False)
        git(repo, "worktree", "add", "-q", "--detach", mark_internal(tree), sha)
        seed = os.path.join(tree, "scripts", "sbt-warm")
        if os.access(seed, os.X_OK):  # a cold tree's first compile seeded from the shared build cache, not from scratch
            try:
                subprocess.run([seed, "--seed"], cwd=tree, capture_output=True, text=True, timeout=600, env=tool_env())
            except subprocess.TimeoutExpired:
                err(f"sbt-warm --seed in {tree} took over 10 min")
    else:
        git(tree, "reset", "-q", "--hard", check=False)
        git(tree, "clean", "-qfd", "-e", "target/", check=False)
        git(tree, "checkout", "-q", "--force", "--detach", sha)
    return tree


def entry_owned(eid):
    return Owned("check", eid, ["waiting", "check", eid])


# THE TICK DRIVES CHANGES (design §5.1, phase 1 step 6). A change item's state is written ONLY by set_change: the store
# item (a transition, with its event) and the entry's mirrored `status` (which the batch code still reads until step
# 8). The executor (`waiting check <id>`, holding check/<entry>) takes a change from queued to checking and, by its
# verdict, to checked, rejected (a test failure, a conflict) or back to queued (infra: never a rejection). The tick
# (tick_changes, every processor cycle) starts executors, re-queues a change whose checker lost its lease, and pushes a
# checked change under the landing lease: pushing -> landed, or back to checked (a push never rejects; after 3
# failures a condition on the git route). Main moving with an overlap is a new round (queued), never an attempt.
ENTRY_STATUS = {"queued": "queued", "checking": "checking", "checked": "checked", "pushing": "pushing",
                "waiting": "joined", "landed": "landed", "rejected": "rejected", "superseded": "superseded",
                "cancelled": "cancelled"}
PUSH_ATTEMPTS_MAX = 3


def set_change(d, e, to, data=None, event=None, store=None, **fields):
    """THE writer of a change's state: its item (transition + event) and its entry's status, together."""
    st = store or Store()
    cur = st.put_item("change", e["id"], ENTRY_STATE.get(e.get("status"), "queued"),
                      {"worktree": e.get("worktree"), "head": e.get("head")})
    try:
        if not machine_state("change", cur["state"]).terminal:
            st.transition("change", e["id"], to, cur["version"], data=data, event=event)
    except StaleVersion:  # another writer moved it: re-read once
        cur = st.item("change", e["id"])
        if not machine_state("change", cur["state"]).terminal:
            st.transition("change", e["id"], to, cur["version"], data=data, event=event)
    e.update(status=ENTRY_STATUS[to], **fields)
    save_entry(d, e)


def queue_verdict(d, e, status, verdict, tail=(), code=1, **fields):
    """A change's final verdict: its state (set_change), whose event the projector makes its author's inbox entry."""
    set_change(d, e, {"joined": "waiting"}.get(status, status), event=change_event(e, verdict, tail, code),
               decided=ts(), why=verdict, **fields)
    wlog(d, f"{e['id']}: {verdict.splitlines()[0]}")


# ONE QUEUE, CLOSED BY CONTENT (2026-10-30 split brain: six pre-cutover entries in the laptop's local waiting dir and
# four store items mirrored from them sat "waiting" for 11 h, every one already on main under rebased shas, and W1
# escalated). A change whose commits are on main BY CONTENT (`git cherry`: landing rebases, so never by sha) is LANDED,
# looked at every LANDED_LOOK_EVERY s per change, every tick: the server's entries and items, and on the laptop the
# store's items its clone has the commits of. The laptop holds no queue of its own: what its local dir still holds
# goes to ci-runner's queue (migrate_local_queue). A store item no queue carries is a STRANDED CHANGE condition.
LANDED_LOOK_EVERY = 600
_landed_looked = {}


def change_landed(repo, head, commits=None):
    """(landed, via): landed True when the change at head is on main by content (head an ancestor, or every commit of it
    patch-equivalent there or with a content twin, cherry_landed), False when not, None when this clone cannot tell (head unknown here); via the main commit
    carrying its last commit, when known."""
    main = f"refs/remotes/{REMOTE}/main"
    if not head or not git_ok(repo, "cat-file", "-e", f"{head}^{{commit}}"):
        return None, None
    if git_ok(repo, "merge-base", "--is-ancestor", head, main):
        return True, head
    limit = [f"{commits[0]}^"] if commits and git_ok(repo, "cat-file", "-e", f"{commits[0]}^^{{commit}}") else []
    commits, left = cherry_landed(repo, main, head, *limit)
    if commits is None:
        return None, None
    if not commits or left:
        return False, None
    return True, equivalent_on(repo, head, main)


def landed_look_due(cid, t):
    if t - _landed_looked.get(cid, 0) < env_num("AGENTCTL_LANDED_LOOK", LANDED_LOOK_EVERY):
        return False
    _landed_looked[cid] = t
    return True


def close_item_landed(st, cid, why):
    """The store's change item cid, unless terminal (or missing), LANDED with an event saying why. True when moved."""
    it = st.item("change", cid)
    if not it or machine_state("change", it["state"]).terminal:
        return False
    try:
        st.transition("change", cid, "landed", it["version"], data={"why": why},
                      event={"text": f"LANDED {cid}: {why}", "refs": {"entry": cid}})
        return True
    except (StaleVersion, TerminalState):
        return False


def close_landed_changes(repo, d=None, store=None, now_=None):
    """Every open change whose commits are on main by content closes as LANDED: the queue entries of d (with their
    items), then the store's other open items (by their head). Returns what it did."""
    st, t = store or Store(), now_ if now_ is not None else time.time()
    did, carried = [], set()
    for e in waiting_entries(d, open_only=False) if d else []:
        if e.get("status") in ENTRY_FINAL or e.get("status") == "pushing":
            continue
        carried.add(e["id"])
        landed, via = change_landed(repo, e.get("head"), e.get("commits")) if landed_look_due(e["id"], t) else (0, 0)
        if landed:
            already_landed(d, e, via)
            did.append(f"{e['id']}: its commits are on main (by content): landed")
            carried.discard(e["id"])
    for it in st.items("change"):
        if machine_state("change", it["state"]).terminal or it["state"] == "pushing" or it["id"] in carried:
            continue
        if landed_look_due(it["id"], t) and change_landed(repo, (it.get("data") or {}).get("head"))[0] and \
                close_item_landed(st, it["id"], "its commits are on main (by content)"):
            did.append(f"{it['id']}: its commits are on main (by content): landed")
    return did


def stranded_changes(repo, d, store=None, now_=None):
    """THE SERVER'S: each open store change item its queue (d) has no entry for -- no source, nothing will ever check,
    push or close it -- opens a STRANDED CHANGE condition naming it; its probe (the item closed, or an entry for it)
    closes it. Returns the ids."""
    st = store or Store()
    entries = {e["id"] for e in waiting_entries(d, open_only=False)}
    out = []
    for it in st.items("change"):
        if machine_state("change", it["state"]).terminal or it["id"] in entries:
            continue
        r = run_probe("change_source", {"id": it["id"], "repo": repo})
        if r.ok is False:
            dd = it.get("data") or {}
            open_condition("change-stranded", f"STRANDED CHANGE {it['id']} ({it['state']}, head "
                           f"{str(dd.get('head') or '?')[:10]}, {dd.get('worktree') or '?'}): no queue entry carries it, "
                           "so nothing checks, pushes or closes it. Landed: it closes itself once a clone with its "
                           f"commits sees them on main. Else `agentctl land cancel {it['id']}`, and land it again.",
                           r, [], key=f"change-stranded-{it['id']}", store=st)
            out.append(it["id"])
    return out


def probe_change_source(a):
    """The change item is closed, or the server's queue carries it again."""
    it = Store().item("change", a["id"])
    if not it or machine_state("change", it["state"]).terminal:
        return True, f"change {a['id']} is {it['state'] if it else 'gone'}"
    d = waiting_state(a.get("repo") or movies_repo_path())[0]
    if os.path.exists(os.path.join(d, "entries", f"{a['id']}.json")):
        return True, f"change {a['id']} is in the queue"
    return False, f"change {a['id']} is {it['state']} and no queue entry carries it"


def migrate_local_queue(repo, store=None):
    """THE LAPTOP'S (sync role): its local waiting dir holds no queue. Each open entry there is LANDED when its commits
    are on main by content, else pushed to refs/heads/land/<id> and submitted to ci-runner's queue under its own id
    (the entry: `migrated`). One that cannot move is said loudly (stderr, and its author's inbox once an hour).
    Returns what it did."""
    if host_role() != "sync" or not os.path.isdir(repo):
        return []
    d = waiting_state(repo)[0]
    st = store or Store()
    did = []
    for e in waiting_entries(d, open_only=False):
        if e.get("status") in ENTRY_FINAL:
            continue
        landed, via = change_landed(repo, e.get("head"), e.get("commits"))
        if landed:
            e.update(status="landed", landed_sha=via, decided=ts(), why="its commits are on main (by content)")
            save_entry(d, e)
            close_item_landed(st, e["id"], "its commits are on main (by content)")
            did.append(f"{e['id']}: its commits are on main (by content): landed")
            continue
        why = None
        if not submit_ref(repo, d, e, remote=True):
            why = f"could not push refs/heads/land/{e['id']}"
        else:
            try:
                st.submit({**{k: e.get(k) for k in SUBMIT_FIELDS}, "id": e["id"]})
            except (Exception, SystemExit) as ex:
                why = f"ci-runner's store refused or did not answer ({str(ex)[:160]})"
        if why is None:
            e.update(status="migrated", decided=ts(), why="moved to ci-runner's queue")
            save_entry(d, e)
            did.append(f"{e['id']}: moved to ci-runner's queue")
            continue
        line = (f"LOCAL QUEUE: {e['id']} ({e.get('worktree')}) is in the laptop's local queue, which nothing runs: {why}; "
                "tried again every sync cycle")
        did.append(line)
        if time.time() - float(e.get("migrate_told_at") or 0) > 3600:
            e["migrate_told_at"] = time.time()
            save_entry(d, e)
            inbox_write("land", line, [line], [], 1, cwd=e.get("worktree") or safe_cwd())
    return did


def change_rows(store=None):
    """THE QUEUE as everyone reads it (`land status`, the status line, the page): the store's open change items,
    by id, as {id, state, worktree, head, deadline, level, since (submitted)}."""
    rows = [{"id": i["id"], "state": i["state"], "worktree": (i.get("data") or {}).get("worktree"),
             "head": (i.get("data") or {}).get("head"), "deadline": i["deadline"], "level": i["level"],
             "since": float((i.get("data") or {}).get("submitted_at") or i.get("entered_at") or 0)}
            for i in (store or Store()).items("change") if not machine_state("change", i["state"]).terminal]
    return sorted(rows, key=lambda r: r["id"])


def rebase_in_tree(d, tree, e, base):
    """The entry's commits on base, in the tree: (head, None); (None, None) on a conflict (aborted); (None, via) when
    every commit is on base already (dropped: LANDED via that commit, never an empty push)."""
    git(tree, "checkout", "-q", "--force", "--detach", base)
    how, x = apply_entry(d, tree, e, base)
    if how == "conflict":
        return None, None
    if how in ("landed", "duplicate"):
        return None, x or "an equivalent commit"
    return git(tree, "rev-parse", "HEAD").strip(), None


def check_entry(repo, eid):
    """THE EXECUTOR of one change's checks (`waiting check <id>`, detached): holding check/<entry> and a check tree slot
    (leases), it takes the change to checking and leaves it checked (with the tip and base it passed on), rejected (a
    test failure, a conflict) or queued again (infra). It never pushes: the tick does (push_change)."""
    d, _c, _w = waiting_state(repo)
    lease = checker_lease(eid)
    if not lease.acquire():  # another checker holds it (and progresses)
        return 0
    own = entry_owned(eid)
    if not own.acquire():
        lease.release()
        return 0
    try:
        e = read_record(os.path.join(d, "entries", f"{eid}.json"))
        if not e or e.get("status") not in ("queued", "checking"):
            return 0
        slot = check_tree_slot(repo, d)
        if not slot:  # all trees busy: it stays queued, the tick starts it again when one frees
            return 0
        i, slot_h = slot
        try:
            with clone_shared(repo):  # a clone swap (repair_clone) waits for the check
                return _check_entry(repo, d, e, i, own)
        finally:
            slot_h.release()
    finally:
        own.release()
        lease.release()


def _check_entry(repo, d, e, i, own=None):
    hook = precheck_hook(repo)
    log_path = os.path.join(d, f"check-{e['id']}")
    if e.get("status") != "checking":
        set_change(d, e, "checking", event={"text": f"{e['id']}: checking"}, since=ts())
    fetched, ferr = fetch_main(repo)  # a corruption signature marks the clone for repair (clone_heal_tick)
    if not fetched:  # never checked on a stale main; git's own words say why (2026-10-29: "Could not read 409b8bb...")
        last = (ferr.strip().splitlines() or ["?"])[-1][:240]
        requeue_infra(d, e, f"{FETCH_MAIN_FAILED}: {last}", reason=FETCH_MAIN_FAILED, detail=ferr,
                      repo=repo)
        return 0
    base = git(repo, "rev-parse", f"{REMOTE}/main").strip()
    if not git_ok(repo, "cat-file", "-e", f"{e['head']}^{{commit}}"):  # phase 2: the change is on Forgejo, as land/<id>
        lr = git(repo, "fetch", "--quiet", REMOTE, f"refs/heads/land/{e['id']}", check=False)
        if not git_ok(repo, "cat-file", "-e", f"{e['head']}^{{commit}}"):
            lerr = (lr.stderr or "").strip()
            note_clone_suspect(repo, lerr)
            requeue_infra(d, e, f"its commits are not here and refs/heads/land/{e['id']} gave none"
                          + (f": {lerr.splitlines()[-1][:240]}" if lerr else ""), detail=lerr,
                          repo=repo)
            return 0
    rem = e.get("remote") or {}
    adopt = (rem.get("head") == e["head"] and rem.get("base") == base and not rem.get("collected_at")
             and rem.get("status") not in ("cancelled", "skipped", "abandoned")
             and git_ok(repo, "cat-file", "-e", f"{rem.get('tip')}^{{commit}}"))
    if adopt:  # a replacement checker (the last one lost its lease, died or re-exec'd mid-wait): the same tip, its run
        tree = check_tree(repo, i, rem["tip"])
        head, via = rem["tip"], None
    else:
        if rem.get("run") is not None and not rem.get("status"):
            cancel_entry_remote(e, "superseded by a new round of its checks")
        e.pop("remote", None)
        e.pop("remote_run", None)
        e["round_since"] = ts()
        save_entry(d, e)
        tree = check_tree(repo, i, base)
        head, via = rebase_in_tree(d, tree, e, base)
    if via:
        already_landed(d, e, via if via != "an equivalent commit" else None)
        return 0
    if not head:
        queue_verdict(d, e, "rejected", f"NOT LANDED: the change no longer applies on {REMOTE}/main {base[:10]} "
                      f"(main moved since QUEUED): rebase onto forgejo/main and land again")
        return 1
    e["tip"] = head
    save_entry(d, e)
    reds = gate_reds(repo) if waiting_supported(repo) else set()
    commits = waiting_commits(tree, base, head)
    joining = bool(reds) and must_wait(repo, commits, reds)
    cls = gov_class_of(repo, commits, reds)
    mode = "--checks-only" if joining else "--range"
    where = "no hook"
    if hook:
        ok, out, where = run_entry_checks(repo, d, e, tree, base, head, mode, cls, log_path, own)
        if ok is None:  # stalled: the laptop's, not the change's
            requeue_infra(d, e, f"its checks stalled ({where})")
            return 0
        if not ok and INFRA_CHECK_FAILURE.search(out or ""):
            requeue_infra(d, e, f"its checks failed on the laptop, not on the change "
                             f"({INFRA_CHECK_FAILURE.search(out).group(0)})")
            return 0
        if not ok and e.get("infra_reason") == NETWORK_REASON and network_failure(out):
            requeue_infra(d, e, f"{NETWORK_REASON} ({network_failure(out)})", reason=NETWORK_REASON,
                          detail=out, repo=repo)
            return 0
        if not ok:
            queue_verdict(d, e, "rejected", f"NOT LANDED: the pre-push checks failed ({where}); the change "
                          f"{e['head'][:10]} stays in {e['worktree']}", (out or "").splitlines())
            return 1
    set_change(d, e, "checked", data={"tip": head, "base": base, "mode": mode, "where": where},
               event={"text": f"{e['id']}: checked {head[:10]} on {base[:10]} ({where})"},
               tip=head, checked_base=base, checked_mode=mode, checked_where=where, tree=i, infra_reason=None, infra_same=0)
    wlog(d, f"{e['id']}: checked {head[:10]} on {base[:10]} ({where})")
    return 0


def push_change(repo, d, e):
    """The tick's push of one checked change, under the landing lease: rebased onto a moved main (re-checked when what
    landed overlaps: a new round, queued), joined to `waiting` while main is red and it must wait, else pushed with
    the push's bounds and verified. pushing -> landed, or back to checked (a push never rejects); after
    PUSH_ATTEMPTS_MAX failures a condition on the git route, and it waits for it."""
    slot = tree_slot(d, e.get("tree") or 1)  # its tree: never checked out under a checker that took the slot since
    if not slot.acquire():
        return "busy"
    try:
        return _push_change(repo, d, e)
    finally:
        slot.release()


def _push_change(repo, d, e):
    hook = precheck_hook(repo)
    log_path = os.path.join(d, f"check-{e['id']}")
    tree = check_tree_for_push(repo, e)
    with land_lock(repo, timeout=0) as held:
        if not held:
            return "busy"
        now_ = read_record(os.path.join(d, "entries", f"{e['id']}.json")) or {}
        if now_.get("status") != "checked":  # superseded / cancelled since its checks: no push
            return now_.get("status")
        fetch_main(repo)  # a corrupt clone is marked for repair; a failed fetch pushes onto the last known main
        moved = git(repo, "rev-parse", f"{REMOTE}/main").strip()
        base, head, where, skip = e["checked_base"], e["tip"], e.get("checked_where") or "", None
        if moved != base:
            new_head, via = rebase_in_tree(d, tree, e, moved)
            if via:
                already_landed(d, e, via if via != "an equivalent commit" else None)
                return "landed"
            if not new_head:
                queue_verdict(d, e, "rejected", f"NOT LANDED: the change no longer applies on {REMOTE}/main "
                              f"{moved[:10]} (main moved during its checks): rebase onto forgejo/main and land again")
                return "rejected"
            tree_hook = precheck_hook(tree) if hook else None
            mine = range_paths(tree, moved, new_head)
            n, why = recheck_reason(tree_hook, tree, base, moved, mine)
            if why is None and tree_hook:
                skip = hook_query(tree_hook, tree, "--plan", mine)
                if skip is None:
                    why = "the hook's plan could not be read"
            moved_by = f"main moved by {n} commit{'' if n == 1 else 's'}"
            if why is not None:  # a new round, not an attempt
                set_change(d, e, "queued", data={"rounds": e.get("rounds", 0) + 1},
                           event={"text": f"{e['id']}: {moved_by} since its checks, {why}: checked again"},
                           rounds=e.get("rounds", 0) + 1, retry_after=0)
                wlog(d, f"{e['id']}: {moved_by} during its checks, {why}: queued for a new round")
                return "queued"
            note = f"{moved_by}, no overlap: pushed without re-check"
            wlog(d, f"{e['id']}: {note}")
            where = f"{where}; {note}"
            e.pop("remote_run", None)
            base, head, e["tip"] = moved, new_head, new_head
        commits = waiting_commits(tree, base, head)
        reds = gate_reds(repo) if waiting_supported(repo) else set()
        if reds and must_wait(repo, commits, reds):
            return "joined" if queue_join(repo, d, e, tree, head) == 0 else "rejected"
        fixes = sorted(fixes_of(repo, commits))
        set_change(d, e, "pushing", event={"text": f"{e['id']}: pushing {head[:10]}"})
        pushed_at = time.time()
        env = tool_env()
        if e.get("remote_run"):  # the hook takes ci-runner's pass once Forgejo confirms it (remote checks)
            env["PREPUSH_REMOTE_RUN"] = str(e["remote_run"])
        if skip:  # checked on the old base, no overlap with what landed since: the hook judges the red gate only
            env["PREPUSH_SKIP"] = " ".join(skip)
        rc, out = run_watched(push_argv(REMOTE, head), tree, env, log_path, "push", **push_bounds())
        git(repo, "fetch", "--quiet", REMOTE, "main", check=False)
        if landed(repo, head, f"refs/remotes/{REMOTE}/main"):  # verify: the tip is on main, whatever rc said
            mark_gate_dirty()
            queue_verdict(d, e, "landed", f"LANDED {head} (queued {e['id']}, checked {where})", code=0,
                          landed_sha=head, pushed_at=pushed_at)
            close = None if e.get("keep_worktree") else e["worktree"]
            start_watch(repo, head, pushed_at, worktree=e["worktree"],
                        close=close if close and os.path.isdir(close) else None, session=e.get("session"))
            return "landed"
        if rc is not None and (RACE.search(out or "") or REDGATE.search(out or "")):
            set_change(d, e, "checked", event={"text": f"{e['id']}: the push raced or met a red gate: tried again"})
            return "checked"
        n = e.get("push_attempts", 0) + 1
        why = "the push stalled" if rc is None else ((TRANSIENT_PUSH.search(out or "") or [None])[0]
                                                     or f"the push failed (exit {rc})")
        set_change(d, e, "checked", data={"push_attempts": n},
                   event={"text": f"{e['id']}: {why}: push attempt {n}", "severity": "escalate" if n >= PUSH_ATTEMPTS_MAX else "info",
                          "audience": {"worktree": e.get("worktree")}},
                   push_attempts=n, retry_after=time.time() + min(60 * 2 ** n, 1800))
        wlog(d, f"{e['id']}: {why} (push attempt {n}); its tail: {tail_of(out or '')[:300]}")
        if n >= PUSH_ATTEMPTS_MAX:
            open_condition("STALLED", f"STALLED: the push of {e['id']} failed {n} times ({why}); it is tried again when "
                                      f"git can read {REMOTE}/main", run_probe("git_fetch", {"repo": repo}), [],
                           key=f"push-failing-{e['id']}", command="land", cwd=e.get("worktree"),
                           tail=(out or "").splitlines()[-15:])
        return "checked"


def check_tree_for_push(repo, e):
    """The tree the change was checked in (its tip is there), else a fresh check tree at its tip."""
    _d, clone, _w = waiting_state(repo)
    i = e.get("tree") or 1
    tree = f"{clone}-wt-check-{i}"
    if not os.path.isdir(tree) or not git_ok(tree, "cat-file", "-e", f"{e['tip']}^{{commit}}"):
        tree = check_tree(repo, i, e["tip"])
    git(tree, "checkout", "-q", "--force", "--detach", e["tip"], check=False)
    return tree


def tick_changes(repo, d, now_=None):
    """One tick over the change items: re-queueing re-derived (only the newest entry per worktree and branch runs), an
    executor started for each queued or lease-less checking change (red fixes first, while a slot is free), a
    checking change past its deadline back to queued (infra), a queued one past its deadline escalated, and each
    checked change pushed. Returns what it did."""
    t = now_ if now_ is not None else time.time()
    st = Store()
    try:
        reds = gate_reds(repo)
    except Exception:
        reds = set()
    todo = sorted(queued_entries(d), key=lambda e: (0 if reds and set(e.get("fixes") or []) & reds else 1, e["seq"]))
    newest = {}
    for e in sorted(todo, key=lambda e: e["seq"]):  # entries queued before re-queueing replaced: the newest one runs
        for k, older in list(newest.items()):
            if same_change_source(older, e["worktree"], e.get("branch") or ""):
                supersede(d, older, e["id"], f"{e['worktree']} queued again; only the newest entry runs")
                del newest[k]
        newest[e["id"]] = e
    did = []
    for e in [e for e in todo if e["id"] in newest]:
        it = st.put_item("change", e["id"], ENTRY_STATE.get(e["status"], "queued"),
                         {"worktree": e.get("worktree"), "head": e.get("head")})
        state = it["state"]
        if state == "pushing":  # a tick that died mid-push: verified next, never pushed twice blindly
            set_change(d, e, "checked", event={"text": f"{e['id']}: its push was interrupted: verified, then tried again"})
            state = "checked"
        if state == "checked":
            if e.get("retry_after", 0) > t:
                continue
            first = batch_goes_first(st, d)
            if first:  # a solo push now moves main under the batch, and its whole check is thrown away
                did += behind_batch(e, first, "push")
                continue
            did.append(f"{e['id']}: {push_change(repo, d, e)}")
            continue
        alive = checker_alive(e["id"])
        if state == "checking" and not alive and t > it["deadline"]:
            requeue_infra(d, e, "its check passed its deadline with no live checker")
            did.append(f"{e['id']}: re-queued (checking past its deadline)")
            continue
        if state == "queued" and t > it["deadline"]:  # waits past its deadline: said, re-deadlined (an escalation)
            st.transition("change", e["id"], "queued", it["version"],
                          event={"text": f"{e['id']}: queued for {fmt_age(t - it['entered_at'])}, waiting on "
                                         f"{e.get('waiting_on') or 'a check slot' if not e.get('retry_after', 0) > t else 'its retry'}",
                                 "audience": {"worktree": e.get("worktree")}})
        if alive or e.get("retry_after", 0) > t:
            continue
        first = batch_goes_first(st, d, ("assembling", "pushing"))
        if first:  # the next free check tree is the batch's: to assemble it, or to push from its own tree
            did += behind_batch(e, first, "check")
            continue
        slot = check_tree_slot(repo, d)
        if not slot:
            continue
        slot[1].release()  # the executor takes it again itself
        spawn_executor(repo, d, e["id"])
        did.append(f"{e['id']}: executor started")
    return did


def spawn_executor(repo, d, eid):
    """`agentctl waiting check <eid>`, detached: the change's checks (check_entry)."""
    with open(os.path.join(d, "processor.log"), "a") as logf:
        spawn_detached([sys.executable, TOOL_PATH, "waiting", "check", eid, "--repo", repo],
                         stdin=subprocess.DEVNULL, stdout=logf, stderr=logf, start_new_session=True, cwd=repo,
                         env=detached_env(repo))
    time.sleep(1)  # it takes its tree slot before the next looks


def queue_join(repo, d, e, tree, head):
    """Under the landing lock, main red: the checked change joins `waiting` (land's join, from the check tree)."""
    a = argparse.Namespace(follow=False)
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = join(repo, f"refs/remotes/{REMOTE}/main", lambda *x: subprocess.run(x, cwd=tree, capture_output=True,
                                                                                  text=True, env=tool_env()),
                  os.path.join(d, f"check-{e['id']}"), a, head=head, worktree=e["worktree"],
                  branch=e.get("branch") or "")
    line = (out.getvalue().strip().splitlines() or [""])[-1]
    if rc == 0:
        set_change(d, e, "waiting", event=change_event(e, line, code=None, final=False), decided=ts(),
                   waiting_id=a.waiting["id"])
        wlog(d, f"{e['id']}: {line}")
        return 0
    queue_verdict(d, e, "rejected", line or "NOT LANDED: could not join `waiting`")
    return 1


def run_entry_checks(repo, d, e, tree, base, head, mode, cls, log_path, own=None):
    """(ok, output, where) of the change's checks: on the CI tier when the repo has the remote check workflow (owner
    decision 1; the reconciler is ci-runner's, the laptop runs no checks since its tick went), else in the tree,
    admitted by the governor. ok None: stalled."""
    adopted = remote_matches(e, base, head, mode)
    why = "its remote run, adopted" if adopted else "the CI tier (owner decision 1)"
    if adopted or remote_checks_supported(tree):
        wlog(d, f"{e['id']}: checks on {why}")
        ok, out = remote_check(repo, e, base, head, mode, own)
        if ok is not None:
            return ok, out, f"on ci-runner ({why})"
        wlog(d, f"{e['id']}: the remote check gave no verdict ({out.splitlines()[-1] if out else '?'}): checking here")
    # the range through the remote's ref (forgejo/main is `base` now, fetched just before): the hook finds the remote, and
    # with it Forgejo's red gate, from it -- given bare shas it fell back to gh (GitHub, suspended): the gate unread
    rng = f"{REMOTE}/main..{head}" if git(tree, "rev-parse", f"{REMOTE}/main").strip() == base else f"{base}..{head}"
    e["waiting_on"] = "the governor (a check slot)"
    save_entry(d, e)
    try:
        with check_slot(repo, f"queued {e['id']} {head[:10]}", cls) as slot:
            e["waiting_on"] = None
            save_entry(d, e)
            rc, out = run_watched([os.path.join(tree, "scripts", "hooks", "pre-push"), mode, rng], tree,
                                  slot.env(tool_env()), log_path, "checks", sampler=slot.sample)
            slot.done(rc, out)
    except GovTimeout as ex:  # an infra failure of the laptop, re-queued by the caller (INFRA_CHECK_FAILURE)
        return False, f"TIMEOUT 0s {ex}", "locally (never admitted)"
    with open(os.path.join(d, "checks.log"), "a") as f:
        f.write(f"=== {ts()} {e['id']} {base[:10]}..{head[:10]} {mode} rc {rc}\n{out}\n")
    return (None if rc is None else rc == 0), out, f"locally, check tree {os.path.basename(tree)}"
