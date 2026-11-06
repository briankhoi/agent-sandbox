<a id="sec-gke-benchmarking"></a>

[Learnings home](../learnings.md) · [Field guide index](../learnings.md#sec-field-guide)

# Benchmarking and GKE at 1,000-sandbox scale

**How to read this page.** Running `148,544` real sandbox claims and multi-step RL rollouts across `36` zero-error GKE sweeps (`origin/bench/rl-gke`, `examples/agent-sandbox-rl/bench-results/gke/campaign-v2/` and `ai-on-gke/sandbox-rl-performance` PR #5) surfaced two families of lessons that unit tests and `kind` clusters cannot show:

- **GKE control-plane, node, storage, and runtime physics (`GKE-1` to `GKE-12`):** how GKE's Control Plane Autoscaler, API Priority and Fairness (APF), `RuntimeClass` vs. `DaemonSet` taint admission, GCFS image streaming vs. Secondary Boot Disks (`CONTAINER_IMAGE_CACHE`), Kubelet `ImageGC`, `terminationGracePeriodSeconds`, and gVisor `gofer` filesystem metadata actually behave at `500–1,024` concurrent sandboxes.
- **Benchmark harness and measurement rigor (`BENCH-1` to `BENCH-9`):** non-obvious engineering in `bench_core.py`, `run_daemonset_prepull.py`, `run_job_v2.sh`, and `verify_cluster.sh` that kept runs isolated, reproducible, and free of survivorship or platform-drift distortion.

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

**Takeaway:** GKE regional master VM sizing (`go/kcp-resize`) scales in discrete node buckets. Creating an initial temporary Spot pool of `510` nodes sizes regional KCP to `n1-standard-32` (`gomaxprocs = 16`, `1,449` nominal APF seats). Using `--disk-type=pd-standard` for OS boot disks counts against `DISKS_TOTAL_GB` (`102,400 GB`) and `0 GB` of `SSD_TOTAL_GB`, reserving the regional `40,960 GB` SSD quota exclusively for `50 × 600 GB = 30,000 GB` Secondary Boot Disks.

<details>
<summary>The full story (Campaign 3 Multi-Cluster Fleet, CAMPAIGN_3_MASTER_TRACKER.md §3 GKE-1)</summary>

- **Situation:** Multi-cluster testing across `5` regional GKE clusters (`asrl-c3-1..5`) required `50 × n2-standard-32` nodes with `600 GB` `swebench-500-preload-600g` Secondary Boot Disks (`30,000 GB` total `pd-ssd`).
- **What goes wrong:** If temporary master-warmup Spot pools or OS boot disks use `pd-balanced`, GCP charges them against regional `SSD_TOTAL_GB` (`40,960 GB` quota limit in `us-central1`), causing node pool creation to fail with `Quota SSD_TOTAL_GB exceeded`.
- **What we do:** In `provision_multicluster_c3.sh`, the temporary Spot warmup pool used `--num-nodes=170 --node-locations=us-central1-a,b,c` (`510` nodes) with `--disk-type=pd-standard --disk-size=30`, sizing `kube-apiserver` to `16` cores (`gomaxprocs = 16`) immediately on birth while consuming `0 GB` of `SSD_TOTAL_GB`.
- **Related:** [GKE-1](#gke-1).

</details>

<a id="gke-14"></a>

## GKE-14 GCE Secondary Boot Disk images hydrate blocks lazily from GCS on first read

**Takeaway:** When attaching a custom GCE disk image as a `CONTAINER_IMAGE_CACHE` Secondary Boot Disk, first-boot container startups pay lazy GCS block-hydration page faults. Once hydrated, subsequent runs hit local NVMe speeds. Always note whether a T1 benchmark ran on cold first-boot blocks or hydrated blocks.

<details>
<summary>The full story (Campaign 3 C3-1a Anchor, RESULTS.md #39 vs #42)</summary>

- **Situation:** `C3-1a` ran `500 × 6 = 3,000` SWE-bench tasks on a freshly provisioned `10 × n2-standard-32` node pool with `swebench-500-preload-600g` SBD.
- **What goes wrong:** On the first run right after node creation, reading the `600 GB` image cache pulls blocks on demand from Google Cloud Storage (`wait_pool_ready avg = 68.05s`, `claim p50 = 59.78s` on `per_claim`; `12.72s` and `9.37s` on `batch64`).
- **What we do:** After the first wave touches the disk blocks, the SBD is fully resident in local SSD cache (`wait_pool_ready avg = 3.68s–4.11s`, wall `124.73s` `per_claim` vs `146.22s` `batch64`), reproducing Campaign 2's `139.10s` within `5.1%`. Always distinguish cold-block first boot from hydrated SBD parity in benchmark reports.
- **Related:** [GKE-4](#gke-4).

</details>

<a id="gke-15"></a>

## GKE-15 Do not set `resources.requests["ephemeral-storage"]` when primary boot disks are 50 GB and SBD hosts images

**Takeaway:** Specifying `resources.requests["ephemeral-storage"]: 4Gi` on guest sandboxes when primary OS boot disks are `50 GB` limits K8s node capacity to `~10` pods/node (`100` pods/cluster). Keep `emptyDir.sizeLimit: 4Gi` in `volumes` but omit `requests["ephemeral-storage"]` so high-density packing (`240` pods/node) schedules cleanly.

<details>
<summary>The full story (Campaign 3 C3-3d scheduling bug fix)</summary>

- **Situation:** In `C3-3d`, `mc5-sdk-swebench-wp` requested `ephemeral-storage: 4Gi` to match the `4Gi` `/tmp` disk size limit.
- **What goes wrong:** Kubelet reserves primary boot disk space based on `ephemeral-storage` requests. With `50 GB` primary boot disks, K8s refused to schedule more than `10` pods per node, capping each 10-node cluster at `100` pods instead of `1,000`.
- **What we do:** Set `emptyDir: {"sizeLimit": "4Gi"}` on the volume mount without requesting `ephemeral-storage` in container resources, allowing `1,000` warm pods per cluster to boot in `13.76s`.
- **Related:** [GKE-5](#gke-5).

</details>

<a id="gke-16"></a>

## GKE-16 Tune `aiohttp` `keepalive_timeout` and pace initial connection handshakes across Cloud NAT in multi-cluster drivers

**Takeaway:** `aiohttp.TCPConnector` defaults to `keepalive_timeout = 15.0s`. When multi-cluster warm-pool refills take `>15s`, all idle client TLS sockets close, putting hundreds of outbound sockets into kernel `TIME_WAIT` on the single Cloud NAT external IP. When the next burst opens hundreds of new connections simultaneously, GKE L4 load balancers drop excess SYN packets (`tcp_syn_retries` 31s timeout). Set `_keepalive_timeout = 900.0` and pace initial connection establishment on pool misses.

<details>
<summary>The full story (Campaign 3 C3-3d driver hardening)</summary>

- **Situation:** `C3-3d` drove `5` regional GKE clusters simultaneously from 1 Python `asyncio` process via `AsyncSandboxClient` (which uses `kubernetes_asyncio` over `aiohttp`).
- **What goes wrong:** After refilling `1,000` pods per cluster (`~30s`), `aiohttp`'s default `15s` keepalive timer had closed all `320` pooled sockets. Launching `5` concurrent `claim_batch` requests blasted `320` simultaneous new SYN packets from the single Cloud NAT external IP, hitting GKE's external L4 load balancer SYN rate limit and raising `TimeoutError` in `aiohappyeyeballs`.
- **What we do:** In `run_mc5_sdk_batch_bench.py`, set `ac.rest_client.pool_manager._connector._keepalive_timeout = 900.0` so sockets never close during refills, and space `_create_direct_connection` calls by `6ms` on pool misses to prevent NAT SYN storms.
- **Related:** [GKE-11](#gke-11).

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

---

[Learnings home](../learnings.md) · [Field guide index](../learnings.md#sec-field-guide)
