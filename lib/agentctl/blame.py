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
#    fix, rebase, re-apply its change with the fix and land again (a revert that conflicts: fix forward). It holds the
#    red's lease (tier `lander`), which the revert's own fix never takes from it.
BLAME_MAX = 90 * 60              # s a red may wait for its culprit to be decided before a fresh fixer gets it ($AGENTCTL_BLAME_MAX)
BLAME_PROBES = 4                 # CI probes one red's culprit search may start ($AGENTCTL_BLAME_PROBES)
BLAME_PROBE_WALL = 2 * 3600      # s one probe may run before it is no verdict ($AGENTCTL_BLAME_PROBE_WALL)
BLAME_RANGE_MAX = 200            # commits a red's range may hold to be searched at all
CONVERGENCE_FLOWS = {"Identity model convergence", "Identity takes"}  # the drift rows moved to Identity takes (2026-11-08)
# A drift row's job name as GitHub lists it: `leg (united-kingdom) / takes-drift-2 (united-kingdom)` (a reusable call's
# inner job, prefixed by its caller), or bare `takes-drift-2 (united-kingdom)` -- read like movies'
# scripts/ci/convergence_reuse.py jobs_by_name (the inner name after ` / `). 2026-11-01: `^takes-drift` missed the
# prefixed name, #629's kind came out None, and #628's green on the parent (OTHER rotating seeds) was taken as a pass.
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


def job_inner(name):
    """A job's own name: the part after ` / ` of GitHub's `<caller> / <job>` (convergence_reuse.py's jobs_by_name)."""
    caller, sep, inner = str(name).partition(" / ")
    return inner if sep else caller


def is_drift_job(name):
    return bool(DRIFT_JOB.match(job_inner(name)))


def blame_kind(o_r, d):
    """How a boundary is judged on CI: `drift` (the convergence drift rows, re-run with the red run's own seeds),
    `checks` (movies: the boundary's tree through precommit-check.yml), `run` (only a decided run of the workflow on that
    very commit), or None (no CI check repeats this red: a convergence leg other than drift, whose pinned pair rotates
    nightly)."""
    jobs = [str(j) for j in d.get("jobs") or [d.get("job")] if j]
    if d.get("workflow") in CONVERGENCE_FLOWS:
        return "drift" if jobs and all(is_drift_job(j) for j in jobs) else None
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
    -- pass on success; None when no such run exists, and ALWAYS None for a convergence red: its drift seeds rotate per
    run (a green run on other seeds is never evidence, #628 for #629) and its other legs' pinned pairs rotate nightly.
    Only a re-run with the red's own seeds forced (probe_drift) judges a convergence boundary."""
    if b.get("kind") not in ("run", "checks") or d.get("workflow") in CONVERGENCE_FLOWS:
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
        why = blame_unproven(d, b)
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
            red_offer(store, it, line, data={"blame": b}, quiet=True)
            return [line]
    b = dict(b, state="undetermined", why=why, decided_at=t)
    line = f"RED BLAME: {red_what(d)}: no culprit determined ({why}): the normal fixer flow"
    dispatch_log({"workflow": d.get("workflow"), "run": d.get("last_run") or {}, "job": d.get("job")}, line)
    red_offer(store, it, line, data={"blame": b}, quiet=True)
    return [line]


def blame_unproven(d, b):
    """Why the search's passing parent (boundary lo) is no proof for this red, or None. A convergence red's parent
    passes ONLY on a drift probe that replayed the red's own seeds, each row's verdict line naming the seed it pinned
    (probe_drift: `seeds: True`); anything else -- a green run on rotating seeds, an unknown kind -- leaves the culprit
    undetermined: the normal fixer, never a revert."""
    if d.get("workflow") not in CONVERGENCE_FLOWS:
        return None
    if b.get("kind") != "drift":
        return "no CI check repeats this red with its own seeds (not a takes-drift row): the culprit is undetermined"
    lo = b.get("lo")
    proof = [p for p in b.get("probes") or [] if p.get("boundary") == lo and p.get("verdict") == "pass"]
    if not any(p.get("ci") and p.get("kind") == "drift" and p.get("seeds") is True for p in proof):
        return (f"the parent {str(blame_boundary(b, lo or 0))[:10]} was never re-run with the red's own seeds "
                f"({'; '.join(str(p.get('evidence') or '') for p in proof)[:160] or 'no pass'}): no evidence for a culprit")
    return None


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


DRIFT_OVERLAY = (".github", "scripts/ci")  # main's CI laid over an old commit, so its drift rows honour forced seeds
DRIFT_PINS = re.compile(r"seeds (\d*,\d*) \(the red's own\)")
DRIFT_ROW = re.compile(r"^takes-drift-(\d+) \([^)]*\) \[seed ([^\]]*)\]:", re.M)


def drift_overlay(repo, commit):
    """A commit holding `commit`'s code with <remote>/main's .github/ and scripts/ci/ laid over it (its parent: commit),
    or None. An old commit's drift script ignores a forced seed and silently replays its default (2026-11-01: four
    "parent" runs on 73f1dcfcbb replayed seed 2 and proved nothing); with main's CI it replays the seed asked for."""
    import shutil, tempfile
    idx = os.path.join(tempfile.mkdtemp(prefix="agentctl-overlay-"), "index")
    env = {**os.environ, "GIT_INDEX_FILE": idx}

    def g(*args):
        return subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, env=env)
    try:
        if g("read-tree", commit).returncode != 0:
            return None
        for d in DRIFT_OVERLAY:
            # -f: outside a checkout of `commit` its files differ from the worktree and HEAD, and a plain
            # `rm --cached` refuses ("staged content different from both the file and the HEAD")
            if g("rm", "-r", "-q", "-f", "--cached", "--ignore-unmatch", d).returncode != 0:
                return None
            if g("cat-file", "-e", f"{remote()}/main:{d}").returncode == 0 and \
                    g("read-tree", f"--prefix={d}/", f"{remote()}/main:{d}").returncode != 0:
                return None
        tree = g("write-tree").stdout.strip()
        r = g("commit-tree", tree, "-p", commit, "-m", f"blame probe: {commit} with {remote()}/main's "
              f"{' and '.join(DRIFT_OVERLAY)} (its drift rows honour the forced seed)")
        return r.stdout.strip() if r.returncode == 0 and tree else None
    finally:
        shutil.rmtree(os.path.dirname(idx), ignore_errors=True)


def drift_seed_check(out):
    """None when every row identity-drift-ci reports replayed the seed it pinned (`seeds S1,S2 (the red's own)`; a row
    names its seed on its verdict line, `takes-drift-N (<country>) [seed S]: ...`), else why not."""
    m = DRIFT_PINS.search(out)
    rows = DRIFT_ROW.findall(out)
    if not m:
        return "the run never said which seeds it pinned"
    if not rows:
        return "no drift row named the seed it replayed"
    pins = m.group(1).split(",")
    bad = [f"row {n} replayed seed {seed}, not {pins[int(n) - 1]}" for n, seed in rows
           if int(n) - 1 >= len(pins) or not pins[int(n) - 1] or seed.strip() != pins[int(n) - 1]]
    return "; ".join(bad) or None


def probe_drift(repo, commit, run, wall):
    """The red run's failing drift rows replayed on `commit` with THE RED RUN'S OWN SEEDS (identity-drift-ci
    --for-red), on drift_overlay's commit (main's CI over its code). The replayed seed is checked against the pinned one
    on every row (drift_seed_check): a mismatch is NO VERDICT, never a pass. DRIFT CLEAN is a pass, DRIFT RED or DRIFT
    PROGRESS (fewer flips, still some) a fail, anything else no verdict."""
    probe = drift_overlay(repo, commit)
    if not probe:
        return {"verdict": "none", "evidence": f"could not lay {remote()}/main's CI over {commit[:10]}"}
    try:
        r = subprocess.run([drift_ci_path(repo), probe, "--for-red", str(run), "--timeout", str(int(wall // 60))],
                           cwd=repo, capture_output=True, text=True, timeout=wall + 120, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as e:
        return {"verdict": "none", "evidence": f"identity-drift-ci: {str(e)[:160]}"}
    last = (r.stdout.strip().splitlines() or [""])[-1].strip()
    v = "pass" if last.startswith("DRIFT CLEAN") else "fail" if last.startswith(("DRIFT RED", "DRIFT PROGRESS")) else "none"
    said = f"identity-drift-ci {probe[:10]} ({commit[:10]} + main's CI) --for-red {run}: {last[:200] or f'exit {r.returncode}'}"
    if v != "none":
        wrong = drift_seed_check((r.stdout or "") + "\n" + (r.stderr or ""))
        if wrong:
            return {"verdict": "none", "evidence": f"{said}; NOT THE RED'S SEEDS: {wrong}"}
        return {"verdict": v, "evidence": said, "seeds": True}  # every row replayed the seed it pinned
    return {"verdict": v, "evidence": said}


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


# -- 3. the revert: the culprit's commits undone, landed as a gate fix
def blame_culprit(b):
    """The culprit's group (confirmed, or refuted since), or None."""
    b = b or {}
    return b["groups"][b["culprit"]] if b.get("culprit") is not None and b.get("groups") else None


def blame_worktree(repo, kind, num):
    """A fresh <clone>-wt-<kind>-<run>[-n] off <remote>/main (its branch named the same): (path, None) or (None, why)."""
    _d, clone, _w = waiting_state(repo)
    wt, branch, n = f"{clone}-wt-{kind}-{num}", f"{kind}-{num}", 1
    while os.path.exists(wt) or git_ok(repo, "rev-parse", "--verify", "-q", f"refs/heads/{branch}"):
        n += 1
        wt, branch = f"{clone}-wt-{kind}-{num}-{n}", f"{kind}-{num}-{n}"
    git(repo, "worktree", "prune", check=False)
    git(repo, "fetch", "--quiet", remote(), "main", check=False)
    r = git(repo, "worktree", "add", "-q", "-b", branch, wt, f"{remote()}/main", check=False)
    if r.returncode != 0:
        return None, f"git worktree add failed ({r.stderr.strip()[:160]})"
    return wt, None


def blame_revert(o_r, repo, u, b):
    """ONE `git revert` of the culprit's commits (newest first, one commit) with `Reverts-Change:` and `Fixes-Red:`,
    landed by a detached `agentctl land` (a gate fix: it lands while main is red, through the normal hook). Returns
    {ok, msg, worktree, at}; a revert that does not apply is aborted (ok False)."""
    g = blame_culprit(b)
    num = u["run"].get("number")
    rec = {"at": ts(), "commits": g["commits"], "change": g.get("change")}
    if red_owner_only(u.get("repo") or o_r):
        return {**rec, "ok": False, "msg": f"no revert in {u.get('repo') or o_r}: its pushes are the owner's"}
    wt, why = blame_worktree(repo, "revert", num)
    if not wt:
        return {**rec, "ok": False, "msg": why}
    r = git(wt, "revert", "--no-commit", *reversed(g["commits"]), check=False)
    if r.returncode != 0:
        git(wt, "revert", "--abort", check=False)
        git(wt, "reset", "-q", "--hard", f"{remote()}/main", check=False)
        return {**rec, "ok": False, "worktree": wt, "conflict": True,
                "msg": f"the revert of {blame_change_name(g)} does not apply ({(r.stderr or r.stdout).strip()[:160]})"}
    name = g.get("change") or g["commits"][0]
    msg = (f"Revert {blame_change_name(g)}: {str(g.get('subject') or '')[:60]}\n\n"
           f"agentctl blame: {u['workflow']} #{num} turned red with it; its parent {str(b.get('parent'))[:10]} passes the "
           f"same check, its end fails ({str(b.get('evidence') or '')[:300]}).\nReverted: "
           f"{', '.join(c[:10] for c in g['commits'])}. Its lander, {lander_name(g.get('lander'))}, re-applies it with "
           f"its fix.\n\n" + "".join(f"This reverts commit {c}.\n" for c in reversed(g["commits"]))  # prepush-check's
           + f"\nReverts-Change: {name}\nFixes-Red: {num}\nCo-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>\n")
    c = git(wt, "commit", "-q", "-m", msg, check=False)
    if c.returncode != 0:
        return {**rec, "ok": False, "worktree": wt, "msg": f"git commit of the revert failed ({c.stderr.strip()[:160]})"}
    pid = spawn_land(wt, fixer_env(o_r, num), reds_dir(f"revert-{num}.log"))
    return {**rec, "ok": True, "worktree": wt, "sha": git(wt, "rev-parse", "HEAD").strip(),
            "msg": f"landing a revert of {blame_change_name(g)} from {wt} (pid {pid})"}


def blame_reland(o_r, repo, u, b):
    """The revert did not clear the red, so the culprit was wrong: its revert reverted (`Relands-Change:`), landed by
    `agentctl land` (no Fixes-Red: a production change, it lands at the next green). {ok, msg, worktree, at}."""
    rv = (b.get("revert") or {}).get("landed") or (b.get("revert") or {}).get("sha")
    num = u["run"].get("number")
    g = blame_culprit(b) or {}
    if not rv:
        return {"ok": False, "at": ts(), "msg": "no landed revert to undo"}
    wt, why = blame_worktree(repo, "reland", num)
    if not wt:
        return {"ok": False, "at": ts(), "msg": why}
    r = git(wt, "revert", "--no-edit", rv, check=False)
    if r.returncode != 0:
        git(wt, "revert", "--abort", check=False)
        return {"ok": False, "at": ts(), "worktree": wt, "msg": f"the revert {rv[:10]} cannot be undone ({(r.stderr or r.stdout).strip()[:160]})"}
    body = git(wt, "log", "-1", "--format=%B").strip()
    git(wt, "commit", "--amend", "-q", "-m", f"{body}\n\nagentctl blame: reverting {rv[:10]} did not clear {u['workflow']} "
        f"#{num}: {blame_change_name(g)} was not its culprit, so it is relanded.\n\nRelands-Change: "
        f"{g.get('change') or (g.get('commits') or ['?'])[0]}\nCo-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>\n")
    pid = spawn_land(wt, fixer_env(o_r, num), reds_dir(f"reland-{num}.log"))
    return {"ok": True, "at": ts(), "worktree": wt, "msg": f"relanding {blame_change_name(g)} (reverting {rv[:10]}) from {wt} (pid {pid})"}


def blame_refuted(repo, b, sha):
    """The landed fix `sha` that a run refuted is this red's revert (its `Reverts-Change:` trailer): the culprit was
    wrong. The blame, marked refuted with the reland due -- the laptop's dispatcher relands it (blame_reland)."""
    if (b or {}).get("state") != "confirmed" or not (b.get("revert") or {}).get("ok") or not sha:
        return None
    body = git(repo, "log", "-1", "--format=%B", sha, check=False).stdout if repo else ""
    if "Reverts-Change:" not in body:
        return None
    return dict(b, state="refuted", why=f"its revert {sha[:10]} did not clear the red: the culprit was wrong",
                revert=dict(b["revert"], landed=sha), reland_due=sha)


def blame_note(b):
    """What a fresh fixer is told of a red's blame (its change's diff and worktree), or ''."""
    b = b or {}
    g = blame_culprit(b)
    if not g:
        return ""
    rv = b.get("revert") or {}
    shas = " ".join(c[:10] for c in g["commits"])
    if b.get("state") == "refuted":
        return (f" THE BLAME WAS WRONG: {blame_change_name(g)} ({shas}) was reverted and that did not clear this red, so "
                f"it is being relanded ({(b.get('reland') or {}).get('msg') or 'due'}). Look elsewhere for the cause.")
    if rv.get("ok"):
        return (f" THE OFFENDING CHANGE is {blame_change_name(g)} ({shas}: `git show {shas}`), landed from "
                f"{str(g.get('worktree') or '?').replace(HOME, '~')}; its parent passes the same check, its end fails "
                f"({str(b.get('evidence') or '')[:200]}). It was REVERTED ({rv.get('msg')}): your job is to make it land "
                f"again, fixed -- in your worktree, after `git fetch {remote()} && git rebase {remote()}/main`, "
                f"`git revert <the revert's sha>` re-applies it; add the fix and land it (`Fixes-Red:` not needed: the "
                f"revert fixed the red). Its lander could not be revived: {b.get('revive_failed') or 'none recorded'}.")
    return (f" THE OFFENDING CHANGE is {blame_change_name(g)} ({shas}: `git show {shas}`), landed from "
            f"{str(g.get('worktree') or '?').replace(HOME, '~')}; its parent passes the same check, its end fails. Its "
            f"revert could not be landed ({rv.get('msg') or 'not tried'}): fix it forward (`Fixes-Red:`).")


def blame_status(d):
    """The red's blame for the status line and `red status`: (text, short) or None."""
    b = d.get("blame") or {}
    g = blame_culprit(b)
    if not g:
        return None
    rv = b.get("revert") or {}
    who = lander_name(g.get("lander")) if (g.get("lander") or {}).get("session") else "the owner (by hand)"
    if b.get("state") == "refuted":
        return (f"blame refuted: the revert of {blame_change_name(g)} did not clear it; relanded",
                f"revert of {g.get('change') or g['commits'][0][:9]} refuted")
    if rv.get("ok"):
        rev = "; lander revived" if b.get("revived") else ""
        return (f"REVERTED {blame_change_name(g)} ({rv.get('msg')}), landed by {who}{rev}",
                f"reverted {g.get('change') or g['commits'][0][:9]}{rev}")
    if rv:
        return f"revert of {blame_change_name(g)} failed: {rv.get('msg')}", "revert failed"
    return f"culprit: {blame_change_name(g)}, landed by {who}", f"culprit {g.get('change') or g['commits'][0][:9]}"


def lander_spawn(store, o_r, repo, off, took, u):
    """Tier `lander` (the decision rule's determined culprit), under the taken lease: the culprit reverted, then its
    lander revived; an owner's change (no session): reverted and the owner told. Whatever cannot be done falls back to
    a fresh fixer under the same lease, briefed with the change (never a loop). (ok, message)."""
    pic = off["picture"]
    b = dict(pic.get("blame") or {})
    num = u["run"].get("number")
    it_id = off["item"].split(":", 1)[1]
    g = blame_culprit(b)
    who, why = blame_decision(b)
    rv = b.get("revert") or blame_revert(o_r, repo, u, b)
    b["revert"] = rv
    line = (f"RED REVERT: {red_what(pic)}: {blame_change_name(g)} ({', '.join(c[:10] for c in g['commits'][:5])}), "
            f"landed by {lander_name(g.get('lander')) if who == 'lander' else 'the owner, by hand'}: {rv['msg']}")
    if who == "owner" and rv.get("ok"):
        line += (" -- OWNER: it was yours (no agent session landed it); re-apply it with a fix once you know why "
                 f"(`git revert <the revert>`), see `~/bin/agentctl ci-why {u['run'].get('databaseId')}`")
    inbox_write("red", line, [line], [], 0 if rv.get("ok") and who != "owner" else 1)
    dispatch_log(u, line)
    store.note(off["lease"], took["epoch"], took["secret"], line[:300])
    it = store.item("red", it_id)
    if it:
        store.update_item("red", it_id, it["version"], {"blame": b, "lander_tried": True})
    if who == "owner" and rv.get("ok"):
        return True, line  # nobody to revive: the lease lapses by its TTL, the revert's Fixes-Red holds the red meanwhile
    if who == "lander" and (rv.get("ok") or rv.get("conflict")):  # a revert that conflicts: it fixes forward instead
        return lander_revive(store, o_r, repo, off, took, u, b)
    b["revive_failed"] = "its revert did not land" if not rv.get("ok") else "an owner's change"
    return lander_fallback(store, o_r, repo, off, took, u, b, f"revert failed ({rv['msg'][:120]})")


def lander_fallback(store, o_r, repo, off, took, u, b, why):
    """A FRESH FIXER, under the same lease: the decision rule's only other way -- the culprit or its lander could not be
    acted on. Briefed with the change (blame_note)."""
    it_id = off["item"].split(":", 1)[1]
    it = store.item("red", it_id)
    if it:
        store.update_item("red", it_id, it["version"], {"blame": b, "lander_tried": True})
    u = dict(u, blame_note=blame_note(b))
    return red_dispatch(o_r, repo, u, why=f"tier lander: {why} -> a fresh fixer", lease=off["lease"], took=took)


# -- 4. the lander revived
def lander_ci_why(rid, limit=25):
    """The failing step and its log lines, as `agentctl ci-why <run id>` says them (bounded; '' when it cannot)."""
    try:
        r = subprocess.run([sys.executable, TOOL_PATH, "ci-why", str(rid)], capture_output=True, text=True, timeout=90,
                           stdin=subprocess.DEVNULL, env={**os.environ, "AGENTCTL_DETACHED": "1"})
        return "\n".join((r.stdout or "").strip().splitlines()[-limit:])
    except (OSError, subprocess.TimeoutExpired):
        return ""


def lander_revive(store, o_r, repo, off, took, u, b):
    """THE LANDER REVIVED, under the red's lease (tier `lander`): its own Claude session resumed in its worktree
    (`claude --bg --resume <session>`, launched in the session's own cwd); a SUBAGENT -- which has no session of its own
    to resume -- as a session in its worktree that reads its transcript first. Told the red run, the failing step and
    lines (ci-why), that its change was reverted, and to fix it, rebase, re-apply it with the fix and land again. No
    session, its transcript gone, no worktree to work in, or the launch refused: a fresh fixer (lander_fallback)."""
    g = blame_culprit(b)
    l = g.get("lander") or {}
    num, rid = u["run"].get("number"), u["run"].get("databaseId")
    sid = l.get("session")
    tr = l.get("transcript") if l.get("transcript") and os.path.exists(l["transcript"]) else \
        (None if l.get("agent") else bg_transcript(sid) if sid else None)
    if not sid:
        return lander_fallback(store, o_r, repo, off, took, u, dict(b, revive_failed="no session recorded"),
                               "no lander session")
    if not tr:
        why = f"its transcript is gone ({l.get('transcript') or 'session ' + sid[:8]})"
        return lander_fallback(store, o_r, repo, off, took, u, dict(b, revive_failed=why), why)
    wt, moved = l.get("worktree"), None
    if not wt or not os.path.isdir(wt) or not allowed_path(wt):
        moved = wt
        wt, why = blame_worktree(repo, "lander", num)
        if not wt:
            return lander_fallback(store, o_r, repo, off, took, u, dict(b, revive_failed=why), why)
    rv = b.get("revert") or {}
    name = g.get("change") or g["commits"][0]
    brief = {"mode": "transcript" if l.get("agent") else "resume", "session": sid, "agent": l.get("agent"),
             "transcript": tr, "change": name, "shas": [c[:10] for c in g["commits"]], "subject": g.get("subject"),
             "revert": rv.get("msg"), "reverted": bool(rv.get("ok")), "worktree": wt, "moved_from": moved,
             "evidence": b.get("evidence"),
             "ci_why": lander_ci_why(rid) if rid else ""}
    cwd = l.get("session_cwd") if not l.get("agent") and l.get("session_cwd") and os.path.isdir(l["session_cwd"]) else wt
    resume = {"worktree": wt, "note": None, "launch_cwd": cwd}
    if not l.get("agent"):
        resume["session"] = sid
    ok, msg = red_dispatch(o_r, repo, dict(u, lander=brief, blame_note=None),
                           why=f"tier lander: {lander_name(l)} revived", resume=resume, lease=off["lease"], took=took)
    if not ok:
        return lander_fallback(store, o_r, repo, off, took, u, dict(b, revive_failed=msg), f"revival failed ({msg[:120]})")
    it = store.item("red", off["item"].split(":", 1)[1])
    if it:
        store.update_item("red", it["id"], it["version"], {"blame": dict(b, revived={"at": ts(), "how": brief["mode"],
                                                                                    "worktree": wt})})
    line = (f"LANDER REVIVED: {red_what(u)}: {lander_name(l)} -- its change {name} "
            f"{'was reverted' if rv.get('ok') else 'could not be reverted (it fixes forward)'} -- "
            f"{'resumed' if brief['mode'] == 'resume' else 'continued from its transcript'} in {wt.replace(HOME, '~')} "
            "to fix it and land it again (`claude agents`)")
    inbox_write("red", line, [line], [], 0, cwd=wt)
    return True, line


def lander_prompt(u, wt):
    """The revived lander's task (fixer_prompt's, for tier `lander`)."""
    l = u["lander"]
    num, rid = u["run"].get("number"), u["run"].get("databaseId")
    name = l.get("change")
    head = (f"YOU ARE THE AGENT THAT LANDED {name}, continued from your transcript {l.get('transcript')} (subagent "
            f"{l.get('agent')} of session {str(l.get('session'))[:8]}): read its last part first, it is your own context. "
            if l.get("mode") == "transcript" else "")
    moved = (f"Your old worktree {l['moved_from']} is gone: work in {wt} (off {remote()}/main). " if l.get("moved_from")
             else "")
    why = f"\n\n`agentctl ci-why {rid}` says:\n{l['ci_why']}\n\n" if l.get("ci_why") else " "
    if l.get("reverted") is False:  # its revert did not apply: fix forward, as a gate fix
        return (head + f"agentctl REVIVED YOU: your change {name} ({', '.join(l.get('shas') or [])}) turned main red -- "
                f"{u['workflow']} #{num} (run id {rid}) failed in job `{u.get('job')}` with "
                f"{str(u.get('line') or '')[:300]!r}; its parent passes the same check, its end fails. It COULD NOT BE "
                f"REVERTED ({l.get('revert')}).{why}" + moved + f"FIX IT FORWARD in your worktree {wt}: `git fetch "
                f"{remote()} && git rebase {remote()}/main`, fix the failure (never loosen the test), commit it with a "
                f"`Fixes-Red: {num}` trailer and land it with `~/bin/agentctl land` (a gate fix: it lands while main is "
                "red). End at `LANDED <sha>`, or `GAVE UP: <why>`.")
    return (head + f"agentctl REVIVED YOU: your change {name} ({', '.join(l.get('shas') or [])}: "
            f"{str(l.get('subject') or '')[:80]}) turned main red -- {u['workflow']} #{num} (run id {rid}) failed in job "
            f"`{u.get('job')}` with {str(u.get('line') or '')[:300]!r}; its parent passes the same check, its end fails "
            f"({str(l.get('evidence') or '')[:200]}). IT WAS REVERTED ({l.get('revert')}).{why}" + moved +
            f"FIX IT in your worktree {wt}: `git fetch {remote()} && git rebase {remote()}/main` (your original commits "
            "drop out as already upstream), re-apply your change with `git revert <the revert's sha>` once the revert is "
            f"on {remote()}/main (`git log {remote()}/main --grep 'Reverts-Change: {name}'`), add the fix, reproduce the "
            "failure and see it pass (the failing test, fast; never loosen it), then land again with "
            "`~/bin/agentctl land` (no Fixes-Red: the revert fixed the red). End at `LANDED <sha>`, or `GAVE UP: <why>`.")


# -- a withdrawn or refuted revert never lands again (2026-11-01: blame's wrong revert of ...-retained-input for #629 was
# WITHDRAWN -- the red re-decided "pre-existing" -- yet maintain's orphan duty found its leftover worktree
# movies-wt-revert-629, "author gone", and submitted it: re-queued at 22:33Z, landing on red through its Fixes-Red: 629.)
# A blame stops calling a change the culprit in two ways: WITHDRAWN (the decision itself was wrong, before its revert
# landed: blame_withdraw, `agentctl red unblame`) and REFUTED (its revert landed and did not clear the red). And a
# confirmed blame's revert is OBSOLETE once its red is over (RED_TERMINAL) by anything else -- the lander's fix forward
# under the same Fixes-Red (Main #2409, 2026-11-12: the revert sat refused, then escalated as an orphan, while the
# change it would take out was fixed and green on main). Whichever it is,
# its revert -- the commit carrying `Reverts-Change: <change>` and `Fixes-Red: <run>` -- and the worktree blame made for
# it are dead: blame_land_refusal is asked by every path that lands or queues commits (land's sync, the server's
# submission, the queue's checker, a recovery re-land, orphan duty).
REVERTS_CHANGE = re.compile(r"^Reverts-Change:\s*(\S+)", re.M)


def blame_records(store=None):
    """(dead, live) from every red's blame. dead: {change: [{run, why, verdict, worktree, sha}]} -- the reverts blame
    withdrew or saw refuted ("not the culprit"), or whose red ended without them ("obsolete"); live: {"runs": {change: {run}}, "worktrees": {path}} -- the confirmed blames whose
    revert stands (a change blamed AGAIN for a newer red is reverted for that red: never refused)."""
    dead, live = {}, {"runs": {}, "worktrees": set()}
    try:
        items = (store or Store()).items("red")
    except (Exception, SystemExit):
        return dead, live
    for it in items:
        b = (it.get("data") or {}).get("blame") or {}
        run = str((b.get("run") or {}).get("number") or "")
        w = b.get("withdrawn")
        if isinstance(w, dict):
            rv = w.get("revert") or {}
            ch = w.get("change") or rv.get("change") or (rv.get("commits") or [None])[0]
            if ch:
                dead.setdefault(ch, []).append({"run": run, "why": f"withdrawn: {str(w.get('why') or '?')[:160]}",
                                                "verdict": f"NOT THE CULPRIT of #{run}",
                                                "worktree": rv.get("worktree"), "sha": rv.get("sha")})
        g = blame_culprit(b)
        if not g:
            continue
        ch = g.get("change") or g["commits"][0]
        rv = b.get("revert") or {}
        if b.get("state") == "refuted":
            dead.setdefault(ch, []).append({"run": run, "why": f"refuted: {str(b.get('why') or '?')[:160]}",
                                            "verdict": f"NOT THE CULPRIT of #{run}",
                                            "worktree": rv.get("worktree"), "sha": rv.get("sha")})
        elif b.get("state") == "confirmed" and not w and it.get("state") in RED_TERMINAL:
            fixed = ((it.get("data") or {}).get("fix") or {}).get("sha") or ""
            dead.setdefault(ch, []).append({"run": run, "why": f"#{run} is {it['state']}"
                                            + (f" by {fixed[:10]}" if fixed else "") + " without it",
                                            "verdict": f"OBSOLETE for #{run}",
                                            "worktree": rv.get("worktree"), "sha": rv.get("sha")})
        elif b.get("state") == "confirmed" and not w:
            live["runs"].setdefault(ch, set()).add(run)
            if rv.get("worktree"):
                live["worktrees"].add(os.path.realpath(rv["worktree"]))
    return dead, live


def blame_records_quiet():
    """blame_records for `agentctl land` on the laptop: a land never needs a token (it never runs `secrets`), so a
    remote store is read only with a token already in hand; else nothing here -- ci-runner's submission and its checker
    refuse the revert with their own store (submit_change, _check_entry)."""
    st = Store()
    if isinstance(st, RemoteStore) and not (os.environ.get("AGENTCTL_STORE_TOKEN") or _tokens.get("movies")):
        return {}, {"runs": {}, "worktrees": set()}
    return blame_records(st)


def blame_land_refusal(repo, commits, worktree=None, store=None, records=None):
    """Why these commits (from `worktree`) must never land, or None: a commit carrying `Reverts-Change: <change>` for a
    change blame withdrew or saw refuted (not the culprit) -- unless a confirmed blame of a newer red reverts that change
    again, under its own Fixes-Red -- or the worktree blame made for such a revert."""
    dead, live = records if records is not None else blame_records(store)
    if not dead:
        return None
    for c in commits or []:
        msg = git(repo, "log", "-1", "--format=%B", c, check=False).stdout if repo else ""
        for ch in REVERTS_CHANGE.findall(msg):
            if ch not in dead:
                continue
            if fixes_red_in_message(msg) & live["runs"].get(ch, set()):
                continue  # blamed again for a newer red: that red's revert
            d = dead[ch][-1]
            return (f"commit {c[:10]} reverts {ch}, which agentctl's blame marked {d['verdict']} "
                    f"({d['why']}): its revert never lands again")
    if worktree:
        rw = os.path.realpath(worktree)
        if rw not in live["worktrees"]:
            for ch, ds in dead.items():
                for d in ds:
                    if d.get("worktree") and os.path.realpath(d["worktree"]) == rw:
                        return (f"{worktree.replace(HOME, '~')} is the worktree blame made to revert {ch} for #{d['run']}, "
                                f"marked {d['verdict']} ({d['why']}): nothing from it lands")
    return None


def blame_only_reverts(repo, wt, change=None):
    """The worktree's commits off <remote>/main, when every one is blame's own revert (`Reverts-Change:`, of `change`
    when given) -- else None: somebody else's work is in it, never removed."""
    main = f"{remote()}/main"
    r = git(wt, "rev-list", f"{main}..HEAD", check=False)
    if r.returncode != 0:
        return None
    shas = r.stdout.split()
    for c in shas:
        found = REVERTS_CHANGE.findall(git(wt, "log", "-1", "--format=%B", c, check=False).stdout)
        if not found or (change and change not in found):
            return None
    return shas


def blame_drop_revert_worktree(repo, wt, change=None, why="its blame was withdrawn"):
    """Remove the worktree blame made for a revert that will never land, and its branch: only when its commits are all
    blame's reverts (blame_only_reverts), its tree clean and nobody in it; its open queue entries cancelled. Logged in
    agentctl-deleted.log. Returns one line saying what."""
    if not wt or not os.path.isdir(wt):
        return f"revert worktree {str(wt or '?').replace(HOME, '~')} already gone"
    rw = os.path.realpath(wt)
    shas = blame_only_reverts(repo, rw, change)
    if shas is None:
        return f"revert worktree {rw.replace(HOME, '~')} kept: it holds commits that are not blame's revert"
    mine = own_ancestry()
    busy = [p for p, c in process_cwds().items() if p not in mine and (c == rw or c.startswith(rw + "/"))]
    if busy:
        return f"revert worktree {rw.replace(HOME, '~')} kept: in use by pid {busy[:3]}"
    dirty = git(rw, "status", "--porcelain", "--untracked-files=no", check=False)
    if dirty.returncode or dirty.stdout.strip():
        return f"revert worktree {rw.replace(HOME, '~')} kept: uncommitted changes"
    cancelled = blame_cancel_changes_of(rw, why)
    branch = git(rw, "symbolic-ref", "-q", "--short", "HEAD", check=False).stdout.strip()
    head = git(rw, "rev-parse", "HEAD", check=False).stdout.strip()
    root = os.path.realpath(worktrees(rw)[0]["worktree"])
    r = git(root, "worktree", "remove", "--force", rw, check=False)
    if r.returncode:
        return f"revert worktree {rw.replace(HOME, '~')} kept: git worktree remove refused: {r.stderr.strip()[:160]}"
    log_deletion("worktree", os.path.basename(root), rw, head, f"blame revert never landing: {why}")
    msg = f"removed revert worktree {rw.replace(HOME, '~')}"
    if branch and branch not in PROTECTED_BRANCHES and \
            git(root, "update-ref", "-d", f"refs/heads/{branch}", head, check=False).returncode == 0:
        log_deletion("branch", os.path.basename(root), branch, head, f"blame revert never landing: {why}")
        msg += f" and branch {branch}"
    return msg + (f"; cancelled {', '.join(cancelled)}" if cancelled else "")


def blame_cancel_changes_of(wt, why):
    """Cancel every open queued change submitted from `wt`; returns their ids."""
    rw, out = os.path.realpath(wt), []
    try:
        st = Store()
        items = st.items("change")
    except (Exception, SystemExit):
        return out
    for it in items:
        if machine_state("change", it["state"]).terminal or \
                os.path.realpath(str((it.get("data") or {}).get("worktree") or "")) != rw:
            continue
        try:
            if hasattr(st, "cancel_change"):
                st.cancel_change(it["id"])
            else:
                cancel_submitted(it["id"])
            out.append(it["id"])
        except (Exception, SystemExit) as e:
            err(f"could not cancel {it['id']} ({why}): {str(e)[:120]}")
    return out


def blame_withdraw(store, it, why, repo=None):
    """WITHDRAW a red's blame: its culprit was wrongly decided and its revert has not landed. The blame becomes
    undetermined (the normal fixer flow) with the dead revert recorded under `withdrawn` (blame_records reads it: that
    revert never lands again), the revived lander's lease is stopped, and the worktree blame made for the revert is
    removed. A revert that already landed is not withdrawn: that is a refutation (FIX FAILED relands it). (ok, line)."""
    d = it["data"]
    b = d.get("blame") or {}
    g = blame_culprit(b)
    if not g or b.get("state") not in ("confirmed",):
        return False, f"NOT WITHDRAWN: {red_what(d)} has no confirmed culprit (blame {b.get('state') or 'none'})"
    rv = b.get("revert") or {}
    ch = g.get("change") or g["commits"][0]
    repo = repo or movies_repo_path()
    if rv.get("ok") and repo and os.path.isdir(repo):
        git(repo, "fetch", "--quiet", remote(), "main", check=False)
        on_main = git(repo, "log", f"{remote()}/main", "-n", "400", "--format=%H",
                      f"--grep=^Reverts-Change: {re.escape(ch)}$", check=False).stdout.split()
        if on_main:
            return False, (f"NOT WITHDRAWN: the revert of {ch} is on {remote()}/main ({on_main[0][:10]}): a landed "
                           "revert is refuted by its red's next run, which relands it")
    w = {"change": ch, "revert": rv, "revived": b.get("revived"), "why": why, "at": ts()}
    nb = {k: v for k, v in b.items() if k not in ("culprit", "revert", "revived", "parent", "evidence", "reland_due")}
    nb.update(state="undetermined", why=f"withdrawn: {why}", withdrawn=w)
    red_stop_holder(store, d["key"], f"the blame of {ch} was withdrawn: {why}", "blame withdrawn", force=True)
    dropped = blame_drop_revert_worktree(repo, rv.get("worktree"), ch, why) if rv.get("worktree") else "no revert worktree"
    line = (f"RED BLAME WITHDRAWN: {red_what(d)}: {blame_change_name(g)} is not the culprit ({why}); its revert never "
            f"lands; {dropped}; the normal fixer flow")
    inbox_write("red", line, [line], [], 0)
    dispatch_log({"workflow": d.get("workflow"), "run": d.get("last_run") or {}, "job": d.get("job")}, line)
    cur = store.item("red", it["id"]) or it
    if machine_state("red", cur["state"]).terminal:  # a red already cleared: only its record changes
        store.update_item("red", cur["id"], cur["version"], {"blame": nb})
    else:
        red_offer(store, cur, line, data={"blame": nb}, quiet=True)
    return True, line
