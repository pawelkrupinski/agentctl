# agentctl, part `conditions`: THE CONDITION MACHINE: a non-green condition opened with the probe that closes it, rechecked with backoff, escalated, closed with its recovery actions; the ConditionRegistry cli builds, and the world probes
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from agentctl.clock import epoch_now
import glob
import json
import os
import re
import time
from typing import Any, Callable
from contextlib import suppress
from agentctl.core import SHORT, attempt, err, fmt_age, git, host_role, recovery_dir, remote
from agentctl.timefmt import iso_at, ts
from agentctl.statefile import read_record, unlink_file
from agentctl.gating import mark_gate_dirty
from agentctl.inboxentry import inbox_dir, inbox_write
from agentctl.statemachines import StaleVersion, TerminalState
from agentctl.blameknobs import knob_condition_pending_lapse, knob_recover_max, knob_unknown_grace
import urllib.error
import urllib.request
from agentctl.repos import movies_repo_path
from agentctl.cistate import ci_state_load
from agentctl.conditionregistry import PROBE_EVERY, RECOVER_ACTION_NAMES, Check, ConditionBypass, ConditionRegistry, probe_kind


class ProbeResult:
    """What one probe said: ok True (passes), False (fails) or None (cannot tell: stays open). Made by run_probe only."""
    __slots__ = ("kind", "args", "ok", "detail", "at")

    def __init__(self, kind: str, args: dict[str, Any], ok: bool | None, detail: str, _made_by_run_probe: object = None) -> None:
        if _made_by_run_probe is not _RUN_PROBE_TOKEN:
            raise ConditionBypass("a ProbeResult is made by run_probe only: the check that opens is the check that closes")
        self.kind, self.args, self.ok, self.detail, self.at = kind, dict(args), ok, str(detail)[:300], epoch_now()


_RUN_PROBE_TOKEN = object()


def probe_words(kind: Any, a: Any) -> Any:
    """A probe in a person's words: the status line's `auto-clears when <this>`."""
    return {"disk_free": lambda: f"{a.get('min_gb')} GB are free again",
            "quota_reset": lambda: f"the quota resets and {a.get('workflow')}'s {a.get('job')} passes",
            "workflow_green": lambda: f"{a.get('workflow')} is green", "http": lambda: f"{a.get('url')} answers",
            "tcp": lambda: f"{a.get('host')}:{a.get('port')} accepts",
            "route": lambda: "the network is back", "tunnel": lambda: "the tunnel answers",
            "gate_read": lambda: "the gate is read", "token": lambda: "the processor holds a token",
            "git_fetch": lambda: f"git can read {remote()}/main", "push_credential": lambda: "a push succeeds again",
            "invariant": lambda: f"invariant {a.get('id')} holds", "waiting_ok": lambda: "a waiting round completes",
            "orphan_landed": lambda: "its commits are on main or somebody works in it",
            "change_source": lambda: f"change {a.get('id')} closes or is queued again",
            "clone_health": lambda: f"{a.get('repo')} verifies and fetches {remote()}/main",
            "infra_requeue": lambda: f"queue entry {a.get('eid')} gets past {a.get('reason')}",
            "queue_moving": lambda: "a change moves again (or none is open)",
            "red_read": lambda: f"ci-runner reads {', '.join(a.get('repos') or [])} (the agentctl-reader App)",
            "red_newer_verdict": lambda: f"a newer {a.get('workflow')} run than #{a.get('run')} no longer fails this way"}.get(kind, lambda: kind)()


def run_probe(check: Check, args: dict[str, Any] | None = None) -> ProbeResult:
    """Run the probe check (a probe_<kind> function, or a partial of one) on args: the ONE way to a ProbeResult. A probe
    that raises is ok=None (cannot tell)."""
    kind = probe_kind(check)
    args = dict(args or {})
    try:
        ok, detail = check(args)
    except (Exception, SystemExit) as e:
        ok, detail = None, f"probe error: {str(e)[:160]}"
    # True, False or None, never a falsy number (2026-11-16: W1's `x or (0.0 and y)` was 0.0 on a green main: neither
    # `ok is False`, so the watchdog never refreshed its condition, nor true, so nothing closed it)
    return ProbeResult(kind, args, None if ok is None else bool(ok), detail, _made_by_run_probe=_RUN_PROBE_TOKEN)


# -- the world probes, which the modules opening their conditions import: (ok, detail) for their args. Background-safe:
# no `secrets`, bounded, never raising (run_probe catches).
def probe_workflow_green(a: dict[str, Any]) -> tuple[bool | None, str]:
    """The newest decided run of workflow a['workflow'] on main is green (the CI-state snapshot's `latest`)."""
    snap = ci_state_load(a.get("repo") or SHORT["movies"])
    if not snap:
        return None, "no fresh CI-state snapshot"
    last = (snap.get("latest") or {}).get(a["workflow"])
    if not last:
        return None, f"no decided run of {a['workflow']} on main in the snapshot"
    ok = last.get("conclusion") == "success"
    return ok, f"{a['workflow']} #{last.get('number')} {last.get('conclusion')}"


def probe_http(a: dict[str, Any]) -> tuple[bool | None, str]:
    lo, hi = a.get("expect") or (200, 499)
    try:
        req = urllib.request.Request(a["url"], method=a.get("method") or "GET")
        with urllib.request.urlopen(req, timeout=float(a.get("timeout") or 10)) as r:
            code = r.status
    except urllib.error.HTTPError as e:
        code = e.code
    except Exception as e:
        return False, f"{a['url']}: {str(e)[:120]}"
    return lo <= code <= hi, f"{a['url']}: HTTP {code}"


def probe_tcp(a: dict[str, Any]) -> tuple[bool | None, str]:
    import socket
    try:
        with socket.create_connection((a["host"], int(a["port"])), timeout=float(a.get("timeout") or 5)):
            return True, f"{a['host']}:{a['port']} accepts"
    except OSError as e:
        return False, f"{a['host']}:{a['port']}: {e}"


def probe_gate_read(a: dict[str, Any]) -> tuple[bool | None, str]:
    """The gate was read (a CI-state snapshot written) after the condition opened."""
    snap = ci_state_load(a.get("repo") or SHORT["movies"])
    at = float((snap or {}).get("at") or 0)
    return at > float(a.get("since") or 0), (f"gate read at {iso_at(at)}" if at else "no fresh CI-state snapshot")


def probe_git_fetch(a: dict[str, Any]) -> tuple[bool | None, str]:
    """The remote's main can be read over git (what a land's `git fetch` needs)."""
    repo = a.get("repo") or movies_repo_path()
    try:  # core.git: no prompt, a bounded ssh, a timeout read as exit 124
        r = git(repo, "ls-remote", "--heads", remote(), "main", check=False, timeout=60)
    except OSError as e:
        return False, f"git ls-remote {remote()}: {str(e)[:120]}"
    return r.returncode == 0 and "refs/heads/main" in r.stdout, f"git ls-remote {remote()} main: exit {r.returncode} " \
        + (r.stderr.strip()[-120:] if r.returncode else "ok")


def probe_waiting_ok(a: dict[str, Any]) -> tuple[bool | None, str]:
    """A waiting round completed after the condition opened."""
    at = float((read_record(os.path.join(a["d"], "round-ok.json")) or {}).get("at") or 0)
    return at > float(a.get("since") or 0), f"last good round {iso_at(at)}" if at else "no good round yet"


def condition_key(kind: Any, probe: Any) -> Any:
    return re.sub(r"[^\w.-]+", "_", f"{kind}-{probe.kind}-" + "-".join(f"{v}" for _k, v in sorted(probe.args.items())
                                                                           if _k != "since"))[:150]


# -- conditions are items (design §5.4; phase 1 step 12): the store's "condition" machine, open -> closed
CONDITION_ESCALATE_EVERY = 2 * 3600  # an open condition escalates 10 min after opening, then every 2 h


def condition_item_of(store: Any, key: Any) -> Any:
    """The open condition item for key, or None."""
    for it in store.items("condition", ["open"]):
        if it["data"].get("key") == key:
            return it
    return None


def condition_dict(it: Any) -> Any:
    return {**it["data"], "id": it["id"], "version": it["version"], "deadline": it["deadline"], "level": it["level"]}


def condition_grace(kind: Any) -> Any:
    return knob_unknown_grace() if kind == "UNKNOWN" else 0


def condition_pending(st: Any, key: Any, grace: Any, t: Any) -> Any:
    """True while key's failure is younger than grace (THE BLIP rule above): the sighting is marked, nothing opens."""
    if grace <= 0:
        return False
    mk = f"condition_pending:{key}"
    try:
        p = json.loads(st.meta_get(mk) or "null") or {}
        lapse = knob_condition_pending_lapse()
        if not p or t - float(p.get("last") or 0) > lapse:
            p = {"first": t}
        if t - float(p["first"]) < grace:
            st.meta_set(mk, json.dumps({"first": p["first"], "last": t}))
            return True
        st.meta_set(mk, "")
    except (Exception, SystemExit) as e:
        err(f"condition {key}: its pending mark could not be kept ({str(e)[:120]}): opened at once")
    return False


def _opened_text(text: Any, effect: Any, cur: Any, inbox: bool, command: str, exit_code: int, cwd: Any, tail: Any,
                 key: str) -> Any:
    """The condition's effect run and its text made (_OPENING held, so the inbox writes of an opening are allowed), and its one
    inbox entry per spell written. (the effect's dict, the text, the inbox entry ids)"""
    _OPENING[0] += 1
    try:
        extra = (effect() if effect else None) or {}
        text = text(extra) if callable(text) else text
        ids = []
        if inbox and not cur:
            eid = inbox_write(command, text, [text], list(tail), exit_code, cwd=cwd, item=f"condition:{key}")
            ids = [eid] if eid else []
    finally:
        _OPENING[0] -= 1
    return extra, text, ids


def _refreshed(st: Any, cur: Any, probe: Any, new: Any, text: Any, t: Any) -> Any:
    """A condition still open: re-described too, so its escalations say what is wrong NOW, not at its opening."""
    d = cur["data"]
    acts = list(d.get("on_recover") or []) + [x for x in new if x not in (d.get("on_recover") or [])]
    st.update_item("condition", cur["id"], cur["version"], {"on_recover": acts, "last_checked": t, "text": text,
                                                             "last_result": {"ok": probe.ok, "detail": probe.detail}})
    return condition_dict(st.item("condition", cur["id"]))


def _put_condition(st: Any, key: str, kind: str, text: Any, probe: Any, new: Any, ids: Any, command: str, cwd: Any, t: Any) -> Any:
    """A new condition item (an earlier spell is closed: its id is the next free one)."""
    iid, n = key, 1
    while st.item("condition", iid) is not None:
        n += 1
        iid = f"{key}.{n}"
    data = {"key": key, "kind": kind, "text": text, "probe": {"kind": probe.kind, "args": probe.args},
            "until": probe_words(probe.kind, probe.args), "on_recover": new, "opened": ts(), "opened_at": t,
            "inbox": ids, "last_checked": t, "last_result": {"ok": probe.ok, "detail": probe.detail}, "fails": 0,
            "next_check": t + PROBE_EVERY.get(probe.kind, 60), "command": command, "cwd": cwd, "host": host_role()}
    return condition_dict(st.put_item("condition", iid, "open", data))


def _require_recovery(kind: str, probe: Any, on_recover: Any) -> None:
    """No condition without the probe that closes it and recovery actions that exist."""
    if not isinstance(probe, ProbeResult):
        raise ConditionBypass(f"open_condition({kind!r}) without a ProbeResult: no condition without its recovery probe")
    for name, _a in on_recover:
        if name not in RECOVER_ACTION_NAMES:
            raise ConditionBypass(f"unknown on_recover action {name!r}")


def open_condition(kind: str, text: str, probe: Any, on_recover: Any, key: str | None = None, inbox: bool = True,
                   command: str = "recover", exit_code: int = 1, cwd: str | None = None, tail: Any = (), effect: Any = None,
                   *, store: Any, grace: float | None = None) -> Any:
    """Open (or refresh) a non-green condition: an item of the "condition" machine. probe: the FAILED ProbeResult of the
    check that found it (required: the same probe, passing, closes it); on_recover: [(action, args)] from
    RECOVER_ACTION_NAMES. effect: a callable run inside; its dict result is merged into every on_recover args. Writes ONE
    inbox entry (text) per spell. Returns the condition, or None when the probe passes now (nothing is wrong) or the
    failure is still inside its kind's grace (grace: seconds, None for the kind's: THE BLIP rule above). Its time is
    the store's clock."""
    _require_recovery(kind, probe, on_recover)
    if probe.ok:
        return None
    st = store
    key = key or condition_key(kind, probe)
    cur = condition_item_of(st, key)
    if not cur and condition_pending(st, key, condition_grace(kind) if grace is None else grace, st.clock()):
        return None
    extra, text, ids = _opened_text(text, effect, cur, inbox, command, exit_code, cwd, tail, key)
    t = st.clock()
    new = [[n, {**x, **extra}] for n, x in on_recover]
    if cur:
        return _refreshed(st, cur, probe, new, text, t)
    return _put_condition(st, key, kind, text, probe, new, ids, command, cwd, t)


def open_conditions(store: Any) -> Any:
    """The open conditions, as dicts (key, kind, text, probe, on_recover, inbox, ... plus id and version)."""
    return [condition_dict(it) for it in store.items("condition", ["open"])]


def _close_result(cond: Any, result: Any) -> Any:
    """The probe's result; a reason string (a manual close) made one that says so."""
    if isinstance(result, str):
        return ProbeResult(cond["probe"]["kind"], cond["probe"]["args"], True,
                           result if result.startswith("INVALID:") else f"closed by hand: {result}",
                           _made_by_run_probe=_RUN_PROBE_TOKEN)
    return result


def _ack_entries(ids: Any) -> None:
    """The closed condition's inbox entries acked."""
    acked = os.path.join(inbox_dir(), "acked")
    os.makedirs(acked, exist_ok=True)
    for eid in ids or []:
        open(os.path.join(acked, eid), "a").close()


def _recovery_done(cond: Any, it: Any, st: Any, registry: ConditionRegistry) -> list[str]:
    """The condition's on_recover actions run, each failure said; what they say they did."""
    did = []
    for name, a in it["data"].get("on_recover") or []:
        try:
            w = registry.actions[name](cond, a, st)
            if w:
                did.append(w)
        except (Exception, SystemExit) as e:
            did.append(f"{name} failed: {str(e)[:120]}")
    return did


def _closed_line(cond: Any, result: Any, reason: str, manual: bool, did: list[str]) -> str:
    """The one RECOVERED (or, closed by hand, CLOSED) note."""
    text = cond.get("text", "")[:200]
    if not manual:
        return f"RECOVERED: {cond.get('until')} ({result.detail}): {text}" + (f"; {'; '.join(did)}" if did else "")
    if reason.startswith("INVALID:"):
        return f"CLOSED (unjustified): {reason[len('INVALID: '):]}: {text}"
    return f"CLOSED: {reason[len('closed by hand: '):]}: {text}"


def close_condition(cond: Any, result: Any, store: Any, registry: ConditionRegistry) -> Any:
    """The probe passed (result: its ProbeResult, or a reason string for a manual close): the item moves to closed (a
    compare-and-set: one closer), its inbox entries are acked, its on_recover actions run, one RECOVERED note."""
    st = store
    result = _close_result(cond, result)
    it = st.item("condition", cond["id"])
    if not it or it["state"] != "open":
        return None
    manual = str(result.detail).startswith(("INVALID:", "closed by hand:"))
    reason = str(result.detail)
    try:
        st.transition("condition", it["id"], "closed", it["version"],
                      data={"closed": ts(), "closed_by": reason},
                      event={"cause": "probe", "text": f"{cond.get('kind')} closed: {reason[:200]}", "refs": {"key": cond.get("key")}})
    except (StaleVersion, TerminalState):
        return None  # another closer won
    _ack_entries(it["data"].get("inbox"))
    did = _recovery_done(cond, it, st, registry)
    mark_gate_dirty()
    line = _closed_line(cond, result, reason, manual, did)
    err(line)
    inbox_write("recover", line, [line], [], 0)
    return line


def migrate_condition_files(store: Any) -> Any:
    """Step 12's migration: every open condition file under recovery_dir() becomes a condition item; the file goes."""
    out = []
    for p in sorted(glob.glob(recovery_dir("*.json"))):
        c = read_record(p)
        if not c or not c.get("probe"):
            continue
        if c.get("kind") != "BLOCKED" and not condition_item_of(store, c["key"]):
            data = {k: v for k, v in c.items() if k != "path"}
            data.setdefault("opened_at", epoch_now())
            data["escalate_after"] = epoch_now() + CONDITION_ESCALATE_EVERY  # its inbox entry told already: no storm
            store.put_item("condition", c["key"], "open", data)
            out.append(f"migrated the condition {c['key']}")
        unlink_file(p)
    return out


def probed_here(c: Any) -> Any:
    """A condition's probe runs on the host role that opened it (its `host`; before it was recorded: the server's):
    the laptop's watchdog, tunnel or token conditions read the laptop -- on ci-runner a W5 probe found no plist and
    closed, `route -n get default` never answered, and the laptop's inbox entries were acked in the server's."""
    if c["probe"]["kind"] == "token" and host_role() == "sync":
        return True  # the processor's socket is the laptop's: a token condition the server opened (2026-11-02:
        # token-movies stayed open for good, ci-runner has no socket) is cleared by the laptop's probe
    return c.get("host", "server") == host_role()


def _reprobe(st: Any, it: Any, c: Any, t: Any, out: list[str], registry: ConditionRegistry) -> Any:
    """A due condition's probe run again: a pass closes it (its line to out), a failure backs its next check off. The
    item to go on with, or None when it closed or its version moved."""
    r = run_probe(registry.check(c["probe"]["kind"]), c["probe"]["args"])
    if r.ok:
        line = close_condition(c, r, st, registry)
        if line:
            out.append(line)
        return None
    fails = int(c.get("fails") or 0) + 1
    every = PROBE_EVERY.get(r.kind, 60) * 2 ** min(fails, 8)
    try:
        st.update_item("condition", it["id"], it["version"], {
            "fails": fails, "last_checked": t, "last_result": {"ok": r.ok, "detail": r.detail},
            "next_check": t + min(every, knob_recover_max())})
    except StaleVersion:
        return None
    return st.item("condition", it["id"])


def _escalation_due(it: Any, t: Any) -> bool:
    """An open condition past its deadline and its own escalate_after."""
    return it["state"] == "open" and t >= max(it["deadline"], float(it["data"].get("escalate_after") or 0))


def _escalate_overdue(st: Any, it: Any, t: Any) -> None:
    """An open condition past its deadline (and its own escalate_after): the state's self-transition, an escalate event."""
    age = fmt_age(t - float(it["data"].get("opened_at") or t))
    line = f"ESCALATED: {it['data'].get('kind')} open for {age}: {str(it['data'].get('text'))[:200]}"
    with suppress(StaleVersion):
        st.transition("condition", it["id"], "open", it["version"], deadline_s=CONDITION_ESCALATE_EVERY,
                      event={"cause": "timeout", "text": line, "audience": {"worktree": it["data"].get("cwd")} if it["data"].get("cwd")
                             else {}, "refs": {"verdict": line, "key": it["data"].get("key"), "command": "recover"}})


def _tick_condition(st: Any, it: Any, t: Any, force: bool, server: bool, out: list[str],
                    registry: ConditionRegistry) -> None:
    """One open condition's look: its probe re-run when due (on the host that opened it), then, on the server, its
    escalation when overdue."""
    c = condition_dict(it)
    if probed_here(c) and (force or float(c.get("next_check") or 0) <= t):
        it = _reprobe(st, it, c, t, out, registry)
        if it is None:
            return
    if server and _escalation_due(it, t):
        _escalate_overdue(st, it, t)


def recovery_tick(store: Any, registry: ConditionRegistry, now_: Any = None, force: bool = False) -> Any:
    """Each host's look (the server's tick and the laptop's sync both run it): each open condition OPENED ON THIS HOST
    whose next check is due re-runs ITS probe; a pass closes it. Backoff: PROBE_EVERY x 2^fails, at most
    RECOVER_BACKOFF_MAX. Past its deadline (10 min after opening, then every 2 h) an open condition escalates -- on the
    server only (one writer of escalations): the state's self-transition, an escalate event the notifier delivers.
    Returns the RECOVERED lines."""
    st = store
    t = now_ if now_ is not None else epoch_now()
    out: list[str] = []
    server = host_role() == "server"
    out += attempt(lambda: migrate_condition_files(st), [], catch=(Exception, SystemExit),
                   what="recover: could not migrate the condition files")
    for it in st.items("condition", ["open"]):
        _tick_condition(st, it, t, force, server, out, registry)
    return out


def recovery_recheck(kind: str, store: Any, registry: ConditionRegistry) -> None:
    """An event that may have cleared conditions of probe `kind`: probe those now."""
    try:
        for c in open_conditions(store):
            if c["probe"]["kind"] == kind and probed_here(c):
                r = run_probe(registry.check(kind), c["probe"]["args"])
                if r.ok:
                    close_condition(c, r, store, registry)
    except (Exception, SystemExit) as e:
        err(f"recover: recheck of {kind} failed ({str(e)[:120]})")


# ---------------------------------------------------------------- recovery: every non-green condition clears itself
# The owner (2026-10-28): "make this self-recover", "i want a general solution. recovery mechanism", "the way it got
# red / blocked i want it to go green as well", "structural". ONE registry, one JSON per open CONDITION in
# recovery_dir(). Every non-green state the status line or the inbox shows -- an escalation-kind inbox entry
# (CONDITION_KINDS: RED UNCLAIMED / BUDGET / ESCALATE, RED BLOCKED ON OWNER, STALLED, UNKNOWN, NETWORK), an owner block
# (`red block`), the gate UNREAD, an infra NOT LANDED -- is opened by open_condition(kind, text, probe, on_recover) and
# ONLY there: inbox_write refuses those kinds anywhere else (ConditionBypass), a blocked claim cannot be written outside
# it, and test_recovery's lint fails on a bypass in the source.
# THE RULE: the probe is the check that found the problem -- a FAILED ProbeResult, which only run_probe makes -- so the
# check that turned it red is the check that turns it green. The duties thread re-runs each open condition's probe
# (recovery_tick; per-condition backoff; never `secrets`). When it passes the condition closes itself: its inbox entries
# are acked, the gate is marked dirty, the status line refreshes, its on_recover actions run (release a block and
# dispatch the red's workflow to verify it, kick the tunnel, land a change again), and one RECOVERED note is written.
# Nobody acks a solved problem. `agentctl recover status` lists the open conditions.
CONDITION_KINDS = ("INVARIANT", "UNLANDED_ORPHAN", "STALLED", "UNKNOWN", "NETWORK")


_OPENING = [0]  # > 0 inside open_condition: the one place a condition-kind inbox entry or a block may be written


def condition_guarded(writer: Callable[[dict[str, Any]], Any]) -> Callable[[dict[str, Any]], Any]:
    """The inbox writer with the condition rule in front of it: a final, standing entry of a CONDITION_KINDS kind is
    written only inside open_condition (ConditionBypass otherwise: it would have no probe to clear it). A reply, a
    command's own verdict to its caller, is no standing condition."""
    def guarded(rec: dict[str, Any]) -> Any:
        if rec.get("final") and not rec.get("reply") and not _OPENING[0] and rec.get("kind") in CONDITION_KINDS:
            raise ConditionBypass(f"a {rec['kind']} inbox entry outside open_condition: {str(rec.get('verdict'))[:160]}")
        return writer(rec)
    return guarded
