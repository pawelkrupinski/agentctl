# agentctl, part `ownerleases`: a family owner's leases: the families each holder owns, handed to it and dropped, told to its session, listed
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import fcntl
import os
import time
from agentctl.statefile import read_record, write_json
from agentctl.redfamilies import red_families
from agentctl.redrecord import lease_is_live
from agentctl.owners import owner_dir, owner_lease, owner_record_path


def owner_holder(fam):
    return f"laptop:owner:{fam}"


def owner_leases_path(fam):
    return os.path.join(owner_dir(fam), "leases.json")


def family_owned(fam):
    """A configured family with a long-lived owner (red-families.json `"owner": true`)."""
    return any(f["name"] == fam and f.get("owner") for f in red_families())


def owner_leases(fam):
    return read_record(owner_leases_path(fam), {})


def owner_set_leases(fam, leases):
    write_json(owner_leases_path(fam), leases, indent=None, mode=0o600)  # lease secrets: the owner's eyes only


def owner_change_leases(fam, change):
    """The family's leases read, changed in place by change(leases) and written back, under the family's lock."""
    os.makedirs(owner_dir(fam), exist_ok=True)
    with open(os.path.join(owner_dir(fam), ".lock"), "a+") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        leases = owner_leases(fam)
        change(leases)
        owner_set_leases(fam, leases)


def owner_hand(fam, name, took):
    """The dispatcher hands a red lease it took for the owner to the owner's wrapper (its inbox of leases)."""
    owner_change_leases(fam, lambda leases: leases.update(
        {name: {"epoch": took["epoch"], "secret": took["secret"], "at": time.time()}}))


def owner_drop(fam, names):
    def drop(leases):
        for n in names:
            leases.pop(n, None)
    owner_change_leases(fam, drop)


def owner_tell_leases(st, leases, s, wt, me, rec):
    """Each red lease the owner holds carries its worktree, notebook, wrapper pid and session (`red status` shows the
    holder's worktree, never "?"): sent once per lease epoch and session."""
    for n, x in leases.items():
        key = (x.get("epoch"), rec.get("session"), s.get("bid"))
        if s["meta"].get(n) == key:
            continue
        st.set_meta(n, x["epoch"], x["secret"], {"worktree": wt, "notebook": rec.get("notebook"), "pid": me,
                                                  "session": rec.get("session"), "bg_id": s.get("bid")})
        s["meta"][n] = key


def owners_status(store, t):
    """Each configured owner family: its record and whether its heartbeat lives (the status model's `owners`)."""
    out = []
    for f in red_families():
        if not f.get("owner"):
            continue
        rec = read_record(owner_record_path(f["name"])) or {}
        row = store.get(owner_lease(f["name"]))
        out.append({"family": f["name"], "alive": lease_is_live(row, t), "worktree": rec.get("worktree"),
                    "notebook": rec.get("notebook"), "session": rec.get("session"), "turn": rec.get("turn"),
                    "restarts": rec.get("restarts"), "started": rec.get("started"), "ended": rec.get("ended"),
                    "verdict": rec.get("verdict"), "reds": len(owner_leases(f["name"])),
                    "context": rec.get("context_tokens"), "rollovers": rec.get("rollovers") or []})
    return out
