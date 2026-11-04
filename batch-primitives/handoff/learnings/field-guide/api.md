<a id="sec-api"></a>

[Learnings home](../../learnings.md) · [Field guide index](../../learnings.md#sec-field-guide)

# API design: making the right thing easy and the wrong thing hard

This section is about the public surface of the batch SDK: the types, method signatures, errors, and documented promises that callers see. The one big idea is that a good API makes misuse impossible or loud, and makes the correct path the default. That means invalid states can't be represented, bad input fails before anything happens, failures carry their root cause, and every method says clearly what it promises when it fails. Read the first entries (types and validation) as general rules for any SDK. The later entries are specific choices for consumers, release methods, and labels that show those rules applied.

<a id="api-1"></a>

## API-1 One enum for lifecycle state, not several booleans

**Takeaway:** Model an object's lifecycle as one enum so impossible combinations can't exist. When a terminal cause and a later event collide, keep the more informative terminal state.

<details>
<summary>The full story (4/N)</summary>

- **Situation:** A `Member` (one claim in the batch, as the SDK sees it) originally carried three booleans: `ready`, `terminal`, and `lost`.
- **What goes wrong:** Callers had to combine three flags. Contradictory combinations, such as ready and lost at once, were representable. A member that failed and whose claim was then deleted became "lost", so its state no longer said why it could never become Ready.
- **What we do:** `models.MemberState` is one enum (an `enum` type with a fixed set of named values) with four states: `PENDING`, `READY`, `FAILED`, `LOST`. `models.Member.state` holds it. `batch_state.derive_member` and `BatchState.mark_lost` compute it. LOST means the claim was deleted before it failed, and it clears `reason` and `message`, because they described the deleted claim's last Ready condition. A member that is already FAILED stays FAILED, with its reason, when its claim is deleted or is missing from a re-list. `connect()` simply requires `READY` (4/N, `4bb0db30`).
- **Example:**
  ```text
  t0  Ready=False reason=ClaimExpired  -> FAILED(ClaimExpired)
  t1  watch sees DELETED              -> still FAILED(ClaimExpired), not LOST
  ```
- **Related:** [API-2](#api-2), [PAT-23](../patterns.md#pat-23).

</details>

<a id="api-2"></a>

## API-2 Hand out immutable snapshots, and treat them as keys later

**Takeaway:** Objects you return from shared, concurrently updated state should be deeply immutable values, so you can hand out references without copying. When a caller passes a snapshot back, use it only as an identifier and re-read the mutable fields from the live source.

<details>
<summary>The full story (1/N, 4/N, 6/N, 13/N, 17/N)</summary>

- **Situation:** `members()`, `groups()`, `events()`, `group_outcomes()`, and `wait_for_quorum()` hand objects to caller threads or tasks, while the watch thread (a background loop that streams changes from the API server) keeps updating the cache. Callers then pass a `Member` back into `connect()`.
- **What goes wrong:** If a caller mutated a returned `BatchGroup` or a `Member`'s `pod_ips` list, it would corrupt later snapshots and the shared state. Callers could also see torn updates from the watch thread. And a snapshot can be minutes old: if `connect()` used its `sandbox_name` or `pod_ips`, it would route to a sandbox or pod IP that the watch already knows changed.
- **What we do:** In `models.py`, `BatchGroup`, `Member`, and `BatchEvent` use pydantic's `ConfigDict(frozen=True)` (pydantic is the data-model library; frozen blocks attribute reassignment), and `Member.pod_ips` is a `tuple`, so a `Member` is fully immutable and hashable. `MemberState` is a `StrEnum`. `GroupOutcome` is a `@dataclass(frozen=True)`, not a pydantic model, because its `error` field holds a live Exception with a traceback and cause. A before-validator fills `min_ready = size` when omitted, and an after-validator checks `0 <= min_ready <= size`. The watch never edits a member in place: it replaces the entry with a new object from `derive_member` or `model_copy` (see `BatchState.mark_lost`). `connect()` (`sandbox_batch.py: SandboxBatch.connect`, `async_sandbox_batch.py: AsyncSandboxBatch.connect`) uses the passed `Member` only for its `claim_name`. Under the lock it reads `current = self._state.get_member(...)` and builds the Sandbox from `current.sandbox_name` and `select_pod_ip(current.pod_ips)`. The test `TestSnapshots.test_members_pod_ips_cannot_be_mutated` pins immutability.
- **Example:**
  ```text
  t0  m = batch.members()[0]      # pod_ip 10.0.0.1
  t1  watch: pod moved to 10.0.0.2
  t2  batch.connect(m)            # seeds 10.0.0.2, not m's 10.0.0.1
  ```
- **Related:** [API-1](#api-1), [PAT-8](../patterns.md#pat-8). [WATCH-7](watch.md#watch-7) covers how `connect()` re-validates queued snapshots, and [WATCH-8](watch.md#watch-8) covers when a cached connection stays valid.

</details>

<a id="api-3"></a>

## API-3 Validate and build every caller payload before the first side effect

**Takeaway:** Check and build all caller input before any I/O, before consuming an identifier, and before reserving capacity. Then bad input fails as a plain argument error with nothing to roll back.

<details>
<summary>The full story (23/N)</summary>

- **Situation:** 23/N added a keyword-only `pod_annotations` argument to `acquire()`, so callers such as a GKE PodSnapshot pin can stamp the pod without leaving the batch.
- **What goes wrong:** If the dict were validated lazily, at the POST, a bad value would already have cost a first-use WarmPool and Template GET and an ordinal (the per-batch claim number, which is never reused because a released claim may still be terminating). The error would then surface as `TerminalMemberError(CreateFailed)` or as a rollback delete, not as a clean `ValueError`.
- **What we do:** `SandboxBatch.acquire` and `AsyncSandboxBatch.acquire` call `pod_metadata.build_pod_metadata(None, pod_annotations)` right after the timeout check, before the deadline and before `_check_pool`. `models.AdditionalPodMetadata` types annotations as `dict[str, str]`, so pydantic rejects non-str values with a `ValidationError`, which subclasses `ValueError`. The test `test_pod_annotations_are_sent_on_create_and_retries_and_omitted_when_empty` asserts that `acquire('pool-unseen', pod_annotations={'bad': None})` raises `ValueError` and that no GET reaches `pool-unseen`.
- **Example:**
  ```python
  batch.acquire("pool-unseen", pod_annotations={"bad": None})
  # ValueError; no pool GET, no ordinal used, nothing to delete
  ```
- **Related:** [API-4](#api-4), [API-6](#api-6), [API-27](#api-27), [PERF-10](perf.md#perf-10).

</details>

<a id="api-4"></a>

## API-4 Numeric validators must reject bool, NaN, and infinity

**Takeaway:** In Python, exclude `bool` explicitly (it is a subclass of `int`) and check ranges with a chained comparison like `0 < x < math.inf`, which also rejects NaN. Otherwise a stray value turns into a 1-second timeout, disabled pacing, or an endless wait.

<details>
<summary>The full story (12/N, 14/N, 16/N, 17/N)</summary>

- **Situation:** Knobs such as `create_rps`, `timeout`, `max_in_flight`, and `pool_size` feed `1/create_rps`, semaphores, and deadlines. `acquire(timeout=None)` defaults to `quorum_timeout` and otherwise accepts an int or float.
- **What goes wrong:** `isinstance(True, int)` is true, so `acquire(True)` would mean a 1 s timeout and `pool_size=True` would read as 1. NaN fails every comparison, so `remaining <= 0` never fires. `inf` makes `monotonic() + inf` an unbounded wait and makes `1/create_rps` zero, which turns pacing off.
- **What we do:** For floats, `batch_utils.positive_number` rejects bools first, then requires an int or float with `0 < value < math.inf`. For ints, `_positive_int`, `validate_lease_duration_value`, and `utils.validate_pool_size` use `type(value) is not int`, which rejects `True`. `parse_lease_metadata` re-applies the same `not 0 < x < inf` guard to `create_rps` read back from a Lease annotation ([API-8](#api-8)). All of this runs before any I/O. Tests: `test_rejects_each_invalid_input` ('create_rps True', 'create_rps inf', 'max_in_flight True') and `test_parse_table` ('nan', 'inf').
- **Example:**
  ```python
  positive_number("create_rps", True, 50)          # ValueError
  positive_number("timeout", float("nan"), 600)    # ValueError: 0 < nan is False
  ```
- **Related:** [API-3](#api-3), [API-6](#api-6), [API-7](#api-7), [API-8](#api-8).

</details>

<a id="api-5"></a>

## API-5 Use `re.fullmatch` for whole-string validation

**Takeaway:** Validate whole strings with `fullmatch` (or `\Z`), not `match` with `$`. In Python, `$` also matches before a trailing newline, so malformed input slips through to a later layer that fails less clearly.

<details>
<summary>The full story (12/N)</summary>

- **Situation:** `batch_utils.validate_batch_id` used `re.match` with a `$` anchor.
- **What goes wrong:** `'abc\n'` passed validation. `claim_batch` then failed with an API server error on the Lease name, instead of the `ValueError` it raises for every other invalid id.
- **What we do:** Switch to `_BATCH_ID_RE.fullmatch`, and keep the `isinstance(str)` and length checks (12/N, `522dad4c`). The test `test_rejects_trailing_newline` pins it.
- **Example:**
  ```python
  re.match(r"^[a-z]+$", "abc\n")   # matches
  re.fullmatch(r"[a-z]+", "abc\n")  # None
  ```
- **Related:** [API-3](#api-3), [BP-13](../best-practices.md#bp-13).

</details>

<a id="api-6"></a>

## API-6 Derive validation bounds from the margins other code applies to the same value

**Takeaway:** If other code subtracts or adds a safety margin to a value, reject inputs that the margin would make useless, so no accepted input is dead on arrival. Check safety-critical knobs at the boundary (the argument or CLI flag), before any side effect.

<details>
<summary>The full story (1/N, 18/N)</summary>

- **Situation:** Callers pass `lease_duration` to `claim_batch` and `grace` to `detach()`, and `get_batch` reads the duration back from an annotation (a free-form key/value string on a Kubernetes object). Operators run the reaper (a CronJob that deletes abandoned batch Leases) with `--skew-margin` and often omit `--namespaces` in-cluster. A Lease is a small Kubernetes object that records who holds something and when they last renewed.
- **What goes wrong:** `get_batch` subtracts a 5 s clock-skew margin, so a duration of 5 s or less is stale the moment it is written, and `detach(grace=5)` would make handoff impossible. Floats and bools would serialize oddly into an int32 field. A reaper margin below the SDK's own assumption would break the ordering of the three staleness thresholds. A default namespace that differs from client-go's lookup surprises operators who set `POD_NAMESPACE` through the downward API.
- **What we do:** `batch_utils.validate_lease_duration_value` requires `type(value) is int`, and `_require_duration_exceeds_skew_margin` requires `value > CLOCK_SKEW_MARGIN`. `detach()` validates `grace` first, so an invalid grace leaves the handle running and unwritten (`test_invalid_grace_raises_value_error_and_leaves_handle_running`). A bad stored annotation goes through `parse_lease_duration_annotation` and raises `BatchError`, not `ValueError` (see [API-8](#api-8)). In the reaper, `batch_reaper._skew_margin` is an argparse `type` function that rejects margins below `batch_utils.CLOCK_SKEW_MARGIN` (5 s) at parse time with `SystemExit`. `_default_namespace` follows client-go's in-cluster order: `POD_NAMESPACE`, then the service account namespace file, then `default`. `main` splits `--namespaces` on commas, strips, and drops empties, so `' a, b'` becomes `['a', 'b']` (`TestMain.test_flags_and_exit_code`).
- **Example:**
  ```text
  detach(grace=1.5)   -> ValueError, handle still renewing
  detach(grace=5)     -> ValueError (5 <= 5 s margin)
  --skew-margin 2     -> argparse error, reaper never starts
  ```
- **Related:** [API-4](#api-4), [API-8](#api-8). [LEASE-4](lease.md#lease-4) explains the three staleness thresholds.

</details>

<a id="api-7"></a>

## API-7 Reject configuration knobs that can't take effect

**Takeaway:** If a setting cannot apply to the object the caller provided, raise instead of silently ignoring it. Read capacity from the object actually in use, not from the argument you hoped would apply.

<details>
<summary>The full story (17/N)</summary>

- **Situation:** A connection pool is sized when `ApiClient` builds its REST client, and it can't be resized afterwards. Callers can pass `pool_size`, or inject their own `api_client`.
- **What goes wrong:** Accepting `pool_size` together with an injected `api_client` would do nothing, so the caller would believe the pool fits `max_in_flight + 2` while it doesn't.
- **What we do:** `utils.validate_pool_size` raises `ValueError` when both are given, with a message pointing at `configuration.connection_pool_maxsize` on the injected client. It also rejects `type(pool_size) is not int or pool_size <= 0`. `pool_size` applies only to a client the helper loads itself: `K8sHelper.__init__` at construction (through `Configuration.get_default_copy()` with `connection_pool_maxsize` set), and `AsyncK8sHelper` lazily in `_ensure_initialized`. `api_pool_size` in both helpers reads `connection_pool_maxsize or None`, because urllib3 `LifoQueue(0)` and aiohttp `limit=0` both mean unbounded ([LIB-15](lib.md#lib-15) covers that zero case and [PROC-11](../process.md#proc-11) covers how defaults compose). Tests: `test_rejects_an_invalid_pool_size`, `test_a_zero_limit_reads_as_unbounded`.
- **Example:**
  ```python
  K8sHelper(api_client=my_client, pool_size=30)
  # ValueError: pool_size can't be combined with api_client; set connection_pool_maxsize on the api_client's configuration instead
  ```
- **Related:** [API-4](#api-4). [PERF-16](perf.md#perf-16) and [RETRY-19](retry.md#retry-19) cover pool sizing per batch.

</details>

<a id="api-8"></a>

## API-8 Classify errors by who can fix them

**Takeaway:** Raise `ValueError` for bad caller arguments and a domain error for bad persisted or remote state. Use a default only when a value is missing, never when it is present but invalid. When parsing numbers from external state, use a positive-range check (`lo < x < hi`) so NaN fails too.

<details>
<summary>The full story (12/N, 16/N)</summary>

- **Situation:** `get_batch` reads settings (lease duration, `quorum_timeout`, `work_budget`, next-ordinal, `create_rps`) back from Lease annotations, which anyone with Lease write access could edit.
- **What goes wrong:** Raising `ValueError` for a malformed annotation would look like the caller's mistake and would escape `except BatchError`. Silently defaulting a corrupt next-ordinal could reuse ordinals whose claims may still be terminating. `float('nan')` and `float('inf')` parse without error, and NaN slips past a naive `<= 0` check, so a tampered `create_rps` would poison the `1/create_rps` pacing.
- **What we do:** Caller arguments raise `ValueError` (`batch_utils.validate_claim_batch_args`). Missing annotations fall back to defaults, but present-and-invalid ones raise `BatchError` (`parse_lease_metadata`, `parse_lease_duration_annotation`). `parse_lease_metadata` parses `create_rps` with `float()`, maps an unparsable string to 0, and raises `BatchError` when `not 0 < create_rps < math.inf`, which rejects NaN, inf, and values `<= 0` in one check (`batch_lease_metadata` writes it with `f'{create_rps:g}'`). `_parse_int_annotation` takes a `minimum` (1 for `quorum_timeout` and `work_budget`, 0 for next-ordinal) and maps an unparsable value to `minimum - 1`, so parse failures and range failures share one error path. API and network errors propagate unchanged. The `BatchError` docstring in `exceptions.py` states the argument half of this rule (bad arguments raise `ValueError`, API and network errors are raised unchanged). Tests: `test_parse_table`, `test_invalid_annotations_raise_batch_error`, `test_quorum_timeout_annotation_is_validated_and_counts_from_attach`.
- **Example:**
  ```text
  annotation agents.x-k8s.io/batch-next-ordinal: "-1"  -> BatchError
  float("nan") <= 0                                  -> False (naive check passes)
  0 < nan < inf                                      -> False (rejected, BatchError)
  claim_batch(max_in_flight=0)                       -> ValueError
  ```
- **Related:** [API-4](#api-4), [API-6](#api-6), [API-9](#api-9), [API-24](#api-24), [API-26](#api-26), [LEASE-25](lease.md#lease-25).

</details>

<a id="api-9"></a>

## API-9 Add domain exceptions under both the package base and the closest standard type

**Takeaway:** Put new exceptions under your package's base class and also under the nearest existing or built-in category, so old `except` clauses keep working. Attach structured fields so callers never parse messages.

<details>
<summary>The full story (1/N, 13/N, 15/N, 16/N)</summary>

- **Situation:** Callers already catch `SandboxError`, `SandboxNotFoundError`, or `TimeoutError`.
- **What goes wrong:** A separate new hierarchy would force callers to add new `except` clauses. String-only errors would force them to parse messages to get counts or the failing member.
- **What we do:** In `exceptions.py`, every batch error is a `BatchError`, which is a `SandboxError`. `BatchNotFoundError(BatchError, SandboxNotFoundError)` and `BatchTimeoutError(BatchError, TimeoutError)` use dual inheritance (two base classes). `QuorumUnreachableError` carries `warmpool`, `size`, `min_ready`, `failed`, `lost`, and `released`. `TerminalMemberError` carries the immutable `member`. Inheriting `TimeoutError` has a cost: on Python 3.11+ it also matches `asyncio.TimeoutError`, so internal transport-retry predicates and broad `except Exception` wrappers must exclude `BatchError` first ([LIB-5](lib.md#lib-5)).
- **Example:**
  ```python
  try:
      batch.wait_for_quorum()
  except QuorumUnreachableError as e:
      log(e.warmpool, e.failed, e.lost, e.released)
  ```
- **Related:** [API-8](#api-8), [API-10](#api-10), [API-11](#api-11), [PAT-25](../patterns.md#pat-25).

</details>

<a id="api-10"></a>

## API-10 The first fatal error is sticky and wins

**Takeaway:** Record a component's first fatal error permanently and chain later failures to it. The root cause is the error worth surfacing, and a clearable error lets a broken handle resume dangerous work.

<details>
<summary>The full story (1/N, 6/N, 15/N)</summary>

- **Situation:** The watch, Lease renewal, and creators can each record a fatal error, sometimes in a cascade.
- **What goes wrong:** Overwriting with the latest error hides the root cause, for example a later "stopped" error masking the original 403 or the Lease expiry. If the error could be cleared, a handle that may have lost its Lease could go back to connecting or deleting.
- **What we do:** `BatchState.note_error` stores an error only `if self._error is None`, and nothing ever clears it. `BatchState.error` backs the public `err()`. `_check_healthy` (both shells) raises `BatchError` chained from it (`raise ... from err`) for `connect()` and the `release_*` methods, while `detach()` still works.
- **Example:**
  ```text
  t0  watch gets 403      -> err() = 403
  t1  renewal stops       -> err() still 403
  t2  connect(m)          -> BatchError, __cause__ = 403
  ```
- **Related:** [API-11](#api-11), [API-12](#api-12), [RETRY-4](retry.md#retry-4).

</details>

<a id="api-11"></a>

## API-11 Streams can end quietly, but single-value calls must raise

**Takeaway:** When a background failure stops work, an iterator may simply end and let the caller check a status. A call that returns one value must raise a domain error chained from the root cause. Never return a value that looks like success.

<details>
<summary>The full story (13/N, 14/N)</summary>

- **Situation:** Iterator consumers (`events()`, `group_outcomes()`) and the one-shot barrier `wait_for_quorum()` all wait on the same `_next` loop when the watch or renewal fails.
- **What goes wrong:** Returning `[]` from `wait_for_quorum` on `err()` would look like a successful empty quorum. Raising the raw `err()` would leak arbitrary `ApiException` or transport types into the documented Raises contract.
- **What we do:** `_next` returns `None` when the handle is done or `self._state.error() is not None`, so iterators stop and the caller checks `err()`. `wait_for_quorum` (both shells) turns `None` into `BatchError('stopped while waiting for quorum')` chained `from self._state.error()`. So `except BatchError` catches every failure, and `__cause__` keeps the root error (P5 in `pr4_implementation_spec.md`).
- **Example:**
  ```python
  for ev in batch.events(): ...
  if batch.err(): handle(batch.err())   # stream ended on failure
  batch.wait_for_quorum()               # raises BatchError from err()
  ```
- **Related:** [API-9](#api-9), [API-10](#api-10).

</details>

<a id="api-12"></a>

## API-12 Expose degraded health as a sticky, pollable flag

**Takeaway:** Give callers an early-warning flag that turns on at the first problem and that only a real recovery clears. Log the transition, not every retry.

<details>
<summary>The full story (3/N)</summary>

- **Situation:** Callers want to know that Lease renewal is failing before it becomes fatal.
- **What goes wrong:** With only `err()`, callers learn nothing until the Lease is already lost. A flag that flips back to False after renewal stops would let a poller miss the failure. Logging every failed retry floods the logs.
- **What we do:** `SandboxBatch.lease_degraded()` (and the async twin) is True from the first failed renewal until one succeeds. `_mark_renewal_degraded` sets it and logs once per episode at INFO. `_renew_once` clears it only on success. `_stop_renewal` also sets it, so once renewal stops for any reason it stays True. The proposal's alternative, a `LeaseDegraded` event, was replaced by this polled flag (`reaper_contract.md`). Tests: `test_lease_degraded_from_the_first_failed_renewal_until_one_succeeds`, `test_failed_renewals_retry_on_a_backoff_and_stop_at_expiry` (stays True after expiry).
- **Example:**
  ```text
  t0  renewal fails   -> lease_degraded() True, one INFO log
  t1  renewal fails   -> still True, no log
  t2  renewal ok      -> False
  ```
- **Related:** [API-10](#api-10), [BP-34](../best-practices.md#bp-34). [RETRY-10](retry.md#retry-10) and [BP-1](../best-practices.md#bp-1) cover renewal timing.

</details>

<a id="api-13"></a>

## API-13 Let the first consumer lock the hand-out mode

**Takeaway:** When several consumption APIs make incompatible promises about the same items, let the first call fix the mode. Allow only combinations whose guarantees compose, and name both methods in the conflict error.

<details>
<summary>The full story (13/N, 14/N)</summary>

- **Situation:** Three consumers read the same Ready members. `events()` streams each one. `group_outcomes()` hands out a cohort per group. `wait_for_quorum()` is a batch-wide barrier.
- **What goes wrong:** Under streaming, every Ready member is handed out at once, so a later group or quorum consumer could never guarantee a cohort. Two barrier consumers would compete for the same held members.
- **What we do:** `batch_state.ConsumerMode` has STREAM, GROUP, and QUORUM. `BatchState.set_mode` allows only these pairs (first mode, joining consumer): (STREAM, STREAM), (GROUP, STREAM), (GROUP, GROUP), and (QUORUM, STREAM). `events()` can join any mode as the side channel for extras and status events. `group_outcomes()` can be called again to continue. Every other pair raises `BatchError` naming both methods (`_CONSUMER_METHODS`), except a second `wait_for_quorum()`, which raises `BatchError("wait_for_quorum() can only be called once")`. The `_dispatched` set ensures each member is handed out at most once. Test: `TestGroupMode.test_mode_rules`.
- **Example:**
  ```text
  group_outcomes() then events()  -> events() yields a group's members only after it succeeds
  events() then group_outcomes()  -> BatchError naming both methods
  ```
- **Related:** [API-14](#api-14), [API-15](#api-15), [API-16](#api-16), [API-17](#api-17). [WATCH-6](watch.md#watch-6) covers holding Ready members until a mode is set.

</details>

<a id="api-14"></a>

## API-14 Do validation and state changes outside the generator

**Takeaway:** For an API that returns an iterator but has preconditions or side effects, do the checks in a plain wrapper function that returns the generator. A Python generator (a function with `yield`) runs none of its body until the first `next()`.

<details>
<summary>The full story (13/N, 14/N)</summary>

- **Situation:** `events()` and `group_outcomes()` return iterators.
- **What goes wrong:** If `set_mode` and `_check_active` ran inside the generator body, nothing would happen until the first `next()`. A misuse error would surface far from the call. The mode would not be set when creates or watch events arrive in between, and `test_failed_group_stops_creates_only_in_group_mode` depends on it being set.
- **What we do:** `SandboxBatch.events` and `SandboxBatch.group_outcomes` are plain functions. Under the lock they check that the handle is active and call `set_mode`, then `return self._iterate(...)`, which is the generator. `AsyncSandboxBatch.events` and `group_outcomes` do the same with no await in between, which makes the step atomic in asyncio.
- **Example:**
  ```python
  items = batch.group_outcomes()  # mode is GROUP now
  ...                              # creates already see GROUP mode
  list(items)
  ```
- **Related:** [API-13](#api-13), [API-15](#api-15).

</details>

<a id="api-15"></a>

## API-15 Don't cancel remaining work until the consumer says partial results are useless

**Takeaway:** Skip or cancel remaining work only after a consumer has committed to semantics that make it useless. A failure that is fatal for one consumption mode can be fine for another, so decide from the declared contract, not from a guess about partial failure.

<details>
<summary>The full story (13/N, 14/N)</summary>

- **Situation:** The design says a failed group gets no more creates and no hand-outs. But the batch can't know whether the caller wants per-group, batch-wide, or streaming semantics until a consumer is called.
- **What goes wrong:** Skipping creates for a "doomed" group under STREAM would throw away members a streaming caller still wants, because a streamer takes any Ready member.
- **What we do:** `BatchState.group_failed` returns True only in GROUP mode for a group whose outcome has an error, and in QUORUM mode for every pool once the quorum fails. In STREAM mode or with no mode yet, it returns False, so `_create_worker.can_send` keeps sending creates (13/N, `8ad5a407`). Because the shells call `set_mode` eagerly ([API-14](#api-14)), the mode applies to every create sent after the consumer call. Only creates that were never sent count as skipped ([COUNT-5](count.md#count-5) covers that `sent == 0` gate).
- **Example:**
  ```text
  groups A(3), B(2); a 403 on b-0
  group mode:  only b-3 and b-4 are created
  stream mode: b-1 through b-4 are created
  ```
- **Related:** [API-13](#api-13), [API-14](#api-14).

</details>

<a id="api-16"></a>

## API-16 Back iterators with one shared destructive queue: re-calling resumes, it doesn't replay

**Takeaway:** For at-most-once hand-out, keep one queue per consumer kind in the state and pop from it destructively. Document that calling again resumes where the last call stopped and that concurrent iterators split the items.

<details>
<summary>The full story (13/N)</summary>

- **Situation:** A caller may stop iterating, or the iterator may end once the fill settles, and then a member changes (for example, a handed-out member fails).
- **What goes wrong:** Replaying per iterator would hand members out twice. Ending the stream for good would lose later status changes.
- **What we do:** `BatchState` keeps `_events_to_yield` and `_group_outcomes_to_yield` as deques (double-ended queues), and `pop_event` and `pop_group_outcome` use `popleft`. A later `events()` or `group_outcomes()` call continues where the previous one stopped and yields changes queued after it ended, as the `events()` docstring says. Because every pop removes the item, two iterators running at once split the items between them. Tests: `test_events_end_on_settle_and_a_second_call_continues`, `test_events_end_when_a_ready_member_stops_being_ready_after_the_fill_settles`.
- **Example:**
  ```text
  t0  for ev in batch.events(): ...   # ends at settle
  t1  a handed-out member fails
  t2  batch.events()                   # yields only the new MEMBER change
  ```
- **Related:** [API-13](#api-13), [API-17](#api-17), [PAT-9](../patterns.md#pat-9).

</details>

<a id="api-17"></a>

## API-17 A barrier returns exactly min_ready per group and streams the rest at the same moment

**Takeaway:** A barrier API should return a fixed-size cohort in a deterministic order, and hand any surplus to a streaming side channel in the same step, so nothing is duplicated or delayed.

<details>
<summary>The full story (13/N, 14/N)</summary>

- **Situation:** When the quorum or a group is reached, some groups hold more Ready members than `min_ready`.
- **What goes wrong:** Returning all of them makes the cohort size unpredictable. Holding the extras back until later delays sandboxes the caller could already use.
- **What we do:** In `BatchState._check_quorum` and `_decide_group_outcome`, the cohort is the first `min_ready` members of each group in Ready order, with groups in batch order for the quorum. That order comes from iterating `_waiting`, which was built from the groups list, so a re-attached handle relies on 7/N's group ordering. In the same transition the extras go to `events()` as `MEMBER_READY`. Cohort members are marked dispatched, so a later flap never hands them out again. Tests: `test_reached_only_when_every_group_has_min_ready_then_the_rest_stream_once`, `test_returns_min_ready_per_group_in_group_order_then_events_yield_the_rest`.
- **Related:** [API-13](#api-13), [API-16](#api-16).

</details>

<a id="api-18"></a>

## API-18 Enforce deadlines where they are observed, and document the leniency

**Takeaway:** In a state machine with no timers, apply deadlines lazily at observation points and make the expiry step idempotent (safe to run more than once). The resulting leniency is a contract choice, so write it down.

<details>
<summary>The full story (13/N, 14/N)</summary>

- **Situation:** The Sans-I/O core (state logic with no I/O, locks, or timers) can't fire a timer, so the shells must drive fill expiry.
- **What goes wrong:** A background timer thread or task would add one more owner to manage and race with. Strict expiry would also fail a group that becomes Ready just after the deadline while nobody was waiting.
- **What we do:** `_expire_fill_if_due` (both shells) runs under the lock on every `_next` iteration and in the creators' `can_send`. `BatchState.expire_fill` returns False after its first call, so `notify_all` fires once. A group or quorum reached after the deadline but before any check still succeeds. Reviewers (tomergee) asked about this, and it stayed as documented behavior in the `group_outcomes` and `wait_for_quorum` docstrings, because it favors the caller.
- **Example:**
  ```text
  t0  deadline passes, nobody is waiting
  t1  last member of group A turns Ready
  t2  caller calls group_outcomes()   -> A succeeds
  ```
- **Related:** [API-14](#api-14). [COUNT-12](count.md#count-12) covers how the fill deadline is computed, and [COUNT-13](count.md#count-13) covers how it is enforced in waits.

</details>

<a id="api-19"></a>

## API-19 Make release methods idempotent and retryable

**Takeaway:** A repeat call on an already-released item should resume any unfinished cleanup, not raise and not redo finished work.

<details>
<summary>The full story (15/N)</summary>

- **Situation:** A first `release_member(m)` call's delete failed, and the caller calls it again with the same `Member`.
- **What goes wrong:** Raising `ValueError` ("not a member") once the member has left `members()` would make a failed release impossible to retry. Re-deleting on every call would waste requests.
- **What we do:** `BatchState.release_member` does `if claim_name in self._released: return claim_name in self._pending_deletes`. So a released name triggers a delete only while one is still owed. `BatchState.release_not_ready` returns `list(self._pending_deletes)`, which includes earlier failed `release_member` deletes, so one call sweeps leftovers.
- **Example:**
  ```text
  t0  release_member(m)  -> DELETE fails, name stays pending
  t1  release_member(m)  -> DELETE again
  t2  release_member(m)  -> no request (nothing owed)
  ```
- **Related:** [API-20](#api-20), [API-21](#api-21), [API-22](#api-22), [RETRY-12](retry.md#retry-12).

</details>

<a id="api-20"></a>

## API-20 Validation for a retryable destructive call must accept targets earlier calls emptied

**Takeaway:** When a validity check is derived from state that the operation itself changes, also record that the operation was started. Then a retry after a partial failure still passes validation.

<details>
<summary>The full story (15/N, 16/N)</summary>

- **Situation:** `release_group(pool)` raises `ValueError` for unknown pools. For a pool that is not a declared group (one only `acquire()` used), "known" was derived from current members.
- **What goes wrong:** The first `release_group` call drops the members before its `deletecollection` (a Kubernetes call that deletes every object matching a label selector) runs. If that call failed, for example with a 403, the retry raised `ValueError` because the pool no longer looked known, so the documented retry could not clean up the claims.
- **What we do:** `BatchState.has_pool` is true if the pool has a group, a member, an acquire in flight (`self._acquiring.values()`), or an earlier `release_group` (`_fill_stopped_pools`). Unknown pools still raise `ValueError` (16/N `dc9ea439`, review commit `9ba5a6c6`). Tests: `test_concurrent_first_acquires_check_a_pool_once...` (403 then retry), `TestAcquired.test_has_pool_covers_groups_members_and_acquires_in_flight`. The case came up in review (vicentefb, `replies_1991.md`).
- **Example:**
  ```text
  t0  release_group("p")  -> members dropped, deletecollection 403
  t1  release_group("p")  -> accepted (p is in _fill_stopped_pools), retries delete
  ```
- **Related:** [API-19](#api-19).

</details>

<a id="api-21"></a>

## API-21 Offer an atomic "select and act" call instead of snapshot-then-act

**Takeaway:** When callers would otherwise pick items from a snapshot and then act on them, give them one call that selects and acts under the state lock, so the predicate can't go stale in between.

<details>
<summary>The full story (15/N)</summary>

- **Situation:** Callers want to drop stragglers once they have enough members.
- **What goes wrong:** Looping over `members()` and calling `release_member` on non-Ready ones lets a member turn Ready and be handed out between the snapshot and the release, so the caller deletes a sandbox it was just given. A handed-out member that flickers NotReady would also be deleted. This is a TOCTOU race (time of check to time of use).
- **What we do:** `SandboxBatch.release_not_ready` (and the async twin) calls `BatchState.release_not_ready`, which, in one locked step, picks fill members that are not READY and not in `_dispatched`, releases them, and marks every group's pool fill-stopped. `acquire()` members (no group annotations) are kept. Review kept the "never handed out" wording, because a member with a `MEMBER_READY` already queued is committed to the caller (`replies_1990.md`). Test: `test_release_not_ready_keeps_ready_handed_out_and_non_fill_members_and_ends_the_fill`.
- **Related:** [API-19](#api-19), [API-22](#api-22), [LOCK-1](lock.md#lock-1).

</details>

<a id="api-22"></a>

## API-22 Best-effort bulk cleanup tries every item, then raises one error

**Takeaway:** For bulk cleanup, attempt every item under a shared concurrency limit, keep failures queued for the next call, and surface one representative error.

<details>
<summary>The full story (15/N)</summary>

- **Situation:** `release_not_ready` deletes many claims by name.
- **What goes wrong:** Stopping at the first failure would leave the rest undeleted until another call. Serial deletes are slow. Unbounded parallel deletes would exceed the shared connection pool and API Priority and Fairness budget (the API server's per-client request limits).
- **What we do:** `sandbox_batch.py: SandboxBatch.release_not_ready` fans out with `ThreadPoolExecutor(min(len(names), max_in_flight))`. `async_sandbox_batch.py: AsyncSandboxBatch.release_not_ready` uses `asyncio.gather(return_exceptions=True)`. Each delete also takes `_in_flight`, so creates and deletes together never exceed `max_in_flight`. Both collect every exception and raise `errors[0]`. Failed names stay pending for the next call ([API-19](#api-19)). Test: `test_member_deletes_run_in_parallel_and_share_max_in_flight_with_creates['release_not_ready respects max_in_flight']`.
- **Related:** [API-19](#api-19), [API-21](#api-21). [CANCEL-23](cancel.md#cancel-23) compares the thread pool and gather fan-outs.

</details>

<a id="api-23"></a>

## API-23 An optimization path must never raise or change the caller's error

**Takeaway:** Side paths that only make things faster should fall back to the slow path on any failure, and leave error reporting to the authoritative operation. Parse external status fields defensively, checking type and range.

<details>
<summary>The full story (19/N)</summary>

- **Situation:** `acquire()` may refresh warm credit (the warm pool's `readyReplicas`, used to let creates skip pacing). The refresh is purely a latency optimization on top of the real operation, the claim create.
- **What goes wrong:** If a transient GET failure, a deleted pool (`None`), or a malformed status escaped from the refresh, `acquire` would fail with an error unrelated to whether a claim can be created, or with a confusing type.
- **What we do:** `SandboxBatch._refresh_warm_credit` (and the async twin) turns any `Exception` (plus a debug log), a `None` object, or a non-int or `<= 0` `readyReplicas` (`batch_utils.warmpool_ready_replicas`) into credit 0. An exception that arrives after this acquire's own deadline records nothing, so another acquire with time left can still refresh. Only health errors from `remaining_if_due`'s `_check_healthy` propagate. `acquire` then takes a paced slot, "so the claim create surfaces any real pool failure" (19/N, `2ecdf384`). Recording 0 also keeps callers from hammering a broken endpoint during the 1 s refresh interval (old learnings 3.11).
- **Related:** [API-11](#api-11). [RETRY-15](retry.md#retry-15) and [PERF-13](perf.md#perf-13) cover when the refresh runs, and [RETRY-18](retry.md#retry-18) covers how its timeout is capped.

</details>

<a id="api-24"></a>

## API-24 Reserve the label keys that drive destructive selectors

**Takeaway:** When labels decide what gets deleted, reject caller labels that override those keys, and enforce a one-to-one mapping between logical groups and label values. Strip framework-owned labels when you hand user metadata back.

<details>
<summary>The full story (12/N, 15/N, 16/N)</summary>

- **Situation:** Caller labels (key/value tags Kubernetes uses for selection) go onto every claim and onto the Lease. `agents.x-k8s.io/batch-id` scopes the watch and `release()`, and `agents.x-k8s.io/batch-group` scopes `release_group()`. On re-attach, `get_batch` rebuilds the caller labels from the Lease's labels.
- **What goes wrong:** A caller label overriding batch-id or batch-group would make `release()` or `release_group()` delete the wrong claims, or miss some. Two groups on one pool would share one group-label hash, so `release_group` of one would delete both. On re-attach, the SDK's own labels would come back as if the caller had set them, and re-applying them as caller labels would duplicate or override the batch-id label on new claims.
- **What we do:** `batch_utils.validate_claim_batch_args` rejects `BATCH_ID_LABEL` and `BATCH_GROUP_LABEL` in caller labels and duplicate warm pools with `ValueError`, and runs `validate_labels`. `batch_lease_metadata` writes the Lease labels as the caller labels plus `BATCH_ID_LABEL` and `CREATED_BY_LABEL`, and `parse_lease_metadata` returns the Lease labels minus those two keys (12/N `522dad4c`, 15/N `113fae28`, 16/N `dc9ea439`).
- **Example:**
  ```python
  client.claim_batch(groups=[...], labels={"agents.x-k8s.io/batch-id": "x"})
  # ValueError: labels must not set agents.x-k8s.io/batch-id; claim_batch sets it
  ```
- **Related:** [API-8](#api-8), [API-25](#api-25). [K8S-14](k8s.md#k8s-14) explains the group label hash.

</details>

<a id="api-25"></a>

## API-25 Use one marker for cleanup scope and another for accounting

**Takeaway:** Tag items with one marker for "which bulk operations include me" and a different one for "which totals I count toward". Then items added later share cleanup scope without joining aggregates that are already decided.

<details>
<summary>The full story (16/N)</summary>

- **Situation:** `acquire()` claims must be freed by `release_group(pool)`, yet stay out of `events()`, the quorum, `min_ready`, and `release_not_ready()`.
- **What goes wrong:** Reusing the fill's group annotations would make replacements count toward, and corrupt, finished group and quorum accounting. Leaving off the group label would make `release_group` miss them.
- **What we do:** `SandboxBatch.acquire` (and the async twin) sets labels `{**self._labels, BATCH_GROUP_LABEL: group_label_value(warmpool)}` and only a trace annotation. The hashed group label scopes `deletecollection`, and the size and min-ready annotations mark fill membership. `BatchState.upsert_claim` treats a claim as fill only if both annotations are present. A size-0 group (`BatchGroup(size=0)`, a pool with no initial fill) is left out of `_group_by_pool`, and the quorum consumers skip it. `get_batch` rebuilds a pool that has only acquired claims as a size-0 group. Tests: `test_creates_a_claim_outside_the_fill...`, `test_release_group_frees_the_acquired_members_of_a_size_zero_group`.
- **Related:** [API-24](#api-24), [API-26](#api-26). [COUNT-11](count.md#count-11) covers quorum when every group has size 0.

</details>

<a id="api-26"></a>

## API-26 Fail closed when replicated configuration copies disagree

**Takeaway:** When you rebuild configuration from many copies stored on child objects, require the copies to agree and fail loudly if they don't. Don't take a majority or the first value. Check which library exceptions subclass which, so one handler covers them.

<details>
<summary>The full story (1/N, 7/N)</summary>

- **Situation:** Group `size` and `min_ready` are stored as annotations on each fill claim and read back when a handle attaches with `get_batch`.
- **What goes wrong:** If one claim carries only one annotation, two claims disagree, or the values are invalid, silently picking one would make the quorum math wrong for the whole batch.
- **What we do:** `batch_state.reconstruct_groups` raises `BatchError` for one annotation without the other, different `(size, min_ready)` pairs in one pool, or values that `int()` or the `BatchGroup` validator reject. One `except ValueError` covers both, because pydantic's `ValidationError` subclasses `ValueError`. A pool with no annotations on any claim (acquire-only claims, or a fill group whose fill claims are all gone) becomes `BatchGroup(size=0)`. Tests in `test_batch_state.py`: `test_conflicting_annotations_raise`, `test_partial_annotations_raise`, `test_invalid_annotation_values_raise_batch_error`, `test_unannotated_pool_becomes_size_zero`.
- **Related:** [API-8](#api-8), [API-25](#api-25). [K8S-18](k8s.md#k8s-18) covers rebuilding group order from ordinals.

</details>

<a id="api-27"></a>

## API-27 Build an idempotent create's body once and resend it unchanged

**Takeaway:** If a retry treats "409 AlreadyExists" as "my earlier attempt landed", every attempt must describe the same object, so build the body once and resend it. Leave optional sections out when they are empty instead of sending empty objects.

<details>
<summary>The full story (23/N)</summary>

- **Situation:** Create retries treat 409 AlreadyExists on attempt 2 or later as success, so a retry must describe the same object as attempt 1.
- **What goes wrong:** If the body were rebuilt per attempt, or the pod metadata dropped on retry, a 503-then-201 sequence could create a claim without the pin. Writing an empty `additionalPodMetadata`, or copying the annotations onto the claim's own metadata, changes the object's shape for no reason.
- **What we do:** `acquire` builds `pod_metadata` once and passes the same object through `_create_one` into each `_send_create` (both shells) (23/N, `8f504f86`). `build_pod_metadata` returns `None` for `None` or `{}`. The pre-existing `k8s_helper.create_sandbox_claim` (from #979, not changed by 23/N) emits `spec.additionalPodMetadata` only `if pod_metadata:`, using `model_dump(exclude_none=True)`. The test fails attempt 1 of `b1-1` with a 503, then checks that both bodies carry `{'annotations': pin}`, that the claim's `metadata.annotations` lacks the key, and that the other creates have no `additionalPodMetadata`.
- **Example:**
  ```text
  b1-0  fill           -> no field
  b1-1  503            -> field
  b1-1  retry          -> same field
  b1-2  None, b1-3 {}  -> no field
  ```
- **Related:** [API-3](#api-3). [K8S-6](k8s.md#k8s-6) and [RACE-4](race.md#race-4) cover what 409 means on each attempt.

</details>

<a id="api-28"></a>

## API-28 Replace `Any` at SDK boundaries with the real generated types

**Takeaway:** Type helper boundaries with the client library's real models, and narrow Optional fields once in a helper that raises a domain error. Typing often exposes stub bugs and objects crossing between runtimes.

<details>
<summary>The full story (21/N)</summary>

- **Situation:** The helper Lease methods, and the helper parameters in the batch modules, were typed `Any`, so mypy (Python's static type checker) checked nothing at those call sites.
- **What goes wrong:** Typing them showed that `V1Lease.metadata` and `V1Lease.spec` are Optional on the client model, and that kubernetes_asyncio 36's stub types `holder_identity` as `str` although `None` is how the holder is cleared (per the 21/N commit message). The async client's atexit hook also passes an `AsyncV1Lease` into the sync cleanup path.
- **What we do:** An overloaded `batch_utils.lease_parts(lease)` returns `(metadata, spec)` for sync or async Leases, or raises `BatchError` on a malformed Lease, and call sites unpack it. `k8s_helper.py` and `async_k8s_helper.py` type Lease methods as `V1Lease`. `async_sandbox_batch.py: AsyncSandboxBatch.detach` casts `None` for `holder_identity`. `sandbox_batch` types `last_lease` as `V1Lease | AsyncV1Lease` (21/N, `8ba561c3`, `7f65a8c0`).
- **Related:** [API-8](#api-8).

</details>

<a id="api-29"></a>

## API-29 Surface the server's error message, not the HTTP exception's str()

**Takeaway:** Never show an HTTP client exception's `str()` as a user-facing message. Extract the server's structured error message and drop transport details.

<details>
<summary>The full story (12/N, 16/N)</summary>

- **Situation:** A create that fails for good becomes a FAILED `Member` with reason `CreateFailed` and a human-readable `message`, which callers log or show in UIs.
- **What goes wrong:** `str(ApiException)` includes every response header and the raw body. That is noisy, adds up across thousands of members, and can leak headers into logs and UIs.
- **What we do:** `_create_failure_message` (in `sandbox_batch.py` and `async_sandbox_batch.py`) calls `batch_utils.api_status_message(error.body, error.reason)` for an `ApiException`, and uses `str(error)` for any other error. `api_status_message` parses the body as a Kubernetes Status and returns its `message`, falling back to `reason` when the body is empty, not JSON, or has no message (12/N `522dad4c`, 16/N `dc9ea439`). The test `test_create_retries_transient_errors_and_fails_on_403` (both shells) gives a 403 whose body is a Status and asserts the member ends as `(FAILED, "CreateFailed", "sandboxclaims is forbidden")`.
- **Example:**
  ```text
  403 body {"kind": "Status", "message": "sandboxclaims is forbidden"}
  member.message -> "sandboxclaims is forbidden" (no headers, no raw body)
  ```
- **Related:** [API-9](#api-9), [API-1](#api-1), [LIB-19](lib.md#lib-19).

</details>

---

[Learnings home](../../learnings.md) · [Field guide index](../../learnings.md#sec-field-guide)
