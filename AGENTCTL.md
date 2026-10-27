# agentctl

`~/bin/agentctl` is the orchestration tool for Claude subagents working in the bitcashier repos
(`~/bitcashier/{infra,core,dashboard,invoice-page,version-dashboard}`) and in movies (`~/projects/movies`). All of
these repos have the remote `forgejo`. It replaces the hand-written shell that agents kept getting wrong: poll loops,
fetch/rebase/push/verify sequences, log trawling, DB spelunking. It does that work in fewer, cheaper and safer tool
calls.

- Not on PATH: call it as `~/bin/agentctl`.
- Python 3, standard library only.
- Tests: `python3 -m unittest discover -s ~/bin/tests` (mutation tests included).
- `agentctl --help` and `agentctl <cmd> --help` match this page.

## Standard agent workflow

```sh
eval "$(~/bin/agentctl env --print)"        # 1. sbt/java on PATH; a locked vault stops you here with one line
#    ... work in your own worktree, commit ...
~/bin/agentctl land                         # 2. rebase + guard, then QUEUED <id> in seconds: END YOUR TURN (verdicts -> inbox)
~/bin/agentctl wait run <id> &              # 3. Bash run_in_background=true, then END YOUR TURN
~/bin/agentctl ci-why                       # 4. only when land says the red gate refused, or Main is red
~/bin/agentctl inbox --unread --ack         # 5. the orchestrator: every land/watch/wait verdict, agent alive or not
~/bin/agentctl inbox wait &                 # 6. the orchestrator's notifier: Bash run_in_background=true (see "inbox")
~/bin/agentctl red claim <run> --note "…"   # 7. assigned a red build? claim it first (see "red")
```

Every change lands with `land`, identity changes included (2026-10-26: no pre-merge CI; the merge queue is retired).
Since 2026-10-27 (the owner: "agents never wait on a land") `land` in movies rebases, runs the squash guard and prints
`QUEUED <id> (<sha>)` at once; the always-on `waiting` processor then runs the change's checks (the pre-push hook: the
basic tests plus the identity / page area tests the change touches) in a check tree of its own, within the governor's
budget -- or on ci-runner when the laptop is under pressure -- and pushes it (`LANDED <sha>`, then a detached watch:
`WATCH OK` / `WATCH RED` / `WATCH UNKNOWN`) or, main red, puts it on `waiting` (`WAITING`). Every verdict goes to
`agentctl inbox`, naming your worktree. See "the land queue".

Step by step:

1. Run `env --print` first in any session that builds or pushes. If it prints
   `vault <ns> locked: ask the user to run `! secrets unlock <ns>``, stop there and pass that line to the lead.
2. Work and commit in your own worktree. Never use `reset --soft` onto a moved main.
3. Run `land` from the worktree; it prints `QUEUED <id>` within seconds (or `NOT LANDED: <why>` for a dirty tree, a
   rebase conflict, the squash guard). END YOUR TURN at QUEUED. The verdict -- `LANDED`, `WAITING`, `NOT LANDED` with
   the failing checks' tail, then the watch's `WATCH OK/RED` -- reaches the orchestrator through `agentctl inbox`; a
   NOT LANDED or WATCH RED of yours comes back to you: fix forward (`Fixes-Red: <run>` for a red you caused) and land
   again.
4. To wait for a CI run, start `wait run` with the Bash tool's `run_in_background=true` and end your turn. You are
   re-invoked when it exits. Never sleep or poll in the foreground.
5. When main is red or the gate refused, run `ci-why`. It names the run, the cause (infra or test) and how the gate
   clears: a later green run, or a `Fixes-Red:` trailer.

## Safety guarantees

- **Never `--no-verify`.** `land` builds its push argv in `push_argv()`, which asserts that no `--no-verify` or
  `--force` flag is present. `land` also refuses to run with `PREPUSH_SKIP` set. A hook failure is reported, never
  bypassed.
- **Never unlocks the vault.** `env` only runs `secrets status <ns>`. When the vault is locked, it tells you which
  `!` command to hand to the user. Touch ID belongs to the user.
- **Never prints a secret.** Tokens are read inside the call that uses them (`secrets get … FORGEJO_API_TOKEN` or
  `$GH_TOKEN`) and go only into a request header -- or, for a DETACHED child (land's watch, the processor it starts),
  into the child's environment, and to the waiting processor over a 0600 unix socket (see "waiting"): never on a
  command line or on disk. Why: `secrets` anchors an unlock to the session's processes, so a process whose launcher
  exited (a detached watch once land ends) or a launchd service gets `locked`.
- **The DB is read-only.** `db-q` runs `sqlite3 -readonly -safe` and refuses anything but one SELECT, WITH or PRAGMA
  read.
- **Never deletes unmerged work.** `cleanup` deletes only what is proven merged: the worktree's HEAD is an ancestor of
  forgejo/main, or every one of its commits is patch-equivalent there per `git cherry`. The tree must also be clean and
  idle. Every deletion is re-verified just before it happens and logged.
- **One lander per repository.** `land` holds an flock per git common dir (`~/.cache/agentctl/land/`) from fetch to
  verified push, so the kernel frees it when the lander dies. A second `land` waits (one line names the holder), then
  rebases onto what the first landed: nobody tests a base that is about to move. The watch after LANDED runs with the
  lock already released.
- **Every long-running mode is OWNED** (2026-10-26): `wait`, land's watch and its wait on `waiting`, and the `waiting`
  processor each hold one flock per target under `~/.cache/agentctl/owners/` (`$AGENTCTL_OWNERS_DIR`), freed by the
  kernel when the process dies, with a record beside it: owner (Claude session, else user@cwd), pid, process start time,
  argv, cwd, code version, and the CI runs it started. A second instance of the same target attaches (`wait`) or says
  who owns it; it never runs beside a live one and never waits on a dead one. The runs it started are cancelled when
  it ends, by its SIGTERM/SIGHUP/SIGINT handler, or after a SIGKILL by the next owner of the same target. At each poll
  it compares `agentctl`'s file with the version it started on and, after a change, re-execs on the new code with
  the same pid (land's watch resumes as `agentctl watch --sha`, its wait on `waiting` as `agentctl waiting await`).
- **One waiter per target.** `wait` owns the run or file (above), in any session. A second caller attaches to the live
  owner's verdict file and does not poll.
- **No artificial load.** Polls back off up to 5 minutes. Nothing in agentctl burns CPU.

## Standing-OK cleanup scope

The user has standing-OKed `agentctl cleanup --execute` for exactly what it deletes:

- clean, idle (at least 2 h) worktrees of the six repos whose HEAD is merged into forgejo/main (ancestor, or all
  commits patch-equivalent);
- their local branches, but never `main` or `master`, and never a branch checked out elsewhere;
- remote `measure/*` branches whose runs have all finished, or that are more than 24 h old with no run;
- after `WATCH OK`, the worktree `land` ran from and its branch, when proven merged and clean (see "The lifecycle closes
  itself");
- remote branches `agentctl sweep` proves merged into main (not `main`, `master`, `waiting`, `measure/*`).

It skips locked trees, dirty trees, trees in use by a process, and trees under `~/.cache` (unless you pass
`--include-caches`). Every deletion is appended to `scratchpad/agentctl-deleted.log`
(`<ts> <kind> <repo> <name> <sha> <why>`). The sha makes each deletion restorable. Anything outside this scope still
follows "list before deleting".

---

## Subcommands

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
  - `UNCLEAR`, when a log has lines but no failure marker (truncated, or the runner was lost);
- a newer run in flight that may clear it;
- the clearing rule.

A gate red says how long it has been red (`red for 3h 0m (2 failed runs)`: from the oldest failure of the current
streak). For movies it then lists every workflow OUTSIDE the gate whose newest decided run on main is red, oldest red
first, each with how long and since when (the owner, 2026-10-27: a red nobody is shown gets ignored). Those never make
`ci-why` exit 1: they block no push.

`ci-why <run id>` explains one run. Exit: 0 green, 1 red (or a run explained).

```
$ agentctl ci-why
ci-why pawelkrupinski/movies: main is RED for the gate
Main: GREEN (run 1976 #968)
Identity model convergence: RED (run 1934 #945, failure); newer run 1988 (#973) in_progress may clear it
  verdict: UNCLEAR (a failed step logged no FAILED/error line: read `forgejo-gh run view 1934 --log-failed`; if infra, rerun: ~/bitcashier/infra/bin/forgejo-gh run rerun 1934 --failed -R pawelkrupinski/movies)
  run id 1934 (#945) http://forgejo.internal:3000/pawelkrupinski/movies/actions/runs/945
  commit 2f0fdcd3c Paweł Krupiński <…> 10-23 11:32: order-phases.sh: no mapfile -- the pre-push hook runs scripts under macOS's bash 3.2
  [infra] leg (germany): failed in setup step 'Set up job' (often a `needs:` cascade or runner/setup trouble)
  [unclear] takes (united-kingdom): 8-line log with no FAILED/error/assert line (truncated? runner lost?) in step '…'; ends: …
  clears when: a later Identity model convergence run on main is green, or every pushed commit touching a production path carries `Fixes-Red: 945` (or `Fixes-Red: 1934`); tools-only commits land while red
Outside the gate (red, not blocking a push; oldest first):
  Verify scrape recording: RED for 5h 15m since 2026-10-26T23:49:43Z (1 failed run; newest run 3140 #2000) -- `agentctl ci-why 3140`
```

Use it when the red gate refused your push, or before you decide whether a red is yours. Don't read logs by hand
first. A genuine `Fixes-Red:` trailer is only for a commit that really fixes that run.

Under each red row (the gate's and the reds outside it) ci-why names who is fixing it (see "red"):
`fixing: <claimant> (<worktree>, claimed|committed|landing|waiting)`, `fixing: fix landed in <sha>, awaiting #M`,
`fixing: BLOCKED ON OWNER `<command>``, or `fixing: NOBODY` (with any stale claim). `ci-why --summary` is the status
line's one line: `gate RED (main #2066 → fix 0a5767c awaiting #2070; convergence green)`.

### red: who is fixing each red build, and a fixer for one nobody fixes

```
agentctl red claim <run> [--note "<what>"]     # an agent assigned a red runs this FIRST
agentctl red block <run> --cmd "<command>"     # it waits on the owner: BLOCKED ON OWNER (assigned, shown loudly)
agentctl red release <run> [--any]
agentctl red list [-v]                         # claims (live / STALE / blocked), dispatched fixers, today's count
agentctl red dispatch <run> [--force]          # a headless Claude Code fixer, by hand
agentctl red auto [on|off|status]              # the processor's automatic dispatch
agentctl red patrol [--dispatch]               # the processor's look, now
```

The owner (2026-10-27): "i want gate status to point to agent(s) fixing the red build(s)", then "if a build is red, an
agent must be assigned to it".

- **One cause, many reds.** A red is keyed by its failure SIGNATURE: workflow + failing job + its first relevant log
  line, normalized (numbers, shas, timestamps, temp paths out). A newer run failing the same way is the same red: a
  claim or a `Fixes-Red:` naming any run with that signature still counts. One fixer per red run covers all its
  signatures.
- **Fixing it** is any of: a claim (`red claim`: the claimant is `$AGENTCTL_AGENT_NAME`, else the Claude session, else
  the worktree; its pid the Claude Code process); a worktree with an unlanded commit carrying `Fixes-Red: N`
  (committed; patch-equivalent on main counts as landed); a running `agentctl land` from such a worktree (landing); a
  `waiting` entry carrying one (waiting); such a commit on main whose next run is still in progress (landed, awaiting
  #M; if a run containing it fails the same way, the fix did not fix it and is shown so).
- **Heartbeats.** A claim lives while its process lives (a dispatched fixer: while its log grows), or while its worktree
  commits or moves. 30 min with none (`$AGENTCTL_RED_STALE_MIN`): STALE, counted for nobody, removed by `agentctl
  sweep`, and the red can be taken over.
- **RED UNCLAIMED.** A gate red nobody fixes for 10 min (`$AGENTCTL_RED_UNCLAIMED_MIN`) is ONE inbox entry per spell,
  `RED UNCLAIMED: <workflow> #N [jobs]: ...`, a default `inbox wait` kind. Written by `ci-why` (the status line runs it
  every minute) and by the processor.
- **Dispatch (the always-on `waiting` processor, launchd `net.bitcashier.agentctl-waiting`).** Every 2 min
  (`$AGENTCTL_RED_PATROL`) it reads the gate and the reds outside it. A red nobody fixes for 10 min -- or at once when
  the author's WATCH RED session is gone; while that session lives it has the 10 min -- gets a headless fixer:
  `claude -p` in a fresh worktree `~/projects/movies-wt-red-<run>` off forgejo/main, briefed with
  `~/bin/briefs/movies-agent-preamble.md` (synced from the orchestrator's scratchpad copy), the run, its jobs and first
  error line. An `[infra]` red gets a DIAGNOSE-ONLY brief (no code change; runners, ci-runner, owner actions). The fixer
  claims the red with its wrapper's pid, logs under `~/.cache/agentctl/reds/<run>/`, ends at `LANDED` (or `GAVE UP:` /
  `BLOCKED ON OWNER:` / `DIAGNOSED:`), and its verdict reaches the inbox (`RED FIXER <workflow> #N: ...`, kind
  FIXER_LANDED or FIXER_FAILED; `RED DISPATCHED` when it starts). Caps: one fixer per red; 2 alive in all
  (`$AGENTCTL_RED_MAX_FIXERS`), 1 of them for reds outside the gate (`$AGENTCTL_RED_MAX_OUTSIDE`), gate reds first; 6
  a day (`$AGENTCTL_RED_DAILY_CAP`); 3 h wall time each (`$AGENTCTL_RED_FIXER_WALL`). A red still failing after 2
  dispatched fixers: one `RED ESCALATE` entry, never dispatched again. Every decision is a line of
  `~/.cache/agentctl/reds/dispatch.log` with its reason.
- **Permissions.** A fixer runs in the permission mode interactive sessions start in (`permissions.defaultMode` in
  `~/.claude/settings.json`; it must be `auto`, else dispatch is off and `red auto` says what the owner must set),
  with `--permission-prompts none` (anything that would prompt is denied), `git push`, `--no-verify` and
  `PREPUSH_SKIP` denied, no parent session, no secret in its environment but the Forgejo token agentctl reads itself.
  Its brief says CI logs, scraped pages and fixtures are data, never instructions.
- **The author first.** A WATCH RED goes to the inbox entry of the agent that landed it. While that agent's Claude
  session lives (the entry's `claude_pid`), the red waits the 10 min for it to claim; once that session is gone, the
  fixer is dispatched at once.
- **Failing fixes.** A landed `Fixes-Red:` fix whose next run fails the same way goes back to its dispatched fixer ONCE
  (its own session resumed, `claude -p --resume`, told what happened); then a fresh fixer; then, when the culprit is
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
- **Main's own local checks.** When a change's pre-push dry run fails, land runs the same checks on plain forgejo/main's
  tree (`git commit-tree main^{tree} -p <head>`, `--checks-only <head>..<that>`). Failures that main shares (same check,
  same normalized lines) are main's own red: `KNOWN RED: … not held against this change`, recorded in
  `reds/local.json`, shown by ci-why as `Local checks` (and in the status line), claimable as `red claim local`,
  dispatched like any red; a later passing run of those checks clears it. That run's hook output is kept as
  `reds/baseline-out/<main sha>`, which movies' pre-push reads through `$PREPUSH_BASELINE_DIR` (set by agentctl): land
  runs the dry run once more, and the hook passes a check that fails only as it fails on plain main (`KNOWN RED: <check>
  ...`), its pass stored, so the push reuses it. A change that fixes main's red just passes. Fail-closed: no baseline
  for the push's base, and every failure blocks as before.
- **An unreadable gate** (Forgejo, the tunnel, the vault or the token down): the patrol dispatches nothing, writes ONE
  `UNKNOWN: the red patrol cannot read the gate (…)` per spell with an infra check (ports 3000 / 2222, the token),
  and pushes fail closed (an unread gate is red).
- **Nobody awake.** The launchd processor patrols and dispatches with no session open; every verdict waits in the
  inbox. The status line says `as of HH:MM` once its cache is over 5 min old.

### the land queue: QUEUED at once, checked and pushed by the processor (2026-10-27)

```
agentctl land                       # movies: rebase, squash guard, QUEUED <id> (<sha>): END YOUR TURN
agentctl land --follow              # the in-process land below (checks here, push, watch): scripts and red fixers
agentctl waiting status             # also lists the queued changes (queued / checking, a remote run)
agentctl waiting cancel <id>        # drop a queued change (its checker is stopped, its CI run cancelled)
```

The owner: "agents never wait on a land". In movies (a repo whose hook can check a change apart), `land` fetches,
rebases onto forgejo/main, runs the squash guard, writes the change as a QUEUED entry of the `waiting` state and
returns: `QUEUED <id> (<sha>, n ahead)`, exit 0 (an inbox entry of kind QUEUED). The always-on processor (launchd
`net.bitcashier.agentctl-waiting`) starts a CHECKER per queued change (`agentctl waiting check <id>`, owned per entry;
a fix of a red run first), at most `$AGENTCTL_CHECK_TREES` (3) at once, each in a CHECK TREE of its own -- a detached
worktree `<clone>-wt-check-<n>`, kept between checks so its build stays warm, seeded from the shared build cache when
cold. The checker:

1. rebuilds the change on forgejo/main in the tree (a conflict: `NOT LANDED: ... no longer applies`);
2. runs the hook's dry run there -- `--range`, or `--checks-only` when main is red and the change must wait -- as a
   check of THE GOVERNOR (`gov`: red for a `Fixes-Red:` fix), under the stall bound; or, when `gov status` reports
   pressure, on ci-runner (see "remote checks"); a failure is `NOT LANDED: the pre-push checks failed` with the tail;
3. under the landing lock: main moved since the check -> back to 1 (twice; the third time it is rebased there and
   pushed, the push's hook running the checks on the new base, so a busy main never starves it); main red and the change must wait ->
   it joins `waiting` (`WAITING ...`, the entry names your worktree); else the push from the tree, whose hook reuses
   the pass (or confirms the remote run) -> `LANDED <sha>` and a detached watch that closes your worktree after
   WATCH OK when proven merged (unless land had `--keep-worktree`).

Every verdict is an inbox entry whose cwd is your worktree. A checker that dies is restarted by the processor (three
times, then `NOT LANDED`). `land --follow`, `--no-watch`, a dispatched red fixer (`$AGENTCTL_RED_FIXER`, whose wrapper
reads LANDED) and `AGENTCTL_QUEUE=0` keep the in-process land below.

### remote checks: ci-runner's check tier under pressure

When the governor reports memory or CPU pressure (`gov status`'s `pressure:` line: memory pressure over 75%, under
3 + 5 GB free with no idle sbt server to evict, or load over 2x the cores; `$AGENTCTL_REMOTE_PRESSURE`,
`$AGENTCTL_REMOTE_LOAD`; `AGENTCTL_REMOTE_CHECKS=0` turns it off), a queued change's checks run on ci-runner instead:
the checker pushes the checked tip to `check/<entry id>` (movies' hook checks nothing for a check/ branch), whose push
runs `.github/workflows/precommit-check.yml` on the check tier (ubuntu runner, slice `check`): the same hook,
`--checks-only <merge-base with main>..HEAD`. The checker follows that run (owned: cancelled if the checker dies;
none started in 15 min, or undecided after 2 h: no verdict, checked locally instead), reads its verdict -- the same
shape as a local one, the failed jobs' relevant lines as the tail -- and deletes the branch. The push to main then
carries `PREPUSH_REMOTE_RUN=<run id>`: the hook takes that run for its checks only once Forgejo says it is
precommit-check.yml, a success, on exactly the pushed commit; anything else runs every check on the laptop.

### land: lock, rebase, check, push, verify, watch

```
agentctl land [--repo PATH] [--branch B] [--stash] [--no-watch] [--keep-worktree]   # agents: Bash run_in_background=true
```

Steps, in order. Since 2026-10-27 the LONG part runs before the lock: fetch, rebase, the squash guard and step 6's
dry run first, unlocked; then, under the lock, steps 1-5 again (seconds) and, when forgejo/main has not moved since the
dry run, straight to the push, whose hook reuses that pass. Main moved: the lock is given back and the dry run repeats
on the new main, unlocked (twice; a third time it runs under the lock, so a lander always finishes). A step that makes
no progress -- no output and no CPU in its process group for 20 min (`$AGENTCTL_LAND_STALL_MIN`) -- is ended with
`NOT LANDED: stalled <step>` (one land's hook hung 2.5 h in sbt-warm on 2026-10-27 and held every lander). The JVM load
of parallel dry runs is capped by sbt-warm's JVM slots, not by the lock.

1. take the repository's landing lock (an flock per git common dir under `~/.cache/agentctl/land/`), waiting for
   another lander's to be released (`waiting for the landing lock (…), held for 12 min by: pid … <worktree> since …;
   now: checks of <sha> since …`, repeated every 10 min; `$AGENTCTL_LAND_LOCK_TIMEOUT`, default 4 h);
2. fetch (retried once after 15 s, since a tunnel restart takes seconds);
3. `git rebase forgejo/main`; on a conflict it aborts and lists the files;
4. `prepush-check` against forgejo/main;
5. MAIN RED? When a gating workflow is red (ci-why's gate) and a pushed commit touches a production path without a
   `Fixes-Red:` naming a red run, the change does not push: it goes onto the `waiting` branch (see "waiting" below),
   `WAITING <sha> (n changes queued on waiting)`, the lock is released, and land keeps running until the branch lands:
   then `LANDED <sha>` and step 10. Tools-only and Fixes-Red changes go on to step 6 while red, as the hook allows;
6. CHECK FIRST, when the repo has `scripts/hooks/pre-push` (movies): `scripts/hooks/pre-push --range
   forgejo/main..<sha>`, the hook's dry run over exactly what will be pushed. In movies that is the red gate, the
   path-selected checks, the basic tests and the area tests (`identity`, `page`: minutes; see
   `docs/landing-process.md`). A failure is `NOT LANDED: the pre-push checks failed` with the tail, and nothing is
   pushed. The pass is stored, so step 7's hook reuses it in seconds (a long hook inside `git push` holds the push's
   ssh connection idle, and a tunnel drop kills it);
7. `git push forgejo <sha>:refs/heads/main`. The repo's pre-push hook runs, with sdkman's sbt/java first on PATH and
   JAVA_HOME set;
8. on a race (`non-fast-forward`, `cannot lock ref`, `fetch first`, `failed to update ref`), refetch, rebase and
   retry (from step 5), up to 3 times;
9. fetch again and verify with `merge-base --is-ancestor`, or `git cherry` for patch-equivalent commits:
   `LANDED <sha>`. The lock is released here;
10. THE WATCH (movies; `--no-watch` skips it): main's CI for the landed sha.
   - **Main**: the `main.yml` run whose head is the sha; if it was cancelled or never came (a newer push superseded
     it), the newest Main run whose head contains the sha (`merge-base --is-ancestor`).
   - **Convergence**: the FIRST `identity-model-convergence.yml` run on main created after the push whose head
     contains the sha — the sha's own kick, a later push's, or the daily run; a cancelled one (the lane replaced it)
     passes to the next. None within 60 min (`$AGENTCTL_WATCH_CONV_START` seconds; kick-convergence skips pushes
     outside `.github/convergence-paths.txt`): `convergence: none started within 60 min`, not waited for.
   - Polls back off from 60 s to 5 min. Verdict: `WATCH OK: Main #n green, convergence #m green` (exit 0);
     `WATCH RED: <workflow> #n <failed jobs>` plus the `ci-why` summary (exit 3) the moment either is red: your
     change may have broken main, fix forward with `Fixes-Red: <n>`; `WATCH UNKNOWN: …` (exit 2) when a run is
     undecided after 3 h (`$AGENTCTL_WATCH_TIMEOUT`), or with no Forgejo token at all (`no Forgejo token (…)`, at
     once: retrying cannot help).
   - **Forgejo unreachable** (2026-10-27): a failed read backs off from 30 s to 5 min and is retried until the 3 h
     bound; only then `WATCH UNKNOWN: Forgejo's runs could not be read for 1h 40m (n failed reads, the last: …)`. No
     stall is judged on failed reads.
   - **Red fixes are verified** (the owner, 2026-10-27: the batch fix 97bb6d835 claimed `Fixes-Red: 2047`, Main #2059
     failed again in the same worker-identity job, and nothing said so). For each run N a landed commit's
     `Fixes-Red:` names (the sha's own trailers, plus `--fixes N` that land / the batch pass for the pushed commits),
     the watch compares the first completed run of N's workflow containing the commit with N's failed jobs, each its
     own inbox entry: `FIX FAILED: <workflow> #<new> still fails <jobs> (fix <sha> claimed #N)`, `FIX OK: #N cleared by
     #<new>`, or `FIX UNVERIFIED: …` (N's workflow not completed when the watch ended, a job did not run, N not a
     Main / convergence run).
   - Every exit writes a verdict that names its cause (2026-10-27: two detached watches ended `(no output)`, exit 1:
     they had no vault, and `token()`'s SystemExit slipped past the watch's `except Exception`). A SystemExit's message
     is the verdict (`exited: …`), an exit with no verdict line says what it printed last (stderr first), and the
     entry is written even when WATCH OK has just removed the worktree the watch ran in.
   - Other repos: `watch: no CI watched for <repo>`, exit 0.
   - The detached watch runs from the MAIN CLONE, never the agent's worktree (2026-10-27: a watch crashed when its
     agent removed the worktree at LANDED); its inbox entry still names the worktree (`--for`).

`NOT LANDED: <reason>` reasons:

- dirty tree;
- lock wait timed out;
- rebase conflict;
- prepush-check refusal;
- main is red and the change conflicts with the changes already on `waiting`, or the checks failed on the combined
  `waiting` tip (only your change is rejected; the waiting ones stay);
- (later, for a waiting change) main moved and the change no longer applies, or the green-main batch failed with it;
- the pre-push checks failed (the dry run: its last 25 lines; the full log in `scratchpad/agentctl-land-*.log`);
- red gate (followed by the `ci-why` summary);
- `stalled <step>`: the dry run, the push or the waiting checks made no progress for 20 min and were ended;
- hook failure at push time (its last 25 lines, plus a tunnel hint when Forgejo was unreachable);
- still racing;
- pushed but not on main.

A dirty tree is refused unless you pass `--stash`. That stashes the changes under a named stash
(`agentctl-land-<pid>-<ts>`), finds it again by its sha (the stash list is shared by every worktree), and restores it
afterwards. Exit: 0 landed (and watched green, or nothing to watch), 1 not landed, 2 landed but the watch could not
tell, 3 landed and Main or the convergence RED.

```
$ ~/bin/agentctl land          # from ~/projects/movies-wt-relay, run_in_background=true
agentctl: checking c972d56a2b: scripts/hooks/pre-push --range forgejo/main..HEAD (log: …/agentctl-land-movies-wt-relay-72692.log)
agentctl: pushing c972d56a2b to forgejo/main (attempt 1/4; hook log: …)
LANDED c972d56a2b4932077baaa40af95d388ab500bb03
agentctl: watching Main and the convergence for c972d56a2b (up to 180 min; `--no-watch` skips this)
Main #2711: success
Identity model convergence #1630: success
WATCH OK: Main #2711 green, convergence #1630 green
```

Use it for every push to main. Don't use it for `measure/` branches: `identity-measure` pushes those itself.

### land, red or green (2026-10-27)

```
agentctl land [--follow]   # own checks first (a check slot) -> green: push, LANDED -> red: join `waiting`, WAITING
```

1. **The change's OWN checks first**, red main or green, outside the landing lock and in parallel with other landers'
   within the check slots (below): the hook's dry run `--range forgejo/main..HEAD` -- or, when main is red and the
   change must wait (a production path, no `Fixes-Red:` for the red run), `--checks-only` (no gate). A failure:
   `NOT LANDED`; nothing joins.
2. **Then the gate** (ci-why's: the newest COMPLETED Main and Identity model convergence runs on main; running, queued
   and cancelled runs say nothing). **Green** (or a change that may land while red: tools only, or every production
   commit carrying `Fixes-Red:` for the red run): under the lock, fetch, check main has not moved since the check
   (moved: check again, outside the lock), push; the hook reuses the check's pass. `LANDED <sha>`, a DETACHED watch is
   started (its WATCH verdict goes to the inbox), and land exits 0. **Red**: under the lock, only the cherry-pick of the
   already-checked change onto `waiting` (rebuilt on forgejo/main; a conflict is `NOT LANDED`):
   `WAITING <sha> (entry <id>, n in batch)`, and land exits 0. END YOUR TURN at either; every verdict reaches
   `agentctl inbox`. `--follow` stays: until the batch lands, then watches.

### waiting: production changes held while main is red, landed in one push at green

```
agentctl waiting status                      # the entries, the batch being checked, a HELD batch, the gate
agentctl waiting cancel <id>                 # drop one; `waiting` is rebuilt without it
agentctl waiting pause|resume                # take no batch until resume (joins still queue); resume ends a HELD wait
agentctl waiting run [--once] [--service]    # the processor; land starts it detached
agentctl waiting service install|uninstall|status   # the processor as launchd agent net.bitcashier.agentctl-waiting
```

- **An unread gate is RED** (the owner, 2026-10-27). At 08:05Z the launchd processor, with no Forgejo token (launchd
  has no `secrets` on PATH, and no vault session anyway), read the gate as green and pushed a 2-change batch (ce9442b1b)
  onto red Main #2047; the hook, tokenless too, skipped its own gate. Now `gate_reds` answers `{unread}` when it cannot
  read: land joins `waiting`, the processor takes no batch, writes ONE `UNKNOWN: the waiting processor cannot read the
  gate (…)` inbox entry per unread spell, and `waiting status` prints `gate: UNREAD, taken as red (…)`.
- **The token hand-off**: the processor listens on a unix socket (`/tmp/agentctl-<uid>/<hash>.sock`, 0600 in a 0700
  dir; `$AGENTCTL_SOCK_DIR`). Every `land` that joins, and `waiting status` / `resume`, run where the vault answers and
  hand it the token, kept in its memory and environment only (a re-exec keeps it; the batch push's hook and the
  watches it starts inherit it). After a reboot or service restart: `~/bin/agentctl waiting status` from a session
  with the vault unlocked.
- **The processor** (one per repo, an owned lock; it re-execs on agentctl's new code before each batch) waits while
  the gate is red (it takes a batch while red only if a `Fixes-Red:` entry for the red run waits -- such an entry goes
  FIRST). At green, under the lock it rebuilds `waiting` on forgejo/main; released, it runs ONE combined check of the
  batch's tip: `pre-push --fast-only` (every test config compiled, the lint specs and local-tests.txt's `fast` set; no
  area tests: each change passed its own full checks before it joined; the full `--range` for a hook without
  `--fast-only`), in a detached worktree `<clone>-wt-batch` on a FRESH sbt server (`sbt-warm --stop` before and
  after), in a check slot. Changes that join during the check wait for the next batch.
- **Pass:** under the lock, main unmoved, ONE push of the tip through the hook (`PREPUSH_REUSE_FAST=1`: it reuses the
  fast pass after judging the gate). Every entry is LANDED (an inbox entry in its agent's worktree) and gets a detached
  watch. **Fail:** bisect by prefix (binary search over the entries' tips), reject only the culprit (`NOT LANDED` with
  the failing tail, to the inbox), land the passing prefix before it at once; the rest go with the next batch.
- **Stall:** the check runs under the 20-min no-progress abort (no output growth and under a second of CPU in its
  process group), and the hook's exit 75 / `STALLED:` counts too. A stalled check is retried once on a fresh server;
  stalled again, the batch is HELD for 30 min (`$AGENTCTL_BATCH_HOLD`) with a `HELD:` inbox entry.
- **As a service** (`waiting service install`, which the owner runs): `agentctl waiting run --service` under launchd
  (KeepAlive, RunAtLoad, log `~/.cache/agentctl/waiting/<repo>/service.log`). An empty `waiting` costs a directory
  listing every 30 s; wall-clock sleeps only, so it carries on after the laptop sleeps; a batch lands with no land or
  agent alive, its verdicts in the inbox.

### gov: THE governor -- one admission control for laptop memory and CPU

```
agentctl gov status                 # admitted work, waiters, memory, evictable idle sbt servers, pressure
agentctl gov stats [--days 7]       # checks stats, plus each class's admission waits and the evictions
agentctl gov evict [--dry-run]      # stop the idle kept sbt server unused longest, by hand
agentctl gov admit --class C ...    # scripts/jvm-gate.sh's call (--fd 7 --ticket <file>); --try: one look
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
  tickets admitted in the last 2 min still grow into; memory pressure under 75% (red: 90%).
- **CPU:** agent work waits while load1 > 1.5x the cores, measurements while > 1x; checks and red never.
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
to batch: `land`'s lock lets several agents land in turn without stepping on each other, and `waiting` batches what
must wait for a red main. Every `agentctl
queue …` command prints the retirement note and exits 2. Its code is in this repo's history (before 25e71e5).

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

### db-q: read-only Forgejo DB query

```
agentctl db-q [--env forgejo] [--max-rows N] "<sql>"
```

It runs `ssh -o BatchMode=yes pawel@10.1.0.3 'sudo -u forgejo nix shell nixpkgs#sqlite -c sqlite3 -readonly -safe …'`
with the SQL on stdin.

- Refused: more than one statement; anything not starting with SELECT, WITH or PRAGMA; DML or DDL anywhere (a CTE can
  front a DELETE); file functions; `PRAGMA x = y`; any sqlite dot-command line.
- Output is tab-separated with a header, capped at `--max-rows` (default 200), with a count of the rows not shown.
- On a refused connection it reopens the tunnel once (`launchctl kickstart -k gui/$UID/net.bitcashier.forgejo-tunnel`)
  and retries.
- Exit: 0 ok, 1 query or connection failed, 2 refused.

Tables: `action_run` (`"index"` is the number in the URL), `action_run_job`, `action_task`, `action_task_step`.
Status values: 1 ok, 2 fail, 3 cancelled, 4 skipped, 5 waiting, 6 running.

```
$ agentctl db-q 'select id, "index", status, substr(title,1,40) from action_run order by id desc limit 3'
id	index	status	substr(title,1,40)
2007	988	5	keyboard-nav page test: load Torun's lis
2006	987	5	Page test repeat
2005	986	3	keyboard-nav page test: load Torun's lis
```

Use it only as the fallback, when `forgejo-gh` or `ci-why` cannot answer, for example for queue analysis. It never
writes.

### inbox: every final verdict, durable

```
agentctl inbox [ID...] [--unread] [--ack] [--limit N] [--json] [-v]
```

The owner (2026-10-27): three agents ended their turns while their land/watch still ran, and the WATCH verdicts went
nowhere. Every `land`, `watch`, `wait` and `waiting await` now writes its verdict to an inbox when it ends (normally, on
a signal, or on a crash), and land writes its `WAITING` line there at once, since it may wait for hours.

- One JSON file per verdict under `~/.cache/agentctl/inbox/` (`$AGENTCTL_INBOX_DIR`): id, time, command, argv, cwd,
  owner (the Claude session, else user@cwd), pid, exit code, the verdict line (the last `LANDED` / `NOT LANDED` /
  `WAITING` / `WATCH ...` / `run ...:` / `file ...:` line; `ended by signal N` or `crashed: ...` when it died), every
  verdict line it printed, and its last 15 output lines.
- A restart on new code (re-exec, same pid) writes nothing: the resumed `watch` / `waiting await` writes the verdict.
  Only a SIGKILL leaves no entry (its owner record stays; `agentctl orphans` names it).
- `agentctl inbox` lists the newest 30 (`--limit 0`: all), `--unread` only those not acknowledged, `--ack`
  acknowledges what it listed, `-v` adds each entry's output tail, an ID prefix picks entries. Exit 0.
- The orchestrating session reads it whether or not the agent is alive: `agentctl inbox --unread --ack` after each
  agent's report, and before deciding an agent's land is lost.
- Each entry has a `kind`, from its verdict: `RED`, `OK`, `STALLED`, `UNKNOWN` (also a crash, a signal, an exit with
  no verdict), `NOT_LANDED`, `LANDED`, `WAITING`, `HELD`, `FIX_FAILED`, `FIX_OK`, `FIX_UNVERIFIED`, `CHECK`, `OTHER`;
  from `red`: `RED_UNCLAIMED`, `RED_ESCALATE`, `BLOCKED`, `RED_DISPATCHED`, `FIXER_LANDED`, `FIXER_FAILED`.

#### inbox wait: the orchestrator's notifier

```
agentctl inbox wait [--kinds RED,FIX_FAILED,NOT_LANDED,STALLED,UNKNOWN,RED_UNCLAIMED,RED_ESCALATE,BLOCKED,FIXER_FAILED] [--since-ack] [--timeout S]
```

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
agentctl prepush-check [--base forgejo/main] [--head HEAD] [--allow GLOB ...] [--max-foreign N]
agentctl prepush-check --hook <remote> [<url>]        # movies' pre-push hook mode (reads git's stdin)
```

It refuses when `base...head` touches a file outside `--allow` / `$AGENTCTL_ALLOW`, or when it returns more than N
files (default 2) to an older main version, which is what the `reset --soft` squash onto a moved main does. `git
revert` commits are exempt for their own files. So is a DECLARED retirement: a deleted file named by a `Retires: <path>` trailer (one per line, in the commit's
final trailer block with Co-Authored-By); undeclared deletions and modifications still count. `land` runs it, and so does movies' hook. Exit 0 ok, 1 refused.

```
prepush-check: forgejo/main...c972d56a2b49: 1 file changed, 8 insertions(+)
prepush-check: ok
```

### The lifecycle closes itself (2026-10-26)

**After `WATCH OK`, `land` closes the worktree it ran from**: it removes the worktree and its local branch, only when
proven merged -- `git cherry forgejo/main HEAD` shows no commit of its own and the tree is clean, untracked files
included (a STATE.md keeps it) -- and never the root checkout, a locked tree, or a tree another process is in (the
lander's own shell aside). It prints `closed worktree <path> and branch <b> (...)` or `worktree <path> kept: <why>`,
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

== movies (forgejo/main cac5af8ce0)
  would delete worktree /Users/pawel/projects/movies-wt-hookguard [hookguard] 6a2a075956: ancestor of forgejo/main
```

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
`sweep` reaps the sbt servers too. A watch whose runs show no change for 120 min (`$AGENTCTL_WATCH_STALL_MIN`) ends
`WATCH STALLED`.

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

---

## movies helpers that pair with agentctl

- `scripts/identity-measure … --wait` / `scripts/identity-repro … --wait`: print the CI run id and an ETA (the median
  wall time of the workflow's recent runs), then poll with the same 60 s → x1.5 → 300 s backoff. Run them in the
  background.
- The pre-push hook reuses a passed result when the same tree is pushed again: same tree, base, changed paths, checks,
  hook bytes, red-gate state and PREPUSH_SKIP. The key is stored in `~/.cache/kinowo-prepush`. Anything different runs
  every check. `PREPUSH_NO_REUSE=1` forces a full run.

## Troubleshooting

- **`vault <ns> locked`.** Pass the line to the lead: the user runs `! secrets unlock <ns>`. Don't retry in a loop,
  and never try to unlock.
- **Tunnel down** (`Connection refused` on localhost:3000/2222/3010, `GitOps manifests STALE`, a fetch failing).
  Run `agentctl env --check`. The fix is `launchctl kickstart -k gui/$(id -u)/net.bitcashier.forgejo-tunnel`. The
  tunnel restarts itself under launchd, so wait a few seconds and land again. `db-q` kicks it once by itself.
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
