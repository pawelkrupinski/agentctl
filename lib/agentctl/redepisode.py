# agentctl, part `redepisode`: THE RUNS OF A RED'S EPISODE -- the one source of which runs a `Fixes-Red:` may name.
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from dataclasses import dataclass
from typing import Any
from agentctl.core import SHORT, env_num, err, process_cache, reds_dir
from agentctl.statefile import read_record
from agentctl.classify import norm_line
from agentctl.forge import RED_CONCLUSIONS
from agentctl.ciwhy import Run, ci_state_load, streak_runs
from agentctl.runsigs import classifier_version, run_names, run_sigs


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
STREAK_REREADS = 3  # stale or missing streak run signatures read again per tick ($AGENTCTL_STREAK_REREADS)
Sig = dict[str, Any]
episode_unread: dict[str, str] = process_cache({})  # workflow -> why its episode's reference could not be read


@dataclass(frozen=True)
class Episode:
    names: frozenset[str]  # the reference run's (when given) and every streak run's that failed the same way
    unread: str | None = None  # why the reference could not be read: only the reference run counts


def streak_budget() -> list[int]:
    """The tick's re-reads of stale streak signatures (a one-element list, spent across a pass)."""
    return [int(env_num("AGENTCTL_STREAK_REREADS", STREAK_REREADS))]


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
