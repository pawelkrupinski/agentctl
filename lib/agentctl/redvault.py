# agentctl, part `redvault`: vault asks, spent quotas as external waits, blocks nobody can see clear.
# Runs inside agentctl's one namespace, after the parts before it in lib/agentctl/__init__.py's PARTS (it uses their names).

# A VAULT ASK IS ONE OWNER ITEM PER VAULT, ANSWERED BY ONE LINE (2026-11-16, Order independence #77 [ios]: its fixer's
# tested fix waited on commit signing while the `movies` vault was locked; it ended BLOCKED ON OWNER five turns in four
# minutes, then sat as an owner decision until the owner hand-ran `agentctl decide` at 05:01Z). A fixer's ask whose one
# condition is a vault (`secrets unlock <ns>` in a BLOCKED ON OWNER verdict or a DIAGNOSED decide) puts the red, whatever
# its kind, on ONE lease per vault, block/cause/vault-<ns>: the owner is told once for every red it holds (ask.py), with
# the one line that resumes them all, `! ~/bin/agentctl decide vault-<ns> here` (red_vault_decide).
# NO UNLOCK RESUMES THEM BY ITSELF (review round 3, 2026-11-16). An unlock is pinned to ONE session anchor, the caller's
# nearest Claude Code ancestor, and signing asks Touch ID once per anchor (~/projects/secrets README, "Session pinning"):
# the owner's `secrets unlock` in his own session never lets a fixer sign. The secrets-log watch that resumed the reds on
# any unlock (2a8139c) read only "the owner touched the vault somewhere", never fired once, and would have had a fixer
# raise a Touch ID prompt minutes later that the owner had not asked for -- the prompt secrets' README says never to
# approve. `decide vault-<ns>` says what matters: the owner is at the machine now. Each resumed fixer signs a probe
# commit FIRST (its own Touch ID prompt, raised while he is there) and ends BLOCKED ON OWNER at once if that fails: back
# on the vault's one lease, told once again.
# `decide vault-<ns>` too (2026-11-22, Order independence #105: turns 69-71 ended `BLOCKED ON OWNER: ~/bin/agentctl
# decide vault-movies "here"`, the cmd the owner is told, not `secrets unlock`; unmatched, each was re-offered ~2 min later)
VAULT_ASK = re.compile(r"secrets\s+unlock\s+([A-Za-z0-9_-]+)|decide\s+vault-([A-Za-z0-9_-]+)")
VAULT_TARGET = re.compile(r"vault-([A-Za-z0-9_-]+)")


def vault_ask_ns(text):
    """The vault an owner ask waits on (`secrets unlock <ns>` in it), or None."""
    m = VAULT_ASK.search(str(text or ""))
    return (m.group(1) or m.group(2)) if m else None


# A VAULT ASK NAMES THE SECRET IT NEEDS (2026-11-22: Tool self-tests #480, Order independence #116 and Alert backtest #20
# asked `! secrets unlock movies` while nothing in their fixes read a movies secret -- a land's push had failed (DNS),
# its NOT LANDED said `agentctl env --check`, and env --check said the vault was locked; released three times, back on
# the vault within minutes each time). In a repo whose main is unsigned (red-repos.json `signed: false`) nothing a fixer
# does but reading a secret touches the vault: its commits are unsigned (fixer_env), its pushes authenticate as gh's
# keyring login on the laptop and the GitHub App on ci-runner, agentctl's store token is in its environment. So there a
# vault ask counts only when it names the read that failed -- `secrets run|get <ns> ...` (SECRET_USE) -- and any other
# is routed back to the fixer (red_ask_route), saying why: decided from the fixer's own first secret use, never by
# default. A repo that signs keeps every vault ask (its commits need the vault's signing key).
SECRET_USE = re.compile(r"secrets\s+(?:run|get)\s+([A-Za-z0-9_-]+)\b")


def vault_ask_unfounded(o_r: str, text: object, ns: str) -> str | None:
    """Why the vault ask `text` (for vault ns) is no owner's -- None when it stands: a repo that signs, or the ask names
    a read of one of ns's secrets."""
    if repo_signs(o_r) or ns in SECRET_USE.findall(str(text or "")):
        return None
    return (f"no {ns} secret was read: in {o_r} a land, a push, a commit and agentctl's own reads need no vault -- "
            "commits are unsigned, GitHub authenticates through gh's login (the App on ci-runner), the store token is "
            "in your environment; a NOT LANDED push or `agentctl env --check` saying the vault is locked is no reason. "
            f"Ask only when `secrets run {ns} <KEY> -- <cmd>` for a secret your fix needs failed: name that command and "
            "its error in the ask")


def red_vault_resume(store, it, ns, answer, t):
    """The owner is at the machine (`decide vault-<ns>`): red `it`, waiting on vault ns, offered again as a decide
    would, its fixer briefed to prove its OWN session can sign before anything else."""
    d = it["data"]
    q = (d.get("ask") or {}).get("q") or f"agents' work waits on the {ns} vault"
    # a repo whose main is unsigned (fixer_env commits unsigned there): no signing probe, which would itself sign and
    # raise a prompt for nothing; the vault was for a secret, so the probe is the command that needed it
    probe = ("run `git commit-tree -S HEAD^{tree} -m 'signing probe' < /dev/null` in your worktree"
             if repo_signs(d.get("repo") or RED_PRIMARY) else
             "run again the one command that needed the vault -- a `secrets run` of the secret your fix uses, never "
             "`agentctl env --check` or `secrets status` (they only read the vault's state, raise no Touch ID prompt, "
             "and a fixer's land needs no vault: its credentials are in its environment); your commits here are "
             "unsigned: never sign one. A fix needing no secret just lands")
    dec = {"answer": f"the owner is at the machine (`decide vault-{ns}`: {answer}). No other session's unlock lets "
                     "yours use the vault: your first use raises a Touch ID prompt for YOUR session, which the owner "
                     f"answers now. So FIRST {probe}; if it fails, end `BLOCKED ON OWNER: ! secrets unlock {ns}` at "
                     "once, and in it name the command that failed and its error (no secret's value). Then go on "
                     "with the fix and land it.", "question": str(q)[:600], "at": t,
           "status": "in-progress", "category": "vault"}
    store.close_offer(f"red:{it['id']}", "resumed")
    red_offer(store, it, f"{red_what(d)}: the owner resumed it (the {ns} vault): offered again",
              data={"cause": None, "block": None, "ask": None, "decision": dec, "dispatch_after": 0})
    return [f"resumed {red_what(d)} (the {ns} vault)"]


def red_vault_decide(store, ns, answer):
    """`agentctl decide vault-<ns> "<answer>"`: every red on vault ns's lease resumed (red_vault_resume), the lease
    released. (rc, lines)."""
    lname, t = block_lease(cause=f"vault-{ns}"), store.clock()
    row = store.get(lname)
    if not lease_is_live(row, t) or lease_meta(row).get("vault") != ns:
        return 1, [f"NOT DECIDED: no red waits on the {ns} vault (`~/bin/agentctl red status`)"]
    keys = lease_meta(row).get("keys") or []
    # RESUMED FIRST, RELEASED AFTER (2026-11-19 03:01Z, core #1053: the owner's `decide vault-bitcashier here` revoked
    # the lease, the tick's invariant saw the red blocked-external with no block in that same second and re-offered it
    # bare -- and the decide, finding no red still blocked, said "NOT DECIDED" while its red went on without the
    # signing brief). While the lease is live no invariant touches its reds; one the tick re-offered meanwhile (offered,
    # no decision of its own) is still the owner's answer's: resumed too.
    out = []
    for key in keys:
        for _try in range(10):  # StaleVersion: the store ticks meanwhile -- re-read and re-apply
            it = red_of_key(store, key)
            if not it or it["state"] not in ("blocked-external", "offered") or \
                    (it["state"] == "offered" and red_decision(it["data"])):
                break
            try:
                out += red_vault_resume(store, it, ns, answer, t)
                break
            except StaleVersion:
                continue
    store.revoke(lname, f"RELEASED: DECIDED by the owner: {answer[:200]}")
    if not out:
        return 1, [f"NOT DECIDED: the {ns} vault's lease held no red still waiting on it (released)"]
    return 0, [f"DECIDED: {line} -- IN PROGRESS: each fixer's first signature asks Touch ID for its own session: answer "
               "it; if the action fails you are told" for line in out]


def red_vault_block(store, it, upd, ns, why, t):
    """The red waits on vault ns: it joins the vault's ONE lease (opened when none is live). Moved quietly: the tick's
    red_owner_reminders tells the owner once, with `decide vault-<ns>`."""
    d = {**it["data"], **upd}
    key = d["key"]
    lname = block_lease(cause=f"vault-{ns}")
    row = store.get(lname)
    if lease_is_live(row, t) and lease_meta(row).get("vault") == ns:
        store.set_meta(lname, row["epoch"], row["secret"], {"keys": sorted(set(lease_meta(row).get("keys") or []) | {key})})
    else:
        got = store.acquire(lname, "owner:vault", BLOCK_TTL, BLOCK_MAX, progress=f"locked@{int(t)}",
                            meta={"kind": "block", "vault": ns, "category": "vault", "cause": f"vault-{ns}",
                                  "cmd": vault_decide_cmd(ns),
                                  "question": f"agents' work waits on the {ns} vault (commit signing, its secrets) and "
                                              "only you can answer its Touch ID prompts", "keys": [key], "probed_at": t,
                                  "text": f"BLOCKED: the {ns} vault is locked for its fixers (`decide vault-{ns} here`)",
                                  "details": " ".join(str(why).split())[:400]})
        if "held" in got:  # somebody else's lease by that name: never this red's to take over
            red_offer(store, it, f"{red_what(d)}: {lname} is held by {got['held'].get('holder')}: offered again", data=upd)
            return [f"re-offered {red_what(d)} ({lname} held)"]
    store.close_offer(f"red:{it['id']}", "blocked")
    red_move(store, it, "blocked-external", f"{red_what(d)}: waits on the {ns} vault (the owner's `decide vault-{ns}`)",
             data={**upd, "cause": f"vault-{ns}", "block": lname})
    return [f"{red_what(d)}: waits on the {ns} vault"]


# A SPENT QUOTA IS AN EXTERNAL WAIT, NOT THE OWNER'S (the owner's coordinator, 2026-10-29: Record scrape fixtures #2426
# sat BLOCKED ON OWNER for OMDb's `HTTP 401: Request limit reached` -- a daily quota, which resets on its own; only an
# invalid key is the owner's). A red whose error line says a quota is spent is held by a WAIT lease (block/cause/quota-
# <service>, holder `wait:quota`), never offered to a fixer and never an owner block. Its probe re-checks the key the
# cheap way the workflow itself does: the red's workflow dispatched on main once the quota's window has reset (OMDb's
# daily window: midnight UTC), whose first job (the recording preflight: one OMDb call) fails within minutes if the key
# is still refused; the wait clears when a run of the workflow created after the block got past that job.
WAIT_CAUSES = {"quota": "an outside service's quota for the run's key, spent; it resets on its own"}
QUOTA_RESET_HOUR_UTC = 0


def quota_service(line):
    m = re.search(r"::error[^:]*::(\w+) refuses the key", line or "") or re.search(r"\b(OMDb|TMDB|Decodo)\b", line or "", re.I)
    return m.group(1) if m else "the service"


def next_quota_reset(after):
    """The first quota window reset (QUOTA_RESET_HOUR_UTC, daily) after unix time `after`."""
    d = dt.datetime.fromtimestamp(after, dt.timezone.utc)
    r = d.replace(hour=QUOTA_RESET_HOUR_UTC, minute=0, second=0, microsecond=0)
    if r <= d:
        r += dt.timedelta(days=1)
    return r.timestamp()


def probe_quota_reset(a):
    """The key accepted again: a run of a['workflow'] on main created after a['since'] got past its first job
    (a['job'], the preflight) -- or the whole run is green."""
    o_r = a.get("repo") or SHORT["movies"]
    # listing: runs created since the block; none listed keeps the wait (a probe that cannot tell never clears it)
    runs = gh_json(o_r, "run", "list", "--workflow", a["workflow"], "--branch", "main", "--json", RUN_FIELDS, "-L", "10")
    since = float(a.get("since") or 0)
    for r in runs or []:
        try:
            if parse_time(r["createdAt"]).timestamp() <= since:
                continue
        except (KeyError, ValueError, TypeError):
            continue
        if r.get("conclusion") == "success":
            return True, f"{a['workflow']} #{r.get('number')} green"
        jobs = run_jobs(o_r, r) or []
        first = next((j for j in jobs if j.get("name") == a.get("job")), None)
        if first and first.get("conclusion") == "success":
            return True, f"{a['workflow']} #{r.get('number')}: {a.get('job')} passed (the key is accepted again)"
        if first and first.get("conclusion") in ("failure", "timed_out"):
            return False, f"{a['workflow']} #{r.get('number')}: {a.get('job')} failed again (still spent)"
    return False, (f"no run of {a['workflow']} since the quota was spent; the next window opens "
                   f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(next_quota_reset(since)))}")


def red_quota_waits(store, o_r, repo, t):
    """Each open red whose line says a quota is spent, held by nobody (no fixer lease, no block): put on the WAIT lease
    of its service; each wait past its window's reset with no run since: its workflow dispatched once per window."""
    out = []
    for it in red_active_of(store, o_r).values():
        d = it["data"]
        if not QUOTA_LOG.search(str(d.get("line") or "")) or d.get("kind") == "test":
            continue
        if lease_is_live(store.get(red_lease(d["key"])), t) or red_block_of(it, store):
            continue
        svc = quota_service(d.get("line"))
        lname = block_lease(cause=f"quota-{svc.lower()}")
        row = store.get(lname)
        if lease_is_live(row, t):
            m = lease_meta(row)
            store.set_meta(lname, row["epoch"], row["secret"], {"keys": sorted(set(m.get("keys") or []) | {d["key"]})})
        else:
            probe = {"kind": "quota_reset", "args": {"repo": o_r, "workflow": d["workflow"], "since": t, "job": d.get("job")
                                                     if d.get("job") else "preflight"}}
            until = f"{svc}'s quota resets and {d['workflow']}'s {probe['args']['job']} passes"
            store.acquire(lname, "wait:quota", BLOCK_TTL, BLOCK_MAX, progress=f"spent@{int(t)}",
                          meta={"kind": "block", "wait_cause": "quota", "cause": f"quota-{svc.lower()}", "probe": probe,
                                "until": until, "keys": [d["key"]], "probed_at": t, "redispatch": d["workflow"],
                                "text": f"WAITING: {svc}'s quota for the run's key is spent; it resets on its own "
                                        f"(auto-clears when {until})"})
            line = f"RED WAITING: {red_what(d)}: {svc}'s quota is spent -- an external wait, not the owner's; " \
                   f"{d['workflow']} is dispatched again after the reset"
            inbox_write("red", line, [line], [], 0)
        cur = store.item("red", it["id"])
        store.update_item("red", it["id"], cur["version"], {"block": lname})
        out.append(f"{red_what(d)}: waits on {svc}'s quota ({lname})")
    for row in store.list("block/cause/quota-"):
        m = lease_meta(row)
        if not lease_is_live(row, t) or not m.get("redispatch"):
            continue
        since = float((m.get("probe") or {}).get("args", {}).get("since") or t)
        last = float(m.get("dispatched_at") or since)
        if t >= next_quota_reset(last) + 300:
            msg = red_verify_dispatch(o_r, repo, m["redispatch"], "quota-reset")
            out.append(f"{m['redispatch']} after {m.get('cause')}'s reset (re-checks the key): {msg}")
            store.set_meta(row["name"], row["epoch"], row["secret"], {"dispatched_at": t})
    return out


def red_owner_push(store, o_r, target, note="", wt=None):
    """(rc, lines) of `red owner-push`: an owner-only repo's fix (red-repos.json; infra, whose push is a deploy), prepared
    on the worktree's local branch, filed with the owner. The red goes to fix-pending with a fix of status `owner` (its
    branch, head and the owner's one-liner) and one escalation reaches the inbox with the command; a green gate run
    verifies it, and unproven after RED_FIX_GIVEUP it is offered again (red_fix_pending). Never pushes."""
    if not red_owner_only(o_r):
        return 2, [f"NOT FILED: {o_r} is not owner-only (red-repos.json): land the fix with `~/bin/agentctl land`"]
    here = safe_cwd()
    wt = os.path.realpath(wt or worktree_of(here) or here)
    if not os.path.isdir(wt) or root_checkout(wt, o_r):
        return 2, [f"NOT FILED: {wt.replace(HOME, '~')} is not a worktree of your own: `--worktree <yours>`"]
    try:
        if slug(wt) != o_r:
            return 2, [f"NOT FILED: {wt.replace(HOME, '~')} is a clone of {slug(wt)}, not {o_r}"]
    except (Exception, SystemExit) as e:
        return 2, [f"NOT FILED: {wt.replace(HOME, '~')} is no clone of {o_r} ({str(e)[:120]})"]
    if git(wt, "status", "--porcelain", "--untracked-files=no", check=False).stdout.strip():
        return 1, ["NOT FILED: the worktree has uncommitted changes: commit each step first"]
    git(wt, "fetch", "--quiet", remote(), "main", check=False)
    ahead = unlanded_commits(wt)
    if not ahead:
        return 1, [f"NOT FILED: nothing in {wt.replace(HOME, '~')} that is not on {remote()}/main"]
    items = red_targets(store, o_r, target, create=False)
    if not items:
        return 1, [f"NOT FILED: #{target} names no open red of {o_r}"]
    head = git(wt, "rev-parse", "HEAD").strip()
    branch = git(wt, "rev-parse", "--abbrev-ref", "HEAD", check=False).stdout.strip() or head[:10]
    tw = wt.replace(HOME, "~")
    cmd = f"git -C {tw} pull --rebase {remote()} main && git -C {tw} push {remote()} HEAD:main"
    t = store.clock()
    what = ", ".join(red_what(it["data"]) for it in items)
    line = (f"RED OWNER PUSH: {what}: a fix is ready on {branch} ({len(ahead)} commit(s), "
            f"{', '.join(s[:10] for s, _ in ahead[:6])}) in {wt.replace(HOME, '~')}"
            + (f" -- {note}" if note else "") + f". {o_r} is owner-only (a push is a deploy): `! {cmd}`")
    for it in items:
        cur = store.item("red", it["id"])
        red_move(store, cur, "fix-pending", line, data={"fix": {"status": "owner", "entry": f"owner-push:{branch}",
                                                                "sha": head, "at": t, "worktree": wt, "cmd": cmd,
                                                                "note": note, "verify_since": t}})
    inbox_write("red", line, [line], [], 1, cwd=wt)
    return 0, [line, f"BLOCKED ON OWNER: {cmd}"]


# A BLOCK NOBODY CAN SEE CLEAR (2026-11-07: Verify scrape recording #14's reds read "BLOCKED ON OWNER: ... re-pin movies
# runner scheduler to 362196b15a" hours after the owner had re-pinned, and past it -- ci-runner ran a later scheduler).
# That block was a turn's VERDICT, not a block lease: no probe could clear it, so it stayed the red's last word. A
# verdict with no live block now reads with its age and how to strike it (block_verdict_note), and `red unblock` strikes
# it -- or ends a live block lease (RELEASED: its reds go on at the next tick, re-offered, never escalated) -- leaving
# the reason in the red's history.
def red_blocked_verdict(d):
    """The red's last word when it is a BLOCKED ON OWNER verdict (its history's newest entry), else None."""
    last = (d.get("history") or [{}])[-1]
    return last if str(last.get("outcome") or "").startswith("BLOCKED ON OWNER") else None


def block_verdict_note(it, t, store=None):
    """A BLOCKED ON OWNER verdict that no live block lease holds, shown as what it is: an old word, its age, and how to
    strike it; None when the red's last word is no such verdict or a block holds it."""
    d = it["data"]
    last = red_blocked_verdict(d)
    if not last or red_block_of(it, store):
        return None
    at = iso_epoch(last.get("at"))
    age = t - at if at is not None else None
    said = f"said {fmt_age(age)} ago" if age is not None else f"said at {last.get('at')}"
    return (f"{str(last.get('outcome'))[:300]} -- {said}, no block holds it (no probe clears it): if it is done or "
            f"superseded, `agentctl red unblock {d.get('key')} --why \"<what changed>\"`")


def red_unblock(store, o_r, target, why, who=None):
    """(rc, lines) of `red unblock`: the reds a target names are no longer the owner's -- a live block lease holding
    them is ended (RELEASED: the tick offers them again, never escalated) and an UNBLOCKED entry ends their history, so a
    stale BLOCKED ON OWNER verdict is no longer their last word."""
    if not str(why or "").strip():
        return 2, ["NOT UNBLOCKED: --why \"<what changed>\" (the owner did it, it is superseded by ...)"]
    items = red_targets(store, o_r, target, create=False)
    if not items:
        return 1, [f"NOT UNBLOCKED: #{target} names no open red"]
    who = who or claimant(safe_cwd())
    t = store.clock()
    lines = []
    for it in items:
        d = it["data"]
        blk = red_block_of(it, store)
        if blk:
            row = store.get(blk["name"])
            keep = [k for k in lease_meta(row).get("keys") or [] if k != d.get("key")]
            if keep:  # an outage's ONE lease holding other reds too: only this red leaves it
                store.set_meta(row["name"], row["epoch"], row["secret"], {"keys": keep})
            else:
                store.revoke(row["name"], f"RELEASED: unblocked by {who}: {why}")
        elif not red_blocked_verdict(d):
            lines.append(f"{red_what(d)} ({d.get('key')}): not blocked on the owner -- left as it is")
            continue
        cur = store.item("red", it["id"])
        h = {"at": iso_at(t), "holder": who, "outcome": f"UNBLOCKED: {why}"}
        store.update_item("red", it["id"], cur["version"], {"history": (cur["data"].get("history") or []) + [h]})
        lines.append(f"unblocked {red_what(d)} ({d.get('key')})" + (f": ended {blk['name']}" if blk else
                                                                    ": its BLOCKED ON OWNER verdict struck") + f" -- {why}")
    return 0, lines


def red_block(o_r, a):
    """`red block`: the red waits on the OWNER, for an owner cause only, as a block lease the tick renews while its
    probe -- named (--until-green / --until-http / --until-tcp), an open cause's, or the red's own workflow going green
    -- still fails and its justification holds. --cause NAME: ONE lease for that outage, every red it explains attached."""
    st = open_store()
    n = str(a.run).lstrip("#")
    name, units = None, []
    known = red_targets(st, o_r, n, create=False)  # a red key, or a run the tick has seen: as `red claim` finds it
    try:
        name, _r, units = red_run_units(o_r, n)
    except (Exception, SystemExit):
        if not known:
            name, _r = resolve_run(o_r, n)
    if known and name and name != known[0]["data"].get("workflow"):  # a bare NUMBER another workflow's run also has
        name, units = None, []  # (issue 20261107T072345-7bca40: `red block 14` read Report run timings #14): the red's
    if known and not units:  # issue 20261119T030011-293b9a: a key, or a run whose units no longer read (a green rerun)
        name = name or known[0]["data"].get("workflow")
        units = [{"kind": it["data"].get("kind"), "line": it["data"].get("line"), "job": it["data"].get("job")}
                 for it in known]
    cause = getattr(a, "cause", None)
    lname = block_lease(cause=cause) if cause else None
    old = st.get(lname) if lname else None
    old = old if lease_is_live(old, st.clock()) else None
    om = lease_meta(old)
    owner_cause = getattr(a, "owner_cause", None) or om.get("owner_cause")
    vault = vault_ask_ns(a.owner_cmd)  # a locked vault: no owner cause, any red kind (red_vault_block, below)
    if owner_cause not in OWNER_CAUSES and not vault:
        print(f"NOT BLOCKED: a block on the owner needs --owner-cause, one of: {', '.join(OWNER_CAUSES)} (a cause outside "
              "the code that only the owner can act on). A code red is yours: claim it and fix it.")
        return 2
    if any(QUOTA_LOG.search(str(x.get("line") or "")) for x in units):
        print(f"NOT BLOCKED: #{n}'s key hit a spent quota ({str(units[0].get('line'))[:120]}): it resets on its own, so "
              "it is no owner action -- the tick holds the red as an external wait and re-runs its workflow after the "
              "reset (`agentctl red status`)")
        return 2
    if any(x.get("kind") == "test" for x in units) and not vault:
        print(f"NOT BLOCKED: #{n} is a TEST FAILURE ({units[0].get('job')}): a code red is never the owner's -- "
              f"`~/bin/agentctl red claim {n}` and fix it")
        return 2
    unsafe = owner_cmd_unsafe(a.owner_cmd or om.get("cmd"))
    if unsafe:
        print(f"NOT BLOCKED: the owner command is unsafe -- {unsafe}. A command that deletes names each path and "
              "checks, inside the command, that it is still unused when the owner runs it.")
        return 2
    items = known or (red_targets(st, o_r, n) if units else [])
    own = read_record(own_fixer_record_path() or "") or {}
    handed = [] if not items else [red_lease(it["data"]["key"]) for it in items]
    fam = own.get("family")  # A FAMILY OWNER blocks a red it holds (issues 20261106T210149-680984,
    # 20261119T015532-983695: "a live fixer is on #1005: laptop:owner:convergence" -- the owner itself)
    handed = [x for x in handed if lease_is_live(st.get(x), st.clock()) and (
        x == own.get("lease") and st.get(x)["epoch"] == own.get("epoch")
        or fam and st.get(x)["holder"] == owner_holder(fam) and lease_meta(st.get(x)).get("family") == fam)]
    live = [st.get(red_lease(it["data"]["key"]))["holder"] for it in items
            if red_lease(it["data"]["key"]) not in handed and lease_is_live(st.get(red_lease(it["data"]["key"])), st.clock())]
    if live:
        print(f"NOT BLOCKED: a live fixer is on #{n}: {live[0]}")
        return 1
    ns = vault
    if ns and items:  # A LOCKED VAULT IS ONE WAIT, never an owner cause (issue 20261119T015532-983695): the red joins the
        # vault's lease (red_vault_block), which the owner's `decide vault-<ns>` lifts for every red on it
        t = st.clock()
        for it in items:
            for line in red_vault_block(st, st.item("red", it["id"]), {}, ns, f"`red block`: {a.owner_cmd}", t):
                print(line)
        for x in handed:
            st.revoke(x, f"BLOCKED ON OWNER (handed over by its fixer): the {ns} vault is locked")
        return 0
    if old and not (a.until_green or a.until_http or a.until_tcp):  # attach to the outage's ONE lease: its probe
        iprobe = run_probe(om["probe"]["kind"], om["probe"]["args"])
    elif cause and not (a.until_green or a.until_http or a.until_tcp):
        print(f"NOT BLOCKED: no open cause {cause!r} to attach to; open it with its probe: --cause {cause} "
              "--until-green <workflow> | --until-http <url> | --until-tcp <host:port>")
        return 2
    elif a.until_green:
        iprobe = run_probe("workflow_green", {"repo": o_r, "workflow": a.until_green})
    elif a.until_http:
        iprobe = run_probe("http", {"url": a.until_http})
    elif a.until_tcp:
        host, _, port = a.until_tcp.rpartition(":")
        iprobe = run_probe("tcp", {"host": host, "port": int(port)})
    elif name:
        iprobe = run_probe("workflow_green", {"repo": o_r, "workflow": name})
    else:
        print(f"NOT BLOCKED: #{n}'s workflow is unknown, so no probe can clear the block; name one: --until-green "
              "<workflow> | --until-http <url> | --until-tcp <host:port>")
        return 2
    words = probe_words(iprobe.kind, iprobe.args)
    if iprobe.ok:
        print(f"NOT BLOCKED: {words} already ({iprobe.detail}): nothing waits on the owner -- `red claim {n}` and fix it")
        return 1
    if not items:
        print(f"NOT BLOCKED: #{n} names no red on main")
        return 1
    details = owner_details(o_r, n, name, units, a.owner_cmd or om.get("cmd"), words, owner_cause)
    keys = [it["data"]["key"] for it in items]
    what = ", ".join(red_what(it["data"]) for it in items)
    text = (f"BLOCKED: {cause} down (auto-clears when {words})" if cause else
            f"BLOCKED ON OWNER: {details}")
    verify = not (iprobe.kind == "workflow_green" and iprobe.args.get("workflow") == name)
    if old:
        st.set_meta(lname, old["epoch"], old["secret"], {"keys": sorted(set(om.get("keys") or []) | set(keys))})
        print(f"attached {what} to {cause!r}: {om.get('text')} ({iprobe.detail}; `agentctl red status`)")
    else:
        lname = lname or block_lease(keys[0])
        r = st.acquire(lname, f"owner:{owner_cause}", BLOCK_TTL, BLOCK_MAX, progress=f"{iprobe.detail}@{int(time.time())}",
                       meta={"kind": "block", "owner_cause": owner_cause, "category": OWNER_CAUSE_CATEGORY[owner_cause],
                             "cmd": a.owner_cmd, "cause": cause,
                             "question": f"run `! {a.owner_cmd}` ({OWNER_CAUSES[owner_cause]})" +
                                         (f" -- {a.note}" if getattr(a, "note", "") else ""),
                             "probe": {"kind": iprobe.kind, "args": iprobe.args}, "until": words, "keys": keys,
                             "text": text, "details": details, "verify": verify, "probed_at": time.time()})
        same = st.get(lname) if "held" in r else None
        if "held" in r and lease_meta(same).get("kind") != "block":
            print(f"NOT BLOCKED: {lname} is held by {r['held']['holder']}")
            return 1
        if same:  # the run's block is already up (named after its first red): these reds join it
            sm = lease_meta(same)
            st.set_meta(lname, same["epoch"], same["secret"], {"keys": sorted(set(sm.get("keys") or []) | set(keys))})
            print(f"already blocked: {what} joined {lname}: {sm.get('text')}")
        else:
            cat = OWNER_CAUSE_CATEGORY[owner_cause]
            line = (f"RED BLOCKED ON OWNER [{cat}]: {cause} down (blocks {what}): {details}" if cause else
                    f"RED BLOCKED ON OWNER [{cat}]: {what}: {details}")
            # the owner is told by the tick (red_owner_reminders: ONE inbox entry and a notification, then reminders)
            print(line + f" ({iprobe.detail}; `agentctl red status`)")
    for it in items:
        cur = st.item("red", it["id"])
        st.update_item("red", it["id"], cur["version"], {"block": lname})
    for x in handed:  # the dispatched fixer's own lease, handed to the owner: its wrapper ends with the block's verdict
        st.revoke(x, f"BLOCKED ON OWNER (handed over by its fixer): {a.owner_cmd or om.get('cmd')}")
    return 0

