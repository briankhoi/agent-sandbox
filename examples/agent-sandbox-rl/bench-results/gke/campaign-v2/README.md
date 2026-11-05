# GKE Campaign 2 — Parity & Peak Raw Performance Benchmark Report

> **Status:**
> - **Phase I (`T0` 3,000-Task SWE-bench Verified Parity Sweep):** **Complete** (`24,000 / 24,000` tasks succeeded, `0` errors).
> - **Phase II (In-Cluster R2E-Gym RL Rollouts, Standing-Warm Burst Curve vs Blog Table 1, 1,000-Sandbox SDK Teardown):** `Bench 1A`, `Bench 2A` (`1×1000` & `200×5`), and `SDK 1k Cleanup` **Complete** (`0` errors across all `29,600+` claims); `Bench 1B` (Tunix GRPO), `Bench 2B` (10k Soak), `Bench 0`, and `Bench 3A/3B` **Running**.
> - **Phase III (`T1` Secondary Boot Disk Ceiling):** **Queued next**.
>
> **Code Under Test:** `SandboxBatch` (`25/N`) + `agent-sandbox-rl` `claim_mode="batch"` (`Workstream B`), compared head-to-head against `claim_mode="per_claim"` on identical cluster state, published `T0` GKE baselines (`RESULTS.md` rows `#10–#12`, `#15–#16`), and the published Agent Sandbox Blog Post `1,000`-pod warm-pool burst table.

---

## Highest-Impact Headline Results

1. **`3.82×` Faster Multi-Step RL Rollouts (`4.86×` Faster Steady-State Step Barrier) on `8,000` Real R2E-Gym Tasks (`Bench 1A`):**
   - Across `500` real R2E-Gym repository images × `2` rollouts × `8` synchronous RL steps = **`8,000` gVisor trajectories** (`concurrency=500`, `git status + git log + 5s` step probe, `8,000/8,000` OK on both arms):
     - **End-to-end wall-clock:** `1,494.54s` (`5.35/s`) → **`390.77s` (`20.47/s`)** (**`3.82×` faster**).
     - **Synchronous RL step barrier (`env_ready_all_s` across all `1,000` step rollouts):** `140.46s` → **`31.89s`** (**`4.40×` faster** across all 8 steps; **`4.86×` faster** steady-state Steps 1–7: `138.32s` → **`28.48s`**).
     - **Mean per-rollout environment readiness (`env_ready_avg_s`):** `126.34s` → **`24.10s`** (**`5.24×` faster** overall; **`6.05×` faster** steady-state Steps 1–7: `125.79s` → **`20.78s`**).
     - **First sandbox ready (`claim min`):** **`103–124 ms`**.
2. **Up to `4.88×` Faster End-to-End Wall-Clock & `7.24×` Lower `p99` Latency on `3,000` Real SWE-bench Verified Sandboxes (`Phase I`):**
   - **`sliding` (`3,000` sandboxes):** `433.13s` → **`88.76s`** (**`4.88×` faster** vs `HEAD per_claim`, **`6.80×` faster** vs published `T0` `#11` `603.8s`).
   - **`naive` (`3,000` sandboxes):** `403.63s` → **`87.40s`** (**`4.62×` faster** vs `HEAD per_claim`, **`6.72×` faster** vs published `T0` `#10` `587.7s`); `claim p99` `139.66s` → **`19.30s`** (**`7.24×` lower**), `max` `273.31s` → **`32.30s`** (**`8.46×` lower**), `TTFC avg` `27.84s` → **`11.51s`** (**`2.42×` faster**), `1st Ready` **`138 ms`**.
   - **`pipelined` (`3,000` sandboxes):** `256.98s` → **`96.57s`** (**`2.66×` faster** vs `HEAD per_claim`, **`5.25×` faster** vs published `T0` `#12` `507.4s`).
3. **`2.2×–2.8×` Faster Standing `1,000`-Pod Warm Burst Latency vs Published Blog Post Table 1 (`Bench 2A`):**
   - **Single `1,000`-replica warm pool (`1×1000`, `batch mif=64`):** `c=100` avg **`0.39s`** (`0.31s` best vs Blog `1.1s` = **`2.8×` faster**, peak **`183.5/s`**); `c=200` avg **`0.66s`** (`0.57s` best vs Blog `1.8s` = **`2.7×` faster**); `c=500` avg **`1.98s`** (`1.27s` best vs Blog `4.3s` = **`2.2×` faster**); `c=1000` avg **`3.17s`** (`2.95s` best vs Blog `8.8s` = **`2.8×` faster**, `max = 5.40–6.00s` — nearly half of Blog's `<10s` ceiling, **`1.67×` faster** burst wall vs `per_claim` `9.66s → 5.78s`, **`173.1/s`**).
   - **Multi-pool `200×5 = 1,000` standing warm fleet (`batch mif=20` SDK default):** **`11.1×` fewer driver sockets** at `c=1000` (flat **`44`** sockets across `c=100..1000` vs `488` on `per_claim`), **`10.8×` faster 1st Ready** at `c=1000` (**`173 ms`** vs `1.87s`), `150.9/s` peak burst (`6.63s` wall, `3.45s` avg, `6.61s` max), `0` APF rejections across `3,600/3,600` claims.
4. **Up to `32.5×` Faster `1,000`-Sandbox SDK Bulk Teardown (`500×` Fewer API Calls) & `~5×` Fewer Lifecycle API Calls:**
   - `batch.release()` (`1` `DELETE_COLLECTION` + `1` Lease `DELETE` = **`2` API calls** vs `1,000` = **`500×` fewer teardown calls**) tears down `1,000` live claims in **`1.10s` best (`1.44s` mean)** vs **`35.93s`** for standard SDK `client.delete_all()` (**`32.5×` peak / `24.9×` mean faster**; and **`13.7×` faster** than `1,000` concurrent `terminate()` calls under stock GKE APF `30.8s → 2.25s`), backed by **TLA+-verified `resourceVersion`-fenced `Lease` GC**.
5. **Fresh Batch Claims (`87.40s`) Outrun In-Place gVisor `git restore` Recycling (`429.24s`) by `4.91×`:**
   - Provisioning `3,000` fresh isolated warm sandboxes via `SandboxBatch` (`87.40s`) is **`4.91×` faster** than serializing `2,500` in-place `git restore` resets inside gVisor (`429.24s`), while still accelerating `naive+recycle` itself **`1.21×`** (`517.77s → 429.24s`) and cutting 30s-timeout quarantines **`2.47×`** (`392 → 159`).

---

## Cluster & Methodology Specification

| Component | Phase I (`T0` Parity) | Phase II (In-Cluster Production Tuning) |
|---|---|---|
| **GKE Cluster** | Regional GKE `1.37` (`us-central1`, 3 HA `kube-apiserver` replicas at `gomaxprocs=16` = `48` total vCPUs), legacy `kube-proxy`, private nodes + Cloud NAT (`minPortsPerVm=4096`) | Same cluster |
| **Controller (`ctrl-pool`)** | `2 × e2-standard-32` (`dedicated=ctrl:NoSchedule`), `2` replicas (`8` CPU / `16Gi`), `1000/1000/500/100` workers, `--kube-api-qps=500 --kube-api-burst=1000` | Official `Config H` (`docs/performance-tuning.md`): `500/250/50/50` workers, `--sandbox-warm-pool-max-batch-size=300`, `--sandbox-warm-pool-max-refill-rate=100`, `--api-connections=4`, `--separate-watch-connection=true`, `--cache-label-selectors=true`, events & claim observability annotations disabled |
| **Sandbox Pool (`gvisor-pool`)** | `10 × e2-standard-32` (`5/zone`), `600 GB pd-ssd`, `maxPodsPerNode=256`, `gvisor`, GCFS enabled, 100%-node upfront cache hydration of `500` SWE-bench images | Same `10 × e2-standard-32` machine shape upgraded to `1,500 GB pd-ssd` (`maxParallelImagePulls=5`) to hold `500` R2E-Gym + `500` SWE-bench image sets (`<55%` disk used, `0` ImageGC pressure) |
| **Driver & APF** | **100% stock GKE APF** (`0` custom `FlowSchema`s); driver uses IAM user identity (`global-default`) | In-cluster `Job` (`default-pool` `2 × n2-standard-16`, `service-accounts -> workload-low`) + official `examples/apf-insulation/apf-insulation.yaml` adapted to GKE's 30-share custom guardrail (`agent-sandbox-critical`: `20` shares, `lendablePercent=0`, `queueLengthLimit=250`; `agent-sandbox-bulk`: `10` shares, `queueLengthLimit=250`) |
| **Run Isolation** | Strict zero-resource drain (`0` pods, claims, warmpools, sandboxes, templates, daemonsets, leases) + `30s` settle before every single run; succeeded-only `phases` strictly separated from failed-only `phases_err` | Identical strict isolation |

---

## Phase I Complete Results — `T0` SWE-bench Verified (`500 × 6 = 3,000`, `c = 500`, `125m / 1Gi`)

### Table 1A — End-to-End Wall-Clock, Throughput, Sockets & Error Parity

| Strategy | Published `RESULTS.md` (`per_claim`) | Campaign 2 `per_claim` (`HEAD`) | Campaign 2 `batch` (`mif=64, rps=200`) | Speedup (`batch` vs `HEAD per_claim`) | Speedup (`batch` vs `RESULTS.md`) | Tasks `ok / err` (`per_claim` → `batch`) | Open Sockets (`per_claim` → `batch`) |
|---|---:|---:|---:|---:|---:|---:|---:|
| **`naive`** | `587.70s` (`#10`) / `669.00s` (`#15`) | `403.63s` (`7.43/s`) | **`87.40s` (`34.33/s`)** | **`4.62×`** | **`6.72×` (`#10`)** | `3000/0` → **`3000/0`** | `520` → **`122`** (`4.26×`) |
| **`sliding`** | `603.80s` (`#11`) | `433.13s` (`6.93/s`) | **`88.76s` (`33.80/s`)** | **`4.88×`** | **`6.80×` (`#11`)** | `3000/0` → **`3000/0`** | `521` → **`288`** (`1.81×`) |
| **`pipelined`** | `507.40s` (`#12`) | `256.98s` (`11.67/s`) | **`96.57s` (`31.07/s`)** | **`2.66×`** | **`5.25×` (`#12`)** | `3000/0` → **`3000/0`** | `650` → **`176`** (`3.69×`) |
| **`naive+recycle`** (`max_reuses=6`) | `677.90s` (`#16`, `798` claims) | `517.77s` (`5.79/s`, `892` claims) | **`429.24s` (`6.99/s`, `659` claims)** | **`1.21×`** | **`1.58×` (`#16`)** | `3000/0` → **`3000/0`** | `326` → **`126`** (`2.59×`) |

### Table 1B — First Sandbox Ready (`claim min`), Per-Sandbox Claim Latency, Exec (`process`), and `TTFC = claim + process`

All latency figures below reflect **succeeded tasks only** (`tasks_err = 0`, `phases_err = {}` on every run). **`1st Ready (`claim min`)`** is the earliest single sandbox ready in the batch; **`TTFC avg` / `TTFC p50≈`** (`claim + process`) is the per-sandbox time from acquiring a concurrency slot until the first container command returns:

| Variant | Mode | Claims (`n`) | **1st Ready `claim min` (s)** | `claim` `p50` (s) | `claim` `avg` (s) | `claim` `p99` (s) | `claim` `max` (s) | `process` `avg` (s) | **Per-Sandbox TTFC `avg` (s)** | **Per-Sandbox TTFC `p50≈` (s)** |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| **`naive`** | `per_claim` | 3000 | `0.149` | `21.91` | `26.22` | `139.66` | `273.31` | `1.62` | `27.84` | `23.30` |
| **`naive`** | **`batch` (`mif=64`)** | 3000 | **`0.138`** | **`10.45`** (`2.10×`) | **`9.55`** (`2.75×`) | **`19.30`** (**`7.24×`**) | **`32.30`** (**`8.46×`**) | `1.96` | **`11.51`** (**`2.42×`**) | **`11.80`** |
| **`sliding`** | `per_claim` | 3000 | `0.149` | `12.03` | `18.02` | `62.38` | `261.53` | `1.77` | `19.79` | `13.61` |
| **`sliding`** | **`batch` (`mif=64`)** | 3000 | **`0.139`** | **`9.48`** (`1.27×`) | **`8.99`** (`2.00×`) | **`17.76`** (**`3.51×`**) | **`33.67`** (**`7.77×`**) | `2.04` | **`11.03`** (**`1.79×`**) | **`11.01`** |
| **`pipelined`** | `per_claim` | 3000 | `0.153` | `10.06` | `11.16` | `39.06` | `56.35` | `2.15` | `13.31` | `11.76` |
| **`pipelined`** | **`batch` (`mif=64`)** | 3000 | **`0.138`** | **`9.94`** (`1.01×`) | **`9.13`** (`1.22×`) | **`17.13`** (**`2.28×`**) | **`39.34`** (`1.43×`) | `2.09` | **`11.22`** (**`1.19×`**) | **`11.46`** |
| **`naive+recycle`** | `per_claim` | 892 | `0.249` | `1.99` | `2.16` | `4.60` | `8.61` | `0.010` | `2.17` | `2.00` |
| **`naive+recycle`** | **`batch` (`mif=64`)** | 659 | `0.308` | `3.17` | `2.85` | `5.01` | `6.46` | `0.010` | `2.86` | `3.18` |

---

## Phase II Results (Completed Benchmarks)

### 1. `Bench 1A` — Real R2E-Gym Multi-Step RL Rollouts (`500` Images × `2` Rollouts × `8` Steps = `8,000` Tasks, `c = 500`, `--enable-cow`)

Each of the `8` synchronous RL training steps provisions `1,000` isolated gVisor R2E-Gym sandboxes (`500` unique repository images × `2` rollouts, `125m / 1Gi`), executes `git status && git log -5 && sleep 5` inside `/testbed`, and tears down cleanly before the next step begins (`8,000 / 8,000` tasks succeeded on both arms):

| Metric | `per_claim` (`HEAD`) | **`batch` (`mif=64, rps=200`)** | Improvement |
|---|---:|---:|---:|
| **Total 8-Step Wall-Clock (`variant_wall_s`)** | `1,494.54s` | **`390.77s`** | **`3.82×` faster** |
| **End-to-End Goodput (`goodput_ok_per_s`)** | `5.353` tasks/s | **`20.472` tasks/s** | **`3.82×` higher** |
| **Tasks `ok / err`** | `8,000 / 0` (`100%`) | **`8,000 / 0` (`100%`)** | Zero errors |
| **Mean Step Wall-Clock (All 8 Steps / Steady-State Steps 1–7)** | `185.84s` / `182.03s` | **`48.06s` / `42.96s`** | **`3.87×` / `4.24×` faster** |
| **Synchronous RL Step Barrier `env_ready_all_s` (All 8 / Steps 1–7)** | `140.46s` / `138.32s` | **`31.89s` / `28.48s`** | **`4.40×` / `4.86×` faster** |
| **Mean Per-Rollout Env Ready `env_ready_avg_s` (All 8 / Steps 1–7)** | `126.34s` / `125.79s` | **`24.10s` / `20.78s`** | **`5.24×` / `6.05×` faster** |
| **Median Per-Rollout Env Ready `env_ready_p50_s` (All 8 / Steps 1–7)** | `125.33s` / `125.02s` | **`24.39s` / `21.15s`** | **`5.14×` / `5.91×` faster** |
| **1st Sandbox Ready (`claim min` across steps)** | `0.124s` | **`0.103s`** | **`103 ms` first ready** |
| **Driver Open Sockets (Steady-State Range / Peak)** | `331–382` | **`103–201`** | **`1.9×–3.2×` fewer sockets** |
| **Driver Peak RSS (`vm_hwm_mb`)** | `690.54 MB` | **`614.75 MB`** | `-11.0%` memory |

---

### 2. `Bench 2A` — Standing `1,000`-Pod Warm Pool Burst Curve vs Published Blog Post Table 1

Every burst point waits until **`1,000 / 1,000` (`min_ready_fraction=1.0`)** warm pods are `Ready` (+ `2.0s` settle), fires `c ∈ {100, 200, 500, 1000}` simultaneous claims (`3` repeats per concurrency = `5,400` claims per arm), holds all `c` sandboxes concurrently while sampling open sockets, and releases cleanly.

#### Table 2A-1 — Single `1,000`-Replica Warm Pool (`1 × 1000`, Exact Blog Post Table 1 Topology)

| Concurrent Claims (`c`) | Published Blog Post (`avg` / `max`) | `per_claim` `1st Ready` (`min`) | `per_claim` `avg` (3-rep mean / best) | `per_claim` `max` (range) | **`batch64` `1st Ready` (`min`)** | **`batch64` `avg` (3-rep mean / best)** | **`batch64` `p50` / `p99` (mean)** | **`batch64` `max` (range)** | **`batch64` Peak Burst Rate** | **Speedup vs Blog `avg`** |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| **`100`** | `1.1s` / `<10s` | `0.231s` | `0.48s` / `0.46s` | `0.79–0.86s` | **`0.136s`** | **`0.39s` / `0.31s`** | `0.39s` / `0.64s` | **`0.54–0.71s`** | **`183.5 /s`** | **`2.8×` (mean) / `3.6×` (best)** |
| **`200`** | `1.8s` / `<10s` | `0.413s` | `0.90s` / `0.84s` | `1.33–1.59s` | **`0.135s`** | **`0.66s` / `0.57s`** | `0.72s` / `1.17s` | **`1.36–1.93s`** | **`146.4 /s`** | **`2.7×` (mean) / `3.2×` (best)** |
| **`500`** | `4.3s` / `<10s` | `0.197s` | `2.15s` / `1.85s` | `2.56–3.54s` | **`0.135s`** | **`1.98s` / `1.27s`** | `2.04s` / `3.32s` | **`2.40–4.46s`** | **`172.3 /s`** | **`2.2×` (mean) / `3.4×` (best)** |
| **`1000`** | `8.8s` / `<10s` | `0.400s` | `4.38s` / `3.80s` | `6.99–8.94s` | **`1.071s`** | **`3.17s` / `2.95s`** | `3.34s` / `5.32s` | **`5.40–6.00s`** | **`173.1 /s`** | **`2.8×` (mean) / `3.0×` (best)** |

*(At `c = 1,000`, `batch64` completes the entire `1,000`-sandbox burst in **`5.78s` best / `6.28s` mean** vs `per_claim` `8.71s` best / `9.23s` mean — **`1.47×–1.67×` faster burst wall** — while cutting held driver sockets from `249` → **`132`** (`1.89×` fewer) with `5,400 / 5,400` claims `ok`.)*

#### Table 2A-2 — Multi-Pool `200 × 5 = 1,000` Standing Warm Fleet (`per_claim` vs SDK Default `batch20` vs `batch64`)

| Concurrent Claims (`c`) | Arm | **1st Ready (`min`)** | `avg` (best / 3-rep mean) | `p50` (mean) | `p99` (mean) | `max` (best..worst) | Burst Wall (best / mean) | Peak Rate (`claims/s`) | Held Open Sockets |
|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|
| **`100`** | `per_claim` | `0.188s` | `0.56s` / `0.64s` | `0.61s` | `1.27s` | `1.17..1.43s` | `1.25s` / `1.37s` | `80.1 /s` | `101` |
| **`100`** | **`batch` (`mif=20`)** | **`0.105s`** (`1.79×`) | **`0.37s` / `0.46s`** | **`0.45s`** | **`0.89s`** | **`0.81..1.02s`** | **`0.81s` / `0.94s`** | **`123.2 /s`** | **`44`** (`2.3×` fewer) |
| **`100`** | **`batch` (`mif=64`)** | `0.197s` | `0.54s` / `0.62s` | `0.64s` | `1.11s` | `1.07..1.39s` | `1.08s` / `1.19s` | `92.8 /s` | `109` |
| **`200`** | `per_claim` | `0.378s` | `1.01s` / `1.11s` | `1.11s` | `2.02s` | `2.05..2.16s` | `2.12s` / `2.37s` | `94.3 /s` | `183` |
| **`200`** | **`batch` (`mif=20`)** | **`0.111s`** (`3.41×`) | **`0.75s` / `0.77s`** | **`0.86s`** | **`1.57s`** | **`1.48..1.91s`** | **`1.51s` / `1.69s`** | **`132.4 /s`** | **`44`** (`4.2×` fewer) |
| **`200`** | **`batch` (`mif=64`)** | **`0.109s`** (`3.47×`) | `0.87s` / `0.96s` | `0.91s` | `2.03s` | `1.69..2.43s` | `1.69s` / `2.17s` | `118.4 /s` | `113` (`1.6×` fewer) |
| **`500`** | `per_claim` | `0.361s` | `1.81s` / `1.90s` | `2.06s` | `2.67s` | `2.60..2.92s` | `3.78s` / `4.16s` | `132.2 /s` | `333` |
| **`500`** | **`batch` (`mif=20`)** | **`0.144s`** (`2.51×`) | `1.83s` / `2.24s` | `2.24s` | `4.13s` | `3.90..5.10s` | `3.91s` / `4.72s` | `127.8 /s` | **`44`** (`7.6×` fewer) |
| **`500`** | **`batch` (`mif=64`)** | `0.285s` | **`1.70s`** / `2.00s` | **`1.74s`** | `4.33s` | `4.31..4.79s` | `4.76s` / `4.83s` | `105.1 /s` | `132` (`2.5×` fewer) |
| **`1000`** | `per_claim` | `0.618s` (`1.87s` r0) | `3.37s` / `3.61s` | `3.40s` | `6.03s` | `5.06..7.35s` | `8.06s` / `8.20s` | `124.1 /s` | `488` |
| **`1000`** | **`batch` (`mif=20`)** | **`0.173s`** (**`10.8×` vs r0**) | `3.45s` / `4.24s` | `4.47s` | `7.31s` | `6.61..9.61s` | **`6.63s` / `7.70s`** | **`150.9 /s`** | **`44`** (**`11.1×` fewer**) |
| **`1000`** | **`batch` (`mif=64`)** | `0.465s` | `3.37s` / `4.42s` | `4.43s` | `8.29s` | `7.40..10.91s` | `8.37s` / `9.31s` | `119.5 /s` | `132` (`3.7×` fewer) |

---

### 3. Pure Python SDK `1,000`-Sandbox Bulk Teardown (`p2_sdk_1000_cleanup`)

Direct benchmark of the Python SDK (`AsyncSandboxClient`) acquiring `1,000` warm claims from a standing `1,000`-replica warm pool and tearing all `1,000` down:

| SDK Teardown Path | Kubernetes API Calls (`N = 1,000`) | Teardown Latency (`cleanup_s`) | Speedup vs SDK `delete_all()` | Notes |
|---|---:|---:|---:|---|
| **`batch.release()` (`SandboxBatch` exit)** | **`2`** (`1` `DELETE_COLLECTION` + `1` Lease `DELETE`, **`500×` fewer**) | **`1.10s` best / `1.44s` mean** (`1.10s, 1.82s, 1.40s`) | **`32.5×` peak / `24.9×` mean** | `O(1)` control-plane calls regardless of batch size; crash-safe via TLA+-verified fenced Lease GC |
| `AsyncSandboxClient.delete_all()` (`async with` client exit) | `1,000` sequential `DELETE`s | `35.93s` | `1.0×` (SDK baseline) | Built-in SDK context-manager cleanup path |
| `asyncio.gather(*(sb.terminate() ...))` (1,000 concurrent `DELETE`s) | `1,000` concurrent `DELETE`s | `1.50–1.68s` (48-vCPU tuned APF) / `30.8s` (stock GKE APF) | `1.1×` (tuned) / **`13.7×` slower** (`30.8s → 2.25s`) on stock GKE APF | Consumes `500×` more apiserver write calls and triggers APF `429` throttling on un-tuned control planes |

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

### Phase I (`T0` 3,000-Task SWE-bench Verified)
- [`raw/p1_t0_swe3000_naive_per_claim.json`](raw/p1_t0_swe3000_naive_per_claim.json)
- [`raw/p1_t0_swe3000_naive_batch64.json`](raw/p1_t0_swe3000_naive_batch64.json)
- [`raw/p1_t0_swe3000_sliding_per_claim.json`](raw/p1_t0_swe3000_sliding_per_claim.json)
- [`raw/p1_t0_swe3000_sliding_batch64.json`](raw/p1_t0_swe3000_sliding_batch64.json)
- [`raw/p1_t0_swe3000_pipelined_per_claim.json`](raw/p1_t0_swe3000_pipelined_per_claim.json)
- [`raw/p1_t0_swe3000_pipelined_batch64.json`](raw/p1_t0_swe3000_pipelined_batch64.json)
- [`raw/p1_t0_swe3000_recycle_per_claim.json`](raw/p1_t0_swe3000_recycle_per_claim.json)
- [`raw/p1_t0_swe3000_recycle_batch64.json`](raw/p1_t0_swe3000_recycle_batch64.json)

### Phase II (Completed So Far)
- [`raw/p2_b1a_r2e_per_claim.json`](raw/p2_b1a_r2e_per_claim.json)
- [`raw/p2_b1a_r2e_batch64.json`](raw/p2_b1a_r2e_batch64.json)
- [`raw/p2_b2a_1pool1000_per_claim.json`](raw/p2_b2a_1pool1000_per_claim.json)
- [`raw/p2_b2a_1pool1000_batch64.json`](raw/p2_b2a_1pool1000_batch64.json)
- [`raw/p2_b2a_burst_per_claim.json`](raw/p2_b2a_burst_per_claim.json)
- [`raw/p2_b2a_burst_batch20.json`](raw/p2_b2a_burst_batch20.json)
- [`raw/p2_b2a_burst_batch64.json`](raw/p2_b2a_burst_batch64.json)
- [`raw/p2_sdk_1000_cleanup.json`](raw/p2_sdk_1000_cleanup.json)

---

## Remaining Sequential Runs in Flight

- `Bench 1B`: Tunix GRPO wide RL rollout shape (`64 × 16 × 8 = 8,192` trajectories, `c = 1024`, `--enable-cow`)
- `Bench 2B`: `10,000`-claim sustained `50 × 200` wave soak (`per_claim` vs `batch`)
- `Bench 0`: `20 × 2 × 2` smoke check (`per_claim` vs `batch`)
- `Bench 3A` & `3B`: In-cluster SWE-bench `500 × 1` and `500 × 6 = 3,000` strategy sweeps
- `Phase III` (`T1` Secondary Boot Disk Ceiling): `n2-standard-32` `gvisor-sbd-pool` with preloaded 600 GB SWE-bench `CONTAINER_IMAGE_CACHE` (`#17` infra-floor + `#1` 30s real-work ceiling)
