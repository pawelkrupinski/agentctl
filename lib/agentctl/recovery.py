# agentctl, part `recovery`: the recovery registry.
# Runs inside agentctl's one namespace, after the parts before it in lib/agentctl/__init__.py's PARTS (it uses their names).

# ---------------------------------------------------------------- recovery: every non-green condition clears itself
# The owner (2026-10-28): "make this self-recover", "i want a general solution. recovery mechanism", "the way it got
# red / blocked i want it to go green as well", "structural". ONE registry, one JSON per open CONDITION in
# recovery_dir(). Every non-green state the status line or the inbox shows -- an escalation-kind inbox entry
# (CONDITION_KINDS: RED UNCLAIMED / BUDGET / ESCALATE, RED BLOCKED ON OWNER, STALLED, UNKNOWN, NETWORK), an owner block
# (`red block`), the gate UNREAD, an infra NOT LANDED -- is opened by open_condition(kind, text, probe, on_recover) and
# ONLY there: inbox_write refuses those kinds anywhere else (ConditionBypass), a blocked claim cannot be written outside
# it, and test_recovery's lint fails on a bypass in the source.
# THE RULE: the probe is the check that found the problem -- a FAILED ProbeResult, which only run_probe makes -- so the
# check that turned it red is the check that turns it green. The duties thread re-runs each open condition's probe
# (recovery_tick; per-condition backoff; never `secrets`). When it passes the condition closes itself: its inbox entries
# are acked, the gate is marked dirty, the status line refreshes, its on_recover actions run (release a block and
# dispatch the red's workflow to verify it, kick the tunnel, land a change again), and one RECOVERED note is written.
# Nobody acks a solved problem. `agentctl recover status` lists the open conditions.
CONDITION_KINDS = ("INVARIANT", "UNLANDED_ORPHAN", "STALLED", "UNKNOWN", "NETWORK")
RECOVER_BACKOFF_MAX = 600  # a probe is re-run at most this far apart, however often it failed ($AGENTCTL_RECOVER_MAX)
_OPENING = [0]  # > 0 inside open_condition: the one place a condition-kind inbox entry or a block may be written


class ConditionBypass(RuntimeError):
    """A non-green condition written without open_condition (and so without a probe that can clear it): a bug."""


class ProbeResult:
    """What one probe said: ok True (passes), False (fails) or None (cannot tell: stays open). Made by run_probe only."""
    __slots__ = ("kind", "args", "ok", "detail", "at")

    def __init__(self, kind, args, ok, detail, _made_by_run_probe=None):
        if _made_by_run_probe is not _RUN_PROBE_TOKEN:
            raise ConditionBypass("a ProbeResult is made by run_probe only: the check that opens is the check that closes")
        self.kind, self.args, self.ok, self.detail, self.at = kind, dict(args), ok, str(detail)[:300], time.time()


_RUN_PROBE_TOKEN = object()


def recovery_dir(*parts):
    d = os.environ.get("AGENTCTL_RECOVERY_DIR") or (reds_dir("recovery") if os.environ.get("AGENTCTL_REDS_DIR")
                                                   else state_root("recovery"))
    return os.path.join(d, *parts)


# -- the probes: (ok, detail) for their args. Background-safe: no `secrets`, bounded, never raising (run_probe catches).
def probe_workflow_green(a):
    """The newest decided run of workflow a['workflow'] on main is green (the CI-state snapshot's `latest`)."""
    snap = ci_state_load(a.get("repo") or SHORT["movies"])
    if not snap:
        return None, "no fresh CI-state snapshot"
    last = (snap.get("latest") or {}).get(a["workflow"])
    if not last:
        return None, f"no decided run of {a['workflow']} on main in the snapshot"
    ok = last.get("conclusion") == "success"
    return ok, f"{a['workflow']} #{last.get('number')} {last.get('conclusion')}"


def probe_http(a):
    lo, hi = a.get("expect") or (200, 499)
    try:
        req = urllib.request.Request(a["url"], method=a.get("method") or "GET")
        with urllib.request.urlopen(req, timeout=float(a.get("timeout") or 10)) as r:
            code = r.status
    except urllib.error.HTTPError as e:
        code = e.code
    except Exception as e:
        return False, f"{a['url']}: {str(e)[:120]}"
    return lo <= code <= hi, f"{a['url']}: HTTP {code}"


def probe_tcp(a):
    import socket
    try:
        with socket.create_connection((a["host"], int(a["port"])), timeout=float(a.get("timeout") or 5)):
            return True, f"{a['host']}:{a['port']} accepts"
    except OSError as e:
        return False, f"{a['host']}:{a['port']}: {e}"


def probe_route(_a):
    r = default_route()
    return (True, f"default route {r[0]} via {r[1]}") if r else (False, "no default route (offline)")


def probe_tunnel(_a):
    r = tunnel_probe()
    return r == "ok", f"the store tunnel (localhost:{tunnel_ports()[0][0]}) {r}"


def probe_gate_read(a):
    """The gate was read (a CI-state snapshot written) after the condition opened."""
    snap = ci_state_load(a.get("repo") or SHORT["movies"])
    at = float((snap or {}).get("at") or 0)
    return at > float(a.get("since") or 0), (f"gate read at {iso_at(at)}" if at else "no fresh CI-state snapshot")


def probe_token(a):
    """A token without `secrets`: the environment, this processor's own, or the processor's socket."""
    ns = a.get("ns") or "movies"
    if os.environ.get("GH_TOKEN") or (ns == "movies" and os.environ.get(token_env_name(SHORT["movies"]))) or _tokens.get(ns):
        return True, f"{ns} token present"
    tok = token_from_processor(SHORT["movies"]) if ns == "movies" else None
    return bool(tok), f"{ns} token " + ("held by the processor" if tok else "unavailable")


def probe_git_fetch(a):
    """The remote's main can be read over git (what a land's `git fetch` needs)."""
    repo = a.get("repo") or movies_repo_path()
    try:
        r = subprocess.run(["git", "-C", repo, "ls-remote", "--heads", remote(), "main"], capture_output=True, text=True,
                           timeout=60, stdin=subprocess.DEVNULL, env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
    except (OSError, subprocess.SubprocessError) as e:
        return False, f"git ls-remote {remote()}: {str(e)[:120]}"
    return r.returncode == 0 and "refs/heads/main" in r.stdout, f"git ls-remote {remote()} main: exit {r.returncode} " \
        + (r.stderr.strip()[-120:] if r.returncode else "ok")


def probe_waiting_ok(a):
    """A waiting round completed after the condition opened."""
    at = float((read_record(os.path.join(a["d"], "round-ok.json")) or {}).get("at") or 0)
    return at > float(a.get("since") or 0), f"last good round {iso_at(at)}" if at else "no good round yet"


PROBES = {"disk_free": lambda a: probe_disk_free(a), "quota_reset": lambda a: probe_quota_reset(a), "invariant": probe_invariant, "waiting_ok": probe_waiting_ok, "orphan_landed": probe_orphan_landed, "workflow_green": probe_workflow_green, "http": probe_http, "tcp": probe_tcp,
          "route": probe_route, "tunnel": probe_tunnel, "gate_read": probe_gate_read, "token": probe_token,
          "git_fetch": probe_git_fetch, "clone_health": probe_clone_health,
          "change_source": lambda a: probe_change_source(a),
          "infra_requeue": probe_infra_requeue, "red_newer_verdict": lambda a: probe_red_newer_verdict(a),
          "batch_progress": lambda a: probe_batch_progress(a), "red_read": lambda a: probe_red_read(a)}
PROBE_EVERY = {"disk_free": 300, "quota_reset": 300, "invariant": 60, "waiting_ok": 30, "orphan_landed": 300, "route": 5, "tunnel": 10, "gate_read": 15,
               "workflow_green": 60, "token": 60, "tcp": 60, "http": 120, "git_fetch": 120,
               "clone_health": 120, "infra_requeue": 60, "change_source": 60, "red_newer_verdict": 60,
               "batch_progress": 30, "red_read": 300}


def probe_words(kind, a):
    """A probe in a person's words: the status line's `auto-clears when <this>`."""
    return {"disk_free": lambda: f"{a.get('min_gb')} GB are free again",
            "quota_reset": lambda: f"the quota resets and {a.get('workflow')}'s {a.get('job')} passes",
            "workflow_green": lambda: f"{a.get('workflow')} is green", "http": lambda: f"{a.get('url')} answers",
            "tcp": lambda: f"{a.get('host')}:{a.get('port')} accepts",
            "route": lambda: "the network is back", "tunnel": lambda: "the tunnel answers",
            "gate_read": lambda: "the gate is read", "token": lambda: "the processor holds a token",
            "git_fetch": lambda: f"git can read {remote()}/main",
            "invariant": lambda: f"invariant {a.get('id')} holds", "waiting_ok": lambda: "a waiting round completes",
            "orphan_landed": lambda: "its commits are on main or somebody works in it",
            "change_source": lambda: f"change {a.get('id')} closes or is queued again",
            "clone_health": lambda: f"{a.get('repo')} verifies and fetches {remote()}/main",
            "infra_requeue": lambda: f"queue entry {a.get('eid')} gets past {a.get('reason')}",
            "batch_progress": lambda: "the waiting changes form a batch (or the gate turns red)",
            "red_read": lambda: f"ci-runner reads {', '.join(a.get('repos') or [])} (the agentctl-reader App)",
            "red_newer_verdict": lambda: f"a newer {a.get('workflow')} run than #{a.get('run')} no longer fails this way"}.get(kind, lambda: kind)()


def run_probe(kind, args=None):
    """Run probe `kind` on args: the ONE way to a ProbeResult. A probe that raises is ok=None (cannot tell)."""
    if kind not in PROBES:
        raise ConditionBypass(f"unknown probe {kind!r}: every condition needs a probe from PROBES")
    args = dict(args or {})
    try:
        ok, detail = PROBES[kind](args)
    except (Exception, SystemExit) as e:
        ok, detail = None, f"probe error: {str(e)[:160]}"
    # True, False or None, never a falsy number (2026-11-16: W1's `x or (0.0 and y)` was 0.0 on a green main: neither
    # `ok is False`, so the watchdog never refreshed its condition, nor true, so nothing closed it)
    return ProbeResult(kind, args, None if ok is None else bool(ok), detail, _made_by_run_probe=_RUN_PROBE_TOKEN)


def recover_tunnel_reset(cond, a):
    st = read_json_file(tunnel_state_path())
    if st.get("network") or st.get("fails") or st.get("pending_since"):
        st.update(fails=0, network=False, pending_since=None, escalated=False)
        write_json_file(tunnel_state_path(), st)
    return ""


def recover_clear_flag(cond, a):
    try:
        os.unlink(a["path"])
    except OSError:
        pass
    return ""


# A RE-LAND NEVER TOUCHES THE AUTHOR'S WORKTREE (2026-11-06 22:51Z: orphan duty ran `agentctl land` IN
# ~/projects/movies-wt/es-gate-check, which rebased a live session's check worktree onto origin/main). The re-land runs
# in a throwaway tree of agentctl's own (reland_tree_path: tagged internal, named after the author's worktree so the
# change id still names it), detached at the author's HEAD: land rebases THAT tree; the author's branch, HEAD and files
# stay exactly as they were. A later re-land replaces the tree; LANDED closes it like any land's tree.
def reland_tree_path(wt):
    return recovery_dir("reland-trees", os.path.basename(os.path.realpath(wt).rstrip("/")))


def reland_tree(wt):
    """A fresh detached tree at wt's HEAD (reland_tree_path), or raise RuntimeError saying why not."""
    head = git(wt, "rev-parse", "HEAD", check=False).stdout.strip()
    if not head:
        raise RuntimeError(f"{wt} has no HEAD")
    tree = reland_tree_path(wt)
    if os.path.exists(tree):
        git(wt, "worktree", "remove", "--force", tree, check=False)
        if os.path.exists(tree):
            import shutil
            shutil.rmtree(tree, ignore_errors=True)
    git(wt, "worktree", "prune", check=False)
    os.makedirs(os.path.dirname(tree), exist_ok=True)
    r = git(wt, "worktree", "add", "-q", "--detach", mark_internal(tree), head, check=False)
    if r.returncode:
        raise RuntimeError(f"could not make a throwaway tree at {tree}: {(r.stderr or '').strip()[:200]}")
    return tree


def reland_worktree(cond, a):
    """`agentctl land` again for the change in a["worktree"], from a throwaway tree at its HEAD (it fetches and rebases
    there: a conflict is a real NOT LANDED). 'landing ...' when started, else why not (the caller says it: never a
    silent drop)."""
    wt = a["worktree"]
    if not os.path.isdir(wt):
        return f"not landed again: {wt} is gone"
    if git(wt, "status", "--porcelain", "--untracked-files=no", check=False).stdout.strip():
        return f"not landed again: {wt} has uncommitted changes"
    commits = git(wt, "rev-list", f"{remote()}/main..HEAD", check=False).stdout.split()
    why = blame_land_refusal(wt, commits, wt) or held_commit(wt, commits)
    if why:
        return f"not landed again: {why}"
    try:
        tree = reland_tree(wt)
    except RuntimeError as e:
        return f"not landed again: {e}"
    os.makedirs(recovery_dir(), exist_ok=True)
    with open(recovery_dir("reland.log"), "a") as f:
        f.write(f"=== {ts()} re-land of {wt} from {tree}\n")
        f.flush()
        spawn_detached([sys.executable, TOOL_PATH, "land"], stdin=subprocess.DEVNULL, stdout=f,
                       stderr=f, start_new_session=True, cwd=tree,
                       env={**os.environ, "AGENTCTL_NO_SECRETS": "1"})
    return f"landing {wt.replace(HOME, '~')} again from a throwaway tree {tree.replace(HOME, '~')} (attempt {a.get('attempt')})"


def recover_clear_hold(cond, a):
    st = open_store()
    it = current_batch(st, a["d"])
    if it and it["state"] == "held":
        batch_move(st, it, "assembling", text=f"batch {it['id']}: its hold lifted")
    return "the batch hold is lifted"


RECOVER_ACTIONS = {"clear_hold": recover_clear_hold, "tunnel_reset": recover_tunnel_reset, "clear_flag": recover_clear_flag}


def condition_key(kind, probe):
    return re.sub(r"[^\w.-]+", "_", f"{kind}-{probe.kind}-" + "-".join(f"{v}" for _k, v in sorted(probe.args.items())
                                                                           if _k != "since"))[:150]


_last_recovery = [0.0]


def maybe_recovery_tick():
    if time.time() - _last_recovery[0] < 5:
        return
    _last_recovery[0] = time.time()
    recovery_tick()


# -- conditions are items (design §5.4; phase 1 step 12): the store's "condition" machine, open -> closed
CONDITION_ESCALATE_EVERY = 2 * 3600  # an open condition escalates 10 min after opening, then every 2 h


def condition_item_of(store, key):
    """The open condition item for key, or None."""
    for it in store.items("condition", ["open"]):
        if it["data"].get("key") == key:
            return it
    return None


def condition_dict(it):
    return {**it["data"], "id": it["id"], "version": it["version"], "deadline": it["deadline"], "level": it["level"]}


# A BLIP OPENS NOTHING (the owner, 2026-11-17: 19 UNKNOWN spells in a week, 18 closed by themselves in ~70 s --
# "movies token present" -- each an inbox entry and a notification for nothing): a kind in CONDITION_GRACE opens only
# once it is seen failing again at least that long after it was first seen (one sighting run: sightings no more than
# CONDITION_PENDING_LAPSE apart; a run that lapsed starts again). Until then the sighting is a pending mark in the
# store's meta (`condition_pending:<key>`), and open_condition answers None as for a passing probe. A store that cannot
# keep the mark opens the condition at once, as before: loud over silent.
UNKNOWN_GRACE = 300  # s an UNKNOWN condition must keep failing (seen again past this) before it opens
CONDITION_PENDING_LAPSE = 1800  # s between two sightings after which a pending condition's sighting run starts again


def condition_grace(kind):
    return env_num("AGENTCTL_UNKNOWN_GRACE", UNKNOWN_GRACE) if kind == "UNKNOWN" else 0


def condition_pending(st, key, grace, t):
    """True while key's failure is younger than grace (THE BLIP rule above): the sighting is marked, nothing opens."""
    if grace <= 0:
        return False
    mk = f"condition_pending:{key}"
    try:
        p = json.loads(st.meta_get(mk) or "null") or {}
        lapse = env_num("AGENTCTL_CONDITION_PENDING_LAPSE", CONDITION_PENDING_LAPSE)
        if not p or t - float(p.get("last") or 0) > lapse:
            p = {"first": t}
        if t - float(p["first"]) < grace:
            st.meta_set(mk, json.dumps({"first": p["first"], "last": t}))
            return True
        st.meta_set(mk, "")
    except (Exception, SystemExit) as e:
        err(f"condition {key}: its pending mark could not be kept ({str(e)[:120]}): opened at once")
    return False


def open_condition(kind, text, probe, on_recover, key=None, inbox=True, command="recover", exit_code=1, cwd=None,
                   tail=(), effect=None, store=None, grace=None):
    """Open (or refresh) a non-green condition: an item of the "condition" machine. probe: the FAILED ProbeResult of the
    check that found it (required: the same probe, passing, closes it); on_recover: [(action, args)] from
    RECOVER_ACTIONS. effect: a callable run inside; its dict result is merged into every on_recover args. Writes ONE
    inbox entry (text) per spell. Returns the condition, or None when the probe passes now (nothing is wrong) or the
    failure is still inside its kind's grace (grace: seconds, None for the kind's: THE BLIP rule above)."""
    if not isinstance(probe, ProbeResult):
        raise ConditionBypass(f"open_condition({kind!r}) without a ProbeResult: no condition without its recovery probe")
    for name, _a in on_recover:
        if name not in RECOVER_ACTIONS:
            raise ConditionBypass(f"unknown on_recover action {name!r}")
    if probe.ok:
        return None
    st = store or open_store()
    key = key or condition_key(kind, probe)
    cur = condition_item_of(st, key)
    if not cur and condition_pending(st, key, condition_grace(kind) if grace is None else grace, time.time()):
        return None
    _OPENING[0] += 1
    try:
        extra = (effect() if effect else None) or {}
        text = text(extra) if callable(text) else text
        ids = []
        if inbox and not cur:
            eid = inbox_write(command, text, [text], list(tail), exit_code, cwd=cwd, item=f"condition:{key}")
            ids = [eid] if eid else []
    finally:
        _OPENING[0] -= 1
    t = time.time()
    new = [[n, {**x, **extra}] for n, x in on_recover]
    if cur:  # refreshed: re-described too, so its escalations say what is wrong NOW, not at its opening
        d = cur["data"]
        acts = list(d.get("on_recover") or []) + [x for x in new if x not in (d.get("on_recover") or [])]
        st.update_item("condition", cur["id"], cur["version"], {"on_recover": acts, "last_checked": t, "text": text,
                                                                 "last_result": {"ok": probe.ok, "detail": probe.detail}})
        return condition_dict(st.item("condition", cur["id"]))
    iid, n = key, 1
    while st.item("condition", iid) is not None:  # an earlier spell is closed: a new item
        n += 1
        iid = f"{key}.{n}"
    data = {"key": key, "kind": kind, "text": text, "probe": {"kind": probe.kind, "args": probe.args},
            "until": probe_words(probe.kind, probe.args), "on_recover": new, "opened": ts(), "opened_at": t,
            "inbox": ids, "last_checked": t, "last_result": {"ok": probe.ok, "detail": probe.detail}, "fails": 0,
            "next_check": t + PROBE_EVERY.get(probe.kind, 60), "command": command, "cwd": cwd, "host": host_role()}
    return condition_dict(st.put_item("condition", iid, "open", data))


def open_conditions(store=None):
    """The open conditions, as dicts (key, kind, text, probe, on_recover, inbox, ... plus id and version)."""
    return [condition_dict(it) for it in (store or open_store()).items("condition", ["open"])]


def close_condition(cond, result, store=None):
    """The probe passed (result: its ProbeResult, or a reason string for a manual close): the item moves to closed (a
    compare-and-set: one closer), its inbox entries are acked, its on_recover actions run, one RECOVERED note."""
    st = store or open_store()
    if isinstance(result, str):
        result = ProbeResult(cond["probe"]["kind"], cond["probe"]["args"], True,
                             result if result.startswith("INVALID:") else f"closed by hand: {result}",
                             _made_by_run_probe=_RUN_PROBE_TOKEN)
    it = st.item("condition", cond["id"])
    if not it or it["state"] != "open":
        return None
    manual = str(result.detail).startswith(("INVALID:", "closed by hand:"))
    reason = str(result.detail)
    try:
        st.transition("condition", it["id"], "closed", it["version"],
                      data={"closed": ts(), "closed_by": reason},
                      event={"text": f"{cond.get('kind')} closed: {reason[:200]}", "refs": {"key": cond.get("key")}})
    except (StaleVersion, TerminalState):
        return None  # another closer won
    acked = os.path.join(inbox_dir(), "acked")
    os.makedirs(acked, exist_ok=True)
    for eid in it["data"].get("inbox") or []:
        open(os.path.join(acked, eid), "a").close()
    did = []
    for name, a in it["data"].get("on_recover") or []:
        try:
            w = RECOVER_ACTIONS[name](cond, a)
            if w:
                did.append(w)
        except (Exception, SystemExit) as e:
            did.append(f"{name} failed: {str(e)[:120]}")
    mark_gate_dirty()
    line = ((f"CLOSED (unjustified): {reason[len('INVALID: '):]}: {cond.get('text', '')[:200]}"
             if reason.startswith("INVALID:") else f"CLOSED: {reason[len('closed by hand: '):]}: {cond.get('text', '')[:200]}")
            if manual else
            f"RECOVERED: {cond.get('until')} ({result.detail}): {cond.get('text', '')[:200]}"
            + (f"; {'; '.join(did)}" if did else ""))
    err(line)
    inbox_write("recover", line, [line], [], 0)
    return line


def migrate_condition_files(store):
    """Step 12's migration: every open condition file under recovery_dir() becomes a condition item; the file goes."""
    out = []
    for p in sorted(glob.glob(recovery_dir("*.json"))):
        c = read_record(p)
        if not c or not c.get("probe"):
            continue
        if c.get("kind") != "BLOCKED" and not condition_item_of(store, c["key"]):
            data = {k: v for k, v in c.items() if k != "path"}
            data.setdefault("opened_at", time.time())
            data["escalate_after"] = time.time() + CONDITION_ESCALATE_EVERY  # its inbox entry told already: no storm
            store.put_item("condition", c["key"], "open", data)
            out.append(f"migrated the condition {c['key']}")
        try:
            os.unlink(p)
        except OSError:
            pass
    return out


def probed_here(c):
    """A condition's probe runs on the host role that opened it (its `host`; before it was recorded: the server's):
    the laptop's watchdog, tunnel or token conditions read the laptop -- on ci-runner a W5 probe found no plist and
    closed, `route -n get default` never answered, and the laptop's inbox entries were acked in the server's."""
    if c["probe"]["kind"] == "token" and host_role() == "sync":
        return True  # the processor's socket is the laptop's: a token condition the server opened (2026-11-02:
        # token-movies stayed open for good, ci-runner has no socket) is cleared by the laptop's probe
    return c.get("host", "server") == host_role()


def recovery_tick(now_=None, force=False, store=None):
    """Each host's look (the server's tick and the laptop's sync both run it): each open condition OPENED ON THIS HOST
    whose next check is due re-runs ITS probe; a pass closes it. Backoff: PROBE_EVERY x 2^fails, at most
    RECOVER_BACKOFF_MAX. Past its deadline (10 min after opening, then every 2 h) an open condition escalates -- on the
    server only (one writer of escalations): the state's self-transition, an escalate event the notifier delivers.
    Returns the RECOVERED lines."""
    st = store or open_store()
    t = now_ if now_ is not None else time.time()
    out = []
    server = host_role() == "server"
    try:
        out += migrate_condition_files(st)
    except (Exception, SystemExit) as e:
        err(f"recover: could not migrate the condition files ({str(e)[:160]})")
    for it in st.items("condition", ["open"]):
        c = condition_dict(it)
        if probed_here(c) and (force or float(c.get("next_check") or 0) <= t):
            r = run_probe(c["probe"]["kind"], c["probe"]["args"])
            if r.ok:
                line = close_condition(c, r, store=st)
                if line:
                    out.append(line)
                continue
            fails = int(c.get("fails") or 0) + 1
            every = PROBE_EVERY.get(r.kind, 60) * 2 ** min(fails, 8)
            try:
                st.update_item("condition", it["id"], it["version"], {
                    "fails": fails, "last_checked": t, "last_result": {"ok": r.ok, "detail": r.detail},
                    "next_check": t + min(every, env_num("AGENTCTL_RECOVER_MAX", RECOVER_BACKOFF_MAX))})
            except StaleVersion:
                continue
            it = st.item("condition", it["id"])
        if server and it["state"] == "open" and t >= max(it["deadline"], float(it["data"].get("escalate_after") or 0)):
            age = fmt_age(t - float(it["data"].get("opened_at") or t))
            line = f"ESCALATED: {it['data'].get('kind')} open for {age}: {str(it['data'].get('text'))[:200]}"
            try:
                st.transition("condition", it["id"], "open", it["version"], deadline_s=CONDITION_ESCALATE_EVERY,
                              event={"text": line, "audience": {"worktree": it["data"].get("cwd")} if it["data"].get("cwd")
                                     else {}, "refs": {"verdict": line, "key": it["data"].get("key"), "command": "recover"}})
            except StaleVersion:
                pass
    return out


def recovery_recheck(kind, store=None):
    """An event that may have cleared conditions of probe `kind`: probe those now."""
    try:
        for c in open_conditions(store):
            if c["probe"]["kind"] == kind and probed_here(c):
                r = run_probe(kind, c["probe"]["args"])
                if r.ok:
                    close_condition(c, r, store=store)
    except (Exception, SystemExit) as e:
        err(f"recover: recheck of {kind} failed ({str(e)[:120]})")


def recovery_recheck_all(store=None):
    """The network changed or came back: every open condition is probed at the next tick, backoff reset."""
    st = store or open_store()
    for it in st.items("condition", ["open"]):
        try:
            st.update_item("condition", it["id"], it["version"], {"next_check": 0, "fails": 0})
        except StaleVersion:
            pass


def cmd_recover(a):
    if a.rcmd == "tick":
        lines = recovery_tick(force=True)
        print("\n".join(lines) if lines else "recover: nothing cleared")
        return 0
    cs = open_conditions()
    if not cs:
        print("recover: no open conditions")
        return 0
    t = time.time()
    for c in cs:
        lr = c.get("last_result") or {}
        print(f"{c['kind']} {c['key']} (open since {c.get('opened')}{', ESCALATED' if c.get('level') else ''}): "
              f"{c.get('text', '')[:140]}\n"
              f"  probe: {c['probe']['kind']} {json.dumps({k: v for k, v in c['probe']['args'].items() if k != 'since'})}"
              f" -- clears when {c.get('until')}\n"
              f"  last checked {iso_at(float(c.get('last_checked') or 0))}: {'cannot tell' if lr.get('ok') is None else 'fails'}"
              f" ({lr.get('detail')}); next check in {max(0, int(float(c.get('next_check') or 0) - t))} s"
              f" (fails {c.get('fails') or 0})\n"
              f"  on recover: {', '.join(n for n, _x in c.get('on_recover') or []) or 'ack + gate dirty'}")
    return 0


# THE ESCALATION THAT WAKES SOMEBODY is an event (severity escalate) the notifier delivers once (notified_at). What is
# escalated NOW is level-triggered: every red or condition item whose state escalated and that still sits in it (a red
# offered with nobody taking it, a fix unverified, a condition still open). Taking the red, verifying the fix, the probe
# passing: the item leaves that state and the escalation is gone -- nobody acks a solved problem.
def escalated_now(store=None):
    """[(item type, item, words)] of the items escalated and still in that state."""
    st = store or open_store()
    out = []
    t = time.time()
    for it in st.items("red", ["offered", "fix-pending"]):  # only what is the OWNER's (THE ALERT MODEL, alerts.py)
        if it["level"] and alert_red_audience({"kind": "RED_UNCLAIMED"}, it, st, t) == "owner":
            out.append(("red", it, f"{red_what(it['data'])}: {red_view(st, it)[1]}"))
    for it in st.items("condition", ["open"]):
        d = it["data"]
        if it["level"] and alert_condition_audience(d.get("kind"), {"verdict": d.get("text")}, d.get("opened_at"), t) == "owner":
            out.append(("condition", it, str(it["data"].get("text") or it["data"].get("kind"))))
    return out


def escalation_line(es):
    if not es:
        return ""
    first = re.sub(r"\s+--\s+`.*$", "", es[0][2])[:90]
    return f"ESCALATED: {len(es)} ({first}{'; …' if len(es) > 1 else ''})"


def applescript_str(text):
    return '"' + str(text).replace("\\", "\\\\").replace('"', '\\"') + '"'


def notifier_bin():
    """terminal-notifier (clickable), from $AGENTCTL_NOTIFIER, PATH or the nix profile; None when absent."""
    import shutil
    return os.environ.get("AGENTCTL_NOTIFIER") or shutil.which("terminal-notifier") \
        or next((p for p in (f"{HOME}/.nix-profile/bin/terminal-notifier",) if os.path.exists(p)), None)


def notifier_argv(tn, title, text, url=None, group=None):
    """terminal-notifier's argv: a click runs `open -a 'Google Chrome' URL` (Chrome forced); -group replaces the older
    notification of the same item instead of stacking."""
    argv = [tn, "-title", title, "-message", text, "-sound", "Basso"]
    if group:
        argv += ["-group", str(group)]
    if url:
        argv += ["-execute", f"open -a 'Google Chrome' {shlex.quote(url)}"]
    return argv


# WHAT THE OWNER DOES ABOUT A FAILED DELIVERY, per channel and cause (2026-11-22: WN told the owner to "allow
# terminal-notifier" -- not installed -- for an osascript killed by its 10 s timeout: the laptop at load ~150, osascript's
# cold start (XProtect, trustd) took the whole 10 s and was killed at its TCC request; the same command takes 0.5 s on an
# idle machine). Each channel's failure carries its own fix; WN names them.
NOTIFY_TIMEOUT = 30  # s, each channel: an overloaded laptop's osascript cold start takes 10 s and more
NOTIFY_FIX_TIMEOUT = ("{ch} timed out: the laptop was overloaded (`agentctl gov status`, `uptime`); the notifier tries "
                      "again next pass")
NOTIFY_FIX = {"terminal-notifier": "allow terminal-notifier in System Settings > Notifications",
              "osascript": "allow Script Editor (osascript's notifications) in System Settings > Notifications",
              "none": "no notification channel on this host: set $AGENTCTL_NOTIFY"}
_notify_cause = ["", ""]  # (cause, fix) of the last notify() that reached nobody


def notify_cause():
    """Why the last notify() reached nobody ("" when it got through)."""
    return _notify_cause[0]


def notify_fix():
    """What the owner does about the last notify() that reached nobody ("" when it got through)."""
    return _notify_cause[1]


def _delivered(ok, failures=()):
    """failures: (channel, what it said, fix) per channel that failed."""
    failures = list(failures) or [("none", "no channel", NOTIFY_FIX["none"])]
    _notify_cause[:] = ["", ""] if ok else ["; ".join(f"{c}: {w}" for c, w, _f in failures),
                                             "; ".join(dict.fromkeys(f for _c, _w, f in failures))]
    return ok


def _proc_said(r):
    """A failed channel's exit code and the last line it printed (stderr's, else stdout's): the real cause, short."""
    def last(b):
        return next((x.strip() for x in reversed((b or b"").decode(errors="replace").splitlines()) if x.strip()), "")
    said = re.sub(r"\s+", " ", last(r.stderr) or last(r.stdout))[:140]
    return f"exit {r.returncode}" + (f": {said}" if said else "")


def _run_said(e):
    """A channel that never exited: "timed out after N s", or the OS's error -- never the exception's str, whose
    argv (the whole notification text) buries the cause (2026-11-22: WN's "Command '['osascript', ..." cut at 140)."""
    if isinstance(e, subprocess.TimeoutExpired):
        return f"timed out after {e.timeout:g} s"
    return (getattr(e, "strerror", None) or type(e).__name__) + (f": {e.filename}" if getattr(e, "filename", None) else "")


def _channel(ch, argv, fix):
    """Runs one channel: None when it got through, else (channel, what it said, the owner's fix)."""
    try:
        r = subprocess.run(argv, capture_output=True, timeout=env_num("AGENTCTL_NOTIFY_TIMEOUT", NOTIFY_TIMEOUT))
    except subprocess.TimeoutExpired as e:
        return ch, _run_said(e), NOTIFY_FIX_TIMEOUT.format(ch=ch)
    except (OSError, subprocess.SubprocessError) as e:
        return ch, _run_said(e), fix
    return None if r.returncode == 0 else (ch, _proc_said(r), fix)


def notify(title, text, url=None, group=None):
    """A macOS notification, clickable (opens `url` in Chrome) through terminal-notifier; on ANY failure of it (absent,
    not allowed in System Settings, a timeout) the osascript fallback (no click). $AGENTCTL_NOTIFY: a command run with
    (title, text, url) instead (the tests'). Returns True only when one of them got through (exit 0); else False, the
    cause in notify_cause() and the owner's fix in notify_fix() -- the notifier records a delivery only on True
    (2026-11-03). Each channel is bounded by $AGENTCTL_NOTIFY_TIMEOUT (NOTIFY_TIMEOUT); none is retried."""
    cmd = os.environ.get("AGENTCTL_NOTIFY")
    if cmd:
        f = _channel(cmd, [cmd, title, text, url or ""], "fix the notification command ($AGENTCTL_NOTIFY)")
        return _delivered(f is None, [f] if f else ())
    if sys.platform != "darwin":
        return _delivered(False, [("none", "not macOS: no notification channel on this host", NOTIFY_FIX["none"])])
    failures = []
    tn = notifier_bin()
    if tn:
        f = _channel("terminal-notifier", notifier_argv(tn, title, text, url, group), NOTIFY_FIX["terminal-notifier"])
        if f is None:
            return _delivered(True)
        failures.append(f)
    f = _channel("osascript", ["osascript", "-e", f"display notification {applescript_str(text)} with title "
                               f"{applescript_str(title)} " 'sound name "Basso"'], NOTIFY_FIX["osascript"])
    if f is None:
        return _delivered(True)
    return _delivered(False, failures + [f])


def entry_ids(e):
    """The change ids an inbox entry carries: its own, and those of the verdicts merged into it (alert_write)."""
    return [x for x in [e.get("entry")] + list(e.get("entries") or []) if x]


def cmd_inbox(a):
    if a.ids[:1] == ["escalated"]:  # the status line's: what is escalated now (the notifier already told) -- bold
        line = escalation_line(escalated_now())
        if line:
            print(line)
        return 0
    if a.ids[:1] == ["wait"]:
        if a.ids[1:]:
            print("usage: agentctl inbox wait [--kinds K,...] [--audience A,...] [--since-ack] [--timeout S] [--forever]")
            return 2
        return inbox_wait(a.kinds, a.since_ack, a.timeout, a.verbose, getattr(a, "forever", False),
                          audience=getattr(a, "audience", None) or "owner")
    entries = inbox_entries()
    aud = getattr(a, "audience", None)
    if aud and aud != "all":
        want = {x.strip() for x in aud.split(",") if x.strip()}
        entries = [e for e in entries if alert_audience(e) in want]
    if a.ids:
        entries = [e for e in entries if any(e["id"].startswith(i) or any(x.endswith(i) for x in entry_ids(e))
                                              for i in a.ids)]
    if a.unread:
        entries = [e for e in entries if not e["read"]]
    if getattr(a, "count", False):  # every match: a count capped by the listing's --limit read "30 unread" for 44
        print(len(entries))
        return 0
    if a.limit:
        entries = entries[-a.limit:]
    if a.json:
        print(json.dumps(entries, indent=1))
    elif not entries:
        print("inbox: no " + ("unread " if a.unread else "") + f"entries ({inbox_dir()})")
    else:
        for e in entries:
            print(entry_line(e))
            if a.verbose:
                for line in e.get("tail") or []:
                    print(f"    {line}")
    if a.ack and entries:
        # ACK ONLY YOUR OWN (2026-10-29 19:00: an agent's `inbox --unread --ack` marked every agent's verdicts read):
        # --all (the owner, the orchestrator) acks what was listed; an entry named by its WHOLE id is acked whoever's;
        # otherwise only the caller's. A prefix is no name: `inbox 2026 --ack` matches every entry ever written.
        mine = entries if getattr(a, "all", False) else [e for e in entries if e["id"] in (a.ids or ()) or own_inbox_entry(e)]
        os.makedirs(os.path.join(inbox_dir(), "acked"), exist_ok=True)
        for e in mine:
            open(os.path.join(inbox_dir(), "acked", e["id"]), "a").close()
        left = len(entries) - len(mine)
        print(f"inbox: acknowledged {len(mine)} entr{'y' if len(mine) == 1 else 'ies'}"
              + (f"; {left} left unread for the agents they belong to (`--all` acks them: the owner's)" if left else ""))
    return 0


def caller_identity():
    """(session, worktree, agent name) of the process running agentctl: its Claude session, the git worktree of its cwd
    (None outside one, or at $HOME), its agent name (agent_name)."""
    here = safe_cwd()
    r = run_cmd(["git", "-C", here, "rev-parse", "--show-toplevel"]) \
        if os.path.isdir(here) else None
    top = os.path.realpath(r.stdout.strip()) if r is not None and r.returncode == 0 and r.stdout.strip() else None
    if top in (os.path.realpath(HOME), "/"):
        top = None
    return os.environ.get("CLAUDE_CODE_SESSION_ID") or None, top, agent_name()


def own_inbox_entry(e, who=None):
    """Whether inbox entry e is the caller's: its session's, from inside its worktree, or its agent name's."""
    session, top, name = who or caller_identity()
    if session and e.get("owner") == session:
        return True
    if name and e.get("agent") == name:
        return True
    if top:
        for p in (e.get("cwd"), str(e.get("owner") or "").partition("@")[2]):
            if p and os.path.isabs(p):
                p = os.path.realpath(p)
                if p == top or p.startswith(top.rstrip("/") + "/"):
                    return True
    return False
