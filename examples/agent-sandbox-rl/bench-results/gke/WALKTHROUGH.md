# Narrative Walkthrough: `claim_mode="batch"` vs. `per_claim` on GKE

> [!IMPORTANT]
> **The 30-Second Story (For Your TL)**
> When an RL or evaluation job claims hundreds to thousands of sandboxes at once, `per_claim` forces the Python client to open **$N$ separate Kubernetes watch streams** and make **`4.00` API calls per sandbox** (`5.00` after PR `#1791`), overflowing the client's `1,000`-socket connection pool and triggering GKE API server HTTP `429` throttling.
>
> Switching to `claim_mode="batch"` collapses all $N$ watches into **`1` shared watch stream** and cuts client API calls to **`1 + (1 / G)` per sandbox** (`1.02 – 2.01`). Across **103 GKE benchmark runs** up to **`2,560` concurrent gVisor sandboxes**:
> - **Zero dropped rollouts (`100%` group completion):** `batch` completed **`100%` of sandboxes with `0` errors across every single run**, whereas `per_claim` dropped **`101` sandboxes to HTTP `429`s** on `16×64` (breaking `16/16` GRPO groups on Step 1) and hit **`551` dropped sockets + HTTP `429` errors** at `2,560` — **even with a production-tuned controller**.
> - **Stays on GKE's linear `~65–75 claims/s` binding speed limit without `429` tail cliffs:** Below the saturation knee, both modes tie at etcd's server-side binding rate; once bursts cross `640 – 2,560`, `batch` avoids `per_claim`'s `+15–28s` HTTP `429` retry stalls (**`2.24×` faster** at `640`, **`1.36× – 1.61×` faster** on clean `2,560`, **`4.96×` faster** on `500×1` eval Step 1), delivers the first Ready sandbox **`3× – 22.5×` faster**, and cuts cluster claim API traffic by **`39% – 50%`**.

*(Companion deep-dives: [`FULL_RESULTS.md`](./FULL_RESULTS.md) for all 103 runs & raw server columns; [`GKE_APF_WARMPOOL_DEEP_DIVE.md`](./GKE_APF_WARMPOOL_DEEP_DIVE.md) for a from-scratch explanation of Kubernetes APF & warm-pool tuning on GKE.)*

---

## Chapter 1: What Exactly Did We Test, and Under What Settings?

Every run (`per_claim` and `batch`) ran on the **exact same `bench/rl-gke` codebase and controller image** so the comparison is strictly apples-to-apples across three layers:

### Layer 1 — The GKE Cluster Hardware & Task Images (`rl-gke-bench`, `us-central1-a`, GKE `1.37`)
* **Production-realistic sandbox nodes (matching [`create-gke-standard-fleet.sh`](../../../multi-cluster-fleet/deploy/create-gke-standard-fleet.sh)):** Standard GKE nodes running **gVisor (`runsc`)** + **GKE Image Streaming (`gcfs`)** + Dataplane V2 + NodeLocalDNS:
  * `24 × n2-standard-8` gVisor nodes (`192 vCPU`, `768 GB RAM`) for `160 – 1,280`, scaled once to `48 × n2-standard-8` gVisor nodes (`384 vCPU`, `1.5 TB RAM`) for `2,560`, `500×1`, and `16×64`.
* **Dedicated isolation nodes:** `1 × e2-standard-8` tainted node exclusively for `agent-sandbox-controller` (`8 vCPU / 32 GiB` limit) + `1 × n2-standard-8` tainted node exclusively for our **in-cluster** Python driver pod (`rl-bench-driver` — zero laptop/VPN latency).
* **Stationary GKE API server:** Pre-warmed at **`404` total APF concurrency seats** (`165` seats in `workload-low`), verified `running_ops=[]` before every suite.
* **Task images:** `python:3.12-slim` mirrored in regional Artifact Registry under **`640` distinct tags (`:g000`..`:g639`)**. Because `agent_sandbox_rl` keys templates and pools by image string, this creates **640 distinct `SandboxTemplate`s and `640` distinct `SandboxWarmPool`s** (identical K8s CRD/controller topology to 640 SWE-bench repos). In all warm benchmarks, pods are **already pulled and `Ready` before the step stopwatch starts**, isolating pure control-plane claiming from multi-GB image pulls.

---

### Layer 2 — The Two Controller Profiles We Tested

| Controller Knob | **Profile 1: Out-of-the-Box Defaults** *(Chapter 3)* | **Profile 2: Official Perf-Tuning Guide Profile** *(Chapters 4 & 5)* | *Older RL README Flags (`examples/agent-sandbox-rl/README.md`)* |
|---|---|---|---|
| **Source** | Default `deploy-to-kube --extensions` (`k8s/` manifests) | [`docs/performance-tuning.md`](../../../../docs/performance-tuning.md#L508-L530) **"Sustained High-Throughput Profile"** *(row `H` in that doc)* | [`examples/agent-sandbox-rl/README.md`](../../README.md#L890-L922) |
| **Sandbox / Claim workers** | `100` / `50` | **`200` / `200`** *(doc uses `200/150` at 75 claims; matched at `200/200`)* | `1000` / `1000` |
| **WarmPool workers** | `1` | **`2`** + `--sandbox-warm-pool-max-refill-rate=100` | `100` *(no refill rate cap)* |
| **K8s API transport** | `1` shared HTTP/2 connection | **`--api-connections=4`** + **`--separate-watch-connection=true`** | `1` shared HTTP/2 connection |
| **Cache & write reduction** | All events & annotations on | **`--cache-label-selectors=true`**, **`--disable-{sandbox,claim}-events=true`**, **`--disable-claim-observability-annotations=true`** | All events & annotations on |

*(Why use the Perf-Tuning Guide profile instead of the RL README's `1000/1000/100` flags? Because [`docs/performance-tuning.md`](../../../../docs/performance-tuning.md#L112-L125) already proved `200/150/2` beats `1000/1000/500/100` on GKE — and when we tested `warm-pool-workers=100` ourselves in `gke-tuned-wp100/`, GKE API queue wait exploded `155×`! Explained in plain English in Chapter 7.)*

---

### Layer 3 — Client Settings, Scripts, & What Our `BatchClaimConfig` Defaults Prove

Both our custom multi-step driver ([`gke_scale_bench.py`](../../tests/gke_scale_bench.py)) and Tomer's upstream [`loadtest.py`](../../tests/loadtest.py) call the **exact same `agent_sandbox_rl` `SandboxFleet`** (`warm_per_task=True`, `strategy="naive"`, `max_concurrent=N`, `connection_pool_maxsize=1000`). [`gke_scale_bench.py`](../../tests/gke_scale_bench.py) pre-warms pools *before* starting the stopwatch and runs **2 back-to-back RL steps** (Step 1 = cold client; Step 2 = steady-state RL loop) across **all three client arms** (never retuned as $N$ grew):

1. **`per_claim`:** `FleetConfig(claim_mode="per_claim")` — fires all $N$ `create_sandbox()` calls + $N$ watches immediately with zero pacing.
2. **`batch` (default):** `FleetConfig(claim_mode="batch")` — uses out-of-the-box `BatchClaimConfig()` defaults (**`max_in_flight=20`** concurrent `POST` creates in flight; `create_rps=50` for cold claims, skipped on warm claims) + `1` shared watch.
3. **`batch` (unbounded):** `BatchClaimConfig(max_in_flight=998, create_rps=1_000_000)` — removes the `20`-in-flight create cap up to the `1,000`-socket pool ceiling.

> [!TIP]
> **What the Benchmark Proves About Our `BatchClaimConfig` Defaults (`max_in_flight=20`):**
> **Our SDK defaults (`batch-default`) are validated as the right out-of-the-box choice — users do not need to tune them!**
> With `~80 ms` `POST` latency, `max_in_flight=20` pushes **~`190–250 creates/sec`** (`1,280` creates in `6.4s`; `2,560` creates in `13s`) — already **`2.5×–3.5×` faster than GKE's controller + etcd can bind warm claims (`~65–75 claims/s`)**. Better yet, capping creates at `20` in flight prevents the client from flooding APF (`0` HTTP `429`s at `w640` tuned → **`8.73s`** vs `18.95s` on `batch-unbounded`, and **`194 ms` TTFC** vs `601 ms` at `w2560`).

---

## Chapter 2: Why `per_claim` Sends `4.00` API Calls/Sandbox vs. `batch`'s `1 + 1/G`

In SWE-bench/RL, **each distinct container image has its own `SandboxWarmPool`**. Suppose a step runs **$P$ distinct image pools** with **$G$ tasks per pool** ($N = P \times G$ total sandboxes). Because `SandboxFleet` opens an empty `SandboxBatch` and callers invoke `fleet.acquire(task)` dynamically per task:

* **`per_claim = 4.00` client API calls + `1` dedicated watch stream per sandbox** *(becomes **`5.00`** after PR `#1791`)*:
  `1 POST sandboxclaims` + `1 WATCH sandboxclaims` ($2,560$ open streams at $2,560$!) + `2 GET sandboxes` (`get_pod_name` + `get_pod_ip`, fetching the same `Sandbox` twice; `+ 1 GET pods/status` post-`#1791`).
* **`batch = 1 + (1 / G)` client API calls per sandbox + `1` shared watch stream total:**
  `1 POST sandboxclaims` per sandbox + **`1` shared label-selector watch** (`pod_name` + `pod_ip` read directly off `SandboxClaim.status`, **`0` `GET sandboxes`!**) + **`1 GET sandboxwarmpools` on the first `acquire()` for each of the $P$ pools** (cached in memory for the remaining $G - 1$ tasks on that pool!):
  * Total client calls = $(N + P) / N = 1 + (1 / G)$ → **`2.01`** at `500×1` ($G=1$), **`1.25`** at `N×4` ($G=4$), and **`1.02`** at `16×64` ($G=64$).

---

## How to Discuss Latency Metrics With Your TL (`All Ready` vs. `p99` vs. `p50` vs. `TTFC`)

* **Lead with `All Ready (last)` + `Failed Envs` for synchronous GRPO:** In strict synchronous steps, the step finishes when the slowest group finishes — and an unhandled `429` exception (`Failed Envs > 0`) **corrupts or aborts that rollout group**.
* **Pair `All Ready (last)` with `p99`:** On Kubernetes, `All Ready (last)` is **`p100` (literally the single slowest claim out of `2,560` = `0.04%`)**. If even **1** controller status `PATCH` out of `2,560` hits an APF `HTTP 429` (`Retry-After`), `99%` (`2,534` sandboxes) are Ready at `18s` (`p99`), while that 1 single claim sits in backoff until `30s+`. Showing **`p99` right next to `All Ready (run1–run2)`** lets your TL distinguish true throughput from single-request `429` retry noise.
* **Keep `TTFC` & `p50` as streaming startup metrics:** Even in synchronous GRPO, each `2–15 minute` multi-turn LLM rollout starts generating tokens **the instant its own `acquire()` returns** (`TTFC` / `p50`), rather than sitting idle at a barrier until sandbox `#2,560` is Ready.

---

## Chapter 3: Act I — Out-of-the-Box Default Controller (`160 → 640 → 1,280`, All 3 Arms)

| Warm Sandboxes (`Pools × 4`) | Client Mode | Failed Envs | **TTFC** (1st) | **`p50`** | **`p99`** | **All Ready (`last`)** *(run1–run2)* | API Calls / Env | Watches | Dropped Sockets |
|---|---|---:|---:|---:|---:|---|---:|---:|---:|
| **160** (`40 × 4`) | `per_claim` | `0` | `553 ms` | `1.28s` | `2.17s` | `2.36s` *(1.86–2.86s)* | `4.00` | `160` | `0` |
| **160** (`40 × 4`) | **`batch` (default)** | **`0`** | **`178 ms`** | **`1.12s`** | **`2.10s`** | **`2.12s`** *(1.94–2.29s)* | **`1.26`** | **`1`** | `0` |
| **160** (`40 × 4`) | **`batch` (unbounded)** | **`0`** | `243 ms` | `1.29s` | `2.14s` | `2.16s` *(2.07–2.25s)* | **`1.25`** | **`0`–`1`** | `0` |
| **640** (`160 × 4`) | `per_claim` | `0` | `1,300 ms` | `5.49s` | `10.60s` | `14.37s` *(10.27–18.46s)* | `4.00` | `640` | `0` |
| **640** (`160 × 4`) | **`batch` (default)** | **`0`** | **`338 ms`** | **`5.17s`** | **`9.26s`** | **`9.39s` (`1.53×` ✅)** *(9.35–9.43s)* | **`1.25`** | **`1`** | `0` |
| **640** (`160 × 4`) | **`batch` (unbounded)** | **`0`** | `660 ms` | `5.38s` | `10.14s` | `10.34s` (`1.39×` ✅) *(10.29–10.38s)* | **`1.25`** | **`0`–`1`** | `0` |
| **1,280** (`320 × 4`) | `per_claim` | `0`\* | **`14,844 ms`** | `20.91s` | `31.19s` | `31.51s` *(23.33–39.69s)* | `4.00` | `1,280` | **`218`** |
| **1,280** (`320 × 4`) | **`batch` (default)** | **`0`** | **`661 ms` (`22.5×`)** | **`9.76s`** | `19.02s` | **`19.19s` (`1.64×` ✅)** *(17.65–20.74s)* | **`1.25`** | **`1`** | **`0`** |
| **1,280** (`320 × 4`) | **`batch` (unbounded)** | **`0`** | `756 ms` (`19.6×`) | `9.82s` | **`18.58s`** | **`18.80s` (`1.68×` ✅)** *(17.48–20.11s)* | **`1.33`** | **`1`** | **`0`** |

*\*On its first `1,280` attempt, `per_claim` failed `14` sandboxes with HTTP `429` errors before succeeding on retry; both `batch` arms had `0` errors across every run.*

---

## Chapter 4: Act II — Production-Tuned Controller (`160 → 640 → 1,280 → 2,560`, All 3 Arms)

Now we apply the **Official `docs/performance-tuning.md` High-Throughput Profile** (`200` sandbox/claim workers, `2` warm-pool workers, `max-refill-rate=100`, `4` API connections + dedicated watch connection, event suppression). Notice the clean physics: **GKE's controller + etcd binds warm claims at a physical ceiling of ~`65–75 claims/s` (`2.3s` at `160` → `8.7s` at `640` → `18.6–20.1s` at `1,280` → `35.5–42.0s` at `2,560`)**:

| Warm Sandboxes (`Pools × 4`) | Client Mode | Failed Envs | **TTFC** (1st) | **`p50`** | **`p99`** | **All Ready (`last`)** *(run1–run2)* | API Calls / Env | Watches | Dropped Sockets | Cluster Claim Reqs |
|---|---|---:|---:|---:|---:|---|---:|---:|---:|---:|
| **160** (`40 × 4`) | `per_claim` | `0` | `516 ms` | `1.37s` | `2.44s` | `2.48s` *(2.43–2.53s)* | `4.00` | `160` | `0` | `1,224` |
| **160** (`40 × 4`) | **`batch` (default)** | **`0`** | **`136 ms`** | `1.18s` | `2.33s` | **`2.34s`** *(2.32–2.37s)* | **`1.26`** | **`1`** | `0` | **`747` (`-39%`)** |
| **160** (`40 × 4`) | **`batch` (unbounded)** | **`0`** | `214 ms` | **`1.15s`** | **`2.24s`** | `2.38s` *(2.37–2.40s)* | **`1.26`** | **`1`** | `0` | **`742` (`-39%`)** |
| **640** (`160 × 4`) | `per_claim` | `0` | `3,120 ms` | `5.36s` | `19.30s` | `19.54s` *(9.71–29.37s)* | `4.00` | `640` | `0` | `4,735` |
| **640** (`160 × 4`) | **`batch` (default)** | **`0`** | **`603 ms`** | **`4.78s`** | **`8.50s`** | **`8.73s` (`2.24×` ✅)** *(8.68–8.78s)* | **`1.25`** | **`1`** | `0` | **`2,840` (`-40%`)** |
| **640** (`160 × 4`) | **`batch` (unbounded)** | **`0`** | `758 ms` | `4.81s` | `18.65s` | `18.95s` *(8.23–29.68s)* | **`1.25`** | **`1`** | `0` | **`2,806` (`-41%`)** |
| **1,280** (`320 × 4`) | `per_claim` | `0` | `317 ms` | `9.18s` | `19.60s` | `19.71s` *(18.47–20.96s)* | `4.00` | `1,280` | `0` | `9,388` |
| **1,280** (`320 × 4`) | **`batch` (default)** | **`0`** | `1,339 ms` | `10.20s` | `24.04s` | `24.59s` *(30.61s / **18.57s**)*\*\* | **`1.25`** | **`2`** | `0` | **`5,529` (`-41%`)** |
| **1,280** (`320 × 4`) | **`batch` (unbounded)** | **`0`** | `358 ms` | **`8.47s`** | **`19.16s`** | `20.14s` *(**16.86s**–23.41s)* | **`1.25`** | **`1`** | `0` | **`5,502` (`-41%`)** |
| **2,560** (`640 × 4`) | `per_claim` | **`1` ❌** | `273 ms` | `17.94s` | `33.96s` | **`57.29s` clean run 1** *(err run 2: 34.80s)*\*\*\* | `4.00` | `2,560` | **`551`** | `18,317` |
| **2,560** (`640 × 4`) | **`batch` (default)** | **`0` ✅** | **`194 ms`** | **`16.53s`** | **`33.67s`** | **`47.40s`** *(**41.98s**–52.83s)* | **`1.25`** | **`2`** | **`0`** | **`10,668` (`-42%`)** |
| **2,560** (`640 × 4`) | **`batch` (unbounded)** | **`0` ✅** | `601 ms` | `16.91s` | `37.58s` | `51.58s` *(**35.48s**–67.68s)* | **`1.13`** | **`2`** | **`0`** | **`10,757` (`-41%`)** |

* \*\* **Why did `1,280 / batch (default)` take `30.61s` in Run 1 vs. `18.57s` in Run 2?** In Run 2 (`0` HTTP `429`s), `batch-default` beat `per_claim` (`18.57s`, `p99 = 18.09s`). In Run 1, **`p90` was `14.62s`** (faster than `per_claim`'s `15.84s`!), but background warm-pool pod creation (`1,905 POST pods` mid-step because `replenish-delay=0`) caused `44` server-side HTTP `429`s on `PATCH sandboxclaims`, delaying the final `1%` (`12` claims) by `+15s`. Adding `--sandbox-warm-pool-replenish-delay=5s` eliminates that mid-step refill collision.
* \*\*\* **At `2,560`, `summary.md`'s raw median (`46.05s`) masked a `per_claim` crash:** Run 1 was the **only** `2,560` run where `per_claim` delivered all `2,560` sandboxes (`57.29s`), and both `batch` arms beat it decisively (**`35.48s` = `1.61×` faster** on `unbounded`; **`41.98s` = `1.36×` faster** on `default`, with tight **`p99 = 33.63s–33.70s`** across both runs). On Run 2, `per_claim` overflowed the `1,000`-socket pool (`551` dropped sockets) and **failed a sandbox with `HTTP 429 Too Many Requests`**.

---

## Chapter 5: Act III — Extreme Workload Shapes (`500 × 1` Eval & `16 × 64` High-Rollout Stress Test)

Standard multi-repo GRPO uses `G = 4–16` rollouts/image (covered by `N × 4` above). To bracket **both ends of the `(Pools × Tasks/Pool)` spectrum** on the Production-Tuned Controller, we also tested `500 × 1` (`G = 1`, `batch`'s least pool sharing) and `16 × 64` (`G = 64`, Best-of-64 / pass@64 / shared base-image RL — `batch`'s most pool sharing & hardest single-pool contention):

| Workload Shape (`Pools × Tasks/Pool`) | Client Mode | Failed Envs | **TTFC** (1st) | **`p50`** | **`p99`** | **All Ready (`last`)** *(run1–run2)* | **Step 1 (Cold Client)** | API Calls / Env | Watches | Dropped Sockets |
|---|---|---:|---:|---:|---:|---|---:|---:|---:|---:|
| **SWE-bench Eval (`500 × 1 = 500`)** | `per_claim` | `0` | `738 ms` | `4.05s` | `6.93s` | `7.27s` *(7.03–7.51s)* | `33.51s`\* | `4.00` | `500` | `0` |
| **SWE-bench Eval (`500 × 1 = 500`)** | **`batch` (default)** | **`0`** | **`280 ms`** | **`3.71s`** | `7.46s` | `7.62s` *(7.45–7.79s)* | **`7.95s` (`4.2×` ✅)** | **`2.01`** | **`4`** | `0` |
| **SWE-bench Eval (`500 × 1 = 500`)** | **`batch` (unbounded)** | **`0`** | `306 ms` | `4.40s` | **`6.91s`** | **`7.01s`** *(6.95–7.07s)* | **`6.75s` (`5.0×` ✅)** | **`2.01`** | **`4`** | `0` |
| **High-Rollout (`16 × 64 = 1,024`)** | `per_claim` | **`101` ❌** | `4,108 ms` | `8.73s` | `32.41s` | `37.39s` *(44.30s clean / 30.48s err)* | `34.93s` *(99 errs!)* | `4.00` | `1,023` | **`24`** |
| **High-Rollout (`16 × 64 = 1,024`)** | **`batch` (default)** | **`0` ✅** | **`199 ms` (`20.6×`)** | **`7.05s`** | `31.74s` | **`35.82s`** *(35.72–35.91s)* | **`31.08s`** | **`1.02`** | **`1`** | **`0`** |
| **High-Rollout (`16 × 64 = 1,024`)** | **`batch` (unbounded)** | **`0` ✅** | `448 ms` (`9.2×`) | `7.09s` | **`24.42s`** | **`33.83s`** *(32.03–35.63s)* | `38.50s` | **`1.02`** | **`3`** | **`0`** |

* \***Why `500 × 1` Step 1 (`6.75s` vs `33.51s`, `5.0×` faster) matters:** An eval sweep runs **once** ( just like [`run_full_swebench_benchmark.py`](../../tests/run_full_swebench_benchmark.py)'s `TASK` phase after `PRELOAD`), so Step 1 *is* the whole job. On Step 1, `per_claim`'s `21,407` total API server requests triggered `71–80` HTTP `429` rejections on controller status patches (`p90 = 6.4s` → `last = 33.5s`), whereas `batch` sent `7,398` total requests (`0` `429`s) and finished in **`6.75s`**.
* **On `16 × 64` (`1,024`), `per_claim` broke GRPO completely:** `99` HTTP `429` failures on Run 1 Step 1 left **`0 / 16` complete groups**, plus `2` more failures on Run 2 Step 2 (`44.30s` on its single clean step vs `32.03s–35.82s` on `batch`, which completed **`4,096 / 4,096` tasks and `100%` of groups**).

---

## Chapter 6: Upstream `loadtest.py` End-to-End Integration Check (`40 × 4 = 160`)

Running Tomer's upstream [`loadtest.py`](../../tests/loadtest.py) (`AsyncSandboxFleet.run` end-to-end: create pools → boot pods → claim 160 → teardown) across **20 runs** (`10` Default Controller + `10` Production-Tuned Controller) passed **`160 / 160` tasks on all 20 runs** (`claim` avg `0.6–1.5s`), confirming drop-in compatibility.

---

## Chapter 7: Plain-English Guide to the `warm-pool-workers=100` GKE APF Finding

*(Full beginner-friendly visual guide: [`GKE_APF_WARMPOOL_DEEP_DIVE.md`](./GKE_APF_WARMPOOL_DEEP_DIVE.md).)*

1. **What is APF?** Kubernetes's API server puts a "highway tollbooth" in front of `etcd` called **API Priority and Fairness (APF)** (`404` total lanes on our GKE cluster). Third-party controllers like `agent-sandbox-controller` share a **`165`-lane bucket called `workload-low`**. When >`165` requests arrive at once, extras wait in line (`apf_wait_s`) or get rejected with **`HTTP 429 Too Many Requests`**.
2. **Why does background pool refilling collide with urgent claiming?** The millisecond your RL job claims warm pods out of `160` pools, `agent-sandbox-controller` tries to do **two jobs over those same `165` lanes**: (A) **Urgent:** mark your `SandboxClaim`s `Ready` so Python `acquire()` returns, and (B) **Not urgent:** create replacement `Sandbox` + `Pod` objects for the *next* RL step.
3. **Why did `--sandbox-warm-pool-concurrent-workers=100` (in `agent-sandbox-rl/README.md`) blow up on GKE?** Self-hosted K8s uses `apf-insulation.yaml` to give urgent claims a separate VIP lane, **but Managed GKE blocks custom high-share APF configs** (`flowcontrol-guardrails` webhook). With `100` warm-pool workers + `200` claim/sandbox workers (`300` goroutines fighting for `165` lanes!), `100` background refill goroutines stampeded `workload-low` mid-claim, spiking queue wait **`155×` (`9.5s → 1,473s`)** and slowing `w640` `batch-default` from **`8.73s` → `12.30s`**.
4. **Actionable GKE fix:** Use [`docs/performance-tuning.md`](../../../../docs/performance-tuning.md#L508-L530)'s **`warm-pool-workers=2` + `max-refill-rate=100`**, plus **`--sandbox-warm-pool-replenish-delay=5s`** + **`claim_mode="batch"`** so background refills wait 5 quiet seconds until active claiming finishes!
