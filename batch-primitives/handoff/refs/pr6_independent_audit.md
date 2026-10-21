# PR 6 (acquire) independent audit

Reviewed `9eefcd8..origin/feat/batch-6-acquire-nbbp5j` at 4d08dd8, against the Membership section of `batch_claim_proposal.md` on `batch-primitives-notes`. Read-only, nothing pushed. Paths are under `clients/python/agentic-sandbox-client/k8s_agent_sandbox/` and line numbers are the sync file's. The async file has the same code at slightly different lines. I wrote the findings below before reading `pr6_review_answers.md` or the PR 6 thread. The comparison is at the end.

## Summary

One real bug. Once the fill deadline has passed, every `acquire()` busy-spins while it waits for its member, burning a CPU core and churning the batch lock. That's exactly the rolling-replacement case acquire exists for. The implementing thread's audit missed it. Two medium edge cases follow. acquire can create a claim it has already run out of time for (the implementing thread found this one too). It can also start a create after the Lease has expired. Everything else is small. The new state the PR adds (the next-ordinal annotation, the create-rps annotation, `_checked_pools`, `take_claim_name`) is all needed, and I'd keep the design choices Brian asked about.

## Findings, ranked

### 1. acquire busy-spins after the fill deadline (high)

`_next` wakes at whichever comes first, the fill deadline or acquire's own deadline (`sandbox_batch.py:539-541`, async `:552-555`). `_fill_deadline` is never cleared once it passes. After that, `wake_at` is in the past, so the timeout is 0 and `Condition.wait(0)` returns at once. The loop then runs flat out until the member is Ready or the timeout ends. This hits every acquire made more than `quorum_timeout` after the last fill create, and every acquire on a handle from `get_batch` more than `quorum_timeout` after attach (`:377`). Before PR 6 nothing waited past the fill deadline, because the fill consumers are all done by then. I reproduced the loop logic on its own, and it did about 120,000 iterations in 0.2 s. Forty concurrent acquires would mean forty spinning threads fighting the watch thread for the GIL and the lock. In async, it starves the event loop. No test catches it, because every acquire test runs before the fill deadline.

**Recommendation: fix in PR 6.** Leave the fill deadline out of `wake_at` once it has passed, and add one regression test that acquires after the fill deadline and counts the waits. It's a two-line change in each handle.

### 2. acquire creates a claim it has already run out of time for (medium)

The deadline starts when acquire is called (`:582-583`), but the create waits for its pacing slot first (`:596`) and may wait for retry slots too (`:1196-1200`). If the slot comes after the deadline, acquire still creates the claim, then times out at once and deletes it. That costs a wasted create and delete, and a pacing slot another create could have used. It happens with many concurrent acquires at a low `create_rps`.

**Recommendation: fix.** Raise `BatchTimeoutError` before creating when the slot is at or past the deadline. The implementing thread proposes the same fix and I agree with it. I'd also apply the check before each retry, since a retry slot can pass the deadline the same way.

### 3. acquire can start a create after the Lease has expired (medium-low)

acquire checks `err()` only before it takes its slot (`:589`). The fill workers check it again after the slot wait, so they never start a create once the handle has lost its Lease (`:1101-1106`). acquire doesn't, so a create can go out after `err()` is set. The proposal's Liveness section says the driver "stops starting new claim creates" once the Lease expires. The claim is deleted right after, because `_next` returns on the error, but by then the reaper could already be acting on the batch.

**Recommendation: fix.** Re-check `err()` under the lock after the slot wait, before creating, as `_create_worker` does. It's three lines in each handle.

### 4. Releasing a pending acquired member makes acquire wait out its whole timeout (low)

`members()` lists a pending acquired member, so a caller can pass it to `release_member` while acquire is still waiting. `release_member` removes it from `_members`. Then `acquired_member` returns `None` ("pending") forever (`batch_state.py:376-387`), and acquire only fails at its timeout, with a misleading `BatchTimeoutError`.

**Recommendation: fix, small.** `acquired_member` should raise a `BatchError` when the name is in `_released`. A claim deleted before the watch ever saw it has the same symptom (`mark_lost` ignores unknown names, `:256-263`), but it's rare and the timeout bounds it, so I'd leave that case alone.

### 5. Infinite values pass validation (low)

`positive_number` accepts `inf` (`batch_utils.py:216-226`). `claim_batch(create_rps=float("inf"))` works, but it writes `"inf"` to the Lease, and `get_batch` then rejects it (`batch_utils.py:389`), so the batch can't be re-attached. `acquire(timeout=float("inf"))` raises `OverflowError` from `Condition.wait` while no fill deadline is set (I checked that `Condition.wait(inf)` raises).

**Recommendation: fix.** Require a finite value in `positive_number` with `math.isfinite`, which also makes claim_batch and get_batch agree.

### 6. The `stopped` branch in acquire is redundant (low, simplification)

`_stop_creating` is only set by `_stop_background_threads` (`:810`). detach and release call that only after setting `_detached` under the lock. So whenever `stopped` is true, falling through to `_next` already raises "has been detached or released" via `_check_active`. The extra `if stopped: raise BatchError("... stopped creating claims")` (`:614-615`) and the docstring's "stops creating claims" clause can go. It makes acquire a little shorter, which bears on Brian's question 8.

**Recommendation: simplify.** No behavior change apart from the error message.

### 7. The README still says every claim gets the same shutdownTime (low, docs)

`README.md:751` reads "Every claim gets the same `shutdownTime`". That's no longer true now that acquired claims get their own.

**Recommendation: change it to "Every initial claim"**, the minimal wording change.

### 8. Accepted risks, no change recommended

- **Async double cancellation.** acquire's cleanup awaits the lock and the delete inside `except BaseException` (async `:638-655`). A second cancellation (for example a TaskGroup shutting down) can skip the delete. `release()` deletes by label anyway, and `shutdownTime` backstops it, so the gap is small.
- **Precheck errors other than 404.** A 403 or 5xx from the pool or template GET propagates without a retry and isn't in acquire's Raises list (`:1207-1233`). claim_batch already behaves this way, so changing it would be a separate PR.
- **Lease labels on re-attach.** A re-attached handle copies every Lease label except the batch-id and created-by labels onto acquired claims (`batch_utils.py:403`). If someone labels the Lease by hand, those labels spread to new claims. That's unlikely, and filtering would need its own rule.

## What's needed, item by item (Brian's question 12)

- **The next-ordinal annotation** is needed. Without it, a re-attached handle could only continue past the highest ordinal it still sees. It could then reuse the name of a claim that is fully gone while that claim's cold-started Sandbox, which shares the name, is still terminating. The proposal requires it (Batch Model item 3, Membership).
- **The create-rps annotation and the stored labels** are needed so a re-attached handle's acquire paces and labels like the original (approved decision).
- **A max-in-flight annotation** isn't needed. `max_in_flight` only sets how many fill workers start (`:290`). acquire never uses it, and a re-attached handle has no fill.
- **The `_checked_pools` cache** is needed. The precheck matters because `derive_member` treats WarmPoolNotFound and TemplateNotFound as non-terminal (`batch_state.py:149`), so a mistyped pool would otherwise hang until the timeout. The cache saves two GETs on every acquire after the first.
- **`take_claim_name`** is fine to keep, since it keeps the ordinal counter inside the state. `continue_ordinals_from` is already gone at 4d08dd8.
- **The ordinal bump in `upsert_claim` (`batch_state.py:240-241`)** is needed for a re-attach without a Lease, which `get_batch` allows when claims remain (`sandbox_batch.py:311`).
- **Removing the `size < 1` check** is safe. `BatchGroup.size` is `Field(ge=0)` (`models.py:236`), so negative sizes are still rejected.
- **Raising, the error types and the timeout parameter.** I'd keep all three. Every error but `ValueError` is a `SandboxError`, and every runtime one is a `BatchError`, so one `except BatchError` handles failures. acquire runs after the fill and has nothing else bounding its wait, which is why it needs its own timeout. The proposal's RL example passes one (`batch_claim_proposal.md:693`).
- **Tests** follow the one-owner-per-contract rule. The failure table in `TestAcquire` is a good use of subTests. The gap is finding 1, since no test acquires after the fill deadline.

## Where I agree and disagree with the implementing thread

- **Agree** on items 1, 2, 3, 4, 6, 7, 9, 10 and 11, with the same reasons I reached on my own.
- **Agree** with its audit item 1 (fail before creating once the slot is past the deadline). I'd extend the check to retry slots (finding 2).
- **Agree** with its audit item 2 (a re-attached handle drops an unused size-0 group) and its audit item 3 (templates aren't cached across calls). Both are low impact, and documenting is enough.
- **Disagree** with its answer to item 5 and its audit item 4 ("nothing else"). The new wake logic is what causes the busy spin in finding 1, because a fill deadline that has passed still sets the wake time. Its answer to item 7 says acquire "has no fill deadline", but the handle's fill deadline does apply to acquire's wait, through `_next`.
- **Not covered there**: findings 3 to 7.

## Decisions for Brian

1. Fix the busy spin in PR 6 (finding 1)? **Recommend yes.** It's a real CPU bug in acquire's main use.
2. Fail before creating when the slot or a retry slot is past the deadline (finding 2)? **Recommend yes.**
3. Re-check `err()` before acquire's create (finding 3)? **Recommend yes.**
4. Make acquire fail at once when its pending member is released (finding 4)? **Recommend yes.**
5. Reject `inf` in `positive_number` (finding 5)? **Recommend yes.**
6. Drop the redundant `stopped` branch and fix the README line (findings 6 and 7)? **Recommend yes.** Neither changes behavior.
