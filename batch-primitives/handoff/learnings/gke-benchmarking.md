<a id="sec-gke-benchmarking"></a>

[Learnings home](../learnings.md) · [Field guide index](../learnings.md#sec-field-guide)

# Benchmarking and GKE at 1,000-sandbox scale

**How to read this page.** Running `148,544` real sandbox claims and multi-step RL rollouts across `36` zero-error GKE sweeps (`origin/bench/rl-gke`, `examples/agent-sandbox-rl/bench-results/gke/campaign-v2/` and `ai-on-gke/sandbox-rl-performance` PR #5) surfaced two families of lessons that unit tests and `kind` clusters cannot show:

- **GKE control-plane, node, storage, and runtime physics (`GKE-1` to `GKE-20`):** how GKE's Control Plane Autoscaler, API Priority and Fairness (APF), `kube-controller-manager` cascading GC QPS, `RuntimeClass` vs. `DaemonSet` taint admission, GCFS image streaming vs. Secondary Boot Disks (`CONTAINER_IMAGE_CACHE`), Kubelet `ImageGC`, `terminationGracePeriodSeconds` vs. uncached `containerd` wedges, controller candidate-queue races, and gVisor `gofer` filesystem metadata actually behave at `500–1,024` concurrent sandboxes.
- **Benchmark harness and measurement rigor (`BENCH-1` to `BENCH-12`):** non-obvious engineering in `bench_core.py`, `run_daemonset_prepull.py`, `run_job_v2.sh` / `run_job_v8.sh`, `verify_cluster.sh`, and independent raw-JSON report auditing that kept runs isolated, reproducible, and free of survivorship, queueing, or platform-drift distortion.

## GKE Campaign 2 scoreboard (`asrl-bench-c2`, `148,544 / 148,544` ok, `0` task errors)

| Suite & shape | `HEAD per_claim` | `SandboxBatch` (`batch`) | Pure `SandboxBatch` gain (and prior-baseline context) |
| :-- | --: | --: | :-- |
| **SWE-bench `500x6 = 3,000` (`T0`, stock APF, `naive`)** | `403.63 s` (`p99` `139.66 s`, `520` socks) | **`87.40 s`** (`p99` **`19.30 s`**, **`122`** socks) | **`4.62x` wall**, **`7.24x` lower `claim p99`**, `4.26x` fewer sockets (`6.72x` vs July `#10` `587.7 s`) |
| **SWE-bench `500x6 = 3,000` (`T0`, stock APF, `sliding`)** | `433.13 s` (`p99` `62.38 s`) | **`88.76 s`** (`p99` **`17.76 s`**) | **`4.88x` wall**, **`3.51x` lower `claim p99`** (`6.80x` vs July `#11` `603.8 s`) |
| **SWE-bench `500x6 = 3,000` (`T0`, stock APF, `pipelined`)** | `256.98 s` (`650` socks) | **`96.57 s`** (**`176`** socks) | **`2.66x` wall**, **`3.69x` fewer sockets** (`5.25x` vs July `#12` `507.4 s`) |
| **R2E-Gym Sync RL `500x2x8 = 8,000` (`Bench 1A`, `git + 5 s`)** | `1,494.54 s` (`138.32 s` steady step) | **`390.77 s`** (**`28.48 s`** steady step) | **`3.82x` wall**, **`4.86x` steady step barrier** (Steps 1-7), **`6.05x` `env_ready_avg`** |
| **Single `1,000`-pod warm burst (`Bench 2A`, `c=100..1000`)** | `0.48 / 0.90 / 2.15 / 4.38 s` | **`0.39 / 0.66 / 1.98 / 3.17 s`** | **`1.23x-1.47x` vs `per_claim`**, **`2.2x-2.8x` mean** (`3.0x-3.6x` best) vs Blog (`1.1 / 1.8 / 4.3 / 8.8 s`), **`183.5/s`** peak |
| **Multi-pool `200x5 = 1,000` warm burst (`batch20` default)** | `8.06 s` best (`488` socks) | **`6.63 s` best** (**`44` flat socks**) | **`11.1x` fewer sockets**, **`173 ms` `1st Ready`** (`3.56x` vs best, `10.8x` vs `r0`) |
| **Pure Python SDK `1,000`-claim cleanup (`p2_sdk_1000_cleanup`)** | `35.93 s` (`delete_all`, `1,000` DELETEs) | **`1.10 s` best / `1.44 s` mean** (`4` calls) | **`32.5x` peak / `24.9x` mean**, **`250x` fewer API calls** (`13.7x` faster than concurrent `terminate()` on stock APF) |
| **`10,000`-claim wave soak (`Bench 2B`, `50 x 200`)** | `610.37 s` (`512 MB` RSS, `3,017` threads) | **`573.28 s`** (**`321 MB` RSS**, **`1,857`** threads) | **`-37.3%` RSS**, **`-38.4%` threads**, **`+23.7%` peak burst rate** (`162.8/s`), `104 ms` `1st Ready` |
| **`T1` Secondary Boot Disk SWE-bench `3,000` (`Phase III`)** | `naive` `281.65 s`; `git+30s` `257.44 s` | **`naive` `139.10 s`**; **`git+30s` `248.84 s`** | **`2.02x` `naive` wall** (**`5.74x` lower `claim p50`**); **`8.91x` faster than `T1` record `#1`** (`2,218.10 s`) |

---

## GKE control plane, APF, storage, and runtime physics

<a id="gke-1"></a>

## GKE-1 Regional GKE apiserver seat capacity depends on control-plane sizing (`go_sched_gomaxprocs_threads`), not just worker node count

**Takeaway:** A brand-new regional GKE cluster starts with small `kube-apiserver` replicas (`2` vCPUs, ~`25` `workload-low` APF seats) even if you attach a `10`-node pool. Always inspect `go_sched_gomaxprocs_threads` on `/metrics` before benchmarking, and ensure the control plane has scaled to match production cluster tiers.

<details>
<summary>The full story (GKE Campaign 1 -> Campaign 2, PREFLIGHT_VERIFICATION.md #1)</summary>

- **Situation:** GKE's Control Plane Autoscaler sets each regional `kube-apiserver` replica's CPU (`GOMAXPROCS`) and `--max-requests-inflight` / `--max-mutating-requests-inflight` from historical cluster size. APF divides that nominal concurrency budget across priority levels (`workload-low`, `global-default`, `system`, etc.).
- **What goes wrong:** On a freshly created `10`-node cluster (Campaign 1), each regional `kube-apiserver` replica ran with `2` vCPUs and only `25` `workload-low` seats. Any `500`-concurrency burst immediately saturated `workload-low`, generating hundreds of HTTP `429` rejections that masked client and controller behavior behind control-plane starvation.
- **What we do:** In Campaign 2 (`asrl-bench-c2`, `PREFLIGHT_VERIFICATION.md` row #1), we verified `kubectl get --raw /metrics | grep '^go_sched_gomaxprocs_threads '` before running a single benchmark: `go_sched_gomaxprocs_threads = 16` across `3` regional HA replicas (`48` regional control-plane vCPUs, `483` nominal seats/replica = `1,449` regional seats, and `196` `workload-low` seats/replica = `588` regional `workload-low` seats — `23.5x` Campaign 1's seat budget), with `0` warm-up VMs left running during measurement.
- **Example:**
  ```text
  fresh un-scaled control plane -> gomaxprocs=2  ->  25 workload-low seats -> 429 storms at c=500
  scaled regional HA control plane -> gomaxprocs=16 -> 588 workload-low seats -> 0 429s across 24,000 Phase I tasks
  ```
- **Related:** [GKE-2](#gke-2), [GKE-3](#gke-3), [PERF-21](field-guide/perf.md#perf-21).

</details>

<a id="gke-2"></a>

## GKE-2 Driver authentication credentials decide which APF priority lane your client shares with the controller

**Takeaway:** On GKE, an in-cluster pod authenticating with a Kubernetes `ServiceAccount` token lands in `FlowSchema = service-accounts -> PriorityLevel = workload-low` (the exact same bucket as `agent-sandbox-controller` under stock APF), whereas GCE metadata IAM / user credentials land in `global-default`. Know which lane your driver hits and verify it in `/metrics`.

<details>
<summary>The full story (Campaign 2 Phase I & Phase II, PREFLIGHT_VERIFICATION.md #4)</summary>

- **Situation:** Stock Kubernetes APF ships a built-in `service-accounts` FlowSchema at precedence `9000` that routes every `system:serviceaccount:*` identity to `workload-low`, while authenticated non-ServiceAccount IAM users fall through to `catch-all` / `global-default`.
- **What goes wrong:** If a benchmark driver runs as an in-cluster `Job` with an automounted `ServiceAccount` token on stock APF, its `500` client creates and watches directly compete for the *same* `workload-low` seats that `agent-sandbox-controller` needs to adopt warm pods and patch `Ready` status ([PERF-21](field-guide/perf.md#perf-21)). Conversely, comparing an out-of-cluster laptop/VM driver (`global-default`) against an in-cluster `ServiceAccount` Job (`workload-low`) silently changes the APF lane between runs.
- **What we do:** In Phase I (`T0` stock-APF parity with `RESULTS.md` `#10-#16`), the GCE VM driver ran in the same `us-central1` region (`<1 ms` RTT) using GCE IAM authentication (`automountServiceAccountToken: false`), and we verified via `apiserver_flowcontrol_dispatched_requests_total{flow_schema="global-default"}` that `0` driver requests touched `workload-low`. In Phase II (in-cluster `ServiceAccount` Job), we paired the in-cluster driver with 3-lane APF insulation ([GKE-3](#gke-3)) so controller reconciles had dedicated non-lendable seats.
- **Example:**
  ```text
  in-cluster ServiceAccount token -> FlowSchema=service-accounts -> workload-low (shares seats with controller)
  GCE IAM / user token            -> FlowSchema=global-default   -> global-default (isolated from stock controller)
  ```
- **Related:** [GKE-1](#gke-1), [GKE-3](#gke-3), [PERF-21](field-guide/perf.md#perf-21).

</details>

<a id="gke-3"></a>

## GKE-3 GKE's managed APF webhook caps custom `PriorityLevelConfiguration` shares at 30 total

**Takeaway:** Vanilla Kubernetes APF manifests that allocate `100+` custom `nominalConcurrencyShares` fail or get clamped under GKE's managed APF guardrails. On GKE, split the `30`-share custom budget into a non-lendable critical controller lane (`20` shares) and a bulk client lane (`10` shares), while leaving controller `Lease` renewals on `global-default`.

<details>
<summary>The full story (Campaign 2 Phase II, manifests/apf-insulation.yaml)</summary>

- **Situation:** Upstream `examples/apf-insulation` separates controller traffic from bulk tenant traffic with custom `PriorityLevelConfiguration` and `FlowSchema` objects so a burst of `SandboxClaim` creates cannot starve `agent-sandbox-controller` status updates or leader election.
- **What goes wrong:** GKE's control-plane admission controller enforces a `30`-share budget across user-defined `PriorityLevelConfiguration`s to protect system priority levels. Applying vanilla high-share manifests either fails admission or distorts expected seat ratios; routing controller `coordination.k8s.io/leases` traffic into the controller's own work-queue lane can also delay leader-election renewals during `500`-sandbox reconcile bursts.
- **What we do:** In Phase II (`campaign-v2/manifests/`), we configured 3-lane APF insulation within GKE's exact guardrail: `agent-sandbox-critical` (`nominalConcurrencyShares: 20`, `lendablePercent: 0`, `queueLengthLimit: 250`) for `agent-sandbox-controller` CRD/Pod reconciles, `agent-sandbox-bulk` (`nominalConcurrencyShares: 10`, `queueLengthLimit: 250`) for `asrl-driver` client traffic, and a higher-precedence `FlowSchema` routing controller `coordination.k8s.io/leases` renewals straight to `global-default`. Result: `0` controller restarts and `0` task errors across all `106,544` Phase II rollouts.
- **Related:** [GKE-1](#gke-1), [GKE-2](#gke-2), [PERF-21](field-guide/perf.md#perf-21).

</details>

<a id="gke-4"></a>

## GKE-4 `DaemonSetController` checks node taints before GKE's `RuntimeClass` webhook injects `gvisor` tolerations

**Takeaway:** Setting `runtimeClassName: gvisor` on a `DaemonSet` pod template is not enough to schedule onto GKE Sandbox nodes. Add an explicit `sandbox.gke.io/runtime=gvisor:NoSchedule` toleration on the `DaemonSet` pod spec, because `DaemonSetController` filters tainted nodes before creating Pods.

<details>
<summary>The full story (Campaign 2 prepull, harness/run_daemonset_prepull.py)</summary>

- **Situation:** GKE Sandbox (`--sandbox=type=gvisor`) taints every node in the pool with `sandbox.gke.io/runtime=gvisor:NoSchedule`. When you create a standalone `Pod` (or a `Sandbox` controller creates one) with `runtimeClassName: gvisor`, Kubernetes's `RuntimeClass` admission plugin automatically appends the matching `NoSchedule` toleration onto the `Pod`.
- **What goes wrong:** `DaemonSetController` does not create a `Pod` and wait for the scheduler; it simulates scheduling in-controller against `daemonset.spec.template.spec.tolerations` *before* any `Pod` object hits admission. Because the template itself lacks the `sandbox.gke.io/runtime=gvisor` toleration prior to Pod admission, the `DaemonSet` reports `desiredNumberScheduled: 0` and silently pulls `0` images across all `10` gVisor nodes.
- **What we do:** `harness/run_daemonset_prepull.py` explicitly injects `{"key": "sandbox.gke.io/runtime", "operator": "Equal", "value": "gvisor", "effect": "NoSchedule"}` whenever `runtime_class == "gvisor"` (lines 36-43), hydrating all `500` SWE-bench images across all `10` nodes (`20` chunked DaemonSets, `10/10` nodes per DaemonSet).
- **Example:**
  ```python
  if a.runtime_class == "gvisor":
      # DaemonSetController evaluates node taints before Pod RuntimeClass admission runs.
      tolerations = [{"key": "sandbox.gke.io/runtime", "operator": "Equal",
                      "value": "gvisor", "effect": "NoSchedule"}]
  ```
- **Related:** [BENCH-6](#bench-6), [GKE-5](#gke-5).

</details>

<a id="gke-5"></a>

## GKE-5 GCFS image streaming (`T0`) still needs 100%-node hydration for `500` distinct images; Secondary Boot Disks (`T1`) eliminate network pulls completely

**Takeaway:** GKE Image Streaming (GCFS) streams data layers on demand, but cold-starting `500` distinct multi-GB repository images simultaneously still bottlenecks on registry metadata and remote block fetches unless cached across all nodes. Preloading a GCE disk image into `secondaryBootDisks` (`CONTAINER_IMAGE_CACHE`) gives every node local SSD block access from boot (`370 ms` median `exec`, `32.74 tasks/s`).

<details>
<summary>The full story (Campaign 2 Phase I vs Phase III, 11_05_2026_RESULTS.md)</summary>

- **Situation:** SWE-bench Verified and R2E-Gym use `500` distinct multi-gigabyte container images per sweep (one repository environment per problem), unlike synthetic benchmarks that reuse a single `alpine` or `python:slim` image across `1,000` pods.
- **What goes wrong:** Without full-fleet hydration on `T0` (GCFS streaming, no Secondary Boot Disk), tasks landing on a node that has not yet cached image `i` pay multi-second remote streaming latency and Artifact Registry QPS contention, inflating tail `claim` and `process` (`exec` first-page-fault) times.
- **What we do:** On `T0` (`Phase I`), we hydrated all `500` `@sha256` digests across all `10` `e2-standard-32` nodes up front ([BENCH-6](#bench-6)), achieving `87.40 s` (`34.33 tasks/s`) on `naive` `batch64`. On `T1` (`Phase III`, `10 x n2-standard-32` with a `600 GB` preloaded GCE Secondary Boot Disk in `CONTAINER_IMAGE_CACHE` mode), containerd mounted pre-unpacked layers directly from the attached SSD with zero prepull step, hitting `91.62 s` (`32.74 tasks/s`, `process p50 = 0.37 s`) on `3,000` `pipelined` tasks and `248.84 s` on `3,000` real-work `git + 30 s` tasks (`8.91x` faster than the prior `2,218.10 s` `T1` record `#1`, over a `180 s` pure-sleep lower bound).
- **Related:** [GKE-4](#gke-4), [GKE-6](#gke-6), [BENCH-6](#bench-6).

</details>

<a id="gke-6"></a>

## GKE-6 Size worker node SSDs below Kubelet's `85%` `ImageGC` high-water mark when caching multi-hundred-image suites

**Takeaway:** Caching `500+` repository images on an undersized node boot disk crosses Kubelet's `ImageGCHighThresholdPercent` (`85%`), causing Kubelet to silently evict warmed images mid-run and re-trigger cold pulls. Keep disk usage safely under `60%`.

<details>
<summary>The full story (Campaign 2 Phase II, README.md Cluster Specification)</summary>

- **Situation:** Phase II ran both `500`-image `R2E-Gym` suites (`Bench 1A`, `Bench 1B`) and `500`-image `SWE-bench Verified` suites (`Bench 3A`, `Bench 3B`) on the same `10`-node `gvisor-pool`.
- **What goes wrong:** On a `600 GB` boot disk, holding both `500`-image sets plus active container writable layers pushes disk occupancy past Kubelet's default `85%` `imageGCHighThresholdPercent`. Kubelet then garbage-collects cached image layers in the background *during* timed RL steps, causing sudden `20-60 s` cold-pull spikes and disk I/O contention.
- **What we do:** We provisioned `gvisor-pool` with `1,500 GB pd-ssd` per node and `maxParallelImagePulls=5` in Phase II, keeping disk utilization below `55%` across all `1,000` cached images with `0` `ImageGC` evictions across `106,544` tasks.
- **Related:** [GKE-5](#gke-5), [BENCH-6](#bench-6).

</details>

<a id="gke-7"></a>

## GKE-7 `terminationGracePeriodSeconds: 30` creates a hidden 30-second `maxPods` and IPAM wall between waves

**Takeaway:** Sandbox containers running `sleep infinity` ignore `SIGTERM` until `terminationGracePeriodSeconds` expires (`30 s` by default). Set `terminationGracePeriodSeconds: 0` on disposable RL/eval sandboxes so deleted pods release node `maxPods` slots and pod IPs immediately.

<details>
<summary>The full story (Campaign 2 TemplateSpec, harness/bench_core.py: build_template_spec)</summary>

- **Situation:** Each RL rollout or SWE-bench task claims a sandbox whose main container runs an idle command (`sleep infinity` or similar) while the runner drives commands via `exec`. At wave or step end, `500-1,024` sandboxes are deleted to make room for the next step on a `10`-node pool (`maxPodsPerNode = 256`, `2,560` total pod slots).
- **What goes wrong:** PID 1 `sleep` has no `SIGTERM` handler, so Kubernetes waits the full default `30 s` grace period before sending `SIGKILL`. Across rapid waves, up to `1,500+` `Terminating` pods pile up on the nodes, exhausting `maxPodsPerNode`, CNI IPAM allocations, and Kubelet PLEG throughput while doing nothing.
- **What we do:** `bench_core.py: build_template_spec` wires `extra_pod_spec["terminationGracePeriodSeconds"] = 0` uniformly across `bench_core`, `run_step_bench`, and `run_claim_soak_bench`. Deleted sandbox pods transition to `SIGKILL` immediately, eliminating the `30 s` teardown tail (and accounting for a major share of the `1.39x-1.97x` baseline platform speedup between July `RESULTS.md` `#10-#12` and Campaign 2 `HEAD per_claim` `#17-#19`).
- **Related:** [BENCH-2](#bench-2), [BENCH-5](#bench-5).

</details>

<a id="gke-8"></a>

## GKE-8 Fresh warm-pool `SandboxBatch` claims (`87.4 s`) beat in-place gVisor `git restore` recycling (`429.2 s`) by `4.91x`

**Takeaway:** Inside gVisor, resetting a large repository worktree in place (`git restore --worktree --staged . && git clean -fdx`) is `4.91x` slower than throwing the pod away and adopting a fresh warm-pool pod via `SandboxBatch`. Measure reset cost under your target sandbox runtime before assuming pod reuse wins.

<details>
<summary>The full story (Campaign 2 Phase I #21 vs #24, 11_05_2026_RESULTS.md)</summary>

- **Situation:** `agent-sandbox-rl` offers `recycle=True` (`GitRestoreReset`) to reuse one claimed sandbox across `G=6` rollouts (`500` initial claims instead of `3,000`), avoiding `2,500` pod creations.
- **What goes wrong:** Under gVisor (`runsc`), filesystem metadata syscalls (`lstat`, `open`, `unlink` across tens of thousands of files in repos like `django`, `sympy`, or `scikit-learn`) traverse gVisor's userspace sentry/gofer boundary. Running `2,500` in-place `git restore + git clean` resets serialized behind gVisor filesystem metadata latency (`429.24 s` wall on `batch64` `#24`, with `159` resets exceeding the `30 s` `reset_timeout` and triggering quarantine replacements; `517.77 s` and `392` quarantines on `per_claim` `#20`).
- **What we do:** Adopting a pre-warmed `Sandbox` via `SandboxBatch` (`#21`) is a single Kubernetes metadata adoption (`138 ms` `1st Ready`, zero filesystem scan) while Kubelet prepares replacement warm pods asynchronously in the background — finishing all `3,000` isolated tasks in **`87.40 s`** (`4.91x` faster than `naive+recycle`, with stronger isolation because no state can leak across episodes). Even when `recycle=True` *is* required, `SandboxBatch` still cut `30 s` reset quarantines `2.47x` (`392 -> 159`) by eliminating client/apiserver CPU and socket contention during replacements.
- **Example:**
  ```text
  SWE-bench 3,000 on gVisor (T0):
    per_claim + recycle (git restore) -> 517.77 s (892 claims, 392 30s-timeout quarantines)
    batch64   + recycle (git restore) -> 429.24 s (659 claims, 159 30s-timeout quarantines)
    batch64   fresh warm claims       ->  87.40 s (3,000 fresh pods, 0 resets, 4.91x faster!)
  ```
- **Related:** [LEASE-32](field-guide/lease.md#lease-32).

</details>

<a id="gke-9"></a>

## GKE-9 Controller `--sandbox-warm-pool-max-refill-rate` sets a hard step floor (`c / refill_rate`) — and interacts subtly with streaming vs. bulk deletes

**Takeaway:** When synchronous RL steps consume `c` sandboxes per step and the controller caps warm-pool pod creation at `R = max-refill-rate`, steady-state step time cannot beat `c / R`. When `exec` has stragglers, per-claim `DELETE`s start refilling *during* the straggler tail, whereas end-of-step `DELETECOLLECTION` starts refilling at the barrier.

<details>
<summary>The full story (Campaign 2 Phase II Bench 1B Tunix GRPO, 11_05_2026_RESULTS.md §4)</summary>

- **Situation:** `Bench 1B` ran the wide Tunix GRPO rollout shape (`64` images x `G=16` = `1,024` concurrent sandboxes per synchronous RL step x `8` steps = `8,192` tasks) under `Config H` (`--sandbox-warm-pool-max-refill-rate=100`).
- **What goes wrong:** Refilling `1,024` pods at `100 pods/s` imposes an irreducible `10.24 s` controller pod-creation floor per step across Steps 1-7. On `Bench 1B`, `SandboxBatch` accelerated fresh Step 0 (`51.83 s -> 43.18 s`, `1.20x` faster), cut `process` exec latency `1.42x` (`9.95 s -> 7.00 s`), and cut post-run open sockets `3.88x` (`516 -> 133`), yet 8-step macro wall was `358.46 s` vs `315.45 s` (`per_claim`). Tracing step timelines showed why: in `per_claim`, fast tasks released individual claims immediately while straggler tasks were still finishing their `9.95 s` `exec`, giving the `100 pods/s` controller refiller a ~`5 s/step` head start before the step barrier closed!
- **What we do:** Recognize when a workload is controller-refill-bounded (`c / refill_rate`) vs. apiserver-claim-bounded (`Bench 1A` `500` pools, where `SandboxBatch` won `3.82x` wall: `1,494.54 s -> 390.77 s`). When running wide synchronous steps under a tight controller refill cap, releasing individual finished members early via `batch.release_member(m)` overlaps warm-pool refill with straggler `exec` time while keeping the single shared watch!
- **Related:** [OPT-1](optimizations.md#opt-1), [OPT-2](optimizations.md#opt-2).

</details>

<a id="gke-10"></a>

## GKE-10 Watch fan-out (`N` watches), not `CREATE` QPS, is what blows up `claim p99` on stock GKE

**Takeaway:** Opening `500` per-claim watches (`520-650` client TCP/TLS sockets) stalls `kube-apiserver` watch dispatch and inflates `claim p99` `7.24x` (`139.66 s` vs `19.30 s`) even when `0` HTTP `429` errors occur. Multiplexing onto `1` label-scoped watch fixes tail latency without touching cluster APF settings.

<details>
<summary>The full story (Campaign 2 Phase I #17 vs #21, brian-nguyen_20261105-073700.md)</summary>

- **Situation:** Phase I ran on `100%` stock GKE APF (`0` custom `FlowSchema`s) with `0` rejected requests (`apiserver_flowcontrol_rejected_requests_total = 0`) on both `per_claim` (`#17`) and `batch64` (`#21`).
- **What goes wrong:** Even with zero `429`s, `per_claim` held `520` concurrent open watch sockets (`650` on `pipelined`), forcing `kube-apiserver` and Python's event loop / thread pool to serialize and dispatch status transitions across hundreds of concurrent streams while competing with `500` `exec` WebSockets. Median `claim p50` was `11.12 s`, but `claim p99` ballooned to **`139.66 s`** (`max = 273.31 s`).
- **What we do:** `SandboxBatch` (`#21`) kept `claim p50` at `11.09 s` while collapsing `claim p99` from **`139.66 s` to `19.30 s` (`7.24x` lower)** and `max` from **`273.31 s` to `32.30 s` (`8.46x` lower)**, cutting open sockets from `520` to `122` (`44` flat sockets on pure `200x5` warm bursts vs `488` = `11.1x` fewer).
- **Related:** [OPT-1](optimizations.md#opt-1), [WATCH-1](field-guide/watch.md#watch-1), [PERF-21](field-guide/perf.md#perf-21).

</details>

<a id="gke-11"></a>

## GKE-11 Reserve `minPortsPerVm >= 4096` on Cloud NAT for high-density (`maxPods=256`) private GKE node pools

**Takeaway:** Private GKE nodes default to `64` Cloud NAT source ports per VM, which silently drops outbound connections during `256`-pod/node image pulls or external calls. Provision Cloud NAT with `minPortsPerVm=4096` and dynamic port allocation before running high-density sandbox pools.

<details>
<summary>The full story (Campaign 2 cluster provisioning, README.md Cluster Specification)</summary>

- **Situation:** Production and benchmark GKE sandbox clusters run private worker nodes (`--enable-private-nodes`, `maxPodsPerNode=256`) behind Cloud Router + Cloud NAT.
- **What goes wrong:** Cloud NAT's default `64` ephemeral ports per VM is quickly exhausted when `10` high-density nodes run concurrent GCFS/Artifact Registry connections or outbound probes, surfacing as mysterious `TLS handshake timeout` or `connection reset` flakes.
- **What we do:** Configure Cloud NAT (`asrl-nat-config`) with `--min-ports-per-vm=4096` (and tear down both Cloud NAT and Cloud Router explicitly on cluster deletion so no hourly NAT gateway charges linger).
- **Related:** [GKE-5](#gke-5).

</details>

<a id="gke-12"></a>

## GKE-12 Isolate the controller (`ctrl-pool`), benchmark driver (`default-pool`), and sandboxes (`gvisor-pool`) onto separate node pools

**Takeaway:** Never schedule `agent-sandbox-controller` or the benchmark driver on the `gvisor-pool` being hammered by `500-1,024` container startups. Taint `ctrl-pool` (`dedicated=ctrl:NoSchedule`) and pin the driver (`bench-role=driver`) to their own pools so CPU, disk I/O, and Kubelet PLEG spikes on sandbox nodes never perturb controller reconcile loops or client timing.

<details>
<summary>The full story (Campaign 2 topology, PREFLIGHT_VERIFICATION.md #2 & run_job_v2.sh)</summary>

- **Situation:** Starting and tearing down `1,000` gVisor sandboxes drives heavy containerd unpack, `runsc` boot, and Kubelet PLEG churn on worker nodes.
- **What goes wrong:** If `agent-sandbox-controller` or the Python driver pod shares nodes with `gvisor-pool`, kernel CPU scheduling delays and disk I/O stalls inflate both controller reconciliation latency and client-side clock measurements (`time.monotonic()` wakeups and Lease renewals).
- **What we do:** Campaign 2 used a strict 3-pool layout: `ctrl-pool` (`2 x e2-standard-32`, tainted `dedicated=ctrl:NoSchedule`) for the `2` HA controller replicas, `default-pool` (`2 x n2-standard-16`, labeled `bench-role=driver`) for the in-cluster driver `Job`, and `gvisor-pool` (`10` x `32`-vCPU nodes) exclusively for sandbox workloads.
- **Related:** [GKE-1](#gke-1), [BENCH-4](#bench-4).

</details>

<a id="gke-13"></a>

## GKE-13 Use `501–1,000` node bucket (`go/kcp-resize`) and `pd-standard` OS disks to preserve regional `SSD_TOTAL_GB` quota for Secondary Boot Disks

**Takeaway:** GKE regional master VM sizing (`go/kcp-resize`) scales in discrete node buckets. Creating an initial temporary Spot warmup pool above `500` total nodes (`170` nodes/zone × `3` zones = `510` nodes) sizes regional KCP to `gomaxprocs = 16` (`1,449` nominal APF seats) immediately on birth. Using `--disk-type=pd-standard` for temporary warmup pools and worker OS boot disks consumes `0 GB` of `SSD_TOTAL_GB`, reserving the regional `40,960 GB` SSD quota exclusively for `50 × 600 GB = 30,000 GB` Secondary Boot Disks.

<details>
<summary>The full story (Campaign 3 5-Cluster Fleet, provision_multicluster_c3.sh & provision_all5_c3_rerun.sh)</summary>

- **Situation:** Multi-cluster testing across `5` regional GKE clusters (`asrl-c3-1..5`) required `50 × n2-standard-32` worker nodes backed by `600 GB` `swebench-500-preload-600g` Secondary Boot Disks (`30,000 GB` total `pd-ssd`).
- **What goes wrong:** If temporary master-warmup Spot pools or worker OS boot disks use `pd-balanced`, GCP charges them against regional `SSD_TOTAL_GB` (`40,960 GB` limit in `us-central1`), causing `gvisor-pool` creation to fail with `Quota SSD_TOTAL_GB exceeded`.
- **What we do:** In `provision_multicluster_c3.sh` and `provision_all5_c3_rerun.sh` (`--num-nodes=170` × `3` zones = `510` nodes), the temporary Spot warmup pool used `--disk-type=pd-standard --disk-size=30` (and `gvisor-pool`, `ctrl-pool`, and `driver-pool` primary OS boot disks used `100 GB pd-standard`), sizing `kube-apiserver` to `go_sched_gomaxprocs_threads = 16` on all `5` clusters while consuming `0 GB` of `SSD_TOTAL_GB` for OS boot disks.
- **Related:** [GKE-1](#gke-1).

</details>

<a id="gke-14"></a>

## GKE-14 GCE Secondary Boot Disk images hydrate blocks lazily from GCS on first read — recreate SBD nodepools per arm for cold-SBD tests, and account for `wp=1` streaming deletes vs wave-boundary `DELETE_COLLECTION`

**Takeaway:** Attaching a custom GCE disk image as a `CONTAINER_IMAGE_CACHE` Secondary Boot Disk pays lazy GCS block-hydration page faults (`wait_pool_ready avg = 63.98s–67.44s`) on the first run after node creation before dropping `>20×` (`3.68s–4.11s`) once hydrated. Never compare a first-boot run against a second run on the same SBD pool; recreate the SBD nodepool from scratch before each arm for cold-SBD comparisons. On `wp=1` `naive` waves when 3-lane APF + `gomaxprocs=16` eliminate HTTP `429`s, `per_claim` finishes `1.09×` faster on symmetric cold SBD (`492.52s` vs `539.18s`) and `1.17×` faster on symmetric hydrated SBD (`124.73s` vs `146.22s`) because streaming individual `DELETE`s start controller refill before wave stragglers finish, while `batch64` cuts open sockets `4.51×–4.68×` (`559 → 124` and `524 → 112`).

<details>
<summary>The full story (Campaign 3 C3-1a Symmetric Cold & Hydrated SBD, RESULTS.md #39–#42)</summary>

- **Situation:** `C3-1a` ran `500 × 6 = 3,000` SWE-bench tasks (`naive`, `probe='true'`) on `10 × n2-standard-32` nodes with `600 GB` `swebench-500-preload-600g` SBD.
- **What goes wrong:** In an initial sequential diagnostic (`tgs=-1`), Run 1 (`per_claim`, `wall = 545.64s`, `wait_pool_ready avg = 68.05s`) pulled SBD blocks lazily from GCS while Run 2 (`batch64`, `wall = 308.59s`, `wait_pool_ready avg = 12.72s`) inherited partially hydrated blocks — the same sequential order that inflated Campaign 2 `#31` (`281.65s`) vs `#32` (`139.10s`).
- **What we do:** First, `run_c3_1a_symmetric_cold_sbd.sh` deleted and recreated a brand-new `10 × n2-standard-32 + 600 GB SBD` `gvisor-pool` before **each** arm (`tgs=0, cow=True`, non-SWE-bench smoke gate): `#39` `per_claim` (`492.52s`, `wpr avg = 63.98s`, `559` sockets) vs `#40` `batch64` (`539.18s`, `wpr avg = 67.44s`, `124` sockets). Second, running both arms back-to-back after full SBD hydration (`#41` `per_claim` `124.73s` vs `#42` `batch64` `146.22s`) reproduced Campaign 2 `#32` (`139.10s`) within `5.1%` and confirmed the `wp=1` streaming-delete vs wave-boundary `DELETE_COLLECTION` tradeoff across both cache states.
- **Related:** [GKE-4](#gke-4).

</details>

<a id="gke-15"></a>

## GKE-15 Do not set `resources.requests["ephemeral-storage"]` when `100 GB` COS primary OS boot disks host kubelet overhead and SBD hosts container images

**Takeaway:** Specifying `resources.requests["ephemeral-storage"]: 4Gi` on guest sandboxes when primary COS OS boot disks are `100 GB pd-standard` (`~47 GiB` allocatable `ephemeral-storage` after GKE system/image-gc/eviction reservations) caps scheduling at `10` guest pods/node (`100` pods across a `10`-node cluster). Keep `emptyDir.sizeLimit: 4Gi` on `/tmp` without setting `requests["ephemeral-storage"]`, and verify `--node-selector` matches the actual pool label (`cloud.google.com/gke-nodepool=gvisor-pool`).

<details>
<summary>The full story (Campaign 3 C3-3d warm-pool scheduling failures #4 and #8)</summary>

- **Situation:** `C3-3d` stood up `1,000` warm gVisor SWE-bench pods per cluster (`100` pods/node) across `5` clusters with `100 GB pd-standard` primary boot disks + `600 GB` SBD image caches.
- **What goes wrong:** Requesting `ephemeral-storage: 4Gi` caused `kube-scheduler` to stall warm pools at exact `100/1000` ready replicas per cluster (`Insufficient ephemeral-storage`); later passing `--node-selector bench-role=worker` (absent on `gvisor-pool`) left `0/1000` pods scheduled for `600s`.
- **What we do:** Omit `resources.requests["ephemeral-storage"]` (retaining `volumes.emptyDir.sizeLimit: 4Gi`) and pass `--node-selector cloud.google.com/gke-nodepool=gvisor-pool`. All `5,000` warm gVisor pods (`1,000/cluster` across `5` clusters) scheduled and reached `Ready` in `10.81s`.
- **Related:** [GKE-5](#gke-5).

</details>

<a id="gke-16"></a>

## GKE-16 Provision multiple Cloud NAT external IPs, extend `aiohttp` `keepalive_timeout` (`900s`), retry cold TCP/TLS handshakes, and measure both `release_api_s` and `SandboxClaim`-filtered `pods_gone_s` in multi-cluster drivers

**Takeaway:** When an in-cluster driver on private nodes (`asrl-c3-1`) drives remote GKE clusters (`asrl-c3-2..5`) via their external control-plane VIPs, `aiohttp`'s default `15.0s` `keepalive_timeout` closes pooled TLS sockets during `20–30s` warm-pool refills, and `per_claim` watches close response sockets on `Watch.stop()`. Re-opening hundreds of sockets through **1** Cloud NAT IP triggers external L4 load-balancer SYN drops or resets (`31s–122s` TCP SYN retransmit stalls and `ClientConnectorError`s because `aiohttp` does not retry failed TCP `connect()`s). Attach `8` static external IPs to Cloud NAT, set `_keepalive_timeout = 900.0` + `5ms`-paced idempotent `_create_direct_connection` retries (`6.0s` per attempt, picking a fresh ephemeral source port before any HTTP bytes are sent), and track `pods_gone_s` alongside `release_api_s` by filtering strictly to `SandboxClaim`-adopted sandboxes (never unclaimed `SandboxWarmPool`-owned sandboxes).

<details>
<summary>The full story (Campaign 3 C3-3d root cause & verified 5,000-sandbox rerun)</summary>

- **Situation:** `C3-3d` drove `1,000`, `2,500`, and `5,000` simultaneous claims and bulk releases across `5` regional GKE clusters from one Python `asyncio` loop on `asrl-c3-1`.
- **What goes wrong:** Launches #5–#7 suffered `31.3s` and `122.3s` stalls before crashing with `aiohttp` `TimeoutError` because all `~320` idle `kubernetes_asyncio` sockets expired after `15s` during pool refills and burst out of a single Cloud NAT IP; even with `8` NAT IPs, a single dropped SYN or L4 LB reset during `per_claim`'s `O(N)` watch reconnects raises `ClientConnectorError` (`aiohttp` does not retry failed TCP connects, and `AsyncSandboxClient.delete_sandbox` logs and swallows the error). In addition, measuring only `release()` API return overstates real pod cleanup speed, while naively polling `ownerReferences.kind == "Sandbox"` accidentally waits on unclaimed warm-pool pods (which are also owned by `Sandbox` CRs whose owner is `SandboxWarmPool`).
- **What we do:** Attach `8` static regional external IPs (`asrl-c3-nat-ip-1..8`, `--max-ports-per-vm=32768`, `--min-ports-per-vm=4096`) to `asrl-c3-nat`, set `conn._keepalive_timeout = 900.0`, `ClientTimeout(total=300, connect=30, sock_connect=10, sock_read=180)`, and retry `_create_direct_connection` up to `5` times (`6.0s` timeout + `_SynPacer(0.005)`, 100% idempotent since zero HTTP bytes have been sent), and snapshot claimed pod names strictly from `SandboxClaim.status.sandbox.name` / `Sandbox.ownerReferences[kind=="SandboxClaim"]`. Across all `6` `SandboxBatch` bursts (`17,000 / 17,000` claims), `batch` kept its `337 → 342` warm keep-alive pool intact with **`0` errors**, whereas `per_claim`'s `Watch.stop()` socket closes drained its pool from `336 → 6` sockets and still incurred `5/2,500` and `12–13/5,000` external GKE VIP `ConnectionTimeoutError`s (because GCP Cloud NAT `ENDPOINT_TYPE_VM` hashes `(driver VM IP, dest VIP, 443)` to one NAT IP per target cluster). At `n = 5,000`, `batch.release()` returned in `0.97s` (`5,176.6/s`, with `4,623/5,000` pods still alive at API return) vs `14.45s` on concurrent `terminate()` and `30.29s` on `delete_all()`, while full deletion of all `5,000` claimed gVisor pods (`pods_gone_s`) converged in `10.05s` (`497.8 pods/s`) on `batch`, `15.24s` (`327.2 pods/s`) on concurrent `terminate()`, and `31.36s` (`159.1 pods/s`) on `delete_all()`.
- **Related:** [GKE-11](#gke-11).

</details>

<a id="gke-17"></a>

## GKE-17 Distinguish all five tiers of the GKE sandbox "warmness spectrum" so cache state never confounds A/B benchmarks

**Takeaway:** "Warm" vs. "cold" is not binary on GKE: container sandboxes sit on a five-tier spectrum (**Pod/RAM-warm**, **OS page-cache-warm**, **disk-warm / hydrated SSD**, **cold SBD with lazy GCS block hydration**, and **network-cold / uncached GCFS streaming**). Always pin and verify which tier both A/B arms start in, because sliding silently up one tier between Run 1 and Run 2 creates `2x-20x` phantom speedups.

<details>
<summary>The full story (Campaigns 2-5 across WarmPool, GCFS T0, and Secondary Boot Disks)</summary>

- **Situation:** Across `500`-image SWE-bench and R2E-Gym benchmarks, two runs with the exact same YAML manifests can differ by an order of magnitude depending on where image layers and container processes sit in the node storage/memory hierarchy:
  1. **Pod / RAM-warm (`SandboxWarmPool` Ready replica):** The pod is already scheduled, the `gVisor` (`runsc`) sentry and container entrypoint have already booted, and hot binary/filesystem pages sit in RAM. Claiming only updates Kubernetes ownership metadata (`< 1 s`, zero kubelet/container startup).
  2. **Node OS page-cache-warm (recent container run on the same node):** A new pod still pays scheduling + `runsc` boot, *but* `containerd` overlayfs blocks already reside in the host kernel's RAM page cache, so the first `exec` (`git status` / `pytest`) hits zero disk or remote block faults.
  3. **Disk-warm / hydrated local SSD (`prepull` DaemonSet or post-hydration SBD):** Image layers are unpacked on the node's attached Persistent Disk / SSD (`wait_pool_ready ~3.7-4.1 s`), avoiding network pulls while remaining bounded by disk IOPS and Kubelet's `85%` `ImageGC` eviction mark ([GKE-6](#gke-6)).
  4. **Cold Secondary Boot Disk (first read on a fresh GCE `CONTAINER_IMAGE_CACHE` SBD):** Even though the disk is attached at VM creation, GCE streams underlying blocks lazily from GCS on first read (`wait_pool_ready ~64-67 s` on Run 1 vs `~3.7-4.1 s` on Run 2, [GKE-14](#gke-14)).
  5. **Network-cold (Artifact Registry pull or unhydrated `T0` GCFS Image Streaming):** Layers are absent from the node; GCFS streams metadata and data chunks over the network on demand, contending on Cloud NAT ports and registry QPS across `500` distinct images ([GKE-5](#gke-5), [GKE-11](#gke-11)).
- **What we do:** Match warmness tiers explicitly per benchmark goal: use standing `SandboxWarmPool`s with verified `readyReplicas` before timing Pod/RAM-warm bursts; run chunked `100%`-node hydration `DaemonSet`s ([BENCH-6](#bench-6)) before disk-warm `T0` runs; recreate SBD nodepools from scratch before *each* arm for cold-SBD comparisons ([GKE-14](#gke-14)); and interleave `ABABAB` repetitions once disk/page caches are symmetrically warm.
- **Related:** [GKE-5](#gke-5), [GKE-6](#gke-6), [GKE-14](#gke-14), [BENCH-2](#bench-2), [BENCH-6](#bench-6).

</details>

<a id="gke-18"></a>

## GKE-18 Control-plane sizing sets `kube-controller-manager` cascading GC rate (`~40 req/s` / `3 req/object = 13.3 objects/s`), turning cluster GC throughput into a `4.9x` hidden variable

**Takeaway:** Deleting a `SandboxClaim` frees node memory and pod IPs only after `kube-controller-manager`'s `GarbageCollectorController` cascades the delete across `SandboxClaim -> Sandbox -> Pod` (`3` API requests per dependent, `6` per claim chain). On smaller regional control planes (`324` APF seats), GC traffic pinned flat at `~40 req/s` (`13.3 objects/s`, `~6.7 claims/s`), whereas a larger control plane (`483` APF seats) ran GC at `100-209 req/s`. Always inspect GC throughput alongside `apiserver` seat counts ([GKE-1](#gke-1)), or bypass cascading GC by deleting dependents directly in groups ([OPT-31](optimizations.md#opt-31)).

<details>
<summary>The full story (Campaign 5 GC Forensics across three GKE clusters, RL_BATCH_FINAL_REPORT.md §3)</summary>

- **Situation:** Before grouped direct deletion (`OPT-31`) landed, multi-step R2E `500x2x8` stalled at `984-1,010 s` on two 4-day-old clusters (`10`-node and `20`-node, same GKE `1.37.1-gke.2097000` version and identical controller image), while grouped direct deletion cut batch R2E wall to `207 s` vs `1,015 s` on `per_claim` (`4.9x`). Yet running the exact same comparison on a newly provisioned `10`-node cluster measured `239 s` vs `239 s` (`1.00x`).
- **What goes wrong:** Read-only `/metrics` time-series forensics across all three clusters ruled out etcd size and event count (the fast cluster held `2.4x` the events and `2.3x` the etcd bytes), webhooks, APF queue rejections (`~0 ms` APF queue wait), and kubelet termination lag (`301` of `302` leftover pods sat `Running` with `deletionTimestamp == None` and missing owner `Sandbox`es). Instead, `kube-system-service-accounts` traffic on the apiserver replica serving `GarbageCollectorController` sat flat at **`40.3-40.8 req/s`** on both `324`-seat clusters regardless of `9-22 ms` server latency (inferred client-side KCM `--kube-api-qps` cap; Cloud Logging was disabled on the project so KCM flags could not be read directly), versus **`100-209 req/s`** on the `483`-seat fresh cluster (born with a large Spot warm-up pool per [GKE-13](#gke-13)). Because cascading GC issues `GET` dependent + `GET` owner (`404`) + `DELETE` (`3` requests/dependent), `40 req/s` removes `13.3 objects/s` (measured: `13.2 Sandbox deletes/s`), backing up hundreds of dead `Running` pods (`252,675` `Insufficient memory` scheduler events) behind every `500-1,000`-claim step!
- **What we do:** Check both total APF seats (`324` vs `483`) and `kube-system-service-accounts` request rates before comparing multi-step benchmarks across clusters, provision fresh benchmark clusters with the [GKE-13](#gke-13) Spot warm-up pool so KCM scales with the master tier, and delete released `Sandbox` and `Pod` dependents directly in `50`-UID `deletecollection` batches (`OPT-31`) so batch workloads stay fast even on `40 req/s`-capped control planes.
- **Related:** [GKE-1](#gke-1), [GKE-13](#gke-13), [OPT-31](optimizations.md#opt-31), [PERF-26](field-guide/perf.md#perf-26).

</details>

<a id="gke-19"></a>

## GKE-19 Churning `1,000` `terminationGracePeriodSeconds: 0` gVisor pods per step across `500` uncached GCFS-streamed images wedges `containerd`/`runsc` and exhausts node pod IPs

**Takeaway:** Never use a high-churn multi-step benchmark (`1,000` pods/step, `terminationGracePeriodSeconds: 0`) as a "warm-up run" on fresh or recycled gVisor nodes whose GCFS image cache is cold. Killing hundreds of gVisor sandboxes while their image layers are still streaming triggers `containerd` `KillPodSandboxError: DeadlineExceeded` storms (`475,000+` `FailedKillPod` events), leaking sandbox network namespaces until CNI `ptp` exhausts every `/23` node pod CIDR (`no IP addresses available in range set`), even under unmodified `per_claim` alone!

<details>
<summary>The full story (Campaign 5 fresh-cluster recycle & `per_claim`-only reproduction, RL_BATCH_FINAL_REPORT.md §0)</summary>

- **Situation:** After recycling the `10` `gvisor-pool` nodes on a fresh cluster (`maxPodsPerNode=256`, `/23` pod CIDR = `510` IPs/node, GCFS image streaming enabled), we ran one untimed upstream `per_claim` R2E `500x2x8` pass (`--termination-grace-seconds 0`, zero batch code, zero client direct deletes) to warm node caches before timed rounds.
- **What goes wrong:** Step walls degraded monotonically (`96.7 -> 169.3 -> 188.2 -> 207.9 s`, reaching `222-267 s` on an earlier uncached pass vs `26-33 s` on hydrated nodes). Within `10` minutes of `per_claim` starting, all `10` fresh nodes logged `1,123` new `FailedKillPod` event objects (`2,567` occurrences; `560,071` occurrences across the earlier uncached session): `containerd` timed out stopping `runsc` sandboxes (`KillContainerError` / `KillPodSandboxError: context deadline exceeded`) when rapid wave teardown overlapped active GCFS remote layer fetches. Once leaked sandboxes accumulated, CNI `ptp` failed new pod creations with `no IP addresses available in range set: 10.45.14.1-10.45.15.254` while `kubectl top nodes` returned `<unknown>` on `4/10` wedged nodes.
- **What we do:** Hydrate all `500` images across `100%` of gVisor nodes *without* churn using chunked `DaemonSet`s (`25` images/pod, `max_parallel=5`, [BENCH-6](#bench-6)) before running any step benchmark, recycle wedged node pools (`resize 0 -> N`) if `FailedKillPod` or `no IP addresses` Warning events appear, and verify steady `25-35 s` baseline steps with `~0` `FailedKillPod` deltas before starting A/B rounds.
- **Related:** [GKE-5](#gke-5), [GKE-7](#gke-7), [GKE-17](#gke-17), [BENCH-6](#bench-6).

</details>

<a id="gke-20"></a>

## GKE-20 Exact-fit `1,000`-claim bursts against a `1,000`-replica `SandboxWarmPool` cold-launch tail claims when stale second reconciles hold candidates past the `2 s` grace cutoff

**Takeaway:** Firing `1,000` batch claims at `c=1,000` against an exact-fit `1,000`-replica warm pool cold-started `18-56` tail claims (`ranks 957-999`) in half of bursts while leaving `>=` that many original `Ready` warm sandboxes unclaimed. Two `sandboxclaim_controller.go` behaviors combine: stale second reconciles of already-bound claims pop and hold the last `Ready` candidates until a `409 Conflict` + fresh `GET` returns them, and waiting claims stop retrying once `age > DefaultWarmCandidateGracePeriod` (`2 s` from `creationTimestamp`). Either raise `--sandbox-claim-warm-candidate-grace-period=15s` on the controller or cap created-but-unbound client claims (`max_unbound_claims=256`, [OPT-42](optimizations.md#opt-42)).

<details>
<summary>The full story (Campaign 5 Cold-Launch Forensics & 4-Arm GKE Validation, cold-launch-cause-report.md)</summary>

- **Situation:** On a `1` image x `1,000` `Ready` warm-pool burst (`ASRL_COUNT_COLD=1`, counting `Sandbox`es owned by `SandboxClaim`s via `agents.x-k8s.io/launch-type`), `c=500` always achieved `0` cold launches, `per_claim` at `c=1,000` achieved `0` cold launches, and batch at `c=1,000` cold-launched `18-56` pods in `4/8` to `5/8` bursts (dropping those bursts from `206-248 claims/s` to `90-109 claims/s`).
- **What goes wrong (and why our first controller patch failed):**
  1. **Stale second reconcile holds a second candidate (verified in Go unit test + measured `~60` `adoption write conflict: claim X already assigned sandbox Y` logs per `1k` burst):** Before the claim informer reflects pass 1's status update, a second reconcile of the same claim pops *another* `Ready` sandbox from the in-memory candidate queue, hits `409 Conflict` updating the claim, re-reads the claim, sees it already bound, and re-queues the candidate. At `957..999` of `1,000`, those temporarily held keys empty the pool's tail!
  2. **`creationTimestamp`-based `2 s` grace cutoff (`warmCandidateRetryAfter`):** Our first 32-line controller patch counted in-flight popped candidates as pending so an empty queue with held keys requeued after `100 ms`. On GKE it failed completely (**`2/8` cold bursts before -> `5/12` cold bursts with patch**; its wait log fired in all `5` cold bursts and `0` of `7` clean bursts, only `15` times total)! Why? Because batch creates `1,000` claims in `~1-3 s` while the controller binds `~250/s`, many tail claims exceed `2 s` from `creationTimestamp` (`14/17` dumped cold claims fit the `2.0 s` expiry; `3/17` hit `pending == 0` on the unpatched controller and cold-started in `0.4-1.4 s`), so the controller's `2 s` grace check refused further retries and cold-created immediately!
- **What we do (4-arm GKE validation, `8` bursts/arm at `c=1,000`):**
  - Unchanged controller, no window: **`4/8` cold bursts** (`20, 30, 18, 54`), `206-248/s` clean / `90-109/s` cold.
  - Controller `--sandbox-claim-warm-candidate-grace-period=15s` (config only): **`0/8` cold bursts**, `212-271 claims/s`.
  - Client `BatchClaimConfig.max_unbound_claims=256` (`5 s` max-wait cap, unchanged `2 s` controller): **`0/8` cold bursts**, `201-258 claims/s` (keeps tail claims `< 2 s` old so they adopt warm/refill sandboxes instead of cold-starting; note `1-18` original replicas still swap with fast refills until the upstream controller decouples held-candidate retries from `creationTimestamp` age).
- **Related:** [OPT-42](optimizations.md#opt-42), [LOCK-24](field-guide/lock.md#lock-24), [BENCH-11](#bench-11).

</details>

---

## Benchmark harness and measurement engineering

<a id="bench-1"></a>

## BENCH-1 Separate succeeded (`phases`) and failed (`phases_err`) latency distributions so fast failures never reward broken runs

**Takeaway:** A rejected HTTP `429` or `404` fails in `5 ms`, whereas adopting and booting a real sandbox takes `100 ms` to `10+ s`. Never merge failed calls into `claim p50/p95/p99`: compute latency percentiles strictly over `ok=True` tasks and report error counts and error timings in a separate `phases_err` table.

<details>
<summary>The full story (Campaign 2 harness, bench_core.py & PREFLIGHT_VERIFICATION.md #7)</summary>

- **Situation:** Under overload, a naive timer around `try: acquire() ... finally: record(dt)` records every `5 ms` `ApiException(429)` alongside real `10 s` claim adoptions in the same array.
- **What goes wrong:** An arm that fails `30%` of its claims at `5 ms` suddenly reports a *lower* `claim p50` and `claim avg` than a healthy `100%`-success arm — a classic survivorship/fast-fail distortion that makes broken configurations look faster.
- **What we do:** `agent-sandbox-rl`'s `RunReport` and `bench_core.py` record `phases` strictly over `ok=True` completions (`min/avg/p50/p90/p95/p99/max`) and isolate failed operations in `phases_err`, pinned by a Docker-build assertion (`PREFLIGHT_VERIFICATION.md` #7) and `0` task errors across all `36` Campaign 2 runs.
- **Related:** [AGENT-2](process.md#agent-2).

</details>

<a id="bench-2"></a>

## BENCH-2 Run a matched `HEAD` baseline on the same cluster to factor code wins from platform drift

**Takeaway:** When comparing against numbers published months ago, always run the unmodified baseline (`HEAD per_claim`) back-to-back on the exact same cluster. That decomposes the cumulative ratio (`6.72x`) into platform/upstream gains (`1.46x`) times your isolated architectural speedup (`4.62x`).

<details>
<summary>The full story (ai-on-gke/sandbox-rl-performance PR #5, 11_05_2026_RESULTS.md §2)</summary>

- **Situation:** July 2026 `RESULTS.md` row `#10` recorded `587.7 s` (`0.1959 s/task`) for SWE-bench `3,000` `naive` on GKE `v1.36.0` (`main-af8a790`). Our November `SandboxBatch` `#21` finished in `87.40 s` (`0.0291 s/task`) — `6.72x` faster than `#10`.
- **What goes wrong:** Claiming a `6.72x` speedup from `SandboxBatch` alone would be scientifically wrong, because 3+ months of upstream controller commits, GKE `v1.37.1`, `terminationGracePeriodSeconds: 0` ([GKE-7](#gke-7)), and 100%-node DaemonSet hydration ([BENCH-6](#bench-6)) also improved the baseline.
- **What we do:** Running `HEAD per_claim` (`#17`: `403.63 s`) and `SandboxBatch` (`#21`: `87.40 s`) back-to-back on identical cluster state let `11_05_2026_RESULTS.md` §2 factor the gain cleanly: **`6.72x` total = `1.46x` (platform + upstream controller) x `4.62x` (pure `SandboxBatch`)**. Every suite in Campaign 2 paired matched `per_claim` and `batch` arms on the same cluster.
- **Related:** [AGENT-2](process.md#agent-2), [GKE-7](#gke-7).

</details>

<a id="bench-3"></a>

## BENCH-3 Sample open sockets, FDs, threads, and kernel `VmHWM` peak RAM from `/proc/self` inside the runner

**Takeaway:** External `kubectl top pod` scrapes lag by `15-60 s` and miss sub-second burst peaks completely. Read `/proc/self/fd` (`socket:[...]`) and `/proc/self/status` (`VmRSS`, `VmHWM`, `Threads`) directly in Python at exact phase boundaries for zero-overhead, kernel-exact socket and peak-memory telemetry.

<details>
<summary>The full story (harness/bench_core.py: proc_stats)</summary>

- **Situation:** A `1,000`-pod warm-pool burst (`Bench 2A`) finishes in `3-6 s`. We needed exact client socket counts (`488` vs `44`), thread counts (`3,017` vs `1,857`), and peak RSS (`VmHWM`) while all `1,000` sandboxes were held simultaneously.
- **What goes wrong:** `tracemalloc` misses C/OpenSSL/urllib3 allocations and slows execution, while `kubectl top` (cAdvisor) averages over slow scrape windows and cannot count socket FDs.
- **What we do:** `bench_core.py: proc_stats()` walks `/proc/self/fd` (`os.readlink` starting with `"socket:["`) and parses `/proc/self/status` (`VmRSS:`, `VmHWM:`, `Threads:`) in `<1 ms`. Notice `VmHWM` (high-water mark): the Linux kernel tracks the true all-time peak RSS of the process between samples even if a `70 MB` spike ([OPT-4](optimizations.md#opt-4)) was freed milliseconds later!
- **Example:**
  ```python
  target = os.readlink(f"/proc/self/fd/{fd}")
  if target.startswith("socket:["):
      open_sockets += 1
  # /proc/self/status gives kernel-tracked VmRSS, VmHWM (true peak!), and Threads
  ```
- **Related:** [OPT-4](optimizations.md#opt-4), [GKE-10](#gke-10).

</details>

<a id="bench-4"></a>

## BENCH-4 Bracket every benchmark Job with `/metrics` APF counters and controller `restartCount` snapshots

**Takeaway:** A benchmark run can return exit code `0` even if `agent-sandbox-controller` OOM-restarted mid-run or `kube-apiserver` rejected `500` requests with `429`. Snapshot APF metrics and controller pod `restartCount` immediately before and after every run, and store the delta alongside the result JSON.

<details>
<summary>The full story (harness/run_job_v2.sh lines 70-204)</summary>

- **Situation:** Client retries (`SandboxBatch` and `agent-sandbox-rl`) are designed to mask transient `429`s and controller blips so tasks still succeed (`ok=True`).
- **What goes wrong:** Because retries hide server errors from the exit code, an unmonitored control plane can suffer silent APF queue drops or a controller container restart (`restartCount` `0 -> 1`) that adds `20 s` of leader-election stall to whichever arm happened to be running.
- **What we do:** `run_job_v2.sh` scrapes `apiserver_flowcontrol_rejected_requests_total`, `apiserver_flowcontrol_dispatched_requests_total`, `apiserver_flowcontrol_current_inqueue_requests`, and `go_sched_gomaxprocs_threads` from `kubectl get --raw /metrics` plus `kubectl -n agent-sandbox-system get pods -o json` before and after every Job, writing `controller_restarts_before` / `after` into `${RUN_NAME}.meta.json` (`0 -> 0` verified across all `36` Campaign 2 runs).
- **Related:** [GKE-1](#gke-1), [GKE-2](#gke-2), [TEST-26](testing.md#test-26).

</details>

<a id="bench-5"></a>

## BENCH-5 Enforce a 7-resource zero-drain barrier plus a 30-second settle window between runs

**Takeaway:** Never start Run `N+1` while Run `N` still has `Terminating` pods, lingering `SandboxWarmPool`s, or uncollected batch `Lease`s. Poll all seven workload resource kinds down to `0`, escalate cleanup only if stuck, and sleep `30 s` so apiserver watch caches, etcd compaction, and node PLEG settle completely.

<details>
<summary>The full story (harness/run_job_v2.sh lines 33-68)</summary>

- **Situation:** Back-to-back A/B sweeps (`per_claim` followed by `batch`, or vice versa) share one GKE cluster and namespace.
- **What goes wrong:** Even after `fleet.teardown()` returns, Kubernetes garbage collection of dependent `Sandbox` and `Pod` objects is asynchronous ([K8S-3](field-guide/k8s.md#k8s-3)). Starting the next run `2 s` later forces the second arm to compete with `500` dying gVisor pods for node CPU, CNI IPAM, and controller workqueue workers.
- **What we do:** `run_job_v2.sh` loops up to `90` checks verifying `pods == 0`, `sandboxclaims == 0`, `sandboxwarmpools == 0`, `sandboxes == 0`, `sandboxtemplates == 0`, `daemonsets == 0`, and `leases (-l agents.x-k8s.io/batch-id) == 0`, then sleeps `30 s` before snapshotting baseline metrics and launching the next Job.
- **Related:** [GKE-7](#gke-7), [K8S-3](field-guide/k8s.md#k8s-3).

</details>

<a id="bench-6"></a>

## BENCH-6 Hydrate 100% of worker nodes with chunked `DaemonSet`s (`25` images/pod, `5` parallel) instead of a "warm-up run"

**Takeaway:** A throwaway "warm-up run" schedules each image onto only `1` of `M` worker nodes, leaving an `(M-1)/M` (`90%` on `10` nodes!) cold-cache miss rate for the timed run. Use chunked `DaemonSet`s (`images_per_pod=25`, `max_parallel=5`) to guarantee every image digest is unpacked on `100%` of nodes before timing starts.

<details>
<summary>The full story (harness/run_daemonset_prepull.py & agent_sandbox_rl/prepull.py)</summary>

- **Situation:** We needed all `500` SWE-bench (and `500` R2E-Gym) `@sha256` images cached identically across all `10` `gvisor-pool` nodes so neither `per_claim` nor `batch` won by lucky node-cache placement.
- **What goes wrong:** Two naive approaches fail at `500` images: (1) running one untimed benchmark pass pulls each image on just `1` node (`10%` fleet coverage), so whichever arm runs *second* enjoys `2x` warmer node caches than the arm that ran first; (2) putting `500` containers into a single `DaemonSet` pod exceeds Kubelet/API pod size limits and chokes `containerd` unpack queues.
- **What we do:** `run_daemonset_prepull.py` chunks the `500` images into `20` `DaemonSet`s of `25` containers each (`max_parallel=5` concurrent `DaemonSet`s, matching Kubelet's `maxParallelImagePulls=5`), waits for `10/10` nodes on every chunk (`902.46 s` total), and deletes all prepull `DaemonSet`s before Run 1 begins.
- **Related:** [GKE-4](#gke-4), [GKE-5](#gke-5), [GKE-6](#gke-6).

</details>

<a id="bench-7"></a>

## BENCH-7 Exfiltrate structured JSON from finished Kubernetes `Job` pods via stdout sentinel markers

**Takeaway:** `kubectl cp` fails on `Completed` containers once the runtime stops, and mounting GCS/PVCs adds cloud IAM/storage dependencies to the benchmark image. Print the final JSON between deterministic sentinel markers (`===RESULT_JSON_BEGIN===` / `===RESULT_JSON_END===`) on `stdout` while routing runner logs to `stderr`, then slice `kubectl logs` cleanly on the host.

<details>
<summary>The full story (harness/run_job_v2.sh lines 123-181)</summary>

- **Situation:** In-cluster benchmarks run inside a `restartPolicy: Never` Kubernetes `Job` (`asrl-driver`). Once the Python script exits `0`, the container immediately enters `Completed` (terminated) state.
- **What goes wrong:** `kubectl cp` executes `tar` inside the running container via `exec` — which fails on a `Completed` pod! Meanwhile, mixing Python `logging` output onto `stdout` corrupts raw JSON redirection if any library prints a warning.
- **What we do:** Inside the `Job` args (`run_job_v2.sh`), the driver writes `--out /out/result.json` on an `emptyDir` while redirecting `2>/out/driver.stderr.log`, prints the last 80 lines of `stderr` for diagnostics, and then cats `/out/result.json` strictly bracketed by `===RESULT_JSON_BEGIN===` and `===RESULT_JSON_END===`. After `Job` completion, `run_job_v2.sh` fetches the full pod log via `kubectl logs` (which kubelet serves reliably for terminated pods) and extracts the exact JSON payload in 10 lines of Python.
- **Related:** [BENCH-4](#bench-4).

</details>

<a id="bench-8"></a>

## BENCH-8 Gate cloud benchmark runs behind a fast, read-only `verify_cluster.sh` preflight

**Takeaway:** Discovering after a 40-minute, `3,000`-pod sweep that `--extensions` was unset, `maxPods` was `110` instead of `256`, or GCFS image streaming was off wastes hours and cloud budget. Run a strictly read-only (`creates nothing`) preflight script that hard-fails on broken invariants and warns on tuning drift in two seconds.

<details>
<summary>The full story (harness/verify_cluster.sh)</summary>

- **Situation:** Reproducing GKE scale numbers requires over a dozen cluster, node-pool, RuntimeClass, CRD, and controller flags to align simultaneously (`v1.36/1.37`, `gvisor` RuntimeClass + node labels, 4 CRDs, `--extensions`, `--sandbox-concurrent-workers`, `maxPodsPerNode=256`, Secondary Boot Disk / `gcfsConfig.enabled`).
- **What goes wrong:** Combining cluster *verification* with cluster *mutation* risks accidentally modifying shared clusters, while skipping verification lets subtle defaults (like GKE's default `maxPods=110`, which caps `10` nodes at `1,100` pods including system DaemonSets!) silently bottleneck `1,000`-sandbox runs.
- **What we do:** `harness/verify_cluster.sh` runs 8 read-only `kubectl` + `gcloud container node-pools describe` checks in `<3 s`, separating fatal `FAIL`s (unreachable context, missing `gvisor` RuntimeClass or CRDs, missing controller) from `WARN`s (`maxPods != 256`, low worker counts, missing SBD/GCFS), exiting non-zero before a single benchmark pod is launched.
- **Related:** [GKE-1](#gke-1), [GKE-5](#gke-5).

</details>

<a id="bench-9"></a>

## BENCH-9 Mount a `Memory` `emptyDir` (`tmpfs`) over `/tmp` and `/root/.cache` (`--enable-cow`) when benchmarking orchestration overhead

**Takeaway:** Python and `git` probes that write `.pyc` bytecode or `/tmp` lockfiles trigger container overlayfs copy-up I/O on disk, mixing node storage jitter into sandbox scheduling benchmarks. Set `PYTHONDONTWRITEBYTECODE=1` and mount an in-memory `emptyDir` over `/tmp` and `/root/.cache` when isolating orchestration and `exec` overhead.

<details>
<summary>The full story (harness/bench_core.py: build_template_spec)</summary>

- **Situation:** Multi-step RL benchmarks (`Bench 1A` R2E-Gym and `Bench 1B` Tunix GRPO) execute `git` and Python commands across `500-1,024` simultaneous gVisor sandboxes per step (`50-100` pods per node).
- **What goes wrong:** Even read-mostly `pytest` or Python invocations attempt to write `__pycache__/*.pyc`, `/root/.cache`, and `/tmp` files, causing `100` concurrent overlayfs copy-ups per node on `pd-ssd` that inflate `process` (`exec`) tail latency independently of Kubernetes or SDK behavior.
- **What we do:** `bench_core.py: build_template_spec` (`--enable-cow`) injects `PYTHONDONTWRITEBYTECODE=1`, `TMPDIR=/tmp`, and a `2Gi` `emptyDir: {medium: Memory}` (`ram-scratch`) mounted at `/tmp`, `/root/.cache`, and `/tmp/pytest-of-root` across `bench_core`, `run_step_bench`, and `run_claim_soak_bench`.
- **Related:** [GKE-6](#gke-6), [GKE-8](#gke-8).

</details>

<a id="bench-10"></a>

## BENCH-10 Time identical wire spans (`paired_wire_claim_s`), and check `avg` + `p99` alongside `p50` under Little's Law (`L = \lambda W`) before calling a lower median faster

**Takeaway:** Two classic measurement traps make a faster system look slower in summary tables: (1) asymmetric phase timers (`per_claim`'s `0.50 s` in-thread `phases.claim` omitted `2.4 s` of thread hand-off and event-loop starvation that `paired_wire_claim_s` caught at `2.81-2.94 s` vs batch `1.22-1.61 s`), and (2) head-of-line slot skewness on cold pools, where `per_claim` starved most slots (`p99 = 357 s`, `avg = 57.3 s`) while cycling half its tasks through a few fast slots (`p50 = 6.3 s` vs batch's fair `p50 = 18.8 s`, `avg = 18.7 s`, `p99 = 40.7 s`).

<details>
<summary>The full story (Campaign 5 `eval500` wire-clock audit & `z1_3k` concurrency sweep, RL_BATCH_FINAL_REPORT.md §0 & Appendix D)</summary>

- **Situation:** In initial Campaign 5 tables, two numbers looked worse for `batch` than upstream `per_claim`: `eval 500x1` reported `phases.claim avg = 1.25-1.47 s` (`batch`) vs `0.50 s` (`per_claim`), and cold-pool `3,000`-task SWE-bench (`500x6`, `wp=1`, `c=500`) reported `TTFI p50 = 18.6-22.5 s` (`batch`) vs `6.3-6.8 s` (`per_claim`) even though `batch` finished the `3,000` tasks `2.5x` faster (`160 s` vs `393 s`)!
- **What goes wrong:**
  1. **Mismatched timer boundaries:** In `per_claim`, `phases.claim` timed only `create_sandbox() + get_pod_name() + get_pod_ip()` inside the worker thread, excluding `run_in_executor` submission, `ClaimWatchdog` thread creation, `record_pod_identity()` (`1` Pod `GET`), and delivering the future back onto an event loop starved by `1,517` threads (`loop_lag_s max = 4.5-6.8 s` vs `0.10-0.26 s` on `batch`). On the common `paired_wire_claim_s` clock around `await af.acquire()`, `batch` was **`2x` faster (`1.22-1.61 s` vs `2.81-2.94 s`)**.
  2. **Little's Law (`W = L / \lambda`) vs skewed starvation:** On `3k` (`wp=1`, `84%` cold starts), the cluster readied pods at a steady ceiling of `~16-18 tasks/s`. Holding `L ~= 362` claims in flight (`c=500`) forces mean wait `W ~= 19.9 s` regardless of scheduler fairness; `batch` served claims FIFO (`p50 = 18.8 s ~= avg = 18.7 s`), whereas `per_claim` (`avg = 57.3 s`, `p99 = 357 s`) let a handful of fast-cycling slots complete half the tasks quickly (`p50 = 6.3 s`) while starving the rest!
- **What we do:** Always compare caller-visible wire timers (`paired_wire_claim_s`, `paired_ttfi_s`) across modes, report `(p50, avg, p99)` together, and size cold-pool concurrency near `ready_rate * cold_start_s` (`c = 150` on `z1_3k` cut `batch` TTFI `p50` **`18.8 s -> 6.6-6.9 s`** and `p99` **`40.7 s -> 17.0-17.9 s`** at the same `16-18 tasks/s` throughput, documented in `examples/agent-sandbox-rl/README.md` at `ec01bf0d`).
- **Related:** [BENCH-1](#bench-1), [GKE-10](#gke-10), [OPT-33](optimizations.md#opt-33).

</details>

<a id="bench-11"></a>

## BENCH-11 Pin driver images by `@sha256` digest, interleave A/B arms in one session, and audit historical slide ratios against raw JSONs and current `HEAD`

**Takeaway:** Mutable image tags, cross-day comparisons, and un-audited summary ratios silently corrupt benchmark claims: upstream `per_claim` sped up `4.5-6x` between Campaign 4 and Campaign 5 (`1,129-1,576 s -> 239-252 s` on R2E), turning a real single-run `3.81x` ratio into a non-reproducible historical artifact, while several slide shorthand labels (`"9.65x p50"`, `"3.63x TTFI"`, `"32.5x cleanup"`) compressed means-of-medians, `first`-vs-`p50`, or serial-vs-bulk semantics.

<details>
<summary>The full story (HEADLINE_CLAIMS_FORENSIC_AUDIT.md & `run_job_v8.sh` digest pinning)</summary>

- **Situation:** Before opening the final RL batch PR, we audited every prior slide/resume headline number back to its raw `.json` / `.pod.log` file and re-ran the original Campaign 4 driver image alongside current `HEAD` on both older and fresh clusters.
- **What goes wrong:**
  - **Baseline & cluster drift:** Re-running the original Campaign 4 image stalled at `1,010-1,037 s` on the churned 20-node cluster (`GC` lag, [GKE-18](#gke-18)) while current upstream `per_claim` (`300d05e7`) ran `4.5-6x` faster than Campaign 4's `per_claim` (`252 s` vs `1,129-1,576 s`), proving cross-campaign ratios cannot be mixed without same-session `ABAB` controls ([BENCH-2](#bench-2)).
  - **Metric-label drift:** Forensic tracing showed `"9.65x env_ready p50"` was the arithmetic mean of 8 per-step `p50`s (not a task-level `p50`), `"3.63x TTFI"` on the `4k` burst compared `first` sandbox (`p50` was `2.28x`), `"5x fewer API calls"` was analytical rather than scraped on GKE, and `"32.5x cleanup"` compared `batch.release()` (`1` `deletecollection`) against sequential `delete_all()` rather than concurrent `release_all()` (`~0.7-1.7x`, cluster-dependent).
  - **Un-tested fixes:** Our unit-tested 32-line controller patch for cold launches looked airtight in fake-client tests yet measured `5/12` cold bursts vs `2/8` baseline on GKE ([GKE-20](#gke-20)).
- **What we do:** Pin every benchmark Job in `run_job_v8.sh` to an immutable `IMAGE_DIGESTS.txt` `@sha256` digest (never reusing a tag), interleave `batch` and unmodified upstream `per_claim` (`ABAB`) in the same plan, generate Markdown tables mechanically from `runs/*.json` (`build_report_k1.py`), and verify every proposed optimization or fix with a real GKE A/B before claiming a win.
- **Related:** [BENCH-2](#bench-2), [BENCH-4](#bench-4), [GKE-18](#gke-18), [GKE-20](#gke-20), [PROC-5](process.md#proc-5).

</details>

<a id="bench-12"></a>

## BENCH-12 Run an independent raw-JSON report auditor before publishing benchmark claims

**Takeaway:** Even when a table generator reads raw JSONs, surrounding prose summaries introduce blanket overclaims (`"all tasks succeeded"`), omit failed setup jobs that wrote no `.json`, or mix numbers across image revisions. Have a fresh subagent recompute every prose and table figure directly from `runs/*.{json,meta.json,pod.log}` without reading your generator script before publishing.

<details>
<summary>The full story (AUDIT_RL_BATCH_FINAL_REPORT.md on RL_BATCH_FINAL_REPORT.md)</summary>

- **Situation:** Under deadline pressure, we drafted `RL_BATCH_FINAL_REPORT.md` from `tables_k1.md` and session notes, then spawned an independent read-only auditor subagent (`4fbea1c2`) instructed to recompute every number in §§2, 4, and 5 from raw `.json`, `.meta.json`, and `.pod.log` files without trusting `tables_k1.md`.
- **What goes wrong (caught by the auditor in 7 minutes):** While headline medians matched, the auditor caught `20` validity and precision bugs in the draft:
  1. A false blanket sentence (`"All tasks succeeded in every run in this section"`): `k3_burst_1x1000_pc_up_r1` had `3` empty-output `probe_err` failures at `c=1,000`, and `k1_parity500x2_batch_r1` finished `999/1,000`.
  2. An undisclosed setup failure (`k3_burst_1x1000_batch_r2.meta.json` had `job_failed=1`, warm pool stuck at `992/1,000` for `601 s`), invisible to `build_report_k1.py` because failed jobs emit no result `.json`!
  3. Subtle range/attribution slips: `3k` sliding `TTFI p99` reached `62.4 s` (not `54 s`), `1` cold launch occurred in a fresh R2E step and `3` in Tunix (contradicting `"in any R2E step"`), `f85a5072` was mis-grouped with `2eb5ef6d`, and an unpaired single-run ratio was phrased as a tie.
- **What we do:** Treat benchmark reports like production code ([AGENT-1](process.md#agent-1), [AGENT-6](process.md#agent-6)): scan `*.meta.json` (`job_failed`, `attempt`, `controller_restarts`) alongside `*.json`, disclose every partial or failed job explicitly, state `n` and min-max ranges on every cell, and gate publication on an independent raw-file audit.
- **Related:** [BENCH-1](#bench-1), [BENCH-4](#bench-4), [PROC-5](process.md#proc-5), [AGENT-1](process.md#agent-1).

</details>

---

[Learnings home](../learnings.md) · [Field guide index](../learnings.md#sec-field-guide)
