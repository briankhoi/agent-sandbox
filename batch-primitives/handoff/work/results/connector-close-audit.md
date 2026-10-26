# Connector close audit (sync vs async), for PR 2 (handle/connector cycle)

Base: upstream/main `f83f2d87`. Paths are under `clients/python/agentic-sandbox-client/k8s_agent_sandbox/`. Scratch: `/tmp/par-audit2/` (not committed).

## One-minute summary
- **The claim is right.** Async `close()` is final. Sync `close()` is also the reset: `send_request` calls it on errors, and the GKE snapshot extension calls it on a live handle during suspend and resume. If the sync callbacks were cleared inside `close()`, resume would break.
- **Is it drift in behavior?** Mostly no. The two shells were different from the first commit: sync `close()` was a reset from day one (#382), and async `close()` was final from day one (#510). Async later made that explicit (`_closed`/`_ensure_open`, #1438), and sync never got the same split, so the gap grew a little later on, but only on the async side.
- **Is it the nature of sync vs async?** No. It comes from the HTTP library. `requests.Session` still works after `close()`. `httpx.Client` refuses requests after `close()`, and it does so in sync mode too (measured).
- **Is it drift in intent or naming?** Yes, and this is the real cause. Sync `close()` does two jobs, reset and close-for-good, under one name. Async splits them.
- **Best practice:** `close()` is final and idempotent, and reconnecting is a separate internal step (Python `io`, httpx, aiohttp, gRPC). requests is the outlier.
- **Best approach for PR 2:** keep it a memory fix. In async, clear the callbacks in connector `close()`. In sync, add one small connector method that clears them, and call it from `Sandbox.close_connection()`. Splitting sync `close()` into reset + final close (with use-after-close refused, like async) is the right cleanup, but it changes behavior and belongs in its own PR.

## Evidence
**Current semantics (verified, file:line on upstream/main)**
- Async connector: `close()` sets `_closed` and is a no-op once `_close_complete` (`async_connector.py:596-643`). `_ensure_open` raises `RuntimeError("sandbox connector is closed")` (`:481-484`) and guards `send_request`, `connect` and `grpc_channel` (`:320, :478, :521, :534`). On errors, async resets inline by clearing cached URLs and the pod IP, and never calls `close()` (`:426-431, :456-461`). The strategy already splits reset `_close_locked` (`:814`, used by connect `:712, :760, :773, :779`) from final `close()` (`:782-788`, sets `_closed`).
- Sync connector: one `close()` (`connector.py:827-844`) with no closed flag. Callers that use it as a reset: `send_request` on `SandboxPortForwardError` (`:968-970`) and on no-response errors outside in-cluster mode (`:994`). The next request reconnects through `self.connect()` (`:887-889`). Strategy `close()` is also a reset: `connect()` calls it before restarting a port-forward (`:276, :433`).
- Another caller of sync `close()` as a reset on a live handle: `gke_extensions/snapshots/sandbox_with_snapshot_support.py:279` (suspend) and `:330` (restore/resume). The handle is used again afterwards. Added in #799 and #957. There is no async snapshot extension.
- Callers that use it as a final close: `Sandbox.close_connection()` (`sandbox.py:191`) and `AsyncSandbox.close_connection()` (`async_sandbox.py:195`).
- Where the cycle edges live: sync connector `:647-649`, plus copies in the strategies at `:401` (pod tunnel), `:532-533` (sandboxd in-cluster) and `:596` (in-cluster). Async connector `:149-150`, plus strategies at `:664` and `:843-844`. Any fix must also clear the strategy copies, or the cycle stays (verified).
- Measured (`/tmp/par-audit2/m2.py`): a sync `SandboxConnector` (direct mode) returns 200 from `send_request` after `close()`. Async raises `RuntimeError: sandbox connector is closed`. A sync executor captured before `close_connection()` therefore keeps working, and an async one fails (inferred from the same paths: executors have no closed check, `commands/command_executor.py:121-201`).

**History (verified, `git log -S` on upstream/main)**
| Date | Commit / PR | Change |
|---|---|---|
| 2026-03-17 | `749a1ce5` #382 | sync `close()` + `send_request` calls `self.close()` on errors, "re-establishes if closed/dead" |
| 2026-04-06 | `e272eee8` #510 | async connector: `close()` = `client.aclose()` (final); errors only clear `_base_url`. No closed flag yet. |
| 2026-06-12 | `a2ff59b1` #957 | snapshot suspend calls sync `connector.close()` as a reset |
| 2026-09-18 | `ac186f1d` #1438 | async only: `_closed`, `_ensure_open`, `_lifecycle_lock`, strategy `_close_locked` vs `close` |
| 2026-09-23 | `e0e56e84` #1697 | async close made retryable (`_close_complete`), also sync port-forward cleanup made retryable. Touched both shells, kept sync `close()` as a reset. |
| 2026-09-28 | `0f5087f5` #1683 | sync strategy lock, "Reentrant: connect() calls close()" |
None of these commit messages discusses close-semantics parity (inferred: messages only, PR threads not read).

**Library behavior (measured on this host: requests 2.34.2, httpx 0.28.1, grpcio, `/tmp/par-audit2/m.py`)**
- `requests.Session` after `close()`: 200. `close()` only clears urllib3's pools ([Session.close](https://requests.readthedocs.io/en/latest/api/#requests.Session.close), [PoolManager.clear](https://urllib3.readthedocs.io/en/stable/reference/urllib3.poolmanager.html)).
- `httpx.Client` and `httpx.AsyncClient` after close: `RuntimeError: Cannot send a request, as the client has been closed` ([httpx clients](https://www.python-httpx.org/advanced/clients/)).
- `grpc` channel after `close()`: `ValueError: Channel closed!` ([Channel.close](https://grpc.github.io/grpc/python/grpc.html#grpc.Channel.close)). Reconnecting is internal to the channel (IDLE/TRANSIENT_FAILURE, [connectivity semantics](https://github.com/grpc/grpc/blob/master/doc/connectivity-semantics-and-api.md)).
- aiohttp: `ClientSession` after `close()` raises `RuntimeError: Session is closed` ([client reference](https://docs.aiohttp.org/en/stable/client_reference.html)), cited, not measured.
- Python `io`: `close()` "has no effect if the file is already closed", and later operations raise ([IOBase.close](https://docs.python.org/3/library/io.html#io.IOBase.close)).
- Pattern: reconnecting is an internal reset (urllib3 replaces dropped pool connections, gRPC reconnects inside the channel), and the public `close()` is final and idempotent. Async in this SDK follows that pattern. Sync `close()` mixes the two.

## Answers to Q1
- (a) Behavior drift (one shell changed later and the other was not): **mostly no.** The shells differed from the start (#382 vs #510). #1438 widened the gap on the async side only, and #1697 touched both shells without aligning them.
- (b) Inherent to sync vs async I/O: **no.** httpx in sync mode is final too. The cause is requests vs httpx.
- (c) Drift in intent or naming: **yes.** Sync `close()` is both the reset (`connector.py:968, :994`, `sandbox_with_snapshot_support.py:279, :330`) and the final close (`sandbox.py:191`).

## Alternatives for PR 2 (simplest first)
| Option | Size | Behavior risk | Parity | Scope | Failure modes / notes |
|---|---|---|---|---|---|
| (vi) leave sync alone | 0 | none | no | n/a | sync handles still need a full GC (the prior audit measured +311 to 459 MiB in async; sync has the same edges) |
| (i) async only, in connector `close()` | ~4 lines | none: a closed async connector already refuses work | no | memory fix | sync leak remains |
| (ii) sync: `Sandbox.close_connection()` clears connector and strategy privates | ~6 lines in `sandbox.py` | captured-executor reuse after close changes (below) | partial | memory fix | the handle must know which strategies hold which fields (3 of 6 types), so a new strategy can be missed. Splits the rule across two classes. |
| (iii) one small connector method that clears its callbacks and the strategy copies. Sync calls it from `close_connection()`, async calls it inside its final `close()` | ~7 lines sync + ~4 async, +1 call | same as (ii) | yes for the rule (both clear at the handle's final close), call sites differ | memory fix | precedent for handle-to-connector private calls: `async_sandbox.py:211` (`_close_for_atexit`). Adds no state and widens no type: the fields are already `Optional`. |
| (iv) split sync `close()` into `_reset()` + final `close()` (+ `_closed`/`_ensure_open`), clear in final `close()` in both shells | ~25-40 lines + tests | **behavior change:** sync use-after-close raises. The snapshot extension (`:279, :330`) must switch to a reset that it can call, so either a public `reset()` or a private call across modules. | full | separate refactor | best long-term shape. Adds state (`_closed`) and touches the extension. One concern per PR says not in a memory fix. |
| (v) no bound methods in the connector (`WeakMethod` wrappers, or pass the helper and names) | weak: ~10 lines + async variant. Restructure: large | weak: getters return `None` if the handle dies first. Restructure: pod-name cache and suspend reset move. | yes | weak: memory fix. Restructure: refactor. | the weak version is a pattern the SDK doesn't use, and it hides when the edge goes away. The restructure moves `_pod_name` state, which is churn. |

Is there a smaller change with the same behavior? For async, (i) is the smallest. For sync, (ii) and (iii) are about the same size, and (iii) states the rule in the one class that owns the fields. Nothing smaller frees sync handles. Clearing inside sync `close()` would be smaller, but it is wrong: it breaks reconnecting and snapshot resume.

## Recommendation
**(iii):** a connector helper (for example `_release_handle_callbacks()`) that sets the connector's `_get_pod_ip`, `_get_pod_name` and `_get_service_fqdn` to `None`, plus the strategy copies, in both shells. Async calls it inside `close()` (final). Sync calls it from `Sandbox.close_connection()` right after `connector.close()`. Test, per shell: with `gc.disable()`, a closed handle is freed on `del` (its `weakref` goes dead). This catches any missed edge, including a strategy copy. Mutation: removing the call must fail the test. Propose (iv) to Brian as a separate parity PR.

Rule the change promises: once a handle's connection is closed for good, nothing the connector owns references the handle. Paths: `close_connection` (both shells), `terminate` (via `close_connection`), async `_close_for_atexit` (process exit, so it can be left alone), async `close()` retried after a partial failure (helper is idempotent), sync `send_request` resets and snapshot suspend/resume (must NOT clear, and do not, because they call `close()`, not the helper).

**What would make this wrong:**
1. If use-after-close of a sync connector through a captured executor must keep working: after (iii), pod-tunnel mode fails with "pod name not resolved" and in-cluster mode falls back to DNS, while direct, gateway and local-tunnel modes keep working. If that inconsistency is unacceptable, do (iv) first.
2. If Brian wants identical code shapes in both shells: also call the helper from `AsyncSandbox.close_connection()` instead of from inside `close()`.
3. If the Python GC starts collecting these cycles promptly (for example 3.14's incremental GC), PR 2 matters less.
