#!/bin/bash
# usage: run.sh NAME  (runs TLC on NAME.cfg, log to logs/NAME.log)
cd "$(dirname "$0")"; mkdir -p logs states
timeout ${TLC_TIMEOUT:-600} ${JAVA:-java} -XX:+UseParallelGC -cp tla2tools.jar tlc2.TLC \
  -workers auto -deadlock -metadir states/$1 -config $1.cfg BatchLease.tla > logs/$1.log 2>&1
grep -E 'Error: Invariant|No error|states generated|depth of|Finished in' logs/$1.log
echo "trace states: $(grep -c '^State [0-9]' logs/$1.log)"
