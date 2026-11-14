# Intern Final Self-Evaluation Copy-Paste Draft

---

## 1. Project Description

Currently in the open-source Kubernetes Agent Sandbox project (kubernetes-sigs/agent-sandbox), claiming sandboxes at large scale through the SDK requires calling a per-sandbox create_sandbox API for every claim, opening N separate watch streams and N delete requests for N sandboxes, and forcing each caller to re-implement fan-out, quorum handling, retries, pacing, and cleanup from scratch. Because every caller builds around single-claim primitives, large bursts suffer from 4-6 Kubernetes API calls per sandbox, HTTP connection-pool and socket exhaustion, and duplicated orchestration logic.

The goal of my project is to design and add official batch-claiming support (claim_batch and Batch handles) to both the synchronous and asynchronous Python SDKs, giving callers a scalable way to claim and manage large batches of sandboxes across multiple warm pools with built-in support for quorum and streaming delivery, pacing and retries, and automatic/crash-safe cleanup.

The second part of my project integrates batch claiming into agent-sandbox-rl (a downstream library used with Tunix by teams such as Trellis for reinforcement learning rollout orchestration) to remove its per-sandbox claiming bottleneck during large RL runs, and benchmarks the resulting throughput and latency gains across real SWE-bench and R2E-Gym workloads at 1,000+ sandbox scale on GKE. Because Kubernetes Agent Sandbox is also used by Google's Vertex AI platform and external customers such as Mistral AI (running 1M sandboxes) and Thinking Machines Lab, closing this scale and performance gap against faster competing alternatives delivers immediate efficiency gains to their workloads and helps prevent customers from churning off GKE / Agent Sandbox while my team builds next-generation optimized RL support on Agent Substrate.

---

## 2. Project Accomplishments (Core Project)

Features implemented in the Python SDK:
- Designed and implemented the claim_batch() API on SandboxClient/AsyncSandboxClient, which supports claiming a batch of SandboxClaims across multiple warmpools and returns a SandboxBatch/AsyncSandboxBatch handle to manage it; uses a shared Sans-I/O BatchState core so sync and async share identical state logic.
- Added 3 types of batch consumption modes: events() (streams members immediately as they become ready; when used after the other two modes, it streams the remaining spare members instead), group_outcomes() (yields each group's outcome as soon as that pool's quorum settles), and wait_for_quorum() (blocks until quorum across all groups is satisfied).
- Expanded the batch design by adding acquire() (creates a claim dynamically inside an open batch) and size=0 lazy groups, allowing asynchronous and sliding-window RL workloads to continuously claim and release individual sandboxes inside one shared batch.
- Aggregated readiness and lifecycle state over 1 shared label-scoped Kubernetes watch stream instead of N per-claim watches, with watch BOOKMARKs to advance idle resourceVersions, automatic reconnect + consistent re-list diffing across network/TLS drops and etcd 410 Gone compactions, and connect(member) seeding pod IPs directly from watched status (0 extra Sandbox GETs) while caching member connections and automatically closing/invalidating them if a pod is replaced or changes IP.
- Designed a Kubernetes Lease-based single-driver ownership model: 1-RTT conditional (resourceVersion CAS) background heartbeats, lease_degraded() and err() health reporting, cross-process handoff via detach() and get_batch() (reconstructing group order and batch config from deterministic ordinal claim names, claim annotations, and Lease annotations), and split-brain protections (batch-lease-uid incarnation labels, leaseTransitions checks, and monotonic send-time self-fencing across SIGSTOP/GC pauses).
- Built multi-granularity, automatic, and crash-safe cleanup: 5 explicit cleanup methods (release, release_members, release_group, release_not_ready, release_member) backed by multi-round label deletecollection sweeps (recovering across 60s apiserver timeouts while fencing concurrent acquire() calls so new claims are not swept), automatic client context-exit and loop-free atexit cleanup, work_budget shutdownTime bounds, and UID-bound Lease ownerReferences paired with a standalone batch_reaper CronJob CLI that reaps expired Leases under bounded clock skew so Kubernetes cascading GC cleans up orphaned claims after SIGKILL, OOM, or node preemption.
- Added client-side pacing, concurrency, and retry controls: a create_rps rate limiter (reserving send timestamps under a lock and sleeping outside it), a max_in_flight concurrency semaphore (released before backoff sleeps, with connection-pool sizing checks >= max_in_flight + 2 so the watch stream and Lease heartbeats never starve), warm-pool-aware pacing (using readyReplicas counts to skip create_rps when warm pods are ready, with temporary 429 cooldowns and single-flight pool refreshes), min_ready-first create ordering so groups reach quorum sooner, and jittered exponential backoff on transient errors with Retry-After support (plus disabling default urllib3 retries on Lease and batch API clients so hidden retries cannot cause false 409 conflicts).
- Optimized SDK memory, CPU, and startup latency for 10,000+ claim batches: validating warm pools and templates in parallel (deduplicating shared templates) before creating the Lease so invalid calls leave no orphan resources, draining deletecollection HTTP responses without JSON-decoding them (since the response body is unused), paginating claim LIST calls (limit=500) from a single resourceVersion snapshot while counting live claims page-by-page instead of holding all pages in memory, stripping unused metadata.managedFields (~50% of claim bytes), waking only the matching acquire() caller per watch event via per-claim Events, and keeping state updates O(1) via per-pool counters, per-pool member indexes, and lazy filtering of released claims when popping queued events/outcomes.
- Validated concurrency, sync/async parity, and distributed invariants by writing 750+ new unit, kind E2E, and fault-injection tests alongside dedicated verification harnesses: TLA+ formal model checking (3.27M states across Lease fencing and intra-process specs), stateful Hypothesis property fuzzing against a reference model, and lockstep differential tests checking that sync and async behave identically.

Agent Sandbox RL Integration & Optimizations:
- Integrated claim_mode="batch" into agent-sandbox-rl: uses one size=0 batch per cluster (lazily validating pools in parallel on unplanned runs instead of running serial startup GETs), skips eager Pod GETs on acquire(), checks pod liveness at RL step boundaries in memory via the batch watch before making any API call, shares one CoreV1Api HTTP pool for REST status checks, parallelizes bulk fleet acquire and release_all calls with automatic rollback on partial failure, and runs one cold-start BatchWatchdog thread per batch that makes 0 Pod GETs when warm claims become ready quickly.
- Eliminated worker-thread bottlenecks in high-concurrency async RL runs by running batch claims natively on Python's asyncio event loop (AsyncBatchClaims) and running pod exec over a shared event-loop WebSocket client (aexec(), 0 threads per exec), paired with prewarming 64 keep-alive TLS connections per cluster, skipping aiohttp's per-request .netrc/proxy thread-pool lookups when unconfigured, and replacing per-pool WarmPool GETs with a single paginated namespace LIST.
- Diagnosed a multi-step warm-pool refill stall on multi-day GKE clusters caused by slow Kubernetes cascading garbage collection (confirmed with fresh-vs-aged cluster control runs) and fixed it by deleting the underlying Sandbox and Pod directly in a bounded background pool after claim release (automatically disabling itself on 403 if optional RBAC is absent), alongside batching large release tails into one deletecollection (while keeping small rolling releases per-claim and flushing immediately if another acquire() is waiting for capacity) and delaying background pipelined prefetch creates by up to 250 ms when foreground creates are active.

Scale & GKE Benchmark Results (Batch vs. Per-Claim Baseline):
- Kubernetes API & Watch Scalability: Reduced per-sandbox Kubernetes lifecycle traffic from 4-6 API calls and N watch streams down to ~1.03 API calls/sandbox and 1 shared watch stream.
- Multi-Step Synchronous RL on GKE (8,000 real R2E-Gym rollouts across 500 repo images, 8 steps per rollout): Achieved 3.81x faster end-to-end wall time (21.7 min to 5.7 min), 5.41x faster steady step barriers (175.3s to 32.4s), 9.65x faster median sandbox readiness time (151.2s to 15.7s), and 4.9x fewer client sockets (505 to 103) with 0% task errors, plus ~4.5x faster runtime (~1,007s to ~223s) on multi-day clusters where Kubernetes garbage collection lagged.
- Multi-Cluster SWE-bench on GKE (5 clusters, up to 8,000 concurrent warm sandboxes): Achieved ~5x faster median claim latency (17.1s to 3.4s) and 1.83x faster wall time across 8 concurrent jobs (4,096 sandboxes), and 3.3x-4.0x faster bulk sandbox teardown (3,608-3,852 deletes/sec vs. 961-1,092 deletes/sec, with ~146x fewer control-plane sockets at 4,000 scale) across 4,000-8,000 warm bursts.
- High-Concurrency Async RL Driver on GKE (1,000 concurrent claims and pod execs): Reduced client process overhead from ~3,000 threads and ~1,050 open sockets down to 3 threads and 68 sockets, delivering 2.3x-2.9x higher claim throughput (246-267/sec vs. 91-106/sec) and 3.45x faster median time to first command inside the sandbox (10.0s to 2.9s).

---

## 3. Additional Project Accomplishments

- Reduced core Go Sandbox controller average reconcile latency by 55% by removing a deprecated pod name annotation (with backwards compatibility support for SDKs) that was written on each reconcile loop: https://github.com/kubernetes-sigs/agent-sandbox/pull/1417 and https://github.com/kubernetes-sigs/agent-sandbox/pull/1467
- Cut Python client RAM growth 195x (90.1 to now 0.46 MiB/100 sandboxes) by untracking terminated handles and breaking circular OpenSSL references: https://github.com/kubernetes-sigs/agent-sandbox/pull/2085 and https://github.com/kubernetes-sigs/agent-sandbox/pull/2086
- Added Kubernetes events for the Sandbox CRD, which previously had none (and helping a downstream issue in the process): https://github.com/kubernetes-sigs/agent-sandbox/pull/1532
- Individually identified and fixed a major bug in the Python SDK that leaked 100% of Sandboxes on automatic async exit due to a race condition: https://github.com/kubernetes-sigs/agent-sandbox/pull/1512
- Extended the Python SDK to support injecting a different ApiClient, fixing two issues: https://github.com/kubernetes-sigs/agent-sandbox/pull/1509
- Sped up first acquire time ~18x in agent-sandbox-rl by eliminating duplicate, racing ApiClient creation: https://github.com/kubernetes-sigs/agent-sandbox/pull/2035

---

## 4. Technologies & Concepts Learned (Concise, Non-Redundant with Accomplishments)

For systems/distributed systems, I thought every new related concept I learned was so cool and fascinating, as they were ideas I would have never thought of and it's just fun to learn new things:
- Why wall clocks cannot be trusted for distributed ownership under process pauses (SIGSTOP, GC pauses) or clock skew, and how monotonic send-time deadlines and two-observation wait windows solve split-brain safely.
- Reasoning about ambiguous network timeouts ("did my timed-out request land on the server?"), designing idempotent retries around 409 Conflict vs. name-based cleanup, and reconciling two out-of-order observers (a watch stream racing with direct RPCs).
- Closed-loop rate limiting (only pacing work that actually contends on the underlying resource) and using formal methods (TLA+ model checking and stateful property fuzzing) to catch multi-actor interleavings that unit tests miss.

For Kubernetes and Go, I had some prior exposure to basic resource types, but this project taught me a lot about control-plane internals and controller conventions:
- etcd MVCC mechanics: optimistic concurrency via resourceVersion, history compaction (410 Gone), watch BOOKMARK events on idle streams, and consistent-snapshot LIST pagination.
- Ownership and deletion internals: Lease primitives, conditional DELETE preconditions, UID-bound ownerReferences and how cascading garbage collection can lag under heavy churn, finalizers/deletionTimestamp lifecycles, server-side deletecollection behavior, and API Priority and Fairness (APF) seat contention.
- Idiomatic Go controller-runtime patterns, including idempotent status patching, transition-gated Event emission, and avoiding redundant writes on reconcile hot paths.

For Python, building both sync and async SDK variants taught me library internals and language primitives I had not used in depth before:
- Behavioral differences between sync (threading + urllib3) and async (asyncio + aiohttp) stacks: non-terminating async close() vs. terminating exit, default urllib3 retries vs. aiohttp pool queuing, raw-stream socket hygiene, hidden aiohttp .netrc thread-pool lookups, and CPython atexit constraints during interpreter shutdown.
- Structuring dual sync/async libraries cleanly over a shared Sans-I/O state machine and custom watch reflector (since Python's Kubernetes clients do not support informers on custom resources).
- Core concurrency, typing, and memory primitives: Condition, Event, BoundedSemaphore, RLock vs. task-owner re-entrancy tracking on asyncio.Lock, Future with asyncio.shield(), contextlib (ExitStack, closing, aclosing), PEP 561 Protocols, and weakref callbacks to break reference cycles holding native OpenSSL C-heap memory.

I also learned a lot about reinforcement learning orchestration and GKE benchmarking from my RL integration work:
- Tradeoffs across RL rollout strategies (synchronous step barriers vs. asynchronous, sliding-window, and pipelined prefetching) and why high-cardinality multi-image workloads (500 distinct SWE-bench/R2E environments) stress clusters differently than shared pools.
- The 5-tier "warmness" spectrum on GKE and how easily cache state confounds benchmarks if uncontrolled: Pod/RAM-warm (running WarmPool pod, control-plane adoption only) vs. OS page-cache-warm (fresh pod start, image blocks in kernel RAM) vs. disk-warm (unpacked on local SSD or hydrated Secondary Boot Disk) vs. cold SBD (lazy first-read block hydration from GCS) vs. network-cold (Artifact Registry / unhydrated GCFS image streaming).
- Isolating infrastructure ceilings via parameter sweeps and cluster controls: kubelet 85% ImageGC eviction thresholds, gVisor (runsc) pod packing density, Cloud NAT VIP port hashing across multi-cluster fleets, and measuring real system convergence (TTFI and full pod-termination drain) rather than client API return time.

---

## 5. Top 5 PRs Representing Coding Ability

1. https://github.com/kubernetes-sigs/agent-sandbox/pull/1824 - feat(batch,python-sdk): add claim_batch and release (12/N)
2. https://github.com/kubernetes-sigs/agent-sandbox/pull/1991 - feat(python-sdk): add acquire (16/N)
3. https://github.com/kubernetes-sigs/agent-sandbox/pull/2166 - feat(python-sdk): skip warm-pool create pacing and stop create retries on release (19/N)
4. https://github.com/kubernetes-sigs/agent-sandbox/pull/2252 - fix(python-sdk,batch): harden batch client cleanup, handle connection close, and PEP 561 typing (25/N)
5. https://github.com/kubernetes-sigs/agent-sandbox/pull/2175 - perf(python-sdk,batch): page the claim list and stop parsing the deletecollection body (20/N)
