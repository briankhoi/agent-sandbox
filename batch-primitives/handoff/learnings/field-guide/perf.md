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

---

[Learnings home](../../learnings.md) · [Field guide index](../../learnings.md#sec-field-guide)
