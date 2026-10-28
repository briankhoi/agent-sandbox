# GKE claim-mode bench summary

Steady state is the median over steps 2 to 2 of each run, then the median over runs. Ratios are batch over per-claim: under 1 means batch is faster. Ready is when fleet.acquire returned; start adds the one exec.

## Verdict

```
{
 "w160": {
  "no_limit": {
   "ratio": 0.963379836307535,
   "call": "inside noise",
   "last_start_ratio": 0.9489974995637261,
   "step1_ratio": 0.9538140623788951
  },
  "defaults": {
   "ratio": 0.9463070253724523,
   "call": "inside noise",
   "last_start_ratio": 0.9634003944038319,
   "step1_ratio": 1.061752065046668
  },
  "requests_per_env": {
   "batch": 1.25625,
   "per_claim": 4.0
  },
  "watches": {
   "batch": 1.0,
   "per_claim": 160.0
  },
  "apiserver_create_ms": {
   "batch": 58.085,
   "per_claim": 87.805
  },
  "claim_path_requests": {
   "batch": 742.5,
   "per_claim": 1224.0
  },
  "apf_wait_s": {
   "batch": 10.674000000000001,
   "per_claim": 22.238
  },
  "pool_full_discards": {
   "batch": 0.0,
   "per_claim": 0.0
  },
  "tail_p99_ratio": 0.9190277008907901
 },
 "w640": {
  "no_limit": {
   "ratio": 0.9700020778515218,
   "call": "inside noise",
   "last_start_ratio": 0.9698296538982988,
   "step1_ratio": 0.8317128665925884
  },
  "defaults": {
   "ratio": 0.4468737288421495,
   "call": "batch wins",
   "last_start_ratio": 0.45441111784449534,
   "step1_ratio": 0.42679691573884904
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
   "batch": 127.745,
   "per_claim": 132.97
  },
  "claim_path_requests": {
   "batch": 2805.5,
   "per_claim": 4735.0
  },
  "apf_wait_s": {
   "batch": 506.6275,
   "per_claim": 698.945
  },
  "pool_full_discards": {
   "batch": 0.0,
   "per_claim": 0.0
  },
  "tail_p99_ratio": 0.9665133147624568
 },
 "w1280": {
  "no_limit": {
   "ratio": 1.0215289236144027,
   "call": "inside noise",
   "last_start_ratio": 1.0220995097246006,
   "step1_ratio": 1.1678124511632229
  },
  "defaults": {
   "ratio": 1.2474731430593178,
   "call": "inside noise",
   "last_start_ratio": 1.2516084647958086,
   "step1_ratio": 1.4267663695371888
  },
  "requests_per_env": {
   "batch": 1.25078125,
   "per_claim": 4.0
  },
  "watches": {
   "batch": 1.0,
   "per_claim": 1280.0
  },
  "apiserver_create_ms": {
   "batch": 257.355,
   "per_claim": 156.155
  },
  "claim_path_requests": {
   "batch": 5502.5,
   "per_claim": 9387.5
  },
  "apf_wait_s": {
   "batch": 970.835,
   "per_claim": 949.0065
  },
  "pool_full_discards": {
   "batch": 0.0,
   "per_claim": 0.0
  },
  "tail_p99_ratio": 0.9778181740702154
 },
 "warm_ratio_by_size": [
  [
   160,
   0.963379836307535
  ],
  [
   640,
   0.9700020778515218
  ],
  [
   1280,
   1.0215289236144027
  ]
 ],
 "win_grows_with_size": false
}
```

## Steady state

| shape/variant | runs | errors | last Ready s (min-max) | Ready p50 s | Ready p99 s | last start s | step 1 last Ready s | requests/env | watches | pool-full discards | CPU s | API create ms | APF wait s | API claim-path requests | all API requests per s (context) | 429s |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| w160/batch-default | 2 | 0 | 2.34 (2.32-2.37) | 1.18 | 2.33 | 2.52 | 2.54 | 1.26 | 1 | 0 | 1.17 | 45.41 | 4.54 | 747 | 1006 | 0 |
| w160/batch-unbounded | 2 | 0 | 2.38 (2.37-2.40) | 1.15 | 2.24 | 2.48 | 2.28 | 1.26 | 1 | 0 | 1.06 | 58.09 | 10.67 | 742 | 981 | 0 |
| w160/per-claim | 2 | 0 | 2.48 (2.43-2.53) | 1.37 | 2.44 | 2.61 | 2.39 | 4.00 | 160 | 0 | 2.06 | 87.81 | 22.24 | 1224 | 1130 | 0 |
| w640/batch-default | 2 | 0 | 8.73 (8.68-8.78) | 4.78 | 8.50 | 8.92 | 8.92 | 1.25 | 1 | 0 | 6.29 | 75.38 | 497.75 | 2840 | 967 | 0 |
| w640/batch-unbounded | 2 | 0 | 18.95 (8.23-29.68) | 4.81 | 18.65 | 19.05 | 17.38 | 1.25 | 1 | 0 | 7.19 | 127.75 | 506.63 | 2806 | 674 | 10 |
| w640/per-claim | 2 | 0 | 19.54 (9.71-29.37) | 5.36 | 19.30 | 19.64 | 20.90 | 4.00 | 640 | 0 | 15.84 | 132.97 | 698.95 | 4735 | 811 | 50 |
| w1280/batch-default | 2 | 0 | 24.59 (18.57-30.61) | 10.20 | 24.04 | 24.85 | 33.75 | 1.25 | 2 | 0 | 17.69 | 84.90 | 847.88 | 5529 | 857 | 22 |
| w1280/batch-unbounded | 2 | 0 | 20.14 (16.86-23.41) | 8.47 | 19.16 | 20.29 | 27.62 | 1.25 | 1 | 0 | 11.26 | 257.36 | 970.84 | 5502 | 928 | 50 |
| w1280/per-claim | 2 | 0 | 19.71 (18.47-20.96) | 9.18 | 19.60 | 19.85 | 23.65 | 4.00 | 1280 | 0 | 24.64 | 156.16 | 949.01 | 9388 | 1090 | 98 |

Requests and watches are the driver's own, from the step's start to its last exec, before release. API columns are the API server's counters over the same window, from steps whose claim creates matched the driver's. Claim-path requests are every sandboxclaims request and the sandboxes reads, from every client (the controller included), the arm's API cost; sandbox writes (warm-pool refills) and leases (node heartbeats) are left out. All API requests per second covers every client in the cluster (kubelets and refills too), so it leans against the slower arm: context only.
