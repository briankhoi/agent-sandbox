<a id="sec-cancel"></a>

[Learnings home](../../learnings.md) · [Field guide index](../../learnings.md#sec-field-guide)

# Cancellation, interrupts, and sync/async parity

This section covers what happens when work stops early. That includes Ctrl-C in sync code, `task.cancel()` in async code, a timeout that expires halfway through, and teardown (`detach()`, `release()`, `close()`) racing background workers. The one big idea: an interrupt can land on almost any line, so every exit path must leave shared state consistent and retryable, and every blocking point must re-check whether its work still matters before it does something irreversible. The SDK ships a sync shell (threads) and an async shell (asyncio) over one shared state core, and most entries show the same rule in both shells, plus the few places where the two concurrency models force a deliberate difference. Read the first seven entries as general Python rules. The middle entries apply them to teardown and deadlines, and the last entries cover single-flight checks, adapters, and how parity is kept.

<a id="cancel-1"></a>

## CANCEL-1 KeyboardInterrupt and CancelledError skip `except Exception`

**Takeaway:** Put restore-on-failure logic in `finally`, keyed on a success flag, not in `except Exception`. Sync Ctrl-C and async cancellation both raise exceptions that `except Exception` never sees.

<details>
<summary>The full story (connection put-back; 10/N, 74cd8fa5)</summary>

- **Situation:** Batch cleanup takes a cached connection out of the cache, closes it, and puts it back if the close fails, so a later cleanup can retry it.
- **What goes wrong:** The sync put-back ran only for `Exception`. In Python, `KeyboardInterrupt` (what Ctrl-C raises) and `asyncio.CancelledError` (what `task.cancel()` raises, since Python 3.8) derive from `BaseException`, not `Exception`. A Ctrl-C during the close skipped the put-back, and the connection was lost for good.
- **What we do:** Track success in a flag and restore in `finally`. Async uses the same shape for `CancelledError`.
- **Example:**
  ```python
  closed = False
  try:
      closed = close()
  finally:
      if not closed:
          put_back()
  ```
- **Related:** [CANCEL-2](#cancel-2), [CANCEL-3](#cancel-3), [CANCEL-7](#cancel-7), [CLEAN-1](clean.md#clean-1), [CLEAN-15](clean.md#clean-15), [BP-29](../best-practices.md#bp-29).

</details>

<a id="cancel-2"></a>

## CANCEL-2 Keep CancelledError out of retry and degrade classification

**Takeaway:** In async retry or "mark degraded" loops, re-raise `CancelledError` before any broad handler, and use `asyncio.timeout` so a deadline turns into a catchable `TimeoutError` that you can tell apart from a real cancel.

<details>
<summary>The full story (1/N..19/N)</summary>

- **Situation:** The async loops retry transient errors, mark Lease renewal degraded, or log and continue on `Exception`.
- **What goes wrong:** If a loop treated a cancellation as transient, a cancelled watch or renewal would retry forever, or a normal stop would mark the Lease degraded. If a timeout surfaced as `CancelledError`, it would skip the "my deadline cut this read short" logic.
- **What we do:** `async_sandbox_batch.py: _watch_loop`, `_renew_once`, `_delete_claim`, `_create_one`, `_refresh_warm_credit`, `_drop_acquired`, and `_delete_released_create` each have `except asyncio.CancelledError: raise` before `except Exception`. On 3.8+ this is technically redundant, but it documents intent and survives a later refactor to `except BaseException`. `asyncio.timeout` converts only its own expiry into `TimeoutError` (an `Exception`), so `_refresh_warm_credit`, which wraps `get_sandbox_warmpool` in `asyncio.timeout(request_timeout)`, still sees its deadline, while an outside cancel propagates. Why that GET has an `asyncio.timeout` on top of the request timeout is inferred: it bounds the whole awaited call, not just the transport.
- **Example:**
  ```python
  try:
      await self._renew_once()
  except asyncio.CancelledError:
      raise
  except Exception:
      mark_degraded()
  ```
- **Related:** [CANCEL-1](#cancel-1), [CANCEL-16](#cancel-16), [BP-29](../best-practices.md#bp-29), [LIB-5](lib.md#lib-5).

</details>

<a id="cancel-3"></a>

## CANCEL-3 In asyncio, the safest critical section has no await

**Takeaway:** In asyncio, code between two awaits cannot be interrupted, so a state change with no await is atomic. Don't take a lock just to make one dict operation "safe", because awaiting the lock is itself a point where a second cancel can land.

<details>
<summary>The full story (connection put-back; 10/N, 74cd8fa5)</summary>

- **Situation:** The async put-back did `async with lock` in a `finally` after a cancelled close.
- **What goes wrong:** A task can be cancelled again while it waits for the lock. The second `CancelledError` raises out of the `async with` before the put-back runs, so the connection is lost.
- **What we do:** Do the put-back with no await. A plain `dict.setdefault` runs to completion because asyncio switches tasks only at an await.
- **Example:**
  ```text
  t0: close() is cancelled -> finally starts
  t1: async with lock -> lock busy, task suspends
  t2: second cancel -> CancelledError at t1, put-back never runs
  fix: self._connected.setdefault(name, conn)   # no await, can't be skipped
  ```
- **Related:** [CANCEL-4](#cancel-4), [CANCEL-5](#cancel-5), [LOCK-20](lock.md#lock-20), [CLEAN-1](clean.md#clean-1).

</details>

<a id="cancel-4"></a>

## CANCEL-4 Decrement async completion counters in `finally` with no await, and signal with an Event

**Takeaway:** In asyncio, decrement "operation finished" counters and wake waiters in a `finally` with no await. Prefer `asyncio.Event` (setting it needs no lock) over `asyncio.Condition` (notifying needs the lock) when the signal comes from a cleanup path.

<details>
<summary>The full story (15/N, 113fae28)</summary>

- **Situation:** Each member delete increments an active-delete counter. `detach()`, `release()`, and client `close()` wait for it to drain to zero before they stop renewal or write the Lease.
- **What goes wrong:** If the async `finally` did `async with self._lock` before decrementing, a second cancel arriving while the task waits for the lock would skip the decrement. The counter would never reach zero and teardown would hang. The BFS model checker (a tool that explores every interleaving of a small state machine) found a 6-step trace with 284 livestuck states (notes `bfs_state_machine_guide.md`, "Flaw D").
- **What we do:** `async_sandbox_batch.py: AsyncSandboxBatch._member_delete` decrements `_active_deletes` and sets `_no_active_deletes` synchronously in `finally`. `_wait_active_deletes` waits on that Event. `asyncio.Condition.notify_all()` raises `RuntimeError` unless the lock is held, which would force an await into the `finally`, so the async shell uses an Event instead. The sync shell (`sandbox_batch.py: SandboxBatch._member_delete`) keeps a `Counter` under `_lock` plus the shared Condition, because a thread can't be cancelled mid-acquire the way a task can.
- **Example:**
  ```python
  finally:
      self._active_deletes -= 1  # no await, can't be skipped
      if self._active_deletes == 0:
          self._no_active_deletes.set()
  ```
- **Related:** [CANCEL-3](#cancel-3), [CANCEL-12](#cancel-12), [PAT-16](../patterns.md#pat-16).

</details>

<a id="cancel-5"></a>

## CANCEL-5 Two async cleanup paths still await a lock, and only an unenforced invariant keeps them safe

**Takeaway:** In asyncio cleanup paths, apply the state transition before any await. If you keep `async with lock` there, write down the invariant that makes the acquire non-suspending (nobody holds the lock across an await) and protect it, because one new await under the lock reopens the hole.

<details>
<summary>The full story (14/N 3b57b191, 16/N dc9ea439)</summary>

- **Situation:** Two cancel handlers take the Condition's lock before their state change, because `Condition.notify_all()` needs the lock. `async_sandbox_batch.py: AsyncSandboxBatch.wait_for_quorum` does `except BaseException: async with self._changed: fail_quorum(...); notify_all()`. `AsyncSandboxBatch.acquire` does `except BaseException: await self._drop_acquired(...)`, and `_drop_acquired` starts with `async with self._changed:` before calling `BatchState.drop_acquired`.
- **What goes wrong:** If that acquire ever suspends, a second cancel (for example from a TaskGroup, asyncio's structured group of tasks, layered with a timeout) raises before the state change. For the quorum, it stays undecided and holds its members forever ([CANCEL-7](#cancel-7)). For acquire, the name stays in `_acquiring` (so `has_pool` stays true), the Event stays in `_acquire_waiters`, and no pending delete is recorded for a claim that may exist.
- **What we do:** Today the window is closed by an invariant, not by code. An AST scan of `async_sandbox_batch.py` at 23/N finds that the only await under `_lock` or `_changed` is the `Condition.wait` in `_next`, which releases the lock while waiting. So no task ever suspends while holding `_lock`, the lock is always free when another task runs, and `asyncio.Lock.acquire` takes its no-yield fast path. Nothing enforces this, so the safety is inferred. Hardening options, not implemented: call the pure `BatchState` method first with no lock (it's Sans-I/O, meaning no I/O, locks, or timers, so it's atomic in asyncio), then notify on a best-effort basis or through per-waiter Events that need no lock; or retry the cleanup in a loop that tolerates `CancelledError`. Compare the connection put-back ([CANCEL-3](#cancel-3)) and the drain counter ([CANCEL-4](#cancel-4)), which already avoid the await.
- **Example:**
  ```python
  except BaseException:
      self._state.fail_quorum(err)   # hardening: transition first, no await
      async with self._changed:      # wakeup only; skipping it no longer strands members
          self._changed.notify_all()
      raise
  ```
- **Related:** [CANCEL-3](#cancel-3), [CANCEL-4](#cancel-4), [CANCEL-6](#cancel-6), [CANCEL-7](#cancel-7), [LOCK-20](lock.md#lock-20).

</details>

<a id="cancel-6"></a>

## CANCEL-6 Cancellation cleanup should match what the caller can still reach

**Takeaway:** "Cancellation-safe" means nothing is lost and a retry works, not "do everything anyway". If the cancelled call created something it never returned, undo it (at least in local state) before re-raising. If everything stays reachable and retryable, don't block the cancel with network cleanup.

<details>
<summary>The full story (delete_all review; 16/N dc9ea439)</summary>

- **Situation:** Two calls get cancelled in practice. `delete_all()` releases every registered batch. `acquire()` creates one claim and waits for it to become Ready.
- **What goes wrong:** A reviewer asked to run `delete_all()`'s batch cleanup in a `finally` on cancellation. That would run network calls with up to 90 s timeouts after the caller asked to stop. For `acquire()`, the opposite risk applies: the caller never received the `Member`, so it has no handle to release it. Without a rollback, an orphan claim would sit in `members()` (or land later) holding a warm sandbox until `release()`, shutdownTime, or Lease garbage collection.
- **What we do:** `delete_all()` doesn't force cleanup. Nothing becomes unreachable: the batches stay registered, so the next `close()` or context exit stops them. `acquire()` (both shells: `SandboxBatch.acquire`, `AsyncSandboxBatch.acquire`) catches `BaseException`, calls `_drop_acquired`, and re-raises. The design (`batch_claim_proposal.md`) says: "If it raises, including on cancellation, it deletes the claim it created." The state drop (tombstone plus pending delete) always runs. The DELETE is best effort: failures are logged and left pending, and deletes after `err()` or detach are skipped. An interrupt during pacing sends neither a create nor a delete, because `may_exist` is still False.
- **Example:**
  ```text
  delete_all() cancelled -> batches still registered -> next close() releases them
  acquire() cancelled after POST -> drop member + DELETE by name -> re-raise
  acquire() cancelled while pacing -> no POST sent, nothing to delete
  ```
- **Related:** [CANCEL-5](#cancel-5), [CANCEL-7](#cancel-7), [CLEAN-15](clean.md#clean-15), [CLEAN-16](clean.md#clean-16).

</details>

<a id="cancel-7"></a>

## CANCEL-7 A one-shot API must reach a terminal decision on every exit, including interrupts

**Takeaway:** If an API can be called only once and holds resources until its caller reads a decision, every exit path, including `BaseException` (sync interrupts and async cancellation), must move the shared state to a final decision. Otherwise the held resources stay stranded with no API left to release them.

<details>
<summary>The full story (14/N, 3b57b191)</summary>

- **Situation:** `wait_for_quorum()` can be called only once per handle, because handing the same cohort out twice would break at-most-once delivery. While it waits, the batch holds Ready members for it, and those members are held back from `events()` too: called after `wait_for_quorum()`, `events()` yields only the members the quorum doesn't return, once the quorum is reached, and none if it fails.
- **What goes wrong:** If a cancel, a `KeyboardInterrupt`, or `err()` ends the wait while the quorum is undecided, nobody can ever receive the held members (a second call isn't allowed), `events()` keeps waiting on them, and creates keep running for a barrier nobody awaits. Async callers hit this from Ctrl-C too, because `asyncio.run` turns Ctrl-C into a cancellation of the main task, so the sync shell sees `KeyboardInterrupt` where the async shell sees `CancelledError`.
- **What we do:** `sandbox_batch.py: SandboxBatch.wait_for_quorum` and `async_sandbox_batch.py: AsyncSandboxBatch.wait_for_quorum` wrap the wait in `except BaseException:`. The handler takes the lock, calls `fail_quorum(BatchTimeoutError("wait_for_quorum() ended before the quorum was reached"))`, runs `notify_all`, and re-raises. `batch_state.py: BatchState.fail_quorum` marks every held member resolved, so `events()` sees a decided (failed) quorum and stops waiting for them. Through `group_failed`, this also stops the remaining creates. If the watch reached the quorum between the end of the wait and the handler taking the lock, `fail_quorum` leaves that quorum reached. An `err()` ends the wait as a `BatchError` chained from `err()`, so it goes through the same path. Each shell has its own test, keyed to its own interrupt type: `test_any_exception_that_ends_the_wait_fails_the_quorum` (sync), `test_a_cancelled_wait_fails_the_quorum` (async).
- **Example:**
  ```text
  t0: wait_for_quorum() blocks in _changed.wait
  t1: KeyboardInterrupt -> quorum failed, notify_all, re-raise
  t2: b-0 turns Ready -> not yielded; b-1 is never created
  ```
- **Related:** [CANCEL-1](#cancel-1), [CANCEL-5](#cancel-5), [COUNT-10](count.md#count-10).

</details>

<a id="cancel-8"></a>

## CANCEL-8 Every background sleep is a timed wait on the stop event

**Takeaway:** In background workers, write every sleep as a timed wait on the shutdown event and treat waking up as a cancellation check. Teardown stays prompt, and tests get one seam to control time.

<details>
<summary>The full story (12/N, 522dad4c)</summary>

- **Situation:** Create workers can sleep up to 30 s in retry backoff, or a long time waiting for a far pacing slot (about ceil(N/rps) seconds for the last create).
- **What goes wrong:** With `time.sleep`, `release()` and `detach()` would wait for every sleeping worker, and each worker would then send a create after teardown began.
- **What we do:** Every wait is `if self._stop_creating.wait(delay): return` in `sandbox_batch.py: _create_worker` and `_create_one`. The async shell uses `AsyncSandboxBatch._wait_for_stop`, a wrapper around `asyncio.wait_for(Event.wait(), timeout)` that returns at once when `timeout <= 0`. The workers re-check their predicates after waking. Tests replace this wait to gate timing exactly. One exception: the sync `_delete_claim` retry backoff, which a create worker can reach through `_delete_released_create`, is a plain `time.sleep` after a `_check_lease_held()` check.
- **Example:**
  ```python
  if self._stop_creating.wait(max(0.0, slot - time.monotonic())):
      return  # teardown started while we slept
  ```
- **Related:** [CANCEL-9](#cancel-9), [CANCEL-15](#cancel-15), [PAT-15](../patterns.md#pat-15).

</details>

<a id="cancel-9"></a>

## CANCEL-9 When an optimization moves where requests wait, re-audit cancellation at the new spot

**Takeaway:** If a change moves the waiting from an interruptible sleep to a semaphore or queue, add a re-check under the state lock at that new blocking point, right before the irreversible action, and mark the action started in the same critical section.

<details>
<summary>The full story (19/N, a2a19cfa, 2ecdf384)</summary>

- **Situation:** Before warm credit (using a warm pool's `readyReplicas` to skip pacing), creates waited mostly in the pacing sleep, which the stop event interrupts ([CANCEL-8](#cancel-8)). Warm-credit-covered creates now get `slot=now` and queue on the `max_in_flight` semaphore instead.
- **What goes wrong:** A create that was released while blocked on the semaphore would send its POST as soon as it got a slot. The 19/N walkthrough notes this check ("Layer A") was "needed by 19/N anyway because warm credit queues creates on `_in_flight`".
- **What we do:** `sandbox_batch.py: SandboxBatch._send_create` (and the async mirror) checks `_can_still_send(can_send)` under `_lock` at three points: before waiting on the semaphore, right after acquiring it (where `on_send()` marks the attempt "sent" in the same critical section as the final check), and on timeout before raising. `_can_still_send` combines `_stop_creating`, `err()`, and the per-claim `can_send`.
- **Example:**
  ```text
  t0: covered create queues on _in_flight (no pacing sleep)
  t1: release_not_ready() releases the claim
  t2: slot frees -> re-check under _lock -> released -> skip POST
  ```
- **Related:** [CANCEL-8](#cancel-8), [CANCEL-14](#cancel-14), [RETRY-15](retry.md#retry-15).

</details>

<a id="cancel-10"></a>

## CANCEL-10 Fence a worker you can't join with stop checks before every visible write

**Takeaway:** If you can't promptly interrupt a blocking worker, don't join it on shutdown. Make it check a stop flag right before every externally visible change, so a late return does nothing.

<details>
<summary>The full story (1/N stop checks, 12/N creators stopped before the watch)</summary>

- **Situation:** The sync watch thread can sit in a blocking socket read for up to 35 s (or 90 s during a re-list) when `detach()` or `release()` wants it gone. Python threads can't be cancelled from outside.
- **What goes wrong:** Joining it would stall `detach()`/`release()` for up to the request timeout. Not joining it means a late wakeup could mark every member LOST from a post-stop re-list, or set `err()` from an error the shutdown itself caused, after the handle detached or another handle took the batch.
- **What we do:** `sandbox_batch.py: SandboxBatch._stop_background_threads` joins the creators and the renew thread but only sets `_watch_stop` for the daemon watch thread. `_watch_loop` re-checks the flag wherever it would publish: after a re-list returns and before `resync_from_list`, before applying each event (`break`), before `note_error`, and in the short-watch branch. The async shell can cancel the watch task and does await it (`AsyncSandboxBatch._stop_background_tasks`). Creators stop (and are joined with a grace) before the watch stops, so the watch keeps observing claims from creates that land during the creator grace (inferred from the ordering). Tests: `test_410_relist_after_stop_set_does_not_touch_state`, `test_error_after_stop_set_leaves_err_none`.
- **Example:**
  ```text
  t0: release() sets _watch_stop and moves on
  t1: 20 s later the watch thread's list() returns
  t2: sees stop -> return (no LOST marking, err() stays None)
  ```
- **Related:** [CANCEL-8](#cancel-8), [CANCEL-24](#cancel-24), [CLEAN-19](clean.md#clean-19).

</details>

<a id="cancel-11"></a>

## CANCEL-11 Fence each network attempt, not the whole retry loop

**Takeaway:** Wrap each request, not the whole retry loop, in the "active operation" fence that teardown waits on, and re-check the fence before each backoff sleep. Teardown then waits only for requests actually on the wire.

<details>
<summary>The full story (15/N)</summary>

- **Situation:** `release_group` shares the deletecollection round loop (`sandbox_batch.py: _delete_claims_in_rounds`) with `release()`, but it runs while the handle stays live, so `detach()` or `release()` can start mid-loop.
- **What goes wrong:** Holding the active-delete guard across the whole loop would make `detach()` wait through backoffs of up to 30 s. With no guard, a round could keep deleting after the handle detached or `err()` was set.
- **What we do:** `SandboxBatch.release_group` passes `guard=self._member_delete`. The guard wraps each individual request (the deletecollection, then the count), so each one re-checks health and counts as active only while on the wire. An empty `with guard(): pass` before the sleep fails fast once the handle stops. `release()` passes no guard, because it set `_detached` itself, and uses `hold=take` for ownership instead. Test: `test_lifecycle_calls_wait_for_active_deletes_and_stop_further_deletes` (no count with the group selector after detach).
- **Example:**
  ```text
  round k: [guard: deletecollection] [guard: count] [guard: pass] sleep
  detach() sets _detached during the sleep
  next guard -> BatchError, loop ends
  ```
- **Related:** [CANCEL-12](#cancel-12), [CANCEL-4](#cancel-4), [PAT-19](../patterns.md#pat-19), [LOCK-10](lock.md#lock-10).

</details>

<a id="cancel-12"></a>

## CANCEL-12 Sync drain barriers need per-thread counters, an early exit, and targeted wakeups

**Takeaway:** A sync drain barrier that a signal handler may enter must count active operations per thread and leave out the calling thread. It should also wake when the operation it guards becomes moot, and notify only when a waiter can exist.

<details>
<summary>The full story (15/N, 113fae28)</summary>

- **Situation:** `detach()` and `release()` wait for in-flight member and group deletes (`_wait_active_deletes`) before they stop Lease renewal or give up the Lease. In sync code, a SIGINT or SIGTERM handler (a function Python runs on the main thread between bytecodes when a signal arrives) or a re-entrant callback can run on the very thread that is inside `_member_delete()` and call `batch.release()` or `batch.detach()`. The lifecycle lock is an `RLock` (a lock the same thread can take again), so it lets that nested call in.
- **What goes wrong:** With a plain integer counter, that thread waits forever for itself. Two smaller issues: if T2 is in `detach()` waiting for T1's delete, and T1's signal handler settles the outcome first with a nested `detach()` or `release()`, T2 keeps waiting on a decision that is already final. And notifying the shared `_changed` Condition on every finished delete would wake every fill consumer for nothing.
- **What we do:** `sandbox_batch.py: SandboxBatch._member_delete` counts in `Counter[int]` keyed by `threading.get_ident()`. `_wait_active_deletes` loops while `self._active_deletes.total() > self._active_deletes[tid]`, so it waits for every other thread's deletes but not its own. With `for_detach=True`, it returns early once `_release_started()` or `_lease_released` holds, and the paths that set those flags notify (`release` when it starts and on `BatchInUseError`, `detach` after setting `_lease_released`). `_member_delete`'s `finally` calls `notify_all` only `if self._detached`, because only teardown waits on the count. The docstring states the early exit; why it was added is inferred, and the tests assert the detach wakes before the delete returns (subtest "same-thread signal-handler reentry does not deadlock"). The async shell has no `for_detach`: its deletes always make progress, and `detach()` re-checks `_detach_done()` after the wait ([CANCEL-4](#cancel-4) covers the async counter).
- **Example:**
  ```text
  T1: _member_delete -> count[T1]=1 -> blocked in HTTP
  T1 (signal handler): release() -> waits while total() > count[T1] -> 1 > 1 false -> proceeds
  ```
- **Related:** [CANCEL-4](#cancel-4), [CANCEL-11](#cancel-11), [CANCEL-13](#cancel-13), [PAT-16](../patterns.md#pat-16), [LOCK-18](lock.md#lock-18), [LOCK-19](lock.md#lock-19).

</details>

<a id="cancel-13"></a>

## CANCEL-13 Under reentrancy, re-validate state after every blocking call, including in error handlers

**Takeaway:** Once a lock is reentrant (or signal handlers can run), every blocking call inside it is a point where the state can change under you. Re-check the goal and preconditions after each blocking call and in exception handlers, before writing or raising.

<details>
<summary>The full story (12/N 522dad4c, plus later review rounds)</summary>

- **Situation:** Because the RLock lets a same-thread signal handler re-enter, a nested `release()` or `detach()` can finish while the outer `detach()` is blocked inside `read_batch_lease` or `replace_batch_lease`.
- **What goes wrong:** Without re-checks, the outer `detach()` writes a stale Lease over the nested call's result (including over a Lease that a nested `release()` already re-took), raises a transport error even though the nested detach already handed the batch off, or marks the Lease released after a nested `release()` started deleting the batch.
- **What we do:** `sandbox_batch.py: SandboxBatch.detach` re-reads `_detach_done()` under `_lock` after `_wait_active_deletes`, on entering the lifecycle block, before each `replace_batch_lease` attempt, in the `except Exception` handler, and before setting `_lease_released`. It returns if the Lease is already released and raises `BatchError` if a release started. In the handler, if the nested call finished the job, the outer call returns quietly instead of raising. `AsyncSandboxBatch.detach` has none of these in-loop checks: its lifecycle lock serializes tasks, and nothing re-enters a coroutine mid-await. Tests: `test_a_detach_during_detach_on_the_same_thread_does_not_hang`, `test_a_release_during_detach_on_the_same_thread_makes_detach_raise`.
- **Example:**
  ```text
  outer detach: PUT Lease
    [signal handler: detach() completes]
  outer PUT raises ECONNRESET -> _detach_done() is True -> return quietly
  ```
- **Related:** [CANCEL-12](#cancel-12), [CANCEL-24](#cancel-24), [LOCK-18](lock.md#lock-18), [LOCK-17](lock.md#lock-17).

</details>

<a id="cancel-14"></a>

## CANCEL-14 One absolute deadline, computed once, bounds every phase

**Takeaway:** Turn a caller's timeout into one absolute monotonic deadline at entry and pass it down. Recompute the time left in every phase, and treat "no time left" as "don't start", never as "clamp to 0 and try anyway".

<details>
<summary>The full story (16/N dc9ea439, 19/N 2ecdf384, a2a19cfa)</summary>

- **Situation:** `acquire(timeout)` has up to six blocking phases: the first-use pool check (or waiting on another caller's check), the per-pool refresh lock, a `max_in_flight` slot, the pacing slot, retry backoff plus a retry slot, and the wait for Ready.
- **What goes wrong:** If each phase took its own `timeout`, one call could block for several times the caller's timeout. If a phase used `max(0, remaining)`, a free slot could still let a doomed create go out after the deadline.
- **What we do:** `acquire` computes `deadline = monotonic() + wait` once, before any I/O. `_check_pool`, `_refresh_warm_credit`, `_send_create`, and `_wait_acquired` (both shells) recompute `remaining = deadline - monotonic()`, and `_check_retry_deadline` checks the retry's pacing slot against `deadline`. Phases skip the wait entirely when `remaining <= 0`: `_send_create` checks `(wait is not None and wait <= 0) or not acquire(timeout=wait)`, and `_check_pool` checks `if remaining > 0`. This replaced an earlier `max(0.0, deadline - now)`, which still took a free slot after the deadline. Tests: `test_fill_and_acquire_creates_share_max_in_flight`, `test_an_acquire_waiting_on_another_acquires_pool_check_stops_at_its_timeout`.
- **Example:**
  ```text
  timeout=0.5
  t0: pool check takes 0.3 s
  t1: slot wait takes 0.15 s
  t2: Ready wait gets the remaining 0.05 s, not another 0.5 s
  ```
- **Related:** [CANCEL-15](#cancel-15), [CANCEL-19](#cancel-19), [RETRY-7](retry.md#retry-7), [BP-26](../best-practices.md#bp-26).

</details>

<a id="cancel-15"></a>

## CANCEL-15 A timeout of zero or less doesn't mean "fail": guard expired waits yourself

**Takeaway:** Never pass a raw `deadline - now` to a blocking primitive. Some APIs treat -1 as "wait forever", most still succeed at timeout 0 when the resource is free, and asyncio fast paths never yield to a timeout. Check for `<= 0` first, and skip building a timeout wrapper around an already expired wait.

<details>
<summary>The full story (1/N..19/N; 19/N 2ecdf384 for the send guard)</summary>

- **Situation:** Deadlines are computed as `deadline - time.monotonic()` and can be negative by the time they're used.
- **What goes wrong:** `threading.Lock.acquire(timeout=-1)` means wait forever, and other negative timeouts raise `ValueError`, so an expired deadline could turn into an unbounded wait. `threading.Semaphore.acquire(timeout=0)` (or negative) still succeeds when a slot is free, and `asyncio.Semaphore.acquire` takes its no-await fast path before `asyncio.timeout` can fire, so a caller past its deadline would still send a create it then has to roll back. In asyncio, `wait_for` with a non-positive timeout still wraps and cancels the inner awaitable, a wasted step on hot paths such as warm-credit-covered creates.
- **What we do:** The sync shell clamps each deadline-based `Event.wait` and `join` to `max(0.0, ...)` or checks `remaining <= 0` first (for example `_stop_creating.wait(max(0.0, slot - time.monotonic()))`, `creator.join(max(0.0, deadline - time.monotonic()))`) and guards each lock or semaphore acquire: `_refresh_warm_credit` returns if `remaining <= 0` before `refresh_lock.acquire(timeout=remaining)`, `_check_pool` checks `remaining > 0` before `_in_flight.acquire`, and `_send_create` short-circuits `wait <= 0`. The async `_send_create` enters `asyncio.timeout(wait)` only `if wait is None or wait > 0`, and `_wait_for_stop` returns the stop state at once when `timeout <= 0`. In both shells, the expired case re-checks `_can_still_send` first, so a teardown surfaces as a teardown and not as a timeout. The rationale is inferred from the code; no note states it.
- **Example:**
  ```python
  refresh_lock.acquire(timeout=deadline - now)  # -1.0 => blocks forever
  if remaining <= 0 or not lock.acquire(timeout=remaining):  # safe
      ...
  ```
- **Related:** [CANCEL-14](#cancel-14), [CANCEL-16](#cancel-16).

</details>

<a id="cancel-16"></a>

## CANCEL-16 An asyncio bounded-acquire idiom that never leaks a slot

**Takeaway:** In asyncio, bound a lock or semaphore acquire with `asyncio.timeout` plus `suppress(TimeoutError)` and an `acquired` flag set on the line right after the await. Release only when the flag is set, and let `asyncio.timeout` (not a bare `except CancelledError`) tell its own expiry from an outside cancel.

<details>
<summary>The full story (19/N, 2ecdf384)</summary>

- **Situation:** `asyncio.Lock` and `asyncio.Semaphore` have no `acquire(timeout=)`, unlike threading's.
- **What goes wrong:** Wrapping `acquire()` in `wait_for` or `timeout` can, in principle, lose a slot when a release and the timeout fire in the same event-loop tick. Swallowing `CancelledError` to handle the timeout would also hide an external `task.cancel()`.
- **What we do:** `async_sandbox_batch.py: AsyncSandboxBatch._check_pool` and `_refresh_warm_credit` use the pattern below. The 19/N adversarial audit checked CPython 3.11 to 3.13: an uncontended acquire completes without yielding, and a contended acquire hit by a same-tick release and timeout restores the semaphore value or wakes the next lock waiter, so no slot leaks. An external cancel still propagates, because `asyncio.timeout` converts only its own cancellation (it compares `uncancel()` with the cancel count it saw) into `TimeoutError`.
- **Example:**
  ```python
  acquired = False
  try:
      with contextlib.suppress(TimeoutError):
          async with asyncio.timeout(remaining):
              await sem.acquire()
              acquired = True
      ...
  finally:
      if acquired:
          sem.release()
  ```
- **Related:** [CANCEL-2](#cancel-2), [CANCEL-15](#cancel-15).

</details>

<a id="cancel-17"></a>

## CANCEL-17 `suppress(CancelledError)` around awaiting a cancelled child also swallows your own cancel

**Takeaway:** When you await a child task you just cancelled, swallow only the child's `CancelledError`. Check whether the current task is itself being cancelled (`cancelling()` on 3.11+) and re-raise if so.

<details>
<summary>The full story (1/N 3a1607bc, 12/N 522dad4c; inferred, no code or note addresses it)</summary>

- **Situation:** After cancelling its watch, renewal, and straggler creator tasks, `detach()`, `release()`, or `close()` awaits each one inside `contextlib.suppress(asyncio.CancelledError)` (`async_sandbox_batch.py: AsyncSandboxBatch._stop_background_tasks`, verified in code).
- **What goes wrong:** If the caller's own task is cancelled while it awaits a child, the cancel is forwarded to the awaited child, and the `CancelledError` that reaches the caller is suppressed. Teardown keeps running and ignores the caller's cancel (for example, a TaskGroup shutdown). On 3.11+, `task.cancelling()` (the count of pending cancel requests on the current task) stays above 0, so the information is there but unused.
- **What we do:** The code suppresses unconditionally today. A tighter pattern re-raises when `asyncio.current_task().cancelling()` is non-zero, or awaits with `asyncio.wait([task])`, which never raises the child's `CancelledError` into the caller.
- **Example:**
  ```python
  task.cancel()
  try:
      await task
  except asyncio.CancelledError:
      if asyncio.current_task().cancelling():
          raise
  ```
- **Related:** [CANCEL-18](#cancel-18), [BP-29](../best-practices.md#bp-29).

</details>

<a id="cancel-18"></a>

## CANCEL-18 A single-flight follower checks `task.cancelling()` before taking over

**Takeaway:** When a shared future is cancelled as a "leader gave up" signal, a follower must check `current_task().cancelling()` before retrying, so it never swallows its own cancellation. Attach a done-callback that retrieves the exception of futures nobody may await.

<details>
<summary>The full story (16/N dc9ea439, 810d3773; 19/N 2ecdf384)</summary>

- **Situation:** The first `acquire()` on an unseen warm pool runs `_check_pool` as the leader, and concurrent callers on that pool wait on one shared future in `_prechecks` (single-flight: one in-flight check shared by all callers). Async followers wait through `asyncio.shield(check)`, which protects the shared future from a follower's own cancel. When the leader is interrupted, it cancels the future so a follower takes over.
- **What goes wrong:** Through `shield`, an async follower gets `CancelledError` both when the leader cancels the shared future and when the follower's own task is cancelled (for example, a TaskGroup cancelling siblings together). Always looping would swallow a real cancel and start a fresh check. Always re-raising would fail innocent followers when only the leader died. Separately, a failed check that no follower retrieves logs "Future exception was never retrieved".
- **What we do:** `async_sandbox_batch.py: AsyncSandboxBatch._check_pool` loops only `if check.cancelled() and task is not None and not task.cancelling()`, and otherwise re-raises. A done-callback `lambda d: d.cancelled() or d.exception()` marks the exception as retrieved. `sandbox_batch.py: SandboxBatch._check_pool` loops on any `concurrent.futures.CancelledError`, because nothing outside can cancel a thread, so it always means the leader cancelled. Its bare `concurrent.futures.Future` never calls `set_running_or_notify_cancel`, so the leader's `cancel()` always succeeds. On 3.11+ (the SDK floor), both `TimeoutError` types alias the built-in, so one `except TimeoutError` works in both shells. Test: `test_an_interrupted_pool_check_is_run_again_by_a_waiting_acquire` (both shells; async cancels first and second together "as a TaskGroup cancels its tasks").
- **Example:**
  ```text
  TaskGroup cancels A (leader) and B; C is untouched
  -> B re-raises (cancelling() > 0)
  -> C sees check.cancelled() with cancelling() == 0 -> runs the check
  -> 2 GETs total
  ```
- **Related:** [CANCEL-17](#cancel-17), [CANCEL-19](#cancel-19), [CANCEL-20](#cancel-20), [LOCK-15](lock.md#lock-15), [PAT-12](../patterns.md#pat-12), [LIB-18](lib.md#lib-18).

</details>

<a id="cancel-19"></a>

## CANCEL-19 Takeover loops re-check the deadline, and a timeout on a just-cancelled future is a timeout

**Takeaway:** A takeover loop must re-check the caller's deadline before it starts new work. When a wait times out on a future that was cancelled at the same moment, report a timeout, not the cancellation.

<details>
<summary>The full story (16/N, 810d3773)</summary>

- **Situation:** A follower's wait can expire at the exact moment the leader cancels the shared future.
- **What goes wrong:** The follower could see `TimeoutError` with `check.cancelled()` true, and calling `check.result()` would leak a `CancelledError`. Or it could see `CancelledError` and loop into leading a brand-new check after its own deadline.
- **What we do:** In `sandbox_batch.py: SandboxBatch._check_pool`, the loop starts with `if time.monotonic() >= deadline: raise BatchTimeoutError`. The `TimeoutError` branch raises `BatchTimeoutError(...) from None` unless the future finished normally (`if not check.done() or check.cancelled()`). Tests inject both orderings deterministically with `_RaceFuture` / `race_wait_for` ("pool-c" `TimeoutError`, "pool-e" `CancelledError`) and assert one GET per pool (review replies, item 2).
- **Example:**
  ```text
  t0: follower waits, deadline at t1
  t1: leader cancels the future AND follower's wait times out
  -> BatchTimeoutError, no second check started
  ```
- **Related:** [CANCEL-14](#cancel-14), [CANCEL-18](#cancel-18), [CANCEL-20](#cancel-20), [LOCK-15](lock.md#lock-15).

</details>

<a id="cancel-20"></a>

## CANCEL-20 Complete a shared future before you unregister it

**Takeaway:** Cancel or resolve a shared future before, or atomically with, removing it from the registry. Bounded waits on followers turn an interrupt in that gap into a timeout instead of a hang.

<details>
<summary>The full story (16/N, 19/N; inferred)</summary>

- **Situation:** The sync check leader's failure handler (`sandbox_batch.py: SandboxBatch._check_pool`, `except BaseException`) first does `with self._lock: del self._prechecks[warmpool]`, then calls `check.set_exception(e)` or `check.cancel()`. That's two steps (verified in code).
- **What goes wrong:** A `KeyboardInterrupt` delivered between the two steps leaves the future pending forever. Followers already waiting on it see no result and fail with `BatchTimeoutError` at their own deadline instead of taking over. The audit (P2-SDK-2) first worried about a `KeyError` from a non-identity delete. At 23/N each path has one delete, so only the stranding remains, and each follower's deadline bounds it.
- **What we do:** Nothing completes the future in a `finally` today. Followers are protected only by their bounded `check.result(timeout=...)` and the `not check.done()` -> `BatchTimeoutError` branch ([CANCEL-19](#cancel-19)). A fix would cancel or resolve the future first, then unregister it.
- **Example:**
  ```text
  leader: del _prechecks[pool]
          <- KeyboardInterrupt here
          check.cancel()   # never runs
  followers: wait until their own deadline -> BatchTimeoutError
  ```
- **Related:** [CANCEL-18](#cancel-18), [CANCEL-19](#cancel-19), [LOCK-15](lock.md#lock-15).

</details>

<a id="cancel-21"></a>

## CANCEL-21 Hold the lock per item inside generator helpers, never across a yield

**Takeaway:** Take locks per item inside generator helpers and release them before yielding. When you time out an asyncio `Condition.wait`, rely on its documented re-acquire-on-cancel so the code after the timeout still holds the lock.

<details>
<summary>The full story (1/N 3a1607bc, 13/N 8ad5a407)</summary>

- **Situation:** `events()` and `group_outcomes()` are generators that wait on the shared Condition but must also wake at the fill deadline to expire pending members.
- **What goes wrong:** If a timed-out async `Condition.wait` left the lock released, the next loop iteration would read and change state without the lock. Holding the lock across a `yield` would block the watch and every other consumer while the caller processes an item, or forever if the caller abandons the iterator.
- **What we do:** `async_sandbox_batch.py: AsyncSandboxBatch._next` runs inside `async with self._changed:` and does `with suppress(asyncio.TimeoutError): await asyncio.wait_for(self._changed.wait(), timeout)`. `asyncio.Condition.wait()` re-acquires its lock even when `wait_for`'s timeout cancels it. The sync `_next` uses `self._changed.wait(timeout)`, which just returns False with the lock held. In both shells, `_iterate` yields only after `_next` returns and has released the lock.
- **Example:**
  ```python
  async def _iterate(self, pop, done):
      while (item := await self._next(pop, done)) is not None:
          yield item   # lock already released
  ```
- **Related:** [CANCEL-5](#cancel-5), [PAT-14](../patterns.md#pat-14).

</details>

<a id="cancel-22"></a>

## CANCEL-22 Normalize edge-case differences between sync and async client libraries in the adapter

**Takeaway:** Sync and async client libraries that look alike can return different types on edge cases. Normalize them in your adapter layer so both shells see the same event stream.

<details>
<summary>The full story (12/N, 522dad4c)</summary>

- **Situation:** A proxy or a truncated stream can deliver a watch line that isn't valid JSON.
- **What goes wrong:** The sync `kubernetes` library's `Watch.unmarshal_event` returns `None` on a `JSONDecodeError`, so the line is dropped. `kubernetes_asyncio` returns the raw `str`. The async watch loop called `event.get` on that string, hit an unexpected exception, and stopped the watch with `err()` set, a divergence between shells (12/N commit message). Together with streams that end at once, this also fed a busy reconnect loop (about 200 requests in a few ms in a unit test), which 12/N fixed with short-watch backoff.
- **What we do:** `async_k8s_helper.py: AsyncK8sHelper.watch_sandbox_claims` yields only events that are dicts (`if isinstance(event, dict): yield event`), and `k8s_helper.py: K8sHelper.watch_sandbox_claims` drops `None` (`if event is not None`). Both shells now skip garbage lines, and a stream of only garbage backs off instead of looping. Test: `test_async_k8s_helper.test_watch_sandbox_claims_skips_none_and_undecodable_events`.
- **Example:**
  ```python
  async for event in stream:
      if isinstance(event, dict):  # kubernetes_asyncio may yield a raw str
          yield event
  ```
- **Related:** [CANCEL-24](#cancel-24), [LIB-15](lib.md#lib-15), [RETRY-3](retry.md#retry-3).

</details>

<a id="cancel-23"></a>

## CANCEL-23 Bounded fan-out: size thread pools to the real limit, gather coroutines freely

**Takeaway:** For bounded fan-out, size a thread pool to the real concurrency limit, while coroutines can be unbounded behind a semaphore. Collect every outcome before raising, so one failure doesn't hide others or abandon in-flight work.

<details>
<summary>The full story (15/N, 113fae28)</summary>

- **Situation:** `release_not_ready` deletes many claims and must attempt every one, then raise the first error.
- **What goes wrong:** Sequential deletes are slow for large batches. More threads than `max_in_flight` would just block on the shared semaphore while costing OS threads. A plain `gather` would stop collecting at the first failure and leave the other errors unretrieved.
- **What we do:** `sandbox_batch.py: SandboxBatch.release_not_ready` uses `ThreadPoolExecutor(min(len(names), self._max_in_flight))`, lets the context manager join all futures, and reads `f.exception()` (`__init__` keeps `_max_in_flight` only for this). `async_sandbox_batch.py: AsyncSandboxBatch.release_not_ready` starts one coroutine per name with `asyncio.gather(..., return_exceptions=True)`. In both, the shared `_in_flight` semaphore inside `_delete_claim` sets the actual concurrency bound. Tests: `test_member_deletes_run_in_parallel_and_share_max_in_flight_with_creates` (both shells).
- **Example:**
  ```python
  results = await asyncio.gather(*(self._delete_claim(n) for n in names), return_exceptions=True)
  first = next((r for r in results if isinstance(r, BaseException)), None)
  ```
- **Related:** [CANCEL-24](#cancel-24), [API-22](api.md#api-22), [LOCK-9](lock.md#lock-9).

</details>

<a id="cancel-24"></a>

## CANCEL-24 Keep sync/async parity with mirrored tests, and allow differences only for concurrency-model hazards

**Takeaway:** Keep parallel sync and async APIs honest with mirrored test names, so a diff of the two name lists shows missing coverage. Allow a behavior difference only when the concurrency model itself forces it, and write down why.

<details>
<summary>The full story (all batch PRs; the coroutine check is from 1/N)</summary>

- **Situation:** AGENTS.md requires sync/async parity, and both shells call the same Sans-I/O `BatchState` core. Fixes still land in shell code (guards, ordering), so one shell can drift.
- **What goes wrong:** Without mirrored tests, a fix to one shell can be missing from the other, and only a reviewer would notice.
- **What we do:** `test_sandbox_batch.py` and `test_async_sandbox_batch.py` mirror test names across shells so a diff of the two name lists shows missing coverage, with unique tests on each side covering shell-specific hazards: same-thread signal re-entrancy, registry-lock races, and daemon threads on the sync side; task cancellation, two-phase `__aexit__` / `close()`, atexit with the sync helper, and task shutdown on the async side. `test_members_and_err_are_plain_not_coroutine_functions` asserts that async accessors doing no I/O stay plain functions. The deliberate differences across the two shells are:
  - Client close vs. terminate: `AsyncSandboxClient` exposes non-terminating `await client.close()` (local connections, batch tasks, and `aiohttp` session only) and runs `try: _delete_automatic_sandboxes() finally: close()` in `__aexit__()`, requiring `_release_attempted` retention and a writer-preferring `_batch_op` gate; `SandboxClient` has no `close()` and only terminates or detaches in `__exit__()` and `delete_all()` ([CLEAN-29](clean.md#clean-29)).
  - Reentrancy: sync faces same-thread `SIGINT` re-entrancy (`threading.RLock`, per-thread `tid` delete/create counters, re-checks after Lease RPCs; [CANCEL-12](#cancel-12), [CANCEL-13](#cancel-13), [LOCK-18](lock.md#lock-18)); async faces same-task callback re-entrancy when `close_connection()` calls `client.close()` or `delete_all()` under non-reentrant `asyncio.Lock` (`_lifecycle_owner` and `_close_owner`; [LOCK-18](lock.md#lock-18)).
  - Cancellation vs. blocking syscalls: async tasks can be cancelled at any `await`, requiring await-free `finally` decrements/put-backs, `asyncio.shield()`, and `task.cancelling()` checks ([CANCEL-3](#cancel-3), [CANCEL-4](#cancel-4), [CANCEL-18](#cancel-18)); sync threads cannot be cancelled externally, so `Watch.stop()` shuts down the `urllib3` socket on `<v37` and workers fence writes with stop flags ([CANCEL-10](#cancel-10), [WATCH-10](watch.md#watch-10)).
  - Process exit (`atexit`): sync reuses `K8sHelper` directly; async cannot run `asyncio` or `aiohttp` during interpreter shutdown and falls back to `_close_for_atexit()` plus a temporary sync `K8sHelper` ([CLEAN-12](clean.md#clean-12)).
  - Transport and K8s client libraries (`kubernetes` + `urllib3` vs. `kubernetes_asyncio` + `aiohttp`): `urllib3` retries 3x and sleeps on `Retry-After` by default (`_UnretriedApiClient` in sync only) and opens unpooled one-off sockets when full, while `aiohttp` never retries, blocks callers when its pool is full (starving Lease renewals without `BATCH_POOL_HEADROOM`), and skips HTTP status checks on `_preload_content=False` ([CANCEL-22](#cancel-22), [LIB-6](lib.md#lib-6), [LIB-13](lib.md#lib-13), [LIB-14](lib.md#lib-14), [LIB-23](lib.md#lib-23)).
  - Fan-out and completion signals: sync sizes `ThreadPoolExecutor` to `max_in_flight` and uses `Condition`; async gathers coroutines behind a `Semaphore` and signals drain barriers with `Event` ([CANCEL-4](#cancel-4), [CANCEL-23](#cancel-23)).
- **Example:**
  ```bash
  diff <(grep -o 'def test_[a-z0-9_]*' test_sandbox_batch.py | sort) \
       <(grep -o 'def test_[a-z0-9_]*' test_async_sandbox_batch.py | sort)
  ```
- **Related:** [CANCEL-12](#cancel-12), [CANCEL-13](#cancel-13), [CANCEL-18](#cancel-18), [CANCEL-22](#cancel-22), [CANCEL-23](#cancel-23), [TEST-24](../testing.md#test-24), [LOCK-20](lock.md#lock-20), [LOCK-18](lock.md#lock-18).

</details>

<a id="cancel-25"></a>

## CANCEL-25 Attach thread-pool cancellation cleanup to `concurrent.futures.Future` (never the wrapper `asyncio.Future`), and catch `BaseException` on every slot rollback

**Takeaway:** Cancelling `asyncio.wrap_future(executor.submit(fn))` cancels only the `asyncio.Future` view while the OS worker thread keeps running `fn` to completion (`F-05`). Attach post-cancel resource reclamation via `cf_future.add_done_callback(...)` on the underlying `concurrent.futures.Future`, and guard every reservation counter or handle rollback in sync code with `except BaseException:` rather than `except Exception:` (`F-06`).

<details>
<summary>The full story (Chaos & Verification Suite: F-05 & F-06, async_fleet.py & fleet.py)</summary>

- **Situation:** `AsyncSandboxFleet` (`examples/agent-sandbox-rl/agent_sandbox_rl/async_fleet.py`) delegates blocking sync operations (`self._sync.acquire(...)`) to a `ThreadPoolExecutor` via `_to_thread`, while `SandboxFleet` (`fleet.py`) reserves `_claims_reserved` slots and `_handles` entries across multi-step setup, release, resume, and restore calls.
- **What goes wrong:**
  1. **Orphaned worker-thread `SandboxHandle` on `asyncio` cancel (`F-05`, `async_fleet.py:179-182`):** When a caller cancelled `await asyncio.wait_for(async_fleet.acquire(...), timeout=0.05)`, the `asyncio.Future` raised `CancelledError` immediately while the pool thread finished `self._sync.acquire(...)` seconds later, permanently leaking a live `SandboxHandle`, `BatchClaim`, and `_claims_reserved` slot. Subtle trap: attaching `add_done_callback` to the *`asyncio.Future`* does not work because `asyncio.Future` transitions to `cancelled` immediately before the worker thread finishes!
  2. **Five `except Exception:` rollback gaps on `SIGINT` (`F-06`, `fleet.py:1473,1567,1737,1852,1869`):** `acquire`, `_acquire_reserved`, `release`, `resume`, and `restore` caught `except Exception:` to roll back `_claims_reserved -= 1`, `batch_claim.release()`, or `_handles` re-insertion, so a `KeyboardInterrupt` (`BaseException`) arriving mid-RPC permanently leaked capacity slots or dropped live handles from `teardown()`.
- **What we do:** Keep a reference to the raw `cf_fut = self._thread_pool().submit(...)`; if the awaiting coroutine exits via `BaseException` while `not cf_fut.cancel()`, set an `abandoned = True` flag and register `cf_fut.add_done_callback(...)` to invoke `handle.release()` as soon as the worker thread produces a `SandboxHandle`. Across `fleet.py`, widen every resource-rollback `except Exception:` to `except BaseException:` (enforced statically by `verify_batch_static.py` `Check 5`).
- **Related:** [CANCEL-1](#cancel-1), [CANCEL-6](#cancel-6), [CLEAN-25](clean.md#clean-25), [CHAOS-9](../chaos-verification-suite.md#chaos-9).

</details>

---

[Learnings home](../../learnings.md) · [Field guide index](../../learnings.md#sec-field-guide)
