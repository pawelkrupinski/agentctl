# agentctl, part `syncplumbing`: the laptop's sync steps over its own state: the role claim, the outbox, the offline event log, the state mirror, children, events, alerts
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from typing import Any, Callable
from agentctl.core import print_error, reap_children
from agentctl.tickknobs import knob_sync_mirror
from agentctl.filelog import drain_event_log
from agentctl.store import StoreLacksCall
from agentctl.laptoprole import laptop_role_step
from agentctl.alerts import reconcile_alerts
from agentctl.storestate import mirror_state
from agentctl.eventprojection import project_events
from agentctl.outboxflush import flush_outbox


class SyncRole:
    """ONE MACHINE HOLDS THE LAPTOP ROLE: a standby machine runs none of the laptop's duties (`duty`). `held` is None
    until the loop has claimed it (a step run on its own: a test)."""

    def __init__(self, store: Any):
        self.store = store
        self.held: bool | None = None

    def claim(self) -> None:
        self.held = laptop_role_step(self.store)

    def duty(self, fn: Callable[[], Any]) -> Callable[[], Any]:
        return lambda: None if self.held is False else fn()


class MirrorPace:
    """The state mirror's step: the store's state copied again once `knob_sync_mirror` seconds have passed."""

    def __init__(self, store: Any, now: Callable[[], float]):
        self.store, self.now = store, now
        self.last = 0.0

    def __call__(self) -> None:
        if self.now() - self.last >= knob_sync_mirror():
            self.last = self.now()
            mirror_state(self.store)


def file_events_refused(e: BaseException) -> None:
    """The offline log's lines were not taken: a store older than `record_events` is said in one line, once per its
    recheck (remotestore.py LACKING_RECHECK: the call is not sent again until then, nor said; the lines stay in the
    log for a store that has it); a store that is down is said by the tunnel's and the state's own lines."""
    if isinstance(e, StoreLacksCall) and not e.remembered:
        print_error(f"file events: {e}")


def plumbing_steps(store: Any, now: Callable[[], float], role: SyncRole, *, zombies_seen: set[int], detached: list[Any],
                   notify: Callable[[], Any]) -> list[tuple[str, Callable[[], Any]]]:
    """The steps over the laptop's own state: the outbox, the offline log, the mirror, the children it started, the events
    projected, the escalations notified (`notify`) and the alerts reconciled (those two only while it holds the role)."""
    return [("outbox", lambda: flush_outbox(store)),
            ("file events", lambda: drain_event_log(store, refused=file_events_refused)),
            ("state mirror", MirrorPace(store, now)),
            ("children", lambda: reap_children(zombies_seen, detached)),
            ("events", lambda: project_events(store)),
            ("notifier", role.duty(notify)),
            ("alerts", role.duty(lambda: reconcile_alerts(store)))]
