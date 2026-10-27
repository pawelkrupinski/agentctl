# Shared brief for movies-repo agents (2026-10-27)

Repo: the movies monorepo (Scala 3, sbt; modules common, identity, worker, web, e2e, testkit; Mongo, one db per country
plus the fleet db). Remote `forgejo` (ssh alias `forgejo-git`, never the Secure Enclave key, never retry loops on git
auth). Main checkout `~/projects/movies` is the USER's dev and local-mirror checkout: do NOT work in it. Make your own worktree:
`git -C ~/projects/movies worktree add -b <branch> ~/projects/movies-wt-<name> forgejo/main` (after `git fetch forgejo`).

Read first: `~/bin/AGENTCTL.md`, which explains `~/bin/agentctl`: env, land, wait, ci-why, db-q, prepush-check. Also read the repo's CLAUDE.md.

## Landing" section, 2026-10-26)

## Landing
- No pre-merge CI (the owner, 2026-10-26). EVERY change -- identity included -- lands with `~/bin/agentctl land`
  from your worktree. There is no merge queue any more (`agentctl queue` prints a retirement note), and no
  identity-landing-check LANDABLE is required.
- Start it with the Bash tool's `run_in_background=true`, then END YOUR TURN. It:
  1. waits for the repository's landing lock (one lander at a time; a line names the holder);
  2. fetches, rebases onto forgejo/main, runs the squash guard;
  3. runs the pre-push hook as a dry run: the red gate, the path-selected checks, the BASIC tests
     (`scripts/hooks/local-tests.txt`'s `basic` set: every test config compiled, the snapshot/e2e and deploy specs)
     and, when your paths touch an AREA, that area's local tests -- `identity` (identity/, worker identity code and
     tests, title rules, identity fixtures) or `page` (web assets, twirl templates, page specs, Playwright). Each area
     set runs in under 15 min warm on the laptop; anything longer is main's CI's;
     WHILE MAIN IS RED, a production change (one without a `Fixes-Red:` naming the red run) is not refused: it is
     cherry-picked onto the ONE local `waiting` branch (worktree ~/projects/movies-wt-waiting) and checked there on
     the combined tip (`pre-push --checks-only`): `WAITING <sha> (n changes queued on waiting)`. Land keeps running;
     once main is green a background processor rebuilds `waiting` on main, pushes the whole branch in one push, and
     your land continues at step 5 with `LANDED <sha>`. Tools-only and Fixes-Red changes skip `waiting`;
  4. pushes (the hook reuses the dry run's pass), verifies `LANDED <sha>`, releases the lock;
  5. WATCHES main's CI for your sha: Main's run for it (or the newest containing it), and the first Identity model
     convergence run on main started after your push that contains it (none within 60 min: said, not waited for).
- Green main: your change lands directly; its local checks run in parallel with other landers' (outside the lock).
  Red main: a production change joins the ONE `waiting` branch and lands with the others in one push at green.
- Assigned a red build (a run number)? Run `~/bin/agentctl red claim <run> --note "<one line>"` FIRST, so the gate
  status names you as its fixer (`agentctl ci-why`, the status line); land the fix with `Fixes-Red: <run>`.
- END YOUR TURN as soon as land prints `WAITING <sha> ...` or `LANDED <sha>`. Do NOT stay alive for the CI watch:
  land keeps running detached, and every verdict (LANDED, WATCH OK/RED/UNKNOWN, NOT LANDED) goes to
  `~/bin/agentctl inbox`, which the orchestrator reads and routes; a WATCH RED or NOT LANDED for your change comes
  back to you as a new message. Your final report names your worktree and the WAITING entry or LANDED sha.
- If land prints `NOT LANDED: <why>` before WAITING/LANDED, it's yours to fix (failing check tail, rebase conflict, a
  conflict with changes on `waiting`): fix forward, commit, land again.
- On a WATCH RED routed back to you: if your change caused it, fix forward AT ONCE with a `Fixes-Red: <n>` trailer
  (same final trailer block as Co-Authored-By) and land again; if not, say so with the run number.
- Before landing, check that `git diff --stat forgejo/main...HEAD` lists only your files, and run the test layers your
  change touches (CLAUDE.md). `scripts/identity-measure --footprint` / `identity-landing-check` stay available as
  optional confidence tools for a risky identity change; neither is required, and never run their `--full` mode
  "to be safe" -- main's convergence runs it after the merge.
- There are no PRs and no other branches on the remote.
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
- Reference: `docs/landing-process.md`, `~/bin/AGENTCTL.md` ("land", "waiting").

## Efficiency
- Local sbt only through `scripts/sbt-warm "<cmd>" ...`, one warm server per worktree. Compile plus touched specs only.
  Full suites run on CI. No CPU-burning loops.
- One wait loop per CI run. `agentctl wait` runs in the background; don't poll `run view` repeatedly.
- Clean up your own CI runs (measurements, landing-check groups, dispatches: any run you started). The moment one of
  your runs is no longer needed (superseded by a newer commit,
  question answered, branch abandoned), cancel it: `secrets unlock bitcashier`, then
  `~/bitcashier/infra/bin/forgejo-gh run cancel <run id>`, using the run id, not the index. Delete the measure branch
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
