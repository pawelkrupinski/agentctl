# agentctl, part `orphans`: orphaned work: a worktree with commits nothing lands, found, offered to a fixer or escalated
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import glob
import json
import os
import re
import subprocess
import sys
import time
from agentctl.core import (
    HOME, branch_of, cherry_landed, env_num, git, git_ok, hashlib_sha1, reds_dir, remote, slug, spawn_detached)
from agentctl.timefmt import parse_time, ts
from agentctl.statefile import read_record, write_json
from agentctl.classify import NETWORK_CHECK_FAILURE
from agentctl.store import open_store
from agentctl.selfcode import TOOL_PATH
from agentctl.repos import movies_repo_path
from agentctl.trailers import do_not_land, held_commit
from agentctl.waitingdir import WAITING_BRANCH, waiting_entries, waiting_state
from agentctl.procs import process_cwds, process_start
from agentctl.fixerrecords import fixer_records
from agentctl.inboxentry import inbox_entries, inbox_write
from agentctl.worktrees import gitdir_activity, worktrees
from agentctl.fixerenv import fixer_env
from agentctl.dispatches import internal_tree
from agentctl.conditions import open_condition, recovery_dir, run_probe
from agentctl.sessionpaths import session_worktrees
from agentctl.dispatchblock import land_cwds
from agentctl.recovery import reland_tree_path, reland_worktree
from agentctl.superseded import superseded_duty, superseded_mark, worktree_change_items
from agentctl.park import park_duty, parked_mark
from agentctl.blamerecords import blame_drop_revert_worktree, blame_land_refusal, blame_records


# ORPHANED WORK (the owner, 2026-10-28: "review and fix landing code ... so things don't get missed or stalled"; the
# audit found movies-wt-workflow-prune rejected 6x and idle 15 h, identity-impact / unit-before-push / red-local idle
# 12-19 h, flipflop / nodrift-cadence / unify / mongo-csot never landed at all): every maintain pass, a worktree whose
# commits are not on origin/main (by patch) and whose author is gone -- no process in it, no land, no live claim,
# idle ORPHAN_IDLE, no live session that ever named it -- gets ONE action: a re-land (its last verdict infra, unknown or
# none; only with LAND INTENT, never work NOT FOR LANDING, and from a throwaway tree, never the author's: see below and
# reland_worktree), or a dispatched fixer (a real failure: the checks rejected it). Still orphaned after that: UNLANDED ORPHAN, a condition whose probe clears
# when its commits are on main, or somebody works in it again. Nothing is ever deleted.
ORPHAN_IDLE = 2 * 3600


ORPHAN_PER_PASS = 3


def orphan_key(repo, wt):
    """The change's identity across rebases: its commits' patch-ids (not HEAD, which every rebase moves)."""
    main = f"refs/remotes/{remote()}/main"
    plus = cherry_landed(wt, main, "HEAD")[1] or []  # landed by patch or by content: not the orphan's
    ids = []
    for sha in plus:  # bytes: a commit may carry binary content
        show = subprocess.run(["git", "-C", wt, "show", sha], capture_output=True, timeout=60).stdout
        r = subprocess.run(["git", "patch-id", "--stable"], input=show, capture_output=True, timeout=60)
        ids.append((r.stdout.decode(errors="replace").split() or [sha])[0])
    return plus, hashlib_sha1("\n".join(sorted(ids)))[:16] if ids else None


def land_entries_by_cwd(entries=None):
    """{realpath of a cwd: its land/waiting inbox entries, oldest first}."""
    out = {}
    for e in inbox_entries() if entries is None else entries:
        if e.get("command") in ("land", "waiting"):
            out.setdefault(os.path.realpath(str(e.get("cwd") or "")), []).append(e)
    return out


def land_entries_once():
    """land_entries_by_cwd, read on first use and kept for the caller's pass: the inbox is thousands of files (7849 on
    2026-11-07, ~2.4 s a read), and an orphan pass asks it of every orphan twice or more (review 2026-11-07)."""
    memo = []

    def get():
        if not memo:
            memo.append(land_entries_by_cwd())
        return memo[0]
    return get


def last_land_entry(wt, lands=None):
    """The newest final land entry in the inbox for this worktree, or None. lands: a land_entries_once reader."""
    es = [e for e in (lands or land_entries_once())().get(os.path.realpath(wt)) or [] if e.get("final", True)]
    return es[-1] if es else None


# AN INFRA FAILURE IS NO VERDICT ON AN ORPHAN (2026-11-02: movies-wt-flip-stage5's land `crashed: StoreUnavailable: POST
# /v1/store/items: ... Connection refused` during a tunnel outage, and that crash was its final word: UNLANDED ORPHAN
# "after a submit (crashed: StoreUnavailable ...)" for good). A verdict that says the store, the tunnel, a fetch or the
# network failed -- not the change -- never escalates: the orphan is submitted again once the store answers, the waits
# between tries backing off (ORPHAN_INFRA_BACKOFF), however many it takes.
ORPHAN_INFRA = re.compile(r"crashed: (StoreUnavailable|StoreAuthFailed|URLError|ConnectionError|ConnectionRefusedError|"
                          r"TimeoutError|RemoteDisconnected)\b|\bStoreUnavailable\b|store (did not answer|unreachable)|"
                          r"its fetch of main failed|could not fetch|\btunnel\b|^NETWORK:", re.I)


ORPHAN_INFRA_BACKOFF = (900, 1800, 3600, 7200, 21600)


# A REAL FAILURE GETS ONE FIXER (the owner, 2026-11-03: movies-wfnames and -order-rows, pre-push NOT LANDED, escalated
# again after every rebase): an orphan whose submit was rejected by its checks (or whose rebase conflicts) and that
# nobody owns gets ONE fixer in its own worktree (the red-fixer pattern: a background Claude session under a lease of its
# own, `orphan · <worktree>` in `claude agents`), once per worktree -- whatever its later rebases do to its commits.
# Only when that fixer ended without landing it does UNLANDED ORPHAN go to a person. A parked worktree gets none.
ORPHAN_REAL_FAILURE = re.compile(r"^NOT LANDED: (the pre-push checks failed|rebase onto \S+ conflicts)")


ORPHAN_FIXERS_MAX = 2  # orphan fixers alive at once ($AGENTCTL_ORPHAN_FIXERS); one dispatched a pass


def orphan_infra(verdict):
    v = str(verdict or "")
    return bool(ORPHAN_INFRA.search(v) or NETWORK_CHECK_FAILURE.search(v)) and not ORPHAN_REAL_FAILURE.search(v)


def store_answers(store=None):
    """True when the store answers now (an infra-failed orphan's retry waits for it)."""
    try:
        (store or open_store()).item("change", "store-answers-probe")
        return True
    except (Exception, SystemExit):
        return False


def orphan_fixers_path():
    return recovery_dir("orphan-fixers.json")


def orphan_fixers_alive():
    return [r for r in fixer_records() if r.get("orphan") and r.get("alive")]


def orphan_dispatch(repo, wt, name, verdict):
    """A background fixer IN the orphan's worktree (its author's), told the last verdict. (ok, message)."""
    num = f"orphan-{name}"
    d = reds_dir(num)
    os.makedirs(d, exist_ok=True)
    k = 1 + len(glob.glob(os.path.join(glob.escape(d), "fixer*.json")))
    rec_path = os.path.join(d, "fixer.json" if k == 1 else f"fixer-{k}.json")
    rec = {"sig": None, "sigs": [], "jobs": ["land"], "run": num, "run_id": None, "workflow": "Unlanded change",
           "job": "land", "kind": "test", "line": (verdict or "")[:400], "gate": False, "worktree": wt, "started": ts(),
           "why": "orphaned change, author gone, its checks rejected it", "log": os.path.join(d, f"claude-{k}.log"),
           "names": [num], "orphan": True}
    write_json(rec_path, rec)
    with open(os.path.join(d, "fixer.log"), "a") as logf:
        p = spawn_detached([sys.executable, TOOL_PATH, "red", "fixer", "--repo", repo, "--record", rec_path],
                           stdin=subprocess.DEVNULL, stdout=logf, stderr=logf, start_new_session=True, cwd=wt,
                           env=fixer_env(slug(repo), num))
    rec.update(pid=p.pid, process_start=process_start(p.pid))
    write_json(rec_path, rec)
    inbox_write("maintain", f"ORPHAN FIXER DISPATCHED: {wt.replace(HOME, '~')} -> fixer session 'orphan · {name}' "
                            f"(`claude agents`): {str(verdict or '')[:200]}", [], [], 0, cwd=wt)
    return True, f"fixer pid {p.pid} in {wt.replace(HOME, '~')}"


def live_lease_worktrees():
    """The worktrees a live lease names (a fixer's, an interactive claim's): somebody works there."""
    out, t = set(), time.time()
    for r in open_store().list():
        if r["holder"] and r["expires_at"] > t:
            wt = (json.loads(r["meta"] or "{}") or {}).get("worktree")
            if wt:
                out.add(os.path.realpath(wt))
    return out


def orphan_candidates(repo, now_=None, dead=None, refused=None, lands=None):
    """[(worktree, commits, key, last verdict)] of the orphans now. A worktree holding a withdrawn or refuted blame
    revert (blame_land_refusal) is never one: it goes to `dead` [(worktree, why)] when given; one whose work is not for
    landing (orphan_refusal) goes to `refused` [(worktree, key, why)]. lands: the pass's land_entries_once reader."""
    t = now_ if now_ is not None else time.time()
    lands = lands or land_entries_once()
    try:
        cwds = {os.path.realpath(c) for c in process_cwds().values()}
    except (Exception, SystemExit):
        cwds = set()
    try:
        landers = {os.path.realpath(w) for w in land_cwds()}
    except (Exception, SystemExit):
        landers = set()
    live = live_lease_worktrees()
    try:
        sessions = session_worktrees([w.get("worktree") for w in worktrees(repo)], t)
    except (Exception, SystemExit):
        sessions = {}
    records = blame_records()
    try:  # a change in the land queue (queued, checking, waiting) is not orphaned
        d, _c, _w = waiting_state(repo)
        live |= {os.path.realpath(e.get("worktree") or "") for e in waiting_entries(d, open_only=False)
                 if e.get("status") in ("queued", "checking", "waiting")}
    except (Exception, SystemExit):
        pass
    out = []
    for w in worktrees(repo):
        p = w.get("worktree")
        if not isinstance(p, str) or w.get("bare") or os.path.realpath(p) == os.path.realpath(repo) or internal_tree(p) \
                or p.endswith((f"-wt-{WAITING_BRANCH}", "-wt-batch")) or not os.path.isdir(p):
            continue
        rp = os.path.realpath(p)
        if rp in landers or rp in live or any(c == rp or c.startswith(rp + "/") for c in cwds):
            continue
        if superseded_mark(rp):  # its work went into another change (superseded_duty closes it when that lands)
            continue
        if parked_mark(rp):  # parked for a reason (park_duty re-surfaces it when its condition is met)
            continue
        if rp in sessions:  # a live Claude session works in it: it has an owner
            continue
        act = gitdir_activity(repo, p) or 0
        if t - act < env_num("AGENTCTL_ORPHAN_IDLE", ORPHAN_IDLE):
            continue
        commits, key = orphan_key(repo, p)
        if commits and key:
            why = blame_land_refusal(p, commits, p, records=records)
            if why:  # 2026-11-01: movies-wt-revert-629 submitted as an orphan after its blame was withdrawn
                if dead is not None:
                    dead.append((p, why))
                continue
            why = orphan_refusal(p, commits)
            if why:  # 2026-11-06: es-gate-check (check/*, held by the owner) rebased and queued as an orphan
                if refused is not None:
                    refused.append((p, key, why))
                continue
            out.append((p, commits, key, orphan_verdict(p, lands)))
    return out


# NOT FOR LANDING (2026-11-06 22:51Z, es-gate-check: a check/* worktree holding a gate re-enable the owner holds was
# queued as an orphan). Nothing submits, unasked, work that says it is not for landing: a branch named check/*,
# measure/*, experiment/*, probe/*, prototype/*, spike/*, wip-* or wip/*, or a commit with a `Hold: <why>` trailer
# (held_commit; `land --release-hold` is the owner's go). Such a worktree is said once (`orphan <name>: not submitted
# (...)`) and never escalated: it is not stranded, it is kept.
ORPHAN_NOT_FOR_LANDING = re.compile(r"^((check|measure|experiment|probe|prototype|spike)/|wip[-/])", re.I)


def orphan_branch_refusal(branch):
    if ORPHAN_NOT_FOR_LANDING.match(branch or ""):
        return f"branch {branch} is not for landing"
    return None


def orphan_refusal(wt, commits):
    """Why orphan duty must never submit this worktree's commits (not for landing), or None."""
    return orphan_branch_refusal(branch_of(wt)) or held_commit(wt, commits) or do_not_land(wt, commits, branch_of(wt))


def orphan_land_entry(wt, lands=None):
    """The newest final land entry for the orphan: from its own worktree or the throwaway tree its re-land ran in."""
    lands = lands or land_entries_once()
    es = [e for e in (last_land_entry(wt, lands), last_land_entry(reland_tree_path(wt), lands)) if e]
    return max(es, key=lambda e: float(e.get("at") or 0)) if es else None


def orphan_verdict(wt, lands=None):
    """The newest final land verdict for the orphan (orphan_land_entry's), or None."""
    e = orphan_land_entry(wt, lands)
    return str(e.get("verdict") or "") if e else None


# LAND INTENT (2026-11-06, es-gate-check: queued though nobody had ever asked to land it). Orphan duty submits only
# work its author once meant to land: a worktree that ran `agentctl land` itself (a land entry in the inbox with that
# cwd) or has a change item of its own. A worktree that never did -- an agent that died mid-task, a check or design
# tree -- goes to a person (UNLANDED ORPHAN) instead: nothing lands that its author did not ask to land.
def land_intent(wt, items=None, lands=None):
    if (items or {}).get("last"):
        return True
    return bool((lands or land_entries_once())().get(os.path.realpath(wt)))


def orphan_submitted_alert(wt, commits, msg):
    """ORPHAN SUBMITTED, an owner alert: recovery put somebody's work into the queue -- say which, so it can be stopped."""
    subjects = [f"{c[:10]} {git(wt, 'log', '-1', '--format=%s', c, check=False).stdout.strip()[:80]}" for c in commits[:5]]
    more = f" (+{len(commits) - 5} more)" if len(commits) > 5 else ""
    inbox_write("maintain", f"ORPHAN SUBMITTED: {wt.replace(HOME, '~')} (branch {branch_of(wt) or 'detached'}): "
                            f"{len(commits)} commit(s) queued by orphan duty -- {'; '.join(subjects)}{more}. {msg}. "
                            "Stop it with `agentctl land cancel <id>` and `agentctl park <worktree> --reason ...`",
                [], [], 0, cwd=wt, item=f"orphan:{wt.replace(HOME, '~')}")


def probe_orphan_landed(a):
    """The orphan's change is on main (by patch), its worktree is gone, or somebody works in it again."""
    wt = a["worktree"]
    if not os.path.isdir(wt):
        return True, f"{wt} is gone"
    repo = a.get("repo") or movies_repo_path()
    _c, key = orphan_key(repo, wt)
    if key != a.get("key"):
        return True, "its commits are on main" if key is None else "its change moved on (new commits)"
    rp = os.path.realpath(wt)
    sup = superseded_mark(rp)
    if sup:
        return True, f"superseded by {sup['by']}"
    dead = blame_land_refusal(wt, _c, wt)
    if dead:
        return True, f"never landed: {dead}"
    kept = orphan_refusal(wt, _c)
    if kept:
        return True, f"not for landing: {kept}"
    if rp in live_lease_worktrees():
        return True, "a live claim works in it"
    park = parked_mark(rp)
    if park:
        return True, f"parked: {park['reason']}"
    owner = session_worktrees([rp]).get(rp)
    if owner:
        return True, f"a live session works in it ({owner})"
    cover = orphan_covered_by(wt, worktree_change_items(wt))  # orphan_duty's rule (a): an open change carries it
    if cover:
        return True, f"covered by {cover}"
    return False, f"{len(_c)} commit(s) still not on main, nobody in it"


def orphan_covered_by(wt, items):
    """The id of an open change item whose head carries every commit of the orphan (by patch or content), else None."""
    for it in items.get("open") or []:
        head = (it.get("data") or {}).get("head")
        if not head or not git_ok(wt, "cat-file", "-e", f"{head}^{{commit}}"):
            continue
        commits, left = cherry_landed(wt, head, "HEAD")
        if commits is not None and not left:
            return it["id"]
    return None


def orphan_escalate(repo, wt, name, commits, key, rec, verdict):
    text = (f"UNLANDED ORPHAN: {wt.replace(HOME, '~')}: {len(commits)} commit(s) not on {remote()}/main, its author "
            f"gone, still not landed after a {rec['actions'][-1]['what']} ({verdict or 'never landed'}) -- land it, "
            "hand it to an agent, park it (`agentctl park <worktree> --reason ...`), or drop it")
    c = open_condition("UNLANDED_ORPHAN", text, run_probe("orphan_landed", {"worktree": wt, "key": key, "repo": repo}),
                       [], key=re.sub(r"[^\w.-]+", "_", f"orphan-{name}"), command="maintain", cwd=wt)
    rec["escalated"] = ts() if c else None
    return [f"orphan {name}: escalated UNLANDED ORPHAN" if c else f"orphan {name}: cleared"]


def orphan_duty(repo=None, dry_run=False, now_=None):
    """One maintain pass over the orphans: one action each, then UNLANDED ORPHAN. Returns what it did."""
    repo = repo or movies_repo_path()
    if not os.path.isdir(repo):
        return []
    did, acted, dead = superseded_duty(repo, dry_run) + park_duty(repo, dry_run, now_), 0, []
    path = recovery_dir("orphans.json")
    st = read_record(path) or {}  # after park_duty: a released park's records are gone (its one submit again)
    fixers = read_record(orphan_fixers_path()) or {}
    fixers_changed = fixer_sent = False
    refused, lands = [], land_entries_once()  # one inbox read for the whole pass
    cands = orphan_candidates(repo, now_, dead, refused, lands)
    for wt, key, why in refused:  # not for landing: said once, never submitted, never escalated
        rec = st.get(key) or {"worktree": wt, "first": ts(), "actions": []}
        if rec.get("refused") != why:
            did.append(f"orphan {os.path.basename(wt)}: not submitted ({why})")
            rec["refused"] = why
        st[key] = rec
    for wt, why in dead:  # a dead blame revert: never submitted; blame's own worktree removed (only its reverts in it)
        name = os.path.basename(wt)
        did.append(f"orphan {name}: not submitted ({why})" if dry_run else
                   f"orphan {name}: not submitted ({why}); {blame_drop_revert_worktree(repo, wt, None, why)}")
    for wt, commits, key, verdict in cands:
        rec = st.get(key) or {"worktree": wt, "first": ts(), "actions": []}
        name = os.path.basename(wt)
        if dry_run:
            did.append(f"orphan {name}: {len(commits)} commit(s); last verdict: {verdict or 'never landed'}")
            continue
        items = worktree_change_items(wt)
        cover = orphan_covered_by(wt, items)
        if cover:  # (a) already in the queue under another change: its verdict is that change's (the probe clears too)
            if rec.get("covered_by") != cover:
                did.append(f"orphan {name}: carried by open change {cover}: not submitted")
            rec["covered_by"] = cover
            st[key] = rec
            continue
        was = rec.pop("covered_by", None)
        if was and rec["actions"]:  # escalated before its carrier came; the carrier closed (rejected, cancelled)
            # without landing it: the orphan reopens, to a person. Never acted on yet: its one submit, as before.
            rec["actions"].append({"at": ts(), "what": f"none (the change carrying it, {was}, closed unlanded)"})
            did += orphan_escalate(repo, wt, name, commits, key, rec, verdict or f"{was} closed unlanded")
            st[key] = rec
            continue
        if not rec["actions"]:
            last = items.get("last")
            if last and last["state"] == "cancelled":  # (b) somebody cancelled it: only a person submits it again
                rec["actions"].append({"at": ts(), "what": f"none (its last change {last['id']} was cancelled)"})
                did += orphan_escalate(repo, wt, name, commits, key, rec, verdict or f"cancelled ({last['id']})")
                st[key] = rec
                continue
            if not land_intent(wt, items, lands):  # LAND INTENT: its author never asked to land it -- a person decides
                rec["actions"].append({"at": ts(), "what": "none (it never ran `agentctl land`)"})
                did += orphan_escalate(repo, wt, name, commits, key, rec,
                                       "its author never ran `agentctl land` there: nothing submits it unasked")
                st[key] = rec
                continue
            if acted >= ORPHAN_PER_PASS:  # a few a pass: the queue is shared
                continue
            acted += 1  # SUBMITTED ONCE (design §11 I9): its checks then decide; a rejection is its red's to rescue
            w = reland_worktree(None, {"worktree": wt, "attempt": 1, "repo": repo})
            rec["actions"].append({"at": ts(), "what": "submit", "msg": w})
            did.append(f"orphan {name}: {rec['actions'][-1]['what']} ({verdict or 'never landed'})")
            if not str(w).startswith("not landed"):
                orphan_submitted_alert(wt, commits, w)
        elif not rec.get("escalated"):
            last_at = parse_time(rec["actions"][-1]["at"]).timestamp()
            e = orphan_land_entry(wt, lands)
            after = str(e.get("verdict") or "") if e and float(e.get("at") or 0) >= last_at - 1 else None
            msg = str(rec["actions"][-1].get("msg") or "")
            if msg.startswith("not landed again:"):  # the submit itself was refused: that is its verdict
                after = msg
            if after is not None and orphan_infra(after):  # (3) infra, not the change: submitted again, never escalated
                n = int(rec.get("infra_retries") or 0)
                wait_s = ORPHAN_INFRA_BACKOFF[min(n, len(ORPHAN_INFRA_BACKOFF) - 1)]
                if time.time() - last_at < wait_s:
                    pass
                elif not store_answers():
                    if rec.get("infra_said") != after[:120]:
                        rec["infra_said"] = after[:120]
                        did.append(f"orphan {name}: its last try failed on infra ({after[:120]}); retried when the "
                                   "store answers")
                elif acted < ORPHAN_PER_PASS:
                    acted += 1
                    rec["infra_retries"] = n + 1
                    w = reland_worktree(None, {"worktree": wt, "attempt": n + 2, "repo": repo})
                    rec["actions"].append({"at": ts(), "what": "submit", "msg": w, "infra": after[:200]})
                    did.append(f"orphan {name}: submit again (its last try failed on infra: {after[:120]})")
                    if not str(w).startswith("not landed"):
                        orphan_submitted_alert(wt, commits, w)
            elif time.time() - last_at >= env_num("AGENTCTL_ORPHAN_IDLE", ORPHAN_IDLE):  # its action had its time
                rw = os.path.realpath(wt)
                real = after if after is not None else verdict
                if real and ORPHAN_REAL_FAILURE.search(real) and rw not in fixers:  # (4) one fixer, then a person
                    if not fixer_sent and len(orphan_fixers_alive()) < env_num("AGENTCTL_ORPHAN_FIXERS", ORPHAN_FIXERS_MAX):
                        ok, line = orphan_dispatch(repo, wt, name, real)
                        rec["actions"].append({"at": ts(), "what": "fixer", "msg": line})
                        fixers[rw] = {"at": ts(), "key": key, "msg": line, "verdict": real[:300]}
                        fixers_changed = True
                        fixer_sent = True
                        did.append(f"orphan {name}: fixer dispatched ({real[:120]}): {line}")
                    else:
                        did.append(f"orphan {name}: a fixer waits for a free slot ({real[:80]})")
                else:
                    if rw in fixers and rec["actions"][-1]["what"] != "fixer":
                        rec["actions"].append({"at": ts(), "what": "fixer (ended unlanded)",
                                               "msg": fixers[rw].get("msg")})
                    did += orphan_escalate(repo, wt, name, commits, key, rec, real)
        st[key] = rec
    if not dry_run:
        write_json(path, st)
        if fixers_changed:
            write_json(orphan_fixers_path(), fixers)
    return did
