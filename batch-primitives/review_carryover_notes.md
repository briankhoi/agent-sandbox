# Review carry-over notes (read before later PRs)

These are lessons from the review of PR 1 (https://github.com/kubernetes-sigs/agent-sandbox/pull/1742) and from the design pass of 2026-09-26, recorded so that later PRs don't repeat the same mistakes. `python_sdk_batch_plan.md` is still the spec, and its "As built" section wins where the two differ.

## Reviewer comments on PR 1 and how they were handled

### Aditya Shantanu

1. **"A crashed holder's stale Lease can never be re-attached; is that intended?"** Yes, by design (OPEN-N, OPEN-C). The code is unchanged. The `get_batch` docstrings now say only a detached batch can be re-attached; at Brian's request, the README and docstrings don't mention the reaper or `shutdownTime`, which PR 1 doesn't have. Reply to paste:

   > Intended. A crashed driver's batch is meant to be cleaned up rather than resumed. Once its Lease is stale, a reaper (planned as a follow-up PR) may already be deleting it, so adopting it in `get_batch` could hand the caller a batch that's being deleted underneath it. That's why the stale check runs before the holder check. Re-attaching is for planned handoff: `detach(grace)` clears `holderIdentity` and keeps the Lease live for `grace` seconds, and `get_batch` adopts it within that window. A crash while the Lease is still live gives `BatchInUseError`, since every handle has its own holder identity. The `get_batch` docstrings now say only a detached batch can be re-attached.

2. **"`BatchEvent`, `BatchEventType`, `GroupReady` are exported but nothing produces them."** Valid. They move to PR 2, where `events()`/`iter_ready_groups()` land (PR 1 item 5, PR 2 item R1). Reply:

   > Agreed. Moved them out of this PR; they're added with `events()`/`iter_ready_groups()` in the next PR of the stack.

   Since the 2026-10-02 split, `events()`/`iter_ready_groups()` land in PR 3, not the next PR. If this reply is already posted, it's off by one PR; a short follow-up on the thread can correct it.

3. **"'Returns an error' is inaccurate in `get_batch`."** Valid. Both clients now have a `Raises:` section (PR 1 item 6). Reply:

   > Fixed in both clients; the docstrings now list the exceptions `get_batch` raises.

### CodeRabbit

Checked on 2026-09-26: every CodeRabbit finding is already handled in `0f7a03f` except two, which are declined:
- **Make `GroupReady` a frozen pydantic model with `arbitrary_types_allowed`.** Declined (OPEN-O). It holds a raw `Exception`, which pydantic can only carry by switching off validation for that field, so a frozen dataclass is the simpler fit; the class docstring says why. The class moves to PR 2 anyway. Reply, if the thread is still open:

  > Keeping it a frozen dataclass: `error` holds a raw `Exception` (traceback and cause intact), which pydantic can only hold with `arbitrary_types_allowed`, i.e. without validating it, so a model would add nothing. The docstring notes the reason.

- **Remove `deletecollection` from the driver Role.** Agreed (2026-09-26). The README Role now lists only PR 1's verbs (claims `get`/`list`/`watch`, leases `get`/`update`), and PR 2 adds the rest with `claim_batch`/`release()`. Reply, if the thread is still open:

  > Agreed. Trimmed the Role to the verbs this PR uses; the next PR adds `create`/`delete`/`deletecollection` along with `claim_batch()` and `release()`.

## Rules to carry into later PRs

- **Tests follow `skills/test-audit/SKILL.md`.** Each contract gets one owning test at the strongest boundary. Don't repeat a `batch_utils` or `batch_state` table at the handle level; one representative value there checks the wiring. Don't add a test that exercises the same code path as an existing one with a different exception type or status. Run the audit on each PR before handing it over.
- **A PR describes and contains only itself.** Comments, docstrings, README text, and RBAC verbs mention only what the PR has; a later feature appears only when the text says explicitly that it comes later. Likewise, no code (functions, fields, constants) whose only caller lands in a later PR: move it to the PR that calls it. Brian's rule, after the PR 1 review.
- **Export only what the PR produces.** A public type, exception, or export lands in the PR whose code first returns or raises it: `TerminalMemberError` in PR 5, and nothing new for PR 4 unless `wait_for_quorum` needs it. Public SDK surface is hard to walk back.
- **Docstrings say "Raises", and list what is raised.** Never "returns an error". Every public method that raises SDK exceptions gets a `Raises:` section in both clients/handles.
- **Every request made from a background loop, or on a path the caller can't interrupt, gets a request timeout.** Neither Kubernetes client has a default read timeout. PR 1 added one to renewal. In PR 5, `acquire()`'s create and wait paths and `release_member`/`release_not_ready` deletes need a bound; `acquire` already has its own `timeout`, so derive the request timeout from it or bound it with `BATCH_REQUEST_TIMEOUT_SECONDS` (PR 2).
- **Transient-error policy is shared.** Use `batch_utils.is_retryable_status` and `backoff_delay` (PR 1) for any new retry loop, e.g. `acquire`'s create (reuse the handles' `_create_one` and `batch_utils.create_error_outcome`, PR 2), and `release_not_ready`'s per-claim deletes (404 is success there). Honor `Retry-After` through `batch_utils.retry_delay` (PR 2).
- **Anything that scales with N must hold up at the proposal's stated scale (tens of thousands of claims).** Check each new timing against pacing time (`N / create_rps`), deletecollection time, and watch-cache aging, as PR 2's fill deadline and release rounds and PR 1's bookmark change did.
- **Quorum-mode semantics after proposal A.** A group with an error verdict is finished: no creates, no hand-outs. PR 4's `wait_for_quorum()` failing with `QuorumUnreachableError`/`TimeoutError` should treat **every** group as finished the same way (no further creates, no `MEMBER_READY`). Decide whether that follows from the same `group_failed` check, or needs a batch-level "failed" flag, when PR 4 is planned. **Resolved in PR 4:** in quorum mode `group_failed` returns true for every pool once the quorum has failed (`pr4_implementation_spec.md` P2).
- **The fill deadline counts from the last paced create** (PR 3, Q6). `wait_for_quorum(timeout=None)` in PR 4 should default to the same deadline, not to `now + quorum_timeout`. **Resolved in PR 4:** it does, and a caller `timeout` can only end the wait earlier.
- **PR 5 must parse `batch-work-budget` back in `get_batch`** (plan "As built", PR 2 item 7), and `acquire` uses `is_retryable_status`/`backoff_delay` like the fill.
- **PR 6 pool validation** (OPEN-I): the watch and renewal each hold a connection, so validate `pool >= max_in_flight + 2`.
- **PR 6 performance ideas (measure before building).** Found while reviewing PR 2; none is needed for correctness.
  - `members()` sorts every member by ordinal on each call, about 10 ms at 20k members, and the sync handle does it under the batch lock. Cache the sorted claim names and re-sort only when a new claim name appears (only during the fill), so later calls are a plain copy. Keep the "sorted by ordinal" contract; it is public API from PR 1.
  - The handles call `notify_all()` after every watch event. Skipping it when nothing a consumer can see changed (no event queued, no group outcome, no change to the settle point) would save a wake-up per pending-to-pending update.
  - Apply a burst of already-received watch events under one lock acquisition instead of one per event.
  - Each handle has one condition variable (`_changed`) that every consumer, creator and the watch share, and every change calls `notify_all()`. With many waiters (several consumers plus up to `max_in_flight` creators in the async shell), each wake-up rouses all of them to re-check and go back to sleep, a thundering herd. Measure how much this costs at large sizes, and if it matters, look at cheaper options such as separate conditions per consumer kind (events, group outcomes, the fill deadline) or `notify()` where only one waiter can act.
  - `connect()` builds the `Sandbox` under the lock. That is fine today because `Sandbox`/`SandboxConnector` do no I/O in their constructors; if that changes, build it outside the lock and re-check the cache before inserting.
  - **Paginate claim lists (from the 2026-10-08 API review, for future consideration).** `list_sandbox_claim_objects` (`k8s_helper.py`, both helpers) lists every matching claim in one request, with no `limit` or `continue`. It runs in `get_batch`, in the watch's 410 re-list, in every `release()` round, and in the `claim_batch` precheck. At 20k claims each call is one large read, bounded only by the 90 s collection timeout, and builds tens to hundreds of MB of dicts on the client (estimated, not measured). Options are to page with `limit=500` and continue tokens (the first page's resourceVersion still starts the watch), `limit=1` for the precheck, which only asks whether any claim exists, and a metadata-only list for `release()`, which only counts claims without a `deletionTimestamp`. Measure at 20k before building. The proposal's Scalability section should then mention list cost next to watch cost.
- **PR 6 adaptive create pacing (from the 2026-10-05 efficiency analysis; a design change, needs Brian's approval).** Unlike the ideas above, this is the one change that shortens time to Ready, and it also fixes a failure mode under throttling.
  - **Why.** At the default `create_rps=50`, 20k claims take 400 s to send. The cluster absorbs much more when the warm pools already hold the claims: about 300 warm adoptions/s per cluster (`docs/performance-tuning.md`). When claims need new pods, the limit is about 70 pods/s at kube-scheduler's default QPS (`sandboxwarmpool_controller.go` sizing comment). So pacing is the limit by about 6x in the warm case and about 1.4x in the cold case. One client is not the limit: against a fake apiserver with 50 ms creates, it measured 330–370 creates/s at `max_in_flight=20` and 680–1040/s at 100.
  - **What.** Pace from the cluster's own signals instead of a fixed rate. On a 429 (honoring `Retry-After` through `batch_utils.retry_delay`) or rising create latency, slow the shared pacing slot for every worker, then speed up again while creates succeed. Today a 429 only delays the worker that got it; retries don't take a pacing slot, and after `BATCH_CREATE_ATTEMPTS` the claim becomes `CreateFailed`, which can fail a group in quorum mode. Under adaptive pacing, a 429 should not use up attempts while the fill deadline allows. Client-only: no controller or API change.
  - **Defaults.** Re-derive `create_rps` and `max_in_flight` after a real-cluster run, with `max_in_flight` of about `create_rps × p99 create latency`. The pool check above (`pool >= max_in_flight + 2`) should also cover the sync client: its urllib3 pool (`cpu_count() * 5`, non-blocking) discarded 247 connections in the `max_in_flight=100` run. The async client's aiohttp connector caps in-flight requests at 100.
  - **Fill deadline.** The deadline counts from the last create's pacing slot, so faster pacing starts the `quorum_timeout` clock earlier while cold claims still become Ready at about 70/s (50k cold claims need about 714 s, more than the 600 s default). Either document that faster pacing shortens the effective `quorum_timeout`, or tie the deadline to expected readiness.
  - **Not worth building:** unpaced creates and sharding creates across clients (one client is already above the cluster's limits), and server-side bulk creation for time to Ready (the controller's 3–6 writes per adoption and the scheduler still set it; the proposal lists a batch CRD as a non-goal).
  - **Measured vs. not.** The client numbers are from a local fake apiserver without TLS (scripts not committed). The cluster numbers come from the repo's own benchmarks. Confirm both on a real cluster before changing defaults.

## Deferred from PR 1: must come back in a later PR

**Resolved in PR 2** (`feat/batch-2-cohorts` at `8e757e4`): both clients track `claim_batch` and `get_batch` handles, `delete_all()` and exit cleanup release them, and `detach()`/`release()` unregister them. The cleanup test covers every tracked handle in both clients. Kept below for the record.

- **Sync client batch tracking and cleanup.** PR 1 removed the sync `SandboxClient._active_batches` registry and `_unregister_batch` (and `SandboxBatch.detach()`'s call to it) because nothing read them yet. The async client kept its registry, since `AsyncSandboxClient.close()` stops tracked handles' background tasks.
  - **Where:** the PR that first adds client-level cleanup of batch handles. That is PR 2 if `claim_batch`/`release()` bring exit or close cleanup (e.g. an `atexit` hook, `delete_all`, or a sync `close()`); otherwise whichever later PR does.
  - **What:** add the sync registry back with parity to async:
    - register in `get_batch`/`claim_batch`;
    - unregister when a handle finishes (`detach()`/`release()`);
    - make the client's cleanup path act on every tracked handle.

    Decide per path whether cleanup means stopping background loops only, as async `close()` does today, or releasing or detaching the batch.
  - **Tests:** one test that the cleanup path reaches every tracked handle, in both clients. Don't bring back PR 1's old registers/unregisters test, which only checked a dict.

## Roadmap (Brian, 2026-09-26; split and renumbered 2026-10-02)

On 2026-10-02 Brian split the old PR 2 (`feat/batch-2-cohorts`, now kept on the fork as `stale/batch-2-cohorts` at `806cdeb`) into PR 2 and PR 3, so every later PR moved up by one. Older notes (the plan's PR sections, `pr2_design.md`, `pr2_implementation_spec.md`) use the old numbers.

- **PR 2 (`feat/batch-2-claim`):** `claim_batch`, `release`, and client batch tracking and cleanup for both clients (resolves "Deferred from PR 1"). A claimed batch exposes its members through `members()` only.
- **PR 3 (`feat/batch-3-group-consumers`):** `events`, `iter_ready_groups`, the fill accounting, the fill deadline, `LEASE_DEGRADED`, and `get_batch` reading `batch-quorum-timeout`. Its tree equals the old PR 2. Spec for both: `pr2_implementation_spec.md`.
- **PR 4 (`feat/batch-4-group-quorum`):** `wait_for_quorum()`. Spec: `pr4_implementation_spec.md` (approved 2026-10-02 as the PR 3 spec).
- **PR 5:** dynamic scaling and replacement: `acquire`, `replace`, `release_member`, `release_not_ready`, and lazy `size=0` groups.
- **PR 6:** connection pool sizing enforced against `max_in_flight`, plus performance tuning.
- **Proposed PR 7 (cleanup):** the reaper (a stateless CronJob consuming the Lease contract; owns OPEN-V's margin). Possibly also `client.delete_batch(batch_id, namespace)` for a batch that can't be re-attached (see "Ideas raised but not planned"), since it would share the reaper's delete steps.
- **RL integration PR (number set by the roadmap reorder):** a batch-backed `SandboxPool` in `examples/agent-sandbox-rl/`. It needs `acquire` and `release_member`, so it comes after dynamic groups. See "RL integration PR" below.
- **Proposed PR 8:** examples and docs (`examples/<name>/`, runnable versions of the proposal's usage examples, the driver Role).

## RL integration PR (Thread B review, 2026-10-08)

Brian approved the recommended direction on 2026-10-08; the open decisions are listed at the end. The evidence (file and line references into the RL library at upstream `cd0761d`, the blog post's numbers) is in `/mnt/project-files/thread-b/rl_integration_direction.md` in the project files, not in this repo.

**What the library does today.**
- Each claim is a `create_sandbox` (one watch per claim) plus two Sandbox GETs for the pod name and IP, and each release is one delete. A claim from a warm pool makes the controller create a replacement pod.
- `recycle=True` reuses one sandbox per image only within one `run()` call. The sync recycler runs an image's tasks one after another in that sandbox; the async one runs `shards_per_image` sandboxes in parallel. With `scale_on_hold`, the sync recycler deletes the image's pool and template after the first claim and re-creates them on every rotation and quarantine.
- Every `run()` calls `setup()`, so a training step re-creates the pools recycle dropped, waits for them, and claims again.
- The blog credits reuse for the claim churn cut (18,312 to 5,869) and warm pools, image streaming and controller rate controls for the latency gains. Batch claiming changes neither image hydration nor warm-pool timing.

**Design: one `SandboxPool` (sync and async) per cluster, with two lifetimes.**
1. **Per-step cohort**, for training that samples new problems each step (standard GRPO). Each step claims a batch with one group per problem image, `size` set to that image's concurrent rollouts and `min_ready` defaulting to `size`, and releases it when the step ends. Dispatch is the proposal's `rollout_wave` modes: `iter_ready_groups()` by default (a GRPO group starts when its own sandboxes are Ready), `wait_for_quorum()` for joint-batch steps, `events()` for streaming trainers. This is where quorum is used. A batch per step is cheap (one Lease) and keeps the quorum consumers on the initial fill, the only members they cover.
2. **Held across steps**, for training that repeats problems. The batch stays claimed for the run, sandboxes are git-restored between episodes (`GitRestoreReset`), and quarantine and `max_reuses` rotation use `release_member` plus `acquire` on the same pool.

Both lifetimes compose with reuse inside a step: a group of `K` sandboxes can serve `G` rollouts, `G/K` each.

**Changes from the proposal's RL section** (proposal edits not made yet):
- Keep `rollout_wave`'s cohort-per-step model, its three dispatch modes, and "Adjacent Paradigms". Expose them through the pool instead of `fleet.run(wave=True, dispatch=...)`, since `run()` returns only when every task is done and can't stream.
- `rollout_wave` composes with `recycle` instead of excluding it.
- `min_ready < size` only when the trainer opts in, since a dropped rollout biases rewards.
- `BatchClaimer` (`batch=True`, per-task `acquire` for eval) is not in this PR. It keeps N creates and N deletes, and it calls `fleet.handle_for` and `config.work_budget`, which don't exist. It's a candidate for a later eval PR once measured.

**Rules for the PR.**
- Scale pools down after the fill with `set_pool_replicas`, never `unwarm_image`. `unwarm_image` deletes the pool and template, which a later `acquire` needs.
- `Member` has no pod name, and claim status doesn't mirror it. The RL handle's router-free `exec` gets it once per member through `connect(member).get_pod_name()` (one GET). No SDK change.
- Pass `fleet.config.labels` to `claim_batch`, so `reap(run_id=...)` and the circuit breaker still see batch claims. `teardown()` releases the pool's batches before its own sweep.
- At 18k members per batch, the pool keeps its own index of idle members and never calls `members()` per checkout.

**Inputs to the API review (Thread A).**
- Dynamic groups need only `acquire` and `release_member` for RL. `acquire` keeps its own `timeout` (the pool passes `ready_timeout`).
- A held batch outlives `events()` by hours, so a renewal problem has to be readable by polling (recommendation 2).
- The pool catches `BatchError` around the quorum consumers, so a quorum timeout should be one (recommendation 3).

**Open decisions (Brian).**
- Which lifetime first. That depends on whether the target trainers sample new problems each step. Thread B recommends the per-step cohort if they do.
- `shutdownTime` for a held batch. `work_budget` has to cover the whole run, which makes the reaper the only prompt crash cleanup. The alternative is to rotate members before their deadline, but `Member` doesn't expose it. Thread B recommends `work_budget` set to the run length for now.

## Ideas raised but not planned

- **An SDK path to clean up a batch that can't be re-attached.** After a crash, `get_batch` raises, so the SDK has no way to delete a stale batch before the reaper or `shutdownTime` does; today that takes `kubectl delete sandboxclaims -l agents.x-k8s.io/batch-id=<id>` plus deleting the Lease. A `client.delete_batch(batch_id, namespace)` doing exactly the reaper's steps (label `deletecollection`, bounded re-list, then Lease delete, never adopting the Lease) wouldn't race the reaper, since both only delete. Worth raising with Brian when PR 7 (reaper) is planned, since the two would share code.
- **A per-group fill deadline** (each group's clock starting at its own last paced create) instead of R4's batch-wide one. Declined for now as extra state for a small gain; revisit if groups are very unequal in size.
