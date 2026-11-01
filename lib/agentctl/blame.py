# agentctl, part `blame`: who landed what, the change that turned a gate red, its revert, and its lander revived.
# Runs inside agentctl's one namespace, after the parts before it in lib/agentctl/__init__.py's PARTS (it uses their names).

# ---------------------------------------------------------------- blame (the owner, 2026-11-01)
# "change agentctl to: remember which agent landed what change, when a build fails: revert the offending change and
# revive the landing agent to fix it" -- and "only if it can't be determined spinup a new agent".
#
# THE DECISION RULE (one place: blame_decision): a gate red whose offending change AND its lander are determined goes
# back to that lander -- the change is reverted and the lander's own Claude session is revived to fix it. A fresh fixer
# is spun up ONLY when that cannot be determined: no single culprit (the parent fails the same check too, the bisect is
# inconclusive or found no verdict), no recorded lander, or the lander's session cannot be resumed.
#
# 1. THE LANDER. Every `agentctl land` records who ran it (lander_record): the Claude session ($CLAUDE_CODE_SESSION_ID),
#    the subagent when the land ran inside one (its transcript under <session>/subagents/; the session id alone would
#    name the orchestrator), the transcript, the worktree, the branch, the cwd -- and each commit's KEYS (sha, patch-id,
#    subject, author time: a landing rebases, so a commit on main is found by its patch-id or its subject and time,
#    never by its sha alone). Queued changes carry it in their change item; a direct land (no queue) writes a `lander`
#    item. Both live in the reconciler's store, for 14 days, across restarts and batches; once a change is landed the
#    tick notes its commits on main (`landed_commits`: main sha -> patch-id).
# 2. THE CULPRIT. A gate test red's range is (the newest green run's head, the first red run's head]; its commits are
#    grouped into changes by the registry (a commit no change carries is the owner's, by hand). The search keeps an
#    invariant -- boundary lo PASSES the same check, boundary hi FAILS it -- and a change is the culprit only when its
#    parent passes and its end fails. Boundary 0 (the green head) passes for free only for an unseeded check (Main's
#    green run there IS the same check on the same inputs); convergence drift seeds rotate per run, so it is re-run with
#    the red run's own seeds (`scripts/identity-drift-ci <parent> --for-red <run>`). Between boundaries: a decided run of
#    the workflow on exactly that commit, else a probe on CI, never on the laptop (drift: identity-drift-ci; Main:
#    precommit-check.yml on a `check/blame-*` branch holding the boundary's tree, its pass counted only when its log
#    names the failing test). The failing test's files hint which boundary to probe first; at most BLAME_PROBES probes.
#    Infra-shaped reds, flakes, outside-the-gate reds and a bot's change (Dependabot, Renovate, Scala Steward) are never
#    blamed or reverted.
# 3. THE REVERT (tier `lander`): one `git revert` of the change's commits with `Reverts-Change: <id>` and
#    `Fixes-Red: <run>`, landed by `agentctl land` (a gate fix: it lands on red and is never throttled). It does not
#    clear the red (FIX FAILED): the culprit was wrong -- the revert is reverted (`Relands-Change: <id>`) and the red goes
#    to the normal fixer.
# 4. THE LANDER REVIVED: `claude --bg --resume <session>` in its own worktree (a subagent: a session in its worktree
#    briefed with its transcript), told the red run, the failing step and lines, that its change was reverted, and to
#    fix, rebase, re-apply its change with the fix and land again. It holds the red's lease (kind `lander`), which the
#    revert's own fix never takes from it.
BLAME_MAX = 90 * 60              # s a red may wait for its culprit to be decided before a fresh fixer gets it ($AGENTCTL_BLAME_MAX)
BLAME_PROBES = 4                 # CI probes one red's culprit search may start ($AGENTCTL_BLAME_PROBES)
BLAME_PROBE_WALL = 2 * 3600      # s one probe may run before it is no verdict ($AGENTCTL_BLAME_PROBE_WALL)
BLAME_RANGE_MAX = 200            # commits a red's range may hold to be searched at all
CONVERGENCE_FLOW = "Identity model convergence"
DRIFT_JOB = re.compile(r"^takes-drift-\d+ \(")
BOT_AUTHOR = re.compile(r"dependabot|renovate|scala-steward|github-actions\[bot\]", re.I)
TEST_TOKEN = re.compile(r"\b([A-Z][A-Za-z0-9]+(?:Spec|Test|Tests|Suite|IT))\b")


# -- 2. the culprit
def blame_hint(repo, groups, d):
    """The one change whose files the failing test or step names (its Spec/Test class, a path in its first line), or
    None: the boundary the search probes first."""
    line = " ".join([str(d.get("line") or "")] + [str(j) for j in d.get("jobs") or []])
    toks = set(TEST_TOKEN.findall(line)) | {os.path.basename(p) for p in PATHISH.findall(line)}
    toks = {t for t in toks if len(t) > 3}
    if not toks:
        return None
    hit = []
    for i, g in enumerate(groups):
        r = git(repo, "diff-tree", "--no-commit-id", "--name-only", "-r", "--root", *g["commits"], check=False)
        files = r.stdout.split() if r.returncode == 0 else []
        if any(t in f for t in toks for f in files):
            hit.append(i)
    return hit[0] if len(hit) == 1 else None


def blame_kind(o_r, d):
    """How a boundary is judged on CI: `drift` (the convergence drift rows, re-run with the red run's own seeds),
    `checks` (movies: the boundary's tree through precommit-check.yml), `run` (only a decided run of the workflow on that
    very commit), or None (no CI check repeats this red: a convergence leg other than drift, whose pinned pair rotates
    nightly)."""
    jobs = [str(j) for j in d.get("jobs") or [d.get("job")] if j]
    if d.get("workflow") == CONVERGENCE_FLOW:
        return "drift" if jobs and all(DRIFT_JOB.match(j) for j in jobs) else None
    return "checks" if o_r == RED_PRIMARY else "run"


def blame_open(store, repo, o_r, ob, t):
    """The red's culprit search, opened with the red (`blame` in its data): {state: none|probing, ...}."""
    if ob.get("kind") != "test" or ob.get("red_kind") != "gate":
        return {"state": "none", "why": "only a gate test red is blamed (infra, flakes and outside reds never)"}
    if not repo:
        return {"state": "none", "why": "no clone here to read its range"}
    runs = ob.get("runs_list") or []
    decided = [r for r in runs if r.get("status") == "completed" and r.get("conclusion") in ("success",) + RED_CONCLUSIONS]
    streak = streak_runs(runs)
    if not streak or len(decided) <= len(streak) or decided[len(streak)].get("conclusion") != "success":
        return {"state": "none", "why": "no green run before the red streak"}
    first, green = streak[-1], decided[len(streak)]
    base, head = green.get("headSha"), first.get("headSha")
    if not base or not head or not git_ok(repo, "cat-file", "-e", f"{head}^{{commit}}"):
        git(repo, "fetch", "--quiet", remote(), "main", check=False)
    if not base or not head or not git_ok(repo, "cat-file", "-e", f"{head}^{{commit}}") \
            or not git_ok(repo, "merge-base", "--is-ancestor", base, head):
        return {"state": "none", "why": f"the range {str(base)[:10]}..{str(head)[:10]} cannot be read here"}
    keys = commit_keys(repo, base, head)
    if not keys:
        return {"state": "none", "why": "no commit between the green run and the red one"}
    if len(keys) > BLAME_RANGE_MAX:
        return {"state": "none", "why": f"{len(keys)} commits in its range"}
    idx = lander_index(lander_records(store, o_r))
    groups = []
    for k in keys:
        rec = lander_of_commit(idx, k)
        cid = rec["change"] if rec else None
        if groups and cid and groups[-1]["change"] == cid:
            groups[-1]["commits"].append(k["sha"])
        else:
            au = git(repo, "log", "-1", "--format=%an <%ae>", k["sha"], check=False).stdout.strip()
            groups.append({"change": cid, "commits": [k["sha"]], "lander": (rec or {}).get("lander"),
                           "worktree": (rec or {}).get("worktree") or ((rec or {}).get("lander") or {}).get("worktree"),
                           "author": au, "bot": bool(BOT_AUTHOR.search(au)), "subject": k.get("subject")})
        groups[-1]["end"] = k["sha"]
    return {"state": "probing", "base": base, "head": head, "groups": groups, "lo": None, "hi": len(groups),
            "probes": [], "kind": blame_kind(o_r, ob), "since": t,
            "runs": [{k: r.get(k) for k in ("number", "headSha", "status", "conclusion")} for r in decided[:30]],
            "run": {"number": first.get("number"), "databaseId": first.get("databaseId")},
            "hint": blame_hint(repo, groups, ob)}


def blame_boundary(b, i):
    return b["base"] if i == 0 else b["groups"][i - 1]["end"]


def blame_next(b):
    """The boundary to judge next, or None when lo and hi are adjacent (decided)."""
    lo, hi = b.get("lo"), b["hi"]
    if lo is None:
        return 0
    if hi - lo <= 1:
        return None
    k = b.get("hint")
    if k is not None and lo < k < hi:  # the hinted change's parent
        return k
    if k is not None and k == lo and k + 1 < hi:  # ... and its end
        return k + 1
    return (lo + hi) // 2


def blame_decision(b):
    """THE DECISION RULE: (who, why). who: `lander` -- the culprit and its lander are determined: revert it and revive
    the lander; `owner` -- the culprit is determined and nobody's session landed it (the owner by hand, a land outside
    Claude): revert it and tell the owner; `fixer` -- it cannot be determined: a fresh fixer; None -- still deciding."""
    st = (b or {}).get("state")
    if st == "probing":
        return None, "deciding the culprit"
    if st != "confirmed":
        return "fixer", (b or {}).get("why") or "no culprit"
    g = b["groups"][b["culprit"]]
    if not g.get("change") or not (g.get("lander") or {}).get("session"):
        return "owner", f"{blame_change_name(g)} was landed by hand (no agent session recorded)"
    return "lander", f"{blame_change_name(g)} landed by {lander_name(g['lander'])}"


def blame_waiting(b):
    """Why a `blame` offer is not taken yet: what the search is judging."""
    b = b or {}
    p = b.get("probe") or {}
    judged = len(b.get("probes") or [])
    what = (f"probing {str(p.get('commit'))[:10]} ({p.get('kind')}) on CI" if p else "reading its range")
    return (f"deciding the culprit among {len(b.get('groups') or [])} change(s): {what}; {judged} boundary(ies) judged "
            f"(at most {fmt_age(env_num('AGENTCTL_BLAME_MAX', BLAME_MAX))}, then a fixer)")


def blame_change_name(g):
    return f"change {g['change']}" if g.get("change") else f"commit {g['commits'][0][:10]}"


def lander_name(l):
    l = l or {}
    who = f"subagent {l['agent']} of session {str(l.get('session'))[:8]}" if l.get("agent") else \
        f"session {str(l.get('session'))[:8]}" if l.get("session") else "the owner"
    return f"{who} ({str(l.get('worktree') or '?').replace(HOME, '~')})"


def blame_free(o_r, d, b, i, runs):
    """A boundary's verdict that costs nothing: a decided run of the red's own (unseeded) workflow on exactly that commit
    -- pass on success; None for a drift check (its seeds rotate: never the same inputs) and when no such run exists."""
    if b.get("kind") == "drift":
        return None
    sha = blame_boundary(b, i)
    for r in runs or []:
        if r.get("headSha") == sha and r.get("status") == "completed" and r.get("conclusion") == "success":
            return {"verdict": "pass", "evidence": f"{d.get('workflow')} #{r.get('number')} green on {sha[:10]}"}
    return None


def blame_apply(b, i, v):
    """A boundary's verdict into the search: (b, None) to go on, or (b, why) when the search ends undetermined."""
    b = dict(b, probes=(b.get("probes") or []) + [{"boundary": i, "commit": blame_boundary(b, i), **v}], probe=None)
    if v["verdict"] == "none":
        return b, f"no verdict on {blame_boundary(b, i)[:10]} ({v.get('evidence') or '?'})"
    if i == 0:
        if v["verdict"] == "fail":
            return b, (f"pre-existing: the parent {b['base'][:10]} fails the same check "
                       f"({v.get('evidence') or ''}); no change of its range is the culprit")
        b["lo"] = 0
    elif v["verdict"] == "pass":
        b["lo"] = i
    else:
        b["hi"] = i
    return b, None


def blame_finish(store, it, b, why, t):
    """The search decided: confirmed (lo and hi adjacent), or undetermined with why. The red is offered again with the
    tier the decision gives (red_offer). Returns the act lines."""
    d = it["data"]
    if why is None:
        k = b["hi"] - 1
        g = b["groups"][k]
        if g.get("bot"):
            why = f"the culprit {g['commits'][0][:10]} is a bot's change ({g.get('author')}): never reverted"
        else:
            b = dict(b, state="confirmed", culprit=k, parent=blame_boundary(b, k), decided_at=t,
                     evidence="; ".join(str(p.get("evidence") or "") for p in b.get("probes") or [] if p.get("evidence")))
            who, text = blame_decision(b)
            line = (f"RED BLAME: {red_what(d)}: the culprit is {blame_change_name(g)} "
                    f"({', '.join(c[:10] for c in g['commits'][:5])}, {g.get('subject') or ''!s:.80}), {text}: its parent "
                    f"{b['parent'][:10]} passes the same check, its end fails ({b['evidence'][:200]})")
            inbox_write("red", line, [line], [], 0)
            dispatch_log({"workflow": d.get("workflow"), "run": d.get("last_run") or {}, "job": d.get("job")}, line)
            red_offer(store, it, line, data={"blame": b})
            return [line]
    b = dict(b, state="undetermined", why=why, decided_at=t)
    line = f"RED BLAME: {red_what(d)}: no culprit determined ({why}): the normal fixer flow"
    dispatch_log({"workflow": d.get("workflow"), "run": d.get("last_run") or {}, "job": d.get("job")}, line)
    red_offer(store, it, line, data={"blame": b})
    return [line]


def blame_step(store, it, repo, o_r, t):
    """One step of a probing red's culprit search (the red tick, before its offer step). Returns act lines, or None when
    the red is not probing."""
    d = it["data"]
    b = d.get("blame") or {}
    if b.get("state") != "probing":
        return None
    if t - float(b.get("since") or t) > env_num("AGENTCTL_BLAME_MAX", BLAME_MAX):
        blame_probe_stop(b.get("probe"))
        return blame_finish(store, it, b, f"no decision in {fmt_age(t - float(b['since']))}", t)
    p = b.get("probe")
    if p:
        v = blame_probe_result(p, t)
        if v is None:
            return []
        b, why = blame_apply(b, p["boundary"], v)
        if why:
            return blame_finish(store, it, b, why, t)
    while True:
        i = blame_next(b)
        if i is None:
            return blame_finish(store, it, b, None, t)
        v = blame_free(o_r, d, b, i, b.get("runs"))
        if v:
            b, why = blame_apply(b, i, v)
            if why:
                return blame_finish(store, it, b, why, t)
            continue
        started = len([x for x in b.get("probes") or [] if x.get("ci")])
        if started >= env_num("AGENTCTL_BLAME_PROBES", BLAME_PROBES):
            return blame_finish(store, it, b, f"inconclusive within {started} CI probes", t)
        if b.get("kind") not in ("drift", "checks"):
            why = ("no CI check repeats this red on another commit (a convergence leg's pinned pair rotates nightly)"
                   if b.get("kind") is None else f"no decided {d.get('workflow')} run on {blame_boundary(b, i)[:10]}")
            return blame_finish(store, it, b, why, t)
        p = blame_probe_start(repo, o_r, d, b, i)
        b = dict(b, probe=p)  # counted among the CI probes when its verdict lands (ci: True)
        store.update_item("red", it["id"], it["version"], {"blame": b})
        line = (f"RED BLAME: {red_what(d)}: probing {blame_boundary(b, i)[:10]} (boundary {i} of {len(b['groups'])}, "
                f"{b['kind']}) on CI")
        dispatch_log({"workflow": d.get("workflow"), "run": d.get("last_run") or {}, "job": d.get("job")}, line)
        return [line]


# -- the probes: a detached `agentctl red probe` per boundary, its verdict in a JSON file
def blame_probe_start(repo, o_r, d, b, i):
    num = (b.get("run") or {}).get("number")
    out = reds_dir(red_repo_tag(o_r, num), f"blame-{i}-{os.urandom(2).hex()}.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    argv = [sys.executable, TOOL_PATH, "red", "probe", "--kind", b["kind"], "--repo", repo, "--commit",
            blame_boundary(b, i), "--run", str(num), "--out", out, "--tokens", " ".join(blame_tokens(d))]
    with open(out[:-5] + ".log", "a") as logf:
        p = spawn_detached(argv, cwd=repo, stdin=subprocess.DEVNULL, stdout=logf, stderr=logf, start_new_session=True)
    return {"boundary": i, "commit": blame_boundary(b, i), "out": out, "pid": p.pid, "start": process_start(p.pid),
            "at": time.time(), "kind": b["kind"]}


def blame_tokens(d):
    line = " ".join([str(d.get("line") or "")])
    return sorted(set(TEST_TOKEN.findall(line)) | {os.path.basename(p) for p in PATHISH.findall(line)})


def blame_probe_result(p, t):
    """The probe's verdict ({verdict: pass|fail|none, evidence, ci: True}), or None while it runs."""
    rec = read_record(p.get("out"))
    if rec and rec.get("verdict"):
        return {**rec, "ci": True}
    if p.get("pid") and proc_alive(p["pid"], p.get("start")):
        if t - float(p.get("at") or t) > env_num("AGENTCTL_BLAME_PROBE_WALL", BLAME_PROBE_WALL) + 300:
            blame_probe_stop(p)
            return {"verdict": "none", "evidence": "the probe ran past its wall time", "ci": True}
        return None
    return {"verdict": "none", "evidence": "the probe ended with no verdict (see its .log)", "ci": True}


def blame_probe_stop(p):
    if p and p.get("pid") and int(p["pid"]) != os.getpid() and proc_alive(p["pid"], p.get("start")):
        try:
            os.killpg(int(p["pid"]), signal.SIGTERM)
        except OSError:
            pass


def drift_ci_path(repo):
    return os.environ.get("AGENTCTL_DRIFT_CI") or os.path.join(repo, "scripts", "identity-drift-ci")


def probe_drift(repo, commit, run, wall):
    """The red run's failing drift rows replayed on `commit` with THE RED RUN'S OWN SEEDS (identity-drift-ci
    --for-red): DRIFT CLEAN is a pass, DRIFT RED or DRIFT PROGRESS (fewer flips, still some) a fail, anything else no
    verdict."""
    try:
        r = subprocess.run([drift_ci_path(repo), commit, "--for-red", str(run), "--timeout", str(int(wall // 60))],
                           cwd=repo, capture_output=True, text=True, timeout=wall + 120, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as e:
        return {"verdict": "none", "evidence": f"identity-drift-ci: {str(e)[:160]}"}
    last = (r.stdout.strip().splitlines() or [""])[-1].strip()
    v = "pass" if last.startswith("DRIFT CLEAN") else "fail" if last.startswith(("DRIFT RED", "DRIFT PROGRESS")) else "none"
    return {"verdict": v, "evidence": f"identity-drift-ci {commit[:10]} --for-red {run}: {last[:200] or f'exit {r.returncode}'}"}


def probe_checks(repo, o_r, commit, run, tokens, wall):
    """The boundary's TREE through precommit-check.yml on CI: a commit holding it on top of <remote>/main pushed as
    `check/blame-<run>-<sha>` (the change's paths -- everything after the boundary, undone -- select the checks, as
    the known-red baseline does). A pass counts only when the run's log names the failing test (else the check never
    ran it: no verdict); a failure is a fail (the parent fails: never reverted on it)."""
    git(repo, "fetch", "--quiet", remote(), "main", check=False)
    tree = git(repo, "rev-parse", f"{commit}^{{tree}}", check=False).stdout.strip()
    if not tree:
        return {"verdict": "none", "evidence": f"{commit[:10]} unknown here"}
    tip = git(repo, "commit-tree", tree, "-p", f"{remote()}/main", "-m",
              f"blame probe: the tree of {commit} (red run {run})", check=False).stdout.strip()
    branch = f"check/blame-{run}-{commit[:10]}"
    r = git(repo, "push", "--quiet", "--force", remote(), f"{tip}:refs/heads/{branch}", check=False)
    if r.returncode != 0:
        return {"verdict": "none", "evidence": f"push of {branch} failed: {r.stderr.strip()[:200]}"}
    rem = {"branch": branch, "tip": tip, "run": None, "status": None}
    t0 = time.time()
    try:
        for delay in backoff(30, 300):
            try:
                rem = remote_poll(o_r, rem)
            except (Exception, SystemExit) as e:
                err(f"blame probe {branch}: {str(e)[:160]}")
            if rem.get("status") or time.time() - t0 > wall:
                break
            time.sleep(delay)
    finally:
        delete_check_branch(o_r, branch)
    st = rem.get("status")
    name = f"{REMOTE_WORKFLOW} run {rem.get('run')} on the tree of {commit[:10]}"
    if st == "failure":
        return {"verdict": "fail", "evidence": f"{name}: failure"}
    if st != "success":
        return {"verdict": "none", "evidence": f"{name}: {st or 'undecided'}"}
    if not tokens:
        return {"verdict": "none", "evidence": f"{name}: success, but the red names no test to look for in its log"}
    try:
        log = subprocess.run(forge_argv(o_r, "run", "view", str(rem["run"]), "--log"), capture_output=True, text=True,
                             env=forge_env(o_r), timeout=GH_JSON_TIMEOUT).stdout
    except (OSError, subprocess.TimeoutExpired):
        log = ""
    seen = [t for t in tokens if t in log]
    if not seen:
        return {"verdict": "none", "evidence": f"{name}: success, but it never ran {', '.join(tokens)}"}
    return {"verdict": "pass", "evidence": f"{name}: success, ran {', '.join(seen)}"}


def cmd_red_probe(a):
    """`agentctl red probe`: one boundary's verdict on CI, written to --out (the red tick reads it)."""
    wall = env_num("AGENTCTL_BLAME_PROBE_WALL", BLAME_PROBE_WALL)
    try:
        if a.kind == "drift":
            v = probe_drift(a.repo, a.commit, a.run, wall)
        elif a.kind == "checks":
            v = probe_checks(a.repo, slug(a.repo), a.commit, a.run, [t for t in (a.tokens or "").split() if t], wall)
        else:
            v = {"verdict": "none", "evidence": f"no probe of kind {a.kind}"}
    except (Exception, SystemExit) as e:
        v = {"verdict": "none", "evidence": f"{type(e).__name__}: {str(e)[:200]}"}
    write_json(a.out, {**v, "at": ts(), "commit": a.commit, "kind": a.kind})
    print(f"{v['verdict']}: {v.get('evidence')}")
    return 0
