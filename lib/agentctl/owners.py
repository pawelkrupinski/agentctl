# agentctl, part `owners`: a red family owner's lease: its name, and whether its owner is alive
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from typing import Any, Optional
from agentctl.redrecord import lease_is_live


def owner_lease(fam: str) -> str:
    return f"owner/{fam}"


def owner_alive(store: Any, fam: str, t: float) -> Optional[dict[str, Any]]:
    """The owner's heartbeat lease while it is live, else None."""
    row = store.get(owner_lease(fam))
    return row if lease_is_live(row, t) else None
