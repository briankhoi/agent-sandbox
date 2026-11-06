#!/usr/bin/env bash
# Execute a 5-cluster Campaign 3 benchmark or smoke gate (`asrl-c3-1..5`) from an intra-region
# driver Job on `asrl-c3-1` (`default-pool`, `us-central1`, `<0.5ms` RTT to all 5 regional apiservers):
#   1. Drains ALL 5 clusters to 0 pods/claims/warmpools/sandboxes/templates/daemonsets/leases + settle
#   2. Mints fresh 8h ServiceAccount tokens for `asrl-driver` across `c1..c5` into a mounted kubeconfig Secret
#   3. Snapshots APF + controller restarts across all 5 clusters before & after
#   4. Extracts structured JSON + pod logs to scratch and campaign-v3/{raw,smoke,telemetry}
set -euo pipefail

export PATH="$HOME/google-cloud-sdk/bin:/google/google-cloud-sdk/bin:/usr/lib/google-cloud-sdk/bin:$PATH"
export CLOUDSDK_CORE_PROJECT="<gcp-project>"

RUN_NAME="${1:?usage: run_mc5_job_v3.sh <run_name> <driver_cmd...>}"
shift
DRIVER_CMD="$*"

PROJECT="<gcp-project>"
REGION="us-central1"
BENCH_DIR="/usr/local/google/home/brianknguyen/.gemini/jetski/brain/8634985c-d147-49db-bef1-bba51884db9b/scratch/gke_bench_v3"
export KUBECONFIG="$BENCH_DIR/kubeconfig_all5.yaml"
RUNS_DIR="$BENCH_DIR/runs"
REPO_C3="/usr/local/google/home/brianknguyen/agent-sandbox/.claude/worktrees/rl-gke/examples/agent-sandbox-rl/bench-results/gke/campaign-v3"
mkdir -p "$RUNS_DIR" "$REPO_C3/raw" "$REPO_C3/smoke" "$REPO_C3/telemetry"

NS="agent-sandbox-rl"
CTRL_NS="agent-sandbox-system"
SETTLE_S="${SETTLE_S:-30}"
IS_SMOKE="${IS_SMOKE:-0}"
DRIVER_IMAGE="${DRIVER_IMAGE:?DRIVER_IMAGE must be set to @sha256-pinned image}"
JOB_NAME="asrl-$(echo "$RUN_NAME" | tr '_' '-' | tr '[:upper:]' '[:lower:]' | cut -c1-50)"
HOST_CTX="gke_${PROJECT}_${REGION}_asrl-c3-1"

echo "=================================================================="
echo "[$(date -u +%FT%TZ)] C3 5-CLUSTER PRE-FLIGHT: $RUN_NAME (job=$JOB_NAME)"
echo "CMD: $DRIVER_CMD"
echo "=================================================================="

kubectl --context "$HOST_CTX" -n "$NS" delete job -l app=asrl-driver --ignore-not-found --wait=true >/dev/null 2>&1 || true

cleanup_cluster() {
  local ctx="$1"
  kubectl --context "$ctx" -n "$NS" delete daemonset --all --ignore-not-found --wait=false >/dev/null 2>&1 || true
  kubectl --context "$ctx" -n "$NS" delete sandboxclaim --all --ignore-not-found --wait=false >/dev/null 2>&1 || true
  kubectl --context "$ctx" -n "$NS" delete sandboxwarmpool --all --ignore-not-found --wait=false >/dev/null 2>&1 || true
  kubectl --context "$ctx" -n "$NS" delete sandbox --all --ignore-not-found --wait=false >/dev/null 2>&1 || true
  kubectl --context "$ctx" -n "$NS" delete sandboxtemplate --all --ignore-not-found --wait=false >/dev/null 2>&1 || true
  kubectl --context "$ctx" -n "$NS" delete lease -l agents.x-k8s.io/batch-id --ignore-not-found >/dev/null 2>&1 || true
  kubectl --context "$ctx" -n "$NS" delete pod -l app=agent-sandbox-rl --grace-period=0 --force --ignore-not-found >/dev/null 2>&1 || true
}

for idx in 1 2 3 4 5; do
  CTX_I="gke_${PROJECT}_${REGION}_asrl-c3-${idx}"
  DRAINED=0
  for attempt in $(seq 1 90); do
    PODS=$({ kubectl --context "$CTX_I" -n "$NS" get pods --no-headers 2>/dev/null || true; } | grep -vc '^asrl-' || true)
    CLAIMS=$({ kubectl --context "$CTX_I" -n "$NS" get sandboxclaims --no-headers 2>/dev/null || true; } | wc -l | tr -d ' ')
    WPS=$({ kubectl --context "$CTX_I" -n "$NS" get sandboxwarmpools --no-headers 2>/dev/null || true; } | wc -l | tr -d ' ')
    SBS=$({ kubectl --context "$CTX_I" -n "$NS" get sandboxes --no-headers 2>/dev/null || true; } | wc -l | tr -d ' ')
    TPLS=$({ kubectl --context "$CTX_I" -n "$NS" get sandboxtemplates --no-headers 2>/dev/null || true; } | wc -l | tr -d ' ')
    DSS=$({ kubectl --context "$CTX_I" -n "$NS" get daemonsets --no-headers 2>/dev/null || true; } | wc -l | tr -d ' ')
    LEASES=$({ kubectl --context "$CTX_I" -n "$NS" get leases -l agents.x-k8s.io/batch-id --no-headers 2>/dev/null || true; } | wc -l | tr -d ' ')
    if [ "$PODS" -eq 0 ] && [ "$CLAIMS" -eq 0 ] && [ "$WPS" -eq 0 ] && [ "$SBS" -eq 0 ] && [ "$TPLS" -eq 0 ] && [ "$DSS" -eq 0 ] && [ "$LEASES" -eq 0 ]; then
      echo "  [c${idx}] clean (0 pods/claims/warmpools/sandboxes/templates/daemonsets/leases)"
      DRAINED=1
      break
    fi
    echo "  [c${idx} attempt=$attempt/90] draining: pods=$PODS claims=$CLAIMS wps=$WPS sbs=$SBS tpls=$TPLS dss=$DSS leases=$LEASES"
    if [ "$attempt" -eq 3 ] || [ "$attempt" -eq 15 ] || [ "$attempt" -eq 35 ] || [ "$attempt" -eq 60 ]; then
      cleanup_cluster "$CTX_I"
    fi
    sleep 4
  done
  if [ "$DRAINED" -ne 1 ]; then
    echo "ERROR: c${idx} failed to drain!" >&2
    exit 1
  fi
done
echo "  All 5 clusters clean; settling ${SETTLE_S}s..."
sleep "$SETTLE_S"

# Build 5-cluster kubeconfig (`c1..c5`) using fresh 8h ServiceAccount tokens
KCFG_FILE="$BENCH_DIR/mc5_kubeconfig.yaml"
python3 - "$PROJECT" "$REGION" "$NS" "$KCFG_FILE" <<'PY'
import json, subprocess, sys

project, region, ns, out_path = sys.argv[1:5]
clusters_cfg, contexts_cfg, users_cfg = [], [], []

for idx in range(1, 6):
    cname = f"asrl-c3-{idx}"
    alias = f"c{idx}"
    gctx = f"gke_{project}_{region}_{cname}"
    ep = subprocess.check_output([
        "gcloud", "container", "clusters", "describe", cname,
        f"--region={region}", f"--project={project}", "--format=value(endpoint)"
    ], text=True).strip()
    ca = subprocess.check_output([
        "gcloud", "container", "clusters", "describe", cname,
        f"--region={region}", f"--project={project}", "--format=value(masterAuth.clusterCaCertificate)"
    ], text=True).strip()
    tok = subprocess.check_output([
        "kubectl", "--context", gctx, "-n", ns,
        "create", "token", "asrl-driver", "--duration=8h"
    ], text=True).strip()

    server = "https://kubernetes.default.svc" if idx == 1 else f"https://{ep}"
    clusters_cfg.append({
        "name": alias,
        "cluster": {"certificate-authority-data": ca, "server": server},
    })
    users_cfg.append({
        "name": f"user-{alias}",
        "user": {"token": tok},
    })
    contexts_cfg.append({
        "name": alias,
        "context": {"cluster": alias, "user": f"user-{alias}", "namespace": ns},
    })

kcfg = {
    "apiVersion": "v1",
    "kind": "Config",
    "clusters": clusters_cfg,
    "users": users_cfg,
    "contexts": contexts_cfg,
    "current-context": "c1",
}
with open(out_path, "w", encoding="utf-8") as fh:
    json.dump(kcfg, fh, indent=2)
print(f"Generated 5-cluster kubeconfig -> {out_path}")
PY

kubectl --context "$HOST_CTX" -n "$NS" create secret generic asrl-mc5-kubeconfig \
  --from-file=config="$KCFG_FILE" --dry-run=client -o yaml | kubectl --context "$HOST_CTX" apply -f -
rm -f "$KCFG_FILE"

# Snapshot APF + controller restarts across all 5 clusters BEFORE
{
  for idx in 1 2 3 4 5; do
    echo "=== c${idx} ==="
    kubectl --context "gke_${PROJECT}_${REGION}_asrl-c3-${idx}" get --raw /metrics 2>/dev/null \
      | grep -E '^apiserver_flowcontrol_rejected_requests_total|^apiserver_flowcontrol_dispatched_requests_total|^go_sched_gomaxprocs_threads ' || true
  done
} > "$RUNS_DIR/${RUN_NAME}.apf_before.txt"

CTRL_BEFORE=0
for idx in 1 2 3 4 5; do
  R=$(kubectl --context "gke_${PROJECT}_${REGION}_asrl-c3-${idx}" -n "$CTRL_NS" get pods -o json 2>/dev/null \
    | python3 -c 'import json,sys; d=json.load(sys.stdin); print(sum(cs.get("restartCount",0) for it in d.get("items",[]) for cs in it.get("status",{}).get("containerStatuses",[])))' 2>/dev/null || echo 0)
  CTRL_BEFORE=$((CTRL_BEFORE + R))
done

START_UTC="$(date -u +%FT%TZ)"
START_EPOCH="$(date +%s)"

cat <<EOF | kubectl --context "$HOST_CTX" apply -f -
apiVersion: batch/v1
kind: Job
metadata:
  name: ${JOB_NAME}
  namespace: ${NS}
  labels:
    app: asrl-driver
    run: ${JOB_NAME}
spec:
  backoffLimit: 0
  activeDeadlineSeconds: 14400
  template:
    metadata:
      labels:
        app: asrl-driver
        run: ${JOB_NAME}
    spec:
      restartPolicy: Never
      serviceAccountName: asrl-driver
      nodeSelector:
        bench-role: driver
      containers:
        - name: driver
          image: ${DRIVER_IMAGE}
          imagePullPolicy: IfNotPresent
          env:
            - name: KUBECONFIG
              value: /etc/mc5-kube/config
          resources:
            requests:
              cpu: "10"
              memory: "24Gi"
            limits:
              cpu: "15"
              memory: "48Gi"
          command: ["bash", "-lc"]
          args:
            - |
              set -o pipefail
              ulimit -n 65535 2>/dev/null || true
              ulimit -s 1024 2>/dev/null || true
              mkdir -p /out
              echo "=== START 5-CLUSTER ${RUN_NAME} \$(date -u +%FT%TZ) nofile=\$(ulimit -n) ==="
              ${DRIVER_CMD} --out /out/result.json 2>/out/driver.stderr.log
              rc=\$?
              echo "=== EXIT rc=\$rc \$(date -u +%FT%TZ) ==="
              if [ -s /out/driver.stderr.log ]; then
                echo "=== STDERR TAIL ==="
                tail -n 80 /out/driver.stderr.log
              fi
              if [ -f /out/result.json ]; then
                echo "===RESULT_JSON_BEGIN==="
                cat /out/result.json
                echo
                echo "===RESULT_JSON_END==="
              fi
              exit \$rc
          volumeMounts:
            - name: out
              mountPath: /out
            - name: kubeconfig
              mountPath: /etc/mc5-kube
              readOnly: true
      volumes:
        - name: out
          emptyDir: {}
        - name: kubeconfig
          secret:
            secretName: asrl-mc5-kubeconfig
EOF

echo "Waiting for 5-cluster Job ${JOB_NAME} to complete..."
while true; do
  SUCCEEDED=$(kubectl --context "$HOST_CTX" -n "$NS" get job "$JOB_NAME" -o jsonpath='{.status.succeeded}' 2>/dev/null || echo "")
  FAILED=$(kubectl --context "$HOST_CTX" -n "$NS" get job "$JOB_NAME" -o jsonpath='{.status.failed}' 2>/dev/null || echo "")
  if [ "${SUCCEEDED:-0}" -ge 1 ] || [ "${FAILED:-0}" -ge 1 ]; then
    break
  fi
  POD_NAME=$(kubectl --context "$HOST_CTX" -n "$NS" get pods -l "job-name=${JOB_NAME}" -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || echo "")
  if [ -n "$POD_NAME" ]; then
    LAST_LINE=$(kubectl --context "$HOST_CTX" -n "$NS" logs "$POD_NAME" --tail=2 2>/dev/null | tr '\n' ' ' || echo "")
    echo "[$(date -u +%H:%M:%S)] ${RUN_NAME}: ${LAST_LINE:0:180}"
  fi
  sleep 15
done

END_UTC="$(date -u +%FT%TZ)"
END_EPOCH="$(date +%s)"
ELAPSED=$((END_EPOCH - START_EPOCH))

POD_NAME=$(kubectl --context "$HOST_CTX" -n "$NS" get pods -l "job-name=${JOB_NAME}" -o jsonpath='{.items[0].metadata.name}')
kubectl --context "$HOST_CTX" -n "$NS" logs "$POD_NAME" > "$RUNS_DIR/${RUN_NAME}.pod.log"

DEST_JSON_DIR="$REPO_C3/raw"
if [ "$IS_SMOKE" = "1" ]; then
  DEST_JSON_DIR="$REPO_C3/smoke"
fi

python3 - "$RUNS_DIR/${RUN_NAME}.pod.log" "$RUNS_DIR/${RUN_NAME}.json" "$DEST_JSON_DIR/${RUN_NAME}.json" <<'PY'
import shutil, sys
log_path, out_path, repo_path = sys.argv[1], sys.argv[2], sys.argv[3]
txt = open(log_path, encoding="utf-8", errors="replace").read()
beg = "===RESULT_JSON_BEGIN==="
end = "===RESULT_JSON_END==="
if beg in txt and end in txt:
    body = txt.split(beg, 1)[1].split(end, 1)[0].strip()
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(body + "\n")
    shutil.copyfile(out_path, repo_path)
    print(f"Saved JSON -> {out_path} and {repo_path} ({len(body)} bytes)")
else:
    print("WARNING: No RESULT_JSON markers found in pod log!", file=sys.stderr)
PY

{
  for idx in 1 2 3 4 5; do
    echo "=== c${idx} ==="
    kubectl --context "gke_${PROJECT}_${REGION}_asrl-c3-${idx}" get --raw /metrics 2>/dev/null \
      | grep -E '^apiserver_flowcontrol_rejected_requests_total|^apiserver_flowcontrol_dispatched_requests_total|^go_sched_gomaxprocs_threads ' || true
  done
} > "$RUNS_DIR/${RUN_NAME}.apf_after.txt"
cp -f "$RUNS_DIR/${RUN_NAME}.apf_before.txt" "$REPO_C3/telemetry/${RUN_NAME}.apf_before.txt"
cp -f "$RUNS_DIR/${RUN_NAME}.apf_after.txt" "$REPO_C3/telemetry/${RUN_NAME}.apf_after.txt"

CTRL_AFTER=0
for idx in 1 2 3 4 5; do
  R=$(kubectl --context "gke_${PROJECT}_${REGION}_asrl-c3-${idx}" -n "$CTRL_NS" get pods -o json 2>/dev/null \
    | python3 -c 'import json,sys; d=json.load(sys.stdin); print(sum(cs.get("restartCount",0) for it in d.get("items",[]) for cs in it.get("status",{}).get("containerStatuses",[])))' 2>/dev/null || echo 0)
  CTRL_AFTER=$((CTRL_AFTER + R))
done

cat > "$RUNS_DIR/${RUN_NAME}.meta.json" <<EOF
{
  "run_name": "${RUN_NAME}",
  "mode": "5_cluster_driver_job",
  "clusters": ["asrl-c3-1", "asrl-c3-2", "asrl-c3-3", "asrl-c3-4", "asrl-c3-5"],
  "job_name": "${JOB_NAME}",
  "driver_image": "${DRIVER_IMAGE}",
  "driver_cmd": $(python3 -c 'import json,sys; print(json.dumps(sys.argv[1]))' "$DRIVER_CMD"),
  "start_utc": "${START_UTC}",
  "end_utc": "${END_UTC}",
  "job_elapsed_s": ${ELAPSED},
  "job_succeeded": ${SUCCEEDED:-0},
  "job_failed": ${FAILED:-0},
  "controller_restarts_5c_before": ${CTRL_BEFORE},
  "controller_restarts_5c_after": ${CTRL_AFTER}
}
EOF
cp -f "$RUNS_DIR/${RUN_NAME}.meta.json" "$REPO_C3/telemetry/${RUN_NAME}.meta.json"

kubectl --context "$HOST_CTX" -n "$NS" delete job "$JOB_NAME" --ignore-not-found --wait=true >/dev/null 2>&1 || true
kubectl --context "$HOST_CTX" -n "$NS" delete secret asrl-mc5-kubeconfig --ignore-not-found >/dev/null 2>&1 || true

echo "[$(date -u +%FT%TZ)] COMPLETED 5-CLUSTER RUN: $RUN_NAME in ${ELAPSED}s (succeeded=${SUCCEEDED:-0} failed=${FAILED:-0} ctrl_restarts_5c=${CTRL_BEFORE}->${CTRL_AFTER})"
if [ "${FAILED:-0}" -ge 1 ]; then
  echo "ERROR: Job ${JOB_NAME} failed! Tail of pod log:"
  tail -n 60 "$RUNS_DIR/${RUN_NAME}.pod.log"
  exit 1
fi
