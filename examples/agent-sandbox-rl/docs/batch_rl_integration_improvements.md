# `agent-sandbox-rl` Batch Integration: Rebase & Optimization Specification

This document is the complete, self-contained specification for **Workstream B**: rebasing `examples/agent-sandbox-rl/` onto `upstream/main` and implementing all remaining correctness, fail-fast, cleanup, and high-concurrency optimizations for `FleetConfig(claim_mode="batch")`.

Every requirement below is anchored by **module path, class/method symbol, exact SDK contract, and behavioral invariant** (never brittle line numbers) so an implementing agent can execute it cleanly in an isolated worktree without modifying files outside `examples/agent-sandbox-rl/`.

---

## 1. Scope & Workspace Isolation Rules

1. **Strict directory boundary:** Only modify files under `examples/agent-sandbox-rl/`. **Do NOT modify `clients/python/agentic-sandbox-client/`** (another agent owns the Python SDK `15/N` branch stack in parallel).
2. **Isolated git worktree & branch:**
   - Do **not** work inside `/usr/local/google/home/brianknguyen/agent-sandbox/.claude/worktrees/rl-gke` or any existing SDK review worktree (`agent-sandbox-r1989`, `agent-sandbox-restack`, etc.).
   - Create a fresh git worktree and branch (for example `feat/rl-batch-improvements`) starting from `bench/rl-gke` and rebase/merge `upstream/main` (`6342c063` or newer) into `examples/agent-sandbox-rl/`.
3. **OSS hygiene:** Keep Apache-2.0 headers on any new `.py` files, Python `>= 3.11` compatibility, 2-space indentation matching `examples/agent-sandbox-rl/agent_sandbox_rl/`, bounded-cardinality Prometheus labels, and zero internal URLs or project IDs.

---

## 2. Exact Baseline & SDK Contracts

### 2.1 `upstream/main` Commits Merged Since `bench/rl-gke` Branched

Rebasing `bench/rl-gke` onto `upstream/main` (`6342c063`) brings in 8 feature/fix PRs under `examples/agent-sandbox-rl/`:

| PR / Commit | Summary & Key Symbols Introduced |
|---|---|
| `#1791` (`c70b415b`) | Fail-fast `ClaimWatchdog` + `classify_pod` + `StartVerdict` (`failfast.py`), `FailFastPolicy` + `FleetConfig.effective_claim_timeout()` (`config.py`), `SandboxStartError` + `SandboxLostError` (`exceptions.py`), `SandboxHandle.record_pod_identity()` + `is_alive()` + `_raise_if_lost()` (`handles.py`), `forget_claim_in_client()` (`handles.py`), and `find_orphan_runs()` + `reap_orphans()` (`reaper.py`). |
| `#1961` (`10c3f700`) | Hardened pod defaults, resource limits, task-image sidecars, extra template spec (`config.py`, `constants.py`, `resources.py`). |
| `#1960` (`f5de668a`) | Multi-container exec: `SandboxHandle.runtime_container`, `target_container`, `exec(..., *, container=None)`, `open_session(..., *, container=None)`. |
| `#1962` (`8c999a5b`) | OpenHands agent-server preset (`adapters/openhands.py`). |
| `#1963` (`92f4584e`) | `LookaheadWarmer` (`lookahead.py`), `SandboxFleet.add_tasks()`, `SandboxFleet.wait_pools_ready()`. |
| `#1964` (`f41cf22f`) | Dataset rows & image manifests as task sources (`sources.py`, `load_tasks(..., image_field=..., id_field=...)`). |
| `#1965` (`99100ae7`) | Batched self-cleaning `prepull` with optional page-cache drop (`prepull.py`). |
| `#1966` (`dd38b094`) | `SandboxFleet.teardown(delete_namespace=False, *, verify=False)`, `SandboxFleet.verify_teardown()`, `self._sweep_lock`, `self._deleted_namespaces`. |
| `#1967` (`841cefbb`) | In-memory test doubles `FakeFleet`, `FakeHandle`, `FakeSandboxGone` (`testing.py` + strict signature parity assertions in `tests/test_testing_fake_fleet.py`). |
| `#2035` (`7f40dc1a`) | `Cluster._sandbox_client_lock` / `_k8s_helper_lock` (`cluster.py`, `snapshots.py` — already mirrored in `bench/rl-gke`; resolve cleanly in favor of keeping `Cluster.batch_sandbox_client()` alongside `#2035`). |

### 2.2 Exact Python SDK (`k8s_agent_sandbox`) Batch Surface

Do **not** assume `start_acquire` or `wait_acquired` are public methods on `SandboxBatch` — in `k8s_agent_sandbox/sandbox_batch.py`, `_state.start_acquire` and `_wait_acquired` are private. Rely **only** on these public SDK APIs and types:

- `SandboxClient.claim_batch(groups, *, namespace=..., labels=..., create_rps=..., max_in_flight=..., work_budget=..., quorum_timeout=..., lease_duration=...) -> SandboxBatch`
- `SandboxBatch.acquire(warmpool: str, timeout: float | None = None, *, pod_annotations: dict[str, str] | None = None) -> Member`
  - **Nuance (`15/N` `feat/batch-11-acquire-annotations`):** On the `15/N` tip, `SandboxBatch.acquire()` accepts keyword-only `pod_annotations: dict[str, str] | None = None`. On the slightly earlier SDK commit currently in `bench/rl-gke`, `acquire(warmpool, timeout=None)` did not yet have `pod_annotations`. Detect support dynamically via `inspect.signature(batch.acquire).parameters` (see **P1-7** below) so `agent_sandbox_rl` works seamlessly with both!
  - Each acquired claim's `spec.lifecycle.shutdownTime` is always `create_time + self._quorum_timeout + self._work_budget + BATCH_SHUTDOWN_MARGIN_SECONDS` (note: it uses the batch's `quorum_timeout`, **not** the per-call `acquire(..., timeout=...)` argument!).
- `SandboxBatch.members(warmpool: str | None = None) -> list[Member]`
  - Thread-safe, zero-RPC in-memory snapshot from the batch's shared watch. Includes `MemberState.PENDING` claims currently waiting inside `acquire()`, with `member.claim_name` (`<batch_id>-<ordinal>`) immediately present and `member.sandbox_name` populated as soon as the controller binds a `Sandbox` to the `SandboxClaim`.
- `SandboxBatch.connect(member: Member) -> Sandbox`
  - Zero-`GET` construction seeded with `select_pod_ip(member.pod_ips)`.
- `SandboxBatch.release_member(member: Member) -> None`
  - Deletes a single member's `SandboxClaim` (parallelized up to `max_in_flight` in `15/N`). Raises `BatchError` if detached/released or if `batch.err()` is set.
- `SandboxBatch.release() -> None`
  - Idempotent. Stops creates/watch/Lease-renewal, closes cached connections, deletes **all** batch `SandboxClaim`s via label-scoped `DELETECOLLECTION` rounds (`agents.x-k8s.io/batch-id=<batch_id>`), and deletes the batch `Lease`.
- `SandboxBatch.err() -> Exception | None`
- Exceptions in `k8s_agent_sandbox.exceptions`:
  - `BatchError` (base class)
  - `BatchTimeoutError(BatchError, TimeoutError)` (already a `TimeoutError` subclass)
  - `TerminalMemberError(BatchError)` — raised by `acquire()` when a member fails terminally (`MemberState.FAILED`, e.g. `CreateFailed`) or its claim is deleted externally before Ready (`MemberState.LOST`); exposes **`exc.member: Member`** (`exc.member.claim_name`, `exc.member.warmpool`, `exc.member.state`, `exc.member.reason`, `exc.member.message`).

---

## 3. Core Invariants to Preserve ("Do Not Break")

1. **Zero-`GET` steady-state `acquire()` in `claim_mode="batch"`:** `SandboxFleet._acquire_reserved()` builds `SandboxHandle` from `claim.batch.connect(claim.member)` and `select_pod_ip(claim.member.pod_ips)` with **zero** `GET Sandbox` or `GET Pod` calls on the `acquire()` critical path.
2. **Lazy size-0 batch opening & automatic error rotation:** `BatchClaims._batch_for()` opens `[BatchGroup(warmpool=pool, size=0)]` lazily on the first acquire per cluster (avoiding `2 × P` upfront sequential `GET`s across all planned pools), and if `batch.err() is not None`, opens a fresh `SandboxBatch` for subsequent acquires while keeping the stopped batch in `_opened` so `teardown()` still calls `batch.release()` on it.
3. **Proactive `shutdownTime` rotation under `recycle=True`:** `recycle._claim_expiring()` rotates batch-backed handles before `handle.claim_expires_at` so the controller never deletes a recycled sandbox mid-task.
4. **Teardown race safety:** Flipping `SandboxFleet._torndown` and `BatchClaims._closed` under locks guarantees an `acquire()` racing `teardown()` never leaks an untracked claim or an open renewing `Lease`.

---

## 4. Detailed Implementation Items (`P0` – `P2`)

### P0 — Rebasing, Zero-`GET` Preservation, Fail-Fast & Reaper Parity

#### P0-1. Preserve 0-`GET` `acquire()` with `#1791` `record_pod_identity()` and wire `#1960` `runtime_container`

- **Target symbols:**
  - `agent_sandbox_rl/fleet.py`: `SandboxFleet._acquire_reserved()`
  - `agent_sandbox_rl/handles.py`: `SandboxHandle`, `SandboxHandle.record_pod_identity()`, `SandboxHandle.is_alive()`, `SandboxHandle.exec()`, `SandboxHandle.open_session()`, `SandboxHandle.refresh()`
- **Nuance & problem:**
  1. In `#1791`, `_acquire_reserved()` unconditionally calls `handle.record_pod_identity()` right after creating the sandbox. `record_pod_identity()` performs a synchronous `GET /api/v1/namespaces/{ns}/pods/{pod}/status`. Calling that eagerly on `claim_mode="batch"` adds **`N` unthrottled per-pod `GET`s** right back onto every `acquire()` burst (destroying the 0-`GET` batch fast path, even for HTTP/Router workloads that never call `kubectl exec`).
  2. In `#1791`, both `record_pod_identity()` and `is_alive()` call `self._cluster.exec_core_api().read_namespaced_pod_status(...)`. `exec_core_api()` builds a **separate `kubernetes.client.ApiClient` + `urllib3` pool per thread** because `kubernetes.stream()` websocket upgrades mutate `ApiClient`. Standard REST `GET`s on `self._cluster.core_api` are completely thread-safe and share the cluster's main connection pool — calling `exec_core_api()` for REST `GET`s needlessly allocates hundreds of extra connection pools and TLS handshakes.
  3. `#1960` added `runtime_container` to `SandboxHandle(...)` in `_acquire_reserved()`. Batch-acquired handles need the exact same `runtime_container` wiring.
- **Design:**
  1. Pass `runtime_container=((entry.runtime_container if entry is not None else None) or constants.RUNTIME_CONTAINER)` when constructing `SandboxHandle` for both `batch` and `per_claim` paths.
  2. Switch `record_pod_identity()` and `is_alive()` to use `self._cluster.core_api.read_namespaced_pod_status(...)` instead of `exec_core_api()`.
  3. Add `_identity_recorded: bool = field(default=False, repr=False)` on `SandboxHandle`:
     - In `claim_mode="per_claim"`, `_acquire_reserved()` may continue calling `handle.record_pod_identity()` eagerly (preserving every existing `#1791` unit test in `tests/test_fail_fast.py` untouched!).
     - In `claim_mode="batch"`, **skip** calling `handle.record_pod_identity()` inside `_acquire_reserved()` (keeping batch `acquire()` strictly 0-`GET`!). Instead, in `SandboxHandle.exec()` and `SandboxHandle.open_session()`, if `not self._identity_recorded and self._cluster is not None`, call `self.record_pod_identity()` **before** opening the exec stream / running the command.
     - Set `self._identity_recorded = True` inside `record_pod_identity()` (even if the best-effort `GET` fails, so every `exec()` on an unreachable/mocked pod doesn't retry it repeatedly), and reset `self._identity_recorded = False` (or call `self.record_pod_identity()`) inside `SandboxHandle.refresh()` after a snapshot `resume()` / `restore()` swaps pods.
- **Required tests:**
  - In `tests/test_batch_claims.py`: assert `fleet.acquire(task)` in `claim_mode="batch"` makes **zero** calls to `core_api.read_namespaced_pod_status` and `exec_core_api`, and sets `handle.runtime_container` properly (including adopted-pool custom `runtime_container`).
  - Assert that the first `handle.exec(...)` / `handle.open_session()` on a batch handle lazily records `pod_uid` and `runtime_restarts` via `cluster.core_api`, so a subsequent pod replacement or container restart causes `is_alive()` to return `False` and a failed `exec()` to raise `SandboxLostError`.

---

#### P0-2. Per-batch `BatchWatchdog` for `#1791` fail-fast parity (`ClaimWatchdog` + `effective_claim_timeout()`)

- **Target symbols:**
  - `agent_sandbox_rl/failfast.py`: reuse `classify_pod()`, `StartVerdict`, `_REQUEST_TIMEOUT`, and add `BatchWatchdog` (or place in `batch_claims.py`)
  - `agent_sandbox_rl/batch_claims.py`: `BatchClaims`
  - `agent_sandbox_rl/fleet.py`: `SandboxFleet.__init__()`, `SandboxFleet._acquire_reserved()`
- **Nuance & problem:**
  1. `FleetConfig.effective_claim_timeout()` returns `fail_fast.claim_timeout` (`180s` default) when `fail_fast.enabled` is true, else `ready_timeout` (`900s`).
     - **Critical `shutdownTime` nuance:** In `claim_batch(..., quorum_timeout=...)`, the SDK bakes `quorum_timeout` into every acquired claim's `spec.lifecycle.shutdownTime = create_time + quorum_timeout + work_budget + 600s`. Meanwhile, `batch.acquire(pool, timeout=acquire_timeout)` controls how long the caller waits for Ready. Therefore, keep `quorum_timeout=config.ready_timeout` on `claim_batch(...)` and compute `BatchClaim.expires_at = started + quorum_timeout + work_budget + BATCH_SHUTDOWN_MARGIN_SECONDS` using `quorum_timeout`, while passing `timeout=config.effective_claim_timeout()` to `batch.acquire(...)`!
  2. When a cold-start pod sits in `ImagePullBackOff`, `ErrImageNeverPull`, `InvalidImageName`, `CreateContainerConfigError`, `OOMKilled`, or `Unschedulable`, `per_claim` mode uses `ClaimWatchdog` to inspect the pod and delete the `SandboxClaim`, converting the error into `SandboxStartError(reason, message, claim_name=..., pod_name=..., image=...)` and recording `self._obs.claim(cluster.name, "start_failed")`.
  3. Because `SandboxBatch.acquire()` generates `<batch_id>-<ordinal>` internally, spawning 1,000 per-claim threads doesn't know `claim_name` upfront — **and we can do much better** using `batch.members()` + `TerminalMemberError.member.claim_name`!
- **Design (`BatchWatchdog` — 1 daemon thread per open `SandboxBatch` when `fail_fast.enabled`):**
  1. When `fail_fast.enabled` is true (and `hasattr(batch, "members")`), associate one `BatchWatchdog(cluster, batch, fail_fast)` daemon thread with each opened `SandboxBatch` (started lazily on first acquire or when the batch is opened; stopped in `BatchClaims.release_all()` and when `_batch_for()` replaces a stopped batch).
  2. Every `policy.poll_s`, `BatchWatchdog` reads `batch.members()` (zero-RPC in-memory snapshot):
     - Track `pending_since[m.claim_name]` for members in `MemberState.PENDING` (prune entries no longer in `PENDING` so state stays `O(in_flight)`).
     - Skip any `PENDING` member where `now - pending_since[m.claim_name] < policy.initial_delay_s` — **warm-pool adoptions become Ready in milliseconds and never pass `initial_delay_s`, so steady-state warm runs do 0 pod `GET`s!**
     - For any `PENDING` member past `initial_delay_s`, resolve its pod name: if `m.sandbox_name` is already populated on the `Member`, resolve via `Sandbox` annotation / `m.sandbox_name` (falling back to `_resolve_pod_name(cluster, m.claim_name)` if `m.sandbox_name` is empty), read `core_api.read_namespaced_pod_status(pod_name, ...)`, and run `classify_pod(pod, now=now, first_seen=..., last_seen=..., policy=policy)` per claim.
     - When `classify_pod` returns a `StartVerdict`: under `_decide_lock` (if not stopped), store `self._verdicts[m.claim_name] = verdict` and call `cluster.resources.delete_claim(m.claim_name, request_timeout=_REQUEST_TIMEOUT)`.
  3. Deleting the claim causes the batch's shared watch to mark the member `LOST`, waking `batch.acquire()` with `TerminalMemberError` (`exc.member.claim_name == m.claim_name`)!
  4. In `BatchClaims.acquire()` / `SandboxFleet._acquire_reserved()`:
     - If `batch.acquire()` raises `BatchError` (specifically `TerminalMemberError`, or any `BatchError` if a test double raises it) and `watchdog.pop_verdict(claim_name)` has a `StartVerdict` for `exc.member.claim_name`, record `self._obs.claim(cluster.name, "start_failed")` and raise `SandboxStartError(verdict.reason, verdict.message, claim_name=claim_name, pod_name=verdict.pod_name, image=task.image) from exc`.
     - Also check `watchdog.pop_verdict(member.claim_name)` right after `batch.acquire()` returns Ready (handling the tiny race where Ready arrived just as the watchdog deleted the claim, identical to `per_claim`!).
- **Required tests:**
  - Unit tests verifying:
    1. Warm batch acquires returning before `initial_delay_s` issue **zero** pod/sandbox `GET`s from `BatchWatchdog`.
    2. A cold `PENDING` batch member stuck in `ImagePullBackOff` / `InvalidImageName` / `Unschedulable` is deleted by `BatchWatchdog`, surfaces `SandboxStartError` with `claim_name`, `pod_name`, and `image`, and records `claim(..., "start_failed")`.
    3. `BatchClaim.expires_at` uses `ready_timeout` (`quorum_timeout`), while `batch.acquire(..., timeout=...)` receives `effective_claim_timeout()`.

---

#### P0-3. Sweep and verify batch `Lease`s in `reaper.py` (`#1791`) and `verify_teardown()` (`#1966`)

- **Target symbols:**
  - `agent_sandbox_rl/resources.py` & `agent_sandbox_rl/cluster.py`: label-scoped `Lease` list/delete helpers via `client.CoordinationV1Api(cluster.api_client)`
  - `agent_sandbox_rl/reaper.py`: `_reap_with_cluster()`, `find_orphan_runs()`, `reap_orphans()`
  - `agent_sandbox_rl/fleet.py`: `SandboxFleet.verify_teardown()`
- **Nuance & problem:**
  - `BatchClaims._batch_for()` passes `labels=dict(self._labels)` (`agent-sandbox-rl/run-id=<run_id>`, `app.kubernetes.io/managed-by=agent-sandbox-rl`) to `claim_batch()`, which stamps those exact labels onto both every `SandboxClaim` and its owning `coordination.k8s.io/v1` `Lease` (`batch-<id>`).
  - Today, `reaper.py` sweeps `SandboxClaim`s, `SandboxWarmPool`s, `Sandbox`es, `SandboxTemplate`s, and `Pod`s, but **not** `Lease`s. If a driver crashes on a cluster not running the standalone `batch_reaper` cron, expired `Lease` CRs accumulate in etcd, and `teardown(verify=True)` (`#1966`) does not verify `Lease` cleanup.
- **Design:**
  1. Add `list_leases(label_selector: str)` and `delete_lease(name: str)` / `delete_leases(label_selector: str)` helpers on `Resources` (lazily constructing `client.CoordinationV1Api(self.core.api_client)` or injecting `coordination_api` on `Resources` so existing `Resources(custom_api, core_api, ns)` callers keep working without breaking signatures!).
     - **Safety guard:** Never call `delete_leases` with an empty `label_selector` (refuse `ValueError` if `not label_selector`) so system leases (like controller leader-election leases in the same namespace) can never be touched even accidentally.
  2. In `reaper._reap_with_cluster()`: list & delete matching `Lease`s (right alongside `SandboxClaim`s — deleting the `Lease` also triggers K8s `ownerReferences` GC on its batch claims) and include `"leases": int` in the returned counts dict.
  3. Include labeled `Lease`s in `find_orphan_runs()` run discovery (so a crashed run with only its `Lease` left is still discovered and reaped after `min_age_s`) and in `SandboxFleet.verify_teardown()`'s counts and `"remaining"` settle check.
- **Required tests:**
  - Extend `tests/test_teardown_verify.py` and `tests/test_batch_claims.py` / `tests/test_fail_fast.py` to verify `reap(run_id=...)`, `find_orphan_runs()`, and `teardown(verify=True)` count, delete, and verify batch `Lease`s.

---

### P1 — High-Impact Throughput, Teardown, Pod-Annotation & Pool-Sizing Optimizations

#### P1-4. Eliminate redundant `O(N)` per-handle `DELETE` storm in `SandboxFleet._teardown()`

- **Target symbols:**
  - `agent_sandbox_rl/fleet.py`: `SandboxFleet._teardown()`
- **Nuance & problem:**
  - Currently `_teardown()` runs `self.release_all()` before `self._batch_claims.release_all()`.
  - When `N` batch-backed handles (`h._batch_claim is not None`) are still held at `teardown()`, `self.release_all()` fires **`N` individual `DELETE /sandboxclaims/<name>` RPCs** (`batch.release_member()`) right before `_batch_claims.release_all()` calls `batch.release()`, which **already deletes every claim of the batch in 1 label-scoped `DELETECOLLECTION` call** + deletes the `Lease`!
  - Furthermore, if a batch stopped (`batch.err() is not None`, e.g. `BatchLeaseExpiredError`), `batch.release_member()` in `15/N` raises `BatchError("batch ... has stopped")` — causing `self.release_all()` inside `_teardown()` to raise `BatchError` and abort the rest of `_teardown()` (skipping pool/template/namespace sweep!)! Look at `sandbox_batch.py`'s `release_member` docstring on `15/N`:
    > `Raises: BatchError: If this handle has been detached or released, or once err() is set, since the handle may no longer hold the batch's Lease.`
    Skipping `release_member()` on `_teardown()` fixes **both** the `O(N)` `DELETE` storm **and** the stopped-batch `teardown()` crash!
- **Design:**
  - Inside `_teardown()` (under `_sweep_lock` from `#1966`):
    1. Detach all tracked handles from `self._handles` under `self._lock`.
    2. For **batch-backed handles (`h._batch_claim is not None`)**: close any persistent exec websocket (`h.close_session()`) and decrement local counters (`c.release_claim()` and `self._claims_reserved` if `not h._slot_released`), **without** calling `h._batch_claim.release()`. `_batch_claims.release_all()` in `finally:` will delete all batch claims via `DELETECOLLECTION` and delete every `Lease`.
    3. For **non-batch handles (`h._batch_claim is None`)**: release them concurrently (via the same bounded `ThreadPoolExecutor` helper as `release_all()`, logging any per-handle termination error during teardown rather than aborting pool/template cleanup, or keeping `_batch_claims.release_all()` in `finally:`).
- **Required tests:**
  - Update `tests/test_batch_claims.py`:
    1. `teardown()` with open batch handles closes sessions, resets `active_claims` and `_claims_reserved` to `0`, calls `batch.release()` on every opened batch, and calls `batch.release_member()` **0 times**.
    2. `teardown()` when a batch has `batch.err() != None` (stopped batch) completes cleanly without raising `BatchError` and still sweeps pools/templates!
    3. `teardown()` with a `per_claim` handle whose `terminate()` raises still executes `_batch_claims.release_all()` via `finally:`.

---

#### P1-5. Parallelize `SandboxFleet.release_all()` and `SandboxFleet.acquire_batch()`

- **Target symbols:**
  - `agent_sandbox_rl/fleet.py`: `SandboxFleet.release_all()`, `SandboxFleet.acquire_batch()`
- **Problem:**
  - `release_all()` (`for h in list(self._handles): self.release(h)`) and `acquire_batch(tasks)` (`[self.acquire(t) for t in tasks]`) are single-threaded sequential loops. Even with `15/N`'s parallel `release_member()`, synchronous RL step loops calling `fleet.acquire_batch(tasks)` and `fleet.release_all()` only achieve concurrency = `1`.
- **Design:**
  1. **`release_all()`:** Snapshot `handles = list(self._handles)`. If `len(handles) <= 1`, release inline. Otherwise, fan `self.release(h)` out across `ThreadPoolExecutor(max_workers=min(len(handles), max(1, self.config.max_concurrent)))`. Wait for **all** futures to complete before re-raising the first exception (if any), so one failing handle never leaves the other `N - 1` handles unreleased.
  2. **`acquire_batch(tasks)`:** If `len(tasks) <= 1`, return `[self.acquire(t) for t in tasks]`. Otherwise, fan `self.acquire(t)` out across `ThreadPoolExecutor(max_workers=min(len(tasks), max(1, self.config.max_concurrent)))`, returning handles in the **exact input `tasks` order**. If any task raises, release all handles that *did* succeed in that `acquire_batch()` call (in parallel, best-effort logging cleanup errors) before re-raising the first exception so callers never leak unreachable handles.
- **Required tests:**
  - Unit tests verifying concurrency (`>1` overlapping calls when `max_concurrent > 1`), input-order preservation in `acquire_batch()`, partial-failure rollback on `acquire_batch()`, and full-sweep + error propagation on `release_all()`.

---

#### P1-6. Auto-size `Cluster` `connection_pool_maxsize` above `max_in_flight + 2` and lock `_leases_injected`

- **Target symbols:**
  - `agent_sandbox_rl/cluster.py`: `build_api_client()`, `Cluster.__init__()`, `Cluster.batch_sandbox_client()`, `ClusterRegistry.from_configs()`
  - `agent_sandbox_rl/fleet.py`: `SandboxFleet._default_registry()`
- **Problem:**
  1. `build_api_client()` hardcodes `pool_maxsize=1000`, while `claim_batch()` validates `check_pool_size(pool_maxsize, max_in_flight)` (`pool_maxsize >= max_in_flight + 2`). Setting `BatchClaimConfig(max_in_flight=1000)` crashes on first acquire with `ValueError` (`1000 < 1002`), and `FleetConfig(max_concurrent=1500)` overflows the `1000`-connection pool.
  2. `Cluster.batch_sandbox_client()` checks/sets `self._leases_injected` without an internal lock.
- **Design:**
  1. Compute `pool_maxsize = max(1000, config.max_concurrent + 32, (config.batch_claims.max_in_flight or 0) + 16)` in `SandboxFleet._default_registry(config)` and thread optional `pool_maxsize: int = 1000` through `ClusterRegistry.from_configs` → `Cluster` → `build_api_client`.
  2. In `Cluster.batch_sandbox_client()`, resolve `helper = self.k8s_helper` first, then guard `if not self._leases_injected:` with a dedicated lock (or `self._k8s_helper_lock` *after* `self.k8s_helper` has returned, avoiding non-reentrant lock deadlock).
- **Required tests:**
  - Assert `SandboxFleet(FleetConfig(claim_mode="batch", batch_claims=BatchClaimConfig(max_in_flight=1000)))` configures cluster `api_client.configuration.connection_pool_maxsize >= 1002`.
  - Thread-safety test calling `cluster.batch_sandbox_client()` concurrently from 16 threads and asserting `CoordinationV1Api` is constructed and injected once.

---

#### P1-7. Use native `SandboxBatch.acquire(..., pod_annotations=...)` when supported by the installed SDK

- **Target symbols:**
  - `agent_sandbox_rl/batch_claims.py`: `BatchClaims.supports_pod_annotations(cluster)` / `BatchClaims.acquire(..., pod_annotations=None)`
  - `agent_sandbox_rl/fleet.py`: `SandboxFleet._acquire_reserved()`
- **Nuance & opportunity:**
  - When `bench/rl-gke` was originally written, `SandboxBatch.acquire()` did not accept `pod_annotations`, so `fleet.py` fell back to `per_claim` whenever `snapshot_uid` or `pod_annotations` was passed.
  - On the `15/N` branch (`feat/batch-11-acquire-annotations`), `SandboxBatch.acquire(warmpool, timeout=None, *, pod_annotations=None)` **natively supports `pod_annotations`**!
- **Design:**
  - Check whether `batch.acquire` accepts `pod_annotations` via `inspect.signature(batch.acquire).parameters` (safe for both real `SandboxBatch` and test mocks/fakes!).
  - When `"pod_annotations"` is accepted by `batch.acquire`, pass `pod_annotations=annotations` directly through `BatchClaims.acquire(...)` so snapshot-pinned and pod-annotated acquires stay on the fast `batch` path (sharing the single watch and batch `Lease`)!
  - Keep the existing `per_claim` fallback (and one-time info log) **only** when the installed `SandboxBatch.acquire` signature does not accept `pod_annotations`.
- **Required tests:**
  - Test both paths in `tests/test_batch_claims.py`:
    1. When `FakeBatch.acquire` accepts `pod_annotations=None` (matching `15/N` `SandboxBatch.acquire`), `fleet.acquire(t, snapshot_uid="snap-1", pod_annotations={"a": "b"})` uses `batch.acquire(..., pod_annotations=...)` and **never** calls `create_sandbox()`.
    2. When a legacy batch object's `acquire(self, warmpool, timeout=None)` lacks `pod_annotations`, `fleet.acquire(t, pod_annotations={"a": "b"})` cleanly falls back to `create_sandbox()`.

---

### P2 — Async Executor Ceiling, Long-Episode `work_budget` Diagnostics & `FakeFleet` Parity

#### P2-8. Prevent `AsyncSandboxFleet._thread_pool()` from self-throttling at `max_concurrent >= 1000`

- **Target symbols:**
  - `agent_sandbox_rl/async_fleet.py`: `AsyncSandboxFleet._thread_pool()`
- **Problem:**
  - `_thread_pool()` computes `workers = min(1024, max(64, mc + 2 * win + 16))`. Because `AsyncSandboxFleet.acquire()` delegates to blocking `SandboxFleet.acquire()` on this pool, at `max_concurrent = 1000` (plus background `pipelined` warm/unwarm calls) `1024` threads can saturate, and at `max_concurrent = 1500+`, hundreds of `acquire()` calls queue in Python before ever reaching the batch.
- **Design:**
  - Ensure `workers` is never clamped below `mc + 64` (up to a safe ceiling like `4096`):
    `workers = min(4096, max(mc + 64, min(1024, max(64, mc + 2 * win + 16))))`.
  - Also mirror any new `SandboxFleet` parameters added on `upstream/main` (e.g., `teardown(..., *, verify=False)`, `verify_teardown()`, `add_tasks()`, `wait_pools_ready()`) if not already present after rebase.
- **Required tests:**
  - Assert in `tests/test_async_fleet.py` that `AsyncSandboxFleet(FleetConfig(max_concurrent=1200))._thread_pool()._max_workers >= 1264` while `max_concurrent=100` stays compact (`<= 1024`).

---

#### P2-9. Actionable `SandboxLostError` diagnostics and docs when `BatchClaimConfig.work_budget` expires

- **Target symbols:**
  - `agent_sandbox_rl/handles.py`: `SandboxHandle._raise_if_lost()`
  - `agent_sandbox_rl/exceptions.py`: `SandboxLostError`
  - `agent_sandbox_rl/config.py` & `agent_sandbox_rl/fleet.py`: docstrings on `FleetConfig.claim_mode`, `BatchClaimConfig.work_budget`, and `SandboxFleet.suspend()`
- **Problem:**
  - `per_claim` claims have no `shutdownTime`, whereas `batch` claims always carry `shutdownTime` (`work_budget=3600`s default + `quorum_timeout` + `600s` margin). `recycle=True` rotates handles proactively via `_claim_expiring()`, **but** a non-recycled multi-hour trajectory or a sandbox parked via `fleet.suspend()` across several hours will hit `shutdownTime` and be deleted by the controller, surfacing as a generic pod-gone error.
- **Design:**
  1. In `SandboxHandle._raise_if_lost(cause)`, when `not alive` and `self.claim_expires_at is not None and time.monotonic() >= self.claim_expires_at`, include an explicit hint in the `SandboxLostError` message stating that the batch claim reached its `BatchClaimConfig.work_budget` `shutdownTime` and suggesting increasing `BatchClaimConfig(work_budget=...)` (or enabling `recycle=True`).
  2. Document `work_budget` lifetime behavior concisely on `FleetConfig.claim_mode`, `BatchClaimConfig.work_budget`, and `SandboxFleet.suspend()`.
- **Required tests:**
  - Unit test verifying that an expired batch handle (`claim_expires_at <= time.monotonic()`) whose pod 404s on `exec()` raises `SandboxLostError` mentioning `work_budget`.

---

#### P2-10. `FakeFleet` / `FakeHandle` (`#1967`) parity with `claim_mode="batch"`

- **Target symbols:**
  - `agent_sandbox_rl/testing.py`: `FakeFleet`, `FakeHandle`
  - `examples/agent-sandbox-rl/tests/test_testing_fake_fleet.py`
- **Problem:**
  - `tests/test_testing_fake_fleet.py` enforces strict method/signature/property parity between `FakeFleet` / `FakeHandle` and `SandboxFleet` / `SandboxHandle`. And frameworks testing `recycle` or expiry logic against `FakeFleet(FleetConfig(claim_mode="batch"))` expect `handle.claim_expires_at` to be populated just like a real batch handle.
- **Design:**
  1. Ensure `FakeHandle` supports `claim_expires_at` (`property` or attribute matching `SandboxHandle`) — returning `None` in `claim_mode="per_claim"` and `time.monotonic() + config.ready_timeout + config.batch_claims.work_budget + BATCH_SHUTDOWN_MARGIN_SECONDS` in `claim_mode="batch"`.
  2. Keep all signature parity assertions in `tests/test_testing_fake_fleet.py` green.
- **Required tests:**
  - Test in `tests/test_testing_fake_fleet.py` verifying `FakeFleet(FleetConfig(claim_mode="batch"))` populates `claim_expires_at` while default `per_claim` returns `None`.

---

## 5. Verification & Deliverable Checklist

1. Run the full `agent-sandbox-rl` test suite:
   ```bash
   pytest examples/agent-sandbox-rl/tests
   ```
   All existing tests (including `#1791` `test_fail_fast.py`, `#1963` `test_lookahead.py`, `#1966` `test_teardown_verify.py`, `#1967` `test_testing_fake_fleet.py`, and `test_batch_claims.py`) plus all new tests for **P0-1 through P2-10** must pass with zero regressions.
2. Verify `git diff --name-only upstream/main` (or against your branch base) touches **only** files inside `examples/agent-sandbox-rl/`.
3. Push the completed branch to `origin` (`https://github.com/briankhoi/agent-sandbox.git`) and report the branch name + commit SHA so the benchmark runner can merge it alongside `15/N`.
