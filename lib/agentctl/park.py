# agentctl, part `park`: a parked worktree: the marks, their release, the digest, `agentctl park` / `unpark`
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import datetime as dt
import os
import re
import time
from agentctl.core import HOME, env_num, git
from agentctl.timefmt import ts
from agentctl.statefile import read_record, write_json
from agentctl.inboxentry import inbox_write
from agentctl.conditions import recovery_dir
from agentctl.queueclose import change_landed
from agentctl.superseded import change_chain_end


# PARKED (the owner, 2026-11-03: "fix root cause and prevent structurally" -- movies-wt-bare-borrow (a design branch the
# owner is still deciding on), -flip-stage5 (an unproven design kept for reference), -order-rows (blocked on another
# change landing first) escalated UNLANDED ORPHAN over and over: an orphan was "author gone + commits not on main", with
# no way to say "this work is parked for a reason"). `agentctl park <worktree> --reason R` with one condition:
#   --until-landed <change id | commit>   re-surfaces once that change LANDED (or its re-submission did) or the commit
#                                        is on main by content;
#   --until <date>                        re-surfaces at that time;
#   --owner-decision                      only `agentctl unpark` (a person) ends it.
# A parked worktree gets no orphan action and no escalation (an open UNLANDED ORPHAN clears: "parked: R"); `agentctl
# status` shows `parked: N` with the reasons. Each maintain pass reads every condition again (self-recovering): met, the
# mark is RELEASED and the orphan rules apply afresh (its orphan records dropped, so it gets its one submit -- it lands).
# A parked worktree older than PARK_DIGEST_DAYS is named in ONE weekly digest line (an inbox entry that wakes nobody).
PARK_DIGEST_DAYS = 14


PARK_DIGEST_EVERY = 7 * 86400


def parked_path():
    return recovery_dir("parked.json")


def parked_marks():
    """{realpath of the parked worktree: its mark} (state: parked | released | unparked)."""
    return read_record(parked_path()) or {}


def parked_mark(wt):
    m = parked_marks().get(os.path.realpath(wt))
    return m if m and m.get("state") == "parked" else None


def park_until_text(m):
    u = m.get("until") or {}
    if u.get("kind") == "landed":
        return f"until {u.get('change') or ''}{' / ' if u.get('change') and u.get('commit') else ''}{(u.get('commit') or '')[:11]} lands"
    if u.get("kind") == "date":
        return f"until {u.get('at')}"
    return "until the owner decides"


def park_release_why(m, repo, store, t=None):
    """Why mark m's condition is met now, else None (unread: None -- the mark stands)."""
    t = time.time() if t is None else t
    u = m.get("until") or {}
    if u.get("kind") == "date":
        return f"its date {u['at']} passed" if t >= float(u.get("epoch") or 0) else None
    if u.get("kind") != "landed":
        return None
    if u.get("change"):
        try:
            end = change_chain_end(store, u["change"])
        except (Exception, SystemExit):
            end = None
        if end and end["state"] == "landed":
            return f"its blocker {end['id']} landed"
    if u.get("commit"):
        done, via = change_landed(repo, u["commit"])
        if done:
            return f"its blocker {u['commit'][:11]} is on main" + (f" ({via[:10]})" if via and via != u["commit"] else "")
    return None


def drop_orphan_records(wt):
    """Forget worktree wt's orphan records: the orphan rules start afresh for it (its one submit)."""
    path = recovery_dir("orphans.json")
    st = read_record(path) or {}
    rw = os.path.realpath(wt)
    keep = {k: v for k, v in st.items() if os.path.realpath(str((v or {}).get("worktree") or "")) != rw}
    if len(keep) != len(st):
        write_json(path, keep)


def park_duty(repo, store, dry_run=False, now_=None):
    """One maintain pass over the parks: a met condition releases its mark; the weekly digest of the old ones."""
    t = time.time() if now_ is None else now_
    marks = parked_marks()
    did, changed, old = [], False, []
    for wt, m in sorted(marks.items()):
        if m.get("state") != "parked":
            continue
        name = os.path.basename(wt)
        if not os.path.isdir(wt):
            if not dry_run:
                m.update(state="released", why="its worktree is gone", decided=ts())
                changed = True
            did.append(f"parked {name}: released (its worktree is gone)")
            continue
        why = park_release_why(m, m.get("repo") or repo, store, t)
        if why:
            if not dry_run:
                m.update(state="released", why=why, decided=ts())
                changed = True
                drop_orphan_records(wt)
                inbox_write("maintain", f"UNPARKED {wt.replace(HOME, '~')}: {why}; the orphan rules apply again "
                                        f"(parked: {m['reason']})", [], [], 0, cwd=wt)
            did.append(f"parked {name}: released ({why}); the orphan rules apply again")
            continue
        if t - float(m.get("at") or t) >= env_num("AGENTCTL_PARK_DIGEST_DAYS", PARK_DIGEST_DAYS) * 86400:
            old.append((wt, m))
    last = max([float(m.get("digest_at") or 0) for _w, m in old] or [0])
    if old and t - last >= PARK_DIGEST_EVERY:
        line = "PARKED DIGEST: " + "; ".join(
            f"{os.path.basename(w)} ({int((t - float(m['at'])) // 86400)} d, {park_until_text(m)}: {m['reason']})" for w, m in old)
        if not dry_run:
            inbox_write("maintain", line, [], [], 0)
            for _w, m in old:
                m["digest_at"] = t
            changed = True
        did.append(line)
    if changed:
        write_json(parked_path(), marks)
    return did


def parked_lines():
    """`agentctl status`'s parked section: `parked: N` and one line per parked worktree with its reason."""
    live = sorted((w, m) for w, m in parked_marks().items() if m.get("state") == "parked")
    return [f"parked: {len(live)}"] + [f"  {os.path.basename(w)}  {park_until_text(m)}: {m['reason']}" for w, m in live]


def resolve_blocker(x, repo, store):
    """{change, commit} of `--until-landed x`: a change id (whole or a unique part; its head the commit), else a commit
    of repo. Raises ValueError."""
    st = store
    try:
        items = st.items("change")
    except (Exception, SystemExit):
        items = []
    its = [i for i in items if i["id"] == x] or [i for i in items if x in i["id"]]
    if len(its) == 1:
        return {"change": its[0]["id"], "commit": (its[0].get("data") or {}).get("head")}
    if len(its) > 1:
        raise ValueError(f"--until-landed {x}: {len(its)} change ids match it")
    r = git(repo, "rev-parse", "--verify", "-q", f"{x}^{{commit}}", check=False)
    if r.returncode == 0 and r.stdout.strip():
        return {"change": None, "commit": r.stdout.strip()}
    if re.fullmatch(r"\d{8}-\d{6}-[\w.-]+", x):  # a change id the store does not hold (yet): named, read each pass
        return {"change": x, "commit": None}
    raise ValueError(f"--until-landed {x}: no change id and no commit of that name")


def park_target(path):
    wt = os.path.realpath(os.path.expanduser(path))
    top = git(wt, "rev-parse", "--show-toplevel", check=False).stdout.strip() if os.path.isdir(wt) else ""
    if not top:
        return None, None
    common = git(top, "rev-parse", "--path-format=absolute", "--git-common-dir").strip()
    return os.path.realpath(top), (os.path.dirname(common) if common.endswith("/.git") else common)


def cmd_park(a, store):
    marks = parked_marks()
    if not a.worktree:
        for line in parked_lines():
            print(line)
        return 0
    wt, repo = park_target(a.worktree)
    if not wt:
        print(f"park: {a.worktree} is no git worktree")
        return 2
    conds = [c for c in (a.until_landed, a.until, a.owner_decision or None) if c]
    if not a.reason or len(conds) != 1:
        print('usage: agentctl park <worktree> --reason "<why>" (--until-landed <change id|commit> | --until <date> | '
              "--owner-decision)")
        return 2
    if a.until_landed:
        try:
            until = {"kind": "landed", **resolve_blocker(a.until_landed, repo, store)}
        except ValueError as e:
            print(f"park: {e}")
            return 2
    elif a.until:
        try:
            when = dt.datetime.fromisoformat(a.until.replace("Z", "+00:00"))
        except ValueError:
            print(f"park: --until {a.until}: not a date (YYYY-MM-DD or an ISO-8601 time)")
            return 2
        until = {"kind": "date", "at": a.until, "epoch": when.timestamp()}
    else:
        until = {"kind": "owner"}
    marks[wt] = {"reason": a.reason, "until": until, "at": time.time(), "parked": ts(), "state": "parked", "repo": repo}
    write_json(parked_path(), marks)
    print(f"PARKED {wt.replace(HOME, '~')} {park_until_text(marks[wt])}: {a.reason} -- no orphan escalation")
    return 0


def cmd_unpark(a):
    marks = parked_marks()
    wt, _repo = park_target(a.worktree)
    wt = wt or os.path.realpath(os.path.expanduser(a.worktree))
    m = marks.get(wt)
    if not m or m.get("state") != "parked":
        print(f"unpark: {wt.replace(HOME, '~')} is not parked")
        return 0
    m.update(state="unparked", decided=ts())
    write_json(parked_path(), marks)
    drop_orphan_records(wt)
    print(f"UNPARKED {wt.replace(HOME, '~')}: the orphan rules apply again")
    return 0
