# agentctl, part `inboxcli`: `agentctl inbox`: the owner's entries listed, waited on, matched and acknowledged, and who is asking
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import os
import re
from typing import Any
import json
import argparse
from agentctl.clock import epoch_now
from agentctl.core import HOME, run_cmd
from agentctl.procs import safe_cwd
from agentctl.fixerrecords import agent_name
from agentctl.filelog import file_event, note_events
from agentctl.inboxentry import inbox_dir, inbox_entries
from agentctl.inbox import entry_line
from agentctl.redrecord import red_what
from agentctl.alertroute import alert_audience, alert_condition_audience, alert_red_audience
from agentctl.redview import red_view
from agentctl.inboxwait import inbox_wait
from agentctl.paths import claude_session_id


# THE ESCALATION THAT WAKES SOMEBODY is an event (severity escalate) the notifier delivers once (notified_at). What is
# escalated NOW is level-triggered: every red or condition item whose state escalated and that still sits in it (a red
# offered with nobody taking it, a fix unverified, a condition still open). Taking the red, verifying the fix, the probe
# passing: the item leaves that state and the escalation is gone -- nobody acks a solved problem.
def escalated_now(store):
    """[(item type, item, words)] of the items escalated and still in that state."""
    st = store
    out = []
    t = epoch_now()
    for it in st.items("red", ["offered", "fix-pending"]):  # only what is the OWNER's (THE ALERT MODEL, alerts.py)
        if it["level"] and alert_red_audience({"kind": "RED_UNCLAIMED"}, it, st, t) == "owner":
            out.append(("red", it, f"{red_what(it['data'])}: {red_view(st, it)[1]}"))
    for it in st.items("condition", ["open"]):
        d = it["data"]
        if it["level"] and alert_condition_audience(d.get("kind"), {"verdict": d.get("text")}, d.get("opened_at"), t) == "owner":
            out.append(("condition", it, str(it["data"].get("text") or it["data"].get("kind"))))
    return out


def escalation_line(es):
    if not es:
        return ""
    first = re.sub(r"\s+--\s+`.*$", "", es[0][2])[:90]
    return f"ESCALATED: {len(es)} ({first}{'; …' if len(es) > 1 else ''})"


def entry_ids(e):
    """The change ids an inbox entry carries: its own, and those of the verdicts merged into it (alert_write)."""
    return [x for x in [e.get("entry")] + list(e.get("entries") or []) if x]


def cmd_inbox(a: argparse.Namespace, store: Any) -> int:
    if a.ids[:1] == ["escalated"]:  # the status line's: what is escalated now (the notifier already told) -- bold
        line = escalation_line(escalated_now(store))
        if line:
            print(line)
        return 0
    if a.ids[:1] == ["wait"]:
        return inbox_wait_command(a)
    entries = inbox_matches(a)
    if getattr(a, "count", False):  # every match: a count capped by the listing's --limit read "30 unread" for 44
        print(len(entries))
        return 0
    entries = entries[-a.limit:] if a.limit else entries
    inbox_print(a, entries)
    if a.ack and entries:
        inbox_acknowledge(a, entries)
    return 0


def inbox_wait_command(a: argparse.Namespace) -> int:
    """`agentctl inbox wait [--kinds K,...] [--audience A,...] [--since-ack] [--timeout S] [--forever]`."""
    if a.ids[1:]:
        print("usage: agentctl inbox wait [--kinds K,...] [--audience A,...] [--since-ack] [--timeout S] [--forever]")
        return 2
    return inbox_wait(a.kinds, a.since_ack, a.timeout, a.verbose, getattr(a, "forever", False),
                      audience=getattr(a, "audience", None) or "owner")


def inbox_matches(a: argparse.Namespace) -> list[dict[str, Any]]:
    """The inbox entries the audience, the ids and `--unread` of the command line select, oldest first."""
    entries = entries_of_audience(inbox_entries(), getattr(a, "audience", None))
    if a.ids:
        entries = [e for e in entries if entry_named_by(e, a.ids)]
    return [e for e in entries if not e["read"]] if a.unread else entries


def entries_of_audience(entries: list[dict[str, Any]], audience: str | None) -> list[dict[str, Any]]:
    """The entries addressed to one of the comma-separated audiences (`all` or none: every entry)."""
    if not audience or audience == "all":
        return entries
    want = {x.strip() for x in audience.split(",") if x.strip()}
    return [e for e in entries if alert_audience(e) in want]


def entry_named_by(e: dict[str, Any], ids: list[str]) -> bool:
    """Whether inbox entry e is the one any of `ids` names: by the start of its id, or the end of one of its ids."""
    return any(e["id"].startswith(i) or any(x.endswith(i) for x in entry_ids(e)) for i in ids)


def inbox_print(a: argparse.Namespace, entries: list[dict[str, Any]]) -> None:
    """The entries as JSON, as lines (with their tails under --verbose), or the one line saying there are none."""
    if a.json:
        print(json.dumps(entries, indent=1))
    elif not entries:
        print("inbox: no " + ("unread " if a.unread else "") + f"entries ({inbox_dir()})")
    else:
        for e in entries:
            inbox_print_entry(e, a.verbose)


def inbox_print_entry(e: dict[str, Any], verbose: bool) -> None:
    print(entry_line(e))
    for line in (e.get("tail") or []) if verbose else []:
        print(f"    {line}")


def entries_to_acknowledge(a: argparse.Namespace, entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """What `--ack` may acknowledge: the entries named by ID or the caller's own, never an owner-audience entry unless
    `--audience owner` is explicit (the owner's or orchestrator's), which acknowledges every owner entry listed."""
    owners = entries_of_audience(entries, "owner")
    if "owner" in {x.strip() for x in (getattr(a, "audience", None) or "").split(",")}:
        return owners
    named, owner_ids = set(a.ids or ()), {e["id"] for e in owners}
    return [e for e in entries if e["id"] in named or (e["id"] not in owner_ids and own_inbox_entry(e))]


def inbox_acknowledge(a: argparse.Namespace, entries: list[dict[str, Any]]) -> None:
    mine = entries_to_acknowledge(a, entries)
    os.makedirs(os.path.join(inbox_dir(), "acked"), exist_ok=True)
    for e in mine:
        open(os.path.join(inbox_dir(), "acked", e["id"]), "a").close()
    note_events([file_event("inbox", e["id"], "unread", "acked", "agent", key="acked") for e in mine])
    left = len(entries) - len(mine)
    print(f"inbox: acknowledged {len(mine)} entr{'y' if len(mine) == 1 else 'ies'}"
          + (f"; {left} left unread for the agents they belong to (`--audience owner --ack` acks the owner's)" if left else ""))


def caller_identity():
    """(session, worktree, agent name) of the process running agentctl: its Claude session, the git worktree of its cwd
    (None outside one, or at $HOME), its agent name (agent_name)."""
    here = safe_cwd()
    r = run_cmd(["git", "-C", here, "rev-parse", "--show-toplevel"]) \
        if os.path.isdir(here) else None
    top = os.path.realpath(r.stdout.strip()) if r is not None and r.returncode == 0 and r.stdout.strip() else None
    if top in (os.path.realpath(HOME), "/"):
        top = None
    return claude_session_id(), top, agent_name()


def _inside_worktree(e: Any, top: str) -> bool:
    """Inbox entry e names a path (its cwd, its owner's `@<path>`) inside the worktree `top`."""
    for p in (e.get("cwd"), str(e.get("owner") or "").partition("@")[2]):
        if p and os.path.isabs(p):
            p = os.path.realpath(p)
            if p == top or p.startswith(top.rstrip("/") + "/"):
                return True
    return False


def own_inbox_entry(e, who=None):
    """Whether inbox entry e is the caller's: its session's, from inside its worktree, or its agent name's."""
    session, top, name = who or caller_identity()
    return bool(session and e.get("owner") == session) or bool(name and e.get("agent") == name) \
        or bool(top and _inside_worktree(e, top))
