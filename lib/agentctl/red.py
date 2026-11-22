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
# unless the repo's ONE per-workflow policy registry, RED_WORKFLOWS_FILE on main, says `verify_dispatch` no (VERIFY
# PROMPTLY BY DEFAULT, 2026-11-21: no row is yes, and the dispatch goes at once); a `no` workflow waits for its slot. A
# workflow with no schedule is dispatched as before (no other run will come). The same row's `replay` is the command a fixer runs before landing (FIX REPLAY, below).
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


# VERIFY PROMPTLY BY DEFAULT (the owner, 2026-11-21: "structural fix for the order independence being abandoned" --
# Order independence #94's fix landed and waited ~24 h for its 02:40Z cron, no one at work): a workflow with no row
# in RED_WORKFLOWS_FILE MAY be dispatched to verify a landed fix -- at once, never after its next slot. A costly one
# opts out with a row, `verify_dispatch no` and why (more than ~60 runner-minutes a run, or a paid service).
def red_workflow_policy(repo, workflow, ref=None):
    return red_workflow_policies(repo, ref).get(workflow) or {"verify_dispatch": True, "replay": None, "why": ""}


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
            # listing: a fix is verified by a run CONTAINING it, the newest 30 read; none there keeps it pending, never refuted
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


# A DECIDED REVERT THE FIXERS DID NOT LAND IS LANDED BY AGENTCTL (the owner, 2026-11-22: "fix this structurally, so
# it recovers by itself"; Identity takes #349: the owner decided "revert f64d8a73a" at ~07:00Z, the fixer's revert was
# rejected at 07:52Z and the gate stayed red, every land waiting behind it). A gate red whose in-progress decision is
# `revert <sha>`, offered again RED_DECIDED_REVERT_MAX after the owner decided it, goes to the revert tier: agentctl's
# own revert of that commit (red_revert: Fixes-Red, a specific `Fix-Replay: none`, landed by `agentctl land` under the
# normal checks), once -- `reverted` set, the ladder goes on as before if it does not land.
RED_DECIDED_REVERT_MAX = 45 * 60
DECIDED_REVERT = re.compile(r"^\W*revert\s+([0-9a-f]{7,40})\b", re.I)


def decided_revert(d, t=None):
    """The commit the owner decided red d is fixed by reverting, once that decision is RED_DECIDED_REVERT_MAX old (t:
    now) and agentctl has not reverted it yet; else None."""
    dec = red_decision(d) or {}
    m = DECIDED_REVERT.match(str(dec.get("answer") or ""))
    if not m or not d.get("gate") or d.get("reverted"):
        return None
    age = (time.time() if t is None else t) - float(dec.get("at") or 0)
    return m.group(1) if age >= env_num("AGENTCTL_RED_DECIDED_REVERT_MAX", RED_DECIDED_REVERT_MAX) else None


def red_tier(d, culprit=None, t=None):
    if (d.get("crash_loop") or {}).get("stage") == "deep":  # the crash-loop breaker moved it up (red_crash_loop)
        return "deep"
    if decided_revert(d, t):
        return "revert"
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
