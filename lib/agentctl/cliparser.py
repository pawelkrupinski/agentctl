# agentctl, part `cliparser`: the command line's parser: every command's arguments, one add_<command> each (the landing and CI commands here, the operations commands in cliops).
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import argparse
from agentctl.timelinecmd import add_timeline_commands
from agentctl.usage import USAGE
from agentctl.core import CI_WHY_HELP, ENV_HELP, LAND_HELP, WAIT_RUN_HELP, remote
from agentctl.ghapp import GHAPP_ROLES
from agentctl.cleanup import SWEEP_IDLE_DAYS
from agentctl.queueuntil import parse_duration
from agentctl.waiting import WAITING_HELP
from agentctl.remotestore import DEFAULT_STORE_URL, STORE_PORT
from agentctl.launchd import SERVICE_LABEL
from agentctl.cliops import (
    Subparsers, add_config, add_decide, add_dispatch, add_gov, add_inbox, add_issue, add_laptop, add_lease,
    add_maintain, add_park, add_reap, add_recover, add_red, add_revert, add_status, add_supersede, add_unpark)
from agentctl.storeauth import STORE_AUTHS
from agentctl.slots import LAND_SUBCOMMANDS


def parser() -> argparse.ArgumentParser:
    """agentctl's argument parser: the usage text, then every command's subparser, in COMMANDS order."""
    ap = argparse.ArgumentParser(prog="agentctl", description=USAGE, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for add in COMMANDS:
        add(sub)
    return ap


def add_wait(sub: Subparsers) -> None:
    w = sub.add_parser("wait")
    wsub = w.add_subparsers(dest="kind", required=True)
    wr = wsub.add_parser("run", help="block until GitHub Actions runs finish; print only the verdicts",
                         description=WAIT_RUN_HELP, formatter_class=argparse.RawDescriptionHelpFormatter)
    wr.add_argument("ids", nargs="+", metavar="ID", help="run ids (action_run.id); a leading repo name is accepted (legacy form)")
    wr.add_argument("--repo", "-R", help="o/r or short name (movies, core, infra, ...); default: the cwd's remote, else movies")
    wr.add_argument("--until", choices=("done", "landable"), default="done",
                    help="landable: for an Identity takes (or a --full measure's Identity model convergence) measure/ run, stop once the takes rows end and print their verdict files")
    wr.add_argument("--tail", type=int, default=8, metavar="N", help="relevant log lines (FAILED/error/assert) per failed job (default 8; 0 = none)")
    wf = wsub.add_parser("file", help="block until a file appears (and matches)")
    wf.add_argument("target")
    wf.add_argument("--contains")
    for p in (wr, wf):
        p.add_argument("--interval", type=int, default=300, help="backoff cap in seconds (<= 300)")
        p.add_argument("--timeout", type=int, default=6 * 3600)


def add_state(sub: Subparsers) -> None:
    s = sub.add_parser("state", help="print/update scratchpad/state-<topic>.md (the hand-off file for long waits)",
                       description="Print scratchpad/state-<topic>.md, creating it from the template first. --set field=value "
                       "replaces a field, --add field=value appends to a comma-separated one. Write it before ending a turn "
                       "that waits >30 min on CI. Reference: ~/bin/AGENTCTL.md")
    s.add_argument("topic")
    s.add_argument("--set", action="append")
    s.add_argument("--add", action="append")


def add_prepush_check(sub: Subparsers) -> None:
    pp = sub.add_parser("prepush-check", help="refuse a push that strays outside --allow or reverts others' work on main",
                        description="Refuses when base...head touches a file outside the allowed set, or returns more than "
                        "--max-foreign (2) files to an older main version (the `reset --soft` squash onto a moved main). "
                        "movies' pre-push hook calls it with --hook; `agentctl land` runs it before every push. Exit 1 refused.")
    pp.add_argument("--base", default=f"{remote()}/main")
    pp.add_argument("--head", default="HEAD")
    pp.add_argument("--allow", action="append")
    pp.add_argument("--max-foreign", type=int)
    pp.add_argument("--hook", nargs="+", metavar="REMOTE [URL]")


def add_cleanup(sub: Subparsers) -> None:
    c = sub.add_parser("cleanup", help="remove ONLY proven-merged worktrees/branches and finished measure/* branches",
                       description="Removes only clean, idle worktrees whose HEAD is on origin/main (ancestor or all commits "
                       "patch-equivalent), their branches, and remote measure/* branches whose runs finished. Every deletion "
                       "is logged to scratchpad/agentctl-deleted.log. Dry run by default on a TTY; --execute acts.")
    g = c.add_mutually_exclusive_group()
    g.add_argument("--dry-run", action="store_true")
    g.add_argument("--execute", action="store_true", help="act even when stdin is a TTY")
    c.add_argument("--repo", action="append")
    c.add_argument("--no-fetch", action="store_true")
    c.add_argument("--no-remote", action="store_true", help="skip remote measure/* branches")
    c.add_argument("--min-age-hours", type=float, default=2.0)
    c.add_argument("--deadline", type=float, metavar="S", help="stop by itself S seconds in: no tree looked at past it, "
                   "no removal begun within the removal reserve of it (the upkeep's, under its own bound)")
    c.add_argument("--include-caches", action="store_true", help="also consider worktrees under ~/.cache")
    c.add_argument("-v", "--verbose", action="store_true", help="also list what is kept and why")


def add_ci_why(sub: Subparsers) -> None:
    cw = sub.add_parser("ci-why", help="why is main red: the gating runs' failed jobs, log tails, infra vs test, clearing rule",
                        description=CI_WHY_HELP, formatter_class=argparse.RawDescriptionHelpFormatter)
    cw.add_argument("target", nargs="?", default="main", help="`main` (default) or a run id to explain")
    cw.add_argument("--repo", "-R")
    cw.add_argument("--tail", type=int, default=6, metavar="N", help="relevant log lines per failed job")
    cw.add_argument("--summary", action="store_true", help="one line for the status line: each gate row, and who is fixing a red one")
    cw.add_argument("--cached", action="store_true", help="with --summary: the line the waiting processor published if under 3 min old, else a live read, else `gate UNREAD (as of HH:MM ...)`")


def add_land(sub: Subparsers) -> None:
    ld = sub.add_parser("land", help="fetch, rebase onto origin/main, prepush-check, push (hook runs), verify: LANDED <sha>",
                        description=LAND_HELP, formatter_class=argparse.RawDescriptionHelpFormatter)
    ld.add_argument("lcmd", nargs="?", choices=sorted(LAND_SUBCOMMANDS), metavar="status|hold|release|cancel",
                    help="the queue instead of a land: status (the changes, the batch, the gate), hold / release (take "
                         "no batch until release or the hold's expiry, on ci-runner's store), cancel <id>")
    ld.add_argument("entry", nargs="?", help="cancel, status: the change's id (a unique part of it); status <entry> prints that one change, open or decided, with its verdict")
    ld.add_argument("--for", dest="hold_for", type=float, metavar="MIN",
                    help="hold: minutes until the hold expires by itself (default 240, at most 1440)")
    ld.add_argument("--why", dest="hold_why", help="hold: the reason, shown on the status line and page")
    ld.add_argument("--repo", help="checkout to land from (default: cwd)")
    ld.add_argument("--branch", help="local branch to land (default: the checked-out HEAD)")
    ld.add_argument("--stash", action="store_true", help="set uncommitted changes aside in a NAMED stash and restore them after")
    ld.add_argument("--release-hold", action="store_true",
                    help="land commits that carry a `Hold: <why>` trailer (only on the owner's go; refused otherwise)")
    ld.add_argument("--no-watch", action="store_true", help="exit at LANDED instead of watching Main and the convergence")
    ld.add_argument("--follow", action="store_true",
                    help="movies: run the checks here and stay until it lands, then watch (default: QUEUED at once; "
                         "the checks run in the waiting processor and every verdict goes to `agentctl inbox`)")
    ld.add_argument("--no-fast-precheck", action="store_true",
                    help="movies: start no scripts/land-precheck beside the queue (default: it runs alongside, and a "
                         "refusal cancels the queued change with NOT LANDED in the inbox)")
    lp = sub.add_parser("land-precheck", help=argparse.SUPPRESS)  # land's detached fast precheck (queuesubmit.py)
    lp.add_argument("entry")
    lp.add_argument("--repo", required=True)
    lp.add_argument("--base", required=True)
    lp.add_argument("--head", required=True)
    ld.add_argument("--no-batch", action="store_true",
                    help="movies: check this change alone, never in a backlog batch with other queued changes")
    ld.add_argument("--until-done", action="store_true",
                    help="movies agents' ending: submit (or resume this worktree's entry at HEAD), wait for the verdict, "
                         "then NOT LANDED + the next command (exit 1), or prove every local commit on origin/main, "
                         "remove the clean worktree and its branch: DONE <sha> ... worktree removed (exit 0); "
                         "3 landed but kept, 2 the deadline")
    ld.add_argument("--deadline", type=parse_duration, metavar="DUR",
                    help="--until-done: give up waiting after DUR (90s, 30m, 3h; default 4h); called again, it resumes")
    ld.add_argument("--keep-worktree", action="store_true",
                    help="after WATCH OK, keep the worktree land ran from (default: closed when proven merged and clean)")


def add_env(sub: Subparsers) -> None:
    ev = sub.add_parser("env", help="print sdkman sbt/java export lines (--print) or check the agent environment (--check)",
                        description=ENV_HELP, formatter_class=argparse.RawDescriptionHelpFormatter)
    eg = ev.add_mutually_exclusive_group()
    eg.add_argument("--print", action="store_true", help="export lines for eval (default)")
    eg.add_argument("--check", action="store_true", help="check sbt, java, the vault, the store tunnel and gh")
    ev.add_argument("--ns", choices=("movies", "bitcashier"), help="vault namespace (default: movies inside ~/projects/movies, else bitcashier)")


def add_orphans(sub: Subparsers) -> None:
    o = sub.add_parser("orphans", help="list stray owners, wait loops and sbt servers (worktree, owner or runs gone; old code)",
                       description="Lists owner records that are strays (a dead process's record and its uncancelled runs, "
                       "an owner on old code, an owner whose cwd is gone), and wait loops and sbt servers that are orphaned: "
                       "worktree gone, owning shell dead, every target run finished, or a duplicate of an older loop on "
                       "the same runs. --kill cancels a dead owner's runs and drops its record, and sends SIGTERM to the "
                       "rest (to them and their children).")
    o.add_argument("--kill", action="store_true")
    o.add_argument("--safe", action="store_true",
                   help="the scheduled reaper's mode: with --kill, end only strays (dead owners' records and runs, stray "
                        "wait loops, stuck sbt servers), never an owner merely on old code; every kill is logged")


def add_waiting(sub: Subparsers) -> None:
    wg = sub.add_parser("waiting", help="the processor and its queue: changes checked and pushed, held while main is red, batched at green",
                        description=WAITING_HELP, formatter_class=argparse.RawDescriptionHelpFormatter)
    wgs = wg.add_subparsers(dest="wcmd", required=True)
    wgs.add_parser("status", help="the queued and waiting changes, the batch, the processor, the gate").add_argument("--repo")
    wgs.add_parser("pause", help="the processor takes no batch until resume (changes still queue)").add_argument("--repo")
    wgs.add_parser("resume", help="undo pause, and a HELD batch's wait").add_argument("--repo")
    wgc = wgs.add_parser("cancel", help="drop a queued or waiting change")
    wgc.add_argument("entry")
    wgc.add_argument("--repo")
    wgr = wgs.add_parser("run", help="the processor (land starts it detached; one at a time, by lock)")
    wgr.add_argument("--once", action="store_true")
    wgr.add_argument("--service", action="store_true", help="stay when `waiting` is empty (the launchd agent's mode)")
    wgsv = wgs.add_parser("service", help=f"the processor as a launchd agent ({SERVICE_LABEL}: KeepAlive, RunAtLoad)")
    wgsv.add_argument("action", choices=("install", "uninstall", "status"))
    wgsv.add_argument("--repo")
    wgr.add_argument("--repo")
    wcb = wgs.add_parser("check-batch", help="the batch's executor: its check, its bisect (the processor starts it)")
    wcb.add_argument("entry", help="the batch item's id")
    wcb.add_argument("--repo")
    wgk = wgs.add_parser("check", help="one queued change's executor: its checks -> checked / rejected / queued again "
                                       "(the processor starts it; the processor pushes)")
    wgk.add_argument("entry")
    wgk.add_argument("--repo")
    wga = wgs.add_parser("await", help="wait for one change on `waiting` to land, then watch (land's own wait, resumed "
                                       "after a restart on new code)")
    wga.add_argument("entry")
    wga.add_argument("--repo")
    wga.add_argument("--close")


def add_watch(sub: Subparsers) -> None:
    wt = sub.add_parser("watch", help="watch Main and the convergence for a landed sha (land's watch, resumed after a "
                                      "restart on new code): WATCH OK / RED / UNKNOWN")
    wt.add_argument("--sha", required=True)
    wt.add_argument("--repo")
    wt.add_argument("--pushed-at", type=float, default=None)
    wt.add_argument("--close", help="after WATCH OK, remove this worktree and its branch when proven merged")
    wt.add_argument("--for", dest="agent_cwd", help="the agent's worktree (the inbox entry's cwd; it may be removed)")
    wt.add_argument("--fixes", action="append", default=[], metavar="N",
                    help="a run the landed commits claim to fix (Fixes-Red); the sha's own trailers are read too")


def add_sweep(sub: Subparsers) -> None:
    sw = sub.add_parser("sweep", help="daily report: unmerged idle worktrees/branches (listed); remote branches merged into "
                                      "main (deleted, standing OK)")
    sw.add_argument("--repo", action="append", help="a checkout (repeatable); default movies")
    sw.add_argument("--idle-days", type=float, default=SWEEP_IDLE_DAYS)
    sw.add_argument("--dry-run", action="store_true")
    sw.add_argument("--no-report", action="store_true", help="do not write ~/.cache/agentctl/sweep/<date>.txt")


def add_store(sub: Subparsers) -> None:
    sto = sub.add_parser("store", help="phase 2: the store's HTTP API (`store serve`, ci-runner's agentctl-store unit)")
    stos = sto.add_subparsers(dest="scmd", required=True)
    sts = stos.add_parser("serve", help="serve $AGENTCTL_STATE_ROOT/store.db: the §6.3 API and GET /metrics")
    sts.add_argument("--listen", default=f"127.0.0.1:{STORE_PORT}", help="ADDR:PORT")
    sts.add_argument("--allow-user", action="append", help="a login with the whole API (repeatable)")
    sts.add_argument("--metrics-user", action="append", help="a login with GET /metrics only (repeatable)")
    sts.add_argument("--auth", choices=STORE_AUTHS, default="store-tokens",
                     help="store-tokens (the only one): the store's own tokens (`store token issue`)")
    stt = stos.add_parser("token", help="the store's own tokens (`store serve`), on the store's host")
    stts = stt.add_subparsers(dest="tcmd", required=True)
    sti = stts.add_parser("issue", help="print a new token for USER (once; keep it in the vault)")
    sti.add_argument("--user", required=True, help="the login it names (an --allow-user or --metrics-user)")
    sti.add_argument("--name", default="", help="what holds it: laptop, scrape, ...")
    str_ = stts.add_parser("revoke", help="revoke a token by its id (`store token list`)")
    str_.add_argument("id")
    stts.add_parser("list", help="every token: id, user, name, issued, revoked")
    stu = stos.add_parser("use", help=f"the laptop's switch: every agentctl process here uses the store at URL "
                                      f"(default without one: {DEFAULT_STORE_URL}, ci-runner's through the tunnel)")
    stu.add_argument("url", help="http://localhost:3014 (through the tunnel)")


def add_github_app(sub: Subparsers) -> None:
    gha = sub.add_parser("github-app", help="ci-runner's GitHub App identity (AGENTCTL_IDENTITY=github-app): an "
                         "installation token, or git's credential helper")
    ghs = gha.add_subparsers(dest="gcmd", required=True)
    ght = ghs.add_parser("token", help="print an installation token scoped to one repository")
    ght.add_argument("--repo", required=True, help="owner/name")
    ght.add_argument("--role", choices=GHAPP_ROLES, help="default: lander on its repos, reader elsewhere")
    ghc = ghs.add_parser("credential", help="git's credential helper protocol (get only)")
    ghc.add_argument("op", nargs="?", default="get")


def add_tick(sub: Subparsers) -> None:
    tk = sub.add_parser("tick", help="the processor: the reconciler's loop of ticks (`tick service install|uninstall|status`: "
                        "it as the launchd agent)")
    tk.add_argument("tcmd", nargs="?", choices=("service",), help="service: the launchd agent")
    tk.add_argument("action", nargs="?", choices=("install", "uninstall", "status"))
    tk.add_argument("--once", action="store_true", help="one tick, then exit")
    tk.add_argument("--repo", help="the repository (default: the cwd's, else the movies clone)")


def add_queue(sub: Subparsers) -> None:
    q = sub.add_parser("queue", help="RETIRED 2026-10-26: use `agentctl land` (prints this and exits 2)")
    q.add_argument("rest", nargs=argparse.REMAINDER)


def add_checks(sub: Subparsers) -> None:
    ck = sub.add_parser("checks", help="the pre-commit check slots: status, and stats per concurrency level")
    cks = ck.add_subparsers(dest="ccmd", required=True)
    cks.add_parser("status", help="the slots and their holders")
    cst = cks.add_parser("stats", help="p50/p90 check duration and slot wait per concurrency level")
    cst.add_argument("--days", type=float, default=7)


COMMANDS = (add_wait, add_state, add_prepush_check, add_cleanup, add_ci_why, add_land, add_env, add_orphans,
            add_waiting, add_watch, add_sweep, add_store, add_github_app, add_tick, add_queue, add_checks, add_gov,
            add_laptop, add_lease, add_status, add_recover, add_inbox, add_decide, add_red, add_issue, add_revert,
            add_dispatch, add_maintain, add_supersede, add_park, add_unpark, add_reap, add_config,
            add_timeline_commands)
