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
W8_WINDOW = 3600         # W8 looks at the change items that closed within this ($AGENTCTL_W8_WINDOW)
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
    """W1: the oldest UNLANDED change of the one queue (the store's open items, after the landed-by-content look) is
    younger than W1_MAX (while main is red: past the red's own start). Names the change and why it has not landed."""
    repo = movies_repo_path()
    st = Store()
    if os.path.isdir(repo):
        close_landed_changes(repo, store=st)
    open_ = [r for r in change_rows(st) if r["state"] in ("queued", "checking", "checked", "waiting")]
    if not open_:
        return True, "no change waits"
    oldest = min(open_, key=lambda r: r["since"])
    age = time.time() - oldest["since"]
    line, _e = cached_gate_summary(SHORT["movies"])
    red_since = 0.0
    if line and line.startswith("gate RED"):
        snap = ci_state_load(SHORT["movies"]) or {}
        starts = [parse_time(g["since"]).timestamp() for g in snap.get("gate") or [] if g.get("since")]
        red_since = (time.time() - min(starts)) if starts else 0.0
    lim = env_num("AGENTCTL_W1_MAX", W1_MAX)
    ok = age < lim or (red_since and age < red_since + lim)
    stranded = any(c["key"] == f"change-stranded-{oldest['id']}" for c in open_conditions(st))
    why = {"queued": "waiting for a check slot", "checking": "its checks run", "checked": "waiting for its push",
           "waiting": "waiting for main to turn green"}[oldest["state"]]
    return ok, f"oldest unlanded change {oldest['id']} ({oldest['state']}: " + \
        ("STRANDED, no queue entry carries it" if stranded else why) + f") is {fmt_age(age)} old" + \
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
        if fx is None and f.get("status") not in ("verify", "owner"):  # owner: filed with the owner (red owner-push)
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


def inv_wn():
    """WN: the owner's notifications get through -- the notifier's last delivery did not fail on both channels
    (terminal-notifier and the osascript fallback). Raised once, naming the cause and the owner's fix; cleared by the
    notifier's next delivery that gets through (or nothing left undelivered)."""
    try:
        f = notify_failure(Store())
    except Exception as e:
        return None, f"cannot read the store ({str(e)[:120]})"
    if not f:
        return True, "notifications reach the owner"
    return False, (f"notifications cannot reach the owner ({str(f.get('cause'))[:140]}; {f.get('pending', '?')} waiting): "
                   f"{NOTIFY_FIX}")


def inv_w6():
    """W6: no escalation event left unnotified for over W6_MAX (the notifier's delivery, from the store). While the
    delivery itself is broken (WN) that is WN's one condition, not a count of every escalation waiting behind it."""
    try:
        st = Store()
        if notify_failure(st):
            return None, "the notifier cannot deliver (see WN)"
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


def inv_w8():
    """W8: no change item left the queue (landed, rejected, superseded, cancelled) without an event addressed to its
    author's inbox -- Store.transition completes every such event (change_exit_event); this catches a writer around it.
    Looks at the items closed within W8_WINDOW, so it clears itself once a stray one ages out (and the fix lands)."""
    try:
        st = Store()
        silent = change_exits_unannounced(st, st.clock() - env_num("AGENTCTL_W8_WINDOW", W8_WINDOW))
    except Exception as e:
        return None, f"cannot read the store ({str(e)[:120]})"
    return not silent, (f"{len(silent)} change(s) left the queue with no inbox entry: {', '.join(silent[:3])}"
                        if silent else "every change that left the queue told its author")


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


WATCHDOG_MISSES = 2  # consecutive passes a self-healing invariant (W5, WM, W7) fails before its condition opens

INVARIANTS = {"WB": inv_wb, "W1": inv_w1, "W2": inv_w2, "W5": inv_w5, "W6": inv_w6, "W7": inv_w7, "W8": inv_w8, "WM": inv_wm,
              "WN": inv_wn}


def probe_invariant(a):
    return INVARIANTS[a["id"]]()


def cmd_watchdog(a):
    """One watchdog pass: every invariant checked; a violation opens its INVARIANT condition (and notifies), a restored
    one closes it; W5 kickstarts the processor. Prints one line per invariant."""
    write_beat("watchdog")
    st = watchdog_state()
    rc = 0
    misses = dict(st.get("misses") or {})
    for wid in INVARIANTS:
        r = run_probe("invariant", {"id": wid})
        key = f"invariant-{wid}"
        cond = next((c for c in open_conditions() if c["key"] == key), None)
        print(f"{wid}: {'ok' if r.ok else 'cannot tell' if r.ok is None else 'VIOLATED'} -- {r.detail}")
        if r.ok is False:
            rc = 1
            misses[wid] = int(misses.get(wid) or 0) + 1
            # HYSTERESIS (THE ALERT MODEL, 2026-11-04: W5/WM/W7 fired 31 times in a week on a single missed beat, each
            # cleared by itself): a self-healing invariant opens its condition only at WATCHDOG_MISSES consecutive
            # misses, and is told to the owner only once open past its bound (alerts.py: the notifier, ESCALATED)
            heals = wid in ALERT_SELF_HEALING_INVARIANTS
            if not cond and (not heals or misses[wid] >= env_num("AGENTCTL_WATCHDOG_MISSES", WATCHDOG_MISSES)):
                open_condition("INVARIANT", f"INVARIANT {wid} violated: {r.detail}", r, [], key=key, command="watchdog")
                if not heals:
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
        if r.ok:
            misses.pop(wid, None)
    write_json(state_root("watchdog.json"), {**watchdog_state(), "misses": misses})
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
    print("== logs")
    rotated = [] if a.dry_run else rotate_logs()
    for line in rotated or ["(dry run)" if a.dry_run else "none over the size"]:
        print(f"  {line}")
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
# commits are not on origin/main (by patch) and whose author is gone -- no process in it, no land, no live claim,
# idle ORPHAN_IDLE, no live session that ever named it -- gets ONE action: a re-land (its last verdict infra, unknown or
# none; only with LAND INTENT, never work NOT FOR LANDING, and from a throwaway tree, never the author's: see below and
# reland_worktree), or a dispatched fixer (a real failure: the checks rejected it). Still orphaned after that: UNLANDED ORPHAN, a condition whose probe clears
# when its commits are on main, or somebody works in it again. Nothing is ever deleted.
ORPHAN_IDLE = 2 * 3600
ORPHAN_PER_PASS = 3


def orphan_key(repo, wt):
    """The change's identity across rebases: its commits' patch-ids (not HEAD, which every rebase moves)."""
    main = f"refs/remotes/{remote()}/main"
    plus = cherry_landed(wt, main, "HEAD")[1] or []  # landed by patch or by content: not the orphan's
    ids = []
    for sha in plus:  # bytes: a commit may carry binary content
        show = subprocess.run(["git", "-C", wt, "show", sha], capture_output=True, timeout=60).stdout
        r = subprocess.run(["git", "patch-id", "--stable"], input=show, capture_output=True, timeout=60)
        ids.append((r.stdout.decode(errors="replace").split() or [sha])[0])
    return plus, hashlib_sha1("\n".join(sorted(ids)))[:16] if ids else None


def hashlib_sha1(text):
    import hashlib
    return hashlib.sha1(text.encode()).hexdigest()


def land_entries_by_cwd(entries=None):
    """{realpath of a cwd: its land/waiting inbox entries, oldest first}."""
    out = {}
    for e in inbox_entries() if entries is None else entries:
        if e.get("command") in ("land", "waiting"):
            out.setdefault(os.path.realpath(str(e.get("cwd") or "")), []).append(e)
    return out


def land_entries_once():
    """land_entries_by_cwd, read on first use and kept for the caller's pass: the inbox is thousands of files (7849 on
    2026-11-07, ~2.4 s a read), and an orphan pass asks it of every orphan twice or more (review 2026-11-07)."""
    memo = []

    def get():
        if not memo:
            memo.append(land_entries_by_cwd())
        return memo[0]
    return get


def last_land_entry(wt, lands=None):
    """The newest final land entry in the inbox for this worktree, or None. lands: a land_entries_once reader."""
    es = [e for e in (lands or land_entries_once())().get(os.path.realpath(wt)) or [] if e.get("final", True)]
    return es[-1] if es else None


# AN INFRA FAILURE IS NO VERDICT ON AN ORPHAN (2026-11-02: movies-wt-flip-stage5's land `crashed: StoreUnavailable: POST
# /v1/store/items: ... Connection refused` during a tunnel outage, and that crash was its final word: UNLANDED ORPHAN
# "after a submit (crashed: StoreUnavailable ...)" for good). A verdict that says the store, the tunnel, a fetch or the
# network failed -- not the change -- never escalates: the orphan is submitted again once the store answers, the waits
# between tries backing off (ORPHAN_INFRA_BACKOFF), however many it takes.
ORPHAN_INFRA = re.compile(r"crashed: (StoreUnavailable|StoreAuthFailed|URLError|ConnectionError|ConnectionRefusedError|"
                          r"TimeoutError|RemoteDisconnected)\b|\bStoreUnavailable\b|store (did not answer|unreachable)|"
                          r"its fetch of main failed|could not fetch|\btunnel\b|^NETWORK:", re.I)
ORPHAN_INFRA_BACKOFF = (900, 1800, 3600, 7200, 21600)
# A REAL FAILURE GETS ONE FIXER (the owner, 2026-11-03: movies-wfnames and -order-rows, pre-push NOT LANDED, escalated
# again after every rebase): an orphan whose submit was rejected by its checks (or whose rebase conflicts) and that
# nobody owns gets ONE fixer in its own worktree (the red-fixer pattern: a background Claude session under a lease of its
# own, `orphan · <worktree>` in `claude agents`), once per worktree -- whatever its later rebases do to its commits.
# Only when that fixer ended without landing it does UNLANDED ORPHAN go to a person. A parked worktree gets none.
ORPHAN_REAL_FAILURE = re.compile(r"^NOT LANDED: (the pre-push checks failed|rebase onto \S+ conflicts)")
ORPHAN_FIXERS_MAX = 2  # orphan fixers alive at once ($AGENTCTL_ORPHAN_FIXERS); one dispatched a pass


def orphan_infra(verdict):
    v = str(verdict or "")
    return bool(ORPHAN_INFRA.search(v) or NETWORK_CHECK_FAILURE.search(v)) and not ORPHAN_REAL_FAILURE.search(v)


def store_answers(store=None):
    """True when the store answers now (an infra-failed orphan's retry waits for it)."""
    try:
        (store or Store()).item("change", "store-answers-probe")
        return True
    except (Exception, SystemExit):
        return False


def orphan_fixers_path():
    return recovery_dir("orphan-fixers.json")


def orphan_fixers_alive():
    return [r for r in fixer_records() if r.get("orphan") and r.get("alive")]


def orphan_dispatch(repo, wt, name, verdict):
    """A background fixer IN the orphan's worktree (its author's), told the last verdict. (ok, message)."""
    num = f"orphan-{name}"
    d = reds_dir(num)
    os.makedirs(d, exist_ok=True)
    k = 1 + len(glob.glob(os.path.join(glob.escape(d), "fixer*.json")))
    rec_path = os.path.join(d, "fixer.json" if k == 1 else f"fixer-{k}.json")
    rec = {"sig": None, "sigs": [], "jobs": ["land"], "run": num, "run_id": None, "workflow": "Unlanded change",
           "job": "land", "kind": "test", "line": (verdict or "")[:400], "gate": False, "worktree": wt, "started": ts(),
           "why": "orphaned change, author gone, its checks rejected it", "log": os.path.join(d, f"claude-{k}.log"),
           "names": [num], "orphan": True}
    write_json(rec_path, rec)
    with open(os.path.join(d, "fixer.log"), "a") as logf:
        p = spawn_detached([sys.executable, TOOL_PATH, "red", "fixer", "--repo", repo, "--record", rec_path],
                           stdin=subprocess.DEVNULL, stdout=logf, stderr=logf, start_new_session=True, cwd=wt,
                           env=fixer_env(slug(repo), num))
    rec.update(pid=p.pid, process_start=process_start(p.pid))
    write_json(rec_path, rec)
    inbox_write("maintain", f"ORPHAN FIXER DISPATCHED: {wt.replace(HOME, '~')} -> fixer session 'orphan · {name}' "
                            f"(`claude agents`): {str(verdict or '')[:200]}", [], [], 0, cwd=wt)
    return True, f"fixer pid {p.pid} in {wt.replace(HOME, '~')}"


def live_lease_worktrees():
    """The worktrees a live lease names (a fixer's, an interactive claim's): somebody works there."""
    out, t = set(), time.time()
    for r in Store().list():
        if r["holder"] and r["expires_at"] > t:
            wt = (json.loads(r["meta"] or "{}") or {}).get("worktree")
            if wt:
                out.add(os.path.realpath(wt))
    return out


def orphan_candidates(repo, now_=None, dead=None, refused=None, lands=None):
    """[(worktree, commits, key, last verdict)] of the orphans now. A worktree holding a withdrawn or refuted blame
    revert (blame_land_refusal) is never one: it goes to `dead` [(worktree, why)] when given; one whose work is not for
    landing (orphan_refusal) goes to `refused` [(worktree, key, why)]. lands: the pass's land_entries_once reader."""
    t = now_ if now_ is not None else time.time()
    lands = lands or land_entries_once()
    try:
        cwds = {os.path.realpath(c) for c in process_cwds().values()}
    except (Exception, SystemExit):
        cwds = set()
    try:
        landers = {os.path.realpath(w) for w in land_cwds()}
    except (Exception, SystemExit):
        landers = set()
    live = live_lease_worktrees()
    try:
        sessions = session_worktrees([w.get("worktree") for w in worktrees(repo)], t)
    except (Exception, SystemExit):
        sessions = {}
    records = blame_records()
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
        if superseded_mark(rp):  # its work went into another change (superseded_duty closes it when that lands)
            continue
        if parked_mark(rp):  # parked for a reason (park_duty re-surfaces it when its condition is met)
            continue
        if rp in sessions:  # a live Claude session works in it: it has an owner
            continue
        act = gitdir_activity(repo, p) or 0
        if t - act < env_num("AGENTCTL_ORPHAN_IDLE", ORPHAN_IDLE):
            continue
        commits, key = orphan_key(repo, p)
        if commits and key:
            why = blame_land_refusal(p, commits, p, records=records)
            if why:  # 2026-11-01: movies-wt-revert-629 submitted as an orphan after its blame was withdrawn
                if dead is not None:
                    dead.append((p, why))
                continue
            why = orphan_refusal(p, commits)
            if why:  # 2026-11-06: es-gate-check (check/*, held by the owner) rebased and queued as an orphan
                if refused is not None:
                    refused.append((p, key, why))
                continue
            out.append((p, commits, key, orphan_verdict(p, lands)))
    return out


# NOT FOR LANDING (2026-11-06 22:51Z, es-gate-check: a check/* worktree holding a gate re-enable the owner holds was
# queued as an orphan). Nothing submits, unasked, work that says it is not for landing: a branch named check/*,
# measure/*, experiment/*, probe/*, prototype/*, spike/*, wip-* or wip/*, or a commit with a `Hold: <why>` trailer
# (held_commit; `land --release-hold` is the owner's go). Such a worktree is said once (`orphan <name>: not submitted
# (...)`) and never escalated: it is not stranded, it is kept.
ORPHAN_NOT_FOR_LANDING = re.compile(r"^((check|measure|experiment|probe|prototype|spike)/|wip[-/])", re.I)


def orphan_branch_refusal(branch):
    if ORPHAN_NOT_FOR_LANDING.match(branch or ""):
        return f"branch {branch} is not for landing"
    return None


def orphan_refusal(wt, commits):
    """Why orphan duty must never submit this worktree's commits (not for landing), or None."""
    return orphan_branch_refusal(branch_of(wt)) or held_commit(wt, commits) or do_not_land(wt, commits, branch_of(wt))


def orphan_land_entry(wt, lands=None):
    """The newest final land entry for the orphan: from its own worktree or the throwaway tree its re-land ran in."""
    lands = lands or land_entries_once()
    es = [e for e in (last_land_entry(wt, lands), last_land_entry(reland_tree_path(wt), lands)) if e]
    return max(es, key=lambda e: float(e.get("at") or 0)) if es else None


def orphan_verdict(wt, lands=None):
    """The newest final land verdict for the orphan (orphan_land_entry's), or None."""
    e = orphan_land_entry(wt, lands)
    return str(e.get("verdict") or "") if e else None


# LAND INTENT (2026-11-06, es-gate-check: queued though nobody had ever asked to land it). Orphan duty submits only
# work its author once meant to land: a worktree that ran `agentctl land` itself (a land entry in the inbox with that
# cwd) or has a change item of its own. A worktree that never did -- an agent that died mid-task, a check or design
# tree -- goes to a person (UNLANDED ORPHAN) instead: nothing lands that its author did not ask to land.
def land_intent(wt, items=None, lands=None):
    if (items or {}).get("last"):
        return True
    return bool((lands or land_entries_once())().get(os.path.realpath(wt)))


def orphan_submitted_alert(wt, commits, msg):
    """ORPHAN SUBMITTED, an owner alert: recovery put somebody's work into the queue -- say which, so it can be stopped."""
    subjects = [f"{c[:10]} {git(wt, 'log', '-1', '--format=%s', c, check=False).stdout.strip()[:80]}" for c in commits[:5]]
    more = f" (+{len(commits) - 5} more)" if len(commits) > 5 else ""
    inbox_write("maintain", f"ORPHAN SUBMITTED: {wt.replace(HOME, '~')} (branch {branch_of(wt) or 'detached'}): "
                            f"{len(commits)} commit(s) queued by orphan duty -- {'; '.join(subjects)}{more}. {msg}. "
                            "Stop it with `agentctl land cancel <id>` and `agentctl park <worktree> --reason ...`",
                [], [], 0, cwd=wt, item=f"orphan:{wt.replace(HOME, '~')}")


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
    sup = superseded_mark(rp)
    if sup:
        return True, f"superseded by {sup['by']}"
    dead = blame_land_refusal(wt, _c, wt)
    if dead:
        return True, f"never landed: {dead}"
    kept = orphan_refusal(wt, _c)
    if kept:
        return True, f"not for landing: {kept}"
    if rp in live_lease_worktrees():
        return True, "a live claim works in it"
    park = parked_mark(rp)
    if park:
        return True, f"parked: {park['reason']}"
    owner = session_worktrees([rp]).get(rp)
    if owner:
        return True, f"a live session works in it ({owner})"
    cover = orphan_covered_by(wt, worktree_change_items(wt))  # orphan_duty's rule (a): an open change carries it
    if cover:
        return True, f"covered by {cover}"
    return False, f"{len(_c)} commit(s) still not on main, nobody in it"


# SUPERSEDED (the owner, 2026-10-31: movies-wt-ci-1/2/3 and -review were carried into ci-5 on 2026-10-30 -- rebased
# with real conflict resolution, so neither patch-id nor the content twin could match them -- and escalated UNLANDED
# ORPHAN until removed by hand). `agentctl supersede <worktree> --by <worktree|change id>` says its work went into
# another change. A superseded worktree gets no orphan action and no escalation (a probe it holds open clears: "superseded
# by"), and closes itself once its successor lands -- a change item of the successor's worktree LANDED since the mark,
# the named change (or the change that superseded it in the queue) LANDED, or the successor's head at the mark on main
# by content: then maintain removes it (clean and idle only; logged; its branch with it). The successor ending unlanded
# -- its worktree gone with nothing landed, or the named change rejected or cancelled -- LAPSES the mark: the orphan
# rules apply again, and the probe that cleared reopens. Each state is re-read every maintain pass (self-recovering).
SUPERSEDE_CHAIN = 10  # change ids followed through `superseded_by` (re-submissions) at most


def superseded_path():
    return recovery_dir("superseded.json")


def superseded_marks():
    """{realpath of the superseded worktree: its mark} (state: open | landed | closed | lapsed)."""
    return read_record(superseded_path()) or {}


def superseded_mark(wt):
    """The live mark of worktree wt (open or landed: its successor carries it), else None."""
    m = superseded_marks().get(os.path.realpath(wt))
    return m if m and m.get("state") in ("open", "landed") else None


def change_chain_end(st, cid):
    """The change item cid, followed through its `superseded_by` (a re-submission replaces it), or None."""
    it = st.item("change", cid)
    for _ in range(SUPERSEDE_CHAIN):
        nxt = (it or {}).get("data", {}).get("superseded_by") if it and it["state"] == "superseded" else None
        if not nxt:
            break
        it = st.item("change", nxt) or it
        if it["id"] != nxt:
            break
    return it


def successor_state(m, repo, store=None):
    """(open | landed | lapsed, why) of mark m's successor."""
    st = store or Store()
    by, at = m["by"], float(m.get("at") or 0)
    end = change_chain_end(st, m["change"]) if m.get("change") else None
    if end and end["state"] == "landed":
        return "landed", f"its change {end['id']} landed"
    succ = m.get("worktree")
    if succ:
        rs = os.path.realpath(succ)
        for it in st.items("change", ["landed"]):
            if os.path.realpath(str(it["data"].get("worktree") or "")) == rs and float(it.get("entered_at") or 0) >= at:
                return "landed", f"{os.path.basename(rs)}'s change {it['id']} landed"
    if m.get("head"):
        done, via = change_landed(repo, m["head"])
        if done:
            return "landed", f"{by}'s head {m['head'][:10]} is on main" + (f" ({via[:10]})" if via else "")
    if m.get("change"):  # a named change decides: open while it (or its re-submission) is
        if not end:
            return "lapsed", f"change {m['change']} is not known"
        if machine_state("change", end["state"]).terminal:
            again = [i for i in (worktree_change_items(succ, st)["open"] if succ else [])
                     if os.path.realpath(str(i["data"].get("worktree") or "")) == os.path.realpath(succ)]
            if again:  # rejected, then landed again from the same worktree: that change carries it now
                return "open", f"its change {end['id']} ended {end['state']}; {again[-1]['id']} carries it now"
            return "lapsed", f"its change {end['id']} ended {end['state']}, unlanded"
        return "open", f"its change {end['id']} is {end['state']}"
    if succ and not os.path.isdir(succ):
        return "lapsed", f"{succ.replace(HOME, '~')} is gone with nothing of it landed"
    return "open", f"{by} has not landed yet"


def close_superseded(repo, wt, why):
    """Remove the superseded worktree wt and its branch: never the root checkout, a locked tree, one a process is in or
    one with uncommitted work. Returns (closed, line)."""
    wt = os.path.realpath(wt)
    if not os.path.isdir(wt):
        return True, f"{wt} is already gone"
    trees = worktrees(repo)
    me = next((t for t in trees if os.path.realpath(t.get("worktree") or "") == wt), None)
    if me is None or wt == os.path.realpath(trees[0]["worktree"]):
        return False, f"kept: {wt} is not a linked worktree of {repo}"
    if "locked" in me:
        return False, "kept: locked"
    try:
        busy = [p for p, c in process_cwds().items() if c == wt or c.startswith(wt + "/")]
    except (Exception, SystemExit):
        busy = []
    if busy:
        return False, f"kept: in use by pid {busy[:3]}"
    dirty = git(wt, "status", "--porcelain", "--untracked-files=all", check=False)
    if dirty.returncode or dirty.stdout.strip():
        return False, f"kept: the tree is not clean ({len(dirty.stdout.splitlines())} changed/untracked)"
    head = git(wt, "rev-parse", "HEAD").strip()
    branch = (me.get("branch") or "").removeprefix("refs/heads/") or None
    r = git(repo, "worktree", "remove", wt, check=False)
    if r.returncode:
        return False, f"kept: git worktree remove refused: {r.stderr.strip()[:160]}"
    log_deletion("worktree", os.path.basename(repo), wt, head, f"superseded: {why}")
    line = f"closed {wt.replace(HOME, '~')}"
    if branch and branch not in PROTECTED_BRANCHES and f"refs/heads/{branch}" not in {t.get("branch") for t in worktrees(repo)}:
        if git(repo, "update-ref", "-d", f"refs/heads/{branch}", head, check=False).returncode == 0:
            log_deletion("branch", os.path.basename(repo), branch, head, f"superseded: {why}")
            line += f" and branch {branch} ({head[:10]})"
    return True, line


def superseded_duty(repo, dry_run=False):
    """One maintain pass over the marks: each successor read again; a landed one's worktree closed, a lapsed mark said
    (the orphan rules take it back). Returns what it did."""
    marks = superseded_marks()
    did, changed = [], False
    for wt, m in sorted(marks.items()):
        if m.get("state") not in ("open", "landed"):
            continue
        name = os.path.basename(wt)
        try:
            state, why = successor_state(m, repo)
        except (Exception, SystemExit) as e:  # the store unread: no verdict, the mark stands
            did.append(f"superseded {name}: could not read its successor ({str(e)[:120]})")
            continue
        if dry_run:
            did.append(f"superseded {name} by {m['by']}: {state} ({why})")
            continue
        if state == "lapsed":
            m.update(state="lapsed", why=why, decided=ts())
            changed = True
            did.append(f"superseded {name}: the mark lapsed ({why}); the orphan rules apply again")
            continue
        if state == "landed":
            if m.get("state") != "landed":
                m.update(state="landed", why=why, decided=ts())
                changed = True
            closed, line = close_superseded(repo, wt, f"by {m['by']}, which landed ({why})")
            if closed:
                m.update(state="closed", closed=ts(), why=why)
                changed = True
                did.append(f"superseded {name}: {m['by']} landed ({why}): {line}")
                inbox_write("maintain", f"CLOSED {wt.replace(HOME, '~')}: superseded by {m['by']}, which landed ({why})",
                            [], [line], 0, cwd=wt)
            elif m.get("said") != line:
                m["said"] = line
                changed = True
                did.append(f"superseded {name}: {m['by']} landed ({why}); {line}")
    if changed:
        write_json(superseded_path(), marks)
    return did


def resolve_successor(by, repo, store=None):
    """(display name, change id or None, successor worktree or None, its head or None) of `--by`: a worktree path (or
    the name of a sibling of repo's worktrees), else a change id (the whole id or a unique part of it)."""
    cands = [os.path.expanduser(by), os.path.join(os.path.dirname(os.path.realpath(repo)), by)]
    path = next((c for c in cands if os.path.isdir(c)), None)
    if path:
        top = git(path, "rev-parse", "--show-toplevel", check=False).stdout.strip()
        if not top:
            raise ValueError(f"{by} is no git worktree")
        _c, left = cherry_landed(top, f"refs/remotes/{remote()}/main", "HEAD")
        if not left:
            raise ValueError(f"{os.path.basename(top)} carries nothing that is not on main yet: mark it superseded once "
                             "its successor holds the work (commit it there first)")
        return os.path.basename(top), None, os.path.realpath(top), git(top, "rev-parse", "HEAD").strip()
    st = store or Store()
    its = [i for i in st.items("change") if i["id"] == by] or [i for i in st.items("change") if by in i["id"]]
    if len(its) != 1:
        raise ValueError(f"--by {by}: no worktree of that name, and {'no' if not its else len(its)} change id"
                         f"{'s' if len(its) != 1 else ''} match{'es' if len(its) == 1 else ''} it")
    it = its[0]
    wt = it["data"].get("worktree")
    return it["id"], it["id"], os.path.realpath(wt) if wt else None, it["data"].get("head")


def cmd_supersede(a):
    marks = superseded_marks()
    if not a.worktree:
        live = {w: m for w, m in marks.items() if a.all or m.get("state") in ("open", "landed")}
        for w, m in sorted(live.items()):
            print(f"{w.replace(HOME, '~')}: SUPERSEDED BY {m['by']} ({m.get('state')}{': ' + m['why'] if m.get('why') else ''})")
        if not live:
            print("supersede: no superseded worktree" + ("" if a.all else " open (--all: the closed and lapsed too)"))
        return 0
    wt = os.path.realpath(os.path.expanduser(a.worktree))
    top = git(wt, "rev-parse", "--show-toplevel", check=False).stdout.strip() if os.path.isdir(wt) else ""
    if not top:
        print(f"supersede: {a.worktree} is no git worktree")
        return 2
    wt = os.path.realpath(top)
    if a.undo:
        m = marks.pop(wt, None)
        write_json(superseded_path(), marks)
        print(f"supersede: {wt.replace(HOME, '~')} " + ("is no longer marked" if m else "was not marked"))
        return 0
    if not a.by:
        print("usage: agentctl supersede <worktree> --by <worktree|change id>   (or --undo)")
        return 2
    repo = git(wt, "rev-parse", "--path-format=absolute", "--git-common-dir").strip()
    repo = os.path.dirname(repo) if repo.endswith("/.git") else repo
    try:
        name, change, succ, head = resolve_successor(a.by, wt)
    except (Exception, SystemExit) as e:  # SystemExit: NoToken (a change id is read from the store)
        print(f"supersede: {str(e).replace('agentctl: ', '')}")
        return 2
    if succ and os.path.realpath(succ) == wt:
        print("supersede: a worktree cannot supersede itself")
        return 2
    marks[wt] = {"by": name, "change": change, "worktree": succ, "head": head, "at": time.time(), "marked": ts(),
                 "state": "open", "repo": repo}
    write_json(superseded_path(), marks)
    print(f"SUPERSEDED BY {name}: {wt.replace(HOME, '~')} -- no orphan escalation; it closes itself once {name} lands")
    return 0


# OWNERSHIP FOLLOWS ACTIVITY (the owner, 2026-11-03: movies-wt-unmatched-imdbslip escalated UNLANDED ORPHAN four times
# while an agent was landing it -- the agent's Claude session runs from another directory and works in the worktree by
# absolute path, so no process had its cwd there). A LIVE Claude session -- any transcript of it (the session's own or
# one of its subagents') written within OWNER_ACTIVE -- owns every worktree a tool call of it EVER named (or whose
# project directory is the worktree's): no orphan action, and an open UNLANDED ORPHAN clears ("a live session works in
# it"). Ownership lapses when the session goes quiet.
# 2026-11-06 22:51Z (es-gate-check): the rule used to ask for a tool call naming the worktree within the last 30 min, in
# the transcript's last 256 KB. The subagent that made ~/projects/movies-wt/es-gate-check had named it last 104 min
# before while it waited on CI from a sibling worktree, its transcript written minutes before: alive, and its worktree
# was taken for an orphan, rebased and queued. Every transcript of a live session is now read whole -- incrementally,
# from where the last pass stopped (SESSION_PATHS, a cache of the paths each one named) -- and `~/` / `$HOME/` paths
# count as the absolute paths they are.
OWNER_ACTIVE = 2 * 3600
SESSION_PATHS = "session-paths.json"
SESSION_PATH_DEPTH = 10  # path components kept per named path (a worktree is never deeper)
NAMED_PATH = re.compile(r"(?<![\w.~/$-])(~|\$HOME|\$\{HOME\})?(/[^\s\"'`;|&<>(){}\\,*?\[\]]+)")


def tool_call_paths(text):
    """The absolute paths a tool call's text names (`~/` and `$HOME/` expanded), each cut to SESSION_PATH_DEPTH parts."""
    out = set()
    for m in NAMED_PATH.finditer(str(text or "")):
        p = (HOME if m.group(1) else "") + m.group(2)
        p = p.rstrip(":.")
        parts = p.split("/")
        if len(parts) > 1 and parts[1]:
            out.add("/".join(parts[:SESSION_PATH_DEPTH + 1]))
    return out


def tool_input_strings(x):
    if isinstance(x, str):
        yield x
    elif isinstance(x, dict):
        for v in x.values():
            yield from tool_input_strings(v)
    elif isinstance(x, list):
        for v in x:
            yield from tool_input_strings(v)


def session_transcripts(t=None, active=None):
    """{session key: [(path, mtime)]} of every LIVE session's transcripts (its own and its subagents'): a session is live
    while any of them was written within `active` s."""
    t = time.time() if t is None else t
    active = env_num("AGENTCTL_OWNER_ACTIVE", OWNER_ACTIVE) if active is None else active
    root = claude_projects_root()
    groups = {}
    for g in (os.path.join(glob.escape(root), "*", "*.jsonl"), os.path.join(glob.escape(root), "*", "*", "subagents", "*.jsonl")):
        for p in glob.glob(g):
            try:
                m = os.stat(p).st_mtime
            except OSError:
                continue
            key = p[:-len(".jsonl")] if "subagents" not in p else os.path.dirname(os.path.dirname(p))
            groups.setdefault(key, []).append((p, m))
    return {k: v for k, v in groups.items() if any(t - m <= active for _p, m in v)}


def transcript_paths(path, cache):
    """The paths the tool calls of transcript `path` named, read on from where cache[path] stopped (re-read whole when
    the file was rewritten). Updates cache in place."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            head = hashlib_sha1(f.read(512).decode("utf-8", "replace"))
            rec = cache.get(path)
            if not rec or rec.get("head") != head or int(rec.get("offset") or 0) > size:
                rec = {"head": head, "offset": 0, "paths": []}
            f.seek(int(rec["offset"]))
            data = f.read()
    except OSError:
        return set((cache.get(path) or {}).get("paths") or [])
    end = data.rfind(b"\n") + 1  # a line still being written is read next time
    paths = set(rec["paths"])
    for line in data[:end].split(b"\n"):
        if b'"tool_use"' not in line or b"/" not in line:
            continue
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if r.get("type") != "assistant":
            continue
        for c in (r.get("message") or {}).get("content") or []:
            if isinstance(c, dict) and c.get("type") == "tool_use":
                for text in tool_input_strings(c.get("input")):
                    paths |= tool_call_paths(text)
    rec.update(offset=int(rec["offset"]) + end, paths=sorted(paths))
    cache[path] = rec
    return paths


def session_worktrees(wts, t=None, active=None):
    """{realpath of a worktree of wts: the session that owns it} -- see OWNERSHIP FOLLOWS ACTIVITY."""
    want = {}
    for w in wts:
        if isinstance(w, str) and w:
            want[os.path.realpath(w)] = {w.rstrip("/"), os.path.realpath(w)}
    if not want:
        return {}
    cache_path = recovery_dir(SESSION_PATHS)
    cache = read_record(cache_path) or {}
    seen, out = {}, {}
    for _key, files in session_transcripts(t, active).items():
        for path, _m in sorted(files):
            sid = os.path.basename(path)[:-len(".jsonl")]
            proj = os.path.basename(os.path.dirname(path) if "subagents" not in path else
                                    os.path.dirname(os.path.dirname(os.path.dirname(path))))
            paths = transcript_paths(path, cache)
            seen[path] = cache.get(path)
            for rp, forms in want.items():
                if rp in out:
                    continue
                if proj == live_slug(rp) or any(p == f or p.startswith(f + "/") for p in paths for f in forms):
                    out[rp] = f"session {sid[:12]}"
    try:
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        write_json(cache_path, {k: v for k, v in seen.items() if v})  # only live sessions' transcripts are kept
    except OSError:
        pass
    return out


# PARKED (the owner, 2026-11-03: "fix root cause and prevent structurally" -- movies-wt-bare-borrow (a design branch the
# owner is still deciding on), -flip-stage5 (an unproven design kept for reference), -order-rows (blocked on another
# change landing first) escalated UNLANDED ORPHAN over and over: an orphan was "author gone + commits not on main", with
# no way to say "this work is parked for a reason"). `agentctl park <worktree> --reason R` with one condition:
#   --until-landed <change id | commit>   re-surfaces once that change LANDED (or its re-submission did) or the commit
#                                        is on main by content;
#   --until <date>                        re-surfaces at that time;
#   --owner-decision                      only `agentctl unpark` (a person) ends it.
# A parked worktree gets no orphan action and no escalation (an open UNLANDED ORPHAN clears: "parked: R"); `agentctl
# status` shows `parked: N` with the reasons. Each maintain pass reads every condition again (self-recovering): met, the
# mark is RELEASED and the orphan rules apply afresh (its orphan records dropped, so it gets its one submit -- it lands).
# A parked worktree older than PARK_DIGEST_DAYS is named in ONE weekly digest line (an inbox entry that wakes nobody).
PARK_DIGEST_DAYS = 14
PARK_DIGEST_EVERY = 7 * 86400


def parked_path():
    return recovery_dir("parked.json")


def parked_marks():
    """{realpath of the parked worktree: its mark} (state: parked | released | unparked)."""
    return read_record(parked_path()) or {}


def parked_mark(wt):
    m = parked_marks().get(os.path.realpath(wt))
    return m if m and m.get("state") == "parked" else None


def park_until_text(m):
    u = m.get("until") or {}
    if u.get("kind") == "landed":
        return f"until {u.get('change') or ''}{' / ' if u.get('change') and u.get('commit') else ''}{(u.get('commit') or '')[:11]} lands"
    if u.get("kind") == "date":
        return f"until {u.get('at')}"
    return "until the owner decides"


def park_release_why(m, repo, t=None, store=None):
    """Why mark m's condition is met now, else None (unread: None -- the mark stands)."""
    t = time.time() if t is None else t
    u = m.get("until") or {}
    if u.get("kind") == "date":
        return f"its date {u['at']} passed" if t >= float(u.get("epoch") or 0) else None
    if u.get("kind") != "landed":
        return None
    if u.get("change"):
        try:
            end = change_chain_end(store or Store(), u["change"])
        except (Exception, SystemExit):
            end = None
        if end and end["state"] == "landed":
            return f"its blocker {end['id']} landed"
    if u.get("commit"):
        done, via = change_landed(repo, u["commit"])
        if done:
            return f"its blocker {u['commit'][:11]} is on main" + (f" ({via[:10]})" if via and via != u["commit"] else "")
    return None


def drop_orphan_records(wt):
    """Forget worktree wt's orphan records: the orphan rules start afresh for it (its one submit)."""
    path = recovery_dir("orphans.json")
    st = read_record(path) or {}
    rw = os.path.realpath(wt)
    keep = {k: v for k, v in st.items() if os.path.realpath(str((v or {}).get("worktree") or "")) != rw}
    if len(keep) != len(st):
        write_json(path, keep)


def park_duty(repo, dry_run=False, now_=None):
    """One maintain pass over the parks: a met condition releases its mark; the weekly digest of the old ones."""
    t = time.time() if now_ is None else now_
    marks = parked_marks()
    did, changed, old = [], False, []
    for wt, m in sorted(marks.items()):
        if m.get("state") != "parked":
            continue
        name = os.path.basename(wt)
        if not os.path.isdir(wt):
            if not dry_run:
                m.update(state="released", why="its worktree is gone", decided=ts())
                changed = True
            did.append(f"parked {name}: released (its worktree is gone)")
            continue
        why = park_release_why(m, m.get("repo") or repo, t)
        if why:
            if not dry_run:
                m.update(state="released", why=why, decided=ts())
                changed = True
                drop_orphan_records(wt)
                inbox_write("maintain", f"UNPARKED {wt.replace(HOME, '~')}: {why}; the orphan rules apply again "
                                        f"(parked: {m['reason']})", [], [], 0, cwd=wt)
            did.append(f"parked {name}: released ({why}); the orphan rules apply again")
            continue
        if t - float(m.get("at") or t) >= env_num("AGENTCTL_PARK_DIGEST_DAYS", PARK_DIGEST_DAYS) * 86400:
            old.append((wt, m))
    last = max([float(m.get("digest_at") or 0) for _w, m in old] or [0])
    if old and t - last >= PARK_DIGEST_EVERY:
        line = "PARKED DIGEST: " + "; ".join(
            f"{os.path.basename(w)} ({int((t - float(m['at'])) // 86400)} d, {park_until_text(m)}: {m['reason']})" for w, m in old)
        if not dry_run:
            inbox_write("maintain", line, [], [], 0)
            for _w, m in old:
                m["digest_at"] = t
            changed = True
        did.append(line)
    if changed:
        write_json(parked_path(), marks)
    return did


def parked_lines():
    """`agentctl status`'s parked section: `parked: N` and one line per parked worktree with its reason."""
    live = sorted((w, m) for w, m in parked_marks().items() if m.get("state") == "parked")
    return [f"parked: {len(live)}"] + [f"  {os.path.basename(w)}  {park_until_text(m)}: {m['reason']}" for w, m in live]


def resolve_blocker(x, repo, store=None):
    """{change, commit} of `--until-landed x`: a change id (whole or a unique part; its head the commit), else a commit
    of repo. Raises ValueError."""
    st = store or Store()
    try:
        items = st.items("change")
    except (Exception, SystemExit):
        items = []
    its = [i for i in items if i["id"] == x] or [i for i in items if x in i["id"]]
    if len(its) == 1:
        return {"change": its[0]["id"], "commit": (its[0].get("data") or {}).get("head")}
    if len(its) > 1:
        raise ValueError(f"--until-landed {x}: {len(its)} change ids match it")
    r = git(repo, "rev-parse", "--verify", "-q", f"{x}^{{commit}}", check=False)
    if r.returncode == 0 and r.stdout.strip():
        return {"change": None, "commit": r.stdout.strip()}
    if re.fullmatch(r"\d{8}-\d{6}-[\w.-]+", x):  # a change id the store does not hold (yet): named, read each pass
        return {"change": x, "commit": None}
    raise ValueError(f"--until-landed {x}: no change id and no commit of that name")


def park_target(path):
    wt = os.path.realpath(os.path.expanduser(path))
    top = git(wt, "rev-parse", "--show-toplevel", check=False).stdout.strip() if os.path.isdir(wt) else ""
    if not top:
        return None, None
    common = git(top, "rev-parse", "--path-format=absolute", "--git-common-dir").strip()
    return os.path.realpath(top), (os.path.dirname(common) if common.endswith("/.git") else common)


def cmd_park(a):
    marks = parked_marks()
    if not a.worktree:
        for line in parked_lines():
            print(line)
        return 0
    wt, repo = park_target(a.worktree)
    if not wt:
        print(f"park: {a.worktree} is no git worktree")
        return 2
    conds = [c for c in (a.until_landed, a.until, a.owner_decision or None) if c]
    if not a.reason or len(conds) != 1:
        print('usage: agentctl park <worktree> --reason "<why>" (--until-landed <change id|commit> | --until <date> | '
              "--owner-decision)")
        return 2
    if a.until_landed:
        try:
            until = {"kind": "landed", **resolve_blocker(a.until_landed, repo)}
        except ValueError as e:
            print(f"park: {e}")
            return 2
    elif a.until:
        try:
            when = dt.datetime.fromisoformat(a.until.replace("Z", "+00:00"))
        except ValueError:
            print(f"park: --until {a.until}: not a date (YYYY-MM-DD or an ISO-8601 time)")
            return 2
        until = {"kind": "date", "at": a.until, "epoch": when.timestamp()}
    else:
        until = {"kind": "owner"}
    marks[wt] = {"reason": a.reason, "until": until, "at": time.time(), "parked": ts(), "state": "parked", "repo": repo}
    write_json(parked_path(), marks)
    print(f"PARKED {wt.replace(HOME, '~')} {park_until_text(marks[wt])}: {a.reason} -- no orphan escalation")
    return 0


def cmd_unpark(a):
    marks = parked_marks()
    wt, _repo = park_target(a.worktree)
    wt = wt or os.path.realpath(os.path.expanduser(a.worktree))
    m = marks.get(wt)
    if not m or m.get("state") != "parked":
        print(f"unpark: {wt.replace(HOME, '~')} is not parked")
        return 0
    m.update(state="unparked", decided=ts())
    write_json(parked_path(), marks)
    drop_orphan_records(wt)
    print(f"UNPARKED {wt.replace(HOME, '~')}: the orphan rules apply again")
    return 0


# AN ORPHAN IS SUBMITTED ONLY WHEN NOTHING ELSE CARRIES IT (ISSUE 20261030T165514-e5f431). At 15:50:59Z maintain
# re-submitted ~/projects/movies-wt-review with no session behind it: its commits were checked only against
# origin/main, while the open change movies-wt-ci-5 already carried them -- a duplicate in the shared queue. Now, before
# the one submit: (a) an open change item whose head carries every one of its commits (cherry_landed, the landed-by-
# content helper, against that head) covers it: no submit, no condition -- that change's verdict reaches its author, and
# once it lands the orphan is gone by the same helper; (b) a worktree whose last change item was CANCELLED is never
# re-submitted: a person cancelled it, so UNLANDED ORPHAN goes to a person at once. Any other orphan still gets its
# one submit (design §11 I9) -- turning every orphan into a condition (option c) would leave real finished work, the
# common case, waiting on the owner.
def worktree_change_items(wt, store=None):
    """{"open": [the open change items], "last": this worktree's newest change item (any state) or None}."""
    try:
        items = (store or Store()).items("change")
    except (Exception, SystemExit):
        return {"open": [], "last": None}
    rw = os.path.realpath(wt)
    mine = sorted((i for i in items if os.path.realpath(str((i.get("data") or {}).get("worktree") or "")) == rw),
                  key=lambda i: i["id"])
    return {"open": [i for i in items if not machine_state("change", i["state"]).terminal],
            "last": mine[-1] if mine else None}


def orphan_covered_by(wt, items):
    """The id of an open change item whose head carries every commit of the orphan (by patch or content), else None."""
    for it in items.get("open") or []:
        head = (it.get("data") or {}).get("head")
        if not head or not git_ok(wt, "cat-file", "-e", f"{head}^{{commit}}"):
            continue
        commits, left = cherry_landed(wt, head, "HEAD")
        if commits is not None and not left:
            return it["id"]
    return None


def orphan_escalate(repo, wt, name, commits, key, rec, verdict):
    text = (f"UNLANDED ORPHAN: {wt.replace(HOME, '~')}: {len(commits)} commit(s) not on {remote()}/main, its author "
            f"gone, still not landed after a {rec['actions'][-1]['what']} ({verdict or 'never landed'}) -- land it, "
            "hand it to an agent, park it (`agentctl park <worktree> --reason ...`), or drop it")
    c = open_condition("UNLANDED_ORPHAN", text, run_probe("orphan_landed", {"worktree": wt, "key": key, "repo": repo}),
                       [], key=re.sub(r"[^\w.-]+", "_", f"orphan-{name}"), command="maintain", cwd=wt)
    rec["escalated"] = ts() if c else None
    return [f"orphan {name}: escalated UNLANDED ORPHAN" if c else f"orphan {name}: cleared"]


def orphan_duty(repo=None, dry_run=False, now_=None):
    """One maintain pass over the orphans: one action each, then UNLANDED ORPHAN. Returns what it did."""
    repo = repo or movies_repo_path()
    if not os.path.isdir(repo):
        return []
    did, acted, dead = superseded_duty(repo, dry_run) + park_duty(repo, dry_run, now_), 0, []
    path = recovery_dir("orphans.json")
    st = read_record(path) or {}  # after park_duty: a released park's records are gone (its one submit again)
    fixers = read_record(orphan_fixers_path()) or {}
    fixers_changed = fixer_sent = False
    refused, lands = [], land_entries_once()  # one inbox read for the whole pass
    cands = orphan_candidates(repo, now_, dead, refused, lands)
    for wt, key, why in refused:  # not for landing: said once, never submitted, never escalated
        rec = st.get(key) or {"worktree": wt, "first": ts(), "actions": []}
        if rec.get("refused") != why:
            did.append(f"orphan {os.path.basename(wt)}: not submitted ({why})")
            rec["refused"] = why
        st[key] = rec
    for wt, why in dead:  # a dead blame revert: never submitted; blame's own worktree removed (only its reverts in it)
        name = os.path.basename(wt)
        did.append(f"orphan {name}: not submitted ({why})" if dry_run else
                   f"orphan {name}: not submitted ({why}); {blame_drop_revert_worktree(repo, wt, None, why)}")
    for wt, commits, key, verdict in cands:
        rec = st.get(key) or {"worktree": wt, "first": ts(), "actions": []}
        name = os.path.basename(wt)
        if dry_run:
            did.append(f"orphan {name}: {len(commits)} commit(s); last verdict: {verdict or 'never landed'}")
            continue
        items = worktree_change_items(wt)
        cover = orphan_covered_by(wt, items)
        if cover:  # (a) already in the queue under another change: its verdict is that change's (the probe clears too)
            if rec.get("covered_by") != cover:
                did.append(f"orphan {name}: carried by open change {cover}: not submitted")
            rec["covered_by"] = cover
            st[key] = rec
            continue
        was = rec.pop("covered_by", None)
        if was and rec["actions"]:  # escalated before its carrier came; the carrier closed (rejected, cancelled)
            # without landing it: the orphan reopens, to a person. Never acted on yet: its one submit, as before.
            rec["actions"].append({"at": ts(), "what": f"none (the change carrying it, {was}, closed unlanded)"})
            did += orphan_escalate(repo, wt, name, commits, key, rec, verdict or f"{was} closed unlanded")
            st[key] = rec
            continue
        if not rec["actions"]:
            last = items.get("last")
            if last and last["state"] == "cancelled":  # (b) somebody cancelled it: only a person submits it again
                rec["actions"].append({"at": ts(), "what": f"none (its last change {last['id']} was cancelled)"})
                did += orphan_escalate(repo, wt, name, commits, key, rec, verdict or f"cancelled ({last['id']})")
                st[key] = rec
                continue
            if not land_intent(wt, items, lands):  # LAND INTENT: its author never asked to land it -- a person decides
                rec["actions"].append({"at": ts(), "what": "none (it never ran `agentctl land`)"})
                did += orphan_escalate(repo, wt, name, commits, key, rec,
                                       "its author never ran `agentctl land` there: nothing submits it unasked")
                st[key] = rec
                continue
            if acted >= ORPHAN_PER_PASS:  # a few a pass: the queue is shared
                continue
            acted += 1  # SUBMITTED ONCE (design §11 I9): its checks then decide; a rejection is its red's to rescue
            w = reland_worktree(None, {"worktree": wt, "attempt": 1, "repo": repo})
            rec["actions"].append({"at": ts(), "what": "submit", "msg": w})
            did.append(f"orphan {name}: {rec['actions'][-1]['what']} ({verdict or 'never landed'})")
            if not str(w).startswith("not landed"):
                orphan_submitted_alert(wt, commits, w)
        elif not rec.get("escalated"):
            last_at = parse_time(rec["actions"][-1]["at"]).timestamp()
            e = orphan_land_entry(wt, lands)
            after = str(e.get("verdict") or "") if e and float(e.get("at") or 0) >= last_at - 1 else None
            msg = str(rec["actions"][-1].get("msg") or "")
            if msg.startswith("not landed again:"):  # the submit itself was refused: that is its verdict
                after = msg
            if after is not None and orphan_infra(after):  # (3) infra, not the change: submitted again, never escalated
                n = int(rec.get("infra_retries") or 0)
                wait_s = ORPHAN_INFRA_BACKOFF[min(n, len(ORPHAN_INFRA_BACKOFF) - 1)]
                if time.time() - last_at < wait_s:
                    pass
                elif not store_answers():
                    if rec.get("infra_said") != after[:120]:
                        rec["infra_said"] = after[:120]
                        did.append(f"orphan {name}: its last try failed on infra ({after[:120]}); retried when the "
                                   "store answers")
                elif acted < ORPHAN_PER_PASS:
                    acted += 1
                    rec["infra_retries"] = n + 1
                    w = reland_worktree(None, {"worktree": wt, "attempt": n + 2, "repo": repo})
                    rec["actions"].append({"at": ts(), "what": "submit", "msg": w, "infra": after[:200]})
                    did.append(f"orphan {name}: submit again (its last try failed on infra: {after[:120]})")
                    if not str(w).startswith("not landed"):
                        orphan_submitted_alert(wt, commits, w)
            elif time.time() - last_at >= env_num("AGENTCTL_ORPHAN_IDLE", ORPHAN_IDLE):  # its action had its time
                rw = os.path.realpath(wt)
                real = after if after is not None else verdict
                if real and ORPHAN_REAL_FAILURE.search(real) and rw not in fixers:  # (4) one fixer, then a person
                    if not fixer_sent and len(orphan_fixers_alive()) < env_num("AGENTCTL_ORPHAN_FIXERS", ORPHAN_FIXERS_MAX):
                        ok, line = orphan_dispatch(repo, wt, name, real)
                        rec["actions"].append({"at": ts(), "what": "fixer", "msg": line})
                        fixers[rw] = {"at": ts(), "key": key, "msg": line, "verdict": real[:300]}
                        fixers_changed = True
                        fixer_sent = True
                        did.append(f"orphan {name}: fixer dispatched ({real[:120]}): {line}")
                    else:
                        did.append(f"orphan {name}: a fixer waits for a free slot ({real[:80]})")
                else:
                    if rw in fixers and rec["actions"][-1]["what"] != "fixer":
                        rec["actions"].append({"at": ts(), "what": "fixer (ended unlanded)",
                                               "msg": fixers[rw].get("msg")})
                    did += orphan_escalate(repo, wt, name, commits, key, rec, real)
        st[key] = rec
    if not dry_run:
        write_json(path, st)
        if fixers_changed:
            write_json(orphan_fixers_path(), fixers)
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
            secs = tosec(f3[2])
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
