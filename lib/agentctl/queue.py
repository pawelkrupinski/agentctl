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
QUEUE_POOL = 4          # check trees, so at most this many queued changes are checked at once ($AGENTCTL_CHECK_TREES);
# a backlog batch (batch.py, THE BACKLOG BATCH) is up to BACKLOG_BATCH_MAX of them checked in ONE tree.
# 4, not 3 (2026-11-17): ci-runner's check runners were 3 in effect (a lane bug let borrowed checks hold the check
# lane: movies runner_scheduler.py), up to 2 lane + 1 CPU-admitted + 2 borrowed since; 4 is a first step, re-measure.
# A check the LAPTOP failed, not the change: sbt's server never came up or was lost, a script test cut off by its
# time bound, a stall. Queued again after QUEUE_INFRA_DELAY, at most QUEUE_INFRA_RETRIES times, then judged as it is.
# THE CHECK'S ENVIRONMENT LACKED THE GITOPS MANIFESTS (2026-11-22, landing checks 44984605717 and 44998261508: the
# deploy specs failed "infra/kubernetes/... is missing because the GitOps manifests are no longer in this repository",
# the beside script tests having moved the fetched manifests aside; Identity takes #349's fix was REJECTED twice and the
# gate stayed red ~7 h). The manifests are another repository's, fetched by the check itself (infra/bin/fetch-gitops):
# their absence, or the fetch failing, is the check's environment, never the change. This is an INFRA RETRY of the
# check, NOT a rerun-to-green of a test: no test result is discarded, the check never ran the specs against their
# input. Re-queued like any infra failure (backed off, escalated from the third, a condition opened past
# QUEUE_INFRA_RETRIES), so a change that really breaks the fetch is still seen.
GITOPS_MISSING = (r"GitOps manifests are no longer in this repository|fetch-gitops could not fetch|"
                  r"GitOps manifests STALE")
INFRA_CHECK_FAILURE = re.compile(r"failed to connect to server|sbt server did not start within \d+ seconds|"
                                 r"sbt server disconnected|^TIMEOUT +\d+s|STALLED:|" + GITOPS_MISSING, re.M)
QUEUE_INFRA_RETRIES = 3
QUEUE_INFRA_DELAY = 300
# A PUSH, FETCH OR TUNNEL BLIP IS NO VERDICT ON THE CHANGE (audit #6, 2026-10-28: entry 67 "the push failed (pre-push
# hook or remote)" was a tunnel error in its tail; a fixer GAVE UP on it): such a failure, a stalled push, a lock that
# never came free and main moving under every round re-queue the entry (retry_after, at most QUEUE_INFRA_RETRIES),
# its `attempts` reset (a checker restart after an infra re-queue is no failure of the change).
TRANSIENT_PUSH = re.compile(r"Connection (reset|refused|timed out|closed)|closed by remote host|Could not resolve host|ssh: connect to host|"
                            r"kex_exchange_identification|[Bb]roken pipe|early EOF|remote end hung up|RPC failed|"
                            r"unable to access|HTTP (500|502|503|504)|timed out|Operation timed out|tunnel|"
                            # the forge's own fault (issue 20261104T104042-83b1c0, GitHub 2026-11-04)
                            r"fatal error in commit_refs|\[remote rejected\][^\n]*\((Internal Server Error|internal error)\)",
                            re.I)


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
STALLED_REASON = "its checks stalled"
STALL_KEEP_PLACE = 2  # stalls in a row re-queued with no backoff (the change keeps its age-order place), then backed off
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
# SELECTION-AWARE (the owner, 2026-11-17: 17 full rounds of ~40 min since 11-16 06:00, nearly all for a hook or registry
# change): when every forcing path that landed only steers WHAT the check selects (SELECTION_PATHS: the hook, its lint
# registry, local-tests.txt), the hook is asked what it selects for the change's paths at the tip the full check passed
# (full_tip) and at the rebased tip: its --plan, --compile-plan, --unit-plan and --lint-plan, and local-tests.txt's
# lines for every area the plan runs (`basic` with sbt). The verify round runs the NEW hook's compile plan (every test
# config), lint plan and unit plan over full_base..tip, a superset of the change's paths, so what those add is run there
# (the added selection, and only it, on top of the verification); a check --plan adds, or an area line local-tests.txt
# adds, it does not run: then the full round. Removals need nothing. An unreadable selection or no full_tip recorded:
# the full round. BUILD-AWARE (the owner, 2026-11-17: 6 full rounds of ~22 min that day for build.sbt / project/
# churn): what landed changes a root *.sbt or project/ -> the same selection test, AND the build as the change's
# modules see it (buildview.py's build_view_change: build.sbt's build-wide statements, the change's and its selection's module
# definitions with everything they name -- settings helpers, configurations, the projects they dependsOn, the
# project/ members -- at full_tip and at the rebased tip) unchanged, and no file of the change naming the changed build
# file (`git grep -F <basename>`: a script or test reading it) -> the verify round, which compiles every test config
# under the new build; anything changed, unreadable, or the change touching the build itself -> the full round.
# SHARE-AWARE (the owner, 2026-11-18, item 6: 1242 min of full rounds in a week for precommit-share.sh or the registry):
# scripts/ci/precommit-share.sh is a SELECTION_PATH too -- it only splits the hook's selection over the CI check's jobs
# -- and every selection read also holds the union of what its shares (SHARES) run for the change: a check one of
# them adds forces the full round, as a check --plan adds does. A file BOTH sides change stays a full round, these too.
# MODULE OVERLAP (the same decision): no shared file but an sbt module both sides touch -> the verify round plus the
# change's AREA tests (verify_areas: a check/verifyareas- branch; module_overlap), where the tree's precommit-share.sh
# runs that mode; the risk of a cross-change break the area tests miss is accepted (Main catches it).
# The other forcing paths (scripts/hooks/* beyond SELECTION_PATHS, hooks-path.sh, ...) stay a full round.
RECHECK_ALWAYS = re.compile(r"^([^/]+\.sbt|project/.*|scripts/hooks/.*|scripts/hooks-path\.sh"
                            r"|scripts/tool-conventions\.sh"
                            r"|scripts/ci/precommit-share\.sh)$")
HOOK_SELF_TEST = re.compile(r"^scripts/hooks/[^/]+(-test\.sh|-test-lib\.sh|_test\.py)$")
VERIFY_MODE = "--verify-only"
SHARE_SCRIPT = "scripts/ci/precommit-share.sh"
SELECTION_PATHS = frozenset({"scripts/hooks/pre-push", "scripts/hooks/lint-registry.tsv", "scripts/hooks/local-tests.txt",
                             SHARE_SCRIPT})
SELECTION_MODES = ("--plan", "--compile-plan", "--unit-plan", "--lint-plan")
# what each share of the CI check runs for the change (precommit-share.sh `checks <share>`, the hook's own PREPUSH_LIST
# under the share's environment), read on a check/<id> ref -- the change's own checks, its range the change since main.
# No `oldrefs` since 2026-11-21: the old-ref compile runs on Main, after landing, and the script refuses the name.
SHARES = ("rest", "identity", "plan")
SHARE_KEY = "shares"
# MODULE OVERLAP (the owner, 2026-11-18): a verify round whose two sides touch one sbt module also runs that change's
# AREA tests -- a check/verifyareas-<full base>-<id> branch, which a precommit-share.sh naming the mode runs with the
# area shares (identity) in --checks-only over <full base>..HEAD; one that does not name it gets the plain verify round
VERIFY_AREAS_MODE = "verifyareas"
VERIFY_RUNS_ADDED = frozenset({"--compile-plan", "--unit-plan", "--lint-plan"})  # the verify round runs what these add


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


def named_reader(tree, tip, paths, readers):
    """(path, reader) of a path (a deleted one, a changed build file) whose file name a reader names at tip; None when
    none does."""
    readers = [p for p in readers if git_ok(tree, "cat-file", "-e", f"{tip}:{p}")]
    if not paths or not readers:
        return None
    for p in sorted(paths):
        r = git(tree, "grep", "-l", "-F", "-e", os.path.basename(p), tip, "--", *readers, check=False)
        hits = sorted(line.split(":", 1)[1] for line in r.stdout.splitlines() if ":" in line)
        if hits:
            return p, hits[0]
    return None


def forces_full_recheck(path):
    """Whether a path landing meanwhile forces a FULL re-check whatever the change touches (THE RULE's first clause)."""
    return bool(RECHECK_ALWAYS.match(path)) and not HOOK_SELF_TEST.match(path)


def hook_selection(tree, paths):
    """What the tree's hook selects for paths (SELECTION-AWARE, above): {mode: words} for each dry-run mode the hook has,
    and local-tests.txt's lines for the areas its plan runs; None when a mode it has cannot be read."""
    hook = precheck_hook(tree)
    try:
        with open(hook or "") as f:
            text = f.read()
    except OSError:
        return None
    out = {}
    for mode in SELECTION_MODES:
        if f"{mode})" not in text:  # never asked: an unknown flag would run the hook's real push checks
            continue
        out[mode] = hook_query(hook, tree, mode, paths)
        if out[mode] is None:
            return None
    shares = share_selection(tree)
    if shares is False:
        return None
    if shares is not None:
        out[SHARE_KEY] = shares
    areas = {"basic"} if "sbt" in out.get("--plan", []) else set()
    areas |= set(out.get("--plan", []))
    try:
        with open(os.path.join(tree, "scripts", "hooks", "local-tests.txt")) as f:
            out["local-tests"] = [line.rstrip("\n") for line in f if line.split()[:1] and line.split()[0] in areas]
    except OSError:
        out["local-tests"] = []
    return out


def share_selection(tree):
    """The union of what precommit-share.sh's shares run for the change at the tree's HEAD (sorted words); None when the
    tree has no such script, False when a share cannot be read. Read as a change's own check (GITHUB_REF_NAME check/<x>,
    --checks-only): its range is HEAD since its merge base with origin/main, which in a check tree is the change."""
    script = os.path.join(tree, SHARE_SCRIPT)
    if not os.access(script, os.X_OK):
        return None
    words = set()
    env = {**tool_env(), "GITHUB_REF_NAME": "check/selection"}
    for share in SHARES:
        try:
            r = subprocess.run([script, "checks", share], cwd=tree, capture_output=True, text=True, timeout=120,
                               env=env, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.TimeoutExpired):
            return False
        if r.returncode != 0:
            return False
        words |= set(r.stdout.split())
    return sorted(words)


def sbt_module_of(path):
    """The sbt module a path is in (`worker` for worker/src/...), or None."""
    parts = path.split("/")
    return parts[0] if len(parts) > 2 and parts[1] == "src" else None


def module_overlap(landed_paths, change_paths):
    """The sbt modules both sides touch (MODULE OVERLAP), sorted."""
    landed = {sbt_module_of(p) for p in landed_paths} - {None}
    return sorted(landed & ({sbt_module_of(p) for p in change_paths} - {None}))


def verify_areas_supported(tree):
    """Whether the tree's precommit-share.sh runs the area tests of a check/verifyareas- branch."""
    try:
        with open(os.path.join(tree, SHARE_SCRIPT)) as f:
            return f"{VERIFY_AREAS_MODE}-" in f.read()
    except OSError:
        return False


def selection_at(tree, rev, tip, paths):
    """hook_selection at rev, read in tree (checked out at tip, and back at tip after); None when rev is unknown."""
    if not rev or not git_ok(tree, "cat-file", "-e", f"{rev}^{{commit}}"):
        return None
    if rev == tip:
        return hook_selection(tree, paths)
    git(tree, "checkout", "-q", "--force", "--detach", rev)
    try:
        return hook_selection(tree, paths)
    finally:
        git(tree, "checkout", "-q", "--force", "--detach", tip)


def selection_change(tree, full_tip, tip, paths, new=None):
    """None when the hook selects nothing at tip for paths that the verify round leaves unrun and the full check at
    full_tip did not run (SELECTION-AWARE, above); else what it adds, in words. new: the selection at tip, when read."""
    old, new = selection_at(tree, full_tip, tip, paths), new or selection_at(tree, tip, tip, paths)
    if old is None or new is None:
        return "its selection at the full check cannot be read" if old is None else "its selection cannot be read"
    for key in sorted(set(old) | set(new)):
        if key in VERIFY_RUNS_ADDED:
            continue
        added = [w for w in (new.get(key) or []) if w not in set(old.get(key) or [])]
        if key not in old and key in new or added:
            return f"its {key.lstrip('-')} for the change adds {', '.join(added[:3]) or 'a mode'}" \
                + (f" (+{len(added) - 3} more)" if len(added) > 3 else "")
    return None


# OUT OF THE BUILD'S REACH (the owner, 2026-11-21: "narrow the 27 'build' rechecks (about 24 min each)"; 26 of them in
# 11-19..21 were BUILD-AWARE's view moving, every view holding e2e, worker and repochecks -- the `basic` lines every sbt
# check runs -- 12 of those 26 changes to scripts, docs, workflows, infra or the mobile apps, 5 of them touching no file
# the build compiles, reads or names). A change none of whose paths the build reaches -- none under an sbt project's directory, the build
# itself (a root *.sbt, project/), the shared resources (test/, data/, conf/), and none the build's own files name --
# (a root Markdown file -- CLAUDE.md -- is no input of the build) runs under the new build only the checks that read its files, which are the lint and script self-tests its paths
# select; the verify round runs exactly those, over the combined tree, under the new build (with every test config
# compiled). Every other check -- the basic lines, the area tests -- reads none of its files, and main's own landing
# check ran them under the new build: the verify round, not the full one. A file the change has that names the build
# file still forces the full round (named_reader, above).
BUILD_SHARED_DIRS = ("test", "data", "conf")


def out_of_build_reach(tree, tip, paths):
    """Whether no path of the change is one the sbt build compiles or reads at tip (OUT OF THE BUILD'S REACH); False when
    the build cannot be read."""
    b = read_build(tree, tip)
    if b is None or not paths:
        return False
    dirs = (set(b["projects"]) - {"."}) | set(BUILD_SHARED_DIRS) | {"project"}
    if any(("/" not in p and not p.endswith(".md")) or p.split("/", 1)[0] in dirs or p.split("/")[1:2] == ["src"]
           or p.endswith((".scala", ".java"))
           for p in paths):
        return False  # a root file but Markdown (build.sbt, the Dockerfile ...) is the build's or read by it; source too
    files = [f for f in git(tree, "ls-tree", "-r", "--name-only", tip, "--", "project", check=False).stdout.split()
             if "/target/" not in f"/{f}"] + [f for f in git(tree, "ls-tree", "--name-only", tip, check=False).stdout.split()
                                             if f.endswith(".sbt")]
    if not files:
        return False
    names = [a for p in sorted({os.path.basename(p) for p in paths}) for a in ("-e", p)]
    r = git(tree, "grep", "-l", "-F", *names, tip, "--", *files, check=False)
    return r.returncode == 1  # 0: the build names one of them (it may read it); else unreadable


def recheck_reason(tree, base, moved, tip, change_paths, full_tip=None):
    """(commits main moved by, why the change must be FULLY checked again or None) -- THE RULE above, of what landed in
    base..moved against the change's paths (moved..tip, the rebased change; tree checked out at tip). full_tip: the tip
    the full check passed, for SELECTION-AWARE."""
    n = len(git(tree, "rev-list", f"{base}..{moved}", check=False).stdout.split())
    landed_paths = range_paths(tree, base, moved)
    build = sorted(p for p in landed_paths if forces_full_recheck(p))
    other = [p for p in build if p not in SELECTION_PATHS and not BUILD_VIEW_PATHS.match(p)]
    if other:
        return n, f"they change {other[0]}"
    if build:
        builds = [p for p in build if BUILD_VIEW_PATHS.match(p)]
        new = selection_at(tree, tip, tip, change_paths) if builds else None
        moves = selection_change(tree, full_tip, tip, change_paths, new=new)
        if not moves and builds:
            # a script, workflow or test reading the build file by name (production code names it in comments only)
            named = named_reader(tree, tip, builds, [p for p in change_paths if "/src/main/" not in p])
            moves = f"{named[1]} names {named[0]}" if named else \
                (None if out_of_build_reach(tree, tip, change_paths) else
                 build_view_change(tree, full_tip, tip, change_paths, new))
        if moves:
            return n, f"they change {(builds or build)[0]}, and {moves}"
    same = without_generated(tree, sorted(set(landed_paths) & set(change_paths)))  # regenerated: the ratchet judges
    if same:
        return n, f"both touch {same[0]}" + (f" (+{len(same) - 1} more)" if len(same) > 1 else "")
    for deleted, readers, whose in ((range_paths(tree, base, moved, deleted=True), change_paths, "they delete"),
                                    (range_paths(tree, moved, tip, deleted=True), landed_paths, "it deletes")):
        hit = named_reader(tree, tip, deleted, readers)
        if hit:
            return n, f"{whose} {hit[0]}, which {hit[1]} names"
    return n, None


# MINIMAL RECHECKS (the owner, 2026-11-20: "batch those as well ... so that rechecks are kept to a minimum"; that
# morning fix-red-2717 sat through a full re-check, then a verify round, after main moved by 12 commits). THE RULE above
# decides between a FULL round and the combined tree's verify round; before this, a change main moved under always paid
# at least the verify round. Now a third answer: its VERDICT IS KEPT -- rebased and pushed, no round -- when no check the
# change's paths select reads a path main changed since its last check, by the hook's own path selection, read at the
# rebased tip: its compile plan (each test config compiles its module and every module upstream of it, so the two
# sides' plans meet where one side's code is compiled against the other's), its whole-tree ratchets (`--coupling-plan`,
# `--quality-plan`: any path either selects reads the whole production tree), its plan's checks (an area such as
# `identity`, `scripttests`, `drift`, ... -- but not the checks that read only the files they are handed,
# PER_FILE_CHECKS, whose shared file THE RULE already sees, nor `sbt`, which the plans above spell out), and its lint
# plan's path-selected specs (the standing `*` rows, which every push runs, read no path in particular). Two sides
# selecting one such check -> the verify round, as before, the check named (`read_overlap`); the rebase changing the
# change's own diff (its patch id: a merged generated row, a three-way pick) -> the verify round too
# (`own_diff_changed`). A hook without `--plan` and `--compile-plan` cannot say what its checks read: the verify round.
# The accepted risk is a standing lint or a basic line reading both sides (Main catches it), the risk the module-overlap
# rule takes. The push then skips the plan the change's checks passed (PREPUSH_SKIP) and the hook judges the red gate.
# Every decision -- a full round, a verify round, a kept verdict -- and why is kept on the change (`rechecks`) and
# printed by `land status <id>`.
READ_MODES = ("--compile-plan", "--coupling-plan", "--quality-plan", "--plan", "--lint-plan",
              "--script-test-plan")  # cheapest first
# PER TEST (2026-11-20): a hook answering --script-test-plan names the script self-tests a path selects; its `scripttests`
# plan word -- shared by any two script changes, 33 of 131 rechecks in 3 days -- is then read per test instead
SCRIPT_TESTS_CHECK, SCRIPT_TEST_MODE = "scripttests", "--script-test-plan"
WHOLE_TREE_MODES = ("--coupling-plan", "--quality-plan")
# PER-FILE LINTS (movies, 2026-11-21, landing-speed proposal 1): a hook answering --per-file-lints names, of the lint
# specs some paths select, those judging each file alone that the paths select by what the spec reads -- never by its
# own code. Both sides naming one is no overlap of its lint plan: the two meet in its verdict only through a file both
# change, which THE RULE rechecks in full, or through its code, which then selects it not "alone".
PER_FILE_MODE = "--per-file-lints"
PER_FILE_CHECKS = frozenset({"shellcheck", "actionlint", "eslint"})


def hook_text_modes(hook, flags=(PER_FILE_MODE,)):
    """The flags the hook (a path) answers, of flags: a flag is never asked unless the hook has it."""
    try:
        with open(hook or "") as f:
            text = f.read()
    except OSError:
        return []
    return [m for m in flags if f"{m})" in text]


def hook_modes(hook):
    """The dry-run modes of READ_MODES the hook (a path) answers."""
    return hook_text_modes(hook, READ_MODES)


def compiled_path(path):
    """Whether sbt compiles path or builds with it: a source file, or the build (a root *.sbt, project/)."""
    return path.endswith((".scala", ".java", ".sbt")) or path.startswith("project/")


SOURCE_CONFIGS = {"test": "Test", "it": "It", "page": "Page"}  # <module>/src/<dir>/ -> the config compiling it


def own_configs(plan, paths):
    """The configs of a compile plan (`<module>/<Config>/compile` words) standing for the paths' OWN code, as compiled:
    `<module>/src/test|it|page/` its config; `src/main/` (and fixtures) its module's Test config, which compiles main
    with it. A compiled path under no module of the plan (the build, project/) is every config of it; a config the plan
    does not name, every config of its module."""
    out = set()
    for p in paths:
        if not compiled_path(p):
            continue
        parts = p.split("/")
        module = {w for w in plan if w.split("/", 1)[0] == parts[0]} if len(parts) > 1 else set()
        if not module:
            return set(plan)
        word = f"{parts[0]}/{SOURCE_CONFIGS.get(parts[2], 'Test') if len(parts) > 3 and parts[1] == 'src' else ''}/compile"
        out |= {word} if word in module else module
    return out


# ONE SIDE COMPILED AGAINST THE OTHER (2026-11-21: in 3 days, kept_verdicts 0 of every landing; 24 of the verify rounds
# run after main moved outside the change's files named the compile plan, most of them `e2e/Test/compile`): a compile
# plan names the configs compiling against the paths -- a module's own and those of every module downstream of it -- so
# ANY two code changes meet in e2e, which depends on everything, though neither side's code is compiled against the
# other's there (worker and web are independent; e2e's own sources are unchanged). The two sides' compiles interact only
# where one side's plan holds the config compiling the other side's OWN code (own_configs): common's plan holds
# web/Test/compile, which compiles a web change; worker's plan holds none of web's; a repochecks spec's plan holds
# worker/It/compile (worker's integration specs compile against repochecks' specs), which a worker/src/main change is
# not compiled in alone (worker/Test/compile stands for it, and the spec's plan does not hold it).
def compile_meets(hook, tree, mine, theirs):
    """The configs where one side's code is compiled against the other's, as words; None when a plan is unreadable."""
    a, b = hook_query(hook, tree, "--compile-plan", mine), hook_query(hook, tree, "--compile-plan", theirs)
    if a is None or b is None:
        return None
    a, b = set(a), set(b)
    return (a & own_configs(b, theirs)) | (b & own_configs(a, mine))


def check_reads(hook, tree, mode, paths, standing, per_file):
    """The checks of one dry-run mode that paths select, as words (MINIMAL RECHECKS); None when unreadable."""
    out = hook_query(hook, tree, mode, paths)
    if out is None:
        return None
    if mode in WHOLE_TREE_MODES:
        return {"the whole production tree"} if out else set()
    if mode == "--plan":
        return set(out) - per_file
    if mode == "--lint-plan":
        return set(out) - standing
    return set(out)


def read_overlap(tree, landed_paths, change_paths):
    """Why the change's checks must run again over what main changed meanwhile (landed_paths): a check both sides'
    paths select, in words -- or None when none is (its verdict is kept). MINIMAL RECHECKS, above; tree is checked out
    at the rebased tip."""
    hook = precheck_hook(tree)
    modes = hook_modes(hook)
    if "--plan" not in modes or "--compile-plan" not in modes:
        return "the hook has no plan saying what its checks read"
    if not landed_paths or not change_paths:
        return None
    standing = set()
    if "--lint-plan" in modes:
        standing = hook_query(hook, tree, "--lint-plan", [])
        if standing is None:
            return "its lint plan cannot be read"
        standing = set(standing)
    # a hook that cannot say what its whole-tree ratchets read: its `sbt` check is one check both sides may share
    per_file = PER_FILE_CHECKS | ({"sbt"} if all(m in modes for m in WHOLE_TREE_MODES) else set()) \
        | ({SCRIPT_TESTS_CHECK} if SCRIPT_TEST_MODE in modes else set())
    per_file_mode = PER_FILE_MODE in hook_text_modes(hook)
    for mode in modes:
        if mode == "--compile-plan":
            both = compile_meets(hook, tree, change_paths, landed_paths)
            if both is None:
                return "its compile-plan cannot be read"
            if both:
                return (f"its compile-plan reads what main changed: {', '.join(sorted(both)[:3])}"
                        + (f" (+{len(both) - 3} more)" if len(both) > 3 else ""))
            continue
        mine = check_reads(hook, tree, mode, change_paths, standing, per_file)
        theirs = check_reads(hook, tree, mode, landed_paths, standing, per_file) if mine else set()
        if mine is None or theirs is None:
            return f"its {mode.lstrip('-')} cannot be read"
        both = mine & theirs
        if mode == "--lint-plan" and both and per_file_mode:
            # PER-FILE LINTS: a spec judging each file alone meets both sides only through a file both change (THE
            # RULE's full round) or its own code; one both sides select by what it reads alone is no overlap
            alone = [hook_query(hook, tree, PER_FILE_MODE, ps) for ps in (change_paths, landed_paths)]
            if None in alone:
                return f"its {PER_FILE_MODE.lstrip('-')} cannot be read"
            both -= set(alone[0]) & set(alone[1])
        both = sorted(both)
        if both:
            return (f"its {mode.lstrip('-')} reads what main changed: {', '.join(both[:3])}"
                    + (f" (+{len(both) - 3} more)" if len(both) > 3 else ""))
    return None


def own_diff_changed(tree, base, tip, moved, new_head):
    """Whether rebasing the change (base..tip) onto moved (moved..new_head) changed its own diff: their patch ids
    differ (MINIMAL RECHECKS: a generated row merged, a three-way pick). True when either cannot be read."""
    def pid(a, b):
        diff = git(tree, "diff", "--no-renames", "--binary", a, b, check=False)
        if diff.returncode != 0:
            return None
        try:
            r = subprocess.run(["git", "patch-id", "--stable"], cwd=tree, input=diff.stdout, capture_output=True,
                               text=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired):
            return None
        return (r.stdout.split() or [""])[0] if r.returncode == 0 else None
    old, new = pid(base, tip), pid(moved, new_head)
    return old is None or new is None or old != new


RECHECK_LOG_MAX = 12  # the rounds and kept verdicts a change keeps (`land status <id>` prints them)


def recheck_logged(e, kind, why, main):
    """e's recheck log with one more decision: a `full` round, a `verify` round or a `kept` verdict, why, on which
    main."""
    return (list(e.get("rechecks") or [])[-(RECHECK_LOG_MAX - 1):]
            + [{"at": int(time.time()), "kind": kind, "main": (main or "")[:10], "why": str(why)[:300]}])


_kept_known = process_cache({})  # (entry id, tip, checked base, main) -> verdict_kept's answer


def verdict_kept(repo, e, main):
    """Whether checked change e would keep its verdict on main (MINIMAL RECHECKS) as the repo reads it now -- no
    shared file, no forcing path, no check reading what landed -- so it pushes alone rather than waiting for a push
    batch's verify round. Its push decides again, at its rebased tip (_push_change)."""
    base, tip = e.get("checked_base"), e.get("tip")
    if not base or not tip or not main or base == main:
        return False
    key = (e["id"], tip, base, main)
    if key not in _kept_known:
        landed, mine = range_paths(repo, base, main), range_paths(repo, base, tip)
        _kept_known[key] = bool(landed) and not any(forces_full_recheck(p) for p in landed) \
            and not without_generated(repo, sorted(set(landed) & set(mine))) \
            and read_overlap(repo, landed, mine) is None
    return _kept_known[key]


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
            "fixes": sorted(fixes_of(repo, commits)), "diagnoses": sorted(diagnoses_of(repo, commits)) or None,
            "keep_worktree": bool(getattr(a, "keep_worktree", False)),
            "no_batch": bool(getattr(a, "no_batch", False)),
            "claude_pid": int(os.environ["CLAUDE_PID"]) if (os.environ.get("CLAUDE_PID") or "").isdigit() else None,
            "session": os.environ.get("CLAUDE_CODE_SESSION_ID") or None,
            "base": git(repo, "rev-parse", main_ref).strip(), "host": os.uname().nodename,
            # THE LANDER (lander.py): who landed it, and each commit's keys to find it on main after the rebase
            "lander": lander_record(repo), "commit_keys": commit_keys(repo, main_ref, head),
            # FIX REPLAY (red.py): agentctl's own replays of the push, for the server's check
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
                return state, data, time.time() - t0
        if time.time() - t0 >= bound:
            return state, data, time.time() - t0
        time.sleep(next(delays))


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


# LAND --UNTIL-DONE (the owner, 2026-11-18): agents kept ending badly -- stopping after the rebase without landing,
# handing back partial reports or the same report twice, leaving worktrees behind, and `git branch -D` by hand after a
# landing rebased their commits (never merged by ancestry). This mode owns the whole tail, bounded by --deadline:
# submit (or, called again after an interruption, find its own entry by worktree and head: never queued twice), the
# fast precheck in the foreground, the wait for the verdict, and then
#   NOT LANDED -> the exact reason and the next command (rebase / fix / land again), exit 1;
#   LANDED     -> every local commit's patch proven on origin/main (git cherry, content twins), the worktree clean and
#                 this agent's own (the change's worktree, not the root checkout, no other session's) -> the worktree
#                 removed and its branch deleted (the force delete only after that patch check, close_worktree):
#                 `DONE <sha> (...): ... worktree removed`, exit 0. Not provable -> `LANDED <sha> ... NOT DONE: <why>`,
#                 the worktree kept, exit 3. The caller's own processes in it (its kept sbt server) are stopped first
#                 (cleanup.py free_worktree); still held by another's -> each holder named and the command, exit 4;
#   the deadline -> `NOT DONE: <id> is <state> ...`, the command to resume, exit 2.
# A VERDICT IS DURABLE (2026-11-19: subagents' `--until-done` waits died with a Claude session restart, and they handed
# back "queued, waiting" with no verdict). The change's item holds it, and its exit event reaches the inbox naming the
# worktree and the change (the projector), whether or not a call waited; `land status <entry>` prints it after the
# fact; and a call again from the worktree at that head finds its entry (own_change_item): open -> RESUMING, landed ->
# the cleanup, REJECTED -> that verdict and the next command (before, a rejection decided while nothing waited was
# silently queued again unchanged).
UNTIL_DONE_DEADLINE = 4 * 3600  # s (--deadline): a batch at green, a verify round and a push fit well inside it


def parse_duration(text):
    """Seconds of `90`, `90s`, `30m`, `3h`."""
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([smh]?)\s*", str(text))
    if not m:
        raise argparse.ArgumentTypeError(f"not a duration: {text!r} (90, 90s, 30m, 3h)")
    return float(m.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600}[m.group(2)]


UNTIL_DONE_AGAIN = "~/bin/agentctl land --until-done"


def own_change_item(repo, head, store=None):
    """The change land submitted from worktree `repo` at `head`, as {id, state, data, entered_at}: the open one, else
    the newest decided one. Read from the store's change items and from this host's queue entries (an entry the tick
    has not made an item of yet: the reconciler's own queue)."""
    rp = os.path.realpath(repo)

    def ours(x):
        return x.get("head") == head and os.path.realpath(x.get("worktree") or "") == rp
    mine = [it for it in (store or Store()).items("change") if ours(it.get("data") or {})]
    known = {it["id"] for it in mine}
    try:
        d = waiting_state(repo)[0]
    except RuntimeError:
        d = None
    for e in waiting_entries(d, open_only=False) if d else []:
        if e["id"] not in known and ours(e) and e.get("status") in ENTRY_STATE:
            mine.append({"id": e["id"], "state": ENTRY_STATE[e["status"]], "data": e, "entered_at": e.get("seq") or 0})
    open_ = [it for it in mine if not machine_state("change", it["state"]).terminal]
    pool = open_ or mine
    return max(pool, key=lambda it: (it.get("entered_at") or 0, it["id"])) if pool else None


def next_command(repo, why):
    """The command an agent runs next after NOT LANDED `why` (one line)."""
    w = why or ""
    if re.search(r"no longer applies|conflict", w, re.I):
        return (f"next: git -C {repo} fetch origin main && git -C {repo} rebase origin/main (resolve, commit), then "
                f"{UNTIL_DONE_AGAIN}")
    if re.search(r"checks failed|precheck refused|PRECHECK FAIL|failing with", w):
        return f"next: fix what failed above in {repo}, commit, then {UNTIL_DONE_AGAIN}"
    if re.search(r"SUPERSEDED|superseded", w):
        return f"next: {UNTIL_DONE_AGAIN} (it resumes the newer entry of this worktree)"
    return f"next: read the reason above, act on it in {repo}, then {UNTIL_DONE_AGAIN}"


def until_done_finish(repo, eid, sha, a, item=None):
    """LANDED: the proof, then the cleanup (LAND --UNTIL-DONE above). Returns the exit code."""
    def kept(why):
        print(f"LANDED {sha} (queued {eid}); NOT DONE: the worktree {repo} is kept: {why}", flush=True)
        return 3
    data = (item or {}).get("data") or {}
    sid = os.environ.get("CLAUDE_CODE_SESSION_ID")
    if data.get("session") and sid and data["session"] != sid:
        return kept(f"not this session's (the change was landed by session {data['session']}); its own agent cleans up")
    try:
        root = os.path.realpath(worktrees(repo)[0]["worktree"])
    except RuntimeError as e:
        return kept(str(e)[:200])
    if os.path.realpath(repo) == root:
        return kept("the root checkout is never removed")
    git(repo, "fetch", "--quiet", remote(), "main", check=False)
    commits, left = cherry_landed(repo, f"refs/remotes/{remote()}/main", "HEAD")
    if commits is None:
        return kept(f"git cherry could not compare it with {remote()}/main")
    if left:
        return kept(f"{len(left)} local commit(s) whose patch is not on {remote()}/main ({', '.join(c[:10] for c in left[:3])})"
                    f": land them with {UNTIL_DONE_AGAIN}")
    if getattr(a, "keep_worktree", False):
        print(f"DONE {sha} (queued {eid}): every local commit is on {remote()}/main; worktree kept (--keep-worktree)",
              flush=True)
        return 0
    held = free_worktree(repo)
    if held:
        print(f"LANDED {sha} (queued {eid}); NOT DONE: the worktree {repo} is kept: still held after "
              f"{int(env_num('AGENTCTL_FREE_WAIT', FREE_WAIT))} s by processes not this session's to stop:", flush=True)
        for pid, cmd in sorted(held.items()):
            print(f"  pid {pid}: {cmd[:200]}", flush=True)
        print(f"next: ask their owner to stop them (or `kill {' '.join(map(str, sorted(held)))}` if they are yours), "
              f"then {UNTIL_DONE_AGAIN} from {repo}: it only cleans up", flush=True)
        return 4
    msg = close_worktree(repo)
    if not msg.startswith("closed worktree"):
        return kept(msg.split(": ", 1)[-1])
    print(f"DONE {sha} (queued {eid}): every local commit's patch is on {remote()}/main; worktree removed ({msg})",
          flush=True)
    return 0


def land_until_done(repo, main_ref, sh, a):
    """`land --until-done` (LAND --UNTIL-DONE above)."""
    t0 = time.time()
    bound = a.deadline if a.deadline is not None else env_num("AGENTCTL_UNTIL_DONE_DEADLINE", UNTIL_DONE_DEADLINE)
    head = git(repo, "rev-parse", "HEAD").strip()
    it = own_change_item(repo, head)
    if it and it["state"] == "landed":
        eid = it["id"]
    elif it and it["state"] == "rejected":  # A VERDICT IS DURABLE (below): decided while no call waited, said now
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
                                 or Store().item("change", eid))
    if change_decided(state):
        why = str(data.get("why") or f"{eid} {state}")
        print(why if why.startswith("NOT LANDED") else f"NOT LANDED: {why}", flush=True)
        print(next_command(repo, why), flush=True)
        return 1
    print(f"NOT DONE: {eid} is {state} after {fmt_age(time.time() - t0)} (the --deadline); it stays queued -- run "
          f"{UNTIL_DONE_AGAIN} again from {repo}: it resumes this entry", flush=True)
    return 2


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
# place a change can be held for main or for other changes exempts a red fix (its KIND, changekind.py): it takes
# THE RED-FIX TREE no ordinary change may take (08:58-09:15Z: Main #2515's fix sat "queued for 15 min, waiting on a
# check slot" behind three ordinary checks), it goes before an open batch, a known red never parks it on `waiting`,
# must_wait lets it go (the hook's rule), and the watchdog's WR escalates one that waits anyway.
RED_FIX_TREE = "redfix"  # <clone>-wt-check-redfix, under its own lease: only a red fix's checker takes it


# A FIX OF ANY OPEN RED GOES FIRST (2026-11-19, 08010c: `Fixes-Red: 6`, Alert backtest #6, red 5254bffda9d2 outside
# the gate): whether a change is a red fix -- and a GATE red's fix -- is its KIND (changekind.py classify_change), read
# against the gate's reds and every red the store has open (queue_facts). Where the queue ORDERS changes (a push before
# an open batch or a combined-tree verification, the red-fix tree, no batch membership) a fix of any open red counts
# (kind red_fix); where the GATE decides (waiting for green, a known red), the gate's reds alone still do (gate_fix).


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
                "waiting": "waiting", "landed": "landed", "rejected": "rejected", "superseded": "superseded",
                "cancelled": "cancelled"}
PUSH_ATTEMPTS_MAX = 3


def change_item_fields(e):
    """A change item's data at its creation, from its queue entry: where it is, its lander, the reds it claims to fix
    (`fixes`: the watchdog's WR reads them; a red fix never waits for green), and when it was submitted (`submitted_at`:
    its queue age -- an entry queued again after a conflict keeps its first one, entry_queued_since)."""
    since = entry_queued_since(e)
    return {"worktree": e.get("worktree"), "head": e.get("head"), **lander_fields(e),
            **({"fixes": sorted(e["fixes"])} if e.get("fixes") else {}),
            **({"diagnoses": sorted(e["diagnoses"])} if e.get("diagnoses") else {}),
            **({"replays": e["replays"]} if e.get("replays") else {}),
            **({"submitted_at": since} if since else {})}


def entry_queued_since(e):
    """When the change was first queued (epoch): `queued_since` (carried over a re-queue that kept its place), else
    its `created`; None when neither reads."""
    if e.get("queued_since"):
        return float(e["queued_since"])
    try:
        return parse_time(e["created"]).timestamp() if e.get("created") else None
    except (ValueError, TypeError):
        return None


def set_change(d, e, to, data=None, event=None, store=None, **fields):
    """THE writer of a change's state: its item (transition + event) and its entry's status, together. A waiting entry
    that queued changes joined passes a final state on to them (joined_verdicts)."""
    st = store or Store()
    joined = joined_changes(d, e, st) if machine_state("change", to).terminal else []
    if joined and event:  # its author hears it under the queued id alone (joined_verdicts)
        event = {**event, "refs": {**(event.get("refs") or {}), "no_inbox": True}}
    cur = st.put_item("change", e["id"], ENTRY_STATE.get(e.get("status"), "queued"), change_item_fields(e))
    if fields.get("why"):  # a decided change's why, on its item: a fixer's land reads it there (await_queued)
        data = {**(data or {}), "why": str(fields["why"])[:600]}
    landed = {k: fields[k] for k in ("landed_sha", "pushed_at", "batch", "batch_id", "superseded_by")
              if fields.get(k) is not None}
    if landed:  # where and when it landed (or what superseded it), on its item: an awaiter on any host reads it there
        # (await_entry)
        data = {**(data or {}), **landed}
    if to != cur["state"] and (cur.get("data") or {}).get("hold"):  # a new state's wait is said anew (note_hold): a
        data = {**(data or {}), "hold": None}  # hold of the state it leaves never shows on the next (no stale reason)
    try:
        if not machine_state("change", cur["state"]).terminal:
            st.transition("change", e["id"], to, cur["version"], data=data, event=event)
    except StaleVersion:  # another writer moved it: re-read once
        cur = st.item("change", e["id"])
        if not machine_state("change", cur["state"]).terminal:
            st.transition("change", e["id"], to, cur["version"], data=data, event=event)
    e.update(status=ENTRY_STATUS[to], **fields)
    save_entry(d, e)
    joined_verdicts(st, e, joined, to, event, data)


def joined_changes(d, w, st):
    """The queued changes that joined waiting entry w and are still open: entries from before ONE CHANGE, ONE ITEM
    (queue_join), when a queued change meeting a red gate became a waiting entry of its own, the queued one `joined`."""
    out = []
    for q in waiting_entries(d, open_only=False):
        if q.get("status") == "joined" and q.get("waiting_id") == w["id"] and q["id"] != w["id"]:
            it = st.item("change", q["id"])
            if it and not machine_state("change", it["state"]).terminal:
                out.append((q, it))
    return out


def joined_verdicts(st, w, joined, to, event, data):
    """Waiting entry w's final state on each queued change that joined it, under the QUEUED id -- the one its `land`
    printed -- its line the projection's (joined_verdict), its author told there alone (review round 7:
    20261116-184811-movies-r2-ci-ac68b8's batch LANDED went to its waiting entry's id; its own item only followed later,
    through the projection, as a second LANDED)."""
    refs = (event or {}).get("refs") or {}
    for q, it in joined:
        line = joined_verdict(q, w if w.get("why") else {**w, "why": f"{w['id']}: {to}"}, to)
        sha = w.get("landed_sha") if to == "landed" else None
        extra = {k: refs[k] for k in refs if k not in ("verdict", "tail", "exit", "final", "command", "entry", "no_inbox")}
        ev = change_event(q, line, refs.get("tail") or (), refs.get("exit", 0 if to == "landed" else 1), **extra)
        try:
            st.transition("change", q["id"], to, it["version"],
                          data={**(data or {}), "why": line[:600], **({"tip": sha, "landed_sha": sha} if sha else {})},
                          event=ev)
        except (StaleVersion, TerminalState):
            pass


def queue_verdict(d, e, status, verdict, tail=(), code=1, **fields):
    """A change's final verdict: its state (set_change), whose event the projector makes its author's inbox entry."""
    # its recheck log on its item too (stats' kept_verdicts reads it there): a batch's kept verdict is logged on the
    # entry alone
    set_change(d, e, {"joined": "waiting"}.get(status, status), event=change_event(e, verdict, tail, code),
               data={"rechecks": e["rechecks"]} if e.get("rechecks") else None, decided=ts(), why=verdict, **fields)
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


def close_item_landed(st, cid, why, via=None):
    """The store's change item cid, unless terminal (or missing), LANDED with an event saying why; via (the main
    commit carrying it) is its landed sha, which await_entry watches. True when moved."""
    it = st.item("change", cid)
    if not it or machine_state("change", it["state"]).terminal:
        return False
    try:
        st.transition("change", cid, "landed", it["version"], data={"why": why, **({"landed_sha": via} if via else {})},
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
        if not landed_look_due(it["id"], t):
            continue
        landed, via = change_landed(repo, (it.get("data") or {}).get("head"))
        if landed and close_item_landed(st, it["id"], "its commits are on main (by content)", via):
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
            close_item_landed(st, e["id"], "its commits are on main (by content)", via)
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


HOLD_SHOWN = ("queued", "checked")  # the states whose item's `hold` says why it waits (a check, a push)


def change_rows(store=None):
    """THE QUEUE as everyone reads it (`land status`, the status line, the page): the store's open change items,
    by id, as {id, state, worktree, head, deadline, level, hold, kind (a red fix's, changekind.py), since (submitted)}."""
    rows = [{"id": i["id"], "state": i["state"], "worktree": (i.get("data") or {}).get("worktree"),
             "head": (i.get("data") or {}).get("head"), "deadline": i["deadline"], "level": i["level"],
             "hold": ((i.get("data") or {}).get("hold") or {}).get("why") if i["state"] in HOLD_SHOWN else None,
             "kind": kind_text((i.get("data") or {}).get("change_kind")) or None,
             "resolved": (i.get("data") or {}).get("autoresolved") if i["state"] == "checking" else None,
             "since": float((i.get("data") or {}).get("submitted_at") or i.get("entered_at") or 0)}
            for i in (store or Store()).items("change") if not machine_state("change", i["state"]).terminal]
    return sorted(rows, key=lambda r: r["id"])


# A BOUNCE NAMES WHAT CAUSED IT (2026-11-19: lane F2b's `no longer applies on origin/main 2cfa0ee95e` named neither
# file nor commit; its agent rebased blind). Only a real conflict reaches the agent (generated files are resolved in
# the check tree, waiting.py GENERATED FILES ARE NO OVERLAP), and its verdict names each conflicting file with the
# commit of main that last changed it since the change's base; the files are kept on the entry (`conflict_files`), so
# the change queued again from the same source takes its old place (server.py queue_change, A RESOLVED CONFLICT
# KEEPS ITS PLACE).
def conflict_paths(conflicts):
    """The file paths of a pick's conflict list (a generated file's entry carries its resolution's why)."""
    return [c.split(" (generated: ", 1)[0] for c in conflicts or []]


def conflict_cause(repo, since, main, conflicts, n=3):
    """`; it conflicts in F (changed on main by <sha> <subject>), ...` for the conflict list, each file's newest
    commit in since..main; '' when none is known."""
    out = []
    for c in (conflicts or [])[:n]:
        path = c.split(" (generated: ", 1)[0]
        line = git(repo, "log", "-1", "--format=%h %s", "--abbrev=10", f"{since}..{main}", "--", path,
                   check=False).stdout.strip() if since and main else ""
        out.append(f"{c} (changed on main by {line[:90]})" if line else c)
    more = len(conflicts or []) - n
    return f"; it conflicts in {', '.join(out)}" + (f" (+{more} more)" if more > 0 else "") if out else ""


def provisional_hint(tree, e):
    """`; its F kept main's rows ...: rebase, run `regen`, land again` for a change whose generated files the check
    tree merged keeping main's side of a row both changed (MAIN'S ROW, PROVISIONALLY, waiting.py); '' otherwise."""
    files = e.get("provisional") or []
    cmds = sorted({" ".join(g["regen"]) for g in (generated_entry(tree, f) for f in files) if g and g.get("regen")})
    return (f"; its {', '.join(files)} kept {remote()}/main's rows where both sides changed them (no regen command "
            f"runs where the queue does): rebase onto {remote()}/main, run {' and '.join(f'`{c}`' for c in cmds) or 'the regen command'}, "
            "commit and land again") if files else ""


# A ROW ONLY A REGEN DECIDES IS NO BOUNCE (2026-11-19, lane F2b): main re-measured the coupling-baseline rows the change
# changed too; ci-runner runs no JVM, so the check tree kept main's rows (MAIN'S ROW, PROVISIONALLY, waiting.py) and the
# checks' ratchet failed over them. That is no defect of the change, only a regeneration this host cannot run: the
# change is HELD in its place (queued, not checked again) and its agent told ONE command -- `agentctl land` in its
# worktree, which rebases onto main and runs the regen where the JVM is (the laptop's conflict_resolve) -- and the
# change queued again from that worktree takes the held entry's place and queue age (server.py queue_change). A hold
# nobody answers within CONFLICT_PLACE_KEPT is NEVER rejected (the owner, 2026-11-20: "2 drop the reject"): it RECOVERS
# (regen_hold_overdue, below).
def regen_hold(d, e, tree, failing, out):
    """Hold change e for its agent's regeneration (above): queued with retry_after at the hold's end, `regen_hold` on
    its entry, its author told the one command (the event's inbox entry). Returns 1 (nothing pushed)."""
    files = e.get("provisional") or []
    cmds = sorted({" ".join(g["regen"]) for g in (generated_entry(tree, f) for f in files) if g and g.get("regen")})
    cmd = f"cd {e['worktree']} && ~/bin/agentctl land"
    t = time.time()
    why = (f"REGEN NEEDED {e['id']}: its {', '.join(files)} kept {remote()}/main's rows where both sides changed them "
           f"(ci-runner runs no {' / '.join(cmds) or 'regen command'}) and its checks failed over them ({failing}); it "
           f"keeps its place in the queue: run `{cmd}` (rebases onto {remote()}/main and runs "
           f"{' and '.join(f'`{c}`' for c in cmds) or 'the regen'} on this machine)")
    set_change(d, e, "queued", event=change_event(e, why, (out or "").splitlines()[-40:], code=None, final=False),
               why=why, retry_after=t + CONFLICT_PLACE_KEPT,
               regen_hold={"since": t, "cmd": cmd, "files": files, "head": e.get("head")})
    wlog(d, f"{e['id']}: held for its regeneration ({', '.join(files)}): `{cmd}`")
    return 1


# AN OVERDUE REGEN HOLD RECOVERS (the owner, 2026-11-20: "1 approve, 2 drop the reject"; lane F2b's 24-h NOT LANDED is
# struck). Past CONFLICT_PLACE_KEPT the change tick runs the regeneration AGAIN: a new round on the change's item
# (`regen_round`), which the laptop's sync answers by starting `agentctl land` in its worktree once more (server.py
# answer_regen_holds, once per change, head and round; it says so in `regen_answered`). When the hold still stands
# REGEN_RECOVERY_WAIT later -- the laptop unreachable, its JVM down, the regen failing -- the owner is told ONCE (an
# escalation on the change: what is held and why, whether the laptop answered). The change stays held in its place.
REGEN_RECOVERY_WAIT = 2 * 3600  # s from the re-run to the owner's one escalation ($AGENTCTL_REGEN_RECOVERY_WAIT)


def regen_hold_overdue(st, d, e, t):
    """The change tick's step for regen-held entry e (above): False while within its bound (the retry decides), else
    True -- held, never checked again nor rejected -- with the recovery's round or escalation done; lines to `did`."""
    h = e.get("regen_hold") or {}
    if not h or t - float(h.get("since") or t) < CONFLICT_PLACE_KEPT:
        return False
    rec = h.get("recovery") or {}
    wait = env_num("AGENTCTL_REGEN_RECOVERY_WAIT", REGEN_RECOVERY_WAIT)
    if rec and (rec.get("escalated") or t - float(rec.get("at") or t) < wait):
        return True
    it = st.item("change", e["id"])
    if not it or machine_state("change", it["state"]).terminal:
        return True
    files, cmd = ", ".join(h.get("files") or []), h.get("cmd")
    if not rec:
        rnd = int((it.get("data") or {}).get("regen_round") or 0) + 1
        h["recovery"] = {"at": t, "round": rnd}
        text = (f"REGEN RECOVERY {e['id']}: held {fmt_age(t - float(h['since']))} for its regeneration of {files}; "
                f"running it again (the laptop's sync starts `{cmd}`), round {rnd}")
        update, ev = {"regen_round": rnd}, {"kind": "regen.recover", "severity": "info", "text": text}
    else:
        answered = int((it.get("data") or {}).get("regen_answered") or 0) >= int(rec.get("round") or 1)
        rec["escalated"] = t
        text = (f"REGEN HELD {e['id']}: held {fmt_age(t - float(h['since']))}: its {files} need a regen only the "
                f"laptop runs (both sides changed a row); run again {fmt_age(t - float(rec['at']))} ago, "
                + ("the laptop started it, yet it still holds" if answered else "the laptop never answered")
                + f" -- the laptop or its JVM is unreachable, or the regen fails: run `{cmd}` there, or "
                f"`agentctl land cancel {e['id']}`")
        update, ev = {}, {"kind": "regen.escalate", "severity": "escalate", "text": text}
    e["regen_hold"], e["retry_after"] = h, t + wait
    save_entry(d, e)
    try:
        if update:
            st.update_item("change", e["id"], it["version"], update)
            it = st.item("change", e["id"])
        st.transition("change", e["id"], it["state"], it["version"], event=ev)
    except StaleVersion:
        pass  # recorded on the entry; the item catches up next tick
    wlog(d, text)
    return True


def rebase_in_tree(d, tree, e, base, regen=True, conflicts=None):
    """The entry's commits on base, in the tree: (head, None); (None, None) on a conflict (aborted; its files appended
    to `conflicts`); (None, via) when every commit is on base already (dropped: LANDED via that commit, never an empty
    push). regen False: a conflict in a generated file is left a conflict (waiting.py conflict_resolve)."""
    git(tree, "checkout", "-q", "--force", "--detach", base)
    how, x = apply_entry(d, tree, e, base, regen=regen)
    if how == "conflict":
        if conflicts is not None:
            conflicts.extend(x or [])
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
        if not e or e.get("status") not in ("queued", "checking") or e.get("backlog_batch"):  # a backlog batch's
            return 0
        fix = bool(e.get("fixes")) and classify_change(e, queue_facts(gate_reds(repo)))["red_fix"]
        slot = check_tree_slot(repo, d, fix=fix)
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
        conflicts = []
        head, via = rebase_in_tree(d, tree, e, base, conflicts=conflicts)
    if via:
        already_landed(d, e, via if via != "an equivalent commit" else None)
        return 0
    if not head:
        # policy: no-longer-applies
        queue_verdict(d, e, "rejected", f"NOT LANDED: the change no longer applies on {remote()}/main {base[:10]} "
                      f"(main moved since QUEUED{conflict_cause(tree, e.get('base'), base, conflicts)}): rebase onto "
                      f"{remote()}/main and land again (it keeps its place in the queue)",
                      conflict_files=conflict_paths(conflicts))
        return 1
    e["tip"] = head
    save_entry(d, e)
    reds = gate_reds(repo) if waiting_supported(repo) else set()
    commits = waiting_commits(tree, base, head)
    dead = blame_land_refusal(tree, commits, e.get("worktree"))
    if dead:  # queued before its blame was withdrawn or refuted (2026-11-01: revert-629 re-queued by orphan duty)
        # policy: blame-dead
        queue_verdict(d, e, "rejected", f"NOT LANDED: {dead}")
        return 1
    # FIX REPLAY (red.py): the laptop's land may hold no token; its replays ride with the submission (`replays`)
    unreplayed = fix_replay_refusal(repo, commits, proofs=e.get("replays"))
    if unreplayed and unreplayed[0] == "fix-replay" and policy_blocks("fix-replay", None, e["id"], unreplayed[1][:160]):
        queue_verdict(d, e, "rejected", f"NOT LANDED: {unreplayed[1]}")  # policy: fix-replay
        return 1
    if unreplayed and unreplayed[0] == "fix-replay-outside" \
            and policy_blocks("fix-replay-outside", None, e["id"], unreplayed[1][:160]):
        queue_verdict(d, e, "rejected", f"NOT LANDED: {unreplayed[1]}")  # policy: fix-replay-outside
        return 1
    joining = bool(reds) and must_wait(repo, commits, reds)
    cls = gov_class_of(repo, commits, reds)
    mode = "--checks-only" if joining else "--range"
    if adopt and rem.get("mode") == VERIFY_MODE:
        mode = VERIFY_MODE
    elif e.get("verify") and hook:  # THE RULE (recheck_reason) again, on the main this round checks on
        _n, why = recheck_reason(tree, e.get("full_base") or e.get("checked_base") or base, base, head,
                                 range_paths(tree, base, head), full_tip=e.get("full_tip"))
        if why is None and verify_supported(precheck_hook(tree)):
            mode = VERIFY_MODE
            full_base_ = e.get("full_base") or e.get("checked_base") or base
            areas = module_overlap(range_paths(tree, full_base_, base), range_paths(tree, base, head)) \
                if verify_areas_supported(tree) else []
            e["verify_areas"] = areas or None
            if areas:
                wlog(d, f"{e['id']}: sbt modules both sides touch ({', '.join(areas)}): verified with the area tests")
        else:
            wlog(d, f"{e['id']}: {why or f'the hook has no {VERIFY_MODE}'}: fully checked, not only verified")
    where = "no hook"
    if mode != VERIFY_MODE:
        e.pop("verify_areas", None)
    if hook:
        ok, out, where = run_entry_checks(repo, d, e, tree, base, head, mode, cls, log_path, own)
        if ok is None:  # stalled: the laptop's, not the change's -- and what stalled it is said (2026-11-17: two
            # 13-s "stalls" whose cause no record kept)
            last = ((out or "").strip().splitlines() or [""])[-1].strip()[:240]
            # ITS PLACE KEPT (2026-11-17, ref-srp-54ebae 2 h old: a 13-s stall's 5-min backoff let younger changes take
            # every check tree, and it waited 15+ min more for one): the first STALL_KEEP_PLACE stalls in a row are due
            # at once -- the tree it just freed goes to it, first by age; then the infra backoff as for any infra cause
            same = int(e.get("infra_same") or 0) if e.get("infra_reason") == STALLED_REASON else 0
            requeue_infra(d, e, f"its checks stalled ({where}{': ' + last if last else ''})",
                          delay=0 if same < STALL_KEEP_PLACE else None, reason=STALLED_REASON, detail=out, repo=repo)
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
            if side == "head" and e.get("provisional") and not CHECK_TIMEOUT.search(out or ""):
                return regen_hold(d, e, tree, keys_text(what), out)  # A ROW ONLY A REGEN DECIDES IS NO BOUNCE
            if side == "head":
                timed = " (and they ran past their time cap, " \
                        f"{TIMEOUT_RETRIES + 1} times)" if CHECK_TIMEOUT.search(out or "") else ""
                # policy: checks-failed
                queue_verdict(d, e, "rejected", f"NOT LANDED: the pre-push checks failed ({where}){timed}; failing "
                              f"with this change only: {keys_text(what)}{provisional_hint(tree, e)}; the change "
                              f"{e['head'][:10]} stays in {e['worktree']}", (out or "").splitlines())
                return 1
            srcs = sorted(set(what.values()))
            known_red = {"keys": sorted(what), "where": srcs, "base": base}
            wlog(d, f"{e['id']}: its checks failed only where its base fails too ({keys_text(what)}; {'; '.join(srcs)})"
                    ": it waits behind that red")
    full_base = (e.get("full_base") or e.get("checked_base") or base) if mode == VERIFY_MODE else base
    full_tip = e.get("full_tip") if mode == VERIFY_MODE else head
    areas = e.pop("verify_areas", None) if mode == VERIFY_MODE else None
    verified = (f"verified {head[:10]} on {base[:10]} (fully checked on {full_base[:10]}"
                + (f"; with the area tests: sbt modules both sides touch, {', '.join(areas)}" if areas else "") + ")") \
        if mode == VERIFY_MODE else f"checked {head[:10]} on {base[:10]}"
    set_change(d, e, "checked", data={"tip": head, "base": base, "mode": mode, "where": where},
               event={"text": f"{e['id']}: {verified} ({where})"
                      + (" -- failing only as its base fails" if known_red else "")},
               tip=head, checked_base=base, full_base=full_base, full_tip=full_tip, checked_mode=mode, checked_where=where, tree=i,
               infra_reason=None, infra_same=0, known_red=known_red, verify=False, push_alone=None,
               verified_areas=areas)
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
        if gated_repo(o_r):
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
            conflicts = []  # no regen command under the landing lock: a generated file's conflict is a new round's
            new_head, via = rebase_in_tree(d, tree, e, moved, regen=False, conflicts=conflicts)
            if via:
                already_landed(d, e, via if via != "an equivalent commit" else None)
                return "landed"
            if not new_head and conflicts and not [f for f in without_generated(tree, conflicts) if not union_safe(f)]:
                what = (f"main moved by what conflicts with it only in generated files ({', '.join(conflicts[:3])}): "
                        "a new round, which regenerates them over the combined tree")
                set_change(d, e, "queued", data={"rounds": e.get("rounds", 0) + 1},
                           event={"text": f"{e['id']}: {what}"}, rounds=e.get("rounds", 0) + 1, retry_after=0,
                           verify=True)
                wlog(d, f"{e['id']}: {what}: queued for a new round")
                return "queued"
            if not new_head:
                # policy: no-longer-applies
                queue_verdict(d, e, "rejected", f"NOT LANDED: the change no longer applies on {remote()}/main "
                              f"{moved[:10]} (main moved during its checks{conflict_cause(tree, base, moved, conflicts)}"
                              f"): rebase onto {remote()}/main and land again (it keeps its place in the queue)",
                              conflict_files=conflict_paths(conflicts))
                return "rejected"
            tree_hook = precheck_hook(tree) if hook else None
            n, why = recheck_reason(tree, full_base, moved, new_head, range_paths(tree, moved, new_head),
                                    full_tip=e.get("full_tip"))
            moved_by = f"main moved by {n} commit{'' if n == 1 else 's'}"
            if why is None and tree_hook and not verify_supported(tree_hook):
                why = f"no file overlap, but the hook has no {VERIFY_MODE} for the combined tree"
            kept = verify_why = None
            if why is None and tree_hook:  # MINIMAL RECHECKS: a round only when its checks read what main changed
                verify_why = read_overlap(tree, range_paths(tree, base, moved), range_paths(tree, moved, new_head)) \
                    or ("the rebase changed its own diff" if own_diff_changed(tree, base, head, moved, new_head)
                        else None)
                kept = None if verify_why else hook_query(tree_hook, tree, "--plan", range_paths(tree, moved, new_head))
                if not verify_why and kept is None:
                    verify_why = "the hook's plan could not be read"
            if kept is not None:
                note = f"{moved_by} since its checks on {base[:10]}, no check of it reading what changed: its verdict kept"
                wlog(d, f"{e['id']}: {note}")
                e["rechecks"] = recheck_logged(e, "kept", note, moved)
                where, skip = f"{where}; {note}", kept
                e.pop("remote_run", None)
                base, head, e["tip"] = moved, new_head, new_head
            elif why is not None or tree_hook:  # a new round, not an attempt: the full checks, or the combined tree's
                verify = why is None
                areas = module_overlap(range_paths(tree, full_base, moved), range_paths(tree, moved, new_head)) \
                    if verify and verify_areas_supported(tree) else []
                what = ("no file overlap: its combined tree verified (every test config compiled, the touched "
                        "modules' unit tests" + (f", the area tests: sbt modules both sides touch, {', '.join(areas)}"
                                                 if areas else "") + ")"
                        + (f", as {verify_why}" if verify_why else "")) if verify else f"{why}: checked again"
                log = recheck_logged(e, "verify" if verify else "full", f"{moved_by}: {verify_why or why}", moved)
                set_change(d, e, "queued", data={"rounds": e.get("rounds", 0) + 1, "rechecks": log},
                           event={"text": f"{e['id']}: {moved_by} since its checks, {what}"},
                           rounds=e.get("rounds", 0) + 1, retry_after=0, verify=verify, rechecks=log)
                wlog(d, f"{e['id']}: {moved_by} during its checks, {what}: queued for a new round")
                return "queued"
            else:
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
            areas = e.get("verified_areas")
            where = (f"{where}; main moved since its full check on {full_base[:10]}: no file overlap, the combined tree "
                     "verified" + (f" with the area tests (sbt modules both sides touch: {', '.join(areas)})" if areas else ""))
            e.pop("remote_run", None)
        commits = waiting_commits(tree, base, head)
        reds = gate_reds(repo) if waiting_supported(repo) else set()
        kr = None if classify_change(e, gate_facts(reds))["gate_fix"] else e.get("known_red")  # a gate red's fix
        # failing only as main fails lands: no wait
        if kr or (reds and must_wait(repo, commits, reds)):
            why = (f"its checks failed only where {remote()}/main fails too ({keys_text(kr['keys'])}: "
                   f"{'; '.join(kr['where'])}): not held against it; it lands with the batch once main is green "
                   "or the red's fix lands") if kr else None
            queue_join(repo, d, e, tree, head, why)
            return "joined"
        fixes = sorted(fixes_of(repo, commits))
        set_change(d, e, "pushing", data={"rechecks": e["rechecks"]} if e.get("rechecks") else None,
                   event={"text": f"{e['id']}: pushing {head[:10]}"})
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


# A RED FIX'S VERIFICATION IS NEVER THROWN AWAY BY AN ORDINARY PUSH (2026-11-19: 08010c, `Fixes-Red: 6`, verified its
# combined tree 09:34-10:18Z; at 10:11Z ratchet-diff -- queued BEFORE it, so not held by the age rule -- pushed, main
# moved, and the fix verified again until 10:36Z, holding 100213 and 102400 behind it the whole time). A red fix goes
# first (changekind.py PRIORITY): while its verification runs, every ordinary checked change holds its push, older or
# younger. Tests: tests/test_backlog_batch.py PushBatchTest ..._never_pushes_under_a_red_fixs_verification.
def older_verifying(todo, e, t, kinds=None):
    """The entry whose combined-tree verification runs now (verifying) and that e's push waits for: one queued before
    e, or a red fix (its kind in kinds, classify_queue) queued at any time; the oldest such, or None."""
    red = lambda o: bool(((kinds or {}).get(o["id"]) or {}).get("red_fix"))
    return next((o for o in sorted(todo, key=lambda o: o["seq"])
                 if o["id"] != e["id"] and (o["seq"] < e["seq"] or red(o)) and verifying(o, t)), None)


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
    facts = queue_facts(reds, st)  # A FIX OF ANY OPEN RED GOES FIRST (above): each change's KIND, recorded on its item
    entries = queued_entries(d)
    kinds = classify_queue(repo, d, entries, facts, t, st)
    todo = sorted(entries, key=lambda e: kind_order(kinds, e))  # THE PRIORITY ORDER (changekind.py PRIORITY)
    newest = {}
    for e in sorted(todo, key=lambda e: e["seq"]):  # entries queued before re-queueing replaced: the newest one runs
        for k, older in list(newest.items()):
            if same_change_source(older, e["worktree"], e.get("branch") or ""):
                supersede(d, older, e["id"], f"{e['worktree']} queued again; only the newest entry runs")
                del newest[k]
        newest[e["id"]] = e
    did = []
    # PUSHES FIRST, THEN THE HOLDS (queue simulator, 2026-11-18): the holds below were read before this tick's pushes,
    # and a checked change's push due holds every backlog batch back (backlog_candidates) -- so the tree a finished
    # check had just freed went to the oldest lone check, never to the batch: under load (every tree busy, a tree
    # freed only by a check ending) no backlog batch ever formed (seed 0 of the simulator's load profile: 40 changes,
    # 40 lone checks, 0 batches). The checked changes are pushed in a first pass; the holds are read again for the
    # second, which starts checks.
    for phase in ("push", "check"):
        held = backlog_held(repo, d, reds, st, t, facts)
        pheld = push_held(repo, d, reds, st, t, facts)
        # THE BATCH KEEPS ITS AGE-ORDER PLACE (review 2026-11-18): this tick runs before the batch tick, and the
        # changes held for a backlog or push batch take no tree, so a tree freed went to the next YOUNGER lone change
        # and the batch tick found none: with a steady stream of lone changes (identity changes, --no-batch) the batch
        # never formed. A lone change younger than the oldest held one never takes the LAST free tree: that is the
        # batch's (a red fix excepted).
        batch_seq = min((x["seq"] for x in todo if x["id"] in held | pheld), default=None)
        did += _tick_changes_pass(repo, d, st, t, facts, kinds, phase, [e for e in todo if e["id"] in newest], todo,
                                  held, pheld, batch_seq)
    return did


def _tick_changes_pass(repo, d, st, t, facts, kinds, phase, mine, todo, held, pheld, batch_seq):
    """One pass of tick_changes over the newest entries: `push` (the checked and interrupted pushes) or `check` (the
    rest: executors started, deadlines). facts: queue_facts; kinds: each entry's kind (classify_queue)."""
    did = []
    for e in mine:
        if e.get("backlog_batch"):  # a backlog batch's member: the batch checks and pushes it (backlog_reconcile)
            continue
        if e["status"] not in (("checked", "pushing") if phase == "push" else ("queued", "checking")):
            continue  # (a push this tick made it landed, waiting or rejected: no check for it)
        it = st.put_item("change", e["id"], ENTRY_STATE.get(e["status"], "queued"), change_item_fields(e))
        state = it["state"]
        if state == "pushing":  # a tick that died mid-push: verified next, never pushed twice blindly
            set_change(d, e, "checked", event={"text": f"{e['id']}: its push was interrupted: verified, then tried again"})
            state = "checked"
        kind = kinds.get(e["id"]) or classify_change(e, facts, t)
        record_change_kind(st, e, kind)  # on its item, now that it surely has one (a no-op when unchanged)
        fix = kind["red_fix"]
        if state == "checked":
            if e.get("retry_after", 0) > t:
                note_hold(st, e, "retry", f"its push is retried at {hhmmz(e['retry_after'])}", t)
                continue
            did += _push_checked(repo, d, st, t, e, fix, todo, pheld, kinds)
            continue
        alive = checker_alive(e["id"])
        if state == "checking" and not alive and t > it["deadline"]:
            requeue_infra(d, e, "its check passed its deadline with no live checker")
            did.append(f"{e['id']}: re-queued (checking past its deadline)")
            continue
        if state == "queued":  # past its deadline: said, re-deadlined -- an escalation only when its hold is stuck
            lap_change_deadline(st, e, "queued", t)  # (A HELD WAIT IS NO ESCALATION, below)
        if state == "queued" and not alive:
            gone = main_conflict(repo, e)
            if gone:  # it no longer applies on main itself: said now, never after a wait for a tree or an older change
                # policy: no-longer-applies
                queue_verdict(d, e, "rejected", f"NOT LANDED: the change no longer applies on {remote()}/main "
                              f"{gone[0][:10]} (main moved since QUEUED{conflict_cause(repo, e.get('base'), gone[0], gone[1])}"
                              f"): rebase onto {remote()}/main and land again (it keeps its place in the queue)",
                              conflict_files=conflict_paths(gone[1]))
                did.append(f"{e['id']}: rejected (no longer applies on main)")
                continue
            hold = overlap_hold(repo, d, e, todo, facts, t)
            if hold and fix:  # a red fix never waits: its author is told of a conflict, and it goes on
                did += hold_for_overlap(st, d, e, *hold, waits=False, repo=repo)
                hold = None
            if hold:  # OVERLAPPING CHANGES ARE CHECKED AS ONE (below)
                did += hold_for_overlap(st, d, e, *hold, repo=repo)
                o, shared = hold[0], hold[1]
                note_hold(st, e, "overlap", f"its check waits for {o['id']} ({o.get('status')}): both change "
                          f"{', '.join(shared[:3])}" + (f" (+{len(shared) - 3} more)" if len(shared) > 3 else ""), t,
                          blocker=o["id"], files=shared[:10])
                continue
        if e.get("regen_hold") and regen_hold_overdue(st, d, e, t):
            continue
        if alive or e.get("retry_after", 0) > t:
            if not alive:  # said on its item: `land status` shows every wait (changekind.py, no held change silent)
                note_hold(st, e, *retry_hold(e), t)
            continue
        first = None if fix else batch_goes_first(st, d, ("assembling", "pushing"))
        if first:  # the next free check tree is the batch's: to assemble it, or to push from its own tree
            did += behind_batch(e, first, "check")
            note_hold(st, e, "batch", f"its check waits for batch {first['id']} ({first['state']})", t,
                      batch=first["id"])
            continue
        if not fix and e["id"] in pheld:  # THE PUSH BATCH verifies it with others
            if not _behind_said.get((e["id"], "push batch")):
                _behind_said[(e["id"], "push batch")] = True
                did.append(f"{e['id']}: its verify round waits for the next push batch")
            note_hold(st, e, "push-batch", "its verify round waits for the next push batch", t)
            continue
        if not fix and e["id"] in held:  # THE BACKLOG BATCH checks it with others
            if not _behind_said.get((e["id"], "backlog")):
                _behind_said[(e["id"], "backlog")] = True
                did.append(f"{e['id']}: its check waits for the next backlog batch")
            note_hold(st, e, "backlog-batch", "its check waits for the next backlog batch", t)
            continue
        slot = check_tree_slot(repo, d, fix=fix)
        if not slot:
            note_hold(st, e, "trees", "every check tree is busy (a check slot)", t)
            continue
        if not fix and batch_seq is not None and batch_seq < e["seq"] and not check_tree_slot_free(repo, d) \
                and policy_blocks("older-batch", st, e["id"], "the last free check tree kept for the older batch"):
            slot[1].release()  # AGE ORDER: the last free tree is the older changes' batch's
            if not _behind_said.get((e["id"], "older batch")):
                _behind_said[(e["id"], "older batch")] = True
                did.append(f"{e['id']}: its check waits: the last free tree goes to the older changes' batch")
            note_hold(st, e, "older-batch", "the last free check tree goes to the older changes' batch", t)
            continue
        slot[1].release()  # the executor takes it again itself
        note_hold(st, e, "starting", "its executor is starting its check", t)
        spawn_executor(repo, d, e["id"])
        did.append(f"{e['id']}: executor started")
    return did


def retry_hold(e):
    """(kind, why) of a queued change waiting for its retry_after: an infra re-queue's retry, or -- a REGEN NEEDED
    hold (regen_hold) -- its agent's regeneration, which no time brings (2026-11-20, phase8-units: `held: its check is
    retried at 12:03Z (an infra failure)`, read as a retry long overdue: it was the regen hold's end, the next day)."""
    hold = e.get("regen_hold") or {}
    if hold:
        wt = e.get("worktree") or ""
        gone = "" if not wt or os.path.isdir(wt) or host_role() == "server" else f"; its worktree {wt} is gone"
        return "regen", (f"its check waits for its agent's regeneration of {', '.join(hold.get('files') or [])}: "
                         f"`{hold.get('cmd')}` (run again at {time.strftime('%Y-%m-%d %H:%MZ', time.gmtime(float(e['retry_after'])))} "
                         f"if unanswered; never rejected for it){gone}")
    return "retry", f"its check is retried at {hhmmz(e['retry_after'])} (an infra failure)"


def _push_checked(repo, d, st, t, e, fix, todo, pheld, kinds=None):
    """The change tick's step for checked entry e: pushed, unless its push is held -- before an open batch it is not
    in (THE BATCH GOES FIRST), for the next push batch, or behind an older change's combined-tree verification; a red
    fix (fix) is held by none of them. The hold is recorded on its item (`land status` prints it) and its deadline laps
    quietly while it is live; a checked change past its deadline that nothing holds escalates (lap_change_deadline)."""
    did = []
    first = None if fix else batch_goes_first(st, d)  # a red fix goes before the batch
    older = None if fix or first or e["id"] in pheld else older_verifying(todo, e, t, kinds)
    if first:  # a solo push now moves main under the batch, and its whole check is thrown away
        did += behind_batch(e, first, "push")
        note_hold(st, e, "batch", f"its push waits for batch {first['id']} ({first['state']}), which it is not in", t,
                  batch=first["id"])
    elif e["id"] in pheld:  # THE PUSH BATCH verifies and pushes it with others (never a red fix: push_candidates)
        if not _behind_said.get((e["id"], "push batch")):
            _behind_said[(e["id"], "push batch")] = True
            did.append(f"{e['id']}: its push waits for the next push batch")
        note_hold(st, e, "push-batch", "its push waits for the next push batch", t)
    elif older:
        did.append(f"{e['id']}: its push waits for {older['id']}'s combined-tree verification")
        note_hold(st, e, "verify", f"its push waits for {older['id']}'s combined-tree verification", t,
                  blocker=older["id"])
    if first or older or e["id"] in pheld:
        lap_change_deadline(st, e, "checked", t)
        return did
    # THE PUSH IS ITS HOLD WHILE IT RUNS (issue 20261119T123823-f7d6c9): clearing the hold before push_change -- which
    # may fetch, find main moved and re-queue the change for a verification -- left a checked change past its deadline
    # with nothing recorded for the seconds it ran, and the watchdog's pass filed it as unheld
    note_hold(st, e, "push", "its push is under way (re-checked first if main moved)", t)
    out = push_change(repo, d, e)
    if out in ("checked", "busy"):  # raced, met the red gate, or the landing lock / push tree was busy: tried again
        note_hold(st, e, "push", "its push waits for the landing lock or a push tree" if out == "busy" else
                  "its last push did not land (a race or the red gate): tried again at the next tick", t)
    else:  # another answer, still checked: nothing holds it now (a new state's set_change cleared it already)
        note_hold(st, e, None, None, t)
    lap_change_deadline(st, e, "checked", t, f"its last push: {out}")
    return did + [f"{e['id']}: {out}"]


def lap_change_deadline(st, e, state, t, note=None):
    """A queued or checked change past its deadline: said and re-deadlined -- quietly while its hold is live, an
    escalation when it is stuck (change_hold_stuck: nothing holds it, its blocker is gone, held too long). note: what
    else the line says (a checked change's last push)."""
    it = st.item("change", e["id"])
    if not it or it["state"] != state or t <= it["deadline"]:
        return
    hold = change_hold_visible(st, it, e, t)
    stuck = change_hold_stuck(st, hold, it, t)
    if stuck and stuck_recovery(hold, stuck) == "rederive":
        # SELF-HEALING (2026-11-20, THE HOLD TABLE): a hold that describes a world that moved -- its blocker ended, or
        # its kind's bound passed -- is dropped, and the tick decides afresh (it records the hold again if one still
        # applies, its clock restarted); said quietly on the change's events, never a page
        if state == "queued":  # its holds are derived after this lap, in the same tick: from nothing
            note_hold(st, e, None, None, t)
        else:  # a checked change's were derived just before it, this tick: their clock restarts
            restart_hold(st, e, t)
        try:
            it = st.item("change", e["id"]) or it
            st.transition("change", e["id"], state, it["version"],
                          event={"kind": "change.rederived", "severity": "info",
                                 "text": f"{e['id']}: {state} for {fmt_age(t - it['entered_at'])}; its hold "
                                         f"({hold.get('why')}) was stuck -- {stuck} -- so it is decided afresh"})
        except StaleVersion:
            pass
        return
    issue = None if hold else file_unheld_change(st, it, t, "agentctl (the change tick)")  # agentctl's bug: an issue,
    # A HELD LAP IS NO INBOX ENTRY (2026-11-19 08:39Z, 08:55Z: movies-fix-replay's "queued for 15 min / 30 min, held:
    # ..." reached the inbox every 15 min -- 93 of the day's 442 entries -- because the lap's event named the worktree
    # as its audience, and the projector writes every event with one). A live hold's lap is an event with no audience:
    # `land status` and the change's events say it; only a stuck or unheld lap goes to its author.
    quiet = bool(hold) and not stuck
    try:  # never an owner page (changekind.py, A CHANGE PAST ITS DEADLINE WITH NO RECORDED HOLD)
        it = st.item("change", e["id"]) or it
        st.transition("change", e["id"], state, it["version"],
                      event={"text": f"{e['id']}: {state} for {fmt_age(t - it['entered_at'])}, "
                                     + (f"held: {hold['why']}" if hold else "nothing holds it")
                                     + (f" -- STUCK: {stuck}" if stuck else "") + (f" ({note})" if note else "")
                                     + (f" -- filed as agentctl issue {issue}" if issue else ""),
                             **({} if quiet else {"audience": {"worktree": e.get("worktree")}}),
                             **({} if stuck and not issue else {"severity": "info"})})
    except StaleVersion:
        pass  # the next tick says it


# OVERLAPPING CHANGES ARE CHECKED AS ONE (the coordinator, 2026-11-18: B1 against the CI-workflows change, the scraper
# catalog against the scrapers refactor, the cycle-breaking change against both -- each a check, then `NOT LANDED: no
# longer applies on origin/main ... main moved during its checks`, a rebase and a full recheck). A queued change that
# shares a FILE with a change queued before it and still in flight (queued, checking, checked, pushing, or waiting for
# a green gate -- the last only when they conflict, below):
# - both queued, both batchable, and they assemble together (git merge-tree of their heads is clean): nothing held --
#   THE BACKLOG BATCH checks them as one tree;
# - else its check waits for the older one: once that lands, it is checked ONCE, on top of it (no check thrown away, no
#   full recheck); once that is rejected or cancelled, it goes on as before;
# - and when the two cannot be assembled together (a textual conflict), its author is told AT ONCE, at its first tick
#   in the queue: `CONFLICTS <id>: ...` to the inbox, the files named -- rebase onto the older change's branch, or wait
#   for it to land and rebase onto main. Its check waits meanwhile (one is never spent on a tree that cannot land).
# And a queued change that no longer applies on main itself (main moved since its base) is NOT LANDED at its next
# tick, never after a wait for a check tree (main_conflict).
# A red fix never waits (told of a conflict all the same).
# A WAITING CHANGE HOLDS NOTHING IT ASSEMBLES WITH (2026-11-20, Main #2716 red): metrics-packages and phase7-houses sat `held: its check
# waits for movies-misc-cx (waiting)`. A waiting change lands only with the batch at green, so a change held behind it
# was held until green -- a cycle for a red fix, pointless serialisation for the rest -- and cancelling misc-cx moved
# the hold to the next waiting change. A younger change that ASSEMBLES with a waiting one (git merge-tree clean) is
# checked on main now and, waiting too, meets it in the batch at green, assembled in order. Only one that cannot be
# assembled with it waits (its author told: CONFLICTS -- a check of it is spent on a tree that cannot land), and a red
# fix never does. queue.py overlap_hold, pair_conflict; tests: tests/test_queue_sim.py CollisionTest (the three
# collisions of 2026-11-17 replayed), tests/test_backlog_batch.py OverlapTest.
OVERLAP_OLDER = ("queued", "checking", "checked", "pushing", "waiting")
_overlap_known = process_cache({})  # (older id, head, younger id, head) -> (shared files, conflicting files or None)


def pair_conflict(repo, older, younger):
    """The files changes `older` and `younger` conflict in when assembled together (git merge-tree of their heads:
    exit 1 lists them), or None: they merge cleanly, or git cannot tell (an old git, a head missing)."""
    r = git(repo, "merge-tree", "--write-tree", "--name-only", "--no-messages", older["head"], younger["head"],
            check=False)
    if r.returncode != 1:
        return None
    return [x for x in r.stdout.splitlines()[1:] if x.strip()] or ["(a file both change)"]


# THE BATCH TIERS (the owner, 2026-11-21: "make agentctl batch even more, merge / fix conflicts"): two queued changes
# that overlap are one backlog batch when (1) git merges them cleanly, (2) the overlap is in generated files (rows
# merged, else regenerated; a snapshot always regenerated, its output explained by the sides), or (3) their source
# conflict is additions on both sides (pair_resolvable here; resolved in the batch's assembly, whose full check is the
# verification); else (4) the younger waits and its author is told, the hunks and the fix named (conflict_hunks).
# AGENTCTL.md THE BATCH TIERS; waiting.py SNAPSHOTS ARE REGENERATED, NEVER MERGED and A SOURCE CONFLICT IS RESOLVED IN
# THE BATCH.
def pair_resolvable(repo, older, younger, files):
    """The files of `files` (where the two changes' heads conflict) that the backlog batch's assembly resolves
    (THE BATCH TIERS): UNION_SAFE prose, and source whose conflicting hunks are additions on both sides
    (waiting.py hunks_resolve, over the heads' merge base)."""
    mb = git(repo, "merge-base", older["head"], younger["head"], check=False).stdout.strip()
    out = []
    for f in files:
        if union_safe(f):
            out.append(f)
            continue
        got = [git(repo, "show", f"{x}:{f}", check=False) for x in (mb, older["head"], younger["head"])] if mb else []
        if got and all(r.returncode == 0 for r in got) and hunks_resolve(*(r.stdout for r in got)) is not None:
            out.append(f)
    return out


def overlap_of(repo, older, younger, strict=False):
    """(the files both change, the files they conflict in or None, the conflicting files the batch resolves), per pair
    of heads (cached in the process); the repo's generated files are neither (GENERATED FILES ARE NO OVERLAP,
    waiting.py) unless strict (a change whose generated files or source conflict could not be resolved in a batch:
    held as before), and with strict every textual conflict is one."""
    key = (older["id"], older.get("head"), younger["id"], younger.get("head"), strict)
    if key not in _overlap_known:
        shared = sorted(set(change_paths_of(repo, older) or []) & set(change_paths_of(repo, younger) or []))
        shared = shared if strict else without_generated(repo, shared)
        conflict = pair_conflict(repo, older, younger) if shared else None
        conflict = conflict if strict or not conflict else (without_generated(repo, conflict) or None)
        resolvable = [] if strict or not conflict else pair_resolvable(repo, older, younger, conflict)
        conflict = [f for f in conflict or [] if f not in resolvable] or None
        _overlap_known[key] = (shared, conflict, resolvable)
    return _overlap_known[key]


# A GENERATED OVERLAP WAITS FOR A CHECK IN FLIGHT (2026-11-21, measured in the queue simulator before choosing): a
# queued change sharing only generated files (baselines, hand-written tables, snapshots) with a change QUEUED before it
# is no overlap -- the backlog batch checks them as one tree (THE BATCH TIERS, tier 2). With the older already IN
# FLIGHT (checking, checked, pushing: past the batch it could have joined), checking the younger now spent a check that
# main moving under it then voids: both read the generated files, so its push owes a verify round, often two. Over 20
# seeds the six baseline lanes of 2026-11-19 ran 148 checks unheld, 40 held behind the change in flight (median 5 ->
# 6 ticks, p90 9 -> 7, mean 5.4 both); the two holds of 2026-11-21 as observed, 100 checks -> 40 (median 5 both, p90
# 8 -> 9). So the younger waits, quietly (A HELD WAIT IS NO ESCALATION), and meets the others waiting in the next
# batch, its generated files merged or regenerated there. Never behind a `waiting` change (it holds nothing it
# assembles with), and a red fix never waits.
IN_FLIGHT = ("checking", "checked", "pushing")


def generated_overlap(repo, older, younger):
    """The registered generated files both changes change (GENERATED FILES ARE NO OVERLAP's, for the in-flight wait)."""
    both = sorted(set(change_paths_of(repo, older) or []) & set(change_paths_of(repo, younger) or []))
    return [f for f in both if generated_entry(repo, f)]


def main_conflict(repo, e):
    """(main, the files) when queued change e no longer applies on the current main (main moved since its base and
    they conflict: git merge-tree), else None; asked once per main and head."""
    main = git(repo, "rev-parse", f"{remote()}/main", check=False).stdout.strip()
    if not main or main == e.get("base") or not e.get("head"):
        return None
    key = ("main", main, e["id"], e.get("head"))
    if key not in _overlap_known:
        files = pair_conflict(repo, {"id": "main", "head": main}, e)
        _overlap_known[key] = without_generated(repo, files) if files else files  # regenerated in its rebase
    files = _overlap_known[key]
    return (main, files) if files else None


def overlap_hold(repo, d, e, entries, facts, t):
    """(older entry, shared files, conflicting files or None) queued change e waits for (OVERLAPPING CHANGES ARE
    CHECKED AS ONE), or None."""
    if e.get("backlog_batch"):
        return None
    together, first = None, None
    ids = {x["id"] for x in entries}
    entries = list(entries) + [w for w in waiting_entries(d) if w["id"] not in ids]  # and those waiting for green
    for o in sorted(entries, key=lambda x: x["seq"]):
        # A KEPT PLACE NEVER JUMPS A CHECK IN FLIGHT (2026-11-20, the simulated 2026-11-19 seed 0: a change queued again
        # after a conflict took its first entry's seq, 10, and so was "older" than a change it overlaps that was
        # already checking, seq 20 -- checked beside it, then fully rechecked once that landed). A younger-by-seq
        # change counts too once it is past queued: its check (or push) runs now, before this one could.
        if o["seq"] >= e["seq"] and o.get("status") == "queued":
            continue
        if o["id"] == e["id"] or o.get("status") not in OVERLAP_OLDER \
                or same_change_source(o, e["worktree"], e.get("branch") or ""):  # its own older version: superseded
            continue
        shared, conflict, resolvable = overlap_of(repo, o, e, strict=e.get("batch_alone") in ("regen", "resolve"))
        if not shared:
            gen = generated_overlap(repo, o, e) if o.get("status") in IN_FLIGHT else []
            if gen:  # A GENERATED OVERLAP WAITS FOR A CHECK IN FLIGHT: they meet in the next batch
                first = first or (o, gen, None)
            continue
        if conflict is None and o.get("status") == "queued" and not o.get("backlog_batch"):
            if together is None:
                together = backlog_batchable(repo, d, e, t, facts)
            if together and backlog_batchable(repo, d, o, t, facts):
                continue  # the next backlog batch checks them as one (its assembly resolves the `resolvable`)
        conflict = conflict or resolvable or None  # resolved only in a batch of the two: else it cannot be assembled
        if conflict:  # the one it cannot be assembled with is the one its author hears of
            return o, shared, conflict
        if o.get("status") == "waiting":  # A WAITING CHANGE HOLDS NOTHING it assembles with: they meet in the batch
            continue
        first = first or (o, shared, conflict)
    return first


# A HELD WAIT IS NO ESCALATION (2026-11-19: phase3-agreement, -f2 and -enrichment2 each escalated to the owner every
# 15 min -- "queued for 45 min, waiting on a check slot" -- while each was held behind an older change it overlaps:
# F2 behind agreement on coupling-baseline.tsv, E2 behind F2 on TestWiring.scala). `queued`'s deadline loops into
# itself, and every lap raised the level and notified. Now each tick records WHY a queued change waits on its item
# (`hold`: the overlap and its blocking change and files, the batch, every check tree busy, the governor) -- `land
# status` and `status` show it -- and a lap past the deadline is QUIET while that hold is live: its blocker still in
# flight (whose own machine escalates should IT stick), a batch or busy trees (their holders' deadlines bound them).
# It escalates only when the hold is stuck: nothing named holds it, its blocking change is gone, or it is older than
# CHANGE_HOLD_MAX (the `waiting` state's own bound).
CHANGE_HOLD_MAX = 6 * 3600  # s ($AGENTCTL_CHANGE_HOLD_MAX)


def note_hold(st, e, kind, why, t, blocker=None, files=None, batch=None):
    """Record why queued change e waits (None: nothing holds it now), on its item, when that changed. kind is a row of
    THE HOLD TABLE (policy.py HOLDS): an unregistered kind is refused (ValueError), so no wait goes unbounded."""
    if kind is not None:
        hold_kind(kind)
    it = st.item("change", e["id"])
    if not it or machine_state("change", it["state"]).terminal:
        return
    cur = (it.get("data") or {}).get("hold") or {}
    if (cur.get("why") or None) == why:
        return
    rec = None if not why else {"kind": kind, "why": why, "blocker": blocker, "files": files, "batch": batch,
                                "since": cur.get("since") if cur.get("kind") == kind and cur.get("blocker") == blocker
                                else t}
    try:
        st.update_item("change", e["id"], it["version"], {"hold": rec})
    except StaleVersion:
        pass  # the next tick records it


def restart_hold(st, e, t):
    """The recorded hold of change e, its clock (since) restarted at t: re-derived this tick (stuck_recovery)."""
    it = st.item("change", e["id"])
    hold = ((it or {}).get("data") or {}).get("hold")
    if not hold:
        return
    try:
        st.update_item("change", e["id"], it["version"], {"hold": {**hold, "since": t}})
    except StaleVersion:
        pass


def change_hold_now(it, e, t):
    """The hold a queued change waits on now: its recorded one, its retry, or the governor's slot; None: nothing."""
    if e.get("retry_after", 0) > t:
        return {"kind": "retry", "why": "its retry (an infra failure of its last check)"}
    if e.get("waiting_on"):
        return {"kind": "governor", "why": f"waiting on {e['waiting_on']}"}
    return (it.get("data") or {}).get("hold")


def change_hold_stuck(st, hold, it, t):
    """Why a queued or checked change's hold is stuck (an escalation), or None (a legitimate wait: quiet)."""
    if not hold:
        return "nothing holds it, yet it has not " + ("pushed" if it["state"] == "checked" else "started")
    if hold_recovers_itself(hold):
        return None  # its own recovery runs at its bound (regen_hold_overdue): never a stuck lap, the owner told once
    if t - float(it.get("entered_at") or t) > env_num("AGENTCTL_CHANGE_HOLD_MAX", CHANGE_HOLD_MAX):
        return f"held for over {fmt_age(env_num('AGENTCTL_CHANGE_HOLD_MAX', CHANGE_HOLD_MAX))}"
    if hold.get("blocker"):
        b = st.item("change", hold["blocker"])
        if b is None or machine_state("change", b["state"]).terminal:
            return f"its blocking change {hold['blocker']} is {'gone' if b is None else b['state']}"
    hk = HOLDS.get(hold.get("kind"))
    if hk and hold.get("since") and t - float(hold["since"]) > hk.bound:
        return f"held on {hk.label} for over {fmt_age(hk.bound)} (that hold's bound)"
    return None


def hold_recovers_itself(hold):
    """A hold whose kind's recovery is `recover` (THE HOLD TABLE): the change tick runs it at the bound itself."""
    hk = HOLDS.get((hold or {}).get("kind"))
    return bool(hk) and hk.recovery == "recover"


def stuck_recovery(hold, stuck):
    """What the change tick does with a stuck hold (THE HOLD TABLE's recovery): `rederive` -- the recorded hold is
    dropped and this tick decides afresh -- when its blocker is gone or its kind's own bound passed and the table says
    so; else `escalate` (nothing recorded, the 6-h cap: the wait is real, or agentctl's bug)."""
    if not hold or not stuck or stuck.startswith("held for over"):
        return "escalate"
    if stuck.startswith("its blocking change"):
        return "rederive"
    hk = HOLDS.get(hold.get("kind"))
    return hk.recovery if hk else "escalate"


def conflict_hunks(repo, older, younger, files, limit=3):
    """The first conflicting hunk of each of the files (at most `limit`), in words -- `F line N: <older's line> vs
    <this change's line>` -- for the author told CONFLICTS (THE BATCH TIERS, tier 4); [] when git cannot tell."""
    out = []
    for f in [x for x in files if " (" not in x][:limit]:
        try:
            mb = git(repo, "merge-base", older["head"], younger["head"], check=False).stdout.strip()
            got = [git(repo, "show", f"{x}:{f}", check=False) for x in (mb, older["head"], younger["head"])] if mb else []
        except Exception:  # noqa: BLE001 -- a hint, never a reason the author is not told
            got = []
        if not got or any(r.returncode != 0 for r in got):
            continue
        hunk = first_hunk(*(r.stdout for r in got))
        if hunk:
            n, theirs, mine = hunk
            out.append(f"{f} line {n}: {older['id']} has `{theirs[:80]}`, this change `{mine[:80]}`")
    return out


def hold_for_overlap(st, d, e, o, shared, conflict, waits=True, repo=None):
    """The change tick's lines for a change held behind an older one it overlaps (waits; a red fix's: not held); its
    author told once of a conflict."""
    did = []
    if conflict and e.get("conflict_told") != o["id"]:
        it = st.item("change", e["id"])
        hunks = conflict_hunks(repo, o, e, conflict) if repo else []
        text = (f"CONFLICTS {e['id']}: it cannot be assembled with {o['id']} ({o.get('worktree')}), queued before it "
                f"(both change {', '.join(conflict[:3])})"
                + (f": its check waits for {o['id']}" if waits else ": a red fix, it is checked now all the same")
                + f"; if {o['id']} lands first, this change no "
                f"longer applies -- rebase onto it (its branch, or {remote()}/main once it landed) and land again, or "
                "`agentctl land cancel` this one"
                + (f". The hunks: {'; '.join(hunks)}" if hunks else "")
                + (f". Fix now: `cd {e.get('worktree')} && git rebase {o['branch']}`, resolve those hunks, "
                   "`~/bin/agentctl land`" if o.get("branch") and e.get("worktree") else ""))
        if it and not machine_state("change", it["state"]).terminal:
            try:
                st.transition("change", e["id"], it["state"], it["version"],
                              event={"text": text, "audience": {"worktree": e.get("worktree")}, "severity": "info"})
            except StaleVersion:
                return did
        e["conflict_told"] = o["id"]
        save_entry(d, e)
        wlog(d, text)
        did.append(text)
    if waits and _behind_said.get((e["id"], "overlap")) != o["id"]:
        _behind_said[(e["id"], "overlap")] = o["id"]
        did.append(f"{e['id']}: its check waits for {o['id']} ({o.get('status')}): both change {shared[0]}"
                   + (f" (+{len(shared) - 1} more)" if len(shared) > 1 else ""))
    return did


def spawn_executor(repo, d, eid):
    """`agentctl waiting check <eid>`, detached: the change's checks (check_entry)."""
    with open(os.path.join(d, "processor.log"), "a") as logf:
        spawn_detached([sys.executable, TOOL_PATH, "waiting", "check", eid, "--repo", repo],
                         stdin=subprocess.DEVNULL, stdout=logf, stderr=logf, start_new_session=True, cwd=repo,
                         env=detached_env(repo))
    time.sleep(1)  # it takes its tree slot before the next looks


# ONE CHANGE, ONE ITEM (2026-11-18 07:15Z: `waiting: 6 — movies-sentry, movies-tg-inapp, flapmem, movies-sentry, ...`
# for the three changes of batch #36). A checked change meeting the red gate joined `waiting` through land's join: a
# waiting entry with an id of its own beside the queued one (status `joined`), and the server tick projected that entry
# into a second open change item -- every held change counted and listed twice by the status line, `land status`, the
# watchdog and the stats. Now the queued change ITSELF waits: its entry joins the waiting entries the batch reads (its
# tip the one it was checked at, its commits and counting fixes as land's join records them) and its item moves to
# `waiting`; the batch lands, rejects or supersedes it under the id its `land` printed. A `joined` entry on disk (from
# before) still follows the waiting entry that carries it (joined_changes, project_changes) until it is decided.
QUEUE_ONLY_FIELDS = ("backlog_batch", "batch_alone", "push_alone", "drift_split", "remote_run")  # a check's, not a wait's


def queue_join(repo, d, e, tree, head, why=None):
    """Under the landing lock, main red (or its checks failing only as its base fails, why): the checked change waits
    for green at `head` (ONE CHANGE, ONE ITEM, above); its WAITING note says why."""
    commits = waiting_commits(repo, f"{remote()}/main", head)
    supersede_older_waiting(d, e["worktree"], e.get("branch") or "", e["id"])
    for k in QUEUE_ONLY_FIELDS:
        e.pop(k, None)
    e.update(tip=head, commits=commits, fixes=sorted(fixes_of(repo, commits)))
    line = note_waiting(d, os.path.join(d, f"check-{e['id']}"), head, e["id"]) + (f": {why}" if why else "")
    # policy: red-gate
    set_change(d, e, "waiting", event=change_event(e, line, code=None, final=False), decided=ts())
    wlog(d, f"{e['id']}: {line}")
    return 0


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
        ok, out = remote_check(repo, e, base, head, mode, own, red=cls == "red")
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
