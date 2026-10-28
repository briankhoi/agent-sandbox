# Paste-ready replies for #1989 (drafts, not posted)

## CodeRabbit thread PRRT_kwDOPcbYcc6vAxgU (latch fill settlement)
> Good catch. `fill_settled()` recomputed its count on each call, so a Ready member that turned not-Ready un-settled the fill, and `events()` then waited until the fill deadline. The fix lands in #1988, which owns this code. A fill member now counts toward settlement once its outcome is decided. That means it failed or was lost, its Ready event went out, or the SDK dropped it because its group or the quorum failed. A held member that flips back to not-Ready still keeps the fill open, so `events()` never skips its event. #1988 and this PR add regression tests for group and quorum mode.

## CodeRabbit thread PRRT_kwDOPcbYcc6ukWNV (`_stop_batches()` marks handles detached)
> This is fixed on the current head. `_stop_batches()` now sets `batch._detached` and calls `notify_all()` under the batch lock before it stops the background tasks. A consumer blocked in `wait_for_quorum()`, `events()` or `group_outcomes()` wakes up and gets `BatchError`. The fix landed upstream in #1986 and is part of this stack's base.

## CodeRabbit nitpick in review 5606953886 (store `members` as a tuple)
> I'm keeping `members` as a list. The class is now `GroupOutcome`, and the SDK reads only its `error` field internally, so a caller that mutates `members` changes only its own view and can't break batch state. A list also matches `wait_for_quorum()`, which returns `list[Member]`, so the two consumers stay consistent.

## tomergee review 5689391414 (3 nits)
> Thanks. All three are in the latest push, and the handles also document that a quorum reached after the deadline, before any consumer checks it, still succeeds. Both `claim_batch` docstrings now say that `quorum_timeout` also bounds the batch quorum. Each handle has a test where `quorum_timeout` expires in quorum mode, and `TestQuorumMode` plus the README cover a batch where every group has `min_ready=0`. The fill-settlement latch you flagged lands in #1988.

## #1989 PR body ("What this PR does")
> This PR is the fourteenth in a series of PRs to implement a batch. It adds `wait_for_quorum()` to `SandboxBatch` and `AsyncSandboxBatch`.
>
> - `wait_for_quorum()` blocks until every group has `min_ready` Ready members. It returns those members in one list, with the groups in batch order.
> - It raises `QuorumUnreachableError` once any group can no longer reach `min_ready`. It raises `BatchTimeoutError` if the quorum isn't reached within `quorum_timeout` of the last claim create.
> - A failed quorum stops the remaining creates and hands out no members. A wait that ends with any other exception, such as a cancellation, also fails the quorum.
> - Members beyond the quorum are handed out through `events()` once the quorum is reached.
> - `wait_for_quorum()`, `group_outcomes()` and `events()` exclude each other, and each handle can call `wait_for_quorum()` only once.
> - The batch state gains a quorum consumer mode. The PR also adds unit tests for both handles, an e2e test, README docs and the regenerated Python SDK reference.
>
> As previous PRs have not been merged yet, this PR contains all previous, un-merged commits as well. This PR's actual diff consists only of the last four commits.
