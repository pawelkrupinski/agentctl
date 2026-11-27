# agentctl, part `percentile`: the q-quantile of a list of values
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from typing import Any


def pct(vals: Any, q: float) -> Any:
    vals = sorted(vals)
    if not vals:
        return None
    return vals[min(len(vals) - 1, int(round(q * (len(vals) - 1))))]
