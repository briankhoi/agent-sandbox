<a id="sec-k8s"></a>

[Learnings home](../../learnings.md) · [Field guide index](../../learnings.md#sec-field-guide)

# Kubernetes API semantics that bit us (or could have)

This section covers how the apiserver, the garbage collector, and controllers actually behave, as opposed to how they seem to behave from a quick read of the API. The one big idea: the cluster is a shared, eventually consistent database. Every object can change under you, any read can be stale, and an object's identity is its UID, not its name. Most entries are about one of those three facts. Read the takeaways first. Open "The full story" when you want the concrete case and the code pointers.

<a id="k8s-1"></a>

## K8S-1 Make contended deletes conditional, and read a 409 as "someone else owns it now"

**Takeaway:** When you delete an object that others may write, send a precondition on the version you last wrote (or the UID). If the server answers 409, the object moved on without you, so stop instead of retrying.

<details>
<summary>The full story (12/N, 18/N)</summary>

- **Situation:** Kubernetes uses optimistic concurrency. Every object has a `resourceVersion` (a version stamp the apiserver bumps on each write), and a write or delete can carry a *precondition* that says "only do this if the object is still at this version". If the object has changed, the apiserver answers 409 Conflict. `release()` deletes the batch Lease last, after the claims are gone, and the reaper deletes stale Leases. Meanwhile another handle may have taken over, detached, or recreated the batch.
- **What goes wrong:** An unconditional delete would remove a Lease that another holder just wrote. The garbage collector would then delete that holder's claims through their ownerReferences ([K8S-2](#k8s-2)). On the other side, looping on the 409 would let two concurrent `release()` calls on one handle undo each other's takes until one delete landed, and raising would report failure after the claims were already gone.
- **What we do:** `k8s_helper.py: K8sHelper.delete_batch_lease` (and its async twin) passes `V1DeleteOptions(preconditions=V1Preconditions(resource_version=...))` with the resourceVersion from this handle's last write. It treats a 404 as done, and it treats a 409 as done only when a precondition was sent. The newer Lease stays in place for its writer or the reaper. Round 2 of the 12/N review (pr2_round2_decisions.md section 3) checked what can cause that 409. A late renewal can't, because renewals are conditional PUTs carrying an older resourceVersion and the renew thread is joined before the first take. The real causes are an outside writer, a concurrent `detach()`, or a second concurrent release on the same handle, and the lifecycle lock later removed the same-handle cases.
- **Example:**
  ```python
  delete_namespaced_lease(name, ns, body=V1DeleteOptions(
      preconditions=V1Preconditions(resource_version=last_written_rv)))
  # 404 -> already gone; 409 (with precondition) -> not ours to delete
  ```
- **Related:** [K8S-2](#k8s-2), [K8S-17](#k8s-17). The reaper deliberately does not reuse this helper, because it must tell a 409 apart from a delete (see [K8S-17](#k8s-17)). Lease writes themselves use PUT for the same reason (Lease section).

</details>

<a id="k8s-2"></a>

## K8S-2 Garbage collection matches owners by UID, so never "heal" a missing owner

**Takeaway:** Once dependents point at an owner through an ownerReference, the owner's identity is its UID, not its name. Copy the UID from the server's response, and never recreate a missing owner under the same name.

<details>
<summary>The full story (12/N, 18/N)</summary>

- **Situation:** An *ownerReference* is a pointer in an object's metadata to its owner (apiVersion, kind, name, uid). The *garbage collector* (GC) is a controller that deletes dependents whose owner no longer exists. Every fill claim and every `acquire()` claim carries an ownerReference to the batch Lease, so the claims go away when the Lease does, including after a crash and after a re-attach through `get_batch`.
- **What goes wrong:** The GC checks the UID, not just the name. A reference whose UID does not match the live object counts as a missing owner, and the GC deletes the dependent at once. So a "helpful" recreate of a deleted Lease (same name, new UID) would delete live work. A recreated Lease would also lack the batch annotations (pr2_round2_decisions.md #4). Separately, setting `blockOwnerDeletion=true` needs extra permission on the owner, which the OwnerReferencesPermissionEnforcement admission plugin checks.
- **What we do:** `batch_utils.py: lease_owner_reference` builds a plain reference `{apiVersion: coordination.k8s.io/v1, kind: Lease, name, uid}` with no `controller` and no `blockOwnerDeletion`, so it needs no extra RBAC. It raises `BatchError` if name or uid is missing rather than write a bad reference. `SandboxBatch.__init__` / `AsyncSandboxBatch.__init__` build `_owner_references` from the Lease the handle holds: the create response in `claim_batch`, or the takeover in `get_batch` (a replace keeps the UID). No path recreates the Lease: `_renew_once` stops on 404, and `_take_batch_lease` returns None on 404 so `release()` deletes the leftover claims by label and creates nothing. An earlier draft of 12/N had `release()` create a missing Lease. Round 2 of the 12/N review dropped that, because it contradicted the proposal and broke the UID fence.
- **Example:**
  ```
  t0  Lease batch-b1 uid=A; claims b1-0, b1-1 owned by uid=A
  t1  Lease deleted
  t2  someone recreates batch-b1 -> uid=B
  t3  GC: claims point at uid=A, which is gone -> deletes them
  ```
- **Related:** [K8S-3](#k8s-3), [K8S-1](#k8s-1). Tests: `test_fill_and_acquired_claims_are_owned_by_the_lease_the_handle_holds`, `test_missing_lease_stops_renewal_and_records_expired`, `test_release_with_no_lease_deletes_the_claims_and_creates_nothing`, e2e `test_deleting_a_batch_lease_garbage_collects_its_claims` (checks the UID against the live Lease).

</details>

<a id="k8s-3"></a>

## K8S-3 For crash cleanup, delete the root object and let the GC cascade

**Takeaway:** To clean up after a dead owner, delete the root and let ownerReference GC remove the rest by UID. A name- or label-based sweep can hit a newer object that reuses the name, and needs broader permissions. Measure before you keep a faster manual cascade.

<details>
<summary>The full story (18/N)</summary>

- **Situation:** An earlier design had the reaper delete the Lease and then run label-scoped deletecollection rounds over the claims until none were left.
- **What goes wrong:** Re-listing claims by label until none are left would also delete the claims of a new batch that reuses the id in that window. It would also need `list` and `deletecollection` on claims, which would let the reaper delete claims that belong to no batch.
- **What we do:** Option B (approved 2026-10-19): `batch_reaper.py: reap` deletes only the Lease, and the GC deletes the claims (and their Sandboxes and Pods) by owner UID, so a newer batch with the same id is never touched. The review measured that the reaper's own claim delete freed pods only 12-15% sooner: 351 s vs 400 s for 2,000 members, and 25.2 s vs 29.7 s for 200 (the GC runs at about 20 claims/s). Sandboxes and Pods go through the GC on both paths anyway. The review also cited the CronJob controller, which deletes Jobs and leaves their Pods to the GC. Rejected: keeping the reaper's claim delete, a single deletecollection without re-listing, and a foreground Lease delete. `release()` keeps its own deletecollection because a caller is waiting on it.
- **Related:** [K8S-2](#k8s-2), [K8S-8](#k8s-8), [K8S-20](#k8s-20). Test `test_waits_each_leases_renew_interval_then_deletes_only_the_leases` asserts the helper saw only Lease list and delete calls. Source: reaper_contract.md "Why the Reaper Leaves Claims to the Collector".

</details>

<a id="k8s-4"></a>

## K8S-4 A terminating object (deletionTimestamp set) is already gone

**Takeaway:** Treat an object with `deletionTimestamp` set as deleted for readiness and "what's left" decisions. Controllers stop updating a terminating object, so its status is stale.

<details>
<summary>The full story (5/N, 12/N, 20/N)</summary>

- **Situation:** When you delete an object that has *finalizers* (keys that block removal until some controller finishes cleanup) or a grace period, it stays visible for a while with `metadata.deletionTimestamp` set. Claims can do this, though they carry no finalizer of their own (pr8_plan.md).
- **What goes wrong:** The controller stops reconciling a terminating claim, so its Ready condition can stay True until the object is gone. Before 5/N, `members()` kept reporting such a claim READY with old pod IPs, and `connect()` could hand out a connection to a sandbox being torn down. At release time, refs/benchmark_rerun_review.md section 7 measured the claims gone 20.1 s after `release()` returned and estimated that waiting for an empty list would make release about 20 s slower. (pr8_plan.md later found the re-list near empty in those runs, so how long claims stay listed while terminating is unsettled.)
- **What we do:** `batch_state.py: derive_member` turns `deletionTimestamp` into LOST, the same state a DELETED event gives, unless the member already FAILED. It clears reason, message, pod IPs, and service FQDN, so `still_reaches()` fails and the cached connection closes when deletion starts. The later DELETED event changes nothing. In `_delete_claims_in_rounds`, `count_live_sandbox_claims` (both helpers) counts only items with no `deletionTimestamp`, because a terminating claim's delete was already accepted. A `limit=1` probe can't work, because a CRD list can't field-select on `deletionTimestamp`.
- **Example:**
  ```
  MODIFIED {deletionTimestamp: t, Ready: True, podIPs: [10.0.0.1]}
    -> Member(state=LOST, pod_ips=())
  ```
- **Related:** [K8S-5](#k8s-5). Test `test_terminating_claim_counts_as_deleted`.

</details>

<a id="k8s-5"></a>

## K8S-5 Use consistent reads for decisions, not the watch cache

**Takeaway:** A list served from the apiserver's cache can be behind. Use a consistent read (no `resourceVersion`) whenever the answer drives a "nothing left" decision or feeds a conditional write, unless you have measured a real list-cost problem.

<details>
<summary>The full story (12/N, 18/N, 20/N)</summary>

- **Situation:** A list with no `resourceVersion` is a *consistent read*: the apiserver guarantees it reflects every write so far. A list with `resourceVersion="0"` may be answered from the apiserver's *watch cache* (an in-memory copy fed by etcd), which is cheaper but can lag. Two places list for decisions: `release()` between delete rounds ("is anything live left?"), and the reaper ("which Leases are stale?").
- **What goes wrong:** For `release()`, a cached list could report "none left" too early. For the reaper, reaper_contract.md and batch_claim_proposal.md proposed `resourceVersion="0"`, arguing that the delete precondition makes a stale answer harmless. Safety does hold (the delete would get 409). But a cached list can show an older resourceVersion and renewTime, so a dead batch whose last renewal had not reached the cache would be skipped until a later run (inferred). With RV=0, older apiservers may also serve from the watch cache and ignore `limit`, which defeats paging (inferred; not found in the notes).
- **What we do:** `sandbox_batch.py: _delete_claims_in_rounds` (comment "A consistent read") and `K8sHelper.count_live_sandbox_claims` / the async twin list with no resourceVersion. `batch_reaper.py: find_stale_leases` lists with `label_selector=BATCH_ID_LABEL`, `limit=500`, `_continue`, a 90 s timeout, and no resourceVersion. `test_lists_every_page` pins this (`assertNotIn("resource_version", ...)`). Rejected for release: using the batch watch (already stopped, served from the watch cache, and absent on the atexit path), and a metadata-only list (recommended in the notes but not built). The notes add that Kubernetes 1.31+ with ConsistentListFromCache serves consistent lists from the watch cache once it catches up, so the cost gap is small.
- **Related:** [K8S-4](#k8s-4), [K8S-1](#k8s-1), [K8S-9](#k8s-9).

</details>

<a id="k8s-6"></a>

## K8S-6 POST has no idempotency key: a 409 on a retry is only a hint

**Takeaway:** Without server-side idempotency keys, "AlreadyExists on a retry" can mean "my earlier attempt succeeded" only when names are unguessable and reserved. Even then, never delete by a name whose first create was rejected.

<details>
<summary>The full story (12/N, 15/N)</summary>

- **Situation:** Kubernetes POST has no idempotency key. If attempt 1 reaches the server but its response is lost, attempt 2 gets 409 AlreadyExists.
- **What goes wrong:** Treating every 409 as success would adopt a foreign claim. Treating every 409 as failure would mark a claim the cluster really stored as CreateFailed. Deleting a name that got a 409 could delete someone else's object.
- **What we do:** `batch_utils.py: create_error_outcome`: a 409 on attempt 1 is FAIL (a CreateFailed member, `may_exist` false). A 409 on attempt 2 or later is SUCCESS. `batch_state.py: BatchState._take_deletes` never schedules a delete for CreateFailed or LOST members. The heuristic is safe because names are `<random 12-char id>-<ordinal>`, and the id is reserved by the Lease create plus the "no claims with this id" list ([K8S-7](#k8s-7)).
- **Example:**
  ```
  attempt 1: POST b1x...-3 -> stored, response lost (timeout)
  attempt 2: POST b1x...-3 -> 409  => SUCCESS (our attempt 1 landed)
  attempt 1: POST b1x...-3 -> 409  => FAIL, never delete this name
  ```
- **Related:** [K8S-7](#k8s-7). [RACE-4](race.md#race-4) covers the other half: a delete between attempts turns the retry into a resurrecting 201, and the retry must re-check the watch and release state. Test `TestCreateErrorOutcome.test_outcome_table`.

</details>

<a id="k8s-7"></a>

## K8S-7 Use one atomic create as the mutex for a name

**Takeaway:** A create either wins or gets 409, so it is a free distributed lock on a name. Win that first, run the other existence checks after, and on any failure (including interrupts) roll back only what this call created.

<details>
<summary>The full story (12/N)</summary>

- **Situation:** Two `claim_batch` calls may race on one caller-chosen id, or an id may be reused while the old batch's claims (possibly terminating) still exist.
- **What goes wrong:** Listing first and then creating is a check-then-act race. Rolling back on a 409 would delete the other creator's Lease. A cancel or Ctrl-C during the list could leak the Lease.
- **What we do:** `sandbox_batch.py: SandboxBatch._claim` (and the async twin) runs the warm-pool and template precheck before any write. Then the Lease create claims the id. A 409 raises `BatchExistsError` and deletes nothing. Then the claim list runs: any claim with the id raises `BatchExistsError`, and a failure or interrupt (`except BaseException`) deletes the just-created Lease. Old claims keep the old Lease UID, so the GC collects them. The list's resourceVersion seeds the watch so no create is missed.
- **Example:**
  ```
  precheck pools -> create Lease (409 -> BatchExistsError, no delete)
                 -> list claims (any -> BatchExistsError)
                 -> except BaseException: delete our Lease
  ```
- **Related:** [K8S-6](#k8s-6), [K8S-2](#k8s-2). [LEASE-1](lease.md#lease-1) covers the ordering and rollback from the Lease's side. Tests `test_setup_order_and_lease`, `test_setup_failure_deletes_only_a_lease_it_created`.

</details>

<a id="k8s-8"></a>

## K8S-8 Delete a scope by label selector, and remember it is a point-in-time snapshot

**Takeaway:** For "delete everything in this scope", delete by a server-side label selector rather than your cached list of names. The selector delete only sees objects that exist when it runs, so pair it with creator-side deletes for writes still in flight.

<details>
<summary>The full story (15/N, 16/N)</summary>

- **Situation:** *deletecollection* is one DELETE request on a collection with a *label selector* (a filter on labels); the apiserver deletes every matching object. A group may have claims the handle has not observed yet: in-flight fill creates, `acquire()` creates, or claims from before a crash.
- **What goes wrong:** Deleting by known names would miss claims the local cache doesn't know about. And a POST that commits right after the deletecollection survives it, with no error on either call.
- **What we do:** `sandbox_batch.py: release_group` deletes with batch-id plus the hashed group label (`batch_utils.group_label_selector`, see [K8S-14](#k8s-14)). `BatchState.release_group` returns names only so the shell can close cached connections. In-flight acquires (`_acquiring`) and unsent planned creates are released in state, and a create that lands after the label delete is deleted by name by its creator.
- **Related:** [K8S-3](#k8s-3), [K8S-14](#k8s-14). [RACE-7](race.md#race-7) owns the in-flight `acquire()` vs `release_group` race. Test `test_release_group_deletes_by_group_label_and_skips_only_its_creates`.

</details>

<a id="k8s-9"></a>

## K8S-9 A paged list is one snapshot only if every page comes from one continue chain

**Takeaway:** Continue tokens expire. When one does, restart the list from page one once, tell the consumer to throw away partial results, and take the watch's starting resourceVersion from the first page of the snapshot.

<details>
<summary>The full story (20/N)</summary>

- **Situation:** A paged list returns `limit` items plus a *continue token* for the next page, and all pages share one snapshot. Since 20/N (a95cb2a1), the watch's re-list and `get_batch`'s seed page at 500, and `count_live_sandbox_claims` pages too.
- **What goes wrong:** Continue tokens expire after a few minutes (5-15 min per the kubernetes client docstring cited in pr8_plan.md), and the apiserver answers 410 Gone. `is_retryable_status` doesn't class 410 as transient, so without a restart a mid-list 410 would fail `release()` (keeping the Lease) or fail attach. Resuming with the inconsistent token the 410 offers, mixing pages from two snapshots, or starting the watch from a later page's resourceVersion can miss or duplicate events. Treating an empty page as the end truncates the list.
- **What we do:** `k8s_helper.py: _claim_list_pages` and its async twin: on a 410 with a continue token in hand and no earlier restart, drop the token and re-list from page 1, yielding `first=True` again. `list_sandbox_claim_objects` and `count_live_sandbox_claims` reset their items or count when `first` arrives. A 410 on the first page, a second 410, or any other error propagates. The watch resourceVersion comes from the first page. Only a missing continue token ends the list. This matches client-go's pager, which falls back to a full list. (Dropping `managedFields` per page and keeping only an integer count are memory choices covered in the performance section.)
- **Example:**
  ```
  page1(t1) -> page2 -> 410
  page1'(first=True, discard) -> page2' -> done
  second 410 -> raise
  ```
- **Related:** [K8S-10](#k8s-10), [K8S-5](#k8s-5). Test `test_list_sandbox_claim_objects_restarts_once_when_a_continue_token_expires`.

</details>

<a id="k8s-10"></a>

## K8S-10 Ask for watch bookmarks, or a quiet watch ages out

**Takeaway:** Any long-lived watch that reconnects periodically should request bookmarks and use them only to advance its resourceVersion. Otherwise an idle stream turns into a loop of 410s and full re-lists.

<details>
<summary>The full story (1/N)</summary>

- **Situation:** A watch resumes from the last resourceVersion it saw. etcd *compaction* periodically discards old history, so the apiserver can only resume from recent versions. After the fill, a batch is often quiet for hours: every member is Ready and no claim changes, so the watch delivers no events.
- **What goes wrong:** Without events, `rv` never advances. Once compaction ages it out of the watch cache, every 30 s reconnect gets a 410 and re-lists every claim of the batch (PR 1 revision notes, pr1_revision_prompt.md item 2).
- **What we do:** Both helpers' `watch_sandbox_claims` pass `allow_watch_bookmarks=True`. The apiserver then sends a BOOKMARK event (an event carrying only a fresh resourceVersion) just before a timed-out watch ends (KEP-956). `_watch_loop` in both shells takes the bookmark's resourceVersion into `rv` before the `continue`, and never upserts the bookmark object, which is incomplete.
- **Example:**
  ```python
  if seen_rv:
      rv = seen_rv
  if event_type == "BOOKMARK":
      continue
  ```
- **Related:** [K8S-9](#k8s-9). [WATCH-1](watch.md#watch-1) and [WATCH-3](watch.md#watch-3) own the overall informer loop and the 410 re-list. Tests `test_bookmark_only_advances_resource_version`, plus helper tests asserting `allow_watch_bookmarks`.

</details>

<a id="k8s-11"></a>

## K8S-11 "Ready" means every field the consumer needs is present

**Takeaway:** Derive a usable state only when all the status fields the consumer needs are set, not from one readiness flag. Controllers write status fields separately, so partial status means "not ready yet".

<details>
<summary>The full story (1/N, 4/N)</summary>

- **Situation:** A *status condition* is an entry in `status.conditions` with a type (here `Ready`), a status (`True`/`False`/`Unknown`), a reason, and a message. A claim's Ready condition and its `status.sandbox.name` are separate fields that the controller may write at different times.
- **What goes wrong:** A member reported READY without a sandbox name can't be connected, since `SandboxBatch.connect` builds `Sandbox(sandbox_id=current.sandbox_name)` without a GET. It also can't be identity-checked by `still_reaches`.
- **What we do:** `batch_state.py: derive_member` yields READY only when Ready is True and `status.sandbox.name` is non-empty. Otherwise the member stays PENDING. The rule dates from 1/N and was rewritten onto the `MemberState` enum in 4/N.
- **Example:**
  ```
  Ready=True, status.sandbox missing -> PENDING
  Ready=True, status.sandbox.name=s1 -> READY
  ```
- **Related:** [K8S-12](#k8s-12), [K8S-4](#k8s-4). Test `test_ready_without_sandbox_name_is_not_ready`.

</details>

<a id="k8s-12"></a>

## K8S-12 Classify failure reasons with a terminal allowlist that mirrors the controller

**Takeaway:** Treat a remote failure as permanent only if its reason is on an explicit list that matches the controller's own retry behavior. Let unknown reasons fall through to "still pending" under a deadline.

<details>
<summary>The full story (1/N)</summary>

- **Situation:** A claim's `Ready=False` reason can be permanent (`ClaimExpired`, `PodFailed`, `InvalidConfiguration`, ...) or transient (`AdoptionPending`, `SandboxMissing`, `SandboxNotReady`, `ReconcilerError`, `WarmPoolNotFound`, `TemplateNotFound`).
- **What goes wrong:** Treating every `Ready=False` as FAILED would fail groups and quorum on conditions the controller requeues every minute. Treating none as terminal burns the whole `quorum_timeout` on permanent failures.
- **What we do:** `constants.py: TERMINAL_CLAIM_READY_REASONS` (`InvalidMetadata`, `EnvVarsInjectionRejected`, `VolumeClaimTemplatesError`, `ClaimExpired`, `SandboxExpired`, `InvalidConfiguration`, `PodFailed`, `PodSucceeded`) mirrors the controller's `computeReadyCondition`. `derive_member` sets FAILED only for those. Every other reason stays PENDING with reason and message exposed, so unknown or new reasons fall open to PENDING, bounded by the fill deadline.
- **Example:**
  ```
  Ready=False reason=WarmPoolNotFound -> PENDING (reason kept)
  Ready=False reason=PodFailed        -> FAILED
  ```
- **Related:** [K8S-11](#k8s-11). Tests `test_terminal_claim_ready_reasons`, `test_template_not_found_is_not_terminal`, `test_warmpool_not_found_is_not_terminal`.

</details>

<a id="k8s-13"></a>

## K8S-13 Validate a name against every object it will be derived into

**Takeaway:** Check user-supplied identifiers against the strictest name rule of every object they will become (after prefixes, suffixes, and downstream controllers), and reserve room for generated suffixes up front.

<details>
<summary>The full story (1/N, 12/N)</summary>

- **Situation:** Claim names are `<id>-<ordinal>`. The claim name becomes the Sandbox name, and then the Service name.
- **What goes wrong:** A Service name is a DNS-1035 label: at most 63 characters, starting with a letter. An id that is too long, or that starts with a digit, fails only when the controller creates the Service, long after `claim_batch` returned.
- **What we do:** `batch_utils.py: validate_batch_id` caps the id at `BATCH_ID_MAX_LENGTH` = 52 = 63 - 1 (`-`) - 10 ordinal digits, which mirrors Kubernetes' CronJob rule (DNS1035LabelMaxLength - 11 = 52), and requires a leading letter. `generate_batch_id` returns `b` plus 11 `secrets`-random `[a-z0-9]` characters.
- **Related:** [K8S-14](#k8s-14), [K8S-6](#k8s-6) (the random id is also what makes the 409 heuristic safe), [K8S-20](#k8s-20) (the reaper reuses `validate_batch_id` to decide which Leases it may touch).

</details>

<a id="k8s-14"></a>

## K8S-14 Hash unbounded names into label values, sized by the cost of a collision

**Takeaway:** Label values max out at 63 characters. When a label must encode an unbounded name, hash it, and if a collision would make a destructive selector hit the wrong objects, use a cryptographic hash with enough bits. Verify end to end that the writer and the selector agree.

<details>
<summary>The full story (15/N)</summary>

- **Situation:** `release_group(warmpool)` deletes a group by label-scoped deletecollection ([K8S-8](#k8s-8)), so it needs one label value per warm pool. Pool names can be up to 253 characters.
- **What goes wrong:** The raw name breaks on long pools. A short hash such as the Go controller's FNV-32 could collide between two pools in one batch, and `release_group` would then delete the other pool's claims.
- **What we do:** `batch_utils.py: group_label_value` is `sha256(pool)[:16]` (16 hex characters, 64 bits) under `constants.py: BATCH_GROUP_LABEL`. It is deterministic across SDKs, and a collision is practically impossible. The pool name stays readable in `spec.warmPoolRef.name`, so no extra annotation is needed. FNV-32 was rejected (review_carryover_notes.md PR 5 row; batch_claim_proposal.md item 5 records the SHA-256 choice). The e2e test `test_claim_batch_release_not_ready_then_release_group` checks that the label written on claims matches the selector `release_group` deletes by, which unit fakes can't show.
- **Related:** [K8S-8](#k8s-8), [K8S-13](#k8s-13).

</details>

<a id="k8s-15"></a>

## K8S-15 MicroTime needs six fractional digits, and isoformat() drops them at zero

**Takeaway:** Check how your client serializes edge values (a zero fraction, an empty list, None) against the server's strict parsers. A bug that hits one call in a million is a random production flake at scale.

<details>
<summary>The full story (1/N)</summary>

- **Situation:** Lease `renewTime` and `acquireTime` are *MicroTime* fields: timestamps the apiserver parses as RFC3339 with exactly six fractional digits. The Python kubernetes client serializes datetimes with `isoformat()`.
- **What goes wrong:** `isoformat()` omits the fraction entirely when `microsecond == 0`. About once in a million calls, a Lease create or renewal fails validation at random.
- **What we do:** `batch_utils.py: lease_now` returns `datetime.now(UTC)` with `microsecond=1` when it would be 0. `_claim`, `_attach`, `_write_renewal`, `detach`, and `_take_batch_lease` all use it.
- **Example:**
  ```python
  now if now.microsecond else now.replace(microsecond=1)
  # 2026-01-01T00:00:00+00:00        -> rejected
  # 2026-01-01T00:00:00.000001+00:00 -> accepted
  ```
- **Related:** Test `TestLeaseNow.test_lease_now_never_has_a_zero_microsecond` patches datetime to cover both branches. A comment in `test_batch_e2e.py: _batch_manifest` notes the same rule.

</details>

<a id="k8s-16"></a>

## K8S-16 Follow ecosystem Lease conventions, and read a half-cleaned-up state as "expired"

**Takeaway:** Match the de facto conventions of other implementations of a shared object (an empty Lease holder means free). Pick error types from the combined cluster state, so a resource the GC is still cleaning up reads as expired, not missing.

<details>
<summary>The full story (1/N, 3/N)</summary>

- **Situation:** Lease writers clear the holder differently: this SDK writes None, client-go-style releases write `''`. The reaper deletes the Lease before the GC removes the claims, so for a while the claims exist with no Lease.
- **What goes wrong:** Treating `''` as held blocks handoff from other tooling. Reporting a reaped batch (claims still terminating) as "not found" hides that it was abandoned, and adopting it would race the GC.
- **What we do:** `sandbox_batch.py: SandboxBatch._attach` (and the async twin) uses a truthiness check, so None and `''` are both free, as in client-go. With no Lease, it raises `BatchLeaseExpiredError` if claims remain and `BatchNotFoundError` otherwise. The proposal's `adopt_expired` flag was dropped, so a stale Lease always raises.
- **Related:** [K8S-2](#k8s-2), [K8S-3](#k8s-3). Tests `test_empty_holder_identity_counts_as_free`, `test_missing_lease_with_claims_present_raises_expired`, e2e `test_get_batch_not_found_raises`.

</details>

<a id="k8s-17"></a>

## K8S-17 Classify each outcome of a cleanup sweep: goal met, lost a benign race, or real failure

**Takeaway:** In an idempotent cleanup sweep, a 404 means the goal is met, a precondition 409 means you lost a benign race (try next run), and anything else is a real failure. Isolate failures per item and per scope, and surface real failures only in the exit status. Before you reuse a helper that swallows error codes, check that its idea of success matches yours: the same status can mean "done" for one workflow and "don't count this" for another.

<details>
<summary>The full story (12/N, 18/N)</summary>

- **Situation:** The reaper's precondition delete can fail for benign reasons (a renewal, `detach()`, or a takeover wrote the Lease, or `release()` or another reaper already deleted it) or for real ones (5xx, 403). The SDK already has a precondition Lease delete, `K8sHelper.delete_batch_lease`, built for `release()` ([K8S-1](#k8s-1)). It treats "someone wrote it since" as done, because `release()` only needs the Lease to stop being its job. The reaper has to report whether it actually reaped.
- **What goes wrong:** Treating 409 as an error makes every skipped live batch a failed CronJob run. Treating 404 as an error makes overlapping reapers fail. Aborting on the first error leaves other namespaces unreaped. Reusing `delete_batch_lease` would go wrong the other way: its rule `e.status != 404 and not (e.status == 409 and resource_version is not None)` counts a precondition 409 as deleted, so a live batch that renewed during the confirmation wait would be logged and returned as reaped while its Lease is still there.
- **What we do:** `batch_reaper.py: _delete_lease_if_unchanged` calls the generated client (`helper.coordination_v1_api.delete_namespaced_lease`) directly with the resourceVersion precondition, instead of the shared helper. It returns False on 409, so the batch is skipped with an info log and no error. A 404 returns True and the batch counts as reaped. The helper keeps its release-oriented "conflict counts as deleted" rule. In `reap`, any other exception is logged, adds to `errors`, and the loop continues. A namespace whose list raises (for example 403 without a RoleBinding) is logged, counted, and skipped. `main()` exits 1 if `errors > 0`, so failed runs show up as failed Jobs.
- **Example:**
  ```
  delete_batch_lease (release):  404 -> done, 409 w/ precondition -> done
  _delete_lease_if_unchanged:    404 -> reaped, 409 -> skipped, other -> error
  ```
- **Related:** [K8S-1](#k8s-1), [K8S-20](#k8s-20). Tests `test_lease_delete_outcomes`, `test_a_namespace_that_fails_to_list_is_skipped_and_counted`, `test_flags_and_exit_code`, and `test_lease_delete_at_a_resource_version_sends_a_precondition_and_counts_a_conflict_as_deleted` (pins the helper's rule).

</details>

<a id="k8s-18"></a>

## K8S-18 Persist ordering you'll need later in identifiers the cluster already stores

**Takeaway:** When you must recover an order that no object records, encode it in an identifier you already persist (for example contiguous per-group blocks of a monotonic counter in object names). Keep it separate from execution order, so you can reorder work without losing recoverability.

<details>
<summary>The full story (7/N, 12/N)</summary>

- **Situation:** `get_batch` rebuilds `groups` from claims alone. No object records the order the caller passed to `claim_batch`.
- **What goes wrong:** Before 7/N (d07e8941), groups were sorted by pool name, so a batch claimed as `[zeta, alpha]` came back as `[alpha, zeta]`. The group listing then differed between the creating handle and a re-attached one, and the same bug would break `wait_for_quorum`'s "groups in batch order" contract (added later, in 14/N).
- **What we do:** Each group gets one contiguous ordinal block, in group order. Only the create order differs (`batch_utils.py: fill_create_plan`: each group's first `min_ready` go first). Because the blocks are disjoint and ascending, `batch_state.py: reconstruct_groups` restores group order from each pool's lowest surviving ordinal, no matter which claims were released or lost. Ties break on pool name. `ordinal_sort_key` sorts names without an ordinal last via `(ordinal is None, ordinal or 0)`, and `parse_ordinal` parses ints, so `b1-10` sorts after `b1-2`.
- **Example:**
  ```
  groups [zeta(2), alpha(1)] -> zeta: b-0, b-1; alpha: b-2
  b-0 released -> min(zeta)=1 < min(alpha)=2 -> still [zeta, alpha]
  ```
- **Related:** [K8S-19](#k8s-19). Tests `TestReconstructGroups.test_groups_come_back_in_ordinal_order`, `test_claims_without_an_ordinal_sort_last_in_groups_and_members`.

</details>

<a id="k8s-19"></a>

## K8S-19 Stamp set membership on the object instead of deriving it from a counter range

**Takeaway:** Don't derive "is this object in set X" from a range test over a counter when parts of the range can disappear. Stamp the membership on the object itself, for example as annotations.

<details>
<summary>The full story (13/N)</summary>

- **Situation:** The early design (pr2_design.md, OPEN-H) counted a claim as part of the initial fill when `ordinal < self.size`.
- **What goes wrong:** A re-attached handle only knows the sizes of groups that still have claims. When a whole group's claims are gone, the reconstructed size shrinks, so the surviving groups' fill claims fall at or above `size` and stop counting as fill.
- **What we do:** Since 13/N (8ad5a407), `batch_state.py: BatchState._is_fill` counts a claim as fill if it carries both group annotations and its pool is a known group with size > 0 (`_fill_claims`, `upsert_claim`). `acquire()` claims carry the group label but no annotations, so fill accounting never counts them. `record_create_failure` adds the failed create's planned claim name to `_fill_claims`, standing for a claim that would have carried the annotations.
- **Example:**
  ```
  A(2): b-0, b-1 (both gone); B(2): b-2, b-3
  re-attached size = 2 -> "ordinal < 2" calls b-2, b-3 non-fill
  ```
- **Related:** [K8S-18](#k8s-18). Test `TestReattachedFill.test_a_group_with_no_claims_left_still_counts_the_other_groups_claims_as_fill`.

</details>

<a id="k8s-20"></a>

## K8S-20 When RBAC is coarser than your blast radius, narrow the scope in code

**Takeaway:** If the authorization system can't express the narrow scope you need, enforce it with strict identity checks in code (name and label must agree and both must validate), and keep the grant namespace-scoped and minimal.

<details>
<summary>The full story (18/N)</summary>

- **Situation:** RBAC grants `delete leases` for a whole namespace; it can't limit deletes to batch Leases. Deleting a batch Lease cascades to that batch's claims through the GC ([K8S-3](#k8s-3)), so a wrong delete is destructive.
- **What goes wrong:** A label-only or prefix-only match could delete another component's Lease: for example a leader-election Lease in kube-system under a cluster-wide binding, or any Lease that merely carries the batch-id label.
- **What we do:** Since 18/N (a97eeae7), `batch_reaper.py: _batch_id_of` returns a batch id only if the Lease's `agents.x-k8s.io/batch-id` label passes `batch_utils.validate_batch_id` and the Lease name is exactly `batch-<id>`. Anything else returns None and the reaper never touches it. The README's "Batch reaper" section tells operators to bind the reaper's ClusterRole with a RoleBinding in each namespace it reaps, never a ClusterRoleBinding. The ClusterRole grants only `list`, `get`, and `delete` on Leases, with no watch, update, or claim verbs. Source: reaper_contract.md "Namespace-Scoped Authorization".
- **Example:**
  ```
  name=batch-b1,  label=b1        -> reapable
  name=other-name, label=b1       -> skipped (name and label disagree)
  name=...,       label=Not-Valid -> skipped (label is not a valid batch id)
  ```
- **Related:** [K8S-3](#k8s-3), [K8S-13](#k8s-13), [K8S-17](#k8s-17). Test `test_never_touches_a_lease_that_isnt_a_valid_batch_lease`.

</details>

---

[Learnings home](../../learnings.md) · [Field guide index](../../learnings.md#sec-field-guide)
