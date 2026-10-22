# Answers for the local agent (Task 3 questions)

## Q1. Drop members() cache and wake-up items for PR 8 scope?

No. Of the benchmark rerun review's section 3B "Drop" row, `members()` caching and wake-up changes stay dropped and are not in PR 8 (measured: `members()` 9 ms at 10k claims, wake-ups 0.05 to 0.08 cores). The metadata-only re-list in `release()` IS in PR 8. Section 7 of the review reversed that part of 3B, because scenario 1d showed the full-object re-list costs about 69.5 MB at 5,000 claims. PR 8 = claim-list pagination (attach and release re-list), metadata-only re-list in `release()`, and skip parsing the deletecollection body. Follow tasks.md 4.1.

## Q2. Does PR 7 warm-aware pacing replace the adaptive pacing PR on the roadmap?

No. PR 7 only adds static warm credit: creates a pool's ready replicas cover skip pacing, and the first 429 turns the credit off. It has no adaptive slowdown or speed-up, no latency signal, and a 429 still uses a retry attempt. The roadmap's Pacing PR stays, as a later, conditional item (kind measured no 429s up to about 940 creates/s).
