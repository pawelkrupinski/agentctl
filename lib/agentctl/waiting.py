# agentctl, part `waiting`: waiting.
# Runs inside agentctl's one namespace, after the parts before it in lib/agentctl/__init__.py's PARTS (it uses their names).


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
    """The ids (run numbers and run ids) of the gating workflows' red runs on main; empty when green or not gated. A
    report-only red (gate_report_only: red only in the registry's unproven jobs) holds no push.
    When the gate cannot be read: {GATE_UNREAD}, which every caller takes as RED -- a production change waits, the
    processor takes no batch (the owner, 2026-10-27: an unread gate is never green; at 08:05Z a launchd processor with
    no token read it as green, and the hook, with no token either, skipped its own gate: a push onto red #2047)."""
    try:
        o_r = slug(repo)
        if not gated_repo(o_r):
            return set()
        reds = set()
        for name, decided, _newer, _streak in gate_runs(o_r):
            if decided and decided.get("conclusion") != "success" and not gate_report_only(o_r, name, decided)[0]:
                reds |= {str(decided.get("number")), str(decided.get("databaseId"))} | episode_names(o_r, name, decided)
        gate_unread_why[0] = ""
        return reds
    except (Exception, SystemExit) as e:  # SystemExit: NoToken
        gate_unread_why[0] = str(e).replace("agentctl: ", "")[:200]
        err(f"could not read the gate ({gate_unread_why[0]}): taken as RED until it is read")
        return {GATE_UNREAD}


# A FIX MATCHES THE RED IT FIXES, NOT ONE RUN NUMBER (2026-11-05). Every push that lands while main is red (tools-only
# commits land freely) starts a new run failing the same way, so a fix naming the run it was written against was
# stranded WAITING once the next run went red: a red owner's fixes named Main #2164, then #2172, while the gate read
# #2173, then #2174; main stayed red ~5.5 h until the fix was re-trailered by hand, twice. Now a `Fixes-Red:` counts
# when it names ANY run of the CURRENT red EPISODE: the workflow's failed runs on main since its last green (the red
# streak) that share a failure signature with its newest red run -- the names red_units links a red item by. A run of
# an older episode (main went green since) never counts. movies' pre-push judges the same rule from gh's run list and
# failed job names (episode_reds there).
def episode_names(o_r, name, decided):
    """The names (numbers and ids) of workflow `name`'s red streak runs on main that fail the way its newest decided red
    run does (red_units' names); empty when its signatures cannot be read -- then only the newest run counts."""
    try:
        sigs = run_sigs(o_r, decided, name)
        units = red_units(o_r, name, decided, _gate_lists.get(name) or [], sigs, True)
    except (Exception, SystemExit) as e:  # SystemExit: NoToken
        err(f"could not read {name} #{decided.get('number')}'s failures ({str(e)[:120]}): only that run counts as its red")
        return set()
    return set().union(*(set(u.get("names") or ()) for u in units))


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


def apply_entry(d, wt, e, base, regen=True, autoresolve=False):
    """Pick the entry's commits onto wt's HEAD (base, or base plus earlier entries): ('ok', None), ('conflict', files),
    ('landed', via) or ('duplicate', via). A commit already on MAIN (base's history) is dropped from the entry for good
    (a partly landed entry keeps only the rest); one only an earlier entry carries is left out of this build alone (that
    entry may still be rejected). Nothing left of it on main: 'landed' -- the caller says LANDED, never pushes empty."""
    first = git(wt, "rev-parse", "HEAD").strip() == base
    dropped, regenerated, provisional, resolved = [], [], [], [] if autoresolve else None
    conflict = pick(wt, e["commits"], dropped, regen, regenerated, provisional, resolved)
    if resolved and not conflict:  # A SOURCE CONFLICT IS RESOLVED IN THE BATCH: what it took, for the author and bisect
        e["autoresolved"] = resolved
        wlog(d, f"{e['id']}: auto-resolved in the batch, its full check the verification: "
                + "; ".join(f"{m['file']} ({', '.join(m['hunks'])})" for m in resolved))
    else:
        e.pop("autoresolved", None)
    if regenerated:  # resolved by regeneration in this build (GENERATED FILES ARE NO OVERLAP): a failure is not its own
        e["regenerated"] = sorted(regenerated)
    else:
        e.pop("regenerated", None)
    if provisional and not conflict:
        e["provisional"] = sorted(set(provisional))
        wlog(d, f"{e['id']}: {', '.join(e['provisional'])}: a row both sides changed and this host runs no regen "
                "command: kept the tree's row (main's), for the checks' ratchet to judge over the combined tree")
    else:
        e.pop("provisional", None)
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


def diagnoses_of(repo, commits):
    """The run ids/numbers the commits' `Diagnoses-Red:` trailers name (DIAGNOSTICS ARE NO FIX, redmove.py)."""
    out = set()
    for c in commits:
        r = git(repo, "log", "-1", "--format=%(trailers:key=Diagnoses-Red,valueonly,separator=%x2C)", c, check=False)
        out |= trailer_values(r.stdout) if r.returncode == 0 else set()
    return out


def batch_order(repo, entries, reds):
    """The order a batch is built in: a fix for the current red first, then (main red) what may land while red -- tools
    only, or every commit carrying a Fixes-Red for a red run -- then the rest; by join order within each."""
    facts = gate_facts(reds)

    def rank(e):
        if reds and classify_change(e, facts)["gate_fix"]:  # its KIND: a fix of the gate's red (changekind.py)
            return 0
        if reds and not must_wait(repo, e["commits"], reds):
            return 1
        return 2
    return sorted(entries, key=lambda e: (rank(e), e["seq"]))


def supersede_older_waiting(d, worktree, branch, eid):
    """Waiting again replaces this worktree and branch's older waiting entry: only the newest (eid) lands."""
    for old in waiting_entries(d):
        if old["id"] != eid and same_change_source(old, worktree, branch):
            supersede(d, old, eid, f"{worktree} waits again; only the newest entry lands")


def note_waiting(d, log_path, head, eid):
    """The change's `=== waiting` line in its log, and its verdict line `WAITING <head> (entry <id>, n waiting)` (n:
    the waiting entries, it among them, saved yet or not)."""
    n = len({x["id"] for x in waiting_entries(d)} | {eid})
    with open(log_path, "a") as f:
        f.write(f"=== waiting {ts()} {head} as {eid} ({n} waiting)\n")
    return f"WAITING {head} (entry {eid}, {n} waiting)"


def join(repo, main_ref, sh, log_path, a, head=None, worktree=None, branch=None):
    """The change (its own checks passed) waits for main to turn green: a `waiting` entry (design §14.1: no branch, no
    worktree -- the batch is computed at green, in a check tree). Prints `WAITING <head> (entry <id>, n waiting)`."""
    if head is None:
        rc, head = sync(repo, main_ref, sh, getattr(a, "release_hold", False))
        if rc is not None:
            return rc
    d, _clone, _wt = waiting_state(repo)
    commits = waiting_commits(repo, f"{remote()}/main", head)
    branch = branch_of(repo) if branch is None else branch
    with open(os.path.join(d, "seq.lock"), "a+") as seq_lock:  # a seq of its own, beside a land queueing meanwhile
        fcntl.flock(seq_lock, fcntl.LOCK_EX)
        seq = max([e["seq"] for e in waiting_entries(d, open_only=False)] + [0]) + 1
        eid = f"{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}-{os.path.basename(worktree or repo)}-{seq}"
        supersede_older_waiting(d, worktree or repo, branch, eid)
        e = {"id": eid, "seq": seq, "worktree": worktree or repo, "branch": branch, "head": head, "commits": commits,
             "fixes": sorted(fixes_of(repo, commits)), "status": "waiting", "created": ts(), "tip": head,
             "follow": bool(getattr(a, "follow", False))}
        save_entry(d, e)
    print(note_waiting(d, log_path, head, e["id"]), flush=True)
    a.waiting = e
    spawn_waiting_processor(repo)
    offer_token(repo)
    return 0
