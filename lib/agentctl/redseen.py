# agentctl, part `redseen`: what is red now: one observation per key from the CI snapshot, stuck and unkicked runs.
# Runs inside agentctl's one namespace, after the parts before it in lib/agentctl/__init__.py's PARTS (it uses their names).

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
        if s.get("queued") or s.get("waits_on"):
            continue  # waiting its turn behind busy runners, or on the run it dispatched: not a stuck run
        r = s["run"]
        units.append({"key": red_key(s["name"], "stuck", "stuck run"), "workflow": s["name"], "job": "stuck",
                      "kind": "infra", "red_kind": "stuck-run", "gate": s["name"] in {g["name"] for g in snap.get("gate") or []},
                      "line": f"run #{r.get('number')} undecided for {fmt_age(s['age'])} (limit {fmt_age(s['limit'])}"
                              + (f", {s['limit_from']})" if s.get("limit_from") else ")"),
                      "run": r, "runs": [], "names": run_names(r)})
    return red_obs_of_units(units)


# THE STUCK LIMIT IS THE WORKFLOW'S OWN (the owner, 2026-11-06, deciding red 971723aec893: six false stuck reds in a
# week). Record scrape fixtures #165/#172 (daily, 32-65 min a run) and Identity drift explorer #1 (its first run) were
# judged against the 30-minute floor because the newest-200 run list held none of their finished runs. Now a workflow
# with fewer than STUCK_MIN_HISTORY finished runs in the list has its own last 30 finished runs read (history_of, only
# once one of its runs is past the floor); with fewer still, its jobs' timeout-minutes bound it (timeout_of). A run
# waiting on a run it dispatched (a workflow_dispatch run of another workflow created after its first job started,
# still undecided) is judged by that child: `waits_on`, no red -- the child is judged on its own. A run whose unfinished
# jobs all wait for runners whose every slot is busy is `queued`, not stuck, whatever ran before.
STUCK_MIN_HISTORY = 5
GITHUB_JOB_TIMEOUT = 360 * 60  # GitHub's own timeout of a job that sets none
UNSTARTED = ("queued", "waiting", "pending", "requested")


# A RUN THAT DID NOTHING IS NO SAMPLE (issues 20261101T101427-e15fe1, 20261108T205920-0e4015): Order independence #27
# took 31 s with every real job skipped, and Identity takes' first runs failed in ~3 min, so the p90 fell to the floor
# and healthy 40-min runs were stuck reds. A finished run shorter than STUCK_SAMPLE_MIN tells nothing of how long the
# workflow takes: it is left out of the p90, and the stuck line says how many runs the limit came from.
STUCK_SAMPLE_MIN = 5 * 60
# A SCHEDULED BOT WAITS ITS TURN (issue 20261108T135045-06bcab: Scala Steward #145, a daily run queued behind a
# CPU-saturated ci-runner -- checks first, by design -- was stuck at 1 h): a run its schedule started that has not
# started a job yet waits its turn behind checks, so its limit is at least STUCK_SCHEDULED; once a job runs, the
# workflow's own limit judges it.
STUCK_SCHEDULED = 6 * 3600
STUCK_SCHEDULED_EVENTS = ("schedule", "repository_dispatch")  # movies' scheduler dispatches the crons (repository_dispatch)


def stuck_durations(runs):
    """The durations of the finished runs that ran (STUCK_SAMPLE_MIN or longer)."""
    durs = []
    for r in runs:
        if r.get("status") == "completed" and r.get("createdAt") and r.get("updatedAt"):
            try:
                dur = parse_time(r["updatedAt"]).timestamp() - parse_time(r["createdAt"]).timestamp()
            except (ValueError, TypeError):
                continue
            if dur >= STUCK_SAMPLE_MIN:
                durs.append(dur)
    return durs


def stuck_limit(name, runs, history_of, timeout_of, how=None):
    """max(STUCK_FACTOR x the p90 of the workflow's finished runs, STUCK_FLOOR): from the list, else its own history;
    with too few of those, its jobs' timeout-minutes bound (when readable). how: a dict told where it came from."""
    durs = stuck_durations(runs)
    if len(durs) < STUCK_MIN_HISTORY and history_of:
        seen = {r.get("databaseId") for r in runs if r.get("status") == "completed"}
        durs += stuck_durations([r for r in history_of(name) or [] if r.get("databaseId") not in seen])
    limit = max(STUCK_FACTOR * (pct(durs, 90) if durs else 0), STUCK_FLOOR)
    src = f"from {len(durs)} runs" if durs else "the floor"
    if len(durs) < STUCK_MIN_HISTORY and timeout_of:
        bound = timeout_of(name)
        if bound and bound > limit:
            limit, src = bound, f"its jobs' timeouts ({len(durs)} runs)"
    if how is not None:
        how["src"] = src
    return limit


def stuck_child(lists, name, since):
    """'<workflow> #N' of an undecided workflow_dispatch run of another workflow created at or after `since` (the run
    its parent dispatched and waits on), or None."""
    for other, runs in lists.items():
        if other == name:
            continue
        for r in runs:
            if r.get("event") == "workflow_dispatch" and r.get("status") != "completed" and r.get("createdAt"):
                try:
                    if parse_time(r["createdAt"]).timestamp() >= since:
                        return f"{other} #{r.get('number')}"
                except (ValueError, TypeError):
                    continue
    return None


def stuck_runs(lists, now_=None, o_r=None, jobs_of=None, queue=None, history_of=None, timeout_of=None):
    """[{name, run, age, limit[, queued | waits_on]}]: per workflow, its undecided runs older than its stuck limit
    (stuck_limit). A run's age counts from its first job's start (Convergence bisect #2385, run 3568: an hour queued
    behind a measurement holding every `measure` slot was called a stuck red): a run whose unfinished jobs all wait,
    only on labels whose every runner is up with all slots busy, carries `queued`; one waiting on a run it dispatched,
    `waits_on`. Neither is a red."""
    t = now_ if now_ is not None else time.time()
    jobs_of = jobs_of or (lambda r: run_jobs(o_r, r))
    if history_of is None and o_r:
        history_of = lambda name: stuck_history(o_r, name)  # noqa: E731
    if timeout_of is None and o_r:
        timeout_of = lambda name: stuck_timeout(o_r, name)  # noqa: E731
    out = []
    for name, runs in lists.items():
        limit, how = None, {}  # read only once one of its runs is past the floor: the history and the file cost a read
        for r in runs:
            if r.get("status") != "completed" and r.get("createdAt"):
                try:
                    age = t - parse_time(r["createdAt"]).timestamp()
                except (ValueError, TypeError):
                    continue
                if age <= STUCK_FLOOR:
                    continue
                if limit is None:
                    limit = stuck_limit(name, runs, history_of, timeout_of, how)
                lim = limit
                if age <= lim:
                    continue
                jobs = jobs_of(r)
                starts = sorted(parse_time(j["startedAt"]).timestamp() for j in jobs or [] if j.get("startedAt"))
                queued, waits_on = None, None
                if starts:
                    age = t - starts[0]
                open_jobs = [j for j in jobs or [] if j.get("status") != "completed"]
                if jobs is not None and open_jobs and all(j.get("status") in UNSTARTED for j in open_jobs):
                    # nothing running: queued, not stuck, when every runner that could take it is busy
                    queued = queue_full(o_r, r.get("number"), runner_queue(t) if queue is None else queue)
                    if not starts and r.get("event") in STUCK_SCHEDULED_EVENTS:  # a scheduled bot not started yet
                        lim = max(lim, STUCK_SCHEDULED)
                if age > lim:
                    if starts and not queued:
                        waits_on = stuck_child(lists, name, starts[0])
                    s = {"name": name, "run": {k: r.get(k) for k in ("number", "databaseId", "headSha", "createdAt")},
                         "age": age, "limit": lim,
                         "limit_from": "its schedule" if lim != limit else how.get("src")}
                    out.append({**s, "queued": queued} if queued else {**s, "waits_on": waits_on} if waits_on else s)
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


UNKICKED_AFTER = 60 * 60  # a convergence-relevant main push with no convergence (or Identity takes) run on main by then is a red
# (suite, its paths, their matcher, who dispatches it): Main kicks the convergence when the paths change, and the
# convergence's preflight dispatches Identity takes (its takes rows, split out 2026-11-08) under the same condition
CONVERGENCE_PATHS = (".github/convergence-paths.txt", ".github/actions/changed-paths/matches.sh")
UNKICKED = {"pawelkrupinski/movies": [("Identity model convergence", *CONVERGENCE_PATHS, "Main's kick-convergence"),
                                      ("Identity takes", *CONVERGENCE_PATHS, "convergence's preflight")]}


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
    older than UNKICKED_AFTER, with no main run of the suite started since it, is an infra red until one runs. Each
    suite of UNKICKED is judged alone."""
    units = []
    for spec in UNKICKED.get(o_r) or []:
        u = unkicked_unit(repo, snap, now_, *spec)
        if u:
            units.append(u)
    return red_obs_of_units(units) if units else {}


def unkicked_unit(repo, snap, now_, name, paths_file, matcher, kicker):
    """unkicked_obs's red unit for one suite, or None."""
    if not repo or not snap:
        return None
    runs = next((g.get("runs") or [] for g in snap.get("gate") or [] if g["name"] == name), [])
    if not runs:
        return None
    head = git(repo, "rev-parse", f"refs/remotes/{remote()}/main", check=False).stdout.strip()
    ct = git(repo, "log", "-1", "--format=%ct", head, check=False).stdout.strip() if head else ""
    if not head or not ct.isdigit():
        return None
    t = now_ if now_ is not None else time.time()
    if t - float(ct) < env_num("AGENTCTL_UNKICKED_AFTER", UNKICKED_AFTER):
        return None
    newest = runs[0]
    try:
        if parse_time(newest.get("createdAt")).timestamp() >= float(ct):
            return None  # a run started since the push: kicked
    except (ValueError, TypeError, AttributeError):
        return None
    base = newest.get("headSha")
    if not base or not git_ok(repo, "cat-file", "-e", f"{base}^{{commit}}") \
            or convergence_relevant(repo, base, head, paths_file, matcher) is not True:
        return None
    u = {"key": red_key(name, "kick", "convergence not kicked"), "workflow": name, "job": "kick", "kind": "infra",
         "red_kind": "stuck-run", "gate": True,
         "line": f"no {name} run on main for {head[:10]} ({fmt_age(t - float(ct))} after it; the newest is "
                 f"#{newest.get('number')} on {str(base)[:10]}): {kicker} did not dispatch it",
         "run": {"number": f"unkicked-{head[:10]}", "databaseId": None, "headSha": head}, "runs": [], "names": set()}
    return u


def red_observe(o_r, repo, snap):
    """{key: observation} of everything red on main now: the gate, every other workflow (its newest decided run, audit
    #7), main's local checks, the branch runs that predict main red, and the stuck runs (audit #8). A report-only gate
    run's reds (gate_report_only) are outside the gate's."""
    rows = [(n, d, _gate_lists.get(n, []), not gate_report_only(o_r, n, d)[0]) for n, d, _nw, _s in gate_from_snap(snap)]
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
        jobs = {str(j) for j in (d.get("jobs_seen") or d.get("jobs") or [d.get("job")]) if j and j != "?"}
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
    # -- or its OWN run, rerun green (issue 20261112T164528-14afee: core #987's attempt 2 concluded success, same
    # databaseId, and the red was re-dispatched): cleared, and red_flake_check opens its flake when no commit between
    # can have fixed it (a rerun to green is a flake, never a fix)
    last = ((snap or {}).get("latest") or {}).get(d.get("workflow"))
    own = int((d.get("last_run") or {}).get("databaseId") or 0)
    return bool(last) and last.get("headBranch", "main") == "main" and (
        int(last.get("databaseId") or 0) > own
        or int(last.get("databaseId") or 0) == own and last.get("conclusion") == "success")
