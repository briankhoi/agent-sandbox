<a id="sec-lease"></a>

[Learnings home](../../learnings.md) · [Field guide index](../../learnings.md#sec-field-guide)

# Leases, liveness, handoff, and the reaper

This section covers the batch's Lease: a small `coordination.k8s.io/v1` object named `batch-<id>` that says "someone is still working on this batch". The handle creates it, renews it, hands it off with `detach()`, re-takes it during `release()`, and the reaper deletes it once it has gone stale. Every claim points at the Lease through an ownerReference (a pointer from a child object to its owner, matched by UID), so deleting the Lease lets Kubernetes garbage collection (GC) delete the claims. The one big idea is this: **ownership must be provable from the server's own records (UIDs, resourceVersions, transition counters), never assumed from local state or from a clock alone, and every path must fail toward "keep the work" or "let expiry clean up".** Read the first ten entries for the reusable lease rules. The rest show how those rules play out in takeover, handoff, release, and recovery.

<a id="lease-1"></a>

## LEASE-1 Create the liveness parent first and delete it last

**Takeaway:** When one parent object stands for liveness, ownership, and a name reservation, create it first with create-if-absent semantics and delete it last. Then "child without parent" always means orphan, never "still being created", and GC by UID can clean up.

<details>
<summary>The full story (12/N, 18/N)</summary>

- **Situation:** `claim_batch` has to reserve a batch id, find out whether claims with that id already exist, and give every claim an ownerReference to the Lease.
- **What goes wrong:** If claims went first, two callers with the same id could both create claims. A crash before the Lease existed would leave claims with no liveness record. The claims would also have no Lease UID (the unique id the apiserver assigns at create time) to point an ownerReference at. And a reused id could adopt old claims that are still terminating, or whose Lease the reaper already deleted. Those old claims point at the old UID, so GC would collect them under the new batch.
- **What we do:** `sandbox_batch.py: SandboxBatch._claim` and `async_sandbox_batch.py: AsyncSandboxBatch._claim` create the Lease first. A 409 (Conflict, here "already exists") raises `BatchExistsError`, so the Lease create works as an atomic mutex on the id. Only then does `_claim` list claims by the batch-id label, and any leftover item raises `BatchExistsError`. The UID of the created Lease becomes `_owner_references` on every claim. On any `BaseException` (the Python base class that also covers Ctrl-C's `KeyboardInterrupt` and asyncio cancellation) between create and list, setup deletes the Lease it just created, best-effort. `release()` deletes the Lease last. The reaper contract relies on the resulting rule (reaper_contract.md 'Claims Without a Lease'): a claim with a batch-id label but no Lease never belongs to a batch still being created.
- **Example:**
  ```text
  POST lease batch-b1          -> 201 (uid U)
  LIST claims -l batch-id=b1   -> []
  workers POST claims with ownerReferences=[U]
  ```
- **Related:** [LEASE-7](#lease-7), [LEASE-29](#lease-29). [CLEAN-15](clean.md#clean-15) and [CLEAN-13](clean.md#clean-13) cover the rollback details (setup failure deletes only the Lease this call created; the Lease is deleted last); [K8S-7](k8s.md#k8s-7) covers using the Lease create as the mutex for claim_batch.

</details>

<a id="lease-2"></a>

## LEASE-2 Write the Lease with PUT and the last-seen resourceVersion

**Takeaway:** For mutual exclusion on Kubernetes objects, write with PUT carrying the last-seen resourceVersion (or a PATCH with a resourceVersion precondition) so the apiserver decides who wins. Reuse the last write's response instead of reading before each write.

<details>
<summary>The full story (1/N, 3/N)</summary>

- **Situation:** Two handles may race to hold one batch Lease: during takeover, renewal, and detach.
- **What goes wrong:** A PATCH without a resourceVersion (the object's version counter, which the apiserver bumps on every write) applies unconditionally. Two clients could both believe they hold the Lease.
- **What we do:** `k8s_helper.py: K8sHelper.replace_batch_lease` (and its async twin) calls `replace_namespaced_lease`, which sends the full object with its resourceVersion. The apiserver rejects a stale write with 409. Since 3/N, renewal writes from the last successful response without a read first, and reads only on a conflict.
- **Related:** [LEASE-11](#lease-11), [LEASE-14](#lease-14), [LEASE-28](#lease-28), [BP-4](../best-practices.md#bp-4). [PERF-8](perf.md#perf-8) covers renewing from the last written Lease without reading it first.

</details>

<a id="lease-3"></a>

## LEASE-3 Make the holder identity unique per handle, not per process

**Takeaway:** A lease holder identity must name the logical owner instance (the handle), not the host or process. Otherwise every "is it still mine?" check is unsound inside one process.

<details>
<summary>The full story (1/N, 12/N)</summary>

- **Situation:** Every Lease ownership check compares the Lease's `holderIdentity` field with the handle's identity: renewal, detach, and `_take_batch_lease` in release.
- **What goes wrong:** An identity shared by two handles in one process (for example hostname plus pid only) would let a stale handle treat another handle's takeover as its own. It would then renew, detach, or release a batch it no longer owns.
- **What we do:** `batch_utils.py: generate_holder_identity` returns `hostname_pid_uuid8` (8 random hex characters). `SandboxBatch._claim` and `_attach` call it, so each handle gets its own identity. The 12/N D1 decision required checking this uniqueness before building the release fence on top of it.
- **Related:** [LEASE-14](#lease-14).

</details>

<a id="lease-4"></a>

## LEASE-4 Order the staleness thresholds: subtract the skew margin to adopt, add it to destroy

**Takeaway:** When several parties act on one lease under clock skew, order their thresholds so the action that is costly to get wrong (destroying work) always comes last. Subtract the skew margin for actions that acquire, add it for actions that destroy, and document the order.

<details>
<summary>The full story (1/N, 3/N, 18/N)</summary>

- **Situation:** Three actors with different clocks judge one Lease. `get_batch` decides whether it may adopt it. The holder decides whether it still owns it. The reaper decides whether it may delete it. Clock skew means two machines disagree about the current time.
- **What goes wrong:** With one shared staleness formula, skew lets an attacher adopt a batch whose holder still thinks it owns it. Worse, a reaper whose clock runs ahead of the driver's could delete a batch while its driver still holds the Lease, which destroys running work.
- **What we do:**
  1. `batch_utils.py: is_lease_stale` returns `now > renewTime + D - skew_margin` with `CLOCK_SKEW_MARGIN = 5` (in `batch_utils.py`). `get_batch` stops adopting early, the safe side for attaching.
  2. The holder gives up at `last_sent + D` on its own monotonic clock (see [LEASE-5](#lease-5) and the comment on `_lease_expires_at` in `SandboxBatch.__init__`).
  3. `batch_reaper.py: is_lease_reapable` returns `now > renewTime + D + skew_margin` (default `REAPER_DEFAULT_SKEW_MARGIN_SECONDS = 60`), so the reaper acts late. It also deletes only if the resourceVersion stays unchanged for one renew interval ([LEASE-8](#lease-8)).

  Both docstrings explain why the sign differs. `test_spec_duration_boundary_adoptable_then_expired_one_second_later` and `test_reaps_only_leases_stale_past_duration_plus_margin` pin the boundaries.
- **Example:**
  ```text
  D=60, renewTime=t0
  >t0+55s   get_batch refuses (stale for adopting)
  t0+60s    holder declares itself expired
  >t0+120s  reaper may pick it (default margin 60), then waits one 20s interval to confirm
  ```
- **Related:** [LEASE-5](#lease-5), [LEASE-8](#lease-8), [LEASE-9](#lease-9), [BP-16](../best-practices.md#bp-16). [TEST-23](../testing.md#test-23) covers the SIGKILL/SIGSTOP e2e cases that prove a paused holder is not reaped early.

</details>

<a id="lease-5"></a>

## LEASE-5 Take the holder's expiry reading before you stamp renewTime

**Takeaway:** When a local deadline mirrors a timestamp other parties read, take the local clock reading before you stamp the shared timestamp, so the local deadline errs early. Use a monotonic clock (one that never jumps with wall-clock changes) for the local side.

<details>
<summary>The full story (1/N, 3/N (31a47728), 12/N)</summary>

- **Situation:** The holder decides on its own when its Lease has expired. Other clients judge expiry from the `renewTime` stored on the server.
- **What goes wrong:** Before 3/N, the holder took its expiry timestamp after the replace returned. That could be up to one RenewInterval after the `renewTime` the server stored. The holder then believed it still held the batch after other clients could already take it (reaper_contract.md 'Driver Side').
- **What we do:** `SandboxBatch._write_renewal` reads `sent_at = time.monotonic()` right before `lease_now()` stamps `renewTime`. `_renew_once` sets `_lease_expires_at = sent_at + _lease_duration` only after the write succeeds. The initial create (`_claim`) and the takeover (`_attach`) read `renewed_at` before `lease_now()` the same way. `async_sandbox_batch.py` does the same. `test_expiry_fires_a_lease_duration_after_the_last_successful_renewal_was_sent` pins it.
- **Example:**
  ```text
  t0  sent_at=0, stamp renewTime=T0, send PUT
  t2  response arrives -> _lease_expires_at = 0 + D   (not 2 + D)
  ```
- **Related:** [LEASE-4](#lease-4), [LEASE-29](#lease-29), [BP-2](../best-practices.md#bp-2). [RETRY-9](retry.md#retry-9) covers firing expiry at a deadline and bounding each renewal request by the time left.

</details>

<a id="lease-6"></a>

## LEASE-6 Lease expiry is the leak guard: never defeat it

**Takeaway:** A lease-based design needs something that eventually frees resources when the holder disappears. Once a holder tells its callers it has lost the lease, it must stop writing the lease, and no "keep alive on failure" path may bypass expiry.

<details>
<summary>The full story (3/N (31a47728), detach design)</summary>

- **Situation:** Two cases. (1) Renewals fail long enough that the holder reports `BatchLeaseExpiredError` through `err()`. (2) A `detach()` fails, and someone proposed restarting renewal so the batch stays alive.
- **What goes wrong:** (1) Before 3/N, the renew loop kept going after expiry. A renewal that later succeeded kept the Lease fresh while `err()` said expired. The batch was then neither usable (`err()` set) nor reapable (Lease fresh): a permanent leak. (2) Restarting renewal after a failed `detach()` would keep a batch held for the whole process lifetime, even if nobody ever retries.
- **What we do:** On expiry, `SandboxBatch._renew_loop` calls `_stop_renewal` (which records the error with `note_error` and wakes waiters with `notify_all`) and returns. The handle never writes the Lease again, so the reaper's staleness test can eventually match it. `test_failed_renewals_retry_on_a_backoff_and_stop_at_expiry` makes renewals succeed again once the Lease has expired (t=10) and asserts none are sent after that (`sent == [3, 4, 6, 9]`). For detach, we rejected the restart-renewal option.
- **Related:** [LEASE-7](#lease-7), [LEASE-22](#lease-22), [LEASE-28](#lease-28). [API-12](api.md#api-12) covers `lease_degraded()` as the early-warning signal before expiry.

</details>

<a id="lease-7"></a>

## LEASE-7 Never recreate a missing Lease

**Takeaway:** When dependents are bound to an owner by UID, losing the owner is final. Surface it as a terminal error, and never recreate the owner under the same name, because the identity that mattered is gone and a fresh object breaks every reader of the old metadata.

<details>
<summary>The full story (3/N, 12/N, 18/N (a97eeae7))</summary>

- **Situation:** The reaper, a manual `kubectl delete lease`, or an earlier partial `release()` removes the Lease. The original holder may still be running (for example after a long GC pause), or a `release()` round may start and find it gone.
- **What goes wrong:** Recreating the Lease is tempting. But the new Lease gets a new UID. Every existing claim references the old UID, so GC deletes them anyway, and the handle would report a healthy batch whose members are disappearing. An early version of `release()` did recreate the Lease to reserve the id. That Lease lacked the batch's annotations and caller labels, so `get_batch` and the reaper would see a different batch. Its new UID would also trip the release fence ([LEASE-14](#lease-14)) on the next round, and creating anything is a surprising side effect for a call named "release".
- **What we do:**
  - Renewal: when `_renew_once` gets a 404, or a 409 followed by a read that finds nothing, it records `BatchLeaseExpiredError` and stops. `TestRenewal.test_missing_lease_stops_renewal_and_records_expired` asserts `create_batch_lease.assert_not_called()`. Creators already stop once `err()` is set ([LEASE-12](#lease-12)). [LEASE-28](#lease-28) covers the rest of the renewal conflict handling.
  - Release: `_take_batch_lease` returns `None` for a missing Lease. The round still sends deletecollection (a bulk delete by label selector) and the live count, and `finish()` skips the Lease delete because there is no resourceVersion. If a Lease with another UID appears in a later round, the fence raises `BatchInUseError` before the next delete (`test_release_with_no_lease_deletes_the_claims_and_creates_nothing`, `test_release_deletes_nothing_while_another_handle_holds_the_lease['a Lease created again after a round']`). Source: pr2_round2_decisions.md section 4. Note that the 12/N squash message still says "or creates it if it's missing", but the code does not.
  - Attach: `get_batch` raises `BatchLeaseExpiredError` when claims exist without a Lease, so nobody adopts a batch that is being reaped.
- **Example:**
  ```text
  t0  reaper DELETE lease
  t1  holder wakes, PUT -> 404 -> err() = BatchLeaseExpiredError; no POST lease
  release round 1: read -> 404, deletecollection, count=1
  release round 2: read -> Lease(uid=other) -> BatchInUseError
  ```
- **Related:** [LEASE-1](#lease-1), [LEASE-6](#lease-6), [LEASE-14](#lease-14), [LEASE-28](#lease-28). [K8S-2](k8s.md#k8s-2) covers why GC matching by UID means you never recreate the Lease.

</details>

<a id="lease-8"></a>

## LEASE-8 The reaper proves death with a confirmation wait and a resourceVersion-precondition delete

**Takeaway:** Never delete on a remote timestamp alone. Prove the holder is dead by watching that the object's server-assigned version stays unchanged for one full heartbeat interval, and make the delete conditional on that version.

<details>
<summary>The full story (18/N (a97eeae7))</summary>

- **Situation:** A healthy driver whose clock runs behind by more than the margin writes `renewTime` values that already look stale to the reaper.
- **What goes wrong:** A timestamp check alone cannot tell a dead driver from a live driver with a slow clock, however large the margin is.
- **What we do:** `batch_reaper.py: reap` records each candidate's resourceVersion from the list (a `StaleLease`). It waits one renew interval, `max(1, D // 3)`, counted from `listed_at` after all lists returned, shortest interval first. Then `_delete_lease_if_unchanged` deletes with `V1Preconditions(resource_version=...)`. A live driver renews within one interval, which changes the resourceVersion, so the delete fails with 409 and the reaper skips that batch. reaper_contract.md compares this to client-go leader election's `observedTime`: it relies on the server's version counter, not on clocks. `test_lease_delete_outcomes` and `test_waits_each_leases_renew_interval_then_deletes_only_the_leases` pin it.
- **Example:**
  ```text
  list:   rv=42, renewTime looks 2 min old
  +20s    driver PUTs (rv=57)
  reaper  DELETE {precondition rv=42} -> 409 -> skip
  ```
- **Model check:** the TLA+ spec agrees the confirmation wait helps a lot, but shows it bounds clock skew rather than removing the need for it: with the defaults the reaper must run less than about 80 s ahead (about 25 s for a stalled holder with `--skew-margin 5`), because renewals land up to one interval plus two network legs apart. See [TLA-2](../tla.md#tla-2).
- **Related:** [LEASE-4](#lease-4), [LEASE-9](#lease-9), [LEASE-10](#lease-10), [LEASE-19](#lease-19), [BP-17](../best-practices.md#bp-17). [K8S-17](k8s.md#k8s-17) covers how delete outcomes are classified and [K8S-3](k8s.md#k8s-3) why the reaper deletes only the Lease (the reaper deliberately does not reuse `delete_batch_lease`, see [K8S-1](k8s.md#k8s-1)).

</details>

<a id="lease-9"></a>

## LEASE-9 The renew interval is a protocol constant shared by every writer and the reaper

**Takeaway:** When a remote observer infers liveness from "changed within N", N is a protocol constant. Derive it from data on the object itself, and document it as a requirement for every writer implementation.

<details>
<summary>The full story (1/N, 18/N)</summary>

- **Situation:** The reaper's confirmation wait ([LEASE-8](#lease-8)) assumes a live driver changes the resourceVersion within one renew interval.
- **What goes wrong:** If any client (a future Go SDK, say) renewed less often than D/3, a live driver with a skewed clock could keep the same resourceVersion through the reaper's whole wait and get reaped.
- **What we do:** `batch_reaper.py: find_stale_leases` uses `renew_interval = max(1, lease_duration_seconds // 3)`, and `SandboxBatch.__init__` uses `_renew_interval = max(1, lease_duration // 3)`. The reaper reads D from the Lease's own `leaseDurationSeconds`, which every handle write sets to the handle's D, so `detach(grace)`, which writes D = grace, also changes the reaper's wait. reaper_contract.md 'Driver Side' states it: "Renewal cadence is part of the contract ... Go SDK and any other client must keep it".
- **Related:** [LEASE-8](#lease-8), [LEASE-30](#lease-30), [RETRY-10](retry.md#retry-10).

</details>

<a id="lease-10"></a>

## LEASE-10 A destructive sweeper skips what it cannot reason about

**Takeaway:** A destructive sweeper should fail closed on missing inputs. If the safety precondition (a version or a timestamp) is absent, skip the item and log it, rather than fall back to an unconditional action.

<details>
<summary>The full story (18/N (a97eeae7))</summary>

- **Situation:** Listed Leases can lack fields: a hand-edited Lease, a client that never stamped `renewTime`, or a malformed response.
- **What goes wrong:** Without a resourceVersion, the delete would have no precondition and would remove a live Lease outright. Assuming an expiry for a Lease with no `renewTime` or duration could delete live work.
- **What we do:** `batch_reaper.py: find_stale_leases` silently skips a Lease with no `metadata.resource_version`, because an unconditional delete is never allowed. It skips a Lease with no `spec.renew_time` or `lease_duration_seconds` and logs a warning. The code comment says: "Waiting is always safe, while guessing the expiry could delete live work." `test_never_touches_a_lease_that_isnt_a_valid_batch_lease` covers the `resource_version=None` case.
- **Related:** [LEASE-8](#lease-8).

</details>

<a id="lease-11"></a>

## LEASE-11 Once err() is set, the handle sends no by-name deletes, retries included

**Takeaway:** When resource names are deterministic, gate every destructive call on still holding the lease that scopes those names, and re-check that gate on every attempt, retries included. Once you might have lost the lease, leave cleanup to the owner-aware path or to GC. Prefer one simple stop rule over several partial ownership tests, and scale the ownership check to how often the operation runs: a fencing take for rare whole-batch teardown, a local health flag for frequent per-item deletes.

<details>
<summary>The full story (12/N (522dad4c), 15/N (113fae28), 16/N (dc9ea439))</summary>

- **Situation:** `err()` is set when renewal stops (expired or lost) or the watch fails for good. Single-claim deletes (`release_member`, `release_not_ready`, `release_group`, acquire rollback) run while the handle is live and renewing, and retry transient errors.
- **What goes wrong:** Claim names (`<batch>-<ordinal>`) and labels are deterministic. After the Lease may be lost, another handle may own the batch, or a batch recreated under the same id reuses the same claim names. A DELETE by name could destroy someone else's sandbox.
- **What we do:** Every delete attempt goes through `_member_delete`, which calls `_check_healthy()` under `_lock` (detached or `err()` raises `BatchError`; its comment notes that a recreated batch reuses claim names and labels). The check runs before any local state changes and before each request. `_delete_claim` calls `_check_lease_held` again before each retry sleep. `_drop_acquired` (acquire rollback) catches that `BatchError` and leaves the claim recorded, for `release()` (label-scoped, after re-checking ownership) or for Lease GC through the ownerReference. Its docstring says "Once err() is set the claim is kept".
- **Why not a stronger check:** Taking the Lease per delete, as `release()` does per round ([LEASE-18](#lease-18)), would cost a GET and a PUT per member, too expensive for RL callers that release one member at a time. An expiry-based gate was also proposed, but it would add a second ownership rule just for deletes. The 12/N review also considered a narrower "still owns the Lease" test for deletes after a watch-only failure and rejected it: one rule, "err() or detached means the handle has stopped", is easier to document and verify. A watch-only failure therefore also blocks deletes, which is acceptable because a permanent watch failure is a 403 or 404, likely RBAC, so deletes would likely fail too (pr2_round2_decisions.md section 2, decisions_1824_round2.md section 2).
- **Tests:** `test_release_methods_delete_nothing_once_err_is_set`, and the "err set" case of `test_failures_drop_the_member_...` (deleted False).
- **Related:** [LEASE-12](#lease-12), [LEASE-13](#lease-13), [LEASE-18](#lease-18). [LOCK-4](lock.md#lock-4) covers the ordering rule of checking every precondition before mutating local state (old 4.3).

</details>

<a id="lease-12"></a>

## LEASE-12 Stop creating once err() is set

**Takeaway:** Once a component loses its means to observe or own what it produces, stop producing at once. Every send path must check the health latch, not only the worker loop.

<details>
<summary>The full story (12/N (522dad4c))</summary>

- **Situation:** The watch or Lease renewal fails terminally and sets `err()`.
- **What goes wrong:** Further creates would make claims that this handle can neither observe (the watch is dead) nor keep alive (the Lease may be gone or held by someone else).
- **What we do:** `SandboxBatch._create_worker` exits at its next loop when `err()` is set, and `_can_still_send` refuses every attempt and retry. `_delete_released_create` also returns early, because the handle may no longer hold the batch ([LEASE-11](#lease-11)). `test_creation_stops_once_err_is_set` pins it.
- **Related:** [LEASE-7](#lease-7), [LEASE-11](#lease-11), [LEASE-28](#lease-28).

</details>

<a id="lease-13"></a>

## LEASE-13 Delete by name only what you have evidence you created

**Takeaway:** Delete by name only objects you have positive evidence you created. A name you tried and failed to create may belong to someone else, and a name already gone is a wasted call.

<details>
<summary>The full story (15/N)</summary>

- **Situation:** `release_member` or `release_not_ready` runs on a member whose claim the batch can't vouch for.
- **What goes wrong:** A LOST claim is already gone, so the DELETE is wasted. A CreateFailed name (for example a first-attempt 409) may belong to a foreign claim this batch never created, and a delete by name would destroy it.
- **What we do:** `batch_state.py: BatchState._take_deletes` skips members in state `LOST` and members with reason `CREATE_FAILED_REASON`. They still drop out of `members()`, but they are never queued for delete. `test_release_member_deletes_until_noted_and_raises_for_unknown_claims` covers it. The acquire() rollback follows the same rule through `may_exist` ([CLEAN-16](clean.md#clean-16)).
- **Related:** [LEASE-11](#lease-11), [RACE-3](race.md#race-3). [CLEAN-16](clean.md#clean-16) covers deleting by name on rollback only if a sent attempt may have created the claim.

</details>

<a id="lease-14"></a>

## LEASE-14 get_batch takeover: validate everything, then make one conditional write

**Takeaway:** In a takeover, finish every fallible local validation before the one conditional write that transfers ownership, so a failed adoption leaves the shared lock untouched.

<details>
<summary>The full story (1/N (3a1607bc), 2/N, 3/N)</summary>

- **Situation:** `get_batch` adopts an existing batch by writing itself as the Lease holder.
- **What goes wrong:** If validation ran after the takeover write, a `get_batch` that failed on corrupt annotations or inconsistent group annotations would leave the Lease held by an identity nobody renews. That blocks handoff until expiry. An unconditional write could also steal a Lease that another attacher took between read and write.
- **What we do:** `SandboxBatch._attach` runs, in order: read the Lease, list claims, `parse_lease_metadata`, staleness check, holder check (an empty holder counts as free since 3/N), `reconstruct_groups` and state seeding. Only then does it make the single replace, carrying the read's resourceVersion. A 409 maps to `BatchInUseError`. The write sets `leaseTransitions + 1` (which feeds the release fence, [LEASE-17](#lease-17)) and `acquireTime = now`. Staleness and `renewed_at` are computed after the claim list, which can take up to 90 s (2/N timeouts), so the slow list doesn't eat into the new holder's window. Tests: `test_invalid_annotation_raises_batch_error_with_no_write`, `test_conflicting_group_annotations_raise_batch_error_with_no_write`, `test_takeover_write_conflict_raises_in_use`, `test_takeover_increments_existing_lease_transitions`.
- **Related:** [LEASE-2](#lease-2), [LEASE-17](#lease-17), [LEASE-25](#lease-25), [LEASE-29](#lease-29), [LEASE-30](#lease-30).

</details>

<a id="lease-15"></a>

## LEASE-15 Define release by its goal: "I no longer hold it" is success

**Takeaway:** Define a release operation by its goal. If the lock is gone, owned by someone else, or already released by a concurrent call of yours, the goal is met. On an optimistic-concurrency conflict, re-read and check whether the goal is already met before failing.

<details>
<summary>The full story (1/N (3a1607bc), 11/N (7cfe8484))</summary>

- **Situation:** `detach()` hands the batch off by writing the Lease with no holder. By the time it runs, the reaper may have deleted the Lease, or another handle may have taken it over after this handle expired. Two code paths on one handle (a signal handler and a `finally`, or two async tasks) can also call `detach()` at once: both read the Lease, the first releases it, and the second's write carries a stale resourceVersion and gets a 409. A timed-out detach write or a late renewal can change the Lease between read and write the same way.
- **What goes wrong:** Raising in these cases reports failure although the goal is met. Before 11/N, the second caller raised even though the release had succeeded. Raising also leaves the handle registered, so client cleanup would retry forever. Writing anyway would steal or recreate the Lease.
- **What we do:** In `SandboxBatch.detach`, if the first read finds no Lease, or a holder other than this handle, detach makes no write (logging at INFO for another holder), sets `_lease_released = True`, and unregisters the handle (`test_foreign_holder_lease_is_left_untouched`). The write runs in a loop of at most two attempts (`for attempt in range(2)`). On a 409, it re-reads. A missing Lease, no holder, or another holder all count as done, and the loop breaks (11/N). Only if this handle still holds the Lease does it write again ([LEASE-16](#lease-16)). `test_release_conflict_succeeds_only_once_the_lease_is_not_held` covers it.
- **Example:**
  ```text
  task A: read (rv=7, holder=me)    task B: read (rv=7, holder=me)
  task A: PUT holder=None -> rv=8
  task B: PUT holder=None @7 -> 409 -> re-read: holder=None -> done
  ```
- **Related:** [LEASE-16](#lease-16).

</details>

<a id="lease-16"></a>

## LEASE-16 After you stop a writer, assume one more of its writes may land

**Takeaway:** Cancelling a request on the client does not cancel it on the server. After you stop a writer, assume one more of its writes may land, and retry exactly once on a conflict so real conflicts still surface. The same rule applies to a take-before-destroy step: re-read, re-run every fence check, retry once, then treat a repeat as a real competitor.

<details>
<summary>The full story (11/N, 12/N, review on #1996)</summary>

- **Situation:** `detach()` cancels Lease renewal first, but an in-flight renewal (or a sync request that timed out on the client) can still be applied by the server afterwards and bump the resourceVersion. `release()` has the same problem in its per-round Lease take, a conditional replace (read, then PUT at the read's resourceVersion), where this handle's own renewal or detach can land late.
- **What goes wrong:** After a 409, the re-read shows this handle still holding the Lease, so `detach()` raised for a benign race with itself. In `release()`, raising on the first 409 would block a handle from releasing its own batch.
- **What we do:** `detach()` retries the release once on the fresh Lease, then raises if it still fails (Vicente's suggestion on #1996). `_take_batch_lease` in release does the same, tracked by a `retried` flag: on a 409 it re-reads and re-runs the full check (UID, leaseTransitions, holder; [LEASE-17](#lease-17)) before writing once more, so the retry cannot slip past the fence. A second 409 raises `BatchInUseError`. A non-409 error propagates to the round loop, which classifies it as transient or not (`test_release_retries_one_lease_conflict_then_raises_in_use`).
- **Example:**
  ```text
  t0  renewal PUT sent, client times out
  t1  detach(): stop renewal, read (rv=10, holder=me)
  t2  server applies the late renewal (rv=11)
  t3  detach PUT @10 -> 409 -> re-read (rv=11, holder=me) -> retry once -> ok
  ```
- **Related:** [LEASE-15](#lease-15), [LEASE-17](#lease-17), [LEASE-18](#lease-18). [LIB-6](lib.md#lib-6) covers why urllib3 retries are off for the Lease client (a retried write that landed comes back as 409).

</details>

<a id="lease-17"></a>

## LEASE-17 Fence release() on Lease UID plus leaseTransitions, not on the holder alone

**Takeaway:** To prove you still own an incarnation of a lease, compare stable epoch markers (UID plus a transition counter) with your last write. Pick a fencing token that only changes on a change of owner, so your own writes never fence you out. "Nobody holds it now" does not mean "nobody held it since me".

<details>
<summary>The full story (12/N (522dad4c), review round 2)</summary>

- **Situation:** `release()` may run after this handle detached, or after its Lease expired. Meanwhile another handle can attach and then detach, leaving a Lease with no holder. Or the batch can be reaped and recreated under the same id. A fencing token is a value that proves which owner "epoch" a write belongs to.
- **What goes wrong:** A holder-only check takes over any Lease with no holder. Two paths get past it. (1) A's `detach()` lands but A sees a timeout; B attaches and later detaches; A's `release()` then deletes B's parked batch. (2) The batch is reaped, someone recreates the same id and detaches it, and A's old handle deletes the new batch's identically labelled claims.
- **What we do:** `_take_batch_lease` (both shells) compares the fresh Lease with `last_lease`, the Lease from this handle's last successful write (`SandboxBatch.release` passes `self._lease`). A different `metadata.uid` means the Lease was recreated. A different `spec.leaseTransitions` means another handle attached, because only create (0) and takeover (+1) write it; renewals and detach don't. Either one raises `BatchInUseError` before any delete. The holder check stays for writers that don't bump transitions (kubectl, a buggy port). Rejected alternatives: comparing resourceVersion (refuses after the handle's own late renewal or landed detach), a new epoch annotation (duplicates leaseTransitions), and refusing release after any detach (breaks the supported release-after-failed-detach path). Any future Go `get_batch` must bump leaseTransitions like client-go does. Source: pr2_round2_decisions.md section 1. `test_release_deletes_nothing_while_another_handle_holds_the_lease` has rows for another holder, leaseTransitions bumped with no holder, and a new UID after a no-Lease round.
- **Example:**
  ```text
  A.detach() lands (A sees a timeout)       transitions=1, holder=None
  B.get_batch()                             transitions=2, holder=B
  B.detach()                                transitions=2, holder=None
  A.release(): uid same, 2 != 1 -> BatchInUseError, nothing deleted
  ```
- **Related:** [LEASE-3](#lease-3), [LEASE-14](#lease-14), [LEASE-16](#lease-16), [LEASE-18](#lease-18), [LEASE-20](#lease-20), [BP-18](../best-practices.md#bp-18).

</details>

<a id="lease-18"></a>

## LEASE-18 Re-take the Lease before every delete round, and delete it only at the version you wrote

**Takeaway:** When a long destructive operation runs without a background heartbeat, re-assert ownership right before each destructive step. Bind the final "delete the lock" step to the exact version you wrote in that same round, and if the take failed this round, do the reads but defer the finish. Never collapse "absent" and "unknown because the check failed" into one value: a completion step may run only after its precondition step positively succeeded in the same round.

<details>
<summary>The full story (12/N (522dad4c), review round 3)</summary>

- **Situation:** `release()` stops renewal, then runs deletecollection rounds that can take minutes for a large batch. Once Lease recreation was dropped ([LEASE-7](#lease-7)), the take step has three outcomes: took it, no Lease exists, or the request failed.
- **What goes wrong:** With renewal stopped, another client could attach mid-delete and lose its claims. A final unconditional Lease delete could remove a new holder's Lease. And if a round's take failed with a transient error, the round can still read a live count of 0, but it has no resourceVersion to delete the Lease at. If that failure were treated like "no Lease", the round would return success while the Lease still exists; finishing otherwise would delete it blindly.
- **What we do:** In `_delete_batch_objects`, each round first calls `take()` (`hold()` in the round loop), which writes the Lease with this handle as holder, a fresh `renewTime`, and the configured duration, conditional on the fresh read's resourceVersion. Every delete therefore goes out while this handle visibly holds the Lease, and the Lease stays fresh for `get_batch` and the reaper. `_take_batch_lease` returns `None` only for a confirmed 404 and lets every failure raise. `_delete_claims_in_rounds` sets `held` only after `hold()` returns, and calls `finish()` only when `live == 0 and held`; otherwise it goes around again. `finish()` deletes the Lease with a resourceVersion precondition equal to the last take, or skips the delete when the take found no Lease (no resourceVersion). A 409 on that delete counts as done (`k8s_helper.py: delete_batch_lease`), because the claims are already gone, and looping would let two concurrent `release()` calls undo each other's takes (pr2_round2_decisions.md #3). Tests: `test_release_takes_the_lease_before_each_round_and_deletes_it_at_the_version_written`, `test_release_with_no_lease_deletes_the_claims_and_creates_nothing['the take fails']`.
- **Example:**
  ```text
  take(rv=11) -> deletecollection
  take fails (transient)          -> count only, no finish
  take(rv=13) -> deletecollection -> live=0 -> DELETE lease @13
  ```
- **Model check:** "or no Lease exists" is a snapshot, not a fence. The TLA+ spec found a 19-state trace where a stalled `release()` reads no Lease, a new batch reuses the same caller-chosen id, and the stale round's label deletecollection deletes the new batch's claims. Scoping release deletes to the Lease UID passes. See [TLA-1](../tla.md#tla-1).
- **Related:** [LEASE-7](#lease-7), [LEASE-16](#lease-16), [LEASE-17](#lease-17), [LEASE-19](#lease-19). [K8S-1](k8s.md#k8s-1) covers why a 409 on the final Lease delete counts as deleted.

</details>

<a id="lease-19"></a>

## LEASE-19 During release, the per-round take is the renewal; check step time against everyone who acts on expiry

**Takeaway:** When teardown replaces a renewal loop with per-step re-takes, check the worst-case step time against every party that acts on expiry (attachers, reapers), and make the takeover path safe if expiry happens anyway. When two actors converge on the same teardown, make every step a delete that treats "already gone" as success, so interleavings cause only redundant no-ops, never failures.

<details>
<summary>The full story (12/N, 18/N)</summary>

- **Situation:** The renew thread is stopped during `release()`. Each round's `_take_batch_lease` rewrites `renew_time = now` and `lease_duration_seconds` to the original duration (verified in code), so it acts as one renewal per round. On a large batch the Lease can still go stale and be reaped mid-release.
- **What goes wrong:** A round can exceed a 60 s Lease: deletecollection up to 90 s, the count up to 90 s, backoff up to 30 s. The Lease can look expired mid-round. If either `release()` or the reaper treated the other's delete as an error, a correct teardown would raise or be reported as failed.
- **What we do:** No extra renewal thread. The safety argument (inferred, from the same bound check as old 6.10): `get_batch` refuses a Lease that is held but stale; the reaper deletes only after `renewTime + D + margin` (default 60 s, so at least 120 s by default, more than the 90 s maximum delete-retry time) and one unchanged renew interval; and the next round's take re-asserts ownership. If the reaper did delete the Lease, the next take returns `None` and the claims are still deleted (GC by ownerReference deletes them too). The structural reason the overlap is safe: both paths only delete. The reaper deletes only the Lease and counts a 404 as gone (`batch_reaper.py: _delete_lease_if_unchanged`). `release()` only deletes and ignores a 404 on its Lease delete (`k8s_helper.py: delete_batch_lease`). reaper_contract.md 'Coexistence with Renewal' describes this.
- **Related:** [LEASE-4](#lease-4), [LEASE-7](#lease-7), [LEASE-8](#lease-8), [LEASE-18](#lease-18).

</details>

<a id="lease-20"></a>

## LEASE-20 Losing ownership during release() is terminal: latch it and drop the handle from cleanup

**Takeaway:** Once a destructive operation learns it has lost ownership, latch that fact on the handle and drop it from every destructive retry path, keeping it only for local resource cleanup. A failed ownership check must never become retryable.

<details>
<summary>The full story (10/N, 12/N (522dad4c))</summary>

- **Situation:** `release()` (called by the user or by client cleanup) finds that another handle now holds the batch's Lease, or that it changed hands.
- **What goes wrong:** If the handle stayed registered and retryable, client exit cleanup or a user retry would keep probing, and might delete once the other owner detaches or its Lease lapses.
- **What we do:** `SandboxBatch.release` (and `AsyncSandboxBatch.release`) catches `BatchInUseError`, sets `_lease_released = True` and wakes waiters (`_changed.notify_all()`) under `_lock`, calls `_unregister_batch`, and re-raises. `_check_releasable_locked` then raises `BatchError` on every later call, before any read. In `sandbox_client.py: _release_batches`, cleanup logs a warning and stops tracking the handle (D1 item 4); if connections remain, `_track_closing` moves the handle to `_closing_batches`, where later cleanups only close connections. Tests assert that a second `release()` makes no `read_batch_lease` call (`test_release_deletes_nothing_while_another_handle_holds_the_lease`), and `test_cleanup_unregisters_a_handle_whose_batch_another_handle_holds` asserts one read and no deletes. Before 12/N's take and fence, release deleted by label without checking the holder (old 1.5).
- **Example:**
  ```text
  release() -> BatchInUseError   (_lease_released=True, unregistered)
  release() -> BatchError        (read_batch_lease not called)
  ```
- **Related:** [LEASE-17](#lease-17).

</details>

<a id="lease-21"></a>

## LEASE-21 Drain in-flight deletes while still renewing, then hand off

**Takeaway:** Hand off a lease only after every operation issued under it has finished. Refuse new work first, then keep renewing while you drain, so ownership never lapses under an in-flight write.

<details>
<summary>The full story (15/N)</summary>

- **Situation:** A `release_member` or `release_group` delete is in flight when `detach()`, `release()`, or async client cleanup starts.
- **What goes wrong:** Stopping renewal or handing off the Lease first lets a lingering delete land after another handle attaches, or after a batch is recreated under the same id. In async cleanup, the delete would also hit a closed HTTP session.
- **What we do:** First set `_detached` under `_lock`, which rejects new deletes. Then `_wait_active_deletes` waits for other threads' `_active_deletes` to reach 0 while renewal is still running. Only then stop renewal, write or delete the Lease, or close the session. `async_sandbox_client.py: _stop_batches` drains before `_stop_background_tasks` and the session close. `test_lifecycle_calls_wait_for_active_deletes_and_stop_further_deletes` asserts `_renew_thread.is_alive()` and `calls == ['delete']` while the delete is blocked.
- **Example:**
  ```text
  t0  release_member: DELETE in flight
  t1  detach(): _detached=True; wait (renew thread alive)
  t2  DELETE returns -> stop renewal -> PUT lease holder=None
  ```
- **Related:** [LEASE-11](#lease-11), [CLEAN-10](clean.md#clean-10). The per-thread counter that avoids signal-handler self-deadlock is [CANCEL-12](cancel.md#cancel-12); [LOCK-19](lock.md#lock-19) and [PAT-16](../patterns.md#pat-16) cover the sync/async drain differences.

</details>

<a id="lease-22"></a>

## LEASE-22 Exit cleanup follows the caller's latest intent: finish a half-done handoff, retry a started release

**Takeaway:** Cleanup must tell "abandoned" from "in the middle of a handoff" and honor the caller's latest intent. Encode that intent in state that later calls overwrite, and have implicit cleanup follow it over the default derived from how the handle was obtained.

<details>
<summary>The full story (10/N, 12/N, 13/N)</summary>

- **Situation:** Two cases. (1) `detach()` failed at the Lease write and left the handle registered; later client cleanup saw it as abandoned and called `release()`, which deletes the batch. (2) An attached handle (from `get_batch`) normally gets detached on exit, but the caller may have called `detach()` (failed) and then `release()` (also failed).
- **What goes wrong:** (1) Cleanup deleted a batch the caller was handing off. (2) Detaching on exit would reverse the caller's latest explicit intent (delete) and hand off a batch the caller tried to destroy.
- **What we do:** `detach()` stores the caller's grace on the handle (`_detach_grace`). Cleanup retries `detach()` with that grace (`_finish_detach`, only while `_detach_grace` is set) and never releases a half-detached handle. `release()` clears the stored grace, so the latest call wins. `_exit_detaches()` is `_attached and not _release_started()`, where release-started means `_detached` with `_detach_grace` None. So after a `release()` attempt, every cleanup Lease write takes the Lease (release), never a detach. `test_cleanup_releases_a_handle_whose_release_failed_after_a_failed_detach` checks the written holders are `[self, self]`.
- **Limits:** This is a safety net. Cleanup only runs on explicit close/`delete_all`, a context exit, or atexit with `cleanup=True`. Async atexit cannot await `detach()`. After a crash nothing runs, the Lease expires, and the reaper frees the batch. A late retry either completes the handoff or quietly finds the batch already reaped ([LEASE-15](#lease-15)). `get_batch` then fails clearly (`BatchLeaseExpiredError` / `BatchNotFoundError`) rather than returning a cleaned-up batch.
- **Example:**
  ```text
  detach(42) fails -> release() gets 403 -> exit cleanup: release() again
  written holders == [self, self]   (no detach write)
  ```
- **Related:** [LEASE-6](#lease-6), [LEASE-23](#lease-23). The "created vs attached" exit rule is [CLEAN-11](clean.md#clean-11); [LOCK-3](lock.md#lock-3) covers carrying the detach grace in a local, which release() clears.

</details>

<a id="lease-23"></a>

## LEASE-23 Closing the client is neither release nor handoff: stop keepalives and let expiry reclaim

**Takeaway:** Give "close the client" its own semantics, separate from destroy and handoff. Stop keepalives and let the lease-expiry path reclaim server state, and make later handoff attempts fail loudly instead of acting on a dead session.

<details>
<summary>The full story (9/N, 10/N, 12/N)</summary>

- **Situation:** `AsyncSandboxClient.close()` closes the API session while tracked batches still have running tasks.
- **What goes wrong:** Leaving the tasks running would renew the Lease for the life of the event loop, so the batch would never go stale. Releasing on `close()` would delete batches the caller didn't ask to delete, and detaching would make an unintended handoff.
- **What we do:** `async_sandbox_client.py: _stop_batches` first finishes unfinished detaches (`_finish_detach`). Then it sets `_detached` and `_stop_creating` under `batch._lock` and leaves `_detach_grace` None. That is the same encoding as "release started" (`_release_started`), so a later `detach()` raises `BatchError('...or its client was closed')`. It stops the tasks, closes connections, and leaves the Lease held. `get_batch` can't re-attach until the Lease is stale, and then the reaper and GC clean up. `test_close_stops_tracked_batches_and_closes_their_connections` covers it.
- **Example:**
  ```python
  await client.close()
  await batch.detach()   # raises BatchError
  ```
- **Related:** [LEASE-6](#lease-6), [LEASE-22](#lease-22). [CLEAN-10](clean.md#clean-10) covers why cleanup must stop new resources from being created.

</details>

<a id="lease-24"></a>

## LEASE-24 Claim ordinals are a high-water mark that survives failures and handoff

**Takeaway:** For deterministic names of objects that can outlive their logical release, use a high-water-mark counter that never goes backwards, persist it at handoff, and on recovery take the max of the persisted mark and every name you observe. Snapshot the counter only after new allocation is fenced off, so the persisted value is final.

<details>
<summary>The full story (16/N (dc9ea439))</summary>

- **Situation:** Each `acquire()` names its claim `<batch>-<ordinal>`. A released or timed-out claim may still be terminating when a successor is created, and once deleted it no longer appears in a re-attach list. A create that never landed leaves no listed claim either.
- **What goes wrong:** Reusing an ordinal would make a create collide with a terminating claim (409, read as "already mine") or adopt a foreign claim. A re-attached handle that derived the next ordinal only from listed claims would reuse ordinals of deleted claims.
- **What we do:** `batch_state.py: BatchState.start_acquire` increments `_next_ordinal` and never decrements it, even when the acquire fails. `upsert_claim` sets `_next_ordinal = max(_next_ordinal, ordinal + 1)` for every claim the watch sees. `detach()` reads `_state.next_ordinal` under `_lock` in the same critical section that sets `_detached`; `acquire()` takes no ordinal after that, so the value is final (the code comment says so). It writes the value as `BATCH_NEXT_ORDINAL_ANNOTATION` (`batch-next-ordinal`) on every attempt of the `replace_batch_lease` that clears the holder. `get_batch` seeds `BatchState(..., max(size, recorded))` via `parse_lease_metadata` (minimum 0). Tests: `TestOrdinals.test_claim_names_continue_past_the_fill_listed_claims_and_a_recorded_ordinal`, `test_reattached_handle_reads_back_ordinals_labels_and_settings`, `test_final_write_clears_holder_sets_renew_time_and_grace_duration`, and e2e `test_acquire_adds_members_and_continues_ordinals_after_detach`.
- **Example:**
  ```text
  acquire -> b-1 Ready
  acquire(unready) -> b-2 times out, deleted
  detach; get_batch; acquire -> b-3 (not b-2)
  ```
- **Related:** [LEASE-25](#lease-25), [PAT-7](../patterns.md#pat-7). [WATCH-5](watch.md#watch-5) covers advancing the ordinal allocator from every observed claim name; [LOCK-5](lock.md#lock-5) covers why detach's recorded value is final.

</details>

<a id="lease-25"></a>

## LEASE-25 Treat a corrupt recovery value differently from a missing one

**Takeaway:** When recovery state that protects an invariant is read from a shared object, default a missing value but refuse a corrupt one.

<details>
<summary>The full story (16/N (dc9ea439))</summary>

- **Situation:** `get_batch` reads `batch-next-ordinal` (and the other settings) from Lease annotations that anyone with Lease write access could edit.
- **What goes wrong:** Treating an unparsable value as the default 0 would silently fall back to max(size, seen ordinals) and could reuse ordinals of deleted `acquire()` claims.
- **What we do:** `batch_utils.py: _parse_int_annotation` returns the default only when the key is absent. A non-int or below-minimum (0) value raises `BatchError`, so the attach fails loudly before any write ([LEASE-14](#lease-14)). The 16/N reply notes that only `detach()` clears `holderIdentity`, and it writes the annotation in that same write, so a re-attachable Lease always carries it.
- **Related:** [LEASE-14](#lease-14), [LEASE-24](#lease-24), [LEASE-30](#lease-30), [API-8](api.md#api-8). [API-26](api.md#api-26) covers failing closed on partial or conflicting group annotations.

</details>

<a id="lease-26"></a>

## LEASE-26 Persist every creation input a successor needs on the Lease

**Takeaway:** If successors must create objects identical to the original holder's, persist every input to that creation (labels, rate limits, TTL budgets) on the coordination object, and document any knob that intentionally falls back to a default.

<details>
<summary>The full story (16/N (dc9ea439), 19/N (2ecdf384))</summary>

- **Situation:** A `get_batch` handle calls `acquire()` on a batch another process created.
- **What goes wrong:** Without the original labels, `create_rps`, and budgets, the successor's claims would miss caller labels (breaking caller selectors), pace at a different rate, and get different shutdownTimes.
- **What we do:** 16/N added caller labels and `batch-create-rps` to the Lease. `batch_utils.py: batch_lease_metadata` writes and `parse_lease_metadata` reads the settings in one place: labels (minus batch-id and created-by), `create_rps`, `quorum_timeout`, `work_budget` (plus `next_ordinal`, which `detach()` writes and `parse_lease_metadata` reads). `SandboxBatch._attach` sets `_create_interval = 1 / settings.create_rps` and `_labels`. Commit 2ecdf384 notes that `max_in_flight` is not stored, so an attached handle uses the default 20, as the `acquire()` docstring says. `test_reattached_handle_reads_back_ordinals_labels_and_settings` covers it.
- **Example:**
  ```text
  attached handle: acquire -> b1-7 with {'team': 'rl', batch-id, batch-group}
  two concurrent acquires start > 0.15 s apart at create_rps=5
  ```
- **Related:** [LEASE-24](#lease-24), [LEASE-27](#lease-27), [LEASE-30](#lease-30) (the configured duration, kept in an annotation because detach overwrites `spec.leaseDurationSeconds` with the grace).

</details>

<a id="lease-27"></a>

## LEASE-27 Count each claim's backstop shutdownTime from its own create, and include pacing in the fill's deadline

**Takeaway:** Compute a backstop TTL relative to each object's own creation time, not a group's start, and persist the inputs so every future holder computes it the same way. When one deadline is computed up front for work that a client-side rate limiter spreads over time, add the limiter's worst-case drain time, or late items get less budget than early ones.

<details>
<summary>The full story (12/N (522dad4c), 16/N (dc9ea439))</summary>

- **Situation:** shutdownTime is a field on each claim after which the controller deletes it, even if nobody else does. Every fill claim carries one absolute shutdownTime as a crash backstop. `acquire()` adds replacements throughout a long rolling run.
- **What goes wrong:** (1) At create_rps=50, a 10k-claim fill takes 200 s just to send. Quorum can arrive quorum_timeout after the last create, so a fill deadline that ignored pacing would silently eat into work_budget. (2) A replacement created an hour in that reused the fill's deadline would inherit one that has nearly passed and be deleted mid-task (batch_claim_proposal.md line 125).
- **What we do:**
  - Fill: `SandboxBatch._claim` computes one `claim_lifecycle` for the whole fill: start + `ceil(size / create_rps)` + `quorum_timeout` + `work_budget` + `BATCH_SHUTDOWN_MARGIN_SECONDS` (600 s). The code comment says the pacing time counts too, "or it would eat into work_budget". `test_claim_manifests` checks it ('counting ceil(3 / 2.5) seconds of pacing').
  - Replacements: `SandboxBatch.acquire` computes `construct_sandbox_claim_lifecycle_spec(quorum_timeout + work_budget + BATCH_SHUTDOWN_MARGIN_SECONDS)` locally (no API call), after the pacing wait so it counts from the send, and reuses the same value across retries. `quorum_timeout` and `work_budget` come from Lease annotations, so a re-attached handle computes the same thing ([LEASE-26](#lease-26)). Tests: `test_creates_a_claim_outside_the_fill...` and `test_reattached_handle_reads_back_ordinals_labels_and_settings` (shutdownTime 2026-01-01T01:13:30Z).
- **Related:** [LEASE-26](#lease-26).

</details>

<a id="lease-28"></a>

## LEASE-28 Renewal continues after a conflict only on proof that this handle still holds the Lease

**Takeaway:** After a conditional-write conflict, continue only on positive proof that you still own the resource. Treat "no owner" the same as "another owner" when you could not have cleared it yourself, and never recreate or overwrite.

<details>
<summary>The full story (3/N (31a47728))</summary>

- **Situation:** A renewal write (`_write_renewal` from the last written Lease, [LEASE-2](#lease-2)) fails with 404, or with 409 followed by a read. A 409 can mean another holder took the Lease, or that an earlier renewal whose response was lost did land.
- **What goes wrong:** Overwriting a Lease that another handle took would steal the batch. Recreating a deleted one would get a new UID, so GC would still collect the claims through their ownerReferences ([LEASE-7](#lease-7)).
- **What we do:** In `SandboxBatch._renew_once`:
  - A 404 on the PUT, or `None` from the read after a 409, means the Lease is gone: `_stop_renewal(self._lease_gone_error())` records `BatchLeaseExpiredError` and stops.
  - A holder that differs from this handle's identity, including `None` (another handle attached and then detached), means the Lease was lost: `_stop_renewal` records `BatchInUseError` ("Lease is no longer held by this handle") and stops, with no write.
  - Only when the read shows this handle as holder (its own late renewal landed) does it write again from the fresh Lease.
  - Any other error marks renewal degraded and the loop retries.

  Both stop paths set `err()` and keep `lease_degraded()` True, and neither ever calls create. Tests: `test_holder_mismatch_stops_renewal_without_writing` (with a `None` holder subTest), `test_missing_lease_stops_renewal_and_records_expired`.
- **Example:**
  ```text
  PUT @rv=20 -> 409 -> GET: holder=None   -> err()=BatchInUseError, no write
  PUT @rv=20 -> 409 -> GET: holder=me     -> PUT @rv=21 -> ok
  ```
- **Related:** [LEASE-2](#lease-2), [LEASE-6](#lease-6), [LEASE-7](#lease-7), [LEASE-12](#lease-12), [LEASE-17](#lease-17).

</details>

<a id="lease-29"></a>

## LEASE-29 Bound every call between creating the Lease and starting its renewal

**Takeaway:** Account for every blocking call between acquiring a lease and starting its heartbeat. Either start the heartbeat first or bound those calls well below the lease duration.

<details>
<summary>The full story (12/N (522dad4c), 22/N)</summary>

- **Situation:** In `claim_batch`, `SandboxBatch._claim` takes `renewed_at` and creates the Lease before it lists claims ([LEASE-1](#lease-1)), and the renew thread starts only afterwards, in `_start`. In `get_batch`, `_attach` lists claims before it writes the Lease.
- **What goes wrong:** A list that takes longer than D before renewal starts lets the new Lease expire at birth. Old 3.17 notes that urllib3 read retries on this list could reach 4 x 30 s = 120 s, more than a 60 s Lease.
- **What we do:** `_claim`'s post-create list passes `_request_timeout=BATCH_REQUEST_TIMEOUT_SECONDS` (`30`) and `deadline=lease_expires_at`, and `_claim_list_pages` (`k8s_helper.py`, `async_k8s_helper.py`) clamps each page's `_request_timeout` to `min(page_timeout, deadline - time.monotonic())` with no transport retries (22/N; see [LEASE-33](#lease-33)). `_attach` lists before writing the Lease and takes `renewed_at` after the list ([LEASE-14](#lease-14)), so it can afford `BATCH_COLLECTION_REQUEST_TIMEOUT_SECONDS` (90 s). The timeout choice follows from whether the Lease clock is already running.
- **Example:**
  ```text
  claim_batch: POST lease (clock starts) -> LIST claims, 30s timeout -> start renewal
  get_batch:   LIST claims, 90s timeout -> PUT lease (clock starts) -> start renewal
  ```
- **Related:** [LEASE-1](#lease-1), [LEASE-5](#lease-5), [LEASE-14](#lease-14). [LIB-9](lib.md#lib-9) covers the urllib3 retry risk.

</details>

<a id="lease-30"></a>

## LEASE-30 Keep the configured duration in an annotation, because detach overwrites spec.leaseDurationSeconds with the grace

**Takeaway:** When one field does double duty (a handoff window now, a renewal contract later), store the durable setting separately and restore it on takeover, rather than trusting whatever the last writer left in the shared field.

<details>
<summary>The full story (1/N (3a1607bc), 12/N)</summary>

- **Situation:** `detach(grace)` writes `spec.leaseDurationSeconds = grace`, so the parked batch stays adoptable (and unreaped) for exactly the handoff window ([LEASE-9](#lease-9)).
- **What goes wrong:** If the next holder inherited `spec.leaseDurationSeconds`, a 300 s handoff grace would silently become the batch's renewal duration, or a short grace would make renewal too tight.
- **What we do:** `batch_utils.py: batch_lease_metadata` writes the batch's real duration as `BATCH_LEASE_DURATION_ANNOTATION` at `claim_batch`. `SandboxBatch._attach` judges staleness with the spec value (the grace window), but the new holder writes and renews with `settings.lease_duration` from the annotation. `parse_lease_duration_annotation` falls back to `BATCH_DEFAULT_LEASE_DURATION_SECONDS` (60) when the annotation is absent and raises `BatchError` before any write when it is not a valid integer or does not exceed the skew margin. Each renewal (`_write_renewal`) resets the spec to `_lease_duration`. Tests: `test_annotation_duration_overrides_leftover_spec_duration_on_takeover`, `test_no_annotation_takeover_writes_default_duration`.
- **Example:**
  ```text
  Lease: spec.leaseDurationSeconds=300 (leftover grace), annotation=90
  get_batch -> staleness judged with 300; takeover writes spec=90; handle renews for 90
  ```
- **Related:** [LEASE-9](#lease-9), [LEASE-14](#lease-14), [LEASE-25](#lease-25), [LEASE-26](#lease-26).

</details>

<a id="lease-31"></a>

## LEASE-31 A caller that never reuses batch IDs or detaches may fall back to direct delete on BatchError

**Takeaway:** A general SDK handle must refuse by-name deletes once its lease may be lost, because another holder or a recreated batch can own those deterministic names. A caller that generates unique IDs and never hands off ownership can safely catch that health error and run a 404-tolerant direct delete.

<details>
<summary>The full story (25/N (92115dd8))</summary>

- **Situation:** In `examples/agent-sandbox-rl`, `SandboxHandle.release()` calls `BatchClaim.release()`, which delegates to `self.batch.release_member(self.member)`. A handle handed out earlier can be released after its batch stopped (`batch.err()` set, so `BatchClaims` opened a fresh batch for new acquires) or after `fleet.teardown()` called `batch.release()`.
- **What goes wrong:** `SandboxBatch.release_member` raises `BatchError` once `err()` is set or `release()` has run ([LEASE-11](#lease-11)). Letting `BatchError` escape `handle.release()` fails caller cleanup and, if `batch.err()` came from a watch failure or `teardown()`'s `batch.release()` failed transiently, leaves the claim running until `shutdownTime` or reaper expiry. Catching `Exception` broadly, on the other hand, would swallow real `403` or `503` delete errors and fire a duplicate unguarded DELETE.
- **What we do:** `batch_claims.py: BatchClaim.release` catches `BatchError` (and only `BatchError`, which `release_member` raises if and only if `_check_healthy()` fails) and calls `self.cluster.resources.delete_claim(self.member.claim_name)`, which treats `404` as success. That fallback is safe here because `BatchClaims` never passes a fixed `batch_id` (so `claim_batch` generates a random ID) and never calls `detach()` or `get_batch()`, so no other holder or recreated batch can ever own `<batch_id>-<ordinal>`. Tests: `test_batch_claims.py: test_stopped_batch_is_replaced_and_old_members_still_release`, `test_claim_landing_after_teardown_falls_back_to_direct_claim_delete`.
- **Example:**
  ```python
  try:
      self.batch.release_member(self.member)
  except BatchError:
      self.cluster.resources.delete_claim(self.member.claim_name)
  ```
- **Related:** [LEASE-11](#lease-11), [CLEAN-24](clean.md#clean-24), [TEST-17](../testing.md#test-17).

</details>

<a id="lease-32"></a>

## LEASE-32 Stamp a local lower-bound expiry before create, and rotate recycled claims before shutdownTime

**Takeaway:** When a pooled worker reuses an object that carries a fixed server-side TTL, stamp a conservative local expiry from the monotonic clock *before* creating the object, and retire the object once the remaining time falls below a safety multiple of observed task duration plus reset cost.

<details>
<summary>The full story (25/N (8cfb221f))</summary>

- **Situation:** Every claim from `SandboxBatch.acquire` gets a fixed `spec.lifecycle.shutdownTime` (`now + quorum_timeout + work_budget + BATCH_SHUTDOWN_MARGIN_SECONDS`, [LEASE-27](#lease-27)). In RL sandbox recycling (`recycle.py`), one claimed sandbox runs many reset-and-rollout tasks back to back instead of releasing after one episode.
- **What goes wrong:** Per-claim `create_sandbox` sets no `shutdownTime`, so the recycler originally rotated only on `max_reuses` or a dirty reset. Holding a batch claim across many tasks lets wall-clock time reach `shutdownTime`, and the controller deletes the claim mid-episode. Recording `expires_at` from `time.monotonic()` *after* `batch.acquire()` returns can also overestimate the remaining life if readiness took time after the server stamped `shutdownTime`.
- **What we do:** `batch_claims.py: BatchClaims.acquire` reads `started = time.monotonic()` before `batch.acquire(...)` and sets `expires_at = started + self._ready_timeout + self._settings.work_budget + BATCH_SHUTDOWN_MARGIN_SECONDS`, so `expires_at` is never later than the claim's real `shutdownTime`. `SandboxHandle.claim_expires_at` exposes that deadline (`None` for per-claim handles). Before each reuse, `recycle.py: _claim_expiring` rotates the claim like a `max_reuses` hit when `expires_at - time.monotonic() < 2 * longest_task_s + reset_timeout`, leaving twice the group's longest task so far for runtime variance. Tests: `test_batch_claims.py: test_recycle_rotates_a_batch_claim_before_it_expires` and `test_async_recycle_rotates_a_batch_claim_before_it_expires`.
- **Example:**
  ```python
  started = time.monotonic()
  member = batch.acquire(pool, timeout=self._ready_timeout, pod_annotations=pod_annotations)
  expires_at = started + self._ready_timeout + self._settings.work_budget + BATCH_SHUTDOWN_MARGIN_SECONDS
  ```
- **Related:** [LEASE-27](#lease-27).

</details>

<a id="lease-33"></a>

## LEASE-33 Bound multi-page setup across pages before renewal starts, and fail closed on missing rollback preconditions

**Takeaway:** A per-request `_request_timeout` bounds only one page of a `continue`-token list, so pass an overall Lease `deadline` across pages whenever listing runs after creating a Lease and before its renewal loop starts. When rolling back that Lease on setup failure, require a non-empty `resourceVersion` rather than downgrading `None` to an unpreconditioned delete.

<details>
<summary>The full story (22/N)</summary>

- **Situation:** During `claim_batch`, `SandboxBatch._claim` creates the batch Lease first so existing or concurrently created claims cannot slip past the pre-create check ([LEASE-1](#lease-1)), then calls `list_sandbox_claim_objects` (which pages in `CLAIM_LIST_PAGE_SIZE = 500` chunks via `k8s_helper.py: _claim_list_pages`) before `_start` launches `_renew_loop`. If setup fails (for example, pre-existing claims exist or listing times out), `_claim` rolls back the Lease with `delete_batch_lease`.
- **What goes wrong:** Passing `_request_timeout=BATCH_REQUEST_TIMEOUT_SECONDS` (`30`) alone bounds each page individually, so a short `lease_duration` (`< 30 s`), a multi-page list (`N` pages taking up to 30 s each), or a process pause right after the list returns can run past `renewed_at + lease_duration` while the reaper deletes the unrenewed Lease and another `claim_batch` creates a replacement Lease under the same `batch_id` (`BatchLease.tla`: `neg_claimNoExpiryCheck`, 10 states). Worse, `delete_batch_lease(..., resource_version=None)` means an unpreconditioned delete; if `create_batch_lease` returned an empty or missing `metadata.resource_version` and `_claim` passed `created_rv or None`, rollback would delete the successor's replacement Lease unconditionally (`neg_unconditionalRollbackDelete`, 23 states).
- **What we do:** `SandboxBatch._claim` passes `_request_timeout=BATCH_REQUEST_TIMEOUT_SECONDS` (`30`) and `deadline=lease_expires_at` (`renewed_at + lease_duration`), and `_claim_list_pages` (`k8s_helper.py`, `async_k8s_helper.py`) recomputes `remaining = deadline - time.monotonic()` before every page, clamps each page's `_request_timeout` to `min(page_timeout, remaining)`, and raises `TimeoutError` once `remaining <= 0`. After the list returns, `_claim` re-checks `time.monotonic() >= lease_expires_at`, converts deadline-expired timeouts and transport errors into `BatchLeaseExpiredError`, validates a non-empty `created_rv` and `lease_uid` up front (raising `BatchError` before any unpreconditioned rollback can run), and passes `resource_version=created_rv` to rollback `delete_batch_lease` (22/N, `sandbox_batch.py: SandboxBatch._claim`, `k8s_helper.py: _claim_list_pages`; verified by `ClaimSetupFencing` in `BatchLease.tla`).
- **Example:**
  ```text
  POST Lease (uid=1, rv=10, expires at t0+60)
  LIST page 1 (25s) -> LIST page 2 clamped to 35s -> stalls to t0+60 -> TimeoutError
  rollback DELETE Lease precondition rv=10 -> 409 if reaper+h2 replaced it (h2 safe)
                                           -> BatchLeaseExpiredError raised to h1
  ```
- **Related:** [LEASE-1](#lease-1), [LEASE-29](#lease-29); [TLA-3](../tla.md#tla-3).

</details>

---

[Learnings home](../../learnings.md) · [Field guide index](../../learnings.md#sec-field-guide)
