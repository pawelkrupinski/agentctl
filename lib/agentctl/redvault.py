# agentctl, part `redvault`: vault asks, blocks nobody can see clear.
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from agentctl.clock import epoch_now
import re
import time
from dataclasses import dataclass
from typing import Any
from agentctl.core import fmt_age, git
from agentctl.timefmt import iso_at, iso_epoch
from agentctl.statefile import read_record
from agentctl.classify import QUOTA_LOG
from agentctl.repos import RED_PRIMARY, repo_signs
from agentctl.procs import safe_cwd
from agentctl.fixerrecords import own_fixer_record_path
from agentctl.worktrees import claimant
from agentctl.ciwhy import resolve_run
from agentctl.statemachines import StaleVersion
from agentctl.redrecord import lease_is_live, red_lease, red_of_key, red_what
from agentctl.leases import block_lease, lease_meta, red_block_of
from agentctl.conditions import probe_words, run_probe
from agentctl.redask import OWNER_CAUSES, OWNER_CAUSE_CATEGORY, red_decision, vault_decide_cmd
from agentctl.red import red_move
from agentctl.redblock import BLOCK_MAX, BLOCK_TTL, owner_cmd_unsafe, owner_details, red_run_units
from agentctl.redopen import red_offer
from agentctl.redclaim import red_targets
from agentctl.ownerleases import owner_holder


# A VAULT ASK IS ONE OWNER ITEM PER VAULT, ANSWERED BY ONE LINE (2026-11-16, Order independence #77 [ios]: its fixer's
# tested fix waited on commit signing while the `movies` vault was locked; it ended BLOCKED ON OWNER five turns in four
# minutes, then sat as an owner decision until the owner hand-ran `agentctl decide` at 05:01Z). A fixer's ask whose one
# condition is a vault (`secrets unlock <ns>` in a BLOCKED ON OWNER verdict or a DIAGNOSED decide) puts the red, whatever
# its kind, on ONE lease per vault, block/cause/vault-<ns>: the owner is told once for every red it holds (ask.py), with
# the one line that resumes them all, `! ~/bin/agentctl decide vault-<ns> here` (red_vault_decide).
# NO UNLOCK RESUMES THEM BY ITSELF (review round 3, 2026-11-16). An unlock is pinned to ONE session anchor, the caller's
# nearest Claude Code ancestor, and signing asks Touch ID once per anchor (~/projects/secrets README, "Session pinning"):
# the owner's `secrets unlock` in his own session never lets a fixer sign. The secrets-log watch that resumed the reds on
# any unlock (2a8139c) read only "the owner touched the vault somewhere", never fired once, and would have had a fixer
# raise a Touch ID prompt minutes later that the owner had not asked for -- the prompt secrets' README says never to
# approve. `decide vault-<ns>` says what matters: the owner is at the machine now. Each resumed fixer signs a probe
# commit FIRST (its own Touch ID prompt, raised while he is there) and ends BLOCKED ON OWNER at once if that fails: back
# on the vault's one lease, told once again.
# `decide vault-<ns>` too (2026-11-22, Order independence #105: turns 69-71 ended `BLOCKED ON OWNER: ~/bin/agentctl
# decide vault-movies "here"`, the cmd the owner is told, not `secrets unlock`; unmatched, each was re-offered ~2 min later)
VAULT_ASK = re.compile(r"secrets\s+unlock\s+([A-Za-z0-9_-]+)|decide\s+vault-([A-Za-z0-9_-]+)")
VAULT_TARGET = re.compile(r"vault-([A-Za-z0-9_-]+)")


def vault_ask_ns(text):
    """The vault an owner ask waits on (`secrets unlock <ns>` in it), or None."""
    m = VAULT_ASK.search(str(text or ""))
    return (m.group(1) or m.group(2)) if m else None


# A VAULT ASK NAMES THE SECRET IT NEEDS (2026-11-22: Tool self-tests #480, Order independence #116 and Alert backtest #20
# asked `! secrets unlock movies` while nothing in their fixes read a movies secret -- a land's push had failed (DNS),
# its NOT LANDED said `agentctl env --check`, and env --check said the vault was locked; released three times, back on
# the vault within minutes each time). In a repo whose main is unsigned (red-repos.json `signed: false`) nothing a fixer
# does but reading a secret touches the vault: its commits are unsigned (fixer_env), its pushes authenticate as gh's
# keyring login on the laptop and the GitHub App on ci-runner, agentctl's store token is in its environment. So there a
# vault ask counts only when it names the read that failed -- `secrets run|get <ns> ...` (SECRET_USE) -- and any other
# is routed back to the fixer (red_ask_route), saying why: decided from the fixer's own first secret use, never by
# default. A repo that signs keeps every vault ask (its commits need the vault's signing key).
SECRET_USE = re.compile(r"secrets\s+(?:run|get)\s+([A-Za-z0-9_-]+)\b")


def vault_ask_unfounded(o_r: str, text: object, ns: str) -> str | None:
    """Why the vault ask `text` (for vault ns) is no owner's -- None when it stands: a repo that signs, or the ask names
    a read of one of ns's secrets."""
    if repo_signs(o_r) or ns in SECRET_USE.findall(str(text or "")):
        return None
    return (f"no {ns} secret was read: in {o_r} a land, a push, a commit and agentctl's own reads need no vault -- "
            "commits are unsigned, GitHub authenticates through gh's login (the App on ci-runner), the store token is "
            "in your environment; a NOT LANDED push or `agentctl env --check` saying the vault is locked is no reason. "
            f"Ask only when `secrets run {ns} <KEY> -- <cmd>` for a secret your fix needs failed: name that command and "
            "its error in the ask")


def red_vault_resume(store, it, ns, answer, t):
    """The owner is at the machine (`decide vault-<ns>`): red `it`, waiting on vault ns, offered again as a decide
    would, its fixer briefed to prove its OWN session can sign before anything else."""
    d = it["data"]
    q = (d.get("ask") or {}).get("q") or f"agents' work waits on the {ns} vault"
    # a repo whose main is unsigned (fixer_env commits unsigned there): no signing probe, which would itself sign and
    # raise a prompt for nothing; the vault was for a secret, so the probe is the command that needed it
    probe = ("run `git commit-tree -S HEAD^{tree} -m 'signing probe' < /dev/null` in your worktree"
             if repo_signs(d.get("repo") or RED_PRIMARY) else
             "run again the one command that needed the vault -- a `secrets run` of the secret your fix uses, never "
             "`agentctl env --check` or `secrets status` (they only read the vault's state, raise no Touch ID prompt, "
             "and a fixer's land needs no vault: its credentials are in its environment); your commits here are "
             "unsigned: never sign one. A fix needing no secret just lands")
    dec = {"answer": f"the owner is at the machine (`decide vault-{ns}`: {answer}). No other session's unlock lets "
                     "yours use the vault: your first use raises a Touch ID prompt for YOUR session, which the owner "
                     f"answers now. So FIRST {probe}; if it fails, end `BLOCKED ON OWNER: ! secrets unlock {ns}` at "
                     "once, and in it name the command that failed and its error (no secret's value). Then go on "
                     "with the fix and land it.", "question": str(q)[:600], "at": t,
           "status": "in-progress", "category": "vault"}
    store.close_offer(f"red:{it['id']}", "resumed")
    red_offer(store, it, f"{red_what(d)}: the owner resumed it (the {ns} vault): offered again",
              data={"cause": None, "block": None, "ask": None, "decision": dec, "dispatch_after": 0})
    return [f"resumed {red_what(d)} (the {ns} vault)"]


def _vault_resume_key(store: Any, key: Any, ns: Any, answer: Any, t: Any) -> Any:
    """red_vault_resume's lines for the red `key` names, while it still waits on the vault ([] once it does not)."""
    for _try in range(10):  # StaleVersion: the store ticks meanwhile -- re-read and re-apply
        it = red_of_key(store, key)
        if not it or it["state"] not in ("blocked-external", "offered") or \
                (it["state"] == "offered" and red_decision(it["data"])):
            return []
        try:
            return red_vault_resume(store, it, ns, answer, t)
        except StaleVersion:
            continue
    return []


def red_vault_decide(store, ns, answer):
    """`agentctl decide vault-<ns> "<answer>"`: every red on vault ns's lease resumed (red_vault_resume), the lease
    released. (rc, lines)."""
    lname, t = block_lease(cause=f"vault-{ns}"), store.clock()
    row = store.get(lname)
    if not lease_is_live(row, t) or lease_meta(row).get("vault") != ns:
        return 1, [f"NOT DECIDED: no red waits on the {ns} vault (`~/bin/agentctl red status`)"]
    keys = lease_meta(row).get("keys") or []
    # RESUMED FIRST, RELEASED AFTER (2026-11-19 03:01Z, core #1053: the owner's `decide vault-bitcashier here` revoked
    # the lease, the tick's invariant saw the red blocked-external with no block in that same second and re-offered it
    # bare -- and the decide, finding no red still blocked, said "NOT DECIDED" while its red went on without the
    # signing brief). While the lease is live no invariant touches its reds; one the tick re-offered meanwhile (offered,
    # no decision of its own) is still the owner's answer's: resumed too.
    out = [line for key in keys for line in _vault_resume_key(store, key, ns, answer, t)]
    store.revoke(lname, f"RELEASED: DECIDED by the owner: {answer[:200]}")
    if not out:
        return 1, [f"NOT DECIDED: the {ns} vault's lease held no red still waiting on it (released)"]
    return 0, [f"DECIDED: {line} -- IN PROGRESS: each fixer's first signature asks Touch ID for its own session: answer "
               "it; if the action fails you are told" for line in out]


def red_vault_block(store: Any, it: dict[str, Any], upd: dict[str, Any], ns: str, why: Any, t: float) -> list[str]:
    """The red waits on vault ns: it joins the vault's ONE lease (opened when none is live). Moved quietly: the tick's
    red_owner_reminders tells the owner once, with `decide vault-<ns>`."""
    d = {**it["data"], **upd}
    # A PENDING FIX WAITS ON NO VAULT (2026-11-22: five vault asks on reds whose fix had landed -- 9a333c1f0ccc.4,
    # ec12a4564dbc.9, 2da9f366a0b5, 5254bffda9d2.4, 90683fa5cb3b -- each REFUSED as fix-pending -> blocked-external, no
    # move of the red machine, so neither the ask nor the fixer's verdict was kept). Its fix decides it (verified, or
    # refuted and offered again); nobody needs the vault meanwhile: the red stays fix-pending, the ask noted on it.
    if it["state"] == "fix-pending":
        red_move(store, it, "fix-pending", f"{red_what(d)}: its fix is pending: the {ns} vault ask waits for its "
                 "verdict (asked again only if the fix is refuted)", data={**upd, "vault_ask": ns})
        return [f"{red_what(d)}: fix pending, the {ns} vault ask kept"]
    key = d["key"]
    lname = block_lease(cause=f"vault-{ns}")
    row = store.get(lname)
    if lease_is_live(row, t) and lease_meta(row).get("vault") == ns:
        store.set_meta(lname, row["epoch"], row["secret"], {"keys": sorted(set(lease_meta(row).get("keys") or []) | {key})})
    else:
        got = store.acquire(lname, "owner:vault", BLOCK_TTL, BLOCK_MAX, progress=f"locked@{int(t)}",
                            meta={"kind": "block", "vault": ns, "category": "vault", "cause": f"vault-{ns}",
                                  "cmd": vault_decide_cmd(ns),
                                  "question": f"agents' work waits on the {ns} vault (commit signing, its secrets) and "
                                              "only you can answer its Touch ID prompts", "keys": [key], "probed_at": t,
                                  "text": f"BLOCKED: the {ns} vault is locked for its fixers (`decide vault-{ns} here`)",
                                  "details": " ".join(str(why).split())[:400]})
        if "held" in got:  # somebody else's lease by that name: never this red's to take over
            red_offer(store, it, f"{red_what(d)}: {lname} is held by {got['held'].get('holder')}: offered again", data=upd)
            return [f"re-offered {red_what(d)} ({lname} held)"]
    store.close_offer(f"red:{it['id']}", "blocked")
    red_move(store, it, "blocked-external", f"{red_what(d)}: waits on the {ns} vault (the owner's `decide vault-{ns}`)",
             data={**upd, "cause": f"vault-{ns}", "block": lname})
    return [f"{red_what(d)}: waits on the {ns} vault"]


# A BLOCK NOBODY CAN SEE CLEAR (2026-11-07: Verify scrape recording #14's reds read "BLOCKED ON OWNER: ... re-pin movies
# runner scheduler to 362196b15a" hours after the owner had re-pinned, and past it -- ci-runner ran a later scheduler).
# That block was a turn's VERDICT, not a block lease: no probe could clear it, so it stayed the red's last word. A
# verdict with no live block now reads with its age and how to strike it (block_verdict_note), and `red unblock` strikes
# it -- or ends a live block lease (RELEASED: its reds go on at the next tick, re-offered, never escalated) -- leaving
# the reason in the red's history.
def red_blocked_verdict(d):
    """The red's last word when it is a BLOCKED ON OWNER verdict (its history's newest entry), else None."""
    last = (d.get("history") or [{}])[-1]
    return last if str(last.get("outcome") or "").startswith("BLOCKED ON OWNER") else None


def block_verdict_note(it, t, store):
    """A BLOCKED ON OWNER verdict that no live block lease holds, shown as what it is: an old word, its age, and how to
    strike it; None when the red's last word is no such verdict or a block holds it."""
    d = it["data"]
    last = red_blocked_verdict(d)
    if not last or red_block_of(it, store):
        return None
    at = iso_epoch(last.get("at"))
    age = t - at if at is not None else None
    said = f"said {fmt_age(age)} ago" if age is not None else f"said at {last.get('at')}"
    return (f"{str(last.get('outcome'))[:300]} -- {said}, no block holds it (no probe clears it): if it is done or "
            f"superseded, `agentctl red unblock {d.get('key')} --why \"<what changed>\"`")


def _unblock_one(store: Any, it: Any, who: Any, why: Any, t: Any) -> Any:
    """Unblock one red (see red_unblock); the line saying what was done."""
    d = it["data"]
    blk = red_block_of(it, store)
    if blk:
        row = store.get(blk["name"])
        keep = [k for k in lease_meta(row).get("keys") or [] if k != d.get("key")]
        if keep:  # an outage's ONE lease holding other reds too: only this red leaves it
            store.set_meta(row["name"], row["epoch"], row["secret"], {"keys": keep})
        else:
            store.revoke(row["name"], f"RELEASED: unblocked by {who}: {why}")
    elif not red_blocked_verdict(d):
        return f"{red_what(d)} ({d.get('key')}): not blocked on the owner -- left as it is"
    cur = store.item("red", it["id"])
    h = {"at": iso_at(t), "holder": who, "outcome": f"UNBLOCKED: {why}"}
    store.update_item("red", it["id"], cur["version"], {"history": (cur["data"].get("history") or []) + [h]})
    return (f"unblocked {red_what(d)} ({d.get('key')})" + (f": ended {blk['name']}" if blk else
                                                         ": its BLOCKED ON OWNER verdict struck") + f" -- {why}")


def red_unblock(store, o_r, target, why, who=None):
    """(rc, lines) of `red unblock`: the reds a target names are no longer the owner's -- a live block lease holding
    them is ended (RELEASED: the tick offers them again, never escalated) and an UNBLOCKED entry ends their history, so a
    stale BLOCKED ON OWNER verdict is no longer their last word."""
    if not str(why or "").strip():
        return 2, ["NOT UNBLOCKED: --why \"<what changed>\" (the owner did it, it is superseded by ...)"]
    items = red_targets(store, o_r, target, create=False)
    if not items:
        return 1, [f"NOT UNBLOCKED: #{target} names no open red"]
    who = who or claimant(safe_cwd())
    t = store.clock()
    return 0, [_unblock_one(store, it, who, why, t) for it in items]


class NotBlocked(Exception):
    """`red block` refused: what to tell (after `NOT BLOCKED: `) and the exit code."""

    def __init__(self, rc: int, message: str) -> None:
        super().__init__(message)
        self.rc = rc


def block_target_units(st: Any, o_r: str, n: str) -> tuple[Any, list[Any], list[Any]]:
    """(workflow name, units, the reds already known) of `red block`'s target: a red key, or a run the tick has seen
    (as `red claim` finds it)."""
    name, units = None, []
    known = red_targets(st, o_r, n, create=False)
    try:
        name, _r, units = red_run_units(o_r, n)
    except (Exception, SystemExit):
        if not known:
            name, _r = resolve_run(o_r, n)
    if known and name and name != known[0]["data"].get("workflow"):  # a bare NUMBER another workflow's run also has
        name, units = None, []  # (issue 20261107T072345-7bca40: `red block 14` read Report run timings #14): the red's
    if known and not units:  # issue 20261119T030011-293b9a: a key, or a run whose units no longer read (a green rerun)
        name = name or known[0]["data"].get("workflow")
        units = [{"kind": it["data"].get("kind"), "line": it["data"].get("line"), "job": it["data"].get("job")}
                 for it in known]
    return name, units, known


def check_blockable(n: str, units: list[Any], vault: Any, owner_cmd: Any, owner_cause: Any) -> None:
    """Raises NotBlocked unless the units are the owner's to act on: an owner cause (or a locked vault, any red kind),
    no spent quota (an external wait), no test failure (a code red is never the owner's), a safe owner command."""
    if owner_cause not in OWNER_CAUSES and not vault:
        raise NotBlocked(2, f"a block on the owner needs --owner-cause, one of: {', '.join(OWNER_CAUSES)} (a cause outside "
                            "the code that only the owner can act on). A code red is yours: claim it and fix it.")
    if any(QUOTA_LOG.search(str(x.get("line") or "")) for x in units):
        raise NotBlocked(2, f"#{n}'s key hit a spent quota ({str(units[0].get('line'))[:120]}): it resets on its own, so "
                            "it is no owner action -- the tick holds the red as an external wait and re-runs its workflow "
                            "after the reset (`agentctl red status`)")
    if any(x.get("kind") == "test" for x in units) and not vault:
        raise NotBlocked(2, f"#{n} is a TEST FAILURE ({units[0].get('job')}): a code red is never the owner's -- "
                            f"`~/bin/agentctl red claim {n}` and fix it")
    unsafe = owner_cmd_unsafe(owner_cmd)
    if unsafe:
        raise NotBlocked(2, f"the owner command is unsafe -- {unsafe}. A command that deletes names each path and "
                            "checks, inside the command, that it is still unused when the owner runs it.")


def is_handed_lease(st: Any, name: str, own: Any) -> bool:
    """True when the live red lease `name` is the block caller's own, or its family owner's (A FAMILY OWNER blocks a red
    it holds: issues 20261106T210149-680984, 20261119T015532-983695: "a live fixer is on #1005:
    laptop:owner:convergence" -- the owner itself)."""
    row = st.get(name)
    fam = own.get("family")
    if not lease_is_live(row, st.clock()):
        return False
    return (name == own.get("lease") and row["epoch"] == own.get("epoch")) \
        or bool(fam and row["holder"] == owner_holder(fam) and lease_meta(row).get("family") == fam)


def handed_leases(st: Any, items: list[Any]) -> list[str]:
    """The reds' leases the block's caller holds (its own fixer's, handed to the owner with the block)."""
    own = read_record(own_fixer_record_path() or "") or {}
    return [x for x in (red_lease(it["data"]["key"]) for it in items) if is_handed_lease(st, x, own)]


def refuse_live_fixer(st: Any, items: list[Any], handed: list[str], n: str) -> None:
    """Raises NotBlocked when a fixer other than the caller holds one of the reds."""
    for it in items:
        name = red_lease(it["data"]["key"])
        if name not in handed and lease_is_live(st.get(name), st.clock()):
            raise NotBlocked(1, f"a live fixer is on #{n}: {st.get(name)['holder']}")


def block_on_vault(st: Any, items: list[Any], handed: list[str], ns: str, a: Any) -> int:
    """A LOCKED VAULT IS ONE WAIT, never an owner cause (issue 20261119T015532-983695): the reds join the vault's lease
    (red_vault_block), which the owner's `decide vault-<ns>` lifts for every red on it."""
    t = st.clock()
    for it in items:
        for line in red_vault_block(st, st.item("red", it["id"]), {}, ns, f"`red block`: {a.owner_cmd}", t):
            print(line)
    for x in handed:
        st.revoke(x, f"BLOCKED ON OWNER (handed over by its fixer): the {ns} vault is locked")
    return 0


def named_probe(o_r: str, a: Any) -> Any:
    """The probe --until-green / --until-http / --until-tcp names, or None."""
    if a.until_green:
        return run_probe("workflow_green", {"repo": o_r, "workflow": a.until_green})
    if a.until_http:
        return run_probe("http", {"url": a.until_http})
    if a.until_tcp:
        host, _, port = a.until_tcp.rpartition(":")
        return run_probe("tcp", {"host": host, "port": int(port)})
    return None


def pick_probe(o_r: str, a: Any, cause: Any, old: Any, om: Any, name: Any, n: str) -> Any:
    """The run probe that clears the block: the outage's own (attaching to its ONE lease), a named one
    (--until-green / --until-http / --until-tcp), or the red's own workflow going green; NotBlocked when none."""
    if not (a.until_green or a.until_http or a.until_tcp):
        if old:
            return run_probe(om["probe"]["kind"], om["probe"]["args"])
        if cause:
            raise NotBlocked(2, f"no open cause {cause!r} to attach to; open it with its probe: --cause {cause} "
                                "--until-green <workflow> | --until-http <url> | --until-tcp <host:port>")
    probe = named_probe(o_r, a) or (run_probe("workflow_green", {"repo": o_r, "workflow": name}) if name else None)
    if probe:
        return probe
    raise NotBlocked(2, f"#{n}'s workflow is unknown, so no probe can clear the block; name one: --until-green "
                        "<workflow> | --until-http <url> | --until-tcp <host:port>")


def join_block(st: Any, lname: str, row: Any, keys: list[Any]) -> None:
    """The reds' keys added to a live block lease's."""
    st.set_meta(lname, row["epoch"], row["secret"], {"keys": sorted(set(lease_meta(row).get("keys") or []) | set(keys))})


@dataclass(frozen=True)
class BlockPlan:
    """What `red block` decided: the reds it blocks, the probe that clears it, and the lease it joins or opens."""
    lname: Any
    owner_cause: Any
    items: Any
    iprobe: Any
    words: str
    details: str
    workflow: Any

    @property
    def keys(self) -> list[Any]:
        return [it["data"]["key"] for it in self.items]

    @property
    def what(self) -> str:
        return ", ".join(red_what(it["data"]) for it in self.items)


def block_meta(a: Any, plan: BlockPlan) -> dict[str, Any]:
    """The meta of a new block lease: its probe, the owner's command and question, the reds it holds."""
    cause, ip, oc = getattr(a, "cause", None), plan.iprobe, plan.owner_cause
    text = f"BLOCKED: {cause} down (auto-clears when {plan.words})" if cause else f"BLOCKED ON OWNER: {plan.details}"
    return {"kind": "block", "owner_cause": oc, "cmd": a.owner_cmd,
            "cause": cause, "question": f"run `! {a.owner_cmd}` ({OWNER_CAUSES[oc]})" +
                                        (f" -- {a.note}" if getattr(a, "note", "") else ""),
            "probe": {"kind": ip.kind, "args": ip.args}, "until": plan.words, "keys": plan.keys, "text": text,
            "details": plan.details, "verify": not (ip.kind == "workflow_green" and ip.args.get("workflow") == plan.workflow),
            "probed_at": epoch_now()}


def open_block(st: Any, a: Any, plan: BlockPlan) -> str:
    """Acquires the block lease (named after the cause, else the first red) and tells what it holds: the lease's name.
    NotBlocked when another holder has the name."""
    cause, ip, oc = getattr(a, "cause", None), plan.iprobe, plan.owner_cause
    lname = plan.lname or block_lease(plan.keys[0])
    r = st.acquire(lname, f"owner:{oc}", BLOCK_TTL, BLOCK_MAX, progress=f"{ip.detail}@{int(epoch_now())}",
                   meta={**block_meta(a, plan), "category": OWNER_CAUSE_CATEGORY[oc]})
    same = st.get(lname) if "held" in r else None
    if "held" in r and lease_meta(same).get("kind") != "block":
        raise NotBlocked(1, f"{lname} is held by {r['held']['holder']}")
    if same:  # the run's block is already up (named after its first red): these reds join it
        join_block(st, lname, same, plan.keys)
        print(f"already blocked: {plan.what} joined {lname}: {lease_meta(same).get('text')}")
    else:  # the owner is told by the tick (red_owner_reminders: ONE inbox entry and a notification, then reminders)
        head = f"{cause} down (blocks {plan.what})" if cause else plan.what
        print(f"RED BLOCKED ON OWNER [{OWNER_CAUSE_CATEGORY[oc]}]: {head}: {plan.details} "
              f"({ip.detail}; `agentctl red status`)")
    return lname


def existing_block(st: Any, cause: Any) -> tuple[Any, Any, Any]:
    """(lease name, its live row or None, its meta) of the outage lease --cause names."""
    lname = block_lease(cause=cause) if cause else None
    old = st.get(lname) if lname else None
    old = old if lease_is_live(old, st.clock()) else None
    return lname, old, lease_meta(old)


def unclear_probe(o_r: str, a: Any, cause: Any, old: Any, om: Any, name: Any, n: str, items: list[Any]) -> Any:
    """The probe that clears the block, which must still fail (else nothing waits on the owner), with reds to block."""
    iprobe = pick_probe(o_r, a, cause, old, om, name, n)
    if iprobe.ok:
        raise NotBlocked(1, f"{probe_words(iprobe.kind, iprobe.args)} already ({iprobe.detail}): nothing waits on the "
                            f"owner -- `red claim {n}` and fix it")
    if not items:
        raise NotBlocked(1, f"#{n} names no red on main")
    return iprobe


def block_reds(st: Any, o_r: str, a: Any, n: str) -> int:
    """The stages of `red block`: the target's units, what may be blocked, the reds and the fixers holding them, the
    probe that clears the block, then the block lease the reds join. NotBlocked refuses at any stage."""
    name, units, known = block_target_units(st, o_r, n)
    cause = getattr(a, "cause", None)
    lname, old, om = existing_block(st, cause)
    owner_cause = getattr(a, "owner_cause", None) or om.get("owner_cause")
    owner_cmd = a.owner_cmd or om.get("cmd")
    vault = vault_ask_ns(a.owner_cmd)  # a locked vault: no owner cause, any red kind (block_on_vault)
    check_blockable(n, units, vault, owner_cmd, owner_cause)
    items = known or (red_targets(st, o_r, n) if units else [])
    handed = handed_leases(st, items)
    refuse_live_fixer(st, items, handed, n)
    if vault and items:
        return block_on_vault(st, items, handed, vault, a)
    iprobe = unclear_probe(o_r, a, cause, old, om, name, n, items)
    words = probe_words(iprobe.kind, iprobe.args)
    plan = BlockPlan(lname, owner_cause, items, iprobe, words,
                     owner_details(o_r, n, name, units, owner_cmd, words, owner_cause, st), name)
    if old:
        join_block(st, lname, old, plan.keys)
        print(f"attached {plan.what} to {cause!r}: {om.get('text')} ({iprobe.detail}; `agentctl red status`)")
    else:
        lname = open_block(st, a, plan)
    for it in items:
        cur = st.item("red", it["id"])
        st.update_item("red", it["id"], cur["version"], {"block": lname})
    for x in handed:  # the dispatched fixer's own lease, handed to the owner: its wrapper ends with the block's verdict
        st.revoke(x, f"BLOCKED ON OWNER (handed over by its fixer): {owner_cmd}")
    return 0


def red_block(st, o_r, a):
    """`red block`: the red waits on the OWNER, for an owner cause only, as a block lease the tick renews while its
    probe -- named (--until-green / --until-http / --until-tcp), an open cause's, or the red's own workflow going green
    -- still fails and its justification holds. --cause NAME: ONE lease for that outage, every red it explains attached."""
    try:
        return block_reds(st, o_r, a, str(a.run).lstrip("#"))
    except NotBlocked as e:
        print(f"NOT BLOCKED: {e}")
        return e.rc
