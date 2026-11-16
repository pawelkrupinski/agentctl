# agentctl, part `queue`: the land queue and submission.
# Runs inside agentctl's one namespace, after the parts before it in lib/agentctl/__init__.py's PARTS (it uses their names).

# ---------------------------------------------------------------- the land queue: agents never wait on a land
# The owner (2026-10-27): "agents never wait on a land". `land` rebases the change onto origin/main, runs the squash
# guard, enqueues it and returns at once: `QUEUED <id> (<sha>)`. The always-on `waiting` processor (launchd
# net.bitcashier.agentctl-waiting) runs each queued change's checks in a CHECK TREE of its own (a pool of detached
# worktrees <clone>-wt-check-<n>, kept warm between checks, seeded from the shared build cache when cold), admitted by
# the governor (a check; red for a Fixes-Red fix of a red run, which goes first) -- or on ci-runner's check tier when the
# governor reports pressure (see "remote checks") -- then, under the landing lock: main green (or a change that may land
# while red), the checked tip is pushed (`LANDED`, and a detached watch); main red, it joins `waiting` (`WAITING`). Main
# moved meanwhile: rebased in the tree and checked again. Every verdict goes to the inbox with the agent's worktree.
# `land --follow` keeps the old in-process land (checks, push, watch) for scripts; a red fixer queues and waits
# (fixer_land).
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


# Main moved between a change's check and its push: FULLY re-checked only when what landed meanwhile can change what the
# full check judged (the owner, 2026-10-27: memo-key -41 passed runs 3341 and 3350, then main moved and it was checked a
# third time). FILE GRANULARITY (the owner, 2026-11-16, "but always recompile and unit test": at module granularity
# nearly every change shared worker or scrapers with what landed, and changes looped 3-4 full rounds of 15-20 min, some
# over two test files in worker/src/test). THE RULE, judged against the base of the change's last FULL check:
#   - what landed changes the build or the hook (RECHECK_ALWAYS, scripts/ci/precommit-share.sh included: it picks what
#     each CI share of the check runs) -- but not the hook's own self-tests (HOOK_SELF_TEST: scripts/hooks/*-test.sh,
#     *-test-lib.sh, *_test.py; the owner, 2026-11-16): no landing check runs one but by its `scripttests` check, for
#     a change selecting it, like any script's test, so they get the verification round below -- or
#   - a path both sides change (added, modified or deleted; a rename counts as both of its paths), or
#   - a path one side deletes (a rename's old path included) whose file name a file the other side changes names
#     (`git grep -F <basename>` over those files at the rebased tip: a script, fixture or test reading it by name; a
#     Scala reference by class name the combined compile below catches)
# -> a new FULL round. Otherwise no full re-check, but ALWAYS the COMBINED-TREE VERIFICATION (VERIFY_MODE): the rebased
# tip checked once more, on the CI tier like any check, with the hook's `--verify-only <full base>..<tip>` -- every test
# config compiled, the lint and fast specs, and the unit tests of every module either side touches -- then pushed with
# the change's own checks skipped at the push (PREPUSH_SKIP: the hook's plan for its paths; the red gate still runs).
# A hook without --verify-only gets the full round, as before the rule. A rebase conflict is NOT LANDED, as ever.
RECHECK_ALWAYS = re.compile(r"^(build\.sbt|project/.*|scripts/hooks/.*|scripts/hooks-path\.sh|scripts/tool-conventions\.sh"
                            r"|scripts/ci/precommit-share\.sh)$")
HOOK_SELF_TEST = re.compile(r"^scripts/hooks/[^/]+(-test\.sh|-test-lib\.sh|_test\.py)$")
VERIFY_MODE = "--verify-only"


def range_paths(cwd, a, b, deleted=False):
    """The paths a..b changes (both sides of a rename); deleted: only those it deletes (a rename's old path too)."""
    flt = ["--diff-filter=D"] if deleted else []
    return [p for p in git(cwd, "diff", "--name-only", "--no-renames", *flt, a, b).splitlines() if p]


def hook_query(hook, tree, flag, paths):
    """The hook's dry-run answer for paths on stdin (`--plan`, `--compile-plan`), as words; None when unreadable."""
    try:
        r = subprocess.run([hook, flag], cwd=tree, input="".join(f"{p}\n" for p in paths), capture_output=True,
                           text=True, timeout=120, env=tool_env())
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout.split() if r.returncode == 0 else None


def verify_supported(hook):
    """Whether the hook (a path) has the combined-tree verification mode."""
    try:
        with open(hook) as f:
            return VERIFY_MODE in f.read()
    except (OSError, TypeError):
        return False


def named_deletions(tree, tip, deleted, readers):
    """(deleted path, reader) of a deleted path whose file name a reader names at tip; None when none does."""
    readers = [p for p in readers if git_ok(tree, "cat-file", "-e", f"{tip}:{p}")]
    if not deleted or not readers:
        return None
    for p in sorted(deleted):
        r = git(tree, "grep", "-l", "-F", "-e", os.path.basename(p), tip, "--", *readers, check=False)
        hits = sorted(line.split(":", 1)[1] for line in r.stdout.splitlines() if ":" in line)
        if hits:
            return p, hits[0]
    return None


def forces_full_recheck(path):
    """Whether a path landing meanwhile forces a FULL re-check whatever the change touches (THE RULE's first clause)."""
    return bool(RECHECK_ALWAYS.match(path)) and not HOOK_SELF_TEST.match(path)


def recheck_reason(tree, base, moved, tip, change_paths):
    """(commits main moved by, why the change must be FULLY checked again or None) -- THE RULE above, of what landed in
    base..moved against the change's paths (moved..tip, the rebased change)."""
    n = len(git(tree, "rev-list", f"{base}..{moved}", check=False).stdout.split())
    landed_paths = range_paths(tree, base, moved)
    build = sorted(p for p in landed_paths if forces_full_recheck(p))
    if build:
        return n, f"they change {build[0]}"
    same = sorted(set(landed_paths) & set(change_paths))
    if same:
        return n, f"both touch {same[0]}" + (f" (+{len(same) - 1} more)" if len(same) > 1 else "")
    for deleted, readers, whose in ((range_paths(tree, base, moved, deleted=True), change_paths, "they delete"),
                                    (range_paths(tree, moved, tip, deleted=True), landed_paths, "it deletes")):
        hit = named_deletions(tree, tip, deleted, readers)
        if hit:
            return n, f"{whose} {hit[0]}, which {hit[1]} names"
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
    """Whether land queues: a repo whose hook can check a change apart (movies), unless --follow / --no-watch
    (scripts) or AGENTCTL_QUEUE=0. A dispatched red fixer queues too (RED FIX LANDS THROUGH THE QUEUE, below)."""
    if getattr(a, "follow", False) or getattr(a, "no_watch", False):
        return False
    return os.environ.get("AGENTCTL_QUEUE", "1") != "0" and batching(repo)


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
            "fixes": sorted(fixes_of(repo, commits)), "keep_worktree": bool(getattr(a, "keep_worktree", False)),
            "claude_pid": int(os.environ["CLAUDE_PID"]) if (os.environ.get("CLAUDE_PID") or "").isdigit() else None,
            "session": os.environ.get("CLAUDE_CODE_SESSION_ID") or None,
            "base": git(repo, "rev-parse", main_ref).strip(), "host": os.uname().nodename,
            # THE LANDER (lander.py): who landed it, and each commit's keys to find it on main after the rebase
            "lander": lander_record(repo), "commit_keys": commit_keys(repo, main_ref, head)}


# THE LAPTOP'S LAND NEEDS NO VAULT (2026-10-31: agents' `land` still died NoToken -- a `Fixes-Red:` trailer made
# change_fields read the reds from ci-runner's store, the token read failed in a session whose vault was locked, and
# fix_check let the NoToken (a SystemExit) through; the 10-30 fix had only let agents run `secrets` themselves, which
# prompts). The submission's git half is the land/<id> push over ssh; its store half takes a token from the
# environment or the processor's socket, never `secrets` (no_vault). With none, the change waits in the outbox and the
# laptop's sync -- which holds the token -- submits it; the reconciler checks the Fixes-Red trailers again at the push.
def submit_to_server(repo, d, fields, note="", after_queued=None):
    """THE LAPTOP'S ONLY WAY INTO THE QUEUE (phase 2; it holds no queue of its own): the change pushed to
    refs/heads/land/<id>, then submitted to ci-runner's queue (the store unreachable, or no token here: the outbox,
    which the sync sends). Prints QUEUED / QUEUED-LOCAL / NOT LANDED; returns land's exit code."""
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
        print(f"QUEUED-LOCAL {e['id']} ({head[:10]}){note}: {why}. LANDED / WAITING / NOT LANDED goes to "
              "`agentctl inbox`", flush=True)
        if after_queued:
            after_queued(e["id"])
        return 0
    ahead = r.get("ahead") or 0
    print(f"QUEUED {r['id']} ({head[:10]}{f', {ahead} ahead' if ahead > 0 else ''}){note}: its checks run on ci-runner; "
          "LANDED / WAITING / NOT LANDED goes to `agentctl inbox`", flush=True)
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


def cmd_land_precheck(a):
    """The detached half of start_fast_precheck: run the repo's precheck, and on a refusal cancel the queued change."""
    # Its own process group, ended WHOLE at the bound (review 2026-11-07: subprocess.run killed only the script, so what
    # it started -- sbt, a test JVM -- lived on, and kept the output pipe, and so this wait, open): run_watched.
    rc, out = run_watched([os.path.join(a.repo, FAST_PRECHECK), "--base", a.base, "--head", a.head], a.repo, None,
                          os.path.join(scratch(), f"agentctl-precheck-{a.entry}"), "precheck",
                          stall=FAST_PRECHECK_BOUND, wall=FAST_PRECHECK_BOUND)
    if rc is None:
        print(f"{out or ''}PRECHECK UNCHECKED: past its {FAST_PRECHECK_BOUND} s bound, ended with its children: "
              "could not check, its landing check judges it", flush=True)
        return 0
    lines = (out or "").splitlines()
    print("\n".join(lines), flush=True)
    if rc != 1:
        return 0
    last = next((l for l in reversed(lines) if l.startswith("PRECHECK ")), lines[-1] if lines else "exit 1")
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
    a.queued_id = None

    def after_queued(eid):
        a.queued_id = eid
        if not fixer:
            start_fast_precheck(repo, main_ref, head, eid, a)
    if host_role() == "sync":  # THE LAPTOP'S LAND NEEDS NO VAULT (see submit_to_server)
        with no_vault():
            rc = submit_to_server(repo, d, change_fields(repo, main_ref, head, a), after_queued=after_queued)
    else:
        fields = change_fields(repo, main_ref, head, a)
        e, ahead = queue_change(d, fields)
        submit_ref(repo, d, e)
        print(f"QUEUED {e['id']} ({head[:10]}{f', {ahead} ahead' if ahead > 0 else ''}): its checks run in the "
              "background; LANDED / WAITING / NOT LANDED goes to `agentctl inbox`", flush=True)
        after_queued(e["id"])
        spawn_waiting_processor(repo)
        offer_token(repo)
        rc = 0
    if rc != 0 or not fixer or not a.queued_id:
        return rc
    return fixer_land(repo, main_ref, head, a.queued_id, a)


# A RED FIX LANDS THROUGH THE QUEUE (2026-11-16, Main #2515: a fixer's land, in-process since the queue began, ran the
# whole pre-push hook on the laptop -- its identitydrift step waited 75 min on the starved measure queue -- while its
# fixer's own `timeout` killed it twice and its 180-min turn ran out; the queue's 45-s precheck would have refused the
# missing Drift-Slice: trailer at once). A fixer's change is queued like every change -- the CI tier, red fixes first
# -- and its land runs the fast precheck in the FOREGROUND (its refusal is the fixer's NOT LANDED in seconds), then
# waits for the change's verdict, bounded: LANDED <sha> / NOT LANDED: <why>, the lines its wrapper reads.
FIXER_LAND_WAIT = 2 * 3600  # s a fixer's land waits for its queued change's verdict ($AGENTCTL_FIXER_LAND_WAIT)
FIXER_LAND_POLL = (15, 60)  # s: the await's backoff, first and cap


def fixer_land(repo, main_ref, head, eid, a):
    if not getattr(a, "no_fast_precheck", False) and os.access(os.path.join(repo, FAST_PRECHECK), os.X_OK):
        base = git(repo, "rev-parse", main_ref).strip()
        if cmd_land_precheck(argparse.Namespace(entry=eid, repo=repo, base=base, head=head)) == 1:
            return 1  # its NOT LANDED (or PRECHECK REFUSED) line is the last printed
    return await_queued(eid)


def await_queued(eid, store=None):
    """Block until queued change eid is decided (its store item; through a store blip, asked again), bounded by
    FIXER_LAND_WAIT: LANDED <sha> (0), NOT LANDED: <why> (1), or NOT LANDED (still queued) (1)."""
    t0, delays = time.time(), backoff(*FIXER_LAND_POLL)
    bound = env_num("AGENTCTL_FIXER_LAND_WAIT", FIXER_LAND_WAIT)
    state = "unread"
    while True:
        try:
            it = (store or Store()).item("change", eid)
        except StoreUnavailable as e:
            it, state = None, f"the store did not answer ({str(e)[:80]})"
        if it:
            state, data = it["state"], it.get("data") or {}
            if state == "landed":
                sha = data.get("tip") or next(iter(data.get("landed_commits") or {}), "") or data.get("head")
                print(f"LANDED {sha} (queued {eid})", flush=True)
                return 0
            if machine_state("change", state).terminal:
                why = str(data.get("why") or f"{eid} {state}")
                print(why if why.startswith("NOT LANDED") else f"NOT LANDED: {why}", flush=True)
                return 1
        if time.time() - t0 >= bound:
            print(f"NOT LANDED (still queued): {eid} is {state} after {fmt_age(time.time() - t0)}; it stays queued as "
                  "its red's fix (the red machine verifies it once it lands) -- `agentctl land status`", flush=True)
            return 1
        time.sleep(next(delays))


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


# A RED FIX NEVER WAITS FOR GREEN (the owner, 2026-11-16: "Red fix should never wait for green build (obv)"): every
# place a change can be held for main or for other changes exempts a change fixing an open gate red (red_fix): it takes
# THE RED-FIX TREE no ordinary change may take (08:58-09:15Z: Main #2515's fix sat "queued for 15 min, waiting on a
# check slot" behind three ordinary checks), it goes before an open batch, a known red never parks it on `waiting`,
# must_wait lets it go (the hook's rule), and the watchdog's WR escalates one that waits anyway.
RED_FIX_TREE = "redfix"  # <clone>-wt-check-redfix, under its own lease: only a red fix's checker takes it


def red_fix(e, reds):
    """Whether change entry e fixes an open gate red: a `Fixes-Red:` naming a run of the current episode."""
    return bool(reds) and bool(set(map(str, e.get("fixes") or [])) & set(reds))


def check_tree_slot(repo, d, fix=False):
    """(index, LeaseHold) of a free check tree slot, or None when all QUEUE_POOL are busy; a red fix (fix) tries the
    red-fix tree first."""
    for i in ([RED_FIX_TREE] if fix else []) + list(range(1, int(env_num("AGENTCTL_CHECK_TREES", QUEUE_POOL)) + 1)):
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


def change_item_fields(e):
    """A change item's data at its creation, from its queue entry: where it is, its lander, and the reds it claims to
    fix (`fixes`: the watchdog's WR reads them; a red fix never waits for green)."""
    return {"worktree": e.get("worktree"), "head": e.get("head"), **lander_fields(e),
            **({"fixes": sorted(e["fixes"])} if e.get("fixes") else {})}


def set_change(d, e, to, data=None, event=None, store=None, **fields):
    """THE writer of a change's state: its item (transition + event) and its entry's status, together."""
    st = store or Store()
    cur = st.put_item("change", e["id"], ENTRY_STATE.get(e.get("status"), "queued"), change_item_fields(e))
    if fields.get("why"):  # a decided change's why, on its item: a fixer's land reads it there (await_queued)
        data = {**(data or {}), "why": str(fields["why"])[:600]}
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
_landed_looked = process_cache({})


def change_landed(repo, head, commits=None):
    """(landed, via): landed True when the change at head is on main by content (head an ancestor, or every commit of it
    patch-equivalent there or with a content twin, cherry_landed), False when not, None when this clone cannot tell (head unknown here); via the main commit
    carrying its last commit, when known."""
    main = f"refs/remotes/{remote()}/main"
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
        if not submit_ref(repo, d, e, to_server=True):
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
        slot = check_tree_slot(repo, d, fix=red_fix(e, gate_reds(repo)) if e.get("fixes") else False)
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
    base = git(repo, "rev-parse", f"{remote()}/main").strip()
    if not git_ok(repo, "cat-file", "-e", f"{e['head']}^{{commit}}"):  # phase 2: the change is on the remote, as land/<id>
        lr = git(repo, "fetch", "--quiet", remote(), f"refs/heads/land/{e['id']}", check=False)
        if not git_ok(repo, "cat-file", "-e", f"{e['head']}^{{commit}}"):
            lerr = (lr.stderr or "").strip()
            note_clone_suspect(repo, lerr)
            requeue_infra(d, e, f"its commits are not here and refs/heads/land/{e['id']} gave none"
                          + (f": {lerr.splitlines()[-1][:240]}" if lerr else ""), detail=lerr,
                          repo=repo)
            return 0
    rem = e.get("remote") or {}
    # ON ITS OWN BASE (2026-11-15 22:42Z: the reconciler's restart on a new pin killed its detached checkers; their
    # replacements, after main had moved, cancelled one half-done run and threw a green one away -- this adoption once
    # demanded the CURRENT main, from before the push learned to judge a moved main, e6a5b93). The run is adopted on
    # the base it checked, while main still holds that base; the push then judges what landed since, exactly as for a
    # checker that had survived: re-checked when it overlaps, pushed without re-check when it does not.
    adopt = (rem.get("head") == e["head"] and bool(rem.get("base")) and not rem.get("collected_at")
             and rem.get("status") not in ("cancelled", "skipped", "abandoned")
             and git_ok(repo, "cat-file", "-e", f"{rem.get('tip')}^{{commit}}")
             and git_ok(repo, "merge-base", "--is-ancestor", rem["base"], base))
    if adopt:  # a replacement checker (the last one lost its lease, died or re-exec'd mid-wait): the same tip, its run
        tree = check_tree(repo, i, rem["tip"])
        head, via, base = rem["tip"], None, rem["base"]
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
        queue_verdict(d, e, "rejected", f"NOT LANDED: the change no longer applies on {remote()}/main {base[:10]} "
                      f"(main moved since QUEUED): rebase onto {remote()}/main and land again")
        return 1
    e["tip"] = head
    save_entry(d, e)
    reds = gate_reds(repo) if waiting_supported(repo) else set()
    commits = waiting_commits(tree, base, head)
    dead = blame_land_refusal(tree, commits, e.get("worktree"))
    if dead:  # queued before its blame was withdrawn or refuted (2026-11-01: revert-629 re-queued by orphan duty)
        queue_verdict(d, e, "rejected", f"NOT LANDED: {dead}")
        return 1
    joining = bool(reds) and must_wait(repo, commits, reds)
    cls = gov_class_of(repo, commits, reds)
    mode = "--checks-only" if joining else "--range"
    if adopt and rem.get("mode") == VERIFY_MODE:
        mode = VERIFY_MODE
    elif e.get("verify") and hook:  # THE RULE (recheck_reason) again, on the main this round checks on
        _n, why = recheck_reason(tree, e.get("full_base") or e.get("checked_base") or base, base, head,
                                 range_paths(tree, base, head))
        if why is None and verify_supported(precheck_hook(tree)):
            mode = VERIFY_MODE
        else:
            wlog(d, f"{e['id']}: {why or f'the hook has no {VERIFY_MODE}'}: fully checked, not only verified")
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
        if not ok and CHECK_TIMEOUT.search(out or "") and int(e.get("timeout_retries") or 0) < TIMEOUT_RETRIES:
            e["timeout_retries"] = int(e.get("timeout_retries") or 0) + 1
            requeue_infra(d, e, f"its checks ran past their time cap ({where}; retry {e['timeout_retries']} of "
                                f"{TIMEOUT_RETRIES})", reason="its checks ran past their time cap", detail=out, repo=repo)
            return 0
        known_red = None
        if not ok:
            side, what = judge_failure(repo, d, e, base, out)
            if side == "head":
                timed = " (and they ran past their time cap, " \
                        f"{TIMEOUT_RETRIES + 1} times)" if CHECK_TIMEOUT.search(out or "") else ""
                queue_verdict(d, e, "rejected", f"NOT LANDED: the pre-push checks failed ({where}){timed}; failing "
                              f"with this change only: {keys_text(what)}; the change {e['head'][:10]} stays in "
                              f"{e['worktree']}", (out or "").splitlines())
                return 1
            srcs = sorted(set(what.values()))
            known_red = {"keys": sorted(what), "where": srcs, "base": base}
            wlog(d, f"{e['id']}: its checks failed only where its base fails too ({keys_text(what)}; {'; '.join(srcs)})"
                    ": it waits behind that red")
    full_base = (e.get("full_base") or e.get("checked_base") or base) if mode == VERIFY_MODE else base
    verified = f"verified {head[:10]} on {base[:10]} (fully checked on {full_base[:10]})" if mode == VERIFY_MODE \
        else f"checked {head[:10]} on {base[:10]}"
    set_change(d, e, "checked", data={"tip": head, "base": base, "mode": mode, "where": where},
               event={"text": f"{e['id']}: {verified} ({where})"
                      + (" -- failing only as its base fails" if known_red else "")},
               tip=head, checked_base=base, full_base=full_base, checked_mode=mode, checked_where=where, tree=i,
               infra_reason=None, infra_same=0, known_red=known_red, verify=False)
    wlog(d, f"{e['id']}: {verified} ({where})")
    return 0


# THE DIFFERENTIAL LANDING CHECK (2026-11-04: two good changes, credited-memo and G1 retention, were NOT LANDED because
# their checks failed on `uk-mowlem-theatre-purbeck-film-festival-hen-736301b7 lacks answers`, a stale slice failing on
# main itself; each author diagnosed it, landed again and alerted). The check judged the head absolutely; now, like
# the convergence gate, relative to its base. A failed check's FAILURE KEYS (the failing tests: a scalatest
# `- <name> *** FAILED ***`, an sbt `Failed tests:` class) are compared with the base's: main's red gate runs' failed
# logs (cached per run) and the other queued changes' failures on the same base (the queue's own record: two
# unrelated changes failing the same test on one main is main's). Every failing job explained by base keys: the change
# is no culprit -- it joins `waiting` behind that red (lands with the batch at green, or with the red's fix), ONE
# author note naming the tests. A head-only key, or a failing job with no key to judge by: rejected, naming them.
# And a run stopped at its 80-minute cap is no verdict on the change (69 of the week's 145 "pre-push checks failed",
# under ci-runner load 30..90 on 14 cores): re-queued as infra, TIMEOUT_RETRIES times, then rejected as too slow.
FAIL_TEST = re.compile(r"(?:^|\s)-\s+(.+?)\s+\*\*\* FAILED \*\*\*")
FAIL_CLASS = re.compile(r"\[error\]\s+([a-z_][\w]*(?:\.[\w]+)*\.[A-Z]\w*)\s*$")
CHECK_TIMEOUT = re.compile(r"ran past \d+ minutes and were stopped")
TIMEOUT_RETRIES = 2
BASE_CORPUS_AGE = 24 * 3600


def test_keys(lines):
    """{key: name} of the failing tests the lines name: the key normalized (numbers, shas out), the name as printed."""
    out = {}
    for raw in lines:
        line = ANSI.sub("", raw).replace("\t", " ").rstrip()
        m = FAIL_TEST.search(line)
        if m:
            out["test " + norm_line(m.group(1))] = m.group(1).strip()
            continue
        m = FAIL_CLASS.search(line)
        if m:
            out["class " + m.group(1)] = m.group(1)
    return out


def failure_keys(jobs):
    """({key: name}, unjudged units) of failed jobs' lines ({job: [lines]}). The unit judged is each failed hook check
    (`pre-push <check> FAILED`, its lines those since the check before: one job runs many checks, and a shellcheck
    failure must not hide behind a base-red test in the same job), else the job. A unit failing with no test line is
    unjudged (nothing to compare: its failure counts as the change's)."""
    keys, unjudged = {}, []
    for job, lines in (jobs or {}).items():
        seg, failed, units = [], False, []
        for raw in lines:
            m = HOOK_STATUS.search(ANSI.sub("", raw))
            if m:
                if m.group(2) == "FAILED":
                    failed = True
                    units.append((f"{job}: {m.group(1)}", seg))
                seg = []
                continue
            seg.append(raw)
        if not failed:
            units = [(job, lines)]
        for unit, ls in units:
            mine = test_keys(ls)
            keys.update(mine)
            if not mine:
                unjudged.append(unit)
    return keys, sorted(unjudged)


def log_jobs(log_text):
    """{job: [lines]} of `gh run view --log-failed` output (job<TAB>step<TAB>line), timestamps and colours off."""
    jobs = {}
    for job, line in log_entries(log_text):  # the runner's step-script headers left out (wait.py, STEP_ECHO)
        jobs.setdefault(job, []).append(line)
    return jobs


def jobs_of_out(out):
    """{job: lines} of a check's output: a remote check's `-- <job>` sections, else one job."""
    jobs, cur = {}, "checks"
    for line in (out or "").splitlines():
        if line.startswith("-- "):
            cur = line[3:].strip() or "?"
            jobs.setdefault(cur, [])
            continue
        jobs.setdefault(cur, []).append(line)
    if set(jobs) - {"checks"}:  # the run's header line (`... failed: <url>`) is no job
        jobs.pop("checks", None)
    return {j: ls for j, ls in jobs.items() if ls or j != "checks"}


def head_failure_jobs(repo, e, out):
    """The head's failed jobs, from the run's whole failed log when the remote run is known (the verdict's output
    keeps 25 relevant lines a job, without the hook's per-check status: a head-only failure must not hide), else
    from the output."""
    run = (e.get("remote") or {}).get("run") or e.get("remote_run")
    if run:
        try:
            return log_jobs(fetch_failed_log(slug(repo), run))
        except (Exception, SystemExit) as ex:
            err(f"failed log of run {run} unread ({str(ex)[:120]}): judged from the verdict's lines")
    return jobs_of_out(out)


def run_failure_keys(o_r, run_id):
    """The failure keys of one finished run of main (cached: a finished run's log never changes)."""
    cache = reds_dir("runkeys", f"{run_id}.json")
    rec = read_record(cache)
    if rec is not None:
        return set(rec.get("keys") or [])
    keys = set(test_keys(fetch_failed_log(o_r, run_id).splitlines()))
    os.makedirs(os.path.dirname(cache), exist_ok=True)
    write_json(cache, {"run": run_id, "at": ts(), "keys": sorted(keys)})
    return keys


def base_failure_keys(repo, d, e, base):
    """{key: where} failing on the change's base: main's red gate runs, and other changes' failed checks on this
    base (a different head) in the last day."""
    out = {}
    try:
        o_r = slug(repo)
        if o_r in GATING:
            for name, decided, _newer, _streak in gate_runs(o_r):
                if decided and decided.get("conclusion") != "success" and decided.get("databaseId"):
                    try:
                        for k in run_failure_keys(o_r, decided["databaseId"]):
                            out.setdefault(k, f"{name} #{decided.get('number') or decided['databaseId']} on main")
                    except (Exception, SystemExit) as ex:
                        err(f"{name} run {decided['databaseId']}: failed log unread ({str(ex)[:120]})")
    except (Exception, SystemExit) as ex:
        err(f"the gate's runs unread ({str(ex)[:120]}): judged against the queue's own record only")
    now_ = time.time()
    for o in waiting_entries(d, open_only=False):
        f = o.get("failed_on") or {}
        if o.get("id") == e.get("id") or o.get("head") == e.get("head") or f.get("base") != base:
            continue
        if now_ - float(f.get("at") or 0) > BASE_CORPUS_AGE:
            continue
        for k in f.get("keys") or []:
            out.setdefault(k, f"change {o['id']} on the same main")
    return out


def judge_failure(repo, d, e, base, out):
    """('base', {key: where}) when every failing job's tests fail on the base too; else ('head', [what is the
    change's own]). The head's keys are kept on the entry (failed_on), the queue's own record of the base."""
    jobs = head_failure_jobs(repo, e, out)
    keys, unjudged = failure_keys(jobs)
    e["failed_on"] = {"base": base, "head": e.get("head"), "keys": sorted(keys), "at": time.time()}
    save_entry(d, e)
    if CHECK_TIMEOUT.search(out or ""):  # checks it never reached are unjudged: never a base verdict
        return "head", sorted(keys.values()) + ["the checks' time cap"]
    if not keys:
        return "head", [f"{u} (no failing test named)" for u in unjudged] or ["(no failing test named)"]
    known = base_failure_keys(repo, d, e, base)
    mine = sorted(n for k, n in keys.items() if k not in known)
    if mine or unjudged:
        return "head", mine + [f"{u} (no failing test named)" for u in unjudged]
    return "base", {keys[k]: known[k] for k in sorted(keys)}


def keys_text(keys, n=4):
    keys = list(keys)
    return ", ".join(keys[:n]) + (f" (+{len(keys) - n} more)" if len(keys) > n else "")


def push_change(repo, d, e):
    """The tick's push of one checked change, under the landing lease: rebased onto a moved main (re-checked when what
    landed overlaps: a new round, queued), joined to `waiting` while main is red and it must wait, else pushed with
    the push's bounds and verified. pushing -> landed, or back to checked (a push never rejects); after
    PUSH_ATTEMPTS_MAX failures a condition on the git route, and it waits for it."""
    # Its own tree first (its tip and build are there), never checked out under a checker that took the slot since;
    # held, the PUSH TREE: a checker holds its slot for its remote run's whole wait (up to CHECK_LEASE_MAX), and a
    # push waiting for it sat `busy` 30 min past its deadline (2026-11-06, three changes, every tree held).
    for i in (e.get("tree") or 1, PUSH_TREE):
        slot = tree_slot(d, i)
        if slot.acquire():
            try:
                return _push_change(repo, d, e, i)
            finally:
                slot.release()
    return "busy"


PUSH_TREE = "push"  # <clone>-wt-check-push, under its own lease; no checker ever takes it (check_tree_slot: 1..N,
# and a red fix's RED_FIX_TREE)


def _push_change(repo, d, e, i):
    hook = precheck_hook(repo)
    log_path = os.path.join(d, f"check-{e['id']}")
    tree = check_tree_for_push(repo, e, i)
    with land_lock(repo, timeout=0) as held:
        if not held:
            return "busy"
        now_ = read_record(os.path.join(d, "entries", f"{e['id']}.json")) or {}
        if now_.get("status") != "checked":  # superseded / cancelled since its checks: no push
            return now_.get("status")
        fetch_main(repo)  # a corrupt clone is marked for repair; a failed fetch pushes onto the last known main
        moved = git(repo, "rev-parse", f"{remote()}/main").strip()
        base, head, where, skip = e["checked_base"], e["tip"], e.get("checked_where") or "", None
        full_base = e.get("full_base") or base
        if moved != base:
            new_head, via = rebase_in_tree(d, tree, e, moved)
            if via:
                already_landed(d, e, via if via != "an equivalent commit" else None)
                return "landed"
            if not new_head:
                queue_verdict(d, e, "rejected", f"NOT LANDED: the change no longer applies on {remote()}/main "
                              f"{moved[:10]} (main moved during its checks): rebase onto {remote()}/main and land again")
                return "rejected"
            tree_hook = precheck_hook(tree) if hook else None
            n, why = recheck_reason(tree, full_base, moved, new_head, range_paths(tree, moved, new_head))
            moved_by = f"main moved by {n} commit{'' if n == 1 else 's'}"
            if why is None and tree_hook and not verify_supported(tree_hook):
                why = f"no file overlap, but the hook has no {VERIFY_MODE} for the combined tree"
            if why is not None or tree_hook:  # a new round, not an attempt: the full checks, or the combined tree's
                verify = why is None
                what = ("no file overlap: its combined tree verified (every test config compiled, the touched "
                        "modules' unit tests)") if verify else f"{why}: checked again"
                set_change(d, e, "queued", data={"rounds": e.get("rounds", 0) + 1},
                           event={"text": f"{e['id']}: {moved_by} since its checks, {what}"},
                           rounds=e.get("rounds", 0) + 1, retry_after=0, verify=verify)
                wlog(d, f"{e['id']}: {moved_by} during its checks, {what}: queued for a new round")
                return "queued"
            note = f"{moved_by}, no file overlap: pushed without re-check"  # no hook: nothing to check it with
            wlog(d, f"{e['id']}: {note}")
            where = f"{where}; {note}"
            e.pop("remote_run", None)
            base, head, e["tip"] = moved, new_head, new_head
        elif e.get("checked_mode") == VERIFY_MODE:  # the combined tree passed: the change's own checks stand
            skip = hook_query(precheck_hook(tree), tree, "--plan", range_paths(tree, base, head))
            if skip is None:
                set_change(d, e, "queued", event={"text": f"{e['id']}: the hook's plan could not be read: checked again"},
                           retry_after=0, verify=False)
                return "queued"
            where = f"{where}; main moved since its full check on {full_base[:10]}: no file overlap, the combined tree verified"
            e.pop("remote_run", None)
        commits = waiting_commits(tree, base, head)
        reds = gate_reds(repo) if waiting_supported(repo) else set()
        kr = None if red_fix(e, reds) else e.get("known_red")  # a red fix failing only as main fails lands: no wait
        if kr or (reds and must_wait(repo, commits, reds)):
            why = (f"its checks failed only where {remote()}/main fails too ({keys_text(kr['keys'])}: "
                   f"{'; '.join(kr['where'])}): not held against it; it lands with the batch once main is green "
                   "or the red's fix lands") if kr else None
            return "joined" if queue_join(repo, d, e, tree, head, why) == 0 else "rejected"
        fixes = sorted(fixes_of(repo, commits))
        set_change(d, e, "pushing", event={"text": f"{e['id']}: pushing {head[:10]}"})
        pushed_at = time.time()
        env = tool_env()
        if e.get("remote_run"):  # the hook takes ci-runner's pass once GitHub confirms it (remote checks)
            env["PREPUSH_REMOTE_RUN"] = str(e["remote_run"])
        if skip:  # fully checked on an older base, the combined tree verified: the hook judges the red gate only
            env["PREPUSH_SKIP"] = " ".join(skip)
        rc, out = run_watched(push_argv(remote(), head), tree, env, log_path, "push", **push_bounds())
        git(repo, "fetch", "--quiet", remote(), "main", check=False)
        if landed(repo, head, f"refs/remotes/{remote()}/main"):  # verify: the tip is on main, whatever rc said
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
                                      f"git can read {remote()}/main", run_probe("git_fetch", {"repo": repo}), [],
                           key=f"push-failing-{e['id']}", command="land", cwd=e.get("worktree"),
                           tail=(out or "").splitlines()[-15:])
        return "checked"


def check_tree_for_push(repo, e, i):
    """Tree i (the one the change was checked in, or the push tree) at the change's tip, made when it is missing."""
    _d, clone, _w = waiting_state(repo)
    tree = f"{clone}-wt-check-{i}"
    if not os.path.isdir(tree) or not git_ok(tree, "cat-file", "-e", f"{e['tip']}^{{commit}}"):
        tree = check_tree(repo, i, e["tip"])
    git(tree, "checkout", "-q", "--force", "--detach", e["tip"], check=False)
    return tree


# THE OLDEST VERIFICATION PUSHES FIRST (2026-11-16, workermem: fully checked by 09:15, then its combined tree verified
# four times, each ~10 min, each on a main a YOUNGER change had pushed onto meanwhile, until a hook change forced a full
# round; 10 h unlanded). Main moving under a verification sends it round again, so a change verifies until no push
# lands during its verification -- with a steady stream of younger changes, never. While a change verifies (a round
# queued or checking with `verify`, not in an infra backoff), every younger checked change holds its push (a red's fix
# excepted) and the verification takes the next free check tree: it lands after ONE more round, and the younger ones
# verify after it.
def verifying(e, t):
    """Whether entry e is in a combined-tree verification round that can run now."""
    return bool(e.get("verify")) and e.get("status") in ("queued", "checking") and not e.get("retry_after", 0) > t


def older_verifying(todo, e, t):
    """The oldest entry queued before e whose combined-tree verification runs now (verifying), or None."""
    return next((o for o in sorted(todo, key=lambda o: o["seq"]) if o["seq"] < e["seq"] and verifying(o, t)), None)


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
    todo = sorted(queued_entries(d), key=lambda e: (0 if red_fix(e, reds) else 1, 0 if verifying(e, t) else 1, e["seq"]))
    newest = {}
    for e in sorted(todo, key=lambda e: e["seq"]):  # entries queued before re-queueing replaced: the newest one runs
        for k, older in list(newest.items()):
            if same_change_source(older, e["worktree"], e.get("branch") or ""):
                supersede(d, older, e["id"], f"{e['worktree']} queued again; only the newest entry runs")
                del newest[k]
        newest[e["id"]] = e
    did = []
    for e in [e for e in todo if e["id"] in newest]:
        it = st.put_item("change", e["id"], ENTRY_STATE.get(e["status"], "queued"), change_item_fields(e))
        state = it["state"]
        if state == "pushing":  # a tick that died mid-push: verified next, never pushed twice blindly
            set_change(d, e, "checked", event={"text": f"{e['id']}: its push was interrupted: verified, then tried again"})
            state = "checked"
        fix = red_fix(e, reds)
        if state == "checked":
            if e.get("retry_after", 0) > t:
                continue
            first = None if fix else batch_goes_first(st, d)  # a red fix goes before the batch
            if first:  # a solo push now moves main under the batch, and its whole check is thrown away
                did += behind_batch(e, first, "push")
                continue
            older = None if fix else older_verifying(todo, e, t)
            if older:
                did.append(f"{e['id']}: its push waits for {older['id']}'s combined-tree verification")
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
        first = None if fix else batch_goes_first(st, d, ("assembling", "pushing"))
        if first:  # the next free check tree is the batch's: to assemble it, or to push from its own tree
            did += behind_batch(e, first, "check")
            continue
        slot = check_tree_slot(repo, d, fix=fix)
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


def queue_join(repo, d, e, tree, head, why=None):
    """Under the landing lock, main red (or its checks failing only as its base fails, why): the checked change joins
    `waiting` (land's join, from the check tree); its WAITING note says why."""
    a = argparse.Namespace(follow=False)
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = join(repo, f"refs/remotes/{remote()}/main", lambda *x: subprocess.run(x, cwd=tree, capture_output=True,
                                                                                  text=True, env=tool_env()),
                  os.path.join(d, f"check-{e['id']}"), a, head=head, worktree=e["worktree"],
                  branch=e.get("branch") or "")
    line = (out.getvalue().strip().splitlines() or [""])[-1]
    if rc == 0:
        line = f"{line}: {why}" if why else line
        set_change(d, e, "waiting", event=change_event(e, line, code=None, final=False), decided=ts(),
                   waiting_id=a.waiting["id"])
        wlog(d, f"{e['id']}: {line}")
        return 0
    queue_verdict(d, e, "rejected", line or "NOT LANDED: could not join `waiting`")
    return 1


def checks_here():
    """Whether this machine may run a change's checks itself: the laptop (its tools), never ci-runner (its check trees
    have none of the hook's tools: perl, shellcheck, sbt)."""
    return host_role() != "server"


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
        if not checks_here():
            # NOT CHECKED HERE: ci-runner's check trees are no checking machine (no perl, shellcheck or sbt there; the
            # rate-limit agent's issue, 2026-11-01) -- a remote check without a verdict is no verdict, re-queued as infra
            wlog(d, f"{e['id']}: the remote check gave no verdict ({out.splitlines()[-1] if out else '?'}): re-queued")
            return None, out, "on ci-runner (no verdict)"
        wlog(d, f"{e['id']}: the remote check gave no verdict ({out.splitlines()[-1] if out else '?'}): checking here")
    # the range through the remote's ref (origin/main is `base` now, fetched just before): the hook finds the remote, and
    # with it the forge's red gate, from it -- given bare shas it fell back to the wrong forge: the gate unread
    rng = f"{remote()}/main..{head}" if git(tree, "rev-parse", f"{remote()}/main").strip() == base else f"{base}..{head}"
    if mode == VERIFY_MODE:  # the combined tree: what landed since the full check and the change, together
        rng = f"{e.get('full_base') or base}..{head}"
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
