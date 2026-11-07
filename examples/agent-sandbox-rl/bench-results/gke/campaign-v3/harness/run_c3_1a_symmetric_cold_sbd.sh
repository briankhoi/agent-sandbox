#!/usr/bin/env bash
# Step 5.5 of Campaign 3: 100% Symmetric First-Boot Cold SBD Head-to-Head (`c3_1a_cold_sbd_symmetric_{per_claim,batch64}`)
#
# Guarantees strict apples-to-apples fairness on GCE lazy SBD block hydration:
#   1. Deletes gvisor-pool and creates Brand-New 10x n2-standard-32 + 600GB swebench-500-preload-600g SBD Pool #1
#   2. Smoke Gate #12 (non-SWE-bench image -> 0/500 SWE-bench SBD blocks touched, SETTLE_S=30)
#   3. Arm 1: per_claim 500x6=3,000 SWE-bench T1 SBD naive (tgs=0, cow=True, SETTLE_S=30)
#   4. Deletes gvisor-pool and creates Brand-New 10x n2-standard-32 + 600GB swebench-500-preload-600g SBD Pool #2
#   5. Smoke Gate #13 (non-SWE-bench image -> 0/500 SWE-bench SBD blocks touched, SETTLE_S=30)
#   6. Arm 2: batch64 (mif=64, rps=200) 500x6=3,000 SWE-bench T1 SBD naive (tgs=0, cow=True, SETTLE_S=30)
set -euo pipefail

export PATH="$HOME/google-cloud-sdk/bin:/google/google-cloud-sdk/bin:/usr/lib/google-cloud-sdk/bin:$PATH"
PROJECT="<GCP_PROJECT>"
REGION="us-central1"
CLUSTER="asrl-c3-1"
export CTX="gke_${PROJECT}_${REGION}_${CLUSTER}"
BENCH_DIR="<SCRATCH_DIR>/gke_bench_v3"
export KUBECONFIG="$BENCH_DIR/kubeconfig_c1_sym.yaml"
export DRIVER_IMAGE="us-central1-docker.pkg.dev/<GCP_PROJECT>/rl-gke-bench/asrl-driver@sha256:6d0b37df8c80b092186d5c8ee66d13e646283d48a686301d6ece6c3532f2a8c3"

gcloud container clusters get-credentials "$CLUSTER" --region="$REGION" --project="$PROJECT"

K() { kubectl --context "$CTX" "$@"; }

# Pre-flight verification (§3 Mandatory Rules)
GMP=$({ K get --raw /metrics 2>/dev/null || true; } | awk '/^go_sched_gomaxprocs_threads /{v=int($2)} END{print v+0}')
echo "[$(date -u +%FT%TZ)] [$CLUSTER] Verified go_sched_gomaxprocs_threads = ${GMP}"
if [ "${GMP}" -lt 16 ]; then
  echo "ERROR: $CLUSTER has go_sched_gomaxprocs_threads=${GMP} (< 16)!" >&2
  exit 1
fi

SPOT_CNT=$(K get nodes -l cloud.google.com/gke-spot=true --no-headers 2>/dev/null | wc -l | tr -d ' ')
echo "[$(date -u +%FT%TZ)] [$CLUSTER] Verified Spot nodes = ${SPOT_CNT}"
test "$SPOT_CNT" -eq 0

K -n agent-sandbox-system wait --for=condition=Ready pod -l control-plane=controller-manager --timeout=120s
FS_CNT=$(K get flowschema | grep -c '^agent-sandbox-' || true)
echo "[$(date -u +%FT%TZ)] [$CLUSTER] Verified agent-sandbox APF FlowSchemas = ${FS_CNT}"
test "$FS_CNT" -ge 2

recreate_fresh_cold_sbd_pool() {
  local label="$1"
  echo "=================================================================="
  echo "[$(date -u +%FT%TZ)] RECREATING FRESH UNHYDRATED SBD gvisor-pool FOR ${label}"
  echo "=================================================================="
  K -n agent-sandbox-rl delete job -l app=asrl-driver --ignore-not-found --wait=true >/dev/null 2>&1 || true
  K -n agent-sandbox-rl delete sandboxclaim --all --ignore-not-found --wait=false >/dev/null 2>&1 || true
  K -n agent-sandbox-rl delete sandboxwarmpool --all --ignore-not-found --wait=false >/dev/null 2>&1 || true
  K -n agent-sandbox-rl delete sandbox --all --ignore-not-found --wait=false >/dev/null 2>&1 || true
  K -n agent-sandbox-rl delete sandboxtemplate --all --ignore-not-found --wait=false >/dev/null 2>&1 || true
  K -n agent-sandbox-rl delete lease -l agents.x-k8s.io/batch-id --ignore-not-found >/dev/null 2>&1 || true
  K -n agent-sandbox-rl delete pod -l app=agent-sandbox-rl --grace-period=0 --force --ignore-not-found >/dev/null 2>&1 || true

  if gcloud container node-pools describe gvisor-pool --cluster="$CLUSTER" --region="$REGION" --project="$PROJECT" >/dev/null 2>&1; then
    gcloud container node-pools delete gvisor-pool \
      --project="$PROJECT" --region="$REGION" --cluster="$CLUSTER" --quiet
  fi

  gcloud container node-pools create gvisor-pool \
    --project="$PROJECT" \
    --region="$REGION" \
    --cluster="$CLUSTER" \
    --node-locations=us-central1-a,us-central1-b \
    --machine-type=n2-standard-32 \
    --num-nodes=5 \
    --disk-type=pd-standard \
    --disk-size=100 \
    --secondary-boot-disk=disk-image=projects/${PROJECT}/global/images/swebench-500-preload-600g,mode=CONTAINER_IMAGE_CACHE \
    --max-pods-per-node=256 \
    --sandbox=type=gvisor \
    --enable-image-streaming \
    --scopes=cloud-platform \
    --quiet

  K wait --for=condition=Ready nodes -l cloud.google.com/gke-nodepool=gvisor-pool --timeout=300s
  local node_cnt
  node_cnt=$(K get nodes -l cloud.google.com/gke-nodepool=gvisor-pool --no-headers | wc -l | tr -d ' ')
  echo "[$(date -u +%FT%TZ)] [${label}] Fresh SBD gvisor-pool Ready nodes: ${node_cnt}"
  test "$node_cnt" -eq 10
}

COMMON_ARGS="--in-cluster --namespace agent-sandbox-rl --node-selector cloud.google.com/gke-nodepool=gvisor-pool --runtime-class gvisor --images-file /app/drivers/images/swebench500.txt --unmanaged-network-policy --termination-grace-seconds 0 --enable-cow"

# --- ARM 1: per_claim on Fresh Cold SBD Pool #1 ---
recreate_fresh_cold_sbd_pool "ARM 1 (per_claim)"

echo "[$(date -u +%FT%TZ)] === Smoke Gate #12 (Fresh Cold SBD Pool #1 — non-SWE-bench image, SETTLE_S=30) ==="
IS_SMOKE=1 SETTLE_S=30 bash "$BENCH_DIR/run_job_v3.sh" c3_smoke_12_cold_sbd_per_claim \
  "python run_smoke_gate.py --in-cluster --namespace agent-sandbox-rl --node-selector cloud.google.com/gke-nodepool=gvisor-pool --runtime-class gvisor --smoke-image ${DRIVER_IMAGE} --per-cluster-claims 10 --unmanaged-network-policy"

echo "[$(date -u +%FT%TZ)] === Arm 1: Symmetric First-Boot Cold SBD 3,000 SWE-bench naive (per_claim, SETTLE_S=30) ==="
SETTLE_S=30 bash "$BENCH_DIR/run_job_v3.sh" c3_1a_cold_sbd_symmetric_per_claim \
  "python run_basic_swebench_3000.py ${COMMON_ARGS} --problems 500 --rollouts 6 --concurrency 500 --cpu 125m --memory 1Gi --strategies naive --probe true --claim-mode per_claim"

# --- ARM 2: batch64 on Fresh Cold SBD Pool #2 ---
recreate_fresh_cold_sbd_pool "ARM 2 (batch64)"

echo "[$(date -u +%FT%TZ)] === Smoke Gate #13 (Fresh Cold SBD Pool #2 — non-SWE-bench image, SETTLE_S=30) ==="
IS_SMOKE=1 SETTLE_S=30 bash "$BENCH_DIR/run_job_v3.sh" c3_smoke_13_cold_sbd_batch64 \
  "python run_smoke_gate.py --in-cluster --namespace agent-sandbox-rl --node-selector cloud.google.com/gke-nodepool=gvisor-pool --runtime-class gvisor --smoke-image ${DRIVER_IMAGE} --per-cluster-claims 10 --unmanaged-network-policy"

echo "[$(date -u +%FT%TZ)] === Arm 2: Symmetric First-Boot Cold SBD 3,000 SWE-bench naive (batch64, SETTLE_S=30) ==="
SETTLE_S=30 bash "$BENCH_DIR/run_job_v3.sh" c3_1a_cold_sbd_symmetric_batch64 \
  "python run_basic_swebench_3000.py ${COMMON_ARGS} --problems 500 --rollouts 6 --concurrency 500 --cpu 125m --memory 1Gi --strategies naive --probe true --claim-mode batch --batch-max-in-flight 64 --batch-create-rps 200"

echo "[$(date -u +%FT%TZ)] === STEP 5.5 SYMMETRIC FIRST-BOOT COLD SBD BENCHMARK COMPLETE! ==="
