#!/bin/bash
# devtools/suite-at.sh <repo> <rev> <out dir> [cov]: agentctl's full suite on a SNAPSHOT of <rev> (git archive), so
# edits to the worktree meanwhile never touch the run. The snapshot is <out>/snap, the log <out>/suite.log, its last
# line `exit N`; cov: branch coverage (the quality venv's coverage) into <out>/cov, its TOTAL line in the log.
# $SUITE_CMD replaces the command run in the snapshot (default: `~/bin/agentctl suite`, a governor ticket; with cov,
# the coverage venv's python on tests/run.py, which takes its own ticket: only an interpreter with coverage's .pth
# hook starts coverage in the processes the tests spawn, and they spawn sys.executable). $COVERAGE_VENV: that venv's
# bin/ (default ~/.cache/agentctl-quality/venv/bin).
set -uo pipefail
repo=$1; rev=$2; out=$3; cov=${4:-}
snap=$out/snap; log=$out/suite.log
rm -rf "$snap"; mkdir -p "$snap"
git -C "$repo" archive "$rev" | tar -x -C "$snap" || { echo "exit 2" > "$log"; exit 2; }
unset AGENTCTL_RED_FIXER FIXER_RECORD AGENT_NAME
cd "$snap" || exit 2
if [ -n "$cov" ]; then
  venv=${COVERAGE_VENV:-$HOME/.cache/agentctl-quality/venv/bin}
  if ! "$venv/python" -c 'import coverage' 2>/dev/null; then
    printf 'no coverage in %s/python\nexit 2\n' "$venv" > "$log"; exit 2
  fi
  rm -rf "$out/cov"; mkdir -p "$out/cov"
  printf '[run]\nbranch = True\nparallel = True\ndata_file = %s/cov/.coverage\nsource = %s/lib/agentctl\n[report]\nprecision = 1\n' \
    "$out" "$snap" > "$out/covrc"
  COVERAGE_PROCESS_START=$out/covrc ${SUITE_CMD:-"$venv/python" tests/run.py} > "$log" 2>&1
  rc=$?
  ( cd "$out/cov" && "$venv/coverage" combine --rcfile "$out/covrc" >/dev/null 2>&1
    "$venv/coverage" report --rcfile "$out/covrc" | tail -1 ) >> "$log" 2>&1
else
  ${SUITE_CMD:-"$HOME/bin/agentctl" suite} > "$log" 2>&1
  rc=$?
fi
echo "exit $rc" >> "$log"
exit $rc
