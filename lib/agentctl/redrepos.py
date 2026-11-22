# agentctl, part `redrepos`: a watched repo's red config read for a red: another repo's or the primary's, its gate workflows, owner-only pushes, its run directories and keys
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from agentctl.repos import RED_PRIMARY, gated_repo, red_repo_conf, red_repo_short
from agentctl.gating import gate_flows


def red_is_other(o_r):
    """True for a watched repo other than the primary (red-repos.json). Any other name -- the primary, or a repo nobody
    configured (the tests' `o/r`) -- is treated as the primary, exactly as before 2026-10-30."""
    return o_r != RED_PRIMARY and red_repo_conf(o_r) is not None


def red_gate_flows(o_r):
    """[(name, file)] of a watched repo's gate workflows: the registry's for a gated repo (gate_flows), its config's for
    the others."""
    if gated_repo(o_r):
        return gate_flows(o_r)
    c = red_repo_conf(o_r)
    return list(c["gate"]) if c else []


def red_owner_only(o_r):
    """True for a repo whose fixer never pushes (red-repos.json `"push": "owner-only"`)."""
    c = red_repo_conf(o_r)
    return bool(c and c["push"] == "owner-only")


def red_repo_tag(o_r, num):
    """A red run's directory name under reds/: the run number for the primary (as always), `<repo>-<number>` for the
    others -- run numbers are per repo, so infra #3455 and movies #3455 are two runs."""
    return f"{red_repo_short(o_r)}-{num}" if red_is_other(o_r) else str(num)


def red_repo_key(o_r, key):
    """A red's key in the store: as it always was for the primary; for another repo, its key bound to the repo, so the
    same workflow name and line in two repos are two reds."""
    if not red_is_other(o_r):
        return key
    import hashlib
    return hashlib.sha1(f"{o_r}\0{key}".encode()).hexdigest()[:12]
