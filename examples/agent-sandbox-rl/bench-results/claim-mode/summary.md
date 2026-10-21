# claim-mode bench summary

## Verdict

```
{
 "per_claim_last_start": 4.102685832884163,
 "batch_default_ratio": 0.3216357861188877,
 "best_batch": {
  "variant": "batch-mif50",
  "ratio": 0.3177076074109811
 },
 "best_fill": {
  "variant": "fill-mif50",
  "ratio": 0.3073167821045347
 },
 "failures_all_pass": true,
 "per_claim_errors": 0,
 "batch_default_vs_per_claim_c20": 0.6980725647204207,
 "batch_mif50_vs_per_claim_c50": 0.57779548836402,
 "ship_pr1": true,
 "build_prefetch": false,
 "prefetch_note": "measured at 160 sandboxes only; the decision doc's rule asks for 640 or more, which one kind node can't hold"
}
```

## Burst (one step, 40 prompts x 4)

| variant | runs | errors | last start p50 (min-max) s | first group p50 s | last group p50 s | Ready p50 s | last Ready s | create window s | requests | watches | creates/s | 429s | throttled | CPU s |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| per-claim | 3 | 0 | 4.10 (3.66-4.53) | 3.58 | 4.10 | 3.23 | 3.67 | 0.80 | 640 | 160 | 200 | 0 | 0 | 5.73 |
| per-claim-c20 | 3 | 0 | 1.89 (1.82-1.90) | 0.70 | 1.89 | 1.13 | 1.80 | 1.30 | 640 | 160 | 122 | 0 | 0 | 1.89 |
| per-claim-c50 | 3 | 0 | 2.26 (2.10-2.35) | 1.47 | 2.26 | 1.51 | 2.04 | 1.05 | 640 | 160 | 152 | 0 | 0 | 2.81 |
| batch-default | 3 | 0 | 1.32 (1.27-1.34) | 0.28 | 1.32 | 0.60 | 1.12 | 0.50 | 473 | 1 | 318 | 0 | 0 | 1.47 |
| batch-mif50 | 3 | 0 | 1.30 (1.27-1.34) | 0.32 | 1.30 | 0.59 | 1.10 | 0.45 | 467 | 1 | 352 | 0 | 0 | 1.45 |
| batch-mif100 | 3 | 0 | 1.41 (1.32-1.47) | 0.30 | 1.41 | 0.69 | 1.20 | 0.43 | 473 | 1 | 366 | 0 | 0 | 1.44 |
| batch-mif200 | 3 | 0 | 1.35 (1.33-1.38) | 0.53 | 1.35 | 0.67 | 1.17 | 0.48 | 463 | 1 | 328 | 0 | 0 | 1.42 |
| fill-default | 3 | 0 | 1.28 (1.26-1.30) | 0.56 | 1.28 | 0.53 | 1.08 | 0.15 | 243 | 1 | 1054 | 0 | 0 | 0.92 |
| fill-mif50 | 3 | 0 | 1.26 (1.24-1.28) | 0.70 | 1.26 | 0.54 | 1.11 | 0.09 | 243 | 1 | 1717 | 0 | 0 | 0.98 |

## Rolling

| variant | run | rollouts/min | acquire p50/p90/p99 s | requests/rollout | watches | min warm Ready | starved | 429s | throttled | errors |
|---|---|---|---|---|---|---|---|---|---|---|
| per-claim | 1 | 118.2 | 0.04/0.07/0.24 | 6.01 | 473 | 78 | False | 0 | 0 | 0 |
| batch-default | 1 | 118.2 | 0.03/0.05/0.26 | 3.31 | 8 | 76 | False | 0 | 0 | 0 |
| batch-mif50 | 1 | 118.2 | 0.03/0.05/0.30 | 3.31 | 8 | 76 | False | 0 | 0 | 0 |
| batch-default | 2 | 117.0 | 0.03/0.06/0.22 | 3.32 | 8 | 74 | False | 0 | 0 | 0 |
| batch-mif50 | 2 | 117.0 | 0.03/0.05/0.23 | 3.32 | 8 | 75 | False | 0 | 0 | 0 |
| per-claim | 2 | 117.0 | 0.04/0.08/0.27 | 6.03 | 468 | 75 | False | 0 | 0 | 0 |

## Lease writes

| variant | workers | rollouts | Lease writes per process per hour (measured / analytic) | requests/rollout | errors |
|---|---|---|---|---|---|
| lease60 | 8 | 2159 | 179 / 180 | 2.47 | 0 |
| lease300 | 8 | 1997 | 34 / 36 | 2.39 | 0 |

## Composition, failures and the long run

| run | pass | details |
|---|---|---|
| comp-recycle | True | `{"case": "recycle", "warm_wait_s": 7.128714583115652, "modes": {"per_claim": {"tasks": 16, "ok": 16, "creates": 4}, "batch": {"tasks": 16, "ok": 16, "creates": 4}}, "pass": true, "total_requests": 42, "watches": 5, "http_429": 0, "creates": 8, "create_window_s": 0.26473191613331437, "creates_per_s": 26.441843893408464, "wall_s": 7.841902732849121}` |
| comp-ondemand | True | `{"case": "ondemand", "warm_wait_s": 7.123578791040927, "on_demand_member": true, "on_demand_pool_left": true, "pass": true, "total_requests": 16, "watches": 1, "http_429": 0, "creates": 1, "wall_s": 8.531160116195679}` |
| comp-adopt | True | `{"case": "adopt", "warm_wait_s": 9.11887437500991, "adopted_acquires": 4, "pass": true, "total_requests": 20, "watches": 1, "http_429": 0, "creates": 4, "create_window_s": 0.10852604196406901, "creates_per_s": 27.643134732521116, "wall_s": 9.606207132339478}` |
| comp-maxlive | True | `{"case": "maxlive", "warm_wait_s": 7.0828104158863425, "fourth": "FleetOvercommitError", "creates_for_fourth": 0, "pass": true, "total_requests": 14, "watches": 1, "http_429": 0, "creates": 3, "create_window_s": 0.06207425007596612, "creates_per_s": 32.21947905214177, "wall_s": 7.484575033187866}` |
| comp-pin | True | `{"case": "pin", "warm_wait_s": 7.083572917152196, "per_claim_fallback": true, "pass": true, "total_requests": 5, "watches": 1, "http_429": 0, "creates": 1, "wall_s": 7.369387149810791}` |
| fail-sigkill-reaper | True | `{"case": "sigkill-reaper", "lease_duration": 30, "members": 20, "warm_wait_s": 7.067881166934967, "claims_before": 20, "claims_gone_after_kill_s": 45.63349216617644, "reaped": ["b47il9b8gaz9"], "pass": true, "wall_s": 54.07150101661682}` |
| fail-sigkill-noreaper | True | `{"case": "sigkill-noreaper", "lease_duration": 30, "members": 20, "warm_wait_s": 5.027884291950613, "claims_before": 20, "claims_after_lease_plus_30s": 20, "claims_gone_after_kill_s": 71.16988770803437, "reaped": ["bcmff9qqtu41"], "pass": true, "wall_s": 77.54160714149475}` |
| fail-stall | True | `{"case": "stall", "lease_duration": 30, "members": 20, "warm_wait_s": 5.037788667017594, "claims_while_stopped": 0, "reaped": ["bqy5r867g0hg"], "pass": true, "claims_left_s": 0.004400582984089851, "wall_s": 71.8601188659668}` |
| fail-partition | True | `{"case": "partition", "lease_duration": 30, "members": 20, "warm_wait_s": 7.080235790926963, "reaped": ["bck2rzrpnyvg"], "pass": true, "claims_left_s": 0.005832624854519963, "wall_s": 70.74769902229309}` |
| fail-member-deleted | True | `{"case": "member-deleted", "lease_duration": 30, "members": 20, "warm_wait_s": 7.0855829999782145, "member_state": "lost", "exec_error": null, "release_error": null, "others_ok": 3, "pass": true, "wall_s": 7.586412191390991}` |
| long-recycle-expiry | True | `{"case": "recycle-expiry", "tasks": 13, "task_s": 60, "ok": 13, "failures": [], "claims": 2, "warm_wait_s": 7.072642666986212, "pass": true, "wall_s": 788.3175737857819}` |

Not measured here: snapshot suspend/resume/restore on a batch member (needs GKE Pod Snapshots); the async fleet (sync only); connections; the cold and throttled create path (every claim is warm-covered, so create_rps only applies after a 429); scale past 160. Rolling holds are lognormal, median 15 s and p99 about 150 s, capped at 150 s, shorter than the decision doc's 60 s median so that 240 s runs hold several rollouts per env. The SIGKILL reaper is a 5 s loop in the driver, not the CronJob, so claims_gone_after_kill_s leaves out the CronJob's schedule interval.
