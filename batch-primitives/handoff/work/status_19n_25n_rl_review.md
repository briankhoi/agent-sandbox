# Status of 19/N and 25/N Work After the RL Review

Snapshot at `2026-11-04 08:36 UTC` from the core batch-claims stack conversation.

## 1. Live `origin` Branches (Untouched)

No remote code branches have been updated yet in this round:

| Stack | Remote branch (`origin`) | Live head |
| :-- | :-- | :-- |
| `19/N` | `feat/batch-7a-create-path` | `0081826e` (on `upstream/feature/batch-claim` `a97eeae7`) |
| `20/N` | `feat/batch-8-list-memory` | `39dcd169` |
| `21/N` | `feat/batch-9-types` | `617d34b0` |
| `22/N` | `feat/batch-10-deletecollection-retry` | `9d3f4a75` |
| `23/N` | `feat/batch-11-acquire-annotations` | `ba3bc84c` |
| `24/N` | `merge/main-into-batch-claim-2` | `070d0abe` (merges `upstream/main` `bcf5d4a0`) |
| `25/N` | `feat/rl-batch-claim-mode` | `2d731966` |

## 2. Local Work Completed in `/usr/local/google/home/brianknguyen/agent-sandbox-restack`

### A. `19/N` (`restack/19n` at `4090e7bb`, 4 commits on `a97eeae7`)

Folded the three core-SDK optimizations from Section 3 of the RL review into `19/N`:

1. **Temporary `WarmCredit` cooldown instead of permanent latch (`ed41fe08`, commit 1):**
   - Replaced `WarmCredit.throttled: bool` in `clients/python/agentic-sandbox-client/k8s_agent_sandbox/batch_utils.py` with `BATCH_WARM_CREDIT_THROTTLE_SECONDS = 5.0`, `_throttled_until: float`, `is_throttled()`, `throttle(retry_after)` (`max(retry_after or 0.0, 5.0)` seconds), and `spend(warmpool)`.
   - Paced creates sent during a cooldown call `spend(warmpool)` to deduct from cached pool credit so post-cooldown recovery neither drops remaining warm credit nor over-credits cold claims, and `refresh_due(warmpool)` returns `False` while throttled.
2. **Conditional `_refresh_warm_credit` + `self._in_flight` gating on pool reads (`26b22edf`, commit 2):**
   - `SandboxBatch.acquire` and `AsyncSandboxBatch.acquire` check `self._next_create_at > now` atomically with `self._warm_credit.take(warmpool)` and `self._warm_credit.refresh_due(warmpool)` under `self._lock`, skipping the `GET SandboxWarmPool` refresh when `create_rps` pacing would not wait anyway (`self._next_create_at <= now`).
   - Both `_check_pool` (leader `_precheck_pools`) and `_refresh_warm_credit` (`get_sandbox_warmpool` inside per-pool `refresh_lock`) acquire `self._in_flight` bounded by `deadline - time.monotonic()`. If `_check_pool`'s leader times out waiting for `self._in_flight`, it removes `self._prechecks[warmpool]` and calls `check.cancel()` so surviving followers take over the precheck cleanly.
   - Kept `BATCH_DEFAULT_MAX_IN_FLIGHT = 20` unchanged as the SDK default.
   - Updated docstrings, `README.md`, `test_batch_utils.py`, `test_sandbox_batch.py`, and `test_async_sandbox_batch.py`.
3. **Rebased create-retry stop and cleanup commit (`2baceb4d`, commit 3) and regenerated `docs/python_sdk_reference.md` (`4090e7bb`, commit 4).**
   - Verified at `4090e7bb`: `pytest` passed (`48 passed` across `test_batch_utils.py`, `test_sandbox_batch.py`, `test_async_sandbox_batch.py`) and `mypy` passed (`0` issues in `37` files).
   - Not yet run on `4090e7bb`: independent verifier subagent pass and mutation checks.

### B. Mechanical Restack of `20/N` through `24/N` onto `4090e7bb`

Rebased mechanically with zero code changes:

- `restack/20n`: `ce5d82e0` (`range-diff` all `=`)
- `restack/21n`: `ef86914c` (`range-diff` all `=`)
- `restack/22n`: `9ebd0558` (`range-diff` `=`)
- `restack/23n`: `fd368c40` (regenerated `docs/python_sdk_reference.md` cleanly)
- `restack/24n`: `42e9166c` (two-parent merge `4e30e1eb` with `bcf5d4a0`, `12160070` `file_config`/SSL forwarding, `42e9166c` regenerated `docs/python_sdk_reference.md`; verified zero content drift via `git diff ba3bc84c restack/23n` vs `git diff 0bb19f14 4e30e1eb`)

### C. `25/N` (`examples/agent-sandbox-rl/`) Local Branches

Two local branches exist on top of `restack/24n` (`42e9166c`):

1. **`restack/25n` (`aa72017c`, 4 commits: `bc760aaf`, `831f50fc`, `d8a071b1`, `aa72017c`):**
   - Pure mechanical rebase of `origin/feat/rl-batch-claim-mode` (`2d731966`) onto `restack/24n` (`range-diff` all `=`, zero RL changes).
2. **`restack/25n-p0-1` (`51c9f541`, 4 commits: `ebec472c`, `2daddc49`, `6265951d`, `51c9f541`; `3 files changed, 75 insertions(+), 14 deletions(-)` vs `aa72017c`):**
   - Implements **only `P0-1`** across `25/N`'s 4 commits (`P0-2` through `P2-8` were not touched):
     - `examples/agent-sandbox-rl/agent_sandbox_rl/batch_claims.py`: adds `cluster: "Cluster" = field(repr=False)` on `BatchClaim` (passed in `BatchClaims.acquire`), and in `BatchClaim.release()` catches `BatchError` from `self.batch.release_member(self.member)` and calls `self.cluster.resources.delete_claim(self.member.claim_name)` (which ignores `404` via `Resources._delete`).
     - `examples/agent-sandbox-rl/tests/test_batch_claims.py`: adds `FakeBatch._check_healthy()` (raising `BatchError` when `self.released` or `self.error is not None`) on `FakeBatch.acquire()`, `connect()`, and `release_member()`; updates `test_release_goes_through_the_batch`, `test_teardown_releases_every_batch`, `test_claim_landing_after_teardown_is_released_through_the_batch` (covering both teardown before `connect` and teardown after `connect`), and `test_stopped_batch_is_replaced_and_old_members_still_release` (including retry preservation when `delete_claim` raises `ApiException(status=500)`).
     - `examples/agent-sandbox-rl/README.md`: notes that `release` frees the member through the batch, or deletes its claim directly once the batch has stopped or released.
   - `examples/agent-sandbox-rl/tests/test_batch_claims.py` passed locally against `51c9f541`; independent verifier subagent pass and mutation checks have not run on `51c9f541`.

## 3. Code Fact Noted While Reading `origin/bench/rl-gke` (`3df167a4`)

- In `clients/python/agentic-sandbox-client/k8s_agent_sandbox/constants.py` (line 48, re-exported in `k8s_agent_sandbox.__init__`), `BATCH_ID_LABEL` is `"agents.x-k8s.io/batch-id"`.
- In `examples/agent-sandbox-rl/docs/batch_rl_integration_improvements.md` on `origin/bench/rl-gke` (`3df167a4`, lines 398 and 408), the `P0-4` code snippet currently writes `"agent-sandbox.sigs.k8s.io/batch-id"`.
