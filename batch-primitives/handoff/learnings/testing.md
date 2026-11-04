<a id="sec-test"></a>

[Learnings home](../learnings.md) · [Field guide index](../learnings.md#sec-field-guide)

# Testing techniques for concurrent and distributed code

This section covers how we tested code that runs on several threads or asyncio tasks, sleeps, retries, and talks to a Kubernetes apiserver that other actors also change. The one big idea: **a race test is only useful if the test, not the scheduler, picks the interleaving.** Almost every entry below is a way to take control of time, of blocking calls, or of the exact moment a competing action fires, so the test runs the same way every time in milliseconds. The first entries are habits that apply to any test suite. The middle entries are concrete tricks for forcing interleavings. The last ones cover fakes, end-to-end (e2e) tests, and the blind spots of static tools.

<a id="test-1"></a>

## TEST-1 Mutation-check every new or changed test

**Takeaway:** A test only proves something if it fails when the code it protects is broken. After writing or changing a test, break the protected code on purpose and confirm the test fails for the reason you intended.

<details>
<summary>The full story (stack-wide; examples from 12/N, 15/N, 20/N)</summary>

- **Situation:** A "mutation check" means you make a small deliberate bug in production code (delete a line, swap a lock type, flip a condition) and run the test. If the test still passes, it is not guarding that line.
- **What goes wrong:** Several tests passed against broken code until we checked them:
  - `assert_called_once` on an async mock passes even when the code never awaits the call. The fix was `assert_awaited_once`.
  - A sync put-back (returning a connection to the cache after a failed close) lost its owning test when a later PR rerouted cleanup.
  - A no-grace retry branch in `detach()` had no test at all.
- **What we do:** Every new or changed test comes with a stated mutation and the observed failure. Examples from the suite:
  - The same-thread re-entry tests state "use `Lock` instead of `RLock`: hangs" (see [TEST-11](#test-11)).
  - Removing either `del page` in the paged claim count failed both the sync and async weakref tests (see [TEST-19](#test-19)).
  - The BFS model checker had 5 negative-control toggles, so we knew it could catch each bug class (see [TEST-24](#test-24)).
- **Example:**
  ```python
  mock.close.assert_called_once()   # passes even if the code forgot `await`
  mock.close.assert_awaited_once()  # fails unless the coroutine was awaited
  ```
- **Related:** [TEST-2](#test-2), [TEST-24](#test-24).

</details>

<a id="test-2"></a>

## TEST-2 One owning test per contract, at the strongest boundary

**Takeaway:** Give each promise your code makes one clearly owning test at the layer where it is strongest. When a later change reroutes the code path, re-check that every contract still has its owner.

<details>
<summary>The full story (9/N-13/N cleanup rework, 13/N-16/N)</summary>

- **Situation:** A "contract" here is a promise such as "a connection whose close fails stays tracked for a retry". The batch handle has a pure state core plus two shells (sync and async), so a rule can be tested at several layers.
- **What goes wrong:** When a later PR reroutes a path (sync exit cleanup became release-based in the stack), the old test may keep passing while it no longer runs the code that keeps the promise. The put-back case in [TEST-1](#test-1) is exactly this.
- **What we do:** Decision logic gets its owning tests in the pure core (`test_batch_state.py`, see [TEST-3](#test-3)). The shell suites keep only one wiring test per contract, to prove the shell calls the core correctly. After any reroute, walk the list of contracts and confirm each one still has an owning test at that layer.
- **Related:** [TEST-1](#test-1), [TEST-3](#test-3).

</details>

<a id="test-3"></a>

## TEST-3 Put race-prone decisions in a pure core and test races as plain call sequences

**Takeaway:** Keep state transitions in a "Sans-I/O" core (no network, no locks, no timers). Then any race becomes a straight-line test that calls methods in the order the race would produce, with zero timing. Save threaded tests for wiring.

<details>
<summary>The full story (13/N, 14/N, 15/N, 16/N core extraction and later)</summary>

- **Situation:** `batch_state.BatchState` holds the members, settlement, quorum, release tombstones, and consumer modes. Both shells (`SandboxBatch`, `AsyncSandboxBatch`) hold their own lock and call the same core methods. The rules include flaps (Ready, then NotReady, then Ready), release racing queued events, and success racing a deadline.
- **What goes wrong:** Testing those rules through threads and a fake apiserver is slow and depends on timing. Many interleavings are hard to even state.
- **What we do:** `test_batch_state.py` (`TestStreamMode`, `TestGroupMode`, `TestQuorumMode`, `TestSettle`, `TestReattachedFill`, `TestRelease`) calls `upsert_claim`, `mark_lost`, `expire_fill`, `set_mode`, and `release_*` on a bare `BatchState` in the exact order of the race, then drains the queues with helpers like `_drain_events`. Mode-parameterized `subTest`s (Python `unittest`'s way to run one test body for several inputs) share one scenario between GROUP and QUORUM. `TestSnapshots.test_members_pod_ips_cannot_be_mutated` also checks that `members()` returns immutable data (`pod_ips` is a tuple, so `.append` raises `AttributeError`), so callers cannot corrupt the core's state.
- **Example:**
  ```python
  state.upsert_claim(_ready('b1-0')); state.upsert_claim(_ready('b1-1'))
  state.release_member('b1-0')
  _drain_events(state)   # -> [READY b1-1]; the queued READY for b1-0 is not handed out
  ```
  A flap (size 3, min_ready 2): `upsert(ready b1-0); upsert(pending b1-0); upsert(ready b1-1); upsert(ready b1-2)` gives a cohort `[b1-1, b1-2]` that excludes `b1-0`.
- **Related:** [TEST-2](#test-2), [TEST-24](#test-24), [BP-33](best-practices.md#bp-33).

</details>

<a id="test-4"></a>

## TEST-4 Gate sleeps on Events instead of racing them with short sleeps

**Takeaway:** When a test must act "during" a sleep or backoff, replace the sleep with a function that signals arrival and blocks until the test says go. Never time a race with a tiny real sleep.

<details>
<summary>The full story (19/N)</summary>

- **Situation:** A create retry sleeps in backoff before its next attempt. The test needs `release_member()` to run while the retry is in backoff, then check what the retry does afterwards.
- **What goes wrong:** With `backoff = 0.05s`, fast runs sometimes finished before entering backoff, and slow runs outran the sleep. The test was flaky and did not reliably hit the post-backoff check.
- **What we do:** The test intercepts `_stop_creating.wait` (sync) and `_wait_for_stop` (async) at a sentinel backoff duration (1.0 s, returned by a patched `batch_utils.retry_delay`). The fake sets an `entered_backoff` Event and blocks until the test sets `finish_backoff`. This tests the exact post-backoff check with no real sleep and no flake risk.
- **Example:**
  ```python
  def wait_gate(timeout=None):
      if timeout == SENTINEL:
          entered_backoff.set(); finish_backoff.wait(5)
          return batch._stop_creating.is_set()
      return orig_wait(timeout)
  ```
- **Related:** [TEST-5](#test-5), [TEST-6](#test-6), [TEST-12](#test-12).

</details>

<a id="test-5"></a>

## TEST-5 Inject the clock and make the fake wait advance it, then assert exact timestamps

**Takeaway:** For deadline logic, route every wait through an injectable event or clock, and make the fake wait add its timeout to a fake clock. Assert exact instants, and cap the fake's iterations so a broken stop condition fails fast instead of hanging.

<details>
<summary>The full story (3/N, 31a47728)</summary>

- **Situation:** The renew loop keeps the batch's Lease (a small `coordination.k8s.io` object that says "this holder is alive") fresh. The holder must declare its Lease expired exactly D seconds after it sent its last successful renewal. That depends on interval waits, backoff jitter, and request durations. The SDK reads a monotonic clock (`time.monotonic()`, which never jumps backward like wall-clock time can).
- **What goes wrong:** Real sleeps make these tests slow and flaky. Jitter makes expected timestamps random. You cannot assert "expired at 13".
- **What we do:** `test_sandbox_batch.py` has `_FakeClock` and `_ClockStopEvent`. `TestRenewal._run_renew_loop` patches `sandbox_batch.time` with the fake clock and replaces `_renew_stop` with a `_ClockStopEvent` whose `wait(timeout)` adds `timeout` to the clock and returns False. It raises after 100 waits so a loop that never ends fails. Mocked `replace` calls advance the clock to model slow responses (`+2s`) or hangs (`clock.now += _request_timeout; raise TimeoutError`). `batch_utils.backoff_delay` is patched to return the failure count, so the nth retry waits n seconds and every instant is an integer. Tests such as `test_expiry_fires_a_lease_duration_after_the_last_successful_renewal_was_sent` and `test_failed_renewals_retry_on_a_backoff_and_stop_at_expiry` assert `sent == [3, 4, 6, 9]` and `clock.now == 13`. The async suite has a twin `_FakeClock`.
- **Example:**
  ```text
  t3  renew ok (takes 2s)
  t8  renew fails
  t9  renew hangs until t12
  t13 next retry is capped at expiry -> Lease declared expired at exactly 13
  ```
- **Related:** [TEST-4](#test-4), [TEST-6](#test-6), [TEST-7](#test-7), [LEASE-5](field-guide/lease.md#lease-5), [RETRY-9](field-guide/retry.md#retry-9).

</details>

<a id="test-6"></a>

## TEST-6 Jump a patched clock and inject competing actions at the exact wait point

**Takeaway:** Make time and blocking primitives patchable at module or instance level. Tests can then jump the clock past a deadline, raise at the exact wait, or perform another worker's action during a sleep, which turns timing races into fast, exact assertions. Synchronize on observable state, never on sleeps.

<details>
<summary>The full story (13/N, 14/N, 16/N, 19/N)</summary>

- **Situation:** Behaviors depend on 60 s quorum timeouts, 5 s 429 cooldowns (HTTP 429 means "too many requests"), interrupts during `wait_for_quorum`, and other workers reserving pacing slots while one worker is in backoff.
- **What goes wrong:** Real waits take a minute. Real signals cannot be aimed at a specific line. A competing worker's timing cannot be reproduced reliably.
- **What we do:**
  - Patch the module's `time` (in `sandbox_batch` or `batch_utils`) with `SimpleNamespace(monotonic=lambda: time.monotonic() + offset[0])`. Advance `offset[0]` (sometimes from inside a create hook) to jump past the cooldown or deadline, then push one watch event to wake the waiter. Used in `test_groups_yield_when_ready_and_time_out_after_the_last_create`, `test_quorum_timeout_raises_batch_timeout_error_and_stops_the_remaining_creates`, `test_first_429_paces_creates_during_cooldown_and_recovers`.
  - Inject `KeyboardInterrupt` by patching `batch._changed.wait` (`test_any_exception_that_ends_the_wait_fails_the_quorum`).
  - Replace `handle._stop_creating.wait` with a function that records timeouts, simulates another worker calling `_reserve_create_slot` or moving `_next_create_at`, and returns at once (`test_a_backoff_retry_leaves_the_next_pacing_slot_free_and_a_retry_after_takes_it`, `test_a_backoff_retry_is_not_paced_past_the_deadline`).
  - Synchronize a background waiter by polling an observable flag (`_wait_until` the state's mode is QUORUM in `TestWaitForQuorum.wait_in_background`) instead of sleeping. Gate the first create with an Event and set `max_in_flight=1` to serialize creates.
- **Example:**
  ```python
  def wait(t):
      waits.append(t)
      handle._reserve_create_slot(now)  # a competitor takes the next slot during backoff
      return False
  handle._stop_creating.wait = wait
  ```
- **Related:** [TEST-4](#test-4), [TEST-5](#test-5).

</details>

<a id="test-7"></a>

## TEST-7 Make infinite loops testable: give them a terminal input and record their sleeps

**Takeaway:** Run a background loop inline in the test and end it with a terminal input from the fake (a fatal error, or a stop flag set from inside the mock). Inject the sleep primitive and record its arguments, so the retry schedule and jitter bounds are asserted exactly with no sleeping.

<details>
<summary>The full story (1/N, 2/N, 12/N, 17/N)</summary>

- **Situation:** The watch loop (a long-lived HTTP stream of change events for the batch's claims) runs forever on a background thread or task, with backoff sleeps, network calls, and concurrent cleanup of stale connections.
- **What goes wrong:** On a real thread with real sleeps the tests are slow and flaky, and you cannot assert exact retry delays or interleavings.
- **What we do:** `test_sandbox_batch.TestWatcher` calls `handle._watch_loop()` synchronously. The helper mock's `side_effect` list (a mock's scripted sequence of return values or exceptions) ends with a non-retryable 403 (`_terminating_error`), or with a mock that sets `_watch_stop` from inside the call (`_retry_then_stop`), so the loop returns deterministically. Replacing `handle._watch_stop.wait` with a recorder (`lambda t: delays.append(t)`) checks the exact backoff sequence and the equal-jitter bounds. Events inside a mocked `close_connection` pause the watch mid-close while cleanup runs (`test_a_stale_connection_another_caller_took_is_not_closed_again`). The async test `test_a_watch_cancelled_while_closing_stale_connections_keeps_them_for_cleanup` cancels the task at that point to prove nothing leaks (see [TEST-14](#test-14)).
- **Example:** from `test_consecutive_failures_and_empty_watches_back_off_and_an_event_resets_the_delay`:
  ```text
  side_effect = [503, ProtocolError, 429, iter(()), event_then_503(), stop()]
  delays within [0.25,0.5], [0.5,1], [1,2], [2,4], then [0.25,0.5] after the event resets
  ```
- **Related:** [TEST-5](#test-5), [TEST-14](#test-14), [CANCEL-8](field-guide/cancel.md#cancel-8).

</details>

<a id="test-8"></a>

## TEST-8 Fire the competing action from inside the test double

**Takeaway:** A race test is deterministic when a test double triggers the interleaving at the exact point: a container method, or a mocked I/O call that performs the concurrent caller's state change. Keep such doubles minimal, and change them only when the code under test legitimately changes.

<details>
<summary>The full story (9/N, 10/N, 12/N, 13/N)</summary>

- **Situation:** Two kinds of race needed coverage. (1) Registry races: the client's batch registry dict is read and changed by several threads (unregister, `get_batch`, exit cleanup snapshots). (2) "Carry the value in a local" rules: `detach(grace=17)` decides its grace under the lock and must keep it across a 409 retry (HTTP 409 Conflict, here a stale resourceVersion) even if another detach stores a different grace in the meantime.
- **What goes wrong:** Without a hook at the exact point, the racing thread rarely lands in the window, so code that re-reads shared state or skips a lock looks correct.
- **What we do:**
  - Registry: a dict subclass (`RacingDict`) whose `get`, `items`, or `clear` starts the competing operation on another thread. With the lock, the other thread blocks. Without it, the operations interleave and the test fails every time. Tests include `test_a_handle_registered_during_unregister_stays_registered` and `test_a_handle_registered_during_cleanups_snapshot_is_not_released`. Our double later had to race only on its first lookup, because the code under test legitimately added a second lookup. The original doubles joined the racer with a short timeout; [TEST-9](#test-9) replaced that.
  - Local-carry: in `test_another_detach_during_the_write_keeps_this_calls_grace` (async), the mocked `replace_batch_lease` sets `handle._detach_grace = 99` itself, then raises 409 so the code retries. The test asserts the retried Lease has duration 17.
- **Example:**
  ```python
  async def replace(*a, **k):
      handle._detach_grace = 99      # what a concurrent detach would do
      raise ApiException(status=409) # force the retry that would re-read it
  ```
- **Related:** [TEST-9](#test-9), [TEST-10](#test-10), [TEST-11](#test-11).

</details>

<a id="test-9"></a>

## TEST-9 Instrument the lock so the test knows the moment a racer blocks

**Takeaway:** Wrap the real lock so it announces contention: try a non-blocking acquire first, and set an Event before blocking. "The racer is blocked or finished" is then a deterministic rendezvous, which removes sleeps and timed joins from race tests.

<details>
<summary>The full story (12/N, 522dad4c; also 13/N cleanup tests)</summary>

- **Situation:** Tests must prove that a second caller waits on a lock (or does not) while the first caller is mid-operation, for example `release()` and `detach()` racing on the lifecycle lock.
- **What goes wrong:** Joining the racer with a short timeout (the original approach in [TEST-8](#test-8)) is slow when the lock works and can be flaky on a loaded CI machine.
- **What we do:** `test_sandbox_batch.py: _SignalingLock` wraps the lock. In `__enter__` it tries `acquire(blocking=False)`. On failure it sets `_blocked_or_done` and then blocks. `start_racing(target)` starts the racer thread and waits on that Event, which the racer also sets when it finishes. The test continues the moment the racer is blocked or done, then asserts which one. `test_the_first_lifecycle_call_wins` runs a matrix of (first, second) pairs: release/detach, detach/release, release/release. The async twin checks `lock.locked()` instead.
- **Example:**
  ```python
  def __enter__(self):
      if not self._lock.acquire(blocking=False):
          self._blocked_or_done.set(); self._lock.acquire()
  ```
- **Related:** [TEST-8](#test-8), [TEST-10](#test-10).

</details>

<a id="test-10"></a>

## TEST-10 Hit the window between two attribute writes with a `__class__` swap

**Takeaway:** In Python, swapping an instance's `__class__` to a subclass that overrides `__setattr__` lets a test fire a concurrent action the moment a given field changes, with no hooks in production code.

<details>
<summary>The full story (13/N, 8ad5a407)</summary>

- **Situation:** `detach()` sets `_detached` and then `_detach_grace`. Exit cleanup on another thread reads both to decide whether to detach or release the batch.
- **What goes wrong:** The gap between two plain assignments has no hook point. Mocks cannot reach it without changing production code.
- **What we do:** `TestClientBatchCleanup.test_exit_cleanup_during_detach_detaches_an_attached_handle` sets `handle.__class__` to `CleanupBetweenDetachWrites`, whose `__setattr__` calls `super()` and then, on `_detached = True`, starts exit cleanup through `_SignalingLock.start_racing` ([TEST-9](#test-9)). The test asserts that no deletecollection happened and the registry is empty.
- **Example:**
  ```python
  class CleanupBetweenDetachWrites(SandboxBatch):
      def __setattr__(self, name, value):
          super().__setattr__(name, value)
          if name == '_detached' and value: lock.start_racing(cleanup)
  handle.__class__ = CleanupBetweenDetachWrites; handle.detach()
  ```
- **Related:** [TEST-8](#test-8), [TEST-9](#test-9).

</details>

<a id="test-11"></a>

## TEST-11 Simulate signal-handler re-entry from inside a mocked call, and bound every possible deadlock

**Takeaway:** Model an asynchronous interruption (like a signal handler) by calling the API again from inside the mocked blocking call on the same thread. Run code that might deadlock on a daemon thread with a bounded join, so a hang becomes a test failure.

<details>
<summary>The full story (12/N, 522dad4c, review round 3; 15/N)</summary>

- **Situation:** In sync Python, a SIGINT or SIGTERM handler runs on the main thread between bytecodes, so it can re-enter `detach()` or `release()` while the same thread is already inside one of them. The lifecycle lock is an `RLock` (a lock the same thread may take again) for this reason.
- **What goes wrong:** Real signals land at random points in tests. A self-deadlock would hang the whole suite instead of failing one test. And you need both variants: the outer write landed on the server, or it did not.
- **What we do:** In `test_a_detach_during_detach_on_the_same_thread_does_not_hang`, the mocked `replace_batch_lease` calls `handle.detach()` on its first invocation (the "signal handler"), then raises, as if the outer write failed. `test_a_release_during_detach_on_the_same_thread_makes_detach_raise` covers both before and after the write lands (raise vs return). In the detach-during-detach test, the outer call runs on a daemon thread joined with `timeout=5`. The test asserts the thread finished, no error escaped, and `_lease_released` is set. The stated mutation is "use `Lock` instead of `RLock`: hangs", which the bounded join turns into a failure. The `_drain` helper applies the same pattern to iterators that must end: it collects on a daemon thread and raises `AssertionError` if the thread is still alive.
- **Example:**
  ```python
  def write(*a, **k):
      handle.detach()               # inner call completes during the outer write
      raise RuntimeError('reset')   # outer write did not land
  ```
- **Related:** [TEST-1](#test-1), [TEST-8](#test-8), [LOCK-18](field-guide/lock.md#lock-18).

</details>

<a id="test-12"></a>

## TEST-12 Enumerate every phase of a retry loop as gated rows in one table-driven test

**Takeaway:** For a retry loop with several guards, write one table-driven test whose rows name each phase, and gate every phase boundary with an Event so each row runs the interleaving it names.

<details>
<summary>The full story (19/N)</summary>

- **Situation:** `release_member()` can race a create retry at any phase: before backoff, during backoff, with attempt 2 on the wire, or after attempt 2 fails. The claim can come from the initial fill or from `acquire()`.
- **What goes wrong:** One race test covers one interleaving. Guards for the other phases can regress unnoticed.
- **What we do:** `TestReleaseMembers.test_release_member_stops_create_retries_and_deletes_an_in_flight_retry` loops over 9 (origin, timing) rows: origin in {fill, acquire} times timing in {seen_pending, before_backoff, during_backoff, in_flight}, plus retry_then_fail for fill only. Each case gates the mocked create, delete, and backoff with 9 Events (`fail_first`, `entered_backoff`, `finish_backoff`, `attempt_two`, `finish_retry`, `in_first_delete`, ...), so `release_member` runs at exactly that phase. It asserts the delete count and `members()`. `test_release_not_ready_keeps_handed_out_member_when_create_retries_late` covers the related `release_not_ready` case.
- **Related:** [TEST-4](#test-4).

</details>

<a id="test-13"></a>

## TEST-13 Test single-flight by swapping the Future class for one that reports waiters

**Takeaway:** To test coalescing ("single-flight", where many callers share one in-flight check), substitute the coordination primitive at its import site with a subclass that reports when waiters arrive or forces an interleaving. Release the shared work only after the expected number of waiters are parked. In asyncio, yield one turn with `sleep(0)` and assert on public state, never on private attributes.

<details>
<summary>The full story (16/N, dc9ea439, 810d3773)</summary>

- **Situation:** The first `acquire()` on a new warm pool runs a precheck, and concurrent `acquire()` calls on that pool wait on one shared `Future` in `_prechecks`. Tests must cover followers that really wait, a leader that is interrupted, and a follower that times out as the leader cancels.
- **What goes wrong:** Starting threads and sleeping cannot guarantee that followers reached `future.result()` before the leader finished, so the test might only cover the path where nobody waits. The earlier async tests read the private `Future._callbacks`, which broke on Python 3.14.
- **What we do:**
  - Sync: `patch.object(sandbox_batch, 'Future', _WaitedFuture)`. Its `result()` appends `self` to a list, and the test waits until `len(waiting) == 2` before releasing the gated GET (`test_concurrent_first_acquires_check_a_pool_once...`; with one waiter, `test_an_interrupted_pool_check_is_run_again_by_a_waiting_acquire`).
  - `_RaceFuture.result(timeout)` sleeps past the timeout, releases the leader, waits until `self.cancelled()`, then raises the chosen error. That forces "the check is cancelled as or after the waiter's timeout" (`test_an_acquire_waiting_on_another_acquires_pool_check_stops_at_its_timeout`).
  - Async: one `await asyncio.sleep(0)` puts the followers on the future, checked with public state (`not task.done()` and `warmpool in batch._prechecks`). A patched `asyncio.wait_for` (`race_wait_for`) cancels and interleaves deterministically.
- **Example:**
  ```python
  class _WaitedFuture(Future):
      def result(self, timeout=None):
          waiting.append(self); return super().result(timeout)
  ```
- **Related:** [TEST-14](#test-14), [LOCK-15](field-guide/lock.md#lock-15).

</details>

<a id="test-14"></a>

## TEST-14 Cancel an async task at an exact await, and pre-hold locks to prove a section has no await

**Takeaway:** Pin async cancellation tests by having the fake signal its arrival at the target await and then park there forever, so the cancel lands exactly there. To prove a critical section has no suspension point, have the fake hold any lock the section might take: an accidental await then blocks until the cancel, and the assertion fails.

<details>
<summary>The full story (9/N-11/N cleanup tests, 10/N, 12/N, 17/N)</summary>

- **Situation:** Async cleanup must keep connections tracked when it is cancelled mid-close or mid-put-back. The put-back must contain no `await`, because in asyncio a task can only be interrupted at an await, so a section without one is atomic.
- **What goes wrong:** Cancelling after an arbitrary sleep may land before or after the interesting await, so the test passes without testing anything. A regression that reintroduces `async with self._lock` in the put-back would pass a naive test.
- **What we do:**
  - `test_a_watch_cancelled_while_closing_stale_connections_keeps_them_for_cleanup`: the mocked close sets a `closing` Event, then `await asyncio.Event().wait()` (a fresh Event nobody sets). The test awaits `closing`, then calls `task.cancel()`.
  - `test_a_connection_whose_put_back_is_cancelled_stays_on_the_handle`: the mocked `close_connection` acquires `handle._lock` and then raises. Any lock wait in the put-back in `async_sandbox_batch.py: _close_one` blocks until the test cancels the task. The test asserts the entry is back in `_connected` and `_track_closing` was called.
- **Example:**
  ```python
  async def close_connection():
      await handle._lock.acquire()   # hold what the put-back must never wait on
      raise RuntimeError
  ```
- **Related:** [TEST-7](#test-7), [TEST-13](#test-13), [CANCEL-3](field-guide/cancel.md#cancel-3).

</details>

<a id="test-15"></a>

## TEST-15 Count waits and wakeups to catch busy spins and stray wakeups

**Takeaway:** Some bugs only waste CPU and never produce a wrong result. Pin wakeup efficiency by wrapping the wait and set primitives to count calls, and set unrelated deadlines to "already expired" to expose spins.

<details>
<summary>The full story (16/N, dc9ea439, 810d3773)</summary>

- **Situation:** `acquire()` waits on a per-claim Event. Its wait must not depend on unrelated timers (the fill deadline) or wake for other claims' watch events.
- **What goes wrong:** A wait whose timeout comes from an expired, unrelated deadline (for example `max(0, fill_deadline - now)` = 0) spins in a busy loop. Functional asserts still pass, so nobody notices.
- **What we do:** `test_failures_drop_the_member_and_delete_a_claim_that_may_exist` sets `batch._fill_deadline = time.monotonic()` (like an acquire that replaces a member late in a run), wraps the claim's `waiter.wait` to record each call, and asserts `len(waits) < 20` for the whole acquire. `test_watch_events_for_other_claims_do_not_wake_waiting_acquire` wraps `waiter.set`, pushes a pending event for the claim itself plus not-Ready, Ready, and DELETED events for another claim, and asserts `wakes == []` until the claim turns Ready, then `['set']`.
- **Related:** [TEST-6](#test-6), [LOCK-12](field-guide/lock.md#lock-12).

</details>

<a id="test-16"></a>

## TEST-16 Keep fakes faithful to apiserver causality, and patch only the heuristic the fake breaks

**Takeaway:** A fake server must keep the real system's causal order, such as "the watch event arrives independently of the RPC response". Where a fake must depart from real behavior, neutralize only the production heuristic that depends on it, say why at the patch site, and keep a per-test way to restore the real order.

<details>
<summary>The full story (12/N, 522dad4c; 19/N)</summary>

- **Situation:** `test_sandbox_batch.py: _Cluster` is the fake apiserver. Its `_watch` yields from a `queue.Queue` and returns on `Empty`. Its `_create` pushes MODIFIED events (`ready_on_create`), or tests push events directly. The async `_Cluster` uses `asyncio.Queue`. Production backs off when a watch ends sooner than `_SHORT_WATCH_SECONDS = 1` (`sandbox_batch.py`), because a real apiserver watch never ends at once unless something is wrong.
- **What goes wrong:**
  - The fake watch returns as soon as no events are queued, so without help every test would hit the short-watch backoff and slow down or stall.
  - Pushing attempt 1's watch event synchronously inside the mocked attempt 1 POST collapsed two independent channels into one. Once `can_send()` checked `get_member(claim_name) is None`, attempt 2 could never reach the `in_flight` race. That briefly led us to drop a valid production guard instead of fixing the mock's causal ordering (19/N).
- **What we do:** `BaseClaimBatchTest.setUp` patches `sandbox_batch._SHORT_WATCH_SECONDS` to 0, with a comment saying the fake differs from a real watch here. Race tests deliver attempt 1's watch event on their own schedule (gated, see [TEST-4](#test-4) and [TEST-12](#test-12)), or not at all, instead of inside attempt 1's create mock.
- **Related:** [TEST-17](#test-17), [RACE-1](field-guide/race.md#race-1).

</details>

<a id="test-17"></a>

## TEST-17 Fix the test double, never production code, and keep doubles faithful to preconditions

**Takeaway:** When a double misbehaves, fix the double. Never widen production code to suit a mock. Doubles of your own classes must also enforce the real lifecycle preconditions, or they hide real failures.

<details>
<summary>The full story (stack-wide; 15/N, 25/N)</summary>

- **Situation:** Tests use `MagicMock` objects and hand-written fakes such as `FakeBatch` in `test_batch_claims.py`.
- **What goes wrong:**
  - A `MagicMock` attribute is truthy, so `if batch._connected` broke a test. Earlier, production code had been widened to suit a mock; that was reverted.
  - `FakeBatch.release_member` omitted 15/N's `_check_healthy()` guard. That hid that the real `SandboxBatch.release_member` raises `BatchError` once `batch.err()` is set or `batch.release()` has run, which needed a 404-safe `delete_claim` fallback in `BatchClaim.release()` (25/N).
- **What we do:** Set the attribute on the mock (`_connected={}`). Copy each lifecycle guard of the real class into its fake, and add a test for the guarded path.
- **Example:**
  ```python
  batch = MagicMock(_connected={})   # a bare MagicMock attribute would be truthy
  ```
- **Related:** [TEST-16](#test-16).

</details>

<a id="test-18"></a>

## TEST-18 Make the source change on every read to catch "computed once" regressions

**Takeaway:** To check that a value is computed once and shared, make its underlying source return a wildly different value on every read. Repeated reads then show up as different results instead of hiding behind clock resolution.

<details>
<summary>The full story (12/N, 522dad4c; reused in 16/N acquire tests)</summary>

- **Situation:** Every initial claim must carry the same `shutdownTime` (a backstop deletion time on each claim), computed once in `claim_batch`. Each acquired claim's `shutdownTime` counts from its own create.
- **What goes wrong:** Real `now()` calls microseconds apart look equal after formatting, so a regression that recomputes `shutdownTime` per claim would pass.
- **What we do:** `_SteppingDatetime` patches `k8s_agent_sandbox.utils.datetime`, and its `now()` returns a time one hour later on each call. `test_claim_manifests` and the acquire `shutdownTime` tests then see visibly different times if anything recomputes. The async suite has a twin.
- **Related:** [TEST-5](#test-5), [LEASE-27](field-guide/lease.md#lease-27).

</details>

<a id="test-19"></a>

## TEST-19 Prove bounded memory with a weakref checked when the next item is requested

**Takeaway:** In streaming loops over large items, drop references in every frame (producer and consumer) before fetching the next item. Pin it by handing out objects you hold weak references to, and assert inside the next fetch that the previous one is gone.

<details>
<summary>The full story (20/N, a95cb2a1, fc9d8557)</summary>

- **Situation:** `k8s_helper.py: _claim_list_pages` is a generator that fetches claim list pages (500 per page), and `K8sHelper.count_live_sandbox_claims` consumes it. While the generator fetches page N+1, both the generator's local `page` and the consumer's loop variable still point at page N. The async helpers have twins.
- **What goes wrong:** Two parsed pages are alive at once. That is the inferred cause of a 10-12 MB peak instead of roughly half that, one page (C1-pr8-impl.md F3). Asserting only the count misses a regression that keeps every page in a list.
- **What we do:** Both frames `del page` before advancing. `test_count_live_sandbox_claims_counts_claims_not_being_deleted_across_pages` returns `_Page(dict)` instances (plain dicts cannot be weakly referenced). The side effect stores `weakref.ref(page)` and, on the next call, asserts the referent is already gone. This relies on CPython reference counting, which frees an object the moment its last reference goes. The fixture also checks that a 410 (expired continue token) drops the earlier partial count and that items with `deletionTimestamp` are excluded. Removing either `del` failed both the sync and async tests (C2-pr8-fixes.md F1 mutation check).
- **Example:**
  ```python
  def next_page(**_):
      if prev_ref: assert prev_ref() is None
      item = pages.pop(0); prev_ref = weakref.ref(item); return item
  ```
- **Related:** [TEST-1](#test-1), [PERF-5](field-guide/perf.md#perf-5).

</details>

<a id="test-20"></a>

## TEST-20 Test HTTP retry policy against a real loopback socket

**Takeaway:** Mocks cannot show what a transport library's retry logic really does. Test it against a local socket whose state you control (bound, listening, scripted reply), count server-side accepts or requests, and assert the setting on the object the library actually reads, not only where you set it.

<details>
<summary>The full story (22/N, 05b7420c)</summary>

- **Situation:** The claim-collection client uses `urllib3.Retry(3, read=False, respect_retry_after_header=False)`: retry connects, never retry reads, never sleep on `Retry-After`. urllib3 is the HTTP library under the sync Kubernetes client.
- **What goes wrong:** A config that looks right (for example, one that forgets `respect_retry_after_header`) passes every mock-based test. Checking only `configuration.retries` misses whether the pool manager actually got the `Retry` object.
- **What we do:** `test_k8s_helper.py: TestLeaseApiClient.test_claim_collection_requests_do_not_retry_reads_or_retry_after_and_keep_injected_settings`:
  - A socket bound but not listening refuses connects. The test expects `MaxRetryError`, which proves connect retries are wrapped.
  - After `listen()` with no reply, each call raises `ReadTimeoutError`. The test `accept()`s exactly one pending connection and asserts a second `accept()` raises `BlockingIOError`, which proves one attempt and no read retry.
  - A tiny threaded server answers `429` with `Retry-After: 0`. The test asserts `throttled_attempts == 1` and an `ApiException(429)`.
  - It checks both `configuration.retries` and `rest_client.pool_manager.connection_pool_kw['retries']`.
- **Example:**
  ```text
  bind only        -> connect refused -> MaxRetryError (connect retries ran)
  listen, no reply -> ReadTimeoutError; accept() once ok, accept() again -> BlockingIOError (1 attempt)
  429 Retry-After:0 server -> attempts == 1
  ```
- **Related:** [TEST-21](#test-21), [LIB-7](field-guide/lib.md#lib-7), [LIB-8](field-guide/lib.md#lib-8).

</details>

<a id="test-21"></a>

## TEST-21 Mutate an injected client after construction to prove derived clients read config live

**Takeaway:** When you derive a client with different transport settings from a caller's client, test credential propagation by changing the original after construction. That catches accidental snapshots of auth state.

<details>
<summary>The full story (7/N shallow-copied config; 22/N claim-collection client)</summary>

- **Situation:** The Lease client and the claim-collection client are shallow copies of a caller-injected `ApiClient`, with different retry settings.
- **What goes wrong:** A deep copy, or a snapshot of headers or cookies, would freeze credentials. Rotated tokens or exec-plugin refreshes (kubeconfig plugins that fetch fresh tokens) would then stop working on those clients only.
- **What we do:** `_assert_delegates_to_injected` and `test_lease_requests_are_not_retried_and_keep_the_injected_client_settings` build the helper, then change the injected configuration's `api_key`, `api_key_prefix`, default headers (including replacing the dict), and cookie. They assert the derived client and its serialized request headers reflect each change, while retries stay overridden (`False` or the `Retry` object) and the injected config's own retries stay `None`.
- **Related:** [TEST-20](#test-20), [LIB-12](field-guide/lib.md#lib-12), [PAT-22](patterns.md#pat-22).

</details>

<a id="test-22"></a>

## TEST-22 Test a wire-contract reader against fixtures written without your writer

**Takeaway:** When other programs may write data your code reads, test the reader against hand-written fixtures, not only against your own writer. Round-trip tests drift together with the writer and hide contract changes.

<details>
<summary>The full story (1/N, get_batch e2e)</summary>

- **Situation:** Other SDKs (the planned Go SDK) and operators may create batches that the Python `get_batch` must adopt.
- **What goes wrong:** A test where Python writes and Python reads can change label, annotation, or Lease field formats on both sides at once and still pass.
- **What we do:** `test_batch_e2e.py: _batch_manifest` writes claims with the batch-id label and `group-size` / `min-ready` annotations, plus a Lease with the `batch-lease-duration` annotation and `renewTime` formatted as `'%Y-%m-%dT%H:%M:%S.%fZ'` (no ownerReferences). A comment notes that MicroTime (the Lease timestamp type) needs exactly six fractional digits. `test_batch_get_batch_members_connect_detach` applies them with kubectl, calls `get_batch`, connects, detaches, and asserts the claims remain and `holderIdentity` is cleared.
- **Related:** [TEST-23](#test-23), [K8S-15](field-guide/k8s.md#k8s-15).

</details>

<a id="test-23"></a>

## TEST-23 E2E crash tests: a real child process you can SIGKILL or SIGSTOP, and lower-bound timing asserts

**Takeaway:** Test crash recovery with a real child process controlled over stdin and stdout, so you can kill it (`SIGKILL`) or pause it (`SIGSTOP`). Use resources that exist but never become ready, and assert lower bounds on cleanup time to prove the cleaner is never early, not only that cleanup eventually happens.

<details>
<summary>The full story (18/N, a97eeae7; SIGSTOP case 73a16a7a)</summary>

- **Situation:** The reaper (a CronJob that deletes stale batch Leases) must free batches whose driver died and leave live or paused drivers alone. Unit tests fake the apiserver, so they cannot show real Lease staleness, a real precondition delete, or the garbage collector (GC) deleting claims whose ownerReference points at a deleted Lease UID.
- **What goes wrong:** You cannot kill the driver if it runs inside the test process. Waiting for Ready sandboxes makes e2e slow and flaky.
- **What we do:** In `test/e2e/clients/python/test_batch_e2e.py`:
  - `_DRIVER_SCRIPT` runs as a `python -c` subprocess. It prints the batch id. On a stdin line it prints `repr(err())` and releases; otherwise it sleeps until killed.
  - `_UNREADY_POOL_MANIFEST` is a warm pool with `replicas: 0` and an image from `registry.invalid`, so claims exist but never become Ready.
  - `test_reaper_reaps_a_killed_drivers_batch_and_leaves_a_live_one` kills one holder, reaps it once it is past `leaseDurationSeconds + skew_margin`, and asserts `reaped_at - killed_at >= D + margin - renew_interval` (via `_reap_until_reaped`), while the live batch keeps its Lease and claims.
  - `test_reaper_leaves_a_paused_drivers_batch_within_lease_duration` pauses the driver with SIGSTOP for 11 s (more than the renew interval of 30//3 = 10, less than D = 30), checks the reaper finds nothing, resumes with SIGCONT, waits for the Lease resourceVersion to change, and asserts `err() is None`. This proves a quiet or stalled holder is never reaped early.
  - `test_reaper_waits_out_a_detach_grace` checks reaping waits at least grace + margin. `test_deleting_a_batch_lease_garbage_collects_its_claims` asserts the ownerReference UID, deletes the Lease by hand, and checks GC removes the claims.
  - Gap: neither the SIGKILL nor the SIGSTOP case reaches the 409 precondition path, because the paused Lease is never stale enough to be picked. The protection for a driver whose clock runs behind is unit-tested only (`test_lease_delete_outcomes`).
- **Example:**
  ```text
  t0   SIGSTOP driver (D=30, renew every 10)
  t0+  reaper runs -> nothing stale
  t11  SIGCONT -> renewal resumes, resourceVersion changes, err() is None
  ```
- **Related:** [TEST-22](#test-22), [LEASE-8](field-guide/lease.md#lease-8), [BP-19](best-practices.md#bp-19).

</details>

<a id="test-24"></a>

## TEST-24 Keep sync and async suites name-aligned, and model-check interleavings that tests cannot enumerate

**Takeaway:** Give every behavior an identically named test in the sync and async suites so a missing twin shows up in a diff, plus shell-specific tests for each runtime's own interrupt model. When two or more of locks, signals, cancellation, and optimistic retries interact, model-check the interleavings and mutation-test the checker.

<details>
<summary>The full story (1/N..23/N; model checker during 15/N)</summary>

- **Situation:** The sync shell uses threads, an RLock, and signals. The async shell uses tasks and cancellation. Same semantics, different primitives, so they drift easily.
- **What goes wrong:** Unit tests check only the schedules someone thought to script. The signal-handler ABBA deadlock (two paths taking two locks in opposite order) and the double-cancel leaked counter would have shipped otherwise.
- **What we do:** `test/unit/test_sandbox_batch.py` and `test_async_sandbox_batch.py` have 136 test names each, 123 shared. The 13 shell-specific ones cover each shell's unique interrupt: same-thread re-entry (`test_a_detach_during_detach_on_the_same_thread_does_not_hang`), stop flags for unjoined threads, cancelled put-backs (`test_a_connection_whose_put_back_is_cancelled_stays_on_the_handle`), and plain-def getters. Races are gated with Events, not sleeps. A standard-library-only Python breadth-first search (BFS) over frozen states (`handoff/work/verify_delete_lifecycle_model.py`) explored every interleaving of threads, tasks, cancels, and signal handlers: 5,905 sync and 3,782 async states (notes `handoff/work/bfs_state_machine_guide.md`). Its 5 negative-control toggles (Ordering 1, Flaws A to D) proved it catches each bug class.
- **Related:** [TEST-1](#test-1), [TEST-3](#test-3), [TEST-11](#test-11), [TEST-14](#test-14), [CANCEL-24](field-guide/cancel.md#cancel-24).

</details>

<a id="test-25"></a>

## TEST-25 Static checks have blind spots: run every affected suite and lint whole files

**Takeaway:** A passing type checker or diff-only linter does not prove the change is clean. Run every test suite that calls a changed signature, and run blank-line style checks on whole changed files, not only changed lines.

<details>
<summary>The full story (19/N; stack-wide lint)</summary>

- **Situation:** The stack relies on `mypy` (Python type checker) and pycodestyle (style linter), sometimes in `--diff` mode that checks only changed lines.
- **What goes wrong:**
  - Adding a required `seq` argument to `BatchState.note_deleted` passed `mypy` and both shell suites (`test_sandbox_batch.py`, `test_async_sandbox_batch.py`). It still failed two direct `state.note_deleted(...)` calls in `test_batch_state.py`, because the package's `[tool.mypy]` config in `pyproject.toml` excludes `test/` directories, so `mypy` never type-checks the unit tests (19/N).
  - pycodestyle `--diff` reports E303 (too many blank lines) on the unchanged line after an added blank line, so the error slipped through.
- **What we do:** After changing an internal signature, search for every caller (including tests) and run each suite that has one. Run the linter on the whole changed file for blank-line rules.
- **Related:** [TEST-26](#test-26).

</details>

<a id="test-26"></a>

## TEST-26 After an environment incident, rerun every check instead of reusing outputs

**Takeaway:** Once the machine or toolchain has had a failure, treat every earlier result as possibly partial and measure again before trusting it.

<details>
<summary>The full story (stack-wide)</summary>

- **Situation:** Test and lint outputs were saved as the stack moved forward.
- **What goes wrong:** After a disk-full reboot, saved outputs might be truncated or stale, and a "pass" might be a check that never finished.
- **What we do:** We reran every check after the reboot instead of reusing outputs that might be partial.
- **Related:** [TEST-25](#test-25).

</details>

---

[Learnings home](../learnings.md) · [Field guide index](../learnings.md#sec-field-guide)
