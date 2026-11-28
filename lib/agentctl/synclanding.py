# agentctl, part `synclanding`: the laptop's sync steps over landed work: the workspace trust asks, landed worktrees and changes, the local queue, regen holds
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from typing import Any, Callable
from agentctl.core import print_errors
from agentctl.fixersession import trust_asks
from agentctl.landedchange import close_landed_changes, migrate_local_queue
from agentctl.regenholds import answer_regen_holds
from agentctl.syncworktrees import close_landed_worktrees


def landing_steps(repo: str, d: str, store: Any, clock: Any, duty: Callable[[Callable[[], Any]], Callable[[], Any]], *,
                  landed_looked: dict[str, float], regen_answered: dict[tuple[str, str, int], bool]) -> list[tuple[str, Callable[[], Any]]]:
    """Each step says what it did, one line each: the trust asks, the worktrees of landed changes closed, the local queue
    migrated, landed changes closed (`landed_looked`: when each was looked at), regen holds answered (`regen_answered`,
    while the laptop holds the role: `duty`)."""
    return [("workspace trust", lambda: print_errors(trust_asks())),
            ("landed worktrees", lambda: print_errors(close_landed_worktrees(store))),
            ("local queue", lambda: print_errors(migrate_local_queue(repo, store), "land: ")),
            ("landed changes", lambda: print_errors(close_landed_changes(repo, clock, store=store, looked=landed_looked), "land: ")),
            ("regen holds", duty(lambda: print_errors(answer_regen_holds(d, store, regen_answered), "land: ")))]
