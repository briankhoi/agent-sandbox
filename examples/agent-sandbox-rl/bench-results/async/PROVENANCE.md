# Provenance: async fleet claim modes (2026-10-22)

## Step 0 checks
$ git rev-parse HEAD HEAD^{tree} HEAD:clients/python/agentic-sandbox-client
2936f4f0f624dde5ec33fa94b3b9744ea91736df
fb8d70b2dd01319b16f7ef879cb8ba58276e8386
3f62697482e62bdd6fe9c82d5b1f1d3d6c38ce1f
(worktree .claude/worktrees/rl-claim-mode-async was missing; created from origin/bench/rl-claim-mode-async; status clean; ff-only merge: Already up to date)

$ kubectl config current-context
kind-batch-bench
$ kubectl get nodes
batch-bench-control-plane   Ready   control-plane   2d20h   v1.35.0
$ docker ps --format '{{.Names}}'
batch-bench-control-plane
$ ps aux | grep -E "bench.py|claim_mode_bench" | grep -v grep
(nothing)

Controller (agent-sandbox-system/agent-sandbox-controller, 1/1):
image:   kind.local/agent-sandbox-controller:bench
imageID: sha256:1cdd9b021600b9081bb0c26274b4e7f5b9d2b1149b84124809bb8a4771768d59
Earlier run (rl-claim-mode worktree, claim-mode-limits/PROVENANCE.md) exists: same image and imageID, no difference.

Pod slots: allocatable 400, pods outside rl-bench 11 -> 389 free >= 340.

## Step 1
python3 = 3.14.3. venv at bin/rl-bench-venv; both packages import from inside WT.
pytest tests/test_claim_mode_bench.py tests/test_batch_claims.py tests/test_async_fleet.py: 55 passed. WT clean afterwards.

## Step 2
crictl images | grep -c rl-task = 51; sandboxwarmpool count in rl-bench = 50. No setup needed.

## Step 3 smoke
async smoke passed at 2936f4f0f624dde5ec33fa94b3b9744ea91736df
8 variants x 8 envs: no exception, errors 0, started 8, settings.loop async, pool_workers 64 only on pool64, acquire_concurrency 20/50 on c20/c50, pool_workers_actual 64, loop_lag_s present, leaked_pool_threads 0, no "-1" status, watches batch 1 vs per-claim 8, provenance.head = HEAD, dirty false.
After smoke: leases in rl-bench = 0, sandboxclaims = 0.

## Step 4/5 run
Suite: 42 burst runs (14 variants x 3), one pass, exit 0, no errored/superseded/retried runs. Wall clock of the suite about 21 min (16:22 to 16:44).
After the last run: leases in rl-bench = 0, sandboxclaims = 0.
Validation: no run with exception, started != 160, or step_s > 60. Totals: 42 files, 429s 0, throttled_logs 0, "-1" statuses 0, driver_requests 0.
