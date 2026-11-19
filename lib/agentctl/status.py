# agentctl, part `status`: the status page.
# Runs inside agentctl's one namespace, after the parts before it in lib/agentctl/__init__.py's PARTS (it uses their names).

# ---------------------------------------------------------------- the status page (2026-10-29)
# The owner: "write a status page for the agentctl - all reds and agents assigned, status, progress, logs etc". The
# laptop's processor (the sync; phase 1's local processor too) serves a READ-ONLY page at http://agentctl.localhost/:
# 127.0.0.1:80, else 127.0.0.1:3015 (logged) when port 80 cannot be bound or something already listens on it (Rancher
# Desktop's ssh forward holds *:80 on this laptop: a loopback bind beside it would shadow it). *.localhost resolves to
# loopback in Chrome and curl without /etc/hosts. Routes: `/` (the page; it renders /api/status every 15 s),
# `/api/status` (JSON), `/log?path=<a .log/.out/.txt file under the state root>` (its tail). Only the Host names
# agentctl.localhost, 127.0.0.1 and localhost are answered (DNS rebinding). Every source is read in a thread of its own
# against a bound; a failing source is an error in its own section and never the page's or the tick's. It only reads:
# no `secrets` (the remote store's token is the one the sync already holds), no transitions, no TCC-protected folders,
# and lease secrets never leave the process.
STATUS_PAGE_HOST = "agentctl.localhost"
STATUS_PAGE_PORT = 80
STATUS_PAGE_FALLBACK = 3015
STATUS_PAGE_HOSTS = (STATUS_PAGE_HOST, "127.0.0.1", "localhost")
STATUS_SOURCE_BOUND = 8         # s a source may take ($AGENTCTL_STATUS_BOUND); the agents' source twice that
STATUS_STORE_TIMEOUT = 5        # s per remote store call
STATUS_CACHE = 5                # s an assembled status is served again
STATUS_LOG_TAIL = 40            # lines of each log shown in the page
STATUS_LOG_MAX = 2 << 20        # bytes /log serves (the file's tail)
STATUS_EVENTS = 50
STATUS_AGENT_RECENT = 3 * 3600  # s an ended fixer stays on the page
STATUS_AGENTS_MAX = 16
STATUS_LOG_SUFFIXES = (".log", ".out", ".txt")
STATUS_CHANGE_OPEN = ("queued", "checking", "checked", "pushing", "waiting")
STATUS_BATCH_OPEN = ("blocked-red", "assembling", "checking", "pushing", "bisecting", "held")


def status_store():
    """The store the page reads: the remote one with short timeouts when the laptop is the sync, else the local one."""
    url = store_url()
    return RemoteStore(url, timeout=STATUS_STORE_TIMEOUT) if url else Store()


def status_run_url(repo, number, run=None):
    """A run's page on the forge (GitHub links by the run's id: `run`, the run's record, carries it)."""
    return forge_run_url(repo or SHORT["movies"], number, run)


def status_iso_epoch(s):
    try:
        return dt.datetime.strptime(str(s), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc).timestamp()
    except (TypeError, ValueError):
        return None


def status_err(e):
    return {"error": f"{type(e).__name__}: {str(e).replace('agentctl: ', '')[:300]}"}


_status_last = process_cache({})      # source name -> (finished at, result, took s) of its last read (the server's own reads only)
_status_reading = process_cache({})   # source name -> the thread reading it


def status_sources(sources, bound, keep=False):
    """({name: what fn returned, or {"error": ...}}, {"stale": {name: why}, "took": {name: s}}): every (name, fn, factor)
    in a daemon thread of its own, joined against one deadline (bound * factor s each). keep (the server's): a source
    still reading then shows its LAST finished read, `stale` saying since when, and its read goes on in the background
    for the next refresh (one read per source at a time) -- a slow source never empties its section; else it is an
    error."""
    out, threads = {}, []

    def work(name, fn):
        t0 = time.time()
        try:
            v = fn()
        except (Exception, SystemExit) as e:
            v = status_err(e)
        out[name] = (time.time(), v, round(time.time() - t0, 2))
        if keep:
            _status_last[name] = out[name]
    start = time.time()
    for name, fn, factor in sources:
        th = _status_reading.get(name) if keep else None
        if not (th and th.is_alive()):
            th = threading.Thread(target=work, args=(name, fn), daemon=True, name=f"status:{name}")
            th.start()
            if keep:
                _status_reading[name] = th
        threads.append((name, th, bound * factor))
    for _name, th, b in threads:
        th.join(max(0.0, start + b - time.time()))
    res, stale, took = {}, {}, {}
    for name, th, b in threads:
        got = out.get(name) or (_status_last.get(name) if keep and not th.is_alive() else None)
        if got:
            res[name], took[name] = got[1], got[2]
        elif keep and name in _status_last:
            at, res[name], _took = _status_last[name]
            stale[name] = f"still reading after {b:.0f} s; shown as of {time.strftime('%H:%M:%S', time.localtime(at))}"
        else:
            res[name] = {"error": f"no answer within {b:.0f} s"}
    return res, {"stale": stale, "took": took}


def status_strip_lease(r, t):
    """A lease row for the page: never its secret."""
    if not r:
        return None
    m = lease_meta(r)
    return {"name": r.get("name"), "holder": r.get("holder") or None, "live": lease_is_live(r, t), "epoch": r.get("epoch"),
            "acquired_at": r.get("acquired_at"), "renewed_at": r.get("renewed_at"), "expires_at": r.get("expires_at"),
            "max_until": r.get("max_until"), "progress": r.get("progress"), "kind": m.get("kind"), "note": m.get("note"),
            "worktree": m.get("worktree"), "pid": m.get("pid"), "tier": m.get("tier"), "run": m.get("run"),
            "workflow": m.get("workflow"), "log": m.get("log")}


# ---- ONE STATUS MODEL (docs/simplification-2026-10.md item 4): status_data() is THE assembled status (what /api/status
# returns); `status --line`, `status`, `red status`, `ci-why --summary --cached` and the page render from it (or from
# the sections they need: status_data(only=...)). No second formatter reads state.json or the store for itself.
def status_gate_text(d, t, o_r=None, unreachable=False):
    """THE gate text, from the published summary in state.json d: the line itself while fresh; stale or missing, what
    it last said marked with its age -- `gate ?` when ci-runner's store does not answer (no verdict), `gate NETWORK`
    on a lossy uplink, else `gate UNREAD`. o_r: only that repository's line counts."""
    sm = d.get("summary") or {}
    try:
        epoch = int(sm["at"])
    except (KeyError, TypeError, ValueError):
        epoch = None
    line = sm.get("line") if epoch and GATE_LINE.match(sm.get("line") or "") and (not o_r or sm.get("repo") == o_r) else None
    age = t - epoch if epoch else None
    if line and -5 <= age <= GATE_SUMMARY_MAX_AGE:
        return line
    net = d.get("network") or {}
    lossy = net.get("at") and t - float(net["at"]) < NETWORK_SHOWN_FOR
    asof = time.strftime("%H:%M", time.localtime(epoch)) if epoch else "never"
    rest = line[5:] if line else None
    if unreachable:
        return f"gate ? (as of {asof}: {rest})" if rest else "gate ?"
    if lossy:
        return (f"gate NETWORK (lossy uplink: {int(float(net.get('loss') or 0) * 100)}% loss"
                + (f"; as of {asof}: {rest})" if rest else ")"))
    return f"gate UNREAD (as of {asof}: {rest or 'no fresh published gate'})"


def status_header(t, d=None, probes=True):
    """The cheap section every renderer starts from: state.json (on the sync refreshed from ci-runner's /v1/state when
    its mirror is old, at most one GET), the gate text, the reconciler's tick, the store's reachability, the waiting and
    checking changes, the open items, the pins. probes: also the tunnel's state and the store port it forwards (the page's)."""
    url = store_url()
    if d is None:
        d = remote_status_state(url, t) if url else state_read()
    ci = d.get("ci") or {}
    gate = []
    for g in ci.get("gate") or []:
        dec, new = g.get("decided") or {}, g.get("newer") or {}
        gate.append({"name": g.get("name"), "number": dec.get("number"), "conclusion": dec.get("conclusion"),
                     "url": status_run_url(ci.get("repo"), dec.get("number"), dec), "title": dec.get("displayTitle"),
                     "newer": new.get("number"), "newer_status": new.get("status"),
                     "newer_url": status_run_url(ci.get("repo"), new.get("number"), new), "since": g.get("since"),
                     "count": g.get("count")})
    tick = d.get("tick_at")
    fetched = float(d.get("fetched_at") or 0) if url else None
    unreachable = bool(url) and t - fetched > STORE_UNREACHABLE_AFTER
    h = {"gate": gate, "gate_red": any(x["conclusion"] in RED_CONCLUSIONS for x in gate),
         "gate_text": status_gate_text(d, t, unreachable=unreachable),
         "summary": (d.get("summary") or {}).get("line"), "summary_at": (d.get("summary") or {}).get("at"),
         "summary_repo": (d.get("summary") or {}).get("repo"),
         "ci_at": ci.get("at"), "repo": ci.get("repo") or SHORT["movies"],
         "tick_at": tick, "tick_age": t - float(tick) if tick else None, "fetched_at": d.get("fetched_at"),
         "store_url": url, "store_unreachable": unreachable, "store_error": d.get("store_error"),
         "code": d.get("version"), "rev": d.get("rev"), "network": d.get("network"), "waiting": d.get("waiting") or [],
         "queued": d.get("queued") or [], "hold": d.get("hold"),
         "items": {k: d.get(k) or [] for k in ("changes", "batch", "reds", "conditions")},
         "pins": pins_status(now_=t)}
    if probes:
        tunnel = read_json_file(tunnel_state_path())
        h["tunnel"] = {k: tunnel.get(k) for k in ("fails", "last_kick", "pending_since", "escalated", "network")}
        h["store_port"] = port_open(tunnel_ports()[0][0], timeout=1.0)
    h["line"] = render_status_line(h, t)
    return h


def render_status_line(h, t):
    """The status line: the gate text, the waiting and checking changes; `RECONCILER STALLED since HH:MM` when the
    tick is over 2 min old, `STORE UNREACHABLE since HH:MM` when ci-runner's store has not answered for a minute."""
    short = lambda wts: ", ".join(re.sub(r"^.*-wt-", "", os.path.basename(w or "")) for w in wts)
    hm = lambda x: time.strftime('%H:%M', time.localtime(float(x)))
    parts, tick, url = [h["gate_text"]], h["tick_at"], h["store_url"]
    if h["store_unreachable"]:  # no verdict on ci-runner's reconciler: its store did not answer (never STALLED)
        se = h.get("store_error") or {}
        parts.insert(0, f"STORE UNREACHABLE since {hm(float(h.get('fetched_at') or 0) or se.get('since') or t)} ({url}"
                        + (f": {se['why'][:80]}" if se.get("why") else "") + ")"
                        + (f"; ci-runner ticked at {hm(tick)}" if tick else ""))
    elif not tick or t - float(tick) > 120:
        parts.insert(0, (f"RECONCILER STALLED since {hm(tick)}" if tick else "RECONCILER not running")
                     + (" (ci-runner)" if url else ""))
    hold = h.get("hold")
    if hold and float(hold.get("until") or 0) > t:  # the store's land hold (expired ones lift at the next tick)
        parts.append("LAND " + hold_text(hold))
    waiting, queued = h["waiting"], h["queued"]
    parts.append(f"waiting: {len(waiting)}" + (f" — {short(waiting)}" if waiting else "")
                 + (f" · checking: {len(queued)} — {short(queued)}" if queued else ""))
    return " · ".join(parts)


def status_line(now_=None):
    """`status --line`: the header's line (no git, no GitHub; on the sync at most one GET /v1/state)."""
    t = now_ if now_ is not None else time.time()
    return status_header(t, probes=False)["line"]


def code_text(h):
    """The reconciler's code: its git revision first (`4d0fc921c9e6 (code d1677f40)`), the hash alone from a tick
    that predates `rev`."""
    code = str(h.get("code") or "?")
    return f"{h['rev']} (code {code[:8]})" if h.get("rev") else code


def render_status(h, t):
    """`agentctl status`: the line, the open items with their states and deadlines, the tick, the pins."""
    out = [h["line"]]
    for key in ("changes", "batch", "reds", "conditions"):
        rows = h["items"][key]
        out.append(f"{key}: {len(rows)}")
        out += [f"  {r['id']}  {r['state']}  deadline in {int((r['deadline'] - t) // 60)} min"
                + (f"  (level {r['level']})" if r.get("level") else "")
                + (f"  held: {r['hold']}" if r.get("hold") else "") for r in rows]
    try:  # parked worktrees: shown once here, never escalated (watchdog.py, PARKED)
        out += parked_lines()
    except (Exception, SystemExit) as e:
        out.append(f"parked: unread ({str(e)[:120]})")
    tick = h["tick_at"]
    out.append(f"tick: {int(t - tick)} s ago, code {code_text(h)}" if tick else "tick: never")
    out.append(h["pins"])
    return "\n".join(out)


def cmd_status(a):
    if getattr(a, "open", False):
        return status_page_open()
    t = time.time()
    h = status_header(t, probes=False)
    standby = None if on_ci_runner() else laptop_role_standby()  # ONE MACHINE HOLDS THE LAPTOP ROLE
    print((f"STANDBY: {standby}\n" if standby and not a.line else "") + (h["line"] if a.line else render_status(h, t)))
    return 0


def render_red_status(m, verbose=False):
    """`agentctl red status` from the model's reds, families and dispatcher sections."""
    reds = m["reds"]
    if isinstance(reds, dict) and "error" in reds:
        return f"reds: unread ({reds['error']})"
    out = [f"reds ({len(reds)} open):" if reds else "reds: none open"]
    for r in reds:
        out.append(f"  {r['state']:<16} {r['what']} {r['key']}" + (f" [{r['family']}]" if r.get("family") else "")
                   + f": {r['text']}")
        if verbose:
            out += [f"      {h.get('at')} {h.get('holder')}: {str(h.get('outcome'))[:140]}" for h in r["history"]]
    for f in m.get("families") or []:
        if f.get("holder") and f.get("at_work") is False:  # never "leased ... none at work": orphaned, re-offered
            who = f"ORPHANED LEASE ({f['holder']}: {f.get('work')}) -> revoked and re-offered at the next tick"
        elif f.get("holder"):
            who = (f"{f['holder']} ({str(f.get('worktree') or '?').replace(HOME, '~')})"
                   + (f": {f['work']}" if f.get("at_work") else ""))
        else:
            who = "none at work"
        out.append(f"family {f['family']}: {f['open']} open red(s), ONE fixer: {who}")
    for o in m.get("owners") or []:
        if o.get("worktree"):
            out.append(f"owner {o['family']}: {'ALIVE' if o['alive'] else 'not running'}, turn {o.get('turn') or 0}, "
                       f"{o.get('reds') or 0} red(s) in hand, {o.get('restarts') or 0} restart(s); "
                       f"{str(o['worktree']).replace(HOME, '~')}" + (f" (ended: {o['verdict']})" if o.get("ended") else ""))
    dp = m.get("dispatcher") or {}
    if "error" in dp:
        out.append(f"dispatcher: unread ({dp['error']})")
    else:
        out.append(f"dispatcher: {'OFF -- ' + dp['blocker'] if dp.get('blocker') else 'ON'}; fixers alive: "
                   f"{dp['alive']}/{dp['cap']}; gate reds never wait (no slot cap, no laptop pressure)")
        if dp.get("pressure"):
            out.append(f"laptop: {dp['pressure']} (holds other work only, never a red fix)")
    return "\n".join(out)


def status_families(st, t):
    """Each family with open reds: how many, and its ONE fixer (the live primary lease), if any."""
    active = red_active(st)
    fams = {}
    for it in active.values():
        f = red_family(it["data"])
        if f:
            fams[f] = fams.get(f, 0) + 1
    out = []
    leases = [r for r in st.list("red/") if lease_is_live(r, t)]
    cache = {}
    for f in sorted(fams):
        row = red_family_holder(st, f, t, active=active)
        if not row:  # an owner's (or any other) live lease on a red of the family is its holder too
            row = next((r for r in leases if (active.get(r["name"].split("/", 1)[1]) or {}).get("data")
                        and red_family(active[r["name"].split("/", 1)[1]]["data"]) == f), None)
        work, why = holder_work(row, t, cache) if row else (None, None)
        wt = lease_meta(row).get("worktree") if row else None
        if row and not wt and lease_meta(row).get("kind") in ("owned", "owner"):  # an owner's: its ONE worktree
            wt = (read_record(owner_record_path(lease_meta(row).get("family") or f)) or {}).get("worktree")
        out.append({"family": f, "open": fams[f], "holder": row["holder"] if row else None, "worktree": wt,
                    "at_work": work, "work": why})
    return out


def status_store_probe(st, t):
    """Is the store answering, and the reconciler's own tick (the remote /v1/state's tick_at) when it is remote."""
    t0 = time.time()
    if isinstance(st, RemoteStore):
        snap = st.state() or {}
        tick = snap.get("tick_at")
        return {"remote": st.url, "reachable": True, "ms": int((time.time() - t0) * 1000), "tick_at": tick,
                "tick_age": t - float(tick) if tick else None, "code": snap.get("version")}
    st.meta_get("last_alive", 0)
    return {"remote": None, "reachable": True, "ms": int((time.time() - t0) * 1000)}


def status_pressure():
    """The laptop's pressure reading, with its numbers (why it holds other work, else the plain reading)."""
    try:
        why, summary = pressure_report()
    except Exception as e:
        return f"unread ({e})"
    return f"UNDER PRESSURE: {why} [{summary}]" if why else summary


def status_dispatcher(st, t):
    return {"blocker": cached_dispatch_blocker(), "off_flag": os.path.exists(reds_dir("DISPATCH_OFF")),
            "alive": len(red_fixers_alive(st, t)), "cap": red_cap(), "pressure": status_pressure(),
            "escalated": [{"type": typ, "id": it["id"], "words": words} for typ, it, words in escalated_now(st)]}


def status_reds(st, t, repo=None):
    """Every open red: its machine state, its run, its holder and when the hold lapses, its last note."""
    leases = {r["name"]: r for r in st.list("red/")}
    out = []
    for it in sorted(red_active(st).values(), key=lambda i: (not i["data"].get("gate"), i["entered_at"])):
        d = it["data"]
        key = d.get("key") or it["id"]
        try:
            text, short = red_view(st, it, t)
        except (Exception, SystemExit) as e:
            text, short = f"(view failed: {str(e)[:120]})", it["state"]
        lease = status_strip_lease(leases.get(red_lease(key)), t)
        hist = d.get("history") or []
        lr = d.get("last_run") or {}
        opened = d.get("opened_at") or it["entered_at"]
        out.append({"key": key, "id": it["id"], "state": it["state"], "short": short, "text": text,
                    "workflow": d.get("workflow"), "run": lr.get("number"), "run_url": status_run_url(repo, lr.get("number"), lr),
                    "jobs": [str(j) for j in d.get("jobs") or [d.get("job")] if j], "line": d.get("line"),
                    "gate": bool(d.get("gate")), "kind": d.get("kind"), "red_kind": d.get("red_kind"), "tier": d.get("tier"),
                    "opened_at": opened, "age": t - float(opened), "entered_at": it["entered_at"],
                    "deadline": it["deadline"], "level": it.get("level"), "lease": lease,
                    "fix": d.get("fix"), "attempts": d.get("attempts"), "family": red_family(d),
                    "note": (lease or {}).get("note") or block_verdict_note(it, t, st) or red_history_note(it),
                    "what": red_what(d),
                    "history": [{"at": h.get("at"), "holder": h.get("holder"), "outcome": str(h.get("outcome"))[:300]}
                                for h in hist[-5:]]})
    return out


def red_history_note(it):
    """A red's last history entry as its note -- but for a fix-pending red, only one written since it went fix-pending:
    an older one is a holder's word from before the fix (2026-11-15, Order independence #74: both reds, fix-pending on
    09e482c6a with no holder, read "ORPHANED: laptop:owner:outside-gate holds it with no work" from 11:34)."""
    hist = it["data"].get("history") or []
    if not hist:
        return None
    if it["state"] == "fix-pending":
        try:
            if parse_time(hist[-1].get("at")).timestamp() < float(it["entered_at"]):
                return None
        except (ValueError, TypeError, AttributeError):
            return None
    return str(hist[-1].get("outcome"))[:400]


def tail_bytes(path, max_bytes):
    """(the last max_bytes of the file, its size). Raises OSError/TypeError."""
    with open(path, "rb") as f:
        f.seek(0, 2)
        size = f.tell()
        f.seek(max(0, size - max_bytes))
        return f.read(max_bytes), size


def status_log_body(path, max_bytes=None):
    """(/log's body, the file's size): the file's tail, and when it is short (just rotated), the tail of its newest
    rotated part (<path>.1) before it, marked."""
    max_bytes = max_bytes or STATUS_LOG_MAX
    body, size = tail_bytes(path, max_bytes)
    prev = f"{path}.1"
    if size < max_bytes and os.path.isfile(prev):
        older, _s = tail_bytes(prev, max_bytes - size)
        body = older + f"\n---- rotated: the above is the end of {os.path.basename(prev)} ----\n".encode() + body
    return body, size


def status_tail(path, lines=STATUS_LOG_TAIL, max_bytes=64 << 10):
    """The log's last lines, MASKED (live_mask_text): the page serves them."""
    try:
        data = tail_bytes(path, max_bytes)[0].decode("utf-8", "replace")
    except OSError:
        return None
    return live_mask_text("\n".join(data.splitlines()[-lines:]))


def status_transcript_last(path, max_bytes=256 << 10):
    """(the last assistant text, its time) of a claude transcript (.jsonl): what the agent last said it is doing."""
    try:
        lines = tail_bytes(path, max_bytes)[0].decode("utf-8", "replace").splitlines()
    except (OSError, TypeError):
        return None, None
    for ln in reversed(lines):
        try:
            rec = json.loads(ln)
        except ValueError:
            continue
        if rec.get("type") != "assistant":
            continue
        content = (rec.get("message") or {}).get("content")
        texts = [c.get("text") for c in content if isinstance(c, dict) and c.get("type") == "text" and c.get("text")] \
            if isinstance(content, list) else [content] if isinstance(content, str) else []
        if texts:
            return live_mask_text(texts[-1][-600:]), rec.get("timestamp")
    return None, None


def status_agent_logs(run):
    """The fixer's logs in its run's reds dir (fixer.log, claude-N.log, ...), newest first, each with its tail."""
    if not re.fullmatch(r"\d+", str(run or "")):  # a lease's meta (any store user's): never a path of its own
        return []
    files = [p for p in glob.glob(reds_dir(str(run), "*")) if p.endswith(STATUS_LOG_SUFFIXES) and os.path.isfile(p)]
    files.sort(key=mtime, reverse=True)
    return [{"path": p, "name": os.path.basename(p), "size": os.path.getsize(p), "mtime": mtime(p), "tail": status_tail(p)}
            for p in files[:4]]


# THE AGENTS' DETAILS ARE BOUNDED PER AGENT AND CACHED PER WORKTREE HEAD (2026-10-29: the section was "no answer within
# 16 s" on every refresh -- the processor runs at launchd's background QoS, where three git calls in each of 10 movies
# worktrees, one after the other, took 117 s; the same pass in a shell took 3 s). Each agent's details are read in a
# thread of their own, all at once; the page waits STATUS_AGENT_BOUND s for each and shows a slower one from its last
# read (marked stale) while the read finishes in the background for the next refresh (one read per worktree at a time).
# The commits and their files are read again only when the worktree's HEAD (read from its files, no git) moved; `git
# status` at most every STATUS_DIRTY_TTL s. Logs and transcripts are read by seeking from their end.
STATUS_AGENT_BOUND = 4      # s the page waits for one agent's details ($AGENTCTL_STATUS_AGENT_BOUND)
STATUS_GIT_TIMEOUT = 120    # s one git call may run (in its background read; never the page's wait)
STATUS_DIRTY_TTL = 60       # s a worktree's `git status` is shown again before it is read again
_status_wt = process_cache({})             # worktree -> {"head", "commits", "recent_files", "dirty", "dirty_at", "error", "at"}
_status_wt_reads = process_cache({})       # worktree -> the thread reading it
_status_said = process_cache({})           # transcript path -> ((size, mtime), (text, at))
_status_wt_lock = threading.Lock()


def status_head(wt):
    """The worktree's HEAD sha from its files (no git, no subprocess); None when it cannot be read that way."""
    try:
        gd = os.path.join(wt, ".git")
        if os.path.isfile(gd):
            with open(gd) as f:
                line = f.read().strip()
            if not line.startswith("gitdir:"):
                return None
            gd = os.path.normpath(os.path.join(wt, line[7:].strip()))
        common = gd
        if os.path.isfile(os.path.join(gd, "commondir")):
            with open(os.path.join(gd, "commondir")) as f:
                common = os.path.normpath(os.path.join(gd, f.read().strip()))
        with open(os.path.join(gd, "HEAD")) as f:
            head = f.read().strip()
        if not head.startswith("ref:"):
            return head if re.fullmatch(r"[0-9a-f]{40,64}", head) else None
        ref = head[4:].strip()
        for base in (gd, common):
            try:
                with open(os.path.join(base, ref)) as f:
                    return f.read().strip() or None
            except OSError:
                pass
        with open(os.path.join(common, "packed-refs")) as f:
            for ln in f:
                parts = ln.split()
                if len(parts) == 2 and parts[1] == ref:
                    return parts[0]
    except OSError:
        return None
    return None


def status_git(wt, *args, timeout=None):
    r = subprocess.run(["git", "-C", wt] + list(args), capture_output=True, text=True,
                       timeout=timeout or STATUS_GIT_TIMEOUT, stdin=subprocess.DEVNULL,
                       env=dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0"))
    return r.stdout if r.returncode == 0 else ""


def status_worktree_read(wt, head):
    """One background read of a worktree into _status_wt: the commits again only when HEAD moved (or is unknown)."""
    c = dict(_status_wt.get(wt) or {})
    try:
        if head is None or c.get("head") != head or "commits" not in c:
            commits = [dict(zip(("sha", "when", "subject"), ln.split("\x1f", 2)))
                       for ln in status_git(wt, "log", "-3", "--format=%h\x1f%cr\x1f%s").splitlines() if ln]
            recent = []
            for ln in status_git(wt, "log", "-3", "--name-only", "--format=").splitlines():
                if ln and ln not in recent:
                    recent.append(ln)
            c.update(head=head, commits=commits, recent_files=recent[:20])
        if time.time() - float(c.get("dirty_at") or 0) >= STATUS_DIRTY_TTL:
            c["dirty"] = [ln for ln in status_git(wt, "status", "--porcelain").splitlines() if ln][:20]
            c["dirty_at"] = time.time()
        c.pop("error", None)
    except (OSError, subprocess.SubprocessError) as e:
        c["error"] = str(e)[:200]
    c["at"] = time.time()
    with _status_wt_lock:
        _status_wt[wt] = c


def status_worktree(wt, bound):
    """{commits, recent_files, dirty} of a worktree: from the cache when its HEAD has not moved and its status is young;
    else from a read (one per worktree at a time) the caller waits `bound` s for -- a slower read is shown from the last
    one, `git_stale` saying so, and fills the cache for the next refresh."""
    head = status_head(wt)
    c = _status_wt.get(wt)
    fresh = c and "commits" in c and head is not None and c.get("head") == head \
        and time.time() - float(c.get("dirty_at") or 0) < STATUS_DIRTY_TTL
    if not fresh:
        with _status_wt_lock:
            th = _status_wt_reads.get(wt)
            if not (th and th.is_alive()):
                th = threading.Thread(target=status_worktree_read, args=(wt, head), daemon=True, name="status:git")
                _status_wt_reads[wt] = th
                th.start()
        th.join(bound)
        c = _status_wt.get(wt)
        if th.is_alive():
            out = {k: (c or {}).get(k) for k in ("commits", "recent_files", "dirty")}
            out["git_stale"] = (f"git still reading after {bound:.0f} s; shown from the read at "
                                f"{time.strftime('%H:%M:%S', time.localtime(c['at']))}" if c
                                else f"git still reading after {bound:.0f} s; shown at a later refresh")
            return out
    c = c or {}
    out = {k: c.get(k) for k in ("commits", "recent_files", "dirty")}
    if c.get("error"):
        out["git_error"] = c["error"]
    if c.get("head") != head:
        out["git_stale"] = "HEAD moved since the last read"
    return out


def status_said(path):
    """status_transcript_last, cached by the transcript's size and mtime."""
    try:
        sig = (os.path.getsize(path), mtime(path))
    except (OSError, TypeError):
        return None, None
    hit = _status_said.get(path)
    if hit and hit[0] == sig:
        return hit[1]
    v = status_transcript_last(path)
    _status_said[path] = (sig, v)
    return v


def status_agent_enrich(a, bound=None):
    b = bound if bound is not None else env_num("AGENTCTL_STATUS_AGENT_BOUND", STATUS_AGENT_BOUND)
    if a.get("alive") is None and a.get("pid"):
        try:
            a["alive"] = proc_alive(a["pid"])
        except (Exception, SystemExit):
            a["alive"] = None
    wt = a.get("worktree")
    if wt and os.path.isdir(wt):
        a.update({k: v for k, v in status_worktree(wt, b).items() if v is not None})
    a["logs"] = status_agent_logs(a.get("run"))
    transcript = live_transcript_ok(a.get("transcript"))  # a lease's meta: only a transcript the page may stream
    if transcript:
        a["said"], a["said_at"] = status_said(transcript)
    return a


def status_enrich_all(agents, bound=None):
    """Every agent's details at once, each in a thread of its own; the wait is one agent's bound (plus a margin), and an
    agent still unread then keeps its row with `detail_error`."""
    b = bound if bound is not None else env_num("AGENTCTL_STATUS_AGENT_BOUND", STATUS_AGENT_BOUND)
    done = set()

    def work(i, a):
        try:
            status_agent_enrich(a, b)
        except (Exception, SystemExit) as e:
            a["detail_error"] = f"{type(e).__name__}: {str(e)[:200]}"
        done.add(i)
    ths = [threading.Thread(target=work, args=(i, a), daemon=True, name="status:agent") for i, a in enumerate(agents)]
    for th in ths:
        th.start()
    end = time.time() + b + 2
    for th in ths:
        th.join(max(0.0, end - time.time()))
    for i, a in enumerate(agents):
        if i not in done:
            a.setdefault("detail_error", f"details not read within {b + 2:.0f} s")
    return agents


STATUS_RECORD_MAX_AGE = 8 * 3600  # s: an older fixer record (by mtime) is neither alive nor recently ended


def status_fixer_records(t):
    """fixer_records, of the records written in the last STATUS_RECORD_MAX_AGE s only (a fixer's wall time is bounded
    and its record is rewritten when it ends): ~150 old ones are never opened, nor their pids checked."""
    out = []
    for p in glob.glob(reds_dir("*", "fixer*.json")):
        if t - mtime(p) > STATUS_RECORD_MAX_AGE:
            continue
        r = read_record(p)
        if r:
            r["path"] = p
            r["alive"] = not r.get("ended") and proc_alive(r.get("pid"), r.get("process_start"))
            out.append(r)
    return out


def status_agents(st, t, records=None):
    """Every agent holding a red lease (fixer or claim) and every fixer dispatched (alive, or ended within
    STATUS_AGENT_RECENT): pid alive, worktree, tier, lease progress, its worktree's last commits, its logs."""
    agents = {}
    for r in st.list("red/"):
        if not lease_is_live(r, t):
            continue
        ls = status_strip_lease(r, t)
        k = f"pid:{ls['pid']}" if ls.get("pid") else f"holder:{r.get('holder')}"
        a = agents.setdefault(k, {"pid": ls.get("pid"), "holder": r.get("holder"), "kind": ls.get("kind"),
                                  "tier": ls.get("tier"), "workflow": ls.get("workflow"), "run": ls.get("run"),
                                  "worktree": ls.get("worktree"), "transcript": ls.get("log"), "leases": []})
        a["leases"].append(ls)
    for rec in records if records is not None else status_fixer_records(t):
        ended = status_iso_epoch(rec.get("ended"))
        if not rec.get("alive") and (not ended or t - ended > STATUS_AGENT_RECENT):
            continue
        k = f"pid:{rec.get('pid')}"
        a = agents.setdefault(k, {"pid": rec.get("pid"), "holder": None, "kind": "fixer", "leases": []})
        for f in ("workflow", "run", "worktree", "transcript"):
            a[f] = a.get(f) or rec.get(f)
        a["tier"] = a.get("tier") or str(rec.get("why") or "").replace("tier ", "") or None
        a["alive"] = a.get("alive") or bool(rec.get("alive"))  # fixer_records' own pid check
        a.update({"record": rec.get("path"), "started": rec.get("started"), "ended": rec.get("ended"), "rc": rec.get("rc"),
                  "verdict": rec.get("verdict"), "label": rec.get("label"), "key": rec.get("key"),
                  "jobs": rec.get("jobs")})
    out = []
    for a in agents.values():
        if not a.get("pid"):
            a["alive"] = False
        prog = [x.get("renewed_at") for x in a["leases"] if x.get("renewed_at")]
        a["last_progress"] = max(prog) if prog else None
        a["started"] = a.get("started") or (min(x["acquired_at"] for x in a["leases"] if x.get("acquired_at"))
                                            if any(x.get("acquired_at") for x in a["leases"]) else None)
        out.append(a)
    def started(a):
        s = a.get("started")
        return float(s) if isinstance(s, (int, float)) else status_iso_epoch(s) or 0.0
    out.sort(key=lambda a: (not a["leases"], not a.get("alive"), -started(a)))
    out = status_enrich_all(out[:STATUS_AGENTS_MAX])
    try:
        return live_annotate(out, t)
    except (Exception, SystemExit) as e:  # the live panes are extra: never the section's
        err(f"status page: live ids: {type(e).__name__}: {str(e)[:200]}")
        return out


def status_landing(st, t):
    """The open change items and the batch, each with its deadline (and whether it is past)."""
    def row(it):
        d = it["data"]
        return {"id": it["id"], "state": it["state"], "entered_at": it["entered_at"], "deadline": it["deadline"],
                "past_deadline": it["deadline"] < t, "level": it.get("level"), "worktree": d.get("worktree"),
                "branch": d.get("branch"), "head": str(d.get("head") or "")[:10] or None,
                "submitted_at": d.get("submitted_at"), "why": d.get("why") or d.get("reason"),
                "hold": (d.get("hold") or {}).get("why") if it["state"] in HOLD_SHOWN else None,
                "changes": d.get("changes") or d.get("entries")}
    return {"changes": [row(it) for it in st.items("change", list(STATUS_CHANGE_OPEN))],
            "batch": [row(it) for it in st.items("batch", list(STATUS_BATCH_OPEN))],
            "hold": hold_row(land_hold(st, t, expire=False))}


def status_conditions(st, t):
    out = []
    for c in open_conditions(st):
        lr = c.get("last_result") or {}
        out.append({"kind": c.get("kind"), "key": c.get("key"), "text": str(c.get("text") or "")[:400],
                    "opened": c.get("opened"), "level": c.get("level"), "until": c.get("until"),
                    "probe": (c.get("probe") or {}).get("kind"), "ok": lr.get("ok"), "detail": lr.get("detail"),
                    "last_checked": c.get("last_checked"), "next_check": c.get("next_check"), "fails": c.get("fails")})
    return out


def status_events(st, n=STATUS_EVENTS):
    """The last n events of the feed (from just before the projector's cursor on, a few pages at most)."""
    after = max(0, int(st.meta_get("projector_seq", 0) or 0) - n)
    evs = []
    for _page in range(5):
        got = st.events(after=after, limit=500)
        evs = (evs + got)[-n:]
        if len(got) < 500:
            break
        after = got[-1]["seq"]
    return [{"seq": e.get("seq"), "at": e.get("at"), "kind": e.get("kind"), "severity": e.get("severity"),
             "item": f"{e.get('item_type')}:{e.get('item_id')}" if e.get("item_type") else None,
             "from": e.get("from_state"), "to": e.get("to_state"), "text": str(e.get("text") or "")[:400]}
            for e in reversed(evs)]


def status_data(store=None, now_=None, bound=None, keep=False, only=None):
    """THE STATUS MODEL (/api/status): every section from its own bounded source; a failing one is {"error": ...} in
    its place. only: just these sections (a renderer's)."""
    t = now_ if now_ is not None else time.time()
    b = bound if bound is not None else env_num("AGENTCTL_STATUS_BOUND", STATUS_SOURCE_BOUND)
    d = state_read()
    repo = (d.get("ci") or {}).get("repo") or SHORT["movies"]
    st, st_err = None, None
    if not only or set(only) - {"header"}:
        try:
            st = store if store is not None else status_store()
        except (Exception, SystemExit) as e:
            st_err = status_err(e)

    def need():
        if st is None:
            raise RuntimeError(st_err["error"])
        return st
    sources = (("header", lambda: status_header(t), 1),
               ("store", lambda: status_store_probe(need(), t), 1),
               ("dispatcher", lambda: status_dispatcher(need(), t), 1),
               ("reds", lambda: status_reds(need(), t, repo), 1),
               ("families", lambda: status_families(need(), t), 1),
               ("owners", lambda: owners_status(need(), t), 1),
               ("agents", lambda: status_agents(need(), t), 2),
               ("landing", lambda: status_landing(need(), t), 1),
               ("conditions", lambda: status_conditions(need(), t), 1),
               ("issues", lambda: status_issues(need(), t), 1),
               ("events", lambda: status_events(need()), 1))
    res, meta = status_sources(tuple(x for x in sources if not only or x[0] in only), b, keep=keep)
    if "error" in res.get("store", {}):
        res["store"] = {**res["store"], "remote": store_url(), "reachable": False}
    return {"at": t, "repo": repo, "home": HOME, **res, **meta}


_status_cache = {"at": 0.0, "data": None}
_status_cache_lock = threading.Lock()


def status_cached():
    with _status_cache_lock:  # one assembly at a time: refreshing tabs share it
        if _status_cache["data"] is None or time.time() - _status_cache["at"] >= STATUS_CACHE:
            _status_cache["data"] = status_data(keep=True)
            _status_cache["at"] = time.time()
        return _status_cache["data"]


def status_host_ok(host):
    """Only agentctl.localhost, 127.0.0.1 and localhost (any port): another name pointing here is a rebinding page."""
    h = (host or "").strip().lower()
    if not h or h.startswith("["):
        return False
    name, _, port = h.partition(":")
    return name in STATUS_PAGE_HOSTS and (not port or port.isdigit())


def status_log_path(p):
    """The real path of a log /log may serve: a regular .log/.out/.txt file under the state root or the reds dir (after
    symlinks: no traversal out); else None."""
    if not p or "\0" in p or not os.path.isabs(os.path.expanduser(p)):
        return None
    real = os.path.realpath(os.path.expanduser(p))
    if not (real.endswith(STATUS_LOG_SUFFIXES) or re.search(r"\.log\.\d+$", real)) or not os.path.isfile(real):
        return None
    for root in {os.path.realpath(state_root()), os.path.realpath(reds_dir())}:
        if real.startswith(root.rstrip(os.sep) + os.sep):
            return real
    return None


def status_page_server(host, port):
    """The page's HTTP server (not started)."""
    import http.server

    class Handler(http.server.BaseHTTPRequestHandler):
        server_version = "agentctl-status"

        def log_message(self, *_a):
            pass

        def send(self, code, body, ctype):
            data = body.encode() if isinstance(body, str) else body
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.guard_headers()
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)

        def guard_headers(self):
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'unsafe-inline'; "
                             "style-src 'unsafe-inline'; connect-src 'self'; base-uri 'none'; form-action 'none'")

        def do_POST(self):  # read-only: GET (and HEAD) alone, so no request can change anything (no CSRF to guard)
            self.send(405, "read-only: GET only\n", "text/plain; charset=utf-8")
        do_PUT = do_DELETE = do_PATCH = do_POST

        def stream(self, u):
            q = urllib.parse.parse_qs(u.query)
            e = live_lookup((q.get("agent") or [""])[0])
            if not e:
                return self.send(404, "no such agent: an id from the page's Agents section\n", "text/plain; charset=utf-8")
            start = (self.headers.get("Last-Event-ID") or (q.get("from") or ["0"])[0] or "0").strip()
            if not start.isdigit() or len(start) > 15:
                return self.send(400, "from: a byte offset\n", "text/plain; charset=utf-8")
            if self.command == "HEAD":
                return self.send(200, "", "text/event-stream")
            if not _live_streams.acquire(blocking=False):
                return self.send(503, f"{LIVE_STREAMS} live streams already open\n", "text/plain; charset=utf-8")
            try:
                self.close_connection = True
                self.connection.settimeout(LIVE_SEND_TIMEOUT)  # a client that does not take a frame is dropped
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("X-Accel-Buffering", "no")
                self.guard_headers()
                self.end_headers()

                def write(b):
                    self.wfile.write(b)
                    self.wfile.flush()
                live_stream(e, int(start), write, closed=lambda: live_peer_closed(self.connection))
            except OSError:  # gone, or too slow to take a frame
                pass
            finally:
                _live_streams.release()

        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            try:
                if not status_host_ok(self.headers.get("Host")):
                    return self.send(403, "forbidden host\n", "text/plain; charset=utf-8")
                u = urllib.parse.urlsplit(self.path)
                if u.path == "/":
                    return self.send(200, STATUS_PAGE_HTML, "text/html; charset=utf-8")
                if u.path == "/api/status":
                    return self.send(200, json.dumps(status_cached(), default=str), "application/json")
                if u.path == "/stream":
                    return self.stream(u)
                if u.path.startswith("/agent/"):
                    if not live_lookup(urllib.parse.unquote(u.path[len("/agent/"):])):
                        return self.send(404, "no such agent: an id from the page's Agents section\n",
                                         "text/plain; charset=utf-8")
                    return self.send(200, AGENT_PAGE_HTML, "text/html; charset=utf-8")
                if u.path == "/log":
                    q = urllib.parse.parse_qs(u.query).get("path") or [""]
                    real = status_log_path(q[0])
                    if not real:
                        return self.send(403, "only a .log/.out/.txt file under ~/.cache/agentctl\n",
                                         "text/plain; charset=utf-8")
                    body, size = status_log_body(real)
                    body = live_mask_text(body.decode("utf-8", "replace")).encode()
                    head = f"{real} ({size} bytes" + (f"; the last {STATUS_LOG_MAX} shown" if size > STATUS_LOG_MAX else "") + ")\n\n"
                    return self.send(200, head.encode() + body, "text/plain; charset=utf-8")
                return self.send(404, "not found\n", "text/plain; charset=utf-8")
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as e:  # never the processor's: one request's error is that request's
                try:
                    self.send(500, f"error: {type(e).__name__}: {str(e)[:300]}\n", "text/plain; charset=utf-8")
                except OSError:
                    pass

    class Server(http.server.ThreadingHTTPServer):
        daemon_threads = True
        allow_reuse_address = True
    return Server((host, port), Handler)


_status_page = process_cache({})


def status_page_file():
    return state_root("status-page.json")


def status_page_start():
    """Serve the page from a daemon thread of this process, once: 127.0.0.1:80, else :3015 ($AGENTCTL_STATUS_PORT: that
    port alone; AGENTCTL_STATUS_PAGE=off: none). Writes status-page.json (its URL, for `status --open`). Returns the URL
    or None. Never raises."""
    if _status_page.get("url"):
        return _status_page["url"]
    if os.environ.get("AGENTCTL_STATUS_PAGE", "").lower() in ("0", "off", "no"):
        return None
    ports = ([int(os.environ["AGENTCTL_STATUS_PORT"])] if os.environ.get("AGENTCTL_STATUS_PORT")
             else [STATUS_PAGE_PORT, STATUS_PAGE_FALLBACK])
    why = []
    for p in ports:
        try:
            if p == STATUS_PAGE_PORT and port_open(p, timeout=0.5):
                why.append(f"port {p}: something already listens on it (not shadowed)")
                continue
            srv = status_page_server("127.0.0.1", p)
        except (OSError, ValueError) as e:
            why.append(f"port {p}: {e}")
            continue
        threading.Thread(target=srv.serve_forever, daemon=True, name="status-page").start()
        threading.Thread(target=status_warm, daemon=True, name="status-warm").start()  # the first refresh has data
        url = f"http://{STATUS_PAGE_HOST}/" if p == 80 else f"http://{STATUS_PAGE_HOST}:{p}/"
        _status_page.update(url=url, server=srv)
        if why:
            err(f"status page: {'; '.join(why)}: serving {url} instead")
        err(f"status page: {url} (127.0.0.1:{p})")
        try:
            write_json(status_page_file(), {"url": url, "port": p, "pid": os.getpid(), "at": time.time(), "fallback": why})
        except OSError as e:
            err(f"status page: {status_page_file()} not written: {e}")
        return url
    err(f"status page: not served ({'; '.join(why)})")
    return None


def status_warm():
    try:
        status_cached()
    except (Exception, SystemExit) as e:
        err(f"status page: the first read failed: {str(e)[:200]}")


def status_page_url():
    return read_json_file(status_page_file()).get("url") or f"http://{STATUS_PAGE_HOST}/"


def status_page_open():
    url = status_page_url()
    print(url)
    subprocess.run([os.environ.get("AGENTCTL_OPEN", "open"), url], capture_output=True, timeout=30)
    return 0


# ONE agentctl EDITOR (docs/simplification-2026-10.md, Agent B item 4): an agent that meets an agentctl defect -- a red
# fixer above all, which never edits ~/bin (the brief says so; ~/bin's pre-commit hook refuses its commits) -- files it
# with `agentctl issue "<text>"`. An issue is a store item (type `issue`): open until the maintainer closes it, shown
# on the status page, escalated each week it stays open.
MACHINES["issue"] = {
    "open": StateSpec(7 * 86400, "open", ("closed",), escalate=True, wait=True),
    "closed": StateSpec(ARCHIVE, "closed", (), terminal=True),
}
ISSUE_TEXT_MAX = 4000


def issue_open(st, text, by, cwd=None, now_=None):
    """A new open issue; returns the item."""
    t = now_ if now_ is not None else time.time()
    text = str(text or "").strip()[:ISSUE_TEXT_MAX]
    iid = f"{dt.datetime.fromtimestamp(t, dt.timezone.utc):%Y%m%dT%H%M%S}-{hashlib_sha1(f'{by}|{text}|{t}')[:6]}"
    return st.put_item("issue", iid, "open", {"text": text, "by": by, "at": ts(), "cwd": cwd,
                                              "session": os.environ.get("CLAUDE_CODE_SESSION_ID")})


def issue_close(st, iid, note, by):
    it = st.item("issue", iid)
    if not it:
        return None
    if it["state"] != "closed":
        st.transition("issue", iid, "closed", it["version"], data={"closed_by": by, "note": note, "closed_at": ts()},
                      event={"text": f"issue {iid} closed by {by}: {note}"[:400]})
    return st.item("issue", iid)


# THE WEEKLY ESCALATION (the owner, 2026-11-11, issue 20261111T143441-75a957: ONE NOTIFICATION PER ISSUE, never a
# digest). An open issue past its deadline re-enters `open`: one escalate event naming its id, filer and text, its
# level up, its deadline a week on. One issue per ISSUE_ESCALATE_GAP, the most overdue first: a backlog (69 overdue on
# 2026-11-12, none ever escalated) reaches the notifier one at a time -- more than NOTIFY_BURST in one notifier pass
# become one summary -- and their weekly deadlines stay that far apart afterwards.
ISSUE_ESCALATE_GAP = 120  # s between two issues' escalations ($AGENTCTL_ISSUE_ESCALATE_GAP)


def issue_escalation_text(it, t):
    d = it.get("data") or {}
    return (f"AGENTCTL ISSUE {it['id']} open {fmt_age(t - float(it.get('entered_at') or t))} "
            f"(filed by {d.get('by') or '?'}): {d.get('text', '')}")[:400]


def tick_issues(st=None, now_=None):
    """The reconciler's step: the most overdue open issue escalated, at most one per ISSUE_ESCALATE_GAP. Returns what
    it did."""
    st = st or Store()
    t = now_ if now_ is not None else st.clock()
    if t - float(st.meta_get("issue_escalated_at", 0) or 0) < env_num("AGENTCTL_ISSUE_ESCALATE_GAP", ISSUE_ESCALATE_GAP):
        return []
    due = sorted((i for i in st.items("issue", ("open",)) if i["deadline"] <= t), key=lambda i: i["deadline"])
    for it in due:
        try:
            st.transition("issue", it["id"], "open", it["version"],
                          event={"text": issue_escalation_text(it, t), "refs": {"issue": it["id"]}})
        except (StaleVersion, TerminalState):
            continue  # closed or moved meanwhile: the next one
        st.meta_set("issue_escalated_at", t)
        return [f"issue {it['id']}: escalated (level {it['level'] + 1})"]
    return []


def status_issues(st, t):
    return [{"id": i["id"], "text": i["data"].get("text", "")[:1000], "by": i["data"].get("by"),
             "at": i["data"].get("at"), "opened": i.get("entered_at"), "level": i.get("level"),
             "cwd": i["data"].get("cwd")}
            for i in sorted(st.items("issue", ("open",)), key=lambda i: i.get("entered_at") or 0, reverse=True)]


def cmd_issue(a):
    """agentctl issue "<text>" | --list | --close ID [--note N]"""
    st = Store()
    by = claimant()
    try:
        if a.close:
            it = issue_close(st, a.close, a.note or "fixed", by)
            if not it:
                print(f"issue {a.close}: no such issue")
                return 1
            print(f"issue {it['id']}: {it['state']}")
            return 0
        if a.list or not a.text:
            items = st.items("issue", None if a.all else ("open",))
            for i in sorted(items, key=lambda i: i["id"]):
                print(f"{i['id']}  {i['state']:6}  {i['data'].get('by') or '?'}: {i['data'].get('text', '')[:300]}")
            if not items:
                print("no open agentctl issues" if not a.all else "no agentctl issues")
            return 0
        it = issue_open(st, " ".join(a.text), by, cwd=safe_cwd())
    except (ValueError, StoreUnavailable) as e:
        why = str(e).replace("agentctl: ", "")[:200]
        if "machine 'issue'" in why:  # the server runs a pinned revision from before `issue`
            why += "; ci-runner's agentctl pin predates `issue` (a pin bump fixes it)"
        print(f"agentctl issue: NOT FILED: the store refused it ({why})")
        return 2
    print(f"ISSUE {it['id']} filed: the agentctl maintainer sees it on the status page and in `agentctl issue --list`")
    return 0
