# claim-mode bench summary

## Verdict

```
{
 "per_claim_last_start": 3.94208712503314,
 "batch_default_ratio": 0.327738697567125,
 "best_batch": {
  "variant": "batch-default",
  "ratio": 0.327738697567125
 },
 "best_fill": null,
 "failures_all_pass": false,
 "per_claim_errors": 0,
 "batch_c20_vs_per_claim_c20": 0.7844109912127011,
 "batch_c50_vs_per_claim_c50": 0.6074555814840333,
 "ship_pr1": false,
 "build_prefetch": false,
 "prefetch_note": "measured at 160 sandboxes only; the decision doc's rule asks for 640 or more, which one kind node can't hold"
}
```

## Burst (one step, 40 prompts x 4)

| variant | runs | errors | last start p50 (min-max) s | first group p50 s | last group p50 s | Ready p50 s | last Ready s | create window s | requests | watches | creates/s | 429s | throttled | CPU s |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| per-claim | 3 | 0 | 3.94 (3.32-4.06) | 3.46 | 3.94 | 2.93 | 3.60 | 0.80 | 640 | 160 | 198 | 0 | 0 | 5.46 |
| per-claim-c20 | 3 | 0 | 1.87 (1.86-1.88) | 0.64 | 1.87 | 1.12 | 1.79 | 1.33 | 640 | 160 | 120 | 0 | 0 | 1.81 |
| per-claim-c50 | 3 | 0 | 2.20 (2.05-2.25) | 1.47 | 2.20 | 1.53 | 2.10 | 1.00 | 640 | 160 | 160 | 0 | 0 | 2.68 |
| batch-default | 3 | 0 | 1.29 (1.29-1.29) | 0.45 | 1.29 | 0.63 | 1.14 | 0.49 | 461 | 1 | 323 | 0 | 0 | 1.44 |
| batch-c20 | 3 | 0 | 1.46 (1.44-1.49) | 0.28 | 1.46 | 0.70 | 1.35 | 1.20 | 351 | 1 | 133 | 0 | 0 | 1.18 |
| batch-c50 | 3 | 0 | 1.34 (1.29-1.37) | 0.38 | 1.34 | 0.67 | 1.16 | 0.82 | 455 | 1 | 193 | 0 | 0 | 1.33 |

## Rolling

| variant | run | rollouts/min | acquire p50/p90/p99 s | requests/rollout | watches | min warm Ready | starved | 429s | throttled | errors |
|---|---|---|---|---|---|---|---|---|---|---|

## Lease writes

| variant | workers | rollouts | Lease writes per process per hour (measured / analytic) | requests/rollout | errors |
|---|---|---|---|---|---|

## Composition, failures and the long run

| run | pass | details |
|---|---|---|

Not measured here: snapshot suspend/resume/restore on a batch member (needs GKE Pod Snapshots); the async fleet (sync only); connections; the cold and throttled create path (every claim is warm-covered, so create_rps only applies after a 429); scale past 160. Rolling holds are lognormal, median 15 s and p99 about 150 s, capped at 150 s, shorter than the decision doc's 60 s median so that 240 s runs hold several rollouts per env. The SIGKILL reaper is a 5 s loop in the driver, not the CronJob, so claims_gone_after_kill_s leaves out the CronJob's schedule interval.
