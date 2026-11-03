# Paste-ready replies for 18/N (#2154) (drafts, not posted)

## CodeRabbit thread 1 (`4450322585`, `clients/python/agentic-sandbox-client/k8s_agent_sandbox/k8s_helper.py:145`, `api_pool_size()` default pool check on `<= 4`-CPU hosts)

> Owning PR: `17/N` (#2153, `17930115`).
>
> We intended this behavior. When the caller omits `pool_size`, the sync `kubernetes` client sets `urllib3`'s `HTTPConnectionPool` `maxsize` to `multiprocessing.cpu_count() * 5` (`block=False`). Once concurrent requests (`max_in_flight` claim creates and deletes plus the batch watch and Lease renewal) exceed `maxsize`, `urllib3` opens a one-off TLS connection for each overflowing request, logs `"Connection pool is full, discarding connection"`, and closes the socket after a single response (`batch_utils.py:68-74`). Raising `ValueError` up front in `claim_batch()` on `<= 4`-CPU hosts instead of silently churning TLS handshakes gives the caller both concrete remedies right away (`Lower max_in_flight, or construct the client with pool_size=22 or more`, `batch_utils.py:269-283`). `or None` for `0` is also unnecessary because `validate_pool_size` in `K8sHelper.__init__` rejects `pool_size <= 0` (`k8s_helper.py:127`, `utils.py:107-116`).
