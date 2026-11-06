<a id="sec-optimizations"></a>

[Learnings home](../learnings.md) · [Field guide index](../learnings.md#sec-field-guide)

# Optimization showcase: how batch claim got fast and lean

**How to read this page.** Each optimization answers one question: what does this limit protect? Work that doesn't touch that resource may skip the limit, but only behind a guard that falls back to the slow, safe path when the shortcut's assumption breaks. Most wins come from not doing work: fewer requests, waits, and bytes. Read the bill and the budget, scan the scoreboard, then open a card. Ids like [RETRY-15](field-guide/retry.md#retry-15) link to the full story in the field guide.

> [!NOTE]
> **About the numbers.**
> - **Bench tip:** the kind benchmark on `origin/bench/batch-claim` (`test/benchmarks/batch-claim/bench-report.md`). It ran the pre-merge stack at `44fb487f` (`results/PROVENANCE.md`), so it predates warm credit (19/N), the memory work (20/N), and the min_ready-first create order. Its create plan was round-robin, which is why [OPT-7](#opt-7)'s before number comes from it.
> - **GKE Campaign 2:** the `148,544`-rollout production GKE benchmark (`origin/bench/rl-gke`, `examples/agent-sandbox-rl/bench-results/gke/campaign-v2/`, and `ai-on-gke/sandbox-rl-performance` PR #5), running 25/N + Workstream B (`feat/rl-batch-hardening`, `4018f0dd`). See [Benchmarking and GKE at 1,000-sandbox scale](gke-benchmarking.md).
> - **RL evidence:** earlier RL smoke checks (`examples/agent-sandbox-rl`, 25/N).
> - **Analytical:** derived from the code, not measured.
> - **k/N:** the PR in the batch-claim stack that shipped the change (`WS-B` for `feat/rl-batch-hardening`).

**The whole-batch bill** (bench tip, #47 table, batch vs per-claim baseline):

| Shape | Requests | Apiserver watch peak | API connections |
| :-- | :-- | :-- | :-- |
| Cold, 300 claims | 309 vs 1,500 | 3 vs 302 | 5 vs 300 |
| Warm, 160 claims | 168 vs 800 | 3 vs 17 | 16 vs 55 |
| RL shape, 160 claims | 246 vs 800 | 3 vs 23 | 15 vs 59 |

Rolling acquire: 2.0 requests per rollout vs 6.0 (bench tip, rolling table). RL evidence: 243 vs 640 requests and 1 vs 160 watches per 160-sandbox step (`handoff/pr_board.md`, 25/N).

<a id="opt-lead"></a>

## Lead: the per-claim request budget, from about 5 requests per sandbox to about 1

Requests for one sandbox that becomes Ready, has its pod looked up, and is deleted (the bench's baseline shape, modeled on agent-sandbox-rl's `SandboxFleet.acquire`):

| Request kind | Per-claim path (305e9703) | Batch path (23/N) |
| :-- | :-- | :-- |
| Create | 1 POST | 1 POST, retry only on transient error |
| Readiness | 1 watch per claim | Shared: 1 list + 1 watch, reopened about every 30 s |
| Find the pod | 1 Sandbox GET per lookup, +1 on first command | 0 in pod-IP modes |
| Liveness | none | Shared: Lease create, 1 PUT per lease_duration/3, delete |
| Prechecks | none | Shared: 1 GET per warm pool and per distinct template |
| Cleanup | 1 DELETE | Shared: 1 deletecollection + 1 live count per round, usually 1 round |
| **Per sandbox** | **5** (1,500 for 300 cold, 800 for 160 warm); **6 per rollout** | **1 + shared/N**: 1.03 cold, 1.05 warm, 1.54 RL shape; **2.0 per rollout** |

Code. Per-claim: `K8sHelper.create_sandbox_claim` (via `SandboxClient.create_sandbox` → `_create_claim`), `wait_for_claim_ready` → `_watch_claim` (field-selected to the name), `get_sandbox` (via `Sandbox.get_pod_name` / `get_pod_ip`; `SandboxConnector.send_request` adds the first-command GET), `delete_sandbox_claim` (via `Sandbox.terminate`). Batch: `SandboxBatch._create_one`, `_watch_loop` (`_WATCH_TIMEOUT_SECONDS = 30`; fills `Member.sandbox_name` and `pod_ips`), `connect` → `Sandbox._resolve_pod_ip`, `_renew_once` (GET only on a 409), `_precheck_pools`, `_delete_claims_in_rounds`.

**Why it works.** The create is the one write each sandbox truly needs. Readiness, liveness, and cleanup are questions about the whole batch, so one watch, one Lease, and one deletecollection answer them for all N, and the pod IP rides along on the watch. Everything else is a fixed cost divided by N, so per-claim cost approaches 1 as N grows.

**Trace check** (bench tip, stack at `44fb487f`):
- **Cold 300:** baseline sent 300 creates, 300 watches, 600 Sandbox GETs, 300 deletes. Batch sent 300 creates + 9 shared: 1 warm pool GET, 1 template GET, 1 Lease create, 1 Lease update, 2 lists, 1 watch, 1 deletecollection, 1 Lease delete (`results/3a2-*-sync-run1.json`, `requests_total`).
- **RL shape:** 40 pools add 80 precheck GETs (246/160).
- **Rolling:** baseline spent 1 create, 1 watch, 3 Sandbox GETs, 1 delete per rollout; batch only the create and the `release_member` delete. Its 2.02 per rollout is 600 creates, 600 deletes, 5 watch reopens, 7 Lease updates over 600 rollouts (`results/6v2-*-sync-run1.json`, `requests_rolling`).
- **Extra:** an acquire() can add 1 pool refresh GET, at most 1 per second per pool, when warm credit runs out ([OPT-10](#opt-10), OPT-16).

**Deep dive:** [WATCH-1](field-guide/watch.md#watch-1), [K8S-8](field-guide/k8s.md#k8s-8), [PERF-12](field-guide/perf.md#perf-12), [PERF-8](field-guide/perf.md#perf-8) (cards [OPT-1](#opt-1), [OPT-2](#opt-2), [OPT-8](#opt-8), [OPT-14](#opt-14))

## Scoreboard

| # (link) | Optimization (PR) | Before → after | Evidence | Deep dive |
| :-- | :-- | :-- | :-- | :-- |
| [Lead](#opt-lead) | Share all but the create (1/N, 3/N, 12/N, 17/N) | 5 → ~1.03 req/sandbox; 6 → 2.0 per rollout | 309 vs 1,500 cold; 2.0 vs 6.0 rolling (bench tip) | in card |
| [OPT-1](#opt-1) | One batch watch (1/N) | N watches → 1 | Watch peak 3 vs 302 cold (bench tip) | in card |
| [OPT-2](#opt-2) | deletecollection rounds (12/N) | N DELETEs → 1 per round, usually 1 round | 1 request for 10,000 claims (bench tip) | in card |
| [OPT-3](#opt-3) | Warm credit skips pacing (19/N) | Warm fill 3.24 s vs 1.46 s per-claim → covered creates start at once | 99a4a93d; RL evidence | in card |
| [OPT-4](#opt-4) | Drain delete body raw (20/N) | 69.4 → 0.2 MB release peak, N=5,000 | `pr_board.md` 20/N, C1-pr8-impl.md | in card |
| [OPT-5](#opt-5) | Drop managedFields (20/N) | 48.7 → 24.1 MB list peak (after paging) | C1-pr8-impl.md | in card |
| [OPT-6](#opt-6) | Paged lists and count (20/N) | List 69.4 → 48.7 MB; timeout release 81.7 → 11.8 MB | a95cb2a1, C1-pr8-impl.md | in card |
| [OPT-7](#opt-7) | min_ready creates first (12/N) | RL first group 0.65 s vs 0.16 s → not measured | `refs/benchmark_rerun_review.md` | in card |
| [OPT-8](#opt-8) | Seed pod IP once (17/N) | 1 GET per member → 0 | 0 vs 300 GETs, round 1 (bench tip #42) | in card |
| [OPT-9](#opt-9) | Pool ≥ max_in_flight + 2 (17/N) | Sync drops connections → none | Discards in 6/6 runs vs 0 (bench tip #41) | in card |
| [OPT-10](#opt-10) | No refresh GET if slot open (19/N) | 2.01 → ~1 req/env; up to 998 → ≤ max_in_flight GETs | RL evidence, notes commit 88792f54 | in card |
| [OPT-11](#opt-11) | One-float pacer, local backoff (12/N, 13/N) | Blip stalls 20 workers → 1 waits | 20.0..485.2/s on target (bench tip #44) | in card |
| [OPT-12](#opt-12) | O(1) fill accounting (13/N) | O(N²) mass failure, O(G)/wake → O(1)/event | CPU flat across N (bench tip #35) | in card |
| [OPT-13](#opt-13) | Per-claim acquire waiters (16/N) | Wake every acquire → 1 per event | Not measured | in card |
| [OPT-14](#opt-14) | Lease PUT, no pre-read (3/N) | GET + PUT → PUT, GET on 409 | Not measured | in card |
| [OPT-15](#opt-15) | Watch bookmarks (1/N) | 410 + full re-list → resume | Not measured | in card |
| OPT-16 | Refresh lock (19/N) | K GETs for K acquires → 1 GET/pool/s | Not measured | [LOCK-16](field-guide/lock.md#lock-16) |
| OPT-17 | Single-flight first-use pool checks (16/N) | K checks → 1, shared result | Not measured | [LOCK-15](field-guide/lock.md#lock-15), [PERF-11](field-guide/perf.md#perf-11), [BP-28](best-practices.md#bp-28) |
| OPT-18 | Reuse reads (12/N, 16/N, 19/N) | Extra GET per pool/template → 0 | Precheck 1.9% of fill at 1,000 groups (bench tip) | [PERF-10](field-guide/perf.md#perf-10), [PERF-11](field-guide/perf.md#perf-11) |
| OPT-19 | Semaphore only during I/O (12/N, 13/N) | Sleeping retries hold slots → live requests only | Not measured | [LOCK-10](field-guide/lock.md#lock-10) |
| OPT-20 | Shed doomed creates early (16/N, 19/N) | Slot, ordinal, credit, DELETE → 0 requests | Not measured (unit test: no POST) | [RETRY-7](field-guide/retry.md#retry-7), [BP-26](best-practices.md#bp-26) |
| OPT-21 | Reuse member connection (6/N, 17/N) | New Sandbox per connect() → cached | 300 connections for 300 members (bench tip #42) | [WATCH-8](field-guide/watch.md#watch-8) |
| OPT-22 | Lazy READY tombstone (15/N) | O(queue) removal → O(1) | Not measured | [PERF-3](field-guide/perf.md#perf-3) |
| OPT-23 | Reaper: one start, sorted waits (18/N) | Sum of waits → longest | Not measured | [PERF-15](field-guide/perf.md#perf-15) |
| OPT-24 | Per-claim logs at DEBUG (12/N, 15/N) | N INFO lines → aggregate only | Not measured | [PERF-9](field-guide/perf.md#perf-9), [BP-34](best-practices.md#bp-34) |
| OPT-25 | Shared `CoreV1Api` for REST pod status reads (WS-B, 53caa99e) | 1 `PoolManager`/thread → 1 shared pool | Part of `11.1x` socket drop on GKE (`488 → 44`) | [LIB-20](field-guide/lib.md#lib-20) |
| OPT-26 | In-memory `BatchWatchdog` on warm path (WS-B, 05b0a63b) | Pod GETs on warm claims → 0 GETs | `0` watchdog GETs across warm GKE bursts | [PERF-22](field-guide/perf.md#perf-22) |
| OPT-27 | Parallel `acquire_batch` / `release_all` + rollback (WS-B, c9600379) | N serial RTTs → bounded parallel fan-out | `3.82x` 8k R2E-Gym RL wall (`1,495 → 391 s`) | [CLEAN-25](field-guide/clean.md#clean-25) |
| OPT-28 | Memoize on-demand pools + auto-size HTTP/thread pools (WS-B, 6d773276, e7ed074b) | 2 RPCs/acquire & 1,024-thread cap → 0 repeat RPCs, 4,096 cap | `c=1,000-1,024` GKE runs (`0` errors) | [PERF-23](field-guide/perf.md#perf-23) |

OPT-16 to OPT-28 have no card; follow their deep-dive ids. OPT-18 reuses three reads: the precheck GET feeds warm credit, the template GET is deduped, and `_checked_pools` is seeded. OPT-21 keeps a connection while the member's sandbox and pod IP hold.

## Fewer requests

Ask once per batch, not once per claim.

<a id="opt-1"></a>

### OPT-1 One watch for the whole batch

- **Limit protects:** each caller learns when its claim is Ready; the per-claim path opens one watch per claim.
- **Why we may skip it:** every claim carries `agents.x-k8s.io/batch-id=<id>`, so one label-scoped watch sees all N.
- **Guard:** list, then watch from the list's resourceVersion. On a 410 (history expired), re-list and diff against the cache so no event slips through ([WATCH-1](field-guide/watch.md#watch-1), [WATCH-3](field-guide/watch.md#watch-3)).
- **Result:** watch peak 3 vs 302 for 300 cold claims, 3 vs 17 warm (bench tip, #47); cold used 60x fewer connections (bench tip summary). On GKE Campaign 2 (`1,000`-sandbox `200x5` warm burst), client sockets stayed flat at `44` vs `488` (`11.1x` fewer), and `3,000`-task SWE-bench `naive` `claim p99` dropped `7.24x` (`139.66 s → 19.30 s`, [GKE-10](gke-benchmarking.md#gke-10)).
- **Code:** `SandboxBatch._watch_loop`, `K8sHelper.watch_sandbox_claims`. **Deep dive:** [WATCH-1](field-guide/watch.md#watch-1), [WATCH-3](field-guide/watch.md#watch-3), [PAT-32](patterns.md#pat-32), [BP-7](best-practices.md#bp-7)

<a id="opt-2"></a>

### OPT-2 Delete the batch with deletecollection rounds

- **Limit protects:** release() must remove every claim; N DELETEs cost N requests.
- **Why we may skip it:** the batch label selects every claim, so one deletecollection removes them all.
- **Guard:** a selector delete is a point-in-time snapshot and can time out while the server keeps deleting. Each round quiesces in-flight creates, re-takes the Lease, and re-counts live claims with a consistent read; it gives up after 3 rounds in a row without progress (`BATCH_RELEASE_MAX_IDLE_ROUNDS`).
- **Result:** 1 request, 1 round for 10,000 claims (bench tip, scenario 2, "driver requests: deletecollection"), vs 10,000 DELETEs per-claim (analytical). On GKE Campaign 2 (`p2_sdk_1000_cleanup`), `batch.release()` tore down `1,000` live claims in `1.10 s` best / `1.44 s` mean (`4` API calls) vs `35.93 s` for `AsyncSandboxClient.delete_all()` (`32.5x` peak / `24.9x` mean faster, and `13.7x` faster than `1,000` concurrent `terminate()` calls under stock GKE APF).
- **Code:** `_delete_claims_in_rounds`. **Deep dive:** [K8S-8](field-guide/k8s.md#k8s-8), [RETRY-11](field-guide/retry.md#retry-11), [RACE-8](field-guide/race.md#race-8), [LEASE-18](field-guide/lease.md#lease-18), [BP-11](best-practices.md#bp-11)

<a id="opt-8"></a>

### OPT-8 Seed connect() with the watched pod IP, once

- **Limit protects:** the connector needs the pod IP, and the Sandbox object is the source of truth.
- **Why we may skip it:** the batch watch already holds each Ready member's pod IP, so the first lookup needs no Sandbox GET.
- **Guard:** the seed is single-use; after a failed request the connector reads the Sandbox as before. The batch closes a cached connection when the watched pod IP changes or vanishes, or the member is lost or failed ([WATCH-8](field-guide/watch.md#watch-8)).
  ```python
  seeded, self._seeded_pod_ip = self._seeded_pod_ip, None  # take it, then forget it
  ```
- **Result:** fan-out round 1: 0 Sandbox GETs vs 300 for 300 members (bench tip, #42).
- **Code:** `SandboxBatch.connect`, `Sandbox._resolve_pod_ip`. **Deep dive:** [PERF-12](field-guide/perf.md#perf-12), [WATCH-8](field-guide/watch.md#watch-8)

<a id="opt-14"></a>

### OPT-14 Renew the Lease without reading it first

- **Limit protects:** a renewal must not overwrite someone else's write.
- **Why we may skip it:** optimistic concurrency already catches that. A PUT from your last written copy carries its resourceVersion, and the server returns 409 if anything changed.
- **Guard:** only a 409 triggers a GET, and the handle rewrites only if it is still the holder (its own lost write landed). Sync Lease requests go out once (urllib3 retries off), so a retry can't fake a conflict ([LEASE-28](field-guide/lease.md#lease-28), [LIB-6](field-guide/lib.md#lib-6)).
- **Result:** not measured. Analytical: 2 requests per renewal down to 1, every lease_duration/3.
- **Code:** `SandboxBatch._renew_once`, `_write_renewal`. **Deep dive:** [PERF-8](field-guide/perf.md#perf-8), [PAT-33](patterns.md#pat-33), [BP-4](best-practices.md#bp-4), [LEASE-28](field-guide/lease.md#lease-28)

<a id="opt-15"></a>

### OPT-15 Ask for watch bookmarks

- **Limit protects:** a reconnecting watch needs a resourceVersion still inside the server's history window.
- **Why we may skip it:** a quiet batch sends no events to advance the resourceVersion; bookmark events advance it for free.
- **Guard:** bookmarks move only the resourceVersion, never member state. A real 410 still re-lists and diffs ([WATCH-3](field-guide/watch.md#watch-3)).
- **Result:** not measured. Analytical: each idle reconnect avoids a 410 plus a full re-list of N claims.
- **Code:** `K8sHelper.watch_sandbox_claims(allow_watch_bookmarks=True)`. **Deep dive:** [K8S-10](field-guide/k8s.md#k8s-10), [BP-7](best-practices.md#bp-7), [WATCH-1](field-guide/watch.md#watch-1)

## Less waiting

Pace only the work that loads what the pacer protects.

<a id="opt-3"></a>

### OPT-3 Warm credit: covered creates skip create_rps

- **Limit protects:** create_rps keeps a cold burst from starting many pods on one node at once (10.2 s to first Ready unpaced vs 1.35 s at 50/s, 99a4a93d).
- **Why we may skip it:** a warm claim adopts a Ready sandbox and starts no pod. The precheck already GETs each pool, so its `status.readyReplicas` becomes free per-pool credit; a create that spends one is *covered*.
  ```python
  covered = now < deadline and self._warm_credit.take(warmpool)
  slot = now if covered else self._reserve_create_slot(now)
  ```
- **Guard:** covered creates never move the shared pacing cursor, and max_in_flight still bounds them. A 429 starts a `max(Retry-After, 5 s)` cooldown that recovers instead of latching; paced creates keep debiting credit meanwhile, so recovery never over-credits cold claims. A stale count degrades to per-claim behavior.
- **Result:**
  - Before: 160 warm claims took 3.24 s at 50/s vs 1.46 s per-claim (99a4a93d); rolling p99 749 ms vs 560 ms (2ecdf384).
  - RL evidence (25/N, includes warm credit): 1.27 s vs 1.37 s per 160-sandbox step for the fixed per-claim path, from two separate sessions (`pr_board.md` 25/N, RL notes).
  - On GKE (`BENCHMARK_TABLES.md`, commit 8f9ef46d), 640 warm sandboxes (`160 x 4`) reached all-Ready in 8.73 s (`p99` 8.50 s, zero 429s) vs 19.54 s (`p99` 19.30 s) for `per_claim`, a 2.24x speedup (`603 ms` vs `3,120 ms` to first Ready).
  - The first, latching version stuck at paced speed on GKE after one early 429 burst: 1,280 claims in 24.59 s vs 25.6 s at 50/s (notes commit 88792f54). After the non-latching cooldown fix, GKE Campaign 2 (`Bench 2A` `1x1,000` standing warm pool) hit `183.5 claims/s` peak and `0.39 / 0.66 / 1.98 / 3.17 s` mean latency at `c = 100 / 200 / 500 / 1,000` (`2.2x-2.8x` faster than the published Blog baseline `1.1 / 1.8 / 4.3 / 8.8 s`, `0` 429s across `5,400` claims).
- **Code:** `batch_utils.WarmCredit`, `SandboxBatch._create_worker`, `acquire`. **Deep dive:** [RETRY-15](field-guide/retry.md#retry-15), [RETRY-16](field-guide/retry.md#retry-16), [PERF-13](field-guide/perf.md#perf-13), [PERF-10](field-guide/perf.md#perf-10), [PERF-21](field-guide/perf.md#perf-21)

<a id="opt-7"></a>

### OPT-7 Send each group's min_ready creates first

- **Limit protects:** creates share one paced pipe, and names must stay contiguous per group so a re-attached handle can rebuild group order.
- **Why we may skip it:** identity and send order are separate. Ordinal blocks stay per group, but sends sort by `(k >= min_ready, group index, k)`, with k the position in the group. Each group is usable after its own min_ready creates; total fill time is unchanged.
- **Guard:** ordinals are a monotonic high-water mark, never reused, so reordering never changes names ([K8S-18](field-guide/k8s.md#k8s-18), [LEASE-24](field-guide/lease.md#lease-24)).
- **Result:** before (round-robin, create_rps 1000, max_in_flight 50), the RL first group took 0.65 s vs 0.16 s per-claim, since group 0's last claim was create 121 of 160 (`refs/benchmark_rerun_review.md` §2). After: not measured; analytically group 0 completes after its own 4 min_ready creates.
- **Code:** `fill_create_plan`. **Deep dive:** [PERF-14](field-guide/perf.md#perf-14), [PAT-29](patterns.md#pat-29), [K8S-18](field-guide/k8s.md#k8s-18)

<a id="opt-10"></a>

### OPT-10 Never pay an RPC to skip a limiter whose slot is open

- **Limit protects:** with warm credit at 0, acquire() may GET the pool to see if new Ready sandboxes exist.
- **Why we may skip it:** a refresh helps only if the pacer would delay the create. If `_next_create_at <= now`, the slot is already "now" and the GET saves 0 ms.
  ```python
  need_refresh = (not covered and not refreshed and now < deadline
                  and self._next_create_at > now and self._warm_credit.refresh_due(warmpool))
  ```
- **Guard:** the decision runs in the same `_lock` section as `take()`, at most once per acquire, and every on-demand GET holds a max_in_flight slot, preserving pool headroom ([OPT-9](#opt-9)).
- **Result:** RL evidence on GKE before the fix: 2.01 requests per env on 500 pools, up to 998 unbounded GETs at once (notes commit 88792f54, learnings 3.19). After: not measured; analytically about 1 request per env with a free slot, at most max_in_flight concurrent GETs.
- **Code:** `SandboxBatch.acquire`. **Deep dive:** [RETRY-17](field-guide/retry.md#retry-17), [PERF-13](field-guide/perf.md#perf-13), [LOCK-16](field-guide/lock.md#lock-16)

<a id="opt-11"></a>

### OPT-11 A whole rate limiter in one float

- **Limit protects:** the max_in_flight workers and acquire() share one create_rps budget.
- **Why we may skip it** (a token bucket): reserve a future slot under a short lock, sleep outside it. An idle cursor falls into the past, so no burst credit builds up; burst comes only from a real capacity signal ([OPT-3](#opt-3)).
  ```python
  # caller holds self._lock; it sleeps until `slot` after releasing it
  slot = max(earliest, self._next_create_at)
  self._next_create_at = slot + self._create_interval
  ```
- **Guard:** server-wide pushback (Retry-After) moves the shared cursor; a single connection's blip backs off locally, then takes a normal slot (13/N). Retries reserve their own slots, so the total never exceeds create_rps.
- **Result:** creates/s tracked the target: 20.0, 50.0, 99.9, 197.7, 485.2 for targets 20 to 500 (bench tip, #44 defaults). Local backoff: not measured; analytically one blip delays 1 worker, not all 20.
- **Code:** `SandboxBatch._reserve_create_slot`. **Deep dive:** [RETRY-5](field-guide/retry.md#retry-5), [RETRY-6](field-guide/retry.md#retry-6), [PAT-17](patterns.md#pat-17), [BP-24](best-practices.md#bp-24), [BP-25](best-practices.md#bp-25)

## Less memory

Don't hold bytes nobody reads.

<a id="opt-4"></a>

### OPT-4 Don't decode a response nobody reads

- **Limit protects:** the client decodes each response for its caller, and deletecollection returns every deleted object.
- **Why we may skip it:** no caller reads that body; release() judges "done" from its own live count, so the body drains raw.
- **Guard:** sync closes an unfinished response before `release_conn()`, so an interrupted drain can't return a dirty socket. Async checks the HTTP status itself, since raw mode skips it ([LIB-13](field-guide/lib.md#lib-13), [LIB-14](field-guide/lib.md#lib-14)).
- **Result:** release() peak at N=5,000: 69.4 → 0.2 MB, release time unchanged within noise. At N=10,000 the whole 20/N stack gave 138.8 → 9.9 MB; that run hit the apiserver's 504, so the paged count ([OPT-6](#opt-6)) set the 9.9 MB (`pr_board.md` 20/N; C1-pr8-impl.md; e6dde9ff).
- **Code:** `K8sHelper.delete_sandbox_claims_by_label(_preload_content=False)`. **Deep dive:** [PERF-7](field-guide/perf.md#perf-7), [LIB-13](field-guide/lib.md#lib-13), [LIB-14](field-guide/lib.md#lib-14), [BP-15](best-practices.md#bp-15)

<a id="opt-5"></a>

### OPT-5 Drop managedFields as each page arrives

- **Limit protects:** attach holds every listed claim until it builds Members.
- **Why we may skip it:** `metadata.managedFields` is server-side-apply bookkeeping no SDK code reads, and it was 50.6% of claim bytes.
  ```python
  (claim.get("metadata") or {}).pop("managedFields", None)
  ```
- **Guard:** `git grep` proved no reader. The helper's return value changes, a compatibility risk for outside callers.
- **Result:** at N=5,000, list peak 48.7 → 24.1 MB, retained result 46.4 → 18.9 MB (C1-pr8-impl.md). With paging: 69.4 → 24.1 MB, and 138.9 → 43.6 MB at N=10,000 (`pr_board.md` 20/N).
- **Code:** `K8sHelper.list_sandbox_claim_objects`, f80c79b0. **Deep dive:** [PERF-6](field-guide/perf.md#perf-6)

<a id="opt-6"></a>

### OPT-6 Page lists, and fold a count page by page

- **Limit protects:** attach, the leftover check, the 410 re-list, and release rounds all list the whole batch.
- **Why we may skip it:** a paged list is still one consistent snapshot, so pages of 500 cap transient bytes. To learn "how many are live", keep the number and drop each page.
- **Guard:** stop on an empty continue token, never on an empty page. An expired token restarts the list once and resets the count, so no page counts twice ([K8S-9](field-guide/k8s.md#k8s-9)). The count is a consistent read, so a lagging cache can't report zero early ([K8S-5](field-guide/k8s.md#k8s-5)).
- **Result:** list peak 69.4 → 48.7 MB at N=5,000 (a95cb2a1, C1-pr8-impl.md). On the deletecollection-timeout path, release() peak 81.7 → 11.8 MB as shipped (9.7 MB with an unshipped metadata-only count), and 138.8 → 9.9 MB at N=10,000. Three normal runs also hit the apiserver's 60 s 504, so that path is not rare (C1-pr8-impl.md).
- **Code:** `_claim_list_pages`, `K8sHelper.count_live_sandbox_claims`, a95cb2a1, fc9d8557. **Deep dive:** [PERF-4](field-guide/perf.md#perf-4), [PERF-5](field-guide/perf.md#perf-5), [K8S-9](field-guide/k8s.md#k8s-9), [PAT-21](patterns.md#pat-21), [BP-9](best-practices.md#bp-9)

## Less contention

Size shared resources for every user, and wake only who cares.

<a id="opt-9"></a>

### OPT-9 Size the pool for the burst plus the watch and the heartbeat

- **Limit protects:** max_in_flight requests share one HTTP pool with the long-lived watch and Lease renewals.
- **Why we may skip it** (failing on a small pool): the watch holds 1 connection and a renewal borrows 1 briefly, so `pool_size >= max_in_flight + 2` suffices per batch. A default max_in_flight is clamped to `pool_size - 2`.
- **Guard:** an explicit max_in_flight that doesn't fit raises before any API call. The check covers one batch, not a reservation across batches ([PERF-17](field-guide/perf.md#perf-17)). The same bound later covered on-demand pool GETs ([OPT-10](#opt-10)).
- **Result:** pool 100 with the check off: sync discarded a connection in 6 of 6 runs; pool 102: none (bench tip summary, #41). Async never discarded; its late renewals at 1,000 creates/s were likely a busy event loop, not the pool (same source).
- **Code:** `check_pool_size`, `BATCH_POOL_HEADROOM`. **Deep dive:** [PERF-16](field-guide/perf.md#perf-16), [PERF-17](field-guide/perf.md#perf-17), [RETRY-19](field-guide/retry.md#retry-19), [BP-27](best-practices.md#bp-27)

<a id="opt-13"></a>

### OPT-13 Wake only the acquire() whose claim changed

- **Limit protects:** fill consumers watch the whole event stream, so they share one Condition.
- **Why we may skip it:** an acquire() waits on exactly one claim, so it gets its own Event, installed before the create is sent. A watch event wakes at most one acquire().
- **Guard:** global changes (fatal error, re-list, teardown) still wake everyone via `_notify_all`. The Event is cleared under the lock before state is read, so no wake-up is lost ([PAT-13](patterns.md#pat-13)).
- **Result:** not measured. Analytical: with K concurrent acquires, an event costs 1 wake-up, not K; N events cost O(N), not O(N·K).
- **Code:** `SandboxBatch._wake_acquire`, `_acquire_waiters`. **Deep dive:** [LOCK-12](field-guide/lock.md#lock-12), [PAT-13](patterns.md#pat-13)

## Smarter algorithms

Per-event cost should depend on what changed, not on N.

<a id="opt-12"></a>

### OPT-12 O(1) fill accounting per watch event

- **Limit protects:** quorum and group verdicts depend on every member, at tens of thousands of claims.
- **Why we may skip it** (rescanning members): keep per-pool counters and a set of pools still short of quorum; each event re-checks only its pool. An insertion-ordered dict per pool keeps Ready order, so the cohort is its first min_ready entries.
- **Guard:** every counter checks "counted already?", so repeats change nothing, and late arrivals give back assumed losses ([COUNT-2](field-guide/count.md#count-2), [COUNT-6](field-guide/count.md#count-6)).
- **Result:** fill CPU stayed flat: 0.058, 0.063, 0.061 cores at N=1,000, 5,000, 10,000 (bench tip, #35 wake-ups). Analytical: the old 403-storm path was O(N²), now O(N) total.
- **Code:** `BatchState._check_quorum`, `_unreachable_error`. **Deep dive:** [PERF-1](field-guide/perf.md#perf-1), [PERF-2](field-guide/perf.md#perf-2), [PAT-1](patterns.md#pat-1), [PAT-2](patterns.md#pat-2), [PAT-3](patterns.md#pat-3)

## Lessons for your next optimization

- **Ask what each limit protects:** a limit on work that doesn't touch its resource is pure latency ([OPT-3](#opt-3)).
- **Measure the peak before fixing it:** the release() peak looked like the re-list but was the unread delete body ([OPT-4](#opt-4)).
- **Back every shortcut with a fallback that obeys pushback:** a permanent latch turns one 429 into a lasting slowdown ([OPT-3](#opt-3)).
- **Don't pay an RPC to skip a limiter that wouldn't wait:** check the slow path's cost first ([OPT-10](#opt-10)).
- **Use data you already hold:** watch pod IPs, precheck readyReplicas, and your last resourceVersion are free; make stale-prone seeds single-use ([OPT-8](#opt-8), [OPT-14](#opt-14), OPT-18).
- **Keep identity separate from order:** stable names let you reorder for latency without breaking recovery ([OPT-7](#opt-7)).
- **Make per-event cost depend on what changed:** counters, dirty sets, per-key waiters, and pages keep CPU and memory flat; verify at several N ([OPT-12](#opt-12), [OPT-13](#opt-13), [OPT-6](#opt-6)).
- **Bound new paths by old invariants:** extra requests go through the same semaphore and pool headroom as the main work ([OPT-9](#opt-9), [OPT-10](#opt-10)).

---

[Learnings home](../learnings.md) · [Field guide index](../learnings.md#sec-field-guide)
