#!/bin/bash
# Runs every config from configs.sh and writes a one-line summary per run to summary.txt.
cd "$(dirname "$0")"; : > summary.txt
for cfg in $(grep -oP '(?<=mkcfg.sh )\S+' configs.sh); do
  start=$(date +%s)
  TLC_TIMEOUT=${TLC_TIMEOUT:-400} ./run.sh $cfg > /dev/null
  l=logs/$cfg.log
  res=$(grep -oP 'Invariant \w+ is violated|No error has been found' $l | head -1); [ -z "$res" ] && res="TIMEOUT/ERROR"
  st=$(grep -oP '^\d+ states generated, \d+ distinct states found' $l | tail -1)
  dep=$(grep -oP 'depth of the complete state graph search is \d+' $l | grep -oP '\d+$')
  tr=$(grep -c '^State [0-9]' $l)
  echo "$cfg | $res | $st | depth $dep | trace $tr | $(( $(date +%s)-start ))s" | tee -a summary.txt
  rm -rf states/$cfg
done
