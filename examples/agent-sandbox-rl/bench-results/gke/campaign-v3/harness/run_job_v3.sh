#!/usr/bin/env bash
# Execute a single Campaign 3 IN-CLUSTER benchmark or smoke gate cleanly with:
#  - Strict 7-resource zero-drain check + settle
#  - APF + controller restart snapshots before & after
#  - Structured JSON + pod log saved immediately to BOTH scratch and campaign-v3/{raw,smoke,telemetry}
set -euo pipefail

export PATH="$HOME/google-cloud-sdk/bin:/google/google-cloud-sdk/bin:/usr/lib/google-cloud-sdk/bin:$PATH"
export CLOUDSDK_CORE_PROJECT="<gcp-project>"

RUN_NAME="${1:?usage: run_job_v3.sh <run_name> <driver_cmd...>}"
shift
DRIVER_CMD="$*"

BENCH_DIR="${BENCH_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
RUNS_DIR="$BENCH_DIR/runs"
REPO_C3="${REPO_C3:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
mkdir -p "$RUNS_DIR" "$REPO_C3/raw" "$REPO_C3/smoke" "$REPO_C3/telemetry"

CTX="${CTX:-gke_<gcp-project>_us-central1_asrl-c3-1}"
NS="agent-sandbox-rl"
CTRL_NS="agent-sandbox-system"
SETTLE_S="${SETTLE_S:-30}"
IS_SMOKE="${IS_SMOKE:-0}"
DRIVER_IMAGE="${DRIVER_IMAGE:?DRIVER_IMAGE must be set to @sha256-pinned image}"
JOB_NAME="asrl-$(echo "$RUN_NAME" | tr '_' '-' | tr '[:upper:]' '[:lower:]' | cut -c1-50)"

K() { kubectl --context "$CTX" "$@"; }

echo "=================================================================="
echo "[$(date -u +%FT%TZ)] C3 IN-CLUSTER PRE-FLIGHT: $RUN_NAME (ctx=$CTX job=$JOB_NAME)"
echo "CMD: $DRIVER_CMD"
echo "=================================================================="

K -n "$NS" delete job -l app=asrl-driver --ignore-not-found --wait=true >/dev/null 2>&1 || true

cleanup_leftovers() {
  K -n "$NS" delete daemonset --all --ignore-not-found --wait=false >/dev/null 2>&1 || true
  K -n "$NS" delete sandboxclaim --all --ignore-not-found --wait=false >/dev/null 2>&1 || true
  K -n "$NS" delete sandboxwarmpool --all --ignore-not-found --wait=false >/dev/null 2>&1 || true
  K -n "$NS" delete sandbox --all --ignore-not-found --wait=false >/dev/null 2>&1 || true
  K -n "$NS" delete sandboxtemplate --all --ignore-not-found --wait=false >/dev/null 2>&1 || true
  K -n "$NS" delete lease -l agents.x-k8s.io/batch-id --ignore-not-found >/dev/null 2>&1 || true
  K -n "$NS" delete pod -l app=agent-sandbox-rl --grace-period=0 --force --ignore-not-found >/dev/null 2>&1 || true
}

DRAINED=0
for attempt in $(seq 1 90); do
  PODS=$({ K -n "$NS" get pods --no-headers 2>/dev/null || true; } | grep -vc '^asrl-' || true)
  CLAIMS=$({ K -n "$NS" get sandboxclaims --no-headers 2>/dev/null || true; } | wc -l | tr -d ' ')
  WPS=$({ K -n "$NS" get sandboxwarmpools --no-headers 2>/dev/null || true; } | wc -l | tr -d ' ')
  SBS=$({ K -n "$NS" get sandboxes --no-headers 2>/dev/null || true; } | wc -l | tr -d ' ')
  TPLS=$({ K -n "$NS" get sandboxtemplates --no-headers 2>/dev/null || true; } | wc -l | tr -d ' ')
  DSS=$({ K -n "$NS" get daemonsets --no-headers 2>/dev/null || true; } | wc -l | tr -d ' ')
  LEASES=$({ K -n "$NS" get leases -l agents.x-k8s.io/batch-id --no-headers 2>/dev/null || true; } | wc -l | tr -d ' ')
  if [ "$PODS" -eq 0 ] && [ "$CLAIMS" -eq 0 ] && [ "$WPS" -eq 0 ] && [ "$SBS" -eq 0 ] && [ "$TPLS" -eq 0 ] && [ "$DSS" -eq 0 ] && [ "$LEASES" -eq 0 ]; then
    echo "  OK pre-run namespace clean (0 pods/claims/warmpools/sandboxes/templates/daemonsets/leases); settling ${SETTLE_S}s..."
    DRAINED=1
    sleep "$SETTLE_S"
    break
  fi
  echo "  waiting for namespace drain (attempt=$attempt/90: pods=$PODS claims=$CLAIMS wps=$WPS sbs=$SBS tpls=$TPLS dss=$DSS leases=$LEASES)..."
  if [ "$attempt" -eq 5 ] || [ "$attempt" -eq 15 ] || [ "$attempt" -eq 35 ] || [ "$attempt" -eq 55 ] || [ "$attempt" -eq 75 ]; then
    cleanup_leftovers
  fi
  sleep 4
done
if [ "$DRAINED" -ne 1 ]; then
  echo "ERROR: namespace failed to drain after 90 attempts!" >&2
  exit 1
fi

apf_snapshot() {
  K get --raw /metrics 2>/dev/null | grep -E '^apiserver_flowcontrol_rejected_requests_total|^apiserver_flowcontrol_dispatched_requests_total|^apiserver_flowcontrol_current_inqueue_requests|^go_sched_gomaxprocs_threads ' || true
}
ctrl_snapshot() {
  K -n "$CTRL_NS" get pods -o json 2>/dev/null || echo "{}"
}

apf_snapshot > "$RUNS_DIR/${RUN_NAME}.apf_before.txt"
ctrl_snapshot > "$RUNS_DIR/${RUN_NAME}.ctrl_before.json"
START_UTC="$(date -u +%FT%TZ)"
START_EPOCH="$(date +%s)"

cat <<EOF | K apply -f -
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
          resources:
            requests:
              cpu: "8"
              memory: "16Gi"
            limits:
              cpu: "14"
              memory: "28Gi"
          command: ["bash", "-lc"]
          args:
            - |
              set -o pipefail
              ulimit -n 65535 2>/dev/null || true
              mkdir -p /out
              echo "=== START ${RUN_NAME} \$(date -u +%FT%TZ) nofile=\$(ulimit -n) ==="
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
      volumes:
        - name: out
          emptyDir: {}
EOF

echo "Waiting for Job ${JOB_NAME} to complete..."
while true; do
  SUCCEEDED=$(K -n "$NS" get job "$JOB_NAME" -o jsonpath='{.status.succeeded}' 2>/dev/null || echo "")
  FAILED=$(K -n "$NS" get job "$JOB_NAME" -o jsonpath='{.status.failed}' 2>/dev/null || echo "")
  if [ "${SUCCEEDED:-0}" -ge 1 ] || [ "${FAILED:-0}" -ge 1 ]; then
    break
  fi
  POD_NAME=$(K -n "$NS" get pods -l "job-name=${JOB_NAME}" -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || echo "")
  if [ -n "$POD_NAME" ]; then
    LAST_LINE=$(K -n "$NS" logs "$POD_NAME" --tail=2 2>/dev/null | tr '\n' ' ' || echo "")
    echo "[$(date -u +%H:%M:%S)] ${RUN_NAME}: ${LAST_LINE:0:180}"
  fi
  sleep 15
done

END_UTC="$(date -u +%FT%TZ)"
END_EPOCH="$(date +%s)"
ELAPSED=$((END_EPOCH - START_EPOCH))

POD_NAME=$(K -n "$NS" get pods -l "job-name=${JOB_NAME}" -o jsonpath='{.items[0].metadata.name}')
K -n "$NS" logs "$POD_NAME" > "$RUNS_DIR/${RUN_NAME}.pod.log"

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

apf_snapshot > "$RUNS_DIR/${RUN_NAME}.apf_after.txt"
ctrl_snapshot > "$RUNS_DIR/${RUN_NAME}.ctrl_after.json"
cp -f "$RUNS_DIR/${RUN_NAME}.apf_before.txt" "$REPO_C3/telemetry/${RUN_NAME}.apf_before.txt"
cp -f "$RUNS_DIR/${RUN_NAME}.apf_after.txt" "$REPO_C3/telemetry/${RUN_NAME}.apf_after.txt"

CTRL_RESTARTS_BEFORE=$(python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(sum(cs.get("restartCount",0) for it in d.get("items",[]) for cs in it.get("status",{}).get("containerStatuses",[])))' "$RUNS_DIR/${RUN_NAME}.ctrl_before.json" 2>/dev/null || echo 0)
CTRL_RESTARTS_AFTER=$(python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(sum(cs.get("restartCount",0) for it in d.get("items",[]) for cs in it.get("status",{}).get("containerStatuses",[])))' "$RUNS_DIR/${RUN_NAME}.ctrl_after.json" 2>/dev/null || echo 0)

cat > "$RUNS_DIR/${RUN_NAME}.meta.json" <<EOF
{
  "run_name": "${RUN_NAME}",
  "mode": "in_cluster_job",
  "job_name": "${JOB_NAME}",
  "driver_image": "${DRIVER_IMAGE}",
  "driver_cmd": $(python3 -c 'import json,sys; print(json.dumps(sys.argv[1]))' "$DRIVER_CMD"),
  "start_utc": "${START_UTC}",
  "end_utc": "${END_UTC}",
  "job_elapsed_s": ${ELAPSED},
  "job_succeeded": ${SUCCEEDED:-0},
  "job_failed": ${FAILED:-0},
  "controller_restarts_before": ${CTRL_RESTARTS_BEFORE},
  "controller_restarts_after": ${CTRL_RESTARTS_AFTER}
}
EOF
cp -f "$RUNS_DIR/${RUN_NAME}.meta.json" "$REPO_C3/telemetry/${RUN_NAME}.meta.json"

K -n "$NS" delete job "$JOB_NAME" --ignore-not-found --wait=true >/dev/null 2>&1 || true

echo "[$(date -u +%FT%TZ)] COMPLETED RUN: $RUN_NAME in ${ELAPSED}s (succeeded=${SUCCEEDED:-0} failed=${FAILED:-0} ctrl_restarts=${CTRL_RESTARTS_BEFORE}->${CTRL_RESTARTS_AFTER})"
if [ "${FAILED:-0}" -ge 1 ]; then
  echo "ERROR: Job ${JOB_NAME} failed! Tail of pod log:"
  tail -n 60 "$RUNS_DIR/${RUN_NAME}.pod.log"
  exit 1
fi
