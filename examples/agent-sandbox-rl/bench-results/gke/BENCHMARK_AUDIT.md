# Independent Benchmark Audit Report (`bench/rl-gke`)

Audited by an independent read-only subagent across all benchmark scripts (`gke_scale_bench.py`, `claim_mode_bench.py`, `run_shapes_bench.py`, `loadtest.py`, `deploy_controller.sh`), SDK/fleet code (`sandbox_batch.py`, `sandbox_client.py`, `fleet.py`, `cluster.py`), and all **103** raw result JSONs.

---

## 1. Overall Verdict: **Fundamentally Sound (Zero Showstopper Bugs)**

No timing skew, concurrency asymmetry, or inter-step contamination invalidates the benchmark's conclusions:

1. **Symmetric stopwatch boundaries (`gke_scale_bench.py:624–710`):**
   * All $N$ worker threads are spawned and parked on `go.wait()` **before** `t0 = time.monotonic()`, `/metrics` scrape, and request/CPU counters start.
   * `acquired_s` stops right when `fleet.acquire(task)` returns; `start_s` stops right after `handle.exec(["true"])`.
   * `fleet.release(handle)` (`DELETE sandboxclaims`) is gated behind `step_end.wait()`, which only opens **after** `step_s`, client counters, and `kube-apiserver` `/metrics` are captured — zero teardown traffic leaks into step measurements on either arm.
2. **Identical client pool, concurrency, & `exec` path (`cluster.py:42`, `handles.py:240`):**
   * Both `per_claim` and `batch` use `build_api_client()` with `connection_pool_maxsize = 1000`, `max_concurrent = N`, `overcommit_factor = 0`, `enable_metrics = False`, and the same thread-local WebSocket `exec` path (excluded identically from `requests/env` on both arms).
3. **Strict inter-step quiescence (`gke_scale_bench.py:533–585`):**
   * Verified in all active JSONs (`gke/`, `gke-tuned/`, `gke-shapes/`): before every step, `wait_state()` confirmed `ready >= want`, `claims == 0`, `terminating == 0`, `pods_in_namespace == want_pods`, and `sandboxes_in_namespace == want_pods` (+ `5s` settle). Even when downsizing from `2,560` (`640×4`) to `w500` (`500×1`), `wait_state` waited `481.9s` until `pods_in_namespace == 500` before starting `w500` Step 1.
   * Arm execution order was interleaved and rotated each round (`interleave()`).

---

## 2. Five Sharp Q&A Nuances Found by the Audit (Keep in Your Back Pocket!)

1. **"If we just added `POST` HTTP `429` retry to `per_claim`, wouldn't `per_claim` also get `0` errors?"**
   * *Audit finding:* `SandboxBatch` retries `POST` `429`s up to 3× (`Retry-After`), whereas `per_claim` fails immediately on `POST` `429` (`urllib3` doesn't retry `POST`).
   * *Why `batch (default)` still wins cleanly:* **`batch (default)` (`max_in_flight=20`) had `0` client HTTP `429`s across all 103 runs** — it never needed a single retry! By contrast, `batch (unbounded)` (`max_in_flight=998`) *did* hit `26` client `429`s at `w2560` Run 2 and jumped from `35.48s → 67.68s` waiting out `Retry-After` (plus `5` dropped sockets on cold Step 1). So adding retries to `per_claim` would still eat `+30s` `Retry-After` stalls, `551` dropped sockets, and `2,560` watches, whereas `batch (default)` avoids client `429`s proactively.
2. **Survivorship bias in `per_claim`'s `All Ready (last)` on errored runs:**
   * Failed tasks raise `ApiException(429)` without appending to `acquired_s`, so `per_claim`'s `34.80s` at `w2560` Run 2 (`n=2559, err=1`) and `30.48s` at `w1024` Run 2 (`n=1022, err=2`) reflect only surviving tasks — making `per_claim`'s raw 2-run median (`46.05s` at `2,560`) look artificially faster than its true clean run (`57.29s`). *(Already noted in [`BENCHMARK_TABLES.md`](./BENCHMARK_TABLES.md) and [`WALKTHROUGH.md`](./WALKTHROUGH.md).)*
3. **"Why does `Watch Reqs` on `batch` sometimes say `0`, `2`, or `4`?"**
   * `SandboxBatch` spawns **at most 1 watch thread (`1` concurrent stream)**. The counter tracks HTTP `GET ?watch=true` requests initiated during `[t0, step_s]`: `0` means the watch opened before Step 2 stayed alive across the entire step without reconnecting; `2–4` means that single watch stream reconnected sequentially `1–3` times from its last `resourceVersion`. *(Column headers in [`BENCHMARK_TABLES.md`](./BENCHMARK_TABLES.md) updated to `Watch Reqs (1 stream on batch)`.)*
4. **"How did `w2560 / batch (unbounded)` get `1.13` API calls/env when `1 + 1/4 = 1.25` is the minimum?"**
   * On `w2560-batch-unbounded` Run 2 Step 1, `341` `429`s latched `WarmCredit.throttled = True` on that `SandboxBatch`, causing Step 2 to skip `GET sandboxwarmpools` refreshes (`1.01` reqs/env in Run 2 averaged with `1.25` in Run 1 = `1.13`). *(Potential future SDK polish: let `WarmCredit.throttled` decay after a quiet cooldown rather than latching forever on a long-lived batch.)*
5. **Why Default-Controller `w160` showed `540` server claim requests vs. Tuned `747`:**
   * After GKE autoscaled to multiple `kube-apiserver` replicas (`404` seats), the Stock controller (`1` shared HTTP/2 connection) landed on Replica B during `gke/w160-batch-*-run1` while the driver scraped Replica A (`187` + `892` / 2 = `540`). All `w500`, `w640`, `w1024`, `w1280`, `w2560`, `c640`, and Tuned `w160` (`--api-connections=4`) runs captured full controller write counts cleanly.
