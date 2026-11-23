# agentctl, part `netwatch`: the network and tunnel watches: the default route, the uplink's loss, the tunnel's health, each outage a condition
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import os
import re
import subprocess
import time
from agentctl.procrunner import process_runner
from agentctl.core import err, host_role, run_cmd
from agentctl.statefile import read_json_file, write_json_file
from agentctl.store import StoreUnavailable
from agentctl.gating import mark_gate_dirty
from agentctl.conditions import open_condition, recovery_recheck_all, run_probe
from agentctl.cistate import state_read, state_update
from agentctl.tunnel import default_route, tunnel_ports, tunnel_state_path
from agentctl.recovery import recover_tunnel_reset


# The laptop tunnel's launchd agent and its log: names it got before Forgejo was switched off (infra's plist; it now
# carries only the agentctl store).
TUNNEL_LABEL = "net.bitcashier.forgejo-tunnel"


TUNNEL_LOG = "~/Library/Logs/forgejo-tunnel.log"


def has_tunnel():
    """The laptop reaches the agentctl store through its tunnel (a launchd agent); ci-runner directly, with nothing to kick."""
    return host_role() == "sync"


def launchctl_kick():
    """Kickstart the laptop's tunnel. Never on ci-runner (no launchctl there: it raised FileNotFoundError over
    the real error, after a pointless 15 s wait)."""
    if not has_tunnel():
        return
    r = run_cmd(["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{TUNNEL_LABEL}"], timeout=30)
    if r.returncode == 124:
        err(f"launchctl kickstart {TUNNEL_LABEL}: no answer in 30 s")


TUNNEL_KICK_MIN_GAP = 60


TUNNEL_RESTORE_WAIT = 120


# Packet loss to the internet at or above which the laptop's own uplink, not the tunnel, is the fault: ONE lost ping of
# the 10. 2026-11-17 (and 11-16): a phone hotspot losing 15-20% kept every request through the tunnel timing out,
# yet each sample under the old 20% bound kicked the tunnel (11 kicks, launchd runs=260): every kick dropped all
# in-flight requests and a fresh jump+ssh handshake over the same lossy link took longer than the next probe.
# Once a NETWORK outage is open it stays one until a sample is clean (UPLINK_LOSS_CLEAR): no kick on a dip.
UPLINK_LOSS_MAX = 0.1


UPLINK_LOSS_CLEAR = 0.0


def uplink_loss():
    """Packet loss (0..1) from the laptop to 1.1.1.1 over 10 pings; 1.0 when ping fails outright."""
    try:
        out = process_runner().run(["ping", "-c", "10", "-i", "0.2", "-t", "5", "1.1.1.1"], capture_output=True, text=True,
                             timeout=15).stdout
        m = re.search(r"([\d.]+)% packet loss", out)
        return float(m.group(1)) / 100 if m else 1.0
    except (OSError, subprocess.SubprocessError):
        return 1.0


_last_route = ["unseen"]  # the route at the last look; None while offline


def open_outage(kind, text, probe, on_recover, **kw):
    """open_condition for the laptop's own network or tunnel outage -- which is also the store's (ci-runner's, through
    the tunnel): its inbox entry and this host's state are written whether the store answers or not, so the watch's
    state moves on (one note per outage) instead of raising out of it every cycle. The status line says the rest
    (NETWORK, STORE UNREACHABLE)."""
    try:
        return open_condition(kind, text, probe, on_recover, **kw)
    except StoreUnavailable as e:
        err(f"{kind}: the store did not answer, the condition is not recorded there ({str(e)[:120]})")
        return None


def network_watch(route=None, kick=None):
    """React to the laptop's network changing (another Wi-Fi, a hotspot, back online): the tunnel's ssh rides the old
    route and would take ServerAlive's 30 s to notice, the gate line says UNREAD until the next read. A changed default
    route kickstarts the tunnel at once and marks the gate dirty. Returns 'changed', 'offline' or ''."""
    r = (route or default_route)()
    prev, _last_route[0] = _last_route[0], r
    if prev == "unseen" or r == prev:
        return ""
    if r is None:
        err(f"the network went away (was {prev[0]} via {prev[1]})")
        open_outage("NETWORK", f"NETWORK: offline (no default route; was {prev[0]} via {prev[1]})", run_probe("route"),
                       [("tunnel_reset", {})], key="network-offline", inbox=False)  # the kick: below, on the way back
        return "offline"
    err(f"the network {'is back' if prev is None else 'changed'} ({prev[0] if prev else '-'} via {prev[1] if prev else '-'} -> "
        f"{r[0]} via {r[1]}): "
        f"kickstarting {TUNNEL_LABEL}, the gate is read at once")
    (kick or launchctl_kick)()
    recover_tunnel_reset(None, {})
    try:
        recovery_recheck_all()  # a new route: every open condition is probed again now, its backoff reset
    except StoreUnavailable as e:  # the tunnel is not back yet: the next tick's probes run on their own schedule
        err(f"the store did not answer the recheck ({str(e)[:120]})")
    mark_gate_dirty()
    return "changed"


def tunnel_watch(now_=None, probe=None, kick=None, uplink=None):
    """One processor cycle of the health probe. Returns what it did: ok, down, kicked, restored, escalated, network, or
    ''. A tunnel that fails while the laptop's own uplink loses packets is the network's fault: no kickstart (it only
    churns the tunnel), no STALLED escalation, one NETWORK note per outage."""
    t = now_ if now_ is not None else time.time()
    res = run_probe("tunnel") if probe is None else None  # the registry's probe: the one that will clear what it opens
    r = (res.detail.rsplit(" ", 1)[-1] if res else probe())
    st = read_json_file(tunnel_state_path())
    if r == "ok":
        did = "restored" if st.get("pending_since") or st.get("network") else "ok"
        if did == "restored":
            err("the tunnel answers again: the gate is read at once")
            mark_gate_dirty()  # the status line leaves `gate UNREAD` at the next duties tick, not at the 45 s mark
        if st.get("fails") or st.get("pending_since") or st.get("network"):
            st.update(fails=0, pending_since=None, escalated=False, network=False)
            write_json_file(tunnel_state_path(), st)
        if (state_read().get("network") or {}).get("at"):
            state_update(network=None)
        return did
    st["fails"] = st.get("fails", 0) + 1
    loss = (uplink or (uplink_loss if probe is None else (lambda: 0.0)))()
    if loss >= UPLINK_LOSS_MAX or (st.get("network") and loss > UPLINK_LOSS_CLEAR):
        if not st.get("network"):
            msg = (f"NETWORK: the laptop's uplink loses {int(loss * 100)}% of packets to 1.1.1.1, so the tunnel (localhost:{tunnel_ports()[0][0]}) "
                   f"{r}s; not a tunnel fault, nothing to restart -- the gate reads come back with the network")
            err(msg)
            open_outage("NETWORK", msg, res or run_probe("tunnel"), [("tunnel_reset", {})], key="tunnel",
                           command="tunnel")
        st.update(network=True, pending_since=None, escalated=False)  # the kick/escalation clock starts on a good uplink
        write_json_file(tunnel_state_path(), st)
        state_update(network={"loss": loss, "at": t, "tunnel": r})  # the status line says NETWORK, not a bare UNREAD
        return "network"
    if (state_read().get("network") or {}).get("at"):
        state_update(network=None)  # the uplink is fine: a dead forward is the tunnel's (the kickstart below)
    did = "down"
    if st["fails"] >= 2 and t - st.get("last_kick", 0) >= TUNNEL_KICK_MIN_GAP:
        err(f"the tunnel {r} {st['fails']} probes in a row; kickstarting {TUNNEL_LABEL}")
        (kick or launchctl_kick)()
        st["last_kick"] = t
        st["pending_since"] = st.get("pending_since") or t
        did = "kicked"
    if st.get("pending_since") and t - st["pending_since"] >= TUNNEL_RESTORE_WAIT and not st.get("escalated"):
        msg = (f"STALLED: the tunnel (localhost:{tunnel_ports()[0][0]}) still {r} {int((t - st['pending_since']) // 60)} min after a "
               f"kickstart; run `launchctl kickstart -k gui/$(id -u)/{TUNNEL_LABEL}` and check {TUNNEL_LOG}")
        open_outage("STALLED", msg, res or run_probe("tunnel"), [("tunnel_reset", {})], key="tunnel-stalled",
                       command="tunnel", exit_code=2)
        st["escalated"] = True
        did = "escalated"
    write_json_file(tunnel_state_path(), st)
    return did
