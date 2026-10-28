# Wave mode for `fleet.run` (Task 8 design, PROPOSAL for audit)

Written by the cloud project on 2026-10-28, from thread "Decide on fleet.run wave mode". **Nothing here is approved yet.** Brian asked for "quorum features without handrolling" for direct `fleet.run` callers. He wants this design audited, then implemented and benchmarked by the local agent. Labels: **(Brian)** marks his words or decisions, **(proposal)** marks what an agent proposed, **(measured)** marks a benchmark result, and **(inferred)** marks reasoning that hasn't been run.

Code was read at `feat/rl-batch-claim-mode` `3bee2847`, with the SDK copy on that branch. Task 7 rewrites `fleet.py` `_acquire_reserved`, `batch_claims.py` and `tests/test_batch_claims.py`, so line numbers below shift after it lands. Re-read the code at the live RL head before trusting any `file:line`.

## 1. Goal and what it is not

- **Goal (Brian, 2026-10-27 21:14):** "what if people don tuse a RL framework and just call fleet run? we still want them to have quorum features without handrolling right? so i think its worth implementing now (otherwise half of project is not used)".
- **Source design:** `batch_claim_proposal.md` "Rollout Waves: Per-Step Cohorts" (around lines 597-647). It covers QUORUM, GROUP and STREAM, `min_ready` defaulting to `size`, and `fleet.run(wave=True, dispatch=...)`.
- **Not a speed feature.** On kind at 160, a fill (`claim_batch` with sized groups) was 0.97x of the batch-acquire burst: 1.26 vs 1.30 s, with about 243 requests and 1 watch on both **(measured)**. The value is the gate. A group that can't fill fails fast and runs no rollouts. Speed at 640+ is open, and the GKE suite's `w640-fill-*` rows measure it.
- **Who calls it.** Direct `fleet.run` users: the README recipes and `examples/run_swebench_*.py`. tunix calls `fleet.acquire`/`release` per env and never `fleet.run`, so this doesn't reach it.

## 2. API (proposal)

```python
from agent_sandbox_rl import RolloutDispatch   # QUORUM, GROUP, STREAM (str Enum)

fleet.run(process_fn, strategy="sliding", concurrency=40,
          wave=True, dispatch=RolloutDispatch.GROUP,
          wave_size=None,      # default: concurrency
          min_ready=None)      # int or float in (0, 1]; default: group size
# Same keywords on AsyncSandboxFleet.run.
```

- **Return value unchanged.** One result per task in task order, a failed task holding its exception, `list[list]` for `epochs > 1`.
- **Requires `FleetConfig(claim_mode="batch")`** **(proposal)**. Otherwise `ValueError`. Wave batches use `BatchClaimConfig` (`create_rps`, `max_in_flight`, `work_budget`, `lease_duration`) and `ready_timeout` as `quorum_timeout`, the same as `BatchClaims._batch_for`.
- **A group** is the tasks in one wave that share a planned pool, which means the same image. SDK groups are keyed by warm pool (`BatchGroup.warmpool`, `release_group(warmpool)`). On SWE-bench, one image is one problem, so a group is a problem's G rollouts. Tasks from different problems that share an image merge into one group, and the docs say so.
- **`min_ready`** is applied per group. A float means `ceil(min_ready * size)`. A group's members beyond `min_ready` still run, handed out by `events()` after the gate. The proposal warns that a trainer setting `min_ready < size` must account for dropped rollouts, and the README should repeat that.

## 3. Semantics per dispatch mode

`wave_size` caps how many claims a wave declares up front. `concurrency` caps how many `process_fn` calls run at once.

- **Wave size.** Waves are packed with whole groups, up to `wave_size` tasks. A single group larger than `wave_size` raises `ValueError`, because it could never fill.
- **Everything up front.** A caller who wants to claim the whole set at once sets `wave_size` to the task count.
- **`wave_size` larger than `concurrency`.** The extra members sit Ready until a slot frees, and their `shutdownTime` keeps running. Validate this, or warn when `work_budget` < `ceil(wave_size / concurrency) * expected_task_s` (the auditor picks which). The risk is that the controller deletes queued claims before they run.

| Mode | SDK consumer | Batches | When `process_fn` starts | On failure |
|---|---|---|---|---|
| QUORUM | `wait_for_quorum()`, then `events()` for members past `min_ready` | One per wave, back to back. Wave N+1 opens after wave N's last task ends | Only after every group in the wave reaches `min_ready` | Quorum fails: every task in the wave gets the `BatchError` (`QuorumUnreachableError` or `BatchTimeoutError`), and `process_fn` is never called for them. Later waves still run |
| GROUP | `group_outcomes()`, then `events()` | Rolling. When free slots cover the next group(s), a new small `claim_batch` opens for the groups that fit. At most `ceil(concurrency / min group size)` live | Per group, as soon as its own outcome is a success | A failed group's tasks get `outcome.error`, and `release_group(pool)` deletes its claims. Other groups run |
| STREAM | `events()` only | One per run. `claim_batch` fills the first `wave_size` eagerly, and later tasks use `batch.acquire(pool)` on the same batch as slots free (lazy) | Each member as it turns Ready | `MEMBER_FAILED`/`MEMBER_LOST`: that task gets a `BatchError`. An `acquire` failure: that task gets its error |

Why GROUP rolls and STREAM reuses one batch: quorum only covers groups declared in `claim_batch`. A member added by `acquire` is never an initial claim, and `group_outcomes()`/`wait_for_quorum()` ignore it (`SandboxBatch.acquire` docstring, `sandbox_batch.py:611-618` at `3bee2847`). STREAM has no gate, so lazy `acquire` refill on its one batch is fine. That's 1 watch and 1 Lease per cluster per run. `acquire` on a fill batch is supported by the same docstring.

**Releases.**
- A finished task goes through `fleet.release(handle)`, then `BatchClaim.release()`, then `release_member` (existing path, `handles.py:400-411`).
- A batch with no live and no pending members gets `batch.release()`, which drops its Lease and watch.
- A failed group gets `release_group(pool)`.
- Every wave batch is recorded in `BatchClaims._opened`, so teardown (`release_all`) releases it on any error.

**Multi-cluster.** One `claim_batch` per cluster per wave, grouped by `plan_.for_image(task.image).cluster`. QUORUM waits on every cluster's batch, and the wave fails as a whole if any cluster fails. GROUP and STREAM merge per-cluster consumers, with one consumer thread per batch.

**Pins and annotations.** `claim_batch` has no per-member pod metadata.
- Before Task 7, tasks with `snapshot_uid`/`pod_annotations` can't be expressed through `fleet.run` anyway, because `run()` doesn't take them.
- If a future `process_fn` path needs pins, after Task 7 they would go through `wave_batch.acquire(pool, pod_annotations=...)` with no gate.
- No code is needed now. Note it in the README only.

**Recycle (decision for Brian, see §8).**
- The proposal says: "The modes work together with recycling: a group of `K < G` sandboxes serves its `G` rollouts, `G/K` each, with a git restore between them."
- Brian's 2026-10-27 paste said the reverse: "mutually exclusive with `recycle=True`".
- v1 proposal: `wave=True, recycle=True` raises `ValueError`, and composition comes as a follow-up. Recycle's executor (`recycle.py:253`, 600 lines) is its own scheduler.

## 4. Where the code goes (proposal)

| File | Change | Size (inferred) |
|---|---|---|
| `agent_sandbox_rl/fleet.py` | Extract the batch branch of `_acquire_reserved` (handle build, `_handles` append under `_lock`, the `_torndown` race, `reserve_claim`, `_obs.claim`; about `fleet.py:1355-1476`) into `_handle_for_member(task, cluster, claim) -> SandboxHandle`. `acquire` calls it, with no behavior change. Add `_reserve_slots(n)` and `_return_slots(n)` for `max_live_sandboxes`, reusing `acquire`'s check and message (`fleet.py:1292-1300`). `run()` gains `wave`, `dispatch`, `wave_size` and `min_ready`, validates them, and swaps in the wave executor the way recycle does (`fleet.py:1834-1843`) | ~40 moved, ~45 new |
| `agent_sandbox_rl/wave.py` (new, private) | `run_waves(fleet, tasks, process_fn, concurrency, *, dispatch, wave_size, min_ready)` returns a list of results. It packs groups, opens batches through `BatchClaims`, runs the three consumers, uses a slot semaphore of `concurrency`, and writes results back by index, like `process_parallel` (`strategies.py:33-79`). It records per-task obs via `fleet._obs.task_done` like `process_parallel` | ~150 |
| `agent_sandbox_rl/batch_claims.py` | `open_wave(cluster, groups) -> SandboxBatch`, under the cluster lock. It honors `_closed`, appends to `_opened`, and passes the same SDK settings as `_batch_for`. `BatchClaim` for fill members uses the fill's `shutdownTime` formula: `started + ceil(N / create_rps) + quorum_timeout + work_budget + BATCH_SHUTDOWN_MARGIN_SECONDS` (SDK `_claim`, `sandbox_batch.py:229-242`) | ~30 |
| `agent_sandbox_rl/async_fleet.py` | `run()` takes the same keywords. Its executor is a coroutine that runs `run_waves` on the fleet's thread executor and awaits coroutine `process_fn`s, matching the existing async `process_fn` handling | ~60 |
| `agent_sandbox_rl/__init__.py` | Export `RolloutDispatch` | ~2 |
| `agent_sandbox_rl/observability.py` | `RunReport`: `waves`, `wave_groups_failed`, and a `quorum` phase timed per wave | ~15 |
| `README.md` | A "Wave mode (experimental)" section covering modes, sizing, `min_ready` bias, `work_budget` with `wave_size > concurrency`, the precheck cost and the recycle exclusion | ~40 |

`max_live_sandboxes` works like this:
1. Reserve the wave's claim count before `claim_batch`, and raise `FleetOvercommitError` before any create if it doesn't fit.
2. Each handed-out handle keeps its slot until `fleet.release` decrements it (existing path, `fleet.py:1528-1531`).
3. Members never handed out (failed group, failed quorum, batch error) return their slots at once.

The overcommit breaker (`fleet.py:1845-1846`) needs no change while waves are at most `concurrency`. It's an audit item for `wave_size > concurrency`, which may exceed `plan_.total_replicas × overcommit_factor`.

**Serial precheck.** Each new batch prechecks its pools serially, with 2 GETs per pool (`sandbox_batch.py:229`). That's about 65 ms per 40 pools on kind (scaled from 1.61 s per 1,000 groups, measured) and about 0.4-1.6 s from outside the cluster **(inferred)**. GROUP's rolling batches pay it per refill. Measure it in the benchmark, and if it matters, propose a small SDK PR that parallelizes `_precheck_pools`. Don't change the SDK in this task.

## 5. Commits (on a new branch stacked on the post-Task-7 RL head, e.g. `feat/rl-wave-mode`)

1. `refactor(agent-sandbox-rl): build batch handles in one place`: `_handle_for_member`, with no behavior change. Existing tests pass.
2. `feat(agent-sandbox-rl): reserve max_live_sandboxes slots in bulk`
3. `feat(agent-sandbox-rl): add wave mode to fleet.run`: `wave.py`, `open_wave`, the `run()` flags, the enum and the report fields.
4. `feat(agent-sandbox-rl): add wave mode to AsyncSandboxFleet.run`
5. `docs(agent-sandbox-rl): document wave mode`

## 6. Tests (one owning test per contract, sync and async; mutation-check each)

Extend `FakeBatch` in `tests/test_batch_claims.py` with sized groups, controllable per-member Ready/FAILED, `wait_for_quorum`, `group_outcomes`, `events` and `release_group`, and keep its existing `acquire` behavior. Contracts:

1. QUORUM: no `process_fn` starts before every group in the wave has `min_ready` Ready members.
2. QUORUM failure: every task in the wave holds the `BatchError`, `process_fn` is never called for them, their slots come back, and the next wave still runs.
3. GROUP: a group's tasks start only after its own outcome, and a failed group's tasks hold `outcome.error`, `release_group` is called for it, and other groups run.
4. GROUP rolling: a new batch opens once free slots cover the next group, and live batches never exceed the bound.
5. STREAM: one batch per cluster per run, later tasks use `acquire` on that batch, and a `MEMBER_FAILED` fails only that task.
6. Packing: waves hold whole groups and at most `wave_size` tasks, and a group larger than `wave_size` raises `ValueError`.
7. `max_live_sandboxes`: a wave that doesn't fit raises before any `claim_batch`, and slots are back at 0 after the run.
8. Results come back in task order across waves, windows (`sliding`) and `epochs > 1`.
9. Validation: `wave=True` with `recycle=True`, with `claim_mode="per_claim"`, or with `dispatch` outside the enum raises `ValueError`.
10. Teardown after a mid-wave exception releases every wave batch.
11. `_handle_for_member` refactor: the existing batch-acquire tests are the owning tests. Run them before and after commit 1.

## 7. Benchmark (after implementation, on GKE when a cluster is available; kind for a 160 smoke)

**Fairness rules (Brian).** Use identical limiting on both arms. Lead with unlimited vs unlimited. Measure steady steps (2+) of a long-lived fleet.

| Arm | Path |
|---|---|
| `batch` | Today's `process_parallel` with `claim_mode="batch"` (lazy acquire) |
| `wave-stream` | Eager fill plus lazy refill |
| `wave-group` | Rolling per-group batches |
| `wave-quorum` | Back-to-back waves |

Workloads:
- **W1:** P×G = 40×4 and 160×4, with `concurrency` = the task count (everything up front).
- **W2:** 1280 tasks at `concurrency=640` (refill exercised).
- **Exec duration:** fixed and variable, with a lognormal spread of about 10x, to show the idle-slot cost of back-to-back waves.
- **Fail-fast:** one group's pool points at a template whose pod can't start. Measure the time to that group's error and confirm `process_fn` never ran for it.

Report per arm:
- step makespan and last start;
- slot utilization (busy slot-seconds over `concurrency` × makespan);
- requests and watches per step;
- Leases per step;
- precheck seconds per batch;
- for fail-fast, the error latency.

Decision rules:
- `wave-stream` vs `batch` on fixed exec answers "does eager beat lazy". The existing rule is that a 20% win at 640+ justifies keeping eager as the default STREAM fill.
- `wave-group` utilization under variable exec answers whether rolling refill is worth its extra batches.

## 8. Decisions for Brian (to settle at or before the audit)

1. **Recycle:** v1 `ValueError` with a follow-up, as proposed, or compose now (K < G sandboxes serving G rollouts, as the proposal says)? Proposal: follow-up, because recycle has its own scheduler and the gate semantics with reuse need their own design.
2. **`claim_mode="batch"` required for `wave=True`?** Proposal: yes, so there's one source of batch settings.
3. **`wave_size > concurrency`:** validate (error) or warn on the `work_budget` risk? Proposal: warn, since the expected task time isn't known up front.

## 9. What would make this design wrong

- Direct `fleet.run` users need group results before the run ends, for example to train per group. Then the right API is a streaming `fleet.run_iter()` that yields per group (about 100 more lines), not a flag. It isn't in v1, so add it when a caller asks.
- GKE shows per-batch precheck or Lease cost dominating GROUP's rolling batches. Then GROUP should use back-to-back waves like QUORUM, or the SDK precheck gets parallelized first.
- The audit finds that `_handle_for_member` can't be extracted without changing `acquire`'s teardown-race guarantee (`fleet.py:1434-1456`).
