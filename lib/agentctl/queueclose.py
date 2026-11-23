# agentctl, part `queueclose`: one queue closed by content: bounces, regen-only rows, overdue regen holds.
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import os
import time
from typing import Any
from agentctl.clock import Clock
from agentctl.core import cherry_landed, env_num, fmt_age, git, git_ok, host_role, process_cache, remote
from agentctl.timefmt import ts
from agentctl.statefile import read_record
from agentctl.classify import network_failure, network_only
from agentctl.store import open_store
from agentctl.repos import movies_repo_path, precheck_hook, waiting_supported
from agentctl.policy import policy_blocks
from agentctl.waitingdir import ENTRY_FINAL, save_entry, waiting_entries, waiting_state, wlog
from agentctl.procs import safe_cwd
from agentctl.inboxentry import inbox_write
from agentctl.clones import clone_shared, fetch_main, note_clone_suspect
from agentctl.queueslots import check_tree, check_tree_slot, checker_lease, entry_owned, queue_verdict, set_change
from agentctl.conditions import open_condition, run_probe
from agentctl.queue import (
    FETCH_MAIN_FAILED, INFRA_CHECK_FAILURE, STALLED_REASON, STALL_KEEP_PLACE, VERIFY_MODE, module_overlap,
    range_paths, requeue_infra, verify_areas_supported, verify_supported)
from agentctl.patches import equivalent_on
from agentctl.generated import generated_entry
from agentctl.conflicts import CONFLICT_PLACE_KEPT, conflict_cause, conflict_paths
from agentctl.queuerecheck import recheck_reason
from agentctl.redreplay import fix_replay_refusal
from agentctl.changekind import HOLD_SHOWN, classify_change, kind_text, queue_facts
from agentctl.gov import gov_class_of
from agentctl.waiting import already_landed, cancel_entry_remote, must_wait, waiting_commits
from agentctl.queuesubmit import SUBMIT_FIELDS, submit_ref
from agentctl.redseen import gate_reds
from agentctl.blamerecords import blame_land_refusal
from agentctl.queuebatch import run_entry_checks
from agentctl.queuediff import CHECK_TIMEOUT, TIMEOUT_RETRIES, judge_failure, keys_text, rebase_in_tree
from agentctl.statemachines import StaleVersion, TerminalState, change_event, machine_state


# ONE QUEUE, CLOSED BY CONTENT (2026-10-30 split brain: six pre-cutover entries in the laptop's local waiting dir and
# four store items mirrored from them sat "waiting" for 11 h, every one already on main under rebased shas, and W1
# escalated). A change whose commits are on main BY CONTENT (`git cherry`: landing rebases, so never by sha) is LANDED,
# looked at every LANDED_LOOK_EVERY s per change, every tick: the server's entries and items, and on the laptop the
# store's items its clone has the commits of. The laptop holds no queue of its own: what its local dir still holds
# goes to ci-runner's queue (migrate_local_queue). A store item no queue carries is a STRANDED CHANGE condition.
LANDED_LOOK_EVERY = 600
_landed_looked = process_cache({})


def change_landed(repo, head, commits=None):
    """(landed, via): landed True when the change at head is on main by content (head an ancestor, or every commit of it
    patch-equivalent there or with a content twin, cherry_landed), False when not, None when this clone cannot tell (head unknown here); via the main commit
    carrying its last commit, when known."""
    main = f"refs/remotes/{remote()}/main"
    if not head or not git_ok(repo, "cat-file", "-e", f"{head}^{{commit}}"):
        return None, None
    if git_ok(repo, "merge-base", "--is-ancestor", head, main):
        return True, head
    limit = [f"{commits[0]}^"] if commits and git_ok(repo, "cat-file", "-e", f"{commits[0]}^^{{commit}}") else []
    commits, left = cherry_landed(repo, main, head, *limit)
    if commits is None:
        return None, None
    if not commits or left:
        return False, None
    return True, equivalent_on(repo, head, main)


def landed_look_due(cid, t):
    if t - _landed_looked.get(cid, 0) < env_num("AGENTCTL_LANDED_LOOK", LANDED_LOOK_EVERY):
        return False
    _landed_looked[cid] = t
    return True


def close_item_landed(st, cid, why, via=None):
    """The store's change item cid, unless terminal (or missing), LANDED with an event saying why; via (the main
    commit carrying it) is its landed sha, which await_entry watches. True when moved."""
    it = st.item("change", cid)
    if not it or machine_state("change", it["state"]).terminal:
        return False
    try:
        st.transition("change", cid, "landed", it["version"], data={"why": why, **({"landed_sha": via} if via else {})},
                      event={"text": f"LANDED {cid}: {why}", "refs": {"entry": cid}})
        return True
    except (StaleVersion, TerminalState):
        return False


def close_landed_changes(repo, clock, d=None, store=None):
    """Every open change whose commits are on main by content closes as LANDED: the queue entries of d (with their
    items), then the store's other open items (by their head). Returns what it did."""
    st, t = store or open_store(clock.time), clock.time()
    did, carried = [], set()
    for e in waiting_entries(d, open_only=False) if d else []:
        if e.get("status") in ENTRY_FINAL or e.get("status") == "pushing":
            continue
        carried.add(e["id"])
        landed, via = change_landed(repo, e.get("head"), e.get("commits")) if landed_look_due(e["id"], t) else (0, 0)
        if landed:
            already_landed(d, e, via)
            did.append(f"{e['id']}: its commits are on main (by content): landed")
            carried.discard(e["id"])
    for it in st.items("change"):
        if machine_state("change", it["state"]).terminal or it["state"] == "pushing" or it["id"] in carried:
            continue
        if not landed_look_due(it["id"], t):
            continue
        landed, via = change_landed(repo, (it.get("data") or {}).get("head"))
        if landed and close_item_landed(st, it["id"], "its commits are on main (by content)", via):
            did.append(f"{it['id']}: its commits are on main (by content): landed")
    return did


def stranded_changes(repo, d, store=None, now_=None):
    """THE SERVER'S: each open store change item its queue (d) has no entry for -- no source, nothing will ever check,
    push or close it -- opens a STRANDED CHANGE condition naming it; its probe (the item closed, or an entry for it)
    closes it. Returns the ids."""
    st = store or open_store()
    entries = {e["id"] for e in waiting_entries(d, open_only=False)}
    out = []
    for it in st.items("change"):
        if machine_state("change", it["state"]).terminal or it["id"] in entries:
            continue
        r = run_probe("change_source", {"id": it["id"], "repo": repo})
        if r.ok is False:
            dd = it.get("data") or {}
            open_condition("change-stranded", f"STRANDED CHANGE {it['id']} ({it['state']}, head "
                           f"{str(dd.get('head') or '?')[:10]}, {dd.get('worktree') or '?'}): no queue entry carries it, "
                           "so nothing checks, pushes or closes it. Landed: it closes itself once a clone with its "
                           f"commits sees them on main. Else `agentctl land cancel {it['id']}`, and land it again.",
                           r, [], key=f"change-stranded-{it['id']}", store=st)
            out.append(it["id"])
    return out


def probe_change_source(a):
    """The change item is closed, or the server's queue carries it again."""
    it = open_store().item("change", a["id"])
    if not it or machine_state("change", it["state"]).terminal:
        return True, f"change {a['id']} is {it['state'] if it else 'gone'}"
    d = waiting_state(a.get("repo") or movies_repo_path())[0]
    if os.path.exists(os.path.join(d, "entries", f"{a['id']}.json")):
        return True, f"change {a['id']} is in the queue"
    return False, f"change {a['id']} is {it['state']} and no queue entry carries it"


def migrate_local_queue(repo, store=None):
    """THE LAPTOP'S (sync role): its local waiting dir holds no queue. Each open entry there is LANDED when its commits
    are on main by content, else pushed to refs/heads/land/<id> and submitted to ci-runner's queue under its own id
    (the entry: `migrated`). One that cannot move is said loudly (stderr, and its author's inbox once an hour).
    Returns what it did."""
    if host_role() != "sync" or not os.path.isdir(repo):
        return []
    d = waiting_state(repo)[0]
    st = store or open_store()
    did = []
    for e in waiting_entries(d, open_only=False):
        if e.get("status") in ENTRY_FINAL:
            continue
        landed, via = change_landed(repo, e.get("head"), e.get("commits"))
        if landed:
            e.update(status="landed", landed_sha=via, decided=ts(), why="its commits are on main (by content)")
            save_entry(d, e)
            close_item_landed(st, e["id"], "its commits are on main (by content)", via)
            did.append(f"{e['id']}: its commits are on main (by content): landed")
            continue
        why = None
        if not submit_ref(repo, d, e, to_server=True):
            why = f"could not push refs/heads/land/{e['id']}"
        else:
            try:
                st.submit({**{k: e.get(k) for k in SUBMIT_FIELDS}, "id": e["id"]})
            except (Exception, SystemExit) as ex:
                why = f"ci-runner's store refused or did not answer ({str(ex)[:160]})"
        if why is None:
            e.update(status="migrated", decided=ts(), why="moved to ci-runner's queue")
            save_entry(d, e)
            did.append(f"{e['id']}: moved to ci-runner's queue")
            continue
        line = (f"LOCAL QUEUE: {e['id']} ({e.get('worktree')}) is in the laptop's local queue, which nothing runs: {why}; "
                "tried again every sync cycle")
        did.append(line)
        if time.time() - float(e.get("migrate_told_at") or 0) > 3600:
            e["migrate_told_at"] = time.time()
            save_entry(d, e)
            inbox_write("land", line, [line], [], 1, cwd=e.get("worktree") or safe_cwd())
    return did


def change_rows(store: Any = None) -> list[dict[str, Any]]:
    """THE QUEUE as everyone reads it (`land status`, the status line, the page): the store's open change items,
    by id, as {id, state, worktree, head, deadline, level, hold, kind (a red fix's, changekind.py), since (submitted)}."""
    rows = [{"id": i["id"], "state": i["state"], "worktree": (i.get("data") or {}).get("worktree"),
             "head": (i.get("data") or {}).get("head"), "deadline": i["deadline"], "level": i["level"],
             "hold": ((i.get("data") or {}).get("hold") or {}).get("why") if i["state"] in HOLD_SHOWN else None,
             "kind": kind_text((i.get("data") or {}).get("change_kind")) or None,
             "resolved": (i.get("data") or {}).get("autoresolved") if i["state"] == "checking" else None,
             "since": float((i.get("data") or {}).get("submitted_at") or i.get("entered_at") or 0)}
            for i in (store or open_store()).items("change") if not machine_state("change", i["state"]).terminal]
    return sorted(rows, key=lambda r: r["id"])


def provisional_hint(tree, e):
    """`; its F kept main's rows ...: rebase, run `regen`, land again` for a change whose generated files the check
    tree merged keeping main's side of a row both changed (MAIN'S ROW, PROVISIONALLY, waiting.py); '' otherwise."""
    files = e.get("provisional") or []
    cmds = sorted({" ".join(g["regen"]) for g in (generated_entry(tree, f) for f in files) if g and g.get("regen")})
    return (f"; its {', '.join(files)} kept {remote()}/main's rows where both sides changed them (no regen command "
            f"runs where the queue does): rebase onto {remote()}/main, run {' and '.join(f'`{c}`' for c in cmds) or 'the regen command'}, "
            "commit and land again") if files else ""


# A ROW ONLY A REGEN DECIDES IS NO BOUNCE (2026-11-19, lane F2b): main re-measured the coupling-baseline rows the change
# changed too; ci-runner runs no JVM, so the check tree kept main's rows (MAIN'S ROW, PROVISIONALLY, waiting.py) and the
# checks' ratchet failed over them. That is no defect of the change, only a regeneration this host cannot run: the
# change is HELD in its place (queued, not checked again) and its agent told ONE command -- `agentctl land` in its
# worktree, which rebases onto main and runs the regen where the JVM is (the laptop's conflict_resolve) -- and the
# change queued again from that worktree takes the held entry's place and queue age (server.py queue_change). A hold
# nobody answers within CONFLICT_PLACE_KEPT is NEVER rejected (the owner, 2026-11-20: "2 drop the reject"): it RECOVERS
# (regen_hold_overdue, below).
def regen_hold(d, e, tree, failing, out):
    """Hold change e for its agent's regeneration (above): queued with retry_after at the hold's end, `regen_hold` on
    its entry, its author told the one command (the event's inbox entry). Returns 1 (nothing pushed)."""
    files = e.get("provisional") or []
    cmds = sorted({" ".join(g["regen"]) for g in (generated_entry(tree, f) for f in files) if g and g.get("regen")})
    cmd = f"cd {e['worktree']} && ~/bin/agentctl land"
    t = time.time()
    why = (f"REGEN NEEDED {e['id']}: its {', '.join(files)} kept {remote()}/main's rows where both sides changed them "
           f"(ci-runner runs no {' / '.join(cmds) or 'regen command'}) and its checks failed over them ({failing}); it "
           f"keeps its place in the queue: run `{cmd}` (rebases onto {remote()}/main and runs "
           f"{' and '.join(f'`{c}`' for c in cmds) or 'the regen'} on this machine)")
    set_change(d, e, "queued", event=change_event(e, why, (out or "").splitlines()[-40:], code=None, final=False),
               why=why, retry_after=t + CONFLICT_PLACE_KEPT,
               regen_hold={"since": t, "cmd": cmd, "files": files, "head": e.get("head")})
    wlog(d, f"{e['id']}: held for its regeneration ({', '.join(files)}): `{cmd}`")
    return 1


# AN OVERDUE REGEN HOLD RECOVERS (the owner, 2026-11-20: "1 approve, 2 drop the reject"; lane F2b's 24-h NOT LANDED is
# struck). Past CONFLICT_PLACE_KEPT the change tick runs the regeneration AGAIN: a new round on the change's item
# (`regen_round`), which the laptop's sync answers by starting `agentctl land` in its worktree once more (server.py
# answer_regen_holds, once per change, head and round; it says so in `regen_answered`). When the hold still stands
# REGEN_RECOVERY_WAIT later -- the laptop unreachable, its JVM down, the regen failing -- the owner is told ONCE (an
# escalation on the change: what is held and why, whether the laptop answered). The change stays held in its place.
REGEN_RECOVERY_WAIT = 2 * 3600  # s from the re-run to the owner's one escalation ($AGENTCTL_REGEN_RECOVERY_WAIT)


def regen_hold_overdue(st, d, e, t):
    """The change tick's step for regen-held entry e (above): False while within its bound (the retry decides), else
    True -- held, never checked again nor rejected -- with the recovery's round or escalation done; lines to `did`."""
    h = e.get("regen_hold") or {}
    if not h or t - float(h.get("since") or t) < CONFLICT_PLACE_KEPT:
        return False
    rec = h.get("recovery") or {}
    wait = env_num("AGENTCTL_REGEN_RECOVERY_WAIT", REGEN_RECOVERY_WAIT)
    if rec and (rec.get("escalated") or t - float(rec.get("at") or t) < wait):
        return True
    it = st.item("change", e["id"])
    if not it or machine_state("change", it["state"]).terminal:
        return True
    files, cmd = ", ".join(h.get("files") or []), h.get("cmd")
    if not rec:
        rnd = int((it.get("data") or {}).get("regen_round") or 0) + 1
        h["recovery"] = {"at": t, "round": rnd}
        text = (f"REGEN RECOVERY {e['id']}: held {fmt_age(t - float(h['since']))} for its regeneration of {files}; "
                f"running it again (the laptop's sync starts `{cmd}`), round {rnd}")
        update, ev = {"regen_round": rnd}, {"kind": "regen.recover", "severity": "info", "text": text}
    else:
        answered = int((it.get("data") or {}).get("regen_answered") or 0) >= int(rec.get("round") or 1)
        rec["escalated"] = t
        text = (f"REGEN HELD {e['id']}: held {fmt_age(t - float(h['since']))}: its {files} need a regen only the "
                f"laptop runs (both sides changed a row); run again {fmt_age(t - float(rec['at']))} ago, "
                + ("the laptop started it, yet it still holds" if answered else "the laptop never answered")
                + f" -- the laptop or its JVM is unreachable, or the regen fails: run `{cmd}` there, or "
                f"`agentctl land cancel {e['id']}`")
        update, ev = {}, {"kind": "regen.escalate", "severity": "escalate", "text": text}
    e["regen_hold"], e["retry_after"] = h, t + wait
    save_entry(d, e)
    try:
        if update:
            st.update_item("change", e["id"], it["version"], update)
            it = st.item("change", e["id"])
        st.transition("change", e["id"], it["state"], it["version"], event=ev)
    except StaleVersion:
        pass  # recorded on the entry; the item catches up next tick
    wlog(d, text)
    return True


def check_entry(repo: str, eid: str, clock: Clock) -> int:
    """THE EXECUTOR of one change's checks (`waiting check <id>`, detached): holding check/<entry> and a check tree slot
    (leases), it takes the change to checking and leaves it checked (with the tip and base it passed on), rejected (a
    test failure, a conflict) or queued again (infra). It never pushes: the tick does (push_change)."""
    d, _c, _w = waiting_state(repo)
    lease = checker_lease(eid)
    if not lease.acquire():  # another checker holds it (and progresses)
        return 0
    own = entry_owned(eid)
    if not own.acquire():
        lease.release()
        return 0
    try:
        e = read_record(os.path.join(d, "entries", f"{eid}.json"))
        if not e or e.get("status") not in ("queued", "checking") or e.get("backlog_batch"):  # a backlog batch's
            return 0
        fix = bool(e.get("fixes")) and classify_change(e, queue_facts(gate_reds(repo)), clock.time())["red_fix"]
        slot = check_tree_slot(repo, d, fix=fix)
        if not slot:  # all trees busy: it stays queued, the tick starts it again when one frees
            return 0
        i, slot_h = slot
        try:
            with clone_shared(repo):  # a clone swap (repair_clone) waits for the check
                return _check_entry(repo, d, e, i, clock, own)
        finally:
            slot_h.release()
    finally:
        own.release()
        lease.release()


def _check_entry(repo: str, d: str, e: dict[str, Any], i: int, clock: Clock, own: Any = None) -> int:
    hook = precheck_hook(repo)
    log_path = os.path.join(d, f"check-{e['id']}")
    if e.get("status") != "checking":
        set_change(d, e, "checking", event={"text": f"{e['id']}: checking"}, since=ts())
    fetched, ferr = fetch_main(repo)  # a corruption signature marks the clone for repair (clone_heal_tick)
    if not fetched:  # never checked on a stale main; git's own words say why (2026-10-29: "Could not read 409b8bb...")
        last = (ferr.strip().splitlines() or ["?"])[-1][:240]
        requeue_infra(d, e, f"{FETCH_MAIN_FAILED}: {last}", clock.time(), reason=FETCH_MAIN_FAILED, detail=ferr,
                      repo=repo)
        return 0
    base = git(repo, "rev-parse", f"{remote()}/main").strip()
    if not git_ok(repo, "cat-file", "-e", f"{e['head']}^{{commit}}"):  # phase 2: the change is on the remote, as land/<id>
        lr = git(repo, "fetch", "--quiet", remote(), f"refs/heads/land/{e['id']}", check=False)
        if not git_ok(repo, "cat-file", "-e", f"{e['head']}^{{commit}}"):
            lerr = (lr.stderr or "").strip()
            note_clone_suspect(repo, lerr)
            requeue_infra(d, e, f"its commits are not here and refs/heads/land/{e['id']} gave none"
                          + (f": {lerr.splitlines()[-1][:240]}" if lerr else ""), clock.time(), detail=lerr,
                          repo=repo)
            return 0
    rem = e.get("remote") or {}
    # ON ITS OWN BASE (2026-11-15 22:42Z: the reconciler's restart on a new pin killed its detached checkers; their
    # replacements, after main had moved, cancelled one half-done run and threw a green one away -- this adoption once
    # demanded the CURRENT main, from before the push learned to judge a moved main, e6a5b93). The run is adopted on
    # the base it checked, while main still holds that base; the push then judges what landed since, exactly as for a
    # checker that had survived: re-checked when it overlaps, pushed without re-check when it does not.
    adopt = (rem.get("head") == e["head"] and bool(rem.get("base")) and not rem.get("collected_at")
             and rem.get("status") not in ("cancelled", "skipped", "abandoned")
             and git_ok(repo, "cat-file", "-e", f"{rem.get('tip')}^{{commit}}")
             and git_ok(repo, "merge-base", "--is-ancestor", rem["base"], base))
    if adopt:  # a replacement checker (the last one lost its lease, died or re-exec'd mid-wait): the same tip, its run
        tree = check_tree(repo, i, rem["tip"])
        head, via, base = rem["tip"], None, rem["base"]
    else:
        if rem.get("run") is not None and not rem.get("status"):
            cancel_entry_remote(e, "superseded by a new round of its checks")
        e.pop("remote", None)
        e.pop("remote_run", None)
        e["round_since"] = ts()
        save_entry(d, e)
        tree = check_tree(repo, i, base)
        conflicts = []
        head, via = rebase_in_tree(d, tree, e, base, conflicts=conflicts)
    if via:
        already_landed(d, e, via if via != "an equivalent commit" else None)
        return 0
    if not head:
        # policy: no-longer-applies
        queue_verdict(d, e, "rejected", f"NOT LANDED: the change no longer applies on {remote()}/main {base[:10]} "
                      f"(main moved since QUEUED{conflict_cause(tree, e.get('base'), base, conflicts)}): rebase onto "
                      f"{remote()}/main and land again (it keeps its place in the queue)",
                      conflict_files=conflict_paths(conflicts), decided_on=base)
        return 1
    e["tip"] = head
    save_entry(d, e)
    reds = gate_reds(repo) if waiting_supported(repo) else set()
    commits = waiting_commits(tree, base, head)
    dead = blame_land_refusal(tree, commits, e.get("worktree"))
    if dead:  # queued before its blame was withdrawn or refuted (2026-11-01: revert-629 re-queued by orphan duty)
        # policy: blame-dead
        queue_verdict(d, e, "rejected", f"NOT LANDED: {dead}", decided_on=base)
        return 1
    # FIX REPLAY (redreplay.py): the laptop's land may hold no token; its replays ride with the submission (`replays`)
    unreplayed = fix_replay_refusal(repo, commits, proofs=e.get("replays"))
    if unreplayed and unreplayed[0] == "fix-replay" and policy_blocks("fix-replay", None, e["id"], unreplayed[1][:160]):
        queue_verdict(d, e, "rejected", f"NOT LANDED: {unreplayed[1]}", decided_on=base)  # policy: fix-replay
        return 1
    if unreplayed and unreplayed[0] == "fix-replay-outside" \
            and policy_blocks("fix-replay-outside", None, e["id"], unreplayed[1][:160]):
        queue_verdict(d, e, "rejected", f"NOT LANDED: {unreplayed[1]}", decided_on=base)  # policy: fix-replay-outside
        return 1
    joining = bool(reds) and must_wait(repo, commits, reds)
    cls = gov_class_of(repo, commits, reds)
    mode = "--checks-only" if joining else "--range"
    if adopt and rem.get("mode") == VERIFY_MODE:
        mode = VERIFY_MODE
    elif e.get("verify") and hook:  # THE RULE (recheck_reason) again, on the main this round checks on
        _n, why = recheck_reason(tree, e.get("full_base") or e.get("checked_base") or base, base, head,
                                 range_paths(tree, base, head), full_tip=e.get("full_tip"))
        if why is None and verify_supported(precheck_hook(tree)):
            mode = VERIFY_MODE
            full_base_ = e.get("full_base") or e.get("checked_base") or base
            areas = module_overlap(range_paths(tree, full_base_, base), range_paths(tree, base, head)) \
                if verify_areas_supported(tree) else []
            e["verify_areas"] = areas or None
            if areas:
                wlog(d, f"{e['id']}: sbt modules both sides touch ({', '.join(areas)}): verified with the area tests")
        else:
            wlog(d, f"{e['id']}: {why or f'the hook has no {VERIFY_MODE}'}: fully checked, not only verified")
    where = "no hook"
    if mode != VERIFY_MODE:
        e.pop("verify_areas", None)
    if hook:
        ok, out, where = run_entry_checks(repo, d, e, tree, base, head, mode, cls, log_path, clock, own)
        if ok is None:  # stalled: the laptop's, not the change's -- and what stalled it is said (2026-11-17: two
            # 13-s "stalls" whose cause no record kept)
            last = ((out or "").strip().splitlines() or [""])[-1].strip()[:240]
            # ITS PLACE KEPT (2026-11-17, ref-srp-54ebae 2 h old: a 13-s stall's 5-min backoff let younger changes take
            # every check tree, and it waited 15+ min more for one): the first STALL_KEEP_PLACE stalls in a row are due
            # at once -- the tree it just freed goes to it, first by age; then the infra backoff as for any infra cause
            same = int(e.get("infra_same") or 0) if e.get("infra_reason") == STALLED_REASON else 0
            requeue_infra(d, e, f"its checks stalled ({where}{': ' + last if last else ''})", clock.time(),
                          delay=0 if same < STALL_KEEP_PLACE else None, reason=STALLED_REASON, detail=out, repo=repo)
            return 0
        if not ok and INFRA_CHECK_FAILURE.search(out or ""):
            requeue_infra(d, e, f"its checks failed on the laptop, not on the change "
                             f"({INFRA_CHECK_FAILURE.search(out).group(0)})", clock.time())
            return 0
        if not ok and network_only(out):  # no verdict, on its first round too (judge_failure: the change's)
            requeue_infra(d, e, f"{NETWORK_REASON} ({network_failure(out)})", clock.time(), reason=NETWORK_REASON,
                          detail=out, repo=repo)
            return 0
        if not ok and CHECK_TIMEOUT.search(out or "") and int(e.get("timeout_retries") or 0) < TIMEOUT_RETRIES:
            e["timeout_retries"] = int(e.get("timeout_retries") or 0) + 1
            requeue_infra(d, e, f"its checks ran past their time cap ({where}; retry {e['timeout_retries']} of "
                                f"{TIMEOUT_RETRIES})", clock.time(), reason="its checks ran past their time cap", detail=out, repo=repo)
            return 0
        known_red = None
        if not ok:
            side, what = judge_failure(repo, d, e, base, out, clock.time())
            if side == "head" and e.get("provisional") and not CHECK_TIMEOUT.search(out or ""):
                return regen_hold(d, e, tree, keys_text(what), out)  # A ROW ONLY A REGEN DECIDES IS NO BOUNCE
            if side == "head":
                timed = " (and they ran past their time cap, " \
                        f"{TIMEOUT_RETRIES + 1} times)" if CHECK_TIMEOUT.search(out or "") else ""
                # policy: checks-failed
                queue_verdict(d, e, "rejected", f"NOT LANDED: the pre-push checks failed ({where}){timed}; failing "
                              f"with this change only: {keys_text(what)}{provisional_hint(tree, e)}; the change "
                              f"{e['head'][:10]} stays in {e['worktree']}", (out or "").splitlines(), decided_on=base)
                return 1
            srcs = sorted(set(what.values()))
            known_red = {"keys": sorted(what), "where": srcs, "base": base}
            wlog(d, f"{e['id']}: its checks failed only where its base fails too ({keys_text(what)}; {'; '.join(srcs)})"
                    ": it waits behind that red")
    full_base = (e.get("full_base") or e.get("checked_base") or base) if mode == VERIFY_MODE else base
    full_tip = e.get("full_tip") if mode == VERIFY_MODE else head
    areas = e.pop("verify_areas", None) if mode == VERIFY_MODE else None
    verified = (f"verified {head[:10]} on {base[:10]} (fully checked on {full_base[:10]}"
                + (f"; with the area tests: sbt modules both sides touch, {', '.join(areas)}" if areas else "") + ")") \
        if mode == VERIFY_MODE else f"checked {head[:10]} on {base[:10]}"
    set_change(d, e, "checked", data={"tip": head, "base": base, "mode": mode, "where": where},
               event={"text": f"{e['id']}: {verified} ({where})"
                      + (" -- failing only as its base fails" if known_red else "")},
               tip=head, checked_base=base, full_base=full_base, full_tip=full_tip, checked_mode=mode, checked_where=where, tree=i,
               infra_reason=None, infra_same=0, known_red=known_red, verify=False, push_alone=None,
               verified_areas=areas)
    wlog(d, f"{e['id']}: {verified} ({where})")
    return 0


NETWORK_REASON = "its checks could not reach the network"
