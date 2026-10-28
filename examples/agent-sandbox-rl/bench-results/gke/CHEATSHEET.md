# RL Sandbox Claim-Mode Benchmark — Meeting Cheatsheet (`per_claim` vs `batch`)

> **Snapshot status:** Final Sync #2 (`w160`, `w640`, `w1280`, `w2560`, `c640`, `w500` `500×1` SWE-bench Eval, `w1024` `16×64` Deep-Group GRPO, and `loadtest.py` across **Default Controller Flags** and **Production Tuned Controller Flags (`docs/performance-tuning.md` `Config H`)** — **all running our exact same `bench/rl-gke` code & controller binary, pre-`#1791`**). For a simpler plain-English narrative, start with [`WALKTHROUGH.md`](WALKTHROUGH.md)!
>
> **Cluster:** GKE Standard `1.37.1-gke.1697000` (`us-central1-a`), 24 → 48 × `n2-standard-8` sandbox nodes (`gVisor` + Image Streaming, 128 max pods/node), 1 × `e2-standard-8` dedicated `controller-pool`, 1 × `n2-standard-8` dedicated `driver` node, **404 APF seats** (`workload-low` = 165 seats).

---

## 1. Executive Summary (30-Second Read for Meeting with Tomer)

1. **Out-of-the-box Stock Controller (`50` claim workers, `1` HTTP/2 connection):**
   - **`batch` wins decisively on speed, tail latency, CPU, sockets, and API load at scale:**
   - At **`w1280`** (1,280 warm sandboxes across 320 pools):
     - **`batch-unbounded`: `18.80s` (`p50 = 9.82s`, `p99 = 18.58s`)** vs **`per-claim`: `31.51s` (`p50 = 20.91s`, `p99 = 31.19s`)** → **`1.68×` faster `ready_last` (`ratio = 0.597`, `"batch wins"`), `2.13×` faster `p50`**.
     - **`batch-default` (`max_in_flight=20`): `19.19s` (`p50 = 9.76s`, `0` HTTP 429s)** → **`1.64×` faster (`ratio = 0.609`, `"batch wins"`)**.
     - `per-claim` overflowed `agent_sandbox_rl`'s hardcoded **1,000-socket `urllib3` pool** (`cluster.py:35`, **`218` `pool_full_discards`**), burned **`105.66s` Python CPU** vs **`10.97s–12.53s`** (`8.4×–9.6×` less CPU), opened **`1,280` watches** vs **`1`**, and dropped 14 envs to HTTP 429s on run 1 before retry.
   - At **`w640`**: **`batch-default` wins `9.39s` vs `14.37s` (`1.53×` faster, `ratio = 0.653`, `"batch wins"`)**; `batch-unbounded` `10.34s` vs `14.37s` (`ratio = 0.720`).

2. **Tuned Controller Flags (`docs/performance-tuning.md` `Config H` flags on our same `bench/rl-gke` binary: `200` claim & sandbox workers, `2` warm-pool workers, `4` HTTP/2 connections, observability writes off):**
   - **Speed:** Raising controller concurrency rescued a *single* `per-claim` trainer at `1,280` from overflowing its 1,000-socket pool (`discards: 218 → 0`), bringing `w1280` `no_limit` to parity (`batch-unbounded` **`p50 = 8.47s`, `p99 = 19.16s`, `last = 20.14s`** vs `per-claim` **`p50 = 9.18s`, `p99 = 19.60s`, `last = 19.71s`**, `"inside noise"` — both bounded by ~20s GKE `workload-low` APF 429 retry tails).
   - **Out-of-the-box pacing win at `w640`:** **`batch-default` (`8.73s`, `0` HTTP 429s) crushed `per-claim` (`19.54s`, `50` HTTP 429s) by `2.24×` (`ratio = 0.447`, `"batch wins"`)**, because `max_in_flight=20` kept total cluster concurrency below GKE `workload-low`'s 165-seat ceiling (`0` APF 429 tail stalls!).
   - **Structural cost & multi-tenant headroom (even where 1-trainer speed ties):**
     - **Driver API requests:** **`1.25 / env` vs `4.00 / env`** (**`3.2×` fewer** — and **`4.0×` fewer** vs post-`#1791` `5.00 / env`!).
     - **Open watches:** **`1` vs `1,280`** (**`1,280×` fewer**).
     - **Cluster-wide claim-path API requests:** **`5,502` vs `9,388`** (**`-41.4%`** on `Config H`; **`6,634` vs `10,610` = `-37.5%`** on Stock).
     - **Trainer Python CPU:** **`11.26s` vs `24.64s`** (`per-claim` burns >100% of a core inside a `19.85s` step decoding 1,280 separate HTTP/1.1 watch streams).
     - **Crash safety:** `SandboxBatch` stamps Lease `ownerReferences` on every claim so a crashed RL job auto-GCs all 1,280 claims instead of leaking warm sandboxes.

3. **Three Orthogonal Layers (How Tomer's Blog Post + `batch` Compose):**
   | Layer | What it optimizes | Bottleneck removed |
   |---|---|---|
   | **1. GKE Image Streaming + `SandboxWarmPool`s** ([Blog Post](https://cloud.google.com/blog/products/containers-kubernetes/accelerate-agentic-rl-with-gke-agent-sandbox)) | Pre-pulls container layers & pre-boots gVisor pods | Container image pull & cold pod startup (~30s → ~0s) |
   | **2. `recycle=True` (`reset-and-reuse`)** (`PR #1232`) | Reuses already-claimed sandboxes across rollouts of the *same* image | Eliminates repeat `SandboxClaim` churn when image distribution is static |
   | **3. `claim_mode="batch"` (`SandboxBatch`)** (`PR #1991` stack) | Replaces `N` watches + `2N` `GET sandboxes` + unpaced bursts with **1 label-selector watch**, **0 `GET sandboxes`**, Lease GC, and coordinated `max_in_flight` | Eliminates control-plane watch/read fan-out (`4–5 → 1.25 reqs/env`), Python GIL & 1,000-socket HTTP/1.1 pool exhaustion on initial acquisition, heterogeneous SWE-bench/R2E steps, pool rotation, and multi-job GKE clusters |

---

## 2. Side-by-Side Results: Default Controller Flags vs. Tuned Controller Flags (`Config H`)

*(Steady state = Step 2 median across runs; ratios < 1.00 mean `batch` is faster.)*

| Size / Arm | Stock `last Ready` (min–max) | Stock `p50` / `p99` | Tuned `Config H` `last Ready` (min–max) | Tuned `p50` / `p99` | Driver Reqs/env | Watches | Discards (Stock / Tuned) | Driver CPU s (Stock / Tuned) | Claim-Path API Reqs (Stock / Tuned) |
|---|---|---|---|---|---:|---:|---|---|---|
| **`w160` / `per-claim`** | `2.36s` (1.86–2.86) | `1.28s` / `2.17s` | `2.48s` (2.43–2.53) | `1.37s` / `2.44s` | `4.00` | `160` | `0` / `0` | `2.38s` / `2.06s` | `1,022` / `1,224` |
| **`w160` / `batch-unbounded`** | **`2.16s`** (2.07–2.25) | `1.29s` / `2.14s` | **`2.38s`** (2.37–2.40) | **`1.15s`** / **`2.24s`** | **`1.25`** | **`0–1`** | `0` / `0` | **`1.14s`** / **`1.06s`** | **`531`** / **`742`** |
| **`w160` / `batch-default`** | **`2.12s`** (1.94–2.29) | **`1.12s`** / **`2.10s`** | **`2.34s`** (2.32–2.37) | `1.18s` / `2.33s` | **`1.26`** | **`1`** | `0` / `0` | `1.17s` / `1.17s` | `540` / `747` |
| **`w640` / `per-claim`** | `14.37s` (10.27–18.46) | `5.49s` / `10.60s` | `19.54s` (9.71–29.37) | `5.36s` / `19.30s` | `4.00` | `640` | `0` / `0` | `14.60s` / `15.84s` | `5,862` / `4,735` |
| **`w640` / `batch-unbounded`** | `10.34s` (10.29–10.38) | `5.38s` / `10.14s` | `18.95s` (8.23–29.68) | `4.81s` / `18.65s` | **`1.25`** | **`0–1`** | `0` / `0` | `5.96s` / `7.19s` | `3,678` / **`2,806`** |
| **`w640` / `batch-default`** | **`9.39s`** (9.35–9.43) ✅ | **`5.17s`** / **`9.26s`** | **`8.73s`** (8.68–8.78) ✅ | **`4.78s`** / **`8.50s`** | **`1.25`** | **`1`** | `0` / `0` | **`5.10s`** / **`6.29s`** | **`3,404`** / `2,840` |
| **`w1280` / `per-claim`** | `31.51s` (23.33–39.69)\* | `20.91s` / `31.19s` | `19.71s` (18.47–20.96) | `9.18s` / `19.60s` | `4.00` | `1,280` | **`218`** / `0` | **`105.66s`** / `24.64s` | `10,610` / `9,388` |
| **`w1280` / `batch-unbounded`** | **`18.80s`** (17.48–20.11) ✅ | `9.82s` / **`18.58s`** | `20.14s` (16.86–23.41) | **`8.47s`** / **`19.16s`** | **`1.25–1.33`** | **`1`** | **`0`** / **`0`** | `12.53s` / **`11.26s`** | **`6,634`** / **`5,502`** |
| **`w1280` / `batch-default`** | **`19.19s`** (17.65–20.74) ✅ | **`9.76s`** / `19.02s` | `24.59s` (18.57–30.61) | `10.20s` / `24.04s` | **`1.25`** | **`1–2`** | **`0`** / **`0`** | **`10.97s`** / `17.69s` | `6,719` / `5,529` |
| **`w2560` / `per-claim`** *(48 nodes)* | — | — | `46.05s` (34.80–57.29, **`1` HTTP 429 err** ❌) | `17.94s` / `33.96s` | `4.00` | `2,560` | — / **`551`** | — / `44.42s` | — / `18,317` |
| **`w2560` / `batch-unbounded`** *(48 nodes)* | — | — | `51.58s` (**`35.48s`**–67.68, **`0` errs** ✅) | `16.91s` / `37.58s` | **`1.13`** | **`2`** | — / **`0`** | — / `22.13s` | — / **`10,757`** |
| **`w2560` / `batch-default`** *(48 nodes)* | — | — | **`47.40s`** (41.98–52.83, **`0` errs** ✅) | **`16.53s`** / **`33.67s`** | **`1.25`** | **`2`** | — / **`0`** | — / **`21.97s`** | — / **`10,668`** |
| **`w500` (`500×1` Eval) / `per-claim`** | — | — | `7.27s` (Step 1: `33.51s`) | `4.05s` / `6.93s` | `4.00` | `500` | — / `0` | — / `8.51s` | — / `3,844` |
| **`w500` (`500×1` Eval) / `batch-unbounded`** | — | — | **`7.01s`** (Step 1: **`6.75s`**, `5.0×` faster ✅) | `4.40s` / **`6.91s`** | **`2.01`** | **`4`** | — / `0` | — / `5.12s` | — / `2,342` |
| **`w500` (`500×1` Eval) / `batch-default`** | — | — | `7.62s` (Step 1: **`7.95s`**, `4.2×` faster ✅) | **`3.71s`** / `7.46s` | **`2.01`** | **`4`** | — / `0` | — / **`4.41s`** | — / **`2,294`** |
| **`w1024` (`16×64` GRPO) / `per-claim`** | — | — | `37.39s` (**`101` HTTP 429 errs** ❌) | `8.73s` / `32.41s` | `4.00` | `1,023` | — / **`24`** | — / `27.02s` | — / `8,676` |
| **`w1024` (`16×64` GRPO) / `batch-unbounded`** | — | — | **`33.83s`** (**`0` errs** ✅) | `7.09s` / **`24.42s`** | **`1.02`** | **`3`** | — / **`0`** | — / `8.90s` | — / **`4,316`** |
| **`w1024` (`16×64` GRPO) / `batch-default`** | — | — | `35.82s` (**`0` errs** ✅) | **`7.05s`** / `31.74s` | **`1.02`** | **`1`** | — / **`0`** | — / **`7.79s`** | — / `4,555` |
| **`c640` (cold) / `per-claim`** | `27.04s` (25.47–28.60) | `16.92s` / `25.39s` | — | — | `4.00` | `640` | `0` | `12.03s` | `6,770` |
| **`c640` (cold) / `batch-unbounded`** | **`25.28s`** (25.20–25.36) | **`16.17s`** / **`24.55s`** | — | — | **`1.25`** | **`1`** | `0` | **`6.21s`** | **`4,765`** |

*\*Note: On Stock `w1280`, `per-claim` run 1 initially failed 14/1280 envs due to HTTP `429 Too Many Requests` (`superseded/w1280-per-claim-run1.json`) and succeeded on protocol retry; `batch` had `0` failed envs across every single run all night.*

---

## 3. Exact Per-Verb / Per-Resource Driver Request Attribution (`w1280`)

Why is `per-claim` **exactly `4.00` requests/env** (`5,120` requests at `w1280` — and **`5.00` requests/env** on current `upstream/main` after PR `#1791`), while `batch` is **exactly `1.25` requests/env** (`1,601` requests) in steady state?

| Verb & Resource | `per-claim` (`w1280` Steady) | `batch` (`w1280` Steady) | `batch` (`w1280` Step 1 Cold Batch Init) | Exact Code Call Site & Why |
|---|---:|---:|---:|---|
| **`POST sandboxclaims`** | `1,280` (`1.00/env`) | `1,280` (`1.00/env`) | `1,280` (`1.00/env`) | Creating each `SandboxClaim` (`K8sHelper.create_sandbox_claim`). Irreducible 1-per-claim create (until future server-side multi-claim CRD, if ever needed). |
| **`WATCH sandboxclaims`** | **`1,280`** (`1.00/env`) | **`1`** (`0.0008/env`) | `1–2` (`0.001/env`) | `per-claim`: `SandboxClient._wait_for_claim_ready` opens **1 dedicated watch HTTP stream per claim** (`fieldSelector=metadata.name=...`).<br>`batch`: `SandboxBatch._watch_loop` opens **1 shared label-selector watch** (`agents.x-k8s.io/batch-id=<id>`) for all 1,280 claims! |
| **`GET sandboxes`** | **`2,560`** (`2.00/env`) | **`0`** (`0.00/env`) | **`0`** (`0.00/env`) | `per-claim`: `SandboxFleet.acquire` calls `sandbox.get_pod_name()` (**1st GET**) + `sandbox.get_pod_ip()` (**2nd GET**) on `Sandbox` CR because `SandboxClient.create_sandbox` discards the watch event payload.<br>`batch`: `SandboxClaim.status` watch event *already* carries `sandbox.Name` + `podIPs`; `SandboxBatch.connect()` seeds `_seeded_pod_ip` directly (**0 GETs**)! |
| **`GET pods/status`** *(post-`#1791` on `main`)* | *`+1,280` (`+1.00/env` → **`5.00/env`**)* | **`0`** (`0.00/env`) | **`0`** (`0.00/env`) | Upstream PR `#1791` added `record_pod_identity()` (`read_namespaced_pod_status`) to `SandboxFleet.acquire` for `recycle=True` eviction detection. In `batch`, the shared watch tracks `pod_ips` / `MEMBER_LOST` for free in 0 GETs! |
| **`GET sandboxwarmpools`** | `0` | `320` (`0.25/env` = `1/K`) | `320` (`0.25/env`) | `SandboxBatch._maybe_refresh_warm_credit`: 1 read per pool (`320` pools for `K=4` tasks/image) to check `readyReplicas` so warm claims skip `create_rps` pacing. |
| **`GET sandboxtemplates`** | `0` | `0` | `320` (`0.25/env`) | One-time pool template validation on Step 1 (`_checked_pools` caches it across all subsequent steps). |
| **`LIST sandboxclaims` + `POST leases`** | `0` | `0` | `1 + 1` | One initial `LIST` (seeds `resourceVersion`) + 1 `Lease` create for crash-safe `ownerReferences` GC. |
| **TOTAL DRIVER REQUESTS** | **`5,120` (`4.00/env`)**<br>*(post-`#1791`: `6,400` = **`5.00/env`**)* | **`1,601` (`1.25/env`)** | **`1,923` (`1.50/env`)** | **`3.2×` fewer requests** (`4.0×` post-`#1791`), **`1,280×` fewer watches**, **`0` socket discards**. |

---

## 4. Metric Glossary (What Every Column in `summary.md` Means)

- **`last Ready s` (`ready_last`):** Wall-clock seconds from the start of the step until the **1,280th (last)** `fleet.acquire()` returns a Ready sandbox handle. Headline step-completion latency before task execution.
- **`Ready p50 s` / `Ready p99 s`:** Median (`50th` percentile) and tail (`99th` percentile) latency across all `N` `fleet.acquire()` calls in the step.
- **`last start s`:** Wall-clock seconds from step start until the last sandbox **completes its verification `exec` probe** (`CONNECT pods/exec`). Proves the pod is genuinely reachable and running user code inside gVisor, not just CR-Ready.
- **`step 1 last Ready s`:** Cold-client/first-step latency (includes batch Lease creation, template precheck, TLS pool warm-up). Steady state uses Step 2+.
- **`requests/env`:** Total K8s API HTTP requests sent **by the RL Python driver** during the step (before release), divided by `N` (`160`, `640`, `1,280`).
- **`watches`:** Number of `/watch` HTTP streams opened by the Python driver during the step (`1,280` for `per-claim` vs `1` for `batch`).
- **`pool-full discards`:** Count of `urllib3` `"Connection pool is full, discarding connection"` warnings in the driver. Notice `agent_sandbox_rl/cluster.py:35` hardcodes `pool_maxsize=1000` (right below Linux's default `ulimit -n = 1024`). Above 1,000 concurrent HTTP/1.1 requests/watches, `per-claim` creates unpooled one-off TLS sockets and throws them away after 1 request!
- **`CPU s` (`driver_cpu_s`):** Process `user + system` CPU seconds (`time.process_time()`) burned by the single Python RL driver process during the step. When `CPU s >= last start s`, the Python GIL / single-process event loop is saturated!
- **`API create ms` (`apiserver_create_ms`):** Server-side mean latency of `POST sandboxclaims` measured directly from `kube-apiserver` `/metrics` (`apiserver_request_duration_seconds_sum / _count`).
- **`APF wait s` (`apf_wait_s`):** Total cumulative seconds requests spent queued inside Kubernetes **API Priority & Fairness (APF)** (`apiserver_flowcontrol_request_wait_duration_seconds_sum` delta) across the step window.
- **`API claim-path requests`:** Server-side total across **all clients in the cluster (driver + controller)** of *all* `sandboxclaims` operations (`POST`, `PUT`, `PATCH`, `GET`, `LIST`, `WATCH`) **plus** *read-only* `sandboxes` operations (`GET`, `LIST`, `WATCH`). Intentionally excludes warm-pool refill writes (`POST/PATCH sandboxes`) and node heartbeats (`leases`) to isolate pure claim-adoption API cost!
- **`429s` (`http_429_all`):** Server-side count of HTTP `429 Too Many Requests` responses returned by `kube-apiserver` when an APF queue overflows during a burst. When a controller `PATCH/PUT sandboxclaims` gets a `429`, `controller-runtime` requeues that claim with backoff, inflating `p99` / `ready_last` to ~20–30s even when `p50` is ~5–8s!

---

## 5. Major Bonus Finding for Upstream Docs: GKE Multi-Pool APF Self-Interference (`wp=100` vs `wp=2` vs `batch-default`)

While testing controller tuning on managed GKE, we uncovered an important interaction between three upstream docs that is immediately actionable for `examples/agent-sandbox-rl/README.md` and `docs/performance-tuning.md`:

1. **The conflict:**
   - [`examples/agent-sandbox-rl/README.md`](https://github.com/kubernetes-sigs/agent-sandbox/blob/main/examples/agent-sandbox-rl/README.md) recommends `--sandbox-warm-pool-concurrent-workers=100` (for many 1-image warm pools).
   - [`docs/performance-tuning.md`](https://github.com/kubernetes-sigs/agent-sandbox/blob/main/docs/performance-tuning.md) (`Config H`) uses `--sandbox-warm-pool-concurrent-workers=2`, `--api-connections=4`, `--sandbox-warm-pool-max-refill-rate=100`, `--sandbox-warm-pool-replenish-delay=0` — validated on self-managed `kops` **with [`docs/apf-insulation.md`](https://github.com/kubernetes-sigs/agent-sandbox/blob/main/docs/apf-insulation.md) applied**.
2. **What happens on managed GKE:**
   - GKE's admission webhook (`flowcontrol-guardrails.common-webhooks.networking.gke.io`) **blocks** `apf-insulation.yaml` (`exempt` references are forbidden, and custom `PriorityLevelConfiguration`s are capped at 30 spare shares = fewer seats than `workload-low`'s 165 seats!).
   - Consequently, `SandboxClaimReconciler`, `SandboxWarmPoolReconciler`, `SandboxReconciler`, **and** the RL driver's `POST sandboxclaims` all share **`workload-low` (165 seats on a 404-seat GKE control plane)**.
   - Because `--sandbox-warm-pool-max-refill-rate=100` is a **per-pool** token bucket, setting `warm-pool-workers=100` across 160 active pools (`bench-results/gke-tuned-wp100/`) allowed all 100 workers to burst `640 POST sandboxes` + `1,281 POST pods` *inside* the 10s claim window, driving **`APF wait s` up `155×` (`9.49s → 1,473.03s`)** and `PATCH sandboxclaims` latency up `6.8×` (`42.7 ms → 290.2 ms`)!
   - Even with `Config H`'s `warm-pool-workers=2`, `4` HTTP/2 connections (`400` concurrent controller streams > `165` `workload-low` seats) + `--sandbox-warm-pool-replenish-delay=0` still spawned `640` replacement Sandboxes during unpaced `w640` bursts (`10–50` HTTP 429s, pushing `ready_last` on unpaced arms to ~19s), **whereas `batch-default` (`max_in_flight=20`) paced tenant creates smoothly so `0` HTTP 429s occurred and `w640` finished in `8.73s`!**
   - **Actionable GKE recommendation:** On managed GKE (where custom APF PriorityLevels are restricted), pair `Config H` (`warm-pool-workers=2`) with **`--sandbox-warm-pool-replenish-delay=5s`** (so warm-pool refills wait until the claim adoption burst finishes!) and **`claim_mode="batch"`**.

---

## 6. `loadtest.py` (`AsyncSandboxFleet.run` End-to-End 40 Pools × 4 Tasks = 160 Tasks)

All 20/20 runs (`10` Stock in `bench-results/gke-scripts/loadtest/`, `10` Tuned in `bench-results/gke-scripts/loadtest-tuned/`) succeeded (`ok = 160/160`):
- **`--max-concurrent 40` (script default):** `batch/unbounded` `claim_avg = 0.59s–0.64s` (`max = 0.89s–0.96s`) vs `per_claim` `0.67s–0.78s` (`max = 1.14s–1.23s`) on Stock (`0.7s–0.8s` vs `0.8s` on Tuned).
- **`--max-concurrent 160` (uncapped at 160 tasks):** `batch/defaults` `claim_avg = 1.39s–1.40s` (`1.2s–1.5s` Tuned), `batch/unbounded` `1.36s–1.56s` (`1.5s–1.7s` Tuned), `per_claim` `1.03s–1.45s` (`1.4s–1.6s` Tuned) — matching `gke_scale_bench.py` `w160` (`inside noise` at 160 tasks where neither socket pool nor GIL is stressed, while cutting watches `160 → 1` and driver requests `4.0 → 1.25/env`).
