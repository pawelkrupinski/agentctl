# agentctl, part `cireport`: the CI report: ci_why's explanation of main's runs and who holds each red, and the CI-state snapshot's refresh
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from agentctl.clock import epoch_now
from typing import Any, Optional
from agentctl.core import err, remote
from agentctl.forge import RED_CONCLUSIONS, is_cancelled
from agentctl.repos import gated_repo, scan_repo_for
from agentctl.workflows import LOCAL_FLOW
from agentctl.redretired import red_run_retired
from agentctl.gating import GATING_CHECKS_FILE
from agentctl.cistate import ci_state_load, ci_state_write, state_read
from agentctl.ciwhy import NO_VERDICT_FLOWS, gate_from_snap, gate_snap_rows, gate_report_only, gate_runs, outside_from_snap, run_no_verdict, run_target
from agentctl.timefmt import age_since
from agentctl.ciruns import main_runs_of
from agentctl.runsigs import run_sigs, save_run_sigs
from agentctl.gatesummary import SUMMARY_MAX_REDS
from agentctl.fixerblockers import short_flow
from agentctl.redrecord import red_active, red_what
from agentctl.branches import branch_lines, branch_state, branch_summary
from agentctl.redseen import gate_red_units, local_red, local_stale, local_units, predict_units
from agentctl.stuckruns import stuck_runs
from agentctl.reduntracked import red_untracked_runs
from agentctl.redcrash import CRASH_LOOP_CAUSE, DECISION_CAUSE
from agentctl.ask import trim_words
from agentctl.redview import red_lines, red_view, red_view_of_units
from agentctl.runexplain import explain_run, explain_run_jobs
from agentctl.outsideflows import main_callable_flows, outside_gate


Row = tuple[Any, ...]  # a gate row (name, decided, newer, (since, count)), an outside one (name, run, since, count)


def ci_why(o_r: str, tail: int = 6, run_id: Any = None, repo: Optional[str] = None, summary: bool = False,
           snap: Optional[dict[str, Any]] = None, *, store: Any) -> tuple[bool, str]:
    """(red, text). run_id: explain that one run; else the gating workflows' newest decided runs on main, each red row
    followed by who holds it (`fixing: <its state, holder or offer>`; never NOBODY). summary: text is the status line's one line."""
    if run_id:
        return True, run_report(o_r, run_id, tail, repo)
    r = GateReport(o_r, tail, repo, summary, snap, store)
    r.gate_rows()
    r.local_rows()
    r.flake_rows()
    head = f"ci-why {o_r}: " + ("main is RED for the gate" if r.red else "main is green for the gate")
    r.outside_rows()
    r.branch_rows()
    return r.red, (r.summary_line() if summary else "\n".join([head] + r.out))


def run_report(o_r: str, run_id: Any, tail: int, repo: Optional[str]) -> str:
    """One run explained (`agentctl ci-why <run>`); a run GitHub does not know says so."""
    rid, said = run_target(o_r, run_id)
    try:
        lines, _ = explain_run(o_r, {"databaseId": int(rid)}, tail, repo)
    except RuntimeError as e:
        if "HTTP 404" not in str(e):
            raise
        lines = [f"  no such run: {str(e)[:200]}"]
    return f"run {o_r}#{rid}\n" + "".join(f"  {l}\n" for l in said) + "\n".join(lines)


def outside_runs(o_r: str, snap: Optional[dict[str, Any]]) -> tuple[list[Row], Any]:
    """(the reds outside the gate, why they could not be listed or None): from the snapshot when given."""
    if snap:
        return outside_from_snap(snap), snap.get("outside_error")
    if not gated_repo(o_r):
        return [], None
    try:
        return outside_gate(o_r), None
    except Exception as e:  # the gate's verdict stands without them
        return [], e


def outage_cause(st: Optional[dict[str, Any]]) -> Optional[str]:
    """The outage a red is blocked on, None for any other state. A decision or the fixers' crash loop is no outage: its
    red reads WAITING ON YOU: <question>."""
    cause = (st or {}).get("cause") if (st or {}).get("state") == "blocked-external" else None
    return cause if cause and cause not in (DECISION_CAUSE, CRASH_LOOP_CAUSE) else None


def outage_line(cause: str, until: Any, names: list[str], ask: tuple[Any, Any]) -> str:
    """ONE line per outage, naming the reds it blocks."""
    return (f"{cause} down \u2192 blocks: {', '.join(names)}" + (f" (auto-clears when {until})" if until else "")
            + (f" -- WAITING ON YOU: {trim_words(ask[0])} -- {ask[1]}" if ask and ask[0] else ""))


def capped(reds: list[str]) -> list[str]:
    """The first SUMMARY_MAX_REDS, and how many more."""
    if len(reds) > SUMMARY_MAX_REDS:
        return reds[:SUMMARY_MAX_REDS] + [f"+{len(reds) - SUMMARY_MAX_REDS} more"]
    return reds


def short(st: Optional[dict[str, Any]]) -> str:
    return st["short"] if st else "?"


class GateReport:
    """ci-why's reading of main: the gate's newest decided runs, the local checks, the flakes, the reds outside the gate
    and the branch reds, each red with who holds it (its item in the store: the tick acts, ci-why only renders). out:
    the report's lines; parts: each gate row's word for the one-line summary."""

    def __init__(self, o_r: str, tail: int, repo: Optional[str], summary: bool, snap: Optional[dict[str, Any]],
                 store: Any) -> None:
        self.o_r, self.tail, self.repo, self.summary, self.store = o_r, tail, repo, summary, store
        self.out: list[str] = []
        self.parts: list[str] = []
        self.red = False
        self.gate = gate_from_snap(snap) if snap else gate_runs(o_r)
        others, self.others_err = outside_runs(o_r, snap)
        self.lrepo = scan_repo_for(o_r, repo)
        self.lstale = local_stale(self.lrepo)
        self.bsnap = snap or ci_state_load(o_r)  # the branch reds come from the snapshot only (the processor writes it)
        self.explained = self._explain_reds()
        self.states: dict[Any, Any] = {}
        self.units_of: dict[Any, list[Any]] = {}
        self.flakes: list[Any] = []
        self.untracked: dict[Any, Any] = {}
        self._read_holders(others)
        self.proven, self.others, self.never = self._sort_outside(others)

    def _explain_reds(self) -> dict[str, tuple[list[str], Any]]:
        """Each red gate run's explanation, its signatures cached (run_sigs)."""
        explained = {}
        for name, decided, _newer, _streak in self.gate:
            if not (decided and decided.get("conclusion") in RED_CONCLUSIONS):
                continue
            if self.summary:  # THE ONE-LINE SUMMARY NEVER RE-READS A DECIDED RUN'S LOGS (2026-10-29: ci-runner's gate summary
                # read convergence #2473's failed log and five job logs, ~8 s each, every 45 s -- "gate summary: still
                # running after 20 s" at every tick). Its line shows no log line; the run's signatures are cached once
                # per run and classifier (run_sigs), which is all the red units below read.
                run_sigs(self.o_r, decided, name)
                continue
            lines, num, per_job = explain_run_jobs(self.o_r, decided, self.tail, self.repo)
            explained[name] = (lines, num)
            if not any("LOG UNREADABLE" in l for l in lines):
                save_run_sigs(self.o_r, decided, name, per_job)
        return explained

    def _read_holders(self, others: list[Row]) -> None:
        try:  # who holds each red: its item in the store (the tick acts; ci-why only renders)
            mine = main_runs_of(self.o_r)
            rows = [(n, d, mine.gate.get(n, []), not gate_report_only(self.o_r, n, d)[0]) for n, d, _nw, _s in self.gate]
            units = gate_red_units(self.o_r, rows, [(n, r, mine.outside.get(n, []), False) for n, r, _s, _c in others],
                                   rereads=0 if self.summary else None)
            units += local_units(self.lrepo) + predict_units(self.o_r, self.bsnap)
            for u in units:
                self.units_of.setdefault((u["workflow"], u["run"].get("databaseId")), []).append(u)
            self.states = red_view_of_units(self.store, units)
            self.flakes = [it for it in red_active(self.store).values() if it["data"].get("red_kind") == "flake"]
            self.untracked = red_untracked_runs(self.store, self.o_r, units)
        except (Exception, SystemExit) as e:  # who is fixing is information: the gate's verdict stands without it
            err(f"red: could not tell who is fixing ({str(e)[:160]})")

    def state_of(self, name: str, run: dict[str, Any]) -> Any:
        return self.states.get((name, run.get("databaseId")))

    def holder_lines(self, name: str, run: dict[str, Any], indent: str = "  ") -> list[str]:
        return list(red_lines(self.state_of(name, run), indent))

    def _sort_outside(self, others: list[Row]) -> tuple[list[Row], list[Row], list[Row]]:
        """(proven fixed, red, never green on main) of the runs outside the gate."""
        proven = [o for o in others if self._proven_fixed(o)]
        reds = [o for o in others if o not in proven and not red_run_retired(self.o_r, o[0], o[1])]
        never = [o for o in reds if (o[0], o[1].get("databaseId")) in self.untracked]  # never green on main: untracked
        return proven, [o for o in reds if o not in never], never

    def _proven_fixed(self, o: Row) -> bool:
        """A RUN ITS CALLERS PROVED FIXED is no red, though it stays its callable-only workflow's newest run on main for
        good."""
        return bool((self.state_of(o[0], o[1]) or {}).get("state") == "verified")

    def gate_rows(self) -> None:
        for name, decided, newer, (since, count) in self.gate:
            nov = main_runs_of(self.o_r).no_verdict.get(name)
            if nov and not self.summary:
                self.out.append(f"{name}: newest run {nov['databaseId']} #{nov.get('number')}: NO VERDICT (incomplete: "
                                "only `verdict complete` failed -- a takes row skipped, cancelled or missing; no red, no "
                                "fixer); the gate reads the newest complete run, and the next complete run decides")
            self._gate_row(name, decided, newer, since, count)

    def _gate_row(self, name: str, decided: Any, newer: Any, since: Any, count: Any) -> None:
        if not decided:
            self.out.append(f"{name}: no decided run on main")
            self.parts.append(f"{short_flow(name)} ?")
            return
        run = f"run {decided['databaseId']} #{decided.get('number')}"
        if decided["conclusion"] == "success":
            self.out.append(f"{name}: GREEN ({run})")
            self.parts.append(f"{short_flow(name)} green")
            return
        retired = red_run_retired(self.o_r, name, decided)
        if retired:  # its check is gone from main: no run can clear it, it gates nothing and no fixer is offered it
            self.out.append(f"{name}: RETIRED ({run}, {decided['conclusion']}): {retired} -- no red")
            self.parts.append(f"{short_flow(name)} retired")
            return
        report, why = gate_report_only(self.o_r, name, decided)
        if report:  # red only in checks the registry calls unproven: it reports, it holds no push
            self.out.append(f"{name}: RED, REPORT ONLY ({run}, {decided['conclusion']}): {why} ({GATING_CHECKS_FILE}) "
                            "-- holds no push")
            self.out += self.holder_lines(name, decided)
            self.parts.append(f"{short_flow(name)} report-only")
            return
        self._red_row(name, decided, newer, since, count)

    def _red_row(self, name: str, decided: Any, newer: Any, since: Any, count: Any) -> None:
        self.red = True
        pending = f"; newer run {newer['databaseId']} (#{newer.get('number')}) {newer.get('status')} may clear it" if newer else ""
        streak = f"; red for {age_since(since)} ({count} failed run{'s' if count != 1 else ''})" if count else ""
        self.out.append(f"{name}: RED (run {decided['databaseId']} #{decided.get('number')}, {decided['conclusion']}"
                        f"{streak}){pending}")
        self.out += self.holder_lines(name, decided)
        if not self.summary:
            lines, num = self.explained.get(name) or explain_run(self.o_r, decided, self.tail, self.repo)
            self.out += lines
            self.out.append(f"  clears when: a later {name} run on main is green, or every pushed commit touching a "
                            f"production path carries `Fixes-Red: {num}` (or `Fixes-Red: {decided['databaseId']}`) or "
                            "names another run of this red episode failing the same way; tools-only commits land while red")
        self.parts.append(f"{short_flow(name)} #{decided.get('number')} \u2192 {short(self.state_of(name, decided))}")

    def local_rows(self) -> None:
        """The local checks (pre-push on plain main): stale, or red."""
        lus = local_units(self.lrepo)
        local = list(self.units_of.get((LOCAL_FLOW, (lus or [{"run": {}}])[0]["run"].get("databaseId")), []))
        if self.lstale:
            self.out.append(f"{LOCAL_FLOW}: stale, rechecking (recorded on {str(local_red().get('main'))[:10]}; "
                            f"{remote()}/main is {self.lstale[:10]} now)")
            self.parts.append("local \u2192 stale, rechecking")
        if not local:
            return
        main = str(local_red().get("main"))[:10]
        self.out.append(f"{LOCAL_FLOW}: RED on plain {remote()}/main {main} ({', '.join(u['job'] for u in local)}: "
                        "pre-push fails there without any change) -- claim: `agentctl red claim local`")
        self.out += self.holder_lines(LOCAL_FLOW, local[0]["run"])
        self.out += [f"  [{u['job']}] {u['line'][:160]}" for u in local]
        self.parts.append(f"local checks \u2192 {short(self.state_of(LOCAL_FLOW, local[0]['run']))}")

    def flake_rows(self) -> None:
        if not self.flakes:
            return
        self.out.append("Flaky (passed on the next run with no fix; a red until a fix lands and a run proves it):")
        for it in self.flakes:
            self.out.append(f"  {red_what(it['data'])}: {str(it['data'].get('line'))[:120]}")
            self.out.append(f"    fixing: {red_view(self.store, it)[0]}")

    def outside_rows(self) -> None:
        """The reds outside the gate, the never-green runs and the ones proven fixed."""
        if self.others_err is not None:
            self.out.append(f"outside the gate: could not list main's runs ({self.others_err})")
        if self.others:
            self.out.append("Outside the gate (red, not blocking a push; oldest first):")
        for name, r, since, count in self.others:
            self.out.append(f"  {name}: RED for {age_since(since)} since {since} ({count} failed run"
                            f"{'s' if count != 1 else ''}; newest run {r['databaseId']} #{r.get('number')}) -- "
                            f"`agentctl ci-why {r['databaseId']}`")
            self.out += self.holder_lines(name, r, "    ")
        if self.never:
            self.out.append("Untracked (never green on main: no red, no fixer, no alert until its first green run):")
        for name, r, _since, _count in self.never:
            self.out.append(f"  {name}: newest run {r['databaseId']} #{r.get('number')}: "
                            f"{self.untracked[(name, r.get('databaseId'))]}")
        if self.proven:
            self.out.append("Proven fixed outside the gate (no red; it runs only when called, so its newest run on main "
                            "stays red):")
        for name, r, _since, _count in self.proven:
            self.out.append(f"  {name}: newest run {r['databaseId']} #{r.get('number')} -- `agentctl ci-why {r['databaseId']}`")
            self.out += self.holder_lines(name, r, "    ")

    def branch_rows(self) -> None:
        """The branch reds (from the snapshot), each run's predicting unit with who holds it."""
        blines = branch_lines(self.bsnap)
        if not blines:
            return
        self.out += blines
        for units in self.units_of.values():
            x = next((x for x in units if x.get("predicts")), None)
            if x:
                self.out += self.holder_lines(x["workflow"], x["run"], "    ")

    def summary_line(self) -> str:
        local = [p_ for p_ in self.parts if p_.startswith("local")]
        head = "gate RED (" + "; ".join(p_ for p_ in self.parts if p_ not in local) + ")" if self.red else "gate green"
        return head + self._summary_tail(capped(self._outside_summary() + local), branch_summary(self.bsnap))

    def _summary_tail(self, reds: list[str], bsum: str) -> str:
        quiet = not reds and not bsum and not self.red and self.others_err is None
        return ((" \u00b7 reds: " + "; ".join(reds) if reds else "") + (" \u00b7 no reds" if quiet else "")
                + (" \u00b7 " + bsum if bsum else ""))

    def _outside_summary(self) -> list[str]:
        """The reds outside the gate, ONE line per outage naming the reds it blocks."""
        reds: list[str] = []
        causes: dict[str, tuple[Any, list[str], tuple[Any, Any]]] = {}
        for name, r, _since, _count in self.others:
            st = self.state_of(name, r)
            cause = outage_cause(st)
            if cause:
                causes.setdefault(cause, (st.get("until"), [], (st.get("ask"), st.get("ask_how"))))[1].append(name)
                continue
            reds.append(f"{name} #{r.get('number')} \u2192 {short(st)}")
        return [outage_line(c, u_, ns, q_) for c, (u_, ns, q_) in causes.items()] + reds


def ci_state_refresh(o_r: str, write: bool = True) -> dict[str, Any]:
    """Read the gate and every other workflow on main once, write the snapshot (unless write=False: ci_state_write
    later), return it. Raises when the gate cannot be read (nothing is written: the old snapshot ages out)."""
    gate = gate_runs(o_r)
    snap = {"at": epoch_now(), "repo": o_r, "outside_error": None, "outside": [],
            "gate": gate_snap_rows(o_r, gate)}
    if gated_repo(o_r):
        snap.update(outside_snap(o_r))
        snap["callable_only"] = sorted(main_callable_flows(o_r))  # their reds wait for red_callable_evidence
    snap.update(branches_snap(o_r))
    snap["latest"] = latest_decided(o_r)
    snap["stuck"] = stuck_runs({**main_runs_of(o_r).outside, **main_runs_of(o_r).gate}, o_r=o_r)  # audit #8: undecided far past their workflow's p90
    if write:
        ci_state_write(snap)
    return snap


def outside_snap(o_r: str) -> dict[str, Any]:
    try:
        return {"outside": [{"name": n, "decided": r, "since": since, "count": count, "runs": main_runs_of(o_r).outside.get(n, [])}
                            for n, r, since, count in outside_gate(o_r)]}
    except Exception as e:
        return {"outside_error": str(e)[:200]}


def branches_snap(o_r: str) -> dict[str, Any]:
    """The branch runs: measure/*, check/*, red fixers' and every recorded dispatch's (after the main lists)."""
    try:
        prev = (state_read().get("ci") or {}).get("branches") or []
        return {"branches": branch_state(o_r, scan_repo_for(o_r), prev)}
    except (Exception, SystemExit) as e:
        return {"branches_error": str(e)[:200]}


def latest_decided(o_r: str) -> dict[str, Any]:
    """Each workflow's newest decided run on main: what workflow_green probes read."""
    latest = {}
    for name, runs in list(main_runs_of(o_r).outside.items()) + list(main_runs_of(o_r).gate.items()):
        d = next((r for r in runs if decided_on_main(o_r, name, r)), None)
        if d:
            latest[name] = {k: d.get(k) for k in ("number", "databaseId", "conclusion", "headSha", "updatedAt")}
    return latest


def decided_on_main(o_r: str, name: str, r: dict[str, Any]) -> bool:
    return r.get("status") == "completed" and not is_cancelled(r) and not (name in NO_VERDICT_FLOWS and run_no_verdict(o_r, r))
