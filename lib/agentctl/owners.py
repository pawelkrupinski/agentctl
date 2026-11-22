# agentctl, part `owners`: a red family owner's lease: its name, and whether its owner is alive
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from typing import Any, Optional
from agentctl.redrecord import lease_is_live
import os
import re
from agentctl.core import reds_dir


def owner_lease(fam: str) -> str:
    return f"owner/{fam}"


def owner_alive(store: Any, fam: str, t: float) -> Optional[dict[str, Any]]:
    """The owner's heartbeat lease while it is live, else None."""
    row = store.get(owner_lease(fam))
    return row if lease_is_live(row, t) else None


def owner_dir(fam):
    return reds_dir("owners", re.sub(r"[^A-Za-z0-9_.-]", "_", fam))


def owner_record_path(fam):
    return os.path.join(owner_dir(fam), "owner.json")
