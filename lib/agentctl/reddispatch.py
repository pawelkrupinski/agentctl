# agentctl, part `reddispatch`: holders and the dispatcher: renewing claims, family holders, the capacity, spawning a fixer.
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import json
import os
import re
import time
from typing import Any
from agentctl.statemachines import StaleVersion, is_terminal, open_items
from agentctl.timefmt import parse_time, ts
from agentctl.redknobs import (
    knob_red_decided_revert_max, knob_red_fixers, knob_red_held_idle, knob_red_join_idle, knob_red_starve)
from agentctl.core import NO_SIGN, git, git_ok, remote
from agentctl.trailers import trailer_format, trailer_values
from agentctl.allowedpaths import allowed_dir
from agentctl.procs import socket_host
from agentctl.agewords import fmt_age
from agentctl.paths import reds_dir
from agentctl.waitingdir import waiting_state
from agentctl.inboxentry import inbox_write
from agentctl.redrepos import red_identity, red_owner_only, red_repo_note
from agentctl.worktreelife import WorktreeGuard
from agentctl.fixerenv import fixer_env
from agentctl.redrecord import lease_is_live, red_active, red_names, red_what
from agentctl.leases import LeaseKinds, lease_meta
from agentctl.land import spawn_land
from agentctl.red import DECIDED_REVERT, RED_CLAIM_TTL, RED_FIXER_TTL
from agentctl.fixerland import red_dispatch
from agentctl.redland import agentctl_revert_fix_replay
from agentctl.culpritfix import blame_note, blame_reland, lander_spawn
from agentctl.redclaim import red_joined_progress, red_wt_mark
from agentctl.redcrash import red_queue_family


def red_commits_naming(wt: str | None, names: Any, keys: tuple[str, ...] = ("Fixes-Red",)) -> list[tuple[str, float]]:
    """[(sha, commit time)] of the commits in worktree wt not on any remote whose trailers `keys` name one of names
    (runs, the key), the newest first."""
    if not allowed_dir(wt):
        return []
    fmt = f"%H%x09%ct%x09{trailer_format(keys)}"
    out = git(str(wt), "log", "HEAD", "--not", "--remotes", "-n", "200", f"--format={fmt}", check=False).stdout
    want = {str(n).lstrip("#") for n in names if n}
    got = []
    for line in out.splitlines():
        sha, ct, *vals = line.split("\t")
        if any(want & trailer_values(v) for v in vals) and ct.isdigit():
            got.append((sha, float(ct)))
    return got


def red_fixes_red_commits(wt: str | None, names: Any) -> str | None:
    """The first commit in worktree wt not on any remote whose `Fixes-Red:` names one of names (runs, the key), or None."""
    return next((sha for sha, _ct in red_commits_naming(wt, names)), None)


RED_PROGRESS_VERDICT = re.compile(r"^(DIAGNOSED|LANDED)\b")


def red_owned_progress_at(row: dict[str, Any], d: dict[str, Any]) -> float:
    """When red d, held by owned lease row, last showed progress of its own (0 when never)."""
    m = lease_meta(row)
    at = [float(m.get("progressed_at") or 0)]
    for h in d.get("history") or []:
        if RED_PROGRESS_VERDICT.match(str(h.get("outcome") or "")) and h.get("at"):
            try:
                at.append(parse_time(h["at"]).timestamp())
            except (ValueError, TypeError):
                pass
    at += [ct for _sha, ct in red_commits_naming(m.get("worktree"), red_names(d), ("Fixes-Red", "Diagnoses-Red"))[:1]]
    return max(at)


def red_owned_idle(store: Any, row: dict[str, Any], t: float) -> str | None:
    """None while a red under an owner's lease shows progress of its own (RED_HELD_IDLE), else the release outcome."""
    it = store.item("red", str(row.get("item") or "").split(":", 1)[-1]) if row.get("item") else None
    if not it or it["state"] != "leased":
        return None
    d = it["data"]
    since = max(float(row.get("acquired_at") or t), red_owned_progress_at(row, d))
    idle = float(knob_red_held_idle())
    if t - since < idle:
        return None
    why = (f"STALLED: no progress on {red_what(d)} for {fmt_age(t - since)} in {row['holder']}'s hand (no DIAGNOSED "
           f"line, commit naming it or note on it within {fmt_age(idle)}): released, offered again")
    inbox_write("red", f"RED NO PROGRESS: {why}", [f"RED NO PROGRESS: {why}"], [], 1,
                cwd=lease_meta(row).get("worktree"))
    return why


def red_holder_idle(store: Any, row: dict[str, Any], t: float) -> str | None:
    """The release outcome of a live red lease that shows no progress on its red (a joined one's, an owner-held
    one's), else None."""
    if LeaseKinds.is_joined(row):
        return red_joined_idle(store, row, t)
    return red_owned_idle(store, row, t) if LeaseKinds.is_owned(row) else None


def _joined_progress(it: Any, d: dict[str, Any], m: dict[str, Any]) -> str | None:
    """What shows a joined red worked on (a fix on it, or a commit naming it in the holder's worktree), else None."""
    if it and (it["state"] == "fix-pending" or is_terminal("red", it["state"]) or d.get("fix")):
        return f"fix {(d.get('fix') or {}).get('entry') or it['state']}"
    sha = red_fixes_red_commits(m.get("worktree"), red_names(d)) if it else None
    return f"commit {sha[:10]}" if sha else None


def _joined_item(store: Any, row: dict[str, Any]) -> Any:
    """The red item a joined lease row names, or None."""
    return store.item("red", str(row.get("item") or "").split(":", 1)[-1]) if row.get("item") else None


def _release_joined(store: Any, it: Any, d: dict[str, Any], row: dict[str, Any]) -> bool:
    """Mark the joined red's holder as released from it; False when another writer moved the red (the next pass)."""
    try:
        store.update_item("red", it["id"], it["version"],
                          {"family_released": sorted(set(d.get("family_released") or []) | {row["holder"]})})
    except StaleVersion:
        return False
    return True


def red_joined_idle(store, row, t):
    """None while a joined red is within its window or worked on (marked so), else the release outcome."""
    m = lease_meta(row)
    if not m.get("joined_at") or m.get("progressed"):
        return None
    it = _joined_item(store, row)
    d = (it or {}).get("data") or {}
    note = _joined_progress(it, d, m)
    if note:
        red_joined_progress(store, row, note)
        return None
    idle = knob_red_join_idle()
    if t - float(m["joined_at"]) < idle or (it and not _release_joined(store, it, d, row)):
        return None
    return (f"RELEASED: no progress on it {fmt_age(t - float(m['joined_at']))} after it joined {row['holder']}'s lease "
            f"(no commit, land or note for it within {fmt_age(idle)}): offered to a fixer of its own")


def _renew_target(store: Any, row: Any, m: Any, t: Any) -> Any:
    """(mark, ttl) a live claim or sibling row renews with, or None: another kind, or a sibling revoked here."""
    if LeaseKinds.is_claim(row):
        return red_wt_mark(m.get("worktree")), m.get("ttl") or RED_CLAIM_TTL
    if LeaseKinds.of(row) != LeaseKinds.SIBLING:
        return None
    prim = store.get(m.get("primary") or "")  # held by its run's fixer: lives exactly as long as that fixer's own lease
    if not lease_is_live(prim, t) or prim["holder"] != row["holder"]:
        store.revoke(row["name"], "its run's fixer ended")
        return None
    return f"with {prim['renewed_at']}", RED_FIXER_TTL


def red_renew_holders(store, t):
    """Each live interactive claim renewed by its worktree's activity (no activity: it lapses at its TTL); each sibling
    exactly as long as its run's fixer's own lease; a joined red released when it shows no progress (red_joined_idle)."""
    for row in store.list("red/"):
        if not lease_is_live(row, t):
            continue
        m = lease_meta(row)
        why = red_holder_idle(store, row, t)
        if why:
            store.revoke(row["name"], why)
            continue
        target = _renew_target(store, row, m, t)
        if target and target[0]:
            store.renew(row["name"], row["epoch"], row["secret"], *target)


# -- the dispatcher (the phase-2 shape: offers and leases only)
def red_fixers_alive(store, t):
    """One row per live FIXER (a fixer holding several reds' leases counts once)."""
    out = {}
    for r in store.list("red/"):
        if lease_is_live(r, t) and LeaseKinds.is_session(r):
            out.setdefault(r["holder"], r)
    return list(out.values())


RED_MODEL = 2  # the store's red data model (meta red_model): 2 = the queue (item 5); red_migrate brings older data up


def red_migrate(store):
    """IN-PLACE MIGRATION to RED_MODEL, once per store (meta red_model), run by the tick: every open red gets its
    identity fields (signature, family). The capacity classes' lease meta (`slot`) needs nothing: it is no longer read,
    and a fixer's lease ends with it. Idempotent; a red another writer moved meanwhile is done at the next tick.
    Returns how many reds it updated."""
    if int(store.meta_get("red_model", 1) or 1) >= RED_MODEL:
        return 0
    n, left = 0, 0
    for it in open_items(store, "red"):
        want = red_identity(it["data"])
        if want.items() <= it["data"].items():
            continue
        try:
            store.update_item("red", it["id"], it["version"], want)
            n += 1
        except StaleVersion:
            left += 1
    if not left:
        store.meta_set("red_model", RED_MODEL)
    return n


def red_cap():
    return int(knob_red_fixers())


def red_capacity(store, waited, t, alive=None):
    """(True, None) when a fixer may start now (fewer than RED_FIXERS alive), else (False, why): what it waits for and
    WHEN, `STARVED: ...` past RED_STARVE."""
    alive = red_fixers_alive(store, t) if alive is None else alive
    n, cap = len(alive), red_cap()
    if n < cap:
        return True, None
    first = min(alive, key=lambda r: float(r.get("max_until") or r.get("expires_at") or 0)) if alive else None
    hm = lambda x: time.strftime("%H:%M", time.localtime(x))
    eta = f"; next slot when {first['holder']} ends by {hm(float(first.get('max_until') or first.get('expires_at')))}" \
        if first else ""
    starve = knob_red_starve()
    head = f"capacity {n}/{cap}, waiting {fmt_age(waited)}"
    if waited >= starve:
        return False, f"STARVED: {head}, past its {fmt_age(starve)} window{eta}"
    return False, head + eta


def red_family_holder(store, fam, t, active=None):
    """The live primary lease on a red of queue family fam, or None: a fixer's own (not a sibling), or a CLAIM made
    inside a dispatched fixer (`red claim <M>` there: its record on the lease, 2026-11-07 -- the dispatcher started a
    second owner session beside the consolidated non-gate fixer). An interactive session's claim takes ONLY its red,
    never its family (the owner, 2026-11-08): family holding is for the sessions agentctl itself starts."""
    if not fam:
        return None
    active = active if active is not None else red_active(store)
    for row in store.list("red/"):
        if not lease_is_live(row, t) or not LeaseKinds.holds_family(row):
            continue
        it = active.get(row["name"].split("/", 1)[1])
        if it and red_queue_family(it["data"]) == fam:
            return row
    return None  # a holder is ONLY a live lease


def red_backoff_why(d: dict[str, Any], after: float) -> str:
    """Why red (data d) waits until `after`: claude's spent usage limit, or fixers that never got going (the last error)."""
    at, hist = time.strftime('%H:%M', time.localtime(after)), d.get("history") or []
    if hist and str(hist[-1].get("outcome")).startswith("LIMITED"):
        return f"claude's usage limit is spent: the next fixer at {at}"
    last = re.sub(r"^(NOT STARTED: )+", "", next((str(h.get("outcome") or "") for h in reversed(hist)
                                                  if str(h.get("outcome") or "").startswith("NOT STARTED")), ""))
    return f"its last {d.get('start_failures')} fixer(s) never got going; the next at {at}" + (
        f"; launch error: {last[:240]}" if last else "")


def red_dispatch_meta(pic, fam, owned, off, num):
    """The lease meta the dispatcher takes a red's offer with; `host`: the machine its wrapper runs on, the only one
    that may judge its pid (holder_work)."""
    return {"kind": "owned" if owned else "fixer", "gate": bool(pic.get("gate")), "family": fam,
            "tier": off["tier"], "run": num, "workflow": pic.get("workflow"), "host": socket_host()}


def _spawn_facts(o_r: str, pic: dict[str, Any], extra: Any) -> dict[str, Any]:
    """The red's facts a fixer or the revert fallback is started with, the run's other reds' jobs under the same fixer."""
    u = {"workflow": pic["workflow"], "job": pic.get("job"), "jobs": pic.get("jobs"), "kind": pic.get("kind") or "test",
         "line": pic.get("line"), "run": pic.get("run") or {}, "refuted": pic.get("refuted"), "predicts": pic.get("predicts"),
         "flake": pic.get("red_kind") == "flake", "diagnosis": pic.get("diagnosis"), "key": pic["key"],
         "names": set(pic.get("names") or []), "prior_work": pic.get("prior_work"), "old_note": pic.get("old_note"),
         "history": pic.get("history") or [], "repo": pic.get("repo"), "decision": pic.get("decision")}
    u["repo_note"] = red_repo_note(o_r, u["run"])
    for x in extra:  # the run's other reds, under the same fixer
        u["jobs"] = sorted(set(u.get("jobs") or [u["job"]]) | set(x["picture"].get("jobs") or []))
    return u


def _reland_before_fixer(store: Any, o_r: str, repo: str, off: dict[str, Any], u: dict[str, Any]) -> dict[str, Any]:
    """The red's blame; when its revert was refuted, the change relanded first (recorded on the red and in the inbox)."""
    pic = off["picture"]
    b = pic.get("blame") or {}
    if not (b.get("reland_due") and not b.get("reland")):
        return b
    rl = blame_reland(o_r, repo, u, b)
    it_ = store.item("red", off["item"].split(":", 1)[1])
    if it_:
        store.update_item("red", it_["id"], it_["version"], {"blame": dict(b, reland=rl)})
    inbox_write("red", f"RED RELAND: {red_what(pic)}: {rl['msg']}", [], [], 0 if rl.get("ok") else 1)
    return dict(b, reland=rl)


def _spawn_revert(store: Any, o_r: str, repo: str, off: dict[str, Any], took: dict[str, Any], u: dict[str, Any]) -> tuple[bool, str]:
    """The revert tier: the culprit reverted, noted on the lease and on the red (the owner's decided revert says why)."""
    pic = off["picture"]
    dec = pic.get("decision") or {}
    if DECIDED_REVERT.match(str(dec.get("answer") or "")):
        u["revert_why"] = (f"the owner decided {u['workflow']} #{u['run'].get('number')} is fixed by this revert "
                           f"({str(dec.get('answer'))[:120]!r}) and no fixer landed it within "
                           f"{fmt_age(knob_red_decided_revert_max())} "
                           "(decided_revert).")
    ok, msg = red_revert(o_r, repo, u, pic.get("culprit"))
    store.note(off["lease"], took["epoch"], took["secret"], msg)
    it_id = off["item"].split(":", 1)[1]
    it = store.item("red", it_id)
    if it:
        store.update_item("red", it_id, it["version"], {"reverted": {"at": ts(), "culprit": pic.get("culprit"), "ok": ok}})
    return ok, f"RED REVERT: {pic['workflow']} #{u['run'].get('number')}: {msg}"


def _spawn_fixer(store: Any, o_r: str, repo: str, off: dict[str, Any], took: dict[str, Any], u: dict[str, Any], extra: Any) -> Any:
    """A headless fixer of the tier: `resume` continues the last attempt's session and worktree, `deep` investigates."""
    pic, tier = off["picture"], off["tier"]
    last = (pic.get("history") or [{}])[-1]
    resume = None
    if tier == "resume":
        resume = dict(last, note=(f"YOUR FIX DID NOT FIX IT ({last.get('outcome')}): find out why, and fix it properly "
                                  f"(same rules; Fixes-Red: {u['run'].get('number')})."))
    effort = "max" if tier == "deep" else None
    if tier == "deep":
        u["diagnosis"] = red_investigation_brief(pic)
    return red_dispatch(o_r, repo, u, why=f"tier {tier}", resume=resume, effort=effort, lease=off["lease"], took=took,
                        extra=extra, store=store)


def red_spawn(store, o_r, repo, off, took, extra=()):
    """Start what the tier says, under the taken lease: the revert fallback, or a headless fixer (fresh worktree; the
    same session and worktree for `resume`)."""
    u = _spawn_facts(o_r, off["picture"], extra)
    if off["tier"] == "lander":  # THE DECISION RULE's determined culprit: reverted, its lander revived (blame.py)
        return lander_spawn(store, o_r, repo, off, took, u)
    b = _reland_before_fixer(store, o_r, repo, off, u)
    if b.get("culprit") is not None:
        u["blame_note"] = blame_note(b)
    if off["tier"] == "revert":
        return _spawn_revert(store, o_r, repo, off, took, u)
    return _spawn_fixer(store, o_r, repo, off, took, u, extra)


def red_investigation_brief(pic):
    verdicts = " | ".join(f"{h.get('at')}: {h.get('holder')}: {str(h.get('outcome'))[:160]}" for h in pic.get("history") or [])
    return (f"DEEP INVESTIGATION (effort max) after {pic.get('attempts')} attempts did not clear it. Failing: "
            f"`{pic.get('job')}`: {str(pic.get('line') or '')[:300]!r}. Refuted fixes: "
            f"{', '.join(x[:10] for x in pic.get('refuted') or []) or 'none'}. Attempts: {verdicts or 'none'}. Find the "
            "ROOT CAUSE they all missed and fix it in the code. A code red is never the owner's: no `red block` unless you "
            "PROVE the cause is outside the code and only the owner can act")


# DISPATCH OFF PER REPO (the owner, 2026-11-19 ~16:20Z: "no bitcashier builds" -- the laptop CPU-starved). The store's
# meta `repo_dispatch_off` names the repos whose reds are recorded and shown but never offered, dispatched or put to the
# owner (no vault Touch ID ask): red_step holds each in `dispatch-off` (its offer closed, its lease and its owner block
# released), the dispatcher skips any offer of one, and `red auto on --repo R` resumes them (offered at the next tick).
REPO_DISPATCH_OFF = "repo_dispatch_off"


def repo_dispatch_off_all(store):
    try:
        v = json.loads(store.meta_get(REPO_DISPATCH_OFF) or "{}")
        return v if isinstance(v, dict) else {}
    except (ValueError, TypeError):
        return {}


def repo_dispatch_off(store, o_r):
    """The record of o_r's dispatch being off (the owner's), or None."""
    return repo_dispatch_off_all(store).get(o_r)


def red_revert(o_r, repo, u, culprit):
    """The revert fallback: a revert of the culprit with `Fixes-Red: N`, landed by a detached `agentctl land` through
    the normal hook (its verdicts reach the inbox). (ok, message)."""
    num = u["run"].get("number")
    if red_owner_only(u.get("repo") or o_r):
        return False, f"no revert in {u.get('repo') or o_r}: its pushes are the owner's (red-repos.json owner-only)"
    _d, clone, _w = waiting_state(repo)
    wt, branch = f"{clone}-wt-revert-{num}", f"revert-{num}"
    if os.path.exists(wt) or git_ok(repo, "rev-parse", "--verify", "-q", f"refs/heads/{branch}"):
        return False, f"revert worktree {wt} or branch {branch} exists already"
    git(repo, "fetch", "--quiet", remote(), "main", check=False)
    if git(repo, "worktree", "add", "-q", "-b", branch, wt, f"{remote()}/main", check=False).returncode != 0:
        return False, "git worktree add failed"
    with WorktreeGuard(wt):  # EVERY EXIT PUTS THE WORKTREE BACK (slots.py) -- the land starts after it, never inside
        r = git(wt, *NO_SIGN, "revert", "--no-edit", culprit, check=False)
        if r.returncode != 0:
            git(wt, "revert", "--abort", check=False)
            return False, f"git revert {culprit[:10]} does not apply ({(r.stderr or r.stdout).strip()[:120]})"
        msg = git(wt, "log", "-1", "--format=%B").strip()
        why = u.get("revert_why") or f"{u['workflow']} #{num} still failed after two attempts (its tier ladder)."
        git(wt, *NO_SIGN, "commit", "--amend", "-q", "-m", f"{msg}\n\nThe revert fallback (agentctl red): {why}"
            f"\n\nFixes-Red: {num}\n"
            + agentctl_revert_fix_replay(f"agentctl's revert fallback of {culprit[:10]}: {u['workflow']} #{num} outlived "
                                         "two fix attempts, so main goes back to the code before it; nothing new to replay")
            + "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>")
    pid = spawn_land(wt, fixer_env(o_r, num), reds_dir(f"revert-{num}.log"))
    return True, f"landing a revert of {culprit[:10]} from {wt} (pid {pid})"
