# agentctl, part `regenholds`: the laptop answers a regen hold itself: `agentctl land` started in the held change's worktree
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from contextlib import suppress
from typing import Any
from agentctl.core import git
from agentctl.syncsteps import is_dir, start_regen_land


# THE LAPTOP ANSWERS A REGEN HOLD ITSELF (2026-11-20, phase8-units: held a day for `cd <worktree> && agentctl land`, a
# command its lane, finished, never ran). A REGEN NEEDED hold (changelap.py regen_hold: a generated row both sides changed,
# which ci-runner, with no JVM, kept as main's, and the checks failed over) waits for the one command the laptop can run
# -- the rebase and the repo's regen in the change's own worktree. The laptop's sync now runs it: for each queued change
# so held whose worktree is on this machine, clean and still at the held head, `agentctl land` there, detached, once per
# change and head (it queues the change again in the held entry's place). A dirty worktree, or one moved on, is its
# agent's: left to it, as before. AN OVERDUE HOLD'S RECOVERY (changelap.py regen_hold_overdue) is a new `regen_round` on
# the item: answered once more, and `regen_answered` says the round the laptop started (the owner's escalation reads it).


def _regen_held(it: Any) -> Any:
    dd = it.get("data") or {}
    if it["state"] != "queued" or ((dd.get("hold") or {}).get("kind")) != "regen":
        return None
    return dd.get("worktree"), dd.get("head"), int(dd.get("regen_round") or 0)


def _regen_answerable(it: Any, answered: dict[tuple[str, str, int], bool]) -> Any:
    """(worktree, head, round) of a queued regen-held change this laptop can answer now (`answered`: the caller's memo of
    (change id, head, round) -> True, its `agentctl land` started), else None."""
    held = _regen_held(it)
    if not held:
        return None
    wt, head, rnd = held
    if not wt or not head or (it["id"], head, rnd) in answered or not is_dir(wt):
        return None
    if git(wt, "rev-parse", "HEAD", check=False).stdout.strip() != head \
            or git(wt, "status", "--porcelain", check=False).stdout.strip():
        return None  # its agent's: moved on, or at work in it
    return wt, head, rnd


def answer_regen_holds(d: str, st: Any, answered: dict[tuple[str, str, int], bool]) -> list[str]:
    """The laptop's step (above): `agentctl land` started in the worktree of each regen-held change it can answer; its
    lines."""
    out = []
    for it in st.items("change", ["queued"]):  # only a queued change is held for its regeneration (_regen_held)
        target = _regen_answerable(it, answered)
        if not target:
            continue
        wt, head, rnd = target
        answered[(it["id"], head, rnd)] = True
        with suppress(Exception):  # said for the owner's escalation only; the land runs all the same
            st.update_item("change", it["id"], it["version"], {"regen_answered": rnd})
        start_regen_land(d, it["id"], wt)
        out.append(f"{it['id']}: held for its regeneration; `agentctl land` started in {wt} for it")
    return out
