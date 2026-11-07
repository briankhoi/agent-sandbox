#!/bin/bash
# usage: run.sh NAME [SPEC.tla]  (runs TLC on NAME.cfg against SPEC.tla, log to logs/NAME.log)
cd "$(dirname "$0")"; mkdir -p logs states
spec="${2:-BatchLease.tla}"
workers="${TLC_WORKERS:-auto}"
if [[ "$workers" == "auto" && ( "$1" == neg_* || "$1" == *_neg_* ) ]]; then
  workers=1
fi
deadlock_flag=()
if [[ "$spec" == "BatchLease.tla" ]]; then
  deadlock_flag=(-deadlock)
fi
timeout ${TLC_TIMEOUT:-600} ${JAVA:-java} -XX:+UseParallelGC -cp tla2tools.jar tlc2.TLC \
  -workers "$workers" "${deadlock_flag[@]}" -metadir states/$1 -config $1.cfg "$spec" > logs/$1.log 2>&1
grep -E 'Error: Invariant|Error: Deadlock|No error|states generated|depth of|Finished in' logs/$1.log
echo "trace states: $(grep -c '^State [0-9]' logs/$1.log)"
