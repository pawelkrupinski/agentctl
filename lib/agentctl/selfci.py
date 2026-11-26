# agentctl, part `selfci`: invariant WA, agentctl's OWN main CI and ci-runner's pin: red for long, the pin stuck behind a green run, or CI undecided for long, is the owner's
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.
#
# 2026-11-25: tests.yml was red on main for 5 h (an annotation naming `Run` above its definition, which Python 3.15
# accepts and ci-runner's 3.13 does not), ci-runner's auto-pin stayed on f90ba0e, and nothing said so: every later fix
# (holds, governor, RSS limit, refactors) sat undeployed. The pin only moves to a commit whose tests.yml run is green,
# so "red for WA_RED_MAX" and "a green run left unpinned for WA_UNPINNED_MAX" are the two faces of one stall, and
# "no run decided for the head for WA_UNDECIDED_MAX" is the third. A raw commit count is NOT an alert (2026-11-25: a
# 10-commit restructure push tripped it at 12 behind while CI was healthy and still testing it); it is only shown.
# The watchdog's WA opens an INVARIANT condition (an inbox item for the owner, `conditions: N` on `status`).

import json
import os
import re
import time
from typing import Any, Callable
from agentctl.configfile import env_num
from agentctl.core import fmt_age, git, host_role, run_cmd, state_root
from agentctl.timefmt import iso_epoch
from agentctl.statefile import read_record, write_json
from agentctl.cistate import state_read
from agentctl.forge import gh_json
from agentctl.redrepos import diagnosis, read_failure
from agentctl.fixerland import bin_repo
from agentctl.configfile import knob_wa_red_max

SELF_REPO = "pawelkrupinski/agentctl"
WA_UNPINNED_MAX = 15 * 60  # s the newest green run may stay unpinned ($AGENTCTL_WA_UNPINNED_MAX)
WA_UNDECIDED_MAX = 60 * 60  # s main's head may go without a decided run ($AGENTCTL_WA_UNDECIDED_MAX)
DECIDED = ("success", "failure", "timed_out", "startup_failure")
WA_READ_EVERY = 300        # s a gh list is kept ($AGENTCTL_WA_READ_EVERY)
WA_SSH_EVERY = 1800        # s the ssh fallback's reading is kept at least (each new ssh auth may cost the owner a Touch ID)
PIN_LINK = "/var/lib/agentctl-src/current"  # the export the auto-pin runs: a link to the directory named by the full rev


def read_runs() -> list[dict[str, Any]]:
    """agentctl's tests.yml runs on main, newest first."""
    # listing: the newest 30 on main; red_since reads from the newest decided run down, never an absence
    return list(gh_json(SELF_REPO, "run", "list", "--workflow", "tests.yml", "--branch", "main", "--limit", "30",
                        "--json", "databaseId,number,status,conclusion,headSha,createdAt,updatedAt"))


def pin_from_state() -> str | None:
    """The rev ci-runner runs, as its reconciler reports it in the state mirror (`status`: "code <rev>"): the reconciler
    runs the auto-pin's export, so its rev is the pin's (12 hex). No ssh. None when the mirror carries none."""
    rev = str(state_read().get("rev") or "")
    return rev if re.fullmatch(r"[0-9a-f]{12}", rev) else None


def read_pin_over_ssh() -> str | None:
    """The fallback: the rev ci-runner runs, read-only over ssh through the shared ControlMaster (never ControlPath=none:
    a dedicated connection authenticates afresh, and every Touch ID-gated auth is a prompt); None when it cannot say."""
    r = run_cmd(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "ci-runner",
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


def decided(r: dict[str, Any]) -> bool:
    return r.get("status") == "completed" and r.get("conclusion") in DECIDED


def red_since(runs: list[dict[str, Any]]) -> tuple[float, dict[str, Any]] | None:
    """(when the current red streak began, its newest run) when the newest decided run is a failure, else None. Runs
    that are not decided (queued, running, cancelled) say nothing."""
    streak = []
    for r in runs:
        if not decided(r):
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


def red_problem(runs: list[dict[str, Any]], now_: float, why: str = "") -> tuple[str | None, float | None]:
    """(what is wrong, how long the streak has been red) of tests.yml's runs: wrong only past WA_RED_MAX."""
    red = red_since(runs)
    if not red:
        return None, None
    age = now_ - red[0]
    if age <= knob_wa_red_max():
        return None, age
    return (f"agentctl's tests.yml has been red on main for {fmt_age(age)} (run #{red[1].get('number')} "
            f"{red[1].get('conclusion')} at {str(red[1].get('headSha'))[:7]}): ci-runner's auto-pin holds on a red run, "
            f"so no agentctl commit since is deployed" + (f" -- {why}; fix the test, never rerun it" if why else "")), age


def pins(pin_rev: str, sha: Any) -> bool:
    return str(sha).startswith(pin_rev[:12])


def newest_unpinned_green(runs: list[dict[str, Any]], pin_rev: str) -> dict[str, Any] | None:
    """The newest green run, unless it is the pin or the pin is a newer run."""
    for r in runs:
        if pins(pin_rev, r.get("headSha")):
            return None
        if decided(r) and r["conclusion"] == "success":
            return r
    return None


def unpinned_problem(runs: list[dict[str, Any]], pin_rev: str | None, now_: float) -> str | None:
    """The newest green run left unpinned for over WA_UNPINNED_MAX: the auto-pin is stuck."""
    green = newest_unpinned_green(runs, pin_rev) if pin_rev else None
    done = iso_epoch((green or {}).get("updatedAt") or (green or {}).get("createdAt"))
    if green is None or done is None or now_ - done <= env_num("AGENTCTL_WA_UNPINNED_MAX", WA_UNPINNED_MAX):
        return None
    return (f"ci-runner's pin {str(pin_rev)[:7]} has not followed the green run #{green.get('number')} at "
            f"{str(green.get('headSha'))[:7]} for {fmt_age(now_ - done)}: the auto-pin is stuck")


def undecided_problem(runs: list[dict[str, Any]], now_: float) -> str | None:
    """Main's head has had no decided run for over WA_UNDECIDED_MAX (the leading runs are queued, running, cancelled)."""
    leading = []
    for r in runs:
        if decided(r):
            break
        leading.append(r)
    began = iso_epoch(leading[-1].get("createdAt")) if leading else None
    if began is None or now_ - began <= env_num("AGENTCTL_WA_UNDECIDED_MAX", WA_UNDECIDED_MAX):
        return None
    return f"no tests.yml run has decided for main's head in {fmt_age(now_ - began)}: CI is stuck or queued"


def red_why(runs: list[dict[str, Any]], now_: float, failure: Callable[[Any], str | None] | None) -> str:
    """The diagnosis of the red streak's newest run (read once per run, kept), once the streak is past WA_RED_MAX."""
    red = red_since(runs)
    if not red or now_ - red[0] <= knob_wa_red_max():
        return ""
    run_id = red[1].get("databaseId") or red[1].get("number")
    path = state_root("selfci-why.json")
    kept = read_record(path) or {}
    if kept.get("run") != run_id:
        kept = {"run": run_id, "why": diagnosis((failure or (lambda rid: read_failure(SELF_REPO, rid)))(run_id))}
        write_json(path, kept)
    return str(kept.get("why") or "")


def readings(now_: float, runs: Callable[[], Any] | None, pin: Callable[[], str | None] | None) -> tuple[Any, str | None]:
    """(tests.yml's runs, the pin's rev), each kept WA_READ_EVERY seconds."""
    every = env_num("AGENTCTL_WA_READ_EVERY", WA_READ_EVERY)
    pin_rev = pin_from_state() if pin is None else None
    if pin_rev is None:
        pin_rev = cached(state_root("selfci-pin.json"), now_, pin or read_pin_over_ssh, max(every, WA_SSH_EVERY))
    return cached(state_root("selfci-runs.json"), now_, runs or read_runs, every), pin_rev


def holding_words(red_age: float | None, pin_rev: str | None, lag: int | None) -> str:
    ci = f"red for {fmt_age(red_age)}" if red_age else "green"
    behind = f", {lag} behind main" if lag is not None else ""
    return f"agentctl's CI is {ci}; ci-runner's pin {(pin_rev or 'unread')[:7]}{behind}"


def inv_wa(clock: Any, st: Any, runs: Callable[[], Any] | None = None, pin: Callable[[], str | None] | None = None,
           behind: Callable[[str], int | None] | None = None,
           failure: Callable[[Any], str | None] | None = None) -> tuple[bool | None, str]:
    """WA: deploys are not stuck: agentctl's own main CI has not been red past WA_RED_MAX, the newest green run is
    pinned within WA_UNPINNED_MAX, and the head has had a decided run within WA_UNDECIDED_MAX. The commit lag is
    informational. Read from the laptop (ci-runner reads itself through its auto-pin)."""
    if host_role() == "server":
        return True, "the laptop watches agentctl's CI"
    now_ = clock.time()
    runs_now, pin_rev = readings(now_, runs, pin)
    if runs_now is None:
        return None, f"cannot read {SELF_REPO}'s tests.yml runs"
    red_words, red_age = red_problem(runs_now, now_, red_why(runs_now, now_, failure))
    lag = (behind or commits_behind)(pin_rev) if pin_rev else None
    problems = [p for p in (red_words, unpinned_problem(runs_now, pin_rev, now_), undecided_problem(runs_now, now_)) if p]
    if problems:
        return False, "; ".join(problems)
    return True, holding_words(red_age, pin_rev, lag)
