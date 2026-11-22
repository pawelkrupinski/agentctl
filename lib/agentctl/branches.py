# agentctl, part `branches`: branch runs and dispatch owners.
# Runs inside agentctl's one namespace, after the parts before it in lib/agentctl/__init__.py's PARTS (it uses their names).

# ---------------------------------------------------------------- branch runs: every dispatch has an owner, every branch red a route
# The owner (2026-10-27): "red not captured, make a structural fix". Identity model convergence #2116 (run 3264) on
# measure/pawel/982f7681f-0b04a4 failed and nobody was told: the snapshot, the status line and the reds covered main only,
# and a branch run (measure/*, check/*, a red fixer's) had no owner and no route.
#
# OWNER RECORDS, ~/.cache/agentctl/dispatches/<key>.json ($AGENTCTL_DISPATCH_DIR): every CI run agentctl or an agent
# dispatches -- the branch and/or run id, the worktree, the session or agent name, the pid, the purpose, the inputs, the
# time. `agentctl dispatch run` (a `gh workflow run` wrapper) and `agentctl dispatch record` write one; so do
# agentctl's own dispatches (the queue's remote checks, the Fixes-Red verification). A branch run with none (the movies
# scripts call gh themselves) is matched after the fact: measure/<user>/<sha>-<x> (else the run's head commit) to
# the worktree whose HEAD is that commit or whose HEAD reflog holds it; check/<entry> to that queue entry's worktree. The
# match is recorded, so it is made once.
#
# THE SNAPSHOT also records the newest completed run of each tracked branch and workflow (a branch with an owner record,
# or measure/*, check/*, red-*). A failed one is ONE inbox entry, `BRANCH RED: <workflow> #N on <branch> → owner <...>`
# with the first error lines (a wake-up kind for `inbox wait`). It stays a branch red until its branch is deleted or a
# newer run of that workflow on the branch is green.
#
# PREDICTS MAIN RED: the failing run is built on commits all on main now (ancestor or patch-equivalent), AND it ran with
# the inputs main's run of that workflow uses (the dispatch inputs, minus INPUTS_NOT_PREDICTIVE). Main will hit it next,
# so it is claimed and dispatched like a main red, before main fails. Inputs that differ make it a plain BRANCH RED, the
# line naming them ("recording 3166 ≠ main's 573": run 3264 replayed the fresh 10-27 recording, main replays the pinned
# one with its fixture fills). A measure on a NEWER recording that fails hermetically (a test failure, not infra) also
# says "blocks promoting recording N": that recording lacks something a promotion of it will need.
DISPATCH_PATTERNS = ("measure/", "check/", "red-")
MEASURE_BRANCH = re.compile(r"^measure/[^/]+/([0-9a-f]{7,40})-[0-9A-Za-z]+$")
# inputs that change where or how a run runs, or how much of it, never what main's run of the same workflow would decide
INPUTS_NOT_PREDICTIVE = frozenset({"tier", "runner", "profile", "jvm-options", "heap", "reuse-run", "takes-base",
                                   "takes-countries", "countries"})
# What main's convergence replays when its `recording` input is empty: each country's pin, the first word of the
# `hermetic-<code>.txt` asset of the release convergence-setup.sh reads ($FIXTURE_RELEASE_TAG in the convergence
# workflow on origin/main). Read from the movies repo by the laptop's processor with the owner's token
# (refresh_pinned_recording; the local processor, or the sync in remote-store mode, which also writes them into the
# store as the meta record PIN_META), cached in pinned-recording.json; $AGENTCTL_PINNED_RECORDING overrides. The
# reconciler on ci-runner NEVER reads release assets (ci-lander is push-only: repo.code + repo.actions, no releases):
# it copies the store's record, and while the laptop is offline the last stored pins stand (`status` shows their age,
# STALE past PIN_STALE). Never hard-coded (it was "573", and goes stale when a recording is promoted).
CONVERGENCE_WORKFLOW = ".github/workflows/identity-model-convergence.yml"
PIN_REFRESH = 3600
PIN_RETRY = 300  # s after a failed read before the next try (never one error line per tick)
PIN_STALE = 24 * 3600
PIN_META = "pinned_recording"
BRANCH_RUNS_LIMIT = 150
BRANCH_ROUTE_MAX_AGE = 24 * 3600  # a failure first seen older than this is recorded, not routed (no flood on upgrade)
OWNER_REMATCH = 3600  # an unmatched branch is matched again after this long


def dispatch_key(branch=None, run_id=None):
    return f"branch-{re.sub(r'[^A-Za-z0-9._-]', '_', branch)}" if branch else f"run-{run_id}"


def save_dispatch(rec, key=None):
    """Write an owner record (merged over an existing one: a later field wins, a None never erases). Returns it."""
    p = dispatches_dir(f"{key or dispatch_key(rec.get('branch'), rec.get('run_id'))}.json")
    old = read_record(p) or {}
    rec = {**old, **{k: v for k, v in rec.items() if v is not None}}
    write_json(p, rec)
    return rec


def record_dispatch(branch=None, run_id=None, workflow=None, purpose="", inputs=None, worktree=None, sha=None,
                    agent=None, pid=None, key=None, source="registered"):
    """The owner record of a dispatch made from here: who (agent name, Claude session, pid) and where (worktree)."""
    if not branch and not run_id:
        raise SystemExit("agentctl: a dispatch is recorded by its branch or its run id")
    wt = worktree or worktree_of(safe_cwd())
    cp = os.environ.get("CLAUDE_PID") or ""
    pid = pid or (int(cp) if cp.isdigit() else os.getppid())
    return save_dispatch({"branch": branch, "run_id": int(run_id) if str(run_id or "").isdigit() else None,
                          "workflow": workflow, "purpose": purpose, "inputs": inputs, "worktree": wt, "sha": sha,
                          "session": os.environ.get("CLAUDE_CODE_SESSION_ID") or None,
                          "agent": agent or agent_name(), "pid": pid,
                          "process_start": process_start(pid) if pid else None, "at": ts(), "epoch": time.time(),
                          "source": source}, key)


def dispatch_records():
    out = []
    for p in sorted(glob.glob(dispatches_dir("*.json"))):
        r = read_record(p)
        if r:
            out.append(r)
    return out


def dispatch_for(branch, run_id=None):
    """The owner record of a branch run: its run's, else its branch's (None when there is none)."""
    for key in ([dispatch_key(None, run_id)] if run_id else []) + ([dispatch_key(branch)] if branch else []):
        r = read_record(dispatches_dir(f"{key}.json"))
        if r and (r.get("worktree") or r.get("agent") or r.get("session")):
            return r
    return None


def tracked_branch(name, recorded=()):
    return bool(name) and name not in PROTECTED_BRANCHES and (name.startswith(DISPATCH_PATTERNS) or name in recorded)


INTERNAL_TREE = re.compile(r"-wt-(check-\d+|waiting|batch)$")  # legacy fallback: trees made before they were tagged


def internal_trees_dir(*parts):
    d = os.environ.get("AGENTCTL_INTERNAL_TREES_DIR") or state_root("internal-trees")
    return os.path.join(d, *parts)


def mark_internal(path):
    """Tag a worktree agentctl made for itself (check-N, baseline, recheck, waiting, batch, a finished red fixer's):
    a registry entry, so owner and fixer detection exclude it by the tag, not by what the tree is called."""
    if not path:
        return path
    try:
        import hashlib
        p = os.path.realpath(path)
        os.makedirs(internal_trees_dir(), exist_ok=True)
        write_json(internal_trees_dir(hashlib.sha1(p.encode()).hexdigest() + ".json"), {"path": p, "at": ts()})
    except OSError:
        pass
    return path


def internal_tree(path):
    """agentctl's own trees -- tagged at creation (mark_internal): the queue's check trees, the baseline / recheck /
    `waiting` / batch trees, a finished red fixer's tree, the queue's work clone, tool caches -- never an owner, author
    or fixer (2026-10-27: the status line said `main #2146 → check-1 committed` for a fix that had landed; ci-why
    credited movies-wt-baseline-65721). A queued change's owner is its queue entry's source worktree."""
    import hashlib
    p = os.path.realpath(path or "")
    return os.path.exists(internal_trees_dir(hashlib.sha1(p.encode()).hexdigest() + ".json")) \
        or bool(INTERNAL_TREE.search(p.rstrip("/"))) or p.startswith(os.path.realpath(CACHE_TREES).rstrip("/") + "/") \
        or p.startswith(os.path.realpath(waiting_dir()).rstrip("/") + "/")


def worktree_holding(repo, sha):
    """The worktree whose HEAD is sha, else the one whose HEAD reflog holds it (the agent's own commit, since rebased).
    Never one of agentctl's internal trees."""
    try:
        trees = [w for w in worktrees(repo) if isinstance(w.get("worktree"), str) and os.path.isdir(w["worktree"])
                 and not internal_tree(w["worktree"])]
    except (RuntimeError, OSError):
        return None
    trees = trees[1:] + trees[:1]  # an agent's worktree before the root checkout (often on main itself)
    for w in trees:
        if str(w.get("HEAD", "")).startswith(sha):
            return w["worktree"]
    for w in trees:
        r = git(w["worktree"], "reflog", "--format=%H", "HEAD", check=False)
        if r.returncode == 0 and any(h.startswith(sha) for h in r.stdout.split()):
            return w["worktree"]
    return None


def queue_entry_worktree(entry_id):
    for p in glob.glob(os.path.join(waiting_dir(), "*", "entries", f"{entry_id}.json")):
        e = read_record(p) or {}
        if e.get("worktree"):
            return e["worktree"]
    return None


def branch_base(branch, run):
    """The commit a branch run was built on: a measure/<user>/<sha>-<x> branch names it (identity-ci-run pushes main's CI
    laid over it), else the run's head commit."""
    m = MEASURE_BRANCH.match(branch or "")
    return m.group(1) if m else run.get("headSha")


def owner_of(repo, branch, run):
    """The owner record of a branch run, matched after the fact (and recorded) when nobody recorded one."""
    rid = run.get("databaseId")
    rec = dispatch_for(branch, rid)
    if rec and owner_gone(rec):  # its worktree removed / pid dead: a live worktree holding its commit takes it over
        base = branch_base(branch, run)
        wt = repo and base and commit_on_main(repo, base) is False and worktree_holding(repo, base)  # main's are everywhere
        if wt:  # another worktree, or the same one outliving its agent's process: the worktree owns it now
            new = {"branch": branch, "run_id": rid, "workflow": run.get("workflowName"), "worktree": wt, "sha": base,
                   "purpose": f"re-matched: its owner {owner_text(rec)} is gone", "source": "matched", "at": ts(),
                   "epoch": time.time()}
            for key in ([dispatch_key(None, rid)] if rid else []) + [dispatch_key(branch)]:  # REPLACED, not merged:
                if key == dispatch_key(branch) or os.path.exists(dispatches_dir(f"{key}.json")):  # no dead pid stays
                    write_json(dispatches_dir(f"{key}.json"), new)
            return new
        return rec  # nobody holds it: orphan_why closes it, naming the gone owner
    if rec:
        return rec
    key = dispatch_key(branch)
    old = read_record(dispatches_dir(f"{key}.json")) or {}
    if old.get("source") == "unmatched" and time.time() - float(old.get("epoch") or 0) < OWNER_REMATCH:
        return None
    wt, how = None, None
    if branch.startswith("check/"):
        # the lane and mode prefixes off (remote_check: check/red-<...>; remote_mode_prefix: check/fast-<id>,
        # check/verify-<sha>-<id>): the entry's own id
        wt, how = (queue_entry_worktree(re.sub(r"^(red-)?(fast-|verify(areas)?-[0-9a-f]{40}-)?", "", branch[len("check/"):])),
                   "its queue entry")
    base = branch_base(branch, run)
    if not wt and repo and base:
        wt, how = worktree_holding(repo, base), f"the worktree that holds {base[:9]}"
    if not wt:
        save_dispatch({"branch": branch, "run_id": rid, "workflow": run.get("workflowName"), "source": "unmatched",
                       "at": ts(), "epoch": time.time()}, key)
        return None
    return save_dispatch({"branch": branch, "run_id": rid, "workflow": run.get("workflowName"), "worktree": wt,
                          "sha": base, "purpose": f"matched by branch name ({how})", "source": "matched",
                          "at": ts(), "epoch": time.time()}, key)


def owner_gone(rec):
    """True when the owner is gone: its worktree removed, or its recorded pid dead. None: no owner known."""
    if not rec or not (rec.get("worktree") or rec.get("pid")):
        return None
    if rec.get("worktree") and not os.path.isdir(rec["worktree"]):
        return True
    if rec.get("pid") and not proc_alive(rec["pid"], rec.get("process_start")):
        return True
    return False


def commit_on_main(repo, base):
    """True/False: base is on main (ancestor or patch-equivalent); None: unknown here (no repo or no base). A commit
    this clone does not hold is not on main."""
    if not repo or not base:
        return None
    if not git_ok(repo, "cat-file", "-e", f"{base}^{{commit}}"):
        return False
    try:
        return bool(merged_reason(repo, base, f"refs/remotes/{remote()}/main"))
    except (Exception, SystemExit):
        return None


def orphan_why(repo, rec, base, main_runs_it=True, main_since=None):
    """Why a branch red is an ORPHAN, else None: no LIVE owner (no record, or its worktree removed / pid dead -- 2026-11-02:
    eight reds sat on the status line naming worktrees removed days before) and no worktree holding its commit, AND nothing on main
    to compare it with -- its commit is not on main, or it is but main runs no such workflow (it can never PREDICT MAIN
    RED). A commit on main whose workflow main runs keeps today's behaviour; an unknown answer is never an orphan."""
    gone = owner_gone(rec)
    if rec and not gone:  # a live owner (or one we can't judge: no worktree or pid recorded) keeps it
        return None
    on = commit_on_main(repo, base)
    if on is None:
        return None
    sha = str(base)[:9]
    why_gone = ("its worktree removed" if rec and rec.get("worktree") and not os.path.isdir(rec["worktree"])
                else "its process ended")
    who = (f"its owner {owner_text(rec, short=True)} is gone ({why_gone})" if gone else "no owner record") \
        + f", no worktree holds its commit {sha}"
    if not on:
        return f"{who}, and it is not on main"
    if not main_runs_it:
        return f"{who}, and main runs no such workflow"
    if main_since:  # main's own run of the workflow came after it, on a commit holding this one: it predicts nothing
        return f"{who}, and main's own run #{main_since} came since"
    return None


def owner_text(rec, short=False):
    if not rec:
        return "NOBODY (no owner record, no worktree holds its commit)"
    wt = rec.get("worktree") or ""
    who = rec.get("agent") or (f"session {rec['session'][:8]}" if rec.get("session") else None)
    if short:
        return os.path.basename(wt) if wt else (who or "?")
    return wt.replace(HOME, "~") + (f" ({who})" if who and who != os.path.basename(wt) else "") if wt else (who or "?")


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


def pinned_path():
    return os.path.join(os.path.dirname(reds_dir()), "pinned-recording.json")


def pin_release_tag(repo):
    """The release the convergence pins live in: FIXTURE_RELEASE_TAG as main's convergence workflow sets it."""
    r = git(repo, "show", f"refs/remotes/{remote()}/main:{CONVERGENCE_WORKFLOW}", check=False) if repo else None
    tags = re.findall(r"FIXTURE_RELEASE_TAG:\s*['\"]?([\w.-]+)", r.stdout if r and r.returncode == 0 else "")
    return tags[0] if tags else None


def read_pins(o_r, tag):
    """{country code: pinned recording run} from the release's hermetic-<code>.txt assets."""
    rel = api(o_r, "GET", f"releases/tags/{urllib.parse.quote(tag)}")
    pins = {}
    for a in (rel or {}).get("assets") or []:
        m = re.fullmatch(r"hermetic-(\w+)\.txt", a.get("name") or "")
        if not m:
            continue
        first = (forge_release_asset(o_r, tag, a["name"]).decode(errors="replace").splitlines() or [""])[0].split()
        if first and first[0].isdigit():
            pins[m.group(1)] = first[0]
    return pins


_pin_tried = [0.0]


def stored_pins(st=None):
    """The pins record the store holds (PIN_META), or None."""
    try:
        v = (st or open_store()).meta_get(PIN_META)
        rec = json.loads(v) if v else None
    except (Exception, SystemExit) as e:
        err(f"pinned recording: the store's copy not read ({str(e).replace('agentctl: ', '')[:160]})")
        return None
    return rec if isinstance(rec, dict) and rec.get("pins") else None


def store_pins(rec, st=None):
    """The pins into the store (PIN_META) for the reconciler; the local cache marks them stored. Never fatal."""
    try:
        (st or open_store()).meta_set(PIN_META, json.dumps({k: rec[k] for k in ("at", "repo", "tag", "pins")}))
    except (Exception, SystemExit) as e:
        err(f"pinned recording: not stored ({str(e).replace('agentctl: ', '')[:160]})")
        return rec
    rec = {**rec, "stored": True}
    write_json(pinned_path(), rec)
    return rec


def refresh_pinned_recording(o_r, repo, now_=None, force=False, role=None):
    """The convergence pins, never fatal. The reconciler (role server) copies the store's record into its cache and
    never reads release assets itself. The laptop (local, or the sync in remote-store mode) re-reads them from the
    release at most every PIN_REFRESH s (PIN_RETRY s after a failure) with the token it holds, and writes them into the
    store; pins read but not yet stored (the store did not answer) are stored at the next call."""
    t = now_ if now_ is not None else time.time()
    rec = read_record(pinned_path()) or {}
    if (role or host_role()) == "server":
        got = stored_pins()
        if got and {k: got.get(k) for k in ("at", "tag", "pins")} != {k: rec.get(k) for k in ("at", "tag", "pins")}:
            rec = {**got, "stored": True}
            write_json(pinned_path(), rec)
        return rec
    if rec.get("pins") and not rec.get("stored"):
        rec = store_pins(rec)
    if not force and (t - float(rec.get("at") or 0) < PIN_REFRESH or t - _pin_tried[0] < PIN_RETRY):
        return rec
    _pin_tried[0] = t
    try:
        tag = pin_release_tag(repo)
        if not tag:
            return rec
        pins = read_pins(o_r, tag)
        if pins:
            rec = {"at": t, "repo": o_r, "tag": tag, "pins": pins}
            write_json(pinned_path(), rec)
            rec = store_pins(rec)
    except (Exception, SystemExit) as e:
        err(f"pinned recording: not re-read ({str(e)[:160]})")
    return rec


def pins_status(rec=None, now_=None):
    """`status`'s line for the pins: which, from which release, and how old (STALE past PIN_STALE)."""
    rec = read_record(pinned_path()) if rec is None else rec
    if not (rec or {}).get("pins"):
        return "pins: unread (the laptop's processor reads them from the release and stores them)"
    age = (now_ if now_ is not None else time.time()) - float(rec.get("at") or 0)
    runs = "/".join(sorted(set(rec["pins"].values()), key=lambda x: int(x) if x.isdigit() else 0))
    line = f"pins: {runs} ({rec.get('tag')}), read {fmt_age(age)} ago"
    if age > PIN_STALE:
        line += "  STALE: the laptop's processor re-reads them hourly (owner's token); until then these stand"
    return line


def pinned_runs():
    """The recordings main's convergence replays by default (one per pinned country, deduplicated), or []."""
    if os.environ.get("AGENTCTL_PINNED_RECORDING"):
        return [os.environ["AGENTCTL_PINNED_RECORDING"]]
    pins = (read_record(pinned_path()) or {}).get("pins") or {}
    return sorted(set(pins.values()), key=lambda x: int(x) if x.isdigit() else 0)


def pinned_recording():
    """main's pinned recording as a name: `573`, `528/573` when the countries' pins differ, or `the pinned pair
    (unread)`."""
    runs = pinned_runs()
    return "/".join(runs) if runs else "the pinned pair (unread)"


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


def branch_state(o_r, repo, prev=None):
    """[entry] per tracked (branch, workflow): the newest completed run, its owner, whether its branch is alive, and for
    a red one whether it PREDICTS MAIN RED (why not). A red no longer listed carries over from prev until its branch
    is gone."""
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
    for wf, rs in list(_gate_lists.items()) + list(_outside_lists.items()):
        m = next((r for r in rs if r.get("status") == "completed"), None)
        if m and (wf not in newest_main or m["databaseId"] > newest_main[wf]["databaseId"]):
            newest_main[wf] = m
    for e in prev or []:  # a red past the listed window stays one until its branch goes or turns green
        k = (e["branch"], e["workflow"])
        if e.get("red") and k not in latest and e.get("alive"):
            latest[k] = dict(e["run"], headBranch=e["branch"], workflowName=e["workflow"])
    reds = {k: r for k, r in latest.items() if r.get("conclusion") in RED_CONCLUSIONS}
    recs = {r["databaseId"]: owner_of(repo, b, r) for (b, _wf), r in reds.items()}
    mains = {wf: newest_main.get(wf) for (_b, wf) in reds}
    inputs = run_inputs(list(reds.values()) + [m for m in mains.values() if m], recs)
    out = []
    for (b, wf), r in sorted(latest.items()):
        e = {"branch": b, "workflow": wf, "run": {k: r.get(k) for k in ("databaseId", "number", "conclusion", "headSha",
                                                                          "updatedAt", "url", "event")},
             "red": (b, wf) in reds}
        if e["red"]:
            rec, m = recs.get(r["databaseId"]), mains.get(wf)
            base = branch_base(b, r)
            mine, theirs = inputs.get(r["databaseId"]), inputs.get(m["databaseId"]) if m else None
            pred, why = predicts_main(repo, base, m, mine, theirs)
            e.update(owner=rec, owner_gone=owner_gone(rec), base=base, alive=branch_exists(o_r, b), predicts=pred,
                     why=why, orphan=orphan_why(repo, rec, base, main_runs_it=bool(m),
                                                  main_since=m.get("number") if m and m["databaseId"] > r["databaseId"] else None), main_run=m and {"databaseId": m["databaseId"], "number": m.get("number")},
                     newer_recording=newer_recording(mine, theirs) if mine is not None and theirs is not None else None)
        out.append(e)
    return out


def branch_reds(snap):
    """The snapshot's live branch reds: failed, branch still there (newest run of that workflow on it), not an orphan
    (an orphan closed itself: settle_branch_reds)."""
    return [e for e in (snap or {}).get("branches") or [] if e.get("red") and e.get("alive") and not e.get("orphan")]


# -- an ORPHAN branch red closes itself (the owner, 2026-10-29: `branch reds: 3 → NOBODY` -- identity runs #2365, #2370,
# #2371 on measure/* branches whose worktrees were removed -- sat unassigned forever). The "branch-red" machine holds
# the ones that closed: orphaned (no owner, nothing on main to compare with; its measure/* branch is for `agentctl
# cleanup`) -> open again when its commit turns up in a worktree or an owner record (routed to that owner) -> gone when
# its branch is deleted or a newer run on it is green.
def branch_red_id(o_r, rid):
    return f"{o_r}#{rid}"


def settle_branch_reds(o_r, snap, store=None):
    """Close the orphan branch reds (event: why), reopen the ones that found an owner (marked `reopened` in the snapshot
    so route_branch_reds tells that owner), retire the ones gone from the snapshot. Returns the event texts."""
    st = store or open_store()
    out, live = [], set()
    for e in (snap or {}).get("branches") or []:
        if not e.get("red") or not e.get("alive"):
            continue
        r = e["run"]
        iid = branch_red_id(o_r, r["databaseId"])
        live.add(iid)
        what = f"{e['workflow']} #{r.get('number')} on {e['branch']}"
        refs = {"run": r.get("number"), "workflow": e["workflow"], "branch": e["branch"]}
        it = st.item("branch-red", iid)
        if e.get("orphan"):
            if it and it["state"] == "orphaned":
                continue
            it = it or st.put_item("branch-red", iid, "open", {"branch": e["branch"], "workflow": e["workflow"],
                                                                "run": r})
            cleanup = "; its finished branch is for `agentctl cleanup`" if e["branch"].startswith("measure/") else ""
            text = f"branch red closed itself: {what}: {e['orphan']}{cleanup}"
            st.transition("branch-red", iid, "orphaned", it["version"], data={"why": e["orphan"]},
                          event={"text": text, "refs": refs})
            out.append(text)
        elif it and it["state"] == "orphaned":
            text = f"branch red reopened: {what} → owner {owner_text(e.get('owner'))}"
            st.transition("branch-red", iid, "open", it["version"], data={"why": None},
                          event={"text": text, "refs": refs})
            try:
                os.unlink(routed_path(r["databaseId"]))
            except OSError:
                pass
            e["reopened"] = True
            out.append(text)
    t = st.clock()
    for it in st.items("branch-red", ["open", "orphaned"]):
        if not it["id"].startswith(f"{o_r}#"):
            continue
        gone = it["id"] not in live
        if gone or it["deadline"] < t:
            d = it["data"]
            text = (f"branch red gone: {d.get('workflow')} #{(d.get('run') or {}).get('number')} on {d.get('branch')}"
                    + (" (its branch deleted or green)" if gone else " (past its deadline)"))
            st.transition("branch-red", it["id"], "gone", it["version"], event={"text": text})
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


def route_branch_reds(o_r, snap, now_=None):
    """ONE inbox entry per failed branch run (BRANCH RED) whose branch still exists, with ci-why's first error lines;
    signatures saved, so a PREDICTS MAIN RED run is a red unit. Returns the lines written."""
    t = now_ if now_ is not None else time.time()
    written = []
    for e in (snap or {}).get("branches") or []:
        r = e["run"]
        if not e.get("red") or not e.get("alive") or os.path.exists(routed_path(r["databaseId"])):
            continue  # a run on a deleted branch is dropped silently: its owner finished with it
        if e.get("orphan"):
            continue  # nobody to tell: it closed itself (settle_branch_reds); routed if it reopens
        try:
            done = dt.datetime.fromisoformat(str(r.get("updatedAt")).replace("Z", "+00:00")).timestamp()
        except ValueError:
            done = t
        if t - done > BRANCH_ROUTE_MAX_AGE and not e.get("reopened"):
            write_json(routed_path(r["databaseId"]), {"at": ts(), "routed": False, "why": "older than the route window"})
            continue
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
        written.append(line)
    return written


def predict_units(o_r, snap):
    """Red units for the branch reds that PREDICT MAIN RED: gate reds (claimed, RED UNCLAIMED, dispatched) before main
    fails. Their signatures are the run's: main failing the same way later is the same red."""
    units = []
    for e in branch_reds(snap):
        if not entry_predicts(e):
            continue
        run = dict(e["run"], headBranch=e["branch"], workflowName=e["workflow"])
        for u in gate_red_units(o_r, [(e["workflow"], run, [run], True)]):
            u["predicts"] = e["branch"]
            units.append(u)
    return units


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


def orphaned_red(store, o_r, runs):
    """The why of a run of these whose branch red closed as an orphan (settle_branch_reds), else None."""
    for r in runs or []:
        try:
            it = store and store.item("branch-red", branch_red_id(o_r, r.get("databaseId")))
        except (Exception, SystemExit):
            return None
        if it and it["state"] == "orphaned":
            return it["data"].get("why") or "no owner"
    return None


def orphan_branches(repo, o_r, dry, out, runs_for=None, store=None):
    """The sweep's part: remote tracked branches whose owner is gone (worktree removed or pid dead), or whose branch red
    closed as an orphan (no owner at all), and whose runs all finished. measure/* ones are deleted (the standing OK: a
    finished measure/* branch); the rest are listed."""
    # listing: one branch's runs; a branch is swept only when every run listed finished (an unlisted older one is done)
    runs_for = runs_for or (lambda b: gh_json(o_r, "run", "list", "--branch", b, "--limit", "20", "--json", RUN_FIELDS))
    recorded = {r.get("branch") for r in dispatch_records() if r.get("branch")}
    found = []
    try:
        st = store or open_store()
    except (Exception, SystemExit):
        st = None
    for line in git(repo, "ls-remote", "--heads", remote(), check=False).stdout.splitlines():
        sha, ref = line.split()
        b = ref.removeprefix("refs/heads/")
        if not tracked_branch(b, recorded) or b in SWEEP_KEEP_REMOTE:
            continue
        try:
            runs = runs_for(b)
        except (Exception, SystemExit) as e:
            out.append(f"  branch {b}: its runs unread ({str(e)[:100]}), kept")
            continue
        if any(r.get("status") != "completed" for r in runs):
            continue
        rec = owner_of(repo, b, (runs or [{}])[0] | {"headSha": sha}) if runs else dispatch_for(b)
        gone = owner_gone(rec)
        orphan = None if gone or rec else orphaned_red(st, o_r, runs)
        if not gone and not orphan:
            continue
        found.append(b)
        what = (f"owner {owner_text(rec)} gone" if gone else f"its red closed as an orphan ({orphan})") \
            + f", {len(runs)} run(s) finished"
        if not b.startswith("measure/"):
            out.append(f"  OWNER GONE branch {b} {sha[:10]}: {what} (listed, kept)")
        elif dry:
            out.append(f"  would delete OWNER GONE branch {b} {sha[:10]}: {what}")
        else:
            try:
                delete_remote_branch(repo, o_r, b, sha)
                log_deletion("remote-branch", o_r, b, sha, f"sweep: {what}")
                out.append(f"  deleted OWNER GONE branch {b} {sha[:10]}: {what}")
            except Exception as e:
                out.append(f"  OWNER GONE branch {b} not deleted: {e}")
    return found


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
        recs = dispatch_records()
        if not recs:
            print(f"dispatch: no records ({dispatches_dir()})")
        for r in recs:
            gone = owner_gone(r)
            state = "unmatched" if r.get("source") == "unmatched" else "owner gone" if gone else "owner alive" if gone is False else "no owner"
            print(f"{r.get('at', '?')} {r.get('branch') or ''} run {r.get('run_id') or '?'} {r.get('workflow') or ''}: "
                  f"{owner_text(r) if r.get('source') != 'unmatched' else '-'} [{state}] {r.get('purpose') or ''}")
        return 0
    inputs = parse_fields(a.field)
    if a.dcmd == "record":
        rec = record_dispatch(branch=a.branch, run_id=a.run, workflow=a.workflow, purpose=a.purpose, inputs=inputs or None)
        print(f"dispatch: recorded {rec.get('branch') or ''} run {rec.get('run_id') or '?'} → owner {owner_text(rec)}")
        return 0
    # run
    t0 = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=60)
    args = ["workflow", "run", a.workflow, "--ref", a.ref]
    for k, v in inputs.items():
        args += ["-f", f"{k}={v}"]
    r = subprocess.run(forge_argv(o_r, *args), capture_output=True, text=True, env=forge_env(o_r), timeout=300)
    sys.stdout.write(r.stdout)
    sys.stderr.write(r.stderr)
    if r.returncode != 0:
        return r.returncode
    rec = record_dispatch(branch=a.ref, run_id=None, workflow=a.workflow,
                          purpose=a.purpose, inputs=inputs,
                          key=None if a.ref != "main" else f"main-{re.sub(r'[^A-Za-z0-9._-]', '_', a.workflow)}-{int(time.time())}")
    rid = None
    for _ in range(int(env_num("AGENTCTL_DISPATCH_FIND", 12))):
        try:
            # listing: the run just dispatched, created since t0; not listed yet is retried (DISPATCH_FIND), never 'none'
            runs = gh_json(o_r, "run", "list", "--workflow", a.workflow, "--branch", a.ref, "--limit", "5", "--json", RUN_FIELDS)
            fresh = [x for x in runs if str(x.get("createdAt", "")) >= t0.strftime("%Y-%m-%dT%H:%M:%SZ")]
            if fresh:
                rid = max(x["databaseId"] for x in fresh)
                break
        except (Exception, SystemExit):
            pass
        time.sleep(env_num("AGENTCTL_DISPATCH_FIND_POLL", 5))
    if rid:
        rec = save_dispatch(dict(rec, run_id=rid), None if a.ref != "main" else dispatch_key(None, rid))
        write_json(dispatches_dir("inputs", f"{rid}.json"), inputs)
    print(f"dispatched {a.workflow} on {a.ref}: run {rid or '(not listed yet)'} → owner {owner_text(rec)}")
    return 0


SUMMARY_MAX_REDS = 3
GATE_SUMMARY_MAX_AGE = 180  # s: a published gate line older than this is never shown as the gate
GATE_LINE = re.compile(r"gate (RED|green)\b")


def publish_gate_summary(o_r, line, epoch=None):
    """The snapshot's gate line: {at, repo, line}. Only a read gate line is ever published (never UNREAD)."""
    if not GATE_LINE.match(line):
        return
    state_update(summary={"at": int(epoch or time.time()), "repo": o_r, "line": line})


def published_summary():
    """(epoch, repo, line) the processor last published, or (None, None, None)."""
    sm = state_read().get("summary") or {}
    try:
        return int(sm["at"]), sm.get("repo"), sm.get("line")
    except (KeyError, TypeError, ValueError):
        return None, None, None


def cached_gate_summary(o_r, now=None):
    """(line, published_epoch). line is None for a missing, malformed, other-repo, future-dated or too-old line."""
    epoch, repo_, line = published_summary()
    if epoch is None:
        return None, None
    age = (now or time.time()) - epoch
    if repo_ != o_r or not GATE_LINE.match(line) or age < -5 or age > GATE_SUMMARY_MAX_AGE:
        return None, epoch
    return line, epoch


_last_gate_publish = [0.0]
_bounded = process_cache({})


def run_bounded(name, fn, secs):
    """Run fn in a daemon thread and wait at most secs: a hung network call can never stall the processor's loop. A step
    still running from an earlier cycle is not started again (False); True when fn finished in time."""
    import threading
    t = _bounded.get(name)
    if t and t.is_alive():
        return False
    t = threading.Thread(target=fn, name=name, daemon=True)
    _bounded[name] = t
    t.start()
    t.join(secs)
    return not t.is_alive()


def gate_dirty_path():
    return state_root("gate.dirty")


def mark_gate_dirty():
    """A push moved main: the processor re-reads the gate at its next tick (<= TICK s)."""
    try:
        os.makedirs(os.path.dirname(gate_dirty_path()), exist_ok=True)
        with open(gate_dirty_path(), "a"):
            pass
        os.utime(gate_dirty_path(), None)
    except OSError:
        pass


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


HEXISH = re.compile(r"\b[0-9a-f]{7,40}\b")
NUMS = re.compile(r"\d+(\.\d+)?")


def norm_line(line):
    """A first error line with what changes run to run taken out: timestamps, shas, numbers, temp paths."""
    s = ANSI.sub("", line or "")
    s = LOG_TS.sub("", s)
    s = re.sub(r"/(private/)?(tmp|var/folders)/\S*", "<tmp>", s)
    s = re.sub(r"\b0x[0-9a-f]+\b", "H", s.lower())
    s = HEXISH.sub("H", s)
    s = NUMS.sub("N", s)
    return " ".join(s.split())[:160]


def signature(workflow, job, line):
    import hashlib
    return hashlib.sha1(f"{workflow}\0{job}\0{norm_line(line)}".encode()).hexdigest()[:12]


def cause_of(job, line):
    """A failure's CAUSE across workflows (the owner, 2026-10-27: one sparse-checkout failure hit findings report #2049,
    live drift #2051 and lean vs whole #2050): its job and normalized first error line, no workflow. None without a
    line (an empty-log job says nothing about its cause)."""
    return signature("*", job, line) if norm_line(line) else None


def name_causes(o_r=None, values=()):
    """{run number or id: {causes}} from every run whose signatures are known (reds/runsig), plus, with o_r, the runs
    `values` name that are not known yet (looked up once, then cached like any run)."""
    out = {}
    for p in glob.glob(reds_dir("runsig", "*.json")):
        rec = read_record(p)
        if not rec:
            continue
        cs = {cause_of(x["job"], x["line"]) for x in rec.get("sigs") or []} - {None}
        for n in (rec.get("run"), rec.get("number")):
            if n is not None:
                out.setdefault(str(n), set()).update(cs)
        out.setdefault(f"wf:{rec.get('number')}", set()).add(rec.get("workflow"))
    missing = [v for v in values if v not in out and v.isdigit()]
    if o_r and missing:
        tried = read_record(reds_dir("unknown-runs.json")) or {}
        for v in missing:
            if v in tried:
                continue
            name, r = resolve_run(o_r, v)
            if r is not None and r.get("status") == "completed" and r.get("conclusion") in RED_CONCLUSIONS:
                try:
                    sigs = run_sigs(o_r, r, name)
                    cs = {cause_of(x["job"], x["line"]) for x in sigs} - {None}
                    for n in run_names(r):
                        out.setdefault(n, set()).update(cs)
                    continue
                except (Exception, SystemExit):
                    pass
            tried[v] = ts()
        try:
            write_json(reds_dir("unknown-runs.json"), tried)
        except OSError:
            pass
    return out


def run_names(r):
    return {str(x) for x in (r.get("number"), r.get("databaseId")) if x not in (None, "")}


def classifier_version():
    """What a cached signature was judged by: its KIND (classify_job and every pattern it reads) and its LINE, the red's
    key (relevant_lines, job_log_lines, choose_job_lines and the STRONG / WEAK / NOISE / ANNOTATION patterns). A record
    of another version is read again (#2426: cached a TEST FAILURE before KEY_REFUSED_LOG, `red block` kept reading it;
    #2406 / #2481 / #2493: one failure cached under three lines, the older two from before annotations came first)."""
    import hashlib, inspect
    parts = [VERDICT_COMPLETE_JOB.pattern, inspect.getsource(jobs_no_verdict), inspect.getsource(job_is_verdict_report),
             "verdict complete beside a real failure: incomplete", inspect.getsource(classify_job),
             inspect.getsource(job_is_phantom), SETUP_STEP.pattern, INFRA_LOG.pattern, UNKNOWN_LOG.pattern,
             KEY_REFUSED_LOG.pattern, STEP_TIMED_OUT, inspect.getsource(relevant_lines), inspect.getsource(job_log_lines),
             inspect.getsource(choose_job_lines), inspect.getsource(flip_listings),
             inspect.getsource(flip_key_line), inspect.getsource(drift_key), DRIFT_LINE.pattern, FLIP_HEADER.pattern,
             FLIP_ROW.pattern, FLIP_TAKES.pattern, str(FLIP_KEY_MAX), STRONG.pattern, WEAK.pattern, NOISE.pattern, ANNOTATION.pattern,
             NOT_A_FAILURE.pattern, CREDENTIAL_STEP.pattern, RUNNER_FAILED.pattern, inspect.getsource(failed_jobs_log),
             inspect.getsource(step_window_lines), inspect.getsource(run_failed_jobs), inspect.getsource(run_failed_attempt),
             log_verdict_version()]
    return hashlib.sha1("\n".join(parts).encode()).hexdigest()[:12]


def save_run_sigs(o_r, run, workflow, per_job):
    """per_job: [(job, kind, first relevant line)] of a completed red run -> its signatures, cached on disk."""
    sigs = []
    for j, k, l in per_job:
        sigs.append({"job": j, "kind": k, "line": (l or "")[:300], "sig": signature(workflow, j, l)})
    rec = {"run": run.get("databaseId"), "number": run.get("number"), "workflow": workflow, "sigs": sigs,
           "headSha": run.get("headSha"), "classifier": classifier_version()}
    try:
        write_json(reds_dir("runsig", f"{run.get('databaseId')}.json"), rec)
    except OSError:
        pass
    return sigs


# A RUN WHOSE LOG WENT UNREAD IS READ AGAIN WITH BACKOFF, never every tick: its answer is kept in runsig-unread/ (never
# runsig/, whose record means a final read to every reader) and re-read after 5 min, then 10, 20, ... at most 2 h.
# 2026-11-11: run 43014208013's failed log ("log not found: 130501277846", a job GitHub has no log for) was re-read 211
# times in 40 min -- gh run view, --log-failed and the job's --log every tick -- until kinowo-lander's 5000/h was spent
# and the gate itself went unread for 20 min.
RUNSIG_UNREAD_FIRST = 300
RUNSIG_UNREAD_MAX = 7200


def run_sigs(o_r, run, workflow, now_=None):
    """The signatures of a completed red run: [{job, kind, line, sig}] (cached; explain_run fills the cache). A run whose
    log went unread answers what it last read until its backoff is due (RUNSIG_UNREAD_FIRST)."""
    t = time.time() if now_ is None else now_
    rid = run.get("databaseId")
    rec = read_record(reds_dir("runsig", f"{rid}.json"))
    if rec is not None and rec.get("classifier") == classifier_version():
        return rec["sigs"]
    unread_path = reds_dir("runsig-unread", f"{rid}.json")
    unread = read_record(unread_path)
    if unread and unread.get("classifier") == classifier_version() and t < float(unread.get("due") or 0):
        return unread["sigs"]
    lines, _num, per_job = explain_run_jobs(o_r, run, 6)
    if any("LOG UNREADABLE" in l for l in lines):  # a log not read is no verdict: read again when due, never cached
        sigs = [{"job": j, "kind": k, "line": (l or "")[:300], "sig": signature(workflow, j, l)} for j, k, l in per_job]
        tries = int((unread or {}).get("tries") or 0) + 1
        try:
            write_json(unread_path, {"run": rid, "classifier": classifier_version(), "sigs": sigs, "tries": tries,
                                     "due": t + min(RUNSIG_UNREAD_MAX, RUNSIG_UNREAD_FIRST * 2 ** (tries - 1))})
        except OSError:
            pass
        return sigs
    if unread:
        try:
            os.remove(unread_path)
        except OSError:
            pass
    return save_run_sigs(o_r, run, workflow, per_job)


def in_fixer():
    """True inside a dispatched fixer (its session's own record), or a process agentctl spawned with a fixer's
    environment and no Claude session of its own."""
    if own_fixer_record_path():
        return True
    return not os.environ.get("CLAUDE_CODE_SESSION_ID") and bool(os.environ.get("AGENTCTL_RED_FIXER"))


def claimant(cwd=None):
    """The agent name (agent_name: a dispatched fixer's, by its session's record), else the Claude session, else the
    worktree."""
    name = agent_name()
    if name:
        return name
    if os.environ.get("CLAUDE_CODE_SESSION_ID"):
        return f"session {os.environ['CLAUDE_CODE_SESSION_ID'][:8]}"
    return os.path.basename(worktree_of(cwd or safe_cwd()) or safe_cwd())


def worktree_of(path):
    if not path or not os.path.isdir(path):
        return None
    r = git(path, "rev-parse", "--show-toplevel", check=False)
    return r.stdout.strip() if r.returncode == 0 else None


def land_cwds():
    """{worktree: pid} of every running `agentctl land`."""
    out = {}
    for pid, _pp, _e, cmd in ps():
        if re.search(r"agentctl land\b", cmd) and pid != os.getpid():
            cwd = lsof_cwds([pid]).get(pid)
            wt = worktree_of(cwd) if cwd else None
            if wt:
                out[os.path.realpath(wt)] = pid
    return out


def contains_local(repo, sha, head):
    """True / False when both commits are here, else None."""
    if not head:
        return None
    if head == sha:
        return True
    if not git_ok(repo, "cat-file", "-e", f"{head}^{{commit}}"):
        return None
    return git_ok(repo, "merge-base", "--is-ancestor", sha, head)


_blocker_cache = process_cache({})


def cached_dispatch_blocker(ttl=60):
    """dispatch_blocker runs `claude --help`: once a minute per process is enough for a display."""
    key = (reds_dir(), os.path.exists(reds_dir("DISPATCH_OFF")), os.environ.get("AGENTCTL_CLAUDE"),
           os.environ.get("AGENTCTL_CLAUDE_SETTINGS"))
    hit = _blocker_cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    v = dispatch_blocker()
    _blocker_cache[key] = (time.time(), v)
    return v


def short_flow(name):
    return "main" if name == "Main" else "convergence" if "convergence" in name.lower() else name.lower()


def dispatch_blocker():
    """None when a headless fixer may be dispatched, else what stops it (and what the owner must run)."""
    if os.path.exists(reds_dir("DISPATCH_OFF")):
        return "dispatch is OFF (`~/bin/agentctl red auto on` turns it on)"
    cli = claude_cli()
    if not cli:
        return "no `claude` CLI found ($AGENTCTL_CLAUDE, PATH, ~/.local/bin/claude)"
    try:
        r = subprocess.run([cli, "--help"], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as e:
        return f"`{cli} --help` failed ({e})"
    if r.returncode != 0 or "--permission-mode" not in r.stdout or "--bg" not in r.stdout:
        return f"`{cli} --help` did not offer --bg and --permission-mode (exit {r.returncode})"
    mode = interactive_mode()
    if mode != "auto":
        return (f"interactive sessions run in permission mode {mode!r}, and a headless fixer gets no wider: it needs "
                "\"auto\". The owner enables it by setting permissions.defaultMode to \"auto\" in ~/.claude/settings.json")
    return None


def claude_cli():
    if os.environ.get("AGENTCTL_CLAUDE"):  # set: that one only (the tests' fake)
        return os.environ["AGENTCTL_CLAUDE"] if os.access(os.environ["AGENTCTL_CLAUDE"], os.X_OK) else None
    for c in (shutil_which("claude"), os.path.join(HOME, ".local", "bin", "claude")):
        if c and os.access(c, os.X_OK):
            return c
    return None


def shutil_which(name):
    import shutil
    return shutil.which(name)


def interactive_mode():
    """The permission mode interactive sessions start in (~/.claude/settings.json's permissions.defaultMode)."""
    p = os.environ.get("AGENTCTL_CLAUDE_SETTINGS") or os.path.join(HOME, ".claude", "settings.json")
    rec = read_record(p) or {}
    return (rec.get("permissions") or {}).get("defaultMode") or "default"


AUTHOR_ACTIVE_MIN = 15   # live = a pid alive and activity within this ($AGENTCTL_AUTHOR_ACTIVE_MIN)


def red_commits(repo, u, limit=30):
    """The commits of the red run since the workflow's last green run on main (newest first); just its head when the
    range cannot be read here."""
    red = u["run"].get("headSha")
    if not repo or not red or not git_ok(repo, "cat-file", "-e", f"{red}^{{commit}}"):
        return [red] if red else []
    rid = u["run"].get("databaseId") or 0
    green = next((r for r in u.get("runs") or [] if r.get("status") == "completed" and r.get("conclusion") == "success"
                  and (r.get("databaseId") or 0) < rid and r.get("headSha")), None)
    if green and git_ok(repo, "cat-file", "-e", f"{green['headSha']}^{{commit}}"):
        r = git(repo, "rev-list", f"--max-count={limit}", red, f"^{green['headSha']}", check=False)
    else:
        r = git(repo, "rev-list", "--max-count=1", red, check=False)
    return [s for s in r.stdout.split() if s] if r.returncode == 0 else [red]


def landed_heads(repo):
    """{pushed head sha: {worktree, session}}: the changes that landed, as their queue entries recorded them at
    submission (audit #15: the author comes from the change item, never from an inherited environment)."""
    out = {}
    try:
        d, _c, _w = waiting_state(repo)
        for e in waiting_entries(d, open_only=False):
            if e.get("landed_sha"):
                out[e["landed_sha"]] = {"worktree": e.get("worktree"), "session": e.get("session")}
    except (Exception, SystemExit):
        pass
    return out


def agent_pids_in(wts):
    """{worktree: [pid]} of the agent processes (claude, agentctl land/watch, the hook) whose cwd is in one of wts."""
    want = [os.path.realpath(w) for w in wts if w]
    if not want:
        return {}
    pids = [pid for pid, _pp, _e, cmd in ps() if pid != os.getpid()
            and (AGENT_WORK.search(cmd or "") or re.search(r"agentctl (watch|wait)\b", cmd or ""))]
    out = {}
    for pid, cwd in lsof_cwds(pids).items() if pids else ():
        c = os.path.realpath(cwd)
        for w in want:
            if c == w or c.startswith(w + "/"):
                out.setdefault(w, []).append(pid)
    return out


def worktree_activity(repo, wt, entries=()):
    """The worktree's last sign of work: git (index, HEAD log, last commit) or an inbox entry from it."""
    beats = [gitdir_activity(repo, wt) or 0.0]
    r = git(wt, "log", "-1", "--format=%ct", check=False)
    if r.returncode == 0 and r.stdout.strip().isdigit():
        beats.append(float(r.stdout.strip()))
    beats += [float(e.get("at") or 0) for e in entries if os.path.realpath(e.get("cwd") or "/") == os.path.realpath(wt)]
    return max(beats)


def red_authors(repo, u, now_=None):
    """[{worktree, session, shas, pids, live, activity}] for the commits of the red run, the newest commit's author
    first. Live: an agent process works in that worktree now and it was active lately."""
    t = now_ if now_ is not None else time.time()
    commits = red_commits(repo, u)
    if not repo or not commits:
        return []
    heads = landed_heads(repo)
    owner, pending = {}, []
    for c in reversed(commits):  # oldest first: a change's commits end at its landed head
        pending.append(c)
        h = heads.get(c) or next((v for k, v in heads.items() if c.startswith(k) or k.startswith(c)), None)
        if h:
            for p in pending:
                owner[p] = h
            pending = []
    for c in pending:
        wt = worktree_holding(repo, c)
        if wt:
            owner[c] = {"worktree": wt}
    by_wt = {}
    for c in commits:  # newest first
        h = owner.get(c)
        if not h or not h.get("worktree") or not allowed_path(h["worktree"]) or not os.path.isdir(h["worktree"]):
            continue
        a = by_wt.setdefault(os.path.realpath(h["worktree"]), {"worktree": h["worktree"], "shas": [], "pids": set(),
                                                               "session": h.get("session")})
        a["shas"].append(c)
    try:
        entries = inbox_entries()
    except OSError:
        entries = []
    running = agent_pids_in(list(by_wt))
    active = env_num("AGENTCTL_AUTHOR_ACTIVE_MIN", AUTHOR_ACTIVE_MIN) * 60
    out = []
    for k, a in by_wt.items():
        a["pids"] = sorted(set(a["pids"]) | set(running.get(k, [])))
        a["activity"] = worktree_activity(repo, a["worktree"], entries)
        alive = [p for p in a["pids"] if proc_alive(p)]
        a["live_pid"] = alive[0] if alive else None
        a["live"] = bool(alive) and t - a["activity"] <= active
        out.append(a)
    return out
