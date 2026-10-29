# Landing and red handling: a level-triggered reconciler with leases

Status: design, approved in outline by the owner on 2026-10-28. Nothing here is implemented yet.
Scope: movies landing (`agentctl land`, the queue and checkers, the `waiting` batch) and red handling (claims, the
patrol, dispatch, owner blocks, the recovery registry, inbox escalation) in `~/bin/agentctl`, plus where they run.

## 1. Why

Today's machinery is edge-triggered and process-bound. A process does something, remembers it, and is
trusted to come back. Every fix since 2026-10-26 has patched one way that trust fails: heartbeats on
claims, a recovery registry, a watchdog that re-execs the processor, orphan duties, an escalation notifier.
Each patch adds a new store and a new loop. On 2026-10-28 alone, these failures were all staleness or
stuckness:

| # | Incident (2026-10-28) |
|---|---|
| I1 | A dead worktree counted as a "committed" fixer for an hour. |
| I2 | A stale owner block was re-created by the patrol. |
| I3 | A push hung on a dropped ssh tunnel. It held the landing lock for 30 min and blocked a checked batch. |
| I4 | Claims were kept per run, not per red. |
| I5 | The daily dispatch budget produced NOBODY. |
| I6 | Escalations were never notified. |
| I7 | The gate showed UNREAD while the laptop's network was lossy. |
| I8 | The status line lagged behind a 60 s "cached" read. |
| I9 | Orphaned worktrees were never landed. |
| I10 | Tests polluted the real cache. |
| I11 | Retry loops had no bound. |

The 15-crack audit (agent a38c66d975b337219) and its second reviewer (a47edb6ec7a468443) found the same
pattern in code: a `while True` with no cap, a wait with no bound, a claim that a live pid keeps alive
forever, a record that a racing writer overwrites, a verdict that nobody reads.

The owner approved replacing the ad-hoc fixes with four mechanisms:

1. a level-triggered reconciler;
2. leases;
3. per-item state machines with deadlines;
4. an invariant watchdog.

These run first on the laptop (phase 1) and then on ci-runner (phase 2).

## 2. Principles

1. **Level-triggered.** Each tick reads actual state from the sources of truth: Forgejo runs, git refs,
   worktrees, leases and items. It compares that with the desired invariants (§4) and takes the next small,
   idempotent step per item. If a tick is lost, the next one does the same thing. The code never assumes
   "I started X, so X is running".
2. **Nothing blocks.** No step waits on anything slower than a bounded subprocess: at most 120 s, typically
   seconds. Long work (checks, fixers, CI runs) is done by executors that hold leases. The tick only
   observes their leases and results.
3. **Leases are the only way anything is held.** A lease is a holder id plus a TTL. It is renewed only on
   real progress, and expiry is automatic. Nothing is held by a lock, a claim file, a live pid or a
   worktree's existence.
4. **Every state has a deadline and an automatic exit.** That exit is a retry, a re-offer or an escalation.
   This is asserted by a table-driven test. An escalation always notifies someone and never parks a code
   red on the owner.
5. **No state lives only in a process.** Item state, leases and events are in one durable store. A process
   that dies loses nothing: its leases expire and its items are re-derived.
6. **One writer per fact, compare-and-set everywhere.** Items carry a version. Leases carry an epoch (a
   fencing token) and a secret. Git ref updates name their expected old value. A racing writer loses
   cleanly; it never overwrites.
7. **No flags.** The migration (§13) is a sequence of replace-and-delete commits, each reversible by
   `git revert`. There are no feature switches and no dual paths.

## 3. Components

```
PHASE 1: everything on the laptop

 ┌───────────────┐      ┌─────────────────────────────┐
 │ agentctl land │─────▶│ store (sqlite)              │
 │ red claim ... │      │ reconciler tick (processor) │
 └───────────────┘      │ dispatcher (fixers)         │
 ┌───────────────┐      │ watchdog (own launchd job)  │
 │ status line   │◀─────│ state snapshot, inbox       │
 └───────────────┘      └─────────────────────────────┘

PHASE 2: the core on ci-runner

 ci-runner
 ┌──────────────────────────────────────┐
 │ agentctl-store (sqlite + HTTP :3014) │
 │ agentctl-reconciler (ci-lander)      │
 │ metrics → Prometheus → Slack         │
 └──────────────────▲───────────────────┘
                    │ tunnel :3014
 laptop             │
 ┌──────────────────┴───────────────────┐
 │ sync: submit, events → inbox,        │
 │       state mirror                   │
 │ dispatcher (fixers), watchdog        │
 └──────────────────────────────────────┘
```

- **Store.** One sqlite database holds the `leases`, `items`, `events`, `offers` and `meta` tables.
  - Phase 1: `$AGENTCTL_STATE_ROOT/store.db` in WAL mode, used in-process through `class Store`.
    Concurrent writers use `BEGIN IMMEDIATE`.
  - Phase 2: the same schema and code behind a small HTTP API on ci-runner (§6.3). The laptop uses
    `class RemoteStore`, which has the same methods.
- **Reconciler.** `tick(store, sources, clock)` is one loop. It is the only writer of item state.
  - Phase 1: it replaces the body of the `waiting` processor (launchd `net.bitcashier.agentctl-waiting`;
    the label is kept).
  - Phase 2: a systemd service on ci-runner.
  - It runs every 15 s, and at once on a nudge (a submission, a Forgejo webhook, a lease release).
- **Executors.** These are processes that take leases and do the long work:
  - **check executor:** a local check in a check tree under the governor, or a CI run on the check tier;
  - **fixer dispatcher:** takes "red needs fixer" offers and spawns `claude -p` fixers;
  - **pusher:** a bounded step inside the tick, holding the landing lease.
- **Watchdog.** A separate, dumb, cheap process (§10). It reads the store and the tick's heartbeat, and
  never shares code paths with the tick beyond the read-only store accessor.
- **Laptop sync (phase 2).** A thin loop on the laptop that does four things:
  - submits `land/<entry>` branches;
  - pulls the event feed into the existing inbox;
  - mirrors `/state` for the status line and `ci-why`;
  - reports worktree facts (orphans, claim activity).

## 4. Desired invariants (what each tick converges toward)

For each repo (only movies is gated today):

1. Every submitted change is in a state of its machine (§5.1), within its deadline. If it is not
   terminal, a live lease or a live CI run stands behind it.
2. The `waiting` branch equals forgejo/main plus the checked waiting entries, in batch order. Otherwise it
   is rebuilt.
3. Every red signature on main has exactly one of the following, within the state's deadline:
   - a live fixer lease;
   - a fix-pending land item;
   - a live owner-block lease;
   - an open offer.
4. No lease is past its TTL. Expired leases are reaped, and their items re-offered, within one tick.
5. Every `check/*` branch, `land/*` branch and check CI run belongs to a live item. Otherwise it is
   cancelled or deleted. That deletion is within the standing OK: these are the reconciler's own refs.
6. Every event with `severity=escalate` is delivered to a notifier within 5 min.
7. The published snapshot is at most one tick old.

## 5. State machines

Notation:

- **Deadline:** the time after entering the state, or a lease's expiry.
- **On deadline:** the automatic exit.
- **Escalate:** emit an event with `severity=escalate`, raise `level`, set a new deadline. It is a
  self-transition that always notifies.

Every terminal state also has a deadline: the record is archived after 14 days, keeping its events. This
lets the test assert "every state has a deadline and an exit" uniformly.

### 5.1 Change (a submitted land entry)

| State | Means (observed facts) | Deadline | On deadline (automatic exit) | Other exits |
|---|---|---|---|---|
| `queued` | Submitted (`land/<entry>` ref, or an entry file in phase 1). No check lease or check run for its current (tip, base). | 15 min | Route the check to the other tier: laptop → CI check tier. At 45 min: escalate `STALLED: queued, waiting on <cap / executor / gate>`, then every 60 min. | A check lease is taken or a run starts → `checking`. A newer entry from the same worktree and branch → `superseded`. `land cancel` → `cancelled`. The commits are already patch-equivalent on main → `landed`. |
| `checking` | A check lease is held (local), or a precommit-check run for (tip, base) is undecided (CI). | The lease TTL (10 min, renewed on progress); max hold `max(2×p90, 30 min)`. A CI run: 15 min to start, 2 h to decide. | → `queued` with `infra_attempts+1` (lease lost, run cancelled, executor dead). At `infra_attempts ≥ 3`: still → `queued` with backoff 10→60 min, plus an escalation each cycle. Infra never rejects a change. | Pass → `checked`. A test failure (the run's conclusion is failure on a non-setup step, per `classify_job`) → `rejected`. Conflict on the rebase → `rejected`. |
| `checked` | A pass is recorded for (tip tree, base). The gate decides what happens next. | 5 min while the gate is unread | Escalate a `gate unread` condition (§5.4). Stay `checked`; re-evaluated every tick. | Gate green, or the change may land while red (tools-only or `Fixes-Red` for a red run) → `pushing`. Gate red and the change must wait → `waiting`. Main moved with overlap (the existing `recheck_reason`) → `queued` (a new round, not an attempt). |
| `waiting` | The entry's commits are on `waiting` (a cherry-pick of the checked change). | 6 h | Escalate `waiting 6 h behind red <sig>`, then every 6 h. This is the oldest-unlanded invariant's per-item twin. | The batch (§5.2) lands it → `landed`. The batch confirms it as culprit → `rejected`. It no longer applies to the new main → `rejected (conflict)`. |
| `pushing` | The reconciler holds the landing lease `land/<repo>`, with this entry named in the lease meta. | Lease max 300 s (TTL 120 s). The push subprocess runs under a hard 120 s timeout. | Verify: if the tip (or its patch-equivalent) is on forgejo/main → `landed`. Otherwise → `checked` with `push_attempts+1`. At ≥ 3: escalate a `push failing` condition (probe: `git ls-remote` plus the tunnel probe), with backoff. Never `rejected`. | Race (non-fast-forward) → `checked` (not an attempt). Red-gate refusal → `checked`. |
| `landed` | Patch-equivalent on main. | Watch: 3 h for main's Main run and convergence run containing the sha. | Undecided runs past the deadline become a `stuck run` red (§5.3). Archive after 14 d. | Each watch verdict is an event: `WATCH OK`, or `WATCH RED` (which creates or attaches a red, with the change's author offered first). |
| `rejected` | A test failure, a conflict, a confirmed batch culprit, or a squash-guard refusal. | 30 min for its author to re-submit (supersede) or `land abandon` | Create a `rescue` red item (§5.3, kind `rejected-change`) carrying the change's picture. Its author's session no longer owns it. Archive after 14 d. | A re-submission from the same worktree supersedes the rescue. |
| `superseded`, `cancelled` | Terminal. | 14 d | Archive. | none |

Items are keyed by the entry id. Attempts are counted per entry (patch-id set), never per rebased HEAD
(audit S3). Attempts are recorded only on outcome events, never on process starts (audit #11).

### 5.2 Batch (the `waiting` branch at green)

One per repo. A batch exists only while `waiting` entries exist.

| State | Means | Deadline | On deadline | Other exits |
|---|---|---|---|---|
| `blocked-red` | Main is red, and no `Fixes-Red` entry for the red waits. | Tied to the red's own machine. Re-evaluated every tick; 6 h. | Escalate, through the reds' invariant: a red main with entries waiting > 6 h. | Gate green, or a fix entry arrives → `assembling`. |
| `assembling` | The reconciler holds `land/<repo>`, rebuilds `waiting` on forgejo/main and takes the batch. | Lease max 300 s | `attempts+1` → `assembling` again. At 3: → `held`. | Done → `checking`. Nothing left → no batch. |
| `checking` | A fast-check lease or CI run on the batch tip. | As for the change's `checking` | Infra retry once on a fresh server or the CI tier, then → `held`. | Pass → `pushing`. Fail → `bisecting`. |
| `bisecting` | Prefix checks, one lease or run each. | As for each check | As above | A culprit fails twice, on its own prefix and again as a confirmation (audit #10) → that entry `rejected`, the passing prefix → `pushing`, the rest → `assembling`. Every prefix passes → `assembling` once more; the second time → `held`, plus a flake red for a determinism fix. |
| `pushing` | As for the change's `pushing` | 300 s | As for the change | → `landed` |
| `held` | A hold lease `hold/<repo>/waiting` (holder `hold:<reason>`, TTL 30 min, never renewed). | Lease expiry | → `assembling`, with attempts reset. A second hold for the same tip escalates. | `waiting resume` releases the hold lease. |
| `landed` | All entries landed. | 14 d | Archive. | none |

Retries are counted per batch tip in the item (audit #5). There is no `"retry"` return and no `nap(5)`
loop.

### 5.3 Red (keyed by signature)

A red is keyed by its failure signature, as today: workflow + failing job + normalized first error line. A
newer failing run with the same signature attaches to the same item (I4).

Kinds:
- `gate`: Main or convergence;
- `outside`: any other workflow on main, read per workflow file and never from a 200-run window
  (audit #7);
- `stuck-run`: a run undecided for more than 3× its workflow's p90 (audit #8);
- `local`: main's own local-checks red;
- `flake`: a determinism fix;
- `rescue`: a rejected change whose author left (audit #1).

| State | Means | Deadline | On deadline | Other exits |
|---|---|---|---|---|
| `red` | Observed failing. Nothing yet. | 1 tick | → `offered`: the author tier when the commits since the last green run map to a live author session (§7.3), else the fresh tier; infra-shaped → the infra tier. | Signature gone (green run) → `cleared`. |
| `offered` | An open offer `fixer/<sig>`, with tier and picture. | Gate 10 min, others 30 min. Author tier: 10 min. | Escalate: `RED <wf> #n: no taker for <d> (<reason: capacity N/N, no dispatcher seen since HH:MM>)`. Widen the author tier to fresh. Re-deadline 30 min. Repeat. NOBODY is never a resting state: it is `offered` past its deadline, and it notifies every cycle (I5). | Taken → `leased`. A fix entry submitted → `fix-pending`. `red block` with an owner cause → `blocked-external`. Signature gone → `cleared`. |
| `leased` | Lease `red/<sig>` is held by a fixer or an interactive claimant. | TTL 15 min, renewed on progress only (§6.2); max hold 3 h (interactive: 8 h). | → `offered` with `attempts+1`. The partial picture (the holder's notes, last commits) goes into the next offer. Lease lost ⇒ the dispatcher stops that fixer's process group (§7.2). | The holder submits a land entry with `Fixes-Red` → `fix-pending` (lease released by the holder or by the tick). Holder releases with `GAVE UP` / `DIAGNOSED` → `offered`, with the next tier and the diagnosis in the picture. |
| `fix-pending` | A change item whose `fixes` name this red is non-terminal, or landed but not yet verified. | Follows the change's own deadlines. After `landed`: the verifying run must exist within 30 min (else it is dispatched, via `workflow_dispatch`, for outside reds) and decide within 3 h. | No run → dispatch it. Undecided past 3 h → a `stuck-run` red is opened, and this item escalates. Unproven past 6 h, a refused verify dispatch, or no fix at all (fix None) → `offered`, escalated, the fix set aside (`unverified`): never a resting state. | The change is `rejected` → `offered` (attempts+1). The verifying run passes the signature's job → `verified`. The verifying run fails the same way → `offered` (`attempts+1`, failed fix in the picture). |
| `blocked-external` | Lease `block/<sig>`, holder `owner:<cause>`. Allowed only for `OWNER_CAUSES` (external-account, credentials, infra-apply, deploy, billing, dashboard), never for a TEST FAILURE red. | TTL 10 min, renewed by the tick only while the block's probe still fails AND its justification still holds (`owner_block_invalid` logic). Max hold 7 d. | Lease expires, because the probe passes, the justification is gone, or the max hold is reached → `fix-pending (verify)`, which dispatches the workflow, or → `offered`. A max-hold expiry escalates. | A live fixer outranks a block: the block lease is released. |
| `verified` | Terminal: fixed and proven by a run. | 14 d | Archive. | none |
| `cleared` | Terminal: the signature is gone without a known fix. If no commit between can have fixed it → reclassified `flake` (a new item, kind `flake`, determinism tier). | 14 d | Archive. | none |

Tier ladder per red, driven by `attempts`. The ladder is a table, not code paths:

| attempts | tier |
|---|---|
| 0 | author (10 min), then fresh |
| 1 failed fix | the same fixer, resumed with `--resume` |
| 2 | fresh |
| 2 and the culprit is known | revert (a `git revert` with `Fixes-Red`) |
| 3+ | deep investigation (`--effort max`). Each offer escalates. A code red is never parked on the owner. |
| infra-shaped | infra-diagnose only |

### 5.4 Condition (the recovery registry, folded in)

| State | Means | Deadline | On deadline | Other exits |
|---|---|---|---|---|
| `open` | A probe failed: tunnel, gate read, token, git fetch, push, Decodo or another outside dependency. | Probe interval, backoff 2^fails, at most 10 min. 10 min after opening: escalate, then every 2 h. | Re-run the probe. Escalate per schedule. | Probe passes → `closed`, plus its `on_recover` steps, which are now item nudges ("re-evaluate entry X"), not separate actions. |
| `closed` | Terminal. | 14 d | Archive. | none |

The owner's self-recovering-conditions rule holds unchanged: a condition is opened only with a failed
probe, and is closed only by that same probe passing.

### 5.5 The table-driven test

`MACHINES = {"change": {...}, "batch": {...}, "red": {...}, "condition": {...}}`. Each entry is a
`StateSpec(deadline, on_deadline, exits, terminal)`. The test:

1. asserts that every state of every machine has a finite deadline and an `on_deadline` target that is a
   state of the same machine;
2. walks the deadline-only graph (no external progress at all) from every state, and asserts one of:
   - a terminal state is reached;
   - every cycle in it contains an `escalate` action (a red can never time out to "done", but it can never
     be quiet either);
3. asserts that `tick()` refuses an item whose state is not in `MACHINES`, and that the store refuses a
   transition out of a terminal state (audit S2);
4. lints the source: every `item.state = ...` assignment goes through `transition()`.

## 6. Leases

### 6.1 The record

```sql
CREATE TABLE leases (
  name        TEXT PRIMARY KEY,      -- "land/movies", "red/<sig>", "check/<entry>", "block/<sig>", "hold/movies/waiting",
                                     -- "slot/check/<n>", "reconciler/movies", "fixer/<sig>" (offers resolve to red/<sig>)
  holder      TEXT NOT NULL,         -- "<host>:<role>:<id>": laptop:fixer:<session>, laptop:claim:<session>@<worktree>,
                                     -- ci-runner:reconciler:<boot>-<pid>, owner:<cause>, hold:<reason>
  epoch       INTEGER NOT NULL,      -- fencing token: +1 on every acquire; every write made under the lease names it
  secret      TEXT NOT NULL,         -- random, returned only to the acquirer; renew/release need it
  acquired_at REAL, renewed_at REAL, expires_at REAL,  -- the STORE's clock only (no laptop clock, no sleep skew)
  max_until   REAL,                  -- absolute cap; renewal past it is refused
  progress    TEXT,                  -- the last progress mark (opaque to the store)
  item        TEXT,                  -- the item it belongs to ("red:<sig>", "change:<entry>", ...)
  meta        TEXT                   -- json: note, worktree, pid, run, code version
);
```

Expiry is evaluated lazily on every read (`expires_at < now` means free) and actively by the tick. The tick
emits `lease.expired` and moves the item. Expired rows are kept for 1 day for the record, then deleted.

### 6.2 Operations

These are the same in-process (phase 1, `Store`) and over HTTP (phase 2, `RemoteStore`).

| Operation | Arguments | Result |
|---|---|---|
| `acquire` | name, holder, ttl_s, max_hold_s, progress, item, meta | `{epoch, secret, expires_at, max_until}`, or `held {holder, expires_at, meta}`. An expired lease is free. The same holder re-acquiring an expired, untaken lease gets a new epoch (a laptop waking from sleep resumes cleanly). |
| `renew` | name, epoch, secret, progress, note | `ok {expires_at}` when `progress` differs from the stored mark. `no_progress {expires_at}` (unchanged, not extended) when it is the same. `lost {reason: expired-and-taken / max_until / released / unknown}`. |
| `release` | name, epoch, secret, outcome, notes | `ok`; the outcome becomes an event. |
| `list` | prefix | Live and recently expired leases. |
| `offer` | kind, item, tier, priority, deadline, picture | Offer id. Reconciler only. |
| `offers` | kind, capabilities | Open offers, by priority: gate > fix > outside > flake. |
| `take` | offer_id, holder, ttl_s | Atomically closes the offer and acquires the item's lease. Returns the lease plus the picture. |

Rules:

- **Progress is real work, measured by the renewer, never self-declared.** The marks by holder kind:

  | Holder | Progress mark |
  |---|---|
  | dispatched fixer | transcript bytes + worktree HEAD + `git status` hash |
  | interactive claim | the claim's worktree HEAD + `git status` hash + newest mtime of changed files. It must name a worktree, never the root checkout (audit #3: a long-lived orchestrator no longer keeps claims alive) |
  | local check | output bytes + process-group CPU seconds bucketed to 1 s (the existing `stall_clock`) |
  | pusher | none: it never renews, its max hold is its bound |
  | owner block | the probe's fresh failing result id, renewed by the tick |
  | reconciler singleton | the tick counter |

  An explicit `agentctl red beat <sig> --note` is a renewal whose mark is the note text. Repeating the same
  note is `no_progress`.
- **Max hold is absolute.** After it, the item is re-offered. The same holder may take the new offer, but
  that counts as an attempt and is an event.
- **Fencing.** Store writes under a lease carry its epoch (compare-and-set). Git writes carry expected old
  values:
  - `waiting` is updated with `--force-with-lease=waiting:<old>`;
  - pushes to main are fast-forward only, against the base the check ran on.

  A holder that lost its lease and wakes up cannot clobber its successor.
- **Store downtime grace.** At start the store reads `meta.last_alive`, and extends every live lease's
  `expires_at` by the downtime. A store restart never expires everyone at once.

Default TTLs (open question Q9):

| Lease | TTL | Max hold |
|---|---|---|
| `land/<repo>` | 120 s | 300 s |
| `check/<entry>` | 10 min | max(2×p90, 30 min) |
| `slot/check/<n>` | follows its check | same |
| `red/<sig>` (fixer) | 15 min | 3 h |
| `red/<sig>` (interactive) | 30 min | 8 h |
| `block/<sig>` | 10 min | 7 d |
| `hold/<repo>/waiting` | 30 min, no renew | 30 min |
| `reconciler/<repo>` | 60 s | none (renewed per tick) |

### 6.3 Phase 2: where the store lives (decision)

**Decision:** sqlite on ci-runner behind a small HTTP API, `agentctl-store`, on port 3014. It is reached
through the existing Forgejo tunnel.

Why, against a Forgejo-native mechanism (refs as leases, issues or commit statuses as events):

- **Atomicity.** A lease needs compare-and-set over (holder, epoch, expiry) and a single clock. In git,
  refs could emulate CAS with `--force-with-lease`, but every renewal would be an ssh push (seconds, over
  the very tunnel whose drops caused I3), TTLs would need a trusted clock that git does not have, and an
  offer-take that touches two keys is not atomic.
- **Event feed with a cursor.** A monotonically increasing `seq` with long-poll is natural in sqlite.
  Forgejo issues or comments would be slow, rate-limited, noisy in the UI, and still need a cursor.
- **Precedent.** This is exactly the shape of `forgejo-actions-api` (`nix/files/forgejo-gh/actions_api.py`,
  :3010): a stdlib Python service beside Forgejo that authenticates each request by checking the caller's
  own Forgejo token with Forgejo. The tunnel already forwards 3010, so this adds 3014 to the same
  `permitOpen` list and one `LocalForward`.
- **Locality.** The reconciler and the store run on the same host, so the reconciler's store calls never
  cross a network.

API (JSON over HTTP; every call carries `Authorization: token <forgejo token>`):

```
POST /v1/leases/{name}:acquire|:renew|:release      GET /v1/leases?prefix=
GET  /v1/offers?kind=fixer|check                    POST /v1/offers/{id}:take
POST /v1/changes            (submit: entry, branch land/<entry>, tip, base, worktree, session, author, fixes[])
POST /v1/changes/{entry}:cancel|:abandon            POST /v1/reds/{sig}:block   (owner cause + probe + cmd)
GET  /v1/events?after=<seq>&wait=25[&audience=...]  POST /v1/events/{seq}:ack
GET  /v1/state              (the snapshot: gate, items with state/deadline/holder, leases, tick_at, version)
POST /v1/tick               (nudge; also the Forgejo webhook target for push / run-completed)
GET  /metrics               (Prometheus text, for §10's alert rules)
```

The reconciler itself uses the store in-process and is its only writer of items. The HTTP service and the
reconciler share the database file. They are two units, so a reconciler crash leaves the API (leases,
submissions, the event feed) up.

## 7. Executors

### 7.1 Checks

- **Phase 1.** The tick turns `queued` into `checking` by acquiring `check/<entry>` plus a check slot,
  then starting `agentctl waiting check <entry> --lease <epoch>` detached. That checker:
  - renews the lease by its stall-clock progress;
  - writes its verdict to the item;
  - exits on `lost`, killing its process groups.

  A check routed to CI pushes `check/<entry>-<round>` (precommit-check.yml, as today). The run is the
  item's, so a replacement checker adopts it (this is already true today).
- **Phase 2.** The reconciler runs checks on the CI check tier by default (heavy-work-on-ci). The laptop's
  dispatcher may also take `check` offers when the governor admits, if the owner wants local checks
  (open question Q1).

The check verdict comes from the run's conclusion and `classify_job`'s per-step classification. A
setup-step or infra failure is never a test failure (audit #6). The text of a push failure is never parsed
to decide anything: pushes never reject.

### 7.2 Fixer dispatcher

This is on the laptop in both phases. Fixers need Claude Code and the owner's account; ci-runner gets no
Anthropic credentials (open question Q8). The loop:

1. `offers(kind=fixer)`, highest priority first, within its slots and the governor. The slots are reserved per
   class (gate 3 alive, others 2; one fixer on its run's several reds is one slot). An offer that waited 20 min
   borrows an idle slot of the other class (the lender keeps one idle); at 45 min it takes the one overflow slot;
   with that taken it is `STARVED` and escalates. Every capacity wait names what frees it and when. A red whose
   last 3 fixers ended with no result (crash, no verdict line, lease lost without progress) is not dispatched at
   that tier again (G9): escalated once with their log tails and moved up to `deep`; a deep crash loop blocks it on
   the owner (`fixer-crash-loop`), cleared by a newer run that no longer fails this way, or after 24 h;
2. `take(offer)`, which returns the lease and the **full picture**:
   - the runs and failing jobs, with their relevant log lines;
   - every prior attempt: who, verdict, diff, and the run that refuted it;
   - the diagnosis, the culprit candidates, the tier's brief, and `recurring: N` when it applies;
3. create the worktree `~/projects/movies-wt-red-<sig8>-<attempt>` off forgejo/main and spawn
   `claude -p` (`--resume` for the same-fixer tier), exactly as `red_dispatch` does today;
4. every 60 s: compute the progress mark and `renew`. `no_progress` for the TTL means the lease lapses on
   its own. On `lost`, stop the process group and write `RED-STOP.md`;
5. on exit, map the fixer's verdict line (`LANDED` / `GAVE UP:` / `DIAGNOSED:` / `BLOCKED ON OWNER:`) to
   `release(outcome, notes)`. `LANDED` means the fixer ran `agentctl land`, so the red is already
   `fix-pending` through its submitted change.

If the laptop sleeps or is offline, no renewals arrive, the lease expires, and the red is re-offered and
escalated per §5.3. On wake, the dispatcher's renew answers `lost` if someone else took it (the fixer is
stopped), or it re-acquires with a new epoch if nobody did. Interactive agents use the same API:
`agentctl red claim <run|sig> --worktree .` calls `acquire`, and the sync renews it from that worktree's
activity.

### 7.3 Authors

The author of a red is the session recorded at submission (`session`, `author_pid`, `worktree` in the
change item). It is never inherited from the environment of whichever process started a checker
(audit #15). The author tier's offer is addressed to that session through an event (`audience.session`).
The author takes it with `red claim` within 10 min, or the offer widens.

## 8. Events and the inbox

### 8.1 Schema

```json
{
  "seq": 18234,                         // store-assigned, strictly increasing: THE cursor
  "at": "2026-10-28T09:14:03.512Z",     // the store's clock
  "repo": "pawelkrupinski/movies",
  "kind": "change.landed",              // <machine>.<transition>: change.queued|checking|checked|waiting|pushing|landed|
                                        //   rejected|superseded|cancelled|watch_ok|watch_red, batch.*, red.opened|offered|
                                        //   leased|lease_expired|fix_pending|fix_failed|verified|cleared|blocked|unblocked,
                                        //   condition.opened|closed, lease.expired, invariant.violated|restored,
                                        //   reconciler.started|stalled|resumed, offer.taken
  "item": {"type": "change", "id": "20261028-091011-movies-wt-relay-7"},
  "from": "pushing", "to": "landed",
  "severity": "info",                   // info | action (someone must act: their audience) | escalate (a person, now)
  "audience": {"worktree": "/Users/pawel/projects/movies-wt-relay", "session": "974d…", "agent": "relay"},
  "verdict": "LANDED 4c1d2e9f0a…",      // the exact line today's inbox and `land --follow` key on
  "text": "LANDED 4c1d2e9f0a (main moved by 2 commits, no overlap)",
  "refs": {"sha": "4c1d2e9f0a…", "run": 2291, "entry": "…", "sig": "…", "lease": "land/movies", "epoch": 812},
  "tail": ["…last lines of the relevant output, ≤ 25…"],
  "dedupe": "change:…:landed"           // one event per (item, transition) even if a tick repeats
}
```

### 8.2 Into the existing inbox

A projector (in-process in phase 1, the laptop sync in phase 2) writes each event with an `audience` or a
`severity` other than `info` as one inbox JSON file, in today's format. The id is
`<at>-ev<seq>-<kind>`, so a repeated write is a no-op. The `verdict` and `kind` fields map one to one:

| event kind | inbox kind |
|---|---|
| change.landed | LANDED |
| change.waiting | WAITING |
| change.rejected | NOT_LANDED |
| change.watch_red | RED |
| red.fix_failed | FIX_FAILED |
| red.offered past its deadline | RED_UNCLAIMED |
| tier deep | RED_ESCALATE |
| condition.opened | STALLED or UNKNOWN |
| … | … |

So `agentctl inbox`, `inbox wait [--forever]`, `land --follow` and the orchestrator's routing keep
working unchanged. `land --follow` waits for the next event for its entry; it no longer waits on a
process, so a superseded or joined entry ends the follow at once (audit S1).

Escalations: the notifier (the processor in phase 1, the sync in phase 2) sends a macOS notification for
every `severity=escalate` event and posts `:ack` with `by=notifier`. In phase 2 the store also exports
`agentctl_escalations_unnotified`, and Alertmanager sends to Slack any escalation not notified within
5 min (the laptop is asleep). `inbox --ack` of an escalation also posts the ack.

## 9. Status line and ci-why

Each tick writes the snapshot: the gate (with the run numbers), each red with its state, holder and
deadline, the waiting and queued entries, open conditions, `tick_at`, and the code version.

- **Phase 1:** `$AGENTCTL_STATE_ROOT/state.json`, written atomically by the tick.
- **Phase 2:** the sync mirrors `GET /v1/state` every 10 s to the same path, adding `fetched_at`.

`claude-statusline` and `ci-why --summary` read only that file. Their only staleness words are computed
from the two times:

| Condition | Shown |
|---|---|
| `fetched_at` older than 60 s (the line reads `GET /v1/state` itself once the mirror is 30 s old) | `STORE UNREACHABLE since HH:MM (URL: why)` -- never STALLED or UNREAD |
| `tick_at` older than 2 min | `RECONCILER STALLED since HH:MM` |

The gate itself is never shown as UNREAD because the laptop's network is lossy (I7). The gate is UNREAD
only when the reconciler cannot read it, and that is a condition with a probe. The separate `ci-state.json`
60 s cache, `gate-summary.txt` and the `statusline.stale` marker go away (I8).

## 10. The invariant watchdog

The watchdog is a separate process that is cheap and dumb. It takes one corrective action: kickstarting
the reconciler unit. It reads only the snapshot and the store, read-only.

| Id | Invariant | Threshold (open question Q2) |
|---|---|---|
| W1 | The oldest non-terminal change is younger than X. | 2 h while main is green; while red, 2 h past the red's own age |
| W2 | Main red with no live lease, open offer, fix on its way (an open fix change, or a landed fix / verify run within 6 h) or live block lease: for less than 10 min. The tick enforces it itself (`red_enforce`: such a red is offered again, escalated, and dispatched), so W2 firing means the tick is not running. | 10 min |
| W3 | No lease is past its TTL by more than 2 ticks. Otherwise the reconciler is not reaping. | 2 ticks |
| W4 | No item is past its state deadline by more than 2 ticks. | 2 ticks |
| W5 | `tick_at` is younger than 2 min. | 2 min |
| W6 | No escalate event is unnotified for more than 5 min. | 5 min |
| W7 | Phase 2: the laptop sync's event cursor lags by less than 2 min while the laptop is online. | 2 min |

- **Phase 1:** launchd `net.bitcashier.agentctl-watchdog`, every 60 s, `agentctl watchdog`. A violation
  sends a macOS notification and writes an `invariant.violated` event. Restoration writes
  `invariant.restored`. On W5 it also runs `launchctl kickstart -k` on the processor (at most once per
  10 min).
- **Phase 2:**
  - the store exports `agentctl_*` gauges (oldest change age, unleased red seconds, expired-unreaped
    leases, items past deadline, tick timestamp, unnotified escalations);
  - Prometheus alert rules in `nix/files/monitoring/rules/agentctl.rules`, tested with promtool under
    `rules/tests/` like the existing rules, send to Slack;
  - `absent_over_time(agentctl_reconciler_tick_timestamp[5m])` is the dead-man for the reconciler and the
    store;
  - systemd `WatchdogSec=120` with `sd_notify` per tick restarts a hung tick;
  - the laptop watchdog keeps W5 and W7 from the laptop's side and reports `ci-runner unreachable` versus
    `tunnel down` using the existing uplink and tunnel probes.

## 11. Mapping: how each failure is eliminated

### 11.1 Today's incidents

| Incident | Mechanism that removes it |
|---|---|
| G9 a `Fixes-Red:` on a commit unrelated to the red (E9: unit-test speed-ups naming three convergence reds) | `fix_check`: a trailer counts only when the commit touches a path the red's failing job reads (its job's steps, called workflows, actions and scripts, the failing test's sources; unsure counts). One that does not count moves nothing to `fix-pending`; the change lands as an ordinary one, and its author is told (`FIX NOT COUNTED`). |
| G5 a big batch hides which identity change broke drift (E4: 13 commits, E9: 24) | A batch with more than one identity-model commit runs `scripts/identity-drift-ci` on its tip before the push (no script: a logged no-op; no verdict: pushed). DRIFT RED splits it: one identity change per batch, the rest lands now; a lone one red twice is rejected. |
| I1 dead worktree counted as a "committed" fixer | A fixer is only a lease renewed by progress. An unlanded `Fixes-Red` commit in a worktree counts for nothing; only a submitted change item counts (`fix-pending`), and it has its own deadlines. |
| I2 stale owner block re-created by the patrol | A block is a lease the tick renews only while its probe fails and its owner cause holds. Nothing re-creates a block: after expiry, a new block is an explicit `red block` call, evented and counted. |
| I3 push hung on a dropped tunnel, holding the lock 30 min | The landing lease has a 300 s maximum, and the push runs under a 120 s timeout. After expiry the step is verify-then-retry (idempotent). The batch is not blocked past 5 min. In phase 2 the push runs on ci-runner to local Forgejo, with no tunnel at all. |
| I4 claims per run | Leases are keyed `red/<sig>`. The red item aggregates its runs. |
| I5 daily budget → NOBODY | No budgets. Capacity shows as `offered` with a reason and a deadline, and escalates every cycle. NOBODY is not a state. |
| I6 escalations never notified | Escalation is an event severity, delivered with an ack. W6 checks delivery. In phase 2, Slack covers a sleeping laptop. |
| I7 gate UNREAD on a lossy network | Phase 2: the gate is read on ci-runner (the actions API on localhost). Phase 1: the snapshot carries the last good read and its age. A push needs a read younger than 1 tick, and the display shows the age instead of flipping to UNREAD. |
| I8 status line behind a 60 s cache | The status line reads the per-tick snapshot (≤ 15 s), with explicit `as of`. The second cache is deleted. |
| I9 orphaned worktrees never landed | Submission puts the change on Forgejo (`land/<entry>`). After that the worktree is irrelevant. A worktree with unsubmitted commits and no live session is auto-submitted once by the sync (`origin: orphan`). A rejection becomes a `rescue` red. |
| I10 tests polluted the real cache | Everything goes through an injected `Store` and `Sources`. Tests use an in-memory store and a temp state root. The real-cache guard test stays. |
| I11 unbounded retry loops | Every retry is an item attempt with a deadline and a cap. A lint test forbids `while True` and `time.sleep` outside `tick_loop` and the executors' renew loop. |

### 11.2 The 15 audit cracks, and the smaller items

| Crack | Eliminated by |
|---|---|
| 1 NOT LANDED or never landed, author ended | `rejected` → after 30 min a `rescue` red (offered, leased, escalated). Never-submitted worktrees → orphan auto-submit (I9). |
| 2 RED ESCALATE never notified | Escalate events with delivery ack, W6, Slack in phase 2. |
| 3 claim never stale while a long session lives | Renewal needs progress in the claim's own worktree, and the max hold is absolute. A live pid counts for nothing. |
| 4 tests write the real cache | Injected store and root (I10). |
| 5 batch retries forever | The batch machine: attempts per tip, cap 3 → `held` lease → escalate. |
| 6 transient push failure → NOT LANDED | `pushing` never rejects. A failure → verify → `checked`, attempts and backoff, then a push-failing condition. Check verdicts come from run conclusions, not text. |
| 7 reds outside the 200-run window are invisible | The newest decided run per workflow file (phase 1: the API's per-workflow query; phase 2: the local actions API). |
| 8 stuck undecided runs | A `stuck-run` red after 3× the workflow's p90. A landed change's watch deadline opens it too. |
| 9 a git error crash-loops the processor | Steps are per item, inside `try`. An exception is an attempt on that item; the tick never dies. `worktree prune` runs before `add`. |
| 10 bisect rejects on one flake | The culprit is confirmed by a second failing run before rejection. |
| 11 restarts counted as attempts | Attempts are recorded only on outcome events, by type (infra or test). |
| 12 queued change hangs with a live checker | `queued` and `checking` have deadlines. Waits are leases and offers, not loops in a checker. A live process proves nothing. |
| 13 watchdog re-exec mid-batch leaves a hook running | There is no in-process batch. Checks are executors with leases, and they kill their process groups on `lost`. Re-exec happens only between ticks. |
| 14 `land` from `movies-wt-waiting` accepted | Submission refuses internal trees and branches (`waiting`, `check/*`, `land/*`, `*-wt-check-*`, `*-wt-batch`). Tested. |
| 15 wrong owner on entries | Author fields are explicit at submission. Events carry `audience`. Nothing reads `CLAUDE_PID` from an inherited environment. |
| S1 `land --follow` hangs on supersede or join | Follow is an event wait; supersede and join are events. |
| S2 `supersede` races the checker's save | Items are updated by compare-and-set on the version, and the store refuses writes out of terminal states. |
| S3 RELANDING announced, result discarded; counter keyed on HEAD | There is no re-land action. Re-evaluation is level-triggered, and attempts are keyed per entry. |

The second reviewer's 9 items are the same defects: R1 = #5, R2 = #6, R3 = S3, R4 = #11, R5 = #12,
R6 = #3, R7 = #4, R8 = S2, R9 = #1 and #15.

## 12. What existing code is deleted

About 3,300 of agentctl's 11,445 lines, measured by function spans. The rest (git helpers, `ci-why`'s
explanation, the governor, `prepush-check`, `env`, `db-q`, `cleanup`, `orphans`/reap, `inbox` reading,
signatures and flake classification) stays.

| Area | Functions removed | ≈ lines |
|---|---|---|
| Landing lock, queue, checkers | `land_lock*`, `lock_note`, `held_for`, `land_loop`, `check_entry`/`_check_entry`, `spawn_checks`, `check_tree_slot`, `reconcile_dir`/`reconcile_checking`, `queue_join`, `run_entry_checks`, `remote_check`/`remote_poll`/`save_remote`, `await_entry`/`_await_entry`, `queue_verdict`, `supersede` | 760 |
| Waiting processor | `waiting_round`, `waiting_run`, `start_watchdog`, `start_duties`, `HEARTBEAT`/`IN_BATCH`/`DUTY_BEAT`, `push_batch`'s lock, `batch_state`, `bisect`'s retry path, `unread_note` | 330 |
| Red claims and patrol | `claim_*`, `all_claims`, `scan_fix_commits`, `live_claim_worktrees`, `fix_commit_stale`, `stale_fix_worktrees`, `find_fixers`' worktree scan, `red_state`/`RED_STATE_RANK`, `fixer_queue*`, `dispatch_blocker*`, `dispatches_today`/`RED_DAILY_CAP`/`dispatch_refund`, `note_unclaimed`, `red_patrol`/`maybe_patrol`/`patrol_repo`, `sweep_claims`, `assign_gate_red`/`author_*`, `keep_diagnosed_claim`/`diagnosed_followup`, `validate_owner_blocks`, `maybe_dispatch`, `fixer_duplicate` | 1,270 |
| Recovery registry and escalation | `open_condition` and friends, `RECOVER_ACTIONS`, `recover_reland`, `land_failure_cause`, `land_not_landed`, `inbox_escalations`/`maybe_escalate_inbox`. The probes themselves stay, as condition probes. | 330 |
| Watch and verify processes | `_watch`, `watch_landed`, `spawn_watch*`, `verify_outside_gate`, `verify_fixes`, `release_claims_for` (now the `landed` watch and the `fix-pending` steps) | 270 |
| Orphan duties | `orphan_*`, `maintain_stale_fixes` (now orphan auto-submit in the sync) | 170 |
| Status publishing | `ci_state_*`, `publish_gate_summary`, `cached_gate_summary`, `maybe_publish_gate`, `mark_gate_dirty`, the statusline marker | 150 |

Also deleted: the `reds/*.json` claim files, `waiting/<repo>/reconcile/`, `recovery/*.json`,
`inbox/escalated/`, `ci-state.json` and `gate-summary.txt`, each migrated or expired in its step.
`AGENTCTL.md`'s "recover", "always claimed", "red" and "land queue" sections are rewritten. The
`claude-statusline` refresh logic shrinks to reading `state.json`.

New code, estimated: store and leases about 400 lines, machines and tick about 900, executors and
dispatcher about 400, sync about 250, watchdog about 150. Phase 2 adds the HTTP layer (about 300) and the
infra module.

## 13. Migration plan

Each step is one commit, or a short series. It replaces and deletes in the same change: no flag, no dual
path. It keeps the pipeline landing throughout, and is reversible by `git revert`. Leases are soft and
items re-derive from git and Forgejo, so a revert loses at most in-flight attempt counters.

### Phase 1 (laptop)

1. **The store.** Add `Store` (sqlite at the state root; `leases`, `items`, `events`, `offers`) and
   `agentctl lease list|acquire|renew|release` for debugging. Unit tests run with an in-memory store.
   Nothing else uses it yet.
2. **The watchdog first, read-only, over today's system.** Add `agentctl watchdog` and its launchd plist.
   W1, W2, W5 and W6 are computed from today's files (queue entries, claims, the processor heartbeat,
   unescalated inbox entries). This makes the next steps observable, and it catches regressions during
   the migration.
3. **The landing lock becomes a lease.** `land_lock()` keeps its interface and is backed by `land/<repo>`
   (TTL 120 s, max 300 s). Pushes run under a hard timeout; on expiry, verify-then-retry. The flock is
   deleted. This removes I3 on its own.
4. **Check slots become leases.** `Slot` and `check_slot` are backed by `slot/check/<n>` and
   `check/<entry>`, renewed by `stall_clock` progress. `gov` keeps admission. `check_tree_slot`'s
   unbounded wait is deleted.
5. **The machines and `transition()`.** Add `MACHINES`, the table-driven test (§5.5), and `items` derived
   from today's entry files (a read-only projection) that `waiting status` prints. Every deadline
   violation already shows in the watchdog (W4).
6. **The tick drives changes.** The processor loop becomes `tick()` for change items: `queued` →
   `checking` → `checked` → `pushing` | `waiting` | `rejected`. This deletes `spawn_checks`,
   `reconcile_checking`, `_check_entry`'s tail, `land_failure_cause`, `land_not_landed` and
   `recover_reland`. Checkers become executors with `--lease`.
7. **Events and the inbox projector.** Transitions emit events, and the projector writes the inbox
   entries. Direct `inbox_write` in landing code is deleted. `land --follow` and `waiting await` wait on
   events.
8. **The batch machine.** It replaces `waiting_round`, `waiting_run`'s nap loop, `push_batch`'s lock, the
   bisect retry, `start_watchdog` and `start_duties` (the tick is the only loop).
9. **Watches from items.** The `landed` watch and `fix-pending` verification are derived each tick.
   Detached watch processes and `spawn_watch*` are deleted.
10. **Red items and leases.**
    - `red claim`, `release` and `beat` become lease operations. Today's claims are imported as leases
      with a 15-min TTL, so they lapse unless their worktree shows progress.
    - The patrol becomes the tick's red steps.
    - Dispatch becomes `offer` plus an in-process dispatcher that uses only the offers and leases API
      (the phase 2 shape).
    - Claim files, the fixer queue, the budget and the patrol are deleted.
11. **Owner blocks become block leases**, renewed by their probe. `validate_owner_blocks` and the block
    side of `open_condition` are deleted.
12. **Conditions become items.** The recovery registry folds into `condition` items. Escalation becomes
    the event severity plus the notifier. `inbox/escalated/` and `maybe_escalate_inbox` are deleted.
13. **The snapshot.** The tick writes `state.json`, and the status line and `ci-why --summary` read it.
    `ci-state.json`, `gate-summary.txt` and the marker are deleted.
14. **Submission by ref, and orphans.**
    - `land` pushes `land/<entry>` to Forgejo and the item points at it. Checks rebuild from the ref,
      not the worktree. This needs a one-line movies hook and workflow exclusion so `land/*` runs nothing.
    - Orphan detection becomes auto-submit-once.
    - `orphan_*` and `maintain_stale_fixes` are deleted.
15. **Docs.** `AGENTCTL.md` is rewritten for the new model. The memory rules (`red-build-always-assigned`,
    `self-recovering-conditions`, `landing-policy-local-then-main`) are updated where their mechanics
    are named.

### Phase 2 (ci-runner)

The owner pushes and deploys every infra step.

1. **Code delivery.** `~/bin` gets a Forgejo remote (`pawel/agentctl`). infra pins a revision (open
   question Q6). Nothing runs from it yet.
2. **infra: the store service.**
   - `agentctl-store.service` (store + HTTP :3014, auth through Forgejo like actions_api) is added, with
     `StateDirectory`, an hourly `sqlite .backup`, firewall, `permitOpen` + 3014, and the
     `ci-lander` bot (a oneshot like `forgejo-cancel-bot`; its ssh key is generated on the host and never
     leaves it).
   - `agentctl.rules`, with promtool tests, is added.
   - It is inert: nothing calls it.
3. **The laptop's store becomes remote.** The laptop adds `LocalForward 3014`, and `Store` →
   `RemoteStore` for the laptop's processor. A one-shot import uploads non-terminal items, and leases
   re-acquire on their next renew. The laptop reconciler still ticks, holding `reconciler/movies`
   (rank 1). Revert = back to the local file.
4. **infra: the reconciler service.** `agentctl-reconciler.service` (rank 2, user `ci-lander`, reads runs
   from the actions API on localhost, pushes to local Forgejo). On start it requests
   `reconciler/movies`. The laptop holder sees a higher-rank waiter at its next renewal and hands over
   at a tick boundary. The laptop instance becomes a standby: it acquires only if the server's lease
   expires. This is the handover; there is no switch.
5. **Checks route to the CI tier by default** (Q1). A laptop `check` offer taker runs only if the owner
   keeps local checks.
6. **The laptop sync.** The laptop processor shrinks to the sync: submit, events → inbox, state mirror,
   orphan facts, the dispatcher, and the notifier. The laptop's tick code path stays only as the standby
   (Q7).
7. **Watchdog phase 2.** The Prometheus alerts go live (Slack). The laptop watchdog keeps W5 and W7.

## 14. Simplifications

The owner (2026-10-28): "simplify the pipeline / process / branches if possible". Each candidate is kept only if no
guarantee is lost. Phase 1 implements the simplified shape directly; §13's steps stay in order, each deleting more.

### 14.1 One path for every change (kept)

**Goes:** the long-lived `waiting` branch and its worktree (`-wt-waiting`, `-wt-batch`), the separate batch
processor loop, `join`/`queue_join`, `rebuild`, `ensure_waiting_tree`/`drop_waiting_tree`, and the
`waiting pause|resume|cancel|run|service|await` subcommands. Every change is one item in one queue. The tick lands
items in order, Fixes-Red first. While main is green, a checked item is pushed alone. While main is red, checked
items wait in state `waiting`. When main turns green with more than one item waiting, the tick computes the batch
tip in a throwaway check tree (main plus the items, in queue order), checks it once, and pushes that tip. On
failure it bisects, confirming the culprit as §5.2 says. A batch is a computed item, not a branch.

**Guarantees kept:**
- a production change never lands on a red main (unchanged gate rule);
- a batch is checked as a whole on the current main before its one push;
- a culprit is confirmed before it is rejected;
- Fixes-Red lands while red.

**Lines deleted:** about 640 (the waiting branch and batch functions, 546; the waiting commands, about 100). The
batch machine (§5.2) keeps its states, but `assembling` builds a tip in a check tree instead of a branch.

### 14.2 Fewer branch kinds (kept)

**Goes:** `check/<id>` (remote-check refs), the local `waiting` branch, and the reconciler's own ad-hoc names.
There are two namespaces, each owned by a live item:
- `land/<entry>`: the submitted change; each round force-updates it (`--force-with-lease`) to the rebased tip, and
  that ref is what a CI check runs on;
- `measure/<id>`: measurement runs, as today.

A red fixer's worktree branch stays local and becomes a `land/<entry>` when it submits. The tick deletes every
`land/*` or `measure/*` ref not backed by a live item (the reconciler's own refs, within the standing OK). The
forgejo-src patch branch is infra's, so it is out of scope.

**Guarantees kept:** a CI check runs on exactly the tree it judges, and nothing is pushed to main except a checked
tip. **Lines deleted:** about 120 (`cancel_entry_remote`, the check-branch naming and its cleanup).

### 14.3 One check definition (kept, with two kinds)

**Goes:** the split between `run_entry_checks`, `remote_check`/`remote_poll`/`save_remote`, `batch_check` and
`check_slot`'s callers. One executor: `check(tree, base, kind) → verdict`, with kind in {`change`, `batch`}.
- It runs where capacity is: local under the governor, or on the CI check tier.
- Its verdict is stored once, keyed by (tree hash, base hash, kind), and reused wherever the same tree appears: a
  replacement checker, a batch whose tip equals an already-checked change, and the push (agentctl sets the movies
  hook's REUSE env from the stored verdict).

**Kept separate:**
- the two kinds, because the batch's fast-only check is a deliberate trade the owner made;
- the hook's known-red baseline, a correctness feature for a red main, passed through unchanged.

**Guarantees kept:** every pushed tip has a pass for exactly that tree and base; a check's infra failure is never a
test failure. **Lines deleted:** about 240 (the check paths, 243, folded into one executor of about 120 new lines).

### 14.4 One way to hold work, one way to report (kept)

**Goes:**
- every per-mechanism store: `reds/claims/*.json`, block claims, `recovery/*.json`, `recovery/relands.json`,
  `recovery/orphans.json`, `reds/fixer-queue.json`, `waiting/<repo>/batch*.json`, `waiting/<repo>/reconcile/`,
  `ci-state.json`, `gate-summary.txt`, `statusline.stale`, `inbox/escalated/`;
- the detached `watch` processes.

What replaces them: a lease (holding) and an item (state), both in the store; an event (reporting), projected to
the inbox and the snapshot.

**Guarantees kept:**
- the owner's rules (a condition opens and closes on one probe; always claimed; never NOBODY; a code red is never
  the owner's), now as properties of the machines and leases;
- the inbox format, which the orchestrator keys on.

**Lines deleted:** the §12 table (about 3,300), unchanged by this item: it is §12 itself.

### 14.5 Fewer commands (kept)

| Today | After phase 1 |
|---|---|
| `waiting status` | `land status` (the queue with each item's state, deadline and holder) |
| `waiting pause` / `resume` | `land hold` / `land release` (a `hold/<repo>` lease) |
| `waiting cancel <id>` | `land cancel <id>` |
| `waiting run` / `service` | `agentctl tick` (the launchd service's loop, same label) |
| `waiting check` / `await` | internal: the check executor; `land --follow` waits on events |
| `queue` (retired) | deleted |
| `watch` | deleted (the `landed` state's watch, derived each tick) |
| `red claim` / `release` / `block` | unchanged names, lease operations |
| `red list` | `red status` (reds with state, deadline and holder) |
| `red patrol` / `red auto` / `red dispatch` | `red patrol` deleted (the tick); `red auto` kept (dispatcher on or off); `red dispatch` kept (an offer at top priority) |
| `red verify` | deleted (the `fix-pending` step dispatches the run) |
| `red fixer` | internal (the dispatcher's wrapper) |
| `recover status` / `tick` | `agentctl status` (items, leases, open conditions); `agentctl tick --once` |
| `maintain` | kept for the reap and the sweep; orphan and stale-fix duties move to the tick |
| `dispatch` (CI owner records) | kept (`measure/<id>` items later) |

Thin aliases stay for one week where agents' briefs name the old command, printing the new one.

### 14.6 Not simplified (rejected)

- **Dropping the batch.** It is what lets several changes land in one check at green after a red spell. Pushing
  them one by one would multiply the checks, and the queue would drain slowly.
- **Dropping local checks in phase 1.** The owner's landing policy is "local tests, then main"; CI-by-default is
  phase 2 (decision 1).

## 15. Security

- **No new credentials on the laptop.** The sync and dispatcher authenticate to `agentctl-store` with the
  Forgejo token they already have: the Touch ID vault, handed to the launchd processor over the existing
  0600 socket, `TokenDrop`. The store checks every token with Forgejo's `/api/v1/user`, caches the result
  for 5 min, and authorizes only an allow-list of users (`pawel`). Lease operations additionally need the
  lease's secret, so a fixer holding the token cannot release another holder's lease by guessing an epoch.
- **Tunnel.** The tunnel stays forward-only with the `forgejo-tunnel` key. It gains one `permitOpen`
  entry (`<private>:3014`) and one `LocalForward`.
- **The reconciler's push identity.** It is the Forgejo bot user `ci-lander`:
  - it is created on-host by a oneshot unit, on the `forgejo-cancel-bot` pattern;
  - its ssh key or token is generated and kept in its `StateDirectory` (0700) and never leaves ci-runner;
  - it is a write collaborator on movies only;
  - rebases set the committer to `ci-lander` and keep each author, so provenance stays visible;
  - main's branch protection allows pushes from `pawel` and `ci-lander` only. Making the reconciler the
    sole pusher of main is open question Q3.
- **What the reconciler checks instead of the hook.** The reconciler does not run movies' client-side
  pre-push hook, which needs sbt. It enforces the hook's guarantees itself before a push:
  - the gate is green, read this tick;
  - a passing precommit-check run exists for exactly the pushed tree and base;
  - `prepush-check` (the squash and empty-commit guard) passes on the range;
  - no `--no-verify` and no force: main is fast-forward only.
- **Untrusted data.** CI logs, scraped pages and fixtures are data in fixer pictures, as today. Event text
  shown in notifications goes through `applescript_str`. The store never executes payloads.

## 16. Failure modes of the new design

| Failure | What happens | What notices it |
|---|---|---|
| Reconciler process crashes or hangs (store up) | systemd restarts it, and `WatchdogSec` kills a hung tick. Its `reconciler/movies` lease (60 s) expires, and the laptop standby takes over if awake. Nothing is lost: items are in the store, truth is in git and Forgejo. | Alert on `tick_timestamp` stale > 3 min, laptop W5, status line `RECONCILER STALLED`. |
| Store service down (reconciler up) | The reconciler uses the database in-process and keeps landing. Laptop submissions and renewals fail: `land` keeps the submission in a local outbox (`QUEUED-LOCAL <entry>`), retried by the sync. Fixer leases are extended by the downtime grace on restart. | Prometheus `up{job="agentctl-store"}`; the laptop sync's error. |
| ci-runner down | Forgejo is down too, so nothing can land on any path. Fixers keep working locally; their renewals fail, and leases survive through the downtime grace. Submissions queue in the local outbox. | Existing node-down alerts. The laptop shows `ci-runner unreachable` (tunnel up, 3014 refused) as distinct from `tunnel down`. |
| Tunnel down or laptop offline or asleep | The server side keeps reading the gate, checking, batching, pushing and watching (the main win). Laptop fixers' leases lapse, and reds are re-offered and escalated to Slack. The status line says `view as of HH:MM`. The existing tunnel watch kickstarts the tunnel. | W7; Slack for unnotified escalations. |
| Two reconcilers both believe they hold | Every store write carries the lease epoch (compare-and-set), and every git write names its expected old value. The stale one's writes fail, and it steps down on the next renew (`lost`). | `reconciler.stalled` / `lost` events. |
| Clock skew or laptop sleep | TTLs use the store's clock only. Laptop sleep is just "no renewals". | none needed |
| Store database lost | Re-derived: changes from `land/*` refs, reds from runs, leases re-acquired by their holders. Lost: attempt counters and terminal history (restored from the hourly backup). | Reconciler start-up logs `re-derived N items`. |
| A bug pins items in a state | The deadline exit fires anyway. If the tick itself is broken, W4 (items past deadline) and W3 (expired leases not reaped) fire. | W3, W4 |
| Watchdog down | Phase 2: Prometheus alerting is independent of both services. Phase 1: the maintain launchd job checks the watchdog's own heartbeat file, and the watchdog checks maintain's. | Each other |

## 17. Test strategy

1. **Machines.** The table-driven test of §5.5: deadlines, exits, liveness through escalation,
   terminal-state immutability.
2. **Leases.** Unit tests on a fake clock: acquire, renew with the same mark (`no_progress`), max hold,
   expiry, re-acquire after sleep, fencing (a stale epoch's write refused), downtime grace, secret
   required.
3. **Tick idempotence.** For a corpus of fact snapshots, run `tick()` twice; the second run performs no
   side effect. Side effects are recorded by fake `Sources` (git, Forgejo, spawn).
4. **Incident replays.** One scenario test per row of §11:
   - I3: a fake push blocks forever → `land/` lease expires at 300 s, the batch is retried, no other
     lander waits past it;
   - I1: a worktree with a `Fixes-Red` commit and no process → the red stays `offered`;
   - #5: `push` always fails → `held` plus escalation after 3;
   - the others likewise.

   These use the existing fakes (`fake_forgejo_gh.py`, `fake_claude.py`), temp bare repos and an
   in-memory store.
5. **Fault injection.** A randomized simulation (seeded, about 500 runs in CI) drives the tick, executors
   and dispatcher on a fake clock. It kills processes at random points, drops the network, jumps the
   clock (sleep), and makes Forgejo unreadable. It asserts that W1–W6 hold within a bounded number of
   ticks after the faults stop, and that no change is lost or double-pushed.
6. **Lints, extending `test_recovery.py`'s approach:**
   - no `while True` or `time.sleep` outside the tick and renew loops;
   - no `fcntl.flock` outside the store;
   - no `inbox_write` outside the projector;
   - no item state write outside `transition()`;
   - no `CLAUDE_PID` read from an inherited environment.
7. **Isolation.** The real-cache guard stays: every test gets a temp `AGENTCTL_STATE_ROOT`, and the store
   path derives from it at call time.
8. **Phase 2.**
   - The HTTP layer is tested against the same `Store` contract suite: one suite, two implementations.
   - The infra rules have promtool tests.
   - The NixOS module gets an `evalModules` or VM check if the infra repo has one for actions_api.
     Otherwise a `nixos-rebuild build` of ci-runner in infra CI.

## 18. Open questions for the owner

1. **Where checks run in phase 2.** Default to the CI check tier (heavy-work-on-ci), or keep the laptop's
   local pre-commit checks ("run unit tests locally before checking in") with CI only under pressure?
   The design supports both: checks are offers.
2. **X for "the oldest unlanded change".** Proposed: 2 h while main is green; while red, 2 h past the red's
   age.
3. **Sole pusher.** Should main's branch protection make `ci-lander` (and the owner) the only pushers?
   Agents push as `pawel` today, so a strict rule would need the owner to use a separate key for their
   own pushes.
4. **Auto-cancel.** May the reconciler auto-cancel a run stuck past 3× its p90, per
   ci-availability-over-running-jobs, before offering an infra fixer?
5. **Slack.** Which channel gets escalations when the laptop is away: `slack-infra-alerts`, or a new
   receiver?
6. **Code delivery.** Is the agentctl code delivered to ci-runner from a new Forgejo repo for `~/bin`,
   pinned by infra (deploy = a pin bump the owner pushes)?
7. **The laptop standby.** Keep the laptop reconciler as a standby after phase 2, or drop it to keep one
   code path?
8. **Fixers overnight.** With the laptop asleep, reds wait offered (escalated to Slack) until it wakes.
   Acceptable, or should headless fixers also run on ci-runner (Anthropic credentials on the server)?
9. **TTL defaults.** Are the defaults in §6.2 right? In particular: a fixer's 15 min without progress, a
   3 h maximum, and a push's 300 s maximum.

## Owner decisions (2026-10-28)

Approved: build phase 1, then phase 2, with these defaults for the open questions.

1. Checks run on the CI check tier by default in phase 2; the laptop only compiles and runs quick tests.
2. "Oldest unlanded change" invariant: X = 2 h.
3. `ci-lander` becomes the only pusher of `main` once phase 2 has run stable for a week.
4. The reconciler may cancel its own stuck runs and runs superseded on main; never anyone else's.
5. Slack channel for escalations: not decided yet (needed only in phase 2).
6. `~/bin` gets a Forgejo repo; infra pins the reconciler's version from it.
7. No laptop standby reconciler after phase 2: one writer.
8. Overnight fixers on ci-runner: later, not part of phase 1 or 2.
9. Lease TTLs: start with the defaults in §6; tune from the watchdog's data.
