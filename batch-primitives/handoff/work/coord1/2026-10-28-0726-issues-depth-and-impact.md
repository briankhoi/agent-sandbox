# Restored investigation depth and impact details in Issues 1–3

Request from Brian:
- "sorry, this is one more nit. i would appreciate it if you could keep the \"cool\" parts in the issues, do you know what i mean? like showing that for issue 1 for example, we investigated and it is a memory leak, not due to allocator fragmentation. just things that show depth or impact."

## What was restored cleanly in `issues-final.md`

- **Issue 1 (`#2082`, [`issues-final.md#L5`](https://github.com/briankhoi/agent-sandbox/blob/batch-primitives-notes/batch-primitives/handoff/work/results/issues-final.md#L5)):** Restored the `MALLOC_ARENA_MAX=2` experiment (`94.5%` of default slope, ruling out glibc arena fragmentation and proving live object retention), the `memray` attribution (`84%` of tracked bytes in `ssl.create_default_context` under `AsyncSandboxConnector.__init__`), the `1,660 MiB` glibc live-heap figure (`0.68 MiB` per cycle), the `0.044 MiB` sync comparison, and the redundant exit `DELETE` per retained terminated handle.
- **Issue 2 ([`issues-final.md#L62`](https://github.com/briankhoi/agent-sandbox/blob/batch-primitives-notes/batch-primitives/handoff/work/results/issues-final.md#L62)):** Restored why CPython's container-count GC thresholds miss `~0.7 MiB` of native OpenSSL state, the 40-worker `kind` measurement (`29.3 MiB` per 100 cycles and `309 MiB` glibc heap peak vs `0.46 MiB` per 100 cycles once close breaks the cycle, phrased with zero cross-issue reference), and the 60-second idle retention proof (`0.66 MiB` -> `0.0001 MiB` per handle).
- **Issue 3 ([`issues-final.md#L111`](https://github.com/briankhoi/agent-sandbox/blob/batch-primitives-notes/batch-primitives/handoff/work/results/issues-final.md#L111)):** Restored why naive `atexit.unregister()` in `close()` or `__exit__` breaks failed-close retry and post-close client reuse, plus the `urllib3` `weakref.finalize` cleanup-state binding comparison.
