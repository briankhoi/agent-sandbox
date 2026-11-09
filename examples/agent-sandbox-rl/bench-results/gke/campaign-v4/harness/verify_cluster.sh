#!/usr/bin/env bash
# Verify (NEVER create) that the target cluster matches the benchmark prerequisites.
# Hard-fails on things that would break a run; warns on spec deviations.
# Exit 0 = ready, 1 = fix required. Provision separately (see README Prerequisites).
set -u
CTX=${CTX:?set your kube context, e.g. gke_PROJECT_REGION_CLUSTER}
NS=${NS:-default}
POOL=${POOL:-gvisor-pool}
CTRL_NS=${CTRL_NS:-agent-sandbox-system}
EXPECT_GKE=${EXPECT_GKE:-}            # optional substring, e.g. v1.36.0-gke.3712000
EXPECT_NODES=${EXPECT_NODES:-10}
EXPECT_MAXPODS=${EXPECT_MAXPODS:-256}
FAIL=0; WARN=0
ok(){   echo "  OK   $*"; }
warn(){ echo "  WARN $*"; WARN=$((WARN+1)); }
err(){  echo "  FAIL $*"; FAIL=$((FAIL+1)); }
K(){ kubectl --context "$CTX" "$@"; }

echo "verify: context=$CTX ns=$NS pool=$POOL  (read-only — creates nothing)"

# 1) context reachable
if K cluster-info >/dev/null 2>&1; then ok "context reachable"; else
  err "context '$CTX' unreachable"; echo; echo "VERIFY FAILED: cannot reach cluster."; exit 1; fi

# 2) server version
SV=$(K version -o json 2>/dev/null | python3 -c 'import json,sys;print(json.load(sys.stdin).get("serverVersion",{}).get("gitVersion","?"))' 2>/dev/null)
if [ -n "$EXPECT_GKE" ]; then
  case "$SV" in *"$EXPECT_GKE"*) ok "GKE $SV";; *) warn "GKE $SV (expected *$EXPECT_GKE*)";; esac
else ok "GKE $SV"; fi

# 3) namespace
K get ns "$NS" >/dev/null 2>&1 && ok "namespace $NS" || err "namespace $NS missing"

# 4) gVisor RuntimeClass
K get runtimeclass gvisor >/dev/null 2>&1 && ok "RuntimeClass gvisor" \
  || err "RuntimeClass gvisor missing (node pool needs --sandbox=type=gvisor)"

# 5) CRDs (controller + extensions)
for c in sandboxes.agents.x-k8s.io \
         sandboxclaims.extensions.agents.x-k8s.io \
         sandboxtemplates.extensions.agents.x-k8s.io \
         sandboxwarmpools.extensions.agents.x-k8s.io; do
  K get crd "$c" >/dev/null 2>&1 && ok "CRD $c" || err "CRD $c missing"
done

# 6) controller present + tuned
ARGS=$(K -n "$CTRL_NS" get deploy agent-sandbox-controller \
  -o jsonpath='{.spec.template.spec.containers[0].args}' 2>/dev/null)
if [ -z "$ARGS" ]; then err "controller deploy not found in ns $CTRL_NS (set CTRL_NS?)"; else
  ok "controller present"
  echo "$ARGS" | grep -q -- "--extensions" && ok "  --extensions enabled" \
    || warn "  --extensions not set (claims/warmpools require it)"
  echo "$ARGS" | grep -qE -- '--sandbox-warm-pool-concurrent-workers=[1-9][0-9]+' \
    && ok "  warm-pool workers tuned" || warn "  warm-pool-concurrent-workers low/default (spec: 50)"
  echo "$ARGS" | grep -qE -- '--sandbox-concurrent-workers=[1-9][0-9]{2,}' \
    && ok "  sandbox workers tuned" || warn "  sandbox-concurrent-workers low/default (spec: 1000)"
fi

# 7) gVisor node pool (machine type / count / maxPods / gvisor label) — read from node objects
NODES=$(K get nodes -l cloud.google.com/gke-nodepool="$POOL" \
  -o jsonpath='{range .items[*]}{.metadata.labels.node\.kubernetes\.io/instance-type}{" "}{.status.capacity.pods}{"\n"}{end}' 2>/dev/null)
NCOUNT=$(printf '%s\n' "$NODES" | grep -c .)
if [ "${NCOUNT:-0}" -eq 0 ]; then err "no nodes in pool '$POOL'"; else
  ok "pool $POOL: $NCOUNT nodes"
  [ -n "$EXPECT_NODES" ] && [ "$NCOUNT" -ne "$EXPECT_NODES" ] && warn "  node count $NCOUNT (expected $EXPECT_NODES)"
  MT=$(printf '%s\n' "$NODES" | awk '{print $1}' | sort -u | paste -sd, -)
  MP=$(printf '%s\n' "$NODES" | awk '{print $2}' | sort -u | paste -sd, -)
  ok "  machine=$MT  maxPods=$MP"
  printf '%s' "$MP" | grep -q "$EXPECT_MAXPODS" || warn "  maxPods=$MP (spec: $EXPECT_MAXPODS)"
  GV=$(K get nodes -l cloud.google.com/gke-nodepool="$POOL",sandbox.gke.io/runtime=gvisor \
    --no-headers 2>/dev/null | grep -c .)
  [ "${GV:-0}" -eq "$NCOUNT" ] && ok "  all $NCOUNT nodes labeled gvisor" \
    || warn "  $GV/$NCOUNT nodes have sandbox.gke.io/runtime=gvisor"
  NR=$(K get nodes -l cloud.google.com/gke-nodepool="$POOL" \
    -o jsonpath='{range .items[*]}{.metadata.name}{" "}{range .status.conditions[?(@.type=="Ready")]}{.status}{end}{" "}{.spec.unschedulable}{"\n"}{end}' 2>/dev/null \
    | awk '$2 != "True" || $3 == "true" {count++} END {print count+0}')
  [ "${NR:-0}" -eq 0 ] && ok "  all $NCOUNT nodes Ready and schedulable" \
    || err "  $NR/$NCOUNT nodes in '$POOL' are NotReady or SchedulingDisabled"
fi

# 8) SBD + image streaming (optional; needs gcloud) — the T0/T1 signal (warn-only).
if command -v gcloud >/dev/null 2>&1; then
  GP=${GCLOUD_PROJECT:-$(printf '%s' "$CTX" | cut -d_ -f2)}
  GL=${GCLOUD_LOCATION:-$(printf '%s' "$CTX" | cut -d_ -f3)}
  GC=${GCLOUD_CLUSTER:-$(printf '%s' "$CTX" | cut -d_ -f4)}
  DESC(){ gcloud container node-pools describe "$POOL" --cluster "$GC" --project "$GP" \
            --location "$GL" --format="value($1)" 2>/dev/null; }
  if gcloud container node-pools describe "$POOL" --cluster "$GC" --project "$GP" \
       --location "$GL" >/dev/null 2>&1; then
    SBD=$(DESC config.secondaryBootDisks); GCFS=$(DESC config.gcfsConfig.enabled)
    { [ -n "$SBD" ] && [ "$SBD" != "None" ]; } \
      && ok "secondary boot disk present (T1 image cache)" \
      || warn "no secondary boot disk on '$POOL' — T0 only (expect cold-pull warm-up)"
    [ "$GCFS" = "True" ] && ok "image streaming ON" \
      || warn "image streaming OFF on '$POOL' (spec: --enable-image-streaming)"
  else
    echo "  SKIP SBD/streaming: gcloud describe failed (set GCLOUD_PROJECT/LOCATION/CLUSTER)"
  fi
else
  echo "  SKIP SBD/streaming: gcloud not on PATH"
fi

# 9) control plane gomaxprocs check (warn if < 16, spec: >= 16 via regional HA or resize)
GOMAX=$(K get --raw /metrics 2>/dev/null | awk '/^go_sched_gomaxprocs_threads/ {print $2; exit}')
if [ -n "$GOMAX" ]; then
  if awk -v g="$GOMAX" 'BEGIN {exit !(g >= 16)}'; then
    ok "apiserver gomaxprocs=$GOMAX (>= 16)"
  else
    warn "apiserver gomaxprocs=$GOMAX (< 16; control plane may bottleneck under high burst)"
  fi
else
  echo "  SKIP apiserver gomaxprocs: /metrics unavailable"
fi

# 10) clean namespace check (warn if leftover workload objects exist in NS)
REM_CLAIMS=$(K -n "$NS" get sandboxclaims --no-headers 2>/dev/null | grep -c .)
REM_WPS=$(K -n "$NS" get sandboxwarmpools --no-headers 2>/dev/null | grep -c .)
REM_SBXS=$(K -n "$NS" get sandboxes --no-headers 2>/dev/null | grep -c .)
REM_PODS=$(K -n "$NS" get pods --no-headers 2>/dev/null | awk '$1 !~ /^asrl-/ {c++} END {print c+0}')
if [ "${REM_CLAIMS:-0}" -eq 0 ] && [ "${REM_WPS:-0}" -eq 0 ] && [ "${REM_SBXS:-0}" -eq 0 ] && [ "${REM_PODS:-0}" -eq 0 ]; then
  ok "namespace $NS clean (0 claims, warmpools, sandboxes, workload pods)"
else
  warn "namespace $NS has leftovers (claims=$REM_CLAIMS warmpools=$REM_WPS sandboxes=$REM_SBXS pods=$REM_PODS)"
fi

echo
if [ "$FAIL" -gt 0 ]; then
  echo "VERIFY FAILED: $FAIL error(s), $WARN warning(s). Fix/provision before running — the benchmark does not create clusters."
  exit 1
else
  echo "VERIFY OK: 0 errors, $WARN warning(s)."
  exit 0
fi
