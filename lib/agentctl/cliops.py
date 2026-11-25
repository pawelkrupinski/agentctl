# agentctl, part `cliops`: the command line's operations commands: their arguments (the governor, the reds, the inbox, the store's lease, maintenance, park and supersede, config)
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import argparse
from typing import TypeAlias
from agentctl.inbox import WAIT_KINDS_DEFAULT
from agentctl.redask import OWNER_CAUSES
from agentctl.gov import GOV_CLASSES
from agentctl.red import RED_HELP
from agentctl.branches import DISPATCH_HELP

# a parser's subcommands, as argparse's add_subparsers returns them (its class is private: the name is typeshed's)
Subparsers: TypeAlias = "argparse._SubParsersAction[argparse.ArgumentParser]"


def add_gov(sub: Subparsers) -> None:
    gv = sub.add_parser("gov", help="the governor: ONE admission control for laptop memory and CPU (status, stats, admit, evict, route, replay, usage, cores, probe)")
    gvs = gv.add_subparsers(dest="gcmd", required=True)
    gvs.add_parser("status", help="admitted work, waiters, memory, evictable idle sbt servers, pressure")
    gvs.add_parser("probe", help="one line: the governor's features (admit cores route width); scripts/jvm-gate.sh reads "
                                 "it, never agentctl's source")
    gvs.add_parser("capacity", help="one line: starved | ok | spare, the cores busy (charged, measured), admitted, waiting")
    gst = gvs.add_parser("stats", help="checks stats, plus each class's admission waits and the evictions")
    gst.add_argument("--days", type=float, default=7)
    gad = gvs.add_parser("admit", help="wait until admitted (scripts/jvm-gate.sh: --fd 7 --ticket <its file>); exit 0 admitted, 1 not (--try)")
    gad.add_argument("--class", dest="cls", default="agent", choices=list(GOV_CLASSES))
    gad.add_argument("--mem", type=float, default=None, help="expected GB (default: the class's)")
    gad.add_argument("--label", default="")
    gad.add_argument("--kind", default="job", choices=["job", "check"])
    gad.add_argument("--fd", type=int, default=None, help="lock this inherited descriptor (the caller's) as the ticket")
    gad.add_argument("--ticket", default=None, help="the ticket's lock file (the one --fd is open on)")
    gad.add_argument("--pid", type=int, default=None, help="the holder's pid, for the record")
    gad.add_argument("--cwd", default=None)
    gad.add_argument("--try", dest="try_", action="store_true", help="one look, never waiting")
    gad.add_argument("--force", action="store_true", help="admit at once (memory already counted elsewhere)")
    gad.add_argument("--job", default=None, help="what the JVM runs (testUnit, test, compile, testOnly, what-if, slice, "
                                                 "measure, serve, keeper): charged its cores")
    gad.add_argument("--cores", type=float, default=None, help="the cores it is charged (default: its --job's)")
    gus = gvs.add_parser("usage", help="record one run's CPU (sbt-warm's call): the job's measured cores")
    gus.add_argument("--job", required=True)
    gus.add_argument("--wall", type=float, required=True, help="seconds it ran")
    gus.add_argument("--cpu", type=float, required=True, help="CPU seconds it used")
    gus.add_argument("--load1", type=float, default=None, help="the 1-min load at its end (default: now)")
    gus.add_argument("--label", default="")
    gus.add_argument("--meter", default="", help="how the CPU was measured: `tree` (the process tree, sampled); a run "
                                                "without it is not counted (GOV_USAGE_METER)")
    gco = gvs.add_parser("cores", help="the cores a job is charged (measured p75, else its default)")
    gco.add_argument("--job", required=True)
    gco.add_argument("--class", dest="cls", default="agent", choices=list(GOV_CLASSES))
    gro = gvs.add_parser("route", help="ci (exit 0) when the governor would hold the job now and ci-runner's measure "
                                       "lane is free, else local (exit 1); prints why")
    gro.add_argument("--job", required=True)
    gro.add_argument("--class", dest="cls", default="agent", choices=list(GOV_CLASSES))
    gro.add_argument("--mem", type=float, default=None)
    grp = gvs.add_parser("replay", help="the admissions around --at again under the cores rule")
    grp.add_argument("--at", required=True, help="ISO time, e.g. 2026-11-18T05:45Z")
    grp.add_argument("--hold", type=float, default=30, help="minutes a ticket with no end record is taken to hold")
    grp.add_argument("--cores", type=int, default=None, help="the machine's cores (default: this one's)")
    gev = gvs.add_parser("evict", help="stop the idle kept sbt server unused longest")
    sui = sub.add_parser("suite", help="agentctl's own tests under the governor's `suite` ticket (one run at once): "
                                       "`agentctl suite` = python3 tests/run.py (parallel, a timeout per test)")
    sui.add_argument("args", nargs=argparse.REMAINDER, help="tests/run.py's arguments (-j N, --timeout S, PATTERN...)")
    gev.add_argument("--dry-run", action="store_true")


def add_laptop(sub: Subparsers) -> None:
    lap = sub.add_parser("laptop", help="the laptop role (ONE machine holds it): `laptop status`, `laptop take [--yes]`")
    lap.add_argument("lcmd", choices=("status", "take"))
    lap.add_argument("--yes", action="store_true", help="take it without asking (it is held by another machine)")


def add_lease(sub: Subparsers) -> None:
    ls_ = sub.add_parser("lease", help="the store's leases (debugging): `lease list [prefix]`")
    ls_.add_argument("lcmd", choices=("list",))
    ls_.add_argument("prefix", nargs="?", default="")
    ls_.add_argument("--all", action="store_true", help="also the leases free for over an hour")


def add_status(sub: Subparsers) -> None:
    sta = sub.add_parser("status", help="the snapshot (state.json): `--line` is the status line; else the open items "
                         "with their states and deadlines, and the leases")
    sta.add_argument("--line", action="store_true", help="the one status line (no git, no GitHub: state.json only)")
    sta.add_argument("--open", action="store_true", help="open the status page (http://agentctl.localhost/) in the browser")
    sub.add_parser("watchdog", help="the invariant watchdog's pass (launchd every 60 s): W1 oldest unlanded change, W2 "
                   "gate red nobody holds, W5 processor beat, W6 escalations notified, WM maintain ran")


def add_recover(sub: Subparsers) -> None:
    rcv = sub.add_parser("recover", help="the recovery registry: each open non-green condition, its probe, its next check")
    rcv.add_argument("rcmd", nargs="?", choices=("status", "tick"), default="status",
                     help="status (default): list; tick: probe every open condition now (clears what passes)")


def add_inbox(sub: Subparsers) -> None:
    ib = sub.add_parser("inbox", help="every verdict: the reconciler's events (QUEUED, LANDED, WAITING, NOT LANDED, WATCH ...) and wait's, "
                                      "kept whether or not the agent that ran it is alive")
    ib.add_argument("ids", nargs="*", metavar="ID", help="only these entries (an id prefix); or `wait`: block until an "
                                                          "unread entry of --kinds exists, print it, exit 0 (the notifier)")
    ib.add_argument("--kinds", default=None,
                    help=f"wait: the kinds that wake it (default {WAIT_KINDS_DEFAULT}, and any alert addressed to the "
                         "owner; also OK, FIX_OK, FIX_UNVERIFIED, "
                         "LANDED, WAITING, HELD, CHECK, OTHER)")
    ib.add_argument("--since-ack", action="store_true", help="wait: only entries not acknowledged yet, "
                    "each judged by itself (always so: kept for the watchers that pass it)")
    ib.add_argument("--timeout", type=float, default=0, help="wait: give up after S seconds, exit 2 (default: never)")
    ib.add_argument("--forever", action="store_true", help="wait: never exit; stream each entry once, one line each")
    ib.add_argument("--audience", default=None, help="only the alerts addressed to these (owner, author, fixer, info; "
                    "comma list, or all): `wait` defaults to owner, a listing to all (THE ALERT MODEL)")
    ib.add_argument("--unread", action="store_true", help="only the entries not acknowledged yet")
    ib.add_argument("--ack", action="store_true", help="acknowledge the entries listed that are YOURS (your session's, "
                                                        "worktree's or agent name's), or those named by ID")
    ib.add_argument("--all", action="store_true", help="with --ack: every entry listed, anyone's (the owner)")
    ib.add_argument("--limit", type=int, default=30, help="the newest N (default 30; 0 = all)")
    ib.add_argument("--count", action="store_true", help="print only how many entries match (every one: no --limit; "
                    "the status line's)")
    ib.add_argument("--json", action="store_true")
    ib.add_argument("-v", "--verbose", action="store_true", help="each entry's last output lines too")


def add_decide(sub: Subparsers) -> None:
    dc = sub.add_parser("decide", help="answer a red WAITING ON YOU: the answer is stored, the red re-dispatched with "
                        "it (IN PROGRESS); if the action fails you are told")
    dc.add_argument("red", help="the red: its key, or its run number or id (as the status line / ci-why name it); "
                    "`vault-<ns>`: every red waiting on that vault (you are at the machine for their Touch ID prompts)")
    dc.add_argument("answer", help="your decision, or `done` once you ran the command it asked for")
    dc.add_argument("--repo", help="the red's repo when it is not the primary's (a run number is per repo)")


def add_red_claims(rds: Subparsers) -> list[argparse.ArgumentParser]:
    """`red claim | beat | block | unblock | owner-push`: the parsers taking --repo."""
    rc_ = rds.add_parser("claim", help="take a red's lease (a run number or id, `local`, or its key): you are fixing it")
    rc_.add_argument("run")
    rc_.add_argument("--note", default="", help="one line: what you are doing")
    rc_.add_argument("--worktree", help="the worktree whose activity renews the claim (default: the current one; never "
                     "the root checkout)")
    rc_.add_argument("--fixer", help="a dispatched fixer's own record (its prompt names it): binds this session to it")
    rbt = rds.add_parser("beat", help="renew your red lease by hand: a new note is progress")
    rbt.add_argument("run")
    rbt.add_argument("--note", required=True)
    rb = rds.add_parser("block", help="the red waits on the owner: BLOCKED ON OWNER <exact command> (shown loudly; assigned)")
    rb.add_argument("run")
    rb.add_argument("--cmd", dest="owner_cmd", required=True, help="the exact command the owner must run")
    rb.add_argument("--note", default="")
    rbu = rb.add_mutually_exclusive_group()  # the probe that clears it (default: the red's own workflow goes green)
    rbu.add_argument("--until-green", metavar="WORKFLOW", help="clears when this workflow's newest run on main is green")
    rbu.add_argument("--until-http", metavar="URL", help="clears when this URL answers 2xx-4xx")
    rbu.add_argument("--until-tcp", metavar="HOST:PORT", help="clears when this port accepts a connection")
    rb.add_argument("--owner-cause", choices=sorted(OWNER_CAUSES), help="why only the owner can act (required: a code red is "
                    "never the owner's): " + "; ".join(f"{k}: {v}" for k, v in OWNER_CAUSES.items()))
    rb.add_argument("--cause", metavar="NAME", help="an outside outage (e.g. decodo): ONE condition for every red it "
                    "explains; attach to the open one, or open it with --until-*")
    rub = rds.add_parser("unblock", help="the red no longer waits on the owner (done, or superseded): ends its block, "
                         "or strikes a BLOCKED ON OWNER verdict no probe can clear")
    rub.add_argument("run")
    rub.add_argument("--why", required=True, help="what changed (the owner ran it; superseded by ...)")
    rop = rds.add_parser("owner-push", help="an owner-only repo's fix (red-repos.json, infra): file your prepared branch "
                         "with the owner as a one-liner; the red waits for it (fix-pending) until a green gate run")
    rop.add_argument("run")
    rop.add_argument("--note", default="", help="what it fixes, and which units a deploy restarts")
    rop.add_argument("--worktree", help="the worktree holding the fix (default: the current one)")
    return [rc_, rbt, rb, rop]


def add_red_others(rds: Subparsers) -> list[argparse.ArgumentParser]:
    """`red release | status | dispatch | unblame | auto` and the internal ones: the parsers taking --repo."""
    rr = rds.add_parser("release", help="drop your red lease")
    rr.add_argument("run")
    rr.add_argument("--any", action="store_true", help="release anyone's claim of it")
    rl = rds.add_parser("status", help="every red: its state, holder or offer, deadline")
    rl.add_argument("-v", "--verbose", action="store_true", help="also each red's attempts")
    rl.add_argument("--live", action="store_true", help="read the store now, not the sync's snapshot")
    rdp = rds.add_parser("dispatch", help="a red's offer at top priority: the dispatcher takes it now")
    rdp.add_argument("run")
    rub = rds.add_parser("unblame", help="WITHDRAW a red's blame (its culprit was wrongly decided, its revert not landed): "
                         "the revert never lands, its worktree is removed, the red goes to the normal fixer")
    rub.add_argument("run")
    rub.add_argument("--why", required=True, help="why the culprit was wrong (recorded; every land refusal quotes it)")
    ra = rds.add_parser("auto", help="automatic dispatch by the waiting processor: on / off / status")
    ra.add_argument("state", nargs="?", choices=("on", "off", "status"), default="status")
    rf = rds.add_parser("fixer", help=argparse.SUPPRESS)
    rf.add_argument("--record", required=True)
    rpb = rds.add_parser("probe", help=argparse.SUPPRESS)  # one boundary of a red's culprit search, judged on CI (blame.py)
    rpb.add_argument("--kind", required=True, choices=("drift", "checks"))
    rpb.add_argument("--commit", required=True)
    rpb.add_argument("--run", required=True)
    rpb.add_argument("--out", required=True)
    rpb.add_argument("--tokens", default="")
    rfo = rds.add_parser("owner", help=argparse.SUPPRESS)  # a family owner's wrapper (lib/agentctl/owner.py)
    rfo.add_argument("--family", required=True)
    return [rr, rdp, ra, rf, rfo, rl, rpb]


def add_red(sub: Subparsers) -> None:
    rd = sub.add_parser("red", help="every red build held by a lease or offered: status / claim / beat / release / block",
                        description=RED_HELP, formatter_class=argparse.RawDescriptionHelpFormatter)
    rds = rd.add_subparsers(dest="rcmd", required=True)
    for p_ in add_red_claims(rds) + add_red_others(rds):
        p_.add_argument("--repo")
    ram = rds.add_parser("am-fixer", help="exit 0 (and print who) inside a dispatched red fixer, else 1 (~/bin's "
                         "pre-commit hook asks)")


def add_issue(sub: Subparsers) -> None:
    iss = sub.add_parser("issue", help="file an agentctl defect (a fixer never edits ~/bin): stored, on the status page")
    iss.add_argument("text", nargs="*", help="what is wrong (none: --list)")
    iss.add_argument("--list", action="store_true", help="the open issues")
    iss.add_argument("--all", action="store_true", help="with --list: closed ones too")
    iss.add_argument("--close", metavar="ID", help="close an issue (the maintainer, once fixed)")
    iss.add_argument("--note", help="with --close: what fixed it")


def add_revert(sub: Subparsers) -> None:
    rvp = sub.add_parser("revert", help="reverted changes told to their landers: list the open notices, or ack one")
    rvs = rvp.add_subparsers(dest="rcmd")
    rvl = rvs.add_parser("list", help="the open notices (the default)")
    rva = rvs.add_parser("ack", help="acknowledge a notice: you rework the change (--rework) or drop it (--drop)")
    rva.add_argument("id", help="the notice's id (a prefix will do)")
    how = rva.add_mutually_exclusive_group(required=True)
    how.add_argument("--rework", action="store_true", help="you re-land it (with `Rework-Of: <sha>`)")
    how.add_argument("--drop", action="store_true", help="it stays reverted")
    rva.add_argument("--note", help="what you will do")
    rvp.add_argument("--all", action="store_true", help="closed notices too (with who acked them, how and the note)")
    # SUPPRESS: `revert --all list` keeps the parent's --all (a subparser's own default would reset it to False)
    rvl.add_argument("--all", action="store_true", default=argparse.SUPPRESS, help="closed notices too")


def add_dispatch(sub: Subparsers) -> None:
    dp = sub.add_parser("dispatch", help="own every CI dispatch: run (the forge CLI's workflow run, recorded) / record / list",
                        description=DISPATCH_HELP, formatter_class=argparse.RawDescriptionHelpFormatter)
    dps = dp.add_subparsers(dest="dcmd", required=True)
    dpr = dps.add_parser("run", help="gh workflow run, with an owner record")
    dpr.add_argument("workflow", help="the workflow file, e.g. identity-model-convergence.yml")
    dpr.add_argument("--ref", required=True)
    dpc = dps.add_parser("record", help="register a dispatch made another way")
    dpc.add_argument("--branch")
    dpc.add_argument("--run", help="its run id")
    dpc.add_argument("--workflow")
    for p_ in (dpr, dpc):
        p_.add_argument("-f", "--field", action="append", default=[], metavar="KEY=VALUE", help="a dispatch input")
        p_.add_argument("--purpose", default="", help="one line: what the run is for")
    dpl = dps.add_parser("list", help="every owner record")
    for p_ in (dpr, dpc, dpl):
        p_.add_argument("--repo")


def add_maintain(sub: Subparsers) -> None:
    mt = sub.add_parser("maintain", help="THE reaper, every 15 min (launchd net.bitcashier.agentctl-maintain): the reap, "
                                         "the cache expiry (7 days), and once a day the sweep")
    mt.add_argument("--dry-run", action="store_true", help="list what would be reaped / swept, kill and delete nothing")
    mt.add_argument("--sweep", action="store_true", help="run the sweep now, whatever the hour")


def add_supersede(sub: Subparsers) -> None:
    sup = sub.add_parser("supersede", help="mark a worktree SUPERSEDED BY another change: no orphan escalation; it closes "
                         "itself when that change lands (no worktree: list the marks)",
                         description="A worktree whose work was carried into another change (rebased with real conflict "
                         "resolution, so landed-by-content matching cannot see it) is marked SUPERSEDED BY that change: "
                         "maintain never submits or escalates it as an orphan, and removes it (clean and idle; logged, its "
                         "branch with it) once the successor lands. The successor ending unlanded lapses the mark.")
    sup.add_argument("worktree", nargs="?", help="the superseded worktree (default: list the marks)")
    sup.add_argument("--by", help="the successor: its worktree (path or name beside this one) or its change id")
    sup.add_argument("--undo", action="store_true", help="drop the mark")
    sup.add_argument("--all", action="store_true", help="list: the closed and lapsed marks too")


def add_park(sub: Subparsers) -> None:
    pk = sub.add_parser("park", help="PARK a worktree that is deliberately not landed: no orphan escalation; it "
                        "re-surfaces when its condition is met (no worktree: list the parked)",
                        description="A worktree whose commits are not on main for a reason (a design the owner is "
                        "deciding on, a change blocked on another one) is PARKED: maintain never submits, fixes or "
                        "escalates it as an orphan, `agentctl status` shows it under `parked: N`, and once its condition "
                        "is met (the blocking change landed, the date passed) it is released and the orphan rules apply "
                        "again (it gets its one submit). Parked longer than 14 days: one weekly digest line.")
    pk.add_argument("worktree", nargs="?", help="the worktree (default: list the parked)")
    pk.add_argument("--reason", help="why it is parked (shown in status and the digest)")
    pk.add_argument("--until-landed", metavar="CHANGE|COMMIT", help="until this change id (or a unique part) or commit is on main")
    pk.add_argument("--until", metavar="DATE", help="until this date (YYYY-MM-DD or ISO-8601)")
    pk.add_argument("--owner-decision", action="store_true", help="until a person unparks it")


def add_unpark(sub: Subparsers) -> None:
    upk = sub.add_parser("unpark", help="release a parked worktree: the orphan rules apply again")
    upk.add_argument("worktree")


def add_reap(sub: Subparsers) -> None:
    rp_ = sub.add_parser("reap", help="the reap alone: orphans --kill --safe (stale JVMs included); --dry-run lists")
    rp_.add_argument("--dry-run", action="store_true")


def add_config(sub: Subparsers) -> None:
    cf = sub.add_parser("config", help="the knobs: what applies and from where (default), `defaults` (a complete "
                                       "agentctl.toml), `doc [--write]` (AGENTCTL.md's reference)")
    cf.add_argument("ccmd", nargs="?", choices=("show", "defaults", "doc"), default="show")
    cf.add_argument("--all", action="store_true", help="show: every knob, set or not")
    cf.add_argument("--write", action="store_true", help="doc: regenerate AGENTCTL.md's config reference")
