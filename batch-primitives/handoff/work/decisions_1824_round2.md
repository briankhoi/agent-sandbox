# 12/N (#1824): four decisions before the round is finished

Written by the stack conversation, 2026-10-27 22:45 UTC. Everything below is a proposal until Brian answers. Code references are at the pending review copies `review/d1824/*` (PR 2 `4c67b14e`, PR 5 `2810ca65`). "Verified" means read in the code. "Inferred" means reasoned, not run.

## Summary

| # | Question | Recommendation | Size |
| :-- | :-- | :-- | :-- |
| 1 | Should `release()` refuse a batch that changed hands since this handle last wrote its Lease? | Yes. Compare the Lease's UID and `leaseTransitions` with the handle's last write. | About 6 lines per shell, no new field |
| 2 | What should stop PR 5's `release_member`, `release_group` and `release_not_ready`? | The handle's own Lease expiry, not `err()`. | About 3 lines per shell, no new field |
| 3 | What does `release()` do when the final Lease delete gets a 409? | Go around once more instead of treating it as done. | About 2 lines |
| 4 | Should `release()` create the Lease when it's missing? | No. Drop the creation, and only delete the leftover claims. | Removes about 15 lines per shell |

Already approved, and included in the round: the two race tests wait on an Event instead of a 0.2 s join.

## Background: what this round adds

CodeRabbit asked on #1824 whether `release()` checks who holds the batch before it deletes it. You approved D1 (`work/decisions_1824_d1_d2.md`). Before each delete round, `release()` now writes the batch Lease with itself as holder, using a conditional write (`_take_batch_lease`, `sandbox_batch.py:880`). If another handle holds the Lease, it raises `BatchInUseError` and deletes nothing. At the end it deletes the Lease only at the resourceVersion it wrote (`sandbox_batch.py:983-994`).

An auditor reviewed that round and raised the four questions below.

## 1. A handle can delete a batch it already handed off

**What happens.**
- A holds batch X and calls `detach()`. The write reaches the API server, but A's client sees an error, such as a timeout. A thinks the detach failed.
- B calls `get_batch(X)` and attaches. Later B calls `detach()` too, so the Lease now has no holder.
- A calls `release()` (or client cleanup calls it for A). `_take_batch_lease` reads the Lease, sees no holder, writes A in, and deletes X's claims. B handed X off for someone to use, and A deleted it.

**Why the current check misses it.** `_take_batch_lease` only asks "does someone else hold the Lease right now?" (`sandbox_batch.py:912`). Its conditional write uses the resourceVersion of the read it just did, so it only stops a writer that changes the Lease in the next few milliseconds. It can't tell that the Lease changed hands while A wasn't looking.

The same gap covers a second path. X's Lease expires, the reaper deletes it, someone creates a new batch with the same id, and that client detaches it. A's old handle would then delete the new batch's claims, which carry the same label.

**Proposed fix.** Before taking the Lease, compare it with `self._lease`, the Lease from this handle's own last successful write (set at `sandbox_batch.py:150` and `:781`). Refuse with `BatchInUseError` and delete nothing if either value changed:
- `metadata.uid` changes when the Lease was deleted and created again, which covers the second path.
- `spec.leaseTransitions` goes up by one on every attach (`get_batch`, `sandbox_batch.py:349`, verified). `detach()` and renewals don't change it (verified: those two lines are the only writes of `lease_transitions` in the module). So it changes exactly when another handle attached, which covers the first path.

**Why this and not something else.**

| Option | Catches both paths? | Cost | Why not chosen |
| :-- | :-- | :-- | :-- |
| Holder check only (current) | No | None | Misses both paths above. |
| Compare resourceVersion with `self._lease` | Yes | Same as the proposal | Too strict. A's own late renewal, or A's own detach that landed, also change the resourceVersion, so A could never release its own batch after them. |
| Add a new "epoch" annotation | Yes | A new field written on every attach, and old Leases lack it | Duplicates what `leaseTransitions` already counts. |
| **UID plus `leaseTransitions` (proposed)** | Yes | Two comparisons against data the handle already keeps | None found. |

**Best practice.** This is the standard fencing-token idea. A holder carries a number that goes up on every change of owner, and anyone acting on stale ownership is refused. Kubernetes defines `leaseTransitions` as exactly that count, and client-go leader election increments it on each change of holder. It's minimal because it adds no state, using `self._lease`, which the handle keeps already.

**What would make this wrong.** A caller who wants an old handle to force-delete a batch that changed hands. No such caller exists in the stack, and the reaper deletes Leases without `release()`.

## 2. PR 5's delete guard blocks deletes it shouldn't

**What happens.** PR 5's `release_member`, `release_group` and `release_not_ready` delete claims by name or label. This round makes them refuse once `err()` is set (`_check_lease_held` in PR 5). The point is not to delete claims of a batch this handle may no longer own.

**The issue.** `err()` is also set by failures that have nothing to do with ownership. One example is a watch that fails for good while the Lease is still renewed. The handle still holds the batch, yet `release_member` refuses. In RL use, a caller releasing members one by one would get stuck on a batch it owns.

**Proposed fix.** Gate the deletes on what ownership actually depends on, the handle's own Lease expiry: allow a delete only while `time.monotonic() < self._lease_expires_at`. Also end that expiry at once when renewal finds that someone else holds the Lease or that it's gone (one line in `_stop_renewal`, which both cases already go through).

**Why it's safe without an extra margin (verified).** No other client can take over a Lease while it's live.
- `get_batch` refuses a Lease that has a holder (`sandbox_batch.py:336`), and it treats a Lease as stale 5 s early (`batch_utils.is_lease_stale`, which subtracts `CLOCK_SKEW_MARGIN`).
- The reaper deletes a Lease only after its own margin (default 60 s, `--skew-margin`).
- The handle's own expiry is measured from just before it sent the renewal, so it is never later than what others see (`sandbox_batch.py:224-226` and `:782`).

The one case the expiry can't see is an outside writer, such as kubectl, overwriting the Lease while it's live. The `_stop_renewal` line covers that as soon as the next renewal notices it.

**Why this and not something else.**

| Option | Cost | Why not chosen |
| :-- | :-- | :-- |
| Keep `err()` (current) | None | Blocks deletes on a batch the handle still owns. |
| Split `err()` into "ownership lost" and "other failure" | A new field and a refactor of every place that sets errors | The expiry already says whether the handle owns the batch. |
| Take the Lease before each delete, like `release()` | A GET and a PUT per member delete | Too costly for `release_member`, which RL calls per member. |
| **Own Lease expiry (proposed)** | About 3 lines per shell, existing fields | None found. |

**Best practice.** A lease holder acts only while its own lease is valid. client-go leader election works the same way. It's minimal because it uses existing fields.

## 3. A conflict on the final Lease delete counts as "done"

**What happens.** After the claims are gone, `release()` deletes the Lease only if it's still at the resourceVersion that `release()` wrote (`sandbox_batch.py:985-990`). If the Lease changed since, the API server answers 409. The helper treats that 409 as success (`k8s_helper.py:657`), so `release()` returns as if everything were deleted.

**The issue.** The Lease is left behind, and the caller is told the release finished. It then stays until the reaper removes it after expiry. Two things can cause the 409.
- An outside writer, such as kubectl.
- A late renewal from this same handle. A sync request that timed out on the client side can still be applied on the server afterwards (learning 3.2). The earlier note said only an outside writer could cause the 409. This second cause was found today while re-reading the code.

**Proposed fix.** Treat the 409 as "not done yet" and let the loop go around once more. The next round takes the Lease again (or raises `BatchInUseError` if someone else holds it now) and deletes it at the new resourceVersion. The helper keeps treating a 404 as deleted. The claim_batch rollback path deletes without a resourceVersion (`sandbox_batch.py:260`), so it's unaffected.

**Why this and not something else.**

| Option | Why not chosen |
| :-- | :-- |
| Keep "done" | It reports success while a Lease is left behind. The stakes are low, because the reaper cleans it up, but the result is wrong. |
| Raise at once | A late renewal from this handle is harmless, and one more round fixes it. |
| **Go around once more (proposed)** | The loop already re-takes and retries, so this only removes the 409 exception. |

**Your earlier question.** Is "done" fine because of graceful handoff? No. A handoff goes through `detach()`, and `release()` closes that path at its start (`sandbox_batch.py:462-469`), so no handoff can happen while it runs.

## 4. `release()` creates a missing Lease

**What happens.** If the Lease is gone when `release()` starts a round, `_take_batch_lease` creates a new one (`sandbox_batch.py:895-910`). You approved this earlier to reserve the batch id while the orphaned claims are deleted. The race it guards: someone creates a new batch with the same id between our read and our delete, and our delete removes their new claims.

**Why the previous agent recommends dropping it (a correction, not a new flip).** It found three facts after your approval.
- The created Lease lacks the batch's settings, such as the duration annotation and the caller labels and `create_rps` that PR 6 stores (compare `sandbox_batch.py:228-238` with `:897-904`). Anything that reads it, such as `get_batch` or the reaper, would see a different batch.
- Once the created Lease expires, every later round creates it again.
- It's a surprising side effect for a call named "release". It surprised you too.

The race it guards is rare. It needs a new batch with the same id to be created in the few milliseconds between our read and our delete.

**Proposed fix.** Drop the creation. If the Lease is missing, `release()` deletes the leftover claims and has no Lease to delete. Fix 1 still applies on every round. If a Lease with another UID shows up in the middle of the release, `release()` refuses before its next delete. The existing test for "created by another client first" changes to cover a Lease that disappears in the middle of a release.

**Why it's minimal.** It removes about 15 lines per shell and one surprising behavior. The remaining exposure is the read-to-delete gap, which is the same gap any label delete has.

## After you answer

1. Apply the changes on new copies on top of `review/d1824/*`, in the owning commits (PR 2 for 1, 3 and 4, PR 5 for 2), with tests in both shells, a mutation check for each new or changed test, lint, mypy on every commit, and docs regenerated.
2. A fresh verifier gets the rule ("a handle deletes batch objects only while it owns the batch, and owning means its own Lease is unexpired, or the Lease it takes is the one it last wrote") plus every path that touches it.
3. Short compare links for you, and the sync after your OK.
4. The two CodeRabbit replies and the 12/N description.
