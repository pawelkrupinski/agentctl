# ---------------------------------------------------------------- THE POLICY REGISTRY and THE HOLD TABLE (2026-11-20)
# The owner, 2026-11-20: "fix agentctl issues structurally". Of agentctl's 146 closed issues, 9 were a rule nobody owned
# (an invented 24-h reject, a governor order, a gate read two ways) and 46 a state that moved, or sat, where no table
# said it could. Two tables close both:
#
# POLICIES: every rule that HOLDS, REJECTS or DELAYS a change, one row each, with WHO DECIDED IT -- the owner's words
# and date, or None: agent-proposed, unapproved. An unapproved rule never blocks: `policy_blocks` answers False for it
# and records one `policy.unapproved` event naming the change it would have held, so its effect is visible and the
# owner can approve it (fill `decided`) or strike it. A `mechanism` row is no rule but a resource wait (a busy tree, an
# executor starting, a push under way): it needs no decision. tests/test_policy.py fails any hold kind, queue reject or
# batch reject in lib/agentctl/ that names no row here, and any row nothing uses.
#
# HOLDS: every kind of recorded hold (queuebatch.py note_hold) -- its `land status` label, the longest it may last before it
# is STUCK, and what the tick does then (its recovery). note_hold refuses an unknown kind, change_hold_stuck reads the
# bound, and the change tick runs the recovery: no hold outlives its bound in silence.

from agentctl.core import err, process_cache
from agentctl.statemachines import machine_state


class Policy:
    """A rule: id, effect (hold / reject / delay), the rule in one line, who decided it (None: agent-proposed), whether
    it is a resource wait (mechanism), and `softens`: the mechanism outcome an unapproved rule only makes milder (a
    hold in place of that reject) -- such a rule acts before it is approved, since switching it off blocks more."""
    __slots__ = ("id", "effect", "rule", "decided", "mechanism", "softens")

    def __init__(self, id, effect, rule, decided=None, mechanism=False, softens=None):
        assert effect in ("hold", "reject", "delay"), effect
        self.id, self.effect, self.rule, self.decided, self.mechanism = id, effect, rule, decided, mechanism
        self.softens = softens

    @property
    def approved(self):
        return self.mechanism or bool(self.decided)


POLICIES = {p.id: p for p in (
    # -- resource waits (mechanism: no rule, nothing to decide)
    Policy("trees", "delay", "a check waits for a free check tree", mechanism=True),
    Policy("starting", "delay", "a check waits while its executor starts", mechanism=True),
    Policy("push", "delay", "a push waits for the landing lock or a push tree, or is under way", mechanism=True),
    Policy("retry", "delay", "an infra failure's check or push is retried after its backoff", mechanism=True),
    Policy("governor", "delay", "a local check waits for the governor's slot", mechanism=True),
    Policy("stacked", "hold", "a change whose branch carries another open change's commits waits for it, landing "
           "after it, checked on top of it (holdgraph.py A STACKED CHANGE WAITS FOR ITS PARENT)", mechanism=True),
    Policy("checks-failed", "reject", "a change whose pre-push checks fail is NOT LANDED", mechanism=True),
    Policy("no-longer-applies", "reject", "a change that no longer applies on main is NOT LANDED (rebase and land again)",
           mechanism=True),
    Policy("batch-checks-failed", "reject", "a batch member the bisect blames is NOT LANDED", mechanism=True),
    # -- rules the owner decided
    Policy("red-gate", "hold", "a production change waits on `waiting` while main's gate is red (unless it fixes it)",
           decided="the owner, 2026-10-27: red builds gate every push to main (CLAUDE.md, the owner's gate)"),
    Policy("batch", "hold", "a solo check or push waits while a batch it is not in assembles or pushes",
           decided="the owner, 2026-11-17: \"batch whenever there's a backlog, not only on red\""),
    Policy("backlog-batch", "hold", "a batchable queued change waits for the next backlog batch",
           decided="the owner, 2026-11-17: \"batch whenever there's a backlog, not only on red\""),
    Policy("push-batch", "hold", "a checked change's push (or verify round) waits for the next push batch",
           decided="the owner, 2026-11-17: \"combine them instead of landing them one by one\""),
    Policy("verify", "hold", "a younger push waits for an older change's combined-tree verification",
           decided="the owner, 2026-11-19 (R3): no unverified combined tree reaches main (simulator invariant 11)"),
    Policy("overlap", "hold", "a change sharing a file with an older open one is checked once, after it",
           decided="the owner, 2026-11-20: \"so that rechecks are kept to a minimum\" (MINIMAL RECHECKS)"),
    Policy("hold-trailer", "reject", "a commit with a `Hold: <why>` trailer lands only on `land --release-hold`",
           decided="the owner, 2026-11-06 (es-gate-check): held work lands only on the owner's go"),
    Policy("drift-check", "reject", "a batch member the tip's identity drift check blames is NOT LANDED",
           decided="the owner, 2026-10-29 (G5): a batch must not hide which identity change moved a take"),
    Policy("blame-dead", "reject", "a withdrawn or refuted blame revert never lands",
           decided="the owner, 2026-11-01: \"revert the offending change\" -- a refuted revert reverts no offender"),
    Policy("older-batch", "hold", "a lone change younger than a held batch never takes the last free check tree",
           decided="the owner, 2026-11-20: \"4 confirm\" -- an older waiting change goes ahead of a younger one in a "
                   "batch; red fixes keep their own lane (DERIVED from his 2026-11-17 \"batch whenever there's a "
                   "backlog\": without it no backlog batch forms under a stream of lone changes, the simulator's "
                   "loadlone profile fails I5 with it off, tests/test_queue_sim.py ee7b744)"),
    Policy("regen", "delay", "a change whose generated rows only a regen decides is held for that regen (not bounced); "
           "overdue, the regeneration is run again and, unanswered, the owner told once -- never rejected",
           decided="the owner, 2026-11-20: \"1 approve\" (the hold) and \"2 drop the reject\" (lane F2b's 24-h "
                   "regen-expiry reject struck: an overdue hold recovers instead)", softens="checks-failed"),
    Policy("fix-replay", "reject", "a Fixes-Red landing for a GATE red (Main, Identity model convergence, Identity "
           "takes) needs `Fix-Replay: <command>`, which agentctl runs to see base=FAIL head=PASS, or `Fix-Replay: none "
           "— <why>` (a specific reason, 30+ characters, listed in the owner's weekly report)",
           decided="the owner, 2026-11-20: \"3 approve and enforce for gate reds\""),
    # -- rules that WARN, never block: agent-proposed and undecided, or (fix-replay-outside) kept warn-only by the
    # owner's own decision -- 2026-11-20, "enforce for gate reds": a red outside the gate stays warn-only
    Policy("fix-replay-outside", "reject", "a Fixes-Red landing for a red OUTSIDE the gate whose workflow names a "
           "replay (red-workflows.tsv) needs Fix-Replay: <it> base=FAIL head=PASS (Alert backtest, 2026-11-19)",
           decided=None),
)}


def policy(pid):
    try:
        return POLICIES[pid]
    except KeyError:
        raise ValueError(f"no policy {pid!r} in the registry (policy.py POLICIES): register the rule, with who "
                         "decided it, before it may hold or reject a change") from None


_unapproved_said = process_cache({})  # (policy id, subject) -> True: its warning recorded


def policy_blocks(pid, st, subject=None, what=None):
    """Whether the rule `pid` may hold or reject: True for a mechanism or an owner-approved rule; False for an
    unapproved one, which only WARNS -- one `policy.unapproved` event per (rule, subject), on the subject's change item
    when it has one (`what`: what it would have done)."""
    p = policy(pid)
    if p.approved:
        return True
    key = (pid, subject)
    if not _unapproved_said.get(key):
        _unapproved_said[key] = True
        line = (f"POLICY UNAPPROVED {pid}: would {p.effect} {subject or 'a change'}"
                + (f" ({what})" if what else "") + f" -- {p.rule}; agent-proposed, never decided by the owner, so it "
                "only warns (policy.py POLICIES)")
        err(line)
        try:
            it = st.item("change", subject) if subject else None
            if it and not machine_state("change", it["state"]).terminal:
                st.transition("change", subject, it["state"], it["version"],
                              event={"kind": "policy.unapproved", "text": line[:400], "severity": "info",
                                     "refs": {"policy": pid}})
        except Exception:
            pass  # a warning, never a failure
    return False


def unapproved_policies():
    return [p for p in POLICIES.values() if not p.approved]


class HoldKind:
    __slots__ = ("kind", "policy", "label", "bound", "recovery", "red_fix_ok")

    def __init__(self, kind, policy_id, label, bound, recovery, red_fix_ok=False):
        assert recovery in ("rederive", "escalate", "requeue", "recover"), recovery
        self.kind, self.policy, self.label, self.bound, self.recovery = kind, policy_id, label, float(bound), recovery
        self.red_fix_ok = red_fix_ok  # a red fix may wait on it (its own resources: never on other work)


# bound: how long a hold of this kind may last (from when it was first recorded) before it is STUCK. recovery: what the
# change tick does with a stuck one -- `rederive` drops the recorded hold so the same tick decides afresh (a blocker
# gone, a batch over: the hold describes a world that moved), `escalate` says it to its author and the watchdog (the
# wait is real, its holder's own machine is late), `requeue` puts the change back in the queue (a push that never ended),
# `recover` -- the hold's own recovery, run by the change tick at its bound and never a stuck lap: a REGEN NEEDED hold
# overdue runs the regeneration again and, still held, tells the owner once (queueclose.py regen_hold_overdue; the owner,
# 2026-11-20: "drop the reject").
HOLDS = {h.kind: h for h in (
    HoldKind("trees", "trees", "every check tree is busy", 6 * 3600, "escalate", red_fix_ok=True),
    HoldKind("starting", "starting", "its executor is starting its check", 10 * 60, "rederive", red_fix_ok=True),
    HoldKind("push", "push", "its push is under way or waits for the landing lock", 30 * 60, "rederive",
             red_fix_ok=True),
    HoldKind("retry", "retry", "an infra failure's retry", 6 * 3600, "escalate", red_fix_ok=True),
    HoldKind("governor", "governor", "the governor's check slot", 6 * 3600, "escalate", red_fix_ok=True),
    HoldKind("regen", "regen", "its agent's regeneration", 24 * 3600, "recover"),
    HoldKind("stacked", "stacked", "the change its branch is stacked on", 6 * 3600, "rederive"),
    HoldKind("overlap", "overlap", "an older change it overlaps", 6 * 3600, "rederive"),
    HoldKind("batch", "batch", "a batch it is not in", 4 * 3600, "rederive"),
    HoldKind("backlog-batch", "backlog-batch", "the next backlog batch", 2 * 3600, "rederive"),
    HoldKind("push-batch", "push-batch", "the next push batch", 2 * 3600, "rederive"),
    HoldKind("verify", "verify", "an older change's combined-tree verification", 4 * 3600, "rederive"),
    HoldKind("older-batch", "older-batch", "the older changes' batch takes the last free tree", 2 * 3600, "rederive"),
)}


def hold_kind(kind):
    try:
        return HOLDS[kind]
    except KeyError:
        raise ValueError(f"no hold kind {kind!r} in policy.py HOLDS: every recorded hold has a label, a bound and a "
                         "recovery") from None
