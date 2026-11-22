# agentctl, part `redclaim`: interactive holders: claim, beat, release a red.
# Runs inside agentctl's one namespace, after the parts before it in lib/agentctl/__init__.py's PARTS (it uses their names).

# -- interactive: claim, beat, release
def red_targets(store, o_r, target, create=True):
    """The non-terminal red items a target names: a red key, or a run (number, id, or `local`); a run not yet seen by
    the tick is opened here (its observation), so a claim always has its item."""
    t = str(target).lstrip("#")
    active = red_active(store)
    if t in active:
        return [active[t]]
    # A RUN NUMBER is per repo: o_r's reds when it is another watched repo, else the primary's (as before 2026-10-30)
    mine = o_r if red_is_other(o_r) else RED_PRIMARY
    hits = [h for h in red_item_of_run(store, t) if red_repo_of(h["data"]) == mine]
    if hits or not create:
        return hits
    name, r, units = red_run_units(o_r, t)
    out = []
    for key, ob in red_obs_for(o_r, red_obs_of_units(units)).items():
        it = active.get(key) or red_open(store, ob, None, store.clock(), o_r)
        out.append(red_attach_obs(store, it, ob))
    return out


def root_checkout(wt, o_r):
    """True for a repo's own clone (the root checkout): a claim names a worktree of its own (audit #3)."""
    roots = set()
    for f in (lambda: scan_repo_for(o_r), lambda: repo_path_for(o_r), movies_repo_path):
        try:
            roots.add(os.path.realpath(f()))
        except (Exception, SystemExit):
            pass
    return os.path.realpath(wt) in roots


def bind_fixer_session(sid, record):
    """The session -> fixer record binding (reds/sessions/<session id>.json): written by the wrapper once `claude
    agents` names its session, and by the fixer's own first command (`red claim N --fixer <record>`, in its prompt).
    Refused (None) for a record that ended or names another session."""
    rec = read_record(record) if sid and record else None
    if not rec or rec.get("ended") or not record_session_is(rec, sid, unset=True):
        return None
    write_json(fixer_session_path(sid), {"record": record, "at": ts()})
    return record


def red_claim(store, o_r, target, note="", wt=None):
    """(rc, lines): the red's lease to the caller (a claim, renewed by its worktree's activity). Inside a dispatched
    fixer, a red its wrapper already holds (its own, or a family / run sibling under the same holder) gets the fixer's
    note; ANY OTHER red N is claimed for the fixer, in its worktree -- how a fixer (the convergence family's) takes a
    second red. 2026-10-29: `red claim <N>` in a fixer wrote the note on its own lease and ignored N."""
    own = own_fixer_record_path()
    rec = (read_record(own) or {}) if own else {}
    here = safe_cwd()
    items = None
    if own:
        t = str(target).lstrip("#")
        if t in (str(rec.get("run")), str(rec.get("lease") or "").split("/", 1)[-1]):  # its own red, by run or key
            rec["note"] = note or rec.get("note") or "working"
            write_json(own, rec)
            return 0, [f"noted on your lease {rec.get('lease')}: {rec['note']}"]
        items = red_targets(store, o_r, target)
        primary = store.get(rec["lease"]) if rec.get("lease") else None
        mine = primary["holder"] if lease_is_live(primary, store.clock()) else None

        def held_by_me(it):
            row = store.get(red_lease(it["data"]["key"]))
            return lease_is_live(row, store.clock()) and (row["holder"] == mine or row["name"] == rec.get("lease"))
        if not items or all(held_by_me(it) for it in items):
            rec["note"] = note or rec.get("note") or "working"
            write_json(own, rec)
            for it in items or []:  # a red joined to its lease, worked on: no longer one the backstop releases
                row = store.get(red_lease(it["data"]["key"]))
                if lease_meta(row).get("joined_at"):
                    red_joined_progress(store, row, f"note: {rec['note']}")
            return 0, [f"noted on your lease {rec.get('lease')}: {rec['note']}"]
        items = [it for it in items if not held_by_me(it)]
        wt = wt or rec.get("worktree")
    wt = os.path.realpath(wt or worktree_of(here) or here)
    if root_checkout(wt, o_r) or not os.path.isdir(wt):
        return 2, [f"NOT CLAIMED: {wt.replace(HOME, '~')} is not a worktree of your own (a claim is renewed by its "
                   "worktree's activity; the root checkout counts for nobody): `--worktree <your worktree>`"]
    items = items if items is not None else red_targets(store, o_r, target)
    if not items:
        return 1, [f"NOT CLAIMED: #{target} names no red on main"]
    who = claimant(here)
    holder = f"laptop:claim:{who}@{wt}"
    rc, lines = 0, []
    for it in items:
        name = red_lease(it["data"]["key"])
        row = store.get(name)
        if lease_is_live(row, store.clock()) and row["holder"] == holder:
            store.renew(name, row["epoch"], row["secret"], f"beat {time.time()}", RED_CLAIM_TTL, note=note or None)
            lines.append(f"renewed {red_what(it['data'])} ({name})")
            continue
        r = store.acquire(name, holder, RED_CLAIM_TTL, RED_CLAIM_MAX, progress=red_wt_mark(wt), item=f"red:{it['id']}",
                          meta={"kind": "claim", "worktree": wt, "who": who, "note": note,
                                "session": os.environ.get("CLAUDE_CODE_SESSION_ID"), "gate": bool(it["data"].get("gate")),
                                # made inside a dispatched fixer: its family's holder (red_family_holder); an
                                # interactive session's claim takes only its red (the owner, 2026-11-08)
                                **({"fixer": own} if own else {})})
        if "held" in r:
            rc = 1
            lines.append(f"NOT CLAIMED: {red_what(it['data'])} is held by {r['held']['holder']} until "
                         f"{time.strftime('%H:%M', time.localtime(r['held']['expires_at']))}")
            continue
        store.close_offer(f"red:{it['id']}", "taken")
        lines.append(f"claimed {red_what(it['data'])} for {who} ({wt.replace(HOME, '~')}) as {name}"
                     + (f": {note}" if note else "") + f"; it lapses after {RED_CLAIM_TTL // 60} min without activity in "
                     f"your worktree; release: ~/bin/agentctl red release {it['data']['key']}")
    return rc, lines


def red_beat(store, o_r, target, note):
    if own_fixer_record_path():
        return red_claim(store, o_r, target, note)
    out, rc = [], 1
    for it in red_targets(store, o_r, target, create=False):
        name = red_lease(it["data"]["key"])
        row = store.get(name)
        if not lease_is_live(row, store.clock()):
            out.append(f"{red_what(it['data'])}: no live lease (`red claim` it)")
            continue
        r = store.renew(name, row["epoch"], row["secret"], f"note:{note}", RED_CLAIM_TTL, note=note)
        rc = 0 if r == "ok" else rc
        out.append(f"{red_what(it['data'])}: {r}" + (" (the same note is no progress)" if r == "no_progress" else ""))
    return rc, out or [f"#{target} names no red"]


def red_release(store, o_r, target, any_=False):
    me = claimant()
    out, rc = [], 1
    for it in red_targets(store, o_r, target, create=False):
        name = red_lease(it["data"]["key"])
        row = store.get(name)
        blk = red_block_of(it, store) if any_ and not lease_is_live(row, store.clock()) else None
        if blk and (blk["meta"].get("crash_loop") or blk["meta"].get("decision")):
            # THE OWNER LIFTS A BLOCK OF THE HARNESS'S OR A DECISION: the red is offered again at the next tick
            keys = [k for k in blk["meta"].get("keys") or [] if k != it["data"]["key"]]
            brow = store.get(blk["name"])
            if keys:  # the block holds other reds too: only this one leaves it
                store.set_meta(blk["name"], brow["epoch"], brow["secret"], {"keys": keys})
            else:
                store.revoke(blk["name"], f"RELEASED by {me}: offered again")
            rc = 0
            out.append(f"lifted the owner block of {red_what(it['data'])} ({blk['cause']}): offered again at the next tick")
            continue
        if not lease_is_live(row, store.clock()):
            continue
        if not any_ and lease_meta(row).get("who") != me:
            out.append(f"{red_what(it['data'])} is held by {row['holder']}, not {me} (--any releases anyone's)")
            continue
        store.revoke(name, f"RELEASED by {me}")
        rc = 0
        out.append(f"released {red_what(it['data'])} ({row['holder']})")
    return rc, out or [f"red: no live lease on #{target}"]
