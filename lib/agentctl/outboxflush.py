# agentctl, part `outboxflush`: the sync's outbox flush: the changes `land` kept while the store was down, sent oldest first
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from typing import Any, Callable, Sequence
from agentctl.procs import safe_cwd
from agentctl.inboxentry import inbox_write
from agentctl.store import StoreUnavailable
from agentctl.statefile import read_record
from agentctl.syncsteps import outbox_drop, outbox_files


def flush_pending(pending: Sequence[tuple[str, Any]], submit: Callable[[Any], Any], refuse: Callable[[Any, Exception], None],
                  drop: Callable[[str], None]) -> int:
    """Each (file, change) of `pending` submitted, then dropped: the store unreachable ends it (the rest wait), a
    change the store refuses is said once (`refuse`) and dropped. Returns how many were dropped."""
    n = 0
    for p, e in pending:
        try:
            submit(e)
        except StoreUnavailable:
            break
        except Exception as ex:  # refused (bad request): said once, dropped
            refuse(e, ex)
        drop(p)
        n += 1
    return n


def outbox_pending() -> list[tuple[str, Any]]:
    """(file, change) of every change the outbox holds, oldest first; a file that reads as nothing is left out."""
    return [(p, e) for p in outbox_files() if (e := read_record(p))]


def refuse_outbox_change(e: dict[str, Any], ex: Exception) -> None:
    """The inbox's NOT LANDED line for a change the store refused."""
    inbox_write("land", f"NOT LANDED: {e.get('id')}: the store refused it ({str(ex)[:200]})", [], [], 1,
                cwd=e.get("worktree") or safe_cwd())


def flush_outbox(st: Any) -> int:
    """The changes `land` could not submit (the store down): sent now, oldest first. Returns how many went."""
    return flush_pending(outbox_pending(), st.submit, refuse_outbox_change, outbox_drop)
