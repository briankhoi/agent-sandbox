# C1 result: PR 8 implementation and memory measurement

Branch `feat/batch-8-list-memory` = `e5367671`, 6 commits off live PR 7 `91d5ffc4` (types sync present, `async_k8s_helper.py:105` cast checked). Bench branch `review/pr8/bench` = `99ef5e56`. Both pushed to `origin`. Nothing opened, nothing commented. Kind cluster `par-pr8` deleted. No stack branch touched.

## 1. Summary
- Commit 3 (stop parsing the deletecollection body) does almost all of the release work. At N=5000 the release peak drops from **69.4 MB to 0.2 MB** (measured, A vs B2). This confirms the plan: the release peak was the deletecollection body, and A's re-list returned 0 items.
- Commit 1 (paging) lowers the list peak from **69.4 to 48.7 MB** (measured), as predicted (48 to 49).
- On the deletecollection-timeout path, commit 2 (paged count) holds the release at **9.7 to 11.8 MB**, against **81.7 MB** on A. At N=10,000 it stays at 9.9 MB, against 138.8 MB on A.
- Optional commits, savings for your call:
  - Commit 4 (metadata-only count): about 2 MB (17%), timeout path only.
  - Commit 5 (metadata-only deletecollection): about 15% fewer wire bytes, no traced-memory or timing effect.
  - Commit 6 (`managedFields` trim): halves the list peak, 48.7 to 24.1 MB.
- The bench found a real bug, fixed in commit 4 with regression tests. For an empty result, a metadata-only list sends `items: null`, which crashed `release()` on arm D.
- Pre-existing, not caused by PR 8: urllib3 retries a timed-out sync deletecollection 3 times, so one 90 s timeout can stretch to about 6 minutes per round. This needs a decision from you (decision 3).

## 2. Findings (ranked)

### F1. Commit 3 removes the release peak on the normal path. Recommendation: keep.
- **Measured** (N=5000, 3 or more runs per arm, `review/pr8/bench` `results/pr8/1d-n5000-memprobe-*`):
  - `release_traced_peak_mb`: A 69.34 to 69.43, B2 0.19 to 0.21.
  - In A the re-list returned 0 items (`release_list_items [0]`) and took 1 list request.
- **Verified:**
  - Sync passes `_preload_content=False`, drains with `stream()` and calls `release_conn()` in `finally` (`k8s_helper.py:758-789`). It adds no status check, because `kubernetes` 37 raises before returning (`api_client.py:174-185`).
  - Async checks the status itself and raises `ApiException(http_resp=RESTResponse(r, body))` (`async_k8s_helper.py:829-863`). `kubernetes_asyncio` 33.3/36.1 `rest.py:197-208` doesn't check.
  - The error body is decoded the way the library decodes it (`api_client.py:190-192`), so `e.body` is a `str` like every other `ApiException`.
  - `release()` goes through `iter_any()` and calls `release()` in `finally`.

### F2. Commit 1 cuts the list peak by 30%. Recommendation: keep.
- **Measured:**
  - `list_traced_peak_mb`: B2 69.37 to 69.43, C2 48.68 to 48.81.
  - `list_traced_with_result_mb` stays at 46.3 to 46.5.
  - N=10,000: list peak 138.9 MB on A, 43.6 MB on F2.
  - Each list was 10 pages of 500 at N=5000 (20 at N=10,000), and all pages of one list carried one resourceVersion.
- **Verified:**
  - One page iterator per shell (`k8s_helper.py:85`, `async_k8s_helper.py:57`).
  - It stops only on `continue`, returns the first page's RV and restarts once on a 410 for a continue page. A second 410 propagates.
  - `list_sandbox_claim_objects` keeps its signature, so every caller (precheck, attach, watch re-list) gets paging.

### F3. Commit 2 bounds the timeout path to a page or two. Recommendation: keep.
- **Measured, forced 5 s collection timeout:**

  | Arm | Rounds | Release peak | Live claims per round | List requests per round |
  |---|---|---|---|---|
  | A | 3 | 81.73 MB | `[4885, 2609, 0]` | 1 each |
  | C2 | 2 | 11.77 MB | `[2009, 0]` | `[5, 1]` |
  | D2 | 2 | 9.72 MB | `[1546, 0]` | `[4, 1]` |

- **Measured, normal path:** three runs hit the apiserver's own 60 s deletecollection timeout (504): D2 run2, E2 run3 and F2 at N=10,000. The paged count carried each one to 9.6 to 9.9 MB. So this path is not rare under load.
- **Inferred:**
  - The 10 to 12 MB is about two parsed pages. The consumer's loop variable and the generator both still hold the previous page while the next one is fetched.
  - A `del` on both sides could roughly halve it (decision 4).

### F4. Bug found by the bench and fixed in commit 4: `items: null`.
- **Measured** with curl on `par-pr8` (Kubernetes 1.36.1):
  - For an empty result, a `PartialObjectMetadataList` sends `"items": null`; a full list sends `[]`.
  - `page.get("items", [])` returned `None`, and `release()` crashed (old D-ct5 run, kept in the repo as superseded).
- **Fix:** `page.get("items") or []` in the count (`k8s_helper.py:641-662`, async `:662-682`).
- **Tests:** the metadata tests in both shells now end with a null page (`test_k8s_helper.py:1133`, `test_async_k8s_helper.py:1152`). With the old line restored, both fail with `TypeError`.
- I was wrong about metadata-only pages being shaped like full lists, because my mocks always returned `items: []`.

### F5. Optional commits, savings for your call (all measured unless marked)

| Commit | Path it can affect | Before | After | Saving |
|---|---|---|---|---|
| 4: metadata-only count | Timeout path release peak (C2-ct5 vs D2-ct5) | 11.77 MB | 9.72 MB | 2.05 MB (17%) |
| 4 | Normal path | 0.14 to 0.19 MB | 0.19 to 0.21 MB | none (the re-list is empty) |
| 5: metadata-only deletecollection | Wire bytes | 9,492 B for 3 claims | 8,031 B | 15.4% |
| 5 | Wire bytes at 5000 (inferred) | 11.5 MB | about 9.7 MB | about 15.6% |
| 5 | `release_s` | D2 30.7 to 33.1 s | E2 29.1 to 50.4 s | not separable from host-load noise |
| 6: `managedFields` trim | `list_traced_peak_mb` | 48.7 MB | 24.1 MB | 24.6 MB (50%) |
| 6 | `list_traced_with_result_mb` | 46.4 MB | 18.9 MB | 27.4 MB (59%) |

- **Commit 4:**
  - The metadata-only page keeps `managedFields`, so it is only 84.4% of a full page in bytes (measured `list_bytes`).
  - Since commit 3, the normal-path re-list returns 0 items, so commit 4 does nothing there.
- **Commit 5:**
  - The apiserver honours the header on deletecollection: `PartialObjectMetadataList`, HTTP 200, `items` without spec and status (measured, kubectl proxy plus curl, throwaway label, 3 claims).
  - No traced-MB effect, because commit 3 already stopped decoding the body.
  - E2/F2 release peaks (0.21 to 0.34 MB) sit about 0.1 MB above D2. Inferred cause: the dedicated client opening its own connection.
- **Commit 6:**
  - The attach keeps only `Member`s, not the dicts (verified, `batch_state.py:261-275`, `:303`). So the trim lowers the transient attach peak, not memory held over time.
  - `managedFields` is 50.6% of claim bytes (measured).
  - Nothing in the SDK reads it (verified with `git grep managedFields`).
- **Dependencies:** commit 5 depends on commit 4, which adds the routes it reuses. Commit 6 stands alone.
- **Async:** these savings are unmeasured, because 1d is sync-only.

### F6. Pre-existing: urllib3 retries a timed-out sync deletecollection 3 times. Recommendation: a separate small PR (decision 3).
- **Measured:**
  - A-ct5: each 5 s timeout took about 20 s and ended in `MaxRetryError`.
  - The apiserver counted 12 DELETEs for 3 SDK calls.
- **Verified:**
  - `kubernetes` 37 passes `retries` only when it is set (`rest.py:155-156`).
  - urllib3 defaults to `Retry(3)` and includes DELETE among the retried methods (`urllib3/util/retry.py:199-200`, `:569`).
  - The reviewer saw the same thing for a 429 with Retry-After.
- **Inferred:**
  - In production a 90 s timeout can become about 360 s per round on the sync and async-atexit paths.
  - Async (aiohttp) doesn't retry.
- **Not changed in PR 8:** the change would alter release timing, which is out of PR 8's scope. Options:
  - send deletecollection through a no-retries client;
  - set `retries` on that request.

### Other measured facts
- **Host:** 24 CPUs and 94 GiB. Load average 11 to 20 most of the time (53.9 once), available memory 20 to 69 GiB. Clusters `agent-sandbox` and `par-pr8`. Recorded per run in `results/pr8/host/`.
- **`release_s`:** follows the host load, from 29 to 50 s in every arm at N=5000. 1-round runs are within that band for all arms, so there is no SDK timing regression (measured). The 18.5 to 22.0 s band in the plan came from another host.
- **E2 run3:** its claims were smaller, because the controller hadn't finished status writes (8.5 MB list instead of 11.5 MB). It is excluded from the list comparisons. F2 at N=10,000 also had slightly smaller claims than A (21.3 vs 22.9 MB list).
- **Request counts:**
  - On the normal path the SDK sends 1 deletecollection, 1 count page and 1 Lease delete per release.
  - The counter sees the dedicated sync client and the direct async route. The `(meta)` key appears in D2 (release lists), and in E2 and F2 (lists and deletecollection).
- **SDK SHA and tree per arm:**

  | Arm | Change | SHA | Tree |
  |---|---|---|---|
  | A | PR 7 | `91d5ffc4` | `e305e548` |
  | B2 | throwaway local commit (PR 7 + commit 3), not pushed | `dfa9d2f9` | `beff60fb` |
  | C2 | commits 1-3 | `fb2a7e52` | `127a201a` |
  | D2 | + commit 4 | `30e6fb20` | `6d8f96f1` |
  | E2 | + commit 5 | `d8ec0178` | `ce0e790a` |
  | F2 | + commit 6 | `e5367671` | `6ea16e4c` |

## 3. Self-check
- **Reviewer:** a fresh `inherit` reviewer got only P7, the head `e7034777`, the task section and the rules. It found no blockers. Resolutions, each read in code:
  1. *The count's restart reset was untested.* Valid. I added a 410 restart case to both count tests (commit 2). With the reset removed, both fail with `5 != 3`.
  2. *The async error body was bytes, while the library's errors carry str.* Valid (`api_client.py:190-192`). I now decode it (commit 3), and the test asserts `str`. That mutation is killed.
  3. *The Accept header had no fallback, so a 406 would fail release.* Adopted (commit 4), as client-go's metadata client does. Measured: the apiserver still answers with `PartialObjectMetadataList`.
  4. *Smaller async adapter.* Not adopted. Binding the callable at 4 call sites would repeat group, version, plural and limit; the iterator keeps them in one place.
  5. *No test that the sync Accept header reaches the wire.* Not added. Default headers override the generated Accept (`kubernetes` 37 `api_client.py:287`, verified), and a regression would fail benignly with the same count.
  - Its notes (commit 5 depends on 4, commit 6 only lowers the transient attach peak, the 429 retry) are in F5 and F6.
- **Mine, not raised by the reviewer:** the two-pages-held inference (F3) and the urllib3 timeout retry (F6, found by the bench).
- **Mutation checks:** every new test was mutation-checked in both shells and fails for the intended reason:

  | Test | Mutations |
  |---|---|
  | Paging | drop `_continue`; stop on an empty page; return the last RV |
  | 410 | remove the restart |
  | Count | count all items; drop the restart reset |
  | Deletecollection | skip release; release outside `finally`; drop `_preload_content` |
  | Async status | no status check; undecoded body |
  | Commit 4 | no Accept; full API used; shared client instead of a copy; no header copy; null items |
  | Commit 5 | full API used; no Accept on DELETE |
  | Commit 6 | no trim |

  One commit 4 test first hung on a MagicMock page under mutation. I fixed it by giving the patched full-list API a dict page.
- **Checks per commit** (both venvs, `kubernetes_asyncio` 36.1.0 and 33.3.0):
  - mypy clean, and pycodestyle (max line length 120) clean on changed lines.
  - Targeted test files pass: the helper, batch, sandboxclient and lifecycle-integration files, 547 to 557 tests.
  - `make generate-python-docs` equivalent (pydoc-markdown 4.8.2 outside the repo): no diff.
  - I once started the whole Python unit directory by mistake and killed it before it finished.
- **Test audit** (`skills/test-audit`, read-only): **0 of 23 new or changed tests recommended for removal.**
  - Kept despite resembling a junk pattern:
    - The count case in the timeout-forwarding tables (`test_k8s_helper.py:1452`, `test_async_k8s_helper.py:1360`) is a distinct contract (timeout forwarding), not a duplicate of the count test.
    - The metadata-routing tests (`test_k8s_helper.py:1133`, `:1164`, `test_async_k8s_helper.py:1152`) look like call-shape tests. They are the only guard of the wire contract (header, path, `_preload_content`).
    - The changed injection test (`test_k8s_helper.py:1018`) now uses `assert_any_call`, because the helper builds a second `CustomObjectsApi`. It still proves the injected client is used.
  - Release-round tests and fixtures: I switched them to the count and removed now-dead list setup in `test_release_stops_creates_then_deletes_claims_then_the_lease` (both shells).
  - Verified by mutation (above). No inferred-only candidates.
- I was wrong about D's full SHA in a message to the bench subagent, because I extended the short SHA by hand. The subagent caught it before any run used it.

## 4. Decisions for Brian
1. **Which optional commits stay:**
   - Commit 4: 17% on the timeout path only, plus a dedicated sync `ApiClient`.
   - Commit 5: about 15% wire bytes, no memory or timing effect.
   - Commit 6: halves the attach and list peak.
   - My recommendation: keep 6. Keep 4 and 5 only if you value the timeout-path and wire savings over the extra client and request code.
2. **Sync metadata route:** built with a dedicated `ApiClient`, per coordinator 2's proposal, which you hadn't explicitly answered. It is created eagerly, keeps the library's retries and shares copy logic with the Lease client (`k8s_helper.py:56-82`, `:158-162`). Async uses `call_api` on the shared client (`async_k8s_helper.py:684-731`). OK?
3. **urllib3 timeout retries on the sync deletecollection (F6):** a separate small PR (recommended), fold into PR 8, or leave as is?
4. **Count holds about two pages (F3, inferred):** add `del page` on both sides to aim for one page (about 5 MB on the timeout path), or leave it?

### Draft PR (title and description cover this PR only)
**Title:** `perf(python-sdk): page the claim list and stop parsing the deletecollection body (20/N)`

**Description:**
> Releasing or attaching to a large batch held whole claim lists in memory. At 5,000 claims, `release()` peaked at 69 MB decoding the deletecollection response, which no caller reads. One claim list peaked at 69 MB too.
>
> - Claim lists are paged at 500 (client-go's page size). The first page's resourceVersion still starts the watch, and an expired continue token restarts the list once.
> - The release rounds count the claims not yet being deleted page by page, instead of holding every remaining claim. The count lists metadata only (`PartialObjectMetadataList`).
> - `delete_sandbox_claims_by_label` drains the deletecollection response unread, asking for metadata only, and releases the connection in a `finally` block. The async helper raises `ApiException` for a non-2xx response itself, because `kubernetes_asyncio` doesn't check the status of an unread response.
> - Listed claims drop `metadata.managedFields`, which nothing in the SDK reads.
>
> Measured on kind with 5,000 claims (sync):
> - `release()` peak: 69.4 MB to 0.3 MB.
> - When deletecollection times out: 81.7 MB to 9.7 MB.
> - Claim-list peak: 69.4 MB to 24.1 MB.
>
> At 10,000 claims:
> - `release()` peak: 138.8 MB to 9.9 MB.
> - List peak: 138.9 MB to 43.6 MB.
>
> Release time is unchanged within noise.
>
> (Adjust the numbers and bullets to the commits Brian keeps.)

### Suggested changes to `pr8_plan.md` (coordinator 2 edits)
- **Line numbers:** stale; the pins are now PR 7 `91d5ffc4` and branch `e5367671`.
- **Release peak:** the "deletecollection body" claim is confirmed (A vs B2, re-list items 0).
- **Metadata-only:**
  - It saves 15.6% of bytes, measured, since `managedFields` stays.
  - It needs no `close()` (sync `K8sHelper` has none), and async opens no second pool.
- **Metadata-only quirks to add:** `items: null` on empty results; the `,application/json` fallback.
- **Rejected alternative to drop:** "as=PartialObjectMetadataList on deletecollection rejected". It was built as commit 5 through the same routes.
- **Success criteria:**
  - "lists per release round = ceil(N/500)" applies only on the timeout path; the normal path is 1.
  - The `release_s` band is host-dependent.
- **New risks:**
  - The apiserver's 60 s request timeout (504) fires before the SDK's 90 s, measured at 5,000 claims under load.
  - urllib3 retries the timed-out deletecollection (F6).
  - The count holds about two pages (F3).

## 5. Links
- PR 8 full diff: https://github.com/briankhoi/agent-sandbox/compare/91d5ffc485dbd13fd762e833c08af5ffdb9da02f...feat/batch-8-list-memory
- Per commit:
  1. page the claim list: https://github.com/briankhoi/agent-sandbox/commit/76d718e0
  2. count live claims page by page: https://github.com/briankhoi/agent-sandbox/commit/765e159f
  3. stop parsing the deletecollection response: https://github.com/briankhoi/agent-sandbox/commit/fb2a7e52
  4. metadata-only count (optional): https://github.com/briankhoi/agent-sandbox/commit/30e6fb20
  5. metadata-only deletecollection (optional): https://github.com/briankhoi/agent-sandbox/commit/d8ec0178
  6. drop managedFields (optional): https://github.com/briankhoi/agent-sandbox/commit/e5367671
- Bench harness and results: https://github.com/briankhoi/agent-sandbox/compare/4a205e9f...review/pr8/bench
- Bench README (arm table, caveats, provenance): https://github.com/briankhoi/agent-sandbox/blob/review/pr8/bench/test/benchmarks/batch-claim/results/pr8/README.md
- This report: https://github.com/briankhoi/agent-sandbox/blob/batch-primitives-notes/batch-primitives/handoff/work/results/C1-pr8-impl.md
