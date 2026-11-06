#!/usr/bin/env bash
# Provision & bootstrap clusters asrl-c3-2..5 in parallel, each born directly at `gomaxprocs=16`
# (`e2-standard-16` KCP) via a 110-node initial Spot `default-pool` (`go/kcp-resize` 101-500 bucket).
set -euo pipefail

export PATH="$HOME/google-cloud-sdk/bin:/google/google-cloud-sdk/bin:/usr/lib/google-cloud-sdk/bin:$PATH"
PROJECT="<gcp-project>"
REGION="us-central1"
BENCH_DIR="/usr/local/google/home/brianknguyen/.gemini/jetski/brain/8634985c-d147-49db-bef1-bba51884db9b/scratch/gke_bench_v3"
LOG_DIR="$BENCH_DIR/mc_prov_logs"
mkdir -p "$LOG_DIR"

gcloud compute networks subnets update default --region="$REGION" --project="$PROJECT" --enable-private-ip-google-access --quiet || true

prov_one() {
  local idx="$1"
  local CLUSTER="asrl-c3-${idx}"
  local MASTER_CIDR="172.16.${idx}.0/28"
  local CTX="gke_${PROJECT}_${REGION}_${CLUSTER}"
  export KUBECONFIG="$LOG_DIR/kubeconfig_c${idx}.yaml"

  echo "[$(date -u +%FT%TZ)] [$CLUSTER] Creating GKE cluster born at n1-standard-32 KCP / gomaxprocs=16 (170x3=510 initial Spot nodes)..."
  if ! gcloud container clusters describe "$CLUSTER" --region="$REGION" --project="$PROJECT" >/dev/null 2>&1; then
    gcloud container clusters create "$CLUSTER" \
      --project="$PROJECT" \
      --region="$REGION" \
      --node-locations=us-central1-a,us-central1-b,us-central1-c \
      --release-channel=rapid \
      --spot \
      --machine-type=e2-medium \
      --num-nodes=170 \
      --disk-type=pd-standard \
      --disk-size=30 \
      --max-pods-per-node=16 \
      --default-max-pods-per-node=256 \
      --node-taints=warmup=only:NoSchedule \
      --enable-ip-alias \
      --cluster-ipv4-cidr=/16 \
      --services-ipv4-cidr=/20 \
      --enable-private-nodes \
      --master-ipv4-cidr="$MASTER_CIDR" \
      --enable-master-authorized-networks \
      --master-authorized-networks=0.0.0.0/0 \
      --enable-image-streaming \
      --scopes=cloud-platform \
      --quiet
  fi

  wait_running() {
    for _w in $(seq 1 60); do
      local st
      st=$(gcloud container clusters describe "$CLUSTER" --region="$REGION" --project="$PROJECT" --format='value(status)' 2>/dev/null || echo "UNKNOWN")
      [ "$st" = "RUNNING" ] && return 0
      sleep 10
    done
    return 1
  }

  echo "[$(date -u +%FT%TZ)] [$CLUSTER] Creating driver-pool (2x n2-standard-16)..."
  if ! gcloud container node-pools describe driver-pool --cluster="$CLUSTER" --region="$REGION" --project="$PROJECT" >/dev/null 2>&1; then
    wait_running
    gcloud container node-pools create driver-pool \
      --project="$PROJECT" \
      --region="$REGION" \
      --cluster="$CLUSTER" \
      --node-locations=us-central1-a,us-central1-b \
      --machine-type=n2-standard-16 \
      --num-nodes=1 \
      --disk-type=pd-standard \
      --disk-size=100 \
      --node-labels=bench-role=driver \
      --max-pods-per-node=110 \
      --scopes=cloud-platform \
      --quiet
  fi

  echo "[$(date -u +%FT%TZ)] [$CLUSTER] Creating ctrl-pool (2x e2-standard-32)..."
  if ! gcloud container node-pools describe ctrl-pool --cluster="$CLUSTER" --region="$REGION" --project="$PROJECT" >/dev/null 2>&1; then
    wait_running
    gcloud container node-pools create ctrl-pool \
      --project="$PROJECT" \
      --region="$REGION" \
      --cluster="$CLUSTER" \
      --node-locations=us-central1-a,us-central1-b \
      --machine-type=e2-standard-32 \
      --num-nodes=1 \
      --disk-type=pd-standard \
      --disk-size=100 \
      --node-labels=dedicated=ctrl \
      --node-taints=dedicated=ctrl:NoSchedule \
      --max-pods-per-node=110 \
      --scopes=cloud-platform \
      --quiet
  fi

  echo "[$(date -u +%FT%TZ)] [$CLUSTER] Creating 10x n2-standard-32 + 600GB SBD gvisor-pool..."
  if ! gcloud container node-pools describe gvisor-pool --cluster="$CLUSTER" --region="$REGION" --project="$PROJECT" >/dev/null 2>&1; then
    wait_running
    gcloud container node-pools create gvisor-pool \
      --project="$PROJECT" \
      --region="$REGION" \
      --cluster="$CLUSTER" \
      --node-locations=us-central1-a,us-central1-b \
      --machine-type=n2-standard-32 \
      --num-nodes=5 \
      --disk-type=pd-standard \
      --disk-size=100 \
      --secondary-boot-disk=disk-image=projects/<gcp-project>/global/images/swebench-500-preload-600g,mode=CONTAINER_IMAGE_CACHE \
      --max-pods-per-node=256 \
      --sandbox=type=gvisor \
      --enable-image-streaming \
      --scopes=cloud-platform \
      --quiet
  fi

  echo "[$(date -u +%FT%TZ)] [$CLUSTER] Bootstrapping (verifying gomaxprocs=16, deleting Spot default-pool, installing CRDs/controller/APF)..."
  bash "$BENCH_DIR/bootstrap_cluster_c3.sh" "$CLUSTER"

  kubectl --context "$CTX" wait --for=condition=Ready nodes -l cloud.google.com/gke-nodepool=gvisor-pool --timeout=300s
  local CNT
  CNT=$(kubectl --context "$CTX" get nodes -l cloud.google.com/gke-nodepool=gvisor-pool --no-headers | wc -l | tr -d ' ')
  echo "[$(date -u +%FT%TZ)] [$CLUSTER] READY! SBD gvisor-pool nodes=$CNT"
  test "$CNT" -eq 10
}

PIDS=()
for idx in 2 3 4 5; do
  prov_one "$idx" > "$LOG_DIR/asrl-c3-${idx}.log" 2>&1 &
  PIDS+=("$!")
  sleep 8
done

echo "[$(date -u +%FT%TZ)] Waiting for parallel provisioning of asrl-c3-2..5 (PIDs: ${PIDS[*]})..."
FAIL=0
for i in 0 1 2 3; do
  cidx=$((i + 2))
  if wait "${PIDS[$i]}"; then
    echo "[$(date -u +%FT%TZ)] asrl-c3-${cidx} PROVISIONED SUCCESSFULLY"
  else
    echo "ERROR: asrl-c3-${cidx} provisioning failed! Tail of log:" >&2
    tail -n 40 "$LOG_DIR/asrl-c3-${cidx}.log" >&2
    FAIL=1
  fi
done

if [ "$FAIL" -ne 0 ]; then
  exit 1
fi

export KUBECONFIG="$BENCH_DIR/kubeconfig_all5.yaml"
for idx in 1 2 3 4 5; do
  gcloud container clusters get-credentials "asrl-c3-${idx}" --region="$REGION" --project="$PROJECT"
done

echo "[$(date -u +%FT%TZ)] ALL 5 CLUSTERS (asrl-c3-1..5) READY WITH gomaxprocs >= 16 AND 50 SBD NODES!"
