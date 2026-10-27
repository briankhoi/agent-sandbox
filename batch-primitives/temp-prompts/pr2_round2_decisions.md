# 12/N (#1824) round 2: answers for the stack agent

Answers to `handoff/work/decisions_1824_round2.md`. Every claim was checked against `review/d1824/feat/batch-2-claim` `4c67b14e`, `review/d1824/feat/batch-5-release-group-76a8m3` `2810ca65` and `review/d1824/feat/batch-7a-create-path` `5f69ade8`. Line numbers are at `4c67b14e` unless marked. "Verified" means read in the code. "Inferred" means reasoned, not run.

## Summary

| # | Answer | Needs Brian? |
| :-- | :-- | :-- |
| 1 | **Do it.** UID plus `leaseTransitions` fence in `_take_batch_lease`. It finishes approved D1, which promised to close both paths but closes them only while the other handle still holds the Lease. | No, it completes approved D1 |
| 2 | **No change.** Keep the `err()` guard in PR 5. | No |
| 3 | **No change** to the 409 handling. The file's two causes are wrong (see below). | No |
| 4 | **Drop the creation.** It conflicts with the written D1 answer and the proposal, and it would break fix 1. | Only if Brian approved creation in chat (no written record found) |
| N1 | **New finding.** `detach()` can run during `release()` and hand the batch off mid-delete. Fix: one per-handle lock around both calls. | Yes, it changes what concurrent calls do |
| - | The 0.2 s join to Event change stays as approved. | No |

## 1. Fence on UID and `leaseTransitions`: do it

**Checked (verified).** `_take_batch_lease` only refuses a Lease another identity holds (`:912`), and its conditional write uses the resourceVersion of its own fresh read (`:893`, `:918`). A Lease with no holder is taken. Round 1 claimed option (b) left no risk for either path, but that is true only while B still holds the Lease. `leaseTransitions` is written only at create (`:237`, `= 0`) and attach (`:349`, `+1`), and the same holds at the top of the stack (7a `:258`, `:384`). `self._lease` is set at `:150` and `:781`. Default batch ids are generated (`sandbox_client.py:372`), so path 2 needs a caller that reuses its own ids. Path 1 is a supported sequence, because `release()` after a failed `detach()` is meant to work (`:468-469`).

**Do it because** the approved D1 was meant to stop exactly these two paths (round 1 brief, "Two real paths remain"). The cost is two comparisons against data the handle already keeps, and deleting a batch someone parked for handoff can't be undone.

**Alternatives.** Holder check only: misses both paths. Compare resourceVersion: refuses after the handle's own late renewal or landed detach. New epoch annotation: duplicates `leaseTransitions`. Refuse `release()` after any `detach()` attempt: breaks the supported release-after-failed-detach path.

**How to build.**
- Pass the handle's last-written Lease into `_delete_batch_objects` and `_take_batch_lease` (read `self._lease` under the lock). The async atexit hook (`async_sandbox_client.py:802`) passes `batch._lease` too.
- In `_take_batch_lease`, after each read, refuse if `uid` differs or `(lease_transitions or 0)` differs, and raise `BatchInUseError` with a message saying the batch changed hands since this handle held it. Put this before the holder check. The 409 retry re-reads, so it re-checks too.
- `release()` docstring: `BatchInUseError` also covers "the Lease changed hands since this handle last held it".
- Tests, both shells, one each: no holder but `leaseTransitions` bumped refuses and deletes nothing; another UID refuses and deletes nothing; the handle's own landed detach (no holder, same UID and count) still releases. Mutation check for each comparison.

**Wrong if** some attacher takes a batch over without bumping `leaseTransitions`. Nothing in this repo does today (verified at 7a). A future Go `GetBatch` has to bump it like client-go leader election does. Say so in the Go port notes.

## 2. PR 5's `err()` guard: no change

**Checked (verified).** `_check_lease_held` (PR 5, 7a `:426`) refuses once `err()` is set, and `_delete_claim` checks it before every attempt. `err()` is set by a watch error that won't recover (`:686`, `:699`) and by renewal stopping (`:790`). The file's point is right: a watch-only failure keeps renewing, so the handle still owns the batch.

**No change, because:**
- After `err()`, PR 2 already stops `connect()` (`:401`) and creates (`:827`). One rule, "err() means the handle has stopped", is easier to document and verify than a second ownership test that only deletes ignore.
- The watch fails for good only on a non-retryable status (403 or 404) or a non-transport exception. Transient errors and 410 retry (`:669-700`). A 403 on the watch most likely means RBAC changed, and then the deletes would get 403 too (inferred).
- The RL library already treats a stopped batch this way. It opens a new batch and leaves the old one's claims to teardown (`examples/agent-sandbox-rl/agent_sandbox_rl/batch_claims.py:99-108` on `feat/rl-batch-claim-mode`). `release()` isn't gated, so teardown still frees them. The caller is not stuck. Per-member releases on that batch wait until teardown or `shutdownTime`.
- `release_not_ready` picks from the watch cache. After a watch failure that cache is stale, so refusing is the right answer there.

**Alternatives.** Expiry gate (the file's proposal): about 3 lines per shell plus three docstrings per shell, and it helps only in the rare watch-only case. Note the two notes disagree on it: `stack_next_prompt.md` subtracts `CLOCK_SKEW_MARGIN`, the round-2 file doesn't. Splitting `err()`: more code for the same small gain.

**Wrong if** watch-only failures show up in real runs (for example in the GKE run), or the RL library needs a stopped batch's members freed before teardown. Then the expiry gate is the fix, with no margin, since nothing can take a held Lease before the reaper's 60 s margin (`batch_reaper.py:68`).

## 3. A 409 on the final Lease delete: no change

**The file's two causes don't hold (verified).**
- *A late renewal of this handle can't cause it.* Every renewal is a conditional replace carrying `self._lease`'s resourceVersion (`:799-804`; `replace_batch_lease` is a PUT, `k8s_helper.py:616-620`). The renewal thread is joined before the first take (`:502-504`). A renewal that lands late carries a version older than the one the take wrote, so the API server refuses it. The take's own 409 retry (`:919-927`) already handles a late renewal that lands before the take.
- *`release()` does not close `detach()`.* `:462-469` only clears `_detach_grace`. `detach()` checks just `_released` and `_lease_released` (`:559-562`), and both are still false during a release. See N1.

**Real causes:** an outside writer such as kubectl, a concurrent `detach()` on the same handle (N1), or a concurrent second `release()` on the same handle. In the second-release case "done" is right, because the other call finishes the job.

**No change, because** the claims are already gone when this 409 happens (`live == []`, `:983`). All that's left is an empty Lease, which the reaper removes. Without a reaper it only blocks the same batch id, and default ids are generated. Going around once more would make two concurrent `release()` calls on one handle undo each other's takes until one delete lands. With N1 fixed, the only cause left is an outside writer who edited the Lease on purpose.

**Wrong if** callers reuse their own batch ids and run without the reaper. Then go around once more (about 2 lines), after N1, so concurrent releases can't undo each other.

## 4. `release()` creating a missing Lease: drop it

**Checked.**
- The written round-1 answer says "Lease 404: proceed with the label deletes. Nobody can own the batch without a Lease" (`temp-prompts/pr2_d1_d2_decisions.md` item 1). The current create (`:895-911`) doesn't match it. I found no written record of Brian approving creation. `stack_next_prompt.md` says he did, without a quote.
- The proposal says "Nothing may recreate a Lease for existing claims, since the new Lease would have a different UID" (`batch_claim_proposal.md:43`). `reaper_contract.md:11` says the same.
- Fix 1 needs it gone. A Lease that `release()` created has a new UID, so the next round's fence would refuse it.
- The created Lease lacks the batch's annotations (`:897-904` against `:221-238`) (verified).
- Of the file's three reasons, "re-created on every round after expiry" is weak. A round creates only on a 404, and the reaper deletes only after expiry plus 60 s. "It surprised you" is not evidence. The first two facts above are enough.

**Build.** `_take_batch_lease` returns `None` on a 404. The round still deletes and lists claims. When nothing is live and there was no Lease, return. Drop "creating it if it's missing" from the `release()` docstrings in both shells and in `_take_batch_lease`'s own docstring. Replace the "created by another client first" test with: the Lease is missing, claims are deleted, nothing is created; and a Lease with another UID shows up mid-release, so the next round refuses before deleting (this is fix 1's UID test, so one test can cover both).

**What's left.** A batch created under the same id between our 404 read and our deletecollection can lose claims. That needs a caller-chosen reused id timed within one round.

**Needs Brian** only if he approved creation in chat. Then this reverses it, on the named facts above (the proposal text and fix 1).

## N1. `detach()` during `release()` hands the batch off mid-delete (new, inferred)

**Path (read in code, not run).** `release()` takes the Lease as this handle's identity (`:955`) and starts the deletecollection. A concurrent `detach()` on the same handle passes its checks (`:559-562`), reads the Lease, sees its own identity (the take used the same one), and writes no holder (`:584-597`). Another client can now attach, and the running deletecollection deletes the claims it just took over. `detach()` also returns success for a batch that is being deleted. The same happens in async (`async_sandbox_batch.py:564-579`). Client cleanup can race a user's call at shutdown (`sandbox_client.py:580-584`).

**Recommended fix.** Add a per-handle lock (`threading.Lock`, or `asyncio.Lock` in async) held for the whole of `detach()` and `release()`. Whichever call starts first finishes first, and the other then sees its end state: `release()` after a finished `detach()` raises `BatchError`, `detach()` after `release()` raises `BatchError`, and `release()` after a failed `detach()` still runs. This also removes the same-handle causes of the 409 in decision 3. About 4 lines per shell plus one test per shell, folded into PR 2's claim_batch and release commit.

**Alternatives.** A "releasing" flag checked before each detach write still leaves a gap: check, then take, then detach's read sees its own identity. Different holder identities for release and renewal: a larger change. Defer to an issue: OK if Brian wants 12/N smaller, since it needs two concurrent calls on one handle.

**Needs Brian** because it changes the outcome of truly concurrent `detach()` and `release()`. Today release usually ends up deleting. With the lock, the first call wins.

## Order for the stack agent

1. Copies on top of `review/d1824/*`. PR 2: fix 1, drop the create (4), N1 if Brian approves it, and the Event change. PR 5: no change for decision 2. Restack above.
2. Both shells, tests as listed, a mutation check per test, lint, mypy on every commit, docs regenerated.
3. Verifier rule: "A handle sends a delete only while it owns the batch. For `release()`, that means the Lease it takes is the same Lease (same UID) with no attach since its last write (same `leaseTransitions`), or there is no Lease. For PR 5's per-member deletes, it means `err()` is unset." Give it every path that touches this, including `detach()` and `release()` racing on one handle, and client cleanup.
4. 12/N description: add a bullet. "`release()` takes the batch Lease before each delete round and refuses, deleting nothing, if another handle holds it or it changed hands since this handle last held it."
