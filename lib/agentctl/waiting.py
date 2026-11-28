# agentctl, part `waiting`: the waiting changes' rules: who waits for a red main, applying an entry to a clone, superseding, cancelling, the batch order
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import os
import signal
from typing import Any
from agentctl.statefile import note_failure, read_record
from agentctl.statemachines import StaleVersion, change_event, machine_state, open_items
from agentctl.timefmt import ts
from agentctl.core import SHORT, print_error, git, remote
from agentctl.repos import server_repo, slug
from agentctl.trailers import commit_trailer_values
from agentctl.waitingdir import ChangeRecords, entry_final, waiting_state, wlog
from agentctl.owned import cancel_run
from agentctl.context import Context
from agentctl.conflicts import pick
from agentctl.queueslots import entry_owned, queue_verdict, set_change
from agentctl.queuerecheck import same_change_source
from agentctl.redland import may_land_on_red
from agentctl.changekind import facts_for
from agentctl.gitrepo import is_ancestor


WAITING_HELP = """The processor (one loop of ticks, launchd net.bitcashier.agentctl-waiting) and its queue: every landed change is
an item -- queued, checking, checked, pushing, landed; or waiting while main is red. At green the waiting changes are ONE
batch item: its tip assembled in a check tree (main plus the changes, Fixes-Red first), checked once (pre-push
--fast-only, a fresh sbt server; a stall retried once, then HELD), pushed in one push; a failing batch is bisected and
only the confirmed culprit rejected. No `waiting` branch. Every verdict goes to `agentctl inbox`.

  agentctl land status                          # the changes, the batch, the gate, the processor (hands it the token)
  agentctl land cancel <id>                     # drop a change
  agentctl land hold|release                    # stop / restart taking batches (changes still queue)
  agentctl tick [--once]                        # the processor
  agentctl tick service install|uninstall|status   # it as the launchd agent net.bitcashier.agentctl-waiting
(`waiting run|check|check-batch|await` are internal: the processor's own re-exec and its children; the laptop's launchd
job still starts `waiting run --service` until `agentctl tick service install` is run again.)"""


GATE_UNREAD = "unread"  # gate_reds' member when the gate could not be read: never green, and no Fixes-Red matches it


class UnreadGate(set[str]):
    """gate_reds' answer when the gate could not be read: {GATE_UNREAD} carrying `why` -- the reason travels with the
    answer, never in module state a second caller could overwrite."""

    def __init__(self, why: str) -> None:
        super().__init__({GATE_UNREAD})
        self.why = why


def unread_why(reds: set[str]) -> str:
    """Why the gate was unread, from the answer itself ('' for a read gate, or a set built without a reason)."""
    return reds.why if isinstance(reds, UnreadGate) else ""


def waiting_commits(repo, base, head):
    return git(repo, "rev-list", "--reverse", f"{base}..{head}").split()


# A FIX OF AN OPEN GATE RED NEVER WAITS (2026-11-25, batch #124): the classifier (changekind.py) called a change naming
# #2896 `red fix of 2896 (gate)` -- the red item's names -- while this rule read only the gate's newest runs (#2897,
# #2898) and kept the fix on `waiting` beside a non-fix change: a batch assembles only what may land on red, so the
# fix waited for the green only it could bring (watchdog WR).
def must_wait(repo: str, commits: list[str], reds: set[str], store: Any, *, ctx: Context) -> bool:
    """A production commit with no Fixes-Red: naming a red run waits; tools-only and declared fixes go (the hook's rule).
    A red run is one of the gate's (`reds`) or any name of an open gate red in the store."""
    return must_wait_why(repo, commits, reds, store, ctx=ctx) is not None


def must_wait_why(repo: str, commits: list[str], reds: set[str], store: Any, *, ctx: Context) -> str | None:
    """None when the commits may land on the gate's red (`reds`; or it is green), else why the first that may not
    waits, in words: THE RED-LANDING RULE (redland.may_land_on_red)."""
    return may_land_on_red(facts_for(reds, store, ctx.caches.tick_facts), repo=repo, commits=commits, store=store, ctx=ctx).wait


def record_autoresolved(d: Any, e: dict[str, Any], resolved: Any, conflict: Any) -> None:
    """A SOURCE CONFLICT IS RESOLVED IN THE BATCH: what it took, kept on the entry for the author and bisect."""
    if resolved and not conflict:
        e["autoresolved"] = resolved
        wlog(d, f"{e['id']}: auto-resolved in the batch, its full check the verification: "
                + "; ".join(f"{m['file']} ({', '.join(m['hunks'])})" for m in resolved))
    else:
        e.pop("autoresolved", None)


def record_regenerated(e: dict[str, Any], regenerated: Any) -> None:
    """Resolved by regeneration in this build (GENERATED FILES ARE NO OVERLAP): a failure is not its own."""
    if regenerated:
        e["regenerated"] = sorted(regenerated)
    else:
        e.pop("regenerated", None)


def record_provisional(d: Any, e: dict[str, Any], provisional: Any, conflict: Any) -> None:
    """A row both sides changed on a host with no regen command: the tree's row kept, for the checks' ratchet to judge."""
    if provisional and not conflict:
        e["provisional"] = sorted(set(provisional))
        wlog(d, f"{e['id']}: {', '.join(e['provisional'])}: a row both sides changed and this host runs no regen "
                "command: kept the tree's row (main's), for the checks' ratchet to judge over the combined tree")
    else:
        e.pop("provisional", None)


def dropped_on_main(wt: Any, dropped: Any, base: Any, first: bool) -> Any:
    """The dropped (commit, via) pairs that main (base's history) carries: via is an ancestor of base, or the commit was
    an empty pick of the entry's first commit."""
    return [(c, v) for c, v in dropped
            if (v and is_ancestor(wt, v, base)) or (not v and first)]


def drop_landed(st: Any, d: Any, e: dict[str, Any], on_main: Any) -> Any:
    """The entry without its commits main carries (`dropped` keeps them); ('landed', via) when nothing is left of it."""
    gone = {c for c, _v in on_main}
    e["commits"] = [c for c in e["commits"] if c not in gone]
    e.setdefault("dropped", []).extend([[c, v] for c, v in on_main])
    ChangeRecords(st, d).save(e)
    wlog(d, f"{e['id']}: {len(on_main)} commit(s) already on {remote()}/main, dropped: "
            + ", ".join(f"{c[:10]} (via {v[:10] if v else 'an empty pick'})" for c, v in on_main))
    if not e["commits"]:
        return "landed", next((v for _c, v in reversed(on_main) if v), None)
    return None


def apply_entry(st, d, wt, e, base, regen: bool = True, autoresolve: bool = False):
    """Pick the entry's commits onto wt's HEAD (base, or base plus earlier entries): ('ok', None), ('conflict', files),
    ('landed', via) or ('duplicate', via). A commit already on MAIN (base's history) is dropped from the entry for good
    (a partly landed entry keeps only the rest); one only an earlier entry carries is left out of this build alone (that
    entry may still be rejected). Nothing left of it on main: 'landed' -- the caller says LANDED, never pushes empty."""
    first = git(wt, "rev-parse", "HEAD").strip() == base
    dropped, regenerated, provisional, resolved = [], [], [], [] if autoresolve else None
    conflict = pick(wt, e["commits"], dropped, regen, regenerated, provisional, resolved)
    record_autoresolved(d, e, resolved, conflict)
    record_regenerated(e, regenerated)
    record_provisional(d, e, provisional, conflict)
    if conflict:
        return "conflict", conflict
    if not dropped:
        return "ok", None
    return _after_drops(st, d, wt, e, base, first, dropped)


def _after_drops(st, d, wt, e, base, first, dropped):
    """apply_entry's verdict when some commits were dropped: 'landed' (nothing left of it on main), 'duplicate' (all of
    it on an earlier entry), else 'ok' with the rest."""
    on_main = dropped_on_main(wt, dropped, base, first)
    landed = drop_landed(st, d, e, on_main) if on_main else None
    if landed:
        return landed
    if not [c for c in e["commits"] if c not in {x for x, _v in dropped}]:  # all of it is on an earlier entry
        return "duplicate", next((v for _c, v in reversed(dropped) if v), None)
    return "ok", None


def already_landed(st, d, e, via) -> None:
    """Every commit of the entry is on main already (an earlier entry, or someone, landed it): LANDED, nothing pushed."""
    queue_verdict(st, d, e, "landed", f"LANDED {e['head']} (already on main via {via or 'an equivalent commit'}): "
                                  "nothing left to push", code=0, landed_sha=via, already=True)


def cancel_entry_remote(st: Any, e: dict[str, Any], why: str) -> None:
    """An entry that ends (superseded, cancelled) takes its undecided remote run with it: the run is the entry's."""
    rem = e.get("remote") or {}
    if rem.get("run") is not None and not rem.get("status"):
        try:
            cancel_run(slug(e["worktree"]) if os.path.isdir(e.get("worktree") or "") else SHORT["movies"], rem["run"], why)
        except (Exception, SystemExit) as ex:
            print_error(f"could not cancel run {rem['run']} of {e['id']}: {str(ex)[:120]}")


def stop_checker(e: dict[str, Any]) -> None:
    """An entry's running checker stopped (its CI run is cancelled with it): never this process itself, and a checker
    that ended since its lock was read is no error (review 2026-11-26: `land cancel` had its own copy with neither)."""
    h = read_record(entry_owned(e["id"]).record) or {}
    if entry_owned(e["id"]).live() and h.get("pid") and int(h["pid"]) != os.getpid():
        try:
            os.kill(int(h["pid"]), signal.SIGTERM)
        except ProcessLookupError:  # it ended on its own meanwhile
            pass
        except OSError as exc:
            note_failure("checker not signalled", exc)


def supersede(st, d, e, by, why) -> None:
    """The entry gives way to `by` (a newer entry of the same worktree and branch, or one carrying the same change): its
    checker, if running, is stopped; said in the inbox."""
    line = f"SUPERSEDED {e['id']} by {by}: {why}"
    set_change(st, d, e, "superseded", cause="superseded", event=change_event(e, line, code=0), superseded_by=by, why=why, decided=ts())
    cancel_entry_remote(st, e, f"its queue entry was superseded by {by}")
    stop_checker(e)
    wlog(d, line)


def tail_of(out, n: int = 25):
    return "\n  ".join([l for l in out.splitlines() if l.strip()][-n:])


def diagnoses_of(repo, commits):
    """The run ids/numbers the commits' `Diagnoses-Red:` trailers name (DIAGNOSTICS ARE NO FIX, redmove.py)."""
    out = set()
    for c in commits:
        out |= commit_trailer_values(repo, c, "Diagnoses-Red")
    return out


def batch_order(repo, entries, reds, store, *, ctx):
    """The order a batch is built in: a fix for the current red first, then (main red) what may land while red -- tools
    only, or every commit carrying a Fixes-Red for a red run -- then the rest; by join order within each."""
    facts = facts_for(reds, store, ctx.caches.tick_facts)

    def rank(e):
        landing = may_land_on_red(facts, e, repo, store=store, ctx=ctx)  # THE RED-LANDING RULE (redland.py)
        return 2 if not reds or landing.wait else 0 if landing.gate_fix else 1
    return sorted(entries, key=lambda e: (rank(e), e["seq"]))


def supersede_older_waiting(st, d, worktree, branch, eid) -> None:
    """Waiting again replaces this worktree and branch's older waiting entry: only the newest (eid) lands."""
    for old in ChangeRecords(st, d).waiting():
        if old["id"] != eid and same_change_source(old, worktree, branch):
            supersede(st, d, old, eid, f"{worktree} waits again; only the newest entry lands")


def note_waiting(st, d, log_path, head, eid):
    """The change's `=== waiting` line in its log, and its verdict line `WAITING <head> (entry <id>, n waiting)` (n:
    the waiting entries, it among them, saved yet or not)."""
    n = len({x["id"] for x in ChangeRecords(st, d).waiting()} | {eid})
    with open(log_path, "a") as f:
        f.write(f"=== waiting {ts()} {head} as {eid} ({n} waiting)\n")
    return f"WAITING {head} (entry {eid}, {n} waiting)"


def cancel_submitted(st: Any, eid: str, how: str = "cancel", repo: str | None = None) -> dict[str, Any]:
    d = waiting_state(repo or server_repo())[0]
    hits = [e for e in [ChangeRecords(st, d).get(eid)] if e and not entry_final(e.get("status"))]
    if not hits:  # an item no entry carries (stranded): the item alone
        it = st.item("change", eid)
        if not it or machine_state("change", it["state"]).terminal:
            raise KeyError(eid)
        st.transition("change", eid, "cancelled", it["version"], data={"why": f"{how}led through the store API"},
                      event={"cause": "agent", "text": f"{eid}: {how}led", "refs": {"entry": eid}})
        clear_holds_on(st, eid)
        return {"id": eid, "status": "cancelled"}
    e = hits[0]
    set_change(st, d, e, "cancelled", cause="agent", event={"text": f"{e['id']}: {how}led"}, why=f"{how}led through the store API",
               decided=ts())
    cancel_entry_remote(st, e, f"its queue entry was {how}led")
    clear_holds_on(st, eid)
    return {"id": eid, "status": "cancelled"}


def clear_holds_on(st, eid) -> None:
    """Every open change's recorded hold naming change eid as its blocker, cleared (eid is gone: the next tick reads
    each one's hold again); never a phantom blocker in `land status`."""
    for it in open_items(st, "change"):
        if ((it.get("data") or {}).get("hold") or {}).get("blocker") != eid:
            continue
        try:
            st.update_item("change", it["id"], it["version"], {"hold": None})
        except StaleVersion:
            pass  # moved meanwhile: the next tick records its hold
