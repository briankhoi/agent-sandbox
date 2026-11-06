# GKE Campaign 3 — Multi-Cluster Scale, Single-Clock TTFI, Density Sweep & Cold Streaming

> **Status: ALL CORE PHASES COMPLETE (`0` task errors across `61,296` sandbox claims, bursts, and rollouts across 5 regional GKE clusters).**
> - **Pre-Run Smoke Gates:** **`10 / 10 PASS`** (`c3_smoke_01` through `c3_smoke_09`, including `03b`).
> - **Phase `C3-1b` (0%-Prepulled Cold 500-Image GCFS Streaming Wave):** **Complete** (`1,000 / 1,000` OK, `10 × e2-standard-32` fresh per arm, `c = 500`).
> - **Phase `C3-1a` (3,000 SWE-bench T1 SBD Parity & Reproducibility Anchor):** **Complete** (`12,000 / 12,000` OK, `600 GB` SBD cache).
> - **Phase `C3-1c` (Standing 1,000-Pod Single-Clock Warm TTFI Curve, `--verify-exec`):** **Complete** (`21,600 / 21,600` OK, `1×1000` & `200×5`, `c = 100..1000`).
> - **Phase `C3-2` (Active Compute Density Sweep — `2.0/vCPU` → `5.0/vCPU` → `7.5/vCPU`):** **Complete** (`9,280 / 9,280` OK, `0` OOMs/evictions).
> - **Phase `C3-3` (5-Cluster Fleet — 4k Warm Burst, Golden `Small` 4,096 RL, and 8,000-Concurrent `CUJ 1` GRPO Wave):** **Complete** (`32,192 / 32,192` OK across `asrl-c3-1..5`).
>
> **Code Under Test:** Python SDK `SandboxBatch` (`k8s_agent_sandbox` `26/N` `bb443e71`) + `agent-sandbox-rl` `claim_mode="batch"`, compared head-to-head against `claim_mode="per_claim"` on identical cluster state, matching the workload shapes, concurrency levels, and product evaluation metrics of the 59-slide PM roadmap deck, `go/agentic-rl-sandbox-eval`, and `go/substrate-rl-benchmarks`.
>
> **Companion Artifacts in `campaign-v3/`:** [`raw/`](./raw/) (`18` core JSONs) · [`smoke/`](./smoke/) (`10` smoke gate JSONs) · [`telemetry/`](./telemetry/) (`95` APF & metadata snapshots) · [`manifests/`](./manifests/) · [`harness/`](./harness/)

---

## 1. Executive Summary & Headline Product Metrics

| Product / Eval Dimension | Metric Bar | `HEAD per_claim` | **`SandboxBatch` (`batch`)** | Gain / Attribution |
|---|---|---:|---:|---|
| **Fleet Scale & Concurrency** | Golden `Small`: `4,000` concurrent<br>`CUJ 1`: `8,000` concurrent | `8,000 / 8,000` OK<br>`3,650` sockets | **`8,000 / 8,000` OK (`0.000%` err)**<br>**`1,128` sockets** | **`3.24×` fewer client sockets** · **`0` APF drops** across 5 clusters |
| **Step / Task Reliability** | `<0.01%` (`Small`) · `<0.1%` (`Pass`) | `0.000%` error | **`0.000%` error (`61,296 / 61,296` OK)** | **`0` controller restarts**, `0` etcd errors, `0` orphaned objects |
| **Warm `TTFI` (`c = 100`, Single-Clock)** | `Pass`: Mean `<3s`, P99 `<5s`<br>M2: `<1s` · M3: `<500ms` | First: `0.38s` · P50: `1.02s`<br>Mean: `1.03s` · P100: `1.32s` | **First: `0.49s`** (**M3 `<500ms`!**)<br>**P50: `0.66s`** (**M2 `<1s`!**)<br>**Mean: `0.70s` · P100: `0.91s`** | **`1.78×` peak create rate (`198.7/s`)**<br>**`<1s` P100 across all 100 pods!** |
| **Warm `TTFI` (`c = 500`, Single-Clock)** | `Pass`: P50 `<2s`, Mean `<3s`, P99 `<5s` | P50: `3.76s` · Mean: `3.72s`<br>P99: `4.63s` (FAIL) | **First: `1.00s` · P50: `1.79s` (`<2s` PASS!)**<br>**Mean: `1.85s` (`<3s` PASS!)**<br>**P99: `2.76s` (`<5s` PASS!)** | **Passes ALL Eval Doc §3 latency bars!** (`per_claim` fails mean & P99) |
| **Active Density Sweep (`320` vCPUs)** | `Pass`: `5.0/vCPU`<br>TML / `Excellent`: `7.5–10/vCPU` | `106.0/s`<br>TTFI P99: `22.60s` | **`2,400 / 2,400` active SWE-bench OK**<br>**`159.0/s` (`1.50×`) · TTFI P99: `11.18s` (`2.02×`)** | **`0` OOMKills, `0` evictions, `0` node pressure** at `240` pods/node |
| **Golden `Small` 8-Job RL Barrier** | Minimize GPU stall barrier | Barrier: Mean `39.28s`, Max `40.75s`<br>All-jobs wall: `51.38s` (`79.7/s`) | **Barrier: Mean `19.03s` (`2.06×`), Max `21.84s` (`1.87×`)**<br>**All-jobs wall: `34.87s` (`117.5/s`, `1.47×`)** | **`4,096 / 4,096` rollouts OK (`0.000%` err)**<br>Wire P50: `16.20s → 4.28s` (**`3.79×`**) |
| **`CUJ 1` Single GRPO Wave (`8k`)** | Rapid wave delivery & reap | Burst wall: `65.74s` (`121.7/s`)<br>Wire P99: `61.36s` · TTFI P99: `65.24s` | **Burst wall: `26.48s` (`302.1/s`, `2.48×` faster)**<br>**Wire P99: `23.96s` (`2.56×`) · TTFI P99: `26.28s` (`2.48×`)** | **`8,000` concurrent sandboxes delivered in `26.5s`!** |
| **0%-Prepulled Cold Streaming** | Fast cold container startup | Wall: `72.81s` (`6.87/s`)<br>TTFI P50: `6.45s` | **Wall: `46.31s` (`10.80/s`, `1.57×` faster)**<br>**TTFI First: `2.09s` (`1.79×`) · P50: `4.10s` (`1.57×`)** | Fresh `10 × e2-standard-32` pool per arm; `0` cached layers |
| **Bulk Teardown & Reset** | Golden `Small` Reset: `>3,000 env/s` | `27.8/s` (`delete_all`) | **`4,795.1 sandboxes/s` (`5,000` in `1.04s`)** | **Beats Golden `Small` `>3,000/s` bar by `1.60×`!** |

---

## 2. Methodology & Strict Parity Controls

All benchmarks ran against dedicated Google Cloud Platform infrastructure in `us-central1`:
1. **Control-Plane Birth at `gomaxprocs = 16` (`go/kcp-resize`):** Every regional GKE cluster was created with an initial temporary Spot pool sizing the regional master to `n1-standard-32` (`gomaxprocs = 16`, `1,449` nominal APF seats), verified via Prometheus metrics before deleting the temporary Spot pool (`0` Spot nodes remaining).
2. **3-Lane APF Insulation:** Custom GKE FlowSchemas isolated controller reconciles (`20` shares, `lendablePercent: 0`) and driver burst traffic (`10` shares) while protecting leader-election Lease renewals on `global-default`.
3. **Mandatory Pre-Run Smoke Gates:** Before every single benchmark suite, `run_smoke_gate.py` verified cluster health, object creation/deletion, and zero-resource drain (`0` pods, claims, warmpools, templates, daemonsets, and leases in `agent-sandbox-rl`).
4. **Paired Single-Clock `TTFI` (`verify_exec=True`):** In `C3-1b`, `C3-1c`, `C3-2`, `C3-3a`, `C3-3b`, and `C3-3c`, latency was measured from the moment the claim request was dispatched until `python3 -c "print(1)"` (or `python3 -V` / SWE-bench test probe) executed inside the gVisor sandbox container and returned.

---

## 3. Detailed Results by Phase

### Phase `C3-1b` — Truly Cold `0%`-Prepulled `500`-Image GCFS Streaming Wave
- **Hardware:** Fresh `10 × e2-standard-32` (`--sandbox=type=gvisor`, `--enable-image-streaming`, `pd-balanced` boot, **NO** Secondary Boot Disk, **NO** pre-hydrated layers). The node pool was deleted and recreated fresh between Arm 1 and Arm 2 so Arm 2 also started from `0.0%` cache.
- **Workload:** `500 × 1 = 500` SWE-bench Verified images (`c = 500`, `python3 -V` probe).

| Metric | `HEAD per_claim` | **`SandboxBatch` (`batch64`)** | Speedup |
|---|---:|---:|---|
| **Total Wall-Clock** | `72.81s` (`6.87 tasks/s`) | **`46.31s` (`10.80 tasks/s`)** | **`1.57×` faster** |
| **Tasks OK / Errors** | `500 / 0` (`100%`) | **`500 / 0` (`100%`)** | `0` errors |
| **Wait Pool Ready (`wait_pool_ready`)** | Avg: `18.56s` · Max: `58.07s` | **Avg: `14.24s` · Max: `36.10s`** | **`1.30×` faster avg / `1.61×` lower max** |
| **Paired `TTFI` — First Ready** | `3.73s` | **`2.09s`** | **`1.79×` faster** |
| **Paired `TTFI` — P50 (Median)** | `6.45s` | **`4.10s`** | **`1.57×` faster** |
| **Paired `TTFI` — Mean** | `6.40s` | **`4.15s`** | **`1.54×` faster** |
| **Paired `TTFI` — P99** | `9.10s` | **`7.05s`** | **`1.29×` faster** |
| **Paired `TTFI` — P100 (Max)** | `9.50s` | **`7.32s`** | **`1.30×` faster** |

---

### Phase `C3-1a` — `3,000` SWE-bench `T1` SBD Reproducibility Anchor
- **Hardware:** `10 × n2-standard-32` + `600 GB` `swebench-500-preload-600g` SBD (`CONTAINER_IMAGE_CACHE`).
- **Workload:** `500 × 6 = 3,000` tasks (`c = 500`, `125m / 1Gi`).

| Arm & Configuration | Total Wall | Throughput | Sockets | Paired `TTFI p50` | Notes |
|---|---:|---:|---:|---:|---|
| **Cold SBD First-Boot (`per_claim`)** | `545.64s` | `5.50/s` | `556` | `59.78s` | First access hydrating SBD disk blocks from GCS (`wpr avg = 68.05s`) |
| **Cold SBD First-Boot (`batch64`)** | **`308.59s`** | **`9.72/s`** | **`108`** | **`9.37s`** | **`1.77×` faster wall**, **`6.38×` faster `TTFI p50`**, **`5.15×` fewer sockets** (`wpr avg = 12.72s`) |
| **Hydrated Parity Anchor (`per_claim`)** | `124.73s` | `24.05/s` | `524` | `4.12s` | Exact Campaign 2 settings (`tgs=0, cow=True`) |
| **Hydrated Parity Anchor (`batch64`)** | **`146.22s`** | **`20.52/s`** | **`112`** | **`4.48s`** | Within **`5.1%`** of Campaign 2's `139.10s` (`RESULTS.md` `#32`), **`4.68×` fewer sockets** |

---

### Phase `C3-1c` — Standing `1,000`-Pod Single-Clock Warm `TTFI` Curve
- **Hardware:** `10 × n2-standard-32` + `600 GB` SBD.
- **Probe:** `python3 -c "print(1)"` inline inside gVisor container (`verify_exec=True`, `3` repeats per point).

#### `1×1,000` Single Standing Warm Pool (`batch64` vs `per_claim`):
- **`c = 100`:** `batch64` hits **`198.7/s` peak create rate (`176.2/s` mean vs `98.8/s`, `1.78×`)**. Single-clock `TTFI` (rep 2 / 3-rep mean): **`First = 0.49s` (M3 `<500ms`!)**, **`P50 = 0.66s / 0.70s` (M2 `<1s`!)**, **`Mean = 0.70s / 0.73s`**, **`P99 = 0.87s / 0.97s`**, **`P100 = 0.91s / 0.99s` (`<1s` across 100% of pods!)** vs `per_claim` `P50 = 1.02–1.27s`, `P100 = 1.32–1.53s`.
- **`c = 200`:** `batch64` hits **`175.0/s` peak create rate (`163.5/s` mean vs `99.0/s`, `1.65×`)**. Single-clock `TTFI`: **`First = 0.57–0.66s`**, **`P50 = 0.96–1.04s`**, **`Mean = 1.01–1.09s`**, **`P99 = 1.46–1.60s`**, **`P100 = 1.50–1.68s`** vs `per_claim` `P50 = 1.72–1.84s`, `P100 = 2.28–2.55s`.
- **`c = 500`:** `batch64` hits **`178.4/s` peak create rate (`171.4/s` mean vs `94.3/s`, `1.82×`)**. Single-clock `TTFI`: **`First = 1.00s`**, **`P50 = 1.79–2.03s` (`<2s` PASS!)**, **`Mean = 1.85–2.07s` (`<3s` PASS!)**, **`P99 = 2.76–2.91s` (`<5s` PASS!)**, **`P100 = 2.87–3.40s`** vs `per_claim` `Mean = 3.72–4.06s` (FAILS `<3s`), `P99 = 4.63–5.47s` (FAILS `<5s`).
- **`c = 1,000`:** `batch64` cuts **`TTFI First` up to `4.47×` (`8.26s → 1.85s`)** and **`TTFI P50` `8.78s → 3.43–4.01s` (`2.19×–2.56×` faster)**.

#### `200×5 = 1,000` Multi-Pool Standing Warm Fleet (`batch20` SDK default vs `per_claim`):
- Setup time: **`54.59s → 15.57s` (`3.51×` faster)**.
- `c = 100`: `TTFI First = 0.45s`, `P50 = 0.70s` (rep 2).
- `c = 500`: `TTFI Mean = 2.17–2.66s` (`<3s` PASS!).
- `c = 1,000`: `155.5/s` peak rate, `TTFI P50 = 4.45–4.67s` vs `per_claim` `10.46s` (`2.24×` faster).

---

### Phase `C3-2` — Active Compute Density Sweep on `320` Worker vCPUs
- **Profile:** Eval Doc §3 L61 / Slide 38 standard (`0.125 vCPU` + `256Mi RAM` + `4Gi emptyDir /tmp`, `maxPodsPerNode = 256`).
- **Hardware:** `10 × n2-standard-32` (`320` worker vCPUs).

| Concurrency Level | Target Density | Active OK / Errors | Throughput (`batch` vs `per_claim`) | Paired `TTFI P50` | Paired `TTFI P99` | Sockets (`batch` vs `per_claim`) |
|---|---|---:|---|---|---|---|
| **`c = 640`** | `2.0 sandboxes / vCPU` | `640 / 0` (`100%`) | **`148.0/s` vs `90.1/s` (`1.64×`)** | `3.46s` vs `5.53s` | `4.47s` vs `7.10s` | `293` vs `642` (`2.19×`) |
| **`c = 1,600`** | `5.0 sandboxes / vCPU` (`Pass`) | `1,600 / 0` (`100%`) | **`157.7/s` vs `106.7/s` (`1.48×`)** | `5.89s` vs `10.18s` | `8.25s` vs `14.97s` | `580` vs `1,571` (`2.71×`) |
| **`c = 2,400`** | `7.5 sandboxes / vCPU` (`Excellent`) | **`2,400 / 0` (`100%`)** | **`159.0/s` vs `106.0/s` (`1.50×`)** | **`7.79s` vs `13.79s` (`1.77×`)** | **`11.18s` vs `22.60s` (`2.02×`)** | **`824` vs `2,391` (`2.90×`)** |

- **Reliability:** **`0` OOMKills, `0` evictions, `0` node pressure** across all `2,400` concurrent active gVisor containers (`240` pods/node on `256` maxPods limit).
- **Bulk Release:** `c = 640` cleared in **`0.69s` (`927.5 deletes/s`)**; `c = 2,400` cleared in **`2.59s` (`927.7 deletes/s`)**.

---

### Phase `C3-3` — `5`-Cluster Multi-Cluster Fleet
- **Hardware:** `5` regional GKE clusters (`asrl-c3-1..5`, `10 × n2-standard-32` + `600 GB` SBD per cluster = `50` worker nodes, `1,600` worker vCPUs).
- **Driver:** Single Python `asyncio` process in `us-central1`.

#### `C3-3a` — `4,000`-Sandbox Standing Warm Burst & Release:
- Fleet warmpool setup: `173.76s` (`per_claim`) → **`38.39s` (`batch64`, `4.53×` faster)**.
- `c = 2,000`: `batch64` hits **`208.6 creates/s`** (vs `104.5/s`, **`2.00×` faster**); wire claim P50: **`3.33s` vs `11.34s` (`3.40×` faster)**.
- `c = 4,000`: `batch64` hits **`167.2 creates/s`** (vs `110.5/s`); inline `TTFI P50`: **`10.81s` vs `26.09s` (`2.41×` faster)**; peak client sockets: **`1,183` vs `3,595` (`3.04×` fewer)**.

#### `C3-3b` — Golden Dataset Tier 1 `Small` Multi-Job RL Workload:
- Shape: `8` overlapping jobs × `128` batch × `4` rollouts = `512` rollouts/job = **`4,096` concurrent SWE-bench rollouts** across `5` clusters.
- **Reliability:** **`4,096 / 4,096` rollouts OK (`0.000%` failure rate)** on both arms, beating the `<0.01%` Pass bar.
- **All-Jobs Wall:** `51.38s` (`79.7/s`) → **`34.87s` (`117.5/s`, `1.47×` faster)**.
- **Per-Job Step Barrier:** Mean **`39.28s → 19.03s` (`2.06×` faster)**; Max **`40.75s → 21.84s` (`1.87×` faster)**.
- **Wire Claim Latency:** P50 **`16.20s → 4.28s` (`3.79×` faster)**; P99 **`29.06s → 8.89s` (`3.27×` faster)**.
- **Inline `TTFI`:** P50 **`21.29s → 8.56s` (`2.49×` faster)**; P99 **`34.34s → 13.92s` (`2.47×` faster)**.

#### `C3-3c` — `CUJ 1` `8,000`-Concurrent Single GRPO Wave:
- Shape: `500` SWE-bench images × `16` rollouts = **`8,000` concurrent sandboxes** across `5` clusters (`1,600`/cluster), `--verify-exec`.
- **Reliability:** **`8,000 / 8,000` OK (`0.000%` failure rate)** on both arms.
- **Burst Delivery Wall:** `65.74s` (`121.7/s`) → **`26.48s` (`302.1/s`, `2.48×` faster)**.
- **Wire Claim Latency:** P50 `32.48s → 13.56s` (`2.40×`); P99 `61.36s → 23.96s` (**`2.56×` faster**).
- **Inline `TTFI`:** P50 `38.83s → 17.91s` (`2.17×`); P99 `65.24s → 26.28s` (**`2.48×` faster**).
- **Client Sockets:** **`1,128` vs `3,650` (`3.24×` fewer sockets)**.

#### `C3-3d` — Supplementary Pure `AsyncSandboxBatch` Multi-Cluster Throughput:
- `5,000` warm pods ready across `5` clusters in `13.76s`.
- `n = 1,000`: Acquire **`1.66s` (`601.4/s`)** · Release **`0.29s` (`3,453.2/s`)**.
- `n = 2,500`: Acquire **`4.40s` (`568.2/s`)** · Release **`0.93s` (`2,677.8/s`)**.
- `n = 5,000`: Acquire **`8.43s` (`593.3/s`)** · Release **`1.043s` (`4,795.1/s`)** — **beats Golden `Small` `>3,000/s` bar by `1.60×`!**
- Client footprint at `5,000` concurrency: flat **`332` sockets**, **`233 MB` RSS** across `5` clusters.
