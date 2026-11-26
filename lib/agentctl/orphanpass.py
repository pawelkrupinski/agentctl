# agentctl, part `orphanpass`: one maintain pass over the orphans: one action each (a submit from a throwaway tree, again on infra; one fixer for a real failure), then UNLANDED ORPHAN
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from agentctl.clock import epoch_now
import glob
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Optional
from agentctl.timefmt import parse_time, ts
from agentctl.statefile import read_record, write_json
from agentctl.core import branch_of, git, reds_dir, remote, slug, tilde
from agentctl.classify import NETWORK_CHECK_FAILURE
from agentctl.repos import movies_repo_path
from agentctl.fixerrecords import fixer_records
from agentctl.inboxentry import inbox_write
from agentctl.fixerenv import fixer_env
from agentctl.conditions import open_condition, recovery_dir, run_probe
from agentctl.blamerecords import blame_drop_revert_worktree
from agentctl.recovery import reland_worktree
from agentctl.superseded import superseded_duty, worktree_change_items
from agentctl.park import park_duty
from agentctl.orphans import land_entries_once, orphan_candidates, orphan_covered_by, orphan_land_entry
from agentctl.fixerland import spawn_fixer
from agentctl.fixerknobs import knob_orphan_idle
from agentctl.fixerknobs import knob_orphan_fixers


ORPHAN_PER_PASS = 3


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


def orphan_infra(verdict: Optional[str]) -> bool:
    v = str(verdict or "")
    return bool(ORPHAN_INFRA.search(v) or NETWORK_CHECK_FAILURE.search(v)) and not ORPHAN_REAL_FAILURE.search(v)


def store_answers(store: Any) -> bool:
    """True when the store answers now (an infra-failed orphan's retry waits for it)."""
    try:
        store.item("change", "store-answers-probe")
        return True
    except (Exception, SystemExit):
        return False


def orphan_fixers_path() -> str:
    path: str = recovery_dir("orphan-fixers.json")
    return path


def orphan_fixers_alive() -> list[dict[str, Any]]:
    return [r for r in fixer_records() if r.get("orphan") and r.get("alive")]


def orphan_dispatch(repo: str, wt: str, name: str, verdict: Optional[str]) -> tuple[bool, str]:
    """A background fixer IN the orphan's worktree (its author's), told the last verdict. (ok, message)."""
    num = f"orphan-{name}"
    d = reds_dir(num)
    os.makedirs(d, exist_ok=True)
    k = 1 + len(glob.glob(os.path.join(glob.escape(d), "fixer*.json")))
    rec_path = os.path.join(d, "fixer.json" if k == 1 else f"fixer-{k}.json")
    rec: dict[str, Any] = {"sig": None, "sigs": [], "jobs": ["land"], "run": num, "run_id": None, "workflow": "Unlanded change",
           "job": "land", "kind": "test", "line": (verdict or "")[:400], "gate": False, "worktree": wt, "started": ts(),
           "why": "orphaned change, author gone, its checks rejected it", "log": os.path.join(d, f"claude-{k}.log"),
           "names": [num], "orphan": True}
    p = spawn_fixer(repo, d, rec_path, rec, wt, fixer_env(slug(repo), num))
    inbox_write("maintain", f"ORPHAN FIXER DISPATCHED: {tilde(wt)} -> fixer session 'orphan · {name}' "
                            f"(`claude agents`): {str(verdict or '')[:200]}", [], [], 0, cwd=wt)
    return True, f"fixer pid {p.pid} in {tilde(wt)}"


# LAND INTENT (2026-11-06, es-gate-check: queued though nobody had ever asked to land it). Orphan duty submits only
# work its author once meant to land: a worktree that ran `agentctl land` itself (a land entry in the inbox with that
# cwd) or has a change item of its own. A worktree that never did -- an agent that died mid-task, a check or design
# tree -- goes to a person (UNLANDED ORPHAN) instead: nothing lands that its author did not ask to land.
def land_intent(wt: str, items: Optional[dict[str, Any]] = None, lands: Any = None) -> bool:
    if (items or {}).get("last"):
        return True
    return bool((lands or land_entries_once())().get(os.path.realpath(wt)))


def orphan_submitted_alert(wt: str, commits: Any, msg: Any) -> None:
    """ORPHAN SUBMITTED, an owner alert: recovery put somebody's work into the queue -- say which, so it can be stopped."""
    subjects = [f"{c[:10]} {git(wt, 'log', '-1', '--format=%s', c, check=False).stdout.strip()[:80]}" for c in commits[:5]]
    more = f" (+{len(commits) - 5} more)" if len(commits) > 5 else ""
    inbox_write("maintain", f"ORPHAN SUBMITTED: {tilde(wt)} (branch {branch_of(wt) or 'detached'}): "
                            f"{len(commits)} commit(s) queued by orphan duty -- {'; '.join(subjects)}{more}. {msg}. "
                            "Stop it with `agentctl land cancel <id>` and `agentctl park <worktree> --reason ...`",
                [], [], 0, cwd=wt, item=f"orphan:{tilde(wt)}")


def orphan_escalate(store: Any, repo: str, wt: str, name: str, commits: Any, key: str, rec: dict[str, Any],
                    verdict: Optional[str]) -> list[str]:
    text = (f"UNLANDED ORPHAN: {tilde(wt)}: {len(commits)} commit(s) not on {remote()}/main, its author "
            f"gone, still not landed after a {rec['actions'][-1]['what']} ({verdict or 'never landed'}) -- land it, "
            "hand it to an agent, park it (`agentctl park <worktree> --reason ...`), or drop it")
    c = open_condition("UNLANDED_ORPHAN", text, run_probe("orphan_landed", {"worktree": wt, "key": key, "repo": repo}),
                       [], key=re.sub(r"[^\w.-]+", "_", f"orphan-{name}"), command="maintain", cwd=wt, store=store)
    rec["escalated"] = ts() if c else None
    return [f"orphan {name}: escalated UNLANDED ORPHAN" if c else f"orphan {name}: cleared"]


def orphan_duty(store: Any, repo: Optional[str] = None, dry_run: bool = False, now_: Optional[float] = None) -> list[str]:
    """One maintain pass over the orphans: one action each, then UNLANDED ORPHAN. Returns what it did."""
    repo = repo or movies_repo_path()
    if not os.path.isdir(repo):
        return []
    return OrphanPass(store, repo, dry_run, now_).run()


@dataclass
class Orphan:
    """One orphan of a pass: its worktree, commits, change key, last land verdict, its record in orphans.json (rec)
    and its change items (worktree_change_items)."""
    wt: str
    commits: Any
    key: str
    verdict: Optional[str]
    rec: dict[str, Any]
    items: dict[str, Any] = field(default_factory=dict)

    @property
    def name(self) -> str:
        return os.path.basename(self.wt)


class OrphanPass:
    """One maintain pass: st is orphans.json (each orphan's record by its change key: a state dict, never the store),
    fixers the orphan fixers sent (by worktree); `acted` counts the submits, ORPHAN_PER_PASS a pass."""

    def __init__(self, store: Any, repo: str, dry_run: bool, now_: Optional[float]) -> None:
        self.store, self.repo, self.dry_run, self.now_ = store, repo, dry_run, now_
        self.did: list[str] = superseded_duty(repo, store, dry_run) + park_duty(repo, store, dry_run, now_)
        self.acted = 0
        self.path = recovery_dir("orphans.json")
        self.st = read_record(self.path) or {}  # after park_duty: a released park's records are gone (its one submit again)
        self.fixers = read_record(orphan_fixers_path()) or {}
        self.fixers_changed = self.fixer_sent = False
        self.lands = land_entries_once()  # one inbox read for the whole pass

    def run(self) -> list[str]:
        dead: list[Any] = []
        refused: list[Any] = []
        cands = orphan_candidates(self.repo, self.store, self.now_, dead, refused, self.lands)
        for wt, key, why in refused:  # not for landing: said once, never submitted, never escalated
            rec = self._record(key, wt)
            if rec.get("refused") != why:
                self.did.append(f"orphan {os.path.basename(wt)}: not submitted ({why})")
                rec["refused"] = why
            self.st[key] = rec
        for wt, why in dead:  # a dead blame revert: never submitted; blame's own worktree removed (only its reverts in it)
            name = os.path.basename(wt)
            self.did.append(f"orphan {name}: not submitted ({why})" if self.dry_run else
                            f"orphan {name}: not submitted ({why}); "
                            f"{blame_drop_revert_worktree(self.repo, wt, None, why, self.store)}")
        for wt, commits, key, verdict in cands:
            self._orphan(Orphan(wt, commits, key, verdict, self._record(key, wt)))
        if not self.dry_run:
            write_json(self.path, self.st)
            if self.fixers_changed:
                write_json(orphan_fixers_path(), self.fixers)
        return self.did

    def _record(self, key: str, wt: str) -> dict[str, Any]:
        return self.st.get(key) or {"worktree": wt, "first": ts(), "actions": []}

    def _orphan(self, o: Orphan) -> None:
        if self.dry_run:
            self.did.append(f"orphan {o.name}: {len(o.commits)} commit(s); last verdict: {o.verdict or 'never landed'}")
            return
        o.items = worktree_change_items(o.wt, self.store)
        if self._act(o):
            self.st[o.key] = o.rec

    def _act(self, o: Orphan) -> bool:
        """Its one action this pass; False when the pass's submits are spent (its record left as it was)."""
        cover = orphan_covered_by(o.wt, o.items)
        if cover:  # (a) already in the queue under another change: its verdict is that change's (the probe clears too)
            if o.rec.get("covered_by") != cover:
                self.did.append(f"orphan {o.name}: carried by open change {cover}: not submitted")
            o.rec["covered_by"] = cover
            return True
        was = o.rec.pop("covered_by", None)
        if was and o.rec["actions"]:  # escalated before its carrier came; the carrier closed (rejected, cancelled)
            # without landing it: the orphan reopens, to a person. Never acted on yet: its one submit, as before.
            self._escalate(o, f"none (the change carrying it, {was}, closed unlanded)", o.verdict or f"{was} closed unlanded")
            return True
        if not o.rec["actions"]:
            return self._first_action(o)
        if not o.rec.get("escalated"):
            self._after_action(o)
        return True

    def _escalate(self, o: Orphan, what: str, verdict: Optional[str]) -> None:
        """No action taken (what says why): UNLANDED ORPHAN, to a person."""
        o.rec["actions"].append({"at": ts(), "what": what})
        self.did += orphan_escalate(self.store, self.repo, o.wt, o.name, o.commits, o.key, o.rec, verdict)

    def _first_action(self, o: Orphan) -> bool:
        last = o.items.get("last")
        if last and last["state"] == "cancelled":  # (b) somebody cancelled it: only a person submits it again
            self._escalate(o, f"none (its last change {last['id']} was cancelled)", o.verdict or f"cancelled ({last['id']})")
            return True
        if not land_intent(o.wt, o.items, self.lands):  # LAND INTENT: its author never asked to land it -- a person decides
            self._escalate(o, "none (it never ran `agentctl land`)",
                           "its author never ran `agentctl land` there: nothing submits it unasked")
            return True
        if self.acted >= ORPHAN_PER_PASS:  # a few a pass: the queue is shared
            return False
        self.acted += 1  # SUBMITTED ONCE (design §11 I9): its checks then decide; a rejection is its red's to rescue
        self._submit(o, 1)
        self.did.append(f"orphan {o.name}: submit ({o.verdict or 'never landed'})")
        return True

    def _submit(self, o: Orphan, attempt_n: int, **extra: Any) -> None:
        """Re-landed from a throwaway tree; the owner told what was queued, unless the re-land itself refused."""
        w = reland_worktree(None, {"worktree": o.wt, "attempt": attempt_n, "repo": self.repo}, self.store)
        o.rec["actions"].append({"at": ts(), "what": "submit", "msg": w, **extra})
        if not str(w).startswith("not landed"):
            orphan_submitted_alert(o.wt, o.commits, w)

    def _after_action(self, o: Orphan) -> None:
        """Its action had its verdict: infra (submitted again), else, its time up, one fixer, then a person."""
        last_at = parse_time(o.rec["actions"][-1]["at"]).timestamp()
        after = self._verdict_since(o, last_at)
        if after is not None and orphan_infra(after):  # (3) infra, not the change: submitted again, never escalated
            self._infra_retry(o, after, last_at)
        elif epoch_now() - last_at >= knob_orphan_idle():  # its action had its time
            self._fixer_or_person(o, after if after is not None else o.verdict)

    def _verdict_since(self, o: Orphan, last_at: float) -> Optional[str]:
        """The land verdict since its last action, or None; a submit that was itself refused: that is its verdict."""
        e = orphan_land_entry(o.wt, self.lands)
        after = str(e.get("verdict") or "") if e and float(e.get("at") or 0) >= last_at - 1 else None
        msg = str(o.rec["actions"][-1].get("msg") or "")
        return msg if msg.startswith("not landed again:") else after

    def _infra_retry(self, o: Orphan, after: str, last_at: float) -> None:
        n = int(o.rec.get("infra_retries") or 0)
        if epoch_now() - last_at < ORPHAN_INFRA_BACKOFF[min(n, len(ORPHAN_INFRA_BACKOFF) - 1)]:
            return
        if not store_answers(self.store):
            if o.rec.get("infra_said") != after[:120]:
                o.rec["infra_said"] = after[:120]
                self.did.append(f"orphan {o.name}: its last try failed on infra ({after[:120]}); retried when the "
                                "store answers")
            return
        if self.acted >= ORPHAN_PER_PASS:
            return
        self.acted += 1
        o.rec["infra_retries"] = n + 1
        self._submit(o, n + 2, infra=after[:200])
        self.did.append(f"orphan {o.name}: submit again (its last try failed on infra: {after[:120]})")

    def _fixer_or_person(self, o: Orphan, real: Optional[str]) -> None:
        """(4) A real failure gets one fixer, then a person; anything else goes to a person."""
        rw = os.path.realpath(o.wt)
        if real and ORPHAN_REAL_FAILURE.search(real) and rw not in self.fixers:
            self._fixer(o, rw, real)
            return
        if rw in self.fixers and o.rec["actions"][-1]["what"] != "fixer":
            o.rec["actions"].append({"at": ts(), "what": "fixer (ended unlanded)", "msg": self.fixers[rw].get("msg")})
        self.did += orphan_escalate(self.store, self.repo, o.wt, o.name, o.commits, o.key, o.rec, real)

    def _fixer(self, o: Orphan, rw: str, real: str) -> None:
        if self.fixer_sent or len(orphan_fixers_alive()) >= knob_orphan_fixers():
            self.did.append(f"orphan {o.name}: a fixer waits for a free slot ({real[:80]})")
            return
        _ok, line = orphan_dispatch(self.repo, o.wt, o.name, real)
        o.rec["actions"].append({"at": ts(), "what": "fixer", "msg": line})
        self.fixers[rw] = {"at": ts(), "key": o.key, "msg": line, "verdict": real[:300]}
        self.fixers_changed = self.fixer_sent = True
        self.did.append(f"orphan {o.name}: fixer dispatched ({real[:120]}): {line}")
