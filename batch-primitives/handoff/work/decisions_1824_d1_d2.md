# 12/N (#1824) decisions D1 and D2

Brian's open decisions for PR 2 (12/N, upstream #1824, branch `feat/batch-2-claim`, live head `386b39fc`; pending review copy `review/cr1824b/feat/batch-2-claim` `88049a56`). Written by the stack conversation, 2026-10-27. Facts are marked verified (read in code) or measured (run). Recommendations are proposals until Brian approves.

## D1. Should `release()` check who holds the batch Lease before deleting?

**Source.** CodeRabbit, unresolved thread on #1824 at `sandbox_batch.py:466` (release()).

**What happens (verified).** `release()` stops the handle, closes its cached connections, then deletes every claim with the batch's label in rounds, then deletes the Lease (`_delete_batch_objects`, `sandbox_batch.py` near :1492 at the top of the stack). It never checks that this handle still holds the Lease.

**The issue.** An old handle can delete a batch that another handle now owns. CodeRabbit's own example (renewal fails until the Lease expires, then another client attaches) can't happen, because `get_batch` refuses a stale Lease (`_attach`, `sandbox_batch.py:325-333` at PR 2, verified). Two real paths remain (verified by reading, not reproduced):
1. Handle A calls `detach()`. Its Lease write reaches the API server but A's client sees an error (for example, a response timeout). A thinks the detach failed. Handle B calls `get_batch` within the grace and attaches. A then calls `release()` (or client cleanup does), and A deletes B's claims.
2. The batch is reaped (Lease expired, reaper deleted it, claims garbage-collected). Someone creates a new batch with the same batch id, so B holds it. A's old handle calls `release()` and deletes B's new claims, which carry the same batch label.

Client cleanup reaches the same code, because from 12/N on it calls `release()` on every tracked batch.

**Options.**

| Option | What it does | Cost | Remaining risk |
| :-- | :-- | :-- | :-- |
| (a) Check first | GET the Lease. If another handle holds it, raise `BatchInUseError` and delete nothing. | One GET per release, one new `Raises` entry, a test per shell. | A race between the GET and the first delete. |
| (b) Re-take before deleting (recommended) | GET the Lease, then write it back with this handle as holder using the GET's resourceVersion (a conditional replace). On a 409, or if another handle holds it, raise `BatchInUseError` and delete nothing. Then delete as today. | One GET and one PUT per release, about 10 lines per shell, a test per shell. RBAC already allows get and update on Leases. | None for the two paths above. Another writer can't slip in between the write and the deletes without first winning the Lease, which the write blocks. |
| (c) Document only | Keep the behavior and document that `release()` deletes by label regardless of the Lease holder. | No code. | Both paths stay possible. It narrows what `release()` promises, so it needs Brian's explicit OK. |

**Recommendation: (b), folded into PR 2's "add claim_batch and release" commit, in both shells.** Client cleanup would treat `BatchInUseError` from `release()` as "this handle no longer owns the batch": it logs it and unregisters the handle instead of retrying it at every cleanup. The same question likely applies to PR 5's `release_member` and `release_group`, which also delete by label (inferred, not checked yet). Check those in the same round and fold any fix into PR 5.

**What would make this wrong.** If some caller legitimately needs `release()` to delete a batch it no longer holds (for example, a recovery tool that force-deletes an abandoned batch), (b) would block it. The reaper deletes Leases itself and never calls `release()`, so it is unaffected (verified for the reaper's design; reaper code not re-read for this note).

## D2. Add a test for the lock around `_release_batches`' snapshot?

**Source.** The readiness check on 12/N (measured).

**What happens (verified).** `SandboxClient._release_batches` takes a snapshot of the tracked batches under `_batches_lock`, then releases each one outside the lock (`sandbox_client.py:575` at PR 2). The lock keeps the snapshot consistent with a concurrent `get_batch`, `claim_batch` or `detach()` on another thread.

**The issue (measured).** Removing that lock breaks no test. The lock line is in PR 2's `_release_batches`, so nothing in PR 2 owns that contract.

**Options.**

| Option | Cost |
| :-- | :-- |
| (a) Add one deterministic race test (recommended) | About 15 lines in the sync tests. A dict double triggers a concurrent registration on another thread at the snapshot point. With the lock, the other thread blocks until the snapshot is taken. Without the lock, the test fails every time. The same technique already tests the registry's identity check. |
| (b) Skip | No code. One locking rule stays untested. |

**Recommendation: (a), folded into PR 2, with a mutation check (remove the lock and the test must fail).** Async needs no test, because its snapshot has no await between reading and copying the dict.

## Status
- Pending review copies, already rebased onto the renamed stack: `review/cr1824b/*` (PR 2 `88049a56`, PR 3 `529e51da`, PR 4 `821a4a81`, PR 5 `6d385668`, PR 6 `22871672`, transport `d36266fe`, reaper `f23913a1`, PR 7 `bb1a4dec`). They hold only the CodeRabbit doc fixes. D1 and D2 would be added on top after Brian decides.
- Live heads: PR 2 `386b39fc`, PR 3 `469b8d77`, PR 4 `a85caf4d`, PR 5 `7f1d6d5d`, PR 6 `51c1814a`, transport `9fa22b54`, reaper `e531baf3`, PR 7 `91d5ffc4`.
