# A2: Task 3 notes, minimal-change fix

**Summary.** This commit trims `00781c63` back to a minimal change. The proposal paragraph keeps Brian's wording and replaces only the words that described the interleaved order. The carryover roadmap PR 2 bullet now points at the PR 2 row, and the PR 2 row's evidence sentence is shortened. The PR 8 branch in `work_plan.md` reads "not created". Brian's 2026-10-22 answers on PR 8 scope and the Pacing PR are quoted verbatim, checked against `temp-prompts/pr8_scope_and_pacing_answers.md` lines 5 and 9.

## `batch_claim_proposal.md:58`, for the Google Doc

Before (at `00781c63^`):
```
Claims are created in the order that reaches quorum soonest. Each group's first `MinReady` claims go first, interleaved across groups in proportion to their sizes, and the rest follow. Ordinals still come from one batch-wide counter, and each group keeps a contiguous block of them; only the create order is interleaved.
```

After:
```
Claims are created in the order that reaches quorum soonest. Each group's first `MinReady` claims go first, one group after another in the caller's group order, and the rest follow in the same order. For groups A and B, that is A's first `MinReady` claims, then B's, then the rest of A, then the rest of B. Ordinals still come from one batch-wide counter, and each group keeps a contiguous block of them; only the create order differs.
```

## Self-check

A fresh `inherit` reviewer checked the proposal diff against `fill_create_plan` (`batch_utils.py:277-292` at `f3b61c61`). It found the text accurate and free of em dashes. It raised three findings:
1. "in group order" in the ordinal sentence is extra, because the ordinal behavior did not change. **Applied:** removed.
2. "differs from the ordinal order" can be just "differs". **Applied.**
3. The A/B example repeats the sentence before it. **Not applied**, because the coordinator's brief says to keep the example. The reviewer's smaller alternative merges the two sentences into one, for Brian to choose if he prefers it: "Each group's first `MinReady` claims go first, and the rest follow, both in the caller's group order: for groups A and B, A's first `MinReady` claims, then B's, then the rest of A, then the rest of B."

## Commit

See the commit link given with this report (the commit contains this file).
