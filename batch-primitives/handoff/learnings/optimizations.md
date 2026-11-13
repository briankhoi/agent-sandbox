<a id="sec-optimizations"></a>

[Learnings home](../learnings.md) · [Field guide index](../learnings.md#sec-field-guide)

# Optimization showcase: how batch claim and RL orchestration got fast and lean

**How to read this page.** Every optimization answers one question: *what resource does this limit, copy, or round-trip protect?* Work that does not touch that resource may skip the cost, as long as a guard falls back to the slow, safe path when the shortcut's assumption breaks. Part I covers every optimization in the Python SDK (`SandboxBatch` / `AsyncSandboxBatch`, `1/N` to `25/N`). Part II covers every optimization in `agent-sandbox-rl` (`claim_mode="batch"`, `27/N` + Campaign 5). Each card explains the bottleneck, the mechanism under the hood, the safety guard, and the measured result.

> [!NOTE]
> **About the numbers.**
> - **Bench tip:** the kind benchmark on `origin/bench/batch-claim` (`test/benchmarks/batch-claim/bench-report.md`), running `44fb487f` (`results/PROVENANCE.md`).
> - **GKE Campaign 2 & 4 (`27/N`):** production GKE scale benchmarks (`origin/bench/rl-gke`, `examples/agent-sandbox-rl/bench-results/gke/campaign-v2/` and `campaign-v4/`), running `25/N` + `27/N` (`feat/rl-batch-hardening`). See [Benchmarking and GKE at 1,000-sandbox scale](gke-benchmarking.md).
> - **GKE Campaign 5 (`c5`):** the 2026-11-13 RL batch-mode optimization runs (`examples/agent-sandbox-rl/bench-results/gke/RL_BATCH_OPTIMIZATIONS.md`, branch `feat/c5-batch-rebased-300d05e7` stacked on `feat/rl-batch-hardening`).
> - **Analytical:** derived directly from the code when not isolated in its own A/B run.

**The whole-batch bill** (bench tip, #47 table, batch vs per-claim baseline):

| Shape | Requests | Apiserver watch peak | API connections |
| :-- | :-- | :-- | :-- |
| Cold, 300 claims | 309 vs 1,500 | 3 vs 302 | 5 vs 300 |
| Warm, 160 claims | 168 vs 800 | 3 vs 17 | 16 vs 55 |
| RL shape, 160 claims | 246 vs 800 | 3 vs 23 | 15 vs 59 |

Rolling acquire: 2.0 requests per rollout vs 6.0 (bench tip, rolling table). RL evidence: 243 vs 640 requests and 1 vs 160 watches per 160-sandbox step (`handoff/pr_board.md`, 25/N).

<a id="opt-lead"></a>

## Lead: the per-claim request budget, from about 5 requests per sandbox to about 1

Requests for one sandbox that becomes Ready, has its pod looked up, and is deleted:

| Request kind | Per-claim path (`305e9703`) | Batch path (`25/N`) |
| :-- | :-- | :-- |
| Create | 1 POST | 1 POST, retry only on transient error |
| Readiness | 1 watch per claim | Shared: 1 list + 1 watch, reopened about every 30 s |
| Find the pod | 1 Sandbox GET per lookup, +1 on first command | 0 in pod-IP modes (seeded from watch) |
| Liveness | none | Shared: Lease create, 1 PUT per `lease_duration/3`, delete |
| Prechecks | none | Shared: 1 GET per warm pool + 1 per distinct template, fanned out in parallel |
| Cleanup | 1 DELETE | Shared: 1 `deletecollection` + 1 paged live count per round, usually 1 round |
| **Per sandbox** | **5** (1,500 for 300 cold, 800 for 160 warm); **6 per rollout** | **1 + shared/N**: 1.03 cold, 1.05 warm, 1.54 RL shape; **2.0 per rollout** |

**Why it works.** Creating the `SandboxClaim` is the only write each sandbox individually needs. Readiness, liveness, and cleanup are batch-wide questions, so one label-scoped watch, one `Lease`, and one `deletecollection` answer them for all $N$ claims while carrying each pod IP on the watch stream. Every shared cost divides by $N$, so per-claim API cost approaches $1$ as $N$ grows.

**Deep dive:** [WATCH-1](field-guide/watch.md#watch-1), [K8S-8](field-guide/k8s.md#k8s-8), [PERF-12](field-guide/perf.md#perf-12), [PERF-8](field-guide/perf.md#perf-8)

---

## Scoreboard

### Part I: Python SDK Batch Optimizations (`1/N` to `25/N`)

| # | Optimization (PR) | Before -> after | Evidence |
| :-- | :-- | :-- | :-- |
| [OPT-1](#opt-1) | One shared label-scoped batch watch (`1/N`) | $N$ watches -> $1$ | Watch peak `3 vs 302` cold; GKE sockets `44 vs 488` |
| [OPT-2](#opt-2) | Multi-round `deletecollection` teardown (`12/N`) | $N$ `DELETE`s -> $1$ per round | `1` call for `10k` claims; GKE `1k` cleanup `1.10 s vs 35.93 s` (`32.5x`) |
| [OPT-3](#opt-3) | Warm-aware pacing via `WarmCredit` (`19/N`) | Warm claims wait on `create_rps` -> start immediately | GKE `640` warm `8.73 s vs 19.54 s` (`2.24x`); `183.5 claims/s` |
| [OPT-4](#opt-4) | Drain unread `deletecollection` body raw (`20/N`) | `69.4 MB -> 0.2 MB` `release()` peak (`N=5,000`) | `347x` peak drop (`pr_board.md` `20/N`) |
| [OPT-5](#opt-5) | Drop unused `metadata.managedFields` per page (`20/N`) | `48.7 MB -> 24.1 MB` list peak (`N=5,000`) | `50.6%` of claim bytes removed (`C1-pr8-impl.md`) |
| [OPT-6](#opt-6) | Paginated claim lists and page-folded live count (`20/N`) | Timeout release `81.7 MB -> 11.8 MB` (`9.9 MB` at `10k`) | $O(\text{page})$ memory instead of $O(N)$ |
| [OPT-7](#opt-7) | `min_ready`-first create ordering (`12/N`) | Round-robin `0.65 s` first group -> immediate quorum | Analytical: group 0 finishes after its own `min_ready` |
| [OPT-8](#opt-8) | Single-use watch-seeded pod IP in `connect()` (`17/N`) | $1$ `Sandbox` `GET` per member -> $0$ | `0 vs 300` `GET`s on round-1 fan-out |
| [OPT-9](#opt-9) | Pool sizing `>= max_in_flight + 2` (`17/N`) | Sync drops sockets / async starves Lease -> `0` discards | `0/6` runs discard sockets vs `6/6` |
| [OPT-10](#opt-10) | Skip warm-credit refresh `GET` when pacer slot is open (`19/N`) | `2.01 -> ~1` req/env; `998 -> <= max_in_flight` `GET`s | Eliminates zero-benefit `GET` storms on GKE |
| [OPT-11](#opt-11) | Single-float lock-free-sleep rate limiter & local backoff (`12/N`, `13/N`) | Transient blip stalls `20` workers -> `1` worker backs off | Tracks target `20.0..485.2/s` cleanly |
| [OPT-12](#opt-12) | $O(1)$ incremental quorum & fill accounting (`13/N`) | $O(N^2)$ failure storm, $O(G)$/wake -> $O(1)$/event | Flat `~0.06` CPU cores across `N=1k..10k` |
| [OPT-13](#opt-13) | Per-claim `Event` waiters on `acquire()` (`16/N`) | $O(N \cdot K)$ condition wakeups -> $1$ wake per event | Analytical $O(N)$ total wakeups |
| [OPT-14](#opt-14) | Blind Lease renewal `PUT` without pre-`GET` (`3/N`) | `GET` + `PUT` -> `PUT` (`GET` only on `409`) | Cuts heartbeat API traffic by `50%` |
| [OPT-15](#opt-15) | Watch bookmarks (`allowWatchBookmarks=True`) (`1/N`) | Idle `410 Gone` + $N$-claim re-list -> free RV advance | Avoids full re-lists on quiet batches |
| [OPT-16](#opt-16) | Single-flight per-pool warm-credit refresh (`19/N`) | $K$ concurrent pool `GET`s -> $1$ `GET`/pool/s | Bounds refresh traffic per pool |
| [OPT-17](#opt-17) | Single-flight first-use `acquire()` pool checks (`16/N`, `25/N`) | $K$ checks per new pool -> $1$ shared check | Shielded across async task cancellations |
| [OPT-18](#opt-18) | Reuse precheck reads across credit, templates, and caches (`12/N`, `16/N`, `19/N`) | Extra `GET`s per pool/template -> `0` | Prechecks take `1.9%` of fill at `1,000` groups |
| [OPT-19](#opt-19) | Hold `max_in_flight` semaphore only during live I/O (`12/N`, `13/N`) | Backoff sleepers hold concurrency slots -> live I/O only | Keeps pipe full during retries |
| [OPT-20](#opt-20) | Shed doomed creates before ordinal, credit, or slot (`16/N`, `19/N`) | Wasted POST + cleanup DELETE -> `0` requests | Unit-verified zero network calls |
| [OPT-21](#opt-21) | Cache member `Sandbox` connections with watch invalidation (`6/N`, `17/N`, `24/N`) | Fresh connector + SSL parse per `connect()` -> cached | `300` connectors + `1` `SSLContext` for `300` members |
| [OPT-22](#opt-22) | $O(1)$ lazy tombstones, revoked outcomes, and `_members_by_pool` (`15/N`, `24/N`, `25/N`) | $O(N)$ queue/member scans -> $O(1)$ / $O(\text{pool})$ | Instruction ratio `1.000` across $N$ |
| [OPT-23](#opt-23) | Deadline-sorted single-pass Lease reaper waits (`18/N`) | Sum of renew intervals -> longest single interval | `30 s` instead of `50 s` on mixed Leases |
| [OPT-24](#opt-24) | Bulk per-claim `DEBUG` logging (`12/N`, `15/N`) | $2N$ `INFO` log lines -> aggregate milestones only | Eliminates log formatting bottleneck |
| [OPT-29](#opt-29) | Parallel deduplicated `_precheck_pools` & whole-batch `release_members()` (`25/N`) | $2G$ serial precheck RTTs & $G$ group sweeps -> parallel + `1` sweep | Keeps Lease/watch alive across RL waves |

### Part II: RL Integration & GKE Batch-Mode Optimizations (`27/N` + Campaign 5)

| # | Optimization (Branch / Commit) | Before -> after | Evidence |
| :-- | :-- | :-- | :-- |
| [OPT-25](#opt-25) | Shared `cluster.core_api` pool for REST pod status reads (`27/N`, `53caa99e`) | $1$ `PoolManager`/thread -> $1$ shared pool | Part of `11.1x` GKE socket drop (`488 -> 44`) |
| [OPT-26](#opt-26) | In-memory `BatchWatchdog` on warm path (`27/N`, `05b0a63b`) | $N$ Pod `GET`s on warm claims -> `0` `GET`s | `0` watchdog `GET`s on warm GKE bursts |
| [OPT-27](#opt-27) | Bounded-parallel `acquire_batch` / `release_all` + rollback (`27/N`, `c9600379`) | $N$ serial RTTs -> bounded parallel fan-out | `3.81x` `8k` R2E-Gym wall (`21.7m -> 5.7m`) |
| [OPT-28](#opt-28) | Memoize on-demand pools + auto-size HTTP/executor pools (`27/N`, `6d773276`, `e7ed074b`) | `2` RPCs/acquire & `1,024`-thread cap -> `0` repeat RPCs, `4,096` cap | `0` errors at `c=1,000..2,350` on GKE |
| [OPT-30](#opt-30) | Zero-`GET` `acquire()`, watch-backed `is_alive()`, & `size=0` seed batch (`27/N`, `e4ba2706`) | Eager Pod `GET` + `1,000` serial prechecks -> `0` hot-path `GET`s | Eliminates `GET` per acquire & step boundary |
| [OPT-31](#opt-31) | Direct background `Sandbox` + `Pod` delete on release (`c5`, `2eb5ef6d`, `3a14c1dc`) | K8s GC lag stalls warm-pool refill -> immediate slot reclaim | R2E `500x2x8` `~1,007 s -> ~223 s` (`4.5x`) |
| [OPT-32](#opt-32) | Skip `aiohttp` per-request proxy and `.netrc` lookups (`c5`, `feffbac2`, `03390885`) | `23` driver threads -> `3` threads; `-10-20%` CPU | `1,000`-claim burst `191-211/s -> ~240/s` |
| [OPT-33](#opt-33) | Event-loop-native batch claims (`AsyncBatchClaims`) (`c5`, `b386d881`) | Thread-pool hop per claim op -> native `asyncio` | Eliminates executor scheduling overhead |
| [OPT-34](#opt-34) | Multiplexed event-loop WebSocket pod exec (`handle.aexec()`) (`c5`, `b386d881`) | `1` thread + `ApiClient`/exec -> shared async WS client | `c=1,000`: `~3,000` threads / `~1,050` sockets -> `3` / `68` |
| [OPT-35](#opt-35) | Prewarm `64` keep-alive TLS connections per cluster (`c5`, `93f93fc9`) | Cold TCP+TLS handshakes on first burst -> pre-established | `c=1,000` burst + first exec `12.5 s -> 5.4 s` |
| [OPT-36](#opt-36) | Raw watch pass-through & zero-decode create/delete drains (`c5`, `b386d881`) | Model serialize + JSON decode -> raw dict / byte drain | Cuts event-loop CPU under `1,000`-claim bursts |
| [OPT-37](#opt-37) | Single paginated namespace `LIST` for warm pools (`c5`, `b386d881`) | $O(P)$ per-pool `GET`s -> `1` paginated `LIST` | Eliminates `500`-pool precheck/refresh fan-out |
| [OPT-38](#opt-38) | Hybrid release coalescing & sibling-aware deferred tail release (`c5`, `b386d881`, `2f87f7a9`) | Blind `deletecollection` or premature/stalled tail -> adaptive | Never starves a waiting sibling `acquire()` |
| [OPT-39](#opt-39) | Pipelined readiness seeding & `250 ms` prefetch yield (`c5`, `5f87abe9`) + tuned defaults (`50e4531b`) | Prefetch competes with active step -> foreground wins | `c=500`: `267/s vs 91/s`; TTFI P50 `2.9 s vs 10.0 s` |

---

# Part I: Python SDK Batch Optimizations (In Depth)

## 1. Fewer API requests

Ask once per batch, not once per claim, and derive answers from state you already hold.

<a id="opt-1"></a>

### OPT-1 One shared label-scoped watch for the whole batch (`1/N`)

- **What bottleneck it hits:** In `per_claim` mode, `wait_for_claim_ready` opens a dedicated field-selected watch (`metadata.name=<claim>`) per sandbox. Claiming $N$ sandboxes opens $N$ concurrent HTTP/1.1 streaming connections, exhausting `urllib3`/`aiohttp` connection pools, burning file descriptors, and forcing kube-apiserver to fan out watch serialization across $N$ watchers ([GKE-10](gke-benchmarking.md#gke-10)).
- **How it works under the hood:** Every claim created by the batch carries `agents.x-k8s.io/batch-id=<batch-id>` (plus `agents.x-k8s.io/batch-lease-uid=<uid>` since `22/N`). One background `_watch_loop` thread/task first lists matching claims to obtain a consistent `resourceVersion` snapshot, then opens a single `watch_sandbox_claims(label_selector=..., resource_version=rv, timeout_seconds=30)` stream that delivers readiness, pod IPs, terminal failures, and deletions for all $N$ claims into `BatchState.upsert_claim` / `remove_claim`.
- **Safety guard:** If etcd compacts history (`410 Gone`) or the socket drops (TLS EOF, connection reset), `_watch_loop` re-lists paged claims without a `resourceVersion` and calls `BatchState.reconcile_list`, diffing live server state against cached members so zero transitions or deletions slip through the reconnect gap ([WATCH-1](field-guide/watch.md#watch-1), [WATCH-3](field-guide/watch.md#watch-3)).
- **Measured result:** Apiserver watch peak `3 vs 302` on `300` cold claims (`60x` fewer connections). On GKE (`200x5` `1,000`-sandbox warm burst), client sockets dropped `11.1x` (`488 -> 44`) and `3,000`-task SWE-bench `claim p99` dropped `7.24x` (`139.66 s -> 19.30 s`).
- **Code:** `SandboxBatch._watch_loop`, `K8sHelper.watch_sandbox_claims`. **Deep dive:** [WATCH-1](field-guide/watch.md#watch-1), [WATCH-3](field-guide/watch.md#watch-3), [GKE-10](gke-benchmarking.md#gke-10).

<a id="opt-2"></a>

### OPT-2 Multi-round `deletecollection` teardown (`12/N`)

- **What bottleneck it hits:** Deleting $N$ claims individually costs $N$ `DELETE` RPCs, hitting client rate limits and Kubernetes API Priority and Fairness (APF) seats during wave teardown.
- **How it works under the hood:** Because every claim in the batch carries the batch label (and per-pool claims carry `agents.x-k8s.io/warm-pool-name` or `batch-pool-hash`), `release()`, `release_members()`, and `release_group()` issue a single label-selected `delete_collection_namespaced_custom_object` call (`_delete_claims_in_rounds`).
- **Safety guard:** In Kubernetes, `deletecollection` lists matching objects at a point in time and deletes that snapshot; an in-flight `create` landing right after the snapshot, or a 60-second apiserver gateway `504` mid-sweep, leaves stragglers behind. `_delete_claims_in_rounds` therefore first stops/fences new creates, re-verifies or re-takes the batch Lease via `_take_batch_lease`, sends `deletecollection`, and counts remaining non-terminating claims via a consistent paged read (`count_live_sandbox_claims`), repeating until `live == 0` or `BATCH_RELEASE_MAX_IDLE_ROUNDS = 3` rounds make zero progress.
- **Measured result:** `1` `deletecollection` deletes `10,000` claims in 1 round on kind. On GKE (`1,000` live claims), `batch.release()` finished in `1.10 s` best / `1.44 s` mean (`4` total API calls) vs `35.93 s` for `delete_all()` (`32.5x` peak faster).
- **Code:** `SandboxBatch._delete_claims_in_rounds`. **Deep dive:** [K8S-8](field-guide/k8s.md#k8s-8), [RETRY-11](field-guide/retry.md#retry-11), [RACE-8](field-guide/race.md#race-8).

<a id="opt-8"></a>

### OPT-8 Single-use watch-seeded pod IP in `connect()` (`17/N`)

- **What bottleneck it hits:** Direct pod-IP connectors (`SandboxDirectConnectionConfig`) need the pod's IP before sending commands. Previously, `Sandbox` always called `get_sandbox` (`1` `GET` per member) on the first command even though the batch watch had already received `status.podIPs` and `status.sandbox.name` on the `SandboxClaim` `READY` event!
- **How it works under the hood:** `SandboxBatch.connect(member)` extracts `select_pod_ip(member.pod_ips)` and `member.sandbox_name` from the watched `Member` and injects `sandbox._seeded_pod_ip = ip` onto the constructed `Sandbox` / `AsyncSandbox` handle without making any Kubernetes API call. On first command, `_resolve_pod_ip()` atomically consumes the seed:
  ```python
  seeded, self._seeded_pod_ip = self._seeded_pod_ip, None
  if seeded is not None:
      return seeded
  ```
- **Safety guard:** Making the seed **single-use** prevents routing forever to a dead IP if the pod restarts or changes IP later: if an HTTP/gRPC request to the seeded IP fails, the connector invalidates its cached transport and calls `_resolve_pod_ip()` again, which sees `_seeded_pod_ip is None` and falls back to a live `get_sandbox()` `GET`. In parallel, `_watch_loop` closes cached connectors whenever a watched member's `sandbox_name` or `pod_ip` changes ([WATCH-8](field-guide/watch.md#watch-8)).
- **Measured result:** `0` `Sandbox` `GET`s vs `300` `GET`s across `300` connected members on round-1 command fan-out.
- **Code:** `SandboxBatch.connect`, `Sandbox._resolve_pod_ip`. **Deep dive:** [PERF-12](field-guide/perf.md#perf-12), [WATCH-8](field-guide/watch.md#watch-8).

<a id="opt-14"></a>

### OPT-14 Blind Lease renewal `PUT` without pre-`GET` (`3/N`)

- **What bottleneck it hits:** Renewing a Kubernetes `Lease` naively (`GET` current Lease -> mutate `renewTime` -> `PUT`) costs 2 sequential RTTs every `lease_duration / 3` (`20 s`), doubling heartbeat API load and widening the window where slow networks miss renewal deadlines.
- **How it works under the hood:** Because only the active holder should be mutating the `Lease`, `SandboxBatch` caches the `V1Lease` returned by its own last successful create/renewal (`self._lease`, carrying the latest `metadata.resourceVersion`). `_renew_once` updates `spec.renew_time` on a copy of `self._lease` and sends `replace_namespaced_lease` (`PUT`) directly in **1 RTT**.
- **Safety guard:** Kubernetes optimistic concurrency control (`resourceVersion` matching in etcd) rejects the `PUT` with `409 Conflict` if *anything* changed on the server. Only on `409` does `_renew_once` perform a `GET`: if `uid`, `holderIdentity`, and `leaseTransitions` prove this handle is still the legitimate owner (meaning our own prior `PUT` succeeded on the server after its HTTP response timed out), it updates `self._lease` and retries the `PUT` once; otherwise another client took over or detached the Lease and renewal stops immediately ([LEASE-28](field-guide/lease.md#lease-28)). Sync Lease calls run over `_UnretriedApiClient(retries=False)` so `urllib3` never silently retries a timed-out `PUT` into a synthetic `409`.
- **Measured result:** Cuts steady-state Lease renewal RPCs by `50%` (`2 -> 1` RTT per renewal).
- **Code:** `SandboxBatch._renew_once`, `_write_renewal`. **Deep dive:** [PERF-8](field-guide/perf.md#perf-8), [LEASE-28](field-guide/lease.md#lease-28).

<a id="opt-15"></a>

### OPT-15 Watch bookmarks (`allowWatchBookmarks=True`) (`1/N`)

- **What bottleneck it hits:** `_watch_loop` reconnects cleanly every `_WATCH_TIMEOUT_SECONDS = 30 s` using the highest `resourceVersion` (`_last_rv`) it has seen. On an idle batch (all sandboxes already `READY` during a long multi-minute RL episode) while *other* namespaces write heavily to etcd, the batch watch receives zero claim events, so `_last_rv` goes stale, etcd compacts past it, and the next watch open fails with `410 Gone`, forcing a full $N$-claim paginated re-list!
- **How it works under the hood:** `watch_sandbox_claims` passes `allow_watch_bookmarks=True`. Kube-apiserver periodically sends lightweight `BOOKMARK` frames containing only `{"type": "BOOKMARK", "object": {"metadata": {"resourceVersion": "<fresh>"}}}` with zero claim payload. `_watch_loop` advances `_last_rv` on `BOOKMARK` events without touching `BatchState`.
- **Safety guard:** Bookmark frames never mutate member state, and if a genuine `410 Gone` still occurs (e.g., after a long network partition), `_watch_loop` still executes the full paged re-list + `reconcile_list` fallback.
- **Measured result:** Eliminates idle-reconnect `410 Gone` storms and full $N$-claim re-lists during long-running workloads.
- **Code:** `K8sHelper.watch_sandbox_claims`, `SandboxBatch._watch_loop`. **Deep dive:** [K8S-10](field-guide/k8s.md#k8s-10), [WATCH-1](field-guide/watch.md#watch-1).

<a id="opt-18"></a>

### OPT-18 Reuse precheck reads across warm credit, template dedup, and `_checked_pools` (`12/N`, `16/N`, `19/N`)

- **What bottleneck it hits:** Three separate features need warm-pool metadata: pre-flight validation (`claim_batch` failing fast before writing a Lease), `WarmCredit` seeding (`status.readyReplicas`), and `acquire()` pool existence checks. Fetching them separately would triple `GET`s per warm pool, and fetching `SandboxTemplate` per pool would repeat `GET`s when hundreds of warm pools share one template.
- **How it works under the hood:** One `get_sandbox_warmpool` read in `_precheck_pools` does three jobs at once:
  1. Validates the pool exists and extracts `spec.sandboxTemplateRef.name`.
  2. Deduplicates template `GET`s via `checked_templates: set[str]` so $P$ pools sharing 1 template cost $P + 1$ `GET`s instead of $2P$.
  3. Parses `status.readyReplicas` via `warmpool_ready_replicas()` to seed `WarmCredit`, and populates `self._checked_pools` on the handle (including pools reconstructed from existing claims during `get_batch()` attach!) so subsequent `acquire()` calls against known pools send **0** discovery `GET`s.
- **Safety guard:** Only a *completed, successful* pool + template check inserts the pool into `_checked_pools`; transient `5xx` or missing-template errors never poison the cache ([PERF-11](field-guide/perf.md#perf-11)).
- **Measured result:** Prechecks accounted for just `1.9%` of total fill time even at `1,000` groups on kind, and `0` extra `GET`s on `acquire()` for pre-declared pools.
- **Code:** `_precheck_pools`, `SandboxBatch.__init__`, `warmpool_ready_replicas`. **Deep dive:** [PERF-10](field-guide/perf.md#perf-10), [PERF-11](field-guide/perf.md#perf-11).

<a id="opt-29"></a>

### OPT-29 Parallel deduplicated `_precheck_pools` and whole-batch `release_members()` (`25/N`, `#2252`)

- **What bottleneck it hits:**
  1. **Serial precheck RTTs:** When `claim_batch()` is called with $G$ distinct warm pools (e.g., `200` task pools), running `_precheck_pools` sequentially pays $G$ (up to $2G$) serial network RTTs (`200 * 5 ms = 1+ second` of pure idle waiting) before creating the Lease or sending the first claim!
  2. **Wave reset overhead:** When an RL trainer or multi-wave runner wants to clear all claims across $G$ warm pools between waves *while reusing the same batch*, calling `release_group(pool)` $G$ times sends $G$ separate `deletecollection` + `count_live` sweeps, whereas calling `release()` + `claim_batch()` tears down and recreates the Lease, restarts the watch stream, and throws away `_checked_pools`!
- **How it works under the hood:**
  1. **Parallel `_precheck_pools(..., max_concurrency=max_in_flight)`:** Deduplicates input pool names in order (`list(dict.fromkeys(warmpools))`), shares `checked_templates`, and fans out `check_pool` concurrently over `ThreadPoolExecutor(min(len(pools), max_concurrency))` on sync and `BoundedSemaphore(max_concurrency)` + `asyncio.gather(..., return_exceptions=True)` on async.
  2. **Whole-batch `release_members()`:** Calls `self._release_pools(None)`, which cancels remaining planned fill creates, transitions all active/pending claims into `self._state._released` under `self._changed`, fences concurrent `acquire()` calls until the sweep finishes, and runs **one** batch-wide label-scoped `deletecollection` sweep (`batch_claim_label_selector(batch_id, lease_uid)`) while keeping the batch handle, its `Lease` renewal loop, its watch stream, and `_checked_pools` open for the next wave of `acquire()` calls!
- **Safety guard:** Parallel prechecks collect all outcomes and inspect them in **caller input order** (`for name, result in zip(pools, results)`), raising the exact first failure a sequential check would have raised so caller-visible error determinism never depends on which HTTP call returned faster. During `release_members()`, `BatchState._release_sweeps` blocks new `acquire()` calls from creating claims carrying the batch label mid-sweep (which the running `deletecollection` would immediately delete!), and revokes any unconsumed `wait_for_quorum()` or `group_outcomes()` results holding those members.
- **Measured result:** Collapses $O(G)$ serial precheck latency to $O(G / \text{max\_in\_flight})$ RTTs, and reduces multi-pool wave resets from $G$ `deletecollection` sweeps (or full batch teardown/re-creation) to `1` `deletecollection` sweep.
- **Code:** `_precheck_pools`, `SandboxBatch.release_members`, `SandboxBatch._release_pools` (`#2252`).

---

## 2. Less waiting (pacing & scheduling)

Pace only the work that loads the bottleneck resource, and schedule urgent items first.

<a id="opt-3"></a>

### OPT-3 Warm-aware pacing via `WarmCredit` (`19/N`, `#2166`)

- **What bottleneck it hits:** `create_rps` (`50/s` default) exists to keep a cold burst from starting hundreds of pods simultaneously on kubelets and containerd (`10.2 s` unpaced vs `1.35 s` paced to first Ready on cold pods). However, claiming from a **warm pool** with `readyReplicas > 0` adopts an *already-running* pod and starts zero containers! Pacing warm claims at `50/s` artificially throttled `160` warm claims to `3.24 s` (slower than `per_claim`'s `1.46 s`!).
- **How it works under the hood:** `WarmCredit` tracks an integer credit balance per warm pool, seeded from `status.readyReplicas` during `_precheck_pools` ([OPT-18](#opt-18)). Before reserving a rate-limiter slot, `_create_worker` and `acquire()` check `covered = now < deadline and self._warm_credit.take(warmpool)`:
  - If `covered` is `True`, `slot = now`: the create fires immediately (still bounded by `max_in_flight`) and **never advances `self._next_create_at`**, leaving the pacing budget untouched for genuinely cold claims!
  - If `covered` is `False`, the worker reserves a paced slot via `_reserve_create_slot(now)` and still calls `_warm_credit.spend(warmpool)` if credit arrived concurrently so cached credit never drifts above reality.
- **Safety guard (non-latching `429` cooldown):** If kube-apiserver pushes back with `429 Too Many Requests`, a naive design that zeroes `WarmCredit` permanently ("latching") caused one early APF blip on GKE to lock a `1,280`-claim burst at `50/s` for `24.6 s`! Instead, `WarmCredit.on_throttle(now, retry_after)` enters a temporary cooldown `until = now + max(retry_after, 5.0)`: creates pace normally during the window (continuing to debit credit per create), and once the window expires, any remaining true warm credit resumes unpaced automatically!
- **Measured result:** On GKE (`640` warm sandboxes across `160` pools), all-Ready dropped from `19.54 s` (`per_claim`) to `8.73 s` (`2.24x` faster, `603 ms vs 3,120 ms` first Ready, zero `429`s). On `Bench 2A` (`1x1,000` warm pool), hit `183.5 claims/s` peak (`2.2x-2.8x` faster than baseline across `c=100..1,000`).
- **Code:** `batch_utils.WarmCredit`, `SandboxBatch._create_worker`, `SandboxBatch.acquire`. **Deep dive:** [RETRY-15](field-guide/retry.md#retry-15), [RETRY-16](field-guide/retry.md#retry-16), [PERF-13](field-guide/perf.md#perf-13), [PERF-21](field-guide/perf.md#perf-21).

<a id="opt-10"></a>

### OPT-10 Skip warm-credit refresh `GET` when the pacer slot is already open (`19/N`, `#2166`)

- **What bottleneck it hits:** Once initial `WarmCredit` hits `0` on a long-lived dynamic batch (as the controller refills warm pools in the background), `acquire()` can refresh `status.readyReplicas` via `get_sandbox_warmpool` (`1` `GET` per second per pool) to see if new warm pods are ready. On GKE with `500` single-replica pools at moderate rate, refreshing unconditionally whenever `credit == 0` doubled API traffic (`2.01` requests/env) and fired up to `998` concurrent `GET`s, **even when `self._next_create_at <= now`** (meaning the rate limiter wouldn't have slept anyway!).
- **How it works under the hood:** Inside the same `with self._lock:` critical section as `take()`, `acquire()` checks whether the pacer would actually delay this create before requesting a refresh:
  ```python
  need_refresh = (
      not covered
      and not refreshed
      and now < deadline
      and self._next_create_at > now
      and self._warm_credit.refresh_due(warmpool, now)
  )
  ```
  If `self._next_create_at <= now`, the create slot is already `now` (`0 ms` wait), so paying a network RTT to ask whether we can skip a `0 ms` wait is pure waste!
- **Safety guard:** When `need_refresh` *is* true (`_next_create_at > now`), the refresh `GET` acquires `self._in_flight` (`BoundedSemaphore(max_in_flight)`) just like a create, guaranteeing total in-flight requests never breach `pool_size - 2` ([OPT-9](#opt-9)).
- **Measured result:** Eliminated the `998`-`GET` burst on `500`-pool GKE runs and kept steady-state `acquire()` at `~1` request/sandbox whenever `acquire` arrival rate <= `create_rps`.
- **Code:** `SandboxBatch.acquire`. **Deep dive:** [RETRY-17](field-guide/retry.md#retry-17), [PERF-13](field-guide/perf.md#perf-13).

<a id="opt-16"></a>

### OPT-16 Single-flight per-pool warm-credit refresh (`19/N`, `#2166`)

- **What bottleneck it hits:** When `50` concurrent `acquire("pool-a")` calls arrive at once after `pool-a`'s cached warm credit hits `0` and `_next_create_at > now`, all `50` callers would simultaneously see `credit == 0` and fire `50` duplicate `GET /sandboxwarmpools/pool-a` requests!
- **How it works under the hood:** `WarmCredit` maintains `_refreshing: set[str]` and `_refreshed_at: dict[str, float]` under `self._lock`. `refresh_due(pool, now)` returns `True` only if `pool not in _refreshing` and `now - _refreshed_at.get(pool, -inf) >= 1.0`, atomically adding `pool` to `_refreshing`. The single winning caller performs the `GET` under `self._in_flight` and calls `finish_refresh(pool, count, now)` (or `cancel_refresh(pool)` on error) in `finally`, depositing the fresh `readyReplicas` credit for its peers.
- **Safety guard:** `cancel_refresh` runs in `BaseException` / `finally` blocks so a `KeyboardInterrupt`, `CancelledError`, or timeout never leaves a pool permanently stuck in `_refreshing`.
- **Measured result:** Caps warm-pool credit discovery to at most `1 GET/pool/s` regardless of `acquire()` concurrency $K$.
- **Code:** `batch_utils.WarmCredit.refresh_due`, `finish_refresh`, `cancel_refresh`. **Deep dive:** [LOCK-16](field-guide/lock.md#lock-16).

<a id="opt-7"></a>

### OPT-7 `min_ready`-first create ordering (`12/N`, `#1824`)

- **What bottleneck it hits:** When `claim_batch` fills $G$ groups (`BatchGroup(warmpool, size, min_ready)`) through one shared paced pipe, round-robin ordering (`k / size`) spreads creates evenly across all groups' spare tail replicas, so group 0's `min_ready`-th claim isn't sent until near the end of the entire batch (`create 121 of 160` in the `40x4` RL benchmark, taking `0.65 s` vs `0.16 s` per-claim!). Yet we cannot scramble claim names (`<batch-id>-<ordinal>`), because `get_batch()` reconstructs original group order from contiguous ordinal blocks per pool!
- **How it works under the hood:** `fill_create_plan(groups)` decouples **claim identity (ordinal assignment)** from **network send order**:
  1. First, it assigns each group $i$ a contiguous block of ordinals `[start_i, start_i + size_i)` so `<batch-id>-0 .. <batch-id>-(size_0-1)` belong to group 0, etc.
  2. Next, it sorts the planned create tasks by key `(k >= min_ready, group_index, k)` where `k` is the member's `0`-based offset within its group!
  Every group's `0 .. min_ready - 1` creates go out first (finishing group 0's quorum, then group 1's quorum, ...), and only after *all* groups have their `min_ready` claims in flight do spare capacity creates (`k >= min_ready`) go out!
- **Safety guard:** Ordinals remain strictly contiguous per group and monotonic across the batch, so `derive_groups_from_claims` on re-attach (`get_batch`) reconstructs exact group order from the minimum surviving ordinal per pool ([K8S-18](field-guide/k8s.md#k8s-18)).
- **Measured result:** Group 0 reaches quorum after its own `min_ready` creates (`4` creates instead of `121` in the `40x4` RL shape) with zero increase in total batch fill time.
- **Code:** `batch_utils.fill_create_plan`. **Deep dive:** [PERF-14](field-guide/perf.md#perf-14), [PAT-29](patterns.md#pat-29), [K8S-18](field-guide/k8s.md#k8s-18).

<a id="opt-11"></a>

### OPT-11 Single-float lock-free-sleep rate limiter & scoped transient backoff (`12/N`, `13/N`)

- **What bottleneck it hits:** Classic token-bucket rate limiters either wake background refill timers or hold a mutex while sleeping, and naive global error backoff stalls *all* `20` create workers whenever *one* socket hits a transient TCP reset or single-request `500`.
- **How it works under the hood:** The entire `create_rps` pacer is a single monotonic timestamp float `self._next_create_at` (`_create_interval = 1.0 / create_rps`):
  ```python
  # Under self._lock (nanoseconds, zero I/O or sleep):
  slot = max(earliest, self._next_create_at)
  self._next_create_at = slot + self._create_interval
  # Outside self._lock: sleep(slot - now)
  ```
  - When idle, `_next_create_at` falls behind `now`, so `max(now, _next_create_at) == now`: zero burst credit accumulates silently while idle!
  - **Scoped backoff:** Only an explicit server-wide `Retry-After` header (`429` / `503`) pushes the shared `_next_create_at` forward (`_push_back_creates`), throttling all workers together. A transient error *without* `Retry-After` (e.g. a dropped connection or `500`) computes jittered exponential backoff **locally** inside that one worker (`earliest = now + delay`), then reserves a normal slot after sleeping so the other `19` healthy workers keep running at full speed!
- **Safety guard:** Every retry reserves a fresh slot via `_reserve_create_slot`, so aggregate request rate (creates + retries) never exceeds `create_rps`.
- **Measured result:** Delivered `20.0, 50.0, 99.9, 197.7, 485.2` creates/s for targets `20..500/s` (`bench tip #44`), while isolating single-connection blips to 1 worker.
- **Code:** `SandboxBatch._reserve_create_slot`, `_push_back_creates`. **Deep dive:** [RETRY-5](field-guide/retry.md#retry-5), [RETRY-6](field-guide/retry.md#retry-6), [PAT-17](patterns.md#pat-17).

<a id="opt-19"></a>

### OPT-19 Hold `max_in_flight` semaphore only during live I/O, never across backoff sleep (`12/N`, `13/N`)

- **What bottleneck it hits:** If a create worker acquires `self._in_flight` (`BoundedSemaphore(max_in_flight)`) for the entire create-and-retry loop, a worker sleeping `1-4 s` in exponential backoff holds onto an `in_flight` permit while doing zero network I/O! Under brief `503` / `429` blips, all `20` permits fill with sleeping coroutines/threads and live throughput collapses to zero.
- **How it works under the hood:** `_send_create` acquires `self._in_flight` tightly around the single `create_sandbox_claim` HTTP call and releases it immediately in `finally` *before* evaluating retry backoff or sleeping for the next pacing slot!
- **Safety guard:** Because `max_in_flight` bounds **concurrent live HTTP connections** (to protect `pool_size >= max_in_flight + 2` and apiserver concurrency seats), releasing the permit before sleeping preserves the exact connection-pool invariant while letting ready creates use the idle socket immediately.
- **Measured result:** Prevents retry-sleep head-of-line blocking across `max_in_flight` workers.
- **Code:** `SandboxBatch._create_one`, `_send_create`. **Deep dive:** [LOCK-10](field-guide/lock.md#lock-10).

<a id="opt-20"></a>

### OPT-20 Shed doomed creates before taking an ordinal, warm credit, or HTTP slot (`16/N`, `19/N`)

- **What bottleneck it hits:** When a group has already failed (`_fill_stopped_pools`), quorum has timed out, the Lease has expired, or `acquire()`'s deadline has already elapsed while waiting on a pacer slot, sending the `POST` anyway wastes a monotonic ordinal, consumes `WarmCredit`, occupies a `max_in_flight` socket, and creates an orphan `SandboxClaim` on the cluster that must immediately be deleted with an extra `DELETE` RPC!
- **How it works under the hood:** Both `_create_worker` and `acquire()` re-check health, pool release sweeps, Lease validity, and `time.monotonic() >= deadline` **at every stage boundary**:
  1. Before reserving a create ordinal or `WarmCredit`.
  2. Again right after waking from pacing/backoff sleep, *before* acquiring `_in_flight` or sending the `POST`.
- **Safety guard:** If a fill create is aborted before its first attempt ever touches the wire (`sent == False`), it records no `_pending_deletes` entry so `release()` sends zero unnecessary per-name `DELETE`s; if an earlier attempt *did* hit the wire with an ambiguous transport timeout (`sent == True`), its claim name stays tracked for cleanup ([RACE-3](field-guide/race.md#race-3)).
- **Measured result:** `0` wasted `POST` + `DELETE` pairs after deadline expiry, group cancellation, or `release()`.
- **Code:** `SandboxBatch._create_one`, `SandboxBatch.acquire`. **Deep dive:** [RETRY-7](field-guide/retry.md#retry-7), [RACE-3](field-guide/race.md#race-3).

---

## 3. Less memory

Never materialize or retain bytes nobody reads, and bound working sets by page size rather than batch size $N$.

<a id="opt-4"></a>

### OPT-4 Drain unread `deletecollection` response body raw (`20/N`, `#2175`)

- **What bottleneck it hits:** Kubernetes `DELETECOLLECTION` serializes and echoes back a `Status` / `List` JSON body containing **every single deleted `SandboxClaim` object** (including its full spec, status, labels, annotations, and `managedFields`). Neither `release()` nor `release_group()` ever reads that return value (they judge completion via `count_live_sandbox_claims`), yet `kubernetes` / `kubernetes_asyncio` eagerly read the entire response buffer, UTF-8 decoded it, and `json.loads`-parsed thousands of claim dicts just to discard the return value! At `N=5,000`, that unread response caused a **`69.4 MB` heap spike** during `release()`.
- **How it works under the hood:** `delete_sandbox_claims_by_label` passes `_preload_content=False` so the generated client returns the raw `urllib3.HTTPResponse` / `aiohttp.ClientResponse` without buffering or JSON-decoding:
  - **Sync (`k8s_helper.py`):** Streams fixed raw byte chunks via `resp.stream(65536, decode_content=False)` (skipping even gzip decompression!) and discards each chunk immediately so the socket returns cleanly to `urllib3`'s keep-alive pool.
  - **Async (`async_k8s_helper.py`):** Validates HTTP status manually (`_raise_for_status`, since `kubernetes_asyncio` skips status checks when `_preload_content=False`!), drains `resp.content.iter_any()`, and calls `resp.release()`.
- **Safety guard:** On sync, `urllib3`'s `release_conn()` returns a **corrupted/dirty socket** to the pool if an exception or `KeyboardInterrupt` aborts mid-body! We wrap the stream in a `try/finally` that calls `resp.close()` whenever `drained` is `False` before calling `resp.release_conn()` ([LIB-13](field-guide/lib.md#lib-13), [LIB-14](field-guide/lib.md#lib-14)).
- **Measured result:** `release()` peak memory at `N=5,000` dropped **`347x` (`69.4 MB -> 0.2 MB`)** in the common 1-round case!
- **Code:** `K8sHelper.delete_sandbox_claims_by_label`, `AsyncK8sHelper.delete_sandbox_claims_by_label`. **Deep dive:** [PERF-7](field-guide/perf.md#perf-7), [LIB-13](field-guide/lib.md#lib-13), [LIB-14](field-guide/lib.md#lib-14).

<a id="opt-5"></a>

### OPT-5 Drop unused `metadata.managedFields` as each page arrives (`20/N`, `#2175`)

- **What bottleneck it hits:** When `get_batch()` attaches or `_watch_loop` re-lists after a `410 Gone`, `list_sandbox_claim_objects` holds all listed claim dicts briefly to build `Member` objects. Profiling at `N=5,000` showed that **`50.6%` of every claim's JSON/dict memory** was `metadata.managedFields` (Kubernetes Server-Side Apply field-ownership tracking), which zero lines of SDK code ever read!
- **How it works under the hood:** As each 500-item page arrives in `list_sandbox_claim_objects`, we immediately strip `managedFields` in place before appending items to the accumulated list and before fetching the next page:
  ```python
  for item in page.get("items", []):
      (item.get("metadata") or {}).pop("managedFields", None)
  ```
  Python's allocator frees those nested dicts/lists before the next 500-item page is allocated!
- **Safety guard:** Verified via repo-wide audit (`git grep managedFields`) that no SDK or RL caller inspects `managedFields` on claim dicts.
- **Measured result:** At `N=5,000`, cut retained list memory from `46.4 MB -> 18.9 MB` and (combined with paging) cut list peak from `69.4 MB -> 24.1 MB` (`138.9 MB -> 43.6 MB` at `N=10,000`).
- **Code:** `K8sHelper.list_sandbox_claim_objects`. **Deep dive:** [PERF-6](field-guide/perf.md#perf-6).

<a id="opt-6"></a>

### OPT-6 Paginated claim lists and streaming page-folded live counts (`20/N`, `#2175`)

- **What bottleneck it hits:**
  1. Unpaged `LIST` of `10,000` claims allocates one giant raw HTTP buffer + UTF-8 string + parsed JSON dict (`138.9 MB` peak) simultaneously.
  2. Even worse, when `deletecollection` hits apiserver's 60-second `504` timeout on huge batches, `_delete_claims_in_rounds` re-listed the entire batch solely to check *how many claims still lacked `deletionTimestamp`* (`81.7 MB` peak at `N=5,000`)!
- **How it works under the hood:**
  1. **Consistent snapshot paging (`_claim_list_pages`):** Fetches pages of `CLAIM_LIST_PAGE_SIZE = 500` using `limit=500` and `continue=<token>`. Kube-apiserver serves all pages of a `continue` chain from the **exact same etcd snapshot `resourceVersion`** (captured from page 1), so starting the watch from page 1's RV remains 100% gap-free!
  2. **Streaming page-folded live count (`count_live_sandbox_claims`):** Iterates `_claim_list_pages`, increments an integer counter `live += sum(1 for c in items if not deletionTimestamp)`, and immediately drops each page (`del page`), keeping memory strictly **$O(\text{page}) = O(500)$** whether the batch has `500` or `50,000` claims!
- **Safety guard:**
  - Loops terminate **only** when `metadata.continue` is empty (`not continue_token`), never on an empty `items` list (Kubernetes filters can produce 0-item intermediate pages while more pages follow!).
  - If etcd compacts the snapshot mid-pagination (`410 Gone` on a `continue` token), `_claim_list_pages` restarts once from scratch and yields `first=True`, causing `count_live_sandbox_claims` to **reset `live = 0`** so earlier pages are never double-counted ([K8S-9](field-guide/k8s.md#k8s-9))!
- **Measured result:** On `deletecollection` timeout paths, `release()` peak fell **`14x` at `N=10,000` (`138.8 MB -> 9.9 MB`)** and `81.7 MB -> 11.8 MB` at `N=5,000` (`O(1)` in $N$).
- **Code:** `_claim_list_pages`, `K8sHelper.count_live_sandbox_claims`. **Deep dive:** [PERF-4](field-guide/perf.md#perf-4), [PERF-5](field-guide/perf.md#perf-5), [K8S-9](field-guide/k8s.md#k8s-9).

---

## 4. Less contention

Size shared pools for both burst traffic and background liveness loops, wake only the waiter that cares, and coalesce duplicate work.

<a id="opt-9"></a>

### OPT-9 Connection pool sizing `>= max_in_flight + 2` (`17/N`, `#2153`)

- **What bottleneck it hits:** `max_in_flight` worker requests share the client's HTTP connection pool with two long-lived / liveness-critical consumers: the streaming batch watch (`1` socket held continuously) and the Lease renewal loop (`1` socket borrowed every `D/3`).
  - On **async (`aiohttp.TCPConnector`)**, when all `limit` sockets are busy, new requests **block in a FIFO queue**: queuing Lease renewals behind slow creates until the Lease expires!
  - On **sync (`urllib3.PoolManager`)**, when `maxsize` is full (`block=False`), `urllib3` silently opens unpooled one-off TLS sockets and **discards/closes them** after every single request, paying a fresh TLS handshake per create!
- **How it works under the hood:** `BATCH_POOL_HEADROOM = 2` reserves 1 slot for the watch and 1 for Lease renewals. `check_pool_size` enforces `pool_size >= max_in_flight + 2`: if the caller left `max_in_flight` at its default (`20`) on a smaller pool (`>= 3`), it clamps `max_in_flight = pool_size - 2`; if an explicit `max_in_flight` doesn't fit, it raises `ValueError` up front before creating any Kubernetes resources.
- **Safety guard:** Validates per-batch consistency without permanently locking burst reservations across idle batches ([PERF-17](field-guide/perf.md#perf-17)), and bounds on-demand pool `GET`s under the exact same `max_in_flight` semaphore ([OPT-10](#opt-10)).
- **Measured result:** Eliminated `urllib3` connection discards (`6/6` runs discarded sockets at pool `100` vs `0/6` at `102`) and prevented `aiohttp` pool-induced Lease starvation.
- **Code:** `batch_utils.check_pool_size`, `BATCH_POOL_HEADROOM`. **Deep dive:** [PERF-16](field-guide/perf.md#perf-16), [PERF-17](field-guide/perf.md#perf-17).

<a id="opt-13"></a>

### OPT-13 Per-claim `Event` waiters on `acquire()` (`16/N`, `#1991`)

- **What bottleneck it hits:** Fill consumers (`events()`, `group_outcomes()`, `wait_for_quorum()`) care about batch-wide progress and sleep on a shared `Condition` (`self._changed`). If $K = 1,000$ concurrent `acquire()` calls also slept on `self._changed`, every single watch event arriving for claim $i$ would wake all $1,000$ threads/tasks (`notify_all()`), causing $999$ threads to contend on `self._lock`, see their claim isn't Ready yet, and go back to sleep: **$O(N \cdot K)$ (`1,000,000` lock acquisitions for `1,000` claims)**!
- **How it works under the hood:** Each `acquire()` installs a dedicated `threading.Event` / `asyncio.Event` in `self._acquire_waiters[claim_name]` *before* sending the create `POST`. When `_watch_loop` processes an update for `claim_name` (or `release_member` / `release_group` cancels it), `_wake_acquire(claim_name)` looks up that single `Event` in $O(1)$ and wakes **at most 1 caller**!
- **Safety guard:** Batch-wide state transitions (Lease loss, fatal watch stop, `410` re-list, `release_members()`) call `_wake_all_acquires()`, iterating `_acquire_waiters.values()` under `self._lock` so no `acquire()` hangs on a dead batch. Waiters clear their `Event` *under `self._lock`* before inspecting state so zero edge-triggered wakeups are lost ([PAT-13](patterns.md#pat-13)).
- **Measured result:** Reduces total `acquire()` wakeups and lock acquisitions from $O(N \cdot K)$ to **$O(N)$** (`1` wakeup per watch event).
- **Code:** `SandboxBatch._wake_acquire`, `_acquire_waiters`. **Deep dive:** [LOCK-12](field-guide/lock.md#lock-12), [PAT-13](patterns.md#pat-13).

<a id="opt-17"></a>

### OPT-17 Single-flight first-use `acquire()` pool checks (`16/N`, `25/N`)

- **What bottleneck it hits:** On a dynamic batch opened with `size=0` (like `agent-sandbox-rl`), `50` tasks targeting a new warm pool `"pool-x"` may call `acquire("pool-x")` simultaneously. Without coordination, all `50` callers see `"pool-x" not in self._checked_pools` and fire `100` redundant `GET`s (`50` WarmPool + `50` Template).
- **How it works under the hood:** `self._prechecks` implements a **single-flight map** (`dict[str, _PoolPrecheck]` on sync; `dict[str, asyncio.Task]` on async):
  - The first caller to arrive for `"pool-x"` registers an in-flight entry under `self._lock` and runs `_precheck_pools` under `self._in_flight`.
  - Concurrent callers for `"pool-x"` see the in-flight entry, release `self._lock`, and await that same completion event/task, sharing its outcome (or exception) in **1 pair of `GET`s** instead of $K$!
- **Safety guard (`asyncio.shield` in `25/N`):** On `asyncio`, awaiting a shared `Future`/`Task` directly cancels the shared RPC for *all* `49` other waiters if waiter #1's task gets cancelled! `AsyncSandboxBatch._check_pool` spawns a background `Task` and awaits `asyncio.shield(task)`, re-raising `CancelledError` only for the cancelled caller while letting surviving waiters finish using the single shared RPC ([CANCEL-18](field-guide/cancel.md#cancel-18))!
- **Measured result:** Collapses $2K$ first-use pool validation `GET`s to `2` `GET`s per distinct pool.
- **Code:** `SandboxBatch._check_pool`, `AsyncSandboxBatch._check_pool`. **Deep dive:** [LOCK-15](field-guide/lock.md#lock-15), [CANCEL-18](field-guide/cancel.md#cancel-18).

<a id="opt-21"></a>

### OPT-21 Cache member `Sandbox` connections with watch invalidation & shared `SSLContext` (`6/N`, `17/N`, `24/N`)

- **What bottleneck it hits:** Calling `batch.connect(member)` repeatedly across multi-turn agent steps would otherwise allocate a new `Sandbox` / `AsyncSandbox` + `httpx.AsyncClient` / `requests.Session` every turn, losing HTTP keep-alive to the sandbox pod and (on async HTTPS) calling `ssl.create_default_context()` $N$ times to re-parse the OS CA bundle from disk!
- **How it works under the hood:**
  1. `self._connections[claim_name]` caches `(sandbox_handle, sandbox_name, pod_ip)` across `connect()` calls, returning the warm keep-alive connector in $O(1)$ as long as `member_still_reaches(live_member, sandbox_name, pod_ip)` holds (`state` in `{READY, PENDING}` with unchanged `sandbox_name` and `pod_ip`).
  2. On `AsyncSandboxBatch.connect`, every constructed handle receives `sandbox.connector._default_ssl_context = self._client._default_ssl_context` (`24/N`), sharing **one lazily cached `ssl.SSLContext`** across all $N$ sandboxes!
- **Safety guard:** Whenever `_watch_loop`, `reconcile_list`, or `release_*` observes a member become `LOST`/`FAILED`, get released, or change `sandbox_name` / `pod_ip`, the batch pops the stale connector into `_stale_connections` (an ordered `dict` for $O(1)$ dedup/removal) and closes its sockets outside `self._lock` so callers never talk to a recycled pod IP ([WATCH-8](field-guide/watch.md#watch-8), [PERF-19](field-guide/perf.md#perf-19)).
- **Measured result:** `300` connectors and `1` CA-bundle load across `300` sandboxes regardless of step count.
- **Code:** `SandboxBatch.connect`, `AsyncSandboxBatch.connect`, `member_still_reaches`. **Deep dive:** [WATCH-8](field-guide/watch.md#watch-8), [PERF-19](field-guide/perf.md#perf-19).

---

## 5. Smarter $O(1)$ algorithms & observability hygiene

Per-event and per-pool cost must depend on what changed, never on total batch size $N$.

<a id="opt-12"></a>

### OPT-12 $O(1)$ incremental quorum & fill accounting per watch event (`13/N`, `#1988`)

- **What bottleneck it hits:** At `N = 10,000` claims and `G = 1,000` groups, scanning members to check whether a group can still reach `min_ready` or scanning all $G$ groups on every wakeup turns a `403` failure storm into **$O(N^2)$ (`100,000,000` iterations)** and every consumer wakeup into $O(G)$!
- **How it works under the hood:** `BatchState` maintains three $O(1)$ structures:
  1. **Per-pool integer counters (`_failed`, `_lost`, `_released_count`):** `_unreachable_error(pool)` checks `size - (failed + lost + released) < min_ready` in **3 integer additions**!
  2. **Ordered ready set (`_waiting[pool]: dict[str, None]`):** Insertion-ordered `dict` gives $O(1)$ insert when a claim turns `READY`, $O(1)$ middle removal if a claim flaps back to `PENDING`/`LOST`, and $O(\text{min\_ready})$ arrival-order slicing (`list(waiting)[:min_ready]`) so slow low-ordinal claims never block fast higher-ordinal claims ([PERF-2](field-guide/perf.md#perf-2))!
  3. **Dirty blocking set (`_groups_short_of_quorum: set[str]`):** Initialized to pools with `min_ready > 0`. Each event updates *only* its own pool's membership in `_groups_short_of_quorum`; quorum is reached the instant `len(_groups_short_of_quorum) == 0` in **$O(1)$**!
- **Safety guard:** Monotonic sets (`_unable`, `_resolved`, `_dispatched`) ensure duplicate watch events or late re-list replays never double-decrement counters or un-settle a decided group ([COUNT-1](field-guide/count.md#count-1), [COUNT-6](field-guide/count.md#count-6)).
- **Measured result:** Client watch-processing CPU stayed completely flat across scale: `0.058, 0.063, 0.061` cores at `N = 1,000, 5,000, 10,000` (`bench tip #35`; `475` Python bytecode instructions per `upsert_claim` at both `N=100` and `N=5,000`).
- **Code:** `BatchState._check_quorum`, `_unreachable_error`, `_waiting`. **Deep dive:** [PERF-1](field-guide/perf.md#perf-1), [PERF-2](field-guide/perf.md#perf-2).

<a id="opt-22"></a>

### OPT-22 $O(1)$ lazy tombstones, revoked outcomes, and `_members_by_pool` (`15/N`, `24/N`, `25/N`)

- **What bottleneck it hits:** Three subtle $O(N)$ traps hide outside `upsert_claim`:
  1. Releasing a member whose `MEMBER_READY` event already sits inside `_events_to_yield: deque` would take $O(N)$ to scan and delete from the `deque`.
  2. Releasing a group (`release_group(pool)`) whose `GroupOutcome` already sits inside `_group_outcomes_to_yield: deque` would take $O(G)$ to scan and rewrite in the `deque`.
  3. Calling `batch.members(warmpool=pool)` or `release_group(pool)` on a `2,000`-claim batch across `200` pools used to scan all `2,000` entries in `self._members` (`O(N)` per pool -> `O(G * N)` across all pools)!
- **How it works under the hood:**
  1. **Lazy event tombstones (`_released: dict[str, str]`):** `release_member` / `release_group` records `claim_name -> pool` in `_released` in $O(1)$; `pop_event()` checks `claim_name in self._released` at pop time and skips stale `MEMBER_READY` events in $O(1)$ amortized ([PERF-3](field-guide/perf.md#perf-3)).
  2. **Lazy outcome revocation (`_revoked_outcomes: set[str]`, `25/N`):** `release_group(pool)` adds `pool` to `_revoked_outcomes` in $O(1)$; when `pop_group_outcome()` pops that pool's queued success, it substitutes a `BatchError` outcome on the fly without scanning the `deque`!
  3. **Secondary pool index (`_members_by_pool: dict[str, dict[str, Member]]`, `24/N` `#2235`):** Maintained alongside `_members` on every insert/pop so `members(warmpool=pool)`, `has_pool(pool)`, and `release_group(pool)` run in **$O(|\text{pool}|)$** instead of $O(N)$, and `get_member(name)` (`24/N`/RL) looks up a single member in **$O(1)$** without copying/sorting the batch!
- **Safety guard:** A single pair of internal helpers updates `_members` and `_members_by_pool` atomically under `self._lock` (cleaning up empty pool dicts so memory never leaks across ephemeral pools).
- **Measured result:** Cut per-pool lookup/release instruction scaling from `33x-45x` growth (`N=50 -> 2,000`) to an exact `1.000` flat ratio ([PERF-24](field-guide/perf.md#perf-24)).
- **Code:** `BatchState.pop_event`, `pop_group_outcome`, `_members_by_pool`. **Deep dive:** [PERF-3](field-guide/perf.md#perf-3), [PERF-24](field-guide/perf.md#perf-24).

<a id="opt-23"></a>

### OPT-23 Deadline-sorted single-pass Lease reaper waits (`18/N`, `#2154`)

- **What bottleneck it hits:** To prove a batch Lease with an unparsable/clock-skewed timestamp or dead holder is genuinely abandoned without trusting wall clocks, `batch_reaper.py` lists stale candidate Leases, waits one renewal interval (`leaseDurationSeconds // 3`), and re-checks that `resourceVersion` did not advance before conditionally deleting the Lease. If a cluster has `10` stale Leases with mixed intervals (`20 s`, `30 s`, ...), sleeping per Lease sequentially takes the **sum** of all intervals (`200+ s`), while sleeping the global maximum up front delays fast `20 s` Leases behind slow `300 s` detached Leases!
- **How it works under the hood:** `reap()` snapshots a single monotonic `listed_at = time.monotonic()` right after listing all namespaces, sorts candidate Leases in ascending order of `renew_interval`, and sleeps only the remaining delta `max(0.0, listed_at + interval - time.monotonic())` before re-reading and conditionally deleting (`DELETE` with `preconditions.uid` + `resourceVersion`) each Lease!
- **Safety guard:** Re-verifies `resourceVersion` and `uid` via Kubernetes `V1Preconditions` on the `DELETE` itself (`22/N` `#2188`), so if a paused driver woke up and renewed the Lease 1 millisecond before the `DELETE` arrived, etcd rejects the `DELETE` with `409 Conflict` and the live batch survives untouched!
- **Measured result:** Total reaper runtime equals `max(intervals)` rather than `sum(intervals)` while reaping short-duration Leases at their exact earliest safe deadline.
- **Code:** `batch_reaper.reap`. **Deep dive:** [PERF-15](field-guide/perf.md#perf-15), [LEASE-8](field-guide/lease.md#lease-8), [LEASE-9](field-guide/lease.md#lease-9).

<a id="opt-24"></a>

### OPT-24 Bulk per-claim `DEBUG` logging (`12/N`, `15/N`)

- **What bottleneck it hits:** `K8sHelper.create_sandbox_claim` and `delete_sandbox_claim` originally logged `INFO` lines on every single call. At `N = 5,000` claims, formatting and flushing `10,000` `INFO` lines through synchronized Python `logging` handlers adds lock contention on stdout/stderr I/O and buries real warnings in noise.
- **How it works under the hood:** Added a keyword-only `log_level: int = logging.INFO` parameter to `create_sandbox_claim` and `delete_sandbox_claim` (preserving `INFO` for single-sandbox callers), while `SandboxBatch` passes `log_level=logging.DEBUG` on bulk create/delete paths and emits `INFO` only on batch-level state transitions (e.g. first `429` throttle window or `max_in_flight` pool clamping).
- **Safety guard:** Backward-compatible default keeps existing `SandboxClient.create_sandbox()` logging unchanged.
- **Measured result:** `O(1)` `INFO` lines per batch instead of `O(N)`.
- **Code:** `K8sHelper.create_sandbox_claim`, `SandboxBatch._send_create`. **Deep dive:** [PERF-9](field-guide/perf.md#perf-9).

---

# Part II: RL Integration & GKE Batch-Mode Optimizations (In Depth)

`examples/agent-sandbox-rl` orchestrates thousands of multi-turn RL rollouts (SWE-bench, R2E-Gym, OpenHands, Tunix GRPO) across single and multi-cluster GKE fleets (`claim_mode="batch"`). Optimizing the SDK alone was only half the battle: at `1,000+` concurrent rollouts, Python's thread pools, `aiohttp` filesystem lookups, WebSocket `exec` clients, warm-pool `GET` storms, and Kubernetes garbage-collection lag inside the cluster became the dominant bottlenecks.

## 1. Zero-`GET` hot paths & shared client pools (`27/N`)

<a id="opt-30"></a>

### OPT-30 Zero-`GET` `acquire()`, watch-backed `is_alive()`, and `size=0` single-pool seed batch (`27/N`, `e4ba2706`)

- **What bottleneck it hits:**
  1. In `per_claim` mode, every `SandboxFleet.acquire()` called `handle.record_pod_identity()` (`1` Pod `GET` to record `pod.metadata.uid` and `restartCount`), and every RL step boundary called `handle.is_alive()` (`1` Pod `GET`).
  2. Opening a dynamic `SandboxBatch` by passing all `500` SWE-bench task pools up front as `BatchGroup(size=0)` ran `1,000` sequential precheck `GET`s on the very first thread before creating any claim!
- **How it works under the hood:**
  1. **Single-pool `size=0` seed (`PERF-20`):** `BatchClaims._batch_for` opens one `SandboxBatch` per cluster seeded with *only* the first caller's warm pool (`[BatchGroup(warmpool=pool, size=0)]`, costing 2 `GET`s). Later pools validate lazily and concurrently on their first `acquire()` ([OPT-17](#opt-17)).
  2. **Zero-`GET` `acquire()` + watched `still_reaches()`:** When `claim_mode="batch"`, `acquire()` skips `record_pod_identity()` (`0` Pod `GET`s; `pod_name` comes directly from watched `member.sandbox_name`). At step boundaries, `SandboxHandle.is_alive()` first checks `self._batch_claim.still_reaches()` in memory against the batch watch (`member.state` in `{READY, PENDING}` with unchanged `sandbox_name` and `pod_ip`), immediately catching claim loss, expiry, or pod replacement with **`0` `GET`s** before falling back to a bounded `read_namespaced_pod_status` check when alive!
- **Safety guard:** If the batch watch stops (`batch.err() is not None`) or a caller explicitly baselines `record_pod_identity()` (`pod_uid is not None`), `is_alive()` transparently runs the full REST Pod UID + container `restartCount` verification.
- **Measured result:** Eliminates `1` Pod `GET` per `acquire()`, `998` serial startup `GET`s on `500`-pool SWE-bench runs, and catches dead claims in memory in $O(1)$.
- **Code:** `agent_sandbox_rl/batch_claims.py`, `handles.py: SandboxHandle.is_alive`, `fleet.py`. **Deep dive:** [PERF-20](field-guide/perf.md#perf-20).

<a id="opt-25"></a>

### OPT-25 Shared `cluster.core_api` pool for REST pod status reads (`27/N`, `53caa99e`)

- **What bottleneck it hits:** `kubernetes.stream.stream()` (used for WebSocket pod `exec`) mutates `ApiClient` headers/configuration in place and is *not* thread-safe, so `Cluster.exec_core_api()` intentionally allocates a brand-new `ApiClient` + `urllib3.PoolManager` in `threading.local()` for every OS thread. However, `record_pod_identity()` and `is_alive()` were also calling `self._cluster.exec_core_api().read_namespaced_pod_status(...)`: a plain REST `GET`! Across hundreds of worker threads, that leaked hundreds of separate `PoolManager`s and TLS sockets.
- **How it works under the hood:** Switched all REST `read_namespaced_pod_status` calls in `handles.py` onto the shared, thread-safe `self._cluster.core_api` `PoolManager`, reserving `exec_core_api()` strictly for `stream()` WebSocket upgrades.
- **Safety guard:** Plain `CoreV1Api` REST calls do not invoke `kubernetes.stream`, so sharing `cluster.core_api` across threads is 100% thread-safe while keeping WebSocket upgrades isolated.
- **Measured result:** Contributed to the **`11.1x` client socket reduction (`488 -> 44` sockets)** on `1,000`-sandbox GKE warm bursts.
- **Code:** `agent_sandbox_rl/handles.py`, `cluster.py`. **Deep dive:** [LIB-20](field-guide/lib.md#lib-20).

<a id="opt-26"></a>

### OPT-26 In-memory `BatchWatchdog` on the warm path (`27/N`, `05b0a63b`, `4018f0dd`)

- **What bottleneck it hits:** Cold-start sandboxes with broken container images (`ImagePullBackOff`, `ErrImagePull`, `CrashLoopBackOff`) otherwise hang `acquire()` until the full `claim_timeout` (`120-600 s`). Yet `per_claim`'s `ClaimWatchdog` (1 polling thread + Pod `GET`s per claim) would recreate an $N$-thread, $N$-`GET` storm if run per batch claim!
- **How it works under the hood:** `BatchWatchdog` runs **1 daemon thread per `SandboxBatch`**, reading `batch.members()` purely in memory (`0` HTTP calls):
  - Tracks `first_seen_at[claim_name]` for `PENDING` members and drops entries the instant a member turns `READY`.
  - Because warm-pool adoptions become `READY` in `< 1 s` (well below `fail_fast.initial_delay_s = 10.0 s`), **healthy warm bursts execute `0` Pod `GET`s**!
  - Only claims stuck `PENDING` past `10 s` get a `classify_pod()` `GET`. On a terminal startup failure, `BatchWatchdog` records the `StartVerdict` and calls `batch.release_member(member)`, which wakes that single `acquire()`'s per-claim `Event` immediately in memory to raise `SandboxStartError`!
- **Safety guard:** Before calling `release_member`, `BatchWatchdog` re-checks that the member did not turn `READY` or get handed out while the Pod `GET` was in flight (`4018f0dd`), preventing a late watchdog check from killing a live rollout's sandbox!
- **Measured result:** `0` watchdog `GET`s across all warm GKE bursts, with `~10 s` fail-fast parity on broken cold images.
- **Code:** `agent_sandbox_rl/failfast.py: BatchWatchdog`, `fleet.py`. **Deep dive:** [PERF-22](field-guide/perf.md#perf-22).

<a id="opt-27"></a>

### OPT-27 Bounded-parallel `acquire_batch` / `release_all` with deterministic rollback (`27/N`, `c9600379`)

- **What bottleneck it hits:** Synchronous multi-step RL trainers acquire a full wave of sandboxes before step 0 and release all handles at teardown or wave boundaries. Running `acquire_batch` or `release_all` sequentially pays $N$ serial RTTs.
- **How it works under the hood:** Fans out `SandboxFleet.acquire_batch` and `release_all` across bounded `ThreadPoolExecutor`s while assembling returned `SandboxHandle`s in exact caller `tasks` input order.
- **Safety guard:** If any `acquire` in `acquire_batch` fails, the fleet catches the error, releases every handle that succeeded in the partial batch in parallel (so zero sandboxes leak!), and re-raises the first exception in input order ([CLEAN-25](field-guide/clean.md#clean-25)).
- **Measured result:** Part of the **`3.81x` end-to-end speedup (`21.7 min -> 5.7 min`)** and **`5.41x` faster step barrier** on `8,000`-trajectory GKE R2E-Gym (`Campaign 4 #4A`).
- **Code:** `agent_sandbox_rl/fleet.py: acquire_batch`, `release_all`. **Deep dive:** [CLEAN-25](field-guide/clean.md#clean-25).

<a id="opt-28"></a>

### OPT-28 Memoize on-demand pools and auto-size HTTP & executor pools (`27/N`, `6d773276`, `e7ed074b`)

- **What bottleneck it hits:** At `max_concurrent >= 1,000`, three hidden client bottlenecks sat *in front* of `SandboxBatch`:
  1. Dynamic on-demand `acquire()` called `_ensure_pool()` (`ensure_template` + `create_warmpool` = `2` K8s RPCs) on *every* acquire even after `(cluster, image)` was already created!
  2. `Cluster`'s fixed `connection_pool_maxsize = 1000` tripped `check_pool_size` (`>= max_in_flight + 2`) at high `max_in_flight`.
  3. `AsyncSandboxFleet._thread_pool()` capped its `ThreadPoolExecutor` at `1,024` threads, causing `max_concurrent = 1,024` + pipelined prefetch tasks (`mc + 2 * win`) to queue inside Python before ever reaching `SandboxBatch`!
- **How it works under the hood:** Memoizes `(cluster.name, task.image)` in `self._ondemand` so repeat acquires cost **`0` setup RPCs**; auto-sizes `connection_pool_maxsize = max(1000, max_concurrent + 32, max_in_flight + 16)`; and raises `AsyncSandboxFleet._thread_pool()` ceiling to `min(4096, max(64, mc + 64, mc + 2 * win + 16))`.
- **Safety guard:** Per-`(cluster, image)` creation remains lock-guarded so concurrent first callers create the template/pool once, and failed creations are never memoized.
- **Measured result:** Enabled `0`-error GKE runs at `c = 1,000..2,350` (`7.34` gVisor sandboxes/vCPU in `Campaign 4 #6`).
- **Code:** `agent_sandbox_rl/fleet.py`, `async_fleet.py`, `cluster.py`. **Deep dive:** [PERF-23](field-guide/perf.md#perf-23).

---

## 2. Event-loop-native driver, multiplexed exec & GC-stall elimination (`Campaign 5`)

In `27/N`, `AsyncSandboxFleet` still bridged into synchronous `SandboxFleet` / `SandboxBatch` and `kubernetes.stream` via `loop.run_in_executor(...)`. At `c = 1,000` concurrent rollouts, Campaign 5 (`RL_BATCH_OPTIMIZATIONS.md`, `feat/c5-batch-rebased-300d05e7`) eliminated the worker-thread bridge completely and solved a `4.5x` cluster-side warm-pool refill stall.

<a id="opt-31"></a>

### OPT-31 Direct background `Sandbox` + `Pod` deletion on release (`c5`, `2eb5ef6d`, `3a14c1dc`)

- **What bottleneck it hits:** When a rollout finishes and deletes its `SandboxClaim`, `SandboxWarmPool` cannot finish refilling node capacity (or freeing node `maxPods` / pod IPAM slots) until the claimed `Sandbox` and its backing `Pod` actually terminate. By default, deleting a `SandboxClaim` relies on **Kubernetes's built-in `GarbageCollectorController`** (watching dependency graphs via informers) to cascade the delete from `SandboxClaim -> Sandbox -> Pod`. On GKE clusters that had been running heavy churn for several days (`4 days old`), cascading GC lagged severely behind burst releases, starving warm-pool refills and stalling multi-step R2E `500x2x8` runs at **`993-1,010 s`** (and `250x2x6` multi-step at `251-456 s`)!
- **How it works under the hood:** Immediately after a released claim's own `DELETE` succeeds, a small best-effort background pool (bounded at **`min(16, max_in_flight)`** in `3a14c1dc` so low-concurrency fleets never send 16 extra requests) issues a direct `DELETE` on the `Sandbox` and then on its `Pod`, bypassing the Kubernetes GC queue so the kubelet starts tearing down the container immediately!
- **Safety guard & fresh-cluster control:**
  - Runs **strictly after** the `SandboxClaim` delete succeeds (so a live rollout's pod is never deleted early).
  - **RBAC self-disabling:** Deleting `sandboxes` and `pods` directly requires optional RBAC permissions beyond `sandboxclaims`. On the very first `403 Forbidden`, the background deleter flips a flag and disables itself cleanly for the rest of the process, falling back to standard Kubernetes GC with zero errors!
  - **Scientific control (`7769066b`):** Re-running the exact same R2E and multi-step benchmarks on a **freshly created** 10-node cluster (where Kubernetes GC cleared sandboxes promptly) yielded `236-243 s` (batch) vs `236-252 s` (`per_claim`) on R2E and `99-102 s` vs `108 s` on multi-step (`~7%` faster), proving that the `4.5x` speedup specifically eliminates slow cluster GC cascading!
- **Measured result:** On 4-day-old GKE clusters:
  - **R2E `500x2x8` (20 nodes):** `993-1,010 s -> 209-279 s` (**`~4.5x` faster**)
  - **Multi-step `250x2x6` (20 nodes):** `251-281 s -> 116-123 s` (**`~2.2x` faster**)
  - **Multi-step `250x2x6` (10 nodes):** `364-456 s -> 172-199 s` (**`~2.2x` faster**)
  - **3,000 tasks `500x6` naive / sliding / pipelined (10 nodes):** `390-421 s -> 148-181 s` (**`2.2x-2.6x` faster**)
- **Code:** `feat/c5-batch-rebased-300d05e7` (`2eb5ef6d`, `3a14c1dc`); `RL_BATCH_OPTIMIZATIONS.md` §1. **Deep dive:** [PERF-26](field-guide/perf.md#perf-26).

<a id="opt-33"></a>

### OPT-33 Event-loop-native batch claims (`AsyncBatchClaims`) (`c5`, `b386d881`)

- **What bottleneck it hits:** Even though the Python SDK ships native `AsyncSandboxClient` and `AsyncSandboxBatch`, `agent-sandbox-rl`'s `AsyncSandboxFleet` originally wrapped synchronous `SandboxFleet.acquire()` / `release()` inside `loop.run_in_executor(...)`. At `c = 1,000`, that spawned `1,000+` OS threads just to park on `threading.Event.wait()`, contending on Python's GIL, thread stack memory, and cross-thread event-loop wakeups (`call_soon_threadsafe`).
- **How it works under the hood:** Added `AsyncBatchClaims` driving `AsyncSandboxClient` and `AsyncSandboxBatch` **natively on the `asyncio` event loop**, so `acquire`, watch readiness dispatch, Lease heartbeats, and `release` execute as lightweight coroutines on **1 thread** with zero `run_in_executor` hops!
- **Safety guard:** Preserves `per_claim` mode untouched (`100%` backward compatible) and mirrors all `BatchClaims` stopped-batch replacement and teardown invariants on `asyncio` locks/tasks.
- **Measured result:** Eliminates `1,000` acquire/release worker threads at `c = 1,000` (see combined table under [OPT-34](#opt-34)).
- **Code:** `feat/c5-batch-rebased-300d05e7` (`b386d881`); `RL_BATCH_OPTIMIZATIONS.md` §2. **Deep dive:** [PERF-27](field-guide/perf.md#perf-27).

<a id="opt-34"></a>

### OPT-34 Multiplexed event-loop WebSocket pod exec (`handle.aexec()`) (`c5`, `b386d881`)

- **What bottleneck it hits:** Synchronous `kubernetes.stream` opens a blocking `websocket-client` socket and mutates `ApiClient`, forcing `exec_in_pod` to run on an executor thread with a dedicated per-thread `ApiClient` + `PoolManager`. At `c = 1,000` concurrent rollouts running commands simultaneously, `per_claim` spawned **`~3,000` OS threads and held `~1,050` open sockets**!
- **How it works under the hood:** Added `handle.aexec()` using `kubernetes_asyncio.stream.WsApiClient` directly on the `asyncio` event loop, multiplexing concurrent WebSocket `/exec` streams across a shared async client with **zero OS threads per exec**!
- **Safety guard:** Enforces per-command timeouts via `asyncio.timeout` and cleanly closes WebSocket channels on cancellation or non-zero exit codes.
- **Measured result:** Combined with `AsyncBatchClaims` and [OPT-32](#opt-32), reduced `c = 1,000` driver footprint from **`~3,000` threads / `~1,050` sockets to `3` threads / `68` sockets**, while cutting TTFI P50 from **`10.0 s` to `2.9 s`**!
- **Code:** `feat/c5-batch-rebased-300d05e7` (`b386d881`); `RL_BATCH_OPTIMIZATIONS.md` §2. **Deep dive:** [PERF-27](field-guide/perf.md#perf-27).

<a id="opt-32"></a>

### OPT-32 Skip `aiohttp` per-request proxy and `.netrc` lookups (`c5`, `feffbac2`, `03390885`)

- **What bottleneck it hits:** Even after moving claims and exec onto `asyncio`, `/proc/self/status` still showed **`23` OS threads** spinning inside the driver and burning `10-20%` extra CPU during `1,000`-claim bursts! Why? By default, `aiohttp.ClientSession` (`trust_env=True` or netrc fallback) checks environment proxies and calls `netrc.netrc()`: which reads `~/.netrc` from the filesystem via **`loop.run_in_executor()` on every single HTTP request** (`1,000` thread-pool dispatches per burst)!
- **How it works under the hood:** Detects once at startup whether the process has proxy env vars (`HTTP_PROXY` / `HTTPS_PROXY` / `ALL_PROXY`) or a `~/.netrc` file configured. When neither exists (the standard case for kubeconfig/bearer-token GKE drivers), configures `aiohttp` to skip per-request proxy and `.netrc` lookups completely!
- **Safety guard:** Whenever proxy environment variables or a `.netrc` file *are* present in the environment, falls back to standard `aiohttp` lookup behavior automatically so corporate proxy / `.netrc` setups never break.
- **Measured result:** Isolated A/B across 4 bursts/arm on two clusters:
  - Driver threads dropped **`23 -> 3`** (just the main Python thread + runtime internals!).
  - Driver CPU during a `1,000`-claim burst dropped **`10-20%`**.
  - `1,000`-claim burst rate rose from **`191-211/s` to `~240/s`**.
- **Code:** `feat/c5-batch-rebased-300d05e7` (`feffbac2`, `03390885`); `RL_BATCH_OPTIMIZATIONS.md` §1 #3. **Deep dive:** [PERF-27](field-guide/perf.md#perf-27).

<a id="opt-35"></a>

### OPT-35 Prewarm `64` keep-alive TLS connections per cluster (`c5`, `93f93fc9`)

- **What bottleneck it hits:** On the very first `1,000`-claim burst against a GKE regional VIP, opening `max_in_flight = 64` cold HTTPS connections simultaneously pays `64` concurrent TCP + TLS 1.3 handshakes (and asymmetric key-exchange CPU on both the single-threaded event loop and GKE's load balancer) right on the critical path of step 0!
- **How it works under the hood:** Before firing the first burst, opens **`64` concurrent lightweight requests** per cluster to populate `aiohttp.TCPConnector`'s keep-alive pool with `64` warm, TLS-established sockets (`max_in_flight = 64`). *(Sweeping `> 64` prewarmed connections showed zero additional gain because `max_in_flight = 64` bounds concurrent creates.)*
- **Safety guard:** Uses standard TLS 1.3 settings without weakening cipher/key-exchange suites (an experiment restricting TLS key exchange was faster locally but explicitly rejected for weakening transport security!).
- **Measured result:** Part of cutting `c = 1,000` **burst + first exec wall from `12.5 s` to `5.4 s` (`2.3x` faster)**.
- **Code:** `feat/c5-batch-rebased-300d05e7` (`93f93fc9`); `RL_BATCH_OPTIMIZATIONS.md` §2 & §4. **Deep dive:** [PERF-27](field-guide/perf.md#perf-27).

<a id="opt-36"></a>

### OPT-36 Raw watch pass-through & zero-decode create/delete drains (`c5`, `b386d881`)

- **What bottleneck it hits:** On a single-threaded `asyncio` loop driving `240-267 claims/s` plus `1,000` watch events/s, Python CPU spent deserializing OpenAPI models on watch events (or re-serializing objects back to dicts) and JSON-decoding `create_sandbox_claim` / `delete_sandbox_claim` HTTP response bodies (when the caller already knows the deterministic `<batch-id>-<ordinal>` claim name and learns readiness from the watch!) steals event-loop time from scheduling I/O.
- **How it works under the hood:**
  1. Streams watch events directly as raw parsed JSON dicts without OpenAPI model instantiation or re-serialization.
  2. Sends claim `POST` and `DELETE` calls with `_preload_content=False`, validates the HTTP status code (and `Retry-After` headers on errors), and drains/releases the response bytes without UTF-8 or `json.loads` decoding!
- **Safety guard:** Error statuses (`>= 400`) still read the error body and headers so `ApiException` classification (`409`, `429`, `403`, `404`) and `Retry-After` parsing remain 100% intact.
- **Measured result:** Keeps single-event-loop CPU low enough to sustain `267 claims/s` (`c=500`) and `246 claims/s` (`c=1,000`) on 1 thread.
- **Code:** `feat/c5-batch-rebased-300d05e7` (`b386d881`); `RL_BATCH_OPTIMIZATIONS.md` §2. **Deep dive:** [PERF-27](field-guide/perf.md#perf-27).

<a id="opt-37"></a>

### OPT-37 Single paginated namespace `LIST` for warm pools (`c5`, `b386d881`)

- **What bottleneck it hits:** SWE-bench and R2E-Gym create **`500` distinct single-image `SandboxWarmPool`s** per cluster (`1` per repository image). Even with parallel or lazy per-pool `GET`s ([OPT-16](#opt-16), [OPT-17](#opt-17)), checking or refreshing `500` pools individually costs **`500` separate HTTP `GET`s**!
- **How it works under the hood:** Replaces $O(P)$ individual `get_sandbox_warmpool` calls during multi-pool prechecks and warm-credit refreshes with **one paginated namespace-level `LIST /sandboxwarmpools`** (`limit=500`), updating existence and `status.readyReplicas` for all `500` pools in **1 RTT**!
- **Safety guard:** Uses pagination (`limit` + `continue`) so large namespaces remain bounded in response size, and falls back cleanly when listing is not applicable.
- **Measured result:** Compresses `500` warm-pool `GET`s into `1` `LIST` call on `500`-image SWE-bench / R2E workloads.
- **Code:** `feat/c5-batch-rebased-300d05e7` (`b386d881`); `RL_BATCH_OPTIMIZATIONS.md` §2. **Deep dive:** [PERF-28](field-guide/perf.md#perf-28).

<a id="opt-38"></a>

### OPT-38 Hybrid release coalescing & sibling-aware deferred tail release (`c5`, `b386d881`, `2f87f7a9`)

- **What bottleneck it hits:**
  1. `deletecollection` beats individual `DELETE`s on large synchronous wave tails (`500-1,000` claims at once), *but* on small rolling/sliding windows (`1-8` tasks finishing at a time), issuing a label-scoped `deletecollection` + consistent `count_live` list round-trip is heavier than a direct per-claim `DELETE`!
  2. Deferring releases to coalesce a tail batch saves API calls when the cluster is idle, *but* if another task (`acquire()`) is currently blocked waiting for a warm-pool or node slot, holding onto finished claims in a deferred buffer **starves the waiting `acquire()`**!
- **How it works under the hood:**
  1. **Hybrid threshold (`b386d881`):** Large release tails coalesce into a single `deletecollection` (`release_members`), whereas small rolling windows issue lightweight per-claim `release_member` `DELETE`s.
  2. **Sibling-aware flush (`2f87f7a9`):** Tail release is deferred **only while zero sibling `acquire()` calls are waiting**! The moment any sibling `acquire()` is queued for capacity, pending releases flush immediately so warm-pool refill and pod teardown start without a millisecond of artificial coalescing delay.
- **Safety guard:** Every deferred claim is still flushed unconditionally on wave barriers, fleet teardown, or error paths so zero claims leak.
- **Measured result:** Gives batch barriers bulk `deletecollection` efficiency without stalling rolling/pipelined `acquire()` callers.
- **Code:** `feat/c5-batch-rebased-300d05e7` (`b386d881`, `2f87f7a9`); `RL_BATCH_OPTIMIZATIONS.md` §2. **Deep dive:** [PERF-28](field-guide/perf.md#perf-28).

<a id="opt-39"></a>

### OPT-39 Pipelined readiness seeding, `250 ms` prefetch yield, and sweep-tuned defaults (`c5`, `5f87abe9`, `50e4531b`)

- **What bottleneck it hits:**
  1. In **pipelined** rollout strategies (`strategies.py`), background prefetch tasks pre-acquire the *next* window's sandboxes while the *current* window starts executing. If background prefetch creates fire at the exact same instant as foreground window creates, prefetch steals `max_in_flight` slots, `WarmCredit`, and controller reconciliation workers from the active step!
  2. SDK conservative defaults (`create_rps=50`, `max_in_flight=20`) under-utilize a tuned GKE controller, while setting `max_in_flight` too high (`256+`) adds apiserver contention for zero wall-clock gain.
- **How it works under the hood:**
  1. **Window readiness seeding + `250 ms` prefetch yield (`5f87abe9`):** Seeds warm-pool readiness counts when each pipelined window starts, and makes background prefetch creates yield up to **`250 ms`** whenever foreground creates are active so foreground tasks always claim warm replicas and controller workers first!
  2. **Empirical parameter sweeps (`50e4531b` + `RL_BATCH_OPTIMIZATIONS.md` §3):**
     - Sweeping `max_in_flight` across `64 / 128 / 256` produced the **exact same wall time** (and running two batch clients concurrently got `~0.5x` the single-client rate each), proving claim throughput above `mif=64` is bounded by the **in-cluster controller**, not the Python client!
     - Sweeping `create_rps` across `50 / 500 / 2,000` (with warm pools at `50%` of workload size) gave identical wall time (`33.3-33.5 s`, set by kubelet pod start rate `~50 pods/s`), while `create_rps=500` improved TTFI P99 (`9.9 s -> 8.6 s`) without needing `2,000`.
     - Locked in RL defaults **`create_rps=500`, `max_in_flight=64`**.
- **Measured result (Campaign 5 combined 10-node bursts, `0` errors):**
  - `1,000` claims at `c=500`: **`267 claims/s`** vs `91/s` (`2.93x`)
  - `1,000` claims at `c=1,000`: **`246 claims/s`** vs `106/s` (`2.32x`)
  - TTFI P50 at `c=1,000`: **`2.9 s`** vs `10.0 s` (`3.45x` faster)
  - Burst + first exec wall at `c=1,000`: **`5.4 s`** vs `12.5 s` (`2.31x` faster)
  - Eval `500x1` wall at `c=500`: **`15.6 s`** vs `18.6 s`
- **Code:** `feat/c5-batch-rebased-300d05e7` (`5f87abe9`, `50e4531b`); `RL_BATCH_OPTIMIZATIONS.md` §2 & §3. **Deep dive:** [PERF-28](field-guide/perf.md#perf-28).

---

## What we tested and intentionally rejected (`Campaign 5` negative results)

Recording negative results keeps future engineers from re-implementing dead ends (`RL_BATCH_OPTIMIZATIONS.md` §4):

| Experiment | Measured outcome & why we dropped it |
| :-- | :-- |
| Exec on extra event loops in worker threads | Zero throughput gain once `aexec()` ran natively on the main `asyncio` loop |
| Hand-written minimal WebSocket exec client | No faster than `aiohttp` WebSockets in local microbenchmarks; extra maintenance risk |
| TLS session resumption for `exec` | Saved only `~10%` of handshake cost locally; not worth custom SSL session-ticket plumbing |
| Restricting TLS key-exchange curves | Faster handshakes, but weakens transport security; **rejected on security grounds** |
| Concurrent per-claim `DELETE`s ahead of `deletecollection` | `797-983` vs `854-1,004 sandboxes/s`; added complexity for zero gain |
| Separate concurrency slot budget for per-claim deletes | Zero change in wall-clock time |
| Cancelling unused warm-pool refill in sliding windows | Inside run-to-run noise (node pod-creation rate `~50 pods/s` dominates) |
| Prewarming `> 64` TLS connections | Zero measured benefit because `max_in_flight = 64` caps concurrent bursts |

---

## Lessons for your next optimization

- **Ask what each limit protects:** a rate limit on work that never touches the protected resource is pure artificial latency ([OPT-3](#opt-3)).
- **Measure the heap and `/proc/self` before guessing:** the `69.4 MB` `release()` spike was the unread `deletecollection` body, not the re-list ([OPT-4](#opt-4)); the `23` mystery threads in an `asyncio` process were `aiohttp`'s `.netrc` executor lookups ([OPT-32](#opt-32)).
- **Always run a control to separate cluster state from code:** testing on a fresh cluster alongside 4-day-old clusters proved the `4.5x` multi-step speedup came from bypassing slow Kubernetes cascading GC rather than client `POST` speed ([OPT-31](#opt-31)).
- **Sweep parameters to locate the true system ceiling:** when `max_in_flight = 64, 128, 256` give identical wall times, stop tuning the client and look at the controller or kubelet pod start rate ([OPT-39](#opt-39)).
- **Back every shortcut with a self-healing guard:** non-latching `429` cooldowns ([OPT-3](#opt-3)), single-use pod-IP seeds ([OPT-8](#opt-8)), and `403`-self-disabling direct pod deletes ([OPT-31](#opt-31)) give fast-path performance without fragility.

---

[Learnings home](../learnings.md) · [Field guide index](../learnings.md#sec-field-guide)
