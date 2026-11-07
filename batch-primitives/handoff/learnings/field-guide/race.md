<a id="sec-race"></a>

[Learnings home](../../learnings.md) · [Field guide index](../../learnings.md#sec-field-guide)

# Two observers: when the watch and an RPC see the same object

The batch handle learns about claims in two ways. It sends RPCs (single HTTP calls such as a create POST or a DELETE), and a background watch (a long-lived stream of ADDED, MODIFIED and DELETED events from the apiserver, the Kubernetes API server) reports what the cluster actually stores. The two channels are not ordered, so the watch can report a claim before the create call returns, and a create call can fail on the client after the server already stored the object. The one big idea in this section: the cluster is the truth, an RPC result is only one possibly late or ambiguous report about it, so reconcile every RPC outcome against what the watch has already shown and against what the caller has already decided. Read [RACE-1](#race-1) to [RACE-3](#race-3) first for the core rules, then the later entries for the specific races they cause (retries, pending deletes, and bulk deletes).

<a id="race-1"></a>

## RACE-1 The watch can see your write before the RPC answers

**Takeaway:** When an RPC and a watch both report the same object, the watch may see it first, and decisions may already have been made from that view. Reconcile a late RPC completion, or a late pre-send check, against current watched state instead of assuming the RPC caller is the first to know.

<details>
<summary>The full story (15/N, 19/N)</summary>

- **Situation:** create workers send the initial fill's claim creates over HTTP while the watch observes the batch. `release_not_ready()` and `release_group()` can stop a pool's fill while a worker holds a create that is in flight, waiting for its `max_in_flight` slot, or sleeping between retries. `release_not_ready()` keeps members the watch already shows as Ready.
- **What goes wrong:** two versions of the same bug.
  - After the RPC: `_delete_released_create` deleted any claim whose create call returned after the fill stopped. But if the create had landed on the apiserver before `release_not_ready()` (a slow HTTP response, or a client timeout followed by a `409 AlreadyExists` retry), the watch had already marked it Ready, `wait_for_quorum` had handed it to the caller, and `release_not_ready()` had decided to keep it. The late delete then destroyed a live Ready sandbox.
  - Before the send: if the pre-send check in `can_send` released the claim unconditionally, it would drop that same kept member. If it just returned `False` and recorded nothing, the ordinal would never be accounted for, and the fill would never settle.
- **What we do:**
  - `sandbox_batch.py` / `async_sandbox_batch.py: _delete_released_create` checks under `_lock` whether `_state.get_member(claim_name)` exists. If the watch saw it, the release call already decided its fate, so the creator returns without deleting (15/N, 113fae28).
  - `_create_worker.can_send` (both shells): when `fill_creates_released(pool, claim)` is true, it calls `release_claim` only if `get_member(claim)` is None, then returns `False`. `release_claim` counts the claim as unable and resolved and wakes waiters, so the fill can still settle (19/N, a2a19cfa).
  - Test: `test_release_not_ready_keeps_handed_out_member_when_create_retries_late`.
- **Example:**
  ```text
  t0  POST#1 stored on apiserver, client times out
  t1  watch: b1-4 READY -> wait_for_quorum hands it out
  t2  release_not_ready() keeps b1-4 (it is Ready) and stops the fill
  t3  retry's can_send: fill stopped, but get_member(b1-4) exists -> no release, no delete
  ```
- **Related:** [RACE-2](#race-2), [RACE-4](#race-4), [CLEAN-18](clean.md#clean-18).

</details>

<a id="race-2"></a>

## RACE-2 A client-side create failure never overrides the watch or a release

**Takeaway:** When an RPC's final error contradicts what an independent observer already confirmed, trust the observer. Record a client-side failure only for an object the cluster has never shown you, and never delete by name something you can't prove you created.

<details>
<summary>The full story (12/N, 13/N, 15/N, 19/N)</summary>

- **Situation:** a fill create can fail on the client after retries run out, or a later attempt can fail non-transiently (for example a 403). Meanwhile attempt 1 may have stored the claim and the watch may already track it. Or the caller may have released the pool in the meantime.
- **What goes wrong:** writing a synthetic FAILED member would overwrite a real member the watch tracks, or bring back a claim the caller released, and would wrongly fail the group. Counting a released create as failed would misreport `QuorumUnreachableError`. Deleting a `CreateFailed` name by name could delete a foreign claim, for example after a `409` on attempt 1, where another object already owns the name.
- **What we do:** `batch_state.py: BatchState.record_create_failure`, called from `_create_worker`'s `except` branch in both shells:
  - If the claim is already in `_members` (the watch saw it, so an earlier attempt created it) or in `_released`, return with no change.
  - If the pool's fill creates are stopped, call `release_claim` so the ordinal counts as released, not failed.
  - Otherwise add a synthetic FAILED member with reason `CreateFailed` (`CREATE_FAILED_REASON`).
  - `BatchState._take_deletes` never schedules a delete for LOST members (their claim is already gone) or for `CreateFailed` members (the name may belong to a claim this batch never created).
  - Tests: `test_batch_state.py: test_status_events` ('create failed'), `test_release_member_deletes_until_noted_and_raises_for_unknown_claims`.
- **Example:**
  ```text
  t0  POST#1 stored, client times out
  t1  watch ADDED b1-9
  t2  POST#2 -> 403
  t3  record_create_failure(b1-9) -> no-op; the watched member stays
  ```
- **Related:** [RACE-1](#race-1), [RACE-3](#race-3), [LEASE-13](lease.md#lease-13).

</details>

<a id="race-3"></a>

## RACE-3 Name-based rollback deletes must track whether an ambiguous write hit the wire

**Takeaway:** A write whose response you never got may or may not have landed. Track whether each attempt actually left the client: never delete before the request was sent or after a clean first-attempt rejection, and always delete after any sent attempt whose server outcome is still uncertain.

<details>
<summary>The full story (16/N, 810d3773; fill side 15/N, 19/N)</summary>

- **Situation:** when `acquire()` fails, times out, or hits an interrupt (Ctrl-C or task cancel) before its claim is Ready, `_drop_acquired` rolls back by deleting `claim_name` by name. Fill workers do the same through `_delete_released_create` when their pool was released.
- **What goes wrong:** both simple choices are wrong.
  - Setting `may_exist = True` up front makes a timeout during pacing or while waiting for `max_in_flight` send a needless DELETE (which also competes for `max_in_flight`). And when attempt 1 fails with a clean `409 AlreadyExists` or `403`, deleting by name can destroy a foreign claim the batch never created.
  - Setting `may_exist = True` only after `_create_one` returns leaks claims when attempt 1 hits the wire and is then cancelled mid-flight, times out before a retry, or when attempt 2 fails non-transiently after attempt 1's response was dropped.
- **What we do:**
  - `acquire()` (both shells) starts with `may_exist = False` and `sent = 0`. An `on_send` callback runs right before each socket write, inside the `max_in_flight` guard, and does `sent += 1; may_exist = True`.
  - On a caught `Exception` it recomputes `may_exist = sent > 1 or _is_transient_error(e)`: a single clean rejection means nothing of ours exists, but a retry or a transient error means some attempt may have landed. It keeps `may_exist` as is across `BatchTimeoutError` and `BaseException` interrupts, then calls `_drop_acquired(claim_name, may_exist, retried=sent > 1)`.
  - `_create_worker` uses the same rule: `may_exist = sent > 0` on success, `sent > 1 or _is_transient_error(e)` on failure, then `_delete_released_create(claim_name, pool, retried=sent > 1)` when `may_exist`.
- **Example:**
  ```python
  except Exception as e:
      # A transient error, or a failure after a retry, may hide an attempt that landed.
      may_exist = sent > 1 or _is_transient_error(e)
  ...
  except BaseException:
      self._drop_acquired(claim_name, may_exist, retried=sent > 1)
  ```
- **Related:** [RACE-2](#race-2), [RACE-4](#race-4), [RACE-7](#race-7), [CLEAN-16](clean.md#clean-16).

</details>

<a id="race-4"></a>

## RACE-4 A delete between retries turns a 409 retry into a resurrecting 201

**Takeaway:** Treating `409 AlreadyExists` on a retry as "my first attempt worked" is only safe while nothing deletes the object between attempts. Once an observer shows the first attempt landed, stop retrying, and clean up any retry that was already on the wire when a delete ran.

<details>
<summary>The full story (19/N, a2a19cfa)</summary>

- **Situation:** claim creation retries transient transport timeouts. `create_error_outcome` treats `409 AlreadyExists` on attempt 2+ as proof that attempt 1 stored the claim (on attempt 1 a 409 is a name collision). The watch observes the cluster independently.
- **What goes wrong:** attempt 1 stores the claim but its response times out on the client. The watch shows the claim to the caller while `_create_one` is still in backoff or sending attempt 2. If the caller deletes that one claim with `release_member()`, attempt 2 arrives after the delete and gets `201 Created` instead of `409`. It recreates a claim the caller already released. Even without a delete, a retry after the watch saw attempt 1 can only produce a pointless 409.
- **What we do:**
  - Reuse the existing release and member checks in `can_send`, which `_can_still_send` runs at every check point: before backoff, deadline checks, slot reservation, and the HTTP send.
    - Fill: `_create_worker.can_send` returns `False` once `fill_creates_released(...)` or `get_member(claim_name) is not None`.
    - `acquire()`: `can_send` requires `acquired_member(claim_name) is None and get_member(claim_name) is None`.
  - Delete any retry already on the wire when `release_member()` ran. The `retried=sent > 1` flag reaches `BatchState.drop_acquired` and `_delete_released_create`. A released claim still needs a follow-up delete if its create may have landed and a retry was sent, because `release_member()` deleted attempt 1 itself.
  - Test: `test_release_member_stops_create_retries_and_deletes_an_in_flight_retry` (the 'seen_pending' case).
- **Example:**
  ```text
  t0  POST#1 stored, response times out on the client
  t1  watch ADDED b1-7 -> caller sees it
  t2  release_member(b1-7) -> DELETE b1-7
  t3  POST#2 -> 201 Created   (resurrected, before the fix)
  fix: at t1 get_member(b1-7) exists -> can_send False -> no POST#2
  ```
- **Related:** [RACE-3](#race-3), [RACE-5](#race-5), [RACE-6](#race-6), [K8S-6](k8s.md#k8s-6).

</details>

<a id="race-5"></a>

## RACE-5 Version per-key "delete owed" markers

**Takeaway:** When two operations can owe the same piece of cleanup at overlapping times, stamp each obligation with a version and clear it only with the version you started from. Otherwise an old operation that finishes late erases a newer obligation.

<details>
<summary>The full story (19/N)</summary>

- **Situation:** `_pending_deletes` maps a claim name to a delete the handle still owes. An earlier `release_member()` delete and a later cleanup delete (queued after a create retry recreated the claim, see [RACE-4](#race-4)) can overlap.
- **What goes wrong:** with a plain set, the earlier delete finishing late clears the newer pending entry, and the recreated claim leaks.
- **What we do:**
  - `batch_state.py: BatchState._need_delete` bumps one monotonic counter (`_next_delete_seq`) and stores the new value for that claim.
  - `_delete_claim` (both shells) snapshots `delete_seq(claim_name)` under `_lock`, inside the `max_in_flight` slot, right before sending the DELETE.
  - `note_deleted(claim_name, seq)` clears the entry only if it still equals that snapshot. `delete_seq` returns 0 when nothing is pending, so a stale 0 never matches a real entry.
  - Test: `test_batch_state.py: test_release_member_deletes_until_noted_and_raises_for_unknown_claims`. Adding the required `seq` argument passed `mypy` and both shell suites but broke two direct calls in `test_batch_state.py` (see [TEST-25](../testing.md#test-25)).
- **Example:**
  ```text
  t0  seq=1, DELETE#1 sent
  t1  create retry lands -> _need_delete -> seq=2
  t2  DELETE#1 returns -> note_deleted(1): no match, no-op
  t3  seq 2 stays pending, so a later release call deletes the recreated claim
  ```
- **Related:** [RACE-4](#race-4), [PAT-6](../patterns.md#pat-6).

</details>

<a id="race-6"></a>

## RACE-6 A "may I still send?" check should stop quietly or raise the real error

**Takeaway:** Give "should I keep trying?" predicates two outputs: a quiet stop that hands reporting to the authoritative waiter, or raising the authoritative error itself. Never fold either into the generic failure type of the operation you cancelled.

<details>
<summary>The full story (16/N, dc9ea439; 19/N, a2a19cfa)</summary>

- **Situation:** before each attempt and retry, under `_lock`, `acquire()`'s create asks `can_send` whether to send.
- **What goes wrong:** if "don't send" were signalled by a generic error, or if the create loop wrapped every non-API error, then a release, a terminal state, or an already-landed claim would surface as `TerminalMemberError(CreateFailed)`. That is the wrong type and message, and it could trigger the wrong rollback.
- **What we do:**
  - `batch_state.py: BatchState.acquired_member` raises the authoritative error: `BatchError` ("released before it was Ready") if the claim is in `_released`, or `TerminalMemberError` with the FAILED or LOST member. That error propagates through `_create_one`, and `acquire()`'s `except BatchError: raise` passes it through unwrapped, ahead of the `except Exception` that builds `CreateFailed`.
  - When the claim exists but is PENDING or READY, or creation stopped, `_send_create` / `_create_one` simply return. `_wait_acquired` then reports the outcome: the member, a release, a detach, or `err()`.
- **Example:**
  ```text
  t0  attempt 1 times out but lands
  t1  watch: claim PENDING -> can_send False -> no attempt 2
  t2  watch: claim READY -> _wait_acquired returns the member
  ```
- **Related:** [RACE-4](#race-4), [PAT-18](../patterns.md#pat-18).

</details>

<a id="race-7"></a>

## RACE-7 A label-scoped deletecollection does not stop creates already in flight

**Takeaway:** A server-side bulk delete by label is a point-in-time snapshot, not a tombstone against concurrent writers. Register in-flight creates in local state before sending them, so a concurrent bulk delete obligates the creator to delete its own late-landing object by name.

<details>
<summary>The full story (16/N, 19/N)</summary>

- **Situation:** `release_group(warmpool)` deletes a pool's claims with one label-scoped `deletecollection` (a DELETE on the whole collection, filtered by a label selector). A concurrent `acquire(warmpool)` may already have a `POST /sandboxclaims` in flight that carries the same group label.
- **What goes wrong:** `deletecollection` deletes only the objects stored (in etcd, the apiserver's database) when it runs. A POST that commits right afterwards survives the group delete, and neither call reports an error.
- **What we do:**
  - `BatchState.start_acquire` records each in-flight acquire in `_acquiring[claim] = pool` under the batch lock, before the lock is released to send the POST.
  - When `release_group` marks those names released while they are still in `_acquiring`, `BatchState.drop_acquired` sees `was_acquiring=True` and, if the create may have landed, calls `_need_delete` so `_drop_acquired` sends a follow-up single-object DELETE by name.
- **Example:**
  ```text
  t0  acquire: _acquiring[b1-12] = pool-a, POST in flight
  t1  release_group(pool-a): deletecollection by group label (b1-12 not stored yet)
  t2  POST commits b1-12
  t3  acquire's rollback: released + was_acquiring + may_exist -> DELETE b1-12
  ```
- **Related:** [RACE-3](#race-3), [RACE-8](#race-8), [K8S-8](k8s.md#k8s-8).

</details>

<a id="race-8"></a>

## RACE-8 Quiesce writers, bounded by their request timeout, before a bulk delete

**Takeaway:** Before a snapshot-style bulk delete, stop every producer that can add to the set and wait for in-flight writes for at most their request timeout, using one shared deadline instead of one per worker. Back that up with a re-count and a garbage-collection backstop for writes that land even later.

<details>
<summary>The full story (12/N, 522dad4c; ownerReference backstop 18/N)</summary>

- **Situation:** `release()` deletes all of the batch's claims by label with `deletecollection` rounds, then deletes the Lease. A fill create may still be on the wire, and the renewal loop may still be writing the Lease.
- **What goes wrong:** a create that commits after the `deletecollection` survives it ([RACE-7](#race-7)). A live renewal could change the Lease between the round's take and its delete. But waiting forever on a hung create would block `release()`.
- **What we do:**
  - Sync `sandbox_batch.py: SandboxBatch._stop_background_threads` sets `_stop_creating`, then joins each creator against one shared deadline of `BATCH_REQUEST_TIMEOUT_SECONDS` (30 s) + `BATCH_CREATOR_STOP_GRACE_SECONDS` (1 s), so any in-flight POST has returned or timed out. Then it sets the watch and renew stop flags and joins the renew thread.
  - Async `async_sandbox_batch.py: AsyncSandboxBatch._stop_background_tasks` does the same with `asyncio.wait` and then cancels stragglers.
  - Only after that does `release()` close connections and start the rounds. The rounds count live claims and repeat until none are left, and the Lease ownerReference (garbage collection deletes claims whose owner Lease is gone; added in 18/N) covers anything that lands even later.
  - Tests: `test_hung_create_delays_release_only_until_the_request_timeout`, `test_release_stops_creates_then_deletes_claims_then_the_lease`.
- **Example:**
  ```python
  self._stop_creating.set()
  deadline = time.monotonic() + BATCH_REQUEST_TIMEOUT_SECONDS + BATCH_CREATOR_STOP_GRACE_SECONDS
  for creator in self._creators:
      creator.join(max(0.0, deadline - time.monotonic()))
  ```
- **Related:** [RACE-7](#race-7), [CLEAN-20](clean.md#clean-20).

</details>

<a id="race-9"></a>

## RACE-9 Mark late watch `ADDED` claims on stopped batches or groups `needs_delete=True`, and drain caller-thread `acquire()` `POST`s before `release()` or `detach()`

**Takeaway:** Stopping fill creation (`release_not_ready`, `release_group`) or starting `release()`/`detach()` does not mean the apiserver has finished delivering earlier creates. Any late watch `ADDED` or `410` relist entry for an already-stopped batch or group must enter `_released` with `needs_delete=True` (`F-03`), and `release()`/`detach()` must drain in-flight caller-thread `acquire()` `POST`s just like background `_creators` (`F-24`).

<details>
<summary>The full story (Chaos & Verification Suite: F-03 & F-24, batch_state.py, sandbox_batch.py, async_sandbox_batch.py)</summary>

- **Situation:** Two producer-vs-teardown races occur when claims land on the apiserver just as a caller tears down part or all of a batch:
  1. **Late watch `ADDED` after `release_not_ready()` / `release_group()` (`F-03`):** `_create_worker` `POST`s a claim, `release_not_ready()` runs before the watch sees that `ADDED` event, and the watch delivers `ADDED` milliseconds later.
  2. **Caller-thread `acquire()` `POST` vs. `release()` / `detach()` (`F-24`):** Dynamic `acquire()` sends `create_sandbox_claim` directly on the caller's thread/task (outside `_creators`), while another thread/task calls `release()` or `detach()`.
- **What goes wrong:**
  1. In `batch_state.py:258-262` (and `resync_from_list` `:415-426`), `upsert_claim` caught unseen claims arriving after `_fill_stopped` / `pool in _fill_stopped_pools` and called `self._mark_released(name, needs_delete=False)`. That hid the claim from `members()` while **never deleting it in Kubernetes**, leaving the sandbox pod running until Lease GC (`BatchWatch.tla` `bw_neg_late_added`, `20` states).
  2. In `sandbox_batch.py:2166-2207` (`_stop_background_threads`) and `async_sandbox_batch.py:2166-2206` (`_stop_background_tasks`), teardown joined only initial-fill `_creators` without waiting for `self._state.has_in_flight_acquires()` (`_acquiring`). On `detach()`, `acquire()`'s `POST` committed *after* `_detached = True`, `_drop_acquired()` skipped cleanup because `self._detached` was set, and nobody held the Lease to delete the orphan; on `release()`, a `POST` committing after `deletecollection` survived until async GC (`BatchAcquireRelease.tla` `bar_neg_inflight_release`, `88` states).
- **What we do:** Pass `needs_delete=True` in `upsert_claim` and `resync_from_list` when tombstoning unseen claims on stopped batches/groups (queuing `_delete_claim(name)` from the watch loop), and wait for in-flight `_acquiring` creates (bounded by `BATCH_REQUEST_TIMEOUT_SECONDS`) before `release()` sweeps claims or `detach()` surrenders the Lease.
- **Related:** [RACE-3](#race-3), [RACE-7](#race-7), [RACE-8](#race-8), [CHAOS-7](../chaos-verification-suite.md#chaos-7).

</details>

<a id="race-10"></a>

## RACE-10 Evict closed cached connectors (`is_active`), fence `sbx.terminate()` on batch Lease health, and reconnect watch-evicted handles in `refresh()`

**Takeaway:** Caching a `Sandbox` handle in `batch._connected` creates three lifecycle aliases that must stay synchronized: `batch.connect(m)` must replace a cached `Sandbox` if the caller closed its transport (`not sbx.is_active`), `sbx.terminate()` on a batch-managed `Sandbox` must refuse un-fenced deletes after `detach()` or Lease expiry (`F-17`), and `SandboxHandle.refresh()` must fall back to `batch.connect(member)` when the batch watch already evicted and closed the old pod's connector (`F-10a`).

<details>
<summary>The full story (Chaos & Verification Suite: F-10a, F-17, F-21a)</summary>

- **Situation:** `SandboxBatch.connect(member)` caches connected `Sandbox` objects in `_connected[claim_name]`, evicting and closing them when the watch sees pod IP or sandbox name changes ([WATCH-7](watch.md#watch-7)). Downstream `agent-sandbox-rl` wraps that `Sandbox` inside `SandboxHandle`.
- **What goes wrong:**
  1. **Returning dead cached `Sandbox`es & unfenced `terminate()` (`F-17`):** Calling `sbx.close_connection()` sets `sbx._is_active = False` without notifying `SandboxBatch._connected`, so the next `batch.connect(member)` returned the dead `Sandbox` (`SandboxError: Sandbox connection is closed`). Worse, calling `sbx.terminate()` called `k8s_helper.delete_sandbox_claim` directly without checking `_detached` or `_lease_expires_at`, bypassing batch Lease fencing (`INV-OWN-1`).
  2. **Watch eviction races `SandboxHandle.refresh()` (`F-10a`) and `fleet._connect_batch_claim()` (`F-21a`):** After pod suspend/resume or restart, the batch watch sees the Pod IP change first and calls `close_connection()` on the cached `Sandbox`, setting `_is_active = False`. When `SandboxHandle.refresh()` (`handles.py:296-306`) then ran `self._sandbox.refresh()`, it raised `Sandbox connection is closed` instead of reconnecting via `batch.connect(fresh_member)`. Similarly, if a member flapped `READY -> PENDING` in the narrow window between `batch.acquire()` and `fleet._connect_batch_claim()` (`fleet.py:1533`), raw `SandboxNotReadyError` escaped unwrapped instead of `FleetUnavailableError`.
- **What we do:** Check `sbx.is_active` before returning a cached entry in `batch.connect()`, route or fence `terminate()` on batch-managed `Sandbox` instances through batch Lease checks, reconnect from `self._batch_claim.batch.connect(...)` inside `SandboxHandle.refresh()` when the cached connection was closed by watch eviction, and translate transient `SandboxNotReadyError` in `_connect_batch_claim` into `FleetUnavailableError` after releasing the claim.
- **Related:** [WATCH-7](watch.md#watch-7), [WATCH-8](watch.md#watch-8), [WATCH-10](watch.md#watch-10), [API-30](api.md#api-30).

</details>

---

[Learnings home](../../learnings.md) · [Field guide index](../../learnings.md#sec-field-guide)
