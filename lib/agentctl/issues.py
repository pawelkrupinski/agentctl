# agentctl, part `issues`: the issues anyone files for the owner: opened, closed, escalated weekly; `agentctl issue`
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from agentctl.clock import epoch_now
import datetime as dt
import os
import time
from typing import Any, Optional
from agentctl.knobs import env_num
from agentctl.core import fmt_age, hashlib_sha1
from agentctl.timefmt import ts
from agentctl.store import StoreUnavailable
from agentctl.procs import safe_cwd
from agentctl.worktrees import claimant
from agentctl.statemachines import ARCHIVE, MACHINES, StaleVersion, StateSpec, TerminalState


ISSUE_TEXT_MAX = 4000


def issue_open(st: Any, text: Any, by: str, cwd: Optional[str] = None, now_: Optional[float] = None) -> Any:
    """A new open issue; returns the item."""
    t = now_ if now_ is not None else epoch_now()
    text = str(text or "").strip()[:ISSUE_TEXT_MAX]
    iid = f"{dt.datetime.fromtimestamp(t, dt.timezone.utc):%Y%m%dT%H%M%S}-{hashlib_sha1(f'{by}|{text}|{t}')[:6]}"
    return st.put_item("issue", iid, "open", {"text": text, "by": by, "at": ts(), "cwd": cwd,
                                              "session": os.environ.get("CLAUDE_CODE_SESSION_ID")})


def issue_close(st: Any, iid: str, note: str, by: str) -> Any:
    it = st.item("issue", iid)
    if not it:
        return None
    if it["state"] != "closed":
        st.transition("issue", iid, "closed", it["version"], data={"closed_by": by, "note": note, "closed_at": ts()},
                      event={"cause": "agent", "text": f"issue {iid} closed by {by}: {note}"[:400]})
    return st.item("issue", iid)


# ONE agentctl EDITOR (docs/simplification-2026-10.md, Agent B item 4): an agent that meets an agentctl defect -- a red
# fixer above all, which never edits ~/bin (the brief says so; ~/bin's pre-commit hook refuses its commits) -- files it
# with `agentctl issue "<text>"`. An issue is a store item (type `issue`): open until the maintainer closes it, shown
# on the status page, escalated each week it stays open.
MACHINES["issue"] = {
    "open": StateSpec(7 * 86400, "open", ("closed",), escalate=True, wait=True),
    "closed": StateSpec(ARCHIVE, "closed", (), terminal=True),
}


# THE WEEKLY ESCALATION (the owner, 2026-11-11, issue 20261111T143441-75a957: ONE NOTIFICATION PER ISSUE, never a
# digest). An open issue past its deadline re-enters `open`: one escalate event naming its id, filer and text, its
# level up, its deadline a week on. One issue per ISSUE_ESCALATE_GAP, the most overdue first: a backlog (69 overdue on
# 2026-11-12, none ever escalated) reaches the notifier one at a time -- more than NOTIFY_BURST in one notifier pass
# become one summary -- and their weekly deadlines stay that far apart afterwards.
ISSUE_ESCALATE_GAP = 120  # s between two issues' escalations ($AGENTCTL_ISSUE_ESCALATE_GAP)


def issue_escalation_text(it, t):
    d = it.get("data") or {}
    return (f"AGENTCTL ISSUE {it['id']} open {fmt_age(t - float(it.get('entered_at') or t))} "
            f"(filed by {d.get('by') or '?'}): {d.get('text', '')}")[:400]


def tick_issues(st, now_ = None):
    """The reconciler's step: the most overdue open issue escalated, at most one per ISSUE_ESCALATE_GAP. Returns what
    it did."""
    t = now_ if now_ is not None else st.clock()
    if t - float(st.meta_get("issue_escalated_at", 0) or 0) < env_num("AGENTCTL_ISSUE_ESCALATE_GAP", ISSUE_ESCALATE_GAP):
        return []
    due = sorted((i for i in st.items("issue", ("open",)) if i["deadline"] <= t), key=lambda i: i["deadline"])
    for it in due:
        try:
            st.transition("issue", it["id"], "open", it["version"],
                          event={"cause": "timeout", "text": issue_escalation_text(it, t), "refs": {"issue": it["id"]}})
        except (StaleVersion, TerminalState):
            continue  # closed or moved meanwhile: the next one
        st.meta_set("issue_escalated_at", t)
        return [f"issue {it['id']}: escalated (level {it['level'] + 1})"]
    return []


def status_issues(st: Any, t: float) -> list[dict[str, Any]]:
    return [{"id": i["id"], "text": i["data"].get("text", "")[:1000], "by": i["data"].get("by"),
             "at": i["data"].get("at"), "opened": i.get("entered_at"), "level": i.get("level"),
             "cwd": i["data"].get("cwd")}
            for i in sorted(st.items("issue", ("open",)), key=lambda i: i.get("entered_at") or 0, reverse=True)]


def _close_issue(a, st, by):
    it = issue_close(st, a.close, a.note or "fixed", by)
    if not it:
        print(f"issue {a.close}: no such issue")
        return 1
    print(f"issue {it['id']}: {it['state']}")
    return 0


def _list_issues(a, st):
    items = st.items("issue", None if a.all else ("open",))
    for i in sorted(items, key=lambda i: i["id"]):
        print(f"{i['id']}  {i['state']:6}  {i['data'].get('by') or '?'}: {i['data'].get('text', '')[:300]}")
    if not items:
        print("no open agentctl issues" if not a.all else "no agentctl issues")
    return 0


def _not_filed(e):
    """The line for an issue the store refused."""
    why = str(e).replace("agentctl: ", "")[:200]
    if "machine 'issue'" in why:  # the server runs a pinned revision from before `issue`
        why += "; ci-runner's agentctl pin predates `issue` (a pin bump fixes it)"
    return f"agentctl issue: NOT FILED: the store refused it ({why})"


def cmd_issue(a, st):
    """agentctl issue "<text>" | --list | --close ID [--note N]"""
    by = claimant()
    try:
        if a.close:
            return _close_issue(a, st, by)
        if a.list or not a.text:
            return _list_issues(a, st)
        it = issue_open(st, " ".join(a.text), by, cwd=safe_cwd())
    except (ValueError, StoreUnavailable) as e:
        print(_not_filed(e))
        return 2
    print(f"ISSUE {it['id']} filed: the agentctl maintainer sees it on the status page and in `agentctl issue --list`")
    return 0
