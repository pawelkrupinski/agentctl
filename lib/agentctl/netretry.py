# agentctl, part `netretry`: the network's one bounded retry: a call or a run that failed the network's way tried again (net_retry, net_run)
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import subprocess
import time
from typing import Any, Callable, Optional, Sequence, TypeVar
from agentctl.procrunner import process_runner
from agentctl.core import err
from agentctl.networksignatures import is_transient_network


# THE NETWORK'S TABLE AND ITS ONE BOUNDED RETRY (the table is networksignatures'; classify's INFRA_SIGNATURES is built on it too).
# A TRANSIENT NETWORK FAILURE IS RETRIED, NEVER A VERDICT (2026-11-22: packet loss on the laptop's uplink to ci-runner,
# gh and GitHub API timeouts). Each read of the forge, each fetch and each idempotent push goes through net_retry /
# net_run: a failure whose text is the network's (TRANSIENT_NETWORK: every row of the table below -- a connection that
# broke, a name that did not resolve, a timeout, the forge's 5xx) is tried again NET_RETRY_DELAYS apart (2 s to 20 s),
# within NET_RETRY_BUDGET seconds of the first try (so a call that hangs to its own timeout is tried again once, not
# five times), and only then raises TransientNetwork -- which its caller reads as infra (no answer), never as a red, a
# rejection or a fault of the change. Anything else (a 404, a refusal, a parse error) is raised at once: retrying it
# hides a real answer.
_NetT = TypeVar("_NetT")


NET_RETRY_DELAYS: Sequence[float] = (2, 5, 10, 20)


NET_RETRY_BUDGET = 120  # s from the first try past which no retry starts


class TransientNetwork(RuntimeError):
    """The network failed every try of a call (net_retry): no answer -- infra, never a verdict."""


class _NetBlip(RuntimeError):
    """net_run's own retryable failure (one run that failed the network's way)."""


def _network_why(e: BaseException, transient: Callable[[BaseException], bool]) -> Optional[str]:
    """Why failure `e` is the network's (its last line, or the timeout), or None when it is another failure."""
    if isinstance(e, subprocess.TimeoutExpired):
        return f"timed out after {e.timeout:g} s"
    if isinstance(e, TransientNetwork) or not transient(e):
        return None
    text = str(e).strip()
    return text.splitlines()[-1][:160] if text else type(e).__name__


def _wait_or_give_up(what: str, tries: int, delay: Optional[float], why: str, elapsed: float, budget: float,
                     sleep: Callable[[float], Any], most: int) -> None:
    """After the network failed try `tries`: wait `delay` s for the next -- or, none left (or it would start past
    `budget` s), raise TransientNetwork."""
    if delay is None or elapsed + delay > budget:
        raise TransientNetwork(f"{what}: the network failed {tries} tries ({why})")
    err(f"{what}: {why} -- the network's, tried again in {delay:g} s ({tries}/{most})")
    sleep(delay)


def net_retry(what: str, call: Callable[[], _NetT], delays: Optional[Sequence[float]] = None,
              sleep: Optional[Callable[[float], Any]] = None,
              transient: Callable[[BaseException], bool] = is_transient_network,
              clock: Optional[Callable[[], float]] = None, budget: Optional[float] = None) -> _NetT:
    """call(), tried again while it fails the network's way (_network_why). Raises TransientNetwork after the last
    try, or once the next would start past `budget` s; any other failure at once."""
    delays = NET_RETRY_DELAYS if delays is None else delays
    limit = NET_RETRY_BUDGET if budget is None else budget
    read_clock = clock or time.monotonic  # wall: waits on and times real processes and threads
    t0 = read_clock()
    for n, delay in enumerate((*delays, None)):
        try:
            return call()
        except Exception as e:
            why = _network_why(e, transient)
            if why is None:
                raise
        _wait_or_give_up(what, n + 1, delay, why, read_clock() - t0, limit, sleep or time.sleep, len(delays) + 1)  # wall: waits on and times real processes and threads
    raise AssertionError("unreachable: the last try gives up")


def _run_bounded(argv: Sequence[str], kw: Any) -> "subprocess.CompletedProcess[Any]":
    """process_runner().run(argv, **kw), a run past its `timeout` read as exit 124 ("timed out after N s"), as `timeout`
    says it: a hung call is the network's too."""
    try:
        return process_runner().run(argv, **kw)
    except subprocess.TimeoutExpired as e:
        text = bool(kw.get("text"))
        return subprocess.CompletedProcess(argv, 124, "" if text else b"",
                                           f"timed out after {e.timeout:g} s" if text else b"timed out")


def _failed_on_the_network(r: "subprocess.CompletedProcess[Any]", kw: Any) -> bool:
    return r.returncode != 0 and bool(kw.get("text")) and is_transient_network(f"{r.stderr or ''}\n{r.stdout or ''}")


def net_run(what: str, argv: Sequence[str], delays: Optional[Sequence[float]] = None,
            sleep: Optional[Callable[[float], Any]] = None, clock: Optional[Callable[[], float]] = None,
            **kw: Any) -> "subprocess.CompletedProcess[Any]":
    """process_runner().run(argv, **kw) through net_retry: a (text) run that exits non-zero with the network's words in its
    output, or times out (exit 124), is tried again; the last run's result is returned, for its caller to judge
    (is_transient_network(its output) says it was the network all along). Never raises for the network."""
    last: list["subprocess.CompletedProcess[Any]"] = []

    def once() -> "subprocess.CompletedProcess[Any]":
        last[:] = [_run_bounded(argv, kw)]
        if _failed_on_the_network(last[0], kw):
            raise _NetBlip((last[0].stderr or last[0].stdout or "").strip()[-160:] or f"exit {last[0].returncode}")
        return last[0]
    try:
        return net_retry(what, once, delays, sleep, transient=lambda e: isinstance(e, _NetBlip), clock=clock)
    except TransientNetwork:
        return last[0]


