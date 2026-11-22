# agentctl, part `owner`: long-lived red family owners.
# Runs inside agentctl's one namespace, after the parts before it in lib/agentctl/__init__.py's PARTS (it uses their names).

# ---------------------------------------------------------------- family owners (docs/simplification-2026-10.md item 6)
# The owner, 2026-10-29: instead of a fresh fixer per red with a 3 h maximum lease, ONE PERSISTENT AGENT SESSION PER
# FAMILY (red-families.json `"owner": true`: infra, convergence, main tests). The owner of a family:
#   - is one `claude --bg` session and one wrapper (`agentctl red owner --family F`, detached), in ONE worktree of its
#     own (<clone>-wt-owner-<family>, branch owner-<family>) that it keeps across reds and restarts, with a NOTEBOOK
#     (RED-NOTEBOOK.md in that worktree, never committed) where it keeps what it learned: reproducers, hypotheses
#     tried, what is left;
#   - holds `owner/<family>`, a lease that is its HEARTBEAT: renewed every RED_RENEW_EVERY s while the wrapper lives,
#     lapsing OWNER_TTL after it dies -- never a per-red hold with a maximum;
#   - gets the family's reds through its inbox: the dispatcher takes each new red of the family under the owner's
#     holder and hands its lease to the wrapper (owners/<family>/leases.json, 0600), with a line in RED-INBOX.md. The
#     wrapper renews those leases with its heartbeat while the session works on them; a turn ends with a verdict, and
#     the leases of that turn are released with it (LANDED, GAVE UP, DIAGNOSED, BLOCKED ON OWNER), exactly as a
#     fixer's: the red machine then verifies, re-offers -- back to the same owner -- or blocks, and the attempt ladder
#     and the crash-loop breaker count as before. New reds start the next turn of the SAME session (`--resume`);
#   - is restarted only when it died: its leases lapse, its reds are offered again, and the dispatcher starts the
#     wrapper again with the same worktree, notebook and session. It ends on its own after OWNER_IDLE with no red,
#     on RED-STOP.md, when its heartbeat lease is lost, or after OWNER_EMPTY_TURNS turns in a row with no verdict.
OWNER_TTL = 15 * 60          # its heartbeat lease lapses this long after the wrapper's last beat
OWNER_MAX = 7 * 86400        # ...and is held at most this long in one spell (a restart takes it again)
OWNER_IDLE = 6 * 3600        # no red held this long: the owner ends ($AGENTCTL_OWNER_IDLE)
OWNER_EMPTY_TURNS = 2        # turns in a row ending with no verdict line: the owner ends (its reds are offered again)
OWNER_NOTEBOOK = "RED-NOTEBOOK.md"

def owner_lease(fam):
    return f"owner/{fam}"


def owner_holder(fam):
    return f"laptop:owner:{fam}"


def owner_dir(fam):
    return reds_dir("owners", re.sub(r"[^A-Za-z0-9_.-]", "_", fam))


def owner_record_path(fam):
    return os.path.join(owner_dir(fam), "owner.json")


def owner_leases_path(fam):
    return os.path.join(owner_dir(fam), "leases.json")


def family_owned(fam):
    """A configured family with a long-lived owner (red-families.json `"owner": true`)."""
    return any(f["name"] == fam and f.get("owner") for f in red_families())


def owner_alive(store, fam, t):
    """The owner's heartbeat lease while it is live, else None."""
    row = store.get(owner_lease(fam))
    return row if lease_is_live(row, t) else None


def owner_leases(fam):
    return read_record(owner_leases_path(fam), {})


def owner_set_leases(fam, leases):
    write_json(owner_leases_path(fam), leases, indent=None, mode=0o600)  # lease secrets: the owner's eyes only


def owner_hand(fam, name, took):
    """The dispatcher hands a red lease it took for the owner to the owner's wrapper (its inbox of leases)."""
    os.makedirs(owner_dir(fam), exist_ok=True)
    with open(os.path.join(owner_dir(fam), ".lock"), "a+") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        leases = owner_leases(fam)
        leases[name] = {"epoch": took["epoch"], "secret": took["secret"], "at": time.time()}
        owner_set_leases(fam, leases)


def owner_drop(fam, names):
    os.makedirs(owner_dir(fam), exist_ok=True)
    with open(os.path.join(owner_dir(fam), ".lock"), "a+") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        leases = owner_leases(fam)
        for n in names:
            leases.pop(n, None)
        owner_set_leases(fam, leases)


def owner_worktree(repo, fam, rec):
    """The owner's ONE worktree: the recorded one while it exists, else <clone>-wt-owner-<family> (created off
    origin/main, or its branch taken up again). The notebook and RED-INBOX.md are excluded from commits. It is LOCKED
    (`git worktree lock`), which `cleanup` keeps: an owner's tree is clean and its HEAD an ancestor of main between
    commits, so upkeep's hourly cleanup removed it -- once mid-turn (outside-gate turn 67, 2026-11-22T04:01Z: "my
    worktree was deleted mid-turn"), and at 11-21 12:39Z, 19:46Z, 11-22 00:12Z and 04:27Z between turns."""
    if rec.get("worktree") and os.path.isdir(rec["worktree"]):
        lock_owner_worktree(repo, fam, rec["worktree"])
        return rec["worktree"]
    _d, clone, _w = waiting_state(repo)
    slug_ = re.sub(r"[^A-Za-z0-9_.-]", "-", fam)
    wt, branch = f"{clone}-wt-owner-{slug_}", f"owner-{slug_}"
    if not os.path.isdir(wt):
        git(repo, "worktree", "prune", check=False)
        git(repo, "fetch", "--quiet", remote(), "main", check=False)
        has = git_ok(repo, "rev-parse", "--verify", "-q", f"refs/heads/{branch}")
        r = git(repo, "worktree", "add", "-q", *([wt, branch] if has else ["-b", branch, wt, f"{remote()}/main"]),
                check=False)
        if r.returncode != 0:
            return None
    lock_owner_worktree(repo, fam, wt)
    try:
        worktree_exclude(wt, (OWNER_NOTEBOOK, "RED-INBOX.md", "RED-NUDGE.md", "RED-STOP.md"))
    except OSError:
        pass
    return wt


def lock_owner_worktree(repo, fam, wt):
    """Lock the owner's worktree against `git worktree remove` / prune (idempotent: an already locked tree refuses
    the lock, which is fine)."""
    git(repo, "worktree", "lock", "--reason", f"agentctl red owner {fam}: its one worktree", wt, check=False)


# ONE START PER FAMILY (2026-11-21 22:12:33Z and 22:12:36Z): the convergence owner, ended ORPHANED, was started twice
# 3 s apart (pids 6921, 7139) -- two of its reds re-offered in two dispatcher passes, each finding no live heartbeat
# because the first wrapper had not taken it yet; the second wrapper ended DUPLICATE. The dispatcher now takes the
# owner's heartbeat lease ITSELF before it starts the wrapper, atomically in the store, and hands it over
# ($AGENTCTL_OWNER_HEARTBEAT): a later pass finds the owner alive and hands it the red; a pass that loses the take is a
# no-op that hands its leases to the owner starting.
OWNER_HEARTBEAT_ENV = "AGENTCTL_OWNER_HEARTBEAT"


def owner_meta(fam, wt, pid, notebook):
    return {"kind": "owner", "family": fam, "worktree": wt, "pid": pid, "notebook": notebook, "host": socket_host()}


def owner_spawn(store, o_r, repo, fam, off, took, extra=()):
    """Start (or restart, after it died) the family's owner: its worktree and notebook kept, the taken leases handed
    to it, its heartbeat taken for it (ONE START PER FAMILY), its wrapper detached. (ok, message)."""
    rec = read_record(owner_record_path(fam)) or {"family": fam, "restarts": -1}
    wt = owner_worktree(repo, fam, rec)
    if not wt:
        return False, f"RED OWNER {fam}: NOT STARTED: no worktree"
    for x in [{"name": off["lease"], **took}] + [dict(x) for x in extra]:
        owner_hand(fam, x["name"], x)
    nb = os.path.join(wt, OWNER_NOTEBOOK)
    hb = store.acquire(owner_lease(fam), owner_holder(fam), OWNER_TTL, OWNER_MAX, progress=f"beat {int(time.time())}",
                       meta=owner_meta(fam, wt, None, nb))
    if "held" in hb:  # another pass started it a moment ago: the red is the starting owner's next turn
        return True, f"RED OWNER {fam}: already starting ({hb['held']['holder']}): red {off['lease']} handed to it"
    if not os.path.exists(nb):
        with open(nb, "w") as f:
            f.write(f"# {fam} red family: the owner's notebook\n\nKept by the family's owner session across reds and "
                    "restarts: reproducers, hypotheses tried (and how they ended), what is left.\n")
    restarted = bool(rec.get("started"))
    # A RESTART CONTINUES (2026-11-03): the turn count, the session (rec["session"], resumed or adopted by the wrapper),
    # the worktree and the notebook go on; the last spell's end is kept for the resumed session's first prompt
    rec.update(family=fam, worktree=wt, notebook=nb, repo=repo, o_r=o_r, started=ts(), ended=None, verdict=None,
               last_end=rec.get("verdict") or rec.get("last_end"), turn=int(rec.get("turn") or 0), bg_id=None,
               pid=None, in_turn=False, progress_at=None,
               restarts=int(rec.get("restarts", -1)) + 1, log=os.path.join(owner_dir(fam), "owner.log"))
    write_json(owner_record_path(fam), rec)
    try:  # a STOP left by an earlier spell would stop this one at its first pass
        os.unlink(os.path.join(wt, "RED-STOP.md"))
    except OSError:
        pass
    env = fixer_env(o_r, f"owner-{fam}")
    env[OWNER_HEARTBEAT_ENV] = json.dumps({"epoch": hb["epoch"], "secret": hb["secret"]})
    try:
        with open(rec["log"], "a") as logf:
            p = spawn_detached([sys.executable, TOOL_PATH, "red", "owner", "--family", fam, "--repo", repo],
                               stdin=subprocess.DEVNULL, stdout=logf, stderr=logf, start_new_session=True, cwd=wt,
                               env=env)
    except OSError as e:  # never a heartbeat held for a wrapper that never ran
        store.release(owner_lease(fam), hb["epoch"], hb["secret"], outcome=f"NOT STARTED: {e}")
        return False, f"RED OWNER {fam}: NOT STARTED: {e}"
    rec.update(pid=p.pid, process_start=process_start(p.pid))
    write_json(owner_record_path(fam), rec)
    pic = off["picture"]
    line = (f"RED OWNER {fam}: {'RESTARTED (it had died)' if restarted else 'STARTED'} pid {p.pid} in "
            f"{wt.replace(HOME, '~')} for {pic.get('workflow')} #{(pic.get('run') or {}).get('number')}")
    inbox_write("red", line, [], [], 0, cwd=wt)
    return True, line


def owner_intro(owner):
    fam = owner["family"]
    reds = "; ".join(owner.get("reds") or [])
    return (f"You are the LONG-LIVED OWNER of the `{fam}` red family: one background session (the owner sees it in "
            "`claude agents`) that keeps every red of the family, across reds and restarts, in ONE worktree. Your "
            f"NOTEBOOK is {owner['notebook']}: read it FIRST, and keep it current after every step (the reproducer you "
            "built and how to run it, each hypothesis and how it ended, what is left) -- a restart of you continues "
            "from it and this same worktree. Your reds' leases are held for you by agentctl while you work (do not "
            "`red claim` them); new reds of your family arrive in RED-INBOX.md and as your next turn. "
            + (f"This turn's reds: {reds}. " if reds else "")
            + "End each turn with ONE verdict line for the turn's reds (LANDED <sha> / WAITS ON <sha> (red <key>): / "
              "GAVE UP: / DIAGNOSED: / BLOCKED ON OWNER:). When the durable fix needs the OWNER'S DECISION in an owner-only "
              "category (the brief's ASK THE OWNER ONLY WHAT NO AGENT CAN DO), end `DIAGNOSED: <cause>; "
              "decide[<category>]: <the decision only the owner can make>`: the red then waits on the owner with it. "
              "Any other question is yours: proceed under the repo's standing rules and checked-in policies. ")


def owner_resume_target(rec):
    """The session the owner's next turn resumes: its recorded one when that holds a conversation (fixer.bg_resumable),
    else the last one that did (`resumable`, kept here), else None -- a fresh session, never a resume claude refuses."""
    for sid in (rec.get("session"), rec.get("resumable")):
        if sid and bg_resumable(sid):
            rec["resumable"] = sid
            return sid
    rec.pop("resumable", None)
    return None


def owner_turn_prompt(fam, rec, st, names, first, resumed=False):
    """The turn's prompt: the first red's task (fixer_prompt) in the owner's voice, every red of the turn listed. A
    resumed session (its wrapper restarted) is told to continue, never to start over."""
    active = red_active(st)
    its = [active[n.split("/", 1)[1]] for n in names if n.split("/", 1)[1] in active]
    reds = [f"{red_what(i['data'])} (red {i['data'].get('key')}): {str(i['data'].get('line') or '')[:160]}" for i in its]
    if not its:
        return None
    d = its[0]["data"]
    u = {"workflow": d.get("workflow"), "job": d.get("job") or (d.get("jobs") or ["?"])[0], "jobs": d.get("jobs"),
         "kind": d.get("kind") or "test", "line": d.get("line"), "run": d.get("last_run") or {}, "key": d.get("key"),
         "refuted": d.get("refuted"), "predicts": d.get("predicts"), "flake": d.get("red_kind") == "flake",
         "diagnosis": d.get("diagnosis"), "prior_work": d.get("prior_work"), "old_note": d.get("old_note"),
         "decision": red_decision(d)}
    if resumed:
        note = ("RESUMED AFTER A RESTART, NEVER START OVER: your wrapper restarted (" + str(rec.get("last_end") or
                "it had died")[:200] + "); you are the SAME session, in the same worktree, with your notebook and "
                "your context: continue from where you left off (your notebook's `what is left`, your commits, the CI "
                "runs you started). The reds above are yours again; read RED-INBOX.md, then go on.")
    elif first:
        note = None
    else:
        note = ("NEXT TURN: the reds above are new or offered to you again (a previous verdict did not "
                "clear them): read RED-INBOX.md and your notebook, then go on.")
    return fixer_prompt(u, rec["worktree"], sync_brief(), note,
                        owner={"family": fam, "notebook": rec["notebook"], "reds": reds})


OWNER_HEARTBEAT_LOST = "STOPPED: the owner's heartbeat lease was lost"


def in_turn_of(s, transcript):
    """The wrapper's turn's session is still at work (its last `claude agents` row, and its transcript, not over)."""
    ent = s.get("ent")
    return bool(ent and ent.get("id") and not bg_turn_over(ent, transcript))


def red_owner(a):
    """THE OWNER'S WRAPPER (detached; `agentctl red owner --family F`): its heartbeat lease, its reds' leases renewed
    by the turn's progress, one session resumed turn after turn, each turn's leases released with the turn's verdict.
    A turn is bounded as a fixer's is: RED_FIXER_WALL, and a session missing from `claude agents` three polls running
    has ended. Whatever raises, the session is stopped and every lease released (never a live session nobody holds)."""
    fam = a.family
    path = owner_record_path(fam)
    rec = read_record(path) or {}
    wt = rec.get("worktree")
    me = os.getpid()
    st = CurrentStore()
    standby = laptop_role_standby(st)
    if standby:  # ONE MACHINE HOLDS THE LAPTOP ROLE (server.py): a standby machine runs no owner
        print(f"RED OWNER {fam}: NOT STARTED ({standby})")
        return 0
    holder = owner_holder(fam)
    hb = owner_take_heartbeat(st, fam, holder, owner_meta(fam, wt, me, rec.get("notebook")))
    if "held" in hb:
        print(f"RED OWNER {fam}: DUPLICATE ({hb['held']['holder']} is alive)")
        return 0
    if "lost" in hb:
        print(f"RED OWNER {fam}: NOT STARTED: the heartbeat taken for it was lost ({str(hb['lost'])[:300]})")
        return 0
    rec.update(pid=me, process_start=process_start(me))
    write_json(path, rec)
    keep_awake()  # the laptop must not idle-sleep past OWNER_TTL under a held heartbeat (host_slept)
    cli = claude_cli()
    prev = bg_session(cli, rec["session"][:8]) if rec.get("session") else None
    # A RESTART ADOPTS A SESSION STILL AT WORK (2026-11-03): the dead wrapper's session, mid-turn (a bisect, a measure
    # it waits on), goes on under this wrapper -- never stopped and started over. One whose turn is over but whose
    # process lingers is stopped: `--resume` of a running session starts a copy beside it; stopped, it goes on under
    # its own id.
    adopt = prev if (prev and prev.get("id") and not bg_turn_over(prev, rec.get("transcript"))
                     and owner_leases(fam)) else None
    if prev and prev.get("id") and prev.get("pid") and not adopt:
        bg_stop(cli, prev["id"])
    env = {k: v for k, v in os.environ.items() if k not in FIXER_IDENTITY_ENV and k != "AGENTCTL_DETACHED"}  # an agent
    every = env_num("AGENTCTL_RED_RENEW", RED_RENEW_EVERY)
    poll = env_num("AGENTCTL_BG_POLL", BG_POLL)
    idle_max = env_num("AGENTCTL_OWNER_IDLE", OWNER_IDLE)
    wall = env_num("AGENTCTL_RED_FIXER_WALL", RED_FIXER_WALL)
    stall = env_num("AGENTCTL_RED_STALL", RED_STALL)
    s = {"bid": None, "turn": int(rec.get("turn") or 0), "names": [], "empty": 0, "beat": 0.0,
         "idle_since": time.time(), "first": not rec.get("session"), "resumed": bool(rec.get("session")),
         "missing": 0, "turn_at": 0.0, "prog": TurnProgress(stall, 0.0), "hb": hb, "meta": {},
         "hbl": HeldLease(owner_lease(fam), hb["epoch"], hb["secret"], holder, OWNER_TTL)}  # ONE FIXER LIFECYCLE

    def turn_state(in_turn, now):
        """What the laptop's tick reads to tell an owner at work from an orphan (red_reap_orphans)."""
        rec.update(in_turn=in_turn, progress_at=s["prog"].at if in_turn else now)
        write_json(path, rec)

    def release(names, outcome, notes=""):
        leases = owner_leases(fam)
        for n in names:
            x = leases.get(n)
            if x:
                try:
                    st.release(n, x["epoch"], x["secret"], outcome=verdict_cut(outcome), notes=notes)
                except StoreUnavailable:  # it lapses by its TTL instead: the red is offered again
                    pass
        owner_drop(fam, names)

    def cancelled(signum, _frame):
        if s["bid"]:
            bg_stop(cli, s["bid"])
        release(list(owner_leases(fam)), "STOPPED: the owner's wrapper was stopped")
        try:
            st.release(owner_lease(fam), s["hb"]["epoch"], s["hb"]["secret"], outcome="STOPPED")
        except StoreUnavailable:
            pass
        os._exit(143)
    for sg in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        signal.signal(sg, cancelled)

    def end_turn(outcome_if_none, text=None):
        if text is None:  # THIS turn's answer: a resumed session's transcript also holds the turns before it
            text = bg_turn(rec.get("transcript") or bg_transcript(rec.get("session") or ""))["final"]
        verdict = fixer_verdict(text)
        tail = [l[:300] for l in text.splitlines()[-INBOX_TAIL:]]
        s["empty"] = 0 if verdict else s["empty"] + 1
        outcome = verdict or outcome_if_none
        line = f"RED OWNER {fam} turn {s['turn']}: {outcome[:300]}"
        inbox_write("red fixer", line, [line], tail, 0 if outcome.startswith("LANDED") else 1, cwd=wt)
        release(s["names"], outcome, "\n".join(tail[-8:]))
        if s["bid"]:  # THE TURN'S SESSION STOPPED (bg_spent): claude keeps its process on as a spare, and the next
            bg_stop(cli, s["bid"])  # turn's `--resume` of a running session starts a COPY -- one more per turn
        s["bid"], s["missing"], s["idle_since"] = None, 0, time.time()  # OWNER_IDLE counts from the turn's END: from
        # its start, a turn longer than OWNER_IDLE left the owner "idle" the moment it ended
        turn_state(False, time.time())
        return f"ENDED: {s['empty']} turns in a row with no verdict" if s["empty"] >= OWNER_EMPTY_TURNS else None

    def step(now):
        """One pass; returns the stop reason, or None to go on. PROOF OF WORK FIRST: a turn whose session ended (done,
        stopped, blocked, gone) or made no progress for RED_STALL ends -- its reds' leases released with that -- before
        any lease is renewed: a living wrapper alone never holds a red."""
        if s["bid"] is not None:
            ent = bg_session(cli, s["bid"])
            if ent and ent.get("sessionId") and rec.get("session") != ent["sessionId"]:
                rec.update(session=ent["sessionId"], transcript=bg_transcript(ent["sessionId"]))
                write_json(path, rec)
                bind_fixer_session(rec["session"], path)  # the session is a fixer's: ~/bin refuses its commits
            s["missing"] = s["missing"] + 1 if ent is None else 0
            s["ent"] = ent
            if bg_turn_over(ent, rec.get("transcript"), now) or s["missing"] >= 3:  # the turn ended (done, stopped,
                # blocked; or waiting with its verdict said, bg_turn_over) or the session is gone
                failure = bg_failure(s["bid"])
                why = (f"ended with no LANDED / GAVE UP line (session {s['bid']}: "
                       f"{(ent or {}).get('state') or 'gone from `claude agents`'}"
                       + (f": {failure}" if failure else "") + ")")
                return end_turn(why)
            if now - s["turn_at"] > wall:
                bg_stop(cli, s["bid"])
                return end_turn(f"TIMED OUT after {int(wall // 60)} min (maximum wall time of a turn)")
        if now - s["beat"] >= every:  # THE HEARTBEAT: the owner's lease; its reds' leases by the turn's progress
            try:
                if s["bid"] is not None:
                    _mark, moved, stalled = s["prog"].judge(rec.get("transcript") or rec.get("log"), s["bid"], wt,
                                                            s.get("ent"), rec.get("transcript"), now, every)
                    if moved:
                        turn_state(True, now)
                    elif stalled:
                        bg_stop(cli, s["bid"])
                        return end_turn(f"STALLED: no progress (transcript, worktree) for {fmt_age(stall)}")
                # THE HEARTBEAT, kept as every lease of either kind is (HeldLease): one that merely LAPSED -- the laptop
                # asleep, the store out of reach past OWNER_TTL -- and that nobody took is taken again by this living
                # wrapper; a revoked (orphaned), released or re-taken one ends it
                gone = s["hbl"].renew(st, f"beat {int(now)}")
                s["hb"] = dict(s["hb"], epoch=s["hbl"].epoch, secret=s["hbl"].secret)
                if gone:  # why it was lost, from the lease itself (2026-11-19: the bare line hid pawel-mbp's revokes)
                    s["handover"] = True
                    return f"{OWNER_HEARTBEAT_LOST} ({str(gone[1])[:300]})"
                # THE WRAPPER JUDGES PROGRESS (RED_STALL, above), so its reds' leases renew with each beat of a turn
                # not stalled (and between turns). Renewed only on a new mark, they lapsed after OWNER_TTL (15 min)
                # while the turn was inside its 30 min stall budget: "lease expired" every 15-30 min (2026-11-03,
                # convergence red 8fbdbe0d5a86), each a crash for the breaker and a re-offer
                beat = f"{s['prog'].mark or 'held'} beat {int(now)}"
                leases = owner_leases(fam)
                lost = []
                for n, x in leases.items():  # a lapsed one taken again (HeldLease); cleared, released, revoked: dropped
                    hl = HeldLease(n, x["epoch"], x["secret"], holder, OWNER_TTL)
                    if hl.renew(st, beat):
                        lost.append(n)
                    elif hl.retaken:
                        owner_hand(fam, n, {"epoch": hl.epoch, "secret": hl.secret})
                        leases[n] = dict(x, epoch=hl.epoch, secret=hl.secret)
                if lost:  # no longer the owner's
                    owner_drop(fam, lost)
                owner_tell_leases(st, {n: x for n, x in leases.items() if n not in lost}, s, wt, me, rec)
                s["beat"] = now
            except StoreAuthFailed as e:  # never "asked again" forever: a fresh store token once, else the owner ends
                if not store_reauth():
                    return f"store auth failed: {e}"
                env["AGENTCTL_STORE_TOKEN"] = os.environ["AGENTCTL_STORE_TOKEN"]
                print(f"{ts()} the store refused the token ({e}): a fresh store token taken", flush=True)
            except StoreUnavailable as e:
                print(f"{ts()} the store did not answer ({e}): asked again at the next beat", flush=True)
        stop_file = os.path.join(wt, "RED-STOP.md")
        if os.path.exists(stop_file):
            try:
                with open(stop_file) as f:
                    return "STOPPED: " + f.readline().strip()
            except OSError:
                pass
        held = list(owner_leases(fam))
        if s["bid"] is None:  # between turns: a new turn when the owner holds reds, else idle
            if not held:
                return f"IDLE: no red of {fam} for {fmt_age(idle_max)}" if now - s["idle_since"] >= idle_max else None
            s["idle_since"] = now
            resume = owner_resume_target(rec)
            if resume != rec.get("session"):  # A SESSION WITH NO CONVERSATION CANNOT BE RESUMED (fixer.bg_resumable)
                print(f"{ts()} RED OWNER {fam}: session {str(rec.get('session'))[:8]} holds no conversation: "
                      + (f"resuming {resume[:8]}, the last one that does" if resume else "a fresh session"), flush=True)
                rec.update(session=resume, transcript=bg_transcript(resume) if resume else None)
                s["first"], s["resumed"] = s["first"] or not resume, s["resumed"] and bool(resume)
            prompt = owner_turn_prompt(fam, rec, st, held, s["first"], resumed=s["resumed"])
            if prompt is None:  # its reds closed meanwhile
                owner_drop(fam, held)
                return None
            argv = [cli, "--bg", prompt, "--permission-mode", interactive_mode(), "--permission-prompts", "none",
                    "-n", f"red owner · {fam}", "--disallowedTools", "Bash(git push:*)", "Bash(*--no-verify*)",
                    "Bash(*PREPUSH_SKIP*)"]
            if rec.get("session"):
                argv += ["--resume", rec["session"]]
            bid, why = bg_start(argv, wt, env)
            if not bid:
                release(held, f"NOT STARTED: `claude --bg` {why}")
                return f"NOT STARTED: `claude --bg` {why}"
            s.update(bid=bid, turn=s["turn"] + 1, names=held, first=False, resumed=False, turn_at=now, missing=0,
                     prog=TurnProgress(stall, now))
            rec.update(bg_id=bid, turn=s["turn"])
            turn_state(True, now)
            return None
        return None

    if adopt:  # the dead wrapper's turn goes on under this one (A RESTART ADOPTS A SESSION STILL AT WORK)
        t0 = time.time()
        s.update(bid=adopt["id"], names=list(owner_leases(fam)), first=False, resumed=False, turn_at=t0,
                 prog=TurnProgress(stall, t0), turn=max(s["turn"], 1))
        rec.update(bg_id=adopt["id"], turn=s["turn"])
        turn_state(True, t0)
        print(f"{ts()} RED OWNER {fam}: adopted its session {adopt['id']}, still at work (turn {s['turn']})", flush=True)
    handed = rec.pop("handed_turn", None)
    text = bg_turn(rec.get("transcript") or bg_transcript(rec.get("session") or ""))["final"] \
        if handed and not adopt and owner_leases(fam) else ""
    if fixer_verdict(text):  # the turn handed over ended meanwhile with a verdict: its reds (offered to this owner
        s["names"] = list(owner_leases(fam))  # again) end their leases with it, never a new turn over the same ground
        end_turn("", text)
        print(f"{ts()} RED OWNER {fam}: turn {handed.get('turn')}, handed over, ended: its verdict to its reds",
              flush=True)
    write_json(path, rec)
    stop = None
    try:
        while stop is None:
            stop = step(time.time())
            if stop is None:
                time.sleep(poll)
    except StoreAuthFailed as ex:
        stop = f"store auth failed: {str(ex)[:300]}"
    except Exception as ex:  # never a dead wrapper with a live session and held leases
        stop = f"STOPPED: the owner's wrapper failed ({type(ex).__name__}: {str(ex)[:200]})"
    handed = bool(s["bid"] and s.get("handover") and in_turn_of(s, rec.get("transcript")))
    if s["bid"] and not handed:  # A LOST HEARTBEAT HANDS THE TURN OVER: its session
        bg_stop(cli, s["bid"])  # at work goes on, and the family's next owner adopts it (never started over)
    if handed:  # ... AND ITS VERDICT (issue 20261116T162652-930aa9: the turn's DIAGNOSED, said after this wrapper ended,
        # reached no red, and the same diagnosis was offered again): the next owner adopts the session still at work,
        # or -- its turn over by then -- reads that turn's verdict and ends its reds' leases with it
        rec["handed_turn"] = {"bid": s["bid"], "turn": s["turn"], "at": ts()}
    release(list(owner_leases(fam)), stop)
    try:
        st.release(owner_lease(fam), s["hb"]["epoch"], s["hb"]["secret"], outcome=stop)
    except StoreUnavailable:
        pass
    rec.update(ended=ts(), verdict=stop, in_turn=False)
    if (read_record(path) or {}).get("pid") in (None, me):  # a restarted owner's record is its own, never overwritten
        write_json(path, rec)
    if stop.startswith("store auth failed"):
        inbox_write("red", f"RED OWNER {fam}: {stop[:300]} (its reds offered again; the next owner starts with a fresh "
                    "store token)", [], [], 1, cwd=wt)
    print(f"RED OWNER {fam}: {stop}")
    return 0


def owner_take_heartbeat(st, fam, holder, meta):
    """The wrapper's heartbeat lease: the one its dispatcher took for it ($AGENTCTL_OWNER_HEARTBEAT, ONE START PER
    FAMILY), still held, its meta made the wrapper's -- else {'lost': why}; with none handed over (`red owner` run by
    hand), taken now -- {'held': ...} when another owner holds it."""
    handed = os.environ.pop(OWNER_HEARTBEAT_ENV, None)
    if not handed:
        return st.acquire(owner_lease(fam), holder, OWNER_TTL, OWNER_MAX, progress=f"beat {int(time.time())}",
                          meta=meta)
    x = json.loads(handed)
    hl = HeldLease(owner_lease(fam), x["epoch"], x["secret"], holder, OWNER_TTL)
    gone = hl.renew(st, f"beat {int(time.time())}")
    if gone:
        return {"lost": gone[1]}
    st.set_meta(owner_lease(fam), hl.epoch, hl.secret, meta)
    return {"epoch": hl.epoch, "secret": hl.secret}


def owner_tell_leases(st, leases, s, wt, me, rec):
    """Each red lease the owner holds carries its worktree, notebook, wrapper pid and session (`red status` shows the
    holder's worktree, never "?"): sent once per lease epoch and session."""
    for n, x in leases.items():
        key = (x.get("epoch"), rec.get("session"), s.get("bid"))
        if s["meta"].get(n) == key:
            continue
        st.set_meta(n, x["epoch"], x["secret"], {"worktree": wt, "notebook": rec.get("notebook"), "pid": me,
                                                  "session": rec.get("session"), "bg_id": s.get("bid")})
        s["meta"][n] = key


def owners_status(store, t):
    """Each configured owner family: its record and whether its heartbeat lives (the status model's `owners`)."""
    out = []
    for f in red_families():
        if not f.get("owner"):
            continue
        rec = read_record(owner_record_path(f["name"])) or {}
        row = store.get(owner_lease(f["name"]))
        out.append({"family": f["name"], "alive": lease_is_live(row, t), "worktree": rec.get("worktree"),
                    "notebook": rec.get("notebook"), "session": rec.get("session"), "turn": rec.get("turn"),
                    "restarts": rec.get("restarts"), "started": rec.get("started"), "ended": rec.get("ended"),
                    "verdict": rec.get("verdict"), "reds": len(owner_leases(f["name"]))})
    return out


# ---------------------------------------------------------------- PROOF OF WORK: orphaned leases (2026-10-31)
# A lease is held by WORK, never by a living process. 2026-10-31 (issue 20261031T203744-4a4a59): the main-tests owner's
# session stopped at 20:12 (its turn ended `blocked`), while its wrapper (pid 51249) waited for `done` and kept renewing
# the owner's heartbeat; every red of the family was handed to it, held "leased ... none at work" and never fixed. The
# wrappers now end a turn whose session ended or stalled (red_owner, red_fixer). And whatever a wrapper does -- older
# code, a hang -- the laptop's tick checks every laptop-held fixer and owner lease against the evidence of work: the
# wrapper's process, its session's row in `claude agents`, the transcript's and the worktree's progress. A holder not at
# work for RED_ORPHAN_GRACE loses its leases at once (revoked `ORPHANED: <why>`): the red is offered again, and a
# wrapper still running reads its next renewal as lost and exits. `red status` shows such a lease as orphaned, never
# "leased ... none at work".
RED_ORPHAN_GRACE = 5 * 60    # an ended session's turn still held this long is orphaned ($AGENTCTL_RED_ORPHAN_GRACE)


def owner_work(fam, t, holder_pid=None):
    """(True|False|None, why): is family fam's owner at work? None when this machine cannot tell. holder_pid: the
    wrapper the lease itself names (its meta's pid): a record of another spell judges nothing of it."""
    rec = read_record(owner_record_path(fam)) or {}
    pid = rec.get("pid")
    if not pid:
        return None, "no owner record on this machine"
    if holder_pid and str(holder_pid) != str(pid):
        # THE RECORD IS ANOTHER SPELL'S (2026-11-15 11:34:52, Order independence #74 and Main #2475): the leases of
        # two live wrappers in their turns (outside-gate pid 37262, main-tests) were revoked "ORPHANED ... its wrapper
        # ended (IDLE: no red of <family> for 6 h)" -- the verdicts of the previous spells (main-tests' ended
        # 2026-11-14), read from records that were not the holders'. A lease is judged by the wrapper it names.
        if pid_alive(int(holder_pid)):
            return None, f"its wrapper (pid {holder_pid}) lives; the owner record here is another spell's (pid {pid})"
        return False, f"its wrapper (pid {holder_pid}) is gone"
    if rec.get("ended"):
        return False, f"its wrapper ended ({str(rec.get('verdict') or '?')[:160]})"
    if not proc_alive(pid, rec.get("process_start")):
        return False, f"its wrapper (pid {pid}) is gone"
    if not rec.get("in_turn", bool(rec.get("bg_id"))):  # older records have no in_turn: a recorded session is a turn
        return True, "between turns"
    grace = env_num("AGENTCTL_RED_ORPHAN_GRACE", RED_ORPHAN_GRACE)
    idle = t - max(float(rec.get("progress_at") or 0), mtime(rec.get("transcript")))
    bid = rec.get("bg_id")
    if bid:
        ent = bg_session(claude_cli(), bid)
        if ent == {}:
            return None, "`claude agents` unread"
        if (ent is None or bg_turn_over(ent, rec.get("transcript"), t)) and idle > grace:
            return False, (f"its session {bid} is {(ent or {}).get('state') or 'gone'} while its wrapper (pid {pid}) "
                           f"still holds the turn, {fmt_age(idle)} without progress")
    if idle > env_num("AGENTCTL_RED_STALL", RED_STALL) + grace:
        return False, f"turn {rec.get('turn')}: no progress (transcript) for {fmt_age(idle)}"
    return True, f"turn {rec.get('turn')} at work"


def wrapper_last_words(m, n=3):
    """The last lines a fixer's wrapper wrote to its stderr (its lease meta's wrapper_log), joined; "" when none."""
    path = m.get("wrapper_log")
    lines = [x.strip() for x in tail_lines(path, 40)] if path and os.path.isfile(path) else []
    return " | ".join([x for x in lines if x][-n:])[:600]


def fixer_work(row, t):
    """(True|False|None, why): is a dispatched fixer's lease held by work? Its wrapper alive, its session not ended
    (beyond RED_ORPHAN_GRACE since the lease's last progress). A dead wrapper's why carries its last words (stderr)."""
    m = lease_meta(row)
    pid = m.get("pid")
    if pid and not pid_alive(int(pid)):
        words = wrapper_last_words(m)
        return False, f"its wrapper (pid {pid}) is gone" + (f"; its last words: {words}" if words else "")
    bid = m.get("bg_id")
    if not bid:
        return None, "its session is starting"
    ent = bg_session(claude_cli(), bid)
    if ent == {}:
        return None, "`claude agents` unread"
    idle = t - max(float(row.get("renewed_at") or row.get("acquired_at") or 0), mtime(m.get("log")))
    if (ent is None or bg_turn_over(ent, m.get("log"), t)) and idle > env_num("AGENTCTL_RED_ORPHAN_GRACE", RED_ORPHAN_GRACE):
        return False, (f"its session {bid} is {(ent or {}).get('state') or 'gone'}, "
                       f"{fmt_age(idle)} without progress")
    return True, "at work"


def holder_work(row, t, cache=None):
    """(True|False|None, why) for a live lease's holder: an owner's (its heartbeat or a red it holds) or a dispatched
    fixer's. None for what this machine cannot judge: a claim (renewed by its worktree's activity), a sibling (lives
    with its primary), a holder on another machine, or anything read on ci-runner."""
    if on_ci_runner() or not str(row.get("holder") or "").startswith("laptop:"):
        return None, "not this machine's"
    cache = {} if cache is None else cache
    m = lease_meta(row)
    # A PID IS JUDGED ONLY WHERE IT RUNS (2026-11-18 23:42:21Z, core Main Pipeline #1053): a fixer another machine
    # signed in as the laptop dispatched lost its lease 53 s after it was taken -- this tick found no pid 83138 among
    # its own processes. Every "laptop:" holder names its host in its meta; an older wrapper's (no host) is ours only
    # when its worktree is here.
    if m.get("host") and m["host"] != socket_host():
        return None, f"held on {m['host']}, judged there"
    if not m.get("host") and m.get("worktree") and m.get("kind") == "fixer" and not os.path.isdir(m["worktree"]):
        return None, "its worktree is not on this machine"
    if row["name"].startswith("owner/") or m.get("kind") in ("owner", "owned"):
        fam = m.get("family") or row["name"].split("/", 1)[1]
        k = ("owner", fam, m.get("pid"))
        if k not in cache:
            cache[k] = owner_work(fam, t, m.get("pid"))
        return cache[k]
    if m.get("kind") == "fixer":
        return fixer_work(row, t)
    return None, "not judged here"


def red_reap_orphans(store, t):
    """The laptop's tick: every live laptop-held fixer and owner lease whose holder is not at work is revoked
    (`ORPHANED: <why>`) -- the red offered again at once, its wrapper's next renewal lost. An orphaned fixer's session,
    if any still runs, is stopped (its wrapper is gone)."""
    acts, cache = [], {}
    for row in list(store.list("owner/")) + list(store.list("red/")):
        if not lease_is_live(row, t):
            continue
        ok, why = holder_work(row, t, cache)
        if ok is not False:
            continue
        if store.revoke(row["name"], f"ORPHANED: {row['holder']} holds it with no work ({why})"):
            m = lease_meta(row)
            if "wrapper" in why and "gone" in why and m.get("bg_id"):
                bg_stop(claude_cli(), m["bg_id"])
            line = f"RED ORPHANED LEASE: {row['name']} ({row['holder']}): {why}: revoked, offered again"
            inbox_write("red", line, [line], [], 1)
            acts.append(line)
    return acts + bg_reap_spent(t)


# ---------------------------------------------------------------- SPENT SESSIONS (2026-11-02)
# claude keeps a background session's process after its turn (`claude bg-spare`), and `claude --bg --resume <id>` of a
# session whose process still runs starts a COPY under a new id. The owner's wrapper stopped no finished turn: each
# turn resumed the last one's still-running session as a copy, and the spares piled up -- 11 idle `red owner ·
# convergence` sessions in 40 min, ~25 claude processes, 4 GB. The wrapper now stops each turn's session when the turn
# ends (end_turn: the next `--resume` continues the SAME session, under the same id, as the owner's design says); and
# whatever older code or a crash leaves, the laptop's tick stops every agentctl-launched session (by its label) that
# waits idle with its process alive and is not the current turn of a live wrapper. `claude stop` keeps the
# conversation (`claude attach` reopens it); sessions agentctl did not launch are never touched.
BG_LABELS = ("red owner · ", "red fixer #", "orphan · ", "lander · ")  # owner_turn's -n and fixer_label's
BG_SPENT_GRACE = 120  # s a session must have run before the reaper judges it ($AGENTCTL_BG_SPENT_GRACE)


def bg_live_turns():
    """The background ids that are the current turn of a live wrapper (an owner in its turn, a fixer running)."""
    live = set()
    for p in glob.glob(reds_dir("owners", "*", "owner.json")):
        r = read_record(p) or {}
        if (r.get("bg_id") and not r.get("ended") and r.get("in_turn", True)
                and proc_alive(r.get("pid"), r.get("process_start"))):
            live.add(r["bg_id"])
    live.update(r["bg_id"] for r in fixer_records() if r.get("alive") and r.get("bg_id"))
    return live


def bg_spent(rows, live, t):
    """The rows of `claude agents --json --all` that are agentctl's spent sessions: launched by it (BG_LABELS), waiting
    idle with their process alive (bg_waiting), not a live wrapper's current turn, and older than BG_SPENT_GRACE."""
    grace = env_num("AGENTCTL_BG_SPENT_GRACE", BG_SPENT_GRACE)
    out = []
    for x in rows:
        if not isinstance(x, dict) or x.get("kind") != "background":
            continue
        if not str(x.get("name") or "").startswith(BG_LABELS):  # never a session agentctl did not launch
            continue
        bid = str(x.get("id") or "")
        if not bid or bid in live or any(str(x.get("sessionId") or "").startswith(b) for b in live):
            continue
        if not bg_waiting(x) or t - float(x.get("startedAt") or 0) / 1000 < grace:
            continue
        out.append(x)
    return out


def bg_reap_spent(t):
    """The laptop's tick: every spent agentctl session stopped (bg_spent). Lines for what it did."""
    if on_ci_runner():
        return []
    cli = claude_cli()
    rows = bg_rows(cli)
    if rows is None:
        return []
    acts = []
    for x in bg_spent(rows, bg_live_turns(), t):
        bg_stop(cli, x["id"])
        acts.append(f"RED SESSION SPENT: {x['id']} ({x.get('name')}) idle with its process alive, no wrapper's turn: "
                    "stopped")
    return acts


def owner_still_ending(store, fam, t):
    """Why the family's next owner waits, or None: its last wrapper lost its heartbeat less than OWNER_TTL ago and its
    process still runs (it exits at its next beat; a new one now would race it for the record and the leases)."""
    rec = read_record(owner_record_path(fam)) or {}
    if not rec.get("pid") or rec.get("ended") or not proc_alive(rec["pid"], rec.get("process_start")):
        return None
    gone = float(lease_meta(store.get(owner_lease(fam))).get("released_at") or 0)
    if not gone or t - gone > OWNER_TTL:
        return None
    return f"its owner's last wrapper (pid {rec['pid']}) lost its heartbeat and is ending: the next owner starts after it"
