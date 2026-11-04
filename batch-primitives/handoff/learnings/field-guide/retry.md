<a id="sec-retry"></a>

[Learnings home](../../learnings.md) · [Field guide index](../../learnings.md#sec-field-guide)

# Retries, backoff, pacing, and deadlines

This section covers how the batch SDK waits and tries again: how long it backs off after an error, how it spreads creates out over time (`create_rps` pacing), when it lets some creates skip that pacing (warm credit), and how it keeps every wait inside a caller's deadline or a Lease's expiry. One idea ties the entries together: **every wait must be scoped to the thing it protects and bounded by the deadline that matters.** Server-wide pushback goes on the shared limiter, and one socket's blip stays on one worker. A deadline gets checked before anything shared is spent, and a retry budget counts progress instead of attempts. Read the first few entries for rules that apply to almost any retry loop, then the later ones for the pacing and warm-credit machinery specific to batch creates.

A few terms used throughout: the **apiserver** is the Kubernetes API server. **APF** (API Priority and Fairness) is its server-side rate limiter, which answers overloaded clients with `429 Too Many Requests` and often a **Retry-After** header saying how many seconds to wait. **Jitter** means adding randomness to a wait so many clients don't retry at the same instant. A **monotonic clock** (`time.monotonic()`) only moves forward, so deadlines computed from it don't jump when the wall clock changes.

<a id="retry-1"></a>

## RETRY-1 Retry only an allowlist of known-transient errors, with a hard attempt cap

**Takeaway:** Make retry classification an allowlist of conditions you know are transient (no HTTP status, 429, 5xx, listed transport errors), cap the attempts, and let everything else fail on the first occurrence. Use the same predicate for every step of a multi-step loop so permanent errors and domain errors surface at once.

<details>
<summary>The full story (12/N, 15/N)</summary>

- **Situation:** Claim creates can raise an `ApiException` (an HTTP error from the Kubernetes client), a urllib3 or aiohttp transport error, or anything else (a bug, a `ValueError`, an SDK error). The release loop `_delete_claims_in_rounds` funnels errors from several steps into one place: the Lease take ("hold"), the `deletecollection` call (one request that deletes every object matching a label), the live-claim count, the final step, and `guard()`.
- **What goes wrong:** A broad "retry on `Exception`" retries programming errors and SDK timeouts (see [LIB-5](lib.md#lib-5)), burns pacing slots, and hides bugs. In the release loop it would keep re-deleting after a 403 (an RBAC permission error), or after a `BatchInUseError` or `BatchError` that means this handle no longer owns the batch.
- **What we do:** `sandbox_batch.py: SandboxBatch._create_one` (and the async `AsyncSandboxBatch._create_one`) maps an `ApiException` through `batch_utils.create_error_outcome(status, attempt)`, a transport error (`_is_transport_error`) through `create_error_outcome(None, attempt)`, and any other exception straight to `FAIL`. Only status `None`, 429, or 5xx (`batch_utils.is_retryable_status`) retries, and only while `attempt < BATCH_CREATE_ATTEMPTS` (3). A failed create becomes a `CreateFailed` member, not a batch error. In `_delete_claims_in_rounds` (both shells), only `_is_transient_error` (status `None`/429/5xx, or a listed transport error) moves to the next round. A 403, `BatchInUseError`, or the guard's `BatchError` raises at once. After the last idle round (see [RETRY-11](#retry-11)), the loop raises the last transient error rather than a generic one.
- **Example:**
  ```text
  create attempt 1 -> 503       -> RETRY
  create attempt 2 -> 403       -> FAIL (CreateFailed member)
  create attempt 1 -> ValueError -> FAIL at once, no retry
  release round    -> 403       -> raise at once, no next round
  ```
- **Related:** [RETRY-2](#retry-2), [RETRY-11](#retry-11), [RETRY-12](#retry-12), [BP-21](../best-practices.md#bp-21).

</details>

<a id="retry-2"></a>

## RETRY-2 Backoff shape: equal jitter, a capped exponent, a capped Retry-After, reset only on progress, and an interruptible sleep

**Takeaway:** A background retry loop needs jittered exponential backoff with both the wait and the exponent capped. It should honor the server's Retry-After (parsed strictly as ASCII digits and capped by your own liveness needs), reset only on real progress, and sleep on something that a stop can interrupt. Python's `isdigit()` is Unicode-wide, so it is not a safe numeric validator by itself.

<details>
<summary>The full story (1/N, 12/N commit 522dad4c, 13/N commit 0bf8e443)</summary>

- **Situation:** The watch (the long-lived stream that tells the batch about claim changes) retries 429/5xx responses, transport errors, short watches ([RETRY-3](#retry-3)), and failed re-lists. Creates and release rounds share the same helpers. Retry-After may be delta-seconds or an HTTP-date, and the header value is untrusted.
- **What goes wrong:** The original watch waited a fixed 0.5 s, so a struggling apiserver got two requests a second per batch, forever (PR 1 revision item 3 replaced it). Without jitter, many batches reconnect in lockstep after an apiserver restart. An uncapped exponent (`0.5 * 2**n`) eventually overflows a float during a long outage. A plain `time.sleep` would also delay `detach()`. On the Retry-After side, `str.isdigit()` accepts characters such as `'²'` that `float()` rejects, which would crash the retry path. `float()` alone would accept `'inf'` and `'nan'`. A huge value would stall every worker, because a Retry-After moves the shared pacing cursor ([RETRY-6](#retry-6)).
- **What we do:** `batch_utils.backoff_delay` and `batch_utils.retry_delay` with `BATCH_BACKOFF_BASE_SECONDS=0.5` and `BATCH_BACKOFF_MAX_SECONDS=30.0`. The wait is `min(30, 0.5 * 2**(n-1))` and the returned delay is equal-jittered in `[wait/2, wait]`. The exponent is capped at `ceil(log2(cap/base))` before `2**n` is computed. `batch_utils.retry_after_seconds` lets a numeric Retry-After override the backoff: it strips the value, requires `isascii()` and `isdigit()` (which rejects negatives, `inf`, `nan`, and decimals), and returns `min(value, 30)`. The HTTP-date form and anything unparsable return `None`, so `retry_delay` falls back to the jittered backoff. `test_batch_utils.py: TestRetryDelay` covers the HTTP-date, `'²'`, and capped `'600'` cases. In `_watch_loop` (both shells), `failures` counts consecutive failures across watch and re-list, and any received event, or a stream that ends at its timeout, resets it to 0. The sync loop sleeps on `self._watch_stop.wait(...)` so a stop wakes it, and the async loop's `asyncio.sleep` is cancelled by `task.cancel()`.
- **Example:**
  ```python
  doublings = min(attempt - 1, math.ceil(math.log2(cap / base)))
  wait = min(cap, base * 2 ** doublings)
  return wait / 2 + rand() * wait / 2
  ```
  ```text
  503, ProtocolError, 429, empty watch -> waits in [0.25,0.5], [0.5,1], [1,2], [2,4]
  event arrives                        -> failures = 0
  next 503                             -> wait in [0.25,0.5]
  Retry-After '7' -> 7.0, '600' -> 30.0
  Retry-After '²', '-1', 'inf', HTTP-date -> None (jittered backoff)
  ```
  Known limit: a stream that always dies right after one event keeps retrying every 0.25 to 0.5 s.
- **Related:** [RETRY-3](#retry-3), [RETRY-6](#retry-6), [BP-22](../best-practices.md#bp-22). [LIB-17](lib.md#lib-17) and [LIB-7](lib.md#lib-7) also cover Retry-After parsing, including the urllib3 6-hour default.

</details>

<a id="retry-3"></a>

## RETRY-3 A stream that ends at once with no events counts as a failure

**Takeaway:** A reconnect loop must treat "ended cleanly, but suspiciously fast and empty" as a failure. If only exceptions count as failures, a clean end-of-stream loop hammers the server.

<details>
<summary>The full story (12/N, commit 522dad4c)</summary>

- **Situation:** The watch loop assumed every clean end of a stream was the apiserver's 30 s watch timeout and reopened the stream at once.
- **What goes wrong:** A proxy or broken connection that ends each stream immediately, or a stream of lines the client can't decode, turned the loop into a busy loop: about 200 requests in a few milliseconds in a unit test (12/N commit message, "back off when a batch watch ends at once with no events").
- **What we do:** Like client-go's reflector (the Go watch-and-cache loop), `_watch_loop` in both shells tracks `got_event` and `watch_started`. A stream that ends within `_SHORT_WATCH_SECONDS` (1 s) without delivering an event increments `failures` and waits `retry_delay(failures)`. A stream that ran its course resets `failures` to 0. Test: `test_consecutive_failures_and_empty_watches_back_off_and_an_event_resets_the_delay`.
- **Example:**
  ```python
  if not got_event and time.monotonic() - watch_started < _SHORT_WATCH_SECONDS:
      failures += 1
      wait(retry_delay(failures))
      continue
  ```
- **Related:** [RETRY-2](#retry-2), [BP-8](../best-practices.md#bp-8).

</details>

<a id="retry-4"></a>

## RETRY-4 Split errors into recover, retry, and fatal, and on fatal publish the error and wake every waiter

**Takeaway:** A background worker that owns shared state must sort errors into recover, retry, and fatal. On a fatal error it must record the error where callers can see it and wake everyone who is waiting before it exits. A thread that dies without telling anyone is the worst outcome.

<details>
<summary>The full story (1/N, 2/N)</summary>

- **Situation:** The watch thread (or task) is the only thing that keeps `members()` current, and consumers (`events()`, `wait_for_quorum()`, `acquire()`) block on it.
- **What goes wrong:** If the watch exits on an error silently, `members()` freezes, consumers wait forever, and `err()` stays `None`. Retrying permanent errors forever (403 RBAC, 404 because the CRD is missing) hides misconfiguration.
- **What we do:** In `_watch_loop` (both shells), a 410 Gone (the server no longer has history at our resourceVersion) sets `relist=True` and continues at once, with no backoff and no failure count. Status `None`/429/5xx or a transport error backs off ([RETRY-2](#retry-2)) and retries from the last resourceVersion, or retries the re-list. Any other `ApiException` or unknown exception goes to `BatchState.note_error`, then `_notify_all()` wakes every fill consumer and sets every `acquire()` waiter, and the loop returns. Consumers check `state.error()`: the `events()` and `group_outcomes()` iterators end, and `wait_for_quorum()` and `acquire()` raise `BatchError`. Tests: `test_403_surfaces_through_err`, `test_410_relist_403_surfaces_through_err_and_stops`, `test_unexpected_error_surfaces_through_err_and_stops`, `test_iterators_end_when_the_watch_fails`.
- **Example:**
  ```text
  t0 watch -> 403
  t1 note_error(403); _notify_all()
  t2 events() iterator returns; acquire() raises BatchError; err() returns the 403
  ```
- **Related:** [RETRY-1](#retry-1), [RETRY-2](#retry-2), [BP-21](../best-practices.md#bp-21).

</details>

<a id="retry-5"></a>

## RETRY-5 Pace creates with a shared reservation cursor: reserve under the lock, sleep outside it

**Takeaway:** To share one client-side rate limit across many workers, reserve a future time slot under a short lock and sleep without the lock (the GCRA or "virtual scheduling" idea). Count retries against the same budget, and give burst capacity only from a real capacity signal, not from a constant.

<details>
<summary>The full story (12/N, 13/N)</summary>

- **Situation:** `max_in_flight` create workers, plus `acquire()`, share one `create_rps` budget.
- **What goes wrong:** Per-worker sleeps would multiply the rate by the number of workers. A token bucket needs an arbitrary burst size; benchmark_rerun_review.md rejected it because "the burst size is arbitrary, and it ignores what the pool actually has". Sleeping while holding the lock would serialize every worker.
- **What we do:** `sandbox_batch.py: SandboxBatch._reserve_create_slot` (and the async mirror) runs under `_lock`: `slot = max(earliest, _next_create_at)` and `_next_create_at = slot + _create_interval` (`1/create_rps`). The caller then sleeps until `slot` outside the lock, on the stop event so teardown interrupts it. An idle cursor sits in the past, so the next slot is "now" and no burst credit builds up, unlike a token bucket. Every retry reserves its own slot, so retries don't push the total past `create_rps`. Burst capacity comes only from warm credit ([RETRY-15](#retry-15)). Test: `test_creates_are_bounded_by_max_in_flight_and_paced_by_create_rps`.
- **Example:**
  ```text
  interval 0.05 s
  worker A reserves t0, worker B reserves t0+0.05, worker C reserves t0+0.10
  each releases the lock and sleeps to its own slot
  ```
- **Related:** [RETRY-6](#retry-6), [RETRY-15](#retry-15), [BP-25](../best-practices.md#bp-25), [PAT-17](../patterns.md#pat-17).

</details>

<a id="retry-6"></a>

## RETRY-6 Put server-wide pushback on the shared limiter and per-connection blips on the worker

**Takeaway:** A Retry-After from the server applies to everyone, so it moves the shared rate limiter. A transport blip or a 5xx without Retry-After belongs to one connection, so that worker sleeps alone and then re-enters normal pacing. Check the deadline again after every wait.

<details>
<summary>The full story (13/N commit 0bf8e443, 16/N commit 810d3773; was 3.9)</summary>

- **Situation:** Every create retry used to advance the shared `_next_create_at` pacing cursor by its full exponential backoff. Retries may also carry an optional caller deadline (from `acquire(timeout=...)`).
- **What goes wrong:** One single-socket transport blip stalled all 20 creator workers. Only an APF Retry-After really applies to the whole priority level. Separately, a retry paced past the deadline only creates a claim that then has to be deleted.
- **What we do:** 13/N split retries by cause in `SandboxBatch._create_one` and `AsyncSandboxBatch._create_one`, matching client-go. Without Retry-After, the worker checks `_check_retry_deadline` against `now + backoff`, sleeps locally on `_stop_creating.wait(backoff)`, then re-checks the deadline (other creates kept taking slots meanwhile) and reserves a normal pacing slot. With Retry-After, it reserves a slot at `now + Retry-After` on the shared cursor, so every worker waits behind it.
- **Example:**
  ```python
  if retry_after is None:
      backoff = retry_delay(attempt, None)
      check_retry_deadline(now + backoff); stop.wait(backoff)
  earliest = now + (retry_after or 0.0)
  check_retry_deadline(earliest); slot = reserve_create_slot(earliest)
  ```
  Tests: `test_a_backoff_retry_leaves_the_next_pacing_slot_free_and_a_retry_after_takes_it`, `test_a_backoff_retry_is_not_paced_past_the_deadline`.
- **Related:** [RETRY-5](#retry-5), [RETRY-7](#retry-7), [RETRY-16](#retry-16), [BP-24](../best-practices.md#bp-24).

</details>

<a id="retry-7"></a>

## RETRY-7 Test the deadline before consuming any shared, accounted resource

**Takeaway:** When callers carry deadlines in front of a shared rate limiter, check that the earliest available slot comes before the deadline before you touch anything shared: cache credit, identifiers, or rate-limit slots. Re-check after any phase that can overrun the deadline.

<details>
<summary>The full story (16/N commits dc9ea439 and 810d3773, 19/N commit 2ecdf384; was 3.12)</summary>

- **Situation:** `acquire(warmpool, timeout)` and its create retries reserve slots from the shared `create_rps` cursor (`_next_create_at`). Before that, `_check_pool` runs a first-use pool check, which can return after the deadline because the shared check is not capped by any one caller's deadline ([RETRY-18](#retry-18)).
- **What goes wrong:** Under heavy load `_next_create_at` can already sit past the caller's deadline. Reserving anyway burns a monotonic ordinal (the claim name number, never reused), pushes `_next_create_at` further out for callers behind it, sleeps past the timeout, creates a doomed claim, and immediately sends a DELETE to tear it down. Calling `WarmCredit.take()` before the deadline test would also spend cached warm credit on a call that is about to raise, starving later callers of covered creates.
- **What we do:** In `SandboxBatch.acquire` (and the async mirror), one `_lock` section does `covered = now < deadline and self._warm_credit.take(warmpool)`, so the short-circuit `and` keeps a doomed call away from the cache. Then `if not covered and max(now, _next_create_at) >= deadline` raises `BatchTimeoutError` before `spend()`, `start_acquire()` (ordinal allocation), waiter install, or `_reserve_create_slot()`. `_check_retry_deadline` repeats the check before retry backoffs and retry slots ([RETRY-6](#retry-6)).
- **Example:**
  ```text
  timeout=0.05, pool check takes 0.08 s
  -> BatchTimeoutError "no create slot for warm pool pool-d within 0.05s"
  -> no POST, next_ordinal unchanged
  ```
  Test: `test_an_acquire_waiting_on_another_acquires_pool_check_stops_at_its_timeout` (the pool-d case asserts `create_call_count == 3` and `next_ordinal == 3`).
- **Related:** [RETRY-6](#retry-6), [RETRY-16](#retry-16), [RETRY-18](#retry-18), [BP-26](../best-practices.md#bp-26).

</details>

<a id="retry-8"></a>

## RETRY-8 Give every request an explicit timeout, and set long calls just above the server's own timeout

**Takeaway:** Never rely on HTTP client defaults for timeouts. Bound every request on a path the caller can't interrupt, set long-running calls slightly above the server's own timeout so the server's clearer error wins, and remember that paging multiplies per-request timeouts.

<details>
<summary>The full story (2/N commit 77c05c26, 12/N commit 522dad4c)</summary>

- **Situation:** Neither the `kubernetes` nor the `kubernetes_asyncio` client has a default request timeout, so a hung connection blocks forever.
- **What goes wrong:** Paths the caller can't interrupt (get_batch's Lease read and claim list, the takeover write, detach, the 410 re-list, claim_batch's Lease create and precheck) could hang indefinitely. A collection timeout below 60 s would cut off a list or `deletecollection` that the apiserver would otherwise finish or end with a clean 504.
- **What we do:** Every batch helper in `k8s_helper.py` takes `_request_timeout`. Single-object calls use `batch_utils.BATCH_REQUEST_TIMEOUT_SECONDS` (30 s). Lists and `deletecollection` use `BATCH_COLLECTION_REQUEST_TIMEOUT_SECONDS` (90 s), so the apiserver's own 60 s limit ends a long request first with a 504. Callers include `SandboxBatch._attach`, `_claim`, and `_delete_claims_in_rounds`. Paging makes the timeout per page, so one list can take pages x 90 s in total (a risk noted in pr8_plan.md). Test: `test_claim_batch_requests_forward_resource_selector_and_timeout`.
- **Related:** [RETRY-9](#retry-9), [RETRY-18](#retry-18), [BP-20](../best-practices.md#bp-20), [LIB-10](lib.md#lib-10).

</details>

<a id="retry-9"></a>

## RETRY-9 Treat Lease expiry as a deadline: cap every sleep and every request by the time left

**Takeaway:** A lease holder's expiry is a deadline, not something you notice when a failed attempt returns. Cap every sleep and every request timeout by the time left before that deadline.

<details>
<summary>The full story (3/N commit 31a47728)</summary>

- **Situation:** A **Lease** (a small `coordination.k8s.io` object used as a liveness heartbeat) is renewed by a loop that sleeps between attempts, and each attempt is an HTTP call that can hang. The holder declares itself expired D seconds (the lease duration) after it sent its last successful renewal.
- **What goes wrong:** Expiry used to be checked only after a failed attempt returned. Attempts were one renew interval apart and each could take one renew interval to time out, so expiry could fire about 5D/3 after the stored renewTime (reaper_contract.md). That breaks the ordering with the reaper's threshold.
- **What we do:** `SandboxBatch._renew_loop` (and the async mirror) sleeps `min(next wait, deadline - now)`, so it wakes at expiry even when no renewal is due. `_renew_request_timeout` returns `min(_renew_interval, time left)` and raises `TimeoutError` at once if no time is left. `_write_renewal` uses that timeout. Expiry therefore fires at `last_sent + D`.
- **Example:**
  ```text
  D=10, interval 3
  t3  renewal sent, succeeds -> deadline 13
  t8  renewal fails
  t9  retry hangs until t12
  t12 next wait capped to 13-12 = 1
  t13 expired
  ```
  Test: `test_expiry_fires_a_lease_duration_after_the_last_successful_renewal_was_sent` (the clock ends at exactly 13).
- **Related:** [RETRY-10](#retry-10), [RETRY-8](#retry-8), [RETRY-19](#retry-19).

</details>

<a id="retry-10"></a>

## RETRY-10 Renew on two cadences: a relaxed interval when healthy, a short deadline-capped backoff after a failure

**Takeaway:** Lease renewal needs a relaxed steady-state interval and an aggressive, deadline-capped retry after a failure. When other actors key off the steady cadence, treat it as a cross-client contract.

<details>
<summary>The full story (3/N commit 31a47728)</summary>

- **Situation:** The holder renews every `max(1, D // 3)` seconds, so a couple of missed renewals still leave margin.
- **What goes wrong:** Waiting a full interval after a failure lets a brief apiserver blip use up most of the time left before expiry.
- **What we do:** `SandboxBatch._renew_loop` (both shells) counts consecutive failures from the `_renewal_degraded` flag. With 0 failures it waits `_renew_interval`. Otherwise it waits `backoff_delay(failures, 0.5, 30)`, still capped by the expiry deadline ([RETRY-9](#retry-9)). One success clears `_renewal_degraded` and resets to the normal cadence. The cadence `max(1, D // 3)` is part of the reaper contract, because the reaper's confirm step waits one renew interval before deleting a stale Lease.
- **Example:** `test_failed_renewals_retry_on_a_backoff_and_stop_at_expiry` (with `backoff_delay` patched so the nth retry waits n seconds) pins the send times to `[3, 4, 6, 9]`.
- **Related:** [RETRY-9](#retry-9), [RETRY-2](#retry-2), [BP-1](../best-practices.md#bp-1), [LEASE-9](lease.md#lease-9).

</details>

<a id="retry-11"></a>

## RETRY-11 For long server-side bulk work, give up after N rounds in a row with no progress

**Takeaway:** When a server-side bulk operation can keep running after your request times out, measure progress between attempts. Give up after K rounds in a row without progress, not after K attempts, and grow the backoff only while stalled.

<details>
<summary>The full story (12/N)</summary>

- **Situation:** `release()` deletes claims with `deletecollection`. The apiserver works through it one object at a time, so for a large batch the request can time out while deletion keeps going on the server.
- **What goes wrong:** A fixed attempt count would abandon a large batch that is still being deleted. Retrying forever would hang on a real stall.
- **What we do:** `_delete_claims_in_rounds` (both shells) counts live claims after each round. A round whose count is lower than the previous round's (`remaining_before`) resets `idle_rounds` to 0, and any other round increments it. At `BATCH_RELEASE_MAX_IDLE_ROUNDS` (3), it raises the last transient error, or `BatchError("N claims are still not being deleted")`. The backoff is `retry_delay(max(idle_rounds, 1), last_error)`, so it grows only while stalled. Test: `test_release_retries_rounds_until_no_progress` ("progress resets the idle rounds").
- **Example:**
  ```text
  live counts 5,5,4,4,4,0 -> succeeds (the 4 resets the idle counter)
  live counts 2,2,2,2     -> BatchError
  ```
- **Related:** [RETRY-1](#retry-1), [RETRY-12](#retry-12), [BP-11](../best-practices.md#bp-11), [PAT-31](../patterns.md#pat-31).

</details>

<a id="retry-12"></a>

## RETRY-12 Retry single-object deletes a few times, count not-found as success, and re-check ownership before each sleep

**Takeaway:** For idempotent single-object deletes, treat not-found as success, retry only transient errors with a small cap, re-check that you still own the object before each sleep, and keep a pending marker so a later call can retry.

<details>
<summary>The full story (15/N)</summary>

- **Situation:** `release_member`, `release_not_ready`, and the other single-claim cleanup paths delete claims by name through `SandboxBatch._delete_claim` (and the async mirror).
- **What goes wrong:** Without retries, a single 503 leaves a claim behind. Sleeping without re-checking could send a delete after `detach()` or after `err()`, possibly against a batch recreated under the same name.
- **What we do:** Retry 429, 5xx, and transport errors up to `BATCH_DELETE_ATTEMPTS` (3), with `retry_delay` honoring Retry-After capped at 30 s. Call `_check_lease_held()` before each sleep. `k8s_helper.py: delete_sandbox_claim` swallows a 404, so a claim already gone counts as deleted. The claim stays in `_pending_deletes` until `note_deleted`, so calling the release again retries it. Each attempt holds a `max_in_flight` slot only for the HTTP call and releases it before sleeping (see [LOCK-10](lock.md#lock-10)). Test: `test_release_member_deletes_its_claim_with_the_shared_retry_policy`.
- **Related:** [RETRY-1](#retry-1), [RETRY-11](#retry-11), [LEASE-11](lease.md#lease-11).

</details>

<a id="retry-13"></a>

## RETRY-13 Start the readiness deadline when the last unit of work gets its pacing slot, not when the job starts

**Takeaway:** When work goes out through a rate limiter, start a "time to readiness" clock when the last item is dispatched, not when the job begins. Re-derive it on every path that can end dispatch early.

<details>
<summary>The full story (13/N, 15/N)</summary>

- **Situation:** `quorum_timeout` bounds how long groups may take to reach `min_ready`, but the fill is paced at `create_rps`.
- **What goes wrong:** A deadline of construction time + `quorum_timeout` would time out every group before its claims even existed: 50k claims at 50 rps need 1,000 s of pacing (pr2_design.md).
- **What we do:** In `SandboxBatch._create_worker` (both shells), the worker that pops the last planned create sets `self._fill_deadline = slot + self._quorum_timeout` (the slot is the send time, not the response time or any retries) and notifies waiters. If `release_group` or `release_not_ready` empties the plan first, `_drop_planned_creates` sets it from `max(now, _next_create_at - _create_interval)`, the last slot already taken. A re-attached handle (`_attach`) counts from attach time: `time.monotonic() + settings.quorum_timeout`. Test: `test_groups_yield_when_ready_and_time_out_after_the_last_create`.
- **Example:**
  ```text
  10 creates at 5 rps, quorum_timeout 60
  t0    first create slot
  t1.8  last create slot -> fill deadline = t61.8 (not t60)
  ```
- **Related:** [RETRY-14](#retry-14), [RETRY-5](#retry-5), [COUNT-12](count.md#count-12).

</details>

<a id="retry-14"></a>

## RETRY-14 Cancel queued work by removing it from the queue atomically, so it takes no rate-limit slots

**Takeaway:** Cancel queued work by removing it from the queue in the same critical section as the cancel decision, so it never consumes rate-limit budget. Record each removed item as resolved so completion accounting still adds up, and recompute any deadline derived from the queue.

<details>
<summary>The full story (15/N commit 113fae28)</summary>

- **Situation:** `release_group` and `release_not_ready` can run while many creates are still planned.
- **What goes wrong:** Leaving released entries in the plan lets workers pop them and burn pacing slots (and move the shared cursor) on creates that would be skipped or deleted anyway, which delays other groups. Removing them silently would leave the fill unable to settle, and a fill deadline computed from the full plan would stay too far out.
- **What we do:** In the same `_lock` block as the state release, `SandboxBatch._drop_planned_creates(warmpool)` (both shells) rebuilds the `_create_plan` deque without that pool's entries (or without all entries when `warmpool` is `None`) and calls `release_claim` on each one, so it counts as released or unable. If the plan becomes empty, it also resets the fill deadline ([RETRY-13](#retry-13)).
- **Example:** In `test_release_group_deletes_by_group_label_and_skips_only_its_creates`, b1-11 follows b1-10 within 0.6 s at 5 rps, because the released pool's planned creates took no slots in between.
- **Related:** [RETRY-13](#retry-13), [RETRY-5](#retry-5).

</details>

<a id="retry-15"></a>

## RETRY-15 Let creates that won't touch the protected resource skip pacing (warm credit)

**Takeaway:** Pace by the resource the limit actually protects. When a cheap signal shows that some requests won't touch it, let exactly that many through unpaced, and accept that a stale signal degrades to the unoptimized baseline rather than to something worse.

<details>
<summary>The full story (19/N commit 99a4a93d)</summary>

- **Situation:** `create_rps` pacing exists so a burst of cold claims doesn't start many pods on one node at once: 10.2 s to first Ready unpaced versus 1.35 s at 50/s (99a4a93d commit message). A **warm pool** keeps pre-started sandboxes, and its `readyReplicas` field says how many are Ready right now. A claim adopted from a Ready sandbox starts no pod.
- **What goes wrong:** Pacing warm claims made the batch slower than plain per-claim creates: 160 warm claims were Ready in 3.24 s at 50/s, against 1.46 s with per-claim creates (99a4a93d commit message).
- **What we do:** `batch_utils.py: WarmCredit` holds a per-pool credit. `_claim` records each pool's `readyReplicas` from `_precheck_pools`. In `_create_worker` (both shells), a create the credit covers (`take()`) uses `slot = now`, so only `max_in_flight` bounds it. Otherwise `spend()` deducts and the create is paced. Credit is per pool, so pool-a's spare credit never covers pool-b. The fill never re-reads the pool (only `acquire()` refreshes, [RETRY-17](#retry-17)). A stale count, for example because another client took those sandboxes, just sends some claims unpaced, which is what per-claim creates do anyway. Retries of covered creates still reserve paced slots through `_reserve_create_slot`.
- **Example:**
  ```text
  pool-a ready=5 size 3, pool-b ready=0 size 3, create_rps 4
  b1-0..b1-3 go at once
  b1-4 at +0.25 s, b1-5 at +0.5 s
  ```
  Test: `test_creates_the_warm_pool_covers_skip_pacing`.
- **Related:** [RETRY-5](#retry-5), [RETRY-16](#retry-16), [RETRY-17](#retry-17), [PERF-13](perf.md#perf-13).

</details>

<a id="retry-16"></a>

## RETRY-16 Every bypass of a rate limiter must obey server pushback, through a bounded cooldown that keeps debiting

**Takeaway:** If some requests bypass your rate limiter, they must also obey the server pushback the limiter obeys. Make that pushback a bounded cooldown (`max(Retry-After, floor)`), not a permanent latch, keep debiting the capacity cache for requests sent during the cooldown, apply the pushback before deciding retry or fail, and log once per cooldown window.

<details>
<summary>The full story (19/N commit 99a4a93d; was 3.18)</summary>

- **Situation:** A Retry-After moves the shared `_next_create_at` cursor ([RETRY-6](#retry-6)), but warm-covered creates never read `_next_create_at` ([RETRY-15](#retry-15)).
- **What goes wrong:** Without a throttle, covered creates keep bursting into an apiserver that is returning 429. The first fix, `WarmCredit.throttle()` setting `self.throttled = True` for the life of the `SandboxBatch`, overcorrected: a brief early APF 429 burst on GKE disabled warm credit for the rest of a large step and for every later training step on a long-lived batch (1280 / 50 = 25.6 s expected, 24.59 s measured; 2560 / 50 = 51.2 s expected, 47.40 s measured), even after the warm pools were back to 100% Ready. Simply expiring the flag after a cooldown is not enough either: creates sent during the cooldown would leave the cached pre-429 `readyReplicas` untouched and over-credit cold claims once the cooldown ends. A per-create INFO log would flood the logs during a burst.
- **What we do:** `WarmCredit` keeps `_throttled_until = max(_throttled_until, monotonic() + max(retry_after or 0.0, 5.0))` (`BATCH_WARM_CREDIT_THROTTLE_SECONDS`). While `_is_throttled()` holds, `take()` returns `False` and `refresh_due()` suppresses `_refresh_warm_credit`. Each paced create calls `spend(warmpool)`, so creates sent during a cooldown still consume cached credit. `SandboxBatch._create_one` (both shells) calls `self._warm_credit.throttle(retry_after)` under `_lock` on every 429 before outcome handling, so a final-attempt 429 that FAILs still starts a cooldown. `throttle()` returns whether this call opened a new window, and only that call logs at INFO.
- **Example:**
  ```python
  if isinstance(e, ApiException) and e.status == 429:
      with self._lock:
          first_throttle = self._warm_credit.throttle(retry_after)
      if first_throttle:
          logger.info("... temporarily pacing creates at create_rps")
  ```
  Test: `test_first_429_paces_creates_during_cooldown_and_recovers` shows paced gaps of at least 0.18 s after a 429 and unpaced gaps after a 503.
- **Related:** [RETRY-6](#retry-6), [RETRY-15](#retry-15), [RETRY-17](#retry-17).

</details>

<a id="retry-17"></a>

## RETRY-17 Don't pay an RPC to bypass a limiter whose next slot is already open, and put every on-demand read under the concurrency bound

**Takeaway:** Never spend a request to skip a rate limiter whose next slot is already free. Route every background or on-demand metadata read through the same concurrency semaphore as the main work, so fan-out across many keys keeps connection-pool and server headroom.

<details>
<summary>The full story (19/N; was 3.19)</summary>

- **Situation:** `acquire()` called `_refresh_warm_credit(warmpool, deadline)` whenever cached credit hit 0 and 1 s had passed since the last read. Both `_check_pool` (first-use WarmPool and SandboxTemplate GETs) and `_refresh_warm_credit` ran outside `self._in_flight`, the `BoundedSemaphore(max_in_flight)` that also bounds creates and deletes.
- **What goes wrong:** When `now >= self._next_create_at` (at step start, at streaming rates below `create_rps`, or with a high `create_rps`), `_reserve_create_slot(now)` already dispatches in 0 ms, so a blocking WarmPool GET saves nothing while doubling API calls per acquire (2.01 requests per env on 500 distinct pools). Worse, 500 concurrent `acquire()` calls across 500 pools fired up to 998 unbounded GETs at once. That broke 17/N's `pool_size >= max_in_flight + 2` invariant ([RETRY-19](#retry-19)), which keeps room for the watch and Lease renewal, and flooded APF seats into 429s.
- **What we do:** In `SandboxBatch.acquire` (and the async mirror), the refresh runs only when `not covered and not refreshed and now < deadline and self._next_create_at > now and self._warm_credit.refresh_due(warmpool)`, decided in the same `_lock` section as `take()`. At most one refresh per acquire, then the decision re-runs. Both `_check_pool` and `_refresh_warm_credit` acquire `self._in_flight` bounded by the deadline around their HTTP GETs. If `_check_pool` times out before it gets its slot, it cancels the shared check future so a waiting follower takes over the check cleanly.
- **Example:**
  ```python
  need_refresh = (not covered and not refreshed and now < deadline
                  and self._next_create_at > now
                  and self._warm_credit.refresh_due(warmpool))
  ```
- **Related:** [RETRY-15](#retry-15), [RETRY-16](#retry-16), [RETRY-7](#retry-7), [RETRY-19](#retry-19), [PERF-13](perf.md#perf-13), [BP-27](../best-practices.md#bp-27).

</details>

<a id="retry-18"></a>

## RETRY-18 Time out a shared operation by how its result is shared, not by the leader's deadline

**Takeaway:** Choose a shared operation's timeout by how its result is shared. If one failure is broadcast or cached for everyone, don't let one participant's short deadline decide it. Bound it by the operation's own budget and re-check the caller's deadline afterward.

<details>
<summary>The full story (16/N commit dc9ea439, 19/N commit 2ecdf384)</summary>

- **Situation:** The first `acquire()` on a pool this batch hasn't seen leads the shared first-use check in `SandboxBatch._check_pool`, which calls `_precheck_pools` (the WarmPool and SandboxTemplate GETs). A success is cached for the life of the batch, and a failure goes to every follower waiting on the same future. The warm-credit refresh ([RETRY-17](#retry-17)) is different: its failure only sets a 1 s negative cache.
- **What goes wrong:** Capping the leader's GETs by its own remaining time, as the refresh does, would let a `timeout=0.05` leader set a `ReadTimeoutError` on the shared future and fail followers that had 600 s left.
- **What we do:** `sandbox_batch.py: _precheck_pools` always uses `timeout = batch_utils.BATCH_REQUEST_TIMEOUT_SECONDS` (30 s, [RETRY-8](#retry-8)). The review in audit_19n_25n_rl_review.md (note under P2-SDK-2) confirmed this is intentional. The cost is that the leader may return after its own deadline, so `acquire()` then raises `BatchTimeoutError` before it takes an ordinal or warm credit ([RETRY-7](#retry-7)).
- **Example:**
  ```text
  leader timeout=0.05, follower timeout=600, pool check takes 0.08 s
  -> check succeeds and is cached for both
  -> leader raises BatchTimeoutError with no POST and no ordinal taken
  -> follower continues
  ```
  Test: `test_an_acquire_waiting_on_another_acquires_pool_check_stops_at_its_timeout` (the pool-d case, which covers the leader side; it has no follower).
- **Related:** [RETRY-7](#retry-7), [RETRY-8](#retry-8), [RETRY-17](#retry-17), [LOCK-15](lock.md#lock-15).

</details>

<a id="retry-19"></a>

## RETRY-19 Size the connection pool per batch (max_in_flight + 2), not as a cross-batch reservation

**Takeaway:** Reserve connection capacity for steady-state holders (streams, keepalives) and treat bursty workers as transient. Validate that a single unit is consistent on its own rather than summing worst-case bursts across units the check can't fully see anyway.

<details>
<summary>The full story (17/N commit 6feedc5c)</summary>

- **Situation:** A batch runs up to `max_in_flight` creates, its watch holds one streaming connection, and Lease renewal needs one more. When the pool is full, aiohttp queues requests, so a renewal can wait behind creates long enough for the Lease to expire ([RETRY-9](#retry-9)). urllib3 instead opens one-off connections that each pay a new handshake (the `batch_utils` comment).
- **What goes wrong:** CodeRabbit suggested reserving `max_in_flight + 2` per client for every open batch. That would treat short create bursts as permanent. On a default pool of 100 it would clamp a 5th batch to 10 and reject a 6th, while 4 idle batches really hold only 4 watch connections (review reply in handoff/work/replies_2153.md).
- **What we do:** `batch_utils.check_pool_size(pool_size, max_in_flight, explicit=...)`, called from `claim_batch` in `sandbox_client.py` and `async_sandbox_client.py` with the pool size from `api_pool_size` in `k8s_helper.py` and `async_k8s_helper.py`, checks one batch on its own against `max_in_flight + BATCH_POOL_HEADROOM` (2). An omitted `max_in_flight` is clamped to `pool_size - 2` with an INFO log. It raises `ValueError` only for an explicit value that doesn't fit, or a pool too small to leave one create slot (under 3). A pool size of `None` means unbounded and passes, and a configured 0 maps to unbounded for both urllib3 (`LifoQueue(0)`) and aiohttp. Users who run several batches on one client size `pool_size` for the combined load.
- **Example:**
  ```text
  pool_size 10, max_in_flight omitted (default 20) -> clamped to 8, INFO log
  pool_size 10, max_in_flight=20 explicit          -> ValueError: needs a pool of at least 22
  pool_size 2                                      -> ValueError
  ```
- **Related:** [RETRY-9](#retry-9), [RETRY-17](#retry-17), [PERF-17](perf.md#perf-17). [PERF-16](perf.md#perf-16) covers the default pool composition and the 0-means-unbounded validator gap.

</details>

---

[Learnings home](../../learnings.md) · [Field guide index](../../learnings.md#sec-field-guide)
