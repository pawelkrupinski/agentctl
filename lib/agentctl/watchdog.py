# agentctl, part `watchdog`: the invariant watchdog and orphaned work.
# Runs inside agentctl's one namespace, after the parts before it in lib/agentctl/__init__.py's PARTS (it uses their names).

# ---------------------------------------------------------------- the invariant watchdog (design §10, phase 1 step 2)
# A separate, cheap, dumb launchd job (net.bitcashier.agentctl-watchdog, every 60 s): `agentctl watchdog` reads today's
# files only -- the queue entries, the published gate line, the processor's beat file, the inbox -- and checks the
# invariants the reconciler must converge to. A violation is a condition (INVARIANT, its probe the invariant itself:
# it closes when the invariant holds again) and a macOS notification; on W5 (the processor's duties stopped) it also
# kickstarts the processor, at most once per WATCHDOG_KICK_GAP. It shares no code path with the processor beyond the
# registry and the read-only readers.
W1_MAX = 2 * 3600       # the oldest unlanded change ($AGENTCTL_W1_MAX); while main is red, counted from the red's start
W2_MAX = 10 * 60        # a gate red with nobody on it
W5_MAX = 300            # the processor's duties beat (its bounded steps take up to 2 min)
W6_MAX = 5 * 60         # a due escalation not notified
W7_MAX = 5 * 60         # phase 2: an event ci-runner wrote that the laptop's sync has not projected (60x its 5 s tick)
WM_MAX = 45 * 60        # maintain's last pass (launchd every 15 min)
WATCHDOG_KICK_GAP = 600


def beat_path(name):
    return state_root(f"{name}.beat")


def write_beat(name):
    try:
        os.makedirs(state_root(), exist_ok=True)
        with open(beat_path(name), "w") as f:
            f.write(f"{time.time():.0f} {os.getpid()}\n")
    except OSError:
        pass


def beat_age(name):
    try:
        return time.time() - float(open(beat_path(name)).read().split()[0])
    except (OSError, ValueError, IndexError):
        return None


def watchdog_state():
    return read_record(state_root("watchdog.json")) or {}


def inv_w1():
    """W1: the oldest non-terminal change is younger than W1_MAX (while main is red: past the red's own start)."""
    d, _c, _w = waiting_state(movies_repo_path())
    open_ = [e for e in waiting_entries(d, open_only=False) if e.get("status") in ("queued", "checking", "waiting")]
    if not open_:
        return True, "no change waits"
    oldest = min(open_, key=lambda e: e.get("created") or "")
    age = time.time() - parse_time(oldest.get("created") or ts()).timestamp()
    line, _e = cached_gate_summary(SHORT["movies"])
    red_since = 0.0
    if line and line.startswith("gate RED"):
        snap = ci_state_load(SHORT["movies"]) or {}
        starts = [parse_time(g["since"]).timestamp() for g in snap.get("gate") or [] if g.get("since")]
        red_since = (time.time() - min(starts)) if starts else 0.0
    lim = env_num("AGENTCTL_W1_MAX", W1_MAX)
    ok = age < lim or (red_since and age < red_since + lim)
    return ok, f"oldest unlanded change {oldest['id']} ({oldest.get('status')}) is {fmt_age(age)} old" + \
        (f", main red for {fmt_age(red_since)}" if red_since else "")


def red_fix_held(it, t, facts=None):
    """A fix-pending red's holder, or None: its fix change open in the queue, or a fix landed / a verify run dispatched
    (or a gate run due) within RED_FIX_GIVEUP of entering fix-pending. With facts the queue decides; without (the
    watchdog), the fix the item recorded."""
    d = it["data"]
    f = d.get("fix") or {}
    if facts is not None:
        fx = red_fix_of(it, facts)
        if fx and fx["status"] in FIX_OPEN:
            return f"fix {fx.get('entry')} {fx['status']}"
        if fx is None and f.get("status") != "verify":
            return None
    elif f.get("status") in FIX_OPEN and f.get("entry"):
        return f"fix {f.get('entry')} {f.get('status')}"
    if not f or (not f.get("entry") and not f.get("sha") and f.get("status") != "verify"):
        return None  # fix None: nothing is on its way
    if f.get("status") == "verify" and str(f.get("verify") or "").startswith("FIX UNVERIFIED:"):
        return None  # its verifying run was refused: none will come
    if t - red_fix_since(it) >= env_num("AGENTCTL_RED_FIX_GIVEUP", RED_FIX_GIVEUP):
        return None
    return f"fix {f.get('entry') or (f.get('sha') or '')[:10] or 'verify'} {f.get('status')}"


# A HOLDER IS ONLY A LIVE LEASE (2026-10-30: Main #2647 sat offered for hours, its offer saying "its previous fixer pid
# 20496 ... is still at work on it: it stays the holder" -- a dead fixer of another, closed red, and no lease anywhere).
# An open offer whose reason names a holder, while the red has no live lease, is a violation: its reason is cleared
# and the dispatcher takes it again.
HOLDER_CLAIM = re.compile(r"stays the holder|still at work on it|is its holder", re.I)


def offer_names_holder(off):
    return bool(off and HOLDER_CLAIM.search(str(off.get("reason") or "")))


def red_unheld(store, t=None, facts=None):
    """The non-terminal reds with NOTHING live behind them: no live lease, no open offer, no fix on its way (red_fix_held),
    no live block. The tick's invariant (never NOBODY), enforced by red_enforce; a red the tick has not looked at yet
    (state `red`) counts after 2 min."""
    t = t if t is not None else store.clock()
    out = []
    for it in red_active(store).values():
        st = it["state"]
        if st == "red" and t - it["entered_at"] < 120:
            continue
        if lease_is_live(store.get(red_lease(it["data"]["key"])), t):
            continue
        if st == "fix-pending" and red_fix_held(it, t, facts):
            continue
        if st == "blocked-external" and red_block_of(it, store):
            continue
        if st == "offered" and (off := store.offer_of(f"red:{it['id']}")) and not offer_names_holder(off):
            continue
        out.append(it)
    return out


def red_enforce(store, facts, t):
    """THE INVARIANT, SELF-RECOVERING (the owner, 2026-10-29): a red with no live holder (red_unheld) is offered again,
    escalated, its dead fix set aside -- the dispatcher, next in the same tick, gives it a fixer. Runs after every red's
    own step, so it only catches what the step's own transitions left resting."""
    acts = []
    for it in red_unheld(store, t, facts):
        it = store.item("red", it["id"])
        if not it or it["state"] in RED_TERMINAL:
            continue
        d, st = it["data"], it["state"]
        try:
            if st == "offered":
                off = store.offer_of(f"red:{it['id']}")
                if offer_names_holder(off):  # a stated holder with no live lease: no holder at all
                    store.offer_reason(off["id"], None)
                    acts.append(f"INVARIANT: {red_what(d)}: its offer named a holder with no live lease "
                                f"({str(off.get('reason'))[:120]}): offered again")
                else:
                    red_put_offer(store, it)  # an offered red always has its open offer
                    acts.append(f"INVARIANT: {red_what(d)}: its offer reopened")
            elif st == "fix-pending":
                acts += red_fix_unverifiable(store, it, f"INVARIANT: no fix on its way for "
                                                        f"{fmt_age(t - it['entered_at'])} in fix-pending", t)
            elif st == "leased":
                acts += red_lease_ended(store, it, store.get(red_lease(d["key"])), red_block_of(it, store), t)
            else:
                acts.append(f"INVARIANT: {red_what(d)}: {st} with no holder: offered")
                red_offer(store, it, f"INVARIANT: {red_what(d)}: {st} with no holder for "
                                     f"{fmt_age(t - it['entered_at'])}: offered", escalate=st != "red",
                          data={"cause": None, "block": None} if st == "blocked-external" else None)
        except StaleVersion:
            pass
    return acts


def inv_w2():
    """W2: every red has a live lease, an open offer, a fix on its way or an owner block -- for no red is that missing
    for W2_MAX (read from the store)."""
    try:
        bad = red_unheld(Store())
    except Exception as e:
        return None, f"cannot read the store ({str(e)[:120]})"
    st = watchdog_state()
    first = st.get("w2_since")
    now_ = time.time()
    if not bad:
        if first:
            write_json(state_root("watchdog.json"), {**st, "w2_since": None})
        return True, "every red is held or offered"
    if not first:
        first = now_
        write_json(state_root("watchdog.json"), {**st, "w2_since": first})
    it = bad[0]
    return now_ - first < env_num("AGENTCTL_W2_MAX", W2_MAX), \
        f"{red_what(it['data'])} ({it['state']}) has no lease, offer, fix or block for {fmt_age(now_ - first)}"


def inv_w5():
    """W5: the processor's duties beat within W5_MAX (only where the processor service is installed)."""
    if not os.path.exists(os.path.join(launch_agents_dir(), f"{SERVICE_LABEL}.plist")):
        return True, "no processor service here"
    age = beat_age("processor")
    if age is None:
        return None, "no processor beat yet (a processor from before the watchdog)"
    return age < env_num("AGENTCTL_W5_MAX", W5_MAX), f"the processor's duties beat {int(age)} s ago"


def inv_w6():
    """W6: no escalation event left unnotified for over W6_MAX (the notifier's delivery, from the store)."""
    try:
        st = Store()
        t = st.clock()
        late = [e for e in st.events(after=max(0, int(st.meta_get("notifier_seq", 0) or 0) - 500), limit=5000)
                if e["severity"] == "escalate" and not e.get("notified_at") and t - e["at"] > env_num("AGENTCTL_W6_MAX", W6_MAX)]
    except Exception as e:
        return None, f"cannot read the store ({str(e)[:120]})"
    return not late, (f"{len(late)} escalation(s) not notified: {str(late[0].get('text'))[:100]}" if late
                      else "every escalation was notified")


def inv_w7():
    """W7 (phase 2, the sync role only): the laptop's sync keeps up with ci-runner's events -- no event older than
    W7_MAX is still past its projector cursor (meta projector_seq). The store not answering is no verdict (the tunnel
    and ci-runner have their own conditions). Raised and cleared by this same check, as an INVARIANT condition."""
    if host_role() != "sync":
        return True, "no sync here (the server)"
    try:
        st = Store()
        behind = st.events(after=int(st.meta_get("projector_seq", 0) or 0), limit=1)
    except Exception as e:
        return None, f"cannot read the store ({str(e)[:120]})"
    if not behind:
        return True, "the sync is up to date with the event feed"
    age = time.time() - float(behind[0]["at"])
    return age <= env_num("AGENTCTL_W7_MAX", W7_MAX), \
        f"the sync is behind ci-runner's events: #{behind[0]['seq']} unprojected for {fmt_age(age)}"


def inv_wm():
    """WM: maintain (the reaper; the watchdog's own watcher) ran within WM_MAX."""
    if not os.path.exists(os.path.join(launch_agents_dir(), "net.bitcashier.agentctl-maintain.plist")):
        return True, "no maintain service here"
    age = beat_age("maintain")
    if age is None:
        return None, "no maintain beat yet"
    return age < env_num("AGENTCTL_WM_MAX", WM_MAX), \
        f"maintain last ran {fmt_age(age) + ' ago' if age is not None else 'never'}"


WB_MAX = 10 * 60


def inv_wb():
    """WB: ~/bin's own checkout carries no uncommitted edit older than WB_MAX (work goes in worktrees: an edit there
    blocks every other agent's merge). Names the processes working in it."""
    br = bin_repo()
    if not br:
        return True, "no agentctl repository here"
    files = [l[3:] for l in git(br, "status", "--porcelain", "--untracked-files=no", check=False).stdout.splitlines()
             if l.strip() and not l[3:].startswith("briefs/")]
    if not files:
        return True, f"{br} is clean"
    newest = max((os.path.getmtime(os.path.join(br, f)) for f in files if os.path.exists(os.path.join(br, f))), default=0)
    if time.time() - newest < env_num("AGENTCTL_WB_MAX", WB_MAX):
        return True, f"{br}: {len(files)} file(s) edited just now"
    try:
        cwds = process_cwds()
        names = {pid: cmd for pid, _pp, _e, cmd in ps()}
        who = [f"pid {p} {names.get(p, '')[:60]}" for p, c in cwds.items()
               if os.path.realpath(c) == os.path.realpath(br) or os.path.realpath(c).startswith(os.path.realpath(br) + "/")]
    except (Exception, SystemExit):
        who = []
    return False, (f"{br} has uncommitted edits ({', '.join(files[:4])}) unchanged for {fmt_age(time.time() - newest)}"
                   + (f"; working there: {'; '.join(who[:3])}" if who else "; nobody works there now") +
                   ": commit them in a worktree of ~/bin")


INVARIANTS = {"WB": inv_wb, "W1": inv_w1, "W2": inv_w2, "W5": inv_w5, "W6": inv_w6, "W7": inv_w7, "WM": inv_wm}


def probe_invariant(a):
    return INVARIANTS[a["id"]]()


def cmd_watchdog(a):
    """One watchdog pass: every invariant checked; a violation opens its INVARIANT condition (and notifies), a restored
    one closes it; W5 kickstarts the processor. Prints one line per invariant."""
    write_beat("watchdog")
    st = watchdog_state()
    rc = 0
    for wid in INVARIANTS:
        r = run_probe("invariant", {"id": wid})
        key = f"invariant-{wid}"
        cond = next((c for c in open_conditions() if c["key"] == key), None)
        print(f"{wid}: {'ok' if r.ok else 'cannot tell' if r.ok is None else 'VIOLATED'} -- {r.detail}")
        if r.ok is False:
            rc = 1
            if not cond:
                open_condition("INVARIANT", f"INVARIANT {wid} violated: {r.detail}", r, [], key=key, command="watchdog")
                notify(f"agentctl watchdog: {wid}", r.detail[:220])
            # a kickstart costs the processor its token (handed over again by the next interactive agentctl): only for a
            # processor silent past 3 x W5_MAX, at most every WATCHDOG_KICK_GAP
            if wid == "W5" and (beat_age("processor") or 0) > 3 * env_num("AGENTCTL_W5_MAX", W5_MAX) \
                    and time.time() - float(st.get("kicked_at") or 0) >= WATCHDOG_KICK_GAP:
                launchctl("kickstart", "-k", f"gui/{os.getuid()}/{SERVICE_LABEL}")
                st["kicked_at"] = time.time()
                write_json(state_root("watchdog.json"), {**watchdog_state(), "kicked_at": st["kicked_at"]})
                print(f"W5: kickstarted {SERVICE_LABEL}")
        elif r.ok and cond:
            close_condition(cond, r)
    return rc


def cmd_maintain(a):
    """THE reaper (the owner, 2026-10-27: one service, not three): launchd net.bitcashier.agentctl-maintain runs it every
    15 min. Each pass: the reap (`orphans --kill --safe`: dead owners, stray wait loops, stuck sbt servers, stale JVMs);
    once a day, at the first pass from 09:00 local, the sweep. --dry-run lists and kills nothing."""
    if hasattr(sys.stdout, "reconfigure"):  # its log shows each step as it happens
        sys.stdout.reconfigure(line_buffering=True)
    print(f"== agentctl maintain {ts()}{' (DRY RUN)' if a.dry_run else ''}")
    if not a.dry_run:
        write_beat("maintain")
        wd = beat_age("watchdog")  # the watchdog's watcher (design §15): it must have run within 5 min
        if os.path.exists(os.path.join(launch_agents_dir(), "net.bitcashier.agentctl-watchdog.plist")) and \
                (wd is None or wd > 300):
            notify("agentctl: the watchdog is not running", f"last ran {fmt_age(wd) + ' ago' if wd else 'never'}")
            print(f"== the watchdog last ran {fmt_age(wd) + ' ago' if wd else 'never'}: notified")
    print("== reap")
    cmd_orphans(argparse.Namespace(kill=not a.dry_run, safe=True))
    path = state_root("maintain.json")
    st = read_record(path) or {}
    today, hour = dt.date.today().isoformat(), dt.datetime.now().hour
    if a.sweep or (st.get("sweep") != today and hour >= int(env_num("AGENTCTL_SWEEP_HOUR", MAINTAIN_SWEEP_HOUR))):
        print("== sweep (daily)")
        rc = cmd_sweep(argparse.Namespace(repo=None, idle_days=SWEEP_IDLE_DAYS, dry_run=a.dry_run, no_report=a.dry_run))
        if not a.dry_run:
            write_json(path, {**st, "sweep": today, "sweep_rc": rc, "sweep_at": ts()})
    else:
        print(f"== sweep: done today ({st.get('sweep_at')})" if st.get("sweep") == today else
              f"== sweep: from {MAINTAIN_SWEEP_HOUR:02d}:00")
    print("== orphaned work")
    try:
        for line in orphan_duty(dry_run=a.dry_run) or ["  none"]:
            print(f"  {line}")
    except (Exception, SystemExit) as e:
        print(f"  could not look ({str(e)[:160]})")
    print("== cache expiry")
    try:
        gone = expire_cache(dry_run=a.dry_run)
        print(f"  {'would remove' if a.dry_run else 'removed'} {len(gone)} cache entr{'y' if len(gone) == 1 else 'ies'} older "
              f"than {env_num('AGENTCTL_CACHE_EXPIRE_DAYS', CACHE_EXPIRE_DAYS):g} days")
    except (Exception, SystemExit) as e:
        print(f"  could not expire ({str(e)[:160]})")
    return 0


# CACHE EXPIRY (docs/simplification-2026-10.md, Agent B item 3): the state root collects a log per land (and its
# .checks.out / .push.out / .baseline.out), the phase-2 apply logs, sbt thread dumps, a dir per red run (its fixer
# records and claude logs), the baseline caches and the internal-tree tags. Each maintain pass removes what is older
# than CACHE_EXPIRE_DAYS: a file by its mtime, a red run's dir only when all of it is that old and none of its fixers is
# still running, a tag only when its worktree is gone. Nothing outside the state root, nothing a protected-folder
# check refuses (allowed_path: never ~/Desktop and the like), no symlink is followed.
CACHE_EXPIRE_DAYS = 7
CACHE_EXPIRE_FILES = (("", ("agentctl-land-*", "phase2-apply-*.log")), ("sbt-dumps", ("*",)),
                      (os.path.join("reds", "baseline"), ("*.json",)), (os.path.join("reds", "baseline-out"), ("*",)))


def _newest_mtime(path):
    newest = os.lstat(path).st_mtime
    for dp, dns, fns in os.walk(path):
        for n in dns + fns:
            try:
                newest = max(newest, os.lstat(os.path.join(dp, n)).st_mtime)
            except OSError:
                pass
    return newest


def expire_cache(root=None, days=None, now=None, dry_run=False):
    """[(path, why)] of what this pass removed (or, dry_run, would remove) under the state root."""
    root = os.path.realpath(root or state_root())
    days = env_num("AGENTCTL_CACHE_EXPIRE_DAYS", CACHE_EXPIRE_DAYS) if days is None else days
    cutoff = (time.time() if now is None else now) - days * 86400
    if not allowed_path(root):
        return []
    out = []

    def inside(p):
        return os.path.realpath(os.path.dirname(p)).startswith(root + os.sep) or \
            os.path.realpath(os.path.dirname(p)) == root

    def drop(p, why):
        if not inside(p) or not allowed_path(p):
            return
        try:
            if not dry_run:
                if os.path.isdir(p) and not os.path.islink(p):
                    import shutil
                    shutil.rmtree(p)
                else:
                    os.unlink(p)
            out.append((p, why))
        except OSError:
            pass

    for sub, pats in CACHE_EXPIRE_FILES:
        d = os.path.join(root, sub)
        for pat in pats:
            for p in glob.glob(os.path.join(glob.escape(d), pat)):
                try:
                    st = os.lstat(p)
                except OSError:
                    continue
                if not os.path.isdir(p) and st.st_mtime < cutoff:
                    drop(p, f"older than {days:g} days")
    reds = os.path.join(root, "reds")
    for p in glob.glob(os.path.join(glob.escape(reds), "[0-9]*")):
        if os.path.islink(p) or not os.path.isdir(p) or not os.path.basename(p).isdigit():
            continue
        try:
            if _newest_mtime(p) >= cutoff:
                continue
        except OSError:
            continue
        running = [r for r in (read_record(f) for f in glob.glob(os.path.join(glob.escape(p), "fixer*.json")))
                   if r and not r.get("ended")]
        if not running:
            drop(p, f"red run dir, untouched for {days:g} days, no fixer running")
    for p in glob.glob(os.path.join(glob.escape(os.path.join(root, "internal-trees")), "*.json")):
        try:
            if os.lstat(p).st_mtime >= cutoff:
                continue
        except OSError:
            continue
        tree = (read_record(p) or {}).get("path")
        if not tree or (allowed_path(tree) and not os.path.exists(tree)):  # a protected path is never stat'ed
            drop(p, "internal-tree tag of a gone worktree")
    return out


# ORPHANED WORK (the owner, 2026-10-28: "review and fix landing code ... so things don't get missed or stalled"; the
# audit found movies-wt-workflow-prune rejected 6x and idle 15 h, identity-impact / unit-before-push / red-local idle
# 12-19 h, flipflop / nodrift-cadence / unify / mongo-csot never landed at all): every maintain pass, a worktree whose
# commits are not on forgejo/main (by patch) and whose author is gone -- no process in it, no land, no live claim,
# idle ORPHAN_IDLE -- gets ONE action: a re-land (its last verdict infra, unknown or none), or a dispatched fixer (a
# real failure: the checks rejected it). Still orphaned after that: UNLANDED ORPHAN, a condition whose probe clears
# when its commits are on main, or somebody works in it again. Nothing is ever deleted.
ORPHAN_IDLE = 2 * 3600
ORPHAN_PER_PASS = 3


def orphan_key(repo, wt):
    """The change's identity across rebases: its commits' patch-ids (not HEAD, which every rebase moves)."""
    main = f"refs/remotes/{REMOTE}/main"
    ch = git(repo, "-C", wt, "cherry", main, "HEAD", check=False)
    plus = [l[2:].strip() for l in (ch.stdout if ch.returncode == 0 else "").splitlines() if l.startswith("+ ")]
    ids = []
    for sha in plus:  # bytes: a commit may carry binary content
        show = subprocess.run(["git", "-C", wt, "show", sha], capture_output=True, timeout=60).stdout
        r = subprocess.run(["git", "patch-id", "--stable"], input=show, capture_output=True, timeout=60)
        ids.append((r.stdout.decode(errors="replace").split() or [sha])[0])
    return plus, hashlib_sha1("\n".join(sorted(ids)))[:16] if ids else None


def hashlib_sha1(text):
    import hashlib
    return hashlib.sha1(text.encode()).hexdigest()


def last_land_verdict(wt):
    """The newest final land verdict in the inbox for this worktree, or None."""
    rw = os.path.realpath(wt)
    es = [e for e in inbox_entries() if e.get("final", True) and e.get("command") in ("land", "waiting")
          and os.path.realpath(str(e.get("cwd") or "")) == rw]
    return str(es[-1].get("verdict") or "") if es else None


def live_lease_worktrees():
    """The worktrees a live lease names (a fixer's, an interactive claim's): somebody works there."""
    out, t = set(), time.time()
    for r in Store().list():
        if r["holder"] and r["expires_at"] > t:
            wt = (json.loads(r["meta"] or "{}") or {}).get("worktree")
            if wt:
                out.add(os.path.realpath(wt))
    return out


def orphan_candidates(repo, now_=None):
    """[(worktree, commits, key, last verdict)] of the orphans now."""
    t = now_ if now_ is not None else time.time()
    try:
        cwds = {os.path.realpath(c) for c in process_cwds().values()}
    except (Exception, SystemExit):
        cwds = set()
    try:
        landers = {os.path.realpath(w) for w in land_cwds()}
    except (Exception, SystemExit):
        landers = set()
    live = live_lease_worktrees()
    try:  # a change in the land queue (queued, checking, waiting) is not orphaned
        d, _c, _w = waiting_state(repo)
        live |= {os.path.realpath(e.get("worktree") or "") for e in waiting_entries(d, open_only=False)
                 if e.get("status") in ("queued", "checking", "waiting")}
    except (Exception, SystemExit):
        pass
    out = []
    for w in worktrees(repo):
        p = w.get("worktree")
        if not isinstance(p, str) or w.get("bare") or os.path.realpath(p) == os.path.realpath(repo) or internal_tree(p) \
                or p.endswith((f"-wt-{WAITING_BRANCH}", "-wt-batch")) or not os.path.isdir(p):
            continue
        rp = os.path.realpath(p)
        if rp in landers or rp in live or any(c == rp or c.startswith(rp + "/") for c in cwds):
            continue
        act = gitdir_activity(repo, p) or 0
        if t - act < env_num("AGENTCTL_ORPHAN_IDLE", ORPHAN_IDLE):
            continue
        commits, key = orphan_key(repo, p)
        if commits and key:
            out.append((p, commits, key, last_land_verdict(p)))
    return out


def probe_orphan_landed(a):
    """The orphan's change is on main (by patch), its worktree is gone, or somebody works in it again."""
    wt = a["worktree"]
    if not os.path.isdir(wt):
        return True, f"{wt} is gone"
    repo = a.get("repo") or movies_repo_path()
    _c, key = orphan_key(repo, wt)
    if key != a.get("key"):
        return True, "its commits are on main" if key is None else "its change moved on (new commits)"
    rp = os.path.realpath(wt)
    if rp in live_lease_worktrees():
        return True, "a live claim works in it"
    return False, f"{len(_c)} commit(s) still not on main, nobody in it"


def orphan_duty(repo=None, dry_run=False, now_=None):
    """One maintain pass over the orphans: one action each, then UNLANDED ORPHAN. Returns what it did."""
    repo = repo or movies_repo_path()
    if not os.path.isdir(repo):
        return []
    path = recovery_dir("orphans.json")
    st = read_record(path) or {}
    did, acted = [], 0
    for wt, commits, key, verdict in orphan_candidates(repo, now_):
        rec = st.get(key) or {"worktree": wt, "first": ts(), "actions": []}
        name = os.path.basename(wt)
        if dry_run:
            did.append(f"orphan {name}: {len(commits)} commit(s); last verdict: {verdict or 'never landed'}")
            continue
        if not rec["actions"]:
            if acted >= ORPHAN_PER_PASS:  # a few a pass: the queue is shared
                continue
            acted += 1  # SUBMITTED ONCE (design §11 I9): its checks then decide; a rejection is its red's to rescue
            w = reland_worktree(None, {"worktree": wt, "attempt": 1})
            rec["actions"].append({"at": ts(), "what": "submit", "msg": w})
            did.append(f"orphan {name}: {rec['actions'][-1]['what']} ({verdict or 'never landed'})")
        elif not rec.get("escalated") and time.time() - parse_time(rec["actions"][-1]["at"]).timestamp() \
                >= env_num("AGENTCTL_ORPHAN_IDLE", ORPHAN_IDLE):  # its action had its time
            text = (f"UNLANDED ORPHAN: {wt.replace(HOME, '~')}: {len(commits)} commit(s) not on forgejo/main, its author "
                    f"gone, still not landed after a {rec['actions'][-1]['what']} ({verdict or 'never landed'}) -- land it, "
                    "hand it to an agent, or drop it")
            c = open_condition("UNLANDED_ORPHAN", text, run_probe("orphan_landed", {"worktree": wt, "key": key, "repo": repo}),
                               [], key=re.sub(r"[^\w.-]+", "_", f"orphan-{name}"), command="maintain", cwd=wt)
            rec["escalated"] = ts() if c else None
            did.append(f"orphan {name}: escalated UNLANDED ORPHAN" if c else f"orphan {name}: cleared")
        st[key] = rec
    if not dry_run:
        write_json(path, st)
    return did


FIXTURE_SERVER = re.compile(r"serve\.py\b|FixtureServerMain")
FIXTURE_MAX_AGE = 6 * 3600


def fixture_reason(cmd, ppid, etime, isdir=os.path.isdir):
    """Why a test fixture server (`python -I <tmp>/serve.py`, tools.FixtureServerMain) is stale, or None: its temp dir is
    gone, or it is older than 6 h and orphaned (no live parent test)."""
    if not FIXTURE_SERVER.search(cmd) or "agentctl" in cmd:
        return None
    m = re.search(r"(/(?:private/)?var/folders/\S+?/tmp\.[^/\s]+|/(?:private/)?tmp/\S*?)/serve\.py", cmd)
    if m and not isdir(m.group(1)):
        return f"fixture server: its temp dir {m.group(1)} is gone"
    if tosec(etime) >= FIXTURE_MAX_AGE and ppid == 1:
        return f"fixture server up {etime}, older than 6 h with no live parent test"
    return None


def cmd_orphans(a):
    sbt_only = getattr(a, "sbt_only", False)  # the daily sweep: stuck sbt servers only
    safe = getattr(a, "safe", False)  # the scheduled reaper: never an owner merely on old code (it re-execs itself)
    owners = [] if sbt_only else owner_orphans(a.kill and not safe) if safe else owner_orphans(a.kill)
    if safe and a.kill:  # a dead owner's record and runs only
        owners = [o for o in owners if "process gone" in o[2]]
        owner_orphans_dead_only()
    procs = ps()
    cwds = process_cwds()
    me = {os.getpid(), os.getppid()}
    found = []
    status_cache = {}

    def finished(repo, rid):
        k = (repo, rid)
        if k not in status_cache:
            try:
                status_cache[k] = gh_json(repo, "run", "view", rid, "--json", "status").get("status") == "completed"
            except Exception:
                status_cache[k] = None
        return status_cache[k]

    seen_targets = {}
    loops = [] if sbt_only else [p for p in procs if p[0] not in me and is_wait_loop(p[3])]
    loops.sort(key=lambda p: p[0])
    for pid, ppid, etime, cmd in loops:
        body = loop_body(cmd)
        reasons = []
        if ppid == 1:
            reasons.append("owning shell/agent is gone")
        gone = missing_tree_paths(body + " " + cwds.get(pid, ""))
        if gone:
            reasons.append(f"worktree missing: {', '.join(gone)}")
        repo, ids = loop_targets(body)
        if repo and ids:
            states = [finished(repo, i) for i in ids]
            if all(s is True for s in states):
                reasons.append(f"all target runs finished ({repo} {' '.join(ids)})")
            key = (repo, tuple(ids))
            if key in seen_targets and pid_alive(seen_targets[key]):
                reasons.append(f"duplicate of loop pid {seen_targets[key]}")
            else:
                seen_targets.setdefault(key, pid)
        if reasons:
            found.append(("wait-loop", pid, etime, "; ".join(reasons), body[:140].replace("\n", " ")))
    cpu_path = state_root("sbt-cpu.json")
    try:
        with open(cpu_path) as f:
            cpu_state = json.load(f)
    except (OSError, ValueError):
        cpu_state = {}
    procs_cpu, children = {}, {}
    for line in run_cmd(["ps", "-A", "-o", "pid=,ppid=,time="]).stdout.splitlines():
        f3 = line.split()
        if len(f3) == 3:
            secs = 0.0
            for x in re.split(r"[:-]", f3[2]):
                try:
                    secs = secs * 60 + float(x)
                except ValueError:
                    pass
            procs_cpu[int(f3[0])] = secs
            children.setdefault(int(f3[1]), []).append(int(f3[0]))
    clients = {}
    for pid, ppid, etime, cmd in procs:
        if "sbtn" in cmd.split(" ", 1)[0] and cwds.get(pid):
            clients.setdefault(cwds[pid], []).append(pid)
    serving, now, live = sbt_serving(), time.time(), set()
    for pid, ppid, etime, cmd in procs:
        if pid in me or not is_sbt(cmd):
            continue
        cwd = cwds.get(pid, "")
        reasons = []
        if "java" in cmd.split(" ", 1)[0] and os.environ.get("AGENTCTL_SBT_REAP") != "0":  # tests: "0"
            live.add(str(pid))
            reasons += sbt_stuck_reasons(pid, cwd, tree_cpu(pid, procs_cpu, children), clients, serving, cpu_state, now)
        if cwd and not os.path.isdir(cwd):
            reasons.append(f"cwd gone: {cwd}")
        elif cwd and not os.path.exists(os.path.join(cwd, ".git")) and cwd.startswith(HOME + "/projects"):
            reasons.append(f"cwd {cwd} is no longer a git checkout")
        gone = missing_tree_paths(cmd)
        if gone:
            reasons.append(f"worktree missing: {', '.join(gone)}")
        if reasons:
            found.append(("sbt", pid, etime, "; ".join(reasons), f"{cwd}: {cmd[:120]}"))
    for pid, ppid, etime, cmd in procs:  # stale test fixture servers: listed first, reaped, logged
        why = fixture_reason(cmd, ppid, etime) if pid not in me else None
        if why:
            found.append(("fixture", pid, etime, why, cmd[:160]))
    try:
        if not live and os.environ.get("AGENTCTL_SBT_REAP") == "0":
            raise OSError("tests: the real state is left alone")
        with open(cpu_path + ".tmp", "w") as f:
            json.dump({k: v for k, v in cpu_state.items() if k in live}, f)
        os.replace(cpu_path + ".tmp", cpu_path)
    except OSError:
        pass
    jvms = []
    if not sbt_only and (os.environ.get("AGENTCTL_SBT_REAP") != "0" or os.environ.get("AGENTCTL_JVM_PS")):
        try:
            rows, jcwd, kept, ports = jvm_scan()
            taken = {f[1] for f in found}
            jvms = [c for c in jvm_candidates(rows, jcwd, kept, ports, os.getpid()) if c[0] not in taken]
        except (OSError, ValueError) as e:
            print(f"jvm scan failed: {e}")
    for kind, pid, why, what in owners:
        print(f"{kind:9} pid {pid!s:<6}  {why}\n          {what}")
    if not found:
        if jvms:
            jvm_reap(jvms, a.kill, procs)
        elif not owners:
            print("no orphans")
        return 0
    for kind, pid, etime, why, what in found:
        print(f"{kind:9} pid {pid:<6} up {etime:>11}  {why}\n          {what}")
    if a.kill:
        for kind, pid, *_ in found:
            if kind == "sbt" and "java" in (next((c for p, _pp, _e, c in procs if p == pid), "").split(" ", 1)[0]):
                print(f"thread dump of sbt server pid {pid}: {thread_dump(pid, cwds.get(pid, ''))}")
            kids = run_cmd(["pgrep", "-P", str(pid)]).stdout.split()
            for p in [pid] + [int(k) for k in kids]:
                try:
                    os.kill(p, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            print(f"killed {kind} pid {pid}" + (f" (+children {kids})" if kids else ""))
            reap_log(f"killed {kind} pid {pid}" + (f" (+children {kids})" if kids else "") + ": "
                     + next((f"{w} | {what}" for k, p, _e, w, what in found if p == pid), ""))
    if jvms:
        jvm_reap(jvms, a.kill, procs)
    return 0
