# agentctl, part `cliparser`: the command line's parser: every command's arguments, one add_<command> each (the landing and CI commands here, the operations commands in cliops).
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import argparse
from agentctl.storeauth import STORE_AUTHS
from agentctl.core import remote
from agentctl.ghapp import GHAPP_ROLES
from agentctl.timelinecmd import add_timeline_commands
from agentctl.remotestore import DEFAULT_STORE_URL, STORE_PORT
from agentctl.waiting import WAITING_HELP
from agentctl.cleanup import SWEEP_IDLE_DAYS
from agentctl.queueuntil import parse_duration
from agentctl.cliops import (
    Subparsers, add_config, add_decide, add_dispatch, add_gov, add_inbox, add_issue, add_laptop, add_lease,
    add_maintain, add_park, add_reap, add_recover, add_red, add_revert, add_status, add_supersede, add_unpark)
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
    wgr = wgs.add_parser("run", help="the processor (land starts it detached; one at a time, by lock)")
    wgr.add_argument("--once", action="store_true")
    wgr.add_argument("--service", action="store_true", help="stay when `waiting` is empty (the launchd agent's mode)")
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


USAGE = """agentctl: cheaper, safer Claude subagent orchestration for the bitcashier repos and movies.

  agentctl wait run <id>... [--repo R] [--until done|landable] [--tail N] [--interval S] [--timeout S]
        Blocks on GitHub's actions API (backoff 60 s -> x1.5 -> cap 300 s) and prints ONLY the verdict per run:
        `run <o/r>#<id>: <conclusion>[; failed: job, ...]` plus each failed job's last N relevant log lines
        (FAILED / error / assert). ONE waiter per run (lock in the scratchpad); a second call attaches to the live
        waiter's verdict file instead of polling. --until landable: an Identity takes (or --full convergence) measure/ run is
        done once its takes rows end; prints their verdict.txt files. Exit 0 all green, 1 any red, 2 timeout.
        AGENTS: start it with run_in_background=true and END YOUR TURN; never loop/sleep in the foreground.
  agentctl wait file <path> [--contains REGEX] [--interval S] [--timeout S]
        Same, for a file appearing (and containing REGEX).
  agentctl ci-why [main|<run id>|<run number>] [--repo R] [--tail N]
        Why main is red: the red gate's three workflows (movies: Main, convergence, Identity takes), their newest decided runs, failed jobs + log lines,
        commit/author, INFRA-SHAPED vs TEST FAILURE, and the clearing rule (later green run / Fixes-Red:). Exit 1 red.
  agentctl land [--repo PATH] [--branch B] [--stash]
        fetch, rebase onto <remote>/main, prepush-check, push (the hook runs, sdkman sbt/java on PATH), retry push races
        x3, verify: last line `LANDED <sha>` or `NOT LANDED: <reason>` (+ ci-why on a red gate). Never --no-verify.
        One lander per repository at a time (an flock): the next waits, then rebases onto what the previous landed.
        In a repo with scripts/hooks/pre-push the hook runs FIRST as a dry run (--range <remote>/main..HEAD; the long
        area tests run there), then the push reuses its pass. The identity/page area tests take minutes: run land
        with run_in_background=true.
  agentctl queue ...
        RETIRED 2026-10-26 (no pre-merge CI left to batch): prints the retirement note and exits 2. Use `land`.
  agentctl env [--print|--check] [--ns movies|bitcashier]
        eval "$(agentctl env --print)" puts sdkman sbt/java on PATH; --check checks sbt, java, vault, tunnel, token.
        Locked vault: one line `vault <ns> locked: ask the user to run `! secrets unlock <ns>``, exit 1. Never unlocks.
  agentctl inbox [ID...] [--unread] [--ack [--all]] [--limit N] [--json] [-v]
        Every final verdict of land / watch / wait / waiting await (and land's WAITING line), one JSON file each in
        ~/.cache/agentctl/inbox/, written however the command ended. The orchestrator reads it, agent alive or not.
  agentctl state <topic> [--set field=value ...] [--add field=value ...]
        Print scratchpad/state-<topic>.md, creating it from the template first; --set replaces a field,
        --add appends to a comma-separated one (commits, run ids).
  agentctl prepush-check [--base <remote>/main] [--head HEAD] [--allow GLOB ...] [--max-foreign N]
  agentctl prepush-check --hook <remote> [<url>]       (pre-push hook mode: reads the hook's stdin)
        Refuses when `base...head` touches a file outside the allowed set ($AGENTCTL_ALLOW, whitespace-separated
        globs, or --allow), or when it deletes/reverts more than N (default 2, $AGENTCTL_MAX_FOREIGN) files back to
        an older version of main -- the `reset --soft` squash onto a moved main (not when the push also rewrites the
        definition those lines referred to: a coherent edit). `git revert` commits are exempt
        for the files of the commit they revert.
  agentctl cleanup [--dry-run|--execute] [--repo PATH ...] [--no-fetch] [--no-remote] [--min-age-hours H] [--deadline S]
        Removes ONLY proven-merged items: clean, idle worktrees whose HEAD is an ancestor of <remote>/main or whose
        commits are all patch-equivalent on it (`git cherry`), their local branches, and remote measure/* branches
        whose runs all finished (or >24 h old with no run in progress). Every deletion -> agentctl-deleted.log.
        Bounded: each tree's git status 120 s (else kept, undecided), a fetch the unattended 180 s (one timing out
        skips the other repos); --deadline S stops it by itself (no removal begun within 300 s of it).
        Dry run by default when stdin is a TTY.
  agentctl orphans [--kill]
        Wait loops / sbt servers whose worktree is gone, whose owning shell died, whose target runs finished,
        or that duplicate an older loop on the same runs.

Full reference, examples and the standard agent workflow: ~/bin/AGENTCTL.md

Scratchpad: $AGENTCTL_SCRATCH, else /private/tmp/claude-<uid>/*/<$CLAUDE_CODE_SESSION_ID>/scratchpad, else
~/.cache/agentctl. gh reads GitHub with its own auth ($GH_TOKEN, else `gh auth login`'s). The store token comes from $AGENTCTL_STORE_TOKEN, else the waiting processor's private socket, else `secrets get bitcashier AGENTCTL_STORE_TOKEN` (absolute path); never printed.
"""


WAIT_RUN_HELP = """Block until GitHub Actions runs finish; print only the verdicts.

Agents: start this with the Bash tool's run_in_background=true and END YOUR TURN -- you are re-invoked when it
exits. Never wrap it (or gh run watch) in a foreground sleep/until loop. A second `wait run` on the same id attaches to
the first waiter (single waiter per target), so it is safe to call again after a resume.

Examples:
  agentctl wait run 1961 --until landable          # a measure run: stop when the takes rows end
  agentctl wait run 1976 1984 --repo movies --tail 12
Exit: 0 all green, 1 any red/cancelled, 2 timeout. A waiter that vanished is taken over."""


CI_WHY_HELP = """Why is main red? For movies: the newest DECIDED run on main (success/failure; cancelled and skipped say
nothing) of exactly the three workflows the gate reads (the owner, 2026-10-27; Identity takes 2026-11-08) -- Main, Identity model convergence and Identity takes;
every other red on main is listed after, as information only. Other repos: every workflow seen on main. For each red one: failed jobs, their relevant log lines, the commit
and author, INFRA-SHAPED (setup-step failure, 0-length log, artifact already exists, ENOENT, ...) vs TEST FAILURE, and
the gate's clearing rule (a later green run, or `Fixes-Red: <#number|run id>` on every production commit).
  agentctl ci-why                 # movies (or the cwd's repo)
  agentctl ci-why 1934            # explain one run
Exit: 0 green, 1 red (or a run explained)."""


LAND_HELP = """Land the current commit(s) on origin/main, safely, in one command.
MOVIES (the reconciler, ~/bin/AGENTCTL.md "land"): fetch -> rebase onto origin/main -> prepush-check (the squash
guard) -> a queued change item, its commits pushed to refs/heads/land/<id> -> `QUEUED <id> (<sha>)` at once: END YOUR
TURN. The processor's tick checks it in a check tree (an executor holding check/<id>, within the governor; on ci-runner
under pressure), pushes it under the land/<repo> lease -> LANDED and a watch item (Main and the convergence for the sha:
WATCH OK / RED / UNKNOWN); main red and the change must wait (a production path, no Fixes-Red: for the red run):
WAITING, landed with the batch checked once at green. Every verdict goes to `agentctl inbox`, naming your worktree.
--UNTIL-DONE (movies agents' last step, run in the background): submit -- or resume this worktree's entry at HEAD, never
queued twice -- the fast precheck in the foreground, wait for the verdict (--deadline, default 4h), then: NOT LANDED:
<why> + `next: <command>` (exit 1); LANDED, every local commit's patch on origin/main and the worktree clean and this
agent's own -> worktree and branch removed, `DONE <sha> ... worktree removed` (exit 0); landed but kept (dirty,
foreign, root, unproven) exit 3; the deadline exit 2 (call it again to resume).
THE FAST PRECHECK: a repo with an executable scripts/land-precheck (movies) has it run beside the queue, never before
QUEUED; a refusal cancels the queued change with `NOT LANDED: the fast precheck refused <id>` in the inbox, minutes
before the landing check would say it. --no-fast-precheck starts none.
IN-PROCESS (--follow, --no-watch, AGENTCTL_QUEUE=0; other repos always; a red fixer queues and waits): the checks here
(movies: the hook's dry run --range origin/main..<sha>), then under the land/<repo> lease fetch, push (the repo's
pre-push hook runs, reusing the dry run's pass), on a race refetch, rebase and retry up to 3 times, verify with
merge-base / git cherry: `LANDED <sha>` or `NOT LANDED: <exact reason>` (red gate: + the ci-why summary). --follow then
waits for a WAITING change's batch and watches in-process; --no-watch exits at LANDED.
Never passes --no-verify and refuses to run with PREPUSH_SKIP set. Refuses a dirty tree unless --stash (a named stash,
restored afterwards, found by its sha -- the stash list is shared by every worktree).
Exit: 0 queued / landed (and watched green), 1 not landed, 2 landed but the watch could not tell, 3 landed and main's CI
RED."""


ENV_HELP = """Agent environment, without secrets.
  eval "$(agentctl env --print)"   # sdkman's sbt + java first on PATH, JAVA_HOME; exit 1 (one line on stderr) on a locked vault
  agentctl env --check            # sbt, a java that runs, `secrets status <ns>`, the tunnel's localhost:3014 (the
                                  # agentctl store) and gh
A locked vault prints exactly: vault <ns> locked: ask the user to run `! secrets unlock <ns>`  -- and exits 1.
An agentctl fixer holding its credentials (AGENTCTL_RED_FIXER, the store token) reads it as not needed: exit 0.
agentctl never runs `secrets unlock` (Touch ID is the user's). Output never contains a token: tools read it at use."""
