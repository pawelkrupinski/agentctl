# agentctl, part `waiting`: waiting.
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import fnmatch
import os
import signal
from agentctl.core import SHORT, err, git, remote, slug
from agentctl.timefmt import ts
from agentctl.statefile import read_record
from agentctl.store import change_event
from agentctl.trailers import trailer_values
from agentctl.waitingdir import save_entry, waiting_entries, wlog
from agentctl.owned import cancel_run, lock_held
from agentctl.queueslots import entry_owned, queue_verdict, set_change
from agentctl.conflicts import pick
from agentctl.queuerecheck import same_change_source
from agentctl.redfixes import commit_fixes
from agentctl.changekind import classify_change, gate_facts


PRODUCTION_GLOBS = ("*/src/main/*", "conf/*", "*/conf/*", "build.sbt", "project/*", "infra/nix/*", "infra/kubernetes/*",
                    "Dockerfile")
WAITING_AWAIT_TIMEOUT = 24 * 3600
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
The old names (`waiting status|pause|resume|cancel|run|service`) answer until 2026-11-05, each naming its new one."""


def is_production_path(p):
    """movies' pre-push is_production_path: what reaches a pod or a fleet host (the rest lands while red)."""
    return any(fnmatch.fnmatch(p, g) for g in PRODUCTION_GLOBS)


GATE_UNREAD = "unread"  # gate_reds' member when the gate could not be read: never green, and no Fixes-Red matches it
gate_unread_why = [""]


def waiting_commits(repo, base, head):
    return git(repo, "rev-list", "--reverse", f"{base}..{head}").split()


def must_wait(repo, commits, reds):
    """A production commit with no Fixes-Red: naming a red run waits; tools-only and declared fixes go (the hook's rule)."""
    for c in commits:
        paths = git(repo, "diff-tree", "--no-commit-id", "--name-only", "-r", "--root", "-m", c).split("\n")
        if not any(p and is_production_path(p) for p in paths):
            continue
        if not (commit_fixes(repo, c) & reds):  # a trailer that does not count (fix_check) is no fix: it waits
            return True
    return False


def apply_entry(d, wt, e, base, regen=True, autoresolve=False):
    """Pick the entry's commits onto wt's HEAD (base, or base plus earlier entries): ('ok', None), ('conflict', files),
    ('landed', via) or ('duplicate', via). A commit already on MAIN (base's history) is dropped from the entry for good
    (a partly landed entry keeps only the rest); one only an earlier entry carries is left out of this build alone (that
    entry may still be rejected). Nothing left of it on main: 'landed' -- the caller says LANDED, never pushes empty."""
    first = git(wt, "rev-parse", "HEAD").strip() == base
    dropped, regenerated, provisional, resolved = [], [], [], [] if autoresolve else None
    conflict = pick(wt, e["commits"], dropped, regen, regenerated, provisional, resolved)
    if resolved and not conflict:  # A SOURCE CONFLICT IS RESOLVED IN THE BATCH: what it took, for the author and bisect
        e["autoresolved"] = resolved
        wlog(d, f"{e['id']}: auto-resolved in the batch, its full check the verification: "
                + "; ".join(f"{m['file']} ({', '.join(m['hunks'])})" for m in resolved))
    else:
        e.pop("autoresolved", None)
    if regenerated:  # resolved by regeneration in this build (GENERATED FILES ARE NO OVERLAP): a failure is not its own
        e["regenerated"] = sorted(regenerated)
    else:
        e.pop("regenerated", None)
    if provisional and not conflict:
        e["provisional"] = sorted(set(provisional))
        wlog(d, f"{e['id']}: {', '.join(e['provisional'])}: a row both sides changed and this host runs no regen "
                "command: kept the tree's row (main's), for the checks' ratchet to judge over the combined tree")
    else:
        e.pop("provisional", None)
    if conflict:
        return "conflict", conflict
    if not dropped:
        return "ok", None
    on_main = [(c, v) for c, v in dropped
               if (v and git(wt, "merge-base", "--is-ancestor", v, base, check=False).returncode == 0) or (not v and first)]
    if on_main:
        gone = {c for c, _v in on_main}
        e["commits"] = [c for c in e["commits"] if c not in gone]
        e.setdefault("dropped", []).extend([[c, v] for c, v in on_main])
        save_entry(d, e)
        wlog(d, f"{e['id']}: {len(on_main)} commit(s) already on {remote()}/main, dropped: "
                + ", ".join(f"{c[:10]} (via {v[:10] if v else 'an empty pick'})" for c, v in on_main))
        if not e["commits"]:
            return "landed", next((v for _c, v in reversed(on_main) if v), None)
    if not [c for c in e["commits"] if c not in {x for x, _v in dropped}]:  # all of it is on an earlier entry
        return "duplicate", next((v for _c, v in reversed(dropped) if v), None)
    return "ok", None


def already_landed(d, e, via):
    """Every commit of the entry is on main already (an earlier entry, or someone, landed it): LANDED, nothing pushed."""
    queue_verdict(d, e, "landed", f"LANDED {e['head']} (already on main via {via or 'an equivalent commit'}): "
                                  "nothing left to push", code=0, landed_sha=via, already=True)


def cancel_entry_remote(e, why):
    """An entry that ends (superseded, cancelled) takes its undecided remote run with it: the run is the entry's."""
    rem = e.get("remote") or {}
    if rem.get("run") is not None and not rem.get("status"):
        try:
            cancel_run(slug(e["worktree"]) if os.path.isdir(e.get("worktree") or "") else SHORT["movies"], rem["run"], why)
        except (Exception, SystemExit) as ex:
            err(f"could not cancel run {rem['run']} of {e['id']}: {str(ex)[:120]}")


def supersede(d, e, by, why):
    """The entry gives way to `by` (a newer entry of the same worktree and branch, or one carrying the same change): its
    checker, if running, is stopped; said in the inbox."""
    line = f"SUPERSEDED {e['id']} by {by}: {why}"
    set_change(d, e, "superseded", event=change_event(e, line, code=0), superseded_by=by, why=why, decided=ts())
    cancel_entry_remote(e, f"its queue entry was superseded by {by}")
    h = read_record(entry_owned(e["id"]).record) or {}
    if lock_held(entry_owned(e["id"]).lockpath) and h.get("pid") and int(h["pid"]) != os.getpid():
        try:
            os.kill(int(h["pid"]), signal.SIGTERM)  # its checker: its CI run is cancelled with it
        except OSError:
            pass
    wlog(d, line)


def tail_of(out, n=25):
    return "\n  ".join([l for l in out.splitlines() if l.strip()][-n:])


def diagnoses_of(repo, commits):
    """The run ids/numbers the commits' `Diagnoses-Red:` trailers name (DIAGNOSTICS ARE NO FIX, redmove.py)."""
    out = set()
    for c in commits:
        r = git(repo, "log", "-1", "--format=%(trailers:key=Diagnoses-Red,valueonly,separator=%x2C)", c, check=False)
        out |= trailer_values(r.stdout) if r.returncode == 0 else set()
    return out


def batch_order(repo, entries, reds):
    """The order a batch is built in: a fix for the current red first, then (main red) what may land while red -- tools
    only, or every commit carrying a Fixes-Red for a red run -- then the rest; by join order within each."""
    facts = gate_facts(reds)

    def rank(e):
        if reds and classify_change(e, facts)["gate_fix"]:  # its KIND: a fix of the gate's red (changekind.py)
            return 0
        if reds and not must_wait(repo, e["commits"], reds):
            return 1
        return 2
    return sorted(entries, key=lambda e: (rank(e), e["seq"]))


def supersede_older_waiting(d, worktree, branch, eid):
    """Waiting again replaces this worktree and branch's older waiting entry: only the newest (eid) lands."""
    for old in waiting_entries(d):
        if old["id"] != eid and same_change_source(old, worktree, branch):
            supersede(d, old, eid, f"{worktree} waits again; only the newest entry lands")


def note_waiting(d, log_path, head, eid):
    """The change's `=== waiting` line in its log, and its verdict line `WAITING <head> (entry <id>, n waiting)` (n:
    the waiting entries, it among them, saved yet or not)."""
    n = len({x["id"] for x in waiting_entries(d)} | {eid})
    with open(log_path, "a") as f:
        f.write(f"=== waiting {ts()} {head} as {eid} ({n} waiting)\n")
    return f"WAITING {head} (entry {eid}, {n} waiting)"
