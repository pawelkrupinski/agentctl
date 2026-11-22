# agentctl, part `sdnotify`: systemd's notify protocol: READY and the watchdog's keep-alive, when systemd started us
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import os
from agentctl.core import err


# set once systemd heard READY=1 (the clone's keep-alive then says WATCHDOG=1, not EXTEND_TIMEOUT_USEC)
SD_READY = [False]


def sd_notify(msg: str) -> bool:
    """systemd's notify protocol ($NOTIFY_SOCKET; '@' an abstract socket): READY=1, WATCHDOG=1. A no-op elsewhere."""
    path = os.environ.get("NOTIFY_SOCKET")
    if not path:
        return False
    import socket
    addr = "\0" + path[1:] if path.startswith("@") else path
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.connect(addr)
            s.sendall(msg.encode())
        return True
    except OSError as e:
        err(f"sd_notify {msg}: {e}")
        return False
