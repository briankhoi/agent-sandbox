# C2 result: finalize `feat/batch-8-list-memory` (`219985c0`)

Branch `feat/batch-8-list-memory` = `219985c0`, 4 commits on PR 7 `91d5ffc4` (`origin/feat/batch-7a-create-path` is still `91d5ffc4`). Synthetic C1-to-C2 delta branch `review-diff/pr8-c2` = `00ecc42d` (parent `e5367671`, tree `219985c0^{tree}`). Both pushed to `origin`. No GitHub issues or PRs opened or commented on.

## 1. Summary
- Dropped the two metadata-only optional commits (`30e6fb20` metadata-only count and `d8ec0178` metadata-only deletecollection) and kept commit 6 (`e5367671` -> `219985c0`, drop `metadata.managedFields` from listed claims) as commit 4, per Brian's decision 1 and decision 2.
- Folded `del page` into commit 1 (`_claim_list_pages`) and commit 2 (`count_live_sandbox_claims`) in both sync and async helpers so the generator and loop frame release each page before fetching the next one, per Brian's decision 4, with weakref unit tests that fail if either `del page` is removed.
- Added the per-page `_request_timeout` `Args:` docstring to `list_sandbox_claim_objects` in commit 1 (both shells), matching `count_live_sandbox_claims`.
- Updated `C1-pr8-impl.md` to clarify the urllib3 retry behavior at the default 90 s client timeout versus the apiserver's 60 s request timeout (504), and to note the provenance of the 3-claim wire-byte measurement.

## 2. Findings (ranked)

### F1. Single-page retention in `_claim_list_pages` and `count_live_sandbox_claims` (`a93dacff`, `b64216bc`). Recommendation: keep as folded.
- **Reason:** While `_claim_list_pages` fetches page `N+1`, both the generator frame (`_claim_list_pages`) and the consumer's `for` loop variable (`count_live_sandbox_claims`) otherwise retain references to page `N`. Deleting `page` in both frames before advancing frees page `N` before page `N+1` is allocated.
- **Verified:**
  - `del page` in `_claim_list_pages` right after extracting `continue_token` (`k8s_helper.py:113-116`, `async_k8s_helper.py:92-95`).
  - `del page` at the end of the loop body in `count_live_sandbox_claims` (`k8s_helper.py:647-653`, `async_k8s_helper.py:677-683`).
  - Unit tests `test_count_live_sandbox_claims_counts_claims_not_being_deleted_across_pages` pop `_Page(dict)` instances from a list in `next_page` and assert `self.assertIsNone(prev_page_ref())` on each subsequent call (`test_k8s_helper.py:1226-1259`, `test_async_k8s_helper.py:1140-1167`).
- **Measured (mutation check):**
  - Commenting out `del page` in `_claim_list_pages` (`k8s_helper.py:114`, `async_k8s_helper.py:93`) fails both sync and async tests with `AssertionError: {'items': ...} is not None`.
  - Commenting out `del page` in `count_live_sandbox_claims` (`k8s_helper.py:653`, `async_k8s_helper.py:683`) fails both sync and async tests with `AssertionError: {'items': ...} is not None`.

### F2. Dropped metadata-only commits 4 and 5 and kept `managedFields` trim as commit 4 (`219985c0`). Recommendation: ready for PR 8.
- **Reason:** Matches Brian's decision 1 and decision 2, removing the dedicated sync metadata `ApiClient`, `_list_claim_metadata_page`, `_claim_metadata_request`, and `PARTIAL_OBJECT_METADATA_LIST_ACCEPT` while retaining the 50% list-peak reduction from dropping `metadata.managedFields`.
- **Verified:**
  - `git diff e5367671..219985c0` contains only the removal of commits 4 (`30e6fb20`) and 5 (`d8ec0178`), the 4 `del page` lines, the 2 `Args:` docstring blocks in `list_sandbox_claim_objects` (`k8s_helper.py:614-616`, `async_k8s_helper.py:645-646`), and the weakref checks in the 2 count tests.
  - Commit 3 (`ac13f8e9`, stop parsing the deletecollection response) is patch-identical to `fb2a7e52`.
  - Commit 4 (`219985c0`, drop `managedFields` from listed claims) cherry-picked cleanly onto commit 3 (`k8s_helper.py:626-630`, `async_k8s_helper.py:658-662`).
- **Measured (per-commit checks across all 4 commits in `91d5ffc4..219985c0`):**
  - `mypy k8s_agent_sandbox` clean (`0` issues in 37 files) in both `kubernetes_asyncio` 36.1.0 and 33.3.0 venvs on every commit.
  - Targeted unit suites (`test_k8s_helper.py`, `test_async_k8s_helper.py`, `test_sandbox_batch.py`, `test_async_sandbox_batch.py`) pass on every commit in both venvs (365 to 372 passed, 154 to 156 subtests passed).
  - `pycodestyle --max-line-length=120` on all 468 changed lines across the 9 touched Python files: `0` hits.
  - Regenerating `docs/python_sdk_reference.md` via `pydoc-markdown 4.8.2`: `0` diff.

### F3. Report cleanup in `C1-pr8-impl.md`. Recommendation: complete.
- **Verified:**
  - `C1-pr8-impl.md:14` and `C1-pr8-impl.md:96` now state that at the default 90 s client timeout, the apiserver's own 60 s request timeout returns a `504` first (which urllib3 does not retry), and the 4x stretch happens when a client read timeout fires before the server responds (as in the 5 s forced-timeout run, 5 s to 20 s) or on `429`/`503` with `Retry-After`.
  - `C1-pr8-impl.md:67` marks the 3-claim wire-byte numbers (`9,492 B` to `8,031 B`) as measured ad-hoc via `kubectl proxy` + `curl` rather than from a committed JSON file.

## 3. Self-check
- Spawned a fresh `inherit` subagent (`ff676b36-2727-4a32-8c71-52721a124524`) with only `91d5ffc4`, `219985c0`, `e5367671`, `C1-pr8-impl.md`, the C2 specification, and the project rules.
- **Reviewer result:** Approved with 0 blockers and 0 rule violations. Confirmed no added state or widened types (`live` narrowed to `int | None`, metadata client attributes removed), both `del page` placements necessary and verified by `_Page(dict)` weakrefs without altering production code for a test double, exact match with the C2 specification across all 4 commits and `C1-pr8-impl.md`, and full sync/async comment and code parity.

## 4. Decisions for Brian
- None. All four decisions from C1 are applied; `feat/batch-8-list-memory` (`219985c0`) is ready for the stack conversation.

### Updated draft PR description (4-commit series)
**Title:** `perf(python-sdk): page the claim list and stop parsing the deletecollection body (20/N)`

**Description:**
> Releasing or attaching to a large batch held whole claim lists in memory. At 5,000 claims, `release()` peaked at 69 MB decoding the deletecollection response, which no caller reads. One claim list peaked at 69 MB too.
>
> - Claim lists are paged at 500 (client-go's page size). The first page's resourceVersion still starts the watch, and an expired continue token restarts the list once.
> - The release rounds count the claims not yet being deleted page by page, freeing each page before fetching the next, instead of holding every remaining claim.
> - `delete_sandbox_claims_by_label` drains the deletecollection response unread and releases the connection in a `finally` block. The async helper raises `ApiException` for a non-2xx response itself, because `kubernetes_asyncio` doesn't check the status of an unread response.
> - Listed claims drop `metadata.managedFields`, which nothing in the SDK reads.
>
> Measured on kind with 5,000 claims (sync):
> - `release()` peak: 69.4 MB to 0.2 MB.
> - When deletecollection times out: 81.7 MB to 11.8 MB (measured before `del page`).
> - Claim-list peak: 69.4 MB to 24.1 MB.
>
> At 10,000 claims:
> - `release()` peak: 138.8 MB to 9.9 MB.
> - Claim-list peak: 138.9 MB to 43.6 MB.
>
> Release time is unchanged within noise.

## 5. Links
- PR 8 full diff (`91d5ffc4..219985c0`): https://github.com/briankhoi/agent-sandbox/compare/91d5ffc485dbd13fd762e833c08af5ffdb9da02f...feat/batch-8-list-memory
- C1-to-C2 delta (`e5367671..00ecc42d`, 1-click view): https://github.com/briankhoi/agent-sandbox/commit/00ecc42d62654c3d3ea66e247f79544ac629b416 (or https://github.com/briankhoi/agent-sandbox/compare/e5367671765202883ebf9673a413b9bd4b3858e3...review-diff/pr8-c2)
- Per commit on `feat/batch-8-list-memory`:
  1. `a93dacff` `perf(python-sdk): page the claim list`: https://github.com/briankhoi/agent-sandbox/commit/a93dacff406e2de593531711a75b6f032d233c9a
  2. `b64216bc` `perf(python-sdk): count live claims page by page in release rounds`: https://github.com/briankhoi/agent-sandbox/commit/b64216bcfc4c223d9fe564018f8511de091c0ae6
  3. `ac13f8e9` `perf(python-sdk): stop parsing the deletecollection response`: https://github.com/briankhoi/agent-sandbox/commit/ac13f8e900f7686d09087fc7d98b71adb3753356
  4. `219985c0` `perf(python-sdk): drop managedFields from listed claims`: https://github.com/briankhoi/agent-sandbox/commit/219985c08e7cd92fdc36943ba2f8aa26d22f299b
- Updated C1 report: https://github.com/briankhoi/agent-sandbox/blob/batch-primitives-notes/batch-primitives/handoff/work/results/C1-pr8-impl.md
- This C2 report: https://github.com/briankhoi/agent-sandbox/blob/batch-primitives-notes/batch-primitives/handoff/work/results/C2-pr8-fixes.md
