# agentctl simplification, 2026-10-29 onward: progress log

**PACKAGE SPLIT LANDED 23:30 (Agent A, 2ce6ce6): agentctl's code is lib/agentctl/*.py now; edit the parts, not the 73-line entry.**

Agents (owner, 2026-10-29 evening): **Agent A** (core: items 2-7, this log's Agent A sections), **Agent B** (item 1's
workflow and suite speed, 9, 10, 8), **Agent C** (infra: the Actions branch, item 11). Item 1 was finished by Agent A
before the split of work (below).

One maintainer, items strictly in order, each pushed to forgejo `main` before the next. The coordinator reads this.
Baseline (2026-10-29, forgejo/main 67cd130): agentctl 17,335 lines, 798 functions, 58 subcommands; 601 tests, 702 s
serial.

## Item 1 (owner's item 7): CI for the agentctl repository -- DONE

What changed:
- `tests/run.py`: the suite in parallel workers (default min(cpus, 8)), a per-test timeout (SIGALRM, default 60 s;
  a test that ignores it loses its worker after a faulthandler dump), the suite bounded (`--suite-timeout`). Longest
  first by the last run's timings (`~/.cache/agentctl-tests/durations.json`). `agentctl suite` runs it.
- Two waits removed: the hung-sbt fake hung on `--seed` too (60 s per test), and the red tests' tearDown waited 15 s
  for a zombie of its own (now reaped). Two environment dependencies removed: the gate-publish test read the real
  `~/projects/movies`; `allowed_path` let `~/Desktop` through when HOME sat below an allowed root.
- Suite: 605 tests, ~70 s on the laptop with 8 workers (was 702 s), also green with an empty HOME and a bare PATH.
- `.forgejo/workflows/tests.yml`: the suite on ci-runner's `ubuntu-26.04` container tier for every push to main.

Commits (~/bin): e8af14e, adfab84, 705acdd, d4db8a9.

Owner action: Actions are off for the repository. Infra branch **`agentctl-ci`** (local, not pushed; worktree
`~/bitcashier/infra-agentctl-ci`), commit a539a9f1, sets `has_actions` on in `forgejo-settings.nix`. No host changes:

```
! cd ~/bitcashier/infra && git pull --rebase -q forgejo main && git rebase -q main agentctl-ci && git checkout -q main && git merge --ff-only -q agentctl-ci && git push forgejo main && bin/forgejo-settings --dry-run && bin/forgejo-settings
```

The dry run should show the agentctl repository's `has_actions` changing to true. The workflow has not run on
ci-runner yet; its first run comes with the next push to main after that.

## Item 2 (owner's item 1): drop local-store mode -- DONE

What changed:
- Two roles only. `server` (AGENTCTL_HOST=ci-runner or AGENTCTL_ROLE=server): the database in-process, the tick
  (`server_steps`). `sync` (everywhere else): `Store()` is always `RemoteStore` (`store.url`, else
  http://localhost:3014), the loop is `sync_steps`. The laptop's combined phase-1 tick, `LAPTOP_ONLY_STEPS`, the
  laptop-pressure branch of the entry checks, `store import`, `Store.import_lease`, `store use --local` are gone.
- Disaster recovery: no laptop copy is needed -- ci-runner's 24 hourly backups, and design §16's rebuild from the
  land/* refs and Forgejo's runs. Documented in AGENTCTL.md ("store") and the code's ROLES comment.
- Bug found on the way: noting a missing token on the sync recursed (the remote store's call reads the same token);
  guarded and tested.
- Tests: the harness runs in the server role with a closed-port default store URL (no test can reach the real
  store); phase-2 tests that play the laptop say so (`Phase2Base.sync()`).
- `waiting` aliases: kept, with a dated TODO(2026-11-05). The laptop's launchd job still runs `waiting run
  --service`; removing the alias before the owner reinstalls it (`agentctl tick service install`, which kickstarts
  and needs the token again) would stop the sync.

Commits (~/bin): 32715f7, dbe6578 (+ this log).

### Milestone: what the owner runs (pin bump to the item-2 revision)

ci-runner still runs the older pinned revision; item 2 changes the server's code too (server_steps, checks always on
the CI tier). Bump the pin, preview, apply:

```
! cd ~/bitcashier/infra && git pull --rebase -q forgejo main && rev=$(git -C ~/bin rev-parse forgejo/main) && sed -i '' "s/rev = \"[0-9a-f]*\";/rev = \"$rev\";/" nix/hosts/ci-runner/agentctl-pin.nix && git diff --stat && git commit -qam "agentctl: pin ~/bin $rev (simplification item 2)" && git push forgejo main && bin/deploy ci-runner
```

then `! cd ~/bitcashier/infra && bin/deploy ci-runner --apply` (restarts only agentctl-src/store/reconciler).

## Agent A: token regression check (2026-10-29 22:31-22:45)

Not caused by a push: the network dropped, every sync step failed, and after ROUND_ERRORS_MAX rounds the STALLED
condition's own (remote) store call raised StoreUnavailable out of the loop. The processor died, launchd restarted it
(pid 58047, 22:44), and a fresh process holds no token. Re-exec onto new code keeps it (os.execv inherits the
environment). Fixed in 869b48c (the note is never fatal, retried each round), with two regression tests
(tests/test_tick_bounds.py StoreDownNeverEndsTheLoopTest). Laptop-only: no pin bump needed for it.
Note for Agent B: one full-suite run of 869b48c failed one test and the next three passed -- a flaky test somewhere
(name not captured).

## Agent C: infra -- Actions for agentctl, the auto-pin, the safer manual bump (2026-10-29 22:50-23:10)

All on LOCAL infra branches, nothing pushed or deployed (owner does both):
- **Actions** (`agentctl-ci`, a539a9f1, already there from item 1): `has_actions` on. tests.yml needs no secret,
  variable or new label (`ubuntu-26.04` exists; actions/checkout is mirrored). Re-checked with check-nix-formatting.
- **Auto-pin** (`agentctl-autopin` = agentctl-ci + 736ce8a5): `agentctl-autopin.timer` every 5 min runs
  `agentctl-pin auto` on ci-runner: newest agentctl main commit descending from the running one, not marked bad,
  whose latest tests.yml run on main is success (Forgejo's sqlite, read-only, no token) -> host pin, restart
  agentctl-src/-store/-reconciler only, event `pin.bumped`; a revision that does not stay up is rolled back from,
  marked bad, `escalate` event. Infra's agentctl-pin.nix still wins (host pin counts only over the infra pin it was
  written on). Veto: `sudo agentctl-pin hold|release|rollback|status`. Units now run /var/lib/agentctl-src/current.
  `bin/prove-agentctl-pin` (stubs; mutation-checked) proves red/running/untested/other-workflow never ship.
- **Manual bump** (6d46c9e2): infra `bin/agentctl-pin-bump [<rev>] [--apply]`, refusing a non-green tests.yml run.
- Owner steps: phase2-runbook.md "Auto-pin" and ~/Desktop/claude docs/agentctl-autopin.md/.pdf.

For Agent A: the auto-pin writes its events straight into the store's `events` table with sqlite3 (columns at, kind,
item_type='agentctl', item_id='pin', severity, audience='{}', text, refs, dedupe), as ci-lander. A schema change
there must keep those columns, or give me an `agentctl event add` to call instead. It also runs
`/var/lib/agentctl-src/current/agentctl` -- keep that entry point through the package split.
For Agent B: auto-pin ships only on `success` of the LATEST tests.yml run of a commit on refs/heads/main; a flaky
suite therefore delays shipping, never ships red.

## Agent B

### B1: CI and suite speed -- DONE

The workflow (`.forgejo/workflows/tests.yml`, `ubuntu-26.04` = ci-runner's container tier, the label the infra
repo's own workflows use) and `tests/run.py` (parallel workers, a per-test timeout) were Agent A's item 1. Added: the
test `Repo` (a bare remote + clone, built a few hundred times per run) is copied from a per-process template, 0.4 s ->
0.04 s each. Full suite on the loaded laptop (load ~30): 129 s -> 88 s; at load ~10: ~68 s. Commit 7ba70b1.

### B2: test hygiene -- DONE

`tests/harness.py`, imported by test_agentctl before agentctl loads (every test module imports test_agentctl):
- HOME is a temp dir per process; PATH = a harness bin (python3 = the running interpreter, git, node) + the system
  dirs; SDKMAN_*, GIT_*, JAVA_HOME and tokens dropped; git reads the harness's gitconfig. The workflow no longer sets
  git config.
- The per-test timeout (SIGALRM, 60 s, $AGENTCTL_TEST_TIMEOUT) lives in the harness: a hang or busy spin fails with
  its traceback under any runner, plain `python3 -m unittest` included. run.py keeps the hard backstop.
- The clock rule (82fdc89's proxy, universal): every agentctl module -- each `agentctl.*` module after the split too
  -- gets fresh `time` and `subprocess` proxies before and after every test; a test that leaves the stdlib's clock or
  subprocess faked fails.
- agentctl's `_IN_PROCESSOR` flag and token cache are reset per test. This was the flaky test from Agent A's note:
  a processor run left the flag set, and test_token's secrets test then failed at random.
- tests/test_harness.py checks each rule. The suite is green under `env -i PATH=/usr/bin:/bin HOME=/nonexistent`.
Commit 8f054a9.

## Agent A: item 3 (owner's item 6), the package split -- DONE (23:30)

- `~/bin/agentctl` is a 73-line entry: its docstring and the loader. The code is `lib/agentctl/` in 27 parts (core,
  wait, owned, ciwhy, branches, red, fixer, land, gov, slots, landwatch, waiting, queue, remote, batch, tools, cleanup,
  maint, store, server, upkeep, watchdog, inbox, recovery, status, live, cli), listed in order in
  `lib/agentctl/__init__.py` PARTS.
- ONE NAMESPACE, deliberately: the entry runs every part, in order, in its own module namespace -- exactly the old
  file's semantics, so the move changed no behaviour (checked: the parts' concatenation equals the old file except the
  groundwork commit), `agentctl.<name>` is every name, and the tests' `A.<name> = ...` patches still reach every part.
  `import agentctl` with `~/bin/lib` on sys.path loads the same. Real module boundaries (imports between parts) can
  come part by part later.
- The entry finds `lib/` relative to its own real path, so ci-runner's `python3 <pinned tree>/agentctl` works.
- Live safety: first a843bb1 (code_version, the restart gate and the smoke check cover every part) went live, the
  laptop processor re-execed onto it, then the split; the processor (pid 58047) re-execed onto the split with its
  token (23:30).
- Tests that read the source use `code_source()` (the entry and its parts in order).
- Rebased over 869b48c (token fix), 7c3c342 (FIXER_SWAP_GB: now in lib/agentctl/gov.py) and Agent B's harness.

Commits: a843bb1 (groundwork), 955ad34 .. 2ce6ce6 (one part per commit, the suite green before each).
Pin bump: needed for ci-runner to run it (behaviour unchanged, so it can ride with item 4's bump).
