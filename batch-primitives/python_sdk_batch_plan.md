# Python SDK Batch Claim: Implementation Plan (PRs 1-5)

This plan splits the Python SDK part of `batch-primitives/batch_claim_proposal.md` (the proposal) into five stacked PRs, following Brian's revised roadmap. The proposal is the source of truth for behavior; this plan fixes the shared contracts every PR relies on so that separate agents implementing separate PRs make the same choices. Where the proposal is silent or the roadmap conflicts with it, the item is marked **OPEN-x** with a recommendation. An agent must not start a PR until the OPEN items listed under that PR's "Blocked on" are resolved by Brian.

Base: `main` at `2d855f5` (upstream `kubernetes-sigs/agent-sandbox`). Every existing identifier below was checked against that commit. Identifiers marked *(new)* do not exist yet.

How an agent uses this file: read the proposal, then "Ground rules", "Module layout", "Shared contracts", "Decisions", and the section for your PR. Do not implement scope from other PRs.

## As built: where PR 2 departs from this plan

> PR 2 is being rebuilt from `pr2_implementation_spec.md`. The PR 2 items below describe the old PR 2 and will be replaced.

This plan was drafted before implementation and is partly stale. Where it and the code on `feat/batch-2-cohorts` disagree, the code and this section are the source of truth. Later PRs should read this section first.

1. **Quorum picks lowest ordinals, not earliest Ready.** `iter_ready_groups()` hands out the `min_ready` lowest-ordinal Ready members of a group. `BatchState` keeps no Ready-time sequence at all: `events()` emits in change order from one insertion-ordered stream (`_pending_changes`), with `LEASE_DEGRADED` queued into the same stream. Members already Ready when `get_batch` attaches are seeded in ordinal order, so OPEN-G's ordinal ordering still holds. When a group's verdict is an error, its held-back members enter the stream at that point, in ordinal order.
2. **No per-ordinal create outcome table.** The plan's "pre-allocated, ordinal-indexed table" of create outcomes is not built; it was write-only in production. Create failures are visible as synthetic `CreateFailed` members in `members()`.
3. **No watch-before-first-create gate.** Creation does not wait for the watch request to be issued. The watch resumes from the initial list's resourceVersion, so no status transition can be missed either way; `claim_batch` still starts the watch (`_start`) before it starts creation.
4. **Create fail-fast only in quorum mode.** Cancelling a group's remaining creates (OPEN-S) applies only once `iter_ready_groups()` has been called; a stream-only batch keeps creating after a failed create. A group that crossed the threshold before `iter_ready_groups()` was called has its remaining creates cancelled when it is.
5. **The dependency precheck skips on 403.** If the driver Role lacks `get` on `sandboxwarmpools` or `sandboxtemplates`, `claim_batch` logs a warning and skips that check instead of failing. A 404 still fails fast (OPEN-W).
6. **Bounded wait for in-flight creates.** The sync handle's `release()`/`detach()` waits at most `BATCH_STOP_CREATION_TIMEOUT_SECONDS` (30 s) for creates already sent, since urllib3 has no read timeout; the async handle cancels them. A create that lands after `deletecollection` is caught by the re-list rounds or by its `shutdownTime`.
7. **`get_batch` does not parse `batch-work-budget` yet (OPEN-E, deferred to PR 4).** `claim_batch` still writes both `batch-work-budget` and `batch-quorum-timeout` on the Lease. `get_batch` parses only `batch-quorum-timeout`, which sets a re-attached handle's fill deadline. In PR 2 a re-attached handle never creates claims, so it has no use for `work_budget`. **PR 4 must add the `batch-work-budget` parse back** (missing falls back to the default, present but not a positive integer raises `BatchError`) so that `acquire()`/`replace()` on a re-attached handle compute `shutdownTime` from the batch's own budget.
8. **Numeric defaults live in `batch_state.py`.** `constants.py` holds only wire and cluster names; every tuning value (defaults, timeouts, retry, pacing, loop bounds, `CLOCK_SKEW_MARGIN`) is defined at the top of `batch_state.py`. Argument and annotation validation lives in `batch_utils.py`.
9. **PR 1 revision (2026-09-26, `feat/batch-1-core` at `f4a4867`, rebased on upstream `68db683`).**
   - Lease renewal requests carry `_request_timeout` equal to the renew interval (`max(1, lease_duration // 3)`, stored as `_renew_interval`). A hung apiserver therefore counts as a failed renewal: degraded, then `BatchLeaseExpiredError`. `read_batch_lease`/`replace_batch_lease` take an optional `_request_timeout`.
   - `watch_sandbox_claims` requests bookmarks (`allow_watch_bookmarks=True`), so a quiet batch's watch resumes from a fresh resourceVersion instead of re-listing after a 410.
   - Watch retries back off. `batch_state.is_retryable_status` (None/429/5xx) and `backoff_delay(attempt, base, cap, rand)` (equal jitter, exponent bounded) are shared helpers, with `BATCH_WATCH_BACKOFF_BASE_SECONDS = 0.5` and `BATCH_WATCH_BACKOFF_MAX_SECONDS = 30.0`. A `failures` counter resets on any event or a normal watch end.
   - The 410 re-list now runs at the top of the watch loop behind a `relist` flag, inside the same `try`. A transport or unexpected error during the re-list is therefore retried or surfaced through `err()`; before, it killed the watch thread/task with `err()` still `None`.
   - Takeover sets `acquireTime` and increments `leaseTransitions`, as client-go leader election does.
   - `BatchEventType`, `BatchEvent` and `GroupReady` moved to PR 2.
   - `get_batch` docstrings have a `Raises:` section and say that only a detached batch can be re-attached.
   - `batch_utils.py` exists from PR 1 and holds the policy helpers: batch-id, holder-identity, and lease-duration validation; `is_lease_stale`; `is_retryable_status`; `backoff_delay`; and their constants (`CLOCK_SKEW_MARGIN`, `BATCH_DEFAULT_LEASE_DURATION_SECONDS`, the watch backoff values). `batch_state.py` is only claims → state (`parse_ordinal`, `reconstruct_groups`, `derive_member`, `BatchState`). This replaces item 8's "numeric defaults live in `batch_state.py`". `backoff_delay` stops doubling once the wait reaches `cap` (the exponent is derived from `cap / base`, with no magic bound).
   - `BatchState` pieces with no PR 1 caller moved to PR 2: `try_dispatch`/`_dispatched`, `mark_released`/`_released`, `compute_next_ordinal`/`_next_ordinal`, and `_initial_fill`.
   - The README RBAC Role lists only PR 1's verbs; each later PR adds the verbs it needs.
   - `reconstruct_groups` raises `BatchError` for invalid group annotation values (non-integer, negative, `min_ready > size`); before, these leaked `ValueError`/pydantic `ValidationError` out of `get_batch`.
   - The sync client has no batch registry: its `_active_batches`/`_unregister_batch` were never read. The async client keeps its registry for `close()`.
   - Test audit (test-audit skill): 18 duplicate tests removed; PR 1 now adds 168 unit tests.

## Pending: proposal A, a failed group is finished (approved, not yet implemented)

Status: approved by Brian on 2026-09-26. It is implemented as item R3 of `STALE_pr2_revision_prompt.md`, together with the other approved revisions (`pr1_revision_prompt.md` for PR 1). After it lands, move items 4 and the new behavior into "As built" above and delete this section.

State when written: `feat/batch-2-cohorts` at `df1c11d` (six commits on `feat/batch-1-core` at `0f7a03f`):
`4ad064e` constants/exceptions, `ab4cc08` k8s helpers, `b77ddca` batch state (commit 3), `8295a30` claim_batch + handles (commit 4), `2498b41` e2e, `df1c11d` docs. Re-fetch before starting; Brian may have rebased.

### Why

Calling `iter_ready_groups()` means a group is only useful with `min_ready` members together. Today, when a group's verdict is an error, its held-back Ready members are released to `events()`, later-Ready members of that group stream too, and creation continues unless the error came from create failures. That turns a failed cohort into loose sandboxes the caller never asked for, and keeps spending API calls and pods on a group that has already failed.

### Behavior (quorum mode only; stream-only mode is unchanged)

Once a group has an error verdict (`QuorumUnreachableError` for any cause: terminal, lost, create-failed, released; or `TimeoutError`):

1. **No more creates for that group.** Before each create the producer asks the state whether the claim's group has an error verdict, and skips it (`mark_create_cancelled`) if so. This replaces the create-failure-only threshold (OPEN-S), and late cancellation is automatic because `claim_groups_consumer()` already computes verdicts when quorum mode is fixed.
2. **None of its members are handed out.** No `MEMBER_READY` on `events()` for that group: not the held-back ones, not ones that become Ready later. Its `MEMBER_FAILED` and `MEMBER_LOST` still stream (status, not hand-outs). Its members stay visible in `members()`.
3. **Nothing is deleted.** Teardown stays the caller's `release()`.

A group with a successful verdict is unchanged: its extras beyond `min_ready` stream on `events()`. Stream-only batches keep creating after failures and deliver every member.

`events()` still closes as today: settled (or deadline) and, in quorum mode, every non-zero group has a verdict.

### Code changes

Commit 3 (`batch_state.py`):
- Delete `_past_create_failure_threshold`; `mark_create_failed` returns `None` (drop the bool and its docstring paragraph about cancelling); `claim_groups_consumer()` returns `None` (drop the pools list and its docstring sentence).
- Add a query the producer uses, e.g. `group_failed(pool) -> bool`: quorum mode and that group's verdict has an `error`.
- `_set_verdict`: delete the block that re-marks held members as changed on an error verdict (and its comment).
- `_is_held_for_quorum` becomes "withheld": in quorum mode a non-zero group's Ready members are withheld while it has no verdict **or** its verdict is an error. Rename to fit, e.g. `_is_withheld`.
- `collect_events()` done check is unchanged.

Commit 4 (`sandbox_batch.py`, `async_sandbox_batch.py`, README):
- Delete `_cancelled_pools` and `_cancel_remaining_creates`; `iter_ready_groups()` no longer uses a return value from `claim_groups_consumer()`; `_create_one` no longer acts on `mark_create_failed`'s return.
- `_skip_if_pool_cancelled` checks the state's `group_failed(pool)` under the lock instead of `_cancelled_pools`. Keep the info log once per group (log from the producer the first time it skips a group).
- Docstrings: `events()` and `iter_ready_groups()` in both handles no longer say held-back members are released to `events()` on an error; say a failed group's members are not handed out and remain in `members()`.
- README batch section: same wording change (the bullet that says "(and a group's held-back members, if the group yields an error)", and the fail-fast bullet, which becomes "once `iter_ready_groups()` has been called, a group that can no longer reach `min_ready` or times out gets no more creates and none of its members are handed out").

### Tests to change (both handles unless noted)

- State (`test_batch_state.py`): replace `test_create_failure_fail_fast_threshold`, `test_create_failures_never_ask_to_cancel_outside_quorum_mode`, `test_quorum_mode_cancels_groups_already_past_threshold`, `test_quorum_mode_with_no_failures_cancels_nothing` with tests of `group_failed()` (false outside quorum mode; true after unreachable and after timeout; false for a successful group). Flip `test_error_verdict_releases_held_members_to_events` to assert held members are **not** emitted and stay in `members()`; add: a member of a failed group that becomes Ready later is not emitted, while its `MEMBER_FAILED` is.
- Handles: `TestPerGroupFailFast` keeps its shape (creates for the failed group stop; the healthy group still fills; no deletecollection; Lease kept). `TestFailFastConsumerMode.test_entering_quorum_mode_cancels_a_group_already_past_its_threshold` stays, now driven by the verdict. Add a case where the group fails by terminal claims (not create failures) and its remaining creates are skipped. `test_stream_only_batch_keeps_creating_after_a_create_failure` unchanged. Flip both `test_error_verdict_releases_held_members_to_events` to assert only the `MEMBER_FAILED` events stream.

Report unit-suite counts before and after and explain every change in counts.

### Workflow rules (from Brian)

- Brian rewrote comments and docstrings to be concise: leave his wording alone; change a comment only where the code it describes changes, and then minimally.
- Fold each change into the commit that owns it: `git commit --fixup=<sha>` then `GIT_SEQUENCE_EDITOR=true git rebase -i --autosquash origin/feat/batch-1-core`. No new commits, no trailers, all commits authored by Brian Nguyen <brianknguyen@google.com> (set `git config user.name/user.email` if needed). Do not touch `feat/batch-1-core`.
- Verify before pushing: every PR 2 commit passes pytest and `mypy k8s_agent_sandbox` on its own (run from the package directory as `dev/tools/test-unit` does); pyflakes clean on changed files; `make generate-python-docs` with any change folded into the docs commit; `git log --format='%(trailers)'` empty.
- Push with `git push --force-with-lease=feat/batch-2-cohorts:<fetched full sha> origin feat/batch-2-cohorts`, using the SHA from `git rev-parse origin/feat/batch-2-cohorts` right after fetching (never typed from memory).
- Afterwards, update this doc: move the change into "As built" (it broadens OPEN-S to any error verdict and changes OPEN-F: no release of held members on error) and remove this section.

## Changes relative to the revised roadmap

The roadmap's grouping is kept. These items were missing from it or conflict with the proposal, and are placed as follows:

1. **Label key.** The roadmap says `agent-sandbox.k8s.io/batch-id`; the proposal (Batch Model, property 1) says `agents.x-k8s.io/batch-id`. This plan uses the proposal's key. Changing it is a proposal change.
2. **The batch Lease is in no PR of the roadmap.** Reading, renewing, and taking over the Lease lands in PR 1 (the proposal says `get_batch` resumes lease renewal). Creating it lands in PR 2 with `claim_batch`. `err()` lands in PR 1; `LEASE_DEGRADED` lands in PR 2, where `events()` first exists.
3. **`claim_batch` itself, the `shutdownTime` backstop, paced creation (`create_rps`, `max_in_flight`), and create-retry classification** are in no PR of the roadmap. All land in PR 2 with upfront provisioning.
4. **PR 1 needs a way to stop a handle.** A PR 1 handle runs a watch and a lease-renewal loop, and `release()` does not arrive until PR 2. `detach(grace)` is moved into PR 1, since it is `get_batch`'s counterpart (OPEN-A, resolved).
5. **PR 5 as written conflicts with the proposal.** The roadmap says "pool dynamically sized to batch concurrency N". The proposal (Transport & Connection Scaling) says "connection budgets cannot be dynamically resized at claim time" and specifies a construction-time `pool_size` plus validation in `claim_batch`. Keep-alive and idle-timeout tuning are not in the proposal. This plan writes PR 5 to the proposal and leaves the data-plane part blocked on OPEN-B.
6. **Connection teardown** on `release()`/`detach()` lands in the PRs that add those methods (PR 1 and PR 2), not in PR 5.
7. **Exceptions and client exit hooks** (release on `delete_all()`, `__aexit__`, atexit) land in PR 1 and PR 2.
8. **Sync/async parity** applies to every PR (AGENTS.md), so each PR ships `SandboxBatch` and `AsyncSandboxBatch` changes together.

## Ground rules (every PR)

- **Scope.** Python SDK only. `git diff main...HEAD --stat -- clients/go examples/agent-sandbox-rl` must print nothing. No controller, CRD, `api/`, `extensions/api/`, or `k8s/` changes.
- **Sync/async parity.** Every public sync addition has its async twin in the same PR, with the same semantics and the same unit-test cases. Async-only imports stay behind the `async` extra. Export the async batch class from `__init__.py` with the same `try/except ImportError` placeholder pattern `AsyncSandboxClient` uses there.
- **Python floor is 3.11** (`pyproject.toml`). `ExceptionGroup` and `asyncio.TaskGroup` are fine; PEP 695 generics and `type` statements are not.
- **Models** go in `models.py` as pydantic models (AGENTS.md). New `.py` files start with the Apache-2.0 header plus a module docstring, as existing files do.
- **Style.** Match the file being edited. No reformatting, no drive-by refactors. Sync modules log with `logging.info(...)`; async modules use a module `logger`. Follow each file's existing convention. Per-claim log lines go at DEBUG, since a batch can create thousands of claims.
- **Tests.** Unit tests go in `k8s_agent_sandbox/test/unit/` and run under `make test-unit`. Every behavior listed under a PR's "Unit tests" needs a test, in both sync and async variants where the behavior exists in both. Recommended: one e2e smoke test per PR in `test/e2e/clients/python/` (existing suites: `test_e2e_python_sdk.py`, `test_async_client_e2e.py`; fixtures in `framework/context.py`, `TestContext`), run against `make deploy-kind EXTENSIONS=true`. Mocks cannot show whether a real label-scoped watch, `deletecollection`, or Lease update behaves as assumed.
- **Docs.** Each PR updates the batch section of `clients/python/agentic-sandbox-client/README.md` for what it ships (this file is published to the docs site at `/docs/python-client/`). PR 1 adds `-m k8s_agent_sandbox.sandbox_batch` to the `generate-python-docs` target in the `Makefile` (it currently lists `sandbox_client` and `models`). Every PR regenerates `docs/python_sdk_reference.md` with `make generate-python-docs`.
- **Commits.** Make logical commits, each covering one concern a reviewer would rather read on its own (for example: models, constants and exceptions; Kubernetes helpers; the state core; the handles and client wiring; regenerated docs). Title each with a conventional prefix (`feat(python-sdk): ...`) and lead the body with motivation. Don't leave fixup or iteration commits: amend or re-squash a change into the commit it belongs to. No AI co-author or session trailers. Commit and push only as that PR's kickoff prompt authorizes, push only to `origin` (Brian's fork), and never open a PR.
- **Done means:** `make test-unit` passes, the PR's e2e test (if written) passes, the scope diff check above is empty, and the PR's acceptance criteria are met.

## Module layout

New modules:

- `k8s_agent_sandbox/batch_state.py` *(new)*: an I/O-free core shared by sync and async. It holds the member table, turns claim objects into `Member` snapshots, and owns the dispatched set, per-group accounting, ordinal allocation, settle detection, and group reconstruction from annotations. It uses no threads, no asyncio, and makes no Kubernetes calls. Most logic and most unit tests live here, so the two shells cannot drift on semantics (parity is the SDK's stated drift risk in AGENTS.md).
- `k8s_agent_sandbox/sandbox_batch.py` *(new)*: `SandboxBatch`, the sync shell. It runs a watcher thread and a lease-renewal thread (both daemon) and, from PR 2, a creation executor. Waiters use a `threading.Condition` and events use a `queue.Queue`. All `batch_state` access happens under one lock.
- `k8s_agent_sandbox/async_sandbox_batch.py` *(new)*: `AsyncSandboxBatch`, the async shell, built on asyncio tasks, `asyncio.Condition`, and `asyncio.Queue`.

Existing files touched across the stack: `models.py`, `constants.py`, `exceptions.py`, `k8s_helper.py`, `async_k8s_helper.py`, `sandbox_client.py`, `async_sandbox_client.py`, `__init__.py`, `README.md`, `Makefile` (docs target only), `docs/python_sdk_reference.md`.

**Watcher: hand-roll list+watch; do not use `kubernetes.informer.SharedInformer`.** The class exists in `kubernetes` 36.0.3, but its `_initial_list` reads `getattr(resp, "items", [])`. On the dict that `CustomObjectsApi` returns, that yields the dict's `items` method, and it raises `TypeError: 'builtin_function_or_method' object is not iterable` (reproduced locally). `kubernetes_asyncio` 33.3.0 (pyproject pins `<34.0.0`) has no informer at all, and pyproject does not pin a `kubernetes` version that ships one. Reuse the reconnect patterns in `K8sHelper._watch_claim` and `AsyncK8sHelper._watch_claim`:

- start from an explicit resourceVersion;
- on 410 Gone, re-list and diff (a claim missing from the new list is treated as deleted);
- on disconnect, reconnect from the last seen resourceVersion, handling the same exception sets those methods handle;
- on BOOKMARK events, advance the resourceVersion only.

## Shared contracts (fixed in PR 1; later PRs must not change them)

### Names, labels, annotations (`constants.py`)

- `BATCH_ID_LABEL = "agents.x-k8s.io/batch-id"` *(new)*, from the proposal, Batch Model 1.
- `BATCH_GROUP_SIZE_ANNOTATION = "agents.x-k8s.io/batch-group-size"` and `BATCH_GROUP_MIN_READY_ANNOTATION = "agents.x-k8s.io/batch-group-min-ready"` *(new)*, from Batch Model 4. Both are decimal strings. Claims in `size=0` groups do not carry them.
- `BATCH_LEASE_NAME_PREFIX = "batch-"` *(new)*. The Lease is named `batch-<id>` and lives in the batch's namespace.
- `BATCH_LEASE_DURATION_ANNOTATION = "agents.x-k8s.io/batch-lease-duration"` *(new; OPEN-K, resolved)*: a Lease annotation holding the batch's original `lease_duration` in seconds, as a decimal string. `claim_batch` writes it once at creation (PR 2). `get_batch` reads it on takeover (PR 1) and never writes it.
- `BATCH_WORK_BUDGET_ANNOTATION = "agents.x-k8s.io/batch-work-budget"` and `BATCH_QUORUM_TIMEOUT_ANNOTATION = "agents.x-k8s.io/batch-quorum-timeout"` *(new; OPEN-E, resolved)*: Lease annotations holding the batch's `work_budget` and `quorum_timeout` in seconds, as decimal strings. `claim_batch` writes both once at creation; `get_batch` reads them and never writes them. They exist so a re-attached handle can compute a `shutdownTime` for PR 4's replacements. Parsing follows the lease-duration annotation's rule: missing falls back to the default, present but not a positive integer raises `BatchError`.
- **Claim name** is `<id>-<ordinal>`. The ordinal is a per-batch integer that starts at 0, only ever increases, and is never reused (proposal, Membership). The initial fill takes ordinals `0..N-1` (N = sum of the initial group sizes) in the order the groups were passed. Every `acquire`/`replace` takes the next ordinal. `get_batch` relies on this: `ordinal < N` means the claim is part of the initial fill.
- **Batch id.** A generated id is `"b"` followed by 11 random `[a-z0-9]` characters (the length is a placeholder). A caller-supplied `batch_id` must be a DNS-1123 label that starts with a letter and is at most 52 characters. That keeps `<id>-<ordinal>` within a 63-character DNS label for the cold-start Sandbox/Service name (Batch Model 1) and within the 63-character label-value limit (52 = 63 - 1 - 10 ordinal digits). The claim name becomes the Sandbox name (`sandboxclaim_controller.go`), which becomes the Service name (`sandbox_controller.go`), and a Service name is a DNS-1035 label: at most 63 characters and starting with a letter, which is also why the id must start with a letter. The cap matches Kubernetes' own rule for CronJob names, `DNS1035LabelMaxLength-11 = 52`, which reserves an 11-character `-$TIMESTAMP` suffix for the Jobs it creates (`pkg/apis/batch/validation/validation.go`).

### Claim manifest (written from PR 2)

Claims are created through the existing `K8sHelper.create_sandbox_claim(name, warmpool, namespace, annotations=..., labels=..., lifecycle=...)` and its async twin. That call already adds `CREATED_BY_LABEL` and `CLIENT_REQUEST_TIME_ANNOTATION`.

- **labels**: the caller's `labels`, checked with `validate_labels` (`pod_metadata.py`), plus `BATCH_ID_LABEL`. Reject caller labels that set `BATCH_ID_LABEL`.
- **annotations**: the group annotations (size>0 groups only), plus the trace-context annotation, computed once per batch the same way `SandboxClient._create_claim` computes it. Do not call `_create_claim` per member: it is decorated with `@trace_span`, so it would open one span per claim.
- **lifecycle**: `{"shutdownTime": <create time> + quorum_timeout + work_budget + margin, "shutdownPolicy": "Delete"}`, computed at each claim's own create time (proposal, Shutdown Backstop). `construct_sandbox_claim_lifecycle_spec(int(seconds))` in `utils.py` builds exactly this shape.
- **Create retry** (one helper, shared by the fill in PR 2 and `acquire` in PR 4; lives in the shells, with classification in `batch_state`):
  - Retry 429 and 5xx with jittered exponential backoff, honoring `Retry-After` when present, up to 3 attempts in total (OPEN-1b).
  - Do not retry 400/403/404/422; they are create failures.
  - Exhausted retries are create failures.
  - A 409 on the first attempt is an error (name collision). A 409 on a later attempt counts as success, since the deterministic name means an earlier attempt landed.

### Lease (the reaper in PR 6 depends on this)

- **Object.** `coordination.k8s.io/v1` Lease `batch-<id>`, with labels `{BATCH_ID_LABEL: id, CREATED_BY_LABEL: "python-client"}`. The spec sets `holderIdentity`, `leaseDurationSeconds`, and `acquireTime`/`renewTime`. The handle generates its own `holderIdentity` as `f"{socket.gethostname()}_{os.getpid()}_{uuid.uuid4().hex[:8]}"` (OPEN-P), unique per handle. Both client libraries have `CoordinationV1Api` with `create_namespaced_lease`, `read_namespaced_lease`, `replace_namespaced_lease`, and `delete_namespaced_lease` (checked in `kubernetes` 36.0.3 and `kubernetes_asyncio` 33.3.0).
- **Two clocks (OPEN-R).** The Lease duration (default 60 s, renewed every 20 s) is only a process-liveness heartbeat. It is completely independent of the execution budget: `work_budget`, `quorum_timeout`, and the per-claim `shutdownTime` backstop.
- **Duration validation (OPEN-M, OPEN-D).** Every caller-supplied duration that ends up in `leaseDurationSeconds` (`claim_batch`'s `lease_duration` and `detach`'s `grace`) is checked in two parts:
  - It must be an `int`: `type(v) is int` rejects floats (including whole ones like `60.0`), `bool`, and strings.
  - It must be greater than `CLOCK_SKEW_MARGIN`. A value `<= CLOCK_SKEW_MARGIN` raises `ValueError(f"Duration must be greater than clock skew margin ({CLOCK_SKEW_MARGIN}s)")`, since such a Lease would look stale to `get_batch` as soon as it was written.
- **Renewal.** Every `max(1, lease_duration // 3)` seconds (20 s at the 60 s default; OPEN-L, resolved), read the Lease and `replace` it using its resourceVersion.
- **Staleness, as the reaper reads it:** at least `now > renewTime + leaseDurationSeconds`, including after a detach. PR 6 may add its own margin, but only in the direction that makes the reaper wait longer, never one that makes it delete sooner (OPEN-V). The two margins point opposite ways on purpose: `get_batch` subtracts so it refuses to adopt a Lease the reaper might already treat as dead, and the reaper adds so it refuses to delete claims a driver might still be renewing. Between `renewTime + duration - CLOCK_SKEW_MARGIN` and the reaper's own threshold, neither side acts.
- **No detach-specific annotations.** Do not add annotations such as `batch-detached-at` or `batch-grace-seconds`. A detach expresses its grace window only through `renewTime` and `leaseDurationSeconds`, so the reaper needs no annotation logic.
- **Degraded / expired (OPEN-2, resolved).** The first failed renewal after a success is "degraded": it is logged, and from PR 2 it also emits `LEASE_DEGRADED`, once per episode. If no renewal succeeds for `lease_duration`, the handle is "expired": `err()` returns `BatchLeaseExpiredError` and keeps returning it.
- **Detach (OPEN-D, resolved).** `detach(grace)` runs in this order:
  1. Validate `grace` (unless it is `None`) per "Duration validation" above. Any invalid value raises `ValueError` before anything is stopped. Callers who want immediate teardown should call `release()` (PR 2) or delete claims explicitly, not pass a tiny `grace`.
  2. Stop the watcher and renewal loops, and close cached sandbox connections.
  3. Make one final Lease update that:
     - sets `holderIdentity` to `None` (the field is a `*string` in `coordination.k8s.io/v1`, so it can be unset);
     - sets `renewTime` to now;
     - sets `leaseDurationSeconds` to `int(grace)` when `grace` is given, and otherwise keeps the handle's current `lease_duration`;
     - leaves the Lease's annotations untouched.

  The reaper therefore waits exactly `grace` seconds after the detach before cleaning up.
- **Takeover (OPEN-C, OPEN-K, resolved).** `get_batch` handles the Lease it finds as follows:
  - **Held by someone else:** if the Lease is live (not stale) and its `holderIdentity` is set to a different holder, raise `BatchInUseError`.
  - **Stale or missing:** raise `BatchLeaseExpiredError`, always, if the Lease is missing while labeled claims exist, or if it is stale by `get_batch`'s test: `now > renewTime + leaseDurationSeconds - CLOCK_SKEW_MARGIN`. There is no override (OPEN-N).
    - `CLOCK_SKEW_MARGIN` *(new)* is 5 s (OPEN-Q). The subtraction makes `get_batch` more cautious than the reaper, so clock drift or network round-trip time can't let it adopt a Lease the cluster or the reaper may already treat as expired.
    - This includes an unheld Lease whose detach grace has elapsed, since the reaper may already be deleting its claims.
  - **Take over:** only an unheld Lease (`holderIdentity` is `None`) that is not stale by that test (`now <= renewTime + leaseDurationSeconds - CLOCK_SKEW_MARGIN`) can be adopted. The handle writes its own `holderIdentity`, sets `renewTime` to now, and restores `leaseDurationSeconds` to the batch's original duration. See `get_batch` in PR 1 for the order of these checks.
  - **Duration source:** read the original duration from `BATCH_LEASE_DURATION_ANNOTATION`, falling back to the 60 s default when it is missing (PR 1 tests, batches created before PR 2). If the annotation is present but cannot be parsed as an integer, or parses to a value `<= CLOCK_SKEW_MARGIN` (valid means `int(val) > CLOCK_SKEW_MARGIN`), fail fast with `BatchError`, so a corrupted or hand-edited annotation can never trap the handle in an immediately stale Lease. Never read it from the spec field, which holds `grace` after a detach.
  - **Using the duration:** the handle uses it for the takeover write, for its OPEN-2 expiry threshold, and for its renew interval, `max(1, duration // 3)`.
  - **Writing the annotation:** only `claim_batch` writes it, once, at creation (PR 2). Takeovers never write or change it.
- **Ordering.** The Lease is created before the watcher starts and before any claim create (PR 2). `release()` deletes it last (PR 2).

### `Member` derivation (`batch_state.py`)

Build `Member` from a claim object as follows. The state core replaces snapshots rather than mutating them, since members cross threads.

`Member` is a pydantic model with `model_config = ConfigDict(frozen=True)`, and `pod_ips` is `tuple[str, ...] = ()` (OPEN-O, OPEN-Y). The tuple is what makes the model genuinely immutable; with `list[str]` instead, pydantic 2.13.4 leaves two holes:

- `frozen` blocks reassigning attributes but not in-place changes to a list, so the state core would have to rebuild every member with a fresh `pod_ips` list on each `members()` call, at O(N) model copies per call, to stop a caller's `append` from corrupting the table. With a tuple, `members()` returns the stored objects directly.
- `hash(member)` raises `TypeError: unhashable type: 'list'`. A tuple field makes `Member` hashable, but internal sets and dicts, including the dispatched set, still key on `claim_name`, since the claim name is the member's identity and a snapshot's other fields change over time.

- **claim_name**: `metadata.name`.
- **sandbox_name**: `status.sandbox.name`, falling back to the legacy `Name` key as `_watch_claim` does. Typed `str | None = None`; it is `None` until the claim is bound (OPEN-4, resolved).
- **warmpool**: `spec.warmPoolRef.name`.
- **pod_ips**: `status.sandbox.podIPs`. **service_fqdn**: `status.sandbox.serviceFQDN`. Both fields exist on `SandboxStatus` in `extensions/api/v1beta1/sandboxclaim_types.go`.
- **ready**: the `Ready` condition is `True` and `sandbox_name` is set. This is the same test `_watch_claim(require_ready=True)` uses.
- **terminal**: true if either holds:
  - the `Ready` condition is `False` with a reason in `TERMINAL_CLAIM_READY_REASONS`;
  - the create failed. That member is synthetic, with reason `"CreateFailed"` *(new string)* and the API error as its message.

  `WarmPoolNotFound` and `TemplateNotFound` are **not** terminal for a batch member (OPEN-W), which is a deliberate divergence from `_watch_claim`: the controller requeues both every minute rather than failing them (`sandboxclaim_controller.go`, the `ErrWarmPoolNotFound`/`ErrTemplateNotFound` branch, whose comment calls the requeue a fallback "for missed watch events or cache lag"). Such a member stays pending, bounded by `quorum_timeout` and by its `shutdownTime`.
- **lost**: the claim disappeared (a watch `DELETED` event, or missing from a re-list) and the caller did not release it.
- **reason / message**: taken from the `Ready` condition, or from the create error.
- **Caller-released members** (`release_member`, `release_not_ready`, `replace`) are dropped from `members()` and never reported as lost or as `MEMBER_LOST` (OPEN-5, resolved). In quorum arithmetic and settle detection they count as unable to arrive, exactly like lost members (OPEN-5b, resolved).

### Dispatched set

Each handle hands a member to the caller at most once. Every hand-out path goes through one `batch_state` method (`try_dispatch`, PR 1) that checks the dispatched set and adds to it atomically under the state lock. The paths are:

- `events()` `MEMBER_READY`
- `iter_ready_groups()`
- `wait_for_quorum()`
- `acquire()`/`replace()`

The set is per handle and is not persisted, so `get_batch` starts with an empty one (OPEN-G): at-most-once is a guarantee about one handle, not about a batch across a driver restart.

**Consumer mode (OPEN-F, resolved; implemented in PR 2).** The first of `events()`, `iter_ready_groups()`, and `wait_for_quorum()` to be called fixes the handle's mode, under the same lock:

- **Quorum first.** `events()` holds back each non-zero group's first `min_ready` Ready members, in Ready order, until the quorum consumer dispatches them. Every later Ready member of that group streams immediately. Once a group yields, its hold-back is over; if the group yields an error instead (unreachable, or the OPEN-J timeout), its held-back members are released to `events()` rather than stranded, since nothing else would ever deliver them.
- **`events()` first.** The handle is stream-only, and a later `iter_ready_groups()` or `wait_for_quorum()` raises `BatchError`.
- Ready order is only known for transitions this handle observed. On a re-attached handle, members that were already Ready at attach time have no known order, so hold-back and dispatch use ordinal order for them.

### Exceptions (`exceptions.py`, OPEN-3, resolved)

The proposal names only `TerminalMemberError`, which appears in usage example 4 but is never defined. Brian accepted this hierarchy. PR 1 implements `BatchError`, `BatchNotFoundError`, `BatchLeaseExpiredError`, and `BatchInUseError`; each other class lands with the PR that first raises it. All are *(new)*:

- `BatchError(SandboxError)`: the base class.
- `TerminalMemberError(BatchError, SandboxClaimFailedError)`: an `acquire`/`replace` member failed terminally or was lost before becoming Ready. It carries `.member`. Subclassing the existing `SandboxClaimFailedError` keeps single-claim `except` clauses working.
- `BatchExistsError(BatchError)`: `claim_batch` got a 409 creating the Lease.
- `BatchNotFoundError(BatchError, SandboxNotFoundError)`: `get_batch` found neither a Lease nor claims.
- `BatchLeaseExpiredError(BatchError)`: returned by `err()`, and raised by `get_batch` when the Lease is stale or missing while claims exist.
- `BatchInUseError(BatchError)`: `get_batch` found a live Lease held by a different `holderIdentity`.
- `QuorumUnreachableError(BatchError)`: carries `.warmpool` and the counts behind the verdict.
- Timeouts raise the builtin `TimeoutError`, as `_watch_claim` does. Misuse (a consumer called twice, mutual exclusion, calls after release/detach) raises `BatchError`.

### Defaults (OPEN-1)

The proposal says defaults are TBD. Define them as named constants in `constants.py` (PR 1 adds the confirmed ones and `CLOCK_SKEW_MARGIN`; each placeholder lands with the PR that first uses it, once confirmed) so they change in one place. The first five values are confirmed. The rest are still placeholders, not derived from measurement, and must be confirmed before the PR that first uses them.

| Setting | Value | Status |
| :--- | :--- | :--- |
| `lease_duration` | 60 s | Confirmed |
| renew interval | `max(1, lease_duration // 3)` (20 s at the default) | Confirmed (OPEN-L) |
| `quorum_timeout` | 600 s | Confirmed |
| `work_budget` | 3600 s | Confirmed |
| `shutdownTime` margin | 600 s | Confirmed |
| `CLOCK_SKEW_MARGIN` (used in `get_batch`'s staleness test) | 5 s | Confirmed (OPEN-Q) |
| `create_rps` | 50.0 | Confirmed (OPEN-1b) |
| `max_in_flight` | 20 | Confirmed (OPEN-1b; see PR 5 for the interaction with pool size) |
| create retry | 3 attempts, jittered exponential backoff, on 429/5xx | Confirmed (OPEN-1b) |
| `acquire` timeout | 180 s, matching `create_sandbox`'s `sandbox_ready_timeout` default | Placeholder (PR 4, OPEN-1c) |

## Decisions (Brian)

### Resolved

| ID | Resolution |
| :--- | :--- |
| OPEN-1 | `lease_duration` 60 s, renew interval `max(1, lease_duration // 3)` (20 s at the default, per OPEN-L), `quorum_timeout` 600 s, `work_budget` 3600 s, `shutdownTime` margin 600 s, as named constants in `constants.py`. The pacing and retry defaults are OPEN-1b. |
| OPEN-2 | The first renewal failure logs "degraded". Exceeding `lease_duration` without a successful renewal marks the handle "expired", and `err()` returns `BatchLeaseExpiredError`. |
| OPEN-3 | The exception hierarchy in "Shared contracts" is accepted. PR 1 implements `BatchError`, `BatchNotFoundError`, `BatchLeaseExpiredError`, and `BatchInUseError`. |
| OPEN-4 | `Member.sandbox_name: str \| None = None`. |
| OPEN-5 | Caller-released members are dropped from `members()` and never produce `MEMBER_LOST`. Their effect on quorum math is OPEN-5b. |
| OPEN-A | `detach(grace)` is implemented in PR 1, so the handle can stop its background loops and close its cached connections. |
| OPEN-C | `get_batch` raises `BatchInUseError` if the Lease is live and held by a different `holderIdentity`. |
| OPEN-D | `detach(grace)` requires `grace` to be `None` or an `int` greater than `CLOCK_SKEW_MARGIN` ("Duration validation" in the Lease contract); anything else raises `ValueError`. It then stops its loops and closes connections, and makes one final Lease update: `holderIdentity = None`, `renewTime = now`, and `leaseDurationSeconds = int(grace)`, or the handle's current `lease_duration` when `grace` is `None`. There are no detach- or grace-specific annotations; the reaper's staleness check stays `now > renewTime + leaseDurationSeconds`. |
| OPEN-K | `agents.x-k8s.io/batch-lease-duration` records the original duration and is written once by `claim_batch` (PR 2). On a `get_batch` takeover (only an unheld Lease that is not stale; see OPEN-N), the handle writes its `holderIdentity` and `renewTime = now`, and restores `leaseDurationSeconds` from the annotation. It falls back to 60 s when the annotation is missing, and fails fast with `BatchError` when it is present but not an integer greater than `CLOCK_SKEW_MARGIN`. The same duration sets the OPEN-2 expiry threshold. A live Lease held by a different holder raises `BatchInUseError`. |
| OPEN-L | The renew interval is `max(1, lease_duration // 3)`. |
| OPEN-M | `claim_batch` requires `lease_duration` to be `None` or an `int` greater than `CLOCK_SKEW_MARGIN` ("Duration validation" in the Lease contract), since `leaseDurationSeconds` is an integer (`*int32` in `coordination.k8s.io/v1`). Anything else raises `ValueError`; values `<= CLOCK_SKEW_MARGIN` use the message `f"Duration must be greater than clock skew margin ({CLOCK_SKEW_MARGIN}s)"`. |
| OPEN-N | `get_batch` has no `adopt_expired` argument. A stale Lease (by the OPEN-Q test), or a missing Lease while claims exist, always raises `BatchLeaseExpiredError`, so `get_batch` never races a reaper that is deleting claims. An unheld Lease is adoptable only while it is not stale. Before the reaper ships, a stale batch's claims are cleaned up only by their `shutdownTime`, which is the intended backstop. |
| OPEN-O | `Member` uses `model_config = ConfigDict(frozen=True)` and, per OPEN-Y, `pod_ips: tuple[str, ...] = ()`. `GroupReady` is `@dataclass(frozen=True)` with `warmpool: str`, `members: list[Member] = field(default_factory=list)`, and `error: Exception \| None = None`. |
| OPEN-P | The handle generates its `holderIdentity` internally as `f"{socket.gethostname()}_{os.getpid()}_{uuid.uuid4().hex[:8]}"`. |
| OPEN-Q | `CLOCK_SKEW_MARGIN = 5` in `constants.py`. `get_batch` treats a Lease as stale when `now > renewTime + leaseDurationSeconds - CLOCK_SKEW_MARGIN`. The reaper's own margin is still PR 6's to decide. |
| OPEN-R | Two clocks: the Lease duration (60 s, renewed every 20 s) is only a process-liveness heartbeat, independent of `work_budget` and the `shutdownTime` backstop. |
| OPEN-1b | `create_rps = 50.0`, `max_in_flight = 20`, create retry 3 attempts with jittered exponential backoff on 429/5xx. The `acquire` timeout (PR 4) is still a placeholder. |
| OPEN-5b | A caller-released initial-fill member counts as unable to arrive, exactly like a lost one: it decrements the group's reachable count for the unreachability test and decrements the pending initial-fill count for settle detection. |
| OPEN-E | `claim_batch` writes `agents.x-k8s.io/batch-work-budget` and `agents.x-k8s.io/batch-quorum-timeout` on the Lease, as decimal strings, at creation. `get_batch` reads them into the handle (PR 2), falling back to the defaults when absent, so a re-attached handle can compute `shutdownTime` for PR 4's replacements without guessing. Like the lease-duration annotation, only `claim_batch` writes them. |
| OPEN-F | First-call-fixes-the-mode with hold-back. The first call among `events()`, `iter_ready_groups()`, and `wait_for_quorum()` fixes the handle's mode. Quorum-consumer first: `events()` holds back each non-zero group's first `min_ready` Ready members (by Ready order) until the quorum consumer dispatches them, and streams every later Ready member of that group immediately. `events()` first: the handle is stream-only and `iter_ready_groups()`/`wait_for_quorum()` raise `BatchError`. |
| OPEN-G | Quorum is level-triggered, so a re-attached handle supports `iter_ready_groups()` and `wait_for_quorum()`; neither raises. A group already at `min_ready` when `get_batch` attaches yields (or returns) immediately, without blocking. The dispatched set is per handle: members consumed by this handle's quorum call are not re-emitted by this handle's `events()`. Nothing is carried over from the previous handle, so a member the crashed driver already delivered can be delivered again to the new one; at-most-once is a per-handle guarantee, not a per-batch one. |
| OPEN-H | `MEMBER_FAILED`/`MEMBER_LOST` cover initial-fill members only: the ones in `BatchState._initial_fill`, i.e. ordinal below the batch's total initial size. Members added later by `acquire`/`replace` report through that call's exception and through `members()`. |
| OPEN-J | A group that has not yielded within `quorum_timeout` (600 s by default) yields with `error=TimeoutError("Group quorum timed out")`. |
| OPEN-S | Create-failure fail-fast is per group, and never auto-releases the batch. When a group's create failures cross its threshold (`create_failed > size - min_ready`, the create-only form of the unreachability test), that group's pending creates are cancelled and the group is marked unreachable, so `iter_ready_groups()` yields it with `QuorumUnreachableError`. Other groups keep filling and their Ready members stay alive; tearing the batch down remains the caller's call via `release()`. |
| OPEN-T | `work_budget` and `quorum_timeout` follow the same strict rule as `lease_duration`: `None` or an `int` greater than 0, anything else raises `ValueError`. They feed `shutdownTime` through `construct_sandbox_claim_lifecycle_spec(shutdown_after_seconds: int)` (`utils.py`), which itself rejects any value where `type(v) is not int`. |
| OPEN-U | `release()` makes no fallback when `deletecollection` returns 403. The driver Role in the proposal's RBAC section grants `deletecollection` on sandboxclaims explicitly (verbs `["create", "get", "list", "watch", "delete", "deletecollection"]`), so a 403 is a misconfigured Role, not a case to work around. `release()` raises it, and the per-claim `shutdownTime` backstop still bounds the leak. |
| OPEN-W | `WarmPoolNotFound` and `TemplateNotFound` are not terminal for batch members. The controller requeues both after a minute instead of failing them, so treating them as terminal lets a cache miss or a warm pool created moments before the batch write members off and declare a group unreachable while the controller is still retrying. `claim_batch` instead prechecks each group's `SandboxWarmPool` and the template named in that pool's `spec.sandboxTemplateRef.name`, so a missing or misspelled dependency raises before any claim is created; a dependency that disappears mid-run surfaces as the group's `quorum_timeout`. `_watch_claim`'s single-claim behavior does not change, since that is shipped API behavior and a separate concern. |
| OPEN-Y | `Member.pod_ips` is `tuple[str, ...] = ()`, not `list[str]`. `frozen=True` does not stop in-place list mutation, so a list field makes the immutability contract depend on `BatchState.members()` rebuilding every member on every call, which costs O(N) model copies per call and is invisible in the type. A tuple makes the model genuinely immutable, lets `members()` return stored objects, and makes `Member` hashable. A caller mutating `pod_ips` now fails, which is the intended failure; reads, indexing, iteration and `len()` are unchanged, and pydantic coerces an incoming list at validation. |
| OPEN-X | The initial fill settles at `quorum_timeout`: any initial-fill member still pending then counts as unable to arrive, and `events()` closes. Settle otherwise waits on a state that may never come, since the controller retries a missing dependency forever and a member stuck on an unschedulable pod or image-pull backoff carries no terminal reason at all. One deadline on the fill covers every such case, reuses an existing knob, and needs no per-member timers. A member that goes Ready after the deadline is still delivered by `members()` and still `connect()`s; it just arrives after the stream closed. |

### Proposal edits these decisions imply

The plan now differs from `batch_claim_proposal.md` in these places. The proposal has not been edited.

- `get_batch(batch_id, namespace="default")`: drop `adopt_expired` from the Python signature (OPEN-N).
- `claim_batch`: `lease_duration: int | None` instead of `float | None` (OPEN-M), and `work_budget: int | None` / `quorum_timeout: int | None` instead of `float | None` (OPEN-T).
- `iter_ready_groups()`: a group that has not reached `min_ready` within `quorum_timeout` yields with `error=TimeoutError` (OPEN-J). The proposal gives it no timeout at all.
- The Lease carries two annotations the proposal does not mention, `batch-work-budget` and `batch-quorum-timeout` (OPEN-E), so a re-attached handle can compute `shutdownTime`.
- Consumer mode and at-most-once delivery: the first-call mode lock with hold-back (OPEN-F) and the per-handle dispatched set on a re-attached handle (OPEN-G) are not described in the proposal.
- Create-failure fail-fast is per group and never auto-releases (OPEN-S); the proposal describes no fail-fast at all.
- If PR 6 takes OPEN-V's margin, the lifecycle diagram's crash-cleanup bound, "LeaseDuration + poll period", becomes "LeaseDuration + margin + poll period".
- `Member`: `sandbox_name: str | None = None` (OPEN-4), and `pod_ips: tuple[str, ...] = ()` with `frozen=True` (OPEN-O, OPEN-Y). The proposal writes `pod_ips: list[str]`.
- `GroupReady`: `@dataclass(frozen=True)` (OPEN-O).
- `detach(grace)`: its semantics (OPEN-D) and the "no detach annotations" rule are not described in the proposal.
- `TerminalMemberError`: used in usage example 4 but never defined; its definition is part of OPEN-3.

### Still open

| ID | Decision | Recommendation | Blocks |
| :--- | :--- | :--- | :--- |
| OPEN-V | The reaper's own staleness margin, and whether it compares clocks at all. `renewTime` is written from the driver's clock and read by the reaper's, so the comparison is cross-host; `get_batch` covers that with `- CLOCK_SKEW_MARGIN`, and the reaper has no equivalent yet. | Add `+ REAPER_SKEW_MARGIN` so an ambiguous Lease is never reaped, since deleting a live batch's claims destroys running work while waiting only holds idle quota. Consider instead the clock-free alternative: observe the Lease, wait one `leaseDurationSeconds` within the same run, and reap only if `renewTime` has not moved, which is what `client-go` leader election does with its local `observedTime` (`isLeaseValid` compares `observedTime + leaseDuration` against its own `now`, never the writer's timestamp). | PR 6 |
| OPEN-1c | The `acquire` timeout placeholder (see the Defaults table). | 180 s, matching `create_sandbox`'s `sandbox_ready_timeout`. | PR 4 |
| OPEN-B | PR 5 dynamic sizing and data-plane pooling (details below). | Build only the proposal's construction-time `pool_size` and validation. Hold data-plane pooling until measured. | PR 5 |
| OPEN-I | Pool-size validation budget. The proposal says `pool >= max_in_flight`, but the watch and lease renewal each hold a connection too. | Validate `pool >= max_in_flight + 2`. Note that OPEN-1b's `max_in_flight = 20` puts the floor at 22, above `requests`' default pool of 10. | PR 5 |

**OPEN-B detail.** The proposal says the sync connector's shared `requests` pool evicts the least-recently-used host once a batch exceeds 10 pod IPs. The SDK has no shared pool to evict from. Each `Sandbox`/`AsyncSandbox` builds its own `requests.Session` (`SandboxConnector.__init__`, `connector.py`) or `httpx.AsyncClient` (`AsyncSandboxConnector.__init__`, `async_connector.py`). One shared pool would only allow reuse when members share a host (router, Gateway, or Direct modes). It buys nothing for in-cluster pod-IP routing.

## PR 1: State models, watcher cache, `get_batch`, `members`, `connect`

**Goal.** Provide a handle that attaches to an existing batch, keeps a live cache of the batch's claims through one label-scoped watch, exposes `members()`, connects to members, renews the batch Lease, reports `err()`, and can be detached. Nothing in the SDK creates a batch yet, so tests create labeled claims and Leases directly.

**Depends on:** `main`. **Blocked on:** nothing; every PR 1 decision is resolved.

**Files:**

- `models.py`:
  - `BatchGroup` *(new)*: `warmpool: str`, `size: int >= 0`, `min_ready: int | None`, defaulting to `size` and validated `0 <= min_ready <= size`. These are model-level validators, so corrupt annotations fail during reconstruction too. `claim_batch`-specific input rules are in PR 2.
  - `Member` *(new)*: `model_config = ConfigDict(frozen=True)`, `pod_ips: tuple[str, ...] = ()`, `sandbox_name: str | None = None`; the other fields are as in the proposal.
  - `BatchEventType` *(new)*.
  - `BatchEvent` *(new)*.
  - `GroupReady` *(new)*: `@dataclass(frozen=True)` with `warmpool: str`, `members: list[Member] = field(default_factory=list)`, and `error: Exception | None = None` (OPEN-O). AGENTS.md says "Use `pydantic` for data models", so give the reason in the PR description: pydantic needs `arbitrary_types_allowed` to hold a raw `Exception`.
- `constants.py`: the label and annotation keys, the Lease prefix, and the default constants.
- `exceptions.py`: `BatchError`, `BatchNotFoundError`, `BatchLeaseExpiredError`, and `BatchInUseError`. The remaining classes come with the PRs that raise them.
- `batch_state.py` *(new)*:
  - member table and `Member` derivation;
  - reconstruction: groups from annotations, where unannotated pools become `BatchGroup(size=0, min_ready=0)` (proposal, `get_batch`), and conflicting annotations for one pool raise `BatchError`;
  - ordinal parsing from `<id>-<n>`, with the counter set to max ordinal + 1, including claims that have a `deletionTimestamp`;
  - initial-fill marking (`ordinal < N`);
  - the dispatched set.
- `k8s_helper.py` and `async_k8s_helper.py`. Add `self.coordination_v1_api = client.CoordinationV1Api()` in `K8sHelper.__init__`, and the equivalent built on `self._api_client` in `AsyncK8sHelper._ensure_initialized`. New methods:
  - `list_sandbox_claim_objects(namespace, label_selector) -> (items, resource_version)` *(new)*. Leave `list_sandbox_claims` alone: it returns names and backs the public `list_all_sandboxes`.
  - `watch_sandbox_claims(namespace, label_selector, resource_version, timeout_seconds)` *(new)*, which yields events.
  - `read_batch_lease`, `replace_batch_lease` *(new)*. `create_batch_lease` waits for PR 2: with `adopt_expired` gone (OPEN-N), nothing in PR 1 creates a Lease.
- `sandbox_batch.py` and `async_sandbox_batch.py` *(new)*:
  - attributes: `batch_id`, `namespace`, `groups`, `size`;
  - methods: `members(warmpool=None)`, `connect(member)`, `err()`, `detach(grace=None)`;
  - the watcher and lease-renewal loops.
- `sandbox_client.py` and `async_sandbox_client.py`:
  - `get_batch(batch_id, namespace="default")` *(new)*. The proposal's `adopt_expired` argument is dropped (OPEN-N);
  - `_active_batches: dict[tuple[str, str], batch]` *(new)*;
  - `AsyncSandboxClient.close()` stops the watcher and renewal tasks of the batches it tracks. Sync threads are daemon threads.
- `__init__.py`: export the new models, the exceptions, `SandboxBatch`, and `AsyncSandboxBatch` (the latter behind the ImportError placeholder).
- `README.md`, `Makefile` docs target, `docs/python_sdk_reference.md`.

**Behavior:**

- **`get_batch`:**
  1. Validate `batch_id`.
  2. Read the Lease. If it carries `BATCH_LEASE_DURATION_ANNOTATION`, validate it now: a value that cannot be parsed as an integer, or is `<= CLOCK_SKEW_MARGIN`, raises `BatchError` before anything is written.
  3. If there is no Lease and no labeled claims, raise `BatchNotFoundError`.
  4. If the Lease is missing while claims exist, or stale (`now > renewTime + leaseDurationSeconds - CLOCK_SKEW_MARGIN`), raise `BatchLeaseExpiredError`, always.
     - This check runs before the holder check, so an unheld Lease whose detach grace has elapsed also raises.
  5. If the Lease is live and held by a different holder, raise `BatchInUseError`.
  6. If it is live and unheld (`holderIdentity` is `None`), take it over as described under "Takeover" in the Lease contract: this handle's holder, `renewTime` set to now, and the duration from the annotation (60 s when it is missing).
  7. List claims by `BATCH_ID_LABEL=<id>` and reconstruct.
  8. Start the watch from the list's resourceVersion.
  9. Start renewal.
  10. Register the handle in `_active_batches`.
- **`members(warmpool)`:** returns a snapshot list sorted by ordinal. It includes terminal and lost members and excludes caller-released ones.
- **`connect(member)`:**
  - Look up the member's current state by `claim_name`, and raise the existing `SandboxNotReadyError` if it is not ready.
  - Build `client.sandbox_class(claim_name=..., sandbox_id=member.sandbox_name, namespace=..., connection_config=client.connection_config, tracer_config=client.tracer_config, k8s_helper=client.k8s_helper)`. Both `Sandbox` and `AsyncSandbox` take these constructor arguments.
  - Cache the handle per claim, so a repeat `connect` returns the same handle.
  - Do not register it in `_active_connection_sandboxes`. That registry's `delete_all()` would delete batch claims one at a time.
  - This skips `resolve_sandbox_name` and the existence check. The connector still resolves the pod IP lazily through one `get_sandbox` call on its first router request (`SandboxConnector.send_request`). Seeding that IP would change `Sandbox`'s constructor, which is outside this plan. Say so in the PR description.
- **`detach(grace)`:**
  - Idempotent.
  - Validates `grace` first. Anything other than `None` or an `int` greater than `CLOCK_SKEW_MARGIN` raises `ValueError` and leaves the handle running.
  - Stops the watch and renewal, and closes cached connected handles (`close_connection()`; async: `await close_connection()`).
  - Makes the final Lease update, as described under "Detach" in the Lease contract.
  - Unregisters from the client.
  - Later calls on the handle raise `BatchError`.
- **`err()`:** returns `BatchLeaseExpiredError` once no renewal has succeeded for `lease_duration`, or a permanent watcher failure such as a 401/403 on list/watch. Transient failures are retried, not surfaced.

**Unit tests** (both shells, unless the item says state-only):

- **Derivation** (state-only), a table test covering:
  - pending (no sandbox name) and bound-but-not-Ready;
  - Ready;
  - each `TERMINAL_CLAIM_READY_REASONS` reason on a `Ready=False` condition is terminal, while `TemplateNotFound` and `WarmPoolNotFound` are not (OPEN-W);
  - the legacy `Name` key;
  - `podIPs` and `serviceFQDN` mapping.
- **Lost vs released** (state-only): `DELETED` for an unreleased claim sets `lost`; for a released claim, the member simply disappears.
- **Reconstruction** (state-only):
  - annotated groups come back with their size and min_ready;
  - unannotated pools come back as `size=0, min_ready=0`;
  - conflicting annotations raise;
  - `min_ready > size` in annotations raises;
  - the ordinal counter handles gaps and terminating claims;
  - the initial fill is exactly `ordinal < N`.
- **`get_batch` Lease cases:**
  - not found raises `BatchNotFoundError`;
  - a stale held Lease raises `BatchLeaseExpiredError`;
  - a missing Lease with claims present raises `BatchLeaseExpiredError`, and no Lease is created;
  - an unheld but stale Lease (detach grace elapsed) raises `BatchLeaseExpiredError`;
  - skew-margin boundary (fake clock): an unheld Lease exactly at `now == renewTime + leaseDurationSeconds - CLOCK_SKEW_MARGIN` is still adoptable; one second later it raises `BatchLeaseExpiredError`, even though the reaper's plain `now > renewTime + leaseDurationSeconds` test would not yet call it stale;
  - `get_batch` accepts no `adopt_expired` argument;
  - live with a different holder raises `BatchInUseError`;
  - live and unheld takes over.
- **Takeover writes:**
  - this handle's `holderIdentity` and `renewTime` near now;
  - with `batch-lease-duration: "90"` and a spec field holding a detach `grace` (say 300): writes `leaseDurationSeconds: 90`, renews every 30 s, expires after 90 s without success;
  - with no annotation: 60 s, renewing every 20 s;
  - with an annotation of `"abc"`, `"1.5"`, `"0"`, `"-5"`, or `"5"` (equal to `CLOCK_SKEW_MARGIN`): raises `BatchError`, and the Lease is not written;
  - with an annotation of `"6"`: the takeover succeeds and writes `leaseDurationSeconds: 6`;
  - the takeover leaves the annotation unchanged.
- **Watcher:**
  - resumes from the list's resourceVersion;
  - on 410, re-lists and diffs, so a vanished claim becomes lost;
  - on disconnect, reconnects from the last resourceVersion;
  - BOOKMARK advances the resourceVersion;
  - 403 surfaces through `err()`.
- **Renewal:**
  - renews every `max(1, lease_duration // 3)` seconds (60 gives 20; 2 gives 1) using the read resourceVersion;
  - a failure is logged as degraded;
  - no success for `lease_duration` makes `err()` return `BatchLeaseExpiredError`.
- **`members`:** warmpool filter and snapshot immutability (a returned `Member` does not change after a later watch event).
- **`connect`:**
  - builds `client.sandbox_class` without calling `resolve_sandbox_name` or `get_sandbox`;
  - a repeat call returns the same handle;
  - a not-ready member raises.
- **`detach`:**
  - idempotent; closes handles; unregisters; later calls raise;
  - each of `grace=1.5`, `30.0`, `True`, `"30"`, `0`, `-5`, and `5` raises `ValueError`, with no Lease write and the watch and renewal still running. `5` (equal to `CLOCK_SKEW_MARGIN`) raises with exactly the message `"Duration must be greater than clock skew margin (5s)"`;
  - `grace=6` is accepted;
  - the final Lease update has `holderIdentity` of `None`, `renewTime` near now, and `leaseDurationSeconds == grace`;
  - with `grace=None`, `leaseDurationSeconds` equals the handle's `lease_duration`;
  - annotations are unchanged, and no new annotation is added.
- **Client:** `AsyncSandboxClient.close()` stops the tracked batches' tasks.
- **Label selector:** the watcher and list calls use exactly `agents.x-k8s.io/batch-id=<id>`.

**E2E (recommended):**

1. Apply two claims labeled with a batch id, annotated for one size-2 group, plus a Lease.
2. `get_batch` them.
3. Wait until `members()` shows both Ready.
4. `connect` one and run a command.
5. `detach`.
6. Check that the claims still exist and the Lease holder is cleared.

**Acceptance:** the unit tests above pass in both shells; the parity checklist is complete (every public method exists on both classes with matching signatures, modulo `async`); the README has a "Batch claims" section covering `get_batch`, `members`, `connect`, and `detach`, plus the driver Role from the proposal's RBAC section; the reference docs are regenerated; the scope diff check is empty.

## PR 2: `claim_batch`, upfront cohorts, `events`, `iter_ready_groups`, `release`

**Goal.** Create a batch with one or more non-zero groups, create their claims in the background with pacing, and let callers consume them either as a stream (`events()`) or per group (`iter_ready_groups()`). `release()` tears everything down. After this PR, the proposal's usage examples 2 and 3 work end to end.

**Depends on:** PR 1. **Blocked on:** nothing; every PR 2 decision is resolved.

**Files:**

- `constants.py`: `BATCH_WORK_BUDGET_ANNOTATION`, `BATCH_QUORUM_TIMEOUT_ANNOTATION`, the OPEN-1b defaults (`BATCH_DEFAULT_CREATE_RPS = 50.0`, `BATCH_DEFAULT_MAX_IN_FLIGHT = 20`, `BATCH_CREATE_MAX_ATTEMPTS = 3`), and the plurals the OPEN-W precheck needs: `sandboxwarmpools` and `sandboxtemplates`, both in `CLAIM_API_GROUP`.
- `models.py`: `claim_batch` input validation helpers if they don't fit in the client.
- `exceptions.py`: `BatchExistsError` and `QuorumUnreachableError`.
- `batch_state.py`:
  - ordinal allocation for the fill;
  - create-failure members;
  - per-group accounting over initial-fill members;
  - settle detection;
  - the mode lock and hold-back logic (OPEN-F);
  - `parse_positive_int_annotation` for the two new Lease annotations, alongside `parse_lease_duration_annotation`;
  - re-key `reconstruct_groups`'s final ordering from the pool name to each pool's lowest ordinal, so a re-attached handle reports `groups` in the order `claim_batch` was given them rather than alphabetically. Sorting stays: set iteration order varies between processes under hash randomization. Pools with no ordinal-parseable claim sort last, by name.
- `k8s_helper.py` and `async_k8s_helper.py`:
  - `create_batch_lease` and `delete_batch_lease` *(new)*;
  - `get_sandbox_warmpool(name, namespace)` and `get_sandbox_template(name, namespace)` *(new)*, for the OPEN-W precheck. Each raises the SDK's existing `SandboxWarmPoolNotFoundError` / `SandboxTemplateNotFoundError` on 404 and lets every other status propagate;
  - `delete_sandbox_claim_collection(namespace, label_selector)` *(new)*, which wraps `CustomObjectsApi.delete_collection_namespaced_custom_object`. That method exists in `kubernetes` 36.0.3 and in the `kubernetes_asyncio` the `<34.0.0` pin resolves to (33.3.0, checked in the wheel).
- `sandbox_batch.py` and `async_sandbox_batch.py`:
  - the creation pipeline and the create-retry helper;
  - `events()`, `iter_ready_groups()`, `release()`;
  - `LEASE_DEGRADED` emission;
  - `_attach` also parses the two new annotations into the handle, so PR 4's replacements can compute `shutdownTime` (OPEN-E).
- `sandbox_client.py` and `async_sandbox_client.py`:
  - `claim_batch(groups, *, namespace="default", labels=None, batch_id=None, create_rps=None, max_in_flight=None, work_budget=None, quorum_timeout=None, lease_duration=None)` *(new)*, with the proposal's signature except that `lease_duration`, `work_budget`, and `quorum_timeout` are all typed `int | None` rather than `float | None` (OPEN-M, OPEN-T);
  - exit hooks: `SandboxClient.delete_all()` releases tracked batches, which covers the sync atexit path when `cleanup=True`;
  - `AsyncSandboxClient.delete_all()` and `__aexit__` release them;
  - `AsyncSandboxClient._atexit_cleanup` releases them through the synchronous `K8sHelper` it already builds (`deletecollection` then Lease delete, each bounded by `_ATEXIT_DELETE_REQUEST_TIMEOUT_SECONDS`), for the same interpreter-shutdown reason documented on that method.
- `README.md`, reference docs.

**Behavior:**

- **Validation:** groups must be non-empty; every `size > 0` (`size=0` is enabled in PR 4); duplicate `warmpool` names are rejected, because members are bucketed by `spec.warmPoolRef.name` and two groups on one pool could not be told apart; `labels` go through `validate_labels` and must not set `BATCH_ID_LABEL`; `lease_duration` must be `None` or an `int` greater than `CLOCK_SKEW_MARGIN` ("Duration validation" in the Lease contract, OPEN-M); `work_budget` and `quorum_timeout` must each be `None` or an `int` greater than 0 (OPEN-T). Anything else raises `ValueError`, before any object is created.
- **Dependency precheck (OPEN-W):** after the local checks and before the Lease is created, `GET` each group's `SandboxWarmPool` in the batch's namespace, then `GET` the template it names in `spec.sandboxTemplateRef.name`, in that same namespace. This mirrors the controller's own lookups (`getWarmPool` / `getTemplateForWarmPool`). A missing pool or template raises before anything exists, which is what makes the two dependency reasons safe to treat as non-terminal at runtime. Two `GET`s per group; any non-404 error propagates rather than being swallowed.
- **Sequence:**
  1. Resolve or validate the id, and precheck each group's warm pool and template.
  2. Create the Lease with `leaseDurationSeconds = lease_duration`, and annotations `BATCH_LEASE_DURATION_ANNOTATION`, `BATCH_WORK_BUDGET_ANNOTATION`, and `BATCH_QUORUM_TIMEOUT_ANNOTATION` (OPEN-E). A 409 raises `BatchExistsError`.
  3. List and start the watch. The watch must be running before the first claim `POST`, so no status transition can be missed; the initial list is what PR 1's reconnection path resumes from.
  4. Start renewal.
  5. Start background creation (recommended; the proposal is silent on whether `claim_batch` blocks on creates).
  6. Register the handle and return it.
  7. If any step before 5 fails, delete the Lease and re-raise.
- **Creation:**
  - Every claim name is computed upfront: ordinals `0..N-1` assigned across groups in the order the groups were passed, so worker tasks never allocate an ordinal and the name of every planned member is known before the first `POST`. The manifest follows "Claim manifest" above; `shutdownTime` is computed at each claim's own create time.
  - Per-ordinal create outcomes are written into a pre-allocated table indexed by ordinal, so workers do not contend for a lock to record success. Anything that touches `BatchState` (create-failure members, group counters) still goes through the handle's existing lock, since the watcher writes there too.
  - A token bucket enforces `create_rps`, and at most `max_in_flight` creates run at once (sync: bounded executor fed by a paced producer; async: a semaphore plus a paced producer task).
  - Create failures become synthetic terminal members and emit `MEMBER_FAILED`.
  - **Per-group fail-fast (OPEN-S).** Track create failures per group. Once a group's failures cross `create_failed > size - min_ready`, cancel that group's not-yet-started creates and mark the group unreachable, so `iter_ready_groups()` yields it with `QuorumUnreachableError`. Other groups keep filling; the batch is never auto-released, and no already-created member is deleted. Only the caller's `release()` tears the batch down.
  - `release()` cancels outstanding creates, and its `deletecollection` then catches any create that landed.
- **`events()`:**
  - Single consumer: a second call raises `BatchError`.
  - `MEMBER_READY` for initial-fill members not yet dispatched (subject to hold-back under OPEN-F); `MEMBER_FAILED` and `MEMBER_LOST` for initial-fill members only, meaning the ones in `BatchState._initial_fill` (OPEN-H); `LEASE_DEGRADED` from the renewal loop.
  - Ends after draining once the initial fill settles (every initial-fill member is Ready, terminal, lost, released, or create-failed; released ones count as unable to arrive, OPEN-5b), or on `release()`/`detach()`.
  - A member still pending at `quorum_timeout` also counts as unable to arrive (OPEN-X), so the stream closes even when the apiserver keeps a claim alive indefinitely. This deadline applies in both consumer modes, including stream-only, where no group ever yields a verdict.
  - On a re-attached handle the dispatched set starts empty, so members that were already Ready at attach time are emitted to this handle's `events()` in ordinal order unless a quorum consumer takes them first (OPEN-G).
- **`iter_ready_groups()`:**
  - Yields one `GroupReady` per non-zero group, as soon as either:
    - the group has `>= min_ready` Ready members: `members` = exactly `min_ready`, earliest-Ready first (ordinal order for members already Ready at attach), dispatched atomically;
    - or the group becomes unreachable, meaning `size - terminal - lost - create_failed - released < min_ready` (proposal formula plus OPEN-5b): `members=[]`, `error=QuorumUnreachableError`.
  - A group that has not yielded within `quorum_timeout` yields `error=TimeoutError("Group quorum timed out")` (OPEN-J).
  - Works the same on a re-attached handle: quorum is a level-triggered check against current cluster state, so a group already at `min_ready` yields immediately without blocking (OPEN-G).
  - Closes once every non-zero group has yielded exactly once.
  - A group with `min_ready=0` yields immediately with no members.
  - Remaining Ready members of that group go to `events()`, as do the held-back members of a group that yielded an error.
- **`release()`:** idempotent.
  1. Stop creation, the watch, and renewal.
  2. Close connected handles.
  3. `deletecollection` with `BATCH_ID_LABEL=<id>`.
  4. Re-list by label and repeat, bounded, until only claims with `deletionTimestamp` remain (proposal, Cleanup 1).
  5. Delete the Lease last, so the reaper can still clean up if `release` dies partway.
  6. Wake all waiters with `BatchError` and end `events()`.
  7. Unregister from the client.

  A failing deletion raises (OPEN-U: a 403 means the driver Role is missing the `deletecollection` verb the proposal's RBAC section grants, so there is no fallback path). Steps 6 and 7 are the exception to that: waking waiters and ending `events()` happen even when step 3 or 4 raises, since the watch is already stopped and a consumer left blocked would never be woken by anything else. The Lease is not deleted and the handle is not unregistered, so the reaper can still act and a later `release()` retries the deletion.

**Unit tests** (both shells):

- **Validation matrix:** empty groups, `size=0` (rejected in this PR), `min_ready > size`, negative values, duplicate pools, reserved label, bad `batch_id`. Each of `work_budget` and `quorum_timeout` rejects `1.5`, `60.0`, `True`, `"60"`, `0`, and `-5` with `ValueError` before the Lease is created (OPEN-T).
- **Sequence order:** Lease create, then list/watch, then first claim create (asserted on call order). A Lease 409 raises `BatchExistsError`, and nothing else is created. A watch-start failure deletes the Lease.
- **Lease duration:**
  - `claim_batch(lease_duration=90)` creates the Lease with `leaseDurationSeconds: 90` and `batch-lease-duration: "90"`, and renews every 30 s;
  - each of `lease_duration=1.5`, `60.0`, `True`, `"60"`, `0`, `-5`, and `5` raises `ValueError` before the Lease is created. `5` raises with exactly the message `"Duration must be greater than clock skew margin (5s)"`;
  - `lease_duration=6` is accepted and renews every 2 s;
  - the default creates both fields as 60 and renews every 20 s;
  - a `get_batch` takeover of a PR 2-created Lease restores the caller's 90 s.
- **Manifests:**
  - ordinals `0..N-1` in group order;
  - group annotations match each group;
  - labels include the batch label;
  - `shutdownTime` equals create time plus the budget, checked with a fake clock at two different create times.
- **Pacing:**
  - with a fake clock, no more than `create_rps` creates start in any second;
  - an instrumented fake create never sees more than `max_in_flight` concurrent calls.
- **Retry classification table:** 429 and 503 retry, then succeed; 409 after a retried 503 counts as success; 409 on the first attempt fails; 400/403/404/422 fail with no retry; a third attempt is the last one (OPEN-1b) and exhausted retries fail; `Retry-After` is honored.
- **Dependency precheck (OPEN-W):** a missing warm pool raises `SandboxWarmPoolNotFoundError` and a missing template raises `SandboxTemplateNotFoundError`, both before the Lease is created and with no claim creates; a 500 on either `GET` propagates; with both present, `claim_batch` proceeds. At runtime, a member whose `Ready` condition is `False` with reason `WarmPoolNotFound` or `TemplateNotFound` is not terminal, emits no `MEMBER_FAILED`, does not count against its group's reachable count, and lets the group time out through OPEN-J if it never resolves.
- **Lease annotations (OPEN-E):** `claim_batch(work_budget=1200, quorum_timeout=300)` writes `batch-work-budget: "1200"` and `batch-quorum-timeout: "300"`; a `get_batch` takeover reads both back onto the handle; a missing annotation falls back to the default; a non-integer or non-positive one raises `BatchError`.
- **Per-group fail-fast (OPEN-S):** a size-4, `min_ready=3` group whose creates fail twice cancels its remaining creates and yields `QuorumUnreachableError`, while a healthy second group still yields its members; no `deletecollection` is issued and the Lease still exists (no auto-release).
- **`claim_batch` returns before all creates finish.**
- **`events()`:**
  - ordering;
  - `MEMBER_FAILED` for create failures and terminal claims; `MEMBER_LOST`;
  - closes on settle with a mixed outcome;
  - closes on `release()`;
  - a second consumer raises;
  - `LEASE_DEGRADED` emitted once per episode.
- **`iter_ready_groups()`:**
  - a fast group yields before a slow group;
  - exactly `min_ready` members per yield, with extras streamed on `events()`;
  - an unreachable group yields an error while others still yield members;
  - `min_ready=0`;
  - a group stuck below `min_ready` yields `error=TimeoutError` at `quorum_timeout` on a fake clock, and the other groups are unaffected (OPEN-J);
  - closes after all groups yield.
- **OPEN-F:** quorum-consumer-first holds back exactly `min_ready` per group from `events()` and streams the extras immediately; `events()`-first makes `iter_ready_groups()` raise `BatchError`; a group that yields `QuorumUnreachableError` or `TimeoutError` releases its held-back members to `events()` rather than stranding them.
- **OPEN-G, re-attached handle:** `get_batch` on a batch whose group is already at `min_ready` returns from `iter_ready_groups()` immediately without blocking; members dispatched by that call are not re-emitted by the same handle's `events()`; members already Ready at attach and not taken by a quorum consumer are emitted in ordinal order.
- **Dispatched set:** no member is ever handed out twice across `events()` and `iter_ready_groups()` *on one handle*, under interleaved watch updates (seeded random interleavings in the state core).
- **`release()`:**
  - `deletecollection` selector is correct;
  - re-list loop ends when only terminating claims remain;
  - Lease deleted last;
  - idempotent;
  - wakes waiters;
  - cancels creates;
  - a 403 from `deletecollection` propagates, leaves the Lease in place and the handle registered, and still wakes waiters and ends `events()`; a second `release()` retries the deletion and succeeds (OPEN-U).
- **Exit hooks:**
  - `delete_all()` releases batches;
  - `__aexit__` releases;
  - the async atexit path calls the sync helper's `deletecollection` and Lease delete with the request timeout;
  - detached batches are skipped.

**E2E (recommended):**

1. `claim_batch` with one size-3 group.
2. Iterate `events()` to close; expect 3 `MEMBER_READY`.
3. `release()`; no labeled claims and no Lease remain.

Also: two groups, one on a nonexistent warm pool. `iter_ready_groups()` yields an error for that group and members for the other.

**Acceptance:** tests pass in both shells; README documents stream and per-group consumption, including the OPEN-F ordering rule; reference docs regenerated; scope diff empty.

## PR 3: `wait_for_quorum`

**Goal.** Add a global barrier: block until every non-zero group reaches `min_ready`, then return those members. The proposal's usage example 1 works after this PR.

**Depends on:** PR 2. **Blocked on:** nothing beyond PR 2's items.

**Files:** `batch_state.py` (cross-group join), `sandbox_batch.py`, `async_sandbox_batch.py`, `README.md`, reference docs.

**Behavior:**

- **Signature:** `wait_for_quorum(timeout=None)`, where `None` means the batch's `quorum_timeout`.
- **Success:** when every non-zero group has `>= min_ready` Ready members, return exactly `min_ready` per group. The list is in group order, earliest-Ready first within a group, and dispatched atomically.
- **Fast-fail:** raise `QuorumUnreachableError` naming the group as soon as any group is unreachable (same formula as `iter_ready_groups`).
- **Timeout:** raise `TimeoutError`.
- **Misuse:**
  - raises if `iter_ready_groups()` was already called, and `iter_ready_groups()` raises if `wait_for_quorum()` was (the proposal requires mutual exclusion);
  - a second `wait_for_quorum()` raises;
  - follows the OPEN-F mode lock with respect to `events()`.
- **Re-attached handles are supported** (OPEN-G): quorum is level-triggered, so a batch already at `min_ready` in every group returns immediately, with ordinal order standing in for Ready order among members that were already Ready at attach.
- **After quorum:** remaining and late Ready members stream on `events()`.

**Unit tests** (both shells):

- returns exactly `min_ready` per group across 3 groups;
- a re-attached handle whose groups are already at `min_ready` returns immediately (OPEN-G);
- blocks while one group is short;
- one unreachable group fails the call while others are healthy;
- timeout;
- mutual exclusion in both orders;
- second call raises;
- `events()` after quorum yields only the extras;
- `events()`-first makes `wait_for_quorum()` raise (OPEN-F);
- `release()` during the wait raises `BatchError`.

**E2E (recommended):** a single group with size 3 and `min_ready` 2. `wait_for_quorum` returns 2, `events()` yields the third, then release.

**Acceptance:** tests pass in both shells; README shows the fixed-cohort pattern (proposal usage example 1); reference docs regenerated; scope diff empty.

## PR 4: `acquire`, `replace`, `release_member`, `release_not_ready`, lazy groups

**Goal.** Add on-demand members: lazy `size=0` groups, and the ability to acquire, replace, and release individual members. The proposal's usage example 4 and the RL `BatchClaimer` pattern work after this PR.

**Depends on:** PR 3. **Blocked on:** OPEN-1c (`acquire` timeout).

**Files:** `batch_state.py` (acquire-owned members, released marking), `sandbox_batch.py`, `async_sandbox_batch.py`, `exceptions.py` (`TerminalMemberError`), `sandbox_client.py`/`async_sandbox_client.py` (validation now allows `size=0`), `README.md`, reference docs.

**Behavior:**

- **`claim_batch` now accepts `size=0` groups.** `min_ready` defaults to 0 for them. They get no group annotations (proposal, Batch Model 4). An empty groups list is allowed, since `acquire` can add any pool (recommended). The quorum consumers skip `size=0` groups; a batch with only `size=0` groups has `iter_ready_groups()` close immediately and `wait_for_quorum()` return `[]`.
- **`acquire(warmpool, timeout=None)`:**
  1. Allocate the next ordinal.
  2. Register the member as acquire-owned in state before issuing the create, so a watch event cannot beat the waiter.
  3. Create through the shared retry helper. If the pool belongs to a size>0 group, the claim carries that group's annotations. An unknown pool becomes an implicit `size=0` group (proposal: "from any pool in its namespace").
  4. Wait for Ready, then dispatch and return the member.
  - Terminal: delete the claim and raise `TerminalMemberError`.
  - Lost: raise `TerminalMemberError`.
  - Timeout: delete the claim and raise `TimeoutError`.
  - `release()`/`detach()` during the wait: raise `BatchError`.
  - `shutdownTime` uses the batch's budget, which a re-attached handle already parsed from the Lease annotations in PR 2 (OPEN-E).
- **`release_member(member)`:** mark released, delete the claim (404 is fine), close the member's connected handle. Idempotent.
- **`replace(member, timeout=None)`:** `release_member(member)`, then `acquire(member.warmpool, timeout)`.
- **`release_not_ready()`:** mark, then individually delete, every member that is not Ready (terminal included). Skip members owned by an in-flight `acquire`. `deletecollection` cannot select by readiness, so these are individual deletes.
- **Acquired members** never appear on `events()` (OPEN-H), and never count toward quorum.

**Unit tests** (both shells):

- `acquire`:
  - happy path;
  - Ready observed before the waiter exists;
  - terminal deletes and raises `TerminalMemberError`, with `except SandboxClaimFailedError` still catching it;
  - timeout deletes and raises;
  - release during the wait;
  - unknown pool creates an implicit group;
  - size>0-group annotations are copied onto acquired claims;
  - ordinal is monotonic after releases.
- `release_member`: idempotent; no `MEMBER_LOST`; closes the connected handle.
- `replace`: new ordinal, same pool.
- `release_not_ready`: skips Ready members and in-flight acquires; emits no `MEMBER_LOST`.
- Groups:
  - a `size=0` group is ignored by both quorum consumers;
  - a size-0-only batch: `iter_ready_groups()` closes immediately, `wait_for_quorum()` returns `[]`;
  - acquired members never appear on `events()`.
- Re-attached handle: `acquire` uses the Lease-annotated budget, or the defaults when the annotations are absent.

**E2E (recommended):** a batch with one `size=0` group. Acquire 2, `replace` 1, `release_member` 1, then `release`. No labeled claims remain.

**Acceptance:** tests pass in both shells; README covers lazy batches and rolling replacement; reference docs regenerated; scope diff empty.

## PR 5: Control-plane connection pool sizing

**Goal.** Make the Kubernetes API connection pool large enough for batch creation, following the proposal's construction-time configuration with runtime validation.

**Depends on:** PR 4, and on open PR #1509 (injectable `ApiClient`), which changes the same four constructors. Land after #1509, or rebase onto it. **Blocked on:** OPEN-B, OPEN-I.

**Files:** `sandbox_client.py`, `async_sandbox_client.py`, `k8s_helper.py`, `async_k8s_helper.py`, `sandbox_batch.py`/`async_sandbox_batch.py` (validation hook), `README.md`, reference docs.

**Behavior:**

- **`pool_size`:** `pool_size: int | None = None` *(new)* on `SandboxClient` and `AsyncSandboxClient`. When set, it sets `connection_pool_maxsize` on the Kubernetes `Configuration` the helper's `ApiClient` is built from, before that client is created.
- **With #1509's `api_client`:** passing both `pool_size` and `api_client` raises `ValueError`. With an injected client, validation reads `api_client.configuration.connection_pool_maxsize`.
- **Validation in `claim_batch`:** the effective pool must be `>= max_in_flight` (+2 per OPEN-I). Otherwise raise `ValueError` naming both fixes: lower `max_in_flight`, or construct the client with a larger `pool_size` (proposal wording).
- **Default `max_in_flight`:** the `kubernetes` client's default pool is `cpu_count() * 5` (proposal, citing `configuration.py`). A fixed default can therefore fail validation on small machines. Recommended: when `max_in_flight` is not given, derive it as `min(DEFAULT_MAX_IN_FLIGHT, pool - 2)`.
- **Data plane:** no change until OPEN-B is decided.

**Unit tests** (both shells):

- `pool_size` reaches `connection_pool_maxsize`;
- `pool_size` with `api_client` raises;
- validation passes and fails at the boundary;
- the injected client's pool is read;
- default `max_in_flight` derivation on a small pool.

**Acceptance:** tests pass; README documents `pool_size` and the validation error; reference docs regenerated; scope diff empty.

## Follow-ups (not specified in detail here)

- **PR 6, reaper.** It consumes the Lease contract above. Language, location, and image are undecided in the proposal:
  - A Go binary under `cmd/` needs a new image, which touches release tooling that AGENTS.md says not to edit by hand.
  - A Python module in the SDK, run by a CronJob that installs the package, needs no new image but pays a `pip install` on every run.

  It owns the reaper's staleness margin (OPEN-V), which is separate from `get_batch`'s `CLOCK_SKEW_MARGIN` and points the other way. Until it ships, crash cleanup relies only on each claim's `shutdownTime`, which is the intended defense-in-depth backstop.
- **PR 7, examples and docs.** `examples/<name>/` with a README, the driver `Role` from the proposal's RBAC section, and runnable versions of the usage examples.
- **Release window.** If a release is cut from `main` mid-stack, it ships a partial batch API: `get_batch` with no `claim_batch` after PR 1 alone, and no `size=0` groups until PR 4. Options: land the stack between releases, or mark the README section as preview until PR 5.

## Handing a PR to an agent

Implementation branches are based on `main`, not on the proposal branch, so a worktree for them will not contain the proposal or this plan. Give agents absolute paths:

```
Implement PR <N> from /Users/brian/Desktop/comp_sci/google/agent-sandbox/batch-primitives/python_sdk_batch_plan.md.
Read /Users/brian/Desktop/comp_sci/google/agent-sandbox/batch-primitives/batch_claim_proposal.md first, then the plan's
"Ground rules", "Module layout", "Shared contracts", "Decisions", and "PR <N>". Treat "Resolved" decisions as fixed;
do not implement anything that depends on a "Still open" item without asking.
Work on branch <branch> (stacked on <parent>). Implement only PR <N>'s scope. Commit and push only as this prompt authorizes; never open a PR.
Finish by running make test-unit and reporting results and any deviations from the plan.
```
