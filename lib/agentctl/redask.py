# agentctl, part `redask`: the owner's question for a red a block holds: what it asks and how he answers, its category, its refusal, the reminder interval
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import re
from typing import Any
from agentctl.knobs import env_num
from agentctl.core import tilde
from agentctl.leases import red_block_of
from agentctl.conditions import PROBES
from agentctl.knobs import knob_ask_remind_max


# BLOCKED ON OWNER IS FOR THE OWNER'S CAUSES ONLY (the owner, 2026-10-28: "fix this: main #2283 → BLOCKED ON OWNER ...
# root cause and structural fix", "fix structurally so it doesn't false-flag this way" -- an escalation block raised for
# the CdpLaunchSpec crash on #2164 followed the red onto #2283 and hid its live fixer). A code red is never the owner's:
# a block needs an OWNER CAUSE (outside the code, that only the owner can act on), a red whose verdict is not a test
# failure, no live fixer, and the owner's exact command -- checked at the block AND again every tick (block_invalid:
# the tick renews a block lease only while it holds): a block that lost its justification lapses at once.
OWNER_CAUSES = {"external-account": "an external account only the owner holds (a provider dashboard, a plan)",
                "credentials": "credentials or secrets only the owner can issue or rotate",
                "infra-apply": "an infra apply only the owner may run",
                "deploy": "a deploy only the owner may run",
                "billing": "a billing action in an account only the owner holds",
                "dashboard": "an action in a dashboard only the owner can reach"}


def vault_decide_cmd(ns):
    return decide_cmd(f"vault-{ns}", "here")


# ---------------------------------------------------------------- WAITING ON YOU (the owner, 2026-11-03)
# "make it so blocked always communicates with me and reminds me of the decision". Before: convergence #728 read
# `BLOCKED: owner-decision down` for 15 h -- the owner had decided, the decided land failed its checks, and nobody said
# so; Nix update #1 sat BLOCKED ON OWNER (an App's access to grant) with no reminder, the question buried in ci-why
# text and hundreds of repeating RED OWNER / RED ESCALATE entries. Now:
#   - EVERY OWNER-BLOCKED RED HAS A QUESTION (red_ask): what to decide or do, the exact command or the answer to give,
#     and the red it blocks. The status line and ci-why show `<red> → WAITING ON YOU: <question>`, never a bare cause.
#   - THE OWNER IS TOLD ONCE (red_owner_reminders, the primary's tick): one escalate event -- the laptop's projector
#     writes ONE inbox entry (kind BLOCKED) and its notifier one macOS notification -- then REMINDED at
#     ASK_REMIND_FIRST, doubling up to ASK_REMIND_MAX, by a notification only (no inbox entry: one live reminder per
#     red, never one per tick or per fixer attempt), until the red leaves the block.
#   - THE OWNER ANSWERS with `agentctl decide <red> "<answer>"`: the answer is stored, the block lifted, the red reads
#     <its state> (decided HH:MMZ: <answer>) and is offered again -- its fixer is briefed with the answer (fixer_prompt). If the
#     decided action fails (NOT LANDED, GAVE UP, DIAGNOSED, its fix change rejected), the red goes back on the owner
#     with "you decided X; the action failed: Y" (red_decision_failed): never silently left as it was.
#   - It clears itself as before: the block's own probe (a newer run no longer failing this way, the workflow green),
#     the red verified or cleared; a cleared red is no longer blocked-external, so its reminders stop with it.
#   - THE QUESTION IS HELD AN HOUR (the owner, 2026-11-17: 28 of 42 questions resolved themselves before he answered,
#     p50 83 min): it is RECORDED at once (the red's blocked-external move, its `ask` with n 0), but the owner is told
#     only ASK_HOLD after its spell began -- unless the red is a GATE red (it holds every production push) or its
#     deadline falls inside the hold: those are told at once. A red that leaves the block meanwhile was never asked.
#   - ONLY A QUESTION THE WORLD MAY ANSWER IS HELD (the owner, 2026-11-18: a vault-unlock ask sat an hour, read as "a 7-d
#     deadline", while its fixers' tested fixes waited on signing). The 28 that resolved themselves were cleared by
#     their block's PROBE (a newer run no longer failing, the workflow green). A block no probe can lift -- the vault's
#     one lease (only `decide vault-<ns>` resumes it), any block a fixer raised with no probe -- waits on the owner
#     alone: its red's work stops until he answers, so it is told at once (ask_blocks_work). Decided by the block's
#     shape, never a per-kind list.
# THE OWNER-ONLY CATEGORIES (the owner, 2026-11-19: "could any of these decision things could've been handled
# automatically? fix structurally"). That night's six asks: a crash loop of the fixer harness (agentctl's own bug,
# already fixed in 852acad), a plain flaky-test race re-asked after the owner's answer, two ci-runner lane placements a
# ranking policy answers, a vault ask still shown after it had cleared, a workspace-trust prompt raised 21 minutes into
# a red, and one trust line written to the inbox five times. ONLY WHAT NO AGENT CAN DO REACHES THE OWNER: every ask
# names one of these categories (red_ask: an ask that names none is no ask; test_owner_only lints every source of one)
# and is routed back to its fixer otherwise (red_ask_route) -- or, for a fault of agentctl itself, to agentctl's
# maintainer as an issue (redcrash.py, red_harness_issue). A policy ask (`policy-no-rule`) proposes the rule row to add.
OWNER_ONLY = {
    "vault": "a Touch ID prompt: unlocking a vault, signing for a session -- only the owner's finger",
    "tty-trust": "a prompt that needs the owner's terminal (claude's workspace-trust dialog)",
    "money-credentials": "money or credentials: billing, an account or dashboard only the owner holds, a secret to "
                         "issue or rotate",
    "irreversible": "an irreversible or destructive action (a force push, dropped data, a deploy or infra apply only "
                    "the owner runs)",
    "publish-external": "publishing outside: a release, a post, a message under the owner's name",
    "policy-no-rule": "a policy decision no checked-in rule answers -- asked as the rule row to add",
}


# `red block --owner-cause` (redblock.py OWNER_CAUSES): each cause is one of the categories above
OWNER_CAUSE_CATEGORY = {"external-account": "money-credentials", "credentials": "money-credentials",
                        "billing": "money-credentials", "dashboard": "money-credentials", "infra-apply": "irreversible",
                        "deploy": "irreversible"}


POLICY_RULE = re.compile(r"\brule:\s*\S", re.I)


def ask_refusal(category, question):
    """Why an ask may not reach the owner (the lint and the runtime both ask this), or None."""
    if category not in OWNER_ONLY:
        return (f"it names no owner-only category ({category or 'none'}; one of {', '.join(OWNER_ONLY)}): the fixer "
                "proceeds under the repo's standing rules and checked-in policies")
    if category == "policy-no-rule" and not POLICY_RULE.search(str(question or "")):
        return "a policy ask proposes the rule row to add (`rule: <file>: <row>`): none given"
    return None


def ask_category(m):
    """The owner-only category of a block's ask (its meta), or None."""
    if m.get("vault"):
        return "vault"
    if m.get("category"):
        return m["category"]
    return OWNER_CAUSE_CATEGORY.get(m.get("owner_cause"))


ASK_REMIND_FIRST = 3600       # the first reminder this long after the owner was told ($AGENTCTL_ASK_REMIND_FIRST)


def decide_cmd(key, answer="<your answer>"):
    return f'~/bin/agentctl decide {key} "{answer}"'


def _ask_how(m: Any, key: Any, q: Any, until: Any) -> Any:
    """(q, how) of the owner's question for a block's meta m, or None when the block asks nothing of the owner's."""
    if m.get("decision"):
        q = q or m.get("decide") or "decide how this red is fixed"
        return q, f"answer: `! {decide_cmd(key)}` (look: `{m.get('cmd') or '~/bin/agentctl red status -v'}`)"
    if m.get("vault"):  # red_vault_block: ONE item per vault, resumed by one line (an unlock elsewhere is not theirs)
        return q, (f"at the machine: `! {vault_decide_cmd(m['vault'])}` resumes every red on it; each fixer's first "
                   "signature then asks Touch ID for its OWN session (your `secrets unlock` never unlocks theirs): answer it")
    if str(m.get("cmd") or "").strip():
        q = q or (f"run `! {m['cmd']}` ({OWNER_CAUSES.get(m.get('owner_cause'), 'an action only you can take')})")
        return q, f"it clears itself when {until}; or once done: `! {decide_cmd(key, 'done')}`"
    return None


def red_ask(it, b):
    """{q, how, until} of the owner's question for red `it` held by block b (red_block_of), or None when the block
    waits on nothing the owner does (a spent quota resets on its own)."""
    if not b:
        return None
    m, d = b.get("meta") or {}, it["data"]
    if m.get("wait_cause"):
        return None
    key, until = d.get("key"), m.get("until")
    q = m.get("question")
    category = ask_category(m)
    if ask_refusal(category, m.get("decide") or q):  # OWNER-ONLY: no category, no ask (a harness fault's block either)
        return None
    asked = _ask_how(m, key, q, until)
    if not asked:
        return None
    q, how = asked
    return {"q": tilde(" ".join(str(q).split())), "how": tilde(how), "until": until,
            "category": category}


def ask_interval(n):
    """The wait before reminder n+1 (n reminders so far, the first telling included): 1 h, 2 h, 4 h, 4 h, ..."""
    first = env_num("AGENTCTL_ASK_REMIND_FIRST", ASK_REMIND_FIRST)
    return min(first * 2 ** max(0, int(n) - 1), knob_ask_remind_max())


def red_decision(d: dict[str, Any]) -> dict[str, Any] | None:
    """The owner's decision on red d while it is being acted on, else None."""
    dec = d.get("decision") or {}
    return dec if dec.get("status") == "in-progress" and dec.get("answer") else None


# A fixer asks the owner with `decide[<category>]: <the decision>` (in a DIAGNOSED or GAVE UP verdict); a policy ask
# adds `rule: <where>: <the row to add>`. A bare `decide:` names no category: it is routed back (red_ask_route).
ASK_CATEGORY = re.compile(r"^\s*\[\s*([\w-]+)\s*\]\s*:?\s*", re.S)


def ask_split(decide):
    """(category, question) of a fixer's decide text: `[<category>]: <question>` or `[<category>] <question>`."""
    m = ASK_CATEGORY.match(str(decide or ""))
    return (m.group(1).lower(), str(decide)[m.end():].strip()) if m else (None, str(decide or "").strip())


ASK_HOLD = 3600               # s a question its block's probe may answer is held (recorded, not told) ($AGENTCTL_ASK_HOLD)


def ask_blocks_work(b):
    """Block b (red_block_of) waits on the owner alone: no probe of its own can lift it (ONLY A QUESTION THE WORLD MAY
    ANSWER IS HELD, above), so its reds' work stops until he answers."""
    return not b or ((b.get("meta") or {}).get("probe") or {}).get("kind") not in PROBES


def ask_held_until(members, spell, t, store):
    """Until when the owner's question for these reds ((item, ask) pairs) is held (THE QUESTION IS HELD, above), or
    None: tell now (no hold, a gate red, a deadline inside the hold, a block only the owner lifts, or the hold over)."""
    hold = env_num("AGENTCTL_ASK_HOLD", ASK_HOLD)
    until = float(spell) + hold
    if hold <= 0 or t >= until or any(m[0]["data"].get("gate") for m in members):
        return None
    if any(float(m[0].get("deadline") or until + 1) <= until for m in members):
        return None
    if any(ask_blocks_work(red_block_of(m[0], store)) for m in members):
        return None
    return until


def red_ask_mark(store, it, t, told=True):
    """The red just moved onto the owner. told: with an escalation of its own (its inbox entry and notification): that
    is the owner told for this spell -- the reminders count from here, never a second telling; else the question is
    held (n 0) and red_owner_reminders tells it when the hold ends."""
    if not it or it["state"] != "blocked-external":
        return
    ask = red_ask(it, red_block_of(it, store))
    if ask:
        rec = {"q": ask["q"], "since": it["entered_at"], "first_at": t, "last_at": t, "n": 1} if told else \
            {"q": ask["q"], "since": it["entered_at"], "held_at": t, "last_at": t, "n": 0}
        rec["category"] = ask.get("category")
        store.update_item("red", it["id"], it["version"], {"ask": rec})
