# 12/N (#1824) round 3: answers

Answers to `temp-prompts/pr2_round3_review_prompt.md`. All four heads matched: PR 2 `4c67b14e`, PR 5 `2810ca65`, 7a `5f69ade8`, live `386b39fc`. The RL branch was read at `3bee2847`. Line numbers are in `sandbox_batch.py` at `4c67b14e` unless marked. "Verified" means read in the code. "Inferred" means reasoned, not run.

## Brian's decision

Brian, 2026-10-27 23:16 UTC, replying to these answers: "i agree with this."

That approves every recommendation in this file. It covers N1 as written: a blocking `RLock` per handle (an `asyncio.Lock` in async), held for all of `release()` and only around `detach()`'s Lease I/O, with `detach()` refusing once a release has started. It also covers the side effect: a `detach()` after a failed `release()` now raises `BatchError`. #4 (drop Lease creation) was settled earlier. Combined with round 2, the stack agent builds the fence (#1), no change for #2 and #3, the drop of the create (#4), N1, and the tests in section 6.

## Summary

| # | Answer | Needs Brian? |
| :-- | :-- | :-- |
| 1 | A plain `Lock` deadlocks only when the same thread calls in again, for example a signal handler that calls `detach()`. Nothing in the repo does that, but learnings 3.1 names exactly that case as the reason for 11/N. Serializing doesn't break the 11/N contract. | No |
| 2 | **`RLock`, blocking.** A non-blocking acquire breaks 11/N, because a second concurrent `detach()` would raise. A plain `Lock` adds a hang for the same code. | No |
| 3 | **Narrower than proposed.** `release()` holds the lock for its whole run. `detach()` holds it only around its Lease read and write, and refuses at once if a release has started. So `detach()` never waits minutes behind a release. | Approved with N1 |
| 4 | With all three changes, (a) to (h) all keep the rule. Two gaps remain: the read-to-delete gap in one round, in (d) and (e), and (h) when the loop runs on a thread that is still alive. | No |
| 5 | Minimal set: one lock, no new flag, the existing `self._lease`, and one widened return type. The holder check becomes redundant for SDK writers. Keep it. The flags can't replace the lock. | No |
| 6 | Five tests per shell plus one sync-only re-entry test. They change or replace three existing tests. | No |
| N1 | A side effect for your OK. A `detach()` after a `release()` that **failed** now raises instead of handing off a half-deleted batch. | Approved (see above) |

## 1. Deadlock claim

**Real only on same-thread re-entry (verified).**
- The only signal handler in the repo is the RL fleet's (`examples/agent-sandbox-rl/agent_sandbox_rl/fleet.py:358`). It deliberately doesn't tear down inside the handler. It raises `KeyboardInterrupt` or `SystemExit` and leaves teardown to `atexit` (`fleet.py:362-385`), so it never calls into the SDK from the handler.
- `atexit` handlers run on the main thread after its code has unwound, so they can't land inside a main-thread `release()` (Python behavior, inferred from the docs). A daemon thread can still be inside one. Then the hook waits, which is a delay and not a deadlock.
- The SDK has no signal handler. The README and docs don't mention one for the Python batch API.
- Learnings 3.1 (`handoff/learnings.md:80`) names "a signal handler and a `finally`" as the case behind 11/N. With a plain `Lock` held across `detach()`'s Lease I/O, that exact case would hang.

**11/N contract (verified).** The fix in `7cfe8484` handles a 409 by re-reading the Lease. Its test (`test_release_conflict_succeeds_only_once_the_lease_is_not_held`) runs on one thread with mocked responses. It needs both calls to succeed, not both calls to run unblocked. If they are serialized, the second call sees `_lease_released` or a Lease with no holder and returns (`:561-562`, `:578-582`). The 409 path stays for the late-renewal case (learnings 3.2).

**Wrong if** some caller does call `detach()` or `release()` from a signal handler while the same thread is inside one. Then a plain `Lock` hangs, and that is why question 2 picks `RLock`.

## 2. RLock, Lock, or non-blocking

| | Deadlock | What the second caller sees | 11/N | Client cleanup (`sandbox_client.py:574-589`, `:503-513`) |
| :-- | :-- | :-- | :-- | :-- |
| Plain `Lock` | Hangs on same-thread re-entry | Waits, then the first call's end state | Kept | Waits, then the end state |
| **`RLock`** | None | Same across threads. A same-thread re-entry goes through, as today. | Kept | Same |
| Non-blocking, raise | None | `BatchError` at once | **Broken**: a second concurrent `detach()` raises | Cleanup logs an error and gives up, even though the user's call may then fail and leave the batch |

**Pick `RLock`.** It costs the same as `Lock`, removes the hang, and the re-entry case it lets through is the one 11/N already handles. A same-thread `detach()` inside `release()` is still refused by the started check in question 3, so the N1 gap doesn't come back that way. Async uses an `asyncio.Lock`, because a signal callback there runs as a separate task and simply waits.

## 3. Lock scope

Holding the lock for all of both calls makes a `detach()` from another thread wait through the whole release, possibly minutes, and then raise anyway. Instead:

- **`release()`** holds the lock for its whole run. A second `release()`, from client cleanup or another thread, waits and then returns if the first one finished, or retries if it failed. That fixes (g) below. Today two releases on one handle run their rounds side by side, and one of them can misreport `BatchInUseError` after its take loses two conflicts (`:919-923`).
- **`detach()`** first checks under `_lock`. If a release has started, it raises `BatchError` at once. Then it holds the lock only from a second check, through its Lease read and writes, until it sets `_lease_released`. Setting that flag inside the lock is what makes the first call win in (f).
- **No new flag for "release started".** Use `self._detached and self._detach_grace is None`, behind a `_release_started()` helper next to `_detach_unfinished()` (`:531-533`). `detach()` always sets the grace (`:564-567`), and `release()` always clears it (`:469`).

The longest a caller waits is one other `detach()`'s Lease I/O, up to three requests of `BATCH_REQUEST_TIMEOUT_SECONDS` (30 s). The exception is a release waiting behind another release.

**Wrong if** you want `detach()` during a running `release()` to wait and then report the release's outcome rather than fail fast. Then hold the lock across all of `detach()`.

## 4. Interplay (inferred by tracing the code with all three changes applied)

The rule: a delete goes out only if, at the round's read, the Lease has the UID and `leaseTransitions` of this handle's last write, or there is no Lease.

| Path | Lease | Claims | `_detached` / `_lease_released` / `_released` | Registry | Rule |
| :-- | :-- | :-- | :-- | :-- | :-- |
| (a) plain `release()` | deleted at the version this round wrote | gone | T / T / T | removed | holds |
| (b) after own detach that landed but raised | holder empty, same UID and count, so taken and then deleted | gone | T / T / T | removed | holds |
| (c) as (b), another handle attached and detached in between | untouched, since the count differs | untouched | T / T / F | removed | holds |
| (d) Lease missing at round 1 | none, and none created | gone | T / T / T | removed | holds at the read. Gap: a batch created under the same id between the read and the deletecollection |
| (e) Lease recreated under another UID between rounds | untouched | rounds before the change deleted them. None after | T / T / F, `BatchInUseError` | removed | holds at each read. Same one-round gap |
| (f) `detach()` and `release()` on two threads | detach first: handed off, and release raises `BatchError`. Release first: release proceeds, and detach raises `BatchError` | kept / gone | T/T/F, or T/T/T | removed | holds |
| (g) cleanup racing a user's `release()` | the second caller waits, then sees the first's end state | as (a) | T / T / T | removed | holds. Today both run, and one can misreport |
| (h) async atexit hook during a `release()` on the loop | `asyncio.run` cancels the task first, so its lock is released and the hook's sync delete finishes the job as (a) | gone | T / T / T after the hook | n/a at exit | holds. Gap: a loop in a live background thread runs beside the hook with the same identity, so the worst case is a misleading printed warning |

No path deletes after the batch changed hands. The (d) and (e) gap only matters when a caller chooses its own batch ids and reuses one, because default ids are generated (`sandbox_client.py:372`). It is the gap any label delete has.

**Implementation catch.** With no creation, `_take_batch_lease` needs to tell "no Lease" apart from "the take failed". Return `None` for no Lease, and keep a failed take as an exception, so a round whose take hit a transient error never ends as a success while a Lease may still exist (the reason for `:981-983`).

## 5. Minimality

- **The flags can't replace the lock.** N1 is a check, then I/O, then act race, and `_lock` can't be held across I/O because renewal, the watch and `connect()` all take it. One new lock is the minimum. No new flag, since question 3 derives "release started".
- **The fence adds no state.** It reads `self._lease` (`:150`, `:781`). Pass the `V1Lease` as one parameter rather than a UID and a count.
- **One type widens, with a reason.** `_take_batch_lease` returns `str | None` (`None` means no Lease).
- **One rule becomes duplicated.** With the fence in place, the holder check (`:912`) can only fire for a writer that sets a holder without bumping `leaseTransitions`, such as kubectl or a future Go port bug. Keep it: one existing line from approved D1, and a clearer error message.
- **One thing gets removed.** #4 drops the create branch (`:895-911`) and its test.
- **One behavior change.** A `detach()` after a `release()` that failed now raises `BatchError`, because a release has started. Today it hands off a batch that may be half deleted. I think that's right, but it is part of what you approve with N1.

## 6. Test plan (one owning test per contract, mutation in brackets)

Both shells unless marked. The async tests use `asyncio.Event`, and all the race tests use Event handshakes, not sleeps.

1. **Fence: extend `test_release_deletes_nothing_while_another_handle_holds_the_lease` into a table.** Rows: another holder; no holder but `leaseTransitions` bumped; no Lease at round 1, then another UID at round 2 (deletecollection called once). Each row asserts `BatchInUseError` and no further delete. [Remove the count comparison: row 2 fails. Remove the UID comparison: row 3 fails.] Row 3 also owns #4's "Lease shows up mid-release" contract.
2. **Own landed detach still releases: one row in `test_release_takes_the_lease_before_each_round_and_deletes_it_at_the_version_written`.** No holder, same UID and count, so it takes, deletes, and deletes the Lease. [Compare resourceVersion instead of UID and count: fails. Refuse a Lease with no holder: fails.]
3. **No Lease means no create: replace `test_release_creates_a_missing_lease_unless_another_client_creates_it_first`.** The Lease is missing on every read. Claims are deleted, `create_batch_lease` and `delete_batch_lease` are not called, and release returns. Add a row where the take raises a transient error and the list shows no claims: release must not return. [Restore the create: fails. Treat a failed take as no Lease: the second row fails.]
4. **The first lifecycle call wins: one table test.** (i) Release blocked in deletecollection, then `detach()` on another thread raises `BatchError` and writes no Lease. (ii) Detach blocked in its Lease write, then `release()` on another thread sends nothing until it finishes, and then raises `BatchError`. (iii) Release blocked, then a second `release()` sends nothing and returns once the first finishes. [Drop the started check: (i) fails. Drop detach's lock: (ii) fails. Drop release's lock: (iii) fails.]
5. **Cleanup still unregisters on `BatchInUseError`.** Existing `test_cleanup_unregisters_a_handle_whose_batch_another_handle_holds` covers it. Check that it still passes with the fence, and add nothing.
6. **Sync only: same-thread re-entry doesn't hang.** Inside detach's mocked Lease write, call `handle.detach()` on the same thread. It returns within a join timeout. [Use `Lock` instead of `RLock`: it hangs, and the timeout fails the test.]

The existing 11/N test must still pass unchanged.
