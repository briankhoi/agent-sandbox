# PR 8 plan: claim-list memory (was "7b", tasks.md row 4.1)

Status: built on `feat/batch-8-list-memory` (`e5367671` on PR 7 `91d5ffc4`) and measured on `review/pr8/bench` (`99ef5e56`, `work/results/C1-pr8-impl.md`). Verified 2026-10-27 (ACCEPT WITH FIXES). Decisions settled by Brian 2026-10-28: keep commits 1–3 and commit 6 (`managedFields` trim); drop commits 4 and 5 (`PartialObjectMetadataList` count and deletecollection) and the sync metadata `ApiClient`; fold `del page` (4 lines) and per-page `_request_timeout` docstrings into commits 1 and 2 (`work/prompts/C2-pr8-fixes.md`); handle pre-existing sync `deletecollection` urllib3 retries in a separate small PR.

## Pins
- Code read at PR 7 head `a26979f1` (`origin/feat/batch-7a-create-path`, 2026-10-22 14:51). SDK paths below are under `clients/python/agentic-sandbox-client/k8s_agent_sandbox/`.
- Bench: `origin/bench/batch-claim` `4a205e9f`. Notes base `b8c64a0d`.
- Libraries read: venv `kubernetes` 37.0.1, `kubernetes_asyncio` 36.1.0, `urllib3` 2.8.0, `aiohttp` 3.14.5. Also unpacked wheels `kubernetes` 35.0.0 and `kubernetes_asyncio` 33.3.0, because `pyproject.toml:18` allows any `kubernetes` except 36.0.0 and `:56` pins `kubernetes_asyncio<34.0.0`. Site-packages paths below are abbreviated `k37/`, `ka36/`, `k35/`, `ka33/`.

## Evidence (measured, `results/1d-n5000-memprobe-sync-run{1,2,3}.json` on the bench branch, SDK `tip.44fb487`)
- One `list_sandbox_claim_objects` of 5,000 claims: traced peak 69.42 to 69.44 MB, **46.31 to 46.34 MB while the items are held**, 0.003 MB after `del`. So about 23 MB of the peak is transient (response bytes and decoded text) and about 46 MB is the parsed dicts.
- `release()`: traced peak 69.46 MB, 1 round, deletecollection 99.9% of 18.5 to 22.0 s, the re-list 2 to 8 ms (`benchmark_rerun_review.md` 3B and 6 Q3 quote the same peaks).
- Section 6 Q3 and 7 left open whether the release peak is the deletecollection body or the re-list, and section 7 assumed the re-list returns all N. **New reading: the re-list was near empty.** It took 2 to 8 ms against 430 to 510 ms for the full 5,000-claim list in the same runs **(measured)**, and claims carry no finalizer (the claim controller has only the RBAC marker, `extensions/controllers/sandboxclaim_controller.go:281`; the SDK sets none) **(verified)**, so a deleted claim is gone at once. So the 69.5 MB release peak is parsing the deletecollection body **(inferred from the timing)**. The full re-list is the peak only when deletecollection times out at 90 s (`batch_utils.py:55`, comment `sandbox_batch.py:1523-1524`), around 20k+ claims at the measured 230 to 270 deletes/s **(inferred)**.

## Verified facts (both helpers)
| Claim in row 4.1 | Sync | Async | Verdict |
|---|---|---|---|
| No pagination today | `k8s_helper.py:573-583` one call, no `limit`/`_continue` | `async_k8s_helper.py:603-613` same | verified |
| Attach lists full objects | `sandbox_batch.py:327-329` | `async_sandbox_batch.py:324-326` | verified |
| Same list also used by | precheck `sandbox_batch.py:258`, 410 re-list `:1090`, `release_group` via `_delete_claims_in_rounds` `:873` | `:255`, `:1109`, `:899` | verified (row 4.1 omits these callers) |
| Async atexit uses the sync path | `async_sandbox_client.py:45`, `:736`, `:755` run sync `_delete_batch_objects` on a sync `K8sHelper` | | verified, so sync changes reach async users |
| Watch starts at the list RV | `sandbox_batch.py:1082`, `:1103-1104` | `:1101`, `:1120` | verified |
| Attach needs spec and status | `batch_state.py:88-95` (warmPoolRef, annotations), `:140-171` (status, deletionTimestamp) | shared module | verified, so attach can't go metadata-only |
| `release()` re-list reads only a count | `sandbox_batch.py:1537-1540`, uses only `len(live)`/`== []` at `:1546-1567` | `async_sandbox_batch.py:1566-1569`, same uses | verified (needs `metadata.deletionTimestamp` only) |
| Re-list is a consistent read on purpose | comment `sandbox_batch.py:1535-1536` | `async_sandbox_batch.py:1564-1565` | verified |
| Deletecollection parses the body | `k8s_helper.py:690-697` no `_preload_content`; `k37/kubernetes/client/api_client.py:186-188` reads and deserializes | `async_k8s_helper.py:721-728`; `ka36/.../rest.py:198-201` reads, `api_client.py:217` deserializes | verified |
| No caller uses its return value | both return `None` (`k8s_helper.py:683`, `async_k8s_helper.py:713`); only caller `sandbox_batch.py:1525`, `async_sandbox_batch.py:1554` | | verified |
| The reaper lists claims | No. It lists Leases only, already paged at 500 (`batch_reaper.py:48`, `:96-127`) | sync only | verified, out of scope |

Library facts (verified): every version passes `limit` and `_continue` (`k37/.../custom_objects_api.py:4587`, `:4590`; `ka36/...:3180-3187`). A custom Accept header is not portable: `k37` honours `_headers={'Accept': ...}` (`:4932`), `k35` rejects `_headers` with `ApiTypeError` (`k35/...custom_objects_api.py:2513-2526`), and `ka36`/`ka33` copy `_headers` then overwrite Accept (`ka36/...:3197`, `:3204`; same lines in `ka33`). All four merge `ApiClient.default_headers` after the generated Accept (`k37/api_client.py:287`, `ka36:134`, `k35:131`, `ka33:133`), so only a dedicated `ApiClient` with a default Accept works everywhere. With `_preload_content=False`: `k37` raises `ApiException` on non-2xx and returns the unread urllib3 response (`api_client.py:176-185`); `k35` raises in the REST layer (`rest.py:237-238`); **`ka36` and `ka33` return the raw aiohttp response with no status check** (`ka36/rest.py:197-208`, `api_client.py:199-200`; `ka33/rest.py:197-208`). The caller owns the response: urllib3 `drain_conn()`/`release_conn()` (`urllib3/response.py:810-833`), aiohttp `release()` (`aiohttp/client_reqrep.py:813-821`).

## Part 1. Paginate `list_sandbox_claim_objects` (attach, 410 re-list, precheck)
Recommendation: page inside the helper at `limit=500`, follow `continue`, return all items and the **first page's** resourceVersion; on a 410 with a continue token, restart the list once without it. Signature unchanged, so callers and their mocks stay.
- Correctness. A paginated list is one consistent snapshot (`k37` `limit` docstring, `custom_objects_api.py:4590`, which also says a page may hold zero items while `continue` is set, so only `continue` ends the loop), so every page carries the same RV and first equals last **(inferred from the snapshot rule; checked in the measurement run)**. client-go's pager takes the first page's RV (`client-go/tools/pager/pager.go:121-123`, vendored copy) and on an expired token falls back to a full list (`:94-104`); default page 500 (`:31`). Using the 410's inconsistent continue token is rejected: it breaks the snapshot the watch relies on.
- Win, honestly: only the transient part shrinks, because every caller keeps all items. Expected peak about 46 + 2.3 = 48 to 49 MB at N=5,000, not 7 MB as 3B said **(inferred)**.
- Alternatives:
  - No change. 0 lines; 69 MB peak at 5k, linear in N.
  - `limit` 100 / 500 / 1000: 50 / 10 / 5 requests at 5k, transient about 0.5 / 2.3 / 4.6 MB **(inferred)**. 500 matches client-go and the reaper.
  - Also drop `metadata.managedFields` per item after each page (nothing in the SDK reads it, verified by search). Cuts the retained 46 MB by an unknown share, about 5 lines per helper. Needs Brian's OK (it changes what the helper returns).
  - Streaming JSON parse (`ijson` on the raw response): new dependency against the minimal-deps rule, raw-response code in both shells. Rejected.
- Cost: about 20 lines per helper. Same `list` verb, no RBAC change. N/500 requests instead of 1 (each within the 90 s timeout).

## Part 2. `release()` re-list: count live claims page by page
This fixes the timeout path only (see Evidence); Part 3 fixes the normal path. Recommendation: a new helper `count_live_sandbox_claims(namespace, label_selector, _request_timeout)` in both helpers that pages at 500, counts items without `deletionTimestamp`, and drops each page. `_delete_claims_in_rounds` (both shells) uses the count. Peak becomes one page, about 7 MB at any N **(inferred: 500 x 13.9 KB)**. Same consistency (no RV on the first page), same verb.
- Metadata-only on top (row 4.1's proposal): **in PR 8 (Brian approved including it, 2026-10-22); the byte measurement below sets the final shape.** It needs a dedicated `ApiClient` per helper (second connection pool, closed in both clients' `close()`), because no per-call header works on all supported versions (above). PartialObjectMetadata still carries `managedFields`, labels and annotations, so the saving per page is unknown **(inferred)**. In the measurement run, compare `kubectl get --raw` sizes of one claim list with and without the Accept header and report the saving; if a page shrinks by less than half, bring Brian the numbers before dropping it.
- Alternatives:
  - No change: 69 MB peak per round.
  - Paged full list but still accumulated: same 48 MB as Part 1.
  - `limit=1`: rejected (section 7). Terminating claims can't be filtered on the server, so one item can't prove none is live.
  - The batch watch: rejected (section 7). `release()` stops it first (`sandbox_batch.py:943`), the atexit path has none (`async_sandbox_client.py:755`), and it is served from the watch cache whose lag is why the code reads consistently.
  - `fieldSelector`/`labelSelector` tightening: CRDs select only `metadata.name`/`namespace` plus CRD `selectableFields`, which can't name `deletionTimestamp` **(inferred)**; a "deleting" label is one extra write per claim. Rejected.
  - `remainingItemCount` with `limit=1`: counts terminating claims too. Rejected.

## Part 3. Don't read the deletecollection body
Recommendation: pass `_preload_content=False`, then drain the body in chunks and release the connection in both shells, and return `None`. On a non-2xx the async helper reads the (small) error body and raises the library's `ApiException` built from it, as `ka36/rest.py:200-207` does, so a 403 stays immediate and a 429 keeps `Retry-After` for `retry_delay` (`batch_utils.py:388-399`). This is required: async does no status check without preload (`ka36/rest.py:197-208`). The sync library already raises on non-2xx in both supported generators (`k35/rest.py:237-238`, `k37/api_client.py:174-184`), so the sync helper adds no second check; one comment, identical in both files, says why the shells differ.
- Alternatives:
  - No change: the body peak stays at 69 MB once Part 2 lands.
  - `as=PartialObjectMetadataList` on deletecollection: smaller body still parsed, same header problem. Rejected.
  - Close instead of drain: one new TLS connection per round, simpler. Acceptable fallback.
- Cost: about 15 lines per helper. Timing unchanged: the response comes after the server finishes deleting **(inferred)**.

## Risks
- Async non-2xx silently treated as success if the status check is missed (verified library behaviour above).
- Continue-token expiry (5 to 15 min, `k37` `_continue` docstring) is unlikely at 10 pages, handled by one restart in both list helpers; a second 410 propagates. Without the restart a 410 would fail `release()` at once and keep the Lease, because 410 is not transient (`batch_utils.py:201-205`, `sandbox_batch.py:92-95`). The watch-loop re-list already retries on 410 (`sandbox_batch.py:1137-1142`).
- `_request_timeout` becomes per page, so one list can take pages x 90 s in total.
- An undrained response keeps a pooled connection checked out. The pool is sized on purpose (`k8s_helper.py:103-105`, `async_k8s_helper.py:88-93`); on async that could starve the watch and Lease renewal **(inferred)**.
- Paginated reads with no RV may go to etcd instead of the watch cache, depending on apiserver version **(inferred, unverified)**.
- The bench counts requests by wrapping `ApiClient.call_api` (`bench.py` docstring, lines 3-5): a second `ApiClient` (if metadata-only is adopted) must be wrapped too.

## Measurement plan (implementation prompt runs it)
- Harness: `test/benchmarks/batch-claim/bench.py` at `4a205e9f`, scenario `1d n5000-memprobe` (`bench.py:2052-2055`, probe `:960-999`, sync shell). Kind cluster `par-mem` (`--config test/benchmarks/batch-claim/kind-config.yaml`), `KUBECONFIG` per arm (`bench.py:606`), `BENCH_RESULTS` per arm (`:64`), SDK installed into the bench worktree's `bin/bench-venv` (`:79`). Before = PR 7 head, after = the `review/pr8/...` head, 3 runs each, interleaved.
- Arms: before; Part 3 only; all parts, so the release peak is attributed. The timeout path: one run with `batch_utils.BATCH_COLLECTION_REQUEST_TIMEOUT_SECONDS` set to 5 s by the driver, so deletecollection times out and the paged count carries the round.
- Success: `release_traced_peak_mb` from 69.5 to at most 10 on both paths; `list_traced_peak_mb` from 69.4 to at most 50 (`list_traced_with_result_mb` unchanged near 46.3, lower only with managedFields trimming); `release_s` within the 18.5 to 22.0 s band; `release_rounds` 1; lists per release round = ceil(N/500). One extra run at N=10,000 shows the release peak doesn't grow with N. Also log each page's RV once to confirm they match, and the metadata-only byte ratio.

## Unit tests (one owning test per contract, both shells; mutation-check each)
1. `list_sandbox_claim_objects` follows continue tokens with `limit=500` and the same selector, through an empty middle page, returns all items and the first page's RV (mutate: drop `_continue`; stop on an empty page; return the last RV from a fake whose pages differ).
2. A 410 on a continue page restarts once from scratch; a second 410 raises (mutate: remove the restart).
3. `count_live_sandbox_claims` counts only claims without `deletionTimestamp` across pages (mutate: count all).
4. `delete_sandbox_claims_by_label` passes `_preload_content=False` and releases the response (mutate: skip release).
5. Async: a non-2xx deletecollection raises `ApiException` with its status and `Retry-After`, and still releases the response (mutate: remove the status check). Sync gets no twin, since the contract lives in the library and a mock test would only test the mock.
6. Existing release-round tests (`test_sandbox_batch.py:1699`, `test_async_sandbox_batch.py:1760`) switch their mocks to the count helper; no new test.

## Implementation outline
- Branch `feat/batch-8-list-memory` off the live PR 7 head, worked on `review/pr8/list-memory`. Files: `k8s_helper.py`, `async_k8s_helper.py`, `sandbox_batch.py`, `async_sandbox_batch.py`, `test_k8s_helper.py`, `test_async_k8s_helper.py`, `test_sandbox_batch.py`, `test_async_sandbox_batch.py`.
- Commits: (1) page claim lists (Part 1, tests 1-2); (2) count live claims page by page in release rounds (Part 2, tests 3 and 6); (3) don't read the deletecollection response (Part 3, tests 4-5). Each commit sync and async together, identical comments.
- Docs: no public docstring changes expected (`release()` text at `docs/python_sdk_reference.md:1125` stays true); confirm `make generate-python-docs` leaves no diff.

## Decisions for Brian
1. Approve Part 3 (the main release fix) and Part 1 as above. Recommended: yes. Part 1's gain for attach is about 30% of the peak, not 90%.
2. Part 2 as a page-by-page count plus the metadata-only list (approved by Brian, 2026-10-22), sized by the byte measurement.
3. Trim `managedFields` from listed items in Part 1. Recommended: measure in the same run, decide after.
4. Page size 500. Recommended: yes.
5. Accept the correction to section 7: the release peak is the deletecollection body, not the re-list (timing above). Recommended: yes, confirm in the Part-3-only arm.

## Self-check (fresh reviewer, given only the pin, the three parts and the rules)
- Agreed: async status check and response release are blockers for Part 3; metadata-only needs a version-branched bypass, so drop or defer it; accumulated pages save only the transient part; 410 restart needed; first page's RV is correct.
- New from the reviewer, adopted after reading the code: no claim finalizer, so the release peak is the deletecollection body (confirmed by the 2 to 8 ms re-list timing); `release_group` and the async atexit sync path are also callers; per-page timeouts; the sync helper must not re-check status.
