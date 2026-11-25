# agentctl, part `selfci`: invariant WA, agentctl's OWN main CI and ci-runner's pin: red for long, or the pin far behind main, is the owner's
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.
#
# 2026-11-25: tests.yml was red on main for 5 h (an annotation naming `Run` above its definition, which Python 3.15
# accepts and ci-runner's 3.13 does not), ci-runner's auto-pin stayed on f90ba0e, and nothing said so: every later fix
# (holds, governor, RSS limit, refactors) sat undeployed. The pin only moves to a commit whose tests.yml run is green,
# so "red for WA_RED_MAX" and "pin more than WA_BEHIND_MAX commits behind origin/main" are the two faces of one stall.
# The watchdog's WA opens an INVARIANT condition (an inbox item for the owner, `conditions: N` on `status`).

import json
import os
import time
from typing import Any, Callable
from agentctl.core import env_num, fmt_age, git, host_role, run_cmd, state_root
from agentctl.timefmt import iso_epoch
from agentctl.statefile import read_record, write_json
from agentctl.forge import gh_json
from agentctl.fixerland import bin_repo

SELF_REPO = "pawelkrupinski/agentctl"
WA_RED_MAX = 30 * 60       # s agentctl's tests.yml may stay red on main ($AGENTCTL_WA_RED_MAX)
WA_BEHIND_MAX = 10         # commits ci-runner's pin may trail origin/main ($AGENTCTL_WA_BEHIND_MAX)
WA_READ_EVERY = 300        # s a reading (a gh list and an ssh) is kept ($AGENTCTL_WA_READ_EVERY)
PIN_LINK = "/var/lib/agentctl-src/current"  # the export the auto-pin runs: a link to the directory named by the full rev


def read_runs() -> list[dict[str, Any]]:
    """agentctl's tests.yml runs on main, newest first."""
    # listing: the newest 30 on main; red_since reads from the newest decided run down, never an absence
    return list(gh_json(SELF_REPO, "run", "list", "--workflow", "tests.yml", "--branch", "main", "--limit", "30",
                        "--json", "databaseId,number,status,conclusion,headSha,createdAt,updatedAt"))


def read_pin() -> str | None:
    """The rev ci-runner runs, read-only over ssh; None when it cannot say."""
    r = run_cmd(["ssh", "-o", "ControlPath=none", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "ci-runner",
                 f"sudo -n readlink -f {PIN_LINK}"], timeout=30)
    rev = os.path.basename(r.stdout.strip())
    return rev if r.returncode == 0 and len(rev) == 40 else None


def commits_behind(pin: str) -> int | None:
    """How many commits origin/main has past `pin`, or None (no clone, or a pin it does not know)."""
    br = bin_repo()
    if not br:
        return None
    r = git(br, "rev-list", "--count", f"{pin}..origin/main", check=False)
    return int(r.stdout.strip()) if r.returncode == 0 and r.stdout.strip().isdigit() else None


def red_since(runs: list[dict[str, Any]]) -> tuple[float, dict[str, Any]] | None:
    """(when the current red streak began, its newest run) when the newest decided run is a failure, else None. Runs
    that are not decided (queued, running, cancelled) say nothing."""
    streak = []
    for r in runs:
        if r.get("status") != "completed" or r.get("conclusion") not in ("success", "failure", "timed_out", "startup_failure"):
            continue
        if r["conclusion"] == "success":
            break
        streak.append(r)
    if not streak:
        return None
    began = iso_epoch(streak[-1].get("createdAt"))
    return (began, streak[0]) if began is not None else None


def cached(path: str, now_: float, read: Callable[[], Any], every: float) -> Any:
    """read(), kept in the file `path` for `every` seconds; a failed read leaves the last kept one standing."""
    rec = read_record(path) or {}
    if now_ - float(rec.get("at") or 0) < every and "value" in rec:
        return rec["value"]
    try:
        value = read()
    except (Exception, SystemExit):
        return rec.get("value")
    write_json(path, {"at": now_, "value": value})
    return value


def red_problem(runs: list[dict[str, Any]], now_: float) -> tuple[str | None, float | None]:
    """(what is wrong, how long the streak has been red) of tests.yml's runs: wrong only past WA_RED_MAX."""
    red = red_since(runs)
    if not red:
        return None, None
    age = now_ - red[0]
    if age <= env_num("AGENTCTL_WA_RED_MAX", WA_RED_MAX):
        return None, age
    return (f"agentctl's tests.yml has been red on main for {fmt_age(age)} (run #{red[1].get('number')} "
            f"{red[1].get('conclusion')} at {str(red[1].get('headSha'))[:7]}): ci-runner's auto-pin holds on a red run, "
            f"so no agentctl commit since is deployed"), age


def pin_problem(pin_rev: str | None, lag: int | None) -> str | None:
    if pin_rev and lag is not None and lag > env_num("AGENTCTL_WA_BEHIND_MAX", WA_BEHIND_MAX):
        return f"ci-runner's pin {pin_rev[:7]} is {lag} commits behind origin/main"
    return None


def readings(now_: float, runs: Callable[[], Any] | None, pin: Callable[[], str | None] | None) -> tuple[Any, str | None]:
    """(tests.yml's runs, the pin's rev), each kept WA_READ_EVERY seconds."""
    every = env_num("AGENTCTL_WA_READ_EVERY", WA_READ_EVERY)
    return (cached(state_root("selfci-runs.json"), now_, runs or read_runs, every),
            cached(state_root("selfci-pin.json"), now_, pin or read_pin, every))


def holding_words(red_age: float | None, pin_rev: str | None, lag: int | None) -> str:
    ci = f"red for {fmt_age(red_age)}" if red_age else "green"
    behind = f", {lag} behind main" if lag is not None else ""
    return f"agentctl's CI is {ci}; ci-runner's pin {(pin_rev or 'unread')[:7]}{behind}"


def inv_wa(clock: Any, st: Any, runs: Callable[[], Any] | None = None, pin: Callable[[], str | None] | None = None,
           behind: Callable[[str], int | None] | None = None) -> tuple[bool | None, str]:
    """WA: agentctl's own main CI has not been red past WA_RED_MAX, and ci-runner's pin trails origin/main by at most
    WA_BEHIND_MAX commits. Read from the laptop (ci-runner reads itself through its auto-pin)."""
    if host_role() == "server":
        return True, "the laptop watches agentctl's CI"
    now_ = clock.time()
    runs_now, pin_rev = readings(now_, runs, pin)
    if runs_now is None:
        return None, f"cannot read {SELF_REPO}'s tests.yml runs"
    red_words, red_age = red_problem(runs_now, now_)
    lag = (behind or commits_behind)(pin_rev) if pin_rev else None
    problems = [p for p in (red_words, pin_problem(pin_rev, lag)) if p]
    if problems:
        return False, "; ".join(problems)
    return True, holding_words(red_age, pin_rev, lag)
