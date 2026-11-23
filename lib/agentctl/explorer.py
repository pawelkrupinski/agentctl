
import os
import sys
import time
from agentctl.procrunner import process_runner
from agentctl.core import NO_SIGN, err, git, reds_dir, remote
from agentctl.forge import forge_release_asset
from agentctl.land import spawn_land
from agentctl.worktrees import fresh_worktree
from agentctl.fixerenv import fixer_env
from agentctl.statemachines import ARCHIVE, MACHINES, StateSpec


"""The movies flip explorer's filing (2026-11-04): the nightly explorer workflow publishes `new-flips.tsv` on the release
`identity-flip-explorer`; the laptop's sync (the owner's token reads releases; ci-lander gets a 403, as with the pins)
reads the newest asset at most hourly and, for each flip no row of origin/main's identity-flip-backlog.tsv names and no
item already files, files an "explorer-flip" item (a backlog item, never an owner escalation) and lands ONE commit
(scripts/ci/drift_explorer.py file: the seeds + the backlog rows) through the normal `agentctl land`. An item closes
itself once main's backlog names its listing. A missing or unreadable asset does nothing and is logged."""
import csv, io

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


def explorer_parse(raw):
    """The asset's flips: [{venue, rawTitle, country, seed, ...}]. Raises ValueError when it is not the explorer's tsv."""
    text = raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else str(raw)
    rows = list(csv.reader(io.StringIO(text), delimiter="\t", quoting=csv.QUOTE_NONE))
    rows = [r for r in rows if r and any(c.strip() for c in r)]
    if not rows or [c.strip() for c in rows[0][:4]] != ["venue", "rawTitle", "country", "seed"]:
        raise ValueError("no explorer header")
    head = [c.strip() for c in rows[0]]
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
    with open(asset, "w") as f:
        f.write("\t".join(EXPLORER_COLS) + "\n")
        for r in rows:
            f.write("\t".join(r.get(c, "") for c in EXPLORER_COLS) + "\n")
    try:
        p = process_runner().run([sys.executable, "scripts/ci/drift_explorer.py", "file", "--asset", asset], cwd=wt,
                           capture_output=True, text=True, timeout=300)
        if p.returncode != 0 or not p.stdout.strip():
            if p.returncode != 0:
                err(f"drift explorer: the filing script failed ({(p.stderr or p.stdout).strip()[:200]})")
            git(repo, "worktree", "remove", "--force", wt, check=False)
            return None
        git(wt, "add", EXPLORER_BACKLOG, EXPLORER_SEEDS, check=False)
        extra = [x for x in git(wt, "status", "--porcelain").splitlines() if x[3:] not in (EXPLORER_BACKLOG, EXPLORER_SEEDS)]
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
        try:
            os.unlink(asset)
        except OSError:
            pass


def explorer_tick(o_r, repo, st, now_=None, force=False, fetch=None, filer=None):
    """At most hourly. Returns what it said (a list of strings); never raises."""
    t = now_ if now_ is not None else time.time()
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
        out = []
        for it in st.items("explorer-flip", ["open"]):  # self-recovering: main's backlog names it now
            d = it["data"]
            if d.get("venue") and explorer_named(backlog, d):
                st.transition("explorer-flip", it["id"], "closed", it["version"])
        new, seen = [], set()
        for r in rows:
            iid = explorer_iid(r)
            if iid in seen or explorer_named(backlog, r) or st.item("explorer-flip", iid):
                continue
            seen.add(iid)
            new.append(r)
        if not new:
            return out
        wt = (filer or explorer_file)(o_r, repo, new, t)
        if not wt:
            return out + ["drift explorer: nothing to commit for the new flips"]
        for r in new:
            st.put_item("explorer-flip", explorer_iid(r), "open", dict(r, worktree=wt))
        return out + [f"drift explorer: filed {len(new)} flip(s), landing from {wt}"]
    except (Exception, SystemExit) as e:
        return [f"drift explorer: skipped ({str(e).replace('agentctl: ', '')[:200]})"]
