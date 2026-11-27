# agentctl, part `queueknobs`: the land and queue knobs: landing, batches, holds, waits, pushes, regen
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from agentctl.knobread import env_num
from agentctl.knobread import env_text


MAX_FOREIGN = 2  # files a push may revert to an older main before prepush-check refuses ($AGENTCTL_MAX_FOREIGN)


# NO STALLED HOLDER (the owner, 2026-10-27): one land's hook hung 2.5 h in sbt-warm and blocked every lander behind the
# lock. A step land runs (the hook's dry run, the push) that makes NO PROGRESS -- its output does not grow and its
# process group uses no CPU -- for LAND_STALL_MIN minutes ($AGENTCTL_LAND_STALL_MIN) is ended (its process group,
# TERM then KILL) and land says `NOT LANDED: stalled <step>`. And the long checks run BEFORE the lock: under it, only a
# fetch, a check that main has not moved since, and the push (whose hook reuses the dry run's pass). Waiters print
# how long the holder has held the lock, and what it is doing, every LAND_WAIT_REPORT_MIN minutes.
LAND_STALL_MIN = 20


def knob_max_foreign() -> float:
    return env_num("AGENTCTL_MAX_FOREIGN", MAX_FOREIGN)


# the owner, 2026-11-26: keep the STALLED alert, warn-only, at 60 min (policy.py `queue-stall`)
QUEUE_STALL_AFTER = 60 * 60  # s with open changes and no change moving before QUEUE STALLED opens (queuestall.py)


def knob_queue_stall_after() -> float:
    return env_num("AGENTCTL_QUEUE_STALL_AFTER", QUEUE_STALL_AFTER)


def knob_land_stall_min() -> float:
    return env_num("AGENTCTL_LAND_STALL_MIN", LAND_STALL_MIN)


# ---------------------------------------------------------------- the backlog batch (the owner, 2026-11-17)
# "BATCH WHENEVER THERE'S A BACKLOG, NOT ONLY ON RED": with 6-8 changes queued at green each paid a 30-40 min check of
# its own, mostly waiting for a ci-runner check slot. Now, at green with no batch open, BACKLOG_BATCH_MIN or more queued
# changes that no checker has taken and that may share a check (backlog_batchable: identity changes too, 2026-11-20;
# no `land --no-batch`, no red fix, no combined-tree verification
# round, none in an infra backoff or holding an unread remote run) are THE BATCH ABOVE with depth `full`: assembled in
# queue order (Fixes-Red first; a member that does not apply is left out and checked alone, batch_alone), its tip
# checked once with the FULL landing check (the hook's --range dry run; on ci-runner check/batch-<tip>, the same
# precommit-check round a change gets, area tests and heavy rows included -- no member had a check of its own), in
# ONE check tree. Pass: one push carrying that run (PREPUSH_REMOTE_RUN), every member LANDED with its own record and
# watch (batch_id, batch). Fail: bisected by prefix (no whole-batch re-check first: the culprit's own confirmation
# guards a flaky failure), the culprit rejected only when its failure is its own (judge_failure: a base red, a time
# cap or an infra failure sends it to a check of its own instead), the passing prefix pushed at once, the rest queued
# again (backlog_reconcile). At most BACKLOG_BATCH_MAX members, so a bisect is at most 4 extra rounds (3 prefixes and
# the confirmation). While such candidates wait for a tree, the change tick gives them none of its own (they wait for
# the batch); red fixes are checked alone beside it, and a red fix never waits for it.
BACKLOG_BATCH_MIN = 2  # queued changes that may share a check and make a backlog batch


BACKLOG_BATCH_MAX = 8  # a backlog batch's members at most: its bisect is 3 prefix checks + 1 confirmation


# ---------------------------------------------------------------- the push batch (the owner, 2026-11-17)
# "COMBINE THEM INSTEAD OF LANDING THEM ONE BY ONE" (W1: the oldest unlanded change over 2 h). A checked change pushed
# alone: on a main that moved since its full check it got a verify round of its own (THE RULE, queue.py), and THE
# OLDEST VERIFICATION PUSHES FIRST held every younger checked change meanwhile -- so N checked changes landed serially,
# each paying a check-runner wait plus a ~10-min verify round after the previous push moved main (ref-scrapers
# verifying while pr274 and postlanding held their pushes behind it, each to verify again after it pushed). Now, at
# green with no batch open (a change's own verify round running holds none: A LONE VERIFICATION HOLDS NO BATCH), PUSH_BATCH_MIN or more changes that would each need a verify
# round on the current main (push_batchable: checked on an older main, or queued for a verify round; no red fix, no
# `land --no-batch`, no known red, none in a backoff) are THE BATCH ABOVE with depth `verify`, the backlog batch's
# machine: assembled oldest first on the current main, each member judged by THE RULE against main plus the members
# before it (batch.py push_batch_full_round: ONE ROUND, THE DEEPEST A MEMBER OWES -- one needing a FULL round, or a
# queued member never checked, makes it the full landing check; nobody is left out), else the union verified ONCE (the hook's --verify-only
# over <the oldest member's full base>..<tip>: every test config compiled, the lints, the fast specs and the unit
# tests of every module a member or what landed meanwhile touches; on ci-runner check/verify-<base>-batch-<tip>), and
# pushed ONCE (the hook skipping the plan the members' full checks passed, as a lone verified change's push does).
# Fail: bisected by prefix as the backlog batch is (the culprit confirmed, rejected only when the failure is its own),
# the passing prefix pushed, the rest queued again for their verify round. More than one identity-model commit in it:
# G5's drift check of its tip (check_batch); after a drift split, one identity change per batch (drift_serialize). A
# change checked while a push batch runs is no member: its push waits for the batch (batch_goes_first) and it joins
# the next one. At most BACKLOG_BATCH_MAX members.
PUSH_BATCH_MIN = 2  # changes owing a verify round that make a push batch


DRIFT_STALL = 30 * 60  # s with no output before the drift check is ended (no verdict: the batch goes on)


# The batch is an ITEM, not a branch: while main is red, checked changes wait (entries `waiting`); at green -- or with a
# Fixes-Red fix for the red among them -- the tick ASSEMBLES the batch in a check tree (main + each waiting change, in
# batch order: a conflict rejects that change alone, one already on main is LANDED), and the batch executor (`waiting
# check-batch <id>`, holding check/batch-<id> and the tree's slot) checks it once (--fast-only, a fresh sbt server, a
# stall retried once) and, failing, bisects it (a culprit confirmed before it is rejected). The tick pushes a passing
# batch under the landing lease (PREPUSH_REUSE_FAST) and verifies it on main: every change LANDED. A batch that fails to
# land BATCH_RETRY_MAX times, or stalls twice, is held for 30 min (its deadline), then assembled again. Every state has
# its deadline and its exit (MACHINES["batch"]); nothing loops, nothing waits on a lock.
BATCH_RETRY_MAX = 3


# FREEING A LANDED WORKTREE (2026-11-18: `land --until-done` LANDED 94a33a968 but printed no DONE -- the agent's own
# kept sbt server, `scripts/sbt-warm`, still worked in the worktree, and the agent removed it by hand). Before the
# removal, free_worktree stops what is the CALLER's in that worktree and nothing else: the checkout's own sbt server
# (`scripts/sbt-warm --stop`, which only ever stops that checkout's), then every process whose cwd is inside it and
# whose environment carries the caller's CLAUDE_CODE_SESSION_ID (SIGTERM, by pid -- never a pattern). It waits up to
# FREE_WAIT s for the tree to be free; whatever still holds it (another session's, or one without a session) is
# returned, never signalled.
FREE_WAIT = 30  # s --until-done waits for a landed worktree to be free of processes before it keeps it


FIXER_LAND_WATCH_EVERY = 60


# GENERATED FILES ARE NO OVERLAP (2026-11-19 ~06:05Z: six movies changes from parallel refactoring lanes -- agreement,
# f2, datalayer3, resolver2-b1, enrichment2, resolver2 -- sat in single file, each `held: its check waits for <prev>`
# because each edits test/resources/coupling-baseline.tsv, several complexity- and length-baseline.tsv too: every
# coupling or complexity lane landed strictly one at a time, hours of serial checks). Those files are GENERATED (one
# row a class, package or method; `scripts/coupling-report --update`, `scripts/quality-report --update`), and the
# ratchets in the pre-push hook recompute them over the tree they check, so a combined tree's right content is "the
# rows regenerated over it" -- and two changes touching DISJOINT rows do not conflict at all. A repo registers them in
# red-repos.json (`generated`: {paths, regen: its own --update command, timeout}); a registered file is
# - NO OVERLAP: the hold (OVERLAPPING CHANGES ARE CHECKED AS ONE, overlapcheck.py), backlog-batch membership, the conflict a
#   queued change is told of, `no longer applies on main` and the push's full-recheck rule all leave it out -- a
#   source both change (TestWiring.scala) still holds the younger (ONLY A VOIDED CHECK HOLDS, below);
# - RESOLVED, when a rebase, a batch's assembly or land's own rebase conflicts in it: its rows merged three-way (each
#   row keyed by its columns but the value: a row one side changed takes that side's value; disjoint rows always
#   merge), else -- a row both sides changed differently -- the repo's own regen command run in the tree, which IS
#   the combined tree at that commit (the other files picked), and its files staged into the commit being picked (on
#   the laptop: ci-runner's reconciler has no JVM, its checks run on CI, so there a same-row conflict is MAIN'S ROW,
#   below; the push's own rebase, under the landing lock, never runs one: a new verify round instead). The
#   commit keeps its message and trailers (a `Coupling:`/`Complexity:` rise still carries its own), and the hook's
#   ratchet judges the result as it judges any baseline; a member so resolved that its batch then fails is checked
#   alone (batch_alone), never rejected for it;
# - MAIN'S ROW, PROVISIONALLY, where no regen command runs (2026-11-19 06:58Z: lane F2b's queued change NOT LANDED --
#   "no longer applies on origin/main 2cfa0ee95e" -- because c3922ba26 had re-measured the very baseline rows its own
#   baseline commit changed, and ci-runner's reconciler, the server role, has no JVM: a rebase by hand and a new place
#   at the back of the queue, for a file no agent edits by hand): a row both sides changed keeps the side already in
#   the tree (main's, or the earlier batch members'), every other row merged as above; the files are named
#   (`provisional`) and the checks judge the combined tree -- the ratchet passes a fall not yet recorded and fails a
#   rise, naming the rows, and a failure is reported as the change's own with the regen command to run;
# - a SET table (a key on many lines: coupling-fanout-refs.tsv's `<class><TAB><target>`) is merged line by line;
# - else, the merge and the regen failing (a side not a table, the command's exit, its timeout), the member
#   is left out of the batch and checked alone (batch_alone `regen`, the files and why in the log), held as before
#   behind the change it overlaps. Tests: tests/test_generated.py, tests/test_queue_sim.py GeneratedOverlapTest.
GENERATED_REGEN_TIMEOUT = 1200  # s a generated file's regen command may run before the conflict is the author's ($AGENTCTL_GENERATED_REGEN_TIMEOUT)


# One lander per repository: with no pre-merge CI left (movies, 2026-10-26) a landing is the hook's local tests --
# minutes for an identity change -- and two landers would each test a base the other is about to move, then
# race, rebase, and test again. Agents wait for the lock instead (land runs in the background), and it caps the
# laptop at one landing hook's JVM at a time.
LAND_LOCK_TIMEOUT = 4 * 3600


LAND_WAIT_REPORT_MIN = 10


# EVERY PUSH IS BOUNDED (the owner, 2026-10-28, "fix root cause in a structural way": a queued change's `git push` over
# the ssh tunnel died with "Connection to localhost closed by remote host" when the hotspot dropped, then hung -- no
# progress, no exit -- holding the landing lock 30+ min while a checked batch waited behind it with the gate green): a
# push gets a no-progress bound (no output and no CPU) of PUSH_STALL_SECS and a wall bound of PUSH_WALL_MIN, its group
# killed on either; a stalled or dead-transport push is infra (re-queued), never a hang.
# THE HOOK INSIDE A PUSH IS JUDGED BY THE HOOK'S BOUND (2026-11-16: movies 040934987's push lost its landing lease
# 3 min in -- a gate run had completed since the check outside the lock, so the hook could not reuse that pass and ran
# its checks again, silent, their CPU in a kept sbt server and a Docker container, out of the push's process group): while
# a pre-push hook process runs in the push's group, its leases renew at every poll and the hook's own bound (the check
# outside the lock's, LAND_STALL_MIN; movies' hook bounds each step itself) applies; the transport around it keeps
# PUSH_STALL_SECS and progress-only renewal, so a push hung on a dead tunnel still loses the lock within minutes.
PUSH_STALL_SECS = 180


PUSH_WALL_MIN = 45


# THE LANDING LOCK IS A LEASE (design §6, phase 1 step 3; the owner, 2026-10-28: a push hung on a dropped tunnel held
# the flock 30+ min and a checked batch waited behind it). `land/<repo>` in the store: TTL LAND_LEASE_TTL, renewed only
# on real progress (run_watched's output / CPU), capped at LAND_LEASE_MAX; a holder that stops progressing simply
# loses it, and a waiter takes it. No flock, no heartbeat file, no killing another process: expiry IS the release.
# A holder that lost the lease stops its work (run_watched ends its group: LeaseLost).
LAND_LEASE_TTL = 120


LAND_LEASE_MAX = 45 * 60  # the design's 300 s once checks no longer run in the push's hook under the lease (step 6)


LANDER_LANDED_EVERY = 300        # s between the tick's notes of landed changes' commits on main ($AGENTCTL_LANDER_LANDED_EVERY)


WAITING_AWAIT_TIMEOUT = 24 * 3600


INFRA_SAME_MAX = 30 * 60


# ONE QUEUE, CLOSED BY CONTENT (2026-10-30 split brain: six pre-cutover entries in the laptop's local waiting dir and
# four store items mirrored from them sat "waiting" for 11 h, every one already on main under rebased shas, and W1
# escalated). A change whose commits are on main BY CONTENT (`git cherry`: landing rebases, so never by sha) is LANDED,
# looked at every LANDED_LOOK_EVERY s per change, every tick: the server's entries and items, and on the laptop the
# store's items its clone has the commits of. The laptop holds no queue of its own: what its local dir still holds
# goes to ci-runner's queue (migrate_local_queue). A store item no queue carries is a STRANDED CHANGE condition.
LANDED_LOOK_EVERY = 600


# AN OVERDUE REGEN HOLD RECOVERS (the owner, 2026-11-20: "1 approve, 2 drop the reject"; lane F2b's 24-h NOT LANDED is
# struck). Past CONFLICT_PLACE_KEPT the change tick runs the regeneration AGAIN: a new round on the change's item
# (`regen_round`), which the laptop's sync answers by starting `agentctl land` in its worktree once more (server.py
# answer_regen_holds, once per change, head and round; it says so in `regen_answered`). When the hold still stands
# REGEN_RECOVERY_WAIT later -- the laptop unreachable, its JVM down, the regen failing -- the owner is told ONCE (an
# escalation on the change: what is held and why, whether the laptop answered). The change stays held in its place.
REGEN_RECOVERY_WAIT = 2 * 3600  # s from the re-run to the owner's one escalation ($AGENTCTL_REGEN_RECOVERY_WAIT)


# LAND --UNTIL-DONE (the owner, 2026-11-18): agents kept ending badly -- stopping after the rebase without landing,
# handing back partial reports or the same report twice, leaving worktrees behind, and `git branch -D` by hand after a
# landing rebased their commits (never merged by ancestry). This mode owns the whole tail, bounded by --deadline:
# submit (or, called again after an interruption, find its own entry by worktree and head: never queued twice), the
# fast precheck in the foreground, the wait for the verdict, and then
#   NOT LANDED -> the exact reason and the next command (rebase / fix / land again), exit 1;
#   LANDED     -> every local commit's patch proven on origin/main (git cherry, content twins), the worktree clean and
#                 this agent's own (the change's worktree, not the root checkout, no other session's) -> the worktree
#                 removed and its branch deleted (the force delete only after that patch check, close_worktree):
#                 `DONE <sha> (...): ... worktree removed`, exit 0. Not provable -> `LANDED <sha> ... NOT DONE: <why>`,
#                 the worktree kept, exit 3. The caller's own processes in it (its kept sbt server) are stopped first
#                 (cleanup.py free_worktree); still held by another's -> each holder named and the command, exit 4;
#   the deadline -> `NOT DONE: <id> is <state> ...`, the command to resume, exit 2.
# A VERDICT IS DURABLE (2026-11-19: subagents' `--until-done` waits died with a Claude session restart, and they handed
# back "queued, waiting" with no verdict). The change's item holds it, and its exit event reaches the inbox naming the
# worktree and the change (the projector), whether or not a call waited; `land status <entry>` prints it after the
# fact; and a call again from the worktree at that head finds its entry (own_change_item): open -> RESUMING, landed ->
# the cleanup, REJECTED -> that verdict and the next command (before, a rejection decided while nothing waited was
# silently queued again unchanged) -- but only while it STILL HOLDS (verdict_still_holds; 2026-11-21,
# movies-devpanel-oauth: a rejection from 17:02, already printed to its waiter, was replayed at 17:35 as decided "while
# no --until-done waited" though main had moved and fixed the main-side bug behind it): never delivered to a waiter and
# decided on the main of today. Otherwise the change is queued, so checked, again.
UNTIL_DONE_DEADLINE = 4 * 3600  # s (--deadline): a batch at green, a verify round and a push fit well inside it


LAND_BEFORE_QUEUE_SECONDS = 1200


# A HELD WAIT IS NO ESCALATION (2026-11-19: phase3-agreement, -f2 and -enrichment2 each escalated to the owner every
# 15 min -- "queued for 45 min, waiting on a check slot" -- while each was held behind an older change it overlaps:
# F2 behind agreement on coupling-baseline.tsv, E2 behind F2 on TestWiring.scala). `queued`'s deadline loops into
# itself, and every lap raised the level and notified. Now each tick records WHY a queued change waits on its item
# (`hold`: the overlap and its blocking change and files, the batch, every check tree busy, the governor) -- `land
# status` and `status` show it -- and a lap past the deadline is QUIET while that hold is live: its blocker still in
# flight (whose own machine escalates should IT stick), a batch or busy trees (their holders' deadlines bound them).
# It escalates only when the hold is stuck: nothing named holds it, its blocking change is gone, or it is older than
# CHANGE_HOLD_MAX (the `waiting` state's own bound).
CHANGE_HOLD_MAX = 6 * 3600  # s ($AGENTCTL_CHANGE_HOLD_MAX)


# AN IDLE HOLDER'S HOLD EXPIRES (2026-11-26; queuehold.py): an overlap hold on a change that is not in flight -- parked
# on `waiting`, or queued and itself held -- lets go of it for good after this long.
HOLD_IDLE_TTL = 60 * 60  # s ($AGENTCTL_HOLD_IDLE_TTL)


# NO HOLD YET IS NO STUCK HOLD (2026-11-23 04:19Z/04:44Z: py-restore-corpus, checked, was pushed by the next tick 40 s
# and 27 s later; W1 read it in between as "nothing holds it, yet it has not pushed"). A change that has just entered
# its state has no recorded hold until a tick pushes, starts or holds it: past this grace with none, it is stuck.
UNHELD_GRACE = 10 * 60  # s ($AGENTCTL_UNHELD_GRACE)


# SUBMISSION BY REF (design §13 step 14, §14.2): a queued change is also pushed to the remote as refs/heads/land/<entry>
# (its own ref: the movies hook checks nothing on it, no workflow runs on it), so the change no longer lives only in a
# worktree -- an author that ends, a worktree removed, leave it reachable. The tick deletes every land/* ref whose change
# is decided (or unknown). Checks rebuild from the commits, never from the worktree.
LAND_REF_SWEEP_EVERY = 600


LAND_REF_UNKNOWN_AGE = 24 * 3600  # s: a ref of a change neither the entries nor the store know is deleted only past this


NOTE_PRECHECK_WAIT = 110  # s a precheck's note waits at most for the store or its item (the delays above, cut at this)


# A RED FIX LANDS THROUGH THE QUEUE (2026-11-16, Main #2515: a fixer's land, in-process since the queue began, ran the
# whole pre-push hook on the laptop -- its identitydrift step waited 75 min on the starved measure queue -- while its
# fixer's own `timeout` killed it twice and its 180-min turn ran out; the queue's 45-s precheck would have refused the
# missing Drift-Slice: trailer at once). A fixer's change is queued like every change -- the CI tier, red fixes first
# -- and its land runs the fast precheck in the FOREGROUND (its refusal is the fixer's NOT LANDED in seconds), then
# waits for the change's verdict, bounded: LANDED <sha> / NOT LANDED: <why>, the lines its wrapper reads.
FIXER_LAND_WAIT = 2 * 3600  # s a fixer's land waits for its queued change's verdict ($AGENTCTL_FIXER_LAND_WAIT)


def knob_backlog_batch_max() -> float:
    return env_num("AGENTCTL_BACKLOG_BATCH_MAX", BACKLOG_BATCH_MAX)


def knob_backlog_batch_min() -> float:
    return env_num("AGENTCTL_BACKLOG_BATCH_MIN", BACKLOG_BATCH_MIN)


def knob_batch_retry_max() -> float:
    return env_num("AGENTCTL_BATCH_RETRY_MAX", BATCH_RETRY_MAX)


def knob_change_hold_max() -> float:
    return env_num("AGENTCTL_CHANGE_HOLD_MAX", CHANGE_HOLD_MAX)


def knob_hold_idle_ttl() -> float:
    return env_num("AGENTCTL_HOLD_IDLE_TTL", HOLD_IDLE_TTL)


def knob_drift_stall() -> float:
    return env_num("AGENTCTL_DRIFT_STALL", DRIFT_STALL)


def knob_fixer_land_wait() -> float:
    return env_num("AGENTCTL_FIXER_LAND_WAIT", FIXER_LAND_WAIT)


def knob_fixer_land_watch() -> float:
    return env_num("AGENTCTL_FIXER_LAND_WATCH", FIXER_LAND_WATCH_EVERY)


def knob_free_wait() -> float:
    return env_num("AGENTCTL_FREE_WAIT", FREE_WAIT)


def knob_generated_regen_timeout() -> float:
    return env_num("AGENTCTL_GENERATED_REGEN_TIMEOUT", GENERATED_REGEN_TIMEOUT)


def knob_infra_same_max() -> float:
    return env_num("AGENTCTL_INFRA_SAME_MAX", INFRA_SAME_MAX)


def knob_landed_look() -> float:
    return env_num("AGENTCTL_LANDED_LOOK", LANDED_LOOK_EVERY)


def knob_lander_landed_every() -> float:
    return env_num("AGENTCTL_LANDER_LANDED_EVERY", LANDER_LANDED_EVERY)


def knob_land_before_queue_seconds() -> float:
    return env_num("AGENTCTL_LAND_BEFORE_QUEUE_SECONDS", LAND_BEFORE_QUEUE_SECONDS)


def knob_land_lease_max() -> float:
    return env_num("AGENTCTL_LAND_LEASE_MAX", LAND_LEASE_MAX)


def knob_land_lease_ttl() -> float:
    return env_num("AGENTCTL_LAND_LEASE_TTL", LAND_LEASE_TTL)


def knob_land_lock_timeout() -> float:
    return env_num("AGENTCTL_LAND_LOCK_TIMEOUT", LAND_LOCK_TIMEOUT)


def knob_land_ref_sweep() -> float:
    return env_num("AGENTCTL_LAND_REF_SWEEP", LAND_REF_SWEEP_EVERY)


def knob_land_ref_unknown_age() -> float:
    return env_num("AGENTCTL_LAND_REF_UNKNOWN_AGE", LAND_REF_UNKNOWN_AGE)


def knob_land_wait_report_min() -> float:
    return env_num("AGENTCTL_LAND_WAIT_REPORT_MIN", LAND_WAIT_REPORT_MIN)


def knob_note_precheck_wait() -> float:
    return env_num("AGENTCTL_NOTE_PRECHECK_WAIT", NOTE_PRECHECK_WAIT)


def knob_push_batch_min() -> float:
    return env_num("AGENTCTL_PUSH_BATCH_MIN", PUSH_BATCH_MIN)


def knob_push_stall_secs() -> float:
    return env_num("AGENTCTL_PUSH_STALL_SECS", PUSH_STALL_SECS)


def knob_push_wall_min() -> float:
    return env_num("AGENTCTL_PUSH_WALL_MIN", PUSH_WALL_MIN)


def knob_regen_recovery_wait() -> float:
    return env_num("AGENTCTL_REGEN_RECOVERY_WAIT", REGEN_RECOVERY_WAIT)


def knob_unheld_grace() -> float:
    return env_num("AGENTCTL_UNHELD_GRACE", UNHELD_GRACE)


def knob_until_done_deadline() -> float:
    return env_num("AGENTCTL_UNTIL_DONE_DEADLINE", UNTIL_DONE_DEADLINE)


def knob_waiting_timeout() -> float:
    return env_num("AGENTCTL_WAITING_TIMEOUT", WAITING_AWAIT_TIMEOUT)


def knob_baseline() -> str:
    return env_text("AGENTCTL_BASELINE", "1")  # 0: skip the baseline run of a land's dry run


def knob_queue() -> str:
    return env_text("AGENTCTL_QUEUE", "1")  # 0: no batching queue
