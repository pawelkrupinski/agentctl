# agentctl, part `invariants`: the watchdog's invariants (design §10): each a check of the state the reconciler must converge to, a violation a condition
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import os
import time
from agentctl.core import SHORT, env_num, fmt_age, git, host_role, run_cmd, state_root
from agentctl.timefmt import hhmmz, parse_time
from agentctl.statefile import read_record, write_json
from agentctl.store import change_exits_unannounced, open_store
from agentctl.repos import movies_repo_path
from agentctl.redrecord import red_what
from agentctl.procs import process_cwds, ps
from agentctl.procwatch import inv_wp, inv_wt
from agentctl.conditions import open_conditions
from agentctl.changekind import change_hold_visible
from agentctl.cistate import snapshot_gate_reds
from agentctl.projector import notify_failure
from agentctl.fixerland import bin_repo
from agentctl.ciwhy import ci_state_load
from agentctl.queuebatch import hold_recovers_itself
from agentctl.queueclose import change_rows, close_landed_changes
from agentctl.launchd import SERVICE_LABEL, launch_agents_dir
from agentctl.beats import beat_age
from agentctl.redhold import W1_HARD_FACTOR, change_progress, inv_wr, red_unheld


# ---------------------------------------------------------------- the invariant watchdog (design §10, phase 1 step 2)
# A separate, cheap, dumb launchd job (net.bitcashier.agentctl-watchdog, every 60 s): `agentctl watchdog` reads today's
# files only -- the queue entries, the published gate line, the processor's beat file, the inbox -- and checks the
# invariants the reconciler must converge to. A violation is a condition (INVARIANT, its probe the invariant itself:
# it closes when the invariant holds again) and a macOS notification; on W5 (the processor's duties stopped) it also
# kickstarts the processor (silent over awake time; the kick keeps its token), at most once per WATCHDOG_KICK_GAP. It
# shares no code path with the processor beyond the registry and the read-only readers.
W1_MAX = 2 * 3600       # the oldest unlanded change ($AGENTCTL_W1_MAX); while main is red, counted from the red's start


W2_MAX = 10 * 60        # a gate red with nobody on it


W5_MAX = 300            # the processor's duties beat (its bounded steps take up to 2 min)


W6_MAX = 5 * 60         # a due escalation not notified


W8_WINDOW = 3600         # W8 looks at the change items that closed within this ($AGENTCTL_W8_WINDOW)


W7_MAX = 5 * 60         # phase 2: an event ci-runner wrote that the laptop's sync has not projected (60x its 5 s tick)


WM_MAX = 45 * 60        # maintain's last pass (launchd every 15 min)


def watchdog_state():
    return read_record(state_root("watchdog.json")) or {}


# A WAITING CHANGE A BATCH CARRIES IS THE BATCH'S (2026-11-22 09:56Z: the gate went green, batch #94 left blocked-red at
# once and checked impact-same-file and film-codec on the new main -- a full round, the revert having moved identity code
# -- while W1 said of both "waiting for green while the gate is green", 170 min past their own deadlines).
BATCH_AT_WORK = ("assembling", "checking", "pushing", "bisecting")


def change_carrying_batch(st, cid):
    """The open batch item whose members hold change cid, else None."""
    for b in st.items("batch", list(BATCH_AT_WORK) + ["blocked-red", "held"]):
        if cid in ((b.get("data") or {}).get("members") or []):
            return b
    return None


def w1_gate_reds(o_r):
    """(seconds the gate has been red, the red gate rows as "<workflow> #N since HH:MMZ", comma-joined) read from the
    CI snapshot itself; (0.0, "") when every gate workflow is green, (0.0, None) with no snapshot. 2026-11-22: the gate
    was red on Identity takes #349 for 7 h while Main was green, and W1 said "waiting for green while main is green":
    it read the gate from the PUBLISHED summary line, which goes unread past 180 s (a starved processor), and named no
    row."""
    snap = ci_state_load(o_r)
    if not snap:
        return 0.0, None
    reds = [g for g in snapshot_gate_reds(snap) if g.get("since")]
    if not reds:
        return 0.0, ""
    starts = [parse_time(g["since"]).timestamp() for g in reds]
    rows = ", ".join(f"{g.get('workflow') or g.get('name') or '?'} #{(g.get('decided') or {}).get('number')} since "
                     f"{hhmmz(parse_time(g['since']).timestamp())}" for g in reds)
    return time.time() - min(starts), rows


def inv_w1():
    """W1: no UNLANDED change of the one queue (the store's open items, after the landed-by-content look) older than
    W1_MAX (while main is red: past the red's own start) is outside what its state's table allows (change_progress);
    none at all older than W1_HARD_FACTOR x that. Names the change and why it has not landed."""
    repo = movies_repo_path()
    st = open_store()
    if os.path.isdir(repo):
        close_landed_changes(repo, store=st)
    open_ = [r for r in change_rows(st) if r["state"] in ("queued", "checking", "checked", "waiting")]
    if not open_:
        return True, "no change waits"
    t = time.time()
    oldest = min(open_, key=lambda r: r["since"])
    red_since, holding = w1_gate_reds(SHORT["movies"])
    lim = env_num("AGENTCTL_W1_MAX", W1_MAX)
    conds = open_conditions(st)
    bad = None
    for r in sorted(open_, key=lambda r: r["since"]):
        age = t - r["since"]
        if age < lim or (red_since and age < red_since + lim):
            continue
        it = st.item("change", r["id"]) or {}
        carried = change_carrying_batch(st, r["id"]) if r["state"] == "waiting" else None
        if carried and carried["state"] in BATCH_AT_WORK and t < float(carried.get("deadline") or 0):
            continue  # its batch carries it, within the batch's own deadline: the batch's bounds judge it
        stranded = any(c["key"] == f"change-stranded-{r['id']}" for c in conds)
        off = "STRANDED, no queue entry carries it" if stranded else change_progress(st, it, t) if it else "no item"
        if off is None and carried:
            off = (f"carried by batch {carried['id']} ({carried['state']}"
                   + (", past its deadline" if t >= float(carried.get("deadline") or 0) else "") + ")")
        if off is None and r["state"] == "waiting" and not red_since:
            off = "waiting for green while the gate is green" if holding is not None else \
                "waiting for green, the gate unread (no CI snapshot)"
        elif r["state"] == "waiting" and holding:
            off = f"{off}; " if off else ""
            off += f"held by the red gate: {holding}"
        if off is None and age < W1_HARD_FACTOR * lim + (red_since or 0):
            continue
        if off is None and it and hold_recovers_itself(change_hold_visible(st, it, {}, t)):
            continue  # a REGEN NEEDED hold: its own recovery escalates it once (queueclose.py regen_hold_overdue)
        bad = (r, age, off or f"past {W1_HARD_FACTOR} x its bound, though its state still says it progresses")
        break
    if not bad:
        age = t - oldest["since"]
        return True, (f"oldest unlanded change {oldest['id']} ({oldest['state']}) is {fmt_age(age)} old, "
                      "within what its state's table allows")
    r, age, off = bad
    return False, f"unlanded change {r['id']} ({r['state']}: {off}) is {fmt_age(age)} old" + \
        (f", the gate red for {fmt_age(red_since)} ({holding})" if red_since else "")


def inv_w2():
    """W2: every red has a live lease, an open offer, a fix on its way or an owner block -- for no red is that missing
    for W2_MAX (read from the store)."""
    try:
        bad = red_unheld(open_store())
    except Exception as e:
        return None, f"cannot read the store ({str(e)[:120]})"
    st = watchdog_state()
    first = st.get("w2_since")
    now_ = time.time()
    if not bad:
        if first:
            write_json(state_root("watchdog.json"), {**st, "w2_since": None})
        return True, "every red is held or offered"
    if not first:
        first = now_
        write_json(state_root("watchdog.json"), {**st, "w2_since": first})
    it = bad[0]
    return now_ - first < env_num("AGENTCTL_W2_MAX", W2_MAX), \
        f"{red_what(it['data'])} ({it['state']}) has no lease, offer, fix or block for {fmt_age(now_ - first)}"


def inv_w5():
    """W5: the processor's duties beat within W5_MAX (only where the processor service is installed)."""
    if not os.path.exists(os.path.join(launch_agents_dir(), f"{SERVICE_LABEL}.plist")):
        return True, "no processor service here"
    age = beat_age("processor")
    if age is None:
        return None, "no processor beat yet (a processor from before the watchdog)"
    return age < env_num("AGENTCTL_W5_MAX", W5_MAX), f"the processor's duties beat {int(age)} s ago"


def inv_wn():
    """WN: the owner's notifications get through -- the notifier's last delivery did not fail on both channels
    (terminal-notifier and the osascript fallback). Raised once, naming the cause and the owner's fix for the channel
    that failed (notify_fix: a timeout is the load's, not a permission); cleared by the notifier's next delivery that
    gets through (or nothing left undelivered)."""
    try:
        f = notify_failure(open_store())
    except Exception as e:
        return None, f"cannot read the store ({str(e)[:120]})"
    if not f:
        return True, "notifications reach the owner"
    return False, (f"notifications cannot reach the owner ({str(f.get('cause'))[:200]}; {f.get('pending', '?')} waiting): "
                   f"{f.get('fix') or 'see the cause'}")


def inv_w6():
    """W6: no escalation event left unnotified for over W6_MAX (the notifier's delivery, from the store). While the
    delivery itself is broken (WN) that is WN's one condition, not a count of every escalation waiting behind it."""
    try:
        st = open_store()
        if notify_failure(st):
            return None, "the notifier cannot deliver (see WN)"
        t = st.clock()
        late = [e for e in st.events(after=max(0, int(st.meta_get("notifier_seq", 0) or 0) - 500), limit=5000)
                if e["severity"] == "escalate" and not e.get("notified_at") and t - e["at"] > env_num("AGENTCTL_W6_MAX", W6_MAX)]
    except Exception as e:
        return None, f"cannot read the store ({str(e)[:120]})"
    return not late, (f"{len(late)} escalation(s) not notified: {str(late[0].get('text'))[:100]}" if late
                      else "every escalation was notified")


def inv_w7():
    """W7 (phase 2, the sync role only): the laptop's sync keeps up with ci-runner's events -- no event older than
    W7_MAX is still past its projector cursor (meta projector_seq). The store not answering is no verdict (the tunnel
    and ci-runner have their own conditions). Raised and cleared by this same check, as an INVARIANT condition."""
    if host_role() != "sync":
        return True, "no sync here (the server)"
    try:
        st = open_store()
        behind = st.events(after=int(st.meta_get("projector_seq", 0) or 0), limit=1)
    except Exception as e:
        return None, f"cannot read the store ({str(e)[:120]})"
    if not behind:
        return True, "the sync is up to date with the event feed"
    age = time.time() - float(behind[0]["at"])
    return age <= env_num("AGENTCTL_W7_MAX", W7_MAX), \
        f"the sync is behind ci-runner's events: #{behind[0]['seq']} unprojected for {fmt_age(age)}"


def inv_w8():
    """W8: no change item left the queue (landed, rejected, superseded, cancelled) without an event addressed to its
    author's inbox -- Store.transition completes every such event (change_exit_event); this catches a writer around it.
    Looks at the items closed within W8_WINDOW, so it clears itself once a stray one ages out (and the fix lands)."""
    try:
        st = open_store()
        silent = change_exits_unannounced(st, st.clock() - env_num("AGENTCTL_W8_WINDOW", W8_WINDOW))
    except Exception as e:
        return None, f"cannot read the store ({str(e)[:120]})"
    return not silent, (f"{len(silent)} change(s) left the queue with no inbox entry: {', '.join(silent[:3])}"
                        if silent else "every change that left the queue told its author")


def inv_wm():
    """WM: maintain (the reaper; the watchdog's own watcher) ran within WM_MAX."""
    if not os.path.exists(os.path.join(launch_agents_dir(), "net.bitcashier.agentctl-maintain.plist")):
        return True, "no maintain service here"
    age = beat_age("maintain")
    if age is None:
        return None, "no maintain beat yet"
    return age < env_num("AGENTCTL_WM_MAX", WM_MAX), \
        f"maintain last ran {fmt_age(age) + ' ago' if age is not None else 'never'}"


WB_MAX = 10 * 60


def inv_wb():
    """WB: ~/bin's own checkout carries no uncommitted edit older than WB_MAX (work goes in worktrees: an edit there
    blocks every other agent's merge). Names the processes working in it."""
    br = bin_repo()
    if not br:
        return True, "no agentctl repository here"
    files = [l[3:] for l in git(br, "status", "--porcelain", "--untracked-files=no", check=False).stdout.splitlines()
             if l.strip() and not l[3:].startswith("briefs/")]
    if not files:
        return True, f"{br} is clean"
    newest = max((os.path.getmtime(os.path.join(br, f)) for f in files if os.path.exists(os.path.join(br, f))), default=0)
    if time.time() - newest < env_num("AGENTCTL_WB_MAX", WB_MAX):
        return True, f"{br}: {len(files)} file(s) edited just now"
    try:
        cwds = process_cwds()
        names = {pid: cmd for pid, _pp, _e, cmd in ps()}
        who = [f"pid {p} {names.get(p, '')[:60]}" for p, c in cwds.items()
               if os.path.realpath(c) == os.path.realpath(br) or os.path.realpath(c).startswith(os.path.realpath(br) + "/")]
    except (Exception, SystemExit):
        who = []
    return False, (f"{br} has uncommitted edits ({', '.join(files[:4])}) unchanged for {fmt_age(time.time() - newest)}"
                   + (f"; working there: {'; '.join(who[:3])}" if who else "; nobody works there now") +
                   ": commit them in a worktree of ~/bin")


WF_PCT = 70  # % of kern.maxfiles open system-wide past which WF is violated ($AGENTCTL_WF_PCT)


WF_VM = "com.apple.Virtualization"


WF_VM_RELEASE = "~/.rd/bin/rdctl shell -- sudo sh -c 'sync; echo 2 > /proc/sys/vm/drop_caches'"


def file_table_reading():
    """(kern.num_files, kern.maxfiles), or None where there is no such sysctl (ci-runner's Linux) or it cannot say."""
    try:
        r = run_cmd(["sysctl", "-n", "kern.num_files", "kern.maxfiles"], timeout=10)
        num, most = (int(x) for x in r.stdout.split())
    except (OSError, ValueError):
        return None
    return (num, most) if r.returncode == 0 and most > 0 else None


def file_holders(lsof_out):
    """`lsof -n -F pc` -> [(descriptors, pid, command)], the most first."""
    counts, pid, names = {}, None, {}
    for line in lsof_out.splitlines():
        tag, value = line[:1], line[1:]
        if tag == "p":
            pid = int(value) if value.isdigit() else None
        elif tag == "c" and pid is not None:
            names[pid] = value
        elif tag == "f" and pid is not None:
            counts[pid] = counts.get(pid, 0) + 1
    return sorted(((n, p, names.get(p, "?")) for p, n in counts.items()), key=lambda x: (-x[0], x[1]))


def inv_wf(reading=False, lsof_out=None):
    """WF: the system-wide file table under WF_PCT of kern.maxfiles. 2026-11-20 it overflowed (ENFILE: no process
    could open a file): Rancher Desktop's VM held ~200k virtiofs descriptors, one for every file and directory a
    container had seen in ~25 worktrees (`run-script-tests --linux` mounted each; it now mounts fixed copies). Past the
    bound it names the top holders, and for the VM the command that releases what its guest caches."""
    reading = file_table_reading() if reading is False else reading
    if not reading:
        return True, "no kern.num_files here"
    num, most = reading
    pct, bound = 100 * num // most, env_num("AGENTCTL_WF_PCT", WF_PCT)
    said = f"{num} of {most} open ({pct}%)"
    if pct < bound:
        return True, said
    if lsof_out is None:
        try:
            lsof_out = run_cmd(["lsof", "-n", "-F", "pc"], timeout=60).stdout
        except OSError:
            lsof_out = ""
    top = file_holders(lsof_out)[:3]
    detail = f"{said}, past {bound:g}%; top: " + \
        ", ".join(f"pid {p} {c} ({n})" for n, p, c in top)
    if top and top[0][2].startswith(WF_VM):
        detail += f"; Rancher Desktop's VM caches every file a container saw: release it with {WF_VM_RELEASE}"
    return False, detail


INVARIANTS = {"WB": inv_wb, "W1": inv_w1, "WR": inv_wr, "W2": inv_w2, "W5": inv_w5, "W6": inv_w6, "W7": inv_w7, "W8": inv_w8, "WM": inv_wm,
              "WN": inv_wn, "WF": inv_wf, "WP": inv_wp, "WT": inv_wt}


def probe_invariant(a):
    return INVARIANTS[a["id"]]()
