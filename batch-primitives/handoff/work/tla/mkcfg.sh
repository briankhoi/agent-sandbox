#!/bin/bash
set -euo pipefail
# usage: mkcfg.sh NAME key=value ... ; writes NAME.cfg from defaults plus overrides
name=$1; shift
declare -A c=(
  [D]=6
  [Grace]=6
  [G]=1
  [M]=2
  [SkewMax]=2
  [Lat]=1
  [MaxTime]=10
  [MaxInc]=2
  [MaxRounds]=2
  [Features]='{"attach","detach","release","crash","flaky"}'
  [Bugs]='{}'
  [Fixes]='{}'
)
inv="ReaperSafety ReaperSafetyConnected AtMostOneBeliever BelieverIsHolder BelieverKeepsClaims ReleaseFencing ClaimSetupFencing"
for kv in "$@"; do
  if [[ "$kv" != *=* ]]; then
    echo "mkcfg.sh: invalid argument '$kv' (expected key=value)" >&2
    exit 1
  fi
  k=${kv%%=*}
  v=${kv#*=}
  case "$k" in
    INV) inv="$v" ;;
    D|Grace|G|M|SkewMax|Lat|MaxTime|MaxInc|MaxRounds|Features|Bugs|Fixes) c["$k"]="$v" ;;
    *) echo "mkcfg.sh: unknown key '$k'" >&2; exit 1 ;;
  esac
done
{
  echo "SPECIFICATION Spec"
  echo "CONSTANTS"
  for k in D Grace G M SkewMax Lat MaxTime MaxInc MaxRounds Features Bugs Fixes; do
    echo "  $k = ${c[$k]}"
  done
  echo "  Procs = {h1, h2}"
  echo "  NoOne = none"
  echo "SYMMETRY Symm"
  echo "INVARIANTS"
  for i in $inv; do
    echo "  $i"
  done
} > "$name.cfg"
