# Handoff: SandboxBatch warm-pool check & request budget — problems and recommendations

**Code:** `briankhoi/agent-sandbox` @ `bb5c3fbd`, `clients/python/agentic-sandbox-client/k8s_agent_sandbox/`
(`sandbox_batch.py`, `batch_utils.py`; asyncio twin `async_sandbox_batch.py` must be mirrored). Caller: `examples/agent-sandbox-rl/agent_sandbox_rl/batch_claims.py`. Benchmarks used `create_rps=200`, `max_in_flight=64` (SDK defaults are 50 / 20, `batch_utils.py:74-75`). Line numbers are approximate (`sandbox_batch.py` at `bb5c3fbd`).

## 1. How it works today

- **One pacing cursor per batch** (`_next_create_at`, ~L279); each paced create advances it by `1/create_rps`.
- **One `BoundedSemaphore(max_in_flight)` per batch** (~L284), shared by creates (`_send_create`, ~L2278-2347), pool/template prechecks (`_check_pool`, ~L1184), warm-credit refresh GETs (`_refresh_warm_credit`, ~L1250), and deletes (~L1489).
- **Warm credit, per pool:** the batch's belief of how many Ready warm sandboxes a pool has.
  - Credit available → create skips pacing (fast path).
  - Credit spent + pacing backlog → one `GET SandboxWarmPool` to re-read `readyReplicas`, at most once per `BATCH_WARM_CREDIT_REFRESH_SECONDS = 1.0` per pool, under a per-pool refresh lock (siblings wait), **holding a semaphore slot** (~L1019-1025, ~L1228-1279).
  - Still 0 or refresh not due → create is paced at `create_rps` (~L1028-1047).
- **First acquire on an unchecked pool:** two serial GETs (pool, then template) while holding a slot (~L1148-1207; `_precheck_pools` ~L2449-2477). The template is re-fetched per pool (`checked_templates` is call-local). `bb5c3fbd` pre-opens planned pools via `fleet.setup()` / `start_warmpools(wait=True)` / `warm_images(wait=True)` — **unverified whether the benchmark drivers' `fleet.run(strategy=...)` path takes it.**
- **Intent:** avoid a stampede of cold creates (claims on an empty pool force the controller to build a new sandbox) — the historical issue-1215 over-creation runaway.

## 2. Evidence (Campaign 4 GKE benchmarks, same-cluster per_claim vs batch64 A/B)

- **Batch wins big** where claim/release is the bottleneck: large warm bursts, 5-cluster fleet, bulk release, short-probe multi-step RL (2–5× faster claim/TTFI, 4–56× faster release).
- **Batch ties or loses** where wall time is dominated by pod start (cold nodes, image pull, streaming off, real in-sandbox work). Per-claim wire latency is ~0.5–1 s higher with batch:

  | Case | batch claim P50 | per_claim claim P50 |
  | --- | ---: | ---: |
  | 500 pools × 1, cold | 1.33 s | 0.54 s |
  | 500 × 1 eval | ~1.15 s | ~0.48 s |
  | 8-step RL 500×2, steps 1–7 | 1.3–2.3 s | 0.55–1.07 s |
  | Tunix 64×16 @ 1,024 (env-ready) | 15.5 s | 12.2 s |

- In every losing case batch **drains at a fixed ~145–230 claims/s regardless of pool topology** (1×1000 and 200×5 give identical batch curves); per_claim exceeds 550/s in the same driver → points to a **batch-wide cap**.
- Small bursts show a flat ~0.2 s gap (~2 round trips). The multi-step gap grows after step 0 (0.40 s → 0.54–1.37 s), consistent with warm-credit refreshes once step-0 credit is spent.
- Uncontended batch latency is *lower* than per_claim (0.09–0.13 s vs 0.18–0.25 s), so the single shared watch is not the problem.
- **Counter-evidence for "just raise the cap":** in a 5-cluster pure-SDK test, `max_in_flight=256` + `create_rps=500` was 1.4–2.5× *slower* than 64/200.
- **Unresolved:** the ~200/s drain equals `create_rps=200`. It could be pacing via `exhaust()` (`batch_state.py:344-347`) or 429 cooldown (`batch_utils.py:590-644`), or 64 slots ÷ (requests per claim × RTT). APF rejected counters showed no 429s in the cold 500×1 case; throttle/refresh events are not logged at INFO.

## 3. Problems with the warm-pool check (design critique)

1. **Not a real safety guarantee.** It reads `readyReplicas`, then acts later (time-of-check vs time-of-use); other batches/clients or controller adoption can consume sandboxes in between. The real runaway protections are the controller's expectations/create gate and the SDK overcommit breaker, so the check adds latency for a mostly redundant guarantee.
2. **Polling instead of watching.** Per-pool GETs for state the batch could get from a watch/informer — contrary to batch's goal of fewer API requests, and it scales with the number of pools.
3. **Reads compete with writes.** Precheck and refresh GETs hold the same semaphore slots as creates/deletes → head-of-line blocking of creates.
4. **Worst case is the common RL topology.** Many pools × 1 replica (SWE-bench: 500 pools × 1): credit is almost always 0 or 1, so checks fire on nearly every claim in multi-step runs.
5. **Double throttling.** On an empty pool the client paces claims, while the controller already rate-limits refills (`--sandbox-warm-pool-max-refill-rate`) and the apiserver has APF. Claims are cheap objects; the expensive part (pod creation) is already throttled server-side. Batch slows down exactly when slowing down doesn't help — which matches where it ties/loses.
6. Minor: template re-GET per pool on first check; the 1 s refresh floor can delay credit after a fast refill.

## 4. Recommendations (priority order, keeping the safety intent)

1. **Separate read budget.** Pool/template prechecks and warm-credit refresh GETs must not take `max_in_flight` create slots (own small semaphore, or none, bounded by the HTTP pool). *Expected:* removes head-of-line blocking of creates. *Risk:* none for over-creation; slightly more concurrent reads.
2. **Track ready counts via a watch, not GETs.** One namespace-wide, label-selected watch on `SandboxWarmPool` alongside the existing claim watch; credit = watched `readyReplicas` − creates sent since that resourceVersion. Removes refresh GETs and the 1 s floor, and replenishes credit between RL steps. *Risk:* a stale view over-credits → cold creates skip pacing (issue-1215-style). *Mitigate:* keep `exhaust()` on cold-start signals, keep the 429 cooldown and seq guard, cap credit at min(watched, last confirmed), fall back to today's paced behavior if the watch is unhealthy.
3. **Bulk precheck.** For unplanned pools, one LIST of warm pools (all `readyReplicas`) + a per-handle template cache instead of per-pool GET pairs. Keep `bb5c3fbd`'s pre-open for planned pools and **verify the RL lib actually hits it** in `fleet.run(strategy=...)` paths.
4. **Refresh only when it pays off.** Replace "any backlog triggers a GET" (~L1023) with "pacing wait > ~2× RTT"; coalesce refreshes across pools into one LIST.
5. **Adaptive / opt-out empty-pool client pacing.** E.g. pace only when claims actually back up (pending-claim age, or 429-driven AIMD) and let server-side refill-rate limits and APF do the default throttling. Highest runaway risk — only after (2), with the overcommit breaker and a hard cap always on.
6. **Revisit `max_in_flight` defaults only after (1).** Covered (warm-credit) creates start no pod, so a cap tied to fleet concurrency (e.g. `min(max_concurrent, pool_maxsize-2)`) may be safe for covered creates only. Note the counter-evidence above: raising to 256 alone hurt in one test.

## 5. Validation plan

- **A/B to pin the ~200/s ceiling:** rerun the cold 500×1 SWE-bench claim test with (i) `create_rps=2000, max_in_flight=64` and (ii) `create_rps=200, max_in_flight=256`, with `k8s_agent_sandbox` at DEBUG so throttle/refresh/`exhaust()` events are logged. Whichever moves the drain rate is the cause.
- **After each fix**, rerun on identical clusters, ABABAB, ≥3 reps: (a) 500×1 cold one-shot (claim P50/P99, drain rate), (b) 500×2×8 multi-step RL (per-step claim P50), (c) 1×1000 warm burst (ensure existing big wins don't regress). Also track API request counts (`apiserver_flowcontrol_dispatched_requests_total`), over-creation (peak live sandboxes vs expected), and overcommit-breaker trips.
- Mirror every change sync ↔ async (`sandbox_batch.py` ↔ `async_sandbox_batch.py`) and add unit tests in the existing SDK suite, including a regression test for the over-credit/runaway guard.
