"""ONE TRANSIENT CLASSIFIER for a push (netretry.py PUSH_SIGNATURES): every network row network_failure reads a check
for, and the push's own transport and forge rows. 2026-11-22: queue.py kept its own TRANSIENT_PUSH regex beside
classify's table; it lacked `Network is unreachable`, `No route to host`, `TLS handshake timeout`, a DNS
`nodename nor servname`, `Temporary failure in name resolution` and the GitOps fetch, so a push failing that way was
judged the change's failure instead of being retried."""
import os, re, sys, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402

# queue.py's regex as it was (master 27b12c3): everything it called transient still is
OLD = re.compile(r"Connection (reset|refused|timed out|closed)|closed by remote host|Could not resolve host|ssh: connect to host|"
                 r"kex_exchange_identification|[Bb]roken pipe|early EOF|remote end hung up|RPC failed|"
                 r"unable to access|HTTP (500|502|503|504)|timed out|Operation timed out|tunnel|"
                 r"fatal error in commit_refs|\[remote rejected\][^\n]*\((Internal Server Error|internal error)\)", re.I)

WAS_TRANSIENT = [
    "fatal: unable to access 'https://github.com/o/r.git/': Connection reset by peer",
    "Connection refused", "Connection timed out", "Connection closed by 140.82.121.4 port 22",
    "ssh_exchange: closed by remote host", "Could not resolve host: github.com", "ssh: connect to host x port 22",
    "kex_exchange_identification: read: Connection reset", "error: Broken pipe", "fatal: early EOF",
    "fatal: the remote end hung up unexpectedly", "error: RPC failed; curl 92", "The requested URL returned error: HTTP 502",
    "operation timed out", "the tunnel is down", "remote: fatal error in commit_refs",
    "! [remote rejected] main -> main (Internal Server Error)",
]
NOW_TRANSIENT = ["connect: Network is unreachable", "No route to host", "net/http: TLS handshake timeout",
                 "ssh: Could not resolve hostname github.com: nodename nor servname provided",
                 "Temporary failure in name resolution", "fetch-gitops: github.com unreachable"]
NEVER = ["! [rejected] main -> main (fetch first)", "pre-push: REFUSED -- the suite fails on abc",
         "error: failed to push some refs", "[remote rejected] main -> main (protected branch hook declined)"]


class TransientPushTest(unittest.TestCase):
    def test_everything_the_old_regex_called_transient_still_is(self):
        for line in WAS_TRANSIENT:
            self.assertTrue(OLD.search(line), line)
            self.assertTrue(A.TRANSIENT_PUSH.search(line), line)

    def test_every_network_row_of_a_check_is_transient_for_a_push_too(self):
        for line in NOW_TRANSIENT:
            self.assertTrue(A.network_failure(line), line)
            self.assertTrue(A.TRANSIENT_PUSH.search(line), line)

    def test_a_refusal_of_the_change_is_never_transient(self):
        for line in NEVER:
            self.assertFalse(A.TRANSIENT_PUSH.search(line), line)


if __name__ == "__main__":
    unittest.main()
