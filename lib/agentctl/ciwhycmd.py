# agentctl, part `ciwhycmd`: the `ci-why` command: the gate explained for main or a run, or the processor's last published line
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from typing import Any
from agentctl.clock import epoch_now
from agentctl.core import slug
from agentctl.cistate import state_read
from agentctl.gatesummary import GATE_LINE
from agentctl.owned import default_repo
from agentctl.timelinecmd import argparse
from agentctl.statusline import status_gate_text
from agentctl.cireport import ci_why


def ci_why_cached(o_r: str, a: argparse.Namespace, target: Any) -> int:
    """`ci-why --cached --summary`: the gate line as the processor last published it."""
    if not getattr(a, "summary", False) or target:
        raise SystemExit("agentctl: ci-why --cached goes with --summary and main")
    # NEVER LIVE WORK (2026-10-28: a stale file sent the status line's 15 s refresh into a 60 s live read): the
    # processor's published line, or -- stale or missing -- what it last said, marked with its age. Live reads
    # belong to the processor's publish step only.
    text = status_gate_text(state_read(), epoch_now(), o_r)  # the status model's gate text, for this repo
    print(text)
    return 1 if text.startswith("gate RED") else 0 if GATE_LINE.match(text) else 2  # 2: UNREAD / ? / NETWORK


def cmd_ci_why(a: argparse.Namespace, store: Any) -> int:
    o_r = slug(a.repo) if a.repo else default_repo()
    target = None if a.target in (None, "main") else a.target.lstrip("#")
    if target is not None and not target.isdigit():
        raise SystemExit("agentctl: ci-why takes a run id, a run number (#N) or `main`")
    if getattr(a, "cached", False):
        return ci_why_cached(o_r, a, target)
    red, text = ci_why(o_r, a.tail, target, summary=getattr(a, "summary", False), store=store)
    print(text)
    return 1 if red else 0
