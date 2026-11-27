# agentctl, part `redrecord`: a red's record: its lease's name and liveness, the names it answers
# to and what it is, its keys; and THE RUNS OF A RED'S EPISODE -- the one source of which runs a `Fixes-Red:` may name
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import re
from typing import Any
from dataclasses import dataclass
from agentctl.repos import RED_PRIMARY, red_repo_of, red_repo_short
from agentctl.workflows import LOCAL_FLOW
from agentctl.statemachines import machine_state, open_items, is_terminal
from agentctl.classify import norm_line
from agentctl.redrepos import red_is_other, red_repo_key
from agentctl.statefile import read_record
from agentctl.redknobs import knob_streak_rereads
from agentctl.core import SHORT, err, process_cache, reds_dir
from agentctl.forge import RED_CONCLUSIONS
from agentctl.cistate import ci_state_load
from agentctl.ciwhy import streak_runs
from agentctl.ciruns import Run
from agentctl.runsigs import classifier_version, run_names, run_sigs
from agentctl.repos import red_job_names



def red_lease(key: str) -> str:
    return f"red/{key}"


def lease_is_live(row: dict[str, Any] | None, t: float) -> bool:
    """THE LEASE LIVENESS: a holder, and an expiry after t (the caller's clock: the store's, or the tick's)."""
    return bool(row and row.get("holder") and row["expires_at"] > t)


def red_names(d: dict[str, Any]) -> set[Any]:
    return set(d.get("runs") or []) | {d.get("key")} | ({"local"} if d.get("workflow") == LOCAL_FLOW else set())


def red_what(d: dict[str, Any]) -> str:
    jobs = red_job_names(d)
    o_r = red_repo_of(d)
    return ("" if o_r == RED_PRIMARY else f"{red_repo_short(o_r)} ") + f"{d.get('workflow')} #{(d.get('last_run') or {}).get('number')}" + (f" [{', '.join(jobs)}]" if jobs else "")


def red_active(store: Any) -> dict[str, dict[str, Any]]:
    """{key: item} of the non-terminal reds (one per key)."""
    out = {}
    for it in open_items(store, "red"):
        out[it["data"].get("key") or it["id"]] = it
    return out


RED_RECURRING = 3            # landed fixes the same failure outlived: `recurring`, and its fixer hunts the root cause


def red_of_key(store: Any, key: str) -> Any:
    """The red item of key now: its open spell, else its newest closed one. A store that does not answer raises
    (review 2026-11-26: it read as "no such red", and a wait on that red was ended as if the red were gone)."""
    items = store.items("red")
    for it in items:
        if it["data"].get("key") == key and not is_terminal("red", it["state"]):
            return it
    return red_closed_of(store, key, items)


def red_closed_of(store: Any, key: str, items: list[dict[str, Any]] | None = None) -> Any:
    """The newest closed (terminal) red item of a red key or id -- `key`, `key.2`, ... -- or None. The spells are read
    from ONE listing (items, when the caller has it), never one store read each (review 2026-11-26, B 19)."""
    base = re.sub(r"\.\d+$", "", key)
    spells = {it["id"]: it for it in (store.items("red") if items is None else items)}
    last, n = None, 1
    while n < 200:
        it = spells.get(base if n == 1 else f"{base}.{n}")
        if it is None:
            break
        last, n = it, n + 1
    if last is None or not machine_state("red", last["state"]).terminal:
        return None
    return last


def canon_job(job):
    """A job's name as red keys have always spelled it: ci.yml's page tests were `page tests (<engine> / <variant>)` until
    2026-11-01 and are now `page-tests-<engine> (<variant>)`; the new name keys as the old, so an open red keeps its key."""
    import re
    m = re.fullmatch(r"((?:\S+ / )*)page-tests-(safari|chrome) \((.+)\)", job or "")
    return f"{m.group(1)}page tests ({m.group(2)} / {m.group(3)})" if m else job


def red_key(workflow, job, line):
    """A red's identity: its workflow and normalized first error line (the failing test or step); the job only when no
    line was read. Never the job name alone: matrix jobs failing the same way are one red."""
    import hashlib
    n = norm_line(line)
    return hashlib.sha1((f"{workflow}\0{n}" if n else f"{workflow}\0job {canon_job(job)}").encode()).hexdigest()[:12]


def red_active_of(store, o_r):
    """red_active, only o_r's reds: another watched repo's own, else the primary's (a red with no `repo`)."""
    mine = o_r if red_is_other(o_r) else RED_PRIMARY
    return {k: it for k, it in red_active(store).items() if red_repo_of(it["data"]) == mine}


def red_obs_for(o_r, obs):
    """Observations keyed for the store: another repo's keys bound to it (red_repo_key), the primary's as they are."""
    if not red_is_other(o_r):
        return obs
    out = {}
    for key, ob in obs.items():
        k = red_repo_key(o_r, key)
        out[k] = dict(ob, key=k)
    return out


def red_item_of_run(store, n, workflow=None):
    """The non-terminal reds a run number or id names (newest spell first)."""
    n = str(n).lstrip("#")
    return [it for it in red_active(store).values() if n in set(it["data"].get("runs") or [])
            and (workflow is None or it["data"].get("workflow") == workflow)]


# AN EPISODE IS THE RUNS FAILED SINCE THE LAST GREEN, FAILING THE SAME WAY (movies' CLAUDE.md: `Fixes-Red:` names "any red
# run of the current episode"; 2026-11-25, batch #124: the red item's own names had lost a run the fix named, and the fix
# waited for the green only it could bring).
# ONE SOURCE (2026-11-26). Two copies decided it: the gate's (redseen.episode_names, through red_units) read the newest
# red run's signatures and each streak run's -- cached, read again within the tick's budget, else the stale record -- and
# on an error said one line and returned NOTHING, so only the newest run counted and nobody saw why a fix waited; the
# queue facts' (red_episode_names) matched the red item's job and line against signatures cached under THIS classifier
# only. Characterised (tests/test_red_episode.py), they disagreed where a streak run's record was written by another
# classifier: the gate counted a stale record's match, the facts did not. Resolved: one fallback order for every run's
# signatures (below), the gate's; a stale record that names the same job and line is the same failure.
#
# THE FALLBACK ORDER, for a run's signatures: (1) the record cached under this classifier; (2) read again (run_sigs:
# the forge) while the tick's budget lasts; (3) the stale record another classifier wrote; (4) none -- that run is not
# counted. For the episode's own reference -- the newest red run's failing jobs (the gate) or the red item's job and
# line (the queue facts) -- an unreadable reference is SURFACED: Episode.unread says why, and `episode_unread` keeps it
# per workflow, which the queue facts carry into a waiting fix's words (redland.red_wait), until a read succeeds.
Sig = dict[str, Any]


episode_unread: dict[str, str] = process_cache({})  # workflow -> why its episode's reference could not be read


@dataclass(frozen=True)
class Episode:
    names: frozenset[str]  # the reference run's (when given) and every streak run's that failed the same way
    unread: str | None = None  # why the reference could not be read: only the reference run counts


def streak_budget() -> list[int]:
    """The tick's re-reads of stale streak signatures (a one-element list, spent across a pass)."""
    return [knob_streak_rereads()]


class RedEpisode:
    """One workflow's red streak on main (its runs, newest first) and the signatures read of them, once each."""

    def __init__(self, o_r: str, workflow: str, runs: list[Run] | None, budget: list[int] | None = None) -> None:
        self.o_r, self.workflow = o_r, workflow
        self.streak = streak_runs(runs) if runs else []
        self.budget = budget if budget is not None else [0]  # no forge read unless the caller grants some
        self.sigs: dict[Any, list[Sig]] = {}

    def run_sigs_of(self, r: Run) -> list[Sig]:
        """A streak run's signatures, by THE FALLBACK ORDER (above)."""
        rid = r.get("databaseId")
        if rid in self.sigs:
            return self.sigs[rid]
        rec = read_record(reds_dir("runsig", f"{rid}.json"))
        current = rec is not None and rec.get("classifier") == classifier_version()
        fresh = None if current else self._read_again(r) if self._may_read(r) else None
        self.sigs[rid] = fresh if fresh is not None else (rec or {}).get("sigs") or []
        return self.sigs[rid]

    def _may_read(self, r: Run) -> bool:
        if self.budget[0] <= 0 or r.get("status") != "completed" or r.get("conclusion") not in RED_CONCLUSIONS:
            return False
        self.budget[0] -= 1
        return True

    def _read_again(self, r: Run) -> list[Sig] | None:
        try:
            return run_sigs(self.o_r, r, self.workflow)
        except (Exception, SystemExit) as e:  # SystemExit: NoToken
            err(f"red: could not read the failed jobs of run {r.get('databaseId')} ({str(e)[:120]})")
            return None

    def failing(self, job: str, line: str, reference: Run | None = None) -> set[str]:
        """The names of `reference` and of every streak run (but it) whose signatures have job with this line."""
        names = set(run_names(reference)) if reference else set()
        want = norm_line(line)
        for r in self.streak:
            if reference and r.get("databaseId") == reference.get("databaseId"):
                continue
            if any(x.get("job") == job and norm_line(x.get("line")) == want for x in self.run_sigs_of(r)):
                names |= run_names(r)
        return names

    def reference_sigs(self, decided: Run) -> tuple[list[Sig], str | None]:
        """(the newest red run's signatures, why they could not be read): run_sigs (the cache, else the forge), else its
        stale record; neither -- ([], why), the why kept in `episode_unread` until a read succeeds."""
        try:
            sigs = run_sigs(self.o_r, decided, self.workflow)
        except (Exception, SystemExit) as e:  # SystemExit: NoToken
            sigs = (read_record(reds_dir("runsig", f"{decided.get('databaseId')}.json")) or {}).get("sigs") or []
            if not sigs:
                why = f"{self.workflow} #{decided.get('number')}'s failures could not be read ({str(e)[:120]})"
                err(f"red: {why}: only that run counts as its red")
                episode_unread[self.workflow] = why
                return [], why
        episode_unread.pop(self.workflow, None)
        return sigs, None

    def of_run(self, decided: Run) -> Episode:
        """The gate's: every streak run failing as the newest red run `decided` does (any of its failing jobs); its
        signatures unread (reference_sigs), only it counts and Episode.unread says why."""
        sigs, unread = self.reference_sigs(decided)
        names = set(run_names(decided))
        for s in [s for s in sigs if s.get("kind") != "incomplete"]:
            names |= self.failing(s["job"], s["line"], decided)
        return Episode(frozenset(names), unread)

    def of_item(self, d: dict[str, Any]) -> Episode:
        """The queue facts': every streak run whose signature has red item d's failing job and line."""
        return Episode(frozenset(self.failing(d["job"], d.get("line") or "")))


def red_episode_names(d: dict[str, Any], snap: Any = None) -> set[str]:
    """The names (numbers and ids) of the gate runs, in the snapshot's red streak of red data d's workflow, failing as
    d does (RedEpisode.of_item, no forge read); empty for a red outside the gate or with no snapshot row."""
    snap = ci_state_load(d.get("repo") or SHORT["movies"]) if snap is None else snap
    row = gate_row(d, snap)
    if row is None:
        return set()
    return set(RedEpisode(d.get("repo") or SHORT["movies"], d["workflow"], row.get("runs")).of_item(d).names)


def gate_row(d: dict[str, Any], snap: Any) -> dict[str, Any] | None:
    """The snapshot's gate row of red data d's workflow, when d is a gate red with a failing job."""
    row = next((g for g in (snap or {}).get("gate") or [] if g.get("name") == d.get("workflow")), None)
    return row if d.get("gate") and d.get("job") else None
