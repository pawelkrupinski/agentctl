# agentctl, part `red`: the red machine.
# Runs inside agentctl's one namespace, after the parts before it in lib/agentctl/__init__.py's PARTS (it uses their names).

# ---------------------------------------------------------------- red: every red build held by a lease, or offered
# -- reds as items, held by leases (design §5.3, §6, §7.2; reconciler phase 1, step 10)
# A red is an ITEM in the store (type "red"), keyed by its failing test or step -- the workflow and the normalized first
# error line, the job only when no line was read (the owner: a red is its failure, never its job name) -- and a newer
# run failing the same way attaches to the same item. Nothing holds a red but a lease, `red/<key>`: a dispatched
# fixer's (renewed by its own progress: transcript bytes, HEAD, `git status`), an interactive claim's (renewed by the
# tick from its worktree's activity; it must name a worktree, never the root checkout), or none. Every non-terminal red
# has, at every tick, a live lease, an open offer, a fix on its way (fix-pending) or an owner block: NOBODY is not a
# state. An offer nobody takes escalates every cycle, naming why (capacity, dispatch OFF); a code red is never parked
# on the owner (the tier ladder has no owner rung); a refuted fix drops its holder's lease; the observation that opened
# a red (its failing run) is the observation that clears it (a newer decided run without the failure).
RED_HELP = """Reds on main of every watched repo (red-repos.json: movies, infra, core) as items, each held by a LEASE
red/<key>. A red is keyed by its failing test or step (workflow + normalized first error line): a newer run failing
the same way is the same red. A run number is per repo: --repo, or the repo of the current directory.

  agentctl red status [-v]                       every red: state, holder or offer, deadline
  agentctl red claim <run|key> [--worktree DIR] [--note "<what>"]
                                                 take its lease (30 min, renewed by your worktree's activity; 8 h max)
  agentctl red beat <run|key> --note "<what>"    renew it by hand (a new note is progress; the same note is not)
  agentctl red release <run|key> [--any]         drop your lease (--any: anyone's)
  agentctl red block <run> --owner-cause KIND --cmd "<command>" [--cause NAME] [--until-green WF | --until-http URL | --until-tcp HOST:PORT]
                                                 an external cause only the owner can act on (never a test failure)
  agentctl red owner-push <run> [--note "<what; which units restart>"]
                                                 an owner-only repo's (infra's) prepared fix, filed with the owner as a
                                                 one-liner; the red waits in fix-pending until a green gate run
  agentctl red dispatch <run|key>                its offer at top priority: the dispatcher takes it now
  agentctl red auto [on|off|status]              the dispatcher on or off

States: red -> offered (author 10 min, gate 10 min, others 30 min; past it: escalated, widened, again) -> leased ->
fix-pending (a Fixes-Red change submitted or landed, until a run proves it) -> verified; cleared when a newer run no
longer fails that way. Reference: ~/bin/AGENTCTL.md ("red")."""
RED_CLAIM_TTL, RED_CLAIM_MAX = 30 * 60, 8 * 3600      # an interactive claim
RED_FIXER_TTL, RED_FIXER_MAX = 15 * 60, 3 * 3600      # a dispatched fixer
RED_OFFER = {"author": 10 * 60, "gate": 10 * 60, "other": 30 * 60, "flake": 2 * 3600}
RED_FIX_RUN_START = 30 * 60  # a landed fix's verifying run must exist by then (outside the gate: dispatched)
RED_FIX_DECIDE = 3 * 3600
# THE STATE MACHINE IS TOTAL (the owner, 2026-10-29): no red state waits on an event that may never come. A fix-pending
# whose fix is not proven by RED_FIX_GIVEUP after it entered fix-pending (a landed fix no run checks, a verify run an
# on-demand workflow never gets, a fix id that is None) is offered again with that fix set aside (`unverified`): a
# fixer decides, never a resting state.
RED_FIX_GIVEUP = 2 * RED_FIX_DECIDE
RED_PRIORITY = {"gate": 0, "fix": 1, "outside": 2, "flake": 3}
RED_RECURRING = 3            # landed fixes the same failure outlived: `recurring`, and its fixer hunts the root cause
RED_TICK_INTERVAL = 15       # seconds between the processor's red ticks ($AGENTCTL_RED_TICK)
RED_FIXER_WALL = 3 * 3600    # a dispatched fixer's maximum wall time ($AGENTCTL_RED_FIXER_WALL)
RED_RENEW_EVERY = 60         # a fixer's wrapper renews its lease this often
RED_RELEASE_ASKS = 12        # ...and asks a store that does not answer this many times, a poll apart, to release it
STUCK_FACTOR, STUCK_FLOOR = 3, 30 * 60  # audit #8: a run undecided past max(3 x its workflow's p90, 30 min) is a red
BRIEF = os.path.join(HOME, "bin", "briefs", "movies-agent-preamble.md")
BRIEF_SRC = ""  # a file to use instead of the tracked docs/fixer-brief.md in fixers' prompts (an override): none by default
RED_TERMINAL = ("verified", "cleared")


# THE TIER LADDER (design §5.3), a table: (attempts, condition, tier), the first row whose attempts and condition hold.
# Infra-shaped reds (no test failure) get the infra-diagnose tier only. There is no owner rung: a code red is the code's.
RED_LADDER = ((0, "author", "author"), (0, None, "fresh"),
              (1, "resumable", "resume"), (1, None, "fresh"),
              (2, "culprit", "revert"), (2, None, "fresh"),
              (3, None, "deep"))


def red_tier(d, culprit=None):
    if (d.get("crash_loop") or {}).get("stage") == "deep":  # the crash-loop breaker moved it up (red_crash_loop)
        return "deep"
    # THE DECISION RULE (blame.py, blame_decision): while its culprit is decided the red waits (tier `blame`); a
    # determined culprit is reverted and its lander revived (tier `lander`, once); only an undetermined one goes up the
    # ladder below to a fresh fixer
    who, _why = blame_decision(d.get("blame")) if d.get("blame") else ("fixer", None)
    if who is None:
        return "blame"
    if who in ("lander", "owner") and not d.get("lander_tried"):
        return "lander"
    if d.get("kind") in ("infra", "unclear") and d.get("red_kind") != "flake" and not d.get("diagnosis") \
            and int(d.get("attempts") or 0) < 3:
        return "infra"
    last = (d.get("history") or [{}])[-1]
    conds = {"author": bool((d.get("author") or {}).get("worktree")) and not d.get("author_tried"),
             "resumable": bool(last.get("session") and last.get("worktree")),
             "culprit": bool(culprit) and not d.get("reverted")}
    a = min(int(d.get("attempts") or 0), 3)
    for n, cond, tier in RED_LADDER:
        if n == a and (cond is None or conds[cond]):
            return tier
    return "deep"


def canon_job(job):
    """A job's name as red keys have always spelled it: ci.yml's page tests were `page tests (<engine> / <variant>)` until
    2026-11-01 and are now `page-tests-<engine> (<variant>)`; the new name keys as the old, so an open red keeps its key."""
    import re
    m = re.fullmatch(r"((?:\S+ / )*)page-tests-(safari|chrome) \((.+)\)", job or "")
    return f"{m.group(1)}page tests ({m.group(2)} / {m.group(3)})" if m else job


def red_key(workflow, job, line):
    """A red's identity: its workflow and normalized first error line (the failing test or step); the job only when no
    line was read. Never the job name alone: matrix jobs failing the same way are one red."""
    import hashlib
    n = norm_line(line)
    return hashlib.sha1((f"{workflow}\0{n}" if n else f"{workflow}\0job {canon_job(job)}").encode()).hexdigest()[:12]


def red_lease(key):
    return f"red/{key}"


def lease_is_live(row, t=None):
    return bool(row and row.get("holder") and row["expires_at"] > (t if t is not None else time.time()))


def lease_meta(row):
    try:
        return json.loads((row or {}).get("meta") or "{}")
    except ValueError:
        return {}


def red_active(store):
    """{key: item} of the non-terminal reds (one per key)."""
    out = {}
    for it in store.items("red"):
        if it["state"] not in RED_TERMINAL:
            out[it["data"].get("key") or it["id"]] = it
    return out


def red_active_of(store, o_r):
    """red_active, only o_r's reds: another watched repo's own, else the primary's (a red with no `repo`)."""
    mine = o_r if red_is_other(o_r) else RED_PRIMARY
    return {k: it for k, it in red_active(store).items() if red_repo_of(it["data"]) == mine}


def red_obs_for(o_r, obs):
    """Observations keyed for the store: another repo's keys bound to it (red_repo_key), the primary's as they are."""
    if not red_is_other(o_r):
        return obs
    out = {}
    for key, ob in obs.items():
        k = red_repo_key(o_r, key)
        out[k] = dict(ob, key=k)
    return out


def red_item_of_run(store, n, workflow=None):
    """The non-terminal reds a run number or id names (newest spell first)."""
    n = str(n).lstrip("#")
    return [it for it in red_active(store).values() if n in set(it["data"].get("runs") or [])
            and (workflow is None or it["data"].get("workflow") == workflow)]


# -- what is red now: one observation per key, from the CI-state snapshot
STREAK_REREADS = 3  # stale or missing streak run signatures read again per tick ($AGENTCTL_STREAK_REREADS)


def streak_sigs(o_r, name, r, budget):
    """A streak run's signatures: its cached record when the classifier that wrote it is this one; else read again
    through run_sigs while `budget` (a one-element list, the tick's) lasts; else the stale record's (the next tick reads
    it). A raw stale record keyed #2406 by its old WARN line while #2493 was keyed by its annotation: the streak never
    named #2406, so the waiting `Fixes-Red: 2406` fix never held #2493's red."""
    rec = read_record(reds_dir("runsig", f"{r.get('databaseId')}.json"))
    if rec is not None and rec.get("classifier") == classifier_version():
        return rec.get("sigs") or []
    if budget[0] > 0 and r.get("status") == "completed" and r.get("conclusion") in RED_CONCLUSIONS:
        budget[0] -= 1
        try:
            return run_sigs(o_r, r, name)
        except (Exception, SystemExit) as e:
            err(f"red: could not read the failed jobs of run {r.get('databaseId')} ({str(e)[:120]})")
    return (rec or {}).get("sigs") or []


def red_units(o_r, name, decided, runs, sigs, gate, budget=None):
    """The failing jobs of one red run as units: [{workflow, job, kind, line, run, runs, names, gate}]. budget: the
    tick's re-reads of stale streak signatures (a one-element list; default STREAK_REREADS)."""
    sruns = streak_runs(runs) if runs else []
    budget = budget if budget is not None else [int(env_num("AGENTCTL_STREAK_REREADS", STREAK_REREADS))]
    streak = {}
    units = []
    sigs = [s for s in sigs if s.get("kind") != "incomplete"]  # a `verdict complete` beside a real failure is no red
    # (job_is_verdict_report: reporting only)
    for s in sigs:
        names = set(run_names(decided))
        for r in sruns:  # the streak's runs that failed THIS way (their signatures, by this classifier)
            if r.get("databaseId") == decided.get("databaseId"):
                continue
            rid = r.get("databaseId")
            if rid not in streak:
                streak[rid] = streak_sigs(o_r, name, r, budget)
            if any(x.get("job") == s["job"] and norm_line(x.get("line")) == norm_line(s["line"]) for x in streak[rid]):
                names |= run_names(r)
        units.append({"workflow": name, "job": s["job"], "kind": s["kind"], "line": s["line"], "run": decided,
                      "runs": runs, "names": names, "gate": gate})
    if not units:  # no failed job listed (or none readable): the run itself is the red
        units.append({"workflow": name, "job": "?", "kind": "unclear", "line": "", "run": decided, "runs": runs,
                      "names": set(run_names(decided)), "gate": gate})
    return units


def sigs_no_verdict(sigs):
    """A run's signatures say it decided nothing: its only failures are `verdict complete` (kind incomplete)."""
    return {s.get("kind") for s in sigs or []} == {"incomplete"}


def gate_red_units(o_r, rows, others=(), rereads=None):
    """The red units of ci-why's rows: [(name, decided, runs, gate)] -> units (signatures read, cached; at most
    `rereads` (STREAK_REREADS) stale streak runs read again per call -- the tick's; the status line's summary none)."""
    units = []
    budget = [int(env_num("AGENTCTL_STREAK_REREADS", STREAK_REREADS)) if rereads is None else rereads]
    for name, decided, runs, gate in list(rows) + list(others):
        if not decided or decided.get("conclusion") not in RED_CONCLUSIONS:
            continue
        try:
            sigs = run_sigs(o_r, decided, name)
        except (Exception, SystemExit) as e:
            err(f"red: could not read the failed jobs of run {decided.get('databaseId')} ({str(e)[:120]})")
            sigs = []
        if sigs_no_verdict(sigs):
            continue  # an incomplete run (only `verdict complete` failed): no verdict, no red, no fixer
        units += red_units(o_r, name, decided, runs, sigs, gate, budget)
    return units


KIND_ORDER = {"test": 0, "unclear": 1, "infra": 2}


def red_obs_of_units(units):
    """{key: observation}: the units grouped by key (a matrix of jobs failing the same way is one red)."""
    obs = {}
    for u in sorted(units, key=lambda u: KIND_ORDER.get(u["kind"], 1)):
        lineless = not norm_line(u.get("line")) or str(u.get("line")).startswith("step failed:")
        # an infra-shaped failure with no error line of its own (a runner, a crashed leg, a timing report that failed
        # because others did): ONE red for the workflow's run, never one per job -- one cause, one fixer
        key = u.get("key") or (red_key(u["workflow"], "infra", "infra-shaped, no error line")
                               if u["kind"] in ("infra", "unclear") and lineless else red_key(u["workflow"], u["job"], u["line"]))
        o = obs.get(key)
        run = {k: u["run"].get(k) for k in ("number", "databaseId", "headSha", "createdAt", "updatedAt")}
        if o is None:
            red_kind = u.get("red_kind") or ("predicts" if u.get("predicts") else "local" if u["workflow"] == LOCAL_FLOW
                                             else "gate" if u["gate"] else "outside")
            obs[key] = {"key": key, "workflow": u["workflow"], "jobs": [u["job"]], "job": u["job"], "kind": u["kind"],
                        "line": (u.get("line") or "")[:300], "run": run, "names": set(u.get("names") or ()),
                        "gate": bool(u["gate"]), "red_kind": red_kind, "predicts": u.get("predicts"),
                        "runs_list": u.get("runs") or []}
        else:
            o["jobs"] = sorted(set(o["jobs"]) | {u["job"]})
            o["names"] |= set(u.get("names") or ())
    return obs


def stuck_obs(snap):
    """Audit #8: the snapshot's stuck runs (undecided past STUCK_FACTOR x their workflow's p90) as infra reds."""
    units = []
    for s in (snap or {}).get("stuck") or []:
        if s.get("queued"):
            continue  # waiting its turn behind busy runners: the queue, not a stuck run
        r = s["run"]
        units.append({"key": red_key(s["name"], "stuck", "stuck run"), "workflow": s["name"], "job": "stuck",
                      "kind": "infra", "red_kind": "stuck-run", "gate": s["name"] in {g["name"] for g in snap.get("gate") or []},
                      "line": f"run #{r.get('number')} undecided for {fmt_age(s['age'])} (limit {fmt_age(s['limit'])})",
                      "run": r, "runs": [], "names": run_names(r)})
    return red_obs_of_units(units)


def stuck_runs(lists, now_=None, o_r=None, jobs_of=None, queue=None):
    """[{name, run, age, limit[, queued]}]: per workflow, its undecided runs older than max(STUCK_FACTOR x the p90 of its
    decided runs' durations, STUCK_FLOOR). A run's age counts from its first job's start (Convergence bisect #2385, run
    3568: an hour queued behind a measurement holding every `measure` slot was called a stuck red): a run none of whose
    jobs started, waiting only on labels whose every runner is up with all slots busy, carries `queued` and is no red."""
    t = now_ if now_ is not None else time.time()
    jobs_of = jobs_of or (lambda r: run_jobs(o_r, r))
    out = []
    for name, runs in lists.items():
        durs = []
        for r in runs:
            if r.get("status") == "completed" and r.get("createdAt") and r.get("updatedAt"):
                try:
                    durs.append(parse_time(r["updatedAt"]).timestamp() - parse_time(r["createdAt"]).timestamp())
                except (ValueError, TypeError):
                    pass
        limit = max(STUCK_FACTOR * (pct(durs, 90) if durs else 0), STUCK_FLOOR)
        for r in runs:
            if r.get("status") != "completed" and r.get("createdAt"):
                try:
                    age = t - parse_time(r["createdAt"]).timestamp()
                except (ValueError, TypeError):
                    continue
                if age <= limit:
                    continue
                jobs = jobs_of(r)
                starts = sorted(parse_time(j["startedAt"]).timestamp() for j in jobs or [] if j.get("startedAt"))
                queued = None
                if starts:
                    age = t - starts[0]
                elif jobs is not None:  # none started: queued, not stuck, when every runner that could take it is busy
                    queued = queue_full(o_r, r.get("number"), runner_queue(t) if queue is None else queue)
                if age > limit:
                    s = {"name": name, "run": {k: r.get(k) for k in ("number", "databaseId", "headSha", "createdAt")},
                         "age": age, "limit": limit}
                    out.append({**s, "queued": queued} if queued else s)
    return out


def run_jobs(o_r, run):
    """The run's jobs ([{name, status, startedAt, ...}]), or None when they cannot be read."""
    try:
        return gh_json(o_r, "run", "view", str(run.get("databaseId")), "--json", "jobs").get("jobs") or []
    except (Exception, SystemExit):
        return None


RUNNERS_PAGE_MAX_AGE = 5 * 60  # ci-runners-page reads GitHub's runners and jobs every ~15 s


def runner_queue(now_=None):
    """ci-runners-page's last good read of ci-runner (`running` tasks per runner, `waiting_jobs` per label, each runner's
    `limits`), or None when it is missing or older than RUNNERS_PAGE_MAX_AGE."""
    path = os.environ.get("AGENTCTL_RUNNERS_PAGE_CACHE", f"{HOME}/.cache/ci-runners-page/last-good.json")
    try:
        with open(path) as f:
            q = json.load(f)
        fresh = (now_ if now_ is not None else time.time()) - float(q["ok"]) <= RUNNERS_PAGE_MAX_AGE
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return q if fresh else None


def queue_full(o_r, number, q):
    """'queued: <label> full (N/N busy, M ahead, oldest Xm)' when run #number of o_r waits only on labels whose every
    serving runner is up with all its slots busy, else None (a free slot, a runner down, or nothing known: stuck)."""
    if not q or number is None:
        return None
    waits = q.get("waiting_jobs") or []
    mine = [w for w in waits if w.get("repo") == o_r and str(w.get("run")) == str(number)]
    if not mine:
        return None
    says = []
    for label in sorted({w.get("label") for w in mine}):
        try:
            want = set(json.loads(label))
            serving = {n: r for n, r in ((q.get("limits") or {}).get("runners") or {}).items()
                       if want <= set(json.loads(r.get("labels") or "[]"))}
        except (ValueError, TypeError):
            return None
        if not serving or any(r.get("state") != "running" for r in serving.values()):
            return None
        cap = sum(int(r.get("capacity") or 0) for r in serving.values())
        busy = sum(1 for x in q.get("running") or [] if x.get("runner") in serving)
        if busy < cap:
            return None
        queue_ = [int(w.get("secs") or 0) for w in waits if w.get("label") == label]
        own = max(int(w.get("secs") or 0) for w in mine if w.get("label") == label)
        says.append(f"{', '.join(sorted(want))} full ({busy}/{cap} busy, {sum(1 for x in queue_ if x > own)} ahead, "
                    f"oldest {fmt_age(max(queue_))})")
    return "queued: " + "; ".join(says)


UNKICKED_AFTER = 60 * 60  # a convergence-relevant main push with no convergence run on main by then is a red
UNKICKED = {"pawelkrupinski/movies": ("Identity model convergence", ".github/convergence-paths.txt",
                                      ".github/actions/changed-paths/matches.sh")}


def convergence_relevant(repo, base, head, paths_file, matcher):
    """True / False: does base..head touch a path that can change the suite's verdict (main's own list and matcher)?
    None when it cannot be told here."""
    import tempfile
    d = git(repo, "diff", "--name-only", base, head, check=False)
    if d.returncode != 0:
        return None
    with tempfile.TemporaryDirectory() as tmp:
        files = {}
        for name, path in (("paths", paths_file), ("matcher", matcher)):
            r = git(repo, "show", f"{head}:{path}", check=False)
            if r.returncode != 0:
                return None
            files[name] = os.path.join(tmp, name)
            with open(files[name], "w") as f:
                f.write(r.stdout)
        try:
            r = subprocess.run(["bash", files["matcher"], files["paths"]], input=d.stdout, capture_output=True, text=True,
                               timeout=30)
        except (OSError, subprocess.SubprocessError):
            return None
    out = r.stdout.strip()
    return True if out == "true" else False if out == "false" else None


def unkicked_obs(o_r, repo, snap, now_=None):
    """THE MISSING RUN IS A RED (2026-10-28: main had no convergence run for six hours while its kick diffed against a
    measurement's run): origin/main's head, when it touches the suite's paths since the suite's newest MAIN run and is
    older than UNKICKED_AFTER, with no main run of the suite started since it, is an infra red until one runs."""
    spec = UNKICKED.get(o_r)
    if not spec or not repo or not snap:
        return {}
    name, paths_file, matcher = spec
    runs = next((g.get("runs") or [] for g in snap.get("gate") or [] if g["name"] == name), [])
    if not runs:
        return {}
    head = git(repo, "rev-parse", f"refs/remotes/{remote()}/main", check=False).stdout.strip()
    ct = git(repo, "log", "-1", "--format=%ct", head, check=False).stdout.strip() if head else ""
    if not head or not ct.isdigit():
        return {}
    t = now_ if now_ is not None else time.time()
    if t - float(ct) < env_num("AGENTCTL_UNKICKED_AFTER", UNKICKED_AFTER):
        return {}
    newest = runs[0]
    try:
        if parse_time(newest.get("createdAt")).timestamp() >= float(ct):
            return {}  # a run started since the push: kicked
    except (ValueError, TypeError, AttributeError):
        return {}
    base = newest.get("headSha")
    if not base or not git_ok(repo, "cat-file", "-e", f"{base}^{{commit}}") \
            or convergence_relevant(repo, base, head, paths_file, matcher) is not True:
        return {}
    u = {"key": red_key(name, "kick", "convergence not kicked"), "workflow": name, "job": "kick", "kind": "infra",
         "red_kind": "stuck-run", "gate": True,
         "line": f"no {name} run on main for {head[:10]} ({fmt_age(t - float(ct))} after it; the newest is "
                 f"#{newest.get('number')} on {str(base)[:10]}): Main's kick-convergence did not dispatch it",
         "run": {"number": f"unkicked-{head[:10]}", "databaseId": None, "headSha": head}, "runs": [], "names": set()}
    return red_obs_of_units([u])


def red_observe(o_r, repo, snap):
    """{key: observation} of everything red on main now: the gate, every other workflow (its newest decided run, audit
    #7), main's local checks, the branch runs that predict main red, and the stuck runs (audit #8)."""
    rows = [(n, d, _gate_lists.get(n, []), True) for n, d, _nw, _s in gate_from_snap(snap)]
    others = [(n, r, _outside_lists.get(n, []), False) for n, r, _s, _c in outside_from_snap(snap)]
    units = gate_red_units(o_r, rows, others) + local_units(repo) + predict_units(o_r, snap)
    obs = red_obs_of_units(units)
    obs.update(stuck_obs(snap))
    try:
        obs.update(unkicked_obs(o_r, repo, snap))
    except (Exception, SystemExit) as e:
        err(f"red: could not tell whether the convergence was kicked ({str(e)[:120]})")
    return obs


def red_rekeyed(d, obs):
    """A line-less or unreadable red whose own run is now read in full under other keys (its jobs merged into the run's
    one infra red, or its failed jobs read at last): superseded by that red, never 'cleared by a newer run'."""
    if d["key"] in obs or not (d.get("job") == "?" or not norm_line(d.get("line"))
                               or str(d.get("line")).startswith("step failed:")):
        return False
    rid = (d.get("last_run") or {}).get("databaseId")
    same_run = [o for o in obs.values() if o["workflow"] == d.get("workflow") and o["run"].get("databaseId") == rid]
    return bool(same_run) and all(o["job"] != "?" for o in same_run)


def red_merge_rekeyed(store, active, obs):
    """Reds keyed apart that the classifier now reads as ONE failure: an open red keyed by an OLDER classifier (its
    `classifier`, recorded when it was opened or last observed) whose key is not observed any more,
    while a run of its own now fails the same job under another open red's key (the streak's signatures read again by
    this classifier), is merged into that red -- its runs (the names a `Fixes-Red:` change links by), history, refuted
    fixes and attempts kept -- and closed as re-keyed. Verify scrape recording #2406 (a WARN line), #2481 (a
    FileNotFoundException) and #2493 (the GermanyConvergenceSpec annotation) were one failure as three reds, and the
    fix waiting under `Fixes-Red: 2406` never held #2493's. Returns what it did; `active` is updated in place."""
    acts = []
    if not obs:
        return acts
    cv = classifier_version()
    for key, it in list(active.items()):
        d = it["data"]
        if key in obs or it["state"] in RED_TERMINAL or d.get("red_kind") in ("local", "stuck-run", "predicts") \
                or d.get("classifier") == cv:
            continue  # keyed by THIS classifier: a re-read of its run under other keys clears nothing (cf. red_gone)
        runs = set(map(str, d.get("runs") or []))
        jobs = {str(j) for j in (d.get("jobs") or [d.get("job")]) if j and j != "?"}
        into = [o for o in obs.values() if o["key"] != key and o["key"] in active and o["workflow"] == d.get("workflow")
                and runs & set(map(str, o.get("names") or ())) and jobs & set(o.get("jobs") or ())]
        if not into:
            continue
        target = store.item("red", active[into[0]["key"]]["id"])
        td = target["data"]
        merged = red_import_merge(td, d)
        merged["refuted"] = sorted(set(td.get("refuted") or []) | set(d.get("refuted") or []))
        merged["merged_from"] = sorted(set(td.get("merged_from") or []) | {key})
        store.update_item("red", target["id"], target["version"],
                          {k: v for k, v in merged.items() if k not in RED_DERIVED_FIELDS and k != "key"})
        active[td["key"]] = store.item("red", target["id"])
        red_close(store, store.item("red", it["id"]), "cleared",
                  f"re-keyed: the same failure as red {td['key']} ({red_what(td)}), merged into it with its runs "
                  f"{', '.join(sorted(runs))}")
        active.pop(key, None)
        acts.append(f"merged red {key} ({red_what(d)}) into {td['key']}: one failure, re-keyed")
    return acts


def red_gone_why(d, obs):
    """Why red_gone holds, for the cleared red's line."""
    if obs is not None and red_rekeyed(d, obs):
        return "re-keyed: its run's failures are held by the run's other reds"
    return "a newer decided run on main does not fail this way"


def red_gone(it, obs, snap):
    """The observation that opened the red, now negative: True when a NEWER decided run of its workflow does not fail
    this way (local: main's local checks no longer fail it; stuck: no stuck run; a branch prediction: gone)."""
    d = it["data"]
    if d["key"] in obs:
        return False
    if red_rekeyed(d, obs):
        return True
    if d.get("red_kind") == "flake":
        return False  # a flake is already gone: only its fix ends it
    if d.get("red_kind") in ("local", "stuck-run", "predicts"):
        return True
    # ONLY a newer DECIDED run of the same workflow ON MAIN clears it (the snapshot's `latest` reads main's runs only;
    # a branch or measure run never counts)
    last = ((snap or {}).get("latest") or {}).get(d.get("workflow"))
    return bool(last) and last.get("headBranch", "main") == "main" and \
        int(last.get("databaseId") or 0) > int((d.get("last_run") or {}).get("databaseId") or 0)


# -- fixes on their way: Fixes-Red changes in the queue, and Fixes-Red commits landed on main
FIX_OPEN = ("queued", "checking", "checked", "waiting", "joined", "pushing")


def landed_fix_commits(repo):
    """[(sha, commit time, {values}, subject)]: Fixes-Red commits on origin/main in the last 3 days -- but for one whose
    `Red-Unrelated:` trailer says it names the red only to pass the gate (`red_unrelated`): landed, it is no more a fix
    than queued. Identity model convergence #2432 (2026-10-28): four CI speed-ups carrying `Fixes-Red: 2392` beside one
    were each read as the red's fix on its way, its live fixer stopped, then refuted -- "recurring: 4 fixes refuted"."""
    if not repo:
        return []
    fmt = "%H%x09%ct%x09%(trailers:key=Red-Unrelated,valueonly,separator=%x2C)%x09%(trailers:key=Fixes-Red,valueonly,separator=%x2C)%x09%s"
    r = git(repo, "log", f"refs/remotes/{remote()}/main", "--since=3.days", f"--format={fmt}", check=False)
    out = []
    for line in (r.stdout if r.returncode == 0 else "").splitlines():
        sha, ct, unrelated, vals, subj = (line.split("\t", 4) + ["", "", "", "", ""])[:5]
        if trailer_values(vals) and not unrelated.strip():
            counted = fix_check(repo, sha, trailer_values(vals))[0]  # a trailer touching nothing the red reads: no fix
            if counted:
                out.append((sha, float(ct or 0), counted, subj))
    return out


def red_unrelated(repo, e):
    """A change landing on a red gate under the owner's speed-up / CI-fix rule names the red only to pass the gate:
    its `Red-Unrelated: <reason>` trailer says it is no fix for that red, so it never takes the red's fixer away."""
    for c in e.get("commits") or []:
        r = git(repo, "log", "-1", "--format=%(trailers:key=Red-Unrelated,valueonly)", c, check=False)
        if r.returncode == 0 and r.stdout.strip():
            return True
    return False


# A FIXES-RED TRAILER MUST TOUCH WHAT THE RED READS (G9, convergence-failures-2026-10-29 §4: d21ac5e81 and 1548eb448,
# unit-test speed-ups, carried `Fixes-Red: 2390;2375;2392` while touching nothing those reds' jobs read). At land time --
# the land / submit path, the queue and the batch, and the tick reading landed fixes -- a commit's `Fixes-Red: <run>`
# that names a known red is checked against the paths the red's failing job reads, derived CONSERVATIVELY from its
# workflow (the job's steps, the reusable workflows and local actions it uses, the repository scripts they name, one
# level down) plus the failing test's own sources. A commit counts as that red's fix unless it PLAINLY touches none
# of them: every path it changes is another workflow's file, documentation, the mobile apps (android/, ios/) the job
# never names, or another test spec while the job runs only named specs (`testOnly X` / `runMain X`, no whole test
# task). Unsure -- the red unknown, not a test failure, its workflow or job not found, `local` -- it counts. A trailer
# that does not count leaves the red where it is (no fix-pending, its fixer kept) and the change lands as any other
# change (it waits while the gate is red); its author is told once (`FIX NOT COUNTED`, the inbox). Cached per sha.
FIX_UNREAD_DOC = re.compile(r"(^|/)docs?/|(^|/)(README|CHANGELOG|LICENSE)[^/]*$|\.md$")
FIX_UNREAD_TOP = ("android", "ios")
FIX_WORKFLOW_FILE = re.compile(r"^\.github/workflows/[^/]+\.ya?ml$")
FIX_SPEC_FILE = re.compile(r"/src/(test|it|e2e)/\w+/(.*/)?([A-Z]\w*(Spec|Test|Tests|Suite|IT))\.(scala|java|kt)$")
FIX_BROAD_TEST = re.compile(r"(?<![\w-])(test|testQuick|testUnit\w*|Test/test|it:test|[\w-]+/test|"
                            r"testOnly\s+[^\n\"']*\*)(?![\w-])")
FIX_SBT = re.compile(r"\bsbt\b|sbt-warm|sbtw?\b")
FIX_REPO_PATH = re.compile(r"(?<![\w/.-])(?:\./)?((?:scripts|\.github|tools|bin)/[\w./-]+)")


def fix_tree_files(repo, sha):
    r = git(repo, "ls-tree", "-r", "--name-only", sha, check=False)
    return (r.stdout if r.returncode == 0 else "").splitlines()


def fix_show(repo, sha, path):
    r = git(repo, "show", f"{sha}:{path}", check=False)
    return r.stdout if r.returncode == 0 else None


def fix_workflow_jobs(text):
    """{job key: (its block's text, its `name:`)} of a workflow file, by indentation (no YAML parser here)."""
    lines = text.splitlines()
    try:
        start = next(i for i, l in enumerate(lines) if re.match(r"^jobs:\s*$", l))
    except StopIteration:
        return {}
    jobs, ind, cur = {}, None, None
    for l in lines[start + 1:]:
        if l.strip() and not l.startswith((" ", "\t")):
            break
        m = re.match(r"^(\s+)([\w-]+):\s*$", l)
        if m and (ind is None or len(m.group(1)) == ind):
            ind, cur = len(m.group(1)), m.group(2)
            jobs[cur] = []
            continue
        if cur:
            jobs[cur].append(l)
    out = {}
    for k, ls in jobs.items():
        body = "\n".join(ls)
        nm = re.search(r"^\s+name:\s*['\"]?([^'\"\n]+)", body, re.M)
        out[k] = (body, (nm.group(1).strip() if nm else k))
    return out


def red_job_reads(repo, sha, d):
    """What red d's failing job reads, from the tree of `sha`: {files, prefixes, text, broad, sbt} -- or None when it
    cannot be told (no workflow file of that name, nothing readable)."""
    files = fix_tree_files(repo, sha)
    wfs = [f for f in files if FIX_WORKFLOW_FILE.match(f)]
    want = str(d.get("workflow") or "")
    wf = None
    for f in wfs:
        txt = fix_show(repo, sha, f) or ""
        m = re.search(r"^name:\s*['\"]?([^'\"\n]+)", txt, re.M)
        if (m and m.group(1).strip() == want) or os.path.basename(f) == want:
            wf = (f, txt)
            break
    if not wf:
        return None
    reads, texts = {wf[0]}, []
    jobs = fix_workflow_jobs(wf[1])
    names = [str(j) for j in d.get("jobs") or [d.get("job")] if j]
    heads = {re.split(r" / | \(", n, maxsplit=1)[0].strip().lower() for n in names}
    picked = [body for k, (body, nm) in jobs.items()
              if k.lower() in heads or re.sub(r"\$\{\{.*?\}\}", "", nm).strip().lower() in heads] if heads else []
    texts.append("\n".join(picked) if picked else wf[1])  # its job's steps, or (unsure which) the whole workflow
    seen = set()
    todo = list(texts)
    depth = 0
    while todo and depth < 3:
        nxt = []
        for t in todo:
            for m in re.finditer(r"uses:\s*\./([\w./-]+)", t):
                p = m.group(1).rstrip("/")
                reads.add(p)
                if p in seen:
                    continue
                seen.add(p)
                body = fix_show(repo, sha, p) if FIX_WORKFLOW_FILE.match(p) else \
                    (fix_show(repo, sha, p + "/action.yml") or fix_show(repo, sha, p + "/action.yaml"))
                if body:
                    nxt.append(body)
            for m in FIX_REPO_PATH.finditer(t):
                p = m.group(1).rstrip("/.")
                reads.add(p)
                if p in seen or p not in files:
                    continue
                seen.add(p)
                body = fix_show(repo, sha, p)
                if body and len(body) < 400_000:
                    nxt.append(body)
        texts += nxt
        todo, depth = nxt, depth + 1
    text = "\n".join(texts)
    sbt = bool(FIX_SBT.search(text))
    return {"files": reads, "text": text, "sbt": sbt, "broad": sbt and bool(FIX_BROAD_TEST.search(text)),
            "named": set(re.findall(r"\b[A-Z]\w+\b", text + " " + str(d.get("line") or "") + " " + " ".join(names)))}


def fix_path_unread(p, rd):
    """True when the job plainly does not read path p."""
    if p in rd["files"] or any(p.startswith(f.rstrip("/") + "/") for f in rd["files"]):
        return False
    if FIX_WORKFLOW_FILE.match(p):
        return True
    if FIX_UNREAD_DOC.search(p) and p not in rd["text"]:
        return True
    top = p.split("/", 1)[0]
    if top in FIX_UNREAD_TOP and not re.search(rf"(?<![\w-]){re.escape(top)}/", rd["text"]):
        return True
    m = FIX_SPEC_FILE.search(p)
    if m and not rd["broad"] and m.group(3) not in rd["named"]:
        return True
    return False


def fix_check(repo, sha, values, store=None):
    """(the values that count as fixes, {value: why not} for those that do not): each `Fixes-Red:` value of commit sha
    against the reds it names (see above). Cached per sha; a value naming no known red counts (unsure) and is not
    cached, so a red opened later is still checked."""
    values = {str(v).strip().lstrip("#") for v in values if str(v).strip()}
    if not values or not repo:
        return values, {}
    path = reds_dir("fixcheck", f"{sha}.json")
    cache = read_record(path) or {}
    ok, bad, todo = set(), {}, []
    for v in sorted(values):
        c = cache.get(v)
        if c is None:
            todo.append(v)
        elif c.get("ok"):
            ok.add(v)
        else:
            bad[v] = c.get("why") or "touches nothing its job reads"
    if not todo:
        return ok, bad
    try:
        st = store or Store()
        items = [it for it in st.items("red") if it["data"].get("key")]
    except (Exception, SystemExit):  # SystemExit: NoToken (the laptop's land with no token: the server checks again)
        items = []
    changed = None
    fresh = {}
    for v in todo:
        its = [it for it in items if v in {str(x) for x in red_names(it["data"]) if x is not None}]
        if not its or v == "local" or any(it["data"].get("kind") != "test" for it in its):
            ok.add(v)  # unsure: the red unknown (not cached), local, or not a test failure
            if its:
                fresh[v] = {"ok": True, "why": "not a test failure" if v != "local" else "local"}
            continue
        if changed is None:
            r = git(repo, "diff-tree", "--no-commit-id", "--name-only", "-r", "--root", "-m", sha, check=False)
            changed = [p for p in (r.stdout if r.returncode == 0 else "").splitlines() if p]
        verdict = None
        for it in its:
            try:
                rd = red_job_reads(repo, sha, it["data"])
            except (Exception, SystemExit):
                rd = None
            if rd is None or not changed or not all(fix_path_unread(p, rd) for p in changed):
                verdict = {"ok": True}
                break
            verdict = {"ok": False, "why": (f"it touches nothing {red_what(it['data'])} reads "
                                            f"({', '.join(changed[:4])}{' ...' if len(changed) > 4 else ''})")}
        fresh[v] = verdict
        if verdict["ok"]:
            ok.add(v)
        else:
            bad[v] = verdict["why"]
    if fresh:
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            write_json(path, {**cache, **fresh})
        except OSError:
            pass
        for v, c in fresh.items():
            if not c.get("ok"):
                fix_not_counted_tell(repo, sha, v, c["why"])
    return ok, bad


def fix_not_counted_tell(repo, sha, v, why):
    """The author is told, once per (sha, value): the trailer does not count, and what that means for the change."""
    line = (f"FIX NOT COUNTED: {sha[:10]}'s `Fixes-Red: {v}`: {why}. The red stays where it is (its fixer keeps it); "
            f"the change lands as an ordinary one (it waits while the gate is red). Name the red only in a commit that "
            f"changes what its failing job runs or reads")
    err(line)
    try:
        cwd = repo if repo and os.path.isdir(repo) else None
        inbox_write("land", line, [line], [], 1, cwd=cwd)
    except Exception:
        pass


def commit_fixes(repo, sha, store=None):
    """The `Fixes-Red:` values of commit sha that count as fixes (fix_check)."""
    v = git(repo, "log", "-1", "--format=%(trailers:key=Fixes-Red,valueonly,separator=%x2C)", sha, check=False)
    vals = {f.strip().lstrip("#") for f in (v.stdout if v.returncode == 0 else "").split(",") if f.strip()}
    return fix_check(repo, sha, vals, store)[0] if vals else set()


_trailer_fixes = process_cache({})


def entry_fixes(repo, e):
    """The runs a queued change says it fixes: its `fixes` (recorded at submission from its Fixes-Red trailers), else
    read again from its commits' trailers (cached per commit) -- an entry that lost the field still links its red."""
    if e.get("fixes"):
        return [str(x) for x in e["fixes"]]
    out = []
    for c in e.get("commits") or []:
        if c not in _trailer_fixes:
            r = git(repo, "log", "-1", "--format=%(trailers:key=Fixes-Red,valueonly)", c, check=False)
            vals = {x.strip().lstrip("#") for x in re.split(r"[\s,]+", r.stdout) if x.strip()} if r.returncode == 0 else set()
            _trailer_fixes[c] = sorted(fix_check(repo, c, vals)[0]) if vals else []  # only those that count
        out += _trailer_fixes[c]
    return out


def red_fix_facts(repo, store=None):
    """What links a red to its fix: the queue's Fixes-Red changes, the Fixes-Red commits on main, and every run any
    spell of each red key was seen in (key_runs: a red whose own item lost a run -- a duplicate spell, an import --
    still meets the change that names that run)."""
    entries = []
    try:
        d, _c, _w = waiting_state(repo)
        for e in waiting_entries(d, open_only=False):
            try:
                recent = e.get("status") in FIX_OPEN or (e.get("status") == "landed" and time.time() - parse_time(
                    e.get("landed_at") or e.get("updated") or e.get("created") or ts()).timestamp() < 7 * 86400)
            except (ValueError, TypeError):
                recent = False
            fx = entry_fixes(repo, e) if recent or e.get("fixes") else None
            if fx and not red_unrelated(repo, e):
                entries.append({**e, "fixes": fx})
    except (Exception, SystemExit):
        pass
    try:
        landed = landed_fix_commits(repo)
    except (Exception, SystemExit):
        landed = []
    key_runs = {}
    try:
        for it in (store.items("red") if store is not None else []):
            k = it["data"].get("key") or it["id"]
            key_runs.setdefault(k, set()).update(map(str, it["data"].get("runs") or []))
    except Exception:
        pass
    # THE FIXES' CONTENT (simplification-2026-10-31 item 3): the patch-id of every fix commit a red could take and of
    # every fix it refuted or set aside, so a rebased copy of a refuted fix -- another sha, the same change -- counts no
    # more than the original (red_fix_of). A sha whose patch cannot be read is matched by sha alone.
    shas = {e.get("landed_sha") for e in entries if e.get("landed_sha")} | {row[0] for row in landed}
    try:
        for it in (store.items("red") if store is not None else []):
            shas |= set(it["data"].get("refuted") or []) | set(it["data"].get("unverified") or [])
    except Exception:
        pass
    patches = {}
    for sha in shas:
        try:
            pid = patch_id(repo, sha)
        except (Exception, SystemExit):
            pid = None
        if pid:
            patches[sha] = pid
    return {"entries": entries, "landed": landed, "key_runs": {k: sorted(v) for k, v in key_runs.items()},
            "patches": patches}


def red_names(d):
    return set(d.get("runs") or []) | {d.get("key")} | ({"local"} if d.get("workflow") == LOCAL_FLOW else set())


def red_set_aside(d, facts):
    """(shas, patch-ids) of the fixes this red refuted or set aside unverified: a candidate fix is one of them when its
    sha (or a prefix) is, or its CONTENT is -- the same patch-id under another sha (a rebase, a cherry-pick)."""
    shas = set(d.get("refuted") or []) | set(d.get("unverified") or [])
    patches = facts.get("patches") or {}
    return shas, {patches[x] for x in shas if patches.get(x)}


def red_fix_is_set_aside(sha, aside, facts):
    shas, pids = aside
    return sha in shas or any(sha.startswith(x) for x in shas) or (facts.get("patches") or {}).get(sha) in pids


def red_fix_of(it, facts):
    """The fix on its way for this red, or None: {entry, status, sha?, at?}. A refuted or unverified fix -- by sha or
    by content -- counts no more."""
    d = it["data"]
    names = red_names(d) | set((facts.get("key_runs") or {}).get(d.get("key")) or [])
    aside = red_set_aside(d, facts)
    since = float(d.get("opened_at") or 0) - 3600
    best = None
    for e in sorted(facts.get("entries") or [], key=lambda e: e.get("created") or ""):
        if not set(map(str, e.get("fixes") or [])) & names:
            continue
        if e.get("status") in FIX_OPEN:
            best = {"entry": e["id"], "status": e["status"], "author": {"worktree": e.get("worktree"),
                                                                        "session": e.get("session")}}
        elif e.get("status") == "landed" and e.get("landed_sha") and not red_fix_is_set_aside(e["landed_sha"], aside, facts):
            best = {"entry": e["id"], "status": "landed", "sha": e["landed_sha"],
                    "at": parse_time(e.get("landed_at") or e.get("updated") or e.get("created") or ts()).timestamp()}
    if best:
        return best
    for sha, ct, vals, _s in facts.get("landed") or []:
        if vals & names and ct >= since and not red_fix_is_set_aside(sha, aside, facts):
            return {"entry": None, "status": "landed", "sha": sha, "at": ct}
    return None


def red_entry_status(facts, eid):
    return next((e.get("status") for e in facts.get("entries") or [] if e.get("id") == eid), None)


# -- moving a red
def red_event(it, text, **kw):
    d = it["data"]
    ev = {"text": text, "refs": {"key": d.get("key"), "run": (d.get("last_run") or {}).get("number"),
                                 "workflow": d.get("workflow")}}
    ev.update(kw)
    return ev


def red_move(store, it, to, text, data=None, deadline_s=None, **ev):
    v = store.transition("red", it["id"], to, it["version"], data=data, event=red_event(it, text, **ev),
                         deadline_s=deadline_s)
    return store.item("red", it["id"]) if v else None


def red_what(d):
    jobs = [str(j) for j in d.get("jobs") or [d.get("job")] if j]
    o_r = red_repo_of(d)
    return ("" if o_r == RED_PRIMARY else f"{red_repo_short(o_r)} ") + f"{d.get('workflow')} #{(d.get('last_run') or {}).get('number')}" + (f" [{', '.join(jobs)}]" if jobs else "")


def red_offer_class(d):
    return "flake" if d.get("red_kind") == "flake" else "gate" if d.get("gate") else "other"


def red_offer(store, it, text, data=None, repo=None, escalate=False, verdict=None, tail=None, quiet=False):
    """Into `offered`, with its tier from the ladder, its deadline and its offer (the picture a taker gets). quiet: a
    re-offer that is no escalation (its culprit decided: blame.py)."""
    d = {**it["data"], **(data or {})}
    tier = red_tier(d, d.get("culprit"))
    deadline = RED_OFFER["author"] if tier == "author" else RED_OFFER[red_offer_class(d)]
    if tier == "blame":  # waits for its culprit, at most BLAME_MAX: never escalated UNCLAIMED meanwhile
        deadline = max(60, float((d.get("blame") or {}).get("since") or 0) + env_num("AGENTCTL_BLAME_MAX", BLAME_MAX)
                       - store.clock() + 60)
    sev = {"severity": "escalate"} if escalate or tier == "deep" else {"severity": "info"} if quiet else {}
    aud = {"audience": {k: v for k, v in (d.get("author") or {}).items() if k in ("worktree", "session")}} \
        if tier == "author" else {}
    if tier == "author":  # addressed to the change's recorded author (the projector writes it to their inbox)
        a = d["author"]
        verdict = (f"RED AUTHOR: {red_what(d)} contains your {', '.join(s[:9] for s in a.get('shas') or [])} "
                   f"({str(a.get('worktree')).replace(HOME, '~')}): offered to you for {deadline // 60} min -- "
                   f"`~/bin/agentctl red claim {d['key']} --worktree .` and fix it "
                   f"(`Fixes-Red: {(d.get('last_run') or {}).get('number')}`); else a fixer takes it")
    if tier == "deep" and not verdict and not escalate and red_decision(d):  # the owner decided it: no escalation
        sev = {"severity": "info"}
    elif tier == "deep" and not verdict and not escalate and d.get("deep_told") == int(d.get("attempts") or 0):
        sev = {"severity": "info"}  # ESCALATED ONCE PER ATTEMPT: a re-offer with no new attempt (a lease lost to infra,
        # a red back from a block) at the deep tier is no new escalation -- 2026-11-02, #728: 58 RED ESCALATE in a day
    elif tier == "deep":
        verdict = verdict or f"RED ESCALATE: {red_what(d)} still red after {d.get('attempts')} attempts: a deep investigation is offered"
        data = {**(data or {}), "deep_told": int(d.get("attempts") or 0)}
    if verdict:
        sev["refs"] = {"key": d.get("key"), "run": (d.get("last_run") or {}).get("number"), "workflow": d.get("workflow"),
                       "verdict": verdict, "tail": tail or [d.get("line") or ""], "command": "red"}
    it = red_move(store, it, "offered", text, data={**(data or {}), "tier": tier}, deadline_s=deadline, **sev, **aud)
    red_put_offer(store, it)
    return it


# RED FAMILIES (the owner, 2026-10-29: "give all convergence red work to one agent"): the reds of one family -- by
# workflow, or by a failing job's name (Verify scrape recording's `convergence (<cc>)` is convergence work too) -- go to
# ONE fixer. While it is alive, the dispatcher puts a new red of its family under its lease (a sibling it renews with
# its own) and tells it in RED-INBOX.md in its worktree, never a second fixer; when it ends, the next fixer of the
# family goes on in its worktree (previous_fixer_tree). The families are config: red-families.json beside agentctl
# ($AGENTCTL_RED_FAMILIES names another file).
_families_cache = process_cache({})


def red_families():
    path = os.environ.get("AGENTCTL_RED_FAMILIES") or os.path.join(os.path.dirname(TOOL_PATH), "red-families.json")
    try:
        m = os.path.getmtime(path)
    except OSError:
        return []
    hit = _families_cache.get(path)
    if not hit or hit[0] != m:
        try:
            with open(path) as f:
                fams = json.load(f).get("families") or []
            fams = [dict(x, _jobs=[re.compile(j) for j in x.get("jobs") or []]) for x in fams if x.get("name")]
        except (OSError, ValueError, re.error) as e:
            err(f"red families: {path} unreadable ({e}): none apply")
            fams = []
        hit = _families_cache[path] = (m, fams)
    return hit[1]


def red_family(d):
    """The family of a red (its data, its picture, a fixer record: workflow, job, jobs), or None."""
    if not d:
        return None
    o_r = red_repo_of(d)
    if o_r != RED_PRIMARY:  # ANOTHER WATCHED REPO: its red-repos.json family, every red of it (red-families.json's
        return (red_repo_conf(o_r) or {}).get("family") or None  # families are the primary's)
    jobs = [str(j) for j in (d.get("jobs") or []) + [d.get("job")] if j]
    for fam in red_families():  # the first family that matches: its workflows or jobs (if it names any) AND its kinds
        named = bool(fam.get("workflows") or fam["_jobs"])
        by_name = d.get("workflow") in (fam.get("workflows") or []) or any(rx.search(j) for rx in fam["_jobs"] for j in jobs)
        by_kind = not fam.get("kinds") or d.get("kind") in fam["kinds"]
        if (by_name or not named) and by_kind and (named or fam.get("kinds")):
            return fam["name"]
    return None


def red_family_tell(row, pic, fam):
    """Tell the family's fixer a red was added to its lease: a line in RED-INBOX.md in its worktree."""
    wt = lease_meta(row).get("worktree")
    if not wt or not os.path.isdir(wt) or not allowed_path(wt):
        return False
    jobs = ", ".join(str(j) for j in pic.get("jobs") or [pic.get("job")] if j)
    with open(os.path.join(wt, "RED-INBOX.md"), "a") as f:
        f.write(f"{ts()} RED FAMILY {fam}: {pic.get('workflow')} #{(pic.get('run') or {}).get('number')} [{jobs}] "
                f"(red {pic.get('key')}) is YOURS too, held under your lease: {str(pic.get('line') or '')[:300]} -- "
                f"`agentctl ci-why {(pic.get('run') or {}).get('databaseId')}`; land its fix with `Fixes-Red: "
                f"{(pic.get('run') or {}).get('number')}`.\n")
    return True


def red_picture(d):
    """What a taker gets: the failure, every prior attempt with its verdict, the refuted fixes, the diagnosis."""
    return {"key": d.get("key"), "workflow": d.get("workflow"), "job": d.get("job"), "jobs": d.get("jobs"),
            "kind": d.get("kind"), "line": d.get("line"), "run": d.get("last_run"), "gate": bool(d.get("gate")),
            "red_kind": d.get("red_kind"), "attempts": int(d.get("attempts") or 0), "history": (d.get("history") or [])[-6:],
            "refuted": d.get("refuted") or [], "diagnosis": d.get("diagnosis"), "predicts": d.get("predicts"),
            "author": d.get("author"), "culprit": d.get("culprit"), "names": sorted(red_names(d) - {None}),
            "prior_work": d.get("prior_work"), "old_note": d.get("old_note"), "repo": d.get("repo"),
            "blame": d.get("blame"), "lander_tried": d.get("lander_tried"), "decision": red_decision(d)}


def red_put_offer(store, it):
    d = it["data"]
    pri = RED_PRIORITY["flake"] if d.get("red_kind") == "flake" else RED_PRIORITY["gate"] if d.get("gate") else \
        RED_PRIORITY["outside"]
    return store.offer("fixer", f"red:{it['id']}", red_lease(d["key"]), d.get("tier") or "fresh",
                       int(d.get("priority", pri)), it["deadline"], red_picture(d))


def red_open(store, ob, repo, t, o_r=None):
    """A new red item (state red) from its first observation; its author recorded from the change that landed it. o_r:
    its repo (red_repo_key already in ob's key); the primary's reds carry none."""
    key = ob["key"]
    iid, n = key, 1
    while store.item("red", iid) is not None:  # an earlier spell of the same red is terminal: a new item
        n += 1
        iid = f"{key}.{n}"
    data = {"key": key, "workflow": ob["workflow"], "jobs": ob["jobs"], "job": ob["job"], "kind": ob["kind"],
            "line": ob["line"], "red_kind": ob["red_kind"], "gate": ob["gate"], "predicts": ob.get("predicts"),
            "runs": sorted(ob["names"]), "last_run": ob["run"], "opened_at": t, "attempts": 0, "history": [],
            "classifier": classifier_version()}
    if o_r and red_is_other(o_r):
        data["repo"] = o_r
    data.update(red_identity(data))
    if ob["kind"] == "test" and ob["red_kind"] in ("gate", "outside"):
        try:
            data["author"] = red_author(repo, ob, t)
        except (Exception, SystemExit) as e:
            err(f"red: no author for {key} ({str(e)[:120]})")
        try:
            data["culprit"] = culprit_of(repo, {"runs": ob["runs_list"]})
        except (Exception, SystemExit):
            pass
    try:  # THE CULPRIT (blame.py): searched from the red's opening; a gate test red only
        data["blame"] = blame_open(store, repo, o_r or RED_PRIMARY, ob, t)
    except (Exception, SystemExit) as e:
        data["blame"] = {"state": "none", "why": f"its range could not be read ({str(e)[:120]})"}
    return store.put_item("red", iid, "red", data)


def red_attach_obs(store, it, ob):
    """A newer run failing the same way: attached to the red (its runs, its newest run)."""
    d = it["data"]
    runs = sorted(set(d.get("runs") or []) | set(ob["names"]))
    newer = int(ob["run"].get("databaseId") or 0) > int((d.get("last_run") or {}).get("databaseId") or 0)
    cv = classifier_version()
    if runs == d.get("runs") and not newer and sorted(ob["jobs"]) == sorted(d.get("jobs") or []) \
            and d.get("classifier") == cv:
        return it
    upd = {"runs": runs, "jobs": sorted(set(d.get("jobs") or []) | set(ob["jobs"])),
           "classifier": cv}  # observed under this classifier's key
    if newer:
        upd["last_run"] = ob["run"]
    for attempt in range(5):  # a ticking writer moved it (StaleVersion): re-read and re-apply, never lose the attach --
        try:  # an unattached newer run reads as "no longer this failure" to a block's probe and discards its question
            store.update_item("red", it["id"], it["version"], upd)
            break
        except StaleVersion:
            if attempt == 4:
                raise
            it = store.item("red", it["id"])
    return store.item("red", it["id"])


def red_attach(store, workflow, run, author=None, o_r=None):
    """WATCH RED (the landed watch): the run's failures opened or attached, its author offered first. run: the run (a
    dict), or its number."""
    o_r = o_r or SHORT["movies"]
    if not isinstance(run, dict):
        _name, run = resolve_run(o_r, run)
        if run is None:
            return []
    try:
        sigs = run_sigs(o_r, run, workflow)
    except (Exception, SystemExit):
        sigs = []
    if sigs_no_verdict(sigs):
        return []  # an incomplete run: no verdict, no red to open
    gate = workflow in {g for g, _ in red_gate_flows(o_r)}
    obs = red_obs_for(o_r, red_obs_of_units(red_units(o_r, workflow, run, [run], sigs, gate)))
    active, out = red_active(store), []
    for key, ob in obs.items():
        it = active.get(key) or red_open(store, ob, None, store.clock(), o_r)
        it = red_attach_obs(store, it, ob)
        if author and it["state"] == "red" and not it["data"].get("author"):
            store.update_item("red", it["id"], it["version"], {"author": dict(author, shas=[run.get("headSha")])})
            it = store.item("red", it["id"])
        out.append(it)
    return out


def red_author(repo, ob, t):
    """The live author of the red run's newest commit, from the change that landed it (its recorded session and
    worktree; audit #15: never an inherited environment), live = an agent process in that worktree, active lately."""
    u = {"run": ob["run"], "runs": ob["runs_list"]}
    for a in red_authors(repo, u, t):
        if a["live"] and a.get("session"):  # a change item's recorded author, never a worktree that merely holds the commit
            return {"worktree": a["worktree"], "session": a.get("session"), "shas": a["shas"][:5]}
    return None


# -- the red step: one item, one small idempotent step per tick
def red_stop_holder(store, key, why, outcome, force=False):
    """Revoke the red's lease (its holder's next renewal answers lost) and tell its worktree. A REVIVED LANDER (lease
    tier `lander`, blame.py) is spared unless force: the revert is the red's fix, and the lander's work -- its change
    re-applied with its fix -- goes on after it; only a refuted revert (the culprit was wrong) stops it."""
    row = store.get(red_lease(key))
    if not lease_is_live(row, store.clock()):
        return None
    if lease_meta(row).get("tier") == "lander" and not force:
        return None
    store.revoke(red_lease(key), outcome)
    if lease_meta(row).get("kind") == "owned":  # a family owner's: its next beat drops the lease; never a STOP in its
        return row["holder"]                    # one permanent worktree, which would stop every later turn too
    if any(lease_is_live(r, store.clock()) and r["holder"] == row["holder"] for r in store.list("red/")):
        return None  # it still holds other reds of its run: only this lease goes, never its work
    wt = lease_meta(row).get("worktree")
    if wt and os.path.isdir(wt) and allowed_path(wt):
        try:
            with open(os.path.join(wt, "RED-STOP.md"), "w") as f:
                f.write(f"STOP: {why} (agentctl red, {ts()}); remove this file with your worktree.\n")
        except OSError:
            pass
    return row["holder"]


def tail_lines(path, n):
    """The last n non-empty lines of a text file (its last 8 KiB), [] when unreadable."""
    try:
        with open(path, "rb") as f:
            f.seek(max(0, os.path.getsize(path) - 8192))
            return [l for l in f.read().decode(errors="replace").splitlines() if l.strip()][-n:]
    except (OSError, TypeError):
        return []


def red_history(row, t):
    m = lease_meta(row)
    wt, log = m.get("worktree"), m.get("log")
    if m.get("kind") == "owned" and m.get("family"):  # a family owner's: its worktree and its wrapper's log (stderr)
        rec = read_record(owner_record_path(m["family"])) or {}
        wt, log = wt or rec.get("worktree"), log or rec.get("log")
    notes = m.get("notes") or m.get("note") or ""
    if not notes and log and not str(log).endswith(".jsonl"):  # a lapse says nothing: its wrapper's last words do
        notes = "\n".join(tail_lines(log, 4))
    return {"at": iso_at(t), "holder": row.get("holder") if row else None, "outcome": m.get("outcome") or "lease expired",
            "notes": notes[:600], "session": m.get("session"),
            "worktree": wt, "log": log, "dispatched": m.get("kind") in ("fixer", "owned")}


def red_close(store, it, to, text):
    store.close_offer(f"red:{it['id']}", to)
    holder = red_stop_holder(store, it["data"]["key"], text, f"red {to}")
    line = f"RED {to.upper()}: {red_what(it['data'])}: {text}" + (f"; told to stop: {holder}" if holder else "")
    inbox_write("red", line, [line], [], 0)
    return red_move(store, it, to, text)


def red_callable_evidence(d, snap, repo, o_r):
    """For an outside red of a workflow that on main runs only when called (snap's callable_only): the line proving it
    fixed, else None. Proof is BOTH: its file changed on main since the failing run (the first such commit C), and a
    caller's newest decided run on main that contains C passed -- the forge expands the called file into the caller's
    run, so a green one says the forge parses and runs the new file. A red caller vetoes nothing: it fails for reasons of
    its own (Report run timings has 36 callers, some red at any hour; a "none failed" rule never let #38 clear).
    "Report run timings" #38 (2026-10-19, a `workflow_call.secrets` block Forgejo 15 rejected; 32d1805de fixed it that
    night) stayed red for 10 days: no run of its own could come to clear it. Never closed on the trigger alone:
    5cf3f69's 811 re-opened retirements."""
    if not repo or d.get("red_kind") != "outside" or d.get("workflow") not in ((snap or {}).get("callable_only") or ()):
        return None
    run = d.get("last_run") or {}
    main = f"refs/remotes/{remote()}/main"
    meta = main_flow_meta(o_r)
    path = meta["paths"].get(d.get("workflow"))
    if not path or not run.get("headSha"):
        return None
    log = git(repo, "log", "--format=%H", f"{run['headSha']}..{main}", "--", path, check=False)
    changes = log.stdout.split() if log.returncode == 0 else []
    if not changes:
        return None
    first = changes[-1]
    latest = (snap or {}).get("latest") or {}
    passed = sorted(f"{name} #{last.get('number')}" for name, last in
                    ((n, latest.get(n) or {}) for n in meta["callers"].get(os.path.basename(path), []))
                    if last.get("conclusion") == "success" and contains_local(repo, first, last.get("headSha")))
    if not passed:
        return None
    return (f"{d.get('workflow')} runs only when called: its file changed on main since #{run.get('number')} "
            f"({first[:10]}), and its callers' runs since passed: {', '.join(passed[:3])}"
            + (f" and {len(passed) - 3} more" if len(passed) > 3 else ""))


def red_settled_runs(store):
    """{(key, run id): its verified spell} for the runs red_callable_evidence settled. Such a run stays its workflow's
    newest on main for good (the workflow runs only when called): observed again, it opens no new spell, and it reads
    as verified, never "offering" (Report run timings #38 read "offering" in the status line after its FIX OK)."""
    return {(it["data"].get("key"), str(it["data"]["settled_run"])): it for it in store.items("red")
            if it["state"] == "verified" and it["data"].get("settled_run") is not None}


# -- RETIRED CHECKS (agentctl issue 20261031T210634-c53375): core 2b4d9c905 removed every dev/sandbox target, and the
# reds of their last runs (Playwright Tests (deployed) #1080/#1081, Run Post Deployment Tests #1362) could never clear:
# no run of a retired check comes again. A red whose check no longer exists on main is CLEARED by the same probe that
# would re-raise it -- main's workflow files, read through the API at main's head: its workflow gone (by name and by
# the run's file), its job gone (no current job produces its name, and no job of the same key holds its matrix row), or
# the input branch its job took gone (a step it ran under an `if:` on inputs/matrix, absent from the job now); or
# STALE: its workflow made no run on main for RED_STALE_DAYS while main moved (workflow_call-only workflows excepted:
# red_callable_evidence settles those). Conservative: a job renamed in place (same key, no matrix row) still matches,
# and anything unreadable or unparseable keeps the red (fail safe).
RED_STALE_DAYS = 14          # $AGENTCTL_RED_STALE_DAYS
RETIRE_MAIN_TTL = 60         # s: main's head is read again after this
RETIRE_FAIL_TTL = 60         # s: an unreadable repo is not asked again before this
_retire_cache = process_cache({})


def _retire_get(key, ttl, f):
    hit = _retire_cache.get(key)
    now = time.time()
    if hit and (ttl is None or now - hit[0] < ttl):
        return hit[1]
    v = f()
    if len(_retire_cache) > 2000:
        _retire_cache.clear()
    _retire_cache[key] = (now, v)
    return v


def retire_main(o_r):
    """(sha, committer date) of main's head."""
    def read():
        c = gh_json(o_r, "api", f"repos/{o_r}/commits/main")
        return c["sha"], ((c.get("commit") or {}).get("committer") or {}).get("date")
    return _retire_get(("main", o_r), RETIRE_MAIN_TTL, read)


def retire_file(o_r, sha, path):
    """The text of path at sha, or None when it does not exist there."""
    def read():
        import base64
        try:
            c = gh_json(o_r, "api", f"repos/{o_r}/contents/{path}?ref={sha}")
        except RuntimeError as e:
            if "404" in str(e) or "Not Found" in str(e):
                return None
            raise
        return base64.b64decode(c.get("content") or "").decode("utf-8", "replace")
    return _retire_get(("file", o_r, sha, path), None, read)


def retire_flows(o_r, sha):
    """{workflow name: (path, text)} of the workflow files at sha."""
    def read():
        try:
            ls = gh_json(o_r, "api", f"repos/{o_r}/contents/.github/workflows?ref={sha}")
        except RuntimeError as e:
            if "404" in str(e) or "Not Found" in str(e):
                return {}
            raise
        out = {}
        for f in ls if isinstance(ls, list) else []:
            p = f.get("path") or ""
            if not p.endswith((".yml", ".yaml")):
                continue
            txt = retire_file(o_r, sha, p) or ""
            m = re.search(r"^name:\s*[\"']?(.+?)[\"']?\s*$", txt, re.M)
            out[m.group(1) if m else os.path.basename(p)] = (p, txt)
        return out
    return _retire_get(("flows", o_r, sha), None, read)


def retire_run(o_r, rid):
    return _retire_get(("run", o_r, str(rid)), None, lambda: gh_json(o_r, "api", f"repos/{o_r}/actions/runs/{rid}"))


def retire_jobs(o_r, rid):
    return _retire_get(("jobs", o_r, str(rid)), None, lambda: gh_json(
        o_r, "api", f"repos/{o_r}/actions/runs/{rid}/jobs?per_page=100").get("jobs") or [])


def retire_newest(o_r, path):
    """The newest run on main of the workflow file (its API record), or None."""
    def read():
        r = gh_json(o_r, "api", f"repos/{o_r}/actions/workflows/{os.path.basename(path)}/runs?branch=main&per_page=1")
        return ((r or {}).get("workflow_runs") or [None])[0]
    return _retire_get(("newest", o_r, path), 600, read)


def _yaml_scalar(v):
    v = re.sub(r"\s+#.*$", "", v.strip())
    return v[1:-1] if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"" else v


def retire_jobs_of(text):
    """{job key: {name, matrix (None | "dynamic" | set of its values), steps [(name, if)]}} of a workflow file, by
    indentation (no YAML parser here); None when it cannot be read (no `jobs:`, no job)."""
    jobs = fix_workflow_jobs(text or "")
    if not jobs:
        return None
    out = {}
    for key, (body, _nm) in jobs.items():
        lines = [l for l in body.splitlines() if l.strip() and not l.lstrip().startswith("#")]
        if not lines:
            out[key] = {"name": None, "matrix": None, "steps": []}
            continue
        ind = len(lines[0]) - len(lines[0].lstrip())
        name, matrix, steps, sect = None, None, [], None
        cur = None
        step_ind = None
        for l in lines:
            li = len(l) - len(l.lstrip())
            s = l.strip()
            if li == ind:
                m = re.match(r"^([\w-]+):\s*(.*)$", s)
                sect = m.group(1) if m else None
                if sect == "name":
                    name = _yaml_scalar(m.group(2))
                continue
            if sect == "strategy":
                m = re.match(r"^matrix:\s*(.*)$", s)
                if m:
                    matrix = "dynamic" if "${{" in m.group(1) else set()
                    continue
                if isinstance(matrix, set):
                    if "${{" in s:
                        matrix = "dynamic"
                        continue
                    m = re.match(r"^(?:-\s+)?[\w.-]+:\s*(.+)$", s) or re.match(r"^-\s+(.+)$", s)
                    if m:
                        v = m.group(1).strip()
                        if v.startswith("[") and v.endswith("]"):
                            matrix |= {_yaml_scalar(x) for x in v[1:-1].split(",") if x.strip()}
                        elif v not in ("|", ">"):
                            matrix.add(_yaml_scalar(v))
            elif sect == "steps":
                m = re.match(r"^-\s*(.*)$", s)
                if m and (step_ind is None or li == step_ind):
                    step_ind = li
                    cur = {"name": None, "if": None}
                    steps.append(cur)
                    s = m.group(1)
                    li = li + 2
                if cur is not None and li == step_ind + 2:
                    m = re.match(r"^(name|if):\s*(.+)$", s)
                    if m:
                        cur[m.group(1)] = _yaml_scalar(m.group(2))
        out[key] = {"name": name, "matrix": matrix, "steps": [(st["name"], st["if"]) for st in steps]}
    return out


def _tmpl_re(tmpl):
    """A name template as a regex: each ${{ ... }} matches anything."""
    parts = re.split(r"\$\{\{.*?\}\}", tmpl)
    return re.compile("^" + ".+?".join(re.escape(p) for p in parts) + "$")


def _name_match(tmpl, name):
    if name.endswith("..."):  # GitHub cuts a long job name: its head must fit the template
        head = name[:-3]
        parts = re.split(r"\$\{\{.*?\}\}", tmpl)
        return bool(re.match("^" + ".*?".join(re.escape(p) for p in parts), head)) or \
            bool(re.match("^" + re.escape(head), re.sub(r"\$\{\{.*?\}\}", "", tmpl)))
    return bool(_tmpl_re(tmpl).match(name))


def _row_of(name):
    """(head, [row values]) of a job name `head (v1, v2, ...)`; values None without a row. A cut last value is left out."""
    m = re.match(r"^(.*?) \((.*?)\)?$", name)
    if not m or "(" not in name:
        return name, None
    vals = [v.strip() for v in m.group(2).split(",")]
    if name.endswith("..."):
        vals = vals[:-1]
    return m.group(1), [v for v in vals if v]


def _job_has(j, key, name):
    """True when current job j (its key) can produce the job name."""
    tmpl = j["name"] or key
    if _name_match(tmpl, name):
        return True
    if j["matrix"] is not None and "matrix." not in tmpl and name.startswith(tmpl + " ("):
        if j["matrix"] == "dynamic":
            return True
        _h, vals = _row_of(name)
        return bool(vals and set(vals) & j["matrix"])
    return False


def _step_branch_gone(key, cur, old, steps):
    """A step the run's job ran under an `if:` on its inputs or matrix (the row's branch), gone from the job now."""
    if not old or key not in old or key not in cur:
        return None
    gated = {n: c for n, c in old[key]["steps"] if n and c and re.search(r"\b(inputs|matrix|github\.event\.inputs)\.", c)}
    now = [n for n, _c in cur[key]["steps"] if n]
    for s in steps or []:
        n = s.get("name")
        if s.get("conclusion") in ("success", "failure") and n in gated and not any(_name_match(t, n) for t in now):
            return f"job {key} ran step `{n}` (if: {gated[n]}), a branch gone from main"
    return None


def red_job_retired(name, cur, old, steps=None):
    """Why the job `name` (a run's) is gone from the current workflow `cur`, else None. old: the workflow at the run's
    commit (None when unread). A caller's `x / y` job is judged by its own job x."""
    name = name.split(" / ", 1)[0].strip()
    for key, j in cur.items():
        if _job_has(j, key, name):
            return _step_branch_gone(key, cur, old, steps)
    head, vals = _row_of(name)
    k = next((ok for ok, oj in (old or {}).items() if _job_has(oj, ok, name)), None)
    k = k if k in cur else next((ck for ck, cj in cur.items() if head in (ck, re.sub(r" \(.*$", "", cj["name"] or ""))), None)
    if k is None:
        return f"job `{name}` is gone from the workflow"
    old_row = (old[k]["matrix"] is not None) if old and k in old else vals is not None
    if not old_row or not vals:
        return None  # renamed in place
    cj = cur[k]
    if cj["matrix"] == "dynamic":
        return None
    if isinstance(cj["matrix"], set):
        now = cj["matrix"]
    else:
        tmpl = cj["name"] or k
        if "${{" in tmpl:
            return None
        now = set(_row_of(tmpl)[1] or [])
    if set(vals) & now:
        return None
    return f"its matrix row ({', '.join(vals)}) of job {k} is gone"


def _retire_fail(o_r):
    _retire_cache[("fail", o_r)] = (time.time(), True)


def red_run_retired(o_r, workflow, run, jobs=None, now=None):
    """Why the red run of `workflow` can never clear -- "check retired at <sha>: ..." or "stale: no run since <date>" --
    else None. jobs: its failing jobs' names (default: the run's failed jobs). Anything unread or unparseable: None."""
    rid = (run or {}).get("databaseId") or (run or {}).get("id")
    if not rid or not workflow:
        return None
    if _retire_cache.get(("fail", o_r)) and time.time() - _retire_cache[("fail", o_r)][0] < RETIRE_FAIL_TTL:
        return None
    try:
        sha, main_date = retire_main(o_r)
        flows = retire_flows(o_r, sha)
        r = retire_run(o_r, rid)
        path = str(r.get("path") or "").split("@", 1)[0]
        if not path.startswith(".github/workflows/"):
            return None  # a forge-made workflow (Dependabot's `dynamic/...`): no file of ours retires it
        cur = flows.get(workflow) or next(((p, t) for p, t in flows.values() if path and p == path), None)
        if not cur:
            return f"check retired at {sha[:10]}: workflow {workflow} is gone from main"
        cpath, ctext = cur
        t = now if now is not None else time.time()
        if workflow_triggers(ctext) != {"workflow_call"}:
            newest = retire_newest(o_r, cpath) or r
            at = newest.get("created_at") or r.get("created_at")
            days = env_num("AGENTCTL_RED_STALE_DAYS", RED_STALE_DAYS)
            if at and main_date and t - parse_time(at).timestamp() > days * 86400 \
                    and parse_time(main_date) > parse_time(at) and newest.get("head_sha") != sha:
                return f"stale: no run of {workflow} on main since {at[:10]} (main moved to {sha[:10]})"
        cjobs = retire_jobs_of(ctext)
        if not cjobs:
            return None  # unparseable: the red stays
        head = r.get("head_sha") or (run or {}).get("headSha")
        otext = retire_file(o_r, head, path or cpath) if head else None
        ojobs = retire_jobs_of(otext) if otext else None
        api_jobs = retire_jobs(o_r, rid)
        names = [j for j in (jobs or []) if j and j != "?"] or \
            [j.get("name") for j in api_jobs if j.get("conclusion") in ("failure", "timed_out")]
        if not names:
            return None
        whys = []
        for n in names:
            steps = next((j.get("steps") for j in api_jobs if j.get("name") == n), None)
            w = red_job_retired(n, cjobs, ojobs, steps)
            if not w:
                return None
            if w not in whys:
                whys.append(w)
        return f"check retired at {sha[:10]}: " + "; ".join(whys)
    except (Exception, SystemExit) as e:
        _retire_fail(o_r)  # silent (the status line and ci-why call this): the red stays, the reason kept for a look
        _retire_cache[("why", o_r)] = (time.time(), f"{workflow} #{(run or {}).get('number')}: {str(e)[:200]}")
        return None


def red_retired(o_r, d, now=None):
    """red_run_retired for a red item's data (gate and outside reds only: local, stuck, predicted and flake reds have
    probes of their own)."""
    if d.get("red_kind") not in ("gate", "outside"):
        return None
    return red_run_retired(o_r, d.get("workflow"), d.get("last_run") or {}, d.get("jobs") or [d.get("job")], now)


def red_step(store, it, obs, snap, facts, repo, o_r, t):
    it = store.item("red", it["id"])
    d, st, key = it["data"], it["state"], it["data"]["key"]
    row = store.get(red_lease(key))
    live = lease_is_live(row, t)
    gone = obs is not None and red_gone(it, obs, snap)
    fix = red_fix_fresh(store, red_fix_of(it, facts))
    block = red_block_of(it, store)
    acts = []
    if st not in RED_TERMINAL:
        why = red_retired(o_r, d, t)
        if why:  # its check is gone from main: no run can ever clear it, and no fixer is offered it
            red_close(store, it, "cleared", why)
            return [f"cleared {red_what(d)}: {why}"]
    # fix-pending too: the usual end of such a red is its fixer's own `Fixes-Red:` landing, and no dispatch can verify a
    # workflow_call-only workflow -- it waited RED_FIX_GIVEUP (6 h) there unread, then went back to a fixer
    proof = red_callable_evidence(d, snap, repo, o_r) if st in ("red", "offered", "leased", "blocked-external",
                                                                "fix-pending") else None
    if proof:  # recorded as a verified fix is: its own event, the holder told, the run settled
        line = f"FIX OK: {red_what(d)}: {proof}"
        inbox_write("red", line, [line], [], 0)
        store.close_offer(f"red:{it['id']}", "verified")
        red_stop_holder(store, key, "its callers' runs prove it fixed", "red verified")
        red_move(store, it, "verified", line, data={"settled_run": (d.get("last_run") or {}).get("databaseId"),
                                                    "proof": proof})
        return [line]
    if st in ("red", "offered") and not live and not fix and not block:
        old = red_old_run(d, t)
        if old:  # AN OLD RED SEEN ONLY NOW (audit #7 surfaces a rarely run workflow): verified by a fresh run, or fixed
            done = red_resolve_old(store, it, old, o_r, repo, t)
            if done is not None:
                return done
            it = store.item("red", it["id"])  # no fresh run can check it: an ordinary red, offered below
            d = it["data"]
    if st == "red":
        it = red_offer(store, it, f"{red_what(d)} is red: offered")
        acts = [f"offered {red_what(d)} ({it['data']['tier']})"]
        if it["data"]["tier"] == "blame":  # a culprit decided by what is known already (Main's green parent): now
            acts += blame_step(store, it, repo, o_r, t) or []
        return acts
    if st == "offered":
        if gone and not fix:
            red_close(store, it, "cleared", red_gone_why(d, obs))
            return [f"cleared {red_what(d)}"] + red_flake_check(store, it, snap, repo, facts)
        if fix:
            store.close_offer(f"red:{it['id']}", "fix-pending")
            red_move(store, it, "fix-pending", f"{red_what(d)}: fix {fix.get('entry') or fix.get('sha')} {fix['status']}",
                     data={"fix": fix})
            return [f"fix-pending {red_what(d)}"]
        if live:
            store.close_offer(f"red:{it['id']}", "taken")
            red_move(store, it, "leased", f"{red_what(d)}: leased by {row['holder']}", data={"holder": row["holder"]})
            return [f"leased {red_what(d)} to {row['holder']}"]
        if row and float(lease_meta(row).get("released_at") or 0) > it["entered_at"] and \
                float(row.get("acquired_at") or 0) >= it["entered_at"]:  # taken AND ended between two ticks: its attempt
            return red_lease_ended(store, it, row, block, t)
        if block:
            store.close_offer(f"red:{it['id']}", "blocked")
            red_move(store, it, "blocked-external", f"{red_what(d)}: {block['text']}",
                     data={"cause": block.get("cause"), "block": block["name"]})
            return [f"blocked {red_what(d)}"]
        if (d.get("blame") or {}).get("state") == "probing":  # its culprit is being decided: no escalation meanwhile
            if not store.offer_of(f"red:{it['id']}"):
                red_put_offer(store, it)
            return blame_step(store, it, repo, o_r, t) or []
        off = store.offer_of(f"red:{it['id']}") or {}
        if str(off.get("reason") or "").startswith("STARVED") and d.get("starved") != off.get("id"):
            # waited past RED_STARVE with no free fixer slot: escalated NOW (then every cycle, below)
            text = f"RED STARVED: {red_what(d)}: no fixer slot for {fmt_age(t - float(off.get('opened_at') or t))} ({off['reason']})"
            red_offer(store, it, text, data={"starved": off.get("id")}, escalate=True,
                      verdict=text + f" -- `~/bin/agentctl red claim {key} --worktree <yours>` to take it")
            return [text]
        if t >= it["deadline"]:  # no taker in time: escalate (the state's self-transition), widen, offer again
            if lease_is_live(store.get(red_lease(key)), t):  # taken since this step read its lease: never UNCLAIMED
                return acts  # with a holder; the next step moves it to leased
            why = off.get("reason") or "no taker"
            mins = int((t - it["entered_at"]) // 60)
            text = f"RED UNCLAIMED: {red_what(d)}: no taker for {mins} min ({why})"
            red_offer(store, it, text, data={"author_tried": True} if d.get("tier") == "author" else None,
                      verdict=text + f" -- `~/bin/agentctl red claim {key} --worktree <yours>` to take it")
            return [text]
        if not store.offer_of(f"red:{it['id']}"):  # repair: an offered red always has its open offer
            red_put_offer(store, it)
        return acts
    if st == "leased":
        if fix:
            red_stop_holder(store, key, f"its fix {fix.get('entry') or fix.get('sha')} is on its way", "fix submitted")
            red_move(store, it, "fix-pending", f"{red_what(d)}: fix {fix.get('entry') or fix.get('sha')} {fix['status']}",
                     data={"fix": fix})
            return [f"fix-pending {red_what(d)}"]
        if gone:
            red_close(store, it, "cleared", red_gone_why(d, obs))
            return [f"cleared {red_what(d)}"]
        if live:
            if row["holder"] != d.get("holder"):
                store.update_item("red", it["id"], it["version"], {"holder": row["holder"]})
            return acts
        return red_lease_ended(store, it, row, block, t)
    if st == "fix-pending":
        return red_fix_pending(store, it, fix, gone, obs, snap, facts, repo, o_r, t)
    if st == "blocked-external":
        if live:
            red_move(store, it, "leased", f"{red_what(d)}: a live fixer outranks the block", data={"holder": row["holder"]})
            return [f"leased {red_what(d)} (over its block)"]
        if gone:
            red_close(store, it, "cleared", red_gone_why(d, obs))
            return [f"cleared {red_what(d)}"]
        if fix:
            red_move(store, it, "fix-pending", f"{red_what(d)}: fix {fix.get('entry') or fix.get('sha')}", data={"fix": fix})
            return [f"fix-pending {red_what(d)}"]
        if not block:  # its block lapsed: the probe passed (verify it), or its justification or max hold is gone
            brow = store.get(d.get("block") or block_lease(key))
            out = str(lease_meta(brow).get("outcome") or "expired")
            if out.startswith("PROBE PASSED"):
                red_move(store, it, "fix-pending", f"{red_what(d)}: its block's check passes ({out[14:120]}): verifying",
                         data={"fix": {"status": "verify", "at": t, "entry": None, "sha": ""}, "block": None})
                return [f"verifying {red_what(d)} (its block cleared)"]
            red_offer(store, it, f"{red_what(d)}: its block ended ({out[:120]}): offered again",
                      data={"cause": None, "block": None},
                      escalate=not (out.startswith(("INVALID", "RELEASED")) or red_decision(d)))
            return [f"re-offered {red_what(d)} (block ended: {out[:60]})"]
        return acts
    return acts


RED_QUICK_EXIT = 120       # a fixer ending this soon with no verdict never got going
RED_START_BACKOFF = 600    # ...and its red waits this long (doubling) before the next dispatch

# AN ATTEMPT IS A FIXER THAT TRIED AND FAILED TO FIX (2026-11-04: RED ESCALATE 261 and FIXER FAILED 204 in a week; #728
# escalated 58 times on 11-02, each owner turn's "GAVE UP: ... the approved flip-unification stage 5 (a resolver rule
# change, outside red fixing)" counted as one more attempt at the deep tier). Not an attempt:
#   - a dispatched fixer's or owner's lease LOST TO INFRA (lapsed: the laptop asleep, the store out of reach, a wrapper
#     gone; or orphaned): offered again quietly -- a living wrapper takes its lapsed lease again itself
#     (fixer.HeldLease), and the crash-loop breaker still bounds launches that keep ending that way;
#   - "GAVE UP: already fixed": checked by the red's own probe (a newer decided run no longer failing this way) -- the
#     red is closed when it passes, else it waits in fix-pending (verify) for the run that decides it; a claim the run
#     refutes is an attempt then;
#   - "GAVE UP: needs the owner's decision / a design change": the red goes onto the owner ONCE, as the existing WAITING
#     ON YOU (red_decision_block) with the fixer's words as its diagnosis -- never offered again meanwhile.
GAVE_UP_FIXED = re.compile(r"\W*GAVE UP:.*?(\balready (?:fixed|fixes|landed|on (?:origin/)?main)\b|\bfixed upstream\b|"
                           r"\bno fix needed\b|\bnothing (?:left )?to (?:fix|land)\b)", re.I | re.S)
GAVE_UP_DECIDE = re.compile(r"\W*GAVE UP:.*?(\bneeds? (?:the |an? )?owner|\bowner'?s (?:choice|decision|call|approval)|"
                            r"\boutside red fixing\b|\bdesign (?:change|call|decision)\b|\bresolver (?:rule|design)\b)",
                            re.I | re.S)
LEASE_INFRA = re.compile(r"(lease expired|ORPHANED|STOPPED: the red's lease was lost)")


def red_lease_ended(store, it, row, block, t):
    """Its holder's lease ended (released with a verdict, expired without progress, revoked): one attempt (not for a
    release by hand, a fixer that never started, a lease lost to infra, a red found already fixed, or one that needs the
    owner's decision: AN ATTEMPT IS A FIXER THAT TRIED AND FAILED TO FIX), the verdict in its history, offered again up
    the ladder."""
    d = it["data"]
    h = red_history(row, t)
    m = lease_meta(row)
    quick = float(m.get("released_at") or t) - float((row or {}).get("acquired_at") or 0) < RED_QUICK_EXIT
    if quick and re.match(r"(exit -?\d+|ended) with no|NOT STARTED", str(h["outcome"])):  # it never got going (a crash at start): not an
        h["outcome"] = re.sub(r"^(NOT STARTED: )*", "NOT STARTED: ", str(h["outcome"]))  # attempt, nor an instant re-dispatch
    out = str(h["outcome"])
    infra = bool(h.get("dispatched") and LEASE_INFRA.match(out))  # a wrapper's lease renews by its beat: a lapse is
    # the wrapper or its machine gone (an interactive claim lapses by its own inactivity: that one counts)
    counted = not (out.startswith(("NOT STARTED", "RELEASED")) or infra or GAVE_UP_FIXED.match(out)
                   or GAVE_UP_DECIDE.match(out))
    upd = {"history": (d.get("history") or []) + [h], "holder": None,
           "attempts": int(d.get("attempts") or 0) + (1 if counted else 0)}
    if str(h["outcome"]).startswith("NOT STARTED") and m.get("tier") == "lander" and d.get("blame"):
        # THE LANDER COULD NOT BE REVIVED (LAUNCH FAILED): a fresh fixer at once, briefed with the change -- no backoff
        upd["blame"] = dict(d["blame"], revive_failed=str(h["outcome"])[:300])
    elif str(h["outcome"]).startswith("NOT STARTED"):
        fails = int(d.get("start_failures") or 0) + 1
        upd.update(start_failures=fails, dispatch_after=t + min(RED_START_BACKOFF * 2 ** (fails - 1), 3600))
    elif counted:
        upd.update(start_failures=0, dispatch_after=0)
    if str(h["outcome"]).startswith("DIAGNOSED:"):
        upd["diagnosis"] = h["outcome"][len("DIAGNOSED:"):].strip()[:600]
    wt = h.get("worktree")
    if wt and os.path.isdir(wt) and allowed_path(wt):  # its unlanded commits are never lost: the next fixer starts there
        left = [c[:10] for c in cherry_landed(wt, f"refs/remotes/{remote()}/main", "HEAD")[1] or []]
        if left:
            upd["prior_work"] = {"worktree": wt, "commits": left[:20]}
    if block:
        red_move(store, it, "blocked-external", f"{red_what(d)}: {block['text']}",
                 data={**upd, "cause": block.get("cause"), "block": block["name"]})
        return [f"blocked {red_what(d)}"]
    if GAVE_UP_FIXED.match(out):  # ALREADY FIXED: the red's probe decides, never the fixer's word alone
        return red_already_fixed(store, it, upd, out, t)
    if red_decision(d) and DECISION_FAILED.match(str(h["outcome"])):  # THE OWNER'S DECIDED ACTION FAILED: told
        return red_decision_failed(store, it, upd, h["outcome"], t)
    decide = red_decision_needed(d, h) if str(h["outcome"]).startswith("DIAGNOSED:") else None
    if not decide and GAVE_UP_DECIDE.match(out):  # THE FIXER NEEDS THE OWNER'S DECISION: one WAITING ON YOU, with its
        decide = re.sub(r"^\W*GAVE UP:\s*", "", out).strip()  # own words, never offered to the next fixer meanwhile
    if decide:
        return red_decision_block(store, it, upd, decide, t)
    streak = red_crash_streak({**d, **upd})
    if len(streak) >= env_num("AGENTCTL_RED_CRASH_LOOP", RED_CRASH_LOOP):
        return red_crash_loop(store, it, upd, streak, t)
    red_offer(store, it, f"{red_what(d)}: {h['holder']} ended ({h['outcome'][:120]}): offered again", data=upd,
              quiet=not counted)
    return [f"re-offered {red_what(d)} after {h['outcome'][:60]}"]


def red_already_fixed(store, it, upd, outcome, t):
    """Its fixer found the red already fixed on main: the red's probe (a newer decided run of its workflow no longer
    failing this way) closes it when it passes; else it waits in fix-pending (verify) for the run that decides it --
    cleared by it, or offered again, the claim then counted as an attempt (red_fix_pending)."""
    d = {**it["data"], **upd}
    args = {"repo": d.get("repo") or None, "workflow": d.get("workflow"), "item": it["id"],
            "run_id": (d.get("last_run") or {}).get("databaseId"), "run": (d.get("last_run") or {}).get("number")}
    if not args["repo"]:
        args.pop("repo")
    try:
        ok, why = probe_red_newer_verdict(args)
    except Exception as e:  # an unreadable snapshot decides nothing: the verify state waits for the run
        ok, why = None, f"probe failed ({type(e).__name__})"
    store.update_item("red", it["id"], it["version"], upd)
    it = store.item("red", it["id"])
    if ok:
        red_close(store, it, "cleared", f"its fixer found it already fixed, and {why}")
        return [f"cleared {red_what(d)} (already fixed: {why})"]
    store.close_offer(f"red:{it['id']}", "fix-pending")
    red_move(store, it, "fix-pending", f"{red_what(d)}: its fixer found it already fixed ({outcome[:160]}): verifying "
                                       f"({why})",
             data={"fix": {"status": "verify", "at": t, "entry": None, "sha": "", "claimed": outcome[:300]}})
    return [f"verifying {red_what(d)} (already fixed, says its fixer)"]


# THE CRASH-LOOP BREAKER (G9, convergence-failures-2026-10-29 §4: in E9 fixers were launched 52 times over 9 reds, #2335
# 18 and #2375 16 times, mostly `exit 1 with no LANDED / GAVE UP line`): RED_CRASH_LOOP dispatched fixers in a row that
# ended with NO RESULT (a crash, an exit with no verdict line, a lease lost or expired without progress, a time-out, a
# start that never got going) and the same tier is not dispatched blindly again. ONE escalation with the evidence (each
# launch's outcome and log tail), and the red moves UP: to the deep tier (crash_loop.stage `deep`) -- or, when the deep
# tier itself crash-looped, onto an OWNER BLOCK (block/<key>, cause `fixer-crash-loop`): the fixers, not the code, are
# what fails, so this is the one owner block a test red may have. It recovers by itself: its probe passes when a newer
# decided run of the red's workflow no longer fails this way (or the run is green), and it lapses after
# RED_CRASH_BLOCK_MAX (offered again, the streak counted afresh from crash_loop.mark). A launch with a result (LANDED,
# NOT LANDED, GAVE UP, BLOCKED ON OWNER, DIAGNOSED, a refuted fix) ends the streak; a release by hand is no launch.
RED_CRASH_LOOP = 3                 # $AGENTCTL_RED_CRASH_LOOP
RED_CRASH_BLOCK_MAX = 24 * 3600    # $AGENTCTL_RED_CRASH_BLOCK_MAX
RED_CRASH_OUTCOME = re.compile(r"(NOT STARTED|ended with no|exit -?\d+ with no|TIMED OUT|lease expired|"
                               r"STOPPED: the red's lease was lost|STALLED|ORPHANED|store auth failed)")
RED_CRASH_NEUTRAL = re.compile(r"(RELEASED|STOPPED: the fixer's wrapper was stopped)")
CRASH_LOOP_CAUSE = "fixer-crash-loop"


# THE HOST SLEEPING IS NO CRASH (2026-11-01, core/movies main #2046 `fixer-crash-loop`: both family owners' wrappers froze
# in a macOS maintenance sleep -- 03:05:44 to 03:21:11, longer than OWNER_TTL -- so their heartbeat leases lapsed
# ("lease expired"), the wrappers woke to "heartbeat lease was lost" and exited, the dispatcher restarted them, and three
# such cycles blocked the red on the owner with the harness' "cause" being nothing but a closed lid). A wrapper now holds
# a `caffeinate -s` assertion for its life (keep_awake), and a lease lapse that overlaps a host sleep is neutral to the
# breaker (host_slept) and says so in the evidence. `-s` holds only on AC power (owner 2026-11-01: "prevent laptop going
# to sleep when agentctl is running and power supply is on"); on battery the laptop sleeps and host_slept covers it.
# The service (waiting run --service) holds the same assertion, so a plugged-in laptop never sleeps under agentctl.
LEASE_LAPSE = re.compile(r"(lease expired|ORPHANED|STOPPED: the (red's|owner's) (heartbeat )?lease was lost)")
SLEEP_MIN = 120      # s of sleep inside a lapse's window that count as "the host was asleep" ($AGENTCTL_SLEEP_MIN)
_SLEEP_CACHE = [0.0, []]


def keep_awake(pid=None):
    """Hold a no-system-sleep assertion, valid on AC power only, for `pid` (default: this process) so a held lease is
    never starved by the laptop's sleep. Once per pid: an exec'd restart keeps the assertion it already has. Best
    effort: the Popen, or None (already held, no caffeinate, $AGENTCTL_CAFFEINATE empty)."""
    exe = os.environ.get("AGENTCTL_CAFFEINATE", "/usr/bin/caffeinate")
    pid = str(pid or os.getpid())
    if not exe or not os.access(exe, os.X_OK) or os.environ.get("AGENTCTL_AWAKE_PID") == pid:
        return None
    os.environ["AGENTCTL_AWAKE_PID"] = pid
    try:
        return spawn_detached([exe, "-s", "-w", pid], stdin=subprocess.DEVNULL,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError:
        return None


def sleep_intervals():
    """[(start, end)] epoch seconds of the host's recent sleeps, from `pmset -g log` (or $AGENTCTL_SLEEP_LOG, a file in
    its format). [] when unreadable. Cached for a minute."""
    now = time.time()
    if now - _SLEEP_CACHE[0] < 60 and not os.environ.get("AGENTCTL_SLEEP_LOG"):
        return _SLEEP_CACHE[1]
    try:
        path = os.environ.get("AGENTCTL_SLEEP_LOG")
        if path:
            text = open(path).read()
        else:
            text = subprocess.run(["pmset", "-g", "log"], capture_output=True, text=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        text = ""
    out, start = [], None
    for m in re.finditer(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d [+-]\d{4}) (Sleep|Wake|DarkWake)\b", text, re.M):
        try:
            at = dt.datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S %z").timestamp()
        except ValueError:
            continue
        if m.group(2) == "Sleep":
            start = at
        elif start is not None:
            out.append((start, at))
            start = None
    if not os.environ.get("AGENTCTL_SLEEP_LOG"):
        _SLEEP_CACHE[:] = [now, out]
    return out


def host_slept(end, window):
    """Seconds the host slept in [end - window, end] (end: epoch seconds)."""
    lo = end - window
    return sum(max(0.0, min(b, end) - max(a, lo)) for a, b in sleep_intervals())


def lapse_in_sleep(h):
    """True for a launch whose lease merely lapsed (no work seen) while the host slept: not a crash."""
    o = str(h.get("outcome") or "")
    if not LEASE_LAPSE.search(o):
        return False
    try:
        end = dt.datetime.strptime(str(h.get("at")), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc).timestamp()
    except ValueError:
        return False
    return host_slept(end, OWNER_TTL + 300) >= env_num("AGENTCTL_SLEEP_MIN", SLEEP_MIN)


def red_crash_streak(d):
    """The newest fixer launches (newest first) since the red's last crash-loop step that ended with no result -- up to
    the first launch (or fix) with one. A lease lapse while the host slept is no launch (host_slept)."""
    hist = d.get("history") or []
    out = []
    for h in reversed(hist[int((d.get("crash_loop") or {}).get("mark") or 0):]):
        o = str(h.get("outcome") or "")
        if RED_CRASH_NEUTRAL.match(o) or (h.get("dispatched") and lapse_in_sleep(h)):
            continue
        if not h.get("dispatched") or not RED_CRASH_OUTCOME.match(o):
            break
        out.append(h)
    return out


LAUNCH_FAILURE = re.compile(r"^(?:NOT STARTED: )+`claude --bg` (.*)$", re.S)


def launch_failure(outcome):
    """A launch `claude --bg` refused (fixer.bg_start: `exit N: <its stderr>`, or `failed (<error>)`): that text, which
    is the whole story -- the session never started, so there is no log to read. None for any other outcome."""
    m = LAUNCH_FAILURE.match(str(outcome or ""))
    return " ".join(m.group(1).split()) if m else None


def red_crash_evidence(streak):
    """The escalation's tail: each crashed launch's outcome, then the last lines of its log."""
    tail = []
    for h in reversed(streak):
        failed = launch_failure(h.get("outcome"))
        if failed is not None:
            tail.append(f"{h.get('at')} {h.get('holder')}: LAUNCH FAILED: {failed[:400]}")
            continue
        tail.append(f"{h.get('at')} {h.get('holder')}: {str(h.get('outcome'))[:200]}"
                    + (f" (log: {str(h['log']).replace(HOME, '~')})" if h.get("log") else ""))
        tail += [f"    {l[:200]}" for l in str(h.get("notes") or "").splitlines()[-4:] if l.strip()]
    return tail[-30:]


def red_crash_cmd(streak):
    """The owner's look at the crashes: `red status -v`, then the newest crashed launch's log (its wrapper's stderr or its
    transcript), else its worktree -- never a `less ?`, nor a log that was never written (a launch that failed)."""
    for h in streak:
        for p in (h.get("log"), h.get("worktree")):
            if p and os.path.exists(p):
                return (f"~/bin/agentctl red status -v; " + (f"less {p}" if not os.path.isdir(p) else f"cd {p}")
                        ).replace(HOME, "~")
    return "~/bin/agentctl red status -v"


def red_crash_launches(streak):
    """Each crashed launch, oldest first: when, and how it ended (its stop reason, or its log's last line; a launch
    `claude --bg` refused: LAUNCH FAILED and its stderr)."""
    out = []
    for h in reversed(streak):
        failed = launch_failure(h.get("outcome"))
        if failed is not None:
            why = f"LAUNCH FAILED: {failed[:300]}"
        else:
            last = [l.strip() for l in str(h.get("notes") or "").splitlines() if l.strip()]
            why = str(h.get("outcome"))[:120] + (f" -- {last[-1][:100]}" if last else "")
        out.append(f"{str(h.get('at'))[11:16]}Z {why}")
    return "; ".join(out)


def red_crash_loop(store, it, upd, streak, t):
    """RED_CRASH_LOOP launches in a row with no result: escalated once, with the evidence, and moved up -- deep tier,
    or (the deep tier crash-looped too) an owner block that clears itself."""
    d = {**it["data"], **upd}
    key, n = d["key"], len(streak)
    tail = red_crash_evidence(streak)
    mark = len(d.get("history") or [])
    tier = it["data"].get("tier")
    run = (d.get("last_run") or {}).get("number")
    if not (d.get("crash_loop") or {}).get("stage") and tier != "deep":
        verdict = (f"RED ESCALATE: {red_what(d)}: CRASH LOOP -- {n} fixer launches in a row ({tier}) ended with no "
                   f"LANDED / GAVE UP; not dispatched that way again: moved up to the deep tier "
                   f"(`~/bin/agentctl red status -v`)")
        upd = {**upd, "crash_loop": {"stage": "deep", "mark": mark, "at": t, "from": tier, "launches": n},
               "start_failures": 0, "dispatch_after": 0}
        red_offer(store, it, verdict, data=upd, escalate=True, verdict=verdict, tail=tail)
        return [f"crash loop on {red_what(d)}: moved up to deep"]
    until = f"a newer {d.get('workflow')} run than #{run} no longer fails this way"
    cmd = red_crash_cmd(streak)
    text = (f"BLOCKED ON OWNER: the fixers crash on {red_what(d)}: {n} launches in a row at the {tier} tier ended with "
            f"no LANDED / GAVE UP ({red_crash_launches(streak)}). The fixer harness, not the code, is what fails: look "
            f"at their logs (`! {cmd}`). Auto-clears when {until}, or in "
            f"{fmt_age(env_num('AGENTCTL_RED_CRASH_BLOCK_MAX', RED_CRASH_BLOCK_MAX))} (offered again)")
    lname = block_lease(key)
    probe = {"kind": "red_newer_verdict", "args": {"workflow": d.get("workflow"), "item": it["id"],
                                                    "run_id": (d.get("last_run") or {}).get("databaseId"),
                                                    "run": run}}
    got = store.acquire(lname, f"owner:{CRASH_LOOP_CAUSE}", BLOCK_TTL,
                        env_num("AGENTCTL_RED_CRASH_BLOCK_MAX", RED_CRASH_BLOCK_MAX), progress=f"crash-loop@{int(t)}",
                        meta={"kind": "block", "crash_loop": True, "cause": CRASH_LOOP_CAUSE, "cmd": cmd, "probe": probe,
                              "until": until, "keys": [key], "text": text, "details": text, "probed_at": t,
                              "question": f"the fixers crash on {red_what(d)} ({n} launches in a row): repair the "
                                          "fixer harness"})
    if "held" in got:  # somebody's block already holds the red: it stands, the red joins it
        row = store.get(lname)
        store.set_meta(lname, row["epoch"], row["secret"], {"keys": sorted(set(lease_meta(row).get("keys") or []) | {key})})
    upd = {**upd, "crash_loop": {"stage": "owner", "mark": mark, "at": t, "from": tier, "launches": n},
           "cause": CRASH_LOOP_CAUSE, "block": lname}
    line = f"RED BLOCKED ON OWNER: {red_what(d)}: {text}"
    store.close_offer(f"red:{it['id']}", "blocked")
    red_ask_mark(store, red_move(store, it, "blocked-external", line, data=upd, severity="escalate",
                                 refs={"key": key, "run": run, "workflow": d.get("workflow"), "verdict": line,
                                       "tail": tail, "command": "red"}), t)
    return [f"crash loop on {red_what(d)}: blocked on the owner"]


# A DIAGNOSIS THAT NEEDS THE OWNER (2026-11-01, convergence #579: "resolver design ... The gate needs your design
# call"): DIAGNOSED was no end -- the red went back to its fixer (the family's owner), who diagnosed it again, turn after
# turn. A DIAGNOSED verdict that names the owner's decision (`DIAGNOSED: <cause>; decide: <the decision>`), or a second
# DIAGNOSED in a row (the implementer briefed with the first could not make the fix either: crashes and lapses between
# them are skipped), puts the red on an OWNER-DECISION BLOCK (block/<key>, cause `owner-decision`) whose text carries
# the diagnosis and the decision -- never a crash, never the crash-loop breaker's. Like the crash-loop block it is one
# a test red may hold, and it clears itself: a newer run of the red's workflow no longer failing this way, or
# RED_DECISION_BLOCK_MAX (offered again); the owner lifts it at once with `agentctl red release <key> --any`.
RED_DECISION_BLOCK_MAX = 7 * 86400   # an owner-decision block lapses after this: the red offered again (2026-11-03: was
# 24 h -- each lapse re-offered the red to fixers who could not decide it, and the RED ESCALATE / GAVE UP entries
# piled up; the owner is reminded instead (ask.py), and the block's probe still clears it)
DECISION_CAUSE = "owner-decision"
RED_DECIDE = re.compile(r";\s*decide:\s*(.+)$", re.S | re.I)


def red_decision_needed(d, h):
    """The decision the owner must make on red d, whose launch h ended DIAGNOSED, or None (an implementer goes on)."""
    m = RED_DECIDE.search(str(h["outcome"]))
    if m:
        return m.group(1).strip()
    for p in reversed(d.get("history") or []):
        o = str(p.get("outcome") or "")
        if RED_CRASH_OUTCOME.match(o) or RED_CRASH_NEUTRAL.match(o):
            continue
        if o.startswith("DIAGNOSED:"):
            return ("it was DIAGNOSED twice in a row and its fixers cannot fix it within the red rules: decide how it "
                    "is fixed (a design change, a known flip to accept, or the fix a fixer may make)")
        return None
    return None


def red_decision_block(store, it, upd, decide, t):
    """The red waits on the owner's decision: its block carries the diagnosis and the decision, clears itself."""
    d = {**it["data"], **upd}
    key, run = d["key"], (d.get("last_run") or {}).get("number")
    diag = RED_DECIDE.sub("", str(d.get("diagnosis") or "")).strip()
    nb = (read_record(owner_record_path(red_queue_family(d))) or {}).get("notebook")
    cmd = f"~/bin/agentctl red status -v" + (f"; less {nb}" if nb else "")
    until = f"a newer {d.get('workflow')} run than #{run} no longer fails this way"
    maxh = env_num("AGENTCTL_RED_DECISION_BLOCK_MAX", RED_DECISION_BLOCK_MAX)
    text = (f"WAITING ON YOU: {decide[:400]} -- {red_what(d)} waits on it. Answer: `! {decide_cmd(key)}` (its fixer "
            f"is dispatched with your answer). " + (f"Why: DIAGNOSED: {diag[:400]}. " if diag else "") +
            f"Look: `{cmd}`. Auto-clears when {until}, or in {fmt_age(maxh)} (offered again)").replace(HOME, "~")
    lname = block_lease(key)
    probe = {"kind": "red_newer_verdict", "args": {"workflow": d.get("workflow"), "item": it["id"],
                                                    "run_id": (d.get("last_run") or {}).get("databaseId"), "run": run}}
    got = store.acquire(lname, f"owner:{DECISION_CAUSE}", BLOCK_TTL, maxh, progress=f"decision@{int(t)}",
                        meta={"kind": "block", "decision": True, "cause": DECISION_CAUSE, "cmd": cmd, "probe": probe,
                              "until": until, "keys": [key], "text": text, "details": diag, "decide": decide,
                              "question": decide, "probed_at": t})
    if "held" in got:  # somebody's block already holds the red: it stands, the red joins it
        row = store.get(lname)
        store.set_meta(lname, row["epoch"], row["secret"], {"keys": sorted(set(lease_meta(row).get("keys") or []) | {key})})
    line = f"RED BLOCKED ON OWNER: {text}"
    store.close_offer(f"red:{it['id']}", "blocked")
    red_ask_mark(store, red_move(store, it, "blocked-external", line, data={**upd, "cause": DECISION_CAUSE, "block": lname},
                                 severity="escalate", refs={"key": key, "run": run, "workflow": d.get("workflow"),
                                                            "verdict": line, "command": "red"}), t)
    return [f"decision needed on {red_what(d)}: blocked on the owner"]


def probe_red_newer_verdict(a):
    """A newer decided run of the red's workflow on main changed its verdict: green, or no longer attached to the red
    (a run failing the same way is attached to it by the tick's observation, before the blocks are probed)."""
    snap = ci_state_load(a.get("repo") or SHORT["movies"])
    last = ((snap or {}).get("latest") or {}).get(a.get("workflow"))
    if not last:
        return None, f"no decided run of {a.get('workflow')} on main in the snapshot"
    if int(last.get("databaseId") or 0) <= int(a.get("run_id") or 0):
        return False, f"no {a.get('workflow')} run newer than #{a.get('run')}"
    if last.get("conclusion") == "success":
        return True, f"{a.get('workflow')} #{last.get('number')} green"
    it = Store().item("red", a.get("item")) if a.get("item") else None
    if it and str((it["data"].get("last_run") or {}).get("databaseId")) == str(last.get("databaseId")):
        return False, f"{a.get('workflow')} #{last.get('number')} still fails this way"
    return True, f"{a.get('workflow')} #{last.get('number')} {last.get('conclusion')}, no longer this failure"


RED_OLD_RUN = 2 * 86400  # an outside red whose newest run is older than this is checked by a fresh run before it is fixed


def red_old_run(d, t):
    """The age of an outside red's newest failing run when it is older than RED_OLD_RUN and not yet re-checked, else 0."""
    if d.get("red_kind") != "outside" or d.get("old_checked"):
        return 0
    try:
        age = t - parse_time((d.get("last_run") or {}).get("createdAt")).timestamp()
    except (ValueError, TypeError, AttributeError):
        return 0
    return age if age > env_num("AGENTCTL_RED_OLD_RUN", RED_OLD_RUN) else 0


def red_resolve_old(store, it, age, o_r, repo, t):
    """A dispatchable workflow is run again on main (fix-pending: verify) -- green verifies it, the same failure makes it
    an ordinary red. One that no fresh run can check (no workflow_dispatch, its file gone from main, or the dispatch
    refused) returns None: it stays an ordinary red, offered and dispatched like any other. It is never RETIRED:
    "Report run timings" #38 (2026-10-29, a workflow_call-only workflow) was retired, re-opened by the next tick's same
    observation and retired again 811 times in 19 h, never offered, while ci-why showed it "offering" for 9 days."""
    d = it["data"]
    path, txt = workflow_file_of(repo, f"{remote()}/main", d.get("workflow")) if repo else (None, "")
    if path and "workflow_dispatch" in txt:
        line = red_verify_dispatch(o_r, repo, d.get("workflow"), "", (d.get("last_run") or {}).get("databaseId"))
        if line.startswith("dispatched"):
            store.close_offer(f"red:{it['id']}", "old run")
            red_move(store, it, "fix-pending", f"{red_what(d)}: its newest run is {fmt_age(age)} old: {line}",
                     data={"old_checked": True, "fix": {"status": "verify", "at": t, "entry": None, "sha": "",
                                                        "verify_dispatched": ts(), "verify": line}})
            return [f"old red {red_what(d)}: {line}"]
        why = line
    else:
        why = (f"{d.get('workflow')} has not run for {fmt_age(age)}" +
               (" and has no workflow_dispatch" if path else " and its workflow file is gone from main"))
    store.update_item("red", it["id"], it["version"], {"old_checked": True,
                                                      "old_note": f"no fresh run can check it ({why[:200]}): a fixer decides"})
    return None


# A FIX FOLLOWS ITS WORK, NOT ITS CHANGE ID (2026-10-31, Main #2877): "fix 20261031-073114-movies-wt-flakes-9f6bc4
# waiting to land (joined)" for hours -- the change was CANCELLED (the store's item said so; the entry the facts read
# still said `waiting`) and the same worktree re-queued as ...-ed6118, which LANDED d677b1bb6. The store's change item
# is the truth about a change; a fix whose change ended without landing follows its worktree to the later change that
# carries its work (open: waiting on it; landed: verifying from its landing), or its own commits found on main by
# content; with neither, it is offered again once RED_FIX_REQUEUE_GRACE passed since the cancel (a re-queue is quick).
CHANGE_ENDED = ("cancelled", "superseded", "rejected")
RED_FIX_REQUEUE_GRACE = 900  # $AGENTCTL_RED_FIX_REQUEUE_GRACE


def red_change(store, eid):
    try:
        return store.item("change", eid) if eid else None
    except Exception:  # an unread store decides nothing: the facts stand
        return None


def red_fix_fresh(store, fix):
    """The fix red_fix_of read from the facts, corrected by its change's store item: None when that change ended
    without landing, landed when the store says so; as read when the store has no word on it."""
    if not fix or not fix.get("entry") or fix.get("status") == "landed":
        return fix
    ch = red_change(store, fix["entry"])
    if not ch:
        return fix
    if ch["state"] in CHANGE_ENDED:
        return None
    if ch["state"] == "landed":
        sha = ch["data"].get("landed_sha") or ch["data"].get("tip")
        return {**fix, "status": "landed", "sha": sha, "at": float(ch.get("entered_at") or store.clock())} if sha else fix
    return {**fix, "status": ENTRY_STATUS.get(ch["state"], fix.get("status"))}


def red_fix_follow(store, f, repo, t):
    """(fix, its change's state) for a not-yet-landed fix f whose change ENDED without landing: the change that carries
    its work since -- the earliest later change of the same worktree (landed, or still open) -- or f's own commits
    landed on main by content; (None, state) when there is none; (None, None) when its change has not ended."""
    ch = red_change(store, f.get("entry"))
    if not ch or ch["state"] not in CHANGE_ENDED:
        return None, None
    cd = ch["data"] or {}
    wt, since = cd.get("worktree"), float(cd.get("submitted_at") or 0)
    later = []
    try:
        later = sorted((o for o in store.items("change") if o["id"] != ch["id"] and wt and o["data"].get("worktree") == wt
                        and float(o["data"].get("submitted_at") or 0) >= since and o["state"] not in CHANGE_ENDED),
                       key=lambda o: float(o["data"].get("submitted_at") or 0))
    except Exception:
        pass
    for o in later:
        od = o["data"]
        sha = od.get("landed_sha") or od.get("tip")
        if o["state"] == "landed" and sha:
            return {"entry": o["id"], "status": "landed", "sha": sha, "at": float(o.get("entered_at") or t),
                    "followed": f.get("entry"), "author": f.get("author")}, ch["state"]
        if o["state"] != "landed":
            return {"entry": o["id"], "status": ENTRY_STATUS.get(o["state"], o["state"]), "followed": f.get("entry"),
                    "author": f.get("author")}, ch["state"]
    head, base = cd.get("head"), cd.get("base")
    if repo and head:
        commits, left = cherry_landed(repo, f"refs/remotes/{remote()}/main", head, *((base,) if base else ()))
        if commits and left == []:
            return {"entry": f.get("entry"), "status": "landed", "sha": head, "at": t, "followed": f.get("entry"),
                    "by_content": True, "author": f.get("author")}, ch["state"]
    return None, ch["state"]


def red_fix_pending(store, it, fix, gone, obs, snap, facts, repo, o_r, t):
    d = it["data"]
    f = d.get("fix") or {}
    if fix and (fix.get("entry"), fix.get("sha"), fix["status"]) != (f.get("entry"), f.get("sha"), f.get("status")):
        if fix["status"] == "landed" and f.get("status") != "landed":
            # VERIFICATION STARTS WHEN THE FIX LANDS: the state's deadline (and red_fix_since) run from here, never
            # from the hours it waited in the queue behind a red gate
            it = red_move(store, it, "fix-pending", f"{red_what(d)}: fix {fix.get('entry') or fix.get('sha')} landed "
                                                    f"({str(fix.get('sha'))[:10]}): verifying",
                          data={"fix": {**f, **fix, "verify_since": t} if f.get("status") in FIX_OPEN else {**f, **fix}})
        else:
            store.update_item("red", it["id"], it["version"], {"fix": {**f, **fix}})
            it = store.item("red", it["id"])
        d, f = it["data"], it["data"]["fix"]
    if f.get("status") == "owner":  # an owner-only repo's fix, filed with the owner (red owner-push): a green run proves it
        if obs is not None and gone:
            line = f"FIX OK: {red_what(d)} cleared after its fix went to the owner ({f.get('entry')})"
            inbox_write("red", line, [line], [], 0)
            store.close_offer(f"red:{it['id']}", "verified")
            red_move(store, it, "verified", line)
            return [line]
        if t - red_fix_since(it) >= env_num("AGENTCTL_RED_FIX_GIVEUP", RED_FIX_GIVEUP):
            return red_fix_unverifiable(store, it, f"its owner push ({f.get('entry')}: `{f.get('cmd')}`) is unproven "
                                                   f"{fmt_age(t - red_fix_since(it))} after it was filed", t)
        if t >= it["deadline"]:  # the owner has not pushed yet, or no gate run has decided since: said again
            line = f"RED OWNER PUSH WAITING: {red_what(d)}: `! {f.get('cmd')}` ({fmt_age(t - red_fix_since(it))} since filed)"
            inbox_write("red", line, [line], [], 1)
            red_move(store, it, "fix-pending", line)
            return [line]
        return []
    if f.get("status") == "verify":  # an owner block's check passed: the next decided run says whether it is fixed
        last = ((snap or {}).get("latest") or {}).get(d.get("workflow")) or {}
        after = bool(last.get("updatedAt")) and parse_time(last["updatedAt"]).timestamp() > float(f.get("at") or 0)
        if obs is not None and gone:
            line = f"FIX OK: {red_what(d)} cleared after its owner block's check passed"
            inbox_write("red", line, [line], [], 0)
            red_move(store, it, "verified", line)
            return [line]
        if obs is not None and after and d["key"] in obs:
            if f.get("claimed"):  # its fixer said "already fixed" and the run refutes it: that fixer's attempt failed
                red_offer(store, it, f"{red_what(d)}: still red after its fixer found it already fixed: offered again",
                          data={"fix": None, "attempts": int(d.get("attempts") or 0) + 1})
                return [f"re-offered {red_what(d)} (not fixed after all)"]
            red_offer(store, it, f"{red_what(d)}: still red after its block cleared: offered again", data={"fix": None})
            return [f"re-offered {red_what(d)} (still red after its block)"]
        if not d.get("gate") and not f.get("verify_dispatched"):
            line = red_verify_dispatch(o_r, repo, d.get("workflow"), "", (d.get("last_run") or {}).get("databaseId"))
            store.update_item("red", it["id"], it["version"], {"fix": {**f, "verify_dispatched": ts(), "verify": line}})
            if line.startswith("dispatched"):
                return [line]
            it = store.item("red", it["id"])
            f = it["data"]["fix"]
        # NO VERIFY CAN COME: its verifying dispatch failed, or no decided run came by the deadline -- a fix-pending with
        # no fix on its way never rests (Archive recording #823, 2026-10-29: the dispatch was refused with HTTP 400, its
        # workflow runs only on demand, and the red sat "fix None verify" for 19 h, escalating every 3 h, no fixer)
        failed = str(f.get("verify") or "").startswith("FIX UNVERIFIED:")  # refused; "until its next scheduled run" waits
        if failed or t - it["entered_at"] >= env_num("AGENTCTL_RED_FIX_GIVEUP", RED_FIX_GIVEUP):
            return red_fix_unverifiable(store, it, f"its verifying run could not start ({str(f.get('verify'))[:120]})"
                                        if failed else f"no decided {d.get('workflow')} run "
                                                       f"{fmt_age(t - it['entered_at'])} after it entered verify", t)
        if t >= it["deadline"]:
            red_move(store, it, "fix-pending", f"RED {red_what(d)}: unverified {fmt_age(t - it['entered_at'])} after its block")
            return [f"escalated {red_what(d)}: unverified"]
        return []
    if f.get("status") != "landed":
        status = red_entry_status(facts, f.get("entry"))
        cur = red_fix_fresh(store, f)
        if cur is not f and cur:  # the store's word on its change outranks the entry the facts read
            if cur.get("status") == "landed":
                line = f"{red_what(d)}: fix {f.get('entry')} landed ({str(cur['sha'])[:10]}): verifying"
                red_move(store, it, "fix-pending", line, data={"fix": {**cur, "verify_since": t}})
                return [line]
            status = cur.get("status")
        moved, ended = red_fix_follow(store, f, repo, t)
        if moved and moved["status"] == "landed":
            line = (f"{red_what(d)}: its fix {f.get('entry')} ended {ended}; its work landed in {moved['entry']} "
                    f"({str(moved['sha'])[:10]}): verifying")
            red_move(store, it, "fix-pending", line, data={"fix": {**moved, "verify_since": t}})
            return [line]
        if moved:
            if moved["entry"] != f.get("entry"):
                store.update_item("red", it["id"], it["version"], {"fix": moved})
            return []
        if ended:
            ch = red_change(store, f.get("entry")) or {}
            if t - float(ch.get("entered_at") or 0) < env_num("AGENTCTL_RED_FIX_REQUEUE_GRACE", RED_FIX_REQUEUE_GRACE):
                return []  # a re-queue of its worktree may follow the cancel
            status = ended
        if status in FIX_OPEN:
            return []
        if not fix:  # its change was rejected, cancelled or superseded, and no other fix is on its way
            h = {"at": iso_at(t), "holder": f.get("entry"), "outcome": f"its fix change {f.get('entry')} ended {status}"}
            if red_decision(d):  # the owner's decided action: its change did not land -- the owner is told
                return red_decision_failed(store, it, {"attempts": int(d.get("attempts") or 0) + 1, "fix": None,
                                                       "history": (d.get("history") or []) + [h]},
                                           f"NOT LANDED: {h['outcome']}", t)
            red_offer(store, it, f"{red_what(d)}: {h['outcome']}: offered again",
                      data={"attempts": int(d.get("attempts") or 0) + 1, "history": (d.get("history") or []) + [h],
                            "fix": None})
            return [f"re-offered {red_what(d)} ({h['outcome']})"]
        return []
    sha = f.get("sha") or ""
    last = ((snap or {}).get("latest") or {}).get(d.get("workflow")) or {}
    inside = contains_local(repo, sha, last.get("headSha")) if sha and last else False
    holds = bool(inside) or (inside is None and bool(last.get("updatedAt"))
                             and parse_time(last["updatedAt"]).timestamp() > float(f.get("at") or 0) + WATCH_SLACK)
    if d.get("workflow") == LOCAL_FLOW:
        holds = gone
    if obs is not None and gone and (holds or d.get("red_kind") in ("local", "flake", "stuck-run", "predicts")):
        if d.get("red_kind") == "flake" and not holds:
            return []
        line = f"FIX OK: {red_what(d)} cleared by {sha[:10]}" + (f" (#{last.get('number')})" if last else "")
        inbox_write("red", line, [line], [], 0)
        store.close_offer(f"red:{it['id']}", "verified")
        red_stop_holder(store, d["key"], f"verified by {sha[:10]}", "red verified")
        red_move(store, it, "verified", line)
        return [line]
    if obs is not None and holds and d["key"] in obs and str(obs[d["key"]]["run"].get("databaseId")) == str(last.get("databaseId")):
        # THE FIX DID NOT FIX IT: refuted, its holder's lease dropped, offered again with the failed fix in the picture
        red_stop_holder(store, d["key"], f"your fix {sha[:10]} is refuted by #{last.get('number')}", "fix refuted",
                        force=True)
        refuted_blame = blame_refuted(repo, d.get("blame"), sha)  # the red's own revert: the culprit was wrong
        line = f"FIX FAILED: {red_what(d)}: #{last.get('number')} still fails this way (fix {sha[:10]})"
        inbox_write("red", line, [line], [d.get("line") or ""], 1)
        refuted = (d.get("refuted") or []) + [sha]
        h = {"at": iso_at(t), "holder": f.get("entry") or sha[:10], "outcome": f"fix {sha[:10]} refuted by #{last.get('number')}",
             "session": f.get("session"), "worktree": f.get("worktree")}
        red_offer(store, it, line, data={"attempts": int(d.get("attempts") or 0) + 1, "refuted": refuted, "fix": None,
                                         "history": (d.get("history") or []) + [h],
                                         "recurring": len(refuted) >= RED_RECURRING,
                                         **({"blame": refuted_blame} if refuted_blame else {})})
        if refuted_blame:
            line2 = (f"RED REVERT REFUTED: {red_what(d)}: reverting {sha[:10]} did not clear it, so its change was not "
                     "the culprit: it is relanded (its revert reverted) and the red goes to the normal fixer")
            inbox_write("red", line2, [line2], [], 1)
            line = f"{line}\n{line2}"
        return [line]
    if obs is not None and gone and not holds:
        red_close(store, it, "cleared", f"a run before its fix {sha[:10]} no longer fails this way")
        return [f"cleared {red_what(d)}"]
    if not holds and not d.get("gate") and not f.get("verify_dispatched") and t - float(f.get("at") or t) > RED_FIX_RUN_START \
            and d.get("red_kind") in ("outside",):
        line = red_verify_dispatch(o_r, repo, d.get("workflow"), sha, (d.get("last_run") or {}).get("databaseId"))
        store.update_item("red", it["id"], it["version"], {"fix": {**f, "verify_dispatched": ts(), "verify": line}})
        return [line]
    if t - red_fix_since(it) >= env_num("AGENTCTL_RED_FIX_GIVEUP", RED_FIX_GIVEUP):
        return red_fix_unverifiable(store, it, f"its fix {sha[:10] or f.get('entry')} unverified for "
                                               f"{fmt_age(t - red_fix_since(it))} after it landed (no run of "
                                               f"{d.get('workflow')} proves it)", t)
    if t >= it["deadline"]:
        red_move(store, it, "fix-pending", f"RED {red_what(d)}: fix {sha[:10] or f.get('entry')} unverified for "
                                           f"{fmt_age(t - it['entered_at'])}")
        return [f"escalated {red_what(d)}: fix unverified"]
    return []


def red_fix_since(it):
    """When a fix-pending red's verification began: the tick that saw its queued fix land (a fix waits in the queue
    behind a red gate for hours, untested), else entering fix-pending."""
    f = it["data"].get("fix") or {}
    return max(float(it["entered_at"]), float(f.get("verify_since") or 0))


def red_fix_unverifiable(store, it, why, t):
    """A fix-pending that cannot rest any longer: offered again (escalated), its fix set aside so it does not snap back
    to fix-pending, the reason in its history -- the dispatcher gives it a fixer."""
    d = it["data"]
    f = d.get("fix") or {}
    h = {"at": iso_at(t), "holder": f.get("entry") or (f.get("sha") or "")[:10] or None, "outcome": f"UNVERIFIED: {why}"}
    upd = {"fix": None, "history": (d.get("history") or []) + [h]}
    if f.get("sha"):
        upd["unverified"] = (d.get("unverified") or []) + [f["sha"]]
    store.close_offer(f"red:{it['id']}", "unverified")
    red_offer(store, it, f"{red_what(d)}: {why}: offered again", escalate=True, data=upd)
    return [f"re-offered {red_what(d)} ({why[:80]})"]


def red_verify_dispatch(o_r, repo, workflow, sha, run_id=None):
    """The red's workflow dispatched on main to verify a landed fix (no wait: the next tick reads its run), with the
    failing run's own inputs (run_id's) so the verifying run repeats it. Archive recording #823 (2026-10-29): dispatched
    with no inputs, its required `runs` input made Forgejo refuse it (HTTP 400), so no run could ever verify the red."""
    path, txt = workflow_file_of(repo, f"{remote()}/main", workflow) if repo and workflow else (None, "")
    if not path:
        return f"FIX UNVERIFIED: no workflow file named {workflow!r} on main"
    if "workflow_dispatch" not in txt:
        return f"FIX UNVERIFIED until {workflow}'s next scheduled run (no workflow_dispatch)"
    inputs = run_dispatch_inputs(o_r, run_id) if run_id else {}
    try:
        api(o_r, "POST", f"actions/workflows/{os.path.basename(path)}/dispatches", body={"ref": "main", "inputs": inputs})
        record_dispatch(branch="main", workflow=workflow, sha=sha, agent="red verify", pid=os.getpid(), inputs=inputs,
                        purpose=f"verify the fix {sha[:10]}", key=f"verify-{os.path.basename(path)}-{int(time.time())}")
    except (Exception, SystemExit) as e:
        return f"FIX UNVERIFIED: could not dispatch {workflow} ({http_error_text(e)[:240]})"
    return f"dispatched {workflow} on main to verify {sha[:10]}" + (f" with {json.dumps(inputs)}" if inputs else "")


def run_dispatch_inputs(o_r, run_id):
    """The workflow_dispatch inputs run_id was started with ({} for any other event, or when its run cannot be read).
    GitHub serves no run's inputs: they are the dispatch record's (agentctl's own, written by `dispatch run`)."""
    rec = read_record(dispatches_dir("inputs", f"{run_id}.json"))
    return {str(k): str(v) for k, v in rec.items()} if isinstance(rec, dict) else {}


def http_error_text(e):
    """An exception's text, with the response body when it is an HTTP error (the forge names a refusal's cause there)."""
    text = str(e)
    if isinstance(e, urllib.error.HTTPError):
        try:
            body = e.read().decode("utf-8", "replace").strip()
        except Exception:
            body = ""
        if body:
            text += f": {body}"
    return text


def red_flake_check(store, it, snap, repo, facts):
    """A test red that cleared with no commit between that can have fixed it: a FLAKE, its own red (determinism tier,
    lowest priority), open until a fix for it lands and a run proves it."""
    d = it["data"]
    if d.get("kind") != "test" or d.get("red_kind") not in ("gate", "outside"):
        return []
    last = ((snap or {}).get("latest") or {}).get(d.get("workflow")) or {}
    if last.get("conclusion") != "success":
        return []
    rec = {"runs": d.get("runs"), "job": d.get("job"), "line": d.get("line"), "workflow": d.get("workflow")}
    vals = sorted(set().union(*[v for _s, _c, v, _t in facts.get("landed") or []])) if facts.get("landed") else []
    fixed_by, known = fix_between(repo, rec, (d.get("last_run") or {}).get("headSha"), last.get("headSha"),
                                  name_causes(None, vals), run_workflows())
    if fixed_by or not known:
        return []
    ob = {"key": d["key"], "workflow": d["workflow"], "jobs": d.get("jobs"), "job": d.get("job"), "kind": "test",
          "line": d.get("line"), "red_kind": "flake", "gate": False, "names": set(d.get("runs") or []),
          "run": d.get("last_run") or {}, "runs_list": []}
    flake = red_open(store, ob, None, store.clock())
    line = (f"RED FLAKE: {red_what(d)} passed on #{last.get('number')} and no commit between can have fixed it: a flake "
            "-- make it deterministic (never rerun to green); it stays a red until a fix lands")
    inbox_write("red", line, [line], [d.get("line") or ""], 1)
    return [line] + ([f"opened flake {flake['id']}"] if flake else [])


# -- interactive holders: the tick renews a claim from its worktree's activity; imported claim files lapse unless busy
def red_wt_mark(wt):
    """An interactive claim's progress mark: its worktree's HEAD, `git status` and the newest change's mtime."""
    if not wt or not os.path.isdir(wt) or not allowed_path(wt):
        return ""
    import hashlib
    head = git(wt, "rev-parse", "HEAD", check=False).stdout.strip()
    st = git(wt, "status", "--porcelain", check=False).stdout
    newest = 0.0
    for line in st.splitlines()[:200]:
        newest = max(newest, mtime(os.path.join(wt, line[3:].split(" -> ")[-1])))
    return f"{head[:12]}:{hashlib.sha1(st.encode()).hexdigest()[:10]}:{int(newest)}"


def red_renew_holders(store, t):
    """Each live interactive claim renewed by its worktree's activity (no activity: it lapses at its TTL); each sibling
    exactly as long as its run's fixer's own lease."""
    for row in store.list("red/"):
        if not lease_is_live(row, t):
            continue
        m = lease_meta(row)
        if m.get("kind") == "claim":
            mark = red_wt_mark(m.get("worktree"))
            ttl = m.get("ttl") or RED_CLAIM_TTL
        elif m.get("kind") == "sibling":  # held by its run's fixer: lives exactly as long as that fixer's own lease
            prim = store.get(m.get("primary") or "")
            if not lease_is_live(prim, t) or prim["holder"] != row["holder"]:
                store.revoke(row["name"], "its run's fixer ended")
                continue
            mark, ttl = f"with {prim['renewed_at']}", RED_FIXER_TTL
        else:
            continue
        if mark:
            store.renew(row["name"], row["epoch"], row["secret"], mark, ttl)


def fixer_keys(r):
    """The red keys a fixer record holds or held: its own red's, its lease's and its run's other reds' leases'."""
    names = [r.get("lease")] + [x.get("name") for x in r.get("extra_leases") or []]
    keys = {n.split("/", 1)[1] for n in names if n and n.startswith("red/")}
    return keys | ({r["key"]} if r.get("key") else set())


# -- the dispatcher (the phase-2 shape: offers and leases only)
def red_fixers_alive(store, t):
    """One row per live FIXER (a fixer holding several reds' leases counts once)."""
    out = {}
    for r in store.list("red/"):
        if lease_is_live(r, t) and lease_meta(r).get("kind") in ("fixer", "sibling", "owned"):
            out.setdefault(r["holder"], r)
    return list(out.values())


# ONE RED MODEL (docs/simplification-2026-10.md item 5; replaces the gate/other/overflow capacity classes, the borrow
# and the separate one-run and one-family rules). A red is (workflow, failing job, signature) -- `key` hashes the
# workflow and the signature (the normalized first error line; the job when none was read), `jobs` are every job
# failing that way. Every red belongs to ONE QUEUE FAMILY:
# its configured family (red-families.json: convergence, ...), else its run (`run:<workflow>#<number>`). The open
# fixer offers are ONE PRIORITY QUEUE (gate < fix < outside < flake, then a test failure first, then the oldest), and
# the dispatcher keeps EXACTLY ONE FIXER PER FAMILY: a family whose fixer is at work gets each new red under that
# fixer's lease (a sibling, told in RED-INBOX.md); a family with none gets one fixer, its other open offers taken with
# it, while fewer than RED_FIXERS fixers are alive in all. One family never holds two slots, so no class reserves are
# needed for fairness; an offer that waits RED_STARVE with no slot escalates as STARVED (then every cycle). Every wait
# names what it waits for and when.
RED_FIXERS = 5           # live fixers at once, every family together ($AGENTCTL_RED_FIXERS)
RED_STARVE = 45 * 60     # an offer waiting this long for a slot is STARVED ($AGENTCTL_RED_STARVE)


def red_queue_family(d):
    """The queue family of a red (its data, its picture, a fixer record): the configured family, else its run."""
    fam = red_family(d)
    if fam:
        return fam
    run = d.get("run") if d.get("run") is not None else d.get("last_run")
    num = run.get("number") if isinstance(run, dict) else run
    o_r = red_repo_of(d)
    return f"run:{d.get('workflow')}#{num}" if o_r == RED_PRIMARY else f"run:{o_r}:{d.get('workflow')}#{num}"


RED_MODEL = 2  # the store's red data model (meta red_model): 2 = the queue (item 5); red_migrate brings older data up


def red_identity(d):
    """A red's identity fields: its signature (the normalized first error line, None when none was read) and its
    configured family (None: its run is its queue family)."""
    return {"signature": norm_line(d.get("line")) or None, "family": red_family(d)}


def red_migrate(store):
    """IN-PLACE MIGRATION to RED_MODEL, once per store (meta red_model), run by the tick: every open red gets its
    identity fields (signature, family). The capacity classes' lease meta (`slot`) needs nothing: it is no longer read,
    and a fixer's lease ends with it. Idempotent; a red another writer moved meanwhile is done at the next tick.
    Returns how many reds it updated."""
    if int(store.meta_get("red_model", 1) or 1) >= RED_MODEL:
        return 0
    n, left = 0, 0
    for it in store.items("red"):
        if it["state"] in RED_TERMINAL:
            continue
        want = red_identity(it["data"])
        if all(k in it["data"] and it["data"][k] == v for k, v in want.items()):
            continue
        try:
            store.update_item("red", it["id"], it["version"], want)
            n += 1
        except StaleVersion:
            left += 1
    if not left:
        store.meta_set("red_model", RED_MODEL)
    return n


def red_cap():
    return int(env_num("AGENTCTL_RED_FIXERS", RED_FIXERS))


def red_capacity(store, waited, t, alive=None):
    """(True, None) when a fixer may start now (fewer than RED_FIXERS alive), else (False, why): what it waits for and
    WHEN, `STARVED: ...` past RED_STARVE."""
    alive = red_fixers_alive(store, t) if alive is None else alive
    n, cap = len(alive), red_cap()
    if n < cap:
        return True, None
    first = min(alive, key=lambda r: float(r.get("max_until") or r.get("expires_at") or 0)) if alive else None
    hm = lambda x: time.strftime("%H:%M", time.localtime(x))
    eta = f"; next slot when {first['holder']} ends by {hm(float(first.get('max_until') or first.get('expires_at')))}" \
        if first else ""
    starve = env_num("AGENTCTL_RED_STARVE", RED_STARVE)
    head = f"capacity {n}/{cap}, waiting {fmt_age(waited)}"
    if waited >= starve:
        return False, f"STARVED: {head}, past its {fmt_age(starve)} window{eta}"
    return False, head + eta


def red_family_holder(store, fam, t, active=None):
    """The live primary lease (a fixer's own, not a sibling) on a red of queue family fam, or None."""
    if not fam:
        return None
    active = active if active is not None else red_active(store)
    for row in store.list("red/"):
        if not lease_is_live(row, t) or lease_meta(row).get("kind") != "fixer":
            continue
        it = active.get(row["name"].split("/", 1)[1])
        if it and red_queue_family(it["data"]) == fam:
            return row
    return None  # a holder is ONLY a live lease


def red_dispatcher(store, o_r, repo, t):
    """THE QUEUE (see ONE RED MODEL above): the open fixer offers, highest priority first; per queue family exactly one
    fixer -- a new red of a family at work joins its fixer, a family with none is dispatched once (its other open
    offers taken with it) while a slot is free and dispatch can run. An offer that cannot be taken says why (its
    escalation names it)."""
    acts = []
    offs = store.offers("fixer")
    if not offs:
        return acts
    offs.sort(key=lambda o: (o["priority"], KIND_ORDER.get(o["picture"].get("kind"), 1), o["opened_at"]))
    blocker = cached_dispatch_blocker()
    gone = set()
    for off in offs:
        if off["id"] in gone:
            continue
        pic = off["picture"]
        fam = red_queue_family(pic)
        owned = family_owned(fam) and off["tier"] not in ("revert", "author", "lander", "blame")
        live = owner_alive(store, fam, t) if owned else None
        if owned and not live and (ending := owner_still_ending(store, fam, t)):
            # its last wrapper lost its heartbeat (orphaned, revoked) and is still ending: the next starts after it
            if ending != off.get("reason"):
                store.offer_reason(off["id"], ending)
            continue
        if owned and live:
            # THE FAMILY'S OWNER IS ALIVE: the red is its next turn -- its lease taken for the owner and handed over
            took = store.take(off["id"], owner_holder(fam), OWNER_TTL, OWNER_MAX,
                              meta={"kind": "owned", "family": fam, "owner": owner_lease(fam),
                                    "worktree": lease_meta(live).get("worktree"), "gate": bool(pic.get("gate"))})
            if took and "epoch" in took:
                owner_hand(fam, off["lease"], took)
                red_family_tell(live, pic, fam)
                acts.append(f"{pic.get('workflow')} #{(pic.get('run') or {}).get('number')}: red {pic['key']} handed "
                            f"to its family {fam}'s owner")
            continue
        holder = red_family_holder(store, fam, t) if off["tier"] not in ("revert", "lander", "blame") else None
        if holder and holder["name"] != off["lease"]:
            # ONE FIXER PER FAMILY: a new red of a family whose fixer is at work is that fixer's -- under its lease
            took = store.take(off["id"], holder["holder"], RED_FIXER_TTL, RED_FIXER_MAX,
                              meta={**lease_meta(holder), "kind": "sibling", "primary": holder["name"], "family": fam,
                                    "outcome": None, "notes": None})
            if took and "epoch" in took:
                red_family_tell(holder, pic, fam)
                acts.append(f"{pic.get('workflow')} #{(pic.get('run') or {}).get('number')}: red {pic['key']} joined "
                            f"its family {fam}'s fixer {holder['holder']}")
            continue
        item = store.item("red", off["item"].split(":", 1)[1]) or {"data": {}}
        after = float(item["data"].get("dispatch_after") or 0)
        # NOTHING STOPS A GATE FIX, EVER (the owner, 2026-10-31): a gate red's fixer starts past the cap, and no fixer
        # waits on laptop pressure (that holds only other work: gov_fits); a gate fixer still counts toward the cap
        free, cap_why = (True, None) if pic.get("gate") else red_capacity(store, t - float(off.get("opened_at") or t), t)
        if off["tier"] == "author":
            why = f"offered to its author ({str((pic.get('author') or {}).get('worktree') or '?').replace(HOME, '~')})"
        elif off["tier"] == "blame":
            why = blame_waiting(pic.get("blame"))
        elif after > t:
            last = next((str(h.get("outcome") or "") for h in reversed(item["data"].get("history") or [])
                         if str(h.get("outcome") or "").startswith("NOT STARTED")), "")
            last = re.sub(r"^(NOT STARTED: )+", "", last)
            why = (f"its last {item['data'].get('start_failures')} fixer(s) never got going; the next at "
                   f"{time.strftime('%H:%M', time.localtime(after))}" + (f"; launch error: {last[:240]}" if last else ""))
        elif blocker:
            why = blocker
        elif not free:
            why = cap_why
        else:
            why = None
        if why:
            if why != off.get("reason"):
                store.offer_reason(off["id"], why)
            continue
        # ANOTHER WATCHED REPO'S RED: its fixer works in that repo's clone on this machine (red-repos.json `clone`)
        po = pic.get("repo") or o_r
        prepo = repo if po == o_r else red_repo_clone(po)
        if po != o_r and not prepo:
            why = f"no clone of {po} on this machine (red-repos.json `clone`)"
            if why != off.get("reason"):
                store.offer_reason(off["id"], why)
            continue
        gap = claude_trust_gap(prepo)
        if gap:  # never a launch claude refuses: said on its offer (and the inbox, once), dispatched once trusted
            why = f"NOT DISPATCHABLE: {gap}"
            if why != off.get("reason"):
                store.offer_reason(off["id"], why)
                inbox_write("red", f"RED NOT DISPATCHABLE: {pic.get('workflow')} #{(pic.get('run') or {}).get('number')}: {gap}", [], [], 1)
            continue
        num = (pic.get("run") or {}).get("number")
        holder_id = owner_holder(fam) if owned else f"laptop:fixer:red-{pic['key']}-{int(pic.get('attempts') or 0) + 1}"
        meta = {"kind": "owned" if owned else "fixer", "gate": bool(pic.get("gate")), "family": fam,
                "tier": off["tier"], "run": num, "workflow": pic.get("workflow")}
        ttl, max_ = (OWNER_TTL, OWNER_MAX) if owned else (RED_FIXER_TTL, RED_FIXER_MAX)
        took = store.take(off["id"], holder_id, ttl, max_, meta=meta)
        if not took or "held" in took or "gone" in took:
            continue
        extra = []
        if off["tier"] not in ("revert", "resume", "lander"):
            for sib in offs:
                if sib["id"] != off["id"] and sib["id"] not in gone and sib["tier"] not in ("author", "revert", "resume",
                                                                                             "lander", "blame") \
                        and red_queue_family(sib["picture"]) == fam:
                    st_ = store.take(sib["id"], holder_id, ttl, max_, meta=meta)
                    if st_ and "epoch" in st_:
                        extra.append({"name": sib["lease"], "epoch": st_["epoch"], "secret": st_["secret"],
                                      "picture": sib["picture"]})
                        gone.add(sib["id"])
        ok, msg = (owner_spawn if owned else red_spawn)(store, po, prepo, *((fam,) if owned else ()), off, took, extra)
        if not ok:
            for x in [{"name": off["lease"], **took}] + extra:
                store.release(x["name"], x["epoch"], x["secret"], outcome=f"NOT STARTED: {str(msg)[:200]}")
        acts.append(msg)
    return acts


def red_spawn(store, o_r, repo, off, took, extra=()):
    """Start what the tier says, under the taken lease: the revert fallback, or a headless fixer (fresh worktree; the
    same session and worktree for `resume`)."""
    pic, tier = off["picture"], off["tier"]
    u = {"workflow": pic["workflow"], "job": pic.get("job"), "jobs": pic.get("jobs"), "kind": pic.get("kind") or "test",
         "line": pic.get("line"), "run": pic.get("run") or {}, "refuted": pic.get("refuted"), "predicts": pic.get("predicts"),
         "flake": pic.get("red_kind") == "flake", "diagnosis": pic.get("diagnosis"), "key": pic["key"],
         "names": set(pic.get("names") or []), "prior_work": pic.get("prior_work"), "old_note": pic.get("old_note"),
         "history": pic.get("history") or [], "repo": pic.get("repo"), "decision": pic.get("decision")}
    for x in extra:  # the run's other reds, under the same fixer
        u["jobs"] = sorted(set(u.get("jobs") or [u["job"]]) | set(x["picture"].get("jobs") or []))
    if tier == "lander":  # THE DECISION RULE's determined culprit: reverted, its lander revived (blame.py)
        return lander_spawn(store, o_r, repo, off, took, u)
    b = pic.get("blame") or {}
    if b.get("reland_due") and not b.get("reland"):  # its revert was refuted: the change relanded before a fixer starts
        rl = blame_reland(o_r, repo, u, b)
        it_ = store.item("red", off["item"].split(":", 1)[1])
        if it_:
            store.update_item("red", it_["id"], it_["version"], {"blame": dict(b, reland=rl)})
        inbox_write("red", f"RED RELAND: {red_what(pic)}: {rl['msg']}", [], [], 0 if rl.get("ok") else 1)
        b = dict(b, reland=rl)
    if b.get("culprit") is not None:
        u["blame_note"] = blame_note(b)
    if tier == "revert":
        ok, msg = red_revert(o_r, repo, u, pic.get("culprit"))
        store.note(off["lease"], took["epoch"], took["secret"], msg)
        it_id = off["item"].split(":", 1)[1]
        it = store.item("red", it_id)
        if it:
            store.update_item("red", it_id, it["version"], {"reverted": {"at": ts(), "culprit": pic.get("culprit"), "ok": ok}})
        return ok, f"RED REVERT: {pic['workflow']} #{u['run'].get('number')}: {msg}"
    last = (pic.get("history") or [{}])[-1]
    resume = last if tier == "resume" else None
    if tier == "resume":
        resume = dict(last, note=(f"YOUR FIX DID NOT FIX IT ({last.get('outcome')}): find out why, and fix it properly "
                                  f"(same rules; Fixes-Red: {u['run'].get('number')})."))
    effort = "max" if tier == "deep" else None
    if tier == "deep":
        u["diagnosis"] = red_investigation_brief(pic)
    return red_dispatch(o_r, repo, u, why=f"tier {tier}", resume=resume, effort=effort, lease=off["lease"], took=took,
                        extra=extra)


def red_investigation_brief(pic):
    verdicts = " | ".join(f"{h.get('at')}: {h.get('holder')}: {str(h.get('outcome'))[:160]}" for h in pic.get("history") or [])
    return (f"DEEP INVESTIGATION (effort max) after {pic.get('attempts')} attempts did not clear it. Failing: "
            f"`{pic.get('job')}`: {str(pic.get('line') or '')[:300]!r}. Refuted fixes: "
            f"{', '.join(x[:10] for x in pic.get('refuted') or []) or 'none'}. Attempts: {verdicts or 'none'}. Find the "
            "ROOT CAUSE they all missed and fix it in the code. A code red is never the owner's: no `red block` unless you "
            "PROVE the cause is outside the code and only the owner can act")


# -- dispatch: a headless Claude Code fixer in a fresh worktree
SECRET_ENV = re.compile(r"TOKEN|SECRET|PASSWORD|PASSWD|API_KEY|PRIVATE|CREDENTIAL|VAULT", re.I)


def sync_brief():
    """The movies agents' preamble a fixer reads first (~/bin/briefs/movies-agent-preamble.md, $AGENTCTL_BRIEF). The
    fixer's own rules are docs/fixer-brief.md, in its prompt (fixer_brief); nothing is copied from a scratchpad."""
    return os.environ.get("AGENTCTL_BRIEF", BRIEF)
