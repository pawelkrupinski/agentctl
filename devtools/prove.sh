#!/bin/bash
# devtools/prove.sh <repo> [rev] [--no-ratchet]: a pure move's proofs, the worktree's lib against <rev>'s (HEAD):
# sameast (the same statements), samecode (the same bytecode), every module in MODULES imports, then the ratchets'
# check (quality/ratchet.py check; --no-ratchet skips it, for a repo without them). Exit 0 only when all hold; each
# failure is named (`SAMEAST FAILED`, ...).
set -uo pipefail
dev=$(cd "$(dirname "$0")" && pwd)
repo=$1; rev=HEAD; ratchet=1
for a in "${@:2}"; do
  if [ "$a" = "--no-ratchet" ]; then ratchet=0; else rev=$a; fi
done
old=$(mktemp -d "${TMPDIR:-/tmp}/prove-XXXXXX")
trap 'rm -rf "$old"' EXIT
git -C "$repo" archive "$rev" lib | tar -x -C "$old" || { echo "ARCHIVE FAILED"; exit 2; }
rc=0
python3 "$dev/sameast.py" "$old/lib/agentctl" "$repo/lib/agentctl" || { echo "SAMEAST FAILED"; rc=1; }
python3 "$dev/samecode.py" "$old/lib" "$repo/lib" || { echo "SAMECODE FAILED"; rc=1; }
python3 -c 'import importlib, sys; sys.path.insert(0, sys.argv[1]); import agentctl
for m in agentctl.MODULES: importlib.import_module("agentctl." + m)' "$repo/lib" || { echo "IMPORT FAILED"; rc=1; }
if [ "$ratchet" = 1 ]; then
  (cd "$repo" && "$(python3 quality/tools.py)" quality/ratchet.py check) || { echo "RATCHET FAILED"; rc=1; }
fi
exit $rc
