# agentctl, part `recovery`: the recovery registry.
# Runs inside agentctl's one namespace, after the parts before it in lib/agentctl/__init__.py's PARTS (it uses their names).


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


def token_unavailable_note(ns: str, why: str) -> None:
    """No source had the token: ONE condition per spell (UNKNOWN in the inbox), cleared by the token probe -- the
    processor holds one again -- with its entry acked."""
    if ns != "movies" or _noting_token[0]:  # the sync's store is remote: its call needs this very token -- once
        return
    _noting_token[0] = True
    try:
        open_condition("UNKNOWN", f"UNKNOWN: token unavailable ({why}): run `secrets unlock {token_vault_key('pawelkrupinski/movies')[0]}` in any session (the store token's vault); the "
                                  "next land or `waiting status` re-seeds the processor",
                       run_probe("token", {"ns": ns}), [], key=f"token-{ns}", command="token", exit_code=2)
    except (Exception, SystemExit) as e:
        err(f"token: could not note the missing token ({str(e)[:120]})")
    finally:
        _noting_token[0] = False


_noting_token = [False]  # token_unavailable_note is running: the remote store's own token read must not recurse
