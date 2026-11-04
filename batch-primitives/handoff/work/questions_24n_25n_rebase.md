# Questions for Rebasing `24/N` (`merge/main-into-batch-claim-2`) and `25/N` (`feat/rl-batch-claim-mode`)

Date: 2026-11-04
From: Stack conversation
To: Owner of `24/N` and `25/N` (Coordinator 3 / Coordinator 1)

## Context (verified)

Since `origin/merge/main-into-batch-claim-2` (`12e04eb7`, built on `7cfe8484` + `upstream/main` `355803af`) and `origin/review/c3/feat/rl-batch-claim-mode` (`d9d5c258`, built on `review/c3/sdk-post-merge` `778b60e8`) were created on 2026-10-28:

1. `upstream/feature/batch-claim` has advanced to `a97eeae7` (`18/N` `#2154` merged), with `19/N` through `23/N` (`feat/batch-7a-create-path` .. `feat/batch-11-acquire-annotations`) sitting linearly above it.
2. `upstream/main` has advanced from `355803af` to `bcf5d4a0`, merging several Python SDK client-lifecycle and connector PRs that touch the same code paths as batch handles:
   - `#2085` (`7b1a9fbc`): adds `Sandbox._on_terminated` / `AsyncSandbox._on_terminated` bound to `functools.partial(self._untrack, self._active_connection_sandboxes)`.
   - `#2086` (`e7468986`): breaks the handle-to-connector reference cycle on close (`_release_sandbox_callbacks`).
   - `#2087` (`ae6bb389`): converts `SandboxClient._delete_automatic_sandboxes` (`bcf5d4a0:sandbox_client.py:432`) and `AsyncSandboxClient._atexit_cleanup` (`bcf5d4a0:async_sandbox_client.py:560`) into `@staticmethod` callbacks registered via `functools.partial(...)` so `atexit` never retains closed client instances.
   - `#2132` (`9f7999b3`): creates `httpx.AsyncClient` lazily and attaches `sandbox.connector._default_ssl_context = self._default_ssl_context` in `AsyncSandboxClient._attach_handle` (`bcf5d4a0:async_sandbox_client.py:243-247`) so handles share one `SSLContext` + `certifi` bundle per client.
3. On `examples/agent-sandbox-rl/`, `355803af..bcf5d4a0` touches only `CHANGELOG.md`, `docs/design.md`, and `pyproject.toml` (`kubernetes_asyncio<37.0.0`), with zero conflicts against the 9 RL files in `origin/review/c3/feat/rl-batch-claim-mode` (`d9d5c258`), and `17/N`'s `check_pool_size(..., explicit=max_in_flight is not None)` composes cleanly with `BatchClaimConfig.max_in_flight: int | None = None`.

Before we rebuild `24/N` and `25/N` on review copies and sync, please confirm the 3 intent questions below.

---

## Q1. Stack topology and `upstream/main` target for `24/N` and `25/N`

Previously (`H1` / `H2`), `merge/main-into-batch-claim-2` (`12e04eb7`) branched directly off `upstream/feature/batch-claim` (`7cfe8484`) so it could merge upstream immediately with `/label tide/merge-method-merge`, while `review/c3/sdk-post-merge` (`778b60e8`) merged `merge/main-into-batch-claim-2` into `23/N`, and `review/c3/feat/rl-batch-claim-mode` (`d9d5c258`) sat on `review/c3/sdk-post-merge` (leaving live `origin/feat/rl-batch-claim-mode` at pre-rebase `7252e27f`). Brian now refers to `merge/main-into-batch-claim-2` as `24/N` and `feat/rl-batch-claim-mode` as `25/N` in the linear stack.

- **Option 1A (Recommended):** Place `24/N` (`merge/main-into-batch-claim-2`) directly on top of `23/N` (`feat/batch-11-acquire-annotations`), merging current `upstream/main` (`bcf5d4a0`), and rebase `25/N` (`feat/rl-batch-claim-mode`) from `origin/review/c3/feat/rl-batch-claim-mode` (`d9d5c258`, preserving all `H2` `#2035` / `#1791` / `pod_annotations` work) directly onto `24/N`. Eliminates the extra `review/c3/sdk-post-merge` join branch and keeps `19/N`..`25/N` a single linear chain above `upstream/feature/batch-claim`, at the cost that `24/N` waits for `19/N`..`23/N` to merge before landing upstream (or gets re-merged onto `upstream/feature/batch-claim` if you want to land the merge earlier).
- **Option 1B:** Keep `merge/main-into-batch-claim-2` based directly on `upstream/feature/batch-claim` (`a97eeae7`, merging `bcf5d4a0`) so Brian can open and merge it into `feature/batch-claim` right now alongside `19/N`, keep `review/c3/sdk-post-merge` merging `merge/main-into-batch-claim-2` into `23/N`, and rebase `25/N` (`d9d5c258`) onto `review/c3/sdk-post-merge`.
- **Option 1C:** Same as 1A or 1B, but keep the `upstream/main` merge target pinned at `355803af` instead of advancing to `bcf5d4a0` (avoids resolving `#2085`/`#2087`/`#2132` in `24/N` now, but leaves `feature/batch-claim` behind `#2085`-`#2087` and `#2132` and requires a third `main` merge PR later).

**Question 1:** Do you want **Option 1A** (stack `24/N` on `23/N` merging `bcf5d4a0`, and `25/N` from `d9d5c258` on `24/N`), **Option 1B**, or **Option 1C**?

---

## Q2. Preserving `#2087` (static `atexit` hooks) alongside batch exit cleanup in `24/N`

In `#2087` (`ae6bb389`), `SandboxClient._delete_automatic_sandboxes` (`bcf5d4a0:sandbox_client.py:432`) and `AsyncSandboxClient._atexit_cleanup` (`bcf5d4a0:async_sandbox_client.py:560`) became `@staticmethod` callbacks bound via `functools.partial(...)` to `(self._active_connection_sandboxes, self._explicit_claims[, api_client])` so `atexit` does not hold `self` alive after a client closes.

On `23/N` (`f8b388a3`), both exit hooks also clean up tracked batches:
- `SandboxClient._delete_automatic_sandboxes` (`f8b388a3:sandbox_client.py:591-601`) calls `self._release_batches(exiting=True)` (`sandbox_client.py:603`), which reads `self._active_batches`, `self._closing_batches`, and `self._batches_lock`.
- `AsyncSandboxClient._atexit_cleanup` (`f8b388a3:async_sandbox_client.py:780-865`) reads `self._active_batches` and `self._closing_batches`.

Note that each live `SandboxBatch` / `AsyncSandboxBatch` inside `_active_batches` or `_closing_batches` holds `batch._client`, and `_unregister_batch` removes the batch from both containers once `release()` or `detach()` finishes closing cached connections (and `AsyncSandboxClient.close()` / `delete_all()` drains them). Neither container is ever rebound on the client.

- **Option 2A (Recommended):** Pass the in-place batch containers into the static `functools.partial` hooks as well:
  - `SandboxClient`: bind `(self._active_connection_sandboxes, self._explicit_claims, self._active_batches, self._closing_batches, self._batches_lock)` into static `_delete_automatic_sandboxes` and static `_release_batches` (with instance `delete_all()` and `__exit__()` calling the same static helpers). Once all sandboxes and batches on a client have been released/detached and closed, all four containers are empty and `gc.collect()` frees the `SandboxClient` immediately (preserving `#2087`'s contract and unit test).
  - `AsyncSandboxClient`: bind `(self._active_connection_sandboxes, self._explicit_claims, self._active_batches, self._closing_batches, api_client)` into static `_atexit_cleanup`. Once all sandboxes and batches are closed/detached/released, all four containers are empty and `gc.collect()` frees the `AsyncSandboxClient` immediately.
- **Option 2B:** Leave `_release_batches` as an instance method and bind `weakref.WeakMethod` or skip batch cleanup after client GC (rejected unless requested: weakens exit cleanup if a caller drops the `SandboxClient` local variable while still holding a `SandboxBatch` or relying on `cleanup=True` at process exit).

**Question 2:** Confirm **Option 2A** matches `#2087`'s design intent?

---

## Q3. Forwarding `#2132` (`_default_ssl_context`) and `#2085` (`_on_terminated`) in `SandboxBatch.connect()` / `AsyncSandboxBatch.connect()`

In `#2132` (`9f7999b3`) and `#2085` (`7b1a9fbc`), `AsyncSandboxClient._attach_handle` (`bcf5d4a0:async_sandbox_client.py:243-247`) attaches two client-owned attributes after constructing `self.sandbox_class(...)`:
```python
sandbox.connector._default_ssl_context = self._default_ssl_context
sandbox._on_terminated = functools.partial(self._untrack, self._active_connection_sandboxes)
```
(and `SandboxClient` sets `sandbox._on_terminated = functools.partial(self._untrack, self._active_connection_sandboxes)` at `bcf5d4a0:sandbox_client.py:267,331`).

In `AsyncSandboxBatch.connect()` (`f8b388a3:async_sandbox_batch.py:515-558`) and `SandboxBatch.connect()` (`f8b388a3:sandbox_batch.py:509-550`), handles live in `batch._connected` (never `client._active_connection_sandboxes`), and callers release batch members with `batch.release_member(member)`.

- **Proposed resolution (Option 3A, Recommended):**
  1. **Forward `_default_ssl_context` in `AsyncSandboxBatch.connect()`:** set `sandbox.connector._default_ssl_context = self._client._default_ssl_context` when `self._client._default_ssl_context is not None` (and set `self.mock_client._default_ssl_context = None` in `TestConnect.setUp`, matching `self.mock_client.file_config = None` from `H1`), so an async batch connecting to 160 members shares the client's single `SSLContext` + `certifi` bundle instead of loading ~0.7 MiB of OpenSSL state 160 times (~112 MiB).
  2. **Leave `sandbox._on_terminated = None` on batch-connected handles:** batch handles are not in `client._active_connection_sandboxes`, and if a caller ever calls `sandbox.terminate()` directly on a batch member's `Sandbox` handle, `terminate()` already closes its own connector (`#2086` breaks the handle-connector cycle immediately) and the batch watch observes the claim deletion (`LOST`) and evicts/closes the entry in `batch._connected`.

**Question 3:** Confirm **Option 3A** (forward `_default_ssl_context` in `AsyncSandboxBatch.connect()`, leave `_on_terminated = None` on batch handles), or do you also want `_on_terminated` on a batch-connected handle to evict from `batch._connected`?
