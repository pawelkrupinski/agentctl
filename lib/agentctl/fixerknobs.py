# agentctl, part `fixerknobs`: the fixer knobs: the background sessions, owners and orphans; the claude CLI and its settings
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from agentctl.knobread import env_num, env_text


# A FIXER IS A BACKGROUND CLAUDE SESSION (the owner, 2026-10-29: "I want fixers to be visible in ui"): `claude --bg -n
# <label>` starts it, so `claude agents` lists it by its label and the owner can `claude attach | logs | stop` it. The
# wrapper tracks it by the id --bg prints, never a child pid: `claude agents --json --all` says working / done, its
# transcript (~/.claude/projects/*/<session>.jsonl) is its progress and holds its final answer, `claude stop` ends it.
BG_POLL = 5  # seconds between the wrapper's looks at its session ($AGENTCTL_BG_POLL)


STALL_CPU_SECONDS = 1.0  # CPU that counts as progress per window: a watchdog's polls are not work


# ORPHANED WORK (the owner, 2026-10-28: "review and fix landing code ... so things don't get missed or stalled"; the
# audit found movies-wt-workflow-prune rejected 6x and idle 15 h, identity-impact / unit-before-push / red-local idle
# 12-19 h, flipflop / nodrift-cadence / unify / mongo-csot never landed at all): every maintain pass, a worktree whose
# commits are not on origin/main (by patch) and whose author is gone -- no process in it, no land, no live claim,
# idle ORPHAN_IDLE, no live session that ever named it -- gets ONE action: a re-land (its last verdict infra, unknown or
# none; only with LAND INTENT, never work NOT FOR LANDING, and from a throwaway tree, never the author's: see below and
# reland_worktree), or a dispatched fixer (a real failure: the checks rejected it). Still orphaned after that: UNLANDED ORPHAN, a condition whose probe clears
# when its commits are on main, or somebody works in it again. Nothing is ever deleted.
ORPHAN_IDLE = 2 * 3600


def knob_orphan_idle() -> float:
    return env_num("AGENTCTL_ORPHAN_IDLE", ORPHAN_IDLE)


def knob_bg_poll() -> float:
    return env_num("AGENTCTL_BG_POLL", BG_POLL)


def knob_stall_cpu_seconds() -> float:
    return env_num("AGENTCTL_STALL_CPU_SECONDS", STALL_CPU_SECONDS)


def knob_detached() -> bool:
    return bool(env_text("AGENTCTL_DETACHED"))  # set by spawn_detached: a background process never runs `secrets`


def knob_interactive() -> bool:
    return env_text("AGENTCTL_INTERACTIVE") == "1"  # 1: the vault may be asked though no terminal is attached


def knob_owners_dir() -> str:
    return env_text("AGENTCTL_OWNERS_DIR")  # the owners' directory (default owners under the state root)


def knob_claude() -> str:
    return env_text("AGENTCTL_CLAUDE")  # the `claude` CLI to launch (tests' fake); default: PATH, ~/.local/bin


def knob_claude_settings() -> str:
    return env_text("AGENTCTL_CLAUDE_SETTINGS")  # the interactive sessions' settings.json (default ~/.claude/settings.json)


# LAND AS YOU GO (the owner, 2026-10-29: fixer -7 of #2473 sat on five unlanded commits for hours, and the import that
# took its lease away left them stranded). The laptop watches every working fixer's worktree: commits not on
# origin/main for FIXER_UNLANDED_NUDGE (by author time: a rebase keeps it) get a RED-NUDGE.md there, which its prompt
# tells it to act on; still unlanded at FIXER_UNLANDED_ESCALATE, one RED ESCALATE inbox entry. Both clear themselves
# once the commits are on main (RECOVERED after an escalation).
FIXER_UNLANDED_NUDGE = 45 * 60


FIXER_UNLANDED_ESCALATE = 90 * 60


FIXER_CLAIM_WITHIN = 600  # s a started session has to start its transcript / run `red claim` ($AGENTCTL_FIXER_CLAIM_WITHIN)


# A FINISHED TURN WHOSE PROCESS LIVES ON (2026-11-01, convergence #579 / Verify #1 `fixer-crash-loop`): claude now keeps
# a background session's process (`claude bg-spare`) after its turn, its row `blocked` / `idle` with a live pid. bg_ended
# read that as "maybe waiting on its own background work" and the wrapper waited for progress that never came: the
# owner's turns ended with GAVE UP / DIAGNOSED, the wrapper never read it, each red's lease lapsed OWNER_TTL later as
# "lease expired", and three such lapses blocked the red on the owner as a crash loop. The transcript decides now
# (bg_turn): a waiting session whose turn's final answer carries a verdict has ended its turn; one with no background
# task of its own pending, quiet for BG_IDLE_GRACE, has ended it with no verdict; one waiting on its own background task
# (`agentctl wait`, backgrounded before it ended its turn, as agents are told) is at work, and each beat of that wait is
# progress (bg_wait_mark), so its lease never lapses under it.
BG_IDLE_GRACE = 60   # s a waiting session with no pending task of its own and no verdict stays quiet: its turn ended


ORPHAN_FIXERS_MAX = 2  # orphan fixers alive at once ($AGENTCTL_ORPHAN_FIXERS); one dispatched a pass


BG_SPENT_GRACE = 120  # s a session must have run before the reaper judges it ($AGENTCTL_BG_SPENT_GRACE)


OWNER_IDLE = 6 * 3600        # no red held this long: the owner ends ($AGENTCTL_OWNER_IDLE)


# OWNERSHIP FOLLOWS ACTIVITY (the owner, 2026-11-03: movies-wt-unmatched-imdbslip escalated UNLANDED ORPHAN four times
# while an agent was landing it -- the agent's Claude session runs from another directory and works in the worktree by
# absolute path, so no process had its cwd there). A LIVE Claude session -- any transcript of it (the session's own or
# one of its subagents') written within OWNER_ACTIVE -- owns every worktree a tool call of it EVER named (or whose
# project directory is the worktree's): no orphan action, and an open UNLANDED ORPHAN clears ("a live session works in
# it"). Ownership lapses when the session goes quiet.
# 2026-11-06 22:51Z (es-gate-check): the rule used to ask for a tool call naming the worktree within the last 30 min, in
# the transcript's last 256 KB. The subagent that made ~/projects/movies-wt/es-gate-check had named it last 104 min
# before while it waited on CI from a sibling worktree, its transcript written minutes before: alive, and its worktree
# was taken for an orphan, rebased and queued. Every transcript of a live session is now read whole -- incrementally,
# from where the last pass stopped (SESSION_PATHS, a cache of the paths each one named) -- and `~/` / `$HOME/` paths
# count as the absolute paths they are.
OWNER_ACTIVE = 2 * 3600


# A SESSION PAST ITS CONTEXT LIMIT STARTS FRESH (the owner, 2026-11-25: "fix"). Every turn of a family owner resumed the
# SAME session: outside-gate reached ~985k tokens of context by turn 134, so each resume reloaded close to a million
# tokens (a large share of the weekly limit) and one took 32 min to write its first transcript line. Before a resume
# agentctl reads the session's context from its transcript -- the last assistant turn's input + cache-read +
# cache-creation tokens -- and past CONTEXT_FRESH it starts a fresh session instead, its first prompt carrying a short
# handover built from agentctl's own store and records (never asked of the old session): the reds in hand, the
# worktree's branch and dirty files, the last verdicts, the open issues and the old session's last answers. An
# unreadable transcript, or one with no usage figures, resumes as before.
CONTEXT_FRESH = 300000       # tokens: a session whose last turn's context is past this is not resumed but started fresh


def knob_bg_idle_grace() -> float:
    return env_num("AGENTCTL_BG_IDLE_GRACE", BG_IDLE_GRACE)


def knob_bg_spent_grace() -> float:
    return env_num("AGENTCTL_BG_SPENT_GRACE", BG_SPENT_GRACE)


def knob_context_fresh() -> float:
    return env_num("AGENTCTL_CONTEXT_FRESH", CONTEXT_FRESH)


def knob_fixer_claim_within() -> float:
    return env_num("AGENTCTL_FIXER_CLAIM_WITHIN", FIXER_CLAIM_WITHIN)


def knob_fixer_unlanded_escalate() -> float:
    return env_num("AGENTCTL_FIXER_UNLANDED_ESCALATE", FIXER_UNLANDED_ESCALATE)


def knob_fixer_unlanded_nudge() -> float:
    return env_num("AGENTCTL_FIXER_UNLANDED_NUDGE", FIXER_UNLANDED_NUDGE)


def knob_orphan_fixers() -> float:
    return env_num("AGENTCTL_ORPHAN_FIXERS", ORPHAN_FIXERS_MAX)


def knob_owner_active() -> float:
    return env_num("AGENTCTL_OWNER_ACTIVE", OWNER_ACTIVE)


def knob_owner_idle() -> float:
    return env_num("AGENTCTL_OWNER_IDLE", OWNER_IDLE)


def knob_allowed_roots() -> str:
    return env_text("AGENTCTL_ALLOWED_ROOTS")  # colon-separated roots the allowed-path check accepts besides its own


def knob_claude_projects() -> str:
    return env_text("AGENTCTL_CLAUDE_PROJECTS")  # the Claude projects directory (default ~/.claude/projects)


def knob_claude_jobs() -> str:
    return env_text("AGENTCTL_CLAUDE_JOBS")  # the Claude jobs directory (default ~/.claude/jobs)


def knob_claude_tmp() -> str:
    return env_text("AGENTCTL_CLAUDE_TMP")  # the Claude session's scratch directory


def knob_allow() -> str:
    return env_text("AGENTCTL_ALLOW")  # extra allowed paths, space-separated
