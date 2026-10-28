#!/usr/bin/env bash
# Copyright 2026 The Kubernetes Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# Lifecycle helper for the RL GKE scale benchmark cluster (`rl-gke-bench`):
#   create-cluster   — creates the GKE Standard cluster + controller/sandbox/driver node pools
#   tag-images [N]   — ensures python:3.12-slim is pushed as g000..g(N-1) in Artifact Registry
#   setup-driver     — creates bench-driver namespace, 20Gi PVC, RBAC, and rl-bench-driver pod
#   resize-sandbox N — resizes the gVisor `sandbox` node pool (e.g. 24 for w1280, 48 for w2560)
#   teardown-cluster — deletes bench-driver PVC, waits for PV release, deletes GKE cluster
#                      (leaves Artifact Registry repo intact by default; pass --delete-ar to delete)

set -euo pipefail

PROJECT="${PROJECT:?Set PROJECT (e.g. brianknguyen-gke-dev)}"
REGION="${REGION:-us-central1}"
ZONE="${ZONE:-us-central1-a}"
CLUSTER="${CLUSTER:-rl-gke-bench}"
AR="${AR:-${REGION}-docker.pkg.dev/${PROJECT}/rl-gke-bench}"
KCTX="${KCTX:-gke_${PROJECT}_${ZONE}_${CLUSTER}}"
SANDBOX_NODES="${SANDBOX_NODES:-24}"
BRANCH="${BRANCH:-bench/rl-gke}"
GIT_URL="${GIT_URL:-https://github.com/briankhoi/agent-sandbox.git}"

cmd="${1:-}"
shift || true

case "$cmd" in
  create-cluster)
    gcloud container clusters create "$CLUSTER" \
      --project="$PROJECT" --zone="$ZONE" --release-channel=rapid \
      --enable-ip-alias --enable-dataplane-v2 --addons=NodeLocalDNS \
      --cluster-ipv4-cidr=/17 --workload-pool="${PROJECT}.svc.id.goog" \
      --scopes=cloud-platform --num-nodes=2 --machine-type=e2-standard-4 \
      --image-type=cos_containerd --disk-type=pd-balanced --disk-size=50 \
      --labels=purpose=rl-gke-bench

    START_TS="$(date -u +%FT%TZ)"
    END_TS="$(date -u -d '+48 hours' +%FT%TZ)"
    gcloud container clusters update "$CLUSTER" --project="$PROJECT" --zone="$ZONE" \
      --add-maintenance-exclusion-name=rl-gke-bench \
      --add-maintenance-exclusion-start="$START_TS" \
      --add-maintenance-exclusion-end="$END_TS" \
      --add-maintenance-exclusion-scope=no_upgrades

    gcloud container node-pools create controller-pool \
      --project="$PROJECT" --cluster="$CLUSTER" --zone="$ZONE" \
      --num-nodes=1 --machine-type=e2-standard-8 --image-type=cos_containerd \
      --disk-type=pd-balanced --disk-size=50 \
      --node-taints=controller-pool=true:NoSchedule \
      --node-labels=purpose=rl-gke-bench

    gcloud container node-pools create sandbox \
      --project="$PROJECT" --cluster="$CLUSTER" --zone="$ZONE" \
      --num-nodes="$SANDBOX_NODES" --machine-type=n2-standard-8 \
      --image-type=cos_containerd --sandbox=type=gvisor --enable-image-streaming \
      --max-pods-per-node=128 --disk-type=pd-balanced --disk-size=50 \
      --node-labels=purpose=rl-gke-bench

    gcloud container node-pools create driver \
      --project="$PROJECT" --cluster="$CLUSTER" --zone="$ZONE" \
      --num-nodes=1 --machine-type=n2-standard-8 --image-type=cos_containerd \
      --disk-type=pd-balanced --disk-size=50 \
      --node-taints=dedicated=driver:NoSchedule \
      --node-labels=purpose=rl-gke-bench,role=driver

    gcloud container clusters update "$CLUSTER" --project="$PROJECT" --zone="$ZONE" --enable-dns-access
    gcloud container clusters get-credentials "$CLUSTER" --project="$PROJECT" --zone="$ZONE" --dns-endpoint

    PROJ_NUM="$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')"
    gcloud artifacts repositories add-iam-policy-binding rl-gke-bench \
      --project="$PROJECT" --location="$REGION" \
      --member="serviceAccount:${PROJ_NUM}-compute@developer.gserviceaccount.com" \
      --role="roles/artifactregistry.reader" >/dev/null
    ;;

  tag-images)
    TOTAL_TAGS="${1:-320}"
    if ! gcloud artifacts repositories describe rl-gke-bench --project="$PROJECT" --location="$REGION" >/dev/null 2>&1; then
      gcloud artifacts repositories create rl-gke-bench \
        --project="$PROJECT" --location="$REGION" --repository-format=docker \
        --labels=purpose=rl-gke-bench
    fi
    gcloud auth configure-docker "${REGION}-docker.pkg.dev" --quiet
    DIGEST="$(gcloud artifacts docker tags list "${AR}/rl-task" --filter='tag:g000' --format='value(version)' 2>/dev/null | head -1 || true)"
    if [[ -z "$DIGEST" ]]; then
      docker pull --platform linux/amd64 python:3.12-slim
      docker tag python:3.12-slim "${AR}/rl-task:g000"
      docker push "${AR}/rl-task:g000"
      DIGEST="$(gcloud artifacts docker tags list "${AR}/rl-task" --filter='tag:g000' --format='value(version)' | head -1)"
    fi
    seq 1 $((TOTAL_TAGS - 1)) | xargs -P 16 -I{} bash -c '
      tag=$(printf "g%03d" "$1")
      gcloud artifacts docker tags add "'"${AR}"'/rl-task@'"${DIGEST}"'" "'"${AR}"'/rl-task:$tag" --quiet >/dev/null 2>&1
    ' _ {}
    echo "Verified $(gcloud artifacts docker tags list "${AR}/rl-task" --format='value(tag)' | grep -E '^g[0-9]{3}$' | wc -l) tags on ${DIGEST}"
    ;;

  setup-driver)
    kubectl --context "$KCTX" apply -f - <<'EOF'
apiVersion: v1
kind: Namespace
metadata:
  name: bench-driver
---
apiVersion: v1
kind: Namespace
metadata:
  name: rl-loadtest
---
apiVersion: v1
kind: ServiceAccount
metadata:
  name: driver
  namespace: bench-driver
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: rl-bench-driver
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: cluster-admin
subjects:
- kind: ServiceAccount
  name: driver
  namespace: bench-driver
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: work
  namespace: bench-driver
spec:
  accessModes: [ReadWriteOnce]
  resources:
    requests:
      storage: 20Gi
---
apiVersion: v1
kind: Pod
metadata:
  name: rl-bench-driver
  namespace: bench-driver
spec:
  serviceAccountName: driver
  nodeSelector:
    role: driver
  tolerations:
  - key: dedicated
    operator: Equal
    value: driver
    effect: NoSchedule
  containers:
  - name: driver
    image: python:3.12
    command: ["sleep", "infinity"]
    volumeMounts:
    - name: work
      mountPath: /work
  volumes:
  - name: work
    persistentVolumeClaim:
      claimName: work
EOF
    kubectl --context "$KCTX" -n bench-driver wait --for=condition=Ready pod/rl-bench-driver --timeout=300s
    kubectl --context "$KCTX" -n bench-driver exec rl-bench-driver -- bash -euo pipefail -c "
      mkdir -p /work/logs
      if [[ ! -d /work/repo/.git ]]; then
        git clone '$GIT_URL' /work/repo
      fi
      git -C /work/repo fetch origin '$BRANCH' --tags
      git -C /work/repo checkout -B '$BRANCH' 'origin/$BRANCH'
      if [[ ! -x /work/venv/bin/python ]]; then
        python3 -m venv /work/venv
      fi
      /work/venv/bin/pip install --upgrade pip -q
      /work/venv/bin/pip install -e '/work/repo/clients/python/agentic-sandbox-client[test]' -e '/work/repo/examples/agent-sandbox-rl[dev]' -q
      cd /work/repo/examples/agent-sandbox-rl && /work/venv/bin/pytest tests/test_gke_scale_bench.py tests/test_claim_mode_bench.py tests/test_loadtest.py -q
    "
    ;;

  resize-sandbox)
    NODES="${1:?Specify target node count (e.g. 24 or 48)}"
    gcloud container node-pools resize sandbox --project="$PROJECT" --cluster="$CLUSTER" --zone="$ZONE" --num-nodes="$NODES" --quiet
    ;;

  teardown-cluster)
    DELETE_AR="${1:-}"
    PV_HANDLES="$(kubectl --context "$KCTX" get pv -o jsonpath='{.items[*].spec.csi.volumeHandle}' 2>/dev/null || true)"
    echo "Recorded PV volumeHandles: ${PV_HANDLES:-<none>}"
    kubectl --context "$KCTX" delete ns bench-driver --ignore-not-found --wait=true
    for _ in $(seq 1 30); do
      [[ -z "$(kubectl --context "$KCTX" get pv --no-headers 2>/dev/null || true)" ]] && break
      sleep 2
    done
    gcloud container clusters delete "$CLUSTER" --project="$PROJECT" --zone="$ZONE" --quiet
    if [[ "$DELETE_AR" == "--delete-ar" ]]; then
      gcloud artifacts repositories delete rl-gke-bench --project="$PROJECT" --location="$REGION" --quiet
    fi
    echo "=== Post-teardown verification ==="
    gcloud container clusters list --project="$PROJECT"
    gcloud compute instances list --project="$PROJECT" --filter="labels.purpose=rl-gke-bench"
    gcloud compute disks list --project="$PROJECT" --filter="-users:*"
    gcloud artifacts repositories list --project="$PROJECT" --location="$REGION"
    ;;

  *)
    echo "Usage: $0 {create-cluster|tag-images [N]|setup-driver|resize-sandbox N|teardown-cluster [--delete-ar]}" >&2
    exit 2
    ;;
esac
