# agentctl, part `conditions`: THE CONDITION MACHINE: a non-green condition opened with the probe that closes it, rechecked with backoff, escalated, closed with its recovery actions; the probe and action registries
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import glob
import json
import os
import re
import time
from typing import Any, Callable
from agentctl.core import env_num, err, fmt_age, host_role, reds_dir, remote, state_root
from agentctl.timefmt import iso_at, ts
from agentctl.statefile import read_record
from agentctl.gating import mark_gate_dirty
from agentctl.inboxentry import ConditionBypass, _OPENING, inbox_dir, inbox_write
from agentctl.statemachines import StaleVersion, TerminalState


RECOVER_BACKOFF_MAX = 600  # a probe is re-run at most this far apart, however often it failed ($AGENTCTL_RECOVER_MAX)


class ProbeResult:
    """What one probe said: ok True (passes), False (fails) or None (cannot tell: stays open). Made by run_probe only."""
    __slots__ = ("kind", "args", "ok", "detail", "at")

    def __init__(self, kind, args, ok, detail, _made_by_run_probe=None):
        if _made_by_run_probe is not _RUN_PROBE_TOKEN:
            raise ConditionBypass("a ProbeResult is made by run_probe only: the check that opens is the check that closes")
        self.kind, self.args, self.ok, self.detail, self.at = kind, dict(args), ok, str(detail)[:300], time.time()


_RUN_PROBE_TOKEN = object()


def recovery_dir(*parts):
    d = os.environ.get("AGENTCTL_RECOVERY_DIR") or (reds_dir("recovery") if os.environ.get("AGENTCTL_REDS_DIR")
                                                   else state_root("recovery"))
    return os.path.join(d, *parts)


# THE REGISTRIES: every probe (`kind` -> its check of args: (ok True/False/None, detail)) and every recovery action
# (`name` -> run on a condition's close: (condition, args) -> what it did) lives with the code it reads; the
# composition root (cli) wires them in, so this machine reads no part above it.
PROBES: dict[str, Callable[[dict[str, Any]], tuple[Any, str]]] = {}
RECOVER_ACTIONS: dict[str, Callable[[dict[str, Any], dict[str, Any], Any], str]] = {}


def wire_probes(probes: dict[str, Callable[[dict[str, Any]], tuple[Any, str]]]) -> None:
    PROBES.update(probes)


def wire_recover_actions(actions: dict[str, Callable[[dict[str, Any], dict[str, Any], Any], str]]) -> None:
    RECOVER_ACTIONS.update(actions)


PROBE_EVERY = {"disk_free": 300, "quota_reset": 300, "invariant": 60, "waiting_ok": 30, "orphan_landed": 300, "route": 5, "tunnel": 10, "gate_read": 15,
               "workflow_green": 60, "token": 60, "tcp": 60, "http": 120, "git_fetch": 120,
               "clone_health": 120, "infra_requeue": 60, "push_credential": 60, "change_source": 60, "red_newer_verdict": 60,
               "batch_progress": 30, "red_read": 300}


def probe_words(kind, a):
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
            "batch_progress": lambda: "the waiting changes form a batch (or the gate turns red)",
            "red_read": lambda: f"ci-runner reads {', '.join(a.get('repos') or [])} (the agentctl-reader App)",
            "red_newer_verdict": lambda: f"a newer {a.get('workflow')} run than #{a.get('run')} no longer fails this way"}.get(kind, lambda: kind)()


def run_probe(kind: str, args: dict[str, Any] | None = None) -> ProbeResult:
    """Run probe `kind` on args: the ONE way to a ProbeResult. A probe that raises is ok=None (cannot tell)."""
    if kind not in PROBES:
        raise ConditionBypass(f"unknown probe {kind!r}: every condition needs a probe from PROBES")
    args = dict(args or {})
    try:
        ok, detail = PROBES[kind](args)
    except (Exception, SystemExit) as e:
        ok, detail = None, f"probe error: {str(e)[:160]}"
    # True, False or None, never a falsy number (2026-11-16: W1's `x or (0.0 and y)` was 0.0 on a green main: neither
    # `ok is False`, so the watchdog never refreshed its condition, nor true, so nothing closed it)
    return ProbeResult(kind, args, None if ok is None else bool(ok), detail, _made_by_run_probe=_RUN_PROBE_TOKEN)


def condition_key(kind, probe):
    return re.sub(r"[^\w.-]+", "_", f"{kind}-{probe.kind}-" + "-".join(f"{v}" for _k, v in sorted(probe.args.items())
                                                                           if _k != "since"))[:150]


_last_recovery = [0.0]


def maybe_recovery_tick(store: Any) -> None:
    if time.time() - _last_recovery[0] < 5:
        return
    _last_recovery[0] = time.time()
    recovery_tick(store)


# -- conditions are items (design §5.4; phase 1 step 12): the store's "condition" machine, open -> closed
CONDITION_ESCALATE_EVERY = 2 * 3600  # an open condition escalates 10 min after opening, then every 2 h


def condition_item_of(store, key):
    """The open condition item for key, or None."""
    for it in store.items("condition", ["open"]):
        if it["data"].get("key") == key:
            return it
    return None


def condition_dict(it):
    return {**it["data"], "id": it["id"], "version": it["version"], "deadline": it["deadline"], "level": it["level"]}


# A BLIP OPENS NOTHING (the owner, 2026-11-17: 19 UNKNOWN spells in a week, 18 closed by themselves in ~70 s --
# "movies token present" -- each an inbox entry and a notification for nothing): a kind in CONDITION_GRACE opens only
# once it is seen failing again at least that long after it was first seen (one sighting run: sightings no more than
# CONDITION_PENDING_LAPSE apart; a run that lapsed starts again). Until then the sighting is a pending mark in the
# store's meta (`condition_pending:<key>`), and open_condition answers None as for a passing probe. A store that cannot
# keep the mark opens the condition at once, as before: loud over silent.
UNKNOWN_GRACE = 300  # s an UNKNOWN condition must keep failing (seen again past this) before it opens


CONDITION_PENDING_LAPSE = 1800  # s between two sightings after which a pending condition's sighting run starts again


def condition_grace(kind):
    return env_num("AGENTCTL_UNKNOWN_GRACE", UNKNOWN_GRACE) if kind == "UNKNOWN" else 0


def condition_pending(st, key, grace, t):
    """True while key's failure is younger than grace (THE BLIP rule above): the sighting is marked, nothing opens."""
    if grace <= 0:
        return False
    mk = f"condition_pending:{key}"
    try:
        p = json.loads(st.meta_get(mk) or "null") or {}
        lapse = env_num("AGENTCTL_CONDITION_PENDING_LAPSE", CONDITION_PENDING_LAPSE)
        if not p or t - float(p.get("last") or 0) > lapse:
            p = {"first": t}
        if t - float(p["first"]) < grace:
            st.meta_set(mk, json.dumps({"first": p["first"], "last": t}))
            return True
        st.meta_set(mk, "")
    except (Exception, SystemExit) as e:
        err(f"condition {key}: its pending mark could not be kept ({str(e)[:120]}): opened at once")
    return False


def open_condition(kind: str, text: str, probe: Any, on_recover: Any, key: str | None = None, inbox: bool = True,
                   command: str = "recover", exit_code: int = 1, cwd: str | None = None, tail: Any = (), effect: Any = None,
                   *, store: Any, grace: float | None = None) -> Any:
    """Open (or refresh) a non-green condition: an item of the "condition" machine. probe: the FAILED ProbeResult of the
    check that found it (required: the same probe, passing, closes it); on_recover: [(action, args)] from
    RECOVER_ACTIONS. effect: a callable run inside; its dict result is merged into every on_recover args. Writes ONE
    inbox entry (text) per spell. Returns the condition, or None when the probe passes now (nothing is wrong) or the
    failure is still inside its kind's grace (grace: seconds, None for the kind's: THE BLIP rule above). Its time is
    the store's clock."""
    if not isinstance(probe, ProbeResult):
        raise ConditionBypass(f"open_condition({kind!r}) without a ProbeResult: no condition without its recovery probe")
    for name, _a in on_recover:
        if name not in RECOVER_ACTIONS:
            raise ConditionBypass(f"unknown on_recover action {name!r}")
    if probe.ok:
        return None
    st = store
    key = key or condition_key(kind, probe)
    cur = condition_item_of(st, key)
    if not cur and condition_pending(st, key, condition_grace(kind) if grace is None else grace, st.clock()):
        return None
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
    t = st.clock()
    new = [[n, {**x, **extra}] for n, x in on_recover]
    if cur:  # refreshed: re-described too, so its escalations say what is wrong NOW, not at its opening
        d = cur["data"]
        acts = list(d.get("on_recover") or []) + [x for x in new if x not in (d.get("on_recover") or [])]
        st.update_item("condition", cur["id"], cur["version"], {"on_recover": acts, "last_checked": t, "text": text,
                                                                 "last_result": {"ok": probe.ok, "detail": probe.detail}})
        return condition_dict(st.item("condition", cur["id"]))
    iid, n = key, 1
    while st.item("condition", iid) is not None:  # an earlier spell is closed: a new item
        n += 1
        iid = f"{key}.{n}"
    data = {"key": key, "kind": kind, "text": text, "probe": {"kind": probe.kind, "args": probe.args},
            "until": probe_words(probe.kind, probe.args), "on_recover": new, "opened": ts(), "opened_at": t,
            "inbox": ids, "last_checked": t, "last_result": {"ok": probe.ok, "detail": probe.detail}, "fails": 0,
            "next_check": t + PROBE_EVERY.get(probe.kind, 60), "command": command, "cwd": cwd, "host": host_role()}
    return condition_dict(st.put_item("condition", iid, "open", data))


def open_conditions(store):
    """The open conditions, as dicts (key, kind, text, probe, on_recover, inbox, ... plus id and version)."""
    return [condition_dict(it) for it in store.items("condition", ["open"])]


def close_condition(cond, result, store):
    """The probe passed (result: its ProbeResult, or a reason string for a manual close): the item moves to closed (a
    compare-and-set: one closer), its inbox entries are acked, its on_recover actions run, one RECOVERED note."""
    st = store
    if isinstance(result, str):
        result = ProbeResult(cond["probe"]["kind"], cond["probe"]["args"], True,
                             result if result.startswith("INVALID:") else f"closed by hand: {result}",
                             _made_by_run_probe=_RUN_PROBE_TOKEN)
    it = st.item("condition", cond["id"])
    if not it or it["state"] != "open":
        return None
    manual = str(result.detail).startswith(("INVALID:", "closed by hand:"))
    reason = str(result.detail)
    try:
        st.transition("condition", it["id"], "closed", it["version"],
                      data={"closed": ts(), "closed_by": reason},
                      event={"text": f"{cond.get('kind')} closed: {reason[:200]}", "refs": {"key": cond.get("key")}})
    except (StaleVersion, TerminalState):
        return None  # another closer won
    acked = os.path.join(inbox_dir(), "acked")
    os.makedirs(acked, exist_ok=True)
    for eid in it["data"].get("inbox") or []:
        open(os.path.join(acked, eid), "a").close()
    did = []
    for name, a in it["data"].get("on_recover") or []:
        try:
            w = RECOVER_ACTIONS[name](cond, a, st)
            if w:
                did.append(w)
        except (Exception, SystemExit) as e:
            did.append(f"{name} failed: {str(e)[:120]}")
    mark_gate_dirty()
    line = ((f"CLOSED (unjustified): {reason[len('INVALID: '):]}: {cond.get('text', '')[:200]}"
             if reason.startswith("INVALID:") else f"CLOSED: {reason[len('closed by hand: '):]}: {cond.get('text', '')[:200]}")
            if manual else
            f"RECOVERED: {cond.get('until')} ({result.detail}): {cond.get('text', '')[:200]}"
            + (f"; {'; '.join(did)}" if did else ""))
    err(line)
    inbox_write("recover", line, [line], [], 0)
    return line


def migrate_condition_files(store):
    """Step 12's migration: every open condition file under recovery_dir() becomes a condition item; the file goes."""
    out = []
    for p in sorted(glob.glob(recovery_dir("*.json"))):
        c = read_record(p)
        if not c or not c.get("probe"):
            continue
        if c.get("kind") != "BLOCKED" and not condition_item_of(store, c["key"]):
            data = {k: v for k, v in c.items() if k != "path"}
            data.setdefault("opened_at", time.time())
            data["escalate_after"] = time.time() + CONDITION_ESCALATE_EVERY  # its inbox entry told already: no storm
            store.put_item("condition", c["key"], "open", data)
            out.append(f"migrated the condition {c['key']}")
        try:
            os.unlink(p)
        except OSError:
            pass
    return out


def probed_here(c):
    """A condition's probe runs on the host role that opened it (its `host`; before it was recorded: the server's):
    the laptop's watchdog, tunnel or token conditions read the laptop -- on ci-runner a W5 probe found no plist and
    closed, `route -n get default` never answered, and the laptop's inbox entries were acked in the server's."""
    if c["probe"]["kind"] == "token" and host_role() == "sync":
        return True  # the processor's socket is the laptop's: a token condition the server opened (2026-11-02:
        # token-movies stayed open for good, ci-runner has no socket) is cleared by the laptop's probe
    return c.get("host", "server") == host_role()


def recovery_tick(store, now_=None, force=False):
    """Each host's look (the server's tick and the laptop's sync both run it): each open condition OPENED ON THIS HOST
    whose next check is due re-runs ITS probe; a pass closes it. Backoff: PROBE_EVERY x 2^fails, at most
    RECOVER_BACKOFF_MAX. Past its deadline (10 min after opening, then every 2 h) an open condition escalates -- on the
    server only (one writer of escalations): the state's self-transition, an escalate event the notifier delivers.
    Returns the RECOVERED lines."""
    st = store
    t = now_ if now_ is not None else time.time()
    out = []
    server = host_role() == "server"
    try:
        out += migrate_condition_files(st)
    except (Exception, SystemExit) as e:
        err(f"recover: could not migrate the condition files ({str(e)[:160]})")
    for it in st.items("condition", ["open"]):
        c = condition_dict(it)
        if probed_here(c) and (force or float(c.get("next_check") or 0) <= t):
            r = run_probe(c["probe"]["kind"], c["probe"]["args"])
            if r.ok:
                line = close_condition(c, r, store=st)
                if line:
                    out.append(line)
                continue
            fails = int(c.get("fails") or 0) + 1
            every = PROBE_EVERY.get(r.kind, 60) * 2 ** min(fails, 8)
            try:
                st.update_item("condition", it["id"], it["version"], {
                    "fails": fails, "last_checked": t, "last_result": {"ok": r.ok, "detail": r.detail},
                    "next_check": t + min(every, env_num("AGENTCTL_RECOVER_MAX", RECOVER_BACKOFF_MAX))})
            except StaleVersion:
                continue
            it = st.item("condition", it["id"])
        if server and it["state"] == "open" and t >= max(it["deadline"], float(it["data"].get("escalate_after") or 0)):
            age = fmt_age(t - float(it["data"].get("opened_at") or t))
            line = f"ESCALATED: {it['data'].get('kind')} open for {age}: {str(it['data'].get('text'))[:200]}"
            try:
                st.transition("condition", it["id"], "open", it["version"], deadline_s=CONDITION_ESCALATE_EVERY,
                              event={"text": line, "audience": {"worktree": it["data"].get("cwd")} if it["data"].get("cwd")
                                     else {}, "refs": {"verdict": line, "key": it["data"].get("key"), "command": "recover"}})
            except StaleVersion:
                pass
    return out


def recovery_recheck(kind, store):
    """An event that may have cleared conditions of probe `kind`: probe those now."""
    try:
        for c in open_conditions(store):
            if c["probe"]["kind"] == kind and probed_here(c):
                r = run_probe(kind, c["probe"]["args"])
                if r.ok:
                    close_condition(c, r, store=store)
    except (Exception, SystemExit) as e:
        err(f"recover: recheck of {kind} failed ({str(e)[:120]})")


def recovery_recheck_all(store):
    """The network changed or came back: every open condition is probed at the next tick, backoff reset."""
    st = store
    for it in st.items("condition", ["open"]):
        try:
            st.update_item("condition", it["id"], it["version"], {"next_check": 0, "fails": 0})
        except StaleVersion:
            pass


def cmd_recover(a, store):
    if a.rcmd == "tick":
        lines = recovery_tick(store, force=True)
        print("\n".join(lines) if lines else "recover: nothing cleared")
        return 0
    cs = open_conditions(store)
    if not cs:
        print("recover: no open conditions")
        return 0
    t = time.time()
    for c in cs:
        lr = c.get("last_result") or {}
        print(f"{c['kind']} {c['key']} (open since {c.get('opened')}{', ESCALATED' if c.get('level') else ''}): "
              f"{c.get('text', '')[:140]}\n"
              f"  probe: {c['probe']['kind']} {json.dumps({k: v for k, v in c['probe']['args'].items() if k != 'since'})}"
              f" -- clears when {c.get('until')}\n"
              f"  last checked {iso_at(float(c.get('last_checked') or 0))}: {'cannot tell' if lr.get('ok') is None else 'fails'}"
              f" ({lr.get('detail')}); next check in {max(0, int(float(c.get('next_check') or 0) - t))} s"
              f" (fails {c.get('fails') or 0})\n"
              f"  on recover: {', '.join(n for n, _x in c.get('on_recover') or []) or 'ack + gate dirty'}")
    return 0
