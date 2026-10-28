# C3 result: build `feat/batch-10-deletecollection-retry` (`0a80c60c`)

Branch `feat/batch-10-deletecollection-retry` = `0a80c60cee6a3a52e725757f5275993b0218941f`, 1 commit on `origin/feat/batch-8-list-memory` at `1b061c53b2a500d872b2c63f7aa9e1033d93013a` (`origin/feat/batch-9-types` did not exist on `origin` at start `05:18Z` or before push `05:46Z`). Pushed to `origin`; worktree `agent-sandbox-par-retry22` removed. No GitHub issues or PRs opened or commented on.

## 1. Summary
- Replaced `_api_client_without_retries` (`k8s_helper.py:55-74` at `1b061c53`) with `_api_client_with_retries(api_client, retries)` (`k8s_helper.py:59-82` at `0a80c60c`) and `_CLAIM_COLLECTION_RETRIES = urllib3.Retry(3, read=False)` (`k8s_helper.py:56`).
- Added `self._claim_collection_api` in `K8sHelper.__init__` (`k8s_helper.py:157-159`), routing `count_live_sandbox_claims` (`k8s_helper.py:659`) and `delete_sandbox_claims_by_label` (`k8s_helper.py:784`) through it while keeping `coordination_v1_api` on `retries=False` (`k8s_helper.py:161`) and single-resource calls on `self.custom_objects_api` (`k8s_helper.py:156`).
- Updated `TestK8sHelperApiClientInjection` (`test_k8s_helper.py:1014, 1025`) and added `TestLeaseApiClient.test_claim_collection_requests_do_not_retry_reads_and_keep_the_injected_client_settings` (`test_k8s_helper.py:1131-1180`), exercising both `ApiClient` shallow-copy isolation and end-to-end `urllib3` transport behavior over a local socket.

## 2. Findings (ranked)

### F1. Dedicated `_claim_collection_api` with `urllib3.Retry(3, read=False)` (`0a80c60c`). Recommendation: ready for PR 22/N.
- **Reason:** `kubernetes.client` fixes `urllib3` retry policy per `PoolManager` at `RESTClientObject.__init__` and exposes no per-call `retries` argument on `CustomObjectsApi`, `ApiClient.call_api`, or `RESTClientObject.request`, while neither `self.custom_objects_api` (`Retry(3)`, which retries `ReadTimeoutError` 3 times) nor `self.coordination_v1_api` (`retries=False`, which surfaces raw `NewConnectionError` outside `sandbox_batch._TRANSPORT_ERRORS`) matches the retry contract claim collection calls need; generalizing the private helper parameter to `retries: urllib3.Retry | bool` avoids duplicating the `Configuration` shallow-copy logic across the two retry-customized clients.
- **Verified:**
  - `RESTClientObject.__init__` forwards `configuration.retries` to `PoolManager` only when not `None` (`kubernetes/client/rest.py:155-156`), leaving `urllib3`'s `Retry.DEFAULT = Retry(3)` with `GET` and `DELETE` in `DEFAULT_ALLOWED_METHODS` (`urllib3/util/retry.py:199-200, 569`).
  - When `read=False`, `Retry.increment` checks `read is False` for `_is_read_error` (`ReadTimeoutError`, `ProtocolError`) and re-raises after 1 attempt (`urllib3/util/retry.py:409-413, 508-510`), whereas connection errors still retry 3 times and raise `MaxRetryError`.
  - Both `ReadTimeoutError`, `ProtocolError`, and `MaxRetryError` are in `sandbox_batch._TRANSPORT_ERRORS` (`sandbox_batch.py:77-85`), whereas `NewConnectionError` is not (`urllib3/exceptions.py`: subclass of `ConnectTimeoutError`, not `ConnectionError`).
  - `AsyncK8sHelper` needs no counterpart because `kubernetes_asyncio`'s `RESTClientObject.request` calls `aiohttp` once without retrying (`kubernetes_asyncio/client/rest.py:197`, `async_k8s_helper.py:665-685, 790-825`).
- **Measured:**
  - Direct `CustomObjectsApi` probe (`kubernetes 37.0.1`, `urllib3 2.8.0`): `retries=None` and `Retry(3)` make 4 HTTP attempts on `ReadTimeoutError` and raise `MaxRetryError`; `Retry(3, read=False)` makes 1 attempt on `ReadTimeoutError` (raises `ReadTimeoutError`) and 4 attempts on `NewConnectionError` (raises `MaxRetryError`); `retries=False` makes 1 attempt on `NewConnectionError` and raises raw `NewConnectionError`.
  - `mypy k8s_agent_sandbox` clean (`0` errors in 37 source files) in both `kubernetes_asyncio` 36.1.0 (`91dd6bc1.../scratch/venv`) and 33.3.0 (`00bb279e.../scratch/venv33`).
  - Targeted `pytest` (`test_k8s_helper.py`, `test_sandbox_batch.py`) passed in both venvs (`203 passed`).
  - `pycodestyle --max-line-length=120` on all changed lines in `k8s_helper.py` and `test_k8s_helper.py`: `0` hits.
  - Regenerating `docs/python_sdk_reference.md` via `pydoc-markdown 4.8.2`: `0` diff.

### F2. Unit test coverage and mutation verification (`test_k8s_helper.py:1014-1025, 1131-1180`). Recommendation: keep as written.
- **Reason:** Combines `ApiClient` copy assertions (`host`, `cookie`, `default_headers`, in-place rotated `api_key`, untouched `injected.configuration.retries`, and `Retry(total=3, read=False)` on both `configuration.retries` and `pool_manager.connection_pool_kw["retries"]`) with zero-thread loopback socket checks proving refused connects raise `MaxRetryError` and read timeouts on `delete_sandbox_claims_by_label` and `count_live_sandbox_claims` raise `ReadTimeoutError` after a single TCP connection.
- **Measured (mutation checks, all caught):**
  1. `_CLAIM_COLLECTION_RETRIES = urllib3.Retry(3)` -> fails (`AssertionError: None is not False`, plus 4 socket attempts / `MaxRetryError`).
  2. `_CLAIM_COLLECTION_RETRIES = urllib3.Retry(3, read=0)` -> fails (`AssertionError: 0 is not False`, plus `MaxRetryError` instead of `ReadTimeoutError`).
  3. `_CLAIM_COLLECTION_RETRIES = False` -> fails (`AssertionError: False is not an instance of Retry`, plus `NewConnectionError` instead of `MaxRetryError`).
  4. `delete_sandbox_claims_by_label` routed to `self.custom_objects_api` -> fails (`MaxRetryError` after 4 attempts instead of `ReadTimeoutError` after 1).
  5. `count_live_sandbox_claims` routed to `self.custom_objects_api` -> fails (`MaxRetryError` after 4 attempts instead of `ReadTimeoutError` after 1).
  6. Mutating `injected.configuration.retries` in place, dropping `cookie`/`default_headers`, or `deepcopy`-ing `configuration` (breaking rotated `api_key`) -> all fail.

## 3. Self-check
- Spawned a fresh `inherit` subagent (`d174a3a3-ae92-46fe-b53b-8eb6e9b71d27`) given only `1b061c53b2a500d872b2c63f7aa9e1033d93013a`, `0a80c60cee6a3a52e725757f5275993b0218941f`, the C3 task specification, `skills/test-audit/SKILL.md`, and the project rules.
- **Reviewer result:** Approved with `0` defects, `0` rule violations, and `0` `test-audit` removal candidates out of `2` touched tests. Independently verified that `K8sHelper(pool_size=...)` (`k8s_helper.py:152-159`) and `AsyncSandboxClient._atexit_cleanup` (`async_sandbox_client.py:796`) work cleanly with `_api_client_with_retries`, confirmed no smaller change exists because `HTTPConnectionPool` caches `self.retries` at construction (`urllib3/connectionpool.py:205`), and ran the full Python unit suite (`1058 passed`). No differences to resolve.

## 4. Decisions for Brian
- None. Note for Coordinator 2: `feat/batch-10-deletecollection-retry` (`0a80c60c`) is based on `origin/feat/batch-8-list-memory` (`1b061c53`) because `origin/feat/batch-9-types` was not yet pushed; rebase onto `feat/batch-9-types` once 21/N lands.

### Draft PR 22/N title and description

**Title:** `fix(python-sdk): stop urllib3 from retrying timed-out claim deletecollection and count requests`

**Description:**
````markdown
#### What this PR does / why we need it:
The sync Kubernetes client leaves `configuration.retries` unset (`None`) by default, so `urllib3` uses its default `Retry(3)`, whose `DEFAULT_ALLOWED_METHODS` includes `DELETE` and `GET`. When `delete_sandbox_claims_by_label` or `count_live_sandbox_claims` in `_delete_claims_in_rounds` hits a client read timeout (`ReadTimeoutError`), `urllib3` retries the request 3 times with a fresh timeout inside a single SDK call (4 HTTP attempts total), bypassing the release loop's progress check, idle-round accounting, and batch Lease renewal between rounds.

- Generalize `_api_client_without_retries` to `_api_client_with_retries(api_client, retries)` so shallow-copied `ApiClient` instances preserve credentials (including in-place token rotation), cookies, and default headers without mutating an injected client.
- Send `delete_sandbox_claims_by_label` and `count_live_sandbox_claims` through a dedicated `CustomObjectsApi` client configured with `urllib3.Retry(3, read=False)`, surfacing `ReadTimeoutError` and `ProtocolError` after 1 attempt while keeping 3 connection retries wrapped in `MaxRetryError` so `_delete_claims_in_rounds` treats both as transient.
- Leave single-resource calls on `custom_objects_api` so reused keep-alive connections still retry `ProtocolError`, and leave `AsyncK8sHelper` unchanged because `aiohttp` does not retry requests.

#### Which issue(s) this PR is related to:

#### Release Note
```release-note
NONE
```
````

## 5. Links
- Commit (`0a80c60c`): https://github.com/briankhoi/agent-sandbox/commit/0a80c60cee6a3a52e725757f5275993b0218941f
- Compare (`1b061c53..feat/batch-10-deletecollection-retry`): https://github.com/briankhoi/agent-sandbox/compare/1b061c53b2a500d872b2c63f7aa9e1033d93013a...feat/batch-10-deletecollection-retry
- This C3 report: https://github.com/briankhoi/agent-sandbox/blob/batch-primitives-notes/batch-primitives/handoff/work/results/C3-deletecollection-retry.md
