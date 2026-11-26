# agentctl

`~/bin/agentctl` is the orchestration tool for Claude subagents working in the bitcashier repos
(`~/bitcashier/{infra,core,dashboard,invoice-page,version-dashboard}`) and in movies (`~/projects/movies`). All of
these repos live on GitHub, remote `origin` (`<remote>` on this page; `$AGENTCTL_REMOTE` names another); agentctl
reads CI through `gh` and calls the agentctl store with the store token. It replaces the hand-written shell that agents kept getting wrong: poll loops,
fetch/rebase/push/verify sequences, log trawling, DB spelunking. It does that work in fewer, cheaper and safer tool
calls.

- Not on PATH: call it as `~/bin/agentctl`.
- Python 3, standard library only.
- Tests: `~/bin/agentctl suite` from the worktree (= `python3 tests/run.py`, which takes a governor ticket of class `suite`,
  TWO runs at once when the cores allow, `$AGENTCTL_SUITE_CAP`: more agents queue instead of piling up on the laptop). tests/run.py runs
  the suite in parallel workers (`-j N`, default min(cpus, 8)), each test under a timeout (`--timeout S`, default 60:
  the test FAILS with its traceback instead of hanging the suite), the whole suite bounded (`--suite-timeout`); about
  70 s on the laptop. `agentctl suite test_reds` runs the tests whose id contains a pattern. Mutation tests included.
  Run the touched modules while working and the full suite once, before landing.
- CI: `.github/workflows/tests.yml` runs the same suite, in two shards (`tests/run.py --shard K/N`: one 4-cpu runner
  outgrew the suite's bound), for every push to main (a fresh runner: no HOME, sdkman or
  movies clone to lean on). Green there is what ci-runner's auto-pin reads.
- `agentctl --help` and `agentctl <cmd> --help` match this page.
- Coding and testing standards: `~/bin/CLAUDE.md` (movies' standards: the commit gate, coverage before a refactor,
  SOLID, one source of truth per concept, cleanup). THE RATCHETS: `hooks/pre-push` runs `quality/ratchet.py` after the
  suite -- the quality baseline (`quality/quality-baseline.tsv`: cognitive / cyclomatic 10, inner defs 2, function 50
  lines, part 500, 50-token clones, reference cycles) and the design baseline (`quality/design-baseline.tsv`: the movies design rules, CLAUDE.md "Design") and the types baseline (`quality/types-baseline.tsv`:
  `mypy --strict` errors per part). Rows only fall (`ratchet.py update`); a rise needs a `Complexity:` / `Length:` /
  `Duplication:` / `Coupling:` / `Typing:` / `Naming:` / `Design:` trailer. Tools pinned in `quality/tools.lock`, installed by the hook
  (`quality/tools.py`); a missing tool blocks the push.
- The design behind it: `~/bin/docs/landing-reconciler-design.md`. Phase 2 (the store and the reconciler on
  ci-runner): `~/bin/docs/phase2-runbook.md`.

## Standard agent workflow

```sh
eval "$(~/bin/agentctl env --print)"        # 1. sbt/java on PATH; a locked vault stops you here with one line
#    ... work in your own worktree, commit ...
~/bin/agentctl land --until-done            # 2. movies agents' LAST step (run_in_background): submit, wait, DONE <sha> ... worktree removed
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
3. Movies agents end with `land --until-done` from the worktree (Bash `run_in_background=true`, then END YOUR TURN):
   it queues, waits for the verdict and ends `DONE <sha> ... worktree removed` (the worktree and branch gone, every
   local commit's patch proven on main), or `NOT LANDED: <why>` + `next: <command>`; report once, after that line (see
   "land --until-done"). A plain `land` prints `QUEUED <id>` within seconds (or `NOT LANDED: <why>` for a dirty tree, a
   rebase conflict, the squash guard) and its verdicts come through the inbox. A WATCH RED of yours comes back to you
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
- **Items and their machines.** Every state has a deadline and an automatic exit, so nothing waits forever. THE TABLE
  IS THE LAW (2026-11-20): `Store.transition` takes only a move `MACHINES` lists (a listed exit, or a self-move); any
  other raises `IllegalTransition`, changes nothing and leaves one `<type>.illegal` event naming it. Turning that on
  found nine moves the code made that the table never listed (a change cancelled, superseded or landed by content
  from any open state but pushing; a waiting change checked in the batch at green; a pending red fix back to its
  holder; a batch blocked-red from assembling, held after a failed push), now listed with why. tests/
  test_machine_properties.py: random event sequences take exactly the listed moves, every deadline move is legal,
  no reachable state can stall in silence:
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
    fix is gone/None is offered again (escalated), its fix set aside (`unverified`). A set-aside fix stays aside by
    CONTENT too: a rebased or pre-rebase copy of it (another sha, the same patch-id, read even when only a change
    item names it) never puts the red back in fix-pending. An outside red's landed fix is verified by dispatching its
    workflow with the failing run's inputs; when that is refused, the newest decided run of that workflow on main
    containing the fix decides (the verify line says so), and when its workflow has no file on main no run can ever
    prove it: it is offered at once. A red is named by its WORKFLOW, never a run's `run-name` (GitHub's REST `name`;
    the forge names runs from the workflows page, and a red opened under a run-name takes its workflow's name from its
    failing run's file, `RENAMED:` in its history). The red tick fetches main into its clone when the snapshot names a
    main head the clone lacks, so a `Fixes-Red:` commit pushed from anywhere links its red; a blocked red whose fix is
    linked goes to fix-pending with its owner block lifted and its question withdrawn. Every candidate fix is judged in
    turn (queue entries, then landed `Fixes-Red:` commits, then the red's own fixers' `LANDED <sha>` verdicts once that
    sha is on main): a refuted or set-aside one -- a stale `waiting` entry of an earlier spell -- never hides the next
    (784badc4406e.4, 2026-11-13). A landed fix is a commit ON MAIN: a change item's sha main lacks follows its patch
    to the main commit carrying it (a re-cut on landing), else it is no fix; a fix landed before the red's spell opened
    is none either, and a red already fix-pending on such a sha is corrected or offered again (b3f51c5166b0.2, the
    phantom b6d3d8235, 2026-11-16). A red whose fix is ANOTHER red's commit not on main yet waits on it: the fixer ends
    `WAITS ON <sha> (red <key>): <why>` (or a NOT LANDED / GAVE UP naming `<sha> (... red <key>)`), the red goes to
    fix-pending with fix status `waits-on` -- never a failed decided action, never an owner item -- and the reconciler
    resolves it: the sha (or its copy, or that red's own landed fix) on main -> verified like any landed fix; the sha
    set aside by that red, or that red closed without it -> offered again (e948278d144a, 2026-11-16). A wait that
    would close a CYCLE (that red waits, directly or down its chain, on this one) is no wait, and one found standing is
    offered again: neither side could land first, and each one's holder being the other kept W2 quiet. A fixer ending `LANDED <sha>` with the sha on main puts its red in fix-pending at
    once, never offered again nor on the owner; an outside red's landed fix waits, quietly, for its workflow's next
    run, which verifies it (green) or refutes it (back to a fixer). When that workflow has an `on.schedule` cron (the
    movies scheduler, `infra/nix/files/scheduled_dispatch.py`, dispatches the same slots) its VERIFY DEADLINE is the
    first slot after the landing + the p90 of its recent finished runs + `RED_VERIFY_SLACK` (2 h): until then the red
    reads `awaiting next scheduled run at HH:MMZ` -- level 0, no escalation, no inbox line (Record scrape fixtures
    #198, 2026-11-19: a fixed 3 h timer showed it escalated at 07:23Z for a 23:31Z nightly). At the deadline a run of
    it still undecided that will decide the fix (created since the slot, or containing the fix) holds it again, quietly;
    else -- A MISSED DEADLINE IS NEVER SILENT (2026-11-21, below) -- it goes back to a fixer at once, escalated, saying
    whether the slot's run ended without deciding it or the scheduler never dispatched it. With no schedule it waits up
    to `RED_FIX_NEXT_RUN` (30 h). **VERIFY PROMPTLY BY DEFAULT** (the owner, 2026-11-21, "structural fix for the order
    independence being abandoned": Order independence #94's fix waited ~24 h for its 02:40Z cron, nobody at work): a
    workflow with `workflow_dispatch` is DISPATCHED to verify a landed fix in the tick that sees it land (a scheduled
    one at once; an unscheduled one after `RED_FIX_RUN_START`, 30 min, for a push-triggered run to come first) unless
    the repo's one per-workflow registry, `.github/red-workflows.tsv` on main, opts it out: tab-separated `workflow
    verify_dispatch (yes|no)  replay (a command, `{run}` = the red's failing run id, or -)  why`, `#` comments. No row
    is yes. A `no` row is for a costly workflow (over ~60 runner-minutes a run, or a paid service): movies opts out
    Record scrape fixtures (paid proxies), Nightly stress (~76 runner-min), Identity drift explorer (~225) and Identity
    family explorer (~254); those wait for their slot. Order independence (~204) is dispatched all the same: the owner
    approved its cost (2026-11-21, "kick off this one too": its abandoned red is what this rule is for).
    **A FIX WITH NO REPLAY KEEPS ITS FIXER** (the owner, 2026-11-21: #94's `Fixes-Red: 94`, a853b2ee4, was diagnostics
    only, and its fixer was released): for a red outside the gate, a `Fixes-Red:` change whose submission carries no
    `Fix-Replay:` agentctl ran and saw base=FAIL head=PASS (the change's `replays`) is UNPROVEN -- the red goes
    fix-pending with `fix.unproven`, shown as `fix-unproven`, and its holder's lease is NOT revoked (RED-NOTE.md in its
    worktree says so): the red stays in hand until its verification run decides it -- green: FIX OK, the holder
    released; the same failure: FIX FAILED, back to that same holder (leased), the fix refuted. A gate red keeps its
    own FIX REPLAY enforcement (below) and is released as before. **DIAGNOSTICS ARE NO FIX:** a commit that only
    collects evidence says `Diagnoses-Red: <run>` instead: it never links its red (no fix-pending, nor through what it
    changes of the failing step), and once it lands its red's workflow -- when dispatchable as above -- is dispatched on
    main, once per change, and the first decided run after it is told to the same holder (RED-NOTE.md). Movies'
    pre-push trailer lint suggests `Diagnoses-Red` for a `Fixes-Red` commit touching only CI or report plumbing.
    **A MISSED DEADLINE IS NEVER SILENT:** a fix-pending (or fix-unproven) red past its `verified by` deadline with no
    run that decides it -- the slot passed, or its verifying dispatch never produced a run -- is offered again,
    escalated (`red_fix_unverifiable`): its live holder takes it back, else the offer's own escalation (RED UNCLAIMED)
    reaches the owner. red.py `red_workflow_policy`, redmove.py `red_fix_proven`, `red_keep_holder`, `red_diagnosis_step`,
    redverify.py `red_verify_scheduled`; tests: `tests/test_red_in_hand.py`, `tests/test_verify_wait.py`. THE REPO'S STEP AFTER THE REBASE (2026-11-20): a repo's executable
    `scripts/land-before-queue <origin/main> <head>` runs on the rebased change before its checks, and may commit onto it
    (movies: the identity stamp carry's rows); the commit rides the same push. It never refuses a land: a failure or a
    timeout (`$AGENTCTL_LAND_BEFORE_QUEUE_SECONDS`, 1200) leaves the change as it was. The queue PICKS the change's
    commits again -- the check tree's rebuild, a moved main at push, a batch -- so what the step commits must never
    depend on the shas it saw: movies' carry rows cited them and every rewritten carry was refused at push (one carry
    landed 2026-11-21..23); its rows now name each range's commit count and patch-id, and its check finds the range again
    in the pushed history (movies `scripts/identity_stamp_carry.py`, "THE QUEUE REWRITES"). FIX REPLAY: when a red's workflow row names a `replay`, `land` warns on a
    push whose `Fixes-Red:` names that red unless one of its commits carries `Fix-Replay: <the command> base=FAIL
    head=PASS` (the replay run at origin/main and at the fix), naming the command to run (Alert backtest 5254bffda9d2,
    2026-11-19: seven fixes landed unreplayed and the nightly refuted each; policy `fix-replay-outside`, warn-only by
    the owner's 2026-11-20 decision). **FOR GATE REDS IT IS ENFORCED** (the owner, 2026-11-20: "3 approve and enforce
    for gate reds"; policy `fix-replay`): a `Fixes-Red:` naming a red of Main, Identity model convergence or Identity
    takes must carry `Fix-Replay: <command>`, which `land` RUNS on the rebased head -- at origin/main and at the head,
    each in a throwaway detached worktree (`$AGENTCTL_FIX_REPLAY_TIMEOUT`, 30 min a side; logs under the state root's
    `fix-replay/`) -- and the fix counts only on base=FAIL head=PASS; the proof rides with the submission (`replays`)
    and the server's check refuses a gate fix without one. The escape hatch, for a red that genuinely cannot replay
    (infra, a CI-only race): `Fix-Replay: none — <why>` (`--` or `-` will do for the dash), a specific reason of 30+
    characters with no "n/a", "flaky" or "can't"; movies' weekly Coupling exemptions report lists them
    (`scripts/ci/fix_replay_exemptions.py --since 7d`) for the owner to strike. **AGENTCTL'S OWN REVERT USES IT**
    (2026-11-21, Main #2806-#2808: blame's revert of the culprit carried no `Fix-Replay:`, the rule refused it and main
    stayed red ~1 h until a hand revert landed): blame's revert and the fixer's revert fallback write `Fix-Replay: none
    — agentctl blame's revert of <change>: <workflow> #N's parent <sha> passed the same check, ...` (the fallback: `...
    revert fallback of <sha>: ... outlived two fix attempts ...`) in their final trailer block -- a revert puts main back
    on code that passed, nothing new to replay -- and the owner's weekly list shows them like any other; the rule itself
    is unchanged. redreplay.py `agentctl_revert_fix_replay`; tests: test_blame `test_the_reverts_fixes_red_passes_the_gates_fix_replay_rule`,
    test_fix_replay `test_the_revert_fallbacks_fixes_red_passes_the_gates_fix_replay_rule`. redreplay.py `fix_replay_refusal`,
    `run_fix_replays`; slots.py `fix_replay_blocks`; tests: `tests/test_fix_replay.py`. A fix-pending self-move (a fix landing, followed, re-cut) is never an
    escalation unless it says so. A cancelled fix change follows only a change of its worktree submitted after it
    (`submitted_at`, else the change id's stamp), and the invariant waits out `RED_FIX_REQUEUE_GRACE` as the red's own
    step does (Alert backtest #4, 2026-11-19: it followed an 11-11 change of the reused worktree, whose phantom tip
    made the same tick's INVARIANT fire "no fix on its way for 0 min"). An old outside red no fresh run
    can check (no `workflow_dispatch`, a refused dispatch, its file gone) is never retired: it is offered with an
    `old_note` and a fixer decides. A CHECK NEVER GREEN IS NOT TRACKED (the owner, 2026-11-19, Alert backtest #9:
    "don't alert in status line until it gets green for the first time"): a red of a workflow with no green run on
    main ever -- or, in a workflow that has one, of jobs that never passed there -- opens no item (no offer, fixer,
    owner ask, inbox line, status-line or ci-why mention, invariant); an open one is closed quietly (`untracked:
    never green ...`). `red status -v` lists them under `untracked (never green)`. Ever green comes from GitHub's run
    history on main (the snapshot's runs, else `run list --status success`; a job: its success in a green run, every
    green run read), kept in the store (meta `ever_green`) once seen; a never-green answer is asked again after
    `EVER_GREEN_RECHECK` (10 min); anything unread keeps the red tracked. The first green run makes the next red an
    ordinary one. A red of a `workflow_call`-only workflow (only a parse failure gives one
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
  tunnel watches, the gate line, the red tick (observe, offer, dispatch), the conditions, the stats (records into
  VictoriaLogs: "stats"). It writes the snapshot
  `~/.cache/agentctl/state.json` each tick and re-execs onto agentctl's new code between ticks (only onto committed,
  self-tested code).
- **Executors.** Long work runs in a process holding a lease, never inside the tick: `waiting check <id>` (a change's
  checks), `waiting check-batch <id>` (the batch's check and bisect), a red fixer. The tick sees a dead executor as an
  expired lease and starts another.
- **Events → inbox.** Every transition writes an event; the projector turns events with an audience or a non-info
  severity into inbox entries (the format the orchestrator keys on); the notifier sends the escalations (one macOS
  notification each). Nothing in landing code writes the inbox directly.
- **The snapshot.** `state.json` is the only source of `agentctl status --line` (the status line) and `ci-why
  --cached`: no git, no GitHub at read time. Its `asks` are THE OWNER'S PENDING DECISIONS, structured (ask.py `owner_asks`): one row per
  owner block whose question was told -- `{block, keys, reds, category, q, how, until, since}`, only an OWNER-ONLY
  category -- which the movies `scripts/claude-hooks/owner-decisions` hook puts in front of the owner's sessions. Never
  read a decision out of inbox text: an issue or a verdict may QUOTE "WAITING ON YOU".
- **Fixers are visible.** Every red fixer is a Claude Code background session: `claude agents` lists them by name
  (`red fixer #2432 · convergence takes-drift-1 (germany)`, `orphan · movies-wt-proxy-park`); `claude attach | logs |
  stop <id>` work on them (see "red").
- **The watchdog** (launchd `net.bitcashier.agentctl-watchdog`, every 60 s, `agentctl watchdog`) checks the invariants
  from outside the processor: W1 no unlanded change over 2 h old (counted from the red's start while main is red)
  is outside what its state's table allows -- a live checker for `checking` (a backlog batch's member: its batch at
  work within the batch's deadline, the batch's executor its checker -- 2026-11-23, ci-row-scope-py in batch #107 read
  `no live checker`; `land status <id>` names its batch), a recorded hold within its kind's bound
  for `queued` / `checked` (policy.py HOLDS), a red gate for `waiting` -- and none at all over 3 x that (2026-11-20: 13
  of W1's 18 firings were checks simply running past 2 h, each worked out by hand), W2 every red has a holder, an offer, a fix on its way or an owner block, W5 the processor beats, W6 every
  escalation was notified, WN notifications get through (both terminal-notifier and the osascript fallback failing
  is ONE condition naming the real cause -- exit code and last stderr line, or `timed out after N s` -- and
  that channel's fix: a timeout is the overloaded laptop's, a refusal the channel's permission in System Settings >
  Notifications; each channel bounded by `AGENTCTL_NOTIFY_TIMEOUT`, 30 s; W6 defers to it), WM
  maintain ran, WA agentctl's own main CI (tests.yml) has not been red over 30 min (`$AGENTCTL_WA_RED_MAX`), the newest green run is pinned within 15 min (`$AGENTCTL_WA_UNPINNED_MAX`; the pin is the reconciler's reported rev, ssh only as fallback) and main's head has a decided run within 60 min (`$AGENTCTL_WA_UNDECIDED_MAX`; the commit lag is shown, never an alert; selfci.py: 2026-11-25, red 5 h, nothing deployed, nobody told) -- an INVARIANT condition, so an owner inbox item and `conditions: N` on `status`, WR no red fix waits (none on `waiting`, none held by a hold kind that is other work -- a batch, an overlap, a
  verification -- and none unlanded 40 min after it was queued unless its own check runs, or 2 h whatever), WB nobody leaves uncommitted edits in ~/bin's own checkout for over 10 min
  (it names the processes working there), WF the system-wide file table (kern.num_files) stays under 70% of
  kern.maxfiles (`$AGENTCTL_WF_PCT`; past it, it names the top three holders, and for Rancher Desktop's VM on top the
  `rdctl shell -- sudo sh -c 'sync; echo 2 > /proc/sys/vm/drop_caches'` that releases its virtiofs descriptors: on
  2026-11-20 ~200k of them, one per file a container had seen in ~25 worktrees, overflowed it, ENFILE everywhere), WP no
  process -- a system daemon too -- holds an RSS past its own limit (4 GB, `wp_rss_by_name` in agentctl.toml: `idea` 8 GB; a ceiling its kind sets itself + 25% + 1 GB, procwatch's
  CEILINGS table: a JVM's -Xmx, a mongod's WiredTiger cache -- cacheSizeGB, else 50% of (RAM - 1 GB) --, a Linux VM's
  configured guest memory (Rancher Desktop / lima / colima / Docker Desktop); a live governor ticket's charged memory)
  that has kept growing for 30 min (both halves of its 2 h of 5-min samples grew, 500 MB or 5% in all: one jump, or a
  big server's creep, is no trend), or a CPU over 90% (a ticket's charged cores) for 20 min (lib/agentctl/procwatch.py: samples in procwatch.json; 2026-11-22 fseventsd at 21.5 GB and over
  100% for days); it names the process, its RSS and CPU trend and the fix -- a system daemon's is the OWNER's (`sudo
  killall fseventsd`, which launchd restarts; the Spotlight exclusion of ~/projects and /private/tmp): agentctl never
  stops or reconfigures what it did not start -- and one of ours by the worktree it runs in; WT the test suite's roots
  past twice the sweep's bound in /tmp stay at 10 or fewer (upkeep removes the ones no runner holds), said with how many
  worktrees hold build output, W7 (phase 2, the sync role) the sync has projected every event older than
  5 min, W8 no change left the queue in the last hour (landed, rejected, superseded, cancelled) without an event
  addressed to its author's inbox (Store.transition completes every such event; the entry carries the item id, so
  `agentctl inbox <item id>` finds it, also once merged into its worktree's live land alert, which lists every merged
  change id under `entries`; W8 reads the items before the events, so a change exiting mid-read is not named), WU (the sync
  role) the store tunnel is not up but unresponsive: localhost:3014 accepts a connection while the store has not answered the
  sync's reads for over 5 min (state.json `store_error`; 2026-11-26: the tunnel listened for 90+ min, every request timed
  out, nothing was raised). A violation is a condition and a notification. W5 kickstarts a processor
  silent for 15 min (3 x W5_MAX) of AWAKE time only: a pass whose own previous pass is over 3 min old
  (`$AGENTCTL_WATCHDOG_SLEEP_GAP`) slept through the gap and starts the clock again (2026-11-15 16:58: at a 2-second
  dark wake it kicked a processor whose beat was 16 min old because the laptop had slept since 16:42). The kick keeps
  the processor's store token: the old processor is asked for it on its socket first, and it goes to the one launchd
  starts (within `$AGENTCTL_KICK_HANDOFF_WAIT`, 30 s), in the watchdog's memory only.
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
- **Throwaway work never lands (2026-11-01).** `land` (and so every re-land: recovery, orphan duty, blame, fixer) and a
  laptop submission refuse with `NOT LANDED` when the branch is `measure/*` or `experiment/*`, a commit's subject starts
  PROTOTYPE / WIP / DO NOT LAND (any case), or a commit carries a `Do-Not-Land:` trailer (`do_not_land`, tools.py).
- **Held work lands only on the owner's go (2026-11-06, es-gate-check).** A commit with a `Hold: <why>` trailer (in the
  message's trailer block) is refused by `land` with `NOT LANDED` until `agentctl land --release-hold`; orphan duty and
  every re-land never submit it (`held_commit`, tools.py).
- **One push to main at a time.** Every push to main happens under the `land/<repo>` lease, after the pushed tip's own
  checks passed on the base it is pushed onto.
- **A dead blame revert never lands (2026-11-02).** A revert blame WITHDREW or saw REFUTED (`Reverts-Change: <change>`
  for a change marked not the culprit), and the worktree blame made for it, are refused by land, the server's
  submission, the queue's checker, a recovery re-land and orphan duty (`blame_land_refusal`, blame.py). See "blame".
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
ago the tick ran, and on what code: `code 4d0fc921c9e6 (code d1677f40)`, the git revision first (ci-runner's export
directory is named by it; a checkout says its HEAD, `+dirty` when agentctl's code differs), then the code hash the
restart check compares (2026-11-01: the hash alone named no commit, and code that ran looked unpicked). `lease list` prints holder, epoch, expiry, max hold and the holder's note; leases free for over a day
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
- reds: every open red -- workflow, run (linked to GitHub), failing jobs, machine state, age, holder and when its
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
agentctl store serve --listen ADDR:PORT --allow-user U... --metrics-user U...   # ci-runner's unit
agentctl store use http://localhost:3014           # which store every agentctl process on this laptop uses (the default)
agentctl tick                                      # on ci-runner (AGENTCTL_HOST=ci-runner): the reconciler
```

Live since 2026-10-29 (the owner's steps: `~/bin/docs/phase2-runbook.md`). The store is served over HTTP on
ci-runner's :3014 (through the tunnel); every request carries the caller's own store token (`agentctl store token issue`), checked by the store.
Each process has one of TWO roles (owner decision 7: one writer, no laptop standby):
- `server` (AGENTCTL_HOST=ci-runner or AGENTCTL_ROLE=server): the database in-process; `tick` there runs changes,
  batch, watches, reds, conditions; checks on the CI tier; never the laptop's duties.
- `sync` (everywhere else): `land` submits there, events become inbox entries, escalations notifications, the state is
  mirrored into `state.json`, claims are renewed from the worktrees, and the fixer dispatcher takes the offers --
  fixers stay on the laptop, in `claude agents`. `Store()` is the remote store (`store.url` in the state root, else
  http://localhost:3014), so every command works unchanged. With the store down `land` prints `QUEUED-LOCAL` and the
  sync submits it later.

#### laptop: ONE machine holds the laptop role (2026-11-19)

`agentctl laptop status` / `agentctl laptop take [--yes]`. The laptop role is the store lease `role/laptop`, held by
`role:<host>:<machine id>` (macOS IOPlatformUUID; `$AGENTCTL_MACHINE_ID` / `$AGENTCTL_HOSTNAME` override) and renewed
by the sync loop's first step every pass. A machine finding it held by another live holder is STANDBY: its sync loop
skips every laptop duty (notifier, alerts, claims and the orphan reaper, dispatcher, pins, drift explorer),
`red_dispatch` answers `NOT DISPATCHED: laptop role held by <host> since <t>; this machine is standby`, an owner wrapper
does not start, `agentctl status` leads with `STANDBY: ...`, and its owner is told once per holder in its inbox
(`LAPTOP STANDBY`). Its own worktrees' steps (outbox, landed worktrees, the local queue) go on. The STORE enforces it
too: while the role is held, an `acquire` or `take` of a `laptop:` lease from a client whose `X-Agentctl-Role` header is
not the holder (or names none: an agentctl from before the role) is refused with 409 `Standby`; so is a `revoke` of a
lease a `laptop:` holder holds (it needs no secret: on 2026-11-19 pawel-mbp's pre-role orphan reaper revoked every
family owner's heartbeat as "ORPHANED", judged by its own records -- outside-gate restarted 23 times). A client
naming no role is refused for good once a role was ever held, the holder asleep or not, and its calls are no contact
of the laptop's (they kept the laptop's lease clocks running while the holder slept). A holder silent past
`LAPTOP_ROLE_TTL` (10 min) is taken over by the next machine that asks; its holder has no `laptop:` prefix, so the
laptop's contact pause never keeps a dead machine's role. `take` revokes a live holder's lease (asking first; `--yes`
without a terminal). Why: on 2026-11-18 23:41Z a second Mac, `pawel-mbp` (its store tunnel key
`store-tunnel@pawel-mbp`), dispatched fixer red-0c8eea91b712-1 for core #1053 while Pawes-MacBook-Pro-2 ran the
laptop's duties; each revoked the other's "orphans" (a pid not among its own processes) into a crash loop. The `laptop:`
holder names of fixers, owners and claims are kept: with one role holder they name one machine (their meta carries
`host`).

THE CALLS (`lib/agentctl/storecalls.py`, 2026-11-27): one table, `STORE_CALLS`, names each store call, its parameters,
its kind (`read`, `idempotent`, `write`: a call that may have reached the store is sent again only when it is not a
`write`) and the API version that added it. The server serves exactly those calls (`POST /v1/store/<name>`; any other
name is a 404 `UnknownCall`), RemoteStore's methods are made from it where the class is defined, and
`tests/test_storecalls.py` holds it to Store's methods and their parameters. A client asking a store older than its
call gets `StoreLacksCall` (remembered 10 min) and falls back where it can; a row is never removed while a pinned
revision may still send it. Adding a call: its Store method, then its row with the next version.

There is no laptop store any more (`store use --local`, `store import` and the laptop's own tick were removed
2026-10-29, docs/simplification-2026-10.md item 2). Disaster recovery needs no laptop copy: ci-runner keeps 24 hourly
backups (`agentctl-store-backup`), and a lost store.db is rebuilt from the land/* refs and GitHub's runs (design §16).

### wait run: block on CI and print only the verdict

```
agentctl wait run <id>... [--repo R] [--until done|landable] [--tail N] [--interval S] [--timeout S]
```

(The lead's brief called this `run-wait`.)

- `<id>`: action_run.id (GITHUB_RUN_ID), not the number in the page URL. The legacy form `wait run movies 1234`
  still works.
- `--repo`: `o/r` or a short name (`movies`, `core`, `infra`, `dashboard`, `invoice-page`, `version-dashboard`).
  Default: the cwd's remote, else movies.
- `--until landable`: for an *Identity takes* run on a `measure/` branch (scripts/identity-measure dispatches it
  directly since 2026-11-08; a `--full` measure's *Identity model convergence* run too), stop as soon as every
  `takes (<country>)` row has ended, not when the run's slow tail ends. It then prints each
  `identity-takes-<cc>/verdict.txt` and the collect command. For any other run it behaves like `done`.
- `--tail N`: the last N relevant lines of each failed job (default 8). The per-step slices can be shifted a few lines off the stored log, so when a failed job's slice has no relevant line, agentctl reads the whole job log (`--log --job <id>`), `::error` annotations first. `ci-why` does the same. Strong markers (`*** FAILED ***` plus the 2
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

Use it for any wait on a CI run, in the background, followed by ending your turn. Don't use it for
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
agentctl ci-why [main|<run id>|<run number>|#N] [--repo R] [--tail N]
```

For movies it reads exactly THE GATE (the owner, 2026-10-27: "if the latest main and convergence runs are green, just
push then"): *Main*, *Identity model convergence* and -- since the owner's 2026-11-08 split of convergence's takes
rows into their own workflow, dispatched by convergence's preflight -- *Identity takes*, the same three the pre-push
red gate (`check_redgate`), land's
red/green branch and the `waiting` processor read. THE GATE IS movies' GATING CHECKS REGISTRY (issue 7267ea, 2026-11-19):
`.github/gating-checks.tsv` on origin/main (`scripts/ci/gating_checks.py`; tab-separated `kind workflow name proven
note`), read like `red-workflows.tsv` -- from the clone, else GitHub's contents API, else the copy last read (state root
`gating-checks.json`); never read at all is an UNREAD gate (red). Its `workflow` rows ARE the gate workflows (agentctl
keeps no list: `gate_flows`, named by main's workflow files); `red-repos.json` gives movies no `gate`. A red gate run
whose failed jobs are all `unproven` rows (aggregates aside) -- or any red of an `unproven` workflow row -- is REPORT
ONLY: `Main: RED, REPORT ONLY (...) -- holds no push`, no entry in `gate_reds`, its reds tracked outside the gate. A
proven row's red gates. A failed job with NO row is unproven, exactly as the hook reads it (movies' rule: a new check
gates only once it has been green); its `why` -- in ci-why's REPORT ONLY line and the log -- names it: `no row in
.github/gating-checks.tsv: <job> (read as unproven -- add its row)`, since the registry's lint wants a row for every
gate job. Verify scrape recording no longer gates. For each, it takes the newest decided run on main
(success or failure; cancelled and skipped runs are passed over). A gate workflow whose file is not on main yet
(GitHub's `HTTP 404: workflow ... not found on the default branch`) has no run there and decides nothing -- never an
unread gate; any other read error still is one (2026-11-08: agentctl's gate list named identity-takes.yml before its file landed,
and the gate read UNREAD for 1.5 h). For other repos it reads every workflow seen on
main. For each red run it shows:

- the failed jobs and their relevant log lines;
- the commit and author;
- a verdict:
  - `TEST FAILURE`; `TEST FAILURE: REGRESSION, not an order dependence (<jobs>: ...)` when a job's failing step says
    so. THE ORDER VERDICT (movies' Order independence, 2026-11-22; `classify.py`'s `ORDER_VERDICT`): each leg's control
    pass (`scripts/ci/order-compare.sh leg`) runs the failing tests again in default order at the same commit and
    prints `REGRESSION, not an order dependence: <tests> fail in default order too` or `ORDER DEPENDENCE (or a flake):
    <tests> ... pass in default order`. That line becomes the job's first line, so the red's KEY is the tests, not
    the leg (both scala orders failing alike are one red); a REGRESSION red's fixer is told the order is not the cause
    and sent after the commit that broke the tests (the range from the workflow's newest green run to the red head),
    an ORDER one after the state one test leaves another. ~31 of its ~66 reds over runs #1-#100 were such regressions,
    each read as an order red;
  - `INFRA-SHAPED`, with the `rerun --failed` command. Covers failures in a setup step (a step named `Runner
    preflight ...` too: Order independence's runner checks, `scripts/ci/order-preflight.sh` -- a missing simulator, a
    wrong JDK, BSD tools on a Linux leg -- and an emulator that never booted), `HARNESS` (the build tool or the test
    wrapper failed before any test ran: sbt's `[launcher] error during sbt launcher`, Gradle's `No cached version
    available for offline mode`, a `no-network:` setup error -- with no line in the job's log saying a test ran;
    Order independence #113), a 0-length log,
    `artifact already exists`, ENOENT, docker, disk or connection errors, an OOM kill (exit 137), a runner shutdown
    or lost runner, `No test results found for`. A test verdict is read only from a test runner's own line (scalatest's
    `*** FAILED`, sbt's `[error]`): GitHub's `##[error]` step-end annotation is none, or no infra signature would win
    (Identity takes #13, Verify #14, 2026-11-15);
  - `NO VERDICT`, when `verdict complete` (Identity takes'; convergence's before the split) is the run's only real failure (a takes or takes-drift row
    was skipped, cancelled or missing): reporting, not a test failure -- no red, no streak, no fixer; the gate reads
    the newest complete run. Beside another failed job, `verdict complete` is an `[incomplete]` row with no verdict of
    its own: the run's verdict and its reds are the other jobs' (an infra leg stays `INFRA-SHAPED`);
  - `UNCLEAR`, when a log has lines but no failure marker (truncated, or the runner was lost);
    also `LOG UNREADABLE`, when gh could not serve the log (read twice; its error named): no log was read, so
    it is never a 0-length one (convergence #2473 was handed to a diagnose-only fixer as INFRA that way);
- a newer run in flight that may clear it;
- the clearing rule.

A gate red says how long it has been red (`red for 3h 0m (2 failed runs)`: from the oldest failure of the current
streak). For movies it then lists every workflow OUTSIDE the gate whose newest decided run on main is red, oldest red
first, each with how long and since when (the owner, 2026-10-27: a red nobody is shown gets ignored). Those never make
`ci-why` exit 1: they block no push. A `workflow_call`-only workflow's run that its callers proved fixed (its red
verified, the run settled) stays that workflow's newest run on main for good: it is listed apart, under `Proven fixed
outside the gate`, and left out of the status line (Report run timings #38 read "offering" there after its FIX OK).

`ci-why <run id>` explains one run; `ci-why <N>` / `ci-why #N` below 100,000,000 is a run NUMBER (what reds, the
inbox and every link's `#N` print), resolved on main -- a gate workflow's run first, then a red one -- and the others
carrying that number are named; a number no run on main carries is read as an id (issue 20261031T085600-c1cbf8).
Exit: 0 green, 1 red (or a run explained).

How it reads a run (issues of 2026-10-31..11-19, `tests/test_ci_why_accuracy.py`, one fixture each):

- **The failed attempt.** A rerun replaces the run's jobs: when the latest attempt has no failed job (green, or still
  running), the newest earlier attempt that failed is read (`gh run view --attempt N`), and the line `read attempt N,
  the failed one` says so.
- **The whole log, cut to the failed steps.** gh's run-zip reads (`--log-failed`, `--log --job`) no longer map lines
  to steps (every line `UNKNOWN STEP`) and stop at ~1.3-2.6 MB. A job gh could not map is read whole through REST
  (`jobs/<id>/logs`, with `--allow-escape-sequences`; gh's refusal text is never taken for a log) and cut to its failed
  steps by their start and end times. A log that ends minutes before its failed step did is `LOG TRUNCATED`.
- **Never a pass mark or chatter.** A `✓` line or a `... skipped: ...` line is never a failure's line; Playwright's own
  report (`  1) [project] › file`, `  N failed`) is a test runner's line, as scalatest's and sbt's are.
- **No failed job listed.** A completed failed run's job GitHub left undecided (conclusion null) is failed when a
  step of it failed; a cancelled job's failure-level check-run annotations are its cause (`was not acquired by Runner`:
  INFRA, no hosted runner); and a failed run whose created jobs all passed is `INFRA-SHAPED`, GitHub-side, naming the
  workflow's jobs it never created and any githubstatus.com Actions incident of its window (`$AGENTCTL_GITHUBSTATUS_URL`).
- **Credentials.** A step that mints the run's credentials (`create-github-app-token`, cloud auth actions) failing is
  `KEY REFUSED`, infra: the owner's credentials or account scope, never a code red.

```
$ agentctl ci-why
ci-why pawelkrupinski/movies: main is RED for the gate
Main: GREEN (run 1976 #968)
Identity model convergence: RED (run 1934 #945, failure); newer run 1988 (#973) in_progress may clear it
  verdict: UNCLEAR (a failed step logged no FAILED/error line: read `gh run view 1934 --log-failed`; if infra, rerun: gh run rerun 1934 --failed -R pawelkrupinski/movies)
  run id 1934 (#945) https://github.com/pawelkrupinski/movies/actions/runs/1934
  commit 2f0fdcd3c Paweł Krupiński <…> 10-23 11:32: order-phases.sh: no mapfile -- the pre-push hook runs scripts under macOS's bash 3.2
  [unclear] takes (united-kingdom): 8-line log with no FAILED/error/assert line (truncated? runner lost?) in step '…'; ends: …
  clears when: a later Identity model convergence run on main is green, or every pushed commit touching a production path carries `Fixes-Red: 945` (or `Fixes-Red: 1934`) or names another run of this red episode failing the same way; tools-only commits land while red
Outside the gate (red, not blocking a push; oldest first):
  Verify scrape recording: RED for 5h 15m since 2026-10-26T23:49:43Z (1 failed run; newest run 3140 #2000) -- `agentctl ci-why 3140`
```

Use it when the red gate refused your push, or before you decide whether a red is yours. Don't read logs by hand
first. A genuine `Fixes-Red:` trailer is only for a commit that really fixes that run.

**A fix matches the red it fixes, not one run number (2026-11-05).** Every push that lands while main is red starts a
new run failing the same way, so a fix naming the run it was written against was stranded WAITING once the next run
went red (Main #2164 / #2172 named while the gate read #2173 / #2174: main red ~5.5 h). A `Fixes-Red: <run>` counts when
it names ANY run of the CURRENT red EPISODE: the gating workflow's failed runs on main since its last green that share a
failure signature with its newest red run (`gate_reds` / `episode_names`: red_units' names, the ones a red item links
by). A run of an older episode -- main went green in between -- never counts; neither does a run of this episode that
failed another way. Signatures unreadable: only the newest red run counts, and it is SURFACED (`episode_unread`: the queue facts carry why, a waiting fix's words say it). movies' pre-push judges the same rule from
gh (main's runs since that workflow's last green, and their failed job names; unread: refused as before).

**A fix of an open gate red never waits (2026-11-25, batch #124: Main #2898 red, `Fixes-Red: 2896`).** The classifier
(`red fix of 2896 (gate)`) matches a fix by every name of an open red item; the queue's `must_wait` read only the gate's
newest runs (#2897, #2898), so the fix sat on `waiting` beside a non-fix change and a batch, which assembles only what
may land on red, found nothing: WR, a fix waiting for the green only it could bring. `must_wait` now also lets a commit
through that names any name of an open gate red in the store (`redland.gate_red_names`); the batch then takes the fix
alone and the non-fix change stays behind.

**One red-landing rule (2026-11-26, `redland.may_land_on_red`).** Whether a change may land on the gate's red, whether
it fixes an open red (`red_fix`: its own tree, first) and whether it fixes a GATE red (`gate_fix`) are one verdict;
`must_wait_why`, the classifier, the check executor's red-fix tree, the governor's class, the batch order and the
waiting batch's "a fix waits" read it. Characterised first (`tests/test_red_landing.py`: every copy over a seeded matrix),
two disagreements were resolved: a gate red's fix beside a production commit naming no red is a red fix but NO gate fix
(it may not land on the red, so it is not first in the batch order); and a change's commits' COUNTING trailers decide,
not the names its entry recorded at submission (a trailer fix_check stopped counting makes no fix). An item's data read
without git is judged by its recorded names. The queue facts it reads are read once per queue tick (`facts_for` inside
`facts_tick`, opened by `tick_changes` and `tick_batch`); outside a tick every call reads afresh. Whether a fix's commit is
on main is one `maincontain.MainContainment` (a clone's git, or the forge's compare with no clone here): `contains`,
`landed`, `carrying` -- "cannot tell" is None, never "not on main". `land hold` makes its hold in one store transaction
(`Store.open_singleton`): two holders at once make one hold.

**A tools-only change failing only as main's gate fails lands (the owner, 2026-11-26: "let it through"; policy `known-red-tools`).** In the history 13 such changes waited 102 h on `waiting` behind a red they did not cause. `redland.may_land_on_red` now also says `tools_only_known_red`: the gate is red, the change's commits are read and none touches production, and its entry's `known_red` is `gate_only` (every failing key is one main's newest decided gate run fails; `queuefailure.on_gate_run`, never one only another queued change showed). `queuediff.wait_or_push` then pushes it, the move naming the cause `tools-only-known-red`. An extra failure is the change's own (rejected, as before); a production change still waits unless it names the red (IR). Simulator invariant IK; scenario `KNOWN_RED_TOOLS`.

**WR self-heals, and an episode counts by signature (2026-11-25, the owner: "fix the class").** Three more pieces, so a
fix of the open red never waits on bookkeeping again:
- *Episode names* (`redepisode.py`, read by `queue_facts`): besides the red item's own names, a gate red's names include
  every run of its workflow's red streak since the last green whose CACHED run signature has the red's failing job and
  line -- a run the item dropped (or never attached) still counts for a fix naming it; a run failing another way, or one
  before the green, does not (it waits as before). No forge read. ONE SOURCE (2026-11-26, `RedEpisode`): the gate's
  episode and this one read a run's signatures in one fallback order -- this classifier's record, a read again within
  the tick's budget (the facts' is 0), the stale record of an older classifier -- so a stale record naming the red's job
  and line counts here as it does for the gate.
- *The heal* (`batch.py release_red_fixes`, run by the batch tick while red as well as at green): a waiting change the
  store's open red names, whose commits `must_wait` no longer holds, goes back to the queue -- first, alone, in the
  red-fix tree -- and the batch is left to the changes that may wait. A red fix `must_wait` still holds is told what holds
  it: `wait_rule` on its item (`must_wait_why`), which WR's text and the liveness line print -- the escalation left.
- *The liveness line* (`redhold.py red_fix_liveness`): over 60 min of red gate (`$AGENTCTL_RED_LIVENESS_MAX`) with a red
  fix that does not move (WR's own reading, `red_fix_stalls`), `agentctl status` (under its first line) and `red status`
  show ONE line: `RED FIX STALLED: the gate has been red <age>; <change> (red fix of ...) is <state>: <the rule>`.
The simulator holds it as invariant IW (a red fix of an open gate red the store names never waits past N ticks; scenarios
`REDFIX_OLDER_RUN`, `REDFIX_HEALS`, `REDFIX_OTHER_FAILURE`).

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
tick); only the server escalates. A BLIP OPENS NOTHING (2026-11-17: 18 of a week's 19 UNKNOWN spells closed by
themselves in ~70 s, "movies token present"): an UNKNOWN opens only when it is seen failing again at least
`$AGENTCTL_UNKNOWN_GRACE` (300 s) after its first sighting (sightings at most `$AGENTCTL_CONDITION_PENDING_LAPSE`,
30 min, apart; until then a `condition_pending:<key>` meta mark and no inbox entry); `open_condition(..., grace=0)`
opens at once. recovery.py `condition_pending`.
`tests/test_recovery.py` lints the source for a bypass. Owner blocks are block LEASES renewed by their probe (see
"always claimed"), not conditions.

An infra NOT LANDED (fetch / tunnel / network, a remote run unreadable, a stall) lands again when its check passes (it
rebases first; a conflict is a real NOT LANDED); a test failure, conflict or refusal stays with the author.

Decodo (2026-10-28): `Decodo probe` (movies, every 30 min + dispatch) CONNECTs through the proxy with the CI secrets;
Security headers' block is `red block <n> --cmd "Check the Decodo dashboard ..." --until-green "Decodo probe"`, so it
clears and Security headers is re-dispatched once Decodo answers.

### escalations: an agent for every escalation an agent may take (2026-11-26)

The owner: "can agentctl just kick off an agent when sth escalates?" An ESCALATION is an open condition (above) past
its bound; its kind is the condition's kind, an invariant's refined by its id (`INVARIANT:W2`). ONE TABLE, as data,
routes every kind (`lib/agentctl/escalationtable.py` `DISPATCH_TABLE`): `agent` -- its task, its repo (`bin` or
`movies`), its model (sonnet by default, opus where the root cause is unknown: W1, WR, W2, W8, STALLED,
change-stranded, UNKNOWN), its wall time (60 / 120 min) and its tries (3 / 2) -- or `owner-only`, with its ask.py
category: WB (somebody's edits in ~/bin), WN (notification permissions), WF (sudo in the VM), WD (the disk),
NETWORK (an outage), UNLANDED_ORPHAN (its orphan fixer already ran). A kind no row names is the owner's, and so is
any condition whose text names the vault, Touch ID, a credential, a reboot or outage, a force push, the repository's
visibility or an owner decision (`OWNER_SIGNALS`), or the row's own `owner_when` (WP on a process agentctl did not
start, `the owner's to look at`). tests/test_escalations.py fails on a condition kind the code opens, or an
invariant, that has no row.

The laptop's sync runs the dispatcher (`escalation dispatcher`, beside the red one) on the red dispatcher's own
machinery: the store's OFFERS (kind `escalation`, item `condition:<id>`: one offer per kind and subject) and LEASES
(`escalation/<kind>`: taking it closes the offer atomically, and a held lease refuses a second taker -- ONE FIXER PER
KIND), at most 2 escalation fixers alive over all kinds (`ESCALATION_FIXERS`; the red fixers' cap is apart). A taken
offer starts `claude --bg -n "escalation · <kind>" --model <model>` in a fresh `<repo>-wt-escalation-<kind>` off main,
denied what every dispatched session is (`SESSION_DENIED_TOOLS`), briefed with the condition's text, its probe, the
earlier tries, its history (`agentctl history <condition id>`) and the rules (the worktree, `devtools/selfpush.py`
for ~/bin or `agentctl land` for movies, the commit gate, no bypass, the owner's things left to him). The lease is
renewed while `claude agents` shows the session at work and is stopped at the row's wall time. THE ESCALATION CLOSES
WHEN ITS CONDITION CLOSES (its own probe), never on the agent's word: a session that ends with the condition open,
times out, never starts or loses its lease is a FAILED TRY, the next one backed off 10 min, doubled each try; once the
row's tries are spent ONE entry `ESCALATION GAVE UP: <kind> <key>: WAITING ON YOU: ... try 1: ... (worktree)` goes
to the owner under the condition's alert item (it closes with the condition), and the kind is not dispatched again
that spell. A repo claude does not trust is NOT DISPATCHABLE on the offer (no try counted, no retry storm); the owner
is asked once by the sync's `workspace trust` pass, which checks the table's repos beside red-repos.json's clones.
Every offer, dispatch, take (actor `session:<id>`), failed try, give-up and clear is a move of the file-held item
`escalation` (`<kind>/<condition id>`), so `agentctl analyze --type escalation` prints `time to end by kind`
(`INVARIANT:W6 -> cleared  n  p50  p90  max`). The simulator holds it too (tests/queue_sim.py's ESCALATIONS
scenarios): IE never two fixers of a kind nor more than the cap, IO an owner-only kind never dispatched, IG every
dispatched escalation cleared or handed to the owner within its budget.

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
  under `~/.cache/agentctl/reds/fixcheck/` (each verdict with the rules' version, `FIX_RULES`: an older one is read
  again).
- **Per red, per failing step (2026-11-15: 09e482c6a, an ios/KinowoUITests change with `Fixes-Red: 74`, put both reds
  of Order independence #74 in fix-pending; its swift red was the script self-tests under bash 3.2).** A run's value
  counts (for the gate, the batch) when any red of the run reads the commit; but each red TAKES a fix -- a queued
  change, a landed `Fixes-Red:` commit, its fixer's `LANDED <sha>` -- only when its own failing job reads it, or the
  trailer names its key. A red whose failing steps are known (the run's jobs read once, `reds/runsteps/<run id>.json`)
  is judged by those steps and the setup steps before them (an action, a setup / build / install step, a write to
  `$GITHUB_ENV` / `$GITHUB_PATH` / `$GITHUB_OUTPUT`), never the job's other legs. android/ or ios/ is read whole when
  that text builds it (a project or build file of it, `--package-path` / `working-directory` / `cd` into it, a step
  running a script inside it); a directory under it named alone (run-script-tests' `ios/scripts` root) is read alone.
  Another workflow a script names is its data, never followed. A fix-pending red on a fix it does not read (another
  red's) is offered again at the next tick.
- **What a self-test reads (2026-11-15: f872ecbe3, a `.github/workflows/` change with `Fixes-Red: 263`, was FIX NOT
  COUNTED for Tool self-tests #263, whose step runs scripts/run-script-tests, which finds
  download_artifact_layout_test.py, which lints every `.github/workflows/*.yml` through the module it imports).** A
  script that finds its tests by a name glob (`'*-test.sh'`, `'*_test.py'`) reads every test it finds under the roots
  it names; each followed file reads what it names (a path, a glob's directory: `.github/workflows/*.yml` reads every
  workflow), sources (`. f`, `# shellcheck source=f`) or imports (a Python sibling module) -- the same three levels,
  plus the paths the last level names. What a found test reaches only adds paths: never android/ or ios/ whole (a test
  lays out a fake `ios/Kinowo.xcodeproj` in a temp dir), nor sbt or spec names. So a workflow change counts for a
  self-tests red whose suite lints the workflows, and a pure ios/ change still does not (rules version `FIX_RULES` 3:
  cached verdicts are read again).
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
  A live fixer outranks a block. **An owner command that deletes is scoped and guarded** (issue
  20261110T084827-38f01b: an unguarded `rm -rf` of runner workspaces, run 8 h later, wiped three running jobs):
  it names each path (no brace, glob, bare variable or near-root path), checks before it deletes (if / test / [ /
  pgrep) that the target is still unused when the owner runs it, and inside a CI runner's workspace skips a slot
  whose Runner.Worker runs. `red block` refuses anything else (`owner_cmd_unsafe`), and a block already carrying one
  lapses at the next tick. A red with 3 or more refuted fixes is `recurring: N fixes refuted`, and its fixer is told to find the root
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
agentctl red status [-v] [--live]              # every red: its state, holder or offer, deadline (the laptop sync's
                                               # snapshot while under 3 min old, its age said; -v / --live read the store)
agentctl red dispatch <run> [--force]          # the red's offer at top priority: the dispatcher takes it now
agentctl red unblock <run|key> --why "<what changed>"
agentctl red auto on|off|status [--repo R]      # --repo: ONE repo's dispatch (the owner's "no bitcashier builds")
                                               # it no longer waits on the owner (done, superseded): ends its block
                                               # lease, or strikes a BLOCKED ON OWNER verdict no probe can clear
agentctl red unblame <run> --why "<why>"       # WITHDRAW its blame (culprit wrongly decided, revert unlanded): see "blame"
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
| pawelkrupinski/movies (the primary) | Main, Identity model convergence, Identity takes | red-families.json | land |
| bitcashier/infra | Stage NixOS closures | infra-repo | **owner-only** |
| bitcashier/core | Main Pipeline | core-repo | land |

- The primary keeps everything it had (its keys, outside workflows, local checks, its landing gate: its gating checks registry); the
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
  `agentctl land`, `git push`, `bin/deploy`, `nixos-rebuild` and `switch-to-configuration`, and
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
- **A drift / flip row is keyed by its flipped LISTINGS (2026-11-01).** The first error line of a `takes-drift-*` row is
  the same for any listing on any seed ("0 divergence(s) ... N flip(s) on the way ... seed S ... reproduce: IdentityDrift
  uk --seed S"), so every new unrelated flip was the same red and "refuted" the fixes landed for earlier ones (#624, a
  Ladykillers flip, marked #595's Dracula fix and #549's slice refuted). Now the job's whole log is read through
  `gh api repos/<o>/<r>/actions/jobs/<id>/logs` and the rows `<venue> | <raw title>: <takes...>` under each `## <cc> flips:`
  (and divergences; never a `KNOWN:` baseline row or a `FIXED,` one -- they follow the `## <cc> known flips:` summary
  line, which the fresh rows follow too) become the key line `flips: cc:venue|title; ...` (sorted, lowercased;
  past `FLIP_KEY_MAX` listings, 5, or 150 chars a stable letters-only hash of the set). The red key is workflow + that
  line, so two failures share a red only if they share the same set of flipped listings, a landed `Fixes-Red:` is
  refuted only by a newer failure with its own key, and a different listing is a new red with its own fixer. ci-why
  prints `red key: ...` under the job. A log that cannot be read keys the job the old way (its seed line) and says
  `LOG UNREADABLE: ... flipped listings` (never cached, read again next tick); a log naming no listing says so (cached).
  A job log holds ANSI colours, which gh refuses to print without `--allow-escape-sequences` (2026-11-01: every drift
  log was LOG UNREADABLE until it was passed, and #629 was keyed by its seed line).
  Migration is the classifier version (it covers `flip_listings`, `flip_key_line`, `drift_key`): cached signatures are
  read again a few per tick, and an open red keyed the old way whose run now fails under a listing red is MERGED into it
  (runs, history, refuted fixes), as for any re-keyed red; one whose runs no longer fail is cleared by the usual rule.
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
  (convergence: the Identity model convergence and Identity takes workflows; a non-gate `convergence (<cc>)` job is outside-gate's): while a family's fixer is at
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
- **EVERY RED OUTSIDE THE GATE TO ONE AGENT (the owner, 2026-11-07: "move all these to single agent"; Identity rule
  ablation #1, Identity family explorer #5 and Order independence #46 had spawned three fixers).** A family with
  `"outside_gate": true` matches a red whose workflow is not one the gate reads -- `red_gate_flows` (movies' gating checks registry: Main,
  Identity model convergence and Identity takes; another repo's red-repos.json `gate`), the same set ci-why's "Outside the gate" leaves out,
  never a list of its own; the Local checks count as the gate's. The family `outside-gate` (owner) comes FIRST:
  a Verify scrape recording `convergence (<cc>)` job is its too, and an infra-shaped red of a non-gating workflow (an
  explorer OOM-killed, exit 137); it matches only workflows the gate does not read, so Main's and Identity model
  convergence's reds still reach `main-tests`, `convergence` and `infra`.
  A family's holder is its live fixer OR a `red claim` made INSIDE a dispatched fixer (its record on the lease) holding
  one of its reds: while one holds (and the family owner is not running), a new red of the family is claimed for that
  same holder and worktree (renewed by its activity) and told in its `RED-INBOX.md` -- never a second owner session
  beside it.
- **AN INTERACTIVE CLAIM TAKES ONLY ITS RED (the owner, 2026-11-08).** A manual `agentctl red claim` from an
  interactive session holds that red and nothing more: it never makes the session its family's holder, so a new red of
  the family goes to the family's owner or a fixer, never onto the session's lease. Family holding is for the sessions
  agentctl itself starts (fixers, family owners).
- **A JOINED RED SHOWS PROGRESS WITHIN 30 MIN OR GOES ON (the owner, 2026-11-08).** A red joined to a family holder's
  lease (`joined_at` on its lease) with no progress on THAT red within `$AGENTCTL_RED_JOIN_IDLE` (30 min) of joining
  is released (`RELEASED: no progress on it ...`, no attempt counted) and offered again, and never joined to that
  holder again (the red's `family_released`): it gets a fixer of its own. Progress is any of: a commit not on the
  remote in the holder's worktree whose `Fixes-Red:` names it, a fix on its way (the red's `fix`, fix-pending), or a
  note on it (`red claim <it> --note` inside the fixer). Once shown, the lease's `progressed` says what, and the
  backstop is done with it (red_joined_idle, run with the claims' renewal).
- **A RED IN AN OWNER'S HAND SHOWS ITS OWN PROGRESS WITHIN 45 MIN (the owner, 2026-11-22).** A family owner's wrapper
  renews every red lease it holds with each beat of a turn not stalled: the turn's progress, the family's. Alert
  backtest #21 sat ~3 h in the outside-gate owner's hand (16:05Z on) while its turns worked the other red. A red under
  an owned lease with no progress on IT for `$AGENTCTL_RED_HELD_IDLE` (45 min) since the later of its lease's start and
  that progress -- a `DIAGNOSED` or `LANDED` verdict in its history, a commit not on the remote in the owner's worktree
  whose `Fixes-Red:` or `Diagnoses-Red:` names it, a note on it (`red claim <it> --note`: the lease's `progressed_at`)
  -- is released `STALLED: no progress on <red> ...` (an attempt), the reason in the inbox (`RED NO PROGRESS`), and
  goes on up the ladder (red_owned_idle). `red status` shows that deadline as "lapses HH:MM without progress".
- **A BLOCK NO PROBE CAN CLEAR IS SHOWN WITH ITS AGE (2026-11-07).** A turn's `BLOCKED ON OWNER: <cmd>` verdict with no
  `red block` lease behind it has no probe: it stayed the red's last word after the owner had run the command (Verify
  scrape recording #14: "re-pin the scheduler to 362196b15a", long since re-pinned past it). Its note now reads `said
  <age> ago, no block holds it`, and `agentctl red unblock <run|key> --why "<what changed>"` strikes it (an `UNBLOCKED:`
  history entry) or ends a live block lease (`RELEASED`: its reds are offered again at the next tick, never escalated).
  Prefer `red block --until-*` with a probe: a block that can tell when it is over.
- **LONG-LIVED FAMILY OWNERS (simplification item 6, 2026-10-30).** A family marked `"owner": true` in
  `red-families.json` (today: `convergence`, `main-tests` = Main's test failures, `outside-gate` = every red of a
  workflow THE GATE does not read, `infra` = every other infra-shaped or unclear red) has ONE persistent owner instead of a fresh fixer per red: one `claude --bg` session (`red owner ·
  <family>` in `claude agents`) and its wrapper (`agentctl red owner --family F`, detached), working in ONE worktree
  (`<clone>-wt-owner-<family>`, branch `owner-<family>`) with a NOTEBOOK there (`RED-NOTEBOOK.md`, never committed)
  that it reads first and keeps current. Its lease `owner/<family>` is a HEARTBEAT (renewed while the wrapper lives,
  lapsing 15 min after it dies; no 3 h maximum). New reds of the family are handed to it -- their leases taken for the
  owner (kind `owned`), a line in `RED-INBOX.md` -- and become the NEXT TURN of the same session (`--resume`) -- only a session whose transcript holds a conversation: one
  that failed before its first message is never resumed (claude refuses it, `source session ... not found`; 2026-11-21,
  Order independence #99), the turn resumes the last one that spoke, else starts a fresh one. A crashed turn's verdict
  A SESSION PAST ITS CONTEXT LIMIT STARTS FRESH (2026-11-25; outside-gate had ~985k tokens by turn 134, each resume
  reloading it all): before a resume the session's context is read from its transcript (the last assistant turn's
  input + cache-read + cache-creation tokens); past `AGENTCTL_CONTEXT_FRESH` (300k) the turn is a fresh session whose
  prompt carries a handover of under 2k tokens built from agentctl's records -- the reds in hand, the worktree's branch
  and dirty files, the last verdicts, the open issues, the old session's last answers -- and the rollover (old id, new
  id, tokens) is kept in the owner's record and shown by `red status -v`. A fixer resuming a session (a revived lander,
  a resumable fixer) follows the same rule; an unreadable transcript, or one with no usage figures, resumes as before.
  carries claude's own error (its job's `detail`, `~/.claude/jobs/<id>/state.json`), and so does the HARNESS FAULT. A turn
  ends with one verdict line; that turn's red leases are released with it, exactly as a fixer's, so verification,
  re-offers (back to the same owner), the attempt ladder and the crash-loop breaker work as before. It is restarted
  only when it died (same worktree, notebook and session) -- ONCE: the dispatcher takes the owner's heartbeat itself
  before it starts the wrapper (`$AGENTCTL_OWNER_HEARTBEAT` hands it over), so a later pass finds the owner alive and
  hands it the red, never a second wrapper (2026-11-21 22:12Z: two starts 3 s apart, the second DUPLICATE) -- and ends after 6 h with no red (`$AGENTCTL_OWNER_IDLE`, counted from its last turn's end), on
  `RED-STOP.md`, or after 2 turns in a row with no verdict. A wrapper whose heartbeat is LOST (revoked, re-taken; a
  merely lapsed one is taken again) ends `STOPPED: the owner's heartbeat lease was lost (<why, from the lease>)` and
  leaves its turn's session at work: the next owner adopts it, never starting it over. Its brief: fast reproducer first, and for convergence the
  MECHANISM-ONLY rule (fix the incremental state that diverges from a whole resolve; never a resolver rule tuned on one
  recording, never a baseline or allow-list row). `red status` lists each owner (`owner <family>: ALIVE, turn n, ...`).
  Each turn's session is `claude stop`ped when the turn ends (claude keeps a finished session's process as a spare, and
  `--resume` of a running session starts a copy), so a family has ONE session; the laptop's tick also stops any
  agentctl-labelled session idle with its process alive that is no live wrapper's current turn (`RED SESSION SPENT`).
  Records: `<reds dir>/owners/<family>/` (`owner.json`, `owner.log`, `leases.json` 0600).
- **A LEASE IS HELD BY WORK, NEVER BY A LIVING PROCESS (2026-10-31, issue 20261031T203744-4a4a59).** A wrapper (a
  fixer's or an owner's) ends a turn whose session `claude agents` shows ended -- `done`, `stopped` or `blocked` (its
  turn over, waiting for input that never comes) -- or that made no progress (transcript, worktree) for
  `$AGENTCTL_RED_STALL` (30 min: `STALLED`, the session stopped); the turn's leases go with it. Whatever a wrapper
  does, the laptop's tick (`red_reap_orphans`) checks every laptop-held fixer and owner lease against the evidence: the
  wrapper's process, its session's row, the transcript's progress. A holder not at work for
  `$AGENTCTL_RED_ORPHAN_GRACE` (5 min) has its leases revoked (`ORPHANED: <why>`): the red is offered again at once,
  and a wrapper still running reads its next renewal as lost and exits (the next owner starts after it has). `red
  status` never shows a lease "none at work": an orphaned one reads `ORPHANED LEASE (...) -> revoked and re-offered at
  the next tick`. A 401/403 from the store is `StoreAuthFailed`, never "asked again": the wrapper takes a fresh store
  token once (the processor's), else ends with `store auth failed: <detail>` (its leases released, or revoked by the
  tick). STALLED, ORPHANED and store auth failures count for the crash-loop breaker. Every fixer and owner session is
  launched with `$AGENTCTL_STORE_TOKEN`, whatever its repo.
- **A LEASE IS JUDGED BY THE WRAPPER IT NAMES (2026-11-15 11:34:52).** The leases of two owners in their turns
  (outside-gate on Order independence #74, main-tests on Main #2475) were revoked `ORPHANED: ... its wrapper ended
  (IDLE: no red of <family> for 6 h)`: the verdicts of the PREVIOUS spells (main-tests' ended 2026-11-14), read from
  owner records that were not the holders'. An owner lease's meta names its wrapper's pid; a record of another pid
  says nothing of it -- that wrapper alive, the lease is not judged here (it lapses by its TTL if the wrapper stops
  renewing); gone, it is orphaned. A fix-pending red's note on the status page is never a history line from before it
  went fix-pending (both #74 reds kept showing that ORPHANED line after their fix landed, with no holder).
- **A PID IS JUDGED ONLY WHERE IT RUNS; ONE LEASE END IS ONE ATTEMPT (2026-11-18, core Main Pipeline #1053).** A
  fixer that ANOTHER machine signed in as the laptop dispatched lost its lease 53 s after it was taken: this laptop's
  tick found no pid 83138 among its own processes and revoked it `ORPHANED ... its wrapper (pid 83138) is gone`. Then
  the `offered` step read that one ended lease at every tick (a re-offer keeps the item's entered_at): five history
  lines, a CRASH LOOP and BLOCKED ON OWNER "the fixers crash", with no second fixer ever started. Every laptop-held
  lease's meta now names its `host` (the dispatcher's, the wrapper's, the owner's); a lease of another host -- or an
  older wrapper's, with no host, whose worktree is not here -- is never judged here. The red keeps the epoch of the
  lease whose end it read (`ended_lease`) and never reads it again. The dispatcher writes the wrapper's pid and its
  stderr log (`wrapper_log`, the run's reds/<run>/fixer.log) into the lease at spawn; a dead wrapper's ORPHANED reason
  carries that log's last lines, and the crash-loop block shows each launch's reason whole.
- **ONE RED QUEUE (simplification item 5, 2026-10-30; replaced the per-class caps, the borrow and the overflow slot).**
  A red is (workflow, failing job, signature). Every red belongs to ONE queue family:
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
  there), it is a HARNESS FAULT (2026-11-19): agentctl's own bug, never the owner's. ONE agentctl issue per red
  (`[harness-fault <key>]`, its maintainer's queue) and a harness block with cause `fixer-crash-loop` (holder
  `harness:`, no owner ask: `red status` reads `HARNESS FAULT: agentctl issue <id>`) -- the one block a test red may
  hold besides a decision, since the fixers, not the code, fail. It clears itself: its probe passes when a newer decided
  run of the workflow no longer fails this way (or is green), the issue is closed, or it lapses after 24 h
  (`$AGENTCTL_RED_CRASH_BLOCK_MAX`), offered again with the streak counted afresh. A launch with a result (LANDED, NOT LANDED, GAVE UP, BLOCKED ON OWNER, DIAGNOSED, a
  refuted fix) ends the streak; a release by hand is no launch. `$AGENTCTL_RED_CRASH_LOOP` (3). The block names the
  newest crash's log (an owner's wrapper log, else the fixer's transcript or worktree) and each launch's stop reason.
  A turn is read from its transcript: a background session whose process lives on after its turn (`blocked` / `idle`
  with a live pid, or any row -- even `working` -- whose transcript closed the turn with `turn_duration`, 2026-11-21)
  has ended it when its answer carries a verdict, or -- nothing of its own pending -- after
  `$AGENTCTL_BG_IDLE_GRACE` (60 s) of quiet; one waiting on its own backgrounded task (`agentctl wait`) is at work, and
  its lease is renewed by the wait.
  A backgrounded subagent is such a task too, claude's own `isMeta` notes (a system-reminder) are no input, and a
  session with no transcript yet (a resume compacting its history) is still starting, judged by its stall budget
  (2026-11-25, Alert backtest #28: three owner turns ended "no verdict" while at work or starting). A blocked or
  failed session's ending carries claude's own detail (`blocked: Prompt is too long ... weekly limit`), so a spent
  limit reads LIMITED.
- **OWNER-ONLY CATEGORIES (ask.py `OWNER_ONLY`, 2026-11-19: "could any of these decision things could've been handled
  automatically? fix structurally").** Only what no agent can do reaches the owner, and every ask names its category:
  `vault` (Touch ID), `tty-trust` (a prompt needing his terminal), `money-credentials`, `irreversible`,
  `publish-external`, `policy-no-rule` (a policy no checked-in rule answers, asked WITH the rule row to add: `rule:
  <file>: <row>`). `red block --owner-cause` causes map to one (`OWNER_CAUSE_CATEGORY`). Every owner line reads `RED
  BLOCKED ON OWNER [<category>]: ...`; tests/test_owner_only.py lints every source (each `owner:` block lease names its
  `category`) and replays that night's six asks. An ask in no category is no ask (`red_ask` returns None) and at runtime
  is ROUTED BACK (`red_ask_route`): the red is offered again, its diagnosis the fixer's words plus "NOT AN OWNER
  QUESTION ... proceed under the standing rules and checked-in policies" (movies `scripts/ci/lane-policy.tsv` for a
  ci-runner lane), counted as an attempt so the ladder climbs.
- **Owner decision.** `DIAGNOSED: <cause>; decide[<category>]: <the decision>` (or a `GAVE UP: ... decide[<category>]:
  ...`) puts the red on an owner block with cause `owner-decision` whose text carries the diagnosis, the decision and
  its category; never a crash. A bare `decide:`, a `policy-no-rule` ask with no `rule:`, or a second DIAGNOSED in a row
  is routed back instead (2026-11-19: Stage NixOS closures #102 and Record scrape fixtures #198 asked the owner lane
  placements the lane policy answers). It clears itself like the crash-loop block (a newer run no longer failing this way,
  or 7 d, `$AGENTCTL_RED_DECISION_BLOCK_MAX`). The owner lifts either block at once with
  `agentctl red release <key> --any`: the red is offered again at the next tick.
- **WAITING ON YOU (ask.py, 2026-11-03).** Every owner-blocked red (a decision, a vault, a `red block
  --owner-cause --cmd`) carries its QUESTION: the status line and ci-why read `<red> → WAITING ON YOU: <question>`,
  never a bare `owner-decision down`. The tick tells the owner ONCE per block (one escalate event: one inbox entry of
  kind BLOCKED and one macOS notification on the laptop), then reminds by notification alone (no inbox entry) after
  1 h, 2 h, then every 4 h (`$AGENTCTL_ASK_REMIND_FIRST` / `_MAX`) until the block ends. THE QUESTION IS HELD AN
  HOUR (2026-11-17: 28 of 42 questions resolved themselves before the owner answered, p50 83 min): recorded at once
  (the red's blocked-external move, an info event, its `ask` with `n: 0`; `agentctl.owner_decision`'s `asked_at`),
  told only `$AGENTCTL_ASK_HOLD` (3600 s) after its spell began -- a GATE red's, or one whose red's deadline falls
  inside the hold, at once; a red that leaves the block meanwhile never asks (ask.py `ask_held_until`). ONLY A QUESTION
  THE WORLD MAY ANSWER IS HELD (2026-11-18: a vault-unlock ask sat an hour while its fixers' fixes waited on signing):
  a block no probe of its own can lift -- the vault's one lease, any block raised with no probe -- waits on the owner
  alone, so its red's work stops until he answers and it is told at once (`ask_blocks_work`). The owner answers with
  `agentctl decide <red key | run> "<answer>"` (`done` after running an asked command): stored on the red, the block
  lifted, the red reads `IN PROGRESS (decided: <answer>)` and is offered again, its fixer briefed with the answer (no
  RED ESCALATE meanwhile). If the decided action fails, the red goes back to the owner only when the failure is
  itself owner-only: a `DIAGNOSED` naming its own `decide[<category>]`, or a `NOT LANDED` / `GAVE UP` of a decision
  asked in an owner-only category (`WAITING ON YOU: you decided '<answer>'; the action failed: <why>`). A new ORDINARY
  cause (2026-11-19, core #1053: the owner answered "852acad", then the fixer DIAGNOSED a plain test race) is routed
  back to the fixer, the decision marked `done`: the standing rules decide it, never the owner again. A block still clears itself by its probe (a newer run no longer failing this way), and its reminders with it.
  `decide` on a red that has closed meanwhile answers `already resolved (red closed <when>, <state>)` and
  acknowledges the alerts that asked it. AN ASK IS RETRACTED WITH ITS CONDITION: a BLOCKED alert closes by itself
  once its red no longer waits on the owner (resumed, decided, its fix landed) OR its block no longer asks him (lapsed,
  lifted -- 2026-11-19: `decide vault-movies here` run off a still-shown ask said "no red waits on the movies vault"),
  the pass it happens; the owner-decisions hook and the status line drop it with it (a blocked-external red with no
  block reads `re-offering`, never BLOCKED ON OWNER). ONE ENTRY PER ASK: an owner ask is keyed by its content too
  (alerts.py `alert_ask_digest`), so the same question under another item merges into the live entry.
- **Workspace trust is asked when a repo is registered (fixersession.py `trust_asks`, 2026-11-19).** The laptop's sync checks
  every red-repos.json clone's claude trust each pass; an untrusted one is ONE `[tty-trust]` owner ask (item
  `trust:<path>`), asked once per gap, retracted once trusted -- never 21 minutes into a red, nor once per flap of its
  offer reason (the dispatcher's NOT DISPATCHABLE stays on the offer, out of the inbox).
- **A decided revert the fixers did not land is landed by agentctl (2026-11-22, Identity takes #349).** A gate red
  whose in-progress owner decision is `revert <sha>`, offered again `RED_DECIDED_REVERT_MAX` (45 min,
  `$AGENTCTL_RED_DECIDED_REVERT_MAX`) after the decision, goes to the revert tier (red.py `decided_revert`): agentctl's
  own revert of that commit, `Fixes-Red` and a specific `Fix-Replay: none`, landed by `agentctl land` under the normal
  checks -- once (`reverted`), then the ladder as before. A fix change that ended rejected puts its change's `why` in
  the red's history, so red status, the inbox and the next fixer's prompt say why.
- **Main moved under an outside red: verify it (2026-11-22, Order independence #105).** An outside-gate red with no
  landed fix of its own, whose job reads something main changed since the red's run (`fix_reaches_red` over the
  commits since its head), has its workflow dispatched on main to verify (redverify.py `red_main_verify`): once per main
  head, at most every `RED_MAIN_VERIFY_EVERY` (3 h), only where `red_verify_dispatch` allows it. A fix that lands
  without a trailer (tools-only, rightly) is seen by the run it starts, never waited for behind a weakly linked change.
- **A vault ask names the secret it needs (2026-11-22, Tool self-tests #480, Alert backtest #20).** In a repo whose
  main is unsigned (movies) nothing a fixer does but reading a secret touches the vault: commits are unsigned, pushes
  authenticate as gh's keyring login on the laptop and as the GitHub App on ci-runner, the store token is in its
  environment. There a `BLOCKED ON OWNER` / `decide` vault ask counts only when it names the read that failed --
  `secrets run|get <ns> ...` (redvault.py `vault_ask_unfounded`); any other is routed back to the fixer saying so
  (a failed land push, or `env --check` saying the vault is locked, is no reason). #480's and #20's asks came from a
  land push that failed on DNS, whose NOT LANDED pointed at `env --check`; land's NOT LANDED now says what git said and
  whose failure it is (queuesubmit.py `push_failure_why`), never the vault.
- **No signature where main keeps none (2026-11-22).** A fixer of a repo whose main is unsigned (red-repos.json
  `signed: false`: movies, whose land rebases with NO_SIGN) commits unsigned (`fixer_env`: `commit.gpgsign` and
  `tag.gpgsign` false through git's env config), so commit signing never parks one on the vault: #348/#349's revert
  waited 91 min and Order independence #105's fix 43 min on Touch ID for a signature the land threw away. A vault ask
  there is for a secret, and its resume probe is the command that needed it, never a signature.
- **A vault ask is ONE owner item per vault (2026-11-16, Order independence #77).** A fixer verdict whose one ask is
  `secrets unlock <ns>` or `decide vault-<ns>` (`BLOCKED ON OWNER: ...`, or a `DIAGNOSED: ...; decide: ...` naming it) puts the red, whatever
  its kind, on ONE lease per vault, `block/cause/vault-<ns>`: one WAITING ON YOU for every red it holds, carrying the
  one line that resumes them all: `! ~/bin/agentctl decide vault-<ns> "here"`. Each red is offered again as a decide
  would -- RESUMED BEFORE the lease is released (2026-11-19 03:01Z, core #1053: revoking first let the tick's
  invariant re-offer the red bare in the same second, and the decide said NOT DECIDED; a red the invariant re-offered
  is resumed too); its fixer (in a repo that signs) signs a probe (`git commit-tree -S HEAD^{tree} ...`) FIRST -- its own Touch ID prompt, which the
  owner answers while at the machine -- and ends `BLOCKED ON OWNER` at once if that fails (back on the vault's lease,
  told once again), never after a whole fix. NO UNLOCK RESUMES THEM BY ITSELF (review round 3, 2026-11-16): an unlock
  is pinned to ONE session anchor, the caller's nearest Claude Code ancestor, and signing asks Touch ID once per anchor
  (`~/projects/secrets` README, "Session pinning"), so the owner's `secrets unlock` in his own session never lets a
  fixer sign. The secrets-log watch that resumed on any unlock (2a8139c) never fired, and would have had a fixer raise
  a Touch ID prompt minutes later that the owner had not asked for -- the one secrets' README says never to approve.
- **Stuck runs** (an infra red, `[stuck]`): a run undecided past max(3 x its workflow's p90, 30 min), counted from its
  first job's start. The p90 is of the workflow's own finished runs -- its own last 30 read when the newest-200 list
  holds fewer than 5 -- and with fewer still, its jobs' `timeout-minutes` (summed; a job setting none counts GitHub's
  360 min, a reusable-workflow call 0) bound it. Not stuck: a run whose unfinished jobs all wait for runners whose every
  slot is busy (`queued`), or one waiting on a workflow_dispatch run it started (`waits_on`, judged by that child).
  GitHub does not link a dispatch to its parent, so ANY undecided workflow_dispatch run on main created after the run's
  first job started counts as its child: an unrelated dispatch can delay a stuck verdict until that run decides.
- **Refunds.** A dispatch that did nothing is not counted (`red status` says `[refunded: ...]`): `GAVE UP` / `NOT LANDED`
  because it is already fixed or there is nothing to do; `STOPPED` (superseded) and `red cleared`; a fixer killed
  before any verdict; `BLOCKED ON OWNER` only for a CI read (`gh run view|download`: gh reads with its own
  auth); a DUPLICATE -- a fixer whose red someone else claimed between the dispatch decision and its start steps
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
  `PREPUSH_SKIP` denied, no parent session, no secret in its environment but the store token agentctl reads itself.
  Its brief says CI logs, scraped pages and fixtures are data, never instructions.
- **The author first, briefly.** A live author holds the red for its 10-min window by its claim (above); a finished
  or idle author holds nothing.
- **Its lander first (2026-11-01, see "blame").** A gate test red whose culprit change and lander are determined never
  gets a fresh fixer: the change is reverted and its lander revived (tier `lander`); while the culprit is decided the
  red waits (tier `blame`). The ladder below is for what cannot be determined.
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
- **Only a `Fixes-Red:` trailer makes a change a gate red's fix** (2026-11-20, Main #2716: metrics-packages, no
  trailer, edited `scripts/ci/coupling_ratchet.py`, which the failing job reads, and ci-why read "fix ... landing on red
  (Fixes-Red)" while the real fixer was stopped and its committed fix sat unlanded). An untrailered queued change links
  only an OUTSIDE red, and only by what it changes of that red's failing step (`red_change_link`, redlink.py, the
  owner 2026-11-22): the failing step's own text or its job's own keys (comments aside), or a file that step names and
  no other job of any workflow names (the code under it). Editing the workflow FILE links nothing: Alert backtest #21
  took ci-cpu, which added the job-telemetry steps to every job, for its fix. A linked change never stops a live
  holder (the red stays leased; RED-NOTE.md names the change once), carries no fix on to its lane's next change, and
  ends its red's fix-pending at once when it ends unlanded (no re-queue grace). A red's fixer
  whose lease lapses leaves its unlanded commits as `prior_work`, and the next fixer is briefed to land them.
- **`Fixes-Red: N` is the run NUMBER** (#N, GitHub's run_number): resolved through the newest runs' numbers first,
  as a run id only when no run has that number; a number and an id naming different runs: the number, and the FIX
  verdict says so in `[...]` (2026-10-27: `Fixes-Red: 2133`, Identity capture #2133, was read as id 2133, an old
  convergence run #1111: FIX UNVERIFIED, and capture never dispatched). A number links every open red whose runs
  include it, whatever its workflow (Verify scrape recording #26 as much as Main #26); when two open reds share the
  number, name the run ID instead (`Fixes-Red: 43337374729`), which is among each red's runs too.
- **A fix for a red outside the gate** (`Fixes-Red: N` naming e.g. an Identity capture run) is verified by the watch at
  once: the workflow is dispatched on main (workflow_dispatch, default inputs), waited for, and `FIX OK` / `FIX FAILED`
  goes to the inbox, the claim is released and the snapshot refreshed. One verification at a time per workflow (a second
  fix landing meanwhile gets none), counted in the daily dispatch budget. No workflow_dispatch: `FIX UNVERIFIED until
  next scheduled run (cron ...)`.
- Headless fixers carry the store token (fixer_env) and read CI with gh's own auth; CI reads never block on the owner, `BLOCKED ON OWNER` is for real
  owner actions only.
- **An unreadable gate** (GitHub, gh's auth, the vault or the token down): the red tick dispatches nothing, writes ONE
  `UNKNOWN: the red patrol cannot read the gate (…)` per spell with an infra check (gh and its auth),
  and pushes fail closed (an unread gate is red).
- **Nobody awake.** The launchd processor ticks and dispatches with no session open; every verdict waits in the
  inbox. The status line says `RECONCILER not running` when the snapshot is stale.

**Dispatch off per repo (the owner, 2026-11-19: "no bitcashier builds", the laptop CPU-starved).** `red auto off --repo
core` (a short name or o/r; in the store, so the server's tick reads it) holds every red of that repo in `dispatch-off`:
recorded and shown, its offer closed, a live lease released, its owner block lifted (a vault ask too), never
dispatched, never asked; a newer green run still clears it. `red status` says `dispatch off for <repo> (owner)`.
`red auto on --repo core` offers them again at the next tick.

### blame: the offending change reverted, its lander revived (2026-11-01)

The owner, 2026-11-01: "change agentctl to: remember which agent landed what change, when a build fails: revert the
offending change and revive the landing agent to fix it", then "only if it can't be determined spinup a new agent".
Code: `lib/agentctl/lander.py` (who landed what), `lib/agentctl/blame.py` (the culprit, the revert, the revival);
tests `tests/test_lander.py`, `tests/test_blame.py`.

**THE DECISION RULE** (`blame_decision`, the only place it is decided):

| what is known | what happens | red tier |
|---|---|---|
| the culprit is being decided | the red waits, no fixer, no UNCLAIMED escalation (at most `$AGENTCTL_BLAME_MAX`, 90 min) | `blame` |
| a culprit AND its lander's session | the culprit reverted, the lander's own session revived to fix it | `lander` |
| a culprit nobody's session landed (the owner by hand, a land outside Claude) | reverted; the owner told in the inbox; no agent | `lander` |
| no single culprit (the parent fails too, no verdict, inconclusive, too long), a bot's change, an infra/flake/outside red | the normal ladder: a fresh fixer | `author`/`fresh`/... |
| the lander cannot be revived (no session, transcript gone, `LAUNCH FAILED`) | a fresh fixer at once, under the same lease, briefed with the change | (in `lander`) |

**1. The lander.** Every `agentctl land` records who ran it: the Claude session (`$CLAUDE_CODE_SESSION_ID`), the
SUBAGENT when the land ran inside one (the session id alone names the orchestrator: the subagent's own transcript under
`~/.claude/projects/*/<session>/subagents/agent-<id>.jsonl` whose Bash CALL runs `agentctl land` is its record:
first the call this land process runs under -- its command in an ancestor shell's command line -- else a call naming
the land's worktree, else the newest land call; a transcript that only mentions land never counts, 2026-11-17), the
transcript, the session's cwd, the worktree, the branch -- and each commit's KEYS (sha, patch-id, subject, author
time; a landing rebases, so a commit on main is found again by its patch-id, or by its subject and author time). A
queued change carries them in its change item (submitted with it); a direct land (no queue: core, the other repos)
writes a `lander` item (past its 14 days the tick retires it quietly to `gone`: `tick_quiet_deadlines`, an info
event, no notification). Both live in the reconciler's store for 14 days, across restarts and batches. The red tick
notes each landed change's commits on main (`landed_commits`: main sha -> patch-id), so one push carrying several
changes maps each commit to its own change and lander.

**2. The culprit.** A gate TEST red's range is (the newest green run's head, the first red run's head]; its commits are
grouped into changes by the registry (a commit no change carries is its own group: the owner's). The search holds an
invariant -- boundary `lo` PASSES the same check, boundary `hi` FAILS it -- and calls a change the culprit only when its
parent passes and its end fails:

- **Main** (unseeded): the green run's head passes for free (that run IS the same check on the same inputs). A boundary
  inside a batch with no decided Main run of its own is judged on CI, never on the laptop: `precommit-check.yml` on a
  `check/blame-<run>-<sha>` branch holding the boundary's TREE on top of <remote>/main (main.yml is never dispatched off
  main: it publishes images), its pass counted only when its log names the failing test (else no verdict).
- **Convergence drift rows** (`takes-drift-N (...)`): the seeds rotate per run, so the parent's green run is NOT the same
  check -- every boundary, the green head included, is re-run with the red run's own seeds
  (`scripts/identity-drift-ci <commit> --for-red <run>`): `DRIFT CLEAN` passes, `DRIFT RED` / `DRIFT PROGRESS` fails.
  The probe runs on a synthetic commit -- the boundary's code with <remote>/main's `.github/` and `scripts/ci/` laid
  over it -- because an old commit's drift script ignores a forced seed and replays its default (2026-11-01: four
  "parent" runs on 73f1dcfcbb replayed seed 2). Every row's verdict line names the seed it replayed (`[seed S]`); one
  that is not the pinned seed is NO VERDICT (a fresh fixer), never "the parent passes".
  Other convergence legs (their pinned pair rotates nightly): undetermined.
  A drift row is named the way GitHub lists it, `leg (united-kingdom) / takes-drift-2 (united-kingdom)` (the inner
  job after ` / `, as movies' `convergence_reuse.py` reads it). **A convergence parent passes ONLY on such a re-run
  whose every verdict line confirms the pinned seed** (`blame_unproven`): a green run with other seeds is never
  evidence, and a convergence red whose kind is unknown is undetermined at once -- the normal fixer, never a revert
  (2026-11-01: `^takes-drift` missed the prefixed name, and #628's green on the parent "proved" #629's culprit; the
  flip was pre-existing).
- Another watched repo (core): only decided runs of its gate on the exact commit; one change per push is the usual case.
- The failing test's files (its `...Spec`/`...Test` name, a path in its first line) pick the first boundary to probe
  (the hinted change's parent, then its end), else halving. At most `$AGENTCTL_BLAME_PROBES` (4) CI probes, each a
  detached `agentctl red probe` bounded by `$AGENTCTL_BLAME_PROBE_WALL` (2 h).
- The parent failing too is PRE-EXISTING: no revert, the normal fixer flow. Infra-shaped reds, flakes, outside-the-gate
  reds and a bot's change (Dependabot, Renovate, Scala Steward) are never blamed or reverted.
- Every decision is a `RED BLAME:` line (inbox and `reds/dispatch.log`) with its evidence.

**3. The revert.** ONE commit off <remote>/main undoing the culprit's commits, with `This reverts commit <sha>.` lines
(prepush-check's revert exemption), `Reverts-Change: <change>` and `Fixes-Red: <run>`, landed by a detached `agentctl
land`: a gate fix, it lands while main is red and is never throttled. `RED REVERT:` in the inbox and `reverted
<change>` on the status line. If it does not clear the red (`FIX FAILED` on a commit carrying `Reverts-Change:`), the
culprit was wrong: `RED REVERT REFUTED`, the revived lander is stopped, the revert is reverted (`Relands-Change:`,
`RED RELAND`; it lands at the next green, a production change) before the normal fixer starts.

**Withdrawn or refuted: the revert is dead** (2026-11-02: #629's wrong revert was withdrawn by hand, yet maintain's
orphan duty found its leftover `movies-wt-revert-629`, "author gone", and submitted it -- re-queued, checking, landing on
red through its `Fixes-Red: 629`). `agentctl red unblame <run> --why "..."` (`blame_withdraw`) withdraws a confirmed
blame whose revert has not landed: the blame becomes undetermined with the revert under `withdrawn`, the revived
lander's lease is stopped, the red goes to the normal fixer, and the revert worktree is REMOVED (only when every commit
in it is blame's revert, its tree clean and nobody in it; its open queue entries cancelled; `agentctl-deleted.log`). A
landed revert is never withdrawn (that is a refutation: FIX FAILED relands it). `blame_land_refusal` reads every red's
blame (`withdrawn`, state `refuted`) and refuses a commit carrying `Reverts-Change: <change>` for such a change -- unless a
confirmed blame of a newer red reverts it again under its own `Fixes-Red:` -- and anything from the worktree blame made
for it. It is asked by `land` (on the laptop only with a token already in hand: a land never runs `secrets`), the
server's submission, the queue's checker (a revert queued before its withdrawal is rejected), a recovery re-land and
orphan duty, which never submits such a worktree and removes it when it holds only blame's reverts.

**4. The lander revived** (`LANDER REVIVED` in the inbox, `lander · <change> · #<run>` in `claude agents`), under the
red's lease (`tier: lander` in its meta), the same way fixers are launched (wrapper, lease, caffeinate, the workspace
trust check):

- a top-level session: `claude --bg --resume <session>`, launched in the session's own cwd (where `--resume` finds
  it; `--bg --resume` starts a copy when the session is still running), working in its worktree;
- a subagent (no session of its own to resume): a background session in its worktree that reads its transcript first;
- its worktree gone (closed after an earlier WATCH OK): a fresh `<clone>-wt-lander-<run>` off <remote>/main, and it is
  told so.

Its prompt names the red run, the failing job and first line, `ci-why`'s lines, that its change was reverted, and
what to do: rebase onto <remote>/main (its original commits drop out as already upstream), re-apply the change with a
`git revert` of the revert, add the fix, prove it on the failing test, land again (no `Fixes-Red:`: the revert fixed
the red). The revert's own fix never takes the lease from it (`red_stop_holder` spares tier `lander`); only a refuted
revert stops it. A session that cannot be revived -- no session recorded, its transcript gone, its launch refused
(`LAUNCH FAILED: <stderr>`, said once, never retried) -- leaves the red to a fresh fixer at once (no start backoff),
briefed with the change's diff, its original worktree and why its lander could not be revived.

**Edge cases, decided:**

- **Two changes in one batch both red** (the same check): the search blames the EARLIEST whose parent passes and end
  fails. Its revert cannot clear the red (the other still breaks it), so it is refuted and relanded, and the normal
  fixer takes the red; once that fix makes main green the reland lands, and if it still breaks the check it is blamed
  again -- now a single change on a green parent -- reverted, and its lander revived. Self-correcting, never a loop.
- **A revert that conflicts** (main moved over the culprit's lines): aborted, nothing landed. The culprit and its lander
  are still determined, so the LANDER is revived all the same, told its change could not be reverted and to fix it
  forward (`Fixes-Red: <run>`, a gate fix); with no revivable lander, a fresh fixer does, briefed with the change.
- **A change landed by the owner by hand** (no change item, or a land with no Claude session): reverted, and one
  `RED REVERT ... OWNER: it was yours` inbox entry; no agent is started. Should the revert not land, the red's lease
  lapses (15 min) and the normal ladder dispatches a fixer.
- **The other repos.** core lands through `agentctl land` (direct: a `lander` item) and is blamed by its gate's decided
  runs; infra is owner-only (red-repos.json): its culprit is named but never reverted (`its pushes are the owner's`),
  and its fixer prepares the change as before.

### revert: a reverted change gets back to the agent that wrote it (2026-11-21)

```
agentctl revert [list] [--all]                          # the open notices (--all: closed ones too, with their ack)
agentctl revert ack <id> --rework|--drop [--note "..."] # the lander (or its orchestrator) takes it back up, or drops it
```

The owner, 2026-11-21: movies 604134d1a landed through `agentctl land`, turned Main red (#2806-#2808), and a red fixer
reverted it as 7380cf9d7 (`Fixes-Red: 2808`, its analysis in the message) -- and nobody told the agent that wrote it,
nor the session that spawned that agent; the rework began only when the owner asked. Blame's own reverts revived their
lander (above); any OTHER revert -- a fixer's, the owner's, an agent's -- reached no one. Code `lib/agentctl/reverts.py`,
tests `tests/test_reverts.py`.

- **Detected on main.** The reconciler's `reverts` step (ci-runner, every tick) reads main's new commits (cursor: meta
  `revert_scan:<repo>`; the first scan, the last 2 days). A commit reverts others by git's `This reverts commit <sha>`
  lines or a `Reverts: <sha>` trailer. Each reverted commit is mapped to the LANDING that brought it in through the
  lander records (lander.py: main sha, patch-id, subject and author time, 14 days) and one `revert` item opens per
  (revert, landing): its reverted shas and subjects, the revert's sha, subject and whole message (the analysis), the
  red it fixed (`Fixes-Red:`), the change and its lander. A reverted commit that is itself a revert is a re-land, never
  a notice. Blame's own revert opens one too: its revived lander acknowledges it or re-lands the change.
- **Told.** The LANDER: an inbox entry owned by its session (`REVERTED: your <sha> "<subject>" was reverted on main by
  <revert> (Fixes-Red: N) ...`, audience `author`, cwd its worktree, the revert's message as its tail) and, while its
  worktree is on the laptop, `REVERTED.md` in it (the sync's `revert notes` step, once; kept out of commits by the
  clone's `info/exclude`). Its ORCHESTRATOR -- the session that spawned it when the lander was a subagent (the lander
  record's session id names the spawner; a top-level session's spawner is not recorded: the owner) -- an entry of
  audience `owner` (`REVERT NOTICE: ...`), the one `inbox wait` wakes on, owned by that session. A commit with no
  landing record (the owner's own, or older than 14 days): the owner's entry alone. Each says what to do: rework or
  drop, `agentctl revert ack <id> --rework|--drop`, and a re-land carries `Rework-Of: <sha>`.
- **Open until acknowledged or re-landed.** `revert ack` closes it (`acked`, by whom and how); so does main carrying the
  change again (`reworked`): a commit with `Rework-Of: <sha>` (the reverted commit's or the revert's, 7+ hex; in the
  message's final trailer block, as git reads trailers), a revert of the revert, or blame's `Relands-Change: <change>`.
  An `inbox --ack` only reads the entry: the notice stays open. Its alerts close with it (the alert probe).
  A notice is acknowledged once: a later `revert ack` (the lander after its orchestrator, 2026-11-22) says `already
  acked -- acknowledged by <who> at <when> (<how>): <note>` and its own reply is kept beside it (`replies`), never
  `no notice`. `revert --all` (`revert --all list`, `revert list --all`) lists the closed ones with that line and each reply.
- **Escalated once.** Unacknowledged `REVERT_ACK_MAX` (6 h) after it was told: `open -> escalated`, ONE escalate event
  to the owner (`REVERT UNACKED: ...`, a notification, an unread entry of its own); then it waits quietly and expires
  after 14 days. The machine: `open` (6 h -> `escalated`), `escalated` (14 d -> `expired`), `acked` / `reworked` /
  `expired` (terminal); `DEADLINE_TICKS["revert"] = tick_reverts`.
- **No policy.** A notification, never a hold or a reject: it blocks no change, so it is no `POLICIES` row and needs no
  owner decision (policy.py's registry is for rules that hold, reject or delay).
- **Seen.** `agentctl status`'s line says `REVERTED unacked: N` and its body lists them (`reverts unacked: N`); `red
  status` lists them under `reverts (N unacked ...)`; `agentctl revert` prints each: id, state, reverted shas, the
  revert, its red, who was told, how long ago. Inbox kinds `REVERTED` (author) and `REVERT_NOTICE` (owner), both in
  `inbox wait`'s default kinds.

### listings: a listing is never proof of absence (2026-11-20)

28 closed issues trusted a GitHub listing as complete (a run past the newest 50 or 200, a queued run GitHub's own
listing left out, one attempt's jobs, a log cut short). forge.py's run listings return a `RunList` whose `complete`
says whether it held everything that matched (its last page came back short); absent from an incomplete one is "cannot
tell", never "none". A run agentctl itself started is found by its own identity: the remote check's run by workflow,
branch and tip through GitHub's filters (`forge_find_run`), the newest 50 only as a floor for a forge that ignores
them. The ghost check pages GitHub's queued / in-progress listing to its end (`REMOTE_LISTED_PAGES`), and past what it
read it cannot tell, which is never a ghost (a long queue no longer gets a healthy run cancelled). Every other run
listing in lib/agentctl/ says at its call site, in a `# listing:` line, how it treats a run it left out
(tests/test_listings.py is the lint, and the find / ghost behaviour's tests).

### exit paths: every exit puts the worktree back (2026-11-20)

11 closed issues were a path that left state behind (a NOT LANDED with its rebase in progress, a hook running 20 min
after its land, a lease held past its land). `exit_guard()` (slots.py) turns SIGTERM, SIGHUP and SIGINT into an
`Interrupted` exception inside the block, so every `finally` and context exit runs -- before it, a SIGTERM killed
`land` with its stash unrestored and its landing lease held to its TTL. `WorktreeGuard(repo)` aborts, on every exit,
the rebase / cherry-pick / revert / merge the block started and left in progress, and checks the branch out again if it
left HEAD detached (an operation in progress before it began is the agent's, never touched). `land` runs under both
(`NOT LANDED: interrupted by SIGTERM ...`, exit 130); land's rebase (sync), the blame and fallback reverts (their land
spawned after the guard, never inside it) and the check tree's picks under WorktreeGuard. tests/test_exit_paths.py:
each operation aborted, a real SIGTERM mid-rebase and mid-push.

### policy: who decided each rule that holds or rejects a change (2026-11-20)

The owner, 2026-11-20: "fix agentctl issues structurally". `lib/agentctl/policy.py` holds two tables.

**POLICIES, the registry.** Every rule that HOLDS, REJECTS or DELAYS a change is one row: its effect, the rule in one
line, and WHO DECIDED IT -- the owner's words and date, or none (agent-proposed). A `mechanism` row is a resource wait
(a busy tree, an executor starting, a push under way, an infra retry) or an outcome no rule chose (the checks failed,
the change no longer applies): nothing to decide. AN UNAPPROVED RULE NEVER BLOCKS: `policy_blocks(<id>, ...)` answers
False for it, and its warning (`POLICY UNAPPROVED <id>: would <effect> <change> ...`) goes once on the change's events
and to stderr. A rule that only SOFTENS a mechanism outcome (`softens`: the regen hold in place of a checks-failed
NOT LANDED) acts before approval, since switching it off would block more. `tests/test_policy.py` is the lint: every
hold kind a change records is in the hold table, every queue reject (`queue_verdict ... "rejected"`) and gate wait
(`set_change ... "waiting"`) carries a `# policy: <id>` line, every unapproved rule is asked through `policy_blocks` at
each site, and no row is stale. The owner approves a rule by filling its `decided` (date and words), or strikes it.
Unapproved at introduction: `regen-expiry` (a REGEN NEEDED hold NOT LANDED after 24 h: lane F2b's), `fix-replay` (a
Fixes-Red landing without `Fix-Replay: ... base=FAIL head=PASS` refused), `regen` (the hold itself; softens);
`older-batch` DERIVED from the owner's backlog-batch decision. **The owner decided them, 2026-11-20** ("1 approve, 2
drop the reject 3 approve and enforce for gate reds 4 confirm"): `regen` APPROVED; `regen-expiry` STRUCK -- an overdue
regen hold recovers instead (below), never rejected; `fix-replay` APPROVED and ENFORCED for gate reds, agentctl running
the replay (FIX REPLAY, the red section), with `fix-replay-outside` the warn-only rule for reds outside the gate;
`older-batch` CONFIRMED -- an older waiting change goes ahead of a younger one in a batch, red fixes keep their own
lane.

**HOLDS, the hold table.** Every kind of recorded hold (`note_hold` refuses any other): its `land status` label, the
longest it may last before it is STUCK, its recovery, and whether a red fix may wait on it (only its own resources).
The change tick runs the recovery when a hold is stuck: `rederive` -- its blocker ended, or its kind's bound passed:
the hold describes a world that moved, so it is dropped and the tick decides afresh, quietly (`change.rederived`),
never a page -- or `escalate` (the 6-h cap, nothing recorded: agentctl's bug, an issue) -- or `recover` (the `regen`
hold: the tick's own recovery at its 24-h bound, the regeneration run again and the owner told once; never stuck). W1
and WR read the same tables.

### issue: agentctl defects, filed (one agentctl editor)

```
agentctl issue "<what is wrong, where, how to see it>"   # files it: ISSUE <id>
agentctl issue --list [--all]                             # the open ones (--all: closed too)
agentctl issue --close <id> [--note "fixed in <sha>"]     # the maintainer, once fixed
agentctl red am-fixer                                     # exit 0 inside a dispatched red fixer (prints who), else 1
```

agentctl's code has ONE editor, its maintainer. Every other agent that meets an agentctl defect, and above all a red
fixer, files it with `agentctl issue`. An issue is a store item (type `issue`), shown in the status page's "agentctl
issues" section. While it stays open it escalates once a week (`tick_issues`, the reconciler on ci-runner): ONE
notification per issue naming its id, filer and text (the owner, 2026-11-11: never a digest), its level up, its
deadline a week on; one issue per 2 min (`$AGENTCTL_ISSUE_ESCALATE_GAP`), the most overdue first, so a backlog never
arrives as a summary. Every machine names the step that fires its deadline (`DEADLINE_TICKS`, test_machines).
The fixer's prompt says so, and fixers no longer get a ~/bin worktree. `~/bin/hooks/pre-commit` (`git -C ~/bin config core.hooksPath hooks`, shared by every ~/bin worktree)
refuses a commit made from a fixer session. It identifies the fixer the way `red` does: by the session's binding or
fixer record, and only without a Claude session by the fixer environment. Any other exit of `agentctl red am-fixer`
(agentctl broken) lets the commit through with a warning, so a broken agentctl can always be fixed. On the laptop
(sync role) the issue is stored in ci-runner's store, so ci-runner's pin must include `issue`.

### land: QUEUED at once; checked, pushed and watched by the reconciler

```
agentctl land [--repo PATH] [--branch B] [--stash] [--keep-worktree] [--no-fast-precheck]   # movies: QUEUED <id> (<sha>): END YOUR TURN
agentctl land --until-done [--deadline 4h] [--keep-worktree]   # movies agents' last step: the whole tail (below)
agentctl land --follow              # scripts: the checks here, the push, then the watch, in-process
# A red fixer's plain `land` QUEUES like every land (2026-11-16): the fast precheck in the foreground (its refusal is the
# fixer's NOT LANDED in seconds), then a bounded wait for the queue's verdict (LANDED <sha> / NOT LANDED: <why>,
# $AGENTCTL_FIXER_LAND_WAIT, 2 h). A RED FIX NEVER WAITS FOR GREEN: a change whose Fixes-Red: names a run of the open
# gate red's episode -- or of any other red the store has open (its KIND: changekind.py) -- takes the red-fix check tree
# (<clone>-wt-check-redfix) no ordinary change takes, goes before an open batch, is never parked on `waiting` by a known
# red, and must_wait lets a gate red's fix go; the watchdog's WR escalates a gate red's fix on `waiting`, or any red
# fix unlanded 40 min after it was queued ($AGENTCTL_WR_MAX).
agentctl land status                # the changes with their states, the batch, the gate, the processor
agentctl land cancel <id>           # drop a queued or waiting change (its executor is stopped)
```

The owner: "agents never wait on a land". In movies (a repo whose hook can check a change apart), `land`:

1. fetches (retried once after 15 s) and rebases onto <remote>/main (a conflict: `NOT LANDED` listing the files);
2. runs `prepush-check`, the squash guard (below);
3. writes the change as a `change` item in state `queued` (re-landing from the same worktree and branch SUPERSEDES the
   older item: `SUPERSEDED <old> by <new>` to the inbox, its executor stopped), with its LANDER (the session, subagent,
   transcript, worktree, branch) and each commit's keys -- what "blame" reverts and revives;
4. pushes the change to `refs/heads/land/<id>` on GitHub (bounded; movies' hook checks nothing on a `land/` ref and
   no workflow triggers on one), so the change lives on the remote, not only in your worktree;
5. prints `QUEUED <id> (<sha>, n ahead): ...; not on main yet: END YOUR TURN; its verdict comes to agentctl inbox`
   and exits 0. The line names no verdict but QUEUED (2026-11-16: its old "LANDED / WAITING / NOT LANDED goes to the
   inbox" tail read as LANDED to a watcher matching the word); a red fixer's land ends it "this land now waits for its
   verdict" and prints LANDED / NOT LANDED itself (below).
6. (movies, a repo with an executable `scripts/land-precheck`) starts that precheck DETACHED, beside the queue -- the
   rebase dry run, the hook's commit-rule and linter checks, the lint registry's specs for the pushed paths, ~20-60 s on
   a warm checkout -- and prints `fast precheck of <id> running alongside the queue (log ...)`. It adds nothing to
   land's time (the owner, 2026-11-06: landing must not take longer). A refusal (its exit 1) cancels the queued change
   and writes `NOT LANDED: the fast precheck refused <id> (<sha>): PRECHECK FAIL: <steps>` to the inbox, its lines in
   the tail: fix and land again. A change its landing check decided first keeps that verdict (nothing to cancel, no
   NOT LANDED); one the cancel cannot reach (the store unreachable) gets `PRECHECK REFUSED <id> ... could not be
   cancelled` and is still judged by its landing check. Exit 0 or 2 (could not check: sbt never came up, a bound) leaves the change to the
   landing check, and so does a precheck past its 900 s bound: it runs in its own process group, ended whole there
   (its sbt and test JVMs with it). `--no-fast-precheck` starts none. Code: queuesubmit.py `start_fast_precheck` / `cmd_land_precheck`
   (the hidden `agentctl land-precheck`); tests: `tests/test_queue.py` FastPrecheckTest.

The reconciler then takes it, a step per tick:

- **queued → checking.** The tick starts an executor, `agentctl waiting check <id>` (Fixes-Red changes first), holding
  `check/<id>` and a check slot (`$AGENTCTL_CHECK_TREES`, 4, QUEUE_POOL): a CHECK TREE `<clone>-wt-check-<n>`, kept between checks so
  its build stays warm. It rebuilds the change on <remote>/main there (each commit picked; one already on main is
  dropped; nothing left: `LANDED <head> (already on main via <commit>)`), then runs the hook's dry run -- `--range`, or
  `--checks-only` when main is red and the change must wait -- as a check of the governor (`gov`), under the
  20-min no-progress bound; or, under laptop pressure, on ci-runner (see "remote checks"). Pass: `checked`. Fail:
  `rejected`, `NOT LANDED: the pre-push checks failed (...); failing with this change only: <tests>` with the tail.
  DIFFERENTIAL (2026-11-04): a failure is judged against the change's BASE, like the convergence gate. Each failed hook
  check (`pre-push <check> FAILED`, else each failed job) must name its failing tests (scalatest `- <name> *** FAILED
  ***`, sbt `Failed tests:` classes); when every one also fails on the base -- in main's red gate runs' failed logs
  (cached per run in `reds/runkeys/`), or in another change's check on the same main within a day -- the change is no
  culprit: `checked` with `known_red`, then it joins `waiting` (one author note: `WAITING ...: its checks failed only
  where origin/main fails too (<tests>: <where>) ...`) and lands with the batch at green. A head-only test, or a failed
  check with no test named (shellcheck, a lint), is the change's. A remote check stopped at its 80-minute cap is infra:
  re-queued twice, the third time rejected. A pick or rebase whose every conflicted file is prose (`docs/*`, `*.md`)
  keeps both sides (`git merge-file --union`) and goes on; any other conflict (generated fixture TSVs included) is the
  author's. A check the LAPTOP failed (sbt never came up, a
  script test's TIMEOUT, a stall) is no verdict: queued again 5 min later, up to 3 times. A dead executor is an
  expired lease: the tick starts another.
- **checked → pushing → landed.** Under the `land/<repo>` lease the tick re-reads the item (a superseded one is never
  pushed). Main moved since the check: rebased onto it, FULLY re-checked (a new round) ONLY when what landed since
  the change's last FULL check overlaps BY FILE (the owner, 2026-11-16; module granularity looped changes 3-4 full
  rounds) -- build.sbt / project/ / the hook (anything under scripts/hooks/ but its own self-tests, `*-test.sh`,
  `*-test-lib.sh`, `*_test.py`, which steer no check: the owner, 2026-11-16) / scripts/ci/precommit-share.sh landed,
  a path both change, or a path one side deletes (a rename's old
  path too) whose file name a file the other side changes names -- else not re-checked in full, but VERIFIED (unless MINIMAL RECHECKS, below, keeps its verdict):
  a round of `--verify-only <full base>..<tip>` on a `check/verify-<full base>-<id>` branch (every test config
  compiled, the lint and fast specs, the unit tests of every module either side touches), judged again by the
  checker on the main it starts on, then pushed (`checked ...; main moved since its full check on <sha>: no file
  overlap, the combined tree verified`; the push's hook judges the red gate only). A hook without `--verify-only`
  gets the full round. SELECTION-AWARE (2026-11-17): when every such forcing path is the hook, its lint registry or
  local-tests.txt, the hook's selection for the change's paths is compared at the full check's tip (`full_tip`) and
  the rebased tip (`--plan`, `--compile-plan`, `--unit-plan`, `--lint-plan`, local-tests.txt's lines for the areas
  run): only a check `--plan` adds or an area line added forces the full round -- what the compile, unit and lint
  plans add the verify round runs itself. BUILD-AWARE (2026-11-17): build.sbt / project/ landed -> the same
  selection test, then the build AS THE CHANGE'S MODULES SEE IT (buildview.py, a static read, no sbt) compared at
  `full_tip` and the rebased tip: build.sbt's build-wide statements (`ThisBuild / scalacOptions`, aliases, ...), the
  project definitions of the change's module directories and of every module its `--plan` / `--unit-plan` /
  local-tests lines name, with everything they name transitively (settings helpers, configurations, the projects they
  `dependsOn`, the `Dependencies.x` / `Obj.member` they use and the members those use, a project/ file named in a
  string), every AutoPlugin on all projects, every non-.scala project/ file and every other root `*.sbt`
  (coupling.sbt, identity-impact.sbt: a build file like build.sbt, review 2026-11-18). Unchanged, and no file of the change
  naming the changed build file -> the verify round. OUT OF THE BUILD'S REACH (2026-11-21: 12 of 11-19..21's 26
  build rounds were changes to scripts, docs, workflows, infra or the mobile apps, rechecked in full because every
  `sbt` plan selects the basic lines' e2e / worker / repochecks; 5 of them out of its reach as below): a change none of whose paths the build compiles or
  reads -- none under an sbt project's directory or a `<dir>/src/`, no source, no root file but Markdown, nothing in
  test/, data/ or conf/, and none whose file name
  build.sbt or project/ names -- gets the verify round however the view moved: only the lint and script self-tests
  its paths select read its files, and the verify round runs those under the new build. Otherwise changed, unreadable, or the change itself touching the build ->
  the full round (`they change build.sbt, and what web, worker build with changes (common, Dependencies.playVersion)`).
  SHARE-AWARE (2026-11-18): scripts/ci/precommit-share.sh is judged the same way -- it only splits the hook's selection
  over the check's jobs -- and every such selection holds the union of what its shares (`checks rest|identity|oldrefs|plan`
  on a check/<id> ref) run for the change: a check one adds forces the full round (`they change
  scripts/ci/precommit-share.sh, and its shares for the change adds itrest`), else the verify round. A file BOTH sides
  change stays a full round, these too. MODULE OVERLAP (2026-11-18): no shared file, but an sbt module (`<dir>/src/`)
  both sides touch -> the verify round PLUS the area tests: the branch is `check/verifyareas-<full base>-<id>`, whose
  precommit-share.sh runs the identity share in `--checks-only` over `<full base>..HEAD` (the rest is the verify
  round's); a tree whose script does not name the mode gets the plain verify round. The verdict says `with the area
  tests (sbt modules both sides touch: worker)`. A push batch's union verification runs no area tests. The other
  forcing paths (the rest of scripts/hooks/, hooks-path.sh, tool-conventions.sh) stay a full round.
  MINIMAL RECHECKS (the owner, 2026-11-20: "so that rechecks are kept to a minimum"; fix-red-2717 had sat through a
  full re-check and a verify round after main moved by 12 commits): with no full round due, the verify round runs only
  when a check the change's paths select READS a path main changed since its last check -- the hook's own selection,
  read at the rebased tip: `--compile-plan` (where one side's code is compiled against the other's: a config of one
  side's plan standing for the other side's OWN code -- `<module>/src/test|it|page/` its config, `src/main/` its
  module's Test config, the build every config; never a module merely downstream of both, e2e over worker and web,
  which had made every two code changes "meet": 24 of 11-19..21's verify rounds),
  `--coupling-plan` / `--quality-plan` (production code on both sides: the whole-tree ratchets), `--plan`'s checks (an
  area such as `identity`, `scripttests`, `drift`, ...; not `sbt`, which the plans above spell out, nor the per-file
  checks `shellcheck`, `actionlint`, `eslint`) and `--lint-plan`'s path-selected specs (not the standing `*` rows) --
  or when the rebase changed the change's own diff (its patch id: a merged generated row). Otherwise its VERDICT IS
  KEPT: rebased and pushed at once, PREPUSH_SKIP the plan its checks passed (`checked ...; main moved by 2 commits since
  its checks on <sha>, no check of it reading what changed: its verdict kept`), and it is no push batch member
  (`verdict_kept`). A hook without `--plan` and `--compile-plan` gets the verify round. Accepted risk: a standing lint or
  a basic line reading both sides (Main catches it). Every decision is kept on the change (`rechecks`: full / verify /
  kept, the main it was taken on and why) and printed by `land status <id>` (`recheck: 09:41Z verify on 5e2f...: main
  moved by 3 commits: its compile-plan reads what main changed: web/Test/compile`). Measured (2026-11-20) over the
  3 days' 131 rechecks of landed changes: the commonest reads both sides shared were `scripttests` (33, the hook's one
  word for every script self-test) and `e2e/Test/compile` (30). THE LINT PLAN READS WHAT EACH SPEC OPENS (movies, 2026-11-21, landing-speed proposal 1): its registry's 175 `@repo`
  rows, selected by any script, workflow, skill or test-tree path, made every two changes share a lint; each row now
  names the files its spec opens (scripts/ci/lint_reads_trace.py runs each spec and reads the files' access times) and
  its own code. Replayed over 11-19..21's 46 resolvable verify rounds through `read_overlap`: kept 1 -> 3 (the lint
  plan's 19 -> 17 are specs genuinely reading both sides; the compile plan's 25 stand). The simulator models the
  registry as a table read off movies' hook (tests/fixtures/movies-lint-plan.json; `-before.json` the old one): over
  the replay's 3 days x 20 seeds, rounds per landing 1.78 -> 1.67 (p90 3 -> 3), kept-verdict landings 74 -> 346. PER-FILE
  LINTS (2026-11-21): a hook answering `--per-file-lints` names, of the lint specs some paths select, those judging each
  file alone (movies' `@per-file` rows, proved by scripts/ci/lint_per_file_check.py) that the paths select by what they
  read, never by their own code; one both sides name so is no overlap of the lint plan (`read_overlap`): the two meet in
  its verdict only through a file both change, a full round already, or its code, which one side then names it for.
  queuerecheck.py `PER_FILE_MODE`, `hook_text_modes`; tests: `tests/test_queue.py` `*per_file_lint*`; the simulator's table
  carries each kind's per-file specs (`per_file`). PER TEST (2026-11-20): a hook answering
  `--script-test-plan` (movies: the self-tests `scripts/run-script-tests --paths <file> --list` selects -- a tool's own
  test, its users' tests, the tests naming it or declaring they read it) has its `scripttests` word read per test. queuerecheck.py `read_overlap`, `own_diff_changed`, `verdict_kept`, `recheck_logged`; tests:
  `tests/test_queue.py` MinimalRecheckTest, `tests/test_queue_sim.py` MinimalRecheckSimTest (INVARIANT 11 now also
  accepts a tree holding a verified one whose extra patches no check of it reads, the simulation's own read model).
  Code: queuerecheck.py `recheck_reason` (THE RULE), `selection_change`, `share_selection`, `module_overlap`, buildview.py `build_view_change`, `_push_change`,
  `_check_entry`; tests: `tests/test_queue.py` (`*verif*`, `*deleted_file*`, `*rechecks`, `*full_round`,
  `*build_change*`, `*share*`, `*module*`, `*verifyareas*`), `tests/test_buildview.py`.
  THE OLDEST VERIFICATION PUSHES FIRST (2026-11-16: workermem verified four times, each on a main a younger change
  had pushed onto meanwhile): while a change's verification round is queued or checking (not in an infra backoff),
  every younger checked change holds its push (`its push waits for <id>'s combined-tree verification`; a red's fix
  excepted), and verification rounds take the next free check tree. A RED FIX'S verification holds every ordinary
  push, OLDER ones too (2026-11-19: ratchet-diff, queued before 08010c, pushed at 10:11Z under the fix's 09:34-10:18Z
  round; the fix verified again until 10:36Z, with 100213 and 102400 held behind it), and NO PUSH BATCH forms while a
  red fix is in flight (queued..pushing: batch #48 formed 13 s before 08010c pushed, and its passed verify round was
  thrown away). queuepush.py `older_verifying`, batch.py `push_candidates`. With two or more changes owing a round,
  THE PUSH BATCH (below) checks and pushes them together instead -- also while a head's own round runs (A LONE
  VERIFICATION HOLDS NO BATCH): the hold above is only for a change no batch takes. Pushing a disjoint checked change first is
  NOT done: any push moves main, and THE RULE then sends the verification round again (queue simulator, 50 seeds of
  an outside red's fix verifying while an older change is checked: the fix re-verified in 14 seeds before, 9 after;
  over 120 random seeds the mean wait fell 6.47 -> 6.24 ticks, a red fix's 6.79 -> 6.30; the older change itself
  waits ~1 tick longer).
  The push is bounded; on expiry it verifies before it retries. `LANDED <sha>`, and a `watch` item.
  It pushes from its own check tree when that tree's slot is free, else from the PUSH TREE `<clone>-wt-check-push`
  (its own lease, no checker takes it): a checker holds its slot through its remote run's whole wait, and pushes that
  waited for it sat `busy` 30 min past their deadline (2026-11-06).
- **THE BACKLOG BATCH (the owner, 2026-11-17: "batch whenever there's a backlog, not only on red").** At green, with
  no batch open, when 2 or more queued changes that no checker has taken may share a check (`$AGENTCTL_BACKLOG_BATCH_MIN`),
  the change tick gives them no check tree of their own (`its check waits for the next backlog batch`) and the batch
  tick makes them ONE batch item with depth `full`, created only once a check tree is free for it. EVERY CHANGE IS A
  MEMBER (the owner, 2026-11-20: "batching waiting changes ... structurally"): an IDENTITY change joins it like any other,
  the batch's one full check running the union of its members' areas, G5's drift check (below) telling which identity
  change moved takes when it holds more than one. An IDENTITY change is a path that can move takes (the
  hook's `--identity-change` names it -- movies: identity/src/main,
  the worker's identity adapters, the title rules, words and normalizer, identity-judgements.tsv; a hook without it: its
  `--plan` selecting the `identity` area -- or a G5 identity-model commit; since 2026-11-19 a common/ change whose
  identity area runs some identity specs is no identity change and batches; no plan to ask: alone too, kept on the
  entry per head; a commit saying `Pure-Move: verified` counts for neither, 2026-11-19: it moves no take, and movies' hook
  leaves the identity area to the commits without the claim -- batch.py `pure_move_claimed`). Never in one: an identity
  change it cannot tell (no plan to ask), a change landed with `land --no-batch`, a red fix, a combined-tree verification round, one in an
  infra backoff or holding its own unread remote run. At most 8 members (`$AGENTCTL_BACKLOG_BATCH_MAX`; 4 before 2026-11-20), queue order,
  Fixes-Red first; a member that does not apply is left out (`batch_alone`) and checked alone; fewer than 2 kept: no
  batch. None forms while a checked change's push is due; a change whose own verify round RUNS holds none (A LONE
  VERIFICATION HOLDS NO BATCH, 2026-11-23, below), and while one waits for a tree, a younger lone change never takes the
  LAST free tree (review 2026-11-18: lone changes otherwise took every freed tree first). The change tick PUSHES
  FIRST, THEN READS THE HOLDS (queue simulator, 2026-11-18): a checked change's push due holds every backlog batch back,
  and the holds were read before the tick's pushes, so the tree a finished check had freed went to the oldest lone
  check -- under load (every tree busy) no backlog batch ever formed (40 changes, 40 lone checks, waits up to 55
  ticks; now batches, at most 13-27). Its tip is checked ONCE with the FULL landing check -- `pre-push --range`, on ci-runner a `check/batch-<tip>`
  precommit-check round with the area tests and heavy rows the members select, NOT `--fast-only` (no member had a check
  of its own) -- in one check tree, the members `checking` meanwhile. Pass: one push carrying that run
  (`PREPUSH_REMOTE_RUN`), every member `checked` -> `pushing` -> LANDED with its own watch and its own landing record
  (`batch_id`, `batch` = the size). Fail: bisected by prefix like the red batch, but not checked whole again first (its
  confirmation still clears a flaky failure), so at most 3 extra rounds -- except an INFRA-SHAPED failure (an sbt
  server lost, a runner or network signature with no test line: batch.py `batch_infra_failure`), which checks the
  whole batch once more first (`BATCH_INFRA_RETRIES`, 1) and bisects only when that fails too; the culprit is rejected only when its failure
  is its own (the differential judgement; a base red, a time cap or an infra failure sends it to a check of its own),
  the passing prefix is pushed at once, the rest queued again for the next batch. While it is open, it goes first like
  the red batch (no solo push under it; a red fix excepted, which keeps its red-fix tree and lands alone: the batch's
  push then finds main moved and is assembled again over it, each member judged by THE RULE on the new main -- one
  re-verification, the price of never holding a red fix for a whole batch check); a change queued meanwhile joins the
  next one. A second identity change is checked alone beside it. The same machine as the red batch:
  batch.py `backlog_candidates`, `backlog_reconcile` (members follow their batch), the `depth` of `batch_check` /
  `bisect` (remote.py); tests: `tests/test_backlog_batch.py`.
- **A FIX OF ANY OPEN RED GOES FIRST (2026-11-19, 08010c: `Fixes-Red: 6`, Alert backtest #6, red 5254bffda9d2,
  outside the gate).** The queue's red-fix exemptions read only the GATE's reds, so at a green gate a fix of a red
  outside it was an ordinary change: checked at 08:32Z, it waited 22 min behind two combined-tree verifications, was
  left out of push batch #47 (`push_alone`), then sat behind that batch's whole ~110-min verify round (THE BATCH GOES
  FIRST) -- `checked`, past its deadline at level 0, with nothing said -- and fix-replay waited behind it on
  infra/bin/backtest-alerts. Now where the queue ORDERS changes (the push before an open batch or an older change's
  combined-tree verification, the red-fix tree, never a push or backlog batch member) a `Fixes-Red:` naming ANY red
  the store has open counts (the change's KIND, below: `red_fix`); where the gate decides (waiting for green, a known
  red) the gate's reds alone still do (`gate_fix`). An ordinary checked change still waits for a batch it is not in
  (its push would throw the batch's check away: THE BATCH GOES FIRST), but its push's hold is recorded like a queued
  change's (`held: its push waits for batch <id> (<state>), which it is not in`, `... for the next push batch`, `...
  for <id>'s combined-tree verification`, `its push waits for the landing lock or a push tree`, `its last push did not
  land ...`), `land status` prints it, and its `checked` deadline laps quietly while the hold is live (queuepush.py
  `_push_checked`, `lap_change_deadline`). Tests: `tests/test_backlog_batch.py` PushBatchTest
  `test_a_fix_of_an_open_red_outside_the_gate_*` (the 2026-11-19 queue replayed), `test_a_checked_change_*`.
- **ONE CLASSIFIER: A CHANGE'S KIND (the owner, 2026-11-19: "fix that structurally"; changekind.py).** 08010c sat
  because "what kind of change is this" was decided ad hoc in a dozen places that disagreed. Now ONE function,
  `classify_change`, reads a change (its entry, or its item's data) against the QUEUE FACTS (`queue_facts`: the gate's
  current reds and every red the store has open, with its kind) and returns its KIND: the reds its `Fixes-Red:` names
  that are open and each one's kind (`gate`, `outside`, `flake`, `stuck-run`); `red_fix` (any open red) and
  `gate_fix` (a gate red); `identity` (asked of a queued change); why it may not join a backlog batch
  (`backlog_batch`: `red-fix`, `no-batch`, `batch-alone: <why>`, `identity`, `identity-unknown`) or a push batch
  (`push_batch`: those, `push-alone: <why>`, `known-red`); `verify` (its combined-tree round runs now); and its
  PRIORITY class. The tick classifies every open change each pass and records the kind on its item
  (`change_kind`, written only when a fact changed it: a red opening or closing, the gate flipping, a verify round
  starting); the tick's order, the red-fix tree, the batches' membership (`backlog_batchable`, `push_batchable`), the
  overlap hold, the known-red wait, `waiting`'s batch order, the governor's class (`gov_class_of`), the watchdog's WR,
  `land status` (`[red fix of 6 (outside)]`) and the stats record (`red_fix`, `fix_kinds`, `priority`) all read it.
  The `Fixes-Red:` trailer is parsed in ONE place (branches.py `trailer_values`: commas, semicolons and whitespace;
  `fixes_red_in_message` for a whole message), a change's named reds read in one (`fixes_named`). THE PRIORITY ORDER
  (generated from changekind.py `PRIORITY`; `tests/test_queue_sim.py` fails when this table drifts):

<!-- change-kind priority table: generated from changekind.py PRIORITY; do not edit -->
| order | class | what puts a change in it |
|---|---|---|
| 1 | `red-fix` | its `Fixes-Red:` names an open red (the gate's or any other the store has open): checked in the red-fix tree, pushed alone, before any batch, verification or ordinary change; never a batch member, never held behind an overlap |
| 2 | `verify` | its combined-tree verification round runs now: its push goes before any younger change's (THE OLDEST VERIFICATION PUSHES FIRST) |
| 3 | `ordinary` | every other change: batched where it may be (a backlog batch, a push batch), else checked alone |
<!-- end of the change-kind priority table -->

  The disagreements it resolved (2026-11-19): the governor admitted only a GATE red's fix at once (`gov_class_of`) --
  now any open red's, and so ci-runner's red-fix check lane (`check/red-<...>`, `movies-check-red-fix`, its full
  check and its verification rounds alike) serves any open red's fix: before, 08010c, an outside red's fix, verified
  on `movies-check` and waited 32 min for a runner (09:34-10:07Z); movies' `scripts/ci/lane-policy.tsv` names the
  class `red-fix` to match; the backlog batch ordered ANY change carrying a `Fixes-Red:` first, even one naming a closed or
  unknown red -- now queue order (an open red's fix is in no batch); the overlap hold took an older red fix for a
  backlog-batch partner (`backlog_batchable` never asked), so a younger change sharing its file was checked beside
  it and rechecked in full once it landed -- now it waits for the fix; the watchdog's WR flagged only a gate red's fix
  (the CI snapshot's reds) -- now a fix of any open red unlanded 40 min, `waiting` still only for a gate red's; the
  status snapshot (`status`, the page) showed a queued change's hold but not a checked one's -- now both; a hold
  recorded in one state was still shown in the next (a `checked` change read `its executor is starting`) -- a state
  change now clears it; and the four trailer parsers split `Fixes-Red: 6 7` four ways -- now one.
- **A CHANGE PAST ITS DEADLINE WITH NO RECORDED HOLD IS agentctl's BUG (changekind.py `unheld_changes`).** Every wait
  records its hold on the change's item (an overlap, a batch, a push batch, a verification, busy trees, a retry, its
  executor starting, the push's lock); a queued or checked change past its state's deadline with none is filed ONCE as
  an agentctl issue (`[change-unheld <id>] ...`, `agentctl issue --list`; its item's `unheld_issue`) -- by the tick
  that laps its deadline (its lap line says `nothing holds it ... filed as agentctl issue <id>`, severity info: the
  owner is never paged for it) and by every watchdog pass (`unheld: ...`), not after W1's 2 h.
- **A HELD WAIT IS NO ESCALATION (2026-11-19: phase3-f2, held behind phase3-agreement on coupling-baseline.tsv, and
  phase3-enrichment2, held behind f2 on TestWiring.scala, each escalated to the owner every 15 min as `queued for 45
  min, waiting on a check slot`).** Each tick records WHY a queued change waits on its item (`hold`: `overlap` with its
  blocking change and files, `batch`, `push-batch`, `backlog-batch`, `trees` (every check tree busy), `older-batch`;
  `governor` and `retry` from its entry), and `land status` / `status` print it (`held: its check waits for <id>
  (<state>): both change <file>`). A lap of `queued`'s 15-min deadline is a QUIET re-deadline (severity info, no level
  up, no notification, and NO INBOX ENTRY: the lap's event names no audience, so the projector writes none -- on
  2026-11-19 movies-fix-replay's held laps reached the inbox every 15 min, 93 of the day's 442 entries) while that
  hold is live; it escalates only when the hold is stuck (queuebatch.py
  `change_hold_stuck`): nothing named holds it past `$AGENTCTL_UNHELD_GRACE` (10 min in its state: a change just
  checked or queued has no hold until the next tick pushes, starts or holds it -- 2026-11-23, py-restore-corpus read
  `nothing holds it, yet it has not pushed` by W1 in the 40 s before its push), its blocking change is gone, or it is older than
  `$AGENTCTL_CHANGE_HOLD_MAX` (6 h, the `waiting` state's own bound). The blocker's own machine escalates should IT
  stick. Test: `tests/test_backlog_batch.py` OverlapTest `test_overlap_held_changes_wait_quietly...`.
- **THE HOLD GRAPH (2026-11-23: 22 movies changes deadlocked for over an hour -- gitops-specs' push waited for
  landing-check's verification, landing-check's check for kick-convergence, kick's for gitops-specs).** A STACKED
  CHANGE WAITS FOR ITS PARENT: a change whose commits include every commit of another open change of another worktree
  waits for it (hold `stacked`), lands after it, checked once on top of it; its parent never waits for it, and its
  overlap with others is read from its own commits. A HOLD CYCLE IS BROKEN, NEVER WAITED OUT: each tick finds every
  cycle among the recorded holds, its oldest change (lowest seq) drops its wait for good (`cycle_ignore`) and `status`
  shows `HOLD CYCLE ...` on it. A HELD VERIFICATION HOLDS NOTHING: a verify round held behind another change holds no
  younger push and no backlog batch. holdgraph.py, queuebatch.py `break_hold_cycles`, `change_waits`; tests:
  tests/test_hold_graph.py. The watchdog tells one notification per episode (a subject: the change it names) per 6 h
  (`AGENTCTL_WATCHDOG_RETELL`), and the tick's rederive lap never clears a hold between two readings.
- **AN IDLE HOLDER'S HOLD EXPIRES (2026-11-26; 11-23..25: 631 `STUCK: held for over 6 h` laps of four changes, each
  queued 14-19 h behind an older change that was not being checked -- py-reuse-git behind py-under-bar, parked 43 h on
  `waiting` for Main #2877; py-page-tests-local behind py-gitops-specs, itself queued and held).** Two faults: the
  6-h cap (`$AGENTCTL_CHANGE_HOLD_MAX`) read `escalate` whatever the hold's kind, so the overlap kind's own recovery
  (`rederive`, THE HOLD TABLE) never ran; and nothing let a change go of a blocker that was not moving. Now the cap
  runs the kind's recovery, and an overlap hold on a holder NOT IN FLIGHT (not checking, checked or pushing) lasts at
  most `$AGENTCTL_HOLD_IDLE_TTL` (1 h): the change lets go of that blocker for good (`hold_expired` on its entry), is
  told once (`its hold on X (waiting, not being checked) expired after 1 h`) and goes on. queuehold.py
  `idle_hold_expired`; tests: tests/test_conflict_holds.py, the simulator's IT (`hold-on-parked-*`).
- **OVERLAPPING CHANGES ARE CHECKED AS ONE (2026-11-18: B1 against the CI-workflows change, the scraper catalog
  against the scrapers refactor, the cycle-breaking change against both -- each checked alone, then `NOT LANDED: the
  change no longer applies ... main moved during its checks`, a rebase and a full recheck).** A queued change sharing a
  FILE with a change queued before it and still in flight (queued, checking, checked, pushing, waiting; never its own
  older version): both queued, both batchable and assembling together (`git merge-tree` of their heads clean) -> no
  hold, the backlog batch checks them as one tree; otherwise its check WAITS for the older one (`its check waits for
  <id> (<state>): both change <file>`) and runs once on top of it when that lands (or as before when it is rejected).
  When the two cannot be assembled together, its author is told at its first tick: `CONFLICTS <id>: it cannot be
  assembled with <older> ... (both change <files>)` to the inbox, with what to do; a queued change that no longer
  applies on main itself (main moved since its base: `git merge-tree`, once per main and head) is NOT LANDED at its
  next tick, never after a wait for a check tree (`main_conflict`). A red fix never waits (it is told of a conflict all the same).
  A WAITING CHANGE HOLDS NOTHING IT ASSEMBLES WITH (2026-11-20, Main #2716: metrics-packages and phase7-houses sat
  `held: its check waits for movies-misc-cx (waiting)` -- held until green, behind a change that lands only at green):
  the younger is checked now and meets it in the batch at green; only a conflicting one waits (told `CONFLICTS`).
  `land cancel` clears every hold naming the cancelled change (`clear_holds_on`). queuebatch.py `overlap_hold`,
  `pair_conflict`, `hold_for_overlap`; tests: `tests/test_backlog_batch.py` OverlapTest, `tests/test_queue_sim.py`
  CollisionTest (the three collisions of 2026-11-17 replayed: no check spent on a change that could not land, no full
  recheck).
- **A BOUNCE NAMES WHAT CAUSED IT; A RESOLVED CONFLICT KEEPS ITS PLACE (2026-11-19, lane F2b).** Only a real conflict
  reaches the agent: `NOT LANDED: the change no longer applies on <remote>/main <sha> (...; it conflicts in F (changed
  on main by <sha> <subject>), ...)`, the files kept on the entry (`conflict_files`). A change queued again from the same
  worktree and branch within 24 h of such a rejection takes the rejected entry's `seq` -- its place in the age order --
  (`requeued_from` names it), never the back of the queue -- and its QUEUE AGE: `queued_since` carries the first
  entry's, and the change item's `submitted_at` (what `land status`, the stats and the escalations read) is it. A
  stale change main moved under is picked onto the new main in its check tree (a three-way pick per commit) and goes
  on; only a conflict that pick cannot resolve bounces. The id's number is the highest ever DRAWN (`drawn`), never
  the place taken: a kept place handed the next submission the number just used, and two submissions of one worktree
  in one second shared an id, the second answered with the first (queue simulator, replay 2026-11-17 seed 1006,
  introduced by ae6430d). **A ROW ONLY A REGEN DECIDES IS NO BOUNCE:** a change whose checks fail over its
  provisional rows (MAIN'S ROW, PROVISIONALLY, below) stays `queued` in its place, held (`regen_hold`, not checked
  again), and its agent is told ONE command, `REGEN NEEDED <id>: ... run \`cd <worktree> && ~/bin/agentctl land\``
  (the laptop's land rebases and runs the regen where the JVM is); the change queued again from that worktree takes
  the held entry's place and age. **AN OVERDUE REGEN HOLD RECOVERS, NEVER REJECTED** (the owner, 2026-11-20: "drop the
  reject"): unanswered for 24 h, the change tick runs the regeneration again -- a new `regen_round` on the change's
  item, which the laptop's sync answers by starting `agentctl land` in the worktree once more (`regen_answered`) --
  and when the hold still stands `$AGENTCTL_REGEN_RECOVERY_WAIT` (2 h) later (the laptop or its JVM unreachable, the
  regen failing) it escalates ONCE to the owner (`REGEN HELD <id>: ...`, what is held and why, whether the laptop
  answered); the change stays held in its place. Its hold kind's recovery is `recover`: never a stuck lap, nor W1.
  queueclose.py `regen_hold_overdue`, server.py `answer_regen_holds`. queueclose.py `conflict_cause`,
  `regen_hold`, server.py `conflict_bounced`, `queue_change`; tests: `tests/test_generated.py`.
- **GENERATED FILES ARE NO OVERLAP (2026-11-19 ~06:05Z: six movies changes -- agreement, f2, datalayer3, resolver2-b1,
  enrichment2, resolver2 -- each `held: its check waits for <prev>` because each edits coupling-baseline.tsv: every
  coupling or complexity lane landed one at a time).** A repo registers its generated files in red-repos.json
  (`generated`: `[{paths, regen, timeout}]`; movies: coupling-baseline.tsv and coupling-fanout-refs.tsv by
  `scripts/coupling-report --update`, the complexity, length and duplication baselines by `scripts/quality-report
  --update`, py-quality-baseline.tsv by `scripts/py-quality-report --update`; since 2026-11-21 the SNAPSHOTS, `merge:
  regen` (below); since 2026-11-23 the DERIVED FILES, `merge: derived` (below); since 2026-11-20 also the HAND-WRITTEN
  TABLES, registered with no `regen`: coupling-exemptions.tsv,
  coupling-layers.tsv, identity-judgements.tsv, identity-unmatched/expected-matches.tsv and
  scripts/tool-catalogue/migration-classes.tsv; since 2026-11-25 the measured identity-boot-cost-baseline.tsv --
  metrics-packages sat held
  behind phase6-data on coupling-exemptions.tsv -- merged row by row only, a row both sides changed staying the author's
  conflict, never one side's row kept provisionally: no ratchet would judge a lost exemption). Such a file is no overlap -- not
  for the hold (ONLY A VOIDED CHECK HOLDS, below), backlog-batch membership, the CONFLICTS a queued change is told of, `main_conflict`, nor the push's
  full-recheck rule; a real source both change (TestWiring.scala) holds as before. A conflict in one, on a rebase, a
  batch's assembly or land's own rebase, is RESOLVED in the commit being picked (its message and trailers kept): its
  rows merged three-way (keyed by every column but the value; disjoint rows always merge; a SET table, a key on many
  lines with no number for a value -- coupling-fanout-refs.tsv -- line by line), else -- one row both sides changed --
  the repo's `regen` command run in the tree, which is the combined tree at that commit
  (`$AGENTCTL_GENERATED_REGEN_TIMEOUT`, 1200 s; on the laptop only). **MAIN'S ROW, PROVISIONALLY (2026-11-19 06:58Z:
  lane F2b NOT LANDED, `no longer applies on origin/main 2cfa0ee95e`, because c3922ba26 re-measured the very rows its
  baseline commit changed and coupling-fanout-refs.tsv was unregistered: a rebase by hand and the back of the
  queue).** Where no regen command runs (ci-runner's reconciler, the server role: no JVM), a row both sides changed
  keeps the tree's side (main's, or an earlier batch member's), every other row merged, the files kept on the entry
  (`provisional`); the hook's ratchet judges the combined tree as it judges any baseline (a fall not yet recorded
  passes, a rise fails), and a change failing its checks so is held for its agent's regen (above), never bounced. The push's own rebase (under the
  landing lock) never runs a regen command: a generated-only conflict there is a new verify round. A failed resolution
  (no JVM where the queue runs, the command's exit, its timeout) leaves the member out of the batch (`batch_alone`
  `regen`, why in the log) and holds it behind the change it overlaps, as before; a member resolved in a batch that
  then fails is checked alone, never rejected for it. conflicts.py `conflict_resolve`, generated.py `rows_merge`,
  `regenerate`;
  tests: `tests/test_generated.py`, `tests/test_queue_sim.py` GeneratedOverlapTest (the six replayed, 4 check trees,
  seeds 0-19: mean 8.2 -> 5.3 ticks from queued to landed, max 18 -> 11).
- **THE BATCH TIERS (the owner, 2026-11-21: "make agentctl batch even more, merge / fix conflicts").** Two QUEUED
  changes that overlap are checked as ONE backlog batch whenever a tier assembles them, tried in order: (1) git merges
  them cleanly (`git merge-tree`); (2) the overlap is in registered generated files -- rows merged, else the regen
  command; a SNAPSHOT always regenerated (below); (3) a real source conflict whose every hunk is additions on both sides,
  resolved in the batch's assembly (below); (4) else, as before, the younger waits for the older and its author is told
  at its first tick: `CONFLICTS <id>: ...`, now with the conflicting hunks (`The hunks: <file> line N: <older> has
  \`...\`, this change \`...\``) and the fix (`Fix now: \`cd <worktree> && git rebase <older's branch>\`, resolve
  those hunks, \`~/bin/agentctl land\``). Batch size: `$AGENTCTL_BACKLOG_BATCH_MAX` stays 8 -- over the 7-day replay
  (3 seeds a day, 591 landings) a cap of 4, 8, 16 and 32 ran 1252, 1256, 1255 and 1255 checks, median 4 ticks each,
  p90 11, 10, 10, 10: batches seldom reach 8, so a bigger cap buys nothing measurable. queuebatch.py `overlap_of`,
  `pair_resolvable`, `conflict_hunks`; tests: `tests/test_batch_tiers.py`, `tests/test_queue_sim.py` BatchTiersSimTest.
- **SNAPSHOTS ARE REGENERATED, NEVER MERGED (2026-11-21 ~07:40Z: split-shared-js `held: its check waits for
  scrapers-2`, both changing movies' page snapshots expected-{index,warszawa-index,wroclaw-index}.html).** A snapshot
  -- rendered HTML, a JSON read model, a whole-corpus text -- has no rows: a line merge, git's or agentctl's, guesses
  what the combined code renders. Registered with `"merge": "regen"` (movies: expected-schedules.txt,
  read-model-snapshot.json and its inputs stamp, read-model-derivation-rows.jsonl and -hashes.tsv by
  `FilmScheduleEndToEndSpec`; the three expected-*.html by `PageSnapshotSpec`, after the read model: the regenerate-
  snapshots skill's commands, run through `scripts/sbt-warm`, 2400 s and 1800 s), it is generated (no overlap between
  queued changes), and whenever BOTH sides changed it -- a conflict, or a clean textual merge of two edits -- its regen
  command runs over the combined tree, on the laptop only (the server role never keeps a side of a snapshot: the member
  is held). **A REGENERATED SNAPSHOT HIDES NOTHING:** its output must be EXPLAINED by the two sides -- every line it adds
  added by one of them, every line it drops dropped by one of them, and no other snapshot of the command moved -- or the
  resolution fails, the member is left out (`batch_alone` `regen`) and held behind the older as before: a line only the
  combined code renders is an interaction neither author committed, and a regenerator never lands it. What passes is
  then part of exactly the tree the batch's full check runs: movies' `basic` tests assert expected-schedules.txt and the
  read model (FilmScheduleEndToEnd) as for each side's own regen; the page snapshots are asserted on Main (the page
  layer runs after landing), which is why the explained rule, not the check, guards them. A regen failing on the
  combined tree (exit, timeout, no file written) falls back to the hold, never a reject. generated.py
  `snapshot_unexplained`, `snapshots_both_changed`, `regenerate`; tests: `tests/test_batch_tiers.py`.
- **A DERIVED FILE IS THE COMBINED TREE'S (2026-11-23: three parallel shell-to-Python migration lanes each regenerating
  movies' docs/tools.md, every queued one conflicting with the others there; one lane refused four times for it
  alone).** A file the tree derives by a cheap, deterministic command is registered with `"merge": "derived"` (movies:
  docs/tools.md by `python3 scripts/tool_catalogue.py`; .github/convergence-paths.txt by `python3
  scripts/ci/convergence_paths.py --write`). Like a snapshot it is generated (no overlap between queued changes) and,
  whenever both sides changed it -- a conflict, or a clean line merge that leaves it stale (two tools added apart, one
  short in the section's count) -- both sides are dropped and its command runs over the combined tree, the result
  committed in the change being picked (a rebase, a batch's assembly, land's own rebase; on the laptop only, the server
  role holds the member). Unlike a snapshot its output needs NO EXPLAINING by the sides: its aggregate lines (a count,
  a "called by N tools") are by construction lines neither side wrote, and the repo's own check of the combined tree
  asserts it (`scripts/tool_catalogue_test.py`). A file only part of which is generated takes `"block": [<begin line
  prefix>, <end line prefix>]`: its hand-written lines are merged three-way with the block left out (a conflict there
  stays the author's), and the command rewrites the block over the merged file. generated.py `block_merge`,
  `explained`, `regen_base`; tests: `tests/test_derived.py` (two lanes each regenerating the catalogue land in one
  check, main's page the generator's output over the merged tree).
  **A CHANGE RE-APPLIED ONTO MAIN IS JUDGED BY ITS OWN COMMITS (2026-11-23: dispatch-py and title-trace-py, stacked on
  kick-convergence's branch, refused `no longer applies` the moment kick landed).** The queue-time check (queuebatch.py
  `main_conflict`) merged the change's head with main's: commits main carries under new shas (a stacked change's landed
  parent) read as an add/add source conflict. A conflict there is now judged again as the rebase picks it: the
  change's commits main lacks (patch-equivalent ones dropped, as `pick_commits` drops them), each merged onto the last
  (`git merge-tree`, no worktree); only a file the rebase leaves to its author -- source, neither a registered
  generated file nor prose -- refuses. A derived file's conflict is regenerated in the check's rebase. conflicts.py
  `own_commits_conflict`; tests: `tests/test_derived.py` (stacked on a landed parent: lands, the catalogue the
  generator's; a real source conflict still refused; every own commit landed: LANDED).
  **A DERIVED FILE HOLDS NOTHING (2026-11-23: the three migration lanes still sat `its check waits for <older>
  (checking): both change docs/tools.md`, one after another).** A derived file is no overlap even behind a check IN
  FLIGHT (only an entry declared `order_sensitive: true` does: ONLY A VOIDED CHECK HOLDS, below): the
  younger is checked now, beside the older, and its push, finding main moved under a snapshot or derived file both
  changed -- a conflict, or a clean line merge git made, which is no tree's output -- is a new round that regenerates it
  over the combined tree (`main moved under regenerated files it changes too (...)`), never git's merge pushed. On the
  server role too (2026-11-23, the 22-change deadlock on ci-runner): A DERIVED FILE IS REGENERATED WHERE THE COMBINE
  HAPPENS -- its cheap command (no JVM) runs on every role, agentctl's interpreter on PATH (generated.py `regen_runs`,
  `regen_env`). queuebatch.py `ordered_overlap`,
  generated.py `derived`, `regen_owed`, queuediff.py `generated_round`. **A hand-written file both sides only add to
  (scripts/hooks/lint-registry.tsv rows, a skill's bullets) still holds behind a check in flight**, by decision: two
  QUEUED changes adding to it already meet in one batch (tier 3; a `*.md` is union-merged), but with the older in
  flight there is no batch to meet in, the additive resolution runs only in a batch's assembly (whose full check
  verifies it, never a push's rebase), and a file both touch is THE RULE's full round (`both touch ...`; the lint
  registry is a selection path besides) -- a check of the younger now would be thrown away. Tests:
  `tests/test_derived.py` (the lanes adding catalogue tools and lint rows, queued together: one batch, both land;
  behind a check in flight: the catalogue holds nothing, the lint row holds).
- **A SOURCE CONFLICT IS RESOLVED IN THE BATCH WHEN BOTH SIDES ONLY ADD (tier 3).** In a BACKLOG batch's assembly only
  (its check is the full landing check of exactly the tree that lands -- never a lone change's rebase, a push's rebase,
  a push batch or a verify round), a source conflict is resolved hunk by hunk over `git merge-file --diff3`: past the
  lines both sides wrote alike (their common prefix and suffix, which must cover every base line of the hunk), what is
  left on each side must be pure addition -- kept, ours (the tree) first, then theirs; a side holding the other's lines
  is taken; two appends to a list whose last item each gave a comma get a comma between. Anything else (a line both
  changed differently, a deletion against an edit) is no resolution: tier 4. Between two queued changes,
  `pair_resolvable` tries it over the heads' merge base, so a pair it resolves is batched instead of held and told;
  with the older in flight it is a conflict as before (told, held). What was resolved is recorded: an
  `Auto-Resolved: <file>: line N: ...` trailer on the picked commit (so on main), `autoresolved` on the entry, the
  member's `checking in backlog batch` event to its author and `land status` (`AUTO-RESOLVED in the batch: ...`). The
  batch failing with it: the bisect names the resolution as the culprit (`the batch's AUTO-RESOLUTION of <file> is the
  culprit (...)`, to its author), the member is never rejected for it -- `batch_alone` `resolve`, held behind the
  change it conflicts with (strict: the textual conflict counts) and, once that lands, told it no longer applies.
  RepositoryReadSwallows.scala (worker-rest-2 behind review-common) is NOT a registered table: its rows are a Scala
  Map's multi-line entries, the last one without a comma, so no row is one line; two appended entries resolve here.
  waiting.py `hunks_resolve`, `source_resolve`, `conflict_resolve`; batch.py's bisect; tests: `tests/test_batch_tiers.py`.
- **ONLY A VOIDED CHECK HOLDS (2026-11-25, the owner: "fix structurally / for the future" -- py-scalafix `held: its
  check waits for py-isolated (checking): both change docs/tools.md`, py-reuse behind it for py-quality-baseline.tsv).**
  A queued change waits for an older one only where checking it now is a check thrown away: the two do not merge
  (`git merge-tree` of their heads conflicts in a file no batch tier resolves: told `CONFLICTS`); a SOURCE both change
  (THE RULE would recheck it in full once the older lands: simulator invariant I9); or, with the older already in
  flight, a generated file of a registry entry DECLARED `order_sensitive: true`. Whether a generated file holds is data
  on its one registry row (red-repos.json `generated`, which the hold, the batch's row merge and the regeneration all
  read), never a default: a new generated file needs no agentctl change. A derived file never holds -- not in flight,
  not for a change checked alone (`batch_alone`, strict: py-scalafix's hold) -- and `order_sensitive` is refused on a
  derived entry; `holds: false` is gone (a parse error names `order_sensitive`). Movies declares its baselines, its
  hand-written judgement and coupling tables and its snapshots order-sensitive -- measured, the queue simulator over
  the six baseline lanes of 2026-11-19 (20 seeds, 4 trees): held, 40 checks, mean 5.4 ticks to land, p90 7; unheld,
  108 checks, mean 5.8, p90 9 (the younger's check, made beside the older, is voided by its landing) -- and leaves
  migration-classes.tsv, docs/tools.md and convergence-paths.txt free. Replaces A GENERATED OVERLAP WAITS FOR A CHECK
  IN FLIGHT (2026-11-21). generated.py `order_sensitive`, `without_derived`; repos.py `generated_order`; queuebatch.py
  `ordered_overlap`, `held_files`, `conflicting`, `overlap_hold`; tests: `tests/test_conflict_holds.py` (lanes sharing only
  undeclared generated files with a check in flight: one batch, one check, one push; py-scalafix checked alone beside
  a check in flight; a real conflict still held, naming the source alone; a source edited apart still held),
  `tests/test_derived.py`, `tests/test_batch_tiers.py`, `tests/test_generated.py` (movies' declarations).
- **THE PUSH BATCH (the owner, 2026-11-17: "combine them instead of landing them one by one", W1).** A checked change
  pushed alone: each on a main that moved since its full check paid a verify round of its own, and THE OLDEST
  VERIFICATION PUSHES FIRST held the younger ones meanwhile, so N checked changes landed serially (ref-scrapers
  verifying while pr274 and postlanding, both checked, waited to verify again after it). Now, at green with no batch
  open, 2 or more changes that would each need a round on the current
  main (`$AGENTCTL_PUSH_BATCH_MIN`) -- checked on an older main, or queued for their verify round with no checker on
  them; never a red fix (it pushes alone, first, even while a push batch verifies), a `land --no-batch` change, one
  with a known red or in a backoff -- are held by the change tick (`its push waits for the next push batch`) and made
  ONE batch item with depth `verify`, the backlog batch's machine: assembled oldest first on the current main, at most
  4 (`$AGENTCTL_BACKLOG_BATCH_MAX`), each member judged by THE RULE (`recheck_reason`) against main plus the members
  before it. With one such change at least, the QUEUED changes a backlog batch would take join it too (they share
  its one check: queuepush.py `batch_queued`). ONE ROUND, THE DEEPEST A MEMBER OWES (2026-11-23, batch #109: five of
  seven members left out for a FULL round each -- main had changed what selects their checks -- were checked alone,
  then verified one at a time behind each other, `batch: 0` for hours): a member THE RULE sends to a full round, a
  queued member with no check of its own, or a conflict resolved in the assembly makes the batch's one round the FULL
  landing check (depth `full`, the backlog batch's); nobody is left out (batch.py `push_batch_full_round`). A LONE
  VERIFICATION HOLDS NO BATCH (2026-11-23: ~8 checked tools-only changes each `held: its push waits for X's
  combined-tree verification`, one head after another; no push batch formed while any change's own verify round
  ran): a change whose own round runs (its checker alive) is no member, and the others form the batch beside it; THE
  BATCH GOES FIRST, so the head's push waits for it and joins the next round. Else ONE verify round over the union -- `--verify-only <the oldest member's
  full base>..<tip>`, on ci-runner `check/verify-<base>-batch-<tip>` -- then ONE push (`PREPUSH_SKIP`: the plan the
  members' full checks passed; the hook judges the red gate), every member `checking` -> `checked` (`verified ... in
  push batch <id>`) -> `pushing` -> LANDED with its own watch and landing record (`batch_id`, `batch`; its round counts
  as a verify round). Fail: bisected by prefix as the backlog batch is, the culprit rejected only when its failure is
  its own, the passing prefix pushed, the rest queued again for their verify round (the next push batch). A DECIDED MEMBER IS
  NEVER PUSHED (queue simulator, 2026-11-18): a member superseded or cancelled while its batch checked left the batch's
  members but not its assembled tip, and landed with the others; now the batch is assembled again without it. More than
  one identity-model commit in it: G5's drift check of its tip, and after a drift split one identity change per batch
  (`drift_serialize`). A change checked while a push batch verifies is no member of it: its push waits for the batch
  (`its push waits for batch <id>`) and it joins the next one. batch.py `push_candidates`, `push_batch_full_round`,
  `union_base`; tests: `tests/test_backlog_batch.py` `PushBatchTest`.
- **THE BATCH IS THE MECHANISM (the owner, 2026-11-20: "is anyone working on batching waiting changes ... structurally,
  not just now").** Every change goes through a batch: at green the backlog batch (above: queued changes, identity ones
  included, up to 8) and the push batch; at red the WAITING batch, which no longer waits for green to check its
  combination. A production change that must wait is still checked on its own while red (in parallel, in the free check
  trees) and joins `waiting`; the blocked-red waiting batch then PRE-CHECKS every waiting change (at most 8 a round) as
  ONE tree on the current main (depth `red`: the hook's `--checks-only`, the union of their areas), again whenever its
  members change -- main moving while red is judged once, at green. A change that no longer applies is rejected alone
  (the conflict names its files; a generated file is merged, not a conflict); a failure is bisected by prefix, the
  culprit rejected when the failure is its own, and a failure only where main fails too (the red itself) rejects
  nobody. A red pre-check holds no push (what lands on red goes on). At green the batch is assembled on the new main and
  lands in ONE push: its members exactly the pre-checked ones and main's move since read by none of their checks
  (MINIMAL RECHECKS) -> no check again (`pre-checked as one on <sha>; main moved by 1 commit since, no check of it
  reading what changed: its verdict kept`); else the combined tree's fast check, as before. Measured over the simulated
  2026-11-18..20 (20 seeds a day, real round lengths; before = this morning's 5767210): check rounds per landed change
  2.30 -> 1.98, check time 14.3 -> 13.1 ticks, arrival -> LANDED mean 84 -> 53 ticks, p90 158 -> 100. Rejected by the
  same measure: production changes joining `waiting` UNCHECKED, the batch's one tree doing every check (p90 274: the
  red's idle trees went unused), and batches of any size (21-31 members at green, one failure bisected across them). A
  red fix keeps its own lane and lands alone first: 2026-11-17..20 a red fix landed in a median 14 min, a batch member
  in 56. batch.py `red_precheck`, `red_prechecked`, `green_batch_depth`, `waiting_batch_max`; tests:
  `tests/test_backlog_batch.py` RedPrecheckTest, `tests/test_waiting.py` (the in-process land's red batch).
- **checked → waiting** while main is red and the change must wait (a production path, no `Fixes-Red:` naming any red
  run of the current episode): `WAITING <sha> (entry <id>, n in batch)`. Tools-only changes and Fixes-Red changes land while red.
- **The batch.** (A backlog's batch is the same item with depth `full`: above.) When main turns green, the waiting
  changes are ONE `batch` item. The tick assembles its tip in a
  throwaway check tree (<remote>/main plus the changes in queue order, Fixes-Red first; a change that no longer applies
  is rejected alone), and an executor, `waiting check-batch <id>`, checks it ONCE (`pre-push --fast-only`) on a fresh sbt server. What
  that pass runs: the `sbt` check (every test config compiled, the lint specs, the `fast` set), the touched files'
  shellcheck and actionlint, and every check a touched `merge=union` path selects (bar the area tests and heavy rows:
  `judgements` for identity-judgements.tsv). Why that much and no more: each member was checked in full on its own,
  but on the main it was queued against, alone -- so the batch's compile and fast tests judge the combination, and
  a union-merged file is the one file the rebase WRITES from two members' edits without a conflict, which no member's
  check ever saw (2026-11-05: batch 957f4776f kept 81469b9c1's moved and 2a4ac45c4's re-added judgement rows, its pass
  ran `sbt` alone, and Tool self-tests #74-#76 went red on "a second row"; movies 508cb1791). What it never re-runs:
  the area tests (identity; page and e2e run after landing, on Main, since 2026-11-17) and a member's other path checks; a `known_red` member's own checks failed only where
  main failed, so main's CI after the push is the first full run of its failing tests. Pass: one
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
  created after the push whose head contains it; none within 60 min: said, not waited for) and **Identity takes**
  (picked like the convergence: convergence's preflight dispatches it, 2026-11-08). `WATCH OK: Main #n green,
  convergence #m green, takes #k green` closes the worktree you landed from when proven merged (unless
  `--keep-worktree`); `WATCH RED: <workflow> #n <failed jobs>` the moment any is red (fix forward with `Fixes-Red: <n>`); `WATCH UNKNOWN` after
  3 h undecided. A landed `Fixes-Red: N` is verified: `FIX OK` / `FIX FAILED` / `FIX UNVERIFIED`, each an inbox entry;
  a fix for a red outside the gate gets its workflow dispatched on main to verify it, with the failing run's own inputs (a required input left out is refused: Archive recording #823).

**Never an empty commit** (2026-10-27: overlapping entries landed four empty duplicates on main): re-queueing
replaces; a commit already on main is dropped at the rebuild; `prepush-check` refuses a range carrying an empty commit
unless it carries `Allow-Empty: <why>`.

`NOT LANDED: <reason>`: a dirty tree (`--stash` sets it aside in a named stash and restores it); a rebase conflict; a
prepush-check refusal; the pre-push checks failed (the last 25 lines; the full log in the entry's check log); a
conflict with the changes ahead of it in the batch; `stalled <step>` (no progress for 20 min); the red gate (followed
by the `ci-why` summary).

**In-process land** (`--follow`, `--no-watch`, `AGENTCTL_QUEUE=0`; a dispatched red fixer queues and waits, above): the same checks run in
land's own process, outside the lock (the hook's dry run `--range <remote>/main..<sha>`), then under the `land/<repo>`
lease it fetches, pushes when main has not moved since the check (moved: checked again outside the lock, twice; the
third time under it) and verifies; main red and the change must wait: it joins the queue as `waiting` and `--follow`
waits for its batch (a queued change that joined `waiting` gets that entry's final verdict under its OWN id, the one
`land` printed: `joined_verdicts`). `--follow` then watches in-process; otherwise LANDED starts a watch item and land exits. A sha
has ONE watch, its store item, whichever host makes it: `--follow` claims it (a lease it renews, which the tick leaves
alone) and writes its verdict there; a second follower of the same sha, on any host, prints that verdict instead of
watching again, and a lapsed claim is taken over (a follower renews only its own claim). The item names its repo
by its GitHub slug: each host's tick looks in its own clone of it (`watch_clone`: red-repos.json `clone`, the movies
clone), so ci-runner carries on a dead laptop follower's watch. A follower another local watcher's lock refuses gives
its claim back; an item that ended `unknown` is claimed and re-watched the same way, its verdict kept on it (`outcome`).

Other repos (no movies hook): `land` holds `land/<repo>`, fetches, rebases, runs prepush-check, pushes (the repo's
hook runs), verifies with `merge-base --is-ancestor` or `git cherry`, and prints `LANDED <sha>`; no watch.

Use it for every push to main. Don't use it for `measure/` branches: `identity-measure` pushes those itself.

#### the queue simulator: the queue's invariants under random load (the owner, 2026-11-18)

`tests/queue_sim.py` simulates the land queue, deterministically per seed, over the REAL queue, batch and push code
(tick_changes, tick_batch, check_entry, check_batch and bisect, push_change, push_batch_item, backlog_reconcile,
project_changes, close_landed_changes, queue_change, the store's items, leases and events). Only the world is faked:
git is a model of (parent, patch) commits answering the queue's git calls; the forge's main, its push (a race, the
hook's red gate) and the gate's red episodes; CI's verdicts (a breaking patch names its failing test; infra failures
drawn); the agents (identity, plain, red-fix and `--no-batch` arrivals, re-lands after NOT LANDED, supersedes,
cancels; pushes from outside). Executors run in cooperative threads that block inside the faked check holding their
real leases, one thread at a time; a signal to a checker ends its thread as it would end the process. After every
tick: one open item per change (worktree + head); never two pushes of one change; a red fix never behind a batch or
a non-red change (started and pushed when it may go, in the red-fix tree, no batch member); a batch reaches landed,
bisecting or held within 40 ticks; the oldest change lands within a bound when nothing fails (the calm and load
profiles); no change lost (entries and items agree, a LANDED change is on main, every change decided once the gate
is green and nothing arrives); exactly one stats `agentctl.landing` record per decided change; no executor for a
decided change; a change that no longer applies because of a change open when it was queued was told at queue time
(CONFLICTS, or NOT LANDED at its first tick) and never checked meanwhile; no full recheck for a file overlap with a
change open when it was queued; never a superseded, cancelled or rejected change's commit pushed; no change held behind
another for generated files alone; (a) a fix of ANY open red -- the gate's or one outside it, by the simulation's own
truth, never the classifier's -- never waits behind a change that fixes none; (b) every queued or checked change past
its deadline has a recorded hold; (c) no change is silent: each one past its deadline says why on its `land status`
line (`land_status_line`); (d) executors start in THE PRIORITY ORDER within a tick. Reds outside the gate are modelled
(store items, `gate: false`; their fixes arrive as `outside-fix` changes; a stream of draws of their own, so a seed
without one replays as before). The 08010c timeline replays as `TIMELINE_08010C` (Timeline08010cTest: the fix lands
within 8 ticks, never held by a batch, a push batch or a verification; fix-replay waits for it). `tests/test_queue_sim.py`: 200 random seeds in 8 parallel tests (`$AGENTCTL_TEST_SIM_SEEDS` for
more), the calm and load profiles, one replay per day of 2026-11-11..18 from victoria-logs-ci's `agentctl.landing`
records (`tests/fixtures/landing-replay-7d.json`), the three collisions of 2026-11-17, a chaos profile (reds,
breaking changes, conflicts, infra failures and outside pushes far more often) with its fixed seeds, and mutation tests: ee7b744,
PUSHES FIRST, OVERLAPPING CHANGES ARE CHECKED AS ONE or GENERATED FILES ARE NO OVERLAP undone in a copy of
agentctl must fail an invariant (6d206ed undone fails "one open item per change" in 13 of 40 seeds), and so must the
classifier counting the gate's reds alone (the 08010c timeline), the tick's order without PRIORITY, and a checked
change's batch hold left unrecorded. A failing seed
replays alone: `queue_sim.Sim(<seed>).run().violations`; `$AGENTCTL_TEST_SIM_KEEP=<dir>` keeps its processor log.
(11) NO UNVERIFIED COMBINED TREE REACHES MAIN (2026-11-19): every tree the queue pushes is one a check or a batch
check passed (its mutation: a moved main pushed without its verify round). A seed REPLAYS EXACTLY: entry ids and stamps
read the simulated clock (`sim_datetime`; ids carrying the wall clock reordered a seed from one run to the next), with
`PYTHONHASHSEED=0` for a comparison. A profile may set the round lengths in ticks (`check_ticks`, `verify_ticks`,
`batch_ticks`, default 2-4 / 1-2 / 2-5) and `lease_ttl` (s) for longer rounds.

**THE SIMULATOR GATE (2026-11-20, the owner's "fix agentctl issues structurally").** `tests/sim_gate.py` runs 308
fixed scenarios (the suite's 200 random seeds, the calm / load / chaos profiles with a 30-seed x 2-tree chaos sweep,
every replayed day, the collisions, the baselines, the 08010c timeline) in COUNT MODE (`Sim.run(stop=False)`), and
counts, per INVARIANT KIND (`queue_sim.INVARIANT_KINDS`: I1-I11, Ib, Ic, Id, IX, IW; every `violate()` names one), the
scenarios that break it. `tests/sim-invariants.tsv` holds each kind's VERSION, the FINGERPRINT of its definition (the
source of every Sim method raising it, the run loop and the scenario list) and its BASELINE count. It refuses: a count
past its baseline; a definition changed without a new version; a baseline risen at the same version against the push's
base; and a commit changing an invariant row beside `lib/agentctl/` (an invariant changes in a commit of its own, its
old-definition numbers reported beside the new). A fall is a note (`--update` records it). It runs in ~/bin's pre-push
hook after the suite (`--base <the remote's sha>`) and as tests.yml's `sim-gate` job; `--update [--bump K]` rewrites
the registry, `--json F` keeps every scenario's result. Its baseline at its introduction (master 2ab61bf): one scenario
(chaos-19-t1) breaks I3, a red fix made a member of a backlog batch; every other kind 0; mean landing latency 7.75 ticks.
That I3 was real: a fix of a red OUTSIDE the gate waits on `waiting` while the gate is red, and at green the waiting
batch took it with the rest. A RED FIX IS NEVER A BATCH MEMBER, AT GREEN TOO (batch.py `release_red_fixes`): at green,
before a waiting batch forms, each waiting change that fixes an open red goes back to the queue, where it goes first,
alone. I3: 1 -> 0 of the gate's scenarios (4 -> 0 of a 700-scenario sweep beyond them); latency 7.72 -> 7.89 ticks.

**Landing latency, measured (the owner's R3, 2026-11-19; nothing below shipped).** `agent-time`: waiting on `land` was
39% of all lane time; a landed change took ~84 min after 09:00Z (re-check rounds ~28, held checked->push ~21, queued
~13, runner wait ~15). The store's checked spells that day: 386 min held, nearly all `its push waits for <id>'s
combined-tree verification` or `for batch <id>, which it is not in`, then a verify round of its own once that landed.
Replayed in the simulator as that day's 90 arrivals (`agentctl.landing` records, 4 trees; mean / p90 ticks
arrival -> LANDED; `today`: the default round lengths, 40 seeds; `today-real`: rounds of 4-10 / 3-7 / 5-11 ticks, a
tick 5 real min, 20 seeds). Baseline: today 8.24 / 17, today-real 64.6 / 128, 722 verify rounds.
1. SKIP THE VERIFY ROUND WHEN MAIN MOVED DISJOINTLY (the hook's `--plan`s of the two sides share no check): rejected,
   it never applies -- 150 of the day's 151 commits on main select `sbt` (every script, workflow and .claude change
   does: its lint specs read them), and no finer read set (per spec, per compile) is declared, so nothing narrower is
   provably equivalent. docs-only moves select nothing but are rare.
2. SPECULATIVE STACKING (merge-queue style): rejected, it costs more than it saves. A held checked change verified on
   top of the older verification's tip: today-real 89.5 / 148, 2516 verify rounds (stacked rounds mostly invalidated
   before their base landed). THE TRAIN (every verify round on the longest chain's tip, pushes in train order): today
   8.20 / 17, today-real 68.9 / 129, 1747 verify rounds; train order alone 64.9 / 135. Each displaces the push batch
   (175 -> 57-101 batch verifications), which already verifies up to 4 changes in one round.
3. PUSH HOLDS: holding a younger push only behind a verification RUNNING (not one still queued for a tree): today
   8.24 -> 7.80 / 17, but today-real 64.6 -> 68.2, 2042 verify rounds -- each extra push moves main under the
   checked changes. Rejected; the holds stand.
4. THE RUNNER WAIT is ci-runner capacity, not lane order: check jobs queued 17.4 runner-h that day; in the long waits
   (20-32 min) 1-3 of the 10 movies runners were busy and the scheduler logged `STARVED check: ... held by cpu` (the
   host's CPU, shared with bitcashier builders and main-branch rows, which queued 323 runner-h). lane-policy.tsv already
   ranks checks first.

#### land --until-done: the agent's whole tail (the owner, 2026-11-18)

Agents kept ending badly: stopping after the rebase without landing, handing back partial reports or the same report
twice, leaving worktrees behind, and `git branch -D` by hand after a landing rebased their commits (never merged by
ancestry). `land --until-done` (movies, the queue) owns the tail, bounded by `--deadline` (default 4 h,
`$AGENTCTL_UNTIL_DONE_DEADLINE`; `90s`, `30m`, `3h`):

1. Its own entry first: a change item (or this host's queue entry) submitted from THIS worktree at THIS head. Open:
   `RESUMING <id>` and it waits for it -- called again after an interruption it never queues the same change twice.
   Landed: straight to step 4. REJECTED (decided while no call waited: A VERDICT IS DURABLE, 2026-11-19 -- subagents'
   waits died with a Claude session restart and they handed back "queued, waiting"): `DECIDED <id> ...`, its `NOT
   LANDED: <why>` and `next:` as in step 3, exit 1 -- never queued again unchanged (`agentctl land` does that, on
   purpose). That replay is only for a verdict that STILL HOLDS (2026-11-21, movies-devpanel-oauth: a rejection its
   own waiter had already printed at 17:02 was replayed at 17:35, "while no --until-done waited", though main had
   moved and fixed the main-side bug behind it): no waiter took delivery of it (a call that printed it records
   `delivered_at` on the item) and `origin/main` is still the main it was decided on (`decided_on`: the check's base,
   else the one it was queued on). Delivered already, or main moved: the change is queued, so checked, again.
   None (or only a cancelled or superseded one): when every local commit is already on main by patch,
   straight to step 4; else the plain land's rebase, squash guard and `QUEUED <id>`. After the fact, `agentctl land
   status <entry>` (an id or a unique part of one) prints that one change, open or decided: its state, worktree and
   head, then `LANDED <sha>` / `NOT LANDED: <why>` / `OPEN: ... held: ...`; and its exit event's inbox entry names
   the worktree (`cwd`) and the change (`item_id`) whether or not anything waited.
2. The fast precheck runs in the FOREGROUND (as a red fixer's): a refusal is its NOT LANDED at once.
3. It waits for the verdict (the store item, polled with backoff). `NOT LANDED: <why>` is printed with the exact
   reason and then `next: <command>` -- a conflict: `git -C <wt> fetch origin main && git -C <wt> rebase origin/main`;
   failed checks or a precheck refusal: fix, commit; superseded: it resumes the newer entry -- each ending
   `~/bin/agentctl land --until-done`; exit 1. The deadline: `NOT DONE: <id> is <state> ...` and the command to
   resume, exit 2.
4. `LANDED <sha>`, then the proof and the cleanup: every local commit's patch on origin/main (`git cherry` plus content
   twins, cherry_landed), the worktree not the root checkout, not another session's change (its item's `session`),
   no other process working in it, clean (untracked files included). Then the worktree is removed and its branch
   deleted -- a force delete, but only after that patch check (cleanup.py `close_worktree`) -- and the last line is
   `DONE <sha> (queued <id>): every local commit's patch is on origin/main; worktree removed (...)`, exit 0.
   `--keep-worktree`: `DONE ... worktree kept`. Anything unproven: `LANDED <sha> ...; NOT DONE: the worktree <wt> is
   kept: <why>`, exit 3 -- a dirty or foreign worktree is never removed. Before the removal, processes still working
   in the worktree are freed (2026-11-18: LANDED but no DONE, the agent's own kept sbt server held it): the
   checkout's `scripts/sbt-warm --stop`, then a SIGTERM, by pid, to each process in it whose environment carries the
   caller's `CLAUDE_CODE_SESSION_ID` (never an agent session or terminal, never another session's or a session-less
   one), and a wait of up to 30 s (`$AGENTCTL_FREE_WAIT`). Still held: `LANDED <sha> ...; NOT DONE: ... still held`,
   one `pid <pid>: <command>` line per holder and `next:` the command to finish, exit 4 (cleanup.py `free_worktree`).

5. Its output is its own (2026-11-18): two subagents of one session each ran `land --until-done > $SCRATCH/land.log`
   -- one scratchpad, one file -- and one read the other's `NOT LANDED: the fast precheck refused <their id>` there
   as its verdict (its own entry was still checking, and landed; land itself followed only its own entry). A land
   that waits for its verdict (`--until-done`, `--follow`) holds an exclusive flock on its output file while it runs;
   a second one writing the same file does not start: `agentctl: land did not start: another running land writes
   its output file <path> ...`, exit 5 (slots.py `output_file_shared`). Every verdict line names its entry id: one
   naming another id is not yours.

The brief (`~/bin/briefs/movies-agent-preamble.md`) makes it every movies agent's last step: run it with
`run_in_background=true` and NO redirect (the task's own output file is per task), end the turn, report once after
its last line. Code: queueuntil.py `land_until_done`,
`own_change_item`, `until_done_finish`, `next_command`; batch.py `land_status_entry`; tests: `tests/test_until_done.py`.

### remote checks: ci-runner's check tier

Whenever the checkout's hook supports it (owner decision 1), a queued change's checks run on ci-runner's check tier:
the executor pushes the checked tip to `check/<entry id>` (movies' hook checks nothing for a check/ branch), whose push
runs `.github/workflows/precommit-check.yml` on the check tier: the same hook, `--checks-only <merge-base>..HEAD`. The
run is the ENTRY's, not the executor's: its record is saved the moment the branch is pushed, a replacement executor
ADOPTS it (no new push, no new run) on the base it checked even when main moved since -- the push then judges what landed, as for a checker that survived (2026-11-15: a reconciler restart's replacements cancelled a half-done run and threw a green one away for a moved main) -- and the 15-min (no run started) / 2-h (undecided) bounds count from the push;
past them it is checked locally. The 2-h bound judges PROGRESS (2026-11-17, ref-srp-54ebae: every check had passed
and `check (page)` was 14 min into its run, after 23 min waiting for a saturated check runner, when the 2 h from the
push cancelled it): each poll marks `progress_at` when the run's jobs move, a run past 2 h is cancelled only once idle
30 min (`REMOTE_IDLE`), and any run past 4 h (`REMOTE_TIMEOUT_MAX`). The run is polled 15 s after the push, x1.5,
at most every 30 s (`REMOTE_POLL_CAP`; 120 s before 2026-11-21, when a round's verdict came a mean 1.4 min after its
last job ended): an unchanged run reads as a 304, free of the App's quota. A RUNNING JOB INSIDE ITS OWN EXPECTED DURATION
IS PROGRESS (2026-11-18: a healthy run whose only remaining job ran past 30 min, the others done, read as idle): its
`timeout-minutes` at the checked commit, else 1.5 x its recorded p90 (the collector's `source:ci` job rows in
victoria-logs-ci, 30 days, >= 5 runs), else 2 h (`REMOTE_JOB_CAP`), read once per run (`job_bounds`). A failed push of the check branch is tried again 5 s and 20 s later
(2026-11-17: two rounds ended "its checks stalled" 13 s in, no branch created on GitHub, and lost ~20 min each to the
infra backoff and the check-slot queue); a round with no verdict is re-queued as `its checks stalled (<where>: <what
the remote check said>)`, so the landing round's `reason` names the cause (remote.py `check_branch_push`). Such a
stall KEEPS THE CHANGE'S PLACE (2026-11-17, ref-srp-54ebae 2 h old: the 5-min infra backoff let younger changes take
every check tree): the first `STALL_KEEP_PLACE` (2) stalls in a row are due at once, so the tree it freed goes to it,
first by age; then the infra backoff (queue.py `STALLED_REASON`).
**The verdict is the check jobs'** (2026-11-07: batch #20 sat 27+ min past its last
check job behind a queued `timings / report`): the workflow declares its verdict's jobs as a workflow-level
`VERDICT_JOBS: <glob>, ...` of job names, read at the checked commit; once every job it names has concluded the check
is decided, and a job outside it (a report, a publish) still queued or running holds nothing. A job GitHub has not
created yet is not listed (2026-11-08: the then `check (page)`, needing every other check, appeared a second after the last of them), so a
success also waits until every job of the workflow's `jobs:` block that a glob names is listed (a `${{ }}` in its
name matches any row); a `jobs:` block it cannot read leaves the run's conclusion to decide. Any job concluding in
failure fails it at once. No declaration: the run's own conclusion decides. The push to main then carries
`PREPUSH_REMOTE_RUN=<run id>`: the hook takes that run only once GitHub says it is precommit-check.yml on exactly the
pushed commit, and a success -- or, still running, every job its `VERDICT_JOBS` names a success and none failed.
Superseding or cancelling an entry cancels its undecided run.
A GHOST RUN IS GIVEN UP (2026-11-20, phase7-houses: run 44682642375 queued 90 min, no check job started, while later
runs ran): a queued run with no check job started that GitHub's `actions/runs?status=queued|in_progress` listing omits --
the listing ci-runner's runner scheduler reads, so no runner is ever started for it -- is cancelled after
`AGENTCTL_REMOTE_GHOST` (10 min) of that and re-checked, keeping its place (remote.py `remote_ghost`). `land status
<id>` of a checking change prints its remote check (run, branch, pushed and last-progress ages, each job: its verdict,
`running <age>` or `waiting for a runner slot`), mirrored on its item as `check` (`remote_mirror`).

### early reds: a failed job of a run still in progress (2026-11-22)

The owner: "teach agentctl to react to parts of workflow failing and notifying / spinning the agents right away".
Main takes ~20 min and the identity runs longer; a job can fail in minute 3 while the rest runs on. `redearly.py`.

- **Detection (the red tick, every gate publish: ~45 s, at once after a push).** For each run on main the snapshot
  shows `in_progress` -- the gate's workflows first, then the outside ones, at most `$AGENTCTL_EARLY_MAX_RUNS` (6) --
  ONE `GET repos/<o_r>/actions/runs/<id>/jobs?filter=latest&per_page=100` (a page more per 100 jobs, at most 3)
  through `api()`'s conditional GET: an unchanged list is a 304 and costs no quota. A job `completed` with `failure` /
  `timed_out` is an early failure, keyed like any red (workflow + its first relevant line). Only when the set of failed
  job ids (or the attempt) changed is the run explained -- `explain_run_jobs`, the one classifier ci-why uses (a `gh run
  view` and the failed job's log): ~2-3 calls per newly failed job, never again for it. Each run's answer is kept
  under `reds/early/<run id>.json`.
- **Rate limits.** A 403 / 429 / 5xx (or `rate limit` in gh's words) pauses every early read 60 s, doubling to 15 min
  (`reds/early/backoff.json`); a good read ends it. A paused or failed read answers what was last read: no early red
  is ever withdrawn by a read that did not happen.
- **Classification first.** Only a TEST failure (a test or compile failure in the commit's code) is an early red.
  An infra-shaped or unclear one (classify_job's infra / unclear: a setup step, a lost runner, no error line) opens nothing: the
  completed run decides it, through the normal infra path. A `verdict complete` report is never one.
- **Acted on at once.** The early red is an ordinary red item with `early` in its data (`{run, number, attempt,
  seen_at, brief}`): opened, offered (its live author first: `RED AUTHOR` to the lander's session) and dispatched in
  the same tick; the owner's inbox gets `RED EARLY: <workflow> #N [jobs] failed while run #N is still in progress`.
  The fixer's brief says the run goes on, other jobs may still fail, and nothing is blamed or reverted before it ends,
  with ci-why's lines of the failed job. A family owner gets it as its next turn, as any red.
- **One red per failure, one fixer per run.** A later job failing the same way attaches to the same red (its holder
  told in `RED-INBOX.md`); one failing another way is a red of its own, joined to the same fixer by its queue family
  (configured, else the run). When the run COMPLETES failing, its verdict observes the same key: the red is SETTLED in
  place (`early` cleared, `early_settled` kept) -- one red, never a second -- and its culprit search (`blame_open`)
  opens only then. An early red the completed read words otherwise also settles in place (no culprit search).
- **Withdrawn cleanly.** An early red its tick no longer sees is WITHDRAWN when its run ended `cancelled` / green
  (superseded by a newer push, a concurrency cancel), a rerun attempt is clearing the job, or the run is gone from
  main's lists and a newer one decided: `RED CLEARED: ... EARLY RED WITHDRAWN: <why>; stand down -- no blame and no
  revert`, its lease revoked and `RED-STOP.md` in its fixer's worktree.
- **Never a blame or a revert on an early red.** `red_culprit_search` gives an early red no culprit and no blame,
  `decided_revert` skips it, so no tier `blame` / `lander` / `revert` can come up before the completed run's verdict
  (2026-11-22: a wrong blame-revert had followed an infra red; an early red adds no such path).
- **The push gate is unchanged: it reads COMPLETED runs.** An early red holds no push and needs no `Fixes-Red:` of
  anyone else: it can still be withdrawn (a rerun, a cancel), the movies pre-push hook judges the gate by completed
  runs, and a gate the hook and agentctl read differently would refuse pushes agentctl lets through. A fix landed for
  it (`Fixes-Red: <the running run>`) is a red-fix change (first in line) and is verified like any fix.
- **Polling, not a callback (evaluated 2026-11-22).** The tick runs on ci-runner, which has no public endpoint.
  `gh webhook forward` (the cli/gh-webhook extension) is a development tool: it creates a temporary hook that lives
  only while the forwarder runs, and an event sent while it is disconnected is not redelivered to it. A GitHub App webhook (`agentctl github-app`'s
  apps) or a repo webhook needs a public receiver with signature verification -- a relay on our edge (the Flux
  Receiver at flux.kinowo.net is the precedent) that ci-runner pulls through the tunnel. The jobs poll already reacts
  within one tick (~45 s) at ~1 conditional GET per running run per tick; a relay would cut that to seconds and would
  only TRIGGER the same tick (as `mark_gate_dirty` does after a push), never replace it: the poll stays the
  reconciliation for missed events and restarts. Adding it is the owner's call (a deploy and a webhook setting).

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

- **A push credential failure is ONE condition, at once (2026-11-22).** ci-runner's check-branch pushes authenticate
  through agentctl's own git credential helper (the GitHub App, ghapp.py), which runs the agentctl ENTRY (selfcode
  `TOOL_PATH`): after ghapp became a real module the helper named ghapp.py itself, died on its import, and every push
  said `fatal: could not read User` -- four changes re-queued "an infra failure" for hours. A push GitHub refuses for
  its credential (classify.py `CREDENTIAL_SIGNATURES`) is never retried as a blip; the re-queue opens ONE `PUSH
  CREDENTIAL` condition per clone naming the changes it holds and the helper's own error, cleared by the next
  check-branch push that succeeds (save_remote's `pushed_at`), or when no
  change waits on it (probe `push_credential`). The probe reads the queue's own records only, never a credential
  helper (the GitHub App's mints a token per call), and only on the host whose push failed (elsewhere it cannot tell).
  A token GitHub refuses opens the same condition. What it says is redacted (classify.py `redact_credentials`). A held
  change's status names its retry's reason.
- **An unread gate is RED** (the owner, 2026-10-27). When the processor cannot read the gate (GitHub, gh's auth or
  the token down) a production change waits and no batch is taken; one `UNKNOWN: ... cannot read the gate (…)` per
  spell, and `land status` prints `gate: UNREAD, taken as red (…)`.
- **The token hand-off.** A launchd service gets no answer from the vault, so the processor listens on a unix socket
  (`/tmp/agentctl-<uid>/<hash>.sock`, 0600 in a 0700 dir; `$AGENTCTL_SOCK_DIR`). Every `land`, and `land status` /
  `resume` run where the vault answers, hand it the store token, kept in its memory and environment only (a re-exec
  onto new code keeps it: execve with the same environment, `agentctl changed on disk (...): restarting on the new
  code`; executors and fixers inherit it). So does any other command that reads the token from the vault (an agent's
  ci-why or inbox in its Claude Code session, a TTY): a processor holding none is handed it, so the next agent command
  re-seeds a restarted processor. A watchdog kick hands it over itself (see "The watchdog"). Only a restart nobody
  could ask first loses it -- a reboot, launchd restarting a crashed processor, `tick service install`: then the next
  command that reads the vault, or by hand `AGENTCTL_INTERACTIVE=1 ~/bin/agentctl land status` from
  `~/projects/movies` with the vault unlocked (the service log then says `the store token was handed over`). Never on
  disk, and never the macOS Keychain: an item a launchd job reads without a prompt is readable by every process of
  the uid while the login keychain is unlocked, past the vault's own lock and Touch ID -- the token at rest in all but
  name. A command first ASKS the processor for the token it holds and hands it over only
  when it holds none or another one, so `handed over` is logged once per real change. A process without the token
  says so once, on the transition (`the movies token is missing (…)`, with its inbox note), then at most a
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

### gov: THE governor -- one admission control for laptop memory and CPU (cores charged per job)

```
agentctl gov status                 # admitted work, waiters, memory, evictable idle sbt servers, pressure
agentctl gov stats [--days 7]       # checks stats, plus each class's admission waits and the evictions
agentctl gov evict [--dry-run]      # stop the idle kept sbt server unused longest, by hand
agentctl gov admit --class C ...    # scripts/jvm-gate.sh's call (--fd 7 --ticket <file> --job J); --try: one look
agentctl gov route --job J          # ci (exit 0) when the governor would hold J now and ci-runner's measure lane is free
agentctl gov usage --job J --wall S --cpu S   # one run's cores, for J's measured history (sbt-warm's call)
agentctl gov cores --job J          # the cores J is charged (measured p75, else its default)
agentctl gov probe                  # one line: the governor's features (admit cores route width), for other repos' callers
agentctl gov replay --at 2026-11-18T05:45Z [--hold 30] [--cores 18]   # the admissions again under the cores rule
agentctl suite [run.py args]       # agentctl's own tests (tests/run.py) under a `suite` ticket: two at once when the cores allow (SUITE_CAP)
```

- **The busy cores are the whole machine's CPU** (2026-11-22): `machine_busy_cores` reads the CPU tick delta of the
  machine itself (user + sys + nice; host_statistics on macOS, /proc/stat on Linux), never ps's %CPU, which is a
  decaying per-process average blind to kernel time and to fork/exec churn (that day: top 0.24% idle, 56% sys, load
  131-150 on 18 cores, while ps summed to 6.1 cores and pressure read none). The capacity is starved at 95% of the
  cores measured (GOV_SATURATED); `laptop_pressure` says `CPU saturated: B of N cores busy (user + sys), run queue R`
  when that coincides with a run queue over LAPTOP_LOAD x the cores. A long run queue alone, cores idle, is no pressure.
- **THE GOVERNED FAN-OUT** (2026-11-25, the owner: "fix structurally" -- load 103 on 18 cores while `gov status` said
  0 admitted: agentctl's pre-push suite and simulator gate, movies' mutation tests, 43 test processes, none holding a
  ticket; movies' jvm-gate.sh had grepped gov.py for `def cmd_gov`, which the module split moved, so movies took no
  ticket for three days). Every parallel runner takes its width from ONE place (`lib/agentctl/govpool.py` governed();
  movies' `scripts/jvm-gate.sh` gov_width, with `scripts/gov-parallel` for `xargs -P` and `scripts/lib/tool_pool.py`
  for a Python pool): not under a ticket, it takes one charged its workers (`gov admit`); under one already
  (`$AGENTCTL_GOV_WIDTH`, exported by a check's ticket -- its charged cores -- and by every governed pool to its
  workers, `1`) it takes NONE, so a nested runner never waits on its parent, and runs at most that wide. Past the
  governor's bounded wait (900 s) or with it failing, a pool runs ONE wide, never its full width ticketless. Guards:
  `tests/test_govpool.py` (agentctl) and movies' `scripts/ci/fanout_lint.py` refuse a fan-out (`xargs -P`, a
  thread/process pool, `-n auto`) outside it, an allowed one naming why (network-bound).
- **THE GRANTED WIDTH** (2026-11-26: one movies `run-script-tests` ticket was charged 17 of 18 cores at load 9.8 --
  its ask was its tests' widths summed against the machine's cores, and with nothing charged the head went whatever it
  asked -- and every suite waited behind it). `gov admit --min-cores M` marks a pool that can run narrower: it is
  admitted once M cores fit and granted (and charged) what is free now -- `GOV_CPU_FACTOR` x the cores less the busy
  ones -- at most `$AGENTCTL_GOV_POOL_SHARE` (0.5) of the machine, never under M; the grant is the `cores` of the
  ticket's record (`<ticket>.json`), and the pool runs that wide (govpool.governed's `floor`). A TIME-BOUNDED runner
  asks a fixed width: agentctl's own suite and simulator gate do (granted 1 core at load 17, the pre-push suite only
  timed out). `gov probe` lists `grant`. A fixed ask (no `--min-cores`) is charged what it asked, as before. MOVIES' SIDE (not yet
  done; its run-script-tests is being migrated): jvm-gate.sh `gov_take`, called from `gov_width`, passes `--min-cores
  1` when `governor_has grant` and then sets `GOV_WIDTH` and `AGENTCTL_GOV_WIDTH` from the ticket record's `cores`
  instead of its ask; run-script-tests' `govern_run` counts its wide tests' extra only when the grant covers them.
- **`gov probe`**: `agentctl governor: admit cores route width grant`, the stable line a caller in another repository reads
  (jvm-gate.sh governor_bin; bitcashier's bin/gov-sbt calls `gov admit`, whose arguments stay as they are), never
  agentctl's source. `gov status` lists every live ticket with its class, memory, cores, JOB, label, pid, since and
  checkout, and `outside any ticket: B of M busy cores` when the machine's busy cores exceed the charged ones by a core.
- **Every suite run holds a ticket**: `tests/run.py` (and `tests/sim_gate.py`) take a class-`suite` ticket charged
  their workers through the governed fan-out (`agentctl suite` just runs run.py), its wait bounded at 900 s before its
  own `--suite-timeout` starts; the pre-push hook's KILL bounds leave that room. It owns each worker's temp root (`agentctl-t-*` with an `owner.pid`), removed however the worker
  ended, and fails the run naming any that survives; upkeep removes /tmp's `agentctl-t-*` past the bound unless its
  runner is alive or an entry of it changed lately.

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
- **CPU: pause when starved, resume with room** (the owner, 2026-11-19: "if cpu is starved then pause until we have
  enough resources and resume when we have too much"). BUSY is max(the live tickets' charged cores, the cores
  processes use now: ps's %CPU, less the governor's own waiters and idle Claude sessions) -- never the load average,
  which read 46 on 18 cores while processes used 5.2. Over 1.0x the cores (`$AGENTCTL_GOV_STARVED`) the laptop is
  STARVED and every class but red pauses (its waiters block with no CPU) until busy falls under 0.8x
  (`$AGENTCTL_GOV_RESUME`): between the two it keeps its state. Under 0.5x (`$AGENTCTL_GOV_SPARE`) it is SPARE.
  `agentctl gov capacity` prints one line, `starved|ok|spare: B of N cores busy (C charged, M measured, load L); A
  admitted, W waiting` -- what the orchestrator reads before starting or resuming lanes.
- **Cores** (the owner, 2026-11-18: 8 JVMs admitted on 40 of 77 GB drove the 18-core laptop to load 100 at 05:45Z,
  and 6 waiters went in within 7 s at 05:46Z as the lagging load dipped). Every ticket carries the cores its job is
  expected to use (`--job`: testUnit / test / what-if / measure / check 4, compile / job 3, testOnly / slice / suite 2,
  serve 1, keeper 0), or the job's measured p75 once `<gov>/usage.jsonl` holds 3 uncontended runs of it (load at
  most the cores; sbt-warm records each passing call: the server's CPU, exited forks included, over its wall time). Work
  is admitted only while busy (above) + its own <= the cores x `$AGENTCTL_GOV_CPU_FACTOR` (1.0): the charge takes a
  burst before the processes show it. A ticket of no cores never waits on cores, nor does one with nothing charged;
  red never waits. A kept sbt server's keeper is charged 0 (its memory); each command through it takes a
  cores-only ticket (`--mem 0`) for its life, as does each what-if / slice request to a kept JVM. A pre-commit check
  is charged its hook's cores, and the hook's sbt-warm calls take none (`KINOWO_GOV_CPU_HELD`). A ticket from an older
  jvm-gate.sh (no `--job`) is charged none. `waited_on` in `admits.jsonl` now names the last hold (it was always
  null), and a ticket's end is a `{"end", "id"}` line there (exact in process, the next look for a shell's).
- **Route** (`gov route`): a whole-module test run or a fail-before proof the governor would hold now (it does not
  fit, or waiters of its class or higher are ahead) goes to ci-runner's measure lane when ci-runners-page's fresh read
  shows no job waiting on `movies-measure`; otherwise local. Red is never routed. movies' scripts/ci-sbt runs it.
- **Replay** (`gov replay`): admits.jsonl around `--at` again, each admission an arrival at `at - waited`, held to its
  end record or `--hold` minutes, its cores its record's or its label's job; admitted oldest first under the cap.
  2026-11-18T05:45Z (18 cores, 30 min holds): as admitted 7 live, 25 cores (gov status said 8, load 51.8), peak 88;
  under the rule 4 live, 15 cores, 4 waiting, peak 18.
- **Eviction first:** when the head waiter does not fit on memory, the idle kept sbt server unused longest (no call in
  it, unused 1 min, not serving, never the root checkout's) is stopped (`sbt-warm --stop`, its busy lock held), then it
  looks again. Logged in `gov/gov.log`; every admission in `gov/admits.jsonl`.
- **Checks** keep their cap (below), now one of the governor's limits.

**Waiting is cheap (2026-11-19 15:35Z).** ~13 sbt-warm waiters each re-read
every waiter's liveness (two `ps` each) and the machine every 2 s: ~170 short processes a second, a load of ~60 on 18
cores that held them all on the load factor. A waiter behind the head now re-reads the queue only when its change mark
moves (a waiter or ticket leaving) or its backoff (2 s doubling to 30 s) runs out, a stat meanwhile; liveness is a
signal 0 with the start time read once a minute.

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
- `--check`: checks sbt, a java that runs, `secrets status <ns>`, the tunnel on localhost:3014 (the agentctl store)
  and that `gh` is there (it reads GitHub with its own auth: `gh auth status`).
- `--ns`: defaults to movies inside `~/projects/movies`, else bitcashier.
- Locked vault: exactly one line, `vault <ns> locked: ask the user to run `! secrets unlock <ns>``, then exit 1
  (on stderr with `--print`, so the eval stays clean).
- An agentctl fixer holding its credentials (fixer_env: `AGENTCTL_RED_FIXER` and the store token) is never stopped by a
  locked vault: `vault <ns> locked, not needed`, exit 0 (Tool self-tests #480, 2026-11-22: an owner with its fix
  committed ended BLOCKED ON OWNER [vault] on this check alone). A secret its fix needs asks for the vault then.
- Exit: 0 all ok, 1 a problem.

```
$ agentctl env --check --ns movies
ok   sbt: /Users/pawel/.sdkman/candidates/sbt/current/bin/sbt
ok   java: openjdk version "27" 2026-09-15
ok   vault movies: unlocked
ok   tunnel localhost:3014 (agentctl store): open
ok   github: gh /opt/homebrew/bin/gh
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
  `gh workflow run` and records it; `dispatch record` registers a dispatch made another way. agentctl's own
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
  inputs for that workflow (its dispatch inputs, read from the owner record (GitHub serves none), ignoring tier, runner,
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
agentctl inbox [ID...] [--unread] [--ack [--all]] [--limit N] [--count] [--json] [-v]
```

The owner (2026-10-27): three agents ended their turns while their land/watch still ran, and the WATCH verdicts went
nowhere. Every verdict now reaches the inbox: a change's, a batch's and a watch's as EVENTS the processor projects
(`QUEUED`, `LANDED`, `WAITING`, `NOT LANDED`, `WATCH ...`, `FIX ...`, each naming the author's worktree); `wait` and
`land --follow` write theirs when they end (normally, on a signal, or on a crash). A read-only or queue command
(`land status|hold|release|cancel`, `status`, `red status`, `inbox`, `ci-why`) writes none: it answers its caller
there and then (2026-11-16: a monitor polling `land status` had written 2,692 entries, acked by hand).

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
- Each entry has a `kind`, from its verdict: `RED`, `OK`, `STALLED`, `ENDED` (a command killed by a signal), `USAGE` (a misused command), `UNKNOWN` (also a crash, an exit with
  no verdict), `NOT_LANDED`, `SUPERSEDED`, `LANDED`, `WAITING`, `HELD`, `FIX_FAILED`, `FIX_OK`, `FIX_UNVERIFIED`, `CHECK`, `OTHER`;
  from `red`: `RED_UNCLAIMED`, `RED_BUDGET`, `RED_ESCALATE`, `BLOCKED`, `RED_DISPATCHED`, `FIXER_LANDED`,
  `FIXER_FAILED`; from the blame (see "blame"): `RED_BLAME`, `RED_REVERT` (a `RED REVERT REFUTED` too), `RED_RELAND`,
  `LANDER_REVIVED`, `LAUNCH_FAILED`; from the branch runs: `BRANCH_RED` (see "dispatch"); from a revert on main:
  `REVERTED` (its lander's), `REVERT_NOTICE` (its orchestrator's or the owner's; see "revert").

#### inbox escalated: an escalation that wakes somebody (2026-10-27)

Main #2146's `RED UNCLAIMED` woke nobody: the orchestrator's `inbox wait` was not re-armed. An escalation is now an
EVENT of severity `escalate` -- an item past its deadline in a state that escalates (a red offered too long, a
condition left open, a stalled change) -- and the processor's notifier delivers it: a macOS notification
(terminal-notifier, clickable; on any failure the `osascript -e 'display notification ...'` fallback; never a dialog),
marked notified in the store only once a delivery got through (W6 checks none is left undelivered; WN that delivery
works). ONE LIVE NOTIFICATION PER ITEM (2026-11-03, W6 "152 not notified"): an escalation whose item moved on (a red
leased or cleared since, a condition closed, a new spell) is closed untold; of one item's pending escalations only the
newest is told; a repeat (same kind and state, same spell) waits for the reminder schedule (1 h, 2 h, 4 h, 4 h, ...);
more than 3 items at once (a catch-up after a sleep or a store outage) is ONE summary notification. The status line leads with a bold `ESCALATED: N (...)` while any is current; `agentctl inbox escalated`
prints that line, or nothing.

#### the alert model: who must act on what (2026-11-04)

1524 owner-facing alerts in 7 days over 614 items; ~35 needed the owner. The cause: alerts were emitted per EVENT from
many call sites with no model of who acts. Now every inbox entry -- `inbox_write` and the event projector alike -- goes
through ONE router (`lib/agentctl/alerts.py`):

- every entry is keyed by an ITEM (a red, a change's worktree, a condition, a run an author watched...) and stamped with
  an AUDIENCE: `author` (NOT LANDED, WATCH RED, RED AUTHOR, FIX FAILED, a command's own crash/`ENDED`/`USAGE`; a
  change's LANDED / SUPERSEDED, each an entry of its own under the queued change's id, never merged), `fixer`
  (RED FIXER ..., RED DISPATCHED, RED ESCALATE, RED UNCLAIMED/STARVED while a holder or a family owner is alive or
  before `alert_red_owner_level` offer rounds, a self-healing condition before its bound), `owner` (BLOCKED ON OWNER /
  WAITING ON YOU, RED BUDGET, UNLANDED ORPHAN after its fixer, a red nobody can take, a condition past its bound) or
  `info`. Only the owner's reach `inbox wait` (by default), the notifier and `ESCALATED`.
- ONE LIVE ALERT PER ITEM: a new event for an item whose alert is unread updates it in place (`verdict`, `count`,
  `verdicts`, `at`); it is shown again (`surfaced`) only when it becomes the owner's or its red changes tier. It
  closes by itself (acked, `closed: {at, why}`) when its item's probe clears -- the red taken or cleared, the
  condition closed, the change landed, the worktree removed (the sync's `alerts` step, every minute) -- or when a
  closing verdict for the item arrives (LANDED, WATCH OK, FIX OK, RED CLEARED).
- AN ALERT WHOSE CONDITION CLEARED CLOSES, whatever item it was keyed under (2026-11-06: 44 owner alerts, nearly all
  cleared days before): a red alert once no open red names it (its `decide <key>` or `<workflow> #N`); UNLANDED ORPHAN
  once its worktree is gone or its commits are on main (git, at most every 10 min a worktree); WATCH UNKNOWN once every
  gate workflow has a run decided that started after it; NETWORK once no NETWORK condition is open (after 10 min); a
  HELD batch once that batch is no longer held. What cannot be told (no red named, no gate snapshot) stays open.
- invariants W5/WM/W7 open their condition only after 2 consecutive misses and are the owner's only past 30 min.
- The index (item -> live entry) is `<inbox>.alerts/index.json`, beside the inbox. Its first use migrates: every unread
  entry is stamped and one item's older unread entries are folded into its newest (acked, counted): no flood.
- `agentctl inbox --audience owner|author|fixer|info|all` filters a listing; `--count` prints how many match, every one
  (never the listing's `--limit`): the status line's `inbox --unread --audience owner --count`.

#### inbox wait: the orchestrator's notifier

```
agentctl inbox wait [--kinds INVARIANT,NETWORK,RED_STARVED,RED,BRANCH_RED,FIX_FAILED,NOT_LANDED,STALLED,UNKNOWN,RED_UNCLAIMED,RED_BUDGET,RED_ESCALATE,BLOCKED,FIXER_FAILED,...] [--audience owner] [--since-ack] [--timeout S] [--forever]
```

`--audience` (default `owner`; `all` is the old behaviour): only the alerts addressed to it (see the alert model).

`--forever` never exits: it streams each such entry once, one line each, as it appears -- ONE long-lived background
task (Monitor or `run_in_background`) covers the whole session, no re-arm needed; ack as you route.

It blocks until an UNREAD entry of those kinds (the default above, and with no `--kinds` every alert addressed to the
owner, whatever its kind) exists, prints every such entry, and exits 0 (2 at `--timeout`). It acknowledges nothing.
Unread is judged PER ENTRY: every arming prints the whole unread backlog first, whatever was acknowledged after it.
`--since-ack` is accepted and means just that (until 2026-11-20 it skipped everything older than the NEWEST ack, so an
orchestrator acking one id at a time hid an older unread owner alert for a day). An alert whose subject has resolved
(its issue closed, its red closed, its hold released, its question no longer asked...) is acked by the reconcile pass
with its `closed` reason, so the backlog is only what is still open. It is cheap: one `stat()` of the inbox directory and of `acked/` a second (`$AGENTCTL_INBOX_POLL`), the
entries re-read only when either changed; no forge, no git. The orchestrating Claude session's loop:

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
final trailer block with Co-Authored-By); undeclared deletions and modifications still count. A file the change MOVES (git rename detection, `diff -M`, 50% similarity: a move with a small edit counts) is not a deletion: its old path is exempt, so a module extraction or a renamed directory is not "undoing" the commit that created it; a deletion with no matching add still counts. Nor is a COHERENT EDIT (2026-11-19, movies 467e9026: a WebReadModel split dropped constructor parameters, so four specs lost the arguments 73aad048 had added and came back byte-for-byte to their pre-73aad048 content): a file returned to its content before commit C is exempt when the identifiers it loses also leave another file of C that the push rewrites -- removing lines C added there without restoring what C replaced (the definition changed in the same push). A squash or an old-side conflict resolution restores C's replaced lines, or touches no other file of C, and still counts. It also refuses an EMPTY commit (no tree change) unless it carries
an `Allow-Empty: <why>` trailer. `land` runs it, and so does movies' hook. Exit 0 ok, 1 refused.
The refusal names the declared way out, a `Retires: <path>` line per deleted file it counted, and calls
`AGENTCTL_MAX_FOREIGN` what it is: a bypass for every file, to be named in the report (issue 5917bb, 2026-11-20: a lane
deleting its own scaffolding raised the cap to 6, unnamed).
A land never leaves a rebase in progress (issue 494caa): it refuses to start in a worktree with one in progress (the
agent's to `--continue` or `--abort`: commits made on its detached HEAD would be lost to an abort), and aborts any rebase
it started on every path it ends by (slots.py `sync`).
The sync fast-forwards the movies ROOT CHECKOUT to <remote>/main each cycle -- only on `main`, clean in its tracked files,
no rebase/merge/cherry-pick in progress, fast-forward only -- so its `.claude` hooks run main's code (issue 66d831;
server.py `root_checkout_ff`).

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
agentctl cleanup [--dry-run|--execute] [--repo PATH ...] [--no-fetch] [--no-remote] [--min-age-hours H] [--include-caches] [--deadline S] [-v]
```

For the scope and the log, see "Standing-OK cleanup scope" above. It is a dry run by default on a TTY. Exit 0, or 1
if something could not be removed. Bounded (2026-11-22: the upkeep's pass hit its 1800 s bound on a lossy uplink, every
https fetch waiting out git's 600 s): a tree's git status gets 120 s (`$AGENTCTL_CLEANUP_ITEM_TIMEOUT`, else kept as
undecided), the fetch is the unattended 180 s one with an https low-speed cut-off, and one fetch timing out skips the
other repos. `--deadline S` (the upkeep passes 1200) stops it by itself: no tree looked at past S, no removal begun
within 300 s of it (`$AGENTCTL_CLEANUP_REMOVE_RESERVE`); its last line says how long each repo took (into upkeep.log).

```
agentctl cleanup (DRY RUN); log: /…/scratchpad/agentctl-deleted.log

== movies (<remote>/main cac5af8ce0)
  would delete worktree /Users/pawel/projects/movies-wt-hookguard [hookguard] 6a2a075956: ancestor of <remote>/main
```

### orphan worktrees: submitted once

A worktree with commits not on <remote>/main whose author is gone (no live lease, no process in it, no live session
that ever named it, idle) and whose author once ran `agentctl land` there is SUBMITTED ONCE by the tick, whatever its
last verdict: it becomes a queued change like any other (its checks then decide; a rejection is its red's to rescue),
an `ORPHAN SUBMITTED` owner alert names the worktree and its commits, and an `UNLANDED ORPHAN` inbox entry follows if
it still does not land.

The 2026-11-06 incident behind the rules below: orphan duty took `~/projects/movies-wt/es-gate-check` (branch
`check/es-gate-reenable`, a gate re-enable the owner was holding, the worktree of a live subagent waiting on CI from a
sibling worktree) for stranded work, rebased it in place and queued it.

- **Ownership follows activity** (2026-11-03, imdbslip; 2026-11-06, es-gate-check): besides a process in it, a land, a
  live lease or a queue entry, a LIVE Claude session -- any of its transcripts (its own or a subagent's) written within
  2 h (`$AGENTCTL_OWNER_ACTIVE`) -- owns every worktree a tool call of it EVER named (`~/` and `$HOME/` paths too), or
  whose project dir is the worktree: no orphan action, and an open `UNLANDED ORPHAN` clears (`a live session works in
  it`). It lapses when the session goes quiet. Transcripts are read whole, incrementally (the paths each named are
  cached in `recovery/session-paths.json`). The old rule -- a tool call naming it in the last 30 min -- missed the
  es-gate-check subagent, which had named it 104 min before.
- **Land intent**: only a worktree that ran `agentctl land` itself (a land inbox entry with that cwd) or has a change
  item of its own is ever submitted. One that never did (an agent that died mid-task, a design or check tree) goes to a
  person at once: `UNLANDED ORPHAN ... its author never ran agentctl land there`.
- **Not for landing**: a branch named `check/*`, `measure/*`, `experiment/*`, `probe/*`, `prototype/*`, `spike/*`,
  `wip-*` or `wip/*`, a commit with a `Hold: <why>` trailer, or `do_not_land`'s markers: never submitted and never
  escalated (`orphan <name>: not submitted (...)`, said once); an open `UNLANDED ORPHAN` for it clears.
- **The author's worktree is never touched**: a submit runs `agentctl land` in a throwaway tree of agentctl's own
  (`recovery/reland-trees/<worktree name>`, tagged internal, detached at the author's HEAD; replaced by the next
  re-land), so the rebase happens there; the author's branch, HEAD and files stay as they were. Its verdicts (cwd: the
  throwaway tree) are read as the orphan's.
- **Infra is no verdict** (2026-11-02, flip-stage5's `crashed: StoreUnavailable`): a last try that failed on the store,
  the tunnel, a fetch or the network never escalates; it is submitted again once the store answers (backing off 15 min
  → 6 h), however many tries it takes.
- **A real failure gets ONE fixer**: a submit rejected by the pre-push checks (or a rebase conflict), nobody owning
  the worktree, gets one background fixer in it (`orphan · <worktree>` in `claude agents`; at most 2 alive, one
  dispatched a pass), once per worktree whatever later rebases do; only after it ended unlanded does `UNLANDED ORPHAN`
  go to a person.

**Parked work** (2026-11-03: design branches and blocked changes escalated over and over). A worktree that is
deliberately not landed is parked with ONE condition:

```bash
~/bin/agentctl park ~/projects/movies-wt-x --reason "<why>" --owner-decision          # until a person unparks it
~/bin/agentctl park ~/projects/movies-wt-x --reason "<why>" --until-landed <change id|commit>
~/bin/agentctl park ~/projects/movies-wt-x --reason "<why>" --until 2026-11-20
~/bin/agentctl park                                                                   # list (also in `agentctl status`)
~/bin/agentctl unpark ~/projects/movies-wt-x
```

A parked worktree is never submitted, fixed or escalated as an orphan (an open `UNLANDED ORPHAN` clears: `parked:
<why>`); `agentctl status` shows `parked: N` and the reasons. Each maintain pass re-reads the condition: the blocking
change LANDED (or its re-submission) or the commit is on main by content, or the date passed → the park is released
(`UNPARKED` inbox entry), its orphan records dropped, and the orphan rules apply afresh: it gets its one submit. Parked
longer than 14 days: ONE `PARKED DIGEST:` inbox line a week for all of them (wakes nobody), never an escalation.

**A dead blame revert is no orphan** (2026-11-02, revert-629): a worktree whose commits are a withdrawn or refuted
blame revert, or that carry `Reverts-Change:` for a change blame marked not the culprit, is never submitted (`orphan
<name>: not submitted (...)`); one holding only blame's reverts is removed (logged), any other is kept for a person.

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
(sbt-warm's `.serving`), that hold a running command (an sbtn client) with no CPU for 20 min across reaps, or that
are detached (PPID 1), have no client and used no CPU for 120 min -- 30 min once swap free is under 2048 MB
(`sysctl vm.swapusage`; unreadable = the 120) -- and are not kept by an `sbt-warm --keep` in their checkout
(2026-11-07: two lone idle 6 GB servers sat 3 h while swap was full). "No CPU" means under 1 CPU s/min since the
previous reap (`AGENTCTL_SBT_PROGRESS_CPU_PER_MIN`): an idle JVM's background threads burn 0.04-0.12 s/min, which
defeated the earlier fixed 0.5 s tolerance so neither rule ever fired. Readings are dated by the AWAKE clock
(`CLOCK_UPTIME_RAW`, stopped while the laptop sleeps and the JVM is frozen), so the minutes of a rate or an idle time
are awake ones (on the wall clock a 25-min sleep read a busy command as 26 min without CPU); a previous reading more
than 30 awake min old or from the future (another boot, an older agentctl's wall-clock record) starts the server over
(`~/.cache/agentctl/sbt-cpu.json`; a
`jcmd Thread.print` dump first, in `~/.cache/agentctl/sbt-dumps/`) -- never an
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
Each pass also marks every worktree of agentctl's repos, and its `target/`, with `.metadata_never_index` (kept out of
`git status` by the repo's shared `info/exclude`): 2026-11-19 mds indexed ~26 movies worktrees at ~1.5 cores. macOS
honours that marker unevenly; the sure exclusion is the owner's System Settings > Spotlight > Search Privacy entry
for `~/projects`. A worktree some process has its cwd in (a land, a check, a replay, an agent) is marked at a later
pass, once nothing runs there (2026-11-22: a pass marked a fix replay's tree mid run-script-tests, which read the
marker as its test's leftover).

Each pass also sweeps the iOS Simulators agents left booted (`ios_simulator_sweep`, 2026-11-07: three booted for days took
the laptop to load 368): movies' `scripts/ios-simulators sweep` from the root checkout, bounded to 120 s, laptop only,
skipped when the script or `xcrun` is missing; its output is in the maintain log, and an inbox info entry says how many
it shut down. Fixers are told to run `xcodebuild test` / Simulator work through `scripts/ios-simulators run -- <cmd>` and
to name a Simulator they create `fixer<N>-…` (docs/fixer-brief.md, briefs/movies-agent-preamble.md).

Each pass also ROTATES THE LOGS (`rotate_logs`: maintain alone since 2026-11-26, the hourly upkeep no more; 2026-10-30: the processor's service.log
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

### history, analyze: every unit of time attributable (2026-11-26)

```
agentctl history <change-id|sha|red|branch> [--json]   # one item's timeline: each move, the time in the state it left, cause, actor, commits
agentctl analyze [--since 7d] [--type change] [--json|--tsv]  # time per state, variants, rework, refusals, waiting by cause, throughput, outliers, coverage
```

Every event carries `actor` (who: `reconciler`, `agent:<name>`, `session:<id>`, `owner`, `cli:<cmd>`; actors.py, stamped
by CurrentStore at the composition root), `cause` (why: ONE word of the closed enum `causes.CAUSES` -- `submitted`,
`check-running`, `check-failed-test|infra`, `gate-red`, `main-moved`, `overlap-hold`, `regen-hold`, `lock-wait`, `batch`,
`no-longer-applies`, `policy`, `superseded`, `agent`, `owner`, `timeout`, `ci-verdict`, `red-fix`, `probe`, ...) and a
short `detail`. `Store.transition` REFUSES a move between two states without a cause (MissingCause: a programming
error; tests/test_causes.py checks every call site); a reminder (self-move) may carry one -- a held change's reminder
carries its hold kind's (`HOLD_CAUSE`). A store older than the columns migrates in place (old rows NULL); a client older
than them is written `unattributed` (server.py's shim). Creating an item writes its OPENING event (`<type>.<state>`, from
no state: a change's queue entry, a condition's open, a watch's start). A reject's cause is THE reject classifier's
(`causes.reject_cause`, ci-why's own infra signatures), its class (infra | test | policy) the event's detail.

THE BACKFILL never rewrites an event: the server's tick (`timeline` step, hourly) runs `Store.backfill` in-process (idempotent),
deriving into `event_backfill` (an old event's cause and actor) and `derived_events` (the missing openings, from the
items' recorded times), each row with its source and confidence (`exact`: a state or a recorded time; `inferred`: text).
`history` and `analyze` merge them in; analyze's coverage says own / own-or-exact / with-inferred (an undrained offline-log line counts as own). An inferred row the store's own
structured history proves is then marked EXACT (backfillexact.py), its proof its source: a change entering `waiting`
while a gate red item was open (that red's own events), a held reminder whose holder -- a change or a batch -- was open
in the state it names (the holder's own events), a change `checking in ... batch B` that B's item lists as a member.
On the 14:00Z copy this raised the exact share of change time from 55.8% to 89.5% (1,557 rows; 17.0 d of `waiting`
and 10.6 d of held `queued` now proven); what stays inferred is mostly `waiting on a check slot` (3.0 d), push waits
(1.3 d) and cancels (1.2 d). The live store wrote no own cause until 14:29Z: ci-runner's pin held 12cecce while
agentctl's main was red. On the 2026-11-26
store copy: 0% own (the history predates the columns), 56% own-or-exact, ~100% with inferred. Once a week the tick sends
the owner the top 3 time sinks (`TIME SINKS (7 d)`).

THE EVENT MODEL (eventtrace.py, eventreader.py; 2026-11-27): the store's events, the backfill's rows and this host's
offline-log lines not yet drained are ONE row shape, each row's `origin` its provenance -- `own` (the cause written with
the move), `exact` / `inferred` (the backfill's), `file` (an undrained line: its own cause; the store's once drained,
by its dedupe key), `none`. ONE reader, `read_events(store, EventQuery)`, serves `history`, `analyze` and the weekly
summary: the store answers the query through its indexes (`trace_events`, API version 2: one item's rows, every row of
each item of a type active in a window -- a row in it, or not yet ended --, or the rows of some kinds; indexes
`events_type_at`, `events_kind`, `derived_events_item`, `derived_events_type_at`, made in place on an older store).
A store older than the call is read the old way (its whole feed and backfill, merged by the client): the same rows.
On the 2026-11-26 22:00Z copy, `analyze --since 7d` moved 22.0 MB over the API before, 7.9 MB now (3.6 MB of rows, the
rest the change items its check-row table reads), and `history <id>` 11.0 MB before, a few KB now (an exact id is
asked item by item); every report and trace the same.

PER-ROW RUNTIME (checkrows.py): a decided remote check's jobs become ROWS on its change item (`check_runs`, one record
a run, the newest 10: the run, the tip it checked, its verdict, and each row's runner wait, start, end, seconds and
conclusion; a run decided by its conclusion reads its jobs once more for their ends). The runs decided before this (an
item's `check.run`) are fetched from GitHub by the hourly backfill, 30 a pass, 0.5 s apart, cached in
`derived_check_runs` (a run that gave no jobs asked again a day later). `analyze` ranks the rows (`check rows`: n,
p50, p90, total, runner-wait p50, failures, the slowest change); `history <change>` lists its runs and rows.

FILE-HELD STATE AS EVENTS (filelog.py): the governor's tickets (`gov-ticket`: opened `waiting` when it asked -- cause
`check-slot` for a class's cap, else `governor`, the hold in `detail` -- then `admitted`, `released`), an idle sbt
server evicted (`sbt-server`), the inbox (`inbox`: `unread`, `acked`) and the waiting entries' statuses (`entry`) are
lines of this host's OFFLINE LOG ($AGENTCTL_STATE_ROOT/events/offline.jsonl, appended under an flock: no network on
the write). The processor's tick on either host drains it into the store (`record_events`, idempotent by dedupe) from a
byte cursor that moves only once the store took a page: a store that is down, or a server older than the call, keeps
the lines -- nothing is lost. A line the store would refuse is skipped, never a block; a drained log past 1 MB starts
again. `analyze --type gov-ticket` (or inbox, entry) reads them like any item's.

QUEUE STALLED (queuestall.py, the server's tick): changes open and no change item moved (a reminder is no move) for
60 min ($AGENTCTL_QUEUE_STALL_AFTER) opens one STALLED condition naming the head blocker -- the oldest open change, its
state, age and hold, and the open gate red. An alert, never a hold. The owner, 2026-11-26: kept, warn-only, its bound
90 -> 60 min (policy `queue-stall`, effect `alert`). Replayed over the 14:00Z copy's change moves: 20 firings at 60 min
(74.0 h of stall) against 8 at 90 (60.1 h); the 41.4-h stall of 11-23 named at 08:45Z instead of never.

### stats: the pipeline's records in VictoriaLogs (2026-11-17)

The owner, 2026-11-17: "keep landing stats -- timings, time between landing and landed in victoria logs, modules touched
etc -- for easy analysis", then "escalations, gate builds red etc", "how long each job waited for a CI runner" and
"consolidate with older stats". The reconciler's `stats` step (ci-runner, every 60 s, `lib/agentctl/stats.py`) writes
ONE flat JSON record per finished thing into **victoria-logs-ci** (`http://victoria-logs-ci.service.consul:9429`, 400
days; Grafana datasource `VictoriaLogs CI`, uid `victorialogs-ci`; the store the CI collector's `source:ci` events
already live in), with stream fields `source:agentctl` and `record:<name>`. `$AGENTCTL_STATS_URL` names another store
(`off` disables); off ci-runner nothing is sent.

- **Derived, never kept.** Every record is built from the store's items and their events (and, for run figures, the
  collector's `source:ci` rows), so the history is rebuilt by the same code: a record whose exit is older than the first
  pass (meta `stats_since`) has `backfill: true` -- the backfill IS the first passes (150 records a pass). Each record is
  sent once (meta `stats:<name>:<id>`); a refused POST leaves it for the next pass. A change's records wait 15 min after
  its exit (`STATS_SETTLE`: the collector exports a run up to 5 min after it ends). A live (non-backfill) record is also
  printed on the reconciler's stdout, the journal: `agentctl.<name> {json}`.
- **Every record:** `source` (agentctl), `record`, `schema` (1), `backfill`, `_time` (the exit), `_msg` (one line),
  `repo`. Durations are seconds (`*_s`), times ISO UTC (`*_at`). A field with no value is left out. Field names are
  stable; a rename bumps `schema`.

| record | one per | fields |
|---|---|---|
| `agentctl.landing` | change exit (`_time` = exit) | `change_id`, `lander_agent`, `lander_session`, `lander_host`, `worktree`, `branch`, `head_sha`, `base_sha` (main at submit), `checked_base`, `landed_sha`; `outcome` (landed/rejected/superseded/cancelled), `reason_class` (landed, superseded, checks-failed, conflict, too-slow, precheck, blame, cancelled, other), `reason`; `submitted_at`, `exited_at`, `total_s` (land -> exit, i.e. land -> LANDED), `queue_wait_s` (submit -> first check), `queued_s` (all queued time), `checking_s`, `checked_wait_s` (checked -> pushing), `pushing_s`, `push_to_landed_s`, `waiting_s` (held by a red gate); `rounds`, `full_rounds`, `verify_rounds`, `full_rechecks`, `infra_requeues`, `recheck_reasons` (comma list), `kept_verdicts` (main moved under it and no round ran: MINIMAL RECHECKS); `red_fix` (Fixes-Red), `fixes`; `commits_n`, `files_n`, `modules` (top-level dirs, comma list, `(root)` for root files), `sbt_modules` (dirs with a `src/`), `areas` (the precommit-check jobs that ran: rest, identity, oldrefs, heavy:<row>; `page` only before 2026-11-17, when the page area moved to after landing); `check_runs` (precommit-check run ids), `check_runner_wait_s` / `check_run_s` (summed critical-path queue / work of its runs), `check_jobs_n`, `check_job_queue_max_s`, `check_job_queue_sum_s`, `check_job_run_sum_s`; `precheck_s`, `precheck_outcome` (passed/refused/unchecked), `precheck_step` (noted on the item by the laptop's `land-precheck`, tried again through a store blip or a not-yet-submitted item for up to `$AGENTCTL_NOTE_PRECHECK_WAIT` s; none before 2026-11-17); `batch`, `superseded_by` |
| `agentctl.landing_round` | check round of a change (`_time` = round start) | `change_id`, `outcome`, `lander_agent`, `round`, `mode` (full/verify), `reason_class` (first, verify, build, hook, selection, module-overlap, overlap, deletion, plan, from-waiting, infra:<reason>, other), `reason`, `started`, `ended`, `check_s`, `queued_before_s`, `result` (checked, requeued, rejected, waiting), `run_id`, `run_queue_s`, `run_work_s`, `run_wall_s` |
| `agentctl.landing_watch` | landed sha's post-push watch | `landed_sha`, `change_id`, `verdict` (ok/red/unknown), `verdict_line`, `pushed_at`, `watch_s` |
| `agentctl.red` | red episode (a red item, open -> verified/cleared) | `red_id`, `key`, `workflow`, `gate`, `red_kind` (gate/outside/stuck-run/flake), `verdict_class` (test-failure / infra-shaped / no-verdict), `family`, `jobs`, `signature`, `first_run`, `last_run`, `runs_n`, `opened_at`, `closed_at`, `duration_s`, `outcome`, `closed_by`; `fixers_n`, `fixer_restarts`, `fixer_holders`, `first_fixer_at`, `time_to_fixer_s`, `offered_s`, `leased_s`, `fix_pending_s`; `fix_sha`, `fix_entry`, `fix_status`, `fix_landed_at`, `red_to_fix_landed_s`, `red_to_green_s` (verified only); `waited_changes_n`, `waited_changes_s` (changes held by a gate red, their waiting inside the episode); `owner_blocked`, `blocked_s`, `decision_wait_s`; `escalations_n`, `notifications_n`, `attempts` |
| `agentctl.condition` | condition spell (opened -> closed) | `condition_id`, `key`, `kind` (INVARIANT, NETWORK, STALLED, UNKNOWN, UNLANDED_ORPHAN, ...), `invariant` (W1, W2, W5, W8, WR, WB, WD, ...), `host`, `opened_at`, `escalated_at`, `closed_at`, `duration_s`, `violation`, `until`, `closed_by`, `manual_close`, `escalations_n`, `notifications_n` |
| `agentctl.owner_decision` | question a red put to the owner | `red_id`, `belongs_to` (`red:<id>`), `workflow`, `gate`, `question`, `answer`, `asked_at`, `answered_at`, `wait_s`, `answer_path` (decided / auto-resolved / stale: unanswered 7 d), `red_closed_at`, `red_outcome`, `reminders_n`, `asks_n`, `decision_failed` |
| `agentctl.gate_run` | completed gate run on main (Main, Identity model convergence, Identity takes, matched by their workflow FILE: a convergence-dispatched Identity takes run's name is `Identity takes (convergence run <id>)`, kept as `run_name`; the first backfill also wrote ~bitcashier/core "Main Pipeline" runs, `repo:bitcashier/core`: filter on `repo`) | `workflow`, `run_id`, `run_number`, `run_attempt`, `sha`, `event`, `conclusion`, `started_at`, `ended_at`, `duration_s`, `path_queue_s`, `path_work_s`, `critical_path`, `critical_job`, `critical_job_s`, `critical_job_queue_s`, `slowest_job`, `slowest_job_s`, `jobs_n`, `runner_min`, `runner_wait_s` (every job's queue summed), `run_s`, `job_queue_max_s`, `failed_jobs` |

**Per-job runner wait is NOT an agentctl record**: the CI collector's `source:ci kind:job` events (movies'
`scripts/ci/ci_trends.py export`, ci-runner's `ci-metrics-collector`, every workflow of movies and bitcashier/core,
backfilled 90 days from the GitHub API) already are one per completed job -- `workflow`, `run_id`, `job`, `label`
(matrix label), `runner_name`, `machine_hosted` (github-hosted / self-hosted), `queue_s` (queued -> started),
`duration_s` (started -> completed), `conclusion`, `branch`. A copy would be a second truth; the queries below derive
the host and the branch kind from it, and `agentctl.gate_run` and `agentctl.landing*` are computed FROM those rows.

**Ready LogsQL** (vmui: `ssh -L 19429:victoria-logs-ci.service.consul:9429 ci-runner`, then
`curl -s http://127.0.0.1:19429/select/logsql/query --data-urlencode 'query=...'`; or Grafana's "Landing pipeline"):

```
# land -> LANDED p50/p90 per day
source:agentctl record:agentctl.landing outcome:landed | stats by (_time:1d) quantile(0.5, total_s) p50, quantile(0.9, total_s) p90
# queue wait vs check time vs red-gate wait (mean seconds per landed change, per day)
source:agentctl record:agentctl.landing outcome:landed | stats by (_time:1d) avg(queue_wait_s) queue, avg(checking_s) checking, avg(check_runner_wait_s) runner_wait, avg(checked_wait_s) to_push, avg(waiting_s) red_gate
# rechecks by reason (rounds after the first)
source:agentctl record:agentctl.landing_round round:>1 | stats by (reason_class) count() rounds, sum(check_s) check_s
# slowest modules touched (land -> LANDED by top-level dir)
source:agentctl record:agentctl.landing outcome:landed | split "," from modules as module | unroll by (module) | stats by (module) count() n, quantile(0.5, total_s) p50, quantile(0.9, total_s) p90 | sort by (p90 desc)
# red-fix time: red fixes' land -> LANDED, and red -> fix landed / -> green per workflow
source:agentctl record:agentctl.landing outcome:landed red_fix:true | stats quantile(0.5, total_s) p50, quantile(0.9, total_s) p90
source:agentctl record:agentctl.red | stats by (workflow) count() reds, avg(red_to_fix_landed_s) to_fix, avg(red_to_green_s) to_green
# reds per day by workflow and class; mean time to green; the slowest fixes
source:agentctl record:agentctl.red | stats by (_time:1d, workflow, verdict_class) count() reds
source:agentctl record:agentctl.red gate:true outcome:verified | stats avg(red_to_green_s) mttg, quantile(0.9, red_to_green_s) p90
source:agentctl record:agentctl.red red_to_fix_landed_s:>0 | sort by (red_to_fix_landed_s desc) | limit 10 | fields red_id, workflow, jobs, red_to_fix_landed_s, fixers_n, fix_sha
# the most frequent invariants / conditions, and their time open
source:agentctl record:agentctl.condition | stats by (kind, invariant) count() spells, sum(duration_s) open_s, sum(notifications_n) notified | sort by (spells desc)
# owner decision wait p50/p90 by answer path
source:agentctl record:agentctl.owner_decision | stats by (answer_path) count() n, quantile(0.5, wait_s) p50, quantile(0.9, wait_s) p90
# gate run duration trend, and its critical job
source:agentctl record:agentctl.gate_run repo:="pawelkrupinski/movies" | stats by (_time:1d, workflow) quantile(0.5, duration_s) p50, quantile(0.9, duration_s) p90
source:agentctl record:agentctl.gate_run workflow:="Main" | stats by (critical_job) count() n, avg(critical_job_s) s | sort by (n desc)
# runner wait p50/p90 by host and branch kind (main / check / measure / renovate ...), from the collector's jobs
source:ci kind:job | format if (runner_name:~"^GitHub Actions") "github-hosted" as host | format if (runner_name:~"^ci-runner") "ci-runner" as host | format if (runner_name:~"^bitcashier") "bitcashier-runner" as host | extract_regexp "^(?P<branch_kind>[a-z]+)" from branch | stats by (host, branch_kind) count() jobs, quantile(0.5, queue_s) p50, quantile(0.9, queue_s) p90
# runner wait by matrix label / lane, and by hour of day (UTC)
source:ci kind:job | stats by (workflow, label) quantile(0.5, queue_s) p50, quantile(0.9, queue_s) p90
source:ci kind:job | format if (runner_name:~"^GitHub Actions") "github-hosted" as host | format if (runner_name:~"^ci-runner") "ci-runner" as host | format if (runner_name:~"^bitcashier") "bitcashier-runner" as host | extract "T<hour>:" from _time | stats by (hour, host) quantile(0.5, queue_s) p50, quantile(0.9, queue_s) p90 | sort by (hour)
```

The host comes from `runner_name` (`GitHub Actions N` hosted, `ci-runner*`, `bitcashier-gh*`), not `machine_hosted`,
which older collector rows lack. A red fix's check runs are `branch_kind:check` too; `agentctl.landing`
`red_fix:true` names them.

**The older stats, consolidated (2026-11-17 inventory):**
- `source:ci` (victoria-logs-ci) -- THE per-run/per-job CI record; agentctl's records are derived from it, never a copy.
- movies' Prometheus `kinowo_ci_*` (monitoring-1, `scripts/ci/ci_timings.py` over OTLP from the "Report run timings"
  workflow; the CI-regression alerts `ci-build-times.rules`, dashboard `ci-builds.json`) records the same run/job/queue
  facts a second time, in the other fleet (monitoring-1's Grafana cannot reach victoria-logs-ci). Not merged here: moving
  those alerts onto `source:ci` is a movies monitoring change (monitoring-1 deploys are the owner's `--apply`); named as
  the follow-up.
- the runners page (`ci-runners-page`, `last-good.json`): a LIVE snapshot (busy runners, the waiting jobs now) and 24 h
  of CPU -- no per-job history, nothing duplicated; kept.
- ci-runner's `movies-runner-scheduler` journal: free-text tick and `STARVED` lines, no consumer; per-job waits are
  `source:ci`'s `queue_s`. Kept as the scheduler's operational log.
- `agentctl-metrics.timer` (`/metrics` -> node-exporter textfile `agentctl.prom`: `agentctl_oldest_change_age_seconds`,
  `agentctl_red_unleased_seconds`, ...): live health gauges for alert rules, not per-thing history; kept -- the records
  are the history, the gauges the present.

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

**The forge** is GitHub (Forgejo was switched off 2026-10-31): agentctl READS CI -- run lists and views, failed and
job logs, takes artifacts, release pins, `dispatch run`, run cancels, the actions API (remote checks, the Fixes-Red
verify dispatch, measure-branch cleanup) -- through the real `gh` (`gh_cli`) with gh's own auth (`$GH_TOKEN` or
`gh auth login`; on ci-runner the repo's App token, `lib/agentctl/ghapp.py`), and run links go to github.com by run
id. GitHub serves no dispatch inputs: they come from the dispatch records alone. Code: `lib/agentctl/forge.py`;
tests: `tests/test_forge.py` against `tests/fake_gh.py`.

Not knobs (never in a file): a process's identity (`AGENT_NAME`, `RED_FIXER`, `FIXER_RECORD`, lease secrets), the
store token, the role (`ROLE`, `HOST`), and the tests' hooks.

<!-- agentctl config doc: generated by `agentctl config doc --write`; do not edit by hand -->

| key (`agentctl.toml`) | environment | default | what |
|---|---|---|---|
| `alert_network_grace` | `AGENTCTL_ALERT_NETWORK_GRACE` | `600` | s a NETWORK alert stays before "no NETWORK condition open" closes it |
| `alert_orphan_probe` | `AGENTCTL_ALERT_ORPHAN_PROBE` | `600` | s between git probes of one orphan's worktree |
| `alert_quiet_max` | `AGENTCTL_ALERT_QUIET_MAX` | `86400` | s a READ alert stays its item's live one after its last event |
| `alert_reconcile` | `AGENTCTL_ALERT_RECONCILE` | `60` | s between reconcile passes |
| `alert_red_owner_level` | `AGENTCTL_ALERT_RED_OWNER_LEVEL` | `2` | offer rounds a red goes untaken (nobody holding it, no family owner) before it is the owner's |
| `allow` | `AGENTCTL_ALLOW` | - | extra allowed paths, space-separated |
| `allowed_roots` | `AGENTCTL_ALLOWED_ROOTS` | - | colon-separated roots the allowed-path check accepts besides its own |
| `ask_hold` | `AGENTCTL_ASK_HOLD` | `3600` | s a question its block's probe may answer is held (recorded, not told) |
| `ask_remind_first` | `AGENTCTL_ASK_REMIND_FIRST` | `3600` | the first reminder this long after the owner was told |
| `ask_remind_max` | `AGENTCTL_ASK_REMIND_MAX` | `14400` | ...then doubling up to this |
| `assign_bound` | `AGENTCTL_ASSIGN_BOUND` | `20` | s the red tick on the gate snapshot may run before it is abandoned |
| `attach_poll` | `AGENTCTL_ATTACH_POLL` | `15` | s between a wait's looks at an attached session |
| `author_active_min` | `AGENTCTL_AUTHOR_ACTIVE_MIN` | `15` | live = a pid alive and activity within this |
| `awake_pid` | `AGENTCTL_AWAKE_PID` | - | the process that already holds the keep-awake (a child's inherited copy) |
| `backlog_batch_max` | `AGENTCTL_BACKLOG_BATCH_MAX` | `8` | a backlog batch's members at most: its bisect is 3 prefix checks + 1 confirmation |
| `backlog_batch_min` | `AGENTCTL_BACKLOG_BATCH_MIN` | `2` | queued changes that may share a check and make a backlog batch |
| `baseline` | `AGENTCTL_BASELINE` | `1` | 0: skip the baseline run of a land's dry run |
| `batch_retry_max` | `AGENTCTL_BATCH_RETRY_MAX` | `3` | - |
| `bg_idle_grace` | `AGENTCTL_BG_IDLE_GRACE` | `60` | s a waiting session with no pending task of its own and no verdict stays quiet: its turn ended |
| `bg_poll` | `AGENTCTL_BG_POLL` | `5` | seconds between the wrapper's looks at its session |
| `bg_spent_grace` | `AGENTCTL_BG_SPENT_GRACE` | `120` | s a session must have run before the reaper judges it |
| `bin_repo` | `AGENTCTL_BIN_REPO` | - | the agentctl checkout (default: the one the tool runs from) |
| `blame_max` | `AGENTCTL_BLAME_MAX` | `5400` | s a red may wait for its culprit to be decided before a fresh fixer gets it |
| `blame_probes` | `AGENTCTL_BLAME_PROBES` | `4` | CI probes one red's culprit search may start |
| `blame_probe_wall` | `AGENTCTL_BLAME_PROBE_WALL` | `7200` | s one probe may run before it is no verdict |
| `brief` | `AGENTCTL_BRIEF` | - | the agents' brief file (default ~/bin/briefs/movies-agent-preamble.md) |
| `brief_src` | `AGENTCTL_BRIEF_SRC` | - | a file to use instead of the tracked docs/fixer-brief.md in fixers' prompts (an override): none by default |
| `cache_expire_days` | `AGENTCTL_CACHE_EXPIRE_DAYS` | `7` | - |
| `caffeinate` | `AGENTCTL_CAFFEINATE` | `/usr/bin/caffeinate` | the keep-awake tool; empty: none |
| `change_hold_max` | `AGENTCTL_CHANGE_HOLD_MAX` | `21600` | s |
| `checks_dir` | `AGENTCTL_CHECKS_DIR` | - | the directory of the check slots' files (default checks under the state root) |
| `check_lease_ttl` | `AGENTCTL_CHECK_LEASE_TTL` | `600` | - |
| `check_trees` | `AGENTCTL_CHECK_TREES` | `4` | check trees, so at most this many queued changes are checked at once |
| `claude` | `AGENTCTL_CLAUDE` | - | the `claude` CLI to launch (tests' fake); default: PATH, ~/.local/bin |
| `claude_jobs` | `AGENTCTL_CLAUDE_JOBS` | - | the Claude jobs directory (default ~/.claude/jobs) |
| `claude_projects` | `AGENTCTL_CLAUDE_PROJECTS` | - | the Claude projects directory (default ~/.claude/projects) |
| `claude_settings` | `AGENTCTL_CLAUDE_SETTINGS` | - | the interactive sessions' settings.json (default ~/.claude/settings.json) |
| `claude_tmp` | `AGENTCTL_CLAUDE_TMP` | - | the Claude session's scratch directory |
| `cleanup_item_timeout` | `AGENTCTL_CLEANUP_ITEM_TIMEOUT` | `120` | $AGENTCTL_CLEANUP_ITEM_TIMEOUT |
| `cleanup_remove_reserve` | `AGENTCTL_CLEANUP_REMOVE_RESERVE` | `300` | $AGENTCTL_CLEANUP_REMOVE_RESERVE |
| `clone_keepalive` | `AGENTCTL_CLONE_KEEPALIVE` | `20` | - |
| `clone_lock_wait` | `AGENTCTL_CLONE_LOCK_WAIT` | `300` | the swap waits this long for the clone's users to finish, then tries again next tick |
| `clone_repair_retry` | `AGENTCTL_CLONE_REPAIR_RETRY` | `900` | a repair that failed is tried again after this |
| `clone_timeout` | `AGENTCTL_CLONE_TIMEOUT` | `1800` | - |
| `condition_pending_lapse` | `AGENTCTL_CONDITION_PENDING_LAPSE` | `1800` | s between two sightings after which a pending condition's sighting run starts again |
| `contact_gap` | `AGENTCTL_CONTACT_GAP` | `120` | s of a machine's silence before its leases' clocks pause |
| `contact_pause_max` | `AGENTCTL_CONTACT_PAUSE_MAX` | `43200` | s a silent machine's leases are paused at most |
| `context_fresh` | `AGENTCTL_CONTEXT_FRESH` | `300000` | tokens: a session whose last turn's context is past this is not resumed but started fresh |
| `disk_min_gb` | `AGENTCTL_DISK_MIN_GB` | `50` | the Data volume's free space below which the upkeep runs at once, then WD opens |
| `disk_path` | `AGENTCTL_DISK_PATH` | - | the path whose free space the disk checks read (default: the home directory) |
| `dispatch_dir` | `AGENTCTL_DISPATCH_DIR` | - | the dispatches' directory (default beside the reds') |
| `dispatch_find` | `AGENTCTL_DISPATCH_FIND` | `12` | polls for a just-dispatched run to be listed |
| `dispatch_find_poll` | `AGENTCTL_DISPATCH_FIND_POLL` | `5` | s between those polls |
| `drift_ci` | `AGENTCTL_DRIFT_CI` | - | the drift CI script (default scripts/identity-drift-ci of the repository) |
| `drift_stall` | `AGENTCTL_DRIFT_STALL` | `1800` | s with no output before the drift check is ended (no verdict: the batch goes on) |
| `early_max_runs` | `AGENTCTL_EARLY_MAX_RUNS` | `6` | in-progress runs read per tick, the gate's first |
| `ever_green_recheck` | `AGENTCTL_EVER_GREEN_RECHECK` | `600` | s before a never-green answer is asked of GitHub again |
| `fetch_timeout` | `AGENTCTL_FETCH_TIMEOUT` | `180` | an unattended fetch's bound |
| `fixer_claim_within` | `AGENTCTL_FIXER_CLAIM_WITHIN` | `600` | s a started session has to start its transcript / run `red claim` |
| `fixer_land_wait` | `AGENTCTL_FIXER_LAND_WAIT` | `7200` | s a fixer's land waits for its queued change's verdict |
| `fixer_land_watch` | `AGENTCTL_FIXER_LAND_WATCH` | `60` | - |
| `fixer_unlanded_escalate` | `AGENTCTL_FIXER_UNLANDED_ESCALATE` | `5400` | - |
| `fixer_unlanded_nudge` | `AGENTCTL_FIXER_UNLANDED_NUDGE` | `2700` | - |
| `fix_replay_timeout` | `AGENTCTL_FIX_REPLAY_TIMEOUT` | `1800` | s one side of agentctl's replay may run |
| `free_wait` | `AGENTCTL_FREE_WAIT` | `30` | s --until-done waits for a landed worktree to be free of processes before it keeps it |
| `gate_publish` | `AGENTCTL_GATE_PUBLISH` | `45` | s between the gate line's publishes (a push to main publishes at once) |
| `generated_regen_timeout` | `AGENTCTL_GENERATED_REGEN_TIMEOUT` | `1200` | s a generated file's regen command may run before the conflict is the author's |
| `gh_cli` | `AGENTCTL_GH_CLI` | `gh` | the GitHub CLI |
| `githubstatus_url` | `AGENTCTL_GITHUBSTATUS_URL` | `https://www.githubstatus.com/api/v2/incidents.json` | the GitHub status incidents feed |
| `github_api` | `AGENTCTL_GITHUB_API` | `https://api.github.com` | the GitHub API base |
| `github_lander_repos` | `AGENTCTL_GITHUB_LANDER_REPOS` | `pawelkrupinski/movies` | the repos the lander App writes to, comma-separated |
| `gov_admit_max` | `AGENTCTL_GOV_ADMIT_MAX` | `7200` | a check waits this long for the governor at most |
| `gov_budget_gb` | `AGENTCTL_GOV_BUDGET_GB` | `round(0.6 * ram_gb, 1` | the memory in GB the governor hands out; default 0.6 x the machine's RA |
| `gov_busy_probe` | `AGENTCTL_GOV_BUSY_PROBE` | - | a command printing the busy cores (tests) |
| `gov_cpu_factor` | `AGENTCTL_GOV_CPU_FACTOR` | `1.0` | charged cores (or the load) + a job's own may reach this x the cores |
| `gov_evict_settle` | `AGENTCTL_GOV_EVICT_SETTLE` | `3` | s to wait after evicting a ticket for its memory to free |
| `gov_free_need_gb` | `AGENTCTL_GOV_FREE_NEED_GB` | `2.5` | free memory a new JVM needs NOW (beyond the reserve); its heap grows into the budget's share later |
| `gov_idle_max` | `AGENTCTL_GOV_IDLE_MAX` | `30.0` | s a waiter behind the head waits at most between two reads of the queue |
| `gov_poll` | `AGENTCTL_GOV_POLL` | `2.0` | - |
| `gov_pool_share` | `AGENTCTL_GOV_POOL_SHARE` | `0.5` | of the machine's cores, the widest one pool is granted |
| `gov_ps_probe` | `AGENTCTL_GOV_PS_PROBE` | - | a file of ps `pcpu args` rows (tests) |
| `gov_reserve_gb` | `AGENTCTL_GOV_RESERVE_GB` | `1.0` | - |
| `gov_resume` | `AGENTCTL_GOV_RESUME` | `0.8` | x the cores: under it, admitting again |
| `gov_spare` | `AGENTCTL_GOV_SPARE` | `0.5` | x the cores: under it, spare |
| `gov_starved` | `AGENTCTL_GOV_STARVED` | `1.0` | x the cores: over it, starved |
| `hold_idle_ttl` | `AGENTCTL_HOLD_IDLE_TTL` | `3600` | s |
| `hostname` | `AGENTCTL_HOSTNAME` | - | this host's name (default: the system's) |
| `identity` | `AGENTCTL_IDENTITY` | - | github-app: credentials come from the GitHub Apps (ci-runner) |
| `inbox_dir` | `AGENTCTL_INBOX_DIR` | - | the inbox's directory (default inbox under the state root) |
| `inbox_poll` | `AGENTCTL_INBOX_POLL` | `1.0` | seconds between stat()s of the inbox directory |
| `infra_same_max` | `AGENTCTL_INFRA_SAME_MAX` | `1800` | - |
| `internal_trees_dir` | `AGENTCTL_INTERNAL_TREES_DIR` | - | the internal checkouts' directory (default internal-trees under the state root) |
| `issue_escalate_gap` | `AGENTCTL_ISSUE_ESCALATE_GAP` | `120` | s between two issues' escalations |
| `kick_handoff_wait` | `AGENTCTL_KICK_HANDOFF_WAIT` | `30` | s a kick waits for launchd's new processor to listen before handing it the token |
| `landed_look` | `AGENTCTL_LANDED_LOOK` | `600` | - |
| `lander_landed_every` | `AGENTCTL_LANDER_LANDED_EVERY` | `300` | s between the tick's notes of landed changes' commits on main |
| `land_before_queue_seconds` | `AGENTCTL_LAND_BEFORE_QUEUE_SECONDS` | `1200` | - |
| `land_lease_max` | `AGENTCTL_LAND_LEASE_MAX` | `2700` | the design's 300 s once checks no longer run in the push's hook under the lease (step 6) |
| `land_lease_ttl` | `AGENTCTL_LAND_LEASE_TTL` | `120` | - |
| `land_lock_timeout` | `AGENTCTL_LAND_LOCK_TIMEOUT` | `14400` | - |
| `land_ref_sweep` | `AGENTCTL_LAND_REF_SWEEP` | `600` | - |
| `land_ref_unknown_age` | `AGENTCTL_LAND_REF_UNKNOWN_AGE` | `86400` | s: a ref of a change neither the entries nor the store know is deleted only past this |
| `land_stall_min` | `AGENTCTL_LAND_STALL_MIN` | `20` | - |
| `land_wait_report_min` | `AGENTCTL_LAND_WAIT_REPORT_MIN` | `10` | - |
| `laptop_free_pct` | `AGENTCTL_LAPTOP_FREE_PCT` | `20` | holds other work while macOS's memory free % is under this |
| `laptop_load` | `AGENTCTL_LAPTOP_LOAD` | `2.0` | holds other work while the 1-min load is over this x the cores |
| `laptop_pressure_level` | `AGENTCTL_LAPTOP_PRESSURE_LEVEL` | `2` | holds other work at this kernel pressure level or over (1 normal, 2 warn, 4 critical) |
| `laptop_role_ttl` | `AGENTCTL_LAPTOP_ROLE_TTL` | `600` | s a silent holder keeps the role |
| `laptop_swap_growth_mb` | `AGENTCTL_LAPTOP_SWAP_GROWTH_MB` | `1024` | holds other work while swap grew this much (MB) within LAPTOP_SWAP_WINDOW |
| `laptop_swap_window` | `AGENTCTL_LAPTOP_SWAP_WINDOW` | `600` | seconds of swap samples the growth is read over |
| `launch_agents` | `AGENTCTL_LAUNCH_AGENTS` | - | the launchd agents' directory (default ~/Library/LaunchAgents) |
| `live_globs` | `AGENTCTL_LIVE_GLOBS` | - | the live sessions' transcript globs (default: the Claude projects') |
| `live_poll` | `AGENTCTL_LIVE_POLL` | `1.0` | s between looks at a quiet file |
| `log_rotate_keep` | `AGENTCTL_LOG_ROTATE_KEEP` | `3` | rotated parts kept per log (<log>.1 newest .. <log>.N oldest) |
| `log_rotate_max` | `AGENTCTL_LOG_ROTATE_MAX` | `10485760` | bytes over which an agentctl log is rotated (copy-truncate; its newest part kept as .1) |
| `machine` | `AGENTCTL_MACHINE` | - | - |
| `machine_id` | `AGENTCTL_MACHINE_ID` | - | - |
| `max_checks` | `AGENTCTL_MAX_CHECKS` | - | the check slots' cap, overriding the budget's |
| `max_foreign` | `AGENTCTL_MAX_FOREIGN` | `2` | files a push may revert to an older main before prepush-check refuses |
| `min_interval` | `AGENTCTL_MIN_INTERVAL` | `60` | s the least poll interval of `agentctl wait` |
| `movies_remote` | `AGENTCTL_MOVIES_REMOTE` | - | the movies repository's clone URL the server's clone fetches from (default: the clone's own) |
| `movies_repo` | `AGENTCTL_MOVIES_REPO` | - | the movies checkout (default ~/projects/movies) |
| `note_precheck_wait` | `AGENTCTL_NOTE_PRECHECK_WAIT` | `110` | s a precheck's note waits at most for the store or its item (the delays above, cut at this) |
| `notifier` | `AGENTCTL_NOTIFIER` | - | the notification tool (default: terminal-notifier on PATH, else osascript) |
| `notify` | `AGENTCTL_NOTIFY` | - | a command run to notify the owner (set: used instead of the notifier) |
| `notify_timeout` | `AGENTCTL_NOTIFY_TIMEOUT` | `30` | s, each channel: an overloaded laptop's osascript cold start takes 10 s and more |
| `orphan_fixers` | `AGENTCTL_ORPHAN_FIXERS` | `2` | orphan fixers alive at once ; one dispatched a pass |
| `orphan_idle` | `AGENTCTL_ORPHAN_IDLE` | `7200` | - |
| `owners_dir` | `AGENTCTL_OWNERS_DIR` | - | the owners' directory (default owners under the state root) |
| `owner_active` | `AGENTCTL_OWNER_ACTIVE` | `7200` | - |
| `owner_idle` | `AGENTCTL_OWNER_IDLE` | `21600` | no red held this long: the owner ends |
| `park_digest_days` | `AGENTCTL_PARK_DIGEST_DAYS` | `14` | - |
| `pinned_recording` | `AGENTCTL_PINNED_RECORDING` | - | the pinned recording's id the identity checks use |
| `push_batch_min` | `AGENTCTL_PUSH_BATCH_MIN` | `2` | changes owing a verify round that make a push batch |
| `push_stall_secs` | `AGENTCTL_PUSH_STALL_SECS` | `180` | - |
| `push_wall_min` | `AGENTCTL_PUSH_WALL_MIN` | `45` | - |
| `queue` | `AGENTCTL_QUEUE` | `1` | 0: no batching queue |
| `queue_stall_after` | `AGENTCTL_QUEUE_STALL_AFTER` | `3600` | s with open changes and no change moving before QUEUE STALLED opens (queuestall.py) |
| `recovery_dir` | `AGENTCTL_RECOVERY_DIR` | - | the recovery notes' directory |
| `recover_max` | `AGENTCTL_RECOVER_MAX` | `600` | a probe is re-run at most this far apart, however often it failed |
| `reds_dir` | `AGENTCTL_REDS_DIR` | - | the reds' directory (default reds under the state root) |
| `red_crash_block_max` | `AGENTCTL_RED_CRASH_BLOCK_MAX` | `86400` | $AGENTCTL_RED_CRASH_BLOCK_MAX |
| `red_crash_loop` | `AGENTCTL_RED_CRASH_LOOP` | `3` | $AGENTCTL_RED_CRASH_LOOP |
| `red_decided_revert_max` | `AGENTCTL_RED_DECIDED_REVERT_MAX` | `2700` | - |
| `red_decision_block_max` | `AGENTCTL_RED_DECISION_BLOCK_MAX` | `604800` | an owner-decision block lapses after this: the red offered again (2026-11-03: was |
| `red_families` | `AGENTCTL_RED_FAMILIES` | - | the red families' file (default red-families.json beside the tool) |
| `red_fixers` | `AGENTCTL_RED_FIXERS` | `5` | live fixers at once, every family together |
| `red_fixer_wall` | `AGENTCTL_RED_FIXER_WALL` | `10800` | a dispatched fixer's maximum wall time |
| `red_fix_decide` | `AGENTCTL_RED_FIX_DECIDE` | `10800` | - |
| `red_fix_giveup` | `AGENTCTL_RED_FIX_GIVEUP` | `21600` | - |
| `red_fix_next_run` | `AGENTCTL_RED_FIX_NEXT_RUN` | `108000` | s an outside red's landed fix waits unproven for its workflow's next run |
| `red_fix_requeue_grace` | `AGENTCTL_RED_FIX_REQUEUE_GRACE` | `900` | $AGENTCTL_RED_FIX_REQUEUE_GRACE |
| `red_held_idle` | `AGENTCTL_RED_HELD_IDLE` | `2700` | s a red in its owner's hand may go with no progress of its own before it is released |
| `red_join_idle` | `AGENTCTL_RED_JOIN_IDLE` | `1800` | s a red joined to its family holder's lease may go with no progress on it before it is released |
| `red_liveness_max` | `AGENTCTL_RED_LIVENESS_MAX` | `3600` | the gate red this long with a red fix stalled: the status line says so |
| `red_main_verify_every` | `AGENTCTL_RED_MAIN_VERIFY_EVERY` | `10800` | - |
| `red_old_run` | `AGENTCTL_RED_OLD_RUN` | `172800` | an outside red whose newest run is older than this is checked by a fresh run before it is fixed |
| `red_orphan_grace` | `AGENTCTL_RED_ORPHAN_GRACE` | `300` | an ended session's turn still held this long is orphaned |
| `red_others_every` | `AGENTCTL_RED_OTHERS_EVERY` | `60` | seconds between the other watched repos' red ticks: one gate read each |
| `red_renew` | `AGENTCTL_RED_RENEW` | `60` | a fixer's wrapper renews its lease this often |
| `red_repo` | `AGENTCTL_RED_REPO` | - | the one repository reds are read for (default: the movies repository) |
| `red_repos` | `AGENTCTL_RED_REPOS` | - | the red repositories' file (default red-repos.json beside the tool) |
| `red_skip_say` | `AGENTCTL_RED_SKIP_SAY` | `600` | s between the lines of another repo whose tick keeps failing for the same reason |
| `red_stale_days` | `AGENTCTL_RED_STALE_DAYS` | `14` | $AGENTCTL_RED_STALE_DAYS |
| `red_stall` | `AGENTCTL_RED_STALL` | `1800` | a turn with no progress (transcript, worktree) this long is stalled: stopped |
| `red_starve` | `AGENTCTL_RED_STARVE` | `2700` | an offer waiting this long for a slot is STARVED |
| `red_tick` | `AGENTCTL_RED_TICK` | `15` | seconds between the processor's red ticks |
| `red_verify_slack` | `AGENTCTL_RED_VERIFY_SLACK` | `7200` | s past a slot's expected end before its fix is called unverified |
| `regen_recovery_wait` | `AGENTCTL_REGEN_RECOVERY_WAIT` | `7200` | s from the re-run to the owner's one escalation |
| `remote` | `AGENTCTL_REMOTE` | - | the push and fetch remote of every clone |
| `remote_ghost` | `AGENTCTL_REMOTE_GHOST` | `600` | s |
| `restart_check` | `AGENTCTL_RESTART_CHECK` | `30` | seconds between code-version checks |
| `runners_page_cache` | `AGENTCTL_RUNNERS_PAGE_CACHE` | - | the runners page's last good copy (default ~/.cache/ci-runners-page/last-good.json) |
| `sbt_idle_min` | `AGENTCTL_SBT_IDLE_MIN` | `120` | min a detached sbt server with no client may sit idle before the reaper ends it |
| `sbt_idle_pressure_min` | `AGENTCTL_SBT_IDLE_PRESSURE_MIN` | `30` | that limit while swap free is under the floor |
| `sbt_progress_cpu_per_min` | `AGENTCTL_SBT_PROGRESS_CPU_PER_MIN` | `1.0` | CPU s/min since the previous pass above which an sbt server counts as working |
| `sbt_reap` | `AGENTCTL_SBT_REAP` | - | 0: never reap this machine's sbt servers (tests) |
| `sbt_stall_min` | `AGENTCTL_SBT_STALL_MIN` | `20` | - |
| `scratch` | `AGENTCTL_SCRATCH` | - | the scratch directory (default: the Claude session's, else the state root) |
| `sleep_log` | `AGENTCTL_SLEEP_LOG` | - | a file standing for `pmset -g log` (tests) |
| `sleep_min` | `AGENTCTL_SLEEP_MIN` | `120` | s of sleep inside a lapse's window that count as "the host was asleep" |
| `sock_dir` | `AGENTCTL_SOCK_DIR` | - | the token hand-off sockets' directory (default /tmp/agentctl-<uid>) |
| `stall_cpu_seconds` | `AGENTCTL_STALL_CPU_SECONDS` | `1.0` | CPU that counts as progress per window: a watchdog's polls are not work |
| `state_file` | `AGENTCTL_STATE_FILE` | - | the CI state file (default state.json under the state root) |
| `state_root` | `AGENTCTL_STATE_ROOT` | - | the state root (default ~/.cache/agentctl) |
| `stats_every` | `AGENTCTL_STATS_EVERY` | `60` | s between passes |
| `stats_pass_bound` | `AGENTCTL_STATS_PASS_BOUND` | `40` | s a pass works at most (a tick step) |
| `stats_per_pass` | `AGENTCTL_STATS_PER_PASS` | `150` | records a pass sends at most (the backfill spreads over passes) |
| `stats_settle` | `AGENTCTL_STATS_SETTLE` | `900` | s after an exit before its record is built: the collector exports a run <= 5 min after it ends |
| `stats_url` | `AGENTCTL_STATS_URL` | - | the stats collector's URL |
| `status_agent_bound` | `AGENTCTL_STATUS_AGENT_BOUND` | `4` | s the page waits for one agent's details |
| `status_bound` | `AGENTCTL_STATUS_BOUND` | `8` | s a source may take ; the agents' source twice that |
| `status_page` | `AGENTCTL_STATUS_PAGE` | - | 0, off or no: the status page is not served |
| `status_port` | `AGENTCTL_STATUS_PORT` | - | the status page's port (default: the first free of its range) |
| `step_bound` | `AGENTCTL_STEP_BOUND` | `120` | s one tick step may run before it is abandoned (the red tick's steps and the sync steps) |
| `step_join` | `AGENTCTL_STEP_JOIN` | `20` | - |
| `store_retries` | `AGENTCTL_STORE_RETRIES` | `4` | - |
| `store_retry_base` | `AGENTCTL_STORE_RETRY_BASE` | `0.5` | s; doubled each attempt: 0.5 + 1 + 2 + 4 = 7.5 s at most |
| `store_secret` | `AGENTCTL_STORE_SECRET` | - | the store's shared secret (default: the built-in one) |
| `store_url` | `AGENTCTL_STORE_URL` | - | the remote store's URL (set: every process uses it) |
| `streak_rereads` | `AGENTCTL_STREAK_REREADS` | `3` | stale or missing streak run signatures read again per tick |
| `suite_cap` | `AGENTCTL_SUITE_CAP` | `2` | agentctl's own test-suite runs at once : more agents queue, never pile up |
| `swap_free_floor_mb` | `AGENTCTL_SWAP_FREE_FLOOR_MB` | `2048` | swap free (MB) below which the shorter idle limit applies |
| `sweep_hour` | `AGENTCTL_SWEEP_HOUR` | `9` | local time: the daily sweep runs at the first maintain pass from 09:00 |
| `sync_mirror` | `AGENTCTL_SYNC_MIRROR` | `10` | s between the laptop's copies of /v1/state into state.json |
| `tick` | `AGENTCTL_TICK` | `5` | seconds between ticks ; every step is bounded or rate-limited inside |
| `token_summary` | `AGENTCTL_TOKEN_SUMMARY` | `600` | s between the summaries of a still-missing token (said on its transition first) |
| `tunnel_state` | `AGENTCTL_TUNNEL_STATE` | - | the tunnel's health file (default tunnel-health.json under the state root) |
| `unheld_grace` | `AGENTCTL_UNHELD_GRACE` | `600` | s |
| `unkicked_after` | `AGENTCTL_UNKICKED_AFTER` | `3600` | a convergence-relevant main push with no convergence (or Identity takes) run on main by then is a red |
| `unknown_grace` | `AGENTCTL_UNKNOWN_GRACE` | `300` | s an UNKNOWN condition must keep failing (seen again past this) before it opens |
| `until_done_deadline` | `AGENTCTL_UNTIL_DONE_DEADLINE` | `14400` | s (--deadline): a batch at green, a verify round and a push fit well inside it |
| `upkeep_every` | `AGENTCTL_UPKEEP_EVERY` | `3600` | - |
| `w1_max` | `AGENTCTL_W1_MAX` | `7200` | the oldest unlanded change ; while main is red, counted from the red's start |
| `w2_max` | `AGENTCTL_W2_MAX` | `600` | a gate red with nobody on it |
| `w5_max` | `AGENTCTL_W5_MAX` | `300` | the processor's duties beat (its bounded steps take up to 2 min) |
| `w6_max` | `AGENTCTL_W6_MAX` | `300` | a due escalation not notified |
| `w7_max` | `AGENTCTL_W7_MAX` | `300` | phase 2: an event ci-runner wrote that the laptop's sync has not projected (60x its 5 s tick) |
| `w8_window` | `AGENTCTL_W8_WINDOW` | `3600` | W8 looks at the change items that closed within this |
| `waiting_green_ticks` | `AGENTCTL_WAITING_GREEN_TICKS` | `100` | ticks (~10 min) changes may wait at a green gate with no batch moving: then STALLED |
| `waiting_timeout` | `AGENTCTL_WAITING_TIMEOUT` | `86400` | - |
| `watchdog_misses` | `AGENTCTL_WATCHDOG_MISSES` | `2` | consecutive passes a self-healing invariant (W5, WM, W7) fails before its condition opens |
| `watchdog_retell` | `AGENTCTL_WATCHDOG_RETELL` | `21600` | s |
| `watchdog_sleep_gap` | `AGENTCTL_WATCHDOG_SLEEP_GAP` | `180` | s since the watchdog's own last pass (launchd: every 60 s) that mean the laptop slept |
| `watch_conv_start` | `AGENTCTL_WATCH_CONV_START` | `3600` | no convergence (or takes) run containing the sha started by then: said, not waited for |
| `watch_error_poll` | `AGENTCTL_WATCH_ERROR_POLL` | `30` | s of the first retry delay after the watch could not read the runs |
| `watch_follow_lease` | `AGENTCTL_WATCH_FOLLOW_LEASE` | `600` | s a follower's claim on a sha's watch item lasts unrenewed; then the tick (or another follower) takes it |
| `watch_poll` | `AGENTCTL_WATCH_POLL` | `60` | - |
| `watch_stall_min` | `AGENTCTL_WATCH_STALL_MIN` | `120` | a watch with no change in its runs for this long ends WATCH STALLED |
| `watch_timeout` | `AGENTCTL_WATCH_TIMEOUT` | `10800` | either run undecided by then: exit 2, could not tell |
| `wa_read_every` | `AGENTCTL_WA_READ_EVERY` | `300` | s a gh list is kept |
| `wa_red_max` | `AGENTCTL_WA_RED_MAX` | `1800` | s agentctl's tests.yml may stay red on main |
| `wa_undecided_max` | `AGENTCTL_WA_UNDECIDED_MAX` | `3600` | s main's head may go without a decided run |
| `wa_unpinned_max` | `AGENTCTL_WA_UNPINNED_MAX` | `900` | s the newest green run may stay unpinned |
| `wb_max` | `AGENTCTL_WB_MAX` | `600` | - |
| `wf_pct` | `AGENTCTL_WF_PCT` | `70` | % of kern.maxfiles open system-wide past which WF is violated |
| `wm_max` | `AGENTCTL_WM_MAX` | `2700` | maintain's last pass (launchd every 15 min) |
| `wp_rss_by_name` | `AGENTCTL_WP_RSS_BY_NAME` | `idea=8` | comma list of name=GB: the RSS limit of the process of that name, others WP_RSS_GB (IntelliJ's JVM sits at 4-5 GB) |
| `wr_max` | `AGENTCTL_WR_MAX` | `2400` | a red fix still unlanded this long after it was queued ($AGENTCTL_WR_MAX; a CI-tier check ~17 min) |
| `x` | `AGENTCTL_X` | `DEFAULT` | The knobs are what the code reads: each `env_num("AGENTCTL_X", DEFAULT)` and `os.environ.get("AGENTCTL_X", DEFAULT)` |

<!-- end of the generated config reference -->

## Troubleshooting

- **`vault <ns> locked`.** Pass the line to the lead: the user runs `! secrets unlock <ns>`. Don't retry in a loop,
  and never try to unlock.
- **Tunnel down** (`Connection refused` on localhost:3014, the store unreachable). Run `agentctl env --check`. The fix
  is `launchctl kickstart -k gui/$(id -u)/net.bitcashier.forgejo-tunnel` (the launchd agent kept its name; it carries
  only the store now). The tunnel restarts itself under launchd, so wait a few seconds and land again.
  Its source is `~/bin/launchd/net.bitcashier.forgejo-tunnel.plist` (installed as a copy; 8192 open files, not
  launchd's 256: a lossy uplink strands an fd per abandoned request). On a lossy uplink (any lost ping of 10 to
  1.1.1.1, e.g. a phone hotspot) the processor never kicks it -- a restart only drops what is in flight -- and the
  status line says NETWORK until a clean sample.
- **`STORE UNREACHABLE` from `status` / `land status`, or WU.** Both commands bound each store call (15 s, never
  retried) and name the store and the tunnel instead of hanging. A tunnel that listens but does not answer (WU) is a
  wedged forward or a saturated uplink: a second forward (`ssh -O forward -L 13014:10.1.0.3:3014 ci-runner`, then
  `curl localhost:13014/` -- a 401 is an answer) tells which; a wedged one is kickstarted as above. The readers of
  open items ask the store for the open states only (`open_items`), never every decided change (4 MB on 2026-11-26).
- **`version skew` (StoreLacksCall).** ci-runner's store is pinned behind the laptop's ~/bin (agentctl's tests.yml
  red) and lacks a call the client makes (a 404 naming it). The call is not sent again for 10 min
  (`store-lacking.json` under the state root); what it carried (the offline event log) stays for a store that has it.
  It clears itself once the pin moves.
- **gh / GitHub / git timeouts on a lossy uplink.** Every forge read (gh_json, api GETs), the land/ ref push and the
  push's proving fetch go through `net_retry` / `net_run` (lib/agentctl/classify.py, beside the network's signature table): a failure in the network's words
  (TRANSIENT_NETWORK) or a timeout is tried again 2-20 s apart within NET_RETRY_BUDGET (120 s), then raises
  TransientNetwork -- infra, never a red or a NOT LANDED (a push it cannot prove prints `UNKNOWN:`, exit 2). A
  mutation (a dispatch, a cancel) is never retried. A check that failed only reaching the network (no test runner's
  failure line) is an infra re-queue. 2026-11-22's loss was the laptop's uplink, not the fleet: a path-MTU black hole
  at ~1408 bytes behind a 1500 MTU (DF pings of 1380 pass, 1390 vanish) and 37-350 ms jitter to its own gateway;
  ci-runner itself saw 0% loss. `ping -D -s 1380` / `-s 1390` to the jump host tells whether it is back.
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
- **The gate reads UNREAD and nothing lands.** The processor lost its store token (a reboot, a crash's restart, a
  reinstall: see "The token hand-off"). The next agentctl command that reads the vault hands it over; by hand:
  `cd ~/projects/movies && AGENTCTL_INTERACTIVE=1 ~/bin/agentctl land status` with the vault unlocked.
- **The status line says `RECONCILER not running`.** `state.json` is stale: `agentctl tick service status`, the
  service log `~/.cache/agentctl/waiting/<repo>/service.log`, and `agentctl watchdog` (W5).
- **Where is the fixer for red #N?** `claude agents` (by name), `agentctl red status`, `agentctl lease list red/`.
- **WB: uncommitted edits in ~/bin.** Someone edited ~/bin's own checkout. Agentctl changes go in a worktree
  (`git -C ~/bin worktree add ~/bin-wt-<name> -b <name> master`), landed by rebase + `git merge --ff-only`; the
  processor only ever re-execs onto committed code.
