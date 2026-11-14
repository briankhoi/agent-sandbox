# RL batch claim PR: handoff (2026-11-14 01:50Z)

## Branches on the fork (briankhoi/agent-sandbox)

| Branch | Tip | Use |
|---|---|---|
| `feat/rl-batch-released-sandbox-delete-rebased` | `ec01bf0d` | **Open the PR from this one.** The same 24 commits rebased onto the current `feat/rl-batch-hardening` (`e0639edd`); no conflicts; RL suite 803 passed, SDK suite 1528 passed on the rebased tree |
| `feat/rl-batch-released-sandbox-delete` | `74d65bda` | The same commits on the old base `300d05e7`, which is what the benchmark images were built from. Do not open the PR from it: neither upstream `main` nor the current `feat/rl-batch-hardening` contains `300d05e7` |
| `feat/rl-batch-released-sandbox-delete-k3` | `9188a6e9` | Old base, three commits earlier. Fallback only if a reviewer objects to the two default-behaviour changes; it would need the same rebase |

The first is the one to use. The other two exist because the benchmarks ran on the old base and because the last three commits change default behaviour.

PR base: `feat/rl-batch-hardening`, or `main` once that branch's PR has merged. If it merged as a squash commit, rebase once more onto `main`.

## PR title

rl: delete released batch sandboxes directly, release per task, and avoid cold launches at the end of a burst

## PR description

A fleet in batch claim mode released claims faster than the cluster removed their Sandboxes and pods. On two of three GKE clusters measured, the garbage collector removed about 13 objects a second, so finished pods held node memory and warm pools took minutes to refill. The fleet now deletes a released claim's Sandbox and pod itself, after the claim is gone, grouped by the claim UID label where the controller sets it. This needs `delete` and `deletecollection` on `sandboxes` and `pods` (and `list` on `sandboxes` for the label check); without them it turns itself off and nothing else changes.

Also in this PR:

- Step runs release each batch claim when its task finishes instead of one bulk release at the end of the step. This removes a 0.5 s wait per step. It sends one delete per claim, as `per_claim` does.
- `BatchClaimConfig.max_unbound_claims` (default 256): creates wait while that many claims are created but not Ready, for at most 5 s. A burst sized to its warm pool otherwise cold-started the last few dozen claims in about half the bursts, because the controller holds the last Ready sandboxes in stale reconciles and gives a claim only 2 s to wait for one.
- Async exec retries a connection that was never established instead of waiting 30 s.
- The SDK no longer keeps every released batch claim by name (about 90 B per release).
- `release_all` no longer bulk-releases a batch while an acquire is in flight.
- README: how to choose `max_in_flight`, `create_rps` and concurrency, with the measurements behind them.

`per_claim` mode is unchanged.

Measured against upstream `per_claim`, all tasks succeeded:

| Shape | Batch | per_claim |
|---|---|---|
| R2E 500×2×8, cluster with a slow garbage collector | 207 s | 1,015 s |
| R2E 500×2×8, cluster with a fast one | 229–253 s | 237–240 s |
| 3,000 SWE-bench tasks (500×6) | 155–167 s | 393–420 s |
| Eval 500×1 | 15.4–16.2 s | 18.3 s |
| 1,000-claim burst, claims/s | 207–240 | 106–119 |
| 1,000-claim burst, time to first instruction p50 | 2.6–2.8 s | 10.9 s |

The long-run gains come from the direct deletes and appear only where the garbage collector is slow. The burst and latency gains appear on every cluster measured. Full results and method: `examples/agent-sandbox-rl/bench-results/gke/RL_BATCH_FINAL_REPORT.md` on branch `bench/rl-gke`.

Risks: extra RBAC as above; released pods are force-deleted; up to 16 extra concurrent API requests beside `max_in_flight`; the cold-launch default rests on 8 bursts per arm on one cluster and is turned off with `max_unbound_claims=None`.

## Left before merge

1. **Rebase: done** onto the current `feat/rl-batch-hardening` (see the branch table). The benchmark images were built before the rebase; the rebased tree passes both test suites but was not benchmarked again.
2. **`make all`** has not been run on this branch. No Go files changed. RL suite (803) and SDK unit suite (1528) pass. mypy reports 2 errors in `trace_manager.py`, a file this branch does not touch.
3. **`docs/python_sdk_reference.md`** is changed by the branch; run `make fix-api-docs` and commit any difference.
4. **CHANGELOG / README** already describe every change above. The commit message of `aa0fe12f` quotes the first experiment's numbers (108.0 / 107.7 vs 112.6 / 111.8 s) rather than the later three-run comparison; both are correct.
5. **Fresh-cluster numbers for the last three commits** are not measured yet (the replacement fresh cluster is having its images pre-pulled). The fresh-cluster row above is from the tip three commits earlier.
6. **Open the PR** from `feat/rl-batch-released-sandbox-delete-rebased` with the text above.

## Follow-ups, not blocking

- Controller: wait for held warm candidates without the 2 s claim-age cut-off, or stop the second reconcile from taking a candidate. Raising `--sandbox-claim-warm-candidate-grace-period` is the config-only mitigation.
- Exec returns an empty string when the stream closes early (status channel unread, both modes): at least 5 tasks in over 300,000.
- A per-claim label in the SDK would allow grouped per-task claim deletes.
- The bulk-release race is narrowed, not closed.

## Still running at 01:50Z

- Paired `k4-final` vs `per_claim` runs on the two older clusters (`k5_*` runs).
- Image pre-pull, then the fresh-cluster comparison, on `asrl-c4-7`.
- Clusters to delete afterwards: `asrl-c4-7`, and node pool `gvisor-n2-ssd` on `asrl-c4-t0`.
