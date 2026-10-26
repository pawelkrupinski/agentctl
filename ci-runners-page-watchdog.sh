#!/bin/sh
# Health check for net.bitcashier.ci-runners-page, run every 30 s by net.bitcashier.ci-runners-page-watchdog (the
# events page's watchdog, same shape). KeepAlive restarts a server that EXITS; this catches one that is alive but not
# answering, or whose poller stalled (/healthz is 503). An HTTP round-trip, not a port probe: the kernel accepts
# connections on a wedged server's socket. Two probes 5 s apart avoid acting during a restart.
LABEL=net.bitcashier.ci-runners-page
check() { /usr/bin/curl -fsS -o /dev/null --max-time 10 "http://127.0.0.1:8790/healthz" 2>/dev/null; }
if check; then exit 0; fi
sleep 5
if check; then exit 0; fi
# Kill just the server: the supervisor shell brings it back. Only with no supervisor at all, kickstart the job.
if /usr/bin/pkill -f "bin/ci-runners-page --port"; then
  echo "$(date '+%Y-%m-%dT%H:%M:%S') page not healthy -> restarted the server"
elif ! /usr/bin/pgrep -f "ci-runners-page-run.sh" >/dev/null; then
  echo "$(date '+%Y-%m-%dT%H:%M:%S') page not healthy, no supervisor -> kickstart ${LABEL}"
  /bin/launchctl kickstart -k "gui/$(/usr/bin/id -u)/${LABEL}"
fi
