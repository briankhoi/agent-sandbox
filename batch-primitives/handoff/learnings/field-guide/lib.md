<a id="sec-lib"></a>

[Learnings home](../../learnings.md) · [Field guide index](../../learnings.md#sec-field-guide)

# HTTP client and Python runtime quirks

The batch SDK talks to Kubernetes through four libraries: `urllib3` (the HTTP pool under the sync `kubernetes` client), `aiohttp` (the HTTP stack under `kubernetes_asyncio`), and the two Kubernetes clients themselves. Most bugs in this area came from trusting a library's name, docstring, or default instead of what its code actually does. Libraries wrap exceptions in surprising ways, retry and sleep without asking, and behave differently between sync and async versions. The one big idea: read the library code (and measure when it's cheap) for every exception you catch, every retry you rely on, and every timeout you set. Entries go from the most general rules about errors and retries to narrower quirks of specific helpers.

<a id="lib-1"></a>

## LIB-1 Verify which exceptions actually escape a client library

**Takeaway:** Don't trust a docstring or a class name to tell you what a library raises. Read its code and, if cheap, trigger the failure for real, before you write `except` clauses or document error behavior.

<details>
<summary>The full story (2/N, 8/N, 10/N 74cd8fa5)</summary>

- **Situation:** `get_batch`'s docstring (as of 2/N) said a network failure raises `ApiException` (the Kubernetes client's error type for HTTP failures).
- **What goes wrong:** That was not true. Sync `kubernetes` wraps only an *unretried* urllib3 `SSLError` into `ApiException(status=0)`. With default retries, urllib3 raises `MaxRetryError` first (measured against an expired certificate). Async `kubernetes_asyncio` wraps nothing from aiohttp, so raw aiohttp exceptions escape. A plausible docstring fix (`ApiException ... or a TLS failure`) was also wrong and got reverted.
- **What we do:** Every transport-error predicate and docstring is based on what the code and a real failure showed, not on the library docs (10/N rewrote the `get_batch` docstring to list the transport errors a failed request raises). A TLS EOF (`ssl.SSLEOFError`, the peer closing the encrypted connection early) mid-stream is a dropped connection, not a certificate problem, so the watch retries it like a reset while other TLS failures stay fatal (8/N). [LIB-2](#lib-2) covers how the SDK finds that EOF inside the wrappers.
- **Example:**
  ```
  sync, default retries:  expired cert -> MaxRetryError(SSLError(...))
  sync, retries=False:     expired cert -> ApiException(status=0)
  async:                   expired cert -> aiohttp.ClientConnectorCertificateError (unwrapped)
  ```
- **Related:** [LIB-2](#lib-2), [LIB-3](#lib-3), [LIB-4](#lib-4).

</details>

<a id="lib-2"></a>

## LIB-2 Unwrap wrapper exceptions to the root cause before matching a transient-error list

**Takeaway:** If your "retry these" list contains wrapper exception types, unwrap to the root cause before you match, or you will retry fatal errors forever (or give up on transient ones). List every wrapping form your HTTP stack produces in one table-driven test.

<details>
<summary>The full story (2/N 77c05c26, 8/N c56f0791, 19/N)</summary>

- **Situation:** The watch, creates, and deletes retry only network-level failures. The transient tuple `_TRANSPORT_ERRORS` holds wrapper types. Sync has `MaxRetryError`, which urllib3 raises around a refused or DNS-failed connect after its own retries (2/N made this retryable for the watch). Async has `aiohttp.ClientConnectionError`. TLS failures arrive wrapped too: `MaxRetryError(SSLError(x))` at connect, urllib3 `SSLError(SSLEOFError)` mid-stream (urllib3's response `_error_catcher` turns a `BaseSSLError` into `SSLError(e)`), and aiohttp `ClientConnectorSSLError` / `ClientConnectorCertificateError`, which are `ClientSSLError` subclasses of `ClientConnectionError`.
- **What goes wrong:** Matching on the wrapper alone gets it wrong both ways. A certificate failure matches `MaxRetryError` or `ClientConnectionError` and retries forever. Or a load balancer's TLS EOF during a control-plane upgrade looks fatal. Before 8/N, the sync watch stopped with `err()=MaxRetryError(SSLEOFError)`, even though the same drop seen as a connection reset was retried.
- **What we do:** `sandbox_batch.py: _is_transport_error` takes `MaxRetryError.reason` if present. If that is a urllib3 `SSLError`, it replaces the error with `args[0]` (the real `ssl` exception) and only then checks the tuple, which includes `ssl.SSLEOFError`. `async_sandbox_batch.py: _is_transport_error` first excludes `BatchError` ([LIB-5](#lib-5)), then unwraps `aiohttp.ClientSSLError.os_error` before the tuple check. A certificate-verify error then matches nothing and surfaces through `err()`. An EOF matches `SSLEOFError` and retries. Async also treats `aiohttp.ClientPayloadError` (a truncated body) and `asyncio.TimeoutError` (its own client timeout, [LIB-10](#lib-10)) as transient. Tests `test_connection_failure_and_tls_eof_retry_but_certificate_errors_stop` and `test_ssl_error_surfaces_through_err_and_stops` (both shells) cover this. The sync retry test feeds `MaxRetryError(refused)`, `MaxRetryError(SSLError(EOF))`, `SSLError(EOF)`, bare `SSLEOFError`, and finally `MaxRetryError(SSLError(CertVerify))` as the fatal case. The async one feeds the aiohttp forms: `ClientConnectorError(refused)`, `ClientConnectorSSLError(EOF)`, bare `SSLEOFError`, and finally `ClientConnectorCertificateError` as the fatal case.
- **Example:**
  ```python
  cause = e.reason if isinstance(e, MaxRetryError) else e
  if isinstance(cause, urllib3.exceptions.SSLError):
      e = cause.args[0] if cause.args else cause
  return isinstance(e, _TRANSPORT_ERRORS)
  ```
- **Related:** [LIB-1](#lib-1), [LIB-4](#lib-4), [LIB-5](#lib-5), [LIB-10](#lib-10); [RETRY-1](retry.md#retry-1) (watch error classification); [PAT-26](../patterns.md#pat-26).

</details>

<a id="lib-3"></a>

## LIB-3 Changing a client's retry setting changes which exceptions it raises

**Takeaway:** A transport client's retry config is part of its exception surface. Whenever you change `retries`, re-audit every exception tuple that sits behind that client.

<details>
<summary>The full story (3/N, 13/N 8ad5a407, pre-squash 3bb66de3)</summary>

- **Situation:** The sync Lease client sets `retries=False` ([LIB-6](#lib-6)). `_TRANSPORT_ERRORS` was written assuming urllib3 always wraps connection failures into `MaxRetryError`.
- **What goes wrong:** With `retries=False` on urllib3 2.x, a refused or timed-out connect raises a raw `ConnectTimeoutError` or `NewConnectionError`, not `MaxRetryError`. Nothing in the tuple matched, so `release()` stopped on the first transient blip.
- **What we do:** Add `urllib3.exceptions.ConnectTimeoutError` to sync `_TRANSPORT_ERRORS` in `sandbox_batch.py`. It is the superclass of `NewConnectionError` and `NameResolutionError`, so one entry covers all three (13/N, `3bb66de3`).
- **Example:**
  ```
  Retry(3):       refused connect -> MaxRetryError(NewConnectionError)  (in tuple)
  retries=False:  refused connect -> NewConnectionError                  (needed ConnectTimeoutError)
  ```
- **Related:** [LIB-4](#lib-4) (another surface change from `retries=False`), [LIB-6](#lib-6), [LIB-8](#lib-8).

</details>

<a id="lib-4"></a>

## LIB-4 A sentinel status like `ApiException(status=0)` must be handled by every "retryable by status" check

**Takeaway:** When a library turns transport errors into its own API exception with a fake status, every predicate that decides "retryable" by status must recognize that sentinel. Otherwise two clients with different retry configs disagree about the same network failure.

<details>
<summary>The full story (3/N, 8/N, 12/N; inferred)</summary>

- **Situation:** The sync Lease client disables urllib3 retries, so urllib3 re-raises a connect-time `SSLError` (for example `SSLError(SSLEOFError)`) directly. `kubernetes/client/rest.py: RESTClientObject.request` catches `urllib3.exceptions.SSLError` and raises `ApiException(status=0)`.
- **What goes wrong:** `sandbox_batch.py: _is_transient_error` checks `ApiException` first and calls `batch_utils.is_retryable_status(error.status)`, which accepts only `None`, 429, or 5xx. Status 0 is not retryable. So a load balancer's TLS EOF during a release round's Lease write (`_take_batch_lease`, `_delete_batch_objects`, `_delete_claims_in_rounds`) would be fatal, while the same EOF on the watch is retried, which was 8/N's intent. The predicate code is verified. That this path is reached in practice is inferred from reading the library, not measured.
- **What we do:** Nothing yet; this is an open gap. A fix would classify `ApiException(status=0)` by its reason, or unwrap it the way `_is_transport_error` does.
- **Related:** [LIB-1](#lib-1), [LIB-2](#lib-2), [LIB-3](#lib-3), [LIB-6](#lib-6).

</details>

<a id="lib-5"></a>

## LIB-5 Domain `TimeoutError` subclasses match `asyncio.TimeoutError` on Python 3.11+

**Takeaway:** If a domain exception inherits from a built-in like `TimeoutError` or `OSError` for caller convenience, exclude your domain base class explicitly in every internal "is this a transport blip?" check and every broad `except Exception:` wrapper along its path.

<details>
<summary>The full story (19/N)</summary>

- **Situation:** `BatchTimeoutError(BatchError, TimeoutError)` subclasses the built-in `TimeoutError`, so callers can catch SDK timeouts with either `except TimeoutError:` or `except BatchError:`. Meanwhile `async_sandbox_batch.py: _is_transport_error` checks `isinstance(error, asyncio.TimeoutError)`.
- **What goes wrong:** On Python 3.11+, `asyncio.TimeoutError` is just an alias of the built-in `TimeoutError`. When `_send_create` raised `BatchTimeoutError` inside `_create_one` after the deadline expired while waiting for a `max_in_flight` slot, `_is_transport_error` matched it as a retryable socket blip. A broad `except Exception:` create-failure wrapper in `acquire()` would also have miswrapped it as `TerminalMemberError(CreateFailed)` unless `BatchError` was re-raised first.
- **What we do:** `async_sandbox_batch.py: _is_transport_error` returns `False` for any `BatchError` before the tuple check, and `acquire()` has `except BatchError: raise` ahead of the generic create-failure wrapping (19/N).
- **Example:**
  ```python
  class BatchTimeoutError(BatchError, TimeoutError): ...
  isinstance(BatchTimeoutError(), asyncio.TimeoutError)  # True on 3.11+
  ```
- **Related:** [LIB-2](#lib-2); [CANCEL-2](cancel.md#cancel-2) (keep `CancelledError` out of retry classification).

</details>

<a id="lib-6"></a>

## LIB-6 Transport-level retries break conditional writes and deadline bounds

**Takeaway:** Turn off library retries wherever your own code does conflict-aware or deadline-aware retries. A retried write whose first attempt already landed comes back as a conflict, and each retry gets a fresh timeout that breaks your deadline.

<details>
<summary>The full story (3/N 31a47728, 13/N 8ad5a407 (pre-squash 3bb66de3), generalized in 22/N)</summary>

- **Situation:** A Lease is a small Kubernetes object the batch renews to prove it is alive. Renewal is an optimistic-concurrency write: a PUT that carries the `resourceVersion` (the object's version number) it last saw, which the apiserver rejects with 409 Conflict if someone else wrote in between. `sandbox_batch.py: SandboxBatch._renew_request_timeout` bounds each renewal request by `min(renew interval, time left before expiry)`. urllib3's default `Retry` retries timed-out requests with a fresh timeout.
- **What goes wrong:** A renewal bounded to `min(interval, left)` could stretch to about four times that bound, past Lease expiry (3/N commit). Worse, if the first PUT landed but its response was lost, the retry hits a `resourceVersion` conflict on the SDK's own write, and the renewal logic reads that 409 as "someone else wrote the Lease."
- **What we do:** `k8s_helper.py: _api_client_without_retries(api_client)` builds one unretried `ApiClient` (`_UnretriedApiClient` when wrapping an injected client) in `K8sHelper.__init__` and shares it across `coordination_v1_api` and `_batch_custom_objects_api` (22/N), so each Lease and batch request goes out once and `SandboxBatch` owns retry, backoff, and conflict re-reads. The docstring says it directly: "a retried Lease write that already landed comes back as a conflict." aiohttp does not retry, so the async shell uses the shared client and needs no counterpart. The exception-surface follow-up is [LIB-3](#lib-3) (13/N). Test: `test_lease_requests_are_not_retried_and_keep_the_injected_client_settings`.
- **Example:**
  ```
  renewal timeout=10s, urllib3 Retry(3): 10+10+10+10 = 40s, past the time left on the Lease

  t0  PUT Lease rv=41  -> apiserver stores rv=42, response lost (read timeout)
  t1  urllib3 retries PUT rv=41 -> 409 Conflict
  t2  renewal sees 409 and concludes another holder wrote
  ```
- **Related:** [LIB-3](#lib-3), [LIB-4](#lib-4) (exception changes this caused), [LIB-7](#lib-7), [LIB-8](#lib-8), [LIB-21](#lib-21); [BP-23](../best-practices.md#bp-23) (unretried Lease and batch client); [LEASE-2](lease.md#lease-2) (Lease writes use PUT).

</details>

<a id="lib-7"></a>

## LIB-7 `urllib3.Retry` sleeps on `Retry-After` even when `status_forcelist` is unset

**Takeaway:** Turning off status or read retries in `urllib3.Retry` does not stop it from sleeping and retrying on a `Retry-After` header for 413/429/503. Disable `urllib3` retries (`retries=False`) or pass `respect_retry_after_header=False` whenever your own code owns deadlines, Lease renewal, or `Retry-After` capping.

<details>
<summary>The full story (22/N)</summary>

- **Situation:** An early 22/N draft configured claim collection calls with `urllib3.Retry(3, read=False)` so urllib3 would not repeat timed-out GET or DELETE reads inside one SDK call.
- **What goes wrong:** `urllib3.Retry` defaults to `respect_retry_after_header=True` (`RETRY_AFTER_STATUS_CODES = {413, 429, 503}`, `DEFAULT_RETRY_AFTER_MAX = 21600s`), and `Retry.is_retry()` checks that branch independently of `status_forcelist`. On a 429 or 503 carrying `Retry-After`, urllib3 sleeps without interruption for up to 6 hours and retries 3 times inside one SDK call. That happens before `_delete_claims_in_rounds` can check `guard()`, cap backoff at 30s, or renew the 60s batch Lease. Leaving `list_sandbox_claim_objects` on `Retry(3)` also retried read timeouts 3 times (`4 * 30s = 120s`) in `_claim` after Lease creation and before `_renew_loop` started, which outlasts a 60s Lease.
- **What we do:** `k8s_helper.py: _api_client_without_retries` sets `configuration.retries = False` on the shared batch `ApiClient` (`_batch_custom_objects_api` and `coordination_v1_api`), so `Retry-After` responses, read timeouts, and connect timeouts all surface after one attempt (22/N; see [LIB-21](#lib-21)).
- **Example:**
  ```
  Retry(3, read=False) + 503 Retry-After: 3600 -> urllib3 sleeps 3600s, retries, x3
  retries=False        + 503 Retry-After: 3600 -> ApiException(503) after 1 attempt
  ```
- **Related:** [LIB-8](#lib-8), [LIB-9](#lib-9), [LIB-17](#lib-17) (the SDK's own capped Retry-After parsing), [LIB-21](#lib-21).

</details>

<a id="lib-8"></a>

## LIB-8 Split clients at the retry-ownership boundary: default retries for one-shot callers, `retries=False` where the SDK owns retries

**Takeaway:** Separate transport clients by whether the caller above has its own retry and deadline loop, not verb by verb. Keep `urllib3` defaults on standalone one-shot methods that rely on library retries, and route every batch operation through one shared `retries=False` client.

<details>
<summary>The full story (22/N)</summary>

- **Situation:** `K8sHelper` serves both standalone single-sandbox `SandboxClient` calls (`create_sandbox`, `delete_sandbox`) and `SandboxBatch` calls (`claim_batch`, `acquire`, `release`, `_watch_loop`, `_renew_loop`).
- **What goes wrong:** Disabling retries on `K8sHelper.custom_objects_api` would strip default connection and `Retry-After` retries from existing single-sandbox callers that have no outer retry loop. Conversely, leaving any batch custom-object call on `Retry(...)` lets `urllib3` multiply `_request_timeout` across hidden retries or sleep on `Retry-After` past Lease expiry ([LIB-7](#lib-7), [LIB-21](#lib-21)).
- **What we do:** `K8sHelper` keeps two client tiers (22/N, `k8s_helper.py: K8sHelper.__init__`): `custom_objects_api` and `core_v1_api` use the caller's or default `ApiClient` for single-sandbox operations, while `_batch_custom_objects_api` and `coordination_v1_api` share one `unretried_api_client` (`retries=False`). Batch-only methods (`list_sandbox_claim_objects`, `count_live_sandbox_claims`, `delete_sandbox_claims_by_label`, `watch_sandbox_claims`, `get_sandbox_warmpool`, `get_sandbox_template`) always use `_batch_custom_objects_api`, and shared methods (`create_sandbox_claim`, `delete_sandbox_claim`) take `batch: bool = False` so `SandboxBatch` routes through `_batch_custom_objects_api` while `SandboxClient` stays on `custom_objects_api`. `sandbox_batch._TRANSPORT_ERRORS` includes raw `ConnectTimeoutError` and `NewConnectionError` ([LIB-3](#lib-3)). Test: `test_batch_custom_object_requests_do_not_retry_and_keep_injected_settings`.
- **Example:**
  ```
  create_sandbox_claim(..., batch=False) + connect refused -> MaxRetryError (3 retries)
  create_sandbox_claim(..., batch=True)  + connect refused -> NewConnectionError (1 attempt)
  batch call + 503 Retry-After: 3600                       -> ApiException(503) (1 attempt)
  ```
- **Related:** [LIB-3](#lib-3), [LIB-6](#lib-6), [LIB-7](#lib-7), [LIB-9](#lib-9), [LIB-12](#lib-12), [LIB-21](#lib-21).

</details>

<a id="lib-9"></a>

## LIB-9 A urllib3 request timeout bounds each attempt, not the whole call

**Takeaway:** `_request_timeout` in urllib3 caps one attempt. If a caller or Lease deadline matters, disable library retries on that client and recompute each sequential call's `_request_timeout` against both the remaining caller deadline and the remaining Lease budget so sync and async enforce the same total bound.

<details>
<summary>The full story (19/N 2ecdf384, 22/N, 23/N)</summary>

- **Situation:** Warm credit lets creates skip pacing based on a warm pool's ready count, and first-use pool checks in `acquire()` fetch both the `SandboxWarmPool` and its `SandboxTemplate` while holding a `max_in_flight` slot.
- **What goes wrong:** On default `urllib3` `Retry(3)`, `_request_timeout=min(30, remaining)` bounds one attempt rather than the call: urllib3 retries read and connect timeouts with a fresh timeout and sleeps on `Retry-After` ([LIB-7](#lib-7)). Even with `retries=False`, passing a fixed 30s step cap or snapshotting `lease_remaining` once up front lets two sequential GETs (`get_sandbox_warmpool` and `get_sandbox_template`) stall up to 30s each past a short `acquire(timeout=...)` or overrun `_lease_expires_at`.
- **What we do:** `22/N` routes `K8sHelper._get_extensions_object` (`get_sandbox_warmpool` and `get_sandbox_template`) through `_batch_custom_objects_api` (`retries=False`), so one `_request_timeout` bounds the entire sync HTTP attempt just like `aiohttp`'s `ClientTimeout(total=...)` in async. `23/N` (`sandbox_batch.py: SandboxBatch._check_pool`, `_precheck_pools`) passes a `request_timeout` callback (`next_timeout`) into `_precheck_pools` so both GETs recompute `min(BATCH_REQUEST_TIMEOUT_SECONDS, deadline - now, _lease_expires_at - now)` under `_lock` after checking handle and pool health, failing immediately once `deadline` or `_lease_expires_at` passes.
- **Example:**
  ```
  acquire("pool-b", timeout=0.16)
  t0.00  GET warmpool (_request_timeout=0.16) -> returns at t0.08
  t0.08  GET template (_request_timeout=0.08) -> times out at t0.16 -> BatchTimeoutError
  ```
- **Related:** [LIB-7](#lib-7), [LIB-8](#lib-8), [LIB-21](#lib-21); [LOCK-21](lock.md#lock-21); [RETRY-18](retry.md#retry-18) (refresh capped by the leader's deadline).

</details>

<a id="lib-10"></a>

## LIB-10 Set the client timeout just above the server's timeout, and treat it as transient

**Takeaway:** For long calls and long polls, let the server end the request first: give it a server-side timeout and set the client timeout strictly longer. Then only a dead peer hits the client timeout, so you can retry it as transient. Remember that a "total" timeout counts streaming time too.

<details>
<summary>The full story (1/N constants, 2/N 77c05c26, 12/N)</summary>

- **Situation:** A watch (a long-lived HTTP stream of change events) blocks in a socket read, and neither Kubernetes client has a default request timeout. Separately, `deletecollection` and the paged count can run long on a large batch.
- **What goes wrong:** A connection that dies without a FIN (the TCP "I'm closing" packet) blocks the watch forever, and a server timeout alone can't detect it. A client timeout shorter than the server's would turn every healthy 30s stream into a client error. Before 2/N, the async watch treated its own client timeout (`TimeoutError`) as fatal, and a re-list had no timeout at all. For collection calls, a client cut is ambiguous, and with default urllib3 retries it gets resent: at a 5s forced timeout, urllib3 retried a sync `deletecollection` 3 times (C1-pr8-impl.md).
- **What we do:**
  - Watch: `timeout_seconds=_WATCH_TIMEOUT_SECONDS` (30) ends healthy streams so the loop regularly rechecks stop. The client `_request_timeout` is `_WATCH_REQUEST_TIMEOUT` (35 = 30 + `_WATCH_CLOSE_GRACE_SECONDS` 5), so the apiserver closes a healthy stream first and only a dead socket hits the client timeout. Both shells retry that as a transport error (async: `asyncio.TimeoutError` in `_TRANSPORT_ERRORS`). In aiohttp the number becomes `ClientTimeout(total=...)`, which covers the whole stream, which is why the grace must be added on top of the server timeout.
  - Collections: `batch_utils.BATCH_COLLECTION_REQUEST_TIMEOUT_SECONDS = 90`, above the apiserver's 60s request timeout, so the apiserver returns a 504 first, and urllib3 does not retry that. Single-object calls use `BATCH_REQUEST_TIMEOUT_SECONDS = 30`. The release round loop treats the 504 (5xx) as transient and moves to the next round, where the count shows whether deletion continued on the server. Re-lists also use 90s.
  - Tests: `test_get_batch_relist_and_detach_requests_carry_request_timeouts`, `test_client_timeout_retries_from_last_resource_version`, `test_requests_carry_request_timeouts`.
- **Example:**
  ```
  healthy watch: server closes at 30s -> normal end, failures=0
  dead socket:   client raises at 35s -> transport error, back off, resume from rv
  deletecollection > 60s: apiserver 504 at 60s -> next round re-counts
  ```
- **Related:** [LIB-2](#lib-2), [LIB-11](#lib-11); [RETRY-8](retry.md#retry-8) (every batch request carries a timeout, 90s for collections); [BP-20](../best-practices.md#bp-20).

</details>

<a id="lib-11"></a>

## LIB-11 Turn off a client library's built-in watch reconnects when your code tracks position

**Takeaway:** Before you wrap a library's watch or stream helper, read what its "convenience" reconnects do. Turn them off when your own code owns position tracking and recovery, and check how in-band errors surface.

<details>
<summary>The full story (1/N)</summary>

- **Situation:** Both Kubernetes Python clients can watch forever on their own: they silently reconnect from their own tracked `resourceVersion`, and retry once after a 410 Gone (the apiserver saying that version is too old to resume from).
- **What goes wrong:** If the library reconnects by itself, the handle's `rv`, its stop checks, and its 410 re-list reconciliation are all bypassed. Also, a 410 does not arrive as an HTTP status. It arrives as an `ERROR` event inside a 200 stream.
- **What we do:** `k8s_helper.py: watch_sandbox_claims` (and the async helper) pass `timeout_seconds=_WATCH_TIMEOUT_SECONDS`. That switches off the library's internal reconnect and 410 retry in both clients: kubernetes 37.0.1 `Watch.stream` sets `disable_retries = 'timeout_seconds' in kwargs`, and kubernetes_asyncio 36.1.0 `Watch.next` sets `watch_forever = 'timeout_seconds' not in self.func.keywords`. Each stream ends after one server timeout and the SDK's `_watch_loop` decides what to do next. Both libraries turn an in-stream `ERROR` event into `ApiException(status=code)`, which is how a 410 reaches `except ApiException` in `_watch_loop` mid-iteration.
- **Example:**
  ```
  stream yields {type: ERROR, object: {code: 410}}
  -> library raises ApiException(status=410)
  -> _watch_loop sets relist=True
  ```
- **Related:** [LIB-10](#lib-10); [WATCH-1](watch.md#watch-1), [WATCH-3](watch.md#watch-3) (hand-rolled informer, 410 re-list by diff); [RETRY-1](retry.md#retry-1) (watch error classification).

</details>

<a id="lib-12"></a>

## LIB-12 Derive a per-policy client from an injected one by shallow copy, and read headers live

**Takeaway:** When you build a client from one the caller injected, share mutable credential state by reference (shallow copy, delegating properties) and override only the setting you need. A deep copy silently forks credentials that callers rotate in place.

<details>
<summary>The full story (7/N d07e8941, 15/N 113fae28, 22/N)</summary>

- **Situation:** `ApiClient.__init__` builds `rest_client` (the urllib3 PoolManager) from `configuration.retries` at construction. So a different retry policy ([LIB-6](#lib-6), [LIB-8](#lib-8)) needs a separate `ApiClient`, built without mutating the caller's injected client.
- **What goes wrong:** A deep copy of the configuration duplicated the `api_key` / `api_key_prefix` dicts. A caller that rotated its bearer token in place updated the claim and Sandbox APIs but not the Lease API. Renewals failed with 401, and the handle reported the Lease expired while its watch stayed healthy (7/N). Copying `default_headers` and `cookie` once would also miss later `set_default_header` or cookie updates.
- **What we do:** `k8s_helper.py: _UnretriedApiClient` uses `copy.copy(source.configuration)`, which shares the credential dicts, and sets `configuration.retries = False`. It exposes `default_headers` and `cookie` as properties that read `self._source` on every request (15/N). It sets `_source=None` and placeholder fields *before* `super().__init__`, because `ApiClient.__init__` assigns `default_headers` and `cookie` through those setters before `_source` exists. `_api_client_without_retries` returns one `_UnretriedApiClient` shared by `coordination_v1_api` and `_batch_custom_objects_api` (22/N). Test: `TestLeaseApiClient._assert_delegates_to_injected`.
- **Example:**
  ```python
  injected.configuration.api_key['authorization'] = 'rotated'  # Lease client sees it
  injected.set_default_header('X-Rotated', 'token-2')           # visible via property
  ```
- **Related:** [LIB-6](#lib-6), [LIB-8](#lib-8); [TEST-21](../testing.md#test-21) (a test that mutates an injected client after construction); [PAT-22](../patterns.md#pat-22).

</details>

<a id="lib-13"></a>

## LIB-13 urllib3 `release_conn()` returns an unfinished response's dirty socket to the pool

**Takeaway:** Returning a streaming HTTP connection to a pool is only safe after EOF. Check whether your HTTP library closes incomplete streams on release (aiohttp does, urllib3 `release_conn()` does not), and close unfinished responses yourself before releasing their pool slot.

<details>
<summary>The full story (20/N)</summary>

- **Situation:** `k8s_helper.py: K8sHelper.delete_sandbox_claims_by_label` asks for `_preload_content=False` (hand back the raw response unread), drains chunks with `response.stream()`, and calls `response.release_conn()` in `finally:` so an interruption can't leave a pooled connection checked out.
- **What goes wrong:** aiohttp's `BaseConnector._release` checks `protocol.should_close` and closes incomplete transports automatically. urllib3's `HTTPResponse.release_conn()` does not: it returns `self._connection` to `HTTPConnectionPool.pool` without checking whether the body reached EOF. When `KeyboardInterrupt`, `GeneratorExit`, or a mid-chunk error leaves the `for` loop early, `response.closed` stays `False` and `http.client.HTTPConnection` keeps the open response attached with unread bytes on the wire. The next request on that pool fails in `getresponse()` with an unretried `ProtocolError('Connection aborted.', ResponseNotReady('Request-sent'))` (measured on urllib3 2.8.0). Also, `response.stream()` defaults to `decode_content=True`, which decompresses chunks the code throws away.
- **What we do:** Drain with `response.stream(decode_content=False)`. In `finally:`, close an unfinished response first (`if not response.closed: response.close()`), then call `response.release_conn()`. A full drain keeps reusing the keep-alive socket, while a mid-stream exit closes the dirty one (20/N). Closing instead of draining on every call was rejected because it opens a new TLS connection per round.
- **Example:**
  ```python
  try:
      for _ in response.stream(decode_content=False):
          pass
  finally:
      if not response.closed:
          response.close()
      response.release_conn()
  ```
- **Related:** [LIB-14](#lib-14); [PERF-7](perf.md#perf-7) (don't parse the deletecollection body); [BP-15](../best-practices.md#bp-15).

</details>

<a id="lib-14"></a>

## LIB-14 Raw-response mode in kubernetes_asyncio skips the HTTP status check

**Takeaway:** Turning off a client's response decoding can also turn off its error mapping. For each library and version, check whether raw mode still raises on HTTP errors, and re-implement the check where it doesn't.

<details>
<summary>The full story (20/N e6dde9ff)</summary>

- **Situation:** `deletecollection` answers with the full list of deleted objects, and the client used to read and deserialize it only to drop it. That body was the whole 69.4 MB `release()` memory peak at N=5,000, so both helpers switched to `_preload_content=False` and drain raw chunks ([LIB-13](#lib-13)). Measured: release peak 69.4 to 0.2 MB.
- **What goes wrong:** Sync `kubernetes` still raises `ApiException` for non-2xx before returning the raw response. kubernetes_asyncio (33.3 and 36.1) returns the raw aiohttp response with *no* status check. So a 403 or 503 would be drained silently as success. `release()` would lose the immediate 403 and the `Retry-After` that `retry_delay` uses on a 429.
- **What we do:** `async_k8s_helper.py: AsyncK8sHelper.delete_sandbox_claims_by_label` checks `200 <= status <= 299` itself. Otherwise it reads the small error body, decodes it with `errors="replace"` (so a binary proxy 502 page still raises `ApiException` instead of `UnicodeDecodeError`), and raises `ApiException(http_resp=RESTResponse(...))`. It always calls `response.release()` in `finally` (aiohttp closes incomplete transports on release). Tests: `test_delete_sandbox_claims_by_label_drains_the_response_unparsed_and_releases_it`, `test_delete_sandbox_claims_by_label_raises_for_an_error_status_and_releases_the_response`.
- **Example:**
  ```
  sync,  _preload_content=False, 403 -> ApiException(403)
  async, _preload_content=False, 403 -> returns response (looks like success)
  ```
- **Related:** [LIB-13](#lib-13), [LIB-15](#lib-15), [LIB-17](#lib-17), [LIB-19](#lib-19); [PERF-7](perf.md#perf-7) (69.5 MB deletecollection body).

</details>

<a id="lib-15"></a>

## LIB-15 Diff two client libraries on edge semantics and normalize in the helper layer

**Takeaway:** When you mirror one API over two client libraries, compare how they treat raw or streaming responses, malformed stream lines, and "zero means unlimited" settings. Normalize those differences in the helper layer so the sync and async shells stay identical.

<details>
<summary>The full story (12/N 522dad4c, 17/N 6feedc5c, 20/N e6dde9ff)</summary>

- **Situation:** `SandboxBatch` and `AsyncSandboxBatch` must behave the same, but they sit on `kubernetes` and `kubernetes_asyncio`, which differ in small ways.
- **What goes wrong:** Three differences showed up. Raw-response mode skips the status check in async only ([LIB-14](#lib-14)). kubernetes_asyncio yields an undecodable watch line as a `str`, while sync drops it. And both connection pools treat a size of 0 as unbounded (sync `LifoQueue(maxsize=0)`, and aiohttp treats a limit of 0 or `None` as no limit), so a pool-size check that reads 0 literally is wrong.
- **What we do:** `async_k8s_helper.py` checks the deletecollection status itself ([LIB-14](#lib-14)), yields only dict watch events, and closes the watch in `finally` with `await w.close()`. Both helpers' `api_pool_size` map 0 or `None` to `None` ("unbounded") before the `max_in_flight + 2` pool-size check.
- **Example:**
  ```
  connection_pool_maxsize=0  -> api_pool_size() == None -> check passes (unbounded)
  async watch line b'garbage' -> dropped, as sync does
  ```
- **Related:** [LIB-14](#lib-14); [CANCEL-22](cancel.md#cancel-22) (async watch drops undecodable lines); [RETRY-19](retry.md#retry-19) (pool headroom of 2).

</details>

<a id="lib-16"></a>

## LIB-16 Header-based API variants: check every client version can send them, and test empty responses

**Takeaway:** Before you adopt a header-selected API variant, check that every supported client version can send that header per call, and test the empty response, which may serialize a collection as `null`. Weigh the saving against the cost of a second client.

<details>
<summary>The full story (20/N, dropped commits 30e6fb20, d8ec0178)</summary>

- **Situation:** Counting live claims needs only metadata, so asking the apiserver for `PartialObjectMetadataList` (a metadata-only list format chosen with an `Accept` header) looked like a cheaper read, for both the list and `deletecollection`.
- **What goes wrong:** No per-call header works on every supported client. kubernetes 37 honors `_headers={'Accept': ...}`, kubernetes 35 rejects `_headers` with `ApiTypeError`, and kubernetes_asyncio 33/36 overwrite `Accept`. For sync, only a dedicated `ApiClient` with a default `Accept` worked on every version in one code path (async could send it on a direct `call_api`). An empty `PartialObjectMetadataList` also sends `"items": null` where a full list sends `[]`, which crashed `page.get("items", [])` in the benchmark. The saving was small: 2.05 MB (17%) on the timeout path, about 15% of wire bytes, and nothing on the normal path, because each item still carries `managedFields` (server-side bookkeeping of which client set which field).
- **What we do:** The owner dropped both commits and the extra `ApiClient`, and kept the `managedFields` trim (a 50% list peak cut) instead.
- **Example:**
  ```python
  page.get("items", [])   # None when the server sends "items": null
  page.get("items") or [] # safe
  ```
- **Related:** [LIB-12](#lib-12); [PERF-6](perf.md#perf-6) (drop managedFields from each listed claim).

</details>

<a id="lib-17"></a>

## LIB-17 Parse `Retry-After` strictly as ASCII delta-seconds and cap it by your own deadlines

**Takeaway:** Parse server-provided backoff hints strictly (ASCII digits only), cap them by your own liveness deadlines, and fall back to jittered backoff for any form you don't support.

<details>
<summary>The full story (12/N 522dad4c)</summary>

- **Situation:** Creates, release rounds, and the watch honor the server's `Retry-After` header when a request fails, through `batch_utils.py: retry_delay`, which calls `retry_after_seconds` and otherwise falls back to `backoff_delay`.
- **What goes wrong:** Three traps. `'²'.isdigit()` is `True`, but `float('²')` raises. The HTTP-date form of `Retry-After` needs clock-dependent parsing. And an unbounded value (urllib3 allows up to 6 hours, [LIB-7](#lib-7)) would stall the batch past Lease renewal.
- **What we do:** `retry_after_seconds` strips the header and accepts it only if `seconds.isascii() and seconds.isdigit()`, then caps it at `BATCH_BACKOFF_MAX_SECONDS` (30). Anything else, including an HTTP date, returns `None`, and `retry_delay` uses `backoff_delay`: the wait doubles from the base up to the cap and is jittered to a random value between wait/2 and wait, with the exponent capped early so the code never computes a huge `2**n`. Tests: `test_integer_retry_after_wins_and_is_capped`, `test_missing_http_date_or_non_ascii_retry_after_falls_back_to_backoff`.
- **Example:**
  ```
  Retry-After: 5                              -> 5.0s
  Retry-After: 600                            -> 30.0s (capped)
  Retry-After: ²                              -> jittered backoff
  Retry-After: Wed, 21 Oct 2015 07:28:00 GMT  -> jittered backoff
  ```
- **Related:** [LIB-7](#lib-7), [LIB-14](#lib-14) (async deletecollection keeps the `Retry-After` header); [RETRY-2](retry.md#retry-2).

</details>

<a id="lib-18"></a>

## LIB-18 Retrieve the exception of an asyncio single-flight future that may have no awaiters

**Takeaway:** When you create an asyncio Future that may complete with an exception nobody awaits, retrieve the exception in a done-callback so the event loop doesn't log a false "never retrieved" error.

<details>
<summary>The full story (16/N dc9ea439)</summary>

- **Situation:** `async_sandbox_batch.py: AsyncSandboxBatch._check_pool` runs the warm pool precheck single-flight. The first `acquire` for a pool becomes the leader and stores a loop future in `self._prechecks[warmpool]`. Followers wait on `asyncio.shield(check)` under `asyncio.wait_for`. Often there are no followers.
- **What goes wrong:** asyncio logs "Future exception was never retrieved" at garbage collection when a future gets `set_exception` and nobody awaits it. So every failed check with no followers produced a spurious error log.
- **What we do:** Right after creating the future, the leader adds `check.add_done_callback(lambda done: done.cancelled() or done.exception())`. Calling `done.exception()` marks the exception as retrieved (and the `cancelled()` check avoids calling `exception()` on a cancelled future, which would raise). The leader still raises its own error directly (16/N).
- **Example:**
  ```python
  check = asyncio.get_running_loop().create_future()
  # A failed check that no acquire waits for isn't logged as never retrieved.
  check.add_done_callback(lambda done: done.cancelled() or done.exception())
  ```
- **Related:** [LIB-5](#lib-5); [CANCEL-18](cancel.md#cancel-18).

</details>

<a id="lib-19"></a>

## LIB-19 Surface the Status message from an `ApiException` body, not `str(e)`

**Takeaway:** Put the server's structured error message in user-facing state, not the client library's exception repr. Parse defensively, because error bodies from proxies may not be in the API's format.

<details>
<summary>The full story (12/N 522dad4c)</summary>

- **Situation:** A failed create becomes a `CreateFailed` Member whose `message` callers read.
- **What goes wrong:** `str()` of an `ApiException` embeds every response header and the raw body. That makes noisy, oversized messages in `Member.message` and logs, which can carry cookies or audit IDs. Bodies may also be non-JSON (a proxy's HTML page) or JSON that isn't an object.
- **What we do:** `sandbox_batch.py: _create_failure_message` (and its async twin) calls `batch_utils.py: api_status_message(error.body, error.reason)` for an `ApiException` and uses `str(error)` for anything else. `api_status_message` runs `json.loads(body).get("message")`, catches `(ValueError, AttributeError)`, and falls back to `reason` (for example `Forbidden`), then to `''`.
- **Example:**
  ```
  body='{"kind":"Status","message":"sandboxwarmpools \"p\" not found"}' -> 'sandboxwarmpools "p" not found'
  body='<html>502</html>'                                              -> reason
  ```
- **Related:** [LIB-14](#lib-14); [API-29](api.md#api-29).

</details>

<a id="lib-20"></a>

## LIB-20 Isolate `kubernetes.stream` WebSocket clients per thread, and share one `CoreV1Api` pool for REST calls

**Takeaway:** `kubernetes.stream.stream()` mutates `ApiClient` transport state in place to perform WebSocket upgrades, so `exec` needs a dedicated per-thread `ApiClient`. Never route ordinary REST `GET`s through that per-thread `exec` client: doing so allocates a brand-new `urllib3.PoolManager` and TLS socket pool on every worker thread.

<details>
<summary>The full story (WS-B: 53caa99e)</summary>

- **Situation:** `agent_sandbox_rl/cluster.py: Cluster.exec_core_api()` lazily constructs a thread-local `ApiClient` + `CoreV1Api` per OS worker thread so concurrent `kubernetes.stream.stream(...)` WebSocket `exec` sessions cannot race on `ApiClient.request` monkey-patching.
- **What goes wrong:** `handles.py: SandboxHandle.record_pod_identity()` and `SandboxHandle.is_alive()` called `self.cluster.exec_core_api().read_namespaced_pod_status(...)` for plain HTTP REST `GET`s. Across hundreds of `ThreadPoolExecutor` threads, every REST status read allocated a separate `ApiClient` and `urllib3.PoolManager`, multiplying idle TLS sockets and bypassing the shared connection pool.
- **What we do:** Route `read_namespaced_pod_status()` through the shared, thread-safe `self.cluster.core_api` (`53caa99e`), reserving `exec_core_api()` strictly for one-shot and persistent WebSocket `stream()` calls. Pinned in `test_fail_fast.py`.
- **Related:** [LIB-12](#lib-12), [PERF-16](perf.md#perf-16).

</details>

<a id="lib-21"></a>

## LIB-21 Pass `retries=False` rather than disabling `urllib3.Retry` axes piecemeal

**Takeaway:** When an application layer owns retries, backoff, and Lease deadlines, set `retries=False` on one shared transport client instead of tuning `urllib3.Retry(3, read=False, respect_retry_after_header=False)`. Partial retry configs still leave `connect=3` multiplying `_request_timeout` up to fourfold on connect timeouts and allocate an extra connection pool.

<details>
<summary>The full story (22/N)</summary>

- **Situation:** An intermediate 22/N draft disabled read retries and `Retry-After` sleeps on claim collection calls via `urllib3.Retry(3, read=False, respect_retry_after_header=False)` ([LIB-7](#lib-7)) while keeping `coordination_v1_api` on `retries=False` and single-resource batch calls on `Retry(3)`.
- **What goes wrong:** `urllib3.Retry(3, read=False, ...)` keeps `total=3` and `connect=None`, so `ConnectTimeoutError` and `NewConnectionError` still run four attempts inside one SDK call (`4 * _request_timeout`), stretching a Lease-clamped request past `_lease_expires_at` ([TLA-2](../tla.md#tla-2), `neg_retryPastExpiry`). Leaving per-claim batch creates, deletes, watches, and warm-pool GETs on `custom_objects_api` left them exposed to read retries and 6-hour `Retry-After` sleeps as well, and a third `Retry` policy meant a third `urllib3.PoolManager` and socket pool per `K8sHelper`.
- **What we do:** `k8s_helper.py: _UnretriedApiClient` and `_api_client_without_retries` construct one `retries=False` `ApiClient` shared by `coordination_v1_api` and `_batch_custom_objects_api`, and `create_sandbox_claim` / `delete_sandbox_claim` accept `batch=True` so every `SandboxBatch` RPC fails once within `_request_timeout` (22/N). `sandbox_batch._TRANSPORT_ERRORS` already includes `ConnectTimeoutError` and `NewConnectionError` ([LIB-3](#lib-3)), so `SandboxBatch`'s own deadline- and Lease-bounded loops retry transient connection blips cleanly. Pinned in `test_k8s_helper.py: TestLeaseApiClient.test_batch_custom_object_requests_do_not_retry_and_keep_injected_settings`.
- **Example:**
  ```
  Retry(3, read=False, respect_retry_after_header=False) + dead VIP (connect timeout 5s):
    4 * 5s = 20s inside one call, past remaining Lease budget

  retries=False + dead VIP (connect timeout 5s):
    1 * 5s = 5s -> ConnectTimeoutError -> SandboxBatch checks _lease_expires_at first
  ```
- **Related:** [LIB-3](#lib-3), [LIB-6](#lib-6), [LIB-7](#lib-7), [LIB-8](#lib-8), [LIB-9](#lib-9), [LIB-12](#lib-12); [BP-23](../best-practices.md#bp-23).

</details>

<a id="lib-22"></a>

## LIB-22 Bound non-listening TCP sockets send `RST` on Linux (`NewConnectionError`) but drop `SYN` packets silently on BSD/macOS (`ConnectTimeoutError`)

**Takeaway:** Never use a bound-but-non-`listen()`ing socket expecting `NewConnectionError` across platforms, or assert `NewConnectionError` without allowing `ConnectTimeoutError` (subclass of `MaxRetryError`, *not* `NewConnectionError` in `urllib3`!). On BSD/macOS (`xnu`), connecting to a bound, non-listening socket silently drops `SYN` packets until `connect` timeout expires (`F-02`, PR #2188 review).

<details>
<summary>The full story (22/N review & F-02, test_k8s_helper.py:1196-1268)</summary>

- **Situation:** `test_k8s_helper.py:1243` (`test_lease_requests_do_not_retry_and_keep_injected_settings`) bound an ephemeral loopback socket (`sock.bind(("127.0.0.1", 0))`) *without* calling `sock.listen()`, then asserted `with self.assertRaises(urllib3.exceptions.NewConnectionError):` to verify `retries=False` failed in `1` attempt (`~0.2 s` instead of `4 * 0.2 s = 0.8 s`).
- **What goes wrong:**
  1. **OS kernel divergence:** Linux's TCP stack immediately replies `RST` to a `SYN` targeting a bound `TCP_CLOSE` port (`ECONNREFUSED` -> `urllib3.exceptions.NewConnectionError`). Darwin/`xnu` and BSD TCP stacks drop the `SYN` packet without sending `RST`, so `connect()` blocks until `_request_timeout` (`0.2 s`) and raises `urllib3.exceptions.ConnectTimeoutError`.
  2. **`urllib3` exception hierarchy:** In `urllib3`, `ConnectTimeoutError` inherits from `(SocketTimeoutError, MaxRetryError)` (`TimeoutError -> HTTPError`), whereas `NewConnectionError` inherits from `(PoolError, HTTPError)`. `assertRaises(NewConnectionError)` fails hard on macOS!
- **What we do:** Assert `(urllib3.exceptions.NewConnectionError, urllib3.exceptions.ConnectTimeoutError)` in transport no-retry tests. To reproduce BSD silent-`SYN`-drop deterministically on Linux without `iptables`/root, bind a socket with `sock.listen(1)` and exhaust its SYN/accept backlog from filler client sockets without calling `accept()`, or monkeypatch `urllib3.util.connection.create_connection` to raise `socket.timeout("timed out")` after the configured timeout (`test_k8s_helper.py` & `test_batch_fault_injection.py`).
- **Example:**
  ```text
  Linux kernel (bound, no listen): SYN -> immediate RST -> ECONNREFUSED -> NewConnectionError
  macOS/BSD xnu (bound, no listen): SYN -> silent drop  -> socket.timeout -> ConnectTimeoutError
  ```
- **Related:** [LIB-3](#lib-3), [LIB-21](#lib-21), [TEST-20](../testing.md#test-20), [CHAOS-1](../chaos-verification-suite.md#chaos-1).

</details>

<a id="lib-23"></a>

## LIB-23 `kubernetes_asyncio` `_preload_content=False` skips HTTP status checks on `Watch.stream`, drops `429 Retry-After` headers, and clears `w.resp = None` in `Watch.close()`

**Takeaway:** Unlike sync `urllib3`/`kubernetes.watch.Watch`, `kubernetes_asyncio`'s REST client returns the raw `aiohttp.ClientResponse` without raising `ApiException` when `_preload_content=False`. Inspect `resp.status` and `resp.headers` *before* streaming lines, and never read `w.resp` *after* `Watch.stream` exits (`Watch.close()` sets `self.resp = None` in `finally:`, `F-09`).

<details>
<summary>The full story (F-09, async_k8s_helper.py:739-770)</summary>

- **Situation:** `AsyncK8sHelper.watch_sandbox_claims` (`async_k8s_helper.py:739-770`) streams cluster events through `kubernetes_asyncio.watch.Watch().stream(...)`, which passes `_preload_content=False` to `aiohttp`.
- **What goes wrong:** Three `kubernetes_asyncio` quirks compounded in the async watch path:
  1. **Silent HTTP `401`/`403`/`502`/`503` swallow (`F-09`):** Because `RESTClientObject.request` skips `if not 200 <= r.status <= 299: raise ApiException(...)` whenever `_preload_content=False`, `Watch.stream` reads non-JSON HTTP error bodies via `resp.content.readline()`, yields `0` events, and causes `AsyncSandboxBatch._watch_loop` to treat fatal `401`/`403` errors as clean stream EOFs instead of failing watch waiters!
  2. **`Watch.close()` clears `self.resp = None`:** Checking `if w.resp.status >= 400:` *after* `async for event in w.stream(...)` exits always sees `w.resp is None`, because `Watch.stream`'s `finally: await self.close()` releases the response and resets `self.resp = None`. You must wrap the `func` passed to `Watch.stream` to inspect `resp.status` before line iteration (and ensure test doubles implement `await resp.read()` alongside `content.readline()`).
  3. **Dropped `Retry-After` headers on JSON `429` responses (`F-09`):** Even when `Watch.stream` parses a JSON `Status` payload with `"code": 429`, raising a synthetic `ApiException` from the JSON body alone drops `resp.headers` (`Retry-After`), causing `_watch_loop` backoff to ignore apiserver throttle windows (`RETRY-4`).
- **What we do:** Wrap `func` passed to `Watch.stream` inside `AsyncK8sHelper.watch_sandbox_claims` so any `resp.status >= 400` reads the body and raises `ApiException(http_resp=resp)` *before* `Watch.stream` consumes lines, preserving `status`, `reason`, `body`, and `Retry-After` headers.
- **Related:** [LIB-1](#lib-1), [LIB-4](#lib-4), [RETRY-4](retry.md#retry-4), [CHAOS-1](../chaos-verification-suite.md#chaos-1).

</details>

---

[Learnings home](../../learnings.md) · [Field guide index](../../learnings.md#sec-field-guide)
