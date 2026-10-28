# Response to Coordinator 3: `record_pod_identity()` & `is_alive()` in `claim_mode="batch"`

## Decision Summary

**Approve Option (a) (`if claim is None: handle.record_pod_identity()`), augmented with a 0-GET in-memory batch-watch check in `is_alive()` (details below). Do NOT use Option (b) or Option (d), and avoid adding a config knob (Option c).**

Also flag one scale bug in `#1791`'s `record_pod_identity()` implementation (`handles.py:358`) that you should be aware of while touching that code path.

---

## 1. Empirical GKE Scale Data (Measured 2026-10-28 at `N = 1,280` Sandboxes)

On a 28-node GKE cluster (`24 × n2-standard-8` gVisor sandbox pool, `404` APF seats), steady-state `w1280` (`1,280` concurrent rollouts across `320` warm pools) measured:

| Metric (`w1280` steady state) | `per_claim` (pre-`#1791`) | `batch` (`defaults` / `unbounded`) | Impact of Option (b) on `batch` |
| --- | ---: | ---: | --- |
| **Driver API requests / env** | `4.00` (`5,120` reqs) | **`1.25`** (`1,601` reqs) | Jumps **+80%** to `2.25` (`2,881` reqs) |
| **Concurrent watches** | `1,280` | **`1`** | `1` (unchanged) |
| **`GET`s on `acquire()` hot path** | `2,560` (`2.00/env`) | **`0`** (`0.00/env`) | Adds **`1,280` synchronous unpaced `GET`s** |
| **Driver Python CPU / step** | `105.66 s` | **`10.97 s – 12.53 s`** | Adds 1,280 `V1Pod` JSON parses under the GIL |

Preserving **zero per-sandbox `GET`s on `acquire()`** (`fleet.py:1371`, `README.md:531-532`, `test_batch_claims.py:153`) is a core invariant of `claim_mode="batch"`.

---

## 2. Hidden Scale Hazard in `#1791`'s `record_pod_identity()` (`cluster.py:86-98`)

In `upstream/main` (`355803af`), `record_pod_identity()` (`handles.py:358`) calls:

```python
pod = self._cluster.exec_core_api().read_namespaced_pod_status(
    self.pod_name, self._cluster.namespace, _request_timeout=(3, 10)
)
```

`Cluster.exec_core_api()` (`cluster.py:86-98`) exists specifically because `kubernetes.stream` websocket exec mutates client state and is not thread-safe; it therefore instantiates **a separate `client.ApiClient(self.api_client.configuration)` per worker thread**:
- Every `client.ApiClient()` constructor allocates its own `urllib3.PoolManager` **plus** an internal `multiprocessing.pool.ThreadPool(1)`.
- Under Option (b), fanning out 1,000+ `acquire()` calls across worker threads triggers **1,000+ unpooled TLS handshakes and per-thread `ApiClient` allocations** during `acquire()` — even for callers using HTTP/`sandbox-router`/`sandboxd` (`sandbox.commands.run()`) who never invoke `kubectl exec`!
- *(Side note: plain REST `read_namespaced_pod_status` is thread-safe on `self._cluster.core_api`, which shares the 1,000-connection pool. Even in `per_claim` mode, `record_pod_identity` / `is_alive` would be healthier on `self._cluster.core_api` than `exec_core_api()`, though `batch` still should not pay 1,280 synchronous `GET`s on `acquire()`.)*

---

## 3. Closing Most of Option (a)'s Liveness Gap for **0 `GET`s** via the Batch Watch

Coordinator 3 identified two mid-episode wipe scenarios missed when `(pod_uid, runtime_restarts)` is `(None, None)`:
1. **Pod evicted/preempted and recreated by the `Sandbox` controller under the same `pod_name`** (new `pod.metadata.uid`).
2. **Runtime container OOM-killed and restarted in place by kubelet** (`restartCount` increments, same `pod.metadata.uid` and `podIP`).

Notice that **Scenario 1 (and any readiness loss seen by the watch) is already tracked in memory by `SandboxBatch`'s single shared `SandboxClaim` watch at zero API cost**:
- Look at `k8s_agent_sandbox/batch_state.py:175-188` (`still_reaches(member, sandbox_name, pod_ip)`): when a pod is evicted and recreated under the same name, its `SandboxClaim` transitions `Ready=False` / `LOST` or reports a new `podIP` on `SandboxClaim.status.sandbox.podIPs` (`sandbox_batch.py:439-442`).
- If `SandboxHandle` holds a reference to its `BatchClaim` (or records `(claim_name, sandbox_name, initial_pod_ip)` and checks the batch's live `Member` snapshot / a watch-latched `disrupted` bit when `MemberState != READY` or `select_pod_ip(member.pod_ips) != initial_pod_ip`), `is_alive()` immediately detects pod replacement and claim loss with **0 `GET`s at `acquire()`**.

What remains without an `acquire()`-time `Pod` `GET` is strictly **sub-reconcile in-place container restart (`restartCount` `0 -> 1`) on non-recycled handles** (`recycle.py` already catches wiped `/testbed` state independently via its `pristine` git-tag check). Documenting `handle.record_pod_identity()` as opt-in for callers who require baseline `restartCount` tracking in `claim_mode="batch"` is clean, explicit, and avoids `BatchClaimConfig` toggle proliferation (`AGENTS.md`).

---

## Concrete Action for Rebase (`feat/rl-batch-claim-mode` onto `upstream/main`)

1. **In `fleet.py` (`_acquire_reserved`):** Guard `if claim is None: handle.record_pod_identity()` so `claim_mode="batch"` keeps **0 `GET`s** on `acquire()`.
2. **In `handles.py` (`is_alive`):** If the handle came from a `BatchClaim`, also return `False` when the batch's watched member is no longer `still_reaches(...)` (e.g., `LOST`, `FAILED`, `sandbox_name` changed, or `pod_ip` changed from the `pod_ip` observed at `acquire()`).
3. **In `handles.py` docstring & `README.md`:** Add the 1-sentence note from Option (a) stating that `claim_mode="batch"` skips the initial `pods/{name}/status` `GET` on `acquire()` to preserve zero-GET fan-out, and callers needing baseline container `restartCount` tracking for in-place OOM detection can call `handle.record_pod_identity()` explicitly.
