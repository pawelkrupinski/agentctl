# agentctl

`~/bin/agentctl` is the orchestration tool for Claude subagents working in the bitcashier repos
(`~/bitcashier/{infra,core,dashboard,invoice-page,version-dashboard}`) and in movies (`~/projects/movies`). All of
these repos have the remote `forgejo`. It replaces the hand-written shell that agents kept getting wrong: poll loops,
fetch/rebase/push/verify sequences, log trawling, DB spelunking. It does that work in fewer, cheaper and safer tool
calls.

- Not on PATH: call it as `~/bin/agentctl`.
- Python 3, standard library only.
- Tests: `python3 -m unittest discover -s ~/bin/tests` (84 tests, mutation tests included).
- `agentctl --help` and `agentctl <cmd> --help` match this page.

## Standard agent workflow

```sh
eval "$(~/bin/agentctl env --print)"        # 1. sbt/java on PATH; a locked vault stops you here with one line
#    ... work in your own worktree, commit ...
heavy-run ~/bin/agentctl land               # 2. rebase + check + push + verify -> LANDED <sha> | NOT LANDED: <why>
~/bin/agentctl wait run <id> &              # 3. Bash run_in_background=true, then END YOUR TURN
~/bin/agentctl ci-why                       # 4. only when land says the red gate refused, or Main is red
```

For an identity change in movies, step 2 is `queue add` instead of `land`, followed by `queue wait` (see
"queue" below): `land` for one non-identity commit, `queue` for anything that needs `identity-landing-check`.

Step by step:

1. Run `env --print` first in any session that builds or pushes. If it prints
   `vault <ns> locked: ask the user to run `! secrets unlock <ns>``, stop there and pass that line to the lead.
2. Work and commit in your own worktree. Never use `reset --soft` onto a moved main.
3. Run `land` from the worktree. Run it under the scratchpad `heavy-run` when the pre-push hook will start sbt, which
   is any Scala, script or workflow change in movies.
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
- **One queue processor.** `queue run` holds an flock on the queue directory, so the kernel frees it when the
  processor dies. Liveness is that lock, never a pid, so a zombie counts as dead. The processor is started with a
  double fork and setsid, so it is nobody's child. It pushes only through `push_argv()` and the pre-push hook, and refuses to run with `PREPUSH_SKIP`.
- **One waiter per target.** `wait` holds a lock per run or file in the scratchpad. A second caller attaches to the
  live waiter's verdict file and does not poll.
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

### land: rebase, check, push, verify

```
agentctl land [--repo PATH] [--branch B] [--stash]
```

Steps, in order:

1. fetch (retried once after 15 s, since a tunnel restart takes seconds);
2. `git rebase forgejo/main`; on a conflict it aborts and lists the files;
3. `prepush-check` against forgejo/main;
4. `git push forgejo <sha>:refs/heads/main`. The repo's pre-push hook runs, with sdkman's sbt/java first on PATH and
   JAVA_HOME set;
5. on a race (`non-fast-forward`, `cannot lock ref`, `fetch first`, `failed to update ref`), refetch, rebase and
   retry, up to 3 times;
6. fetch again and verify with `merge-base --is-ancestor`, or `git cherry` for patch-equivalent commits.

The last line is `LANDED <sha>` or `NOT LANDED: <reason>`. Possible reasons:

- dirty tree;
- rebase conflict;
- prepush-check refusal;
- red gate (followed by the `ci-why` summary);
- hook failure (its last 25 lines; the full log is in `scratchpad/agentctl-land-*.log`, plus a tunnel hint when
  Forgejo was unreachable);
- still racing;
- pushed but not on main.

A dirty tree is refused unless you pass `--stash`. That stashes the changes under a named stash
(`agentctl-land-<pid>-<ts>`), finds it again by its sha (the stash list is shared by every worktree), and restores it
afterwards. Exit: 0 landed, 1 not.

```
$ ~/bin/agentctl land          # from ~/bitcashier/infra-wt-agentctl-doc
agentctl: pushing c972d56a2b to forgejo/main (attempt 1/4; hook log: …/agentctl-land-infra-wt-agentctl-doc-72692.log)
prepush-check: forgejo/main...c972d56a2b49: 1 file changed, 8 insertions(+)
LANDED c972d56a2b4932077baaa40af95d388ab500bb03
```

Use it for every push to main. Don't use it for `measure/` branches: `identity-measure` pushes those itself.

### queue: batch landings, one identity landing check per batch

```
agentctl queue add [--identity] [--repo PATH]        # from your worktree
agentctl queue wait [<entry>] [--timeout S]          # LANDED <sha> | REJECTED <why>
agentctl queue status
agentctl queue cancel <entry>                        # a queued entry, not one in the current batch
agentctl queue resume                                # lift a hold that waits for it; refreshes the processor's token
agentctl queue requeue <entry> [--last]              # a wrongly rejected entry back (original place, or last)
agentctl queue run [--once]                          # the processor; `queue add` starts it
```

**When to use which:**

- `land`: a single change that needs no identity landing check: tools, docs, non-identity code. It is unchanged.
- `queue`: any change that needs `scripts/identity-landing-check` (identity/, worker identity adapters, the stage,
  the projection, title rules, identity fixtures). Don't take a landing-check slot or run the check yourself: the
  queue does both. Several agents queued at once share ONE check instead of one each.

`queue add` records the worktree, branch, HEAD, its merge-base with forgejo/main, and whether the entry needs the
landing check. A path needs it when it matches the pre-push hook's identity-change list (`identity/`,
`worker/src/main/scala/services/identity/`, title-rules/words, `services/titlerules/`, `test/resources/fixtures/identity-`),
or names identity, matcher, resolver, resolution or projection code outside `scripts/`, `docs/`, `.claude/` and the
CI trees. `--identity` forces the check. The entry also records whether it touches a production path (the hook's
`is_production_path`) and whether every production commit carries `Fixes-Red:`. A worktree has at most one open entry.
Only committed work is queued. If no processor is running, `queue add` starts one detached, in its own session, so it
outlives your turn. It logs to `<queue dir>/processor.log`. The queue directory is `~/.cache/agentctl/queue/<repo>`
(`$AGENTCTL_QUEUE_DIR` overrides it), and the processor builds batches in its own worktree, `<queue dir>/work`.

**The processor's token.** The detached processor runs in its own session, so it cannot read a vault that was unlocked
in yours. `queue add` and `queue resume` resolve FORGEJO_API_TOKEN in your session (`$FORGEJO_API_TOKEN`, else
`secrets get`) and hand it over in `<queue dir>/token` (mode 0600; never in argv, logs or status). Before any check
dispatches, a preflight confirms the processor has a token and that Forgejo accepts it. The token is passed on as
`FORGEJO_API_TOKEN` to the check, the hook and the gate. Without one, the queue HOLDs with
`no-token: run `secrets unlock movies`, then `agentctl queue resume``.

How the processor works, one batch at a time:

1. It fetches and takes every queued entry, in order. Entries added while a batch runs go into the next batch.
2. **Gate.** When `ci-why` says main is red for the gate, production entries are held (their note says why and the
   processor polls with backoff), and only tools-only and `Fixes-Red:` entries go ahead, as the hook allows.
3. **Build.** Starting from forgejo/main, it rebases each entry onto the tip so far (`git rebase --onto`) and runs
   prepush-check on it. An entry that conflicts, or that prepush-check refuses, is rejected alone, and the message
   says whether it conflicts with main or only with an earlier entry.
4. **Check.** When any entry in the batch needs it, it runs ONE `scripts/identity-landing-check --full <tip>`, holding
   one of the shared `/tmp/movies-landing-slot-{1,2}` slots. Verdicts are cached by (main, tree).
5. **BLOCKED: blame needs proof.** A failure's *signature* is the set of specs/steps the check lists under `BLOCKED`.
   - **Main first.** It runs the same check on bare forgejo/main. The verdict is cached per main sha, so it is paid
     at most once per main commit, and only when a batch goes red. Any red there means the batch is not to blame:
     nobody is rejected, and the queue HOLDs with `MAIN RED <signature> at <sha>`. It resumes by itself when main
     moves to a new sha and the gate is green. Only a green main allows a bisect.
   - **Bisect.** It checks the first half of the batch, then halves again, until it finds the first red prefix after
     a green one.
   - **One re-check** of that red prefix, run fresh. Red again with the same signature: that entry is rejected with
     the failing output, the green prefix before it is pushed, and the entries after it are requeued, to be checked
     again without it. Green on the re-check, or a different signature: it is a **FLAKE**. Nobody is rejected and
     nothing lands. The queue HOLDs with `FLAKY <spec(s)> (batch <id>, runs <logs>)`, appends a line to
     `<queue dir>/flaky.log`, and the processor stops until `agentctl queue resume`. It never picks a winner between
     a red and a green run (movies: never rerun to green). `resume` also clears the verdict cache.
   - **No verdict** (exit 2: infra, dispatch or token trouble) is never an entry's fault, so nothing is ever
     REJECTED for it. The queue HOLDs with `no-verdict`, requeues everyone, retries with backoff, and after 5
     attempts in a row waits for `queue resume`. A verdict lifts the hold.
6. **Push** the tip with `git push forgejo <tip>:refs/heads/main`. The repo's pre-push hook runs in the work tree,
   which is at the tip. If main moved since the batch was built, or the push races, it rebuilds and rechecks. If the
   red gate refuses, it holds. If the hook fails on a multi-entry batch, it retries each entry alone. If it fails on
   a single entry, that entry is rejected with the hook's tail.

`queue wait` polls the entry file (no Forgejo calls), restarts a processor if none is running, and prints the verdict
last: `LANDED <sha>` (the rebased commit on main), `REJECTED <why>` with the detail lines above it, or
`HELD <why>` (flaky, no-token, no-verdict) when the queue waits for `queue resume`. HELD is not your fault unless your spec is named: report
it and stop. A `MAIN RED` hold keeps `wait` waiting, since it resumes by itself; `queue status` shows every hold.
Exit: 0 landed, 1 rejected, 2 timeout (12 h by default), 3 HELD. Start it with `run_in_background=true` and end your turn. Your branch is not
rewritten: the landed commits are patch-equivalent to it, so `cleanup` recognises the worktree as merged.

```
$ ~/bin/agentctl queue add                      # from ~/projects/movies-wt-relay
QUEUED 20261026-101500-relay-3f2a: 2 commit(s) up to 9c1e0a2b3d (relay); identity landing check: yes (identity/src/main/scala/services/identity/RelayStage.scala); production; position 2
wait for it (Bash run_in_background=true, then END YOUR TURN): ~/bin/agentctl queue wait 20261026-101500-relay-3f2a
$ ~/bin/agentctl queue wait 20261026-101500-relay-3f2a
LANDED 4be0c1f9...
```

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

It lists wait loops and sbt servers that are orphaned: their worktree is gone, their owning shell died, every target
run finished, or they duplicate an older loop on the same runs. `--kill` sends SIGTERM to them and their children.
Output is `no orphans`, or one line per process with the reason.

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
