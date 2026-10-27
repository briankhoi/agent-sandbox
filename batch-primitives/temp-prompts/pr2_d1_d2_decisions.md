# 12/N (#1824) D1 and D2: decisions for the stack agent

D1: option (b). Before deleting, GET the Lease and conditionally replace it with this handle as holder, using its resourceVersion. On a 409, or if another handle holds it, raise BatchInUseError and delete nothing. Additions:
1. Lease 404: proceed with the label deletes. Nobody can own the batch without a Lease, and this still cleans up orphans when the reaper isn't installed.
2. Delete the Lease at the end with a resourceVersion precondition from your own write, so a new holder can't be removed.
3. First verify that the holder identity is unique per handle, not per client or process. If two handles can share it, stop and report back before you code.
4. Client cleanup treats BatchInUseError as "not ours": log it, unregister, and don't retry.
5. Apply the same check to PR 5's release_member and release_group, and fold any fix into PR 5.
6. No force flag until a real caller needs one. A recovery tool can delete by label directly.
Both shells, folded into PR 2's "add claim_batch and release" commit, with one test per shell plus a mutation check.

D2: option (a). Add the deterministic race test for the _release_batches snapshot lock, sync only, folded into PR 2, with a mutation check: the test must fail with the lock removed. No async test, because its snapshot has no await.
