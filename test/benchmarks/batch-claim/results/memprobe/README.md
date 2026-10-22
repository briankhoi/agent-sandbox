# Async per-claim baseline memory probe

Is the async per-claim baseline driver's steadily rising RSS glibc arena fragmentation or live
growth? This directory has two 2,400-rollout runs of the baseline loop on the base SDK (one as
shipped, one with `MALLOC_ARENA_MAX=2`), each with a malloc probe, and a 600-rollout run under
memray that finds where the memory is allocated. MB here means MiB (2^20 bytes).

## Verdict

**Not arena fragmentation. The growth is live allocations: closed sandbox handles that stay
referenced, each pinning an OpenSSL context.**

- Slope over the second half of the rolling window: 0.507 MB/s as shipped and 0.479 MB/s with
  `MALLOC_ARENA_MAX=2`, so the arena2 run grows at 94.5% of the default run's rate. Per 100
  rollouts (derived): 72.4 and 72.8 MB. Final RSS: 1,752 and 1,749 MB (ratio 0.998). Under the
  rule fixed before the runs (arena2 slope at least 60% of the default run's), this is "not arena
  fragmentation".
- Malloc probe at the end of the rolling phase: glibc's bytes in use (`uordblks`) are 1,660 MB in
  both runs, 95% of RSS. Free bytes held (`fordblks`) are 4.0 and 1.4 MB, and `malloc_trim(0)`
  gave back 0.5 and 0.3 MB. That is live allocations, not fragmentation in any arena. RSS minus
  malloc's arena is about 88 MB in both runs, so pymalloc's arenas are not where the growth is.
- memray (`memray-top.txt`): at the high-water mark of the 600-rollout run, 330.6 of the 391.7 MB
  that memray tracked (84%) were allocated under `ssl.create_default_context`, called from
  `httpx.AsyncHTTPTransport.__init__` (`httpx/_transports/default.py:297`), from
  `AsyncSandboxConnector.__init__` (`k8s_agent_sandbox/async_connector.py:215`), from
  `AsyncSandboxClient.create_sandbox` (`async_sandbox_client.py:349`). That is 0.55 MB per rollout.
  memray's tracked total under-counts the process: at the end of that run glibc reported 994.5 MB
  in use (`malloc_probe` in its result), so the 84% is a share of memray-tracked bytes, not of the
  process.
- Why it is retained (read in the base SDK `7fd162d9`): `create_sandbox` stores each handle in
  `AsyncSandboxClient._active_connection_sandboxes` (`async_sandbox_client.py:368-369`).
  `AsyncSandbox.terminate()` (`async_sandbox.py:213-231`) closes the connection and deletes the
  claim but leaves the handle in that dict. Only `delete_sandbox`, `list_active_sandboxes`,
  `close` and a few other client methods remove entries. Each handle keeps its connector's closed
  `httpx.AsyncClient`, and its transport keeps the SSL context with the loaded CA bundle.
- `httpx_retained.py` measures that cost alone: a closed `httpx.AsyncClient(verify=True)` that
  stays referenced holds 0.70 MB of malloc memory, of which tracemalloc sees 0.02 MB. Run in the
  driver pod (Python 3.12.15, glibc 2.41, httpx 0.28.1) it printed 0.701 MB, and dropping the
  references freed it. The runs retained about 0.68 MB of `uordblks` per rollout ((1,660 - 30.5) MB
  over 2,400 rollouts, subtracting the first sample).
- The same code is on upstream `main` at `840fdfde` (store at `async_sandbox_client.py:370-371`).
- The sync `SandboxClient` stores and keeps handles the same way (`sandbox_client.py:253-254`,
  `sandbox.py:201-220` on `840fdfde`); its cost per handle was not measured here. The batch claim
  path does not use this dict, so it does not hit this retention.

| run | env | rollouts | rolling wall s | rollouts/min | RSS first / last in window / peak MB | 2nd-half slope MB/s | MB per 100 rollouts (derived) | warm pool min Ready | errors |
| --- | --- | ---: | ---: | ---: | --- | ---: | ---: | ---: | --- |
| `6v2-baseline-long-async-run1` | none | 2,400 | 3,427 | 42.0 | 93.8 / 1,752.4 / 1,752.4 | 0.507 | 72.4 | 0 (starved) | 0 |
| `6v2-baseline-arena2-async-run1` | `MALLOC_ARENA_MAX=2` | 2,400 | 3,643 | 39.5 | 93.0 / 1,749.3 / 1,749.3 | 0.479 | 72.8 | 0 (starved) | 0 |
| `6v2-baseline-memray-async-run1` | under memray | 600 | | 34.9 | peak 1,148.5 (inflated by memray) | | | 0 (starved) | 0 |

`rss.png` plots both RSS series and their `uordblks`, with the earlier 600-rollout run
(`../6v2-baseline-async-run1.json`) for reference. `summary.json` has the numbers above and the
probe values.

## Caveats

- All three runs starved the warm pool (0 Ready at times, below 40 Ready in most samples), so
  rollouts waited for cold pods and ran at about 40/min instead of the earlier run's 232/min.
  Both 2,400-rollout runs starved alike, and the growth is per rollout (about 0.69 MB of RSS per
  rollout here, 0.70 MB in the earlier unstarved run), so the comparison holds. The host (8 CPUs, a second kind cluster
  running) is the bottleneck, not the driver.
- One run per arm.
- The memray run's RSS and mallinfo2 include memray's own memory, so they are not compared with
  the other two. Its native frames inside OpenSSL have no symbols on this interpreter, so the
  stacks show Python frames only.

## Setup and provenance

- Cluster `par-mem`, kind v0.32.0, Kubernetes v1.36.1, docker 29.8.0, from `../../kind-config.yaml`
  with the repo mount pointed at this worktree and the kubeadm patch moved to `v1beta4`: kind
  v0.32 renders kubeadm v1beta4, so the file's `v1beta3` patch was dropped (measured: the node got a
  /24 and no `OwnerReferencesPermissionEnforcement`). With `v1beta4` the node has 10.244.0.0/22 and
  both admission plugins.
- Controller and sandboxd images built from `44fb487f` with `dev/tools/push-images
  --image-prefix=kind.local/ --extra-image-tag=bench --kind-cluster-name=par-mem --images
  agent-sandbox-controller sandboxd`, deployed with `dev/tools/deploy-to-kube --image-prefix=kind.local/
  --image-tag=bench --extensions`. Controller image ID
  `docker.io/library/import-2026-10-22@sha256:8be25e6d105f1adf7b892c145cda5b4912d35546559b24774f836fed0de3a92f`.
- Driver in the `bench-driver` pod (`python:3.12-slim`, Python 3.12.15, glibc 2.41) on the base SDK
  `7fd162d9` (tree `588a31ef`), installed as `/opt/venv-base` (`0.0.0+base.7fd162d`,
  `kubernetes_asyncio` 33.3.0). memray 1.20.0 was installed into `/opt/venv-base` after the two
  2,400-rollout runs.
- Shape: `AsyncSandboxClient` with an injected `kubernetes_asyncio` ApiClient
  (`connection_pool_maxsize=1000`), 40 workers, warm pool of 120, hold 10 s. Each rollout does
  `create_sandbox`, `get_pod_name`, `get_pod_ip`, `commands.run("echo ok")`, sleep, `terminate`.
- Harness: the 2,400-rollout runs at `2f1eef29`, the memray run at `1d68fb5a` (each result's
  `bench_head_sha`, `bench_dirty` false). Commands:

```sh
export KUBECONFIG=<par-mem kubeconfig> BENCH_CONTEXT=kind-par-mem BENCH_KIND_NODE=par-mem-control-plane
export BENCH_RESULTS=$PWD/results/memprobe
bench.py setup
bench.py suite --only 6v2-baseline-long-async 6v2-baseline-arena2-async
bench.py suite --only 6v2-baseline-memray-async
```
