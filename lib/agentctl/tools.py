# agentctl, part `tools`: env, db-q, the tunnel, state, prepush-check.
# Runs inside agentctl's one namespace, after the parts before it in lib/agentctl/__init__.py's PARTS (it uses their names).

# ---------------------------------------------------------------- queue (retired)
QUEUE_RETIRED = """NOT QUEUED: the merge queue is RETIRED (2026-10-26). There is no pre-merge CI left to batch: the landing check
is no merge requirement any more. Land every change -- identity changes included -- with `~/bin/agentctl land` (Bash
run_in_background=true): it queues the change at once, and the reconciler checks it (the basic checks plus the identity /
page area tests your paths select) and pushes it. While main is red, a production change waits and lands with the batch
once main is green (`agentctl land status`)."""


def cmd_tick(a):
    """`agentctl tick`: the processor (the reconciler's loop of ticks); `tick service install|uninstall|status`: it as the
    launchd agent (design §14.5: `waiting run` / `waiting service`, renamed)."""
    if host_role() == "server":  # ci-runner: its own clone, systemd's notify protocol, a clean stop between ticks
        repo = ensure_server_clone(a.repo)
        signal.signal(signal.SIGTERM, lambda *_: TICK_STOP.__setitem__(0, True))
        Store()  # the database exists (its schema) before systemd hears READY
        sd_notify("READY=1")
        SD_READY[0] = True
    else:
        repo = a.repo or (git(safe_cwd(), "rev-parse", "--show-toplevel", check=False).stdout.strip()
                          if git_ok(safe_cwd(), "rev-parse", "--show-toplevel") else movies_repo_path())
    w = argparse.Namespace(wcmd="service" if a.tcmd == "service" else "run", repo=repo, once=a.once, service=not a.once,
                           action=a.action, renamed=True)
    if a.tcmd == "service" and not a.action:
        print("usage: agentctl tick service install|uninstall|status")
        return 2
    return cmd_waiting(w)


def cmd_queue_retired(a):
    print(QUEUE_RETIRED)
    return 2


# ---------------------------------------------------------------- env
TUNNEL_PORTS = [(3000, "Forgejo web/API"), (2222, "Forgejo ssh")]


def default_ns():
    return "movies" if os.path.realpath(os.getcwd()).startswith(os.path.realpath(f"{HOME}/projects/movies")) else "bitcashier"


def vault_state(ns):
    """'unlocked', 'locked' or 'unknown: <why>' -- `secrets status` only; never unlock, never read a value."""
    try:
        r = subprocess.run([secrets_bin() or "secrets", "status", ns], capture_output=True, text=True, timeout=10, stdin=subprocess.DEVNULL)
    except FileNotFoundError:
        return "unknown: no `secrets` command"
    except subprocess.TimeoutExpired:
        return "unknown: `secrets status` timed out"
    out = r.stdout.strip()
    return out if out in ("unlocked", "locked") else ("locked" if "locked" in out and "unlocked" not in out else f"unknown: {out or r.stderr.strip()[:80]}")


def locked_line(ns):
    return f"vault {ns} locked: ask the user to run `! secrets unlock {ns}`"


def port_open(port, host="127.0.0.1", timeout=2.0):
    import socket
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def export_lines():
    lines = []
    bins = [b for b in SDKMAN_BINS if os.path.isdir(b)]
    if bins:
        lines.append(f'export PATH="{":".join(bins)}:$PATH"')
    jh = f"{HOME}/.sdkman/candidates/java/current"
    if os.path.isdir(jh):
        lines.append(f'export JAVA_HOME="{jh}"')
    lines.append(f'export AGENTCTL_ENV=1')
    return lines


def token_works(ns):
    """Read the Forgejo token inside this call (never printed) and ask the API who it is. (ok, detail)."""
    r = subprocess.run([secrets_bin() or "secrets", "get", ns, "FORGEJO_API_TOKEN"], capture_output=True, text=True, timeout=15, stdin=subprocess.DEVNULL)
    tok = r.stdout.strip()
    if r.returncode or not tok:
        return False, f"no FORGEJO_API_TOKEN in vault {ns}"
    try:
        req = urllib.request.Request(f"{API}/user", headers={"Authorization": f"token {tok}"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            login = json.loads(resp.read()).get("login")
        return True, f"token valid (user {login})"
    except Exception as e:
        return False, f"token rejected or API unreachable: {str(e)[:80]}"
    finally:
        tok = None


def cmd_env(a):
    ns = a.ns or default_ns()
    if not a.check:
        print("\n".join(export_lines()))
        st = vault_state(ns)
        if st != "unlocked":
            print(locked_line(ns) if st == "locked" else f"vault {ns} {st}", file=sys.stderr)
            return 1
        return 0
    env = tool_env()
    problems, report = [], []

    def which(cmd):
        for d in env["PATH"].split(":"):
            p = os.path.join(d, cmd)
            if os.path.isfile(p) and os.access(p, os.X_OK):
                return p
        return None

    sbt = which("sbt")
    (report if sbt else problems).append(f"sbt: {sbt or 'NOT FOUND (install sdkman sbt)'}")
    java = os.path.join(env["JAVA_HOME"], "bin", "java") if env.get("JAVA_HOME") else which("java")
    jv = subprocess.run([java, "-version"], capture_output=True, text=True, timeout=30) if java else None
    if jv and jv.returncode == 0:
        report.append(f"java: {(jv.stderr or jv.stdout).splitlines()[0]}")
    else:
        problems.append(f"java: no java that runs ({java or 'none on PATH'})")
    st = vault_state(ns)
    if st == "locked":
        print(locked_line(ns))
        return 1
    (report if st == "unlocked" else problems).append(f"vault {ns}: {st}")
    tunnel_ok = True
    for port, what in TUNNEL_PORTS:
        ok = port_open(port)
        tunnel_ok &= ok
        (report if ok else problems).append(f"tunnel localhost:{port} ({what}): {'open' if ok else 'REFUSED'}")
    if not tunnel_ok:
        problems.append("  fix: launchctl kickstart -k gui/$(id -u)/net.bitcashier.forgejo-tunnel")
    if on_github():  # forge = github: runs are read with gh's own auth
        gh = (GH_CLI if os.path.isabs(GH_CLI) and os.access(GH_CLI, os.X_OK) else None) or which(GH_CLI)
        (report if gh else problems).append(f"forge github: gh {gh or 'MISSING (' + GH_CLI + ')'}")
    else:
        if st == "unlocked" and port_open(TUNNEL_PORTS[0][0]):
            ok, detail = token_works(ns)
            (report if ok else problems).append(f"forgejo-gh token ({ns}): {detail}")
        gh = os.path.exists(FORGEJO_GH)
        (report if gh else problems).append(f"forgejo-gh: {FORGEJO_GH if gh else 'MISSING ' + FORGEJO_GH}")
    for l in report:
        print(f"ok   {l}")
    for l in problems:
        print(f"FAIL {l}")
    return 1 if problems else 0


# ---------------------------------------------------------------- db-q
DB_ENVS = {
    "forgejo": ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", "pawel@10.1.0.3",
                'sudo -u forgejo nix --extra-experimental-features "nix-command flakes" shell nixpkgs#sqlite -c '
                "sqlite3 -readonly -safe -bail -header -tabs /mnt/data/forgejo/data/forgejo.db"],
}
# Defence in depth: sqlite3 already runs -readonly -safe. One statement, starting SELECT / WITH / PRAGMA, and none of
# these (a CTE can front an INSERT/UPDATE/DELETE; -safe already blocks the file functions).
WRITE_WORDS = re.compile(r"\bINSERT\b|\bREPLACE\s+INTO\b|\bUPDATE\b\s+\S+\s+SET\b|\bDELETE\s+FROM\b|"
                         r"\b(DROP|ALTER|CREATE|ATTACH|DETACH|VACUUM|REINDEX)\s+\w|"
                         r"\b(load_extension|writefile|readfile|edit|fts3_tokenizer)\s*\(", re.I)
TUNNEL_LABEL = "net.bitcashier.forgejo-tunnel"


def strip_sql(sql):
    """The SQL with comments and string/identifier literals blanked (keywords inside them say nothing), in one pass."""
    tok = re.compile(r"'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"|`[^`]*`|\[[^\]]*\]|--[^\n]*|/\*.*?\*/", re.S)
    return tok.sub(lambda m: " " if m.group(0)[:2] in ("--", "/*") else "''", sql)


def sql_refusal(sql):
    """None when the SQL is a single read-only SELECT / WITH ... SELECT / PRAGMA read; else why it is refused."""
    if any(l.lstrip().startswith(".") for l in sql.splitlines()):
        return "sqlite3 dot-commands are refused"
    bare = strip_sql(sql).strip()
    if not bare:
        return "empty query"
    if ";" in bare.rstrip(";").rstrip():
        return "one statement only"
    first = bare.split(None, 1)[0].upper()
    if first not in ("SELECT", "WITH", "PRAGMA"):
        return f"only SELECT, WITH and PRAGMA are allowed (got {first})"
    m = WRITE_WORDS.search(bare)
    if m:
        return f"write/side-effect keyword {m.group(0)!r} is refused"
    if first == "PRAGMA" and "=" in bare:
        return "PRAGMA assignments are refused (reads only)"
    return None


def has_tunnel():
    """The laptop reaches Forgejo through its tunnel (a launchd agent); ci-runner directly, with nothing to kick."""
    return host_role() == "sync"


def launchctl_kick():
    """Kickstart the laptop's Forgejo tunnel. Never on ci-runner (no launchctl there: it raised FileNotFoundError over
    the real error, after a pointless 15 s wait)."""
    if not has_tunnel():
        return
    r = run_cmd(["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{TUNNEL_LABEL}"], timeout=30)
    if r.returncode == 124:
        err(f"launchctl kickstart {TUNNEL_LABEL}: no answer in 30 s")


def kick_tunnel():
    launchctl_kick()
    for _ in range(15):
        if port_open(TUNNEL_PORTS[0][0]):
            return True
        time.sleep(1)
    return False


# ---- THE TUNNEL HEALTH (2026-10-27: the tunnel's ssh was alive but its connections hung; only a *refused* connection
# was kicked). A timeout is a refusal for every Forgejo read (tunnel_retry: ONE kickstart + retry), and the always-on
# processor probes GET <API>/version each cycle (5 s): 2 failures in a row kick it (at most once a minute, logged), and a
# kick that does not restore it within 2 min is ONE STALLED inbox entry.
TUNNEL_PROBE_TIMEOUT = 5
TUNNEL_KICK_MIN_GAP = 60
TUNNEL_RESTORE_WAIT = 120
TUNNEL_DOWN_TEXT = re.compile(r"Connection refused|timed? ?out|timeout|deadline exceeded|Connection reset|EOF", re.I)


def tunnel_state_path():
    return os.environ.get("AGENTCTL_TUNNEL_STATE") or state_root("tunnel-health.json")


def tunnel_down(e):
    """True when the exception says the tunnel refused or hung (not an HTTP answer such as 401/404)."""
    import socket
    if isinstance(e, urllib.error.HTTPError):
        return False
    if isinstance(e, urllib.error.URLError):
        return tunnel_down(e.reason) if isinstance(e.reason, BaseException) else bool(TUNNEL_DOWN_TEXT.search(str(e.reason)))
    if isinstance(e, (socket.timeout, TimeoutError, ConnectionError, subprocess.TimeoutExpired)):
        return True
    return isinstance(e, RuntimeError) and bool(TUNNEL_DOWN_TEXT.search(str(e)))


def tunnel_kick_rate_limited(now_=None):
    """Claim the right to kickstart: False (and nothing done) when one happened less than TUNNEL_KICK_MIN_GAP ago."""
    t = now_ if now_ is not None else time.time()
    st = read_json_file(tunnel_state_path())
    if t - st.get("last_kick", 0) < TUNNEL_KICK_MIN_GAP:
        return False
    st["last_kick"] = t
    write_json_file(tunnel_state_path(), st)
    return True


def read_json_file(path):
    try:
        with open(path) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def write_json_file(path, d):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w") as f:
            json.dump(d, f)
        os.replace(tmp, path)
    except OSError:
        pass


def tunnel_retry(fn):
    """Run a Forgejo read; on a refusal OR a timeout kickstart the tunnel once (rate-limited across processes) and retry
    once. Any other error, and the retry's own, propagate."""
    try:
        return fn()
    except Exception as e:
        if not tunnel_down(e):
            raise
        err(f"the Forgejo tunnel refused or timed out ({str(e)[:80]}); reopening it ({TUNNEL_LABEL}) once and retrying")
        if not has_tunnel() or read_json_file(tunnel_state_path()).get("network"):  # no tunnel on ci-runner; a
            return fn()  # lossy uplink (tunnel_watch): a restart only churns the tunnel -- one plain retry
        if tunnel_kick_rate_limited():
            launchctl_kick()
        for _ in range(15):
            if tunnel_probe() == "ok":
                break
            time.sleep(1)
        return fn()


def tunnel_probe():
    """'ok' (any HTTP answer), 'timeout' or 'refused' for GET <API>/version."""
    import socket
    try:
        with urllib.request.urlopen(f"{API}/version", timeout=TUNNEL_PROBE_TIMEOUT) as resp:
            resp.read()
        return "ok"
    except urllib.error.HTTPError:
        return "ok"
    except Exception as e:
        if isinstance(e, urllib.error.URLError):
            e = e.reason if isinstance(e.reason, BaseException) else e
        if isinstance(e, (socket.timeout, TimeoutError)) or "timed out" in str(e):
            return "timeout"
        return "refused"


UPLINK_LOSS_MAX = 0.2  # packet loss to the internet above which the laptop's own uplink, not the tunnel, is the fault


def uplink_loss():
    """Packet loss (0..1) from the laptop to 1.1.1.1 over 10 pings; 1.0 when ping fails outright."""
    try:
        out = subprocess.run(["ping", "-c", "10", "-i", "0.2", "-t", "5", "1.1.1.1"], capture_output=True, text=True,
                             timeout=15).stdout
        m = re.search(r"([\d.]+)% packet loss", out)
        return float(m.group(1)) / 100 if m else 1.0
    except (OSError, subprocess.SubprocessError):
        return 1.0


_last_route = ["unseen"]  # the route at the last look; None while offline


def default_route():
    """(interface, gateway) of the default route, or None (offline / route failed)."""
    try:
        out = subprocess.run(["route", "-n", "get", "default"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    f = dict(re.findall(r"^\s*(interface|gateway): (\S+)", out, re.M))
    return (f.get("interface"), f.get("gateway")) if f else None


def open_outage(kind, text, probe, on_recover, **kw):
    """open_condition for the laptop's own network or tunnel outage -- which is also the store's (ci-runner's, through
    the tunnel): its inbox entry and this host's state are written whether the store answers or not, so the watch's
    state moves on (one note per outage) instead of raising out of it every cycle. The status line says the rest
    (NETWORK, STORE UNREACHABLE)."""
    try:
        return open_condition(kind, text, probe, on_recover, **kw)
    except StoreUnavailable as e:
        err(f"{kind}: the store did not answer, the condition is not recorded there ({str(e)[:120]})")
        return None


def network_watch(route=None, kick=None):
    """React to the laptop's network changing (another Wi-Fi, a hotspot, back online): the tunnel's ssh rides the old
    route and would take ServerAlive's 30 s to notice, the gate line says UNREAD until the next read. A changed default
    route kickstarts the tunnel at once and marks the gate dirty. Returns 'changed', 'offline' or ''."""
    r = (route or default_route)()
    prev, _last_route[0] = _last_route[0], r
    if prev == "unseen" or r == prev:
        return ""
    if r is None:
        err(f"the network went away (was {prev[0]} via {prev[1]})")
        open_outage("NETWORK", f"NETWORK: offline (no default route; was {prev[0]} via {prev[1]})", run_probe("route"),
                       [("tunnel_reset", {})], key="network-offline", inbox=False)  # the kick: below, on the way back
        return "offline"
    err(f"the network {'is back' if prev is None else 'changed'} ({prev[0] if prev else '-'} via {prev[1] if prev else '-'} -> "
        f"{r[0]} via {r[1]}): "
        f"kickstarting {TUNNEL_LABEL}, the gate is read at once")
    (kick or launchctl_kick)()
    recover_tunnel_reset(None, {})
    try:
        recovery_recheck_all()  # a new route: every open condition is probed again now, its backoff reset
    except StoreUnavailable as e:  # the tunnel is not back yet: the next tick's probes run on their own schedule
        err(f"the store did not answer the recheck ({str(e)[:120]})")
    mark_gate_dirty()
    return "changed"


def tunnel_watch(now_=None, probe=None, kick=None, uplink=None):
    """One processor cycle of the health probe. Returns what it did: ok, down, kicked, restored, escalated, network, or
    ''. A tunnel that fails while the laptop's own uplink loses packets is the network's fault: no kickstart (it only
    churns the tunnel), no STALLED escalation, one NETWORK note per outage."""
    t = now_ if now_ is not None else time.time()
    res = run_probe("tunnel") if probe is None else None  # the registry's probe: the one that will clear what it opens
    r = (res.detail.rsplit(" ", 1)[-1] if res else probe())
    st = read_json_file(tunnel_state_path())
    if r == "ok":
        did = "restored" if st.get("pending_since") or st.get("network") else "ok"
        if did == "restored":
            err("the Forgejo tunnel answers again: the gate is read at once")
            mark_gate_dirty()  # the status line leaves `gate UNREAD` at the next duties tick, not at the 45 s mark
        if st.get("fails") or st.get("pending_since") or st.get("network"):
            st.update(fails=0, pending_since=None, escalated=False, network=False)
            write_json_file(tunnel_state_path(), st)
        if (state_read().get("network") or {}).get("at"):
            state_update(network=None)
        return did
    st["fails"] = st.get("fails", 0) + 1
    loss = (uplink or (uplink_loss if probe is None else (lambda: 0.0)))()
    if loss >= UPLINK_LOSS_MAX:
        if not st.get("network"):
            msg = (f"NETWORK: the laptop's uplink loses {int(loss * 100)}% of packets to 1.1.1.1, so Forgejo (localhost:3000) "
                   f"{r}s; not a tunnel fault, nothing to restart -- the gate reads come back with the network")
            err(msg)
            open_outage("NETWORK", msg, res or run_probe("tunnel"), [("tunnel_reset", {})], key="tunnel",
                           command="tunnel")
        st.update(network=True, pending_since=None, escalated=False)  # the kick/escalation clock starts on a good uplink
        write_json_file(tunnel_state_path(), st)
        state_update(network={"loss": loss, "at": t, "forgejo": r})  # the status line says NETWORK, not a bare UNREAD
        return "network"
    if (state_read().get("network") or {}).get("at"):
        state_update(network=None)  # the uplink is fine: a dead forward is the tunnel's (the kickstart below)
    did = "down"
    if st["fails"] >= 2 and t - st.get("last_kick", 0) >= TUNNEL_KICK_MIN_GAP:
        err(f"the Forgejo tunnel {r} {st['fails']} probes in a row; kickstarting {TUNNEL_LABEL}")
        (kick or launchctl_kick)()
        st["last_kick"] = t
        st["pending_since"] = st.get("pending_since") or t
        did = "kicked"
    if st.get("pending_since") and t - st["pending_since"] >= TUNNEL_RESTORE_WAIT and not st.get("escalated"):
        msg = (f"STALLED: the Forgejo tunnel (localhost:3000) still {r} {int((t - st['pending_since']) // 60)} min after a "
               f"kickstart; run `launchctl kickstart -k gui/$(id -u)/{TUNNEL_LABEL}` and check ~/Library/Logs/forgejo-tunnel.log")
        open_outage("STALLED", msg, res or run_probe("tunnel"), [("tunnel_reset", {})], key="tunnel-stalled",
                       command="tunnel", exit_code=2)
        st["escalated"] = True
        did = "escalated"
    write_json_file(tunnel_state_path(), st)
    return did


def run_db(argv, sql):
    return subprocess.run(argv, input=sql.rstrip().rstrip(";") + ";\n", capture_output=True, text=True, timeout=300)


def cmd_db_q(a):
    why = sql_refusal(a.sql)
    if why:
        print(f"agentctl db-q: REFUSED: {why}", file=sys.stderr)
        return 2
    argv = DB_ENVS[a.env]
    r = run_db(argv, a.sql)
    if r.returncode != 0 and re.search(r"Connection refused|Connection timed out|No route to host", r.stderr):
        err(f"connection refused; reopening the tunnel ({TUNNEL_LABEL}) once")
        kick_tunnel()
        r = run_db(argv, a.sql)
    if r.returncode != 0:
        print(f"agentctl db-q: failed (exit {r.returncode}): {r.stderr.strip()[-500:]}", file=sys.stderr)
        return 1
    lines = r.stdout.splitlines()
    head, rows = lines[:1], lines[1:]
    print("\n".join(head + rows[: a.max_rows]))
    if len(rows) > a.max_rows:
        print(f"... {len(rows) - a.max_rows} more row(s) not shown (capped at {a.max_rows}; add LIMIT/WHERE or --max-rows)")
    return 0


# ---------------------------------------------------------------- state
TEMPLATE = """# State: {topic}
updated: {ts}
repo:
branch:
worktree:
commits:
run ids:
waiting on:
next step:
notes:
"""


def cmd_state(a):
    path = os.path.join(scratch(), f"state-{a.topic}.md")
    if not os.path.exists(path):
        with open(path, "w") as f:
            f.write(TEMPLATE.format(topic=a.topic, ts=ts()))
        err(f"created {path}")
    text = open(path).read()
    changed = False
    for spec, append in [(s, False) for s in a.set or []] + [(s, True) for s in a.add or []]:
        field, _, value = spec.partition("=")
        field = field.strip()
        pat = re.compile(rf"^{re.escape(field)}:[ \t]*(.*)$", re.M)
        m = pat.search(text)
        if m:
            old = m.group(1).strip()
            new = f"{old}, {value}" if append and old else value
            text = text[: m.start()] + f"{field}: {new}" + text[m.end():]
        else:
            text = text.rstrip("\n") + f"\n{field}: {value}\n"
        changed = True
    if changed:
        text = re.sub(r"^updated:.*$", f"updated: {ts()}", text, count=1, flags=re.M)
        with open(path, "w") as f:
            f.write(text)
    print(f"# {path}")
    print(text, end="")
    return 0


# ---------------------------------------------------------------- prepush-check
def changed_files(repo, a, b):
    """[(status, path)] for a..b (renames split into delete + add)."""
    out = git(repo, "diff", "--name-status", "--no-renames", "-z", a, b)
    parts = out.split("\0")
    res = []
    i = 0
    while i + 1 < len(parts):
        res.append((parts[i], parts[i + 1]))
        i += 2
    return res


def blobs(repo, specs):
    """Map 'rev:path' -> blob sha or None, in one cat-file call."""
    if not specs:
        return {}
    r = git(repo, "cat-file", "--batch-check=%(objectname)", input="\n".join(specs) + "\n")
    lines = r.splitlines()
    return {s: (None if l.endswith("missing") else l.strip()) for s, l in zip(specs, lines)}


def reverted_commits(repo, mb, head):
    shas = set()
    for body in git(repo, "log", "--format=%B%x00", f"{mb}..{head}").split("\0"):
        shas.update(re.findall(r"This reverts commit ([0-9a-f]{7,40})", body))
    files = set()
    for s in shas:
        r = git(repo, "show", "--name-only", "--format=", s, check=False)
        if r.returncode == 0:
            files.update(l for l in r.stdout.splitlines() if l)
    files.update(declared_retirements(repo, mb, head))
    return files


def declared_retirements(repo, mb, head):
    """Paths named by a `Retires: <path>` trailer (final trailer block) of a commit in mb..head AND absent at head:
    a declared deletion is the author's call, not a squash undoing main. Undeclared or still-present files don't count."""
    paths = set()
    for body in git(repo, "log", "--format=%(trailers:key=Retires,valueonly,unfold)%x00", f"{mb}..{head}").split("\0"):
        paths.update(l.strip() for l in body.splitlines() if l.strip())
    return {p for p in paths if git(repo, "cat-file", "-e", f"{head}:{p}", check=False).returncode != 0}


def foreign_reverts(repo, base, head, files, depth=8, since="45.days"):
    """Files whose content at head equals an OLDER version from main's history -- i.e. the push would undo
    someone's change on main (absence counts: deleting a file main recently added)."""
    hits = []
    head_specs = [f"{head}:{p}" for _, p in files]
    head_blob = blobs(repo, head_specs)
    for (_, p), hs in zip(files, head_specs):
        hb = head_blob[hs]
        commits = git(repo, "log", f"-n{depth}", f"--since={since}", "--format=%H", base, "--", p).split()
        if not commits:
            continue
        olds = blobs(repo, [f"{c}^:{p}" for c in commits])
        cur = blobs(repo, [f"{base}:{p}"])[f"{base}:{p}"]
        for c in commits:
            ob = olds[f"{c}^:{p}"]
            if ob == hb and ob != cur:
                hits.append((p, c[:10]))
                break
    return hits


def allowed(path, globs):
    return any(fnmatch.fnmatch(path, g) or path.startswith(g.rstrip("/") + "/") for g in globs)


def check_range(repo, base, head, allow, max_foreign):
    problems = []
    mb = git(repo, "merge-base", base, head).strip()
    files = changed_files(repo, mb, head)
    stat = git(repo, "diff", "--shortstat", mb, head).strip()
    print(f"prepush-check: {base}...{head[:12]}: {stat or 'no changes'}")
    if allow:
        outside = [p for _, p in files if not allowed(p, allow)]
        if outside:
            problems.append(f"{len(outside)} file(s) outside the allowed set {allow}: " + ", ".join(outside[:20]))
    empty = [c for c in git(repo, "rev-list", "--no-merges", f"{mb}..{head}").split()
             if is_empty_commit(repo, c) and not deliberate_empty(repo, c)]
    if empty:  # never an empty commit on main (four landed as re-queued duplicates, 2026-10-27)
        problems.append(f"{len(empty)} EMPTY commit(s) (no change: already on main?): "
                        + ", ".join(f"{c[:10]} {git(repo, 'log', '-1', '--format=%s', c).strip()[:60]}" for c in empty[:10])
                        + f". Drop them (git rebase {base}); a deliberate empty commit needs an "
                          f"`{ALLOW_EMPTY_TRAILER}: <why>` trailer")
    exempt = reverted_commits(repo, mb, head)
    reverts = [(p, c) for p, c in foreign_reverts(repo, base, head, files) if p not in exempt]
    if allow:  # files in the allowed set are the agent's own: reverting them is its call
        reverts = [(p, c) for p, c in reverts if not allowed(p, allow)]
    if len(reverts) > max_foreign:
        problems.append(
            f"{len(reverts)} file(s) would be reverted/deleted back to an older main version (max {max_foreign}) -- "
            f"a squash onto a moved main? `git rebase {remote()}/main` and re-check. "
            + ", ".join(f"{p} (undoes {c})" for p, c in reverts[:20])
        )
    return problems


def cmd_prepush(a):
    repo = os.getcwd()
    allow = (a.allow or []) + os.environ.get("AGENTCTL_ALLOW", "").split()
    max_foreign = a.max_foreign if a.max_foreign is not None else int(env_num("AGENTCTL_MAX_FOREIGN", MAX_FOREIGN))
    problems = []
    if a.hook:
        remote = a.hook[0]
        base_ref = f"refs/remotes/{remote}/main"
        for line in sys.stdin.read().splitlines():
            parts = line.split()
            if len(parts) != 4:
                continue
            _, lsha, rref, rsha = parts
            if lsha == ZERO or not rref.startswith("refs/heads/"):
                continue  # deletes and tags: nothing to check
            base = base_ref if git_ok(repo, "rev-parse", "--verify", "-q", base_ref) else rsha
            if base == ZERO:
                continue
            problems += check_range(repo, base, lsha, allow, max_foreign)
    else:
        problems = check_range(repo, a.base, git(repo, "rev-parse", a.head).strip(), allow, max_foreign)
    for p in problems:
        print(f"prepush-check: REFUSED: {p}", file=sys.stderr)
    if problems:
        return 1
    print("prepush-check: ok")
    return 0
