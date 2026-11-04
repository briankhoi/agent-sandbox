# Independent Adversarial Benchmark Audit Report

**Campaign Audited:** 10.5-hour GKE Benchmark Campaign (`2026-11-04T11:13:06Z` → `2026-11-04T21:35:04Z`)
**Comparison:** `claim_mode="per_claim"` (`SandboxClaim` fan-out) vs `claim_mode="batch"` (`SandboxBatch` + RL integration `19/N`–`26/N`, commit `4018f0ddfa381baf91b41a28c555ac9d0d25903e`)
**Cluster (`T0`-spec):** `asrl-bench-t0` (`us-central1-c`, GKE `1.36.5-gke.1982000`, `10 × e2-standard-32` gVisor sandbox pool `maxPods=256` with Container-Optimized OS + Image Streaming `gcfsd` ON, `2 × n2-standard-16` system/driver pool)
**Audit Date:** `2026-11-04`
**Raw Evidence Audited:** All 22 run bundles in `scratch/gke_bench/runs/` (`20 × .json`, `22 × .meta.json`, `22 × .apf_before.txt`, `22 × .apf_after.txt`, `22 × .ctrl_before.json`, `22 × .ctrl_after.json`, and diagnostic `.pod.log` files), driver/SDK source under `scratch/gke_bench/driver_build/`, `GKE_BENCHMARK_TRACKER.md`, `PERF_NOTES.md`, 100-Node Baseline TL doc tabs (`02`, `07`, `15`, `18`), and `scratch/perf_repo/agent-sandbox__RESULTS.md`.

---

## 1. Executive Audit Verdict & Tracker Discrepancies Caught

### Overall Integrity Verdict
1. **Raw Data Integrity: PASS.** Every internal benchmark measurement reported in `GKE_BENCHMARK_TRACKER.md` (`wall_s`, `tasks_ok`, `tasks_err`, `env_ready_*`, `p50/p95/p99`, `sockets`, `threads`, `RSS`, `verify_teardown`) traces directly to authentic GKE job executions and matches the raw `.json` and `.apf_*.txt` artifacts (subject to the specific tracker discrepancies listed below). Zero fabricated orcherry-picked sub-runs were hidden; aborted/diagnostic runs (`b1b_tunix_64x16x8_per_claim_oc25`, `b3a_swe500x1_per_claim_pipelined429crash`, `b3b_swe3000_batch_mif64` `pipelined`) were preserved in `runs/` and `STATE.json`.
2. **Core Technical Thesis: VERIFIED.** Across all 6 benchmarks (`1A`, `1B`, `2A`, `2B`, `3A`, `3B`), `claim_mode="batch"` (`mif=64`) achieved **`0` task errors across every completed run (`36,292 / 36,292` completed tasks across `1A-3`, `1B-3`, `2A-3`, `2B-2`, `3A-1` all 3 strategies, `3B-2` `naive`+`sliding`, and `3B-3` `pipelined`)**, eliminated `SandboxClaim` APF `429` crashes, bounded client HTTP sockets to `O(mif)` (`25`–`88` steady sockets vs `654`–`1,026` in `per_claim`), eliminated `CLOSE_WAIT` socket storms (`≤ 2` vs `121`–`877`), and set new `T0` wall-clock records on SWE-bench `500×1` (`161.7s` naive) and `500×6 = 3,000` (`523.9s` naive, `503.4s` sliding, `502.2s` pipelined).
3. **CRITICAL EXTERNAL BASELINE CORRECTION REQUIRED BEFORE PUBLICATION:** `GKE_BENCHMARK_TRACKER.md` misquotes `RESULTS.md` Rows `#10`–`#12` (claiming Row `#12` `pipelined` was `624.5s` / `1.244×` slower than `502.2s`). In the actual `agent-sandbox__RESULTS.md`, Row `#10` (`naive`) is **`587.7s`**, Row `#11` (`sliding`) is **`603.8s`**, Row `#12` (`pipelined`) is **`507.4s`**, and Row `#15` (`naive`) is **`669.0s`**. Our `batch` (`mif=64`) runs **still beat every single `RESULTS.md` row** (`523.9s` vs `587.7s` = **`1.122×`**, `503.4s` vs `603.8s` = **`1.199×`**, `502.2s` vs `507.4s` = **`1.010×` / `-5.2s` new `T0` record**), and critically did so with **10× fewer warm-pool workers** (`--sandbox-warm-pool-concurrent-workers=50` vs `RESULTS.md` `#10–#12`'s `500`). Fixing this citation is mandatory before publishing.

### Complete List of Discrepancies Found in `GKE_BENCHMARK_TRACKER.md`

| ID | Severity | Location in `GKE_BENCHMARK_TRACKER.md` | What Tracker Says | Verified Ground Truth (Raw JSON / APF / `RESULTS.md`) | Required Fix in Final Report |
|---|---|---|---|---|---|
| **D1** | **CRITICAL (External Citation Error)** | Lines 79, 111, 112 (`Bench 3B`) | Claims `RESULTS.md` `#12` (`pipelined`) is `624.5s` (`1.244×` slower than `502.2s`), `#11` (`sliding`) is `1.25×` slower than `503.4s` (~`629s`), and `#10` (`naive`) is `1.43×` slower than `523.9s` (~`749s`). | Actual `agent-sandbox__RESULTS.md`: **Row `#10` (`naive`) = `587.7s`** (`0.1959 s/task`); **Row `#15` (`naive`) = `669.0s`** (`0.2230 s/task`); **Row `#11` (`sliding`) = `603.8s`** (`0.2013 s/task`); **Row `#12` (`pipelined`) = `507.4s`** (`0.1691 s/task`). (`624.5` does not exist in `RESULTS.md` or any TL doc tab.) | Replace with exact `RESULTS.md` comparisons: `naive` `523.9s` beats `#10` (`587.7s`) by **`1.122×`** and `#15` (`669.0s`) by **`1.277×`**; `sliding` `503.4s` beats `#11` (`603.8s`) by **`1.199×`** (and beats `#12`'s `507.4s`); `pipelined` `502.2s` beats `#12` (`507.4s`) by **`5.2s` (`1.010×`)**, while using `wp-workers=50` vs `#10–#12`'s `wp-workers=500`. |
| **D2** | **MODERATE (Omitted Non-Zero APF Delta)** | Lines 74–79, 111 (`Bench 3A` & `Bench 3B`) | `APF Δ Rejects` column shows `—` for `3A-1` (`b3a_swe500x1_batch_mif64`) and `3B-2` (`b3b_swe3000_batch_mif64`), while showing `+34`, `+337`, and `0` for sibling rows; Line 111 says "`0` APF rejections across all `batch` runs". | `b3a_swe500x1_batch_mif64` had **`+134` `service-accounts → workload-low` (`queue-full`)** APF rejections (`5306 → 5440`, `0` controller rejects, `0` task errors; caused by `500`-wide `SandboxTemplate`+`SandboxWarmPool` setup/teardown fan-out outside `SandboxBatch`). `b3b_swe3000_batch_mif64` had **`+58` `workload-low` (`queue-full`)** APF rejections (`5811 → 5869`). (`b3b_swe3000_pipelined_oc60_batch_mif64`, `1B-3`, `2A-2`, `2A-3`, `2B-2`, `B3-0` all had **exact `+0`**.) | Report exact `+134` (`3A-1`) and `+58` (`3B-2`) APF deltas in tables; restrict "`0` APF rejections" claim to `1B-3`, `2A-2`, `2A-3`, `2B-2`, `B3-0`, and `3B-3` (`pipelined oc=6.0`). |
| **D3** | **MODERATE (Aborted Rate Definition & Masked Exception)** | Line 111 (`Bench 3B-2 pipelined`) | States `pipelined hit 1,758 > 1,750 Terminating pod guard at 13.94 tasks/s`. | In `b3b_swe3000_batch_mif64.json`, `13.94` is `throughput_tasks_per_s = total_tasks (3000) / wall_s (215.2)` — dividing total planned tasks (including `1,406` unrun tasks) by abort time! Actual completed `goodput_tasks_per_s` at abort was **`1,594 / 215.2 = 7.41 tasks/s`** (`2,400` claims entered = `11.15 claims/s`). Also, `fleet.py:348–378` called `self.teardown()` inside `asrl-breaker`, deleting `WarmPool`s mid-prefetch so `_warm_entry` raised `FleetError` (`did not become ready within 900s`) before `overcommit_guard` could raise `FleetOvercommitError`. | Cite **`7.41 tasks/s` completed goodput** (`1,594` tasks OK / `2,400` claims in `215.2s`), never `13.94 tasks/s`; document the `fleet.py` breaker → `teardown()` → `FleetError` interaction. |
| **D4** | **MINOR (Controller Config Line 53 vs Line 38 & Restart Between Runs)** | Lines 38, 43, 53 | Line 53 (`B0-1`) says `controller 1000/1000/500/300` while Line 38 says `500/500/50/300`. Tracker says `0` controller restarts during runs. | `b0_smoke20_batch` (`B0-1`) did run with `1000/1000/500/300`, then controller was updated to `500/500/50/300` (`qps=500, burst=1000, api-connections=4, separate-watch-connection=true`) for `B0-2` and **all 20 runs of `1A`–`3B`**. Zero restarts occurred *during* any run (`ctrl_before == ctrl_after` on all 22 runs), **1 restart (`0 → 1`, `exitCode=1`) occurred between runs** at `2026-11-04T15:11:47Z` right after GKE's `15:07:31Z` apiserver upgrade (between `1B-2` end `15:09:12Z` and `1B-3` start `15:20:47Z`). | Note `B0-1` → `B0-2` flag change explicitly and disclose the single inter-run controller restart at `15:11:47Z` following the control-plane upsize (`0` in-run restarts across all 22 runs). |
| **D5** | **MINOR (Metric Pairing / Shorthand Nuances in `1A`, `1B-4`, `2A`)** | Lines 59, 61, 66, 68–70, 95, 100, 104 | (a) `1A-1` Line 59 reports `steady env_ready_all=340.98s` (Steps 1–7) while `1A-3` Line 61 divides `342.32 / 153.10 = 2.24×` (Steps 0–7; Steps 1–7 ratio is `340.98 / 158.34 = 2.15×`).<br>(b) `1B-4` attributes all `1,590` errors to HTTP 429.<br>(c) `2A` `Wall (s)` (`453/419/403s`) is `job_elapsed_s` (includes inter-burst `warm_wait`); `2A-1` `c=100` pairs `rep=0` `p50=0.92s` with `rep=1` `rate=58.7/s`; `877 CLOSE_WAIT` is `c=1000 rep=1 proc_before` (`275 CLOSE_WAIT + 603 ESTABLISHED = 880` sockets at `proc_after`); `2A-3` final snapshot has `70` sockets (`69` on the prior 15/16 snapshots); `c=1000` release speedup is `13.69×` on `rep=0` (`2.247s` vs `30.770s`) and `4.40×` on `rep=1` (`7.166s` vs `31.553s`). | Verified from raw JSONs (`b1a_*.json`, `b1b_tunix_64x16x8_per_claim_12vcpu.json` Step 7 `claims=755, ok=754` → `1,589` acquire 429s + `1` exec error, `b2a_*.json`). | Report both `rep=0` and `rep=1` values cleanly in `2A`, specify `All-8-step` vs `Steps 1–7` consistently in `1A`, and note `1,589` HTTP 429s + `1` exec error in `1B-4`. |

---

## 2. Campaign Timeline, Control-Plane Upsize & Complete APF Audit (All 22 Runs)

### Control-Plane State & Controller Stability Audit
- **Controller configuration (`B0-2` through `3B-3`, all 20 measured/prime runs):**
  `--leader-elect --extensions --sandbox-concurrent-workers=500 --sandbox-claim-concurrent-workers=500 --sandbox-warm-pool-concurrent-workers=50 --sandbox-warm-pool-max-batch-size=300 --sandbox-warm-pool-readiness-grace-period=30m --kube-api-qps=500 --kube-api-burst=1000 --api-connections=4 --separate-watch-connection=true` (`1 × replica` on `n2-standard-16`, `4 CPU / 4Gi` req, `8 CPU / 8Gi` lim).
- **Control-plane auto-upsize (`2 vCPU → 12 vCPU`):** GKE Regional HA `kube-apiserver` started the campaign on `2-vCPU` (`--max-requests-inflight=400`, `--max-mutating-requests-inflight=200`, `workload-low` concurrency limit `~13`). At **`2026-11-04T15:07:31Z`** (near the end of `b1b_tunix_64x16x8_batch_mif20`, window `14:49:29Z → 15:09:12Z`), GKE auto-upsized the control plane to `12-vCPU` (`--max-requests-inflight=1600`, `--max-mutating-requests-inflight=800`), resetting cumulative `apiserver_flowcontrol_rejected_requests_total` counters to `0`.
- **Controller restarts:** `restartCount` was `0 → 0` across every run up to `15:09:12Z`, incremented **once between runs** at **`2026-11-04T15:11:47Z`** (`exitCode=1`, leader-election connection reset during GKE apiserver rolling replacement), and remained **`1 → 1` (`0` in-run restarts)** across every run from `15:20:47Z` to `21:35:04Z`.

### Chronological Master APF & Run Audit Table (All 22 Runs)

| # | Run Name | Start → End (UTC) | CP | `job_s` | `rc` | `ctrl_restarts` | APF `workload-low` (`queue-full`) | APF `agent-sandbox-controller` (`time-out`) | Other APF | Total APF Δ |
|---:|---|---|---|---:|---:|---|---:|---:|---:|---:|
| 1 | `b0_smoke20_batch` | `11:13:06 → 11:14:32` | `2-vCPU` | `86` | `0` | `0 → 0` | `0` | `0` | `+1` (`workload-low\|time-out`) | **`+1`** |
| 2 | `b0_cache_prime_r2e_500` | `11:46:49 → 11:56:15` | `2-vCPU` | `566` | `0` | `0 → 0` | `+793` (`0→793`) | `+186` (`0→186`) | `0` | **`+979`** |
| 3 | `b1a_r2e_500x2x8_per_claim` | `12:03:20 → 12:52:20` | `2-vCPU` | `2940` | `0` | `0 → 0` | `+2,515` (`793→3308`) | `+412` (`186→598`) | `+1` (`leader-election\|time-out`) | **`+2,928`** |
| 4 | `b1a_r2e_500x2x8_batch_mif20` | `12:55:27 → 13:34:05` | `2-vCPU` | `2318` | `0` | `0 → 0` | `+485` (`0→485`*) | `0` | `0` | **`+485`** |
| 5 | `b1a_r2e_500x2x8_batch_mif64` | `13:39:11 → 14:02:49` | `2-vCPU` | `1418` | `0` | `0 → 0` | `+176` (`485→661`) | `0` | `0` | **`+176`** |
| 6 | `b1b_tunix_64x16x8_per_claim_oc25` *(breaker abort)* | `14:13:03 → 14:19:13` | `2-vCPU` | `370` | `1` | `0 → 0` | `+3,998` (`661→4659`) | `0` | `0` | **`+3,998`** |
| 7 | `b1b_tunix_64x16x8_per_claim` | `14:31:13 → 14:41:41` | `2-vCPU` | `628` | `0` | `0 → 0` | `+7,747` (`4659→12406`) | `0` | `0` | **`+7,747`** |
| 8 | `b1b_tunix_64x16x8_batch_mif20` | `14:49:29 → 15:09:12` | `2→12 vCPU` | `1183` | `0` | `0 → 0` | **RESET** (`12406→0`) | `0` (`0→0`) | `0` | **RESET (`15:07:31Z`)** |
| 9 | `b1b_tunix_64x16x8_batch_mif64` | `15:20:47 → 15:38:09` | `12-vCPU` | `1042` | `0` | `1 → 1` | **`0`** | **`0`** | `0` | **`0`** |
| 10 | `b1b_tunix_64x16x8_per_claim_12vcpu` | `15:53:46 → 16:09:20` | `12-vCPU` | `934` | `0` | `1 → 1` | `+4,173` (`0→4173`) | `0` | `0` | **`+4,173`** |
| 11 | `b2a_burst_per_claim` | `16:17:19 → 16:24:52` | `12-vCPU` | `453` | `0` | `1 → 1` | `+1,013` (`4173→5186`) | `0` | `0` | **`+1,013`** |
| 12 | `b2a_burst_batch_mif20` | `16:26:59 → 16:33:58` | `12-vCPU` | `419` | `0` | `1 → 1` | **`0`** (`5186→5186`) | **`0`** | `0` | **`0`** |
| 13 | `b2a_burst_batch_mif64` | `16:36:05 → 16:42:48` | `12-vCPU` | `403` | `0` | `1 → 1` | **`0`** (`5186→5186`) | **`0`** | `0` | **`0`** |
| 14 | `b2b_soak10k_per_claim` | `16:56:58 → 17:20:36` | `12-vCPU` | `1418` | `0` | `1 → 1` | **`0`** (`5186→5186`) | **`0`** | `0` | **`0`** |
| 15 | `b2b_soak10k_batch_mif64` | `17:25:17 → 17:49:51` | `12-vCPU` | `1474` | `0` | `1 → 1` | **`0`** (`5186→5186`) | **`0`** | `0` | **`0`** |
| 16 | `b3_cache_prime_swe500` | `18:01:28 → 18:17:04` | `12-vCPU` | `936` | `0` | `1 → 1` | **`0`** (`5186→5186`) | **`0`** | `0` | **`0`** |
| 17 | `b3a_swe500x1_per_claim_pipelined429crash` *(crash)* | `18:57:20 → 19:34:10` | `12-vCPU` | `2210` | `1` | `1 → 1` | `+120` (`5186→5306`) | `0` | `0` | **`+120`** |
| 18 | `b3a_swe500x1_batch_mif64` | `19:37:54 → 20:09:38` | `12-vCPU` | `1904` | `0` | `1 → 1` | **`+134`** (`5306→5440`) | **`0`** | `0` | **`+134`** |
| 19 | `b3a_swe500x1_per_claim` | `20:11:13 → 20:26:29` | `12-vCPU` | `916` | `0` | `1 → 1` | `+34` (`5440→5474`) | `0` | `0` | **`+34`** |
| 20 | `b3b_swe3000_per_claim` | `20:34:05 → 21:01:58` | `12-vCPU` | `1673` | `0` | `1 → 1` | `+337` (`5474→5811`) | `0` | `0` | **`+337`** |
| 21 | `b3b_swe3000_batch_mif64` | `21:03:37 → 21:26:19` | `12-vCPU` | `1362` | `0` | `1 → 1` | **`+58`** (`5811→5869`) | **`0`** | `0` | **`+58`** |
| 22 | `b3b_swe3000_pipelined_oc60_batch_mif64` | `21:26:20 → 21:35:04` | `12-vCPU` | `524` | `0` | `1 → 1` | **`0`** (`5869→5869`) | **`0`** | `0` | **`0`** |

*\*Note on Row 4 (`b1a_r2e_500x2x8_batch_mif20`): In GKE HA (`2`–`3` apiserver replicas behind internal LB), `/metrics` scrapes hit one apiserver replica; replica B started at `0` and accumulated `+485` during `1A-2` and `+176` (`485→661`) during `1A-3` (`+3998` → `4659` in `1B-0`, `+7747` → `12406` in `1B-1`), confirming monotonic single-replica continuity from `12:55Z` until the `15:07:31Z` upsize.*

---

## 3. Benchmark-by-Benchmark Recomputed Audit (`1A`, `1B`, `2A`, `2B`, `3A`, `3B`)

### Benchmark 1A — R2E-Gym `500 × 2 × 8 = 8,000` Step-Synchronous Training (`2-vCPU` Control Plane, Fair A/B)
- **Workload:** `500` unique R2E images × `2` rollouts = `1,000` tasks/step × `8` sync barriers (`concurrency=500`, `warm_per_task=True`, `max_warmpool_size=2`, `cpu=250m`, `memory=512Mi`, `overcommit_factor=2.5`, real `python3 -c` `/testbed` probe). All 3 arms ran back-to-back on the **`2-vCPU` control plane** after `b0_cache_prime_r2e_500` (`543.34s`, `1,000/1,000` OK).

| Metric | `1A-1`: `per_claim` | `1A-2`: `batch` (`mif=20`, `rps=0`) | `1A-3`: `batch` (`mif=64`, `rps=100`) | Verified Multiplier (`1A-3` vs `1A-1`) |
|---|---:|---:|---:|---|
| `variant_wall_s` (`sum_step_wall_s`) | `2,906.55s` (`2,851.14s`) | `2,285.24s` (`2,240.72s`) | **`1,384.64s`** (`1,323.90s`) | **`2.099×` (`2.10×`) faster wall** (`1.272×` at `mif=20`) |
| `tasks_ok / total` (Error %) | `6,960 / 8,000` (**`13.00%` err**, `1,040` HTTP 429s) | **`8,000 / 8,000` (`0.00%`)** | **`8,000 / 8,000` (`0.00%`)** | **`-1,040` task failures (`13.0% → 0.0%`)** |
| `goodput_tasks_per_s` (`ok / wall_s`) | `2.3946 tasks/s` | `3.5007 tasks/s` | **`5.7777 tasks/s`** | **`2.413×` (`2.41×`) higher goodput** (`1.462×` at `mif=20`) |
| `env_ready_all_s` mean (Steps 0–7 / Steps 1–7) | `342.32s` / `340.98s` | `264.90s` / `269.70s` | **`153.10s` / `158.34s`** | **`2.236×` (all 8 steps)** / **`2.153×` (Steps 1–7)** |
| `env_ready` step mean `p50 / p95 / p99` | `295.18s / 328.68s / 335.16s` | `242.24s / 261.35s / 263.47s` | **`130.84s / 147.91s / 150.22s`** | **`2.256×` faster `p50`**, **`2.231×` faster `p99`** |
| Raw `claim.avg` (`n=8,000` all attempts) | `25.71s` (`max=72.91s`) | `11.67s` (`max=28.50s`) | **`11.94s`** (`max=52.51s`, `s1–s7 max=25.05s`) | `2.15×` raw; **`2.44×–2.47×` succeeded-only** (`29.10–29.54s` vs `11.94s`) |
| `release.avg` | `0.465s` | `0.272s` | **`0.259s`** | **`1.79×` faster release** |
| APF Δ (`total` / `ctrl` / `workload-low`) | `+2,928` (`+412` / `+2,515`) | `+485` (`0` / `+485`) | **`+176` (`0` / `+176`)** | **`16.64×` fewer total APF rejects** (`0` vs `+412` ctrl timeouts) |
| Steady step `open_sockets` (`s0..s6` / `s7` / `proc_after`) | `654–687` / `654` / `654` (`max FD=692`) | `34–122` / `501` / `501` (`max FD=506`) | **`71–155`** / `475` / `475` (`max FD=480`) | **`4.2×–9.2×` fewer steady sockets** *(Step 7 spike is `teardown()` `custom_api` pool)* |
| Peak `threads` / `vm_rss_mb` (`proc_after` HWM) | `1,278` / `602.6 MB` (`633.2 MB`) | `1,520` / `567.3 MB` (`610.8 MB`) | `1,520` / `570.1 MB` (`630.9 MB`) | Comparable memory (`~570–603 MB`) |
| `verify_teardown.remaining` (CRs / `pods`) | `0 CRs, 0 leases` (`674` Term pods) | `0 CRs, 0 leases` (`1,369` Term pods) | `0 CRs, 0 leases` (`1,923` Term pods) | **100% CR & Lease cleanup verified** |

---

### Benchmark 1B — Tunix / DeepScaleR `64 × 16 × 8 = 8,192` High-Rollout `G=16` Stress (`2-vCPU` & `12-vCPU` Matched Pairs)
- **Workload:** `64` problems × `16` rollouts = `1,024` concurrent sandboxes/step × `8` steps (`concurrency=1024`, `max_warmpool_size=16`, `overcommit_factor=4.0` after `oc=2.5` tripped `2,848 > 2,560` live+Terminating pods in diagnostic run `b1b_tunix_64x16x8_per_claim_oc25` at Step 4, simulated `mean=7.0s, std=2.5s` lognormal agent dwell).

| Metric | `1B-1`: `per_claim` (`2-vCPU`) | `1B-2`: `batch mif=20` (`2→12 vCPU`) | `1B-3`: `batch mif=64` (`12-vCPU`) | `1B-4`: `per_claim` (`12-vCPU`) | Matched `12-vCPU` Verdict (`1B-3` vs `1B-4`) |
|---|---:|---:|---:|---:|---|
| `variant_wall_s` (`sum_step_wall_s`) | `594.41s`* (`531.29s`) | `1,157.56s` (`1,091.77s`) | **`1,009.45s`** (`974.27s`) | `903.03s`* (`875.04s`) | `per_claim` wall `10.5%` shorter ONLY because it dropped `1,590` tasks (`19.4%`)! |
| `tasks_ok / total` (Error %) | `4,558 / 8,192` (**`44.36%` err**) | **`8,192 / 8,192` (`0.00%`)** | **`8,192 / 8,192` (`0.00%`)** | `6,602 / 8,192` (**`19.41%` err**: `1,589` 429s + `1` exec err) | **`+1,590` more tasks completed (`100%` vs `80.59%`)** |
| `goodput_tasks_per_s` (`ok / wall_s`) | `7.6681 tasks/s` | `7.0770 tasks/s` | **`8.1153 tasks/s`** | `7.3109 tasks/s` | **`1.110×` (`+11.0%`) higher goodput** on `12-vCPU` (`+5.8%` vs `2-vCPU` `per_claim`) |
| `env_ready_all_s` mean (Steps 0–7 / 1–7) | `58.00s`* / `55.73s`* | `128.37s` / `132.50s` | **`109.62s` / `117.25s`** | `99.88s`* / `108.47s`* | Comparable step barrier while delivering `1,024` vs `825` avg pods/step |
| `env_ready` step mean `p50 / p95 / p99` | `38.66s / 57.35s / 57.84s`* | `100.20s / 121.86s / 123.98s` | **`79.64s / 89.39s / 90.45s`** | `58.69s / 69.36s / 69.96s`* | See §4 Load-Shedding Survivorship Bias proof |
| Raw `claim.avg` / Succeeded-only `claim.avg` | `10.05s` / **`15.67–18.02s`** | `22.26s` / **`22.26s`** (exact) | `10.23s` / **`10.23s`** (exact) | `6.67s` / **`7.56–8.27s`** (`err=0` steps: `7.50, 8.88, 7.77s`) | On `2-vCPU`, `per_claim` ok claims took `15.7–18.0s` (`max 35.04s`); on `12-vCPU`, `batch` sustained `10.23s` at full `1,024` load with `0` errors |
| APF Δ (`workload-low\|queue-full`) | **`+7,747`** (`+3,998` in `oc25`) | Counter reset (`15:07:31Z`) | **`0`** | **`+4,173`** | **`+4,173 → 0` APF rejections eliminated** on `12-vCPU` |
| Step `open_sockets` (`min–max` / `proc_after`) | `796–932` / `796` (`FD=937`) | `27–133` / `133` (`FD=138`) | **`70–240` / `88`** (`FD=245`) | `620–1,026` / `994` (`FD=1,031`) | **`11.3×` fewer post-run sockets (`88` vs `994`)**, **`4.3×` lower peak sockets** |
| Peak `vm_rss_mb` (`proc_after` HWM) | `685.7 MB` (`739.9 MB`) | `630.1 MB` (`685.8 MB`) | **`658.2 MB` (`735.3 MB`)** | `842.3 MB` (`892.6 MB`) | **`1.28×` lower step RSS (`658` vs `842 MB`)** |
| `verify_teardown.remaining` | `0 CRs, 0 leases` (`1,695` pods) | `0 CRs, 0 leases` (`2,020` pods) | `0 CRs, 0 leases` (`2,744` pods) | `0 CRs, 0 leases` (`1,781` pods) | **100% CR & Lease cleanup verified** |

---

### Benchmark 2A — Standing Warm-Pool Burst Curve (`c ∈ {100, 200, 500, 1000} × 2` Reps, `12-vCPU` Control Plane)
- **Workload:** `200` pools × `5` warm replicas = `1,000` standing warm pods (`setup_s = 26.1–33.5s`); pure control-plane acquire-hold-release bursts (`0` pod cold-start on hot bursts, inter-burst `warm_wait` refills pools before next burst). Note: `.meta.json` `job_elapsed_s` (`453s`, `419s`, `403s`) includes `setup_s` + inter-burst `warm_wait` (`144–199s` after `c=1000 rep=0`).

| Burst `c` (`rep`) | `2A-1`: `per_claim` (`tot_ok=3,291/3,600`, **`8.58%` err**, APF **`+1,013`**) | `2A-2`: `batch mif=20` (`tot_ok=3,600/3,600`, **`0%` err**, APF **`0`**) | `2A-3`: `batch mif=64, rps=100` (`tot_ok=3,600/3,600`, **`0%` err**, APF **`0`**) |
|---|---|---|---|
| **`c=100` (`r=0`)** | `ok=100/100`, `wall=1.793s` (`55.77/s`), `p50=0.923s`, `p99=1.656s`, `rel=0.303s`, `sock=102` | `ok=100/100`, `wall=2.108s` (`47.43/s`), `p50=1.222s`, `p99=2.094s`, `rel=0.356s`, `sock=25` | `ok=100/100`, `wall=2.126s` (`47.03/s`), `p50=1.155s`, `p99=2.112s`, `rel=0.207s`, `sock=69` |
| **`c=100` (`r=1`)** | `ok=100/100`, `wall=1.702s` (`58.74/s`), `p50=1.079s`, `p99=1.637s`, `rel=0.237s`, `sock=102` | `ok=100/100`, `wall=1.988s` (`50.31/s`), `p50=0.894s`, `p99=1.986s`, `rel=0.384s`, `sock=25` | `ok=100/100`, `wall=2.022s` (`49.45/s`), `p50=0.738s`, `p99=2.021s`, `rel=0.435s`, `sock=69` |
| **`c=200` (`r=0`)** | `ok=200/200`, `wall=3.395s` (`58.91/s`), `p50=1.781s`, `p99=2.990s`, `rel=0.520s`, `sock=202` | `ok=200/200`, `wall=3.689s` (`54.22/s`), `p50=1.772s`, `p99=3.415s`, `rel=1.114s`, `sock=25` | `ok=200/200`, `wall=3.777s` (`52.95/s`), `p50=1.920s`, `p99=3.666s`, `rel=0.649s`, `sock=69` |
| **`c=200` (`r=1`)** | `ok=200/200`, `wall=3.740s` (`53.48/s`), `p50=1.951s`, `p99=3.248s`, `rel=0.552s`, `sock=202` | `ok=200/200`, `wall=3.565s` (`56.10/s`), `p50=1.763s`, `p99=3.490s`, `rel=1.031s`, `sock=25` | `ok=200/200`, `wall=3.715s` (`53.84/s`), `p50=1.667s`, `p99=3.701s`, `rel=0.409s`, `sock=69` |
| **`c=500` (`r=0`)** | `ok=500/500`, `wall=7.206s` (`69.39/s`), `p50=3.590s`, `p99=5.833s`, `rel=0.670s`, `sock=450` | `ok=500/500`, `wall=7.622s` (`65.60/s`), `p50=4.128s`, `p99=6.958s`, `rel=2.667s`, `sock=25` | `ok=500/500`, `wall=7.665s` (`65.23/s`), `p50=4.255s`, `p99=6.961s`, `rel=2.084s`, `sock=69` |
| **`c=500` (`r=1`)** | **`ok=469/500` (`31` 429s)**, `wall=31.963s` (**`14.67/s`**), `p50=4.167s`, **`p99=31.955s`**, `rel=0.568s`, `sock=469` | **`ok=500/500` (`0` err)**, `wall=6.437s` (**`77.67/s`**), `p50=4.292s`, **`p99=6.366s`**, `rel=2.717s`, `sock=25` | **`ok=500/500` (`0` err)**, `wall=7.645s` (**`65.40/s`**), `p50=4.503s`, **`p99=7.503s`**, `rel=1.272s`, `sock=69` |
| **`c=1000` (`r=0`)** | `ok=1000/1000`, `wall=15.320s` (`65.27/s`), `p50=6.598s`, `p99=12.076s`, **`rel=30.770s`**, `sock=880` (`878 EST`) | `ok=1000/1000`, `wall=16.250s` (`61.54/s`), `p50=8.649s`, `p99=15.573s`, **`rel=5.496s`**, `sock=25` (`22 EST, 1 CW`) | `ok=1000/1000`, `wall=18.555s` (`53.89/s`), `p50=9.034s`, `p99=17.060s`, **`rel=2.247s` (`13.69×` faster)**, `sock=69` (`66 EST, 1 CW`) |
| **`c=1000` (`r=1`)** | **`ok=722/1000` (`278` 429s)**, `wall=35.171s` (**`20.53/s`**), `p50=6.996s`, **`p90=34.571s`**, **`p99=35.140s`**, **`rel=31.553s`**, `sock=880` (**`877 CW` in `proc_before` → `275 CW + 603 EST` in `proc_after`**) | **`ok=1000/1000` (`0` err)**, `wall=19.330s` (**`51.73/s`**), `p50=9.546s`, **`p99=18.025s`**, **`rel=5.127s` (`6.15×` faster)**, `sock=25` (`1 CW, 22 EST`) | **`ok=1000/1000` (`0` err)**, `wall=17.952s` (**`55.70/s`, `2.71×` goodput**), `p50=8.459s`, **`p99=17.120s` (`2.05×` lower)**, **`rel=7.166s` (`4.40×` faster)**, `sock=69→70` (`1 CW, 66–67 EST`) |
| **Hygiene Totals** | Peak `880` sockets (`885` FDs), **`877` peak `CLOSE_WAIT`** (`275` post-burst), `1,738` threads, `427.0 MB` RSS, `0` remaining CRs/leases | Flat **`25` sockets (`30` FDs — `35.2×` fewer)**, **`≤ 1` `CLOSE_WAIT`**, `1,006` threads, `277.8 MB` RSS, `0` remaining CRs/leases | Flat **`69–70` sockets (`74–75` FDs — `12.6×–12.8×` fewer)**, **`≤ 1` `CLOSE_WAIT`**, `1,021` threads, `284.0 MB` RSS, `0` remaining CRs/leases |

---

### Benchmark 2B — `10,000`-Claim Endurance Soak (`50` Waves × `200` Conc, `12-vCPU` Control Plane)
- **Workload:** `200` pools × `4` warm = `800` standing warm pods (`overcommit_factor=4.5`); `50` waves × `200` claims = `10,000` claims + `10,000` releases. Moderate `c=200` concurrency stays below `12-vCPU` APF saturation (`0` APF rejects, `10,000/10,000` OK in both arms), isolating **pure client-side socket, FD, `CLOSE_WAIT`, thread, and RSS hygiene** over ~24 minutes.

| Metric | `2B-1`: `per_claim` | `2B-2`: `batch` (`mif=64`, `rps=100`) | Verified Comparison (`batch` vs `per_claim`) |
|---|---:|---:|---|
| `tasks_ok / total` & APF Δ | `10,000 / 10,000` (`0` err, `0` APF) | `10,000 / 10,000` (`0` err, `0` APF) | Both `100%` reliable at `c=200` on `12-vCPU` |
| `soak_wall_s` (`job_elapsed_s`) | `1,358.30s` (`1,418s`) | `1,410.69s` (`1,474s`) | `soak_wall_s` `+52.4s` (`+3.86%`) due to `warm_wait` (`106.71s` vs `8.52s`) |
| Cumulative pure `acquire_wall_s` (`50` waves) | `1,323.405s` | **`1,273.496s`** | **`batch` is `-49.91s` (`3.77%`) faster in pure claim acquisition!** |
| Cumulative `release_wall_s` / `warm_wait_s` | `25.799s` / `8.52s` | `29.956s` / `106.71s` | Faster `batch` waves hit `warm_wait` refill checks slightly sooner |
| Wave `p50` latency (`median` / `mean` / `min–max`) | `2.582s` / `2.725s` / `2.077–4.260s` | **`2.374s` / `2.383s` / `1.683–3.125s`** | **`8.07%` (`1.088×`) lower median `p50`**, **`12.56%` lower mean `p50`** |
| Wave `rate` (`median` / `mean` / `max`) | `36.55/s` / `27.80/s` / `51.40/s` | **`43.97/s` / `35.02/s` / `55.24/s`** | **`+20.3%` higher median wave rate (`43.97` vs `36.55 claims/s`)** |
| Steady `open_sockets` (`open_fds`) across all 50 waves | `202` (`207` FDs) — `min=202, max=202` | **`69` (`74` FDs) — `min=69, max=69`** | **`2.928×` (`2.93×`) fewer sockets**, zero drift across 50 waves |
| Periodic `CLOSE_WAIT` socket storms (`waves > 10`) | **5 storms: `95, 99, 91, 108, 121` (`max=121`)** at waves `10, 18, 27, 37, 46` (as Go apiserver evicts idle HTTP/1.1 keep-alives every ~4–5 min) | **ZERO storms (`min=0, max=2`)** across all 50 waves | **`60.5×` lower peak `CLOSE_WAIT` (`2` vs `121`)** |
| Peak live `threads` (`w0 → w49 → after_teardown`) | `201 → 1,800 → 1` | **`204 → 682 → 1`** | **`2.639×` (`2.64×`) fewer peak threads** |
| Process `vm_rss_mb` (`init → w49`, net growth, HWM) | `120.40 → 379.54 MB` (`+259.14 MB`, HWM `498.56 MB`) | **`120.62 → 234.29 MB` (`+113.67 MB`, HWM `361.09 MB`)** | **`2.280×` (`2.28×`) less RSS growth**, **`1.620×` lower steady RSS** |
| `verify_teardown.remaining` | `0 CRs, 0 leases` (`1,600` Term pods) | `0 CRs, 0 leases` (`1,000` Term pods) | **100% CR & Lease cleanup verified** |

---

### Benchmark 3A — SWE-Bench Verified `500 × 1` Strategy Sweep (`naive`, `sliding`, `pipelined` on `12-vCPU`)
- **Workload:** `500` unique SWE-bench images × `1` rollout = `500` tasks/variant (`concurrency=500`, `window_size=200`, `cpu=250m`, `memory=512Mi`, `overcommit_factor=3.5`, no-op `/bin/true` probe).
- **Execution History & Cache Context:**
  1. `b3_cache_prime_swe500` (`batch`, `18:01–18:17Z`): `904.46s`, `500/500` OK, `0` APF rejects.
  2. `b3a_swe500x1_per_claim_pipelined429crash` (`18:57–19:34Z`): `naive` + `sliding` ran, then `pipelined` crashed the driver process (`rc=1`, `0` JSON output) with unhandled HTTP 429 inside `_warm_entry` (`create_warmpool` without retry, `+120` APF rejects). Driver patched to add `@_k8s_retry` on `create_warmpool` + per-variant `try/except` isolation.
  3. `b3a_swe500x1_batch_mif64` (`19:37–20:09Z`): Ran **first** after rebuild (`1,500/1,500` OK across all 3 variants; `+134` `workload-low` APF rejects from `500`-wide `Template`/`WarmPool` setup/teardown).
  4. `b3a_swe500x1_per_claim` (`20:11–20:26Z`): Ran **second** on nodes freshly re-warmed by `3A-1` (`+34` `workload-low` APF rejects; `pipelined` suffered fatal HTTP 429s at `61.6%` error rate).

| Strategy | `3A-1`: `batch` (`mif=64`, `rps=100`) | `3A-2`: `per_claim` | Verified Comparison & Caveats (`3A` + 100-Node Baseline 100-Node SWE-Bench 500 Baseline`) |
|---|---|---|---|
| **`naive`** (`500`-wide) | **`wall=161.7s`** (`0.3233 s/task`), **`500/500` OK (`0%` err)**, `goodput=3.092 tasks/s`, `env_ready p50=120.53s / p99=125.16s`, `claim avg=4.75s (max 9.61s)`, `wpr max=46.34s`, `sock=420` | `wall=447.1s` (`0.8942 s/task`), `500/500` OK (`0%` err), `goodput=1.118 tasks/s`, `env_ready p50=430.46s / p99=434.84s`, `claim avg=4.53s (max 8.95s)`, `wpr max=211.94s`, `sock=474` | **`batch` is `2.765×` (`2.77×`) faster wall** & **`3.571×` faster `env_ready p50`**! Also beats 100-Node Baseline 100-Node SWE-Bench 500 Baseline`'s **`100-node` (`10×` larger cluster!)** `R2` warm `207.1s` by **`1.281×`** and `R3` streaming `279.7s` by **`1.730×`**! |
| **`sliding`** (`W=200`) | `wall=1,238.9s` (`2.4778 s/task`), **`500/500` OK (`0%` err)**, `goodput=0.404 tasks/s`, `env_ready p50=1,006.95s / p99=1,236.45s`, `claim avg=1.97s (max 4.69s)`, **`wpr max=601.43s`**, `sock=152` | `wall=295.1s` (`0.5903 s/task`), `500/500` OK (`0%` err), `goodput=1.694 tasks/s`, `env_ready p50=194.36s / p99=292.06s`, `claim avg=1.91s (max 4.43s)`, `wpr max=120.96s`, `sock=203` | **Methodological disclosure:** `claim.avg` was identical (`1.97s` vs `1.91s` — `batch` was NOT slower at claiming!). `3A-1` suffered a single cold-node `gcfsd` FUSE stall (`wait_pool_ready.max = 601.43s`), which hydrated the cache right before `3A-2` ran (`wpr.max = 120.96s`). On `500×6` (`3B`), `batch sliding` beat `per_claim` **`503.4s` vs `707.8s` (`1.406×`)**. |
| **`pipelined`** (`W=200` × `2`) | **`wall=480.6s`** (`0.9612 s/task`), **`500/500` OK (`0%` err)**, `goodput=1.040 tasks/s`, `env_ready p50=430.41s / p99=476.61s`, `claim avg=2.59s (max 5.81s)`, `wpr max=376.62s`, `sock=318` | **`FATAL` HTTP 429 crash at `147.5s`**: **`192/500` OK (`308` err = `61.60%` err)**, `claim n=200, avg=2.96s (max 31.69s)`, `Retry-After: 31s`, `sock=387` | **`per_claim` `pipelined` failed `61.6%` of tasks** even on `12-vCPU` (`claim.max` spiked **`5.45×` higher**: `31.69s` vs `5.81s`), whereas `batch` completed **`500/500` (`100%`)** with `0` errors. |

---

### Benchmark 3B — Official `run_basic_swebench_3000.py` (`500 × 6 = 3,000` Tasks, Direct `RESULTS.md` Parity on `12-vCPU`)
- **Workload:** Exact `T0` `RESULTS.md` recipe: `500` images × `6` rollouts = `3,000` tasks/variant (`concurrency=500`, `window_size=200`, `cpu=125m`, `memory=1Gi`, `probe=true`, `recycle=False`, `10 × e2-standard-32` gVisor `maxPods=256`, Image Streaming ON).
- **Execution Order:** `3B-1` (`b3b_swe3000_per_claim`) ran **first** (`20:34–21:01Z`), `3B-2` (`b3b_swe3000_batch_mif64`, `oc=3.5`) ran **second** (`21:03–21:26Z`), and `3B-3` (`b3b_swe3000_pipelined_oc60_batch_mif64`, `oc=6.0`) ran **third** (`21:26–21:35Z`).

| Strategy | `3B-1`: `per_claim` (`wp=50`) | `3B-2` / `3B-3`: `batch mif=64` (`wp=50`) | Actual `RESULTS.md` (`#10–#12`, `#15`, `wp=500`) | Verified Speedups (`batch` vs `per_claim` & vs `RESULTS.md`) |
|---|---|---|---|---|
| **`naive`** (`3,000`) | `wall=688.7s` (`0.2296 s/task`), `3,000/3,000` OK (`0%`), `goodput=4.356/s`, `env_ready p50=369.37s / p99=602.31s`, `claim avg=55.87s (max 370.55s)`, `sock=531` | **`wall=523.9s`** (`0.1746 s/task`), **`3,000/3,000` OK (`0%`)**, `goodput=5.726/s`, **`env_ready p50=156.65s / p99=396.58s`**, `claim avg=56.41s (max 444.85s)`, `sock=360` | Row `#10`: **`587.7s`** (`0.1959 s/task`, `3000/0`)<br>Row `#15`: **`669.0s`** (`0.2230 s/task`, `3000/0`) | **`1.315×` faster than `per_claim`** (`2.36×` lower `env_ready p50`!)<br>**`1.122×` faster than `RESULTS.md` `#10` (`587.7s`)**<br>**`1.277×` faster than `RESULTS.md` `#15` (`669.0s`)** |
| **`sliding`** (`W=200`) | `wall=707.8s` (`0.2359 s/task`), **`2,991/3,000` OK (`9` HTTP 429s = `0.30%` err)**, `goodput=4.226/s`, `env_ready p50=488.38s / p99=679.28s`, `claim avg=17.85s (max 319.95s)`, `sock=693` | **`wall=503.4s`** (`0.1678 s/task`), **`3,000/3,000` OK (`0%`)**, `goodput=5.960/s`, **`env_ready p50=209.73s / p99=497.09s`**, `claim avg=36.94s (max 189.55s)`, **`sock=87`** | Row `#11`: **`603.8s`** (`0.2013 s/task`, `3000/0`) | **`1.406×` faster than `per_claim`** (`0` vs `9` 429s, **`7.97×` fewer sockets**: `87` vs `693`, `2.33×` lower `env_ready p50`)<br>**`1.199×` (`1.20×`) faster than `RESULTS.md` `#11` (`603.8s`)** — and beats `#12`'s `507.4s`! |
| **`pipelined`** (`W=200` × `2`) | **`FATAL` HTTP 429 crash at `249.7s`**: **`1,065/3,000` OK (`1,935` err = `64.50%` err)**, `goodput=4.265/s`, `claim n=1200, avg=11.66s (max 180.81s)`, `Retry-After: 29s`, `sock=519` | `3B-2` (`oc=3.5`): tripped `Terminating` pod guard (`1,758 > 1,750`) at `215.2s` (`1,594` OK = **`7.41 tasks/s` goodput**, `2,400` claims = `11.15 claims/s`).<br>**`3B-3` (`oc=6.0`): `wall=502.2s` (`0.1674 s/task`), `3,000/3,000` OK (`0%` err), `0` APF rejects**, `goodput=5.974/s`, `env_ready p50=187.42s / p99=491.53s`, `claim avg=49.22s (max 196.33s)`, `sock=247` | Row `#12`: **`507.4s`** (`0.1691 s/task`, `3000/0`, previous all-time `T0` best) | **NEW `T0` RECORD (`502.2s` vs `#12` `507.4s` = `-5.2s` / `1.010×` faster)** with **`0` errors, `0` APF rejects, and `wp-workers=50` (10× fewer warm-pool workers than `#12`'s `wp=500`)**, while `per_claim` crashed at `64.5%` error rate! |

---

## 4. Survivorship-Bias & Succeeded-Only Latency Verification

### Code-Level Proof of What Each Metric Records
Direct inspection of `driver_build/drivers/{bench_core.py,run_step_bench.py,run_claim_soak_bench.py}`, `agent_sandbox_rl/observability.py`, and `k8s_agent_sandbox/k8s_helper.py` proves two distinct behaviors:

1. **`env_ready_ok_s` (`3A`, `3B`), `env_ready_*_s` (`1A`, `1B`), and `latency_s` (`2A`, `2B`) ALREADY INCLUDE ONLY SUCCEEDED TASKS:**
   - In `bench_core.py:263` (`run_variant`), `process_fn` only runs *after* `af.acquire()` succeeds and yields a live `SandboxLease`, appending `t_ready = time.monotonic() - t_run0` to `env_ready_ok` (`n == tasks_ok` on every run: e.g., `n=192` in `3A-2 pipelined`, `n=2991` in `3B-1 sliding`, `n=1065` in `3B-1 pipelined`).
   - In `run_step_bench.py:39` (`on_ready`), `step_env_ready` is recorded inside `process()`, which `af.run()` only invokes after `acquire()` succeeds (`len(step_env_ready) == claims_ok`: `6,960` in `1A-1`, `4,558` in `1B-1`, `6,603` in `1B-4`).
   - In `run_claim_soak_bench.py:186,345`, `ok_lats` appends `dt` only inside the `try:` block after `af.acquire()` returns (`len(ok_lats) == ok`).
   - **Therefore, `env_ready_*` and `latency_s` are NOT diluted by fast-failing 429s — HOWEVER, in `1B` and `3A/3B` aborted runs they suffer from *load-shedding survivorship bias* (below)!**

2. **`phases["claim"]` (`claim.avg`, `claim.total`, `claim.n`) RECORDS ALL ATTEMPTS (`ok + failed_in_claim`) AND IS DILUTED BY FAST-FAILING `POST` 429s:**
   - `Observer.phase("claim")` (`observability.py:260–286`) records `dt` in `finally:`, so `claim.n = ok_claims + failed_claims`.
   - In `k8s_helper.py`, `urllib3.Retry(total=3, status_forcelist=[429,...], respect_retry_after_header=True)` uses `DEFAULT_ALLOWED_METHODS` (`GET/HEAD/PUT/DELETE/OPTIONS/TRACE` — **`POST` IS EXCLUDED!**).
   - Thus, when APF rejects `POST /sandboxclaims` with `429 queue-full`, `per_claim` **fails immediately (`~0.05–1s`) without retrying**, artificially dragging `claim.avg` **DOWN**! Conversely, when `POST` succeeds and `GET /sandboxclaims?watch=true` gets `429 Retry-After: 1..31s`, `urllib3` sleeps `Retry-After` (`31s` on `2-vCPU` / `29–31s` in `3A/3B`), creating `31–35s` latency cliffs!

### Verified Succeeded-Only `claim.avg` Bounds & Clean-Step (`err == 0`) Latencies

| Run (Arm with HTTP 429s) | `claim.n` (`ok` / `err_in_claim`) | Raw `claim.avg` (All Attempts) | Clean `err == 0` Step `claim.avg` (`max`) | Post-Hoc Succeeded-Only `claim.avg` Bounds (if failed claims took `0.05s .. 3.0s`) | Matched `batch (mif=64)` Exact `claim.avg` (`err=0`) |
|---|---:|---:|---|---|---:|
| **`1A-1`: `per_claim` (`2-vCPU`)** | `8,000` (`6,960` / `1,040`) | `25.71s` | None (`0/8` clean steps; `99–165` errs/step) | **`29.10s – 29.54s`** (step bounds `27.81s – 31.49s`) | **`11.94s`** (`mif=20`: **`11.67s`**) → **`batch` is `2.44×–2.47×` faster** on succeeded claims! |
| **`1B-1`: `per_claim` (`2-vCPU`)** | `8,192` (`4,558` / `3,634`) | `10.05s` | None (`0/8` clean steps; `313–553` errs/step, `max=34.61–35.04s` every step) | **`15.67s – 18.02s`** (step bounds `12.81s – 20.20s`) | `1B-2` (`mif=20`): `22.26s` (`100%` of `8,192` tasks vs `55.6%`) |
| **`1B-4`: `per_claim` (`12-vCPU`)** | `8,192` (`6,603` / `1,589`) | `6.67s` | **3 clean `err == 0` steps (`1,024/1,024`):**<br>• `s1`: **`7.501s`** (`max 15.51s`)<br>• `s3`: **`8.884s`** (`max 18.05s`)<br>• `s5`: **`7.767s`** (`max 21.60s`)<br>*(Mean of clean steps = **`8.051s`**; vs diluted `s0` `2.956s` with `545` 429s!)* | **`7.56s – 8.27s`** across all `6,603` succeeded claims (matches clean-step mean **`8.05s`**!) | **`10.23s`** (`9.58s – 11.09s` across all `8/8` steps at `100%` `1,024`-pod load, `0` errors, **`+11.0%` higher goodput**) |
| **`2A-1`: `per_claim` (`c=500 r=1`, `c=1000 r=1`)** | `500` (`469`/`31`)<br>`1,000` (`722`/`278`) | `ok_lats` already succeeded-only: `avg=5.19s (p99=31.96s)` at `c=500 r=1`; `avg=14.73s (p90=34.57s, p99=35.14s)` at `c=1000 r=1` | Clean `r=0` bursts:<br>• `c=500 r=0`: `avg=3.49s, p99=5.83s`<br>• `c=1000 r=0`: `avg=6.77s, p99=12.08s` (`rel=30.77s`) | Already exact succeeded-only (`avg` jumped **`2.18×`** from `6.77s → 14.73s` and `p90` jumped **`3.20×`** from `10.79s → 34.57s` due to `GET ?watch` `Retry-After`!) | `c=500 r=1`: `avg=4.21s, p99=7.50s` (`0` err)<br>`c=1000 r=1`: `avg=8.87s, p99=17.12s` (`0` err, **`2.71×` higher goodput**) |
| **`3A-2`: `per_claim` (`pipelined` crash)** | `200` (`192` / `8` in claim + `300` unentered) | `2.964s` (`max=31.69s`) | N/A (variant aborted at `147.5s`) | **`1.77s – 3.09s`** (for the `192` claims before abort; `claim.max = 31.69s`) | **`2.594s`** (`max=5.81s`, **`500/500` OK**) |
| **`3B-1`: `per_claim` (`sliding` & `pipelined`)** | `sliding`: `3,000` (`2,991`/`9`)<br>`pipelined`: `1,200` (`1,065`/`135` + `1,800` unentered) | `sliding`: `17.848s` (`max=319.95s`)<br>`pipelined`: `11.659s` (`max=180.81s`) | `naive` (`0` err): `55.871s` (`max=370.55s`) | `sliding`: **`17.81s – 17.90s`**<br>`pipelined`: **`9.33s – 13.13s`** (only first `1,065/3,000` tasks before `249.7s` crash) | `sliding`: `36.937s` (`max=189.55s`, **`1.406×` faster wall**, `0` err)<br>`pipelined`: `49.217s` (`max=196.33s`, **`502.2s` `3,000/3,000` OK**) |

### Load-Shedding Survivorship Bias in `1B` (`per_claim` `step_wall_s` & `env_ready_*`)
Why did `1B-1` (`per_claim` `2-vCPU`, `594.41s`) and `1B-4` (`per_claim` `12-vCPU`, `903.03s`) report shorter total `variant_wall_s` and lower mean `env_ready_p50` than `1B-3` (`batch mif=64`, `1,009.45s`)?
1. **Unconsumed Warm Pods Skip Refill Latency:** Whenever `per_claim` drops `N` claims in Step `k` via HTTP 429 (`545` dropped in `1B-4` Step 0; `387` dropped in Step 6), **those `N` warm pods remain sitting idle and `Ready` in the `SandboxWarmPool`!** Consequently, Step `k+1`'s `wait_pool_ready` barrier (`wpr`) sees an already-full pool (`wpr.tot = 0.50s` in `1B-4` Step 1 and `0.60s` in Step 7!), completely skipping gVisor pod creation for `19.4%` (`12-vCPU`) to `44.4%` (`2-vCPU`) of the workload.
2. **Proof from Clean Steps in `1B-4`:** Whenever `1B-4` actually completed `1,024/1,024` claims (`s1`, `s3`, `s5`), the *following* step had to replenish all `1,024` consumed warm pods — and `step_wall_s` immediately jumped to `120.26s` (`s2`), `200.54s` (`s5`), and `223.73s` (`s6`), matching `1B-3` (`batch` `100%` load)! Comparing raw `variant_wall_s` without normalizing by `tasks_ok` (`goodput_tasks_per_s`) rewards `per_claim` for failing `19.4%`–`44.4%` of rollouts; on `goodput_tasks_per_s`, `batch mif=64` wins **`8.12` vs `7.31 tasks/s` (`+11.0%`)** with **`100%` step integrity**.

---

## 5. Mandatory Methodological Caveats for Honest Disclosure

The final report MUST explicitly disclose all four caveats below:

1. **GKE Control-Plane Auto-Upsize (`2-vCPU → 12-vCPU` at `2026-11-04T15:07:31Z`) & Single Inter-Run Controller Restart (`15:11:47Z`):**
   - **Bench 1A:** All 3 arms (`1A-1`, `1A-2`, `1A-3`) ran entirely on the **`2-vCPU` control plane** — **100% fair A/B**.
   - **Bench 1B:** Spans the upsize, so the campaign includes **matched pairs on BOTH tiers**: `1B-1` (`per_claim`) vs `1B-2` (`batch mif=20`, mostly `2-vCPU` until `15:07:31Z`), and **`1B-3` (`batch mif=64`) vs `1B-4` (`per_claim`) both 100% on steady `12-vCPU`**. APF delta for `1B-2` is unavailable due to the `15:07:31Z` apiserver counter reset, and `agent-sandbox-controller` restarted once (`0 → 1`) between `1B-2` and `1B-3` at `15:11:47Z` (`0` restarts during any of the 22 runs).
   - **Bench 2A, 2B, 3A, 3B:** All arms ran **100% on the steady `12-vCPU` control plane** — **100% fair A/B** (and note that `per_claim` STILL suffered HTTP 429 failures in `1B-4`, `2A-1`, `3A-2 pipelined`, and `3B-1 sliding+pipelined` even on `12-vCPU`!).

2. **Execution Order & Node `gcfsd` Image-Streaming Cache Hydration (`Bench 3A` vs `Bench 3B`):**
   - Even after `b3_cache_prime_swe500`, secondary image layer chunks across `10` nodes are fetched lazily on demand by `gcfsd`.
   - In **Bench 3A (`500×1`)**, `b3a_swe500x1_batch_mif64` ran *before* `b3a_swe500x1_per_claim` and hit a single-image `601.43s` `wait_pool_ready` stall during `sliding` (`wall = 1,238.9s`), even though `claim.avg` was `1.971s` (`batch`) vs `1.905s` (`per_claim`). That run hydrated the remaining layer chunks before `3A-2` (`per_claim` `sliding` `295.1s`, `wpr.max = 120.96s`).
   - Conversely, in **Bench 3B (`500×6 = 3,000`)**, `b3b_swe3000_per_claim` ran *before* `b3b_swe3000_batch_mif64`. Note, however, that in `3B-1` `per_claim` `naive` (`688.7s`), `wait_pool_ready.max` was already warm (`108.61s`) and the bottleneck was purely `claim` (`avg = 55.87s, max = 370.55s`), and in `sliding` (`707.8s` vs `503.4s`) both arms had already executed `3,000+` tasks on warm nodes (`wpr.avg = 7.60s` vs `8.87s`).

3. **Client-Side `--overcommit-factor` Counting `Terminating` Pods (`live_owned_count()`) & Masked `FleetError`:**
   - `cluster.live_owned_count()` (`cluster.py:397–405`) counts ALL pods carrying label `agent-sandbox-rl/run=<run_id>` regardless of `deletionTimestamp` (`Running + Pending + Terminating`).
   - Under `batch`'s higher throughput (`7.4–8.1 tasks/s`), async kubelet/gVisor pod termination (~`30–90s` GC lag) accumulates `1,000–2,744` harmless `Terminating` pods alongside active warm pods.
   - Raising `--overcommit-factor` (`2.5 → 4.0` in `1B`, `4.5` in `2B`, and `3.5 → 6.0` in `3B pipelined`, where `3B-2` tripped `1,758 > 1,750` at `215.2s` / **`7.41 tasks/s` completed goodput**) only relaxes this client-side safety poll; it does not increase cluster resources or controller concurrency.
   - **SDK Bug Note (`fleet.py:348–378`):** When `asrl-breaker` trips, it calls `self.teardown()` immediately inside the background daemon thread, deleting active `SandboxWarmPool`s while `_warm_entry` is polling them — causing `_warm_entry` to raise `FleetError: warm pool ... did not become ready within 900s` before `overcommit_guard` exits and checks `if tripped["n"]: raise FleetOvercommitError(...)`.

4. **Hardware & Controller-Flag Comparison vs External Baselines (`100-Node Baseline 100-Node SWE-Bench 500 Baseline` & `RESULTS.md` `#10–#12`, `#15`):**
   - **Vs 100-Node Baseline 100-Node SWE-Bench 500 Baseline` (`500×1`):** the 100-node baseline's sweep (`R2` warm `207.1s`, `R3` streaming `279.7s`) used **`100 × e2-standard-32` nodes (`3,200 vCPU`)** — **10× more nodes** than our `10 × e2-standard-32` (`320 vCPU`) `T0` cluster, and `cpu=2` per pod vs our `cpu=250m`. Beating `207.1s` with `161.7s` (`1.281×` faster) on 1/10th the node fleet is remarkable, and the cluster/pod spec difference must be stated explicitly.
   - **Vs `RESULTS.md` Rows `#10–#12` & `#15` (`500×6 = 3,000`):** Exact `T0` hardware & workload parity (`10 × e2-standard-32` gVisor, `maxPods=256`, Image Streaming ON, `500` images × `6` cycles, `conc=500`, `125m / 1Gi`, `recycle=no`, no-op probe). **Controller worker difference:** `RESULTS.md` `#10–#12` & `#15` ran with `Ctrl (sbx/wp) = 1000/500` (`--sandbox-concurrent-workers=1000`, `--sandbox-warm-pool-concurrent-workers=500`), whereas our campaign ran with the M2.5-recommended `500/50` (`--sandbox-concurrent-workers=500`, `--sandbox-warm-pool-concurrent-workers=50`, `--kube-api-qps=500`, `--kube-api-burst=1000`). Beating `#10` (`523.9s` vs `587.7s`), `#11` (`503.4s` vs `603.8s`), `#12` (`502.2s` vs `507.4s`), and `#15` (`523.9s` vs `669.0s`) with **10× fewer warm-pool workers** is a stronger, completely defensible result once cited with exact numbers.
