# Paste-ready replies for 18/N (#2154) (drafts, not posted)

## CodeRabbit thread 1 (`4450322585`, `clients/python/agentic-sandbox-client/k8s_agent_sandbox/k8s_helper.py:145`, `api_pool_size()` default pool check on `<= 4`-CPU hosts)

> Owning PR: `17/N` (#2153).
>
> Fixed this in `17/N` (#2153, merged into `feature/batch-claim` as `6feedc5c`), and `18/N` is now rebased onto `6feedc5c`. When the caller omits `max_in_flight` and the client's connection pool holds fewer than `22` connections (for example the sync `kubernetes` client's `cpu_count() * 5` default on `<= 4`-core machines), `claim_batch()` now clamps `max_in_flight` down to `pool_size - 2` and logs at `INFO`, raising `ValueError` only when an explicit `max_in_flight` exceeds `pool_size - 2` or the pool holds fewer than `3` connections. `K8sHelper.api_pool_size()` also maps `0` to `None` to match `AsyncK8sHelper.api_pool_size()`, since `urllib3` backs `HTTPConnectionPool` with `queue.LifoQueue(maxsize)` where `0` means unbounded.

## CodeRabbit thread 2 (`4462893359`, `clients/python/agentic-sandbox-client/README.md:796`, aggregate pool sizing for concurrent batches)

> Owning PR: `17/N` (#2153, merged into `feature/batch-claim` as `6feedc5c`), so this hunk is no longer in `#2154` now that the branch is rebased onto `6feedc5c`.
>
> Requiring `sum(max_in_flight + 2)` across every active batch also overstates steady-state pool usage. A batch uses up to `max_in_flight` connections only during short create or delete bursts; once Ready, it holds `1` watch connection continuously while `_renew_loop` sleeps between renewals and borrows `1` connection for a few milliseconds per `PUT`. Telling callers holding 9 Ready batches while claiming a 10th to provision `10 * 22 = 220` connections instead of sizing for overlapping bursts (~`31`) would over-provision idle handles, so `README.md` intentionally keeps the general concurrent-batch sizing note.
