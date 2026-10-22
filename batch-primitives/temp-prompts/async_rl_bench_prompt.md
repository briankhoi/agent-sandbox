# RL claim_mode async benchmark: local agent prompt (2026-10-22, v1)

Paste the block below into a new Claude Code chat started at the root of your `agent-sandbox` checkout (`/Users/brian/Desktop/comp_sci/google/agent-sandbox`). It needs no earlier chat. Expect about 45 minutes, most of it unattended. Pasting it again after an interruption is safe: finished runs are skipped, and the suite deletes stale claims and Leases when it starts.

```
You are running one benchmark in the agent-sandbox repo, on the local kind cluster batch-bench. The RL library (examples/agent-sandbox-rl) is about to get a PR that adds FleetConfig(claim_mode="batch"). Every earlier measurement drove the sync SandboxFleet from threads. AsyncSandboxFleet is a thin wrapper: each await runs the sync fleet's call on a thread pool (agent_sandbox_rl/async_fleet.py, _to_thread and _thread_pool). Nobody has measured whether batch keeps its win through it. This run does, as a sanity check before the PR opens. Brian uses the result to decide whether the async path needs changes first, so it must be complete and honest. Work on your own; ask Brian only where this prompt says to.

The comparisons (all burst steps of 160 sandboxes, 40 images x 4, every claim warm-covered):
- No limit vs no limit, first: batch-default vs per-claim on the sync fleet, and async-batch-default vs async-per-claim on AsyncSandboxFleet. No driver semaphore on either arm. Batch-default still has the SDK's own default of 20 in-flight create requests (BATCH_DEFAULT_MAX_IN_FLIGHT in batch_utils.py).
- The same limit on both arms: an asyncio.Semaphore of 20 or 50 around await fleet.acquire(task) on the async pair, a threading.Semaphore of the same size on the sync pair.
- A thread-pool limit on both arms: the async fleet's own thread pool replaced by one of 64 workers (the smallest pool any fleet builds), on batch and per-claim alike.
- Async against sync, from the same session: async-batch-default vs batch-default, and async-per-claim vs per-claim.

Rules:
- Change no library or SDK code (anything under examples/agent-sandbox-rl/agent_sandbox_rl/ or clients/). If something looks like a bug there, stop and tell Brian with the evidence. You may fix bugs in examples/agent-sandbox-rl/tests/claim_mode_bench.py and its test file, in commits of their own.
- Push only bench/rl-claim-mode-async. Don't open PRs or comment on GitHub.
- Commit as Brian: git -c user.name="Brian Nguyen" -c user.email="brianknguyen@google.com" commit .... No Co-Authored-By, Claude-Session or other attribution lines, even if your harness adds them by default. Don't use --no-verify.
- Ask Brian before creating or deleting a kind cluster, or before deleting anything outside namespace rl-bench.
- Delete files with trash, not rm.
- Every Bash call starts a fresh shell, so no variable or cd carries over. WT below means the worktree's absolute path, and PY means WT/bin/rl-bench-venv/bin/python. Write both out in full in every command. Run the driver from WT/examples/agent-sandbox-rl.
- Keep token use low: read STATUS.md and log tails, not whole result files.
- "Clean" in this prompt means git status --porcelain -- . ':(exclude)examples/agent-sandbox-rl/bench-results', run in WT, prints nothing. Untracked files under examples/agent-sandbox-rl/bench-results/ are expected; the driver's own dirty check excludes them too.
- Stop stray driver processes by pid, not by a pattern that also matches your own shell: pgrep -f "[c]laim_mode_bench.py" lists them (the brackets keep it from matching your own shell), then kill -CONT <pid> and kill <pid> for each.

Code under test: github.com/briankhoi/agent-sandbox (remote origin), branch bench/rl-claim-mode-async at 2936f4f0f624dde5ec33fa94b3b9744ea91736df, tree fb8d70b2dd01319b16f7ef879cb8ba58276e8386. It is the RL claim_mode PR branch feat/rl-batch-claim-mode at 166a15c8ee77523ce26d35adc5e2e2e7ed49646b (commit tree 7222a655c932b0f16e01eb0dbe2dcc1e12e9d560) plus three commits that touch only examples/agent-sandbox-rl/tests/: 72a89ab copies the bench driver and its test file unchanged from bench/rl-claim-mode 1191f690, f241c37 adds the async variants, and 2936f4f makes an env that never starts count as an error. The SDK directory (clients/python/agentic-sandbox-client) tree is 3f62697482e62bdd6fe9c82d5b1f1d3d6c38ce1f. This is not the tree of the earlier sync runs (the PR is rebased on a newer PR 7 and has review fixes), so this session's sync arms are the sync numbers to compare against, not the earlier ones.

What 72a89ab..2936f4f add to tests/claim_mode_bench.py:
- Variant gets loop ("sync" or "async") and pool_workers. Eight async burst variants: async-per-claim, async-per-claim-c20, async-per-claim-c50, async-batch-default, async-batch-c20, async-batch-c50, async-per-claim-pool64, async-batch-pool64.
- run_burst_async builds AsyncSandboxFleet(sync_fleet=...) over the same arm_fleet as the sync arms, runs every env as an asyncio task (await acquire, one exec, wait for the step to end, await release), then awaits teardown and close. An env still waiting at the 600 s deadline is recorded as an error ("env did not start before the deadline"), so a stalled run cannot look fast. The exec goes to a driver thread pool of 160 workers, so only the fleet's own pool can bind. For the pool64 variants it swaps the fleet's private _executor for a 64-worker ThreadPoolExecutor before first use.
- Results gain pool_workers_actual, loop_lag_s (the event loop's scheduling lag, sampled every 10 ms) and leaked_pool_threads (live "asrl-async" threads after close).
- The Verdict block gains async_default_ratio, async_c20_vs_async_per_claim_c20, async_c50_vs_async_per_claim_c50, async_pool64_ratio, async_batch_vs_sync_batch, async_per_claim_vs_sync_per_claim and an async_checks block of true/false/null checks (ASYNC_TOLERANCE 1.2, fixed before this run). The report gains an "Async path" table.

Step 0. Checks. Write each command and its output to WT/examples/agent-sandbox-rl/bench-results/async/PROVENANCE.md (create the directory). On a re-paste, append under a new dated heading.
- git fetch origin. The worktree is .claude/worktrees/rl-claim-mode-async. If it's missing, create it with git worktree add .claude/worktrees/rl-claim-mode-async origin/bench/rl-claim-mode-async, then git -C .claude/worktrees/rl-claim-mode-async checkout -B bench/rl-claim-mode-async origin/bench/rl-claim-mode-async. WT is its absolute path (git -C .claude/worktrees/rl-claim-mode-async rev-parse --show-toplevel). Don't reuse the old rl-claim-mode worktree. Its venv is installed from the old tree.
- In WT: it must be clean (as defined in the rules; if not, tell Brian), then git checkout bench/rl-claim-mode-async, git merge --ff-only origin/bench/rl-claim-mode-async. On a first paste, git rev-parse HEAD HEAD^{tree} HEAD:clients/python/agentic-sandbox-client must print 2936f4f0f624dde5ec33fa94b3b9744ea91736df, fb8d70b2dd01319b16f7ef879cb8ba58276e8386 and 3f62697482e62bdd6fe9c82d5b1f1d3d6c38ce1f. On a re-paste, git merge-base --is-ancestor 2936f4f0f624dde5ec33fa94b3b9744ea91736df HEAD must succeed, and git log --oneline 2936f4f0f624dde5ec33fa94b3b9744ea91736df..HEAD must show only your own driver fixes and result commits. If a check fails, stop and tell Brian.
- kubectl config current-context must be kind-batch-bench, and kubectl get nodes must show every node Ready. If the context or cluster is missing (kind get clusters doesn't list batch-bench), or its nodes are stopped (docker ps doesn't list batch-bench-control-plane), stop and ask Brian. Starting or creating it needs his OK.
- The controller Deployment is Running (kubectl get deploy -A | grep -i sandbox). Record its image and imageID. If the earlier run's file exists (.claude/worktrees/rl-claim-mode/examples/agent-sandbox-rl/bench-results/claim-mode-limits/PROVENANCE.md, or the same path in the main checkout), compare and record any difference. If it doesn't exist, say so.
- Nothing else is using the cluster: ps aux | grep -E "bench.py|claim_mode_bench" | grep -v grep prints nothing, and docker ps --format '{{.Names}}' shows only batch-bench nodes. Otherwise ask Brian.
- Pod slots: the run needs 340 for its own pods. rl-bench pods left from earlier runs don't count, because the suite scales its own pools. Count pods outside rl-bench with kubectl get pods -A --field-selector spec.nodeName!= --no-headers | grep -v '^rl-bench ' | wc -l. Allocatable pods (kubectl get node -o jsonpath='{.items[*].status.allocatable.pods}') minus that count must be at least 340. If it's less, ask Brian.

Step 1. Environment.
- Create WT/bin/rl-bench-venv with a Python of 3.11 or newer (try python3, python3.14, python3.13, python3.12, python3.11): <python> -m venv WT/bin/rl-bench-venv, then WT/bin/rl-bench-venv/bin/pip install -e WT/clients/python/agentic-sandbox-client -e "WT/examples/agent-sandbox-rl[test]". Skip this if the venv already exists in WT.
- PY -c "import k8s_agent_sandbox, agent_sandbox_rl; print(k8s_agent_sandbox.__file__, agent_sandbox_rl.__file__)" must print two paths inside WT.
- From WT/examples/agent-sandbox-rl: PY -m pytest -q tests/test_claim_mode_bench.py tests/test_batch_claims.py tests/test_async_fleet.py. All must pass (55 tests: 25, 17 and 13). Don't run the full suites.
- WT must still be clean (as defined in the rules).

Step 2. Images and pools. Both are left over from the earlier runs, so this step usually only checks.
- docker exec batch-bench-control-plane crictl images | grep -c rl-task must print 51. If it doesn't, run PY tests/claim_mode_bench.py images. If that fails because python:3.12-slim is missing from the node, follow the image-loading fallbacks in /mnt/project-files/local-prompts/rl_prototype_prompt.md Step 2 if you can read it, or ask Brian.
- Pools: if kubectl get sandboxwarmpool -n rl-bench --no-headers | wc -l prints 50, skip setup. The suite scales each shape's pools itself and waits for them before every run. Only if the count isn't 50, run PY tests/claim_mode_bench.py setup. Its slot check counts every scheduled pod, so with rl-bench pods still up it can refuse. In that case scale the existing rl-bench pools to 0 with kubectl patch sandboxwarmpool <name> -n rl-bench --type merge -p '{"spec":{"replicas":0}}' for each, wait until kubectl get pods -n rl-bench --no-headers | wc -l prints 0, then run setup again. Afterwards kubectl get sandboxwarmpool -n rl-bench must show 50 pools.
- If the smoke test below fails with "warm state not reached" or a missing pool, the existing pools don't match this tree's plan. Scale them to 0, delete the 50 pools and the rlb-* templates in rl-bench with kubectl delete (this is inside rl-bench, so you may), run setup, and smoke again.

Step 3. Smoke test of the eight async variants only. Skip it if PROVENANCE.md records "async smoke passed at <current HEAD>".
- From WT/examples/agent-sandbox-rl, using the Bash tool's run_in_background: RL_BENCH_SMOKE=1 RL_BENCH_RESULTS=bench-results/async-smoke PY tests/claim_mode_bench.py suite --only burst-b160-async- > bench-results/async-smoke.log 2>&1
  Smoke runs 8 envs per variant, one run each. It takes a few minutes.
- Check the eight JSONs in bench-results/async-smoke/:
  - no "exception" key, errors is 0, and started is 8;
  - settings.loop is "async"; settings.pool_workers is 64 for the two pool64 variants and null for the rest; settings.acquire_concurrency is 20 and 50 for the c20 and c50 variants and null otherwise;
  - pool_workers_actual is 64 in all eight (smoke runs 8 envs, so the default pool is its 64-worker floor; in the real run it is 496 for the non-pool64 variants);
  - loop_lag_s is present, leaked_pool_threads is 0, last_start_s and acquired_s are present, "statuses" has no "-1" key;
  - the batch variants prove they used batch: watches of async-batch-default is smaller than watches of async-per-claim (expect 1 against 8);
  - provenance.head is HEAD, and provenance.dirty is false.
- Record kubectl get lease -n rl-bench --no-headers | wc -l and the sandboxclaim count (kubectl get sandboxclaim -n rl-bench --no-headers | wc -l) after the smoke run. Both should be 0, because teardown releases the batch. If not, record the numbers and carry on, the report asks for them.
- If it fails because of the driver, fix the driver in its own commit and smoke again. If it fails because of library code, stop and tell Brian. When it passes, record "async smoke passed at <HEAD>" in PROVENANCE.md and trash bench-results/async-smoke and bench-results/async-smoke.log.

Step 4. The run. From WT/examples/agent-sandbox-rl, using the Bash tool's run_in_background:
RL_BENCH_RESULTS=bench-results/async PY tests/claim_mode_bench.py suite --only burst-b160-per-claim burst-b160-batch-default burst-b160-batch-c burst-b160-async- >> bench-results/async/suite.log 2>&1
- It runs 42 burst runs, interleaved with the order rotated each round:
  - sync: per-claim, per-claim-c20, per-claim-c50, batch-default, batch-c20, batch-c50;
  - async: async-per-claim, async-per-claim-c20, async-per-claim-c50, async-batch-default, async-batch-c20, async-batch-c50, async-per-claim-pool64, async-batch-pool64.
  Each of the 14 variants gets 3 runs. The prefix burst-b160-per-claim matches only the three sync per-claim variants, because the async labels start with burst-b160-async-. It takes about 40 minutes.
- Keep the Mac awake: find the pid with pgrep -f "[c]laim_mode_bench.py suite" (take the last pid it prints), then run caffeinate -dimsu -w <pid> in the background.
- Check progress with tail -3 bench-results/async/suite.log and cat bench-results/async/STATUS.md every 5 minutes, not more often.
- STATUS.md lists every entry of the full plan (76). Every entry outside these 42 burst labels stays "[ ]". Ignore them.
- If it stops, stop stray driver processes by pid (see the rules) and check that pgrep -f "[c]laim_mode_bench.py" prints nothing. Then start the same command again; it resumes. Start caffeinate again too.
- Errored runs: if a STATUS line shows errors=, FAIL or exception, read that run's error_samples or exception. Move the file to bench-results/async/superseded/ with a one-line reason in superseded/WHY.md, and run it once more with --only <its label> (same RL_BENCH_RESULTS). Don't retry a third time. If a variant still has an errored run (errors above 0), the report drops it from the ratios, which then read null. Report that key as null and give the reason. A run that ends in an "exception" is different: the report leaves it out of the table without marking the variant, so the other runs of that variant still give ratios. After the retries, if the "runs" column of the burst table is below 3 for any variant, report it as a finding naming the exception (an exception in fleet.teardown() on an async arm is a plausible real problem) and count it in (e). An error that appears only on async variants is a finding, not noise: quote the error_samples in your report.
- A "warm state not reached" exception on the run after an async-batch run means that run left claims behind. Treat it as a finding (quote it), and check kubectl get sandboxclaim,lease -n rl-bench.

Step 5. Report and push.
- RL_BENCH_RESULTS=bench-results/async PY tests/claim_mode_bench.py report writes bench-results/async/summary.md.
- This directory has only burst results, so the Verdict block's failures_all_pass, ship_pr1 and build_prefetch don't apply. Report only the keys named below.
- Every result file must show started equal to 160 and errors 0 for the run to count (a run with fewer started envs is an error, even if errors is 0 in a file from an older driver). Check with: PY -c "import glob,json; [print(p, r.get('started'), r.get('errors'), round(r.get('step_s', 0),1)) for p in sorted(glob.glob('bench-results/async/burst-*.json')) for r in [json.load(open(p))] if r.get('exception') or r.get('started')!=160 or r.get('step_s',0)>60]" It prints nothing when every run started all 160 envs within 60 s of the step's start. A line is an error: handle it like an errored run.
- Totals: PY -c "import glob,json; rs=[json.load(open(p)) for p in glob.glob('bench-results/async/burst-*.json')]; print(len(rs), sum(r.get('http_429',0) for r in rs), sum(r.get('throttled_logs',0) for r in rs), sum(r.get('statuses',{}).get('-1',0) for r in rs), sum(r.get('driver_requests',0) for r in rs))". It prints the file count, 429s, throttled logs, "-1" statuses and driver_requests. driver_requests is 0 by design in bursts.
- Record kubectl get lease -n rl-bench --no-headers | wc -l and the sandboxclaim count once more, after the last run finished and before you do anything else on the cluster.
- Stop caffeinate. git add bench-results/async (JSONs, STATUS.md, PROVENANCE.md, summary.md, suite.log, superseded/ if any). Commit "bench(agent-sandbox-rl): async fleet claim modes on kind" with the identity from the rules, then git push origin bench/rl-claim-mode-async.

Reading the result. Write down which of these apply, with the numbers. Medians are of 3 runs; if the min-max ranges of the two things being compared overlap, call that "inside noise", not a pass or a fail. This is a reading guide from the design of AsyncSandboxFleet, not something measured:
- async_batch_within_tolerance_of_sync_batch is false (async-batch-default more than 1.2x the same session's batch-default). The wrapper's thread hop costs more than expected. Look at loop_lag_s, driver_cpu_s and the async-batch-c20 / c50 rows. Needs changes: a native async batch path, or fewer thread hops per acquire.
- async_ratio_within_tolerance_of_sync_ratio is false while the check above is true. Async per-claim got faster than sync per-claim, so batch's relative win shrank without batch getting slower. Say which of async_per_claim_vs_sync_per_claim moved. Probably no change needed.
- async_batch_faster_than_async_per_claim, async_c20_batch_faster or async_c50_batch_faster is false. Batch loses to per-claim on the async path at that setting (if the two ranges overlap, that is inside noise, not a failure; the sync c20 win is slim), where the sync pair at the same setting wins (check batch_default_ratio, batch_c20_vs_per_claim_c20, batch_c50_vs_per_claim_c50). Needs changes before the PR claims batch helps async users. Stop and report; don't tune.
- async_pool64_batch_faster is false, or async-batch-pool64 and async-per-claim-pool64 are both much slower than the no-limit async arms. An acquire holds its pool thread until the claim is Ready (async_fleet.py _to_thread), so the pool is a hidden concurrency limit equal to its size, and _thread_pool sizes it max(64, max_concurrent + 2 x window + 16). A fleet built with a small max_concurrent but more concurrent envs than that is capped without saying so, on both modes. Report the ratio of each pool64 arm to its no-limit arm. If pool64 is much slower than no limit, that is a finding for docs or pool sizing, not a batch regression. Say that this driver sets max_concurrent 160, so it never meets the cap unless pool_workers forces it.
- default_pool_not_binding is false. The default pool was smaller than the 160 envs (it should be 496). Stop and tell Brian the pool size the run saw, since every async arm was then capped.
- no_loop_stall_over_100ms is false. Something blocked the event loop for more than 100 ms. Judge by loop lag p99 (the Async path table's median of runs), and treat one max sample as noise unless it repeats in all 3 runs of a variant. Report which variants and whether it lines up with the batch or per-claim arm. Needs changes if it is only on batch arms.
- no_errors_or_leaked_threads is false. Quote the error_samples or the leaked thread count. Needs changes.
- Leases or claims left in rl-bench after the run. Report the counts. If they are non-zero only after async-batch runs, the async teardown does not release the batch. Needs changes. To tell, you may run one sync batch-default burst alone with --only burst-b160-batch-default and a fresh RL_BENCH_RESULTS directory (bench-results/async-lease-check), and count again. Don't commit that directory.
Cost asymmetries between the arms, both small against the 1.2 tolerance: the sync env threads are started before the clock, while the async arms start up to 160 pool threads and the exec pool's threads inside the timed window (biases against async); the exec runs on a thread-local API client per thread, which sync builds 160 of and async reuses across workers (biases for async). To see the exec and hop cost, compare last start minus last Ready (both are in the table) for the sync and async pairs.
If every check is true, say so plainly: the async path needs no changes for the burst path. Not covered by this run: rolling churn, recycle, fleet.run strategies, and cancelling an await fleet.acquire (the pool thread keeps running and its claim lives until teardown). Say so in (g).

Paste back, in this order and nothing else:
(a) Lead with the no-limit comparisons: the Verdict block's batch_default_ratio (sync) and async_default_ratio (async), next to each other, each with the two last-start medians and min-max it comes from. Then async_batch_vs_sync_batch and async_per_claim_vs_sync_per_claim. Then the same-limit pairs: batch_c20_vs_per_claim_c20 against async_c20_vs_async_per_claim_c20, and batch_c50_vs_per_claim_c50 against async_c50_vs_async_per_claim_c50. Then async_pool64_ratio. Then per_claim_errors and the whole async_checks block, copied as is;
(b) the burst table rows for all 14 variants, plus requests per env for each (its requests column / 160), and the Async path table. Say that these are burst requests from the step's start to the last exec, before release;
(c) whether the six sync variants have a last-start median inside the earlier run's range for that variant (bench/rl-claim-mode 1191f690, a different tree, so this is context and not a pass or fail): per-claim 3.32-4.06 s, per-claim-c20 1.86-1.88 s, per-claim-c50 2.05-2.25 s, batch-default 1.29 s, batch-c20 1.44-1.49 s, batch-c50 1.29-1.37 s. Name any outside, and by how much;
(d) total 429s, throttled_logs, "-1" statuses and driver_requests across the runs;
(e) run accounting: planned 42, files present, errored, superseded, retried;
(f) the Lease and sandboxclaim counts after the smoke run and after the last run, and what you concluded;
(g) "Reading the result": for each bullet that applies, one line with the numbers and whether it means the async path needs changes. End with one line, "async path needs changes: yes / no / unclear", and the reason in a sentence;
(h) the commit SHA you pushed, and https://github.com/briankhoi/agent-sandbox/compare/2936f4f0f624dde5ec33fa94b3b9744ea91736df...bench/rl-claim-mode-async;
(i) wall-clock time, any driver fix with its reason, and anything you did differently from this prompt, with the reason.
```

## What each reference was checked against (2936f4f)

| Reference | Where |
|---|---|
| `AsyncSandboxFleet(sync_fleet=...)`, exported from the package | `async_fleet.py` `__init__`, `agent_sandbox_rl/__init__.py` |
| Each await runs the sync call on a dedicated pool, sized `min(1024, max(64, max_concurrent + 2 x window + 16))`; 496 for `max_concurrent` 160, 64 for `max_concurrent` 8 (smoke) | `_thread_pool`; constructed against a mock registry with this driver's `fleet_config` and printed 496 |
| The pool is built lazily, so setting `_executor` before first use replaces it; `_max_workers` reads its size | `_thread_pool`; ran with a 64-worker pool and printed 64 |
| `await fleet.acquire(task)`, `await fleet.release(handle)`, `await fleet.teardown()`, `fleet.close()`, `fleet.tasks` | `async_fleet.py` |
| `handle.exec(["true"])` is sync only, so the driver runs it on its own pool | `handles.py` `SandboxHandle.exec`; no async exec on `AsyncSandboxFleet` |
| `fleet_config` fields and `BatchClaimConfig(create_rps, max_in_flight, lease_duration)` are unchanged by the PR's review fixes | `git diff` of `config.py` between the prototype and the PR, driver tests and smoke |
| Variant names, labels `burst-b160-<variant>-run<k>`, 14 variants x 3 runs = 42, `--only` is a prefix match; async labels start with `burst-b160-async-` | `BURST_VARIANTS`, `run_plan`, `cmd_suite` |
| `RL_BENCH_RESULTS`, `RL_BENCH_SMOKE`; smoke shape 2 images x 4 | module constants, `BURST` |
| Plan has 76 entries | `len(run_plan())` |
| 25 + 17 + 13 = 55 unit tests pass at 2936f4f; the 10 new ones fail under mutation of the semaphore, teardown call, tolerance compare, pool check, errors filter and lag check | run in the cloud, no cluster |
| `setup` needs 340 free pod slots; `clean_stale` at suite start; `wait_warm` waits for 0 claims | `cmd_setup`, `clean_stale`, `wait_warm` |
| Earlier sync medians and ranges | `bench-results/claim-mode-limits/summary.md` at 1191f690 |
| `BATCH_DEFAULT_MAX_IN_FLIGHT = 20` | `batch_utils.py:63` at 3f626974 |
| Not run in the cloud | the async arms against a cluster (kind can't run pods there), so `run_burst_async` itself is unexecuted end to end; `run_async_step` is covered by a fake fleet |
