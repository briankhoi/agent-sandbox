# GKE claim-mode bench summary

Steady state is the median over steps 2 to 2 of each run, then the median over runs. Ratios are batch over per-claim: under 1 means batch is faster. Ready is when fleet.acquire returned; start adds the one exec.

## Verdict

```
{
 "w160": {
  "no_limit": {
   "ratio": 0.9142881281972748,
   "call": "inside noise",
   "last_start_ratio": 0.9255654032447946,
   "step1_ratio": 0.9286266427690906
  },
  "defaults": {
   "ratio": 0.8945060588471216,
   "call": "inside noise",
   "last_start_ratio": 0.9009408358585362,
   "step1_ratio": 0.9312640969504468
  },
  "c50": {
   "ratio": 0.8091594479057248,
   "call": "inside noise",
   "last_start_ratio": 0.811659016860435,
   "step1_ratio": 1.2961950780565648
  },
  "wave_unbounded": {
   "ratio": 1.8301007764624875,
   "call": "batch loses",
   "last_start_ratio": 1.8236318945754286,
   "step1_ratio": 1.3968809257941492
  },
  "requests_per_env": {
   "batch": 1.253125,
   "per_claim": 4.0
  },
  "watches": {
   "batch": 0.5,
   "per_claim": 160.0
  },
  "apiserver_create_ms": {
   "batch": 36.985,
   "per_claim": 67.42
  },
  "claim_path_requests": {
   "batch": 531.0,
   "per_claim": 1022.0
  },
  "apf_wait_s": {
   "batch": 0.4255,
   "per_claim": 21.104
  },
  "pool_full_discards": {
   "batch": 0.0,
   "per_claim": 0.0
  },
  "tail_p99_ratio": 0.9830891641290159
 },
 "w640": {
  "no_limit": {
   "ratio": 0.7196941334991064,
   "call": "inside noise",
   "last_start_ratio": 0.7268708018597008,
   "step1_ratio": 1.0029986118479983
  },
  "defaults": {
   "ratio": 0.6534652386146075,
   "call": "batch wins",
   "last_start_ratio": 0.6741211345794821,
   "step1_ratio": 0.8133015634366829
  },
  "wave_unbounded": {
   "ratio": 1.2864669814723138,
   "call": "batch loses",
   "last_start_ratio": 1.2816851797765132,
   "step1_ratio": 1.158188730671176,
   "gain_over_20pct": false
  },
  "requests_per_env": {
   "batch": 1.25,
   "per_claim": 4.0
  },
  "watches": {
   "batch": 0.0,
   "per_claim": 640.0
  },
  "apiserver_create_ms": {
   "batch": 119.39000000000001,
   "per_claim": 117.45
  },
  "claim_path_requests": {
   "batch": 3677.5,
   "per_claim": 5861.5
  },
  "apf_wait_s": {
   "batch": 21.3,
   "per_claim": 39.666
  },
  "pool_full_discards": {
   "batch": 0.0,
   "per_claim": 0.0
  },
  "tail_p99_ratio": 0.9557629730666549
 },
 "w1280": {
  "no_limit": {
   "ratio": 0.5965873306708998,
   "call": "batch wins",
   "last_start_ratio": 0.5990657478372562,
   "step1_ratio": 1.3195886158100065
  },
  "defaults": {
   "ratio": 0.6090961063459674,
   "call": "batch wins",
   "last_start_ratio": 0.6105100190104908,
   "step1_ratio": 0.9845090031347643
  },
  "wave_unbounded": {
   "ratio": 1.2956889014266018,
   "call": "batch loses",
   "last_start_ratio": 1.2912569373118434,
   "step1_ratio": 0.6963669931419663,
   "gain_over_20pct": false
  },
  "requests_per_env": {
   "batch": 1.3265625,
   "per_claim": 4.0
  },
  "watches": {
   "batch": 1.0,
   "per_claim": 1280.0
  },
  "apiserver_create_ms": {
   "batch": 164.945,
   "per_claim": 247.26999999999998
  },
  "claim_path_requests": {
   "batch": 6634.0,
   "per_claim": 10609.5
  },
  "apf_wait_s": {
   "batch": 163.7765,
   "per_claim": 153.058
  },
  "pool_full_discards": {
   "batch": 0.0,
   "per_claim": 218.5
  },
  "tail_p99_ratio": 0.5957060183832061
 },
 "c640": {
  "no_limit": {
   "ratio": 0.9351106429636706,
   "call": "inside noise",
   "last_start_ratio": 0.9353585147322886,
   "step1_ratio": 0.9705959339161501
  },
  "requests_per_env": {
   "batch": 1.2515625,
   "per_claim": 4.0
  },
  "watches": {
   "batch": 1.0,
   "per_claim": 640.0
  },
  "apiserver_create_ms": {
   "batch": 165.065,
   "per_claim": 131.07
  },
  "claim_path_requests": {
   "batch": 4765.0,
   "per_claim": 6770.5
  },
  "apf_wait_s": {
   "batch": 127.0395,
   "per_claim": 45.937
  },
  "pool_full_discards": {
   "batch": 0.0,
   "per_claim": 0.0
  },
  "tail_p99_ratio": 0.9669398780768649
 },
 "warm_ratio_by_size": [
  [
   160,
   0.9142881281972748
  ],
  [
   640,
   0.7196941334991064
  ],
  [
   1280,
   0.5965873306708998
  ]
 ],
 "win_grows_with_size": true
}
```

## Steady state

| shape/variant | runs | errors | last Ready s (min-max) | Ready p50 s | Ready p99 s | last start s | step 1 last Ready s | requests/env | watches | pool-full discards | CPU s | API create ms | APF wait s | API claim-path requests | all API requests per s (context) | 429s |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| w160/batch-c50 | 2 | 0 | 4.24 (2.69-5.78) | 1.27 | 2.89 | 4.34 | 3.09 | 1.25 | 0 | 0 | 1.20 | 27.15 | 4.32 | 1100 | 1005 | 0 |
| w160/batch-default | 2 | 0 | 2.12 (1.94-2.29) | 1.12 | 2.10 | 2.20 | 2.34 | 1.26 | 1 | 0 | 1.17 | 23.77 | 0.18 | 540 | 727 | 0 |
| w160/batch-unbounded | 2 | 0 | 2.16 (2.07-2.25) | 1.29 | 2.14 | 2.26 | 2.34 | 1.25 | 0 | 0 | 1.14 | 36.98 | 0.43 | 531 | 697 | 0 |
| w160/fill-unbounded | 2 | 0 | 3.96 (3.12-4.79) | 2.87 | 3.91 | 4.12 | 3.26 | 1.52 | 1 | 0 | 1.16 | 41.09 | 8.78 | 917 | 709 | 0 |
| w160/per-claim | 2 | 0 | 2.36 (1.86-2.86) | 1.28 | 2.17 | 2.44 | 2.52 | 4.00 | 160 | 0 | 2.38 | 67.42 | 21.10 | 1022 | 914 | 0 |
| w160/per-claim-c50 | 2 | 0 | 5.23 (2.60-7.87) | 1.23 | 3.22 | 5.35 | 2.38 | 4.00 | 160 | 0 | 1.89 | 38.08 | 8.38 | 1622 | 1056 | 0 |
| w640/batch-default | 2 | 0 | 9.39 (9.35-9.43) | 5.17 | 9.26 | 9.75 | 8.62 | 1.25 | 1 | 0 | 5.10 | 42.03 | 22.33 | 3404 | 1061 | 0 |
| w640/batch-unbounded | 2 | 0 | 10.34 (10.29-10.38) | 5.38 | 10.14 | 10.51 | 10.64 | 1.25 | 0 | 0 | 5.96 | 119.39 | 21.30 | 3678 | 1017 | 0 |
| w640/fill-unbounded | 2 | 0 | 13.30 (12.81-13.79) | 7.63 | 13.13 | 13.47 | 12.32 | 1.50 | 1 | 0 | 4.71 | 70.12 | 38.14 | 3294 | 820 | 0 |
| w640/per-claim | 2 | 0 | 14.37 (10.27-18.46) | 5.49 | 10.60 | 14.46 | 10.60 | 4.00 | 640 | 0 | 14.60 | 117.45 | 39.67 | 5862 | 1078 | 0 |
| w1280/batch-default | 2 | 0 | 19.19 (17.65-20.74) | 9.76 | 19.02 | 19.30 | 29.04 | 1.25 | 1 | 0 | 10.97 | 35.69 | 28.18 | 6719 | 1027 | 0 |
| w1280/batch-unbounded | 2 | 0 | 18.80 (17.48-20.11) | 9.82 | 18.58 | 18.94 | 38.93 | 1.33 | 1 | 0 | 12.53 | 164.94 | 163.78 | 6634 | 1033 | 100 |
| w1280/fill-unbounded | 2 | 0 | 24.36 (23.92-24.79) | 15.46 | 24.07 | 24.45 | 27.11 | 1.50 | 1 | 0 | 9.94 | 110.75 | 95.36 | 6923 | 812 | 0 |
| w1280/per-claim | 2 | 0 | 31.51 (23.33-39.69) | 20.91 | 31.19 | 31.61 | 29.50 | 4.00 | 1280 | 218 | 105.66 | 247.27 | 153.06 | 10610 | 798 | 0 |
| c640/batch-unbounded | 2 | 0 | 25.28 (25.20-25.36) | 16.17 | 24.55 | 25.36 | 25.01 | 1.25 | 1 | 0 | 6.21 | 165.06 | 127.04 | 4765 | 962 | 0 |
| c640/per-claim | 2 | 0 | 27.04 (25.47-28.60) | 16.92 | 25.39 | 27.11 | 25.77 | 4.00 | 640 | 0 | 12.03 | 131.07 | 45.94 | 6770 | 945 | 0 |

Requests and watches are the driver's own, from the step's start to its last exec, before release. API columns are the API server's counters over the same window, from steps whose claim creates matched the driver's. Claim-path requests are every sandboxclaims request and the sandboxes reads, from every client (the controller included), the arm's API cost; sandbox writes (warm-pool refills) and leases (node heartbeats) are left out. All API requests per second covers every client in the cluster (kubelets and refills too), so it leans against the slower arm: context only.
