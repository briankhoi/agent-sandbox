# Paste-ready replies for 18/N (#2154) (drafts, not posted)

## CodeRabbit thread 1 (`4450322585`, `clients/python/agentic-sandbox-client/k8s_agent_sandbox/k8s_helper.py:145`, `api_pool_size()` default pool check on `<= 4`-CPU hosts)

> Owning PR: `17/N` (#2153).
>
> Fixed this in `17/N` (#2153, `b33e2a1e`), and `18/N` now sits on top of it (`63333e86`). When the caller omits `max_in_flight` and the client's connection pool holds fewer than `22` connections (for example the sync `kubernetes` client's `cpu_count() * 5` default on `<= 4`-core machines), `claim_batch()` now clamps `max_in_flight` down to `pool_size - 2` and logs at `INFO`, raising `ValueError` only when an explicit `max_in_flight` exceeds `pool_size - 2` or the pool holds fewer than `3` connections. `K8sHelper.api_pool_size()` also maps `0` to `None` to match `AsyncK8sHelper.api_pool_size()`, since `urllib3` backs `HTTPConnectionPool` with `queue.LifoQueue(maxsize)` where `0` means unbounded.
