# agentctl, part `waiting`: waiting.
# Runs inside agentctl's one namespace, after the parts before it in lib/agentctl/__init__.py's PARTS (it uses their names).

# ---------------------------------------------------------------- waiting: production changes held while main is red
# The owner (2026-10-26): "when waiting for main to go green, merge all waiting changes into one waiting branch (always
# use the same name) and merge it when main is green". While the gate is red, `land` puts a production change onto the
# local branch `waiting` (worktree <main clone>-wt-waiting) instead of being refused: rebased onto it, the local checks
# run on the combined tip, `WAITING <sha> (n changes queued on waiting)`. One processor (`waiting run`, detached, locked)
# watches the gate; once main is green it rebuilds `waiting` on forgejo/main, runs the hook's dry run, pushes the whole
# branch in ONE push, deletes it, and every waiting `land` goes on to its LANDED and WATCH. Tools-only commits and
# Fixes-Red fixes never wait (the hook lets them land while red). Replaces the merge queue.
WAITING_BRANCH = "waiting"
WAITING_DIR = None  # a test's override; else waiting_dir(): under the state root, resolved at each use


def waiting_dir():
    return WAITING_DIR or state_root("waiting")
PRODUCTION_GLOBS = ("*/src/main/*", "conf/*", "*/conf/*", "build.sbt", "project/*", "infra/nix/*", "infra/kubernetes/*",
                    "Dockerfile")
WAITING_AWAIT_TIMEOUT = 24 * 3600
WAITING_HELP = """The processor (one loop of ticks, launchd net.bitcashier.agentctl-waiting) and its queue: every landed change is
an item -- queued, checking, checked, pushing, landed; or waiting while main is red. At green the waiting changes are ONE
batch item: its tip assembled in a check tree (main plus the changes, Fixes-Red first), checked once (pre-push
--fast-only, a fresh sbt server; a stall retried once, then HELD), pushed in one push; a failing batch is bisected and
only the confirmed culprit rejected. No `waiting` branch. Every verdict goes to `agentctl inbox`.

  agentctl land status                          # the changes, the batch, the gate, the processor (hands it the token)
  agentctl land cancel <id>                     # drop a change
  agentctl land hold|release                    # stop / restart taking batches (changes still queue)
  agentctl tick [--once]                        # the processor
  agentctl tick service install|uninstall|status   # it as the launchd agent net.bitcashier.agentctl-waiting
The old names (`waiting status|pause|resume|cancel|run|service`) answer until 2026-11-05, each naming its new one."""


def is_production_path(p):
    """movies' pre-push is_production_path: what reaches a pod or a fleet host (the rest lands while red)."""
    return any(fnmatch.fnmatch(p, g) for g in PRODUCTION_GLOBS)


GATE_UNREAD = "unread"  # gate_reds' member when the gate could not be read: never green, and no Fixes-Red matches it
gate_unread_why = [""]


def gate_reds(repo):
    """The ids (run numbers and run ids) of the gating workflows' red runs on main; empty when green or not gated.
    When the gate cannot be read: {GATE_UNREAD}, which every caller takes as RED -- a production change waits, the
    processor takes no batch (the owner, 2026-10-27: an unread gate is never green; at 08:05Z a launchd processor with
    no Forgejo token read it as green, and the hook, with no token either, skipped its own gate: a push onto red #2047)."""
    try:
        o_r = slug(repo)
        if o_r not in GATING:
            return set()
        reds = set()
        for _name, decided, _newer, _streak in gate_runs(o_r):
            if decided and decided.get("conclusion") != "success":
                reds |= {str(decided.get("number")), str(decided.get("databaseId"))}
        gate_unread_why[0] = ""
        return reds
    except (Exception, SystemExit) as e:  # SystemExit: NoToken
        gate_unread_why[0] = str(e).replace("agentctl: ", "")[:200]
        err(f"could not read the gate ({gate_unread_why[0]}): taken as RED until it is read")
        return {GATE_UNREAD}


def waiting_supported(repo):
    """A repo hook, if any, must have --checks-only (its checks without the red gate) to check a waiting tip."""
    hook = precheck_hook(repo)
    if not hook:
        return True
    with open(hook) as f:
        return "--checks-only" in f.read()


def waiting_commits(repo, base, head):
    return git(repo, "rev-list", "--reverse", f"{base}..{head}").split()


def must_wait(repo, commits, reds):
    """A production commit with no Fixes-Red: naming a red run waits; tools-only and declared fixes go (the hook's rule)."""
    for c in commits:
        paths = git(repo, "diff-tree", "--no-commit-id", "--name-only", "-r", "--root", "-m", c).split("\n")
        if not any(p and is_production_path(p) for p in paths):
            continue
        if not (commit_fixes(repo, c) & reds):  # a trailer that does not count (fix_check) is no fix: it waits
            return True
    return False


def waiting_state(repo):
    common = os.path.realpath(git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir").strip())
    d = os.path.join(waiting_dir(), re.sub(r"[^A-Za-z0-9._-]", "_", common.strip("/")))
    os.makedirs(os.path.join(d, "entries"), exist_ok=True)
    clone = os.path.dirname(common) if os.path.basename(common) == ".git" else common
    return d, clone, f"{clone}-wt-waiting"


def waiting_entries(d, open_only=True):
    out = []
    for p in sorted(glob.glob(os.path.join(d, "entries", "*.json"))):
        try:
            with open(p) as f:
                e = json.load(f)
        except (OSError, ValueError):
            continue
        if not open_only or e.get("status") == "waiting":
            out.append(e)
    return sorted(out, key=lambda e: e["seq"])


ENTRY_FINAL = ("landed", "rejected", "superseded", "cancelled", "joined", "migrated")  # migrated: the laptop's, to ci-runner


def save_entry(d, e):
    """The entry, written atomically. A final status on disk is never overwritten by a racing writer's non-final one
    (audit S2: `supersede` raced the checker's save): the disk's status and verdict stand."""
    p = os.path.join(d, "entries", f"{e['id']}.json")
    disk = read_record(p) or {}
    if disk.get("status") in ENTRY_FINAL and e.get("status") not in ENTRY_FINAL:
        e.update({k: disk[k] for k in ("status", "why", "decided", "superseded_by", "landed_sha") if k in disk})
    with open(p + ".tmp", "w") as f:
        json.dump(e, f, indent=1)
    os.replace(p + ".tmp", p)


def wlog(d, msg):
    with open(os.path.join(d, "processor.log"), "a") as f:
        f.write(f"{ts()} {msg}\n")


# NEVER AN EMPTY COMMIT (2026-10-27). An agent queued a commit, queued it again, and both entries ran: the later one's
# commit had already landed through the earlier one, its rebase (a cherry-pick with --keep-redundant-commits) left an
# EMPTY commit, and that was pushed -- four times on movies main (317fba9b3, 1fddd8951, 1d554f051, 693e1be78/82f4d9f55).
# Now every rebuild of an entry drops a commit that is patch-equivalent to its new base, or empty after the pick; an
# entry left with nothing is LANDED (already on main via <commit>); re-queueing supersedes the older entry; and the
# squash guard refuses any push carrying an empty commit unless it is a deliberate one with an `Allow-Empty:` trailer.
ALLOW_EMPTY_TRAILER = "Allow-Empty"


def deliberate_empty(repo, c):
    """A commit made empty on purpose: `git commit --allow-empty` with an `Allow-Empty: <why>` trailer."""
    return bool(git(repo, "log", "-1", f"--format=%(trailers:key={ALLOW_EMPTY_TRAILER},valueonly)", c,
                    check=False).stdout.strip())


def is_empty_commit(repo, c):
    """A non-merge commit whose tree is its parent's (a root commit is never empty here)."""
    parents = git(repo, "rev-list", "--parents", "-n", "1", c).split()[1:]
    if len(parents) != 1:
        return False
    return git(repo, "rev-parse", f"{c}^{{tree}}").strip() == git(repo, "rev-parse", f"{parents[0]}^{{tree}}").strip()


def patch_id(repo, c):
    diff = git(repo, "show", "--format=", "--no-color", "--no-ext-diff", c, check=False).stdout
    if not diff.strip():
        return None
    r = run_cmd(["git", "patch-id", "--stable"], cwd=repo, input=diff)
    return (r.stdout.split() or [None])[0]


def equivalent_on(repo, c, upstream, limit=400):
    """The commit of `upstream` (since its merge-base with c) that carries c's patch, or None (`git cherry`'s test,
    naming the commit), else its content twin (content_twin: same subject, author, date and changed lines)."""
    pid = patch_id(repo, c)
    if not pid:
        return None
    mb = git(repo, "merge-base", c, upstream, check=False).stdout.strip()
    rng = f"{mb}..{upstream}" if mb else upstream
    for u in git(repo, "rev-list", "--no-merges", "-n", str(limit), rng, check=False).stdout.split():
        if patch_id(repo, u) == pid:
            return u
    return content_twin(repo, c, upstream)


def pick(wt, commits, dropped=None):
    """Cherry-pick commits onto wt's HEAD, one at a time: None, or the conflicting files (the pick aborted, HEAD back
    where it was). A commit already in HEAD's history (patch-equivalent), or empty once picked, is DROPPED, never kept
    as an empty commit: appended to `dropped` as (commit, the commit of HEAD carrying it, or None). A deliberate empty
    commit (an Allow-Empty: trailer) is kept."""
    start = git(wt, "rev-parse", "HEAD").strip()
    for c in commits:
        before = git(wt, "rev-parse", "HEAD").strip()
        r = git(wt, "cherry-pick", "--allow-empty", "--keep-redundant-commits", c, check=False)
        if r.returncode != 0:
            files = git(wt, "diff", "--name-only", "--diff-filter=U", check=False).stdout.split()
            git(wt, "cherry-pick", "--abort", check=False)
            via = equivalent_on(wt, c, before) if files else None
            if via:  # its patch is already there, and a later change to the same lines conflicts with it again
                git(wt, "reset", "--hard", "-q", before)
                if dropped is not None:
                    dropped.append((c, via))
                continue
            git(wt, "reset", "--hard", "-q", start)
            return files or [r.stderr.strip()[:200]]
        if is_empty_commit(wt, "HEAD") and not deliberate_empty(wt, c):
            git(wt, "reset", "--hard", "-q", before)
            if dropped is not None:
                dropped.append((c, equivalent_on(wt, c, before)))
    return None


def apply_entry(d, wt, e, base):
    """Pick the entry's commits onto wt's HEAD (base, or base plus earlier entries): ('ok', None), ('conflict', files),
    ('landed', via) or ('duplicate', via). A commit already on MAIN (base's history) is dropped from the entry for good
    (a partly landed entry keeps only the rest); one only an earlier entry carries is left out of this build alone (that
    entry may still be rejected). Nothing left of it on main: 'landed' -- the caller says LANDED, never pushes empty."""
    first = git(wt, "rev-parse", "HEAD").strip() == base
    dropped = []
    conflict = pick(wt, e["commits"], dropped)
    if conflict:
        return "conflict", conflict
    if not dropped:
        return "ok", None
    on_main = [(c, v) for c, v in dropped
               if (v and git(wt, "merge-base", "--is-ancestor", v, base, check=False).returncode == 0) or (not v and first)]
    if on_main:
        gone = {c for c, _v in on_main}
        e["commits"] = [c for c in e["commits"] if c not in gone]
        e.setdefault("dropped", []).extend([[c, v] for c, v in on_main])
        save_entry(d, e)
        wlog(d, f"{e['id']}: {len(on_main)} commit(s) already on {remote()}/main, dropped: "
                + ", ".join(f"{c[:10]} (via {v[:10] if v else 'an empty pick'})" for c, v in on_main))
        if not e["commits"]:
            return "landed", next((v for _c, v in reversed(on_main) if v), None)
    if not [c for c in e["commits"] if c not in {x for x, _v in dropped}]:  # all of it is on an earlier entry
        return "duplicate", next((v for _c, v in reversed(dropped) if v), None)
    return "ok", None


def already_landed(d, e, via):
    """Every commit of the entry is on main already (an earlier entry, or someone, landed it): LANDED, nothing pushed."""
    queue_verdict(d, e, "landed", f"LANDED {e['head']} (already on main via {via or 'an equivalent commit'}): "
                                  "nothing left to push", code=0, landed_sha=via, already=True)


def supersede(d, e, by, why):
    """The entry gives way to `by` (a newer entry of the same worktree and branch, or one carrying the same change): its
    checker, if running, is stopped; said in the inbox."""
    line = f"SUPERSEDED {e['id']} by {by}: {why}"
    set_change(d, e, "superseded", event=change_event(e, line, code=0), superseded_by=by, why=why, decided=ts())
    cancel_entry_remote(e, f"its queue entry was superseded by {by}")
    h = read_record(entry_owned(e["id"]).record) or {}
    if lock_held(entry_owned(e["id"]).lockpath) and h.get("pid") and int(h["pid"]) != os.getpid():
        try:
            os.kill(int(h["pid"]), signal.SIGTERM)  # its checker: its CI run is cancelled with it
        except OSError:
            pass
    wlog(d, line)


def tail_of(out, n=25):
    return "\n  ".join([l for l in out.splitlines() if l.strip()][-n:])


def batching(repo):
    """Whether land batches here (every change joins `waiting`): a repo hook with --checks-only (movies)."""
    hook = precheck_hook(repo)
    return bool(hook) and waiting_supported(repo)


def fixes_of(repo, commits):
    """The run ids/numbers the commits' Fixes-Red: trailers name -- those that count as fixes (fix_check)."""
    out = set()
    for c in commits:
        out |= commit_fixes(repo, c)  # only those that count: touching what the red's job reads (fix_check)
    return out


def batch_order(repo, entries, reds):
    """The order a batch is built in: a fix for the current red first, then (main red) what may land while red -- tools
    only, or every commit carrying a Fixes-Red for a red run -- then the rest; by join order within each."""
    def rank(e):
        if reds and set(e.get("fixes") or []) & reds:
            return 0
        if reds and not must_wait(repo, e["commits"], reds):
            return 1
        return 2
    return sorted(entries, key=lambda e: (rank(e), e["seq"]))


def join(repo, main_ref, sh, log_path, a, head=None, worktree=None, branch=None):
    """The change (its own checks passed) waits for main to turn green: a `waiting` entry (design §14.1: no branch, no
    worktree -- the batch is computed at green, in a check tree). Prints `WAITING <head> (entry <id>, n waiting)`."""
    if head is None:
        rc, head = sync(repo, main_ref, sh)
        if rc is not None:
            return rc
    d, _clone, _wt = waiting_state(repo)
    commits = waiting_commits(repo, f"{remote()}/main", head)
    branch = branch_of(repo) if branch is None else branch
    with open(os.path.join(d, "seq.lock"), "a+") as seq_lock:  # a seq of its own, beside a land queueing meanwhile
        fcntl.flock(seq_lock, fcntl.LOCK_EX)
        seq = max([e["seq"] for e in waiting_entries(d, open_only=False)] + [0]) + 1
        eid = f"{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}-{os.path.basename(worktree or repo)}-{seq}"
        for old in waiting_entries(d):  # waiting again replaces this worktree and branch's older waiting entry
            if same_change_source(old, worktree or repo, branch):
                supersede(d, old, eid, f"{worktree or repo} waits again; only the newest entry lands")
        e = {"id": eid, "seq": seq, "worktree": worktree or repo, "branch": branch, "head": head, "commits": commits,
             "fixes": sorted(fixes_of(repo, commits)), "status": "waiting", "created": ts(), "tip": head,
             "follow": bool(getattr(a, "follow", False))}
        save_entry(d, e)
    n = len(waiting_entries(d))
    with open(log_path, "a") as f:
        f.write(f"=== waiting {ts()} {head} as {e['id']} ({n} waiting)\n")
    print(f"WAITING {head} (entry {e['id']}, {n} waiting)", flush=True)
    a.waiting = e
    spawn_waiting_processor(repo)
    offer_token(repo)
    return 0
