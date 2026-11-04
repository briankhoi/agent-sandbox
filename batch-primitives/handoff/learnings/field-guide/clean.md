<a id="sec-clean"></a>

[Learnings home](../../learnings.md) · [Field guide index](../../learnings.md#sec-field-guide)

# Cleanup and resource ownership

This section covers who owns each resource the batch SDK creates (cached connections, background threads and tasks, claims, the Lease) and how each one gets cleaned up, both on the happy path and when something fails halfway. One idea ties the entries together: at every moment, each resource has exactly one owner that is responsible for cleaning it up. It is never zero owners (a leak) and never two (a double close or a wrong delete). The early entries are general patterns for in-process cleanup (locks, `finally`, registries). The later ones apply the same idea to cluster objects, shutdown order, and exit hooks. Read the **Takeaway** lines first, and open "The full story" when you want the details.

<a id="clean-1"></a>

## CLEAN-1 Take a resource out under the lock, act on it, and put it back if the action fails

**Takeaway:** A resource should always be either owned by exactly one closer or still registered, never neither. Remove it from the shared structure under the lock before you act, and restore it in a `finally` if the action didn't succeed.

<details>
<summary>The full story (#2049, 9/N, 10/N)</summary>

- **Situation:** Client cleanup and `detach()` close a batch's cached connections. A connection can hold a port-forward subprocess, so it really needs closing.
- **What goes wrong:** Two first attempts failed in opposite ways.
  - Clear-then-close: cleanup cleared the cache, then closed each connection. A close that raised or was cancelled lost its connection for good. Nothing could retry it. That broke the client's own documented promise that a connection that fails to close stays tracked for a retry.
  - Keep-until-closed: the next attempt left each entry in the cache until its close succeeded. Two cleanups running at once (`detach()` and the client's `close()`, or two threads) could then close the same connection twice.
- **What we do:** Take one entry out of the cache under the lock, so exactly one caller owns it. Close it outside the lock. Put it back only if the close did not succeed. `sandbox_batch.py: SandboxBatch._close_one` (and the async twin) does this with a success flag: `closed = False; try: close(); closed = True ... finally: if not closed: keep()`. The `finally` (not `except Exception`) matters, because Ctrl-C (`KeyboardInterrupt`) and asyncio cancellation (`CancelledError`) are not `Exception` subclasses.
- **Example:**
  ```python
  closed = False
  try:
      sandbox.close_connection()
      closed = True
  finally:
      if not closed:
          with self._lock:
              keep()  # put it back for the next cleanup
  ```
- **Related:** [CLEAN-3](#clean-3), [CLEAN-6](#clean-6). [CANCEL-1](cancel.md#cancel-1) covers the `finally` shape, and [CANCEL-5](cancel.md#cancel-5) the no-await put-back.

</details>

<a id="clean-2"></a>

## CLEAN-2 Decide under the lock, do slow I/O outside it, and claim each item right before acting

**Takeaway:** Make the decision under the lock, do the slow work outside it, and re-claim each item under the lock just before acting on it. A concurrent cleaner then never touches the same item, and nobody waits behind slow I/O.

<details>
<summary>The full story (9/N, 10/N, 17/N)</summary>

- **Situation:** The watch (the background stream of claim changes) decides under `_lock` that a cached connection is stale. Closing that connection can block, for example while it stops a port-forward subprocess. Client cleanup may try to close the same stale list at the same time.
- **What goes wrong:** Closing under `_lock` blocks `members()`, `connect()`, and every consumer behind slow I/O. Closing straight from a list snapshot without re-checking lets the watch and cleanup close the same connection twice. A close that fails or is cancelled must not lose the connection.
- **What we do:** `SandboxBatch._pop_stale_connections` (under `_lock`) moves stale entries into `_stale_connections` and returns them. Then `SandboxBatch._close_connections` loops: for each one it takes `_lock`, skips it if it is no longer in `_stale_connections` (another caller took it), removes it to claim ownership, then calls `_close_one` outside the lock. If the close fails, `_close_one` puts it back with `keep()` and calls `_track_closing`. The async shell has the same methods.
- **Example:**
  ```python
  for sandbox in connected:
      with self._lock:
          if sandbox not in self._stale_connections:
              continue  # someone else owns it now
          self._stale_connections.remove(sandbox)
      self._close_one(sandbox, action, partial(self._stale_connections.append, sandbox))
  ```
  Tests: `test_a_stale_connection_another_caller_took_is_not_closed_again`, `test_a_stale_connection_that_fails_to_close_is_retried_by_the_next_client_cleanup`, `test_a_watch_cancelled_while_closing_stale_connections_keeps_them_for_cleanup`.
- **Related:** [CLEAN-1](#clean-1), [CLEAN-3](#clean-3), [LOCK-6](lock.md#lock-6).

</details>

<a id="clean-3"></a>

## CLEAN-3 Give "unusable but still owed cleanup" its own place, and move items there in the same lock as the state change

**Takeaway:** If an object must be both unusable and still owed cleanup, put it somewhere readers never look. Record that move in the same critical section as the state change that made it unusable, so an interruption can't leave it untracked and unclosed.

<details>
<summary>The full story (transport round, 10/N, 17/N)</summary>

- **Situation:** The watch drops a cached connection when its member is lost or moves to a different pod, then closes it. Each watch event, and each 410 re-list (a full re-read of the claim list after the watch's position expired), can make cached connections stale.
- **What goes wrong:** A failed or cancelled close dropped the connection. Putting it back into the main cache `_connected` would let `connect()` hand out a stale connection. Closing straight out of `_connected` without first recording the item somewhere would lose it if the watch task is cancelled midway.
- **What we do:** A separate `_stale_connections` list that `connect()` never reads. Later cleanups close whatever is in it. In the same `_lock` section that applies the event (or `resync_from_list` after a 410), `_pop_stale_connections` pops each stale entry from `_connected` and appends it to `_stale_connections`. After the lock is released, `_close_connections` closes the returned list. If the watch task is cancelled mid-close, the remaining items are still in `_stale_connections`, and the next client cleanup closes them. Both shells do this.
- **Example:**
  ```python
  with self._lock:
      self._state.upsert_claim(obj)
      stale = self._pop_stale_connections([claim_name])
  self._close_connections(stale, "a watch event")  # a cancel here leaves the rest in _stale_connections
  ```
- **Related:** [CLEAN-2](#clean-2), [CLEAN-9](#clean-9), [WATCH-9](watch.md#watch-9). [WATCH-8](watch.md#watch-8) covers when a connection counts as stale.

</details>

<a id="clean-4"></a>

## CLEAN-4 A failure in one cleanup step must not skip the others

**Takeaway:** When cleanup has several independent steps, run the ones that must always happen in `finally`, not in sequence. Layer scope-exit cleanup as best-effort semantic teardown followed by an unconditional transport shutdown.

<details>
<summary>The full story (#2049, 9/N, 13/N)</summary>

- **Situation:** Client cleanup stops a batch's background work, then closes its cached connections (which hold port-forward subprocesses). One level up, leaving `async with AsyncSandboxClient()` releases claimed batches and detaches attached ones (`_release_batches(exiting=True)`), and both can fail on network errors.
- **What goes wrong:** If the stop raised, the close never ran, and the port-forwards leaked. At the client level, a failure in batch release would skip `close()`, leaving batch tasks renewing their Leases forever and the API session open.
- **What we do:** `async_sandbox_client.py: AsyncSandboxClient._stop_batches` closes connections in a `finally` after the stop ("A failed stop must not leave port-forwards open"). `AsyncSandboxClient.__aexit__` runs `try: await self._delete_automatic_sandboxes() finally: await self.close()` (a shape that predates the batch stack). `close()` then stops anything still registered, which includes handles whose release failed, since those stay registered. The sync `SandboxClient.__exit__` only runs the cleanup, because the sync client has no `close()`.
- **Example:**
  ```python
  try:
      await self._delete_automatic_sandboxes()  # may fail on the network
  finally:
      await self.close()  # always stops tasks and the session
  ```
- **Related:** [CLEAN-5](#clean-5), [CLEAN-10](#clean-10).

</details>

<a id="clean-5"></a>

## CLEAN-5 A local cleanup failure must not block a distributed handoff

**Takeaway:** Log local cleanup failures and retry them later. Don't let them abort a cluster-state change, such as a lease handoff, that other parties are waiting on.

<details>
<summary>The full story (1/N, 9/N, 10/N)</summary>

- **Situation:** `detach()` and `release()` close cached connections before they write or delete the Lease (the Kubernetes object that says who holds the batch).
- **What goes wrong:** If a close error propagated, one stuck port-forward would block the Lease handoff or the batch delete. The handoff would never happen, or the claims would stay until their expiry.
- **What we do:** `SandboxBatch._close_one` (both shells) catches `Exception`, logs a warning with the action name, and puts the connection back for a later retry. `detach()` and `release()` then go on to the Lease write. `KeyboardInterrupt` and `CancelledError` still propagate, but only after the put-back in `finally`. The test `test_close_connection_failure_still_releases_lease` (sync and async) pins this.
- **Related:** [CLEAN-1](#clean-1), [CLEAN-4](#clean-4).

</details>

<a id="clean-6"></a>

## CLEAN-6 "Empty right now" is not "done": a put-back must re-track its owner

**Takeaway:** When you decide to drop something from a snapshot ("the cache is empty, so unregister"), check what an in-flight operation can add back afterwards. A put-back must also re-register whatever holds it.

<details>
<summary>The full story (10/N)</summary>

- **Situation:** Two cleanups can run on the same handle at once.
- **What goes wrong:** Cleanup A took the last connection and was closing it. Cleanup B saw an empty cache and unregistered the batch. A's close failed and put the connection back on a handle nobody tracked anymore, so it would never be closed.
- **What we do:** A put-back also tells the client to keep tracking the handle: `_close_one` calls `self._client._track_closing(self)` after `keep()`. `SandboxClient._track_closing` keeps a handle that still has `_connected` or `_stale_connections` and is no longer the live one in a separate closing set ([CLEAN-7](#clean-7)).
- **Example:**
  ```
  t0  A takes conn X, starts closing (cache now empty)
  t1  B sees empty cache, unregisters the handle
  t2  A's close fails, puts X back, calls _track_closing -> handle goes into _closing_batches
  t3  next client cleanup closes X
  ```
- **Related:** [CLEAN-1](#clean-1), [CLEAN-7](#clean-7).

</details>

<a id="clean-7"></a>

## CLEAN-7 Keep "the live owner" separate from "leftovers still to clean up"

**Takeaway:** A registry that holds one live owner per key should not also hold leftovers. Mixing them forces either data loss (overwrite) or destructive actions on the wrong object, so put leftovers in a second structure that only gets the safe cleanup.

<details>
<summary>The full story (10/N)</summary>

- **Situation:** The client registry `_active_batches` holds one handle per batch (keyed by namespace and batch id). After a `detach()` whose connection close failed, re-attaching with `get_batch` replaced the old handle, and its leftover connection became unreachable.
- **What goes wrong:** Keeping both handles in the main registry was unsafe. Cleanup calls `release()` on registered handles, and `release()` deletes the batch's claims by label without checking who holds the Lease (at that point in the stack).
- **What we do:** A second small set, `_closing_batches`, holds finished handles that only need their connections closed. Cleanup (`_retry_closing_batches`) only closes their connections and never calls `release()` or stop on them. The main registry stays one live handle per batch. `SandboxClient._register_batch` sends a replaced handle to `_track_closing`.
- **Related:** [CLEAN-6](#clean-6), [CLEAN-8](#clean-8). [LOCK-1](lock.md#lock-1) covers the identity-checked unregister, and [LOCK-14](lock.md#lock-14) the registry lock.

</details>

<a id="clean-8"></a>

## CLEAN-8 Each resource gets exactly one owning registry

**Takeaway:** Give each resource one owning registry, the one whose teardown semantics match how the resource was acquired. Outer cleanup should delegate to that owner instead of tracking the resource a second time.

<details>
<summary>The full story (1/N, 9/N)</summary>

- **Situation:** The client tracks standalone sandboxes in `_active_connection_sandboxes`, and its cleanup deletes those claims by name.
- **What goes wrong:** If batch-connected `Sandbox` objects were registered there too, the client's `delete_all()` or exit cleanup would delete individual batch claims by name, bypassing the Lease and handoff rules, and each connection would have two owners.
- **What we do:** `SandboxBatch.connect()` caches only in `batch._connected`. Client cleanup reaches batch connections through each batch handle (`_close_cached_connections`) and through `_closing_batches`, never through the per-sandbox registry. The test `test_connected_handle_not_registered_in_active_connection_sandboxes` pins this.
- **Related:** [CLEAN-7](#clean-7), [CLEAN-9](#clean-9).

</details>

<a id="clean-9"></a>

## CLEAN-9 Only the path that updates routing state may evict a cached connection

**Takeaway:** When a cache pairs a connection with routing data the watch maintains (like a pod IP), let only the code that updates that routing data evict the entry. An out-of-band close should mark the cached handle closed in place, not empty the slot, so nothing re-seeds it with stale data.

<details>
<summary>The full story (24/N)</summary>

- **Situation:** A standalone `Sandbox.terminate()` calls `_on_terminated` to evict the handle from `SandboxClient._active_connection_sandboxes` (`7b1a9fbc`). `SandboxBatch.connect()` instead caches `(sandbox, pod_ip)` in `batch._connected[claim_name]`, seeded with the pod IP the watch saw.
- **What goes wrong:** Suppose `_on_terminated` popped `batch._connected[claim_name]` when the caller ran `sandbox.terminate()` directly. Until the watch sees the claim's deletion, `batch._state` still marks the member `READY` while `batch._connected` is empty. A second `batch.connect(member)` in that window builds a fresh handle seeded with the terminating pod's IP.
- **What we do:** Leave `_on_terminated` unset on batch-connected handles. `batch._connected` keeps the closed `(sandbox, pod_ip)` entry, which fails closed because `is_active == False`, until `batch.release_member()` or the watch marks the member `LOST` and moves the entry to `_stale_connections`.
- **Example:**
  ```
  t0  caller: sandbox.terminate()     (entry stays, is_active False)
  t1  caller: batch.connect(member)   -> sees closed entry, does not re-seed old pod IP
  t2  watch: claim deleted -> member LOST, entry moves to _stale_connections
  ```
- **Related:** [CLEAN-3](#clean-3), [CLEAN-8](#clean-8), [WATCH-8](watch.md#watch-8).

</details>

<a id="clean-10"></a>

## CLEAN-10 Shut down in order: refuse new work, drain in-flight work, stop loops, close local resources, close the transport last

**Takeaway:** Shutting down means refusing new work first, then finishing old work. When you flip a flag that waiters sleep on, wake them. Close the shared transport only after everything that uses it has drained.

<details>
<summary>The full story (cited as PR 3 in the earlier notes, 9/N, 15/N)</summary>

- **Situation:** Client cleanup stops a batch. At the same time, a caller may still hold the handle, and `release_member` or `release_group` deletes may be in flight on other tasks.
- **What goes wrong:** Early cleanup stopped the batch but left it usable. A caller could then call `connect()` and create a connection no cleanup would ever close. On the async client, closing the aiohttp session under an in-flight delete makes that delete fail against a closed session, and a delete that lands after the Lease changes hands could hit claims of a re-attached or recreated batch.
- **What we do:** Cleanup marks the handle detached first, so `connect()` raises. Waiters on the condition variable get notified, or they would hang forever. `async_sandbox_client.py: AsyncSandboxClient._stop_batches` does, under `batch._lock`: set `_detached` (which makes `_member_delete` reject new deletes through `_check_healthy`), set `_stop_creating`, and `_notify_all()`. Then it awaits `_wait_active_deletes()`, then `_stop_background_tasks()`, and closes connections in a `finally`. `close()` calls `k8s_helper.close()` only after `_stop_batches`. `_member_delete` decrements its counter with no await, so a cancellation can't skip the wakeup.
- **Example:**
  ```python
  async with batch._lock:
      batch._detached = True; batch._stop_creating.set(); batch._notify_all()
  try:
      await batch._wait_active_deletes()
      await batch._stop_background_tasks()
  finally:
      await batch._close_cached_connections("client cleanup")
  ```
- **Related:** [CLEAN-4](#clean-4), [CLEAN-17](#clean-17). [CANCEL-12](cancel.md#cancel-12) covers the sync per-thread drain counter.

</details>

<a id="clean-11"></a>

## CLEAN-11 Implicit exit cleanup undoes only what that scope acquired

**Takeaway:** Separate "created by me" from "attached by me". Implicit scope exit should undo only what that scope took, while explicit destructive calls act on everything.

<details>
<summary>The full story (13/N, `7fba1f53`)</summary>

- **Situation:** `get_batch` takes over a batch's Lease. Client exit cleanup used to call `release()` on every registered batch, including ones attached through `get_batch`.
- **What goes wrong:** An inspection or handoff caller that attached with `get_batch` deleted the batch's claims and Lease when its `with` block exited, instead of handing the batch on.
- **What we do:** `get_batch` sets `_attached = True`. `SandboxClient._release_batches(exiting)` detaches attached batches with `detach()`'s default grace and releases claimed ones on exit, while explicit `delete_all()` passes `exiting=False` and releases all of them. Sync exit cleanup reads `_exit_detaches()` under `batch._lock`, because `detach()` sets `_detached` and `_detach_grace` in two writes, and a read between them would look like a started release. A failed detach never falls back to `release()`; the handle stays registered and the next cleanup finishes the detach (`_finish_detach`).
- **Related:** [CLEAN-12](#clean-12), [BP-36](../best-practices.md#bp-36). [LEASE-22](lease.md#lease-22) covers retrying a started release on an attached handle, and [LEASE-20](lease.md#lease-20) `BatchInUseError` ending ownership.

</details>

<a id="clean-12"></a>

## CLEAN-12 Exit hooks: bind static functions to in-place registries, and skip what you can't do safely

**Takeaway:** Never bind `self` into a process-global exit hook on a reusable object. Bind a static helper to registries you only mutate in place. Use loop-independent primitives in the hook, and keep destructive teardown as a plain function of (transport, identity, last-known state) so a different transport can run it when the usual runtime is gone. If the safe action can't run in the hook, do nothing rather than fall back to a destructive one, and leave that state for the Lease-expiry path.

<details>
<summary>The full story (9/N, 12/N, 13/N, 24/N, `ae6bb389`)</summary>

- **Situation:** `atexit` (Python's run-at-interpreter-exit hook list) runs client cleanup at process exit. The hook needs the client's active and closing batches. The async client can't await anything there, because the event loop and the executor may already be shutting down. Claimed async batches and batch port-forwards should still be cleaned up, or they leak until shutdownTime.
- **What goes wrong:**
  - `atexit.register(self._cleanup)` pins `self` for the life of the process unless unregistered, leaking closed `SandboxClient` and `AsyncSandboxClient` instances (`ae6bb389`). Capturing container snapshots at `__init__` misses batches registered later. Rebinding an attribute (`self._active_batches = {}`) detaches the client from the containers the hook holds.
  - A release written only as an `AsyncSandboxBatch` method can't run from the hook. Running the async `release()`, `detach()`, or `close_connection()` there would fail. Running a sync release on an attached or half-detached batch would delete a batch another client may now own.
- **What we do:**
  - 24/N registers a `@staticmethod` through `functools.partial` over the client's in-place containers (`_active_connection_sandboxes`, `_explicit_claims`, `_active_batches`, `_closing_batches`, and sync `_batches_lock`), and mutates them only in place. A test with `gc.disable()` plus `weakref.ref(client)` checks that a cleaned-up client is freed at once by reference counting, while a dropped client with an active batch still cleans up at exit. (In the 23/N code the hooks are still bound methods, `atexit.register(self._delete_automatic_sandboxes)` and `atexit.register(self._atexit_cleanup)`.)
  - 12/N keeps the take, delete, and finish algorithm in the module-level `sandbox_batch.py: _delete_batch_objects(helper, namespace, batch_id, lease_name, holder_identity, lease_duration, last_lease)`, not in a handle method. Its docstring says it is "Also used by `AsyncSandboxClient`'s atexit hook, which can't use the event loop".
  - `async_sandbox_client.py: AsyncSandboxClient._atexit_cleanup` first closes every `Sandbox` in `_connected` and `_stale_connections` of both active and closing batches through the loop-independent `Sandbox._close_for_atexit`, since those hold port-forward processes. Then it builds a sync `K8sHelper` and calls `_delete_batch_objects` (imported as `_delete_batch_objects_sync`) with the handle's async-typed Lease for each active batch. It skips any batch where `batch._detach_unfinished() or batch._exit_detaches()`, and leaves those in place until their Lease goes stale and the reaper and garbage collector act ([CLEAN-14](#clean-14), [CLEAN-23](#clean-23)). Failures go to `sys.stderr`, not raised.
- **Related:** [CLEAN-11](#clean-11), [CLEAN-13](#clean-13). The async atexit limits are also in the cancellation section.

</details>

<a id="clean-13"></a>

## CLEAN-13 Delete the ownership record last in a multi-object teardown

**Takeaway:** Delete the lock or ownership record last. A partial failure then leaves a retryable, still-owned state instead of orphans with no owner.

<details>
<summary>The full story (12/N)</summary>

- **Situation:** `release()` deletes the batch's claims and then its Lease.
- **What goes wrong:** Deleting the Lease first would drop the batch-id reservation and the ownership record while claims remain. A failed claim delete would then leave nothing for the handle to retry against.
- **What we do:** `sandbox_batch.py: _delete_batch_objects` deletes the Lease only after a round sees 0 live claims and that round's Lease take succeeded. On any failure other than `BatchInUseError` the Lease stays, `_released` stays False, the handle stays registered, and `release()` or client cleanup retries. The `release()` docstring says "On failure the Lease is kept". The test `test_release_retries_rounds_until_no_progress` checks that `delete_batch_lease` is not called on error.
- **Related:** [CLEAN-14](#clean-14), [LEASE-1](lease.md#lease-1). Round and take rules are in [LEASE-18](lease.md#lease-18) and [RETRY-11](retry.md#retry-11).

</details>

<a id="clean-14"></a>

## CLEAN-14 Hand cascading cleanup to the garbage collector with an ownerReference bound by UID

**Takeaway:** Make each resource a dependent of a liveness object so the platform's garbage collector cleans it up when the owner goes away. Bind by the server-assigned UID, so cleanup is scoped to that exact incarnation, not to a reusable name or label.

<details>
<summary>The full story (18/N, a97eeae7)</summary>

- **Situation:** A driver that dies without `release()` (SIGKILL, OOM, a lost node) used to leave its claims until their shutdownTime, possibly hours away.
- **What goes wrong:** With no link from claim to Lease, nothing deletes the claims when the Lease goes away (through the reaper, `kubectl`, or a reaper that dies between deleting the Lease and the claims). A label-scoped delete by a reaper could also hit a newer batch that reuses the id.
- **What we do:** Every fill claim and `acquire()` claim gets an ownerReference (a pointer in `metadata` that Kubernetes' garbage collector follows) `{apiVersion: coordination.k8s.io/v1, kind: Lease, name, uid}`, built by `batch_utils.py: lease_owner_reference` and passed by `SandboxBatch._send_create`. The UID comes from the create response (`claim_batch`) or the takeover response (`get_batch`), because a replace keeps the UID. Leaving out `controller` and `blockOwnerDeletion` means the driver needs no extra RBAC. The garbage collector matches owners by UID, so it never touches a newer batch under the same id. A response with no name or UID raises `BatchError` instead of creating unowned claims.
- **Example:**
  ```
  kubectl delete lease batch-b1
    -> GC deletes b1-* claims -> their Sandboxes -> their Pods
  ```
  Tests: `test_fill_and_acquired_claims_are_owned_by_the_lease_the_handle_holds`, e2e `test_deleting_a_batch_lease_garbage_collects_its_claims`.
- **Related:** [CLEAN-23](#clean-23), [K8S-2](k8s.md#k8s-2), [BP-6](../best-practices.md#bp-6). [LEASE-7](lease.md#lease-7) covers never recreating a deleted Lease, and [K8S-3](k8s.md#k8s-3) the reaper relying on GC.

</details>

<a id="clean-15"></a>

## CLEAN-15 Roll back only what this call created, and catch BaseException while nobody owns it yet

**Takeaway:** Between creating an external resource and handing it to an owner, roll it back on `BaseException`, because interrupts and cancellation are the likeliest early exits there. Never roll back after a conflict that proves the object belongs to someone else.

<details>
<summary>The full story (12/N, 522dad4c)</summary>

- **Situation:** `SandboxBatch._claim` (and the async mirror) creates the Lease, then lists existing claims (to detect a duplicate id and to get the watch's resourceVersion) before any handle exists to own the Lease.
- **What goes wrong:** A Ctrl-C, a task cancellation, or a list failure in that window would leave a Lease nobody renews or tracks, which blocks the batch id until the reaper runs. Deleting the Lease after a 409 on create would destroy another caller's live batch.
- **What we do:** A 409 on Lease create raises `BatchExistsError` with no delete. After a successful create, both shells wrap the list in `except BaseException` (not just `Exception`), delete the Lease best effort (logging a delete failure), and re-raise. Once the handle exists, ownership passes to the client registry and `release()`.
- **Example:**
  ```python
  lease = create_lease()          # 409 -> BatchExistsError, nothing deleted
  try:
      claims = list_sandbox_claim_objects(...)
  except BaseException:
      delete_batch_lease(...)     # only the Lease this call created
      raise
  ```
  Test: `test_setup_failure_deletes_only_a_lease_it_created` (a "list interrupted" `KeyboardInterrupt` subtest, and a "lease exists" case that asserts no delete).
- **Related:** [CLEAN-19](#clean-19), [K8S-7](k8s.md#k8s-7). [LEASE-1](lease.md#lease-1) covers why a reused id must never adopt old claims.

</details>

<a id="clean-16"></a>

## CLEAN-16 Roll back by name only if a sent write may have landed

**Takeaway:** For a non-idempotent write (one you can't safely repeat), track whether it actually left the client and whether its outcome is uncertain. Roll back by name only in that case: never before the send, never after a clean first-attempt rejection, always after an ambiguous transmitted attempt.

<details>
<summary>The full story (16/N `810d3773`, 16/N dc9ea439, 19/N a2a19cfa)</summary>

- **Situation:** Claims have deterministic names (`<id>-<ordinal>`). When `acquire()` fails, times out, or is interrupted before its claim is Ready, `_drop_acquired` rolls back by deleting `claim_name`. A fill create worker likewise may need to delete a create that returned after a release.
- **What goes wrong:** Setting `may_exist = True` up front sends a needless DELETE when a timeout hits during pacing or while waiting for a `max_in_flight` slot (and that DELETE also competes for `max_in_flight`). Worse, after a clean `409 AlreadyExists` or `403` on attempt 1, deleting by name can destroy a foreign claim this batch never created. Setting `may_exist` only after `_create_one` returns leaks claims when attempt 1 hits the wire and then is cancelled, times out before a retry, or fails non-transiently on attempt 2 after attempt 1's response was lost.
- **What we do:** Start with `may_exist = False` (and `sent = 0`). An `on_send` callback runs inside the `max_in_flight` guard right before each POST and counts the send.
  - `acquire()`: on a caught `Exception`, recompute `may_exist = sent > 1 or _is_transient_error(e)`. Keep `may_exist` as is across `BatchTimeoutError` and `BaseException` interrupts.
  - Fill path, `SandboxBatch._create_worker`: on success `may_exist = sent > 0`, so a create that `can_send` skipped deletes nothing. On exception `may_exist = sent > 1 or _is_transient_error(e)`. `record_create_failure` runs first, then `_delete_released_create` runs only if `may_exist`.
  - `BatchState._take_deletes` skips members that failed with the CreateFailed reason.
- **Example:** the test table in `test_failures_drop_the_member_and_delete_a_claim_that_may_exist`:
  ```
  403 on attempt 1            -> no delete
  503, then 403               -> delete
  interrupt during send       -> delete
  member LOST                 -> no delete
  ```
- **Related:** [CLEAN-17](#clean-17), [CLEAN-18](#clean-18), [RACE-3](race.md#race-3).

</details>

<a id="clean-17"></a>

## CLEAN-17 Record in state which path owns a delete, so two paths never both send it

**Takeaway:** When several paths can each own an object's cleanup, encode the owner in state. A path that already sent a by-name delete clears the creator's in-flight marker. Only a path whose delete was a snapshot (by label, in bulk) leaves the creator responsible for a late landing.

<details>
<summary>The full story (16/N dc9ea439, 19/N a2a19cfa)</summary>

- **Situation:** A pending acquired claim can be released two ways. `release_member` deletes it by name itself. `release_group` deletes by label, which is a point-in-time snapshot. Either way, the woken `acquire()` then runs `_drop_acquired`.
- **What goes wrong:** Before 19/N, `drop_acquired` asked for a delete whenever the name was in `_pending_deletes`. `release_member` had put it there while its own DELETE was still on the wire, so `acquire` sent a second concurrent DELETE for the same claim. Never deleting after `release_group`, though, leaks a POST that commits after the label snapshot.
- **What we do:** `batch_state.py: BatchState.release_member` pops the claim from `_acquiring` after `release_claim`. `release_group` leaves `_acquiring` alone. So `was_acquiring` now means "release_group released me while my POST may be in flight". `drop_acquired` re-queues a by-name delete only `if created and (was_acquiring or retried)`: the create was sent, and either `release_group` released it, or a retry (`sent > 1`) may have landed after `release_member`'s delete.
- **Example:**
  ```
  release_member(b-1): DELETE#1 in flight -> acquire wakes -> drop_acquired:
      was_acquiring False, retried False -> no DELETE#2
  release_group(pool): label delete -> acquire wakes -> drop_acquired:
      was_acquiring True -> DELETE b-1 by name
  ```
  Test: `TestAcquired.test_releasing_a_pending_acquired_member_ends_its_wait`.
- **Related:** [CLEAN-16](#clean-16), [CLEAN-18](#clean-18). [RACE-7](race.md#race-7) covers label snapshots racing creates, and [RACE-4](race.md#race-4) deleting in-flight retries.

</details>

<a id="clean-18"></a>

## CLEAN-18 Scope a late-completion rollback to the obligations that are still open

**Takeaway:** When a write completes after someone asked to cancel it, delete it only if no broader teardown already owns cleanup, and only if the write could have landed after the competing delete. If the rollback fails, leave it recorded so a retry picks it up.

<details>
<summary>The full story (15/N 113fae28, 19/N a2a19cfa)</summary>

- **Situation:** A fill create returns after the caller may have released its claim, its group, or the whole fill.
- **What goes wrong:** Deleting after `release()` or `detach()` is either redundant (`release()` deletes by label) or wrong (`detach()` hands the batch to someone else). Checking per-claim release for a create whose only send was attempt 1 is unnecessary, because `release_member` can only target a claim the watch saw, and it deletes that claim itself.
- **What we do:** `SandboxBatch._delete_released_create` (both shells):
  - Returns at once if `_stop_creating` or `err()` is set.
  - Calls `fill_creates_released(pool, claim if retried else None)`: non-retried creates count only pool-level stops, while retried creates (`sent > 1`) also count a per-claim release, because a retry can land after `release_member`'s delete.
  - Keeps a member the state still tracks (the `get_member` guard: the watch saw it first and the release kept it).
  - Otherwise marks it released with `needs_delete=True` (which bumps the pending-delete sequence) and deletes it.
  - Ignores `BatchError` (the handle stopped). Logs other errors, and the claim stays in `_pending_deletes` for the next release call. The async mirror re-raises `CancelledError`.
- **Related:** [CLEAN-16](#clean-16), [CLEAN-17](#clean-17). [RACE-1](race.md#race-1) covers the watch-outran-the-RPC guard, and [RACE-5](race.md#race-5) sequence-numbered pending deletes.

</details>

<a id="clean-19"></a>

## CLEAN-19 Stop background work by joining the writers and fencing the readers with a stop flag

**Takeaway:** When you can't interrupt a thread, join only the workers whose late side effects matter and whose blocking time you have bounded. Let idle long-poll readers exit on their own as daemons, and make them re-check a stop flag before every shared-state write, including error reporting.

<details>
<summary>The full story (1/N 3a1607bc, 12/N 522dad4c)</summary>

- **Situation:** `release()` and `detach()` write the Lease themselves after stopping renewal. Sync threads can't be cancelled. The watch thread blocks in a long-poll stream that the apiserver closes only at `_WATCH_TIMEOUT_SECONDS` (30 s). Each renewal request is bounded by the renew interval.
- **What goes wrong:**
  - A live renewal racing the release take or the detach write would get one side a 409, or re-establish this handle as holder after `detach()` cleared it, or change `self._lease` underneath it.
  - Joining the watch thread would make `detach()` take up to 30 s on a quiet batch.
  - A late re-list or error arriving after stop could wipe members (marking them LOST) or set `err()` on a detached handle.
- **What we do:** `sandbox_batch.py: SandboxBatch._stop_background_threads` first stops and joins the creators ([CLEAN-20](#clean-20)), then sets `_watch_stop` and `_renew_stop` and joins the renew thread but not the watch thread, with no timeout. The unbounded join is safe because every renewal request carries a timeout of at most `min(interval, time left)`. The watch is a daemon thread. `_watch_loop` re-checks `_watch_stop` after a re-list returns, before each event's state write, and before `note_error`, and it sleeps its backoff through `_watch_stop.wait` so a stop interrupts it. Anything it pops goes to `_stale_connections`, which later cleanups close. Async `_stop_background_tasks` simply cancels both tasks and awaits them. The 12/N review also ruled out a late renewal causing the final Lease-delete 409: its resourceVersion is older than the take's, so the apiserver rejects it. Only a request the server applies late can still land, and the single 409 retry in take and detach absorbs that.
- **Example:**
  ```python
  self._watch_stop.set(); self._renew_stop.set()
  self._renew_thread.join()   # bounded: each renewal request times out by min(interval, left)
  # watch thread not joined; it checks _watch_stop before every write
  ```
  Tests: `test_error_after_stop_set_leaves_err_none`, `test_410_relist_after_stop_set_does_not_touch_state`.
- **Related:** [CLEAN-20](#clean-20). [CANCEL-10](cancel.md#cancel-10) covers fencing the unjoined watch thread, and [LEASE-16](lease.md#lease-16) the retry-once for a late write.

</details>

<a id="clean-20"></a>

## CLEAN-20 Let in-flight creates finish before a snapshot delete, with one shared deadline

**Takeaway:** Before a bulk delete that only sees a point-in-time snapshot, let every request already on the wire finish instead of cancelling it. Bound the total wait with one shared deadline across all workers, not one timeout per worker, and keep sync and async shutdown the same.

<details>
<summary>The full story (12/N 522dad4c, bounded by 18/N a97eeae7)</summary>

- **Situation:** `release()` stops the create workers, then deletes claims with a label-scoped deletecollection (a bulk delete of everything matching a label at that moment).
- **What goes wrong:** The old design's async shell cancelled in-flight creates while sync waited for them. A cancelled POST may still commit on the server after the deletecollection snapshot and survive the release. Joining each sync thread with its own 31 s timeout would let 20 creators stretch teardown to 20 x 31 s.
- **What we do:** Both shells set `_stop_creating`, then wait up to `BATCH_REQUEST_TIMEOUT_SECONDS + BATCH_CREATOR_STOP_GRACE_SECONDS` (30 + 1 s) in total. Async uses `asyncio.wait(creators, timeout=...)` and cancels only the stragglers still pending. Sync uses one deadline shared across all `creator.join()` calls and abandons stragglers as daemon threads. Stragglers check `_stop_creating` before any further send. `pr2_design.md` Q4 accepted the remaining risk (a create that timed out client-side can still land), and 18/N's Lease ownerReference now bounds that leak ([CLEAN-14](#clean-14)).
- **Example:**
  ```python
  deadline = time.monotonic() + 31
  for t in creators:
      t.join(max(0, deadline - time.monotonic()))
  ```
  Test: `test_close_stops_creator_tasks`.
- **Related:** [CLEAN-14](#clean-14), [CLEAN-19](#clean-19). [RACE-8](race.md#race-8) covers how `release()` joins creators before its first take.

</details>

<a id="clean-21"></a>

## CLEAN-21 Hand a new background task to its owner with no suspension point in between

**Takeaway:** Register a newly started background task with its owner (a registry, a TaskGroup, a handle) before any `await`. Every suspension point between spawn and registration is a window where a cancel leaks the task.

<details>
<summary>The full story (12/N, 522dad4c)</summary>

- **Situation:** `_claim` starts the watch, renewal, and creator tasks, then returns the handle, and the client registers it so `close()` can stop it.
- **What goes wrong:** A cancellation landing between task creation and registration leaves tasks renewing the Lease and creating claims where `close()`, `delete_all()`, and exit cleanup can't reach them, so the batch never goes stale.
- **What we do:** `async_sandbox_batch.py: AsyncSandboxBatch._claim` creates the tasks with no `await` after `_start()`, and `AsyncSandboxClient.claim_batch` registers the handle on the very next statement after `await _claim(...)` returns (comment: "Registered with no await in between, so a cancel here can't leave the batch's tasks running where close() won't stop them"). `AsyncSandboxClient._register_batch` is a plain `def`, not a coroutine, so the registration itself can't suspend either. The async registry needs no lock for the same reason. Sync `SandboxClient._register_batch` uses `_batches_lock`, because `detach()` can unregister from another thread.
- **Example:**
  ```python
  batch = await AsyncSandboxBatch._claim(...)  # tasks already started, no await after _start()
  self._register_batch(batch)                  # plain def, no await in between, so no cancel can land
  ```
- **Related:** [CLEAN-22](#clean-22). Possible gap (inferred): the sync `claim_batch` has no equivalent guard. It runs `batch = SandboxBatch._claim(...)` then `self._register_batch(batch)` with no `try`, so a `KeyboardInterrupt` between those two lines could orphan the daemon threads.

</details>

<a id="clean-22"></a>

## CLEAN-22 Keep strong references to background tasks, name them, and make background threads daemons

**Takeaway:** Always keep a strong reference to a background asyncio task (on its owner or in a set), and name it after the resource that owns it and its role. Apply the same naming to the thread-based twin, or debugging differs between surfaces. Make sync background loops daemon threads, so a leaked handle never blocks process exit.

<details>
<summary>The full story (1/N 3a1607bc, 11/N 7cfe8484, 12/N 522dad4c)</summary>

- **Situation:** A process can run many batches, and each handle runs a watch, a renewal loop, and up to `max_in_flight` creators in the background.
- **What goes wrong:** asyncio keeps only weak references to tasks, so an unreferenced fire-and-forget task can be garbage-collected while it runs. With `ensure_future` and default names, task dumps and debuggers can't tell which batch or role a stuck task belongs to. Non-daemon threads (threads Python waits for at exit) would block interpreter exit for a dropped handle.
- **What we do:** 11/N replaced `asyncio.ensure_future` with `asyncio.create_task` (the call meant for coroutines) and named the watch and renewal tasks after their batch, `batch-{id}-watch` and `batch-{id}-renew` in `_start`. 12/N named its creator tasks the same way, `batch-{id}-create-{i}` in `_claim`. The handle stores `_watch_task`, `_renew_task`, and the `_creators` set, which keeps strong references and lets `_stop_background_tasks` find them. Sync starts every thread with `daemon=True` (test `test_start_launches_two_daemon_threads`). Sync `threading.Thread` workers in `_start` and `_claim` keep their default names, a minor parity gap.
- **Related:** [CLEAN-19](#clean-19), [CLEAN-21](#clean-21), [BP-32](../best-practices.md#bp-32).

</details>

<a id="clean-23"></a>

## CLEAN-23 Make a periodic garbage collector stateless, delete-only, and guarded by preconditions

**Takeaway:** Design a periodic cleanup job as stateless, single-step, idempotent deletes, each guarded by a precondition. Crashes, timeouts, and overlapping runs then become no-ops instead of corruption.

<details>
<summary>The full story (18/N, a97eeae7)</summary>

- **Situation:** The reaper (`python -m k8s_agent_sandbox.batch_reaper`) runs as a CronJob, may be killed partway by `activeDeadlineSeconds`, and two runs could overlap.
- **What goes wrong:** A reaper that wrote state or did multi-step changes could leave half-finished work, and overlapping runs could conflict.
- **What we do:** `batch_reaper.py` keeps no state between runs, never writes a Lease, and makes every action a delete with a resourceVersion precondition (the delete succeeds only if the object hasn't changed). The next run simply starts over. When runs overlap, only one precondition delete succeeds and the other gets 404 or 409. `--dry-run` returns the would-reap list without sleeping or deleting. `main` exits 1 on errors. The README suggests running every 1-2 minutes with `concurrencyPolicy: Forbid` and an `activeDeadlineSeconds` that covers the renew-interval wait. Claims are left to the garbage collector ([CLEAN-14](#clean-14)).
- **Example:**
  ```
  run A: list -> wait -> DELETE lease rv=7 -> 200
  run B (overlap): DELETE lease rv=7 -> 404 or 409 -> skip
  run A killed mid-wait -> nothing half-done; next run starts over
  ```
  Tests: `test_lists_every_page` (dry run neither sleeps nor deletes), `TestMain.test_flags_and_exit_code`.
- **Related:** [CLEAN-14](#clean-14), [BP-35](../best-practices.md#bp-35). [LEASE-4](lease.md#lease-4) covers staleness thresholds, [K8S-17](k8s.md#k8s-17) delete outcome classification, and [LEASE-19](lease.md#lease-19) overlap with a slow `release()`.

</details>

---

[Learnings home](../../learnings.md) · [Field guide index](../../learnings.md#sec-field-guide)
