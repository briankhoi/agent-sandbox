# Benchmark rerun review

2026-10-21. An independent review of the rerun pushed to `bench/batch-claim` at `b48303ff`. Marks: **(measured)** is a number from the result files, **(verified)** means I read the code or ran the check myself, and **(inferred)** is reasoning I haven't checked.

## Summary

- **The rerun is sound.** I checked the push and spot-checked the numbers against the JSONs, and none of the deviations undermines a result.
- **On kind, no create rate makes the batch "way" faster on warm pools.** From 200/s up, all Ready is 0.64x to 0.86x the baseline's. The cap is the controller's adoption, not the SDK. The faster the creates go out, the longer the tail after the fill grows (from 0.07 s at 50/s to about 0.6 s at 500/s and above), and the sum stays around 0.9 to 1.2 s **(measured)**.
- **The batch's clear win is cost**: 4.8x fewer requests, 11x fewer watches, and 2.2x fewer connections **(measured)**.
- **Both regressions have known causes, and code can fix both:**
  - **RL first group (0.65 s vs 0.16 s).** The batch interleaves creates round-robin across groups, so no group finishes until about 3/4 of the fill. **(verified in code, consistent with measured)**
  - **Cold first Ready at a high rate (10.2 s vs 1.35 s).** This is not worse than today's path: the baseline is 10.5 s. Sending 300 pods at once to one node slows the first one, for the baseline and the batch alike. **(measured; the mechanism is inferred)**
- **Recommendation: no plain default change.** Instead, two changes in PR 7. Together they should make the defaults win everywhere: clearly on cold first Ready (about 7.8x) and on cost, and by 0.7 to 0.8x on warm time. Kind's controller caps the warm win, so no setting clearly wins warm time there **(inferred from measured rows)**:
  1. Group-ordered creates.
  2. Warm-aware pacing: claims the warm pools can already cover go out without pacing, and the rest at 50/s.
  Then one short validation run.
- **The RL design thread can start now.** The prototype run should wait for PR 7's code.

## 1. Checks on the run

| Check | Result |
|---|---|
| Push | `b48303ff` has one commit on top of the unchanged `44fb487f`. Author is Brian Nguyen <brianknguyen@google.com>, with no trailers. Versus `2aaf148`, only `test/benchmarks/batch-claim/` changed (the report, `bench.py`, `report.py`, results). No SDK file changed **(verified)**. The commit message still describes only the first run, which is a nit. |
| Code identity | Every 3s run records code_sha `44fb487f`, tree `8a7adc54` (the same tree as reaper tip 600a880), or base `7fd162d9`, tree `588a31ef`. Provenance diffs show the installed SDKs are byte-identical to `git archive` of each commit **(verified tree SHAs; the diffs are the agent's report)**. |
| Table vs JSON | Warm baseline last Ready 1.48/1.29/1.40 s, median 1.40. 1000-mif50: 1.08/0.86/0.90. RL 1000-mif50 first group 0.75/0.65/0.63. RL baseline first group 0.28/0.16/0.08. Cold 3a2 1000-mif50 first Ready 10.15/10.21/13.66. Baseline 10.47/10.87/8.24. 6v2 and 1d also match **(verified)**. |
| Interleaving | The finish times show runs interleaved across variants (for example warm 200 at 15:48, 15:53 and 15:58) **(verified)**. |
| Supersede mishap | `2b-n10000-pool102-sync-run2.json` is byte-identical to `2aaf148`'s **(verified)**. No effect. |
| `wait_clean` bulk delete | Runs only between runs, and only on unscheduled pods. Cleanup timing is taken before it. No effect. |
| memdiag method | `gc.collect()` before each snapshot and dropping the probe's own frames are correct. The limit is that tracemalloc can't see native memory, so the result means "not Python objects". It doesn't say what does grow. Valid as stated. |
| 1a reruns | All five imported runs were rerun, so no run with an unknown SHA is left. |
| Rule label "apiserver binds" | Applied as written. The numbers say more, though: the tail after the fill is 66% of last Ready in warm (0.59 of 0.90 s), and it grows as the fill shrinks. It reads more as client concurrency for the fill plus controller adoption for the rest. That reading is **(inferred)**. |
| RL near miss | RL 1000-mif50 failed "slowest run faster than the baseline's fastest" by 0.02 s (1.13 vs 1.11), and it failed first group. |
| Cold noise | 3a2 last Ready ranges are wide (baseline 23.3 to 40.9 s, batch 50/s 22.7 to 33.1 s), so the "last Ready ≤1.1x" gate is close to noise. First Ready is stable. |

## 2. Why the two regressions happen

**RL first group, 0.65 s vs 0.16 s.**
- `batch_utils.fill_create_plan` (`batch_utils.py:345-361`) sorts creates by `(k >= min_ready, k/size, group index)`. With 40 groups of 4 and `min_ready=4`, that's round-robin: claim 0 of every group, then claim 1 of every group, and so on. Group 0's last claim is the 121st of 160 creates, and every group finishes in the last quarter of the fill **(verified)**.
- At 1000-mif50 the batch sent 355 creates/s, so 121 creates take about 0.34 s, plus adoption p50 0.25 s, which gives about 0.6 s. The measured value is 0.65 s **(measured inputs; the fit is inferred)**.
- The baseline sends all 160 at once on 160 threads, so one group's four claims can finish first, at 0.16 s.
- This ordering is approved decisions #11 and #12, made so that "no group waits behind a whole earlier group". The new fact is the measured first group time. `wait_for_quorum` needs every group at `min_ready` (`sandbox_batch.py:503-504`), so for quorum the order inside the `min_ready` part doesn't matter. Only streaming consumers (`iter_ready_groups`, `events`) feel it **(verified)**.

**Cold first Ready, 10.2 s at 1000-mif50 vs 1.35 s at 50/s.**
- At 1000/s the batch sends 300 claims in 0.4 s, so 300 pods start on the one kind node at once. The baseline does the same and gets 10.5 s. At 50/s, the first pod starts with only a few others alongside it **(measured)**.
- The cause is likely kubelet and container-runtime contention on a single node **(inferred: no node metrics were taken)**.
- So a high rate doesn't make the batch worse than today's path. It loses the head start that 50/s gives cold pools: 1.35 s vs the baseline's 10.5 s, 7.8x better.
- Last Ready is pod-start bound at every rate (22 to 33 s).

## 3. Recommendation

### A. PR 7: two changes that make the defaults win, then a short validation run

**1. Group-ordered creates.**
- Keep "each group's `min_ready` claims first, then the rest".
- Inside each part, create group by group, in order of `min_ready`, smallest first. That's shortest-job-first, which minimizes mean group finish time and keeps a small group from waiting behind a big one. **[Superseded: the approved and built order is the caller's group order, in PR 2 (`state.md`), not sorted by `min_ready`.]**
- Code: `fill_create_plan`'s sort key, one function shared by both shells, plus its test.
- Expected: RL first group at about the cost of 4 creates plus adoption, roughly 0.1 to 0.25 s against the baseline's 0.16 s. The last group stays where it is **(inferred)**.
- What could make this wrong: if the controller adopts one pool's claims more slowly than claims spread across pools, a big group created in one block could take longer. I haven't checked the controller for per-pool serialization, so the validation run measures it.

**2. Warm-aware pacing.**
- `_precheck_pools` already GETs each warm pool (`sandbox_batch.py:1306`), and its `status.readyReplicas` is there (`sandboxwarmpool_types.go:98-100`) **(verified)**.
- Give each pool a credit equal to its ready replicas at the precheck. Creates within the credit take no pacing slot, so only `max_in_flight` bounds them. The rest stay at `create_rps` (50).
- Expected:
  - Warm: claims go out at the measured unpaced rate (474 creates/s at mif 20) and finish like the 1000/s rows, 0.82x warm and 0.72x RL at mif 20 **(measured for 1000-mif20)**.
  - Cold: keeps the 50/s head start, 1.35 s vs the baseline's 10.5 s **(measured)**.
- A stale credit means some claims burst onto a cold pool, which is what the baseline does today.
- Code: pacing in both shells plus tests, probably 30 to 60 lines **(inferred)**.

**Alternatives considered.**

| Option | Warm / RL time vs baseline | Cold first Ready | RL first group | Cost | Why not |
|---|---|---|---|---|---|
| **Group order + warm-aware pacing (recommended)** | Like the 1000/s rows: 0.72 to 0.82x at mif 20 **(inferred from measured rows)** | 1.35 s (50/s) vs 10.5 s | About the baseline's **(inferred)** | Two code changes, plus validation | — |
| `create_rps` 200, mif 20, nothing else | 0.81x / 0.86x **(measured)** | Not measured at 200. Likely near the baseline's, since 300 creates take 1.5 s **(inferred)** | 1.07 s vs 0.16 s **(measured)** | One constant | Fails Brian's goal: not a clear win, and RL first group is 6.7x worse |
| 200 + group order | 0.81x / about 0.86x | As above | About the baseline's **(inferred)** | Two small changes | Warm only 0.8x, and cold loses its head start |
| 1000, mif 50 | 0.64x / 0.67x **(measured)** | 10.2 s, the baseline's level **(measured)** | 0.65 s | A constant, and the default pool would need to be ≥52, while sync defaults to 5 per CPU | Needs a bigger default pool, has the most exposure to production priority and fairness, and loses cold's head start |
| Adaptive pacing (#44) from 429s and latency | — | — | — | The largest change | Kind showed no 429s, and POST p99 stayed in the 0.1 to 0.2 s bucket at every rate, so there's no signal to adapt to **(measured)**. It can't tell warm from cold |
| A token bucket with a fixed burst | Good under the burst | A burst onto a cold pool has the same contention | — | A new knob | The burst size is arbitrary, and it ignores what the pool actually has |

**Max in flight.** Keep 20. On warm pools, mif 50 sent no faster than mif 20 (421 vs 474 creates/s at 1000) **(measured)**, and 50 would make `claim_batch` raise at default settings on sync clients with fewer than 11 CPUs.

**Validation run after PR 7** (local, about 45 minutes):
- 3s warm and RL at the new defaults vs the baseline, 3 runs each.
- 3a2 cold at the new defaults vs the baseline.
- 6v2a rolling.
- Optional, and it needs your approval to create a cluster: a 3-node kind with warm N=500, the only way to test whether the time gap grows with scale, which is where the per-claim path's N threads and N watches should start to cost **(inferred)**.

### B. Other PR 7 items

| Item | Verdict | Numbers |
|---|---|---|
| Claim-list pagination | **Include. Low priority, cheap** | One list of 5,000 claims peaks at 69.4 MB traced and keeps 0 MB afterwards **(measured, 1d)**. So it's a transient spike, about 14 KB per claim, that leaves RSS high afterwards (attach RSS +182 MB at 10k). Paging at 500 would cap the spike near 7 MB **(inferred)**. 1d replaces the RSS-delta readings, which varied with the fill settings (92 to 114 MB at the same N). |
| Don't parse the deletecollection response | **Include. Cheap** | `release()` peaks at 69.5 MB, the same as a full list, and keeps 0.05 MB **(measured)**. The call parses and throws away the whole deleted list (`k8s_helper.py:671`) **(verified)**. `_preload_content=False` would skip that **(inferred)**. |
| Metadata-only release list, `members()` cache, wake-up items | **Drop** (metadata-only reversed in section 7: include, in PR 8; `members()` cache and wake-ups stay dropped) | Ruled out in the first run (listing 0.0 to 0.1% of `release()`; 9 ms at 10k; 0.05 to 0.08 cores) **(measured)** |
| 17 to 20 new TCP connections per command round | **No action** | They come from the existing per-command Sandbox GET overflowing a 22-connection pool, against the baseline's 1000 **(inferred)**. That's about 2 handshakes per 300 commands. If you approve the Transport thread's skip-GET card, they go away. |

### C. Not for the batch series: async native memory
- The base code's async loop grew 93 → 514 MB. The batch's async loop grew 94 → 282 MB. Python-traced memory stayed flat at 11 MB **(measured)**.
- So this is existing growth in the async SDK's native stack, or allocator fragmentation, not batch state **(inferred)**.
- Recommended: a separate upstream issue with these numbers. A cheap next probe there is `MALLOC_ARENA_MAX=2`, or jemalloc, to tell fragmentation apart from a leak.

### D. Rolling acquire (6v2, not starved)
- Rollouts/min are equal: 234 to 236 vs the baseline's 233.5. The 10 s hold caps throughput **(measured)**.
- Acquire p50 is better (79 to 92 ms vs 96) **(measured)**.
- p99 is worse at 50/s (749 ms vs 560) and better at 1000/s (288 ms). The 50/s spacing shows up when acquires bunch **(measured)**.
- Warm-aware pacing as described covers the fill. Applying it to `acquire` needs a fresh ready count, so it's left out for now.

## 4. Can the RL work start?

- **Start the RL design thread now.** It has the numbers it needs:
  - cost ratios of 4.8x requests and 11x watches;
  - warm time is capped by adoption on kind;
  - the RL first group regression and its fix;
  - rolling parity at 6v2.
- Notes commit `44ceb6b4` only changes `reaper_contract.md`. Its RL section is the same as Brian's own edit `71a0a77` (no diff), so it's the right text **(verified)**.
- **Run the prototype only once PR 7's two changes exist**, on that branch before merge. On today's code, STREAM and GROUP dispatch would carry the round-robin first-group delay, which would understate the batch. The prototype's `TUNED_RPS` arm becomes unnecessary once pacing is warm-aware. Keep one arm at defaults and one at the baseline.

## 5. Decisions for Brian

1. **PR 7 = group-ordered creates (smallest first, `min_ready` first kept) + warm-aware pacing + pagination + skipping the deletecollection body.** Drop the three ruled-out items. Keep the default `create_rps` 50 and `max_in_flight` 20. Recommended: yes. Group order reverses approved #11 and #12, on the new measured first-group fact. **[Superseded: group order went into PR 2 in the caller's order, and pagination, the metadata-only re-list and the deletecollection body are PR 8 (`state.md`).]**
2. **A validation run after PR 7** (about 45 minutes local), plus optionally a 3-node kind at warm N=500, which needs your OK to create the cluster. Recommended: the run yes. The scale test only if you want a time result at scale before GKE.
3. **Start the RL design thread now, and run its prototype on the PR 7 branch.** Recommended: yes.
4. **Async native memory goes to a separate upstream issue**, outside the batch series. Recommended: yes.

## 6. Follow-up answers (2026-10-21)

**Q1. Isn't round-robin better overall, because pools refill in parallel? No, and the recommendation holds. There's no new fact.**
- Refill parallelism comes from the warm-pool controller, not from claim order:
  - the controller defaults to one warm-pool worker (`--sandbox-warm-pool-concurrent-workers=1`, `cmd/agent-sandbox-controller/main.go:138`);
  - each pool's deficit is processed serially under its own reconcile key (`sandboxwarmpool_controller.go:187-197`) **(verified)**.
- Refill doesn't gate this batch's Ready when the pool already holds the claims. In 3s rl, each group's pool had 4 Ready for its 4 claims **(measured setup)**.
- Claims beyond a pool's Ready count take the claim controller's cold path (`sandboxclaim_controller.go:668-676`, 50 workers), not refill **(verified)**.
- Overall time, meaning the last group:
  - it's the same in both orders, since both send the same creates at the same pacing, so the last create goes out at the same moment **(inferred from `fill_create_plan`, which changes only the order)**;
  - `wait_for_quorum` waits for every group, so its result is unchanged too **(verified, `sandbox_batch.py:503-504`)**.
- First and median group get earlier with group order. Early pools also start their refills sooner, and the last pool starts at the same moment as with round-robin, so the next step's pools are never ready later **(inferred)**.
- Which matters:
  - a synchronous step barrier waits for the last group, which doesn't change;
  - STREAM or GROUP dispatch, async trainers and eval start rollouts as groups arrive, so they gain. STREAM is the prototype's default.
- Group order is never worse on overall time **(inferred)**. The validation run measures it.

**Q2. kwok scalability test: never used in this project.**
- The project instructions say to use kind, and every prompt followed that. No notes or design file mentions kwok **(verified by search)**.
- `dev/ci/presubmits/test-e2e-scalability-kwok`:
  - runs the controller against a kwokctl cluster with 100 simulated nodes, whose pods are fake: no kubelet and no containers;
  - its apiserver has `--disable-qps-limits` and raised in-flight limits;
  - ClusterLoader2 creates claims with client-go at a fixed QPS (75/s for 10 s, a warm pool of 1,050);
  - it gates on the controller's claim adoption latency (p50/p90 ≤ 300 ms, p99 ≤ 500 ms) **(verified)**.
- So it measures the controller, not the SDK. It never calls `claim_batch` or `create_sandbox`.
- **It is relevant in one way:** running our `bench.py` driver against a kwok cluster would test warm and RL shapes at 1,000 to 10,000 claims across 100 fake nodes. That removes kind's single-node limits (400 pods, kubelet contention), and it's where the per-claim path's N threads and N watches should start to cost **(inferred)**.
- **It can't show:** cold start, image pulls, or commands. Fake pods can't exec, so scenarios 4 and 6 are out. Its apiserver limits also differ from production.
- Using it means stepping outside the project's "use kind" rule, so it's your call.

**Q3. If release() doesn't parse the deletecollection response, how does it know when it's done?**
- It already doesn't use the response.
  - `delete_sandbox_claims_by_label` returns nothing (`k8s_helper.py:660-678`);
  - `_delete_claims_in_rounds` decides by re-listing after each round. It stops when no claim is left without a `deletionTimestamp`, and gives up after idle rounds (`sandbox_batch.py:1347-1415`) **(verified)**.
- The change would pass `_preload_content=False` and then release the response so the connection returns to the pool. A non-2xx status still raises `ApiException` **(inferred from the kubernetes client)**.
- **A correction to section 3.B:** that re-list is a full-object list too, and it runs while the 5,000 claims are still terminating (claims were gone 20 s after `release()` returned) **(measured)**. So `release()`'s 69.5 MB peak could come from the re-list, not the response, or from both. The run doesn't separate them.
- Pagination of `list_sandbox_claim_objects` covers the re-list. Skipping the response body covers the rest. Do both, or measure first.

**Q4. "17 to 20 new TCP connections per command round."**
- In scenario 4, each command after a member's first does a Sandbox GET to the apiserver. That's the existing sandboxd pod-IP behavior.
- 300 commands run at once against the batch client's 22-connection API pool. When it's full, the sync client opens one-off connections, and the netns counter saw 17 to 20 per round of 300. The baseline's pool of 1000 never filled **(measured counts; the cause is inferred)**.
- It doesn't matter: rounds 2 to 10 p50 was 9.71 ms vs the baseline's 9.58 ms **(measured)**.
- It goes away if you approve the Transport thread's skip-GET card, or if a caller raises `pool_size`.

**Q5. Async native memory: unknown whether it's a leak or fragmentation, and it predates our work.**
- Python-traced memory stayed flat at 11 MB while RSS grew 85 MB, and the old SDK's async loop grew more, 93 → 514 MB vs 94 → 282 MB **(measured)**.
- It's native: aiohttp, OpenSSL or gRPC C allocations, or glibc arena fragmentation. The run doesn't tell which **(inferred)**.
- My local probe of AsyncSandbox with grpc.aio, without TLS, stayed bounded **(measured)**.
- Worth one cheap probe: the async baseline with and without `MALLOC_ARENA_MAX=2`, about 10 minutes. If the growth flattens, it's fragmentation; if not, a leak. Then file an upstream issue. It doesn't block the batch series.

**Q6. What is 6v2?**
- The redesigned rolling-acquire scenario: 40 workers, each looping acquire, one command, hold 10 s, then `release_member`, for 600 rollouts, from a warm pool of 120 replicas.
- It replaced scenario 6 (100 workers, a pool of 100, hold 5 s), which ran the pool dry in every run. In 6v2 the pool's minimum Ready stayed at 80 to 86 **(measured)**.

## 7. Second follow-up (2026-10-21)

**Refill: is it continuous, and what does one worker serialize?**
- **Refill is continuous.** The warm-pool controller watches its Sandboxes (`sandboxwarmpool_controller.go:1437-1441`). Each adoption enqueues the pool, and the reconcile creates the current deficit, `desired - total` capped at `--sandbox-warm-pool-max-batch-size` (300), with `slowStartBatch` (`:660-668`, `:713`). So 49 → 50 refills right away. There's no hold by default: `--sandbox-warm-pool-replenish-delay` and `--sandbox-warm-pool-max-refill-rate` are both 0 (`main.go:141-146`) **(verified)**.
- **`--sandbox-warm-pool-concurrent-workers=1` serializes pools, not pods.** One reconcile runs at a time across every pool (`MaxConcurrentReconciles`, `:1441`). Inside a reconcile, `slowStartBatch` creates the pool's whole deficit in parallel batches (`:1478-1493`) **(verified)**.
- **Does round-robin's earlier refill start help?**
  - Under round-robin, every pool's first adoption falls in the first quarter of the fill. Under group order, pool i starts at create 4i. The difference is at most about 3/4 of the fill time.
  - The fill takes 0.33 to 0.41 s at the unpaced rate (3s 1000/s rows, and what warm-aware pacing would give), so refill starts differ by under 0.3 s **(measured fill times)**.
  - A refill pod takes about 1.2 to 3.5 s to become Ready: 1.2 to 1.35 s first cold Ready at 50/s in 3a2 **(measured)**, and 2.5 to 3.5 s for cached-image pod start, from the controller's own comment (`:184`) **(cited)**.
  - So the difference is small next to refill time, and the coordinator's inference holds.
  - At today's 50/s the fill is 3.2 s and the difference could reach about 2.4 s, but warm-aware pacing removes that case.
  - Round-robin also queues one reconcile per adoption, so 160 serial reconciles on the single worker. Group order lets one pool's 4 adoptions merge into fewer reconciles. Refill finishes no later, and likely sooner **(inferred)**.
- **Refill doesn't affect this batch's own Ready** when the pool already holds its claims, and claims beyond a pool's ready count go through the claim controller's cold path.
- The group-order recommendation holds.

**release(): how should it check that nothing live is left?**
- Today it uses a full-object list with no resourceVersion, which is a consistent read on purpose, so a lagging watch cache can't report "none left" too early (`sandbox_batch.py:1381-1388`). It filters `deletionTimestamp` on the client **(verified)**.
- After deletecollection, the claims are still there and terminating. They were gone 20.1 s after `release()` returned **(measured, 1d)**. So this list returns all N, which is the 69.5 MB peak.
- Options:
  - **`limit=1`.** Rejected. A CRD list can't filter on `deletionTimestamp` on the server, so the one result is likely a terminating claim, and it can't show that no *live* claim is left. Waiting until the list is empty instead would make `release()` about 20 s slower **(measured claims-gone time)**. Paging until the first live claim scans all N anyway in the normal case where none are live.
  - **The batch's watch.** Rejected.
    - `release()` stops the watch before it deletes (`_stop_background_threads()`, `sandbox_batch.py:849`) **(verified)**.
    - The atexit path that shares `_delete_batch_objects` has no watch.
    - A watch is served from the watch cache, and lag there is exactly the reason the code uses a consistent read.
  - **Metadata-only list (recommended).**
    - The same list request with `Accept: application/json;as=PartialObjectMetadataList;g=meta.k8s.io;v=v1` returns just each claim's metadata, which still includes `deletionTimestamp`. The same consistency, the same `list` verb, and no RBAC change.
    - The response drops spec and status, so likely several times smaller **(inferred; not measured)**.
    - The Python custom-objects API has no flag for it, so it's sent through `api_client.call_api` with that Accept header **(inferred)**.
    - New fact versus the first review's "ruled out": that was on time, which is 0.0 to 0.1% of `release()`. 1d now shows that list costs a full list's memory.
  - **Paginated full list.** It bounds the client's memory per page, but it still moves the full objects. Keep pagination for `get_batch` attach and the 410 re-list, which need status and pod IPs.
- **Apiserver cost.** A list with no resourceVersion is a consistent read. On Kubernetes 1.31+ with `ConsistentListFromCache`, it's served from the watch cache once the cache catches up with etcd. A `limit` with no resourceVersion has historically forced the read to etcd, and newer releases are moving paginated reads to the cache too. Kind ran v1.35. **(inferred from upstream release notes I didn't re-check here; treat as unverified)**. Either way, the server work is one read of N objects. The options differ in what's sent back and parsed.
- **Skipping the deletecollection body** (`_preload_content=False`) is still worth doing, since its response is also a full list. Recommended: metadata-only re-list plus skipping that body. Step 6 of a validation run can split the two peaks.

**kwok.**
- It's the cheap way past kind's 400-pod ceiling for warm and controller scale, and it runs on your laptop with fake pods.
- GKE adds real pods, cold starts and production priority and fairness.
- Recommended: note it for later. If you want a scale number before GKE, a kwok run of our driver at warm N=1,000 to 10,000 is the cheap first step. Otherwise fold it into the GKE run.
