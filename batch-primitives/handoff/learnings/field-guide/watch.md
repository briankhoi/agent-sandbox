<a id="sec-watch"></a>

[Learnings home](../../learnings.md) · [Field guide index](../../learnings.md#sec-field-guide)

# Watching the cluster: list, watch, and the local cache

Every batch handle keeps a local copy of its SandboxClaims, and a background watch keeps that copy up to date. The watch is how the handle learns that a claim was created, became Ready, moved to a new pod, or vanished. The one big idea behind these entries: the local cache is a *view* of the cluster that can lag, skip, or replay events, so every decision must either come from a consistent anchor (a list's resourceVersion) or re-check the current state at the moment of action. Read the first three entries for the core list-then-watch pattern, then the rest for how the cache stays honest when local intent, late events, and cached connections disagree. Backoff, bookmarks, terminating claims, and watch error handling live in the retry, Kubernetes, and race sections; this section links to them where they meet.

<a id="watch-1"></a>

## WATCH-1 Build your own informer: seed from a list, watch from its resourceVersion, resume from the last one you saw

**Takeaway:** For a client-side cache, list once, start the watch at that list's resourceVersion, track the resourceVersion of every event you receive, and resume from it on reconnect. Re-list only when the server says your resourceVersion is gone.

<details>
<summary>The full story (1/N)</summary>

- **Situation:** The handle needs a live cache of its SandboxClaims. In Go, client-go gives you an *informer* (a helper that lists, watches, and keeps a local cache for you). `kubernetes_asyncio` offers no informer, and the Python `kubernetes` library only recently gained a generic sync `kubernetes.informer.SharedInformer` (present in 37.0.1, which the SDK does not use), so the SDK hand-rolls it. A *resourceVersion* (RV) is the apiserver's opaque version stamp; a watch started at RV `X` delivers every change after `X`.
- **What goes wrong:** If the watch starts at "now" or with no RV, it misses every change between the seed list and the watch. If the handle re-lists on every reconnect instead, each 30 s watch stream ends with a full list, which costs a lot at tens of thousands of claims.
- **What we do:** `SandboxBatch._attach` seeds `BatchState` from one label-scoped list (`k8s_helper.list_sandbox_claim_objects`, via `seed_from_claims`) and keeps that list's RV. `SandboxBatch._watch_loop` and `AsyncSandboxBatch._watch_loop` start at that RV (`rv = self._list_resource_version`). Every event carrying `metadata.resourceVersion` (ADDED, MODIFIED, DELETED, BOOKMARK) advances the local `rv`. A reconnect after a transport error, a 429 or 5xx, or a normal server timeout resumes from that `rv`. Only a 410 (Gone: the server no longer has your RV) triggers a re-list. Tests: `test_resumes_from_list_resource_version`, `test_disconnect_reconnects_from_last_resource_version` (both shells).
- **Example:**
  ```text
  t0 list            -> rv=5
  t1 watch(rv=5)     -> MODIFIED rv=77, then ProtocolError
  t2 watch(rv=77)    -> no re-list needed
  ```
- **Related:** [WATCH-2](#watch-2), [WATCH-3](#watch-3), [PAT-32](../patterns.md#pat-32), [BP-7](../best-practices.md#bp-7). [K8S-10](k8s.md#k8s-10) covers bookmarks keeping a quiet watch's RV fresh, [RETRY-3](retry.md#retry-3) covers short-watch backoff, and [RETRY-4](retry.md#retry-4) covers error classification. [K8S-9](k8s.md#k8s-9) covers the paged list that produces this RV.

</details>

<a id="watch-2"></a>

## WATCH-2 Anchor the watch before any producer writes

**Takeaway:** When you list and then watch, take the watch's starting RV from the same list that defines the initial state, and start the workers that write objects only after the watch is anchored there. Then no write can fall into a gap.

<details>
<summary>The full story (12/N)</summary>

- **Situation:** In `claim_batch`, background create workers make claims while the label watch fills the member cache.
- **What goes wrong:** If the watch started after the first creates, or from "now" instead of a known RV, it could miss the ADDED event for an early claim. That member would never appear in the cache, and the batch would wait for it forever.
- **What we do:** In `SandboxBatch._claim` (and the async twin), the list that proves there are no leftover claims returns `list_rv`, and the handle's watch starts from it. `handle._start()` (watch plus renewal) runs before any `_create_worker` thread or task is spawned, so every create commits after the watch's starting point.
- **Example:**
  ```text
  t0 LIST              -> [] rv=7
  t1 watch from rv=7
  t2 workers start     -> each create gets rv > 7, so the watch delivers it
  ```
- **Related:** [WATCH-1](#watch-1). [LEASE-1](lease.md#lease-1) explains why the Lease is created before this list and before any claim.

</details>

<a id="watch-3"></a>

## WATCH-3 After a 410, reconcile by diffing a fresh list against the cache

**Takeaway:** When a watch loses its position, you cannot replay the missed events. Diff a fresh list against the cache, turn every absence into a synthetic delete, and wake everyone, because any item may have changed while you were blind.

<details>
<summary>The full story (1/N, 4/N, 16/N, 17/N)</summary>

- **Situation:** A 410 means the events between the stale RV and now are gone for good, including DELETED events.
- **What goes wrong:** Replacing the cache wholesale with the list would drop members that exist only locally (for example a CreateFailed member, which never reached the cluster). Only upserting the listed claims (insert or update) would keep deleted claims around as READY ghosts. Waking only the waiters named in events would miss claims that changed during the gap.
- **What we do:** `BatchState.resync_from_list` upserts every listed claim (released claims still stay ignored through the tombstone, [WATCH-4](#watch-4)), then calls `BatchState.mark_lost` for every cached member whose name is missing from the list. That synthesizes the missed DELETED events. The re-list branch of `_watch_loop` (both shells) then calls `_notify_all()`, which wakes all consumers and all acquire waiters (16/N), and runs `_pop_stale_connections(list(self._connected))` over every cached connection (17/N). The per-event path checks only the event's own claim. The re-list itself retries with the same backoff on a 429, 5xx, or transport error, and the watch then resumes from the list's RV. The 4/N MemberState enum is what gives LOST a single clear meaning here. Tests: `test_410_relists_and_diffs_vanished_claim_becomes_lost`, `test_410_relist_retries_503_then_succeeds_reconciling_lost_claim`, `test_410_relist_transport_error_retries_then_reconciles`, `test_connection_is_closed_once_its_members_pod_is_gone['gone on re-list']`.
- **Example:**
  ```text
  t0 cache {b1-0 READY}, connection cached
  t1 watch -> 410
  t2 list -> [] rv=9 -> b1-0 LOST, its connection closed, _notify_all()
  t3 watch(rv=9)
  ```
- **Related:** [WATCH-1](#watch-1), [WATCH-8](#watch-8), [PAT-32](../patterns.md#pat-32). [COUNT-2](count.md#count-2) covers idempotent transitions under repeated re-lists.

</details>

<a id="watch-4"></a>

## WATCH-4 Released names are a tombstone, written before any I/O

**Takeaway:** When local intent ("I released this") must beat a lagging event stream, keep a *tombstone* (a permanent "this one is gone" marker) keyed by an identifier that is never reused. Write it, plus any cleanup obligation, into local state before the cleanup I/O, and filter future events, re-lists, and outputs already queued.

<details>
<summary>The full story (15/N, 16/N, 19/N)</summary>

- **Situation:** `release_member` and `release_group` drop members locally before the cluster deletes the claims. After a release, the watch keeps delivering MODIFIED events (terminating, status updates) and then a DELETED event for the claim until the delete finishes, a re-list can still return the claim while it terminates, and a MEMBER_READY event may already sit in the queue. When `acquire()` fails, its create may still land later, and its rollback DELETE may fail.
- **What goes wrong:** Without a tombstone, the next MODIFIED event or a re-list would re-insert a released member into `members()`, `events()` could hand it out again, and its own deletion would emit MEMBER_LOST. A late ADDED for an abandoned acquire would put it back into `members()`. If the delete obligation lived only inside the failing call, a failed DELETE would leak the claim until its shutdownTime or Lease garbage collection.
- **What we do:** `BatchState._released` is a permanent set. `BatchState.release_claim` adds the name and pops the member, so a later `mark_lost` finds nothing. `BatchState.upsert_claim` returns early (`if claim_name in self._released: return None`). `BatchState.resync_from_list` goes through `upsert_claim`, so a re-list that still lists a terminating released claim ignores it too. `BatchState.pop_event` skips a queued MEMBER_READY for a released name. For acquire rollback, `BatchState.drop_acquired` (called from `_drop_acquired` in both shells) does this under the lock and before any network call: it adds the name to `_released` and, when needed, queues a sequenced pending delete. The shell then tries the DELETE and logs on failure; `release_not_ready()` (which returns `list(self._pending_deletes)`) and `release()` retry whatever is still pending. Ordinals are never reused, so the tombstone can never hide a new claim. Tests: `test_a_ready_event_queued_before_release_is_not_handed_out`, `test_released_members_leave_members_count_as_unable_and_ignore_later_events`, `TestAcquired.test_outcomes_drop_and_deletes`.
- **Example:**
  ```text
  t0 release_member(b1-3)  -> _released += b1-3, member popped
  t1 watch MODIFIED b1-3 (deletionTimestamp) -> upsert_claim ignores it
  t2 410 re-list still lists b1-3            -> ignored, members() never shows b1-3
  t3 watch DELETED b1-3    -> mark_lost finds nothing, no MEMBER_LOST
  ```
- **Related:** [WATCH-3](#watch-3), [WATCH-5](#watch-5). [RACE-5](race.md#race-5) covers sequenced pending deletes.

</details>

<a id="watch-5"></a>

## WATCH-5 Feed the name allocator from every name the watch shows you

**Takeaway:** A name allocator shared with other writers should be monotonic and should advance past every name observed from the server, not only past the names it handed out itself.

<details>
<summary>The full story (16/N)</summary>

- **Situation:** `acquire()` names new claims `<id>-<next_ordinal>`. A previous holder's in-flight create, or a claim the Lease's next-ordinal annotation did not record, can show up on the watch later.
- **What goes wrong:** If the allocator trusted only its own counter and the Lease annotation, a new `acquire()` could pick a name that already exists (a 409) or that collides with a claim still terminating.
- **What we do:** `BatchState.__init__` starts at `max(self.size, next_ordinal)`, and every `BatchState.upsert_claim`, whether from the seed list, the watch, or a re-list, runs `self._next_ordinal = max(self._next_ordinal, ordinal + 1)`. `BatchState.start_acquire` then allocates from that value, so names are never reused, even for claims this handle never created.
- **Example:**
  ```text
  t0 next_ordinal=10 (from the Lease)
  t1 watch sees b1-12 land late -> next_ordinal=13
  t2 acquire() -> creates b1-13
  ```
- **Related:** [WATCH-4](#watch-4) (never-reused names make the tombstone safe), [PAT-7](../patterns.md#pat-7). [LEASE-24](lease.md#lease-24) covers the ordinal contract.

</details>

<a id="watch-6"></a>

## WATCH-6 Decide level-triggered when a consumer subscribes

**Takeaway:** Evaluate the current state when a consumer subscribes, not only on future events. That is *level-triggered* (react to what is true now) rather than *edge-triggered* (react only to changes). When success and an expired deadline are both already true at subscribe time, let success win.

<details>
<summary>The full story (13/N, 14/N)</summary>

- **Situation:** The watch can report Ready members before the caller picks a consumer (events, group outcomes, or quorum), for example while `get_batch` seeds the cache.
- **What goes wrong:** If those members were dropped or streamed before the mode was known, a re-attached batch already at min_ready would never yield anything, and a later group consumer could not build cohorts from them.
- **What we do:** With no mode set, `BatchState._route_ready_member` (its `case _` branch) parks Ready fill members in `_waiting[pool]` and decides no outcome. `BatchState.set_mode` then re-evaluates from that state: STREAM flushes `_waiting` pool by pool, in Ready order within each pool, GROUP runs `_decide_group_outcome` for every pool, and QUORUM runs `_check_quorum` for every pool. If the fill already expired, `set_mode` checks success first, and only then times out the undecided groups or quorum. Tests: `test_ready_members_seen_before_a_mode_are_delivered_when_stream_mode_is_set`, `test_members_ready_before_the_mode_is_set_count`, `test_reattached_batch_at_min_ready_returns_at_once`.
- **Example:**
  ```python
  state.upsert_claim(b1)   # Ready, no mode yet
  state.upsert_claim(b0)   # Ready, no mode yet
  state.expire_fill()
  state.set_mode(QUORUM)   # quorum reached with [b1, b0], not a timeout
  ```
- **Related:** [COUNT-11](count.md#count-11), [COUNT-9](count.md#count-9). [API-15](api.md#api-15) covers why no verdicts happen before a consumer opts in.

</details>

<a id="watch-7"></a>

## WATCH-7 Events are history; re-check the live cache before acting

**Takeaway:** Treat event payloads and cache hits as optimizations behind your validity checks, never as shortcuts around them. On every lookup, check that the owner is still healthy and that the item is still in the right state, then use the cached value.

<details>
<summary>The full story (6/N, 13/N)</summary>

- **Situation:** A MEMBER_READY event or a GroupOutcome holds the immutable `Member` snapshot from the moment it was queued, and the consumer may read it much later. `connect()` also caches one Sandbox per claim name in `_connected`, and before 6/N it returned that cached entry before any other check.
- **What goes wrong:** Acting on a stale snapshot, for example connecting to a pod IP the member has since lost, reaches a dead or wrong sandbox. After a member stopped being Ready, or after `err()` was set (the Lease may be lost, and a batch recreated under the same id reuses claim names), the old `connect()` kept handing out the cached connection, which could reach a dead pod or a sandbox another client now owns.
- **What we do:** `BatchState._dispatch` queues `BatchEvent(member=self._members[claim_name])`, a frozen snapshot that is cheap to queue. The only thing an event carries forward is "this was handed out". Under `_lock`, `SandboxBatch.connect` and `AsyncSandboxBatch.connect` first run `_check_healthy()` (raising BatchError chained `from` `err()`), then re-read the member with `_state.get_member` and raise SandboxNotReadyError unless it is READY right now, and only then consult `_connected`. `detach()` still works after `err()` is set. Tests: `test_member_no_longer_ready_raises_even_when_cached`, `test_connect_after_err_raises_even_when_cached`.
- **Example:**
  ```text
  t0 connect(m)         -> creates and caches a connection
  t1 watch: m FAILED
  t2 connect(m)         -> SandboxNotReadyError, though _connected['b1-0'] still exists
  ```
- **Related:** [WATCH-8](#watch-8), [WATCH-9](#watch-9), [API-2](api.md#api-2). [API-2](api.md#api-2) covers why connect() trusts the live watch state, not the caller's Member snapshot.

</details>

<a id="watch-8"></a>

## WATCH-8 A cached connection is valid only while the member keeps the same sandbox and pod IP

**Takeaway:** When you cache a connection keyed on routing data from a watch, store the full routing identity you connected with (object name plus address) and drop the entry on any identity or address change, not on a passing health blip. That bounds wrong-target routing to the watch's latency, and leaves health gating to the code that hands out new connections.

<details>
<summary>The full story (8/N, 17/N)</summary>

- **Situation:** Since 17/N, `connect()` caches a Sandbox seeded with the watched pod IP, which saves one Sandbox GET per member during fan-out. `_connected` stores the Sandbox and the pod IP for each claim; the sandbox-id check was added to the 17/N change when it was restacked onto the 8/N TLS fix.
- **What goes wrong:** Once a pod is gone, Kubernetes can give its IP to another pod, even another sandbox in the same batch, so a cached connection silently reaches the wrong sandbox, and nothing notices until a request fails. A claim can also be rebound to a new sandbox on the same IP. On the other side, closing on every NotReady flicker would churn connections and break long-running work whenever a readiness probe blips.
- **What we do:** After each watch event `_pop_stale_connections([claim_name])` runs, and after a re-list it runs over every cached entry ([WATCH-3](#watch-3)). It uses `batch_state.still_reaches`: an entry stays only if the member is READY or PENDING, its `sandbox_name` equals the cached sandbox id, and `utils.select_pod_ip(pod_ips)` (IPv4 preferred, normalized) equals the cached IP. LOST, FAILED, a changed or missing IP, or a changed sandbox makes the entry stale, so it moves to `_stale_connections` and gets closed. NotReady on the same pod and IP (PENDING) keeps the connection, which matches Kubernetes, where a failing readiness probe only stops new Service traffic. `connect()` still refuses to hand out a new connection for a member that is not READY ([WATCH-7](#watch-7)). Known limit: a new sandbox that reuses the old sandbox's name on the same IP, as a sandbox created without the warm pool does (it takes the claim's name), can't be told apart. Test: `test_connection_is_closed_once_its_members_pod_is_gone` (8 cases: same IP keep, new IP close, new sandbox same IP close, NotReady same IP keep, no IP close, failed close, deleted close, gone on re-list close).
- **Example:**
  ```text
  t0 cached (sbx-1, 10.0.0.1)
  t1 MODIFIED sandbox=sbx-2 podIPs=[10.0.0.1] -> stale -> closed
  t2 MODIFIED Ready=False, same sbx-1 and IP  -> kept
  ```
- **Related:** [WATCH-3](#watch-3), [WATCH-7](#watch-7), [WATCH-9](#watch-9). [CLEAN-3](clean.md#clean-3) and [CLEAN-9](clean.md#clean-9) cover where stale connections go and who may evict them; [PERF-12](perf.md#perf-12) covers seeding the IP once.

</details>

<a id="watch-9"></a>

## WATCH-9 Put a failed-to-close resource where the read path can't reach it

**Takeaway:** Decide where a resource that failed to close goes by asking whether the read path can still hand it out. A put-back into the main cache is fine when a state check in front of the cache already blocks it. Otherwise use a separate place readers never look.

<details>
<summary>The full story (6/N, 10/N, 15/N, 17/N)</summary>

- **Situation:** `release_member`, `release_group`, and `release_not_ready` close a member's cached connection with `_close_cached_connections(action, names)`, which passes `keep=partial(self._connected.setdefault, claim_name, entry)` to `_close_one` and so puts a failed close back into `_connected`. Staleness the watch detects sends failures to `_stale_connections` instead.
- **What goes wrong:** A put-back into the readable cache is safe only if no reader can get it back out. A member the watch marked stale is often still READY (just at a new IP), so a put-back there would be handed out by the next `connect()`.
- **What we do:** `release_member` closes before it deletes, and by then `_state` no longer has the member, so `connect()` raises SandboxNotReadyError before it ever reads `_connected` (the 6/N ordering in [WATCH-7](#watch-7)). Ordinals are never reused, so the claim-name key can't come back to life. The watch path has no such gate, which is why it uses the side list.
- **Example:**
  ```text
  t0 release_member(m): state drops m
  t1 close fails -> _connected[m] restored
  t2 connect(m) -> SandboxNotReadyError (the entry is never returned)
  ```
- **Related:** [WATCH-7](#watch-7), [WATCH-8](#watch-8), [CLEAN-1](clean.md#clean-1), [PAT-11](../patterns.md#pat-11). [CLEAN-3](clean.md#clean-3) covers how the watch moves stale connections to a side list under the state lock, then closes them outside the lock.

</details>

<a id="watch-10"></a>

## WATCH-10 Bypass a watch-backed liveness check once the target is rebaselined or the watch stops

**Takeaway:** A watch-cached routing snapshot can answer liveness checks in zero RPCs only while the watch is live and the target still matches the snapshot you acquired. Once the caller replaces the pod or records a finer-grained pod baseline, or once the watch stops, fall through to a direct pod read.

<details>
<summary>The full story (25/N, 92115dd8)</summary>

- **Situation:** In `examples/agent-sandbox-rl`, per-claim `fleet.acquire()` runs `SandboxHandle.record_pod_identity()` (one Pod GET) so `is_alive()` can detect pod replacement (`pod_uid`) and in-place container restarts (`runtime_restarts`). With `claim_mode="batch"`, `fleet.acquire()` skips that initial Pod GET because the batch watch already tracks claim loss and pod replacement (`sandbox_name` and `pod_ips`).
- **What goes wrong:** Two transitions invalidate a comparison against the acquired `Member` snapshot:
  - `SandboxHandle.refresh()` (for example after suspend and resume) moves the handle onto a brand-new pod whose IP differs from `member.pod_ips`. Comparing against the acquired `Member` would report every resumed sandbox as dead.
  - Once `batch.err()` is set, the batch watch stops updating, so a stale `READY` entry in `batch.members()` would hide a deleted claim or replaced pod.
- **What we do:** `SandboxHandle.is_alive()` (`handles.py`) consults `self._batch_claim.still_reaches()` only when `self._batch_claim is not None and self.pod_uid is None`, and returns `False` with zero GETs when `batch_state.still_reaches` sees claim loss or a changed sandbox or pod IP. `BatchClaim.still_reaches()` (`batch_claims.py`) returns `True` immediately when `self.rebaselined or self.batch.err() is not None`, falling through to `is_alive()`'s bounded Pod GET. `SandboxHandle.refresh()` sets `self._batch_claim.rebaselined = True` and calls `record_pod_identity()`. Callers that also want in-place container-restart baselines call `record_pod_identity()`, which sets `self.pod_uid` and routes later checks through the Pod GET. Test: `test_batch_claims.py: test_batch_acquire_skips_pod_get_and_is_alive_checks_the_batch_watch`.
- **Example:**
  ```text
  acquire (batch)       -> 0 Pod GETs; is_alive() checks batch watch via still_reaches()
  refresh() or err()    -> rebaselined / err set -> is_alive() falls through to Pod GET
  ```
- **Related:** [WATCH-8](#watch-8), [PERF-12](perf.md#perf-12).

</details>

<a id="watch-11"></a>

## WATCH-11 Restart list pagination cleanly on `410 Gone` `continue` expiry, isolate malformed per-object timestamps, and close watch streams on teardown

**Takeaway:** Any paginated Kubernetes `LIST` (`continue` token) can fail with `410 Gone` on page 2+ when `etcd` compacts the snapshot RV, and a single malformed object in a shared namespace must never abort an entire sweep (`F-18`). On handle teardown, explicitly interrupt sync `Watch.stop()` + join `_watch_thread` and wrap async watch generators in `contextlib.aclosing` (`F-16`).

<details>
<summary>The full story (Chaos & Verification Suite: F-16 & F-18, batch_reaper.py, sandbox_batch.py, async_sandbox_batch.py)</summary>

- **Situation:** `batch_reaper.find_stale_leases` pages through batch Leases with `limit=500` and `continue` tokens (`batch_reaper.py:139-165`), while `SandboxBatch` and `AsyncSandboxBatch` maintain long-lived watch streams (`_watch_loop`).
- **What goes wrong:**
  1. **Reaper `410` crash & poison-pill `renew_time` (`F-18`):** Unlike `_claim_list_pages` (`20/N`), `batch_reaper.find_stale_leases` did not restart from `continue=None` when page 2 raised `ApiException(status=410)`, failing the entire namespace sweep. Worse, one `V1Lease` carrying a timezone-naive `spec.renew_time` raised `TypeError: can't subtract offset-naive and offset-aware datetimes` inside `is_lease_reapable`, aborting all healthy stale Leases behind it in that namespace.
  2. **Un-interrupted sync watch socket & unclosed async generator (`F-16`):** Sync `_stop_background_threads` set `_stop_watch` without interrupting the active `Watch().stream(...)` call or joining `_watch_thread` (`CANCEL-10`), leaving a daemon thread and urllib3 socket blocked up to `65 s` (`BATCH_WATCH_TIMEOUT_SECONDS + 5`) after `release()` returned. Async `_watch_loop` iterated `async for event in self._client.k8s_helper.watch_sandbox_claims(...)` without `contextlib.aclosing(...)`, deferring `aiohttp` stream closure to GC when `break` or cancellation exited early.
- **What we do:** Retry `find_stale_leases` from `continue=None` (bounded retries) on `410 Gone` and catch/log per-Lease evaluation errors inside the loop so one bad Lease never blocks its neighbors; store the active sync `Watch` reference so teardown calls `w.stop()` and joins `_watch_thread` within a bounded grace, and wrap async `watch_sandbox_claims` iteration in `async with contextlib.aclosing(...)`.
- **Related:** [WATCH-2](#watch-2), [CANCEL-10](cancel.md#cancel-10), [LIB-23](lib.md#lib-23), [CLEAN-23](clean.md#clean-23).

</details>

---

[Learnings home](../../learnings.md) · [Field guide index](../../learnings.md#sec-field-guide)
