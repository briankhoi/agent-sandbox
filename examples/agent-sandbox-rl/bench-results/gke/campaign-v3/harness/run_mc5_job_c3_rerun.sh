#!/usr/bin/env bash
# Execute a 5-cluster Campaign 3 smoke gate or C3-3d benchmark (`asrl-c3-1..5`) using:
#   - Mandatory zero-object drain across all 5 clusters + full `SETTLE_S=30` settle before EVERY launch
#   - Exact pinned Campaign 3 driver image (`asrl-driver@sha256:6d0b37df...` from §2C)
#   - ConfigMap mount of `run_mc5_sdk_batch_bench_c3_rerun.py` -> `/app/drivers/run_mc5_sdk_batch_bench.py`
#     (so zero Artifact Registry push is needed and exact image parity is preserved)
#   - Live per-arm raw JSON extraction (`===ARM_RAW_JSON_BEGIN:...===`) to disk while the Job runs
#   - Redacted `<GCP_PROJECT>` in any repo-copied `.meta.json`
set -euo pipefail

export PATH="$HOME/google-cloud-sdk/bin:/google/google-cloud-sdk/bin:/usr/lib/google-cloud-sdk/bin:$PATH"
export CLOUDSDK_CORE_PROJECT="<GCP_PROJECT>"

RUN_NAME="${1:?usage: run_mc5_job_c3_rerun.sh <run_name> <driver_cmd...>}"
shift
DRIVER_CMD="$*"

PROJECT="<GCP_PROJECT>"
REGION="us-central1"
BENCH_DIR="<SCRATCH_DIR>/gke_bench_v3"
export KUBECONFIG="$BENCH_DIR/kubeconfig_all5.yaml"
RUNS_DIR="$BENCH_DIR/runs"
mkdir -p "$RUNS_DIR"

NS="agent-sandbox-rl"
CTRL_NS="agent-sandbox-system"
SETTLE_S="${SETTLE_S:-30}"
if [ "$SETTLE_S" -lt 30 ]; then
  echo "ERROR: Mandatory Campaign 3 guardrail requires SETTLE_S >= 30 (got $SETTLE_S)!" >&2
  exit 1
fi
DRIVER_IMAGE="${DRIVER_IMAGE:-us-central1-docker.pkg.dev/<GCP_PROJECT>/rl-gke-bench/asrl-driver@sha256:6d0b37df8c80b092186d5c8ee66d13e646283d48a686301d6ece6c3532f2a8c3}"
JOB_NAME="asrl-$(echo "$RUN_NAME" | tr '_' '-' | tr '[:upper:]' '[:lower:]' | cut -c1-50)"
HOST_CTX="gke_${PROJECT}_${REGION}_asrl-c3-1"

echo "=================================================================="
echo "[$(date -u +%FT%TZ)] C3 5-CLUSTER PRE-FLIGHT: $RUN_NAME (job=$JOB_NAME, settle=${SETTLE_S}s)"
echo "IMAGE: $DRIVER_IMAGE"
echo "CMD:   $DRIVER_CMD"
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
  kubectl --context "$ctx" -n "$NS" delete pod -l app=mc5-sdk-bench --grace-period=0 --force --ignore-not-found >/dev/null 2>&1 || true
}

for idx in 1 2 3 4 5; do
  CTX_I="gke_${PROJECT}_${REGION}_asrl-c3-${idx}"
  # Verify gomaxprocs >= 16 and 10 Ready gvisor-pool nodes on every cluster before launching
  GMP=$({ kubectl --context "$CTX_I" get --raw /metrics 2>/dev/null || true; } | awk '/^go_sched_gomaxprocs_threads /{v=int($2)} END{print v+0}')
  GV_NODES=$({ kubectl --context "$CTX_I" get nodes -l cloud.google.com/gke-nodepool=gvisor-pool --no-headers 2>/dev/null || true; } | awk '$2 == "Ready" {c++} END{print c+0}')
  if [ "$GMP" -lt 16 ] || [ "$GV_NODES" -lt 10 ]; then
    echo "ERROR: [c${idx}] preflight failed: gomaxprocs=${GMP} (need >=16), ready gvisor-pool nodes=${GV_NODES} (need >=10)!" >&2
    exit 1
  fi

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
      echo "  [c${idx}] clean (gomaxprocs=${GMP}, gvisor_nodes=${GV_NODES}, 0 pods/claims/warmpools/sandboxes/templates/daemonsets/leases)"
      DRAINED=1
      break
    fi
    echo "  [c${idx} attempt=$attempt/90] draining: pods=$PODS claims=$CLAIMS wps=$WPS sbs=$SBS tpls=$TPLS dss=$DSS leases=$LEASES"
    if [ "$attempt" -eq 2 ] || [ "$attempt" -eq 15 ] || [ "$attempt" -eq 35 ] || [ "$attempt" -eq 60 ]; then
      cleanup_cluster "$CTX_I"
    fi
    sleep 4
  done
  if [ "$DRAINED" -ne 1 ]; then
    echo "ERROR: c${idx} failed to drain!" >&2
    exit 1
  fi
done
echo "  All 5 clusters clean; enforcing full ${SETTLE_S}s settle..."
sleep "$SETTLE_S"

# Build 5-cluster kubeconfig (`c1..c5`) using fresh 8h ServiceAccount tokens
KCFG_FILE="$BENCH_DIR/mc5_kubeconfig_tmp_$$.yaml"
rm -rf "$KCFG_FILE"
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

# Mount `run_mc5_sdk_batch_bench_c3_rerun.py` + `campaign-v3/harness` drivers into `/app/drivers` via ConfigMap
HARNESS_DIR="<HOME>/agent-sandbox/.claude/worktrees/rl-gke/examples/agent-sandbox-rl/bench-results/gke/campaign-v3/harness"
kubectl --context "$HOST_CTX" -n "$NS" create configmap asrl-mc5-c3-3d-script \
  --from-file=run_mc5_sdk_batch_bench.py="$BENCH_DIR/run_mc5_sdk_batch_bench_c3_rerun.py" \
  --from-file=bench_core.py="$HARNESS_DIR/bench_core.py" \
  --from-file=run_claim_soak_bench.py="$HARNESS_DIR/run_claim_soak_bench.py" \
  --from-file=run_smoke_gate.py="$HARNESS_DIR/run_smoke_gate.py" \
  --dry-run=client -o yaml | kubectl --context "$HOST_CTX" apply -f -

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
              cp -f /etc/mc5-script/*.py /app/drivers/
              echo "=== START 5-CLUSTER ${RUN_NAME} \$(date -u +%FT%TZ) nofile=\$(ulimit -n) ==="
              ${DRIVER_CMD} --out /out/result.json 2>/out/driver.stderr.log
              rc=\$?
              echo "=== EXIT rc=\$rc \$(date -u +%FT%TZ) ==="
              if [ -s /out/driver.stderr.log ]; then
                echo "=== STDERR TAIL ==="
                tail -n 120 /out/driver.stderr.log
              fi
              if [ -f /out/result.json ]; then
                echo "===RESULT_JSON_BEGIN==="
                cat /out/result.json
                echo
                echo "===RESULT_JSON_END==="
              elif [ -f /out/result.json.partial ]; then
                echo "===PARTIAL_JSON_BEGIN==="
                cat /out/result.json.partial
                echo
                echo "===PARTIAL_JSON_END==="
              fi
              exit \$rc
          volumeMounts:
            - name: out
              mountPath: /out
            - name: kubeconfig
              mountPath: /etc/mc5-kube
              readOnly: true
            - name: script
              mountPath: /etc/mc5-script
              readOnly: true
      volumes:
        - name: out
          emptyDir: {}
        - name: kubeconfig
          secret:
            secretName: asrl-mc5-kubeconfig
        - name: script
          configMap:
            name: asrl-mc5-c3-3d-script
EOF

extract_arm_checkpoints() {
  local log_file="$1"
  python3 - "$log_file" "$RUNS_DIR" "$RUN_NAME" <<'PY'
import json, os, re, sys
log_file, runs_dir, run_name = sys.argv[1:4]
if not os.path.isfile(log_file):
    sys.exit(0)
txt = open(log_file, encoding="utf-8", errors="replace").read()
pat = re.compile(r"===ARM_RAW_JSON_BEGIN:([A-Za-z0-9_]+)===\n(.*?)\n===ARM_RAW_JSON_END:\1===", re.S)
for m in pat.finditer(txt):
    tag, raw = m.group(1), m.group(2).strip()
    dest = os.path.join(runs_dir, f"{run_name}.arm_{tag}.json")
    if not os.path.isfile(dest):
        obj = json.loads(raw)
        with open(dest, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, indent=2)
            fh.write("\n")
        print(f"  [SAVED ARM CHECKPOINT] -> {dest} ({len(obj.get('runs', []))} records)")
PY
}

echo "Waiting for 5-cluster Job ${JOB_NAME} to complete..."
while true; do
  POD_NAME=$(kubectl --context "$HOST_CTX" -n "$NS" get pods -l "job-name=${JOB_NAME}" -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || echo "")
  if [ -n "$POD_NAME" ]; then
    kubectl --context "$HOST_CTX" -n "$NS" logs "$POD_NAME" > "$RUNS_DIR/${RUN_NAME}.pod.log.live" 2>/dev/null || true
    mv -f "$RUNS_DIR/${RUN_NAME}.pod.log.live" "$RUNS_DIR/${RUN_NAME}.pod.log" 2>/dev/null || true
    extract_arm_checkpoints "$RUNS_DIR/${RUN_NAME}.pod.log" || true
    LAST_LINE=$(grep -v '^===' "$RUNS_DIR/${RUN_NAME}.pod.log" 2>/dev/null | grep -v '^{' | tail -n 2 | tr '\n' ' ' || echo "")
    echo "[$(date -u +%H:%M:%S)] ${RUN_NAME}: ${LAST_LINE:0:200}"
  fi
  SUCCEEDED=$(kubectl --context "$HOST_CTX" -n "$NS" get job "$JOB_NAME" -o jsonpath='{.status.succeeded}' 2>/dev/null || echo "")
  FAILED=$(kubectl --context "$HOST_CTX" -n "$NS" get job "$JOB_NAME" -o jsonpath='{.status.failed}' 2>/dev/null || echo "")
  if [ "${SUCCEEDED:-0}" -ge 1 ] || [ "${FAILED:-0}" -ge 1 ]; then
    break
  fi
  sleep 15
done

END_UTC="$(date -u +%FT%TZ)"
END_EPOCH="$(date +%s)"
ELAPSED=$((END_EPOCH - START_EPOCH))

POD_NAME=$(kubectl --context "$HOST_CTX" -n "$NS" get pods -l "job-name=${JOB_NAME}" -o jsonpath='{.items[0].metadata.name}')
kubectl --context "$HOST_CTX" -n "$NS" logs "$POD_NAME" > "$RUNS_DIR/${RUN_NAME}.pod.log"
extract_arm_checkpoints "$RUNS_DIR/${RUN_NAME}.pod.log" || true

python3 - "$RUNS_DIR/${RUN_NAME}.pod.log" "$RUNS_DIR/${RUN_NAME}.json" <<'PY'
import sys
log_path, out_path = sys.argv[1], sys.argv[2]
txt = open(log_path, encoding="utf-8", errors="replace").read()
beg = "===RESULT_JSON_BEGIN==="
end = "===RESULT_JSON_END==="
if beg in txt and end in txt:
    body = txt.split(beg, 1)[1].split(end, 1)[0].strip()
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(body + "\n")
    print(f"Saved final JSON -> {out_path} ({len(body)} bytes)")
else:
    print("ERROR: No RESULT_JSON markers found in pod log!", file=sys.stderr)
    sys.exit(1)
PY

{
  for idx in 1 2 3 4 5; do
    echo "=== c${idx} ==="
    kubectl --context "gke_${PROJECT}_${REGION}_asrl-c3-${idx}" get --raw /metrics 2>/dev/null \
      | grep -E '^apiserver_flowcontrol_rejected_requests_total|^apiserver_flowcontrol_dispatched_requests_total|^go_sched_gomaxprocs_threads ' || true
  done
} > "$RUNS_DIR/${RUN_NAME}.apf_after.txt"

CTRL_AFTER=0
for idx in 1 2 3 4 5; do
  R=$(kubectl --context "gke_${PROJECT}_${REGION}_asrl-c3-${idx}" -n "$CTRL_NS" get pods -o json 2>/dev/null \
    | python3 -c 'import json,sys; d=json.load(sys.stdin); print(sum(cs.get("restartCount",0) for it in d.get("items",[]) for cs in it.get("status",{}).get("containerStatuses",[])))' 2>/dev/null || echo 0)
  CTRL_AFTER=$((CTRL_AFTER + R))
done

REDACTED_IMAGE=$(echo "$DRIVER_IMAGE" | sed 's/<GCP_PROJECT>/<GCP_PROJECT>/g')
cat > "$RUNS_DIR/${RUN_NAME}.meta.json" <<EOF
{
  "run_name": "${RUN_NAME}",
  "mode": "5_cluster_driver_job",
  "clusters": ["asrl-c3-1", "asrl-c3-2", "asrl-c3-3", "asrl-c3-4", "asrl-c3-5"],
  "job_name": "${JOB_NAME}",
  "driver_image": "${REDACTED_IMAGE}",
  "driver_cmd": $(python3 -c 'import json,sys; print(json.dumps(sys.argv[1]))' "$DRIVER_CMD"),
  "settle_s": ${SETTLE_S},
  "start_utc": "${START_UTC}",
  "end_utc": "${END_UTC}",
  "job_elapsed_s": ${ELAPSED},
  "job_succeeded": ${SUCCEEDED:-0},
  "job_failed": ${FAILED:-0},
  "controller_restarts_5c_before": ${CTRL_BEFORE},
  "controller_restarts_5c_after": ${CTRL_AFTER}
}
EOF

kubectl --context "$HOST_CTX" -n "$NS" delete job "$JOB_NAME" --ignore-not-found --wait=true >/dev/null 2>&1 || true
kubectl --context "$HOST_CTX" -n "$NS" delete secret asrl-mc5-kubeconfig --ignore-not-found >/dev/null 2>&1 || true

echo "[$(date -u +%FT%TZ)] COMPLETED 5-CLUSTER RUN: $RUN_NAME in ${ELAPSED}s (succeeded=${SUCCEEDED:-0} failed=${FAILED:-0} ctrl_restarts_5c=${CTRL_BEFORE}->${CTRL_AFTER})"
if [ "${FAILED:-0}" -ge 1 ]; then
  echo "ERROR: Job ${JOB_NAME} failed! Tail of pod log:"
  tail -n 80 "$RUNS_DIR/${RUN_NAME}.pod.log"
  exit 1
fi
