# agentctl

`~/bin/agentctl` is the orchestration tool for Claude subagents working in the bitcashier repos
(`~/bitcashier/{infra,core,dashboard,invoice-page,version-dashboard}`) and in movies (`~/projects/movies`). All of
these repos have the remotes `forgejo` and `origin` (GitHub). THE FORGE SWITCH, `forge` in agentctl.toml
($AGENTCTL_FORGE), picks the one agentctl reads, lands and pushes through, `<remote>` on this page: `forgejo` (forge =
forgejo, the default until the cut-over to GitHub) or `origin` (forge = github). MOVIES FIRST (2026-10-31): while
bitcashier is still primary on Forgejo, `forgejo_owners = "bitcashier"` keeps `land`, `wait`, `ci-why`, `dispatch`,
`prepush-check` and `env` for its repos on forge forgejo (said once on stderr), and `store_auth = "store-tokens"` keeps
the store token for every command. It replaces the hand-written shell that agents kept getting wrong: poll loops,
fetch/rebase/push/verify sequences, log trawling, DB spelunking. It does that work in fewer, cheaper and safer tool
calls.

- Not on PATH: call it as `~/bin/agentctl`.
- Python 3, standard library only.
- Tests: `~/bin/agentctl suite` from the worktree (= `python3 tests/run.py` under a governor ticket of class `suite`,
  ONE run at once, `$AGENTCTL_SUITE_CAP`: parallel agents queue instead of piling up on the laptop). tests/run.py runs
  the suite in parallel workers (`-j N`, default min(cpus, 8)), each test under a timeout (`--timeout S`, default 60:
  the test FAILS with its traceback instead of hanging the suite), the whole suite bounded (`--suite-timeout`); about
  70 s on the laptop. `agentctl suite test_reds` runs the tests whose id contains a pattern. Mutation tests included.
  Run the touched modules while working and the full suite once, before landing.
- CI: `.forgejo/workflows/tests.yml` runs the same suite on ci-runner's container tier for every push to main (a
  fresh container: no HOME, sdkman or movies clone to lean on), and `.github/workflows/tests.yml` on a GitHub-hosted
  runner (Forgejo reads only `.forgejo/` when it exists). Green there is what the auto-pin reads: Forgejo's run, or
  GitHub's once ci-runner's agentctl-pin reads GitHub (infra branch agentctl-github).
- `agentctl --help` and `agentctl <cmd> --help` match this page.
- The design behind it: `~/bin/docs/landing-reconciler-design.md`. Phase 2 (the store and the reconciler on
  ci-runner): `~/bin/docs/phase2-runbook.md`.

## Standard agent workflow

```sh
eval "$(~/bin/agentctl env --print)"        # 1. sbt/java on PATH; a locked vault stops you here with one line
#    ... work in your own worktree, commit ...
~/bin/agentctl land                         # 2. rebase + guard, then QUEUED <id> in seconds: END YOUR TURN (verdicts -> inbox)
~/bin/agentctl wait run <id> &              # 3. Bash run_in_background=true, then END YOUR TURN
~/bin/agentctl ci-why                       # 4. only when land says the red gate refused, or Main is red
~/bin/agentctl inbox --unread --ack         # 5. your own verdicts (the orchestrator/owner: --ack --all), agent alive or not
~/bin/agentctl inbox wait --forever &       # 6. the orchestrator's notifier: Bash run_in_background=true (see "inbox")
~/bin/agentctl red claim <run> --note "…"   # 7. assigned a red build? claim it first (see "red")
~/bin/agentctl dispatch run <wf.yml> --ref <branch> -f k=v --purpose "…"  # 8. dispatching CI yourself? own it (see "dispatch")
~/bin/agentctl status                       # 9. what the reconciler holds: changes, the batch, reds, conditions
```

Every change lands with `land` (2026-10-26: no pre-merge CI). In movies `land` rebases, runs the squash guard, pushes
the change to `refs/heads/land/<id>` and prints `QUEUED <id> (<sha>)` at once (the owner, 2026-10-27: "agents never
wait on a land"). The reconciler then checks it, pushes it -- or, main red, holds it and lands it with the batch at
green -- and watches main's CI for it. Every verdict (`LANDED`, `WAITING`, `NOT LANDED` with the failing tail,
`WATCH OK / RED / UNKNOWN`) reaches `agentctl inbox`, naming your worktree. See "land".

Step by step:

1. Run `env --print` first in any session that builds or pushes. If it prints
   `vault <ns> locked: ask the user to run `! secrets unlock <ns>``, stop there and pass that line to the lead.
2. Work and commit in your own worktree. Never use `reset --soft` onto a moved main.
3. Run `land` from the worktree; it prints `QUEUED <id>` within seconds (or `NOT LANDED: <why>` for a dirty tree, a
   rebase conflict, the squash guard). END YOUR TURN at QUEUED. A NOT LANDED or WATCH RED of yours comes back to you
   through the inbox: fix forward (`Fixes-Red: <run>` for a red you caused) and land again.
4. To wait for a CI run, start `wait run` with the Bash tool's `run_in_background=true` and end your turn. You are
   re-invoked when it exits. Never sleep or poll in the foreground.
5. When main is red or the gate refused, run `ci-why`. It names the run, the cause (infra or test) and how the gate
   clears: a later green run, or a `Fixes-Red:` trailer.

## How it works: one reconciler, items and leases (2026-10-29)

The owner (2026-10-28): "the way it got red / blocked i want it to go green as well", "structural", "simplify the
pipeline / process / branches". Since reconciler phase 1 everything agentctl tracks is DATA in one store, and ONE loop
moves it forward; nothing is a detached process that can die with its state in its head.

- **The store** (sqlite, `~/.cache/agentctl/store.db`; `$AGENTCTL_STATE_ROOT`). Four tables: `items` (one row per
  tracked thing, with its state, deadline and a version every transition compares and sets), `leases` (who holds
  what, below), `events` (every transition and verdict, in order: `seq` is the cursor) and `offers` (open work a taker
  may take: a red waiting for a fixer).
- **Items and their machines.** Every state has a deadline and an automatic exit, so nothing waits forever:
  - `change` (a landed entry): queued → checking → checked → pushing → landed, or waiting (main red), rejected,
    superseded, cancelled;
  - `batch` (the changes held while main was red): blocked-red → assembling → checking → pushing → landed, bisecting
    on a failure, held after a stall. The batch is COMPUTED in a check tree at green; there is no `waiting` branch.
    While red it assembles only when a fix waits AND something in it may land on red now (a fix whose other
    production commits name no red waits for green); otherwise it is blocked-red, which holds no solo change;
  - `watch` (a landed sha): watching → ok / red / unknown (3 h);
  - `red` (a failure signature on main): red → offered → leased → fix-pending → verified / cleared, or
    blocked-external (an owner block). TOTAL: every red state ends in a dispatched fixer. A fix-pending whose fix is
    not proven within 6 h (`RED_FIX_GIVEUP`, `$AGENTCTL_RED_FIX_GIVEUP`), whose verify dispatch was refused, or whose
    fix is gone/None is offered again (escalated), its fix set aside (`unverified`). An old outside red no fresh run
    can check (no `workflow_dispatch`, a refused dispatch, its file gone) is never retired: it is offered with an
    `old_note` and a fixer decides. A red of a `workflow_call`-only workflow (only a Forgejo parse failure gives one
    runs of its own: Report run timings #38) is verified, in any open state, by its callers: its file changed on main
    since the failing run, and a caller's newest decided run on main containing that change passed (a red caller
    vetoes nothing). On ci-runner the reconciler's own clone is the repo these reads use. THE INVARIANT, enforced each
    tick (`red_enforce`, after every red's step): a red with no live lease, open offer, fix on its way or live block
    is offered again and the dispatcher gives it a fixer;
  - `condition` (a non-green state with a probe, the recovery registry): open → closed.
- **Leases.** Holding work is a lease: a name, a holder, an epoch, a secret, a TTL renewed ONLY on new progress, and a
  maximum hold. A holder that stops making progress loses the lease and the work is offered again; an old holder's
  late write is fenced out by the epoch. The landing lock is the lease `land/<repo>` (TTL 2 min, max 45 min), a check
  slot `slot/check/<repo>/<n>`, a change's check `check/<entry>` (10 min, max 3 h), a red's fixer or claim
  `red/<key>` (a fixer 15 min / 3 h, a claim 30 min / 8 h), an owner block a block lease renewed by its probe.
  `agentctl lease list [--all]` shows them.
- **The tick.** The processor (launchd `net.bitcashier.agentctl-waiting`, `agentctl tick`) runs one
  loop every 5 s, each step bounded: the change items (start an executor for a queued change, push a checked one),
  the batch, the watches, the `land/*` refs sweep, the lease archive, events → inbox, the notifier, the network and
  tunnel watches, the gate line, the red tick (observe, offer, dispatch), the conditions. It writes the snapshot
  `~/.cache/agentctl/state.json` each tick and re-execs onto agentctl's new code between ticks (only onto committed,
  self-tested code).
- **Executors.** Long work runs in a process holding a lease, never inside the tick: `waiting check <id>` (a change's
  checks), `waiting check-batch <id>` (the batch's check and bisect), a red fixer. The tick sees a dead executor as an
  expired lease and starts another.
- **Events → inbox.** Every transition writes an event; the projector turns events with an audience or a non-info
  severity into inbox entries (the format the orchestrator keys on); the notifier sends the escalations (one macOS
  notification each). Nothing in landing code writes the inbox directly.
- **The snapshot.** `state.json` is the only source of `agentctl status --line` (the status line) and `ci-why
  --cached`: no git, no Forgejo at read time.
- **Fixers are visible.** Every red fixer is a Claude Code background session: `claude agents` lists them by name
  (`red fixer #2432 · convergence takes-drift-1 (germany)`, `orphan · movies-wt-proxy-park`); `claude attach | logs |
  stop <id>` work on them (see "red").
- **The watchdog** (launchd `net.bitcashier.agentctl-watchdog`, every 60 s, `agentctl watchdog`) checks the invariants
  from outside the processor: W1 the oldest unlanded change is under 2 h old (counted from the red's start while main
  is red), W2 every red has a holder, an offer, a fix on its way or an owner block, W5 the processor beats, W6 every
  escalation was notified, WM maintain ran, WB nobody leaves uncommitted edits in ~/bin's own checkout for over 10 min
  (it names the processes working there), W7 (phase 2, the sync role) the sync has projected every event older than
  5 min, W8 no change left the queue in the last hour (landed, rejected, superseded, cancelled) without an event
  addressed to its author's inbox (Store.transition completes every such event; the entry carries the item id, so
  `agentctl inbox <item id>` finds it). A violation is a condition and a notification.
- **Upkeep** (the laptop's processor, at most hourly, in its own thread; log `~/.cache/agentctl/upkeep.log`): the
  standing-OK `cleanup --execute --no-remote` (clean, idle, proven-merged worktrees and their branches), each ended
  fixer's worktree whose red is closed (when proven merged and clean), and the test suite's `agentctl-*` temp dirs
  older than an hour. **WD, the disk**: under 50 GB free (`$AGENTCTL_DISK_MIN_GB`) the upkeep runs at once; still under
  after it, an INVARIANT condition whose probe is the same free-space check (it closes itself; it escalates only while
  it stays open). The owner, 2026-10-29: the Data volume had filled up with 109 merged fixer worktrees.

## Safety guarantees

- **Never `--no-verify`.** Every push argv is built by `push_argv()`, which asserts that no `--no-verify` or `--force`
  flag is present. `land` refuses to run with `PREPUSH_SKIP` set. A hook failure is reported, never bypassed.
- **Never unlocks the vault.** `env` only runs `secrets status <ns>`. When the vault is locked, it tells you which
  `!` command to hand to the user. Background code never calls `secrets` (it would prompt).
- **Never prints a secret.** Tokens are read inside the call that uses them and go only into a request header, a
  child's environment, or to the processor over a 0600 unix socket (see "tick"): never on a command line or on disk.
- **Never deletes unmerged work.** `cleanup` deletes only what is proven merged: the worktree's HEAD is an ancestor of
  <remote>/main, or every one of its commits is patch-equivalent there per `git cherry`. The tree must also be clean and
  idle. Every deletion is re-verified just before it happens and logged.
- **One push to main at a time.** Every push to main happens under the `land/<repo>` lease, after the pushed tip's own
  checks passed on the base it is pushed onto.
- **Every long wait is OWNED**: `wait` holds one flock per target under `~/.cache/agentctl/owners/`
  (`$AGENTCTL_OWNERS_DIR`), freed by the kernel when the process dies, with a record (owner, pid, start time, argv,
  cwd, code version, the CI runs it started). A second `wait` on the same target attaches; the runs a dead owner
  started are cancelled by the next owner. Owned processes re-exec onto agentctl's new code with the same pid.
- **No artificial load.** Polls back off up to 5 minutes. Nothing in agentctl burns CPU.

## Standing-OK cleanup scope

The user has standing-OKed `agentctl cleanup --execute` for exactly what it deletes:

- clean, idle (at least 2 h) worktrees of the six repos whose HEAD is merged into <remote>/main (ancestor, or all
  commits patch-equivalent);
- their local branches, but never `main` or `master`, and never a branch checked out elsewhere;
- remote `measure/*` branches whose runs have all finished, or that are more than 24 h old with no run;
- after `WATCH OK`, the worktree `land` ran from and its branch, when proven merged and clean (see "The lifecycle closes
  itself");
- remote branches `agentctl sweep` proves merged into main (not `main`, `master`, `measure/*`);
- remote `measure/*` branches whose owner is gone (its worktree removed or its recorded pid dead) and whose runs all
  finished (`agentctl sweep`; a `check/*` or `red-*` one is listed only);
- the reconciler's own `land/<entry>` refs once their change is decided (landed, rejected, superseded, cancelled) or
  unknown (the tick, every 10 min).

It skips locked trees, dirty trees, trees in use by a process, and trees under `~/.cache` (unless you pass
`--include-caches`). Every deletion is appended to `scratchpad/agentctl-deleted.log`
(`<ts> <kind> <repo> <name> <sha> <why>`). The sha makes each deletion restorable. Anything outside this scope still
follows "list before deleting".

---

## Subcommands

### status, lease, watchdog: what the reconciler holds

```
agentctl status [--line]          # the snapshot: the status line, then changes, the batch, reds, conditions
agentctl status --open            # open the status page, http://agentctl.localhost/ (below)
agentctl lease list [prefix] [--all]   # live leases and those freed in the last hour (--all: every one)
agentctl watchdog                 # one invariant pass (launchd every 60 s)
```

`status` reads only `state.json` (written each tick), and prints each item with its state and deadline and how long
ago the tick ran. `lease list` prints holder, epoch, expiry, max hold and the holder's note; leases free for over a day
are archived into the events by the tick, so the table stays small.

**The status page** (2026-10-29): the laptop's processor (the sync; launchd `net.bitcashier.agentctl-waiting`) serves a
read-only page at **http://agentctl.localhost/** (`agentctl status --open`). It binds 127.0.0.1:80; when port 80
cannot be bound or something already listens on it (Rancher Desktop's ssh forward holds `*:80` here, and a loopback
bind beside it would shadow it) it serves 127.0.0.1:3015 instead, logs that in the processor's log, and `status --open`
adds the port (it reads `~/.cache/agentctl/status-page.json`). `*.localhost` resolves to loopback in Chrome and curl
without /etc/hosts; Safari may not resolve it -- use http://127.0.0.1:<port>/ there. On the page, refreshed every
15 s from `/api/status` (JSON):

- header: the gate (each gating workflow's decided run, linked, and a newer one running), the reconciler's tick age
  (the remote store's `tick_at`), the store reachable or not and its latency, network and tunnel, the dispatcher on/off
  and fixer capacity (alive/slots), what is escalated now, and the status line;
- reds: every open red -- workflow, run (linked to Forgejo), failing jobs, machine state, age, holder and when its
  lease lapses, deadline, last note;
- agents: every red-lease holder and every fixer alive or ended in the last 3 h -- pid alive, worktree, tier, started,
  last progress (lease renewal and note), what its transcript last said, `git log -3`, files changed, and a tail of each
  log in its run's reds dir (collapsible) with a full view at `/log?path=` (only `.log`/`.out`/`.txt` files under the
  state root; anything else is 403);
- waiting/landing: the batch and the open change items with deadlines (past ones marked); open conditions; the last 50
  events.

Every source is read in its own thread with a bound: a failing one is an error in its own section, never the page's or
the tick's. It answers only Host `agentctl.localhost`, `127.0.0.1` or `localhost` (DNS rebinding), never calls
`secrets` (the store token is the one the sync holds) and never writes. `AGENTCTL_STATUS_PAGE=off` turns it off,
`AGENTCTL_STATUS_PORT` pins one port. Code: "the status page" section of agentctl; tests: `tests/test_status_page.py`.

**Live agent output** (2026-10-29, "I want full output"): every Agents row whose Claude transcript agentctl knows shows
its state (working / waiting on background / done / failed) and a **live output** button: a pane that streams the whole
transcript from its first line and follows it as it grows -- every assistant message, thinking, every tool call with
all of its input, every tool result in full (long ones collapsed, expanding in place, never cut), user, system and
queued-message entries, each timed -- with *follow* and *pause* toggles; *full screen* opens `/agent/<id>`. Below the
lease holders and fixers, the page also lists every other known session or subagent written in the last 3 h.

- Known agents (ids, never paths): `fixer-<run>-<n>` (reds/<run>/fixer-<n>.json's `transcript`, or its `log` when a
  .jsonl), `session-<uuid>` (a dispatch record's session, or a live lease holder's `log`), `task-<id>` (subagents, via
  `AGENTCTL_LIVE_GLOBS`, os.pathsep-separated; default `/private/tmp/claude-<uid>/*/*/tasks/*.output`). Only a regular
  `.jsonl` owned by you under `~/.claude/projects` (after symlinks) is ever streamed.
- `GET /stream?agent=<id>[&from=<offset>]`: Server-Sent Events -- `meta`, one `entry` per transcript line (id = the
  byte offset past it; a reconnecting EventSource resumes from Last-Event-ID), `synced` after the history, `state` on
  each change, `reset` when the file was truncated or replaced (shown again from 0). Bookkeeping lines (API request
  dumps, cost, mode, titles) are skipped. Bounded: 256 KiB read per step, 16 MiB per line (longer: an `oversize` entry),
  12 streams at once (503), a client that does not take a frame in 10 s is dropped.
- Masked (conservative): known token shapes (gh*_, github_pat_, sk-, xox*, AKIA, hvs., JWTs, PEM private keys), the
  value of a password/secret/token/api-key assignment or JSON key when it looks like a credential, Bearer/Basic/token
  headers, URL passwords. Everything else is shown as written.
- Read-only: GET/HEAD only (others 405), same Host check, 127.0.0.1 only. Tests: `tests/test_live.py`.

### store: phase 2, the store and the reconciler on ci-runner

```
agentctl store serve --listen ADDR:PORT --forgejo-url URL --allow-user U... --metrics-user U...   # ci-runner's unit
agentctl store use http://localhost:3014           # which store every agentctl process on this laptop uses (the default)
agentctl tick                                      # on ci-runner (AGENTCTL_HOST=ci-runner): the reconciler
```

Live since 2026-10-29 (the owner's steps: `~/bin/docs/phase2-runbook.md`). The store is served over HTTP on
ci-runner's :3014 (through the tunnel); every request carries the caller's own Forgejo token, checked with Forgejo.
Each process has one of TWO roles (owner decision 7: one writer, no laptop standby):
- `server` (AGENTCTL_HOST=ci-runner or AGENTCTL_ROLE=server): the database in-process; `tick` there runs changes,
  batch, watches, reds, conditions; checks on the CI tier; never the laptop's duties.
- `sync` (everywhere else): `land` submits there, events become inbox entries, escalations notifications, the state is
  mirrored into `state.json`, claims are renewed from the worktrees, and the fixer dispatcher takes the offers --
  fixers stay on the laptop, in `claude agents`. `Store()` is the remote store (`store.url` in the state root, else
  http://localhost:3014), so every command works unchanged. With the store down `land` prints `QUEUED-LOCAL` and the
  sync submits it later.

There is no laptop store any more (`store use --local`, `store import` and the laptop's own tick were removed
2026-10-29, docs/simplification-2026-10.md item 2). Disaster recovery needs no laptop copy: ci-runner keeps 24 hourly
backups (`agentctl-store-backup`), and a lost store.db is rebuilt from the land/* refs and Forgejo's runs (design §16).

### wait run: block on CI and print only the verdict

```
agentctl wait run <id>... [--repo R] [--until done|landable] [--tail N] [--interval S] [--timeout S]
```

(The lead's brief called this `run-wait`.)

- `<id>`: action_run.id (GITHUB_RUN_ID), not the number in the page URL. The legacy form `wait run movies 1234`
  still works.
- `--repo`: `o/r` or a short name (`movies`, `core`, `infra`, `dashboard`, `invoice-page`, `version-dashboard`).
  Default: the cwd's forgejo remote, else movies.
- `--until landable`: for an *Identity model convergence* run on a `measure/` branch, stop as soon as every
  `takes (<country>)` row has ended, not when the run's slow tail ends. It then prints each
  `identity-takes-<cc>/verdict.txt` and the collect command. For any other run it behaves like `done`.
- `--tail N`: the last N relevant lines of each failed job (default 8). Forgejo's per-step slices can be shifted a few lines off the stored log, so when a failed job's slice has no relevant line, agentctl reads the whole job log (`--log --job <id>`), `::error` annotations first. `ci-why` does the same. Strong markers (`*** FAILED ***` plus the 2
  lines after it, `[error]`, `::error::`, assertion, exit code) take precedence. Plain "failed/error" words are used
  only when a job has none, and never from INFO/DEBUG lines.
- Polling: 60 s, then x1.5 each time, capped at 300 s (`--interval` lowers the cap). `$AGENTCTL_MIN_INTERVAL` sets the
  first delay.
- Exit: 0 all green, 1 any red or cancelled, 2 timeout (default 6 h). A waiter whose holder vanished without a verdict
  takes over.

```
$ agentctl wait run 1960 --tail 5
run pawelkrupinski/movies#1960: failure; failed: run, timings
  -- run: last 3 relevant line(s)
     [info] - should match the checked-in read-model snapshot the page-test servers load *** FAILED ***
     [info]   Read-model snapshot mismatch — the fixture page-test servers would serve stale data.
     [info]   To regenerate after an intentional pipeline/fixture change:
  -- timings: no FAILED/error/assert line in its log (0-length log or setup failure? see `agentctl ci-why`)
```

Use it for any wait on a Forgejo run, in the background, followed by ending your turn. Don't use it for
`identity-measure` (use `--wait` there, or `--no-wait` with `--run <id>` to collect), and don't use it to find out why
something failed (use `ci-why`).

### wait file: block until a file appears

```
agentctl wait file <path> [--contains REGEX] [--interval S] [--timeout S]
```

It has the same lock, attach and exit semantics as `wait run`.

```
file /…/scratchpad/done.flag: present (2 bytes, mtime 13:02:11Z)
```

### ci-why: why main is red

```
agentctl ci-why [main|<run id>] [--repo R] [--tail N]
```

For movies it reads exactly THE GATE (the owner, 2026-10-27: "if the latest main and convergence runs are green, just
push then"): *Main* and *Identity model convergence*, the same two the pre-push red gate (`check_redgate`), land's
red/green branch and the `waiting` processor read. Verify scrape recording no longer gates. For each, it takes the newest decided run on main
(success or failure; cancelled and skipped runs are passed over). For other repos it reads every workflow seen on
main. For each red run it shows:

- the failed jobs and their relevant log lines;
- the commit and author;
- a verdict:
  - `TEST FAILURE`;
  - `INFRA-SHAPED`, with the `rerun --failed` command. Covers failures in a setup step, a 0-length log,
    `artifact already exists`, ENOENT, docker, disk or connection errors;
  - `[derived]` rows are no verdict of their own: a job Forgejo failed in "Set up job" without ever starting it
    (no runner time) because a job it depends on failed -- a reusable caller such as `leg (<country>)`,
    `verify (<country>)` or `timings`. Told apart by the workflow's `needs:` graph (read at the run's head) and the
    run's job results: `[derived] leg (poland): never started: needs: cascade of takes-drift-2 (germany)`. It opens
    no red: it is listed on the red of what it cascades from (`derived`), never takes or waits for a fixer slot,
    and clears with it. A setup failure with nothing failed upstream (a job that started, or no readable graph)
    stays `INFRA-SHAPED`, an infra red of its own;
  - `NO VERDICT`, when convergence's `verdict complete` is the run's only real failure (a takes or takes-drift row
    was skipped, cancelled or missing): reporting, not a test failure -- no red, no streak, no fixer; the gate reads
    the newest complete run. Beside another failed job, `verdict complete` is an `[incomplete]` row with no verdict of
    its own: the run's verdict and its reds are the other jobs' (an infra leg stays `INFRA-SHAPED`);
  - `UNCLEAR`, when a log has lines but no failure marker (truncated, or the runner was lost);
    also `LOG UNREADABLE`, when forgejo-gh could not serve the log (read twice; its error named): no log was read, so
    it is never a 0-length one (convergence #2473 was handed to a diagnose-only fixer as INFRA that way);
- a newer run in flight that may clear it;
- the clearing rule.

A gate red says how long it has been red (`red for 3h 0m (2 failed runs)`: from the oldest failure of the current
streak). For movies it then lists every workflow OUTSIDE the gate whose newest decided run on main is red, oldest red
first, each with how long and since when (the owner, 2026-10-27: a red nobody is shown gets ignored). Those never make
`ci-why` exit 1: they block no push. A `workflow_call`-only workflow's run that its callers proved fixed (its red
verified, the run settled) stays that workflow's newest run on main for good: it is listed apart, under `Proven fixed
outside the gate`, and left out of the status line (Report run timings #38 read "offering" there after its FIX OK).

`ci-why <run id>` explains one run. Exit: 0 green, 1 red (or a run explained).

```
$ agentctl ci-why
ci-why pawelkrupinski/movies: main is RED for the gate
Main: GREEN (run 1976 #968)
Identity model convergence: RED (run 1934 #945, failure); newer run 1988 (#973) in_progress may clear it
  verdict: UNCLEAR (a failed step logged no FAILED/error line: read `forgejo-gh run view 1934 --log-failed`; if infra, rerun: ~/bitcashier/infra/bin/forgejo-gh run rerun 1934 --failed -R pawelkrupinski/movies)
  run id 1934 (#945) http://forgejo.internal:3000/pawelkrupinski/movies/actions/runs/945
  commit 2f0fdcd3c Paweł Krupiński <…> 10-23 11:32: order-phases.sh: no mapfile -- the pre-push hook runs scripts under macOS's bash 3.2
  [derived] leg (germany): never started: needs: cascade of takes (united-kingdom) (through `needs:`; follows that red)
  [unclear] takes (united-kingdom): 8-line log with no FAILED/error/assert line (truncated? runner lost?) in step '…'; ends: …
  clears when: a later Identity model convergence run on main is green, or every pushed commit touching a production path carries `Fixes-Red: 945` (or `Fixes-Red: 1934`); tools-only commits land while red
Outside the gate (red, not blocking a push; oldest first):
  Verify scrape recording: RED for 5h 15m since 2026-10-26T23:49:43Z (1 failed run; newest run 3140 #2000) -- `agentctl ci-why 3140`
```

Use it when the red gate refused your push, or before you decide whether a red is yours. Don't read logs by hand
first. A genuine `Fixes-Red:` trailer is only for a commit that really fixes that run.

Under each red row (the gate's and the reds outside it) ci-why prints ONE line, `fixing: <state>`, the state
`red_state` decides for that red run -- the same function the red tick acts on, so ci-why, `--summary`, the status line
and `red status` can never disagree (2026-10-27: Identity capture #2133 was `UNASSIGNED (assigning ...)` in ci-why
while `red patrol` said "nothing to do", for ~50 min). Precedence: `fix landed in <sha>, awaiting #M` > a change
waiting / landing / committed (`<worktree> (..., waiting)`) > `BLOCKED ON OWNER` > `<claimant> (..., claimed) -- note`
> diagnosed > a notified author > a dispatched fixer before its claim (`dispatching`); with nobody on it:
`dispatching a fixer… (...)` (a gate red before the red tick's look, or outside the gate and due; normal weight),
`UNASSIGNED (outside the gate: a fixer is dispatched at HH:MM unless somebody claims it)`,
`UNASSIGNED (its fix <sha> failed ...: its fixer is resumed ...)`, or `NOBODY (skipped: <reason>)` -- dispatch OFF, a
cap, the daily budget, an escalation (bold NOBODY: only a truly unowned red, always with its reason). **Assign first**
(2026-10-27, "why is main red unassigned?"): the processor cycle that first sees a gate red runs the red tick's assignment
(an offer, then the dispatcher) BEFORE it writes the snapshot, so no published state holds an unassigned red; the
status line reads that snapshot (`state.json`) at every call. A red's state is its item's state plus its lease or
offer, never a guess.
agentctl's own trees (`<clone>-wt-check-N`, `-wt-waiting`, `-wt-batch`, the queue's clone, ~/.cache) are never a
fixer, owner or author: a queued change's owner is its entry's source worktree. `ci-why --summary` is the status
line's one line: `gate RED (main #2066 → fix 0a5767c awaiting #2070; convergence green)`.

### recover: every non-green condition clears itself

```
agentctl recover [status]    # each open condition: what, its probe, last check, next check
agentctl recover tick        # probe every open condition now
```

The owner (2026-10-28): "make this self-recover", "the way it got red / blocked i want it to go green as well",
"structural". Every non-green state -- STALLED, UNKNOWN, NETWORK, the gate UNREAD, an infra NOT LANDED, a watchdog
INVARIANT -- is a `condition` item in the store, opened only by `open_condition(kind, text, probe, on_recover)` with
the FAILED result of the check that found it (`run_probe`, no default). The tick re-runs that same probe (backoff
2^fails, at most 10 min; every condition at once when the network changes). When it passes the condition closes
(`condition.closed`), its actions run (dispatch a red's workflow to verify it, reset the tunnel state, lift a batch
hold, land again) and one `RECOVERED:` event is written. Unacknowledged past its deadline, a condition escalates
(an event of severity `escalate`, which the notifier delivers). Probes: workflow_green, http, tcp, red_resolved,
route, tunnel, gate_read, token, git_fetch, clone_health, infra_requeue, change_source, invariant -- none calls
`secrets`. A condition is probed on the host role that opened it (its `host`: the laptop's sync, or ci-runner's
tick); only the server escalates.
`tests/test_recovery.py` lints the source for a bypass. Owner blocks are block LEASES renewed by their probe (see
"always claimed"), not conditions.

An infra NOT LANDED (fetch / tunnel / network, a remote run unreadable, a stall) lands again when its check passes (it
rebases first; a conflict is a real NOT LANDED); a test failure, conflict or refusal stays with the author.

Decodo (2026-10-28): `Decodo probe` (movies, every 30 min + dispatch) CONNECTs through the proxy with the CI secrets;
Security headers' block is `red block <n> --cmd "Check the Decodo dashboard ..." --until-green "Decodo probe"`, so it
clears and Security headers is re-dispatched once Decodo answers.

### always claimed: no red is ever NOBODY (2026-10-28)

The owner: "why unclaimed ... should always claim, fix structurally".

- **A claim follows its red.** A claim (an agent's, a dispatched fixer's, a block) counts for every newer failed run
  of the same workflow and failing job in the current red streak whose first error line matches (or either line is
  unknown). A different line or job is a new red. A green run ends it, and so do a release or a dead claimant.
- **A committed fix counts only while it is live.** An unlanded `Fixes-Red:` commit counts for the runs it names (never
  by cause) and only while a land, a live claim or a fixer is in its worktree, or it is younger than 30 min. It stops
  counting once a later landed fix for the same runs or cause supersedes it, or once a fix landed since is refuted by a
  newer run. Otherwise it is a `stale` line (`abandoned: no process behind it, 14 h old`, `superseded by <sha>`), and
  `agentctl maintain` lists its worktree (and removes it only when proven merged).
- **A `Red-Unrelated:` commit is no fix.** A change landing on red under the owner's speed-up rule carries `Fixes-Red:`
  only to pass the gate; its `Red-Unrelated: <why>` trailer keeps it, queued or landed, from counting as the red's fix
  (so it never stops the red's fixer, nor is it refuted) and from making a red that passed by chance read as fixed
  (2026-10-28: four speed-ups landed this way stopped #2392's fixer, then went down as #2432's "4 fixes refuted").
- **A `Fixes-Red:` must touch what the red reads (G9, 2026-10-29: d21ac5e81 and 1548eb448, unit-test speed-ups,
  named 2390/2375/2392 while touching nothing their jobs read).** At land time (land / submit, the queue, the batch)
  and when the tick reads landed fixes, each `Fixes-Red: <run>` naming a known test red is checked against the paths
  its failing job reads -- its workflow job's steps, the reusable workflows and local actions it uses, the scripts they
  name (to three levels), the failing test's own sources -- conservatively. It does not count only when every path the
  commit changes is plainly unread: another workflow's file, documentation, android/ or ios/ (unless the job names
  them), or another spec while the job runs only named specs (`testOnly X`, no whole `test` task). Unsure (an unknown
  red, an infra red, `local`, no workflow found) counts. A trailer that does not count leaves the red where it is (no
  fix-pending, its fixer kept); the change lands as an ordinary one -- it waits while the gate is red -- and its
  author gets one `FIX NOT COUNTED: ...` inbox entry with the reason (a default `inbox wait` kind). Cached per sha
  under `~/.cache/agentctl/reds/fixcheck/`.
- **No daily budget.** A red that cannot get a fixer now (a concurrency cap, or dispatch unavailable) waits in a FIFO
  queue per class: `queued for a fixer (#k, next slot when <x> ends)`. That is a recovery condition (probe
  `fixer_slot`, on recover: the red tick runs). Gate reds keep their own cap of 3 alive.
- **Escalation is never NOBODY and never the owner.** After 2 failed fixers comes ONE investigation fixer (named in
  RED ESCALATE). After that come DEEP investigations (`claude --effort max`), again and again within the caps. Each one
  gets the whole history: the failing test and its error, every refuted fix, every fixer's verdict. A code red is
  never parked on the owner.
- **A spent quota is a wait, not the owner's** (2026-10-29: Record scrape fixtures #2426, OMDb's "Request limit
  reached"). A red whose key hit a spent quota (`QUOTA SPENT` in ci-why) is held by a wait lease
  (`block/cause/quota-<service>`, holder `wait:quota`): no fixer, no owner block. After the quota's window resets
  (OMDb: midnight UTC) its workflow is dispatched on main once; the wait ends when a run since got past its preflight
  (one OMDb call). Only an invalid key (`KEY REFUSED`) is the owner's.
- **BLOCKED ON OWNER is only for the owner's causes.** `red block` needs `--owner-cause` (external-account,
  credentials, infra-apply, deploy, billing, dashboard), and it refuses a TEST FAILURE red and a red with a live
  fixer. Every block states what fails, what was tried, why only the owner can act, the exact `!` command, and how it
  auto-clears; `ci-why` shows all of it, while the status line shows a short form with a pointer. Every red tick
  checks each block again (the condition's probe is `owner_block`, which ANDs in the block's justification). A block
  that lost its cause, its command, or its non-test verdict, or whose red has a live fixer, is dropped at once.
  A live fixer outranks a block. A red with 3 or more refuted fixes is `recurring: N fixes refuted`, and its fixer is told to find the root
  cause, with those commits listed.
- **One outage is one condition.** `red block <n> --cmd "..." --cause Decodo --until-green "Decodo probe"` opens it, and
  later reds attach with `--cause Decodo`. When the probe passes, every attached red is released and its workflow
  dispatched to verify it. The status line shows one line: `Decodo down → blocks: A, B (auto-clears when …)`.
- A claim or a block re-checks the red_resolved conditions at once: RED UNCLAIMED clears without waiting for backoff.

### red: who is fixing each red build, and a fixer for one nobody fixes

```
agentctl red claim <run> [--note "<what>"]     # an agent assigned a red runs this FIRST
agentctl red block <run> --cmd "<command>" [--cause <outage>] [--until-green <workflow> | --until-http <url> | --until-tcp <host:port>]
                                               # it waits on the owner: BLOCKED ON OWNER (assigned, shown loudly) until
                                               # its probe passes (default: the red's own workflow goes green)
agentctl red beat <run> --note "<what>"       # renew your lease by hand (a new note is progress; in a dispatched
                                               # fixer only its note: its wrapper renews the lease)
agentctl red release <run> [--any]
agentctl red status [-v]                       # every red: its state, holder or offer, deadline
agentctl red dispatch <run> [--force]          # the red's offer at top priority: the dispatcher takes it now
agentctl red auto [on|off|status]              # the processor's automatic dispatch
agentctl red owner-push <run> [--note "<what; which units restart>"]
                                               # an owner-only repo's (infra's) fixer files its prepared branch
claude agents                                  # every dispatched fixer, by name (attach / logs / stop <id>)
```

**EVERY WATCHED REPO, NOT ONLY MOVIES (2026-10-30, ci-failures recommendation 4: infra's Stage NixOS closures was red
29 h and core's Main Pipeline 21 h with no fixer).** `~/bin/red-repos.json` (`$AGENTCTL_RED_REPOS`) lists the repos
the red machine watches, each with its gate workflows, its `family` and its `push` policy:

| repo | gate | family | push |
|---|---|---|---|
| pawelkrupinski/movies (the primary) | Main, Identity model convergence | red-families.json | land |
| bitcashier/infra | Stage NixOS closures | infra-repo | **owner-only** |
| bitcashier/core | Main Pipeline | core-repo | land |

- The primary keeps everything it had (its keys, outside workflows, local checks, its landing gate `GATING`); the
  others are read through their gate workflows only, once a minute (`$AGENTCTL_RED_OTHERS_EVERY`), after the
  primary's red tick, each in its own try. Watching a repo puts NO red gate in front of its landings.
- Another repo's red carries `repo`, its key is bound to the repo (`red_repo_key`), its records live under
  `reds/<repo>-<run>/` (run numbers are per repo), and it shows as `infra Stage NixOS closures #3455` everywhere. Only
  its own repo's tick observes, steps or clears it. A run number given to `red claim|block|owner-push|dispatch` is the
  current directory's repo (or `--repo`); a key is global.
- Every red of a repo is in its `family` (one fixer per family, red_family_holder). Its fixer works in a worktree of
  that repo's `clone` (`<clone>-wt-red-<run>`); no clone on this machine: the offer says so and waits.
- The fixer brief (`docs/fixer-brief.md`) has a `### REPO <owner/name>` section per repo under "Per-repo rules"; a
  fixer gets only its own repo's section.
- **owner-only (infra): the fixer never pushes or deploys.** Its prompt overrides every "land it", its session denies
  `agentctl land`, `git push`, `bin/deploy`, `forgejo-bootstrap`, `nixos-rebuild` and `switch-to-configuration`, and
  `agentctl land` itself refuses inside such a fixer. It prepares the fix on its local branch and runs
  `agentctl red owner-push <run> --note "..."`: the red goes to fix-pending (fix status `owner`), one escalation
  `RED OWNER PUSH: ... `! git -C <wt> pull --rebase <remote> main && git -C <wt> push <remote> HEAD:main`` reaches the
  inbox, a green gate run verifies it (FIX OK), the reminder repeats at each deadline, and unproven after
  `RED_FIX_GIVEUP` (6 h) it is offered again -- the next fixer continues in the same worktree.

A claim is a LEASE (`red/<key>`, 30 min, renewed by your worktree's activity or a new note, at most 8 h); a fixer's is
renewed by its session's transcript and worktree (15 min, at most 3 h). A lease that lapses offers the red again.

The owner (2026-10-27): "i want gate status to point to agent(s) fixing the red build(s)", then "if a build is red, an
agent must be assigned to it".

- **One cause, many reds.** A red is keyed by its failure SIGNATURE: workflow + failing job + its first relevant log
  line, normalized (numbers, shas, 0x hex, timestamps, temp paths out). The first relevant line: a STRONG line of the
  failed step's slice, else the job's own `::error` annotations (its whole log), else the slice's weak lines. Cached
  signatures carry the classifier version (patterns and line choice); stale ones are read again, a few streak runs per
  tick, and a red keyed by an older classifier whose run now fails under another red's key is MERGED into it (runs,
  history, refuted fixes: a `Fixes-Red:` link carries over). A newer run failing the same way is the same
  red: a claim or a `Fixes-Red:` naming any run WITH THAT SIGNATURE still counts. A red's run names are only the runs
  that failed with its signature, never the whole streak, and a claim counts for the signatures it names: a fix for
  job A never counts for job B (2026-10-27: `Fixes-Red: 2104`, a `capture (uk)` fix, was shown as a failed fix of
  #2133's `publish`). One fixer per red run covers all its signatures.
- **Fixing it** is any of: a claim (`red claim`: the claimant is a dispatched fixer's name from its session's own record,
  else the Claude session, else the worktree -- never an inherited `$AGENTCTL_AGENT_NAME` inside a session; its pid the Claude Code process); a worktree with an unlanded commit carrying `Fixes-Red: N`
  (committed; patch-equivalent on main counts as landed); a running `agentctl land` from such a worktree (landing); a
  `waiting` entry carrying one (waiting); such a commit on main whose next run is still in progress (landed, awaiting
  #M; if a run containing it fails the same way, the fix did not fix it and is shown so).
- **Every fixer is a Claude Code background session.** `claude agents` lists them by name (`red fixer #2392 ·
  convergence takes-drift-2 (united-states)`, `orphan · movies-wt-proxy-park`); `claude attach | logs | stop <id>` work
  on them. Its wrapper tracks it by that id, renews its lease by its transcript and worktree, takes its verdict from its
  final answer and `claude stop`s it when the lease goes (`claude agents --all` keeps it listed, `attach` reopens it).
  A store that does not answer (`StoreUnavailable`: the tunnel to ci-runner reset a request) decides nothing: the
  wrapper asks again at its next poll, and its release up to `RED_RELEASE_ASKS` times (2026-10-29, #2517: one reset
  ended the wrapper a minute in, its lease lapsed with nobody renewing it, and a second fixer was dispatched onto the
  red the first was still fixing).
- **Heartbeats.** A claim lives while its process lives (a dispatched fixer: while its transcript grows), or while its worktree
  commits or moves. 30 min with none (`$AGENTCTL_RED_STALE_MIN`): STALE, counted for nobody, removed by `agentctl
  sweep`, and the red can be taken over.
- **Assigned at the moment of red (2026-10-27: "why is no one assigned to the red? root cause structural fix").** The
  snapshot cycle that first sees a gate red runs the red tick at once, and it assigns the red then:
  - every commit of the red run since that workflow's last green run is attributed to its worktree (landed queue
    entries, `LANDED` inbox entries, HEAD / reflog);
  - a LIVE author (a pid alive -- the queue entry's or LANDED entry's Claude pid, or an agent process in the worktree --
    AND git or inbox activity within 15 min, `$AGENTCTL_AUTHOR_ACTIVE_MIN`) gets a claim `author <worktree>, notified,
    until HH:MM`; each live author gets a `RED AUTHOR` inbox entry (a default `inbox wait` kind), one of them ASSIGNED;
  - an author who neither commits nor claims within 10 min (`$AGENTCTL_AUTHOR_WINDOW_MIN`), or whose session ends, is
    dispatched past, and is never assigned that red again;
  - no live author: a fixer is dispatched NOW (one per red run, whatever the number of authors);
  - an infra-shaped red (every failed job `[infra]` or UNCLEAR: runner lost, truncated log) skips the authors and gets
    an infra-diagnosis fixer at once.
- **No silent skips (2026-10-27).** `red status` prints every red's state line (as ci-why words it) after its
  actions. A red nobody fixes that the red tick cannot put anyone on -- gate or outside -- gets the reason written into
  its record (`NOBODY (skipped: <why>)` in ci-why and the status line), a `SKIPPED` line in dispatch.log, and ONE
  `RED UNCLAIMED: <workflow> #N [jobs]: NOBODY -- <why>` inbox entry per spell, escalated after 10 min unread (see
  "inbox escalated"). `ci-why` (no dispatch) still writes
  it for a red nobody fixes for 10 min (`$AGENTCTL_RED_UNCLAIMED_MIN`), and reds outside the gate keep that wait.
- **Dispatch (the processor's red tick).** A red with nobody on it is an OFFER; the dispatcher takes offers (gate
  reds first, within the caps) and each taken offer is a LEASE its fixer holds. The fixer is a Claude Code background
  session, `claude --bg -n "<label>"` (`claude agents` lists it by that label), in a fresh worktree
  `~/projects/movies-wt-red-<run>` off <remote>/main (a fixer never edits ~/bin: `agentctl issue`), briefed with
  `~/bin/briefs/movies-agent-preamble.md` to read, then `docs/fixer-brief.md` word for word in its prompt (THE fixer
  brief, the single source of every fixer's and family owner's rules: fast reproducer first, mechanism-only identity
  fixes, no allow-list or baseline edits, land as you go, `agentctl issue`, bounded commands; `$AGENTCTL_BRIEF_SRC`
  names another file only to try a wording out), and the run, its jobs and first error line. An `[infra]` red gets a DIAGNOSE-ONLY brief (no code change; runners, ci-runner, owner actions). The fixer
  says who it is first (`red claim N --fixer <its record>`, in its prompt: that binds its session; background sessions
  inherit the claude daemon's environment, so a fixer is never known by its environment), notes what it does on its
  lease (`red claim` of its own red or a sibling under its lease), claims any OTHER red with `red claim <M>`, keeps its record under `~/.cache/agentctl/reds/<run>/`, ends at `LANDED` (or `GAVE UP:` /
  `BLOCKED ON OWNER:` / `DIAGNOSED:`), and its verdict reaches the inbox (`RED FIXER <workflow> #N: ...`, kind
  FIXER_LANDED or FIXER_FAILED; `RED DISPATCHED` when it starts). Caps: one fixer per red; 3 h wall time each
  (`$AGENTCTL_RED_FIXER_WALL`). A red still failing after 2 dispatched fixers: one `RED ESCALATE` entry, never
  dispatched again. Every decision is a line of `~/.cache/agentctl/reds/dispatch.log` with its reason.
- **One fixer per red family; continuity; land as you go (2026-10-29).** `~/bin/red-families.json` names families
  (convergence: the Identity model convergence workflow and any `convergence (<cc>)` job): while a family's fixer is at
  work, a new red of it goes under its lease (`[family convergence]` in `red status` and on the status page) and a line
  in its worktree's `RED-INBOX.md` tells it. A live fixer stays its reds' holder (its lease is taken again for it if it
  lapsed); no second fixer while a previous one of the red, its run or its family works, and a new one goes on in the
  previous one's worktree when that holds unlanded commits. Fixers land each verified step; commits unlanded 45 min
  get `RED-NUDGE.md`, 90 min a `RED ESCALATE`. A queued Fixes-Red change holds its red ("fix waiting to land", "blocked by
  gate <red>" while main is red) and verification counts from its landing. The fix follows its WORK, not its change
  id (2026-10-31, Main #2877): the store's change item is the truth (a stale entry file never holds a red), and a fix
  whose change was cancelled / superseded / rejected follows its worktree to the later change that carries it (waiting
  on it; verifying once it lands) or its commits found on main by content; with neither, the red is offered again
  `$AGENTCTL_RED_FIX_REQUEUE_GRACE` (15 min) after the cancel. A fix of the gate's OWN red (a gate run's
  red carries its key, whichever run its Fixes-Red names) reads "landing on red (Fixes-Red), <queued|checking|...>" in
  `red status`, `status --line` and the status page, never "blocked by gate" that same red (2026-10-29 19:20).
- **LONG-LIVED FAMILY OWNERS (simplification item 6, 2026-10-30).** A family marked `"owner": true` in
  `red-families.json` (today: `convergence`, `main-tests` = Main's test failures, `infra` = every other infra-shaped or
  unclear red) has ONE persistent owner instead of a fresh fixer per red: one `claude --bg` session (`red owner ·
  <family>` in `claude agents`) and its wrapper (`agentctl red owner --family F`, detached), working in ONE worktree
  (`<clone>-wt-owner-<family>`, branch `owner-<family>`) with a NOTEBOOK there (`RED-NOTEBOOK.md`, never committed)
  that it reads first and keeps current. Its lease `owner/<family>` is a HEARTBEAT (renewed while the wrapper lives,
  lapsing 15 min after it dies; no 3 h maximum). New reds of the family are handed to it -- their leases taken for the
  owner (kind `owned`), a line in `RED-INBOX.md` -- and become the NEXT TURN of the same session (`--resume`). A turn
  ends with one verdict line; that turn's red leases are released with it, exactly as a fixer's, so verification,
  re-offers (back to the same owner), the attempt ladder and the crash-loop breaker work as before. It is restarted
  only when it died (same worktree, notebook and session), and ends after 6 h with no red (`$AGENTCTL_OWNER_IDLE`), on
  `RED-STOP.md`, or after 2 turns in a row with no verdict. Its brief: fast reproducer first, and for convergence the
  MECHANISM-ONLY rule (fix the incremental state that diverges from a whole resolve; never a resolver rule tuned on one
  recording, never a baseline or allow-list row). `red status` lists each owner (`owner <family>: ALIVE, turn n, ...`).
  Records: `<reds dir>/owners/<family>/` (`owner.json`, `owner.log`, `leases.json` 0600).
- **ONE RED QUEUE (simplification item 5, 2026-10-30; replaced the per-class caps, the borrow and the overflow slot).**
  A red is (workflow, failing job, signature); its cascade jobs are derived. Every red belongs to ONE queue family:
  its configured family (`red-families.json`), else its run (`run:<workflow>#<number>`). The open offers are one
  priority queue -- the gate's first, then a fix's, then outside reds, flakes last; a test failure before an infra one;
  then the oldest -- and the dispatcher keeps EXACTLY ONE FIXER PER FAMILY: a family at work takes each new red under
  its fixer's lease, a family with none gets one fixer (its other open offers taken with it) while fewer than 5 fixers
  are alive in all (`$AGENTCTL_RED_FIXERS`). One family never holds two slots, so the gate cannot starve the rest, nor
  the rest the gate. An offer waiting 45 min with no slot (`$AGENTCTL_RED_STARVE`) is `STARVED` and escalates at once
  (`RED STARVED: ...`), then every cycle; no running fixer is killed for it. The wait reads, in `red status`, `ci-why`
  and the status page: `capacity 5/5, waiting 12 min; next slot when <fixer> ends by 15:09`; `fixers alive: n/5`.
  The old knobs `$AGENTCTL_RED_GATE_MAX_FIXERS`, `$AGENTCTL_RED_MAX_FIXERS`, `$AGENTCTL_RED_OVERFLOW_FIXERS`,
  `$AGENTCTL_RED_CAPACITY_BORROW` and `_WINDOW` are no longer read. The store's open reds were migrated in place
  (meta `red_model` 2: each red's `signature` and `family`), once, by the tick.
- **Fast reproducer first (the owner's standing rule, 2026-10-29).** Every dispatched fixer's brief says: before
  fixing, build the fastest faithful reproducer -- a pinned minimal fixture or slice replayed by one spec on a warm sbt
  server (identity flips: a slice from `scripts/identity-slice`, then `scripts/sbt-warm 'worker/testOnly
  scripts.IdentityDriftSpec -- -z "<case>"'`), else a CI rerun of only the failing job or row
  (`scripts/identity-drift-ci --for-red <run>`); it fails before the fix and passes after; iterate on it, the full
  suites give only the final verdict.
- **Nothing stops a gate fix, ever (the owner, 2026-10-31).** The dispatcher never holds a fixer for laptop
  pressure, and a GATE red's fixer starts past the slot cap (`$AGENTCTL_RED_FIXERS`; it still counts toward it, so
  outside reds wait while gate fixers fill it). A red fix's governor ticket (class `red`) is admitted at once, whatever
  the budget, free memory, pressure or load. What still stops a dispatch is functional, not a throttle: dispatch OFF,
  no usable `claude` CLI, the red offered to its author, or the crash-loop backoff below. `agentctl red status` states
  the invariant on its `dispatcher:` line and shows the laptop's reading on a `laptop:` line. (2026-10-31: two movies
  reds sat `waiting: laptop pressure (swap 17.7 of 19.0 GB used)` for want of a fixer at 58% free and load 5.)
- **Laptop pressure, read as it is now: it holds other work only.** macOS keeps pages swapped out hours ago counted as
  swap USED until touched, so swap's level is never a signal. The signals: macOS's memory free % (`sysctl
  kern.memorystatus_level`, the number `memory_pressure` prints; Linux: MemAvailable of MemTotal) under 20%
  (`$AGENTCTL_LAPTOP_FREE_PCT`); the kernel's pressure level (`kern.memorystatus_vm_pressure_level`: 1 normal, 2 warn,
  4 critical) at 2 or over (`$AGENTCTL_LAPTOP_PRESSURE_LEVEL`); the 1-min load over 2x the cores
  (`$AGENTCTL_LAPTOP_LOAD`); swap GROWING by 1 GB (`$AGENTCTL_LAPTOP_SWAP_GROWTH_MB`) within 10 min
  (`$AGENTCTL_LAPTOP_SWAP_WINDOW`; samples in `<gov>/swap-samples.json`). The governor holds every class but red on
  it (`laptop pressure: <why>`; the load there is each class's own factor); `agentctl gov status` prints
  `pressure: <why with numbers>` or `pressure: none [memory 58% free, level 1 (normal), load 5.0/14 cores, swap 17.7
  GB (+0.0 GB in 10 min)]`.
- **Launch errors are said, never "never got going" alone (2026-10-31, core #277: 9 fixers in a row ended
  `claude --bg` exit 1 "Workspace not trusted").** `claude --bg` starts only in a workspace the owner trusted, and
  claude keys that trust by the repo's MAIN checkout (a worktree of ~/projects/movies is trusted through it; a parent
  like ~/bitcashier does not count). The dispatcher reads `~/.claude.json` (never writes it): an untrusted repo's red is
  not launched, its offer reads `NOT DISPATCHABLE: claude does not trust <repo> ... owner, once: ! cd <repo> && claude`
  (one inbox line), and it dispatches the tick after. A backed-off red's offer ends `; launch error: <the actual
  error>`. A session that in `$AGENTCTL_FIXER_CLAIM_WITHIN` (10 min) never started its transcript ends `NOT STARTED:
  session <id> never claimed ... (`claude agents`: state ...)`; one that works but never ran `red claim` is reported
  once (lease note + inbox) with its last words.
- **Crash-loop breaker (G9, 2026-10-29: in E9 fixers were launched 52 times over 9 reds, #2335 18 times, mostly
  `exit 1 with no LANDED / GAVE UP line`).** 3 dispatched fixers in a row on one red that end with NO RESULT -- a
  crash, an exit with no verdict line, a lease lost or expired without progress, a time-out, a start that never got
  going -- and that tier is not dispatched again: ONE escalation (`RED ESCALATE: ... CRASH LOOP ...`, each launch's
  outcome and log tail in it) moves the red up to the deep tier. When the deep tier crash-loops too (or it was already
  there), the red is BLOCKED ON OWNER with cause `fixer-crash-loop` -- the one owner block a test red may hold, since
  the fixers, not the code, fail -- and it clears itself: its probe passes when a newer decided run of the workflow no
  longer fails this way (or is green), and it lapses after 24 h (`$AGENTCTL_RED_CRASH_BLOCK_MAX`), offered again with
  the streak counted afresh. A launch with a result (LANDED, NOT LANDED, GAVE UP, BLOCKED ON OWNER, DIAGNOSED, a
  refuted fix) ends the streak; a release by hand is no launch. `$AGENTCTL_RED_CRASH_LOOP` (3).
- **Refunds.** A dispatch that did nothing is not counted (`red status` says `[refunded: ...]`): `GAVE UP` / `NOT LANDED`
  because it is already fixed or there is nothing to do; `STOPPED` (superseded) and `red cleared`; a fixer killed
  before any verdict; `BLOCKED ON OWNER` only for a CI read (`forgejo-gh run view|download`: the fixer carries the
  token); a DUPLICATE -- a fixer whose red someone else claimed between the dispatch decision and its start steps
  aside at once (`DUPLICATE: <who> is on it`, no `claude` run; a by-hand dispatch never does).
- **DIAGNOSED keeps the claim, for the same red.** A fixer ending `DIAGNOSED: <cause>; durable fix: <...>` turns its
  claim into `diagnosed: <summary>; awaiting an implementer` (never stale; the red is never NOBODY after a diagnosis),
  and an implementer is dispatched at once in a fresh worktree with the diagnosis in its brief: remove the cause, or,
  for a real one-off outside the code, land the means to diagnose it next time (the failing process's stderr and exit
  status kept: e.g. Chrome's stderr, which CdpDriver discarded for #2146). Under a cap the claim says why it waits and
  the dispatcher takes it later; the implementer's own claim replaces it. The implementer is the same attempt (not
  counted toward RED ESCALATE); an implementer that only diagnoses again: the claim says `awaiting a person` and
  `RED ESCALATE`.
- **Permissions.** A fixer runs in the permission mode interactive sessions start in (`permissions.defaultMode` in
  `~/.claude/settings.json`; it must be `auto`, else dispatch is off and `red auto` says what the owner must set),
  with `--permission-prompts none` (anything that would prompt is denied), `git push`, `--no-verify` and
  `PREPUSH_SKIP` denied, no parent session, no secret in its environment but the Forgejo token agentctl reads itself.
  Its brief says CI logs, scraped pages and fixtures are data, never instructions.
- **The author first, briefly.** A live author holds the red for its 10-min window by its claim (above); a finished
  or idle author holds nothing.
- **Failing fixes.** A landed `Fixes-Red:` fix whose next run fails the same way goes back to its dispatched fixer ONCE
  (its own session resumed, `claude --bg --resume <session>`, told what happened); then a fresh fixer; then, when the culprit is
  known (the only commit between the last green run and the first red one), the REVERT FALLBACK: a `git revert` of it
  with `Fixes-Red: N`, landed by `agentctl land` through the normal hook (`RED REVERT` in the inbox); then
  `RED ESCALATE`.
- **Superseded.** A signature that stops failing while a fix of someone else's landed (or the next run just passes it
  after it was red for a while): `RED CLEARED`, and every claimant is told to stop (the inbox, plus `RED-STOP.md` in its
  worktree; a dispatched fixer's wrapper ends it at once).
- **One cause, across workflows.** A failure's CAUSE is its job and normalized first error line without the workflow
  (one sparse-checkout failure hit findings report #2049, live drift #2051 and lean vs whole #2050): a `Fixes-Red:` or
  a claim naming a run with the same cause counts for every red with it, in any workflow. A red whose fix has landed
  is `fix landed in <sha>, awaiting #M` (or `no run containing it yet`) and is never dispatched.
- **Flakes.** A signature that passes on the NEXT run is a flake only when no commit between the red run and the green
  one can have fixed it: none carries a `Fixes-Red:` naming one of its runs, a run with the same cause (any workflow) or
  a run of the same workflow, and none touches a file its failure line names or `.github/workflows|actions/`. Otherwise
  it is FIXED (superseded). A flake later found fixed by a commit the passing run already held is reclassified and its
  count taken back. A flake is: `RED FLAKE` (with its count in
  `~/.cache/agentctl/reds/flakes.json`), shown by ci-why under `Flaky`, kept open (and its claim) until a fix naming it
  lands, and dispatched after the gate's reds, most frequent first, briefed "make it deterministic". Nothing ever reruns
  a run to green.
- **Main's own local checks.** When a change's pre-push dry run fails, land runs the same checks on plain <remote>/main's
  tree (`git commit-tree main^{tree} -p <head>`, `--checks-only <head>..<that>`). Failures that main shares (same check,
  same normalized lines) are main's own red: `KNOWN RED: … not held against this change`, recorded in
  `reds/local.json`, shown by ci-why as `Local checks` (and in the status line), claimable as `red claim local`,
  dispatched like any red; a later passing run of those checks clears it. That run's hook output is kept as
  `reds/baseline-out/<main sha>`, which movies' pre-push reads through `$PREPUSH_BASELINE_DIR` (set by agentctl): land
  runs the dry run once more, and the hook passes a check that fails only as it fails on plain main (`KNOWN RED: <check>
  ...`), its pass stored, so the push reuses it. A change that fixes main's red just passes. Fail-closed: no baseline
  for the push's base, and every failure blocks as before.
- **A network failure is infra, never a known red** (2026-10-29 ~18:50: during ci-runner packet loss #823's land
  recorded `Local checks` as a KNOWN RED). When the dry run's failure and its baseline on plain main both carry a
  network signature (`fetch-gitops ... unreachable / could not fetch`, `GitOps manifests STALE`, git's `unable to
  access`, `Could not resolve host`, `ssh: connect to host`, connection reset/refused/timed out, DNS failures), nothing
  is recorded (no `reds/local.json`, no baseline) and the change goes to the land queue as an infra re-queue
  (`QUEUED <id> (<sha>): infra, not a known red -- ...`, checked again after the infra back-off). Its queued check
  failing on the network again is re-queued for the same reason; the third time (or 30 min) opens the change's
  `STALLED` condition, which clears when it gets past it.
- **A stale local red.** `reds/local.json` is keyed to the main sha it was computed on. Once <remote>/main moves past it
  the record is shown as `Local checks: stale, rechecking` (never RED, no unit, no dispatch) and the failing checks are
  run again on the new main (the hook over <recorded main>..main, other checks skipped, governor check class): in the
  background by the processor's red tick, or by a land's passing dry run. A pass clears the
  red, releases its claims, stops its dispatched fixer (process groups) and writes `FIX OK` to the inbox; the fixer's
  worktrees whose commits are patch-equivalent on main (`git cherry`) are removed, any other is an inbox `RED LEFTOVER`.
- **One CI-state snapshot.** The processor reads every main workflow once per cycle into `state.json`'s `ci`; the
  gate verdict, the outside-the-gate reds, the published summary and the red tick read it (older than 3 min: unread,
  never green). `ci-why --summary` is one line: `gate green · reds: Identity capture
  #2104 → drift; local → stale, rechecking` or `gate green · no reds` (at most 3 reds, then `+N more`).
- **Services re-exec only onto good code.** `restart_if_changed` needs the file to equal ~/bin's HEAD version (a dirty
  edit is never picked up) and HEAD to pass `py_compile` and `agentctl --selftest`; otherwise the old code keeps running
  and one inbox entry `CODE REFUSED` names the commit.
- **`Fixes-Red: N` is the run NUMBER** (#N, Forgejo's index): resolved through `actions/runs?run_number=N` first,
  as a run id only when no run has that number; a number and an id naming different runs: the number, and the FIX
  verdict says so in `[...]` (2026-10-27: `Fixes-Red: 2133`, Identity capture #2133, was read as id 2133, an old
  convergence run #1111: FIX UNVERIFIED, and capture never dispatched).
- **A fix for a red outside the gate** (`Fixes-Red: N` naming e.g. an Identity capture run) is verified by the watch at
  once: the workflow is dispatched on main (workflow_dispatch, default inputs), waited for, and `FIX OK` / `FIX FAILED`
  goes to the inbox, the claim is released and the snapshot refreshed. One verification at a time per workflow (a second
  fix landing meanwhile gets none), counted in the daily dispatch budget. No workflow_dispatch: `FIX UNVERIFIED until
  next scheduled run (cron ...)`.
- Headless fixers carry the Forgejo token (fixer_env); CI reads never block on the owner, `BLOCKED ON OWNER` is for real
  owner actions only.
- **An unreadable gate** (Forgejo, the tunnel, the vault or the token down): the red tick dispatches nothing, writes ONE
  `UNKNOWN: the red patrol cannot read the gate (…)` per spell with an infra check (ports 3000 / 2222, the token),
  and pushes fail closed (an unread gate is red).
- **Nobody awake.** The launchd processor ticks and dispatches with no session open; every verdict waits in the
  inbox. The status line says `RECONCILER not running` when the snapshot is stale.

### issue: agentctl defects, filed (one agentctl editor)

```
agentctl issue "<what is wrong, where, how to see it>"   # files it: ISSUE <id>
agentctl issue --list [--all]                             # the open ones (--all: closed too)
agentctl issue --close <id> [--note "fixed in <sha>"]     # the maintainer, once fixed
agentctl red am-fixer                                     # exit 0 inside a dispatched red fixer (prints who), else 1
```

agentctl's code has ONE editor, its maintainer. Every other agent that meets an agentctl defect, and above all a red
fixer, files it with `agentctl issue`. An issue is a store item (type `issue`), shown in the status page's "agentctl
issues" section. While it stays open it escalates once a week. The fixer's prompt says so, and fixers no longer get a
~/bin worktree. `~/bin/hooks/pre-commit` (`git -C ~/bin config core.hooksPath hooks`, shared by every ~/bin worktree)
refuses a commit made from a fixer session. It identifies the fixer the way `red` does: by the session's binding or
fixer record, and only without a Claude session by the fixer environment. Any other exit of `agentctl red am-fixer`
(agentctl broken) lets the commit through with a warning, so a broken agentctl can always be fixed. On the laptop
(sync role) the issue is stored in ci-runner's store, so ci-runner's pin must include `issue`.

### land: QUEUED at once; checked, pushed and watched by the reconciler

```
agentctl land [--repo PATH] [--branch B] [--stash] [--keep-worktree]   # movies: QUEUED <id> (<sha>): END YOUR TURN
agentctl land --follow              # scripts and red fixers: the checks here, the push, then the watch, in-process
agentctl land status                # the changes with their states, the batch, the gate, the processor
agentctl land cancel <id>           # drop a queued or waiting change (its executor is stopped)
```

The owner: "agents never wait on a land". In movies (a repo whose hook can check a change apart), `land`:

1. fetches (retried once after 15 s) and rebases onto <remote>/main (a conflict: `NOT LANDED` listing the files);
2. runs `prepush-check`, the squash guard (below);
3. writes the change as a `change` item in state `queued` (re-landing from the same worktree and branch SUPERSEDES the
   older item: `SUPERSEDED <old> by <new>` to the inbox, its executor stopped);
4. pushes the change to `refs/heads/land/<id>` on Forgejo (bounded; movies' hook checks nothing on a `land/` ref and
   no workflow triggers on one), so the change lives on Forgejo, not only in your worktree;
5. prints `QUEUED <id> (<sha>, n ahead)` and exits 0.

The reconciler then takes it, a step per tick:

- **queued → checking.** The tick starts an executor, `agentctl waiting check <id>` (Fixes-Red changes first), holding
  `check/<id>` and a check slot (`$AGENTCTL_CHECK_TREES`, 3): a CHECK TREE `<clone>-wt-check-<n>`, kept between checks so
  its build stays warm. It rebuilds the change on <remote>/main there (each commit picked; one already on main is
  dropped; nothing left: `LANDED <head> (already on main via <commit>)`), then runs the hook's dry run -- `--range`, or
  `--checks-only` when main is red and the change must wait -- as a check of the governor (`gov`), under the
  20-min no-progress bound; or, under laptop pressure, on ci-runner (see "remote checks"). Pass: `checked`. Fail:
  `rejected`, `NOT LANDED: the pre-push checks failed` with the tail. A check the LAPTOP failed (sbt never came up, a
  script test's TIMEOUT, a stall) is no verdict: queued again 5 min later, up to 3 times. A dead executor is an
  expired lease: the tick starts another.
- **checked → pushing → landed.** Under the `land/<repo>` lease the tick re-reads the item (a superseded one is never
  pushed). Main moved since the check: rebased onto it, re-checked (a new round) ONLY when what landed meanwhile
  overlaps -- a path both touch, an sbt module one touches that the other's is or depends on, build.sbt / project/ /
  the hook -- else pushed at once (`main moved by 3 commits, no overlap: pushed without re-check`; the push's hook
  judges the red gate only). Re-checked twice at most; the third time it is pushed as rebased and the push's hook runs
  the checks. The push is bounded; on expiry it verifies before it retries. `LANDED <sha>`, and a `watch` item.
- **checked → waiting** while main is red and the change must wait (a production path, no `Fixes-Red:` for the red
  run): `WAITING <sha> (entry <id>, n in batch)`. Tools-only changes and Fixes-Red changes land while red.
- **The batch.** When main turns green, the waiting changes are ONE `batch` item. The tick assembles its tip in a
  throwaway check tree (<remote>/main plus the changes in queue order, Fixes-Red first; a change that no longer applies
  is rejected alone), and an executor, `waiting check-batch <id>`, checks it ONCE (`pre-push --fast-only`: every test
  config compiled and the fast tests; each change already passed its own full checks) on a fresh sbt server. Pass: one
  push of the tip; every change is LANDED with its own watch. Fail: bisect by prefix, the culprit confirmed before it
  is rejected, the passing prefix landed at once, the rest in the next batch. A stall is retried once, then the batch
  is HELD 30 min (`HELD:` in the inbox). A waiting change that reached main another way meanwhile is LANDED.
- **The batch tip's drift check (G5, 2026-10-29: E4's 13-commit and E9's 24-commit batches hid which identity change
  broke convergence).** A batch holding MORE THAN ONE identity-model commit (`identity/**`,
  `worker/src/main/**/identity/**`, `worker/src/test/scala/scripts/IdentityDrift*`) runs movies'
  `scripts/identity-drift-ci <tip>` (`--against main` for a fix batch on a red gate) after its check passed and before
  the push. No script at the tip: a logged no-op. No verdict (exit 2): the push goes on. DRIFT RED splits the batch:
  its identity changes are marked and assembled again one per batch beside everything else, so the non-identity part
  lands at once; a lone identity change red on a second run too is rejected with the drift report.
- **The watch.** A `watch` item per landed sha, evaluated each minute by the tick: **Main** (the main.yml run whose
  head is the sha, else the newest whose head contains it) and the **convergence** (the first convergence run on main
  created after the push whose head contains it; none within 60 min: said, not waited for). `WATCH OK: Main #n green,
  convergence #m green` closes the worktree you landed from when proven merged (unless `--keep-worktree`); `WATCH RED:
  <workflow> #n <failed jobs>` the moment either is red (fix forward with `Fixes-Red: <n>`); `WATCH UNKNOWN` after
  3 h undecided. A landed `Fixes-Red: N` is verified: `FIX OK` / `FIX FAILED` / `FIX UNVERIFIED`, each an inbox entry;
  a fix for a red outside the gate gets its workflow dispatched on main to verify it, with the failing run's own inputs (a required input left out is refused: Archive recording #823).

**Never an empty commit** (2026-10-27: overlapping entries landed four empty duplicates on main): re-queueing
replaces; a commit already on main is dropped at the rebuild; `prepush-check` refuses a range carrying an empty commit
unless it carries `Allow-Empty: <why>`.

`NOT LANDED: <reason>`: a dirty tree (`--stash` sets it aside in a named stash and restores it); a rebase conflict; a
prepush-check refusal; the pre-push checks failed (the last 25 lines; the full log in the entry's check log); a
conflict with the changes ahead of it in the batch; `stalled <step>` (no progress for 20 min); the red gate (followed
by the `ci-why` summary).

**In-process land** (`--follow`, `--no-watch`, a dispatched red fixer, `AGENTCTL_QUEUE=0`): the same checks run in
land's own process, outside the lock (the hook's dry run `--range <remote>/main..<sha>`), then under the `land/<repo>`
lease it fetches, pushes when main has not moved since the check (moved: checked again outside the lock, twice; the
third time under it) and verifies; main red and the change must wait: it joins the queue as `waiting` and `--follow`
waits for its batch. `--follow` then watches in-process; otherwise LANDED starts a watch item and land exits.

Other repos (no movies hook): `land` holds `land/<repo>`, fetches, rebases, runs prepush-check, pushes (the repo's
hook runs), verifies with `merge-base --is-ancestor` or `git cherry`, and prints `LANDED <sha>`; no watch.

Use it for every push to main. Don't use it for `measure/` branches: `identity-measure` pushes those itself.

### remote checks: ci-runner's check tier

Whenever the checkout's hook supports it (owner decision 1), a queued change's checks run on ci-runner's check tier:
the executor pushes the checked tip to `check/<entry id>` (movies' hook checks nothing for a check/ branch), whose push
runs `.github/workflows/precommit-check.yml` on the check tier: the same hook, `--checks-only <merge-base>..HEAD`. The
run is the ENTRY's, not the executor's: its record is saved the moment the branch is pushed, a replacement executor
ADOPTS it (no new push, no new run), and the 15-min (no run started) / 2-h (undecided) bounds count from the push;
past them it is checked locally. The push to main then carries `PREPUSH_REMOTE_RUN=<run id>`: the hook takes that run
only once Forgejo says it is precommit-check.yml, a success, on exactly the pushed commit. Superseding or cancelling an
entry cancels its undecided run.

### tick: the processor, the token, the gate

ONE QUEUE (2026-10-30): the queue is ci-runner's store. `land status`, the status line and the status page all read
its change items; `land cancel <id>` reaches any open item (on the laptop through the store API), one no entry carries
included. The laptop holds no queue: `land` (and its network re-queue) push `refs/heads/land/<id>` and submit; what its
local waiting dir still holds, the sync moves to ci-runner's queue each cycle (`migrated`), or says `LOCAL QUEUE: …`
loudly. A change whose commits are on main BY CONTENT (landing rebases, so never by sha) closes itself as LANDED, every tick (each change looked at every 10 min, `$AGENTCTL_LANDED_LOOK`), on the server and on the laptop
(its clone has the commits of the laptop's worktrees). A submission supersedes the older open items of its worktree.
An open item no queue entry carries is a STRANDED CHANGE condition, closed by its own probe once the item closes. W1
counts only those open items, after that landed look, and names the oldest with why it has not landed.
**Landed by content** is ONE helper (`cherry_landed`) for every such check -- orphans, queue entries, change items, W1,
fixers' worktrees, the closers: a commit is on main when main has a patch-equivalent commit (`git cherry`'s `-`) OR a
twin with the same subject, author and author date and identical changed (+/-) lines per file, context and line
numbers left out (2026-10-30: a commit landed 3 days earlier escalated UNLANDED ORPHAN because main had since changed
the lines around it, so its patch-id differed).

```
agentctl land status                         # the changes, the batch, a HELD batch, the gate, the processor (any cwd)
agentctl land cancel <id>                    # drop one change
agentctl land hold [--for MIN] [--why TEXT]  # take no batch until release or expiry (default 240 min; changes still queue)
agentctl land release                        # lift the hold; also ends a HELD batch's wait
agentctl tick [--once]                       # the processor (the tick loop)
agentctl tick service install|uninstall|status   # the processor as launchd agent net.bitcashier.agentctl-waiting
```

The hold is an item in ci-runner's store (machine `hold`: held -> released | expired), so the reconciler honours a
hold made on the laptop (until 2026-10-30 it was a local PAUSED file no tick read after the phase 2 cutover). One hold
at a time: a second `hold` extends it. Its deadline is its expiry: a forgotten hold lifts itself at the reconciler's
next tick (`hold.expired` event), never a manual-only state. The status line says `LAND HELD until HH:MM (by …: why)`,
and the status page shows it above the batch.

The old names (design §14.5) answer until 2026-11-05, each printing its new one: `waiting status` → `land status`,
`waiting pause|resume` → `land hold|release`, `waiting cancel` → `land cancel`, `waiting run|service` → `tick
[service]`. `queue` stays retired (it prints the retirement note). `red list|patrol` are gone (2026-10-30): `red status`.

(`waiting check <id>`, `waiting check-batch <id>`, `waiting await` and `watch` are internal: the executors and `land --follow`.)

- **An unread gate is RED** (the owner, 2026-10-27). When the processor cannot read the gate (Forgejo, the tunnel or
  the token down) a production change waits and no batch is taken; one `UNKNOWN: ... cannot read the gate (…)` per
  spell, and `land status` prints `gate: UNREAD, taken as red (…)`.
- **The token hand-off.** A launchd service gets no answer from the vault, so the processor listens on a unix socket
  (`/tmp/agentctl-<uid>/<hash>.sock`, 0600 in a 0700 dir; `$AGENTCTL_SOCK_DIR`). Every `land`, and `land status` /
  `resume` run where the vault answers, hand it the Forgejo token, kept in its memory and environment only (a re-exec
  keeps it; executors and fixers inherit it). After a reboot or a service restart: `AGENTCTL_INTERACTIVE=1
  ~/bin/agentctl land status` from `~/projects/movies` with the vault unlocked (the service log then says `the
  Forgejo token was handed over`). A command first ASKS the processor for the token it holds and hands it over only
  when it holds none or another one, so `handed over` is logged once per real change. A process without the token
  says so once, on the transition (`the movies Forgejo token is missing (…)`, with its inbox note), then at most a
  summary every 10 min (`$AGENTCTL_TOKEN_SUMMARY`), and `… is back` when it returns; its steps' NoToken is quiet.
- **Who may run `secrets`.** A TTY, `AGENTCTL_INTERACTIVE=1`, or an agent's own command in its Claude Code session
  (`CLAUDECODE` and `CLAUDE_CODE_SESSION_ID` set; ci-why and other reads). Never the processor, a launchd job,
  anything agentctl started in the background (`spawn_detached` marks it `AGENTCTL_DETACHED`) -- and never the
  laptop's `land` (2026-10-31: it died NoToken in agents whose vault was locked). Its submission is the land/<id> push
  over ssh plus a store call with a token from the environment or the processor's socket only; with none it prints
  `QUEUED-LOCAL ... the sync submits it with its own token` and the laptop's sync sends it. Its `Fixes-Red:` trailers
  are checked again by the reconciler at the push. No `secrets run ... --` wrapper is needed.
- **As a service** (`tick service install`, which the owner runs): KeepAlive, RunAtLoad, log
  `~/.cache/agentctl/waiting/<repo>/service.log`. Wall-clock sleeps only, so it carries on after the laptop sleeps; a
  batch lands with no agent alive, its verdicts in the inbox.

### gov: THE governor -- one admission control for laptop memory and CPU

```
agentctl gov status                 # admitted work, waiters, memory, evictable idle sbt servers, pressure
agentctl gov stats [--days 7]       # checks stats, plus each class's admission waits and the evictions
agentctl gov evict [--dry-run]      # stop the idle kept sbt server unused longest, by hand
agentctl gov admit --class C ...    # scripts/jvm-gate.sh's call (--fd 7 --ticket <file>); --try: one look
agentctl suite [run.py args]       # agentctl's own tests (tests/run.py) under a `suite` ticket: one run at once (SUITE_CAP)
```

The owner (2026-10-27): one governor instead of three limiters (jvm-gate.sh's 4 JVM slots, the check-slot cap,
sbt-warm's kept servers each holding a slot, idle or not).

- **Tickets.** Every heavy unit -- a pre-commit check, an sbt JVM (sbt-warm's server, identity tools) -- holds a ticket:
  an flock on `~/.cache/agentctl/gov/t-<id>.lock` (`$AGENTCTL_GOV_DIR`), freed by the kernel when its holders end,
  with a record beside it (class, expected GB, label, pid, cwd). jvm-gate.sh holds it on descriptor 7, as it held a
  slot (riding, the keeper hand-off unchanged); `KINOWO_GOVERNOR=0`, Linux or an agentctl without `gov` keep the slots.
- **Priority:** red (a change carrying `Fixes-Red:` for a red run; a `*-wt-red-*` checkout) > check > agent (an agent's
  own sbt) > measure (identity-measure*). Only the head of the queue is admitted: a lower class never overtakes.
- **Memory:** the expected GB (5 for a JVM and for a check, whose hook's `sbt -batch` takes no ticket of its own; an
  sbt server the hook starts through sbt-warm does, at the check's priority, `KINOWO_GOV_CLASS`) must fit the budget (sum of live tickets, `$AGENTCTL_GOV_BUDGET_GB`, 60% of RAM), and
  2.5 GB (`$AGENTCTL_GOV_FREE_NEED_GB`) must be free now (free + inactive + speculative) beyond a 1 GB reserve and what
  tickets admitted in the last 2 min still grow into; memory pressure under 75%; the laptop not under pressure
  (see the red queue's "Laptop pressure"). Red waits on none of it: a red fix is admitted at once.
- **CPU:** checks wait while load1 > 4x the cores, agent work > 1.5x, measurements > 1x; red never.
- **Eviction first:** when the head waiter does not fit on memory, the idle kept sbt server unused longest (no call in
  it, unused 1 min, not serving, never the root checkout's) is stopped (`sbt-warm --stop`, its busy lock held), then it
  looks again. Logged in `gov/gov.log`; every admission in `gov/admits.jsonl`.
- **Checks** keep their cap (below), now one of the governor's limits.

### checks: the pre-commit check cap

```
agentctl checks status             # the checks running
agentctl checks stats [--days 7]   # p50/p90 check duration and wait per concurrency level
```

The owner (2026-10-27): "just limit the precommit checks to a reasonable number", "keep measuring to adjust that
number". Every check run -- the queued checks, land --follow's dry run, the batch check, the known-red baseline -- is a
check ticket of the governor: at most N run at once (a red fix skips the cap). N: `$AGENTCTL_MAX_CHECKS` if set, else
the automatic cap (default 3, at most the budget's worth of sbt servers). Each run is appended to `checks/runs.jsonl`:
duration, wait, cap, checks running, and the load, CPU % and memory pressure / swap sampled during it. After each run
the cap is adjusted, at most once a day, logged in `checks/adjust.log` and sent to the inbox: up 1 when over 30% of the
day's checks waited over a minute while memory pressure stayed under 50% and swap steady; down 1 (to 1) when memory
pressure reached 75% or swap grew 512 MB during a check, or when the p90 duration at the current level is over 1.5x the
p90 one level below.

### queue: RETIRED (2026-10-26)

The merge queue batched identity landings around ONE CI landing check. With no pre-merge CI left (the owner,
2026-10-26: basic tests before merging, the touched area's tests locally, everything else on main), there is nothing
to batch: the reconciler lands changes in turn, and the batch item batches what must wait for a red main. Every
`agentctl queue …` command prints the retirement note and exits 2. Its code is in this repo's history (before 25e71e5).

### env: the agent environment, without secrets

```
agentctl env [--print|--check] [--ns movies|bitcashier]
```

- `--print` (the default): `export` lines putting sdkman's sbt and java first on PATH and setting JAVA_HOME. Use it
  as `eval "$(~/bin/agentctl env --print)"`.
- `--check`: checks sbt, a java that runs, `secrets status <ns>`, the tunnel on localhost:3000 (Forgejo web/API) and
  :2222 (Forgejo ssh), the Forgejo token (read inside the check and sent to `/api/v1/user`), and `forgejo-gh`.
- `--ns`: defaults to movies inside `~/projects/movies`, else bitcashier.
- Locked vault: exactly one line, `vault <ns> locked: ask the user to run `! secrets unlock <ns>``, then exit 1
  (on stderr with `--print`, so the eval stays clean).
- Exit: 0 all ok, 1 a problem.

```
$ agentctl env --check --ns movies
ok   sbt: /Users/pawel/.sdkman/candidates/sbt/current/bin/sbt
ok   java: openjdk version "27" 2026-09-15
ok   vault movies: unlocked
ok   tunnel localhost:3000 (Forgejo web/API): open
ok   tunnel localhost:2222 (Forgejo ssh): open
ok   forgejo-gh token (movies): token valid (user pawel)
ok   forgejo-gh: /Users/pawel/bitcashier/infra/bin/forgejo-gh
```

### dispatch: every CI dispatch has an owner; branch reds are routed (2026-10-27)

```
agentctl dispatch run <workflow.yml> --ref <branch> [-f key=value ...] [--purpose "..."]
agentctl dispatch record --branch <branch> [--run <id>] [--workflow W] [-f key=value ...] [--purpose "..."]
agentctl dispatch list
```

The owner: "red not captured, make a structural fix". Convergence #2116 (run 3264) on `measure/pawel/982f7681f-0b04a4`
failed and nobody was told: the snapshot, the status line and the reds covered main only.

- **Owner records** (`~/.cache/agentctl/dispatches/`, `$AGENTCTL_DISPATCH_DIR`): branch, run id, worktree, Claude
  session / its fixer name (`$AGENTCTL_AGENT_NAME` only outside a Claude session), pid (`$CLAUDE_PID`), purpose, inputs, time. `dispatch run` wraps
  `forgejo-gh workflow run` (`gh workflow run` on forge = github) and records it; `dispatch record` registers a dispatch made another way. agentctl's own
  dispatches record themselves (the queue's `check/*` remote checks, the Fixes-Red verification on main). A run with no
  record is matched once, after the fact: `measure/<user>/<sha>-<x>` (identity-measure, identity-ci-run) or the run's
  head commit to the worktree whose HEAD is that commit or whose HEAD reflog holds it; `check/<entry>` to its queue
  entry's worktree.
- **The snapshot** (`state.json`'s `ci.branches`) also holds the newest completed run of each tracked branch and
  workflow: any branch with an owner record, and `measure/*`, `check/*`, `red-*`. A branch red ends when its branch is
  deleted or a newer run of that workflow on it is green.
- **Route**: a failed branch run is ONE inbox entry, `BRANCH RED: <workflow> #N on <branch> → owner <worktree (agent)>`,
  with ci-why's first error lines; `inbox wait` wakes on it by default. Route it to that worktree's agent.
- **PREDICTS MAIN RED**: the run is built on commits all on main now (ancestor or patch-equivalent) AND ran with main's
  inputs for that workflow (its dispatch inputs, read from the owner record or Forgejo's DB, ignoring tier, runner,
  profile, jvm-options, heap, reuse-run, takes-base, takes-countries, countries). Main hits it next, so it is a gate red
  at once: `RED UNCLAIMED`, claimed and dispatched like a main red. Inputs that differ make it a plain branch red that
  names them (`recording 3166 ≠ main's 573`; main's pinned recording is read from the movies repo -- the
  `hermetic-<code>.txt` assets of the release that `FIXTURE_RELEASE_TAG` in main's identity-model-convergence.yml
  names -- by the laptop's processor hourly with the owner's token, cached in `~/.cache/agentctl/pinned-recording.json`
  and, in remote-store mode, written by the sync into the store (meta `pinned_recording`: tag, pins, `at`). The
  reconciler on ci-runner never reads release assets (ci-lander is push-only, no releases): it copies the store's
  record, and while the laptop is offline the last stored pins stand. `agentctl status` prints `pins: N (tag), read
  X ago`, `STALE` past 24 h. `$AGENTCTL_PINNED_RECORDING` overrides); a test failure on a NEWER recording also says `blocks promoting recording
  N`. Unread inputs predict nothing (the processor asks the DB only over an open ssh control connection; `ci-why` from
  a terminal reads them).
- **Status line / summary**: `branch reds: N → owner (workflow #n)` after main's reds, `PREDICTS MAIN RED` in bold red;
  `ci-why` lists them under "Branch reds".
- **Orphans close themselves** (2026-10-29: `branch reds: 3 → NOBODY`, identity runs #2365, #2370, #2371 on measure/*
  branches whose worktrees were removed). A branch red with no owner record and no worktree holding its commit, whose
  commit is not on main (or is, but main runs no such workflow, so it can never predict main red), is an ORPHAN: the
  processor moves its `branch-red` item (`<repo>#<run id>`) to `orphaned` with an event saying why, and it leaves the
  status line, `ci-why` and routing. Its finished `measure/*` branch is then for `agentctl cleanup` (the standing OK);
  others are listed. If its commit turns up in a worktree (the hourly re-match) or an owner record, it reopens (event)
  and is routed to that owner, however old. It is `gone` when its branch is deleted or green. A commit on main whose
  workflow main runs is never an orphan: today's behaviour.

### inbox: every final verdict, durable

```
agentctl inbox [ID...] [--unread] [--ack [--all]] [--limit N] [--json] [-v]
```

The owner (2026-10-27): three agents ended their turns while their land/watch still ran, and the WATCH verdicts went
nowhere. Every verdict now reaches the inbox: a change's, a batch's and a watch's as EVENTS the processor projects
(`QUEUED`, `LANDED`, `WAITING`, `NOT LANDED`, `WATCH ...`, `FIX ...`, each naming the author's worktree); `wait` and
`land --follow` write theirs when they end (normally, on a signal, or on a crash).

- One JSON file per verdict under `~/.cache/agentctl/inbox/` (`$AGENTCTL_INBOX_DIR`): id, time, command, argv, cwd,
  owner (the Claude session, else user@cwd), pid, exit code, the verdict line (the last `LANDED` / `NOT LANDED` /
  `WAITING` / `WATCH ...` / `run ...:` / `file ...:` line; `ended by signal N` or `crashed: ...` when it died), every
  verdict line it printed, and its last 15 output lines.
- A restart on new code (re-exec, same pid) writes nothing: the resumed process writes the verdict. Only a SIGKILL of
  a `wait` leaves no entry (its owner record stays; `agentctl orphans` names it); the reconciler's verdicts are events
  in the store and survive any process.
- `agentctl inbox` lists the newest 30 (`--limit 0`: all), `--unread` only those not acknowledged, `--ack`
  acknowledges what it listed that is the CALLER'S OWN -- its session's, from inside its worktree, or its agent name's
  (2026-10-29 19:00: an agent's `--unread --ack` hid every other agent's verdicts) -- entries named by ID whoever's,
  and `--ack --all` everything listed (the owner's, the orchestrator's), `-v` adds each entry's output tail, an ID prefix picks entries. Exit 0.
- The orchestrating session reads it whether or not the agent is alive: `agentctl inbox --unread --ack --all` after each
  agent's report, and before deciding an agent's land is lost.
- Each entry has a `kind`, from its verdict: `RED`, `OK`, `STALLED`, `UNKNOWN` (also a crash, a signal, an exit with
  no verdict), `NOT_LANDED`, `SUPERSEDED`, `LANDED`, `WAITING`, `HELD`, `FIX_FAILED`, `FIX_OK`, `FIX_UNVERIFIED`, `CHECK`, `OTHER`;
  from `red`: `RED_UNCLAIMED`, `RED_BUDGET`, `RED_ESCALATE`, `BLOCKED`, `RED_DISPATCHED`, `FIXER_LANDED`,
  `FIXER_FAILED`; from the branch runs: `BRANCH_RED` (see "dispatch").

#### inbox escalated: an escalation that wakes somebody (2026-10-27)

Main #2146's `RED UNCLAIMED` woke nobody: the orchestrator's `inbox wait` was not re-armed. An escalation is now an
EVENT of severity `escalate` -- an item past its deadline in a state that escalates (a red offered too long, a
condition left open, a stalled change) -- and the processor's notifier delivers each one ONCE: a macOS notification
(`osascript -e 'display notification ...'`, never a dialog), marked notified in the store (W6 checks none is left
undelivered). The status line leads with a bold `ESCALATED: N (...)` while any is current; `agentctl inbox escalated`
prints that line, or nothing.

#### inbox wait: the orchestrator's notifier

```
agentctl inbox wait [--kinds RED,BRANCH_RED,FIX_FAILED,NOT_LANDED,STALLED,UNKNOWN,RED_UNCLAIMED,RED_BUDGET,RED_ESCALATE,BLOCKED,FIXER_FAILED] [--since-ack] [--timeout S] [--forever]
```

`--forever` never exits: it streams each such entry once, one line each, as it appears -- ONE long-lived background
task (Monitor or `run_in_background`) covers the whole session, no re-arm needed; ack as you route.

It blocks until an UNREAD entry of those kinds (the default above) exists, prints every such entry, and exits 0 (2 at
`--timeout`). It acknowledges nothing. `--since-ack` skips entries older than the last acknowledgement (an old
backlog). It is cheap: one `stat()` of the inbox directory and of `acked/` a second (`$AGENTCTL_INBOX_POLL`), the
entries re-read only when either changed; no Forgejo, no git. The orchestrating Claude session's loop:

1. run `~/bin/agentctl inbox wait` with the Bash tool's `run_in_background=true`, and end the turn (or go on working);
2. woken when it exits: route each printed entry (a RED or FIX FAILED to the agent that landed it -- its cwd -- or a
   new fix agent; NOT LANDED back to its agent; STALLED / UNKNOWN: read `inbox <id> -v`, `ci-why`);
3. `~/bin/agentctl inbox <id> --ack` for each entry routed;
4. run it again (step 1). An entry not acked wakes the next `inbox wait` at once.

```
$ agentctl inbox --unread
20261027T041502113201-51234-land UNREAD land ~/projects/movies-wt-gen2-4 exit 0: WATCH OK: Main #2711 green, convergence #1630 green
20261027T031100553120-50111-land-waiting UNREAD land ~/projects/movies-wt-x still running: WAITING 3f2c1a9 (2 changes queued on waiting; entry e7)
```

### state: the hand-off file for long waits

```
agentctl state <topic> [--set field=value ...] [--add field=value ...]
```

It prints `scratchpad/state-<topic>.md`, creating it from the template first. The template has these fields: repo,
branch, worktree, commits, run ids, waiting on, next step, notes. `--set` replaces a field and `--add` appends to a
comma list. Write it before you end a turn that waits more than 30 minutes on CI. Exit 0.

```
# /…/scratchpad/state-demo.md
# State: demo
updated: 2026-10-23T13:21:37Z
repo:
branch: x
```

### prepush-check: the squash guard

```
agentctl prepush-check [--base <remote>/main] [--head HEAD] [--allow GLOB ...] [--max-foreign N]
agentctl prepush-check --hook <remote> [<url>]        # movies' pre-push hook mode (reads git's stdin)
```

It refuses when `base...head` touches a file outside `--allow` / `$AGENTCTL_ALLOW`, or when it returns more than N
files (default 2) to an older main version, which is what the `reset --soft` squash onto a moved main does. `git
revert` commits are exempt for their own files. So is a DECLARED retirement: a deleted file named by a `Retires: <path>` trailer (one per line, in the commit's
final trailer block with Co-Authored-By); undeclared deletions and modifications still count. It also refuses an EMPTY commit (no tree change) unless it carries
an `Allow-Empty: <why>` trailer. `land` runs it, and so does movies' hook. Exit 0 ok, 1 refused.

```
prepush-check: <remote>/main...c972d56a2b49: 1 file changed, 8 insertions(+)
prepush-check: ok
```

### The lifecycle closes itself (2026-10-26)

**After `WATCH OK`, the tick closes the worktree the change was landed from**: it removes the worktree and its local branch, only when
proven merged -- `git cherry <remote>/main HEAD` shows no commit of its own and the tree is clean, untracked files
included (a STATE.md keeps it) -- and never the root checkout, a locked tree, or a tree another process is in (the
lander's own shell aside). It logs `closed worktree <path> and branch <b> (...)` or `worktree <path> kept: <why>`,
and logs to `agentctl-deleted.log`. `--keep-worktree` skips it. WATCH RED or UNKNOWN closes nothing.

**`agentctl sweep` is the daily report** (run by `agentctl maintain` at its first pass from 09:00, see "maintain";
each report also in `~/.cache/agentctl/sweep/<date>.txt`). It also removes STALE red claims (see "red"):

```
agentctl sweep [--repo PATH ...] [--idle-days 3] [--dry-run] [--no-report]
```

- LISTS movies' worktrees and local branches that are unmerged and idle for more than 3 days (idle: no git activity in
  the worktree and no newer tip commit), and remote branches that are unmerged. It deletes nothing unmerged and no
  worktree at all.
- DELETES remote branches proven merged into main (ancestor, or every commit patch-equivalent per `git cherry`), under
  the standing OK, through the API and only if the branch still points where it was read. Never `main`, `master`,
  `waiting`, or `measure/*` (those are `cleanup`'s, by their runs).

### cleanup: remove proven-merged items only

```
agentctl cleanup [--dry-run|--execute] [--repo PATH ...] [--no-fetch] [--no-remote] [--min-age-hours H] [--include-caches] [-v]
```

For the scope and the log, see "Standing-OK cleanup scope" above. It is a dry run by default on a TTY. Exit 0, or 1
if something could not be removed.

```
agentctl cleanup (DRY RUN); log: /…/scratchpad/agentctl-deleted.log

== movies (<remote>/main cac5af8ce0)
  would delete worktree /Users/pawel/projects/movies-wt-hookguard [hookguard] 6a2a075956: ancestor of <remote>/main
```

### orphan worktrees: submitted once

A worktree with commits not on <remote>/main whose author is gone (no live lease, no process in it, idle) is SUBMITTED
ONCE by the tick, whatever its last verdict: it becomes a queued change like any other (its checks then decide; a
rejection is its red's to rescue), and an `UNLANDED ORPHAN` inbox entry names it. Nothing fixes an orphan on its own.

**Superseded work.** When a worktree's work was carried into another change by hand (rebased with real conflict
resolution, so landed-by-content cannot see it -- movies-wt-ci-1/2/3 and -review went into ci-5 on 2026-10-30 and
escalated until removed by hand), mark it:

```bash
~/bin/agentctl supersede ~/projects/movies-wt-ci-1 --by movies-wt-ci-5   # or --by <change id> (a unique part)
~/bin/agentctl supersede                                                 # the open marks (--all: closed, lapsed too)
~/bin/agentctl supersede ~/projects/movies-wt-ci-1 --undo
```

A superseded worktree is never submitted or escalated as an orphan (an open `UNLANDED ORPHAN` for it clears: its probe
says `superseded by`). Each maintain pass reads the successor again: once it lands (a change of the successor's
worktree LANDED since the mark, the named change -- or its re-submission -- LANDED, or the successor's head at the mark
on main by content), the superseded worktree closes itself: removed when clean and idle, its branch with it, both in
`agentctl-deleted.log`, and a `CLOSED` inbox entry. The successor ending unlanded (its worktree gone with nothing
landed, or the named change rejected or cancelled with no new change from its worktree) lapses the mark: the orphan
rules apply again. `--by` refuses a successor worktree with nothing off main yet.

### orphans: leftover loops and sbt servers

```
agentctl orphans [--kill [--safe]]
```

It lists owner records that are strays (a dead process's record and the runs it never cancelled; an owner still on
old code; an owner whose cwd is gone), wait loops and sbt servers that are orphaned (their worktree is gone, their
owning shell died, every target run finished, or they duplicate an older loop on the same runs). `--kill` cancels a
dead owner's runs and removes its record, and sends SIGTERM to the rest (an owner's handler cancels its own runs).
Output is `no orphans`, or one line per stray with the reason.

`--kill --safe` (= `agentctl reap`) is the scheduled reaper's mode (run by `agentctl maintain` every 15 min): it ends
only strays -- a dead owner's record and its runs, wait loops whose agent or runs are
gone or that duplicate an older one, sbt servers whose worktree is gone, whose serving command's sbt-warm or owner died
(sbt-warm's `.serving`), or that hold a running command (an sbtn client) with no CPU for 20 min across reaps
(`~/.cache/agentctl/sbt-cpu.json`; a `jcmd Thread.print` dump first, in `~/.cache/agentctl/sbt-dumps/`) -- never an
owner merely on old code, which re-execs itself. Every kill is a line of `~/.cache/agentctl/reap.log`. The daily
`sweep` reaps the sbt servers too.

**Stale JVMs** (folded in 2026-10-27 from `~/.local/bin/reap-stale-jvms.sh`, launchd `dev.kinowo.jvm-reaper`, now
retired; movies' `scripts/reap-stale-jvms.sh` is now a shim calling `agentctl reap`): the same rules, by the same tunables
(`HOG_MIN_AGE_SECONDS`, `HOG_MIN_CPU_PERCENT`, `ORPHAN_MIN_AGE_SECONDS`, `PROTECT_REGEX`, `PROTECTED_PORTS`, ...):

- `orphan`: an sbt worker / forked-test JVM with PPID 1, 10 min or older, whatever its CPU;
- `hog`: any sbt/java process 1.5 h or older at >= 5% of a core over its LIFETIME, with no tty, never a server a live
  `sbt-warm --keep` keeps in its directory;
- `dup-sbt-server` / `dup-sbt-gone-dir`: detached `sbt-launch --detach-stdio` servers grouped by cwd, all but the newest
  once 10 min old and idle now (recent %CPU <= 5), and one whose cwd is gone (a renamed `.part` is not gone);
- spared: IDEs and the protected names, a listener on port 9000 under 50% of a core, the reaper's own group, and any
  process under a live Claude Code session or a running land / check / waiting processor / red fixer.

They are listed first (`jvm pid=… rule=…`); with `--kill`, an sbt server gets a thread dump first, then TERM and KILL
after 10 s. Every kill and skip is a line of `~/.cache/agentctl/reap.log`.

### maintain: THE reaper, every 15 min

```
agentctl maintain [--dry-run] [--sweep]     # launchd net.bitcashier.agentctl-maintain (RunAtLoad, every 15 min)
agentctl reap [--dry-run]                   # the reap alone
```

The owner (2026-10-27): one reaper, not three. Each pass runs the reap (`orphans --kill --safe`, stale JVMs included)
and, once a day at the first pass from 09:00 local (`$AGENTCTL_SWEEP_HOUR`), the sweep. State in
`~/.cache/agentctl/maintain.json`, output in `~/Library/Logs/agentctl-maintain.log`, kills in
`~/.cache/agentctl/reap.log`. Source `~/bin/launchd/net.bitcashier.agentctl-maintain.plist` (installed as a copy). It
replaces `net.bitcashier.agentctl-reap`, `net.bitcashier.agentctl-sweep` and `dev.kinowo.jvm-reaper` (their plists:
`~/bin/launchd/retired/`); `~/.local/bin/reap-stale-jvms.sh` is now a shim calling `agentctl reap`.

Each pass also ROTATES THE LOGS (`rotate_logs`, and the hourly upkeep too; 2026-10-30: the processor's service.log
had grown to 141 MB): every `*.log` under `~/.cache/agentctl` and the reds dir (not a check's own run output in `*.d`,
`work`, `target`) and `~/Library/Logs/agentctl-*.log`, once over 10 MB (`$AGENTCTL_LOG_ROTATE_MAX`), is copy-truncated
IN PLACE: its newest 10 MB (from a line start) become `<log>.1`, older parts shift to `.2`/`.3` (keep 3,
`$AGENTCTL_LOG_ROTATE_KEEP`; the oldest goes). In place because launchd and the processor hold it open with O_APPEND:
they go on writing at its new end. The status page's `/log` also serves `<log>.N`, and shows the end of `.1` before a
short current file; a live pane reads a truncated file again from 0.

Each pass also expires the cache (`expire_cache`): under `~/.cache/agentctl` it removes the per-land logs
(`agentctl-land-*`, with their `.checks.out` / `.push.out` / `.baseline.out`), `phase2-apply-*.log`, `sbt-dumps/*`,
`reds/baseline*/*`, a `reds/<run>/` dir that is untouched as a whole and has no running fixer, and an `internal-trees/`
tag whose worktree is gone, all once they are older than 7 days (`$AGENTCTL_CACHE_EXPIRE_DAYS`). Nothing else: the
inbox, the store, `waiting/`, `checks/` and the logs it appends to stay. It never follows a symlink and never touches
a protected folder (~/Desktop, ~/Documents, ~/Downloads). `--dry-run` counts what would go.

---

## movies helpers that pair with agentctl

- `scripts/identity-measure … --wait` / `scripts/identity-repro … --wait`: print the CI run id and an ETA (the median
  wall time of the workflow's recent runs), then poll with the same 60 s → x1.5 → 300 s backoff. Run them in the
  background.
- The pre-push hook reuses a passed result when the same tree is pushed again: same tree, base, changed paths, checks,
  hook bytes, red-gate state and PREPUSH_SKIP. The key is stored in `~/.cache/kinowo-prepush`. Anything different runs
  every check. `PREPUSH_NO_REUSE=1` forces a full run.

## Config: agentctl.toml (simplification item 7, 2026-10-30)

Every knob has a code default; a TOML file may set it; an environment variable `AGENTCTL_<KEY>` overrides both (the
old `$AGENTCTL_*` names all still work). Files, lowest precedence first: `/etc/agentctl/agentctl.toml` (the server
copy, which infra can provide on ci-runner), then `~/.config/agentctl/agentctl.toml`; `$AGENTCTL_CONFIG` names one
file instead (`-`: none, as in the tests). Keys are the variable's name without `AGENTCTL_`, lower case
(`red_fixers = 5`); a `[red]` table's `fixers` is the same knob. A config value reaches every child process, and an
edited file applies at the next restart (a processor re-exec included).

```
agentctl config            # the knobs that are set, each with its value and where it came from (--all: every knob)
agentctl config defaults   # a complete agentctl.toml: every knob commented out at its default, with its doc
agentctl config doc --write  # regenerate the reference below from the code (a test keeps it in step)
```

**The forge** (`forge = "forgejo" | "github"`, `$AGENTCTL_FORGE`; github-return plan §4.1, W5a): where agentctl
READS CI -- run lists and views, failed and job logs, takes artifacts, release pins, `dispatch run`, run cancels, the
actions API (remote checks, the Fixes-Red verify dispatch, measure-branch cleanup). `forgejo` (the default until Phase
3 flips it) is the forgejo-gh shim with the vault's Forgejo token through the tunnel; `github` is the real `gh`
(`gh_cli`) with gh's own auth (`$GH_TOKEN` or `gh auth login`), and run links go to github.com by run id. GitHub
serves no dispatch inputs: there they come from the dispatch records alone (no db-q). Pushing, the landing identity
and the store's auth do not follow the switch yet (W5b). Code: `lib/agentctl/forge.py`; tests: `tests/test_forge.py`
runs the same scenarios against `fake_forgejo_gh.py` and `fake_gh.py`.

Not knobs (never in a file): a process's identity (`AGENT_NAME`, `RED_FIXER`, `FIXER_RECORD`, lease secrets), the
store token, the role (`ROLE`, `HOST`), and the tests' hooks.

<!-- agentctl config doc: generated by `agentctl config doc --write`; do not edit by hand -->

| key (`agentctl.toml`) | environment | default | what |
|---|---|---|---|
| `allow` | `AGENTCTL_ALLOW` | - | - |
| `allowed_roots` | `AGENTCTL_ALLOWED_ROOTS` | - | - |
| `assign_bound` | `AGENTCTL_ASSIGN_BOUND` | `20` | - |
| `attach_poll` | `AGENTCTL_ATTACH_POLL` | `15` | - |
| `author_active_min` | `AGENTCTL_AUTHOR_ACTIVE_MIN` | `15` | live = a pid alive and activity within this |
| `baseline` | `AGENTCTL_BASELINE` | `1` | - |
| `batch_retry_max` | `AGENTCTL_BATCH_RETRY_MAX` | `3` | - |
| `bg_poll` | `AGENTCTL_BG_POLL` | `5` | seconds between the wrapper's looks at its session |
| `bin_repo` | `AGENTCTL_BIN_REPO` | - | - |
| `brief` | `AGENTCTL_BRIEF` | `~/bin/briefs/movies-agent-preamble.md` | - |
| `brief_src` | `AGENTCTL_BRIEF_SRC` | - | a file to use instead of the tracked docs/fixer-brief.md in fixers' prompts (an override): none by default |
| `cache_expire_days` | `AGENTCTL_CACHE_EXPIRE_DAYS` | `7` | - |
| `checks_dir` | `AGENTCTL_CHECKS_DIR` | - | - |
| `check_lease_ttl` | `AGENTCTL_CHECK_LEASE_TTL` | `600` | - |
| `check_trees` | `AGENTCTL_CHECK_TREES` | `3` | check trees, so at most this many queued changes are checked at once |
| `claude` | `AGENTCTL_CLAUDE` | - | set: that one only (the tests' fake) |
| `claude_projects` | `AGENTCTL_CLAUDE_PROJECTS` | - | - |
| `claude_settings` | `AGENTCTL_CLAUDE_SETTINGS` | - | - |
| `clone_keepalive` | `AGENTCTL_CLONE_KEEPALIVE` | `20` | - |
| `clone_lock_wait` | `AGENTCTL_CLONE_LOCK_WAIT` | `300` | the swap waits this long for the clone's users to finish, then tries again next tick |
| `clone_repair_retry` | `AGENTCTL_CLONE_REPAIR_RETRY` | `900` | a repair that failed is tried again after this |
| `clone_timeout` | `AGENTCTL_CLONE_TIMEOUT` | `1800` | - |
| `db_inputs` | `AGENTCTL_DB_INPUTS` | - | - |
| `disk_min_gb` | `AGENTCTL_DISK_MIN_GB` | `50` | the Data volume's free space below which the upkeep runs at once, then WD opens |
| `disk_path` | `AGENTCTL_DISK_PATH` | - | - |
| `dispatch_dir` | `AGENTCTL_DISPATCH_DIR` | - | - |
| `dispatch_find` | `AGENTCTL_DISPATCH_FIND` | `12` | - |
| `dispatch_find_poll` | `AGENTCTL_DISPATCH_FIND_POLL` | `5` | - |
| `drift_stall` | `AGENTCTL_DRIFT_STALL` | `1800` | s with no output before the drift check is ended (no verdict: the batch goes on) |
| `fetch_timeout` | `AGENTCTL_FETCH_TIMEOUT` | `180` | an unattended fetch's bound |
| `fixer_claim_within` | `AGENTCTL_FIXER_CLAIM_WITHIN` | `600` | s a started session has to start its transcript / run `red claim` |
| `fixer_land_watch` | `AGENTCTL_FIXER_LAND_WATCH` | `60` | - |
| `fixer_unlanded_escalate` | `AGENTCTL_FIXER_UNLANDED_ESCALATE` | `5400` | - |
| `fixer_unlanded_nudge` | `AGENTCTL_FIXER_UNLANDED_NUDGE` | `2700` | - |
| `forge` | `AGENTCTL_FORGE` | `github` | the forge agentctl reads runs, logs, artifacts and dispatches through (Forgejo was switched off 2026-10-31) |
| `forgejo_api` | `AGENTCTL_FORGEJO_API` | - | - |
| `forgejo_gh` | `AGENTCTL_FORGEJO_GH` | `f"{HOME}/bitcashier/infra/bin/forgejo-gh"` | - |
| `forgejo_owners` | `AGENTCTL_FORGEJO_OWNERS` | - | - |
| `forgejo_url` | `AGENTCTL_FORGEJO_URL` | - | - |
| `gate_publish` | `AGENTCTL_GATE_PUBLISH` | `45` | - |
| `gh_cli` | `AGENTCTL_GH_CLI` | `gh` | the GitHub CLI forge = github runs |
| `github_api` | `AGENTCTL_GITHUB_API` | - | - |
| `github_lander_repos` | `AGENTCTL_GITHUB_LANDER_REPOS` | - | - |
| `gov_admit_max` | `AGENTCTL_GOV_ADMIT_MAX` | `7200` | a check waits this long for the governor at most |
| `gov_budget_gb` | `AGENTCTL_GOV_BUDGET_GB` | `round(0.6 * ram_gb(` | - |
| `gov_dir` | `AGENTCTL_GOV_DIR` | - | - |
| `gov_evict_settle` | `AGENTCTL_GOV_EVICT_SETTLE` | `3` | the freed memory shows in vm_stat |
| `gov_free_need_gb` | `AGENTCTL_GOV_FREE_NEED_GB` | `2.5` | free memory a new JVM needs NOW (beyond the reserve); its heap grows into the budget's share later |
| `gov_poll` | `AGENTCTL_GOV_POLL` | `2.0` | - |
| `gov_reserve_gb` | `AGENTCTL_GOV_RESERVE_GB` | `1.0` | - |
| `identity` | `AGENTCTL_IDENTITY` | - | - |
| `inbox_dir` | `AGENTCTL_INBOX_DIR` | - | - |
| `inbox_poll` | `AGENTCTL_INBOX_POLL` | `1.0` | seconds between stat()s of the inbox directory |
| `infra_same_max` | `AGENTCTL_INFRA_SAME_MAX` | `1800` | - |
| `internal_trees_dir` | `AGENTCTL_INTERNAL_TREES_DIR` | - | - |
| `landed_look` | `AGENTCTL_LANDED_LOOK` | `600` | - |
| `land_lease_max` | `AGENTCTL_LAND_LEASE_MAX` | `2700` | the design's 300 s once checks no longer run in the push's hook under the lease (step 6) |
| `land_lease_ttl` | `AGENTCTL_LAND_LEASE_TTL` | `120` | - |
| `land_lock_timeout` | `AGENTCTL_LAND_LOCK_TIMEOUT` | `14400` | - |
| `land_ref_sweep` | `AGENTCTL_LAND_REF_SWEEP` | `600` | - |
| `land_stall_min` | `AGENTCTL_LAND_STALL_MIN` | `20` | - |
| `land_wait_report_min` | `AGENTCTL_LAND_WAIT_REPORT_MIN` | `10` | - |
| `laptop_free_pct` | `AGENTCTL_LAPTOP_FREE_PCT` | `20` | holds other work while macOS's memory free % is under this |
| `laptop_load` | `AGENTCTL_LAPTOP_LOAD` | `2.0` | holds other work while the 1-min load is over this x the cores |
| `laptop_pressure_level` | `AGENTCTL_LAPTOP_PRESSURE_LEVEL` | `2` | holds other work at this kernel pressure level or over (1 normal, 2 warn, 4 critical) |
| `laptop_swap_growth_mb` | `AGENTCTL_LAPTOP_SWAP_GROWTH_MB` | `1024` | holds other work while swap grew this much (MB) within LAPTOP_SWAP_WINDOW |
| `laptop_swap_window` | `AGENTCTL_LAPTOP_SWAP_WINDOW` | `600` | seconds of swap samples the growth is read over |
| `launch_agents` | `AGENTCTL_LAUNCH_AGENTS` | - | - |
| `live_globs` | `AGENTCTL_LIVE_GLOBS` | - | - |
| `live_poll` | `AGENTCTL_LIVE_POLL` | `1.0` | s between looks at a quiet file |
| `log_rotate_keep` | `AGENTCTL_LOG_ROTATE_KEEP` | `3` | rotated parts kept per log (<log>.1 newest .. <log>.N oldest) |
| `log_rotate_max` | `AGENTCTL_LOG_ROTATE_MAX` | `10485760` | bytes over which an agentctl log is rotated (copy-truncate; its newest part kept as .1) |
| `machine` | `AGENTCTL_MACHINE` | - | - |
| `max_checks` | `AGENTCTL_MAX_CHECKS` | - | - |
| `max_foreign` | `AGENTCTL_MAX_FOREIGN` | `2` | files a push may revert to an older main before prepush-check refuses |
| `min_interval` | `AGENTCTL_MIN_INTERVAL` | `60` | - |
| `movies_remote` | `AGENTCTL_MOVIES_REMOTE` | - | - |
| `movies_repo` | `AGENTCTL_MOVIES_REPO` | - | - |
| `notify` | `AGENTCTL_NOTIFY` | - | - |
| `orphan_idle` | `AGENTCTL_ORPHAN_IDLE` | `7200` | its action had its time |
| `owners_dir` | `AGENTCTL_OWNERS_DIR` | - | - |
| `owner_idle` | `AGENTCTL_OWNER_IDLE` | `21600` | no red held this long: the owner ends |
| `pinned_recording` | `AGENTCTL_PINNED_RECORDING` | - | - |
| `push_stall_secs` | `AGENTCTL_PUSH_STALL_SECS` | `180` | - |
| `push_wall_min` | `AGENTCTL_PUSH_WALL_MIN` | `45` | - |
| `queue` | `AGENTCTL_QUEUE` | `1` | - |
| `recovery_dir` | `AGENTCTL_RECOVERY_DIR` | - | - |
| `recover_max` | `AGENTCTL_RECOVER_MAX` | `600` | a probe is re-run at most this far apart, however often it failed |
| `reds_dir` | `AGENTCTL_REDS_DIR` | - | - |
| `red_crash_block_max` | `AGENTCTL_RED_CRASH_BLOCK_MAX` | `86400` | $AGENTCTL_RED_CRASH_BLOCK_MAX |
| `red_crash_loop` | `AGENTCTL_RED_CRASH_LOOP` | `3` | $AGENTCTL_RED_CRASH_LOOP |
| `red_families` | `AGENTCTL_RED_FAMILIES` | - | - |
| `red_fixers` | `AGENTCTL_RED_FIXERS` | `5` | live fixers at once, every family together |
| `red_fixer_wall` | `AGENTCTL_RED_FIXER_WALL` | `10800` | a dispatched fixer's maximum wall time |
| `red_fix_giveup` | `AGENTCTL_RED_FIX_GIVEUP` | `21600` | - |
| `red_fix_requeue_grace` | `AGENTCTL_RED_FIX_REQUEUE_GRACE` | `900` | $AGENTCTL_RED_FIX_REQUEUE_GRACE |
| `red_old_run` | `AGENTCTL_RED_OLD_RUN` | `172800` | an outside red whose newest run is older than this is checked by a fresh run before it is fixed |
| `red_others_every` | `AGENTCTL_RED_OTHERS_EVERY` | `60` | seconds between the other watched repos' red ticks: one gate read each |
| `red_read_token_file` | `AGENTCTL_RED_READ_TOKEN_FILE` | - | - |
| `red_renew` | `AGENTCTL_RED_RENEW` | `60` | a fixer's wrapper renews its lease this often |
| `red_repo` | `AGENTCTL_RED_REPO` | - | - |
| `red_repos` | `AGENTCTL_RED_REPOS` | - | - |
| `red_skip_say` | `AGENTCTL_RED_SKIP_SAY` | `600` | s between the lines of another repo whose tick keeps failing for the same reason |
| `red_starve` | `AGENTCTL_RED_STARVE` | `2700` | an offer waiting this long for a slot is STARVED |
| `red_tick` | `AGENTCTL_RED_TICK` | `15` | seconds between the processor's red ticks |
| `remote` | `AGENTCTL_REMOTE` | - | - |
| `restart_check` | `AGENTCTL_RESTART_CHECK` | `30` | seconds between code-version checks |
| `runners_page_cache` | `AGENTCTL_RUNNERS_PAGE_CACHE` | `f"{HOME}/.cache/ci-runners-page/last-good.json"` | - |
| `sbt_reap` | `AGENTCTL_SBT_REAP` | - | the tests never reap this machine's servers |
| `sbt_stall_min` | `AGENTCTL_SBT_STALL_MIN` | `20` | - |
| `scratch` | `AGENTCTL_SCRATCH` | - | - |
| `sock_dir` | `AGENTCTL_SOCK_DIR` | - | short: a socket path is <= 104 bytes |
| `stall_cpu_seconds` | `AGENTCTL_STALL_CPU_SECONDS` | `1.0` | CPU that counts as progress per window: a watchdog's polls are not work |
| `state_file` | `AGENTCTL_STATE_FILE` | - | - |
| `state_root` | `AGENTCTL_STATE_ROOT` | - | - |
| `status_agent_bound` | `AGENTCTL_STATUS_AGENT_BOUND` | `4` | s the page waits for one agent's details |
| `status_bound` | `AGENTCTL_STATUS_BOUND` | `8` | s a source may take ; the agents' source twice that |
| `status_page` | `AGENTCTL_STATUS_PAGE` | - | - |
| `status_port` | `AGENTCTL_STATUS_PORT` | - | - |
| `step_bound` | `AGENTCTL_STEP_BOUND` | `120` | - |
| `step_join` | `AGENTCTL_STEP_JOIN` | `20` | - |
| `store_auth` | `AGENTCTL_STORE_AUTH` | - | - |
| `store_retries` | `AGENTCTL_STORE_RETRIES` | `4` | - |
| `store_retry_base` | `AGENTCTL_STORE_RETRY_BASE` | `0.5` | s; doubled each attempt: 0.5 + 1 + 2 + 4 = 7.5 s at most |
| `store_secret` | `AGENTCTL_STORE_SECRET` | - | - |
| `store_url` | `AGENTCTL_STORE_URL` | - | - |
| `streak_rereads` | `AGENTCTL_STREAK_REREADS` | `3` | stale or missing streak run signatures read again per tick |
| `suite_cap` | `AGENTCTL_SUITE_CAP` | `1` | agentctl's own test-suite runs at once : parallel agents queue, never pile up |
| `sweep_hour` | `AGENTCTL_SWEEP_HOUR` | `9` | local time: the daily sweep runs at the first maintain pass from 09:00 |
| `sync_mirror` | `AGENTCTL_SYNC_MIRROR` | `10` | s between the laptop's copies of /v1/state into state.json |
| `tick` | `AGENTCTL_TICK` | `5` | seconds between ticks ; every step is bounded or rate-limited inside |
| `token_summary` | `AGENTCTL_TOKEN_SUMMARY` | `600` | s between the summaries of a still-missing Forgejo token (said on its transition first) |
| `tunnel_state` | `AGENTCTL_TUNNEL_STATE` | - | - |
| `unkicked_after` | `AGENTCTL_UNKICKED_AFTER` | `3600` | a convergence-relevant main push with no convergence run on main by then is a red |
| `upkeep_every` | `AGENTCTL_UPKEEP_EVERY` | `3600` | - |
| `w1_max` | `AGENTCTL_W1_MAX` | `7200` | the oldest unlanded change ; while main is red, counted from the red's start |
| `w2_max` | `AGENTCTL_W2_MAX` | `600` | a gate red with nobody on it |
| `w5_max` | `AGENTCTL_W5_MAX` | `300` | the processor's duties beat (its bounded steps take up to 2 min) |
| `w6_max` | `AGENTCTL_W6_MAX` | `300` | a due escalation not notified |
| `w7_max` | `AGENTCTL_W7_MAX` | `300` | phase 2: an event ci-runner wrote that the laptop's sync has not projected (60x its 5 s tick) |
| `w8_window` | `AGENTCTL_W8_WINDOW` | `3600` | W8 looks at the change items that closed within this |
| `waiting_green_ticks` | `AGENTCTL_WAITING_GREEN_TICKS` | `100` | ticks (~10 min) changes may wait at a green gate with no batch moving: then STALLED |
| `waiting_timeout` | `AGENTCTL_WAITING_TIMEOUT` | `86400` | - |
| `watch_conv_start` | `AGENTCTL_WATCH_CONV_START` | `3600` | no convergence run containing the sha started by then: said, not waited for |
| `watch_error_poll` | `AGENTCTL_WATCH_ERROR_POLL` | `30` | - |
| `watch_poll` | `AGENTCTL_WATCH_POLL` | `60` | - |
| `watch_stall_min` | `AGENTCTL_WATCH_STALL_MIN` | `120` | a watch with no change in its runs for this long ends WATCH STALLED |
| `watch_timeout` | `AGENTCTL_WATCH_TIMEOUT` | `10800` | either run undecided by then: exit 2, could not tell |
| `wb_max` | `AGENTCTL_WB_MAX` | `600` | - |
| `wm_max` | `AGENTCTL_WM_MAX` | `2700` | maintain's last pass (launchd every 15 min) |
| `x` | `AGENTCTL_X` | `DEFAULT` | The knobs are what the code reads: each `env_num("AGENTCTL_X", DEFAULT)` and `os.environ.get("AGENTCTL_X", DEFAULT)` |

<!-- end of the generated config reference -->

## Troubleshooting

- **`vault <ns> locked`.** Pass the line to the lead: the user runs `! secrets unlock <ns>`. Don't retry in a loop,
  and never try to unlock.
- **Tunnel down** (`Connection refused` on localhost:3000/2222/3010, `GitOps manifests STALE`, a fetch failing).
  Run `agentctl env --check`. The fix is `launchctl kickstart -k gui/$(id -u)/net.bitcashier.forgejo-tunnel`. The
  tunnel restarts itself under launchd, so wait a few seconds and land again.
- **The red gate refused the push.** `land` already printed `ci-why`. For a tools-only change, it lands while red. For
  a production change, wait for the green run that `ci-why` names (in the background, with `wait run`), or add
  `Fixes-Red: <#|id>` only if your commit really fixes that run. Never use PREPUSH_SKIP or `--no-verify`.
- **Push races.** `land` retries 3 times by itself. `NOT LANDED: still racing` means main is very busy: land again in
  a minute.
- **A hook check failed** (sbt, a lint, scripttests). Read the tail `land` printed, and the full log at the path it
  named. Fix forward, commit (amend only an unpushed commit), and land again. A same-tree re-push reuses the parts of
  the earlier run that passed only if the whole run passed.
- **`wait run` says "attached to existing waiter".** That is expected: another agent is already polling that run, and
  you will get the same verdict.
- **The gate reads UNREAD and nothing lands.** The processor lost its Forgejo token (a service restart). Hand it over:
  `cd ~/projects/movies && AGENTCTL_INTERACTIVE=1 ~/bin/agentctl land status` with the vault unlocked.
- **The status line says `RECONCILER not running`.** `state.json` is stale: `agentctl tick service status`, the
  service log `~/.cache/agentctl/waiting/<repo>/service.log`, and `agentctl watchdog` (W5).
- **Where is the fixer for red #N?** `claude agents` (by name), `agentctl red status`, `agentctl lease list red/`.
- **WB: uncommitted edits in ~/bin.** Someone edited ~/bin's own checkout. Agentctl changes go in a worktree
  (`git -C ~/bin worktree add ~/bin-wt-<name> -b <name> master`), landed by rebase + `git merge --ff-only`; the
  processor only ever re-execs onto committed code.
