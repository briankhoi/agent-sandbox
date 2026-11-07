#!/usr/bin/env bash
# Bootstrap a Campaign 3 GKE cluster (`asrl-c3-1..5`) born at `e2-standard-16` (`gomaxprocs=16`)
# via initial 110-node Spot `default-pool` (`go/kcp-resize` 101-500 bucket):
#  1. Verify `go_sched_gomaxprocs_threads >= 16`
#  2. Delete Spot `default-pool` (and any `cp-warmup-spot` if present) once `driver-pool` + `ctrl-pool` exist
#  3. Re-verify `go_sched_gomaxprocs_threads >= 16` and `0` Spot VMs
#  4. Install CRDs + controller RBAC + tuned controller (`controller-p3.yaml`) + driver RBAC + 3-lane APF
set -euo pipefail

export PATH="$HOME/google-cloud-sdk/bin:/google/google-cloud-sdk/bin:/usr/lib/google-cloud-sdk/bin:$PATH"
PROJECT="<gcp-project>"
REGION="us-central1"
CLUSTER="${1:?usage: bootstrap_cluster_c3.sh <cluster_name>}"
CTX="gke_${PROJECT}_${REGION}_${CLUSTER}"

BENCH_DIR="${BENCH_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
CRD_DIR="${CRD_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../../../../k8s/crds" && pwd)}"
REPO_C3="${REPO_C3:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

gcloud container clusters get-credentials "$CLUSTER" --region="$REGION" --project="$PROJECT"
K() { kubectl --context "$CTX" "$@"; }

wait_cluster_running() {
  for w in $(seq 1 80); do
    ST=$(gcloud container clusters describe "$CLUSTER" --region="$REGION" --project="$PROJECT" --format='value(status)' 2>/dev/null || echo "UNKNOWN")
    if [ "$ST" = "RUNNING" ]; then
      return 0
    fi
    echo "  [$CLUSTER wait $w/80] cluster status=$ST (waiting for RUNNING)..."
    sleep 15
  done
  echo "ERROR: $CLUSTER still not RUNNING!" >&2
  return 1
}

read_gomaxprocs() {
  { K get --raw /metrics 2>/dev/null || true; } | awk '/^go_sched_gomaxprocs_threads /{v=int($2)} END{print v+0}'
}

GMP=$(read_gomaxprocs)
echo "[$(date -u +%FT%TZ)] [$CLUSTER] go_sched_gomaxprocs_threads = ${GMP}"
if [ "$GMP" -lt 16 ]; then
  echo "ERROR: $CLUSTER go_sched_gomaxprocs_threads=${GMP} (< 16)!" >&2
  exit 1
fi

for POOL_DEL in default-pool cp-warmup-spot; do
  if gcloud container node-pools describe "$POOL_DEL" --cluster="$CLUSTER" --region="$REGION" --project="$PROJECT" >/dev/null 2>&1; then
    IS_SPOT=$(gcloud container node-pools describe "$POOL_DEL" --cluster="$CLUSTER" --region="$REGION" --project="$PROJECT" --format='value(config.spot)' 2>/dev/null || echo "")
    if [ "$IS_SPOT" = "True" ] || [ "$POOL_DEL" = "cp-warmup-spot" ]; then
      echo "[$(date -u +%FT%TZ)] [$CLUSTER] Deleting temporary Spot warm-up pool $POOL_DEL..."
      wait_cluster_running
      gcloud container node-pools delete "$POOL_DEL" --cluster="$CLUSTER" --region="$REGION" --project="$PROJECT" --quiet
      wait_cluster_running
    fi
  fi
done

SPOT_LEFT=999
for sw in $(seq 1 36); do
  SPOT_LEFT=$({ K get nodes -l cloud.google.com/gke-spot=true --no-headers 2>/dev/null || true; } | wc -l | tr -d ' ')
  if [ "${SPOT_LEFT:-0}" -eq 0 ]; then
    break
  fi
  echo "  [$CLUSTER spot-gc $sw/36] waiting for $SPOT_LEFT deleted Spot Node objects to clear..."
  sleep 5
done
if [ "${SPOT_LEFT:-0}" -ne 0 ]; then
  echo "ERROR: $SPOT_LEFT Spot nodes still present on $CLUSTER!" >&2
  exit 1
fi

GMP_AFTER=$(read_gomaxprocs)
echo "  [$CLUSTER] Post-Spot-delete go_sched_gomaxprocs_threads=${GMP_AFTER} (0 Spot VMs remaining)"
if [ "$GMP_AFTER" -lt 16 ]; then
  echo "ERROR: $CLUSTER go_sched_gomaxprocs_threads=${GMP_AFTER} (< 16) after Spot delete!" >&2
  exit 1
fi

echo "[$(date -u +%FT%TZ)] [$CLUSTER] Applying CRDs, controller (P3), driver RBAC, and 3-lane APF..."
K create namespace agent-sandbox-system --dry-run=client -o yaml | K apply -f -
K create namespace agent-sandbox-rl --dry-run=client -o yaml | K apply -f -
K apply --server-side -f "$CRD_DIR/"
K apply -f "$BENCH_DIR/manifests/controller-base-rbac.yaml"
K apply -f "$BENCH_DIR/manifests/controller-p3.yaml"
K apply -f "$BENCH_DIR/manifests/driver-rbac.yaml"
K apply -f "$BENCH_DIR/manifests/apf-insulation-gke.yaml"

K -n agent-sandbox-system rollout status deploy/agent-sandbox-controller --timeout=180s
K get --raw /metrics 2>/dev/null | grep -E '^go_sched_gomaxprocs_threads |^apiserver_flowcontrol_nominal_limit_seats' \
  > "$REPO_C3/telemetry/${CLUSTER}_preflight_apf.txt"

echo "[$(date -u +%FT%TZ)] BOOTSTRAP COMPLETE ON $CLUSTER (gomaxprocs=${GMP_AFTER}, controller Ready, 3-lane APF active)."
