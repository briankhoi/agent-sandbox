# Campaign 4 (`campaign-v4`) Executive Summary & Slide Guide

> [!IMPORTANT]
> **Unified Code & Fairness Provenance (`bb5c3fbd`):**
> - **Branch & Commit:** [`briankhoi/agent-sandbox` `feat/rl-batch-hardening` (`bb5c3fbd7ec6e94f6d96a7e6a544e60bdaae55ce`)](https://github.com/briankhoi/agent-sandbox/tree/bb5c3fbd7ec6e94f6d96a7e6a544e60bdaae55ce)
> - **Controller Image:** `us-central1-docker.pkg.dev/<GCP_PROJECT>/rl-gke-bench/agent-sandbox-controller@sha256:8d11e685651cfb17e406d3440256dedb8317c219a7fe80a789999290d3161ab1`
> - **Driver Image:** `us-central1-docker.pkg.dev/<GCP_PROJECT>/rl-gke-bench/asrl-driver@sha256:faa997cf22194fb81ed0833f54b8fa34469e746e6ac32c149f6f19df9441d38c`
> - **Fairness Guarantees:** Identical `1000/500` controller workers, identical `go_sched_gomaxprocs_threads = 16` regional GKE 1.37 control planes, `ABABAB` interleaved `3`-repetition medians on fast sweeps/bursts, verified `0` residual objects + `30s` settle before every run, and `per_claim` code paths `100%` untouched.
> - **Live Status:** Complete: `#1`, `#2`, `#4A`, `#4C`, `#5A`, `#5B`, `#5C`, `#6`, `#7A`, `#7B`, `#7C`, **`#7D`**. `#4B` clean rerun complete (supersedes the original, whose `batch64` Step 0 had `893/1,024` probe errors). Running: `#3A/#3B/#3C`, `#4-SWE` on the existing SBD cluster (non-canonical hardware, labeled as such).

> [!WARNING]
> **Comparability scope (audit 2026-11-09):** Every `per_claim` vs `batch64` ratio in this file is a same-cluster, back-to-back A/B and is valid. **Absolute numbers are NOT directly comparable to published `sandbox-rl-performance` `RESULTS.md` rows**, whose canonical env is `10× e2-standard-32`, pd-ssd 600 GB, GKE `1.36.0-gke.3712000` (kernel 6.12), controller `1000/50`, and on T1 patched gcfsd/Riptide. Campaign 4 ran GKE `1.37.1-gke.2097000` (kernel 6.18) on:
> - `#1`/`#2` (T0): `10× e2-standard-32`, pd-balanced 1,500 GB (`#1`) / 200 GB (`#2`), stock controller.
> - `#4A`/`#4B`/`#4C`: `10× n2-standard-32`, pd-balanced 1,500 GB, tuned controller.
> - `#3`/`#5`/`#6`/`#7` ("T1"): `10× n2-standard-32`, pd-standard 100 GB boot + 600 GB SBD, tuned controller (`sandbox=1000`, `warmpool=500`, refill 500, custom 3-lane APF), **no patched gcfsd** — i.e. not T1 per the README definition.
> - System pools on every cluster: `2× n2-standard-16` (driver) + `2× e2-standard-32` (controller).
>
> Our own `per_claim` baseline beats `RESULTS.md` `#10–#12` by ~5× on `#1`, so that gap is environment, not SandboxBatch. `#7B` is the only suite that exactly matches a reference shape (Golden `Small`, deck Slide 52). See [REFERENCE_AUDIT.md](REFERENCE_AUDIT.md) for the full suite→reference map.


---

## 1. Top Presentation Slide Numbers (`batch64` vs `per_claim` + Eval/Milestone Bars)

| # | Workload & Scale | `HEAD per_claim` | **`SandboxBatch` (`batch64`)** | Headline Speedup & Milestone / Eval Hit |
| --- | --- | --- | --- | --- |
| **1** | **Multi-Step Synchronous RL (`#4A`)**<br>`8,000` Real R2E-Gym Trajectories (`500` repos × `2` × `8` steps) | `1,302.11s` (`21.7m`)<br>`175.27s` steady step barrier<br>`151.22s` `env_ready` P50 | **`342.05s` (`5.7m`)**<br>**`32.38s` steady step barrier**<br>**`15.67s` `env_ready` P50** | • **`3.81×` faster end-to-end RL wall-clock**<br>• **`5.41×` faster steady GPU step barrier** (`Steps 1–7`)<br>• **`9.65×` faster `env_ready` P50** · **`7.09×` faster P99**<br>• **`4.90×` fewer sockets** (`505 → 103`) · **`0.000%` (`0/8,000`) errors** |
| **2** | **`5`-Cluster Golden `Small` Multi-Job SWE-bench (`#7B`)**<br>`8` Jobs × `(128×4 = 512) = 4,096` Concurrent (`3`-rep median) | `53.45s` all-jobs wall<br>`42.06s` job step barrier P50<br>`17.11s` wire claim P50 | **`29.21s` all-jobs wall**<br>**`19.48s` job step barrier P50**<br>**`3.43s` wire claim P50** | • **Exact Golden Dataset Tier 1 `Small` (`4,096` SWE-bench)**<br>• **`1.83×` faster all-jobs wall** · **`2.16×` faster job step barrier**<br>• **`4.99×` (`~5.0×`) faster Claim P50** (`2.60×` faster Claim P99)<br>• **`2.13×` faster TTFI P50** (`2.87×` inline) · **`0.000%` (`12,288/12,288`) errors** |
| **3** | **`5`-Cluster `4,000` & `8,000` Warm SWE-bench Burst (`#7A` & `#7C`)**<br>`1k → 2k → 4k` (`#7A`) & `8,000` Ceiling (`#7C`, `3`-rep median) | `8k`: `87.14s` claim wall (`961/s` rel)<br>`8k`: `42.56s` Claim P50 / `88.31s` TTFI<br>`4k`: `1,092/s` rel · `~2,343` ctrl socks | **`8k`: `37.37s` claim wall (`3,852/s` rel!)**<br>**`8k`: `14.48s` Claim P50 / `40.15s` TTFI**<br>**`4k`: `3,608/s` rel · `~16` ctrl socks!** | • **Clears Golden `Small` `>3,000 env/s` reset bar** (`4.01×` faster at `8k`: `3,852/s` fleet / `770/s/cl`; `3.30×–3.69×` at `1k–4k`)<br>• **`2.33×` faster `8k` claim wall (`214.1/s` fleet / `42.8/s/cl`) · `2.94×` faster Claim P50**<br>• **`146×` fewer control-plane sockets** at `4k` · **`0.000%` (`45,000/45,000`) errors** |
| **4** | **Standing `1,000`-Pod Warm `TTFI` Curves (`#5A` & `#5B`)**<br>`1×1,000` & `200×5` High-Cardinality Pools (`c = 100..1,000`, `3`-rep median) | `88.6–108.5/s` at `c=1k`<br>`9.28s` (`#5A`) / `10.74s` (`#5B`) TTFI P50<br>`11,436` driver K8s API requests | **`192.3/s` (`#5A`) / `186.0/s` (`#5B`) peak**<br>**`3.94s` (`#5A`) / `4.05s` (`#5B`) TTFI P50**<br>**`1,785` driver K8s API requests** | • **`6.41×` fewer K8s API HTTP requests** (`apiserver_flowcontrol_dispatched_requests_total`)<br>• **`2.36×–2.66×` faster TTFI P50** & **`3.07×` faster First TTFI** at `c=1,000`<br>• **Hits M2 `<1s` TTFI** at `c=100` (`0.69s–0.73s` P50) & **Eval `Pass` `<5s` P99** through `c=500` (`3.45s`) |
| **5** | **Active gVisor Compute Packing Density (`#6`)**<br>`640` (`2/vCPU`), `1,600` (`5/vCPU`), `2,350` (`7.34/vCPU`, `3`-rep median) | `86.3–94.7/s` claim rate<br>`4.997/vCPU` (`3` errs at `c=1,600`)<br>`15.62s` TTFI P50 at `7.34/vCPU`<br>`1,309` held sockets (`~797` ctrl) | **`141.9–157.4/s` claim rate**<br>**`5.000/vCPU` & `7.344/vCPU` (`0` errs)**<br>**`8.21s` TTFI P50 at `7.34/vCPU`**<br>**`535` held sockets (`~23` ctrl)** | • **Clears Eval `Pass = 5.0/vCPU` (`4,800/4,800` OK)** & **`7.34/vCPU` (`7,050/7,050` OK, `0` OOMs)** up to GKE `256`-pod/node cap<br>• **`1.72×–1.82×` faster claim rate** · **`1.89×–2.01×` faster TTFI P50**<br>• **`~34×` fewer control-plane sockets** (`~23` vs `~797`) |
| **6** | **`20,000`-Claim Sustained Reliability Soak (`#5C`)**<br>`100` Waves × `200` Claims on `200` Pools (`1` Python Process) | `1.138s` aggregate P50<br>`168.3 ms` 1st Ready<br>`187` steady sockets | **`0.560s` (`560 ms`) aggregate P50**<br>**`94.4 ms` 1st Ready**<br>**`68` flat sockets (`230.8/s` peak)** | • **`20,000 / 20,000` (`0.000% < 0.01%` Eval `Small` failure bar)**<br>• **`2.03×` faster P50 claim latency** across `20k` claims<br>• **`1.78×` faster 1st Ready (`<100 ms`)** · **`2.75×` fewer sockets** (`0` drift) |
| **7** | **Canonical Stock `T0` Warm SWE-bench `3,000` (`#1`)**<br>`500` Images × `6` Rollouts (`wp=1`, `ABABAB` `3`-rep median) | `naive`: `121.09s` (`329.1s` `r1`)<br>`sliding`: `87.03s` (`551` socks)<br>`pipelined`: `100.07s` (`546` socks) | **`naive`: `96.54s` (`1.25×`)**<br>**`sliding`: `90.86s` (`99` socks)**<br>**`pipelined`: `98.14s` (`90` socks)** | • **`1.25×` faster `naive` median** <br>• **`4.07×–6.07×` fewer open sockets** (`484–551 → 90–119`)<br>• Published `RESULTS.md` `#10–#12` (`507–604s`) ran a different env (see warning above) — not a SandboxBatch comparison; fresh `batch64` (`96.5s`) beats in-place `git restore` recycle (`393.5s`) by **`4.08×`** |

---

## 2. Quick Glossary: Milestones vs. Evals vs. Baselines

- **Product Milestones (`M1 / M2 / M3` on `slide20c`):** Engineering scale roadmap targets:
  - **TTFI:** `M1 < 3s`, **`M2 < 1s`** (`batch64` hits **`0.69s` P50 / `0.98s` P99** at `c=100` and **`0.56s` P50** over `20,000` soak claims), `M3 = 500ms`.
  - **Create / Reset Throughput:** `M1 = 500/s`, `M2 = 1,000/s` (`#7D` pure `asyncio` SDK running now; `#7A` `DELETE_COLLECTION` reset already hits **`3,146–3,724/s` fleet / `629–745/s` per cluster**).
  - **Compute Density:** `M1 = 1.5/vCPU`, Nov 16 Target = `2.0/vCPU` (`batch64` verified at **`2.0`, `5.0`, and `7.344 sandboxes/vCPU`**).
- **Customer Eval Gates (`Pass` / `Excellent` in `go/agentic-rl-sandbox-eval`):**
  - **Step Failure Rate:** `Pass < 0.1%`, **`Small < 0.01%`** (`batch64` achieved **`0.000%`** across `#1` `9,000`, `#2` `500`, `#4A` `8,000`, `#5A/#5B` `11,100`, `#5C` `20,000`, `#6` `13,770`, `#7A` `21,000`, `#7B` `12,288`, and `#7C` `24,000` = **`119,658 / 119,658` tasks with `0` failures**!).
  - **Warm `TTFI`:** **`Pass`: `Mean < 3s, P99 < 5s`** (`batch64` passes through `c=500` on both `1×1,000` and `200×5` high-cardinality pools: `2.18s–2.36s` Mean, `3.45s` P99, whereas `per_claim` misses `P99 < 5s` at `c=500` on `200×5` at `5.39s`).
  - **Compute Density:** **`Pass = 5.0/vCPU`** (`batch64` hits `5.000/vCPU` with `0` errors across 3 reps; `7.344/vCPU` hits the `256`-pod/node kubelet slot ceiling on `32`-vCPU VMs).
  - **Golden Dataset Tier 1 `Small` (Slide 52):** Exact `500` `SWE-bench Verified` images, `4,000–4,096` concurrent (`8` jobs × `128×4`), **`>3,000 env/s` reset** (`#7A` hits **`3,608–3,724/s`** and `#7C` hits **`3,852/s` (`4,334/s` peak)**; `#7B` cuts `4,096`-sandbox Claim P50 **`4.99×`** and all-jobs wall **`1.83×`**).

---

## 3. Detailed Suite Tables (All Completed Runs Through Wave 2 `#7C`)

### 3.1 Suite `#4A` — `8,000` Real R2E-Gym 8-Step Synchronous-Barrier RL (`T1`)

| Arm | Total Wall (`8` Steps) | Goodput | Mean Step Wall (`0–7`) | Steady Step Barrier (`1–7`) | Steady `env_ready` `P50` / `P99` | Peak Sockets | Failures |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `per_claim` | `1,302.11s` (`21.7m`) | `6.14/s` | `161.93s` | `175.27s` | `151.22s` / `159.15s` | `505` | `0 / 8,000` |
| **`batch64`** | **`342.05s` (`5.7m`)** | **`23.39/s`** | **`41.96s`** | **`32.38s`** (`25.3s` best) | **`15.67s` / `22.44s`** | **`103`** | **`0 / 8,000`** |
| **Speedup** | **`3.81×`** | **`3.81×`** | **`3.86×`** | **`5.41×`** | **`9.65×` P50 / `7.09×` P99** | **`4.90×` fewer** | `0.000%` |

### 3.2 Suite `#7B` — `5`-Cluster Golden `Small` Multi-Job SWE-bench (`8 × 512 = 4,096` Concurrent, `ABABAB` 3-Rep Median)

| Arm | All-Jobs Wall (`r1 \| r2 \| r3 \| med`) | Fleet Goodput (`÷5`/cl) | Job Barrier `P50` / `Max` | Wire Claim `P50` / `P99` | Paired TTFI `P50` / `P99` | Inline TTFI `P50` | Failures (`3` reps) |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `per_claim` | `53.3 \| 61.3 \| 53.5 \|` `53.45s` | `76.6/s` (`15.3/s`) | `42.06s` / `42.78s` | `17.11s` / `30.64s` | `24.46s` / `34.73s` | `22.15s` | `0 / 12,288` |
| **`batch64`** | `29.2 \| 29.1 \| 30.1 \|` **`29.21s`** | **`140.2/s` (`28.0/s`)** | **`19.48s` / `21.33s`** | **`3.43s` / `11.77s`** | **`11.49s` / `16.11s`** | **`7.72s`** | **`0 / 12,288`** |
| **Speedup** | **`1.83×` faster** | **`1.83×`** | **`2.16×` P50 / `2.01×` Max** | **`4.99×` P50 / `2.60×` P99** | **`2.13×` P50 / `2.16×` P99** | **`2.87×`** | `0.000%` |

### 3.3 Suite `#7A` — `5`-Cluster `4,000` Standing Warm SWE-bench Burst (`1k / 2k / 4k`, `3`-Rep Median)

| Concurrency | Arm | Claim Rate (Fleet / `÷5`) | TTFI `First` / `P50` / `P99` | Bulk Release (Fleet / `÷5`) | Release Wall | Held Sockets (`-1,024` exec) |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| **`c = 1,000`** | `per_claim` | `81.3/s` (`16.3/s`) | `4.20s` / `8.27s` / `12.31s` | `852.0/s` (`170.4/s`) | `1.174s` | `876` |
| | **`batch64`** | **`161.6/s` (`32.3/s`) (`1.99×`)** | **`3.53s` / `4.83s` (`1.71×`) / `6.21s` (`1.98×`)** | **`3,146.0/s` (`629.2/s`) (`3.69×`)** | **`0.318s`** | `876` |
| **`c = 2,000`** | `per_claim` | `104.8/s` (`21.0/s`) | `19.08s` / `23.03s` / `25.43s` | `1,067.0/s` (`213.4/s`) | `1.874s` | `1,739` |
| | **`batch64`** | **`158.6/s` (`31.7/s`) (`1.51×`)** | **`6.58s` (`2.90×`) / `8.86s` (`2.60×`) / `10.67s` (`2.38×`)** | **`3,723.7/s` (`744.7/s`) (`3.49×`)** | **`0.537s`** | **`1,248`** |
| **`c = 4,000`** | `per_claim` | `100.0/s` (`20.0/s`) | `39.05s` / `47.37s` / `52.15s` | `1,092.3/s` (`218.5/s`) | `3.662s` | `3,367` (`~2,343` ctrl) |
| | **`batch64`** | **`150.2/s` (`30.1/s`) (`1.50×`)** | **`10.75s` (`3.63×`) / `20.74s` (`2.28×`) / `26.73s` (`1.95×`)** | **`3,608.1/s` (`721.6/s`) (`3.30×`)** | **`1.109s`** | **`1,040` (`~16` ctrl = `146×` fewer!)** |

### 3.4 Suites `#5A` (`1×1,000`) & `#5B` (`200×5` High-Cardinality) — Single-Cluster Warm `TTFI` Curves (`3`-Rep Median)

| Pool Shape | Concurrency | `per_claim` Rate | **`batch64` Rate** | `per_claim` TTFI `P50` / `P99` | **`batch64` TTFI `P50` / `P99`** | `batch64` Gains |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| **`#5A` (`1×1,000`)** | `c = 100` | `100.8/s` | **`192.3/s` (`1.91×`)** | `1.225s` / `1.461s` | **`0.688s` / `0.976s`** | **`1.78×` P50 (`<1s` M2!)** · Claim P50 **`2.86×`** |
| (`1,785` vs `11,436` | `c = 250` | `99.0/s` | **`175.7/s` (`1.78×`)** | `2.115s` / `2.737s` | **`1.354s` / `1.865s`** | **`1.56×` P50 · `1.47×` P99** |
| driver API calls = | `c = 500` | `91.9/s` | **`158.7/s` (`1.73×`)** | `3.760s` / `4.707s` | **`2.143s` / `3.449s`** | **`1.75×` P50 · `1.36×` P99 (`<5s` Pass!)** |
| **`6.41×` fewer!**) | `c = 1,000` | `88.6/s` (`1` err) | **`93.9/s` (`0` err)** | `9.280s` / `11.754s` | **`3.938s` / `5.639s`** | **`2.36×` P50 · `2.08×` P99 · `1,328/s` rel (`1.26×`)** |
| **`#5B` (`200×5`)** | `c = 100` | `107.9/s` | **`153.1/s` (`1.42×`)** | `1.154s` / `1.501s` | **`0.726s` / `1.019s`** | **`1.59×` P50 (`<1s` M2!)** · Claim P50 **`1.92×`** |
| (`200` distinct | `c = 250` | `98.8/s` | **`186.0/s` (`1.88×`)** | `2.309s` / `2.889s` | **`1.316s` / `1.867s`** | **`1.75×` P50 · `1.55×` P99** |
| WarmPools) | `c = 500` | `92.4/s` | **`156.6/s` (`1.69×`)** | `3.860s` / `5.385s` | **`2.440s` / `3.449s`** | **`1.58×` P50 · `1.56×` P99 (`<5s` Pass!)** |
| | `c = 1,000` | `108.5/s` (`1` err) | **`151.5/s` (`1.40×`, `0` err)** | `10.742s` / `12.286s` | **`4.045s` / `5.361s`** | **`2.66×` P50 · `3.07×` First · `1,539/s` rel (`1.38×`)** |

### 3.5 Suite `#6` — Active gVisor Packing Density (`2.0 → 5.0 → 7.344 / vCPU`, `ABABAB` 3-Rep Median)

| Density (`320` vCPUs) | `per_claim` Rate & Errors | **`batch64` Rate & Errors** | `per_claim` TTFI `P50` / `P99` | **`batch64` TTFI `P50` / `P99`** | Held Sockets (`per_claim` → `batch64`) |
| --- | --- | --- | ---: | ---: | --- |
| **`2.0 / vCPU` (`c = 640`)** | `94.7/s` (`0` err) | **`141.9/s` (`1.50×`, `0` err)** | `4.97s` / `6.75s` | **`2.47s` (`2.01×`) / `4.33s` (`<5s` Pass!)** | `745 → 636` (`56` vs `126` post-release) |
| **`5.0 / vCPU` (`c = 1,600`)** | `86.3/s` (`4.997/vCPU`, `3` errs) | **`157.4/s` (`1.82×`, `5.000/vCPU`, `0` err)** | `10.91s` / `13.43s` | **`5.76s` (`1.89×`) / `8.71s` (`1.54×`)** | `1,090 → 786` (`68` vs `213` post-release) |
| **`7.34 / vCPU` (`c = 2,350`)** | `90.6/s` (`0` err) | **`155.8/s` (`1.72×`, `0` err, `0` OOMs)** | `15.62s` / `20.09s` | **`8.21s` (`1.90×`) / `11.61s` (`1.73×`)** | `1,309 → 535` (**`~34×` fewer ctrl sockets**: `~23` vs `~797`) |


### 3.6 Suite `#7C` — `5`-Cluster `8,000`-Sandbox Warm SWE-bench Fleet Ceiling (`500` Images × `16`, `3`-Rep Median)

| Arm | Claim Wall (`r1 \| r2 \| r3 \| med`) | Claim Rate (Fleet / `÷5`) | Claim `P50` / `P99` | TTFI `P50` / `P99` (Inline `P50`) | Bulk Release (Fleet / `÷5`) | Release Wall | Failures (`3` reps) |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `per_claim` | `68.8 \| 87.1 \| 109.2 \|` `87.14s` | `91.8/s` (`18.4/s`) | `42.56s` / `82.57s` | `88.31s` / `108.18s` (`56.85s`) | `961.3/s` (`192.3/s`) | `8.322s` | `0 / 24,000` |
| **`batch64`** | `28.7 \| 37.4 \| 52.5 \|` **`37.37s`** | **`214.1/s` (`42.8/s`)** (`278.6/s` peak) | **`14.48s` / `36.33s`** | **`40.15s` / `52.54s` (`20.91s`)** | **`3,852.0/s` (`770.4/s`)** (`4,333.7/s` peak) | **`2.077s`** | **`0 / 24,000`** |
| **Speedup** | **`2.33×` faster** | **`2.33×`** | **`2.94×` P50 / `2.27×` P99** | **`2.20×` P50 / `2.06×` P99 (`2.72×` inline)** | **`4.01×` faster** | **`4.01×` faster** | `0.000%` |

### 3.7 Suite `#5C` — `20,000`-Claim Single-Process Reliability Soak (`100` Waves × `200` on `200` Pools)

| Arm | Completed / Errors | Aggregate Claim `P50` / `P99` | 1st Ready (`P50`) | Wave Claim Rate (`med` / `peak`) | Steady Open Sockets |
| --- | ---: | ---: | ---: | ---: | ---: |
| `per_claim` | `20,000 / 0` (`0.000%`) | `1.138s` / `1.474s` | `168.3 ms` | `127.5/s` / `142.9/s` | `187` |
| **`batch64`** | **`20,000 / 0` (`0.000%`)** | **`0.560s` (`560 ms`) / `1.106s`** | **`94.4 ms`** | **`175.4/s` / `230.8/s`** | **`68`** (`0` drift) |
| **Speedup** | `0.000% < 0.01%` bar | **`2.03×` P50 / `1.33×` P99** | **`1.78×` faster** | **`1.38×` med / `1.62×` peak** | **`2.75×` fewer** |

### 3.8 Suite `#1` — Canonical Stock `T0` Warm SWE-bench `500×6 = 3,000` (`ABABAB` 3-Rep Median) & Suite `#2` — Cold `0%`-Cached `T0` (`500×1`)

| Suite & Strategy | `per_claim` Wall (`r1 \| r2 \| r3 \| med`) | **`batch64` Wall (`r1 \| r2 \| r3 \| med`)** | Speedup / Parity | Open Sockets (`per_claim → batch64`) | Published `RESULTS.md` row (different env — context only) |
| --- | --- | --- | --- | ---: | --- |
| **`#1` `naive` (`3,000`)** | `329.1 \| 121.1 \| 115.6 \|` `121.09s` | `102.7 \| 93.7 \| 96.5 \|` **`96.54s`** | **`1.25×` faster** | `484 → 119` (**`4.07×` fewer**) | `#10`: `587.7s` |
| **`#1` `sliding` (`3,000`)** | `107.7 \| 87.0 \| 84.8 \|` `87.03s` | `94.7 \| 88.2 \| 90.9 \|` **`90.86s`** | `0.96×` parity (`1.20×` lower Claim P99) | `551 → 99` (**`5.57×` fewer**) | `#11`: `603.8s` |
| **`#1` `pipelined` (`3,000`)** | `581.0 \| 100.1 \| 98.5 \|` `100.07s` | `140.3 \| 98.1 \| 93.1 \|` **`98.14s`** | **`1.02×` med** | `546 → 90` (**`6.07×` fewer**) | `#12`: `507.4s` |
| **`#1` `naive+recycle` (`3,000`)** | `395.9 \| 404.1 \| 408.7 \|` `404.09s` | `393.5 \| 391.4 \| 402.8 \|` **`393.53s`** | **`1.03×`** (fresh `batch64` `96.5s` is **`4.08×` faster** than in-place `git restore`) | `64 → 22` (**`2.91×` fewer**) | `#13`: `587.4s` |
| **`#2` Truly Cold `0%` `T0` (`500`)** | `54.45s` (`500/500` OK) | `70.47s` (`500/500` OK; `B0` raw Pod = `73.40s`) | Node GCFS cold FUSE pull-bound (`batch64` faster than raw `B0` `73.4s`) | `484 → 96` (**`5.04×` fewer**) | `#14`: `422.1s` |


### 3.9 Suite `#7D` — `5`-Cluster Pure Python SDK Claim + Release (`1k / 2.5k / 5k`, `ABABAB` 3-Rep Median)

Driver ran on the workstation (`oss-work`) against `c1..c5` for **both arms** (an in-cluster attempt saturated the single driver pod's SNAT under `per_claim`'s per-sandbox watch fan-out and dropped claims). `per_claim` = `AsyncSandboxClient` concurrent create/terminate (`64`/cluster); `batch64` = `claim_batch(max_in_flight=64, create_rps=200)` + `DELETE_COLLECTION` release. Idle-only PodFailed warm-sandbox reaper (claims = 0, between bursts, both arms) documented in harness.

| Burst | Arm | Claim Wall (Fleet / `÷5`) | Release API (Fleet / `÷5`) | Pods Gone | Completed | Speedup (claim · release API · pods gone) |
| --- | --- | ---: | ---: | ---: | ---: | --- |
| `1,000` | `per_claim` | `25.62s` (`39.0/s` / `7.8/s`) | `9.856s` (`101/s` / `20/s`) | `10.74s` | `3,000/3,000` | |
| | **`batch64`** | **`10.12s`** (`98.8/s` / `19.8/s`) | **`0.560s`** (`1,787/s` / `357/s`) | **`1.71s`** | **`3,000/3,000`** | **`2.53×` · `17.6×` · `6.3×`** |
| `2,500` | `per_claim` | `59.45s` (`42.0/s` / `8.4/s`) | `25.32s` (`99/s` / `20/s`) | `26.27s` | `7,500/7,500` | |
| | **`batch64`** | **`19.58s`** (`127.7/s` / `25.5/s`) | **`0.946s`** (`2,644/s` / `529/s`) | **`5.10s`** | **`7,500/7,500`** | **`3.04×` · `26.8×` · `5.2×`** |
| `5,000` | `per_claim` | `125.48s` (`39.9/s` / `8.0/s`) | `23.41s` (`214/s` / `43/s`) | `24.39s` | `14,998/15,000` | |
| | `per_claim` `delete_all()` | `113.56s` | `68.77s` (`73/s` / `15/s`) | `69.67s` | `14,998/15,000` | |
| | **`batch64`** | **`22.73s`** (`219.9/s` / `44.0/s`) | **`1.222s`** (`4,092/s` / `818/s`) | **`10.01s`** | **`15,000/15,000`** | **`5.52×` · `19.2×` (`56×` vs `delete_all`) · `2.4×` (`7.0×` vs `delete_all`)** |

Errors: `batch64` `0 / 25,500`; `per_claim` `4 / 40,500` (two `5k` reps with `2` failed claims each). Note: in this harness `batch64` holds more client sockets (`~344`) than `per_claim` (`5–20`), because `per_claim` here reuses one pooled client per cluster.

### 3.10 Suite `#4C` — Full R2E-Gym `4,578` Unique Images, Cold (fresh 0%-cached pool per arm)

| Arm | Wall | Completed | Claim P50 | TTFI P50 |
| --- | ---: | ---: | ---: | ---: |
| `per_claim` | `2,746.5s` | `4,578/4,578` | `0.734s` | `17.17s` |
| `batch64` | `2,771.0s` | `4,578/4,578` | `0.969s` | `16.55s` |

**Parity (`0.99×`).** With every image new to the node, wall time is bound by image pull, not claim orchestration — an honest "no win on unique-image cold paths" data point.

#### 3.9.1 `#7D` bonus — `max_in_flight` sensitivity (`mif=256`, `create_rps=500` vs default `mif=64`, `create_rps=200`)

Same harness, same 5 clusters, run immediately after `#7D` (own `per_claim` arm interleaved ABABAB).

| Burst | `per_claim` claim | `batch` `mif=256` claim | `batch` `mif=64` claim (from `#7D`) | Release API `mif=256` / `mif=64` |
| --- | ---: | ---: | ---: | ---: |
| `1,000` | `30.2s` | `24.8s` | **`10.1s`** | `0.615s` / `0.560s` |
| `2,500` | `66.2s` | `30.6s` | **`19.6s`** | `0.868s` / `0.946s` |
| `5,000` | `120.3s` | `32.0s` | **`22.7s`** | `1.183s` / `1.222s` |

**Raising `max_in_flight` to 256 is slower at every size** (`1.4×–2.5×` slower claim than `mif=64`); release is unchanged (one `DELETE_COLLECTION` either way). `mif=256`: `0 / 25,500` errors.


### 3.11 Suite `#4B` (clean rerun) — Tunix GRPO `64 × 16 × 8` steps `@ c = 1,024` (supersedes original `#4B`)

Fresh `10× n2-standard-32` pool (pd-balanced 1,500 GB), `r2e_subset_500` DaemonSet pre-pull + smoke gate, ABABAB. **Rep 1 discarded for both arms**: image-streaming pre-pull does not hydrate image data, so the pool warmed during rep 1 (steps fell `~260s → ~20s`). Reported = median of warm reps 2–4.

| Arm | 8-step total (`r2 \| r3 \| r4 \| med`) | Step 0 (med) | Steady step `1–7` (med) | Env-ready P50 `1–7` (med) | Errors (3 reps) |
| --- | --- | ---: | ---: | ---: | ---: |
| `per_claim` | `202.2 \| 214.4 \| 210.2 \|` **`210.2s`** | `34.2s` | `25.1s` | `12.2s` | `3 / 24,576` |
| `batch64` | `261.8 \| 227.6 \| 203.1 \|` `227.6s` | `30.3s` | `25.6s` | `15.5s` | `0 / 24,576` |
| **Ratio** | **`0.92×` (batch slightly slower)** | `1.13×` | `0.98×` | `0.78×` | — |

**No SandboxBatch win on this shape** (64 images, 16 rollouts per image, 1,024-wide). The original `#4B` "`1.62×` faster Step 0" was an artifact of `893/1,024` failed `batch64` probes and is withdrawn.

> [!NOTE]
> **Rep-1 cold-cache bias also affected `#1`.** Its rep 1 ran `per_claim` first on a pool whose streamed images were not yet hydrated (`naive` `329s`, `pipelined` `581s` vs ~`100–120s` afterwards). The "`3.20×` / `4.14×` faster on `r1`" callouts were run-order artifacts and are withdrawn; the medians stand.
