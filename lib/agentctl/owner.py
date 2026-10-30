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

MECHANISM_ONLY = (
    "MECHANISM ONLY (the owner's rule for convergence flips and drift): a fix names and fixes the INCREMENTAL STATE that "
    "diverges from a whole resolve (stage lag, the corrections budget, question-queue order, a cache, a restart key). "
    "Never add an identity resolver rule tuned on one recording (a rule change is identity design, measured on the full "
    "corpus, all countries, outside red fixing), and never add a row to the known-flip baseline or the allow-list to "
    "get green. ")


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
    try:
        with open(owner_leases_path(fam)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def owner_set_leases(fam, leases):
    os.makedirs(owner_dir(fam), exist_ok=True)
    path = owner_leases_path(fam)
    tmp = f"{path}.{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)  # lease secrets: the owner's eyes only
    with os.fdopen(fd, "w") as f:
        json.dump(leases, f)
    os.replace(tmp, path)


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
    forgejo/main, or its branch taken up again). The notebook and RED-INBOX.md are excluded from commits."""
    if rec.get("worktree") and os.path.isdir(rec["worktree"]):
        return rec["worktree"]
    _d, clone, _w = waiting_state(repo)
    slug_ = re.sub(r"[^A-Za-z0-9_.-]", "-", fam)
    wt, branch = f"{clone}-wt-owner-{slug_}", f"owner-{slug_}"
    if not os.path.isdir(wt):
        git(repo, "worktree", "prune", check=False)
        git(repo, "fetch", "--quiet", REMOTE, "main", check=False)
        has = git_ok(repo, "rev-parse", "--verify", "-q", f"refs/heads/{branch}")
        r = git(repo, "worktree", "add", "-q", *([wt, branch] if has else ["-b", branch, wt, f"{REMOTE}/main"]),
                check=False)
        if r.returncode != 0:
            return None
    try:
        common = git(wt, "rev-parse", "--git-common-dir", check=False).stdout.strip()
        excl = os.path.join(wt if not os.path.isabs(common) else "", common, "info", "exclude")
        os.makedirs(os.path.dirname(excl), exist_ok=True)
        have = open(excl).read() if os.path.exists(excl) else ""
        add = [p for p in (OWNER_NOTEBOOK, "RED-INBOX.md", "RED-NUDGE.md", "RED-STOP.md") if p not in have.split()]
        if add:
            with open(excl, "a") as f:
                f.write("\n".join(add) + "\n")
    except OSError:
        pass
    return wt


def owner_spawn(store, o_r, repo, fam, off, took, extra=()):
    """Start (or restart, after it died) the family's owner: its worktree and notebook kept, the taken leases handed
    to it, its wrapper detached. (ok, message)."""
    rec = read_record(owner_record_path(fam)) or {"family": fam, "restarts": -1}
    wt = owner_worktree(repo, fam, rec)
    if not wt:
        return False, f"RED OWNER {fam}: NOT STARTED: no worktree"
    for x in [{"name": off["lease"], **took}] + [dict(x) for x in extra]:
        owner_hand(fam, x["name"], x)
    nb = os.path.join(wt, OWNER_NOTEBOOK)
    if not os.path.exists(nb):
        with open(nb, "w") as f:
            f.write(f"# {fam} red family: the owner's notebook\n\nKept by the family's owner session across reds and "
                    "restarts: reproducers, hypotheses tried (and how they ended), what is left.\n")
    restarted = bool(rec.get("started"))
    rec.update(family=fam, worktree=wt, notebook=nb, repo=repo, o_r=o_r, started=ts(), ended=None, verdict=None,
               turn=0, bg_id=None, pid=None,
               restarts=int(rec.get("restarts", -1)) + 1, log=os.path.join(owner_dir(fam), "owner.log"))
    write_json(owner_record_path(fam), rec)
    env = fixer_env(o_r, f"owner-{fam}")
    with open(rec["log"], "a") as logf:
        p = spawn_detached([sys.executable, TOOL_PATH, "red", "owner", "--family", fam, "--repo", repo],
                           stdin=subprocess.DEVNULL, stdout=logf, stderr=logf, start_new_session=True, cwd=wt, env=env)
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
            + (MECHANISM_ONLY if fam == "convergence" else "")
            + "End each turn with ONE verdict line for the turn's reds (LANDED <sha> / GAVE UP: / DIAGNOSED: / BLOCKED "
              "ON OWNER:). ")


def owner_turn_prompt(fam, rec, st, names, first):
    """The turn's prompt: the first red's task (fixer_prompt) in the owner's voice, every red of the turn listed."""
    active = red_active(st)
    its = [active[n.split("/", 1)[1]] for n in names if n.split("/", 1)[1] in active]
    reds = [f"{red_what(i['data'])} (red {i['data'].get('key')}): {str(i['data'].get('line') or '')[:160]}" for i in its]
    if not its:
        return None
    d = its[0]["data"]
    u = {"workflow": d.get("workflow"), "job": d.get("job") or (d.get("jobs") or ["?"])[0], "jobs": d.get("jobs"),
         "kind": d.get("kind") or "test", "line": d.get("line"), "run": d.get("last_run") or {}, "key": d.get("key"),
         "refuted": d.get("refuted"), "predicts": d.get("predicts"), "flake": d.get("red_kind") == "flake",
         "diagnosis": d.get("diagnosis"), "prior_work": d.get("prior_work"), "old_note": d.get("old_note")}
    note = None if first else ("NEXT TURN: the reds above are new or offered to you again (a previous verdict did not "
                               "clear them): read RED-INBOX.md and your notebook, then go on.")
    return fixer_prompt(u, rec["worktree"], sync_brief(), note,
                        owner={"family": fam, "notebook": rec["notebook"], "reds": reds})


def red_owner(a):
    """THE OWNER'S WRAPPER (detached; `agentctl red owner --family F`): its heartbeat lease, its reds' leases renewed
    with it, one session resumed turn after turn, each turn's leases released with the turn's verdict."""
    fam = a.family
    path = owner_record_path(fam)
    rec = read_record(path) or {}
    wt = rec.get("worktree")
    me = os.getpid()
    st = CurrentStore()
    holder = owner_holder(fam)
    hb = st.acquire(owner_lease(fam), holder, OWNER_TTL, OWNER_MAX, progress=f"beat {int(time.time())}",
                    meta={"kind": "owner", "family": fam, "worktree": wt, "pid": me, "notebook": rec.get("notebook")})
    if "held" in hb:
        print(f"RED OWNER {fam}: DUPLICATE ({hb['held']['holder']} is alive)")
        return 0
    rec.update(pid=me, process_start=process_start(me))
    write_json(path, rec)
    cli = claude_cli()
    env = {k: v for k, v in os.environ.items() if k not in FIXER_IDENTITY_ENV and k != "AGENTCTL_DETACHED"}  # an agent
    every = env_num("AGENTCTL_RED_RENEW", RED_RENEW_EVERY)
    poll = env_num("AGENTCTL_BG_POLL", BG_POLL)
    idle_max = env_num("AGENTCTL_OWNER_IDLE", OWNER_IDLE)
    bid, turn, turn_names, empty, last_beat, idle_since, first = None, 0, [], 0, 0.0, time.time(), True
    stop = None

    def release(names, outcome, notes=""):
        leases = owner_leases(fam)
        for n in names:
            x = leases.get(n)
            if x:
                try:
                    st.release(n, x["epoch"], x["secret"], outcome=outcome[:600], notes=notes)
                except StoreUnavailable:  # it lapses by its TTL instead: the red is offered again
                    pass
        owner_drop(fam, names)

    def cancelled(signum, _frame):
        if bid:
            bg_stop(cli, bid)
        release(list(owner_leases(fam)), "STOPPED: the owner's wrapper was stopped")
        try:
            st.release(owner_lease(fam), hb["epoch"], hb["secret"], outcome="STOPPED")
        except StoreUnavailable:
            pass
        os._exit(143)
    for sg in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        signal.signal(sg, cancelled)
    while stop is None:
        now = time.time()
        if now - last_beat >= every:  # THE HEARTBEAT: the owner's lease and every red lease it holds
            try:
                if st.renew(owner_lease(fam), hb["epoch"], hb["secret"], f"beat {int(now)}", OWNER_TTL) == "lost":
                    stop = "STOPPED: the owner's heartbeat lease was lost"
                    break
                lost = [n for n, x in owner_leases(fam).items()
                        if st.renew(n, x["epoch"], x["secret"], f"beat {int(now)}", OWNER_TTL) == "lost"]
                if lost:  # cleared, released by hand, or revoked: no longer the owner's
                    owner_drop(fam, lost)
                last_beat = now
            except StoreUnavailable as e:
                print(f"{ts()} the store did not answer ({e}): asked again at the next beat", flush=True)
        if os.path.exists(os.path.join(wt, "RED-STOP.md")):
            stop = "STOPPED: " + open(os.path.join(wt, "RED-STOP.md")).readline().strip()
            break
        held = list(owner_leases(fam))
        if bid is None:  # between turns: a new turn when the owner holds reds, else idle
            if held:
                idle_since = now
                prompt = owner_turn_prompt(fam, rec, st, held, first)
                if prompt is None:  # its reds closed meanwhile
                    owner_drop(fam, held)
                    continue
                argv = [cli, "--bg", prompt, "--permission-mode", interactive_mode(), "--permission-prompts", "none",
                        "-n", f"red owner · {fam}", "--disallowedTools", "Bash(git push:*)", "Bash(*--no-verify*)",
                        "Bash(*PREPUSH_SKIP*)"]
                if rec.get("session"):
                    argv += ["--resume", rec["session"]]
                bid, why = bg_start(argv, wt, env)
                if not bid:
                    release(held, f"NOT STARTED: `claude --bg` {why}")
                    stop = f"NOT STARTED: `claude --bg` {why}"
                    break
                turn, turn_names, first = turn + 1, held, False
                rec.update(bg_id=bid, turn=turn)
                write_json(path, rec)
            elif now - idle_since >= idle_max:
                stop = f"IDLE: no red of {fam} for {fmt_age(idle_max)}"
                break
        else:
            ent = bg_session(cli, bid)
            if ent and ent.get("sessionId") and rec.get("session") != ent["sessionId"]:
                rec.update(session=ent["sessionId"], transcript=bg_transcript(ent["sessionId"]))
                write_json(path, rec)
                bind_fixer_session(rec["session"], path)  # the session is a fixer's: ~/bin refuses its commits
            if ent is None or (ent or {}).get("state") == "done":  # the turn ended
                text = bg_final_text(rec.get("transcript") or bg_transcript(rec.get("session") or ""))
                verdict = fixer_verdict(text)
                tail = [l[:300] for l in text.splitlines()[-INBOX_TAIL:]]
                empty = 0 if verdict else empty + 1
                outcome = verdict or "ended with no LANDED / GAVE UP line"
                line = f"RED OWNER {fam} turn {turn}: {outcome[:300]}"
                inbox_write("red fixer", line, [line], tail, 0 if outcome.startswith("LANDED") else 1, cwd=wt)
                release(turn_names, outcome, "\n".join(tail[-8:]))
                bid = None
                if empty >= OWNER_EMPTY_TURNS:
                    stop = f"ENDED: {empty} turns in a row with no verdict"
        time.sleep(poll)
    if bid:
        bg_stop(cli, bid)
    release(list(owner_leases(fam)), stop or "ENDED")
    try:
        st.release(owner_lease(fam), hb["epoch"], hb["secret"], outcome=stop or "ENDED")
    except StoreUnavailable:
        pass
    rec.update(ended=ts(), verdict=stop)
    write_json(path, rec)
    print(f"RED OWNER {fam}: {stop}")
    return 0


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
