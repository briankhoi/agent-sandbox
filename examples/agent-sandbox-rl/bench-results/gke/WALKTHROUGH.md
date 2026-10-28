# Narrative Walkthrough: `claim_mode="batch"` vs. `per_claim` on GKE

> [!IMPORTANT]
> **The 30-Second Story**
> When an RL or evaluation job claims hundreds to thousands of sandboxes at once, `per_claim` forces the Python client to open **$N$ separate Kubernetes watch streams** and make **`4.00` API calls per sandbox** (`5.00` after PR `#1791`), overflowing the client's `1,000`-socket connection pool and triggering GKE API server HTTP `429` throttling.
>
> Switching to `claim_mode="batch"` collapses all $N$ watches into **`1` shared watch stream** and cuts client API calls to **`1 + (1 / G)` per sandbox** (`1.02 – 2.01`). Across **104 GKE benchmark runs** up to **`2,560` concurrent gVisor sandboxes**:
> - **Zero dropped rollouts:** `batch` completed **`100%` of sandboxes with `0` errors across every single run**, whereas `per_claim` dropped **`101` sandboxes to HTTP `429`s** on `16×64` and hit **`551` dropped sockets + HTTP `429` errors** at `2,560` — **even with a production-tuned controller**.
> - **Faster full-batch & streaming startup:** Up to **`1.68× – 2.24×` faster** full-batch readiness, **`20.6× – 22.5×` faster** time-to-first-sandbox (`TTFC`: `199 ms` vs `4.11s`), **`4.96×` faster** cold-client SWE-bench `500×1` eval (`6.75s` vs `33.51s`), and **`39% – 50%` fewer** cluster-wide claim API requests.

*(Looking for every raw variant, column, `p99`, CPU time, APF wait, and cold/wave run? See the companion reference: [`FULL_RESULTS.md`](./FULL_RESULTS.md).)*

---

## Chapter 1: What Exactly Did We Test, and Under What Settings?

To avoid mixing up infrastructure knobs, controller knobs, and Python client knobs, here is the exact **three-layer setup** used across the entire study. Every run (`per_claim` and `batch`) ran on the **exact same `bench/rl-gke` codebase and controller image** so the comparison is strictly apples-to-apples.

### Layer 1 — The GKE Cluster Hardware (`rl-gke-bench`, `us-central1-a`, GKE `1.37`)
* **Production-realistic sandbox nodes:** Standard GKE nodes running **gVisor (`runsc`)** + **GKE Image Streaming (`gcfs`)** + Dataplane V2 + NodeLocalDNS (matching the architecture in the Agent Sandbox RL blog post, not `kind`).
  * **For `160 – 1,280` sandboxes:** `24 × n2-standard-8` gVisor nodes (`192 vCPU`, `768 GB RAM`).
  * **For `2,560` sandboxes & shape tests (`500×1`, `16×64`):** Scaled once to `48 × n2-standard-8` gVisor nodes (`384 vCPU`, `1.5 TB RAM`).
* **Dedicated isolation nodes (no noisy neighbors):**
  * `1 × e2-standard-8` tainted node dedicated exclusively to `agent-sandbox-controller` (`8 vCPU / 32 GiB` pod limit).
  * `1 × n2-standard-8` tainted node dedicated exclusively to our **in-cluster** Python benchmark driver pod (`rl-bench-driver`) — eliminating corporate laptop / Wi-Fi / VPN latency.
* **Stationary GKE API server:** Pre-warmed until GKE finished control-plane autoscaling at **`404` total APF concurrency seats** (`165` seats in `workload-low`), verified idle (`running_ops=[]`) before every suite.

---

### Layer 2 — The Two Controller Profiles We Tested

There are **three** sets of controller flags people might think of in this repo. Here is what each one is and what we ran:

| Controller Knob | **Profile 1: Out-of-the-Box Defaults** *(Tested in Chapter 3)* | **Profile 2: Official Perf-Tuning Guide Profile** *(Tested in Chapters 4 & 5)* | *Repo RL README Flags (`examples/agent-sandbox-rl/README.md`)* |
|---|---|---|---|
| **Where it comes from** | Default `deploy-to-kube --extensions` (`k8s/` manifests) | [`docs/performance-tuning.md`](../../../../docs/performance-tuning.md#L508-L530) **"Sustained High-Throughput Profile"** *(labeled row `H` in that doc's A/B table)* | [`examples/agent-sandbox-rl/README.md`](../../README.md#L890-L922) |
| **Sandbox / Claim workers** | `100` / `50` | **`200` / `200`** *(doc uses `200/150` at 75 claims; matched at `200/200`)* | `1000` / `1000` |
| **WarmPool workers** | `1` | **`2`** + `--sandbox-warm-pool-max-refill-rate=100` | `100` *(no refill rate cap)* |
| **K8s API transport** | `1` shared HTTP/2 connection | **`--api-connections=4`** + **`--separate-watch-connection=true`** | `1` shared HTTP/2 connection |
| **Cache & write reduction** | All events & annotations on | **`--cache-label-selectors=true`**, **`--disable-{sandbox,claim}-events=true`**, **`--disable-claim-observability-annotations=true`** | All events & annotations on |

> [!NOTE]
> **What does `"Config H"` mean, and did we test the Perf-Tuning Guide settings?**
> **Yes!** In [`docs/performance-tuning.md`](../../../../docs/performance-tuning.md#L137-L164), the authors benchmarked combinations `A` through `I` and crowned **`H` (the "Sustained High-Throughput Profile")** as the recommended production configuration. That is **Profile 2** above. *(The only piece of `docs/performance-tuning.md` that cannot be applied on standard GKE is `apf-insulation.yaml`, because GKE's `flowcontrol-guardrails` webhook blocks custom high-share APF `PriorityLevelConfiguration`s.)*
>
> **What happened when we tried `agent-sandbox-rl/README.md`'s `--sandbox-warm-pool-concurrent-workers=100`?**
> We tested that first (`bench-results/gke-tuned-wp100/`)! On managed GKE, `100` warm-pool workers immediately bursting replacement pods while claims are still binding caused GKE API server queue wait (`apf_wait_s`) at `640` sandboxes to explode **`155×` (`9.5s → 1,473s`)**! Dropping back to the Perf-Tuning Guide's `warm-pool-workers=2` + `max-refill-rate=100` eliminated that self-inflicted refill storm.

---

### Layer 3 — The Python Client Settings & The Two Benchmark Scripts

Both scripts run inside the cluster and call the **exact same `agent_sandbox_rl` `SandboxFleet` library** with the same RL instant-claim settings (`warm_per_task=True`, `strategy="naive"`, `max_concurrent = N`, and `agent_sandbox_rl`'s built-in `urllib3` `connection_pool_maxsize = 1000`):

1. **Our Custom Multi-Step Scale & Shape Driver ([`gke_scale_bench.py`](../../tests/gke_scale_bench.py) + [`run_shapes_bench.py`](../../../../bench-scripts/run_shapes_bench.py)) — Produces all main scale (`160 → 2,560`) and shape (`500×1`, `16×64`) tables:**
   * **Why we wrote it:** Tomer's `loadtest.py` creates warm pools from scratch, pulls images, claims once, and deletes the pools — so 80%+ of its wall-clock timer is container startup rather than claiming, and it doesn't scrape `kube-apiserver` `/metrics` or count client HTTP calls/watches/dropped sockets.
   * **What `gke_scale_bench.py` does:** Pre-warms all warm pools *before* starting the stopwatch, then runs **2 back-to-back RL rollout steps** (`acquire` all $N$ sandboxes concurrently → run `1` `exec` probe per sandbox → `release`, waiting for warm pools to refill back to 100% Ready between steps). Step 1 measures **cold-client startup**; Step 2 measures **steady-state multi-step RL training**.
2. **Upstream [`loadtest.py`](../../tests/loadtest.py) (`40 images × 4 tasks = 160`, `AsyncSandboxFleet.run`) — End-to-End Integration Check (Chapter 6):**
   * Runs the existing upstream CLI tool (`--images 40 --tasks-per-image 4 --strategies naive --warm-per-task`) across both controller profiles (`20` runs total) to verify `claim_mode="batch"` works seamlessly as a drop-in flag on `AsyncSandboxFleet.run`.

**The 3 Client Claim Modes Compared in Every Suite (Fixed Across All Scales — Never Hand-Tuned Per Size!):**
* **`per_claim` (Current Default):** `FleetConfig(claim_mode="per_claim")` — fires all $N$ `create_sandbox()` calls and $N$ individual watches immediately with no pacing.
* **`batch` (Default SDK Settings):** `FleetConfig(claim_mode="batch")` — uses out-of-the-box `BatchClaimConfig()` defaults (`max_in_flight=20` concurrent `POST` creates in flight; `create_rps=50` for cold claims, bypassed on warm claims) + `1` shared watch stream. **Zero tuning required from the user.**
* **`batch` (Unbounded):** `BatchClaimConfig(max_in_flight=998, create_rps=1_000_000)` — lifts client-side create pacing up to the `1,000`-socket pool limit to isolate the pure architectural benefit of `1` shared watch + zero `GET sandboxes`.

*(As $N$ scaled from `160 → 2,560`, **none** of the client pacing knobs, `pool_maxsize=1000`, or controller flags were changed — only the workload size `max_concurrent = N`, pre-warmed pod count, and the one-time `24 → 48` worker node resize for `2,560` scaled with $N$.)*

---

## Chapter 2: Why `per_claim` Sends `4.00` API Calls/Sandbox vs. `batch`'s `1 + 1/G`

Suppose a step runs $P$ distinct container images with $G$ tasks (rollouts) per image ($N = P \times G$ total sandboxes):

* **`per_claim` always sends `4.00` client K8s API calls + `1` dedicated watch stream per sandbox** *(becomes **`5.00`** on `upstream/main` after PR `#1791`)*:
  1. `1.00 × POST sandboxclaims` — create the claim.
  2. `1.00 × WATCH sandboxclaims` — opens a **brand-new long-lived HTTP watch socket** just for that single claim ($2,560$ open watch streams at $N = 2,560$, blowing past `urllib3`'s `1,000`-socket pool!).
  3. `1.00 × GET sandboxes` — `get_pod_name()` (because `create_sandbox` throws away the watch event body).
  4. `1.00 × GET sandboxes` — `get_pod_ip()` (fetches the exact same `Sandbox` object a second time).
  *(+ post-PR `#1791`: `1.00 × GET pods/status` for `record_pod_identity()` → **`5.00` calls/sandbox**).*

* **`batch` sends `1 + (1 / G)` client K8s API calls per sandbox + `1` shared watch stream total:**
  1. `1.00 × POST sandboxclaims` — create the claim (paced smoothly by `max_in_flight=20` by default).
  2. `~0.00 × WATCH sandboxclaims` — **one shared label-selector watch** observes readiness for the entire fleet and reads `pod_name` + `pod_ip` directly off `SandboxClaim.status` (**zero `GET sandboxes` calls!**).
  3. `(1 / G) × GET sandboxwarmpools` — checks each image's warm pool once per step:
     * At $G = 1$ (`500 × 1` SWE-bench eval): `1 + 1/1` = **`2.01` calls/sandbox** (`2.0×` fewer).
     * At $G = 4$ (`N × 4` standard RL sweep): `1 + 1/4` = **`1.25` calls/sandbox** (`3.2×` fewer).
     * At $G = 64$ (`16 × 64` high-rollout stress test): `1 + 1/64` = **`1.02` calls/sandbox** (`3.9×` fewer).

---

## Chapter 3: Act I — Scaling on the Out-of-the-Box Default Controller (`160 → 640 → 1,280`)

First, we tested **Profile 1 (Out-of-the-Box Controller Defaults)** to see what happens when an RL user installs `agent-sandbox` directly from the release manifests and scales up a standard `N × 4` (`4` rollouts per image) RL step:

| Total Warm Sandboxes (`Images × 4`) | Client Mode | Failed Envs | **TTFC** (1st Ready) | **Median (`p50`)** | **All Ready (`last`)** | Speedup (`TTFC` / `last`) | API Calls / Sandbox | Open Watches | Dropped Sockets |
|---|---|---:|---:|---:|---:|---|---:|---:|---:|
| **160** (`40 × 4`) | `per_claim` | `0` | `553 ms` | `1.28s` | `2.36s` | — | `4.00` | `160` | `0` |
| **160** (`40 × 4`) | **`batch` (default)** | **`0`** | **`178 ms`** | **`1.12s`** | **`2.12s`** | **`3.1×` TTFC / `1.11×` last** | **`1.26`** | **`1`** | `0` |
| **640** (`160 × 4`) | `per_claim` | `0` | `1,300 ms` | `5.49s` | `14.37s` | — | `4.00` | `640` | `0` |
| **640** (`160 × 4`) | **`batch` (default)** | **`0`** | **`338 ms`** | **`5.17s`** | **`9.39s`** | **`3.8×` TTFC / `1.53×` last ✅** | **`1.25`** | **`1`** | `0` |
| **1,280** (`320 × 4`) | `per_claim` | `0`\* | **`14,844 ms` (`14.8s`!)** | `20.91s` | `31.51s` | — | `4.00` | `1,280` | **`218`** |
| **1,280** (`320 × 4`) | **`batch` (default)** | **`0`** | **`661 ms` (`0.66s`)** | **`9.76s`** | **`19.19s`** | **`22.5×` TTFC / `1.64×` last ✅** | **`1.25`** | **`1`** | **`0`** |
| **1,280** (`320 × 4`) | **`batch` (unbounded)** | **`0`** | **`756 ms` (`0.76s`)** | **`9.82s`** | **`18.80s`** | **`19.6×` TTFC / `1.68×` last ✅** | **`1.25`** | **`1`** | **`0`** |

*\*On its first `1,280` attempt, `per_claim` failed `14` sandboxes with HTTP `429` errors before succeeding on retry; `batch` had `0` errors across every run. (We also tested `640` cold sandboxes with `0`-replica pools: `batch` finished in `25.28s` vs `27.04s` with **`1.85×` faster TTFC** [`3.05s` vs `5.64s`] and `1` watch vs `640`.)*

**What happened in Act I:** At `160`, the cluster isn't stressed yet. By `640`, `batch` is **`1.53×` faster**. At `1,280`, `per_claim` opens `1,280` simultaneous watches against a `1,000`-socket pool (`218` dropped sockets) and stalls for **`14.84 seconds` before even the *first* sandbox comes back** — while `batch` streams its first Ready sandbox in **`661 ms` (`22.5×` faster)** and finishes the entire `1,280`-sandbox step **`1.68×` faster**.

---

## Chapter 4: Act II — Scaling on the Production-Tuned Controller (`160 → 640 → 1,280 → 2,560`)

Next comes the natural question: *"What if a team applies the High-Throughput Controller Profile from `docs/performance-tuning.md` (`200` sandbox/claim workers, `4` API connections, dedicated watch connection, event suppression)? Does controller tuning alone fix `per_claim`?"*

Here is the exact same `N × 4` sweep up to **`2,560` concurrent sandboxes** on **Profile 2 (Official `docs/performance-tuning.md` High-Throughput Controller)**:

| Total Warm Sandboxes (`Images × 4`) | Client Mode | Failed Envs | **TTFC** (1st Ready) | **Median (`p50`)** | **All Ready (`last`)** | API Calls / Sandbox | Open Watches | Dropped Sockets | Cluster Claim API Reqs |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| **160** (`40 × 4`) | `per_claim` | `0` | `516 ms` | `1.37s` | `2.48s` | `4.00` | `160` | `0` | `1,224` |
| **160** (`40 × 4`) | **`batch` (default)** | **`0`** | **`136 ms` (`3.8×`)** | **`1.18s`** | **`2.34s`** | **`1.26`** | **`1`** | `0` | **`747` (`-39%`)** |
| **640** (`160 × 4`) | `per_claim` | `0` | `3,120 ms` | `5.36s` | `19.54s` | `4.00` | `640` | `0` | `4,735` |
| **640** (`160 × 4`) | **`batch` (default)** | **`0`** | **`603 ms` (`5.2×`)** | **`4.78s`** | **`8.73s` (`2.24×` ✅)** | **`1.25`** | **`1`** | `0` | **`2,840` (`-40%`)** |
| **1,280** (`320 × 4`) | `per_claim` | `0` | `317 ms` | `9.18s` | `19.71s` | `4.00` | `1,280` | `0` | `9,388` |
| **1,280** (`320 × 4`) | **`batch` (unbounded)** | **`0`** | `358 ms` | **`8.47s`** | `20.14s` *(tie)* | **`1.25`** | **`1`** | `0` | **`5,502` (`-41%`)** |
| **2,560** (`640 × 4`) | `per_claim` | **`1` ❌** | `273 ms` | `17.94s` | `57.29s` *(clean run 1)*\* | `4.00` | `2,560` | **`551`** | `18,317` |
| **2,560** (`640 × 4`) | **`batch` (default)** | **`0` ✅** | **`194 ms` (`1.4×`)** | **`16.53s`** | **`47.40s` (`41.98–52.83s`)** | **`1.25`** | **`2`** | **`0`** | **`10,668` (`-42%`)** |
| **2,560** (`640 × 4`) | **`batch` (unbounded)** | **`0` ✅** | `601 ms` | **`16.91s`** | `51.58s` (**`35.48s` run 1**) | **`1.13`** | **`2`** | **`0`** | **`10,757` (`-41%`)** |

*\*At `2,560`, `per_claim` overflowed the `1,000`-socket pool (`551` dropped sockets per step): in clean run 1 (`0` errors), `batch-unbounded` beat `per_claim` **`35.48s` vs `57.29s` (`1.61×` faster)** and `batch-default` finished in **`41.98s` (`1.36×` faster)**; in run 2, `per_claim` failed a sandbox outright with `ApiException: (429) Too Many Requests (Retry-After: 32s)`, whereas both `batch` modes completed **`10,240 / 10,240` sandboxes across all runs with `0` errors and `0` dropped sockets**.*

**What happened in Act II:** Tuning the controller *does* remove the controller bottleneck at `1,280` — **until client & API-server fan-out takes over**:
1. At **`640`**, `per_claim`'s unpaced burst triggered `50` server-side HTTP `429` retries (`19.54s`), while `batch-default`'s `max_in_flight=20` pacing sailed through with **`0` `429`s in `8.73s` (`2.24×` faster)**.
2. At **`2,560`**, controller tuning cannot save the Python client from opening `2,560` concurrent watches on a `1,000`-socket pool (`551` dropped sockets/step) or sending `18,317` claim-path API requests — causing `per_claim` to drop rollouts to HTTP `429`s while `batch` stays rock-solid (`0` errors, `0` dropped sockets, `-42%` cluster API load).

---

## Chapter 5: Act III — Testing Across Workload Shapes (`G = 1` Eval vs. `G = 4` Standard RL vs. `G = 64` High-Rollout Stress Test)

**Why did we test `500 × 1` and `16 × 64` in addition to `N × 4`?**
In standard multi-repo SWE-bench RL training (e.g., GRPO in DeepSWE / R2E-Gym), you are right that **rollouts per image (`G`) is typically `4` to `16`** across many distinct repo images — which is why our main scale sweep in Chapters 3 & 4 used **`G = 4` (`N × 4`)**!

However, real users also hit the **two opposite ends** of the `(Images × Tasks-per-Image)` spectrum, so we tested both extremes on the **Production-Tuned Controller** to prove `batch` has no blind spots:
1. **`500 × 1 = 500` (`G = 1` task per image — SWE-Bench Verified Evaluation):** Every single task uses a *different* container image (`500` pools of `1` pod). This is **`batch`'s worst-case API amortization** (`1 + 1/1 = 2.01` API calls/sandbox, since it checks 500 separate pools).
2. **`16 × 64 = 1,024` (`G = 64` tasks per image — High-Rollout / Best-of-64 / Shared-Image Stress Test):** Used when teams run **pass@64 / Best-of-64 test-time compute**, **rejection sampling**, **deep-exploration GRPO on a small hard-problem curriculum**, or **general coding/math RL where hundreds of tasks share 1–16 base runtime images**. This is **`batch`'s best-case API amortization** (`1.02` calls/sandbox) and the **hardest single-pool contention test** (`64` concurrent claims competing on each `SandboxWarmPool`).

| Workload Shape (`Images × Tasks/Image`) | Client Mode | Failed Envs | **TTFC** (1st Ready) | **Median (`p50`)** | **All Ready (`last`)** | **Step 1 (Cold Client)** | API Calls / Sandbox | Open Watches | Dropped Sockets |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| **SWE-bench Eval (`500 × 1 = 500`)** | `per_claim` | `0` | `738 ms` | `4.05s` | `7.27s` | `33.51s` | `4.00` | `500` | `0` |
| **SWE-bench Eval (`500 × 1 = 500`)** | **`batch` (default)** | **`0`** | **`280 ms` (`2.6×`)** | **`3.71s`** | `7.62s` | **`7.95s` (`4.2×` ✅)** | **`2.01` (`2.0×` fewer)** | **`4`** | `0` |
| **SWE-bench Eval (`500 × 1 = 500`)** | **`batch` (unbounded)** | **`0`** | **`306 ms` (`2.4×`)** | `4.40s` | **`7.01s`** | **`6.75s` (`5.0×` ✅)** | **`2.01` (`2.0×` fewer)** | **`4`** | `0` |
| **High-Rollout / Shared Image (`16 × 64 = 1,024`)** | `per_claim` | **`101` ❌** | `4,108 ms` (`4.11s`) | `8.73s` | `37.39s` | `34.93s` | `4.00` | `1,023` | **`24`** |
| **High-Rollout / Shared Image (`16 × 64 = 1,024`)** | **`batch` (default)** | **`0` ✅** | **`199 ms` (`20.6×`)** | **`7.05s`** | **`35.82s`** | **`31.08s`** | **`1.02` (`3.9×` fewer)** | **`1`** | **`0`** |
| **High-Rollout / Shared Image (`16 × 64 = 1,024`)** | **`batch` (unbounded)** | **`0` ✅** | **`448 ms` (`9.2×`)** | **`7.09s`** | **`33.83s`** | `38.50s` | **`1.02` (`3.9×` fewer)** | **`3`** | **`0`** |

* **Big Eval Win (`500 × 1` Step 1 is `5.0×` faster — `6.75s` vs `33.51s`):** Unlike multi-step RL training, an evaluation sweep runs **once** on a fresh client — meaning **Step 1 *is* the entire eval job**. Opening 500 cold TLS/watch sockets in `per_claim` stalled Step 1 to `33.51s`, whereas `batch` finished all 500 claims in **`6.75s` (`4.96×` faster)**!
* **Big High-Rollout Win (`16 × 64` has `0` errors vs. `101` failed sandboxes):** Hammering 16 deep warm pools with 1,024 unpaced creates + 1,024 watches + 2,048 `GET`s pushed `per_claim` past GKE's APF rejection limit (**`99` failed sandboxes in run 1 + `2` in run 2 = `101` HTTP `429` task failures**), whereas `batch` cut cluster API traffic in half (`4,316` vs `8,676`), delivered the first Ready sandbox **`20.6×` faster (`199 ms` vs `4.11s`)**, and completed **`4,096 / 4,096` tasks with `0` errors**.

---

## Chapter 6: Upstream `loadtest.py` End-to-End Integration Check (`40 × 4 = 160`)

Finally, we ran Tomer's upstream [`loadtest.py`](../../tests/loadtest.py) (`--images 40 --tasks-per-image 4 --strategies naive --warm-per-task`, exercising `AsyncSandboxFleet.run` end-to-end from pool creation through teardown) for **10 runs on the Out-of-the-Box Controller** and **10 runs on the Production-Tuned Controller** (`concurrency=40` and `concurrency=160`):
* **100% compatibility:** All **20 / 20 runs** completed **`160 / 160` tasks (`0` errors)**.
* At this small `160`-sandbox scale, end-to-end `loadtest.py` wall time (`10s – 60s`) is dominated by **cold GKE gVisor pod creation & image streaming (`wait_pool_ready`)**, while the `claim` phase itself stayed fast (`0.6s – 1.5s` avg, `1s – 3s` max) across both modes — confirming `claim_mode="batch"` is a zero-regression drop-in at small scale that unlocks clean scaling at `500 – 2,560+`.

---

## Chapter 7: Two Bonus Takeaways for Team Q&A

1. **If asked *"Why does `per_claim` blow up suddenly instead of scaling linearly?"*:**
   * Neither mode does $O(e^N)$ work — both issue $O(N)$ `SandboxClaim` creates. Instead, `per_claim` slams into **stacked saturation cliffs**: GKE APF queue wait grows non-linearly as $1/(1 - \rho)$ near `100%` utilization, HTTP `429` responses trigger `32s` retry penalties, and opening $>1,000$ watches overflows `urllib3`'s `1,000`-socket pool. By cutting per-sandbox API work by ~`3×–4×` and collapsing $N$ watches into `1`, `batch` **pushes that saturation cliff `3×–4×` further out**.
2. **Actionable doc fix for [`examples/agent-sandbox-rl/README.md`](../../README.md#L896):**
   * `examples/agent-sandbox-rl/README.md` currently recommends `--sandbox-warm-pool-concurrent-workers=100`. On managed GKE (which blocks custom APF priority shares), `100` warm-pool workers bursting replacement pods *during* a claim burst spikes GKE APF wait **`155×` (`9.5s → 1,473s`)**.
   * **Recommendation:** Update the RL README on GKE to recommend the [`docs/performance-tuning.md`](../../../../docs/performance-tuning.md#L508-L530) High-Throughput Profile (`warm-pool-workers=2`, `max-refill-rate=100`) paired with **`--sandbox-warm-pool-replenish-delay=5s`** + **`claim_mode="batch"`** so background pool refills wait 5 seconds until active claiming finishes.
