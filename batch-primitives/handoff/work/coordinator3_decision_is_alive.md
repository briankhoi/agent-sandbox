# RL batch claim mode decision: `handle.record_pod_identity()` and `is_alive()`

Open design decision for rebasing `origin/feat/rl-batch-claim-mode` (`7252e27f`, 4 commits `5c585caa..7252e27f`) onto `upstream/main` (`355803af`, which merged `#1791` `c70b415b` after `feature/batch-claim`'s Oct 20 sync `#2005`). Written by Coordinator 3, 2026-10-28. Facts are marked **verified** (read in code) or **measured** (from recorded benchmark runs). Recommendations are proposals until Brian approves.

## Context and source

- **Source:** Rebase interaction between `#1791` (`c70b415b`, *"feat(rl): fail fast on claims that cannot start, handle liveness, and an orphan sweep for the reaper"*, on `upstream/main` `355803af`) and `feat/rl-batch-claim-mode` (`35390caf` / `7252e27f`).
- **What `#1791` does on `main` (verified):**
  1. At the end of `SandboxFleet._acquire_reserved()` (`355803af:examples/agent-sandbox-rl/agent_sandbox_rl/fleet.py:1608`), `#1791` calls `handle.record_pod_identity()` unconditionally on every acquired handle (and again in `handle.refresh()` after `resume()`/`restore()`, `handles.py:494`).
  2. `record_pod_identity()` (`355803af:.../handles.py:349-364`) runs one bounded synchronous `GET /api/v1/namespaces/{ns}/pods/{name}/status` (`read_namespaced_pod_status`, `_request_timeout=(3, 10)`) and stores two Pod-only fields on `SandboxHandle`: `self.pod_uid` (`metadata.uid`) and `self.runtime_restarts` (`containerStatuses[runtime].restartCount`). It is best-effort and swallows errors, leaving `(None, None)`.
  3. `handle.is_alive()` (`355803af:.../handles.py:112-136,366-393`) is called automatically by `handle.exec()` after any transport error (`_raise_if_lost`, `:345-346,395-409`) or explicitly by a rollout loop at step boundaries (`:379-380`). It reads `pods/{name}/status` once and returns `False` if the sandbox was lost so `exec()` raises `SandboxLostError`.
- **What `feat/rl-batch-claim-mode` does (verified & measured):**
  1. In `claim_mode="batch"`, `_acquire_reserved()` (`7252e27f:.../fleet.py:1370-1377`) builds `SandboxHandle` from the SDK batch's watched `Member` (`claim_name`, `sandbox_name`, `pod_ips`) with **zero per-sandbox `GET`s** (`# The batch's watch already reported the member's Sandbox and pod IP, so no GETs here`, echoed in `README.md:531-532` and `tests/test_batch_claims.py:153`).
  2. Neither `SandboxClaim.status.sandbox` (`355803af:extensions/api/v1beta1/sandboxclaim_types.go:156-176`) nor the SDK's `Member` (`1b061c53:clients/python/agentic-sandbox-client/k8s_agent_sandbox/models.py:279-293`) carries `pod.metadata.uid` or container `restartCount`. Those fields exist only on the `Pod` object.
  3. On kind at 160 sandboxes/step (**measured**, `pr_board.md:210`), batch mode issued 243 total API requests (~1.5/sandbox, 1 watch) vs 640 for pre-`#1791` per-claim mode (~4.0/sandbox, 160 watches; now ~5.0/sandbox with `#1791`).

## The issue

We cannot get `(pod_uid, runtime_restarts)` from the `SandboxClaim` batch watch, and modifying `is_alive()` without an acquire-time baseline cannot safely recover them:
- Checking `restartCount > 0` inside `is_alive()` without an acquire-time baseline can false-positive on an adopted warm-pool pod whose container restarted while idle before being claimed.
- Detecting an evicted pod recreated by the `Sandbox` controller under the **same `pod_name`** requires comparing `pod.metadata.uid` against the `uid` at `acquire()`.

Without `(pod_uid, runtime_restarts)` recorded at `acquire()`:
- `is_alive()` **still works without crashing** and still catches `404`, `deletionTimestamp`, `Failed`/`Succeeded`, and a runtime container currently in `state.terminated` (`handles.py:124-135`).
- **However**, `is_alive()` misses the two real-world Kubernetes mid-episode wipe scenarios `#1791` added `record_pod_identity()` for (`c70b415b` commit message, verified):
  1. **Container OOM-killed mid-episode:** kubelet restarts the container in place within ~1s (`state` returns to `running`, `restartCount` increments `0 -> 1`).
  2. **Pod evicted or preempted mid-episode:** the `Sandbox` controller recreates a fresh pod under the **same `pod_name`** (`phase` returns to `Running`, new `pod.metadata.uid`).
  - In both cases `/testbed` episode state is wiped, yet `is_alive()` sees a `Running` pod and returns `True`: an open `SandboxSession` surfaces generic `TimeoutError` instead of `SandboxLostError`, and a one-shot `exec()` continues silently on wiped state. (`recycle.py` is unaffected because its reset script independently checks the `/testbed` `pristine` git tag and quarantines wiped sandboxes, verified at `7252e27f:recycle.py:205-215,360-367`.)

Conversely, calling `handle.record_pod_identity()` in `claim_mode="batch"` adds **+1 synchronous, unpaced `GET pods/{name}/status` per acquired sandbox** (doubling warm per-claim RPCs from `1 POST` to `1 POST + 1 GET`, firing 160–1,000 unpaced `GET`s via `exec_core_api()` during fan-out, and dropping batch mode's zero-GET property).

## Options

| Option | What it does | Cost (API / latency / code) | Behavior & remaining risk |
| :-- | :-- | :-- | :-- |
| **(a) Skip Pod `GET` in batch mode (Coordinator 3 proposal)** | Guard `if claim is None: handle.record_pod_identity()` in `fleet.py`. Update 1 sentence in `record_pod_identity()` docstring (`handles.py:351`) and 1 bullet in `README.md` noting batch `acquire()` skips the initial pod-status `GET` and callers needing restart/replacement detection can call `handle.record_pod_identity()`. | **0 GETs** in batch `acquire()` (stays `1 POST` per warm claim); +2 prod lines, +2 doc lines. | Preserves zero-GET fan-out at 160–1,000+ sandboxes. Out of the box in `claim_mode="batch"`, mid-episode OOM restarts and same-name pod recreations are not flagged as `SandboxLostError` unless the caller calls `handle.record_pod_identity()`. |
| **(b) Call `record_pod_identity()` in both modes** | Keep `handle.record_pod_identity()` unconditional in `_acquire_reserved()` for both `"single"` and `"batch"`. Update `fleet.py:1372`, `README.md:531-532`, and `test_batch_claims.py:153` to say batch mode skips the `Sandbox` GET and per-claim watch (keeping the pod-status `GET`). | **+1 synchronous `GET pods/{name}/status` per acquire** (~403 vs ~800 total requests at N=160 on kind; +1,000 unpaced GETs at N=1,000; +1 RTT per acquire). 0 prod lines added in `fleet.py`. | **100% behavioral parity** between `claim_mode="batch"` and `claim_mode="single"` for `is_alive()` and `SandboxLostError` on OOM kills and pod evictions, with no caveats for `fleet.run()` users. |
| **(c) Configurable on `BatchClaimConfig`** | Add `record_pod_identity: bool = False` (or `True`) on `BatchClaimConfig` so callers (including `fleet.run()` users who do not construct handles directly) can choose 0-GET scale vs OOM/eviction `is_alive()` parity. | 0 GETs when `False`, +1 GET/acquire when `True`; ~8 lines across `config.py`, `fleet.py`, docs, and tests. | Gives `fleet.run()` callers control without manual `handle.record_pod_identity()` calls, at the cost of one additional config field (`AGENTS.md` discourages toggle proliferation unless justified). |
| **(d) Heuristic in `is_alive()` (`restartCount > 0`) without baseline `GET`** | Skip `record_pod_identity()` in batch mode and change `_pod_is_live` to treat `restarts > 0` as dead when `expected_restarts is None`. | 0 GETs at acquire; ~4 lines in `handles.py`. | **Not safe:** false-positives on adopted warm-pool pods that restarted once before adoption, and still misses same-name pod recreation (`uid` change where `restarts == 0`). |

## Coordinator 3's current proposal (not approved)

**Option (a)** if `claim_mode="batch"` prioritizes minimum apiserver QPS and zero-GET fan-out at large cluster scale; **Option (b)** if `claim_mode="batch"` must be a strict behavioral drop-in for per-claim mode on mid-episode OOM kills and pod evictions (especially under `fleet.run()`, where user task functions receive the handle *after* `acquire()` could still call `handle.record_pod_identity()`, though built-in harnesses wouldn't by default).

**What would flip (a) to (b) (or (c)).**
1. In RL rollouts (SWE-bench / code execution / tool-use tasks), container OOM kills and node preemptions mid-episode are common enough that silently returning `TimeoutError` (or continuing one-shot `exec()` on a wiped `/testbed`) in `claim_mode="batch"` surprises users who switched a single config flag (`claim_mode="batch"`).
2. Or on real GKE clusters, 1 bounded `GET pods/{name}/status` per acquire (after the claim is already Ready, spread across worker completions or bounded by client pool size) adds negligible latency compared to pod startup / episode execution, while still cutting watches 160x and total claim-lifecycle RPCs roughly in half (~2.5 vs ~5.0 requests/sandbox).
