# GKE Scale Benchmark Summary: `claim_mode="batch"` vs. `per_claim`

**Environment:** GKE Standard `1.37` (`us-central1-a`), `gVisor` (`runsc`) + GKE Image Streaming (`gcfs`), Dataplane V2, NodeLocalDNS · `24–48 × n2-standard-8` sandbox nodes + isolated `controller-pool` (`e2-standard-8`) + isolated in-cluster Python driver (`n2-standard-8`) · Stationary control plane (`404` APF seats) · `103` total runs across identical `bench/rl-gke` controller & SDK code (`warm_per_task=True`, `max_concurrent=N`, `connection_pool_maxsize=1000`).

---

## 1. Production-Tuned Controller — Scale Sweep (`160 → 2,560` Warm Sandboxes, `N × 4`)

*Controller profile ([`docs/performance-tuning.md`](../../../../docs/performance-tuning.md#L508-L530) Sustained High-Throughput): `200` sandbox/claim workers, `2` warm-pool workers, `max-refill-rate=100`, `api-connections=4`, `separate-watch-connection=true`, `cache-label-selectors=true`, event/annotation suppression.*

| Scale (`Pools × 4`) | Claim Mode | Failed Envs | TTFC (1st Ready) | Median (`p50`) | Tail (`p99`) | All Ready (`last`) *(run1–run2)* | Client API Calls / Env | Open Watches | Dropped Sockets | Cluster Claim API Reqs |
|---|---|---:|---:|---:|---:|---|---:|---:|---:|---:|
| **160** (`40 × 4`) | `per_claim` | `0` | `516 ms` | `1.37s` | `2.44s` | `2.48s` *(2.43–2.53s)* | `4.00` | `160` | `0` | `1,224` |
| **160** (`40 × 4`) | **`batch` (default)** | **`0` ✅** | **`136 ms` (`3.8×` ⚡)** | **`1.18s` (`1.16×`)** | **`2.33s`** | **`2.34s` (`1.06×`)** *(2.32–2.37s)* | **`1.26` (`3.2×` ↓)** | **`1` (`160×` ↓)** | `0` | **`747` (`-39%` ↓)** |
| **160** (`40 × 4`) | **`batch` (unbounded)** | **`0` ✅** | **`214 ms` (`2.4×` ⚡)** | **`1.15s` (`1.19×`)** | **`2.24s`** | **`2.38s`** *(2.37–2.40s)* | **`1.26` (`3.2×` ↓)** | **`1` (`160×` ↓)** | `0` | **`742` (`-39%` ↓)** |
| **640** (`160 × 4`) | `per_claim` | `0` | `3,120 ms` | `5.36s` | `19.30s` | `19.54s` *(9.71–29.37s)* | `4.00` | `640` | `0` | `4,735` |
| **640** (`160 × 4`) | **`batch` (default)** | **`0` ✅** | **`603 ms` (`5.2×` ⚡)** | **`4.78s` (`1.12×`)** | **`8.50s` (`2.27×` ⚡)** | **`8.73s` (`2.24×` ⚡✅)** *(8.68–8.78s)* | **`1.25` (`3.2×` ↓)** | **`1` (`640×` ↓)** | `0` | **`2,840` (`-40%` ↓)** |
| **640** (`160 × 4`) | **`batch` (unbounded)** | **`0` ✅** | **`758 ms` (`4.1×` ⚡)** | **`4.81s` (`1.11×`)** | `18.65s` | `18.95s` *(8.23–29.68s)* | **`1.25` (`3.2×` ↓)** | **`1` (`640×` ↓)** | `0` | **`2,806` (`-41%` ↓)** |
| **1,280** (`320 × 4`) | `per_claim` | `0` | `317 ms` | `9.18s` | `19.60s` | `19.71s` *(18.47–20.96s)* | `4.00` | `1,280` | `0` | `9,388` |
| **1,280** (`320 × 4`) | **`batch` (default)** | **`0` ✅** | `1,339 ms` | `10.20s` | `24.04s` | `24.59s` *(30.61s / **18.57s**)*¹ | **`1.25` (`3.2×` ↓)** | **`2` (`640×` ↓)** | `0` | **`5,529` (`-41%` ↓)** |
| **1,280** (`320 × 4`) | **`batch` (unbounded)** | **`0` ✅** | `358 ms` | **`8.47s` (`1.08×`)** | **`19.16s`** | `20.14s` *(**16.86s**–23.41s)* | **`1.25` (`3.2×` ↓)** | **`1` (`1,280×` ↓)** | `0` | **`5,502` (`-41%` ↓)** |
| **2,560** (`640 × 4`) | `per_claim` | **`1` ❌** | `273 ms` | `17.94s` | `33.96s` | `57.29s` *(clean run 1; run 2 429 err)*² | `4.00` | `2,560` | **`551` ❌** | `18,317` |
| **2,560** (`640 × 4`) | **`batch` (default)** | **`0` ✅** | **`194 ms` (`1.4×` ⚡)** | **`16.53s` (`1.09×`)** | **`33.67s`** | **`47.40s` (`1.36×` ⚡ run 1: `41.98s`)** | **`1.25` (`3.2×` ↓)** | **`2` (`1,280×` ↓)** | **`0` ✅** | **`10,668` (`-42%` ↓)** |
| **2,560** (`640 × 4`) | **`batch` (unbounded)** | **`0` ✅** | `601 ms` | **`16.91s` (`1.06×`)** | `37.58s` | `51.58s` (**`1.61×` ⚡ run 1: `35.48s`**) | **`1.13` (`3.5×` ↓)** | **`2` (`1,280×` ↓)** | **`0` ✅** | **`10,757` (`-41%` ↓)** |

¹ *At `1,280 / batch (default)`, Run 2 (`0` HTTP `429`s) finished in **`18.57s` (`p99 = 18.09s`)**; Run 1 reached **`p90 = 14.62s`** (faster than `per_claim`'s `15.84s`) before mid-step warm-pool refill writes caused `44` server-side HTTP `429` retries on the final `1%` (`30.61s`).*
² *At `2,560 / per_claim`, Run 1 (`0` errors) required **`57.29s`**; Run 2 overflowed the `1,000`-socket pool (`551` dropped sockets) and failed an environment with `ApiException: (429) Too Many Requests` (`34.80s` over `2,559` surviving sandboxes).*

---

## 2. Production-Tuned Controller — Workload Shape Comparison (`500 × 1` Eval & `16 × 64` High-Rollout)

| Workload Shape (`Pools × Tasks/Pool`) | Claim Mode | Failed Envs | TTFC (1st Ready) | Median (`p50`) | Tail (`p99`) | Steady All Ready (`last`) | **Step 1 Cold Ready** | Client API Calls / Env | Open Watches | Dropped Sockets | Cluster Claim API Reqs |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| **SWE-bench Eval (`500 × 1 = 500`)** | `per_claim` | `0` | `738 ms` | `4.05s` | `6.93s` | `7.27s` | `33.51s` | `4.00` | `500` | `0` | `3,844` |
| **SWE-bench Eval (`500 × 1 = 500`)** | **`batch` (default)** | **`0` ✅** | **`280 ms` (`2.6×` ⚡)** | **`3.71s` (`1.09×`)** | `7.46s` | `7.62s` | **`7.95s` (`4.2×` ⚡✅)** | **`2.01` (`2.0×` ↓)** | **`4` (`125×` ↓)** | `0` | **`2,294` (`-40%` ↓)** |
| **SWE-bench Eval (`500 × 1 = 500`)** | **`batch` (unbounded)** | **`0` ✅** | **`306 ms` (`2.4×` ⚡)** | `4.40s` | **`6.91s`** | **`7.01s` (`1.04×`)** | **`6.75s` (`5.0×` ⚡✅)** | **`2.01` (`2.0×` ↓)** | **`4` (`125×` ↓)** | `0` | **`2,342` (`-39%` ↓)** |
| **High-Rollout (`16 × 64 = 1,024`)** | `per_claim` | **`101` ❌** | `4,108 ms` | `8.73s` | `32.41s` | `44.30s` *(clean step)*³ | `34.93s` *(`99` errs ❌)* | `4.00` | `1,023` | **`24` ❌** | `8,676` |
| **High-Rollout (`16 × 64 = 1,024`)** | **`batch` (default)** | **`0` ✅** | **`199 ms` (`20.6×` ⚡)** | **`7.05s` (`1.24×` ⚡)** | **`31.74s`** | **`35.82s` (`1.24×` ⚡✅)** | **`31.08s` (`1.12×` ✅)** | **`1.02` (`3.9×` ↓)** | **`1` (`1,023×` ↓)** | **`0` ✅** | **`4,555` (`-47%` ↓)** |
| **High-Rollout (`16 × 64 = 1,024`)** | **`batch` (unbounded)** | **`0` ✅** | **`448 ms` (`9.2×` ⚡)** | **`7.09s` (`1.23×` ⚡)** | **`24.42s` (`1.33×` ⚡)** | **`33.83s` (`1.31×` ⚡✅)** | `38.50s` *(`0` errs ✅)* | **`1.02` (`3.9×` ↓)** | **`3` (`341×` ↓)** | **`0` ✅** | **`4,316` (`-50%` ↓)** |

³ *At `16 × 64 / per_claim`, Run 1 Step 1 dropped `99` environments (`0 / 16` complete groups), Run 1 Step 2 (`0` errors) took `44.30s`, and Run 2 Step 2 dropped `2` environments (`30.48s` partial; `37.39s` raw 2-run median).*

---

## 3. Out-of-the-Box Default Controller — Warm (`160 → 1,280`) & Cold (`640`)

*Controller profile: Stock release defaults (`100` sandbox workers, `50` claim workers, `1` warm-pool worker, `1` shared API connection).*

| Scale (`Pools × 4`) | Claim Mode | Failed Envs | TTFC (1st Ready) | Median (`p50`) | Tail (`p99`) | All Ready (`last`) | Client API Calls / Env | Open Watches | Dropped Sockets | Cluster Claim API Reqs |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| **160 Warm** (`40 × 4`) | `per_claim` | `0` | `553 ms` | `1.28s` | `2.17s` | `2.36s` | `4.00` | `160` | `0` | `1,022` |
| **160 Warm** (`40 × 4`) | **`batch` (default)** | **`0` ✅** | **`178 ms` (`3.1×` ⚡)** | **`1.12s` (`1.14×`)** | **`2.10s`** | **`2.12s` (`1.11×`)** | **`1.26` (`3.2×` ↓)** | **`1` (`160×` ↓)** | `0` | **`540` (`-47%` ↓)** |
| **160 Warm** (`40 × 4`) | **`batch` (unbounded)** | **`0` ✅** | **`243 ms` (`2.3×` ⚡)** | `1.29s` | `2.14s` | **`2.16s` (`1.09×`)** | **`1.25` (`3.2×` ↓)** | **`0–1`** | `0` | **`531` (`-48%` ↓)** |
| **640 Warm** (`160 × 4`) | `per_claim` | `0` | `1,300 ms` | `5.49s` | `10.60s` | `14.37s` | `4.00` | `640` | `0` | `5,862` |
| **640 Warm** (`160 × 4`) | **`batch` (default)** | **`0` ✅** | **`338 ms` (`3.8×` ⚡)** | **`5.17s` (`1.06×`)** | **`9.26s` (`1.14×`)** | **`9.39s` (`1.53×` ⚡✅)** | **`1.25` (`3.2×` ↓)** | **`1` (`640×` ↓)** | `0` | **`3,404` (`-42%` ↓)** |
| **640 Warm** (`160 × 4`) | **`batch` (unbounded)** | **`0` ✅** | **`660 ms` (`2.0×` ⚡)** | **`5.38s`** | **`10.14s`** | **`10.34s` (`1.39×` ⚡✅)** | **`1.25` (`3.2×` ↓)** | **`0–1`** | `0` | **`3,678` (`-37%` ↓)** |
| **1,280 Warm** (`320 × 4`) | `per_claim` | `0`⁴ | `14,844 ms` | `20.91s` | `31.19s` | `31.51s` | `4.00` | `1,280` | **`218` ❌** | `10,610` |
| **1,280 Warm** (`320 × 4`) | **`batch` (default)** | **`0` ✅** | **`661 ms` (`22.5×` ⚡)** | **`9.76s` (`2.14×` ⚡)** | **`19.02s` (`1.64×` ⚡)** | **`19.19s` (`1.64×` ⚡✅)** | **`1.25` (`3.2×` ↓)** | **`1` (`1,280×` ↓)** | **`0` ✅** | **`6,719` (`-37%` ↓)** |
| **1,280 Warm** (`320 × 4`) | **`batch` (unbounded)** | **`0` ✅** | **`756 ms` (`19.6×` ⚡)** | **`9.82s` (`2.13×` ⚡)** | **`18.58s` (`1.68×` ⚡)** | **`18.80s` (`1.68×` ⚡✅)** | **`1.33` (`3.0×` ↓)** | **`1` (`1,280×` ↓)** | **`0` ✅** | **`6,634` (`-37%` ↓)** |
| **640 Cold** (`0`-replica pools) | `per_claim` | `0` | `5,639 ms` | `16.92s` | `25.39s` | `27.04s` | `4.00` | `640` | `0` | `6,770` |
| **640 Cold** (`0`-replica pools) | **`batch` (unbounded)** | **`0` ✅** | **`3,049 ms` (`1.85×` ⚡)** | **`16.17s` (`1.05×`)** | **`24.55s`** | **`25.28s` (`1.07×`)** | **`1.25` (`3.2×` ↓)** | **`1` (`640×` ↓)** | `0` | **`4,765` (`-30%` ↓)** |

⁴ *`1,280 / per_claim` failed `14` environments with HTTP `429` on its initial attempt before succeeding on retry.*

---

## 4. Managed GKE Warm-Pool Worker Finding (`w640` = `160` Pools × `4`)

| Controller Warm-Pool Concurrency | `batch (default)` All Ready | Server APF Queue Wait (`apf_wait_s`) | Controller `PATCH` Latency |
|---|---:|---:|---:|
| **Default (`warm-pool-workers=1`)** | `9.39s` | `9.5s – 22.3s` | `42 ms` |
| **Perf-Tuning Guide (`warm-pool-workers=2`, `rate=100`)** | **`8.73s` (`1.41×` ⚡ vs `wp=100`)** | `497.8s` | **`75 ms` (`0` HTTP `429`s ✅)** |
| **RL README (`warm-pool-workers=100`, `rate=100`)** | `12.30s` | **`1,043s – 1,473s` (`155×` ↑ ❌)** | **`290 ms` (`6.9×` slower + `49–104` `429`s ❌)** |
