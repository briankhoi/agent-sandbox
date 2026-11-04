<a id="sec-optimizations"></a>

[Learnings home](../learnings.md) · [Field guide index](../learnings.md#sec-field-guide)

<a id="sec-optimizations"></a>

# Optimization showcase: how batch claim got fast and lean

Here is the one skill to take from this page: for every limit, ask what it protects. Work that doesn't touch that resource may skip the limit, as long as a guard keeps the shortcut honest. Without a batch, claiming N sandboxes costs N creates, N per-claim readiness watches, a Sandbox GET per connect, and N deletes. The batch keeps the creates and cuts almost everything else, from about 5 requests per sandbox to about 1 (see the per-claim request budget below). Most of the wins come from not doing work: fewer requests, fewer waits, fewer bytes. Every shortcut below pairs with a guard that falls back to the slow, safe path when its assumption breaks. The full stories live in the field guide. This page gives the insight behind each one, the guard, and the number.

> [!NOTE]
> **About the numbers.** "Bench tip" means the kind benchmark on `origin/bench/batch-claim`, file `test/benchmarks/batch-claim/bench-report.md`. It ran the pre-merge stack at `44fb487f` (see `results/PROVENANCE.md` there), so it predates warm credit (19/N) and the memory work (20/N). It also predates the min_ready-first create order (its create plan was round-robin), which is why [OPT-7](#opt-7)'s before number comes from it. Numbers from the RL library (`examples/agent-sandbox-rl`, 25/N) only show the effect of an SDK optimization. They are labeled "RL evidence", and their harness differs from the SDK bench.

**The whole-batch bill (bench tip, #47 table, batch vs per-claim baseline):**

| Shape | Requests | Apiserver watch peak | API connections |
| :-- | :-- | :-- | :-- |
| Cold, 300 claims | 309 vs 1,500 | 3 vs 302 | 5 vs 300 |
| Warm, 160 claims | 168 vs 800 | 3 vs 17 | 16 vs 55 |
| RL shape, 160 claims | 246 vs 800 | 3 vs 23 | 15 vs 59 |

Rolling acquire took 2.0 requests per rollout against the baseline's 6.0 (bench tip, rolling table). RL evidence at 25/N: 243 vs 640 requests and 1 vs 160 watches for 160 sandboxes per step (`handoff/pr_board.md`, 25/N).

## The per-claim request budget: from about 5 requests per sandbox to about 1

The per-claim path pays for each sandbox's whole lifecycle on its own: a create, a readiness watch, Sandbox GETs to find the pod, and a delete. The batch path pays only the create per claim and shares the rest across the batch. Request kinds per sandbox, for a claim that becomes Ready, gets its pod looked up, and is deleted (the bench's baseline shape, modeled on agent-sandbox-rl's `SandboxFleet.acquire`):

| Request kind | Per-claim path at 305e9703 | Batch path at 23/N |
| :-- | :-- | :-- |
| Claim create | 1 POST per claim (`SandboxClient.create_sandbox` → `_create_claim` → `K8sHelper.create_sandbox_claim`) | 1 POST per claim, plus a retry only on a transient error (`SandboxBatch._create_one`) |
| Readiness | 1 watch per claim, field-selected to its name (`K8sHelper.wait_for_claim_ready` → `_watch_claim`) | Shared: 1 list, then 1 label-scoped watch that reopens about every 30 s (`SandboxBatch._watch_loop`, `_WATCH_TIMEOUT_SECONDS = 30`) |
| Finding the pod | 1 Sandbox GET per lookup (`Sandbox.get_pod_name`, `Sandbox.get_pod_ip` → `K8sHelper.get_sandbox`). The first command does one more through `get_pod_ip` (`SandboxConnector.send_request`) | 0 in pod-IP modes: the watch fills `Member.sandbox_name` and `pod_ips`, and `SandboxBatch.connect` seeds the pod IP, so `Sandbox._resolve_pod_ip` skips the first GET |
| Liveness | None | Shared: 1 Lease create, 1 PUT every lease_duration/3 (GET only on a 409, `_renew_once`), 1 Lease delete |
| Prechecks | None | Shared: 1 GET per warm pool and per distinct template (`_precheck_pools`) |
| Cleanup | 1 DELETE per claim (`Sandbox.terminate` → `K8sHelper.delete_sandbox_claim`) | Shared: 1 deletecollection plus 1 live count per round, usually 1 round (`_delete_claims_in_rounds`) |
| **Requests per sandbox** | **5** (1,500 for 300 cold, 800 for 160 warm); **6 per rollout** with a first command | **1 + shared/N**: 1.03 cold (309/300), 1.05 warm (168/160), 1.54 RL shape (246/160, 40 pools add 80 precheck GETs); **2.0 per rollout** (create + `release_member` delete) |

The measured breakdown matches the trace (bench tip, so the stack at `44fb487f`). For 300 cold claims the baseline sent 300 creates, 300 watches, 600 Sandbox GETs and 300 deletes, and the batch sent 300 creates plus 9 shared requests: 1 warm pool GET, 1 template GET, 1 Lease create, 1 Lease update, 2 lists, 1 watch, 1 deletecollection and 1 Lease delete (`results/3a2-*-sync-run1.json`, `requests_total`). In rolling acquire the baseline spent 1 create, 1 watch, 3 Sandbox GETs and 1 delete per rollout, and the batch only the create and the delete. Its 2.02 per rollout is 600 creates, 600 deletes, 5 watch reopens and 7 Lease updates over 600 rollouts (`results/6v2-*-sync-run1.json`, `requests_rolling`). An acquire() can also add a pool refresh GET, at most 1 per second per pool, when warm credit runs out ([OPT-10](#opt-10), OPT-16).

**The insight:** the create is the one irreducible write per sandbox. Readiness, liveness and cleanup are questions about the whole batch, so one label-scoped watch, one Lease and one deletecollection answer them for all N claims at once, and the pod IP rides along on the watch for free. Per-claim cost then approaches 1 request as N grows, because everything else is a fixed cost divided by N.
**Deep dive:** [WATCH-1](field-guide/watch.md#watch-1), [K8S-8](field-guide/k8s.md#k8s-8), [PERF-12](field-guide/perf.md#perf-12), [PERF-8](field-guide/perf.md#perf-8) ([OPT-1](#opt-1), [OPT-2](#opt-2), [OPT-8](#opt-8), [OPT-14](#opt-14) below)

## Scoreboard

| # | Optimization | Cost before | After | Evidence | Deep dive |
| :-- | :-- | :-- | :-- | :-- | :-- |
| Lead | Per-claim request budget: share everything but the create (1/N, 3/N, 12/N, 17/N) | 5 requests per sandbox, 6 per rollout | About 1.03 per sandbox, 2.0 per rollout | 309 vs 1,500 (cold), 2.0 vs 6.0 (rolling), bench tip | [WATCH-1](field-guide/watch.md#watch-1), [K8S-8](field-guide/k8s.md#k8s-8), [PERF-12](field-guide/perf.md#perf-12), [PERF-8](field-guide/perf.md#perf-8) |
| [OPT-1](#opt-1) | One label-scoped watch for the batch (1/N) | N readiness watches | 1 watch | Watch peak 3 vs 302 (cold), bench tip | [WATCH-1](field-guide/watch.md#watch-1), [PAT-32](patterns.md#pat-32) |
| [OPT-2](#opt-2) | deletecollection rounds instead of N deletes (12/N) | N DELETEs | 1 request per round, usually 1 round | 1 deletecollection for 10,000 claims, bench tip scenario 2 | [K8S-8](field-guide/k8s.md#k8s-8), [RETRY-11](field-guide/retry.md#retry-11) |
| [OPT-3](#opt-3) | Warm credit skips create_rps pacing (19/N) | Warm fill 3.24 s vs 1.46 s per-claim | Covered creates start at once | 99a4a93d; RL evidence below | [RETRY-15](field-guide/retry.md#retry-15), [RETRY-16](field-guide/retry.md#retry-16), [PERF-13](field-guide/perf.md#perf-13) |
| [OPT-4](#opt-4) | Drain the deletecollection body undecoded (20/N) | release() peak 69.4 MB at N=5,000 | 0.2 MB | `pr_board.md` 20/N, C1-pr8-impl.md | [PERF-7](field-guide/perf.md#perf-7), [LIB-13](field-guide/lib.md#lib-13) |
| [OPT-5](#opt-5) | Drop managedFields per page (20/N) | List peak 48.7 MB (after paging) | 24.1 MB | C1-pr8-impl.md | [PERF-6](field-guide/perf.md#perf-6) |
| [OPT-6](#opt-6) | Paged lists and a page-by-page live count (20/N) | List 69.4 MB; timeout-path release 81.7 MB | 48.7 MB; 11.8 MB | a95cb2a1, C1-pr8-impl.md | [PERF-4](field-guide/perf.md#perf-4), [PERF-5](field-guide/perf.md#perf-5), [K8S-9](field-guide/k8s.md#k8s-9) |
| [OPT-7](#opt-7) | Each group's min_ready creates go first (12/N) | RL first group 0.65 s vs 0.16 s | Not measured | `refs/benchmark_rerun_review.md` | [PERF-14](field-guide/perf.md#perf-14), [PAT-29](patterns.md#pat-29) |
| [OPT-8](#opt-8) | connect() seeds the watched pod IP once (17/N) | 1 Sandbox GET per member at fan-out | 0 | Round 1: 0 vs 300 GETs, bench tip #42 | [PERF-12](field-guide/perf.md#perf-12), [WATCH-8](field-guide/watch.md#watch-8) |
| [OPT-9](#opt-9) | Pool headroom: max_in_flight + 2, with clamping (17/N) | Sync pool overflow drops connections | No discards | 6/6 vs 0 runs discarded, bench tip #41 | [PERF-16](field-guide/perf.md#perf-16), [RETRY-19](field-guide/retry.md#retry-19) |
| [OPT-10](#opt-10) | No refresh GET when a pacing slot is open (19/N) | 2.01 req/env, up to 998 GETs at once | About 1 req/env, GETs under max_in_flight | RL evidence, notes commit 88792f54 | [RETRY-17](field-guide/retry.md#retry-17) |
| [OPT-11](#opt-11) | One-float pacing cursor; blips back off locally (12/N, 13/N) | Retry backoff stalled all 20 workers | 1 worker waits | Rate held at 20.0..485.2/s, bench tip #44 | [RETRY-5](field-guide/retry.md#retry-5), [RETRY-6](field-guide/retry.md#retry-6), [PAT-17](patterns.md#pat-17) |
| [OPT-12](#opt-12) | O(1) fill accounting per watch event (13/N) | O(N²) on mass failure, O(G) per wake | O(1) per event | Fill CPU flat across N, bench tip #35 | [PERF-1](field-guide/perf.md#perf-1), [PERF-2](field-guide/perf.md#perf-2), [PAT-1](patterns.md#pat-1) |
| [OPT-13](#opt-13) | Per-claim acquire waiters (16/N) | Every event wakes every acquire() | 1 wake per event | Not measured | [LOCK-12](field-guide/lock.md#lock-12), [PAT-13](patterns.md#pat-13) |
| [OPT-14](#opt-14) | Lease renewal without read-before-write (3/N) | GET + PUT per renewal | PUT only, GET on 409 | Not measured | [PERF-8](field-guide/perf.md#perf-8), [PAT-33](patterns.md#pat-33) |
| [OPT-15](#opt-15) | Watch bookmarks (1/N) | Quiet watch ages out: 410 + full re-list | Resume from a bookmark | Not measured | [K8S-10](field-guide/k8s.md#k8s-10), [BP-7](best-practices.md#bp-7) |
| OPT-16 | Refresh lock: one pool GET per pool per second (19/N) | K concurrent acquires, K GETs | 1 GET | Not measured | [LOCK-16](field-guide/lock.md#lock-16) |
| OPT-17 | Single-flight first-use pool checks (16/N) | K concurrent first acquires, K checks | 1 check, shared outcome | Not measured | [LOCK-15](field-guide/lock.md#lock-15), [PERF-11](field-guide/perf.md#perf-11), [BP-28](best-practices.md#bp-28) |
| OPT-18 | Reuse reads: precheck GET feeds warm credit, template GET deduped, `_checked_pools` seeded (12/N, 16/N, 19/N) | Extra GET per pool or template | 0 extra | Precheck at 1,000 groups: 1.9% of the fill, bench tip | [PERF-10](field-guide/perf.md#perf-10), [PERF-11](field-guide/perf.md#perf-11) |
| OPT-19 | Hold the in-flight semaphore only during live I/O (12/N, 13/N) | Sleeping retries hold slots | Slots only for live requests | Not measured | [LOCK-10](field-guide/lock.md#lock-10) |
| OPT-20 | Shed doomed creates before reserving slots (16/N, 19/N) | Doomed create burns slot, ordinal, credit, then a DELETE | 0 requests | Not measured (unit test asserts no POST) | [RETRY-7](field-guide/retry.md#retry-7), [BP-26](best-practices.md#bp-26) |
| OPT-21 | Reuse a member's connection while its sandbox and pod IP hold (6/N, 17/N) | New Sandbox per connect() | Cached | 300 connections for 300 members, bench tip #42 | [WATCH-8](field-guide/watch.md#watch-8) |
| OPT-22 | Lazy tombstone for queued READY events (15/N) | O(queue) removal on release | O(1) | Not measured | [PERF-3](field-guide/perf.md#perf-3) |
| OPT-23 | Reaper waits from one shared start, sorted by interval (18/N) | Sum of waits | Longest wait | Not measured | [PERF-15](field-guide/perf.md#perf-15) |
| OPT-24 | Per-create and per-delete logs at DEBUG (12/N, 15/N) | N INFO lines | Aggregate INFO only | Not measured | [PERF-9](field-guide/perf.md#perf-9), [BP-34](best-practices.md#bp-34) |

## Fewer requests

<a id="opt-1"></a>

### OPT-1 One watch for the whole batch

**The constraint:** a caller needs to know when each claim is Ready, and the per-claim path opens a readiness watch per claim.
**The insight:** every claim carries `agents.x-k8s.io/batch-id=<id>`, so one label-scoped watch sees all of them. The cost depends on the batch, not on N. (`sandbox_batch.py: SandboxBatch._watch_loop`, `k8s_helper.py: K8sHelper.watch_sandbox_claims`, 1/N)
**The guard:** list first, then watch from that list's resourceVersion, and on a 410, re-list and diff against the cache, so no event slips through the gap ([WATCH-1](field-guide/watch.md#watch-1), [WATCH-3](field-guide/watch.md#watch-3)).
**Result:** apiserver watch peak 3 vs 302 for 300 cold claims, 3 vs 17 warm (bench tip, #47). Cold also used 60x fewer connections (bench tip summary).
**Deep dive:** [WATCH-1](field-guide/watch.md#watch-1), [WATCH-3](field-guide/watch.md#watch-3), [PAT-32](patterns.md#pat-32), [BP-7](best-practices.md#bp-7)

<a id="opt-2"></a>

### OPT-2 Delete the batch with deletecollection rounds

**The constraint:** release() must remove every claim, and N single DELETEs cost N requests.
**The insight:** the same label selects the whole batch, so one deletecollection removes it in one request. (`sandbox_batch.py: _delete_claims_in_rounds`, 12/N)
**The guard:** a selector delete is a point-in-time snapshot and can time out while the server keeps deleting. So each round re-counts live claims with a consistent read, quiesces in-flight creates first, re-takes the Lease, and gives up only after 3 rounds in a row with no progress (`BATCH_RELEASE_MAX_IDLE_ROUNDS`).
**Result:** 1 deletecollection request and 1 round for 10,000 claims (bench tip, scenario 2 table, "driver requests: deletecollection"). The per-claim path would send 10,000 DELETEs (analytical).
**Deep dive:** [K8S-8](field-guide/k8s.md#k8s-8), [RETRY-11](field-guide/retry.md#retry-11), [RACE-8](field-guide/race.md#race-8), [LEASE-18](field-guide/lease.md#lease-18), [BP-11](best-practices.md#bp-11)

<a id="opt-8"></a>

### OPT-8 Seed connect() with the watched pod IP, once

**The constraint:** a connector must find the pod IP, and the Sandbox object is the source of truth.
**The insight:** the batch watch already holds each Ready member's pod IP, so the first lookup can skip the Sandbox GET. (`sandbox_batch.py: SandboxBatch.connect`, `sandbox.py: Sandbox._resolve_pod_ip`, 17/N)
**The guard:** the seed is single-use, so after a failed request the connector reads the Sandbox as before. The batch also closes a cached connection when the watch reports a different or missing pod IP, or the member is lost or failed ([WATCH-8](field-guide/watch.md#watch-8)).
```python
seeded, self._seeded_pod_ip = self._seeded_pod_ip, None
```
**Result:** fan-out round 1 made 0 Sandbox GETs vs the baseline's 300 for 300 members (bench tip, #42).
**Deep dive:** [PERF-12](field-guide/perf.md#perf-12), [WATCH-8](field-guide/watch.md#watch-8)

<a id="opt-14"></a>

### OPT-14 Renew the Lease without reading it first

**The constraint:** a renewal must not overwrite someone else's write.
**The insight:** optimistic concurrency already catches that. A PUT from the last copy you wrote carries its resourceVersion, and the server rejects it with a 409 if anything changed. (`sandbox_batch.py: SandboxBatch._renew_once` / `_write_renewal`, 3/N)
**The guard:** only a 409 triggers a GET, and the handle writes again only if it is still the holder (its own lost write landed). Sync Lease requests go out once (urllib3 retries off), so a retry can't fake a conflict ([LEASE-28](field-guide/lease.md#lease-28), [LIB-6](field-guide/lib.md#lib-6)).
**Result:** not measured. Analytical: 2 requests per renewal down to 1, every lease_duration/3.
**Deep dive:** [PERF-8](field-guide/perf.md#perf-8), [PAT-33](patterns.md#pat-33), [BP-4](best-practices.md#bp-4), [LEASE-28](field-guide/lease.md#lease-28)

<a id="opt-15"></a>

### OPT-15 Ask for watch bookmarks

**The constraint:** a watch reconnects periodically, and its resourceVersion must still be in the server's history window when it does.
**The insight:** on a quiet batch no events arrive to advance the resourceVersion. Bookmarks advance it for free. (`k8s_helper.py: K8sHelper.watch_sandbox_claims`, `allow_watch_bookmarks=True`, 1/N)
**The guard:** bookmarks only move the resourceVersion and never touch member state. A real 410 still falls back to the re-list and diff ([WATCH-3](field-guide/watch.md#watch-3)).
**Result:** not measured. Analytical: each reconnect of an idle watch avoids a 410 plus a full re-list of N claims.
**Deep dive:** [K8S-10](field-guide/k8s.md#k8s-10), [BP-7](best-practices.md#bp-7), [WATCH-1](field-guide/watch.md#watch-1)

## Less waiting

<a id="opt-3"></a>

### OPT-3 Warm credit: covered creates skip create_rps

**The constraint:** create_rps exists so a burst of cold claims doesn't start many pods on one node at once (10.2 s to first Ready unpaced vs 1.35 s at 50/s, 99a4a93d).
**The insight:** a warm claim adopts a Ready sandbox and starts no pod, so it causes none of the load create_rps protects. The precheck already GETs each pool, so its `status.readyReplicas` becomes free per-pool credit. (`batch_utils.py: WarmCredit`, `sandbox_batch.py: SandboxBatch._create_worker` and `acquire`, 19/N)
**The guard:** a covered create never moves the shared cursor, and max_in_flight still bounds it. A 429 starts a cooldown of `max(Retry-After, 5 s)` that recovers instead of latching, and paced creates keep debiting credit during it, so recovery never over-credits cold claims. A stale count degrades to what per-claim creates do anyway.
```python
slot = now if covered else self._reserve_create_slot(now)
```
**Result:** before, 160 warm claims took 3.24 s at 50/s vs 1.46 s per-claim (99a4a93d), and rolling acquire p99 was 749 ms vs 560 ms (2ecdf384). RL evidence at 25/N (warm credit included): 1.27 s per 160-sandbox step vs 1.37 s for the fixed per-claim path, from two separate sessions (`pr_board.md`, 25/N and RL notes). The first latching version stuck at paced speed on GKE after one early 429 burst (1,280 claims in 24.59 s vs 25.6 s at 50/s; notes commit 88792f54), which is what the cooldown fixes. After the fix: not measured.
**Deep dive:** [RETRY-15](field-guide/retry.md#retry-15), [RETRY-16](field-guide/retry.md#retry-16), [PERF-13](field-guide/perf.md#perf-13), [PERF-10](field-guide/perf.md#perf-10)

<a id="opt-7"></a>

### OPT-7 Send each group's min_ready creates first

**The constraint:** creates go through one paced pipe, and claim names must stay contiguous per group so a re-attached handle can rebuild group order.
**The insight:** identity and send order are separate. Keep ordinal blocks per group, but sort sends by `(k >= min_ready, group index, k)`, so a group becomes usable after its own min_ready creates. Total fill time stays the same. (`batch_utils.py: fill_create_plan`, 12/N)
**The guard:** ordinals stay a monotonic high-water mark and are never reused, so order changes never change names ([K8S-18](field-guide/k8s.md#k8s-18), [LEASE-24](field-guide/lease.md#lease-24)).
**Result:** before (round-robin order, create_rps 1000 with max_in_flight 50), the RL first group took 0.65 s vs 0.16 s per-claim, because group 0's last claim was create 121 of 160 (`refs/benchmark_rerun_review.md`, section 2). After: not measured. Analytical: group 0 completes after its own min_ready creates (4 here), not after create 121.
**Deep dive:** [PERF-14](field-guide/perf.md#perf-14), [PAT-29](patterns.md#pat-29), [K8S-18](field-guide/k8s.md#k8s-18)

<a id="opt-10"></a>

### OPT-10 Never pay an RPC to skip a limiter whose slot is open

**The constraint:** once warm credit hits 0, acquire() may re-read the pool to learn whether new Ready sandboxes exist.
**The insight:** a refresh only helps if the pacer would delay the create. When `_next_create_at <= now`, the paced slot is already "now", so the GET saves 0 ms. (`sandbox_batch.py: SandboxBatch.acquire`, 19/N)
**The guard:** the refresh decision runs in the same `_lock` section as `take()`, at most once per acquire, and every on-demand GET holds a max_in_flight slot, so pool headroom stays intact ([OPT-9](#opt-9)).
```python
need_refresh = (not covered and not refreshed and now < deadline
                and self._next_create_at > now and self._warm_credit.refresh_due(warmpool))
```
**Result:** RL evidence on GKE before the fix: 2.01 requests per env on 500 pools, and up to 998 unbounded GETs at once (notes commit 88792f54, learnings 3.19). After: not measured. Analytical: about 1 request per env when the pacer has a free slot, and at most max_in_flight concurrent GETs.
**Deep dive:** [RETRY-17](field-guide/retry.md#retry-17), [PERF-13](field-guide/perf.md#perf-13), [LOCK-16](field-guide/lock.md#lock-16)

<a id="opt-11"></a>

### OPT-11 A whole rate limiter in one float

**The constraint:** max_in_flight workers plus acquire() share one create_rps budget.
**The insight:** reserve a future time slot under a short lock (`slot = max(earliest, _next_create_at)`; `_next_create_at = slot + interval`) and sleep outside it. An idle cursor sits in the past, so no burst credit builds up. Burst comes only from a real capacity signal ([OPT-3](#opt-3)). (`sandbox_batch.py: SandboxBatch._reserve_create_slot`, 12/N)
**The guard:** server-wide pushback (Retry-After) moves the shared cursor. A single connection's blip backs off locally and then takes a normal slot, so one blip doesn't stall every worker (13/N). Retries reserve their own slots, so they never push the total past create_rps.
**Result:** achieved creates/s tracked the target: 20.0, 50.0, 99.9, 197.7 and 485.2 for targets 20 to 500 (bench tip, #44 defaults). Local backoff: not measured. Analytical: one blip delays 1 worker instead of all 20.
**Deep dive:** [RETRY-5](field-guide/retry.md#retry-5), [RETRY-6](field-guide/retry.md#retry-6), [PAT-17](patterns.md#pat-17), [BP-24](best-practices.md#bp-24), [BP-25](best-practices.md#bp-25)

## Less memory

<a id="opt-4"></a>

### OPT-4 Don't decode a response nobody reads

**The constraint:** deletecollection answers with the full list of every object it deleted.
**The insight:** no caller reads it. release() decides "done" from its own live count, so the body can be drained raw. (`k8s_helper.py: K8sHelper.delete_sandbox_claims_by_label`, `_preload_content=False`, 20/N)
**The guard:** the sync helper closes an unfinished response before `release_conn()`, so an interrupted drain can't return a dirty socket to the pool. The async helper checks the HTTP status itself, because raw mode skips that check ([LIB-13](field-guide/lib.md#lib-13), [LIB-14](field-guide/lib.md#lib-14)).
**Result:** release() peak at N=5,000 fell from 69.4 MB to 0.2 MB, with release time unchanged within noise. At N=10,000 the whole 20/N stack took it from 138.8 MB to 9.9 MB, but that run hit the apiserver's 504, so the paged count ([OPT-6](#opt-6)) set the 9.9 MB (`pr_board.md` 20/N; C1-pr8-impl.md; e6dde9ff).
**Deep dive:** [PERF-7](field-guide/perf.md#perf-7), [LIB-13](field-guide/lib.md#lib-13), [LIB-14](field-guide/lib.md#lib-14), [BP-15](best-practices.md#bp-15)

<a id="opt-5"></a>

### OPT-5 Drop managedFields as each page arrives

**The constraint:** attach keeps every listed claim until it builds Members.
**The insight:** `metadata.managedFields` is server-side-apply bookkeeping that nothing in the SDK reads, and it was 50.6% of claim bytes. (`k8s_helper.py: K8sHelper.list_sandbox_claim_objects`, f80c79b0, 20/N)
**The guard:** `git grep` proved no reader. The change alters what the helper returns, so it carries a compatibility risk for any outside caller of the helper.
```python
(claim.get("metadata") or {}).pop("managedFields", None)
```
**Result:** at N=5,000, list peak 48.7 MB to 24.1 MB, retained result 46.4 MB to 18.9 MB (C1-pr8-impl.md). Combined with paging, the list peak went 69.4 to 24.1 MB, and 138.9 to 43.6 MB at N=10,000 (`pr_board.md` 20/N).
**Deep dive:** [PERF-6](field-guide/perf.md#perf-6)

<a id="opt-6"></a>

### OPT-6 Page lists, and fold a count page by page

**The constraint:** attach, the leftover check, the 410 re-list and release rounds all list the whole batch.
**The insight:** a paged list is still one consistent snapshot, so pages of 500 cap the transient bytes. When you need only "how many are live", keep the number and drop each page. (`k8s_helper.py: _claim_list_pages`, `K8sHelper.count_live_sandbox_claims`, a95cb2a1 and fc9d8557, 20/N)
**The guard:** stop only on an empty continue token, never on an empty page. An expired continue token restarts the list once, and the count resets on restart so no page counts twice ([K8S-9](field-guide/k8s.md#k8s-9)). The count is a consistent read, so a lagging cache can't report zero too early ([K8S-5](field-guide/k8s.md#k8s-5)).
**Result:** list peak 69.4 MB to 48.7 MB at N=5,000 (a95cb2a1, C1-pr8-impl.md). On the deletecollection-timeout path, release() peak 81.7 MB to 11.8 MB with the shipped commits (9.7 MB with an optional metadata-only count that didn't ship), and 9.9 MB at N=10,000 against 138.8 MB. Three normal runs hit the apiserver's 60 s 504 too, so that path is not rare (C1-pr8-impl.md).
**Deep dive:** [PERF-4](field-guide/perf.md#perf-4), [PERF-5](field-guide/perf.md#perf-5), [K8S-9](field-guide/k8s.md#k8s-9), [PAT-21](patterns.md#pat-21), [BP-9](best-practices.md#bp-9)

## Less contention

<a id="opt-9"></a>

### OPT-9 Size the pool for the burst plus the watch and the heartbeat

**The constraint:** max_in_flight requests share one HTTP pool with the long-lived watch and the Lease renewals.
**The insight:** the watch holds 1 connection for life, and a renewal borrows 1 briefly, so `pool_size >= max_in_flight + 2` is enough per batch. A default max_in_flight on a small pool is clamped to `pool_size - 2` instead of failing. (`batch_utils.py: check_pool_size`, `BATCH_POOL_HEADROOM`, 17/N)
**The guard:** an explicit max_in_flight that doesn't fit raises before any API call. The check covers one batch only, not a lifetime reservation across batches ([PERF-17](field-guide/perf.md#perf-17)). The same bound later covered on-demand pool GETs ([OPT-10](#opt-10)).
**Result:** with pool 100 and the check off, the sync client discarded a connection in 6 of 6 runs; with 102, in none (bench tip summary, #41). Async never discarded, and its late renewals at 1,000 creates/s were likely a busy event loop, not the pool (same source).
**Deep dive:** [PERF-16](field-guide/perf.md#perf-16), [PERF-17](field-guide/perf.md#perf-17), [RETRY-19](field-guide/retry.md#retry-19), [BP-27](best-practices.md#bp-27)

<a id="opt-13"></a>

### OPT-13 Wake only the acquire() whose claim changed

**The constraint:** fill consumers care about the whole stream, so they share one Condition.
**The insight:** each acquire() waits on exactly one claim, so it gets its own Event, installed before the create is sent. A watch event wakes at most one acquire(). (`sandbox_batch.py: SandboxBatch._wake_acquire`, `_acquire_waiters`, 16/N)
**The guard:** global changes (a fatal error, a re-list, teardown) still wake everyone through `_notify_all`. The Event is cleared under the lock before reading state, so no wake-up is lost ([PAT-13](patterns.md#pat-13)).
**Result:** not measured. Analytical: with K concurrent acquires, each event costs 1 wake-up instead of K, so a burst of N events costs O(N) instead of O(N·K).
**Deep dive:** [LOCK-12](field-guide/lock.md#lock-12), [PAT-13](patterns.md#pat-13)

## Smarter algorithms

<a id="opt-12"></a>

### OPT-12 O(1) fill accounting per watch event

**The constraint:** quorum and group verdicts depend on every member, and the design targets tens of thousands of claims.
**The insight:** keep per-pool counters and a set of pools still short of quorum. Each event re-checks only the pool it touched. An insertion-ordered dict per pool keeps Ready order, so the cohort is just its first min_ready entries. (`batch_state.py: BatchState._check_quorum`, `_unreachable_error`, 13/N)
**The guard:** every counter has a "counted already?" check, so a repeated observation changes nothing, and late arrivals give back assumed losses ([COUNT-2](field-guide/count.md#count-2), [COUNT-6](field-guide/count.md#count-6)).
**Result:** driver CPU during fills stayed flat as N grew: 0.058, 0.063 and 0.061 cores at N=1,000, 5,000 and 10,000 (bench tip, #35 wake-ups). Analytical: the old 403-storm path was O(N²), now O(N) total.
**Deep dive:** [PERF-1](field-guide/perf.md#perf-1), [PERF-2](field-guide/perf.md#perf-2), [PAT-1](patterns.md#pat-1), [PAT-2](patterns.md#pat-2), [PAT-3](patterns.md#pat-3)

## Lessons for your next optimization

- **Ask what each limit protects.** create_rps protects nodes from cold pod starts, so warm claims may skip it. A limit applied to work that doesn't touch its resource is pure latency.
- **Measure the peak before you optimize it.** The release() memory peak looked like the re-list and was really the unread deletecollection body. One measurement moved the fix to the right line.
- **Every shortcut needs a fallback that obeys server pushback.** Warm credit backs off on a 429 through a bounded cooldown. A permanent latch turns one blip into a lasting regression.
- **Never pay an RPC to skip a limiter whose slot is already open.** Check whether the slow path would actually wait before you buy information to avoid it.
- **Use data you already hold.** The watch knows pod IPs, the precheck knows readyReplicas, and the last write carries the resourceVersion. Seed caches from them, and make a seed single-use when it can go stale.
- **Keep identity separate from order.** Stable names let you reorder work freely for latency without breaking recovery.
- **Make per-event cost depend on what changed, not on N.** Use counters, dirty sets, per-key waiters and pages, and verify it by checking that CPU and memory stay flat as N grows.
- **Bound the new path by the old invariants.** An optimization that adds requests (refresh GETs, pool checks) must go through the same semaphore and pool headroom as the main work.

---

[Learnings home](../learnings.md) · [Field guide index](../learnings.md#sec-field-guide)
