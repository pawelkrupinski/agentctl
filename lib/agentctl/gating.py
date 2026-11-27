# agentctl, part `gating`: the gate's workflows: the gating-checks registry (.github/gating-checks.tsv, else the API) and the workflow files it names.
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import os
import re
from contextlib import suppress
from typing import Any, NamedTuple
from agentctl.statefile import note_failure, read_record, write_json
from agentctl.clock import epoch_now
from agentctl.timefmt import ts
from agentctl.core import git, process_cache, remote
from agentctl.repos import scan_repo_for
from agentctl.workflows import workflow_triggers
from agentctl.forge import gh_json
from agentctl.paths import state_root


# ---------------------------------------------------------------- ci-why
# THE GATE IS movies' GATING CHECKS REGISTRY (issue 20261030T095941-7267ea; the owner, 2026-11-19). Which workflows gate
# a push -- THE GATE (the owner, 2026-10-27: "if the latest main and convergence runs are green, just push then"; since
# 2026-11-08 also Identity takes) -- and which of their jobs a red counts for is ONE file on movies' main,
# GATING_CHECKS_FILE (scripts/ci/gating_checks.py), the file movies' pre-push red gate reads too. agentctl reads it from
# origin/main as it reads red-workflows.tsv, and keeps no list of its own. Its `workflow` rows are the gate workflows;
# a `job` row is a run number (proven: its first green run), `aggregate` (fails only because a job it waits on did) or
# `unproven` (a new check: it REPORTS until its first green run). A gate run red only in unproven jobs and the aggregates
# following them -- or any red of an unproven gate workflow -- is REPORT ONLY: it holds no push, it is no gate red (its
# reds are outside the gate's), and ci-why says so. Only movies (GATED_REPOS) puts a red gate in front of its landings.
GATING_CHECKS_FILE = ".github/gating-checks.tsv"  # kind, workflow (file), name, proven (#run | aggregate | unproven), note


_gating_rows = process_cache({})      # (repo, main sha) -> rows


class GateRegistryUnread(RuntimeError):
    """The gating checks registry could not be read and none was read before: the gate is unread (never green)."""


def gating_checks_parse(text):
    """The registry's rows [{kind, workflow, name, proven, note}] (gating_checks.py load's, comments and header out)."""
    rows = []
    for line in (text or "").splitlines():
        if not line.strip() or line.startswith("#") or line.startswith("kind\t"):
            continue
        cols = line.split("\t")
        if len(cols) < 4:
            continue
        rows.append({"kind": cols[0], "workflow": cols[1], "name": cols[2], "proven": cols[3].strip(),
                     "note": cols[4] if len(cols) > 4 else ""})
    return rows


def gating_checks_path():
    return state_root("gating-checks.json")


GATING_API_TTL = 300  # s a registry (or workflow names) read through GitHub's API, with no clone here, is kept


class GatingRegistryRead(NamedTuple):
    """One read of the registry: its rows (none when unread), the main it was read at, why it was not read, and whether
    the process's own cache answered it (nothing to keep then)."""
    rows: list[Any]
    main: str | None
    why: str
    cached: bool = False


def read_registry_clone(repo: Any) -> GatingRegistryRead:
    """The registry at origin/main of the clone `repo` (cached per main sha)."""
    main = git(repo, "rev-parse", f"refs/remotes/{remote()}/main", check=False).stdout.strip()
    if main and (repo, main) in _gating_rows:
        return GatingRegistryRead(_gating_rows[(repo, main)], main, "", True)
    r = git(repo, "show", f"{main}:{GATING_CHECKS_FILE}", check=False) if main else None
    rows = gating_checks_parse(r.stdout) if r is not None and r.returncode == 0 else []
    if rows:
        _gating_rows[(repo, main)] = rows
    return GatingRegistryRead(rows, main, f"{GATING_CHECKS_FILE} unread at {remote()}/main {(main or '?')[:10]}")


def read_registry_api(o_r: Any) -> GatingRegistryRead:
    """The registry through GitHub's contents API (kept GATING_API_TTL), for a repo with no clone here."""
    hit = _gating_rows.get(("api", o_r))
    if hit and epoch_now() - hit[0] < GATING_API_TTL:
        return GatingRegistryRead(hit[1], None, "", True)
    rows, main, why = [], None, "no clone of it here"
    try:
        import base64
        c = gh_json(o_r, "api", f"repos/{o_r}/contents/{GATING_CHECKS_FILE}?ref=main")
        rows = gating_checks_parse(base64.b64decode(c.get("content") or "").decode("utf-8", "replace"))
        main = "api"
    except (Exception, SystemExit) as e:  # SystemExit: NoToken
        why = f"no clone of it here, and {GATING_CHECKS_FILE} unread through the API ({str(e)[:120]})"
    if rows:
        _gating_rows[("api", o_r)] = (epoch_now(), rows)
    return GatingRegistryRead(rows, main, why)


def keep_registry(o_r: Any, rows: Any, main: Any) -> None:
    """The rows last read, kept in the state root (when they changed): a stale registry is an earlier main's, never none."""
    with suppress(OSError):
        kept = read_record(gating_checks_path()) or {}
        if kept.get(o_r, {}).get("rows") != rows:
            kept[o_r] = {**kept.get(o_r, {}), "main": main, "rows": rows, "at": ts()}
            write_json(gating_checks_path(), kept)


def gating_checks(o_r):
    """The registry's rows at main: from the repo's clone at origin/main (cached per main sha), else -- no clone here --
    through GitHub's contents API (kept GATING_API_TTL). Unreadable now (no file, a git or API error): the rows last read
    (kept in the state root: a stale registry is an earlier main's, never none); never read: raises GateRegistryUnread."""
    repo = scan_repo_for(o_r)
    read = read_registry_clone(repo) if repo and os.path.isdir(repo) else read_registry_api(o_r)
    if read.cached:
        return read.rows
    if read.rows:
        keep_registry(o_r, read.rows, read.main)
        return read.rows
    kept = ((read_record(gating_checks_path()) or {}).get(o_r) or {}).get("rows")
    if kept:
        return kept
    raise GateRegistryUnread(f"{o_r}'s gating checks registry: {read.why}")


def api_workflow_names(o_r: Any) -> Any:
    """{workflow file: name} of GitHub's workflows list (kept GATING_API_TTL; {} when unreadable)."""
    hit = _gating_rows.get(("names", o_r))
    if not (hit and epoch_now() - hit[0] < GATING_API_TTL):
        try:
            flows = gh_json(o_r, "api", f"repos/{o_r}/actions/workflows").get("workflows") or []
            api = {os.path.basename(str(w.get("path") or "")): w.get("name") for w in flows if w.get("name")}
        except (Exception, SystemExit):
            api = {}
        hit = (epoch_now(), api)
        if api:
            _gating_rows[("names", o_r)] = hit
    return hit[1]


def gate_flow_names(o_r, files = ()):
    """{workflow file: name} for files (the registry's): main's own workflow files in the clone (main_workflow_files);
    those it does not name, GitHub's workflows list (api_workflow_names); still unnamed, the names last read (kept
    beside the registry's rows)."""
    names = {f: n for n, f in main_workflow_files(o_r).items()}
    if any(f not in names for f in files):
        names = {**api_workflow_names(o_r), **names}
    names = {f: names[f] for f in files if names.get(f)}
    return _kept_names(o_r, files, names)


def _kept_names(o_r, files, names):
    """`names` with the names last read for the rest of files, the registry's record updated when a name moved."""
    try:
        kept = read_record(gating_checks_path()) or {}
        old = (kept.get(o_r) or {}).get("names") or {}
        if any(old.get(f) != n for f, n in names.items()):
            kept.setdefault(o_r, {})["names"] = {**old, **names}
            write_json(gating_checks_path(), kept)
        return {**{f: old[f] for f in files if old.get(f)}, **names}
    except OSError:
        return names


def gate_flows(o_r):
    """[(name, file)] of a gated repo's gate workflows: the registry's `workflow` rows, named as main names their files
    (gate_flow_names; the file itself when nothing names it)."""
    files = [r["workflow"] for r in gating_checks(o_r) if r["kind"] == "workflow" and r["proven"] != "aggregate"]
    names = gate_flow_names(o_r, files)
    return [(names.get(f) or f, f) for f in files]


_flow_files = process_cache({})   # main sha -> {workflow name: file}


_flow_meta = process_cache({})    # main sha -> {"callable": {name whose only trigger is workflow_call}, "paths": {name: path},


def note_workflow_file(txt: Any, path: Any, out: Any, meta: Any) -> None:
    """One workflow file's name -> file, path, whether only workflow_call starts it, and the workflows it calls."""
    m = re.search(r"^name:\s*[\"']?(.+?)[\"']?\s*$", txt, re.M)
    name = m.group(1) if m else os.path.basename(path)
    out[name] = os.path.basename(path)
    meta["paths"][name] = path
    if workflow_triggers(txt) == {"workflow_call"}:
        meta["callable"].add(name)
    for called in re.findall(r"^\s*(?:-\s*)?uses:\s*\./\.github/workflows/([\w.-]+)\s*$", txt, re.M):
        meta["callers"].setdefault(called, []).append(name)


def main_workflow_files(o_r):
    """{workflow name: file} of the workflow files on origin/main (cached per main sha); {} when unreadable."""
    repo = scan_repo_for(o_r)
    if not repo or not os.path.isdir(repo):
        return {}
    main = git(repo, "rev-parse", f"refs/remotes/{remote()}/main", check=False).stdout.strip()
    if not main:
        return {}
    if main not in _flow_files:
        out, meta = {}, {"callable": set(), "paths": {}, "callers": {}}
        for path in git(repo, "ls-tree", "--name-only", main, ".github/workflows/", check=False).stdout.split():
            if path.endswith((".yml", ".yaml")):
                note_workflow_file(git(repo, "show", f"{main}:{path}", check=False).stdout, path, out, meta)
        _flow_files.clear()
        _flow_files[main] = out
        _flow_meta.clear()
        _flow_meta[main] = meta
    return _flow_files[main]


def gate_dirty_path():
    return state_root("gate.dirty")


def mark_gate_dirty() -> None:
    """A push moved main: the processor re-reads the gate at its next tick (<= TICK s)."""
    try:
        os.makedirs(os.path.dirname(gate_dirty_path()), exist_ok=True)
        with open(gate_dirty_path(), "a"):
            pass
        os.utime(gate_dirty_path(), None)
    except OSError as e:
        note_failure("gate-dirty marker not written", e)
