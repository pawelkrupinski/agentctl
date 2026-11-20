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
  agentctl red unblock <run|key> --why "<what changed>"
                                                 it no longer waits on the owner: its block ends, or a BLOCKED ON OWNER
                                                 verdict no probe clears is struck (shown with its age until then)
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
# A NIGHTLY'S FIX WAITS FOR THE NIGHT (2026-11-13, Verify scrape recording #26): a landed fix of a red OUTSIDE the gate
# is proven only by its workflow's next run -- a nightly's is up to a day away, and no push brings it sooner (its verify
# dispatch may be refused). Such a fix holds its red, quietly, up to RED_FIX_NEXT_RUN: the next decided run containing
# it verifies (green) or refutes it (the same failure: back to a fixer, never to the owner).
RED_FIX_NEXT_RUN = 30 * 3600  # s an outside red's landed fix waits unproven for its workflow's next run


def red_fix_giveup(d):
    """How long red data d's fix may wait in fix-pending unproven: RED_FIX_GIVEUP; for a landed fix of a red outside
    the gate (its workflow's next run proves it) one RED_FIX_DECIDE past its verify deadline (red_verify_plan) when its
    workflow has a schedule, else RED_FIX_NEXT_RUN."""
    f = d.get("fix") or {}
    if red_waits_for_its_run(d):
        if f.get("verify_by"):
            since = float(f.get("verify_since") or f.get("at") or 0)
            return max(env_num("AGENTCTL_RED_FIX_GIVEUP", RED_FIX_GIVEUP),
                       float(f["verify_by"]) - since + env_num("AGENTCTL_RED_FIX_DECIDE", RED_FIX_DECIDE))
        return env_num("AGENTCTL_RED_FIX_NEXT_RUN", RED_FIX_NEXT_RUN)
    return env_num("AGENTCTL_RED_FIX_GIVEUP", RED_FIX_GIVEUP)


def red_waits_for_its_run(d):
    """Red data d is a red outside the gate whose fix landed: only its workflow's own next run can prove it."""
    f = d.get("fix") or {}
    return not d.get("gate") and d.get("red_kind") == "outside" and f.get("status") == "landed" and bool(f.get("sha"))


# A SCHEDULED WORKFLOW'S FIX WAITS FOR ITS SLOT (2026-11-19, Record scrape fixtures #198, 50bc15cd7369): its fix
# 123dd6524 landed at 04:23Z; the workflow runs nightly at 23:31Z (its `on.schedule` cron, dispatched by the movies
# scheduler, infra/nix/files/scheduled_dispatch.py, which reads the same crons), so no run could contain the fix before
# ~23:31Z -- yet a fixed RED_FIX_DECIDE timer showed it "fix-pending escalated" at 07:23Z, and the landing itself had
# raised its level. Now a landed fix of a red outside the gate whose workflow has a schedule is VERIFIED BY the first
# slot after the landing plus the workflow's p90 duration (its recent finished runs) plus RED_VERIFY_SLACK, and until
# then it rests quietly: "awaiting next scheduled run at HH:MMZ", no level, no inbox line. At that deadline a run
# containing the fix still queued or running holds it (re-deadlined, quiet); none is an escalation: the slot passed with
# no run verifying it, or the scheduler never dispatched it.
# A VERIFY DISPATCH IS PER WORKFLOW (the owner, 2026-11-19): Record scrape fixtures scrapes every cinema through paid
# proxies -- its 04:34Z verify dispatch (#200) was not cheap. A workflow WITH a schedule is dispatched to verify a fix
# only when the repo's ONE per-workflow policy registry, RED_WORKFLOWS_FILE on main, says `verify_dispatch` yes; every
# other scheduled workflow waits for its slot. A workflow with no schedule is dispatched as before (no other run will
# come). The same row's `replay` is the command a fixer runs before landing (FIX REPLAY, below).
RED_VERIFY_SLACK = 2 * 3600     # s past a slot's expected end before its fix is called unverified ($AGENTCTL_RED_VERIFY_SLACK)
RED_VERIFY_DURATION = 2 * 3600  # s a scheduled run is taken to last when none of its runs finished
RED_VERIFY_HOLD = 30 * 60       # s a run that will decide the fix, still undecided at the deadline, holds it again
RED_WORKFLOWS_FILE = ".github/red-workflows.tsv"  # workflow, verify_dispatch (yes|no), replay (a command or -), why
CRON_HORIZON_DAYS = 32
CRON_RANGES = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))


def cron_fields(cron):
    """A cron's five fields as sets of values (None for `*`), or None when it cannot be read."""
    parts = str(cron or "").split()
    if len(parts) != 5:
        return None
    out = []
    try:
        for text, (low, high) in zip(parts, CRON_RANGES):
            out.append(None if text == "*" else cron_values(text, low, high))
    except ValueError:
        return None
    if out[4] is not None:
        out[4] = {v % 7 for v in out[4]}  # 7 and 0 are both Sunday
    return out


def cron_values(text, low, high):
    vals = set()
    for item in text.split(","):
        rng, _, step = item.partition("/")
        if rng == "*":
            a, b = low, high
        elif "-" in rng:
            a, b = map(int, rng.split("-"))
        else:
            a = int(rng)
            b = high if step else a
        vals |= set(range(a, b + 1, int(step) if step else 1))
    return vals


def cron_hit(vals, v):
    return vals is None or v in vals


def cron_matches(fields, at):
    minutes, hours, days, months, weekdays = fields
    weekday = (at.weekday() + 1) % 7  # Python's Monday=0 -> cron's Sunday=0
    if days is not None and weekdays is not None:  # cron ORs a restricted day of month and day of week
        day = at.day in days or weekday in weekdays
    else:
        day = cron_hit(days, at.day) and cron_hit(weekdays, weekday)
    return cron_hit(minutes, at.minute) and cron_hit(hours, at.hour) and cron_hit(months, at.month) and day


def cron_next(crons, after):
    """The first whole minute (epoch, UTC) after `after` that any of the crons fires at; None when none does within
    CRON_HORIZON_DAYS or none can be read."""
    import datetime as _dt
    fields = [f for f in (cron_fields(c) for c in crons) if f]
    if not fields:
        return None
    at = _dt.datetime.fromtimestamp(int(float(after) // 60) * 60 + 60, _dt.timezone.utc)
    end = at + _dt.timedelta(days=CRON_HORIZON_DAYS)
    while at < end:
        if not any(cron_hit(f[1], at.hour) for f in fields):  # no cron fires in this hour: the next one
            at = at.replace(minute=0) + _dt.timedelta(hours=1)
            continue
        if any(cron_matches(f, at) for f in fields):
            return at.timestamp()
        at += _dt.timedelta(minutes=1)
    return None


def workflow_crons(text):
    """The `on.schedule` crons of a workflow file's text."""
    return [m.group(2) for m in re.finditer(r"^\s*-\s*cron:\s*(['\"])(.+?)\1", text or "", re.M)]


def red_workflow_policies(repo, ref=None):
    """{workflow: {verify_dispatch: bool, replay: command or None, why}} from RED_WORKFLOWS_FILE at ref (main)."""
    txt = git(repo, "show", f"{ref or remote() + '/main'}:{RED_WORKFLOWS_FILE}", check=False).stdout if repo else ""
    out = {}
    for line in (txt or "").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        cols = [c.strip() for c in line.split("\t")]
        cols += [""] * (4 - len(cols))
        out[cols[0]] = {"verify_dispatch": cols[1].lower() == "yes",
                        "replay": cols[2] if cols[2] not in ("", "-") else None, "why": cols[3]}
    return out


def red_workflows_on_main(repo, ref=None):
    """RED_WORKFLOWS_FILE exists at ref (main). Absent is not "no workflow has a replay": a repo whose red-repos.json
    entry says `red_workflows` refuses Fixes-Red landings while it is (fix_replay_refusal)."""
    return bool(repo) and git_ok(repo, "cat-file", "-e", f"{ref or remote() + '/main'}:{RED_WORKFLOWS_FILE}")


def red_workflow_policy(repo, workflow, ref=None):
    return red_workflow_policies(repo, ref).get(workflow) or {"verify_dispatch": False, "replay": None, "why": ""}


def verify_dispatchable(repo, workflow):
    """The registry says `workflow` may be dispatched to verify a fix."""
    return red_workflow_policy(repo, workflow)["verify_dispatch"]


_verify_runs = process_cache({})
VERIFY_RUNS_TTL = 5 * 60


def workflow_main_runs(o_r, wfile):
    """The newest 30 runs on main of workflow file wfile (read at most every VERIFY_RUNS_TTL); [] when unread."""
    at, runs = _verify_runs.get((o_r, wfile), (0.0, None))
    if runs is None or time.time() - at >= VERIFY_RUNS_TTL:
        try:
            runs = gh_json(o_r, "run", "list", "--workflow", wfile, "--branch", "main", "--limit", "30",
                           "--json", RUN_FIELDS) or []
        except (Exception, SystemExit) as e:
            err(f"red verify: could not list {wfile} ({str(e)[:120]})")
            runs = runs or []
        _verify_runs[(o_r, wfile)] = (time.time(), runs)
    return runs


def red_verify_plan(o_r, repo, workflow, after):
    """{slot, by, file} for a fix of a red of `workflow` landed at `after`: the first scheduled slot after the landing,
    and when its run should have decided (slot + the p90 of its finished runs + RED_VERIFY_SLACK). None when the
    workflow has no schedule or its file cannot be read."""
    path, txt = workflow_file_of(repo, f"{remote()}/main", workflow) if repo and workflow else (None, "")
    slot = cron_next(workflow_crons(txt), after) if path else None
    if slot is None:
        return None
    runs = [r for r in workflow_main_runs(o_r, os.path.basename(path)) if not is_cancelled(r)]
    duration = pct(stuck_durations(runs), 0.9) or RED_VERIFY_DURATION
    return {"slot": slot, "by": slot + duration + env_num("AGENTCTL_RED_VERIFY_SLACK", RED_VERIFY_SLACK),
            "file": os.path.basename(path)}


def red_verify_run_on_way(o_r, repo, f):
    """The undecided run on main of a scheduled fix's workflow that will decide it -- created since its slot, or one
    containing the fix -- or None."""
    for r in workflow_main_runs(o_r, f["verify_file"]) if f.get("verify_file") else []:
        if r.get("status") == "completed":
            continue
        try:
            since_slot = parse_time(r.get("createdAt")).timestamp() >= float(f.get("next_run_at") or 0) - 60
        except (ValueError, TypeError, AttributeError):
            since_slot = False
        if since_slot or (repo and r.get("headSha") and contains_local(repo, f.get("sha"), r["headSha"])):
            return r
    return None


def hhmmz(t):
    return time.strftime("%H:%MZ", time.gmtime(float(t)))
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


# -- fixes on their way: Fixes-Red changes in the queue, and Fixes-Red commits landed on main
FIX_OPEN = ("queued", "checking", "checked", "waiting", "joined", "pushing")


# THE FIXES ARE READ FROM MAIN AS IT IS (2026-11-12, Record scrape fixtures #181, red 58f3db9e8a3d): the reconciler's
# own clone fetched main only to check or push a change, so a fix pushed from elsewhere -- 548dfb3c76, `Fixes-Red: 181`,
# landed from the laptop -- was never in it: the red was re-offered after each LANDED and put on the owner. A head of
# main the snapshot names that the clone has not got is fetched once, before the fixes are read.
RED_MAIN_REFETCH = 300
_main_fetched = process_cache({})


def red_main_current(repo, snap):
    """Fetch main into repo when the snapshot's main runs name a head commit it lacks. Returns True when it fetched."""
    if not repo or not snap or not git_ok(repo, "rev-parse", "--verify", "-q", f"refs/remotes/{remote()}/main"):
        return False  # no clone, or one that never fetched main (nothing of main's to keep up to date)
    heads = {r.get("headSha") for g in snap.get("gate") or [] for r in (g.get("runs") or [])[:3]}
    heads |= {v.get("headSha") for v in (snap.get("latest") or {}).values() if isinstance(v, dict)}
    missing = frozenset(h for h in heads if h and not git_ok(repo, "cat-file", "-e", f"{h}^{{commit}}"))
    if not missing:
        return False
    at = _main_fetched.get((repo, missing))
    if at and time.time() - at < RED_MAIN_REFETCH:  # a head main never had (a run of a commit since gone): not every tick
        return False
    _main_fetched.clear()
    _main_fetched[(repo, missing)] = time.time()
    ok, why = fetch_main(repo)
    if not ok:
        err(f"red tick: main moved past the clone and could not be fetched ({why[-160:]}): fixes read from the old main")
    return True


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
# workflow (the job's steps, the reusable workflows and local actions it uses, the repository scripts they name, three
# levels down, and the self-tests a runner among them finds: WHAT A SELF-TEST READS, below) plus the failing test's own sources. A commit counts as that red's fix unless it PLAINLY touches none
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
FIX_REPO_PATH = re.compile(r"(?<![\w/.-])(?:\./)?((?:scripts|\.github|tools|bin|(?:android|ios)/scripts)/[\w./-]+)")
# PER RED, PER FAILING STEP (2026-11-15, Order independence #74): 09e482c6a (ios/KinowoUITests only, `Fixes-Red: 74`)
# fixed the run's ios red and was linked to its swift red too -- a value counted for the run when ANY of its reds read
# the commit, and every red of the run took it; the swift job reads ios/ (`swift test --package-path ios`), but its
# failing step was the script self-tests, which read ios/scripts alone. Now (a) a red takes a fix only when ITS failing
# job reads the commit (red_fix_reaches, red_fix_candidates), and (b) a job whose failing steps are known
# (red_failed_steps) reads those steps, plus the steps before them that set the environment up (an action, a setup /
# build / install step, a write to $GITHUB_ENV / $GITHUB_PATH / $GITHUB_OUTPUT) -- never its other legs. android/ or ios/
# is read WHOLE when the text builds it (a project or build file of it, a `--package-path` / `working-directory` /
# `cd` into it, a script file inside it); a directory under it named alone (run-script-tests' `ios/scripts` root) is
# read alone.
FIX_SETUP_STEP = re.compile(r"\$\{?GITHUB_(ENV|PATH|OUTPUT)\b|^\s*-?\s*uses:|\b(build|compile|assemble|install|set ?up|"
                            r"setup|generate|prepare|bootstrap)\b", re.I | re.M)
FIX_TOP_WHOLE = (r"(?:--package-path|--project-dir|working-directory:|\bcd|-C)\s+['\"]?{top}['\"]?(?![\w/.-])"
                 r"|(?<![\w-]){top}/[\w./-]*?(?:\.xcodeproj|\.xcworkspace|Package\.swift|build\.gradle(?:\.kts)?|"
                 r"settings\.gradle(?:\.kts)?|gradlew)(?![\w-])")
FIX_STEP_FAILED = ("failure", "timed_out")
# WHAT A SELF-TEST READS (2026-11-15, Tool self-tests #263: f872ecbe3, a .github/workflows/ change with `Fixes-Red: 263`,
# was FIX NOT COUNTED, though the red's step -- scripts/run-script-tests -- runs download_artifact_layout_test.py, which
# lints every .github/workflows/*.yml through the workflow_steps module it imports). A runner that finds its tests by a
# name glob ('*-test.sh', '*_test.py') reads every test it finds under the roots it names, and each test reads what it
# names, sources or imports (its siblings) -- the same three levels, plus the paths the last level names. What a found
# test reaches is DATA for android/ and ios/ (a test lays out a fake ios/Kinowo.xcodeproj): it adds the paths it names,
# never "builds the app whole", nor sbt / spec names.
FIX_TEST_GLOB = re.compile(r"""['"]\*([\w.-]*test[\w.-]*\.\w+)['"]""", re.I)
FIX_PY_IMPORT = re.compile(r"^\s*(?:from\s+([\w.]+)\s+import\b|import\s+([\w.]+(?:\s*,\s*[\w.]+)*))", re.M)
FIX_SOURCED = re.compile(r"(?:^|[\s;&|(])(?:\.|source)\s+['\"]?(?:\$\{?\w+\}?/)?([\w./-]+\.(?:sh|bash))"
                         r"|shellcheck\s+source=([\w./-]+)", re.M)


def fix_tree_files(repo, sha):
    r = git(repo, "ls-tree", "-r", "--name-only", sha, check=False)
    return (r.stdout if r.returncode == 0 else "").splitlines()


def fix_show(repo, sha, path):
    r = git(repo, "show", f"{sha}:{path}", check=False)
    return r.stdout if r.returncode == 0 else None


def fix_show_many(repo, sha, paths):
    """{path: text} of `paths` at commit sha, in one `git cat-file --batch` (a path missing there: left out)."""
    paths = list(dict.fromkeys(paths))
    if not paths:
        return {}
    try:
        r = subprocess.run(["git", "-C", repo, "cat-file", "--batch"], input="".join(f"{sha}:{p}\n" for p in paths)
                           .encode(), capture_output=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        return {}
    out, buf, i = {}, r.stdout, 0
    for p in paths:
        nl = buf.find(b"\n", i)
        if nl < 0:
            break
        head = buf[i:nl].split()
        i = nl + 1
        if len(head) == 3 and head[1] == b"blob":
            n = int(head[2])
            out[p] = buf[i:i + n].decode("utf-8", "replace")
            i += n + 1
        elif len(head) == 3:
            i += int(head[2]) + 1
    return out


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


def fix_job_steps(body):
    """(the job block without its steps, [(step name or None, step text)]) of a job block, by indentation."""
    pre, items, steps_ind, item_ind = [], [], None, None
    for l in body.splitlines():
        if steps_ind is None:
            m = re.match(r"^(\s*)steps:\s*$", l)
            if m:
                steps_ind = len(m.group(1))
            else:
                pre.append(l)
            continue
        ind = len(l) - len(l.lstrip())
        if l.strip() and not l.lstrip().startswith("#") and ind <= steps_ind:  # a job key after its steps
            steps_ind, item_ind = -1, None
            pre.append(l)
            continue
        m = re.match(r"^(\s*)-\s", l)
        if steps_ind >= 0 and m and (item_ind is None or len(m.group(1)) == item_ind):
            item_ind = len(m.group(1))
            items.append([l])
        elif items and steps_ind >= 0:
            items[-1].append(l)
        else:
            pre.append(l)
    out = []
    for ls in items:
        nm = None
        for i, l in enumerate(ls):  # the step's own `name:` (never a `with:` input's)
            m = re.match(r"^\s*(-\s+)?name:\s*['\"]?([^'\"\n]+?)['\"]?\s*$", l)
            if m and (i == 0 and m.group(1) or len(l) - len(l.lstrip()) == item_ind + 2):
                nm = m.group(2).strip()
                break
        out.append((nm, "\n".join(ls)))
    return "\n".join(pre), out


def fix_step_text(body, failing):
    """The part of job block `body` its failing steps read (PER FAILING STEP, above): the job without its steps, the
    failing steps, and the setup steps before them; the whole block when no step of it is known to have failed."""
    pre, steps = fix_job_steps(body)

    def failed(nm, txt):
        return any(nm == f or (nm is None and f.startswith("Run ") and f[4:].strip() and f[4:].strip() in txt)
                   for f in failing)
    idx = [i for i, (nm, txt) in enumerate(steps) if failed(nm, txt)]
    if not idx:
        return body
    keep = [txt for i, (_nm, txt) in enumerate(steps) if i in idx or (i < max(idx) and FIX_SETUP_STEP.search(txt))]
    return "\n".join([pre] + keep)


def red_failed_steps(d):
    """The steps that failed in red d's jobs on its newest run (by name): its `steps`, else read once through gh and
    kept (reds/runsteps/<run id>.json: a finished run's steps never change); [] when they cannot be read."""
    if d.get("steps"):
        return sorted(map(str, d["steps"]))
    rid = (d.get("last_run") or {}).get("databaseId")
    if not rid:
        return []
    path = reds_dir("runsteps", f"{rid}.json")
    rec = read_record(path)
    if rec is None:
        try:
            jobs = (gh_json(red_repo_of(d), "run", "view", str(rid), "--json", "jobs") or {}).get("jobs") or []
        except (Exception, SystemExit):
            return []
        if not jobs:
            return []
        rec = {str(j.get("name")): [str(s.get("name")) for s in j.get("steps") or []
                                    if s.get("conclusion") in FIX_STEP_FAILED and s.get("name")]
               for j in jobs if j.get("conclusion") in FIX_STEP_FAILED}
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            write_json(path, rec)
        except OSError:
            pass
    names = {str(j) for j in d.get("jobs") or [d.get("job")] if j}
    return sorted({s for j, ss in rec.items() if j in names for s in ss or []})


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
    failing = red_failed_steps(d) if picked else []
    picked = [fix_step_text(body, failing) for body in picked] if failing else picked
    texts.append("\n".join(picked) if picked else wf[1])  # its job's steps, or (unsure which) the whole workflow
    tree, dirs = set(files), {f.rsplit("/", 1)[0] for f in files if "/" in f}
    seen = set()

    def scan(src, t, data):
        """The paths text t (of file src; data: reached through a self-test a runner found) names, into reads; the
        files to read next, as (path, data)."""
        out = []
        for m in re.finditer(r"uses:\s*\./([\w./-]+)", t):
            p = m.group(1).rstrip("/")
            reads.add(p)
            if p not in seen:
                seen.add(p)
                out.append((p if FIX_WORKFLOW_FILE.match(p) else
                            next((p + a for a in ("/action.yml", "/action.yaml") if p + a in tree), None), data))
        named = [m.group(1).rstrip("/.") for m in FIX_REPO_PATH.finditer(t)]
        base = src.rsplit("/", 1)[0] if src and "/" in src else ""
        for m in FIX_SOURCED.finditer(t):  # a sourced or imported sibling: the file beside its user
            for n in filter(None, (m.group(1), m.group(2))):
                named += [c for c in (n, f"{base}/{n}") if c in tree]
        if src and src.endswith(".py"):
            for m in FIX_PY_IMPORT.finditer(t):
                for n in re.split(r"\s*,\s*", m.group(1) or m.group(2)):
                    c = f"{base}/{n.split()[0].replace('.', '/')}.py" if base else f"{n.split()[0].replace('.', '/')}.py"
                    if c in tree:
                        named.append(c)
        for m in FIX_TEST_GLOB.finditer(t):  # a self-test runner: every test it finds (under the roots it names)
            roots = {w.strip("'\"").removeprefix("./").rstrip("/") for w in re.findall(r"[\w./'\"-]+", t)} & dirs
            for f in files:
                if f.endswith(m.group(1)) and (not roots or any(f.startswith(r + "/") for r in roots)):
                    reads.add(f)
                    if f not in seen:
                        seen.add(f)
                        out.append((f, True))
        for p in named:
            reads.add(p)
            if p in seen or p not in tree or FIX_WORKFLOW_FILE.match(p):
                continue  # another workflow named in a script is its DATA (a lint's input): read, never run here
            seen.add(p)
            out.append((p, data))
        return out

    todo = [(None, texts[0], False)]
    for _depth in range(3):
        want_next = [x for src, t, data in todo for x in scan(src, t, data)]
        bodies = fix_show_many(repo, sha, [p for p, _ in want_next if p])
        todo = [(p, bodies[p], data) for p, data in want_next if bodies.get(p) and len(bodies[p]) < 400_000]
        texts += [t for _p, t, data in todo if not data]  # a found test's reach adds paths alone (above)
    for src, t, data in todo:  # the last level read: the paths it names are read too (never followed further)
        scan(src, t, data)
    text = "\n".join(texts)
    sbt = bool(FIX_SBT.search(text))
    return {"files": reads, "workflow": wf[0], "text": text, "sbt": sbt, "broad": sbt and bool(FIX_BROAD_TEST.search(text)),
            "named": set(re.findall(r"\b[A-Z]\w+\b", text + " " + str(d.get("line") or "") + " " + " ".join(names))),
            "tops": {top: fix_top_reads(top, text, texts[0], files) for top in FIX_UNREAD_TOP}}


def fix_top_reads(top, text, steps, files):
    """What a job reads of the app directory `top` (android/, ios/), from `text` (its steps and all they name) and
    `steps` (the job's own step text): True for all of it -- it builds it (a project or build file, a `--package-path` /
    `working-directory` / `cd` into it) or one of its steps runs a script file inside it, which may -- else the set of
    the paths under it named (a directory: its subtree; a file a deeper script names: that file, its own text read)."""
    if re.search(FIX_TOP_WHOLE.format(top=re.escape(top)), text):
        return True
    tree = set(files)
    named = re.compile(rf"(?<![\w.]){re.escape(top)}/([\w./-]*[\w-])")
    if any(f"{top}/{m.group(1)}" in tree for m in named.finditer(steps)):
        return True
    return {f"{top}/{m.group(1)}".rstrip("/") for m in named.finditer(text)}


def fix_path_unread(p, rd):
    """True when the job plainly does not read path p."""
    if p in rd["files"] or any(p.startswith(f.rstrip("/") + "/") for f in rd["files"]):
        return False
    if FIX_WORKFLOW_FILE.match(p):
        return True
    if FIX_UNREAD_DOC.search(p) and p not in rd["text"]:
        return True
    top = p.split("/", 1)[0]
    if top in FIX_UNREAD_TOP:
        tr = rd["tops"][top]
        if tr is not True and not any(p == x or p.startswith(x + "/") for x in tr):
            return True
    m = FIX_SPEC_FILE.search(p)
    if m and not rd["broad"] and m.group(3) not in rd["named"]:
        return True
    return False


FIX_RULES = 3  # the reading rules' version (2: per red, per failing step; 3: what a self-test reads): a verdict cached under another is read again


def fix_changed(repo, sha):
    r = git(repo, "diff-tree", "--no-commit-id", "--name-only", "-r", "--root", "-m", sha, check=False)
    return [p for p in (r.stdout if r.returncode == 0 else "").splitlines() if p]


def fix_red_verdict(repo, sha, d, changed):
    """{ok, why?}: does commit sha (its paths `changed`) touch what red d's failing job reads? Unsure counts."""
    if d.get("kind") != "test":
        return {"ok": True, "why": "not a test failure"}
    try:
        rd = red_job_reads(repo, sha, d)
    except (Exception, SystemExit):
        rd = None
    if rd is None or not changed or not all(fix_path_unread(p, rd) for p in changed):
        return {"ok": True}
    return {"ok": False, "why": (f"it touches nothing {red_what(d)} reads "
                                 f"({', '.join(changed[:4])}{' ...' if len(changed) > 4 else ''})")}


def fix_cache(sha):
    """(path, the verdicts cached for commit sha under the current FIX_RULES)."""
    path = reds_dir("fixcheck", f"{sha}.json")
    cache = read_record(path) or {}
    return path, {k: v for k, v in cache.items() if isinstance(v, dict) and v.get("rules") == FIX_RULES}


def fix_cache_put(path, cache, fresh):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        write_json(path, {**cache, **{k: {**v, "rules": FIX_RULES} for k, v in fresh.items()}})
    except OSError:
        pass


def fix_reaches_red(repo, sha, d):
    """Commit sha is a fix THIS red can take: it touches what ITS failing job reads (fix_red_verdict), cached per sha
    and red key. A run's `Fixes-Red:` value counts for the run when any of its reds reads the commit (fix_check); each
    red of the run takes it only when it reads it itself (PER RED, PER FAILING STEP)."""
    if not repo or not sha or not d.get("key"):
        return True
    path, cache = fix_cache(sha)
    k = f"red:{d['key']}"
    if k not in cache:
        if not git_ok(repo, "cat-file", "-e", f"{sha}^{{commit}}"):
            return True  # a commit this clone lacks: unsure counts (and is not cached)
        verdict = fix_red_verdict(repo, sha, d, fix_changed(repo, sha))
        fix_cache_put(path, cache, {k: verdict})  # kept for the next tick; this one's answer is the verdict itself
        return bool(verdict.get("ok"))
    return bool(cache[k].get("ok"))


def fix_check(repo, sha, values, store=None):
    """(the values that count as fixes, {value: why not} for those that do not): each `Fixes-Red:` value of commit sha
    against the reds it names (see above): a value counts when any red it names reads the commit. Cached per sha; a
    value naming no known red counts (unsure) and is not cached, so a red opened later is still checked."""
    values = {str(v).strip().lstrip("#") for v in values if str(v).strip()}
    if not values or not repo:
        return values, {}
    path, cache = fix_cache(sha)
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
        if not its or v == "local":
            ok.add(v)  # unsure: the red unknown (not cached), or local
            if its:
                fresh[v] = {"ok": True, "why": "local"}
            continue
        if changed is None:
            changed = fix_changed(repo, sha)
        verdict = None
        for it in its:  # each red's own verdict kept too: the red takes the fix only on its own (fix_reaches_red)
            rv = fix_red_verdict(repo, sha, it["data"], changed)
            fresh[f"red:{it['data']['key']}"] = rv
            if verdict is None or not verdict["ok"]:
                verdict = rv
        fresh[v] = {"ok": True} if verdict["ok"] else verdict
        if verdict["ok"]:
            ok.add(v)
        else:
            bad[v] = verdict["why"]
    if fresh:
        fix_cache_put(path, cache, fresh)
        for v, c in fresh.items():
            if not v.startswith("red:") and not c.get("ok"):
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
    vals = trailer_values(v.stdout if v.returncode == 0 else "")
    return fix_check(repo, sha, vals, store)[0] if vals else set()


_trailer_fixes = process_cache({})


def entry_fixes(repo, e):
    """The runs a queued change says it fixes: its `fixes` (recorded at submission from its Fixes-Red trailers), else
    read again from its commits' trailers (cached per commit) -- an entry that lost the field still links its red."""
    if e.get("fixes"):
        return sorted(fixes_named(e))
    out = []
    for c in e.get("commits") or []:
        if c not in _trailer_fixes:
            r = git(repo, "log", "-1", "--format=%(trailers:key=Fixes-Red,valueonly,separator=%x2C)", c, check=False)
            vals = trailer_values(r.stdout) if r.returncode == 0 else set()
            _trailer_fixes[c] = sorted(fix_check(repo, c, vals)[0]) if vals else []  # only those that count
        out += _trailer_fixes[c]
    return out


def red_fix_facts(repo, store=None):
    """What links a red to its fix: the queue's Fixes-Red changes, the Fixes-Red commits on main, and every run any
    spell of each red key was seen in (key_runs: a red whose own item lost a run -- a duplicate spell, an import --
    still meets the change that names that run)."""
    entries, untrailered = [], []
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
            elif not fx and e.get("status") in FIX_OPEN:  # no Fixes-Red: linked only by the files it edits
                untrailered.append(e)
    except (Exception, SystemExit):
        pass
    try:
        landed = landed_fix_commits(repo)
    except (Exception, SystemExit):
        landed = []
    key_runs, key_aside = {}, {}
    try:
        for it in (store.items("red") if store is not None else []):
            k = it["data"].get("key") or it["id"]
            key_runs.setdefault(k, set()).update(map(str, it["data"].get("runs") or []))
            key_aside.setdefault(k, set()).update(set(it["data"].get("refuted") or [])
                                                  | set(it["data"].get("unverified") or []))
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
    landed_at = {}
    for e in entries:
        sha = e.get("landed_sha")
        if sha and not e.get("landed_at") and repo:
            r = git(repo, "log", "-1", "--format=%ct", sha, check=False)
            if r.returncode == 0 and r.stdout.strip().isdigit():
                landed_at[sha] = float(r.stdout.strip())
    return {"entries": entries, "untrailered": untrailered, "landed": landed, "key_runs": {k: sorted(v) for k, v in key_runs.items()},
            "key_aside": {k: sorted(v) for k, v in key_aside.items() if v}, "landed_at": landed_at,
            "patches": patches, "repo": repo}


def red_names(d):
    return set(d.get("runs") or []) | {d.get("key")} | ({"local"} if d.get("workflow") == LOCAL_FLOW else set())


def red_refuted(d):
    """The red's refuted fixes, each once, in the order they were refuted. A list that took one sha many times (784badc4406e.3,
    2026-11-11: 2 shas stored 593 times) counts its distinct fixes, never its entries."""
    return list(dict.fromkeys(d.get("refuted") or []))


def red_set_aside(d, facts):
    """(shas, patch-ids) of the fixes this red -- or any spell of its key (facts' key_aside) -- refuted or set aside
    unverified: a candidate fix is one of them when its sha (or a prefix) is, or its CONTENT is -- the same patch-id
    under another sha (a rebase, a cherry-pick)."""
    shas = set(d.get("refuted") or []) | set(d.get("unverified") or []) \
        | set((facts.get("key_aside") or {}).get(d.get("key")) or [])
    patches = facts.get("patches") or {}
    return shas, {patches[x] for x in shas if patches.get(x)}


def red_fix_is_set_aside(sha, aside, facts):
    """A candidate fix sha is one the red set aside: by sha, or by CONTENT -- its patch-id read here when the facts did
    not read it (a sha the store supplied: a change's landed tip). 2026-11-12, b059f05c70fa: 19482f5c08 set aside
    unverified, its pre-rebase copy b6d3d8235 (the same patch, a change item's tip) parked the red again for 6 h."""
    shas, pids = aside
    if sha in shas or any(sha.startswith(x) for x in shas):
        return True
    if not pids:
        return False
    patches = facts.setdefault("patches", {})
    if sha not in patches and facts.get("repo"):
        try:
            patches[sha] = patch_id(facts["repo"], sha)
        except (Exception, SystemExit):
            patches[sha] = None
    return patches.get(sha) in pids


RED_LANDED_VERDICT = re.compile(r"^LANDED ([0-9a-f]{7,40})\b")


def red_fix_candidates(d, facts):
    """Every fix that may be on its way for red data d, best first: the queue's changes naming one of its runs (the
    newest first), the `Fixes-Red:` commits landed on main since it opened (the newest first), then its own fixers'
    `LANDED <sha>` verdicts since it opened (the newest first). Unjudged: red_fix_of drops the ones set aside."""
    names = red_names(d) | set((facts.get("key_runs") or {}).get(d.get("key")) or [])
    since = float(d.get("opened_at") or 0) - 3600
    repo = facts.get("repo")

    def its_own(named, shas):  # PER RED: named by its key, or a fix its own failing job reads (fix_reaches_red)
        return d.get("key") in named or any(fix_reaches_red(repo, x, d) for x in shas if x) or not any(shas)
    for e in sorted(facts.get("entries") or [], key=lambda e: e.get("created") or "", reverse=True):
        named = fixes_named(e) & names
        if not named or not its_own(named, list(e.get("commits") or []) + [e.get("landed_sha")]):
            continue
        if e.get("status") in FIX_OPEN:
            yield {"entry": e["id"], "status": e["status"], "author": {"worktree": e.get("worktree"),
                                                                       "session": e.get("session")}}
        elif e.get("status") == "landed" and e.get("landed_sha"):
            at = parse_time(e.get("landed_at") or e.get("updated") or e.get("created") or ts()).timestamp()
            if not e.get("landed_at") and (facts.get("landed_at") or {}).get(e["landed_sha"]) is not None:
                at = float(facts["landed_at"][e["landed_sha"]])  # its commit's time: a file's `updated` moves on
            if at >= since:  # landed before this spell opened: the run that opened it already outlived it
                yield {"entry": e["id"], "status": "landed", "sha": e["landed_sha"], "at": at}
    for e in [] if d.get("gate") else facts.get("untrailered") or []:  # A QUEUED CHANGE EDITING THE RED'S OWN
        # WORKFLOW FILE (issue 20261106T102105-327f4c: family-shards fixed exactly Identity family explorer #2's shard
        # timeouts in its workflow, with no trailer, while fixers were sent whose only fix would conflict with it).
        # NEVER A GATE RED'S, NOR BY A SCRIPT ALONE (2026-11-20, Main #2716 and Order independence #89): metrics-packages,
        # no trailer, edited scripts/ci/coupling_ratchet.py -- read by both failing jobs, as by most -- and was taken
        # for both reds' fix "landing on red (Fixes-Red)"; Main's own fixer was stopped for it while it waited on that
        # very gate. A gate red's fix carries `Fixes-Red:` (nothing else lands while it is red); untrailered, only the
        # red's own workflow file links a change to an outside red.
        if red_workflow_file_touched(repo, d, e.get("commits") or []):
            yield {"entry": e["id"], "status": e["status"], "linked": "its workflow's own files",
                   "author": {"worktree": e.get("worktree"), "session": e.get("session")}}
    for sha, ct, vals, _s in facts.get("landed") or []:
        if vals & names and ct >= since and its_own(vals & names, [sha]):
            yield {"entry": None, "status": "landed", "sha": sha, "at": ct}
    for h in reversed(d.get("history") or []):
        m = RED_LANDED_VERDICT.match(str(h.get("outcome") or ""))
        try:
            at = parse_time(h.get("at")).timestamp() if m and h.get("at") else None
        except (ValueError, TypeError):
            at = None
        if m and at is not None and at >= since and its_own(set(), [m.group(1)]):  # an owner's turn held every red
            yield {"entry": None, "status": "landed", "sha": m.group(1), "at": at, "via": "its fixer's LANDED"}


def red_run_on_way(d, snap, repo):
    """(run, sha): an undecided run on main of red d's workflow, newer than its failing run, containing a commit since
    that run's head that reaches its failing job (fix_reaches_red) -- a fix landed with no Fixes-Red, which that run
    will prove or refute. None when there is none (or the clone cannot tell)."""
    own = d.get("last_run") or {}
    base = own.get("headSha")
    if not repo or not base or not snap:
        return None
    runs = next((g.get("runs") or [] for g in (snap.get("gate") or []) + (snap.get("outside") or [])
                 if g.get("name") == d.get("workflow")), [])
    for r in runs:
        if r.get("status") == "completed" or r.get("headBranch", "main") != "main" or not r.get("headSha") \
                or int(r.get("databaseId") or 0) <= int(own.get("databaseId") or 0) or r["headSha"] == base:
            continue
        got = git(repo, "rev-list", "--max-count=50", f"{base}..{r['headSha']}", check=False)
        for sha in (got.stdout.split() if got.returncode == 0 else []):
            if fix_reaches_red(repo, sha, d):
                return r, sha
    return None


_ci_reads = process_cache({})


def red_workflow_file_touched(repo, d, commits):
    """Red d's own workflow file (red_job_reads, read in main's tree), when these commits edit it; empty when they do
    not or when that cannot be read. The scripts its job runs are no link: shared tooling is read by most jobs."""
    if not repo or not commits:
        return set()
    sha = git(repo, "rev-parse", "-q", "--verify", f"refs/remotes/{remote()}/main", check=False).stdout.strip() or \
        (d.get("last_run") or {}).get("headSha")
    if not sha:
        return set()
    k = (d.get("key"), sha)
    if k not in _ci_reads:
        try:
            rd = red_job_reads(repo, sha, d)
        except (Exception, SystemExit):
            rd = None
        _ci_reads[k] = {rd["workflow"]} if rd and rd.get("workflow") else set()
    reads = _ci_reads[k]
    return {p for c in commits for p in fix_changed(repo, c)} & reads if reads else set()


_landed_fetched = process_cache({})


def red_sha_on_main(repo, sha):
    """sha is on <remote>/main in clone repo -- main fetched (at most once per RED_MAIN_REFETCH for that sha) when the
    clone does not show it yet: a fixer's LANDED comes seconds after its push. Never in a clone that tracks no main."""
    ref = f"refs/remotes/{remote()}/main"
    if not repo or not sha or not git_ok(repo, "rev-parse", "--verify", "-q", ref):
        return False
    if git_ok(repo, "merge-base", "--is-ancestor", sha, ref):
        return True
    if time.time() - _landed_fetched.get((repo, sha), 0) < RED_MAIN_REFETCH:
        return False
    _landed_fetched[(repo, sha)] = time.time()
    return fetch_main(repo)[0] and git_ok(repo, "merge-base", "--is-ancestor", sha, ref)


# A REPO WITH NO CLONE HERE IS ASKED (issue 20261102T085329-930a4a: ci-runner holds no clone of bitcashier/core, so
# red_sha_on_main read False for every fixer's LANDED and the red was offered again after each -- 12 leases, 11
# attempts): its forge's compare of the sha with main says whether main contains it.
_api_on_main = process_cache({})


def red_sha_on_main_api(o_r, sha):
    """main of o_r contains sha, by the forge's compare (asked again at most once per RED_MAIN_REFETCH while it does
    not); False when it cannot tell."""
    if not o_r or not sha:
        return False
    got = _api_on_main.get((o_r, sha))
    if got and (got[0] or time.time() - got[1] < RED_MAIN_REFETCH):
        return got[0]
    try:
        c = gh_json(o_r, "api", f"repos/{o_r}/compare/{sha}...main") or {}
        on = c.get("status") in ("ahead", "identical")
    except (Exception, SystemExit):
        on = False
    _api_on_main[(o_r, sha)] = (on, time.time())
    return on


# ONE SET-ASIDE FIX HIDES NO OTHER (2026-11-13, Verify scrape recording #26, 784badc4406e.4): the queue entry of a refuted
# fix whose file still read `waiting` (b6d3d8235, naming #23, a run of the same red key) was THE candidate red_fix_of
# returned; the store turned it into its landed, set-aside sha and red_fix_current dropped it -- and with it 8d06b1a5ab,
# landed with `Fixes-Red: 26`, which was never looked at. The red was re-offered and its fixer put "hold it as fixed by
# 8d06b1a5ab" on the owner. Every candidate is judged in turn (corrected by the store when one is given); the first one
# still standing is the fix.
# A LANDED FIX IS A COMMIT ON MAIN (2026-11-16, Order independence #78, b3f51c5166b0.2): "fix landed in b6d3d8235,
# awaiting a run containing it" -- b6d3d8235 was a change item's `tip`, the pre-rebase commit of its check tree; the
# landing re-cut it as 19482f5c08 (Verify scrape recording #23's Filmweb fix, 2026-11-11), so no main ever held it. The
# change's entry FILE still read `joined` and also named "40" (a run of an earlier spell of this key), so five days
# later the new spell took it as a fix on its way, and the store turned it into the landed phantom. Now a landed fix's
# sha is resolved against main (red_main_equivalent): kept when main contains it, followed to the main commit carrying
# its patch (a re-cut on landing), and no fix at all when main carries neither; and a fix that landed before this spell
# opened is no fix for it (the run that opened it already contained it), however its entry reads. A red already
# fix-pending on such a sha is corrected the same way (red_fix_pending).
_main_equivalents = process_cache({})


def red_main_equivalent(repo, sha):
    """The commit of <remote>/main that carries landed fix sha: sha itself when main contains it, else the main commit
    with its patch (or content twin); None when main carries neither -- it never landed. sha as given when the clone
    cannot tell (no repo or main ref, git unsure)."""
    ref = f"refs/remotes/{remote()}/main"
    if not repo or not sha or not git_ok(repo, "rev-parse", "--verify", "-q", ref):
        return sha
    if red_sha_on_main(repo, sha):
        return sha
    if not git_ok(repo, "cat-file", "-e", f"{sha}^{{commit}}"):
        return None  # main fetched (red_sha_on_main) and still no such commit: it is on no main
    key = (repo, sha, git(repo, "rev-parse", ref, check=False).stdout.strip())
    if key not in _main_equivalents:
        commits, left = cherry_landed(repo, ref, sha, f"{sha}~1")
        if commits is None:
            _main_equivalents[key] = sha  # git cannot tell (a root commit): unsure counts
        elif left:
            _main_equivalents[key] = None
        else:
            try:
                _main_equivalents[key] = equivalent_on(repo, sha, ref) or sha
            except (Exception, SystemExit):
                _main_equivalents[key] = sha
    return _main_equivalents[key]


def red_fix_on_main(fix, repo):
    """A landed fix with its sha on main (red_main_equivalent: `recut` names the sha it was given), None when main never
    got it; any other fix as given."""
    if not fix or fix.get("status") != "landed" or not fix.get("sha"):
        return fix
    on = red_main_equivalent(repo, fix["sha"])
    if on is None:
        return None
    return fix if on == fix["sha"] else {**fix, "sha": on, "recut": fix["sha"]}


def red_fix_of(it, facts, store=None):
    """The fix on its way for this red, or None: {entry, status, sha?, at?}. A refuted or unverified fix -- by sha or
    by content -- counts no more, and is never the reason a later fix is not seen. store: each candidate corrected by
    its change's store item first (red_fix_fresh). A landed fix is a commit on main (red_fix_on_main) that landed
    since this spell opened."""
    d = it["data"]
    aside = red_set_aside(d, facts)
    since = float(d.get("opened_at") or 0) - 3600
    for fix in red_fix_candidates(d, facts):
        if fix.get("via") and not (red_sha_on_main(facts.get("repo"), fix["sha"]) if facts.get("repo") else
                                   red_sha_on_main_api(d.get("repo"), fix["sha"])):
            continue  # a fixer's LANDED counts once its commit is on main (a verdict alone proves nothing)
        if store is not None:
            fix = red_fix_fresh(store, fix)
        fix = red_fix_on_main(fix, facts.get("repo"))
        if fix and fix.get("status") == "landed" and fix.get("at") is not None and float(fix["at"]) < since:
            continue  # landed before this spell opened: the run that opened it already outlived it
        if fix and not (fix.get("sha") and red_fix_is_set_aside(fix["sha"], aside, facts)):
            return fix
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


# A FIX'S PROGRESS IS NO ESCALATION (2026-11-19, Record scrape fixtures #198): "fix ... landed (123dd6524e): verifying"
# is a fix-pending self-move, and the store raises a self-move's level unless its event says `info` -- so the landing
# of its fix showed the red "escalated" on the owner's board. A fix-pending self-move is quiet unless it says
# severity="escalate" (an unverified fix past its deadline, an owner push still waiting).
def red_move(store, it, to, text, data=None, deadline_s=None, level=None, **ev):
    if to == "fix-pending" and it.get("state") == "fix-pending":
        ev.setdefault("severity", "info")
    kw = {"level": level} if level is not None else {}  # only when said: an older store server takes no `level`
    v = store.transition("red", it["id"], to, it["version"], data=data, event=red_event(it, text, **ev),
                         deadline_s=deadline_s, **kw)
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
# ($AGENTCTL_RED_FAMILIES names another file). `"outside_gate": true` matches every red of a workflow the gate does
# not read (red_outside_gate; the owner, 2026-11-07: every non-gating red to ONE agent).
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
    for fam in red_families():  # the first family that matches: its workflows, jobs or outside_gate (if it names any)
        named = bool(fam.get("workflows") or fam["_jobs"] or fam.get("outside_gate"))  # AND its kinds
        by_name = d.get("workflow") in (fam.get("workflows") or []) or any(rx.search(j) for rx in fam["_jobs"] for j in jobs) \
            or bool(fam.get("outside_gate") and red_outside_gate(d))
        by_kind = not fam.get("kinds") or d.get("kind") in fam["kinds"]
        if (by_name or not named) and by_kind and (named or fam.get("kinds")):
            return fam["name"]
    return None


def red_outside_gate(d):
    """A red of a workflow THE GATE does not read (the owner, 2026-11-07: "move all these to single agent"): the repo's
    gate is red_gate_flows (movies' gating checks registry: Main, Identity model convergence, Identity takes -- the set ci-why's "Outside the gate"
    leaves out); the local checks are the landing gate's own, never outside it. No workflow named: cannot tell, no."""
    wf = d.get("workflow")
    return bool(wf) and wf != LOCAL_FLOW and wf not in {n for n, _f in red_gate_flows(red_repo_of(d))}


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
                f"{(pic.get('run') or {}).get('number')}`. Show progress on it within "
                f"{fmt_age(env_num('AGENTCTL_RED_JOIN_IDLE', RED_JOIN_IDLE))} (a commit carrying that trailer, or "
                f"`agentctl red claim {(pic.get('run') or {}).get('number')} --note \"<what>\"`), or it goes to a fixer "
                f"of its own.\n")
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
    # THE JOBS FAILING NOW (issue 20261104T040527-1b4212: convergence #834's prompt named the German leg, which failed
    # in #818 and passed in #834): a newer run's failing jobs replace the red's; every job it was ever seen failing stays
    # in jobs_seen, which the re-keyed merge matches on
    seen = sorted(set(d.get("jobs_seen") or d.get("jobs") or []) | set(ob["jobs"]))
    upd = {"runs": runs, "jobs": sorted(ob["jobs"]) if newer else sorted(set(d.get("jobs") or []) | set(ob["jobs"])),
           "jobs_seen": seen, "classifier": cv}  # observed under this classifier's key
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


# A CHECK THAT HAS NEVER BEEN GREEN IS NOT TRACKED (the owner, 2026-11-19, of "Alert backtest #9 -> IN PROGRESS ... fix
# 4528ed2 awaiting next scheduled run at 12:41Z": "remove it from tracked reds and don't alert in status line until it
# gets green for the first time, remember that for the future"). Alert backtest was born red on 2026-11-18 and failed
# its first nine runs: every one of them a red with offers, fixers, refuted fixes and a status-line mention, for a check
# that had never once passed. Now a red of a workflow that has NEVER had a green run on main -- or, in a workflow that
# has, of a job that never concluded success in a run on main -- is UNTRACKED: no item opens (so no offer, fixer, owner
# ask, inbox line, status-line mention or watchdog invariant), an open one is closed quietly, and ci-why and the status
# line leave it out; `agentctl red status -v` lists it under "untracked (never green)", for information. The first green
# run makes its next red an ordinary one. "Ever green" is GitHub's run history on main: a green run among the snapshot's
# runs, else `run list --status success`; a job, its success in the newest green run or any run of the snapshot's list.
# A green once seen is kept in the store (meta EVER_GREEN_META) for good; a never-green answer is asked again after
# EVER_GREEN_RECHECK. Anything unread keeps the red tracked: the rule never hides a red on an unknown. It applies to the
# reds of runs on main (gate, outside, stuck, a branch's prediction); main's local checks and flakes are tracked as ever.
EVER_GREEN_META = "ever_green"        # store meta: {repo: {"flows": {workflow: run id}, "jobs": {workflow: {job: run id}}}}
UNTRACKED_META = "red_untracked"      # store meta: {repo: [{key, workflow, jobs, run, why}]}: red status -v's list
EVER_GREEN_RECHECK = 10 * 60          # s before a never-green answer is asked of GitHub again
EVER_GREEN_JOB_RUNS = 10              # runs of the snapshot's list read for a job's success, at most
UNTRACKED_KINDS = ("gate", "outside", "stuck-run", "predicts")
_never_green_at = process_cache({})   # (repo, workflow[, job]) -> when GitHub last said never green
_run_jobs_mem = process_cache({})     # run id -> its jobs (a decided run's never change)


def ever_green_record(store):
    try:
        v = json.loads(store.meta_get(EVER_GREEN_META) or "{}")
        return v if isinstance(v, dict) else {}
    except (ValueError, TypeError):
        return {}


def ever_green_note(store, o_r, workflow, run_id, jobs=()):
    """Records workflow (and its green jobs) as green once, in the store; only a change is written."""
    rec = ever_green_record(store)
    mine = rec.setdefault(o_r, {})
    flows, js = mine.setdefault("flows", {}), mine.setdefault("jobs", {}).setdefault(workflow, {})
    fresh = [j for j in jobs if j not in js]
    if workflow in flows and not fresh:
        return
    flows.setdefault(workflow, run_id)
    for j in fresh:
        js[j] = run_id
    store.meta_set(EVER_GREEN_META, json.dumps(rec, sort_keys=True))


def green_job_names(jobs):
    return sorted({canon_job(j.get("name")) for j in jobs or [] if j.get("conclusion") == "success" and j.get("name")})


def job_matches(name, job):
    a, b = canon_job(name), canon_job(job)
    return a == b or registry_name(a) == registry_name(b) and " / " not in b


def decided_run_jobs(o_r, rid):
    if rid in _run_jobs_mem:
        return _run_jobs_mem[rid]
    jobs = gh_json(o_r, "run", "view", str(rid), "--json", "jobs").get("jobs") or []
    _run_jobs_mem[rid] = jobs
    return jobs


def newest_green_main(o_r, workflow):
    """The newest green run of workflow on main from GitHub (its file when main names it), or None."""
    wfile = main_workflow_files(o_r).get(workflow, workflow) if not red_is_other(o_r) else workflow
    runs = gh_json(o_r, "run", "list", "--workflow", wfile, "--branch", "main", "--status", "success", "--limit", "1",
                   "--json", RUN_FIELDS)
    return next((r for r in runs or [] if r.get("conclusion") == "success"), None)


def workflow_ever_green(store, o_r, workflow, runs=(), now_=None):
    """True when workflow has had a green run on main, False when GitHub says it never had, None when unread."""
    t = now_ if now_ is not None else time.time()
    if workflow in (ever_green_record(store).get(o_r) or {}).get("flows", {}):
        return True
    green = next((r for r in runs or [] if r.get("status") == "completed" and r.get("conclusion") == "success"), None)
    if not green:
        at = _never_green_at.get((o_r, workflow))
        if at is not None and t - at < env_num("AGENTCTL_EVER_GREEN_RECHECK", EVER_GREEN_RECHECK):
            return False
        try:
            green = newest_green_main(o_r, workflow)
        except (Exception, SystemExit):  # SystemExit: NoToken
            return None
    if not green:
        _never_green_at[(o_r, workflow)] = t
        return False
    ever_green_note(store, o_r, workflow, green.get("databaseId"))
    return True


def job_ever_green(store, o_r, workflow, job, runs=(), now_=None):
    """True when job of (ever green) workflow concluded success in a run on main; False when it did in none of the runs
    read -- at least one green run among them, every green one read; None (unknown, tracked) otherwise."""
    t = now_ if now_ is not None else time.time()
    known = ((ever_green_record(store).get(o_r) or {}).get("jobs") or {}).get(workflow) or {}
    if any(job_matches(k, job) for k in known):
        return True
    at = _never_green_at.get((o_r, workflow, job))
    if at is not None and t - at < env_num("AGENTCTL_EVER_GREEN_RECHECK", EVER_GREEN_RECHECK):
        return False
    decided = [r for r in runs or [] if r.get("status") == "completed" and r.get("conclusion") in ("success",) + RED_CONCLUSIONS]
    cands = sorted(decided, key=lambda r: r.get("conclusion") != "success")[:EVER_GREEN_JOB_RUNS]
    if not any(r.get("conclusion") == "success" for r in cands):
        try:
            g = newest_green_main(o_r, workflow)
        except (Exception, SystemExit):
            g = None
        cands = ([g] if g else []) + cands
    if not any(r.get("conclusion") == "success" for r in cands):
        return None  # no green run to read its jobs from: unknown, tracked
    for r in cands:
        rid = r.get("databaseId")
        try:
            jobs = decided_run_jobs(o_r, rid)
        except (Exception, SystemExit):
            if r.get("conclusion") == "success":
                return None  # a green run unread: its jobs may hold this one -- unknown, tracked
            continue
        greens = green_job_names(jobs)
        if greens:
            ever_green_note(store, o_r, workflow, rid, greens)
        if any(job_matches(g, job) for g in greens):
            return True
    _never_green_at[(o_r, workflow, job)] = t
    return False


def red_never_green(store, o_r, ob, now_=None):
    """Why observation ob is untracked ("never green: ..."), or None (tracked). Its workflow never green on main; or, a
    gate or outside red, every one of its jobs never green there."""
    if ob.get("red_kind") not in UNTRACKED_KINDS or not ob.get("workflow"):
        return None
    wf, runs = ob["workflow"], ob.get("runs_list") or []
    g = workflow_ever_green(store, o_r, wf, runs, now_)
    if g is False:
        return f"never green: {wf} has had no green run on main"
    if g is None or ob.get("red_kind") not in ("gate", "outside"):
        return None
    jobs = [j for j in ob.get("jobs") or [ob.get("job")] if j and j not in ("?", "infra", "stuck")]
    if not jobs:
        return None
    if all(job_ever_green(store, o_r, wf, j, runs, now_) is False for j in jobs):
        return f"never green: {', '.join(jobs)} of {wf} never passed on main"
    return None


def red_split_untracked(store, o_r, obs, now_=None):
    """(tracked obs, {key: (ob, why)} untracked): the observations of checks never green on main set apart."""
    tracked, untracked = {}, {}
    for key, ob in (obs or {}).items():
        try:
            why = red_never_green(store, o_r, ob, now_)
        except (Exception, SystemExit) as e:
            err(f"red: could not tell whether {key} was ever green ({str(e)[:120]}): tracked")
            why = None
        if why:
            untracked[key] = (ob, why)
        else:
            tracked[key] = ob
    return tracked, untracked


def red_note_untracked(store, o_r, untracked):
    """The untracked reds of o_r kept in the store (meta UNTRACKED_META) for red status -v; written only on a change."""
    try:
        cur = json.loads(store.meta_get(UNTRACKED_META) or "{}")
        cur = cur if isinstance(cur, dict) else {}
    except (ValueError, TypeError):
        cur = {}
    rows = [{"key": k, "workflow": ob["workflow"], "jobs": ob.get("jobs") or [], "why": why,
             "run": {x: (ob.get("run") or {}).get(x) for x in ("number", "databaseId")}}
            for k, (ob, why) in sorted(untracked.items())]
    if cur.get(o_r, []) == rows:
        return
    if rows:
        cur[o_r] = rows
    else:
        cur.pop(o_r, None)
    store.meta_set(UNTRACKED_META, json.dumps(cur, sort_keys=True))


def red_untracked_list(store):
    """[(repo, row)] of every untracked red (red_note_untracked's)."""
    try:
        cur = json.loads(store.meta_get(UNTRACKED_META) or "{}")
    except (ValueError, TypeError):
        return []
    return [(o_r, r) for o_r, rows in sorted((cur or {}).items()) for r in rows or []]


def red_untracked_runs(store, o_r, units):
    """{(workflow, run id): why} of the runs whose every red is untracked (ci-why and the status line leave them out)."""
    obs = red_obs_for(o_r, red_obs_of_units(units))
    _tracked, untracked = red_split_untracked(store, o_r, obs)
    runs = {}
    for ob in obs.values():
        k = (ob["workflow"], ob["run"].get("databaseId"))
        runs.setdefault(k, []).append(untracked.get(ob["key"], (None, None))[1])
    return {k: whys[0] for k, whys in runs.items() if all(whys)}


def red_active_untracked(store, o_r, it, untracked, obs, now_=None):
    """Why an open red is untracked now (its observation is, or its workflow/jobs were never green on main), else None.
    Only on a read observation (obs not None): an unknown never closes a red."""
    if obs is None or it["state"] in RED_TERMINAL:
        return None
    d = it["data"]
    if d["key"] in untracked:
        return untracked[d["key"]][1]
    if d["key"] in obs:
        return None
    wf = d.get("workflow")
    ob = {"red_kind": d.get("red_kind"), "workflow": wf, "jobs": d.get("jobs") or [d.get("job")],
          "runs_list": _gate_lists.get(wf) or _outside_lists.get(wf) or []}
    try:
        return red_never_green(store, o_r, ob, now_)
    except (Exception, SystemExit):
        return None


def red_untrack(store, it, why):
    """An open red of a check never green on main, closed quietly: its offer closed, its holder told to stop, no inbox
    line (the owner: no alert until it is green once)."""
    store.close_offer(f"red:{it['id']}", "cleared")
    red_stop_holder(store, it["data"]["key"], f"untracked: {why}", "red cleared")
    return red_move(store, it, "cleared", f"untracked: {why}", severity="info")


# A RED IS NAMED BY ITS WORKFLOW (2026-11-12, b059f05c70fa): opened while the forge read a run's run-name for its
# workflow's name, the red "Identity takes (convergence run 43013487979)" #40 named a workflow no other run is in: no
# newer run of it could verify, refute or clear it, its verify dispatch found "no workflow file named" it, and it sat in
# fix-pending 6 h at a time. Such a red takes its workflow's name from its failing run's workflow file, once.
def red_workflow_named(store, it, o_r):
    """The red under its workflow's name (renamed in place when its run says another); it unchanged otherwise."""
    d = it["data"]
    rid = (d.get("last_run") or {}).get("databaseId")
    if not rid or d.get("workflow_named") or d.get("workflow") == LOCAL_FLOW or d.get("red_kind") in ("local", "predicts"):
        return it
    files = main_workflow_files(o_r)
    if not files or d.get("workflow") in files:
        return it
    try:
        run = forge_actions_run(o_r, rid)
    except (Exception, SystemExit):
        run = None
    if not run:
        return it  # unread now: tried again next tick
    name = {f: n for n, f in files.items()}.get(run.get("workflow_id"))
    upd = {"workflow_named": True}
    if name and name != d.get("workflow"):
        upd["workflow"] = name
        upd["history"] = (d.get("history") or []) + [{"at": iso_at(store.clock()), "holder": None,
                                                     "outcome": f"RENAMED: {d.get('workflow')!r} is a run-name; its "
                                                                f"workflow is {name!r} ({run.get('workflow_id')})"}]
    store.update_item("red", it["id"], it["version"], upd)
    return store.item("red", it["id"])


def red_step(store, it, obs, snap, facts, repo, o_r, t):
    it = store.item("red", it["id"])
    if it["state"] not in RED_TERMINAL:
        it = red_workflow_named(store, it, o_r)
    d, st, key = it["data"], it["state"], it["data"]["key"]
    row = store.get(red_lease(key))
    live = lease_is_live(row, t)
    gone = obs is not None and red_gone(it, obs, snap)
    fix = red_fix_current(store, it, facts)
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
    off = repo_dispatch_off(store, red_repo_of(d))
    if off and st in ("red", "offered", "leased", "blocked-external", "dispatch-off"):
        return red_hold_off(store, it, row, block, gone, obs, t)
    if st == "dispatch-off":  # its repo's dispatch is on again: offered
        red_offer(store, it, f"{red_what(d)}: dispatch on again for {red_repo_of(d)}: offered")
        return [f"offered {red_what(d)} (dispatch on again)"]
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
        way = None if live or block else red_run_on_way(d, snap, repo)
        if way:  # A RUN ON THE WAY MAY HAVE FIXED IT (issue 20261107T224109-5c81e4): wait for it, never a fixer meanwhile
            run, sha = way
            store.close_offer(f"red:{it['id']}", "fix-pending")
            line = (f"{red_what(d)}: {sha[:10]} on main reaches its failing job (no Fixes-Red): awaiting "
                    f"#{run.get('number')}")
            red_move(store, it, "fix-pending", line, data={"fix": {
                "status": "verify", "at": t, "entry": None, "sha": "", "awaiting": run.get("number"),
                "verify_dispatched": ts(), "verify": f"awaiting #{run.get('number')}, which contains {sha[:10]}"}})
            return [line]
        if live:
            store.close_offer(f"red:{it['id']}", "taken")
            red_move(store, it, "leased", f"{red_what(d)}: leased by {row['holder']}", data={"holder": row["holder"]})
            return [f"leased {red_what(d)} to {row['holder']}"]
        if row and float(lease_meta(row).get("released_at") or 0) > it["entered_at"] and \
                float(row.get("acquired_at") or 0) >= it["entered_at"] and not red_lease_read(d, row):
            # taken AND ended between two ticks: its attempt -- once (red_lease_read: a re-offer keeps entered_at)
            return red_lease_ended(store, it, row, block, t, repo)
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
        return red_lease_ended(store, it, row, block, t, repo)
    if st == "fix-pending":
        return red_fix_pending(store, it, fix, gone, obs, snap, facts, repo, o_r, t)
    if st == "blocked-external":
        if live:
            red_move(store, it, "leased", f"{red_what(d)}: a live fixer outranks the block", data={"holder": row["holder"]})
            return [f"leased {red_what(d)} (over its block)"]
        if gone:
            red_close(store, it, "cleared", red_gone_why(d, obs))
            return [f"cleared {red_what(d)}"]
        if fix:  # A LINKED FIX ANSWERS THE BLOCK (58f3db9e8a3d, 2026-11-12: on the owner only because the fix was not
            # linked): the red waits for the run that proves it; the owner's question and the block go
            lifted = red_lift_block(store, it, block, f"its fix {fix.get('entry') or str(fix.get('sha'))[:10]} is on its way")
            red_move(store, it, "fix-pending", f"{red_what(d)}: fix {fix.get('entry') or fix.get('sha')}"
                     + (f" ({lifted})" if lifted else ""), data={"fix": fix, "cause": None, "block": None, "ask": None})
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


def red_hold_off(store, it, row, block, gone, obs, t):
    """Red `it` of a repo whose dispatch the owner turned off: recorded, never offered or asked -- its offer closed, a
    live lease released, its owner block (a vault ask included) lifted, the red held in `dispatch-off`; a newer run
    still clears it."""
    d = it["data"]
    repo, acts = red_repo_of(d), []
    if gone and obs is not None:
        red_close(store, it, "cleared", red_gone_why(d, obs))
        return [f"cleared {red_what(d)}"]
    store.close_offer(f"red:{it['id']}", "dispatch-off")
    if lease_is_live(row, t):
        store.revoke(row["name"], f"RELEASED: dispatch off for {repo} (owner)")
        acts.append(f"released {row['name']} ({row['holder']}): dispatch off for {repo}")
    if block:
        lifted = red_lift_block(store, it, block, f"dispatch off for {repo} (owner)")
        if lifted:
            acts.append(lifted)
    if it["state"] != "dispatch-off":
        red_move(store, store.item("red", it["id"]), "dispatch-off",
                 f"{red_what(d)}: dispatch off for {repo} (owner): recorded, not offered",
                 data={"holder": None, "block": None, "cause": None, "ask": None}, severity="info")
        acts.append(f"held {red_what(d)}: dispatch off for {repo}")
    return acts


def red_lift_block(store, it, block, why):
    """Release red it from its live owner block (the block itself when it holds only this red, else this red leaves its
    keys). Returns what it did, or None when no block holds it."""
    if not block:
        return None
    key = it["data"]["key"]
    keys = [k for k in (block.get("meta") or {}).get("keys") or [] if k != key]
    if keys:
        row = store.get(block["name"])
        store.set_meta(block["name"], row["epoch"], row["secret"], {"keys": keys})
        return f"left the block {block['name']}"
    store.revoke(block["name"], f"LIFTED: {why}")
    return f"its block {block['name']} lifted"


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


# ONE END, ONE ATTEMPT (2026-11-18 23:42-23:49Z, core Main Pipeline #1053, 0c8eea91b712): one fixer lease, revoked
# ORPHANED once, was read into the red's history five times a minute apart -- the `offered` step matched the same
# ended lease at every tick, since the re-offer (a self-transition) keeps the item's entered_at -- and "3 launches in
# a row" twice made a CRASH LOOP and then BLOCKED ON OWNER, with no fixer started after the first. The epoch of the
# lease whose end the red read is kept (`ended_lease`); that end is never read again.
def red_lease_read(d, row):
    """True when the red's history already holds the end of this lease (its epoch)."""
    return bool(row) and row.get("epoch") is not None and d.get("ended_lease") == row.get("epoch")


def red_lease_ended(store, it, row, block, t, repo=None):
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
           "attempts": int(d.get("attempts") or 0) + (1 if counted else 0),
           "ended_lease": (row or {}).get("epoch")}
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
    fix = red_fix_of({"data": {**d, **upd}}, {"repo": repo}) if RED_LANDED_VERDICT.match(out) else None
    if fix:  # ITS FIXER LANDED ITS FIX (784badc4406e.4, 2026-11-13: `LANDED 8d06b1a5ab` was offered again, then put on
        # the owner): the red waits for the run that proves it, its owner block (if any) answered by the fix
        lifted = red_lift_block(store, it, block, f"its fixer landed {fix['sha'][:10]}")
        store.close_offer(f"red:{it['id']}", "fix-pending")
        red_move(store, it, "fix-pending", f"{red_what(d)}: {h['holder']} landed its fix {fix['sha'][:10]}: verifying"
                 + (f" ({lifted})" if lifted else ""),
                 data={**upd, "fix": {**fix, "verify_since": t}, "cause": None, "block": None, "ask": None})
        return [f"fix-pending {red_what(d)} (its fixer landed {fix['sha'][:10]})"]
    waits = red_waits_on_of(out, d.get("key"), store)
    if waits:  # A HOLD ON ANOTHER RED'S FIX (red_waits_on_of): a state agentctl resolves itself, never a failed action
        lifted = red_lift_block(store, it, block, f"it waits on red {waits['red']}'s fix {waits['sha'][:10]}")
        store.close_offer(f"red:{it['id']}", "fix-pending")
        dec = d.get("decision")
        red_move(store, it, "fix-pending", f"{red_what(d)}: waits on red {waits['red']}'s fix {waits['sha'][:10]}"
                 + (f" ({lifted})" if lifted else ""),
                 data={**upd, "fix": {"entry": None, "status": RED_WAITS_ON, "at": t, **waits}, "cause": None,
                       "block": None, "ask": None, **({"decision": {**dec, "status": "waiting"}} if dec else {})})
        return [f"fix-pending {red_what(d)} (waits on red {waits['red']}'s {waits['sha'][:10]})"]
    if block:
        red_move(store, it, "blocked-external", f"{red_what(d)}: {block['text']}",
                 data={**upd, "cause": block.get("cause"), "block": block["name"]})
        return [f"blocked {red_what(d)}"]
    if GAVE_UP_FIXED.match(out):  # ALREADY FIXED: the red's probe decides, never the fixer's word alone
        return red_already_fixed(store, it, upd, out, t)
    decide = red_decision_needed(d, h) if str(h["outcome"]).startswith("DIAGNOSED:") else None
    if not decide and GAVE_UP_DECIDE.match(out):  # THE FIXER NEEDS THE OWNER'S DECISION: one WAITING ON YOU, with its
        m = RED_DECIDE.search(out)  # own words (and the category it names), never offered to the next fixer meanwhile
        decide = ((m.group(1) or "") + m.group(2).strip()) if m else re.sub(r"^\W*GAVE UP:\s*", "", out).strip()
        upd["gave_up_ask"] = True  # routed back (red_ask_route), it counts as the attempt it was
        if m:  # its words before the ask are the diagnosis the owner reads
            upd["diagnosis"] = re.sub(r"^\W*GAVE UP:\s*", "", out[:m.start(1) if m.group(1) else m.start(2)])
            upd["diagnosis"] = re.sub(r"[;\s]*decide\s*$", "", upd["diagnosis"]).strip()[:600]
    vault = vault_ask_ns(decide or (out if out.startswith("BLOCKED ON OWNER") else ""))
    if vault:  # ITS ONE ASK IS A VAULT UNLOCK: a wait agentctl checks itself (red_vault_block), never a decision
        return red_vault_block(store, it, upd, vault, out, t)
    if red_decision(d) and DECISION_FAILED.match(str(h["outcome"])):  # THE OWNER'S DECIDED ACTION FAILED: told
        return red_decision_failed(store, it, upd, h["outcome"], t)
    if decide:
        return red_decision_block(store, it, upd, decide, t)
    issue = red_blamed_issue(store, out)
    if issue:
        red_harness_block(store, it, upd, issue, AGENTCTL_DEFECT_CAUSE, f"~/bin/agentctl issue --list",
                          f"a newer {d.get('workflow')} run no longer fails this way",
                          f"its fixer blamed agentctl: open agentctl issue {issue} ({out[:200]}). Offered again when the "
                          "issue is closed or a newer run decides it", t)
        return [f"blocked {red_what(d)} on agentctl issue {issue}"]
    streak = red_crash_streak({**d, **upd})
    if len(streak) >= env_num("AGENTCTL_RED_CRASH_LOOP", RED_CRASH_LOOP):
        return red_crash_loop(store, it, upd, streak, t)
    red_offer(store, it, f"{red_what(d)}: {h['holder']} ended ({h['outcome'][:120]}): offered again", data=upd,
              quiet=not counted)
    return [f"re-offered {red_what(d)} after {h['outcome'][:60]}"]


# A RED WHOSE FIX IS ANOTHER RED'S COMMIT (2026-11-16, Order independence #78, e948278d144a): its fix was the main-tests
# fixer's 715ab5a1a for red e1be6bd9fd46, not on main yet. The owner answered "hold until 715ab5a1a is on main"; the
# fixer held as decided, and its `NOT LANDED: holding ...` read as THE DECIDED ACTION FAILED -- a fresh WAITING ON YOU,
# twice (ev10700, ev10709). Now that hold is a state: fix-pending with a fix of status `waits-on` {sha, red}, from the
# verdict `WAITS ON <sha> (red <key>)` -- or a NOT LANDED / GAVE UP naming `<sha> (... red <key>)` of another red --
# and the reconciler resolves it itself (red_waits_on_step): the sha, or its rebased copy, or the other red's own
# landed fix on main -> a landed fix verified against the next run; the sha set aside by the other red, or that red
# closed without it on main -> offered again. It waits as long as the other red is open: that red has its own holder.
RED_WAITS_ON = "waits-on"
RED_WAITS_ON_VERDICT = re.compile(r"^\W*WAITS ON ([0-9a-f]{7,40})\s*\(red ([0-9a-f]{12}(?:\.\d+)?|[\w.-]+)\)")
RED_WAITS_ON_WORDS = re.compile(r"^\W*(?:NOT LANDED|GAVE UP)\b.*?\b([0-9a-f]{7,40})\s*\([^()]*?\bred ([\w.-]+)\)",
                                re.S)


def red_waits_on_of(outcome, own_key, store):
    """{sha, red} when a fixer's verdict says this red waits on another red's commit, else None."""
    out = str(outcome or "")
    m = RED_WAITS_ON_VERDICT.match(out) or RED_WAITS_ON_WORDS.match(out)
    if not m or m.group(2) == own_key:
        return None
    key = re.sub(r"\.\d+$", "", m.group(2))
    if key == own_key or not (red_closed_of(store, key) or store.item("red", key)):
        return None  # names no red agentctl knows: no state to wait on
    if red_wait_cycle(store, own_key, key):
        return None  # that red waits on this one in turn: no wait, a fixer's work (red_wait_cycle)
    return {"sha": m.group(1), "red": key}


# A WAIT CYCLE HAS NO EXIT (review 2026-11-16): red A waiting on red B's commit while B waits (directly or through others)
# on A's -- neither lands first, each one's holder is "the red it waits on" (red_fix_held), so W2 never fires. A wait
# that would close a cycle is refused at the verdict (red_waits_on_of), and one found standing is offered again.
RED_WAIT_CHAIN_MAX = 20


def red_wait_cycle(store, own_key, other):
    """The keys from `other` back to own_key when other waits (through its waits-on chain) on own_key, else None."""
    chain, cur = [], other
    while cur and len(chain) < RED_WAIT_CHAIN_MAX:
        chain.append(cur)
        o = red_of_key(store, cur)
        f = ((o or {}).get("data") or {}).get("fix") or {}
        if not o or o["state"] in RED_TERMINAL or f.get("status") != RED_WAITS_ON:
            return None
        cur = f.get("red")
        if cur == own_key:
            return chain
        if cur in chain:
            return None  # a cycle among others: theirs to break
    return None


def red_of_key(store, key):
    """The red item of key now: its open spell, else its newest closed one."""
    try:
        for it in store.items("red"):
            if it["data"].get("key") == key and it["state"] not in RED_TERMINAL:
                return it
        return red_closed_of(store, key)
    except Exception:
        return None


def red_waits_on_step(store, it, f, repo, t):
    """A fix-pending red waiting on another red's commit (RED_WAITS_ON, above): resolved, or left waiting."""
    d = it["data"]
    sha, other = f.get("sha") or "", f.get("red")
    o = red_of_key(store, other)
    od = (o or {}).get("data") or {}
    ofix = od.get("fix") or {}
    for cand in [sha] + ([ofix["sha"]] if ofix.get("status") == "landed" and ofix.get("sha") else []):
        on = red_main_equivalent(repo, cand)
        if on and red_sha_on_main(repo, on):
            line = f"{red_what(d)}: red {other}'s fix {cand[:10]} is on main ({on[:10]}): verifying"
            red_move(store, it, "fix-pending", line, data={"fix": {"entry": None, "status": "landed", "sha": on,
                                                                    "at": t, "verify_since": t, "waited_on": other}})
            return [line]
    aside = set(od.get("refuted") or []) | set(od.get("unverified") or [])
    why = None
    cycle = red_wait_cycle(store, d.get("key"), other)
    if not o:
        why = f"red {other}, whose fix {sha[:10]} it waited on, is gone"
    elif cycle:
        why = (f"red {other} waits on this red in turn ({' -> '.join(cycle + [str(d.get('key'))])}), a wait cycle neither "
               f"side can end: land the fix here, or say why not")
    elif any(x.startswith(sha) or sha.startswith(x) for x in aside if x and sha):
        why = f"red {other} set aside the fix {sha[:10]} it waited on"
    elif o["state"] in RED_TERMINAL:
        why = f"red {other} closed ({o['state']}) without the fix {sha[:10]} it waited on reaching main"
    if why:
        h = {"at": iso_at(t), "holder": f"waits-on:{other}", "outcome": f"{why}: offered again"}
        red_offer(store, it, f"{red_what(d)}: {h['outcome']}", data={"fix": None, "history": (d.get("history") or []) + [h]})
        return [f"re-offered {red_what(d)} ({why[:80]})"]
    if t >= it["deadline"]:  # still waiting while the other red is open: its own holder works on it; said quietly
        red_move(store, it, "fix-pending", f"{red_what(d)}: waits on red {other}'s fix {sha[:10]} (not on main yet)",
                 severity="info")
    return []


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
            why = str(h.get("outcome"))[:400] + (f" -- {last[-1][:100]}" if last else "")  # a dead wrapper's last words
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
    issue = red_harness_issue(store, d, n, tier, red_crash_launches(streak), cmd)
    text = (f"HARNESS FAULT: the fixers crash on {red_what(d)}: {n} launches in a row at the {tier} tier ended with "
            f"no LANDED / GAVE UP ({red_crash_launches(streak)}). The fixer harness, not the code, is what fails: "
            f"agentctl's own bug, filed for its maintainer as issue {issue} (logs: `{cmd}`). Auto-clears when {until}, "
            f"when the issue is closed, or in {fmt_age(env_num('AGENTCTL_RED_CRASH_BLOCK_MAX', RED_CRASH_BLOCK_MAX))} "
            "(offered again)")
    upd = {**upd, "crash_loop": {"stage": "harness", "mark": mark, "at": t, "from": tier, "launches": n}}
    red_harness_block(store, it, upd, issue, CRASH_LOOP_CAUSE, cmd, until, text, t, tail=tail)
    return [f"crash loop on {red_what(d)}: a harness fault, filed as agentctl issue {issue}"]


def red_harness_block(store, it, upd, issue, cause, cmd, until, text, t, tail=None):
    """Red `it` waits on agentctl's own issue `issue` (a harness fault, never the owner's): a block lease the issue's
    close, a newer run's verdict or its max hold lifts (block_invalid, red_renew_blocks), the red blocked-external."""
    d = {**it["data"], **upd}
    key, run = d["key"], (d.get("last_run") or {}).get("number")
    lname = block_lease(key)
    probe = red_newer_probe(it, d)
    got = store.acquire(lname, f"harness:{cause}", BLOCK_TTL,
                        env_num("AGENTCTL_RED_CRASH_BLOCK_MAX", RED_CRASH_BLOCK_MAX), progress=f"{cause}@{int(t)}",
                        meta={"kind": "block", "crash_loop": True, "harness": True, "issue": issue,
                              "cause": cause, "cmd": cmd, "probe": probe, "until": until, "keys": [key],
                              "text": text, "details": text, "probed_at": t})
    if "held" in got:  # somebody's block already holds the red: it stands, the red joins it
        row = store.get(lname)
        store.set_meta(lname, row["epoch"], row["secret"], {"keys": sorted(set(lease_meta(row).get("keys") or []) | {key})})
    line = f"RED HARNESS FAULT: {red_what(d)}: {text}"
    store.close_offer(f"red:{it['id']}", "blocked")
    red_move(store, it, "blocked-external", line, data={**upd, "cause": cause, "block": lname}, severity="info",
             refs={"key": key, "run": run, "workflow": d.get("workflow"), "verdict": line, "tail": tail,
                   "command": "red", "final": False})


# A RED BLAMED ON AN OPEN agentctl ISSUE WAITS ON IT (issue 20261101T030050-695594: the false stuck red 50dd506d93c9 was
# offered back to its owner 3 times in 2 minutes, each turn ending GAVE UP naming the filed agentctl issue: no fix for it
# can land in the red's repo). A verdict that names an OPEN agentctl issue as the cause holds the red on a harness block
# until that issue closes (or a newer run decides it, or the max hold): agentctl's maintainer fixes it, not a fixer.
RED_ISSUE_ID = re.compile(r"\b(\d{8}T\d{6}-[0-9a-f]{6})\b")
RED_ISSUE_BLAME = re.compile(r"\bagentctl\b.*\b(defect|bug|issue)\b|\b(defect|bug|issue)\b.*\bagentctl\b", re.I | re.S)
AGENTCTL_DEFECT_CAUSE = "agentctl-defect"


def red_blamed_issue(store, out):
    """The open agentctl issue a fixer's verdict blames its red on, or None."""
    if not RED_ISSUE_BLAME.search(out) or not re.match(r"\W*(GAVE UP|DIAGNOSED|BLOCKED)", out):
        return None
    for iid in RED_ISSUE_ID.findall(out):
        iss = store.item("issue", iid)
        if iss and iss["state"] != "closed":
            return iid
    return None


# A HARNESS FAULT IS agentctl's OWN BUG (the owner, 2026-11-19: core Main Pipeline #1053 read "BLOCKED ON OWNER: the
# fixers crash" -- a cross-host lease misjudgement and a re-counted lease end, agentctl's bugs fixed in 852acad). The
# fixers crash-looping at the deep tier is the harness failing, never the owner's to repair: ONE agentctl issue per red
# (its maintainer's queue: the status page, `agentctl issue --list`, its weekly escalation), and the red waits on a
# harness block, never an owner ask (red_ask: no owner-only category) -- lifted by a newer run's verdict, the issue
# closed (red_renew_blocks), or its max hold.
def red_harness_issue(store, d, n, tier, launches, cmd):
    """The id of the open agentctl issue for this red's harness fault, filed once (an open one is reused)."""
    tag = f"[harness-fault {d.get('key')}]"
    for i in store.items("issue", ("open",)):
        if str((i.get("data") or {}).get("text") or "").startswith(tag):
            return i["id"]
    text = (f"{tag} the fixers crash on {red_what(d)}: {n} launches in a row at the {tier} tier ended with no LANDED / "
            f"GAVE UP ({launches}). Diagnose agentctl (orphaned leases, wrapper deaths, lease ends counted twice, launch "
            f"refusals) in a ~/bin-wt-<name> worktree, test-first, and close this issue with the fix's sha; its red is "
            f"offered again once it is closed. Logs: `{cmd}`")
    return issue_open(store, text, "agentctl (harness fault)")["id"]


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
RED_DECIDE = re.compile(r"(?:;|^\W*GAVE UP:.*?)\s*decide(\[[\w-]+\])?:\s*(.+)$", re.S | re.I)


VERDICT_MAX = 600  # chars of a fixer's or owner's verdict kept as its lease's outcome


def verdict_cut(text, n=VERDICT_MAX):
    """A verdict cut to n chars that keeps its `decide[...]: ...` clause (issue 20261101T082701-ab1154: a long cause
    before `; decide:` cut the decision off, and the red, whose decision is parsed from this outcome, went back to its
    owner every turn): the cause is shortened, never the decision (itself cut only past n)."""
    text = str(text or "")
    m = RED_DECIDE.search(text) if len(text) > n else None
    if not m:
        return text[:n]
    at = text.rfind("decide", 0, m.start(2))
    clause = "; " + text[at:]
    head = text[:at].rstrip().rstrip(";").rstrip()
    return (head[:max(0, n - len(clause) - 4)].rstrip() + " ..." + clause)[:max(n, len(clause) + 40)]


def red_decision_needed(d, h):
    """The decision the owner must make on red d, whose launch h ended DIAGNOSED, or None (an implementer goes on)."""
    m = RED_DECIDE.search(str(h["outcome"]))
    if m:
        return (m.group(1) or "") + m.group(2).strip()
    for p in reversed(d.get("history") or []):
        o = str(p.get("outcome") or "")
        if RED_CRASH_OUTCOME.match(o) or RED_CRASH_NEUTRAL.match(o):
            continue
        if o.startswith("DIAGNOSED:"):
            return ("it was DIAGNOSED twice in a row and its fixers cannot fix it within the red rules: decide how it "
                    "is fixed (a design change, a known flip to accept, or the fix a fixer may make)")
        return None
    return None


def red_ask_route(store, it, upd, why, words, t):
    """An ask outside the owner-only categories (ask.py OWNER_ONLY) never reaches the owner: the red goes back to its
    fixers, briefed to proceed under the repo's standing rules (CLAUDE.md: root cause, test-first, never a rerun) and its
    checked-in policies, with the words it asked in as the diagnosis -- an attempt like any other, so the ladder climbs."""
    d = {**it["data"], **upd}
    note = (f"{str(words)[:400]} -- NOT AN OWNER QUESTION ({why}). Proceed: make the fix the repo's standing rules "
            "dictate (CLAUDE.md: root cause, test-first, never rerun to green) and the checked-in policies answer (e.g. "
            "movies scripts/ci/lane-policy.tsv for a ci-runner lane); ask the owner only in an owner-only category, "
            "`decide[<category>]: ...` (the fixer brief)")
    extra = {"attempts": int(d.get("attempts") or 0) + 1} if upd.get("gave_up_ask") else {}
    store.close_offer(f"red:{it['id']}", "routed")
    red_offer(store, it, f"{red_what(d)}: its ask is no owner's ({why}): offered again to proceed",
              data={**upd, **extra, "gave_up_ask": None, "diagnosis": note[:900], "cause": None, "block": None,
                    "ask": None}, quiet=True)
    return [f"routed {red_what(d)}'s ask back to its fixer: {why[:120]}"]


def red_decision_block(store, it, upd, decide, t, category=None):
    """The red waits on the owner's decision: its block carries the diagnosis and the decision, clears itself. Only
    in an owner-only category (`[<category>] <decision>`, ask_split): any other ask is routed back (red_ask_route)."""
    cat, question = ask_split(decide)
    category = category or cat
    why = ask_refusal(category, question)
    if why:
        return red_ask_route(store, it, upd, why, question, t)
    decide = question
    upd = {k: v for k, v in upd.items() if k != "gave_up_ask"}
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
    probe = red_newer_probe(it, d)
    got = store.acquire(lname, f"owner:{DECISION_CAUSE}", BLOCK_TTL, maxh, progress=f"decision@{int(t)}",
                        meta={"kind": "block", "decision": True, "cause": DECISION_CAUSE, "cmd": cmd, "probe": probe,
                              "until": until, "keys": [key], "text": text, "details": diag, "decide": decide,
                              "question": decide, "category": category, "probed_at": t})
    if "held" in got:  # somebody's block already holds the red: it stands, the red joins it
        row = store.get(lname)
        store.set_meta(lname, row["epoch"], row["secret"], {"keys": sorted(set(lease_meta(row).get("keys") or []) | {key})})
    line = f"RED BLOCKED ON OWNER [{category}]: {text}"
    store.close_offer(f"red:{it['id']}", "blocked")
    red_owner_block(store, it, line, t, data={**upd, "cause": DECISION_CAUSE, "block": lname},
                    refs={"key": key, "run": run, "workflow": d.get("workflow"), "verdict": line, "command": "red"})
    return [f"decision needed on {red_what(d)}: blocked on the owner"]


def red_newer_probe(it, d):
    """The probe a harness or decision block of red `it` (data d) lifts on: a newer decided run of its workflow, read
    in the red's OWN repo's snapshot (issue 20261102T085329-930a4a: a core red's block read movies')."""
    return {"kind": "red_newer_verdict", "args": {"workflow": d.get("workflow"), "item": it["id"],
                                                  "run_id": (d.get("last_run") or {}).get("databaseId"),
                                                  "run": (d.get("last_run") or {}).get("number"), "repo": d.get("repo")}}


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


def red_fix_current(store, it, facts):
    """THE fix on its way for this red: red_fix_of's, corrected by the store (red_fix_fresh) -- and, once the store has
    turned it into a landed sha, never one the red refuted or set aside. red_fix_of can only judge the shas it reads; an
    entry its file still calls open is upgraded to its landed sha afterwards. 2026-11-11, Verify scrape recording #23
    (784badc4406e.3): a stale `waiting` entry came back as the refuted 547709c1e2 every tick, offered -> fix-pending ->
    offered 593 times, and no fixer was ever offered the red. Each candidate is judged in turn (red_fix_of): one set
    aside never hides the next."""
    return red_fix_of(it, facts, store)


# A FOLLOWED FIX IS A LATER CHANGE (2026-11-19, Alert backtest #4, 5254bffda9d2): its fix ...-8b34c0 was cancelled and
# its worktree re-queued a minute later, but the red followed it to ...-130, a change that worktree (one owner's, reused
# for days) landed on 11-11 -- the change items carried no `submitted_at`, so "later than the cancelled one" was "later
# than 0", the oldest first. Its phantom tip b6d3d8235 was no fix for this spell (red_fix_of), so the same tick's
# invariant set it aside and escalated it: "UNVERIFIED: INVARIANT: no fix on its way for 0 min in fix-pending". A
# change's submission is its `submitted_at`, else its id's stamp; one with neither follows nothing.
def change_submitted_at(ch):
    """When change item ch was submitted (epoch): its `submitted_at`, else its id's YYYYMMDD-HHMMSS stamp; None."""
    v = (ch.get("data") or {}).get("submitted_at")
    if v:
        return float(v)
    age = entry_id_age(str(ch.get("id") or ""), 0.0)
    return -age if age is not None else None


def red_fix_follow(store, f, repo, t):
    """(fix, its change's state) for a not-yet-landed fix f whose change ENDED without landing: the change that carries
    its work since -- the earliest later change of the same worktree (landed, or still open) -- or f's own commits
    landed on main by content; (None, state) when there is none; (None, None) when its change has not ended."""
    ch = red_change(store, f.get("entry"))
    if not ch or ch["state"] not in CHANGE_ENDED:
        return None, None
    cd = ch["data"] or {}
    wt, since = cd.get("worktree"), change_submitted_at(ch)
    later = []
    try:
        later = sorted((o for o in store.items("change") if o["id"] != ch["id"] and wt and o["data"].get("worktree") == wt
                        and since is not None and (change_submitted_at(o) or 0) >= since
                        and o["state"] not in CHANGE_ENDED),
                       key=lambda o: change_submitted_at(o) or 0)
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
    if f.get("status") == RED_WAITS_ON and not (obs is not None and gone):
        return red_waits_on_step(store, it, f, repo, t)
    if f.get("sha") and red_fix_is_set_aside(f["sha"], red_set_aside(d, facts), facts):
        # PENDING ON A FIX IT ALREADY REFUTED (or set aside): no run can verify it now; back to a fixer
        if fix and fix.get("sha") != f["sha"]:  # another fix is on its way: it is the one verified now
            line = (f"{red_what(d)}: its fix {f['sha'][:10]} was already set aside; fix "
                    f"{fix.get('entry') or str(fix.get('sha'))[:10]} {fix['status']}")
            red_move(store, it, "fix-pending", line, data={"fix": {**fix, "verify_since": t}
                                                           if fix["status"] == "landed" else fix})
            return [line]
        line = f"{red_what(d)}: its fix {f['sha'][:10]} was already refuted: offered again"
        red_offer(store, it, line, data={"fix": None})
        return [f"re-offered {red_what(d)} (its fix {f['sha'][:10]} already refuted)"]
    if f.get("status") == "landed" and f.get("sha"):  # A LANDED FIX IS A COMMIT ON MAIN (above red_main_equivalent)
        on = red_fix_on_main(f, repo)
        if on is None and not (fix and fix.get("sha") != f["sha"]):
            h = {"at": iso_at(t), "holder": f.get("entry") or f["sha"][:10],
                 "outcome": f"its fix {f['sha'][:10]} is not on main (nor its content): offered again"}
            red_offer(store, it, f"{red_what(d)}: {h['outcome']}",
                      data={"fix": None, "history": (d.get("history") or []) + [h]})
            return [f"re-offered {red_what(d)} (its fix {f['sha'][:10]} is not on main)"]
        early = on is not None and f.get("at") is not None and \
            float(f["at"]) < float(d.get("opened_at") or 0) - 3600 and not (fix and fix.get("sha") != f["sha"])
        if early:  # landed before this spell opened: the run that opened it already contained it (red_fix_of)
            h = {"at": iso_at(t), "holder": f.get("entry") or f["sha"][:10],
                 "outcome": f"its fix {on['sha'][:10]} landed before this red opened (its failing run contained it): "
                            f"offered again"}
            red_offer(store, it, f"{red_what(d)}: {h['outcome']}",
                      data={"fix": None, "history": (d.get("history") or []) + [h]})
            return [f"re-offered {red_what(d)} (its fix {on['sha'][:10]} predates it)"]
        if on is not None and on["sha"] != f["sha"]:
            line = f"{red_what(d)}: its fix {f['sha'][:10]} landed as {on['sha'][:10]}: verifying that"
            it = red_move(store, it, "fix-pending", line, data={"fix": on})
            d, f = it["data"], it["data"]["fix"]
    if f.get("sha") and not fix and not f.get("waited_on") and not fix_reaches_red(repo, f["sha"], d):
        # PENDING ON ANOTHER RED'S FIX (PER RED, PER FAILING STEP: Order independence #74's swift red on 09e482c6a, the
        # ios red's fix): no run of it can prove this red fixed -- offered again, never parked until the give-up. Not
        # a red whose fixer said its fix IS that red's commit (WAITS ON, red_waits_on_step: `waited_on`): the commit on
        # main is the verification it waited for, whatever the reading rules make of its failing job (review 2026-11-16)
        line = f"{red_what(d)}: its fix {f['sha'][:10]} touches nothing its failing job reads: offered again"
        red_offer(store, it, line, data={"fix": None})
        return [f"re-offered {red_what(d)} (its fix {f['sha'][:10]} is another red's)"]
    if fix and not fix.get("entry") and f.get("entry"):  # its own change landed that very commit: that change's fix
        cur = f if f.get("status") == "landed" else red_fix_fresh(store, f)
        if cur and cur.get("status") == "landed" and cur.get("sha") == fix.get("sha"):
            fix = cur
    if fix and (fix.get("entry"), fix.get("sha"), fix["status"]) != (f.get("entry"), f.get("sha"), f.get("status")):
        if fix["status"] == "landed" and f.get("status") != "landed":
            # VERIFICATION STARTS WHEN THE FIX LANDS: the state's deadline (and red_fix_since) run from here, never
            # from the hours it waited in the queue behind a red gate
            it = red_move(store, it, "fix-pending", f"{red_what(d)}: fix {fix.get('entry') or fix.get('sha')} landed "
                                                    f"({str(fix.get('sha'))[:10]}): verifying",
                          data={"fix": {**f, **fix, "verify_since": t} if f.get("status") in FIX_OPEN else {**f, **fix}})
        else:  # another change is the fix now: its own record, never the last one's fields (a stale `linked`)
            store.update_item("red", it["id"], it["version"],
                              {"fix": {**f, **fix} if fix.get("entry") == f.get("entry") else fix})
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
            red_move(store, it, "fix-pending", line, severity="escalate")
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
            if f.get("awaiting"):  # the run it awaited still fails this way: nobody's fix was refuted
                red_offer(store, it, f"{red_what(d)}: still red on #{f['awaiting']}, the run it awaited: offered again",
                          data={"fix": None})
                return [f"re-offered {red_what(d)} (still red on #{f['awaiting']})"]
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
            red_move(store, it, "fix-pending", f"RED {red_what(d)}: unverified {fmt_age(t - it['entered_at'])} after its block",
                     severity="escalate")
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
        refuted = red_refuted({"refuted": (d.get("refuted") or []) + [sha]})
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
    if not holds and red_waits_for_its_run(d) and "next_run_at" not in f and d.get("workflow") != LOCAL_FLOW:
        plan = red_verify_plan(o_r, repo, d.get("workflow"), float(f.get("verify_since") or f.get("at") or t))
        if plan:  # A SCHEDULED WORKFLOW'S FIX WAITS FOR ITS SLOT (red_verify_plan)
            said = red_verify_await(store, it, f, plan, t)
        else:
            store.update_item("red", it["id"], it["version"], {"fix": {**f, "next_run_at": None}})  # no schedule
        it = store.item("red", it["id"])
        d, f = it["data"], it["data"]["fix"]
        if plan and not verify_dispatchable(repo, d.get("workflow")):
            return said  # a listed one goes on to its verify dispatch in this same tick
    if not holds and not d.get("gate") and not f.get("verify_dispatched") and t - float(f.get("at") or t) > RED_FIX_RUN_START \
            and d.get("red_kind") in ("outside",) \
            and not (f.get("next_run_at") and not verify_dispatchable(repo, d.get("workflow"))):  # its slot decides it
        line = red_verify_dispatch(o_r, repo, d.get("workflow"), sha, (d.get("last_run") or {}).get("databaseId"))
        if line.startswith("FIX UNVERIFIED: no workflow file"):  # NO RUN CAN EVER PROVE IT: offered now, never parked
            return red_fix_unverifiable(store, it, f"its fix {sha[:10]}: {line[len('FIX UNVERIFIED: '):][:160]}", t)
        if line.startswith("FIX UNVERIFIED:"):  # the failing run's inputs cannot be dispatched again: said so, and the
            line += (f"; the newest decided {d.get('workflow')} run on main containing {sha[:10]} decides it "  # next
                     f"(offered again if none by {red_fix_giveup(d) // 3600} h after it landed)")  # natural run decides
        store.update_item("red", it["id"], it["version"], {"fix": {**f, "verify_dispatched": ts(), "verify": line}})
        return [line]
    if t - red_fix_since(it) >= red_fix_giveup(d):
        return red_fix_unverifiable(store, it, f"its fix {sha[:10] or f.get('entry')} unverified for "
                                               f"{fmt_age(t - red_fix_since(it))} after it landed (no run of "
                                               f"{d.get('workflow')} proves it)", t)
    if t >= it["deadline"] and f.get("verify_by") and not holds:
        return red_verify_scheduled(store, it, f, sha, o_r, repo, t)
    if t >= it["deadline"]:
        # awaiting its workflow's next run: no one's to act on
        awaiting = red_fix_giveup(d) > env_num("AGENTCTL_RED_FIX_GIVEUP", RED_FIX_GIVEUP)
        red_move(store, it, "fix-pending", f"RED {red_what(d)}: fix {sha[:10] or f.get('entry')} unverified for "
                                           f"{fmt_age(t - it['entered_at'])}"
                 + (f" (awaiting {d.get('workflow')}'s next run)" if awaiting else ""),
                 severity="info" if awaiting else "escalate")
        return [f"{'re-deadlined' if awaiting else 'escalated'} {red_what(d)}: fix unverified"]
    return []


def red_verify_scheduled(store, it, f, sha, o_r, repo, t):
    """A scheduled fix (red_verify_plan) at its deadline with no decided run containing it: a run of its workflow
    still undecided that will decide it holds it again, quietly; else the slot passed without verifying it --
    escalated (the scheduler never dispatched it, or its run ended without deciding it)."""
    d, wf = it["data"], it["data"].get("workflow")
    r = red_verify_run_on_way(o_r, repo, f)
    if r:
        line = (f"{red_what(d)}: fix {sha[:10]} awaits {wf} #{r.get('number')} ({r.get('status')}), the run that "
                f"decides it")
        red_move(store, it, "fix-pending", line, data={"fix": {**f, "verify_by": t + RED_VERIFY_HOLD}},
                 deadline_s=RED_VERIFY_HOLD, severity="info")
        return [line]
    since = [r for r in workflow_main_runs(o_r, f.get("verify_file")) if f.get("verify_file")
             and r.get("createdAt") and parse_time(r["createdAt"]).timestamp() >= float(f.get("next_run_at") or 0) - 60]
    why = (f"its {hhmmz(f['next_run_at'])} run #{since[-1].get('number')} ended {since[-1].get('conclusion')} "
           f"without deciding it" if since else f"no {wf} run was dispatched for its {hhmmz(f['next_run_at'])} slot")
    red_move(store, it, "fix-pending", f"RED {red_what(d)}: fix {sha[:10]} unverified: {why}", severity="escalate")
    return [f"escalated {red_what(d)}: fix unverified ({why})"]


def red_verify_await(store, it, f, plan, t):
    """Into the quiet wait for a scheduled fix's run: its deadline is the plan's `by`, its level back to 0 (a landing
    is no escalation)."""
    d = it["data"]
    fix = {**f, "next_run_at": plan["slot"], "verify_by": plan["by"], "verify_file": plan["file"]}
    line = (f"{red_what(d)}: fix {str(f.get('sha'))[:10]} awaiting next scheduled run at {hhmmz(plan['slot'])} "
            f"(verified by {hhmmz(plan['by'])})")
    red_move(store, it, "fix-pending", line, data={"fix": fix}, deadline_s=max(60.0, plan["by"] - t), severity="info",
             **({"level": 0} if it.get("level") else {}))
    return [line]


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
    if workflow_crons(txt) and not verify_dispatchable(repo, workflow):  # A VERIFY DISPATCH IS PER WORKFLOW
        return (f"FIX UNVERIFIED until {workflow}'s next scheduled run (scheduled, and {RED_WORKFLOWS_FILE} does not "
                f"say verify_dispatch yes: never dispatched to verify)")
    inputs = (run_dispatch_inputs(o_r, run_id) or run_title_inputs(o_r, run_id, txt)) if run_id else {}
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


RUN_TITLE_INPUT = re.compile(r"(?<!\S)([A-Za-z_][\w-]*)=(\S*)")


def run_title_inputs(o_r, run_id, workflow_text):
    """The dispatch inputs a run that agentctl did not start carries in its title: the workflow's `run-name` writes them
    as `name=value` (`Verify scrape recording recording=<id> countries=<cc,...>`), and only names the workflow declares
    as an input are taken. {} when its title names none or the run cannot be read. 2026-11-11, Verify scrape recording
    #23: dispatched by Record scrape fixtures, its verify went out without the required `recording` (HTTP 422)."""
    try:
        title = gh_json(o_r, "run", "view", str(run_id), "--json", "displayTitle").get("displayTitle") or ""
    except (Exception, SystemExit):
        return {}
    return {k: v for k, v in RUN_TITLE_INPUT.findall(title)
            if re.search(rf"^\s+{re.escape(k)}:\s*$", workflow_text or "", re.M)}


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


# A JOINED RED SHOWS PROGRESS OR GOES ON (the owner, 2026-11-08). A red joined to its family holder's lease (the
# dispatcher's join: `joined_at` on the lease) waits on a session that may be deep in its own red. With no progress on
# THAT red within RED_JOIN_IDLE of joining -- a commit carrying its `Fixes-Red:` in the holder's worktree, a fix on its
# way (the red's `fix`, or the red in fix-pending), a note on it (`red claim <it> --note` inside the fixer) -- it is
# RELEASED (no attempt counted) and offered again, never joined to that holder again (`family_released`).
RED_JOIN_IDLE = 30 * 60  # s a red joined to its family holder's lease may go with no progress on it before it is released


def red_joined_progress(store, row, why):
    """Mark a joined red's lease as worked on (why: `note: ...`, `commit <sha>`, `fix <entry>`): the backstop is done."""
    store.set_meta(row["name"], row["epoch"], row["secret"], {"progressed": str(why)[:200]})


def red_fixes_red_commits(wt, names):
    """The first commit in worktree wt not on any remote whose `Fixes-Red:` names one of names (runs, the key), or None."""
    if not wt or not os.path.isdir(wt) or not allowed_path(wt):
        return None
    out = git(wt, "log", "HEAD", "--not", "--remotes", "-n", "200",
              "--format=%H%x09%(trailers:key=Fixes-Red,valueonly,separator=%x2C)", check=False).stdout
    want = {str(n).lstrip("#") for n in names if n}
    for line in out.splitlines():
        sha, _tab, vals = line.partition("\t")
        if want & trailer_values(vals):
            return sha
    return None


def red_joined_idle(store, row, t):
    """None while a joined red is within its window or worked on (marked so), else the release outcome."""
    m = lease_meta(row)
    if not m.get("joined_at") or m.get("progressed"):
        return None
    it = store.item("red", str(row.get("item") or "").split(":", 1)[-1]) if row.get("item") else None
    d = (it or {}).get("data") or {}
    if it and (it["state"] in ("fix-pending",) + RED_TERMINAL or d.get("fix")):
        red_joined_progress(store, row, f"fix {(d.get('fix') or {}).get('entry') or it['state']}")
        return None
    sha = red_fixes_red_commits(m.get("worktree"), red_names(d)) if it else None
    if sha:
        red_joined_progress(store, row, f"commit {sha[:10]}")
        return None
    idle = env_num("AGENTCTL_RED_JOIN_IDLE", RED_JOIN_IDLE)
    if t - float(m["joined_at"]) < idle:
        return None
    if it:
        try:
            store.update_item("red", it["id"], it["version"],
                              {"family_released": sorted(set(d.get("family_released") or []) | {row["holder"]})})
        except StaleVersion:
            return None  # another writer moved the red: the next pass
    return (f"RELEASED: no progress on it {fmt_age(t - float(m['joined_at']))} after it joined {row['holder']}'s lease "
            f"(no commit, land or note for it within {fmt_age(idle)}): offered to a fixer of its own")


def red_renew_holders(store, t):
    """Each live interactive claim renewed by its worktree's activity (no activity: it lapses at its TTL); each sibling
    exactly as long as its run's fixer's own lease; a joined red released when it shows no progress (red_joined_idle)."""
    for row in store.list("red/"):
        if not lease_is_live(row, t):
            continue
        m = lease_meta(row)
        why = red_joined_idle(store, row, t) if m.get("kind") in ("claim", "sibling") else None
        if why:
            store.revoke(row["name"], why)
            continue
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
    """The live primary lease on a red of queue family fam, or None: a fixer's own (not a sibling), or a CLAIM made
    inside a dispatched fixer (`red claim <M>` there: its record on the lease, 2026-11-07 -- the dispatcher started a
    second owner session beside the consolidated non-gate fixer). An interactive session's claim takes ONLY its red,
    never its family (the owner, 2026-11-08): family holding is for the sessions agentctl itself starts."""
    if not fam:
        return None
    active = active if active is not None else red_active(store)
    for row in store.list("red/"):
        m = lease_meta(row)
        if not lease_is_live(row, t) or not (m.get("kind") == "fixer" or (m.get("kind") == "claim" and m.get("fixer"))):
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
        if repo_dispatch_off(store, red_repo_of(pic)):  # its repo's dispatch is off (the owner's): never dispatched
            store.offer_reason(off["id"], f"dispatch off for {red_repo_of(pic)} (owner)")
            continue
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
                              meta={"kind": "owned", "family": fam, "owner": owner_lease(fam), "host": socket_host(),
                                    "worktree": lease_meta(live).get("worktree"), "gate": bool(pic.get("gate"))})
            if took and "epoch" in took:
                owner_hand(fam, off["lease"], took)
                red_family_tell(live, pic, fam)
                acts.append(f"{pic.get('workflow')} #{(pic.get('run') or {}).get('number')}: red {pic['key']} handed "
                            f"to its family {fam}'s owner")
            continue
        holder = red_family_holder(store, fam, t) if off["tier"] not in ("revert", "lander", "blame") else None
        item = store.item("red", off["item"].split(":", 1)[1]) or {"data": {}}
        if holder and holder["holder"] in (item["data"].get("family_released") or []):
            holder = None  # released from that holder for no progress (red_joined_idle): a fixer of its own, never back
        if holder and holder["name"] != off["lease"]:
            # ONE FIXER PER FAMILY: a new red of a family whose fixer is at work is that fixer's -- under its lease; an
            # owned family's too while its owner is not running (never a second session beside a live holder). A
            # claim's red is a claim of the same worktree (renewed by its activity, red_renew_holders), a fixer's a sibling;
            # the holder's note is about its own red, never the joined one's
            claim = lease_meta(holder).get("kind") == "claim"
            took = store.take(off["id"], holder["holder"], *((RED_CLAIM_TTL, RED_CLAIM_MAX) if claim else
                                                             (RED_FIXER_TTL, RED_FIXER_MAX)),
                              meta={**lease_meta(holder), "family": fam, "outcome": None, "note": None, "notes": None,
                                    "gate": bool(pic.get("gate")), "joined_at": t, "progressed": None,
                                    **({} if claim else {"kind": "sibling", "primary": holder["name"]})})
            if took and "epoch" in took:
                red_family_tell(holder, pic, fam)
                acts.append(f"{pic.get('workflow')} #{(pic.get('run') or {}).get('number')}: red {pic['key']} joined "
                            f"its family {fam}'s fixer {holder['holder']}")
            continue
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
        if gap:  # never a launch claude refuses: said on its offer, dispatched once trusted -- the owner was asked
            why = f"NOT DISPATCHABLE: {gap}"  # when the repo was registered (fixer.py trust_asks), never once per offer
            if why != off.get("reason"):
                store.offer_reason(off["id"], why)
            continue
        num = (pic.get("run") or {}).get("number")
        holder_id = owner_holder(fam) if owned else f"laptop:fixer:red-{pic['key']}-{int(pic.get('attempts') or 0) + 1}"
        meta = red_dispatch_meta(pic, fam, owned, off, num)
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


def red_dispatch_meta(pic, fam, owned, off, num):
    """The lease meta the dispatcher takes a red's offer with; `host`: the machine its wrapper runs on, the only one
    that may judge its pid (holder_work)."""
    return {"kind": "owned" if owned else "fixer", "gate": bool(pic.get("gate")), "family": fam,
            "tier": off["tier"], "run": num, "workflow": pic.get("workflow"), "host": socket_host()}


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
                        extra=extra, store=store)


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


# FIX REPLAY (2026-11-19, Alert backtest #1-#5, 5254bffda9d2): seven fixes landed and were refuted, one per run of the
# backtest -- each fixer landed a guess the nightly then refuted, though the backtest replays cheaply over ci-runner's
# Prometheus snapshot. A workflow whose RED_WORKFLOWS_FILE row names a `replay` command (`{run}`: the red's failing run
# id) holds its fixers to it: a `Fixes-Red:` landing for such a red must carry, in one of its commits, the trailer
# `Fix-Replay: <that command> base=FAIL head=PASS` -- the replay run at origin/main and at the fix -- or `land` refuses
# it, naming the command.
FIX_REPLAY_TRAILER = re.compile(r"(?i)^Fix-Replay:\s*(.+?)\s+base=(FAIL|PASS)\s+head=(FAIL|PASS)\s*$")


def red_replay_command(repo, d):
    """The replay command for red data d (its run id in place of `{run}`), or None."""
    cmd = red_workflow_policy(repo, d.get("workflow"))["replay"]
    run = (d.get("last_run") or {}).get("databaseId")
    return cmd.replace("{run}", str(run)) if cmd and run else cmd


def fix_replay_refusal(repo, commits, store=None):
    """Why `land` refuses commits (a Fixes-Red: for a red with a replay, and no `Fix-Replay: ... base=FAIL head=PASS`
    naming it), or None. An unread store refuses nothing (the red's own verification still judges); a registry missing
    from main refuses a red of a repo that keeps one (red-repos.json `red_workflows`: Alert backtest #7's fix landed
    unreplayed because agentctl enforced five hours before the registry reached main)."""
    named, replays = set(), []
    for c in commits or []:
        msg = git(repo, "log", "-1", "--format=%B", c, check=False).stdout
        for ln in commit_trailers(msg):
            k, _, v = ln.partition(":")
            if k.strip().lower() == "fixes-red":
                named |= trailer_values(v)
            m = FIX_REPLAY_TRAILER.match(ln)
            if m:
                replays.append((m.group(1).strip(), m.group(2).upper(), m.group(3).upper()))
    if not named:
        return None
    try:
        with no_vault():  # THE LAPTOP'S LAND NEEDS NO VAULT: never `secrets` for this read (no token: no refusal)
            items = [it for it in (store or Store()).items("red") if it["data"].get("key")
                     and it["state"] not in RED_TERMINAL]
    except (Exception, SystemExit):
        return None
    for it in items:
        d = it["data"]
        if not named & {str(x) for x in red_names(d) if x is not None}:
            continue
        if not red_workflows_on_main(repo) and (red_repo_conf(red_repo_of(d)) or {}).get("red_workflows"):
            return (f"{RED_WORKFLOWS_FILE} is missing on {remote()}/main, though red-repos.json says "
                    f"{red_repo_of(d)} keeps one: no Fixes-Red lands for {red_what(d)} until it is back (its rows say "
                    "which reds need a `Fix-Replay:`)")
        cmd = red_replay_command(repo, d)
        if not cmd:
            continue
        stem = red_workflow_policy(repo, d.get("workflow"))["replay"].split("{run}")[0].strip()
        ok = [r for r in replays if r[0].startswith(stem)]
        if not any(r[1:] == ("FAIL", "PASS") for r in ok):
            seen = f" (this push says base={ok[0][1]} head={ok[0][2]})" if ok else ""
            return (f"{red_what(d)} has a replay: run `{cmd}` at origin/main and at your fix, see base=FAIL head=PASS, "
                    f"and record it as the trailer `Fix-Replay: {cmd} base=FAIL head=PASS`{seen}")
    return None
