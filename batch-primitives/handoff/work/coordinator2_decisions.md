# Coordinator 2: B and C plans, and the decisions needed (2026-10-22 23:05)

Plans: `type_tightening_plan.md` (B), `pr8_plan.md` (C). Both were checked by fresh `inherit` subagents at the live heads (PR 7 `deee6702`, transport `760f8796`, pool commit `df4dbeb2`). Both verdicts were ACCEPT WITH FIXES. The fixes go into the implementation prompts. Labels: measured (run), verified (read in code), inferred.

---

## B. Task 4.4: transport mypy fix and type tightening

### Problem
- From transport's pool commit `df4dbeb2` up to PR 7, mypy reports `async_k8s_helper.py:105: "CustomObjectsApi" has no attribute "api_client" [attr-defined]` (measured, mypy 1.20.2). PR 6 `b1e13656` is clean.
- Cause: `kubernetes_asyncio` 36.1.0 ships type stubs, and its `CustomObjectsApi` stub declares the methods but not the `api_client` attribute. The runtime class does set it, so the code works and only the type check fails (verified).
- CI hits it: `dev/tools/test-unit:359-360` force-reinstalls an unpinned `kubernetes_asyncio` after installing the `<34` pin, which turns 33.3.0 into 36.1.0 (measured with `pip install --dry-run`).
- You want this fixed before transport is opened upstream.
- Separately, 4.4 asks for a list of each `Any` in the batch modules and what it should become. The plan counts 92 lines in 8 files, and 62 of them are in code that is already upstream (measured).

### Proposed solution
1. Fold a one-line fix into `df4dbeb2` on a review copy `review/types/feat/batch-transport-ltlfdm`, restack reaper and PR 7 on top, and hand the SHAs to the stack conversation for the sync. Transport is not opened upstream, so no open PR changes.
2. Everything else in the list goes to later, separate work, after your decisions below.

| Type spot | Proposal | Why |
| :-- | :-- | :-- |
| `helper: Any` in `_precheck_pools`, `_delete_batch_objects` and one more (both shells) | `K8sHelper` / `AsyncK8sHelper` | 0 new mypy errors, and mypy then checks every helper call (measured) |
| Lease methods and `_write_renewal(lease: Any)` | `V1Lease` | Catches a Lease without `spec`. 24 new mypy errors, mostly in upstream code (measured) |
| `lease_owner_reference(lease: Any)` (reaper) | a small Protocol, or take `name` and `uid` | 4 errors to handle, since `uid` may be `None` (measured) |
| `owner_references: list[dict[str, Any]]` (reaper) | `list[dict[str, str]]` | That is what `lease_owner_reference` returns |
| Claim JSON dicts, `SandboxClient[Any]`, pydantic hooks, `object` validators | keep | True boundaries (raw API JSON is turned into the typed `Member`) |

### Decisions

**B1. How to fix the mypy error.** You have to pick one, since each changes code in the transport commit.
- **(Recommended) One-line `cast(client.ApiClient, self._api_client)`.** It is the smallest code and matches the existing sync helper (`k8s_helper.py:111` already uses `cast` on the same attribute). It is clean on 36.1.0 and 33.3.0 (measured).
- 2-line explicit check (`if self._api_client is None: raise RuntimeError(...)`). It is also clean on both versions. It is the "explicit" style, but the branch can never run: `_ensure_initialized()` just set the client, with no `await` in between (verified). So it adds code without adding behavior.
- The original 4-line version (local variable plus comment). Same as the 2-line version, but longer. Rejected.
- `# type: ignore[attr-defined]`. Fails on 33.3.0 with "unused ignore" (`warn_unused_ignores`). Rejected.
- Pin `kubernetes_asyncio<34` in `test-unit`. That hides the error, changes repo tooling, and is a separate concern (see B5).

**B2. Where the typed `helper` parameters go.** They come from open PRs 2, 5 and 6, so editing them in place touches three open PRs.
- **(Recommended) One later PR.** No behavior change and no new errors, so there is no urgency. Folding into PR 6 would also conflict with PR 7 `d99b3c46`, which rewrites the same signature line (verified).
- Fold now into PRs 2, 5 and 6. Each PR is "right" on its own, but three open PRs change and need a restack.

**B3. Sync and async `api_pool_size` disagree on 0.** Sync returns `0` as-is and async returns `None` (verified). This matters because `check_pool_size(0, n)` raises `ValueError` (`batch_utils.py:270`), while urllib3 with maxsize 0 probably doesn't limit concurrency at all. So a sync user who set the pool to 0 might get a wrong error (inferred, not measured).
- **(Recommended) Leave it out of type tightening.** It is a behavior question, not a typing one. If you want, I write a short prompt to measure it, and then propose a fix if it is real.
- Make sync match async now. That is a behavior change in the transport PR, based on an inference.

**B4. Lease typing (`V1Lease` instead of `Any`).**
- **(Recommended) A separate later PR** that also decides what happens to a Lease without `spec` (proposal: raise `BatchError`). It is the most valuable item and the most work: 24 errors, mostly in upstream code, and it needs that policy.
- Keep `Any` for now. Free, but the `AttributeError` risk stays (unlikely, inferred).

**B5. The unpinned force-reinstall in `dev/tools/test-unit`.** Its own comment says it works around `kubernetes` 36.0.0 bundling `kubernetes_asyncio` files, and `pyproject.toml:18` already excludes 36.0.0 (verified). So it is probably obsolete, and it makes CI test a version the `async` extra excludes.
- **(Recommended) You file an upstream issue** (I never create issues). I draft the text including the obsolete-workaround point.
- Ignore it. The B1 fix keeps mypy clean on both versions anyway.

---

## C. Task 4.1: PR 8, claim-list memory

### Problem (measured, benchmark scenario 1d, 5,000 claims, sync)
- One full list of claims peaks at 69.4 MB of traced memory. About 46 MB is the parsed claims and about 23 MB is temporary response bytes.
- `release()` also peaks at 69.5 MB. The plan's new reading: the re-list in `release()` took 2 to 8 ms against 430 to 510 ms for a full list, and claims have no finalizer (verified), so the re-list is almost empty. The peak is the client parsing the deletecollection response, which lists every deleted claim and is thrown away (inferred from the timing; PR 8 measures it).
- No claim list is paginated today (verified, both helpers). Memory grows linearly with N.

### Proposed solution (your scope: "PR 8 = claim-list pagination (attach and release re-list), metadata-only re-list in `release()`, and skip parsing the deletecollection body")
1. **Pagination.** `list_sandbox_claim_objects` pages at 500 with continue tokens inside the helper. It returns all items and the first page's resourceVersion, which the watch starts from, and restarts the list once if a continue token expires (410). The signature is unchanged, so attach, the precheck, the watch's 410 re-list and `release_group` get it with no new call sites. Expected attach peak: about 48 to 49 MB instead of 69 MB, because callers still keep every claim (inferred).
2. **Release re-list, metadata-only, page by page.** A count of live claims (no `deletionTimestamp`) that drops each page. Expected peak: about one page at any N (inferred).
   - Async sends the metadata-only Accept header on a direct `call_api` call. No second client, shared pool (verified working in `kubernetes_asyncio` 33 and 36).
   - Sync: see C2.
   - The count reads only `metadata.deletionTimestamp`, so it stays correct even if the header were ignored.
3. **Don't parse the deletecollection response.** Pass `_preload_content=False`, drain and release the connection in `finally`, return `None` (no caller uses the result, verified).
   - Async must check the status itself: without preload, `kubernetes_asyncio` returns non-2xx responses as success (verified). The check raises `ApiException`, so `Retry-After` still works.
   - Sync already raises in the library (verified).
4. **Measurement.** Before (PR 7) vs Part 3 only vs all parts, 3 runs each, plus a forced-timeout run and one at N=10,000. On a new `par-` kind cluster, with a benchmark copy that records the re-list item count. Target: release peak 69.5 MB to at most about 10 MB, list peak to at most about 50 MB, `release()` time unchanged.
5. Commits on `feat/batch-8-list-memory`, off live PR 7: (1) page claim lists, (2) count live claims page by page in `release()`, (3) don't read the deletecollection response. Sync and async together in each commit, with tests in both shells.

### Decisions

**C1. Approve Parts 1 and 3 as above.**
- **(Recommended) Yes.** Part 3 is the main `release()` fix (about 69 MB down to about one page). Part 1 saves about 30% of the attach peak, not 90%, because callers keep every claim. That is honest but smaller than section 3B expected.
- Part 3 only. Smaller PR, but it leaves attach and the timeout path linear in N, and you already put pagination in PR 8.

**C2. How sync sends the metadata-only Accept header.** You have to decide because both options add code. No per-call header works on every supported sync version: `kubernetes` 37 honours `_headers`, but 35 rejects it (verified).
- **(Recommended) A dedicated `ApiClient` with a default Accept header.** It follows the existing pattern `_api_client_without_retries` (`k8s_helper.py:54-73`), which already builds a second client for Lease calls by copying config, cookie and headers. To stay DRY, that copy logic would likely be shared rather than duplicated (inferred). One code path for every version. Costs: one more connection pool, and sync `K8sHelper` has no `close()` (same as the existing Lease client).
- Try `_headers`, and on `ApiTypeError` (kubernetes 35) fall back to `call_api`. No new client, but two code paths keyed on the library version, both needing tests.
- Line counts for the two options are not measured yet. If you want them before deciding, the implementation task can produce both and stop.

**C3. Trimming `managedFields` from listed claims.**
- **(Recommended) Not in PR 8.** It is not in tasks.md 4.1, and it changes what the helper returns. At most, measure its size in the same run.
- Include it. It would cut the retained 46 MB by an unknown share (nothing in the SDK reads `managedFields`, verified).

**C4. Page size 500.**
- **(Recommended) 500.** Same as client-go's pager and the reaper. 10 requests at 5,000 claims, about 2 MB temporary per page (inferred).
- 100: 50 requests. 1000: 5 requests, more memory per page.

**C5. Correct section 7: the release peak is the deletecollection response, not the re-list.**
- **(Recommended) Accept**, with the Part-3-only run and the recorded re-list item count turning it from inferred into measured.

**C6. Order with B.** B's fix rewrites transport, so every branch above it, including PR 7, gets a new head.
- **(Recommended) Start PR 8 after B's sync lands.** One base, no rebase. B is small, so the wait is short.
- Start now on the current PR 7 and rebase onto the new PR 7 head afterwards. Faster start, plus one extra mechanical rebase.

**Also needed: your word that the stack has settled**, so the B review copy is built on stable heads.
