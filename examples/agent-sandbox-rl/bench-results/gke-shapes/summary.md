# GKE claim-mode bench summary

Steady state is the median over steps 2 to 2 of each run, then the median over runs. Ratios are batch over per-claim: under 1 means batch is faster. Ready is when fleet.acquire returned; start adds the one exec.

## Verdict

```
{
 "w500": {
  "no_limit": {
   "ratio": 0.9640070488364567,
   "call": "inside noise",
   "last_start_ratio": 0.9966450809657095,
   "step1_ratio": 0.2014264350758666
  },
  "defaults": {
   "ratio": 1.047576813028055,
   "call": "inside noise",
   "last_start_ratio": 1.0465790492114078,
   "step1_ratio": 0.23738733113532096
  },
  "requests_per_env": {
   "batch": 2.011,
   "per_claim": 4.0
  },
  "watches": {
   "batch": 4.5,
   "per_claim": 500.0
  },
  "apiserver_create_ms": {
   "batch": 137.605,
   "per_claim": 186.62
  },
  "claim_path_requests": {
   "batch": 2342.5,
   "per_claim": 3844.0
  },
  "apf_wait_s": {
   "batch": 397.128,
   "per_claim": 507.24799999999993
  },
  "pool_full_discards": {
   "batch": 0.0,
   "per_claim": 0.0
  },
  "tail_p99_ratio": 0.9966037550234083
 },
 "w1024": {
  "no_limit": {
   "ratio": 0.9047396709178885,
   "call": null,
   "last_start_ratio": 0.90646854617276,
   "step1_ratio": 1.1021672337394868
  },
  "defaults": {
   "ratio": 0.95779115372783,
   "call": null,
   "last_start_ratio": 0.9571905998583872,
   "step1_ratio": 0.8896203565756466
  },
  "requests_per_env": {
   "batch": 1.0185546875,
   "per_claim": 3.998046875
  },
  "watches": {
   "batch": 3.0,
   "per_claim": 1023.0
  },
  "apiserver_create_ms": {
   "batch": 263.39,
   "per_claim": 315.12
  },
  "claim_path_requests": {
   "batch": 4315.5,
   "per_claim": 8675.5
  },
  "apf_wait_s": {
   "batch": 935.9885,
   "per_claim": 1751.6125
  },
  "pool_full_discards": {
   "batch": 0.0,
   "per_claim": 24.0
  },
  "tail_p99_ratio": 0.753566571258203
 },
 "warm_ratio_by_size": [
  [
   500,
   0.9640070488364567
  ],
  [
   1024,
   0.9047396709178885
  ]
 ],
 "win_grows_with_size": true
}
```

## Steady state

| shape/variant | runs | errors | last Ready s (min-max) | Ready p50 s | Ready p99 s | last start s | step 1 last Ready s | requests/env | watches | pool-full discards | CPU s | API create ms | APF wait s | API claim-path requests | all API requests per s (context) | 429s |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| w500/batch-default | 2 | 0 | 7.62 (7.45-7.79) | 3.71 | 7.46 | 7.72 | 7.95 | 2.01 | 4 | 0 | 4.41 | 81.38 | 428.05 | 2294 | 1030 | 0 |
| w500/batch-unbounded | 2 | 0 | 7.01 (6.95-7.07) | 4.40 | 6.91 | 7.35 | 6.75 | 2.01 | 4 | 0 | 5.12 | 137.60 | 397.13 | 2342 | 1058 | 0 |
| w500/per-claim | 2 | 0 | 7.27 (7.03-7.51) | 4.05 | 6.93 | 7.38 | 33.51 | 4.00 | 500 | 0 | 8.51 | 186.62 | 507.25 | 3844 | 1110 | 0 |
| w1024/batch-default | 2 | 0 | 35.82 (35.72-35.91) | 7.05 | 31.74 | 35.91 | 31.08 | 1.02 | 1 | 0 | 7.79 | 85.37 | 675.79 | 4555 | 631 | 58 |
| w1024/batch-unbounded | 2 | 0 | 33.83 (32.03-35.63) | 7.09 | 24.42 | 34.01 | 38.50 | 1.02 | 3 | 0 | 8.90 | 263.39 | 935.99 | 4316 | 714 | 41 |
| w1024/per-claim | 2 | 101 | 37.39 (30.48-44.30) | 8.73 | 32.41 | 37.52 | 34.93 | 4.00 | 1023 | 24 | 27.02 | 315.12 | 1751.61 | 8676 | 686 | 70 |

Requests and watches are the driver's own, from the step's start to its last exec, before release. API columns are the API server's counters over the same window, from steps whose claim creates matched the driver's. Claim-path requests are every sandboxclaims request and the sandboxes reads, from every client (the controller included), the arm's API cost; sandbox writes (warm-pool refills) and leases (node heartbeats) are left out. All API requests per second covers every client in the cluster (kubelets and refills too), so it leans against the slower arm: context only.
