# agentctl, part `queuebatch`: overlapping changes checked as one, the batch tiers, generated overlaps, held waits, one item per change.
# Runs inside agentctl's one namespace, after the parts before it in lib/agentctl/__init__.py's PARTS (it uses their names).

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
# fix never does. queuebatch.py overlap_hold, pair_conflict; tests: tests/test_queue_sim.py CollisionTest (the three
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
