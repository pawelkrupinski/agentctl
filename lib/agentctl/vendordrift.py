# agentctl, part `vendordrift`: VENDOR DRIFT as a condition: the movies checkout moved ahead of agentctl's byte-equal vendored copies (devtools/vendor.py), routed by the escalation table to an agent that syncs them
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.
#
# 2026-11-27: three times in one day a deliberate movies change to a vendored file (scripts/design-rules/*,
# baseline_ratchet, trailers, ps_table, atomic) broke tests/test_vendored.py and refused EVERY agentctl push until someone
# synced it. The structural fix, in two halves: (a) the test blocks only a push that itself touches a copy or
# VENDORED.tsv (vendor.py drift_verdict), drift elsewhere is reported; (b) THIS: the laptop's sync loop asks
# `devtools/vendor.py drift` (at most every CHECK_EVERY s) and, while the movies checkout is ahead, holds ONE VENDOR_DRIFT
# condition (key `vendor-drift`). The escalation table (escalationtable.py) routes it to a Sonnet agent in ~/bin whose
# task is the sync; its probe -- the same drift check -- closes it once the synced copies are on ~/bin's checkout.
# Opened and closed by the existing condition, offer and lease machinery; it writes no inbox entry (the agent is the answer).

import os
from typing import Any, Callable

from agentctl.core import err, run_cmd
from agentctl.conditions import open_condition, run_probe
from agentctl.escalationtable import VENDOR_DRIFT_KIND

CHECK_EVERY = 300        # s between the opening look's runs of the drift check (the probe closing it runs on PROBE_EVERY)
VENDOR_DRIFT_KEY = "vendor-drift"
MARK = "vendor_drift:checked"


def probe_vendor_drift(a: dict[str, Any]) -> tuple[bool | None, str]:
    """No vendored copy has drifted from the movies checkout (`devtools/vendor.py drift` in the agentctl checkout a["root"]):
    True when none has, False naming the drifted files, None when the check itself cannot run."""
    root = a["root"]
    r = run_cmd(["python3", os.path.join(root, "devtools", "vendor.py"), "drift"], timeout=60, cwd=root)
    if r.returncode not in (0, 1):
        return None, f"vendor.py drift: exit {r.returncode}: {r.stderr.strip()[-120:]}"
    return r.returncode == 0, "; ".join(x.strip() for x in r.stdout.splitlines() if x.strip())[:280]


def vendor_drift_step(store: Any, check: Callable[[dict[str, Any]], tuple[bool | None, str]], clock: Callable[[], float], root: str) -> None:
    """The sync loop's look, at most every CHECK_EVERY s: while a vendored copy trails the movies checkout the VENDOR_DRIFT
    condition is open (once; refreshed while it stays so), its line said. check: the registry's vendor_drift probe; root: agentctl's checkout."""
    now = clock()
    if now - float(store.meta_get(MARK) or 0) < CHECK_EVERY:
        return
    store.meta_set(MARK, str(now))
    probe = run_probe(check, {"root": root})
    if probe.ok is not False:
        return
    text = (f"VENDOR DRIFT: the movies checkout changed a file agentctl vendors, so tests/test_vendored.py fails for the next "
            f"push that touches one: {probe.detail}")
    c = open_condition(VENDOR_DRIFT_KIND, text, probe, [], key=VENDOR_DRIFT_KEY, inbox=False, store=store, grace=0)
    if c:
        err(text)
