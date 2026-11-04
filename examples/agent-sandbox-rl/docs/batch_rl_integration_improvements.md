# Workstream B: `agent-sandbox-rl` Batch Claim Mode (`25/N`) Follow-up Optimizations & Hardening

> **Base branch:** `origin/feat/rl-batch-claim-mode` (`3a25143c`, stacked linearly on `24/N` `origin/merge/main-into-batch-claim-2` `b4cf6575` = `1/N..23/N` + `upstream/main` `bcf5d4a0`)
> **Target directory:** `examples/agent-sandbox-rl/` (do **not** modify `clients/python/` in this workstream unless explicitly requested)

---

## Part 1: Ready-to-Paste Agent Prompt for Workstream B

Copy everything inside the fenced block below into a new agent conversation:

````markdown
You are implementing **Workstream B**: hardening, parity, and scale optimizations for `FleetConfig(claim_mode="batch")` in `examples/agent-sandbox-rl/`, stacked directly on `25/N` (`origin/feat/rl-batch-claim-mode`).

### 1. Authoritative Spec & Context
Read `examples/agent-sandbox-rl/docs/batch_rl_integration_improvements.md` (available on `origin/bench/rl-gke`) in full before writing any code. It contains:
- Exact line-level analysis of `25/N` (`origin/feat/rl-batch-claim-mode`, commit `3a25143c`) and how it interacts with the finalized `15/N`–`23/N` Python SDK batch stack (`clients/python/agentic-sandbox-client/k8s_agent_sandbox/`).
- What `25/N` **already solved** during its `24/N` rebase (which you MUST NOT duplicate or regress).
- The **8 prioritized engineering items (`P0-1` through `P2-8`)** you own, including exact file/line targets, invariants, edge cases, and required unit tests.

### 2. Workspace & Branch Setup
1. Create an isolated git worktree/branch directly off `origin/feat/rl-batch-claim-mode` (do **not** touch any existing worktrees or uncommitted work):
   ```bash
   git fetch origin upstream
   git worktree add ../agent-sandbox-rl-ws-b -b feat/rl-batch-hardening origin/feat/rl-batch-claim-mode
   ```
2. Verify baseline tests pass in an isolated venv with the local Python SDK (`clients/python/agentic-sandbox-client`) installed editable (`pip install -e clients/python/agentic-sandbox-client[async,test] -e examples/agent-sandbox-rl[dev]`):
   ```bash
   pytest examples/agent-sandbox-rl/tests -q
   ```

### 3. Strict Non-Regression Rules (Read Carefully!)
`25/N` (`origin/feat/rl-batch-claim-mode`) **already** includes:
- Full `upstream/main` merge (`#1791`, `#1960` `runtime_container`, `#1961`–`#1967`, `#2035` `Cluster._sandbox_client_lock` / `_k8s_helper_lock`, `#2085`–`#2087`, `#2132`).
- Zero-`GET` batch `acquire()` (`handles.py`: `record_pod_identity()` skipped when `claim_mode="batch"`, `h.pod_uid is None` until `record_pod_identity()` is explicitly called, `BatchClaim.still_reaches()` in `is_alive()`, `rebaselined = True` in `refresh()`).
- Direct `pod_annotations` forwarding to `SandboxBatch.acquire(..., pod_annotations=...)` (`23/N`).
- Direct `cluster.sandbox_client.claim_batch(...)` using `K8sHelper(api_client=self.api_client)` (which already configures `coordination_v1_api` with `retries=False` and `_claim_collection_api` with `read=False`).
- Intentional `quorum_timeout=self.config.effective_claim_timeout()` wiring tested by `test_claim_timeout_overrides_ready_timeout_for_batch_claims` (`tests/test_batch_claims.py:161-170`).

Do **not** undo or alter any of those contracts, and do **not** modify files outside `examples/agent-sandbox-rl/`.

### 4. Deliverables (Implement in Commit Order)
Implement all 8 items from Part 3 of `examples/agent-sandbox-rl/docs/batch_rl_integration_improvements.md` in clean, well-tested commits:
1. **`P0-1`:** Fix stopped/released `SandboxBatch` `BatchError` crash on `BatchClaim.release()` and make `FakeBatch.release_member` faithful to `15/N`'s `SandboxBatch._check_healthy()`.
2. **`P0-2`:** Switch REST `read_namespaced_pod_status` in `handles.py` (`record_pod_identity()` and `is_alive()`) from `exec_core_api()` to shared `cluster.core_api` (keeping `exec_core_api()` strictly for `stream()` websocket exec/sessions).
3. **`P0-3`:** Add per-batch `BatchWatchdog` (`fail_fast` parity for `claim_mode="batch"` cold starts, `0` `GET`s before `initial_delay_s`).
4. **`P0-4`:** Sweep and verify batch `coordination.k8s.io/v1` `Lease` objects in `reaper.py` and `SandboxFleet.verify_teardown()` (with non-empty `label_selector` guard).
5. **`P1-5`:** Parallelize `SandboxFleet.release_all()` and `SandboxFleet.acquire_batch()` over bounded `ThreadPoolExecutor`s (preserving input order and partial-failure rollback).
6. **`P1-6`:** Auto-size `Cluster` `connection_pool_maxsize` so `17/N`'s `check_pool_size` never raises `ValueError` at high `max_in_flight` / `max_concurrent`, and skip redundant `_ensure_pool` K8s RPCs when `(cluster.name, task.image)` is already in `self._ondemand`.
7. **`P2-7`:** Raise `AsyncSandboxFleet._thread_pool()` ceiling (`min(4096, max(mc + 64, ...))`) so `max_concurrent >= 1000` never self-throttles in Python's executor queue.
8. **`P2-8`:** Surface an actionable `work_budget` message on `SandboxLostError` when `claim_expires_at` has elapsed, and add `claim_expires_at` parity to `FakeFleet` / `FakeHandle` in `testing.py`.

Run the full `pytest examples/agent-sandbox-rl/tests` suite after each commit and ensure 100% pass rate with zero regressions. Follow repository style (`2`-space indentation in `agent-sandbox-rl`, Apache-2.0 headers on any new files, no internal URLs or project IDs).
````

---

## Part 2: Alignment Summary — What Changed in `15/N`–`25/N` & Why It Matters

### 2.1 SDK Batch Stack State (`15/N`–`23/N`)
| PR | Branch / Status | Key Mechanics Relevant to `agent-sandbox-rl` |
| --- | --- | --- |
| **`15/N`** (`#1990`) | Merged (`113fae28`) | `SandboxBatch.acquire()` + `release_member()`. **Crucial invariant:** `release_member()` calls `self._check_healthy()`, which raises `BatchError` if `self._released` is `True` **or** `self._error is not None` (e.g. `BatchLeaseExpiredError` or fatal watch stop). |
| **`16/N`** (`#1991`) | Merged (`dc9ea439`) | `BatchGroup(size=0)` opens watch + Lease renewal immediately with zero pre-created claims (`BatchClaims._batch_for` uses `[BatchGroup(warmpool=pool, size=0)]`). |
| **`17/N`** (`#2153`) | Merged (`6feedc5c`) | `check_pool_size(pool_size, max_in_flight)`: raises `ValueError` if `urllib3` `connection_pool_maxsize` (when explicitly set) `< max_in_flight + 2`. |
| **`18/N`** (`#2154`) | Merged (`a97eeae7`) | `Member.pod_ips` populated from shared `SandboxClaim` watch (`Status.PodIPs`); `batch.connect(member)` seeds `Sandbox._pod_ip` and `_sandbox_id` with **0 `GET`s**; `still_reaches()` checks watched `READY` + `sandbox_name` + `pod_ip`. |
| **`19/N`** (`#2166`) | Open (`0081826e`) | Create-path pacing (`WarmCredit` + `_reserve_create_slot`), deadline-bounded `_send_create(timeout=_create_call_timeout(deadline))`, `_is_transport_error` excludes `BatchError`, and sequence-numbered `_pending_deletes` prevents late-delete races against create retries. |
| **`20/N`–`23/N`** | Stacked (`b7a59f29..d314209e`) | Paginated relist + stripped `managedFields` (`20/N`), `V1Lease` typing (`21/N`), `_claim_collection_api` with `read=False` (`22/N`), and `acquire(..., pod_annotations=...)` (`23/N`). |
| **`24/N`–`25/N`** | Stacked (`b4cf6575..3a25143c`) | Merges `upstream/main` (`bcf5d4a0`) into the stack (`24/N`) and layers `FleetConfig(claim_mode="batch")` cleanly on top (`25/N`, `origin/feat/rl-batch-claim-mode`). |

### 2.2 Already Completed in `25/N` (`3a25143c`) — Do NOT Redo
1. **Upstream `main` reconciliation:** `runtime_container`, `Cluster._sandbox_client_lock` / `_k8s_helper_lock`, `Testing` (`FakeFleet`/`FakeHandle`), `verify_teardown()`, and `reaper.py` orphan protection are already merged and wired.
2. **Zero-`GET` hot path on `acquire()` + watched `is_alive()`:**
   - `fleet.py:1648-1649` only calls `handle.record_pod_identity()` when `claim is None` (`claim_mode="per_claim"`).
   - `handles.py:399-404` short-circuits `is_alive()` via `self._batch_claim.still_reaches()` with `0` `GET`s when `self.pod_uid is None`, falling through to `read_namespaced_pod_status` if healthy or if `batch.err() is not None` or `rebaselined` is `True`.
   - `tests/test_batch_claims.py:337-392` (`test_batch_acquire_skips_pod_get_and_is_alive_checks_the_batch_watch`) explicitly locks down `h.pod_uid is None` on acquire and the exact `read_pod.call_count` sequence (`0 -> 1 -> 1 -> 1 -> 2 -> 3 -> 4 -> 5 -> 6 -> 7`). **Preserve this exact contract.**
3. **Native `pod_annotations` (`23/N`):** `batch_claims.py:165-166` passes `pod_annotations=pod_annotations` directly to `batch.acquire(...)`.

---

## Part 3: Detailed Specification of Workstream B Items (`P0-1` – `P2-8`)

### `P0-1`: Fix Stopped / Released `SandboxBatch` Crash in `BatchClaim.release()` & Fix `FakeBatch.release_member`

#### Problem Caught Between `15/N` and `25/N`
In `15/N` (`clients/python/agentic-sandbox-client/k8s_agent_sandbox/sandbox_batch.py`), `SandboxBatch.release_member(member)` starts with `self._check_healthy()`, which **always raises `BatchError`** once:
- `self._error is not None` (e.g. `BatchLeaseExpiredError` after temporary API partition or Lease loss), **or**
- `self._released` is `True` (after `batch.release()` ran during `teardown()`).

In `25/N`, `FakeBatch.release_member` (`tests/test_batch_claims.py:83-86`) omitted `_check_healthy()`, masking three real bugs against `SandboxBatch`:
1. **Mid-run release of a handle from a replaced batch (`test_stopped_batch_is_replaced_and_old_members_still_release`, line 286):** `c.batches[0].error = BatchError("lease lost")` -> `f.release(old)` calls `BatchClaim.release()` -> `batch.release_member(member)` **raises `BatchError`** -> `SandboxFleet.release()` (`fleet.py:1737-1744`) puts `old` back into `self._handles`, never decrements `c.active_claims` or `self._claims_reserved`, and crashes the caller!
2. **Teardown after batch replacement (`test_teardown_releases_every_batch`, line 202):** `f.teardown()` calls `self.release_all()` -> `f.release(old)` -> **raises `BatchError` out of `_teardown()`**, aborting the entire pool/template/claim sweep after `finally:`!
3. **In-flight acquire finishing during teardown (`test_claim_landing_after_teardown_is_released_through_the_batch`, line 251):** `f.teardown()` runs `batch.release()` (`DELETECOLLECTION` + `self._released = True`) while `batch.acquire()` is in flight; when `torn` is detected (`fleet.py:1660-1668`), `claim.release()` calls `batch.release_member(member)` -> raises `BatchError("... has been released")`, **leaving the late-created `SandboxClaim` (created after `DELETECOLLECTION` ran!) leaked on the cluster**!

#### Required Fix
1. **`agent_sandbox_rl/batch_claims.py`:**
   - Add `cluster: "Cluster" = field(repr=False)` (or optional with default `None` for standalone unit tests) to `BatchClaim`, passed by `BatchClaims.acquire(cluster, pool, ...)`.
   - Update `BatchClaim.release()` (`batch_claims.py:77-79`):
     ```python
     def release(self) -> None:
       try:
         self.batch.release_member(self.member)
       except BatchError:
         # On a healthy batch, release_member() records the deletion first so the
         # live watch does not treat it as MemberState.LOST. Once the batch is
         # stopped (batch.err() is not None) or already released by teardown, its
         # watch is dead and release_member() raises BatchError — delete the claim
         # directly (404-safe) so neither a replaced batch's old handles nor an
         # acquire landing after DELETECOLLECTION leaks a live pod or crashes release.
         if self.cluster is not None:
           self.cluster.resources.delete_claim(self.member.claim_name)
         else:
           raise
     ```
     *(Import `BatchError` from `k8s_agent_sandbox` at top of `batch_claims.py`.)*
     Note that non-`BatchError` exceptions (e.g. `RuntimeError("forbidden")` or `ApiException(403)`) still propagate, preserving `test_teardown_releases_batches_when_a_handle_release_fails` (`tests/test_batch_claims.py:215-225`)!
2. **`tests/test_batch_claims.py`:**
   - Make `FakeBatch.release_member` match `15/N` `SandboxBatch._check_healthy()`:
     ```python
     def release_member(self, member):
       if self.released:
         raise BatchError(f"batch {self.batch_id} has been released")
       if self.error is not None:
         raise self.error
       with self._lock:
         self._members.pop(member.claim_name, None)
       self.released_members.append(member.claim_name)
     ```
   - Update `test_claim_landing_after_teardown_is_released_through_the_batch` (line 267) and `test_stopped_batch_is_replaced_and_old_members_still_release` (line 296) to assert `c.resources.delete_claim` deleted the claim directly (with `not old.sandbox.terminate.called`, `c.active_claims` decremented, and `f.teardown()` succeeding cleanly).

---

### `P0-2`: Use Shared `cluster.core_api` Instead of `exec_core_api()` for REST `read_namespaced_pod_status`

#### Problem
In `agent_sandbox_rl/handles.py`:
- Line `372` (`record_pod_identity()`) and line `408` (`is_alive()`) call `self._cluster.exec_core_api().read_namespaced_pod_status(...)`.
- `Cluster.exec_core_api()` (`cluster.py:84-98`) builds a **brand-new `client.ApiClient` + `urllib3.PoolManager` per OS thread** solely because `kubernetes.stream.stream()` mutates `ApiClient` during WebSocket upgrades (`connect_get_namespaced_pod_exec`).
- Plain REST `GET /status` (`read_namespaced_pod_status`) is 100% thread-safe on `self._cluster.core_api`. Using `exec_core_api()` on REST calls creates hundreds of extra `urllib3` connection pools across worker/executor threads (and in `per_claim` mode runs on every `acquire()`).

#### Required Fix
1. In `agent_sandbox_rl/handles.py` lines `372` and `408`, replace `self._cluster.exec_core_api().read_namespaced_pod_status(...)` with `self._cluster.core_api.read_namespaced_pod_status(...)`.
2. Keep `self._cluster.exec_core_api()` in `handles.py:351` (`exec_in_pod`) and `handles.py:446` (`SandboxSession`) — those use `stream()`.
3. Update the two unit-test mocks in `tests/test_fail_fast.py` (`line 319` and `line 354`: set `cluster.core_api = ...` alongside or instead of `cluster.exec_core_api.return_value`) so all `test_fail_fast.py` and `test_batch_claims.py` tests pass with the exact same `read_pod.call_count` assertions.

---

### `P0-3`: Per-Batch `BatchWatchdog` (`fail_fast` Parity for `claim_mode="batch"`)

#### Problem
In `fleet.py:1531-1538`, `ClaimWatchdog` only runs when `batch_claims is None` (`claim_mode="per_claim"`):
- Why `ClaimWatchdog` couldn't be used per-acquire in `claim_mode="batch"`: `SandboxBatch.acquire()` generates `<batch_id>-<seq>` internally, and spawning 1,000 per-claim polling threads would recreate the exact `N`-GET storm `claim_mode="batch"` eliminates!
- Consequence today: when `claim_mode="batch"` hits a cold start with a broken image or startup crash (`ImagePullBackOff`, `ErrImagePull`, `InvalidImageName`, `CrashLoopBackOff`, `CreateContainerConfigError`), `batch.acquire()` hangs for the entire `effective_claim_timeout()` (`120s`–`600s`) instead of failing fast in ~`10s` with `SandboxStartError` (`reason`, `message`, `pod_name`, `image`) and `obs.claim(cluster, "start_failed")`!

#### Required Design (Zero Overhead on Warm Adoptions!)
Add a lightweight **per-batch `BatchWatchdog`** (1 daemon thread per opened `SandboxBatch` when `fail_fast.enabled` is `True`):
1. **Zero K8s `GET`s on warm adoptions:**
   - Track `first_seen_at: dict[str, float]` for non-`READY` members returned by `batch.members()` (pure in-memory read of `SandboxBatch`'s watch table — `0` HTTP calls!).
   - Prune any `claim_name` from `first_seen_at` as soon as it reaches `MemberState.READY` or disappears from `batch.members()`.
   - Warm-pool adoptions become `READY` in `<1s`, **long before** `fail_fast.initial_delay_s` (`10.0s` default) — so healthy warm runs perform **zero** `CoreV1Api` `GET`s!
2. **Cold-start inspection after `initial_delay_s`:**
   - Every `fail_fast.poll_interval_s` (`5.0s` default), for members still in `MemberState.PENDING` whose `now - first_seen_at[claim_name] >= fail_fast.initial_delay_s`:
     - Resolve `pod_name`: use `member.sandbox_name` if populated on `Member`, else fall back to reading the `SandboxClaim` (`status.sandbox.Name`) via `failfast.py` helpers (same logic `ClaimWatchdog` uses, via shared `cluster.core_api` / `cluster.custom_api` with bounded `_request_timeout`).
     - Run `failfast.classify_pod(pod, ...)` (`failfast.py`).
     - When `classify_pod` returns a terminal `PodVerdict`:
       1. Record `verdicts[claim_name] = verdict` under lock **before** releasing the member.
       2. Call `batch.release_member(member)` (falling back to `cluster.resources.delete_claim(claim_name)` if `BatchError` is raised because the batch stopped). In `15/N` (`batch_state.py:461-462`), `release_member` marks `claim_name` in `self._released`, signals the per-claim `waiter` event immediately in memory (no watch round-trip needed!), wakes `batch.acquire()` with `BatchError(f"acquired member '{claim_name}' was released before it was Ready")`, and deletes the claim under `batch._in_flight` without a duplicate delete in `_drop_acquired`!
3. **Translating into `SandboxStartError` in `fleet.py`:**
   - In `fleet.py:_acquire_reserved`: when `batch_claims.acquire(...)` raises `BatchError` (either `BatchError("acquired member '<claim_name>' was released before it was Ready")` or `TerminalMemberError` with `exc.member.claim_name`), extract `claim_name` and check `batch_claims.pop_verdict(cluster.name, claim_name)`!
   - If a `PodVerdict` is present:
     - Record `self._obs.claim(cluster.name, "start_failed")` (instead of `"error"`).
     - Raise `SandboxStartError(verdict.reason, verdict.message, claim_name=claim_name, pod_name=verdict.pod_name, image=task.image) from exc`!
4. **Lifecycle:** Stop the watchdog thread cleanly whenever a batch is replaced (`_batch_for` sees `batch.err() is not None`) or released (`BatchClaims.release_all()`).
5. **Unit tests (`tests/test_batch_claims.py`):**
   - Test that warm acquires (`READY` before `initial_delay_s`) trigger `0` pod `GET`s even with `fail_fast.enabled=True`.
   - Test that a stuck `CREATING` member with `ImagePullBackOff` pod status wakes `fleet.acquire()` with `SandboxStartError(reason="ImagePullBackOff", ...)` and increments `start_failed` in `RunReport`.
   - Preserve `test_claim_timeout_overrides_ready_timeout_for_batch_claims` (`tests/test_batch_claims.py:161-170`).

---

### `P0-4`: Sweep & Verify Batch `Lease`s in `reaper.py` and `SandboxFleet.verify_teardown()`

#### Problem
Every `SandboxBatch` creates a `coordination.k8s.io/v1` `Lease` carrying `self.config.labels` (including `agent-sandbox-rl/managed-by=agent-sandbox-rl` and `agent-sandbox-rl/run-id=<run_id>`) plus `agent-sandbox.sigs.k8s.io/batch-id=<batch_id>`. Every batch `SandboxClaim` has a `metadata.ownerReferences` entry pointing to that `Lease`.
Currently:
- `Resources` (`agent_sandbox_rl/resources.py`) has no `list_leases` / `delete_lease` helpers (`CoordinationV1Api`).
- `reaper.py` (`reap()` / `reap_orphans()`) sweeps claims, warmpools, templates, sandboxes, pods, and prepull daemonsets — **not** batch `Lease`s. Orphaned Leases linger until `batch_reaper` runs (if deployed), and deleting the `Lease` lets Kubernetes GC clean up owned claims automatically.
- `SandboxFleet.verify_teardown()` checks claims, pools, templates, sandboxes, and pods — **not** batch `Lease`s, so a leaked Lease (and still-running renewal thread!) passes `verify_teardown()` silently.

#### Required Fix
1. **`Cluster` (`cluster.py`) & `Resources` (`resources.py`):**
   - Add lazy or eager `coordination_api = client.CoordinationV1Api(self.api_client)` on `Cluster` and pass it (optional, defaulting to `None` so existing `Resources(custom_api, core_api, ns)` call sites and unit-test fixtures don't break!) into `Resources`.
   - Add `list_leases(label_selector: str)` and `delete_lease(name: str)` to `Resources`:
     - **Safety guard:** refuse empty `label_selector` (`if not label_selector: raise ValueError(...)`) AND add `--` / selector guard `agents.x-k8s.io/batch-id` or the caller's `RUN_ID_LABEL` / `MANAGED_BY_LABEL` selector so `kube-node-lease` or controller leader-election Leases (like `agent-sandbox-controller-leader`) can **never** be matched! Even better: only return Leases that **also** carry the `agent-sandbox.sigs.k8s.io/batch-id` label (`BATCH_ID_LABEL` from `k8s_agent_sandbox.batch_utils`), so non-batch Leases in the namespace are structurally untouchable!
     - `delete_lease` ignores `404`, matching `delete_claim`.
2. **`reaper.py`:**
   - In `_reap_with_cluster`, list and delete matching batch Leases (only for runs NOT protected by `protect_claims_of`!) and include `"leases": deleted_count` in the returned `counts` dict (or `0` when none). Check existing `tests/test_reaper.py` assertions on `counts` keys first so existing reaper tests don't break if they assert exact dict equality! *(Important: inspect `tests/test_reaper.py` before deciding whether `"leases"` is always present or only when `coordination_api` is wired / non-zero — keep 100% backward compatibility with `tests/test_reaper.py`!)*
3. **`SandboxFleet.verify_teardown()` (`fleet.py`):**
   - Include leaked run-scoped batch Leases in `verify_teardown()`'s check so a leaked Lease fails verification cleanly.
4. **Unit tests:** Add unit tests covering Lease reaping (including verifying leader-election Leases without `batch-id` are never touched) and `verify_teardown()` Lease detection.

---

### `P1-5`: Parallelize `SandboxFleet.release_all()` and `SandboxFleet.acquire_batch()`

#### Problem
1. **`SandboxFleet.release_all()` (`fleet.py:1975-1982`):** Loops **serially** (`for h in list(self._handles): self.release(h)`). At `N = 1,000` handles with ~`50–75ms` `DELETE` RTT, serial `release_all()` takes **50–75 seconds** even though `SandboxBatch.release_member` (`_in_flight` semaphore) and Kubernetes API handle concurrent deletes cleanly.
2. **`SandboxFleet.acquire_batch(tasks)` (`fleet.py:1696-1697`):** Runs `[self.acquire(t) for t in tasks]` **serially** (`return [self.acquire(t) for t in tasks]`)! While `AsyncSandboxFleet.acquire_batch` uses `asyncio.gather`, synchronous callers of `SandboxFleet.acquire_batch(tasks)` (such as step-synchronous RL loops) pay `N × RTT` sequentially instead of driving concurrent `batch.acquire()` calls!

#### Required Fix
1. **`SandboxFleet.release_all()`:**
   - Snapshot `handles = list(self._handles)` (for `len <= 1`, keep direct single-thread path so trivial unit tests have zero thread overhead).
   - For `len(handles) > 1`, fan `self.release` out over `ThreadPoolExecutor(max_workers=min(len(handles), max(1, self.config.max_concurrent)))`.
   - **Error semantics:** Wait for **all** submitted `self.release(h)` futures to complete before re-raising the first exception (if any), rather than aborting after the first failure as the serial loop did — that way 1 failed handle release still releases the other `N - 1` handles AND `test_teardown_releases_batches_when_a_handle_release_fails` (`tests/test_batch_claims.py:215-225`) still raises `RuntimeError` and still executes `finally: self._batch_claims.release_all()`!
2. **`SandboxFleet.acquire_batch(tasks)`:**
   - For `len(tasks) == 0`, return `[]`; for `len(tasks) == 1`, return `[self.acquire(tasks[0])]`.
   - For `len(tasks) > 1`, dispatch `self.acquire(t)` across `ThreadPoolExecutor(max_workers=min(len(tasks), max(1, self.config.max_concurrent)))`, **preserving the exact input `tasks` order** in the returned `list[SandboxHandle]`.
   - **Partial-failure rollback:** If one or more `acquire(t)` calls raise, cancel any not-yet-started futures, wait for in-flight futures to finish, **release every handle that succeeded in this `acquire_batch` call** (best-effort, logging warnings on release failure) so a failed `acquire_batch` leaks zero sandboxes/slots, and re-raise the first acquire exception! *(Check `AsyncSandboxFleet.acquire_batch` in `async_fleet.py` as well — verify whether it should share the same clean partial-failure rollback or if any existing test tests partial `acquire_batch` failure.)*
3. **Unit tests:** Add tests verifying:
   - `acquire_batch` runs concurrently (wall time `< 2×` single-acquire sleep for 8 tasks), preserves task order, and rolls back already-acquired handles when task `k` fails.
   - `release_all` runs concurrently, attempts all handles even if one raises, and re-raises the error after draining.

---

### `P1-6`: Auto-Size `Cluster` `connection_pool_maxsize` & Skip Redundant On-Demand `_ensure_pool` RPCs

#### Problem A: `17/N` `check_pool_size` + High-Concurrency `urllib3` Pool Sizing
- In `cluster.py`, `Cluster` constructs `api_client` with a hardcoded or default `connection_pool_maxsize` (check `_build_api_client` in `cluster.py` — currently `1000` or config-driven).
- In `17/N` (`k8s_agent_sandbox/batch_utils.py:check_pool_size`), `claim_batch` checks `if pool_size < max_in_flight + 2: raise ValueError(...)`! If a user configures `BatchClaimConfig(max_in_flight=1000)` (or `FleetConfig(max_concurrent=2000)`), `claim_batch` raises `ValueError` immediately because `1000 < 1002`!

#### Problem B: Redundant K8s RPCs on Dynamic On-Demand Pool Hits (`fleet.py:1501-1511`)
Look at `fleet.py:1501-1511` in `_acquire_reserved`:
```python
if on_demand:
  cluster = self.placement.select(task.image, self.registry)
  pool = self._ensure_pool(cluster, task.image, 1)
  key = (cluster.name, task.image)
  with self._lock:
    created_pool = key not in self._ondemand
    if created_pool:
      self._ondemand.add(key)
```
Every single dynamic `acquire()` for an image **already** in `self._ondemand` still calls `self._ensure_pool(cluster, task.image, 1)` (`GET`/`CREATE` Template + `GET`/`CREATE` WarmPool = **2 extra K8s API calls on every acquire** in Tunix / unplanned dynamic streaming mode)!

#### Required Fix
1. **`connection_pool_maxsize` (`cluster.py` / `fleet.py`):**
   - Ensure `Cluster`'s `api_client.configuration.connection_pool_maxsize` (and `rest_client.pool_manager` if already initialized, or pass desired pool size when `SandboxFleet` wires clusters / builds `ClusterRegistry`) is at least `max(1000, max_concurrent + 32, (batch_claims.max_in_flight or 20) + 16)`.
   - Note: `urllib3.PoolManager(maxsize=N)` reads `configuration.connection_pool_maxsize` **at `ApiClient.__init__` time**! So in `Cluster` (and/or `SandboxFleet.__init__` when `registry` is already constructed), if `api_client.configuration.connection_pool_maxsize < needed`, either set it before `ApiClient` creation (default path when `SandboxFleet` builds `ClusterRegistry` from `config.clusters`) AND if an existing `Cluster` was passed with a smaller `connection_pool_maxsize`, update `configuration.connection_pool_maxsize = needed` (which satisfies `17/N`'s `check_pool_size`, since `read_api_client_pool_size` reads `api_client.configuration.connection_pool_maxsize`!) and rebuild/resize `rest_client.pool_manager` if safe.
2. **On-demand fast path (`fleet.py:1501-1524`):**
   - Check `key = (cluster.name, task.image)` against `self._ondemand` **before** calling `self._ensure_pool(cluster, task.image, 1)` (using a per-`(cluster.name, task.image)` lock so concurrent first acquires for the same unplanned image wait for `_ensure_pool` to finish rather than racing ahead of pool creation, while subsequent acquires skip `_ensure_pool` in `O(1)` memory (`pool = self.config.pool_name(task.image)`) with **0** K8s RPCs!).
   - Keep rollback (`self._ondemand.discard(key)`) on failure when `created_pool` is `True`.
3. **Unit tests:**
   - Test `BatchClaimConfig(max_in_flight=1200)` passes `17/N`'s `check_pool_size` without `ValueError`.
   - Test 10 dynamic `acquire()` calls for the same unplanned image call `_ensure_pool` **once** (not 10 times), including under concurrent threads.

---

### `P2-7`: Raise `AsyncSandboxFleet._thread_pool()` Ceiling for `1k–2k` Scale

#### Problem
In `agent_sandbox_rl/async_fleet.py:73`:
```python
workers = min(1024, max(64, mc + 2 * win + 16))
```
At `max_concurrent=1000` (or `2000` in our 2k burst / pipelined SWE-bench runs), `pipelined` overlaps `max_concurrent` active episodes + `window_size` prefetch + `window_size` unwarm, hitting the `1024` thread cap and queueing `acquire` / `exec` / `release` calls inside Python before they ever reach `SandboxBatch`!

#### Required Fix
1. Raise the cap in `async_fleet.py:73` to `min(4096, max(64, mc + 2 * win + 16))` (ensuring `workers >= min(4096, mc + 64)`).
2. Add a unit test verifying `AsyncSandboxFleet(FleetConfig(max_concurrent=1500))._thread_pool()._max_workers > 1500`.

---

### `P2-8`: Actionable `work_budget` Hint on `SandboxLostError` + `FakeFleet` `claim_expires_at` Parity

#### Problem
1. **`SandboxLostError` diagnostics (`handles.py` / `exceptions.py`):**
   - In `claim_mode="batch"`, every claim gets a controller-enforced `spec.lifecycle.shutdownTime` (`work_budget`, default `7200s`). If a rollout runs past `handle.claim_expires_at`, the controller deletes the claim and pod, and `handle.exec()` raises `SandboxLostError(pod_name, claim_name, cause)` with a generic `"pod died or was evicted"` message — giving the user zero indication that `batch_claims.work_budget` expired!
2. **`FakeFleet` / `FakeHandle` parity (`testing.py`):**
   - On a real `SandboxFleet(FleetConfig(claim_mode="batch"))`, `h.claim_expires_at` is a `float` (`started + effective_claim_timeout() + batch_claims.work_budget + BATCH_SHUTDOWN_MARGIN_SECONDS`), and `None` when `claim_mode="per_claim"` (`tests/test_batch_claims.py:324, 400`).
   - On `FakeFleet(FleetConfig(claim_mode="batch"))`, `h._batch_claim` is `None`, so `h.claim_expires_at` silently returns `None` — breaking RL loops that schedule episode cutoffs or checkpointing before `h.claim_expires_at` when tested against `FakeFleet`!

#### Required Fix
1. **`handles.py` / `exceptions.py`:**
   - In `SandboxHandle._raise_if_lost` (`handles.py:417-431`), check `exp = self.claim_expires_at`: when `exp is not None and time.monotonic() >= exp`, pass `expired=True` (or an explicit hint string) to `SandboxLostError` so `str(err)` clearly states that the batch claim reached its `shutdownTime` (`FleetConfig.batch_claims.work_budget`)! Keep `SandboxLostError(pod_name, claim_name, cause)` positional signature backward-compatible (`expired: bool = False` keyword-only).
2. **`testing.py` (`FakeFleet` / `FakeHandle`):**
   - Allow `FakeHandle` to expose `claim_expires_at` matching `SandboxFleet`: `None` when `claim_mode == "per_claim"`, and `time.monotonic() + config.effective_claim_timeout() + config.batch_claims.work_budget + 60` when `claim_mode == "batch"` (without importing `kubernetes` if `testing.py` avoids heavy imports — note line 41 docstring: *"Importing this module pulls nothing beyond the package itself"*, so use constant `60` or `constants` rather than importing `k8s_agent_sandbox` at module top level in `testing.py`!).
3. **Unit tests:**
   - Test `SandboxLostError` message mentions `work_budget` / `shutdownTime` when `claim_expires_at <= time.monotonic()`, and does **not** mention it when `claim_expires_at > time.monotonic()` or `None`.
   - Test `FakeFleet(FleetConfig(claim_mode="batch")).acquire(t).claim_expires_at is not None` and `FakeFleet(FleetConfig(claim_mode="per_claim")).acquire(t).claim_expires_at is None`, while keeping `testing.py`'s import-isolation test passing!

---

## Part 4: Verification Checklist Before Reporting Done

- [ ] `pytest examples/agent-sandbox-rl/tests` passes 100% (all existing tests + new unit tests for `P0-1` through `P2-8`).
- [ ] `git diff origin/feat/rl-batch-claim-mode --stat` touches **only** files under `examples/agent-sandbox-rl/`.
- [ ] `test_batch_acquire_skips_pod_get_and_is_alive_checks_the_batch_watch` still asserts `read_pod.call_count == 0 and h.pod_uid is None` on batch `acquire()`.
- [ ] `testing.py` still imports nothing outside `agent_sandbox_rl` and stdlib (`test_testing.py` import check passes).
- [ ] Zero internal URLs, internal cloud project names, or non-OSS references anywhere in code, comments, or commit messages.
