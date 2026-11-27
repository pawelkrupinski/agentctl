# One change, one record (R3, 2026-11-27) -- DONE

Status: DONE (2026-11-27, steps 1-6 landed). The store item is the only record of a server's change; the entry
files are the laptop's outbox alone. "Where each fact lives today" below is the state before the work, kept as its
rationale.

Review 2 finding A1 (`agentctl-review2/A.md` D1): a change lives twice, as a QUEUE ENTRY FILE
(`<state root>/waiting/<clone>/entries/<id>.json`, `waitingdir.py`) and as a STORE ITEM (`items`, type `change`),
joined by `queueslots.set_change`. At origin/main a627d15, 30 modules name the entry files' readers and writers
(`waiting_entries`, `queued_entries`, `save_entry`, `ENTRY_FINAL`) and 46 name a change item (`item("change"`,
`items("change"`, `open_items(.., "change"`, `"change",` in a store call, `set_change`); 20 do both.

## Where each fact lives today

Both copies are on ONE host: the queue is ci-runner's (role `server`). The laptop (role `sync`) has an entry
directory too, but it is an OUTBOX: `land --follow`'s `join` (a script's land while main is red) writes a `waiting`
entry there and the sync step `migrate_local_queue` submits it to ci-runner (status `migrated`) or closes it landed.
On 2026-11-27 the laptop's directories held 0 entries.

| fact | entry file | item | which wins today |
|---|---|---|---|
| state | `status` (an entry vocabulary: + `joined`, `migrated`) | `state` (the machine, `statemachines.MACHINES["change"]`) | the ITEM for every reader outside the queue (`land status`, the status line, the page, the inbox, awaiters on any host); the ENTRY for the queue's own tick (`queued_entries`, the batch, the checker), which acts on the entry's status. They disagree whenever a writer moves one alone: `close_item_landed`, `supersede_stranded`, `store.cancel_change`, the projector (entry first, item a tick later), `_migrate_landed` (both, by hand) |
| identity, lander | `worktree`, `head`, `branch`, `session`, `lander`, `commit_keys`, `fixes`, `diagnoses`, `replays` | copied once at the item's creation (`change_item_fields`) | the entry (the item's copy is never refreshed: `queue_join` rewrites `fixes` on the entry alone) |
| queue age | `seq`, `drawn`, `created`, `queued_since`, `requeued_from` | `submitted_at`, `entry_created_at` (copies) | the entry |
| the check's working state | `commits`, `tip`, `base`, `checked_base`, `full_base`, `full_tip`, `rounds`, `rechecks`, `known_red`, `remote_run`, `retry_after`, `infra_*`, `timeout_retries`, `backlog_batch`, `batch_alone`, `push_alone`, `drift_split`, `cycle_ignore`, `regen_hold`, `conflict_told`, `verify*`, `waiting_id`, `migrate_told_at`, ... (about 60 keys) | only what `set_change(data=...)` passes: `tip`, `base`, `mode`, `where`, `rounds`, `rechecks`, `autoresolved`, `infra_attempts`, `push_attempts` | the entry; the item's copy is a snapshot at the move that wrote it |
| the verdict | `why`, `decided`, `landed_sha`, `pushed_at`, `batch`, `batch_id`, `superseded_by`, `decided_on` | the same keys (`_landed_fields`, `_item_data`) | written together by `set_change`; `save_entry` keeps a final status already on disk against a racing writer (audit S2), the store's compare-and-set keeps the item's |
| item-only facts | -- | `hold`, `change_kind`, `wait_rule`, `check`, `check_runs`, `precheck`, `landed_commits`, `delivered_at`, `unheld_issue`, `regen_round`, `regen_answered`, `red_accum0`, the events | the item (other writers: `queuehold`, `changekind`, `lander`, `checkrows`, `queuesubmit`, `syncsteps`) |

Measured on the 2026-11-26 23:57Z store copy: 2,001 change items, 36 data keys; none `waiting`, one `checking`.

The legacy statuses: `joined` (a queued change that met a red gate became a waiting entry of its own, the queued one
`joined` it) has had no writer since 6d206ed (2026-11-18, "a change held behind a red gate is one item"); every
joined entry's waiting entry was decided long ago (the copy has no `waiting` item), so `joined_changes`,
`joined_verdicts`, `_finish_joined`, `joined_verdict`, the projector's joined branch and `ENTRY_STATE["joined"]` serve
nothing. `migrated` is the laptop outbox's "sent": it marks a file nothing reads again.

## The target: the item is the change

THE STORE ITEM IS THE ONLY RECORD OF A CHANGE: its state is the item's state, and every fact the queue keeps about it
is a key of the item's data. Nothing else is a truth.

- The queue's tick reads changes with `store.items("change")` (through one view, `changes.py`: a `ChangeRecord`
  over an item -- `.id`, `.state`, and the entry's keys as before), never a file.
- A writer moves a change with ONE call: the transition (state + data + event), or `update_item` for data alone.
  `set_change`'s double write and `save_entry`'s race rule go: the store's compare-and-set is the only race rule.
- The laptop's outbox (a `--follow` land while red, written while ci-runner may be unreachable) stays a FILE, because
  it is what the laptop must keep offline -- but as a cache with one sync, like R1's offline event log: written
  locally, drained to ci-runner by the sync step (`submit`), DELETED once the store took it. It is never read as a
  change's state; its only reader is the drain (and `land status`, which shows it as "not yet submitted").

Why not the other way (the file as the truth, the item a projection)? Every reader outside the queue -- the laptop's
`land status`, awaiters, the inbox, the page, the stats, `history` and `analyze` -- already reads the item over the
API, and the item has the compare-and-set and the events the machine table guards. The file has neither, and is
invisible off ci-runner. The item is the record the system already trusts; the file is the copy.

## Version skew

The laptop and ci-runner may run different revisions: ci-runner's pin holds the last green revision while agentctl's
main is red, and a revert can move it back. The rules that make each step deployable alone:

1. The files and the queue's items are on the same host, so a step changes the server's reading and writing of its
   own two copies; the API between the hosts (`STORE_CALLS`, `POST /v1/changes`) does not change shape in any step.
2. Item data only GROWS (keys are added, none renamed or removed) until the last step, so an old client reading a new
   server's items sees the keys it knew; a new client reading an old server's items falls back where a key is
   missing (no reader may require a key an old server does not write).
3. While an older revision may still run the queue, the files stay WRITTEN (a write-through cache: the item first,
   then the file) so a rollback finds them current. Only the last step stops writing them, after a release in which
   nothing read them.
4. A status an older revision wrote and a newer one no longer knows (`joined`, `migrated`) reads as FINAL (decided):
   an entry is open only when its status is one of the open states, `queued checking checked pushing waiting`.

## The steps

Each step lands with its characterisation tests first, a test failing before it for any behaviour it fixes, and the
simulator gate; the sim's I6 ("no change lost: entries and items agree") is tightened, in a commit of its own, as the
copies converge.

1. **Drop `joined`.** `ENTRY_FINAL` becomes "not an open status" (`ENTRY_OPEN`); `joined_changes`,
   `joined_verdicts`, `_finish_joined`, `statemachines.joined_verdict`, the projector's joined branch and the
   `"joined"` rows of `ENTRY_STATE` / `causes.ENTRY_CAUSE` are deleted. `ENTRY_STATE` and `ENTRY_STATUS`, identities
   once `joined` is gone, are deleted with their lookups. Safe alone: nothing writes `joined` since 2026-11-18, and an
   old joined file still reads as decided (rule 4).
2. **Drop `migrated`.** The outbox's drain deletes the local file once ci-runner took the change (or closes it landed
   by content, as today); nothing is marked `migrated`. Safe alone: the laptop's files are its own, and an old laptop
   revision finding no file has nothing to send.
3. **The item carries the record.** Every entry write (`save_entry`, and so `set_change`) writes the entry's keys to
   the item's data too (`update_item`, the item created first when absent), so the item holds everything the file
   holds. Additive (rule 2). An invariant in the simulator: after every tick, an open change's item data equals its
   entry (but `status`), and its state equals the entry's status.
   As built: THE writer of an entry is `waitingdir.ChangeRecords(store, d).save(e)` (every former `save_entry` call
   site); it writes the file, then the entry but its status under ONE added key, `record`, with `Store.merge_item` (a
   merge that bumps no version and writes no event: no writer's compare-and-set is disturbed; server-only, no API
   call). One key rather than the entry's keys spread over the item: about 20 keys share a name with the item's own
   (`why` truncated, `fixes` only when not empty, the verdict keys a store-only writer sets), and a stale entry must
   not overwrite them. The laptop's outbox (a remote store, or none) writes the file alone. The projector gives an
   open entry's item its record when the item is made after the entry, and catches any write that missed it; a change
   decided before this step has no record (readers fall back, rule 2). Sim invariant IC.
4. **Readers move to the item**, module by module (`changes.records(st)` instead of `waiting_entries(d)`), the
   batch, the tick and the checker last. Each move is a pure read swap (the record is the same dict) proved by the
   tightened invariant and the module's tests.
   As built: the view is `waitingdir.ChangeRecords(store, d)` -- the writer of step 3 is the reader too: `every()`,
   `open()`, `waiting()`, `queued()`, `get(id)`, each change its item's `record` with the item's state as its
   status (`change_record_of`); with no local store (the laptop's outbox) the files. Two problems, solved so:
   - **(a) the item comes first.** `queue_change` (and `slots.join`) wrote the entry before the projector made the
     item, so a reader of the items missed a new change for a tick. `ChangeRecords.save` now creates the item --
     carrying its record, with the projector's creation data (`new_change_data`) -- before it writes the file; the
     store is the server's own database, so this is synchronous. A laptop whose store is remote writes the outbox
     file alone, and the sync's `POST /v1/changes` creates the item later, as before.
   - **(b) no scan of every entry.** The change number came from a scan of every entry ever written (6.8 MB on
     ci-runner). It is the store's atomic counter, `Store.next_seq("change_seq:<queue dir>")`
     (`ChangeRecords.draw_seq`), started once above every number the entries and records hold (the counter never
     goes back). The ~2,000 changes decided before step 3 have no record, and none is backfilled: the view leaves
     them out, and no reader needs a decided change's full record -- the decided changes it reads are recent
     (a conflict-bounced place: 24 h, other changes' failures on this base: 1 day, a push since a condition
     opened, a batch's members, an open red's fix: since the red opened; every one decided after step 3 has its
     record), or need only the item's own fields (the land-ref sweep reads the items; the stats had sent every
     decided change's record before step 3). Measured on the 2026-11-27 03:11 copy: all 2,008 decided changes'
     stats sent, 0 rejected for a conflict in the last 24 h, no change open.
   Still reading the files, the laptop's or the host's own: `land status`'s outbox line, the outbox's drain
   (`migrate_local_queue`), `orphans.queued_worktrees` and `redauthors.landed_heads` (a red's author from the
   landed change: all of history, which the items' own `landed_sha`, `worktree` and `session` hold -- moved with
   their store argument in step 6), `runowners.queue_entry_worktree`, and the projector (it goes in step 6).
5. **Writers write the item first, the file as a cache** (rule 3): `save_entry` stays only as the write-through;
   `set_change` is one transition carrying the record.
   As built: `ChangeRecords.save` makes the item current first (created carrying its record, else its record merged)
   and writes the file after it, IN THE ITEM'S STATE: a copy read before the item moved on writes its keys, never its
   stale status (before, the projector then moved the item back to that status). THE RACE RULE IS THE ITEM'S
   (`take_decision`): a writer of a stale copy meets a decision the item holds -- its state, `why`, `landed_sha`,
   `superseded_by` -- whatever the file says. `set_change` is ONE transition carrying the record in its data; the
   save after it merges nothing unless the item was decided already.
6. **The files go** (after a release of step 5 on ci-runner): `save_entry`, `waiting_entries`, `queued_entries`,
   `ENTRY_FINAL`, the projector's `project_changes`, `change_item_fields`, `entry_created_at`,
   `entry_queued_since`, `_landed_fields`, `_item_data` and the entries directory on the server (kept read-only one
   more release as a backup, then removed by the cleanup sweep). The laptop's outbox stays (rule above).
   As built: `ChangeRecords.save` with the server's store writes the item alone (no file: the rollback cache goes), and
   `set_change` saves nothing after a move that carried the record (only a decided item's decision is taken and
   merged). The projector's `project_changes` and its tick step go. The last direct readers of the files read the
   items: `redauthors.landed_heads(store)` (a landed change's `landed_sha`, `worktree`, `session` on its item),
   `orphans.queued_worktrees(repo, store)` (`ChangeRecords`), `runowners.queue_entry_worktree(store, id)` (the item's
   `worktree`; the store threaded down from the gate publish through `ci_state_refresh`, `branch_state`, `owner_of`);
   `land status`'s outbox line and the outbox's drain (`migrate_local_queue`) read the outbox through
   `ChangeRecords(None, d)`. KEPT, deliberately: `save_entry` / `waiting_entries` / `drop_entry` as the OUTBOX's writer,
   reader and drain (rule above); `change_item_fields`, `entry_created_at`, `entry_queued_since`, `_landed_fields` and
   `_item_data`, which write the item's own top-level keys (`worktree`, `session`, `why`, `landed_sha`, ...) that every
   reader on any host reads -- removing them would break an older client reading a newer server's items (rule 2), and
   moving those readers to the record is a change of its own. The server's existing entries directory is left in
   place, unread and unwritten, for the cleanup sweep. The simulator reads the records (I6, IS, IC redefined).

`analyze` on a fresh read-only store copy must show the same numbers after each step (no step rewrites history:
the events and their causes are written as before).
