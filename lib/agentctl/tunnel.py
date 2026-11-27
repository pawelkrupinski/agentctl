# agentctl, part `tunnel`: the store's ssh tunnel: its ports, its state file, a probe of it, the default route it rides
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import re
import subprocess
import urllib.error
import urllib.request
from agentctl.procrunner import process_runner
from agentctl.storeknobs import knob_tunnel_state
from agentctl.remotestore import default_store_url, store_url
from agentctl.paths import state_root


# ---------------------------------------------------------------- env
TUNNEL_PORTS = [(3014, "agentctl store")]  # what the laptop tunnel carries


def tunnel_ports() -> list[tuple[int, str]]:
    """The ports the laptop's tunnel must hold: the agentctl store's."""
    return TUNNEL_PORTS


# ---- THE TUNNEL HEALTH (2026-10-27: the tunnel's ssh was alive but its connections hung; only a *refused* connection
# was kicked). The always-on processor probes GET <store>/ each cycle (5 s): 2 failures in a row kick it (at most once a minute, logged), and a
# kick that does not restore it within 2 min is ONE STALLED inbox entry.
TUNNEL_PROBE_TIMEOUT = 5


def tunnel_state_path():
    return knob_tunnel_state() or state_root("tunnel-health.json")


def tunnel_probe():
    """'ok' (any HTTP answer), 'timeout' or 'refused' for GET <store>/ (the tunnel carries the agentctl store)."""
    url = (store_url() or default_store_url()).rstrip("/") + "/"
    try:
        with urllib.request.urlopen(url, timeout=TUNNEL_PROBE_TIMEOUT) as resp:
            resp.read()
        return "ok"
    except urllib.error.HTTPError:
        return "ok"
    except Exception as e:
        return "timeout" if probe_timed_out(e) else "refused"


def probe_timed_out(e: BaseException) -> bool:
    """Whether a failed probe's error (the URLError's reason, when it wraps one) is a timeout."""
    import socket
    if isinstance(e, urllib.error.URLError) and isinstance(e.reason, BaseException):
        e = e.reason
    return isinstance(e, (socket.timeout, TimeoutError)) or "timed out" in str(e)


def default_route():
    """(interface, gateway) of the default route, or None (offline / route failed)."""
    try:
        out = process_runner().run(["route", "-n", "get", "default"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    f = dict(re.findall(r"^\s*(interface|gateway): (\S+)", out, re.M))
    return (f.get("interface"), f.get("gateway")) if f else None
