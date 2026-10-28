# Full In-Depth Results Reference: `claim_mode="batch"` vs. `per_claim` on GKE

This document contains **every suite, arm, and metric column** collected during the GKE scale study (**103 benchmark runs** across 5 suites). For the plain-English narrative and setup explanation, read [`WALKTHROUGH.md`](./WALKTHROUGH.md) first.

---

## Metric & Variant Glossary

### Variants Tested
* **`per-claim`**: `FleetConfig(claim_mode="per_claim")` — unpaced individual `create_sandbox()` + 1 watch stream + 2 `GET sandboxes` per task.
* **`batch-default`**: `FleetConfig(claim_mode="batch")` — out-of-the-box SDK `BatchClaimConfig()` defaults (`max_in_flight=20`, `create_rps=50` [cold only], `1` shared watch).
* **`batch-unbounded`**: `FleetConfig(claim_mode="batch", batch_claims=BatchClaimConfig(max_in_flight=998, create_rps=1e6))` — lifts client create pacing up to the `1,000`-socket `urllib3` pool cap.
* **`fill-unbounded`**: Wave-mode control arm — opens a single `claim_batch` per step with group sizes pre-declared up front rather than `N` concurrent `fleet.acquire()` calls (`1.50` reqs/env because `warm_pool_Ensure` + `batch` both read pool status).
* **`per-claim-c50` / `batch-c50`**: Caps concurrent `fleet.acquire()` calls at `50` via a driver semaphore (matching the earlier `kind` benchmark's concurrency cap).

### Columns Reported (Steady State = Median of Step 2 Across Interleaved Runs)
* **`Errors`**: Total failed sandbox acquisitions across all steps of all runs for that arm (`0` = 100% success).
* **`TTFC` (`ms`)**: Time to First Claim Ready (`acquired_s.first` in Step 2) — how fast the first agent rollout can begin work.
* **`Ready p50` / `p99` / `last (min–max)` (`s`)**: Median, 99th-percentile, and full-batch (`last`) `fleet.acquire()` Ready time on Step 2, with `(run1–run2)` range.
* **`Last Start` (`s`)**: Wall time until the last sandbox finishes both `fleet.acquire()` **and** its in-sandbox `exec` probe.
* **`Step 1 Ready` (`s`)**: Full-batch `fleet.acquire()` Ready time on **Step 1** (includes cold client TLS/connection setup & opening the batch).
* **`Reqs/Env`**: Client-side K8s HTTP requests issued by the Python driver divided by $N$ sandboxes.
* **`Watches`**: Concurrent HTTP `WATCH` streams opened by the Python driver during the step.
* **`Discards`**: `urllib3` `"Connection pool is full, discarding connection"` events when concurrent sockets exceed `connection_pool_maxsize=1000`.
* **`Driver CPU` (`s`)**: Total user + system CPU seconds burned by the Python benchmark process during the step (`time.process_time()`).
* **`API Create` (`ms`)**: Server-side `kube-apiserver` mean `POST sandboxclaims` latency scraped from `/metrics`.
* **`APF Wait` (`s`)**: Cumulative server-side Kubernetes API Priority & Fairness queue wait (`apiserver_flowcontrol_request_wait_duration_seconds_sum` delta) across the step.
* **`Claim API Reqs`**: Total cluster-wide `kube-apiserver` requests on `sandboxclaims` + `sandboxes` reads (driver + controller combined).
* **`429s`**: Total server-side HTTP `429 Too Many Requests` responses recorded by `kube-apiserver` `/metrics` during the step.

---

## Suite 1: Production-Tuned Controller — Scale Sweep (`160 → 640 → 1,280 → 2,560`)

* **Directory:** [`bench-results/gke-tuned/`](../gke-tuned/summary.md) (`30` runs; `24` gVisor nodes for `w160–w1280`, `48` gVisor nodes for `w2560`).
* **Controller flags ([`docs/performance-tuning.md`](../../../../docs/performance-tuning.md#L508-L530) Sustained High-Throughput Profile):** `--api-connections=4 --separate-watch-connection=true --sandbox-concurrent-workers=200 --sandbox-claim-concurrent-workers=200 --sandbox-warm-pool-concurrent-workers=2 --sandbox-warm-pool-replenish-delay=0 --sandbox-warm-pool-max-refill-rate=100 --cache-label-selectors=true --disable-sandbox-events=true --disable-claim-events=true --disable-claim-observability-annotations=true`.

| Shape / Variant | Runs | Errors | TTFC (ms) | Ready p50 (s) | Ready p99 (s) | Last Ready s (min–max) | Last Start (s) | Step 1 Ready (s) | Reqs / Env | Watches | Discards | Driver CPU (s) | API Create (ms) | APF Wait (s) | Claim API Reqs | 429s |
|---|---:|---:|---:|---:|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| `w160 / batch-default` | 2 | `0` | **135.9** | 1.18 | 2.33 | **2.34** (2.32–2.37) | 2.52 | 2.54 | 1.26 | 1 | 0 | 1.17 | 45.41 | 4.54 | 747 | 0 |
| `w160 / batch-unbounded` | 2 | `0` | 213.5 | **1.15** | **2.24** | 2.38 (2.37–2.40) | **2.48** | **2.28** | 1.26 | 1 | 0 | **1.06** | 58.09 | 10.67 | 742 | 0 |
| `w160 / fill-unbounded` | 2 | `0` | 1023.0 | 1.90 | 3.00 | 3.05 (2.90–3.20) | 3.18 | 3.01 | 1.52 | 1 | 0 | 1.10 | 26.96 | 26.02 | 736 | 0 |
| `w160 / per-claim` | 2 | `0` | 515.7 | 1.37 | 2.44 | 2.48 (2.43–2.53) | 2.61 | 2.39 | 4.00 | 160 | 0 | 2.06 | 87.81 | 22.24 | 1,224 | 0 |
| `w640 / batch-default` | 2 | `0` | **602.8** | **4.78** | **8.50** | **8.73** (8.68–8.78) | **8.92** | **8.92** | 1.25 | 1 | 0 | 6.29 | **75.38** | **497.75** | 2,840 | **0** |
| `w640 / batch-unbounded` | 2 | `0` | 757.7 | 4.81 | 18.65 | 18.95 (8.23–29.68) | 19.05 | 17.38 | 1.25 | 1 | 0 | 7.19 | 127.75 | 506.63 | 2,806 | 10 |
| `w640 / fill-unbounded` | 2 | `0` | 4135.3 | 7.88 | 17.59 | 17.78 (12.87–22.68) | 18.03 | 15.75 | 1.50 | 1 | 0 | **5.32** | 130.92 | 611.30 | 2,807 | 13 |
| `w640 / per-claim` | 2 | `0` | 3119.6 | 5.36 | 19.30 | 19.54 (9.71–29.37) | 19.64 | 20.90 | 4.00 | 640 | 0 | 15.84 | 132.97 | 698.95 | 4,735 | 50 |
| `w1280 / batch-default` | 2 | `0` | 1338.8 | 10.20 | 24.04 | 24.59 (18.57–30.61) | 24.85 | 33.75 | 1.25 | 2 | 0 | 17.69 | **84.90** | **847.88** | 5,529 | **22** |
| `w1280 / batch-unbounded` | 2 | `0` | 358.4 | **8.47** | **19.16** | 20.14 (16.86–23.41) | 20.29 | 27.62 | 1.25 | 1 | 0 | 11.26 | 257.36 | 970.84 | 5,502 | 50 |
| `w1280 / fill-unbounded` | 2 | `0` | 8013.1 | 15.16 | 24.87 | 25.24 (23.71–26.77) | 25.39 | 24.23 | 1.50 | 1 | 0 | **11.05** | 242.66 | 1520.19 | 5,375 | 32 |
| `w1280 / per-claim` | 2 | `0` | **316.7** | 9.18 | 19.60 | **19.71** (18.47–20.96) | **19.85** | **23.65** | 4.00 | 1,280 | 0 | 24.64 | 156.16 | 949.01 | 9,388 | 98 |
| `w2560 / batch-default` | 2 | **`0`** | **194.0** | **16.53** | **33.67** | **47.40** (41.98–52.83) | **47.54** | 60.11 | 1.25 | 2 | **`0`** | **21.97** | **88.28** | **1840.52** | **10,668** | **77** |
| `w2560 / batch-unbounded` | 2 | **`0`** | 601.0 | 16.91 | 37.58 | 51.58 (**35.48**–67.68) | 51.71 | 57.76 | 1.13 | 2 | **`0`** | 22.13 | 256.15 | 2578.20 | 10,757 | 82 |
| `w2560 / per-claim` | 2 | **`1` ❌** | 273.5 | 17.94 | 33.96 | 46.05\* (34.80–**57.29**) | 46.18\* | 49.44 | 4.00 | 2,560 | **`551`** | 44.42 | 177.19 | 2869.79 | 18,317 | 90 |

*\*In `w2560 / per-claim`, run 1 (`0` errors) took **`57.29s`** (slower than both `batch-unbounded`'s **`35.48s`** and `batch-default`'s **`41.98s`**), while run 2 failed an environment with `ApiException: (429) Too Many Requests` (`34.80s` is over the remaining `2,559` sandboxes).*

---

## Suite 2: Production-Tuned Controller — Workload Shapes (`500 × 1` Eval & `16 × 64` High-Rollout)

* **Directory:** [`bench-results/gke-shapes/`](../gke-shapes/summary.md) (`12` runs on `48` gVisor nodes, same Production-Tuned Controller flags as Suite 1).
* **Shapes:** `w500` = `500 images × 1 task` (SWE-bench 1:1 evaluation); `w1024` = `16 images × 64 tasks` (high-rollout / Best-of-64 / shared-image RL stress test).

| Shape / Variant | Runs | Errors | TTFC (ms) | Ready p50 (s) | Ready p99 (s) | Last Ready s (min–max) | Last Start (s) | Step 1 Ready (s) | Reqs / Env | Watches | Discards | Driver CPU (s) | API Create (ms) | APF Wait (s) | Claim API Reqs | 429s |
|---|---:|---:|---:|---:|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| `w500 (500×1) / batch-default` | 2 | `0` | **280.1** | **3.71** | 7.46 | 7.62 (7.45–7.79) | 7.72 | **7.95** | 2.01 | 4 | 0 | **4.41** | **81.38** | 428.05 | **2,294** | 0 |
| `w500 (500×1) / batch-unbounded` | 2 | `0` | 305.8 | 4.40 | **6.91** | **7.01** (6.95–7.07) | **7.35** | **6.75** | 2.01 | 4 | 0 | 5.12 | 137.60 | **397.13** | 2,342 | 0 |
| `w500 (500×1) / per-claim` | 2 | `0` | 738.2 | 4.05 | 6.93 | 7.27 (7.03–7.51) | 7.38 | 33.51 | 4.00 | 500 | 0 | 8.51 | 186.62 | 507.25 | 3,844 | 0 |
| `w1024 (16×64) / batch-default` | 2 | **`0`** | **199.4** | **7.05** | 31.74 | 35.82 (35.72–35.91) | 35.91 | **31.08** | **1.02** | **1** | **`0`** | **7.79** | **85.37** | **675.79** | 4,555 | 58 |
| `w1024 (16×64) / batch-unbounded` | 2 | **`0`** | 447.6 | 7.09 | **24.42** | **33.83** (32.03–35.63) | **34.01** | 38.50 | **1.02** | 3 | **`0`** | 8.90 | 263.39 | 935.99 | **4,316** | **41** |
| `w1024 (16×64) / per-claim` | 2 | **`101` ❌** | 4107.8 | 8.73 | 32.41 | 37.39 (30.48–44.30) | 37.52 | 34.93 | 4.00 | 1,023 | **`24`** | 27.02 | 315.12 | 1751.61 | 8,676 | 70 |

---

## Suite 3: Out-of-the-Box Default Controller — Warm (`160 → 1,280`) & Cold (`c640`)

* **Directory:** [`bench-results/gke/`](summary.md) (`32` runs on `24` gVisor nodes).
* **Controller flags:** Stock release defaults (`--leader-elect=true --extensions`: `100` Sandbox workers, `50` SandboxClaim workers, `1` WarmPool worker, `1` shared API connection).
* Includes **`c640` (cold sandboxes)**: `160` warm pools scaled to `0` replicas so every claim cold-creates a fresh gVisor pod on demand (`create_rps` pacing active).

| Shape / Variant | Runs | Errors | TTFC (ms) | Ready p50 (s) | Ready p99 (s) | Last Ready s (min–max) | Last Start (s) | Step 1 Ready (s) | Reqs / Env | Watches | Discards | Driver CPU (s) | API Create (ms) | APF Wait (s) | Claim API Reqs | 429s |
|---|---:|---:|---:|---:|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| `w160 / batch-default` | 2 | `0` | **177.6** | **1.12** | **2.10** | **2.12** (1.94–2.29) | **2.20** | **2.34** | 1.26 | 1 | 0 | 1.17 | **23.77** | **0.18** | 540 | 0 |
| `w160 / batch-unbounded` | 2 | `0` | 242.8 | 1.29 | 2.14 | 2.16 (2.07–2.25) | 2.26 | **2.34** | 1.25 | 0 | 0 | **1.14** | 36.98 | 0.43 | **531** | 0 |
| `w160 / batch-c50` | 2 | `0` | 214.4 | 1.27 | 2.89 | 4.24 (2.69–5.78) | 4.34 | 3.09 | 1.25 | 0 | 0 | 1.20 | 27.15 | 4.32 | 1,100 | 0 |
| `w160 / fill-unbounded` | 2 | `0` | 1814.5 | 2.87 | 3.91 | 3.96 (3.12–4.79) | 4.12 | 3.26 | 1.52 | 1 | 0 | 1.16 | 41.09 | 8.78 | 917 | 0 |
| `w160 / per-claim` | 2 | `0` | 552.9 | 1.28 | 2.17 | 2.36 (1.86–2.86) | 2.44 | 2.52 | 4.00 | 160 | 0 | 2.38 | 67.42 | 21.10 | 1,022 | 0 |
| `w160 / per-claim-c50` | 2 | `0` | 196.7 | 1.23 | 3.22 | 5.23 (2.60–7.87) | 5.35 | 2.38 | 4.00 | 160 | 0 | 1.89 | 38.08 | 8.38 | 1,622 | 0 |
| `w640 / batch-default` | 2 | `0` | **338.2** | **5.17** | **9.26** | **9.39** (9.35–9.43) | **9.75** | **8.62** | 1.25 | 1 | 0 | 5.10 | **42.03** | 22.33 | 3,404 | 0 |
| `w640 / batch-unbounded` | 2 | `0` | 659.6 | 5.38 | 10.14 | 10.34 (10.29–10.38) | 10.51 | 10.64 | 1.25 | 0 | 0 | 5.96 | 119.39 | **21.30** | 3,678 | 0 |
| `w640 / fill-unbounded` | 2 | `0` | 4334.0 | 7.63 | 13.13 | 13.30 (12.81–13.79) | 13.47 | 12.32 | 1.50 | 1 | 0 | **4.71** | 70.12 | 38.14 | **3,294** | 0 |
| `w640 / per-claim` | 2 | `0` | 1300.0 | 5.49 | 10.60 | 14.37 (10.27–18.46) | 14.46 | 10.60 | 4.00 | 640 | 0 | 14.60 | 117.45 | 39.67 | 5,862 | 0 |
| `w1280 / batch-default` | 2 | `0` | **661.2** | **9.76** | 19.02 | 19.19 (17.65–20.74) | 19.30 | 29.04 | 1.25 | 1 | **`0`** | 10.97 | **35.69** | **28.18** | 6,719 | **0** |
| `w1280 / batch-unbounded` | 2 | `0` | 756.3 | 9.82 | **18.58** | **18.80** (17.48–20.11) | **18.94** | 38.93 | 1.33 | 1 | **`0`** | 12.53 | 164.94 | 163.78 | **6,634** | 100 |
| `w1280 / fill-unbounded` | 2 | `0` | 8067.2 | 15.46 | 24.07 | 24.36 (23.92–24.79) | 24.45 | **27.11** | 1.50 | 1 | **`0`** | **9.94** | 110.75 | 95.36 | 6,923 | 0 |
| `w1280 / per-claim` | 2 | `0`\* | 14843.9 | 20.91 | 31.19 | 31.51 (23.33–39.69) | 31.61 | 29.50 | 4.00 | 1,280 | **`218`** | 105.66 | 247.27 | 153.06 | 10,610 | 0 |
| `c640 (cold) / batch-unbounded` | 2 | `0` | **3049.0** | **16.17** | **24.55** | **25.28** (25.20–25.36) | **25.36** | **25.01** | 1.25 | 1 | 0 | **6.21** | 165.06 | 127.04 | **4,765** | 0 |
| `c640 (cold) / per-claim` | 2 | `0` | 5639.4 | 16.92 | 25.39 | 27.04 (25.47–28.60) | 27.11 | 25.77 | 4.00 | 640 | 0 | 12.03 | **131.07** | **45.94** | 6,770 | 0 |

*\*`w1280 / per-claim` failed `14` environments with HTTP `429` errors on its first run attempt (archived in `bench-results/gke/superseded/` with `WHY.md`) before succeeding on retry.*

---

## Suite 4: Upstream `loadtest.py` End-to-End Integration Suite (`40 × 4 = 160` Tasks)

* **Directories:** [`bench-results/gke-scripts/loadtest/`](../gke-scripts/loadtest) (Default Controller, `10` runs) & [`bench-results/gke-scripts/loadtest-tuned/`](../gke-scripts/loadtest-tuned) (Production-Tuned Controller, `10` runs).
* **Command:** `python -m tests.loadtest --images 40 --tasks-per-image 4 --strategies naive --warm-per-task --in-cluster` at `--max-concurrent 40` (`mc40`) and `--max-concurrent 160` (`mc160`).
* *(Note: `loadtest.py` creates all 40 warm pools from scratch and tears them down in every run, so `Total Wall` is dominated by GKE cold pod scheduling + image streaming [`pool-ready`], whereas `Claim Avg / Max` measures `fleet.acquire()` time-to-sandbox.)*

| Controller Profile | Concurrency & Arm | Runs | Tasks OK | Claim Avg / Max (Run 1) | Claim Avg / Max (Run 2) | Total Wall s (Run 1 / Run 2) |
|---|---|---:|---:|---|---|---|
| **Default Controller** | `mc40 / per-claim` | 2 | `160/160` | `0.7s / 1s` | `0.8s / 1s` | `49.7s / 13.3s` |
| **Default Controller** | `mc40 / batch-unbounded` | 2 | `160/160` | `0.6s / 1s` | `0.6s / 1s` | `12.2s / 14.2s` |
| **Default Controller** | `mc160 / per-claim` | 2 | `160/160` | `1.0s / 2s` | `1.5s / 3s` | `8.1s / 62.6s` |
| **Default Controller** | `mc160 / batch-defaults` | 2 | `160/160` | `1.4s / 3s` | `1.4s / 3s` | `9.6s / 59.2s` |
| **Default Controller** | `mc160 / batch-unbounded` | 2 | `160/160` | `1.4s / 5s` | `1.6s / 3s` | `12.0s / 12.4s` |
| **Production-Tuned** | `mc40 / per-claim` | 2 | `160/160` | `0.8s / 2s` | `0.8s / 2s` | `55.9s / 67.4s` |
| **Production-Tuned** | `mc40 / batch-unbounded` | 2 | `160/160` | `0.7s / 2s` | `0.8s / 2s` | `22.6s / 25.2s` |
| **Production-Tuned** | `mc160 / per-claim` | 2 | `160/160` | `1.4s / 4s` | `1.6s / 3s` | `10.6s / 67.0s` |
| **Production-Tuned** | `mc160 / batch-defaults` | 2 | `160/160` | `1.2s / 2s` | `1.5s / 3s` | `21.1s / 22.1s` |
| **Production-Tuned** | `mc160 / batch-unbounded` | 2 | `160/160` | `1.5s / 3s` | `1.7s / 3s` | `17.7s / 69.0s` |

---

## Suite 5: Diagnostic Finding — Why `--sandbox-warm-pool-concurrent-workers=100` Hurts on Managed GKE

* **Directory:** [`bench-results/gke-tuned-wp100/`](../gke-tuned-wp100/WHY.md) (`9` runs before switching to `warm-pool-workers=2`).
* Before running Suite 1 (`warm-pool-workers=2` from `docs/performance-tuning.md`), we tested `--sandbox-warm-pool-concurrent-workers=100` (from [`examples/agent-sandbox-rl/README.md`](../../README.md#L896)). On managed GKE (where GKE's `flowcontrol-guardrails` webhook blocks custom high-share APF `PriorityLevelConfiguration`s), `100` warm-pool workers immediately bursting replacement `Sandbox` + `Pod` creates across 160 pools saturated `workload-low` (`165` seats):

| `w640` (`160 × 4`) Controller Setting | `batch-default` Last Ready | `batch-default` APF Queue Wait | `batch-unbounded` APF Queue Wait | `per-claim` APF Queue Wait |
|---|---:|---:|---:|---:|
| **Default Controller (`warm-pool-workers=1`)** | `9.39s` | `22.33s` | `21.30s` | `39.67s` |
| **Perf-Tuning Guide (`warm-pool-workers=2`, `rate=100`)** | **`8.73s`** | `497.75s` | `506.63s` | `698.95s` |
| **RL README (`warm-pool-workers=100`, `rate=100`)** | `12.30s` | **`1,043.31s` (`47×`)** | **`1,473.03s` (`69×`–`155×`)** | **`1,496.76s`** |
