# agentctl, part `laptoprole`: THE LAPTOP ROLE: which machine holds the laptop duties (a lease), this machine's id, and the standby line of the others
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import os
import re
import subprocess
import sys
from agentctl.core import process_cache
from agentctl.timefmt import day_hhmmz
from agentctl.store import open_store
from agentctl.procs import socket_host
from agentctl.leases import lease_meta
from agentctl.core import env_num, err, state_root
from agentctl.statefile import read_record, write_json
from agentctl.inboxentry import inbox_write


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


LAPTOP_ROLE_TTL = 10 * 60          # s a silent holder keeps the role ($AGENTCTL_LAPTOP_ROLE_TTL)


LAPTOP_ROLE_MAX = 100 * 365 * 86400


def laptop_role_path():
    return state_root("laptop-role.json")


def laptop_role(store, t=None, take=False):
    """This machine's claim on the laptop role: (True, its lease row) when it holds it (acquired, or renewed by this
    pass), else (False, the other holder's live row). take: the role taken over from a live holder (`laptop take`)."""
    t = store.clock() if t is None else t
    me = laptop_role_holder()
    ttl = env_num("AGENTCTL_LAPTOP_ROLE_TTL", LAPTOP_ROLE_TTL)
    row = store.get(LAPTOP_ROLE_LEASE)
    live = bool(row and row.get("holder") and float(row.get("expires_at") or 0) > t)
    mine = read_record(laptop_role_path()) or {}
    if live and row["holder"] != me and not take:
        return False, row
    if live and row["holder"] == me and mine.get("epoch") == row.get("epoch"):
        r = store.renew(LAPTOP_ROLE_LEASE, mine["epoch"], mine["secret"], f"beat {t}", ttl)
        if r in ("ok", "no_progress"):
            return True, store.get(LAPTOP_ROLE_LEASE)
    if live:  # taken over (`laptop take`), or ours from a state file this machine lost: the lease is begun again
        store.revoke(LAPTOP_ROLE_LEASE, f"taken by {me}" if row["holder"] != me else "renewed after a restart")
    got = store.acquire(LAPTOP_ROLE_LEASE, me, ttl, LAPTOP_ROLE_MAX, progress=f"beat {t}",
                        meta={"kind": "laptop-role", "host": role_host(), "machine_id": machine_id(),
                              "since": t, "took_from": (row or {}).get("holder")})
    if "held" in got:
        return False, store.get(LAPTOP_ROLE_LEASE)
    write_json(laptop_role_path(), {"epoch": got["epoch"], "secret": got["secret"], "holder": me, "since": t})
    return True, store.get(LAPTOP_ROLE_LEASE)


def laptop_role_step(store=None):
    """The sync loop's first step: the role claimed or renewed. Standby: said to this machine's owner ONCE per holder
    (its inbox), then only in `agentctl status`. Returns True when this machine holds the role."""
    st = store or open_store()
    held, row = laptop_role(st)
    rec = read_record(laptop_role_path()) or {}
    if held:
        if rec.get("told"):
            rec.pop("told", None)
            write_json(laptop_role_path(), rec)
        return True
    if rec.get("told") != (row or {}).get("holder"):
        line = f"LAPTOP STANDBY: {laptop_role_line(row)}"
        inbox_write("laptop", line, [line], [], 1)
        err(line)
        write_json(laptop_role_path(), {**rec, "told": (row or {}).get("holder")})
    return False


def cmd_laptop(a):
    """`agentctl laptop status|take [--yes]`: who holds the laptop role; take it for this machine."""
    st = open_store()
    if a.lcmd == "status":
        row = st.get(LAPTOP_ROLE_LEASE)
        line = laptop_role_standby(st)
        if line:
            print(line)
        elif row and row.get("holder"):
            print(f"laptop role held by this machine ({role_host()}, {machine_id()}) since "
                  f"{day_hhmmz(lease_meta(row).get('since') or 0)}")
        else:
            print("laptop role: free (the next sync pass takes it)")
        return 0
    line = laptop_role_standby(st)
    if line and not a.yes:
        if not sys.stdin.isatty():
            print(f"{line}\nNOT TAKEN: confirm with `agentctl laptop take --yes`")
            return 1
        if input(f"{line}\nTake the laptop role for {role_host()}? Type yes: ").strip() != "yes":
            print("NOT TAKEN")
            return 1
    held, row = laptop_role(st, take=bool(line))
    print(f"TAKEN: laptop role held by {role_host()} ({machine_id()})" if held else f"NOT TAKEN: {laptop_role_line(row)}")
    return 0 if held else 1


LAPTOP_HOLDER_CALLS = ("acquire", "take", "revoke")


def laptop_role_speaks(store, role):
    """None when a client naming `role` (X-Agentctl-Role; None: names none) speaks for the laptop; else why not."""
    row = store.get(LAPTOP_ROLE_LEASE)
    if not row:  # no role ever held (a fresh store, one machine, tests): nothing to refuse
        return None
    if role is None:
        return f"{laptop_role_line(row).split(';')[0]}, and this client is an agentctl from before the laptop role"
    if not row.get("holder") or float(row.get("expires_at") or 0) <= store.clock() or row["holder"] == role:
        return None
    return f"{laptop_role_line(row).split(';')[0]}, and this client is {role}"


def laptop_role_refusal(store, path, body, role):
    """THE STORE ENFORCES THE ROLE: a `laptop:` lease (a fixer, an owner, a claim) is acquired, taken or revoked only by
    the machine that holds the laptop role (laptop_role_speaks; pawel-mbp's agentctl on 2026-11-18/19 named none). The
    refusal's text, or None."""
    m = path[len("/v1/store/"):] if path.startswith("/v1/store/") else ""
    if m not in LAPTOP_HOLDER_CALLS or not isinstance(body, dict):
        return None
    args, kw = body.get("args") or [], body.get("kwargs") or {}
    if m == "revoke":  # its holder is the lease's own
        name = kw.get("name") if "name" in kw else (args[0] if args else None)
        holder = (store.get(name) or {}).get("holder") if isinstance(name, str) else None
    else:
        holder = kw.get("holder") if "holder" in kw else (args[1] if len(args) > 1 else None)
    if not str(holder or "").startswith("laptop:"):
        return None
    why = laptop_role_speaks(store, role)
    return f"STANDBY: {holder} refused: {why}" if why else None
