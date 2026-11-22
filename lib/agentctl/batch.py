# agentctl, part `batch`: the batch machine and the tick loop.
# Runs inside agentctl's one namespace, after the parts before it in lib/agentctl/__init__.py's PARTS (it uses their names).

# ---------------------------------------------------------------- the batch machine (design §5.2 + §14.1; step 8)
# The batch is an ITEM, not a branch: while main is red, checked changes wait (entries `waiting`); at green -- or with a
# Fixes-Red fix for the red among them -- the tick ASSEMBLES the batch in a check tree (main + each waiting change, in
# batch order: a conflict rejects that change alone, one already on main is LANDED), and the batch executor (`waiting
# check-batch <id>`, holding check/batch-<id> and the tree's slot) checks it once (--fast-only, a fresh sbt server, a
# stall retried once) and, failing, bisects it (a culprit confirmed before it is rejected). The tick pushes a passing
# batch under the landing lease (PREPUSH_REUSE_FAST) and verifies it on main: every change LANDED. A batch that fails to
# land BATCH_RETRY_MAX times, or stalls twice, is held for 30 min (its deadline), then assembled again. Every state has
# its deadline and its exit (MACHINES["batch"]); nothing loops, nothing waits on a lock.
BATCH_RETRY_MAX = 3


BATCH_LEASE_MAX = 6 * 3600  # the batch executor's leases: its check (2 h) and its bisect (4 h), its machine's bounds


def batch_key(d):
    return os.path.basename(d.rstrip("/"))


def current_batch(st, d, any_key=False):
    """The repository's open batch item, or None. any_key: whichever host's key it carries -- the laptop's `land
    status` reads ci-runner's store, whose batch is keyed by ci-runner's waiting dir (var_lib_agentctl_movies_.git),
    never the laptop's (2026-10-30: batch #4 checked for an hour while `land status` showed no batch at all)."""
    key = batch_key(d)
    open_ = [it for it in st.items("batch") if (any_key or it["id"].startswith(key + "#"))
             and not machine_state("batch", it["state"]).terminal]
    return max(open_, key=lambda it: int(it["id"].rsplit("#", 1)[1])) if open_ else None


def status_batch(st, d):
    """The batch `land status` shows: this host's, else (the laptop) the one ci-runner's reconciler runs."""
    return current_batch(st, d) or current_batch(st, d, any_key=True)


def new_batch(st, d, state, depth=None):
    key = batch_key(d)
    n = int(st.meta_get(f"batch_seq:{key}", 0) or 0) + 1
    st.meta_set(f"batch_seq:{key}", n)
    return st.put_item("batch", f"{key}#{n}", state, {"retries": 0, **({"depth": depth} if depth else {})})


def batch_move(st, it, to, data=None, text=None, severity=None):
    ev = {"text": text or f"batch {it['id']}: {it['state']} -> {to}"}
    if severity:
        ev["severity"] = severity
    st.transition("batch", it["id"], to, it["version"], data=data, event=ev)
    return st.item("batch", it["id"])


def batch_fail(st, d, it, why, n_changes=0):
    """A batch that did not land: assembled again, or after BATCH_RETRY_MAX tries held (with a condition on the git
    route, cleared by git answering)."""
    n = int(it["data"].get("retries") or 0) + 1
    if n < env_num("AGENTCTL_BATCH_RETRY_MAX", BATCH_RETRY_MAX):
        wlog(d, f"the batch {it['id']} did not land ({why}): retry {n}/{BATCH_RETRY_MAX}")
        return batch_move(st, it, "assembling", {"retries": n})
    text = (f"HELD: the batch {it['id']} ({n_changes} change(s)) did not land {n} times ({why}); it is assembled again "
            f"when git can read {remote()}/main, or in 30 min")
    wlog(d, text)
    it = batch_move(st, it, "held", {"retries": 0, "why": why}, text=text, severity="escalate")
    open_condition("STALLED", text, run_probe("git_fetch", {"repo": repo_of_waiting(d)}),
                   [("clear_hold", {"d": d})], key=f"batch-retry-{it['id'].replace('#', '-')}", command="waiting")
    return it


def repo_of_waiting(d):
    return movies_repo_path()


def batch_members(d, it):
    """The batch's open member entries, in batch order: waiting entries (a red batch's) or a backlog batch's queued ones."""
    ids = it["data"].get("members") or []
    es = {e["id"]: e for e in waiting_entries(d, open_only=False) if e.get("status") not in ENTRY_FINAL}
    return [es[i] for i in ids if i in es]


# ---------------------------------------------------------------- the backlog batch (the owner, 2026-11-17)
# "BATCH WHENEVER THERE'S A BACKLOG, NOT ONLY ON RED": with 6-8 changes queued at green each paid a 30-40 min check of
# its own, mostly waiting for a ci-runner check slot. Now, at green with no batch open, BACKLOG_BATCH_MIN or more queued
# changes that no checker has taken and that may share a check (backlog_batchable: identity changes too, 2026-11-20;
# no `land --no-batch`, no red fix, no combined-tree verification
# round, none in an infra backoff or holding an unread remote run) are THE BATCH ABOVE with depth `full`: assembled in
# queue order (Fixes-Red first; a member that does not apply is left out and checked alone, batch_alone), its tip
# checked once with the FULL landing check (the hook's --range dry run; on ci-runner check/batch-<tip>, the same
# precommit-check round a change gets, area tests and heavy rows included -- no member had a check of its own), in
# ONE check tree. Pass: one push carrying that run (PREPUSH_REMOTE_RUN), every member LANDED with its own record and
# watch (batch_id, batch). Fail: bisected by prefix (no whole-batch re-check first: the culprit's own confirmation
# guards a flaky failure), the culprit rejected only when its failure is its own (judge_failure: a base red, a time
# cap or an infra failure sends it to a check of its own instead), the passing prefix pushed at once, the rest queued
# again (backlog_reconcile). At most BACKLOG_BATCH_MAX members, so a bisect is at most 4 extra rounds (3 prefixes and
# the confirmation). While such candidates wait for a tree, the change tick gives them none of its own (they wait for
# the batch); red fixes are checked alone beside it, and a red fix never waits for it.
BACKLOG_BATCH_MIN = 2  # queued changes that may share a check and make a backlog batch
BACKLOG_BATCH_MAX = 8  # a backlog batch's members at most: its bisect is 3 prefix checks + 1 confirmation
# EVERY CHANGE IS A MEMBER (2026-11-20, the owner: "batching waiting changes ... structurally"): an identity change joins
# like any other -- before, it was always checked alone; for a few hours of 2026-11-20, one per batch. The batch's one
# full check runs the union of its members' areas, and G5's drift check (check_batch) of a tip holding more than one
# identity-model commit, with its split, tells which identity change moved takes.
BACKLOG_LIVE = ("checking", "bisecting", "pushing")  # a backlog (or push) batch's states that hold its members
DEEP_BATCHES = ("full", "verify")  # the depths whose members are queue entries of their own: backlog and push batches


def backlog_min():
    return max(2, int(env_num("AGENTCTL_BACKLOG_BATCH_MIN", BACKLOG_BATCH_MIN)))


def change_paths_of(repo, e, commits=None):
    """The paths the entry's commits (or those of them in `commits`) touch (their commits fetched from
    refs/heads/land/<id> when this clone has none); None when they cannot be read."""
    if not git_ok(repo, "cat-file", "-e", f"{e.get('head')}^{{commit}}"):
        git(repo, "fetch", "--quiet", remote(), f"refs/heads/land/{e['id']}", check=False)
    paths = set()
    for c in (e.get("commits") or []) if commits is None else commits:
        r = git(repo, "diff-tree", "--no-commit-id", "--name-only", "-r", "--root", "-m", c, check=False)
        if r.returncode != 0:
            return None
        paths |= {p for p in r.stdout.splitlines() if p}
    return sorted(paths)


def pure_move_claimed(repo, commit):
    """Whether the commit's message carries the trailer `Pure-Move: verified` (movies' scripts/pure-move-check claim)."""
    r = git(repo, "log", "-1", "--format=%(trailers:key=Pure-Move,valueonly)", commit, check=False)
    return r.returncode == 0 and [v.strip() for v in r.stdout.splitlines() if v.strip()] == ["verified"]


_identity_known = process_cache({})  # (entry id, head) -> identity_change's verdict


def identity_change(repo, e):
    """Whether the change is an identity change (G5's drift check of a batch holding more than one): G5's identity-model commits, or the hook's
    --identity-change naming a path that can move takes (a hook without it: its --plan selecting the `identity` area). None when it cannot tell (no plan, unreadable commits): checked alone. Kept per
    entry and head in the process (never written to the entry: an executor may be saving it meanwhile)."""
    key = (e["id"], e.get("head"))
    if key in _identity_known:
        return _identity_known[key]
    if not git_ok(repo, "cat-file", "-e", f"{e.get('head')}^{{commit}}"):
        git(repo, "fetch", "--quiet", remote(), f"refs/heads/land/{e['id']}", check=False)
    # A VERIFIED PURE MOVE OWES NO IDENTITY AREA (movies' hook, 2026-11-19): a commit saying `Pure-Move: verified` moves
    # no take -- the hook's puremove check proves the move -- so only the commits without the claim make an identity
    # change; the hook selects the identity area by the same rule, so a batch holding the move runs none for it.
    commits = [c for c in e.get("commits") or [] if not pure_move_claimed(repo, c)]
    if not commits:
        _identity_known[key] = False
        return False
    paths = change_paths_of(repo, e, commits)
    hook = precheck_hook(repo)
    # WHAT CAN MOVE TAKES, NOT WHAT RUNS IDENTITY SPECS (2026-11-19): the hook's `--identity-change` names only the
    # take paths (movies: the resolver, its adapters, the title rules, the judgements), while its identity AREA also
    # runs for a common/ change some identity specs read -- which still batches. A hook without it: its --plan's area.
    try:
        with open(hook or "") as f:
            text = f.read()  # a flag is never asked unless the hook has it: an unknown flag runs the real checks
    except OSError:
        text = ""
    flag = "--identity-change" if "--identity-change)" in text else "--plan" if "--plan)" in text else None
    if paths is None or flag is None:
        return None
    plan = hook_query(hook, repo, flag, paths)
    if plan is None:
        return None
    verdict = "identity" in plan or bool(identity_model_commits(repo, commits))
    _identity_known[key] = verdict
    return verdict


def backlog_batchable(repo, d, e, t, facts=None):
    """Whether queued entry e may join a backlog batch now (THE BACKLOG BATCH, above): its KIND lets it (no red fix,
    no --no-batch, no batch_alone, no identity change it cannot tell: changekind.py) and its state does (queued, no verify round, no
    retry due, no own run or live checker)."""
    rem = e.get("remote") or {}
    if e.get("status") != "queued" or e.get("backlog_batch") or e.get("verify") or float(e.get("retry_after") or 0) > t:
        return False
    if rem.get("run") is not None and not rem.get("collected_at"):  # its own run: a checker adopts it
        return False
    if checker_alive(e["id"]):
        return False
    return kind_of(repo, e, facts, t)["backlog_batch"] is None


def kind_of(repo, e, facts, t):
    """Entry e's kind (changekind.py classify_change) under facts (queue_facts of the gate's reds when None), read as
    it is now: a flag set since the tick began (push_alone, batch_alone, known_red) counts."""
    return classify_change(e, facts if facts is not None else queue_facts(gate_reds(repo)), t, repo)


def backlog_candidates(repo, d, reds, t=None, facts=None):
    """The queued changes a backlog batch takes now, in queue order, at most BACKLOG_BATCH_MAX; [] while the gate is
    red, a change verifies its combined tree, or fewer than BACKLOG_BATCH_MIN may go. A fix of an open red (its kind
    under facts: queue_facts, read from the store when None) is checked alone."""
    t = t if t is not None else time.time()
    if reds:
        return []
    facts = queue_facts(reds) if facts is None else facts
    entries = queued_entries(d)
    # THE OLDEST VERIFICATION PUSHES FIRST (queuepush.py) holds the batch too (review 2026-11-18): formed meanwhile, the
    # batch held the verified change's push for its whole check (THE BATCH GOES FIRST), then its push moved main under
    # it and it verified again -- behind the next backlog batch, and the next. No backlog batch while one verifies, or
    # while a checked change's push is due (its verification may have ended since the change tick).
    if any((verifying(e, t) or (e.get("status") == "checked" and not float(e.get("retry_after") or 0) > t))
           and not e.get("backlog_batch") for e in entries):  # a checked change's push is due at the next change tick
        return []
    ok = [e for e in entries if backlog_batchable(repo, d, e, t, facts)
          and not overlap_hold(repo, d, e, entries, facts, t)]
    out = sorted(ok, key=lambda e: e["seq"])[:int(env_num("AGENTCTL_BACKLOG_BATCH_MAX", BACKLOG_BATCH_MAX))]
    return out if len(out) >= backlog_min() else []


def backlog_held(repo, d, reds, st, t=None, facts=None):
    """The ids of the queued changes the change tick holds back for the next backlog batch: with no batch open, the
    candidates one would take now."""
    return set() if current_batch(st, d) else {c["id"] for c in backlog_candidates(repo, d, reds, t, facts)}


def batch_kind(it):
    """`backlog batch` or `push batch`: how the member events name a batch item (by its depth)."""
    return "push batch" if ((it or {}).get("data") or {}).get("depth") == "verify" else "backlog batch"


def backlog_reconcile(d, st):
    """A backlog or push batch's members follow it: once it passed (pushing), each still `checking` is checked (a push
    batch's: verified, its own full check standing); a member it no longer carries (a prefix landing without it, the
    batch assembled again, held or gone) is queued again for the next batch (a push batch's: for its verify round, its
    full check kept). Returns what it did."""
    did = []
    for e in waiting_entries(d, open_only=False):
        bid = e.get("backlog_batch")
        if not bid or e.get("status") in ENTRY_FINAL:
            continue
        it = st.item("batch", bid)
        dd = (it or {}).get("data") or {}
        kind = batch_kind(it) if it else "batch"
        if it and it["state"] in BACKLOG_LIVE and e["id"] in (dd.get("members") or []):
            if it["state"] == "pushing" and e["status"] == "checking" and dd.get("depth") == "verify":
                set_change(d, e, "checked", event={"text": f"{e['id']}: verified {e['tip'][:10]} on {dd['base'][:10]} "
                                                           f"in push batch {bid} (fully checked on "
                                                           f"{(e.get('full_base') or '?')[:10]})"},
                           checked_base=dd["base"], checked_mode=VERIFY_MODE, checked_where=f"in push batch {bid}")
            elif it["state"] == "pushing" and e["status"] == "checking":
                set_change(d, e, "checked", event={"text": f"{e['id']}: checked in backlog batch {bid} ({e['tip'][:10]} "
                                                           f"on {dd['base'][:10]})"},
                           checked_base=dd["base"], full_base=dd["base"], full_tip=e["tip"], checked_mode="--range",
                           checked_where=f"in backlog batch {bid}")
            continue
        if e["status"] == "pushing":
            set_change(d, e, "checked", event={"text": f"{e['id']}: {kind} {bid}'s push did not land"})
        set_change(d, e, "queued", event={"text": f"{e['id']}: queued again: {kind} {bid} is "
                                                  f"{it['state'] if it else 'gone'} without it"},
                   backlog_batch=None, retry_after=0)
        did.append(f"{e['id']}: queued again ({kind} {bid})")
    return did


# ---------------------------------------------------------------- the push batch (the owner, 2026-11-17)
# "COMBINE THEM INSTEAD OF LANDING THEM ONE BY ONE" (W1: the oldest unlanded change over 2 h). A checked change pushed
# alone: on a main that moved since its full check it got a verify round of its own (THE RULE, queue.py), and THE
# OLDEST VERIFICATION PUSHES FIRST held every younger checked change meanwhile -- so N checked changes landed serially,
# each paying a check-runner wait plus a ~10-min verify round after the previous push moved main (ref-scrapers
# verifying while pr274 and postlanding held their pushes behind it, each to verify again after it pushed). Now, at
# green with no batch open and no verify round running, PUSH_BATCH_MIN or more changes that would each need a verify
# round on the current main (push_batchable: checked on an older main, or queued for a verify round; no red fix, no
# `land --no-batch`, no known red, none in a backoff) are THE BATCH ABOVE with depth `verify`, the backlog batch's
# machine: assembled oldest first on the current main, each member judged by THE RULE against main plus the members
# before it (push_batch_select: one that needs a FULL round is left out -- checked again alone now when what landed on
# main demands it, else pushed alone after the batch, push_alone), the union verified ONCE (the hook's --verify-only
# over <the oldest member's full base>..<tip>: every test config compiled, the lints, the fast specs and the unit
# tests of every module a member or what landed meanwhile touches; on ci-runner check/verify-<base>-batch-<tip>), and
# pushed ONCE (the hook skipping the plan the members' full checks passed, as a lone verified change's push does).
# Fail: bisected by prefix as the backlog batch is (the culprit confirmed, rejected only when the failure is its own),
# the passing prefix pushed, the rest queued again for their verify round. More than one identity-model commit in it:
# G5's drift check of its tip (check_batch); after a drift split, one identity change per batch (drift_serialize). A
# change checked while a push batch runs is no member: its push waits for the batch (batch_goes_first) and it joins
# the next one. At most BACKLOG_BATCH_MAX members.
PUSH_BATCH_MIN = 2  # changes owing a verify round that make a push batch


def push_batchable(repo, d, e, t, main, facts=None):
    """Whether entry e would need a verify round on main (THE PUSH BATCH above) and may share one: its KIND lets it (no
    red fix, no --no-batch, batch_alone, push_alone or known red: changekind.py) and it is checked on an older main, or
    queued for its verify round with no checker on it."""
    rem = e.get("remote") or {}
    if e.get("backlog_batch") or float(e.get("retry_after") or 0) > t:
        return False
    if kind_of(repo, e, facts, t)["push_batch"] is not None:
        return False
    if e.get("status") == "checked":  # one keeping its verdict on main (MINIMAL RECHECKS) owes no verify round
        return bool(e.get("checked_base")) and e["checked_base"] != main and not verdict_kept(repo, e, main)
    if e.get("status") != "queued" or not e.get("verify") or not e.get("full_base"):
        return False
    if rem.get("run") is not None and not rem.get("collected_at"):  # its own run: a checker adopts it
        return False
    return not checker_alive(e["id"])


def push_candidates(repo, d, reds, t=None, facts=None):
    """The changes a push batch takes now, oldest first, at most BACKLOG_BATCH_MAX; [] while the gate is red, while a
    change's own verify round runs (it pushes first), or when fewer than PUSH_BATCH_MIN may go. A fix of an open red
    (its kind under facts: queue_facts, read from the store when None) pushes alone."""
    t = t if t is not None else time.time()
    if reds:
        return []
    facts = queue_facts(reds) if facts is None else facts
    entries = queued_entries(d)
    if any(e.get("status") == "checking" and e.get("verify") and not e.get("backlog_batch") for e in entries):
        return []
    # NO PUSH BATCH UNDER A RED FIX IN FLIGHT (2026-11-19 10:37:14Z: push batch #48 formed 13 s before 08010c, a red
    # fix just verified, pushed; its push moved main under the batch's verify round, which passed at 10:46 and was
    # thrown away -- "main moved during its check: assembled again"). A red fix goes first and pushes alone, so a push
    # batch formed meanwhile is verified for nothing: none forms until the open red fixes are decided.
    if any(not e.get("backlog_batch") and not float(e.get("retry_after") or 0) > t
           and classify_change(e, facts, t)["red_fix"] for e in entries):
        return []
    main = git(repo, "rev-parse", f"{remote()}/main", check=False).stdout.strip()
    out = [e for e in sorted(entries, key=lambda e: e["seq"])
           if push_batchable(repo, d, e, t, main, facts)]
    out = drift_serialize(repo, out)[:int(env_num("AGENTCTL_BACKLOG_BATCH_MAX", BACKLOG_BATCH_MAX))]
    return out if len(out) >= max(2, int(env_num("AGENTCTL_PUSH_BATCH_MIN", PUSH_BATCH_MIN))) else []


def push_held(repo, d, reds, st, t=None, facts=None):
    """The ids of the changes the change tick holds back for the next push batch: with no batch open, the candidates
    one would take now."""
    return set() if current_batch(st, d) else {c["id"] for c in push_candidates(repo, d, reds, t, facts)}


def push_batch_select(repo, d, tree, kept, base, tips):
    """(members, tip): the assembled push batch less every member THE RULE (recheck_reason) sends to a FULL round,
    judged against main plus the members before it; the rest assembled again without it. One left out for what landed
    on main is queued for its full round now (as its own push would queue it); one left out only for a member before it
    is marked push_alone (it pushes alone after the batch, its own push judging it). tips: each member's own tip, from
    before the assembly (a checked one left out keeps it)."""
    hook = precheck_hook(tree)
    while kept:
        prev, out = base, None
        for e in kept:
            git(tree, "checkout", "-q", "--force", "--detach", e["tip"])
            full_base = e.get("full_base") or e.get("checked_base") or base
            paths = range_paths(tree, prev, e["tip"])
            why = recheck_reason(tree, full_base, prev, e["tip"], paths, full_tip=e.get("full_tip"))[1]
            if why is None and not verify_supported(hook):
                why = f"the hook has no {VERIFY_MODE} for the combined tree"
            if why is not None:
                main_why = why if prev == base else \
                    recheck_reason(tree, full_base, base, e["tip"], paths, full_tip=e.get("full_tip"))[1]
                out = (e, why, main_why)
                break
            prev = e["tip"]
        if out is None:
            return kept, prev
        e, why, main_why = out
        if e.get("status") == "checked":
            e["tip"] = tips.get(e["id"]) or e["tip"]  # its own tip again, never the batch's
        if main_why is not None:
            set_change(d, e, "queued", data={"rounds": e.get("rounds", 0) + 1},
                       event={"text": f"{e['id']}: main moved since its checks, {main_why}: checked again "
                                      "(left out of the push batch)"},
                       rounds=e.get("rounds", 0) + 1, retry_after=0, verify=False)
        else:
            e["push_alone"] = why
            save_entry(d, e)
        wlog(d, f"{e['id']}: left out of the push batch ({why}): "
                + ("its full round now" if main_why is not None else "it pushes alone after the batch"))
        kept = assemble(repo, d, tree, [k for k in kept if k["id"] != e["id"]], base, alone=True)
    return [], base


def union_base(tree, members, base):
    """The push batch's verify range base: the oldest of its members' full-check bases (base when none is read)."""
    bases = sorted({e.get("full_base") or e.get("checked_base") or base for e in members})
    if len(bases) == 1:
        return bases[0]
    r = git(tree, "merge-base", "--octopus", *bases, check=False)
    return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else base


# ---------------------------------------------------------------- the land hold (`land hold|release`)
LAND_HOLD_DEFAULT = 4 * 3600  # s a `land hold` lasts unless --for says otherwise: it then lifts itself (expired)
LAND_HOLD_MAX = 24 * 3600     # s: the longest --for a hold takes
# the land hold (`land hold|release`): an item in the store, so ci-runner's reconciler honours a hold made on the laptop
# (the PAUSED file it replaced was the laptop's own, read by no tick since the phase 2 cutover). Its deadline is its
# expiry: a forgotten hold lifts itself (`expired`), never a manual-ack-only state.
MACHINES["hold"] = {
    "held": StateSpec(LAND_HOLD_DEFAULT, "expired", ("held", "released", "expired")),
    "released": StateSpec(ARCHIVE, "released", (), terminal=True),
    "expired": StateSpec(ARCHIVE, "expired", (), terminal=True),
}


def hold_by():
    """Who holds or releases: the agent's name, else the user and host."""
    try:
        name = agent_name()
    except (Exception, SystemExit):
        name = None
    return name or f"{os.environ.get('USER', '?')}@{os.uname().nodename.split('.')[0]}"


def land_holds(st):
    return st.items("hold", ["held"])


def land_hold(st, now_=None, expire=True):
    """The land hold in force, or None. A hold past its deadline is moved to `expired` here (an event says so): every
    reader -- the tick, `land status`, the snapshot -- lifts a forgotten hold the moment it is due. expire=False: only
    read (the status page never writes)."""
    t = now_ if now_ is not None else time.time()
    live = None
    for it in land_holds(st):
        if it["deadline"] <= t:
            if not expire:
                continue
            try:
                st.transition("hold", it["id"], "expired", it["version"],
                              event={"text": f"land hold {it['id']} expired (held by {it['data'].get('by')}: "
                                             f"{it['data'].get('why') or 'no reason given'})"})
            except (StaleVersion, TerminalState):
                pass
        elif live is None or it["deadline"] > live["deadline"]:
            live = it
    return live


def hold_text(h, t=None):
    """`HELD until HH:MM (by, why)` for a hold row ({deadline, data} or the snapshot's {until, by, why})."""
    until = h.get("until") or h.get("deadline")
    d = h.get("data") or h
    return (f"HELD until {time.strftime('%H:%M', time.localtime(float(until)))} (`agentctl land release`; "
            f"by {d.get('by') or '?'}" + (f": {d['why']}" if d.get("why") else "") + ")")


def land_hold_set(st, secs, why, by):
    """Hold, or extend the hold in force to now + secs. Returns the hold item."""
    secs = max(60, min(int(secs), LAND_HOLD_MAX))
    cur = land_hold(st)
    data = {"by": by, "why": why or "", "at": time.time()}
    if cur:
        st.transition("hold", cur["id"], "held", cur["version"], data=data, deadline_s=secs,
                      event={"text": f"land hold {cur['id']} extended by {by}: {why or 'no reason given'}",
                             "dedupe": f"hold:{cur['id']}:extend:{cur['version'] + 1}"})
        return st.item("hold", cur["id"])
    n = int(st.meta_get("hold_seq", 0) or 0) + 1
    st.meta_set("hold_seq", n)
    it = st.put_item("hold", f"land#{n}", "held", data)
    if secs != LAND_HOLD_DEFAULT:  # its own expiry: a self-transition re-deadlines it
        st.transition("hold", it["id"], "held", it["version"], deadline_s=secs,
                      event={"text": f"land hold {it['id']} by {by} for {secs // 60} min: {why or 'no reason given'}"})
        it = st.item("hold", it["id"])
    return it


def land_hold_release(st, by):
    """Release every hold in force. Returns their ids."""
    out = []
    for it in land_holds(st):
        try:
            st.transition("hold", it["id"], "released", it["version"], data={"released_by": by},
                          event={"text": f"land hold {it['id']} released by {by}"})
            out.append(it["id"])
        except (StaleVersion, TerminalState):
            pass
    return out


def tick_land_hold(st=None):
    """The reconciler's step: a hold past its deadline expires even while no batch ticks."""
    land_hold(st or open_store())
    return []

# NO SILENT WAIT AT GREEN (2026-10-30). Changes waiting while the gate is green, with no hold and no batch checking,
# bisecting, pushing or held, are a stall: counted per tick (batch-progress.json in the waiting dir) and, past
# WAITING_GREEN_TICKS ticks, a STALLED condition. Its probe (batch_progress) reads the same count, so the tick that
# finds the batch moving again -- or nothing waiting, or the gate red -- is the one that clears it.
WAITING_GREEN_TICKS = 100  # ticks (~10 min) changes may wait at a green gate with no batch moving: then STALLED
BATCH_MOVING = ("checking", "bisecting", "pushing", "held")  # held: its own STALLED condition says so


def batch_progress_path(d):
    return os.path.join(d, "batch-progress.json")


def note_batch_progress(d, seen, now_=None):
    """Count this tick: stalled (changes wait, gate green, no hold, no batch moving) or not. Returns the count."""
    t = now_ if now_ is not None else time.time()
    waiting, it = seen.get("waiting") or [], seen.get("batch")
    stalled = (bool(waiting) and seen.get("reds") is not None and not seen["reds"] and not seen.get("held")
               and not (it and it["state"] in BATCH_MOVING))
    p = batch_progress_path(d)
    rec = read_json_file(p)
    if not stalled:
        if rec.get("ticks"):
            write_json_file(p, {"ticks": 0, "at": t})
        return 0
    n = int(rec.get("ticks") or 0) + 1
    since = float(rec.get("since") or t) if rec.get("ticks") else t
    why = (f"batch {it['id']} is {it['state']}" + (f" ({seen['last']})" if seen.get("last") else "")) if it else \
        "no batch was formed" + (f" ({seen['last']})" if seen.get("last") else "")
    write_json_file(p, {"ticks": n, "at": t, "since": since, "why": why, "waiting": [e["id"] for e in waiting]})
    limit = env_num("AGENTCTL_WAITING_GREEN_TICKS", WAITING_GREEN_TICKS)
    if n >= limit:
        ids = ", ".join(e["id"] for e in waiting)
        open_condition("STALLED", f"STALLED: {len(waiting)} change(s) waiting while the gate is green for {n} ticks "
                                  f"({fmt_age(t - since)}): {why}; waiting: {ids}",
                       run_probe("batch_progress", {"d": d}), [], key=f"waiting-green-{batch_key(d)}",
                       command="waiting")
    return n


def probe_batch_progress(a):
    """The batch tick no longer counts a stall at green (batch-progress.json under the limit)."""
    rec = read_json_file(batch_progress_path(a["d"]))
    n = int(rec.get("ticks") or 0)
    limit = env_num("AGENTCTL_WAITING_GREEN_TICKS", WAITING_GREEN_TICKS)
    return n < limit, (f"{n} stalled tick(s) at green: {rec.get('why')}" if n else "no change waits at green")


def tick_batch(repo, d, now_=None):
    """One tick of the repository's batch, then its stall count (note_batch_progress). Returns what it did."""
    seen = {}
    did = _tick_batch(repo, d, now_, seen)
    try:
        seen["last"] = did[-1] if did else None
        if seen.get("waiting"):  # the batch as this tick left it (assembled -> checking is no stall)
            seen["batch"] = current_batch(open_store(), d)
        note_batch_progress(d, seen, now_)
    except (Exception, SystemExit) as e:  # never the batch's own step
        err(f"batch progress: {str(e)[:160]}")
    return did


_batch_said = [None]


def say_batch(d, lines):
    """The batch tick's lines: every one in processor.log, and in the reconciler's journal when they change (the
    journal had no batch line at all; a repeated `waiting for a check tree` is said once)."""
    for x in lines:
        wlog(d, f"tick: {x}")
    if lines and lines != _batch_said[0]:
        err("batch: " + "; ".join(lines))
    _batch_said[0] = lines or _batch_said[0]
    return lines


# BLOCKED BY WHAT MUST WAIT (2026-10-31 06:44-07:50Z). Main #2877 red; the one waiting change named it (Fixes-Red: 2877
# on one commit) but carried a production commit naming no red, so must_wait kept it for green. "A fix waits" sent the
# batch to assembling; assembling built nothing (every change must wait), said nothing, and its deadline set it
# assembling again every 5 min -- and an assembling batch goes first, so three checked changes that could land on red
# waited behind it for over an hour. Now red, a batch assembles only when something in it may land now (batch_ready);
# otherwise it is blocked-red, which holds no solo change, until the gate turns or a change that may go joins.
def batch_ready(repo, waiting, reds):
    """The changes a batch takes now, in batch order: red, only those that may land on red; after a drift-red batch at
    most one identity-model change (drift_serialize)."""
    order = batch_order(repo, waiting, reds)
    return drift_serialize(repo, [e for e in order if not reds or not must_wait(repo, e["commits"], reds)]
                           )[:waiting_batch_max()]


# A WAITING BATCH IS BOUNDED LIKE A BACKLOG BATCH (2026-11-20, the simulated 2026-11-19: with every production change
# joining `waiting` during a long red, the batch at green held 21-31 changes; one failure bisected across all of them and
# the p90 wait from arrival to LANDED doubled). It takes at most BACKLOG_BATCH_MAX waiting changes a round, in batch
# order -- its red pre-check the same ones --; the rest wait for the next round.
def waiting_batch_max():
    return int(env_num("AGENTCTL_BACKLOG_BATCH_MAX", BACKLOG_BATCH_MAX))


def _tick_batch(repo, d, now_, seen):
    """A backlog batch's members brought in line with it (backlog_reconcile), then the batch's step."""
    st = open_store()
    return backlog_reconcile(d, st) + batch_step(repo, d, now_, seen, st)


def check_tree_slot_free(repo, d):
    """Whether a check tree is free now (its lease taken and given back at once)."""
    slot = check_tree_slot(repo, d)
    if slot:
        slot[1].release()
    return bool(slot)


# THE BATCH IS THE MECHANISM (the owner, 2026-11-20: "batching waiting changes ... structurally, not just now"). While
# the gate is red each production change that must wait is still checked on its own, in parallel in the free check
# trees (its `--checks-only`), and joins `waiting`; the waiting batch -- blocked by the red -- then PRE-CHECKS every
# waiting change as ONE tree on the current main: assembled in batch order (one that no longer applies rejected alone,
# a generated file merged), checked once with `--checks-only` over the union of their areas (depth `red`), a failure
# bisected (the culprit rejected when the failure is its own; one failing only where main fails rejects nobody), and
# the verdict kept on the batch (`prechecked`: its members, base and tip) and on each member. It pre-checks again
# whenever its membership changes (main moving while red is judged once, at green). At green the batch, at most
# BACKLOG_BATCH_MAX changes, is assembled on the new main and lands in ONE push (green_batch_depth): its members exactly
# the pre-checked ones and main's move since read by none of their checks (MINIMAL RECHECKS, queuerecheck.py read_overlap) ->
# no check again; else the fast check of the combined tree, as before. Measured (the simulator over 2026-11-18..20's
# arrivals, 20 seeds a day, real round lengths): joining `waiting` unchecked instead, the batch's one tree doing every
# change's check, cut check time 8% but put the p90 wait at 274 ticks against 158 (the red's idle trees went unused);
# checked alone first, then pre-checked as one: p90 100. A red fix keeps its lane (2026-11-17..20: a red fix landed
# in a median 14 min, a batch member in 56).
def red_precheck(repo, d, st, it, waiting, reds):
    """The blocked-red waiting batch's step: pre-check every waiting change as one tree when its membership or main
    changed since the last pre-check and a check tree is free (THE BATCH IS THE MECHANISM, above)."""
    order = [e for e in batch_order(repo, waiting, reds) if must_wait(repo, e["commits"], reds)][:waiting_batch_max()]
    if not order:
        return []
    # AGAIN ONLY WHEN ITS MEMBERS CHANGE: main moving while red (a tools-only change, the fix) is judged once, at green,
    # by MINIMAL RECHECKS -- pre-checking again on every move cost the simulated 2026-11-18..20 ~20% more check time
    pre = it["data"].get("prechecked") or {}
    if pre.get("members") == [e["id"] for e in order]:
        return []
    if (it["data"].get("precheck_failed") or [None])[1:] == [e["id"] for e in order]:  # failed as it is: changes must
        return []  # come or go
    slot = check_tree_slot(repo, d)
    if not slot:
        return [f"batch {it['id']}: its red pre-check waits for a check tree"]
    i, h = slot
    try:
        git(repo, "fetch", "--quiet", remote(), "main", check=False)
        base = git(repo, "rev-parse", f"{remote()}/main").strip()
        tree = check_tree(repo, i, base)
        kept = assemble(repo, d, tree, order, base)
        tip = git(tree, "rev-parse", "HEAD").strip()
    finally:
        h.release()
    if not kept:
        return [f"batch {it['id']}: nothing to pre-check"]
    batch_move(st, it, "checking", {"members": [e["id"] for e in kept], "tip": tip, "base": base, "tree": i,
                                    "cls": "check", "depth": "red"},
               text=f"batch {it['id']}: pre-checking {len(kept)} waiting change(s) as one at {tip[:10]} on {base[:10]} "
                    "while main is red")
    spawn_batch_executor(repo, d, it["id"])
    return [f"batch {it['id']}: red pre-check of {len(kept)} change(s) at {tip[:10]}"]


def red_prechecked(d, st, it, members, tip, base, how, failed=None):
    """A red pre-check passed for members (the whole batch, or the prefix before a culprit; none: nothing passed): the
    verdict kept on the batch and on each member, the batch blocked by the red again. failed: the main and members it
    failed for as they are (not pre-checked again until either changes)."""
    for e in members:
        e.update(prechecked=it["id"], checked_base=base)
        save_entry(d, e)
    pre = {"members": [e["id"] for e in members], "base": base, "tip": tip} if members else None
    batch_move(st, it, "blocked-red", {"prechecked": pre, "precheck_failed": failed},
               text=f"batch {it['id']}: {how}" + (f": pre-checked {len(members)} change(s) as one at {tip[:10]}"
                                                   if members else ""))


def green_batch_depth(tree, it, kept, base, tip):
    """(depth, why) of the waiting batch at green (THE BATCH IS THE MECHANISM): `kept` when its members are exactly its
    red pre-check's and no check of theirs reads what main changed since, else `fast` (the combined tree's fast check,
    as before)."""
    pre = it["data"].get("prechecked") or {}
    if not pre or pre.get("members") != [e["id"] for e in kept] or not pre.get("base"):
        return "fast", "not its red pre-check's members"
    if pre["base"] == base:
        return "kept", f"pre-checked as one on {base[:10]}, main unchanged since"
    landed, mine = range_paths(tree, pre["base"], base), range_paths(tree, base, tip)
    if any(forces_full_recheck(p) for p in landed) or without_generated(tree, sorted(set(landed) & set(mine))):
        return "fast", "main changed what it changes"
    why = read_overlap(tree, landed, mine)
    if why:
        return "fast", why
    n = len(git(tree, "rev-list", f"{pre['base']}..{base}", check=False).stdout.split())
    return "kept", (f"pre-checked as one on {pre['base'][:10]}; main moved by {n} commit{'' if n == 1 else 's'} since, "
                    "no check of it reading what changed: its verdict kept")


# A RED FIX IS NEVER A BATCH MEMBER, AT GREEN TOO (2026-11-20; the simulator gate's chaos-19-t1, and five more chaos seeds
# of a wider sweep: a fix of a red OUTSIDE the gate waited on `waiting` while the gate was red -- its other production
# commits name no gate red -- and at green the waiting batch took it with the rest, so it waited for their combined
# check). At green, before a new waiting batch forms, each waiting change that fixes an open red goes back to the queue,
# where it goes first, alone, in the red-fix tree (A FIX OF ANY OPEN RED GOES FIRST, changekind.py).
def release_red_fixes(d, st, reds, waiting, t):
    facts = queue_facts(reds, st)
    out = []
    for e in waiting:
        k = classify_change(e, facts, t)
        if not k["red_fix"]:
            continue
        set_change(d, e, "queued", event={"text": f"{e['id']}: main is green -- a fix of an open red ({kind_text(k)}) "
                                                  "is checked alone and first, never a batch member", "severity": "info"},
                   waiting_released=ts())
        out.append(e["id"])
    return out


def batch_step(repo, d, now_, seen, st):
    t = now_ if now_ is not None else time.time()
    waiting = waiting_entries(d)
    it = current_batch(st, d)
    seen.update(waiting=waiting, batch=it)
    backlog = not waiting and not it and \
        sum(1 for e in queued_entries(d) if e.get("status") in ("queued", "checked")) >= backlog_min()
    if not waiting and not it and not backlog:
        return []
    if land_hold(st, t):  # the store's hold: honoured wherever `land hold` ran
        seen["held"] = True
        return ["held"]
    reds = gate_reds(repo)
    seen["reds"] = reds
    waiting = waiting_entries(d)
    released = release_red_fixes(d, st, reds, waiting, t) \
        if not reds and (not it or it["state"] in ("blocked-red", "assembling")) else []
    if released:
        waiting = waiting_entries(d)
    depth = (it or {}).get("data", {}).get("depth") or "fast"
    fix_waits = bool(reds) and any(classify_change(e, gate_facts(reds), t)["gate_fix"] for e in waiting)
    if not waiting and (not it or (depth in DEEP_BATCHES and it["state"] == "assembling")):
        # THE PUSH BATCH (checked changes, each owing a verify round) before THE BACKLOG BATCH (queued changes), no
        # waiting change; created only with a check tree free for it (an assembling batch waiting for one holds every
        # solo check behind it, and is held itself past its 5-min deadline)
        facts = queue_facts(reds, st)
        push = push_candidates(repo, d, reds, t, facts) if not it or depth == "verify" else []
        ready = push or (backlog_candidates(repo, d, reds, t, facts) if not it or depth == "full" else [])
        if not it:
            if not ready or not check_tree_slot_free(repo, d):
                return []
            it = new_batch(st, d, "assembling", depth="verify" if push else "full")
        elif not ready:  # the gate went red, or the changes went their own ways: no batch to assemble
            batch_move(st, it, "landed", {"members": []}, text=f"batch {it['id']}: no {batch_kind(it)} left to form")
            return [f"batch {it['id']}: no {batch_kind(it)} left to form"]
        depth = it["data"]["depth"]
    else:
        if not waiting and not it:
            return []
        unread_note(d, reds, waiting)
        ready = (batch_ready(repo, waiting, reds) if (not reds or fix_waits)
                 and (not it or it["state"] in ("blocked-red", "assembling")) else [])
    go = bool(ready)  # red: a fix waits AND something may land on red now (see BLOCKED BY WHAT MUST WAIT)
    if not it:
        it = new_batch(st, d, "assembling" if go else "blocked-red")
    spec = machine_state("batch", it["state"])
    if t > it["deadline"] and it["state"] != "checking" and it["state"] != "bisecting":
        it = batch_move(st, it, spec.on_deadline, text=f"batch {it['id']}: {it['state']} past its deadline")
        return [f"batch {it['id']}: deadline -> {it['state']}"]
    state = it["state"]
    if state == "blocked-red":
        if go:
            batch_move(st, it, "assembling")
            return [f"batch {it['id']}: assembling"]
        return red_precheck(repo, d, st, it, waiting, reds)
    if state == "assembling":
        if reds and not go:
            batch_move(st, it, "blocked-red")
            return [f"batch {it['id']}: blocked by the red gate"
                    + (" (its fix waits for green: a production commit names no red)" if fix_waits else "")]
        batch = ready
        if not batch:
            if not waiting:
                batch_move(st, it, "landed", {"members": []}, text=f"batch {it['id']}: nothing left to land")
            return []
        slot = check_tree_slot(repo, d)
        if not slot:
            return [f"batch {it['id']}: waiting for a check tree"]
        i, h = slot
        full = depth in DEEP_BATCHES
        tips = {e["id"]: e.get("tip") for e in batch}
        try:
            git(repo, "fetch", "--quiet", remote(), "main", check=False)
            base = git(repo, "rev-parse", f"{remote()}/main").strip()
            tree = check_tree(repo, i, base)
            kept = assemble(repo, d, tree, batch, base, alone=full, autoresolve=depth == "full")
            if depth == "verify":
                kept, _tip = push_batch_select(repo, d, tree, kept, base, tips)
            tip = git(tree, "rev-parse", "HEAD").strip()
            range_base = union_base(tree, kept, base) if depth == "verify" else None
        finally:
            h.release()
        if full and len(kept) < backlog_min():  # one left: it is checked (a push batch's: pushed) alone, as any change
            for e in kept:
                if e.get("status") == "checked" and tips.get(e["id"]):
                    e["tip"] = tips[e["id"]]
                    save_entry(d, e)
            batch_move(st, it, "landed", {"members": []}, text=f"batch {it['id']}: no {batch_kind(it)} left to form")
            return [f"batch {it['id']}: no {batch_kind(it)} left to form"]
        if not kept:
            if not waiting_entries(d):
                batch_move(st, it, "landed", {"members": []}, text=f"batch {it['id']}: nothing left to land")
            return [f"batch {it['id']}: nothing kept"]
        if not full:  # THE WAITING BATCH AT GREEN: pre-checked as one at red, its verdict kept, or checked as needed
            depth, why = green_batch_depth(tree, it, kept, base, tip)
            if depth == "kept":
                moved = (it["data"].get("prechecked") or {}).get("base") != base
                for e in kept if moved else ():  # MINIMAL RECHECKS' log, as a lone change's (land status, kept_verdicts)
                    e["rechecks"] = recheck_logged(e, "kept", why, base)
                    save_entry(d, e)
                batch_move(st, it, "checking", {"members": [e["id"] for e in kept], "tip": tip, "base": base, "tree": i,
                                                "cls": "check", "depth": "fast"}, text=f"batch {it['id']}: {why}")
                batch_move(st, st.item("batch", it["id"]), "pushing", {"kept": why},
                           text=f"batch {it['id']}: {tip[:10]} pushed on its red pre-check, no check again")
                return [f"batch {it['id']}: assembled {len(kept)} change(s), {why}"]
        batch_move(st, it, "checking", {"members": [e["id"] for e in kept], "tip": tip, "base": base, "tree": i,
                                        "cls": "red" if fix_waits else "check", "depth": depth,
                                        **({"range_base": range_base} if range_base else {})},
                   text=f"batch {it['id']}: checking {len(kept)} change(s) at {tip[:10]} on {base[:10]}"
                        + {"full": " (a backlog: the full landing check)",
                           "verify": f" (a push batch: one verify round over {(range_base or base)[:10]}..{tip[:10]})"}
                        .get(depth, ""))
        for e in kept if full else ():  # its members are the batch's until it lands them or lets them go
            verify = {"verify": True, "rounds": e.get("rounds", 0) + (e.get("status") == "checked")} \
                if depth == "verify" else {}
            resolved = "; ".join(f"{m['file']} ({', '.join(m['hunks'])})" for m in e.get("autoresolved") or [])
            set_change(d, e, "checking", data={"autoresolved": resolved or None},
                       event={"text": f"{e['id']}: checking in {batch_kind(it)} {it['id']} "
                                      f"({len(kept)} change(s) at {tip[:10]})"
                                      + (f"; its conflict AUTO-RESOLVED in the batch, the batch's full check its "
                                         f"verification: {resolved}" if resolved else ""),
                              **({"audience": {"worktree": e.get("worktree")}} if resolved else {})},
                       backlog_batch=it["id"], since=ts(), round_since=ts(), **verify)
        spawn_batch_executor(repo, d, it["id"])
        return [f"batch {it['id']}: assembled {len(kept)} change(s), checking {tip[:10]}"]
    if state in ("checking", "bisecting"):
        if not lease_live(f"check/batch-{it['id']}"):
            if t > it["deadline"]:
                return [batch_fail(st, d, it, "its check passed its deadline with no live checker")["state"]]
            spawn_batch_executor(repo, d, it["id"])
            return [f"batch {it['id']}: its executor started again"]
        return []
    if state == "pushing":
        return [push_batch_item(repo, d, st, it)]
    return []


# THE BATCH GOES FIRST (2026-10-30). Batch #4 (the flips and filmweb changes) passed its 45-min check at 14:34Z, but a
# solo change (identity-solid) had pushed at 14:05Z, so its push found main moved and assembled it again; and the moment
# its executor freed check tree 3 the change tick (it runs before the batch tick) started a change's check in it, so
# the batch could neither push from its tree nor, assembling, get any tree: it waited behind every solo change, for
# ever. Now, while the batch is open with changes waiting, a checked change does not push (assembling, checking,
# bisecting, pushing: a push moves main under it) and no new change check takes a tree while the batch needs one
# (assembling, pushing). Its deadlines bound the wait: a held or blocked-red batch lets solo changes go.
BATCH_FIRST = ("assembling", "checking", "bisecting", "pushing")


def batch_goes_first(st, d, states=BATCH_FIRST):
    """The open batch, when solo changes wait for it (its state in `states`, changes waiting in it or a backlog batch),
    else None."""
    it = current_batch(st, d)
    if it and (it.get("data") or {}).get("depth") == "red":  # a red pre-check holds nothing: what lands on red moves
        return None  # main under it, and it pre-checks again (THE BATCH IS THE MECHANISM)
    backlog = it and (it.get("data") or {}).get("depth") in DEEP_BATCHES
    return it if it and it["state"] in states and (backlog or waiting_entries(d)) else None


_behind_said = process_cache({})  # (entry id, what) -> batch id: said once per batch


def behind_batch(e, it, what):
    """The tick's line for a change held behind the batch: once per change, batch and kind."""
    k = (e["id"], what)
    if _behind_said.get(k) == it["id"]:
        return []
    _behind_said[k] = it["id"]
    return [f"{e['id']}: its {what} waits for batch {it['id']} ({it['state']})"]


def batch_row_ids(bt, rows):
    """The change rows the batch carries: its members, and a change that joined one of them (the same worktree: a
    queued change that met a red gate before ONE CHANGE, ONE ITEM, queue_join, became a waiting entry of its own, so
    its item follows that entry)."""
    if not bt:
        return set()
    members = set((bt.get("data") or {}).get("members") or [])
    wts = {r["worktree"] for r in rows if r["id"] in members}
    return {r["id"] for r in rows if r["id"] in members or (r["state"] == "waiting" and r["worktree"] in wts)}


def spawn_batch_executor(repo, d, bid):
    with open(os.path.join(d, "processor.log"), "a") as logf:
        spawn_detached([sys.executable, TOOL_PATH, "waiting", "check-batch", bid, "--repo", repo],
                         stdin=subprocess.DEVNULL, stdout=logf, stderr=logf, start_new_session=True, cwd=repo,
                         env=detached_env(repo))


# THE BATCH TIP'S DRIFT CHECK (G5, convergence-failures-2026-10-29 §4: E4's 13-commit and E9's 24-commit batches masked
# which identity change broke convergence). A batch holding MORE THAN ONE identity-model commit (IDENTITY_MODEL_GLOBS)
# runs the drift-only pre-land check -- movies' scripts/identity-drift-ci (a drift-only convergence run on ci-runner's
# landing tier; `--against main` for a batch landing on a red gate) -- on its tip after its own check passed and before
# it is pushed. No script at the tip: a logged no-op. NO VERDICT (exit 2): the push goes on (an infra blip never holds
# a batch). DRIFT RED splits the batch: its identity changes are marked (drift_split) and assembled again one per
# batch beside the rest, so the non-identity part lands at once and a lone red identity change -- confirmed by a second
# run -- is rejected with the drift report as its reason.
IDENTITY_MODEL_GLOBS = ("identity/*", "worker/src/main/*/identity/*", "worker/src/main/identity/*",
                        "worker/src/test/scala/scripts/IdentityDrift*")
DRIFT_CI = "scripts/identity-drift-ci"
DRIFT_STALL = 30 * 60  # s with no output before the drift check is ended (no verdict: the batch goes on)
# THE DRIFT CHECK WAITS QUIETLY (2026-10-30): identity-drift-ci prints its header and then nothing while its CI run goes
# on, so the batch executor's leases (CHECK_LEASE_TTL, 10 min, renewed on output or CPU only) lapsed at 10 min every
# time (#1, #3, #4: "lease ... was lost: ending drift", no verdict), and the freed check tree went to a change. It runs
# quiet_ok: every poll renews, and DRIFT_STALL alone ends it.


# THE DRIFT CHECK'S TOOLS (2026-10-30, batch #4: "could not push measure/ci-lander/f3b716998-drift-aed681"). It was no
# permission: the lander has write on movies and only main is protected, the same as the check/<id> branches it pushes
# for every change. The reconciler's systemd PATH has no python3 and no gh. movies' lock_take (jvm-gate.sh) flocks
# through `python3`, so identity_push_measure_branch waited 3600 s on a lock that could never be taken and gave up
# before it pushed. forge-gh, the script's `gh`, is python3 too. drift_env gives the script both: python3 (a shim to
# this interpreter, on the PATH only when none is there) and the repository's App token on ci-runner (forge_env), so
# the push and the dispatch run as the lander with the rights it already has.
def drift_env(repo, env=None):
    env = tool_env() if env is None else dict(env)
    try:
        env = forge_env(slug(repo), env)  # forge-gh is the real gh (KINOWO_FORGE), with the repo's App token on ci-runner
        env["KINOWO_FORGE"] = "github"
    except (Exception, SystemExit) as ex:  # no token: the script says so itself (NO VERDICT)
        err(f"drift check: no forge token for the script ({str(ex)[:120]})")
    parts = [p for p in env.get("PATH", "").split(":") if p]
    if not any(os.access(os.path.join(p, "python3"), os.X_OK) for p in parts):
        shim = state_root("tool-bin")
        link = os.path.join(shim, "python3")
        try:
            os.makedirs(shim, exist_ok=True)
            if os.path.realpath(link) != os.path.realpath(sys.executable):
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(link)
                os.symlink(sys.executable, link)
            env["PATH"] = ":".join(parts + [shim])
        except OSError as ex:
            err(f"drift check: no python3 for the script ({ex})")
    return env


def identity_model_commits(repo, commits):
    """The commits among `commits` that touch the identity model."""
    out = []
    for c in commits:
        r = git(repo, "diff-tree", "--no-commit-id", "--name-only", "-r", "--root", "-m", c, check=False)
        if any(fnmatch.fnmatch(p, g) for p in (r.stdout if r.returncode == 0 else "").splitlines() if p
               for g in IDENTITY_MODEL_GLOBS):
            out.append(c)
    return out


def drift_serialize(repo, batch):
    """After a drift-red batch (an entry marked drift_split waits): at most ONE identity-model change per batch, the
    first in batch order; the rest of the identity changes wait for the next batch, every other change goes now."""
    if not any(e.get("drift_split") for e in batch):
        return batch
    out, ident = [], False
    for e in batch:
        if identity_model_commits(repo, e.get("commits") or []):
            if ident:
                continue
            ident = True
        out.append(e)
    return out


def batch_drift_check(repo, d, tree, base, tip, against_main=False):
    """(verdict, output): True clean, False DRIFT RED, None no check (one identity commit or none, no script at the tip,
    or no verdict)."""
    commits = waiting_commits(tree, base, tip)
    n = len(identity_model_commits(tree, commits))
    if n <= 1:
        return None, ""
    if not git_ok(tree, "cat-file", "-e", f"{tip}:{DRIFT_CI}"):
        wlog(d, f"the batch {tip[:10]} holds {n} identity-model commits; no {DRIFT_CI} at its tip: no drift check")
        return None, ""
    git(tree, "checkout", "-q", "--force", "--detach", tip, check=False)
    argv = [os.path.join(tree, DRIFT_CI), tip] + (["--against", "main"] if against_main else [])
    wlog(d, f"the batch {tip[:10]} holds {n} identity-model commits: drift check of its tip ({' '.join(argv[1:])})")
    rc, out = run_watched(argv, tree, drift_env(repo), os.path.join(d, f"drift-{tip[:10]}"), "drift",
                          stall=env_num("AGENTCTL_DRIFT_STALL", DRIFT_STALL), quiet_ok=True)
    with open(os.path.join(d, "checks.log"), "a") as f:
        f.write(f"=== drift {ts()} {base[:10]}..{tip[:10]} rc {rc}\n{out}\n")
    last = ([l for l in (out or "").splitlines() if l.strip()] or ["(no output)"])[-1]
    if rc == 0:
        wlog(d, f"the drift check of {tip[:10]}: {last[:200]}")
        return True, out
    if rc == 1:
        wlog(d, f"the drift check of {tip[:10]} is RED: {last[:200]}")
        return False, out
    wlog(d, f"the drift check of {tip[:10]} gave no verdict (rc {rc}: {last[:200]}): the batch goes on")
    return None, out


def batch_drift_split(repo, d, st, it, members, tree, out):
    """DRIFT RED on the batch tip: a lone identity change confirmed red is rejected; else every identity change is marked
    and the batch assembled again, one identity change per batch (drift_serialize)."""
    ident = [e for e in members if identity_model_commits(tree, e.get("commits") or [])]
    bid = it["id"]
    if len(ident) == 1:
        e = ident[0]
        ok, out2 = batch_drift_check(repo, d, tree, it["data"]["base"], e["tip"], it["data"].get("cls") == "red")
        it = st.item("batch", bid)
        if ok is False:
            # policy: drift-check
            queue_verdict(d, e, "rejected", f"NOT LANDED: the drift check (scripts/identity-drift-ci) is red with your "
                                            f"identity-model change in the batch, twice (entry {e['id']})"
                                            f"\n  {tail_of(out2 or out)}", (out2 or out).splitlines()[-25:])
            batch_move(st, it, "assembling", text=f"batch {bid}: {e['id']} rejected (drift red)", severity="escalate")
            return 1
        batch_move(st, it, "pushing", text=f"batch {bid}: its lone identity change's drift check passed when run again")
        return 0
    for e in ident:
        e["drift_split"] = bid
        save_entry(d, e)
    batch_move(st, it, "assembling", text=f"batch {bid}: its drift check is red with {len(ident)} identity-model "
                                          f"changes in it: split, one identity change per batch")
    return 1


def check_batch(repo, bid):
    """THE BATCH EXECUTOR (`waiting check-batch <id>`): holding check/batch-<id> and its tree's slot, it checks the
    assembled tip; pass -> pushing (the tick pushes); fail -> bisecting -> a confirmed culprit rejected and the passing
    prefix -> pushing (or assembling), every prefix passing -> assembling again (counted); stalled twice -> held."""
    d, _c, _w = waiting_state(repo)
    st = open_store()
    it = st.item("batch", bid)
    if not it or it["state"] not in ("checking", "bisecting"):
        return 0
    # ONE EXECUTOR HOLDS THE BATCH THROUGH ITS CHECK AND ITS WHOLE BISECT (several checks of up to 2 h each): under a
    # change's CHECK_LEASE_MAX (3 h) the bisect lost its lease mid-way, read as a stall, and blamed "flaky" -- the culprit
    # was never rejected. Its machine bounds it (checking 2 h + bisecting 4 h); the leases' max hold matches.
    lease = LeaseHold(f"check/batch-{bid}", env_num("AGENTCTL_CHECK_LEASE_TTL", CHECK_LEASE_TTL), BATCH_LEASE_MAX)
    if not lease.acquire():
        return 0
    i = it["data"]["tree"]
    slot = tree_slot(d, i, BATCH_LEASE_MAX)
    try:
        if not slot.acquire():
            return 0
        _d, clone, _w = waiting_state(repo)
        tree = f"{clone}-wt-check-{i}"
        base, tip, members = it["data"]["base"], it["data"]["tip"], batch_members(d, it)
        if not members:
            batch_move(st, it, "assembling", text=f"batch {bid}: its changes are gone")
            return 0
        depth, runs = it["data"].get("depth") or "fast", dict(it["data"].get("runs") or {})
        range_base = it["data"].get("range_base")
        if it["state"] == "checking":
            for attempt in range(BATCH_INFRA_RETRIES + 1):  # AN INFRA FAILURE IS NO BISECT (batch_infra_failure)
                ok, out, stalled = batch_check(repo, d, tree, base, tip, it["data"].get("cls") or "check", depth,
                                               runs, range_base=range_base)
                infra = None if ok or stalled else batch_infra_failure(out)
                if not infra or attempt == BATCH_INFRA_RETRIES:
                    break
                wlog(d, f"batch {bid}: {tip[:10]} failed on infra ({infra}): checked again, not bisected")
            it = st.item("batch", bid)
            if stalled:
                text = (f"HELD: the batch check of {tip[:10]} ({len(members)} change(s)) stalled twice on a fresh sbt "
                        "server; checked again in 30 min")
                batch_move(st, it, "held", {"why": "stalled"}, text=text, severity="escalate")
                return 0
            if ok and depth == "red":  # pre-checked as one while red: the verdict kept, blocked by the red again
                red_prechecked(d, st, it, members, tip, base, "its red pre-check passed")
                return 0
            if ok:
                drift, dout = batch_drift_check(repo, d, tree, base, tip, it["data"].get("cls") == "red")
                it = st.item("batch", bid)
                if drift is False:
                    return batch_drift_split(repo, d, st, it, members, tree, dout)
                batch_move(st, it, "pushing", {"runs": runs}, text=f"batch {bid}: {tip[:10]} passed its check"
                                                                  + (" and its drift check" if drift else ""))
                return 0
            if REDGATE.search(out or ""):
                batch_move(st, it, "assembling", text=f"batch {bid}: the gate went red")
                return 0
            it = batch_move(st, it, "bisecting", {"failed_out": (out or "")[-20000:]},
                            text=f"batch {bid}: {tip[:10]} failed its check; bisecting")
        culprit, cout = bisect(repo, d, members, base, tree, depth, runs, it["data"].get("failed_out"),
                               range_base=range_base, cls=it["data"].get("cls") or "check")
        it = st.item("batch", bid)
        if culprit is None and depth == "red":  # every prefix passed: no verdict, pre-checked again at the next change
            red_prechecked(d, st, it, [], None, base, "its red pre-check failed, then every prefix passed (flaky or "
                           "infra)", failed=[base] + [e["id"] for e in members])
            return 0
        if culprit is None:
            batch_fail(st, d, it, "it failed, then every prefix passed on its own (flaky or infra)", len(members))
            return 0
        bad = members[culprit]
        if depth == "red" and not culprit_own_failure(repo, d, bad, base, cout):
            # failing only as main fails (the red itself, judged as a lone change's check is): no culprit; the prefix
            # before it pre-checked, the rest checked again at green (the fast check, as before)
            red_prechecked(d, st, it, members[:culprit], members[culprit - 1]["tip"] if culprit else None, base,
                           f"{bad['id']} on fail only where main fails too", failed=[base] + [e["id"] for e in members])
            return 0
        # a member whose generated files the batch resolved (GENERATED FILES ARE NO OVERLAP, waiting.py) failing is
        # the resolution's as much as its own: its own check, on main, judges it
        # and one whose source conflict the batch auto-resolved (A SOURCE CONFLICT IS RESOLVED IN THE BATCH): the
        # resolution is the suspect -- named, and the member held behind the change it conflicts with, never rejected
        regenerated, resolved = bad.get("regenerated"), bad.get("autoresolved")
        alone = depth in DEEP_BATCHES and (bool(regenerated) or bool(resolved)
                                           or not culprit_own_failure(repo, d, bad, base, cout))
        if alone:  # not shown to be its own: its own check judges it (a base red, a time cap, an infra failure)
            bad["batch_alone"] = "resolve" if resolved else "regen" if regenerated else bid
            bad.pop("autoresolved", None)
            save_entry(d, bad)
            said = (f"batch {bid}: {bad['id']}'s prefix fails, but not shown to be by its own fault"
                    + (f" (its {', '.join(regenerated)} resolved in the batch)" if regenerated else "")
                    + (f" -- the batch's AUTO-RESOLUTION of {', '.join(m['file'] for m in resolved)} is the culprit "
                       f"({'; '.join(h for m in resolved for h in m['hunks'])}): held behind the change it "
                       "conflicts with, as before" if resolved else "") + ": checked alone")
            wlog(d, said)
            if resolved:  # its author hears it (the batch says its auto-resolution failed)
                cur = st.item("change", bad["id"])
                if cur and not machine_state("change", cur["state"]).terminal:
                    try:
                        st.transition("change", bad["id"], cur["state"], cur["version"], event={
                            "text": said, "audience": {"worktree": bad.get("worktree")}, "severity": "info"})
                    except StaleVersion:
                        pass  # said in the log; the hold says the rest
        else:
            # policy: batch-checks-failed
            queue_verdict(d, bad, "rejected", f"NOT LANDED: the checks failed with your change in the batch (entry "
                                              f"{bad['id']})\n  {tail_of(cout)}", cout.splitlines()[-25:])
        what = ("checked alone" + (" (its auto-resolution failed the batch)" if alone and resolved else "")) \
            if alone else "rejected"
        if depth == "red":  # the prefix before it pre-checked; the rest are pre-checked again at the next tick
            red_prechecked(d, st, it, members[:culprit], members[culprit - 1]["tip"] if culprit else None, base,
                           f"{bad['id']} {what}")
            return 0 if alone else 1
        if culprit > 0:  # the prefix before it passed its own check: it lands now; the rest join the next batch
            prefix = members[:culprit]
            batch_move(st, it, "pushing", {"members": [e["id"] for e in prefix], "tip": prefix[-1]["tip"], "runs": runs},
                       text=f"batch {bid}: {bad['id']} {what}; the {culprit} change(s) before it land")
        else:
            batch_move(st, it, "assembling", text=f"batch {bid}: {bad['id']} {what}")
        return 0 if alone else 1
    finally:
        slot.release()
        lease.release()


# AN INFRA FAILURE IS NO BISECT (2026-11-19, review of batch #48's bisect): a batch's failed check went straight to its
# bisect whatever failed it -- only the red gate was excepted -- while a lone change's infra-shaped failure is re-queued
# (queue.py INFRA_CHECK_FAILURE). A lost sbt server or runner under a batch cost one prefix check per member, then
# "every prefix passed (flaky or infra)" and a whole new round. Now the batch's own check runs once more first; a second
# infra-shaped failure bisects as before (a member may be what loses the server: its own check then judges it, alone).
# (#48 itself failed for real: the combined tree's coupling ratchet, `services.movies: distance D 40 past its frozen
# row 39` -- a test failure, bisected rightly.)
BATCH_INFRA_RETRIES = 1


def batch_infra_failure(out):
    """The infra signature of a failed batch check's output (an sbt server lost, a runner or network failure with no
    test line beside it: classify.py's INFRA_LOG), else None."""
    m = INFRA_CHECK_FAILURE.search(out or "")
    if m:
        return m.group(0)
    lines = (out or "").splitlines()
    hit = next((line for line in lines if INFRA_LOG.search(line)), None)
    return hit.strip()[:160] if hit and not _test_line(lines) else None


def culprit_own_failure(repo, d, e, base, out):
    """Whether a backlog batch's confirmed culprit failed by its own fault, as a change's own check judges it
    (judge_failure): no infra failure, no time cap, a failing test that main's red runs and the queue's record do not
    share."""
    if INFRA_CHECK_FAILURE.search(out or "") or CHECK_TIMEOUT.search(out or ""):
        return False
    return judge_failure(repo, d, e, base, out)[0] == "head"


def push_batch_item(repo, d, st, it):
    """The tick's push of a passing batch, under the landing lease: main moved -> assembled again; else the tip pushed
    (the batch's fast pass stands for the push: PREPUSH_REUSE_FAST) and verified on main -> every change LANDED."""
    members = batch_members(d, it)
    tip, base, i = it["data"]["tip"], it["data"]["base"], it["data"]["tree"]
    if not members:
        batch_move(st, it, "assembling", text=f"batch {it['id']}: its changes are gone")
        return f"batch {it['id']}: nothing to push"
    slot = tree_slot(d, i)  # its tree: never touched under a checker that took the slot since the batch's check
    if not slot.acquire():
        return f"batch {it['id']}: its check tree is busy"
    try:
        return _push_batch_item(repo, d, st, it, members, tip, base, i)
    finally:
        slot.release()


def _push_batch_item(repo, d, st, it, members, tip, base, i):
    _d, clone, _w = waiting_state(repo)
    tree = f"{clone}-wt-check-{i}"
    with land_lock(repo, timeout=0) as held:
        if not held:
            return f"batch {it['id']}: the landing lease is busy"
        git(repo, "fetch", "--quiet", remote(), "main", check=False)
        if git(repo, "rev-parse", f"{remote()}/main").strip() != base:
            batch_move(st, it, "assembling", text=f"batch {it['id']}: main moved during its check: assembled again")
            return f"batch {it['id']}: main moved"
        # A DECIDED MEMBER IS NEVER PUSHED (queue simulator, 2026-11-18, seed 121 of the chaos profile): a member
        # superseded or cancelled while the batch checked dropped out of batch_members, but its commits were still in
        # the assembled tip -- and landed with the others. The tip is assembled again without it.
        gone = [m for m in it["data"].get("members") or [] if m not in {e["id"] for e in members}]
        if gone:
            batch_move(st, it, "assembling", text=f"batch {it['id']}: {', '.join(gone)} left it (decided) during its "
                                                  "check: assembled again without")
            return f"batch {it['id']}: {len(gone)} member(s) gone"
        lock_note(f"push of the batch {tip[:10]}")
        if not os.path.isdir(tree) or not git_ok(tree, "cat-file", "-e", f"{tip}^{{commit}}"):
            batch_move(st, it, "assembling", text=f"batch {it['id']}: its tree is gone: assembled again")
            return f"batch {it['id']}: tree gone"
        git(tree, "checkout", "-q", "--force", "--detach", tip, check=False)
        pushed_at = time.time()
        depth = it["data"].get("depth")
        if depth == "full":  # its full check's run stands for the push, once GitHub confirms it
            run = (it["data"].get("runs") or {}).get(tip)
            env = dict(tool_env(), **({"PREPUSH_REMOTE_RUN": str(run)} if run else {}))
        elif depth == "verify":  # each member fully checked on an older main, the union verified: the red gate only
            skip = hook_query(precheck_hook(tree), tree, "--plan", range_paths(tree, base, tip))
            if skip is None:
                batch_fail(st, d, it, "the hook's plan for its push could not be read", len(members))
                return f"batch {it['id']}: its plan is unreadable"
            env = dict(tool_env(), PREPUSH_SKIP=" ".join(skip))
        elif it["data"].get("kept"):  # pre-checked as one at red, nothing main changed read by it: the red gate only
            skip = hook_query(precheck_hook(tree), tree, "--plan", range_paths(tree, base, tip))
            if skip is None:
                batch_move(st, it, "assembling", {"kept": None, "prechecked": None},
                           text=f"batch {it['id']}: the hook's plan for its push could not be read: checked again")
                return f"batch {it['id']}: its plan is unreadable"
            env = dict(tool_env(), PREPUSH_SKIP=" ".join(skip))
        else:
            env = dict(tool_env(), PREPUSH_REUSE_FAST="1")
        for e in members if depth in DEEP_BATCHES else ():
            if e.get("status") == "checked":
                set_change(d, e, "pushing", event={"text": f"{e['id']}: pushing in batch {it['id']} ({tip[:10]})"})
        rc, out = run_watched(push_argv(remote(), tip), tree, env, os.path.join(d, f"push-{tip[:10]}"), "push",
                              **push_bounds())
        with open(os.path.join(d, "checks.log"), "a") as f:
            f.write(f"=== push {ts()} {tip[:10]} rc {rc}\n{out}\n")
        git(repo, "fetch", "--quiet", remote(), "main", check=False)
        if not landed(repo, tip, f"refs/remotes/{remote()}/main"):
            if rc is not None and REDGATE.search(out or ""):
                batch_move(st, it, "assembling", text=f"batch {it['id']}: the gate went red before the push")
                return f"batch {it['id']}: red"
            if rc is not None and RACE.search(out or ""):
                batch_move(st, it, "assembling", text=f"batch {it['id']}: raced: assembled again")
                return f"batch {it['id']}: raced"
            why = "its push stalled" if rc is None else f"its push failed: {tail_of(out or '', 3)[:160]}"
            batch_fail(st, d, it, why, len(members))
            return f"batch {it['id']}: {why}"
    mark_gate_dirty()
    for e in members:
        kind = "push batch" if it["data"].get("depth") == "verify" else "batch"
        queue_verdict(d, e, "landed", f"LANDED {e['tip']} ({kind} {it['id']}: {len(members)} change(s) in one push)",
                      code=0, landed_sha=e["tip"], pushed_at=pushed_at, batch=len(members), batch_id=it["id"])
        if not lock_held(Owned("await", e["id"]).lockpath):  # a `land --follow` waiting on it watches it itself
            start_watch(repo, e["tip"], pushed_at, worktree=e["worktree"],
                        close=e["worktree"] if os.path.isdir(e["worktree"]) else None, session=e.get("session"))
    batch_move(st, st.item("batch", it["id"]), "landed", text=f"LANDED {tip} ({len(members)} change(s) in one push)")
    wlog(d, f"LANDED {tip} ({len(members)} change(s) in one push)")
    return f"batch {it['id']}: landed {tip[:10]}"


ROUND_ERRORS_MAX = 3


def hold_row(it):
    return {"id": it["id"], "until": it["deadline"], "by": it["data"].get("by"), "why": it["data"].get("why")} if it else None


def land_status_line(r, bt, in_batch, t):
    """`land status`'s line for change row r (change_rows): its state, its KIND when it is a red fix, the batch it is
    in, its hold, and whether it is past its deadline -- no held change is silent (tests/queue_sim.py checks it)."""
    left = int(r["deadline"] - t)
    return (f"  {r['id']}  {r['state']}  {r['worktree']}  {str(r['head'] or '')[:10]}  deadline in {left // 60} min"
            + (f"  [{r['kind']}]" if r.get("kind") else "")
            + (f"  (in batch {bt['id']}: {bt['state']})" if bt and r["id"] in in_batch else "")
            + (f"  held: {r['hold']}" if r.get("hold") else "")
            + (f"  AUTO-RESOLVED in the batch: {r['resolved']}" if r.get("resolved") else "")
            + (f"  PAST ITS DEADLINE (level {r['level']})" if left < 0 else ""))


def land_status_entry(st, entry, t):
    """`land status <entry>`: ONE change -- the item whose id is `entry`, else the one whose id contains it -- open or
    decided: its state, worktree and head, then its verdict (LANDED <sha> / NOT LANDED: <why>) or what it waits on.
    A VERDICT IS DURABLE (queueuntil.py): an agent whose wait died reads it here after the fact. 1 when none or several
    match."""
    items = st.items("change")
    hits = [it for it in items if it["id"] == entry] or [it for it in items if entry in it["id"]]
    if len(hits) != 1:
        print(f"no single change matches {entry!r}" + (f": {', '.join(it['id'] for it in hits[:8])}" if hits else "")
              + " (`agentctl land status` lists the open ones)")
        return 1
    it = hits[0]
    d = it.get("data") or {}
    print(f"{it['id']}  {it['state']}  {d.get('worktree')}  {str(d.get('head') or '')[:10]}")
    if it["state"] == "landed":
        print(f"LANDED {d.get('landed_sha') or landed_sha_of(it['id'], d)} (queued {it['id']})")
    elif machine_state("change", it["state"]).terminal:
        why = str(d.get("why") or f"{it['id']} {it['state']}")
        print(why if why.startswith(("NOT LANDED", "SUPERSEDED")) else f"NOT LANDED: {why}")
    else:
        hold = (d.get("hold") or {}).get("why") if it["state"] in HOLD_SHOWN else None
        print(f"OPEN: {it['state']} for {fmt_age(t - float(it.get('entered_at') or t))}"
              + (f", held: {hold}" if hold else "") + f"; deadline in {int((it['deadline'] - t) // 60)} min")
        if it["state"] == "checking":
            for line in remote_check_lines(d.get("check"), t) or [
                    "check: no remote check recorded (a local check tree, or its executor is starting)"]:
                print(line)
    for r in d.get("rechecks") or []:  # MINIMAL RECHECKS (queuerecheck.py): why each round after the first ran, or did not
        print(f"recheck: {hhmmz(r.get('at') or 0)} {r.get('kind')} on {r.get('main') or '?'}: {r.get('why')}")
    return 0


def write_snapshot(d, store=None):
    """The tick's part of state.json: the open items and the tick's own time and code version."""
    st = store or open_store()
    open_ = lambda typ: [i for i in st.items(typ) if typ in MACHINES and not machine_state(typ, i["state"]).terminal]
    brief = lambda i: {"id": i["id"], "state": i["state"], "deadline": i["deadline"], "level": i["level"],
                       "worktree": (i["data"] or {}).get("worktree"),
                       **({"hold": ((i["data"] or {}).get("hold") or {}).get("why")}
                          if i["state"] in HOLD_SHOWN and (i["data"] or {}).get("hold") else {})}
    rows = change_rows(st)  # ONE QUEUE: the store's items, never a host's entry files
    state_update(tick_at=time.time(), version=code_version(), rev=start_rev(),
                 changes=[brief(i) for i in open_("change")], batch=[brief(i) for i in open_("batch")],
                 reds=[brief(i) for i in open_("red")] if "red" in MACHINES else [],
                 conditions=[brief(i) for i in open_("condition")] if "condition" in MACHINES else [],
                 reverts=[revert_brief(i) for i in open_("revert")] if "revert" in MACHINES else [],
                 asks=owner_asks(st) if "red" in MACHINES else [],  # THE OWNER'S PENDING DECISIONS (ask.py)
                 hold=hold_row(land_hold(st)),
                 waiting=[r["worktree"] for r in rows if r["state"] == "waiting"],
                 queued=[r["worktree"] for r in rows if r["state"] != "waiting"])


NETWORK_SHOWN_FOR = 600  # a lossy-uplink reading this recent turns the status line's UNREAD into NETWORK


STATUS_LIVE_AFTER = 30   # s: a mirror of ci-runner's /v1/state older than this is read live by the status line
STORE_UNREACHABLE_AFTER = 60  # s without a good read of ci-runner's /v1/state: the line says STORE UNREACHABLE


def note_store_error(e, t=None):
    """The remote store did not answer: kept in state.json (since when, why) for the status line."""
    t = t if t is not None else time.time()
    prev = state_read().get("store_error") or {}
    state_update(store_error={"since": prev.get("since") or t, "at": t, "why": str(e).replace("agentctl: ", "")[:200]})


def remote_status_state(url, t):
    """THE STATUS LINE IN REMOTE MODE (2026-10-29 cutover: the line said RECONCILER STALLED since 13:01 while ci-runner's
    reconciler ticked fine -- the laptop's state.json is only a mirror, and the mirror had stopped). state.json as the
    sync mirrored it, read live from GET /v1/state when the mirror is over STATUS_LIVE_AFTER s old (at most once per
    STATUS_LIVE_AFTER s while the store does not answer); a failed read is recorded as store_error."""
    d = state_read()
    last_try = max(float(d.get("fetched_at") or 0), float((d.get("store_error") or {}).get("at") or 0))
    if t - last_try > STATUS_LIVE_AFTER:
        try:
            mirror_state(RemoteStore(url, timeout=5, retries=0), t)  # the status line never waits out a retry
        except (Exception, SystemExit) as e:
            note_store_error(e, t)
        d = state_read()
    return d


TICK = 5  # seconds between ticks ($AGENTCTL_TICK); every step is bounded or rate-limited inside


def waiting_run(repo, once=False, service=False):
    """THE processor: ONE loop of ticks (design §3). On ci-runner (server_steps): the change items (executors,
    pushes), the batch, the watches, the gate line, the red tick, the recovery registry. On the laptop (sync_steps):
    the outbox, the state mirror, events -> inbox and the notifier, claims, the fixer dispatcher, the network and
    tunnel watches, upkeep. Nothing in it blocks for long (checks run in executors with leases); it re-execs
    onto agentctl's new code between ticks; the external watchdog (W5) watches its beat."""
    d, _c, _wt = waiting_state(repo)
    own = processor_owned(d, repo)
    if not own.acquire():
        return 0
    argv = ["waiting", "run", "--repo", repo] + (["--once"] if once else []) + (["--service"] if service else [])
    with contextlib.ExitStack() as stack:
        stack.callback(own.release)
        try:
            o_r = slug(repo)
        except (Exception, SystemExit):
            o_r = SHORT["movies"]
        _IN_PROCESSOR[0] = True
        drop = TokenDrop(d, o_r).open()
        stack.callback(drop.close)
        errors, stalled_noted = 0, False
        role = host_role()
        if service:
            keep_awake()  # a plugged-in laptop never sleeps under the service (no-op off macOS)
        if service and role != "server":
            status_page_start()  # the read-only status page, a daemon thread (see "the status page"); never fatal
        def shared(fn):  # a step that uses the clone holds it shared: a repair's swap waits for it
            def run():
                with clone_shared(repo):
                    return fn()
            return run
        steps = sync_steps(repo, d, o_r) if role == "sync" else server_steps(repo, d, shared)
        while True:
            if TICK_STOP[0]:  # SIGTERM (systemd's stop): between ticks, never inside one
                return 0
            write_beat("processor")
            if not any(t.is_alive() for k, t in _bounded.items() if k.startswith("step:")):
                restart_if_changed([own], argv)  # never under a step still at work in its thread (a push, say)
            failed = False
            for name, step in steps:
                failed = run_step(name, step) is False or failed
                write_beat("processor")
                sd_notify("WATCHDOG=1")  # each bounded step is progress: systemd's WatchdogSec never cuts a long tick
            errors = errors + 1 if failed else 0
            if errors == 0:
                stalled_noted = False
            if errors >= ROUND_ERRORS_MAX and not stalled_noted:
                # NEVER FATAL (2026-10-29 22:31: the network dropped, every step failed, and this condition's own store
                # call -- the store is remote on the laptop -- raised StoreUnavailable out of the loop: the processor
                # died, launchd restarted it, and the restart holds no token until the owner hands it over).
                # Not noted now: tried again next round.
                try:
                    open_condition("STALLED", f"STALLED: the waiting processor's ticks failed {errors} times in a row; "
                                              f"see {d}/processor.log and service.log",
                                   run_probe("waiting_ok", {"d": d, "since": time.time()}), [], key="waiting-rounds",
                                   command="waiting")
                    stalled_noted = True
                except (Exception, SystemExit) as e:
                    err(f"STALLED could not be noted ({str(e)[:160]}); the loop goes on")
            if not failed:
                write_json(os.path.join(d, "round-ok.json"), {"at": time.time()})
            if role != "sync":  # the sync's state.json is ci-runner's, mirrored
                try:
                    write_snapshot(d)
                except Exception as e:
                    err(f"the snapshot could not be written: {e}")
            sd_notify("WATCHDOG=1")
            if once and (role == "sync" or (not queued_entries(d) and not waiting_entries(d))):
                return 0
            drop.nap(env_num("AGENTCTL_TICK", TICK))


TICK_STOP = [False]  # set by SIGTERM in `agentctl tick`: the loop returns between ticks

# EVERY STEP OF THE TICK IS BOUNDED (2026-10-29, RECONCILER STALLED 05:37-05:41: during a tunnel hang the gate's
# forge reads -- gh calls of up to 90 s each, one kickstart and a retry, ten workflows -- held the loop for minutes,
# and state.json's tick_at aged past 2 min). A step runs in a thread of its own and the tick waits at most STEP_JOIN s
# for it; one still running is not started again until it ends (run_bounded), and the tick goes on to the next step.
# Its error, when it ends, is reported at the next look. The steps' writes are store transitions (compare-and-set) and
# atomic entry files, so a slow step overlapping the next tick's other steps is safe.
STEP_JOIN = 20
_step_errors = process_cache({})


def run_step(name, step):
    """One tick step, bounded (see above). True: done; False: it failed (now or, still running before, since);
    None: still running in its thread."""
    def work():
        try:
            step()
            _step_errors[name] = None
        except NoToken:  # token_missing said it, on its transition: a step never repeats it every tick
            _step_errors[name] = STEP_NO_TOKEN
        except (Exception, SystemExit) as e:  # one step's error is that step's, never the loop's (audit #9)
            _step_errors[name] = str(e).replace("agentctl: ", "")[:200]
    t = _bounded.get(f"step:{name}")
    if t and t.is_alive():
        return None
    if _step_errors.get(name):  # it failed after the tick stopped waiting for it
        step_failed(name, _step_errors.pop(name))
    if not run_bounded(f"step:{name}", work, env_num("AGENTCTL_STEP_JOIN", STEP_JOIN)):
        err(f"{name}: still running after {env_num('AGENTCTL_STEP_JOIN', STEP_JOIN):.0f} s; the tick goes on (it ends "
            "in its own thread)")
        return None
    why = _step_errors.pop(name, None)
    if why:
        step_failed(name, why)
        return False
    return True


STEP_NO_TOKEN = "no token"


def step_failed(name, why):
    if why is not STEP_NO_TOKEN:
        err(f"{name} failed: {why}")

SERVICE_LABEL = "net.bitcashier.agentctl-waiting"


def launch_agents_dir():
    return os.environ.get("AGENTCTL_LAUNCH_AGENTS") or os.path.join(HOME, "Library", "LaunchAgents")


def launchctl(*args):
    return run_cmd([os.environ.get("AGENTCTL_LAUNCHCTL", "launchctl")] + list(args))


def service_plist(repo, log):
    from xml.sax.saxutils import escape
    argv = [sys.executable, TOOL_PATH, "waiting", "run", "--service", "--repo", repo]
    args = "".join(f"\n        <string>{escape(x)}</string>" for x in argv)
    # phase 2: installed with $AGENTCTL_STORE_URL set, the agent is the laptop's SYNC against ci-runner's store
    store_env = (f"\n        <key>AGENTCTL_STORE_URL</key>\n        <string>{escape(os.environ['AGENTCTL_STORE_URL'])}</string>"
                 if os.environ.get("AGENTCTL_STORE_URL") else "")
    path_env = ":".join(SDKMAN_BINS + ["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"])
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{SERVICE_LABEL}</string>
    <key>ProgramArguments</key>
    <array>{args}
    </array>
    <key>WorkingDirectory</key>
    <string>{escape(repo)}</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>ThrottleInterval</key>
    <integer>30</integer>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>{escape(path_env)}</string>
        <key>HOME</key>
        <string>{escape(HOME)}</string>{store_env}
    </dict>
    <key>StandardOutPath</key>
    <string>{escape(log)}</string>
    <key>StandardErrorPath</key>
    <string>{escape(log)}</string>
</dict>
</plist>
"""


def waiting_service(action, repo):
    """The `waiting` processor as a launchd agent (KeepAlive, RunAtLoad): `agentctl waiting run --service` for the clone,
    its log in the waiting state dir. install writes the plist and bootstraps it; uninstall boots it out and removes it."""
    d, clone, _wt = waiting_state(repo)
    plist = os.path.join(launch_agents_dir(), f"{SERVICE_LABEL}.plist")
    domain = f"gui/{os.getuid()}"
    log = os.path.join(d, "service.log")
    if action == "install":
        os.makedirs(launch_agents_dir(), exist_ok=True)
        with open(plist, "w") as f:
            f.write(service_plist(clone, log))
        launchctl("bootout", f"{domain}/{SERVICE_LABEL}")  # a previous one, if any
        r = launchctl("bootstrap", domain, plist)
        if r.returncode != 0:
            print(f"wrote {plist}, but launchctl bootstrap failed: {r.stderr.strip() or r.stdout.strip()}")
            return 1
        print(f"installed {SERVICE_LABEL}: `agentctl waiting run --service --repo {clone}`, log {log}")
        return 0
    if action == "uninstall":
        launchctl("bootout", f"{domain}/{SERVICE_LABEL}")
        try:
            os.unlink(plist)
        except FileNotFoundError:
            pass
        print(f"uninstalled {SERVICE_LABEL}")
        return 0
    if action == "status":
        r = launchctl("print", f"{domain}/{SERVICE_LABEL}")
        if r.returncode != 0:
            print(f"{SERVICE_LABEL}: not loaded" + (f" (plist {plist} present)" if os.path.exists(plist) else ""))
            return 1
        state = re.search(r"^\s*state = (\S+)", r.stdout, re.M)
        pid = re.search(r"^\s*pid = (\d+)", r.stdout, re.M)
        print(f"{SERVICE_LABEL}: {state.group(1) if state else 'loaded'}" + (f", pid {pid.group(1)}" if pid else "")
              + f"; processor {'running' if waiting_processor_alive(d) else 'not running'}; log {log}")
        return 0
    return 2


def cmd_waiting(a):
    if a.wcmd in WAITING_RENAMED and not getattr(a, "renamed", False) and \
            (a.wcmd not in ("run", "service") or sys.stdin.isatty()):
        err(f"`agentctl waiting {a.wcmd}` is now `agentctl {WAITING_RENAMED[a.wcmd]}` (the old name answers until "
            f"{ALIASES_UNTIL})")
    here = a.repo or safe_cwd()
    if a.wcmd == "await":
        repo = main_clone(here)
    elif git_ok(here, "rev-parse", "--show-toplevel"):
        repo = git(here, "rev-parse", "--show-toplevel").strip()
    else:  # outside any git checkout (e.g. ~/bitcashier): the movies clone's queue, as `await` resolves it
        repo = main_clone(movies_repo_path())
    d, _c, wt = waiting_state(repo)
    if a.wcmd == "run":
        return waiting_run(repo, once=a.once, service=getattr(a, "service", False))
    if a.wcmd == "check":  # one queued change's checker (the processor starts it)
        return check_entry(main_clone(repo), a.entry)
    if a.wcmd == "check-batch":  # the batch's executor (the processor starts it)
        return check_batch(main_clone(repo), a.entry)
    if a.wcmd == "service":
        return waiting_service(a.action, repo)
    if a.wcmd == "await":  # land's wait on its entry, resumed after a restart on new code
        a.inbox_cwd = a.close or a.repo
        repo = main_clone(repo)
        os.chdir(repo)
        rc, sha, pushed_at = await_entry(repo, a.entry)
        return watch_landed(repo, sha, pushed_at, close=a.close) if rc == 0 and sha else rc
    if a.wcmd == "pause":  # `land hold`: an item in the store (the reconciler's, wherever this runs)
        st = open_store()
        secs = int(float(getattr(a, "hold_for", None) or LAND_HOLD_DEFAULT // 60) * 60)
        it = land_hold_set(st, secs, getattr(a, "hold_why", None), hold_by())
        print(f"held: the processor takes no batch until `agentctl land release` or "
              f"{time.strftime('%H:%M', time.localtime(it['deadline']))} (then the hold expires); changes still queue")
        return 0
    if a.wcmd == "resume":
        st = open_store()
        gone = land_hold_release(st, hold_by())
        try:
            os.unlink(os.path.join(d, "PAUSED"))  # the pre-store hold file, if an old version left one
        except FileNotFoundError:
            pass
        recover_clear_hold(None, {"d": d})  # a held batch is assembled again at once
        spawn_waiting_processor(repo)
        offer_token(repo)
        print("released" + (f" ({', '.join(gone)})" if gone else " (no hold was in force)")
              + ": the processor takes the next batch")
        return 0
    if a.wcmd == "status" and getattr(a, "entry", None):  # one change, open or decided (A VERDICT IS DURABLE)
        return land_status_entry(open_store(), a.entry, time.time())
    if a.wcmd == "status":  # ONE QUEUE: the store's change items, on either host
        st = open_store()
        rows = change_rows(st)
        print(f"queue: {store_url() or 'this host'}")
        print(f"processor: {'running' if waiting_processor_alive(d) else 'not running'}")
        hold = land_hold(st)
        if hold:
            print(f"land: {hold_text(hold)}")
        bt = status_batch(st, d)
        if bt:
            dd = bt["data"]
            print(f"batch {bt['id']}: {bt['state']}" + (f", {len(dd.get('members') or [])} change(s) at "
                  f"{str(dd.get('tip'))[:10]} on {str(dd.get('base'))[:10]}" if dd.get("tip") else "")
                  + f", deadline in {int((bt['deadline'] - time.time()) // 60)} min")
        offer_token(repo)
        reds = gate_reds(repo)
        print("gate: " + (f"UNREAD, taken as red ({gate_unread_why[0]})" if GATE_UNREAD in reds else
                          f"RED ({', '.join(sorted(reds))})" if reds else "green"))
        print(f"changes: {len(rows)}")
        in_batch = batch_row_ids(bt, rows)
        for r in rows:
            print(land_status_line(r, bt, in_batch, time.time()))
        prog = read_json_file(batch_progress_path(d)) if host_role() == "server" else {}
        if prog.get("ticks"):
            print(f"stall at green: {prog['ticks']} tick(s): {prog.get('why')}")
        if not rows:
            print("  (no change queued or waiting)")
        local = [e for e in waiting_entries(d, open_only=False) if e.get("status") not in ENTRY_FINAL]
        if host_role() == "sync" and local:
            print(f"LOCAL QUEUE: {len(local)} entr(ies) in {d}, which nothing runs; the sync moves them to ci-runner's "
                  f"queue: {', '.join(e['id'] for e in local)}")
        return 0
    if a.wcmd == "cancel" and host_role() == "sync":  # ONE QUEUE: ci-runner's, through the store API
        hits = [r["id"] for r in change_rows() if a.entry in r["id"]]
        if len(hits) != 1:
            print(f"no single open change matches {a.entry!r}: `agentctl land status`")
            return 1
        open_store().cancel_change(hits[0])
        print(f"cancelled {hits[0]}")
        return 0
    if a.wcmd == "cancel":
        queued = [e for e in queued_entries(d) if a.entry in e["id"]]
        if len(queued) == 1:
            e = queued[0]
            set_change(d, e, "cancelled", event={"text": f"{e['id']}: cancelled"},
                       why="cancelled by `agentctl land cancel`", decided=ts())
            cancel_entry_remote(e, "its queue entry was cancelled")
            h = read_record(entry_owned(e["id"]).record) or {}
            if lock_held(entry_owned(e["id"]).lockpath) and h.get("pid"):
                os.kill(int(h["pid"]), signal.SIGTERM)  # its checker: its CI run is cancelled with it
            print(f"cancelled {e['id']}")
            return 0
        hits = [e for e in waiting_entries(d) if a.entry in e["id"]]
        if not hits:  # an item no entry carries (stranded): the item alone
            ids = [r["id"] for r in change_rows() if a.entry in r["id"]]
            if len(ids) == 1:
                cancel_submitted(ids[0], repo=repo)
                print(f"cancelled {ids[0]}")
                return 0
        if len(hits) != 1:
            print(f"no single waiting change matches {a.entry!r}: `agentctl land status`")
            return 1
        e = hits[0]
        set_change(d, e, "cancelled", event={"text": f"{e['id']}: cancelled"}, why="cancelled by `agentctl land cancel`",
                   decided=ts())  # the next batch is assembled without it
        print(f"cancelled {e['id']}")
        return 0
    return 2
