# SWE-bench load test

## Parameters

- **images**: `40`
- **tasks_per_image**: `4`
- **total_tasks**: `160`
- **strategies**: `naive`
- **claim_mode**: `batch`
- **batch_claims**: `unbounded`
- **max_concurrent**: `160`
- **max_warmpool_size**: `8`
- **warm_per_task**: `True`
- **colocate_replicas**: `False`
- **window_size**: `None`
- **task_duration_s**: `0.0`
- **image_template**: `us-central1-docker.pkg.dev/brianknguyen-gke-dev/rl-gke-bench/rl-task:g{i:03d}`

## Methodology

Each strategy runs the **same** synthetic SWE-bench batch — **40** distinct container images, **4** task(s) per image (**160** tasks total) — end to end against the live cluster, then tears its pools down before the next strategy starts (so strategies never share warm state).

Per task the harness: (1) ensures a `SandboxTemplate` + `SandboxWarmPool` exist for the task's image — *when/how many* pools are pre-warmed is what the **strategy** decides; (2) **claims** a ready sandbox; (3) runs `process_fn` — here a no-op probe (`task_duration=0.0s`), so the table isolates *infrastructure* latency from task compute; (4) releases the sandbox.

Wall-clock is the true end-to-end batch time under `max_concurrent=160`. Per-phase totals are summed across concurrent workers, so they exceed wall-clock — divide by the phase count (`n`) for the average a single task saw. **Efficiency** = fastest strategy's wall ÷ this strategy's wall.

RL instant-claim mode: **per-task sizing** (one warm replica per task — `min(tasks_image, max_warmpool_size=8)` — so claims are near-instant).

Strategies compared:
- **naive** — all pools warmed up front (peak = #images) — fastest claims, largest footprint.

## Cluster & nodes

- `loadtest`: **context**=(ambient)  **namespace**=rl-loadtest  **k8s_version**=v1.37.1-gke.1697000  **nodes**=28  **node_pools**=[controller-pool, default-pool, driver, sandbox]  **instance_types**=[e2-standard-4, e2-standard-8, n2-standard-8]  **region**=us-central1

## Warm-pool plan (40 pools)

| image | pool | replicas | cluster |
|---|---|---:|---|
| `rl-task:g000` | `pool-r2e-img-c409b8bd2b09` | 4 | loadtest |
| `rl-task:g001` | `pool-r2e-img-0f003c7dd919` | 4 | loadtest |
| `rl-task:g002` | `pool-r2e-img-f976488e79c9` | 4 | loadtest |
| `rl-task:g003` | `pool-r2e-img-9ee3bf318832` | 4 | loadtest |
| `rl-task:g004` | `pool-r2e-img-9f6ebe7b25e0` | 4 | loadtest |
| `rl-task:g005` | `pool-r2e-img-52fa9ce3c851` | 4 | loadtest |
| `rl-task:g006` | `pool-r2e-img-4e61c9d979b8` | 4 | loadtest |
| `rl-task:g007` | `pool-r2e-img-ad4d3ffb821c` | 4 | loadtest |
| `rl-task:g008` | `pool-r2e-img-ca14c438b94d` | 4 | loadtest |
| `rl-task:g009` | `pool-r2e-img-75506d761caa` | 4 | loadtest |
| `rl-task:g010` | `pool-r2e-img-bcef38296375` | 4 | loadtest |
| `rl-task:g011` | `pool-r2e-img-ff4e351f62dc` | 4 | loadtest |
| `rl-task:g012` | `pool-r2e-img-7f8d23da299a` | 4 | loadtest |
| `rl-task:g013` | `pool-r2e-img-8526f60c924d` | 4 | loadtest |
| `rl-task:g014` | `pool-r2e-img-c10011204f6f` | 4 | loadtest |
| `rl-task:g015` | `pool-r2e-img-b48ebbc3aa2f` | 4 | loadtest |
| `rl-task:g016` | `pool-r2e-img-5bff553f120c` | 4 | loadtest |
| `rl-task:g017` | `pool-r2e-img-3408174e73a6` | 4 | loadtest |
| `rl-task:g018` | `pool-r2e-img-790a453f6965` | 4 | loadtest |
| `rl-task:g019` | `pool-r2e-img-f14b729a8950` | 4 | loadtest |
| `rl-task:g020` | `pool-r2e-img-ef67267bcd7e` | 4 | loadtest |
| `rl-task:g021` | `pool-r2e-img-b389c806dd86` | 4 | loadtest |
| `rl-task:g022` | `pool-r2e-img-d3403edb6a29` | 4 | loadtest |
| `rl-task:g023` | `pool-r2e-img-833c38e8a078` | 4 | loadtest |
| `rl-task:g024` | `pool-r2e-img-c644ca6c3a85` | 4 | loadtest |
| `rl-task:g025` | `pool-r2e-img-29541d0eeeb6` | 4 | loadtest |
| `rl-task:g026` | `pool-r2e-img-e077f1583f5d` | 4 | loadtest |
| `rl-task:g027` | `pool-r2e-img-b23c89bf558b` | 4 | loadtest |
| `rl-task:g028` | `pool-r2e-img-b2f1a0b486c6` | 4 | loadtest |
| `rl-task:g029` | `pool-r2e-img-98fbff509d6d` | 4 | loadtest |
| `rl-task:g030` | `pool-r2e-img-c81a00278346` | 4 | loadtest |
| `rl-task:g031` | `pool-r2e-img-438a4d5edd1e` | 4 | loadtest |
| `rl-task:g032` | `pool-r2e-img-b61569742ae5` | 4 | loadtest |
| `rl-task:g033` | `pool-r2e-img-659ee300f540` | 4 | loadtest |
| `rl-task:g034` | `pool-r2e-img-c2b09f10fdc8` | 4 | loadtest |
| `rl-task:g035` | `pool-r2e-img-c9d770fce3a9` | 4 | loadtest |
| `rl-task:g036` | `pool-r2e-img-213cd4e91caf` | 4 | loadtest |
| `rl-task:g037` | `pool-r2e-img-ce133163ba2d` | 4 | loadtest |
| `rl-task:g038` | `pool-r2e-img-31dd7125ed17` | 4 | loadtest |
| `rl-task:g039` | `pool-r2e-img-45eb80fc25a9` | 4 | loadtest |

## Results — per stage, per strategy

> **wall** is the true end-to-end time for the whole batch. Per-phase columns are **summed across concurrency**, so they exceed the wall-clock — the `avg` figures are the per-task experience. **claim avg/max** is the *time-to-sandbox* (request → ready, claimed sandbox); **efficiency** = fastest strategy's wall ÷ this strategy's wall.

| strategy | wall | prep (create+wait) | pool-ready avg/max | claim avg/max | claims | net task Σ/avg | warm pools (peak/total/created) | ok | efficiency |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| **naive** | 17.7s | 422.9s | 10.5/14s | 1.5/3s | 160 | 0.0/0.00s | 160/160/40 | 160/160 | 100% |

## Metric glossary

Each line in a strategy's RunReport reads `phase  total_s  (n=count, max=slowest)`:

| field | meaning |
|---|---|
| `total_s` | wall-time **summed across all concurrent workers** spent in this phase |
| `n` | how many times the phase ran (one per pool, or one per task) |
| `max` | the slowest single occurrence — the tail latency for that phase |

| phase | what it measures |
|---|---|
| `preflight` | one-time cluster checks (API reachable, namespace, CRDs) before any work |
| `plan` | compute the image→warm-pool plan (in-memory; ~0s) |
| `create_warmpool` | issue the `SandboxWarmPool` create calls (n = pools created) |
| `wait_pool_ready` | block until a pool replica is Ready — the **image pull + pod start** cost |
| `prefetch` | *(pipelined only)* background warm of the next window, overlapped with execution |
| `claim` | request a sandbox → have a claimed, ready one — **time-to-sandbox** |
| `process` | time inside `process_fn` (the task itself; 0s here by design) |
| `release` | return the sandbox / claim to the pool |
| `teardown` | delete pools + templates at the end of the run |

## Full RunReport per strategy

### naive
```
  preflight            0.20s  (n=1, max=0.20s)
  plan                 0.00s  (n=1, max=0.00s)
  create_warmpool      4.39s  (n=40, max=0.26s)
  wait_pool_ready    418.50s  (n=40, max=13.51s)
  claim              244.14s  (n=160, max=2.90s)
  process              0.00s  (n=160, max=0.00s)
  release             24.92s  (n=160, max=0.59s)
  teardown             0.68s  (n=1, max=0.68s)
  TOTAL wall          17.74s
  claims=160  tasks=160ok/0err  warm peak=160
```

## Image list

```
us-central1-docker.pkg.dev/brianknguyen-gke-dev/rl-gke-bench/rl-task:g000
us-central1-docker.pkg.dev/brianknguyen-gke-dev/rl-gke-bench/rl-task:g001
us-central1-docker.pkg.dev/brianknguyen-gke-dev/rl-gke-bench/rl-task:g002
us-central1-docker.pkg.dev/brianknguyen-gke-dev/rl-gke-bench/rl-task:g003
us-central1-docker.pkg.dev/brianknguyen-gke-dev/rl-gke-bench/rl-task:g004
us-central1-docker.pkg.dev/brianknguyen-gke-dev/rl-gke-bench/rl-task:g005
us-central1-docker.pkg.dev/brianknguyen-gke-dev/rl-gke-bench/rl-task:g006
us-central1-docker.pkg.dev/brianknguyen-gke-dev/rl-gke-bench/rl-task:g007
us-central1-docker.pkg.dev/brianknguyen-gke-dev/rl-gke-bench/rl-task:g008
us-central1-docker.pkg.dev/brianknguyen-gke-dev/rl-gke-bench/rl-task:g009
us-central1-docker.pkg.dev/brianknguyen-gke-dev/rl-gke-bench/rl-task:g010
us-central1-docker.pkg.dev/brianknguyen-gke-dev/rl-gke-bench/rl-task:g011
us-central1-docker.pkg.dev/brianknguyen-gke-dev/rl-gke-bench/rl-task:g012
us-central1-docker.pkg.dev/brianknguyen-gke-dev/rl-gke-bench/rl-task:g013
us-central1-docker.pkg.dev/brianknguyen-gke-dev/rl-gke-bench/rl-task:g014
us-central1-docker.pkg.dev/brianknguyen-gke-dev/rl-gke-bench/rl-task:g015
us-central1-docker.pkg.dev/brianknguyen-gke-dev/rl-gke-bench/rl-task:g016
us-central1-docker.pkg.dev/brianknguyen-gke-dev/rl-gke-bench/rl-task:g017
us-central1-docker.pkg.dev/brianknguyen-gke-dev/rl-gke-bench/rl-task:g018
us-central1-docker.pkg.dev/brianknguyen-gke-dev/rl-gke-bench/rl-task:g019
us-central1-docker.pkg.dev/brianknguyen-gke-dev/rl-gke-bench/rl-task:g020
us-central1-docker.pkg.dev/brianknguyen-gke-dev/rl-gke-bench/rl-task:g021
us-central1-docker.pkg.dev/brianknguyen-gke-dev/rl-gke-bench/rl-task:g022
us-central1-docker.pkg.dev/brianknguyen-gke-dev/rl-gke-bench/rl-task:g023
us-central1-docker.pkg.dev/brianknguyen-gke-dev/rl-gke-bench/rl-task:g024
us-central1-docker.pkg.dev/brianknguyen-gke-dev/rl-gke-bench/rl-task:g025
us-central1-docker.pkg.dev/brianknguyen-gke-dev/rl-gke-bench/rl-task:g026
us-central1-docker.pkg.dev/brianknguyen-gke-dev/rl-gke-bench/rl-task:g027
us-central1-docker.pkg.dev/brianknguyen-gke-dev/rl-gke-bench/rl-task:g028
us-central1-docker.pkg.dev/brianknguyen-gke-dev/rl-gke-bench/rl-task:g029
us-central1-docker.pkg.dev/brianknguyen-gke-dev/rl-gke-bench/rl-task:g030
us-central1-docker.pkg.dev/brianknguyen-gke-dev/rl-gke-bench/rl-task:g031
us-central1-docker.pkg.dev/brianknguyen-gke-dev/rl-gke-bench/rl-task:g032
us-central1-docker.pkg.dev/brianknguyen-gke-dev/rl-gke-bench/rl-task:g033
us-central1-docker.pkg.dev/brianknguyen-gke-dev/rl-gke-bench/rl-task:g034
us-central1-docker.pkg.dev/brianknguyen-gke-dev/rl-gke-bench/rl-task:g035
us-central1-docker.pkg.dev/brianknguyen-gke-dev/rl-gke-bench/rl-task:g036
us-central1-docker.pkg.dev/brianknguyen-gke-dev/rl-gke-bench/rl-task:g037
us-central1-docker.pkg.dev/brianknguyen-gke-dev/rl-gke-bench/rl-task:g038
us-central1-docker.pkg.dev/brianknguyen-gke-dev/rl-gke-bench/rl-task:g039
```