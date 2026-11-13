# RL batch-mode optimizations

Status as of 2026-11-13. Branch `feat/c5-batch-rebased-300d05e7`, tip `3a14c1dc`,
12 commits on top of `feat/rl-batch-hardening` at `300d05e7` ("upstream" below).
The branch is local and has not been pushed, so the commit ids below do not resolve
on GitHub yet.

`per_claim` mode is unchanged from upstream; every item below applies to
`claim_mode="batch"` only.

Benchmark image for the measurements: built from `f85a5072`. The one commit after
it (`3a14c1dc`) changes nothing at the benchmarked `max_in_flight=64`.

## 1. Optimizations measured on their own

| # | Change | Commit | Measured effect |
|---|---|---|---|
| 1 | **Delete a released claim's Sandbox and pod directly.** After the claim's own delete succeeds, a small background pool deletes the Sandbox and then its pod instead of leaving them to the cluster's garbage collector. Best effort; needs `delete` on `sandboxes` and `pods`; the first 403 turns it off. | `2eb5ef6d` | Removes the warm-pool refill stall on clusters that clear released sandboxes slowly. See the table below. |
| 2 | **Cap those background deletes at `max_in_flight`** (ceiling 16). | `3a14c1dc` | No change at the default of 64; keeps a fleet with a low `max_in_flight` from sending 16 extra requests. |
| 3 | **Skip aiohttp's per-request proxy and netrc lookups** when the process has neither configured. | `feffbac2`, `03390885` | Driver threads 23 → 3; client CPU in a 1,000-claim burst down 10–20%; claim rate at concurrency 1,000 from 191–211/s to about 240/s (4 bursts per arm, two clusters). |

Effect of #1, batch vs upstream `per_claim`, wall-clock seconds, all tasks OK
unless noted:

| Shape | Cluster | batch | upstream `per_claim` |
|---|---|---|---|
| Multi-step 250×2×6, 10 s per step | 20 nodes, 4 days old | 116, 117, 117, 119, 123 | 251, 266, 281 |
| Multi-step 250×2×6, 10 s per step | 10 nodes, 4 days old | 172, 180, 197, 199 | 364, 402, 456 |
| R2E 500×2×8, 5 s per step | 20 nodes, 4 days old | 209, 222, 224, 279 | 993, 1,007, 1,010, 1,010, 1,010 |
| 3,000 tasks 500×6, naive / sliding / pipelined | 10 nodes, 4 days old | 148 / 163 (2,998 of 3,000 OK) / 181 and 154 / 164 / 181 | 390 / 394 / 421 and 393 / 397 / 403 |
| Multi-step 250×2×6, 10 s per step | 10 nodes, freshly created | 99, 102 | 108, 108 |
| R2E 500×2×8, 5 s per step | 10 nodes, freshly created | 236, 243 (and 292, 337 right after a run that exhausted node pod IPs) | 236, 250, 252 |

Read the last two rows with the others: on a freshly created cluster released
sandboxes were cleared promptly, upstream `per_claim` did not stall, and batch was
level on R2E and about 7% faster on multi-step. The large ratios are specific to
clusters where cleanup is slow. Why the two older clusters clear slowly has not been
identified.

Before #1, batch and upstream `per_claim` were level on these shapes (for example
multi-step on the 10-node cluster: batch 394–405 s, `per_claim` 368–381 s).

## 2. Optimizations carried by earlier commits on the branch

These are in effect at the tip. They were measured together, as "batch vs upstream
`per_claim`", not one by one.

| Change | Commit |
|---|---|
| Batch claims run natively on the asyncio event loop (`AsyncBatchClaims`) instead of through worker threads | `b386d881` |
| Exec over a shared WebSocket API client on the event loop (`handle.aexec()`), no thread per exec | `b386d881` |
| Watch events are not re-serialized; claim create/delete responses are drained without decoding | `b386d881` |
| Warm-credit refresh and pool prechecks use one paginated namespace LIST instead of a GET per pool | `b386d881` |
| Large release tails are coalesced into one deletecollection; small windows release per claim | `b386d881` |
| 64 keep-alive TLS connections are opened per cluster before the first burst | `93f93fc9` |
| RL defaults `create_rps=500`, `max_in_flight=64` | `50e4531b` |
| Tail release is deferred only while no sibling acquire is waiting | `2f87f7a9` |
| Pipelined windows seed warm-pool readiness when the window starts, and background prefetch waits up to 250 ms for foreground creates | `5f87abe9` |

Combined effect, batch vs upstream `per_claim` (10-node cluster, zero errors):

| Measure | batch | upstream `per_claim` |
|---|---|---|
| Claim rate, 1,000 claims at concurrency 500 | 267/s | 91/s |
| Claim rate, 1,000 claims at concurrency 1,000 | 246/s | 106/s |
| Time to first instruction p50 at concurrency 1,000 | 2.9 s | 10.0 s |
| Burst + first exec wall at concurrency 1,000 | 5.4 s | 12.5 s |
| Driver threads / sockets at concurrency 1,000 | 3 / 68 | about 3,000 / about 1,050 |
| Eval 500×1 at concurrency 500, wall | 15.6 s | 18.6 s |

(Medians of 6 bursts per arm; eval is the median of 3 runs per arm.)

## 3. Defaults confirmed by sweeps (no code change)

- `max_in_flight` 64 / 128 / 256: same wall time (500 claims at concurrency 500, and
  1,000 claims at concurrency 200). Claim throughput is limited by the controller.
  Two batch clients run at once got about half the single-client rate each.
- `create_rps` 50 / 500 / 2,000 with pools half the size of the workload: same wall
  time (33.3–33.5 s); only time to first instruction p99 moved (9.9 / 8.6 / 8.2 s).

## 4. Tested and not adopted

| Idea | Result |
|---|---|
| Exec on extra event loops in threads | No gain |
| Hand-written minimal WebSocket exec client | No better than aiohttp in a local benchmark |
| TLS session resumption for exec | About 10% of handshake cost in a local benchmark; not adopted |
| Restricting TLS key exchange | Faster, but weakens TLS; rejected |
| Bounded concurrent per-claim deletes ahead of deletecollection | 797–983 vs 854–1,004 sandboxes/s; no gain |
| Separate slot budget for per-claim deletes | No change in wall time |
| Cancelling unused warm-pool refill in sliding windows | Inside run-to-run noise |
| Prewarming more than 64 connections | No measured benefit |

## 5. Known limits at this tip

- Bulk release of claims is slower than concurrent per-claim deletes on the 10-node
  cluster (about 0.7–0.86×) and level on the 20-node one.
- Sliding-window runs are level with `per_claim`: the cycle is set by how fast the
  cluster starts pods (about 50 a second), not by the client.
- Dwell-dominated runs (2 vCPU, 18–33 s tasks) are level.
