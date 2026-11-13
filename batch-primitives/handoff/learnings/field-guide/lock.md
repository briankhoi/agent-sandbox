<a id="sec-lock"></a>

[Learnings home](../../learnings.md) · [Field guide index](../../learnings.md#sec-field-guide)

# Concurrency inside one process: locks, waiters, and shared work

This section covers how one batch handle stays correct when many threads (sync `SandboxBatch`) or tasks (async `AsyncSandboxBatch`) touch it at once: user calls, the watch (a background loop that streams claim changes from the apiserver), Lease renewal (the heartbeat that keeps the batch alive), create workers, and client cleanup. The one big idea: **a decision is only true while you hold the lock you made it under.** Once you release the lock, wait on something, or do I/O, someone else may have changed the state, so you either carry the value you decided on in a local, or you take the lock again and decide again. The early entries are general rules about locks and check-then-act. The middle entries are about waiters and limits (Conditions, Events, semaphores). The last entries are the specialized patterns: shared single-flight work, per-pool refresh locks, and the lifecycle lock that orders `detach()` and `release()`.

A few terms used throughout: a **lock** (mutex) lets one thread in at a time. A **Condition** is a lock plus a "wait until someone calls notify" list, shared by every waiter. An **Event** is a one-bit flag that waiters sleep on until someone sets it. A **semaphore** is a counter of free slots (here `_in_flight`, a `BoundedSemaphore(max_in_flight)` that caps live HTTP calls). **Single-flight** means many callers share one in-flight piece of work instead of each doing it.

<a id="lock-1"></a>

## LOCK-1 Make check-then-act one critical section, and remove by identity

**Takeaway:** Two operations on a shared structure are never atomic together, even if each one is. Put the check and the act under one lock, and in a registry where a key can be re-bound, remove an entry only if it is still the same object you meant to remove.

<details>
<summary>The full story (9/N, 10/N)</summary>

- **Situation:** the sync client keeps one live handle per `(namespace, batch_id)` in `_active_batches`. It is used from several threads. `detach()` on an old handle can race `get_batch()` registering a new handle for the same batch.
- **What goes wrong:** the first version did `if registry.get(key) is batch: del registry[key]` without a lock. Another thread could register a new handle between the check and the delete, and the delete removed the new one. Before that, an unconditional delete-by-key removed the newer handle outright, and overwriting on register lost the old handle's leftover connections.
- **What we do:** one lock, `_batches_lock`, covers registration, the identity check plus delete, and cleanup snapshots. `sandbox_client.py: _unregister_batch` deletes only if `_active_batches.get(key) is batch`. `_register_batch` hands a replaced handle to `_track_closing`, which puts it in `_closing_batches` if it still has `_connected` or `_stale_connections` entries, and discards it otherwise (9/N `b03ba91b`, 10/N `74cd8fa5`). Async needs no lock, because there is no `await` between the check and the act, and asyncio cannot switch tasks without an `await`.
- **Example:**
  ```python
  with self._batches_lock:
      if self._active_batches.get(key) is batch:
          del self._active_batches[key]
  ```
- **Why it matters beyond this code:** under the GIL (CPython's global interpreter lock, which runs one bytecode at a time) a single dict operation is atomic, but two in a row are not. Make the lock cover every access to the structure. We first missed `claim_batch` and one snapshot, so the lock rule held in only half the places. Tests that pin it: `test_unregistering_an_old_handle_keeps_a_newer_one_registered`, `test_cleanup_closes_a_reattached_batchs_old_connection_without_stopping_the_old_handle`.
- **Related:** [LOCK-2](#lock-2), [LOCK-14](#lock-14), [LOCK-20](#lock-20). The "leftovers set" half of this story (why displaced handles go to `_closing_batches`) is [CLEAN-7](clean.md#clean-7).

</details>

<a id="lock-2"></a>

## LOCK-2 A get-or-create cache must check and insert in one lock section

**Takeaway:** For a cache of resources that need cleanup, do the miss check, the creation, and the insert atomically, and keep the creation step free of I/O so holding the lock stays cheap. Otherwise one creator's resource ends up owned by nobody.

<details>
<summary>The full story (1/N, 6/N, 17/N)</summary>

- **Situation:** several threads or tasks can call `connect()` for the same member at once. `connect()` returns a cached `Sandbox` from `_connected` or builds a new one.
- **What goes wrong:** if the lookup and the insert were split around construction, two callers could each build a `Sandbox`. One insert would overwrite the other, and the losing connection (which can own a port-forward subprocess) would never be closed by any cleanup.
- **What we do:** `sandbox_batch.py: SandboxBatch.connect` and `async_sandbox_batch.py: AsyncSandboxBatch.connect` run the health check, the live member lookup, the cache lookup, the `sandbox_class(...)` construction, pod-IP seeding, and the `_connected` insert under one `with self._lock` (async: `async with self._lock` with no `await` inside). The `Sandbox` constructor only builds objects and sends no request, so the lock is held briefly. Test: `test_repeat_call_returns_same_handle`.
- **Example:**
  ```python
  with self._lock:
      cached = self._connected.get(name)
      if cached is not None:
          return cached[0]
      sandbox = self._client.sandbox_class(...)   # no network I/O
      self._connected[name] = (sandbox, pod_ip)
  ```
- **Related:** [LOCK-1](#lock-1).

</details>

<a id="lock-3"></a>

## LOCK-3 Carry a value decided under a lock in a local

**Takeaway:** Once you read a shared value under a lock, copy it into a local and use that local for the rest of the operation. Every later re-read of shared state, and every retry loop that re-reads it, is a new race window.

<details>
<summary>The full story (10/N, 11/N, 12/N)</summary>

- **Situation:** `_detach_grace` does two jobs. It stores the caller's grace (so cleanup retries reuse it), and it signals "a detach is in progress". `release()` clears it when it starts, and it can run on another thread or task while `detach()` sits between its lock block and its Lease write.
- **What goes wrong:** `detach()` resolved the grace under the lock, then read the field again at write time. It could pick up another call's grace, or `None` after `release()` cleared it, and then wrote a Lease with no duration. A retry loop added later read the field once more, which widened the window.
- **What we do:** `sandbox_batch.py: SandboxBatch.detach` (and the async twin) copies `grace = self._detach_grace` into a local inside the `_lock` block and uses it for every write attempt, including the 409 retry (12/N). Since 12/N, `release()` sets `_detach_grace = None`, so `_release_started()` derives "release started" as `_detached and _detach_grace is None` without a new flag. `_exit_detaches()` reads the same two fields, and `_detach_unfinished()` reads `_detach_grace` with `_lease_released`. Test: `test_another_detach_during_the_write_keeps_this_calls_grace`.
- **Example:**
  ```
  t0 detach: with _lock: grace = self._detach_grace   # 30
  t1 release (other thread): self._detach_grace = None
  t2 detach: PUT Lease with grace=30 (local), not None
  ```
- **Why it matters beyond this code:** deriving a phase from existing fields avoids adding flags, but write the encoding down (here, in the `_release_started` docstring), because the next reader will not guess it.
- **Related:** [LOCK-17](#lock-17), [LOCK-20](#lock-20).

</details>

<a id="lock-4"></a>

## LOCK-4 Check every precondition before mutating local state

**Takeaway:** Validate every condition that can reject an operation before you change in-memory state. Then a failed call leaves both the local view and the cluster untouched.

<details>
<summary>The full story (15/N)</summary>

- **Situation:** `release_member`, `release_not_ready`, and `release_group` checked `_detached` under `_lock`, dropped members from `_state`, and only checked `_lease_released` and `err()` later inside `_delete_claim`.
- **What goes wrong:** when `err()` was set, the call dropped members from `members()`, deleted nothing on the cluster, and raised `BatchError`. The in-memory view no longer matched the cluster.
- **What we do:** call `_check_healthy()` (which checks `_detached` and `_state.error()`) under `_lock` before mutating `_state` (15/N), in both shells.
- **Example:**
  ```python
  with self._lock:
      self._check_healthy()          # all rejections first
      self._state.release(name)      # then mutate
  ```
- **Related:** [LOCK-11](#lock-11) (where the same check runs again once a delete gets its slot), [API-3](api.md#api-3). `release_not_ready` also selects and releases in one lock block to avoid a `members()`-then-release gap ([API-21](api.md#api-21)).

</details>

<a id="lock-5"></a>

## LOCK-5 Flip the "no new work" flag and take the handoff snapshot in one critical section

**Takeaway:** When a handoff records a counter that concurrent workers advance, set the "stop" flag and read the counter in the same critical section, and make the workers check the flag in the same critical section where they advance the counter. Then the recorded value is final.

<details>
<summary>The full story (16/N)</summary>

- **Situation:** `detach()` writes `next_ordinal` (the next free claim number) to the Lease so the next holder never reuses a name. Meanwhile `acquire()` calls on other threads or tasks allocate ordinals.
- **What goes wrong:** if `detach()` read the counter outside the lock that sets `_detached`, an `acquire()` could allocate an ordinal after the read. The Lease would carry a stale mark, and the next holder would reuse that ordinal.
- **What we do:** `SandboxBatch.acquire` calls `_check_healthy()` and `_state.start_acquire()` in one `with self._lock`. `SandboxBatch.detach` sets `_detached = True` and copies `self._state.next_ordinal` into a local in the same critical section (code comment: "acquire() takes no ordinal once the handle is detached, so this is final"). Every Lease write attempt, including the 409 retry, uses that local (16/N `dc9ea439`).
- **Example:**
  ```
  detach:  with _lock: _detached = True; n = next_ordinal   # n = 7
  acquire: with _lock: _check_healthy() -> raises, no ordinal 7 taken
  ```
- **Related:** [LOCK-3](#lock-3). [LEASE-24](lease.md#lease-24) covers the counting side (why the value must be final).

</details>

<a id="lock-6"></a>

## LOCK-6 A snapshot tells you what to look at, not what you own

**Takeaway:** When several workers drain a shared list from snapshots, claim each item with a re-check-and-remove under the lock right before acting on it.

<details>
<summary>The full story (10/N, 17/N)</summary>

- **Situation:** the watch and client cleanup can each snapshot `_stale_connections` (connections that must be closed but never handed out) and close the items at the same time.
- **What goes wrong:** closing straight from a snapshot would double-close a connection another caller already took and may be closing right now.
- **What we do:** for each item in its snapshot, `sandbox_batch.py: _close_connections` takes `_lock`, skips the item if it is no longer in `_stale_connections`, and otherwise removes it before closing. A failed close appends it back through the `keep` callback (`_close_one`). This is the list variant of "remove under lock, act, restore on failure": the keyed cache can pop by key, but the list needs the extra membership check because the snapshot is older than the claim. Test: `test_a_stale_connection_another_caller_took_is_not_closed_again`.
- **Example:**
  ```
  watch:   snapshot [s0, s1]; takes s0; blocks in close(s0)
  cleanup: snapshot [s1]     (s0 already removed) -> closes s1
  watch:   reaches s1: not in list -> skip
  ```
- **Related:** [LOCK-1](#lock-1), [CLEAN-1](clean.md#clean-1). [CLEAN-2](clean.md#clean-2) covers why closes run outside the state lock.

</details>

<a id="lock-7"></a>

## LOCK-7 Re-check teardown and "already done" state before raising a wait timeout

**Takeaway:** When a bounded wait times out, take the state lock and re-check cancellation, teardown, and "goal already met" before you raise the timeout. The world may have changed while you slept.

<details>
<summary>The full story (19/N)</summary>

- **Situation:** `_send_create` waits up to `deadline - time.monotonic()` on the `max_in_flight` semaphore before raising `BatchTimeoutError`.
- **What goes wrong:** `detach()`, `release()`, `err()`, `release_group()`, or the watch seeing an earlier attempt can happen right as the wait expires. Raising `BatchTimeoutError` anyway misreports a teardown or an already-created claim as a slot timeout, which triggers a spurious rollback delete or the wrong exception type.
- **What we do:** after the semaphore timeout, re-acquire `_lock` and check `_can_still_send(can_send)` before raising. If sending is no longer allowed, return cleanly so the caller surfaces the lifecycle `BatchError` or sees the existing member (19/N). Both shells.
- **Example:**
  ```
  t0 create waits on _in_flight (deadline t2)
  t1 release_group() marks the claim released
  t2 wait times out -> with _lock: can_send? no -> return, not BatchTimeoutError
  ```
- **Related:** [LOCK-8](#lock-8), [CANCEL-9](cancel.md#cancel-9). [RACE-6](race.md#race-6) covers what `can_send` decides.

</details>

<a id="lock-8"></a>

## LOCK-8 After optional I/O, re-run the decision under the lock instead of trusting the I/O result

**Takeaway:** If you do some I/O to improve a decision, don't act on what the I/O returned. Go back and re-run the decision under the lock against shared state, and cap how many times one call may retry the optimization.

<details>
<summary>The full story (19/N)</summary>

- **Situation:** `acquire()` may refresh warm credit (a cached count of a warm pool's `readyReplicas`, which lets creates skip pacing) with `_refresh_warm_credit`. By the time the refresh returns, other acquires may already have taken the new credit, or the refresh may have recorded 0 or done nothing.
- **What goes wrong:** using the result directly ("I refreshed, so I'm covered") over-spends credit. Looping until covered would spin GETs against a cold or broken pool.
- **What we do:** the refresh returns nothing. `SandboxBatch.acquire` sets `refreshed = True` and goes back to the top of its `while True` loop, where `take()` and the deadline and pacing checks run again under `_lock` against current state. `not refreshed` in `need_refresh` caps each call at one refresh, after which it falls back to pacing (19/N `2ecdf384`). Async mirrors it.
- **Example:** the refresh records `ready=3` for 5 waiting acquirers. The first 3 to re-enter the lock are covered, the other 2 take paced slots, and no second GET goes out.
- **Related:** [LOCK-16](#lock-16), [LOCK-7](#lock-7).

</details>

<a id="lock-9"></a>

## LOCK-9 Claim the work, charge the budget, and reserve the time slot in one step; keep rate and concurrency as separate limits

**Takeaway:** Pop the work item, spend the budget, and schedule its time in one atomic step. Treat rate (time slots) and concurrency (a semaphore sized to the connection pool) as two independent limits, and size a worker pool to `min(concurrency, work)`.

<details>
<summary>The full story (12/N, 15/N, 19/N)</summary>

- **Situation:** several workers drain one create plan, while `release_*` calls can drop planned entries and warm credit can be spent.
- **What goes wrong:** splitting `popleft`, credit take/spend, and `_reserve_create_slot` across separate lock acquisitions would let two workers spend the same credit, or let a release miss an entry that a worker had popped but not yet reserved.
- **What we do:** in `SandboxBatch._create_worker` one `with self._lock` block checks the stop, `err()`, and empty-plan conditions, pops the entry, calls `take()` (or `spend()` plus a slot reservation), and sets the fill deadline if this was the last entry. `_claim` starts `min(max_in_flight, len(plan))` workers, so a size-0 batch spawns no threads. A separate `BoundedSemaphore(max_in_flight)`, shared with `acquire`, pool reads, and deletes, bounds live HTTP calls (12/N; semaphore 15/N `113fae28`; warm credit 19/N `99a4a93d`).
- **Example:**
  ```python
  with self._lock:
      if stop or err or not plan: return
      entry = plan.popleft()
      covered = credit.take(pool)        # or spend + reserve slot
      if not plan: set_fill_deadline()
  ```
- **Related:** [LOCK-10](#lock-10), [LOCK-13](#lock-13). [RETRY-5](retry.md#retry-5) covers the pacing math.

</details>

<a id="lock-10"></a>

## LOCK-10 Hold a concurrency semaphore only around live I/O, never across a sleep

**Takeaway:** A semaphore that caps concurrent requests should wrap each individual request attempt. Release it before any backoff sleep, or sleeping retries starve active work.

<details>
<summary>The full story (15/N)</summary>

- **Situation:** create and delete loops bound HTTP concurrency with `BoundedSemaphore(max_in_flight)` and retry with backoff.
- **What goes wrong:** holding `max_in_flight` across retry backoff sleeps lets sleeping retries starve active creates and cleanup deletes of HTTP pool slots.
- **What we do:** acquire `_in_flight` inside each individual HTTP attempt, so a retry releases its slot before sleeping (15/N `113fae28`). Both shells.
- **Example:**
  ```python
  for attempt in range(3):
      with self._in_flight:      # slot only for the request
          send()
      sleep(backoff)             # no slot held here
  ```
- **Related:** [LOCK-9](#lock-9), [LOCK-11](#lock-11), [CANCEL-11](cancel.md#cancel-11).

</details>

<a id="lock-11"></a>

## LOCK-11 Get the resource slot first, then join the drain barrier

**Takeaway:** When you combine a resource semaphore with a drain barrier (a counter that teardown waits on), register with the barrier only after you hold the resource. You are counted only while you actually do I/O, and your precondition check is fresh.

<details>
<summary>The full story (15/N)</summary>

- **Situation:** member deletes share the `max_in_flight` semaphore with creates and can queue for a slot. `detach()` and `release()` wait for active deletes before they stop renewal or touch the Lease.
- **What goes wrong:** registering as active before getting a slot would make `detach()` or `release()` wait for deletes that haven't started and may queue for a long time. A health check done before queueing can be stale by the time the slot frees.
- **What we do:** `SandboxBatch._delete_claim` uses `with self._in_flight, self._member_delete():` (async twin in `async_sandbox_batch.py: _delete_claim`). `_member_delete` runs `_check_healthy()` under `_lock` and increments `_active_deletes`. A queued delete is not counted, and it raises `BatchError` if the handle stopped while it queued. On exit the guard is released before the slot. Test: `test_member_deletes_run_in_parallel_and_share_max_in_flight_with_creates`.
- **Example:**
  ```python
  with self._in_flight, self._member_delete():   # slot, then guard
      delete_sandbox_claim(name)
  ```
- **Related:** [LOCK-4](#lock-4), [LOCK-19](#lock-19).

</details>

<a id="lock-12"></a>

## LOCK-12 Broadcast to aggregate waiters, wake per-key waiters per key, and broadcast everything on global changes

**Takeaway:** A shared Condition fits consumers that care about the whole stream. Callers waiting on one key need a per-key Event, installed before the action whose result they wait for and woken only on that key's meaningful transitions. Any global change (a fatal error, a re-list) must still wake every waiter.

<details>
<summary>The full story (16/N)</summary>

- **Situation:** batch fill consumers (`events()`, `wait_for_quorum()`) share one Condition, `_changed`. Each `acquire()` waits for one claim to leave `PENDING`. The watch streams events for the whole batch.
- **What goes wrong:** putting N concurrent `acquire()` calls on `_changed` wakes all N waiters on every watch event for unrelated claims and on intermediate `PENDING` updates, which is quadratic. But if fatal errors or re-lists did not broadcast, per-key waiters would sleep forever.
- **What we do:** each `acquire()` gets an Event, `waiter = self._acquire_waiters[claim_name] = threading.Event()` (async: `asyncio.Event()`), installed in the same `_lock` block that calls `_state.start_acquire()` and takes the ordinal, so it exists before the create POST is sent. `_wait_acquired` clears it under `_lock` before reading state. In `_watch_loop`, a `DELETED` event calls `_wake_acquire` only if the member existed (`mark_lost(...) is not None`). `ADDED`/`MODIFIED` wakes it only when `member.state is not PENDING`. `release_member` calls `_wake_acquire` explicitly for the claim it drops, and `release_group` calls `_notify_all()`. `_changed.notify_all()` still runs on every event for fill consumers. Fatal errors (which set `err()`), re-lists, `detach()`, and `release()` go through `_notify_all()`, which also sets every acquire Event (16/N `810d3773`, `dc9ea439`). Test: `test_watch_events_for_other_claims_do_not_wake_waiting_acquire`.
- **Example:**
  ```
  watch: MODIFIED b-3 (still PENDING) -> notify _changed only
  watch: MODIFIED b-3 (READY)         -> also set _acquire_waiters["b-3"]
  watch: 410 re-list / fatal error    -> _notify_all(): every Event set
  ```
- **Related:** [LOCK-13](#lock-13), [PAT-13](../patterns.md#pat-13). [WATCH-5](watch.md#watch-5) covers how the ordinal allocator advances.

</details>

<a id="lock-13"></a>

## LOCK-13 Wake waiters when the source of their timeout changes, and recompute it every loop

**Takeaway:** If a waiter's timeout comes from shared state that can change (unknown to known, or earlier), notify waiters on that change and recompute the timeout on every loop iteration instead of caching it.

<details>
<summary>The full story (13/N)</summary>

- **Situation:** while creates are still being paced, `_fill_deadline` is `None`, so consumers in `_next` wait on `_changed` with `timeout=None`.
- **What goes wrong:** a waiter that went to sleep with no timeout never wakes at a deadline set later. Group timeouts would fire only if some unrelated watch event happened to arrive.
- **What we do:** `_create_worker` calls `self._changed.notify_all()` right after setting `_fill_deadline`. Every waiter loops and recomputes `max(0.0, deadline - now)` (13/N). Both `sandbox_batch.py` and `async_sandbox_batch.py`.
- **Example:**
  ```
  t0 consumer: deadline None -> wait(timeout=None)
  t1 last create gets its slot: _fill_deadline = t1 + q; notify_all()
  t2 consumer wakes, waits max(0, deadline - now)
  ```
- **Related:** [LOCK-12](#lock-12). [COUNT-12](count.md#count-12) covers how the deadline itself is computed.

</details>

<a id="lock-14"></a>

## LOCK-14 Never hold two lock domains at once; snapshot under one, act under the other

**Takeaway:** When a registry and its entries each have a lock, never hold both. Copy what you need under one lock, release it, then take the other. Lock order can't invert, and slow I/O never runs under the registry lock.

<details>
<summary>The full story (9/N, 10/N, 13/N, 17/N)</summary>

- **Situation:** the sync client has `_batches_lock`, and each handle has `_lock` and `_lifecycle_lock`. Cleanup snapshots the registry and then calls detach, release, or close on each handle.
- **What goes wrong:** holding `_batches_lock` across `detach()`/`release()` would serialize all handles behind network timeouts and block `get_batch`. Nesting it with `batch._lock` in two orders could deadlock (a batch thread holding `_lock` then wanting `_batches_lock`, while cleanup holds `_batches_lock` and wants `_lock`).
- **What we do:** `sandbox_client.py` cleanup copies `list(self._active_batches.items())` (and `_retry_closing_batches` copies `_closing_batches`) under `_batches_lock` and releases it before any batch lock or I/O. `sandbox_batch.py: _close_one` releases `batch._lock` after `keep()` and only then calls `_client._track_closing`, which takes `_batches_lock` alone. `_register_batch` and `_unregister_batch` also call `_track_closing` after leaving their own lock block. `_track_closing` reads `batch._connected` truthiness without the batch lock.
- **Example:**
  ```python
  # _close_one
  with self._lock:
      keep()
  self._client._track_closing(self)   # takes _batches_lock; _lock already released
  ```
- **Related:** [LOCK-1](#lock-1).

</details>

<a id="lock-15"></a>

## LOCK-15 Single-flight futures: shield the shared work, share only real outcomes, cache only success

**Takeaway:** When callers with independent deadlines share one in-flight future, protect it from any one waiter's timeout, broadcast only errors about the shared work (never the leader's own deadline or interrupt), let a surviving waiter take over when the leader aborts, and cache only successes.

<details>
<summary>The full story (16/N, 19/N)</summary>

- **Situation:** the first `acquire()` on an unseen warm pool runs `_check_pool` (WarmPool and Template GETs). Concurrent `acquire()` calls on that pool wait on one shared `Future` in `_prechecks[warmpool]`.
- **What goes wrong:** several ways.
  - In asyncio, passing the shared future straight to `asyncio.wait_for` cancels the shared check when one short-timeout waiter expires, which aborts it for callers with longer deadlines.
  - When an interrupt cancels the task or thread running the check, waiters get `CancelledError` though nobody cancelled them.
  - When the check itself raises `TimeoutError`, it looks like the waiter's own deadline unless you tell them apart.
  - The leader can fail for its own reasons (no `max_in_flight` slot before its deadline, `KeyboardInterrupt`, cancel). Broadcasting that `BatchTimeoutError` or interrupt fails followers with longer deadlines for no reason about the pool.
- **What we do:** async waiters wrap the future in `asyncio.shield`. Both shells check `check.done()` on `TimeoutError`, so a check-level timeout surfaces its real error. In `SandboxBatch._check_pool`, only an `Exception` raised after the leader got its slot (the GETs ran, or the health check failed) goes to followers through `set_exception`. Otherwise the future is cancelled (`if acquired and isinstance(e, Exception): check.set_exception(e) else: check.cancel()`), and an uncancelled follower loops and runs the check itself. `_prechecks[warmpool]` is cleared either way, and only success adds the pool to `_checked_pools` (16/N `810d3773`, `dc9ea439`; 19/N `2ecdf384`). The 19/N change that put the check GETs under `_in_flight` is what created the "leader timed out before its slot" case.
- **Example:**
  ```
  t0 A (timeout 0.05) leads check; B (timeout 600) follows
  t1 A times out waiting for _in_flight -> check.cancel()
  t2 B sees cancelled, not by its own caller -> B runs the check
  ```
- **Related:** [LOCK-16](#lock-16). [TEST-13](../testing.md#test-13) covers the test technique (swapping the module's Future class), and [CANCEL-18](cancel.md#cancel-18) and [CANCEL-20](cancel.md#cancel-20) cover async cancellation details (`task.cancelling()`, stranded followers).

</details>

<a id="lock-16"></a>

## LOCK-16 A per-pool refresh lock coalesces callers only if you re-check after every acquire

**Takeaway:** A mutex can coalesce a shared refresh only if each waiter re-checks "is a refresh still needed?" after acquiring it, and again after every further blocking acquire. Keep the lock order fixed, never hold the state lock while waiting, bound the refresh by the leader's own deadline, and never cache a failure that the leader's deadline caused. To detect that case, ask "did my own deadline pass?" instead of classifying library-specific timeout exceptions.

<details>
<summary>The full story (19/N)</summary>

- **Situation:** concurrent `acquire()` calls on one warm pool share one `_refresh_warm_credit` GET behind a per-pool lock in `_credit_refresh_locks`. A failed GET records `0` credit so callers don't hammer a broken endpoint during the `1s` refresh interval.
- **What goes wrong:**
  - Without a re-check after getting the lock, each waiter sends its own GET in turn as soon as it gets the lock. Checking only before the lock does not coalesce anything.
  - With the fixed `30s` request timeout inside the refresh, a caller with `timeout=0.05` could block for up to `30s`, ignoring its own deadline (inferred).
  - Once the GET was capped to `min(30s, remaining)`, a short-deadline caller timing out at `0.05s` recorded `0` credit and stopped concurrent `timeout=600` callers from refreshing a healthy pool for the next second.
- **What we do:** in `SandboxBatch._refresh_warm_credit` (async mirror too), `remaining_if_due()` (health check, remaining time, `refresh_due`) runs three times: under `_lock` before taking the per-pool lock, after acquiring `refresh_lock`, and after acquiring `_in_flight`. A waiter that gets the lock after the leader recorded fresh credit sees `refresh_due` false (credit > 0, or read less than 1s ago), returns, and takes credit in the acquire loop. Locks are always taken in the order `refresh_lock` then `_in_flight`, and `_lock` is never held across either wait. The GET uses `request_timeout = min(BATCH_REQUEST_TIMEOUT_SECONDS, remaining)`, and in the `except` the refresh returns without recording `0` when `time.monotonic() >= deadline`, so a waiter with time left can refresh at once (19/N `2ecdf384`). The 19/N round-2 walkthrough dropped a rejected alternative that classified the exception (`MaxRetryError.reason`, builtin `TimeoutError` vs urllib3's `TimeoutError`, excluding `NewConnectionError`). The plain deadline check is enough, because a timeout caused by the cap always fires at or after the deadline. Test: the short-deadline subcase of `test_acquires_the_warm_pool_covers_skip_pacing...`.
- **Example:**
  ```
  T1 holds refresh_lock, GETs; T2, T3 block
  T1 records 3, releases
  T2 gets lock: refresh_due=False -> return
  T3 same                      -> 1 GET total
  ```
- **Related:** [LOCK-8](#lock-8), [LOCK-15](#lock-15). [RETRY-18](retry.md#retry-18) also covers the deadline cap and negative-cache rule.

</details>

<a id="lock-17"></a>

## LOCK-17 A per-handle lifecycle lock makes the first of detach() and release() win

**Takeaway:** When two teardown operations on one object conflict over a shared external lock, serialize them with a per-object lock that spans their remote writes, so the first one wins. Scope the lock narrowly for the short operation, and have the short operation refuse once the long one has started.

<details>
<summary>The full story (12/N, review round 3)</summary>

- **Situation:** `detach()` (hand the batch off) and `release()` (delete it) can run at once on one handle: a user call plus client cleanup, or two threads or tasks.
- **What goes wrong:** `release()` takes the Lease under the handle's own holder identity. A concurrent `detach()` read the Lease, saw its own identity, wrote `holder=None`, and let another client attach while `deletecollection` deleted the claims that client had just taken over (round-2 finding N1). `detach()` also reported success for a batch being deleted.
- **What we do:** one lock per handle, `_lifecycle_lock`. `release()` holds it for its whole teardown (since 15/N, sync drops it while it waits for active deletes, see [LOCK-19](#lock-19)). `detach()` takes it only around its Lease read and write, so it never waits minutes behind a release. `release()` clears `_detach_grace`, so `_release_started()` (`_detached and _detach_grace is None`) makes `detach()` raise `BatchError` (`_detach_done`). Outcomes:
  - detach first: handed off, and release raises.
  - release first: deleted, and detach raises.
  - a second release waits, then returns.
  - Side effect approved in round 3: detach after a failed release now raises instead of handing off a half-deleted batch.
  - Rejected: a "releasing" flag checked before the detach write (leaves a check-then-take gap), and separate holder identities for release and renewal (a larger change).
- **Example:**
  ```
  t0 release(): takes _lifecycle_lock; clears _detach_grace; takes Lease
  t1 detach():  waits for _lifecycle_lock
  t2 release(): deletes claims and Lease; unlocks
  t3 detach():  _release_started() -> BatchError (no hand-off of a deleted batch)
  ```
  Tests: `test_the_first_lifecycle_call_wins` (uses a `_SignalingLock`), `test_a_release_during_detach_on_the_same_thread_makes_detach_raise`.
- **Related:** [LOCK-3](#lock-3), [LOCK-18](#lock-18), [LOCK-19](#lock-19). Adding this lock also triggered a stack-wide audit ([PROC-10](../process.md#proc-10)).

</details>

<a id="lock-18"></a>

## LOCK-18 Pick lock reentrancy per concurrency model: RLock for sync teardown, task-owner tracking for async

**Takeaway:** A sync teardown lock that a signal handler or callback can reach on the same thread must be reentrant (`threading.RLock`). Because `asyncio.Lock` has no reentrant variant, any async teardown lock held across an extensible callback (`await sandbox.close_connection()`) must record the owning `asyncio.Task` so same-task re-entry into client cleanup skips re-acquiring the lock instead of deadlocking (`11/N`, `12/N`, `25/N`).

<details>
<summary>The full story (11/N, 12/N, 25/N)</summary>

- **Situation:** in sync code a POSIX signal handler (for example Ctrl-C's `SIGINT`) runs on the main thread between bytecodes, so it can call `detach()` or `release()` while that same thread is already inside one of them holding `_lifecycle_lock`. In async code, signals run as separate loop tasks, *but* `release()` and `detach()` await `sandbox.close_connection()` while holding `batch._lifecycle_lock`, and a custom `AsyncSandbox` subclass can call `await client.close()` or `await client.delete_all()` from inside `close_connection()` on that same task.
- **What goes wrong:** with a plain `threading.Lock`, a same-thread sync re-entry hangs forever. In async, `asyncio.Lock` is non-reentrant: if `close_connection()` calls `client.close()` or `delete_all()`, re-acquiring `batch._lifecycle_lock` on the same task deadlocks immediately, closing `k8s_helper` mid-call breaks the outer `release()` or `detach()`, and waiting on `client._close_lock` while another task's `close()` holds `_close_lock` and waits on `batch._lifecycle_lock` forms a two-task lock-order cycle.
- **What we do:** `SandboxBatch.__init__` sets `_lifecycle_lock = threading.RLock()`: calls from other threads are serialized, and a same-thread re-entry goes through, after which the outer call sees the end state. `AsyncSandboxBatch` uses `asyncio.Lock()` and records `_lifecycle_owner = asyncio.current_task()` inside `release()` and `detach()` (`25/N`): `AsyncSandboxClient.close()` returns early whenever the current task owns `_close_owner` or any tracked batch's `_lifecycle_owner`, and `_release_batches()` skips any batch whose `_lifecycle_owner` is the current task. Plain Lock (hangs) and non-blocking-raise (breaks 11/N) were compared and rejected in `temp-prompts/pr2_round3_decisions.md` section 2 (12/N `522dad4c`).
- **Example:**
  ```
  T1: detach() -> holds RLock -> PUT Lease ...
      SIGINT -> handler: detach() -> RLock re-entered (a Lock would deadlock here)
  ```
  Test: `test_a_detach_during_detach_on_the_same_thread_does_not_hang`.
- **Related:** [LOCK-17](#lock-17), [LOCK-19](#lock-19), [LOCK-20](#lock-20).

</details>

<a id="lock-19"></a>

## LOCK-19 Never wait on a drain barrier while holding a lock the drained work's handler may need

**Takeaway:** Don't block on "wait until active work finishes" while holding a lock that the active work's signal handler or callback must take. Split it: set the "no new work" flag under the lock, drain with the lock released, then lock again and re-check the state.

<details>
<summary>The full story (12/N, 15/N)</summary>

- **Situation:** `release()` must wait for in-flight member deletes before it stops renewal and deletes the Lease. A Ctrl-C handler can run `release()` (or `detach()`) on a thread whose DELETE is in flight.
- **What goes wrong:** with "take the lifecycle lock, then wait" (Ordering 1), sync code has a 5-step ABBA deadlock (two parties each holding what the other needs). T2 holds the RLock and waits for T1's delete. T1's signal handler calls `release()` and blocks on the RLock, which only T2 may re-enter. The BFS model checker found 12 deadlocked states.
- **What we do:** sync `SandboxBatch.release` uses two `_lifecycle_lock` blocks. Phase 1 under the lock: `_check_releasable_locked`, set `_detached`, clear `_detach_grace`, set `_stop_creating`. Then `_wait_active_deletes` runs outside the lock (it already skips the caller's own thread, see [CANCEL-12](cancel.md#cancel-12)). Phase 2 re-takes the lock and runs `_check_releasable_locked` again, because a re-entrant or concurrent release may have finished meanwhile (then it returns). Async `AsyncSandboxBatch.release` keeps the simpler wait-inside-the-lock order, since tasks cannot re-enter on one thread. With Ordering 2 the checker found 0 deadlocks over 5,905 sync states (15/N `113fae28`; notes `handoff/work/bfs_state_machine_guide.md` section 4). Its 3,782 async states model Ordering 2 too, not the shipped async mix (detach waits before the lock, release waits inside it); a later scratch run of that exact mix also found 0 violations ([VER-7](../verification.md#ver-7)).
- **Example:**
  ```
  T1: release_member DELETE in flight
  T2: release(): RLock; wait for T1          <- Ordering 1
  T1: SIGINT -> release(): wait for RLock    => deadlock
  fix: T2 drops the RLock before waiting, re-takes it after
  ```
- **Related:** [LOCK-11](#lock-11), [LOCK-17](#lock-17), [LOCK-18](#lock-18).

</details>

<a id="lock-20"></a>

## LOCK-20 Async getters are plain lock-free defs; sync getters take the lock

**Takeaway:** In asyncio, an invariant over several fields stays atomic as long as no `await` separates the writes, so read-only accessors can be lock-free plain functions. Pin that with a test, so nobody later makes them coroutines or puts an `await` between the writes. In threaded code, every read of a multi-field invariant takes the lock.

<details>
<summary>The full story (1/N, 13/N)</summary>

- **Situation:** snapshot getters (`members()`, `err()`, `lease_degraded()`) and the client's exit-cleanup decision (`_exit_detaches()`) read state that the watch, renewal, and `detach()` write.
- **What goes wrong:** in sync code, `detach()` writes `_detached` and `_detach_grace` as two separate steps. A cleanup thread that reads between them sees a "started release" and wrongly releases a batch that was being handed off. In async code, making the getters coroutines would force `await` onto trivial reads and add suspension points nobody needs.
- **What we do:** `async_sandbox_batch.py: members`, `err`, and `lease_degraded` stay plain `def` without the lock, and `events`/`group_outcomes` run `_check_active` and `set_mode` without it. `async_sandbox_client.py: _release_batches` calls `batch._exit_detaches()` without the batch lock. This is safe because no task can interleave without an `await`, and `detach()` writes both fields in one await-free block, so `_exit_detaches()` never sees half of the update. Test: `test_members_and_err_are_plain_not_coroutine_functions`. Sync `sandbox_batch.py` takes `with self._lock` in the same getters, and `sandbox_client.py: _release_batches` computes `detaches = exiting and batch._exit_detaches()` under `batch._lock` (1/N `3a1607bc`, 13/N `8ad5a407`).
- **Example:**
  ```python
  # async: no await, no lock needed
  def members(self, warmpool=None):
      return self._state.members(warmpool)

  # sync cleanup: the read must not land between detach()'s two writes
  with batch._lock:
      detaches = exiting and batch._exit_detaches()
  ```
- **Related:** [LOCK-1](#lock-1) (the same "no await between check and act" rule for the async registry), [LOCK-3](#lock-3) (the `_detached`/`_detach_grace` encoding), [LOCK-18](#lock-18), [CANCEL-3](cancel.md#cancel-3).

</details>

<a id="lock-21"></a>

## LOCK-21 In single-flight coalescing with heterogeneous caller deadlines, bound every wait and RPC by both `deadline` and Lease expiry and cancel on `BatchError`

**Takeaway:** When concurrent callers with different `timeout` budgets coalesce onto one shared check (`Future`), clamp every slot wait, follower wait, and sequential RPC to `min(step_cap, deadline - now, _lease_expires_at - now)`, re-check shared handle, Lease, and pool health before classifying a timeout, and `cancel()` the shared `Future` on any `BatchError` instead of calling `set_exception(e)` so waiting callers either take over or surface their own batch state.

<details>
<summary>The full story (23/N)</summary>

- **Situation:** The first time `acquire(warmpool, timeout=...)` uses a pool outside `_checked_pools`, `_check_pool` coalesces concurrent callers through `self._prechecks[warmpool]` (`Future`) while one leader acquires a `max_in_flight` slot and calls `_precheck_pools` (`get_sandbox_warmpool` followed by `get_sandbox_template`).
- **What goes wrong:** Three bugs meet when callers bring different `timeout` values alongside a live Lease and `release_group()`:
  1. Snapshotting `lease_remaining` once before `_precheck_pools` or failing to clamp by `deadline - now` and `_lease_expires_at - now` across the `_in_flight` wait and both GETs lets `get_sandbox_warmpool` or `get_sandbox_template` stall up to 30 s past `acquire(timeout=0.12)` or run past `_lease_expires_at` when the first GET consumes the remaining Lease budget or all `max_in_flight` slots are busy.
  2. Publishing `BatchError` (such as `BatchTimeoutError`) via `check.set_exception(e)` poisons concurrent followers waiting on `check`: a leader with `timeout=0.12` fails a concurrent follower that passed `timeout=5.0`.
  3. Having a follower wait `deadline - now` without clamping to `_lease_expires_at - now`, or raising `BatchTimeoutError` directly in `except TimeoutError` without re-acquiring `self._lock` to run `_check_healthy(now)` and `_check_acquire_pool_active(warmpool, acquire_seq)`, blocks followers past Lease expiry or misreports `release_group()` and Lease expiry as `BatchTimeoutError`.
- **What we do:** In `_check_pool` (`sandbox_batch.py`, `async_sandbox_batch.py`), followers compute `wait_timeout = min(deadline, self._lease_expires_at) - now` under `self._lock` after `_check_healthy(now)` and `_check_acquire_pool_active(warmpool, acquire_seq)`, and on `TimeoutError` re-raise a completed check's exception or `continue` back into `self._lock` so health and `release_group()` run before `now >= deadline`. The leader defines `next_timeout()`, which under `self._lock` runs `_check_healthy(now)` and `_check_acquire_pool_active(warmpool, acquire_seq)`, raises `BatchTimeoutError` once `now >= deadline`, and returns `min(BATCH_REQUEST_TIMEOUT_SECONDS, deadline - now, self._lease_expires_at - now)`. Both the `while not acquired:` `_in_flight` wait loop and `_precheck_pools(..., request_timeout=next_timeout)` call `next_timeout` before each step (`_in_flight` acquire, `get_sandbox_warmpool`, and `get_sandbox_template`). In `except BaseException`, `_check_pool` excludes `isinstance(e, BatchError)` from `check.set_exception(e)` and calls `check.cancel()` instead so followers re-evaluate under their own budgets (23/N, `sandbox_batch.py: SandboxBatch._check_pool`, `_precheck_pools`). Pinned against a real loopback TCP socket in `test_pool_check_bounds_reads_by_deadline_and_hands_off_on_timeout` (both sync and async).
- **Example:**
  ```text
  leader (timeout=0.12)   -> GET warmpool (_request_timeout=0.12) -> times out at 0.12s
                          -> check.cancel(), raises BatchTimeoutError
  follower (timeout=5.0)  -> wakes on CancelledError, becomes leader
                          -> GET warmpool + GET template succeed at 0.18s -> returns Member
  ```
- **Related:** [LOCK-15](#lock-15), [LOCK-16](#lock-16); [LIB-9](lib.md#lib-9); [RETRY-18](retry.md#retry-18).

</details>

<a id="lock-22"></a>

## LOCK-22 Never hold `AsyncSandboxClient._lock` across `await close_connection()`, and serialize `client.close()` on `batch._lifecycle_lock` before closing `k8s_helper`

**Takeaway:** Client-level registries (`AsyncSandboxClient._lock`, `SandboxClient._batches_lock`) follow the exact same rule as batch locks ([LOCK-1](#lock-1), [LOCK-2](#lock-2)): snapshot and clear the registry inside the lock, then `await close_connection()` outside it (`F-15`). Before closing `self.k8s_helper` (`aiohttp.ClientSession` / `ApiClient` pools), wait on each batch's `_lifecycle_lock` and read `batch._connected` / `_stale_connections` under `batch._lock` (`F-15`, `F-23`).

<details>
<summary>The full story (Chaos & Verification Suite: F-15 & F-23, async_sandbox_client.py, sandbox_client.py)</summary>

- **Situation:** `AsyncSandboxClient.close()` and `SandboxClient.close()` tear down active single-sandbox connectors, stop/detach registered batches, and close the shared `k8s_helper` HTTP transport.
- **What goes wrong:**
  1. **Holding `self._lock` across `await` (`F-15a`, `async_sandbox_client.py:424-430`):** `AsyncSandboxClient.close()` iterated `self._active_connection_sandboxes` *inside* `async with self._lock:` while awaiting `await sandbox.close_connection()`, starving concurrent `create_sandbox`, `get_sandbox`, `claim_batch`, and `delete_all` callers behind slow connector teardown (caught both statically by `verify_batch_static.py` `INV-LOCK-ASYNC-1` and dynamically by `test_batch_concurrency_stress.py`'s `<50 ms` lock-starvation probe, [TEST-33](../testing.md#test-33)).
  2. **Closing `k8s_helper` underneath in-flight `batch.release()` (`F-15b`, `verify_delete_lifecycle_model.py` Variant E):** Calling `client.close()` concurrently with `batch.release()` allowed `_stop_batches()` to finish and close `k8s_helper`'s `aiohttp.ClientSession` while `batch.release()` was still mid-HTTP `DELETE` under `batch._lifecycle_lock`.
  3. **Unlocked cross-object dict reads (`F-23a`, `sandbox_client.py:451-471`):** Sync `SandboxClient._stop_batches` and `_atexit_cleanup` read `batch._detach_unfinished()` and iterated `batch._connected.values()` and `batch._stale_connections` without `batch._lock`, risking `RuntimeError: dictionary changed size during iteration` when `_watch_loop` mutated `_connected` concurrently.
- **What we do:** Copy and clear `_active_connection_sandboxes` under `self._lock` before awaiting `close_connection()` outside the lock, acquire `batch._lifecycle_lock` before closing `k8s_helper` so in-flight `release()`/`detach()` RPCs finish before their transport closes, and snapshot `_connected` / `_stale_connections` / `_detach_unfinished()` under `batch._lock`.
- **Related:** [LOCK-1](#lock-1), [LOCK-2](#lock-2), [LOCK-19](#lock-19), [VER-6](../verification.md#ver-6), [TEST-33](../testing.md#test-33).

</details>

<a id="lock-23"></a>

## LOCK-23 Read `batch._acquire_waiters` directly instead of maintaining shadow ordinal watermarks, and record fail-fast verdicts inside `_decide_lock`

**Takeaway:** When an external watchdog needs the set of currently waiting `SandboxBatch.acquire()` claims, snapshot `batch._acquire_waiters` under `batch._lock` and probe each active claim via `batch.get_member(claim_name)` (`O(in_flight)`) rather than maintaining shadow ordinal counters, and record `StartVerdict` inside `_decide_lock` before releasing the member (`27/N`).

<details>
<summary>The full story (27/N: failfast.py & batch_claims.py)</summary>

- **Situation:** `BatchWatchdog` (`examples/agent-sandbox-rl/agent_sandbox_rl/failfast.py`) polls pending cold-start batch claims and releases broken claims early so `SandboxBatch.acquire()` wakes immediately with `SandboxStartError` instead of calling `batch.members()` (`O(N log N)` under `batch._lock`, `F-30`).
- **What goes wrong:**
  1. **Shadow ordinal watermarks drift (`0`-indexed vs `1`-indexed and unparsed exits):** Maintaining duplicate `_next_ordinal` / `_completed_ordinals` counters in `BatchWatchdog` started at `1` while `BatchState._next_ordinal` starts at `0` (skipping `<batch_id>-0` on every `size=0` batch), and stalled under continuous load whenever a cancelled `acquire()` exited before its claim name was parsed.
  2. **Post-lock verdict race:** Releasing `_decide_lock` before calling `_record_verdict(claim_name, verdict)` opened a window where a concurrently completing `acquire()` could finish `exit_acquire(claim_name)` and observe `pop_verdict() == None` right before `BatchWatchdog` recorded the verdict and called `batch.release_member(cur)` on the caller's live claim.
- **What we do:** Read `batch._acquire_waiters` directly under `batch._lock` (`O(1)` when idle, `O(in_flight)` under load, zero shadow ordinal state), keep `_decide_lock` strictly downstream of `batch._lock` and upstream of `BatchClaims._lock`, record the verdict inside `if claim_name in self._first_seen_at:` under `_decide_lock`, and release `_decide_lock` before calling `batch.release_member(cur)`.
- **Related:** [LOCK-1](#lock-1), [RACE-1](race.md#race-1), [PERF-1](perf.md#perf-1).

</details>

---

[Learnings home](../../learnings.md) · [Field guide index](../../learnings.md#sec-field-guide)
