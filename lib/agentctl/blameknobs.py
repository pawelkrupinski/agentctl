# agentctl, part `blameknobs`: the blame and ask knobs: whom a condition or a red is told to, how often and when it is given up; the recovery notes
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from agentctl.knobread import env_num, env_text


ASK_REMIND_MAX = 4 * 3600     # ...then doubling up to this ($AGENTCTL_ASK_REMIND_MAX)


BLAME_MAX = 90 * 60              # s a red may wait for its culprit to be decided before a fresh fixer gets it ($AGENTCTL_BLAME_MAX)


BLAME_PROBE_WALL = 2 * 3600      # s one probe may run before it is no verdict ($AGENTCTL_BLAME_PROBE_WALL)


def knob_blame_probe_wall() -> float:
    return env_num("AGENTCTL_BLAME_PROBE_WALL", BLAME_PROBE_WALL)


def knob_ask_remind_max() -> float:
    return env_num("AGENTCTL_ASK_REMIND_MAX", ASK_REMIND_MAX)


def knob_blame_max() -> float:
    return env_num("AGENTCTL_BLAME_MAX", BLAME_MAX)


def knob_no_secrets() -> bool:
    return bool(env_text("AGENTCTL_NO_SECRETS"))  # set: this process never runs `secrets` (a child of recovery)


def knob_recovery_dir() -> str:
    return env_text("AGENTCTL_RECOVERY_DIR")  # the recovery notes' directory


# ---------------------------------------------------------------- blame (the owner, 2026-11-01)
# "change agentctl to: remember which agent landed what change, when a build fails: revert the offending change and
# revive the landing agent to fix it" -- and "only if it can't be determined spinup a new agent".
#
# THE DECISION RULE (one place: blame_decision): a gate red whose offending change AND its lander are determined goes
# back to that lander -- the change is reverted and the lander's own Claude session is revived to fix it. A fresh fixer
# is spun up ONLY when that cannot be determined: no single culprit (the parent fails the same check too, the bisect is
# inconclusive or found no verdict), no recorded lander, or the lander's session cannot be resumed.
#
# 1. THE LANDER. Every `agentctl land` records who ran it (lander_record): the Claude session ($CLAUDE_CODE_SESSION_ID),
#    the subagent when the land ran inside one (its transcript under <session>/subagents/; the session id alone would
#    name the orchestrator), the transcript, the worktree, the branch, the cwd -- and each commit's KEYS (sha, patch-id,
#    subject, author time: a landing rebases, so a commit on main is found by its patch-id or its subject and time,
#    never by its sha alone). Queued changes carry it in their change item; a direct land (no queue) writes a `lander`
#    item. Both live in the reconciler's store, for 14 days, across restarts and batches; once a change is landed the
#    tick notes its commits on main (`landed_commits`: main sha -> patch-id).
# 2. THE CULPRIT. A gate test red's range is (the newest green run's head, the first red run's head]; its commits are
#    grouped into changes by the registry (a commit no change carries is the owner's, by hand). The search keeps an
#    invariant -- boundary lo PASSES the same check, boundary hi FAILS it -- and a change is the culprit only when its
#    parent passes and its end fails. Boundary 0 (the green head) passes for free only for an unseeded check (Main's
#    green run there IS the same check on the same inputs); convergence drift seeds rotate per run, so it is re-run with
#    the red run's own seeds (`scripts/identity-drift-ci <parent> --for-red <run>`). Between boundaries: a decided run of
#    the workflow on exactly that commit, else a probe on CI, never on the laptop (drift: identity-drift-ci; Main:
#    precommit-check.yml on a `check/blame-*` branch holding the boundary's tree, its pass counted only when its log
#    names the failing test). The failing test's files hint which boundary to probe first; at most BLAME_PROBES probes.
#    Infra-shaped reds, flakes, outside-the-gate reds and a bot's change (Dependabot, Renovate, Scala Steward) are never
#    blamed or reverted.
# 3. THE REVERT (tier `lander`): one `git revert` of the change's commits with `Reverts-Change: <id>` and
#    `Fixes-Red: <run>`, landed by `agentctl land` (a gate fix: it lands on red and is never throttled). It does not
#    clear the red (FIX FAILED): the culprit was wrong -- the revert is reverted (`Relands-Change: <id>`) and the red goes
#    to the normal fixer.
# 4. THE LANDER REVIVED: `claude --bg --resume <session>` in its own worktree (a subagent: a session in its worktree
#    briefed with its transcript), told the red run, the failing step and lines, that its change was reverted, and to
#    fix, rebase, re-apply its change with the fix and land again (a revert that conflicts: fix forward). It holds the
#    red's lease (tier `lander`), which the revert's own fix never takes from it.
BLAME_PROBES = 4                 # CI probes one red's culprit search may start ($AGENTCTL_BLAME_PROBES)


RECOVER_BACKOFF_MAX = 600  # a probe is re-run at most this far apart, however often it failed ($AGENTCTL_RECOVER_MAX)


# A BLIP OPENS NOTHING (the owner, 2026-11-17: 19 UNKNOWN spells in a week, 18 closed by themselves in ~70 s --
# "movies token present" -- each an inbox entry and a notification for nothing): a kind in CONDITION_GRACE opens only
# once it is seen failing again at least that long after it was first seen (one sighting run: sightings no more than
# CONDITION_PENDING_LAPSE apart; a run that lapsed starts again). Until then the sighting is a pending mark in the
# store's meta (`condition_pending:<key>`), and open_condition answers None as for a passing probe. A store that cannot
# keep the mark opens the condition at once, as before: loud over silent.
UNKNOWN_GRACE = 300  # s an UNKNOWN condition must keep failing (seen again past this) before it opens


CONDITION_PENDING_LAPSE = 1800  # s between two sightings after which a pending condition's sighting run starts again


# THE WEEKLY ESCALATION (the owner, 2026-11-11, issue 20261111T143441-75a957: ONE NOTIFICATION PER ISSUE, never a
# digest). An open issue past its deadline re-enters `open`: one escalate event naming its id, filer and text, its
# level up, its deadline a week on. One issue per ISSUE_ESCALATE_GAP, the most overdue first: a backlog (69 overdue on
# 2026-11-12, none ever escalated) reaches the notifier one at a time -- more than NOTIFY_BURST in one notifier pass
# become one summary -- and their weekly deadlines stay that far apart afterwards.
ISSUE_ESCALATE_GAP = 120  # s between two issues' escalations ($AGENTCTL_ISSUE_ESCALATE_GAP)


ASK_REMIND_FIRST = 3600       # the first reminder this long after the owner was told ($AGENTCTL_ASK_REMIND_FIRST)


ASK_HOLD = 3600               # s a question its block's probe may answer is held (recorded, not told) ($AGENTCTL_ASK_HOLD)


AUTHOR_ACTIVE_MIN = 15   # live = a pid alive and activity within this ($AGENTCTL_AUTHOR_ACTIVE_MIN)


def knob_ask_hold() -> float:
    return env_num("AGENTCTL_ASK_HOLD", ASK_HOLD)


def knob_ask_remind_first() -> float:
    return env_num("AGENTCTL_ASK_REMIND_FIRST", ASK_REMIND_FIRST)


def knob_author_active_min() -> float:
    return env_num("AGENTCTL_AUTHOR_ACTIVE_MIN", AUTHOR_ACTIVE_MIN)


def knob_blame_probes() -> float:
    return env_num("AGENTCTL_BLAME_PROBES", BLAME_PROBES)


def knob_condition_pending_lapse() -> float:
    return env_num("AGENTCTL_CONDITION_PENDING_LAPSE", CONDITION_PENDING_LAPSE)


def knob_issue_escalate_gap() -> float:
    return env_num("AGENTCTL_ISSUE_ESCALATE_GAP", ISSUE_ESCALATE_GAP)


def knob_recover_max() -> float:
    return env_num("AGENTCTL_RECOVER_MAX", RECOVER_BACKOFF_MAX)


def knob_unknown_grace() -> float:
    return env_num("AGENTCTL_UNKNOWN_GRACE", UNKNOWN_GRACE)
