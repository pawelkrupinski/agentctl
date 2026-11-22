# agentctl, part `reverts`: a reverted change gets back to the agent that wrote it.
# Runs inside agentctl's one namespace, after the parts before it in lib/agentctl/__init__.py's PARTS (it uses their names).

# ---------------------------------------------------------------- THE REVERT NOTICE (the owner, 2026-11-21)
# 604134d1a landed through `agentctl land`, turned Main red (#2806-#2808), and a red fixer reverted it as 7380cf9d7
# (`Fixes-Red: 2808`, its analysis in the message). Nobody told the agent that wrote it, nor the session that spawned
# that agent: the rework started only when the owner asked. Now every commit on main that reverts others (git's `This
# reverts commit <sha>` line, or a `Reverts: <sha>` trailer) is mapped to the LANDING that brought each reverted commit
# in (the lander records, lander.py: main sha, patch-id, subject and author time) and opens a `revert` item:
#
# - its LANDER is told: an inbox entry owned by its session (audience author, `REVERTED: ...`) and, while its worktree
#   is on the laptop, REVERTED.md in it (the laptop's sync, excluded from commits);
# - its ORCHESTRATOR -- the session that spawned it when the lander was a subagent (the session id names it), else the
#   owner -- gets an owner-audience entry (`REVERT NOTICE: ...`), the one `inbox wait` wakes on;
# - each carries the reverted sha and subject, the revert's sha, the red it fixed and the revert's whole message (its
#   analysis), and asks for one reply: `agentctl revert ack <id> --rework|--drop`.
#
# It stays OPEN until acked, or until main carries the change again: a commit with `Rework-Of: <sha>` (the reverted
# commit's or the revert's), a revert of the revert, or blame's `Relands-Change: <change>`. Unacked past
# REVERT_ACK_MAX it escalates ONCE to the owner (`REVERT UNACKED: ...`, a notification), then waits quietly until it
# expires. A NOTIFICATION, never a hold or a reject: it blocks no change, so it is no POLICIES row and needs no owner
# decision (policy.py) -- it is a machine of its own, ticked by the reconciler.
REVERT_ACK_MAX = 6 * 3600     # s an unacknowledged notice waits before it escalates to the owner
REVERT_LOOKBACK = 2 * 86400   # s of main's history the first scan (no cursor yet) reads
REVERT_SCAN_MAX = 300         # commits one scan reads at most
REVERT_NOTE = "REVERTED.md"   # the note in the lander's worktree
REVERT_LINE = re.compile(r"^This reverts commit ([0-9a-f]{7,40})", re.M)
REVERT_TRAILERS = ("Reverts", "Rework-Of", "Relands-Change")
MACHINES["revert"] = {
    "open": StateSpec(REVERT_ACK_MAX, "escalated", ("acked", "reworked", "escalated")),
    "escalated": StateSpec(ARCHIVE, "expired", ("acked", "reworked", "expired")),
    "acked": StateSpec(ARCHIVE, "acked", (), terminal=True),
    "reworked": StateSpec(ARCHIVE, "reworked", (), terminal=True),
    "expired": StateSpec(ARCHIVE, "expired", (), terminal=True),
}
REVERT_OPEN = ("open", "escalated")


def revert_scan(repo, since, main, now_):
    """[{sha, subject, body, reverts, rework_of, relands}] of the commits since..main, oldest first: since None reads
    the last REVERT_LOOKBACK s. Trailers are read as git reads them (the final block only)."""
    rng = [f"{since}..{main}"] if since else [f"--since=@{int(now_ - REVERT_LOOKBACK)}", main]
    fmt = "%x1e%H%x1f%s%x1f%B%x1f" + "%x1f".join(f"%(trailers:key={k},valueonly,separator=%x2C)" for k in REVERT_TRAILERS)
    r = git(repo, "log", "--no-merges", f"--max-count={REVERT_SCAN_MAX}", f"--format={fmt}", *rng, check=False)
    if r.returncode != 0:
        return None
    out = []
    for chunk in r.stdout.split("\x1e")[1:]:
        p = chunk.split("\x1f")
        if len(p) < 6:
            continue
        sha, subject, body, reverts, rework, relands = (x.strip() for x in p[:6])
        out.append({"sha": sha, "subject": subject, "body": body,
                    "reverts": REVERT_LINE.findall(body) + sorted(trailer_values(reverts)),
                    "rework_of": trailer_values(rework), "relands": trailer_values(relands)})
    return list(reversed(out))


def revert_resolve(repo, sha):
    """(full sha, its message) of a commit this clone has, or (None, None)."""
    r = git(repo, "log", "-1", "--format=%H%x1f%B", f"{sha}^{{commit}}", "--", check=False)
    if r.returncode != 0 or "\x1f" not in r.stdout:
        return None, None
    full, body = r.stdout.split("\x1f", 1)
    return full.strip(), body


def revert_landings(repo, c, idx):
    """The landings commit c reverts: [(record or None, [{sha, subject}])], one per landing (None: no landing record --
    the owner's commit, or one older than the records). A reverted commit that is itself a revert is a RELAND, never a
    notice."""
    groups = {}
    for ref in dict.fromkeys(c["reverts"]):
        full, body = revert_resolve(repo, ref)
        if not full or REVERT_LINE.search(body or ""):
            continue
        at, _, subject = git(repo, "log", "-1", "--format=%at%x1f%s", full, check=False).stdout.strip().partition("\x1f")
        key = {"sha": full, "patch_id": patch_ids(repo, [full]).get(full), "subject": subject[:200],
               "at": int(at) if at.isdigit() else None}
        rec = lander_of_commit(idx, key)
        g = groups.setdefault((rec or {}).get("change"), [rec, []])
        g[1].append({"sha": full, "subject": key.get("subject") or ""})
    return list(groups.values())


def revert_matches(ref, sha):
    return bool(ref and sha) and len(ref) >= 7 and sha.startswith(ref.lower())


def revert_closer(it, c):
    """How commit c re-lands revert item it (`Rework-Of`, a revert of the revert, `Relands-Change`), or None."""
    d = it["data"] or {}
    shas = [d.get("revert_sha")] + [x["sha"] for x in d.get("reverted") or []]
    if any(revert_matches(ref, s) for ref in c["rework_of"] for s in shas):
        return f"Rework-Of: in {c['sha'][:10]}"
    if any(revert_matches(ref, d.get("revert_sha")) for ref in c["reverts"]):
        return f"{c['sha'][:10]} reverts the revert"
    if d.get("change") and d["change"] in c["relands"]:
        return f"Relands-Change: {d['change']} in {c['sha'][:10]}"
    return None


def revert_who(lander):
    """(the lander's audience, the orchestrator's session or None): a subagent's session id names the session that
    spawned it; a top-level session's spawner is not recorded -- the owner."""
    lander = lander or {}
    aud = {k: lander[k] for k in ("session", "worktree", "claude_pid") if lander.get(k)}
    return aud, (lander.get("session") if lander.get("agent") else None)


def revert_texts(iid, d):
    """(the lander's verdict, the orchestrator's verdict, the tail both carry)."""
    what = ", ".join(f"{x['sha'][:10]} \"{x['subject'][:80]}\"" for x in d["reverted"])
    red = f" (Fixes-Red: {', '.join(d['fixes'])})" if d.get("fixes") else ""
    ask = f"rework or drop: reply `~/bin/agentctl revert ack {iid} --rework|--drop --note \"...\"`"
    by = f"{d['revert_sha'][:10]}{red}"
    lander = (f"REVERTED: your {what} was reverted on main by {by} -- read its analysis (below, or `git show "
              f"{d['revert_sha'][:10]}`); {ask}; a re-land says `Rework-Of: {d['reverted'][0]['sha'][:10]}`")
    whose = lander_name(d.get("lander")) if d.get("lander") else "nobody's landing (no record: the owner's, or older)"
    orch = f"REVERT NOTICE: {what}, landed by {whose}, was reverted by {by}; {ask} (or route it to its lander)"
    tail = [f"revert {d['revert_sha']} {d.get('revert_subject', '')}"] + str(d.get("revert_message") or "").splitlines()
    return lander, orch, tail[:40]


def revert_open(st, c, rec, reverted, t):
    """The revert item of landing rec reverted by commit c, and its notices (the lander's, the orchestrator's). None
    when it exists already."""
    iid = f"{c['sha'][:12]}-{reverted[0]['sha'][:12]}"
    if st.item("revert", iid):
        return None
    lander = (rec or {}).get("lander") or None
    d = {"revert_sha": c["sha"], "revert_subject": c["subject"], "revert_message": c["body"][:4000],
         "fixes": sorted(fixes_red_in_message(c["body"])), "reverted": reverted, "change": (rec or {}).get("change"),
         "lander": lander, "worktree": (lander or {}).get("worktree") or (rec or {}).get("worktree"),
         "blame": bool(re.search(r"^Reverts-Change:", c["body"], re.M)), "opened_at": t}
    it = st.put_item("revert", iid, "open", d)
    lv, ov, tail = revert_texts(iid, d)
    aud, orch = revert_who(lander)
    v = it["version"]
    if aud:
        v = st.transition("revert", iid, "open", v, event={
            "kind": "revert.notice", "text": lv, "severity": "info", "audience": aud, "dedupe": f"revert:{iid}:lander",
            "refs": {"verdict": lv, "tail": tail, "command": "revert", "exit": 1, "alert_item": f"revert:{iid}"}})
    st.transition("revert", iid, "open", v, event={
        "kind": "revert.notice", "text": ov, "severity": "info", "dedupe": f"revert:{iid}:owner",
        "audience": {"to": "owner", **({"session": orch} if orch else {})},
        "refs": {"verdict": ov, "tail": tail, "command": "revert", "exit": 1, "alert_item": f"revert-owner:{iid}"}})
    return iid


def revert_close(st, it, how, by):
    try:
        st.transition("revert", it["id"], "reworked", it["version"], data={"reworked_by": by, "how": how, "closed_at": ts()},
                      event={"text": f"revert {it['id']}: reworked ({how})", "severity": "info"})
        return True
    except (StaleVersion, TerminalState, IllegalTransition):
        return False


def tick_reverts(repo, st=None, now_=None, o_r=None):
    """The reconciler's step: main's new commits scanned (re-lands close notices, reverts open them), then each notice
    past its deadline moved (open -> escalated, once, to the owner; escalated -> expired, quietly). Its lines."""
    st = st or Store()
    t = now_ if now_ is not None else st.clock()
    o_r = o_r or RED_PRIMARY
    out = []
    main = git(repo, "rev-parse", "-q", "--verify", f"refs/remotes/{remote()}/main", check=False).stdout.strip() \
        if repo else ""
    cur_key = f"revert_scan:{o_r}"
    cur = st.meta_get(cur_key) or None
    if main and main != cur:
        if cur and not git_ok(repo, "merge-base", "--is-ancestor", cur, main):
            cur = None  # main rewritten under the cursor: read the lookback again (items dedupe)
        commits = revert_scan(repo, cur, main, t)
        if commits is not None:
            idx = None
            for c in commits:
                for it in st.items("revert", REVERT_OPEN):
                    how = revert_closer(it, c)
                    if how and revert_close(st, it, how, c["sha"]):
                        out.append(f"revert {it['id']}: reworked ({how})")
                if not c["reverts"]:
                    continue
                idx = lander_index(lander_records(st, o_r)) if idx is None else idx
                for rec, reverted in revert_landings(repo, c, idx):
                    iid = revert_open(st, c, rec, reverted, t)
                    if iid:
                        out.append(f"revert {iid}: {c['sha'][:10]} reverts {', '.join(x['sha'][:10] for x in reverted)}"
                                   f" -- told {lander_name(rec.get('lander')) if rec and rec.get('lander') else 'the owner'}")
            st.meta_set(cur_key, main)
    out += revert_deadlines(st, t)
    return out


def revert_deadlines(st, t):
    out = []
    for it in st.items("revert", REVERT_OPEN):
        if it["deadline"] > t:
            continue
        d = it["data"] or {}
        try:
            if it["state"] == "open":
                lv, _ov, tail = revert_texts(it["id"], d)
                v = (f"REVERT UNACKED: nobody acknowledged {fmt_age(t - float(it.get('entered_at') or t))} after it was "
                     f"told: " + lv[len("REVERTED: "):].replace("your ", "", 1))
                st.transition("revert", it["id"], "escalated", it["version"], event={
                    "text": v, "severity": "escalate", "audience": {"to": "owner"},
                    "refs": {"verdict": v, "tail": tail, "command": "revert", "exit": 1,
                             "alert_item": f"revert-owner:{it['id']}/escalated"}})
                out.append(f"revert {it['id']}: unacked past its deadline -> escalated to the owner")
            else:
                st.transition("revert", it["id"], "expired", it["version"],
                              event={"text": f"revert {it['id']}: never acknowledged, expired", "severity": "info"})
                out.append(f"revert {it['id']}: expired")
        except (StaleVersion, TerminalState, IllegalTransition):
            continue
    return out


def revert_closed_text(d):
    """How a closed notice was closed: who acknowledged it, when, how and with what note -- or what re-landed it."""
    if d.get("reworked_by"):
        return f"re-landed by {str(d['reworked_by'])[:10]} at {d.get('closed_at')} ({d.get('how')})"
    if d.get("acked_by"):
        return (f"acknowledged by {d['acked_by']} at {d.get('closed_at')} ({d.get('how')})"
                + (f": {d['note']}" if d.get("note") else ""))
    return "never acknowledged"


def revert_ack(st, ref, how, note, by):
    """Acknowledge the notice whose id starts with ref. (item or None, message). A notice closed already (acked by
    its orchestrator, re-landed) says by whom, when and how, and keeps this reply's note beside its own."""
    every = [i for i in st.items("revert") if i["id"].startswith(ref)]
    hits = [i for i in every if i["state"] in REVERT_OPEN] or every
    if len(hits) != 1:
        return None, f"revert {ref}: {'no notice' if not hits else f'{len(hits)} notices'} with that id"
    it = hits[0]
    if it["state"] not in REVERT_OPEN:
        d = it["data"] or {}
        reply = {"by": by, "how": how, "note": note, "at": ts()}
        try:
            st.update_item("revert", it["id"], it["version"], {"replies": (d.get("replies") or []) + [reply]})
        except StaleVersion:
            pass
        return st.item("revert", it["id"]), (f"revert {it['id']}: already {it['state']} -- {revert_closed_text(d)}; "
                                             f"your reply ({how}) is noted beside it")
    st.transition("revert", it["id"], "acked", it["version"],
                  data={"acked_by": by, "how": how, "note": note, "closed_at": ts()},
                  event={"text": f"revert {it['id']} acknowledged by {by}: {how}" + (f" ({note})" if note else ""),
                         "severity": "info"})
    return st.item("revert", it["id"]), f"revert {it['id']}: acknowledged ({how})"


def revert_line(r, t):
    """One notice, for `status`, `red status` and `revert`: an open one's age, a closed one's ack (or re-land) and
    every later reply."""
    d = r.get("data") or r
    what = ", ".join(x["sha"][:10] for x in d.get("reverted") or [])
    who = lander_name(d["lander"]) if d.get("lander") else "the owner"
    head = (f"  {r['id']}  {r['state']}  {what} reverted by {str(d.get('revert_sha'))[:10]}"
            + (f" (Fixes-Red: {', '.join(d['fixes'])})" if d.get("fixes") else "") + f" -- told {who}")
    if r["state"] in REVERT_OPEN:
        return head + ", unacked " + fmt_age(t - float(d.get("opened_at") or t))
    return "\n".join([f"{head}; {revert_closed_text(d)}"]
                     + [f"      reply by {x.get('by')} at {x.get('at')} ({x.get('how')})"
                        + (f": {x['note']}" if x.get("note") else "") for x in d.get("replies") or []])


def revert_brief(i):
    d = i["data"] or {}
    return {"id": i["id"], "state": i["state"], "deadline": i["deadline"], "level": i["level"],
            **{k: d.get(k) for k in ("revert_sha", "reverted", "fixes", "lander", "opened_at", "acked_by", "how", "note",
                                     "closed_at", "reworked_by", "replies")}}


def revert_note_text(iid, d):
    lv, _ov, tail = revert_texts(iid, d)
    return f"# {ts()} {lv}\n\n" + "\n".join(f"    {x}" for x in tail) + "\n\n"


def revert_notes(st=None):
    """The laptop's step: REVERTED.md in each told lander's worktree that is on this machine (once per notice), kept
    out of commits by the clone's info/exclude. Its lines."""
    st = st or Store()
    out = []
    for it in st.items("revert", REVERT_OPEN):
        d = it["data"] or {}
        wt = d.get("worktree")
        if d.get("noted") or not wt or not allowed_path(wt) or not os.path.isdir(wt):
            continue
        try:
            worktree_exclude(wt, [REVERT_NOTE])
            with open(os.path.join(wt, REVERT_NOTE), "a") as f:
                f.write(revert_note_text(it["id"], d))
            st.update_item("revert", it["id"], it["version"], {"noted": ts()})
            out.append(f"revert {it['id']}: noted in {wt.replace(HOME, '~')}/{REVERT_NOTE}")
        except (OSError, StaleVersion) as e:
            out.append(f"revert {it['id']}: note not written ({str(e)[:120]})")
    return out


def cmd_revert(a):
    """agentctl revert [list] [--all] | ack ID --rework|--drop [--note N]"""
    st = Store()
    t = time.time()
    try:
        if a.rcmd == "ack":
            it, msg = revert_ack(st, a.id, "drop" if a.drop else "rework", a.note, claimant())
            print(msg)
            return 0 if it else 1
        items = st.items("revert", None if getattr(a, "all", False) else REVERT_OPEN)
    except StoreUnavailable as e:
        print(f"agentctl revert: the store did not answer ({str(e)[:200]})")
        return 2
    for i in sorted(items, key=lambda i: i["id"]):
        print(revert_line(i, t))
    if not items:
        print("reverts: none recorded" if getattr(a, "all", False) else "reverts: none open")
    return 0
