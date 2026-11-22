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


# ---------------------------------------------------------------- family owners (docs/simplification-2026-10.md item 6)
# The owner, 2026-10-29: instead of a fresh fixer per red with a 3 h maximum lease, ONE PERSISTENT AGENT SESSION PER
# FAMILY (red-families.json `"owner": true`: infra, convergence, main tests). The owner of a family:
#   - is one `claude --bg` session and one wrapper (`agentctl red owner --family F`, detached), in ONE worktree of its
#     own (<clone>-wt-owner-<family>, branch owner-<family>) that it keeps across reds and restarts, with a NOTEBOOK
#     (RED-NOTEBOOK.md in that worktree, never committed) where it keeps what it learned: reproducers, hypotheses
#     tried, what is left;
#   - holds `owner/<family>`, a lease that is its HEARTBEAT: renewed every RED_RENEW_EVERY s while the wrapper lives,
#     lapsing OWNER_TTL after it dies -- never a per-red hold with a maximum;
#   - gets the family's reds through its inbox: the dispatcher takes each new red of the family under the owner's
#     holder and hands its lease to the wrapper (owners/<family>/leases.json, 0600), with a line in RED-INBOX.md. The
#     wrapper renews those leases with its heartbeat while the session works on them; a turn ends with a verdict, and
#     the leases of that turn are released with it (LANDED, GAVE UP, DIAGNOSED, BLOCKED ON OWNER), exactly as a
#     fixer's: the red machine then verifies, re-offers -- back to the same owner -- or blocks, and the attempt ladder
#     and the crash-loop breaker count as before. New reds start the next turn of the SAME session (`--resume`);
#   - is restarted only when it died: its leases lapse, its reds are offered again, and the dispatcher starts the
#     wrapper again with the same worktree, notebook and session. It ends on its own after OWNER_IDLE with no red,
#     on RED-STOP.md, when its heartbeat lease is lost, or after OWNER_EMPTY_TURNS turns in a row with no verdict.
OWNER_TTL = 15 * 60          # its heartbeat lease lapses this long after the wrapper's last beat
