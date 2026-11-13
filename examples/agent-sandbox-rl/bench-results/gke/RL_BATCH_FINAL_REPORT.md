# RL batch claim mode: final report (2026-11-13)

Batch claim mode vs upstream `per_claim` (`300d05e7`, unchanged), three GKE clusters, gVisor, image streaming on. Benchmark numbers in sections 2, 4 and 5 are read from the raw result JSON and pod logs in `scratch/gke_bench_v8/runs/` (full tables: `scratch/gke_bench_v8/tables_k1.md`) and were recomputed independently by an auditor, whose corrections are applied here (`brain/4fbea1c2-e01e-40b1-9cd4-773c916a7f11/AUDIT_RL_BATCH_FINAL_REPORT.md`). Sections 3 and 7 summarise investigation reports and were not recomputed. Batch runs use the defaults `create_rps` 500, `max_in_flight` 64 and async exec. "n" is the number of runs; ranges are min–max.

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

Same shapes on the previous image (`k1-final` = `aefae49e`, which differs from the final SHA only by the SDK memory fix, the bulk-release guard and the label probe) agree: R2E older 208 s (n=2) vs 1,000 s; eval 15.6 s (n=3) vs 18.8 s (n=3), TTFI p50 1.81 vs 3.41 s; 3k naive 150 vs 392 s; burst c=1000 234 vs 108 claims/s. On the fresh cluster the 1k burst gives 230 vs 117 claims/s at c=1000 (TTFI p50 8.6 vs 17.2 s; p99 21.2 vs 22.2 s, ranges overlap).

Where batch is worse or equal:

- **Fresh-cluster R2E is a tie on the previous image and 6% slower on the single final-image run**, which had no per_claim run beside it. The scheduler reports `Insufficient memory` there for both arms, so node capacity sets the pace. One batch run on an earlier image had an 86 s step (one pool waited 56 s).
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

