# GKE Campaign 2 — Parity & Peak Raw Performance Benchmark Report

> **Status:** Phase I (`T0` 3,000-Task SWE-bench Verified Parity Sweep) **Complete** (`24,000 / 24,000` tasks succeeded, `0` errors); Phase II (In-Cluster R2E-Gym, Tunix GRPO, Warm Burst Curve, 10k Soak) & Phase III (`T1` Secondary Boot Disk) **In Progress**.
>
> **Code Under Test:** `SandboxBatch` (`25/N`) + `agent-sandbox-rl` `claim_mode="batch"` (`Workstream B`), compared head-to-head against `claim_mode="per_claim"` on identical cluster state and against published `T0` GKE baselines (`RESULTS.md` rows `#10–#12`, `#15–#16`).

---

## Highest-Impact Headline Results (Phase I — 3,000 Real SWE-bench Verified gVisor Sandboxes on Stock GKE)

Across `500` real SWE-bench Verified repository images × `6` rollouts = **`3,000` gVisor sandboxes** (`concurrency=500`, `10 × e2-standard-32` sandbox nodes, `100%` stock GKE APF FlowSchemas, `0` task errors across all `8` runs = `24,000` tasks):

1. **Up to `4.88×` Faster End-to-End Wall-Clock Time (Matched `HEAD` `per_claim` → `batch`):**
   - **`sliding` (`3,000` sandboxes):** `433.13s` → **`88.76s`** (**`4.88×` faster**, goodput `6.93` → **`33.80` tasks/s**; **`6.80×` faster** than published `T0` baseline `603.8s`).
   - **`naive` (`3,000` sandboxes):** `403.63s` → **`87.40s`** (**`4.62×` faster**, goodput `7.43` → **`34.33` tasks/s**; **`6.72×` faster** than published `T0` baseline `587.7s`).
   - **`pipelined` (`3,000` sandboxes):** `256.98s` → **`96.57s`** (**`2.66×` faster**, goodput `11.67` → **`31.07` tasks/s**; **`5.25×` faster** than published `T0` record `507.4s`).
2. **Up to `7.24×` Lower `p99` Claim Tail Latency (`8.46×` Lower Max Tail):**
   - **`naive` (`n = 3,000` claims):** `p99` `139.66s` → **`19.30s`** (**`7.24×` reduction**); `max` `273.31s` → **`32.30s`** (**`8.46×` reduction**); `avg` `26.22s` → **`9.55s`** (**`2.75×`**); `p50` `21.91s` → **`10.45s`** (**`2.10×`**).
   - **`sliding` (`n = 3,000` claims):** `p99` `62.38s` → **`17.76s`** (**`3.51×` reduction**); `max` `261.53s` → **`33.67s`** (**`7.77×` reduction**); `avg` `18.02s` → **`8.99s`** (**`2.00×`**).
   - **`pipelined` (`n = 3,000` claims):** `p99` `39.06s` → **`17.13s`** (**`2.28×` reduction**).
3. **Up to `2.42×` Faster Time-to-First-Call (`TTFC = claim + process`):**
   - **`naive`:** mean TTFC `27.84s` → **`11.51s`** (**`2.42×` faster**).
   - **`sliding`:** mean TTFC `19.79s` → **`11.03s`** (**`1.79×` faster**).
   - **`pipelined`:** mean TTFC `13.31s` → **`11.22s`** (**`1.19×` faster**).
4. **`N → 1` Watch Multiplexing & Up to `4.26×` Fewer Client Sockets (`-76.5%`):**
   - Replaces `500` concurrent per-claim watch streams with **`1` shared label-filtered `SandboxClaims` watch** + **`1` fenced `Lease` heartbeat**.
   - Total driver open sockets (including active `pods/exec` channels at `concurrency=500`) dropped `520` → **`122`** (**`4.26×` fewer**, `-76.5%`) on `naive` and `650` → **`176`** (**`3.69×` fewer**, `-72.9%`) on `pipelined`.
5. **Fresh Batch Claims (`87.40s`) Outrun In-Place `git restore` Recycling (`429.24s`) by `4.91×`:**
   - Because `SandboxBatch` eliminates client/watch contention, provisioning `3,000` fresh isolated warm sandboxes (`87.40s`) is **`4.91×` faster** than serializing `2,500` in-place `git restore` resets inside gVisor (`14.62s` avg reset → `429.24s` wall), while still speeding up `naive+recycle` itself by **`1.21×`** (`517.77s` → `429.24s`) and cutting driver sockets **`2.59×`** (`326` → `126`).
6. **Core SDK Primitives (Complementing GKE Workload Results):**
   - `SandboxBatch` reduces per-sandbox Kubernetes API calls **`~5×`**, cuts `1,000`-sandbox bulk teardown **`13.7×`** via namespace `deletecollection`, and guarantees crash-safe orphan cleanup via **TLA+-verified `resourceVersion`-fenced `Lease` garbage collection**.

---

## Cluster & Methodology Parity (`T0` Specification)

| Component | Configuration |
|---|---|
| **GKE Cluster** | Regional GKE `1.37` (`us-central1`, 3 HA `kube-apiserver` replicas pre-sized to `gomaxprocs=16` = `48` total control-plane vCPUs; `1,449` nominal regional APF seats), legacy `kube-proxy`, private nodes |
| **Controller Pool (`ctrl-pool`)** | `2 × e2-standard-32` (`us-central1-a,b`), dedicated via `dedicated=ctrl:NoSchedule` (`2` replicas, `8` CPU / `16Gi` request, `0` restarts across entire campaign) |
| **Sandbox Pool (`gvisor-pool`)** | `10 × e2-standard-32` (`5/zone` in `us-central1-a,b`), `600 GB pd-ssd`, `maxPodsPerNode=256`, `gvisor` runtime, GKE Image Streaming (`GCFS`) enabled, 100%-node upfront cache hydration of all `500` SWE-bench Verified images |
| **Driver Pool (`default-pool`)** | `2 × n2-standard-16` (`us-central1-a,b`, `<0.5ms` intra-VPC RTT to `https://kubernetes.default.svc`) |
| **Phase I Auth & APF** | **100% stock GKE APF** (`0` custom `FlowSchema`s); driver authenticates via GCP IAM user identity (`FlowSchema = global-default`), isolating driver traffic from `agent-sandbox-controller` (`FlowSchema = service-accounts` → `workload-low`) |
| **Phase I Controller Flags** | `--sandbox-concurrent-workers=1000 --sandbox-claim-concurrent-workers=1000 --sandbox-warm-pool-concurrent-workers=500 --sandbox-template-concurrent-workers=100 --sandbox-warm-pool-max-batch-size=500 --kube-api-qps=500 --kube-api-burst=1000` |
| **Run Isolation & Telemetry** | Strict zero-resource drain (`0` pods, claims, warmpools, sandboxes, templates, daemonsets, leases) + `30s` settle before every single variant; succeeded-only `phases` strictly separated from failed-only `phases_err` |

---

## Phase I Complete Results — `T0` SWE-bench Verified (`500 × 6 = 3,000`, `c = 500`, `125m / 1Gi`)

### Table 1A — End-to-End Wall-Clock, Throughput, Sockets & Error Parity

| Strategy | Published `RESULTS.md` (`per_claim`) | Campaign 2 `per_claim` (`HEAD`) | Campaign 2 `batch` (`mif=64, rps=200`) | Speedup (`batch` vs `HEAD per_claim`) | Speedup (`batch` vs `RESULTS.md`) | Tasks `ok / err` (`per_claim` → `batch`) | Open Sockets (`per_claim` → `batch`) |
|---|---:|---:|---:|---:|---:|---:|---:|
| **`naive`** | `587.70s` (`#10`) / `669.00s` (`#15`) | `403.63s` (`7.43/s`) | **`87.40s` (`34.33/s`)** | **`4.62×`** | **`6.72×` (`#10`)** | `3000/0` → **`3000/0`** | `520` → **`122`** (`4.26×`) |
| **`sliding`** | `603.80s` (`#11`) | `433.13s` (`6.93/s`) | **`88.76s` (`33.80/s`)** | **`4.88×`** | **`6.80×` (`#11`)** | `3000/0` → **`3000/0`** | `521` → **`288`** (`1.81×`) |
| **`pipelined`** | `507.40s` (`#12`) | `256.98s` (`11.67/s`) | **`96.57s` (`31.07/s`)** | **`2.66×`** | **`5.25×` (`#12`)** | `3000/0` → **`3000/0`** | `650` → **`176`** (`3.69×`) |
| **`naive+recycle`** (`max_reuses=6`) | `677.90s` (`#16`, `798` claims) | `517.77s` (`5.79/s`, `892` claims) | **`429.24s` (`6.99/s`, `659` claims)** | **`1.21×`** | **`1.58×` (`#16`)** | `3000/0` → **`3000/0`** | `326` → **`126`** (`2.59×`) |

### Table 1B — Claim Latency, Exec (`process`), and Time-to-First-Call (`TTFC = claim + process`)

All latency figures below reflect **succeeded tasks only** (`tasks_err = 0`, `phases_err = {}` on every run):

| Variant | Mode | Claims (`n`) | `claim` `p50` (s) | `claim` `avg` (s) | `claim` `p99` (s) | `claim` `max` (s) | `process` `p50` (s) | `process` `avg` (s) | **TTFC `avg` (s)** (`claim+process`) | **TTFC `p50≈` (s)** | `wait_pool_ready` `p50` (s) |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| **`naive`** | `per_claim` | 3000 | `21.91` | `26.22` | `139.66` | `273.31` | `1.39` | `1.62` | `27.84` | `23.30` | `17.67` |
| **`naive`** | **`batch` (`mif=64`)** | 3000 | **`10.45`** (`2.10×`) | **`9.55`** (`2.75×`) | **`19.30`** (**`7.24×`**) | **`32.30`** (**`8.46×`**) | `1.35` | `1.96` | **`11.51`** (**`2.42×`**) | **`11.81`** | **`5.39`** |
| **`sliding`** | `per_claim` | 3000 | `12.03` | `18.02` | `62.38` | `261.53` | `1.58` | `1.77` | `19.79` | `13.61` | `10.51` |
| **`sliding`** | **`batch` (`mif=64`)** | 3000 | **`9.48`** (`1.27×`) | **`8.99`** (`2.00×`) | **`17.76`** (**`3.51×`**) | **`33.67`** (**`7.77×`**) | `1.53` | `2.04` | **`11.03`** (**`1.79×`**) | **`11.01`** | **`4.89`** |
| **`pipelined`** | `per_claim` | 3000 | `10.06` | `11.16` | `39.06` | `56.35` | `1.70` | `2.15` | `13.31` | `11.75` | `6.78` |
| **`pipelined`** | **`batch` (`mif=64`)** | 3000 | **`9.94`** (`1.01×`) | **`9.13`** (`1.22×`) | **`17.13`** (**`2.28×`**) | **`39.34`** (`1.43×`) | `1.52` | `2.09` | **`11.22`** (**`1.19×`**) | **`11.46`** | **`3.57`** |
| **`naive+recycle`** | `per_claim` | 892 | `1.99` | `2.16` | `4.60` | `8.61` | `0.008` | `0.010` | `2.17` | `2.00` | `5.05` |
| **`naive+recycle`** | **`batch` (`mif=64`)** | 659 | `3.17` | `2.85` | `5.01` | `6.46` | `0.008` | `0.010` | `2.86` | `3.18` | `5.08` |

*(For `naive+recycle`, gVisor `git restore` `reset` across `2,500` resets averaged `15.85s` on `per_claim` with `392` 30s-timeout quarantines vs `14.62s` on `batch` with `159` quarantines — `2.47×` fewer quarantines under `batch`.)*

---

## Forensic Calibration Verification: Why `HEAD per_claim` Beats July `RESULTS.md` While Preserving Exact Physics

Before running any `batch` arms, `per_claim` was calibrated against the published July `RESULTS.md` `RunReport`s (`#10–#12`, `#15–#16`):

1. **Exact Strategy Rank Order Reproduced:** `pipelined (256.98s)` < `naive (403.63s)` < `sliding (433.13s)` < `naive+recycle (517.77s)` matches `RESULTS.md`'s exact `#12 (507.4s)` < `#10 (587.7s)` < `#11 (603.8s)` < `#16 (677.9s)` ordering with `12,000 / 12,000` tasks `ok`.
2. **Sequential WAN `teardown` Accounted for `148–151s` in `#10`, `#15`, `#16`:** Prior to PR `#1314` (*"add batch fleet cleanup"*), `fleet.teardown()` deleted 500 warm pools + 500 templates sequentially over laptop WAN (`148.33s` on `#10`, `150.90s` on `#16` vs `0.99–1.05s` at `HEAD`). Subtracting `teardown`:
   - **`naive+recycle` execution wall:** `526.95s` (`#16`) vs **`516.78s`** (`HEAD per_claim`) — **within `1.9%`**, with gVisor `git restore` `reset.avg` **`15.66s` vs `15.85s` (`1.2%` apart)**!
   - **`naive` execution wall:** `439.36s` (`#10`) vs **`402.58s`** (`HEAD per_claim`) — **within `8.4%`**!
3. **Upstream `v0.5.3` → `v1.0.9+` Controller & SDK Improvements Benefit Both Arms Equally:** Controller HTTP/2 sharding + dedicated watch (`#1240`), direct merge patches removing redundant reconcile writes (`#1250`), `PodScheduled` mirroring (`#1439`), status patch predicates (`#1254`), and `AsyncSandboxFleet` thread-pool sizing (`815b517f`) lifted `per_claim` on `sliding`/`pipelined`, making our head-to-head `batch` speedups (`2.66×–4.88×`) strictly conservative.

---

## Raw Artifacts (`campaign-v2/raw/`)

- [`raw/p1_t0_swe3000_naive_per_claim.json`](raw/p1_t0_swe3000_naive_per_claim.json)
- [`raw/p1_t0_swe3000_naive_batch64.json`](raw/p1_t0_swe3000_naive_batch64.json)
- [`raw/p1_t0_swe3000_sliding_per_claim.json`](raw/p1_t0_swe3000_sliding_per_claim.json)
- [`raw/p1_t0_swe3000_sliding_batch64.json`](raw/p1_t0_swe3000_sliding_batch64.json)
- [`raw/p1_t0_swe3000_pipelined_per_claim.json`](raw/p1_t0_swe3000_pipelined_per_claim.json)
- [`raw/p1_t0_swe3000_pipelined_batch64.json`](raw/p1_t0_swe3000_pipelined_batch64.json)
- [`raw/p1_t0_swe3000_recycle_per_claim.json`](raw/p1_t0_swe3000_recycle_per_claim.json)
- [`raw/p1_t0_swe3000_recycle_batch64.json`](raw/p1_t0_swe3000_recycle_batch64.json)

---

## Upcoming Phases (Running Sequentially)

- **Phase II (In-Cluster Production Suite — Guardrail-Safe 3-Lane APF + Controller `P2`):**
  - `Bench 2A`: Standing-fleet warm burst latency curve (`c ∈ {100, 200, 500, 1000}`, `min_ready=1.0`, `per_claim` vs `batch mif=20` vs `batch mif=64`)
  - `Bench 0`: `20×2×2` smoke check (`per_claim` vs `batch`)
  - `Bench 3A` & `3B`: In-cluster SWE-bench `500×1` and `500×6` strategy isolation
  - `Bench 1A`: Real R2E-Gym `500×2×8 = 4,000` multi-step trajectories (`git status + git log + sleep 5`, `--enable-cow`)
  - `Bench 1B`: Tunix GRPO `64×16×8 = 8,192` trajectories (`c=1024`, `--enable-cow`)
  - `Bench 2B`: `10,000`-claim sustained `50 × 200` wave soak
- **Phase III (`T1` Secondary Boot Disk Ceiling):**
  - `10 × n2-standard-32` `gvisor-sbd-pool` with preloaded 600 GB SWE-bench `CONTAINER_IMAGE_CACHE` (`per_claim` vs `batch`).
