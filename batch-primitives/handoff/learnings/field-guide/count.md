<a id="sec-count"></a>

[Learnings home](../../learnings.md) · [Field guide index](../../learnings.md#sec-field-guide)

# Completion and accounting: when is the batch done?

A batch asks for N claims, and callers need to know when that fill is over: every slot is Ready, failed, lost, released, or will never be created. This section covers how the SDK keeps that count, how it decides group and quorum outcomes, and when the fill deadline starts. One idea ties it together: completion is a one-way latch built from final, de-duplicated decisions, never from live health numbers that can go up and down. The early entries give the general rules (latches, idempotency, named vs. anonymous counts). The later ones apply them to group outcomes, quorum, and deadlines.

<a id="count-1"></a>

## COUNT-1 Completion is a one-way latch, built from final decisions, not live gauges

**Takeaway:** Decide "is the stream done?" from a sticky set of items whose final fate is known. Never sum a live health counter that can drop after you already handed an item out.

<details>
<summary>The full story (13/N)</summary>

- **Situation:** `fill_settled()` tells `events()` and the group and quorum consumers when every fill slot has a final outcome. Originally (in the pre-merge 13/N design and drafts; no merged commit has this form) it checked `_ready_count + len(_unable) + _skipped_creates == _fill_total`, and `_ready_count` rose and fell with each member's live Ready status.
- **What goes wrong:** A Ready member gets handed out (or dropped with a failed group). Then a watch event (a streamed change notification from the apiserver) flips it NotReady. `_ready_count` drops and a finished `events()` stream becomes unfinished again. The opposite bug also existed: counting a member as settled on bare Ready, before its group or quorum decided, ended `events()` early if a held member flickered NotReady and later recovered.
- **What we do:** Settlement uses a sticky `_resolved` set. A fill claim joins it only once its consumer fate is final: handed out, dropped with a failed group or quorum, unable (failed or lost), or released (13/N, `8ad5a407`). At 23/N the predicate is `batch_state.py: BatchState.fill_settled`, which returns `self._fill_expired or len(self._resolved) + self._never_created >= self.size`. Nothing ever removes a name from `_resolved`.
- **Example:**
  ```text
  t0 b-0 Ready, handed out   -> _resolved = {b-0}
  t1 b-0 NotReady on watch   -> _resolved unchanged, stream stays settled
  ```
- **Related:** [COUNT-2](#count-2), [COUNT-3](#count-3), [COUNT-4](#count-4), [COUNT-6](#count-6) (the one deliberate exception), [PAT-5](../patterns.md#pat-5).

</details>

<a id="count-2"></a>

## COUNT-2 Applying the same observation twice must change nothing

**Takeaway:** Design reconcile transitions so that the same fact, delivered twice or through two different paths, gives the same state and emits nothing new. Keep facts that exist only on the client sticky against "absent from the server" diffs.

<details>
<summary>The full story (4/N, 5/N, 13/N)</summary>

- **Situation:** The same "gone" fact can arrive several times. A claim gets a deletionTimestamp (it is terminating), then a DELETED watch event, then it is missing from every later re-list (a fresh full LIST, which the watch does after a 410 Gone error means its resourceVersion bookmark is too old). Members that failed with a synthetic `CreateFailed` exist only locally, so every re-list reports them as missing.
- **What goes wrong:** A non-idempotent `mark_lost` would double-count a group's losses, emit duplicate `MEMBER_LOST` events, unsettle accounting, or turn a `CreateFailed` member into LOST on the first 410 and erase why it failed.
- **What we do:** `batch_state.py: BatchState.mark_lost` keeps a FAILED member FAILED (with its reason) and only clears its endpoints (4/N made FAILED sticky). A member already LOST maps to an equal LOST member. `BatchState._on_fill_change` counts into `_lost` or `_failed` and queues `MEMBER_LOST` or `MEMBER_FAILED` only when the claim isn't already in the sticky `_unable` set (13/N). `derive_member` already gives a terminating claim exactly the state `mark_lost` would (5/N made terminating mean LOST), so the DELETED that follows changes nothing. Tests: `test_failed_member_missing_from_a_relist_stays_failed`, `test_terminating_claim_counts_as_deleted`.
- **Example:**
  ```text
  t0 record_create_failure(b1-3) -> FAILED/CreateFailed, _failed += 1, MEMBER_FAILED
  t1 410, re-list, b1-3 absent   -> mark_lost -> still FAILED/CreateFailed
                                    no new event, _failed unchanged
  ```
- **Related:** [COUNT-3](#count-3). The re-list diff itself is in [WATCH-3](watch.md#watch-3).

</details>

<a id="count-3"></a>

## COUNT-3 Once you report an item as failed, keep it counted as failed

**Takeaway:** After telling a consumer an item failed, latch its accounting even if reality later improves, and expose the live state through a separate snapshot API. Staying consistent with what callers were already told beats an optimistic recount.

<details>
<summary>The full story (13/N, 15/N)</summary>

- **Situation:** A fill member can become FAILED or LOST, or get a synthetic `CreateFailed` after a create timed out on the client even though the object landed. Later the watch may show that same claim Ready.
- **What goes wrong:** Un-counting it would contradict a `QuorumUnreachableError` or `MEMBER_FAILED` the caller already received, or reopen a finished `events()` stream.
- **What we do:** `batch_state.py: BatchState._unable` gains each name once and never loses it. `_on_fill_change` and `release_claim` check it, and `record_create_failure` goes through `_on_fill_change`. Once a name is in `_unable`, `_on_fill_change` skips its `MEMBER_READY` routing and emits no second status event, so FAILED then DELETED yields only `MEMBER_FAILED`. `members()` still shows the claim's real state. The trade-off: such a claim can be live and Ready yet never handed out. `release()` or the claim's shutdownTime cleans it up. Tests: `test_status_events` ("terminal then deleted"), `test_released_members_leave_members_count_as_unable_and_ignore_later_events`.
- **Example:**
  ```text
  t0 record_create_failure(b-1) -> MEMBER_FAILED, failed = 1
  t1 watch shows b-1 Ready      -> no MEMBER_READY; members() shows READY
  ```
- **Related:** [COUNT-1](#count-1), [COUNT-2](#count-2), [COUNT-7](#count-7).

</details>

<a id="count-4"></a>

## COUNT-4 Count work with a name in a set, and work that will never get a name in a counter

**Takeaway:** Split completion accounting into a named set (de-duplicated by identity) and an anonymous counter for slots that will never produce an identity. Add the two to decide completion.

<details>
<summary>The full story (13/N, 19/N)</summary>

- **Situation:** Some fill slots will never have a claim name to track: creates skipped because their group or the quorum failed, and claims missing when a new handle re-attaches with `get_batch`.
- **What goes wrong:** If settlement only counted named members, it would never reach `size`, and `events()` would hang until the deadline.
- **What we do:** `batch_state.py: BatchState._never_created` counts those nameless slots. The older entry called this counter `_skipped_creates` (a pre-merge name; every merged commit since 13/N uses `_never_created`). At 23/N it is `_never_created`, fed both by `record_skipped_create` and by `count_missing_fill_as_unable` (re-attach), so both sources add up in one settlement term. Only the per-pool `_missing` count records which part can be taken back ([COUNT-6](#count-6)). On the named side, `_unable` and `_dispatched` overlap, so one `_resolved` set keeps `len()` O(1) without double counting. `fill_settled` uses `>=` rather than `==`.
- **Example:**
  ```python
  return self._fill_expired or len(self._resolved) + self._never_created >= self.size
  ```
- **Related:** [COUNT-1](#count-1), [COUNT-5](#count-5), [COUNT-6](#count-6).

</details>

<a id="count-5"></a>

## COUNT-5 Only work that never left the client counts as "never created"

**Takeaway:** A "skipped" counter may include only items that never hit the wire. Once a request is sent, the server may already hold the object, so wait for the response or the watch to settle it.

<details>
<summary>The full story (19/N)</summary>

- **Situation:** Once a group can't reach `min_ready` (`group_failed`), `_create_worker` skips that group's remaining fill creates and calls `record_skipped_create()` so `fill_settled()` knows those slots will never arrive. A group can fail while one of its creates sits between retry attempts.
- **What goes wrong:** In a 19/N draft, after the check moved into `can_send` (which runs before every attempt), it ran before retries as well as before attempt 1 (before 19/N it ran only once, before attempt 1). Attempt 1 could have stored the object on the apiserver and then timed out on the client. The create was counted as skipped, and then the claim arrived on the watch and joined `_resolved`, so one slot counted twice.
- **What we do:** `sandbox_batch.py: SandboxBatch._create_worker.can_send` (and the async shell's twin) skips and calls `record_skipped_create` only when `sent == 0`. Creates that already sent an attempt finish their retries and settle through the watch or `record_create_failure` (19/N, `a2a19cfa`).
- **Example:**
  ```text
  t0 attempt 1 sent, client times out (object did land)
  t1 group fails
  t2 old: skipped += 1, then watch adds b-4 to _resolved -> counted twice
     new: sent == 1, keep retrying, the watch settles b-4 once
  ```
- **Related:** [COUNT-4](#count-4), [RACE-3](race.md#race-3). How a terminal create failure yields to watch evidence is in [RACE-2](race.md#race-2).

</details>

<a id="count-6"></a>

## COUNT-6 Pessimistic placeholders must be reversible counts, separate from named outcomes

**Takeaway:** When recovery assumes missing items are lost, track those assumed losses as a reversible count, separate from confirmed, named losses. A late arrival takes its placeholder back. Say clearly that completion can reopen, and keep verdicts you already published unchanged.

<details>
<summary>The full story (13/N)</summary>

- **Situation:** At `get_batch` attach, the new handle counts each pool's missing fill claims as `group.size - (fill members seen in that pool)`. This is a count, with no ordinal test and no names. `batch_state.py: BatchState.count_missing_fill_as_unable` adds it to `_missing`, `_lost` and `_never_created`. (The older entry described this as "ordinals below a pool's highest listed ordinal"; the 23/N code does not look at ordinals.) The previous handle stopped creating before it detached, so a missing claim can still appear only if a create that timed out landed anyway.
- **What goes wrong:** Such a create lands right after the attach list. Before the fix, `upsert_claim` added the new member without taking back the missing count, so one slot counted twice.
- **What we do:** `BatchState.upsert_claim`, on the first sighting (`previous is None`) of a fill claim in a pool with `_missing > 0`, decrements `_missing`, `_lost` and `_never_created`, then counts the claim by its real state, or as released if fill creates for its pool were stopped (13/N, `8ad5a407`; the released case came with 15/N). Missing slots have no names, so unlike `_unable` the count can go back down. This means `fill_settled()` can turn False again: an intentional exception to [COUNT-1](#count-1)'s one-way latch. Group outcomes already decided stay as they are ([COUNT-9](#count-9)). Tests: `TestReattachedFill.test_a_missing_claim_that_arrives_later_takes_back_its_missing_count`, `TestRelease.test_a_missing_claim_released_when_it_lands_late_takes_back_its_missing_count`.
- **Example:**
  ```text
  t0 size=3, attach sees b-0 -> missing=2, fill settles
  t1 b-2 arrives PENDING     -> missing=1, fill_settled() is False again
  t2 b-2 Ready               -> MEMBER_READY, settled again
  ```
- **Related:** [COUNT-1](#count-1), [COUNT-4](#count-4), [COUNT-9](#count-9).

</details>

<a id="count-7"></a>

## COUNT-7 Treat a release as a terminal outcome: count it once, then re-decide at once

**Takeaway:** Explicit cancellation is a final outcome for completion accounting. Count it once, idempotently against other final states, and re-run any outcome decision right away instead of waiting for a timeout.

<details>
<summary>The full story (15/N)</summary>

- **Situation:** The caller releases a fill member (`release_member`, `release_group`, `release_not_ready`) while a GROUP or QUORUM consumer waits.
- **What goes wrong:** If the release isn't counted, `fill_settled()` never finishes. If a member already FAILED or LOST is counted again, it counts twice. If outcomes aren't re-decided, a group that can no longer succeed stays undecided until the deadline.
- **What we do:** `batch_state.py: BatchState.release_claim` counts only fill claims (not acquire claims, not non-fill claims). It skips a claim already in `_unable`. Otherwise it adds the claim to `_unable` and `_resolved`, bumps the pool's `_released_count` (reported in `QuorumUnreachableError`), removes it from `_waiting`, and immediately calls `_decide_group_outcome` (GROUP mode) or `_check_quorum` (QUORUM mode). A released fill member emits no `MEMBER_LOST`. Test: `test_released_fill_members_count_toward_settling_the_fill`.
- **Related:** [COUNT-3](#count-3), [COUNT-8](#count-8), [COUNT-9](#count-9).

</details>

<a id="count-8"></a>

## COUNT-8 Classify a release you haven't observed yet from the creator's own record

**Takeaway:** When a release can happen before you've seen the object, classify it from the creator's local registration, not from the missing observation. Keep that registration until every classifier has run.

<details>
<summary>The full story (16/N)</summary>

- **Situation:** `acquire()` can target a pool that is also a fill group. `release_group` can release the acquired claim before the watch has delivered any object for it, so `member is None`.
- **What goes wrong:** With no member to inspect, an unseen claim in a group pool looks exactly like a fill create the caller stopped. Counting it would add to `_unable` and `_released_count` and could fail a group or quorum that never included it.
- **What we do:** `batch_state.py: BatchState.release_claim` computes `fill = claim_name not in self._acquiring and warmpool in self._group_by_pool and (member is None or self._is_fill(...))`. `release_member` pops `_acquiring` only after calling `release_claim`, so the classification still sees the entry. `acquired_member` pops `_acquiring` once the claim is READY. After that the claim's lack of fill annotations keeps it out of the fill. Test: `TestAcquired.test_outcomes_drop_and_deletes` ("An acquired claim in a fill pool is never part of the fill").
- **Related:** [COUNT-7](#count-7).

</details>

<a id="count-9"></a>

## COUNT-9 Decide each group outcome once, and freeze its numbers at that moment

**Takeaway:** Make per-key verdicts write-once and snapshot their diagnostics when you decide. Check success before failure, so an item that meets both resolves in the caller's favor.

<details>
<summary>The full story (13/N, 15/N)</summary>

- **Situation:** `_on_fill_change`, `release_claim` and `set_mode` all call `_decide_group_outcome` on every relevant change.
- **What goes wrong:** Re-deciding would let a group succeed after the caller was told it was unreachable, or change the counts in a `QuorumUnreachableError` the caller already holds.
- **What we do:** `batch_state.py: BatchState._decide_group_outcome` returns early with `if pool in self._group_outcomes: return`. The first decision wins: success if `len(waiting) >= min_ready` (checked before unreachability), otherwise a `QuorumUnreachableError` with the failed, lost and released counts copied at that moment. `_time_out_undecided_groups` gives only undecided groups a `BatchTimeoutError` (a subclass of both `BatchError` and `TimeoutError`). After a success, later Ready members of that group stream straight to the caller. After a failure, they're dropped and counted in `_resolved`. Tests: `test_release_group_releases_its_pool_and_any_fill_claim_that_lands_later` ("Decided at the first release"), `test_expire_fill_times_out_undecided_groups_only`.
- **Related:** [COUNT-6](#count-6), [COUNT-7](#count-7), [COUNT-10](#count-10).

</details>

<a id="count-10"></a>

## COUNT-10 A failed quorum fails every group, but status events keep flowing

**Takeaway:** When an all-or-nothing barrier fails, spread the failure to every participant: stop hand-outs and upstream work batch-wide. Keep diagnostic status events flowing, and count dropped items as settled.

<details>
<summary>The full story (14/N)</summary>

- **Situation:** `wait_for_quorum` is an all-or-nothing barrier for joint-batch RL (reinforcement learning), which has no use for a partial mix of groups.
- **What goes wrong:** If healthy groups keep streaming members or creating claims after the barrier fails, they spend capacity on results nobody can use.
- **What we do:** `batch_state.py: BatchState.fail_quorum` records the error, moves every held member into `_resolved`, and clears `_waiting`. `_route_ready_member` drops later Ready members in QUORUM mode but counts them as resolved, so settlement still completes. `group_failed` returns True for every pool, so creators skip unsent creates and count them through `record_skipped_create` ([COUNT-5](#count-5)). `MEMBER_FAILED` and `MEMBER_LOST` events still reach `events()`, because they aren't hand-outs. The first decision wins: later `fail_quorum` calls do nothing. Tests: `test_an_unreachable_group_fails_the_quorum_for_every_group` (`test_batch_state.py`), `test_an_unreachable_group_raises_and_stops_creates_for_every_group` (`test_sandbox_batch.py`).
- **Related:** [COUNT-5](#count-5), [COUNT-9](#count-9). Why any exit from the one-shot wait fails the quorum is in [CANCEL-7](cancel.md#cancel-7).

</details>

<a id="count-11"></a>

## COUNT-11 Decide the empty case when someone subscribes

**Takeaway:** An event-driven check needs one explicit evaluation at subscribe time for the empty (vacuous) case. If no future event can ever trigger it, decide right away.

<details>
<summary>The full story (14/N, 16/N)</summary>

- **Situation:** 16/N allows `size=0` groups (pools used only by `acquire()`). After subscribe, quorum decisions run only when a fill member changes (a watch event or a release).
- **What goes wrong:** With no fill groups at all, no watch event would ever call `_check_quorum`, so `wait_for_quorum` would block until `err()` or detach.
- **What we do:** `BatchState.__init__` leaves size-0 groups out of `_group_by_pool`, so the consumers skip them: no empty `GroupOutcome` and no timeout. `batch_state.py: BatchState.set_mode` sets `self._quorum_members = []` (quorum reached, nobody in it) when `_group_by_pool` is empty (16/N, `dc9ea439`). A group with `min_ready=0` qualifies at once in `_check_quorum` and `_decide_group_outcome`, because `len >= 0` (14/N). Tests: `TestSizeZeroGroups.test_quorum_consumers_skip_a_size_zero_group`, `test_reached_at_once_with_no_members_when_every_group_has_min_ready_zero`.
- **Example:**
  ```python
  if not self._group_by_pool:
      self._quorum_members = []
  ```
- **Related:** [COUNT-10](#count-10), [WATCH-6](watch.md#watch-6).

</details>

<a id="count-12"></a>

## COUNT-12 Start the fill deadline when the last create can go out, and set it on every path that empties the plan

**Takeaway:** Start a timeout from when the work could actually start, not from when it was queued. Every path that ends a queue (normal drain or cancellation) must set the completion deadline and wake the waiters blocked on it.

<details>
<summary>The full story (13/N, 15/N)</summary>

- **Situation:** Groups and the quorum time out `quorum_timeout` after the fill. But creates are paced at `create_rps`, so sending the fill can take up to `(N-1)/create_rps` seconds (less when a warm pool's credit covers some creates).
- **What goes wrong:** Counting from `claim_batch()` would time out groups whose claims are still waiting for a pacing slot. And if `release_group` or `release_not_ready` empties the plan, no worker ever pops the "last" entry, so the deadline is never set and consumers wait forever.
- **What we do:** In `sandbox_batch.py: SandboxBatch._create_worker` (and the async twin), the worker that pops the final planned entry sets `_fill_deadline = slot + quorum_timeout` and calls `notify_all()` on the `_changed` Condition (a lock plus a wait/notify queue). Until then waiters wait with no timeout (13/N, `8ad5a407`); a `get_batch` handle instead sets the deadline at attach. `_drop_planned_creates` sets the deadline from the last slot already reserved, `max(now, _next_create_at - _create_interval)`, when it empties the plan (15/N, `113fae28`). Test: `test_groups_yield_when_ready_and_time_out_after_the_last_create`.
- **Example:**
  ```text
  t0  claim_batch, 10 creates at 1 rps
  t9  worker pops the last create -> deadline = t9 + quorum_timeout, wake waiters
  alt t3 release_group empties the plan -> deadline = last reserved slot + quorum_timeout
  ```
- **Related:** [COUNT-13](#count-13), [RETRY-13](retry.md#retry-13). How the pacing cursor and released slots work is in [RETRY-5](retry.md#retry-5) and [RETRY-14](retry.md#retry-14).

</details>

<a id="count-13"></a>

## COUNT-13 Without a timer, check the deadline right before each irreversible step

**Takeaway:** If no timer thread enforces a deadline, evaluate time-based transitions at every decision point that depends on them, especially right before an irreversible send, not only when a consumer happens to wake.

<details>
<summary>The full story (13/N, 19/N)</summary>

- **Situation:** Nothing runs a timer for the fill deadline. Consumers check it when they wake.
- **What goes wrong:** A create still waiting for its pacing slot when the deadline passes would go out even though the quorum already failed. That makes a claim nobody will use.
- **What we do:** `sandbox_batch.py: SandboxBatch._create_worker.can_send` (and the async twin) calls `self._expire_fill_if_due()` under `_lock` before checking `group_failed`. The expiry, and with it any group or quorum failure, is decided at the moment a create would go out. The pinned test `test_quorum_timeout_raises_batch_timeout_error_and_stops_the_remaining_creates` runs at `create_rps=1` with `quorum_timeout=60`, jumps the clock +62 s, and checks that `b1-1` is never created and `_never_created == 1`.
- **Related:** [COUNT-5](#count-5), [COUNT-12](#count-12), [COUNT-14](#count-14).

</details>

<a id="count-14"></a>

## COUNT-14 In a consumer loop: refuse if torn down, apply time, drain, then decide to end

**Takeaway:** Order a consumer loop's checks as: refuse if torn down, apply time-based transitions, drain what is already decided, then decide whether to end. That way a stream ends only after its last real item.

<details>
<summary>The full story (13/N)</summary>

- **Situation:** Each time the consumer generator wakes, it must choose between yielding, ending, and waiting.
- **What goes wrong:** Checking `done()` or `err()` before `pop()` would drop events already queued, such as a `MEMBER_FAILED` that explains why the batch stopped. Checking `pop()` before liveness would keep handing out members after `detach()` or `release()`.
- **What we do:** `sandbox_batch.py: SandboxBatch._next` and `async_sandbox_batch.py: AsyncSandboxBatch._next` run, in order: `_check_active` (raises after detach or release), `_expire_fill_if_due`, `pop()`, `done() or err()`, then wait. Because `pop()` runs before `done()`, `MEMBER_READY` entries that `pop_event` skips lazily (released members) are drained before `events_done` looks at the queue.
- **Example:**
  ```text
  loop:
    _check_active()        # torn down? raise
    _expire_fill_if_due()  # time-based transitions
    item = pop()           # drain decided items first
    if item: yield item; continue
    if done() or err(): return
    wait()
  ```
- **Related:** [COUNT-1](#count-1), [COUNT-13](#count-13).

</details>

---

[Learnings home](../../learnings.md) · [Field guide index](../../learnings.md#sec-field-guide)
