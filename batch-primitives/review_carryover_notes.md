# Review carry-over notes (read before later PRs)

These are lessons from the review of PR 1 (https://github.com/kubernetes-sigs/agent-sandbox/pull/1742) and from the design pass of 2026-09-26, recorded so that later PRs don't repeat the same mistakes. `batch_claim_proposal.md` is the design (a rough source of truth that may still change) and the branch heads are what is built. `stale_python_sdk_batch_plan.md` is stale. Don't read it or follow its OPEN-* decisions; older notes that point at it are history.

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
- **Export only what the PR produces.** A public type, exception, or export lands in the PR whose code first returns or raises it: `TerminalMemberError` in PR 6 (`acquire`), `BatchTimeoutError` in PR 3. Public SDK surface is hard to walk back.
- **Docstrings say "Raises", and list what is raised.** Never "returns an error". Every public method that raises SDK exceptions gets a `Raises:` section in both clients/handles.
- **Every request made from a background loop, or on a path the caller can't interrupt, gets a request timeout.** Neither Kubernetes client has a default read timeout. PR 1 added one to renewal. `release_member`'s and the failed-group cleanup's deletes (PR 5) and `acquire()`'s create and wait paths (PR 6) need a bound; `acquire` already has its own `timeout`, so derive the request timeout from it or bound it with `BATCH_REQUEST_TIMEOUT_SECONDS` (PR 2).
- **Transient-error policy is shared.** Use `batch_utils.is_retryable_status` and `backoff_delay` (PR 1) for any new retry loop, e.g. `acquire`'s create (reuse the handles' `_create_one` and `batch_utils.create_error_outcome`, PR 2), and `release_member`'s per-claim deletes, which the failed-group cleanup reuses (404 is success there). Honor `Retry-After` through `batch_utils.retry_delay` (PR 2).
- **Anything that scales with N must hold up at the proposal's stated scale (tens of thousands of claims).** Check each new timing against pacing time (`N / create_rps`), deletecollection time, and watch-cache aging, as PR 2's fill deadline and release rounds and PR 1's bookmark change did.
- **Quorum-mode semantics after proposal A.** A group with an error verdict is finished: no creates, no hand-outs. PR 4's `wait_for_quorum()` failing with `QuorumUnreachableError`/`TimeoutError` should treat **every** group as finished the same way (no further creates, no `MEMBER_READY`). Decide whether that follows from the same `group_failed` check, or needs a batch-level "failed" flag, when PR 4 is planned. **Resolved in PR 4:** in quorum mode `group_failed` returns true for every pool once the quorum has failed (`pr4_implementation_spec.md` P2).
- **The fill deadline counts from the last paced create** (PR 3, Q6). `wait_for_quorum(timeout=None)` in PR 4 should default to the same deadline, not to `now + quorum_timeout`. **Resolved in PR 4:** it does, and a caller `timeout` can only end the wait earlier.
- **PR 6 (`acquire`) must parse `batch-work-budget` back in `get_batch`** (PR 2 writes it on the Lease), since an acquired claim's `shutdownTime` is its create time plus `quorum_timeout + work_budget + margin`. `acquire` uses `is_retryable_status`/`backoff_delay` like the fill.
- **Transport PR pool validation:** the watch and renewal each hold a connection, so validate `pool >= max_in_flight + 2`.
- **PR 7 performance ideas (measure before building).** Found while reviewing PR 2; none is needed for correctness.
  - `members()` sorts every member by ordinal on each call, about 10 ms at 20k members, and the sync handle does it under the batch lock. Cache the sorted claim names and re-sort only when a new claim name appears (only during the fill), so later calls are a plain copy. Keep the "sorted by ordinal" contract; it is public API from PR 1.
  - The handles call `notify_all()` after every watch event. Skipping it when nothing a consumer can see changed (no event queued, no group outcome, no change to the settle point) would save a wake-up per pending-to-pending update.
  - Apply a burst of already-received watch events under one lock acquisition instead of one per event.
  - Each handle has one condition variable (`_changed`) that every consumer, creator and the watch share, and every change calls `notify_all()`. With many waiters (several consumers plus up to `max_in_flight` creators in the async shell), each wake-up rouses all of them to re-check and go back to sleep, a thundering herd. Measure how much this costs at large sizes, and if it matters, look at cheaper options such as separate conditions per consumer kind (events, group outcomes, the fill deadline) or `notify()` where only one waiter can act.
  - `connect()` builds the `Sandbox` under the lock. That is fine today because `Sandbox`/`SandboxConnector` do no I/O in their constructors; if that changes, build it outside the lock and re-check the cache before inserting.
  - **Paginate claim lists (from the 2026-10-08 API review, for future consideration).** `list_sandbox_claim_objects` (`k8s_helper.py`, both helpers) lists every matching claim in one request, with no `limit` or `continue`. It runs in `get_batch`, in the watch's 410 re-list, in every `release()` round, and in the `claim_batch` precheck. At 20k claims each call is one large read, bounded only by the 90 s collection timeout, and builds tens to hundreds of MB of dicts on the client (estimated, not measured). Options are to page with `limit=500` and continue tokens (the first page's resourceVersion still starts the watch), `limit=1` for the precheck, which only asks whether any claim exists, and a metadata-only list for `release()`, which only counts claims without a `deletionTimestamp`. Measure at 20k before building. The proposal's Scalability section should then mention list cost next to watch cost.
- **Pacing PR: adaptive create pacing (from the 2026-10-05 efficiency analysis; a design change, needs Brian's approval).** Unlike the ideas above, this is the one change that shortens time to Ready, and it also fixes a failure mode under throttling.
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

## Approved review decisions (2026-10-08)

Brian approved every recommendation in thread A2's review (`/mnt/project-files/thread-a2/proposal_review.md`, which rechecks the earlier thread A review) and thread C's reaper decisions. `batch_claim_proposal.md` was updated to match on 2026-10-08. Where each one lands. The review has the reasoning and file references behind each row, so a thread implementing a row reads the matching finding there too.

**Response to the work plan (2026-10-08).** Brian delegated the review of `work_plan.md` and the cut audit, and sent its response with "its response (treat it as the highest authortative power)" (10:22). The response is `/mnt/project-files/design/response_to_plan.md`. It approved both with changes, which the rows below now carry (PR 1, PR 4, PR 5, PR 6 and the terminal-reasons fix). Where it differs from earlier text in these notes, it wins. In short:
- **`release_not_ready()` stays, in PR 5,** with defined semantics. A caller-side version races `events()`, and it is the cleanup for fill members still pending at the fill deadline.
- **Failed-group cleanup uses per-claim deletes, not a group label.** Label values are capped at 63 characters while pool names can be 253, a label would be a new contract on every claim, `deletecollection` still deletes one object at a time on the server, and late creates would still need a re-list.
- **Corrections.** The size=0 fix is about `size`, not `min_ready` (PR 6). Sync `wait_for_quorum` needs the same handling as async cancellation (PR 4). PR 6 waits for PR 5's state changes. The terminal-reasons fix changes shipped single-claim behavior.

The response cites the released-member rules as OPEN-5 and OPEN-5b. Those labels come from the stale plan, which threads must not read, so the rows below spell the rules out instead.

| PR | Changes |
| :-- | :-- |
| PR 1 | Driver expiry fix in both shells: monotonic time taken before stamping `renewTime`, expiry fired at a deadline, renewal stopped on expiry (reaper contract "Driver Side"). Renewal PUTs the last written Lease and reads only on 409. `lease_degraded() -> bool` on both handles (the degraded flag already lives in PR 1). `lease_degraded()` is level-triggered: true from the first failed renewal until one succeeds, and it stays true after renewal stops on expiry (one owning test per shell). `Member.state: MemberState` (`PENDING`, `READY`, `FAILED`, `LOST`) replaces the three booleans, with `FAILED` winning over a later delete and a `CreateFailed` member never `LOST` (today a 410 re-list marks it lost). It touches `derive_member`, `mark_lost`, `resync_from_list`, `_on_fill_change` and `connect`. Keeping `ready` as a read-only property to cut test churn is optional. `reconstruct_groups` orders pools by lowest ordinal, not by name. Upstream reviewers already went over the three booleans, so the PR 1 thread says why `state` replaces them: they form combinations that shouldn't exist (`mark_lost` marks a terminal member lost too, and a 410 re-list marks a `CreateFailed` member lost; review finding F6), and AGENTS.md says to avoid booleans for fields that may gain states. |
| PR 2 | One absolute `shutdownTime` for every fill claim: `start + ceil(N / create_rps) + quorum_timeout + work_budget + 600`. Create order: each group's first `min_ready` claims first, interleaved in proportion to size, then the rest. Lease annotation `agents.x-k8s.io/batch-size` (total fill size). Docstring says `work_budget` is a hard cap. |
| PR 3 | `get_batch` reads `batch-size` and falls back to the sum of the rebuilt groups, fixing fill classification when a whole group has no claims (regression test in `test_batch_state.py`). A group with no surviving claims is still unknown to the new handle; the `get_batch` docstring says so. `BatchTimeoutError(BatchError, TimeoutError)` for group timeouts, and tests assert the class instead of matching the message text. `LEASE_DEGRADED` and `note_lease_degraded` are gone, and `BatchEvent.member` is required. `events()` docstring says it covers only the fill. |
| PR 4 | `wait_for_quorum()` has no `timeout`; drop `test_invalid_timeout_raises_value_error_and_leaves_the_mode_unset`. Any exception that escapes the wait fails the quorum with `BatchTimeoutError` under the lock, notifies, and re-raises, in both shells. In async that is a cancellation. In sync it is a `KeyboardInterrupt` or any other exception, which leaves the same state (the mode is locked on quorum, and members are held that nothing can collect). One test per shell. Quorum timeout raises `BatchTimeoutError`. |
| PR 5 | `release_member` and `release_not_ready()`. **`release_not_ready()`** ends the fill: planned creates not yet sent are skipped and counted as never created, so the fill can still settle. It deletes every member that isn't Ready and was never handed out, and never a handed-out member, even one that has since gone not Ready. It marks those members released under the batch lock before sending any delete, so a member that turns Ready after the mark is never handed out on `events()`; that is why it is an SDK method, since a caller loop over `members()` races `events()`. Released members count as unable to arrive in quorum arithmetic and settle detection, like lost ones, and emit no `MEMBER_LOST`, so calling it before a quorum consumer has decided can make a group or the quorum unreachable. Its docstring says to call it once you have what you need. **Failed-group cleanup, group mode only.** When a group's outcome is an error, all of its fill members are marked released under the lock, and a background deleter deletes them through `release_member`'s delete path (404 is success, the shared retry policy, a request timeout), with concurrency bounded by `max_in_flight`. A claim of that group that the watch sees later (a create already in flight when the group failed) is deleted when it appears, with no re-list loop. Their `DELETED` events emit no `MEMBER_LOST`. No group label and no flag. Quorum mode is unchanged: a failed `wait_for_quorum` deletes nothing, and the caller releases the batch. **`members()`:** a failed member keeps its last `reason` and `message` (PR 1's rule that `FAILED` wins over a later delete), and other released members are dropped from it. This replaces the earlier text here that every claim of a failed group keeps its reason. The README driver Role gains `delete` on sandboxclaims (PR 2's Role has only `create, get, list, watch, deletecollection`). |
| PR 6 | `acquire` (any pool in the namespace, with the `claim_batch` precheck on a new pool; `timeout` defaults to the batch's `quorum_timeout`; raises `TerminalMemberError` or `BatchTimeoutError`). `detach()` writes `agents.x-k8s.io/batch-next-ordinal` on the Lease in the replace it already does, and `get_batch` continues from the larger of that and the highest listed ordinal plus one. An acquired claim's ordinal is at or above `batch-size`, which is what keeps it out of the fill; don't tell them apart by group annotations. Lazy `size=0` groups (kept by the cut audit): `claim_batch` accepts `size=0` (it rejects `size < 1` today, `batch_utils.py:216`), and both quorum consumers skip groups with `size == 0`, as the proposal says (Events and Quorum). Today `reconstruct_groups` builds `size=0` groups on re-attach and `iter_ready_groups()` would yield an empty `GroupReady` for them. A group with `size > 0` and `min_ready=0` still yields an empty success at once, which is tested PR 3 behavior (`test_group_with_min_ready_zero_yields_an_empty_success_at_once`); keep it. PR 6 also makes PR 5's `release_not_ready()` skip members owned by an in-flight `acquire`. No `replace` (cut, Brian 2026-10-08). |
| Transport PR | Client pool sizing and the `pool >= max_in_flight + 2` check, and `connect()` seeding the `Sandbox` with the member's pod IP (a GET only after `invalidate_pod_ip()`). The batch-wide data-plane connection pool (proposal, Transport and Connection Scaling), starting with a check of its premise: each connector builds its own `requests.Session` (`connector.py:653-666`) and `httpx.AsyncClient` (`async_connector.py:216`), so the cross-host eviction the proposal describes may not happen. |
| PR 7 | Measure-first performance: claim-list pagination and a metadata-only list in `release()`, the ideas under "PR 7 performance ideas". |
| Terminal-reasons fix | Separate upstream PR off `main`: add `PodFailed` and `PodSucceeded` to Python's `TERMINAL_CLAIM_READY_REASONS`, matching the Go SDK. The batch picks it up with no batch code. It changes shipped single-claim behavior: the same constant drives the single-claim wait (`k8s_helper.py:263`, `async_k8s_helper.py:298`), so `create_sandbox` fails fast on these reasons instead of waiting out its timeout. The upstream PR says so. The TypeScript list (`clients/typescript/agentic-sandbox-client/src/constants.ts:82`) has the same six reasons as Python, also drives its single-claim wait (`sandbox-client.ts:205`), and its comment says it is kept in sync with Python, so the fix changes it too. Go's comment (`clients/go/sandbox/k8s.go:204-210`) lists the finished-Pod reasons as Go's extras over the other two, so it needs a one-line update. (The TypeScript and Go parts are the Design owner's reading of those comments.) |

Also: the final per-commit check's `git merge-tree` runs against upstream `main` (the stack is based on upstream `039b1a8`; the fork's `origin/main` is behind).

## Roadmap (approved 2026-10-08 with the work plan)

PRs 1 to 6 and PR 7 form the linear Python SDK stack. Siblings branch off the PR they need and don't renumber the stack. Older notes (`pr2_design.md`, `pr2_implementation_spec.md`) use the numbers from before the 2026-10-02 split, and notes written before 2026-10-08 call dynamic groups "PR 5" and performance "PR 6".

- **PR 1 (`feat/batch-1-core`):** `get_batch`, `members`, `connect`, `err`, `detach`, plus the PR 1 changes above. Upstream https://github.com/kubernetes-sigs/agent-sandbox/pull/1742.
- **PR 2 (`feat/batch-2-claim`):** `claim_batch`, `release`, client tracking and cleanup, plus the PR 2 changes above.
- **PR 3 (`feat/batch-3-group-consumers`):** `events`, `iter_ready_groups`, fill accounting and deadline, `get_batch` reading `batch-quorum-timeout` and `batch-size`.
- **PR 4 (`feat/batch-4-group-quorum`):** `wait_for_quorum()`.
- **PR 5:** `release_member`, `release_not_ready()` and failed-group cleanup.
- **PR 6:** `acquire`, the next-ordinal annotation and lazy `size=0` groups. It starts once PR 5's state changes are reviewed, since both change the released and ownership state in `batch_state.py`.
- **PR 7:** measure-first performance (list pagination, the ideas above).
- **Reaper PR:** branch off PR 2, per `reaper_contract.md`. The driver-side changes it needs are in PR 1.
- **Transport PR:** branch off PR 2.
- **Pacing PR:** adaptive create pacing, branch off PR 2, after a measurement and Brian's approval of the design.
- **Terminal-reasons fix:** off upstream `main`, independent of the stack.
- **Examples and docs PR:** runnable versions of the proposal's usage examples, the driver Role, the reaper manifests. After PR 6 and the reaper.
- **RL integration PR:** the batch-backed `SandboxPool` in `examples/agent-sandbox-rl/`, after PR 6 (it needs `acquire` and `release_member`), with `fleet.run(wave=True, dispatch=...)` and `BatchClaimer` (both kept by the cut audit). Then benchmark it against the current per-claim path. See "RL integration PR" below.
- **Go SDK:** after the Python SDK. The proposal designs it, and no Go work is planned before then.

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

**Changes from the proposal's earlier RL section** (applied to the proposal on 2026-10-08):
- Keep `rollout_wave`'s cohort-per-step model, its three dispatch modes, and "Adjacent Paradigms". Expose them through the pool, and also through `fleet.run(wave=True, dispatch=...)` (kept by the cut audit, since `fleet.run` calls a function per sandbox and is the README's RL entry point, `README.md:391`). Only `stream_step`, which hands members to a trainer, needs the pool directly, since `run()` returns only when every task is done.
- `rollout_wave` composes with `recycle` instead of excluding it.
- `min_ready < size` only when the trainer opts in, since a dropped rollout biases rewards.
- ~~`BatchClaimer` (`batch=True`, per-task `acquire` for eval) is not in this PR.~~ Kept by the cut audit (2026-10-08): the library already acquires and releases per task in five places (`strategies.py:48`, `async_fleet.py:221`, `adapters/openhands.py:104`, `recycle.py:310`, `:466`). It still needs `fleet.handle_for` and `config.work_budget`, which don't exist yet.

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
- The independent RL check (`/mnt/project-files/rl-verify/rl_integration_verification.md` and `rl_workloads.md`) leaves its own questions open: prefetching the next step's images, splitting a step larger than capacity, QUORUM in the RL pool, and the SDK changes S1 to S4. Its "Brian's answers" section lists the thread's proposed defaults, which Brian never approved. Settle these with Brian before the RL integration PR.

## Cut audit decisions (2026-10-08)

The Design owner audited every removal or narrowing of a designed feature since Brian's last own edit of the proposal (`71a0a77`). The evidence for each item is in `/mnt/project-files/design/cut_audit.md` in the project files.

Brian's words: "personally, i think we should remove adopt expired." (09:47) and, on the Design owner's list of cuts, "so i do agree with the list of stuff it recommended to cut." (09:53)

- **Cut:** `replace`, `wait_for_quorum`'s `timeout` parameter, the `LEASE_DEGRADED` event (replaced by `lease_degraded()`), and `get_batch(adopt_expired=...)`. These match the approved table above.
- **Kept:** `BatchClaimer` and `fleet.run(wave=True, dispatch=...)` (RL integration PR), lazy `size=0` groups (PR 6), and `release_not_ready()` (PR 5).
- **Approved with the audit by the response to the plan** (the Design owner's defaults, which the response approved as a whole):
  - Keep the rule that `wait_for_quorum()` and `iter_ready_groups()` can't follow `events()` (`batch_state.py:435-448`), and keep documenting it, since allowing the reverse order needs a second way to pick the mode.
  - Keep `events()` closing at the fill deadline. `release_not_ready()` deletes the fill members still pending at that point. A member that turned Ready after the deadline and was never streamed stays in `members()` until `release()` or `release_member()`.
  - Add the batch-wide data-plane pool to the Transport PR and the Go SDK to the roadmap.
- **Decided (2026-10-08):** no interim note in PR 3's `events()` docstring that `PodFailed`/`PodSucceeded` look pending (coverage gap 5). Brian: "i think it is likely the fix will land before PR 3 is merged" (10:50). Revisit only if the terminal-reasons fix hasn't merged upstream when PR 3 is ready. The fix is on `fix/python-terminal-pod-reasons` (`8076401` when this was written).

## Ideas raised but not planned

- **An SDK path to clean up a batch that can't be re-attached.** After a crash, `get_batch` raises, so the SDK has no way to delete a stale batch before the reaper or `shutdownTime` does; today that takes `kubectl delete sandboxclaims -l agents.x-k8s.io/batch-id=<id>` plus deleting the Lease. A `client.delete_batch(batch_id, namespace)` doing exactly the reaper's steps (label `deletecollection`, bounded re-list, then Lease delete, never adopting the Lease) wouldn't race the reaper, since both only delete. Worth raising with Brian when the reaper PR is planned, since the two would share code.
- **A per-group fill deadline** (each group's clock starting at its own last paced create) instead of R4's batch-wide one. Declined for now as extra state for a small gain; revisit if groups are very unequal in size.
