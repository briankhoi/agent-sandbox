# Type tightening plan (task 4.4), proposal only

Status: 17/N (`async_k8s_helper.py` `cast`) and 18/N (`lease_owner_reference`, `owner_references`, reaper `_continue=`) folded and synced 2026-10-27 (`9fa22b54`, `e531baf3`). 21/N (`feat/batch-9-types`: typed `helper` parameters, `V1Lease` methods, and `lease_parts(lease)` raising `BatchError`) approved by Brian 2026-10-28; implementation prompt ready at `work/prompts/B5-types-pr.md`. B4 (`work/prompts/B4-asyncio-stub-fix.md`) deferred per Brian 2026-10-28.

- Pinned (2026-10-22 14:50Z): PR 7 `a26979f1`, transport `5affc426`, reaper `d4fa3400`, PR 6 `43c0999f`, PR 5 `ad2317c9`, PR 2 `f3b61c61`, fu6 `5c51feca`, upstream base `255d15fc`.
- Re-checked after the reboot and restack (2026-10-22 22:50Z): PR 7 `deee6702`, transport `760f8796`, PR 6 `b1e13656`. The restack adds or removes no `Any`, `cast(` or ignore (verified, diff grep). `async_k8s_helper.py` is unchanged. Line numbers shift in `k8s_helper.py` (-2 after line 61, so the pool cast is now `:111` and the Lease methods `:613-648`), `sandbox_batch.py` (+4) and `async_sandbox_batch.py` (`helper: Any` at `:1474,1507,1528`, `_write_renewal` at `:1278`). Section 1 was re-measured on the new heads with a freshly built 33.3.0 venv: same results.
- Tools: mypy 1.20.2, Python 3.13, repo config (`[tool.mypy]` strict, `warn_unused_ignores`, `warn_redundant_casts`). Libraries: `kubernetes` 37.0.1 (ships `py.typed`, pydantic models) with `kubernetes_asyncio` 36.1.0 (ships `py.typed` and `.pyi` stubs) or 33.3.0 (untyped).
- Labels: measured (I ran it), verified (I read the code, `file:line` at `a26979f1` unless noted), inferred.

## 1. Transport mypy fix (do first, before transport goes upstream)

**Problem.** `async_k8s_helper.py:105` `api_pool_size()` reads `self.custom_objects_api.api_client.configuration.connection_pool_maxsize`. The 36.1.0 stub `kubernetes_asyncio/client/api/custom_objects_api.pyi` declares only `__init__` and leaves out `api_client`. The runtime `.py` sets it (`custom_objects_api.py:37`) (verified). So this is a gap in the upstream stub, and the code is correct at runtime.

**Measured error** (36.1.0, repo config): `async_k8s_helper.py:105: error: "CustomObjectsApi" has no attribute "api_client" [attr-defined]`. It shows up at `780b8aa8`, `5affc426` and `a26979f1`, and again at `760f8796` and `deee6702` (re-measured 22:50Z). `43c0999f` and `b1e13656` (PR 6) are clean. With 33.3.0 the batch code has no error.

**CI hits it (measured).** `dev/tools/test-unit:359-360` runs `pip install --force-reinstall kubernetes_asyncio` with no version, after `pip install -e .[test]`, which pins `kubernetes_asyncio<34.0.0` (`pyproject.toml:71`). In a throwaway venv, that turned 33.3.0 into 36.1.0. So `make test-unit` type-checks against 36.1.0.

**Owning commit (verified).** "check the Kubernetes API connection pool fits a batch" on `feat/batch-transport-ltlfdm` adds both `api_pool_size()` methods. It was `780b8aa8` at the first pin and is `df4dbeb2` after the restack (`git log -S api_pool_size b1e13656..760f8796`).

**Proposed diff (fix A)**, folded into that commit (`df4dbeb2` now) on a new review copy:

```diff
     async def api_pool_size(self) -> int | None:
         ...
         await self._ensure_initialized()
+        api_client = self._api_client
+        if api_client is None:
+            # _ensure_initialized() sets the client, so this only narrows the type for mypy.
+            raise RuntimeError("Kubernetes API client is not initialized")
         # aiohttp treats a limit of 0 or None as no limit.
-        return self.custom_objects_api.api_client.configuration.connection_pool_maxsize or None
+        return api_client.configuration.connection_pool_maxsize or None
```

**Why it's correct (verified).**
- `AsyncK8sHelper.__init__` takes only `api_client` and `pool_size` (`:60`). Callers can't pass their own API objects.
- `_ensure_initialized()` either keeps the injected client (`:65`) or creates one (`:88-93`). Then it builds `custom_objects_api` from that same `self._api_client` (`:95`). So the new read returns the same object as the old one.
- `close()` clears `_api_client` and `_initialized` together (`:772-775`), so the next call initializes again.
- There is no `await` between `_ensure_initialized()` and the read, so a concurrent `close()` can't run in between. The `raise` is unreachable.
- `assert` isn't used anywhere in the SDK's non-test code (grep), so the fix uses an explicit check.

**Measured result.** Fix A gives "no issues" on 36.1.0 and on 33.3.0, at `5affc426` and again at `760f8796` and `deee6702` (22:50Z). `TestAsyncK8sHelperPoolSize` passes 3 of 3 on a scratch copy of each transport head. The self-check reviewer ran the full unit suite on its own copy: 999 passed, 1 failed. The failure was a sync test, `test_sandbox_batch.py::TestAcquire::test_failures_drop_the_member_and_delete_a_claim_that_may_exist [released while pending]`, which passed 3 of 3 when rerun alone. That looks like a flake (inferred).

**Sync parity.** Keep `k8s_helper.py:113` `cast("int | None", ...)` (verified):
- The sync helper doesn't store a client when none is injected and `pool_size` is unset. `CustomObjectsApi(None)` picks the default client.
- The sync `ApiClient.configuration` is untyped, so mypy needs the cast to avoid `no-any-return`. The reviewer measured this.
- The two docstrings differ, and so does the return: async maps `0` to `None`, sync returns `0` as-is. That's a behavior question, not a typing one (see decision D3).

**Alternatives, measured on both versions:**
- B: `cast(client.ApiClient, self._api_client)`. It's one line and clean on both versions, but it hides `None` instead of checking it.
- C: `# type: ignore[attr-defined]`. Clean on 36.1.0, but on 33.3.0 it **fails** with `Unused "type: ignore" comment [unused-ignore]` (`warn_unused_ignores`). Rejected.
- D: `getattr(self.custom_objects_api, "api_client")`. Returns `Any` and needs another cast. Worse than B (inferred).
- E: pin `kubernetes_asyncio<34` in `dev/tools/test-unit:360`. That hides the error instead of fixing it, it changes repo tooling, and it's a separate concern from transport. It's still worth raising on its own, since CI currently tests a version the `async` extra excludes.

**Test impact.** No new test is needed: the behavior doesn't change and the existing pool tests cover both paths (`test_async_k8s_helper.py:1061-1074`).

## 2. Inventory

Counts from grep (`Any`, `cast(`, `# type: ignore`) over the 8 files at `a26979f1`, import lines excluded (measured):
- Per file: `batch_state` 6, `batch_utils` 3, `batch_reaper` 1, `sandbox_batch` 8, `async_sandbox_batch` 8, `k8s_helper` 34, `async_k8s_helper` 29, `models` 3. 92 in total.
- Per owning PR (where the `Any` came from, via `git log -S`): base (already upstream) 62, PR 2 18, reaper 6, PR 6 2, PR 5 2, PR 7 1, transport 1. fu6 to fu9, PR 3 and PR 4 add none.
- `--disallow-untyped-defs` finds 0 unannotated functions (strict already enforces it). `--disallow-any-explicit` reports 92 `explicit-any` hits in these files (measured).

Rows marked "extra scope" are in the helpers or `models.py`. Rows marked "base" are upstream code and out of the stack's scope.

| file:line | current | proposed type | owning PR/commit | mypy impact (measured on 36.1.0 unless noted) | risk |
| :-- | :-- | :-- | :-- | :-- | :-- |
| async_k8s_helper:105 (extra) | untyped attr | `self._api_client` narrowed (section 1) | transport `df4dbeb2` (was `780b8aa8`) | 1 → 0 errors | none |
| sandbox_batch:1444,1477,1500; async_sandbox_batch:1475,1508,1529 | `helper: Any` | `"K8sHelper"` / `"AsyncK8sHelper"` (TYPE_CHECKING import) | PR 6 `38eedb3c`, PR 2 `c0085e20`, PR 5 `c282be82` | 0 new errors; mypy now checks the helper calls' kwargs and returns | low; the atexit hook passes a sync `K8sHelper` (`async_sandbox_client.py:755`), which still fits |
| batch_utils:173 | `lease: Any` | a `Protocol` for `metadata.name`/`uid`, or take `name: str, uid: str` | reaper `d394d43a` | union-of-V1Lease variant gives 4 errors: `metadata`/`uid` may be `None` but the return is `dict[str, str]` | low; real check that the uid is set |
| k8s_helper:649-650, async_k8s_helper:681 (extra) | `body: Any -> Any` | `V1Lease -> V1Lease` per client | PR 2 `2b757c99` | together with the base rows below: 24 new errors (next row) | medium |
| k8s_helper:617,634-635, async_k8s_helper:648,666-667 (extra, base); sandbox_batch:1263, async_sandbox_batch:1279 (base) | Lease `Any` | `V1Lease \| None` for read, `V1Lease` otherwise | base `ef1349cc` | 23 `union-attr` in sync (`spec`/`metadata` are `Optional` in kubernetes 37), e.g. `sandbox_batch.py:336-371,1036-1068,1237-1271`; 1 async `assignment` at `async_sandbox_batch.py:1060` (the stub types the `holder_identity` setter as `str`, but `None` is valid) | medium-high; needs `None` handling for a Lease without `spec`. Today that would be an `AttributeError` in `get_batch` (inferred, unlikely) |
| k8s_helper:126, async_k8s_helper:118 (extra) | `owner_references: list[dict[str, Any]]` | `list[dict[str, str]]` (what `lease_owner_reference` returns) | reaper `d394d43a` | 0 expected (inferred) | none |
| batch_reaper:98 | `kwargs: dict[str, Any]` | pass `label_selector=`, `limit=`, `_continue=` directly (typed kubernetes 37 signature) | reaper `d3191d62` | not measured | low; need to check that `_continue=None` is accepted (inferred) |
| batch_state:73,131,252,257,299; batch_utils:471,477; sandbox_batch:1533; async_sandbox_batch:1562; helper claim/warmpool `dict[str, Any]` | CR JSON | keep `Any`: true boundary (raw CustomObjectsApi JSON; `derive_member` turns it into the typed pydantic `Member`). An alias like `ClaimObject = dict[str, Any]` is optional | base, PR 2 `7a5f12d8`/`c0085e20`/`2b757c99`, PR 7 `b3db1914` | none | a `TypedDict` would claim a shape the API doesn't guarantee (nulls handled by `or {}`) |
| sandbox_batch:120,208,317; async:121,207,314 | `SandboxClient[Any]` | keep: `T` is invariant and the handle doesn't use it | base, PR 2 | none | none |
| batch_state:291; models:251; k8s_helper:113 cast | pydantic `model_copy(update=)`, before-validator, sync pool cast | keep: library boundary | base, transport | none | none |
| async_k8s_helper:272 (base) | `events() -> Any` | `AsyncIterator[dict[str, Any]]` | base `87e23e0f` | not measured | out of stack scope |

## 3. Order of work

1. Fix A on a review copy `review/types/feat/batch-transport-ltlfdm` (fold into `df4dbeb2`, then restack reaper and PR 7). Transport isn't opened upstream yet, so no open PR changes. The stack conversation owns the restack.
2. `helper: Any` → typed helpers: 0 new errors, a cheap win. It would touch open PR 2 (#1824, `_delete_batch_objects`), PR 5 (#1990) and PR 6 (#1991). Wait for Brian.
3. `lease_owner_reference` plus `owner_references` typing: folds into the reaper branch (not opened).
4. Lease `V1Lease` typing: biggest value, but it starts in base code and needs a small `None` policy for `spec`/`metadata`. Proposed as its own later PR on top of the stack, not folded into open PRs.
5. `batch_reaper` kwargs: optional, into the reaper branch.

## 4. Decisions for Brian

- D1. Approve fix A (explicit `None` check) for `df4dbeb2`, or prefer B (one-line `cast`)?
- D2. Fold the typed `helper` params into open PRs 2, 5 and 6 now, or batch them into one later PR?
- D3. Sync `api_pool_size` returns `0` as-is while async returns `None`. Make sync match (behavior change, separate fix), or leave it?
- D4. Lease typing: a separate PR with a `None` policy (raise `BatchError` on a Lease without `spec`), or keep the Lease `Any` until later?
- D5. Raise the unpinned `--force-reinstall kubernetes_asyncio` in `dev/tools/test-unit:360` as a separate issue? Draft text: "test-unit force-reinstalls an unpinned kubernetes_asyncio after installing `.[test]`, which pins `<34.0.0`, so mypy and the tests run against 36.x, which the extras exclude."

## 5. Final Implementation Status (2026-10-28, Verified)

- **17/N & 18/N folds (`B2`/`B3`, synced 2026-10-27):** `cast(client.ApiClient, self._api_client)` in `async_k8s_helper.py:149`; `lease_owner_reference(lease: "V1Lease | AsyncV1Lease") -> dict[str, str]` raising `BatchError` in `batch_utils.py:177-199`; `owner_references: list[dict[str, str]] | None = None` in `k8s_helper.py` and `async_k8s_helper.py`; `_continue=` passed directly in `batch_reaper.py`.
- **21/N Types PR (`B5`, `origin/feat/batch-9-types` at `9785b2f8`, 2 commits on `origin/feat/batch-8-list-memory` `1b061c53`, verified ACCEPT 2026-10-28):**
  - Commit 1 (`8af3741f`): 8 `helper: Any` parameters typed as `"K8sHelper"` / `"AsyncK8sHelper"` (`_precheck_pools`, `_take_batch_lease`, `_delete_batch_objects`, `_delete_claims_in_rounds`).
  - Commit 2 (`9785b2f8`): 16 `V1Lease` annotations (`read_batch_lease`, `replace_batch_lease`, `create_batch_lease`, `_write_renewal`, and `last_lease` on `_take_batch_lease` and `_delete_batch_objects`, using `"V1Lease | AsyncV1Lease"` in `sandbox_batch.py` because `AsyncSandboxClient._atexit_cleanup` passes `AsyncV1Lease`), plus `batch_utils.lease_parts(lease)` raising `BatchError("batch Lease has no metadata or spec")` and `TestLeaseParts` in `test_batch_utils.py`.
  - Report: `work/results/B5-types-pr.md`.
