#!/usr/bin/env bash
# Re-provision Cloud Router + 8-IP Cloud NAT + all 5 Campaign 3 GKE clusters (`asrl-c3-1..5`),
# strictly following CAMPAIGN_3_MASTER_TRACKER.md §§2C, 3, 4:
#   - Cloud NAT `asrl-c3-nat` backed by 8 static external IPs (`asrl-c3-nat-ip-1..8`) + dynamic port
#     allocation (`min=4096, max=32768`) so private-node traffic to `c2..c5` external apiserver LBs
#     is striped across 8 source IPs and never hits single-IP SYN policing.
#   - Each cluster born at `n1-standard-32` KCP (`go_sched_gomaxprocs_threads >= 16`) via `170x3 = 510`
#     initial Spot `e2-medium` `pd-standard` warmup nodes (`go/kcp-resize` 501-1000 bucket).
#   - Wave 1 launches `asrl-c3-1..4` (`4 x 1,020 = 4,080 <= 5,000 PREEMPTIBLE_CPUS`); `asrl-c3-5`
#     waits until `PREEMPTIBLE_CPUS <= 3,800` (as soon as the first Spot warmup pool deletes) so
#     quota is never exceeded.
#   - `bootstrap_cluster_c3.sh` verifies `gomaxprocs >= 16`, deletes Spot `default-pool` (`0` Spot VMs),
#     re-verifies `gomaxprocs >= 16`, and installs CRDs + `controller-p3.yaml` + `driver-rbac.yaml`
#     + 3-lane `apf-insulation-gke.yaml`.
#   - Attaches `10 x n2-standard-32` (`pd-standard` OS disk + `600 GB` `swebench-500-preload-600g` SBD)
#     `gvisor-pool` on every cluster (`50` SBD nodes = `1,600` worker vCPUs, `30,000 GB` SBD).
#   - Runs 8-point preflight verification (`verify_cluster.sh` + APF + controller Ready) on all 5 clusters.
set -euo pipefail

export PATH="$HOME/google-cloud-sdk/bin:/google/google-cloud-sdk/bin:/usr/lib/google-cloud-sdk/bin:$PATH"
PROJECT="<GCP_PROJECT>"
REGION="us-central1"
BENCH_DIR="<SCRATCH_DIR>/gke_bench_v3"
LOG_DIR="$BENCH_DIR/mc_prov_logs_rerun"
VERIFY_SH="<HOME>/sandbox-rl-performance/agent-sandbox/drivers/verify_cluster.sh"
mkdir -p "$LOG_DIR" "$BENCH_DIR/runs"

echo "[$(date -u +%FT%TZ)] === 1. Configuring VPC Private Google Access, Cloud Router & 8-IP Cloud NAT ==="
gcloud compute networks subnets update default --region="$REGION" --project="$PROJECT" --enable-private-ip-google-access --quiet || true

NAT_IPS=()
for n in 1 2 3 4 5 6 7 8; do
  IP_NAME="asrl-c3-nat-ip-${n}"
  if ! gcloud compute addresses describe "$IP_NAME" --region="$REGION" --project="$PROJECT" >/dev/null 2>&1; then
    gcloud compute addresses create "$IP_NAME" --region="$REGION" --project="$PROJECT" --quiet
  fi
  NAT_IPS+=("$IP_NAME")
done
NAT_IP_POOL=$(IFS=,; echo "${NAT_IPS[*]}")

if ! gcloud compute routers describe asrl-c3-router --region="$REGION" --project="$PROJECT" >/dev/null 2>&1; then
  gcloud compute routers create asrl-c3-router \
    --network=default \
    --region="$REGION" \
    --project="$PROJECT" \
    --quiet
fi

if ! gcloud compute routers nats describe asrl-c3-nat --router=asrl-c3-router --region="$REGION" --project="$PROJECT" >/dev/null 2>&1; then
  gcloud compute routers nats create asrl-c3-nat \
    --router=asrl-c3-router \
    --region="$REGION" \
    --project="$PROJECT" \
    --nat-all-subnet-ip-ranges \
    --nat-external-ip-pool="$NAT_IP_POOL" \
    --enable-dynamic-port-allocation \
    --min-ports-per-vm=4096 \
    --max-ports-per-vm=32768 \
    --tcp-established-idle-timeout=1200 \
    --quiet
else
  gcloud compute routers nats update asrl-c3-nat \
    --router=asrl-c3-router \
    --region="$REGION" \
    --project="$PROJECT" \
    --nat-all-subnet-ip-ranges \
    --nat-external-ip-pool="$NAT_IP_POOL" \
    --enable-dynamic-port-allocation \
    --min-ports-per-vm=4096 \
    --max-ports-per-vm=32768 \
    --tcp-established-idle-timeout=1200 \
    --quiet
fi
echo "[$(date -u +%FT%TZ)] Cloud NAT asrl-c3-nat ready with 8 static external IPs ($NAT_IP_POOL)."

wait_preemptible_headroom() {
  local max_allowed="$1"
  for _q in $(seq 1 180); do
    local used
    used=$(gcloud compute regions describe "$REGION" --project="$PROJECT" --format=json | python3 -c '
import json, sys
d = json.load(sys.stdin)
for q in d.get("quotas", []):
    if q.get("metric") == "PREEMPTIBLE_CPUS":
        print(int(float(q.get("usage", 0))))
        break
')
    if [ "${used:-9999}" -le "$max_allowed" ]; then
      echo "[$(date -u +%FT%TZ)] PREEMPTIBLE_CPUS usage=${used} <= ${max_allowed}; proceeding!"
      return 0
    fi
    echo "[$(date -u +%FT%TZ)] Waiting for PREEMPTIBLE_CPUS (${used} > ${max_allowed})..."
    sleep 15
  done
  echo "ERROR: Timed out waiting for PREEMPTIBLE_CPUS headroom!" >&2
  return 1
}

prov_one() {
  local idx="$1"
  local wait_spot="${2:-0}"
  local CLUSTER="asrl-c3-${idx}"
  local MASTER_CIDR="172.16.${idx}.0/28"
  local CTX="gke_${PROJECT}_${REGION}_${CLUSTER}"
  export KUBECONFIG="$LOG_DIR/kubeconfig_c${idx}.yaml"

  if [ "$wait_spot" -eq 1 ]; then
    wait_preemptible_headroom 3800
  fi

  echo "[$(date -u +%FT%TZ)] [$CLUSTER] Creating GKE cluster born at n1-standard-32 KCP / gomaxprocs=16 (170x3=510 Spot warmup nodes)..."
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
    for _w in $(seq 1 80); do
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

  # Bootstrap right away to verify gomaxprocs >= 16 and delete Spot default-pool immediately (freeing PREEMPTIBLE_CPUS for c5!)
  echo "[$(date -u +%FT%TZ)] [$CLUSTER] Bootstrapping (verifying gomaxprocs=16, deleting Spot default-pool, installing CRDs/controller-p3/APF)..."
  bash "$BENCH_DIR/bootstrap_cluster_c3.sh" "$CLUSTER"

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
      --secondary-boot-disk=disk-image=projects/<GCP_PROJECT>/global/images/swebench-500-preload-600g,mode=CONTAINER_IMAGE_CACHE \
      --max-pods-per-node=256 \
      --sandbox=type=gvisor \
      --enable-image-streaming \
      --scopes=cloud-platform \
      --quiet
  fi

  kubectl --context "$CTX" wait --for=condition=Ready nodes -l cloud.google.com/gke-nodepool=gvisor-pool --timeout=300s
  local CNT
  CNT=$(kubectl --context "$CTX" get nodes -l cloud.google.com/gke-nodepool=gvisor-pool --no-headers | wc -l | tr -d ' ')
  echo "[$(date -u +%FT%TZ)] [$CLUSTER] SBD gvisor-pool Ready nodes=$CNT"
  test "$CNT" -eq 10
}

PIDS=()
for idx in 1 2 3 4; do
  prov_one "$idx" 0 > "$LOG_DIR/asrl-c3-${idx}.log" 2>&1 &
  PIDS+=("$!")
  sleep 8
done

# Give c1..c4 90s so GCE quota reflects their 4,080 PREEMPTIBLE_CPUS before c5 polls quota
sleep 90
prov_one 5 1 > "$LOG_DIR/asrl-c3-5.log" 2>&1 &
PIDS+=("$!")

echo "[$(date -u +%FT%TZ)] Waiting for provisioning of asrl-c3-1..5 (PIDs: ${PIDS[*]})..."
FAIL=0
for i in 0 1 2 3 4; do
  cidx=$((i + 1))
  if wait "${PIDS[$i]}"; then
    echo "[$(date -u +%FT%TZ)] asrl-c3-${cidx} PROVISIONED SUCCESSFULLY"
  else
    echo "ERROR: asrl-c3-${cidx} provisioning failed! Tail of log:" >&2
    tail -n 50 "$LOG_DIR/asrl-c3-${cidx}.log" >&2
    FAIL=1
  fi
done

if [ "$FAIL" -ne 0 ]; then
  exit 1
fi

export KUBECONFIG="$BENCH_DIR/kubeconfig_all5.yaml"
rm -f "$KUBECONFIG"
for idx in 1 2 3 4 5; do
  gcloud container clusters get-credentials "asrl-c3-${idx}" --region="$REGION" --project="$PROJECT"
done

echo "[$(date -u +%FT%TZ)] === Running 8-Point Preflight Verification Across All 5 Clusters ==="
for idx in 1 2 3 4 5; do
  CLUSTER="asrl-c3-${idx}"
  CTX="gke_${PROJECT}_${REGION}_${CLUSTER}"
  echo "--- Verifying $CLUSTER ($CTX) ---"
  CTX="$CTX" NS="agent-sandbox-rl" POOL="gvisor-pool" GCLOUD_PROJECT="$PROJECT" GCLOUD_LOCATION="$REGION" GCLOUD_CLUSTER="$CLUSTER" \
    bash "$VERIFY_SH" | tee "$LOG_DIR/verify_${CLUSTER}.txt"
done

echo "[$(date -u +%FT%TZ)] ALL 5 CLUSTERS (asrl-c3-1..5) READY WITH 8-IP CLOUD NAT, gomaxprocs >= 16, 0 SPOT VMS, AND 50 SBD NODES!"
