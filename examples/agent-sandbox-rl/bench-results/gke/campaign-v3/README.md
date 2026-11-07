# GKE Campaign 3 — True TTFI, Compute Density & 5-Cluster Scale Benchmark Report

> **Status: ALL 21 BENCHMARK RUNS COMPLETE (`132,572` sandbox claim & rollout operations across `21` raw JSON artifacts; `0` claim errors across all `SandboxBatch` arms vs `30` `ConnectionTimeoutError` claim errors + `1` `terminate()` error on `per_claim` in `C3-3d`; `0` controller restarts; `5` transient exec `probe_err`s).**
> - **Single-Cluster True TTFI, Cold Pull, Symmetric First-Boot SBD & Packing Density (`asrl-c3-1`):** `14` runs complete (`C3-1a` symmetric first-boot cold SBD + symmetric hydrated `T1` SBD anchor + sequential hydration diagnostic, `C3-1b` `0%`-cached cold GCFS streaming, `C3-1c` `1×1,000` & `200×5` warm TTFI curves, `C3-2` `2.0 / 5.0 / 7.5` sandboxes/vCPU density sweep up to `2,400` concurrent gVisor pods).
> - **5-Cluster Fleet Horizontal Scale (`asrl-c3-1..5`, `50 × n2-standard-32` + `50 × 600 GB` SBD = `1,600` worker vCPUs):** `7` runs complete (`C3-3a` `4,000`-pod warm burst curve, `C3-3b` `8 × 512 = 4,096` concurrent RL jobs, `C3-3c` `8,000`-sandbox single-wave ceiling, and `C3-3d` `1,000 / 2,500 / 5,000`-sandbox pure Python SDK `AsyncSandboxBatch` claim + `DELETE_COLLECTION` release measuring both API return and full pod deletion).
>
> **Companion Artifacts in `campaign-v3/`:** [`raw/`](./raw/) (`21` JSONs) · [`smoke/`](./smoke/) (`14` smoke-gate JSONs) · [`telemetry/`](./telemetry/) (APF & run metadata) · [`manifests/`](./manifests/) · [`harness/`](./harness/)

---

## 1. Key Findings (Computed Directly From `raw/*.json`)

1. **5-Cluster `8,000`-Sandbox Single-Wave Ceiling (`C3-3c`):**
   - Across `5` regional GKE `1.37` clusters (`500` unique SWE-bench images, `8,000` standing warm gVisor pods), `batch64` acquired all `8,000` sandboxes in **`26.48s` (`302.1 claims/s`)** vs `65.73s` (`121.7 claims/s`) on `per_claim` (**`2.48×` faster**), lowered true end-to-end `TTFI` (`claim + first container exec`) `p50` from `46.07s → 26.69s` (**`1.73×` faster**) and `p99` from `65.24s → 29.12s` (**`2.24×` lower**), and reduced held client sockets **`3.24×` (`3,650 → 1,128`)** with `8,000 / 8,000` OK on both arms.
2. **5-Cluster Multi-Job Synchronous RL Step Barrier (`C3-3b`, `8` Concurrent Jobs × `512` Sandboxes = `4,096` Total):**
   - Driving `8` independent RL rollout jobs concurrently across `5` clusters (`4,096 / 4,096` OK, `0.00%` failure rate), `batch20` cut total multi-job wall time from `51.38s → 34.87s` (**`1.47×` faster**), reduced the per-job `P100` step barrier from `40.75s → 21.84s` (**`1.87×` faster**), and lowered aggregate `TTFI` `p50` `23.72s → 12.46s` (**`1.90×`**) and `p99` `32.91s → 17.57s` (**`1.87×`**).
3. **5-Cluster `5,000`-Pod Pure Python SDK Claim & Bulk Teardown — API Return vs Actual Pod Deletion (`C3-3d`):**
   - Across `6` consecutive `SandboxBatch` bursts (`1,000×2 + 2,500×2 + 5,000×2 = 17,000 / 17,000` claims), `batch` recorded **`0` errors**, acquiring `5,000` sandboxes in **`10.74s` (`492.7/s`, `11.00×` faster** than `per_claim` `118.15s`) and returning from `batch.release()` (`5` parallel `DELETE_COLLECTION` + `Lease` deletes = `10` RPCs) in **`0.97s` (`5,176.6/s`)** vs **`14.45s` (`345.1/s`, `14.96×` faster)** on `5,000` concurrent `terminate()` calls and **`30.29s` (`164.7/s`, `31.36×` faster)** on `AsyncSandboxClient.delete_all()`. Because `kubernetes_asyncio.watch.Watch.stop()` closes response sockets on every per-claim watch, `per_claim` drained its keep-alive pool from `336 → 6` sockets and hit `5/2,500` and `12–13/5,000` external GKE VIP `ConnectionTimeoutError`s, whereas `batch` kept its `337 → 342` `ESTABLISHED` socket pool intact with `0` errors.
   - Measuring **actual pod termination convergence** (`pods_gone_s`: time from `release()` start until all `N` claimed `Pod` objects are absent across all `5` clusters, `terminationGracePeriodSeconds: 0`): when `batch.release()` returned at `0.97s`, **`4,623 / 5,000` (`92.5%`) claimed pods were still alive**; `SandboxClaim`s reached `0` at **`1.79s`** (`2,798.7/s`), and all `5,000` claimed gVisor `Pod` objects reached `0` at **`10.05s` (`497.8 pods/s`)** — **`1.52×` faster** than concurrent `terminate()` (`15.24s`, `327.2 pods/s`) and **`3.12×` faster** than `delete_all()` (`31.36s`, `159.1 pods/s`).
4. **True End-to-End Warm `TTFI` (`C3-1c`) & `0%`-Cached Cold GCFS Pull (`C3-1b`):**
   - **Single `1,000`-pod warm pool (`C3-1c`, 3-repeat medians):** True `TTFI` `p50` = **`0.66s`** (`c=100`), **`0.96s`** (`c=200`), **`1.95s`** (`c=500`), and **`3.80s`** (`c=1,000`, **`2.31×` faster** than `per_claim` `8.78s`).
   - **Multi-pool `200 × 5 = 1,000` warm fleet (`C3-1c`, 3-repeat medians):** At `c = 1,000`, `batch20` lowered `TTFI` `p50` `10.46s → 4.66s` (**`2.24×` faster**) with `0` probe errors vs `4` transient probe errors on `per_claim`.
   - **Cold `0%`-cached GCFS streaming (`C3-1b`, fresh `gvisor-pool` recreated before each arm):** `batch64` completed `500` unique SWE-bench startups in **`46.31s`** vs `72.81s` on `per_claim` (**`1.57×` faster**; `TTFI p50` `6.45s → 4.10s`).
5. **High-Density Packing Validation (`C3-2`, up to `7.5` Active Sandboxes / vCPU = `2,400` Concurrent gVisor Pods on `10` Nodes):**
   - Validated `640` (`2.0/vCPU`), `1,600` (`5.0/vCPU`), and `2,400` (`7.5/vCPU` = `240` active pods/node) simultaneous gVisor SWE-bench sandboxes with **`0` claim errors, `0` probe errors, `0` OOM kills, and `0` pod evictions**. At `2,400` active pods, `batch64` cut `TTFI` `p50` from `16.19s → 7.80s` (**`2.08×`**) and `p99` from `22.60s → 11.18s` (**`2.02×`**).

---

## 2. Cluster Topology, Guardrails & Code Provenance

| Parameter | Single-Cluster Suites (`asrl-c3-1`) | 5-Cluster Suites (`asrl-c3-1..5`) |
|---|---|---|
| **GKE Version & Control Plane** | Regional GKE `v1.37.1-gke.1697000` (`us-central1`), `3 × 16`-vCPU HA `kube-apiserver` (`go_sched_gomaxprocs_threads = 16` verified via `/metrics`, `1,449` nominal APF seats) | `5` regional GKE `v1.37.1-gke.1697000` clusters (`15` HA `kube-apiserver` replicas = `240` control-plane vCPUs, all verified at `gomaxprocs = 16`) |
| **Private Networking & Cloud NAT** | `--enable-private-nodes`, Cloud Router + Cloud NAT (`minPortsPerVm=4096`) | Shared VPC Cloud Router + Cloud NAT backed by **`8` static external NAT IPs** (`maxPortsPerVm=32768`, `tcpEstablishedIdleTimeout=1200s`) so private driver pods on `asrl-c3-1` reach `c2..c5` external control-plane VIPs without single-IP SYN throttling |
| **Controller (`ctrl-pool`)** | `2 × e2-standard-32` (`dedicated=ctrl:NoSchedule`), `agent-sandbox-controller@sha256:b0b3c550...`, `--sandbox-concurrent-workers=1000 --sandbox-claim-concurrent-workers=500 --sandbox-warm-pool-concurrent-workers=500 --sandbox-warm-pool-max-refill-rate=500 --kube-api-qps=-1 --api-connections=4 --separate-watch-connection=true --cache-label-selectors=true` | Identical per cluster (`10` `e2-standard-32` controller nodes total across `5` clusters) |
| **Worker Pool (`gvisor-pool`)** | `10 × e2-standard-32` (`320` vCPUs, `200 GB pd-balanced`, `0%` cache) in `C3-1b`; `10 × n2-standard-32` (`320` vCPUs, `maxPodsPerNode=256`, `gvisor`, `100 GB pd-standard` + `600 GB` `swebench-500-preload-600g` `CONTAINER_IMAGE_CACHE` SBD) in `C3-1a`, `C3-1c`, `C3-2` | `10 × n2-standard-32` (`100 GB pd-standard` + `600 GB` `swebench-500-preload-600g` SBD) per cluster = **`50` worker nodes (`1,600` worker vCPUs, `30 TB` preloaded SBD)** |
| **APF & Run Isolation** | 3-lane APF (`agent-sandbox-critical` `20` shares, `agent-sandbox-bulk` `10` shares, leader-election `leases` pinned to `global-default`); mandatory smoke gate + zero-object drain + **`20s–30s` settle (`SETTLE_S=20` in `C3-1b`, `25` in `C3-1c`, `30` in `C3-1a`, `C3-2`, and all `5`-cluster suites)** | Identical across all `5` clusters (`SETTLE_S=30` enforced before every launch) |

### Workstream B Driver SHA Provenance (`4018f0dd` → `bb443e71`)
- Campaign 2 used `agent-sandbox-controller@sha256:b0b3c550ad8eed1571c9494e3dc40b710aff724796cf4671fb1b7a5b959a3716` + Python SDK/RL commit `4018f0dd` (`asrl-driver@sha256:eb77db4f...`).
- Campaign 3 kept the **exact same controller image digest** (`@sha256:b0b3c550...`) and updated the Python driver image to commit **`bb443e71`** (`asrl-driver@sha256:6d0b37df8c80b092186d5c8ee66d13e646283d48a686301d6ece6c3532f2a8c3`), incorporating Workstream B correctness/hardening fixes on `feat/rl-batch-hardening`:
  - `Lease`-UID selector scoping and full-duration crash-reaper grace period (`22/N`–`23/N` restack);
  - Deadline-bounded warm-pool readiness precheck, `pod_annotations` propagation, shared `CoreV1Api` reuse, cold-start watchdog, parallel `acquire_batch` / `release_all` rollback on partial failure, auto-sized HTTP connection pools, `4,096` executor thread ceiling, and pool-resize attribute sync (`25/N`–`26/N`).
- **Empirical Performance Impact Verification (`C3-1a`):** None of those hardening fixes add extra RPCs on the steady-state hot path. Matched `T1` hydrated SBD `batch64` (`#42` `146.22s`) reproduced Campaign 2 `#32` (`139.10s`) within **`5.1%`** (`0` errors on both).

---

## 3. Single-Cluster Results (`asrl-c3-1`)

### 3A. `C3-1a` — `T1` Secondary Boot Disk Symmetric First-Boot Cold SBD (`#39–#40`) & Symmetric Hydrated SBD (`#41–#42`) (`500 × 6 = 3,000`, `c = 500`, `naive`, `probe='true'`)

*(For `#39` and `#40`, a brand-new `10 × n2-standard-32 + 600 GB SBD` `gvisor-pool` was deleted and recreated from scratch immediately before **each** arm with a non-SWE-bench smoke gate (`alpine/socat:1.8.0.1`), guaranteeing identical `0/500`-hydrated first-boot SBD block state. `#41` and `#42` ran back-to-back on fully hydrated SBD nodes. The initial sequential diagnostic pair (`545.64s` → `308.59s`, `tgs=-1`) that uncovered GCE's lazy GCS SBD hydration is included at the bottom for provenance.)*

| Footing & Cache State | Arm | `wall_s` (`s/task`) | `wait_pool_ready` `avg` / `max` | Paired `TTFI` (`First` / `P50` / `P99` / `Max`) | Open Sockets | Notes |
|---|---|---:|---:|---|---:|---|
| **Symmetric First-Boot Cold SBD** (`tgs=0, cow=True`, fresh SBD nodepool) | **`per_claim` (`#39`)** | **`492.52s` (`0.1642`)** | **`63.98s` / `121.34s`** | **`0.38s / 60.39s / 147.31s / 158.53s`** | `559` | **`1.09×` faster wall than `#40`** (streaming `DELETE`s start `wp=1` GCS hydration refill before wave stragglers finish) |
| **Symmetric First-Boot Cold SBD** (`tgs=0, cow=True`, fresh SBD nodepool) | **`batch64` (`#40`)** | `539.18s` (`0.1797`) | `67.44s` / `144.26s` | `0.38s / 62.94s / 154.31s / 169.23s` | **`124`** | **`4.51×` fewer sockets**; wave-boundary `DELETE_COLLECTION` delays refill until wave straggler finishes |
| **Symmetric Hydrated SBD** (`tgs=0, cow=True`, warm SBD blocks) | **`per_claim` (`#41`)** | **`124.73s` (`0.0416`)** | `3.68s` / `6.02s` | `0.38s / 15.21s / 38.40s / 85.15s` | `524` | **`1.17×` faster wall than `#42`** (streaming individual `DELETE`s overlap `wp=1` refill) |
| **Symmetric Hydrated SBD** (`tgs=0, cow=True`, warm SBD blocks) | **`batch64` (`#42`)** | `146.22s` (`0.0487`) | `4.11s` / `6.83s` | **`0.38s`** / `19.22s` / **`36.34s`** / `123.18s` | **`112`** | Within `5.1%` of C2 `#32` (`139.10s`); **`4.68×` fewer sockets** & lower `p99` |
| *Diagnostic — Sequential Run 1 (`tgs=-1, cow=False`, cold SBD)* | `per_claim` *(archived)* | `545.64s` (`0.1819`) | `68.05s` / `171.80s` | `0.41s / 59.78s / 152.07s / 165.71s` | `556` | Uncovered first-boot lazy GCS SBD hydration + 30s pod termination grace |
| *Diagnostic — Sequential Run 2 (`tgs=-1, cow=False`, partially hydrated)* | `batch64` *(archived)* | `308.59s` (`0.1029`) | `12.72s` / `45.93s` | `0.38s / 9.37s / 132.34s / 142.63s` | `108` | Not footing-comparable to Diagnostic Run 1 (inherited hydrated SBD blocks) |

### 3B. `C3-1b` — Truly Cold `0%`-Cached `T0` GCFS Image-Streaming (`500 × 1 = 500`, `c = 500`, `probe='python3 -V'`)

*(Fresh `10 × e2-standard-32` (`200 GB pd-balanced`) `gvisor-pool` deleted and recreated from scratch immediately prior to **each** arm so both arms paid true `0%`-cached GCFS pulls across all `500` images.)*

| Arm | `wall_s` (`tasks/s`) | `wait_pool_ready` `avg` / `max` | `env_ready_ok_s` `p50` / `p99` / `max` | Paired `TTFI` (`First` / `P50` / `P99` / `Max`) | Open Sockets |
|---|---:|---:|---|---:|---:|
| `per_claim` (`#37`) | `72.81s` (`6.87/s`) | `18.56s` / `56.03s` | `64.06s / 64.92s / 64.93s` | `3.73s / 6.45s / 9.10s / 9.50s` | `128` |
| **`batch64` (`#38`)** | **`46.31s` (`10.80/s`, `1.57×`)** | **`14.24s` / `33.26s`** | **`39.95s / 41.39s / 41.67s`** | **`2.09s / 4.10s / 7.05s / 7.32s`** | `369` *(higher concurrency overlap)* |

### 3C. `C3-1c` — True End-to-End Warm `TTFI` Burst Curves (`3` Repeats / Point = `5,400` Claims / Arm, Medians Shown)

#### Single `1,000`-Replica Warm Pool (`1 × 1,000`)

| `c` | Arm | `TTFI First` | `TTFI P50` | `TTFI Mean` | `TTFI P99` | `TTFI Max` | Wire `P50` | Exec `P50` | Acquire Wall (`Rate`) | Release Wall | Held Sockets |
|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| `100` | `per_claim` | `0.84s` | `1.22s` | `1.21s` | `1.44s` | `1.53s` | `0.58s` | `0.49s` | `0.97s` (`102.6/s`) | `0.15s` | `186` |
| **`100`** | **`batch64`** | **`0.50s`** | **`0.66s`** (`1.83×`) | **`0.67s`** (`1.79×`) | **`0.95s`** (`1.50×`) | **`0.96s`** | `0.23s` | `0.43s` | `0.53s` (`187.8/s`) | `0.15s` | `163` |
| `200` | `per_claim` | `1.20s` | `1.77s` | `1.73s` | `2.24s` | `2.37s` | `1.19s` | `0.57s` | `1.93s` (`103.6/s`) | `0.28s` | `283` |
| **`200`** | **`batch64`** | **`0.61s`** | **`0.96s`** (`1.84×`) | **`1.04s`** (`1.66×`) | **`1.49s`** (`1.50×`) | **`1.65s`** | `0.51s` | `0.42s` | `1.19s` (`167.8/s`) | `0.24s` | `269` |
| `500` | `per_claim` | `2.34s` | `3.69s` | `3.73s` | `5.16s` | `5.47s` | `2.69s` | `0.95s` | `5.25s` (`95.2/s`) | `0.57s` | `572` |
| **`500`** | **`batch64`** | **`1.30s`** | **`1.95s`** (`1.89×`) | **`2.01s`** (`1.86×`) | **`2.86s`** (`1.81×`) | **`3.04s`** | `1.39s` | `0.52s` | `2.90s` (`172.3/s`) | `1.07s` | `492` |
| `1000` | `per_claim` | `4.39s` | `8.78s` | `8.22s` | `10.42s` | `11.15s` | `4.94s` | `3.40s` | `8.33s` (`120.0/s`) | `1.10s` | `1109` |
| **`1000`** | **`batch64`** | **`1.85s`** | **`3.80s`** (`2.31×`) | **`3.94s`** (`2.09×`) | **`6.49s`** (`1.61×`) | **`10.82s`** | `3.27s` | `0.57s` | `10.28s` (`97.3/s`) | `1.50s` | `713` |

#### Multi-Pool `200 × 5 = 1,000` Warm Fleet (`per_claim` vs SDK Default `batch20`)

| `c` | Arm | `TTFI First` | `TTFI P50` | `TTFI Mean` | `TTFI P99` | `TTFI Max` | Acquire Wall (`Rate`) | Release Wall | Held Sockets |
|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|
| `100` | `per_claim` | `0.86s` | `1.13s` | `1.13s` | `1.49s` | `1.59s` | `0.93s` (`107.2/s`) | `0.11s` | `259` |
| **`100`** | **`batch20`** | **`0.47s`** | **`0.97s`** (`1.17×`) | **`1.00s`** (`1.13×`) | **`1.42s`** (`1.05×`) | **`1.44s`** | **`0.96s` (`103.8/s`)** | `0.19s` | **`85`** (`3.05×` fewer) |
| `200` | `per_claim` | `1.18s` | `1.58s` | `1.60s` | `1.94s` | `2.19s` | `1.90s` (`105.1/s`) | `0.23s` | `302` |
| **`200`** | **`batch20`** | **`0.67s`** | **`1.28s`** (`1.23×`) | **`1.26s`** (`1.27×`) | **`1.71s`** (`1.14×`) | **`1.85s`** | **`1.36s` (`147.2/s`)** | `0.37s` | **`224`** (`1.35×` fewer) |
| `500` | `per_claim` | `2.29s` | `3.88s` | `3.79s` | `5.03s` | `5.75s` | `5.07s` (`98.6/s`) | `0.51s` | `610` |
| **`500`** | **`batch20`** | **`1.23s`** | **`2.76s`** (`1.41×`) | **`2.63s`** (`1.44×`) | **`3.90s`** (`1.29×`) | **`4.01s`** | **`3.34s` (`149.5/s`)** | `0.90s` | **`473`** (`1.29×` fewer) |
| `1000` | `per_claim` | `8.67s` | `10.46s` | `10.26s` | `11.94s` | `12.20s` | `8.67s` (`115.3/s`) *(4 probe_err)* | `0.93s` | `1262` |
| **`1000`** | **`batch20`** | **`2.31s`** | **`4.66s`** (`2.24×`) | **`4.56s`** (`2.25×`) | **`7.06s`** (`1.69×`) | **`7.47s`** | **`7.00s` (`142.8/s`)** | `2.40s` | **`612`** (`2.06×` fewer) |

### 3D. `C3-2` — Compute Packing Density Sweep (`10 × n2-standard-32` = `320` Worker vCPUs, `125m / 256Mi`, `0` OOMs / Evictions)

| Target Active (`Density`) | Arm | Active / Errors | Acquire Wall (`Rate`) | True `TTFI` (`First` / `P50` / `P99` / `Max`) | Release API (`s`) | Reap to `0` Pods (`s`) | Held Sockets |
|---|---|---|---:|---|---:|---:|---:|
| `640` (`2.0/vCPU`) | `per_claim` | `640/640` (`0` err) | `7.11s` (`90.1/s`) | `3.26s` / `5.53s` / `7.10s` / `7.10s` | `1.54s` | `36.16s` | `752` |
| **`640` (`2.0/vCPU`)** | **`batch64`** | **`640/640` (`0` err)** | **`4.32s` (`148.0/s`, `1.64×`)** | **`1.61s` / `3.46s` (`1.60×`) / `4.47s` (`1.59×`) / `4.72s`** | `0.69s` | `40.30s` | **`658`** (`1.14×` fewer) |
| `1600` (`5.0/vCPU`) | `per_claim` | `1600/1600` (`0` err) | `15.00s` (`106.7/s`) | `6.34s` / `11.14s` / `14.97s` / `14.99s` | `2.03s` | `72.67s` | `1571` |
| **`1600` (`5.0/vCPU`)** | **`batch64`** | **`1600/1600` (`0` err)** | **`10.15s` (`157.7/s`, `1.48×`)** | **`4.33s` / `5.89s` (`1.89×`) / `8.25s` (`1.82×`) / `8.67s`** | `1.79s` | `81.97s` | **`580`** (`2.71×` fewer) |
| `2400` (`7.5/vCPU`) | `per_claim` | `2400/2400` (`0` err) | `22.64s` (`106.0/s`) | `9.41s` / `16.19s` / `22.60s` / `22.63s` | `2.85s` | `225.63s` | `1414` |
| **`2400` (`7.5/vCPU`)** | **`batch64`** | **`2400/2400` (`0` err)** | **`15.10s` (`159.0/s`, `1.50×`)** | **`6.07s` / `7.80s` (`2.08×`) / `11.18s` (`2.02×`) / `12.68s`** | `2.97s` | `155.77s` | **`1091`** (`1.30×` fewer) |

---

## 4. 5-Cluster Fleet Scale Results (`asrl-c3-1..5`, `50` Worker Nodes = `1,600` vCPUs)

### 4A. `C3-3a` — 5-Cluster `4,000`-Pod Warm Fleet Burst Curve (`2` Repeats / Point = `14,000` Claims / Arm, Medians Shown)

| `c` (`5` Clusters) | Arm | `TTFI First` | `TTFI P50` | `TTFI P99` | `TTFI Max` | Acquire Wall (`Rate`) | Release API (`s`) | Held Sockets |
|---:|---|---:|---:|---:|---:|---:|---:|---:|
| `1000` | `per_claim` | `3.58s` | `7.09s` | `10.57s` | `10.63s` | `10.57s` (`94.7/s`) | `1.26s` | `737` |
| **`1000`** | **`batch64`** | **`3.62s`** | **`4.87s`** (`1.46×`) | **`6.40s`** (`1.65×`) | **`6.43s`** | **`6.47s` (`155.1/s`, `1.63×`)** | `1.43s` | `856` |
| `2000` | `per_claim` | `12.60s` | `17.61s` | `21.53s` | `21.58s` | `18.75s` (`106.7/s`) | `2.05s` | `1363` |
| **`2000`** | **`batch64`** | **`6.12s`** | **`9.50s`** (`1.85×`) | **`11.51s`** (`1.87×`) | **`11.98s`** | **`11.29s` (`181.3/s`, `1.66×`)** | `2.53s` | `1264` |
| `4000` | `per_claim` | `34.45s` | `41.45s` | `45.22s` | `46.17s` | `37.59s` (`106.5/s`) | `3.69s` | `3286` |
| **`4000`** | **`batch64`** | **`10.10s`** | **`18.02s`** (`2.30×`) | **`24.07s`** (`1.88×`) | **`24.44s`** | **`24.65s` (`162.4/s`, `1.53×`) *(1 probe_err)*** | `4.67s` | `1209` |

### 4B. `C3-3b` — 5-Cluster Concurrent Multi-Job RL Step (`8` Jobs × `512` Sandboxes = `4,096` Simultaneous Sandboxes)

| Metric | `per_claim` | **`batch20`** | Gain |
|---|---:|---:|---:|
| **Completed Sandboxes / Failure Rate** | `4,096 / 4,096` (`0.00%`) | **`4,096 / 4,096` (`0.00%`)** | `0` errors |
| **Warm Fleet Setup (`setup_s`)** | `17.40s` | **`15.79s`** | `1.10×` |
| **All-8-Jobs Total Wall (`all_jobs_wall_s`)** | `51.38s` (`79.73/s`) | **`34.87s` (`117.46/s`)** | **`1.47×` faster** |
| **Per-Job Step Barrier (`First` / `P50` / `P100` across 8 jobs)** | `37.62s / 40.27s / 40.75s` | **`13.76s / 21.08s / 21.84s`** | **`1.87×` lower `P100` barrier** |
| **Aggregate Wire Claim (`P50` / `P99` / `Max`, `n = 4,096`)** | `16.20s / 29.06s / 29.55s` | **`4.28s / 8.89s / 9.08s`** | **`3.27×` lower wire `p99`** |
| **Aggregate True `TTFI` (`First` / `P50` / `P99` / `Max`, `n = 4,096`)** | `13.10s / 23.72s / 32.91s / 33.40s` | **`5.30s / 12.46s / 17.57s / 18.30s`** | **`1.90×` faster `p50` · `1.87×` lower `p99`** |

### 4C. `C3-3c` — 5-Cluster `8,000`-Sandbox Single-Wave Ceiling (`500` Unique SWE-bench Images Across `5` Clusters)

| Metric (`c = 8,000`) | `per_claim` | **`batch64`** | Gain |
|---|---:|---:|---:|
| **Completed Sandboxes / Errors** | `8,000 / 0` (`0` probe_err) | **`8,000 / 0` (`0` probe_err)** | `100%` success at `c = 8,000` |
| **Acquire Wall (`claims/s`)** | `65.73s` (`121.7/s`) | **`26.48s` (`302.1/s`)** | **`2.48×` faster acquire** |
| **Full Burst + `8,000` Exec Wall** | `88.43s` | **`52.19s`** | **`1.69×` faster** |
| **Wire Claim (`First` / `P50` / `P99` / `Max`)** | `4.38s / 35.55s / 61.36s / 61.65s` | **`3.04s / 14.58s / 23.96s / 24.24s`** | **`2.56×` lower wire `p99`** |
| **True `TTFI` (`First` / `P50` / `P99` / `Max`)** | `26.57s / 46.07s / 65.24s / 65.78s` | **`23.79s / 26.69s / 29.12s / 30.31s`** | **`1.73×` faster `p50` · `2.24×` lower `p99`** |
| **Release API Return (`release_wall_s`)** | **`8.14s` (`982.3/s`)** | `9.40s` (`851.1/s`) | `per_claim` `1.15×` faster on API return |
| **Held Client Sockets / Driver RSS** | `3,650` sockets / `1072.0 MB` | **`1,128` sockets / `774.0 MB`** | **`3.24×` fewer sockets · `-27.8%` RSS** |

### 4D. `C3-3d` — 5-Cluster Pure Python SDK `5,000`-Sandbox Claim & Release (`release_api_s` vs `claims_gone_s` vs `pods_gone_s`)

*(Standing `1,000` warm gVisor pods/cluster × `5` clusters = `5,000` warm pods ready in `10.81s`; `2` repeats at `n ∈ {1000, 2500, 5000}` for `batch` (`6` runs), `1` repeat at `n ∈ {1000, 2500, 5000}` for concurrent `terminate()` (`3` runs), and `1` repeat at `n = 5000` for `delete_all()` (`1` run) = `10` records total. `release_api_s` measures when the SDK release call returns alongside how many claimed pods are still alive at that instant; `claims_gone_s` measures when `SandboxClaim` count hits `0`; `pods_gone_s` measures when all claimed `Pod` objects are completely deleted across all `5` clusters.)*

| Burst Size (`n`) | SDK Arm | Claims `ok` | Acquire Median (`Rate`) | Release API Return (`release_api_s`, Pods Alive) | `SandboxClaim`s Gone (`claims_gone_s`) | **Claimed Pods Gone (`pods_gone_s`)** | Pool Sockets (`Before → Held`) |
|---|---|---:|---:|---:|---:|---:|---:|
| `1,000` (`200`/cluster) | `per_claim` concurrent `terminate()` (`1` rep) | `1000/1000` | `15.75s` (`63.5/s`) | `14.46s` (`69.2/s`, `2` alive) | `14.95s` | `15.26s` (`65.5 pods/s`) | `109 → 18` |
| **`1,000` (`200`/cluster)** | **`batch.release()` (`DELETE_COLLECTION`, `2` reps)** | **`2000/2000`** | **`1.77s` (`565.4/s`)** | **`0.34s` (`2962.0/s`, `893` alive)** | **`0.82s`** | **`1.70s` (`588.6 pods/s`)** | **`337 → 342`** |
| `2,500` (`500`/cluster) | `per_claim` concurrent `terminate()` (`1` rep) | `2495/2500` *(+1 term_err)* | `83.42s` (`29.9/s`) | `32.88s` (`75.8/s`, `1` alive) | `33.65s` | `34.23s` (`72.9 pods/s`) | `337 → 21` |
| **`2,500` (`500`/cluster)** | **`batch.release()` (`DELETE_COLLECTION`, `2` reps)** | **`5000/5000`** | **`8.69s` (`377.0/s`)** | **`0.71s` (`3547.9/s`, `2213` alive)** | **`1.31s`** | **`4.96s` (`503.8 pods/s`)** | **`337 → 342`** |
| `5,000` (`1000`/cluster) | `per_claim` concurrent `terminate()` (`1` rep) | `4987/5000` | `118.15s` (`42.2/s`) | `14.45s` (`345.1/s`, `1` alive) | `15.28s` | `15.24s` (`327.2 pods/s`) | `336 → 6` |
| **`5,000` (`1000`/cluster)** | **`batch.release()` (`DELETE_COLLECTION`, `2` reps)** | **`10000/10000`** | **`10.74s` (`492.7/s`)** | **`0.97s` (`5176.6/s`, `4623` alive)** | **`1.79s`** | **`10.05s` (`497.8 pods/s`)** | **`337 → 342`** |
| `5,000` (`1,000`/cluster) | `per_claim` SDK `delete_all()` (`1` rep) | `4988/5000` | `120.72s` (`41.3/s`) | `30.29s` (`164.7/s`, `1` alive) | `31.39s` | `31.36s` (`159.1 pods/s`) | `337 → 9` |

---

## 5. Complete Accounting of Runs & Metrics Where `batch` Trails `per_claim`

Every case in Campaign 3 where `per_claim` recorded a lower latency, faster wall/release time, or lower resource count than `batch` is listed below without omission:

- **`c3_1a_cold_sbd_symmetric (500x6=3000 symmetric first-boot cold T1 SBD, naive, tgs=0, cow=True)` — `wall_s (and paired_ttfi_s p50/p99)`:** `per_claim = {"wall_s": 492.52, "ttfi_p50_s": 60.3878, "ttfi_p99_s": 147.3077}` vs `batch = {"wall_s": 539.18, "ttfi_p50_s": 62.9405, "ttfi_p99_s": 154.3092}` — With a brand-new 10x n2-standard-32 + 600GB SBD nodepool created from scratch before EACH arm, both arms paid identical first-boot GCE SBD lazy GCS block hydration (~368-393s above hydrated #41/#42), and per_claim beat batch(mif=64, create_rps=200) by 1.09x (492.52s vs 539.18s) for the same wp=1 streaming-delete overlap reason as hydrated #41/#42, while batch64 cut open_sockets_after 559 -> 124 (4.51x).
- **`c3_1a_anchor_exact_c2 (500x6=3000 hydrated T1 SBD, naive, tgs=0, cow=True)` — `wall_s (and paired_ttfi_s.p50)`:** `per_claim = {"wall_s": 124.73, "ttfi_p50_s": 15.2102}` vs `batch = {"wall_s": 146.22, "ttfi_p50_s": 19.2207}` — With gomaxprocs=16 + 3-lane APF + hydrated SBD (0 HTTP 429s) on size-1 warm pools (wp=1), per_claim streams individual claim DELETEs as tasks finish so controller warm-pool refill overlaps wave stragglers, whereas batch(mif=64, create_rps=200) paces creates at mif=64 and bulk-deletes at wave boundaries while cutting open_sockets_after 524 -> 112 (4.68x) and paired_ttfi p99 38.40s -> 36.34s.
- **`c3_1b_cold_swebench500 (500x1 cold 0%-cached GCFS streaming)` — `open_sockets_after`:** `per_claim = 128` vs `batch = 369` — batch64 won wall_s 46.31s vs 72.81s (1.57x faster), so all 500 concurrent probe exec connections overlapped simultaneously in a tighter window (369 pooled sockets vs 128 staggered sockets in slower per_claim).
- **`c3_1c_warm_ttfi_1x1000 (c=500)` — `release_wall_med_s`:** `per_claim = {"release_wall_med_s": 0.574}` vs `batch = {"release_wall_med_s": 1.071}` — 3-repeat median comparison for batch64 vs per_claim at c=500.
- **`c3_1c_warm_ttfi_1x1000 (c=1000)` — `acquire_wall_med_s, release_wall_med_s`:** `per_claim = {"acquire_wall_med_s": 8.331, "release_wall_med_s": 1.104}` vs `batch = {"acquire_wall_med_s": 10.281, "release_wall_med_s": 1.499}` — 3-repeat median comparison for batch64 vs per_claim at c=1000.
- **`c3_1c_warm_ttfi_200x5 (c=100)` — `acquire_wall_med_s, release_wall_med_s`:** `per_claim = {"acquire_wall_med_s": 0.932, "release_wall_med_s": 0.107}` vs `batch = {"acquire_wall_med_s": 0.963, "release_wall_med_s": 0.191}` — 3-repeat median comparison for batch20 vs per_claim at c=100.
- **`c3_1c_warm_ttfi_200x5 (c=200)` — `release_wall_med_s`:** `per_claim = {"release_wall_med_s": 0.226}` vs `batch = {"release_wall_med_s": 0.366}` — 3-repeat median comparison for batch20 vs per_claim at c=200.
- **`c3_1c_warm_ttfi_200x5 (c=500)` — `release_wall_med_s`:** `per_claim = {"release_wall_med_s": 0.51}` vs `batch = {"release_wall_med_s": 0.898}` — 3-repeat median comparison for batch20 vs per_claim at c=500.
- **`c3_1c_warm_ttfi_200x5 (c=1000)` — `release_wall_med_s`:** `per_claim = {"release_wall_med_s": 0.926}` vs `batch = {"release_wall_med_s": 2.405}` — 3-repeat median comparison for batch20 vs per_claim at c=1000.
- **`c3_3a_mc5_warm4000 (c=1000)` — `ttfi_first_med_s, release_wall_med_s, held_open_sockets_med`:** `per_claim = {"ttfi_first_med_s": 3.5806, "release_wall_med_s": 1.2555, "held_open_sockets_med": 737.0}` vs `batch = {"ttfi_first_med_s": 3.6197, "release_wall_med_s": 1.429, "held_open_sockets_med": 856.0}` — 2-repeat median comparison for batch64 vs per_claim at c=1000.
- **`c3_3a_mc5_warm4000 (c=2000)` — `release_wall_med_s`:** `per_claim = {"release_wall_med_s": 2.0515}` vs `batch = {"release_wall_med_s": 2.53}` — 2-repeat median comparison for batch64 vs per_claim at c=2000.
- **`c3_3a_mc5_warm4000 (c=4000)` — `release_wall_med_s, probe_err_all`:** `per_claim = {"release_wall_med_s": 3.688, "probe_err_all": 0}` vs `batch = {"release_wall_med_s": 4.6745, "probe_err_all": 1}` — 2-repeat median comparison for batch64 vs per_claim at c=4000.
- **`c3_2_density (c=640, 2.0/vCPU)` — `reap_to_zero_pods_s`:** `per_claim = {"reap_to_zero_pods_s": 36.158}` vs `batch = {"reap_to_zero_pods_s": 40.299}` — At c=640 (2.0/vCPU), batch64 won acquire_wall_s (4.324s vs 7.106s) and ttfi p50/p99 (3.46s/4.47s vs 5.53s/7.10s), while per_claim was lower on reap_to_zero_pods_s.
- **`c3_2_density (c=1600, 5.0/vCPU)` — `wire_first_s, ttfi_inline_first_s, reap_to_zero_pods_s`:** `per_claim = {"wire_first_s": 2.2196, "ttfi_inline_first_s": 2.5596, "reap_to_zero_pods_s": 72.674}` vs `batch = {"wire_first_s": 3.2483, "ttfi_inline_first_s": 4.33, "reap_to_zero_pods_s": 81.967}` — At c=1,600 (5.0/vCPU), batch64 won acquire_wall_s (10.147s vs 14.998s) and ttfi p50/p99 (5.89s/8.25s vs 11.14s/14.97s), while per_claim was lower on wire_first_s, ttfi_inline_first_s, reap_to_zero_pods_s.
- **`c3_2_density (c=2400, 7.5/vCPU)` — `wire_first_s, ttfi_inline_first_s, release_wall_s`:** `per_claim = {"wire_first_s": 2.4652, "ttfi_inline_first_s": 2.8471, "release_wall_s": 2.855}` vs `batch = {"wire_first_s": 4.6481, "ttfi_inline_first_s": 5.1585, "release_wall_s": 2.973}` — At c=2,400 (7.5/vCPU), batch64 won acquire_wall_s (15.095s vs 22.641s) and ttfi p50/p99 (7.80s/11.18s vs 16.19s/22.60s), while per_claim was lower on wire_first_s, ttfi_inline_first_s, release_wall_s.
- **`c3_3c_mc5_cuj1_8000 (c=8000 across 5 clusters)` — `release_wall_s (API return)`:** `per_claim = 8.144` vs `batch = 9.4` — 5-cluster DELETE_COLLECTION + Lease release returned in 9.400s (851.07/s) vs per_claim 8.144s (982.30/s), while batch won acquire_wall_s 2.48x (26.483s vs 65.735s) and cut held sockets 3.24x (3,650 -> 1,128).
- **`c3_3d_mc5_sdk_5000 (n=1000, vs per_claim_concurrent_terminate)` — `held_open_sockets_med`:** `per_claim = 18.0` vs `batch = 342.0` — batch retained its pre-warmed keepalive pool (337 -> 342 ESTABLISHED sockets, 0 errors), whereas per_claim_concurrent_terminate Watch.stop() closed pooled sockets during per-claim watches (109 -> 18 sockets, 0 claim errors).
- **`c3_3d_mc5_sdk_5000 (n=2500, vs per_claim_concurrent_terminate)` — `held_open_sockets_med`:** `per_claim = 21.0` vs `batch = 342.0` — batch retained its pre-warmed keepalive pool (337 -> 342 ESTABLISHED sockets, 0 errors), whereas per_claim_concurrent_terminate Watch.stop() closed pooled sockets during per-claim watches (337 -> 21 sockets, 5 claim errors).
- **`c3_3d_mc5_sdk_5000 (n=5000, vs per_claim_concurrent_terminate)` — `held_open_sockets_med`:** `per_claim = 6.0` vs `batch = 342.0` — batch retained its pre-warmed keepalive pool (337 -> 342 ESTABLISHED sockets, 0 errors), whereas per_claim_concurrent_terminate Watch.stop() closed pooled sockets during per-claim watches (336 -> 6 sockets, 13 claim errors).
- **`c3_3d_mc5_sdk_5000 (n=5000, vs per_claim_sdk_delete_all)` — `held_open_sockets_med`:** `per_claim = 9.0` vs `batch = 342.0` — batch retained its pre-warmed keepalive pool (337 -> 342 ESTABLISHED sockets, 0 errors), whereas per_claim_sdk_delete_all Watch.stop() closed pooled sockets during per-claim watches (337 -> 9 sockets, 12 claim errors).

---

## 6. Raw JSON Inventory (`campaign-v3/raw/` — `21` Artifacts)

- [`raw/c3_1a_cold_sbd_symmetric_per_claim.json`](raw/c3_1a_cold_sbd_symmetric_per_claim.json) & [`raw/c3_1a_cold_sbd_symmetric_batch64.json`](raw/c3_1a_cold_sbd_symmetric_batch64.json) *(Symmetric First-Boot Cold SBD, `#39–#40`)*
- [`raw/c3_1a_anchor_exact_c2_per_claim.json`](raw/c3_1a_anchor_exact_c2_per_claim.json) & [`raw/c3_1a_anchor_exact_c2_batch64.json`](raw/c3_1a_anchor_exact_c2_batch64.json) *(Symmetric Hydrated SBD, `#41–#42`)*
- [`raw/c3_1a_anchor_t1_swebench3000_per_claim.json`](raw/c3_1a_anchor_t1_swebench3000_per_claim.json) & [`raw/c3_1a_anchor_t1_swebench3000_batch64.json`](raw/c3_1a_anchor_t1_swebench3000_batch64.json) *(Sequential Hydration Diagnostic)*
- [`raw/c3_1b_cold_swebench500_per_claim.json`](raw/c3_1b_cold_swebench500_per_claim.json) & [`raw/c3_1b_cold_swebench500_batch64.json`](raw/c3_1b_cold_swebench500_batch64.json)
- [`raw/c3_1c_warm_ttfi_1x1000_per_claim.json`](raw/c3_1c_warm_ttfi_1x1000_per_claim.json) & [`raw/c3_1c_warm_ttfi_1x1000_batch64.json`](raw/c3_1c_warm_ttfi_1x1000_batch64.json)
- [`raw/c3_1c_warm_ttfi_200x5_per_claim.json`](raw/c3_1c_warm_ttfi_200x5_per_claim.json) & [`raw/c3_1c_warm_ttfi_200x5_batch20.json`](raw/c3_1c_warm_ttfi_200x5_batch20.json)
- [`raw/c3_2_density_per_claim.json`](raw/c3_2_density_per_claim.json) & [`raw/c3_2_density_batch64.json`](raw/c3_2_density_batch64.json)
- [`raw/c3_3a_mc5_warm4000_per_claim.json`](raw/c3_3a_mc5_warm4000_per_claim.json) & [`raw/c3_3a_mc5_warm4000_batch64.json`](raw/c3_3a_mc5_warm4000_batch64.json)
- [`raw/c3_3b_mc5_golden_small_per_claim.json`](raw/c3_3b_mc5_golden_small_per_claim.json) & [`raw/c3_3b_mc5_golden_small_batch20.json`](raw/c3_3b_mc5_golden_small_batch20.json)
- [`raw/c3_3c_mc5_cuj1_8000_per_claim.json`](raw/c3_3c_mc5_cuj1_8000_per_claim.json) & [`raw/c3_3c_mc5_cuj1_8000_batch64.json`](raw/c3_3c_mc5_cuj1_8000_batch64.json)
- [`raw/c3_3d_mc5_sdk_5000_claim_and_cleanup.json`](raw/c3_3d_mc5_sdk_5000_claim_and_cleanup.json)
