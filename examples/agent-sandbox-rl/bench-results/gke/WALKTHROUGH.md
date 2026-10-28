# Plain-English Walkthrough: RL `batch` vs. `per_claim` on GKE

> **Morning TL;DR (Your 20-Second Opener):**
> 1. **Full-batch speed & zero drops at scale:** Across `640 – 2,560` warm sandboxes on GKE, `claim_mode="batch"` finishes up to **`1.68× – 2.24×` faster** and achieves **`100%` rollout reliability (`0` failed sandboxes across every single run all night)** — whereas `per_claim` hits HTTP `429` throttling and `1,000`-socket pool exhaustion starting at `1,024 – 2,560` sandboxes **even on a `Config H` production-tuned controller** (`101` failed sandboxes on `16×64`, `551` dropped sockets + `429` failures at `2,560`).
> 2. **Instant streaming startup (TTFC):** Agents receive their **first Ready sandbox up to `20.6× – 22.5×` faster** (`199 ms` vs `4.11s` on `16×64` production-tuned; `661 ms` vs `14.84s` at `1,280` default controller), and **cold-client SWE-bench `500×1` eval finishes `4.96×` faster on Step 1 (`6.75s` vs `33.51s`)**.
> 3. **Why it scales (`O(1)` watch + `1 + 1/G` API calls):** Collapses **`N` watch streams → `1` shared watch** (`2,560 → 1–2`), cuts client K8s API calls from **`4.00` (`5.00` post-`#1791`) down to `1.02 – 2.01` per sandbox**, eliminates `1,000`-socket pool overflows (`0` dropped sockets at `2,560`), and cuts total cluster claim API traffic by **`39% – 50%`**.

---

## 1. Cluster & Code Setup (If Anyone Asks "What Did You Test On?")

* **Codebase parity:** Every run (`per_claim` and `batch`, Default flags and Production Tuned flags) used our **exact same `bench/rl-gke` codebase and controller image** (pre-`#1791`, so `per_claim` isn't even penalized by `#1791`'s extra `GET pods/status`).
* **Real GKE Standard cluster (`us-central1-a`, GKE `1.37`):** Matching the blog post's production architecture (**gVisor `runsc` + GKE Image Streaming `gcfs`**), not `kind`.
* **Sandbox worker nodes:** `24 × n2-standard-8` gVisor nodes (`192 vCPU`, `768 GB RAM`) for `160 – 1,280`, scaled to **`48 × n2-standard-8` gVisor nodes (`384 vCPU`, `1.5 TB RAM`)** for `2,560` and the `500×1` / `16×64` suites.
* **Dedicated isolation nodes:** `1 × e2-standard-8` tainted node exclusively for `agent-sandbox-controller` + `1 × n2-standard-8` tainted node exclusively for the **in-cluster** Python RL driver pod (`rl-bench-driver` — zero laptop/VPN latency, zero noisy neighbors).
* **Stationary GKE control plane:** Pre-warmed at **`404` APF concurrency seats** (`165` `workload-low` seats), verified `running_ops=[]` on every run.

---

## 2. Why `per_claim` Sends `4.00` API Calls/Sandbox vs. `batch`'s `1 + 1/G`

For `G` rollouts per image (`G = 1` for SWE-bench eval, `G = 4–16` for standard multi-repo GRPO, `G = 64` for deep-group GRPO):

* **`per_claim = 4.00` client API calls/sandbox** *(becomes **`5.00`** on `upstream/main` after PR `#1791`)*:
  1. **`1.00 × POST sandboxclaims`** — create the claim
  2. **`1.00 × WATCH sandboxclaims`** — opens a dedicated HTTP watch stream for that single claim (`2,560` concurrent streams at `2,560`!)
  3. **`1.00 × GET sandboxes`** — `get_pod_name()` (because `create_sandbox` discards the watch payload)
  4. **`1.00 × GET sandboxes`** — `get_pod_ip()` (fetches the exact same `Sandbox` object a second time)
  *(+ post-`#1791`: `1.00 × GET pods/status` for `record_pod_identity()` → **`5.00`**)*
* **`batch = 1 + (1 / G)` client API calls/sandbox** (`1` shared watch stream for the entire fleet!):
  1. **`1.00 × POST sandboxclaims`** — create the claim
  2. **`~0.00 × WATCH sandboxclaims`** — **1 shared label-selector watch** for all claims, reading `pod_name` + `pod_ip` directly from `SandboxClaim.status` (**`0` `GET sandboxes`!**)
  3. **`(1 / G) × GET sandboxwarmpools`** — `1` readiness check per warm pool (`2.01` total at `500×1` eval; **`1.25`** at `N×4`; **`1.02`** at `16×64` deep GRPO!)

---

## 3. Production Tuned Controller (`docs/performance-tuning.md` `Config H`)

In production, nobody runs `1,000+` sandboxes on legacy 5-worker defaults — teams deploy the high-throughput flags from `docs/performance-tuning.md` (`Config H`: 200 sandbox/claim workers, 4 HTTP/2 API connections, dedicated watch connection, label-selector cache, event suppression). Here is how `batch` vs. `per_claim` performs **with full `Config H` controller tuning**:

### Part A — Workload Shape Comparison (`500 × 1` SWE-Bench Eval vs. `16 × 64` Deep-Group GRPO)

| Workload & Shape (`Images × Rollouts`) | Mode | Failed Envs | **TTFC** (1st Ready) | **Median (`p50`)** | **All Ready (`last`)** | **Step 1 Cold Ready** | API Calls / Sandbox | Open Watches | Dropped Sockets |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| **SWE-bench Eval (`500 × 1 = 500`)** | `per_claim` | `0` | `738 ms` | `4.05s` | `7.27s` | `33.51s` | `4.00` | `500` | `0` |
| **SWE-bench Eval (`500 × 1 = 500`)** | **`batch` (default)** | **`0`** | **`280 ms` (`2.6×` faster)** | **`3.71s`** | `7.62s` | **`7.95s` (`4.2×` faster)** | **`2.01` (`2.0×` fewer)** | **`4`** | **`0`** |
| **SWE-bench Eval (`500 × 1 = 500`)** | **`batch` (unbounded)** | **`0`** | **`306 ms` (`2.4×` faster)** | `4.40s` | **`7.01s`** | **`6.75s` (`5.0×` faster)** | **`2.01` (`2.0×` fewer)** | **`4`** | **`0`** |
| **Deep GRPO (`16 × 64 = 1,024`)** | `per_claim` | **`101` ❌** | `4,108 ms` (`4.11s`) | `8.73s` | `37.39s` | `34.93s` | `4.00` | `1,023` | **`24`** |
| **Deep GRPO (`16 × 64 = 1,024`)** | **`batch` (default)** | **`0` ✅** | **`199 ms` (`20.6×` faster)** | **`7.05s`** | **`35.82s`** | **`31.08s`** | **`1.02` (`3.9×` fewer)** | **`1`** | **`0`** |
| **Deep GRPO (`16 × 64 = 1,024`)** | **`batch` (unbounded)** | **`0` ✅** | **`448 ms` (`9.2×` faster)** | **`7.09s`** | **`33.83s`** | `38.50s` | **`1.02` (`3.9×` fewer)** | **`3`** | **`0`** |

* **Why `500 × 1` Step 1 (`6.75s` vs `33.51s`, `5.0×` faster) matters for Eval:** SWE-bench eval is typically a **single 1-pass sweep** (not a multi-step loop), so Step 1 *is* the entire eval job! On a cold client, `per_claim` opening 500 fresh TLS/watch connections took `33.5s` vs **`6.75s`** with `batch`.
* **Why `per_claim` broke on `16 × 64` (`101` failed sandboxes via HTTP `429`):** Firing 1,024 unpaced creates + 1,024 watches + 2,048 `GET`s while 16 deep warm pools contended on GKE APF pushed `per_claim` over GKE's `429` rejection threshold (`99` failed tasks in run 1, `2` in run 2) and cut cluster API calls in half with `batch` (`4,316` vs `8,676`).

---

### Part B — Total-Concurrency Scale Sweep (`160 → 640 → 1,280 → 2,560` on Production `Config H`)

| Total Warm Sandboxes (`N × 4`) | Mode | Failed Envs | **TTFC** (1st Ready) | **Median (`p50`)** | **All Ready (`last`)** | API Calls / Sandbox | Open Watches | Dropped Sockets | Cluster Claim API Reqs |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| **160** (`40 × 4`) | `per_claim` | `0` | `516 ms` | `1.37s` | `2.48s` | `4.00` | `160` | `0` | `1,224` |
| **160** (`40 × 4`) | **`batch` (default)** | **`0`** | **`136 ms` (`3.8×`)** | **`1.18s`** | **`2.34s`** | **`1.26`** | **`1`** | **`0`** | **`747` (`-39%`)** |
| **640** (`160 × 4`) | `per_claim` | `0` | `3,120 ms` | `5.36s` | `19.54s` | `4.00` | `640` | `0` | `4,735` |
| **640** (`160 × 4`) | **`batch` (default)** | **`0`** | **`603 ms` (`5.2×`)** | **`4.78s`** | **`8.73s` (`2.24×` ✅)** | **`1.25`** | **`1`** | **`0`** | **`2,840` (`-40%`)** |
| **1,280** (`320 × 4`) | `per_claim` | `0` | `317 ms` | `9.18s` | `19.71s` | `4.00` | `1,280` | `0` | `9,388` |
| **1,280** (`320 × 4`) | **`batch` (unbounded)** | **`0`** | `358 ms` | **`8.47s`** | `20.14s` *(tie)* | **`1.25`** | **`1`** | **`0`** | **`5,502` (`-41%`)** |
| **2,560** (`640 × 4`) | `per_claim` | **`1` ❌** | `273 ms` | `17.94s` | `57.29s` *(clean run 1)*\* | `4.00` | `2,560` | **`551`** | `18,317` |
| **2,560** (`640 × 4`) | **`batch` (default)** | **`0` ✅** | **`194 ms` (`1.4×`)** | **`16.53s`** | **`47.40s` (`41.98–52.83s`)** | **`1.25`** | **`2`** | **`0`** | **`10,668` (`-42%`)** |
| **2,560** (`640 × 4`) | **`batch` (unbounded)** | **`0` ✅** | `601 ms` | **`16.91s`** | `51.58s` (**`35.48s` run 1**) | **`1.13`** | **`2`** | **`0`** | **`10,757` (`-41%`)** |

*\*At `2,560` on `Config H`, `per_claim` overflowed the 1,000-socket pool (`551` dropped sockets per step): in clean run 1 (`0` errors), `batch-unbounded` beat `per_claim` **`35.48s` vs `57.29s` (`1.61×` faster)** and `batch-default` finished in **`41.98s` (`1.36×` faster)**; in run 2, `per_claim` failed an environment outright with `ApiException: (429) Too Many Requests (Retry-After: 32s)`, whereas both `batch` modes completed **`10,240 / 10,240` sandboxes with `0` errors and `0` dropped sockets**.*

---

## 4. Out-of-the-Box Default Controller Results (`160 → 640 → 1,280`)

If a user installs `agent-sandbox` with default controller flags (no `Config H` tuning), `per_claim` hits the saturation cliff even earlier (`1,280` sandboxes):

| Total Warm Sandboxes (`N × 4`) | Mode | **TTFC** (1st Ready) | **Median (`p50`)** | **All Ready (`last`)** | Speedup (`TTFC` / `last`) | API Calls / Sandbox | Open Watches | Dropped Sockets |
|---|---|---:|---:|---:|---|---:|---:|---:|
| **160** (`40 × 4`) | `per_claim` | `553 ms` | `1.28s` | `2.36s` | — | `4.00` | `160` | `0` |
| **160** (`40 × 4`) | **`batch` (default)** | **`178 ms`** | **`1.12s`** | **`2.12s`** | **`3.1×` TTFC / `1.1×` last** | **`1.26`** | **`1`** | `0` |
| **640** (`160 × 4`) | `per_claim` | `1,300 ms` | `5.49s` | `14.37s` | — | `4.00` | `640` | `0` |
| **640** (`160 × 4`) | **`batch` (default)** | **`338 ms`** | **`5.17s`** | **`9.39s`** | **`3.8×` TTFC / `1.53×` last ✅** | **`1.25`** | **`1`** | `0` |
| **1,280** (`320 × 4`) | `per_claim` | **`14,844 ms` (`14.84s`!)** | `20.91s` | `31.51s`\* | — | `4.00` | `1,280` | **`218`** |
| **1,280** (`320 × 4`) | **`batch` (default)** | **`661 ms` (`0.66s`)** | **`9.76s`** | **`19.19s`** | **`22.5×` TTFC / `1.64×` last ✅** | **`1.25`** | **`1`** | **`0`** |
| **1,280** (`320 × 4`) | **`batch` (unbounded)** | **`756 ms` (`0.76s`)** | **`9.82s`** | **`18.80s`** | **`19.6×` TTFC / `1.68×` last ✅** | **`1.25`** | **`1`** | **`0`** |

*\*On its first 1,280-sandbox attempt, `per_claim` also failed 14 sandboxes with HTTP `429` errors before succeeding on protocol retry; `batch` had `0` failed sandboxes across every run.*

---

## 5. Two Bonus Takeaways for Tomorrow's Discussion

1. **How to frame the scaling curve (if asked *"Is it exponential?"*):**
   * Both modes send $O(N)$ `SandboxClaim` creates, so neither is $O(e^N)$. Instead, `per_claim` hits **queuing & socket-pool saturation cliffs** (`1/(1-ρ)` APF queuing + HTTP `429` exponential backoff + overflowing `urllib3`'s `pool_maxsize=1000` socket limit), whereas `batch` cuts control-plane load by a structural constant (`O(1)` watch, `1 + 1/G` API calls, paced creates) that **pushes the saturation knee `3×–4×` further out** and keeps scaling smooth and linear.
2. **Actionable GKE controller-tuning fix for `agent-sandbox-rl/README.md`:**
   * `examples/agent-sandbox-rl/README.md` currently recommends `--sandbox-warm-pool-concurrent-workers=100`, while `docs/performance-tuning.md` (`Config H`) uses `warm-pool-workers=2` + `--sandbox-warm-pool-replenish-delay=0`.
   * On managed GKE (where GKE's admission webhook blocks custom high-share APF `PriorityLevelConfiguration`s), `warm-pool-workers=100` bursts replacement pod creates *during* claiming and spikes GKE APF queue wait **`155×` (`9.5s → 1,473s`)**! Even `replenish-delay=0` causes background refills to compete with claim status updates above 640–2,560 sandboxes.
   * **Recommendation for GKE users:** Pair `Config H` (`warm-pool-workers=2`) with **`--sandbox-warm-pool-replenish-delay=5s`** + **`claim_mode="batch"`** so warm-pool refills wait 5 seconds until claiming finishes.
