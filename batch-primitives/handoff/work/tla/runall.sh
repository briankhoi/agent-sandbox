#!/bin/bash
set -euo pipefail
# Runs every config from configs.sh and writes a one-line summary per run to summary.txt.
cd "$(dirname "$0")"
: > summary.txt
for cfg in $(awk '/^\.\/mkcfg\.sh / {print $2}' configs.sh); do
  start=$(date +%s)
  rm -rf "states/$cfg"
  TLC_TIMEOUT=${TLC_TIMEOUT:-400} ./run.sh "$cfg" > /dev/null || true
  l="logs/$cfg.log"
  res=$(grep -oP 'Invariant \w+ is violated|No error has been found' "$l" | head -1 || true)
  [ -z "$res" ] && res="TIMEOUT/ERROR"
  st=$(grep -oP '^\d+ states generated, \d+ distinct states found, \d+ states left on queue' "$l" | tail -1 || true)
  dep=$(grep -oP 'depth of the complete state graph search is \d+' "$l" | grep -oP '\d+$' || true)
  tr=$(grep -c '^State [0-9]' "$l" || true)
  echo "$cfg | $res | $st | depth $dep | trace $tr | $(( $(date +%s)-start ))s" | tee -a summary.txt
  rm -rf "states/$cfg"
done
