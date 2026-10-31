# Shared brief for movies-repo agents (2026-10-27)

Repo: the movies monorepo (Scala 3, sbt; modules common, identity, worker, web, e2e, testkit; Mongo, one db per country
plus the fleet db). `<remote>` below is the forge's remote: `forgejo` (ssh alias `forgejo-git`, never the Secure Enclave
key, never retry loops on git auth) until the cut-over to GitHub, `origin` (github.com) after it -- `~/bin/agentctl
config show --all` names the forge. Main checkout `~/projects/movies` is the USER's dev and local-mirror checkout: do NOT work in it. Make your own worktree:
`git -C ~/projects/movies worktree add -b <branch> ~/projects/movies-wt-<name> <remote>/main` (after `git fetch <remote>`).

Read first: `~/bin/AGENTCTL.md`, which explains `~/bin/agentctl`: env, land, wait, ci-why, db-q, prepush-check. Also read the repo's CLAUDE.md.

## Landing" section, 2026-10-26)

## Landing
- No pre-merge CI (the owner, 2026-10-26). EVERY change -- identity included -- lands with `~/bin/agentctl land`
  from your worktree. There is no merge queue CI step, and no identity-landing-check LANDABLE is required.
- AGENTS NEVER WAIT ON A LAND (the owner, 2026-10-27). `land` fetches, rebases onto <remote>/main, runs the squash guard
  and prints `QUEUED <id> (<sha>)` within seconds. END YOUR TURN AT QUEUED. The always-on `waiting` processor then, on
  its own:
  1. checks the change in a check tree of its own (`<clone>-wt-check-<n>`, warm), admitted by the laptop's governor
     (`agentctl gov status`; a `Fixes-Red:` fix of a red run goes first) -- or, when the laptop is under memory or CPU
     pressure, on ci-runner's check tier (a `check/<id>` branch running .github/workflows/precommit-check.yml,
     deleted afterwards). The checks are the pre-push hook's: the red gate (at push), the path-selected checks, the
     BASIC tests (`scripts/hooks/local-tests.txt`'s `basic` set) and, when your paths touch an AREA, that area's local
     tests -- `identity` or `page`;
  2. main green (or a tools-only / Fixes-Red change): pushes it -> `LANDED <sha>`, then watches main's Main and
     convergence runs for it -> `WATCH OK / RED / UNKNOWN`;
  3. main red and a production change: holds it -> `WAITING ...`; it lands with the batch (checked once) at the next
     green.
- Every verdict (QUEUED, LANDED, WAITING, NOT LANDED with the failing checks' tail, WATCH ...) goes to
  `~/bin/agentctl inbox` with your worktree as its cwd; the orchestrator reads and routes it. A NOT LANDED or WATCH RED
  of yours comes back to you as a new message. Your final report names your worktree and the QUEUED id.
- `land` itself refuses at once (`NOT LANDED: <why>`) only for a dirty tree, a rebase conflict or the squash guard: fix,
  commit, land again. `agentctl land status` lists the queue; `agentctl land cancel <id>` drops your change (the old `waiting status|cancel` names answer until 2026-11-05).
  (A dispatched red fixer's land, and `land --follow`, still run the checks in-process and end at `LANDED`.)
- Assigned a red build (a run number)? Run `~/bin/agentctl red claim <run> --note "<one line>"` FIRST, so the gate
  status names you as its fixer (`agentctl ci-why`, the status line); land the fix with `Fixes-Red: <run>`.
- Found an agentctl defect? File it: `~/bin/agentctl issue "<what is wrong, where, how to see it>"`. A red fixer never
  edits ~/bin (agentctl has one editor; ~/bin's pre-commit hook refuses a fixer's commit).
- On a WATCH RED routed back to you: if your change caused it, fix forward AT ONCE with a `Fixes-Red: <n>` trailer
  (same final trailer block as Co-Authored-By) and land again; if not, say so with the run number.
- Before committing, run the unit tests of the modules you touched and their dependents, through scripts/sbt-warm (`git diff --name-only <remote>/main | scripts/unit-shards run` does it, split as CI splits them).
- Before landing, check that `git diff --stat <remote>/main...HEAD` lists only your files, and run the test layers your
  change touches (CLAUDE.md). `scripts/identity-measure --footprint` / `identity-landing-check` stay available as
  optional confidence tools for a risky identity change; neither is required, and never run their `--full` mode
  "to be safe" -- main's convergence runs it after the merge.
- There are no PRs and no other branches on the remote (the tools' own `measure/`, `check/` and `land/` refs aside).
- Never squash, always rebase against upstream (owner): `git fetch <remote> && git rebase <remote>/main`, never a local main; one commit per logical step; no squash, no `reset --soft` recommit.
- Never use `PREPUSH_SKIP` or `--no-verify` -- on ANY commit or push, throwaway `measure/` branches included. Never
  `reset --soft` onto a moved main. `agentctl land --no-watch` is for scripts only.
- Merge on green means MAIN green: while a gating workflow is red on main, a production commit waits on `waiting`
  unless it carries a `Fixes-Red: <run>` naming that run; tools-only commits (scripts, docs, tests, fixtures, CI) land
  while red. Use Fixes-Red only when the commit really fixes that run.
- Never edit `test/resources/fixtures/identity-drift-allow.tsv` to get green. Allocation/render budgets
  (`tools.costs.PerformanceBudgets`) move only for a deliberate trade, with the before/after in the commit message.
- No feature switches or flags unless the user asked for them.
- End every commit with: `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`
- Write code that is already scalafmt-clean, and match the surrounding style, comment density and naming. The house rules are enforced by
  specs: DocCommentPlacementSpec, NoSleepInTestsSpec, nanoTime/systemUTC bans, swallowed-failure checks and others. Run the
  specs your change touches.
- Reference: `docs/landing-process.md`, `~/bin/AGENTCTL.md` ("the land queue", "remote checks", "gov", "waiting").

## Efficiency
- Local sbt only through `scripts/sbt-warm "<cmd>" ...`, one warm server per worktree. Compile plus touched specs only.
  Full suites run on CI. No CPU-burning loops.
- One wait loop per CI run. `agentctl wait` runs in the background; don't poll `run view` repeatedly.
- Clean up your own CI runs (measurements, landing-check groups, dispatches: any run you started). The moment one of
  your runs is no longer needed (superseded by a newer commit,
  question answered, branch abandoned), cancel it: `secrets unlock bitcashier`, then
  `~/bitcashier/infra/bin/forgejo-gh run cancel <run id>` (on GitHub: `gh run cancel <run id> -R pawelkrupinski/movies`),
  using the run id, not the index. Delete the measure branch
  too. You decide what is superseded, and a deliberate before/after or bisect pair stays. Never cancel another
  agent's runs. Before reporting done, list your still-running runs and cancel the ones you no longer need.
- If you'd wait more than 30 min, write a state file `~/projects/movies-wt-<name>/STATE.md` and end with a report, rather than idling.
- Prod is off limits: no prod Mongo reads or writes, no prod deploys, no remote shell writes, no secret-store writes. If
  something needs prod, write the exact command into your report for the user to run.
- Before deleting anything beyond your own worktree and branch, list it in your report instead.

## Report
Under 300 words:
- what landed, with SHAs;
- what's left;
- risks;
- any command the user must run.

When done, remove your worktree with `git worktree remove` once it is merged.

- Owner-run steps (prod Mongo, infra push/deploy, secret writes): report them as ONE complete `!` command the owner can paste as-is (tunnel, `secrets get`, a read-only script file, cleanup), never a bare query.
- Any document you write for the owner (design doc, write-up, report): also put it in ~/Desktop/"claude docs"/ as .md and a rendered .pdf (`nix shell nixpkgs#pandoc -c pandoc X.md -s -o X.html`, then Chrome headless `--print-to-pdf`).
