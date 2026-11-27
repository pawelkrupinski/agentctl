
import os
import sys
from contextlib import suppress
from agentctl.procrunner import process_runner
from agentctl.statemachines import ARCHIVE, MACHINES, StateSpec
from agentctl.clock import epoch_now
from agentctl.core import NO_SIGN, err, git, remote
from agentctl.paths import reds_dir
from agentctl.forge import forge_release_asset
from agentctl.worktreelife import fresh_worktree
from agentctl.fixerenv import fixer_env
from agentctl.land import spawn_land


"""The movies flip explorer's filing (2026-11-04): the nightly explorer workflow publishes `new-flips.tsv` on the release
`identity-flip-explorer`; the laptop's sync (the owner's token reads releases; ci-lander gets a 403, as with the pins)
reads the newest asset at most hourly and, for each flip no row of origin/main's identity-flip-backlog.tsv names and no
item already files, files an "explorer-flip" item (a backlog item, never an owner escalation) and lands ONE commit
(scripts/ci/drift_explorer.py file: the seeds + the backlog rows) through the normal `agentctl land`. An item closes
itself once main's backlog names its listing. A missing or unreadable asset does nothing and is logged."""
import csv, io
from typing import Any

EXPLORER_TAG = "identity-flip-explorer"
EXPLORER_ASSET = "new-flips.tsv"
EXPLORER_BACKLOG = "test/resources/fixtures/identity-flip-backlog.tsv"
EXPLORER_SEEDS = "test/resources/fixtures/drift-gate-seeds.tsv"
EXPLORER_EVERY = 3600
EXPLORER_COLS = ("venue", "rawTitle", "country", "seed", "firstSeen", "slice", "takes")

MACHINES["explorer-flip"] = {  # open until main's backlog names the flip; the deadline only retires a forgotten one
    "open": StateSpec(ARCHIVE, "closed", ("closed",)),
    "closed": StateSpec(ARCHIVE, "closed", (), terminal=True),
}


def _has_cells(row: Any) -> Any:
    """Whether a tsv row holds any non-blank cell."""
    return bool(row and any(c.strip() for c in row))


def _write_flips_asset(path: Any, rows: Any) -> Any:
    """The new flips as the explorer's tsv (EXPLORER_COLS) at path, for the filing script."""
    with open(path, "w") as f:
        f.write("\t".join(EXPLORER_COLS) + "\n")
        for r in rows:
            f.write("\t".join(r.get(c, "") for c in EXPLORER_COLS) + "\n")


def _stray_changes(wt: Any) -> Any:
    """The worktree's changed paths other than the backlog and the seeds the filing script may touch."""
    return [x for x in git(wt, "status", "--porcelain").splitlines() if x[3:] not in (EXPLORER_BACKLOG, EXPLORER_SEEDS)]


def _explorer_header(rows: Any) -> Any:
    """The explorer tsv's column names (its first row); ValueError when it does not start venue, rawTitle, country, seed."""
    head = [c.strip() for c in rows[0]] if rows else []
    if head[:4] != ["venue", "rawTitle", "country", "seed"]:
        raise ValueError("no explorer header")
    return head


def explorer_parse(raw):
    """The asset's flips: [{venue, rawTitle, country, seed, ...}]. Raises ValueError when it is not the explorer's tsv."""
    text = raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else str(raw)
    rows = list(csv.reader(io.StringIO(text), delimiter="\t", quoting=csv.QUOTE_NONE))
    rows = [r for r in rows if _has_cells(r)]
    head = _explorer_header(rows)
    return [dict(zip(head, [c.strip() for c in r])) for r in rows[1:] if len(r) >= 4 and r[0].strip()]


def explorer_iid(row):
    return f"{row['country']}:{row['venue']}|{row['rawTitle']}"


def explorer_named(backlog_text, row):
    """Some backlog row names the listing (its country, venue and title all on one line)."""
    return any(row["venue"] in ln and row["rawTitle"] in ln and row["country"] in ln
               for ln in backlog_text.splitlines())


def explorer_backlog(repo):
    r = git(repo, "show", f"{remote()}/main:{EXPLORER_BACKLOG}", check=False)
    return r.stdout if r.returncode == 0 else ""


def explorer_file(o_r, repo, rows, now_):
    """ONE commit for the new rows in a fresh worktree off main, landed by a detached `agentctl land`. Returns the
    worktree, or None when the script printed nothing (no commit) or the worktree/commit failed (logged)."""
    wt, why = fresh_worktree(repo, "explorer", int(now_))
    if not wt:
        err(f"drift explorer: {why}")
        return None
    asset = wt + ".new-flips.tsv"
    seeds = sorted({r["seed"] for r in rows}, key=lambda s: (len(s), s))
    _write_flips_asset(asset, rows)
    try:
        p = process_runner().run([sys.executable, "scripts/ci/drift_explorer.py", "file", "--asset", asset], cwd=wt,
                           capture_output=True, text=True, timeout=300)
        if p.returncode != 0 or not p.stdout.strip():
            if p.returncode != 0:
                err(f"drift explorer: the filing script failed ({(p.stderr or p.stdout).strip()[:200]})")
            git(repo, "worktree", "remove", "--force", wt, check=False)
            return None
        git(wt, "add", EXPLORER_BACKLOG, EXPLORER_SEEDS, check=False)
        extra = _stray_changes(wt)
        if extra:
            err(f"drift explorer: the script changed more than the backlog and the seeds ({extra[:3]})")
            git(repo, "worktree", "remove", "--force", wt, check=False)
            return None
        n = len(rows)
        msg = (f"Drift explorer: file {n} flip(s) found on seeds {', '.join(seeds)}\n\n" + "\n".join(
            f"- {r['country']} {r['venue']} | {r['rawTitle']} (seed {r['seed']}, slice {r.get('slice', '')})" for r in rows)
            + "\n\nCo-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>\n")
        c = git(wt, *NO_SIGN, "commit", "-q", "-m", msg, check=False)
        if c.returncode != 0:
            err(f"drift explorer: commit failed ({c.stderr.strip()[:160]})")
            return None
        spawn_land(wt, fixer_env(o_r, "explorer"), reds_dir(f"explorer-{int(now_)}.log"))
        return wt
    finally:
        with suppress(OSError):
            os.unlink(asset)


def _explorer_close_named(st: Any, backlog: str) -> None:
    """Self-recovering: an open flip item whose listing main's backlog names now is closed."""
    for it in st.items("explorer-flip", ["open"]):
        d = it["data"]
        if d.get("venue") and explorer_named(backlog, d):
            st.transition("explorer-flip", it["id"], "closed", it["version"], event={"cause": "probe"})


def _explorer_new_flips(st: Any, rows: Any, backlog: str) -> list[Any]:
    """The asset's flips no backlog row names and no item files yet, each listing once."""
    new, seen = [], set()
    for r in rows:
        iid = explorer_iid(r)
        if iid in seen or explorer_named(backlog, r) or st.item("explorer-flip", iid):
            continue
        seen.add(iid)
        new.append(r)
    return new


def _explorer_filed(o_r: Any, repo: Any, st: Any, new: list[Any], t: Any, filer: Any) -> list[str]:
    """File the new flips in one commit and open an item for each; the line saying what came of it."""
    wt = (filer or explorer_file)(o_r, repo, new, t)
    if not wt:
        return ["drift explorer: nothing to commit for the new flips"]
    for r in new:
        st.put_item("explorer-flip", explorer_iid(r), "open", dict(r, worktree=wt))
    return [f"drift explorer: filed {len(new)} flip(s), landing from {wt}"]


def explorer_tick(o_r, repo, st, now_=None, force=False, fetch=None, filer=None):
    """At most hourly. Returns what it said (a list of strings); never raises."""
    t = now_ if now_ is not None else epoch_now()
    try:
        if not force and t - float(st.meta_get("explorer_at", 0) or 0) < EXPLORER_EVERY:
            return []
        st.meta_set("explorer_at", t)
        try:
            rows = explorer_parse((fetch or forge_release_asset)(o_r, EXPLORER_TAG, EXPLORER_ASSET))
        except (Exception, SystemExit) as e:
            return [f"drift explorer: asset not read ({str(e)[:160]}); nothing filed"]
        git(repo, "fetch", "--quiet", remote(), "main", check=False)
        backlog = explorer_backlog(repo)
        _explorer_close_named(st, backlog)
        new = _explorer_new_flips(st, rows, backlog)
        return _explorer_filed(o_r, repo, st, new, t, filer) if new else []
    except (Exception, SystemExit) as e:
        return [f"drift explorer: skipped ({str(e).replace('agentctl: ', '')[:200]})"]
