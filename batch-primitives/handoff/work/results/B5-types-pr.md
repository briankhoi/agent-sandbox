# B5 result: PR 21/N types branch (`feat/batch-9-types`)

Branch `feat/batch-9-types` = `9785b2f8` (tree `3e67d8d6`), 2 commits off live `origin/feat/batch-8-list-memory` = `1b061c53`. Pushed to `origin`; worktree `agent-sandbox-par-types21` removed. No GitHub issues or PRs opened or commented on, no stack branch modified, no kind cluster touched.

## 1. Summary
- Built `feat/batch-9-types` in 2 commits replacing all 24 targeted `Any` annotations across `sandbox_batch.py`, `async_sandbox_batch.py`, `k8s_helper.py`, and `async_k8s_helper.py`:
  1. `8af3741f`: typed the 8 `helper` parameters (`K8sHelper` / `AsyncK8sHelper` under `TYPE_CHECKING`) in `_precheck_pools`, `_take_batch_lease`, `_delete_batch_objects`, and `_delete_claims_in_rounds`.
  2. `9785b2f8`: typed the 16 `V1Lease` helper methods, `_write_renewal`, and `last_lease` parameters, added overloaded `batch_utils.lease_parts(lease)` raising `BatchError` if `metadata` or `spec` is `None`, unpacked it across all 12 sync/async Lease call sites (including inside `detach()`'s 409 retry), added `cast(Any, None)` with its `kubernetes_asyncio` 36 stub comment in `AsyncSandboxBatch.detach()`, and added `TestLeaseParts`.
- One exact union adjustment needed for `mypy` on `kubernetes_asyncio` 36.1.0: in `sandbox_batch.py` (`_take_batch_lease` and `_delete_batch_objects`), `last_lease` is typed `"V1Lease | AsyncV1Lease"` rather than sync-only `V1Lease`, because `AsyncSandboxClient._atexit_cleanup` (`async_sandbox_client.py:825`) passes `AsyncSandboxBatch._lease` (`AsyncV1Lease`) to `sandbox_batch._delete_batch_objects`.
- All checks pass on every commit in `1b061c53..9785b2f8`: `mypy` (`0` errors in 37 files in both `kubernetes_asyncio` 36.1.0 and 33.3.0), targeted `pytest` (`422` to `424` passed, `281` to `283` subtests in both venvs), `TestLeaseParts` mutation check (both `metadata is None` and `spec is None` mutations fail), `pycodestyle --max-line-length=120` (`0` hits on changed lines), regenerated `docs/python_sdk_reference.md` (`0` diff), and commit author/trailer verification.

## 2. Ranked findings

### F1. `last_lease` in `sandbox_batch.py` needs `"V1Lease | AsyncV1Lease"` for `AsyncSandboxClient._atexit_cleanup`. Recommendation: keep `"V1Lease | AsyncV1Lease"` as committed in `9785b2f8`.
- **Reason:** Sync-only `V1Lease` is narrower than runtime reality because `sandbox_batch._delete_batch_objects` has two callers, `SandboxBatch.release()` (passing sync `V1Lease`) and `AsyncSandboxClient._atexit_cleanup()` (passing `AsyncSandboxBatch._lease` of type `AsyncV1Lease`), and `_take_batch_lease` only passes `last_lease` to `batch_utils.lease_parts(last_lease)`.
- **Measured:**
  - With `last_lease: V1Lease` in `sandbox_batch.py`, `mypy` in `venv` (`kubernetes_asyncio` 36.1.0) fails with 1 error: `async_sandbox_client.py:825: error: Argument 7 to "_delete_batch_objects" has incompatible type "kubernetes_asyncio.client.models.v1_lease.V1Lease"; expected "kubernetes.client.models.v1_lease.V1Lease" [arg-type]`.
  - With `last_lease: "V1Lease | AsyncV1Lease"` (`sandbox_batch.py:1653, 1713`), `mypy` passes with `0` errors across all 37 files in both `venv` (36.1.0) and `venv33` (33.3.0).
- **Verified:**
  - `AsyncSandboxClient._atexit_cleanup` calls `_delete_batch_objects_sync` (`sandbox_batch._delete_batch_objects`) with `batch._lease` (`async_sandbox_client.py:45, 818-826`), as documented at `sandbox_batch.py:1720`.
  - `from kubernetes_asyncio.client import V1Lease as AsyncV1Lease` is inside `if TYPE_CHECKING:` (`sandbox_batch.py:57-58`), matching `batch_utils.py:48-54`, so `sandbox_batch.py` still does not import `kubernetes_asyncio` at runtime.
  - `async_sandbox_batch._take_batch_lease` (`:1663`) and `_delete_batch_objects` (`:1723`) are called only from `AsyncSandboxBatch.release()`, so `last_lease: V1Lease` stays exact there.
  - Alternatives rejected: `cast(Any, batch._lease)` at `async_sandbox_client.py:825` (hides type checking on `last_lease` and touches `async_sandbox_client.py` without need); changing `_delete_batch_objects` / `_take_batch_lease` parameters to `(last_uid, last_transitions)` (alters function signatures from PR 11/N-20/N).

### F2. Overloaded `batch_utils.lease_parts`, `V1Lease` helper methods, and `helper` annotations check cleanly across 36.1.0 and 33.3.0. Recommendation: ready for PR 21/N.
- **Reason:** Replaces all 24 remaining non-boundary `Any` annotations in the batch modules without adding state or changing normal-path runtime behavior; a malformed apiserver Lease missing `metadata` or `spec` now raises `BatchError` instead of `AttributeError`.
- **Verified (at `9785b2f8`):**
  - Commit 1 (`8af3741f`): `helper: "K8sHelper"` in `sandbox_batch.py:60, 1613, 1647, 1707, 1759` and `helper: "AsyncK8sHelper"` in `async_sandbox_batch.py:63, 1623, 1657, 1717, 1768`.
  - Commit 2 (`9785b2f8`):
    - `k8s_helper.py:688, 705-710, 724-725` and `async_k8s_helper.py:719-721, 739-740, 754-756` type `read_batch_lease`, `replace_batch_lease`, and `create_batch_lease` with `client.V1Lease`.
    - `batch_utils.py:32, 48-54, 205-223` defines `@overload`ed `lease_parts` with sync `V1Lease` first so `venv33` (where `AsyncV1Lease` is `Any` under `ignore_missing_imports = true`) still narrows sync callers to `(V1ObjectMeta, V1LeaseSpec)`.
    - Unpacked across all 5 methods in both shells (`_attach` at `sandbox_batch.py:357-396` / `async_sandbox_batch.py:354-393`; `detach` including the 409 re-read at `sandbox_batch.py:1179-1225` / `async_sandbox_batch.py:1197-1238`; `_renew_once` at `sandbox_batch.py:1404-1409` / `async_sandbox_batch.py:1397-1402`; `_write_renewal` at `sandbox_batch.py:1431-1440` / `async_sandbox_batch.py:1426-1435`; `_take_batch_lease` at `sandbox_batch.py:1677-1703` / `async_sandbox_batch.py:1687-1713`).
    - `spec.holder_identity = cast(Any, None)` with the `kubernetes_asyncio` 36 stub comment appears only at `async_sandbox_batch.py:1205-1206`; sync `sandbox_batch.py:1187` sets `spec.holder_identity = None` directly.
- **Measured:**
  - Commit 1 (`8af3741f`, detached worktree): `mypy` `0` issues in 37 files in both venvs; targeted `pytest` (`test_batch_utils.py`, `test_k8s_helper.py`, `test_async_k8s_helper.py`, `test_sandbox_batch.py`, `test_async_sandbox_batch.py`) `422 passed, 281 subtests passed` in both venvs.
  - Commit 2 (`9785b2f8`): `mypy` `0` issues in 37 files in both venvs (no `redundant-cast` warning under `warn_redundant_casts = true`); targeted `pytest` `424 passed, 283 subtests passed` in both venvs; `pycodestyle --max-line-length=120` `0` hits across all changed lines in all 6 files; `pydoc-markdown 4.8.2` regeneration of `docs/python_sdk_reference.md` has `0` diff.
  - Mutation check on `TestLeaseParts` (`test_batch_utils.py:118-136`): dropping `lease.metadata is None` fails with `AssertionError: BatchError not raised` on the `metadata=None` subtest; dropping `lease.spec is None` fails with `AssertionError: BatchError not raised` on the `spec=None` subtest.
  - Pre-push multi-agent check: re-fetched `origin`; `origin/feat/batch-8-list-memory` remained at `1b061c53b2a500d872b2c63f7aa9e1033d93013a`.

## 3. Self-check result
- Spawned one fresh `inherit` subagent (`1e277b86-142a-46b7-85f2-7de975fe7672`) given only `1b061c53`, `9785b2f8`, the `B5` task specification, and the project rules (including the minimal-fixes question and read-only `skills/test-audit/SKILL.md`).
- **Agreement:** The reviewer independently ran `mypy` and `pytest` on both commits in both venvs (`0` errors, all tests passing), verified zero added state, confirmed `"V1Lease | AsyncV1Lease"` in `sandbox_batch.py:1653, 1713` is the exact closed union for `SandboxBatch.release()` and `AsyncSandboxClient._atexit_cleanup()`, verified `meta is lease.metadata` / `spec is lease.spec` in-place mutation under Pydantic `validate_assignment=True`, and confirmed sync/async parity, line lengths, commit authors, and no trailers.
- **Reviewer finding 1 (`_attach` conditional structure, `sandbox_batch.py:357-379`, `async_sandbox_batch.py:354-376`):** The reviewer noted that keeping `lease is None` alive past `parse_lease_metadata` requires `if lease is None or spec is None or ...:` where `spec is None` is equivalent at runtime to `lease is None`, and noted an alternative early `if lease is None:` block before `lease_parts(lease)` (-3 lines per file, at the cost of writing `raise BatchLeaseExpiredError(...)` twice). **Resolution:** Checked `batch_utils.py:462-480` and `sandbox_batch.py:354-380`. Keeping a single `raise BatchLeaseExpiredError(f"... Lease is missing or stale ...")` avoids duplicating the raise statement and matches the `B5` specification; kept as-is and listed in Section 4 if Brian prefers the early guard.
- **Reviewer finding 2 (read-only `test-audit` on `TestLeaseParts`, `test_batch_utils.py:118-136`):** Both my audit and the reviewer's audit reached the same verdict: **recommend removing 1 of 2 added test methods** (`TestLeaseParts.test_returns_metadata_and_spec`, `test_batch_utils.py:120-126`) and **keeping** `TestLeaseParts.test_rejects_a_lease_without_metadata_or_spec` (`test_batch_utils.py:128-136`).
  - **Removal candidate (`TestLeaseParts.test_returns_metadata_and_spec`, `test_batch_utils.py:120-126`):**
    - *Junk pattern:* Identity copier / internal helper call-shape test duplicated at real boundaries (`test-audit/SKILL.md:41, 44`).
    - *Detectable failure:* `lease_parts` returning something other than `(lease.metadata, lease.spec)` when both are set.
    - *Non-test callers:* 12 production call sites across `sandbox_batch.py` (`:357, 1179, 1224, 1404, 1437, 1677, 1678, 1702`) and `async_sandbox_batch.py` (`:354, 1197, 1237, 1397, 1432, 1687, 1688, 1712`).
    - *Stronger proof:* Every production Lease path unpacks `(meta, spec)` via `lease_parts` and is exercised at the public batch boundaries (`TestLeaseTakeoverWrites`, `TestRenewal`, `TestDetach`, `TestRelease` in `test_sandbox_batch.py` and `test_async_sandbox_batch.py`); `TestLeaseOwnerReference` right above (`test_batch_utils.py:109-116`) likewise tests only the `BatchError` rejection in `test_batch_utils.py`.
    - *History:* Added in `9785b2f8` per item 1 of the `B5` unit-test specification.
    - *Unlocked deletion:* The 7-line `test_returns_metadata_and_spec` method (`V1LeaseSpec` at `:19` remains needed by `:130`).
    - *Risk and validation:* Low risk; `pytest k8s_agent_sandbox/test/unit/test_batch_utils.py k8s_agent_sandbox/test/unit/test_sandbox_batch.py k8s_agent_sandbox/test/unit/test_async_sandbox_batch.py`.
  - **Kept (`TestLeaseParts.test_rejects_a_lease_without_metadata_or_spec`, `test_batch_utils.py:128-136`):** Protects the `BatchError` runtime contract when `ApiClient.deserialize` produces a `V1Lease` with `metadata=None` or `spec=None`, which no owner-boundary test constructs (verified by grep across `k8s_agent_sandbox/test/`).
  - Per `test-audit/SKILL.md:8`, nothing was removed; waiting for Brian's call below.

## 4. Decisions for Brian
1. **`TestLeaseParts` (`test_batch_utils.py:118-136`):** Keep both `test_returns_metadata_and_spec` and `test_rejects_a_lease_without_metadata_or_spec` as committed in `9785b2f8`, or drop the `test-audit` candidate `test_returns_metadata_and_spec` (`:120-126`) to match `TestLeaseOwnerReference` (`:109-116`)?
2. **`_attach` narrowing (`sandbox_batch.py:357-379`, `async_sandbox_batch.py:354-376`, optional):** Keep the single `BatchLeaseExpiredError` raise (`if lease is None or spec is None or ...:`) as committed, or switch to an early `if lease is None:` guard before `lease_parts(lease)` (saves 3 lines per file, writes `raise BatchLeaseExpiredError(...)` twice)?

### Draft PR 21/N title and description

**Title:** `refactor(python-sdk): replace Any with exact helper and Lease types in batch modules (21/N)`

**Description:**
````markdown
#### What this PR does / why we need it:
This PR is the twenty-first in a series of PRs to implement a batch. It replaces the remaining non-boundary `Any` annotations in the batch modules with exact helper and `V1Lease` types so `mypy` checks helper and Lease calls end to end.

- `sandbox_batch` and `async_sandbox_batch` type their module-level `helper` parameters as `K8sHelper` and `AsyncK8sHelper` under `TYPE_CHECKING`.
- `K8sHelper` and `AsyncK8sHelper` type `read_batch_lease`, `replace_batch_lease`, and `create_batch_lease` with `V1Lease`, and the batch handles type `_write_renewal` and `last_lease` to match (`V1Lease | AsyncV1Lease` on the sync deletion path used by `AsyncSandboxClient`'s `atexit` cleanup).
- `batch_utils.lease_parts` narrows optional `V1Lease.metadata` and `V1Lease.spec` in one place and raises `BatchError` if either is missing, and `AsyncSandboxBatch.detach()` casts `None` when clearing `holder_identity` to work around `kubernetes_asyncio`'s `str`-only setter stub.

#### Which issue(s) this PR is related to:

#### Release Note
```release-note
NONE
```
````

## 5. Links
- Full PR 21/N compare (`1b061c53..feat/batch-9-types`): https://github.com/briankhoi/agent-sandbox/compare/1b061c53b2a500d872b2c63f7aa9e1033d93013a...feat/batch-9-types
- Commit 1 (`8af3741f`, typed `K8sHelper` / `AsyncK8sHelper` parameters): https://github.com/briankhoi/agent-sandbox/commit/8af3741ffc10c08fdac9e987a77312739cf12fbb
- Commit 2 (`9785b2f8`, typed `V1Lease` methods, `last_lease`, and `lease_parts`): https://github.com/briankhoi/agent-sandbox/commit/9785b2f832f82cf71c2ab213deb1ada3220b873f
- This report: https://github.com/briankhoi/agent-sandbox/blob/batch-primitives-notes/batch-primitives/handoff/work/results/B5-types-pr.md
