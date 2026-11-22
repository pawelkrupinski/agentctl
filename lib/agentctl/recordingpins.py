# agentctl, part `recordingpins`: the pinned recording main's convergence replays: read from the release, kept in the store, its runs
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import json
import os
import re
import time
import urllib.parse
from agentctl.core import err, fmt_age, git, host_role, reds_dir, remote
from agentctl.statefile import read_record, write_json
from agentctl.store import open_store
from agentctl.forge import api, forge_release_asset


# What main's convergence replays when its `recording` input is empty: each country's pin, the first word of the
# `hermetic-<code>.txt` asset of the release convergence-setup.sh reads ($FIXTURE_RELEASE_TAG in the convergence
# workflow on origin/main). Read from the movies repo by the laptop's processor with the owner's token
# (refresh_pinned_recording; the local processor, or the sync in remote-store mode, which also writes them into the
# store as the meta record PIN_META), cached in pinned-recording.json; $AGENTCTL_PINNED_RECORDING overrides. The
# reconciler on ci-runner NEVER reads release assets (ci-lander is push-only: repo.code + repo.actions, no releases):
# it copies the store's record, and while the laptop is offline the last stored pins stand (`status` shows their age,
# STALE past PIN_STALE). Never hard-coded (it was "573", and goes stale when a recording is promoted).
CONVERGENCE_WORKFLOW = ".github/workflows/identity-model-convergence.yml"


PIN_REFRESH = 3600


PIN_RETRY = 300  # s after a failed read before the next try (never one error line per tick)


PIN_STALE = 24 * 3600


PIN_META = "pinned_recording"


def pinned_path():
    return os.path.join(os.path.dirname(reds_dir()), "pinned-recording.json")


def pin_release_tag(repo):
    """The release the convergence pins live in: FIXTURE_RELEASE_TAG as main's convergence workflow sets it."""
    r = git(repo, "show", f"refs/remotes/{remote()}/main:{CONVERGENCE_WORKFLOW}", check=False) if repo else None
    tags = re.findall(r"FIXTURE_RELEASE_TAG:\s*['\"]?([\w.-]+)", r.stdout if r and r.returncode == 0 else "")
    return tags[0] if tags else None


def read_pins(o_r, tag):
    """{country code: pinned recording run} from the release's hermetic-<code>.txt assets."""
    rel = api(o_r, "GET", f"releases/tags/{urllib.parse.quote(tag)}")
    pins = {}
    for a in (rel or {}).get("assets") or []:
        m = re.fullmatch(r"hermetic-(\w+)\.txt", a.get("name") or "")
        if not m:
            continue
        first = (forge_release_asset(o_r, tag, a["name"]).decode(errors="replace").splitlines() or [""])[0].split()
        if first and first[0].isdigit():
            pins[m.group(1)] = first[0]
    return pins


_pin_tried = [0.0]


def stored_pins(st=None):
    """The pins record the store holds (PIN_META), or None."""
    try:
        v = (st or open_store()).meta_get(PIN_META)
        rec = json.loads(v) if v else None
    except (Exception, SystemExit) as e:
        err(f"pinned recording: the store's copy not read ({str(e).replace('agentctl: ', '')[:160]})")
        return None
    return rec if isinstance(rec, dict) and rec.get("pins") else None


def store_pins(rec, st=None):
    """The pins into the store (PIN_META) for the reconciler; the local cache marks them stored. Never fatal."""
    try:
        (st or open_store()).meta_set(PIN_META, json.dumps({k: rec[k] for k in ("at", "repo", "tag", "pins")}))
    except (Exception, SystemExit) as e:
        err(f"pinned recording: not stored ({str(e).replace('agentctl: ', '')[:160]})")
        return rec
    rec = {**rec, "stored": True}
    write_json(pinned_path(), rec)
    return rec


def refresh_pinned_recording(o_r, repo, now_=None, force=False, role=None):
    """The convergence pins, never fatal. The reconciler (role server) copies the store's record into its cache and
    never reads release assets itself. The laptop (local, or the sync in remote-store mode) re-reads them from the
    release at most every PIN_REFRESH s (PIN_RETRY s after a failure) with the token it holds, and writes them into the
    store; pins read but not yet stored (the store did not answer) are stored at the next call."""
    t = now_ if now_ is not None else time.time()
    rec = read_record(pinned_path()) or {}
    if (role or host_role()) == "server":
        got = stored_pins()
        if got and {k: got.get(k) for k in ("at", "tag", "pins")} != {k: rec.get(k) for k in ("at", "tag", "pins")}:
            rec = {**got, "stored": True}
            write_json(pinned_path(), rec)
        return rec
    if rec.get("pins") and not rec.get("stored"):
        rec = store_pins(rec)
    if not force and (t - float(rec.get("at") or 0) < PIN_REFRESH or t - _pin_tried[0] < PIN_RETRY):
        return rec
    _pin_tried[0] = t
    try:
        tag = pin_release_tag(repo)
        if not tag:
            return rec
        pins = read_pins(o_r, tag)
        if pins:
            rec = {"at": t, "repo": o_r, "tag": tag, "pins": pins}
            write_json(pinned_path(), rec)
            rec = store_pins(rec)
    except (Exception, SystemExit) as e:
        err(f"pinned recording: not re-read ({str(e)[:160]})")
    return rec


def pins_status(rec=None, now_=None):
    """`status`'s line for the pins: which, from which release, and how old (STALE past PIN_STALE)."""
    rec = read_record(pinned_path()) if rec is None else rec
    if not (rec or {}).get("pins"):
        return "pins: unread (the laptop's processor reads them from the release and stores them)"
    age = (now_ if now_ is not None else time.time()) - float(rec.get("at") or 0)
    runs = "/".join(sorted(set(rec["pins"].values()), key=lambda x: int(x) if x.isdigit() else 0))
    line = f"pins: {runs} ({rec.get('tag')}), read {fmt_age(age)} ago"
    if age > PIN_STALE:
        line += "  STALE: the laptop's processor re-reads them hourly (owner's token); until then these stand"
    return line


def pinned_runs():
    """The recordings main's convergence replays by default (one per pinned country, deduplicated), or []."""
    if os.environ.get("AGENTCTL_PINNED_RECORDING"):
        return [os.environ["AGENTCTL_PINNED_RECORDING"]]
    pins = (read_record(pinned_path()) or {}).get("pins") or {}
    return sorted(set(pins.values()), key=lambda x: int(x) if x.isdigit() else 0)


def pinned_recording():
    """main's pinned recording as a name: `573`, `528/573` when the countries' pins differ, or `the pinned pair
    (unread)`."""
    runs = pinned_runs()
    return "/".join(runs) if runs else "the pinned pair (unread)"
