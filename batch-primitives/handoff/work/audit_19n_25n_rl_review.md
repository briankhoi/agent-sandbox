# Independent Audit & Adversarial Verifier Report: `status_19n_25n_rl_review.md`

Updated at `2026-11-04 09:18 UTC` with full findings from an independent, zero-context adversarial code-verifier subagent across `restack/19n` (`4090e7bb`), `restack/20n`..`restack/24n` (`42e9166c`), and `restack/25n-p0-1` (`51c9f541`).

---

## 1. Executive Verdict: **PASS WITH MINOR NOTES (`0 P0` Blocking Bugs)**

1. **Keep `restack/19n`..`restack/24n` + `restack/25n-p0-1` (`51c9f541`):**
   - **Core `19/N` (`restack/19n`)**: **`0 P0`, `0 P1`, `2` small `P2` polish items** (one 40ms Prow CI test-flake guard in `test_sandbox_batch.py` / `test_async_sandbox_batch.py`, and one identity guard `self._prechecks.get(warmpool) is check` against a `KeyboardInterrupt` micro-window in `sandbox_batch.py`). All core concurrency invariants — including CPython 3.11–3.13 `asyncio.timeout` + `Lock`/`BoundedSemaphore` exact-tick cancellation, `WarmCredit` multi-pool throttling & `spend()` accounting, create-retry vs delete-sequence fencing, and 60-method sync/async AST parity — passed adversarial verification.
   - **Restack `20/N`..`24/N` (`restack/20n`..`restack/24n`)**: **PASS (`0` issues, `0` unintended drift).**
   - **`25/N` + `P0-1` (`restack/25n-p0-1`)**: **`P0-1` (`BatchClaim.release()`) verified 100% sound** (`SandboxBatch.release_member` never raises `BatchError` on a healthy batch, even on HTTP 500/403/timeout). The adversarial verifier caught **1 `P1` snapshot-trigger cleanup gap** in `examples/agent-sandbox-rl/agent_sandbox_rl/handles.py` (`PodSnapshotManualTrigger` CRs leaked on `SandboxHandle.release()` when `claim_mode="batch"` + `ClusterConfig(snapshots=True)`) plus 2 minor `P2` RL notes — **all of which live in `examples/agent-sandbox-rl/` and have been added directly to the Workstream B spec** so the core batch-stack agent does not need to touch RL code again!

---

## 2. Empirical Test & Lint Gate Results (Verified at `51c9f541`)

| Check | Result |
| :-- | :-- |
| Python SDK unit suite (`pytest clients/python/.../test/unit`) | **`1,258 passed, 501 subtests passed`** (`0` failures) |
| `agent-sandbox-rl` unit suite (`pytest examples/agent-sandbox-rl/tests`) | **`701 passed`** in `13.11s` (`0` failures) |
| `mypy k8s_agent_sandbox` | **`0` issues across all batch/SDK modules** |
| `make toc-verify` | **Passed** |
| Tree diff `0081826e..070d0abe` vs `restack/19n..restack/24n` (excl. `docs/python_sdk_reference.md`) | **Identical** (`0` unintended code diffs across `20/N`–`24/N`) |
| `git range-diff 070d0abe..2d731966 restack/24n..restack/25n` | **`=` on all 4 commits** |

---

## 3. Adversarial Verifier Findings

### A. Two Recommended 5-Minute `P2` Polish Fixes on `restack/19n` Before Pushing `#2166` (Core Batch-Stack Agent)

#### `[P2-SDK-1]` Eliminate 40ms Prow CPU-contention flake window in new `_check_pool` `_in_flight` unit tests *(Recommended)*
- **Where:**
  - `clients/python/agentic-sandbox-client/k8s_agent_sandbox/test/unit/test_sandbox_batch.py:3604-3611` (`restack/19n`)
  - `clients/python/agentic-sandbox-client/k8s_agent_sandbox/test/unit/test_async_sandbox_batch.py:3567-3574` (`restack/19n`)
  - *(Also check `timeout=0.03` / `0.04` in `test_warm_credit_refresh_uses_max_in_flight_slot` around `test_sandbox_batch.py:3540-3545, 3640-3650`.)*
- **Why:** `short_check = pool.submit(batch.acquire, "pool-b", timeout=0.04)` followed by `_wait_until(lambda: "pool-b" in batch._prechecks)` (10ms sleep poll) will hang 5s and fail with `AssertionError` on loaded Prow nodes if the test thread is descheduled `>= 40ms` before observing `_prechecks["pool-b"]` (because `short_check` has already timed out and deleted `_prechecks["pool-b"]`).
- **Fix:** Use a deterministic hook/event when entering `_in_flight.acquire` (or raise `short_check`'s timeout to `0.25`–`0.5s` while `gate` holds the sole `_in_flight` slot, since `short_check` is supposed to time out waiting on the gated slot anyway).

#### `[P2-SDK-2]` Guard `self._prechecks` cleanup by identity (`is check`) in `SandboxBatch._check_pool`
- **Where:** `clients/python/agentic-sandbox-client/k8s_agent_sandbox/sandbox_batch.py:871-899` (`restack/19n`) (and mirror `is check` in `async_sandbox_batch.py:888-913` for symmetry).
- **Why:** On sync `_in_flight.acquire` timeout (`not acquired`), Thread T1 runs `with self._lock: del self._prechecks[warmpool]` and releases `self._lock` before `check.cancel()`. If Thread T2 inserts a new `check2 = Future()` at `self._prechecks[warmpool]` and a `KeyboardInterrupt` hits T1 before `check.cancel()`, `check.done()` is still `False` in `except BaseException:`, so `self._prechecks.pop(warmpool, None)` pops T2's `check2` (causing T2 to hit `KeyError` at line 898).
- **Fix:** Guard every removal of `self._prechecks[warmpool]` (on `not acquired`, in `except BaseException:`, and on completion) with `if self._prechecks.get(warmpool) is check: del self._prechecks[warmpool]` and call `check.cancel()` before/immediately with it.

*(Note: The verifier also checked `_precheck_pools` using `BATCH_REQUEST_TIMEOUT_SECONDS` rather than the leader's `remaining` deadline and confirmed keeping `BATCH_REQUEST_TIMEOUT_SECONDS` is intentional so a short-deadline leader cannot poison `check` with a `ReadTimeoutError` for long-deadline followers.)*

---

### B. Findings in `25/N` (`examples/agent-sandbox-rl/`) — Already Routed to Workstream B (`26/N`)

*(Do **not** ask the core batch-stack agent to touch `examples/agent-sandbox-rl/` for these — they are now in Workstream B's spec on `origin/bench/rl-gke`!)*

1. **`[P1-RL-Snapshots]` `SandboxHandle.release()` in `claim_mode="batch"` skips `PodSnapshotManualTrigger` cleanup (`handles.py:614-620`):**
   - In `per_claim` mode, `self.sandbox.terminate()` on `SandboxWithSnapshotSupport` calls `self._snapshots.delete_manual_triggers()` (deleting un-owned `PodSnapshotManualTrigger` CRs created by `snapshot()` / `suspend()`) and sets `self._snapshots = None`.
   - In `batch` mode, `handle.release()` calls `self._batch_claim.release()` (`SandboxBatch.release_member` -> `sandbox.close_connection()`) and returns early without calling `delete_manual_triggers()`. Added as **`P0-5`** in Workstream B.
2. **`[P2-RL-Timeout]` Bounded `_request_timeout` on `BatchClaim.release()` fallback `delete_claim` (`batch_claims.py:87`):**
   - Pass `request_timeout=30` (matching `BATCH_REQUEST_TIMEOUT_SECONDS`) when falling back to `self.cluster.resources.delete_claim(self.member.claim_name)` so a half-open TCP socket during an API stall cannot block `release()` indefinitely. Added to **`P0-5`** in Workstream B.
3. **`[P2-RL-Test]` Realistic `SandboxNotReadyError` in `test_failure_after_acquire_releases_the_member` (`test_batch_claims.py:349-361`):**
   - On a healthy `SandboxBatch`, `connect()` raises `SandboxNotReadyError` (not `BatchError`) if the watch saw the member leave `READY` in the micro-window after `acquire()`. Added to **`P0-5`** in Workstream B.

---

### C. Tricky Concurrency Invariants Explicitly Confirmed Sound

1. **CPython 3.11–3.13 `asyncio.timeout` + `Lock.acquire()` / `BoundedSemaphore.acquire()` (`AsyncSandboxBatch._check_pool` & `_refresh_warm_credit`):**
   - Verified against CPython `asyncio.locks`, `asyncio.timeouts`, and `asyncio.tasks` and empirically tested across both same-tick callback orderings (`release(); _on_timeout()` and `_on_timeout(); release()`):
   - Uncontended `acquire()` completes synchronously without yielding while `Timeout._state == ENTERED`, setting `acquired = True` and cancelling `_timeout_handler` in `__aexit__` before `_on_timeout` can run.
   - Contended `acquire()` hit by same-tick `release()` + `_on_timeout()` throws `CancelledError` into `await fut`, where CPython's `Semaphore.acquire()` restores `self._value += 1` + `_wake_up_next()` and `Lock.acquire()` wakes the next waiter before setting `_locked = True` — **zero lock or semaphore slot leaks**.
   - External `task.cancel()` is preserved (`self._task.uncancel() <= self._cancelling` is `False` when an external cancel also arrived, so `CancelledError` is never swallowed by `contextlib.suppress(TimeoutError)`).
2. **`WarmCredit` multi-pool throttling, `spend()`, & `Retry-After` edge cases (`batch_utils.py:495-588`):**
   - Batch-wide `_throttled_until` matches Kubernetes APF priority-level throttling; `spend(pool)` deducts warm sandboxes adopted by paced creates during cooldown; `Retry-After` handles `"0"`, negative, missing, HTTP-date, `"inf"`/`"nan"`, and huge values (capped at `BATCH_BACKOFF_MAX_SECONDS = 30.0s`).
3. **`SandboxBatch.release_member` exception contract (`P0-1`):**
   - `release_member` raises `BatchError` **iff** `_check_healthy()` fails (`_detached` or `_state.error() is not None`). Healthy-batch HTTP `500`/`403`/timeout errors raise `ApiException` or transport errors (`urllib3`/`ConnectionError`/`SSLEOFError`), so `BatchClaim.release()` never bypasses a healthy batch on transient or RBAC failures.

---

## 4. Exact Action Plan Now

1. **For the Core Batch-Stack Agent (`/usr/local/google/home/brianknguyen/agent-sandbox-restack`):**
   - Optional (recommended, ~5 min): apply `[P2-SDK-1]` (widen the 40ms `_prechecks` test timeout / gate entry so Prow CI never flakes) and `[P2-SDK-2]` (`self._prechecks.get(warmpool) is check`) on `restack/19n`, restack `restack/20n..24n` + `restack/25n-p0-1`.
   - Push `restack/19n..24n` and `restack/25n-p0-1` (as `origin/feat/rl-batch-claim-mode`) to `origin`.
   - **Done** — no further work needed from the batch-stack agent.
2. **For Workstream B (Can Start Immediately in Parallel in a New Conversation):**
   - Branch off local `restack/25n-p0-1` (or `origin/feat/rl-batch-claim-mode` once pushed) using Part 1 of:
     `https://github.com/briankhoi/agent-sandbox/blob/bench/rl-gke/examples/agent-sandbox-rl/docs/batch_rl_integration_improvements.md`
   - Implement `P0-2` through `P2-9` (now including `P0-5` for `[P1-RL-Snapshots]` + `[P2-RL-Timeout]` + `[P2-RL-Test]`) strictly inside `examples/agent-sandbox-rl/`. If `19/N` moves due to the two `P2` SDK tweaks, `git rebase --onto <new-25n> <old-25n> feat/rl-batch-hardening` is 100% conflict-free (`0` shared files).
