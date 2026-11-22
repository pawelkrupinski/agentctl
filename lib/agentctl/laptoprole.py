# agentctl, part `laptoprole`: THE LAPTOP ROLE: which machine holds the laptop duties (a lease), this machine's id, and the standby line of the others
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import os
import re
import subprocess
from agentctl.core import process_cache
from agentctl.timefmt import day_hhmmz
from agentctl.store import open_store
from agentctl.procs import socket_host
from agentctl.leases import lease_meta


# ---------------------------------------------------------------- the laptop role: ONE machine holds it
# ONE MACHINE HOLDS THE LAPTOP ROLE (2026-11-18 23:41Z, core Main Pipeline #1053, red 0c8eea91b712): a second Mac
# (`pawel-mbp`, its store tunnel key `store-tunnel@pawel-mbp`) still ran agentctl's sync loop against ci-runner's store,
# so both machines acted as "the laptop": it dispatched fixer red-0c8eea91b712-1 (pid 83138, session 3acddaae), this
# machine (Pawes-MacBook-Pro-2) found no such pid among its own processes and revoked it, and the two took turns in a
# crash loop. host_role() says only "sync"; both were it. Now the role is a LEASE in the shared store, `role/laptop`,
# held by `role:<host>:<machine id>` (macOS IOPlatformUUID) and renewed by every sync pass. A machine that finds it held
# by another live holder is STANDBY: it refuses every laptop duty (the dispatcher, red_dispatch, owners, the notifier,
# the alerts, the orphan reaper, the pins, the explorer) and tells its owner so ONCE. `agentctl laptop take` hands the
# role over (confirmed); a holder silent past LAPTOP_ROLE_TTL is taken over by the next machine that asks. The holder
# names no `laptop:` prefix: its clock never pauses with the laptop's contact (A SILENT MACHINE'S LEASE CLOCKS PAUSE),
# so a dead machine's role lapses. A fresh store with no role lease standing refuses nothing (one machine, tests).
LAPTOP_ROLE_LEASE = "role/laptop"


_laptop_role_seen = process_cache({})


def machine_id():
    """This machine's stable id: $AGENTCTL_MACHINE_ID, else macOS's IOPlatformUUID, else its hostname."""
    if os.environ.get("AGENTCTL_MACHINE_ID"):
        return os.environ["AGENTCTL_MACHINE_ID"]
    if "id" not in _laptop_role_seen:
        uid = None
        try:
            out = subprocess.run(["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"], capture_output=True, text=True,
                                 timeout=10).stdout
            m = re.search(r'"IOPlatformUUID"\s*=\s*"([^"]+)"', out)
            uid = m.group(1) if m else None
        except (OSError, subprocess.SubprocessError):
            pass
        _laptop_role_seen["id"] = uid or socket_host()
    return _laptop_role_seen["id"]


def role_host():
    return os.environ.get("AGENTCTL_HOSTNAME") or socket_host()


def laptop_role_holder():
    return f"role:{role_host()}:{machine_id()}"


def laptop_role_standby(store=None, t=None):
    """None when this machine may do laptop duties (it holds the role, or no live holder stands); else the standby
    line naming the holder. Read only: never acquires."""
    try:
        st = store or open_store()
        t = st.clock() if t is None else t
        row = st.get(LAPTOP_ROLE_LEASE)
    except Exception:  # an unread store decides nothing here: the duty's own store calls fail on their own
        return None
    if not row or not row.get("holder") or float(row.get("expires_at") or 0) <= t or row["holder"] == laptop_role_holder():
        return None
    return laptop_role_line(row)


def laptop_role_line(row):
    m = lease_meta(row)
    since = m.get("since") or row.get("acquired_at")
    when = day_hhmmz(since) if since else "?"
    return (f"laptop role held by {m.get('host') or row.get('holder')} since {when}; this machine "
            f"({role_host()}) is standby -- `agentctl laptop take` hands the role to it")
