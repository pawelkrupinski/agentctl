# agentctl, part `cli`: the command line (main: the parser and the handlers' table).
# Runs inside agentctl's one namespace, after the parts before it in lib/agentctl/__init__.py's PARTS (it uses their names).

# THE COMPOSITION ROOT: what every process wires before any command runs.
wire_store(process_store)
wire_token_missing_note(token_unavailable_note)


# ---------------------------------------------------------------- main
def main(argv=None):
    if (sys.argv[1:] if argv is None else argv) == ["--selftest"]:
        return selftest()
    ap = argparse.ArgumentParser(prog="agentctl", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
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
    s = sub.add_parser("state", help="print/update scratchpad/state-<topic>.md (the hand-off file for long waits)",
                       description="Print scratchpad/state-<topic>.md, creating it from the template first. --set field=value "
                       "replaces a field, --add field=value appends to a comma-separated one. Write it before ending a turn "
                       "that waits >30 min on CI. Reference: ~/bin/AGENTCTL.md")
    s.add_argument("topic")
    s.add_argument("--set", action="append")
    s.add_argument("--add", action="append")
    pp = sub.add_parser("prepush-check", help="refuse a push that strays outside --allow or reverts others' work on main",
                        description="Refuses when base...head touches a file outside the allowed set, or returns more than "
                        "--max-foreign (2) files to an older main version (the `reset --soft` squash onto a moved main). "
                        "movies' pre-push hook calls it with --hook; `agentctl land` runs it before every push. Exit 1 refused.")
    pp.add_argument("--base", default=f"{remote()}/main")
    pp.add_argument("--head", default="HEAD")
    pp.add_argument("--allow", action="append")
    pp.add_argument("--max-foreign", type=int)
    pp.add_argument("--hook", nargs="+", metavar="REMOTE [URL]")
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
    cw = sub.add_parser("ci-why", help="why is main red: the gating runs' failed jobs, log tails, infra vs test, clearing rule",
                        description=CI_WHY_HELP, formatter_class=argparse.RawDescriptionHelpFormatter)
    cw.add_argument("target", nargs="?", default="main", help="`main` (default) or a run id to explain")
    cw.add_argument("--repo", "-R")
    cw.add_argument("--tail", type=int, default=6, metavar="N", help="relevant log lines per failed job")
    cw.add_argument("--summary", action="store_true", help="one line for the status line: each gate row, and who is fixing a red one")
    cw.add_argument("--cached", action="store_true", help="with --summary: the line the waiting processor published if under 3 min old, else a live read, else `gate UNREAD (as of HH:MM ...)`")
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
    ev = sub.add_parser("env", help="print sdkman sbt/java export lines (--print) or check the agent environment (--check)",
                        description=ENV_HELP, formatter_class=argparse.RawDescriptionHelpFormatter)
    eg = ev.add_mutually_exclusive_group()
    eg.add_argument("--print", action="store_true", help="export lines for eval (default)")
    eg.add_argument("--check", action="store_true", help="check sbt, java, the vault, the store tunnel and gh")
    ev.add_argument("--ns", choices=("movies", "bitcashier"), help="vault namespace (default: movies inside ~/projects/movies, else bitcashier)")
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
    wt = sub.add_parser("watch", help="watch Main and the convergence for a landed sha (land's watch, resumed after a "
                                      "restart on new code): WATCH OK / RED / UNKNOWN")
    wt.add_argument("--sha", required=True)
    wt.add_argument("--repo")
    wt.add_argument("--pushed-at", type=float, default=None)
    wt.add_argument("--close", help="after WATCH OK, remove this worktree and its branch when proven merged")
    wt.add_argument("--for", dest="agent_cwd", help="the agent's worktree (the inbox entry's cwd; it may be removed)")
    wt.add_argument("--fixes", action="append", default=[], metavar="N",
                    help="a run the landed commits claim to fix (Fixes-Red); the sha's own trailers are read too")
    sw = sub.add_parser("sweep", help="daily report: unmerged idle worktrees/branches (listed); remote branches merged into "
                                      "main (deleted, standing OK)")
    sw.add_argument("--repo", action="append", help="a checkout (repeatable); default movies")
    sw.add_argument("--idle-days", type=float, default=SWEEP_IDLE_DAYS)
    sw.add_argument("--dry-run", action="store_true")
    sw.add_argument("--no-report", action="store_true", help="do not write ~/.cache/agentctl/sweep/<date>.txt")
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
    gha = sub.add_parser("github-app", help="ci-runner's GitHub App identity (AGENTCTL_IDENTITY=github-app): an "
                         "installation token, or git's credential helper")
    ghs = gha.add_subparsers(dest="gcmd", required=True)
    ght = ghs.add_parser("token", help="print an installation token scoped to one repository")
    ght.add_argument("--repo", required=True, help="owner/name")
    ght.add_argument("--role", choices=GHAPP_ROLES, help="default: lander on its repos, reader elsewhere")
    ghc = ghs.add_parser("credential", help="git's credential helper protocol (get only)")
    ghc.add_argument("op", nargs="?", default="get")
    tk = sub.add_parser("tick", help="the processor: the reconciler's loop of ticks (`tick service install|uninstall|status`: "
                        "it as the launchd agent)")
    tk.add_argument("tcmd", nargs="?", choices=("service",), help="service: the launchd agent")
    tk.add_argument("action", nargs="?", choices=("install", "uninstall", "status"))
    tk.add_argument("--once", action="store_true", help="one tick, then exit")
    tk.add_argument("--repo", help="the repository (default: the cwd's, else the movies clone)")
    q = sub.add_parser("queue", help="RETIRED 2026-10-26: use `agentctl land` (prints this and exits 2)")
    q.add_argument("rest", nargs=argparse.REMAINDER)
    ck = sub.add_parser("checks", help="the pre-commit check slots: status, and stats per concurrency level")
    cks = ck.add_subparsers(dest="ccmd", required=True)
    cks.add_parser("status", help="the slots and their holders")
    cst = cks.add_parser("stats", help="p50/p90 check duration and slot wait per concurrency level")
    cst.add_argument("--days", type=float, default=7)
    gv = sub.add_parser("gov", help="the governor: ONE admission control for laptop memory and CPU (status, stats, admit, evict, route, replay, usage, cores)")
    gvs = gv.add_subparsers(dest="gcmd", required=True)
    gvs.add_parser("status", help="admitted work, waiters, memory, evictable idle sbt servers, pressure")
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
    lap = sub.add_parser("laptop", help="the laptop role (ONE machine holds it): `laptop status`, `laptop take [--yes]`")
    lap.add_argument("lcmd", choices=("status", "take"))
    lap.add_argument("--yes", action="store_true", help="take it without asking (it is held by another machine)")
    ls_ = sub.add_parser("lease", help="the store's leases (debugging): `lease list [prefix]`")
    ls_.add_argument("lcmd", choices=("list",))
    ls_.add_argument("prefix", nargs="?", default="")
    ls_.add_argument("--all", action="store_true", help="also the leases free for over an hour")
    sta = sub.add_parser("status", help="the snapshot (state.json): `--line` is the status line; else the open items "
                         "with their states and deadlines, and the leases")
    sta.add_argument("--line", action="store_true", help="the one status line (no git, no GitHub: state.json only)")
    sta.add_argument("--open", action="store_true", help="open the status page (http://agentctl.localhost/) in the browser")
    sub.add_parser("watchdog", help="the invariant watchdog's pass (launchd every 60 s): W1 oldest unlanded change, W2 "
                   "gate red nobody holds, W5 processor beat, W6 escalations notified, WM maintain ran")
    rcv = sub.add_parser("recover", help="the recovery registry: each open non-green condition, its probe, its next check")
    rcv.add_argument("rcmd", nargs="?", choices=("status", "tick"), default="status",
                     help="status (default): list; tick: probe every open condition now (clears what passes)")
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
    dc = sub.add_parser("decide", help="answer a red WAITING ON YOU: the answer is stored, the red re-dispatched with "
                        "it (IN PROGRESS); if the action fails you are told")
    dc.add_argument("red", help="the red: its key, or its run number or id (as the status line / ci-why name it); "
                    "`vault-<ns>`: every red waiting on that vault (you are at the machine for their Touch ID prompts)")
    dc.add_argument("answer", help="your decision, or `done` once you ran the command it asked for")
    dc.add_argument("--repo", help="the red's repo when it is not the primary's (a run number is per repo)")
    rd = sub.add_parser("red", help="every red build held by a lease or offered: status / claim / beat / release / block",
                        description=RED_HELP, formatter_class=argparse.RawDescriptionHelpFormatter)
    rds = rd.add_subparsers(dest="rcmd", required=True)
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
    rr = rds.add_parser("release", help="drop your red lease")
    rr.add_argument("run")
    rr.add_argument("--any", action="store_true", help="release anyone's claim of it")
    rl = rds.add_parser("status", help="every red: its state, holder or offer, deadline")
    rl.add_argument("-v", "--verbose", action="store_true", help="also each red's attempts")
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
    for p_ in [rc_, rbt, rb, rop, rr, rdp, ra, rf, rfo, rl, rpb]:
        p_.add_argument("--repo")
    ram = rds.add_parser("am-fixer", help="exit 0 (and print who) inside a dispatched red fixer, else 1 (~/bin's "
                         "pre-commit hook asks)")
    iss = sub.add_parser("issue", help="file an agentctl defect (a fixer never edits ~/bin): stored, on the status page")
    iss.add_argument("text", nargs="*", help="what is wrong (none: --list)")
    iss.add_argument("--list", action="store_true", help="the open issues")
    iss.add_argument("--all", action="store_true", help="with --list: closed ones too")
    iss.add_argument("--close", metavar="ID", help="close an issue (the maintainer, once fixed)")
    iss.add_argument("--note", help="with --close: what fixed it")
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
    mt = sub.add_parser("maintain", help="THE reaper, every 15 min (launchd net.bitcashier.agentctl-maintain): the reap, "
                                         "the cache expiry (7 days), and once a day the sweep")
    mt.add_argument("--dry-run", action="store_true", help="list what would be reaped / swept, kill and delete nothing")
    mt.add_argument("--sweep", action="store_true", help="run the sweep now, whatever the hour")
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
    upk = sub.add_parser("unpark", help="release a parked worktree: the orphan rules apply again")
    upk.add_argument("worktree")
    rp_ = sub.add_parser("reap", help="the reap alone: orphans --kill --safe (stale JVMs included); --dry-run lists")
    rp_.add_argument("--dry-run", action="store_true")
    cf = sub.add_parser("config", help="the knobs: what applies and from where (default), `defaults` (a complete "
                                       "agentctl.toml), `doc [--write]` (AGENTCTL.md's reference)")
    cf.add_argument("ccmd", nargs="?", choices=("show", "defaults", "doc"), default="show")
    cf.add_argument("--all", action="store_true", help="show: every knob, set or not")
    cf.add_argument("--write", action="store_true", help="doc: regenerate AGENTCTL.md's config reference")
    a = ap.parse_args(argv)
    handler = {"maintain": cmd_maintain, "reap": lambda a: cmd_orphans(argparse.Namespace(kill=not a.dry_run, safe=True)),
               "red": cmd_red, "decide": cmd_decide, "config": cmd_config, "issue": cmd_issue, "revert": cmd_revert, "dispatch": cmd_dispatch, "gov": cmd_gov, "suite": cmd_suite, "checks": cmd_checks, "inbox": cmd_inbox, "recover": cmd_recover, "watchdog": cmd_watchdog, "lease": cmd_lease, "laptop": cmd_laptop, "status": cmd_status, "queue": cmd_queue_retired, "tick": cmd_tick, "store": cmd_store, "github-app": cmd_github_app, "waiting": cmd_waiting, "wait": cmd_wait, "state": cmd_state, "prepush-check": cmd_prepush, "cleanup": cmd_cleanup, "orphans": cmd_orphans, "supersede": cmd_supersede, "park": cmd_park, "unpark": cmd_unpark, "watch": cmd_watch, "sweep": cmd_sweep, "ci-why": cmd_ci_why, "land": cmd_land, "land-precheck": cmd_land_precheck, "env": cmd_env}[a.cmd]
    command = inbox_command(a)
    return with_inbox(command, handler, a) if command else handler(a)


def selftest():
    """Fast smoke check of the code a service would re-exec onto: the parser builds, the failure parsers and the
    snapshot readers work on fixtures, a fixture gate reads green."""
    snap = {"at": time.time(), "repo": "x/y", "outside_error": None, "outside": [],
            "gate": [{"name": "Main", "decided": {"databaseId": 1, "number": 1, "status": "completed",
                                                  "conclusion": "success"}, "newer": None, "since": None, "count": 0,
                      "runs": []}]}
    assert gate_from_snap(snap)[0][0] == "Main" and outside_from_snap(snap) == []
    assert set(check_failures("! pre-push sbt FAILED (3s)\n")) == {"sbt"}
    assert red_for("2026-01-01T00:00:00Z", dt.datetime(2026, 1, 1, 1, 5, tzinfo=dt.timezone.utc)) == "1h 5m"
    assert callable(recheck_local) and callable(restart_gate)
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            main(["ci-why", "--help"])
    except SystemExit as e:
        assert e.code == 0
    print("selftest ok")
    return 0
