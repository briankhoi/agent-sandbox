# PR 2 design (clean room): `claim_batch`, `events`, `iter_ready_groups`, `release`

Written 2026-09-26 by following `pr2_redesign_prompt.md`, phase 1. It builds on PR 1 at `feat/batch-1-core` `f4a4867` (on upstream `68db683`). I didn't read the old PR 2 (`feat/batch-2-cohorts`, `df1c11d`), `PR_2_prompt.md`, `pr2_revision_prompt.md`, or the forbidden parts of the plan before this design was committed.

One thing did get through: `review_carryover_notes.md` ("Rules to carry into PRs 3–5", which the prompt asks me to read) names a few old internals in passing: `_create_with_retry`, `parse_retry_after`, `BATCH_STOP_CREATION_TIMEOUT_SECONDS`, and "R4/R5". I didn't reuse any of those names or structures. Where this design needs the same idea (honoring `Retry-After`), it gets its own name and shape.

Terms used below:
- **Fill**: the batch's initial claims, ordinals `0..N-1`, where `N = size`, the sum of group sizes.
- **Unable**: a fill member that can no longer become Ready: terminal, lost, create failed, or create skipped.
- **Verdict**: a group's quorum outcome: success, `QuorumUnreachableError`, or `TimeoutError`.

## 1. Requirements

Sources:
- **P:** the proposal, `batch_claim_proposal.md`, by section.
- **C:** the plan's "Shared contracts".
- **OPEN-x:** Brian's resolved decisions.
- **AB:** the approved behavior list in `pr2_redesign_prompt.md`.
- **GR:** the plan's ground rules.
- **CN:** `review_carryover_notes.md`.

| # | Requirement | Source |
| :-- | :-- | :-- |
| R1 | `claim_batch(groups, *, namespace, labels, batch_id, create_rps, max_in_flight, work_budget, quorum_timeout, lease_duration)` on both clients. It returns a handle right away, and creation continues in the background. | P "SDK API Additions → Python"; P "Core Batch Methods" |
| R2 | Validate inputs before anything is written: `min_ready <= size` (P); `lease_duration` is `None` or an `int` greater than `CLOCK_SKEW_MARGIN` (OPEN-M); `work_budget` and `quorum_timeout` are `None` or an `int` greater than 0 (OPEN-T); `batch_id` is valid (C "Batch id"); caller labels are valid and don't set `BATCH_ID_LABEL` (C "Claim manifest"). | P, OPEN-M, OPEN-T, C |
| R3 | Batch id: generated as `"b"` plus 11 random `[a-z0-9]` characters, or supplied by the caller. | C "Batch id"; P "Batch Model" 1 |
| R4 | Precheck every group's `SandboxWarmPool`, and the `SandboxTemplate` its `spec.sandboxTemplateRef.name` names. A missing one raises before any object is created. | OPEN-W |
| R5 | Create the Lease `batch-<id>` before the watch and before any claim, with labels `{BATCH_ID_LABEL, CREATED_BY_LABEL}`, this handle's `holderIdentity`, `leaseDurationSeconds`, `acquireTime`/`renewTime`, and the annotations `batch-lease-duration`, `batch-work-budget`, `batch-quorum-timeout`. A 409 raises `BatchExistsError`. | C "Lease"; OPEN-K; OPEN-E; C "Exceptions"; P "Liveness" |
| R6 | Start the label-scoped watch before any create, so no event is missed. | P "Batch Lifecycle Diagram" |
| R7 | Claim manifest: name `<id>-<ordinal>`, with ordinals `0..N-1` in the order the groups were passed; labels are the caller's plus `BATCH_ID_LABEL` (the helper adds `CREATED_BY_LABEL`); annotations are the two group annotations plus the trace context, computed once per batch; lifecycle is `shutdownTime = own create time + quorum_timeout + work_budget + 600 s` with `shutdownPolicy: Delete`. | C "Names", C "Claim manifest"; P "Shutdown Backstop"; OPEN-1 |
| R8 | Pacing: at most `create_rps` create starts per second (default 50.0) and at most `max_in_flight` creates in flight (default 20). | P "Paced Creation"; OPEN-1b |
| R9 | Create retry: 429, 5xx, and transport errors are retried with jittered backoff, preferring `Retry-After`, up to 3 attempts in total. 400/403/404/422 and exhausted retries are create failures. A 409 on the first attempt is a create failure (name collision); a 409 on a later attempt is a success. | C "Create retry"; OPEN-1b; AB "Retry policy is shared" |
| R10 | A create failure is a synthetic terminal member with reason `"CreateFailed"` and the API error as its message. It shows in `members()` and counts toward the unreachability test. | C "`Member` derivation"; P "Events and Quorum" |
| R11 | `events()`: `MEMBER_READY` as fill members become Ready, each handed out at most once per handle; `MEMBER_FAILED` for terminal members; `MEMBER_LOST` for lost ones; `LEASE_DEGRADED` once per degraded episode. It closes when the fill settles. | P "Events and Quorum", "Liveness"; C "Dispatched set"; OPEN-2; OPEN-H |
| R12 | `iter_ready_groups()`: yields one `GroupReady` per group as soon as that group's `min_ready` is met (`members` = exactly `min_ready` members), or it becomes unreachable (`size - failed - lost < min_ready` → `QuorumUnreachableError`), or its deadline passes (`TimeoutError("Group quorum timed out")`). It closes once every group has yielded. | P "Events and Quorum"; OPEN-J; C "Exceptions" |
| R13 | Consumer mode: the first of `events()`/`iter_ready_groups()` fixes it. If `events()` comes first, a later `iter_ready_groups()` raises `BatchError`. If `iter_ready_groups()` comes first, `events()` holds a group's Ready members back from the stream until that group's cohort has been yielded. | OPEN-F (see D1) |
| R14 | A failed group is finished (quorum mode): no more creates; no `MEMBER_READY` for any of its members; its `MEMBER_FAILED`/`MEMBER_LOST` still stream; its members stay in `members()`; nothing is deleted. A successful group's extras still stream. Stream-only batches keep creating and deliver everything. | AB "A failed group is finished" |
| R15 | Fill deadline: group timeouts and the close of `events()` are counted `quorum_timeout` from the pacing slot of the batch's last create. A re-attached handle counts from attach. Past the deadline, every still-pending fill member counts as unable. | AB "The fill deadline covers pacing"; OPEN-X |
| R16 | Re-attached handles support both consumers, level-triggered: a group already at `min_ready` yields right away, and members that were Ready before attach are ordered by ordinal. `get_batch` reads `batch-quorum-timeout`: missing falls back to the default, and an invalid value raises `BatchError`. | OPEN-G; OPEN-E; C "Names" |
| R17 | `release()`: stops creation, the watch, and renewal; closes cached connections; then label `deletecollection` in bounded rounds, re-listing for claims without a `deletionTimestamp`; the Lease is deleted last. A 429, 5xx, or transport error inside a round moves to the next round after a backoff that prefers `Retry-After`. After the last round it raises the last transient error, or `BatchError` for claims still not deleting. A 403 raises immediately. | AB "`release()` survives transient errors"; OPEN-U; P "Cleanup" 1 |
| R18 | `detach()` on a handle that is still creating stops creation first, and leaves every claim in place. | P "Core Batch Methods" (`detach`); C "Detach" |
| R19 | Request timeouts on every request made from a background loop or on a path the caller can't interrupt, including sync `release()`, sync `detach()`, and exit cleanup. | AB "Request timeouts"; CN |
| R20 | Exit hooks release batches: `release()` is "called explicitly or by the SDK's exit hooks". Both clients track their batch handles again, with parity. | P "Cleanup" 1; plan "Changes relative to the revised roadmap" 7; CN "Deferred from PR 1" |
| R21 | Scale: tens of thousands of claims. No O(N) work per watch event. Timings are checked against pacing, `deletecollection`, and watch-cache aging. | AB "Scale"; P "Scalability" |
| R22 | RBAC: the README Role gains exactly the verbs PR 2 uses. | AB "RBAC" |
| R23 | Only what PR 2 has: exports, comments, docs, and code whose callers are in PR 2. | AB "Only what PR 2 has"; CN |
| R24 | Sync/async parity: the same public surface and the same tests in both shells. | GR |
| R25 | New exports: `BatchEventType`, `BatchEvent`, `GroupReady`, `BatchExistsError`, `QuorumUnreachableError`. | AB "Event types"; CN "Export only what the PR produces" |

## 2. Scope

**PR 2 ships:**
- `claim_batch` on both clients.
- On both handles: `events()`, `iter_ready_groups()`, `release()`, and creation in the background.
- `LEASE_DEGRADED`.
- The fill deadline.
- `get_batch` reading `batch-quorum-timeout`.
- Client tracking of batch handles, with `delete_all()` and exit-hook release, in both clients.
- The README section and RBAC update, regenerated docs, and one e2e test.

**Left to later PRs** (unchanged from the plan):
- **PR 3:** `wait_for_quorum`.
- **PR 4:**
  - `acquire`, `replace`, `release_member`, `release_not_ready`;
  - `size=0` groups and `TerminalMemberError`;
  - reading `batch-work-budget` in `get_batch`;
  - the `delete` verb.
- **PR 5:** `pool_size` validation.
- **PR 6:** the reaper.

No feature moves between PRs. The one scope call the prompt asked for: **client batch tracking and cleanup comes back in PR 2**, because R20 needs it.

## 3. Public API

### Clients

```python
class SandboxClient:
    def claim_batch(
        self,
        groups: Sequence[BatchGroup],
        *,
        namespace: str = "default",
        labels: dict[str, str] | None = None,
        batch_id: str | None = None,
        create_rps: float | None = None,       # None → 50.0
        max_in_flight: int | None = None,      # None → 20
        work_budget: int | None = None,        # None → 3600 s
        quorum_timeout: int | None = None,     # None → 600 s
        lease_duration: int | None = None,     # None → 60 s
    ) -> SandboxBatch: ...

class AsyncSandboxClient:
    async def claim_batch(...same...) -> AsyncSandboxBatch: ...
```

The call does these steps and returns:
1. Validate.
2. Precheck the warm pools and templates.
3. Create the Lease.
4. List the claims to get a resourceVersion to watch from; this also checks that no claim carries the id yet.
5. Start the watch and renewal.
6. Start creation in the background.

Raises:
- `ValueError`: invalid arguments (R2). This includes an empty `groups`, a group with `size` 0, and two groups naming the same warm pool.
- `SandboxWarmPoolNotFoundError` / `SandboxTemplateNotFoundError`: from the precheck, before anything is written. These are the SDK's existing exceptions.
- `BatchExistsError`: the Lease already exists (409), or claims with this batch id already exist. In the second case the new Lease is deleted before raising.
- `kubernetes` `ApiException`: any other API error before the first create, such as a 403 on the Lease. If the Lease was already created, it is deleted best-effort first.

A `size=0` group is rejected with "size must be at least 1". PR 4 relaxes that, and the message doesn't mention it.

`get_batch` keeps its signature. Its `Raises: BatchError` line gains "or the Lease's quorum-timeout annotation is invalid".

`delete_all()` on both clients now also releases every tracked batch handle; see §4.6.

### Handles (`SandboxBatch`; `AsyncSandboxBatch` is the same with `async`)

```python
def events(self) -> Iterator[BatchEvent]: ...              # async: def events(self) -> AsyncIterator[BatchEvent]
def iter_ready_groups(self) -> Iterator[GroupReady]: ...   # async: AsyncIterator[GroupReady]
def release(self) -> None: ...                             # async: async def release(self)
```

- **`events()`**
  - Fixes stream mode if no mode is set yet. It sets the mode when it is called, not on the first `next()`: it is a plain method that returns a generator.
  - Yields events in the order the handle observed them.
  - Ends when the fill has settled and everything queued has been yielded, or once `err()` is set and the queue is drained.
  - It can be called again and continues from where the stream is. It doesn't replay.
  - Raises `BatchError` if the handle has been released or detached, whether at call time or while waiting.
- **`iter_ready_groups()`**
  - Fixes quorum mode if no mode is set yet.
  - Yields `GroupReady`: `members` holds exactly `min_ready` members for a success; for a failure, `error` is set and `members` is `[]`.
  - Ends when every group has yielded, or once `err()` is set and the queue is drained.
  - Raises `BatchError` if `events()` fixed stream mode, or if the handle is released or detached.
  - Calling it again continues with the groups that haven't yielded yet.
- **`release()`**
  - Idempotent.
  - Raises `BatchError` if the handle was detached (the Lease isn't this handle's any more).
  - Raises the `ApiException` for a 403 immediately.
  - If the bounded rounds end with claims left: raises the last transient error (`ApiException` or transport error), or else `BatchError` naming how many claims are still not deleting.
  - On any failure the Lease stays, and calling `release()` again retries the rounds.
  - Afterwards, `connect`/`events`/`iter_ready_groups`/`detach` raise `BatchError`, except that `detach()` after `release()` is a no-op, as `detach()` after `detach()` is today.
- **`detach(grace)`**: as in PR 1, and it also stops creation first (R18). Its Lease read and write now have a request timeout (R19).

### Models, exceptions, constants

- `models.py`:
  - `BatchEventType(str, Enum)`: `MEMBER_READY`, `MEMBER_LOST`, `MEMBER_FAILED`, `LEASE_DEGRADED` (P).
  - `BatchEvent(BaseModel, frozen)`: `type`, `member: Member | None = None`.
  - `GroupReady`: `@dataclass(frozen=True)` with `warmpool`, `members: list[Member]`, and `error: Exception | None` (OPEN-O).
- `exceptions.py`:
  - `BatchExistsError(BatchError)`.
  - `QuorumUnreachableError(BatchError)`, with `.warmpool`, `.size`, `.min_ready`, `.failed`, and `.lost`. `failed` counts terminal members, create failures included.
- `constants.py`:
  - `BATCH_WORK_BUDGET_ANNOTATION`, `BATCH_QUORUM_TIMEOUT_ANNOTATION` (C).
  - `WARMPOOL_PLURAL_NAME = "sandboxwarmpools"`, `TEMPLATE_PLURAL_NAME = "sandboxtemplates"`, both in `CLAIM_API_GROUP`/`CLAIM_API_VERSION`. These are for the precheck.
- `batch_utils.py`, with the tuning constants next to PR 1's:

  | Constant | Value |
  | :-- | :-- |
  | `BATCH_DEFAULT_CREATE_RPS` | 50.0 |
  | `BATCH_DEFAULT_MAX_IN_FLIGHT` | 20 |
  | `BATCH_DEFAULT_QUORUM_TIMEOUT_SECONDS` | 600 |
  | `BATCH_DEFAULT_WORK_BUDGET_SECONDS` | 3600 |
  | `BATCH_SHUTDOWN_MARGIN_SECONDS` | 600 |
  | `BATCH_CREATE_ATTEMPTS` | 3 |
  | `BATCH_REQUEST_TIMEOUT_SECONDS` | 30 |
  | `BATCH_COLLECTION_REQUEST_TIMEOUT_SECONDS` | 90 |
  | `BATCH_RELEASE_MAX_IDLE_ROUNDS` | 3 |

## 4. Internal design

### 4.1 `batch_utils.py` (pure functions, one owner for each policy)

| Function | Does | Serves |
| :-- | :-- | :-- |
| `generate_batch_id()` | `"b"` plus 11 characters from `secrets.choice` over `[a-z0-9]`. | R3 |
| `validate_claim_batch_args(groups, labels, batch_id, create_rps, max_in_flight, work_budget, quorum_timeout, lease_duration) -> ClaimBatchArgs` | Every R2 rule, plus the defaults filled in. Returns a small `NamedTuple` of resolved values, so both shells take the same values. | R2, R8, R24 |
| `create_error_outcome(status, attempt) -> "success" \| "retry" \| "fail"` | The R9 table: 409 on attempt 1 fails and on a later attempt succeeds; a retryable status (via `is_retryable_status`) retries while attempts remain; anything else fails. | R9 |
| `retry_delay(attempt, error) -> float` | The error's integer `Retry-After` header when there is one (capped at `BATCH_WATCH_BACKOFF_MAX_SECONDS`), else `backoff_delay(attempt, …)`. | R9, R17 |
| `parse_quorum_timeout_annotation(value) -> int` | Missing falls back to the default; not a positive `int` raises `BatchError`. | R16 |

`is_retryable_status` and `backoff_delay` from PR 1 are reused as they are. The backoff constants are PR 1's `BATCH_WATCH_BACKOFF_*`; see PR 1 change 1 in §8.

### 4.2 `batch_state.py`: fill accounting added to `BatchState`

The quorum and settle rules are the subtle part. If each shell had its own copy they would drift, so they live in the core, and the shells only feed in transitions and pop results. Every method is O(1) per call, except `set_mode` (O(G) or O(ready), once per handle) and `expire_fill` (O(G), once).

**New fields**, each with the requirement it serves:

| Field | Purpose | Req |
| :-- | :-- | :-- |
| `_group_by_pool: dict[str, BatchGroup]` | Looks up `min_ready`/`size` per pool, and whether a claim's pool is part of the fill. | R12 |
| `_mode: str \| None` | `"stream"` or `"quorum"` once fixed. | R13 |
| `_waiting: dict[str, dict[str, None]]` | Per pool, the Ready fill members not yet handed out, in Ready order. This is an ordered set; for seeded members, insertion order is ordinal order. | R11–R13, R16 |
| `_dispatched: set[str]` | Claim names already handed out, for at-most-once. | R11 |
| `_unable: set[str]` | Fill claims that can no longer arrive. This is a latch: a member counts once, and never un-counts. | R11, R12, R15 |
| `_failed: dict[str, int]`, `_lost: dict[str, int]` | Per-pool unable counts, for the verdict and its error's counts. | R12 |
| `_skipped: int` | Creates skipped because their group failed; they count toward settle only (see `cancel_create`). | R14, R11 |
| `_ready_count: int` | Fill members currently Ready and not unable, batch-wide, for settle. | R11 |
| `_verdicts: dict[str, GroupReady]` | Set once per pool. | R12, R14 |
| `_events: deque[BatchEvent]` | Events waiting for `events()`. | R11 |
| `_group_results: deque[GroupReady]` | Results waiting for `iter_ready_groups()`. | R12 |
| `_fill_expired: bool` | Set once the deadline has passed. | R15 |

A member is part of the fill when `ordinal < self.size` and its pool is one of the batch's groups (OPEN-H).

**Transition hook.** `upsert_claim` and `mark_lost` (PR 1) now call `_on_fill_change(name, member)` for fill members. It does four things:
1. If the member became terminal or lost, it goes into `_unable` (the latch). The pool's counter is bumped, the name is dropped from `_waiting`, and `MEMBER_FAILED` or `MEMBER_LOST` is queued. This happens even in a failed group (R14).
2. Otherwise, if the member is Ready and not in `_dispatched`, it is routed:
   - **stream mode:** queue `MEMBER_READY` and dispatch it;
   - **quorum mode** with a successful verdict for its pool: the same;
   - **quorum mode** with a failed verdict: nothing;
   - **no verdict yet, or no mode:** add it to `_waiting[pool]`.
3. If the member is no longer Ready, it is dropped from `_waiting`. A member that was already handed out stays handed out.
4. In quorum mode, it re-evaluates that one pool's verdict.

`_ready_count` is kept up to date along the way.

**Verdict for one pool**, only in quorum mode and only while its verdict is unset:
- **Success:** when `len(_waiting[pool]) >= min_ready`, which includes `min_ready == 0`. The first `min_ready` members become `GroupReady.members` and are dispatched; the rest of `_waiting[pool]` is queued as `MEMBER_READY` and dispatched. Later Ready members of that pool then stream directly (step 2).
- **Unreachable:** when `size - failed - lost < min_ready`. The result is `GroupReady(error=QuorumUnreachableError(...))`, and `_waiting[pool]` is cleared.
- The result is appended to `_group_results`.

**Methods added:**
- `set_mode(mode)`:
  - A conflicting mode (`events()` first, then `iter_ready_groups()`) raises `BatchError`. Asking for the mode already set is a no-op, and so is `events()` in quorum mode.
  - The first `"stream"` flushes all of `_waiting` to `_events`, in Ready order within a pool.
  - The first `"quorum"` evaluates every pool's verdict.
- `record_create_failure(claim_name, warmpool, message)`: adds a synthetic member (`terminal=True`, `reason="CreateFailed"`), then `_on_fill_change`. If a timed-out create had in fact landed, the watch later replaces the synthetic member in `members()`, but the latch keeps it counted as unable. That keeps the verdict and the settle point monotonic.
- `cancel_create(warmpool)`: a create skipped because its group failed (R14). It increments `_skipped`, with no event and no member.
- `group_failed(warmpool) -> bool`: quorum mode with an error verdict. The creator reads it (R14).
- `expire_fill()`:
  - Sets `_fill_expired`.
  - Gives every pool without a verdict `GroupReady(error=TimeoutError("Group quorum timed out"))`, in quorum mode, and clears its `_waiting`.
  - Doesn't touch members. A still-pending member simply stops mattering (OPEN-X).
- `count_missing_fill_as_unable()`: for a re-attached handle, `size - seen fill members` per pool becomes unable. The previous driver has stopped, so a claim that doesn't exist now never will (R16).
- `note_lease_degraded()`: queues `LEASE_DEGRADED`.
- `pop_event()`, `pop_group_result()`.
- `fill_settled() -> bool`: `_fill_expired`, or `_ready_count + len(_unable) + _skipped == size`.
- `events_done() -> bool`: `fill_settled()` and `_events` is empty. In quorum mode, a settled fill means every verdict is already decided.
- `groups_done() -> bool`: every pool has a verdict and `_group_results` is empty.
- `seed_from_claims` now inserts in ordinal order, so already-Ready members are handed out in ordinal order (OPEN-G).

### 4.3 Handles: the state each shell holds (added to PR 1's)

| Field | Purpose | Req |
| :-- | :-- | :-- |
| `_changed` | `threading.Condition(self._lock)` / `asyncio.Condition(self._lock)`, notified on every state change. | R11, R12 |
| `_fill_deadline: float \| None` | Monotonic time. `None` until the last create's pacing slot is taken; set at attach for a re-attached handle. | R15 |
| `_quorum_timeout: int` | For the deadline. | R15 |
| `_creators` | Sync: a list of worker threads; async: a set of worker tasks. Empty on a re-attached handle. | R8 |
| `_stop_creating` | `threading.Event` / `asyncio.Event`. | R17, R18 |
| `_create_plan: deque[tuple[str, BatchGroup]]`, `_next_create_at: float` | Claim names still to create, and the next pacing slot. | R7, R8 |
| `_create_spec` | Labels, annotations per pool, `work_budget`, `quorum_timeout`, `create_rps`: the resolved arguments. | R7 |
| `_released: bool` | Completion flag for `release()`. It is set after the Lease delete succeeds, and next to PR 1's `_detached`/`_lease_released` it tells release from detach. | R17 |

### 4.4 Creation (shells)

Creation uses `min(max_in_flight, N)` worker threads (sync) or tasks (async), all running the same loop. There's no separate producer and no pacer class; `max_in_flight` is simply the number of workers. Each worker:

1. Under the lock:
   - pop the next name from `_create_plan`, or exit when it's empty;
   - take a pacing slot: `slot = max(now, _next_create_at)`, then `_next_create_at = slot + 1/create_rps`;
   - if the plan is now empty, set `_fill_deadline = slot + quorum_timeout` (R15).
2. Sleep until `slot`, interruptibly (`_stop_creating.wait(...)`, or `asyncio.wait_for` on the event). If stopped, exit.
3. Under the lock, call `_expire_if_due()`, then `state.group_failed(pool)`. If the group has failed, `cancel_create` and go to step 1.
4. Create with retry:
   - `create_sandbox_claim(..., lifecycle=construct_sandbox_claim_lifecycle_spec(quorum_timeout + work_budget + margin), _request_timeout=BATCH_REQUEST_TIMEOUT_SECONDS)`;
   - on an error, `create_error_outcome(...)` decides; retries wait `retry_delay(...)`, interruptibly;
   - `"fail"` → `state.record_create_failure(...)` and notify;
   - a success does nothing here: the watch delivers the claim.
   - The transient exceptions are the same sets PR 1's watch loops treat as transient: sync `urllib3` `ProtocolError`/`ReadTimeoutError`/`ConnectionError`; async `aiohttp.ClientError` minus SSL, plus `asyncio.TimeoutError`.
5. Log each claim at DEBUG.

Retries don't take a new pacing slot. `Retry-After` and the backoff bound them.

**Stopping** (`release`/`detach`/client `close`): set `_stop_creating`, then wait up to `BATCH_REQUEST_TIMEOUT_SECONDS + 1` for the workers. Sync joins the threads; async uses `asyncio.wait`, then cancels any stragglers. Waiting means every create that was sent has been answered before `deletecollection` runs. Only a create whose request timed out could still land later, and its `shutdownTime` bounds it (see §10 Q4).

### 4.5 Consumers (shells)

There is one wait helper per shell, `_next(pop, done)`. It holds the condition and loops:
- if the handle is detached or released, raise `BatchError`;
- `_expire_if_due()`;
- `item = pop()`: if there is one, return it;
- if `done()` or `state.error()` is set, return `None`;
- otherwise wait on `_changed` with `timeout = _fill_deadline - now`, or no timeout.

`events()` and `iter_ready_groups()` each call `set_mode` under the lock, then return a generator that loops `_next(...)` and yields outside the lock.

`_expire_if_due()` calls `state.expire_fill()` once `now >= _fill_deadline`, then notifies. The consumers and the creators both call it, so group timeouts also stop creates when nobody is iterating.

Notifiers call `_changed.notify_all()`. They are:
- the watch loop, after each event it applies, and after a re-list;
- the create workers, on a failure;
- renewal, on entering degraded, which also calls `note_lease_degraded`;
- `_note_error` paths;
- `release`/`detach`.

### 4.6 Lease, release, detach, and client cleanup

**Lease creation** (`claim_batch`): `create_batch_lease(namespace, body, _request_timeout)` with:
- `acquireTime = renewTime = now`, `leaseTransitions = 0`, `holderIdentity = generate_holder_identity()`;
- `leaseDurationSeconds = lease_duration`;
- the annotations `{lease-duration, work-budget, quorum-timeout}` (R5).

Renewal and takeover are PR 1's, unchanged.

**`release()` sequence**, in both shells:
1. Under the lock: return if `_released`; raise `BatchError` if `_lease_released` (the handle detached); set `_detached = True` so `connect` and the consumers stop.
2. Stop creators (§4.4), stop the watch and renewal (PR 1's `_stop_background_tasks`, or the stop events plus the renew-thread join), close cached connections, and notify.
3. `_delete_batch_objects(helper, namespace, batch_id, lease_name)`.
4. Set `_lease_released = _released = True`, and unregister from the client.

**`_delete_batch_objects`** is a module-level function in each batch module. The sync one is also what the async client's atexit hook calls, with a sync `K8sHelper`, since an atexit hook can't use the event loop. Its loop:

```
idle = 0; last_error = None; remaining_before = None
loop:
    try: delete_sandbox_claims_by_label(ns, selector, timeout=COLLECTION)   # deletecollection
    except 403 → raise; retryable → last_error = e; other ApiException → raise
    try: live = [c for c in list(ns, selector, timeout=COLLECTION) if no deletionTimestamp]
    except retryable → last_error = e; live = None
    if live == []:
        try: delete_batch_lease(name, ns, timeout=REQUEST) (404 ok); return
        except 403 → raise; retryable → last_error = e
    progressed = live is not None and (remaining_before is None or len(live) < remaining_before)
    idle = 0 if progressed else idle + 1
    if idle >= BATCH_RELEASE_MAX_IDLE_ROUNDS: raise last_error or BatchError(f"{len(live)} claims are still not deleting")
    remaining_before = live's length if known
    sleep retry_delay(idle or 1, last_error)
```

Why rounds end on "no progress" instead of a fixed count: `deletecollection` deletes serially on the apiserver (one worker by default). A large batch can outlast the 60 s server timeout while deletion carries on. Each such round still shrinks the live count, so it doesn't use up the budget. The loop only gives up after 3 rounds in a row that delete nothing. The client-side timeout of 90 s sits above the server's 60 s, so the server's 504 (retryable) normally ends a long request first.

The list is a consistent read (no `resourceVersion`), so a lagging watch cache can't report "no claims left" too early. The Lease is deleted only once the list shows none, so a failed `release()` leaves a Lease that goes stale for the reaper (R17).

**`detach()`** (PR 1) now:
- stops creators first (step 2 above);
- raises `BatchError` if the handle was released;
- passes `_request_timeout=BATCH_REQUEST_TIMEOUT_SECONDS` to its Lease read and replace (R19).

**Client tracking** (R20; restores the sync registry with parity, per CN "Deferred from PR 1"):
- **Both clients:** `_active_batches: dict[(namespace, batch_id), handle]`. `claim_batch` and `get_batch` register the handle; `release()`/`detach()` unregister it.
- **`delete_all()`, both clients:** after the sandboxes, `release()` every tracked handle, logging failures as the existing loop does.
- **Automatic cleanup:**
  - Sync `_delete_automatic_sandboxes` (the atexit hook when `cleanup=True`) and async `_delete_automatic_sandboxes` (called by `__aexit__`) release every tracked handle.
  - Async `_atexit_cleanup` (`cleanup=True`, the async default) runs `sandbox_batch._delete_batch_objects(K8sHelper(), ...)` for each tracked batch. Failures go to stderr, as that hook's existing claim deletes do.
  - Async `close()` still only stops the loops of handles that are left (PR 1), and now its creators too.
- **Every tracked handle is released:** both batches this client claimed and re-attached ones. A handle that exits without `detach()` would otherwise leave a Lease that goes stale and can't be re-attached, which is the same state as a crash. A caller who wants a handoff calls `detach()` first, which unregisters the handle. See Q2.

### 4.7 What goes to the cluster, per `claim_batch`

- 2 GETs per distinct pool (the warm pool, then its template; templates deduplicated).
- 1 Lease create.
- 1 list.
- 1 watch.
- N creates (plus retries).
- Lease renewals every `max(1, lease_duration // 3)` s.
- `release()`: at least 1 `deletecollection`, at least 1 list, and 1 Lease delete.

### 4.8 Helpers (`k8s_helper.py`, `async_k8s_helper.py`)

- **Changed:** `create_sandbox_claim` and `list_sandbox_claim_objects` gain `_request_timeout=None`, passed through. Both are keyword arguments with defaults, so existing callers are unaffected.
- **New**, all taking `_request_timeout`:
  - `create_batch_lease(namespace, body)`;
  - `delete_batch_lease(name, namespace)`, where 404 is ignored;
  - `delete_sandbox_claims_by_label(namespace, label_selector)`, which calls `delete_collection_namespaced_custom_object`;
  - `get_sandbox_warmpool(name, namespace)` and `get_sandbox_template(name, namespace)`, each returning a dict, or `None` on a 404.

## 5. Deliberately left out

- **A pacer or rate-limiter class, a create producer, or an executor.** `max_in_flight` workers sharing one slot variable already give both bounds.
- **An `_initial_fill` set.** The test `ordinal < size` plus a known pool is O(1) and needs no state.
- **Ready-order timestamps.** Insertion order in `_waiting` gives Ready order, and seeding in ordinal order covers re-attach.
- **Per-member timers.** One deadline for the whole fill (OPEN-X).
- **A "second call raises" rule for the consumers.** They just drain shared queues, so calling one again continues; only a mode conflict raises. See Q3.
- **Reading `batch-work-budget` in `get_batch`.** Its only reader is PR 4's replacements (R23). PR 2 still writes it, because the wire contract (C, OPEN-E) says `claim_batch` does, and batches created by PR 2 should carry it.
- **Round-robin creation across groups.** Sequential ordinal order gets early groups to quorum sooner, and total fill time is the same.
- **A per-group deadline.** Declined earlier (CN "Ideas raised").
- **Tracing spans around `claim_batch`.** Only the trace-context annotation the contract asks for.
- **`wait_for_quorum`, `acquire`/`replace`/`release_member`/`release_not_ready`, `size=0` groups, `TerminalMemberError`, `pool_size`** (PRs 3–5).
- **Deleting a failed group's claims** (R14: nothing is deleted).

## 6. Size estimate

| File | Source lines | Test lines |
| :-- | --: | --: |
| `models.py` / `constants.py` / `exceptions.py` / `__init__.py` | 30 / 8 / 25 / 10 | — |
| `batch_utils.py` | 90 | 100 (`test_batch_utils.py`) |
| `batch_state.py` | 170 | 260 (`test_batch_state.py`) |
| `k8s_helper.py` / `async_k8s_helper.py` | 85 / 90 | 70 / 70 |
| `sandbox_batch.py` / `async_sandbox_batch.py` | 270 / 270 | 430 / 430 |
| `sandbox_client.py` / `async_sandbox_client.py` | 75 / 80 | 50 / 60 |
| `README.md` | 70 | — |
| e2e (`test_batch_e2e.py`) | — | 60 |
| **Total** | **~1,270** | **~1,530** |

About 45% of the source lines are docstrings and README. The handle estimates include PR 2's edits to `detach` and the watch and renewal notify points.

## 7. Test plan

Each contract has one owning test at the strongest boundary; the handle tests use one representative value for anything a `batch_utils`/`batch_state` table owns. "Both" means sync and async.

**`test_batch_utils.py`**
1. `validate_claim_batch_args` rejects each invalid input (subtests):
   - an empty `groups`, `size` 0, a duplicate pool;
   - `create_rps` of 0, -1, `True`, `"5"`;
   - `max_in_flight` of 0, 1.5, `True`;
   - `work_budget`/`quorum_timeout` of 0, 1.5;
   - `lease_duration` of 5;
   - `labels` carrying `BATCH_ID_LABEL`, or an invalid label;
   - an invalid `batch_id`.

   It also fills in the defaults when every argument is `None`.
2. `generate_batch_id` output passes `validate_batch_id`, and two calls differ.
3. The `create_error_outcome` table: 409 on attempts 1 and 2; 429/503/`None` with attempts left and at the last attempt; 400/403/404/422.
4. `retry_delay`: an integer `Retry-After` wins and is capped; a missing or HTTP-date header falls back to the backoff range.
5. `parse_quorum_timeout_annotation`: missing → 600; `"90"` → 90; `"abc"`, `"0"`, `"-1"` → `BatchError`.

**`test_batch_state.py`** (core semantics, table-driven where possible)
6. Stream mode: each fill member's `MEMBER_READY` appears once, including across a Ready→NotReady→Ready flap; a non-fill claim (`ordinal >= size`) emits nothing.
7. Status events: terminal → `MEMBER_FAILED`; lost → `MEMBER_LOST`; a create failure → `MEMBER_FAILED` with reason `CreateFailed`; terminal then deleted → only `MEMBER_FAILED` (the latch).
8. Ready members seen before any mode are delivered when `events()` fixes stream mode.
9. Quorum success: the group yields its first `min_ready` members in Ready order; extras that were Ready earlier are queued at the yield; later ones stream right away; nothing is queued for the group before the yield.
10. Unreachable: the verdict comes at exactly `size - failed - lost < min_ready`, with the counts on the error; the group gets no `MEMBER_READY` afterwards, even when a member becomes Ready; `MEMBER_FAILED`/`MEMBER_LOST` still queue; `group_failed` is true; its members stay in `members()`.
11. `expire_fill`: groups without a verdict get `TimeoutError`, decided groups are unchanged, and `fill_settled()` becomes true.
12. Settle: `events_done()` becomes true exactly when the last pending member resolves, with `cancel_create` counting.
13. Mode: `events()` first, then quorum → `BatchError`; quorum first, then stream → allowed; a group with `min_ready == 0` yields `[]` as soon as quorum mode is set.
14. Re-attach: seeding in ordinal order (claims listed as `…-10` before `…-2`) hands out the lower ordinal first, and `count_missing_fill_as_unable` makes a group with missing claims settle or become unreachable.

**Handles** (`test_sandbox_batch.py` / `test_async_sandbox_batch.py`, both)
15. `claim_batch` order and Lease body: the precheck, then the Lease create (holder, duration, three annotations, labels), then the list, then the creates. There is no create before the watch starts.
16. Claim manifest: names `<id>-0..N-1` in group order, labels, group annotations, a `shutdownTime` near `now + quorum_timeout + work_budget + 600`, and `shutdownPolicy: Delete`.
17. Precheck: a missing warm pool or template raises its SDK exception, and no Lease is created (subtests).
18. Existing batch: a Lease 409 raises `BatchExistsError` with no creates; a non-empty list raises `BatchExistsError` and deletes the new Lease.
19. `max_in_flight` bounds concurrent creates (the mock create blocks on a barrier), and pacing slots are `1/create_rps` apart (the module clock is patched).
20. Create retry wiring: 503 then success gives one claim and no failure; a 403 gives `MEMBER_FAILED`/`CreateFailed` from `events()`.
21. Failed group, no more creates: in quorum mode, group A (with `min_ready == size`) gets a 403 on its first create, and A's remaining names are never created while group B's all are.
22. `events()` through the shell: watch events produce `MEMBER_READY`s, and the iterator ends on settle.
23. `iter_ready_groups()` through the shell: it yields per group as watch events arrive, and a group with no Ready member yields `TimeoutError` at the deadline (patched clock, deadline counted from the last slot).
24. Ending: both iterators end once `err()` is set, and raise `BatchError` when `release()` runs while a consumer is blocked.
25. `LEASE_DEGRADED` once per episode, from a failing renewal.
26. Release order: creators stop before `deletecollection` (no create after it); list filtering skips claims with a `deletionTimestamp`; the Lease is deleted last. It is idempotent; release after detach raises; `connect` after release raises.
27. Release errors (subtests):
    - a 503 in round 1 is followed by success;
    - a 403 raises at once and keeps the Lease;
    - 3 rounds without progress raise the last error, or `BatchError`, and keep the Lease;
    - a round that makes progress resets the idle count.
28. Request timeouts: creates, the release requests, and `detach`'s Lease read and write all pass `_request_timeout`.
29. `detach()` while creating: no creates after it, and no deletes.
30. `get_batch` with `batch-quorum-timeout: "abc"` → `BatchError`, with no Lease write. A valid value sets the deadline from attach (one representative).

**Clients** (both)
31. Cleanup reaches every tracked handle: `claim_batch` and `get_batch` register; `delete_all()` and the automatic-cleanup path release each one; `release`/`detach` unregister. Async only: `_atexit_cleanup` calls `_delete_batch_objects` per tracked batch, and `close()` stops creator tasks.

**Helpers** (both)
32. One table test per helper file: each new method sends the right group, plural, selector, and `_request_timeout`; `get_*` and `delete_batch_lease` map a 404 to `None`/no-op.

**E2E** (`test_batch_e2e.py`)
33. `claim_batch([BatchGroup(warmpool=pool, size=2)])` → `iter_ready_groups()` yields one group with 2 members → `connect` one and run `echo` → `events()` ends without `MEMBER_READY` → `release()` → no claims with the label remain and the Lease is gone.

Tests left out on purpose:
- per-value validation cases at the handle level;
- a pacing test per shell beyond #19;
- `members()`/`connect` tests (PR 1's stand);
- ordinal parsing, which PR 1 owns.

## 8. PR 1 changes needed

1. **Optional:** rename `BATCH_WATCH_BACKOFF_BASE_SECONDS`/`BATCH_WATCH_BACKOFF_MAX_SECONDS` to `BATCH_BACKOFF_BASE_SECONDS`/`BATCH_BACKOFF_MAX_SECONDS`, since the create retry and release rounds share them in PR 2. Without the rename, PR 2 uses the watch-named constants for creates, which reads oddly. It's a four-line change in PR 1. If PR 1 shouldn't move, PR 2 reuses the names as they are.

Nothing else. PR 2 makes its edits to PR 1 code in its own diff:
- `detach` timeouts and stopping creators;
- the notify points;
- ordinal-ordered seeding;
- `_request_timeout` on two helpers;
- the sync registry.

## 9. Decision changes for Brian

**D1. Hold-back becomes "until the group has yielded"** (changes OPEN-F's second half). OPEN-F holds back a group's *first* `min_ready` Ready members and streams *later* Ready members immediately, even before the group yields. Combined with the approved "a failed group is finished", that lets a group hand out extras and then fail, which is the "failed cohort turns into loose sandboxes" outcome that decision was meant to stop.

The proposed rule: in quorum mode, a group's `MEMBER_READY`s start only once it has yielded successfully. At that point the extras that were already Ready go out, and later ones stream right away. A failed group hands out nothing, ever.

What it saves:
- the running "first `min_ready`" count at event time, and its corner cases (a held member lost before the yield, with an extra already out);
- a stronger guarantee to document.

What it costs: an extra can reach `events()` later than it would under OPEN-F, since it waits for its group's yield. In quorum mode that's the point of choosing quorum.

The alternative, OPEN-F as written, is also implementable in `_on_fill_change`: add a per-pool held count instead of the verdict check, about 10 lines more.

## 10. Open questions

- **Q1. Per-claim INFO logs.** `create_sandbox_claim` logs one INFO line per claim ("Creating SandboxClaim …"), which a 20k batch turns into 20k lines. The ground rule wants per-claim lines at DEBUG. Options:
  - (a) leave it, since it's shipped single-claim behavior;
  - (b) add a keyword-only `log_level` to the helper;
  - (c) move the log line into the single-sandbox callers.

  I'd pick (a) in PR 2, and raise it with reviewers if they object.
- **Q2. What exit cleanup does to re-attached handles.** This design releases every tracked handle, claimed or re-attached (§4.6). The alternative only releases batches this client claimed and stops the loops of re-attached ones, which leaves their Lease to go stale. I'd keep "release all": a driver exiting without `detach()` has signaled no handoff.
- **Q3. Calling a consumer again.** This design lets `events()`/`iter_ready_groups()` be called again, continuing from where they left off. PR 3's plan has `wait_for_quorum` raise on a second call. Should the iterators raise too, for symmetry? That would be one flag each.
- **Q4. A create that lands after `release()`.** A create that timed out on the client can still be applied by the apiserver after `release()`'s rounds. Release waits for every in-flight create to be answered first, so only a timed-out create is at risk. That claim is left until its `shutdownTime` (or the reaper). Acceptable?
- **Q5. Iterators end on `err()`.** This follows the proposal's examples, which check `batch.err()` after the loop. With it, a lease-expired handle stops streaming even though its claims still exist. Brian to confirm.
- **Q6. Fill deadline from the actual last slot, not the planned formula.** It equals `start + (N-1)/create_rps` when creation keeps pace, and is later when `max_in_flight` or slow responses hold creation back, so groups never time out before their claims exist. I'm treating this as a reading of the approved text ("from when pacing issues the batch's last create"), not a change.
- **Q7. Default connection pool.** On small machines (`cpu_count * 5 < max_in_flight + 2`), urllib3 opens extra connections and logs "Connection pool is full" rather than failing. PR 5 adds validation. Should the README mention it in PR 2?
