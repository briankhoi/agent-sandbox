# Paste-ready replies for 17/N (#2153) (drafts, not posted)

## `vicentefb` comment (`6618716326`, default `max_in_flight` clamping, sync `api_pool_size()` `0 -> None`, and `README.md`)

> Good catch! Updated the pool-size commit (`7748c892`) in `17/N`:
>
> 1. When the caller omits `max_in_flight` and the bounded pool holds fewer than `22` connections, `claim_batch()` now clamps `max_in_flight` down to `pool_size - 2` and logs at `INFO` (`batch_utils.py:244-277`, `sandbox_client.py:450-456`, `async_sandbox_client.py:612-618`), raising `ValueError` only when an explicit `max_in_flight` exceeds `pool_size - 2` or the pool holds fewer than `3` connections.
> 2. Sync `K8sHelper.api_pool_size()` now maps `0` to `None` like `AsyncK8sHelper.api_pool_size()` (`k8s_helper.py:143-150`), with the matching `test_a_zero_limit_reads_as_unbounded` unit test (`test_k8s_helper.py:1136-1140`).
> 3. Added `pool_size`, the `max_in_flight + 2` requirement, and default clamping next to `max_in_flight=20` in the `README.md` batch claims section (`clients/python/agentic-sandbox-client/README.md:787-796`).

## CodeRabbit thread 1 (`4462715714`, `batch_utils.py:257`, cross-batch pool reservation on `AsyncSandboxClient`)

> `check_pool_size()` intentionally validates single-batch self-consistency (matching the Go SDK's `Burst >= max_in_flight` check) rather than partitioning `pool_size` across `_active_batches`.
>
> The client's `ApiClient` (and any caller-injected `api_client`) serves more than `claim_batch()`, since `get_batch()` handles also hold a watch, run Lease renewals, and issue `acquire()` creates, and `create_sandbox()` calls open per-claim watches on the same connector. Statically reserving `max_in_flight + 2` for the entire lifetime of every active batch would miss `get_batch()`, `create_sandbox()`, and shared injected clients, while falsely clamping or rejecting a 5th or 6th batch on a default `AsyncSandboxClient()` (`pool_size=100`) even when earlier Ready batches have finished their initial fill and hold only 1 watch connection (`95+` idle slots).
>
> Updated the `README.md` `pool_size` guidance to note sizing `pool_size` when running multiple concurrent batches on one client.

## CodeRabbit thread 2 (`4462715719`, `README.md:791`, document `pool_size >= 3` minimum)

> Updated `clients/python/agentic-sandbox-client/README.md` to state that `claim_batch()` clamps an omitted `max_in_flight` when the pool holds at least `3` connections and raises `ValueError` on a smaller pool.
