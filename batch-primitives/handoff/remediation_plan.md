# Batch Verification Sweep Remediation & Verification Plan

Status: Active master plan (persisted across `/compact` and sessions; verified item-by-item against `VERIFICATION_SWEEP_RESULTS.md`)
Baseline audited:
- `upstream/feature/batch-claim`: `98258d58` (`22/N` `#2188` merged)
- `upstream/main`: `993fd4aa`
- Open upstream PRs: `23/N` (`#2189`, `8a435b0f`), merge-main (`#2194`, `51ba4e78`)
- Unopened RL branches: `origin/feat/rl-batch-claim-mode` (`e84e964c`) + `origin/feat/rl-batch-hardening` (`bb443e71`)
- Verification suite reference branch: `origin/review-diff/batch-verification-suite` (`0ae3271a`) and `origin/batch-primitives-notes` (`25cf3025`)

---

## 1. PR Stack Architecture & Atomic Commit Map (Checked Item-by-Item Against `VERIFICATION_SWEEP_RESULTS.md`)

> [!IMPORTANT]
> Never edit open PR branches directly, never touch `/usr/local/google/home/brianknguyen/agent-sandbox` (keep clean on `main`), and never touch `/usr/local/google/home/brianknguyen/agent-sandbox-restack`. Build all branches in an isolated `--shared` clone under `<scratch>/repo`, push `review/r-sweep/<branch>` and `review-diff/<item>` branches to `origin`, and wait for Brian's approval before atomic `--force-with-lease` sync.

### Phase A: `23/N` (`#2189`, `feat/batch-11-acquire-annotations`)
- Mechanical rebase onto `upstream/feature/batch-claim` (`98258d58`).
- Prove mechanical identity via `git range-diff d9df3a42..8a435b0f 98258d58..HEAD` (all `=`).

### Phase B: New `24/N` (`feat/batch-12-lease-watch-fencing`, stacked on rebased `23/N`)
Six atomic commits (one logical fix group per commit for clean review):
1. **`F-02` (`test_k8s_helper.py`):** Pick a closed socket port (or accept `(NewConnectionError, ConnectTimeoutError)`) so macOS BSD non-listening sockets do not fail `test_k8s_helper.py:1243` (Aditya `#2188` nit).
2. **`F-01` + `F-16` (`sandbox_batch.py`, `async_sandbox_batch.py`, `k8s_helper.py`):** Separate private `self._lease_error` from watch `self._state.error()` while keeping public `batch.err()` shared (`2C`, Janet `#2188` comment), stop watch on Lease loss, add sync `Watch.stop()` + `_watch_thread.join()` in `_stop_background_threads`, and wrap async watch stream in `contextlib.aclosing`.
3. **`F-03` + `F-12` (`sandbox_batch.py`, `async_sandbox_batch.py`):** Return `needs_delete=True` from `BatchState.upsert_claim` when a late watch `ADDED` arrives for a claim already tombstoned by `release_not_ready()` or `release_group()` and drain `_delete_claim` from `_watch_loop` / `resync_from_list`; record timed-out `_waiting` members into `_resolved` in `BatchState._time_out_undecided_groups`.
4. **`F-04` + `F-27` + `F-29` (`sandbox_batch.py`, `async_sandbox_batch.py`, `k8s_helper.py`, `async_k8s_helper.py`):** Clamp `release_group()`'s `_delete_claims_in_rounds` and `count_live_sandbox_claims` to `_lease_expires_at` via a `deadline` parameter; fence `get_batch()` (`_attach`) `replace_batch_lease` timeout/expiry and run `check_pool_size`; handle `ApiException(status=404)` TOCTOU races on `replace_batch_lease` across `_take_batch_lease`, `_attach`, and `detach()`.
5. **`F-24` + `F-25` + `F-26` (`sandbox_batch.py`, `async_sandbox_batch.py`):** Track and drain in-flight dynamic `acquire()` HTTP `POST`s before `release()` / `detach()` delete or detach; resolve already-popped pacing-slot items when `_drop_planned_creates` runs during `release_group()` / `release_not_ready()`; wake `acquire()` pacing and `429` retry sleeps on pool release `waiter` and Lease-clamp `_in_flight` acquisition.
6. **`F-18` (`batch_reaper.py`):** Restart `find_stale_leases` pagination from `continue=None` on `410 Gone` token expiry and isolate individual Leases with naive/malformed `renew_time` instead of aborting the whole namespace.

### Phase C: New `25/N` (`feat/batch-13-client-handle-hardening`, stacked on `New 24/N`)
Four atomic commits:
1. **`F-09` + `F-23b` (`async_k8s_helper.py`, `k8s_helper.py`):** Validate HTTP status and preserve `429` `Retry-After` headers in `AsyncK8sHelper.watch_sandbox_claims` before `Watch.stream` parses lines; add `K8sHelper.close()` to close both `api_client` and `_batch_api_client` pools (including constructor failure cleanup).
2. **`F-08` + `F-15` + `F-23a` (`sandbox_client.py`, `async_sandbox_client.py`):** Preserve failed batches in `AsyncSandboxClient._stop_batches()` (matching `F-08` retry contract when `release()` had not released the Lease), close `sandbox.close_connection()` outside `self._lock` in `AsyncSandboxClient.close()`, serialize `_lifecycle_lock` after `_finish_detach` before `k8s_helper.close()`, and guard `batch._connected`/`_stale_connections`/`_detach_unfinished()` reads under `batch._lock` in `sandbox_client.py`.
3. **`F-17` (`sandbox.py`, `async_sandbox.py`, `sandbox_batch.py`, `async_sandbox_batch.py`):** Recreate closed cached `Sandbox` / `AsyncSandbox` handles in `batch.connect(member)`, evict closed handles from `batch._connected` without GC reference cycles, and route `sbx.terminate()` on batch-connected handles through Lease-fenced `batch.release_member(member)` (idempotent after `batch.release()`).
4. **`F-13a` (`k8s_agent_sandbox/__init__.py`):** Declare explicit `__all__` in package root so `py.typed` PEP 561 `mypy --strict` consumers resolve re-exported types cleanly.

### Phase D: Renumbered `26/N` (`#2194`, `merge/main-into-batch-claim-2`, stacked on `New 25/N`)
- Merge `upstream/main` (`993fd4aa`) into `New 25/N` (`/label tide/merge-method-merge`).
- Reconcile `F-08`/`F-15`/`F-23a` (`sandbox_client.py`, `async_sandbox_client.py`) and `F-17` (`sandbox.py`, `async_sandbox.py`) cleanly with `upstream/main`'s `#2085` (`_on_terminated`), `#2086` (`_release_sandbox_callbacks` + `gc.disable()` cycle tests), `#2087` (weakref `atexit`), and `SandboxFileConfig` / `watcher`, and regenerate `docs/python_sdk_reference.md`.

### Phase E: Existing Unopened RL Branches (`feat/rl-batch-claim-mode` + `feat/rl-batch-hardening`, folded in place as `27/N`)
- **Zero extra PR on top of RL.** Amend/fold all remaining RL fixes directly into their owning commits across `feat/rl-batch-claim-mode` and `feat/rl-batch-hardening` rebased onto `26/N`:
  - Already fixed in `bb443e71` (keep and verify): `F-14a`, `F-19b`, `F-20b`, `F-21c`.
  - Fold into owning commits of `feat/rl-batch-claim-mode`: `F-05` (`AsyncSandboxFleet.acquire` cancellation callback reclaiming leaked handle/claim/slot), `F-06` (`except BaseException:` across 5 rollback sites in `fleet.py`), `F-07` (`BatchClaims._batch_for` unconditional `_opened` registration + retry-preserving `release_all`), `F-13b` (`SandboxBatch` type annotation in `batch_claims.py`), `F-19a` (`1s` `.strftime` floor adjustment on `BatchClaim.expires_at`), `F-20a` (`constants.DEFAULT_LABELS` merged on `BatchClaims`), `F-21a` (`SandboxNotReadyError -> BatchError` on `batch.connect`), `F-21b` (`leases` check in `preflight()` when `claim_mode == "batch"`), `F-28` (`counted_hold` in async recycle + `jit_warmed` unwarm in sync recycle).
  - Fold into owning commits of `feat/rl-batch-hardening`: `F-10a/b/c` (`SandboxHandle.refresh()` batch reconnect, `vars(self.sandbox)` `_snapshots` trigger check without lazy `SnapshotEngine` allocation, `close_connection()` on fallback release), `F-11a/b` (`_teardown` complete handle + pool/template/namespace sweep before re-raising `first_exc` + entry `_torndown` check in `acquire()`), `F-14b` (`constants.REQUEST_TIMEOUT` on `fleet.py` `delete_claim`).

### Phase F: Standalone `upstream/main` PR (`chore/rl-mypy-gate`)
- **`F-22`:** Fix all `mypy` static type errors across `examples/agent-sandbox-rl/agent_sandbox_rl` and wire `agent_sandbox_rl` `mypy` into `dev/tools/test-unit`.

---

## 2. Verified Pre-Build Design Invariants from Critic `6f8731a7` (`F-01` & `F-17`)

### `F-01` (`2C` Authority Separation) Mandatory Implementation Rules
1. **State split:** Private `self._lease_error: BatchError | None = None` on `SandboxBatch` and `AsyncSandboxBatch` (write-once sticky). Only terminal Lease loss (`404`, `409` foreign holder, or monotonic `_lease_expires_at` expiry) sets `_lease_error`. Only fatal `_watch_loop` errors call `self._state.note_error(e)`.
2. **Public `err()`:** Return `self._lease_error or self._state.error()`. In `SandboxBatch`, read under `with self._lock:`. In `AsyncSandboxBatch`, `def err(self)` is synchronous: read `self._lease_error or self._state.error()` directly without `with self._lock:` (which is an `asyncio.Lock` and would raise `TypeError`).
3. **All 5 consumer/waiter sites MUST read `_lease_error or _state.error()` (prevents 100%-CPU spin loops when Lease expires):**
   - `_check_healthy` (`:506` / `:524`)
   - `wait_for_quorum` `raise ... from` (`:693` / `:699`)
   - `_next()` (`:714` and `:719` / `:722` and `:727`) via a shared `_batch_error_locked()` helper
   - `_wait_acquired()` (`:936` / `:944`)
4. **Creator checks:** `_create_worker` (`:1784` / `:1766`) and `_can_still_send` (`:1858` / `:1840`) stop when `self._lease_error is not None or self._state.error() is not None`. Do **not** set `_stop_creating` on watch error (because `_delete_released_create` treats `_stop_creating` as `detach()`/`release()` taking over cleanup).
5. **Lease-fenced cleanup checks (including `_delete_released_create`):** `release_member`, `release_not_ready`, `release_group`, `_member_delete`, `_delete_claim` (via `_check_lease_held`), **and `_delete_released_create` (`:1886` / `:1852`)** gate on `not self._detached and self._lease_error is None` after `_expire_lease_if_due`, **not** `_state.error()`.
6. **Renewal & watch shutdown symmetry:**
   - Snapshot `lease = self._lease` under `_lock` at start of `_renew_once` (checking `if self._detached or self._lease_error is not None: return False`) and check the same condition post-PUT before advancing `_lease` / `_lease_expires_at` / clearing `_renewal_degraded`.
   - Guard `_stop_renewal` under `_lock` with `if self._detached: return False`.
   - Both `_stop_renewal` and `_expire_lease_if_due` stop the watch (`self._watch_stop.set()` + `Watch.stop()` in sync; cancel `_watch_task` if `not asyncio.current_task()` in async), and sync `_watch_loop` checks `if self._watch_stop.is_set(): return` before applying events.
7. **Existing unit tests & docstrings to update in lockstep:**
   - Update `test_release_methods_delete_nothing_once_err_is_set` and `TestAcquire` `"err set"` in `test_sandbox_batch.py` and `test_async_sandbox_batch.py` (split into `"watch err set"` vs `"lease err set"`).
   - Update the `"once err() is set"` docstrings on `acquire`, `release_member`, `release_not_ready`, and `release_group`, and regenerate `docs/python_sdk_reference.md`.

### `F-17` (`batch.connect` + `Sandbox.terminate`) Mandatory Implementation Rules
1. **Post-construction attribute, NOT `__init__` kwarg:** Define `_claim_deleter: Callable[[], None] | None = None` on `Sandbox` and `_claim_deleter: Callable[[], Awaitable[None]] | None = None` on `AsyncSandbox`, assigned post-construction in `connect()` so `sandbox_class(...)` call-signature assertions (`TestConnect`) and custom `sandbox_class` subclasses stay unbroken.
2. **`close_connection()` vs `terminate()` ordering:** `close_connection()` must **never** clear `_claim_deleter` (since `terminate()` calls `close_connection()` first). Only `terminate()` clears `self.claim_name = None` and `self._claim_deleter = None` *after* `_claim_deleter()` succeeds (and invokes `_on_terminated` after `#2194` merges `main`).
3. **Cache eviction & zero GC cycles (`gc.disable()` safe for `#2194` `#2086` tests):** Check `if cached is not None and cached[0].is_active:` in `connect()`, and install a one-shot `_on_closed` eviction callback cleared inside `close_connection()` that removes `(claim_name, sandbox)` from `self._connected` if `cached[0] is sandbox` (breaking any `batch -> _connected -> sandbox -> _claim_deleter -> batch` reference cycle when `close_connection()` runs with `gc.disable()`).
4. **Idempotency after confirmed batch claim deletion:** If `batch._released` is `True` (all claims already deleted by `batch.release()`) or the batch already confirmed deletion of that handle's claim in `release_member` / `release_not_ready` / `release_group`, `sbx.terminate()` is a clean no-op rather than raising `BatchError`; if the batch was `detach()`ed, Lease was lost (`_lease_error` set), or delete failed, `sbx.terminate()` raises `BatchError` without issuing an unfenced `DELETE`.
5. **No context manager on `Sandbox` / `AsyncSandbox`:** Do not add `__enter__`/`__exit__` to `Sandbox` or `AsyncSandbox`.

---

## 3. Six-Gate Obsessive Verification Protocol (Run for Every Phase)

1. **Gate 1 - Pre-Build Per-PR Blueprint Critique (3 total: `New 24/N`, `New 25/N` + `#2194` merge, `27/N` RL):**
   - One un-anchored adversarial critic (`TypeName="self"`, `Model="inherit"`) attacks the combined fix blueprint for all fixes in that PR together before coding (checking lock ordering, sync/async parity, cross-fix interactions in shared functions, and TLA+ alignment).
2. **Gate 2 - Standalone Per-PR Head Check (No Forward Dependencies):**
   - At **each** PR head independently (`23/N`, `New 24/N`, `New 25/N`, `26/N`, `27/N`, `chore/rl-mypy-gate`): `mypy`, affected `pytest`, `make fix-api-docs` zero diff, `make toc-verify`, `ruff check --select E,W,F` on changed lines, zero forward dependencies, and commit hygiene (`Brian Nguyen <brianknguyen@google.com>`, zero trailers, zero `#<num>` refs, zero em/en dashes, active voice).
3. **Gate 3 - Full 10-Stage Automated Chaos, Fuzz & Formal TLA+ Suite:**
   - Overlay the verification suite in an isolated scratch copy of the candidate branch head and run `./dev/tools/run_batch_verification.sh` (Stage 0 `verify_batch_static.py`, Stages 1-2 all 31 `BatchLease.tla` + 22 subsystem TLC runs, Stage 3 `mypy`, Stage 4 BSD socket sim, Stage 5 23 SDK + 20 RL fault-injection tests, Stage 6 Hypothesis fuzzing, Stage 7 lockstep sync/async differential fuzzer, Stage 8 async concurrency stress), updating `.tla` models if transitions change.
4. **Gate 4 - Pre/Post `#2194` Merge-Main Boundary Audit:**
   - Test `F-08`/`F-15`/`F-23a` and `F-17` both on `New 25/N` (pre-merge-main) and `26/N` (`#2194` merged with `#2085`, `#2086`, `#2087`, including `test_sandbox.py` `gc.disable()` cycle tests), plus run an un-anchored verifier on `git show <26n-merge-commit>`.
5. **Gate 5 - Upstream Test Audit & Mutation Check (`skills/test-audit/SKILL.md`):**
   - One owning test per contract at the strongest boundary, full sync/async parity, zero mock-only tautologies, and mutation-checked against pre-fix code.
6. **Gate 6 - Multi-Verifier Convergence Loop Until Unanimous `ZERO_FINDINGS`:**
   - Run 3 parallel un-anchored adversarial verifiers on `base..branch` (Concurrency/Fencing, Cancellation/Retries/Parity, Minimalism/`test-audit`) and iterate with the warm builder + Gates 2 and 3 until unanimous `ZERO_FINDINGS`.
