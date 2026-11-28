# agentctl, part `ghtally`: THE TALLY of gh processes by repository and call site -- counted where forge starts one,
# printed every GH_TALLY_SECONDS.
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from agentctl.clock import epoch_now
import contextlib
import os
import re
import sys
import threading
from collections.abc import Iterator
from types import FrameType
from typing import Any
from agentctl.core import print_error


# THE QUOTA TALLY (2026-11-10): the kinowo-lander installation's 5,000 requests an hour ran out on 11-09 23:00-23:50Z, and
# at a quiet 03:27-03:31Z on 11-10 it spent ~2,500 an hour, ~960 of them the gate refresh's `gh run list` reads
# (dcde282). Nothing said where the rest went: ci-runner hides other users' processes, and no call was logged. Every
# gh process (forge_env) and every REST GET answered 304 (free) is now counted by the agentctl function that asked
# and what it ran, and each process prints its tally every GH_TALLY_SECONDS:
#   agentctl: github calls (pid P, 300 s): 41 gh processes (movies 30, core 11), 12 of them 304 (free): 20 movies
#   red_tick>run_jobs: gh run view ...  -- every site, by repository (the lander's quota is movies' alone)
GH_TALLY_SECONDS = 300
GH_TALLY_PLUMBING = {"forge_env", "gh_tally", "gh_json", "api", "_gh_api_once", "forge_run_list", "forge_actions_runs",
                     "forge_actions_run", "forge_branches", "forge_branch_sha", "forge_delete_branch",
                     "forge_release_asset", "tool_env", "drift_env",
                     "<lambda>", "<listcomp>", "<dictcomp>", "<genexpr>", "wrapper", "inner"}
_gh_tally_lock = threading.Lock()
_gh_tally_state: dict[str, Any] = {"since": epoch_now(), "calls": {}, "free": 0}
_gh_tally_kind = threading.local()


def gh_tally_site() -> str:
    """`outer>inner`: the two nearest agentctl functions above the forge plumbing that made the call."""
    names: list[str] = []
    f: FrameType | None = sys._getframe(1)
    while f is not None and len(names) < 2:
        if f.f_code.co_name not in GH_TALLY_PLUMBING and f.f_code.co_name != "<module>":
            names.append(f.f_code.co_name)
        f = f.f_back
    return ">".join(reversed(names)) or "?"


def _print_tally(calls: Any, freed: Any, span: Any) -> None:
    """The one-line tally of gh processes by repo and by call site, busiest first."""
    top = sorted(calls.items(), key=lambda kv: -kv[1])
    per_repo: dict[str, int] = {}
    for k, n in top:
        per_repo[k.split(" ", 1)[0]] = per_repo.get(k.split(" ", 1)[0], 0) + n
    print_error(f"github calls (pid {os.getpid()}, {span:.0f} s): {sum(calls.values())} gh processes "
        f"({', '.join(f'{r} {n}' for r, n in sorted(per_repo.items(), key=lambda kv: -kv[1]))}), {freed} of them 304 "
        f"(free): " + "; ".join(f"{n} {k}" for k, n in top))


def gh_tally(kind: str | None = None, free: bool = False, now_: float | None = None, o_r: str | None = None) -> None:
    """Count one gh process (or, `free`, mark the last one a 304) against its caller, and print the tally once
    GH_TALLY_SECONDS have passed since the last. `kind` names what ran; None: what gh_json/_gh_api_once said."""
    kind = kind or getattr(_gh_tally_kind, "what", None) or "gh"
    t = epoch_now() if now_ is None else now_
    with _gh_tally_lock:
        st = _gh_tally_state
        if free:
            st["free"] += 1
        else:
            key = f"{(o_r or '?').split('/')[-1]} {gh_tally_site()}: {kind}"
            st["calls"][key] = st["calls"].get(key, 0) + 1
        if t - st["since"] < GH_TALLY_SECONDS:
            return
        calls, freed, span = st["calls"], st["free"], t - st["since"]
        st.update(since=t, calls={}, free=0)
    if calls:
        _print_tally(calls, freed, span)


@contextlib.contextmanager
def gh_tally_as(what: str) -> Iterator[None]:
    """What the gh process forge_env is about to count runs (gh_json's subcommand, a REST path's shape)."""
    before, _gh_tally_kind.what = getattr(_gh_tally_kind, "what", None), what
    try:
        yield
    finally:
        _gh_tally_kind.what = before


def gh_tally_shape(path: str) -> str:
    """A REST path or gh argument with its ids and shas folded: runs/123/jobs -> runs/N/jobs."""
    return re.sub(r"\b[0-9a-f]{40}\b", "SHA", re.sub(r"\b\d+\b", "N", path.split("?")[0]))[:80]
