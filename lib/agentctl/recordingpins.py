# agentctl, part `recordingpins`: the pinned recording main's convergence replays: read from the release, kept in the store, its runs
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from agentctl.statefile import read_record, write_json
from agentctl.clock import epoch_now
from agentctl.redknobs import knob_pinned_recording
from agentctl.core import print_error, git, host_role, remote
from agentctl.forge import api, forge_release_asset
from agentctl.agewords import fmt_age
from agentctl.paths import reds_dir
import json
import os
import re
import urllib.parse


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


def stored_pins(st):
    """The pins record the store holds (PIN_META), or None."""
    try:
        v = st.meta_get(PIN_META)
        rec = json.loads(v) if v else None
    except (Exception, SystemExit) as e:
        print_error(f"pinned recording: the store's copy not read ({str(e).replace('agentctl: ', '')[:160]})")
        return None
    return rec if isinstance(rec, dict) and rec.get("pins") else None


def store_pins(rec, st):
    """The pins into the store (PIN_META) for the reconciler; the local cache marks them stored. Never fatal."""
    try:
        st.meta_set(PIN_META, json.dumps({k: rec[k] for k in ("at", "repo", "tag", "pins")}))
    except (Exception, SystemExit) as e:
        print_error(f"pinned recording: not stored ({str(e).replace('agentctl: ', '')[:160]})")
        return rec
    rec = {**rec, "stored": True}
    write_json(pinned_path(), rec)
    return rec


def refresh_pinned_recording(o_r, repo, st, ran_at, now_ = None, force: bool = False, role = None):
    """The convergence pins, never fatal. The reconciler (role server) copies the store's record into its cache and
    never reads release assets itself. The laptop (local, or the sync in remote-store mode) re-reads them from the
    release at most every PIN_REFRESH s (PIN_RETRY s after a failure) with the token it holds, and writes them into the
    store; pins read but not yet stored (the store did not answer) are stored at the next call. ran_at: the process's
    throttles (Caches.ran_at), "pins" when a re-read was last tried."""
    t = now_ if now_ is not None else epoch_now()
    rec = read_record(pinned_path()) or {}
    if (role or host_role()) == "server":
        return _server_pins(st, rec)
    if rec.get("pins") and not rec.get("stored"):
        rec = store_pins(rec, st)
    if not force and _pins_fresh(rec, t, ran_at.get("pins", 0.0)):
        return rec
    ran_at["pins"] = t
    try:
        return _reread_pins(o_r, repo, st, rec, t)
    except (Exception, SystemExit) as e:
        print_error(f"pinned recording: not re-read ({str(e)[:160]})")
    return rec


def _pins_fresh(rec, t, tried):
    """Whether the pins were read lately (PIN_REFRESH) or tried lately (PIN_RETRY: `tried`, when last tried)."""
    return t - float(rec.get("at") or 0) < PIN_REFRESH or t - tried < PIN_RETRY


def _server_pins(st, rec):
    """The reconciler's pins: the store's record copied into its cache when it differs."""
    got = stored_pins(st)
    if got and {k: got.get(k) for k in ("at", "tag", "pins")} != {k: rec.get(k) for k in ("at", "tag", "pins")}:
        rec = {**got, "stored": True}
        write_json(pinned_path(), rec)
    return rec


def _reread_pins(o_r, repo, st, rec, t):
    """The pins re-read from the release, written and stored; `rec` as it was when there is no release or no pin."""
    tag = pin_release_tag(repo)
    pins = read_pins(o_r, tag) if tag else None
    if not pins:
        return rec
    rec = {"at": t, "repo": o_r, "tag": tag, "pins": pins}
    write_json(pinned_path(), rec)
    return store_pins(rec, st)


def pins_status(rec = None, now_ = None):
    """`status`'s line for the pins: which, from which release, and how old (STALE past PIN_STALE)."""
    rec = read_record(pinned_path()) if rec is None else rec
    if not (rec or {}).get("pins"):
        return "pins: unread (the laptop's processor reads them from the release and stores them)"
    age = (now_ if now_ is not None else epoch_now()) - float(rec.get("at") or 0)
    runs = "/".join(sorted(set(rec["pins"].values()), key=lambda x: int(x) if x.isdigit() else 0))
    line = f"pins: {runs} ({rec.get('tag')}), read {fmt_age(age)} ago"
    if age > PIN_STALE:
        line += "  STALE: the laptop's processor re-reads them hourly (owner's token); until then these stand"
    return line


def pinned_runs():
    """The recordings main's convergence replays by default (one per pinned country, deduplicated), or []."""
    if knob_pinned_recording():
        return [knob_pinned_recording()]
    pins = (read_record(pinned_path()) or {}).get("pins") or {}
    return sorted(set(pins.values()), key=lambda x: int(x) if x.isdigit() else 0)


def pinned_recording():
    """main's pinned recording as a name: `573`, `528/573` when the countries' pins differ, or `the pinned pair
    (unread)`."""
    runs = pinned_runs()
    return "/".join(runs) if runs else "the pinned pair (unread)"
