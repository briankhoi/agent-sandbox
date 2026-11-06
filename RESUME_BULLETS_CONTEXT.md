# Google SWE Internship Resume Bullets — Full Context & Ground-Truth Fact Sheet

**Role:** Software Engineering Intern — Google *(GKE / Kubernetes SIG Apps: `kubernetes-sigs/agent-sandbox`)*
**Target Audience:** Primary = Internal Google SWE Conversion Packet (Hiring Committee); Secondary = External New Grad SWE / AI-Infra Resumes.

---

## 1. Resume Formatting & Style Constraints

- **Bullet count:** 4 bullets covering the internship (2 on the flagship `SandboxBatch` + `agent-sandbox-rl` stack, 1 on the Go `v1.0.0` `Sandbox` controller optimization, 1 on Python SDK memory + process-exit pod leak fixes).
- **Length budget:**
  - **Strict 2-line target:** `~190–215` characters (accounting for bolded numbers).
  - **Medium 2–2.5-line target:** `~210–250` characters.
- **Formatting rules:**
  - **No backticks** in final resume bullets (plain text + **bolded numbers/versions** only).
  - Clear to a strong distributed-systems / ML-infra engineer who has **zero prior context** on `agent-sandbox` or `agent-sandbox-rl` (no unexplained repo-internal shorthand, no ambiguous units, no conflating concurrent vs. cumulative counts).
- **Author's Voice / Cadence Reference (215 chars, 2 full lines on resume):**
  > Migrated **1200+** request edges directly onto mesh (Istio) by capturing **100%** of sidecar fallback requests and consolidating fallback traffic to a new Envoy gateway, eliminating mesh routing reliance on **2** legacy proxies

---

## 2. Full Uncompressed Extensive Bullets (Complete Context per Item)

*(Written without backticks so clauses can be sliced directly into resume bullets.)*

### Item 1 — Core Python Batch Sandbox-Claiming SDK (`SandboxBatch` / `AsyncSandboxBatch`, PRs `1/N–24/N`)
- Architected synchronous and asynchronous batch sandbox-claiming APIs (SandboxBatch and AsyncSandboxBatch) in the Python SDK (k8s-agent-sandbox) for kubernetes-sigs/agent-sandbox, cutting per-sandbox Kubernetes API calls **5x** (from **5** down to **~1.03** RPCs/sandbox in batch lifecycle mode: **309** vs. **1,500** total requests for **300** sandboxes; and **3x** from **6** down to **2.0** RPCs/rollout in rolling individual-release mode) and accelerating **1,000**-sandbox teardown latency by **13.7x** (**30.77s** down to **2.25s**) by replacing N per-sandbox readiness watches with **1** shared label-filtered watch stream (with resourceVersion bookmark recovery and single-use watch-seeded Pod IPs that eliminate **2N** Sandbox GET lookups), replacing N serial DELETE calls with **1** label-scoped Kubernetes DELETECOLLECTION bulk delete (with create quiescing and multi-round consistent live counting), and backing batches with fenced Kubernetes Lease ownership (fencing release on Lease UID, leaseTransitions, and resourceVersion CAS, plus ownerReferences cascade garbage collection and a two-phase stale reaper) to automatically clean up orphaned sandboxes after client crashes while bounding steady client sockets to **69** (**60.5x** fewer stale CLOSE_WAIT sockets, **2.64x** fewer OS threads, and **2.28x** lower RSS growth) across **10,000**-claim single-process endurance runs

### Item 2 — SDK Traffic Shaping + RL Fleet Library Integration & GKE Scale (`19/N` + `25/N` + GKE Campaign)
- Engineered SDK-level warm-pool credit fast paths (WarmCredit, seeding credits from SandboxWarmPool readyReplicas so claims backed by Ready warm pods bypass rate limits while zeroing credits when the watch observes cold fallbacks) and client-side admission control (capping in-flight creates at **64** and creation rate at **100** RPS with non-latching Retry-After cooldowns to stay within Kubernetes API Priority and Fairness per-ServiceAccount queues), and integrated per-cluster batch claiming and pre-expiry recycled-claim rotation into the agent-sandbox-rl fleet library (SandboxFleet and AsyncSandboxFleet, powering synchronous RL training rollouts like Tunix GRPO and R2E-Gym and agent evaluations like SWE-bench Verified on GKE)—eliminating **13%–64%** HTTP 429 rate-limit rollout crashes down to **0%** (**100%** task completion) across **500–1,024** simultaneously active gVisor sandboxes per step on a **10**-node GKE cluster while cutting burst p99 claim latency up to **4.96x** (**31.96s** down to **6.45s**), synchronous RL step-barrier wait time **2.24x**, median environment readiness **3.57x**, and end-to-end wall-clock runtime **2.10x–2.76x** (**2.41x–2.71x** higher usable rollout goodput, beating every published stock-GKE T0 **3,000**-task SWE-bench record)

### Item 3 — Go Kubernetes `Sandbox` Controller Optimization (`#1417`, `#1467` — Shipped in `v1.0.0`)
- Optimized the core Go Sandbox Kubernetes controller in kubernetes-sigs/agent-sandbox for the **v1.0.0** release, cutting reconciliation loop latency by **55%** (**14.54ms** down to **6.51ms**) and reducing unnecessary control-plane write load by eliminating a redundant pod-name annotation write to the Kubernetes API server on every reconcile loop while preserving backward-compatible empty-annotation handling across client SDKs

### Item 4 — Python SDK Memory Leaks (`#2082–#2087`) & Process-Exit Cluster Pod Leak (`#1510`/`#1512`, `v1.0.1`)

#### 4 (Combined — Client RAM Leaks + Process-Exit Cluster Pod Leak)
- Independently uncovered via memray and tracemalloc heap profiling and fixed **3** Python SDK client memory leaks alongside a silent process-exit pod leak—slashing long-lived async client RSS memory growth **196x** (from **90.1 MiB** down to **0.46 MiB** per **100** sandboxes across matched **600**-sandbox runs, and eliminating a **1.75 GiB** linear RAM leak from **93.8 MiB** to **1,752.4 MiB** across **2,400** created/terminated sandboxes) by untracking terminated handles from the connection registry, decoupling global atexit hooks from client self references, and breaking handle-to-connector circular bound-method references on close so CPython reference counting immediately frees **~0.7 MiB** of native OpenSSL SSLContext state per handle instead of stalling behind container-count-triggered cyclic GC—while fixing default AsyncSandboxClient atexit cleanup that previously failed on **100%** of real process exits (**v1.0.1**, leaving every unclosed Kubernetes sandbox pod running orphaned in the cluster) by tracing an aiohttp netrc threadpool dispatch blocked during CPython interpreter shutdown and switching exit teardown to a timeout-bounded synchronous urllib3 client

#### 4A (Standalone — Client Memory Leaks Only: Issues `#2082–#2084` / PRs `#2085–#2087`)
- Independently uncovered via memray heap profiling and resolved **3** memory leaks in long-lived Python SandboxClient and AsyncSandboxClient instances, cutting async client RSS memory growth **196x** (**90.1 MiB** to **0.46 MiB** per **100** sandboxes across matched **600**-sandbox runs) and eliminating a **1.75 GiB** linear RAM leak (**93.8 MiB** to **1,752.4 MiB**) across **2,400** sandbox lifecycles by removing terminated handles from the active connection registry, decoupling global atexit callbacks from client self references, and breaking circular bound-method references between handles and connectors on close so CPython reference counting immediately frees **~0.7 MiB** of native OpenSSL SSLContext state per handle without waiting on cyclic garbage collection

#### 4B (Standalone — Process-Exit Cluster Pod Leak Only: Issue `#1510` / PR `#1512`, Shipped in `v1.0.1`)
- Diagnosed and fixed a silent cleanup bug in the Python AsyncSandboxClient (shipped in **v1.0.1**) where default atexit exit cleanup failed on **100%** of real process exits—leaving every unclosed Kubernetes sandbox pod running orphaned in the cluster and wasting idle compute—by tracing an aiohttp netrc lookup dispatched via run_in_executor after CPython interpreter shutdown had already disabled new threadpool scheduling, switching exit teardown to a timeout-bounded synchronous urllib3 HTTP client with zero event-loop or thread dependencies, and building real-subprocess interpreter-shutdown regression tests

---

## 3. Current Working Medium-Length Drafts (~195–250 Chars)

* Architected Python batch sandbox-claiming APIs in kubernetes-sigs/agent-sandbox, cutting K8s API calls **5x** (**5** to **~1**/sandbox) and **1K**-pod teardown time **13.7x** by replacing N watches/deletes with **1** shared watch, DELETECOLLECTION, and fenced Lease crash GC *(250 chars)*
* Engineered SDK warm-pool credit fast paths and K8s APF admission control, integrating batch claiming into agent-sandbox-rl (Tunix GRPO, SWE-bench) to cut HTTP 429s (**13–64%** to **0%**), p99 claim latency **4.96x**, and wall time **2.76x** at **500–1,024** active GKE pods *(244 chars)*
* Optimized Go Sandbox controller reconciliation by **55%** (**14.54ms** to **6.51ms**) for the **v1.0.0** release by eliminating a redundant Kubernetes API server pod-name annotation write on every reconcile loop *(195 chars)*
* Uncovered and cut Python SDK RAM growth **196x** (**90.1** to **0.46 MiB**/**100** sandboxes; **1.75 GiB** leak via memray) by breaking circular OpenSSL refs, and fixed async atexit cleanup failing on **100%** of exits (**v1.0.1**, leaking live K8s pods) via sync HTTP *(249 chars)*

---

## 4. Exact Ground-Truth Audit & Methodological Guardrails (For Reviewers / Agents)

When editing or tightening these bullets, **do not violate** any of the following verified facts:

1. **Concurrent vs. Cumulative GKE Scale (`Bench 1A–3B`):**
   - `36,292` (or `37,092` including `800` warmup claims) is the **cumulative total of sequential tasks** across 6 benchmark suites over 10.5 hours on a **single 10-node gVisor GKE cluster** (`asrl-bench-t0`), **NOT** 36,000 simultaneously running pods.
   - **True simultaneous active concurrency per step:** **`500`** concurrent sandboxes (SWE-bench `500x1`, SWE-bench `500x6=3,000`, R2E-Gym `500x2x8=8,000`) and **`1,024`** concurrent sandboxes (Tunix GRPO `64x16x8=8,192`), plus **`1,000`** concurrent claims in standing warm-pool burst tests (`Bench 2A`).
   - **Single vs. Multi-cluster:** Although `agent_sandbox_rl.BatchClaims` keys one batch handle per cluster to support multi-cluster fleets, all GKE benchmarks ran on **1 cluster** (`10 x e2-standard-32`). Do not claim multi-cluster benchmark execution.
2. **Exact `5 → ~1` Kubernetes RPCs/Sandbox Math (`optimizations.md`):**
   - Legacy `per_claim` issues **5 RPCs/sandbox** in batch lifecycle (`1 POST` create + `1` dedicated `/watch` + `2 GET /sandboxes/{name}` for Pod IP lookup & first command + `1 DELETE`).
   - `SandboxBatch` issues **`1 + O(1)/N` RPCs/sandbox** (`300 POST` creates + `9` shared calls = `309` vs. `1,500` at `N=300`, i.e., **`1.03` RPCs/sandbox**, a **5x** reduction) because readiness uses `1` shared label watch, Pod IPs are seeded from the watch (`0` `GET`s), liveness uses `1` shared `Lease`, and cleanup uses `1` label-scoped `DELETECOLLECTION`. (In rolling individual `release_member()` mode without `DELETECOLLECTION`, it drops **3x**, from `6` to `2.02` RPCs/rollout.)
3. **Why Teardown Is `13.7x` Faster (`Bench 2A`, `c=1,000`):**
   - We did **not** optimize Kubernetes `DELETECOLLECTION` internals; we replaced **1,000 serial per-claim `DELETE` HTTP calls** (`30.77s–31.55s`) with **1 label-scoped `DELETECOLLECTION` call** (`2.25s–2.30s`). Word bullets so outsiders understand the `N deletes → 1 bulk delete` cause.
4. **Why Raw GKE Latency Seconds Are Omitted in Bullets 1–2:**
   - The `T0` GKE campaign ran in-cluster (`ServiceAccount` in `workload-low` APF), on a Day-1 `2–12 vCPU` apiserver with conservative `wp=50` warm-pool workers and no pre-baked Secondary Boot Disks (`T1`). Matched A/B ratios (`2.76x` wall time, `4.96x` `p99` claim latency, `2.41x–2.71x` goodput, `13.7x` release) and error rates (`13–64% → 0%`) are apples-to-apples, whereas raw seconds (`~10–12s` claim under heavy burst) will drop further in the tuned parity campaign.
5. **Exact Math on the `196x` Memory-Growth Reduction (`#2082` & `#2083`, all 3 PRs `#2085–#2087` Merged):**
   - Public Issue `#2082` (40-worker `kind` run over `2,400` cycles on `main`): RSS grew linearly from `93.8 MiB → 1,752.4 MiB` at a slope of **`72.4 MiB / 100 sandboxes`** (`84%` in `ssl.create_default_context` via `memray`).
   - Matched 600-cycle `kind` benchmark in Issue `#2083` (`g3/summary.json`, 40 workers, `600` rollouts): RSS slope in the second half (`#300 → #600`) was **`90.05 MiB / 100` (`90.1 MiB`)** on `main`, **`29.35 MiB / 100` (`29.3 MiB`)** with `#2085` alone (`309 MiB` glibc heap peak), and **`0.459 MiB / 100` (`0.46 MiB`)** (`156.1 → 162.8 MiB` OS RSS; glibc live allocated bytes fell `72.4 → 40.7 MiB`) with `#2085 + #2086`. Idle per-handle retention 60s after 100 cycles dropped from **`0.66 MiB` to `0.0001 MiB`** (`#2083`).
   - Comparing the matched 600-cycle `main` and fixed slopes gives **`90.050 / 0.459 = 196.1x` (`90.1 / 0.46 = 195.9x` → `196x` lower RSS growth per sandbox)**! (Cross-run `72.4 / 0.46 = 157x` is slightly lower only because glibc arena reuse amortizes per-handle page overhead slightly over 2,400 vs. 600 cycles.)
6. **Exact Behavior of the `v1.0.1` `atexit` Leak (`#1510` / `#1512`):**
   - Affected `AsyncSandboxClient` with default `cleanup=True` when the caller exited without an explicit `await client.close()` / `async with`. Failed deterministically on **100% of real process exits** (`RuntimeError: cannot schedule new futures after interpreter shutdown` when `aiohttp` with `trust_env=True` called `loop.run_in_executor` for `.netrc` lookup during `atexit`).

---

## 5. Primary Source Links

- **GKE Benchmark Executive Summary:** https://github.com/briankhoi/agent-sandbox/blob/bench/rl-gke/examples/agent-sandbox-rl/bench-results/gke/EXECUTIVE_SUMMARY.md
- **GKE Benchmark Full Report:** https://github.com/briankhoi/agent-sandbox/blob/bench/rl-gke/examples/agent-sandbox-rl/bench-results/gke/GKE_BATCH_BENCHMARK_RESULTS.md
- **Go Controller Optimization (`v1.0.0`):** https://github.com/kubernetes-sigs/agent-sandbox/pull/1417 & https://github.com/kubernetes-sigs/agent-sandbox/pull/1467
- **Python `atexit` Pod Leak (`v1.0.1`):** https://github.com/kubernetes-sigs/agent-sandbox/issues/1510 & https://github.com/kubernetes-sigs/agent-sandbox/pull/1512
- **Python SDK Memory Leaks:** Issues `#2082`, `#2083`, `#2084` & PRs `#2085` (merged), `#2086`, `#2087` on `kubernetes-sigs/agent-sandbox`
