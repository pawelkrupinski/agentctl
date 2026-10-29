# agentctl simplification, 2026-10-29 onward: progress log

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
