# Paste-ready replies for 17/N (#2153) (drafts, not posted)

## `vicentefb` comment (`6618716326`, default `max_in_flight` clamping, sync `api_pool_size()` `0 -> None`, and `README.md`)

> Good catch! Updated the pool-size commit (`b33e2a1e`) in `17/N`:
>
> 1. When the caller omits `max_in_flight` and the bounded pool holds fewer than `22` connections, `claim_batch()` now clamps `max_in_flight` down to `pool_size - 2` and logs at `INFO` (`batch_utils.py:244-277`, `sandbox_client.py:450-456`, `async_sandbox_client.py:612-618`), raising `ValueError` only when an explicit `max_in_flight` exceeds `pool_size - 2` or the pool holds fewer than `3` connections.
> 2. Sync `K8sHelper.api_pool_size()` now maps `0` to `None` like `AsyncK8sHelper.api_pool_size()` (`k8s_helper.py:143-150`), with the matching `test_a_zero_limit_reads_as_unbounded` unit test (`test_k8s_helper.py:1136-1140`).
> 3. Added `pool_size`, the `max_in_flight + 2` requirement, and default clamping next to `max_in_flight=20` in the `README.md` batch claims section (`clients/python/agentic-sandbox-client/README.md:787-797`).

## CodeRabbit thread 1 (`4462715714`, `batch_utils.py:257`, cross-batch pool reservation on `AsyncSandboxClient`)

> `check_pool_size()` intentionally validates single-batch self-consistency (matching the Go SDK's `Burst >= max_in_flight` check) rather than subtracting `max_in_flight + 2` for every open batch in `_active_batches`.
>
> Subtracting `max_in_flight + 2` across open batches treats a short create burst as a permanent lifetime reservation, which breaks normal multi-batch usage on default settings. A batch uses up to `max_in_flight` (`20`) connections briefly during claim creation or deletion bursts, then holds only `1` connection (its watch) while workloads run for up to `work_budget`. On a default `AsyncSandboxClient()` (`pool_size=100`), 4 Ready batches whose initial fills already finished hold only `4` connections (`96` idle slots), yet permanently reserving `22` slots per open batch locks up `88` slots on paper, clamps a 5th batch down to `10`, and rejects a 6th batch with `ValueError` while `95` real connections sit free.
>
> `claim_batch()` also cannot see all traffic sharing that `ApiClient` anyway, since `get_batch()` handles hold a watch, renew Leases, and issue `acquire()` creates without calling `claim_batch()`, `create_sandbox()` calls open per-claim watches on the same connector, and callers can share an injected `api_client` across clients. Callers multiplexing concurrent batches onto one client size `pool_size` for their combined load, which `README.md` (`b33e2a1e`) now notes explicitly.

## CodeRabbit thread 2 (`4462715719`, `README.md:791`, document `pool_size >= 3` minimum)

> Updated `clients/python/agentic-sandbox-client/README.md` (`b33e2a1e`) to state that `claim_batch()` clamps an omitted `max_in_flight` when the pool holds at least `3` connections and raises `ValueError` on a smaller pool.
