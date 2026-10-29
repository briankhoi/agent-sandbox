# Standardized GKE RL Benchmark Plan, Tunix Integration Audit & Image Strategy

This document synthesizes the benchmark configurations, workload recipes, Tunix distributed RL architecture (`google/tunix` @ `atwigg/agent-sandbox-direct-apis`), and MLPerf dataset (`hfilaretov/Benchmark-R2E-Gym-Easy`) so our next benchmark runs produce **absolute, apples-to-apples numbers** directly comparable to the RL performance suite and scale recipes.

> [!IMPORTANT]
> **Scope, Runtime & Apples-to-Apples Rules**
> * **We are NOT benchmarking 1 million (`1M`) sandboxes.** Every benchmark below runs on **a single GKE cluster (`~1,000–1,200` warm sandboxes max, `10 × e2-standard-32` or `12–24 × n2-standard-8`)** and finishes end-to-end in **`~6–12 hours` continuous** (well inside a `1–2 day` budget). We reuse the 1M/SWERL campaign's **single-cluster controller recipe and `claim-bench.py` harness** (`2,000`-claim burst + `20,000`-claim endurance soak over a standing ~1K warm pool), not a 4,500-node fleet.
> * **Sync with `upstream/main` & keep A/B runs strictly apples-to-apples:** Always reference `kubernetes-sigs/agent-sandbox@main` for upstream scripts/docs (including [`examples/multi-cluster-fleet/demo/claim-bench.py`](https://github.com/kubernetes-sigs/agent-sandbox/blob/main/examples/multi-cluster-fleet/demo/claim-bench.py)), and run **both** `per_claim` and `batch` arms against the **exact same `upstream/main`-synced controller binary, cluster profile, and SDK baseline** so the only variable between arms is `per_claim` vs. `SandboxBatch`.

---

## 1. What We Found Across the Existing Benchmark Recipes

All existing RL sandbox benchmarks run on **Managed GKE Standard (`rapid` channel, `1.36.x`, `COS_CONTAINERD`, `--sandbox=type=gvisor`, `--enable-image-streaming`)** and fall into **two distinct benchmark families**:

### Family A — Task-Throughput & Synchronous RL Step-Loop Suite (`sandbox-rl-performance`)
* **Cluster (`T0 Portable` — Stock Managed GKE 1.36):**
  * **Sandbox node pool:** `10 × e2-standard-32` (`32 vCPU / 128 GB`, `--max-pods-per-node=256`, `600 GB pd-ssd`, `gvisor`, `--enable-image-streaming`).
  * **Controller + Driver node pool:** Dedicated tainted pool hosting `agent-sandbox-controller` **and** an in-cluster Kubernetes `Job` driver pod (`4–6 vCPU`, `8–12 Gi` RAM) so client latency is never laptop/WAN-bound.
  * *(Note: `T1 Full Stack` adds pre-baked Secondary Boot Disk caches and experimental node daemon swaps for cold image layer fetches; `T0 Portable` is the standard reproducible baseline for control-plane & SDK comparisons.)*
* **Controller Flags (patched directly onto `Deployment` container `args`):**
  * Controller `v0.5.4+` / `v1.0.x` (includes `#1266` warm-pool over-creation fix and `#1852` `readyReplicas` status fix):
  * `--sandbox-concurrent-workers=1000`
  * `--sandbox-claim-concurrent-workers=1000`
  * `--sandbox-warm-pool-concurrent-workers=50` *(canonical recipe; `500` also tested in `T0` rows)*
  * `--kube-api-qps=500` `--kube-api-burst=1000`
* **Per-Sandbox Pod Spec:** `cpu=125m`, `memory=1Gi`, `runtimeClassName: gvisor`.
* **Driver Scripts (Available & Ready to Run Directly):**
  1. **`run_step_bench.py` (Synchronous RL Step-Loop):**
     * Runs **`500` images × `G=2` rollouts (`1,000` tasks/step) × `8` synchronous steps** (`8,000` total claims in a single driver process, with a hard barrier between steps), `concurrency=300` or `500`, executing a real git + compute probe (`cd /testbed && git status >/dev/null && git log --oneline -5 >/dev/null && sleep 30`).
     * **Headline metric:** **`env_ready_all_s`** (step start → **last** task's sandbox acquired = identical to our `All Ready` / `ready_last` metric!) + `step_wall_s`.
  2. **`run_basic_swebench_3000.py` (Canonical 3,000-Task Strategy Sweep):**
     * Runs **`500` images × `6` rollouts = `3,000` tasks**, `concurrency=500`, shallow `max_warmpool_size=1`, `probe="true"`, sweeping `naive`, `sliding`, `pipelined` (± `--recycle`).
     * **Published `T0` no-op baselines (`wp=500`):** `pipelined = 507.4s` (`5.91 tasks/s`), `naive = 587.7s` (`5.10 tasks/s`), `sliding = 603.8s` (`4.97 tasks/s`).

---

### Family B — Standing-Fleet Claim Throughput & Endurance Suite (`M2` / `M2.5` / `SWERL` Recipes)
* **Cluster & Controller Profile (`v1.0.x` / `upstream/main`):**
  * Sandbox nodes: `n2-standard-8` (`--max-pods-per-node=256`, `500 GB pd-balanced`, `gvisor`, `--enable-image-streaming`, `--enable-private-nodes`).
  * Controller (`16Gi` request / `20Gi` limit on dedicated `controller-pool`, **control plane verified `>= 64` `gomaxprocs` cores**):
    ```text
    --leader-elect=true
    --extensions
    --sandbox-concurrent-workers=400
    --sandbox-claim-concurrent-workers=150
    --sandbox-warm-pool-concurrent-workers=50
    --sandbox-warm-pool-max-batch-size=300
    --sandbox-warm-pool-readiness-grace-period=15m
    --api-connections=4
    --separate-watch-connection=true
    --cache-label-selectors=true
    --disable-claim-events=true
    --disable-claim-observability-annotations=true
    ```
    *(Uses stock GKE APF on single-tenant clusters with `>= 64`-core control planes, and `TemplateSpec(network_policy_management="Unmanaged")`.)*
* **Workload ([`examples/multi-cluster-fleet/demo/claim-bench.py`](https://github.com/kubernetes-sigs/agent-sandbox/blob/main/examples/multi-cluster-fleet/demo/claim-bench.py) against Standing Warm Pools):**
  * Uses `FleetSandboxClient` ([`examples/multi-cluster-fleet/python/agent_sandbox_fleet/resolver.py`](https://github.com/kubernetes-sigs/agent-sandbox/blob/main/examples/multi-cluster-fleet/python/agent_sandbox_fleet/resolver.py)) calling `SandboxClient.create_sandbox(template=...)` followed by `delete_sandbox(name)` across `args.concurrency` worker threads, sizing `connection_pool_maxsize = 2 * concurrency + 16` (`create_span` measures `t0` → last create completion separately from delete tail).
  * **2,000-claim burst (`--claims 2000 --concurrency 100 --pool-maxsize 400`):** Published `per_claim` steady-state baseline is **`82 claims/s`**, **`p50 = 0.62s`**, **`p99 = 0.93s`** (after stripping a `45.9s` first-burst client credential warmup on worker indices `0–99`).
  * **20,000-claim endurance soak — Documented `per_claim` Failure:**
    * Raising concurrency `100 → 200` on `per_claim` yielded the **same `82 claims/s` while doubling `p50` (`0.53s → 1.07s`)** (bottlenecked in the SDK/claims layer, not nodes).
    * Worse, `per_claim` hit a **hard ceiling of `~1,500–2,500` claims per client process**: a `20,000`-claim soak **cannot complete** because `K8sHelper._watch_claim` opens a fresh per-claim watch stream (`w = watch.Watch()`) on every `create_sandbox`, churning sockets/watches until the client degrades into connection timeouts.
    * **Verified on `upstream/main` (`b1277676`):** `clients/python/agentic-sandbox-client/k8s_agent_sandbox/k8s_helper.py` (`_watch_claim`) still opens one `watch.Watch()` HTTP stream per claim with no shared watch or batching at `upstream/main`. **`SandboxBatch` (`claim_mode="batch"`) directly fixes this exact defect.**

---

## 2. What Images the Recipes Use vs. `Benchmark-R2E-Gym-Easy`

Do the recipes mandate a specific image set, or do we need to build from `hfilaretov/Benchmark-R2E-Gym-Easy`? **Both are already unified in regional Artifact Registry:**

| Image Set | Unique Images | Image Format | Where Used | Artifact Registry Availability |
|---|---:|---|---|---|
| **SWE-bench Verified 500** (`swebench500.txt`) | `500` | `sweb.eval.x86_64.<repo>-<id>@sha256:...` (`~2–4 GB` each) | `run_basic_swebench_3000.py`, `run_step_bench.py` (`job-swe500-rl.yaml`), `200K` single-cluster recipe | Already mirrored in regional AR |
| **R2E-Gym Subset 500 / Verified 4,578** (`r2e_subset_500.txt`, `r2e_subset_verified_4578.txt`) | `500` / `4,578` | `namanjain12/<repo>_final:<commit_sha>` (`~2.2 GiB` on disk) | `run_step_bench.py` (`job-b0-r2e-step.yaml`, `job-r2e-*.yaml`) & `SWERL` corpus | Already mirrored in regional AR (`us-central1`) |
| **MLPerf v6.0 DeepSWE (`hfilaretov/Benchmark-R2E-Gym-Easy`)** | **`700` train** / **`251` val** | `namanjain12/<repo>_final:<commit_sha>` across 8 Python repos (`pillow`, `scrapy`, `datalad`, `tornado`, `orange3`, `pyramid`, `bokeh`, `numpy`) | **Official `google/tunix` MLPerf RL training (`700`) & eval (`251`) dataset** | **100% (`700/700`) of the `train` images are an exact subset of `r2e_subset_verified_4578.txt` in regional AR!** |

**Why this is ideal:**
1. Unlike `python:3.12-slim` (`1` tiny `~50 MB` image), `Benchmark-R2E-Gym-Easy` exercises **`500–700` distinct multi-GB repository images** (`500–700` `SandboxTemplate`s + `SandboxWarmPool`s, real GKE Image Streaming `gcfsd` layer fetches, and real `/testbed` git repositories).
2. Because **all `700/700` `Benchmark-R2E-Gym-Easy` images already exist in the regional `us-central1` Artifact Registry mirror**, we avoid Docker Hub `429` rate limits and get full GKE Image Streaming compatibility immediately (or can `crane copy` those `700` manifests server-side into our own project's AR repo in minutes if cross-project IAM isn't granted).

---

## 3. Recommended Benchmark Suite, Doc Tab Mapping & Expected Impact

We recommend running **their exact scripts** (synced with `upstream/main`, adding only a `--claim-mode {per_claim,batch}` switch to `bench_core.py` / `run_step_bench.py` and `claim-bench.py`) across **3 single-cluster scenarios (~6–12h total runtime)**:

| Priority | Scenario & Exact Script | Maps to Benchmark Doc Tabs | Cluster & Controller Profile | Workload & Images | What We Expect & How It Helps Our Case |
|---|---|---|---|---|---|
| **P0 (Primary RL Headline)** | **Synchronous 8-Step RL Loop**<br>`run_step_bench.py`<br>(`per_claim` vs `batch`) | • `Weekly Summary`<br>• `Sandeep: Agent Sandbox Benchmarking Evaluation`<br>• `Tomer: Swe-Bench500`<br>• `Chenyi: Recipe - e2e reproduction` | **`T0 Portable`** (`10 × e2-standard-32` + in-cluster driver Job) **AND** Family B controller flags (`400/150/50`, `api-connections=4`, `separate-watch-connection=true`) | **Arm 1 (Standard):** `500` R2E-Gym-Easy (or SWE-500) images × `G=2` (`1,000`/step) × `8` steps (`8,000` claims), `c=500`, warm pools `R=2`, real `/testbed` git probe + `30s` hold.<br>**Arm 2 (Tunix MLPerf GBS=1024):** `64` R2E-Gym-Easy images × `G=16` (`1,024`/step) × `8` steps (`8,192` claims), `c=1024`, warm pools `R=16`. | • Directly measures **`env_ready_all_s` (`All Ready` step barrier)** on pre-warmed pools across 8 steps (`8,000+` cumulative claims in one process).<br>• Crosses `per_claim`'s `~1,500–2,500` claim TCP/watch degradation cliff around Step 2–3 while `batch` holds **1 watch + 20 sockets** across all 8 steps.<br>• Cuts driver K8s API load **`3.2×–4.0×`** and watches **`1,000×`**. |
| **P0 (Direct Defect Closure)** | **Standing-Fleet Claim Burst + 20K Endurance Soak**<br>[`examples/multi-cluster-fleet/demo/claim-bench.py`](https://github.com/kubernetes-sigs/agent-sandbox/blob/main/examples/multi-cluster-fleet/demo/claim-bench.py)<br>(`per_claim` vs `batch`) | • `Vicente: M2` → `M2 Findings`<br>• `Vicente: M2.5` → `M2.5 Findings`, `M2.5 Recipe`<br>• `Vicente: SWERL` → `SWERL Findings`, `Numbers`, `SWERL Recipe` (§11 Claim leg), `RECIPE`, `ISSUES` (#3) | **Family B Recipe** (`sbx=400`, `claim=150`, `wp=50`, `wp-batch=300`, `grace=15m`, `api-connections=4`, `separate-watch-connection=true`, `disable-claim-events=true`, `disable-claim-observability-annotations=true`) | Standing warm pools (`~1,000` ready sandboxes on R2E-Gym-Easy on 1 cluster):<br>1. **Concurrency sweep:** `2,000` claims at `c = 100, 200, 500`.<br>2. **Endurance soak:** `20,000` claims in 1 process (`c = 100..200`, delete-on-bind recycling the standing pool). | • Produces an **exact apples-to-apples number** against the published **`82 claims/s`, `p50=0.62s`, `p99=0.93s`** baseline.<br>• **Proves `batch` scales past `c=100` without doubling `p50`** and **completes the 20,000-claim soak with `0` errors** where `per_claim` fails completely (closing `ISSUES` #3). |
| **P1 (Leaderboard Sanity Check)** | **Canonical `500 × 6 = 3,000` Sweep**<br>`run_basic_swebench_3000.py`<br>(`per_claim` vs `batch`) | • `Sandeep: Agent Sandbox Benchmarking Evaluation`<br>• `Tomer: Swe-Bench500`<br>• `Chenyi: Recipe - e2e reproduction`<br>• `Tomer: Warmpool Concurrency Issues`<br>• `Tomer: 500*6 SweBenchEval Progress` | **`T0 Portable`** (`10 × e2-standard-32`, `1000/50` & `1000/500` workers) | `500` SWE-500 & R2E-Gym-Easy images × `6` = `3,000` tasks, `c=500`, shallow `max_warmpool_size=1`, `probe="true"`, `naive` / `sliding` / `pipelined`. | • Gives a row directly comparable to `RESULTS.md` (`507s–604s`).<br>• *Honest expectation:* Because `max_warmpool_size=1` cold-boots `2,500` replacement gVisor pods mid-run, wall time is mostly node-pull bound, so `batch` mainly protects the `3,000`-claim driver from watch/socket exhaustion and lowers apiserver CPU/memory. |

---

## 4. Tunix (`google/tunix` @ `atwigg/agent-sandbox-direct-apis`) Architectural Verdict

We audited `examples/deepswe/{sandbox_utils.py, swe_env.py, template.py}`, `tunix/experimental/examples/deepswe_dist/{run_deepswe_dist.py, deepswe.py, eval_deepswe.py, eval_worker.py}`, `run_rollout_node.py`, and `mlperf_gbs1024_submission.sh` (`commit 61ff63b9`):

1. **Why `FleetConfig(claim_mode="batch")` + per-episode `fleet.acquire(task)` IS the ideal architecture (vs. upfront `claim_batch`):**
   * **Split Orchestrator / RolloutWorker topology:** Tunix's central CPU orchestrator (`run_deepswe_dist.py`) **never claims sandboxes** — it only runs `PrewarmDatasetIterator` to manage `SandboxWarmPool`s (`R = G = 16`), and dispatches individual trajectory requests over gRPC to **`16–128` distributed TPU `RolloutWorker` pods** (`run_rollout_node.py`). Each `RolloutWorker` runs in-process TPU vLLM alongside up to `256` concurrent `SWEEnv` episodes, calling `_GLOBAL_FLEET.acquire(task)` in `SWEEnv.reset()` and `_GLOBAL_FLEET.release(handle)` in `SWEEnv.close()`.
   * **Asynchronous off-policy streaming (`MAX_STALENESS=1..2` + `175s` initial stagger):** Multi-turn trajectories (`1–50` bash turns) start and finish continuously out-of-order (`pop_next_completed()`), not at synchronous barriers. Claiming upfront on the orchestrator would hold idle sandboxes while requests queue for vLLM KV-cache slots and require cross-pod handle/Lease serialization.
   * **Per-worker `SandboxBatch` gives Tunix every batch benefit with zero env API changes:** Flipping `claim_mode="batch"` inside `sandbox_utils.init_global_fleet()` gives each `RolloutWorker` **1 shared watch + 1 Lease + pooled connections** across the entire training run (preventing the ~2,000-claim socket cliff across steps, cutting `1,024–2,048` cluster watches down to `16–128`, and automatically garbage-collecting ONLY a crashed TPU worker's claims via Lease `ownerReferences` without disturbing shared pools!).
2. **Why SDK-level `min_ready` quorum is NOT needed for Tunix:**
   * Tunix **already** implements full-episode group quorum in `StandardRLProgram` (`--num_generations_to_dispatch > --num_generations` + `--prefer_valid_rollouts` + `FT_SANDBOX_READY_TIMEOUT_S=600`). When `G=16` trajectories finish, the orchestrator cancels remaining stragglers via `RolloutWorker.cancel_by_prompt_id()` (whether stuck in sandbox claim *or* turn 28 of LLM generation), which calls `SWEEnv.close()` → `fleet.release()`.
3. **Code/Config Follow-Ups for Tunix Integration:** Documented side-by-side with the GKE controller tuning fix in [`GKE_APF_WARMPOOL_DEEP_DIVE.md` §6](GKE_APF_WARMPOOL_DEEP_DIVE.md#6-companion-code--config-fixes-discovered-during-the-distributed-rl-tunix-audit).

---

## 5. Pre-Run Checks & Questions Before Spinning Up the Cluster

* **Already Verified Directly on `upstream/main` (`b1277676`) — No Need to Ask:**
  * **Per-claim watch ceiling (`ISSUES` #3):** Confirmed in `clients/python/agentic-sandbox-client/k8s_agent_sandbox/k8s_helper.py` (`_watch_claim`) that `upstream/main` still opens a dedicated `watch.Watch()` stream per claim with no shared watch or batching.
  * **`claim-bench.py` harness:** Located on `upstream/main` at [`examples/multi-cluster-fleet/demo/claim-bench.py`](https://github.com/kubernetes-sigs/agent-sandbox/blob/main/examples/multi-cluster-fleet/demo/claim-bench.py), which drives `FleetSandboxClient` → `SandboxClient.create_sandbox(template=...)` + `delete_sandbox(name)` and works cleanly against a 1-cluster `assignments.json`.

* **3 Remaining Questions to Confirm with Vicente Before Launching:**
  1. **Control-Plane Sizing (`>= 64` `gomaxprocs` cores) on a Small Single Cluster:**
     * For an apples-to-apples comparison against the `82 claims/s` (`p50=0.62s, p99=0.93s`) `claim-bench.py` baseline without spinning up an 850-node cluster, how was the `64`-core GKE `kube-apiserver` (`go_sched_gomaxprocs_threads >= 64`) provisioned (temporary initial node count scale-up/scale-down, regional control plane, or standard auto-sized control plane), and what node count is recommended for a single-cluster `2,000` / `20,000` `claim-bench.py` run?
  2. **Driver Placement & First-Burst Auth Warmup in `claim-bench.py`:**
     * Was `claim-bench.py` run from an external GCE VM (with worker indices `0–99` stripped post-hoc to exclude the `45.9s` `gke-gcloud-auth-plugin` warmup) or from an in-cluster Pod, and should we pre-warm `1` dummy claim before starting the timer so raw `claim-bench.py` output needs no post-hoc slicing?
  3. **Shared Artifact Registry Read Access vs. `crane copy`:**
     * Can our benchmark project's GKE node service account be granted `roles/artifactregistry.reader` on the shared `us-central1` SWE-bench / R2E-Gym Artifact Registry mirrors (which already hold all `500` SWE-bench and `700` `Benchmark-R2E-Gym-Easy` images), or should we `crane copy` the `700` `Benchmark-R2E-Gym-Easy` images into our own project's regional Artifact Registry?
