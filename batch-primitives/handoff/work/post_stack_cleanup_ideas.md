# Post-stack cleanup candidates (identical performance)

Verified during the 2026-11-03 audit of `BatchState`, `SandboxBatch`, `AsyncSandboxBatch`, and `batch_utils` through `23/N`. None of these affect runtime behavior or asymptotic/constant-factor performance, and all touch merged `1/N` to `14/N` code, so we deferred them until the batch stack finishes merging into `feature/batch-claim` / `main`.

## 1. Drop `BatchState._min_ready` (`batch_state.py:218`)

- **Current code:** `self._min_ready: dict[str, int] = {g.warmpool: g.min_ready or 0 for g in self.groups}` (`batch_state.py:218`), read at four sites (`batch_state.py:583, 602, 633, 648`).
- **Why it is removable at identical performance:** Every call site already operates on a pool in `self._group_by_pool` (`_unreachable_error` at line `601` already fetches `group = self._group_by_pool[pool]`). The dict exists only because `BatchGroup.min_ready` has type `int | None` on the Pydantic model (`models.py:247`) even though `_validate_min_ready` (`models.py:257-259`) guarantees an `int` after construction.
- **Cleanup option:** Add an `int`-typed helper/property on `BatchGroup` (or read `self._group_by_pool[pool].min_ready or 0`) and remove `self._min_ready`.

## 2. Pass init-only creator and watch inputs instead of storing four handle attributes (`sandbox_batch.py:151, 223-225`, `async_sandbox_batch.py:151, 221-223`)

- **Current code:** Both `SandboxBatch` and `AsyncSandboxBatch` initialize `_list_resource_version`, `_claim_labels`, `_claim_annotations`, and `_claim_lifecycle` on `self`.
  - `_claim_labels`, `_claim_annotations`, and `_claim_lifecycle` default to `{}` / `""` in `__init__` (staying empty forever on `get_batch` handles), are populated in `_claim()`, and are read only inside `_create_worker`. Neither `acquire()` nor `get_batch()` reads them.
  - `_list_resource_version` is read once at the top of `_watch_loop` (`sandbox_batch.py:1362`, `async_sandbox_batch.py:1349`) to seed the local `rv` variable.
- **Why it is removable at identical performance:** Precomputing the per-group label/annotation dicts and lifecycle spec once in `_claim()` and passing `(claim_annotations, claim_labels, claim_lifecycle)` into `_create_worker` (and passing `list_resource_version` into `_start(list_rv)` / `_watch_loop(list_rv)`) keeps the exact same $O(1)$ precomputed dicts per group while removing four attributes from every batch handle.

## 3. Consolidate sync `_watch_stop` + `_renew_stop` and inline single-caller `_check_lease_held` (`sandbox_batch.py:173-174, 454-456`, `async_sandbox_batch.py:470-472`)

- **Current code:**
  - Sync `SandboxBatch` carries three `threading.Event` stop flags: `_stop_creating` (`199`), `_watch_stop` (`173`), and `_renew_stop` (`174`). Production code always sets `_watch_stop` and `_renew_stop` on consecutive lines in `_stop_background_threads` (`1161-1162`) after joining `_creators` (`_stop_creating` must stay separate because creators stop before Lease renewal stops). Only direct `_watch_loop` unit tests in `test_sandbox_batch.py` set `_watch_stop` on its own.
  - `_check_lease_held` (`sandbox_batch.py:454-456`, `async_sandbox_batch.py:470-472`) wraps `with self._lock: self._check_healthy()` (and `async with self._lock: self._check_healthy()`) for a single call site in `_delete_claim` (`sandbox_batch.py:1069`, `async_sandbox_batch.py:1085`).
- **Cleanup option:** Merge `_watch_stop` and `_renew_stop` into one background-loop stop event (updating the isolated `_watch_loop` tests in `test_sandbox_batch.py`), and inline `_check_lease_held` in `_delete_claim` if desired.
