#!/usr/bin/env bash
# Runs Tomer's loadtest (examples/agent-sandbox-rl/tests/loadtest.py) as per-claim
# and batch arms on the GKE bench cluster, from the driver pod (--in-cluster).
#
# Every arm uses the same tasks, strategy, pools, node pool, runtime class and
# --max-concurrent; only --claim-mode/--batch-claims differ. Arms are interleaved
# and the order is rotated each round. A finished run (its .json exists) is skipped,
# so the script resumes after a stop.
#
# Env: LT_IMAGE_REPO (required, e.g. us-central1-docker.pkg.dev/P/rl-gke-bench/rl-task),
#      LT_OUT (default bench-results/gke-scripts/loadtest), LT_RUNS (default 3),
#      LT_RUNTIME_CLASS (default gvisor), LT_NODE_POOL (default sandbox).
set -uo pipefail
cd "$(dirname "$0")/../examples/agent-sandbox-rl"
: "${LT_IMAGE_REPO:?set LT_IMAGE_REPO}"
OUT=${LT_OUT:-bench-results/gke-scripts/loadtest}
RUNS=${LT_RUNS:-3}
PY=${LT_PYTHON:-/work/venv/bin/python}
mkdir -p "$OUT"

# label | max_concurrent | claim_mode | batch_claims
ARMS=(
  "mc160-per-claim|160|per_claim|unbounded"
  "mc160-batch-unbounded|160|batch|unbounded"
  "mc160-batch-defaults|160|batch|defaults"
  "mc40-per-claim|40|per_claim|unbounded"
  "mc40-batch-unbounded|40|batch|unbounded"
)
n=${#ARMS[@]}
for ((run = 1; run <= RUNS; run++)); do
  for ((k = 0; k < n; k++)); do
    IFS='|' read -r label mc mode batch <<< "${ARMS[$(( (k + run - 1) % n ))]}"
    base="$OUT/$label-run$run"
    [[ -f "$base.json" ]] && continue
    echo "$(date -u +%H:%M:%S) $label-run$run"
    "$PY" tests/loadtest.py --in-cluster --namespace rl-loadtest \
      --images 40 --tasks-per-image 4 --strategies naive --warm-per-task \
      --image-template "$LT_IMAGE_REPO:g{i:03d}" \
      --node-selector "cloud.google.com/gke-nodepool=${LT_NODE_POOL:-sandbox}" \
      --runtime-class "${LT_RUNTIME_CLASS:-gvisor}" \
      --max-concurrent "$mc" --claim-mode "$mode" --batch-claims "$batch" \
      --task-duration 0 --out "$base.md" > "$base.log" 2>&1
    echo "  exit=$? $(grep -m1 -E 'naive: wall=' "$base.log" | sed 's/^ *//')"
  done
done
