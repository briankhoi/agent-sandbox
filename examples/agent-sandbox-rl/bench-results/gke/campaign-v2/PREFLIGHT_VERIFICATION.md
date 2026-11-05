# Campaign 2 — 7-Point Live Pre-Flight Sanity Verification (`asrl-bench-c2`)

**Timestamp:** `2026-11-05T06:44:18Z`

| # | Smoking Gun / Parity Requirement | Target (`RESULTS.md` `#10–#16`) | Verified Live Value on `asrl-bench-c2` | Status |
|---|---|---|---|---|
| 1 | Regional Control Plane Sizing (`GOMAXPROCS`) | `>= 16` vCPUs per replica, `3` regional HA replicas (`us-central1`) | `go_sched_gomaxprocs_threads = 16` (`3 × 16 = 48` regional vCPUs; `483` nominal seats/replica = `1,449` regional seats; `workload-low = 196` seats/replica = `588` regional seats vs `25` in Campaign 1; `0` Spot warm-up VMs left) | PASS |
| 2 | Controller Profile `P1` Flags & Isolation | Dedicated `ctrl-pool` (`2 × e2-standard-32`), `sbx=1000`, `claim=1000`, `wp=500`, `tpl=100`, `max-batch=500`, `grace=30m`, `--kube-api-qps=-1` | Verified live on both pods (`agent-sandbox-controller-8d5f8fb4-*`), `0` restarts | PASS |
| 3 | Stock APF Untouched (`Phase I`) | `workload-low = 100` shares, `0` custom `FlowSchema`s | `workload-low shares=100`, `0` custom `FlowSchema`s active | PASS |
| 4 | Driver APF Classification & Intra-Region RTT | Driver hits `FlowSchema = global-default` (never `workload-low`), `<1 ms` intra-region RTT | Verified via `automountServiceAccountToken: false` + GCE metadata IAM token (`apiserver_flowcontrol_dispatched_requests_total{flow_schema="global-default",priority_level="global-default"}` incremented `25 -> 52`, `0` driver calls on `workload-low`) | PASS |
| 5 | `gvisor-pool` Hardware & 100%-Node Cache Hydration | `10 × e2-standard-32`, `600 GB pd-ssd`, `maxPods=256`, `gvisor`, GCFS `STREAMING=true`, all `500` SWE-bench images warm on all `10` nodes | `10/10` `Ready`, `maxPods=256`, `STREAMING=true`; `prepull_swebench500_real.json`: `500` images × `10` nodes (`20` DaemonSets, `ok=true`, `902.46s`); `0` leftover objects in `agent-sandbox-rl` | PASS |
| 6 | Pod Spec & Strategy Window Parity | `125m / 1Gi`, `window_size=auto` (`500`), `warm_create_budget=1000`, `overcommit_factor=3.5`, `termination_grace_seconds=0` | Verified defaults in `run_basic_swebench_3000.py` + `bench_core.py` (`asrl-driver:c2-25n-wsb`) | PASS |
| 7 | Succeeded vs Failed Latency Separation | `phases` (`ok=True` only, w/ `p50/p90/p95/p99`) strictly separated from `phases_err` (`ok=False` only) | Verified in Docker build assertion & `723` `agent-sandbox-rl` unit tests | PASS |
