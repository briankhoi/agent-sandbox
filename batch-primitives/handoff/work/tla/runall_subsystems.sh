#!/bin/bash
# Automated TLC runner for the modular batch TLA+ subsystem verification suite.
# Deadlock checking is ENABLED across all 5 subsystem specs (BatchHandle, BatchWatch,
# BatchConsumer, BatchAcquireRelease, RLBatchLifecycle); only legacy finite-time
# BatchLease.tla passes -deadlock for its MaxTime = 14 clock bound.
#
# Usage:
#   ./runall_subsystems.sh              # runs all positive specs + negative-control proofs
#   ./runall_subsystems.sh --positive   # runs only positive (fixed-model) specs

set -euo pipefail
cd "$(dirname "$0")"
mkdir -p logs states

MODE="${1:---all}"
JAVA_BIN="${JAVA:-java}"
TLC_TIMEOUT="${TLC_TIMEOUT:-300}"

if [[ ! -f tla2tools.jar ]]; then
  echo "ERROR: tla2tools.jar not found in $(pwd)" >&2
  exit 2
fi

if [[ -f mkcfg.sh && ( ! -f main_claim_setup.cfg || ! -f main_member_ops.cfg ) ]]; then
  bash ./mkcfg.sh >/dev/null
fi

# Format: CONFIG_NAME|SPEC_FILE|EXPECTED_OUTCOME(PASS|VIOLATE)|DESCRIPTION
RUNS=(
  "bh_fixed|BatchHandle.tla|PASS|Subsystem authority separation (decoupled watch/Lease)"
  "bh_ext_loss|BatchHandle.tla|PASS|Subsystem authority separation under 404/409 Lease loss"
  "bw_main|BatchWatch.tla|PASS|Watch stream, 410 multi-page relist, pod swap eviction"
  "bc_main|BatchConsumer.tla|PASS|Consumer modes, post-quorum readiness, STREAM composition"
  "bar_main|BatchAcquireRelease.tla|PASS|Concurrent fill flap, acquire, release_group, delete seq"
  "rl_main|RLBatchLifecycle.tla|PASS|RL BatchClaims rotation and per-batch tracked teardown"
  "main_claim_setup|BatchLease.tla|PASS|Cross-process Lease claim_batch setup fencing"
  "main_member_ops|BatchLease.tla|PASS|Cross-process Lease-bounded member delete fencing"
)

NEG_RUNS=(
  "bh_neg_janet|BatchHandle.tla|VIOLATE|Baseline bug (e84e964c): Watch 403 halts Lease renewal (INV-AUTH-1, F-01a)"
  "bh_neg_janet_release|BatchHandle.tla|VIOLATE|Baseline bug (e84e964c): Watch 403 blocks release_member (INV-AUTH-2, F-01b)"
  "bw_neg_late_added|BatchWatch.tla|VIOLATE|Baseline bug (e84e964c): Late watch ADDED after release omits DELETE (INV-LEAK-1, F-03)"
  "bc_neg_waiting_leak|BatchConsumer.tla|VIOLATE|Baseline bug (e84e964c): _time_out_undecided_groups omits _resolved (INV-ACCT-1, F-12)"
  "bc_neg_popped_slot|BatchConsumer.tla|VIOLATE|Baseline bug (e84e964c): Popped pacing slot misses release_group (INV-ACCT-2, F-25)"
  "bar_neg_inflight_release|BatchAcquireRelease.tla|VIOLATE|Baseline bug (e84e964c): In-flight acquire POST escapes release() (INV-LEAK-1, F-24)"
  "rl_neg_teardown_race|RLBatchLifecycle.tla|VIOLATE|Baseline bug (e84e964c): _batch_for leaks renewing batch on closed+503 (INV-LEAK-1, F-07a)"
  "rl_neg_clear_upfront|RLBatchLifecycle.tla|VIOLATE|Baseline bug (e84e964c): release_all clears _opened upfront before release (INV-LEAK-1, F-07b)"
  "bw_neg_stale_conn|BatchWatch.tla|VIOLATE|Mutation control: pod swap without connection cache eviction (INV-CONN-1)"
  "bw_neg_drop_page1|BatchWatch.tla|VIOLATE|Mutation control: 410 relist drops page-1 items causing false LOST (INV-ACCT-4)"
  "bw_neg_resurrect|BatchWatch.tla|VIOLATE|Mutation control: 410 relist resurrects released claim (INV-OWN-4)"
  "bc_neg_stale_ordinal|BatchConsumer.tla|VIOLATE|Mutation control: stale reattach ordinal collides on acquire (INV-ACCT-3)"
  "bar_neg_seq|BatchAcquireRelease.tla|VIOLATE|Mutation control: unsequenced _pending_deletes drop on retry (INV-OWN-4)"
  "bar_neg_pool_seq|BatchAcquireRelease.tla|VIOLATE|Mutation control: acquire ignores _pool_release_seq across release_group (INV-OWN-4)"
)

if [[ "$MODE" != "--positive" ]]; then
  RUNS+=("${NEG_RUNS[@]}")
fi

printf "%-24s | %-24s | %-8s | %-10s | %-12s | %s\n" "CONFIG" "SPEC" "EXPECTED" "ACTUAL" "STATES" "NOTES"
printf "%s\n" "------------------------------------------------------------------------------------------------------------------------"

failures=0
for entry in "${RUNS[@]}"; do
  IFS='|' read -r cfg spec expected desc <<< "$entry"
  metadir="states/${cfg}.$$"
  rm -rf "$metadir"
  workers="auto"
  if [[ "$expected" == "VIOLATE" ]]; then
    workers="1"
  fi
  deadlock_flag=()
  if [[ "$spec" == "BatchLease.tla" ]]; then
    deadlock_flag=(-deadlock)
  fi
  start_ts=$(date +%s)
  timeout "$TLC_TIMEOUT" "$JAVA_BIN" -XX:+UseParallelGC -cp tla2tools.jar tlc2.TLC \
    -workers "$workers" "${deadlock_flag[@]}" -metadir "$metadir" -config "$cfg.cfg" "$spec" > "logs/$cfg.log" 2>&1 || true
  rm -rf "$metadir"
  elapsed=$(( $(date +%s) - start_ts ))

  if grep -q "No error has been found" "logs/$cfg.log"; then
    actual="PASS"
  elif grep -q "Error: Deadlock reached" "logs/$cfg.log"; then
    actual="DEADLOCK"
  elif grep -q "Error: Invariant .* is violated" "logs/$cfg.log"; then
    actual="VIOLATE"
  else
    actual="ERROR"
  fi

  distinct=$(grep -oP '\d+ distinct states found' "logs/$cfg.log" | tail -1 | awk '{print $1}' || true)
  [[ -z "$distinct" ]] && distinct="0"

  status_ok="OK"
  if [[ "$actual" != "$expected" ]]; then
    status_ok="MISMATCH"
    failures=$((failures + 1))
  fi

  printf "%-24s | %-24s | %-8s | %-10s | %-12s | [%s, %ds] %s\n" \
    "$cfg" "$spec" "$expected" "$actual" "$distinct" "$status_ok" "$elapsed" "$desc"
done

printf "%s\n" "------------------------------------------------------------------------------------------------------------------------"
if [[ "$failures" -gt 0 ]]; then
  echo "TLA+ SUBSYSTEM SUITE FAILED: $failures run(s) did not match expected outcome."
  exit 1
fi
echo "TLA+ SUBSYSTEM SUITE PASSED: all ${#RUNS[@]} model-checking runs matched expected outcomes."
