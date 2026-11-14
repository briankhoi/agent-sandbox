<a id="sec-perf"></a>

[Learnings home](../../learnings.md) · [Field guide index](../../learnings.md#sec-field-guide)

# Performance and memory at scale

This section covers how the batch handle stays fast and lean when a batch holds thousands of claims across hundreds of warm pools. One idea ties it together: the cost of each step should depend on the work in front of it, not on the size of the batch. A watch event should touch only the pool it changes, a list should hold one page at a time, and a request should be sent only when it buys something. The first entries are general patterns (incremental bookkeeping, paging, dropping data you never read). Later entries are tuned to specific paths in this SDK, like warm credit, the reaper, and connection pool sizing. Numbers come only from commits, code, and the project notes.

<a id="perf-1"></a>

## PERF-1 Keep per-event work O(1) with counters and a "still blocking" set

**Takeaway:** When a verdict depends on many items, keep running counters and a set of keys that still block the verdict. Each event then re-checks only the key it touched, so cost per event stays flat as the batch grows.

<details>
<summary>The full story (13/N, 14/N, 15/N)</summary>

- **Situation:** The design targets tens of thousands of claims and thousands of groups. `BatchState` (the Sans-I/O core, meaning pure state logic with no network calls, locks, or timers) runs one state transition per watch event. A watch is a long-lived Kubernetes request that streams every change to matching objects.
- **What goes wrong:** The old design had `cannot_arrive_counts()`, which built a `Counter` over a group's unable members on every create failure. If all 20k claims in a group got a 403, that is O(N²) work. Its `_all_groups_have_verdicts()` was O(G) on every consumer wake (pr2_design.md, "Scale").
- **What we do:** `batch_state.py: BatchState._unreachable_error` checks `size - (failed + lost + released) < min_ready` from per-pool integers (`_failed`, `_lost`, `_released_count`). The quorum keeps `_groups_short_of_quorum`, the set of pools still below min_ready. Each fill change re-checks only the changed pool and adds it to or removes it from the set (`_check_quorum`). The quorum is reached when the set is empty. The set starts as every pool, so `set_mode(QUORUM)` removes the pools that are already satisfied, including pools with min_ready=0.
- **Example:**
  ```text
  t0 set_mode(QUORUM): short = {A, B, C}; A has min_ready=0 -> short = {B, C}
  t1 B reaches min_ready -> recheck B only -> short = {C}
  t2 C reaches min_ready -> short = {} -> quorum reached
  ```
- **Related:** [PERF-2](#perf-2), [PERF-3](#perf-3), [PAT-1](../patterns.md#pat-1).

</details>

<a id="perf-2"></a>

## PERF-2 Use an insertion-ordered dict as an ordered set, and pick cohorts by arrival order

**Takeaway:** For a FIFO group that also needs removals from the middle, use a dict with `None` values: O(1) insert and remove, and it keeps insertion order. When you need "the first k ready", pick by arrival order, not by identity, so one slow item can't hold up the rest.

<details>
<summary>The full story (13/N)</summary>

- **Situation:** Each group holds members that are Ready but not yet handed out until its cohort is decided. Members can flap between Ready and NotReady.
- **What goes wrong:** A list makes it O(n) to remove a held member that goes NotReady. A set loses arrival order. The old design picked the lowest ordinals (the claim's number in the batch) as the cohort, so the caller waited for specific claims while other claims were already Ready.
- **What we do:** `batch_state.py: BatchState._waiting` is `dict[str, dict[str, None]]` (pool to ordered set of claim names). The cohort is `list(waiting)[:min_ready]`, the first members to become Ready (`_decide_group_outcome`, `_check_quorum`). The rest go to `events()` at the same moment. A held member that goes NotReady is popped, and it is appended at the end when it is Ready again.
- **Example:**
  ```text
  Ready order: b-3, b-0, b-1   min_ready=2
  cohort -> [b-3, b-0]; b-1 goes to events()
  ```
  Pinned by `test_batch_state.py: test_success_yields_first_min_ready_by_ready_order_then_streams_the_rest_once`.
- **Related:** [PERF-1](#perf-1), [PAT-3](../patterns.md#pat-3).

</details>

<a id="perf-3"></a>

## PERF-3 Cancel queued items lazily with a tombstone set checked at pop time

**Takeaway:** To cancel items that already sit in a FIFO queue, record the cancellation in a set and skip matching items when you consume them. This is lazy deletion: O(1) at cancel time instead of an O(n) scan of the queue.

<details>
<summary>The full story (15/N, 113fae28)</summary>

- **Situation:** A `MEMBER_READY` event can already be queued when the caller releases that member.
- **What goes wrong:** Removing the event from the deque at release time costs O(queue length). Leaving it hands out a member the caller just deleted.
- **What we do:** `release_claim` adds the name to `_released`, a set that other checks already use. `batch_state.py: BatchState.pop_event` skips `MEMBER_READY` entries whose claim is in `_released`. Status events still go through.
- **Example:**
  ```text
  queue: [READY b-0, READY b-1]
  release_member(b-0)   # O(1): add to _released
  pops yield only b-1
  ```
  Pinned by `TestRelease.test_a_ready_event_queued_before_release_is_not_handed_out`.
- **Related:** [WATCH-4](watch.md#watch-4) covers the same `_released` set as a tombstone against late watch events. See also [PAT-4](../patterns.md#pat-4).

</details>

<a id="perf-4"></a>

## PERF-4 Page large Kubernetes lists at 500 and end the loop only on an empty continue token

**Takeaway:** List large collections with `limit` and `continue`, take the resourceVersion for a follow-up watch from the first page, and stop only when the continue token is empty, never on an empty page. Paging cuts the transient memory (raw bytes and decoded text), but not what the caller keeps.

<details>
<summary>The full story (20/N, a95cb2a1)</summary>

- **Situation:** `list_sandbox_claim_objects` backs attach (`get_batch`), the `claim_batch` leftover check, the watch's re-list after a 410, and (until fc9d8557 moved them to a page-by-page count, [PERF-5](#perf-5)) release rounds. Before 20/N it read every claim of a batch in one response. A continue token is the server's bookmark for "the next page". A resourceVersion (RV) is the server's version stamp for a list or object.
- **What goes wrong:** A 5,000-claim list peaked at 69.4 MB: about 23 MB of transient response bytes and decoded text on top of 46 MB of parsed dicts. The peak grows linearly with N (138.9 MB at 10,000), per pr8_plan.md and C1-pr8-impl.md.
- **What we do:** One page iterator per helper (`k8s_helper.py: _claim_list_pages`, and the same in `async_k8s_helper.py`) lists in pages of `CLAIM_LIST_PAGE_SIZE = 500`, the client-go pager's default, and follows `metadata.continue`. A paged list is one consistent snapshot, so the helper returns the first page's RV, and the watch still starts with no gap. The loop ends only on an empty continue token, because a page can hold zero items while more follow. Measured at N=5,000: list peak 69.4 MB to 48.7 MB, and all 10 pages carried one RV. We rejected streaming JSON parsing with ijson, because it adds a dependency and needs raw-response code in both shells.
- **Example:**
  ```text
  pages: {items:[a], continue:t1, rv:42}
         {items:[],  continue:t2}
         {items:[b]}
  -> items [a, b], rv "42"
  ```
- **Related:** [PERF-5](#perf-5), [PERF-6](#perf-6). [K8S-9](k8s.md#k8s-9) covers the consistent snapshot and the single restart on an expired continue token. [TEST-19](../testing.md#test-19) covers the weakref test that proves each page is freed.

</details>

<a id="perf-5"></a>

## PERF-5 When you need only a count, fold it page by page and drop each page

**Takeaway:** If a loop needs only an aggregate (like "how many live objects are left"), page through a consistent read and keep just the running number. If the list can restart, reset the accumulator on restart so no page counts twice.

<details>
<summary>The full story (20/N, fc9d8557)</summary>

- **Situation:** Between deletecollection rounds, `release()` and `release_group()` re-listed the whole batch only to count claims without `deletionTimestamp` (the field Kubernetes sets when an object is being deleted). deletecollection is one request that deletes every object matching a label.
- **What goes wrong:** When deletecollection hits its 90 s client timeout or the apiserver's 60 s 504 on a large batch, the re-list holds every remaining claim at once. On the forced-timeout path this peaked at 81.7 MB at N=5,000. Three normal-path bench runs also hit the 504, so the path is not rare under load (C1-pr8-impl.md). The notes infer this re-list is the peak around 20k+ claims at the measured 230 to 270 deletes/s (pr8_plan.md, inferred).
- **What we do:** `k8s_helper.py: K8sHelper.count_live_sandbox_claims` (and its async twin) reuses `_claim_list_pages` with a consistent read (no resourceVersion, so a lagging watch cache can't report zero too early). It adds up live claims per page and runs `del page`. If a continue token expires (410), the list restarts once, and the `first` flag resets the count. Peak on the timeout path fell to 9.7 to 11.8 MB, and stayed at 9.9 MB at N=10,000. Rejected alternatives:
  - `limit=1`: terminating claims can't be filtered on the server, so one item can't prove none is live.
  - `remainingItemCount`: it counts terminating claims too.
  - The batch watch: `release()` stops it, it is absent on the atexit path, and it can lag.
  - A CRD field selector on `deletionTimestamp`: CRDs can't select on it (inferred).
- **Example:**
  ```python
  for page, first in _claim_list_pages(...):
      if first:
          live = 0  # list restarted: drop earlier pages' counts
      live += sum(1 for c in page.get("items", []) if not (c.get("metadata") or {}).get("deletionTimestamp"))
      del page
  ```
- **Related:** [PERF-4](#perf-4). [K8S-4](k8s.md#k8s-4) covers why only claims without deletionTimestamp count as live.

</details>

<a id="perf-6"></a>

## PERF-6 Strip managedFields from listed objects as each page arrives

**Takeaway:** Kubernetes objects carry large server bookkeeping in `metadata.managedFields` that clients rarely read. When you hold many objects in memory, drop it per page at ingestion.

<details>
<summary>The full story (20/N, f80c79b0)</summary>

- **Situation:** Attach keeps every listed claim dict until it builds Members. Each claim carries `managedFields`, the server-side-apply record of which client set which field. Nothing in the SDK reads it (checked with `git grep`).
- **What goes wrong:** `managedFields` was 50.6% of claim bytes (measured, C1-pr8-impl.md).
- **What we do:** `k8s_helper.py: K8sHelper.list_sandbox_claim_objects` (and the async twin) pops `managedFields` from each item on each page before extending the result list. Measured at N=5,000: list peak 48.7 MB to 24.1 MB, and the retained result 46.4 MB to 18.9 MB. This changes what the helper returns, so it needed the owner's OK.
- **Example:**
  ```python
  for item in page.get("items", []):
      (item.get("metadata") or {}).pop("managedFields", None)
  ```
- **Related:** [PERF-4](#perf-4).

</details>

<a id="perf-7"></a>

## PERF-7 Don't parse a bulk-mutation response you never read

**Takeaway:** Bulk endpoints often echo the whole affected set back. If you don't use it, drain it unparsed. Before you optimize, measure which response actually sets the memory peak.

<details>
<summary>The full story (20/N, e6dde9ff)</summary>

- **Situation:** deletecollection returns the full list of every object it deleted, and no caller reads it.
- **What goes wrong:** The client read and decoded that list only to throw it away. That was the 69.5 MB peak of `release()` at 5,000 claims (e6dde9ff). pr8_plan.md tied the peak to the deletecollection body, not the re-list: the re-list took 2 to 8 ms against 430 to 510 ms for a full list.
- **What we do:** `k8s_helper.py: K8sHelper.delete_sandbox_claims_by_label` and `async_k8s_helper.py: delete_sandbox_claims_by_label` ask for the response unread (`_preload_content=False`) and drain it without parsing. The sync helper drains raw chunks with `stream(decode_content=False)` so nothing is decompressed, then releases the connection. The async helper checks the status itself, drains `response.content.iter_any()` (aiohttp's session may still decompress), and calls `response.release()`. Pinned by `test_delete_sandbox_claims_by_label_drains_the_response_unparsed_and_releases_it`.
- **Related:** [LIB-13](lib.md#lib-13) (closing an unfinished response before `release_conn()`) and [LIB-14](lib.md#lib-14) (checking status yourself on the async client) cover the socket-safety side.

</details>

<a id="perf-8"></a>

## PERF-8 Renew by conditional write from your last copy, and read only on conflict

**Takeaway:** For an object only you should be writing, keep the copy from your last successful write and send a conditional write directly. Fetch it only when the precondition fails, to tell "my own lost write landed" apart from "someone else wrote".

<details>
<summary>The full story (3/N, 31a47728)</summary>

- **Situation:** The handle renews its Lease (a small coordination.k8s.io object that marks who holds the batch) every lease_duration/3. Every renewal used to GET the Lease and then PUT it.
- **What goes wrong:** Each renewal cost two round trips. That doubles Lease traffic and adds latency inside a window that a deadline bounds.
- **What we do:** `sandbox_batch.py: SandboxBatch._renew_once` / `_write_renewal` (and the async shell) PUT the Lease from `self._lease`, the object from the last successful write, which carries its resourceVersion. Optimistic concurrency (the server rejects a write whose resourceVersion is out of date, with a 409) catches any change. Only a 409 triggers a GET. If this handle is still the holder (its own earlier renewal landed but the response was lost), it writes once more on the fresh object. Otherwise it stops. `self._lease` is updated only on success.
- **Example:**
  ```text
  t0 PUT(self._lease, rv=11) -> 409
  t1 GET -> rv=12, holder=me   # my own late write landed
  t2 PUT(rv=12) -> ok
  ```
  Pinned by `test_renewals_replace_the_last_written_lease_without_reading_it` and `test_conflict_reads_the_lease_and_renews_it_while_still_held`.
- **Related:** [LEASE-28](lease.md#lease-28) covers how a renewal 409 is classified. [LEASE-2](lease.md#lease-2) covers why Lease writes use PUT. See also [PAT-33](../patterns.md#pat-33).

</details>

<a id="perf-9"></a>

## PERF-9 Log per item at DEBUG on bulk paths, through a log-level parameter

**Takeaway:** When you reuse a single-item helper in bulk, make its per-item log level a keyword parameter with a backward-compatible default. Log per item at DEBUG and keep INFO for aggregate state changes.

<details>
<summary>The full story (12/N, 522dad4c; 15/N, 113fae28)</summary>

- **Situation:** `create_sandbox_claim` and `delete_sandbox_claim` logged each call at INFO, which suits single-sandbox use.
- **What goes wrong:** A batch of thousands of claims would flood INFO with one line per create and per delete, and pay to emit each line.
- **What we do:** `k8s_helper.py: K8sHelper.create_sandbox_claim(log_level=...)` and `delete_sandbox_claim(log_level=...)` (and the async twins) take a keyword-only `log_level` that defaults to INFO, so existing callers don't change. Batch creates (`sandbox_batch.py: SandboxBatch._send_create`) and deletes pass `logging.DEBUG`. Batch-level events, like the first 429 window or clamping max_in_flight, stay at INFO. Pinned by `test_claim_create_and_delete_log_at_the_given_level`.
- **Example:**
  ```python
  self._helper.create_sandbox_claim(..., log_level=logging.DEBUG)
  ```
- **See also:** [BP-34](../best-practices.md#bp-34).

</details>

<a id="perf-10"></a>

## PERF-10 Validate before the first write, dedupe repeated lookups, and reuse reads you already make

**Takeaway:** Run every check before the first mutating call, so a failed check leaves nothing to roll back. Cache lookups that repeat across inputs, and pull every field you need out of reads you already make instead of adding requests.

<details>
<summary>The full story (12/N, 522dad4c; 19/N, 99a4a93d)</summary>

- **Situation:** `claim_batch` must fail fast on a missing warm pool or template and must not leave a Lease behind. A warm pool (SandboxWarmPool) keeps pre-started sandboxes, and its `status.readyReplicas` says how many are Ready now.
- **What goes wrong:** Checking after creating the Lease would need a rollback. One template GET per pool repeats work when pools share a template. A separate readyReplicas read would add a GET per pool.
- **What we do:** `sandbox_batch.py: _precheck_pools` and `async_sandbox_batch.py: _precheck_pools` run before `create_batch_lease`. They raise `SandboxWarmPoolNotFoundError` or `SandboxTemplateNotFoundError`, including when a pool names no template. A `checked_templates` set skips repeated templates. The same pool GET supplies `status.readyReplicas`, parsed defensively by `batch_utils.py: warmpool_ready_replicas` (an int > 0, else 0) and recorded as `WarmCredit`. Pinned by `test_precheck_failure_writes_nothing`.
- **Related:** [PERF-11](#perf-11), [PERF-13](#perf-13), [API-3](api.md#api-3).

</details>

<a id="perf-11"></a>

## PERF-11 Seed an existence cache from evidence you already have, and check health before any I/O

**Takeaway:** Cache existence checks only on success, and seed the cache from proof you already hold (earlier checks, objects you can see). Check the handle's health before any request, so a stopped handle fails fast with the real cause and sends nothing.

<details>
<summary>The full story (16/N, dc9ea439, 810d3773)</summary>

- **Situation:** `acquire()` can target any pool in the namespace. Checking a pool costs a WarmPool GET plus a Template GET.
- **What goes wrong:** Re-checking pools that `claim_batch` already checked, or that existing claims prove exist, adds GETs to every call. A detached or failed handle that still sent GETs would do useless I/O and could hide the lifecycle error.
- **What we do:** `sandbox_batch.py: SandboxBatch.__init__` sets `_checked_pools = {g.warmpool for g in state.groups}`. `claim_batch` prechecked those pools, and a re-attached handle rebuilds groups from claims that exist. Only a successful check adds a pool. `_check_pool` calls `self._check_healthy()` first (16/N review item 6), so after `err()`, `detach()`, or `release()`, `acquire()` raises `BatchError` (chained from `err()` when it is set) without touching the API. One cost remains: a re-attached handle records no warm credit at attach, so its first `acquire()` on a busy pacer does one refresh.
- **Example:** One test asserts `get_sandbox_warmpool` is never called once err is set. Another asserts a pool outside the fill is checked once.
- **Related:** [PERF-10](#perf-10), [PERF-13](#perf-13). [LOCK-15](lock.md#lock-15) covers "cache only successes" for the shared single-flight check.

</details>

<a id="perf-12"></a>

## PERF-12 Make a cache-seeded lookup single-use

**Takeaway:** When you prefill a lookup from data you already hold, use the prefill once. After that, the normal re-resolution path takes over, because the hint may now be stale.

<details>
<summary>The full story (17/N, 6feedc5c)</summary>

- **Situation:** The batch watch already knows each Ready member's pod IP. The connector's `get_pod_ip` callback did a Sandbox GET anyway, which cost one extra API call per member at fan-out (17/N commit message).
- **What goes wrong:** A seed that lived forever would keep routing to a dead IP after the connector drops it following a failed request.
- **What we do:** `sandbox_batch.py: connect` sets `sandbox._seeded_pod_ip`. The connector's callback is now `sandbox.py: Sandbox._resolve_pod_ip` (and `async_sandbox.py: AsyncSandbox._resolve_pod_ip`), which swaps the seed out and returns it once. The connector asks again only after a failed request, and that lookup reads the Sandbox as before.
- **Example:**
  ```python
  seeded, self._seeded_pod_ip = self._seeded_pod_ip, None
  ```
  ```text
  t0 first request: _resolve_pod_ip -> 10.0.0.1 (no GET)
  t1 request fails, connector invalidates
  t2 next lookup: GET sandbox -> 10.0.0.2
  ```
  Pinned by `test_seeded_pod_ip_is_used_once_then_read_from_the_sandbox` and `test_builds_sandbox_class_without_resolve_or_get_sandbox`.
- **Related:** [WATCH-8](watch.md#watch-8) covers when a cached connection stays valid (same sandbox name and pod IP).

</details>

<a id="perf-13"></a>

## PERF-13 Let requests skip a rate limiter when they won't trigger the work it protects

**Takeaway:** If a capacity signal shows a request won't cause the expensive work a rate limiter protects, let it bypass the limiter, and don't charge it against the limiter's cursor. And never pay an extra request to bypass a limiter whose next slot is already free.

<details>
<summary>The full story (19/N, 2ecdf384)</summary>

- **Situation:** `acquire()` first used the fill's create_rps pacing even on warm pools. Pacing exists so cold claims don't start many pods on one node at once. A claim that a Ready warm sandbox takes starts no pod.
- **What goes wrong:** Commit 2ecdf384 measured rolling acquire p99 on kind at 749 ms (default 50/s) against 560 ms for the per-claim path. The gap came from waiting on pacing slots it didn't need.
- **What we do:** In `sandbox_batch.py: SandboxBatch.acquire` (and the async shell), a covered acquire uses `slot = now if covered else self._reserve_create_slot(now)`. It never moves `_next_create_at`, so paced callers aren't pushed out. An uncovered acquire reserves a paced slot and still calls `WarmCredit.spend()`, so cached credit stays accurate. Credit comes from the first-use check's readyReplicas ([PERF-10](#perf-10)). Creates are still bounded by max_in_flight. When credit hits 0, `acquire()` can re-read the WarmPool, but only if the pacer would actually delay the create: `need_refresh` requires `not covered and not refreshed and now < deadline and _next_create_at > now and refresh_due(pool)`, evaluated under `_lock` together with `take()`. With an open slot, the GET saves 0 ms while doubling API calls (2.01 req/env on 500 pools, per [RETRY-17](retry.md#retry-17)).
- **Example:**
  ```text
  readyReplicas=2, create_rps=2, 4 concurrent acquires
  acquires 1-2: covered, start at once
  acquire 3: first paced slot, starts at once (<0.15 s)
  acquire 4: next paced slot, starts at >=0.5 s
  ```
  Pinned by `test_acquires_the_warm_pool_covers_skip_pacing_and_reread_its_count_once_a_second`.
- **Related:** [RETRY-16](retry.md#retry-16) (temporary 429 cooldown) and [RETRY-17](retry.md#retry-17) (refresh skip, discovery GETs under max_in_flight). [RETRY-15](retry.md#retry-15) covers warm credit on the fill path.

</details>

<a id="perf-14"></a>

## PERF-14 Send each consumer's minimum set first through a fixed-rate pipe

**Takeaway:** Keep an item's identity (stable, contiguous IDs) separate from its send order. Through a fixed-rate pipe, send each consumer's minimum useful set first and finish consumers one at a time. The first useful result arrives much sooner, and the total time stays the same.

<details>
<summary>The full story (12/N, 522dad4c)</summary>

- **Situation:** N claims across G groups go out through one paced queue. Quorum needs min_ready claims per group, and streaming consumers start on whole groups.
- **What goes wrong:** Round-robin order (sort by k/size) finished every group in the last quarter of the fill. refs/benchmark_rerun_review.md measured the RL first group at 0.65 s against 0.16 s for the per-claim baseline: group 0's last claim was create 121 of 160. Ordinals must also stay contiguous per group, so a re-attached handle can rebuild group order.
- **What we do:** `batch_utils.py: fill_create_plan` gives each group a contiguous ordinal block (`accumulate` of sizes) and sorts create slots by `(k >= min_ready, index, k)`. All min_ready slots go first, then the rest, and inside each part groups go one after another in caller order. Total fill time doesn't change, because the same creates go out at the same pace, but the first groups complete early. Rejected: round-robin, and shortest-group-first (superseded in the notes).
- **Example:**
  ```text
  groups A(size 6, min 3) -> ordinals 0-5, B(size 3, min 1) -> ordinals 6-8
  create order: b1-0, b1-1, b1-2, b1-6, b1-3, b1-4, b1-5, b1-7, b1-8
  ```
  Pinned by `test_creates_each_groups_min_ready_claims_first_in_group_order`.
- **Related:** [K8S-18](k8s.md#k8s-18) covers rebuilding group order from the lowest surviving ordinal. See also [PAT-29](../patterns.md#pat-29).

</details>

<a id="perf-15"></a>

## PERF-15 Measure every wait from one shared start and sort by deadline

**Takeaway:** When many items each need a minimum age before you act, measure every deadline from one shared start time, sort by deadline, and sleep only the remaining gap before each. The run then ends at the latest deadline, not the sum of the waits.

<details>
<summary>The full story (18/N, a97eeae7)</summary>

- **Situation:** The reaper (a CronJob that deletes Leases whose holder died) must see a stale Lease unchanged for one renew interval before deleting it. Stale Leases in one run can have very different durations: detached Leases carry the detach grace, and others use their lease duration (default 60 s).
- **What goes wrong:** reaper_contract.md first proposed one shared wait equal to the longest interval, which delays short Leases behind long ones. Waiting per Lease from that Lease's own discovery would instead make the run grow with the number of namespaces.
- **What we do:** `batch_reaper.py: reap` takes `listed_at = time.monotonic()` once, after every namespace is listed. It sorts candidates by `renew_interval` (`max(1, lease_duration_seconds // 3)`) and, before each delete, sleeps `max(0, listed_at + interval - time.monotonic())`. A run lasts as long as the longest interval, however many namespaces or Leases it covers, and a short Lease never waits behind a long one.
- **Example:**
  ```text
  t0  list all namespaces, listed_at = t0
  t20 sleep 20 -> delete Lease A (interval 20 s)
  t30 sleep 10 -> delete Lease B (interval 30 s)
  total 30 s, not 50 s
  ```
  Pinned by `test_waits_each_leases_renew_interval_then_deletes_only_the_leases`.
- **Related:** [LEASE-9](lease.md#lease-9) covers why the reaper's renew-interval formula must match the driver's cadence. See also [LEASE-8](lease.md#lease-8).

</details>

<a id="perf-16"></a>

## PERF-16 Size a shared connection pool for the burst plus every long-lived and liveness-critical connection

**Takeaway:** A connection pool must fit your burst concurrency plus every connection that stays open or keeps you alive. Check what your HTTP library does when the pool is full: a blocking pool (aiohttp) can starve a heartbeat until it expires, and an overflowing pool (urllib3) quietly pays a fresh handshake per request.

<details>
<summary>The full story (17/N, 6feedc5c)</summary>

- **Situation:** A batch runs up to max_in_flight creates, deletes, and pool reads at once. It also holds one streaming watch connection for its whole life and renews its Lease periodically.
- **What goes wrong:** On async, the Lease client shares the one aiohttp connector (`async_k8s_helper.py: _ensure_initialized` builds `coordination_v1_api` on `self._api_client`). A full pool makes a renewal wait for a free connection, and a long enough wait lets the Lease expire. On sync, a full urllib3 pool doesn't block. It opens one-off connections that close after one request, so each pays a fresh TLS handshake.
- **What we do:** `batch_utils.py: BATCH_POOL_HEADROOM = 2`, one slot for the watch and one for Lease renewals. `check_pool_size` requires `pool_size >= max_in_flight + 2`, and an unbounded pool (`None`) skips the check. A default max_in_flight on a smaller pool of at least 3 is clamped to `pool_size - 2` with an INFO log. An explicit max_in_flight that doesn't fit, or a pool with no create slot left, raises `ValueError`. The sync Lease client is a separate ApiClient with its own pool, because it needs `retries=False` (`k8s_helper.py: K8sHelper.__init__`), so sync strictly needs only the watch slot and the second is spare. The same invariant later bounded on-demand GETs in 19/N ([RETRY-17](retry.md#retry-17)). Pinned by `test_pool_must_hold_max_in_flight_plus_the_watch_and_renewal`.
- **Example:**
  ```text
  pool 100 (kubernetes_asyncio default), max_in_flight 20: 20 creates + 1 watch + 1 renewal fit
  pool 21, max_in_flight 20 (if unchecked): the renewal queues behind creates
  ```
- **Related:** [PERF-17](#perf-17), [PERF-18](#perf-18). [RETRY-19](retry.md#retry-19) and [RETRY-17](retry.md#retry-17) cite the invariant and the clamp.

</details>

<a id="perf-17"></a>

## PERF-17 Don't reserve peak burst capacity as a lifetime reservation

**Takeaway:** Admission checks that hold each component's peak burst for its whole lifetime reject workloads that share capacity in time. Validate what one component can see and control, and document how to size for combined load.

<details>
<summary>The full story (17/N, 6feedc5c)</summary>

- **Situation:** A CodeRabbit reviewer proposed subtracting max_in_flight + 2 for every batch already in `_active_batches` before admitting a new one (handoff/work/replies_2153.md, thread 1).
- **What goes wrong:** Bursts are brief. After the fill, a batch holds 1 watch connection and borrows 1 for a few ms per renewal. Permanent reservations would lock 88 slots for 4 idle batches on a default 100-slot AsyncSandboxClient, clamp a 5th batch to 10, and reject a 6th. `claim_batch` also can't see all traffic: get_batch handles, create_sandbox watches, and shared injected api_clients all use the same pool.
- **What we do:** `batch_utils.py: check_pool_size` checks only that one batch is consistent with the pool, like the Go SDK's Burst >= max_in_flight check. The README tells callers who run several batches on one client to pass `pool_size` (or set `connection_pool_maxsize` on an injected `api_client`) sized for their combined load.
- **Example:**
  ```text
  rejected rule, pool 100: batches 1-4 reserve 22 each = 88
  batch 5: 12 left -> clamped to max_in_flight 10
  batch 6: nothing left -> rejected
  ```
- **Related:** [PERF-16](#perf-16), [RETRY-19](retry.md#retry-19).

</details>

<a id="perf-18"></a>

## PERF-18 Enforce a capacity invariant on every path that builds the object

**Takeaway:** When a capacity check runs in only one constructor path, audit every other path that builds the same object (attach, restore, clone), or move the check into the object's own constructor.

<details>
<summary>The full story (17/N, 6feedc5c; 15/N, 16/N)</summary>

- **Situation:** Only `claim_batch` calls `check_pool_size`. Since 15/N and 16/N, a handle from `get_batch` also runs member deletes and `acquire()` creates and pool reads, all under `BoundedSemaphore(max_in_flight)`.
- **What goes wrong:** `sandbox_batch.py: SandboxBatch._attach` builds the handle without passing max_in_flight, so it takes `BATCH_DEFAULT_MAX_IN_FLIGHT = 20` and never consults `api_pool_size()`. The `release_not_ready` docstring notes the limit "defaults to 20 on a handle from `get_batch`". On a client built with pool_size < 22, a re-attached handle can still run 20 requests at once. On async that can queue the Lease renewal behind them (the [PERF-16](#perf-16) hazard). On sync it opens throwaway connections.
- **What we do:** Not handled at 23/N. This gap is inferred from the code, not from a failing test.
- **Example:**
  ```python
  SandboxClient(pool_size=5).get_batch(batch_id).release_not_ready()
  # up to 20 concurrent deletes on a 5-slot pool
  ```
- **Related:** [PERF-16](#perf-16), [PROC-11](../process.md#proc-11).

</details>

<a id="perf-19"></a>

## PERF-19 Wire shared client TLS state and optional config through secondary handle factories

**Takeaway:** Whenever a secondary path constructs child handles directly instead of calling the client's main factory, forward every shared client resource (such as a cached `SSLContext` or transfer config). Pass newly added optional kwargs only when non-`None` so custom handle subclasses keep working.

<details>
<summary>The full story (24/N, 93e8cae8)</summary>

- **Situation:** `AsyncSandboxClient` holds a `_DefaultSSLContext` callable that lazily builds and caches one `ssl.SSLContext` on first HTTPS use (`9f7999b3`) and wires it onto standalone handles in `_attach_handle`, and both `SandboxClient` and `AsyncSandboxClient` accept `file_config` for `create_sandbox` and `get_sandbox`. `SandboxBatch.connect` and `AsyncSandboxBatch.connect` build handles directly with `self._client.sandbox_class(...)` so batch handles are not registered in `_active_connection_sandboxes` ([CLEAN-8](clean.md#clean-8)).
- **What goes wrong:** Because `connect()` bypassed `_attach_handle`, `AsyncSandboxBatch.connect` left `sandbox.connector._default_ssl_context` at its uncached `httpx.create_ssl_context` default. Connecting `N` members over HTTPS ran `ssl.create_default_context()` `N` times, parsing the CA bundle once per member instead of once per client. Both shells also dropped `self._client.file_config` (such as `max_download_size`). Forwarding `file_config=None` unconditionally, meanwhile, raises `TypeError` on caller-supplied `sandbox_class` subclasses that predate `file_config`.
- **What we do:** `async_sandbox_batch.py: AsyncSandboxBatch.connect` sets `sandbox.connector._default_ssl_context = self._client._default_ssl_context`, and both `SandboxBatch.connect` and `AsyncSandboxBatch.connect` pass `**({"file_config": self._client.file_config} if self._client.file_config is not None else {})`. Pinned by `test_connected_handles_share_client_default_ssl_context` (wraps `ssl.create_default_context` and asserts one call across two connected members) and `test_forwards_client_file_config_when_set` (both shells).
- **Example:**
  ```python
  sandbox = self._client.sandbox_class(
      ...,
      **({"file_config": self._client.file_config} if self._client.file_config is not None else {}),
  )
  sandbox.connector._default_ssl_context = self._client._default_ssl_context
  ```
- **Related:** [CLEAN-8](clean.md#clean-8), [PERF-18](#perf-18), [GIT-12](../process.md#git-12).

</details>

<a id="perf-20"></a>

## PERF-20 Open an on-demand batch with one size-0 seed pool so later pools validate in parallel

**Takeaway:** If a batch constructor validates every declared group sequentially up front, open a long-lived dynamic batch with a single `size=0` group from the first caller's pool and let later pools validate lazily on their own first `acquire()`.

<details>
<summary>The full story (25/N, f81e7e25)</summary>

- **Situation:** `examples/agent-sandbox-rl/agent_sandbox_rl/batch_claims.py: BatchClaims._batch_for` opens one `SandboxBatch` per cluster for `FleetConfig(claim_mode="batch")`. A single evaluation or RL run can span hundreds of distinct per-task warm pools (`500` pools in the SWE-bench `500 x 1` shape).
- **What goes wrong:** `claim_batch` runs `_precheck_pools` across all declared groups one after another before returning the handle ([PERF-11](#perf-11)). Passing all `P` task pools up front as `size=0` groups puts up to `2 * P` sequential GETs (`1,000` serial GETs for `500` pools) onto the first caller's thread before any claim is created.
- **What we do:** `_batch_for` opens the batch under a per-cluster lock (`_cluster_lock`) with `[BatchGroup(warmpool=pool, size=0)]` for only the first `acquire()`'s pool (at most 2 GETs). Every other pool is validated once on the thread of its own first `SandboxBatch.acquire()`, coalesced per pool via `_prechecks` ([LOCK-15](lock.md#lock-15)) and bounded across pools by `max_in_flight` ([RETRY-17](retry.md#retry-17)). Pinned by `test_batch_claims.py: test_first_acquire_opens_one_batch_from_its_pool` and `test_concurrent_first_acquires_open_one_batch`.
- **Example:**
  ```text
  up-front 500 pools -> 1,000 serial GETs on thread 1 before any POST
  1 size-0 seed pool -> 2 GETs on thread 1; pools 2..500 check in parallel under max_in_flight
  ```
- **Related:** [PERF-11](#perf-11), [LOCK-15](lock.md#lock-15), [RETRY-17](retry.md#retry-17).

</details>

<a id="perf-21"></a>

## PERF-21 Bounding client write concurrency speeds up end-to-end readiness by leaving server seats for the controller

**Takeaway:** When a client creates declarative resources and waits for an in-cluster controller to reconcile them Ready, bounding in-flight client creates finishes faster than an unbounded burst. Flooding apiserver concurrency seats queues the controller's own reconciliation writes and delays readiness.

<details>
<summary>The full story (19/N, 25/N; GKE benchmark 8f9ef46d)</summary>

- **Situation:** `SandboxBatch` caps concurrent creates, deletes, and on-demand pool GETs with `BoundedSemaphore(max_in_flight)` (`default=20`, configurable via `BatchClaimConfig.max_in_flight` in `25/N`). On GKE (`BENCHMARK_TABLES.md`, commit `8f9ef46d`), we benchmarked `batch (default)` (`max_in_flight=20`) against `batch (unbounded)` (`max_in_flight=998`, `create_rps=1000000.0` on a `1,000`-socket pool) and `per_claim` across 160 to 2,560 warm sandboxes.
- **What goes wrong:** Every `SandboxClaim` POST still needs the controller to adopt a warm `Sandbox` and patch the claim's Ready status through the same kube-apiserver. Firing 640 unbounded claim POSTs at once saturates APF concurrency seats and queues or throttles the controller's adoption writes behind the client's own burst (even with zero client HTTP 429s, apiserver APF rejected 19 controller `PATCH` and `PUT` writes in run 1), inflating tail readiness.
- **What we do:** Keep `BATCH_DEFAULT_MAX_IN_FLIGHT = 20` as the SDK and `BatchClaimConfig` default, and expose `max_in_flight` for cluster-specific tuning. At 640 warm sandboxes (`160 x 4`) on GKE, `batch (default)` (`max_in_flight=20`) reached all-Ready in `8.73 s` (`p99 = 8.50 s`, zero client and server 429s, `2.24x` faster than `per_claim`'s `19.54 s`), whereas `batch (unbounded)` (`max_in_flight=998`) regressed to `18.95 s` (`p99 = 18.65 s`).
- **Example:**
  ```text
  GKE 640 warm sandboxes (160 x 4):
    per_claim                 -> all Ready 19.54 s (p99 19.30 s, 640 watches)
    batch (max_in_flight=998) -> all Ready 18.95 s (p99 18.65 s, controller queued)
    batch (max_in_flight=20)  -> all Ready  8.73 s (p99  8.50 s, 1 watch, 0 429s)
  ```
- **Related:** [PERF-16](#perf-16), [RETRY-17](retry.md#retry-17), [BP-27](../best-practices.md#bp-27).

</details>

<a id="perf-22"></a>

## PERF-22 Drive cold-start fail-fast watchdogs from in-memory batch state so warm adoptions cost zero GETs

**Takeaway:** When a shared watch already tracks which batch members are `PENDING` vs. `READY`, inspect pods only for members still `PENDING` after `initial_delay_s`, and fence `release_member` so the watchdog never kills a member that became `READY` or was handed out while the pod `GET` was in flight.

<details>
<summary>The full story (WS-B: 05b0a63b, 4018f0dd)</summary>

- **Situation:** In `per_claim` mode, `agent-sandbox-rl` spawned a `ClaimWatchdog` per claim to detect `ImagePullBackOff` or broken startup states early instead of waiting out the full claim timeout. In `claim_mode="batch"`, `SandboxBatch.acquire()` allocates claim names internally, so `ClaimWatchdog` could not wrap individual claims.
- **What goes wrong:** Polling every batch claim's pod on creation would re-introduce `N` Pod `GET`s on the warm-pool fast path (where claims become `READY` in `~100-400 ms`), while releasing a member from a background watchdog thread without re-checking its live state can kill a sandbox that turned `READY` and was handed to a rollout thread while `_check_member()`'s Pod `GET` was in flight!
- **What we do:** `failfast.py: BatchWatchdog` (`05b0a63b`) runs one daemon thread per `SandboxBatch` scanning `batch.members()` in memory: warm adoptions that turn `READY` before `initial_delay_s` incur **zero Kubernetes `GET`s**. Only members still `PENDING` past `initial_delay_s` get a `classify_pod()` check; if terminal (`StartVerdict`), `BatchWatchdog` re-verifies the member has neither transitioned to `READY` nor been handed out before calling `release_member` (`4018f0dd`), waking `acquire()` immediately with `SandboxStartError` (and matching stopped-batch errors via `extract_batch_claim_name`). Tests: `test_batch_claims.py`, `test_fail_fast.py`.
- **Related:** [WATCH-7](watch.md#watch-7), [WATCH-10](watch.md#watch-10), [RACE-1](race.md#race-1).

</details>

<a id="perf-23"></a>

## PERF-23 Memoize on-demand pool setup per `(cluster, image)` and size both HTTP and executor pools above `max_concurrent`

**Takeaway:** Once an on-demand `(cluster, image)` warm pool is created, skip `ensure_template` and `create_warmpool` RPCs on later acquires for the same pair. Size both `urllib3` `connection_pool_maxsize` and `AsyncSandboxFleet`'s worker thread ceiling above `max_concurrent` (plus prefetch headroom) so Python never queues `1,000`-sandbox bursts before they reach `SandboxBatch`.

<details>
<summary>The full story (WS-B: 6d773276, e7ed074b, 63637038)</summary>

- **Situation:** At `max_concurrent >= 1,000` (such as `Bench 2A` `c=1,000` warm bursts and `Bench 1B` `c=1,024` Tunix GRPO), three client-side ceilings inside `agent-sandbox-rl` sat directly in front of `SandboxBatch`:
  1. Dynamic on-demand `acquire()` called `_ensure_pool()` (`ensure_template` + `create_warmpool` = `2` K8s RPCs) on *every* acquire even when `(cluster, image)` was already present in `self._ondemand`.
  2. `Cluster` defaulted to `connection_pool_maxsize = 1000`, which failed `SandboxBatch.claim_batch()`'s `pool_size >= max_in_flight + 2` check ([PERF-16](#perf-16)) whenever `max_in_flight >= 999`.
  3. `AsyncSandboxFleet._thread_pool()` capped its `ThreadPoolExecutor` at `1024` workers, queuing blocking `acquire`, `exec`, and `release` calls inside Python whenever `max_concurrent >= 1000` with pipelined prefetch windows (`mc + 2 * win`).
- **What we do:** Guard initial on-demand pool creation per `(cluster, image)` so subsequent acquires reuse the cached pool name in memory with **zero** extra RPCs (`6d773276`); auto-size `Cluster` `connection_pool_maxsize` (including caller-supplied `Cluster` objects) to `max(1000, max_concurrent + 32, (max_in_flight or 20) + 16)` (`6d773276`); and raise `AsyncSandboxFleet._thread_pool()`'s ceiling to `min(4096, max(64, mc + 64, mc + 2 * win + 16))` (`e7ed074b`). In addition, `SandboxHandle._raise_if_lost()` checks `handle.claim_expires_at` (`63637038`) so rollouts exceeding `BatchClaimConfig.work_budget` surface an actionable `work_budget` hint instead of a generic pod-lost error.
- **Related:** [PERF-10](#perf-10), [PERF-16](#perf-16), [PERF-20](#perf-20), [LEASE-32](lease.md#lease-32).

</details>

<a id="perf-24"></a>

## PERF-24 Look up one member by name, keep per-pool and membership indexes, and never scan or copy a whole collection on a per-event or per-item path

**Takeaway:** On a path that runs once per watch event, RL step, release, or closed connection, every operation must cost O(1) in the batch size: `get_member(name)` instead of filtering `members(pool)`, an index keyed by warmpool instead of scanning `_members`, and an insertion-ordered `dict` instead of `list` membership plus `remove`. A full snapshot (`members()` copies, filters, and sorts under the batch lock) is for callers that want the whole batch, not one entry.

<details>
<summary>The full story (Pipeline 8: F-30..F-41 at e84e964c and bb443e71)</summary>

- **Situation:** `BatchState` keeps its per-event transitions O(1) ([PERF-1](#perf-1)), and Pipeline 8 confirmed that by instruction count: `upsert_claim` costs `475` instructions at both `N=100` and `N=5,000`, and one watch event through `SandboxBatch._watch_loop` costs `3,358` vs `3,364` at `N=100` vs `2,000`. The layers around it did not keep that discipline.
- **What goes wrong:**
  1. `BatchClaim.still_reaches()` (called by `SandboxHandle.is_alive()` at every RL step boundary) found its member with `next(m for m in batch.members(pool) if m.claim_name == name)`. That costs `4,399` instructions at `N=50` and `146,761` at `N=2,000` (`33.4x`), all under `batch._lock`, which the watch thread needs for every event (`F-30`). On `26/N`, `BatchWatchdog._poll_once` sorts the whole batch on every idle poll, up to `50` times per second during acquire bursts (`38.9x`).
  2. `has_pool`, `members(pool)`, and `release_group` walk every member and group (`44.9x`, `44.6x`, `39.7x`), so releasing all `G` pools of a multi-image batch costs `O(G * N)` (`F-31`).
  3. `list` membership plus `remove` turns `S` closes behind a backlog of failed ones into `O(S^2)` comparisons (`F-32`), and `SandboxFleet.release` runs the generated 16-field dataclass `__eq__` on every handle ahead of the target, twice (`19.5x`, `F-33`).
  4. Tombstones that are never pruned (`BatchState._released`) grow by `156 B` per episode for the life of a dynamic batch (`F-35`), and the SDK client keeps every caller-named claim name in `_explicit_claims` forever (`F-41`, also on `origin/main`).
  5. Per-pool work that reaches the whole batch: `release_group()` walks the entire fill plan to drop one pool's creates (`F-36`) and wakes every waiting acquire rather than that pool's (`F-37`).
  6. Per-call work that grows with history or output: `SandboxFleet.add_tasks` re-sums every planned image on each streamed batch (`F-40`, `14.5x` at `3,000` images), and `SandboxSession.run` re-joins its whole output buffer per chunk (`F-39`, `10x` the output costs `107x` the time). A per-claim acquire also reads the Sandbox twice where one read returns both values (`F-38`).
- **What we do:** Add `get_member(name)` to both handles; keep `_members_by_pool` / `_acquiring_by_pool` next to `_members` / `_acquiring`, maintained through one insert helper and one pop helper; store `_stale_connections` as `dict[Sandbox, None]` and fleet handles as `dict[int, SandboxHandle]` keyed by `id(handle)`; index planned creates per pool and wake only the released pool's acquires; keep per-cluster task totals on `FleetPlan`; match a session's end marker against the tail plus the new chunk and join once. Each candidate fix turned its gate green at a ratio of `1.000`, with zero production test regressions. The only tests that changed were white-box assertions on the old container types ([TEST-35](../testing.md#test-35)).
- **Related:** [PERF-1](#perf-1), [PERF-22](#perf-22), [PERF-23](#perf-23), [TEST-35](../testing.md#test-35), [CHAOS-12](../chaos-verification-suite.md#chaos-12).

</details>

<a id="perf-25"></a>

## PERF-25 Fan out multi-pool prechecks in parallel while raising errors in input order, and reset waves with whole-batch `release_members()`

**Takeaway:** When validating $G$ pools before creating a Lease, run the pool and deduplicated template `GET`s concurrently under `max_in_flight`, then inspect outcomes in caller input order so error determinism stays identical to a sequential loop. When resetting all claims between waves on a reusable batch, run one batch-wide `deletecollection` sweep (`release_members()`) instead of $G$ per-pool sweeps or tearing down the batch.

<details>
<summary>The full story (25/N, #2252, 4bd5de49)</summary>

- **Situation:** `claim_batch()` validates every declared warm pool and its referenced `SandboxTemplate` before creating the batch Lease ([PERF-10](#perf-10)), and multi-step callers periodically wipe active claims across many pools before acquiring the next wave.
- **What goes wrong:** Sequential `_precheck_pools` across $G = 200$ pools pays up to $400$ serial network RTTs before sending a single create. Between waves, calling `release_group(pool)` in a loop sends $G$ separate `deletecollection` + `count_live` sweeps, whereas calling `release()` + `claim_batch()` destroys and recreates the Lease, restarts the watch stream, and discards `_checked_pools`.
- **What we do:** `sandbox_batch.py: _precheck_pools` and `async_sandbox_batch.py: _precheck_pools` deduplicate pool names in order (`list(dict.fromkeys(warmpools))`), share `checked_templates`, and fan out `check_pool` across `ThreadPoolExecutor(min(len(pools), max_concurrency))` on sync and `BoundedSemaphore(max_concurrency)` + `asyncio.gather(..., return_exceptions=True)` on async. Both shells then iterate `zip(pools, results)` and raise the first exception in caller input order. For wave resets, `SandboxBatch.release_members()` (`_release_pools(None)`) deletes every claim across all pools in one batch-wide label `deletecollection` sweep while keeping the batch handle, its Lease heartbeat, its watch stream, and `_checked_pools` open, fencing concurrent `acquire()` calls via `_release_sweeps` until the sweep finishes.
- **Related:** [PERF-10](#perf-10), [OPT-29](../optimizations.md#opt-29), [K8S-8](k8s.md#k8s-8).

</details>

<a id="perf-26"></a>

## PERF-26 Bypass slow Kubernetes cascading garbage collection (`~13 objects/s`) by grouping direct `Sandbox` and `Pod` `deletecollection` calls by `claim-uid`

**Takeaway:** Deleting an owner resource (`SandboxClaim`) frees node capacity only after Kubernetes's `GarbageCollectorController` cascades the delete to child `Sandbox` and `Pod` objects. On control planes where KCM GC is capped at `~40 req/s` (`3` requests/dependent = `13.3 objects/s`), warm-pool refills stall; grouping direct `Sandbox` and `Pod` `deletecollection` calls (`2` requests per `50` releases by `agents.x-k8s.io/claim-uid`) eliminates both the cluster stall and the `1.26 ms`/release Python GIL cost of per-object background deletes.

<details>
<summary>The full story (Campaign 5: 20583e9d, ff25d9c4, ec8dead1, df95d2cd, 046df8d5)</summary>

- **Situation:** On two `324`-seat GKE clusters, synchronous multi-step runs (`250x2x6` and R2E `500x2x8`) stalled waiting for `SandboxWarmPool` replicas to refill between steps (`1,004-1,027 s` on R2E `500x2x8`), whereas a `483`-seat fresh cluster ran in `239 s` (`1.00x`). Read-only `/metrics` forensics traced the stall to KCM `GarbageCollectorController` running flat at `~40 req/s` (`13.3 objects/s`) on the smaller control planes ([GKE-18](../gke-benchmarking.md#gke-18)).
- **What goes wrong:**
  1. Leaving `Sandbox` and `Pod` cleanup to `13.3 objects/s` cascading GC leaves hundreds of dead `Running` pods occupying node memory (`Insufficient memory`), starving warm-pool refills.
  2. Issuing `2` individual `DELETE` calls per release on worker threads (`20583e9d`, `ff25d9c4`) removed the cluster stall (`~4.5x` wall speedup), *but* burned `1.26 ms` of Python GIL per release, regressing eval `500x1` first-exec `avg` (`0.50 s -> 0.56-0.80 s`) and pipelined prefetch (`10-13 s -> 35-41 s`).
- **What we do:** Stamp `Member.claim_uid` (`ec8dead1`), batch releases over `100 ms` (up to `50` claims), probe whether `Sandbox`es carry `agents.x-k8s.io/claim-uid` (falling back to `3` candidate `GET`s + `1` label `LIST` with power-of-two backoff on fast-GC clusters, `046df8d5`, [CLEAN-30](clean.md#clean-30)), and submit `2` `deletecollection` calls (`sandboxes`, then `pods` with `grace_period_seconds=0`) in parallel to an unretried `ApiClient` pool bounded by `min(16, max_in_flight)` (`df95d2cd`). Self-disables on `403`. R2E `500x2x8` dropped from `1,015 s` to `207 s` (`4.9x`) while eval first-exec returned to `0.48-0.57 s`.
- **Related:** [OPT-31](../optimizations.md#opt-31), [CLEAN-30](clean.md#clean-30), [GKE-18](../gke-benchmarking.md#gke-18).

</details>

<a id="perf-27"></a>

## PERF-27 Run high-concurrency RL claims and WebSocket exec natively on `asyncio`, prewarm TLS pools, and disable unused `aiohttp` `.netrc`/proxy thread-pool lookups

**Takeaway:** At `1,000` concurrent rollouts, `run_in_executor` bridges and thread-per-exec `kubernetes.stream` clients spawn thousands of OS threads, and even pure `aiohttp` secretly dispatches per-request `.netrc` filesystem lookups to the thread pool unless unconfigured proxy/netrc checks are skipped. Moving claims and multiplexed WebSocket exec onto the event loop with prewarmed keep-alive TLS sockets collapses `~3,000` threads and `~1,050` sockets to `3` threads and `68` sockets.

<details>
<summary>The full story (Campaign 5: b386d881, 93f93fc9, feffbac2, 03390885)</summary>

- **Situation:** At `concurrency = 1,000`, `per_claim` held `~3,000` OS threads and `~1,050` sockets (`10.0 s` TTFI P50, `12.5 s` burst + first exec wall). Even after moving batch claims and WebSocket exec onto `asyncio`, `/proc/self` still showed `23` active threads and extra client CPU during `1,000`-claim bursts.
- **What goes wrong:**
  1. Wrapping sync claims and `kubernetes.stream` pod exec in `run_in_executor` allocates threads and per-thread `PoolManager`s proportional to rollout concurrency.
  2. Cold HTTPS pools pay `64` simultaneous TCP + TLS handshakes on the critical path of burst 0.
  3. `aiohttp` checks environment proxies and `~/.netrc` via `loop.run_in_executor()` on every request when enabled, waking `20` executor threads just to look up nonexistent files.
  4. Instantiating OpenAPI models on watch events and JSON-decoding unread claim create/delete bodies burns event-loop CPU.
- **What we do:** Run batch claims natively on `asyncio` (`AsyncBatchClaims`), multiplex pod exec over a shared event-loop WebSocket client (`handle.aexec()`), pass raw watch events through without re-serialization while draining create/delete bodies unparsed (`b386d881`), prewarm `64` keep-alive TLS connections per cluster before the first burst (`93f93fc9`), and skip `aiohttp` proxy/`.netrc` lookups whenever neither is configured in the process (`feffbac2`, `03390885`). Skipping `.netrc`/proxy lookups alone cut driver threads from `23` to `3`, cut client CPU by `10-20%`, and lifted `1,000`-claim burst rate from `191-211/s` to `~240/s`. Together, `c = 1,000` ran in `3` threads and `68` sockets with `2.9 s` TTFI P50 and `5.4 s` burst + first exec wall.
- **Related:** [OPT-32](../optimizations.md#opt-32), [OPT-33](../optimizations.md#opt-33), [OPT-34](../optimizations.md#opt-34), [OPT-35](../optimizations.md#opt-35), [CLEAN-12](clean.md#clean-12).

</details>

<a id="perf-28"></a>

## PERF-28 Replace $O(P)$ warm-pool `GET`s with one paginated `LIST`, stream per-task step releases while fencing `release_all` on in-flight acquires, and yield prefetch to foreground creates

**Takeaway:** On multi-hundred-pool RL workloads, fetch warm-pool readiness via one paginated namespace `LIST` rather than $P$ `GET`s, release each step task's claim immediately on completion (`0.02 s` tail instead of blocking `0.5 s` on end-of-step `release_members()`) while fencing whole-batch `release_all()` whenever an `acquire()` is in flight, and yield background pipelined prefetch for `250 ms` when foreground creates are active.

<details>
<summary>The full story (Campaign 5: 21971e63, 10e49a38, 69ec0353, bcb8b9cd, 733a1972)</summary>

- **Situation:** SWE-bench and R2E-Gym run across `500` distinct single-image `SandboxWarmPool`s under synchronous, sliding-window, and pipelined strategies (`RL_BATCH_FINAL_REPORT.md`).
- **What goes wrong:** Issuing `1` `GET` per pool during prechecks or warm-credit refreshes costs `500` RPCs; deferring step tail releases into an end-of-step `batch.release_members()` (`deletecollection` + consistent `count_live`) adds `0.43-0.62 s` of blocking wall time after the last task on every synchronous step, while calling `release_members()` inside `release_all()` when a sibling `acquire()` is in flight on the shared batch deletes the sibling's pending claim; and eager pipelined prefetch steals `WarmCredit` and controller reconciliation seats from the active window.
- **What we do:** Use one paginated namespace `LIST` for warm-pool prechecks and credit refreshes (`21971e63`); release each step task's claim in `finally` as soon as its work finishes (`733a1972`, cutting `250x2x6` multi-step `112.4-113.1 s -> 107.6 s`) while keeping bulk `release_members()` for idle `release_all()` / `teardown()` behind `_acquire_in_flight_locked()` (`bcb8b9cd`); seed warm-pool readiness at pipelined window start while yielding background prefetch up to `250 ms` behind foreground creates (`10e49a38`); and set RL defaults to `create_rps=500`, `max_in_flight=64` (`69ec0353`), confirmed optimal by `mif=64/128/256` and `rps=50/500/2000` sweeps showing controller and node pod-start (`~50 pods/s`) ceilings above `mif=64`.
- **Related:** [OPT-37](../optimizations.md#opt-37), [OPT-38](../optimizations.md#opt-38), [OPT-39](../optimizations.md#opt-39), [PERF-21](#perf-21).

</details>

<a id="perf-29"></a>

## PERF-29 Prune settled `<batch-id>-<ordinal>` tombstones into merged integer intervals (`_OrdinalRanges`) so long-lived dynamic batches retain $O(\text{holes})$ memory instead of $O(\text{episodes})$

**Takeaway:** A permanent string tombstone set (`BatchState._released`) on a dynamic `SandboxBatch` retains `87-99 B` per released claim (`~90 MB` per million RL episodes). Once a generated `<batch-id>-<ordinal>` claim's delete settles and the watch has seen it, fold its ordinal into sorted, adjacent-merging `[lo, hi]` integer intervals (`_OrdinalRanges`) so `_is_released()` stays permanent in `840 B` total across `200,000` releases.

<details>
<summary>The full story (Campaign 5: c14bbc90, batch_state.py)</summary>

- **Situation:** `BatchState` allocates monotonic ordinals (`<batch-id>-0`, `<batch-id>-1`, ...) and checks `_is_released(name)` on late watch events, duplicate `release_member` calls, create-failure rollbacks, and `pop_event` (`[WATCH-4](watch.md#watch-4)`). Pipeline 8 (`F-35`, [PERF-24](#perf-24)) and `tracemalloc` at `100k-300k` releases measured `87-99 B` leaked per episode in `_released`.
- **What goes wrong:** Dropping tombstones on `DELETED` watch events breaks when a `410 Gone` relist skips the `DELETED` event (leaking missed names forever) or if a late event arrives after pruning; keeping string keys forever grows memory linearly with total historical episodes.
- **What we do:** `batch_state.py: _OrdinalRanges` (`_released_settled`, `c14bbc90`) stores settled ordinals as sorted `[lo, hi]` ranges merged on insertion and queried via `bisect`. A claim stays by string in `_released` only while it has a pending delete (needed for pool lookup in `pending_deletes_in_pool`) or sits in `_released_unseen` (so a late create still triggers a delete), and returns to `_released` if a create retry queues a second delete; non-canonical names (such as zero-padded strings) stay by name. Because ordinals never reuse values, range count is bounded by `live_members + in_flight_acquires + unsettled_releases + 1`. Fuzz-tested (`300` seeds x `400` adds) against a plain `set`; `200,000` settled releases occupy `840 B`.
- **Related:** [OPT-41](../optimizations.md#opt-41), [PERF-24](#perf-24), [WATCH-4](watch.md#watch-4).

</details>

---

[Learnings home](../../learnings.md) · [Field guide index](../../learnings.md#sec-field-guide)
