#!/bin/bash
# devtools/stats.sh: agentctl's complexity stats, START (27b12c3) against a commit (devtools/stats.py says how).
exec python3 "$(dirname "$0")/stats.py" "$@"
