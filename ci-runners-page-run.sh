#!/bin/sh
# launchd entry point for net.bitcashier.ci-runners-page (the live CI runners page, http://runners.localhost:8790).
# Supervises the server itself, as the events page's run-server.sh does: a crash restarts it here after a pause that
# doubles while it keeps dying within a minute (max 5 min), so a broken build cannot spin launchd in a tight loop.
cd "$(dirname "$0")" || exit 1
PY=/opt/homebrew/bin/python3
delay=5
while :; do
  started=$(date +%s)
  "$PY" /Users/pawel/bin/ci-runners-page --port 8790
  rc=$?
  if [ $(( $(date +%s) - started )) -ge 60 ]; then delay=5; fi
  echo "$(date '+%Y-%m-%dT%H:%M:%S') ci-runners-page exited ($rc); restarting in ${delay}s" >&2
  sleep "$delay"
  delay=$(( delay * 2 > 300 ? 300 : delay * 2 ))
done
