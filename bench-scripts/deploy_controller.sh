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

# Deploys agent-sandbox-controller to the GKE benchmark cluster and pins it to
# the dedicated tainted `controller-pool`.
#
# Profiles:
#   stock     — out-of-the-box controller flags (--leader-elect=true --extensions)
#   config-h  — upstream/main docs/performance-tuning.md Config H high-throughput profile
#
# Usage:
#   PROJECT=my-proj bench-scripts/deploy_controller.sh --profile stock
#   PROJECT=my-proj bench-scripts/deploy_controller.sh --profile config-h
#   PROJECT=my-proj bench-scripts/deploy_controller.sh --controller-args="..."

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PROJECT="${PROJECT:?Set PROJECT (e.g. brianknguyen-gke-dev)}"
REGION="${REGION:-us-central1}"
ZONE="${ZONE:-us-central1-a}"
CLUSTER="${CLUSTER:-rl-gke-bench}"
AR="${AR:-${REGION}-docker.pkg.dev/${PROJECT}/rl-gke-bench}"
KCTX="${KCTX:-gke_${PROJECT}_${ZONE}_${CLUSTER}}"
CTRL_TAG="${CTRL_TAG:-gkebench-2f27a948}"

PROFILE=""
EXTRA_ARGS=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --profile)
      PROFILE="$2"
      shift 2
      ;;
    *)
      EXTRA_ARGS+=("$1")
      shift
      ;;
  esac
done

case "$PROFILE" in
  ""|stock)
    ;;
  config-h)
    EXTRA_ARGS+=(
      "--controller-args=--api-connections=4 --separate-watch-connection=true --sandbox-concurrent-workers=200 --sandbox-claim-concurrent-workers=200 --sandbox-warm-pool-concurrent-workers=2 --sandbox-warm-pool-replenish-delay=0 --sandbox-warm-pool-max-refill-rate=100 --cache-label-selectors=true --disable-sandbox-events=true --disable-claim-events=true --disable-claim-observability-annotations=true"
    )
    ;;
  *)
    echo "Unknown --profile '$PROFILE' (expected 'stock' or 'config-h')" >&2
    exit 2
    ;;
esac

kubectl config use-context "$KCTX" >/dev/null
cd "$REPO_ROOT"
python3 dev/tools/deploy-to-kube --image-prefix="${AR}/" --image-tag="${CTRL_TAG}" --extensions "${EXTRA_ARGS[@]}"
kubectl --context "$KCTX" -n agent-sandbox-system rollout status deploy/agent-sandbox-controller --timeout=300s
kubectl --context "$KCTX" -n agent-sandbox-system patch deploy agent-sandbox-controller --type strategic -p \
  '{"spec":{"template":{"spec":{"nodeSelector":{"cloud.google.com/gke-nodepool":"controller-pool"},"tolerations":[{"key":"controller-pool","operator":"Equal","value":"true","effect":"NoSchedule"}]}}}}'
kubectl --context "$KCTX" -n agent-sandbox-system rollout status deploy/agent-sandbox-controller --timeout=300s
kubectl --context "$KCTX" get crd | grep agents.x-k8s.io
kubectl --context "$KCTX" -n agent-sandbox-system get deploy agent-sandbox-controller \
  -o jsonpath='{.spec.template.spec.containers[0].image} {.spec.template.spec.containers[0].args}{"\n"}'
kubectl --context "$KCTX" -n agent-sandbox-system get pod -o wide
