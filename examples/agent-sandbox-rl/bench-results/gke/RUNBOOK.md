# GKE RL Scale Benchmark — Reboot, Continuation & Upstream Contribution Runbook

This runbook documents how to spin the exact GKE Standard gVisor + Image Streaming benchmark environment back up in ~15 minutes to test follow-up `examples/agent-sandbox-rl` or `k8s-agent-sandbox` changes against the saved `2026-10-28` baselines, how to tear it down cleanly ($0 overnight compute/disk), and how to package the benchmark harnesses into a clean upstream PR for `kubernetes-sigs/agent-sandbox`.

---

## 1. Saved Artifacts & Where Everything Lives on `bench/rl-gke`

| Path | Purpose |
|---|---|
| `examples/agent-sandbox-rl/tests/gke_scale_bench.py` | Multi-step, multi-size (`160`, `640`, `1280`, `2560`) warm/cold/wave/async harness measuring client Ready/exec latency, driver CPU, socket-pool discards, watches, per-verb driver requests, and kube-apiserver `/metrics` deltas (APF wait, HTTP 429s, claim-path requests). |
| `examples/agent-sandbox-rl/tests/claim_mode_bench.py` | Shared arm runner, urllib3 request/discard/watch instrumentation, and single-step harness (usable on `kind` or GKE). |
| `examples/agent-sandbox-rl/tests/loadtest.py` | Adapted `AsyncSandboxFleet` SWE-bench loadtest with `--claim-mode {per_claim,batch}`, `--batch-claims {unbounded,defaults}`, and `--in-cluster`. |
| `bench-scripts/bootstrap_gke_bench.sh` | One-command GKE cluster creation, server-side AR image tagging (`g000..g639`), in-cluster driver pod + PVC setup, sandbox node-pool resize, and clean teardown. |
| `bench-scripts/deploy_controller.sh` | Deploys `agent-sandbox-controller` pinned to `controller-pool` with `--profile stock` or `--profile config-h` (live `upstream/main` `docs/performance-tuning.md` Config H). |
| `bench-scripts/loadtest_arms.sh` | Interleaved, order-rotated runner for `loadtest.py` across `per_claim`, `batch/unbounded`, and `batch/defaults`. |
| `bench-scripts/gke_gate.py` & `bench-scripts/check_results.py` | Control-plane APF readiness gate (`plan.md` §7) and JSON result integrity checker. |
| `examples/agent-sandbox-rl/bench-results/gke/` | Stock controller results (`summary.md`, `STATUS.md`, `CHEATSHEET.md`, `PROVENANCE.md`, `plan.md`, raw `*.json`). |
| `examples/agent-sandbox-rl/bench-results/gke-tuned/` | Live `upstream/main` Tuned (`Config H`, `wp=2`) controller results (`summary.md`, `STATUS.md`, raw `*.json`). |
| `examples/agent-sandbox-rl/bench-results/gke-tuned-wp100/` | Empirical GKE multi-pool APF finding (`--sandbox-warm-pool-concurrent-workers=100` vs `wp=2`; see `WHY.md`). |
| `examples/agent-sandbox-rl/bench-results/gke-scripts/` | `loadtest` (Stock) and `loadtest-tuned` (`Config H`) JSON + Markdown outputs. |

---

## 2. Rebooting Tomorrow to Benchmark Follow-up RL Changes (~15 min)

> **Artifact Registry note:** `us-central1-docker.pkg.dev/brianknguyen-gke-dev/rl-gke-bench` was intentionally preserved (~150 MB deduplicated `python:3.12-slim` + controller image, ~$0.00/day inside GCP's 0.5 GB free tier), so all `640` task tags (`rl-task:g000..g639`) and `agent-sandbox-controller:gkebench-2f27a948` are already ready to pull with zero rebuild/push time.

### Step 2.1 — Environment & Cluster Creation (~10 min)
```bash
export PROJECT=brianknguyen-gke-dev
export REGION=us-central1
export ZONE=us-central1-a
export CLUSTER=rl-gke-bench
export AR=$REGION-docker.pkg.dev/$PROJECT/rl-gke-bench
export KCTX=gke_${PROJECT}_${ZONE}_${CLUSTER}
export CTRL_TAG=gkebench-2f27a948

# Refresh ADC if needed on cloudtop:
gcloud auth application-default login
gcloud auth application-default set-quota-project "$PROJECT"

# Create 28-node cluster (24 x n2-standard-8 gVisor + Image Streaming; set SANDBOX_NODES=48 for w2560):
SANDBOX_NODES=24 bash bench-scripts/bootstrap_gke_bench.sh create-cluster

# (Only needed if AR repo was ever deleted — otherwise all 640 tags already exist):
# bash bench-scripts/bootstrap_gke_bench.sh tag-images 640
```

### Step 2.2 — Deploy Controller (`stock` or `config-h`) & Driver Pod (~3 min)
```bash
# Deploy Stock controller (or `--profile config-h` for upstream/main Config H):
bash bench-scripts/deploy_controller.sh --profile stock

# Create bench-driver namespace, 20Gi PVC, and rl-bench-driver pod with editable venv:
BRANCH=bench/rl-gke bash bench-scripts/bootstrap_gke_bench.sh setup-driver
```

### Step 2.3 — Check Out Your New RL Branch Inside `rl-bench-driver`
```bash
kubectl --context "$KCTX" -n bench-driver exec rl-bench-driver -- bash -c '
  git -C /work/repo fetch origin <your-new-branch>
  git -C /work/repo checkout -B <your-new-branch> origin/<your-new-branch>
  /work/venv/bin/pytest /work/repo/examples/agent-sandbox-rl/tests -q
'
```
*(Remember to also copy or cherry-pick `examples/agent-sandbox-rl/tests/gke_scale_bench.py`, `claim_mode_bench.py`, and `loadtest.py` onto `<your-new-branch>` if they aren't merged yet so `provenance.dirty` stays `false`!)*

### Step 2.4 — Warm the GKE Control Plane Before Measuring (CRITICAL!)
Fresh GKE Standard zonal clusters start on a small control plane (~122 APF seats; `workload-low` ~53 seats), where `w160` takes ~15s instead of ~2.2s due to APF queueing. Driving one `w1280` warm-up triggers GKE control-plane autosizing (`RESIZE_CLUSTER`) up to ~243–404 APF seats:
```bash
kubectl --context "$KCTX" -n bench-driver exec rl-bench-driver -- bash -c '
  cd /work/repo/examples/agent-sandbox-rl
  export RL_BENCH_IMAGE_REPO='"$AR"'/rl-task RL_BENCH_NODE_POOL=sandbox RL_BENCH_RUNTIME_CLASS=gvisor
  /work/venv/bin/python tests/gke_scale_bench.py --in-cluster setup
  RL_BENCH_STEPS=2 RL_BENCH_RESULTS=bench-results/warmup /work/venv/bin/python tests/gke_scale_bench.py \
    --in-cluster suite --only w1280-per-claim-run1 w1280-batch-unbounded-run1
'
# Wait until no RESIZE_CLUSTER operation is RUNNING and seats stabilize (>= 243, ideally 404):
gcloud container operations list --project="$PROJECT" --zone="$ZONE" --filter="targetLink~$CLUSTER" --limit=5
kubectl --context "$KCTX" get --raw /metrics | awk '/^apiserver_flowcontrol_nominal_limit_seats/{s+=$2} END{print "APF seats:", s}'
```

### Step 2.5 — Run Targeted Comparisons & Tear Down When Done
```bash
kubectl --context "$KCTX" -n bench-driver exec rl-bench-driver -- bash -c '
  cd /work/repo/examples/agent-sandbox-rl
  export RL_BENCH_IMAGE_REPO='"$AR"'/rl-task RL_BENCH_NODE_POOL=sandbox RL_BENCH_RUNTIME_CLASS=gvisor
  export RL_BENCH_STEPS=2 RL_BENCH_RUNS=2 RL_BENCH_RESULTS=bench-results/my-new-run
  setsid nohup /work/venv/bin/python tests/gke_scale_bench.py --in-cluster suite \
    --only w160-per-claim-run w160-batch-unbounded w640-per-claim-run w640-batch-unbounded w1280-per-claim-run w1280-batch-unbounded \
    > /work/my-new-run.log 2>&1 < /dev/null &
'

# Copy results out BEFORE tearing down:
kubectl --context "$KCTX" cp bench-driver/rl-bench-driver:/work/repo/examples/agent-sandbox-rl/bench-results/my-new-run /tmp/my-new-run

# Tear down cluster + PVC (keeps AR image repo; pass --delete-ar when completely finished with all RL benchmarks):
bash bench-scripts/bootstrap_gke_bench.sh teardown-cluster
```

---

## 3. Contributing the Benchmark Harness Upstream (`kubernetes-sigs/agent-sandbox`)

Per repository conventions (`AGENTS.md`: *"Do not create new top-level directories without discussion"*), when opening an upstream PR for the RL benchmark suite:

1. **Core Python harnesses & unit tests (already in their final upstream paths!):**
   - `examples/agent-sandbox-rl/tests/claim_mode_bench.py` + `examples/agent-sandbox-rl/tests/test_claim_mode_bench.py`
   - `examples/agent-sandbox-rl/tests/gke_scale_bench.py` + `examples/agent-sandbox-rl/tests/test_gke_scale_bench.py`
   - `examples/agent-sandbox-rl/tests/loadtest.py` + `examples/agent-sandbox-rl/tests/test_loadtest.py` (`--claim-mode`, `--batch-claims`, `--in-cluster`)
2. **Helper shell scripts (`bench-scripts/` → move under `examples/agent-sandbox-rl/scripts/` or `examples/agent-sandbox-rl/tests/scripts/`):**
   - Move `bench-scripts/{bootstrap_gke_bench.sh,deploy_controller.sh,loadtest_arms.sh,gke_gate.py,check_results.py}` into `examples/agent-sandbox-rl/scripts/` rather than creating a top-level `bench-scripts/` directory in `kubernetes-sigs/agent-sandbox`.
   - Do **not** include raw `bench-results/*.json` payloads in the upstream code PR (link to a gist, issue comment, or summary table in the PR body instead so repo git history stays small).
3. **Pre-PR verification:**
   ```bash
   cd examples/agent-sandbox-rl && pytest -q
   make test-unit && make toc-verify
   ```
