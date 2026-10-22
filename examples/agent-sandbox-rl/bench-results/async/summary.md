# claim-mode bench summary

## Verdict

```
{
 "per_claim_last_start": 3.8768490829970688,
 "batch_default_ratio": 0.3279137402127438,
 "best_batch": {
  "variant": "batch-default",
  "ratio": 0.3279137402127438
 },
 "best_fill": null,
 "failures_all_pass": false,
 "per_claim_errors": 0,
 "batch_c20_vs_per_claim_c20": 0.7557643743813249,
 "batch_c50_vs_per_claim_c50": 0.6472567045212178,
 "async_default_ratio": 0.7527302848394244,
 "async_c20_vs_async_per_claim_c20": 0.8381666184120672,
 "async_c50_vs_async_per_claim_c50": 0.918415899166064,
 "async_pool64_ratio": 0.6851863528713641,
 "async_batch_vs_sync_batch": 1.0371017153658164,
 "async_per_claim_vs_sync_per_claim": 0.4517951639732478,
 "async_checks": {
  "async_batch_within_tolerance_of_sync_batch": true,
  "async_ratio_within_tolerance_of_sync_ratio": false,
  "async_batch_faster_than_async_per_claim": true,
  "async_c20_batch_faster": true,
  "async_c50_batch_faster": true,
  "async_pool64_batch_faster": true,
  "default_pool_not_binding": true,
  "no_loop_stall_over_100ms": false,
  "no_errors_or_leaked_threads": true
 },
 "ship_pr1": false,
 "build_prefetch": false,
 "prefetch_note": "measured at 160 sandboxes only; the decision doc's rule asks for 640 or more, which one kind node can't hold"
}
```

## Burst (one step, 40 prompts x 4)

| variant | runs | errors | last start p50 (min-max) s | first group p50 s | last group p50 s | Ready p50 s | last Ready s | create window s | requests | watches | creates/s | 429s | throttled | CPU s |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| per-claim | 3 | 0 | 3.88 (3.83-4.09) | 3.46 | 3.88 | 2.99 | 3.80 | 0.81 | 640 | 160 | 195 | 0 | 0 | 5.49 |
| per-claim-c20 | 3 | 0 | 1.93 (1.85-1.98) | 0.69 | 1.93 | 1.10 | 1.84 | 1.35 | 640 | 160 | 117 | 0 | 0 | 1.88 |
| per-claim-c50 | 3 | 0 | 2.03 (2.00-2.19) | 1.14 | 2.03 | 1.35 | 1.90 | 1.03 | 640 | 160 | 155 | 0 | 0 | 2.47 |
| batch-default | 3 | 0 | 1.27 (1.26-1.28) | 0.42 | 1.27 | 0.60 | 1.08 | 0.32 | 243 | 1 | 502 | 0 | 0 | 1.36 |
| batch-c20 | 3 | 0 | 1.46 (1.44-1.50) | 0.18 | 1.46 | 0.72 | 1.37 | 1.19 | 243 | 1 | 134 | 0 | 0 | 1.12 |
| batch-c50 | 3 | 0 | 1.32 (1.28-1.35) | 0.54 | 1.32 | 0.56 | 1.20 | 0.80 | 243 | 1 | 198 | 0 | 0 | 1.20 |
| async-per-claim | 3 | 0 | 1.75 (1.62-1.77) | 1.09 | 1.75 | 0.82 | 1.40 | 0.41 | 640 | 160 | 386 | 0 | 0 | 2.11 |
| async-per-claim-c20 | 3 | 0 | 1.81 (1.71-1.83) | 0.56 | 1.81 | 0.97 | 1.68 | 1.39 | 640 | 160 | 114 | 0 | 0 | 1.74 |
| async-per-claim-c50 | 3 | 0 | 1.54 (1.49-1.60) | 0.56 | 1.54 | 0.81 | 1.37 | 0.91 | 640 | 160 | 176 | 0 | 0 | 1.93 |
| async-batch-default | 3 | 0 | 1.32 (1.32-1.37) | 0.33 | 1.32 | 0.62 | 1.17 | 0.28 | 243 | 1 | 573 | 0 | 0 | 1.39 |
| async-batch-c20 | 3 | 0 | 1.52 (1.51-1.52) | 0.22 | 1.52 | 0.72 | 1.41 | 1.24 | 243 | 1 | 129 | 0 | 0 | 1.11 |
| async-batch-c50 | 3 | 0 | 1.42 (1.27-1.71) | 0.35 | 1.42 | 0.67 | 1.22 | 0.85 | 243 | 1 | 188 | 0 | 0 | 1.20 |
| async-per-claim-pool64 | 3 | 0 | 1.94 (1.55-1.94) | 0.83 | 1.94 | 0.86 | 1.41 | 0.82 | 640 | 160 | 193 | 0 | 0 | 2.06 |
| async-batch-pool64 | 3 | 0 | 1.33 (1.31-1.45) | 0.29 | 1.33 | 0.64 | 1.20 | 0.74 | 243 | 1 | 214 | 0 | 0 | 1.26 |

## Async path

| variant | thread pool workers | loop lag p99 s (median of runs) | loop lag max s | leaked pool threads |
|---|---|---|---|---|
| async-per-claim | 496 | 0.054 | 0.162 | 0 |
| async-per-claim-c20 | 496 | 0.014 | 0.081 | 0 |
| async-per-claim-c50 | 496 | 0.048 | 0.075 | 0 |
| async-batch-default | 496 | 0.052 | 0.105 | 0 |
| async-batch-c20 | 496 | 0.010 | 0.087 | 0 |
| async-batch-c50 | 496 | 0.030 | 0.126 | 0 |
| async-per-claim-pool64 | 64 | 0.058 | 0.191 | 0 |
| async-batch-pool64 | 64 | 0.028 | 0.049 | 0 |

## Rolling

| variant | run | rollouts/min | acquire p50/p90/p99 s | requests/rollout | watches | min warm Ready | starved | 429s | throttled | errors |
|---|---|---|---|---|---|---|---|---|---|---|

## Lease writes

| variant | workers | rollouts | Lease writes per process per hour (measured / analytic) | requests/rollout | errors |
|---|---|---|---|---|---|

## Composition, failures and the long run

| run | pass | details |
|---|---|---|

Not measured here: snapshot suspend/resume/restore on a batch member (needs GKE Pod Snapshots); the async fleet's rolling and recycle paths (the async arms are bursts only); connections; the cold and throttled create path (every claim is warm-covered, so create_rps only applies after a 429); scale past 160. Rolling holds are lognormal, median 15 s and p99 about 150 s, capped at 150 s, shorter than the decision doc's 60 s median so that 240 s runs hold several rollouts per env. The SIGKILL reaper is a 5 s loop in the driver, not the CronJob, so claims_gone_after_kill_s leaves out the CronJob's schedule interval.
