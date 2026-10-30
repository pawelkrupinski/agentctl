# agentctl, part `upkeep`: upkeep.
# Runs inside agentctl's one namespace, after the parts before it in lib/agentctl/__init__.py's PARTS (it uses their names).

# ---------------------------------------------------------------- upkeep: the tick keeps the laptop's disk (2026-10-29)
# The owner's coordinator, 2026-10-29: the Data volume filled up (739 MB free) -- 109 proven-merged idle worktrees
# (mostly finished fixers' movies-wt-red-*) piled up because nothing ran `cleanup` on its own, and the test suite had
# leaked 3,009 `agentctl-*` temp dirs. Now, at most every UPKEEP_EVERY s (and at once when the disk runs low), the
# laptop's processor runs:
#   - `cleanup --execute --no-remote`, the STANDING-OK scope only (clean, idle, proven-merged worktrees and their
#     branches; every deletion re-verified and logged in agentctl-deleted.log);
#   - each ended fixer's worktree whose red is closed (no open red item with its key) closed by close_worktree (only
#     when proven merged, clean and unused);
#   - the test suite's own leftovers: `agentctl-*` dirs in the user's temp dir and `agentctl-sock-*` in /tmp older than
#     TEST_TMP_MAX_AGE (a run removes its own at exit; these are killed runs').
UPKEEP_EVERY = 3600
DISK_MIN_GB = 50  # the Data volume's free space below which the upkeep runs at once, then WD opens ($AGENTCTL_DISK_MIN_GB)
TEST_TMP_MAX_AGE = 3600
_last_upkeep = [0.0]


def disk_free_gb(path=None):
    import shutil
    return shutil.disk_usage(path or os.environ.get("AGENTCTL_DISK_PATH") or HOME).free / 1e9


def probe_disk_free(a):
    free = disk_free_gb(a.get("path"))
    return free >= float(a.get("min_gb") or DISK_MIN_GB), f"{free:.1f} GB free on {a.get('path') or HOME}"


def user_tmp_dir():
    try:
        return os.confstr("CS_DARWIN_USER_TEMP_DIR") or tempfile_dir()
    except (ValueError, OSError, AttributeError):
        return tempfile_dir()


def tempfile_dir():
    import tempfile
    return tempfile.gettempdir()


def sweep_test_tmp(now_=None):
    """The test suite's leftovers older than TEST_TMP_MAX_AGE: `agentctl-*` in the user's temp dir, `agentctl-sock-*` in
    /tmp (never the processor's own socket dir /tmp/agentctl-<uid>). Returns how many went."""
    import shutil
    t = now_ if now_ is not None else time.time()
    n = 0
    for pat in (os.path.join(user_tmp_dir(), "agentctl-*"), "/tmp/agentctl-sock-*"):
        for p in glob.glob(pat):
            if os.path.realpath(p) == os.path.realpath(os.path.dirname(token_sock_path("x"))):
                continue  # the processor's own socket dir (Linux: the user temp dir IS /tmp)
            try:
                if os.path.isdir(p) and not os.path.islink(p) and t - os.lstat(p).st_mtime > TEST_TMP_MAX_AGE:
                    shutil.rmtree(p, ignore_errors=True)
                    n += 1
            except OSError:
                pass
    return n


def close_finished_fixer_trees(store=None):
    """Each ended fixer's worktree whose red is closed: closed when proven merged, clean and unused (close_worktree
    keeps it otherwise and says why). Returns the lines."""
    st = store or Store()
    try:
        open_keys = {it["data"].get("key") for it in red_active(st).values()}
    except Exception:
        return []
    out = []
    for rec in fixer_records():
        wt = rec.get("worktree")
        if rec.get("alive") or not rec.get("ended") or rec.get("orphan") or not wt or not os.path.isdir(wt):
            continue
        if rec.get("key") in open_keys:
            continue
        out.append(close_worktree(wt))
    return out


def run_upkeep():
    """One upkeep pass (see above); its lines, also appended to the state root's upkeep.log."""
    lines = []
    try:  # its own process: cleanup prints, and the processor's stdout is never redirected under its other threads
        r = subprocess.run([sys.executable, TOOL_PATH, "cleanup", "--execute", "--no-remote"], capture_output=True,
                           text=True, timeout=1800, stdin=subprocess.DEVNULL)
        lines += [x.strip() for x in r.stdout.splitlines() if x.strip().startswith("delete")]
        if r.returncode not in (0, 1):
            lines.append(f"cleanup exit {r.returncode}: {(r.stderr or r.stdout).strip()[-200:]}")
    except (OSError, subprocess.TimeoutExpired) as e:
        lines.append(f"cleanup failed: {str(e)[:200]}")
    lines += close_finished_fixer_trees()
    n = sweep_test_tmp()
    if n:
        lines.append(f"removed {n} test temp dir(s) older than {TEST_TMP_MAX_AGE // 60} min")
    try:
        os.makedirs(state_root(), exist_ok=True)
        with open(state_root("upkeep.log"), "a") as f:
            for x in lines:
                f.write(f"{ts()} {x}\n")
    except OSError:
        pass
    return lines


def maybe_upkeep(now_=None):
    """The tick's upkeep step: at most every UPKEEP_EVERY s, or within 10 min when the disk is low (DISK_MIN_GB); and
    WD, the disk invariant: still low with the last upkeep done, an INVARIANT condition whose probe is that same
    free-space check (it closes itself once space is back; it escalates only while it stays open)."""
    t = now_ if now_ is not None else time.time()
    min_gb = env_num("AGENTCTL_DISK_MIN_GB", DISK_MIN_GB)
    pr = run_probe("disk_free", {"path": os.environ.get("AGENTCTL_DISK_PATH") or HOME, "min_gb": min_gb})
    low = pr.ok is False
    out = []
    if t - _last_upkeep[0] >= env_num("AGENTCTL_UPKEEP_EVERY", UPKEEP_EVERY) or (low and t - _last_upkeep[0] >= 600):
        _last_upkeep[0] = t
        # in its thread (minutes of git): the tick goes on; it logs to upkeep.log
        done = run_bounded("upkeep", run_upkeep, 1)
        out.append(f"{'done' if done else 'started'}{' (the disk is low: ' + pr.detail + ')' if low else ''} "
                   f"(log {state_root('upkeep.log')})")
        return out
    t_up = _bounded.get("upkeep")
    if low and not (t_up and t_up.is_alive()):  # the upkeep ran and the disk is still low: the owner's to free
        open_condition("INVARIANT", f"INVARIANT WD: the disk is low -- {pr.detail} (under {min_gb:.0f} GB) after the "
                                    "upkeep's cleanup: free space by hand (`du -sh ~/* ~/Library/* | sort -h | tail`)",
                       pr, [], key="disk-free")
        out.append(f"WD: {pr.detail}")
    return out
