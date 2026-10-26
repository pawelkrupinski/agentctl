# agentctl

`~/bin/agentctl` is the orchestration tool for Claude subagents working in the bitcashier repos
(`~/bitcashier/{infra,core,dashboard,invoice-page,version-dashboard}`) and in movies (`~/projects/movies`). All of
these repos have the remote `forgejo`. It replaces the hand-written shell that agents kept getting wrong: poll loops,
fetch/rebase/push/verify sequences, log trawling, DB spelunking. It does that work in fewer, cheaper and safer tool
calls.

- Not on PATH: call it as `~/bin/agentctl`.
- Python 3, standard library only.
- Tests: `python3 -m unittest discover -s ~/bin/tests` (93 tests, mutation tests included).
- `agentctl --help` and `agentctl <cmd> --help` match this page.

## Standard agent workflow

```sh
eval "$(~/bin/agentctl env --print)"        # 1. sbt/java on PATH; a locked vault stops you here with one line
#    ... work in your own worktree, commit ...
~/bin/agentctl land                         # 2. Bash run_in_background=true, END YOUR TURN: lock, rebase, local tests, push, watch CI
~/bin/agentctl wait run <id> &              # 3. Bash run_in_background=true, then END YOUR TURN
~/bin/agentctl ci-why                       # 4. only when land says the red gate refused, or Main is red
```

Every change lands with `land`, identity changes included (2026-10-26: no pre-merge CI; the merge queue is retired).
In movies the pre-push hook runs the basic tests plus the identity / page area tests the change touches, locally, and
after LANDED `land` watches main's Main and convergence runs for your sha and ends `WATCH OK` / `WATCH RED` /
`WATCH UNKNOWN`. While main is red, a production change prints `WAITING <sha> (n changes queued on waiting)` and land
keeps running until the `waiting` branch lands with it once main is green.

Step by step:

1. Run `env --print` first in any session that builds or pushes. If it prints
   `vault <ns> locked: ask the user to run `! secrets unlock <ns>``, stop there and pass that line to the lead.
2. Work and commit in your own worktree. Never use `reset --soft` onto a moved main.
3. Run `land` from the worktree with the Bash tool's `run_in_background=true`, then END YOUR TURN. You are re-invoked
   when it exits: `NOT LANDED: <why>` (fix and land again), or `LANDED <sha>` followed by the watch's verdict. On
   `WATCH RED` your change may have broken main: read the ci-why summary it prints and fix forward at once with
   `Fixes-Red: <run>`. (There is no `heavy-run` wrapper on this laptop; the landing lock keeps one landing hook's JVM
   at a time.)
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
  `$GH_TOKEN`) and go only into a request header.
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
- remote `measure/*` branches whose runs have all finished, or that are more than 24 h old with no run.

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

For movies it reads exactly the workflows that the pre-push red gate (`check_redgate`) reads: *Main*,
*Identity model convergence* and *Record scrape fixtures*. For each, it takes the newest decided run on main
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
Record scrape fixtures: GREEN (run 1630 #801)
```

Use it when the red gate refused your push, or before you decide whether a red is yours. Don't read logs by hand
first. A genuine `Fixes-Red:` trailer is only for a commit that really fixes that run.

### land: lock, rebase, check, push, verify, watch

```
agentctl land [--repo PATH] [--branch B] [--stash] [--no-watch]     # agents: Bash run_in_background=true
```

Steps, in order:

1. take the repository's landing lock (an flock per git common dir under `~/.cache/agentctl/land/`), waiting for
   another lander's to be released (`waiting for the landing lock (…), held by: pid … <worktree> since …`;
   `$AGENTCTL_LAND_LOCK_TIMEOUT`, default 4 h);
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
     undecided after 3 h (`$AGENTCTL_WATCH_TIMEOUT`) or Forgejo's runs could not be read 10 times in a row.
   - Other repos: `watch: no CI watched for <repo>`, exit 0.

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

### waiting: production changes held while main is red, landed together on green

```
agentctl waiting status          # the changes on `waiting`, the processor, the gate
agentctl waiting cancel <id>     # drop one (its land ends NOT LANDED: cancelled); the branch is rebuilt without it
agentctl waiting run [--once]    # the processor; land starts it detached
```

The owner (2026-10-26): "when waiting for main to go green, merge all waiting changes into one waiting branch (always
use the same name) and merge it when main is green".

- **The branch** is the local `waiting` of the main clone, in its worktree `<clone>-wt-waiting`
  (`~/projects/movies-wt-waiting`). It exists only while changes wait.
- **Enqueueing** (inside `land`, under the landing lock): fetch, create `waiting` from forgejo/main if missing,
  cherry-pick the change's commits onto it, and run the repo hook's `--checks-only forgejo/main..waiting` there (every
  check but the red gate, over the COMBINED tip: the basic tests plus every touched area's). A conflict or a failure
  resets the branch and rejects only that change, with the output. Otherwise `WAITING <tip> (n changes queued on
  waiting; entry <id>)`, and land waits on its entry file (no Forgejo calls; restarts a dead processor; 24 h bound).
- **The processor** (one per repo, an flock; detached, `start_new_session`; log `processor.log` in the state dir
  `~/.cache/agentctl/waiting/<repo>/`) polls the gate (60 s backing off to 5 min). Green: under the landing lock it
  fetches, rebuilds `waiting` on forgejo/main entry by entry (a change that no longer applies is rejected alone), runs
  the hook's `--range` dry run (gate included), and pushes the whole branch to main in ONE push through the hook.
  Then every entry is LANDED (its own commit's sha on main), the branch and its worktree are deleted, and each
  waiting `land` prints `LANDED <sha>` and goes on to its WATCH. The gate red again at the dry run: back to waiting.
  A failing batch is searched prefix by prefix and only the first failing change rejected; when no single change
  fails (flaky or infra), the whole batch is rejected with the output rather than one guessed.
- **Bypass:** tools-only changes and `Fixes-Red:` fixes never wait (the hook lets them land while red), nor does
  anything when main is green. A repo hook without `--checks-only` (movies before the change that added it) disables
  waiting: land behaves as before.

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
revert` commits are exempt for their own files. `land` runs it, and so does movies' hook. Exit 0 ok, 1 refused.

```
prepush-check: forgejo/main...c972d56a2b49: 1 file changed, 8 insertions(+)
prepush-check: ok
```

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
agentctl orphans [--kill]
```

It lists owner records that are strays (a dead process's record and the runs it never cancelled; an owner still on
old code; an owner whose cwd is gone), wait loops and sbt servers that are orphaned (their worktree is gone, their
owning shell died, every target run finished, or they duplicate an older loop on the same runs). `--kill` cancels a
dead owner's runs and removes its record, and sends SIGTERM to the rest (an owner's handler cancels its own runs).
Output is `no orphans`, or one line per stray with the reason.

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
