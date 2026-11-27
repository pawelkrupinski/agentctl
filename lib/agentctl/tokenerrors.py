# agentctl, part `tokenerrors`: the vault's refusals: NoToken, and NoReadAccess for a repo the reader App cannot read
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.



class NoToken(SystemExit):
    """The vault would not give the token. A process whose launching session ended (a detached watch once land
    exits, a launchd service) is never authorized: `secrets` anchors an unlock to the session's processes (2026-10-27:
    two detached watches died on it with no verdict, and the waiting processor read the gate as green)."""


# THE READ-ONLY RED READER (2026-10-30). On ci-runner the reconciler lands movies as the lander App, which can read no
# other repository; another repo's runs (infra, core: the red tick) are read there with the agentctl-reader App's
# installation token (ghapp.py), never the lander's.
class NoReadAccess(NoToken):
    """ci-runner has no read credential for a repo other than movies (the agentctl-reader App is not configured)."""
