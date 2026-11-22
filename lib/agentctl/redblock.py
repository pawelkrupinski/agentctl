# agentctl, part `redblock`: an unreadable gate, blocks on the owner's causes, owner blocks as leases.
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import re
import time
from agentctl.classify import QUOTA_LOG
from agentctl.store import open_store
from agentctl.forge import GH_CLI, RED_CONCLUSIONS
from agentctl.repos import scan_repo_for
from agentctl.workflows import LOCAL_FLOW
from agentctl.redrecord import lease_is_live, red_active, red_lease, red_what
from agentctl.leases import lease_meta
from agentctl.inboxentry import inbox_write
from agentctl.conditions import PROBES, condition_item_of, open_condition, run_probe
from agentctl.redrepos import red_gate_flows
from agentctl.redask import OWNER_CAUSES, ask_category, ask_refusal
from agentctl.red import red_item_of_run
from agentctl.ciwhy import _gate_lists, _outside_lists, resolve_run
from agentctl.redseen import red_units
from agentctl.redobserve import local_units
from agentctl.runsigs import run_sigs


# -- an unreadable gate: no dispatch, one UNKNOWN per spell, an infra check
def infra_check(o_r):
    """The gate is read from github.com with gh's own auth: no tunnel, no vault token in the way."""
    return f"read with {GH_CLI} (`gh auth status` names its auth; https://www.githubstatus.com for an outage)"


def gate_unread_spell(o_r, why):
    """The patrol could not read the gate: no dispatch, ONE `UNKNOWN:` condition per spell, with an infra check; the
    gate_read probe (a snapshot written after it opened) clears it."""
    if why is None or condition_item_of(open_store(), "gate-unread"):
        return None
    line = (f"UNKNOWN: the red patrol cannot read the gate ({why[:200]}): nothing dispatched, pushes fail closed. "
            f"Infra check: {infra_check(o_r)}")
    c = open_condition("UNKNOWN", line, run_probe("gate_read", {"repo": o_r, "since": time.time()}), [],
                       key="gate-unread", command="red", exit_code=2)
    return line if c else None


def red_run_units(o_r, n):
    """The units of one red run (for claim / dispatch / block by run); `local`: main's own local-checks red."""
    if str(n) == "local":
        us = local_units(scan_repo_for(o_r))
        return LOCAL_FLOW, (us[0]["run"] if us else {"number": "local"}), us
    name, r = resolve_run(o_r, n)
    if r is None:
        return name, None, []
    if r.get("status") == "completed" and r.get("conclusion") in RED_CONCLUSIONS:
        runs = _gate_lists.get(name) or _outside_lists.get(name) or [r]
        gate = name in {g for g, _ in red_gate_flows(o_r)}
        try:
            sigs = run_sigs(o_r, r, name)
        except (Exception, SystemExit):
            sigs = []
        return name, r, red_units(o_r, name, r, runs, sigs, gate)
    return name, r, []


# AN OWNER COMMAND THAT DELETES IS SCOPED AND GUARDED (issue 20261110T084827-38f01b, P0). `red block` handed the owner
# `sudo rm -rf /run/github-runner/bitcashier-gh-builder-{1,4,6,7,8,12}/core/core/docker`; he ran it 8 h later, while
# builders 4, 6 and 12 ran Main Pipeline #939, and docker/ vanished under all three jobs (red #939). An owner command
# runs whenever the owner gets to it, so what was idle when the fixer looked may be busy then: a deletion names each
# path (no brace, glob, bare variable or shallow root), carries its own check before it deletes (if / test / [ /
# pgrep), and one inside a CI runner's workspace skips a slot whose Runner.Worker runs. `red block` refuses anything
# else, and a block raised before this rule carrying one lapses at the next tick (block_invalid).
OWNER_CMD_DELETES = re.compile(r"^(rm|rmdir|unlink|shred|truncate)$")
OWNER_CMD_GUARDS = {"if", "test", "[", "[[", "pgrep", "while", "until"}
OWNER_CMD_RUNNER_WS = re.compile(r"github-runner|actions-runner|/_work(/|$)")
OWNER_CMD_SPLIT = re.compile(r"\s*(?:&&|\|\||;|\||\n|\bdo\b|\bthen\b|\belse\b)\s*")


def owner_cmd_target_unscoped(arg):
    """Why one path a deletion names is not one named path, or None."""
    if re.search(r"[*?\[\]{}`]|\$\(", arg):
        return f"{arg!r} expands (a glob, brace or substitution): name each path"
    if re.fullmatch(r"\$\{?\w+\}?/?", arg) or arg in (".", "..", "-"):
        return f"{arg!r} names no path"
    literal = re.sub(r"\$\{?\w+\}?", "x", arg)
    if literal.startswith(("~", "$HOME", "/")) and len([p for p in literal.lstrip("~").split("/") if p]) < 3:
        return f"{arg!r} is too near the root"
    return None


def owner_cmd_unsafe(cmd):
    """Why the owner must not be handed this command as it stands (it deletes unscoped or unguarded), or None."""
    flat = str(cmd or "").replace("'", " ").replace('"', " ")
    segments = [s.split() for s in OWNER_CMD_SPLIT.split(flat)]
    deleting, guarded = [], False
    for words in segments:
        verbs = [w for w in words if w not in ("sudo", "exec", "command", "nice", "ionice") and not w.startswith("-")]
        if not verbs:
            continue
        head = verbs[0]
        guarded = guarded or bool(OWNER_CMD_GUARDS & {words[0], head})
        if head in ("docker", "podman") and "prune" in words or head == "find" and "-delete" in words or \
                head == "kubectl" and "delete" in words and ("--all" in words or "-A" in words) or \
                re.search(r"\bdrop(Database|Collection)\b|\b(DROP|TRUNCATE)\s+(TABLE|DATABASE)\b", " ".join(words)):
            return (f"unscoped: `{' '.join(words)[:80]}` deletes whatever it matches, not named paths -- hand the owner "
                    "a command naming each thing it removes")
        if OWNER_CMD_DELETES.match(head) or head == "git" and ("clean" in words or "--hard" in words) or \
                head in ("docker", "podman", "kubectl") and ({"rm", "rmi", "delete"} & set(words)):
            targets = verbs[1:] if OWNER_CMD_DELETES.match(head) else []
            for arg in targets:
                why = owner_cmd_target_unscoped(arg)
                if why:
                    return f"unscoped: {why}"
            deleting.append((targets, guarded))
    for targets, was_guarded in deleting:
        if any(OWNER_CMD_RUNNER_WS.search(t) for t in targets) and "Runner.Worker" not in flat:
            return ("it deletes inside a CI runner's workspace without skipping a busy slot: check each slot's "
                    "Runner.Worker first (`pgrep -f \"<slot>/.*Runner.Worker\" >/dev/null || rm -rf <its path>`), as the "
                    "owner may run it hours later, mid-job")
        if not was_guarded:
            return ("unguarded: it deletes with no check before it (if / test / [ / pgrep): the owner runs it whenever "
                    "he gets to it, so it must check what it removes is still unused then")
    return None


def owner_details(o_r, n, name, units, cmd, words, owner_cause):
    """The owner's whole picture, one line each part: what fails, what was tried, why only the owner, the command, how
    it clears (the owner, 2026-10-28: "more details in the future pls")."""
    u = units[0] if units else {}
    failing = f"{u.get('job') or '?'}: {str(u.get('line') or 'no error line read')[:160]}"
    try:
        its = red_item_of_run(open_store(), n, name)
    except Exception:
        its = []
    hist = [h for it in its for h in it["data"].get("history") or []]
    verdicts = [f"{h.get('holder')}: {str(h.get('outcome'))[:90]}" for h in hist]
    refuted = sorted({x[:9] for it in its for x in it["data"].get("refuted") or []})
    tried = (f"{len(refuted)} fix(es) refuted ({', '.join(refuted)})" if refuted else "no fix landed") + \
        (f"; fixers: {' | '.join(verdicts[-4:])}" if verdicts else "; no fixer dispatched")
    return (f"failing: {failing}. tried: {tried}. only the owner: {OWNER_CAUSES[owner_cause]}. run: `! {cmd}`. "
            f"auto-clears when {words}")


# -- owner blocks are leases (design §5.3 blocked-external; phase 1 step 11)
# A block is a lease `block/<key>` (one red) or `block/cause/<name>` (ONE external cause holding every red it explains),
# holder `owner:<owner cause>`, TTL 10 min, max 7 d. Only the tick renews it, and only while its probe still fails AND
# its justification holds for a red it holds (an owner cause, the owner's exact command, never a TEST FAILURE, no live
# fixer). Its probe passing, the justification gone, or the max hold: it lapses, and its reds go on (verify / offered).
BLOCK_TTL, BLOCK_MAX = 10 * 60, 7 * 86400
BLOCK_PROBE_EVERY = 60


# A SPENT QUOTA IS AN EXTERNAL WAIT, NOT THE OWNER'S (the owner's coordinator, 2026-10-29: Record scrape fixtures #2426
# sat BLOCKED ON OWNER for OMDb's `HTTP 401: Request limit reached` -- a daily quota, which resets on its own; only an
# invalid key is the owner's). A red whose error line says a quota is spent is held by a WAIT lease (block/cause/quota-
# <service>, holder `wait:quota`), never offered to a fixer and never an owner block. Its probe re-checks the key the
# cheap way the workflow itself does: the red's workflow dispatched on main once the quota's window has reset (OMDb's
# daily window: midnight UTC), whose first job (the recording preflight: one OMDb call) fails within minutes if the key
# is still refused; the wait clears when a run of the workflow created after the block got past that job.
WAIT_CAUSES = {"quota": "an outside service's quota for the run's key, spent; it resets on its own"}


def block_invalid(store, it, m, t):
    """Why this block no longer justifies holding red `it`, or None (checked at the block AND every tick)."""
    d = it["data"]
    if m.get("vault"):  # it waits on a vault (red_vault_block): the owner's `decide vault-<ns>`, any red kind
        row = store.get(red_lease(d["key"]))
        return f"a live fixer is on it: {row['holder']}" if lease_is_live(row, t) else None
    if m.get("crash_loop") or m.get("decision"):  # the fixers crash-looped on it (red_crash_loop), or it waits on the
        # owner's decision (red_decision_block): the harness, or the owner's call, never the code, holds it
        if m.get("decision") and ask_refusal(ask_category(m), m.get("decide") or m.get("question")):
            return "its question names no owner-only category (ask.py OWNER_ONLY): its fixers proceed"
        if m.get("crash_loop") and not m.get("harness"):  # an owner block of a crash loop from before 2026-11-19
            return "a fixer crash loop is a harness fault, agentctl's own, never the owner's"
        iss = store.item("issue", m["issue"]) if m.get("issue") else None
        if iss and iss["state"] == "closed":  # A HARNESS FAULT FIXED: its maintainer closed the issue (redcrash.py)
            return f"its harness issue {m['issue']} is closed ({str((iss.get('data') or {}).get('note') or '')[:120]})"
        row = store.get(red_lease(d["key"]))
        return f"a live fixer is on it: {row['holder']}" if lease_is_live(row, t) else None
    if m.get("wait_cause") in WAIT_CAUSES:  # an external wait (a spent quota): no owner cause, no owner command
        if d.get("kind") == "test":
            return "its red is a TEST FAILURE: a code red is a fixer's"
        row = store.get(red_lease(d["key"]))
        return f"a live fixer is on it: {row['holder']}" if lease_is_live(row, t) else None
    if QUOTA_LOG.search(str(d.get("line") or "")) or QUOTA_LOG.search(str(m.get("details") or m.get("text") or "")):
        return "a spent quota resets on its own: an external wait, never the owner's"
    if m.get("owner_cause") not in OWNER_CAUSES:
        return f"no owner cause (one of {', '.join(OWNER_CAUSES)}): only the owner's causes may block on the owner"
    if not str(m.get("cmd") or "").strip():
        return "no owner command"
    unsafe = owner_cmd_unsafe(m.get("cmd"))
    if unsafe:
        return f"its owner command is unsafe: {unsafe}"
    if d.get("kind") == "test":
        return "its red is a TEST FAILURE: a code red is never the owner's"
    row = store.get(red_lease(d["key"]))
    if lease_is_live(row, t):
        return f"a live fixer is on it: {row['holder']}"
    return None


def red_renew_blocks(store, t):
    """The tick's look at every live block: its probe (at most every BLOCK_PROBE_EVERY s) and its justification per red.
    Still failing and still justified: renewed (the probe's fresh failing result is its progress). Otherwise it lapses
    now (revoked, the reason in its outcome) or a red that lost its justification is detached from it."""
    out = []
    active = {it["data"]["key"]: it for it in red_active(store).values()}
    for row in store.list("block/"):
        if not lease_is_live(row, t):
            continue
        m = lease_meta(row)
        keys = [k for k in m.get("keys") or [] if k in active]
        dropped = [(k, block_invalid(store, active[k], m, t)) for k in keys]
        dropped = [(k, why) for k, why in dropped if why]
        keep = [k for k in keys if k not in {k_ for k_, _w in dropped}]
        for k, why in dropped:
            out.append(f"dropped the owner block of {red_what(active[k]['data'])}: {why}")
        if not keep:
            why = dropped[0][1] if dropped else "it holds no open red"
            store.revoke(row["name"], f"INVALID: {why}")
            out.append(f"block {row['name']} lapsed: {why}")
            continue
        if keep != m.get("keys"):
            store.set_meta(row["name"], row["epoch"], row["secret"], {"keys": keep})
        if t - float(m.get("probed_at") or 0) < BLOCK_PROBE_EVERY:
            store.renew(row["name"], row["epoch"], row["secret"], f"held@{int(t // BLOCK_PROBE_EVERY)}", BLOCK_TTL)
            continue
        pr = m.get("probe") or {}
        r = run_probe(pr.get("kind"), pr.get("args")) if pr.get("kind") in PROBES else None
        store.set_meta(row["name"], row["epoch"], row["secret"], {"probed_at": t, "last": r.detail if r else "no probe"})
        if r is not None and r.ok:
            store.revoke(row["name"], f"PROBE PASSED: {r.detail}")
            out.append(f"RECOVERED: {m.get('until')} ({r.detail}): the block {row['name']} lapsed")
            line = f"RECOVERED: {m.get('until')} ({r.detail}): {m.get('text', '')[:200]}"
            inbox_write("recover", line, [line], [], 0)
            continue
        store.renew(row["name"], row["epoch"], row["secret"], f"{r.detail if r else 'cannot tell'}@{int(t)}", BLOCK_TTL)
    return out
