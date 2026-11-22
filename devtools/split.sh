#!/bin/bash
# devtools/split.sh <lib/agentctl> <source module> <spec file>: a split of one module by pure moves. Each spec line
# `<module>|<description or ->|<names...>` moves those top-level names out of the source into the module (a
# description: a new module, `-`: an existing one; movedefs --from), then gives every touched module its imports
# (mkmodule), repoints the importers (retarget), drops the imports nothing reads (unused --fix) and puts MODULES in
# import order (placemodules). Prints each touched module's lines. Prove it after with devtools/prove.sh.
set -euo pipefail
dev=$(cd "$(dirname "$0")" && pwd)
lib=$1; src=$2; spec=$3
mods=""
while IFS='|' read -r mod desc names; do
  [ -z "$mod" ] && continue
  if [ "$desc" = "-" ]; then
    python3 "$dev/movedefs.py" "$lib" "$mod" --from "$src" $names
  else
    python3 "$dev/movedefs.py" "$lib" "$mod" --from "$src" --new "$desc" $names
  fi
  mods="$mods $mod"
done < "$spec"
for m in $mods $src; do python3 "$dev/mkmodule.py" "$lib" "$m" >/dev/null || echo "mkmodule $m: $? (again after retarget)"; done
retargeted=$(python3 "$dev/retarget.py" "$lib" | sed -n 's/^retargeted //p')
for m in $mods $src; do python3 "$dev/mkmodule.py" "$lib" "$m" >/dev/null; done
for m in $(printf '%s\n' $mods $src $retargeted | sort -u); do python3 "$dev/unused.py" "$lib" --fix "$m" >/dev/null; done
python3 "$dev/placemodules.py" "$lib" >/dev/null
for m in $src $mods; do wc -l < "$lib/$m.py" | sed "s/^ */$m: /"; done
