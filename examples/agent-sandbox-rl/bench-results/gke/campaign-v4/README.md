# GKE Campaign 4 (`campaign-v4`) — Symmetric Fairness, `ABABAB` 3-Repeat Medians & Multi-Cluster Scale Plan

> **Current Infrastructure State:** **`$0.00 / hr`** (`0` GKE clusters, `0` GCE VMs, `0` Cloud NATs, `0` persistent disks running; only immutable `@sha256`-pinned controller and driver images + resolved `@sha256` benchmark image manifests are staged in Artifact Registry).
>
> **Companion Directories:** [`manifests/`](./manifests/) · [`harness/`](./harness/) · [`harness/images/`](./harness/images/)

---

## 1. Immutable `@sha256` Provenance (`origin/feat/rl-batch-hardening` @ `bb5c3fbd`)

All Campaign 4 runs pin a single, unified `agent-sandbox` / `k8s-agent-sandbox` / `agent-sandbox-rl` commit (`bb5c3fbd7ec6e94f6d96a7e6a544e60bdaae55ce`) across both the Go controller and the Python SDK/driver container, with **100% `@sha256`-pinned** guest workload manifests:

| Component | Source Commit | Immutable Artifact Registry Reference (`@sha256`-Pinned) |
|---|---|---|
| **Controller (`agent-sandbox-controller`)** | `bb5c3fbd7ec6e94f6d96a7e6a544e60bdaae55ce` | `us-central1-docker.pkg.dev/<GCP_PROJECT>/rl-gke-bench/agent-sandbox-controller@sha256:8d11e685651cfb17e406d3440256dedb8317c219a7fe80a789999290d3161ab1` |
| **Benchmark Driver (`asrl-driver`)** | `bb5c3fbd7ec6e94f6d96a7e6a544e60bdaae55ce` + audited `campaign-v4/harness/` | `us-central1-docker.pkg.dev/<GCP_PROJECT>/rl-gke-bench/asrl-driver@sha256:faa997cf22194fb81ed0833f54b8fa34469e746e6ac32c149f6f19df9441d38c` |
| **`R2E-Gym` Verified Full Set (`4,578` images)** | [`harness/images/r2e_subset_verified_4578_digests.txt`](./harness/images/r2e_subset_verified_4578_digests.txt) | `4,578 / 4,578` `@sha256`-pinned (`us-central1-docker.pkg.dev/gke-ai-eco-dev/primeintswe/...`) |
| **`R2E-Gym` Canonical Subset (`500` images)** | [`harness/images/r2e_subset_500.txt`](./harness/images/r2e_subset_500.txt) · [`r2e_subset_500_upstream_digests.txt`](./harness/images/r2e_subset_500_upstream_digests.txt) | `500 / 500` `@sha256`-pinned (same-region mirror + upstream `gke-ai-eco-dev` parity verified: `0` digest mismatches) |
| **`SWE-bench Verified` (`500` images)** | [`harness/images/swebench500.txt`](./harness/images/swebench500.txt) · [`swebench500_upstream_digests.txt`](./harness/images/swebench500_upstream_digests.txt) | `500 / 500` `@sha256`-pinned (`sweb.eval.x86_64.*@sha256:...`) |
| **`R2E-Gym` Pre-Flight Smoke Set (`20` images)** | [`harness/images/r2e_smoke20.txt`](./harness/images/r2e_smoke20.txt) | `20 / 20` `@sha256`-pinned |

---

## 2. Fairness Protocol & Symmetric Footing Rules

Every methodological asymmetry identified across Campaigns 1–3 is eliminated in Campaign 4 by six hard invariants:

1. **Symmetric Repeat Protocol (`ABABAB` 3-Repeat Medians + Documented Long-Suite Single-Pass Rationale per `results-reviewer.md` Rules #7 & #10):**
   - **Outer `ABABAB` Interleaved 3-Repeat Medians (`A1 → B1 → A2 → B2 → A3 → B3`, where `A = per_claim`, `B = batch64`):** Executed on **`Suite #1`** (warm `T0` SWE-bench `3,000` strategy sweep & `naive+recycle`), **`Suite #3B`** (hydrated `T1` `600 GB` SBD `3,000`), **`Suite #3C`** (hydrated `T1` SBD `30s` real-work `3,000`), **`Suite #4-SWE` Eval** (`500×1` `ABABAB` + `B0`), **`Suite #6`** (active packing density `640 / 1,600 / 2,350` across `r1`, `r2`, `r3`), **`Suite #7B`** (`8×512=4,096` multi-job `golden-small`), and **`Suite #7D`** (`1,000 / 2,500 / 5,000` pure SDK claim & `DELETE_COLLECTION` release). Interleaving cancels any slow drift in `kube-apiserver` watch caches, `etcd` compaction cycles, node page caches, or GCE network weather across time.
   - **In-Pool 3-Repeat Medians (`AAABBB`, `--repeats 3` per standing warm pool with full WarmPool refill + `0`-Terminating gate between bursts):** Executed on **`Suites #5A & #5B`** (`1×1,000` and `200×5` warm `TTFI` curves) and **`Suites #7A & #7C`** (5-cluster `1k/2k/4k` and `8,000`-sandbox standing warm bursts).
   - **Symmetric Single-Pass (`AB`, `n = 1`) on Multi-Hour / Fresh-Pool Suites (`results-reviewer.md` Rule #10 Disclosure):** Executed once per arm on **`Suites #2`, `#3A`, and `#4C`** (`R2E-Gym` `4,578` unique-repo sweep) where every individual arm recreates a brand-new `0%`-cached `10`-node GKE worker pool from scratch, on **`Suites #4A`, `#4B`, and `#4-SWE` RL** where each single run already executes **`8` sequential synchronous RL steps (`1,000–1,024` rollouts/step = `8,000–8,192` tasks)** internally (`Steps 1–7` provide `7` steady-state step samples per run), and on **`Suite #5C`** (`20,000` sequential/wave claims across `100` waves in a single process).
2. **Fresh Worker Node-Pool Recreation Before EVERY Cold Arm (`0%` Cache / `0%` SBD Hydration Symmetry):**
   - For **cold `T0` GCFS (`Suite #2`)** and **cold first-boot `T1` SBD (`Suite #3A`)**, the entire `gvisor-pool` (`10` worker nodes) is `gcloud container node-pools delete`d and freshly `create`d **immediately before each individual arm** (`A` and `B`, plus `B0`).
   - Pre-run smoke validation before cold arms uses `--smoke-image` pointing to the non-task `asrl-driver@sha256:faa997cf...` image so **`0 / 500` task images or lazy GCS SBD blocks** are touched prior to `t0`.
3. **Upfront Untimed `500`-Image Hydration Before All Warm Suites:**
   - Before any warm `T0` (`Suite #1`) or warm `T1` SBD (`Suites #3B, #3C, #4, #5, #6, #7`) benchmark begins, [`run_daemonset_prepull.py`](./harness/run_daemonset_prepull.py) runs **untimed** across `100%` of `gvisor-pool` nodes (across `1` or all `5` clusters concurrently via `--contexts`), hydrating every containerd layer and GCE SBD lazy GCS block, deleting all prepull DaemonSets, and waiting until `0` `role=prepull` pods remain before `A1` starts.
4. **Uniform Controller Worker Concurrency (`Ctrl sbx/wp = 1000/500`) Everywhere:**
   - Both [`manifests/controller-t0-stock.yaml`](./manifests/controller-t0-stock.yaml) (Suites #1–#2: stock client/event flags, **no custom APF**) and [`manifests/controller-t1-tuned.yaml`](./manifests/controller-t1-tuned.yaml) (Suites #3–#7: `--api-connections=4 --separate-watch-connection=true --cache-label-selectors=true --sandbox-warm-pool-max-refill-rate=500` + [`manifests/apf-insulation-gke.yaml`](./manifests/apf-insulation-gke.yaml) 3-lane APF) fix `--sandbox-concurrent-workers=1000`, `--sandbox-claim-concurrent-workers=500`, and `--sandbox-warm-pool-concurrent-workers=500` (`Ctrl sbx/wp = 1000/500`).
5. **Hard Readiness, Zero-Terminating-Pod & Zero-Object Drain Gates:**
   - Warm bursts and soak waves (`run_claim_soak_bench.py`, `run_density_bench.py`, `run_multicluster_bench.py`, `run_mc5_sdk_batch_bench.py`) enforce **both** `min_ready_fraction = 1.0` (`ready >= target`) **and** `terminating_pods == 0`, raising `TimeoutError` on breach instead of firing into a partially refilled or terminating pool.
   - Between every arm/variant, `bench_core.wait_clusters_drained()` verifies `0` non-driver `Pod`s, `0` `Sandbox`es, `0` `SandboxClaim`s, `0` `SandboxWarmPool`s, `0` `SandboxTemplate`s, and `0` `agents.x-k8s.io/batch-id` `Lease`s across all clusters, closes all client HTTP pools, and settles `30s`.
6. **Dual Fleet-Wide (`N ×`) & Per-Cluster (`÷ N`) Reporting on Multi-Cluster Suites:**
   - Every multi-cluster benchmark (`run_multicluster_bench.py` and `run_mc5_sdk_batch_bench.py`) emits both **fleet-wide aggregate rates (`5×`)** and **per-cluster normalized rates (`÷ 5`)** (`per_cluster_claims_per_s`, `per_cluster_release_api_per_s`, `per_cluster_pods_gone_per_s`, `per_cluster_tasks_per_s`) so multi-cluster horizontal scaling is never conflated with single-cluster control-plane throughput.

---

## 3. Campaign 4 Benchmark Matrix (`Suites #1–#7`)

| Suite | Tier, Controller & APF | Worker Pool Topology | Workload & Driver | Footing & Arm Order | Primary Metrics |
|---|---|---|---|---|---|
| **Suite #1 (`C4-1`)**<br>Canonical `T0` Warm SWE-bench `3,000` | `T0` · `controller-t0-stock.yaml` (`1000/500`) · **Stock GKE APF** | `10 × e2-standard-32` (`1,500 GB pd-balanced`, GCFS, `maxPods=256`) | `run_basic_swebench_3000.py`<br>`500 × 6 = 3,000`, `c=500`, `125m/1Gi`, `probe='true'`, `naive,sliding,pipelined` + `naive+recycle` | Upfront `500`-img DaemonSet hydration → **`ABABAB` (`3` reps/arm)** | `wall_s`, `s/task`, paired `TTFI` (`First`/`P50`/`P99`/`Max`), sockets |
| **Suite #2 (`C4-2`)**<br>Truly Cold `0%`-Cached `T0` GCFS + `B0` Raw-Pod | `T0` · `controller-t0-stock.yaml` (`1000/500`) · **Stock GKE APF** | Fresh `10 × e2-standard-32` (`200 GB pd-balanced`, GCFS) **recreated per arm** | `run_basic_swebench_3000.py` (`rollouts=1`, `500×1=500`, `c=500`, `probe='python3 -V'`) + `run_docker_baseline.py` (`B0`) | **Cold `0%` cache** (fresh node pool recreated before each arm: `A → B → B0`, `n=1`/arm) | Cold `wall_s`, `env_ready_ok_s`, paired `TTFI`, `B0` vs `per_claim` vs `batch64` |
| **Suite #3 (`C4-3`)**<br>`T1` `600 GB` SBD Cold First-Boot, Warm Hydrated & Real-Work `30s` | `T1` · `controller-t1-tuned.yaml` (`1000/500`) + `apf-insulation-gke.yaml` | `10 × n2-standard-32` (`100 GB pd-standard` + `600 GB` SBD, `maxPods=256`) | `run_basic_swebench_3000.py`<br>• `3A`: Sym Cold SBD (`naive`)<br>• `3B`: Hydrated SBD (`naive,pipelined`, `probe='true'`)<br>• `3C`: Real-Work (`pipelined`, `git status && git log -n 5 && sleep 30`) | • `3A`: Fresh SBD pool before each arm (`AB`, `n=1`)<br>• `3B` & `3C`: Upfront SBD hydration → **`ABABAB` (`3` reps)** | First-boot GCS hydration delta, hydrated `wall_s`, real-work `s/task` vs `180s` floor |
| **Suite #4 (`C4-4`)**<br>Multi-Step Sync RL Loops (`SWE-500`, `R2E-Gym` `500` & `4,578`, `Tunix GRPO`) | `T1`/Prod · `controller-t1-tuned.yaml` (`1000/500`) + 3-lane APF | `10 × n2-standard-32` (`600 GB` SBD for `SWE-500`; `1,500 GB pd-balanced` GCFS for `R2E-Gym` `500` & `4,578`) | `run_step_bench.py` & `run_hicard_20k_full.py`<br>• `4-SWE`: Eval `500×1` (`3` reps + `B0`) + 8-step RL (`naive`, `pipelined`, `recycle`, `hold`, `B0`)<br>• `4A`: `R2E-Gym` `500×2×8=8,000` (`c=500`, `git+5s`)<br>• `4B`: `Tunix GRPO` `64×16×8=8,192` (`c=1,024`)<br>• `4C`: `R2E-Gym` `4,578`-image windowed sweep (`w=300, c=300`) | • `4-SWE` Eval: **`ABABAB` (`3` reps)** + `B0`<br>• `4-SWE` RL, `4A`, `4B`: `8`-step sync run per arm (`AB`, `n=1`)<br>• `4C`: Fresh `0%` pool before each arm (`AB`, `n=1`) | Sync step barrier `env_ready_all_s` (`P100`), `paired_ttfi_s`, `goodput_ok_per_s`, sockets |
| **Suite #5 (`C4-5`)**<br>Standing `1,000`-Pod Warm `TTFI` Curves & `20k` Soak | `T1` · `controller-t1-tuned.yaml` (`1000/500`) + 3-lane APF | `10 × n2-standard-32` + hydrated `600 GB` SBD | `run_claim_soak_bench.py`<br>• `5A`: `1×1,000` (`c=100,250,500,1000`, `--verify-exec`)<br>• `5B`: `200×5=1,000` (`c=100..1000`, `--verify-exec`)<br>• `5C`: `--mode soak --total-claims 20000 --wave-size 200` | Standing warm (`min_ready_fraction=1.0`, `0` terminating) → `5A`/`5B`: **`AAABBB` (`--repeats 3`/arm)**; `5C`: `100`-wave `20k` soak (`AB`, `n=1`) | Single-clock `TTFI` (`First`/`P50`/`Mean`/`P99`/`Max`), `wire_claim_s`, FD/socket/RSS stability |
| **Suite #6 (`C4-6`)**<br>Active gVisor Packing Density Sweep (`2.0 → 7.34 / vCPU`) | `T1` · `controller-t1-tuned.yaml` (`1000/500`) + 3-lane APF | `10 × n2-standard-32` (`320` vCPUs) + hydrated `600 GB` SBD (`c1` reused in Wave 2) | `run_density_bench.py`<br>`c ∈ {640, 1600, 2350}` (`125m / 256Mi / 4Gi /tmp`, `/testbed` Python assertion) | Hydrated SBD → **`ABABAB` (`3` outer reps: `r1, r2, r3`)** | Achieved active sandboxes/vCPU, `0` OOM/Evict verification, `TTFI`, `reap_zero_pods` |
| **Suite #7 (`C4-7`)**<br>5-Cluster `us-central1` Fleet Scale (`4k`, `4,096`, `8k`, `5k` Pure SDK) | `T1` · `controller-t1-tuned.yaml` (`1000/500` per cluster) + 3-lane APF | `5` regional GKE `1.37` clusters × `10 × n2-standard-32` + `600 GB` SBD (`50` worker nodes = `1,600` vCPUs) | `run_multicluster_bench.py` (`7A` `warm-burst` `1k/2k/4k`, `7B` `golden-small` `8×512=4,096`, `7C` `warm-burst` `c=8,000`) + `run_mc5_sdk_batch_bench.py` (`7D` `1k/2.5k/5k`) | Upfront 5-cluster SBD hydration → **`7B` & `7D`: `ABABAB` (`3` outer reps)**; **`7A` & `7C`: `AAABBB` (`--repeats 3`/arm)** | Both **fleet-wide (`5×`)** and **per-cluster (`÷ 5`)** `claims_per_s`, `release_api_per_s`, `pods_gone_per_s`, `TTFI`, job `P100` barrier |

---

## 4. Regional `us-central1` Quota Math & Phased Teardown Gate

To stay strictly below regional `us-central1` project quotas (`SSD_TOTAL_GB <= 40,960 GB`, `E2_CPUS <= 600`, `N2_CPUS <= 3,000`) with comfortable headroom and zero idle burn, **Phases 1–2 (Single-Cluster Suites #1–#6) MUST tear down before Phase 3 (5-Cluster Suite #7) is provisioned**:

| Execution Phase | Active Clusters | `E2_CPUS` Used (`<= 600` Cap) | `N2_CPUS` Used (`<= 3,000` Cap) | `SSD_TOTAL_GB` (`pd-balanced` + `pd-ssd` + SBD, `<= 40,960 GB` Cap) | Quota Verdict |
|---|---|---:|---:|---:|---|
| **Pre-Run Staging** | `0` clusters | **`0` / `600`** | **`0` / `3,000`** | **`0 GB` / `40,960 GB`** | **PASS (`$0/hr`)** |
| **Wave 1: Parallel Track A (`asrl-c4-t0`, `Suites #1, #2, #4A–4C`) + Track B (`asrl-c4-1`, `Suites #3, #4-SWE, #5, #6`)** | `2` (`asrl-c4-t0`, `asrl-c4-1`) | Peak `2 × (2×32)` (`ctrl`) + `10×32` (`T0 gvisor`) = **`448` / `600`** (`74.7%`) | Peak `2 × (2×16)` (`driver`) + `2 × (10×32)` (`gvisor`) = **`704` / `3,000`** (`23.5%`) | `ctrl` + `driver` + `T1` boot use `pd-standard`; peak `10×1,500` (`asrl-c4-t0` `pd-balanced`) + `10×600` (`asrl-c4-1` SBD) = **`21,000 GB` / `40,960 GB`** (`51.3%`) | **PASS** |
| **Mandatory Gate Between Wave 1 & Wave 2** | Delete cluster `asrl-c4-t0` after `Suite #4C`; reuse `asrl-c4-1` as `c1` of `c1..c5` (`provision_wave2_mc5_v4.sh` hard-fails if `asrl-c4-t0` exists) | — | — | Drops `SSD_TOTAL_GB` from `21,000 GB` to `6,000 GB` before provisioning `c2..c5` | **MANDATORY GATE** |
| **Wave 2: 5-Cluster `T1` SBD Fleet (`Suite #7`, `asrl-c4-1..5`)** | `5` (`asrl-c4-1..5`) | `5 × (2×32)` (`ctrl`) = **`320` / `600`** (`53.3%`) | `5 × (2×16)` (`driver`) + `5 × (10×32)` (`gvisor`) = **`1,760` / `3,000`** (`58.7%`) | `ctrl`, `driver`, and worker boot use `pd-standard`; `50 × 600` (`SBD` `pd-balanced`) = **`30,000 GB` / `40,960 GB`** (`73.2%`, **`10,960 GB` margin**) | **PASS (`10.96 TB` margin)** |
| **Post-Run Teardown** | `0` clusters (`delete` all `5` clusters + NAT + static IPs) | **`0` / `600`** | **`0` / `3,000`** | **`0 GB` / `40,960 GB`** | **PASS (`$0/hr`)** |

---

## 5. Substrate Comparability Rules & `results-reviewer.md` Compliance

To guarantee every comparison in `RESULTS.md` and `performance_reports/` passes `results-reviewer.md` (`13 / 13` rules):

1. **Strict Column-Matched Speedup Claims (Rule #8 & Rule #12):**
   - Never compare a no-op probe (`probe='true'`) against a real-work row (`git status + git log + sleep 30`, e.g., `RESULTS.md #1` `2,218.1s`) as a direct `s/task` speedup without matching the `30s` task-work probe (`Suite #3C`).
   - Every `"X is faster than Y"` statement MUST compare rows sharing identical:
     - **Probe** (`true` vs `python3 -V` vs `git + sleep 5` vs `git + sleep 30`),
     - **Footing** (`cold 0%` vs `cold first-boot SBD` vs `warm 100%-hydrated`),
     - **Cache Tier & Machine** (`T0 e2-standard-32 GCFS` vs `T1 n2-standard-32 600 GB SBD`),
     - **Controller Workers** (`Ctrl sbx/wp = 1000/500`), and
     - **Image Streaming** (`GCFS ON`).
2. **Substrate Cross-Comparison Rules (`agent-sandbox` vs Raw Pods `B0` / Other Substrates):**
   - When comparing `agent-sandbox` (`per_claim` or `batch`) against `B0` raw pods (`run_docker_baseline.py` — the `DockerRuntime` / `tunix-deepswe` cold pod-per-task path), match container requests (`125m / 1Gi`), runtime isolation (`gvisor` vs `gvisor`, with any `runc` arm explicitly labeled as isolation-unmatched appendix), task count, concurrency (`c = 500`), guest `@sha256` digest list, and timed teardown-to-zero-pods semantics.
3. **Multi-Cluster Normalization:**
   - Never compare a 5-cluster aggregate rate (`claims_per_s`) directly against a 1-cluster rate without explicitly citing the per-cluster (`÷ 5`) rate alongside the fleet-wide (`5×`) rate.
4. **Repository Hygiene & CLA Authorship (Rules #1, #2, #5, #9, #13):**
   - Results PRs in `sandbox-rl-performance` touch only `agent-sandbox/RESULTS.md` (append-only) and `agent-sandbox/performance_reports/*.md` (zero raw `.json`/`.jsonl`, zero driver edits, zero unexpanded template tokens, 100% `@sha256` digests, and human-only git commit/PR authorship with zero AI/tool attribution trailers or footers).

---

## 6. Customer, PM & Nov 16 Launch Deliverable Mapping

| Customer / PM Requirement (Nov 16 Target) | Campaign 4 Suite & Proof Point | Target Decision Metric |
|---|---|---|
| **1. Synchronous RL Step Barrier (`Tunix` / `GRPO` / `DeepSWE`)**<br>*Unblock idle TPUs/GPUs waiting on the slowest rollout in a step* | **Suite #2 (`B0` vs SDK)** & **Suite #4 (`R2E-Gym` `8,000`-task 8-step + `Tunix GRPO` `8,192`-task `c=1,024`)** | Step `P100` barrier (`env_ready_all_s`), steady-state step wall (`Steps 1–7`), and `0%` step failure rate |
| **2. Interactive & Sub-Second Agent Startup (`True TTFI`)**<br>*Single-clock claim + first container instruction under burst* | **Suite #5 (`1×1,000` & `200×5` Standing Warm `TTFI` + `20k` Soak)** | Paired `TTFI` (`First`, `P50`, `Mean`, `P99`, `P100`) at `c = 100..1,000` + zero socket/FD leak over `20,000` claims |
| **3. High-Cardinality Repository Sweeps (`500` → `4,578` Unique Images)**<br>*Prevent disk eviction, registry throttling & cold-pull tail spikes* | **Suite #1 (`T0` Warm `3,000`)**, **Suite #2 (`T0` Cold `0%`)**, **Suite #3 (`T1` `600 GB` SBD)**, **Suite #4C (`R2E-Gym` `4,578` `@sha256` images)** | Cold vs hydrated `T0`/`T1` `wall_s`, `wait_pool_ready`, and `4,578`-image windowed `pipelined` completion |
| **4. Worker Compute Packing Efficiency (`>= 5.0` Sandboxes / vCPU)**<br>*Maximize active gVisor sandboxes per node without OOMs or evictions* | **Suite #6 (`640 / 1,600 / 2,350` Active SWE-bench Sandboxes on `320` vCPUs = `2.0 / 5.0 / 7.34` per vCPU)** | Achieved active sandboxes/vCPU with `0` `OOMKilled`, `0` `Evicted`, `0` `MemoryPressure`/`DiskPressure` |
| **5. Horizontal Multi-Cluster Scale (`5,000–8,000` Concurrent Sandboxes)**<br>*Linear control-plane scaling + `DELETE_COLLECTION` bulk teardown* | **Suite #7 (`7A` `4k` Burst, `7B` `8×512=4,096` Multi-Job, `7C` `8,000` Ceiling, `7D` `5,000` Pure SDK Release)** | Fleet-wide (`5×`) & per-cluster (`÷ 5`) `claims_per_s`, `release_api_s` vs `pods_gone_s`, client socket reduction |
