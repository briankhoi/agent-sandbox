<a id="sec-practices"></a>

[Learnings home](../learnings.md) · [Field guide index](../learnings.md#sec-field-guide)

# Best practices we followed, and where they come from

Most of the batch code's rules are not new. They come from four places:

- **client-go**, the Go library behind every Kubernetes controller (leader election, the reflector, the pager, rate limiting).
- **Kubernetes API conventions and docs** (resourceVersion, owner references, paging, Leases).
- **Distributed-systems writing** (AWS Builders' Library, the Google SRE book, Martin Kleppmann).
- **The Python docs** (asyncio, threading, time).

These rules have held up across thousands of controllers and clusters. This section names each one, shows where our code applies it, and links the source so you can read the original.

How to read it: the table is the map. The cards below go deeper and point to the edge-case field guide entries (like `LEASE-4`) where a real bug or race tested the rule. When we differ from the source on purpose, the card says so and explains why.

Three deliberate differences from client-go are worth knowing up front:

1. `get_batch` refuses an expired Lease. client-go leader election would take it over ([BP-3](#bp-3)).
2. `detach()` keeps the Lease alive for a grace period. client-go's release writes a 1 s duration ([BP-3](#bp-3)).
3. A `Retry-After` moves our shared pacing cursor for every worker. client-go applies it to the one request ([BP-24](#bp-24)).

A link marked *(unconfirmed)* goes to a real page, but we did not confirm the specific detail named next to it.

| # | Practice (one-line rule) | Where we apply it | Why it scales | Source |
| --- | --- | --- | --- | --- |
| [BP-1](#bp-1) | Renew a Lease at a fraction of its duration, and retry failures on a short backoff | `sandbox_batch.py: SandboxBatch._renew_loop` (1/N, 3/N) | One PUT per batch every D/3, whatever the claim count | client-go leaderelection, KEP-589 |
| [BP-2](#bp-2) | The holder gives up first: start its expiry clock before it stamps renewTime | `sandbox_batch.py: SandboxBatch._write_renewal` (3/N) | No coordination needed: each holder stops on a local deadline | client-go leaderelection (RenewDeadline < LeaseDuration) |
| [BP-3](#bp-3) | An empty holderIdentity means released: hand off by clearing the holder, not deleting the Lease | `sandbox_batch.py: SandboxBatch.detach`, `_attach` (1/N, 11/N, 16/N) | Handoff is one write, independent of batch size | client-go leaderelection `release()` |
| [BP-4](#bp-4) | Write the Lease with PUT and the last resourceVersion; read only on a 409 | `k8s_helper.py: K8sHelper.replace_batch_lease`, `SandboxBatch._renew_once` (1/N, 3/N) | Half the Lease traffic of GET-then-PUT | K8s API conventions: concurrency control |
| [BP-5](#bp-5) | Make creates idempotent with deterministic names, and never reuse an ordinal | `batch_utils.py: create_error_outcome`, `batch_state.py: BatchState.next_ordinal` (12/N, 16/N) | Retries never leak extra objects | K8s API conventions: idempotency |
| [BP-6](#bp-6) | Let the garbage collector delete dependents through an ownerReference bound by UID | `batch_utils.py: lease_owner_reference` (18/N) | Cleanup deletes 1 object; the cluster's GC does the O(N) fan-out | K8s docs: owners and dependents, garbage collection |
| [BP-7](#bp-7) | List, then watch from the list's resourceVersion; relist on 410; ask for bookmarks | `sandbox_batch.py: SandboxBatch._watch_loop` (1/N, 12/N) | One watch per batch replaces N polls | K8s API concepts; client-go reflector |
| [BP-8](#bp-8) | Back off when a watch ends at once with no events | `sandbox_batch.py: _SHORT_WATCH_SECONDS` (12/N) | Stops a reconnect storm from one bad proxy | client-go reflector ("very short watch") |
| [BP-9](#bp-9) | Page large lists at 500, keep one snapshot, hold one page at a time | `k8s_helper.py: _claim_list_pages`, `count_live_sandbox_claims` (20/N) | Memory bounded by page size, not batch size | K8s API concepts: chunking; client-go pager |
| [BP-10](#bp-10) | Use consistent reads (no resourceVersion) for decisions | `sandbox_batch.py: _delete_claims_in_rounds` (12/N) | Correctness: a lagging cache can't trigger a destructive step | K8s API concepts: get and list semantics |
| [BP-11](#bp-11) | Treat deletecollection as a best-effort sweep: loop until nothing live is left, give up only on rounds with no progress | `sandbox_batch.py: _delete_claims_in_rounds` (12/N, 15/N) | Large releases keep going as long as they make progress | kube-apiserver flags |
| [BP-12](#bp-12) | Treat an object with deletionTimestamp as already gone | `batch_state.py: derive_member` (5/N) | Correctness: never hand out a sandbox being torn down | K8s docs: finalizers |
| [BP-13](#bp-13) | Fit name and label limits: budget suffixes, hash unbounded inputs, use `fullmatch` | `batch_utils.py: validate_batch_id`, `group_label_value` (1/N, 12/N, 15/N) | No create fails late on a long name | K8s docs: labels, names; Python `re` |
| [BP-14](#bp-14) | Write MicroTime with exactly six fractional digits | `batch_utils.py: lease_now` (1/N) | Removes a one-in-a-million random failure | apimachinery `micro_time.go`; Python datetime |
| [BP-15](#bp-15) | Close an unfinished streamed response before returning its connection | `k8s_helper.py: delete_sandbox_claims_by_label` (20/N) | Unread 5,000-object responses cost no memory | urllib3, aiohttp docs |
| [BP-16](#bp-16) | Order staleness thresholds so destructive actors act last | `batch_utils.py: is_lease_stale`, `batch_reaper.py: is_lease_reapable` (1/N, 18/N) | Tolerates clock skew without coordination | client-go leaderelection; K8s Leases |
| [BP-17](#bp-17) | Prove death by observing no change: wait `leaseDurationSeconds`, then delete with a resourceVersion precondition | `batch_reaper.py: reap`, `_delete_lease_if_unchanged` (18/N, 22/N) | One sleep per run, not per Lease; stateless | client-go leaderelection (clock-skew note); DeleteOptions |
| [BP-18](#bp-18) | Before each destructive round, check the Lease's UID and leaseTransitions (fencing-like) | `sandbox_batch.py: _take_batch_lease` (12/N) | One conditional write per round | Kleppmann: fencing tokens |
| [BP-19](#bp-19) | Layer cleanup: release(), then reaper + GC, then shutdownTime | `SandboxBatch._claim`, `release`, `batch_reaper.reap` (12/N, 16/N, 18/N, 22/N) | No single crash leaks sandboxes forever | K8s garbage collection |
| [BP-20](#bp-20) | Put an explicit timeout on every request, and two timeouts on watches | `batch_utils.py: BATCH_REQUEST_TIMEOUT_SECONDS` etc. (1/N, 2/N, 3/N, 12/N) | No thread hangs forever on a dead socket | kube-apiserver `--request-timeout`; AWS Builders' Library |
| [BP-21](#bp-21) | Retry only known-transient errors; surface everything else through err() | `batch_utils.py: is_retryable_status`, `sandbox_batch.py: _is_transport_error` (1/N, 2/N, 8/N, 13/N) | Config errors fail fast instead of retrying forever | K8s API conventions: status codes; AWS Builders' Library |
| [BP-22](#bp-22) | Exponential backoff with equal jitter | `batch_utils.py: backoff_delay` (1/N) | Spreads many retrying clients apart | Brooker, "Exponential Backoff And Jitter" |
| [BP-23](#bp-23) | One retry owner per layer: turn off hidden library retries where the SDK owns deadlines | `k8s_helper.py: _api_client_without_retries`, `_UnretriedApiClient` (3/N, 22/N) | Every batch request runs once within `_request_timeout` | urllib3 Retry; Google SRE book |
| [BP-24](#bp-24) | Scope backoff by cause: Retry-After moves the shared cursor, a blip delays one request | `sandbox_batch.py: SandboxBatch._create_one` (13/N) | One worker's blip never stalls the other workers | client-go `with_retry.go`; K8s APF |
| [BP-25](#bp-25) | Pace with one shared reservation cursor, and count retries against it | `sandbox_batch.py: SandboxBatch._reserve_create_slot` (12/N, 19/N) | N workers together never exceed create_rps | client-go flowcontrol; GCRA |
| [BP-26](#bp-26) | Propagate the caller's deadline and shed hopeless work before reserving capacity | `sandbox_batch.py: SandboxBatch.acquire`, `_check_retry_deadline` (16/N, 19/N) | Queues stay short under overload | Google SRE book; gRPC deadlines; AWS load shedding |
| [BP-27](#bp-27) | Bulkhead: cap I/O with one semaphore, and keep pool headroom for the watch and renewals | `SandboxBatch._in_flight`, `batch_utils.py: check_pool_size` (15/N, 17/N, 19/N) | Bulk work can't starve the liveness path | Azure Bulkhead pattern |
| [BP-28](#bp-28) | Single-flight: concurrent identical reads share one in-flight call | `SandboxBatch._check_pool`, `_refresh_warm_credit` (16/N, 19/N) | 500 acquires on a new pool cost 2 precheck GETs | Go `singleflight` |
| [BP-29](#bp-29) | In asyncio, shield shared work, use `Task.cancelling()`, and clean up on BaseException | `AsyncSandboxBatch._check_pool`, `SandboxBatch._claim` (12/N, 16/N) | Correctness under Ctrl-C and task cancellation | Python asyncio docs |
| [BP-30](#bp-30) | Wait on a Condition in a predicate loop, with a timed wake for deadlines | `sandbox_batch.py: SandboxBatch._next` (13/N) | Per-key Events avoid waking every waiter on every change | Python threading docs |
| [BP-31](#bp-31) | Monotonic clock for local timing; wall clock only for values other machines read | `_lease_expires_at`, `WarmCredit`, `lease_now` (1/N, 3/N, 19/N) | Correctness: deadlines survive clock jumps | Python `time.monotonic`; PEP 418 |
| [BP-32](#bp-32) | Background threads are daemons; asyncio tasks get names | `SandboxBatch._start`, `AsyncSandboxBatch` tasks (1/N, 11/N, 12/N) | Many batches per process stay debuggable | Python threading, asyncio docs |
| [BP-33](#bp-33) | Keep state transitions in a Sans-I/O core | `batch_state.py: BatchState` (1/N, 16/N) | One core for sync and async; tests need no mocks | Sans-I/O docs |
| [BP-34](#bp-34) | Log per item at DEBUG, log once per degraded episode, and expose a pollable health flag | `k8s_helper.py: create_sandbox_claim(log_level)`, `_mark_renewal_degraded`, `lease_degraded` (3/N, 12/N, 19/N) | Logs stay readable at 5,000 claims | Python Logging HOWTO |
| [BP-35](#bp-35) | Make a cron cleanup job stateless, delete-only, dry-runnable, with a clear exit code | `batch_reaper.py: main`, `reap` (18/N) | Safe to run twice, late, or killed midway | K8s CronJob; RBAC docs |
| [BP-36](#bp-36) | Implicit teardown undoes only what that scope acquired | `SandboxBatch._exit_detaches`, client exit cleanup (12/N, 13/N) | Inspection scripts never delete someone else's batch | client-go ReleaseOnCancel; Dask `Client.close` |

## Kubernetes clients and controllers

<a id="bp-1"></a>

### BP-1 Renew a Lease at a fraction of its duration, and retry failures on a short backoff

- **Why it is a best practice:** a Lease protects you only if the holder renews well before it expires. Renewing at D/3 means one slow or failed renewal still leaves about two thirds of D. After a failure, waiting another full interval would waste that margin, so retry fast instead.

  It scales because liveness costs one PUT per batch every D/3. A 5,000-claim batch costs the same as a 2-claim batch. That is the main reason the design uses one Lease per batch instead of a heartbeat per claim.
- **Where we apply it:** `sandbox_batch.py: SandboxBatch.__init__` sets `_renew_interval = max(1, lease_duration // 3)` (1/N). `SandboxBatch._renew_loop` switches from that interval to `batch_utils.backoff_delay(failures, 0.5, 30)` after a failure, and caps every wait at `deadline - time.monotonic()` (3/N). `async_sandbox_batch.py` mirrors both.

  How it compares: client-go leader election has three knobs. kube-controller-manager's defaults are LeaseDuration 15 s, RenewDeadline 10 s, and RetryPeriod 2 s. The leader retries every RetryPeriod and gives up at RenewDeadline. We fold this into two cadences: a relaxed D/3 when healthy, a short jittered backoff after a failure, and the expiry itself as the give-up point ([BP-2](#bp-2)). The kubelet's node Lease uses the same idea with a 40 s duration and a 0.25 renew fraction (KEP-589).
- **Source:** [client-go `tools/leaderelection/leaderelection.go`](https://github.com/kubernetes/client-go/blob/master/tools/leaderelection/leaderelection.go) (`LeaderElectionConfig` fields LeaseDuration, RenewDeadline, RetryPeriod). [kube-controller-manager reference](https://kubernetes.io/docs/reference/command-line-tools-reference/kube-controller-manager/) (the `--leader-elect-*` defaults). [KEP-589 Efficient Node Heartbeats](https://github.com/kubernetes/enhancements/tree/master/keps/sig-node/589-efficient-node-heartbeats). [Leases concept](https://kubernetes.io/docs/concepts/architecture/leases/).
- **Example** (D=60, interval 20 s):
  ```text
  t=0     renew ok    -> expires_at = 60
  t=20    renew fails (503) -> retry in ~0.5 s, not 20 s
  t=20.4  retry ok    -> expires_at = 80.4
  ```
- **Field guide:** [LEASE-9](field-guide/lease.md#lease-9), [RETRY-10](field-guide/retry.md#retry-10), [PERF-8](field-guide/perf.md#perf-8).

<a id="bp-2"></a>

### BP-2 The holder gives up first: start its expiry clock before it stamps renewTime

- **Why it is a best practice:** other clients judge the Lease by the `renewTime` written in it. If the holder timed its expiry from when the response came back, it would believe it holds the Lease longer than everyone else believes. Reading the monotonic clock before stamping `renewTime` keeps the holder's view at or before everyone else's. At its local deadline, the holder fails closed: it stops working as holder.
- **Where we apply it:** `sandbox_batch.py: SandboxBatch._write_renewal` reads `sent_at = time.monotonic()` before calling `batch_utils.lease_now()`. `SandboxBatch._renew_once` sets `_lease_expires_at = sent_at + D`. `SandboxBatch._renew_loop` wakes at the deadline even when no renewal is due, and calls `_stop_renewal(BatchLeaseExpiredError(...))`, which sets `err()`. `SandboxBatch._renew_request_timeout` caps each PUT, so a hung request can't carry the holder past its deadline. All of this is 3/N. Fill creates and `acquire()` check `err()` through `_can_still_send` (19/N). `_claim` and `_attach` also read the clock before stamping (3/N, 12/N).

  How it compares: client-go requires RenewDeadline < LeaseDuration (checked in `NewLeaderElector`), so a leader stops leading before any candidate may acquire. Our margin comes from the clock reading order here plus the skew margins in [BP-16](#bp-16).

  Gotcha: neither Python Kubernetes client has a default request timeout. Without `_renew_request_timeout`, one hung renewal blocks the loop past expiry while the handle still thinks it is the holder.
- **Source:** [client-go `leaderelection.go`](https://github.com/kubernetes/client-go/blob/master/tools/leaderelection/leaderelection.go) (RenewDeadline docs and the validation in `NewLeaderElector`). [Python `time.monotonic`](https://docs.python.org/3/library/time.html#time.monotonic).
- **Example:**
  ```python
  sent_at = time.monotonic()        # 100.0
  spec.renew_time = lease_now()     # stamped after the reading
  replace(...)                      # slow, returns at 103.0
  expires_at = sent_at + 60         # 160.0, not 163.0
  ```
- **Field guide:** [LEASE-5](field-guide/lease.md#lease-5), [LEASE-6](field-guide/lease.md#lease-6), [RETRY-9](field-guide/retry.md#retry-9), [LEASE-29](field-guide/lease.md#lease-29).

<a id="bp-3"></a>

### BP-3 An empty holderIdentity means released: hand off by clearing the holder, not deleting the Lease

- **Why it is a best practice:** a Lease carries two facts. `renewTime` plus duration says "alive". `holderIdentity` says "owned by". Clearing only the holder says "nobody owns this, but it is still alive", so another driver can pick it up. That is a graceful handoff with no destroyed state.
- **Where we apply it:** `sandbox_batch.py: SandboxBatch.detach` writes `holder_identity=None`, a fresh `renew_time`, `lease_duration_seconds=grace`, and the next-ordinal annotation (1/N; re-read and retry once on 409 in 11/N; next ordinal in 16/N). `SandboxBatch._attach` raises `BatchInUseError` if `spec.holder_identity` is set, and increments `lease_transitions` (1/N; the truthy holder check since 3/N).

  How it compares: client-go's `release()` (used with ReleaseOnCancel) also writes an empty HolderIdentity, and candidates treat `len(HolderIdentity) == 0` as free. client-go also bumps LeaderTransitions on each acquire, as we do. We differ on purpose in two ways:
  1. We write the caller's `grace` (default: the batch's duration), not client-go's 1 s. That keeps the batch alive long enough for a new driver to attach before the reaper may act.
  2. `get_batch` refuses a stale Lease even when no one holds it. client-go takes over expired leases. We refuse because an expired batch may already be in the middle of being reaped.

  Gotcha: the check is `if spec.holder_identity:`, which treats `None` and `""` the same. A check like `is not None` would wrongly refuse a Lease that a Go client released with `""`.
- **Source:** [client-go `leaderelection.go`](https://github.com/kubernetes/client-go/blob/master/tools/leaderelection/leaderelection.go) (`release()` and `tryAcquireOrRenew()`). [Lease v1 API reference](https://kubernetes.io/docs/reference/kubernetes-api/cluster-resources/lease-v1/) (`holderIdentity`, `leaseTransitions`).
- **Example:**
  ```text
  detach():   holder=None, duration=grace, renewTime=now
  get_batch:  holder empty and not stale -> take it
              holder=me, leaseTransitions += 1
  ```
- **Field guide:** [LEASE-15](field-guide/lease.md#lease-15), [LEASE-22](field-guide/lease.md#lease-22), [LEASE-30](field-guide/lease.md#lease-30), [K8S-16](field-guide/k8s.md#k8s-16).

<a id="bp-4"></a>

### BP-4 Write the Lease with PUT and the last resourceVersion; read only on a 409

- **Why it is a best practice:** Kubernetes uses `resourceVersion` for compare-and-swap. A PUT whose body carries an old `resourceVersion` fails with 409 Conflict, so two drivers can never both believe they renewed the same Lease. A merge PATCH without a `resourceVersion` is last-writer-wins, so it would silently overwrite a takeover.

  It scales because renewal is one request instead of GET then PUT, which halves Lease traffic across all live batches.
- **Where we apply it:** `k8s_helper.py: K8sHelper.replace_batch_lease` (its comment says it uses replace (PUT) over PATCH) (1/N). `sandbox_batch.py: SandboxBatch._renew_once` PUTs `self._lease`, the object returned by the last successful write, and reads only after a 409 (3/N). A 409 means either someone else wrote, or one of our own earlier writes landed after its response was lost. The re-read `holderIdentity` tells the two apart.

  Gotcha: a write that is retried after it already landed also comes back 409. That is one reason transport retries are off for the Lease client ([BP-23](#bp-23)).
- **Source:** [Kubernetes API conventions: Concurrency Control and Consistency](https://github.com/kubernetes/community/blob/master/contributors/devel/sig-architecture/api-conventions.md#concurrency-control-and-consistency). [API concepts: resource versions](https://kubernetes.io/docs/reference/using-api/api-concepts/#resource-versions).
- **Example:**
  ```text
  PUT lease (rv=41) -> 200, rv=42
  PUT lease (rv=42) -> 409
    GET -> holder == me ? rewrite : stop
  ```
- **Field guide:** [LEASE-2](field-guide/lease.md#lease-2), [LEASE-28](field-guide/lease.md#lease-28), [PERF-8](field-guide/perf.md#perf-8), [PAT-33](patterns.md#pat-33).

<a id="bp-5"></a>

### BP-5 Make creates idempotent with deterministic names, and never reuse an ordinal

- **Why it is a best practice:** POST has no idempotency key, but a name works as one. The API conventions say a POST for an existing name returns 409 AlreadyExists. So if attempt 1 timed out on the client but the apiserver stored the claim, attempt 2 gets 409, and we know the claim exists. With `generateName`, each retry would create a new object and leak the first one.
- **Where we apply it:** every claim is named `<batch-id>-<ordinal>` by `batch_utils.py: fill_create_plan` (12/N). `batch_utils.py: create_error_outcome` reads a 409 on attempt 1 as failure (a foreign object owns the name) and on attempt 2+ as success (12/N). `batch_state.py: BatchState.start_acquire` and `next_ordinal` keep one batch-wide counter, with `_next_ordinal = max(size, next_ordinal)` (16/N). `SandboxBatch.detach` writes `BATCH_NEXT_ORDINAL_ANNOTATION` so a re-attached handle continues the counter (16/N).

  We never reuse an ordinal because a released claim may still be terminating under its old name.

  Gotcha: if something deletes the object between attempts, the retry gets 201 and resurrects it. `RACE-4` covers that guard.
- **Source:** [Kubernetes API conventions: Idempotency](https://github.com/kubernetes/community/blob/master/contributors/devel/sig-architecture/api-conventions.md#idempotency). [Object names](https://kubernetes.io/docs/concepts/overview/working-with-objects/names/).
- **Example:**
  ```text
  POST b123-7 -> timeout (but stored server-side)
  POST b123-7 -> 409 AlreadyExists  => attempt 2: treat as created
  ```
- **Field guide:** [K8S-6](field-guide/k8s.md#k8s-6), [K8S-7](field-guide/k8s.md#k8s-7), [RACE-3](field-guide/race.md#race-3), [RACE-4](field-guide/race.md#race-4), [LEASE-24](field-guide/lease.md#lease-24), [API-27](field-guide/api.md#api-27).

<a id="bp-6"></a>

### BP-6 Let the garbage collector delete dependents through an ownerReference bound by UID

- **Why it is a best practice:** with an ownerReference, Kubernetes deletes the dependents for you. A default delete is background cascading: the owner goes away at once, and the GC then deletes each dependent whose owners are all gone. The GC matches owners by UID, not name. So a new batch that reuses an id gets a new Lease UID, and reaping the old Lease never touches it. A label-scoped `deletecollection` can't promise that.

  It scales because cleanup deletes one object per batch, and the cluster's GC does the O(N) fan-out to claims, Sandboxes, and Pods.
- **Where we apply it:** `batch_utils.py: lease_owner_reference` builds the reference from the Lease the apiserver returned (18/N). `SandboxBatch.__init__` stores it as `_owner_references`, `_send_create` passes it, and `k8s_helper.py: create_sandbox_claim` sets `metadata.ownerReferences` (18/N; the create call moved into `_send_create` in 19/N). The e2e test `test_deleting_a_batch_lease_garbage_collects_its_claims` covers it (18/N).

  We leave out `controller` and `blockOwnerDeletion` on purpose. Under the OwnerReferencesPermissionEnforcement admission plugin, setting `blockOwnerDeletion` needs update permission on the owner's `finalizers` subresource. Leaving it out keeps the driver's RBAC unchanged.

  Gotcha: use the UID from the apiserver's response, never a made-up one. A reference to a UID that doesn't exist counts as an absent owner, and the GC deletes the dependent. A namespaced owner must also be in the dependent's namespace.
- **Source:** [Owners and dependents](https://kubernetes.io/docs/concepts/overview/working-with-objects/owners-dependents/) (UID matching, same-namespace rule). [Garbage collection](https://kubernetes.io/docs/concepts/architecture/garbage-collection/) (background vs foreground). [OwnerReferencesPermissionEnforcement](https://kubernetes.io/docs/reference/access-authn-authz/admission-controllers/#ownerreferencespermissionenforcement).
- **Example:**
  ```yaml
  ownerReferences:
  - apiVersion: coordination.k8s.io/v1
    kind: Lease
    name: batch-b123
    uid: 5f1c...   # from the create response
  ```
- **Field guide:** [CLEAN-14](field-guide/clean.md#clean-14), [K8S-2](field-guide/k8s.md#k8s-2), [K8S-3](field-guide/k8s.md#k8s-3), [LEASE-1](field-guide/lease.md#lease-1), [CLEAN-13](field-guide/clean.md#clean-13).

<a id="bp-7"></a>

### BP-7 List, then watch from the list's resourceVersion; relist on 410; ask for bookmarks

- **Why it is a best practice:** a watch started at the list's `resourceVersion` delivers every change after the snapshot. Starting from "now" would miss creates that land between the list and the watch. The apiserver keeps only a window of history. If your `resourceVersion` falls out of it, the watch returns 410 Gone, and the only correct recovery is a fresh list plus a reconcile. Bookmarks are events that carry only a newer `resourceVersion`, so your resume point stays fresh during quiet periods.

  It scales because one watch per batch replaces polling N claims.
- **Where we apply it:** `sandbox_batch.py: SandboxBatch._watch_loop` updates `rv` from every event, including BOOKMARK, before skipping it, and on 410 relists and calls `BatchState.resync_from_list` to catch deletes it never saw (1/N). `_claim` lists right after creating the Lease and starts the watch at that list's `resourceVersion` (12/N); `_attach` does the same (1/N). `k8s_helper.py: watch_sandbox_claims` passes `allow_watch_bookmarks=True` (1/N).

  How it compares: client-go's Reflector runs the same ListAndWatch loop, relists on 410, and can ask for bookmarks. Informers add a shared, indexed cache and event handlers on top. The Python clients give us no client-go style informer, so ours is a small, single-consumer version: one list and one watch per handle.

  Gotcha: update `rv` from BOOKMARK events too. That is their whole purpose.
- **Source:** [API concepts: Efficient detection of changes](https://kubernetes.io/docs/reference/using-api/api-concepts/#efficient-detection-of-changes) and its watch bookmarks section. [client-go `tools/cache/reflector.go`](https://github.com/kubernetes/client-go/blob/master/tools/cache/reflector.go).
- **Example:**
  ```python
  items, rv = list_claims(selector)                 # rv=1000
  for ev in watch(rv=1000, bookmarks=True):
      rv = ev["object"]["metadata"]["resourceVersion"]
  # 410 -> items, rv = list_claims(); reconcile
  ```
- **Field guide:** [WATCH-1](field-guide/watch.md#watch-1), [WATCH-2](field-guide/watch.md#watch-2), [WATCH-3](field-guide/watch.md#watch-3), [K8S-10](field-guide/k8s.md#k8s-10), [PAT-32](patterns.md#pat-32).

<a id="bp-8"></a>

### BP-8 Back off when a watch ends at once with no events

- **Why it is a best practice:** a healthy watch ends at its `timeoutSeconds`, and reopening it at once is fine. But a broken proxy or connection can end every stream instantly. Then "reopen immediately" becomes a request storm. The 12/N PR body measured about 200 requests in a few milliseconds in a unit test.
- **Where we apply it:** `sandbox_batch.py: _SHORT_WATCH_SECONDS = 1`, and `SandboxBatch._watch_loop` treats `not got_event` with an elapsed time under 1 s as a failure and sleeps `retry_delay(failures, None)` (12/N). The async shell mirrors it.

  How it compares: client-go's Reflector uses the same rule. A watch shorter than one second with zero events is an error (`VeryShortWatchError`, "very short watch"), and the reflector backs off. Its default backoff starts at 800 ms, doubles with jitter up to a 30 s cap, and resets after 2 minutes without backing off.

  Gotcha: reset the failure counter only when an event arrives or the watch reaches its normal timeout. Otherwise the backoff never grows.
- **Source:** [client-go `reflector.go`](https://github.com/kubernetes/client-go/blob/master/tools/cache/reflector.go) (the very-short-watch check in `handleWatch`; `defaultBackoffInit`, `defaultBackoffMax`, `defaultBackoffReset`).
- **Example:**
  ```text
  t=0.000 watch opens
  t=0.003 stream ends, 0 events -> failures=1, sleep 0.25-0.5 s (not 0)
  ```
- **Field guide:** [RETRY-3](field-guide/retry.md#retry-3), [RETRY-2](field-guide/retry.md#retry-2).

<a id="bp-9"></a>

### BP-9 Page large lists at 500, keep one snapshot, hold one page at a time

- **Why it is a best practice:** a paged list is still one consistent snapshot, because every page in one continue chain comes from the first page's `resourceVersion`. Paging bounds each response's size. Continue tokens expire (by default after about 5 minutes) with 410 Gone. Restarting the whole list keeps the snapshot consistent. Stitching pages from two snapshots would not.

  It scales because memory depends on the page size, not the batch size. The 20/N PR bodies measured a 5,000-claim attach at about 69.4 MB peak before paging.
- **Where we apply it:** `k8s_helper.py: _claim_list_pages` uses `limit=CLAIM_LIST_PAGE_SIZE` (500), restarts once on a 410 with a continue token, and ends only on an empty continue token (20/N). `count_live_sandbox_claims` folds a count page by page and drops each page (20/N). `list_sandbox_claim_objects` pops `managedFields`, which nothing in the SDK reads (20/N). `batch_reaper.py: find_stale_leases` pages Leases at 500 (18/N). The async helper mirrors the claim paths.

  How it compares: 500 is client-go's pager default.

  Gotcha: a page can be empty while more pages follow. Stop only on an empty continue token, never on empty `items`. On restart, throw away the items gathered so far.
- **Source:** [API concepts: Retrieving large results sets in chunks](https://kubernetes.io/docs/reference/using-api/api-concepts/#retrieving-large-results-sets-in-chunks). [client-go `tools/pager/pager.go`](https://github.com/kubernetes/client-go/blob/master/tools/pager/pager.go) (`defaultPageSize = 500`).
- **Example:**
  ```text
  page = list(limit=500)
  while page.continue: page = list(limit=500, continue=page.continue)
  410 on a continue -> start over once
  ```
- **Field guide:** [K8S-9](field-guide/k8s.md#k8s-9), [PERF-4](field-guide/perf.md#perf-4), [PERF-5](field-guide/perf.md#perf-5), [PERF-6](field-guide/perf.md#perf-6), [PAT-21](patterns.md#pat-21).

<a id="bp-10"></a>

### BP-10 Use consistent reads (no resourceVersion) for decisions

- **Why it is a best practice:** for get and list, `resourceVersion` has three modes. Unset means "most recent", a consistent read. `"0"` means "any", usually served from the apiserver's watch cache, which may lag. A specific value means "not older than" that version. A cache that converges later can use `"0"`. A one-shot decision can't.
- **Where we apply it:** `sandbox_batch.py: _delete_claims_in_rounds` counts live claims with no `resourceVersion`. Its comment says so, so a lagging watch cache can't report that no claims are left too early (12/N). `release()` deletes the Lease once that count hits 0. A stale count of 0 would delete the Lease while late claims still exist. `k8s_helper.py: list_sandbox_claim_objects` and `count_live_sandbox_claims` pass no `resource_version` (list since 1/N; paging in 20/N).

  How it compares: client-go's reflector has traditionally made its first list with `"0"`, because the watch that follows makes its cache converge anyway.

  Scale note: consistent reads used to go to etcd. KEP-2340 lets newer apiservers serve them from the watch cache. It went beta (on by default) in v1.31 and GA in v1.34.

  Gotcha: don't "optimize" these lists with `resourceVersion="0"`. It quietly turns a correctness check into a best-effort one.
- **Source:** [API concepts: Semantics for get and list](https://kubernetes.io/docs/reference/using-api/api-concepts/#semantics-for-get-and-list). [KEP-2340 Consistent reads from cache](https://github.com/kubernetes/enhancements/tree/master/keps/sig-api-machinery/2340-Consistent-reads-from-cache) (`ConsistentListFromCache`, GA in v1.34).
- **Field guide:** [K8S-5](field-guide/k8s.md#k8s-5).

<a id="bp-11"></a>

### BP-11 Treat deletecollection as a best-effort sweep: loop until nothing live is left, give up only on rounds with no progress

- **Why it is a best practice:** the apiserver implements deletecollection as "list, then delete each item". With the default `--delete-collection-workers=1`, it deletes one at a time. So a big batch can hit the 60 s request timeout while deletion continues on the server. It is also a point-in-time snapshot: a create that commits after the list survives.

  It scales because a 5,000-claim release that needs many rounds is never abandoned while each round still makes progress.
- **Where we apply it:** `sandbox_batch.py: _delete_claims_in_rounds` re-counts after each round, resets `idle_rounds` when the live count shrinks, and gives up after `BATCH_RELEASE_MAX_IDLE_ROUNDS` (3) rounds in a row with no progress (12/N; factored out under this name and reused by `release_group` in 15/N). `k8s_helper.py: count_live_sandbox_claims` skips claims with `deletionTimestamp`, since they will go away by themselves (20/N).

  Gotcha: delete the Lease last, and only at the `resourceVersion` the round wrote. Deleting it first removes the anchor that the GC and the reaper rely on.
- **Source:** [kube-apiserver reference](https://kubernetes.io/docs/reference/command-line-tools-reference/kube-apiserver/) (`--delete-collection-workers` default 1, `--request-timeout` default 1m0s). [apiserver generic registry `store.go`](https://github.com/kubernetes/kubernetes/blob/master/staging/src/k8s.io/apiserver/pkg/registry/generic/registry/store.go) (`DeleteCollection` lists, then deletes the items with up to `DeleteCollectionWorkers` workers; not atomic).
- **Example:**
  ```text
  round 1: DELETE ?labelSelector=...  -> 504, live=1800
  round 2: DELETE ...                 -> live=0 -> delete Lease at rv
  ```
- **Field guide:** [RETRY-11](field-guide/retry.md#retry-11), [K8S-8](field-guide/k8s.md#k8s-8), [RACE-7](field-guide/race.md#race-7), [RACE-8](field-guide/race.md#race-8), [CLEAN-13](field-guide/clean.md#clean-13), [LEASE-18](field-guide/lease.md#lease-18), [PAT-31](patterns.md#pat-31).

<a id="bp-12"></a>

### BP-12 Treat an object with deletionTimestamp as already gone

- **Why it is a best practice:** deletion is often a process. The apiserver sets `deletionTimestamp`, finalizers and graceful termination run, and the DELETED event comes later. In that window the controller has stopped reconciling, so a `Ready=True` condition is stale. Trusting it would hand out a sandbox that is being torn down.
- **Where we apply it:** `batch_state.py: derive_member` sets `terminating` from `deletionTimestamp` and maps it to `LOST` with no pod IPs and no service (5/N). A `FAILED` member stays `FAILED`, so the caller still sees the real reason. `count_live_sandbox_claims` excludes terminating claims (20/N).
- **Source:** [Finalizers](https://kubernetes.io/docs/concepts/overview/working-with-objects/finalizers/) (deletionTimestamp semantics). [API conventions: metadata](https://github.com/kubernetes/community/blob/master/contributors/devel/sig-architecture/api-conventions.md#metadata).
- **Example:**
  ```text
  MODIFIED claim: deletionTimestamp=..., Ready=True
  => Member(state=LOST, pod_ips=())
  ```
- **Field guide:** [K8S-4](field-guide/k8s.md#k8s-4).

<a id="bp-13"></a>

### BP-13 Fit name and label limits: budget suffixes, hash unbounded inputs, use `fullmatch`

- **Why it is a best practice:** label values and DNS-label names are capped at 63 characters. If you derive names from user input, check the final length up front, so no create fails late. If an input is unbounded, store a hash in the label instead of the raw value. The repo's AGENTS.md asks for exactly this.
- **Where we apply it:** `batch_utils.py: validate_batch_id` requires a DNS-1123 label that starts with a letter and is at most `BATCH_ID_MAX_LENGTH` (52) characters, so `<id>-<ordinal>` fits 63 (1/N). It switched to `fullmatch` in 12/N after `'abc\n'` slipped through. `generate_batch_id` uses `secrets.choice` (12/N). `group_label_value` stores `sha256(pool)[:16]`, because pool names may exceed 63 characters (15/N). `batch_reaper.py: _batch_id_of` accepts only a valid id and the name `batch-<id>` (18/N).

  Gotcha: a hash is one-way. Keep the original somewhere readable (here, the claim's warmPoolRef).
- **Source:** [Labels: syntax and character set](https://kubernetes.io/docs/concepts/overview/working-with-objects/labels/#syntax-and-character-set). [Object names](https://kubernetes.io/docs/concepts/overview/working-with-objects/names/). [Python `re`](https://docs.python.org/3/library/re.html) (`$` matches before a trailing newline; `fullmatch`). [Python `secrets`](https://docs.python.org/3/library/secrets.html).
- **Example:**
  ```python
  re.match(r"^[a-z]+$", "abc\n")     # matches!
  re.fullmatch(r"[a-z]+", "abc\n")   # None
  ```
- **Field guide:** [K8S-13](field-guide/k8s.md#k8s-13), [K8S-14](field-guide/k8s.md#k8s-14), [API-5](field-guide/api.md#api-5), [API-24](field-guide/api.md#api-24).

<a id="bp-14"></a>

### BP-14 Write MicroTime with exactly six fractional digits

- **Why it is a best practice:** Lease `renewTime` and `acquireTime` are MicroTime fields, which the apiserver parses as RFC3339 with six fractional digits. Python's `isoformat()` drops the fraction entirely when the microsecond is 0. So about one write in a million would be rejected, and it would look like random flakiness.
- **Where we apply it:** `batch_utils.py: lease_now` bumps a zero microsecond to 1 (1/N). `_write_renewal`, `_attach`, `detach`, and `_take_batch_lease` all use it.

  Gotcha: tests that freeze time on whole seconds hit this every time, which makes them a good way to cover it.
- **Source:** [apimachinery `micro_time.go`](https://github.com/kubernetes/apimachinery/blob/master/pkg/apis/meta/v1/micro_time.go) (RFC3339Micro). [Python `datetime.isoformat`](https://docs.python.org/3/library/datetime.html#datetime.datetime.isoformat).
- **Example:**
  ```python
  datetime(2026, 1, 1, tzinfo=UTC).isoformat()  # '2026-01-01T00:00:00+00:00'
  now.replace(microsecond=1)                     # the fix
  ```
- **Field guide:** [K8S-15](field-guide/k8s.md#k8s-15).

<a id="bp-15"></a>

### BP-15 Close an unfinished streamed response before returning its connection

- **Why it is a best practice:** deletecollection returns every deleted object. At 5,000 claims that response was the 69.5 MB peak of `release()` (20/N body), and nothing reads it. Streaming it unread saves the memory. But urllib3's `release_conn()` puts the socket back in the pool without checking for EOF, and a half-read socket breaks the next request.
- **Where we apply it:** `k8s_helper.py: delete_sandbox_claims_by_label` uses `_preload_content=False`, drains with `stream(decode_content=False)`, and in `finally` closes the response if it is not closed before calling `release_conn()` (20/N). `async_k8s_helper.py` checks the status itself, decodes error bodies with `errors='replace'`, then calls `response.release()`, which closes an incomplete connection (20/N).

  Gotcha: the sync client raises `ApiException` for non-2xx before returning, but `kubernetes_asyncio` with `_preload_content=False` does not.
- **Source:** [urllib3 response reference](https://urllib3.readthedocs.io/en/stable/reference/urllib3.response.html) (`release_conn`, `stream`). [aiohttp client reference](https://docs.aiohttp.org/en/stable/client_reference.html) (`ClientResponse.release`).
- **Example:**
  ```python
  try:
      for _ in r.stream(decode_content=False): pass
  finally:
      if not r.closed: r.close()
      r.release_conn()
  ```
- **Field guide:** [LIB-13](field-guide/lib.md#lib-13), [LIB-14](field-guide/lib.md#lib-14), [PERF-7](field-guide/perf.md#perf-7).

## Distributed systems

<a id="bp-16"></a>

### BP-16 Order staleness thresholds so destructive actors act last

- **Why it is a best practice:** three parties judge one Lease with different clocks. If each one errs toward the action that can't destroy work, a skew smaller than the margins can never cause a wrong delete. Waiting is always safe. Deleting early is not.
- **Where we apply it:**
  - `batch_utils.py: is_lease_stale` makes `get_batch` refuse at `renewTime + D - CLOCK_SKEW_MARGIN` (5 s) (1/N). Refusing to attach is harmless.
  - The holder stops at D after its last send ([BP-2](#bp-2)).
  - `batch_reaper.py: is_lease_reapable` picks a Lease only after `renewTime + D + skew_margin`, with `REAPER_DEFAULT_SKEW_MARGIN_SECONDS = 60`, and then waits one more renew interval ([BP-17](#bp-17)) (18/N). `_skew_margin` rejects values below `CLOCK_SKEW_MARGIN`.
  - `find_stale_leases` skips a Lease with no renewTime or duration instead of guessing (18/N).

  The docstrings of both functions now say which way each margin points (18/N).

  Gotcha: don't reuse `is_lease_stale` in the reaper. It subtracts the margin, so it would reap live batches whose clock runs slightly behind.
- **Source:** [client-go `leaderelection.go`](https://github.com/kubernetes/client-go/blob/master/tools/leaderelection/leaderelection.go) (its package doc on not trusting other clients' timestamps). [Leases concept](https://kubernetes.io/docs/concepts/architecture/leases/).
- **Example** (D=60):
  ```text
  T+55   get_batch refuses
  T+60   holder stops (monotonic)
  T+120  reaper may pick it, then waits 20 s more
  ```
- **Field guide:** [LEASE-4](field-guide/lease.md#lease-4), [LEASE-10](field-guide/lease.md#lease-10), [API-6](field-guide/api.md#api-6).

<a id="bp-17"></a>

### BP-17 Prove death by observing no change: wait `leaseDurationSeconds`, then delete with a resourceVersion precondition

- **Why it is a best practice:** judging staleness by `renewTime` compares the driver's clock with the reaper's. If the driver's clock runs 90 s behind, a live batch looks stale. Within a full `leaseDurationSeconds`, a connected driver either lands a fresh renewal or crosses its own `sent_at + leaseDurationSeconds` local self-fencing deadline ([TLA-2](tla.md#tla-2)). So a conditional delete made after that wait only succeeds once the holder has already stopped. client-go's leader election uses the same idea: it trusts that the record changed, not the timestamps inside it.

  It scales because the run sleeps once for the longest candidate `leaseDurationSeconds`, not once per Lease or per namespace. The reaper keeps no state, and every action is a delete, so a run that stops partway is harmless.
- **Where we apply it:** `batch_reaper.py: reap` takes one `listed_at` reading after all lists return, sorts candidate Leases by `lease_duration` (shortest first), and sleeps until `listed_at + lease_duration` (18/N, lengthened from `renew_interval` to `lease_duration` in 22/N). `_delete_lease_if_unchanged` deletes with `V1Preconditions(resource_version=...)`. A 409 means the Lease changed, so skip. A 404 means it is already gone (18/N). `find_stale_leases` skips Leases with no `resourceVersion`, since their delete would have no precondition (18/N).

  Gotcha: count the wait from after the lists return, so it covers every read.
- **Source:** [client-go `leaderelection.go`](https://github.com/kubernetes/client-go/blob/master/tools/leaderelection/leaderelection.go) (package doc: it uses only the change in the record, so it tolerates arbitrary clock skew). [DeleteOptions](https://kubernetes.io/docs/reference/kubernetes-api/common-definitions/delete-options/) (`preconditions`).
- **Example:**
  ```text
  list:   lease rv=812, lease_duration=60
  sleep 60 s
  DELETE precondition rv=812 -> 409 (driver renewed) => skip
                             -> 200 => reaped
  ```
- **Field guide:** [LEASE-8](field-guide/lease.md#lease-8), [LEASE-9](field-guide/lease.md#lease-9), [CLEAN-23](field-guide/clean.md#clean-23), [PAT-34](patterns.md#pat-34), [TLA-2](tla.md#tla-2).

<a id="bp-18"></a>

### BP-18 Before each destructive round, check the Lease's UID and leaseTransitions (fencing-like)

- **Why it is a best practice:** holder identity alone can't prove you still own something. A Lease can be deleted and recreated under the same name, which gives it a new UID. Or another driver can attach and then detach, which leaves `holderIdentity` empty again but `leaseTransitions` higher. Checking both works like a fencing token: a number that proves "I am still the holder from tenure N".
- **Where we apply it:** `sandbox_batch.py: _take_batch_lease` compares `meta.uid` and `spec.lease_transitions` with the last Lease this handle wrote, rejects any other holder, then conditionally rewrites the Lease with this handle as holder (12/N; typed in 21/N). `_delete_batch_objects` calls it before every deletecollection round and deletes the Lease only at the `resourceVersion` that round wrote (12/N). Writing itself back as holder makes `get_batch` refuse to attach while claims are being deleted.

  How it compares, and its limit: this is fencing-like, not true fencing. In Kleppmann's design, the protected resource itself rejects stale tokens. The SandboxClaim API can't check our counter, so a small window remains between the Lease write and the deletecollection. client-go's package doc says plainly that it doesn't guarantee fencing either. So we keep each destructive step short and re-take the Lease every round.
- **Source:** [Martin Kleppmann, "How to do distributed locking"](https://martin.kleppmann.com/2016/02/08/how-to-do-distributed-locking.html) (fencing tokens). [client-go `leaderelection.go`](https://github.com/kubernetes/client-go/blob/master/tools/leaderelection/leaderelection.go) (package doc on fencing).
- **Example:**
  ```text
  last: uid=U1, transitions=3
  now:  uid=U1, transitions=4, holder=""
  -> BatchInUseError (someone attached and left)
  ```
- **Field guide:** [LEASE-14](field-guide/lease.md#lease-14), [LEASE-17](field-guide/lease.md#lease-17), [LEASE-18](field-guide/lease.md#lease-18), [LEASE-20](field-guide/lease.md#lease-20).

<a id="bp-19"></a>

### BP-19 Layer cleanup: release(), then reaper + GC, then shutdownTime

- **Why it is a best practice:** a client process can die without running any code. Each cleanup layer should cover the failure of the layer above, so no single crash leaks expensive resources forever. Every layer here is idempotent: deleting a deleted object is a 404 that counts as success.
- **Where we apply it:**
  1. A healthy driver calls `release()` (`_delete_batch_objects`, 12/N). This takes seconds.
  2. A driver killed with SIGKILL stops renewing. The reaper deletes the stale Lease, and the GC deletes the claims (18/N, 22/N). This takes about D + margin + D.
  3. With no reaper deployed, every claim still has a `shutdownTime`. `SandboxBatch._claim` sets `ceil(N/create_rps) + quorum_timeout + work_budget + BATCH_SHUTDOWN_MARGIN_SECONDS (600)` for the fill (12/N). `acquire()` sets `quorum_timeout + work_budget + 600` (16/N).

  The fill's `shutdownTime` includes the pacing time, so a large, slowly paced fill doesn't eat into `work_budget`.

  Gotcha: each backstop is slower than the one above it, so it doesn't replace calling `release()`. The reaper is an optional CronJob.
- **Source:** [Garbage collection](https://kubernetes.io/docs/concepts/architecture/garbage-collection/). We found no single canonical "defense in depth" source to cite; this is a general principle.
- **Field guide:** [LEASE-6](field-guide/lease.md#lease-6), [LEASE-27](field-guide/lease.md#lease-27), [CLEAN-14](field-guide/clean.md#clean-14), [K8S-3](field-guide/k8s.md#k8s-3).

<a id="bp-20"></a>

### BP-20 Put an explicit timeout on every request, and two timeouts on watches

- **Why it is a best practice:** neither `kubernetes` nor `kubernetes_asyncio` sets a default request timeout, so a half-dead TCP connection can block a call forever. On a background thread, nobody can interrupt it.

  Collections need a longer timeout than the apiserver's own `--request-timeout` (default 60 s), so the server ends a long call with a clear error before the client gives up blind. Watches need two timeouts. The server-side `timeoutSeconds` ends a healthy stream on schedule, so the loop can check for stop requests. A slightly longer client read timeout catches a connection that died without a FIN.
- **Where we apply it:** `batch_utils.py: BATCH_REQUEST_TIMEOUT_SECONDS = 30` and `BATCH_COLLECTION_REQUEST_TIMEOUT_SECONDS = 90` (2/N). `sandbox_batch.py: _WATCH_TIMEOUT_SECONDS = 30` and `_WATCH_REQUEST_TIMEOUT` = 30 + 5 (1/N). Every Lease, list, deletecollection, warm pool, and template call passes `_request_timeout` (2/N, 12/N). `_renew_request_timeout` also caps renewals by the time left (3/N).

  Gotcha: make the client timeout longer than the server timeout. Otherwise the client cuts off healthy streams and reports them as errors.
- **Source:** [kube-apiserver reference](https://kubernetes.io/docs/reference/command-line-tools-reference/kube-apiserver/) (`--request-timeout` default 1m0s). [API concepts](https://kubernetes.io/docs/reference/using-api/api-concepts/) (watch `timeoutSeconds`). [AWS Builders' Library, "Timeouts, retries, and backoff with jitter"](https://aws.amazon.com/builders-library/timeouts-retries-and-backoff-with-jitter/).
- **Example:**
  ```python
  watch(timeout_seconds=30, _request_timeout=35)
  list_claims(..., _request_timeout=90)   # above the apiserver's 60 s
  ```
- **Field guide:** [RETRY-8](field-guide/retry.md#retry-8), [RETRY-9](field-guide/retry.md#retry-9), [LIB-9](field-guide/lib.md#lib-9), [LIB-10](field-guide/lib.md#lib-10).

<a id="bp-21"></a>

### BP-21 Retry only known-transient errors; surface everything else through err()

- **Why it is a best practice:** retrying a 403 or 404 forever hides a real configuration problem. Not retrying a connection reset turns every blip into an outage. An allowlist of transient errors, plus a loud failure for everything else, gets both right.
- **Where we apply it:** `batch_utils.py: is_retryable_status` retries 429 and 5xx (1/N). `sandbox_batch.py: _TRANSPORT_ERRORS` and `_is_transport_error` cover connection errors, including `ssl.SSLEOFError` (8/N) and urllib3 `ConnectTimeoutError` (13/N), and unwrap wrapper exceptions to judge the real cause (2/N onward). A certificate error is not retried. `SandboxBatch._watch_loop` records a non-retryable error with `BatchState.note_error`, so `err()` is set instead of the thread dying silently (1/N). `_create_failure_message` reports the Status message, not `str(ApiException)`, which includes every header and the raw body (12/N).

  Gotcha: a domain exception that subclasses `TimeoutError` matches `asyncio.TimeoutError` on Python 3.11+, so exclude `BatchError` explicitly.
- **Source:** [Kubernetes API conventions: HTTP status codes](https://github.com/kubernetes/community/blob/master/contributors/devel/sig-architecture/api-conventions.md#http-status-codes). [AWS Builders' Library, "Timeouts, retries, and backoff with jitter"](https://aws.amazon.com/builders-library/timeouts-retries-and-backoff-with-jitter/).
- **Example:**
  ```text
  429 / 5xx / transport error -> retry
  409 on attempt >= 2         -> success (BP-5)
  403 / 404 / cert error      -> err() and stop
  ```
- **Field guide:** [RETRY-1](field-guide/retry.md#retry-1), [RETRY-4](field-guide/retry.md#retry-4), [LIB-1](field-guide/lib.md#lib-1), [LIB-2](field-guide/lib.md#lib-2), [LIB-4](field-guide/lib.md#lib-4), [LIB-5](field-guide/lib.md#lib-5), [LIB-19](field-guide/lib.md#lib-19), [API-8](field-guide/api.md#api-8).

<a id="bp-22"></a>

### BP-22 Exponential backoff with equal jitter

- **Why it is a best practice:** without jitter, every client that failed at the same moment retries at the same moment, and those retries cause the next overload. Marc Brooker's AWS post compares the variants. Ours is "Equal Jitter": double the wait up to a cap, then sleep a random time between half the wait and the full wait. It keeps a guaranteed minimum wait while still spreading clients apart.

  It scales because, with many drivers retrying at once, the load on the apiserver drops geometrically and arrives spread out instead of in spikes.
- **Where we apply it:** `batch_utils.py: backoff_delay` with `BATCH_BACKOFF_BASE_SECONDS = 0.5` and `BATCH_BACKOFF_MAX_SECONDS = 30` (1/N). It caps the exponent at `ceil(log2(cap / base))`, so a long outage never computes a huge `2**n`. `retry_delay` wraps it (12/N). The watch, renewal, create retries, and `_delete_claims_in_rounds` all use it.

  Gotcha: reset the attempt counter on success. The watch resets `failures` on any event. Renewal resets it when degraded clears.
- **Source:** [Marc Brooker, "Exponential Backoff And Jitter" (AWS Architecture Blog)](https://aws.amazon.com/blogs/architecture/exponential-backoff-and-jitter/) (the Equal Jitter variant). [AWS Builders' Library, "Timeouts, retries, and backoff with jitter"](https://aws.amazon.com/builders-library/timeouts-retries-and-backoff-with-jitter/).
- **Example:**
  ```text
  retry 1:  wait 0.5 -> sleep 0.25..0.5
  retry 2:  wait 1.0 -> sleep 0.5..1.0
  retry 7+: wait 30  -> sleep 15..30
  ```
- **Field guide:** [RETRY-2](field-guide/retry.md#retry-2), [PAT-28](patterns.md#pat-28).

<a id="bp-23"></a>

### BP-23 One retry owner per layer: turn off hidden library retries where the SDK owns deadlines

- **Why it is a best practice:** retries stacked at several layers multiply. Three urllib3 tries times three SDK tries is nine requests per logical call. They also break the outer layer's timing and conditional writes. The Google SRE book names multi-layer retries as a classic amplifier of cascading failures.
- **Where we apply it:** the sync Lease and batch custom-object clients share one unretried `ApiClient` (`retries=False`) via `k8s_helper.py: _api_client_without_retries` (introduced for Leases in 3/N and extended to `_batch_custom_objects_api` in 22/N). Single-sandbox `SandboxClient` calls stay on `custom_objects_api` with default retries. `_UnretriedApiClient` shallow-copies the caller's configuration and reads `default_headers` and `cookie` live from the source client (7/N, 15/N, 22/N). aiohttp never retries on its own.

  Why these settings: urllib3 retried timed-out connects and reads with a fresh timeout, stretching Lease- or deadline-bounded requests up to 4x their bound. A retried Lease write that already landed comes back 409, which looks like a takeover. And urllib3 sleeps up to 6 hours on `Retry-After` even when status retries are off.

  Gotcha: changing retry config changes which exceptions escape. With `retries=False`, urllib3 2.x raises raw `ConnectTimeoutError` and `NewConnectionError` instead of `MaxRetryError`. Re-audit your transient-error list after any retry change.
- **Source:** [urllib3 `Retry`](https://urllib3.readthedocs.io/en/stable/reference/urllib3.util.html) (`retries=False`, `respect_retry_after_header`). [Google SRE book, "Addressing Cascading Failures"](https://sre.google/sre-book/addressing-cascading-failures/) (retries at multiple layers). [aiohttp client reference](https://docs.aiohttp.org/en/stable/client_reference.html).
- **Field guide:** [LIB-3](field-guide/lib.md#lib-3), [LIB-6](field-guide/lib.md#lib-6), [LIB-7](field-guide/lib.md#lib-7), [LIB-8](field-guide/lib.md#lib-8), [LIB-12](field-guide/lib.md#lib-12), [LIB-21](field-guide/lib.md#lib-21).

<a id="bp-24"></a>

### BP-24 Scope backoff by cause: Retry-After moves the shared cursor, a blip delays one request

- **Why it is a best practice:** server pushback applies to everyone, so everyone should wait. A single connection blip is local, so only the request that hit it should wait. Putting a local blip on a shared limiter stalls every worker. Ignoring server pushback keeps hammering an overloaded server.
- **Where we apply it:** `sandbox_batch.py: SandboxBatch._create_one` (13/N). Without Retry-After, the worker sleeps a local `retry_delay`, then takes the next normal slot with `_reserve_create_slot(now)`. With Retry-After, it reserves `_reserve_create_slot(now + retry_after)`, which moves the shared cursor for all workers. `batch_utils.py: retry_after_seconds` accepts only ASCII integer seconds, caps them at 30 s, and ignores the HTTP-date form (13/N).

  How it compares: client-go's `withRetry.Before()` sleeps the request's own backoff (the larger of its backoff and the server's Retry-After), then sends the retry through the client's shared rate limiter. So a blip delays only that request, and the retry still counts against QPS. We match that for blips. For Retry-After we go further on purpose: API Priority and Fairness queues and rejects requests per priority level, and all our creates share that level, so they should all wait. The 30 s cap stops a buggy header from parking the batch for hours.

  Gotcha: use `isascii() and isdigit()`, because `'²'.isdigit()` is True but `float()` rejects it.
- **Source:** [client-go `rest/with_retry.go`](https://github.com/kubernetes/client-go/blob/master/rest/with_retry.go) (`withRetry.Before`). [client-go `rest/request.go`](https://github.com/kubernetes/client-go/blob/master/rest/request.go) (`retryAfterSeconds` parses integer seconds only, with `strconv.Atoi`). [API Priority and Fairness](https://kubernetes.io/docs/concepts/cluster-administration/flow-control/) (429 with Retry-After). [KEP-1040](https://github.com/kubernetes/enhancements/tree/master/keps/sig-api-machinery/1040-priority-and-fairness) (the 1-32 s Retry-After range quoted in the 13/N body *(unconfirmed)*).
- **Example:**
  ```text
  503, no Retry-After: this worker sleeps 0.25-0.5 s, then takes the next slot
  429, Retry-After: 2:  _next_create_at = now + 2 for ALL workers
  ```
- **Field guide:** [RETRY-6](field-guide/retry.md#retry-6), [RETRY-16](field-guide/retry.md#retry-16), [LIB-17](field-guide/lib.md#lib-17).

<a id="bp-25"></a>

### BP-25 Pace with one shared reservation cursor, and count retries against it

- **Why it is a best practice:** this is a minimal virtual-scheduling limiter, the same idea as GCRA. Each caller reserves the next free time slot under a lock, then sleeps until it. So N workers together never exceed the rate, and a burst of retries can't double it.

  The rate is per batch. Total cluster load is the sum over batches, so operators size `create_rps` against the apiserver's capacity.
- **Where we apply it:** `sandbox_batch.py: SandboxBatch._reserve_create_slot` does `slot = max(earliest, _next_create_at)` and advances `_next_create_at` by `1/create_rps` (12/N). `_create_worker` reserves under `_lock` and sleeps outside it (12/N). `batch_utils.py: WarmCredit` lets creates that a Ready warm-pool sandbox can serve skip pacing, since they start no new pod. A 429 pauses that bypass for a bounded cooldown (19/N).

  How it compares: client-go uses a token bucket (QPS and Burst) inside every REST client. Ours has no burst: it spaces requests evenly.

  Gotcha: reserve under the lock, sleep outside it. Sleeping while holding the lock serializes every worker.
- **Source:** [client-go `util/flowcontrol/throttle.go`](https://github.com/kubernetes/client-go/blob/master/util/flowcontrol/throttle.go) (`NewTokenBucketRateLimiter(qps, burst)`, the token bucket behind QPS/Burst). [Generic cell rate algorithm](https://en.wikipedia.org/wiki/Generic_cell_rate_algorithm) (virtual scheduling).
- **Example** (50 rps, interval 0.02 s):
  ```text
  w1: slot=t0       cursor=t0+0.02
  w2: slot=t0+0.02  cursor=t0+0.04
  ```
- **Field guide:** [RETRY-5](field-guide/retry.md#retry-5), [RETRY-15](field-guide/retry.md#retry-15), [LOCK-9](field-guide/lock.md#lock-9), [PERF-13](field-guide/perf.md#perf-13), [PAT-17](patterns.md#pat-17).

<a id="bp-26"></a>

### BP-26 Propagate the caller's deadline and shed hopeless work before reserving capacity

- **Why it is a best practice:** a request that can't finish in time still costs something: an ordinal, a pacing slot that pushes everyone behind it later, an apiserver write, and then a DELETE to clean up. Asking "can I possibly make it?" first turns that waste into a fast, cheap failure. Under overload, queues stay short because hopeless work never joins them.
- **Where we apply it:** `sandbox_batch.py: SandboxBatch.acquire` raises `BatchTimeoutError` when `max(now, _next_create_at) >= deadline`, before `start_acquire` takes an ordinal (16/N; refined in 19/N, where warm-covered creates skip the check). `_check_retry_deadline` does the same for retries (16/N). `_send_create` waits on `_in_flight` for at most the time left (19/N). `_refresh_warm_credit` caps its GET at `min(30, remaining)` (19/N).

  Gotcha: re-check after every sleep, because other callers keep taking slots during your backoff.
- **Source:** [Google SRE book, "Addressing Cascading Failures"](https://sre.google/sre-book/addressing-cascading-failures/) (deadline propagation, load shedding). [gRPC Deadlines guide](https://grpc.io/docs/guides/deadlines/). [AWS Builders' Library, "Using load shedding to avoid overload"](https://aws.amazon.com/builders-library/using-load-shedding-to-avoid-overload/).
- **Example:**
  ```text
  deadline = now + 5, _next_create_at = now + 7
  => BatchTimeoutError (no ordinal, no POST)
  ```
- **Field guide:** [RETRY-7](field-guide/retry.md#retry-7), [CANCEL-14](field-guide/cancel.md#cancel-14), [LOCK-7](field-guide/lock.md#lock-7).

<a id="bp-27"></a>

### BP-27 Bulkhead: cap I/O with one semaphore, and keep pool headroom for the watch and renewals

- **Why it is a best practice:** a bulkhead keeps one kind of work from sinking the rest. Here the critical work is Lease renewal and the watch. If creates take every pooled connection, the renewal waits for a free one, and a long enough wait lets the Lease expire.
- **Where we apply it:** `sandbox_batch.py: SandboxBatch._in_flight = threading.BoundedSemaphore(max_in_flight)`, acquired per HTTP attempt for creates and deletes (15/N), and around pool reads in `_check_pool` and `_refresh_warm_credit` (19/N). `batch_utils.py: check_pool_size` requires `max_in_flight + BATCH_POOL_HEADROOM` (2) connections: one for the long-lived watch and one for renewals (17/N). It clamps a default `max_in_flight` and logs, and raises for an explicit value that doesn't fit (or a pool with no room for even one create). The semaphore is held only during live I/O, never across a backoff sleep.

  Gotcha: defaults must compose. Raising on small pools broke `SandboxClient().claim_batch()` out of the box, which is why a default value clamps instead of raising.
- **Source:** [Azure Architecture Center, "Bulkhead pattern"](https://learn.microsoft.com/en-us/azure/architecture/patterns/bulkhead). Michael Nygard's book *Release It!* also has a Bulkheads chapter (no URL).
- **Example:**
  ```text
  pool=22, max_in_flight=20: 20 creates + 1 watch + 1 renewal
  pool=10, default max_in_flight -> clamped to 8 (logged)
  ```
- **Field guide:** [RETRY-19](field-guide/retry.md#retry-19), [PERF-16](field-guide/perf.md#perf-16), [PERF-17](field-guide/perf.md#perf-17), [PERF-18](field-guide/perf.md#perf-18), [LOCK-10](field-guide/lock.md#lock-10).

<a id="bp-28"></a>

### BP-28 Single-flight: concurrent identical reads share one in-flight call

- **Why it is a best practice:** a burst of 500 `acquire()` calls on a new pool should cost 2 precheck GETs (WarmPool and Template), not 1,000. Go's `singleflight` package is the standard version of this pattern.
- **Where we apply it:** `SandboxBatch._prechecks: dict[str, Future]` and `_check_pool` (16/N, both shells). `_credit_refresh_locks` and `_refresh_warm_credit` (19/N). The tricky parts: shield the shared Future from one waiter's timeout, let a surviving waiter take over if the leader is interrupted ([BP-29](#bp-29)), cache only successes, and never cache a negative result when the leader's own deadline cut the read short.

  Gotcha: a failed check must clear its map entry, so a later caller retries instead of inheriting the failure.
- **Source:** [golang.org/x/sync/singleflight](https://pkg.go.dev/golang.org/x/sync/singleflight).
- **Example:**
  ```text
  acquire 1:      _prechecks[p] = Future(); runs the GETs
  acquire 2..500: wait on shield(_prechecks[p])
  ```
- **Field guide:** [LOCK-15](field-guide/lock.md#lock-15), [LOCK-16](field-guide/lock.md#lock-16), [RETRY-18](field-guide/retry.md#retry-18), [CANCEL-18](field-guide/cancel.md#cancel-18), [LIB-18](field-guide/lib.md#lib-18), [PAT-12](patterns.md#pat-12).

## Python concurrency

<a id="bp-29"></a>

### BP-29 In asyncio, shield shared work, use `Task.cancelling()`, and clean up on BaseException

- **Why it is a best practice:** since Python 3.8, `asyncio.CancelledError` is a `BaseException`, like `KeyboardInterrupt`. A plain `except Exception` skips cleanup exactly when the user presses Ctrl-C or a task is cancelled. `asyncio.shield` stops one waiter's timeout from cancelling work others depend on. `Task.cancelling()` (3.11+) tells you whether *this* task has a pending cancel, so you can tell "I was cancelled" from "my leader was".
- **Where we apply it:** `async_sandbox_batch.py: AsyncSandboxBatch._check_pool` awaits `asyncio.wait_for(asyncio.shield(check), ...)`, and takes over only when `check.cancelled() and not task.cancelling()` (16/N). `SandboxBatch._claim` (both shells) deletes the just-created Lease on `except BaseException`, so an interrupt during the follow-up list leaves no orphan Lease (12/N). `acquire()` runs `_drop_acquired` on BaseException (16/N).

  Gotcha: after catching BaseException, always re-raise. Swallowing a cancel breaks structured concurrency. The safest critical section has no `await` at all.
- **Source:** [Python asyncio tasks](https://docs.python.org/3/library/asyncio-task.html) ("Shielding From Cancellation", `Task.cancelling()`). [Python exception hierarchy](https://docs.python.org/3/library/exceptions.html#exception-hierarchy).
- **Example:**
  ```python
  try:
      await asyncio.wait_for(asyncio.shield(fut), t)
  except asyncio.CancelledError:
      if fut.cancelled() and not asyncio.current_task().cancelling():
          continue   # the leader was cancelled, not me
      raise
  ```
- **Field guide:** [CANCEL-1](field-guide/cancel.md#cancel-1), [CANCEL-2](field-guide/cancel.md#cancel-2), [CANCEL-3](field-guide/cancel.md#cancel-3), [CANCEL-17](field-guide/cancel.md#cancel-17), [CANCEL-18](field-guide/cancel.md#cancel-18), [CLEAN-15](field-guide/clean.md#clean-15).

<a id="bp-30"></a>

### BP-30 Wait on a Condition in a predicate loop, with a timed wake for deadlines

- **Why it is a best practice:** `Condition.wait` can return when the thing you want hasn't happened. Another consumer took the item, the wake was about something else, or it was spurious. So you re-check the predicate in a loop, as the Python docs show. Pass a timeout whenever a state change is driven by time instead of by a notify.
- **Where we apply it:** `sandbox_batch.py: SandboxBatch._next` loops under `self._changed`, pops, checks done, and waits with a timeout of `fill_deadline - now` (13/N). Without the timed wait, pending members would never expire if no watch event arrived. Single-key waiters in `acquire()` get their own Event in `_acquire_waiters`, so unrelated changes don't wake them (16/N).

  Gotcha: every state flip waiters depend on (detach, err, release) must notify.
- **Source:** [Python threading: Condition objects](https://docs.python.org/3/library/threading.html#condition-objects).
- **Example:**
  ```python
  with cond:
      while not ready():
          cond.wait(deadline - time.monotonic())
  ```
- **Field guide:** [LOCK-7](field-guide/lock.md#lock-7), [LOCK-12](field-guide/lock.md#lock-12), [LOCK-13](field-guide/lock.md#lock-13).

<a id="bp-31"></a>

### BP-31 Monotonic clock for local timing; wall clock only for values other machines read

- **Why it is a best practice:** the wall clock can jump. NTP steps it, an admin changes it, a VM resumes. A deadline computed from it can fire hours early or never. `time.monotonic()` only moves forward, which is why PEP 418 added it. The Lease protocol still needs wall time, because `renewTime` has to mean something on other machines. That is exactly where clock skew enters, and why [BP-16](#bp-16) and [BP-17](#bp-17) exist.
- **Where we apply it:** `_lease_expires_at`, `_fill_deadline`, `_next_create_at`, and `acquire()` deadlines all use `time.monotonic()` (1/N onward; expiry in 3/N). `batch_utils.py: WarmCredit` uses it (19/N). `batch_utils.py: lease_now` gives wall time for Lease fields (1/N). `batch_reaper.py: reap` uses monotonic for its wait, and wall time only to compare against `renewTime` (18/N).

  Gotcha: never subtract a monotonic value from a wall-clock value. Give them clearly different names, like `renewed_at` (monotonic) and `renew_time` (datetime).
- **Source:** [Python `time.monotonic`](https://docs.python.org/3/library/time.html#time.monotonic). [PEP 418](https://peps.python.org/pep-0418/).
- **Example:**
  ```python
  deadline = time.monotonic() + timeout     # local
  spec.renew_time = lease_now()             # shared with other machines
  ```
- **Field guide:** [LEASE-5](field-guide/lease.md#lease-5), [CANCEL-14](field-guide/cancel.md#cancel-14).

<a id="bp-32"></a>

### BP-32 Background threads are daemons; asyncio tasks get names

- **Why it is a best practice:** a non-daemon watch thread would keep the process alive forever after `main()` returns. Daemon threads are stopped abruptly at shutdown, so they must never be the only thing guaranteeing cleanup. Named tasks show up in `asyncio.all_tasks()`, in "Task was destroyed but it is pending" warnings, and in debuggers. With many batches in one process, the name tells you which batch is stuck.
- **Where we apply it:** `sandbox_batch.py: SandboxBatch._start` and `_claim` create the watch, renew, and creator threads with `daemon=True` (1/N, 12/N). `async_sandbox_batch.py` names its tasks `batch-<id>-watch`, `batch-<id>-renew`, and `batch-<id>-create-<i>` (11/N; the creator tasks since 12/N). Cleanup that must happen lives in explicit `release()`/`detach()` and exit hooks, and Lease expiry covers the rest ([BP-19](#bp-19)).
- **Source:** [Python threading: Thread objects](https://docs.python.org/3/library/threading.html#thread-objects) (daemon threads are abruptly stopped at shutdown). [`asyncio.create_task`](https://docs.python.org/3/library/asyncio-task.html#asyncio.create_task) (`name=`).
- **Example:**
  ```python
  asyncio.create_task(self._watch_loop(), name=f"batch-{self.batch_id}-watch")
  ```
- **Field guide:** [CLEAN-21](field-guide/clean.md#clean-21), [CLEAN-22](field-guide/clean.md#clean-22), [LEASE-23](field-guide/lease.md#lease-23).

<a id="bp-33"></a>

### BP-33 Keep state transitions in a Sans-I/O core

- **Why it is a best practice:** pure state code has no network, locks, or timers, so you can unit-test every transition without mocks. Sharing one core between a threading shell and an asyncio shell removes a whole class of sync/async drift bugs.
- **Where we apply it:** `batch_state.py: BatchState` holds every transition and invariant (1/N, shared by both shells from the start; extended for `acquire()` in 16/N). The shells in `sandbox_batch.py` and `async_sandbox_batch.py` only schedule, lock, and do I/O.

  Gotcha: the core is lock-free on purpose, so the shells must call it under their own lock.
- **Source:** [Sans-I/O protocol libraries](https://sans-io.readthedocs.io/).
- **Example:**
  ```python
  with self._lock:
      member = self._state.upsert_claim(obj)
  ```
- **Field guide:** [TEST-3](testing.md#test-3), [TEST-24](testing.md#test-24), [CANCEL-24](field-guide/cancel.md#cancel-24).

## Operability

<a id="bp-34"></a>

### BP-34 Log per item at DEBUG, log once per degraded episode, and expose a pollable health flag

- **Why it is a best practice:** at 5,000 claims, an INFO line per create is 5,000 lines of noise, and the important line gets lost. Episode logging (log on entering a bad state, stay quiet while in it) keeps a flapping apiserver from flooding logs, and still says when trouble started. A boolean health flag lets callers build alerts or backpressure without parsing logs.
- **Where we apply it:** `k8s_helper.py: create_sandbox_claim` takes a `log_level`, and batch creates pass `logging.DEBUG` (12/N). `SandboxBatch._mark_renewal_degraded` logs only on the transition into degraded (3/N), and `lease_degraded()` exposes it (3/N). `WarmCredit.throttle` returns whether a new cooldown started, and `_create_one` logs only then (19/N).

  Gotcha: clear the episode flag on recovery (`_renewal_degraded = False` after a good renewal). Otherwise the next episode logs nothing.
- **Source:** [Python Logging HOWTO](https://docs.python.org/3/howto/logging.html) (when to use each level).
- **Example:**
  ```python
  if not self._renewal_degraded:
      logger.info("lease renewal degraded: %s", e)
      self._renewal_degraded = True
  ```
- **Field guide:** [PERF-9](field-guide/perf.md#perf-9), [API-12](field-guide/api.md#api-12).

<a id="bp-35"></a>

### BP-35 Make a cron cleanup job stateless, delete-only, dry-runnable, with a clear exit code

- **Why it is a best practice:** a CronJob may run twice, run late, or be killed midway. If every action is an idempotent, conditional delete, all of those are harmless. A non-zero exit shows up as a failed Job, which existing alerting already watches. A dry run lets operators preview a first deployment safely.
- **Where we apply it:** `batch_reaper.py: main` offers `--dry-run`, floors `--skew-margin`, and returns 1 if any error was logged (18/N). `reap` logs and skips a namespace it can't list or a Lease it can't delete, so one misconfigured namespace doesn't stop the others (18/N). `_default_namespace` reads `POD_NAMESPACE`, then the service-account namespace file, in the same order as client-go's in-cluster client config in `clientcmd` (18/N). The module docstring suggests `concurrencyPolicy: Forbid`.

  RBAC can't restrict deletes by label, and `resourceNames` can't cover names you don't know yet. So `_batch_id_of` re-checks the name and the id label itself before anything is deleted.
- **Source:** [CronJob concurrency policy](https://kubernetes.io/docs/concepts/workloads/controllers/cron-jobs/#concurrency-policy). [RBAC: referring to resources](https://kubernetes.io/docs/reference/access-authn-authz/rbac/#referring-to-resources). [client-go `tools/clientcmd/client_config.go`](https://github.com/kubernetes/client-go/blob/master/tools/clientcmd/client_config.go) (`inClusterClientConfig.Namespace`).
- **Example:**
  ```text
  python -m k8s_agent_sandbox.batch_reaper --namespaces a,b --dry-run
  exit 0 = clean, exit 1 = something logged an error
  ```
- **Field guide:** [CLEAN-23](field-guide/clean.md#clean-23), [K8S-17](field-guide/k8s.md#k8s-17), [K8S-20](field-guide/k8s.md#k8s-20), [LEASE-10](field-guide/lease.md#lease-10).

<a id="bp-36"></a>

### BP-36 Implicit teardown undoes only what that scope acquired

- **Why it is a best practice:** a script that inspects a batch with `get_batch` and exits must not delete someone else's training run. Implicit cleanup (context managers, exit hooks) should reverse only what that scope took. Explicit destructive calls can do more.
- **Where we apply it:** `sandbox_batch.py: _attached` and `_exit_detaches` mark batches this client attached (13/N). Client exit cleanup in `sandbox_client.py` and `async_sandbox_client.py` releases batches this client claimed and only detaches attached ones. `delete_all()` releases everything (12/N, 13/N).

  How it compares: client-go's ReleaseOnCancel gives up the Lease without deleting anything. Dask's `Client.close` (also cited in the 13/N PR body) works the same way: it shuts down a local cluster only if that client started it.

  Gotcha: the async exit hook can't await `detach()`, so it leaves attached batches to Lease expiry.
- **Source:** [client-go `leaderelection.go`](https://github.com/kubernetes/client-go/blob/master/tools/leaderelection/leaderelection.go) (`ReleaseOnCancel`). [Dask distributed `Client.close`](https://distributed.dask.org/en/stable/api.html#distributed.Client.close).
- **Example:**
  ```python
  with SandboxClient() as c:
      b = c.get_batch(batch_id)   # on exit: detach, not release
  ```
- **Field guide:** [CLEAN-11](field-guide/clean.md#clean-11), [CLEAN-12](field-guide/clean.md#clean-12), [LEASE-22](field-guide/lease.md#lease-22), [LEASE-23](field-guide/lease.md#lease-23).

<a id="bp-37"></a>

### BP-37 Layer small-scope model checking, stateful property fuzzing, differential testing, and deterministic fault simulation

- **Why it is a best practice:** example-based unit suites (`1,100` passing batch and RL tests on `e84e964c`) check only the execution schedules and error shapes their authors thought to script. Combining orthogonal verification layers catches bugs that slip through every single-method blind spot: explicit-state model checking (`TLA+` / BFS) proves multi-actor protocol and lock-order invariants over small domains, stateful property fuzzing (`Hypothesis`) shrinks unexpected call sequences against a reference oracle, differential testing catches silent divergence between twin implementations (`sync` vs. `async`, `FakeFleet` vs. `SandboxFleet`), and deterministic fault simulation exposes OS socket, HTTP framing, and clock-truncation bugs in milliseconds.
- **Where we apply it:** `dev/tools/run_batch_verification.sh` runs `10` deterministic stages across `BatchLease.tla` + `5` subsystem TLA+ specs (`handoff/work/tla/`), `verify_delete_lifecycle_model.py` + `verify_cancel5_exercise.py`, `dev/tools/verify_batch_static.py`, `test_batch_state_fuzz.py` + `batch_invariant_oracle.py`, `test_batch_differential.py`, `test_batch_fault_injection.py` + `test_batch_fault_injection_rl.py`, and `test_batch_concurrency_stress.py`, uncovering `29` verified production bugs on a green unit-test baseline.
- **Source:** Chris Newcombe et al., ["How Amazon Web Services Uses Formal Methods"](https://doi.org/10.1145/2699417), *Communications of the ACM* 58(4), 2015. Jingyu Zhou et al., ["FoundationDB: A Distributed Unbundled Transactional Key Value Store"](https://doi.org/10.1145/3448016.3457559), *SIGMOD* 2021 (deterministic simulation testing). Koen Claessen and John Hughes, ["QuickCheck: A Lightweight Tool for Random Testing of Haskell Programs"](https://doi.org/10.1145/351240.351266), *ICFP* 2000; [Hypothesis stateful testing](https://hypothesis.readthedocs.io/en/latest/stateful.html). William M. McKeeman, ["Differential Testing for Software"](https://www.hpl.hp.com/hpjournal/dtj/vol10num1/vol10num1art9.pdf), *Digital Technical Journal* 10(1), 1998.
- **Example:**
  ```text
  dev/tools/run_batch_verification.sh --quick   # <30 s fast presubmit gate
  dev/tools/run_batch_verification.sh           # full 10-stage formal + fuzz + chaos sweep
  ```
- **Field guide:** [VER-13](verification.md#ver-13), [VER-16](verification.md#ver-16), [TLA-6](tla.md#tla-6), [TEST-29](testing.md#test-29), [TEST-30](testing.md#test-30), [CHAOS-1](chaos-verification-suite.md#chaos-1).

---

[Learnings home](../learnings.md) · [Field guide index](../learnings.md#sec-field-guide)
