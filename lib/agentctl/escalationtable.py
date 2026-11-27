# agentctl, part `escalationtable`: THE ESCALATION DISPATCH TABLE: every escalation kind mapped to an agent (its brief, repo, model, runtime and retry budget) or to the owner alone
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.
#
# THE OWNER (2026-11-26): "can agentctl just kick off an agent when sth escalates?" Until then agentctl dispatched
# fixers for reds only; every other escalation -- a watchdog invariant, a condition, STALLED, a stranded change --
# reached the owner's inbox and waited for him. An ESCALATION is an open condition (conditions.py) past its bound; its
# KIND is the condition's kind, an invariant's refined by its id (`INVARIANT:W2`). THIS TABLE, in one place and as
# data, says for every kind whether an agent may take it (escalations.py offers it, leases it and starts a `claude
# --bg` session on the red dispatcher's offers and leases) or whether it is the OWNER'S ALONE: a process agentctl did
# not start, the vault or Touch ID, an outage or a reboot, a force push, the repository's visibility, a decision the
# policy registry leaves to him. A kind the table does not name is the owner's (UNKNOWN_KIND_ROUTE): agentctl never
# starts an agent on a condition nobody judged.

import re
from dataclasses import dataclass
from typing import Any, Optional

TO_AGENT, TO_OWNER = "agent", "owner-only"
ESCALATION_FIXERS = 2           # escalation fixers alive at once, every kind together (the red fixers' cap is apart)
SONNET_RUNTIME, OPUS_RUNTIME = 60 * 60, 120 * 60   # s: a session's wall time before it is stopped and counted failed
RETRY_BACKOFF = 10 * 60         # s after a failed try before the next; doubled each try
DISPATCH_AFTER = 10 * 60        # s a condition is open before it is an escalation (its first deadline: store.py)
SELF_HEALING_AFTER = 30 * 60    # s for a heartbeat invariant (alertroute.py ALERT_SELF_HEALING_INVARIANTS' bound)


@dataclass(frozen=True)
class EscalationRoute:
    """Where one escalation kind goes. mode: TO_AGENT or TO_OWNER. For an agent: the repository it works in (`bin`:
    agentctl's ~/bin, `movies`: ~/projects/movies), its model, its wall time (s), its tries before the owner is told,
    the task its brief gives, how long the condition is open before it is dispatched (s), and `owner_when`: a pattern
    of the condition's text that makes this one the owner's after all (a runaway process agentctl did not start).
    why: the reason, for an owner-only kind its ask.py category first."""
    mode: str
    why: str
    repo: str = "bin"
    model: str = "sonnet"
    runtime: int = SONNET_RUNTIME
    tries: int = 3
    task: str = ""
    after: int = DISPATCH_AFTER
    owner_when: str = ""


def agent_route(task: str, model: str = "sonnet", after: int = DISPATCH_AFTER, owner_when: str = "",
                repo: str = "bin") -> EscalationRoute:
    """An agent's row: sonnet for a known mechanism (60 min, 3 tries), opus for a root cause nobody knows (120 min,
    2 tries)."""
    opus = model == "opus"
    return EscalationRoute(TO_AGENT, task, repo=repo, model=model, runtime=OPUS_RUNTIME if opus else SONNET_RUNTIME,
                           tries=2 if opus else 3, task=task, after=after, owner_when=owner_when)


def owner_route(category: str, why: str) -> EscalationRoute:
    return EscalationRoute(TO_OWNER, f"[{category}] {why}")


UNKNOWN_KIND_ROUTE = owner_route("policy-no-rule", "no row of the dispatch table names this kind: nobody judged "
                                                   "whether an agent may take it")

# Text that makes ANY kind the owner's (ask.py's owner-only categories): the vault or Touch ID, a credential, a reboot
# or an outage, a force push, the repository's visibility, a decision asked of the owner.
OWNER_SIGNALS = re.compile(r"secrets unlock|\bvault\b|Touch ID|credential|\breboot|outage|uplink|force[- ]push"
                           r"|visibility|go(?:es)? public|WAITING ON YOU|decide\[|\bsudo\b", re.I)

DISPATCH_TABLE: dict[str, EscalationRoute] = {
    # the watchdog's invariants (invariants.py INVARIANTS), each agentctl's own machinery
    "INVARIANT:W1": agent_route("an unlanded change of the queue is past its bound: find what holds it (its item, "
                                "its hold, the gate, the checks) and fix the queue code that let it sit", "opus"),
    "INVARIANT:WR": agent_route("a red fix waits for green or sits unlanded: find why the queue holds it and fix that",
                                "opus"),
    "INVARIANT:W2": agent_route("a red has no lease, offer, fix or owner block: find the red machine's gap", "opus"),
    "INVARIANT:W5": agent_route("the processor's duties do not beat: find the stuck step (its log, `agentctl "
                                "status`) and fix it", after=SELF_HEALING_AFTER),
    "INVARIANT:W6": agent_route("escalation events are left unnotified: find why the notifier does not deliver them"),
    "INVARIANT:W7": agent_route("the laptop's sync falls behind ci-runner's events: find the slow or failing step",
                                after=SELF_HEALING_AFTER),
    "INVARIANT:W8": agent_route("a change left the queue with no event to its author: find the exit that says "
                                "nothing", "opus"),
    "INVARIANT:WA": agent_route("agentctl's own main CI is red or its deploy is stuck: fix the red (tests.yml)"),
    "INVARIANT:WM": agent_route("`agentctl maintain` (the reaper) has not run: find why its launchd job does not",
                                after=SELF_HEALING_AFTER),
    "INVARIANT:WP": agent_route("a process agentctl started runs past its memory or CPU limit: find what it is and "
                                "why, and fix the code that lets it run away (never stop a process you did not "
                                "start)", owner_when=r"agentctl never stops a process it did not start"
                                                    r"|the owner's to look at"),
    "INVARIANT:WT": agent_route("the test suite's temporary roots pile up: find the runner that leaves them"),
    "INVARIANT:WU": agent_route("the store tunnel listens but does not answer: tell a wedged forward from a slow "
                                "uplink (AGENTCTL.md, Troubleshooting) and fix agentctl's side"),
    "INVARIANT:WB": owner_route("irreversible", "uncommitted edits in ~/bin are somebody's work in progress: only "
                                                "their author or the owner may commit or drop them"),
    "INVARIANT:WN": owner_route("tty-trust", "macOS notifications fail on both channels: their permission is the "
                                             "owner's to grant in his GUI session"),
    "INVARIANT:WF": owner_route("irreversible", "the file table is near its limit: its release runs sudo in the VM"),
    "INVARIANT:WD": owner_route("irreversible", "the disk is low after the upkeep's cleanup: what is left to free is "
                                                "the owner's files"),
    # the other conditions
    "STALLED": agent_route("the queue or a push is stalled: find the head blocker and fix the code that stalls on "
                           "it", "opus", owner_when=r"push-credential|credential|tunnel|uplink"),
    "change-stranded": agent_route("a change is stranded in the queue: find why no step moves it and fix it",
                                   "opus"),
    "CLONE": agent_route("a clone on ci-runner is unhealthy: find why its heal does not repair it"),
    "UNKNOWN": agent_route("the reconciler cannot read the gate: find why the read fails and fix it", "opus",
                           owner_when=r"token unavailable|NO READ ACCESS"),
    "CRASH_LOOP": agent_route("a fixer crash-loops at launch: every launch died within seconds of starting of one cause "
                              "(the evidence below: each launch's end and log). Find why the launch dies -- the wrapper, "
                              "`claude --bg`, its environment, the worktree -- and fix that harness code (an owner-only "
                              "cause, the vault or a credential, is never yours: say BLOCKED ON OWNER)", "opus"),
    "NETWORK": owner_route("irreversible", "the laptop's network or the tunnel is out: an outage no agent can "
                                           "reach past"),
    "UNLANDED_ORPHAN": owner_route("policy-no-rule", "an orphaned worktree whose own orphan fixer already ran: "
                                                     "keeping or dropping its commits is the owner's"),
}

# THE FIXER CRASH LOOP (crashloop.py): its condition's kind and key per subject (`red:<id>`, `condition:<id>`), and the
# hold both dispatchers ask before they launch that subject's fixer again -- here, below both, so it is ONE rule
CRASH_LOOP_KIND = "CRASH_LOOP"


def crash_loop_key(subject: str) -> str:
    return "crash-loop-" + re.sub(r"[^\w.-]+", "-", subject)


def crash_loop_hold(open_conditions: list[dict[str, Any]], subject: str) -> Optional[str]:
    """Why the subject's fixer is not launched now -- its CRASH_LOOP condition (an item) is open -- or None."""
    key = crash_loop_key(subject)
    for it in open_conditions:
        d = it.get("data") or {}
        if d.get("kind") == CRASH_LOOP_KIND and d.get("key") == key and it.get("state", "open") == "open":
            return f"held: its fixer crash-loops at launch (condition {it['id']}): {str(d.get('text'))[:160]}"
    return None


INVARIANT_ID = re.compile(r"INVARIANT (W\w+)")


def escalation_kind(condition: dict[str, Any]) -> str:
    """The kind a condition escalates as: its kind, an invariant's refined by its id (from its text, else its key)."""
    kind = str(condition.get("kind") or "")
    if kind != "INVARIANT":
        return kind
    m = INVARIANT_ID.match(str(condition.get("text") or ""))
    wid = m.group(1) if m else str(condition.get("key") or "").removeprefix("invariant-")
    return f"INVARIANT:{wid}"


def escalation_route(condition: dict[str, Any]) -> EscalationRoute:
    """The route of a condition: its kind's row, the owner's when its text says the owner must act (OWNER_SIGNALS, the
    row's own owner_when), the owner's when no row names its kind."""
    route = DISPATCH_TABLE.get(escalation_kind(condition), UNKNOWN_KIND_ROUTE)
    text = str(condition.get("text") or "")
    if route.mode == TO_AGENT and (OWNER_SIGNALS.search(text) or (route.owner_when and re.search(route.owner_when, text))):
        return owner_route("owner-signal", f"its text names what only the owner can do: {route.why}")
    return route


def escalation_repos() -> set[str]:
    """The repositories an escalation agent may work in (each needs claude's workspace trust)."""
    return {r.repo for r in DISPATCH_TABLE.values() if r.mode == TO_AGENT}
