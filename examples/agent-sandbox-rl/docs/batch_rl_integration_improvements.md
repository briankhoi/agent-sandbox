# `agent-sandbox-rl` Batch Integration: Rebase & Optimization Plan

This document specifies the remaining integration, correctness, and performance improvements for `FleetConfig(claim_mode="batch")` in `examples/agent-sandbox-rl/agent_sandbox_rl/` after the SDK's parallel `SandboxBatch.release_member()` change lands.

All targets below are anchored by **module path, class/method symbol, and behavioral invariant** (rather than line numbers) so an agent working on a rebased commit can apply each item unambiguously.

---

## Baseline References

- **RL batch integration branch:** `bench/rl-gke` (evaluated at `9f7e4060`)
- **Upstream target (`sigs.k8s.io/agent-sandbox` `main`):** evaluated at `b1277676`, which includes:
  - `#1791` (`c70b415b`): `ClaimWatchdog` fail-fast on cold claims, `SandboxHandle.is_alive()` / `record_pod_identity()`, and `reaper.py` orphan sweeps
  - `#1960` (`f5de668a`): `runtime_container` / `target_container` container-targeted `exec()` and `open_session()`
  - `#1963` (`92f4584e`): `LookaheadWarmer`, `add_tasks()`, `wait_pools_ready()`
  - `#1966` (`dd38b094`): `teardown(verify=True)` and `verify_teardown()`
  - `#1967` (`841cefbb`): `agent_sandbox_rl.testing.FakeFleet` and `FakeHandle`
- **Python SDK batch streaming API (`k8s_agent_sandbox`):** `SandboxBatch` exposing `start_acquire()`, `wait_acquired()`, `acquire()`, `connect()`, `release_member()` (bounded-concurrency parallel `DELETE`s), and `release()` (label-scoped `DELETECOLLECTION` + `Lease` delete).

---

## Core Invariants to Preserve ("Do Not Break")

Any rebase or refactor **must** keep these properties intact (verified in `examples/agent-sandbox-rl/tests/test_batch_claims.py`):

1. **Zero-`GET` steady-state `acquire()` in `claim_mode="batch"`:** `SandboxFleet._acquire_reserved()` builds `SandboxHandle` directly from `claim.batch.connect(claim.member)` and `select_pod_ip(claim.member.pod_ips)` — issuing **zero** `GET Sandbox` or `GET Pod` requests per acquired sandbox.
2. **Lazy size-0 batch opening per cluster:** `BatchClaims._batch_for()` opens `[BatchGroup(warmpool=pool, size=0)]` on the first acquire for a cluster (avoiding `2 × P` upfront `GET`s across all planned pools) and rotates onto a fresh `SandboxBatch` if `batch.err() is not None` while retaining old batches in `_opened` so teardown releases every `Lease`.
3. **Per-claim fallback for pod-pinned acquires:** `acquire(task, snapshot_uid=...)` and `acquire(task, pod_annotations=...)` fall back cleanly to `create_sandbox()` (`_batch_claim=None`) because `SandboxBatch.acquire()` does not carry per-claim pod metadata.
4. **Proactive `shutdownTime` rotation under `recycle=True`:** `recycle._claim_expiring()` rotates batch-backed handles before `handle.claim_expires_at` so a recycled sandbox is never deleted mid-task by the controller.
5. **Teardown race safety:** Flipping `SandboxFleet._torndown` and `BatchClaims._closed` under locks ensures an in-flight `acquire()` racing `teardown()` never leaks an unreleased claim or a renewing `Lease`.

---

## Prioritized Improvements

### P0 — Required When Rebasing onto `upstream/main`

#### P0-1. Keep `acquire()` strictly 0-`GET` alongside `#1791` `record_pod_identity()` and `#1960` `runtime_container`

- **Modules & symbols:**
  - `agent_sandbox_rl/fleet.py`: `SandboxFleet._acquire_reserved()`
  - `agent_sandbox_rl/handles.py`: `SandboxHandle.record_pod_identity()`, `SandboxHandle.is_alive()`, `SandboxHandle.exec()`, `SandboxHandle.open_session()`
- **Problem:**
  1. On `upstream/main`, `#1791` added an unconditional `handle.record_pod_identity()` call inside `SandboxFleet._acquire_reserved()`. `record_pod_identity()` performs a synchronous `GET /api/v1/namespaces/{ns}/pods/{pod}/status` per sandbox. Merging that unconditionally into `claim_mode="batch"` re-introduces **`N` unthrottled per-pod `GET` requests on every step's `acquire()` burst** (even for workloads talking over HTTP/Router that never call `kubectl exec`).
  2. Both `record_pod_identity()` and `is_alive()` call `self._cluster.exec_core_api()`, which allocates a **brand-new thread-local `kubernetes.client.ApiClient` + `urllib3` pool per OS thread**. Thread-local `ApiClient` isolation is only needed for `kubernetes.stream()` websocket upgrades (which mutate client state), *not* standard REST `GET` calls (`self._cluster.core_api` is thread-safe and shares the cluster's main connection pool).
  3. `#1960` added `runtime_container` to `SandboxHandle(...)` construction in `_acquire_reserved()`; batch-acquired handles need the same `runtime_container` wiring.
- **Required change:**
  1. Pass `runtime_container=((entry.runtime_container if entry is not None else None) or constants.RUNTIME_CONTAINER)` when constructing `SandboxHandle` for both `batch` and `per_claim` paths.
  2. Use `self._cluster.core_api.read_namespaced_pod_status(...)` (not `exec_core_api()`) inside `record_pod_identity()` and `is_alive()`.
  3. In `claim_mode="batch"`, **do not** issue `record_pod_identity()` eagerly inside `_acquire_reserved()`. Instead, record pod identity **lazily on first `exec()` or `open_session()`** (when `self.pod_uid is None and not self._identity_attempted`, reset on `refresh()`), while keeping explicit `handle.record_pod_identity()` working when called directly. (Making lazy recording the default or skipping eager `GET` in `batch` mode preserves 0-`GET` `acquire()` while still capturing baseline `pod_uid` and `runtime_restarts` before any command runs.)
- **Tests to add/update:**
  - Assert in `tests/test_batch_claims.py` that `fleet.acquire(task)` makes **zero** calls to `core_api.read_namespaced_pod*` and `exec_core_api()`.
  - Assert that the first `handle.exec(...)` / `handle.open_session()` records `pod_uid` / `runtime_restarts` via `cluster.core_api` so subsequent OOM/recreate detection in `_raise_if_lost()` / `is_alive()` still raises `SandboxLostError`.

---

#### P0-2. Wire `#1791` `ClaimWatchdog` and `effective_claim_timeout()` into `BatchClaims.acquire()`

- **Modules & symbols:**
  - `agent_sandbox_rl/batch_claims.py`: `BatchClaims.__init__()`, `BatchClaims._batch_for()`, `BatchClaims.acquire()`
  - `agent_sandbox_rl/fleet.py`: `SandboxFleet.__init__()`, `SandboxFleet._acquire_reserved()`
  - `agent_sandbox_rl/failfast.py`: `ClaimWatchdog`
- **Problem:**
  1. On `upstream/main`, `SandboxFleet` uses `self.config.effective_claim_timeout()` (`fail_fast.claim_timeout` = `180s` when `fail_fast.enabled`, else `ready_timeout`) and wraps `create_sandbox()` in `ClaimWatchdog(cluster, claim_name, self.config.fail_fast)`. When a cold pod hits `ImagePullBackOff`, `ErrImageNeverPull`, `InvalidImageName`, or `Unschedulable`, `ClaimWatchdog` deletes the `SandboxClaim` early and `_acquire_reserved()` raises `SandboxStartError(reason, message, ...)` instead of hanging until timeout.
  2. In `claim_mode="batch"`, `SandboxBatch` generates `<batch_id>-<ordinal>` claim names internally, so `_acquire_reserved()` did not know `claim_name` ahead of `batch.acquire()`, and `BatchClaims` only knew `ready_timeout`.
- **Required change:**
  1. Separate **batch `quorum_timeout`** (passed to `claim_batch()`, which contributes to `SandboxClaim.spec.lifecycle.shutdownTime` and `BatchClaim.expires_at`) from **per-acquire wait timeout** (`config.effective_claim_timeout()`):
     - Keep `quorum_timeout=config.ready_timeout` in `claim_batch(...)` and compute `expires_at = started + quorum_timeout + work_budget + BATCH_SHUTDOWN_MARGIN_SECONDS` from that exact `quorum_timeout` so `claim_expires_at` always matches the controller's `shutdownTime`.
     - Pass `acquire_timeout=config.effective_claim_timeout()` to the per-member wait.
  2. In `BatchClaims.acquire(cluster, pool, *, task_image=..., fail_fast=...)`, when `fail_fast.enabled` is true and the batch supports `start_acquire` + `wait_acquired` (fall back to `batch.acquire()` for minimal test doubles if needed, or add `start_acquire`/`wait_acquired` to `FakeBatch`):
     - Call `mh = batch.start_acquire(pool)` to obtain `mh.claim_name` immediately.
     - Start `watchdog = ClaimWatchdog(cluster, mh.claim_name, fail_fast)`.
     - Call `member = batch.wait_acquired(mh, timeout=acquire_timeout)` inside `try ... finally: watchdog.stop()`.
     - If `watchdog.verdict` is non-None (either `wait_acquired` raised `BatchError`/`TerminalMemberError` because the watchdog deleted the claim, or `watchdog.stop()` returned a verdict in the completion race window), release the member if needed, record `self._obs.claim(cluster.name, "start_failed")`, and raise `SandboxStartError(verdict.reason, verdict.message, claim_name=mh.claim_name, pod_name=verdict.pod_name, image=task.image)` — matching `per_claim` behavior 1:1.
- **Tests to add/update:**
  - Unit test in `tests/test_batch_claims.py` (or `tests/test_fail_fast.py`) verifying that a stuck cold batch member caught by `ClaimWatchdog` raises `SandboxStartError` and increments `asrl_claims_total{status="start_failed"}`.
  - Unit test verifying `claim_expires_at` matches `quorum_timeout + work_budget + BATCH_SHUTDOWN_MARGIN_SECONDS` even when `fail_fast.claim_timeout < ready_timeout`.

---

#### P0-3. Sweep and verify batch `Lease`s in `reaper.py` (`#1791`) and `verify_teardown()` (`#1966`)

- **Modules & symbols:**
  - `agent_sandbox_rl/reaper.py`: `_reap_with_cluster()`, `find_orphan_runs()`, `reap_orphans()`
  - `agent_sandbox_rl/fleet.py`: `SandboxFleet.verify_teardown()`
  - `agent_sandbox_rl/cluster.py` / `agent_sandbox_rl/resources.py`: `CoordinationV1Api` helper methods (`list_leases`, `delete_leases`)
- **Problem:**
  - `BatchClaims._batch_for()` stamps `self._labels` (`agent-sandbox-rl/run-id=<run_id>`, `app.kubernetes.io/managed-by=agent-sandbox-rl`) onto both every `SandboxClaim` and its owning `coordination.k8s.io/v1` `Lease`.
  - However, `reaper.py` currently sweeps `SandboxClaim`s, `SandboxWarmPool`s, `Sandbox`es, `SandboxTemplate`s, and `Pod`s — it does **not** sweep `Lease`s. On clusters that do not deploy the optional standalone `batch_reaper` controller/cron, an orphaned driver's expired `Lease` CRs remain in etcd indefinitely, and `teardown(verify=True)` (`#1966`) does not check that batch `Lease`s are gone.
- **Required change:**
  1. Add thin label-scoped `Lease` list/delete helpers using `CoordinationV1Api(cluster.api_client)` (scoped strictly by `label_selector` so system leases like `kube-node-lease` or controller leader-election leases — which never carry `app.kubernetes.io/managed-by=agent-sandbox-rl` — cannot ever be matched).
  2. In `reaper._reap_with_cluster()`, delete matching `Lease`s alongside `SandboxClaim`s (deleting the `Lease` also triggers Kubernetes `ownerReferences` GC on any batch claims) and include `"leases"` in the returned count dict.
  3. Include labeled `Lease`s in `find_orphan_runs()` discovery and `SandboxFleet.verify_teardown()`'s `"remaining"` check.
- **Tests to add/update:**
  - Extend `tests/test_teardown_verify.py` and reaper tests in `tests/test_fail_fast.py` / `tests/test_batch_claims.py` to assert orphaned batch `Lease`s are counted and deleted by `reap(run_id=...)` and verified clean by `teardown(verify=True)`.

---

### P1 — High-Impact Throughput, Teardown & Connection-Pool Fixes

#### P1-4. Eliminate redundant `O(N)` per-handle `DELETE` storm in `SandboxFleet._teardown()`

- **Modules & symbols:**
  - `agent_sandbox_rl/fleet.py`: `SandboxFleet._teardown()`, `SandboxFleet.release()`, `SandboxFleet.release_all()`
- **Problem:**
  - Currently `_teardown()` runs:
    ```python
    try:
      self.release_all()
    finally:
      if self._batch_claims is not None:
        self._batch_claims.release_all()
    ```
  - When `N` batch-backed handles (`h._batch_claim is not None`) are still open at `teardown()`, `self.release_all()` sends **`N` individual `DELETE /sandboxclaims/<name>` requests** (`batch.release_member()`) right before `self._batch_claims.release_all()` calls `batch.release()`, which **already deletes all batch claims in 1 single label-scoped `DELETECOLLECTION` call** + deletes the `Lease`! At `N = 1,000`, teardown wastes `1,000` API server `DELETE` calls and tens of seconds of APF budget on claims that the very next line bulk-deletes in ~50 ms.
- **Required change:**
  - During `_teardown()`, partition `list(self._handles)` into:
    1. **Batch-backed handles (`h._batch_claim is not None`):** Do **not** call `h._batch_claim.release()` individually. Instead, close any open exec websocket (`h.close_session()`), remove `h` from `self._handles`, and decrement local slot counters (`c.release_claim()` and `self._claims_reserved` if `not h._slot_released`), then let `self._batch_claims.release_all()` bulk-delete all claims and `Lease`s in `finally:`.
    2. **Non-batch handles (`h._batch_claim is None`, i.e. `claim_mode="per_claim"` or `snapshot_uid` / `pod_annotations` fallbacks):** Release them normally (and concurrently — see P1-5) while keeping `self._batch_claims.release_all()` in `finally:` so a `per_claim` termination error never skips batch cleanup.
- **Tests to add/update:**
  - Update `tests/test_batch_claims.py`:
    - Verify `fleet.teardown()` with `N` live batch handles closes open sessions, zeroes `active_claims` / `_claims_reserved`, calls `batch.release()` on every opened batch, and issues **zero** `batch.release_member()` calls.
    - Verify `fleet.teardown()` with a mix of batch handles and a `pod_annotations`-pinned `per_claim` handle still calls `terminate()` on the `per_claim` handle *and* still calls `batch.release()` in `finally:` even if `terminate()` raises.

---

#### P1-5. Parallelize `SandboxFleet.release_all()` and `SandboxFleet.acquire_batch()`

- **Modules & symbols:**
  - `agent_sandbox_rl/fleet.py`: `SandboxFleet.release_all()`, `SandboxFleet.acquire_batch()`
- **Problem:**
  - Today, `release_all()` is `for h in list(self._handles): self.release(h)` and `acquire_batch(tasks)` is `[self.acquire(t) for t in tasks]`. Both execute **strictly sequentially on 1 thread**.
  - Even with bounded-concurrency parallel `release_member()` in the Python SDK, any synchronous RL step loop calling `handles = fleet.acquire_batch(step_tasks)` and `fleet.release_all()` only drives **1 in-flight request at a time**, turning 1,000-sandbox step transitions into 1,000 sequential round-trips.
- **Required change:**
  1. **`release_all()`:** Snapshot `handles = list(self._handles)`. For `len(handles) <= 1`, release inline; for `len(handles) > 1`, fan `self.release(h)` out across a `ThreadPoolExecutor(max_workers=min(len(handles), max(1, self.config.max_concurrent)))`. Attempt **every** handle's release before re-raising the first exception (if any) so one failed delete does not leave the remaining `N - 1` handles unreleased.
  2. **`acquire_batch(tasks)`:** For `len(tasks) <= 1`, acquire inline; for `len(tasks) > 1`, fan `self.acquire(t)` out across `ThreadPoolExecutor(max_workers=min(len(tasks), max(1, self.config.max_concurrent)))`, returning handles in the exact input `tasks` order. **Partial-failure rollback:** if any `acquire(t)` raises, release the handles that *did* succeed in this `acquire_batch()` call before re-raising so the caller never leaks unreachable handles from a failed batch acquire.
- **Tests to add/update:**
  - Unit tests verifying `acquire_batch()` preserves task order, executes concurrently up to `max_concurrent`, and releases already-acquired handles from that call if one task fails.
  - Unit tests verifying `release_all()` executes concurrently and still releases all other handles when one handle's `release()` raises.

---

#### P1-6. Auto-size `Cluster` `connection_pool_maxsize` for `BatchClaimConfig.max_in_flight` and lock `_leases_injected`

- **Modules & symbols:**
  - `agent_sandbox_rl/cluster.py`: `build_api_client()`, `Cluster.__init__()`, `Cluster.batch_sandbox_client()`
  - `agent_sandbox_rl/fleet.py`: `SandboxFleet._default_registry()`
- **Problem:**
  1. `build_api_client()` hardcodes `pool_maxsize: int = 1000`. Meanwhile, the Python SDK's `claim_batch()` validates `check_pool_size(pool_maxsize, max_in_flight)` requiring `pool_maxsize >= max_in_flight + 2`. If a user configures `BatchClaimConfig(max_in_flight=1000)` (a natural setting on a 1,000-sandbox benchmark), `claim_batch()` fails immediately with `ValueError` (`1000 < 1002`). Similarly, `FleetConfig(max_concurrent=1500)` exceeds the `1000` pool ceiling.
  2. In `Cluster.batch_sandbox_client()`, `self._leases_injected` is checked and mutated without a lock (relying on callers like `BatchClaims` holding an external lock). Locking inside `batch_sandbox_client()` makes `Cluster` thread-safe on its own.
- **Required change:**
  1. Compute required pool size in `SandboxFleet._default_registry(config)` (and accept `pool_maxsize` on `Cluster` / `ClusterRegistry.from_configs`):
     `pool_maxsize = max(1000, config.max_concurrent + 32, (config.batch_claims.max_in_flight or 0) + 16)`.
  2. Guard `_leases_injected` inside `Cluster.batch_sandbox_client()` under `self._k8s_helper_lock` (note: access `k8s_helper = self.k8s_helper` first or acquire `_k8s_helper_lock` cleanly without deadlock since `threading.Lock` is non-reentrant).
- **Tests to add/update:**
  - Unit test verifying `FleetConfig(claim_mode="batch", batch_claims=BatchClaimConfig(max_in_flight=1000))` builds clusters with `connection_pool_maxsize >= 1002`.
  - Concurrent `batch_sandbox_client()` unit test in `tests/test_cluster.py` / `tests/test_batch_claims.py`.

---

### P2 — Async Scale, Long-Episode Ergonomics & Test-Double Parity

#### P2-7. Prevent `AsyncSandboxFleet._thread_pool()` from self-throttling at `max_concurrent >= 1000`

- **Modules & symbols:**
  - `agent_sandbox_rl/async_fleet.py`: `AsyncSandboxFleet._thread_pool()`
- **Problem:**
  - `AsyncSandboxFleet._thread_pool()` currently computes `workers = min(1024, max(64, mc + 2 * win + 16))`.
  - Because `AsyncSandboxFleet` delegates `acquire()` to sync `SandboxFleet.acquire()` via `run_in_executor`, each in-flight `SandboxBatch.acquire()` blocks 1 executor thread on `threading.Event.wait()` until Ready. At `max_concurrent = 1000` (plus `pipelined` background `warm_images`/`unwarm_images` threads), `1024` workers saturates — and at `max_concurrent = 1500+`, hundreds of `acquire()` coroutines sit queued in Python before `start_acquire()` is ever called.
- **Required change:**
  - Do not clamp `workers` below `mc + 64`: e.g. `workers = max(64, min(4096, max(mc + 64, min(1024, mc + 2 * win + 16))))` (so normal runs stay `<= 1024`, while explicit `max_concurrent >= 1000` gets `mc + 64` headroom up to `4096`).
  - *(Optional follow-up note, out of scope for this patch unless desired: wiring `AsyncSandboxFleet` directly to the SDK's `AsyncSandboxBatch` would allow `10,000+` concurrent acquires with 0 blocked threads.)*
- **Tests to add/update:**
  - Unit test in `tests/test_async_fleet.py` asserting `_thread_pool()._max_workers >= config.max_concurrent + 64` when `max_concurrent=1200`.

---

#### P2-8. Self-diagnosing `SandboxLostError` and docs for `BatchClaimConfig.work_budget` expiry

- **Modules & symbols:**
  - `agent_sandbox_rl/handles.py`: `SandboxHandle._raise_if_lost()`
  - `agent_sandbox_rl/config.py`: `FleetConfig`, `BatchClaimConfig` docstrings
  - `agent_sandbox_rl/fleet.py`: `SandboxFleet.suspend()` docstring
- **Problem:**
  - `claim_mode="per_claim"` creates `SandboxClaim`s with **no** `shutdownTime`, whereas `claim_mode="batch"` sets `spec.lifecycle.shutdownTime` (`work_budget=3600`s + `quorum_timeout` + `600s` margin).
  - `recycle=True` rotates handles proactively before `claim_expires_at`, **but** a single non-recycled multi-hour trajectory — or a sandbox held in `suspend()` across several hours — will reach `shutdownTime` and have its `SandboxClaim` deleted by the controller. Without a clear hint, that surfaces as a mysterious pod 404 / generic `SandboxLostError`.
- **Required change:**
  1. In `SandboxHandle._raise_if_lost(cause)`, when `not alive` and `self.claim_expires_at is not None and time.monotonic() >= self.claim_expires_at`, include an actionable reason in `SandboxLostError` noting that the batch claim passed its `BatchClaimConfig.work_budget` (`shutdownTime`) and advising `BatchClaimConfig(work_budget=...)` (or `recycle=True`).
  2. Note `work_budget` lifetime semantics briefly in the docstrings of `FleetConfig.claim_mode`, `BatchClaimConfig.work_budget`, and `SandboxFleet.suspend()`.
- **Tests to add/update:**
  - Unit test asserting that an expired batch handle whose pod returns 404 on `exec()` raises `SandboxLostError` mentioning `work_budget`.

---

#### P2-9. `FakeFleet` / `FakeHandle` (`#1967`) parity with `claim_mode="batch"`

- **Modules & symbols:**
  - `agent_sandbox_rl/testing.py`: `FakeFleet`, `FakeHandle`
  - `examples/agent-sandbox-rl/tests/test_testing_fake_fleet.py`
- **Problem:**
  - `#1967` added `FakeFleet` and `FakeHandle` along with strict signature/property parity tests in `tests/test_testing_fake_fleet.py` checking `FakeFleet` and `FakeHandle` against `SandboxFleet` and `SandboxHandle`.
  - When a framework tests against `FakeFleet(FleetConfig(claim_mode="batch", batch_claims=BatchClaimConfig(work_budget=...)))`, `FakeHandle.claim_expires_at` remains `None` unless `FakeFleet.acquire()` sets an expiry timestamp on the fake handle.
- **Required change:**
  1. Ensure `FakeHandle` exposes `claim_expires_at` (matching `SandboxHandle.claim_expires_at`) and, when `config.claim_mode == "batch"` (and no `snapshot_uid` / `pod_annotations` pin), sets `claim_expires_at = time.monotonic() + config.ready_timeout + config.batch_claims.work_budget + BATCH_SHUTDOWN_MARGIN_SECONDS`.
  2. Ensure `tests/test_testing_fake_fleet.py` signature checks include any updated signatures/properties and pass cleanly.
- **Tests to add/update:**
  - Add a test in `tests/test_testing_fake_fleet.py` verifying `FakeFleet(FleetConfig(claim_mode="batch"))` populates `handle.claim_expires_at` while default `per_claim` leaves it `None`.

---

## Verification Checklist for Implementing Agent

1. **Rebase cleanly** onto `upstream/main` + the Python SDK `15/N` branch (including parallel `release_member()`).
2. **Run Python SDK + RL unit tests and type checks:**
   - Python SDK: `pytest clients/python/agentic-sandbox-client` and `mypy clients/python/agentic-sandbox-client/k8s_agent_sandbox`
   - RL package: `pytest examples/agent-sandbox-rl/tests`
   - Or run the repo-wide harness: `make test-unit`
3. **Code conventions:**
   - Keep Python `>= 3.11` compatibility and Apache-2.0 headers on any new `.py` files.
   - Match the existing 2-space indentation style in `examples/agent-sandbox-rl/agent_sandbox_rl/` (do not run auto-formatters on unrelated lines).
   - Keep all Prometheus label values bounded-cardinality (`status="start_failed"` / `"ok"` / `"error"`).
