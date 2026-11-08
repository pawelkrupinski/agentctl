# agentctl, part `landwatch`: land's watch.
# Runs inside agentctl's one namespace, after the parts before it in lib/agentctl/__init__.py's PARTS (it uses their names).

# ---------------------------------------------------------------- land: the watch after LANDED
# The owner (2026-10-26): "whenever an agent pushed to main, make a script watch the corresponding convergence and main
# build and tell the agent if it fails". With no pre-merge CI, main's CI is where a change is tested in full: land keeps
# running after LANDED until Main and the convergence say green or red, so the agent (land in the background) hears it.
# Every kind but `main` is a dispatched suite (pick_convergence_run): the convergence, kicked by Main, and Identity takes
# (the takes rows split out of it, 2026-11-08), dispatched by the convergence's preflight.
WATCH = {"pawelkrupinski/movies": {"main": ("Main", "main.yml"),
                                   "convergence": ("Identity model convergence", "identity-model-convergence.yml"),
                                   "takes": ("Identity takes", "identity-takes.yml")}}
WATCH_TIMEOUT = 3 * 3600          # either run undecided by then: exit 2, could not tell
WATCH_CONV_START = 3600           # no convergence (or takes) run containing the sha started by then: said, not waited for
WATCH_SLACK = 120                 # clock skew between the laptop and GitHub's createdAt
WATCH_STALL_MIN = 120  # a watch with no change in its runs for this long ends WATCH STALLED
UNDECIDED = ("cancelled", "skipped", "neutral", "")


def contains(repo, sha, head):
    """Whether a run's head commit contains sha (fetching main once when head is unknown here)."""
    if not head:
        return False
    if head == sha:
        return True
    if not git_ok(repo, "cat-file", "-e", f"{head}^{{commit}}"):
        git(repo, "fetch", "--quiet", remote(), "main", check=False)
    return git_ok(repo, "merge-base", "--is-ancestor", sha, head)


def is_cancelled(r):
    return r.get("status") == "completed" and (r.get("conclusion") or "") in UNDECIDED


def pick_main_run(repo, sha, runs):
    """Main's run for sha: its own push run, unless that was cancelled or never came (a newer push superseded it); then the
    newest run whose head contains sha."""
    own = next((r for r in runs if r.get("headSha") == sha and not is_cancelled(r)), None)
    if own:
        return own
    later = [r for r in runs if not is_cancelled(r) and contains(repo, sha, r.get("headSha"))]
    return max(later, key=lambda r: r["databaseId"]) if later else None


def pick_convergence_run(repo, sha, runs, pushed_at, o_r=None):
    """The FIRST convergence run on main created after the push whose head contains sha -- this commit's kick, a later
    push's, or the daily run (the owner, 2026-10-26: never hunt the sha's own kick). A cancelled one (the lane replaced
    it) passes to the next, and so does an incomplete one (run_no_verdict: only `verdict complete` failed; o_r given)."""
    after = [r for r in runs if r.get("createdAt") and parse_time(r["createdAt"]).timestamp() >= pushed_at - WATCH_SLACK]
    ok = [r for r in sorted(after, key=lambda r: r["databaseId"]) if not is_cancelled(r) and contains(repo, sha, r.get("headSha"))
          and not (o_r and run_no_verdict(o_r, r))]
    return ok[0] if ok else None


def watch_landed(repo, sha, pushed_at, o_r=None, close=None, fixes=()):
    """Watch Main and the convergence for a landed sha. 0 green (or nothing to watch), 3 red, 2 could not tell. OWNED
    (one watcher per sha; on agentctl's new code it re-execs as `agentctl watch`). fixes: the runs the landed commits'
    `Fixes-Red:` trailers name (the sha's own are read here too): each is verified against the new run (FIX OK / FIX
    FAILED, to the inbox)."""
    o_r = o_r or slug(repo)
    flows = WATCH.get(o_r)
    if not flows:
        print(f"watch: no CI watched for {o_r} (only movies' Main and convergence are)")
        return 0
    pushed_at = pushed_at or time.time()
    try:
        fixes = sorted(set(fixes) | fixes_of(repo, [sha]))
    except Exception:
        fixes = sorted(set(fixes))
    own = Owned("watch", f"{o_r}-{sha}", ["watch", "--repo", repo, "--sha", sha, "--pushed-at", str(pushed_at)]
                + (["--close", close] if close else []) + [x for f in fixes for x in ("--fixes", f)])
    if not own.acquire():
        h = own.holder or {}
        print(f"WATCH UNKNOWN: another agentctl (pid {h.get('pid')}, {h.get('owner')}) already watches {sha[:10]}; "
              "its verdict is its own (`agentctl orphans` lists owners)")
        return 2
    try:
        rc = _watch(repo, sha, pushed_at, o_r, flows, own, fixes)
    finally:
        own.release()
    if rc == 0 and close:
        print(close_worktree(close), flush=True)
    return rc


def failed_jobs(o_r, rid):
    d = gh_json(o_r, "run", "view", str(rid), "--json", "jobs")
    return [j["name"] for j in d.get("jobs") or [] if j.get("conclusion") in ("failure", "timed_out", "cancelled")]


def run_id_of_number(o_r, n):
    """The run id whose run NUMBER (run_number, the #N of every link) is n, or None."""
    try:
        runs = forge_actions_runs(o_r, run_number=n, limit=5)
    except (Exception, SystemExit):
        return None
    hit = next((x for x in runs if str(x.get("run_number")) == str(n)), None)
    return hit and hit.get("id")


def workflow_file_of(repo, sha, name):
    """(path in the tree, text) of the workflow file whose `name:` is `name`, at sha."""
    for d in (".github/workflows",):
        r = git(repo, "ls-tree", "--name-only", sha, f"{d}/", check=False)
        for path in r.stdout.split():
            txt = git(repo, "show", f"{sha}:{path}", check=False).stdout
            if re.search(rf"^name:\s*[\"']?{re.escape(name)}[\"']?\s*$", txt, re.M):
                return path, txt
    return None, ""


def _watch(repo, sha, pushed_at, o_r, flows, own, fixes=()):
    timeout = env_num("AGENTCTL_WATCH_TIMEOUT", WATCH_TIMEOUT)
    conv_start = env_num("AGENTCTL_WATCH_CONV_START", WATCH_CONV_START)
    t0 = time.time()
    err(f"watching Main and the convergence for {sha[:10]} (up to {int(timeout // 60)} min; `--no-watch` skips this)"
        + (f"; its Fixes-Red {', '.join(fixes)} is verified by the red's own item (`agentctl red status`)" if fixes else ""))
    done, said = {}, set()  # kind -> (run, ok | None for none started)
    # NO PROGRESS: nothing about the followed runs (which run, its status, its last update) changed for this long ends
    # the watch with a named verdict (a queue no runner serves, a runner lost mid-run), not a silent 3 h wait
    stall = env_num("AGENTCTL_WATCH_STALL_MIN", WATCH_STALL_MIN) * 60
    state, changed = None, time.time()
    delays = backoff(env_num("AGENTCTL_WATCH_POLL", 60), 300)
    # UNREACHABLE: a failed read backs off (to 5 min) and is retried until the watch's own bound; only then UNKNOWN,
    # saying for how long GitHub could not be read (2026-10-27: ten failures in a row used to end it in minutes)
    unreachable, failures, last_error, err_delays = None, 0, "", None

    def verdict(rc, line):
        print(line, flush=True)
        return rc

    def on_run(kind, run):
        name = flows[kind][0]
        if run is None:
            print(f"{kind}: none started within {int(conv_start // 60)} min of the push", flush=True)
            return
        if (kind, run["databaseId"]) not in said:
            said.add((kind, run["databaseId"]))
            err(f"{name}: following run #{run.get('number')} ({run.get('status')}, head {str(run.get('headSha'))[:10]})")
        if kind in done:
            print(f"{name} #{run.get('number')}: {run.get('conclusion')}", flush=True)

    while True:
        seen = {}
        try:
            seen = watch_look(repo, sha, o_r, flows, done, pushed_at, t0, time.time(), on_run)
            if unreachable is not None:
                err(f"watch: GitHub answers again after {red_for(iso_at(unreachable))} ({failures} failed reads)")
            unreachable, failures, err_delays = None, 0, None
        except NoToken as e:  # no vault for this process: retrying cannot help
            return verdict(2, f"WATCH UNKNOWN: no token ({str(e).replace('agentctl: ', '')}); "
                              f"check `agentctl ci-why` for {sha[:10]}")
        except Exception as e:  # a network blip, GitHub down: read again, backing off, until the bound
            failures += 1
            last_error = str(e)[:160]
            if unreachable is None:
                unreachable, err_delays = time.time(), backoff(env_num("AGENTCTL_WATCH_ERROR_POLL", 30), 300)
            err(f"watch: could not read the runs ({last_error}) [{failures} failed, unreachable for "
                f"{red_for(iso_at(unreachable))}]")
        v = watch_verdict(o_r, sha, flows, done)
        if v and v[0] == "red":
            print(v[1], flush=True)
            try:
                print(ci_why(o_r, 4, repo=repo)[1])
            except (Exception, SystemExit) as e:
                print(f"(agentctl ci-why failed: {e})")
            return 3
        if v:
            return verdict(0, v[1])
        if time.time() - t0 >= timeout:
            left = ", ".join(flows[k][0] for k in flows if k not in done)
            if unreachable is not None:
                return verdict(2, f"WATCH UNKNOWN: GitHub's runs could not be read for {red_for(iso_at(unreachable))} "
                                  f"({failures} failed reads, the last: {last_error}); {left} undecided after "
                                  f"{int(timeout // 60)} min; check `agentctl ci-why` later")
            return verdict(2, f"WATCH UNKNOWN: {left} undecided after {int(timeout // 60)} min; check `agentctl ci-why` "
                              "later")
        if unreachable is None:  # a failed read says nothing about the runs: no stall judged on it
            now_state = (tuple(sorted(done)), tuple(sorted(seen.items())))
            if now_state != state:
                state, changed = now_state, time.time()
            elif time.time() - changed >= stall:
                left = ", ".join(f"{flows[k][0]} {('run ' + str(v[0]) + ' ' + str(v[1])) if v else 'no run'}"
                                 for k, v in [(k, seen.get(k)) for k in flows if k not in done])
                return verdict(2, f"WATCH STALLED: no change in {left} for {int(stall // 60)} min; check `agentctl "
                                  "ci-why` later")
        restart_if_changed([own])
        time.sleep(next(err_delays) if unreachable is not None else next(delays))


def iso_at(t):
    return dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
