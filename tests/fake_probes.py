"""Fake probes for the condition machine's tests (lib/agentctl/conditions.py, THE REGISTRY): a check is a probe_<kind>
function, whose name is its kind (conditions.probe_kind), so a fake is named for the kind it stands in for; the
registry a recheck or a close is handed is cli's (condition_registry), built when the test asks for it -- after its
fakes replaced the probe functions (A.probe_<kind> = fake) -- plus any kind the test makes up."""


def probe(kind, fn):
    """fn as the check of kind: named probe_<kind>."""
    fn.__name__ = f"probe_{kind}"
    return fn


def registry(A, clock=None, store=None, **extra):
    """cli's ConditionRegistry on clock and store (the wall's and the process's by default), with the made-up kinds of
    extra (kind=check) added."""
    reg = A.condition_registry(clock or A.Clock(), store if store is not None else A.CurrentStore(A.process_store))
    for kind, fn in extra.items():
        reg.checks[kind] = probe(kind, fn)
    return reg
