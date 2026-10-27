# 12/N (#1824) round 3: review prompt

You are reviewing the stack conversation's response to `temp-prompts/pr2_round2_decisions.md` (your round-2 answers). This is a read-only review. Don't push any branch, edit code, or post on GitHub. Write your answers to `temp-prompts/pr2_round3_decisions.md` on `batch-primitives-notes`, then commit and push only that file (`git pull --rebase` first; author `Brian Nguyen <brianknguyen@google.com>`, no trailers).

## Setup

```sh
cd /usr/local/google/home/brianknguyen/agent-sandbox   # or your clone of briankhoi/agent-sandbox
timeout 60 git fetch origin
```

Check these heads before you read anything. If one differs, stop and say so.

| Branch | Head |
| :-- | :-- |
| `review/d1824/feat/batch-2-claim` (PR 2, 12/N) | `4c67b14e` |
| `review/d1824/feat/batch-5-release-group-76a8m3` (PR 5) | `2810ca65` |
| `review/d1824/feat/batch-7a-create-path` (top) | `5f69ade8` |
| live `feat/batch-2-claim` | `386b39fc` |

Line numbers below are in `clients/python/agentic-sandbox-client/k8s_agent_sandbox/sandbox_batch.py` at `4c67b14e` unless marked. Mark each of your claims as verified (read in the code, with `file:line`) or inferred.

## What is settled

- **#4, drop Lease creation in `release()`.** Brian, 2026-10-27: "i lean toward agreeing with their #4, even if i did previous approve i think we should drop creation".
- **Event handshake in place of the 0.2 s join in the race tests.** Approved by Brian earlier.

## The stack conversation's response to your round 2 (not yet approved by Brian)

1. **#1, UID and `leaseTransitions` fence.** Agrees. It will build it as your file describes: pass the handle's last-written Lease into `_delete_batch_objects` and `_take_batch_lease` (including the async atexit hook at `async_sandbox_client.py:802`), refuse before the holder check, and add three tests per shell.
2. **#2, PR 5's `err()` guard.** Agrees on no change, and drops its expiry-gate proposal. It names two facts it hadn't checked. First, the watch fails for good only on a non-retryable status or a non-transport exception (`:669-700`). Second, `release_not_ready` reads the watch cache, which is stale after a watch failure.
3. **#3, a 409 on the final Lease delete.** Agrees on no change. It was wrong about both causes in its round-2 file. A late renewal is a conditional PUT carrying an older resourceVersion, so the API server refuses it (`:799-804`). Also, `release()` doesn't block `detach()`, because `detach()` checks only `_released` and `_lease_released` (`:559-562`).
4. **N1, `detach()` during `release()`.** It agrees that the race is real (verified `:559-597`). It proposes one change to your fix: use a re-entrant `threading.RLock` in the sync shell in place of a plain `Lock`.
   - **Its reason (inferred, not run).** The 11/N work considered a signal handler that calls `detach()` while the same thread is already inside `detach()` (learnings 3.1, "a signal handler and a `finally`"). A Python signal handler runs on the main thread, between bytecodes of whatever that thread is running. With a plain `Lock` held by the interrupted `release()` or `detach()`, the handler's `detach()` would block forever.
   - **What `RLock` gives.** Calls from different threads are serialized. A same-thread re-entry, such as a signal handler, proceeds as it does today, so that one case is left with the N1 interleaving rather than a deadlock.
   - **Async.** An `asyncio.Lock` held for the whole of `detach()` and `release()`, because async tasks are the only way calls overlap there.
   - **Size.** About 4 lines per shell plus one test per shell, in PR 2's "add claim_batch and release" commit.

## Questions to answer

For each question, give a short answer, the evidence, and what would make it wrong.

1. **Deadlock claim.** Is the plain-`Lock` deadlock real? Check whether anything in the repo (SDK, README, docs, RL library at `feat/rl-batch-claim-mode` `3bee2847`, tests) calls or documents `detach()` or `release()` from a signal handler, or from `atexit` while the main thread may be inside one of them. Also check whether the 11/N "concurrent detach() success" contract (merged as `7cfe8484`) depends on two overlapping `detach()` calls getting through without waiting.
2. **RLock or something else.** Compare `RLock`, a plain `Lock`, and a non-blocking acquire that raises `BatchError` ("another detach() or release() is running") on contention. Cover deadlock risk, what the second caller sees, the 11/N contract, and client cleanup (`sandbox_client.py:575-590`), which can call `release()` or `detach()` on a handle while the user's thread is in one. Pick one.
3. **Lock scope.** The proposal holds the lock across the whole of `release()`, which can take many rounds (minutes for a large batch). Is that acceptable for a `detach()` from another thread, and for client cleanup, which would wait behind it? Is there a smaller scope that still closes N1?
4. **Interplay.** With all of #1 (fence), #4 (no creation) and N1 (lock) applied, trace these paths and give the end state of the Lease, the claims, the handle flags (`_detached`, `_lease_released`, `_released`) and the registry for each:
   - (a) a plain `release()`;
   - (b) `release()` after this handle's own `detach()`, which landed on the server but raised on the client;
   - (c) case (b), but another handle attached in between and then detached;
   - (d) a Lease that is missing at the first round;
   - (e) a Lease deleted and created again under another UID between rounds;
   - (f) `detach()` and `release()` called on two threads at once;
   - (g) client cleanup racing a user's `release()`;
   - (h) the async atexit hook (`async_sandbox_client.py:802`) for a handle whose `release()` is mid-flight on the loop.

   Flag any path where the rule breaks. The rule: "a handle sends a delete only while it owns the batch, meaning the Lease it takes has the same UID and `leaseTransitions` as its last write, or there is no Lease".
5. **Minimality.** Is there a smaller change with the same behavior? Does any part add state, duplicate a rule, or widen a type without need? In particular, could `_lease_released` or `_detached` replace the lock?
6. **Test plan.** List the tests per shell that #1, #4 and N1 need, one owning test per contract (`batch-primitives/skills/test-audit/SKILL.md`), and the mutation that each must fail on.

## Output shape

Start with a summary table: question, answer, and whether it needs Brian. Then one short section per question. Keep it concise. Brian reads the summary first.
