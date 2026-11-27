# agentctl, part `conditionregistry`: the condition machine's registry and its vocabulary: the probe kinds and their recheck intervals, the recovery action names, a check's kind (its probe_<kind> name), and ConditionRegistry, the checks by kind and the actions by name that cli builds
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from typing import Any, Callable


Check = Callable[[dict[str, Any]], tuple[Any, str]]


RecoverAction = Callable[[dict[str, Any], dict[str, Any], Any], str]


RECOVER_ACTION_NAMES = ("clear_hold", "tunnel_reset", "clear_flag")  # an on_recover action named at open: one of these


def probe_kind(check: Any) -> str:
    """The kind a check is: its function's name past `probe_` (a functools.partial binding its store: the function's).
    THE CHECK THAT OPENS IS THE CHECK THAT CLOSES: the registry's check of a kind is the function of that name."""
    name = getattr(getattr(check, "func", check), "__name__", "")
    if not name.startswith("probe_"):
        raise ConditionBypass(f"{name or check!r} is no probe: a probe is a probe_<kind> function")
    return name[len("probe_"):]


class ConditionRegistry:
    """THE REGISTRY, which cli builds: every probe_<kind> check by kind (probe_kind; each PROBE_EVERY kind has one, the
    store and clock bound into those that read them) and every RECOVER_ACTION_NAMES action. A module opening a
    condition runs the check it imports (conditions.run_probe); what re-runs a stored probe by its kind, or closes a
    condition, is handed this: the condition machine reads no part above it, and no cell holds the checks."""

    def __init__(self, checks: list[Check], actions: dict[str, RecoverAction]) -> None:
        self.checks: dict[str, Check] = {probe_kind(c): c for c in checks}
        if set(PROBE_EVERY) - set(self.checks) or sorted(actions) != sorted(RECOVER_ACTION_NAMES):
            raise ConditionBypass(f"the registry lacks probes {sorted(set(PROBE_EVERY) - set(self.checks))} or has "
                                  f"actions {sorted(actions)} for {sorted(RECOVER_ACTION_NAMES)}")
        self.actions = actions

    def check(self, kind: str) -> Check:
        """The check of a stored probe's kind (run_probe runs it again)."""
        if kind not in self.checks:
            raise ConditionBypass(f"unknown probe {kind!r}: every condition needs a probe from the registry")
        return self.checks[kind]


PROBE_EVERY = {"disk_free": 300, "quota_reset": 300, "invariant": 60, "waiting_ok": 30, "orphan_landed": 300, "route": 5, "tunnel": 10, "gate_read": 15,
               "workflow_green": 60, "token": 60, "tcp": 60, "http": 120, "git_fetch": 120,
               "clone_health": 120, "infra_requeue": 60, "push_credential": 60, "change_source": 60, "red_newer_verdict": 60,
               "red_read": 300, "queue_moving": 60}


class ConditionBypass(RuntimeError):
    """A non-green condition written without open_condition (and so without a probe that can clear it): a bug."""
