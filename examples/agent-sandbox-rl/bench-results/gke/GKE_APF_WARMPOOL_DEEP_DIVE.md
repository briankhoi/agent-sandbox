# Deep Dive (From Scratch): Why `--sandbox-warm-pool-concurrent-workers=100` Spikes GKE API Queue Wait `155×`

*(Written assuming zero prior background on Kubernetes API server internals or APF.)*

---

## 1. What Is "APF" (`API Priority and Fairness`) in Plain English?

Every single thing in Kubernetes — your Python RL client creating `SandboxClaim`s, `agent-sandbox-controller` binding warm pods, `kube-scheduler` placing pods, and `48` node kubelets sending heartbeats — talks to **one central brain: `kube-apiserver`** (backed by `etcd`, Kubernetes's database).

If `kube-apiserver` tried to execute `5,000` concurrent database writes at the exact same millisecond, `etcd` would run out of memory and crash the whole cluster. To prevent that, Kubernetes puts a smart traffic cop in front of `kube-apiserver` called **APF (API Priority and Fairness)**:

* Think of `kube-apiserver` as a **highway tollbooth with `404` total open lanes ("concurrency seats")** (the exact number GKE provisioned on our cluster).
* Instead of one giant first-come-first-served line (where one noisy app could block node health checks), APF divides those `404` lanes into **priority buckets (`PriorityLevelConfiguration`s)** using routing rules (`FlowSchema`s):
  * **`system` / `node-high` lanes:** Reserved for kubelets and core GKE components so nodes never disconnect.
  * **`workload-high` / `leader-election` lanes:** Reserved for controller leader leases and built-in Kubernetes controllers.
  * **`workload-low` (`165` lanes on our GKE cluster):** The bucket where **custom third-party controllers running as Kubernetes `ServiceAccount`s live by default** — including **`agent-sandbox-controller`**!

**What happens when `agent-sandbox-controller` sends more than `165` concurrent requests into `workload-low`?**
1. Requests `#1` through `#165` enter the tollbooths immediately.
2. Requests `#166+` sit in APF's **waiting room (queue)**. Every millisecond every request spends sitting in that waiting room is added to a Prometheus counter on the API server called **`apf_wait_s`** (`apiserver_flowcontrol_request_wait_duration_seconds_sum`).
   * *Note:* Because `apf_wait_s` is **summed across all queued requests**, `500` requests each waiting `2` seconds = `1,000s` of cumulative `apf_wait_s`.
3. If the waiting room fills up completely — or a request waits too long in line — `kube-apiserver` **rejects the request with `HTTP 429 Too Many Requests` (`Retry-After`)**, forcing the caller into multi-second backoff!

---

## 2. The Two Competing Jobs Inside `agent-sandbox-controller`

When your RL job claims `640` sandboxes from `160` pre-warmed `SandboxWarmPool`s (`4` pods per pool), `agent-sandbox-controller` suddenly has **two completely different jobs** to do at the exact same time:

```mermaid
flowchart TD
    Client["Python RL Driver (640 acquire calls)"] -->|"POST SandboxClaim"| APIServer["GKE kube-apiserver (workload-low: 165 seats)"]
    subgraph Controller["agent-sandbox-controller (shares workload-low!)"]
        JobA["Job A — URGENT (Foreground Claim Binding)<br/>200 Claim/Sandbox Workers<br/>Bind existing warm pod + PATCH SandboxClaim Ready"]
        JobB["Job B — NOT URGENT (Background Pool Refill)<br/>WarmPool Workers (1 vs. 2 vs. 100)<br/>POST new Sandbox (~10 KB) + POST new Pod for NEXT step"]
    end
    JobA -->|"Competes for same 165 seats!"| APIServer
    JobB -->|"Competes for same 165 seats!"| APIServer
```

* **Job A — Foreground Claim Binding (URGENT — blocks your RL step!):**
  Attach an **already-running** warm pod to your `SandboxClaim` and `PATCH` `SandboxClaim.status` to `Ready` so your Python `fleet.acquire()` call can return in milliseconds.
* **Job B — Background Warm-Pool Refill (NOT URGENT — only needed for the *next* RL step minutes later!):**
  The moment a warm pod is claimed out of `Pool #1`, `Pool #1` notices *"Hey, I only have `3/4` warm pods left!"* and immediately starts creating a brand-new `Sandbox` (`~10 KB` object in `etcd`) + a brand-new `Pod` (`POST sandboxes`, `POST pods`, `kube-scheduler` binding, `PATCH pods`).

**Notice the trap:** Both **Job A (urgent claim binding)** and **Job B (non-urgent pool refill)** come from the **exact same `agent-sandbox-controller` `ServiceAccount`** — so by default, **they fight for the exact same `165` lanes in `workload-low`!**

---

## 3. Why Self-Hosted Kubernetes Can Isolate Them, and Why Managed GKE Blocks It

In [`docs/performance-tuning.md`](../../../../docs/performance-tuning.md#L234-L246) and [`docs/apf-insulation.md`](../../../../docs/apf-insulation.md), the repo ships an overlay (`examples/apf-insulation/apf-insulation.yaml`) for self-managed clusters (`kops` / `kubeadm`) that creates **custom APF priority buckets** (`200` extra concurrency shares) so urgent `SandboxClaim` writes get their own VIP highway lane separate from background `SandboxWarmPool` refills.

**Why couldn't we apply `apf-insulation.yaml` on GKE?**
When we tried on GKE (`PROVENANCE.md` Step 7), GKE's built-in admission webhook (`flowcontrol-guardrails.common-webhooks.networking.gke.io`) **rejected it**:
1. GKE forbids mapping any user workload to `exempt` (`"can't reference cluster-critical priority level configuration"`).
2. GKE caps custom `PriorityLevelConfiguration` shares at `≤ 30` spare shares (`"requested shares 200 exceed available shares by 170"`) — which would actually *shrink* the controller from `workload-low`'s `165` seats down to `44` seats!

GKE enforces this intentionally so a user's runaway controller can never starve GKE's managed control plane. **That means on Managed GKE, `agent-sandbox-controller` MUST regulate its own background refill concurrency in flags.**

---

## 4. What Happened When We Set `--sandbox-warm-pool-concurrent-workers=100`

[`examples/agent-sandbox-rl/README.md`](../../README.md#L896) currently recommends:
```text
--sandbox-warm-pool-concurrent-workers=100
```
*(Why did the RL README suggest `100`? Because each warm-pool worker reconciles one `SandboxWarmPool` at a time, so `100` workers refills `100+` pools faster when warming a cluster from scratch.)*

**Here is what happened on live GKE at `640` warm sandboxes (`160` pools × `4` pods) when we turned on `--sandbox-warm-pool-concurrent-workers=100` (`bench-results/gke-tuned-wp100/`):**

1. At `t = 0s`, the Python driver sent `640` `SandboxClaim`s across `160` warm pools.
2. Within the first `~500 ms`, the controller's `200` claim/sandbox workers bound the first wave of warm pods across all `160` pools.
3. Instantly, all `160` `SandboxWarmPool` objects became "under-replicated" (`3/4` → `0/4` pods).
4. With **`--sandbox-warm-pool-concurrent-workers=100`** (and `--sandbox-warm-pool-replenish-delay=0`), **`100` warm-pool worker goroutines woke up at the exact same instant** and blasted heavy `POST sandboxes` + `POST pods` refill writes into `workload-low` **right on top of the `200` claim/sandbox workers still trying to finish binding your `640` claims!**
5. Now **`300` controller goroutines** (+ the Python driver!) were all slamming a `165`-seat `workload-low` tollbooth at once:

| Controller Warm-Pool Worker Setting (`w640` = `160` pools × `4`) | Cumulative APF Queue Wait (`apf_wait_s`) | Controller `PATCH` Latency | Full-Batch Ready (`batch-default`) |
|---|---:|---:|---:|
| **Out-of-the-Box (`warm-pool-workers=1`)** — refills 1 pool at a time | `9.5s – 22.3s` | `~42 ms` | `9.39s` |
| **Perf-Tuning Guide (`warm-pool-workers=2`, `rate=100`)** — gentle 2-pool refill | `497.8s` | `~75 ms` | **`8.73s` (`0` HTTP `429`s)** |
| **RL README (`warm-pool-workers=100`, `rate=100`)** — 100-pool refill stampede | **`1,043s – 1,473s` (`up to 155×`!)** | **`~290 ms` (`6.8×` slower + `49–104` HTTP `429`s!)** | `12.30s` (`+41%` slower!) |

---

## 5. The Simple Takeaway & Fix for GKE Users

You don't need custom APF manifests on GKE — you just want **background pool refills to stay out of the way while foreground claims are binding**:

1. **Keep `--sandbox-warm-pool-concurrent-workers` bounded (`2`–`50` depending on active pool count, rather than `100` with unpaced refills) + `--sandbox-warm-pool-max-refill-rate=25..100`** so background refills do not stampede all `165` `workload-low` APF seats during a claim burst.
   * *Note on controller connection flags:* Also pair high worker counts with `--api-connections=4 --separate-watch-connection=true` so controller informer watches are isolated from write bursts and a single HTTP/2 connection's ~100-stream limit doesn't bottleneck reconcilers.
2. **For synchronous bursty RL steps with `claim_mode="batch"`:** Optional `--sandbox-warm-pool-replenish-delay=5s` delays replacement pod creation until the foreground claim burst finishes. *(Caveat for `per_claim` callers: under `per_claim`, if transient claim failures drain a pool, a non-zero replenish delay holds back recovery — another reason `claim_mode="batch"`'s `0`-error pacing + retry is safer.)*

---

## 6. Companion Code & Config Fixes Discovered During the Distributed RL (`Tunix`) Audit

Putting all follow-up fixes from our GKE + distributed RL (`google/tunix` `deepswe_dist`) audit in one place so we can review them together:

### Fix A — Docs / Controller Flag Guidance (`examples/agent-sandbox-rl/README.md`)
* **Current issue:** [`examples/agent-sandbox-rl/README.md`](../../README.md#L896) recommends `--sandbox-warm-pool-concurrent-workers=100` without noting Managed GKE's `workload-low` APF sharing or HTTP/2 connection flags.
* **Proposed doc update:** Split the tuning guidance into:
  * **Managed GKE profile (shared `workload-low` APF):** `--sandbox-concurrent-workers=400`, `--sandbox-claim-concurrent-workers=150..200`, `--sandbox-warm-pool-concurrent-workers=2..50` (sized to active pools, paired with `--sandbox-warm-pool-max-refill-rate=25..100`), `--sandbox-warm-pool-readiness-grace-period=15m`, `--api-connections=4`, `--separate-watch-connection=true`, `--disable-claim-events=true`, `--disable-claim-observability-annotations=true`, plus controller resources (`requests: cpu=2, memory=16Gi`).

### Fix B — Avoid Redundant `_ensure_pool` Calls on Unplanned Distributed Workers (`agent_sandbox_rl/fleet.py`)
* **Where it happens:** [`SandboxFleet._acquire_reserved`](../../agent_sandbox_rl/fleet.py#L1316-L1354).
* **How distributed RL triggers it:** In multi-pod RL architectures like `google/tunix` (`deepswe_dist`), **1 central orchestrator pod** pre-warms `SandboxWarmPool`s via `PrewarmDatasetIterator`, while **`16–128` separate TPU `RolloutWorker` pods** lazily initialize their own singleton `SandboxFleet` (`shared_run_id`) and call `fleet.acquire(task)` per episode.
  * When `PREWARM_IMAGES_FILE` is **not** set on the `RolloutWorker` pods (dynamic sliding-window dataset mode in `deepswe.py`), the worker's `init_global_fleet(tasks=None)` creates an empty `self.plan_` (`0` entries).
  * On **every** `fleet.acquire(task)` call on that worker, `entry = self.plan_.for_image(task.image)` is `None` (`on_demand = True`), which currently calls `self._ensure_pool(cluster, task.image, 1)` **before** checking `(cluster.name, task.image) in self._ondemand`:
    ```python
    # Current code in agent_sandbox_rl/fleet.py (lines 1331-1341):
    if on_demand:
      cluster = self.placement.select(task.image, self.registry)
      pool = self._ensure_pool(cluster, task.image, 1)   # <-- 2 API calls (ensure_template + create_warmpool 409) EVERY claim!
      key = (cluster.name, task.image)
      with self._lock:
        created_pool = key not in self._ondemand
        if created_pool:
          self._ondemand.add(key)
    ```
  * Even though `create_warmpool(..., reconcile=False)` safely ignores the `409 Conflict` without shrinking the orchestrator's `16`-replica pool, running `_ensure_pool()` unconditionally adds **2 extra K8s API round-trips per claim** (`ensure_template` + `create_warmpool` 409) — defeating `SandboxBatch`'s `1.02–1.25 reqs/env` efficiency!
* **Minimal fix in `fleet.py`:** Only call `self._ensure_pool(cluster, task.image, 1)` when `key not in self._ondemand` (with per-key synchronization so concurrent first claims for the same image wait until `_ensure_pool` finishes, and rolling `key` back out of `self._ondemand` if `_ensure_pool` raises):
  ```python
  if on_demand:
    cluster = self.placement.select(task.image, self.registry)
    key = (cluster.name, task.image)
    pool = self.config.pool_name(task.image)
    with self._lock:
      created_pool = key not in self._ondemand
    if created_pool:
      pool = self._ensure_pool(cluster, task.image, 1)
      with self._lock:
        created_pool = key not in self._ondemand
        if created_pool:
          self._ondemand.add(key)
  ```
  *(Even cleaner on the Tunix side as well: pass `adopt_existing=True` on `RolloutWorker` pods or populate `init_global_fleet` on workers the same way `eval_worker.py` already does.)*

### Fix C — Multi-Worker `BatchClaimConfig` Guidance for Distributed `RolloutWorker` Pods (`Tunix` `sandbox_utils.py`)
* **Why it matters:** `SandboxBatch`'s default `max_in_flight=20` is tuned for a **single** client process. When **`R` distributed `RolloutWorker` pods** (`R = 16..128` in `mlperf_gbs1024_submission.sh`) each open one `SandboxBatch`, `128 × 20 = 2,560` aggregate in-flight `POST` capacity if all workers burst simultaneously at step 0.
* **Recommended `sandbox_utils.py` wiring once `claim_mode="batch"` merges:**
  ```python
  BatchClaimConfig(
      max_in_flight=max(4, min(20, math.ceil(256 / rollout_replicas))),
      work_budget=max(3600, int(episode_timeout_secs + reward_timeout_secs + 300)),
      lease_duration=300,
  )
  ```
  This keeps cluster-wide `POST` concurrency inside GKE `workload-low`'s `165` seats across any number of rollout replicas, collapses `1,024–2,048` per-claim watches down to `R` (`1` per worker pod), auto-GCs crashed workers' claims via per-worker `Lease` `ownerReferences`, and guarantees `shutdownTime` outlives long multi-turn episodes.
