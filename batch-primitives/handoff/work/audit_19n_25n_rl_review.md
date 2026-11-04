# Independent Audit & Action Plan: `status_19n_25n_rl_review.md`

Audited at `2026-11-04 08:49 UTC` against `/usr/local/google/home/brianknguyen/agent-sandbox-restack` (`restack/19n` `4090e7bb`, `restack/20n`..`restack/24n` `42e9166c`, `restack/25n` `aa72017c`, and `restack/25n-p0-1` `51c9f541`) and live `origin`.

---

## 1. Executive Verdict: **KEEP ALL OF IT (`restack/19n`..`restack/24n` + `restack/25n-p0-1`) — Then Run Workstream B (`P0-2`..`P2-8`) in a Separate Conversation**

Your "accidental" prompt actually worked out **in our favor**:

1. **Take `restack/19n` (`4090e7bb`) + `restack/20n..24n` (`42e9166c`):**
   - The core batch-claims agent **is** the rightful owner of `19/N`–`24/N`. It folded all three Section-3 core SDK optimizations into `19/N` cleanly, preserved full sync/async parity, handled subtle concurrency edge cases we audited below (`WarmCredit.spend()`, leader-timeout `Future` handoff without clobbering successor `_prechecks`), and mechanically restacked `20/N`–`24/N` with **zero unintended code drift**.
2. **Take `restack/25n-p0-1` (`51c9f541`) as `25/N` (`origin/feat/rl-batch-claim-mode`), NOT just `restack/25n`:**
   - `P0-1` is a **bug in `25/N` itself** (`BatchClaim.release()` crashing with `BatchError` when a batch stops or is released, masked by `FakeBatch` omitting `_check_healthy()`). Baking `P0-1` directly into `25/N`'s commits means `25/N` lands bug-free instead of shipping a known release/teardown crash and patching it in `26/N`.
   - Even better, the agent's `P0-1` implementation (`3 files, +75/-14`) went **further** than our spec in two ways: it added `_check_healthy()` to `FakeBatch.acquire()`, `FakeBatch.connect()`, **and** `FakeBatch.release_member()`, and tested **both** teardown race windows (`acquire` → `teardown` → `connect` raising `BatchError` vs `connect` → `teardown` → `_torndown` raising `FleetError`) plus 500-retry slot preservation.
3. **Do NOT continue `P0-2`..`P2-8` in the core batch-claims conversation — launch a fresh Workstream B conversation stacked on `restack/25n-p0-1` (once pushed to `origin/feat/rl-batch-claim-mode`):**
   - `P0-2`..`P2-8` (`BatchWatchdog`, `reaper.py` + `verify_teardown()` Lease sweeping, parallel `release_all` / `acquire_batch`, `Cluster` pool sizing + `_ondemand` fast path, `AsyncSandboxFleet` executor cap, `FakeFleet.claim_expires_at` parity) are 100% `examples/agent-sandbox-rl/` follow-up work (`26/N`), completely orthogonal to `clients/python/`.
   - Keeping the core batch-claims agent focused on `19/N`–`25/N` (`#2166` + stack push) and running `P0-2`..`P2-8` in a dedicated Workstream B conversation avoids context bloat and prevents worktree collisions.

---

## 2. Independent Line-by-Line Audit Findings

### A. Empirical Verification Run (Isolated Scratch Worktree at `51c9f541`)
We checked out `restack/25n-p0-1` (`51c9f541`, which contains `restack/19n` + `restack/20n..24n` + `P0-1`) in an isolated detached worktree and ran the full test and lint gates:

| Check | Result |
| :-- | :-- |
| Python SDK unit suite (`pytest clients/python/.../test/unit`) | **`1,258 passed, 501 subtests passed`** (`0` failures) |
| `agent-sandbox-rl` unit suite (`pytest examples/agent-sandbox-rl/tests`) | **`701 passed`** in `13.11s` (`0` failures) |
| `mypy k8s_agent_sandbox` | **`0` issues across all batch/SDK modules** |
| `make toc-verify` | **Passed** |
| Tree diff `0081826e..070d0abe` vs `restack/19n..restack/24n` (excl. `docs/python_sdk_reference.md`) | **Identical** (`0` unintended code diffs across `20/N`–`24/N`) |
| `git range-diff 070d0abe..2d731966 restack/24n..restack/25n` | **`=` on all 4 commits** |

---

### B. Deep Code Audit of `restack/19n` (`0081826e..4090e7bb`)

1. **`WarmCredit` bounded cooldown + `spend(warmpool)` (`batch_utils.py:533-588`):**
   - **Verified sound.** Replacing permanent `self.throttled = True` with `_throttled_until = max(self._throttled_until, now + max(retry_after or 0.0, 5.0))` fixes the GKE `w1280` / `w2560` permanent `50/s` lock-in while backing off immediately during active `429` bursts.
   - **Bonus catch by the agent:** Adding `WarmCredit.spend(warmpool)` on paced creates (both fill `_create_worker` and `acquire()`) is crucial: while throttled, paced creates still adopt Ready sandboxes from the warm pool. Without `spend()`, post-cooldown recovery would reuse stale pre-cooldown credit and send unpaced creates against an already-drained pool. In `acquire()`, `spend(warmpool)` is placed *after* the `max(now, self._next_create_at) >= deadline` check, so a timed-out `acquire()` that sends nothing never wastes credit.

2. **Conditional `_refresh_warm_credit` (`sandbox_batch.py:737-763`, `async_sandbox_batch.py:746-772`):**
   - **Verified sound.** `need_refresh` checks `not covered and not refreshed and now < deadline and self._next_create_at > now and self._warm_credit.refresh_due(warmpool)` atomically under `self._lock`.
   - When `self._next_create_at <= now` (start of a wave, streaming RL `< 50/s`, or high `create_rps`), caller 1 skips the `GET SandboxWarmPool` completely (`0ms` wait, `0` extra RPCs) and advances `_next_create_at`; if a burst arrives behind it, caller 2 sees `_next_create_at > now`, refreshes once under per-pool `refresh_lock`, and populates credit for callers `2..N`.

3. **`self._in_flight` gating on `_check_pool` and `_refresh_warm_credit` (`sandbox_batch.py:866-949`, `async_sandbox_batch.py:880-980`):**
   - **Lock ordering verified deadlock-free:**
     - `_refresh_warm_credit`: `refresh_lock` → `self._in_flight` → short `self._lock` critical sections (never acquires `refresh_lock` while holding `self._in_flight` or `self._lock`).
     - `_check_pool`: short `self._lock` critical sections + `self._in_flight` around `_precheck_pools`.
   - **Leader-timeout handoff verified race-free:**
     - If a short-deadline leader times out waiting for `self._in_flight`, it removes `self._prechecks[warmpool]` **before** calling `check.cancel()`, and in `except BaseException:` guards cleanup with `if not check.done(): self._prechecks.pop(warmpool, None)` so it cannot clobber a new `Future` inserted by a awakened longer-deadline follower or raise `InvalidStateError` on an already-cancelled `Future`.
     - Both sync (`except CancelledError: continue`) and async (`await asyncio.wait_for(asyncio.shield(check), ...)` + `if check.cancelled() and task is not None and not task.cancelling(): continue`) wake longer-deadline followers cleanly to take over the pool check.

---

### C. Deep Code Audit of `restack/25n-p0-1` (`aa72017c..51c9f541`)

1. **`BatchClaim.release()` (`examples/agent-sandbox-rl/agent_sandbox_rl/batch_claims.py:62, 78-88, 178-179`):**
   - **Verified sound.** Calls `self.batch.release_member(self.member)` first; on `BatchError` (stopped or already-released batch, whose watch is dead and whose random `batch_id` is never re-attached), falls back to `self.cluster.resources.delete_claim(self.member.claim_name)` (404-safe). Non-`BatchError` exceptions (`ApiException(500)`, `RuntimeError("forbidden")`) still propagate so `SandboxFleet.release()` preserves the handle and reserved claim slot for retry, and `teardown()` still runs `finally: self._batch_claims.release_all()`.
   - Checked all `BatchClaim(` call sites across the repo: `batch_claims.py:178` is the sole constructor (`BatchClaim` is internal to `batch_claims.py`).

2. **`FakeBatch` & `test_batch_claims.py` (`examples/agent-sandbox-rl/tests/test_batch_claims.py`):**
   - **Verified sound and thorough.** `FakeBatch._check_healthy()` now gates `acquire()`, `connect()`, and `release_member()` identically to real `15/N` `SandboxBatch`, and tests cover:
     - Healthy release (`delete_claim` not called; goes through `release_member`).
     - Teardown with 1 stopped batch + 1 healthy batch (`delete_claim` called for stopped batch's handle, `release_member` for healthy batch's handle, both batches released, `active_claims == 0`).
     - Both teardown-vs-acquire race windows (Window 1 before `connect()` → `BatchError` + `delete_claim`; Window 2 after `connect()` → `FleetError` + `delete_claim` without `sandbox.terminate()`).
     - Mid-run release of old handle after batch replacement, including retry slot retention on `500` `ApiException`.

3. **Good catch in Section 3 of `status_19n_25n_rl_review.md`:**
   - Confirmed: `k8s_agent_sandbox.constants.BATCH_ID_LABEL` is `"agents.x-k8s.io/batch-id"` (not `"agent-sandbox.sigs.k8s.io/batch-id"`). We have updated `examples/agent-sandbox-rl/docs/batch_rl_integration_improvements.md` on `origin/bench/rl-gke` to fix that constant and mark `P0-1` as folded into `25/N`!

---

## 3. Concrete Next Steps (Exact Instructions for Each Agent)

### Step 1 — Tell the Core Batch-Claims Agent (Current Conversation Owning `/usr/local/google/home/brianknguyen/agent-sandbox-restack`):
1. Run its independent verifier / mutation pass on `restack/19n` (`4090e7bb`) as planned.
2. Push the updated stack from `/usr/local/google/home/brianknguyen/agent-sandbox-restack` to `origin`, **using `restack/25n-p0-1` (`51c9f541`) for `origin/feat/rl-batch-claim-mode` (`25/N`)**:
   - `restack/19n` (`4090e7bb`) → `origin/feat/batch-7a-create-path` (updates PR `#2166`)
   - `restack/20n` (`ce5d82e0`) → `origin/feat/batch-8-list-memory`
   - `restack/21n` (`ef86914c`) → `origin/feat/batch-9-types`
   - `restack/22n` (`9ebd0558`) → `origin/feat/batch-10-deletecollection-retry`
   - `restack/23n` (`fd368c40`) → `origin/feat/batch-11-acquire-annotations`
   - `restack/24n` (`42e9166c`) → `origin/merge/main-into-batch-claim-2`
   - `restack/25n-p0-1` (`51c9f541`) → `origin/feat/rl-batch-claim-mode`
3. Stop touching `examples/agent-sandbox-rl/` after pushing `restack/25n-p0-1`.

### Step 2 — Launch a Fresh Workstream B Agent (Once `restack/25n-p0-1` Is Pushed to `origin/feat/rl-batch-claim-mode`):
- Give the new Workstream B conversation the updated prompt in Part 1 of:
  `https://github.com/briankhoi/agent-sandbox/blob/bench/rl-gke/examples/agent-sandbox-rl/docs/batch_rl_integration_improvements.md`
- It now branches off the updated `origin/feat/rl-batch-claim-mode` (`51c9f541`, with `P0-1` already verified in place) and implements **`P0-2` through `P2-8`** cleanly as `26/N` (`feat/rl-batch-hardening`).
