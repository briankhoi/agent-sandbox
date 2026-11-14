# RL batch claim mode: final report (2026-11-13)

Batch claim mode vs upstream `per_claim` (`300d05e7`, unchanged), three GKE clusters, gVisor, image streaming on. Benchmark numbers in sections 2, 4 and 5 are read from the raw result JSON and pod logs in `scratch/gke_bench_v8/runs/` (full tables: `scratch/gke_bench_v8/tables_k1.md`) and were recomputed independently by an auditor, whose corrections are applied here (`brain/4fbea1c2-e01e-40b1-9cd4-773c916a7f11/AUDIT_RL_BATCH_FINAL_REPORT.md`). Sections 3 and 7 summarise investigation reports and were not recomputed. Batch runs use `max_in_flight` 64 and async exec; eval, 3k and parity pass `create_rps` 500 and the burst and step drivers leave it at the library default. "n" is the number of runs; ranges are min–max.

## 0. Update, 2026-11-14 01:15Z: four more commits (tip `74d65bda`, image `k4-final`)

Sections 1 to 8 describe tip `9188a6e9` (image `k3-final`). Since then the branch gained the changes below, each reviewed with no blockers. RL suite 803 passed, SDK suite 1528 passed on the merged tree. Still local, not pushed.

| Commit | What | Measured |
|---|---|---|
| `aa0fe12f` | Step runs release each batch claim when its task finishes, instead of one bulk release at the end of the step | Multi-step 250×2×6, older 20-node cluster, 3 runs each: 107.6 / 107.6 / 107.6 s vs 112.4 / 113.1 / 113.1 s (4.6%). End-of-step tail 0.02–0.03 s vs 0.43–0.62 s. R2E 500×2×8: 202–208 s vs 202–222 s, a tie |
| `af804da9`, `c93592bd` | `BatchClaimConfig.max_unbound_claims`, default 256: creates wait while that many claims are created but not Ready (never more than 5 s) | Fresh cluster, c=1000, 8 bursts per arm: cold launches in 4 of 8 bursts without it (20, 30, 18, 54), 0 of 8 with it, at 201–258 vs 206–248 claims/s |
| `74d65bda` | README: how to choose concurrency when most claims start cold | 3k batch at c=500 / 250 / 150: TTFI p50 18.8 / 10.9–11.8 / 6.6–6.9 s, wall 164 / 168–187 / 170–181 s |

**Cost of `aa0fe12f`:** one claim DELETE per task, the same count per_claim sends, instead of one bulk request per step. A grouped delete would need a per-claim label in the SDK, which is a wire change and was not made.

**`k4-final` validation, all tasks succeeded in every run:**

| Shape | Cluster | `k4-final` | `k3-final` | Upstream per_claim |
|---|---|---|---|---|
| Eval 500×1 wall | older, 10 nodes | 16.2 / 15.4 s (third run in the raw files) | 16.2 s | 18.3 s |
| Eval 500×1 TTFI p50 | older, 10 nodes | 1.65 / 1.86 s | 1.82 s | 3.34 s |
| 1k burst c=1000 claims/s | older, 10 nodes | 207–240, 0 cold launches in 12 bursts | 233–253 (earlier) | 106–119 (earlier) |
| 3k naive / sliding / pipelined wall | older, 10 nodes | 167 / 163 / 155 s | 156 / 150 / 168 s | 393 / 398 / 420 s (earlier) |
| R2E 500×2×8 wall | older, 20 nodes | 237 / 207 s | 202–222 s (earlier) | 1,004–1,027 s (earlier) |
| Multi-step 250×2×6 wall | older, 20 nodes | 110.0 / 108.8 s | 112.4–113.1 s (earlier) | 226–270 s (earlier) |
| Tunix 64×16×6 wall | older, 20 nodes | 160 s | 173 s (previous image) | 513 / 515 s (earlier) |
| 500×2 naive / sliding / pipelined | older, 20 nodes | 31.3 / 43.1 / 40.0 s | 31.9 / 41.7 / 43.0 s | 41.9–42.6 s sliding and pipelined (earlier) |

- The 3k differences between `k4-final` and `k3-final` are inside the earlier run-to-run range (149–192 s), one run each.
- The 237 s R2E run started right after another test left 1,000 pool pods on the cluster; the second run was 207 s.

**Cold launches: cause found** (replaces the "cause unknown" statements in sections 5 and 6). Two controller behaviours combine. A stale second reconcile of an already-bound claim takes another Ready sandbox out of the queue and returns it only after a conflict and a re-read, so at the end of an exact-fit burst the last Ready sandboxes are unavailable. Claims arriving then either cold-start at once or retry only until they are 2 s old. Measured: cold claims are always the last-created (ranks 957–999 of 1,000) and each cold burst leaves at least as many original Ready sandboxes unclaimed. Raising the controller's `--sandbox-claim-warm-candidate-grace-period` to 15 s also gave 0 of 8 bursts. The client window does not remove the race: it keeps the last claims young enough to take a refill sandbox. Evidence is 8 bursts per arm on one cluster. Report: `brain/a4cd77c4-c7eb-4fe1-a033-3a148909780c/cold-launch-cause-report.md`.

**Ties and losses investigated** (replaces "no cause identified" in section 2):

- **3k TTFI p50** is a queueing effect: both modes keep nearly all 500 slots waiting on a claim. Batch serves evenly (mean 19.9 s); per_claim is skewed (median 6.3 s, mean 57.3 s). Lower concurrency cuts batch TTFI in proportion (table above) for 3–14% more wall time; that cost is not settled.
- **Per-task claim time** (1.25 vs 0.50 s) compared different spans. On the same clock batch takes 1.2–1.6 s and per_claim 2.8–2.9 s; per_claim's worker threads starve its event loop.
- **Sliding and pipelined 500×2:** the window cycle is fixed at about 8.4 s by pod starts however the client splits it. Not client-fixable.
- **Bulk release throughput:** over 90% of the time is inside one deletecollection and one confirming list. Parallel deletes did not help. Not client-fixable.

**Fresh cluster: not yet re-measured for these commits.** The first fresh cluster was deleted; its replacement degraded under an R2E warm-up with empty image caches (kill timeouts, then pod IP exhaustion), with upstream per_claim alone as well as batch. Its nodes were recycled and images are being pre-pulled before the comparison is rerun.

## 1. PR candidate

Branch `feat/c5-batch-rebased-300d05e7`, tip **`9188a6e9`**, local only (not pushed). Benchmarked as image `k3-final` (= `80c6ae71`; the tip adds a README-only commit). RL suite 796 passed, SDK suite 1528 passed (SDK suite last run at `3faf09ca`; the later commits touch only RL code and docs).

Commits on top of `5f87abe9` (the six earlier commits made since the first `/goal`, `b386d881` to `5f87abe9`, are in Appendix A with every experiment tried):

| Commit | What | Review |
|---|---|---|
| `feffbac2`, `03390885`, `f85a5072` | Skip aiohttp proxy/netrc lookups when none is configured | earlier rounds |
| `4f6492c6` | Docs: measured basis for the defaults (`create_rps` 500, `max_in_flight` 64) | – |
| `2eb5ef6d` | Delete a released batch claim's Sandbox and pod directly | no blockers |
| `3a14c1dc` | Cap those deletes at `max_in_flight` (ceiling 16) | no blockers |
| `d1633ac7` | Async exec: 10 s connect timeout, up to 3 attempts when the connection was never established | no blockers |
| `758ba63a`, `aefae49e`, `be4c2577` | Group released-sandbox deletes by claim UID label (2 requests per 50 releases); flusher fixes | no blockers |
| `a8f6fc60` | SDK: stop keeping every released batch claim by name (about 90 B per release before) | no blockers |
| `3faf09ca` | No bulk release while an acquire is in flight (narrows the race, does not close it) | no blockers |
| `80c6ae71` | Label probe decides when the first released Sandbox is already gone | no blockers; confirmed on kind |
| `9188a6e9` | README: where the deletes help, and the `list` permission | docs |

## 2. Results on the final SHA image (`k3-final`) vs upstream per_claim

All batch tasks succeeded in the runs in this table. Exceptions and failures:

- The per_claim 1k burst had 3 exec errors (empty output) at c=1000.
- A second batch 1k burst run on the final image (`k3_burst_1x1000_batch_r2`) failed in setup: the warm pool stayed at 992 of 1,000 ready and timed out after 601 s. It produced no result and is not in the table.
- On the previous image one batch 500×2 naive run finished 999 of 1,000 (empty exec output).
- Several ratios rest on single per_claim runs (multi-step, eval, 3k); treat them as indicative.

| Shape | Cluster | Batch | Upstream per_claim | Ratio |
|---|---|---|---|---|
| **R2E 500×2×8** wall | older, 20 nodes | 207 s (203–220, n=3) | 1,015 s (1,004–1,027, n=2) | 4.9× |
| **R2E 500×2×8** wall | fresh, 10 nodes | 253 s (n=1); previous image 239 s (229–249, n=2) | 239 s (237–240, n=2, run alongside the previous image) | 0.94× (final image, unpaired); 1.00× (previous image) |
| Multi-step 250×2×6 wall | older, 20 nodes | 112 s (n=1) | 226 s (n=1) | 2.0× |
| **SWE-bench eval 500×1** wall | older, 10 nodes | 16.3 s (16.0–16.6, n=2) | 18.7 s (n=1) | 1.15× |
| Eval 500×1 TTFI p50 | older, 10 nodes | 2.09 s | 3.40 s | 1.6× |
| **SWE-bench 3k (500×6)** naive wall | older, 10 nodes | 160 s (158–162, n=2) | 393 s (n=1) | 2.5× |
| 3k sliding wall | older, 10 nodes | 166 s (157–175, n=2) | 398 s (n=1) | 2.4× |
| 3k pipelined wall | older, 10 nodes | 180 s (169–192, n=2) | 420 s (n=1) | 2.3× |
| **1k warm burst** c=1000, claims/s | older, 10 nodes | 233 (233–253, n=3) | 107 (106–119, n=3) | 2.2× |
| 1k burst c=1000, TTFI p50 | older, 10 nodes | 2.69 s | 10.94 s | 4.1× |
| 1k burst c=1000, burst+exec wall | older, 10 nodes | 5.04 s | 12.48 s | 2.5× |
| 1k burst c=500, claims/s | older, 10 nodes | 260 (240–281, n=3) | 92 (88–94, n=3) | 2.8× |
| Soak, 20,000 claims in waves of 200 | older, 10 nodes | 20,000 / 20,000, 0 errors | not rerun | – |

Same shapes on the previous image (`k1-final` = `aefae49e`, which differs from the final SHA only by the SDK memory fix, the bulk-release guard, the label probe and a test-only commit) agree: R2E older 208 s (n=2) vs 1,000 s; eval 15.6 s (n=3) vs 18.8 s (n=3), TTFI p50 1.81 vs 3.41 s; 3k naive 150 vs 392 s; burst c=1000 234 vs 108 claims/s. On the fresh cluster the 1k burst gives 230 vs 117 claims/s at c=1000 (TTFI p50 8.6 vs 17.2 s; p99 21.2 vs 22.2 s, ranges overlap).

Where batch is worse or equal:

- **Fresh-cluster R2E is a tie on the previous image and 6% slower on the single final-image run**, which had no per_claim run beside it. The scheduler logged `Insufficient memory` events there during both arms' runs (from the cluster's event stream, not the result files). Time until all environments are ready is the same in both arms (153–168 s batch vs 163 s per_claim); batch adds 5–7 s per run between the last task and the end of each step, against 1.5–1.7 s (Appendix D). One batch run on an earlier image had an 86 s step (one pool waited 56 s).
- **3k TTFI p50 is worse for batch** (18.6–22.5 s vs 6–14 s) while p99 is far better (40.7–62.4 s vs 229–357 s). Per_claim serves most tasks quickly and then stalls; batch waits evenly on pool refill.
- **Bulk release throughput in the burst** is lower for batch on the older 10-node cluster with the final image (about 750 vs 1,000–1,100 sandboxes/s at c=500 and c=1000, ranges overlap). On the fresh cluster batch was faster (1,520 vs 892 at c=1000). No cause identified.
- **Sliding and pipelined 500×2**: batch 41.7 / 43.0 s. Per_claim was not rerun on this image; its runs earlier today were 41.9–42.6 s. Both are bound by pod start rate.
- **Fresh-cluster eval 500×1** (batch 99–149 s on the previous image and 36 s on the final one, vs per_claim 219–239 s) is dominated by image pulls and should not be quoted.

## 3. Why the long runs differ by cluster

- On the two older clusters the garbage collector runs at about 40 API requests/s and spends 3 per object, so it removes about 13 objects/s. On the fresh cluster it runs at 100–209 requests/s. The 40/s is flat with no server-side queueing, which looks like a client-side limit; the setting itself cannot be read on GKE, so that part is inference. The older control planes are also smaller (324 vs 483 priority-and-fairness seats).
- Released Sandboxes and pods therefore pile up on the older clusters, the scheduler reports `Insufficient memory`, and warm pools stall. Deleting them directly removes the backlog. Before direct delete, batch and per_claim were level on these shapes on the older clusters (for example R2E 984–995 vs 993–1,010 s), so the 2–4.9× wall gains come from it; the collector rate as the underlying reason is the inference above.
- The claim-path gains (burst claims/s, TTFI, eval wall, threads and sockets) appear on all three clusters.
- Upstream per_claim is itself 4.5–6× faster than the per_claim of the Campaign 4 era, so older slide ratios compared against a slower baseline.
- Investigation report: `brain/4e58b8e7-cc70-44a6-8018-5f27ad616c04/gc_slow_cascade_report.md`.

## 4. Experiments today

| Question | Result | Decision |
|---|---|---|
| Per-task release instead of end-of-step bulk release (multi-step 250×2×6, same image) | 108.0 / 107.7 s vs 112.6 / 111.8 s: bulk costs about 0.7 s per step | Keep bulk (1 request instead of 500 per step); documented trade-off |
| Grouped deletes vs per-object deletes | Eval first exec avg 0.48–0.60 s on the three grouped-delete images, against 0.56–0.80 s on the per-object-delete images | Kept |
| `max_in_flight` 64 / 128 / 256 and `create_rps` 50 / 500 / 2000 sweeps | No wall difference | Defaults stay 64 / 500 |
| Background bulk release | Not built: it hits the acquire race the new guard prevents | Rejected |
| Client-side cold-launch mitigation (taper, replace a cold claim) | Designed, not built | Controller fix preferred |
| per_claim on the patched controller (regression check, c=1000, 3 bursts) | 95–114 claims/s, against 115–125 on the current controller earlier today; 0 errors | Small sample; possible slowdown not ruled out |
| per_claim with the same direct delete (reference only) | Multi-step 175 s vs batch 198 s, one run each, on an earlier image | Not a product arm; per_claim stays upstream |
| Create concurrency vs cold launches (`max_in_flight` 16 / 8) | 1 of 6 bursts cold at 16, 0 of 6 at 8 | Too few bursts to conclude |
| Kind end-to-end check of direct delete | No correctness bug: ordering, no replacement Sandboxes, RBAC fallbacks, double release, deleted Lease | Found the label-probe gap, fixed in `80c6ae71` |
| Controller patch for cold launches (fresh cluster, per-object-delete image, c=1000) | Current controller: cold launches in 2 of 8 bursts (38, 25). Patched controller: 5 of 12 bursts (33, 42, 3, 5, 25); its wait fired only 15 times in the whole run | Patch does not fix it; controller rolled back |

Rejected earlier in this campaign: exec on extra event loops, a hand-written WebSocket client, TLS session resumption, restricted TLS groups, concurrent per-claim deletes before deletecollection, a separate delete slot budget, cancelling refill at 1 s, prewarming more than 64 connections, a warm-pool watch. Appendix A lists every change and experiment since the first `/goal`, rebuilt from the conversation log.

## 5. Cold launches on an exact-size pool

With 1,000 claims against a pool of exactly 1,000, some bursts at c=1000 cold-start a few dozen claims even though enough warm sandboxes exist. Per_claim showed none in any burst measured, though it did cold-start in step runs (below).

| Image | Cluster | Bursts at c=1000 with cold launches | Counts |
|---|---|---|---|
| per-object deletes (`i1`) | fresh | 5 of 8 | 42, 42, 28, 1, 56 |
| grouped deletes (`j2`) | fresh | 1 of 20 (`max_in_flight` 64: 0 of 8; 16: 1 of 6; 8: 0 of 6) | 30 |
| `k1-final` | fresh / older | 0 of 3 / 1 of 3 | 1 |
| `k3-final` | older | 0 of 3, plus 0 of 2 in the run that later failed | – |

- No cold launches at c=500. In step runs they are rare for both modes: batch had 1 in one fresh-cluster R2E step and 3 in one Tunix step; per_claim had 2 and 1 in R2E steps.
- The samples are too small to read a trend across images.
- A controller-side race reproduces in a unit test: an already-bound claim's second reconcile briefly holds a second warm candidate, and another claim then finds the queue empty and cold-starts. A 32-line patch for it (`brain/81aa0e51-9eaa-4599-ba24-3a27a13cf9a6/controller-wait-for-held-candidates.NOT-APPLIED.patch`) was tested on the fresh cluster and did not reduce cold launches (2 of 8 bursts before, 5 of 12 with it), so that race is not the main cause on GKE.
- **The cause is still unknown.** The counts cluster (42, 42, 28, 56, 38, 25, 25, 33, 30), which suggests whole groups of claims rather than a random race; that lead was not followed up.

## 6. Known limits and follow-ups

- **Cold launches** as above; cause unknown, and the one controller patch tried did not help.
- **Empty exec output without an error:** at least 5 tasks in over 300,000, in both modes (1 batch task and 3 per_claim tasks today, 1 on the Campaign 4 image). The exec code does not read the status channel in either the sync or async path, so a stream closed early returns an empty string. Not changed.
- **Bulk-release race** is narrowed, not closed; closing it needs the SDK to expose in-flight acquires.
- **Wrong label decision:** if the probe ever decided "labelled" on a controller that does not label Sandboxes, pods would be deleted while their Sandboxes live and be recreated. The reviewer proposed skipping pod deletes when the Sandbox deletecollection matches nothing; on a fast-collector cluster that would skip useful work, so it was not adopted.
- **Extra RBAC:** direct delete needs `delete` and `deletecollection` on `sandboxes` and `pods`, and `list` on `sandboxes` for the probe fallback. Without them it turns itself off or stays on by-name deletes. Pods are force-deleted (grace 0).
- **Up to 16 extra concurrent API requests** beside `max_in_flight`.
- **Variance:** older-cluster batch R2E 203–220 s; Tunix-shaped run 173 s today against 179 / 273 s earlier.
- **Not retested:** the five-cluster MC5 claim and the 4k burst.

## 7. Slide and resume claims

| Claim | Verdict |
|---|---|
| R2E 3.81× | Real single run from Nov 9, not reproducible; today 4.9× on the older 20-node cluster and about 1.0× on a fresh one, against a faster per_claim |
| 9.65× env-ready p50 | Mean of per-step p50s, not a p50 |
| Sockets 4.9× | Holds (120–123 vs 507–605) |
| MC5 about 5× TTFC, 1.83× wall | Best supported (n=3, interleaved); the "512 images × 8" label is wrong; not retested |
| 4k burst 3.63× TTFI | That is first-sandbox time; p50 is 2.28× |
| API calls 5× | Never measured |
| Sockets 11× | Only at `max_in_flight` 20; today 12–14× at c=1000 |
| Cleanup 32.5× | Not like-for-like; about 1.1× |
| 3k 4.6× | Today 2.3–2.5× on the older 10-node cluster |
| 1k bursts 2.8× | Was against a blog number; today 2.2–2.8× claims/s, 2.2–4.1× TTFI p50 |

Details: `HEADLINE_CLAIMS_FORENSIC_AUDIT.md` in this directory.

## 8. PR description draft

**Title:** rl: batch claim mode cleanup, exec reconnect and released-sandbox deletes

**Body:** A fleet in batch claim mode released claims faster than the cluster removed their Sandboxes and pods. On two of three GKE clusters measured, the garbage collector removed about 13 objects a second, so finished pods held node memory and warm pools took minutes to refill. The fleet now deletes a released claim's Sandbox and pod itself, after the claim is gone, grouped by the claim UID label where the controller sets it. This needs `delete`/`deletecollection` on `sandboxes` and `pods`; without them it turns itself off and nothing else changes. `per_claim` mode is untouched.

Also: async exec retries a connection that was never established instead of waiting 30 s; the SDK no longer keeps every released batch claim by name; `release_all` no longer bulk-releases a batch while an acquire is in flight.

Measured against upstream per_claim: R2E 500×2×8 207 s vs 1,015 s on a cluster with a slow collector and level with per_claim on one without; 1,000-claim burst 233 vs 107 claims/s.

## Appendix A. Every change and experiment since the first `/goal`

Rebuilt by the auditor from the conversation log (`Step` is the log step). Effects are as stated at the time; figures from before step 11510 come from an earlier phase whose claims were partly contradicted by the audit at that step.

### A1. Code changes kept (in the branch at tip 9188a6e9)

| # | Change | Commit | Effect as stated at the time | Step |
|---|---|---|---|---|
| 1 | Event-loop-native `AsyncBatchClaims`; raw JSON watch/POST/DELETE; one LIST for warm-credit refresh; wave-end deletecollection | `f7b27235` (pre-rebase), squashed into `b386d881` | Old-model claims: 180 vs 95 claims/s (500x2 c=200); c=1000 TTFI p50 10.75 -> 3.40 s. Later audit (step 11510) called several Campaign 5 claims unsupported | 9651; rebase 10442 |
| 2 | `handle.aexec()` over a shared WebSocket client; cached SSLContext | `a98925bb` (pre-rebase), in `b386d881` | Old-model claim: 218.8 vs 90.0 claims/s at c=500; 23 vs 2,587 threads | 9651 |
| 3 | Two paginated LISTs for pool/template precheck; RL defaults set (first `create_rps=200`, `max_in_flight=256`) | `844550a9` (pre-rebase), in `b386d881` | No isolated figure seen | 10067; tracker section 2 |
| 4 | Async lifecycle hardening after rebase (`max_in_flight=20`, no prewarm at this commit) | `671e4cac` | Step 11510: this commit had the best eval wall, p99 and sockets on single reps | 10671, 11510 |
| 5 | Keep-alive 15 -> 75 s; prewarm 64 TLS connections; windowed release fix; 403 fallback from LIST to GET; defaults `create_rps=500`, `max_in_flight=768` | `93f93fc9` | No isolated figure seen | 10812 |
| 6 | Cross-loop cleanup, pending-acquire release gating; `max_in_flight=512` | `2f87f7a9` | 512 was worse on eval: TTFI first 0.44 -> 1.65 s, p50 1.95 -> 2.52 s | 11086, 11109 |
| 7 | Socket cleanup fixes; defaults settled at `max_in_flight=64`, `create_rps=500` | `50e4531b` | Eval 500x1 16.98 s, TTFI p50 2.16 s | 11259 |
| 8 | Pipelined: seed warm credit at window start; foreground creates ahead of prefetch | `5f87abe9` | Pipelined 37.65 s vs 40.49 s per_claim (parity suite) | 11378, 11502 |
| 9 | Skip aiohttp per-request proxy/netrc lookups | `feffbac2`, `03390885` | Threads 23 -> 3; CPU -15-25%; burst wall about 5-10% better with overlapping ranges; TTFI not improved. Local microbench 5.29 -> 2.48 ms per exec | 11640, 11685, 11702, 11718, 11822 |
| 10 | Docs: measured basis for defaults | `4f6492c6` | Docs only | 11970 |
| 11 | Direct delete of a released claim's Sandbox and pod | `2eb5ef6d`, `f85a5072` | Multi-step 250x2x6 on c4-1: 198 / 194 s (was 394-405; per_claim 368-381). 3k: 317 / 222 / 189 s vs per_claim 392-419 | 12130, 12148, 12195 |
| 12 | Cap those deletes at `max_in_flight` (ceiling 16) | `3a14c1dc` | No change at 64 | 12335 |
| 13 | Async exec: 10 s connect timeout, up to 3 attempts | `d1633ac7` | Targets 4 `ConnectionTimeoutError` failures in about 270,000 tasks; "not yet validated on a cluster" when committed | 12421 |
| 14 | Grouped deletes by claim UID label | `758ba63a` | Motivated by direct-delete regressions: eval first-exec p50 0.52 -> 0.62 s, pipelined prefetch 10-13 -> 35-41 s, pipelined wall 39.2 -> 42.8 s. First validation: R2E fresh 243 s | 12489, 12527 |
| 15 | Flusher recovery, parallel groups, no re-send; test fix | `aefae49e`, `be4c2577` | Correctness | 12549, 12569 |
| 16 | SDK: prune released batch claims | `a8f6fc60` (subagent's `673c9288`) | About 90 MB per million releases before | 12623; final hash only in git/tracker |
| 17 | No bulk release while an acquire is in flight | `3faf09ca` (subagent's `3facb0cc`) | Narrows the race, does not close it | 12623, 12639 |
| 18 | Label probe decides on fast-GC clusters | `80c6ae71` | One GET plus one LIST per run instead of a GET per flush (kind) | 12655, 12691; hash only in git/tracker |
| 19 | README correction | `9188a6e9` | Docs | 12689 |

### A2. Tried and rejected, reverted, or left unchanged

| # | Idea / experiment | Runs | Result as stated | Step |
|---|---|---|---|---|
| R1 | `max_in_flight` 256, then 768, then 512 as RL default | C5 | 512 worse on eval (see A1 #6); settled on 64 | 10067, 10812, 11086, 11259 |
| R2 | Exec on background event loops (0 vs 4 loops) | image `x1-execpool` (step 11577) | Two A/B pairs, first-exec 0.54-0.78 s without vs 0.67-0.80 s with; removed | 11583, 11640, 11671 |
| R3 | Hand-written WebSocket exec client | local | 2.77 ms vs 2.48 ms per exec; dropped | 11685 |
| R4 | TLS session resumption | local | 2.41 -> 2.10-2.18 ms (10-13%); parked, then not adopted | 11652, 11693, 11897 |
| R5 | Restricting TLS key-exchange group | local | Faster (about 0.83 ms) but weakens TLS; rejected | 11652 |
| R6 | Prewarm follows `max_in_flight` up to 256 | `e3*` | No measured benefit; reverted | 11671, 11822 |
| R7 | `max_in_flight` sweep 64 / 128 / 256 | `e3t0_*`, `e3c1_*` | t0 wall 30.97 / 31.44 / 31.28 s; no effect, keep 64 | 11787, 11822 |
| R8 | `create_rps` sweep 50 / 500 / 2000 | `e4t0_*` | Wall flat 33.3-33.5 s; TTFI p99 9.9 / 8.6 / 8.2 s; keep 500 | 11845 |
| R9 | Warm-pool watch for readiness | none | Dropped after user objection (step 11725) | 11724, 11726 |
| R10 | Shorter warm-credit refresh interval; narrower LIST | none found | Noted as ideas at 11724. Outcome UNVERIFIED | 11724 |
| R11 | Bounded concurrent per-claim deletes (64) before deletecollection | `x4_burst_*` | 797-983 vs 854-1,004 sandboxes/s; rejected | 11928 |
| R12 | Separate delete slot budget (64) | `x4_core_*` | Naive 31.00 / 31.00 vs 30.71 / 30.71 s; rejected | 11944 |
| R13 | Sliding timeline; pad batch tasks by 1 s | `d3_*`, `x5_*` | Wall unchanged 42.86 vs 42.35 s; cycle about 8 s per window in both modes | 11897, 11944 |
| R14 | Cancel warm-pool refill 1 s after claims start | `x5_*` | 41.64 / 39.81 vs 42.35 / 42.11 s; inside noise; dropped. Step 12223: "I don't consider that settled" | 11944, 11970, 12223 |
| R15 | Per-task release instead of end-of-step bulk release | `x9*` | 108.0 / 107.7 vs 112.6 / 111.8 s; bulk kept | 12714 |
| R16 | Run bulk release in the background | none | Rejected: hits the acquire race | 12714 |
| R17 | Client-side cold-launch mitigation (slow creates near end of pool credit; warm slack) | none | Options listed, none implemented; workstream ended with no client change | 12489, 12619 |
| R18 | Controller patch for cold launches | c6 | File only, not applied; A/B started at 22:3xZ, no result yet | 12619, 12731 |
| R19 | Reviewer's optional hardening: skip pod deletes when Sandbox deletecollection matches nothing | kind | Not adopted | 12691 |
| R20 | Read exec status channel (empty-output failures) | none | Not changed | 12714 |
| R21 | Give per_claim the same direct delete | `x6_*` | Experiment only; user decided per_claim stays identical to upstream (step 12149) | 12130, 12148 |
| R22 | Suspend/resume (Pod Snapshot) evaluation | UNVERIFIED | Planned at 9907 / 10173; Campaign 5 table found contradicted at 11510. I found no later measured result | 9907, 11510 |

### A3. Diagnostics and benchmark rounds (no code decision by themselves)

| Item | Runs | Result as stated | Step |
|---|---|---|---|
| Loop-lag / CPU / profile diagnostics | `diag1*` | Loop blocked about half the burst at c=1000; claim rate 196-322/s without exec | 11557, 11583, 11640 |
| Controller metrics sampler | read-only | 500 of 500 workers busy, 2,000-2,500 patches/s: claim rate is server-bound | 11741 |
| trust_env A/B | `e2t0_*`, `e2c1_*` | See A1 #9 | 11702, 11718 |
| F1 baseline at `feffbac2` | `f1_*` | Eval 15.59 vs 18.64 s; TTFI p50 1.69 vs 3.41 s | 11845 |
| Two-client test | `d4_*` (failed), `d5_*`, `d6_*` | 114+112 and 122+96 claims/s vs 197/s for one client | 11970, 12095 |
| Slide S1 re-run before direct delete | `s1t0_*`, `s1c1_*` | 984-995 vs 993-1,010 s, about 1.02x; c4-1 runs failed in setup | 12095 |
| per_claim + direct delete reference | `x6_*` | Multi-step 175 s vs batch 198 s (one run each); 3k 185 / 164 / 169 vs batch 317 / 222 / 189 on the pre-review image | 12130; tracker 13:06Z |
| Original Campaign 4 image re-run | t0, c6 | t0 batch 1,009.5 s (tracker); c6 per_claim 1,576 / 1,129 s (tracker) | 12148; tracker |
| Fresh cluster `asrl-c4-6` | `g3*` on c6 | R2E 236, 243 vs 236, 250, 252 s; multi-step 99, 102 vs 108, 108 s | 12376 |
| Final suite on `f85a5072` | `h1_*`, `g3_*`, `g3t0_*` | Tunix 179, 273 vs 513, 515 s; 3k 148 / 163 / 181 vs 393 / 394 / 417 s | 12397 |
| Age probe | `c6age_*` | One point, 241 s at about +5 h; stopped | 12376, 12549 |
| Cold-launch counter | `i1_*` | 42 of 1,000 in 1 of 4 bursts; then 28, 1, 0, 56; per_claim 0 in 8 | 12489, 12513 |
| Grouped-delete validation | `j1_*`, `j1c6_*` | R2E fresh 243 s, 0 cold in 8 steps | 12527 |
| Create concurrency 16 / 8 vs cold launches | `x7c6_*` (tracker) | At 16: cold counts 0, 0, 0, 0, 30, 0. At 8: all six 0. Image `j2-uidbatch`. Seen in a task output, not in a planner message | 12656 |
| Final rounds | `k1_*`, `k1c6_*`, `k3_*` | As in report section 2 | 12617, 12714, 12731 |
| GC investigation | read-only | Old clusters about 40 req/s, 13.3 objects/s; fresh 100-209 req/s | 12689 |
| Kind end-to-end check | kind | No correctness bug; found label-probe gap | 12655 |

Prefix `g2`: `plan_g2_c1.txt` produced runs named `g3_*` (tracker 13:02Z). I found no experiment named `x8`; the `x8`/`g1_`/`g2_` hits before step 11511 are unrelated strings.

## Appendix B. Recipes

Notes that apply to every table below:

- The `commit_sha` column is the label the plan passed to the launcher (`PLAN_SHA`), not the image commit. Per_claim runs carry the batch round's label. The image is fixed by tag and digest.
- Image commits from `IMAGE_DIGESTS.txt`: `k3-final` = `80c6ae71`, `k1-final` = `aefae49e`, `j2-300d05e7` and `f1-300d05e7` = upstream `300d05e7` (`j2` adds cold-launch counters to the burst and step drivers).
- Registry path for all driver images: `us-central1-docker.pkg.dev/<PROJECT>/rl-gke-bench/asrl-driver@<digest>`.
- Settle is the third field of the plan line (seconds slept after the namespace is confirmed empty).
- Burst and step commands do not pass `--batch-create-rps`; their result JSON records `batch_create_rps` 0.0. The eval, 3k and parity commands pass 500.
- Clusters: `asrl-c4-1` and `asrl-c4-6` use node pool `gvisor-pool`; `asrl-c4-t0` uses `gvisor-n2-ssd`. All three pools are n2-standard-32 (sources in B.10).

### B.1 R2E 500×2×8

| Arm | Runs | Cluster | Node pool | Image tag | Digest | `commit_sha` label in meta | Settle |
|---|---|---|---|---|---|---|---|
| batch | `k3_r2e500x2x8_batch_r1`, `k3_r2e500x2x8_batch_r2`, `k3_r2e500x2x8_batch_r3` | asrl-c4-t0 | gvisor-n2-ssd | `k3-final` | `sha256:f7390a93d7c7a78557241efa58eb75c4cb0e0d3b4b10544a96de7f5fd624d7e1` | `80c6ae71` | 25 s |
| batch | `k3c6_r2e500x2x8_batch_r1` | asrl-c4-6 | gvisor-pool | `k3-final` | `sha256:f7390a93d7c7a78557241efa58eb75c4cb0e0d3b4b10544a96de7f5fd624d7e1` | `80c6ae71` | 25 s |
| batch | `k1_r2e500x2x8_batch_r1`, `k1_r2e500x2x8_batch_r2` | asrl-c4-t0 | gvisor-n2-ssd | `k1-final` | `sha256:13e37ac0637117684e46d9c8756c9461718e863029f19361f910370aecaa4b58` | `aefae49e` | 25 s |
| batch | `k1c6_r2e500x2x8_batch_r1`, `k1c6_r2e500x2x8_batch_r2` | asrl-c4-6 | gvisor-pool | `k1-final` | `sha256:13e37ac0637117684e46d9c8756c9461718e863029f19361f910370aecaa4b58` | `aefae49e` | 25 s |
| per_claim | `k3_r2e500x2x8_pc_up_r1`, `k3_r2e500x2x8_pc_up_r2` | asrl-c4-t0 | gvisor-n2-ssd | `j2-300d05e7` | `sha256:70f7e94d365f1aabc3b29b2be6d4c7d38816b982d977eea73c6a50e4261b2898` | `80c6ae71` | 25 s |
| per_claim | `k1_r2e500x2x8_pc_up_r1` | asrl-c4-t0 | gvisor-n2-ssd | `j2-300d05e7` | `sha256:70f7e94d365f1aabc3b29b2be6d4c7d38816b982d977eea73c6a50e4261b2898` | `aefae49e` | 25 s |
| per_claim | `k1c6_r2e500x2x8_pc_up_r1`, `k1c6_r2e500x2x8_pc_up_r2` | asrl-c4-6 | gvisor-pool | `j2-300d05e7` | `sha256:70f7e94d365f1aabc3b29b2be6d4c7d38816b982d977eea73c6a50e4261b2898` | `aefae49e` | 25 s |

Batch command (prefixes k1, k3):

```
python run_step_bench.py --in-cluster --namespace agent-sandbox-rl --node-selector cloud.google.com/gke-nodepool=gvisor-n2-ssd --runtime-class gvisor --images-file /app/drivers/images/r2e_subset_500.txt --problems 500 --rollouts 2 --steps 8 --concurrency 500 --strategies naive --warm-per-task --max-warmpool-size 2 --enable-cow --unmanaged-network-policy --termination-grace-seconds 0 --warm-create-budget 500 --probe 'cd /testbed && git status >/dev/null && git log --oneline -5 >/dev/null && sleep 5' --claim-mode batch --async-exec --batch-max-in-flight 64
```

Batch command (prefixes k1c6, k3c6):

```
python run_step_bench.py --in-cluster --namespace agent-sandbox-rl --node-selector cloud.google.com/gke-nodepool=gvisor-pool --runtime-class gvisor --images-file /app/drivers/images/r2e_subset_500.txt --problems 500 --rollouts 2 --steps 8 --concurrency 500 --strategies naive --warm-per-task --max-warmpool-size 2 --enable-cow --unmanaged-network-policy --termination-grace-seconds 0 --warm-create-budget 500 --probe 'cd /testbed && git status >/dev/null && git log --oneline -5 >/dev/null && sleep 5' --claim-mode batch --async-exec --batch-max-in-flight 64
```

Per_claim command (prefixes k1, k3):

```
python run_step_bench.py --in-cluster --namespace agent-sandbox-rl --node-selector cloud.google.com/gke-nodepool=gvisor-n2-ssd --runtime-class gvisor --images-file /app/drivers/images/r2e_subset_500.txt --problems 500 --rollouts 2 --steps 8 --concurrency 500 --strategies naive --warm-per-task --max-warmpool-size 2 --enable-cow --unmanaged-network-policy --termination-grace-seconds 0 --warm-create-budget 500 --probe 'cd /testbed && git status >/dev/null && git log --oneline -5 >/dev/null && sleep 5' --claim-mode per_claim
```

Per_claim command (prefixes k1c6):

```
python run_step_bench.py --in-cluster --namespace agent-sandbox-rl --node-selector cloud.google.com/gke-nodepool=gvisor-pool --runtime-class gvisor --images-file /app/drivers/images/r2e_subset_500.txt --problems 500 --rollouts 2 --steps 8 --concurrency 500 --strategies naive --warm-per-task --max-warmpool-size 2 --enable-cow --unmanaged-network-policy --termination-grace-seconds 0 --warm-create-budget 500 --probe 'cd /testbed && git status >/dev/null && git log --oneline -5 >/dev/null && sleep 5' --claim-mode per_claim
```


### B.2 Multi-step 250×2×6

| Arm | Runs | Cluster | Node pool | Image tag | Digest | `commit_sha` label in meta | Settle |
|---|---|---|---|---|---|---|---|
| batch | `k3_stept0_250x2x6_batch_r1` | asrl-c4-t0 | gvisor-n2-ssd | `k3-final` | `sha256:f7390a93d7c7a78557241efa58eb75c4cb0e0d3b4b10544a96de7f5fd624d7e1` | `80c6ae71` | 25 s |
| per_claim | `k3_stept0_250x2x6_pc_up_r1` | asrl-c4-t0 | gvisor-n2-ssd | `j2-300d05e7` | `sha256:70f7e94d365f1aabc3b29b2be6d4c7d38816b982d977eea73c6a50e4261b2898` | `80c6ae71` | 25 s |

Batch command (prefixes k3):

```
python run_step_bench.py --in-cluster --namespace agent-sandbox-rl --node-selector cloud.google.com/gke-nodepool=gvisor-n2-ssd --runtime-class gvisor --images-file /app/drivers/images/swebench500.txt --problems 250 --rollouts 2 --steps 6 --concurrency 500 --strategies naive --warm-per-task --max-warmpool-size 2 --enable-cow --overcommit-factor 10 --unmanaged-network-policy --termination-grace-seconds 0 --probe 'sleep 10' --claim-mode batch --async-exec --batch-max-in-flight 64
```

Per_claim command (prefixes k3):

```
python run_step_bench.py --in-cluster --namespace agent-sandbox-rl --node-selector cloud.google.com/gke-nodepool=gvisor-n2-ssd --runtime-class gvisor --images-file /app/drivers/images/swebench500.txt --problems 250 --rollouts 2 --steps 6 --concurrency 500 --strategies naive --warm-per-task --max-warmpool-size 2 --enable-cow --overcommit-factor 10 --unmanaged-network-policy --termination-grace-seconds 0 --probe 'sleep 10' --claim-mode per_claim
```


### B.3 Eval 500×1

| Arm | Runs | Cluster | Node pool | Image tag | Digest | `commit_sha` label in meta | Settle |
|---|---|---|---|---|---|---|---|
| batch | `k3_eval500_batch_r1`, `k3_eval500_batch_r2` | asrl-c4-1 | gvisor-pool | `k3-final` | `sha256:f7390a93d7c7a78557241efa58eb75c4cb0e0d3b4b10544a96de7f5fd624d7e1` | `80c6ae71` | 20 s |
| batch | `k3c6_eval500_batch_r1` | asrl-c4-6 | gvisor-pool | `k3-final` | `sha256:f7390a93d7c7a78557241efa58eb75c4cb0e0d3b4b10544a96de7f5fd624d7e1` | `80c6ae71` | 20 s |
| batch | `k1_eval500_batch_r1`, `k1_eval500_batch_r2`, `k1_eval500_batch_r3` | asrl-c4-1 | gvisor-pool | `k1-final` | `sha256:13e37ac0637117684e46d9c8756c9461718e863029f19361f910370aecaa4b58` | `aefae49e` | 20 s |
| batch | `k1c6_eval500_batch_r1`, `k1c6_eval500_batch_r2` | asrl-c4-6 | gvisor-pool | `k1-final` | `sha256:13e37ac0637117684e46d9c8756c9461718e863029f19361f910370aecaa4b58` | `aefae49e` | 20 s |
| per_claim | `k3_eval500_pc_up_r1` | asrl-c4-1 | gvisor-pool | `j2-300d05e7` | `sha256:70f7e94d365f1aabc3b29b2be6d4c7d38816b982d977eea73c6a50e4261b2898` | `80c6ae71` | 20 s |
| per_claim | `k1_eval500_pc_up_r1`, `k1_eval500_pc_up_r2`, `k1_eval500_pc_up_r3` | asrl-c4-1 | gvisor-pool | `j2-300d05e7` | `sha256:70f7e94d365f1aabc3b29b2be6d4c7d38816b982d977eea73c6a50e4261b2898` | `aefae49e` | 20 s |
| per_claim | `k1c6_eval500_pc_up_r1`, `k1c6_eval500_pc_up_r2` | asrl-c4-6 | gvisor-pool | `j2-300d05e7` | `sha256:70f7e94d365f1aabc3b29b2be6d4c7d38816b982d977eea73c6a50e4261b2898` | `aefae49e` | 20 s |

Batch command (prefixes k1, k1c6, k3, k3c6):

```
python run_basic_swebench_3000.py --in-cluster --namespace agent-sandbox-rl --node-selector cloud.google.com/gke-nodepool=gvisor-pool --runtime-class gvisor --images-file /app/drivers/images/swebench500.txt --problems 500 --rollouts 1 --concurrency 500 --strategies naive --unmanaged-network-policy --termination-grace-seconds 0 --probe true --claim-mode batch --async-exec --batch-max-in-flight 64 --batch-create-rps 500
```

Per_claim command (prefixes k1, k1c6, k3):

```
python run_basic_swebench_3000.py --in-cluster --namespace agent-sandbox-rl --node-selector cloud.google.com/gke-nodepool=gvisor-pool --runtime-class gvisor --images-file /app/drivers/images/swebench500.txt --problems 500 --rollouts 1 --concurrency 500 --strategies naive --unmanaged-network-policy --termination-grace-seconds 0 --probe true --claim-mode per_claim
```


### B.4 3k (500×6; naive, sliding, pipelined)

| Arm | Runs | Cluster | Node pool | Image tag | Digest | `commit_sha` label in meta | Settle |
|---|---|---|---|---|---|---|---|
| batch | `k3_3k_500x6_batch_r1`, `k3_3k_500x6_batch_r2` | asrl-c4-1 | gvisor-pool | `k3-final` | `sha256:f7390a93d7c7a78557241efa58eb75c4cb0e0d3b4b10544a96de7f5fd624d7e1` | `80c6ae71` | 25 s |
| batch | `k1_3k_500x6_batch_r1` | asrl-c4-1 | gvisor-pool | `k1-final` | `sha256:13e37ac0637117684e46d9c8756c9461718e863029f19361f910370aecaa4b58` | `aefae49e` | 25 s |
| per_claim | `k3_3k_500x6_pc_up_r1` | asrl-c4-1 | gvisor-pool | `j2-300d05e7` | `sha256:70f7e94d365f1aabc3b29b2be6d4c7d38816b982d977eea73c6a50e4261b2898` | `80c6ae71` | 25 s |
| per_claim | `k1_3k_500x6_pc_up_r1` | asrl-c4-1 | gvisor-pool | `j2-300d05e7` | `sha256:70f7e94d365f1aabc3b29b2be6d4c7d38816b982d977eea73c6a50e4261b2898` | `aefae49e` | 25 s |

Batch command (prefixes k1, k3):

```
python run_basic_swebench_3000.py --in-cluster --namespace agent-sandbox-rl --node-selector cloud.google.com/gke-nodepool=gvisor-pool --runtime-class gvisor --images-file /app/drivers/images/swebench500.txt --problems 500 --rollouts 6 --concurrency 500 --strategies naive,sliding,pipelined --enable-cow --overcommit-factor 10 --unmanaged-network-policy --termination-grace-seconds 0 --probe true --claim-mode batch --async-exec --batch-max-in-flight 64 --batch-create-rps 500
```

Per_claim command (prefixes k1, k3):

```
python run_basic_swebench_3000.py --in-cluster --namespace agent-sandbox-rl --node-selector cloud.google.com/gke-nodepool=gvisor-pool --runtime-class gvisor --images-file /app/drivers/images/swebench500.txt --problems 500 --rollouts 6 --concurrency 500 --strategies naive,sliding,pipelined --enable-cow --overcommit-factor 10 --unmanaged-network-policy --termination-grace-seconds 0 --probe true --claim-mode per_claim
```


### B.5 500×2 parity (naive, sliding, pipelined)

| Arm | Runs | Cluster | Node pool | Image tag | Digest | `commit_sha` label in meta | Settle |
|---|---|---|---|---|---|---|---|
| batch | `k3_parity500x2_batch_r1` | asrl-c4-t0 | gvisor-n2-ssd | `k3-final` | `sha256:f7390a93d7c7a78557241efa58eb75c4cb0e0d3b4b10544a96de7f5fd624d7e1` | `80c6ae71` | 20 s |
| batch | `k1_parity500x2_batch_r1` | asrl-c4-t0 | gvisor-n2-ssd | `k1-final` | `sha256:13e37ac0637117684e46d9c8756c9461718e863029f19361f910370aecaa4b58` | `aefae49e` | 20 s |
| per_claim | `h1_parity500x2_pc_up_r1`, `h1_parity500x2_pc_up_r2` (no k-prefix run of this arm; latest earlier run shown) | asrl-c4-t0 | gvisor-n2-ssd | `f1-300d05e7` | `sha256:16af39f9332f6839b201071a3d2de2d222c36356c7513682e82af71a6fe06095` | `f85a5072` | 20 s |

Batch command (prefixes k1, k3):

```
python run_basic_swebench_3000.py --in-cluster --namespace agent-sandbox-rl --node-selector cloud.google.com/gke-nodepool=gvisor-n2-ssd --runtime-class gvisor --images-file /app/drivers/images/mixed_interleaved_500.txt --problems 500 --rollouts 2 --concurrency 200 --strategies naive,sliding,pipelined --window-size 100 --warm-per-task --max-warmpool-size 2 --overcommit-factor 10 --enable-cow --unmanaged-network-policy --termination-grace-seconds 0 --probe true --claim-mode batch --async-exec --batch-max-in-flight 64 --batch-create-rps 500
```

Per_claim command (prefixes h1):

```
python run_basic_swebench_3000.py --in-cluster --namespace agent-sandbox-rl --node-selector cloud.google.com/gke-nodepool=gvisor-n2-ssd --runtime-class gvisor --images-file /app/drivers/images/mixed_interleaved_500.txt --problems 500 --rollouts 2 --concurrency 200 --strategies naive,sliding,pipelined --window-size 100 --warm-per-task --max-warmpool-size 2 --overcommit-factor 10 --enable-cow --unmanaged-network-policy --termination-grace-seconds 0 --probe true --claim-mode per_claim
```


### B.6 1k burst

| Arm | Runs | Cluster | Node pool | Image tag | Digest | `commit_sha` label in meta | Settle |
|---|---|---|---|---|---|---|---|
| batch | `k3_burst_1x1000_batch_r1`, `k3_burst_1x1000_batch_r2` (failed) | asrl-c4-1 | gvisor-pool | `k3-final` | `sha256:f7390a93d7c7a78557241efa58eb75c4cb0e0d3b4b10544a96de7f5fd624d7e1` | `80c6ae71` | 25 s |
| batch | `k1_burst_1x1000_batch_r1` | asrl-c4-1 | gvisor-pool | `k1-final` | `sha256:13e37ac0637117684e46d9c8756c9461718e863029f19361f910370aecaa4b58` | `aefae49e` | 25 s |
| batch | `k1c6_burst_1x1000_batch_r1` | asrl-c4-6 | gvisor-pool | `k1-final` | `sha256:13e37ac0637117684e46d9c8756c9461718e863029f19361f910370aecaa4b58` | `aefae49e` | 25 s |
| per_claim | `k3_burst_1x1000_pc_up_r1` | asrl-c4-1 | gvisor-pool | `j2-300d05e7` | `sha256:70f7e94d365f1aabc3b29b2be6d4c7d38816b982d977eea73c6a50e4261b2898` | `80c6ae71` | 25 s |
| per_claim | `k1_burst_1x1000_pc_up_r1` | asrl-c4-1 | gvisor-pool | `j2-300d05e7` | `sha256:70f7e94d365f1aabc3b29b2be6d4c7d38816b982d977eea73c6a50e4261b2898` | `aefae49e` | 25 s |
| per_claim | `k1c6_burst_1x1000_pc_up_r1` | asrl-c4-6 | gvisor-pool | `j2-300d05e7` | `sha256:70f7e94d365f1aabc3b29b2be6d4c7d38816b982d977eea73c6a50e4261b2898` | `aefae49e` | 25 s |

Batch command (prefixes k1, k1c6, k3):

```
ASRL_COUNT_COLD=1 python run_claim_soak_bench.py --in-cluster --namespace agent-sandbox-rl --node-selector cloud.google.com/gke-nodepool=gvisor-pool --runtime-class gvisor --images-file /app/drivers/images/swebench500.txt --mode burst --problems 1 --replicas-per-image 1000 --concurrencies 100,250,500,1000 --repeats 3 --post-warm-settle-s 10 --verify-exec --unmanaged-network-policy --termination-grace-seconds 0 --claim-mode batch --async-exec --batch-max-in-flight 64
```

Per_claim command (prefixes k1, k1c6, k3):

```
ASRL_COUNT_COLD=1 python run_claim_soak_bench.py --in-cluster --namespace agent-sandbox-rl --node-selector cloud.google.com/gke-nodepool=gvisor-pool --runtime-class gvisor --images-file /app/drivers/images/swebench500.txt --mode burst --problems 1 --replicas-per-image 1000 --concurrencies 100,250,500,1000 --repeats 3 --post-warm-settle-s 10 --verify-exec --unmanaged-network-policy --termination-grace-seconds 0 --claim-mode per_claim
```


### B.7 20k soak

| Arm | Runs | Cluster | Node pool | Image tag | Digest | `commit_sha` label in meta | Settle |
|---|---|---|---|---|---|---|---|
| batch | `k3_soak20k_batch_r1` | asrl-c4-1 | gvisor-pool | `k3-final` | `sha256:f7390a93d7c7a78557241efa58eb75c4cb0e0d3b4b10544a96de7f5fd624d7e1` | `80c6ae71` | 25 s |
| per_claim | `f1_soak20k_pc_up_r1` (no k-prefix run of this arm; latest earlier run shown) | asrl-c4-1 | gvisor-pool | `f1-300d05e7` | `sha256:16af39f9332f6839b201071a3d2de2d222c36356c7513682e82af71a6fe06095` | `final` | 25 s |

Batch command (prefixes k3):

```
python run_claim_soak_bench.py --in-cluster --namespace agent-sandbox-rl --node-selector cloud.google.com/gke-nodepool=gvisor-pool --runtime-class gvisor --images-file /app/drivers/images/swebench500.txt --mode soak --problems 200 --replicas-per-image 2 --wave-size 200 --total-claims 20000 --no-verify-exec --overcommit-factor 10 --unmanaged-network-policy --termination-grace-seconds 0 --claim-mode batch --async-exec --batch-max-in-flight 64
```

Per_claim command (prefixes f1):

```
python run_claim_soak_bench.py --in-cluster --namespace agent-sandbox-rl --node-selector cloud.google.com/gke-nodepool=gvisor-pool --runtime-class gvisor --images-file /app/drivers/images/swebench500.txt --mode soak --problems 200 --replicas-per-image 2 --wave-size 200 --total-claims 20000 --no-verify-exec --overcommit-factor 10 --unmanaged-network-policy --termination-grace-seconds 0 --claim-mode per_claim
```


### B.8 Tunix-shaped 64×16×6

| Arm | Runs | Cluster | Node pool | Image tag | Digest | `commit_sha` label in meta | Settle |
|---|---|---|---|---|---|---|---|
| batch | `k1_tunix_64x16x6_batch_r1` | asrl-c4-t0 | gvisor-n2-ssd | `k1-final` | `sha256:13e37ac0637117684e46d9c8756c9461718e863029f19361f910370aecaa4b58` | `aefae49e` | 25 s |
| per_claim | `h1_tunix_64x16x6_pc_up_r1`, `h1_tunix_64x16x6_pc_up_r2` (no k-prefix run of this arm; latest earlier run shown) | asrl-c4-t0 | gvisor-n2-ssd | `f1-300d05e7` | `sha256:16af39f9332f6839b201071a3d2de2d222c36356c7513682e82af71a6fe06095` | `f85a5072` | 25 s |

Batch command (prefixes k1):

```
python run_step_bench.py --in-cluster --namespace agent-sandbox-rl --node-selector cloud.google.com/gke-nodepool=gvisor-n2-ssd --runtime-class gvisor --images-file /app/drivers/images/r2e_subset_500.txt --problems 64 --rollouts 16 --steps 6 --concurrency 1024 --cpu 125m --memory 512Mi --strategies naive --warm-per-task --max-warmpool-size 16 --enable-cow --overcommit-factor 10 --unmanaged-network-policy --termination-grace-seconds 0 --warm-create-budget 512 --probe 'if [ -d /testbed ]; then cd /testbed; fi; (git status >/dev/null 2>&1 || true); sleep 10' --claim-mode batch --async-exec --batch-max-in-flight 64
```

Per_claim command (prefixes h1):

```
python run_step_bench.py --in-cluster --namespace agent-sandbox-rl --node-selector cloud.google.com/gke-nodepool=gvisor-n2-ssd --runtime-class gvisor --images-file /app/drivers/images/r2e_subset_500.txt --problems 64 --rollouts 16 --steps 6 --concurrency 1024 --cpu 125m --memory 512Mi --strategies naive --warm-per-task --max-warmpool-size 16 --enable-cow --overcommit-factor 10 --unmanaged-network-policy --termination-grace-seconds 0 --warm-create-budget 512 --probe 'if [ -d /testbed ]; then cd /testbed; fi; (git status >/dev/null 2>&1 || true); sleep 10' --claim-mode per_claim
```

Soak: no per_claim soak was run in the k rounds. Cross-round only: batch `k3_soak20k_batch_r1` soak wall 1,752 s, 20,000/20,000 ok; per_claim `f1_soak20k_pc_up_r1` (image `f1-300d05e7`, earlier the same day, 10:09Z) soak wall 3,178 s, 20,000/20,000 ok, 0 errors.

### B.9 How to rerun

Add a line `name|tag|settle|command` to a plan file and run `PLAN_SHA=<label> bash gke_bench_v8/exp_plan8.sh <cluster> <planfile>`. That calls, per line, `COMMIT_SHA=<label> DRIVER_TAG=<tag> SETTLE_S=<settle> CTX=<kube context> KUBECONFIG=gke_bench_v4/kubeconfig_<cluster>.yaml bash gke_bench_v8/run_job_v8.sh <name> "<command>"`. A name that already has `runs/<name>.json` is skipped, so a rerun needs a new name. The launcher resolves the tag to a digest in `gke_bench_v7/IMAGE_DIGESTS.txt` and refuses to run if none is recorded. It deletes leftovers in namespace `agent-sandbox-rl`, waits until pods, claims, warm pools, sandboxes, templates, daemonsets and leases are all zero, then sleeps the settle time. It snapshots API priority-and-fairness metrics and the controller pods before and after. The driver runs as a Kubernetes Job (service account `asrl-driver`, node selector `bench-role=driver`, requests 8 CPU / 16 Gi, limits 14 CPU / 28 Gi, `backoffLimit` 0). The result JSON is cut out of the pod log and saved with `meta.json`. One retry happens only if the pod log shows an API 500.

### B.10 Cluster configuration established from files

| Fact | asrl-c4-1 | asrl-c4-t0 | asrl-c4-6 | Source |
|---|---|---|---|---|
| Region | us-central1 | us-central1 | us-central1, nodes in zones a and b | meta `context`; `provision_c6.sh` |
| Sandbox node pool | `gvisor-pool`, 10× n2-standard-32, 600 GB disk | `gvisor-n2-ssd`, 20× n2-standard-32, 1,000 GB pd-ssd | `gvisor-pool`, 10× n2-standard-32 (5 per zone × 2), pd-balanced 1,400 GB, max 256 pods per node | tracker lines 40–41; `provision_c6.sh` |
| Runtime | gVisor (`--runtime-class gvisor` in every command) | same | same; pool created with `--sandbox=type=gvisor` | driver_cmd; `provision_c6.sh` |
| Image streaming | on (tracker: "Riptide V2") | on (tracker: "Riptide V2") | on (`--enable-image-streaming` on cluster and pool) | tracker; `provision_c6.sh` |
| Driver pool | not recorded | not recorded | 1 node per zone × 2, n2-standard-16, label `bench-role=driver` | `provision_c6.sh` |
| Controller pool | not recorded | node names contain `ctrl-pool` | e2-standard-32, 1 per zone × 2, tainted `dedicated=ctrl` | `ctrl_before.json`; `provision_c6.sh` |
| Release channel | not recorded | not recorded | rapid | `provision_c6.sh` |
| GKE version | not recorded | not recorded | not recorded | no version string in runs, meta, pod logs or tracker |

Controller, identical in `ctrl_before.json` on all three clusters for the k3 runs checked (`k3_eval500_batch_r1`, `k3_r2e500x2x8_batch_r1`, `k3c6_r2e500x2x8_batch_r1`):

- Image `us-central1-docker.pkg.dev/<PROJECT>/rl-gke-bench/agent-sandbox-controller@sha256:8d11e685651cfb17e406d3440256dedb8317c219a7fe80a789999290d3161ab1`.
- Args: `--leader-elect=true --extensions --sandbox-concurrent-workers=1000 --sandbox-claim-concurrent-workers=500 --sandbox-warm-pool-concurrent-workers=500 --sandbox-template-concurrent-workers=100 --sandbox-warm-pool-max-batch-size=500 --sandbox-warm-pool-max-refill-rate=500 --sandbox-warm-pool-readiness-grace-period=30m --kube-api-qps=-1 --api-connections=4 --separate-watch-connection=true --cache-label-selectors=true --disable-claim-events=true --disable-sandbox-events=true --disable-claim-observability-annotations=true`.
- Resources: requests 14 CPU / 28 Gi, limits 28 CPU / 32 Gi. Leader election on, 2 replicas.
- `asrl-c4-6` was built with a 170-node-per-zone spot e2-medium warm-up pool, private nodes, and bootstrap tier `t1` (`gke_bench_v4/bootstrap_cluster_v4.sh`), then the controller image and args were copied from `asrl-c4-t0`.

## Appendix C. Metrics a lab would rank: final image (`k3-final`) vs upstream per_claim

Ratio is per_claim ÷ batch for times and resources, batch ÷ per_claim for claims/s, so above 1 means batch is better. TTFI in the eval is `paired_ttfi_s`; in the burst it is `ttfi_s` of each burst. Threads, sockets and RSS are `proc_held` of the three c=1000 bursts.

| Metric | Shape | Cluster | Batch (median, range, n) | per_claim (median, range, n) | Ratio | Runs |
|---|---|---|---|---|---|---|
| Wall s | R2E 500×2×8 | asrl-c4-t0 | 207 (203–220, n=3) | 1,015 (1,004–1,027, n=2) | 4.90× | B: k3_r2e500x2x8_batch_r1, k3_r2e500x2x8_batch_r2, k3_r2e500x2x8_batch_r3; P: k3_r2e500x2x8_pc_up_r1, k3_r2e500x2x8_pc_up_r2 |
| Wall s | R2E 500×2×8 | asrl-c4-6 (per_claim from the k1c6 round, not paired) | 253 (n=1) | 239 (237–240, n=2) | 0.94× | B: k3c6_r2e500x2x8_batch_r1; P: k1c6_r2e500x2x8_pc_up_r1, k1c6_r2e500x2x8_pc_up_r2 |
| Wall s | 3k naive | asrl-c4-1 | 160 (158–162, n=2) | 393 (n=1) | 2.46× | B: k3_3k_500x6_batch_r1, k3_3k_500x6_batch_r2; P: k3_3k_500x6_pc_up_r1 |
| Wall s | 3k sliding | asrl-c4-1 | 166 (157–175, n=2) | 398 (n=1) | 2.39× | B: k3_3k_500x6_batch_r1, k3_3k_500x6_batch_r2; P: k3_3k_500x6_pc_up_r1 |
| Wall s | 3k pipelined | asrl-c4-1 | 180 (169–192, n=2) | 420 (n=1) | 2.33× | B: k3_3k_500x6_batch_r1, k3_3k_500x6_batch_r2; P: k3_3k_500x6_pc_up_r1 |
| Wall s | Eval 500×1 | asrl-c4-1 | 16.3 (16.0–16.6, n=2) | 18.7 (n=1) | 1.15× | B: k3_eval500_batch_r1, k3_eval500_batch_r2; P: k3_eval500_pc_up_r1 |
| TTFI p50 s | Eval 500×1 | asrl-c4-1 | 2.09 (1.87–2.30, n=2) | 3.40 (n=1) | 1.63× | B: k3_eval500_batch_r1, k3_eval500_batch_r2; P: k3_eval500_pc_up_r1 |
| TTFI p99 s | Eval 500×1 | asrl-c4-1 | 3.08 (2.92–3.25, n=2) | 4.08 (n=1) | 1.32× | B: k3_eval500_batch_r1, k3_eval500_batch_r2; P: k3_eval500_pc_up_r1 |
| TTFI p50 s | Burst c=500 | asrl-c4-1 | 1.72 (1.70–1.90, n=3) | 3.83 (3.57–3.84, n=3) | 2.23× | B: k3_burst_1x1000_batch_r1; P: k3_burst_1x1000_pc_up_r1 |
| TTFI p99 s | Burst c=500 | asrl-c4-1 | 2.46 (2.44–2.87, n=3) | 5.30 (4.98–5.41, n=3) | 2.15× | B: k3_burst_1x1000_batch_r1; P: k3_burst_1x1000_pc_up_r1 |
| TTFI p50 s | Burst c=1000 | asrl-c4-1 | 2.69 (2.47–3.14, n=3) | 10.9 (6.36–10.9, n=3) | 4.07× | B: k3_burst_1x1000_batch_r1; P: k3_burst_1x1000_pc_up_r1 |
| TTFI p99 s | Burst c=1000 | asrl-c4-1 | 4.71 (4.42–4.84, n=3) | 12.1 (8.46–12.2, n=3) | 2.56× | B: k3_burst_1x1000_batch_r1; P: k3_burst_1x1000_pc_up_r1 |
| Claims/s | Burst c=500 | asrl-c4-1 | 260 (240–281, n=3) | 92.5 (87.9–94.3, n=3) | 2.81× | B: k3_burst_1x1000_batch_r1; P: k3_burst_1x1000_pc_up_r1 |
| Claims/s | Burst c=1000 | asrl-c4-1 | 233 (233–253, n=3) | 107 (106–119, n=3) | 2.17× | B: k3_burst_1x1000_batch_r1; P: k3_burst_1x1000_pc_up_r1 |
| Driver threads (proc_held) | Burst c=1000 | asrl-c4-1 | 19 (16–19, n=3) | 3,017 (3,017–3,017, n=3) | 159× | B: k3_burst_1x1000_batch_r1; P: k3_burst_1x1000_pc_up_r1 |
| Driver open sockets (proc_held) | Burst c=1000 | asrl-c4-1 | 84 (78–84, n=3) | 1,064 (991–1,151, n=3) | 12.7× | B: k3_burst_1x1000_batch_r1; P: k3_burst_1x1000_pc_up_r1 |
| Driver RSS MB (proc_held) | Burst c=1000 | asrl-c4-1 | 371 (353–376, n=3) | 3,748 (2,782–4,313, n=3) | 10.1× | B: k3_burst_1x1000_batch_r1; P: k3_burst_1x1000_pc_up_r1 |
| Task success, exec errors | R2E 500×2×8 | asrl-c4-t0 | 24,000 / 24,000 ok (100.00%); errors 0; empty-probe 0; n=3 | 16,000 / 16,000 ok (100.00%); errors 0; empty-probe 0; n=2 | – | B: k3_r2e500x2x8_batch_r1, k3_r2e500x2x8_batch_r2, k3_r2e500x2x8_batch_r3; P: k3_r2e500x2x8_pc_up_r1, k3_r2e500x2x8_pc_up_r2 |
| Task success, exec errors | R2E 500×2×8 | asrl-c4-6 | 8,000 / 8,000 ok (100.00%); errors 0; empty-probe 0; n=1 | – | – | B: k3c6_r2e500x2x8_batch_r1 |
| Task success, exec errors | Multi-step 250×2×6 | asrl-c4-t0 | 3,000 / 3,000 ok (100.00%); errors 0; empty-probe 0; n=1 | 3,000 / 3,000 ok (100.00%); errors 0; empty-probe 0; n=1 | – | B: k3_stept0_250x2x6_batch_r1; P: k3_stept0_250x2x6_pc_up_r1 |
| Task success, exec errors | Eval 500×1 | asrl-c4-1 | 1,000 / 1,000 ok (100.00%); errors 0; empty-probe 0; n=2 | 500 / 500 ok (100.00%); errors 0; empty-probe 0; n=1 | – | B: k3_eval500_batch_r1, k3_eval500_batch_r2; P: k3_eval500_pc_up_r1 |
| Task success, exec errors | Eval 500×1 | asrl-c4-6 | 500 / 500 ok (100.00%); errors 0; empty-probe 0; n=1 | – | – | B: k3c6_eval500_batch_r1 |
| Task success, exec errors | 3k (3 strategies) | asrl-c4-1 | 18,000 / 18,000 ok (100.00%); errors 0; empty-probe 0; n=2 | 9,000 / 9,000 ok (100.00%); errors 0; empty-probe 0; n=1 | – | B: k3_3k_500x6_batch_r1, k3_3k_500x6_batch_r2; P: k3_3k_500x6_pc_up_r1 |
| Task success, exec errors | 500×2 parity | asrl-c4-t0 | 3,000 / 3,000 ok (100.00%); errors 0; empty-probe 0; n=1 | – | – | B: k3_parity500x2_batch_r1 |
| Task success, exec errors | Burst (all c) | asrl-c4-1 | 5,550 / 5,550 ok (100.00%); errors 0; empty-probe 0; n=1 | 5,550 / 5,550 claims ok; 3 probes returned empty output (5,547 / 5,550 = 99.95%); n=1 | – | B: k3_burst_1x1000_batch_r1; P: k3_burst_1x1000_pc_up_r1 |
| Task success, exec errors | Soak 20k | asrl-c4-1 | 20,000 / 20,000 ok (100.00%); errors 0; empty-probe 0; n=1 | – | – | B: k3_soak20k_batch_r1 |
| Task success, all k3/k3c6 runs with a result | all | all | 83,050 / 83,050 (100.000%) | 34,050 / 34,050 (100.000%) | – | rows above |

Notes:

- Every per_claim cell for eval and 3k is n=1. The burst cells are 3 bursts inside one run per arm (one driver process).
- The fresh-cluster R2E row compares the final-image batch run with per_claim runs from the previous round; it is not a paired comparison.
- Per_claim RSS rises burst by burst inside the run (2,782, 3,748, 4,313 MB at the three c=1000 bursts), so the median depends on burst order. Batch stays at 353–376 MB.
- Per_claim threads read 3,017 in all three c=1000 bursts.
- The 3 per_claim exec errors are empty probe output at the first c=1000 burst (`errors_sample`: `probe non-zero exit: ''`). The claims themselves are counted ok.
- `k3_burst_1x1000_batch_r2` produced no result (warm pool 992 of 1,000 ready, timeout after 601 s) and is in no row.

## Appendix D. Comparisons where batch is within 10% of per_claim or worse (k1 and k3 rounds)

Scope: every wall, TTFI p50/p99, claims/s, burst+exec wall, release throughput and (step runs) env-ready p50 pair that exists for the same shape and cluster in the k1, k1c6, k3 and k3c6 rounds: 110 pairs. 22 have a ratio below 1.10 (same ratio convention as Appendix C). `k3c6` batch has no per_claim run of its own and is compared with the `k1c6` per_claim runs. For step runs TTFI p50 is the median of the per-step p50s and TTFI p99 is the largest per-step p99.

| # | Shape | Cluster | Batch runs / per_claim runs | Metric | Batch | per_claim | Ratio |
|---|---|---|---|---|---|---|---|
| D1 | r2e500x2x8 | asrl-c4-6 | k1c6_r2e500x2x8_batch_r1, k1c6_r2e500x2x8_batch_r2 / k1c6_r2e500x2x8_pc_up_r1, k1c6_r2e500x2x8_pc_up_r2 | wall s | 239 (229–249, n=2) | 239 (237–240, n=2) | 1.00× |
| D2 | r2e500x2x8 | asrl-c4-6 | k1c6_r2e500x2x8_batch_r1, k1c6_r2e500x2x8_batch_r2 / k1c6_r2e500x2x8_pc_up_r1, k1c6_r2e500x2x8_pc_up_r2 | TTFI p99 s (max of per-step p99) | 11.9 (10.6–13.2, n=2) | 12.9 (12.6–13.2, n=2) | 1.08× |
| D3 | r2e500x2x8 | asrl-c4-6 | k1c6_r2e500x2x8_batch_r1, k1c6_r2e500x2x8_batch_r2 / k1c6_r2e500x2x8_pc_up_r1, k1c6_r2e500x2x8_pc_up_r2 | env-ready p50 s (median of per-step p50) | 14.8 (13.5–16.1, n=2) | 13.1 (13.1–13.2, n=2) | 0.89× |
| D4 | r2e500x2x8 | asrl-c4-6 | k3c6_r2e500x2x8_batch_r1 / k1c6_r2e500x2x8_pc_up_r1, k1c6_r2e500x2x8_pc_up_r2 | wall s | 253 (n=1) | 239 (237–240, n=2) | 0.94× |
| D5 | r2e500x2x8 | asrl-c4-6 | k3c6_r2e500x2x8_batch_r1 / k1c6_r2e500x2x8_pc_up_r1, k1c6_r2e500x2x8_pc_up_r2 | TTFI p99 s (max of per-step p99) | 12.2 (n=1) | 12.9 (12.6–13.2, n=2) | 1.06× |
| D6 | r2e500x2x8 | asrl-c4-6 | k3c6_r2e500x2x8_batch_r1 / k1c6_r2e500x2x8_pc_up_r1, k1c6_r2e500x2x8_pc_up_r2 | env-ready p50 s (median of per-step p50) | 14.7 (n=1) | 13.1 (13.1–13.2, n=2) | 0.89× |
| D7 | 3k_500x6 | asrl-c4-1 | k3_3k_500x6_batch_r1, k3_3k_500x6_batch_r2 / k3_3k_500x6_pc_up_r1 | naive TTFI p50 s | 21.7 (20.8–22.5, n=2) | 6.31 (n=1) | 0.29× |
| D8 | 3k_500x6 | asrl-c4-1 | k3_3k_500x6_batch_r1, k3_3k_500x6_batch_r2 / k3_3k_500x6_pc_up_r1 | sliding TTFI p50 s | 19.2 (18.6–19.8, n=2) | 6.46 (n=1) | 0.34× |
| D9 | 3k_500x6 | asrl-c4-1 | k3_3k_500x6_batch_r1, k3_3k_500x6_batch_r2 / k3_3k_500x6_pc_up_r1 | pipelined TTFI p50 s | 19.1 (18.7–19.5, n=2) | 14.2 (n=1) | 0.75× |
| D10 | 3k_500x6 | asrl-c4-1 | k1_3k_500x6_batch_r1 / k1_3k_500x6_pc_up_r1 | naive TTFI p50 s | 18.3 (n=1) | 6.79 (n=1) | 0.37× |
| D11 | 3k_500x6 | asrl-c4-1 | k1_3k_500x6_batch_r1 / k1_3k_500x6_pc_up_r1 | sliding TTFI p50 s | 19.5 (n=1) | 6.25 (n=1) | 0.32× |
| D12 | 3k_500x6 | asrl-c4-1 | k1_3k_500x6_batch_r1 / k1_3k_500x6_pc_up_r1 | pipelined TTFI p50 s | 18.5 (n=1) | 13.9 (n=1) | 0.75× |
| D13 | burst_1x1000 | asrl-c4-1 | k3_burst_1x1000_batch_r1 / k3_burst_1x1000_pc_up_r1 | c=100 release sandboxes/s | 688 (641–719, n=3) | 949 (847–1,017, n=3) | 0.73× |
| D14 | burst_1x1000 | asrl-c4-1 | k3_burst_1x1000_batch_r1 / k3_burst_1x1000_pc_up_r1 | c=250 release sandboxes/s | 801 (640–853, n=3) | 1,012 (935–1,053, n=3) | 0.79× |
| D15 | burst_1x1000 | asrl-c4-1 | k3_burst_1x1000_batch_r1 / k3_burst_1x1000_pc_up_r1 | c=500 release sandboxes/s | 746 (644–877, n=3) | 996 (738–1,000, n=3) | 0.75× |
| D16 | burst_1x1000 | asrl-c4-1 | k3_burst_1x1000_batch_r1 / k3_burst_1x1000_pc_up_r1 | c=1000 release sandboxes/s | 757 (634–951, n=3) | 1,113 (937–1,142, n=3) | 0.68× |
| D17 | burst_1x1000 | asrl-c4-1 | k1_burst_1x1000_batch_r1 / k1_burst_1x1000_pc_up_r1 | c=100 release sandboxes/s | 648 (477–708, n=3) | 791 (745–919, n=3) | 0.82× |
| D18 | burst_1x1000 | asrl-c4-1 | k1_burst_1x1000_batch_r1 / k1_burst_1x1000_pc_up_r1 | c=250 release sandboxes/s | 699 (559–896, n=3) | 1,019 (1,012–1,040, n=3) | 0.69× |
| D19 | burst_1x1000 | asrl-c4-1 | k1_burst_1x1000_batch_r1 / k1_burst_1x1000_pc_up_r1 | c=1000 release sandboxes/s | 884 (868–999, n=3) | 1,121 (1,114–1,159, n=3) | 0.79× |
| D20 | burst_1x1000 | asrl-c4-6 | k1c6_burst_1x1000_batch_r1 / k1c6_burst_1x1000_pc_up_r1 | c=250 release sandboxes/s | 1,188 (1,066–1,261, n=3) | 1,100 (1,080–1,116, n=3) | 1.08× |
| D21 | burst_1x1000 | asrl-c4-6 | k1c6_burst_1x1000_batch_r1 / k1c6_burst_1x1000_pc_up_r1 | c=500 TTFI p99 s | 8.69 (8.62–8.69, n=3) | 9.10 (8.74–9.57, n=3) | 1.05× |
| D22 | burst_1x1000 | asrl-c4-6 | k1c6_burst_1x1000_batch_r1 / k1c6_burst_1x1000_pc_up_r1 | c=1000 TTFI p99 s | 21.2 (18.0–22.1, n=3) | 22.2 (19.9–28.1, n=3) | 1.05× |

No per_claim run exists in the k rounds for 500×2 parity, the soak or the Tunix shape, so those have no pair here. Cross-round figures for parity are in D-x below.

### Causes

**D1, D4. Fresh-cluster R2E wall (1.00× and 0.94×).** What the JSON shows, per run, summed over the 8 steps:

| Run | Wall | Until all envs ready (`env_ready_all_s`) | Ready to last task done | Last task done to step end | Outside steps |
|---|---|---|---|---|---|
| k1c6 batch r1 | 249.4 | 166.3 | 66.2 | 6.7 | 10.2 |
| k1c6 batch r2 | 228.8 | 153.4 | 58.1 | 5.1 | 12.1 |
| k3c6 batch r1 | 253.3 | 168.3 | 67.4 | 6.2 | 11.4 |
| k1c6 per_claim r1 | 236.9 | 162.6 | 65.4 | 1.5 | 7.4 |
| k1c6 per_claim r2 | 240.2 | 162.8 | 65.3 | 1.7 | 10.5 |

- About two thirds of the wall in both arms is waiting for environments to become ready, and the totals are the same (153–168 s batch, 163 s per_claim). The claim phase is about 1 s per task in both arms. So the step time is set by environment readiness, where the two arms do not differ.
- Batch spends 5–7 s per run between the last task finishing and the step ending, per_claim 1.5–1.7 s. That is the only phase where batch is consistently slower here (about 4–5 s per run).
- Why environments take about 20 s per step to become ready is **not established** from these files. The pod logs of these five runs contain no `Insufficient memory` line (0 matches), so the report's capacity explanation cannot be checked from them. Scheduler events and a pod start timeline per step would settle it.
- The 253 s of the final image against 229–249 s of the previous image: step 0 took 39.4 s (previous image 30.7 and 33.4 s; per_claim 34.7 and 36.6 s). One run, no per_claim run beside it. Whether this is the image or run-to-run spread is **not established**; a paired batch and per_claim rerun on `asrl-c4-6` with `k3-final` would settle it.

**D3, D6. Fresh-cluster R2E env-ready p50 (0.89×).** The JSON shows the warm-pool wait is longer for batch in steps 1–7: `wait_pool_ready` average 0.2–1.8 s per step for batch against 0.05–0.28 s for per_claim, and the per-step maximum up to 11.7 s against up to 5.9 s. Why the pools become ready later for batch is **not established**; per-pool create and ready timestamps set against the deletion times of the previous step's pods would settle it.

**D2, D5. Fresh-cluster R2E TTFI p99 (1.06–1.08×, batch slightly ahead).** Shown in the data: the probe in this shape ends with `sleep 5`, and first-command time is about 6 s in both arms (5.9–6.7 s batch, 6.0–7.4 s per_claim). TTFI is claim time plus that, so a fixed 5 s is in both sides and compresses the ratio. Ranges also overlap (10.6–13.2 vs 12.6–13.2 s).

**D7–D12. 3k TTFI p50 (0.29–0.75×).** Shown in the data: TTFI here is almost all claim time. First-command p50 is 0.32–0.36 s in both arms. Batch claim time is even: average 17–20 s, p50 18–22 s, maximum 82–145 s. Per_claim is skewed: p50 6–14 s, average 48–57 s, maximum 244–373 s. So per_claim's median task is claimed sooner and its tail much later (TTFI p99 229–357 s against 39–62 s for batch), and batch finishes the run 2.3–2.5× sooner. Why batch's median claim waits longer is **not established** from the JSON: it holds no timeline of pool refill against claim arrival. Per-claim start and bind timestamps next to the warm-pool ready count over time would settle it.

**D13–D19. Burst bulk-release throughput on `asrl-c4-1` (0.68–0.82×).** The JSON records only `release_wall_s` per burst (for example 1.58 s for 1,000 sandboxes in the last final-image burst). There is no breakdown of the release, and the pod log has none either. It is not consistent across cells: on the previous image at c=500 batch was ahead (899 vs 718/s), and on the fresh cluster batch was ahead at c=100, 500 and 1000 (1.12–1.71×). **Cause not established.** Timing each request inside the release (the claim deletecollection and the grouped Sandbox and pod deletes), with direct delete on and off on `asrl-c4-1`, would settle it.

**D20. Fresh-cluster release throughput at c=250 (1.08×, batch ahead).** Ranges overlap (1,066–1,261 vs 1,080–1,116/s). **Cause not established**; same measurement as D13–D19.

**D21, D22. Fresh-cluster burst TTFI p99 at c=500 and c=1000 (1.05×, batch ahead).** Shown in the data: on this cluster the first exec, not the claim, sets the p99. At c=1000 claim latency p99 is 3.3–4.8 s for batch against 6.3–8.4 s for per_claim, but first-exec p99 is 14.2–18.4 s against 16.8–22.6 s. At c=500 claim p99 is 1.6–1.8 s against 3.8–3.9 s, and first-exec p99 is 7.3–7.6 s for batch against 5.7–6.4 s for per_claim, which cancels the claim gain. On `asrl-c4-1` batch first-exec p99 at c=1000 is 1.1–1.3 s. Why first exec is slow on the fresh cluster, and slower for batch at c=500, is **not established**; splitting exec time into connect and run, and recording whether the image was already on the node, would settle it.

**D-x. 500×2 parity, cross-round only (not a k pair).** Batch `k3_parity500x2_batch_r1` on `asrl-c4-t0`: naive 31.9 s, sliding 41.7 s, pipelined 43.0 s (`k1_parity500x2_batch_r1`: 31.0, 42.2, 40.2 s). Per_claim `h1_parity500x2_pc_up_r1`/`r2`, image `f1-300d05e7`, earlier the same day: naive 35.0 / 36.6 s, sliding 42.4 / 42.6 s, pipelined 41.9 / 42.2 s. Sliding is level. Pipelined is 2–3% worse on the final image and 4% better on the previous one. The per-task phases do not explain the wall (claim plus process is about 1.5 s per task in both arms; prefetch averages 8.9 s batch and 8.2 s per_claim). **Cause not established**; a per-window timeline (window start, pool ready, last release) would settle it.
